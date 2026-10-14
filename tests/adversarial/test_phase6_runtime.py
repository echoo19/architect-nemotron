"""Independent shared Phase 6 gate tests; all backends are controlled host fixtures."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import JsonValue, ValidationError

from architect.compiler import CompileConfig, Compiler
from architect.compiler.selection import select_preset
from architect.compiler.types import NullLibrary
from architect.floors import EvaluationCase, HeldOutTasks
from architect.models import AdapterError, FakeAdapter, ModelRequest, ModelResponse, UsageQuote
from architect.presets import get_preset
from architect.runtime import BudgetExceeded, RunContext, Runtime, UnlicensedArtifactError
from architect.runtime.evaluation import evaluate_cases
from architect.runtime.evidence import EvaluationScope, current_evaluation, scoped_evaluation
from architect.spec import Budget, FloorDefinition, FloorKind, HarnessArtifact, Spec
from architect.telemetry import TelemetryStore, categorical


def spec(**overrides: object) -> Spec:
    values: dict[str, object] = {
        "task": "Return ok",
        "input_schema": {},
        "output_schema": {"type": "string"},
        "floor": FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="p6-tests"),
        "budget": Budget(cost_usd=1, compute_s=5),
        "domain": "synthesizer",
    }
    values.update(overrides)
    return Spec.model_validate(values)


def adapter(tokens_in: int = 3, tokens_out: int = 7, cost: float = 0.01) -> FakeAdapter:
    return FakeAdapter(
        responses=[
            ModelResponse(
                model="fake",
                text="ok",
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost,
            )
        ]
    )


def runtime(tmp_path: Path) -> Runtime:
    return Runtime(TelemetryStore(tmp_path / "telemetry"))


def floor_factory(definition: FloorDefinition) -> HeldOutTasks:
    return HeldOutTasks(
        definition,
        [EvaluationCase(case_id=f"case-{i}", task={"game": i}, expected="ok") for i in range(4)],
    )


def compiler(tmp_path: Path, **options: object) -> Compiler:
    # Configuration is supplied explicitly by each test; no model-selected policy.
    return Compiler(
        tmp_path / "artifacts",
        runtime(tmp_path),
        lambda model: FakeAdapter(model=model),
        floor_factory,
        config=CompileConfig.model_validate({"max_candidates": 1, **options}),
    )


class Backend:
    def __init__(self) -> None:
        self.seen: list[
            tuple[RunContext, JsonValue, str, dict[str, JsonValue], tuple[str, ...]]
        ] = []

    async def run(
        self,
        context: RunContext,
        task: JsonValue,
        *,
        preset: str,
        params: dict[str, JsonValue],
        components: tuple[str, ...],
    ) -> JsonValue:
        context.check_game_grant()
        self.seen.append((context, task, preset, params, components))
        return "ok"


async def play(context: RunContext, task: JsonValue) -> JsonValue:
    return await context.play_game(task, preset="explorer", params={}, components=("probe",))


async def game_run(service: Runtime, task: JsonValue = None) -> object:
    return await service.evaluate(
        spec(),
        play,
        task,
        FakeAdapter(),
        preset="explorer",
        components=("probe",),
        candidate_digest="a" * 64,
    )


async def test_game_scope_is_runtime_owned_and_context_expires(tmp_path: Path) -> None:
    owner = runtime(tmp_path)
    outsider = Runtime(owner.store)
    backend = Backend()
    async with owner.use_game_backend(backend):
        await game_run(owner, {"game": "private-target"})
        with pytest.raises(BudgetExceeded):
            await game_run(outsider)
        context = backend.seen[0][0]
        assert context.candidate_digest == "a" * 64
        assert context.run_id
        with pytest.raises(BudgetExceeded):
            await play(context, context.original_task)
    with pytest.raises(BudgetExceeded):
        await game_run(owner)
    assert len(backend.seen) == 1


async def test_nested_game_scopes_restore_outer_and_revoke_inherited_tasks(tmp_path: Path) -> None:
    service = runtime(tmp_path)
    outer, inner = Backend(), Backend()
    release = asyncio.Event()

    async def inherited() -> None:
        await release.wait()
        with pytest.raises(BudgetExceeded):
            await game_run(service)

    async with service.use_game_backend(outer):
        await game_run(service)
        async with service.use_game_backend(inner):
            await game_run(service)
            delayed = asyncio.create_task(inherited())
        release.set()
        await delayed
        await game_run(service)
    assert len(outer.seen) == 2
    assert len(inner.seen) == 1


@pytest.mark.parametrize(
    "change", ["task", "preset", "params", "components", "task_type", "param_type"]
)
async def test_game_binding_rejects_substitution_before_backend(
    tmp_path: Path,
    change: str,
) -> None:
    service = runtime(tmp_path)
    backend = Backend()
    original: JsonValue = {"target": [1]}
    params: dict[str, JsonValue] = {"limit": 1}

    async def substitute(context: RunContext, task: JsonValue) -> JsonValue:
        return await context.play_game(
            {"target": [2]}
            if change == "task"
            else {"target": [True]}
            if change == "task_type"
            else task,
            preset="planner" if change == "preset" else "explorer",
            params={"limit": 2}
            if change == "params"
            else {"limit": True}
            if change == "param_type"
            else params,
            components=() if change == "components" else ("probe",),
        )

    async with service.use_game_backend(backend):
        with pytest.raises(ValueError, match="binding"):
            await service.evaluate(
                spec(),
                substitute,
                original,
                FakeAdapter(),
                preset="explorer",
                params=params,
                components=("probe",),
            )
    assert not backend.seen
    assert not service.store.rows()[-1].accepted


async def test_game_binding_copies_original_task_and_host_policy(tmp_path: Path) -> None:
    service = runtime(tmp_path)
    backend = Backend()
    candidate = spec(execution_config={"policy": {"limit": 3}})
    task: JsonValue = {"game": "bound"}

    async def mutate(context: RunContext, task: JsonValue) -> JsonValue:
        original = context.original_task
        assert isinstance(original, dict)
        original["game"] = "different"
        config = context.execution_config
        config["policy"] = None
        assert context.execution_config == {"policy": {"limit": 3}}
        return await play(context, task)

    async with service.use_game_backend(backend):
        result = await service.evaluate(
            candidate,
            mutate,
            task,
            FakeAdapter(),
            preset="explorer",
            components=("probe",),
        )
    assert result.output == "ok" and backend.seen[0][1] == task


class WaitingBackend(Backend):
    def __init__(self, *, suppress_cancel: bool = False) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.drained = asyncio.Event()
        self.suppress_cancel = suppress_cancel

    async def run(
        self,
        context: RunContext,
        task: JsonValue,
        *,
        preset: str,
        params: dict[str, JsonValue],
        components: tuple[str, ...],
    ) -> JsonValue:
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if not self.suppress_cancel:
                raise
            return "must-not-deliver"
        finally:
            self.drained.set()
        return "unreachable"


@pytest.mark.parametrize("suppress_cancel", [False, True])
async def test_scope_exit_cancels_drains_and_rechecks_after_wait(
    tmp_path: Path,
    suppress_cancel: bool,
) -> None:
    service = runtime(tmp_path)
    backend = WaitingBackend(suppress_cancel=suppress_cancel)
    async with service.use_game_backend(backend):
        running = asyncio.create_task(game_run(service))
        await asyncio.wait_for(backend.entered.wait(), 1)
    assert backend.drained.is_set()
    outcome = (await asyncio.gather(running, return_exceptions=True))[0]
    assert isinstance(outcome, (BudgetExceeded, asyncio.CancelledError))
    assert len(service.store.rows()) == 1 and not service.store.rows()[0].accepted


async def test_unfinished_game_work_is_drained_before_rejected_telemetry(tmp_path: Path) -> None:
    service = runtime(tmp_path)
    backend = WaitingBackend()
    background: list[asyncio.Task[JsonValue]] = []

    async def unfinished(context: RunContext, task: JsonValue) -> JsonValue:
        background.append(asyncio.create_task(play(context, task)))
        await backend.entered.wait()
        return "must-not-deliver"

    async with service.use_game_backend(backend):
        with pytest.raises(BudgetExceeded, match="unfinished"):
            await service.evaluate(
                spec(),
                unfinished,
                None,
                FakeAdapter(),
                preset="explorer",
                components=("probe",),
            )
    assert backend.drained.is_set()
    assert all(job.done() for job in background)
    assert not service.store.rows()[0].accepted


async def test_evaluation_identity_nested_cleanup_and_inherited_expiry() -> None:
    events: list[str] = []
    release = asyncio.Event()

    async def inherited(scope: EvaluationScope) -> None:
        await release.wait()
        assert current_evaluation() is scope
        assert not scope.active
        with pytest.raises(ValueError, match="expired"):
            scope.register_cleanup(lambda: events.append("late"))

    assert current_evaluation() is None
    with scoped_evaluation("a" * 64) as outer:
        outer.register_cleanup(lambda: events.append("outer"))
        with scoped_evaluation("a" * 64) as inner:
            assert inner.measurement_id != outer.measurement_id
            inner.register_cleanup(lambda: events.append("inner"))
            delayed = asyncio.create_task(inherited(inner))
        assert current_evaluation() is outer and outer.active and not inner.active
        release.set()
        await delayed
        assert events == ["inner"]
    assert events == ["inner", "outer"] and not outer.active
    assert current_evaluation() is None


def test_evaluation_cleanup_failure_does_not_skip_other_receipt_cleanup() -> None:
    cleaned: list[str] = []

    def faulty_cleanup() -> None:
        raise RuntimeError("injected-cleanup-fault")

    with pytest.raises(RuntimeError, match="injected-cleanup-fault"):
        with scoped_evaluation("a" * 64) as scope:
            scope.register_cleanup(lambda: cleaned.append("receipts-cleared"))
            scope.register_cleanup(faulty_cleanup)
    assert not scope.active and current_evaluation() is None
    assert cleaned == ["receipts-cleared"]


async def one_call(context: RunContext, task: JsonValue) -> JsonValue:
    return await context.call("private-model-input")


@pytest.mark.parametrize("shared_runtime", [False, True])
async def test_descendant_usage_aggregates_once_and_raw_rows_bill_once(
    tmp_path: Path,
    shared_runtime: bool,
) -> None:
    parent = runtime(tmp_path)
    child = parent if shared_runtime else Runtime(parent.store)

    async def nested(context: RunContext, task: JsonValue) -> JsonValue:
        await context.call("parent")
        result = await child.evaluate(spec(), one_call, task, adapter(11, 13, 0.02))
        assert (result.tokens_in, result.tokens_out, result.cost_usd) == (11, 13, 0.02)
        return result.output

    result = await parent.evaluate(spec(), nested, None, adapter())
    assert (result.tokens_in, result.tokens_out) == (14, 20)
    assert result.cost_usd == pytest.approx(0.03)
    assert parent.last_usage is not None and parent.last_row is not None
    assert (parent.last_usage.tokens_in, parent.last_usage.tokens_out) == (14, 20)
    assert (parent.last_row.tokens_in, parent.last_row.tokens_out) == (3, 7)
    rows = parent.store.rows()
    assert len(rows) == 2
    assert sum(row.tokens_in for row in rows) == 14
    assert sum(row.tokens_out for row in rows) == 20
    assert sum(row.cost_usd for row in rows) == pytest.approx(0.03)


class FailingAdapter:
    model = "fake"

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=11, tokens_out=13, cost_usd=0.02)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise AdapterError("injected-provider-failure")


@pytest.mark.parametrize("denied", [False, True])
async def test_failed_descendants_keep_bills_but_do_not_bill_denied_reservations(
    tmp_path: Path,
    denied: bool,
) -> None:
    service = runtime(tmp_path)
    child_adapter = adapter(11, 13, 0.02) if denied else FailingAdapter()

    async def nested(context: RunContext, task: JsonValue) -> JsonValue:
        await context.call("parent")
        return (await service.evaluate(spec(), one_call, task, child_adapter)).output

    with pytest.raises(BudgetExceeded if denied else AdapterError):
        await service.evaluate(
            spec(budget=Budget(cost_usd=0.015 if denied else 1, compute_s=5)),
            nested,
            None,
            adapter(),
        )
    assert service.last_usage is not None
    expected_in, expected_out, expected_cost = (3, 7, 0.01) if denied else (14, 20, 0.03)
    assert service.last_usage.tokens_in == expected_in
    assert service.last_usage.tokens_out == expected_out
    assert service.last_usage.cost_usd == pytest.approx(expected_cost)
    rows = service.store.rows()
    assert len(rows) == 2 and all(not row.accepted for row in rows)
    assert sum(row.tokens_in for row in rows) == expected_in
    assert sum(row.tokens_out for row in rows) == expected_out
    assert sum(row.cost_usd for row in rows) == pytest.approx(expected_cost)
    if denied:
        assert isinstance(child_adapter, FakeAdapter) and child_adapter.calls == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"nested": [float("nan")]},
        {"nested": {"value": float("inf")}},
        {"value": -float("inf")},
        {"payload": "x" * 65_537},
    ],
)
def test_execution_config_rejects_nonfinite_or_oversized_data(
    payload: dict[str, JsonValue],
) -> None:
    with pytest.raises(ValidationError):
        spec(execution_config=payload)


def test_hint_and_execution_config_are_digest_bound_not_provenance() -> None:
    original = spec(execution_config={"policy": {"limit": 1}})
    changed = spec(execution_config={"policy": {"limit": 2}})
    hinted = spec(execution_config=original.execution_config, preset_hint="planner")
    assert len({original.spec_hash, changed.spec_hash, hinted.spec_hash}) == 3
    assert (
        original.spec_hash == original.model_copy(update={"provenance": {"task": "user"}}).spec_hash
    )
    with pytest.raises(ValidationError):
        spec(preset_hint="competition")
    with pytest.raises(ValidationError):
        CompileConfig.model_validate({"allowed_presets": ["competition"]})


async def test_changed_config_or_hint_cannot_reuse_license(tmp_path: Path) -> None:
    service = compiler(tmp_path)
    result = await service.compile(spec())
    artifact = result.artifact
    assert artifact is not None and artifact.licensed
    for changed in (spec(execution_config={"policy": 1}), spec(preset_hint="planner")):
        forged = artifact.model_copy(update={"spec": changed})
        assert not forged.licensed
        with pytest.raises(UnlicensedArtifactError):
            await service.runtime.run(forged, {}, FakeAdapter())
    assert not (artifact.directory / ".consumed").exists()


async def test_explicit_hint_warm_start_still_needs_fresh_stage4(tmp_path: Path) -> None:
    service = compiler(tmp_path, allowed_presets=["synthesizer"])
    candidate = spec(preset_hint="synthesizer")
    first = await service.compile(candidate)
    second = await service.compile(candidate)
    assert first.artifact is not None and first.artifact.licensed
    assert second.artifact is not None and second.artifact.licensed
    assert first.report is not None and second.report is not None
    assert second.warm_started and 4 in second.stages
    assert first.report.verification_id != second.report.verification_id
    assert first.artifact.directory != second.artifact.directory


async def test_allowlist_rejects_before_tuning_and_hint_does_not_override(tmp_path: Path) -> None:
    service = compiler(tmp_path, allowed_presets=["explorer", "planner", "hypothesis_tester"])
    result = await service.compile(spec(preset_hint="synthesizer"))
    assert result.artifact is None and result.failures == ("selection_failed",)
    assert result.stages == (1,) and not service.runtime.store.rows()


async def test_hint_does_not_use_mismatched_licensed_warm_start(tmp_path: Path) -> None:
    service = compiler(tmp_path)
    result = await service.compile(spec(preset_hint="synthesizer"))
    assert result.artifact is not None and result.artifact.licensed
    artifact = result.artifact

    class FixedLibrary(NullLibrary):
        def lookup(self, candidate: Spec, model: str) -> HarnessArtifact | None:
            return artifact

    selected = select_preset(spec(preset_hint="planner"), "fake", FixedLibrary())
    assert selected.preset == "planner" and selected.warm_start is None


@pytest.mark.parametrize("hint", ["explorer", "planner", "hypothesis_tester"])
async def test_declarative_game_policy_and_hint_cannot_mint_capability(
    tmp_path: Path,
    hint: str,
) -> None:
    candidate = spec(preset_hint=hint, execution_config={"kind": "game", "tools": ["all"]})
    preset = get_preset(hint)
    model = FakeAdapter()
    service = runtime(tmp_path)
    with pytest.raises(BudgetExceeded):
        await service.evaluate(
            candidate,
            preset.build(candidate),
            {"game_id": "not-a-capability"},
            model,
            preset=hint,
            params=preset.validate_params({}),
            components=preset.components,
        )
    assert model.calls == 0 and not service.store.rows()[0].accepted


GAME_SOURCE = """async def run(context, task):
    return await context.play_game(task, preset='explorer', params={}, components=('probe',))
"""


def candidate_artifact(tmp_path: Path, candidate: Spec) -> HarnessArtifact:
    directory = tmp_path / "candidate"
    directory.mkdir()
    (directory / "harness.py").write_text(GAME_SOURCE, encoding="utf-8")
    artifact = HarnessArtifact(
        spec=candidate,
        preset="explorer",
        model="fake",
        components=("probe",),
        directory=directory,
    )
    artifact.save()
    return artifact


@pytest.mark.parametrize("failure", ["none", "provider", "denied"])
async def test_real_case_measurement_uses_descendant_usage_and_scoped_identity(
    tmp_path: Path,
    failure: str,
) -> None:
    service = runtime(tmp_path)
    candidate = spec(budget=Budget(cost_usd=0.015 if failure == "denied" else 1, compute_s=5))
    artifact = candidate_artifact(tmp_path, candidate)
    scopes: list[EvaluationScope] = []
    cleaned: list[str] = []
    scored: list[EvaluationScope] = []

    class IdentityFloor(HeldOutTasks):
        async def score(
            self,
            cases: Sequence[EvaluationCase],
            outputs: Sequence[JsonValue],
        ) -> float:
            identity = current_evaluation()
            assert identity is scopes[-1] and identity.active
            scored.append(identity)
            return await super().score(cases, outputs)

    class NestedBackend(Backend):
        async def run(
            self,
            context: RunContext,
            task: JsonValue,
            *,
            preset: str,
            params: dict[str, JsonValue],
            components: tuple[str, ...],
        ) -> JsonValue:
            identity = current_evaluation()
            assert identity is not None and identity.active
            assert identity.candidate_digest == artifact.artifact_digest == context.candidate_digest
            scopes.append(identity)
            identity.register_cleanup(lambda: cleaned.append(context.run_id))
            await context.call("parent")
            descendant = FailingAdapter() if failure == "provider" else adapter(11, 13, 0.02)
            return (await service.evaluate(spec(), one_call, None, descendant)).output

    floor = IdentityFloor(
        candidate.floor,
        [EvaluationCase(case_id=str(i), task={"game": i}, expected="ok") for i in range(2)],
    )
    with scoped_evaluation("b" * 64) as enclosing:
        async with service.use_game_backend(NestedBackend()):
            first = await evaluate_cases(
                artifact, floor, floor.tuning, lambda _: adapter(), service
            )
            assert current_evaluation() is enclosing and enclosing.active
            second = await evaluate_cases(
                artifact, floor, floor.tuning, lambda _: adapter(), service
            )
        assert current_evaluation() is enclosing
    assert len(scopes) == 2 and len(cleaned) == 2
    assert scopes[0].measurement_id != scopes[1].measurement_id
    assert all(not scope.active for scope in scopes)
    assert len(scored) == (2 if failure == "none" else 0)
    expected_tokens = 10 if failure == "denied" else 34
    expected_cost = 0.01 if failure == "denied" else 0.03
    for measurement in (first, second):
        assert measurement.mean_tokens == expected_tokens
        assert measurement.mean_cost_usd == pytest.approx(expected_cost)
        assert measurement.floor_passed == (failure == "none")
        if failure == "denied":
            assert not measurement.budget_passed
            assert measurement.max_cost_usd == pytest.approx(0.03)
    rows = service.store.rows()
    assert len(rows) == 6
    assert sum(row.tokens_in + row.tokens_out for row in rows) == 2 * expected_tokens
    assert sum(row.cost_usd for row in rows) == pytest.approx(2 * expected_cost)
    quality = [row for row in rows if row.task_type == categorical("quality_evaluation")]
    assert len(quality) == 2
    assert all(row.tokens_in == row.tokens_out == row.cost_usd == 0 for row in quality)


async def test_real_receipts_cannot_cross_nested_or_later_measurement_scopes(
    tmp_path: Path,
) -> None:
    # The host fixture issues actual production receipts, not a replacement scorer.
    from architect.floors import FloorError
    from architect.games.evaluation import GameScorer, ReceiptLedger
    from architect.games.types import GameScore

    service = runtime(tmp_path)
    ledger = ReceiptLedger()
    scorer = GameScorer(ledger)
    task: JsonValue = {"game": "same-task"}
    score = GameScore(normalized=1, raw=1, source="local", metric="local-win")

    class ReceiptBackend(Backend):
        async def run(
            self,
            context: RunContext,
            task: JsonValue,
            *,
            preset: str,
            params: dict[str, JsonValue],
            components: tuple[str, ...],
        ) -> JsonValue:
            return ledger.issue(context, task, session_id=context.run_id, score=score)

    async with service.use_game_backend(ReceiptBackend()):
        with scoped_evaluation("a" * 64):
            successful = await service.evaluate(
                spec(output_schema={}),
                play,
                task,
                FakeAdapter(),
                preset="explorer",
                components=("probe",),
                candidate_digest="a" * 64,
            )
            assert await scorer.score(task, successful.output) == 1
            with pytest.raises(FloorError):
                await scorer.score(task, successful.output)
            stale = await service.evaluate(
                spec(output_schema={}),
                play,
                task,
                FakeAdapter(),
                preset="explorer",
                components=("probe",),
                candidate_digest="a" * 64,
            )
            with scoped_evaluation("a" * 64):
                with pytest.raises(FloorError):
                    await scorer.score(task, stale.output)
            outstanding = await service.evaluate(
                spec(output_schema={}),
                play,
                task,
                FakeAdapter(),
                preset="explorer",
                components=("probe",),
                candidate_digest="a" * 64,
            )
        assert not ledger._receipts and not ledger._runs
        with scoped_evaluation("a" * 64):
            with pytest.raises(FloorError):
                await scorer.score(task, outstanding.output)
            with pytest.raises(FloorError):
                await scorer.score(task, {"receipt": "model-forgery", "score": 1})
    ledger.close()


async def test_cached_hint_cannot_license_when_fresh_rollouts_fail(tmp_path: Path) -> None:
    service = compiler(tmp_path, allowed_presets=["synthesizer"])
    candidate = spec(preset_hint="synthesizer")
    first = await service.compile(candidate)
    assert first.artifact is not None and first.artifact.licensed

    def failing_model(model: str) -> FakeAdapter:
        return FakeAdapter(
            model=model,
            responses=[
                ModelResponse(
                    model=model,
                    text="wrong",
                    tokens_in=1,
                    tokens_out=1,
                    cost_usd=0,
                )
            ],
        )

    service.adapter_factory = failing_model
    second = await service.compile(candidate)
    assert second.warm_started and 4 in second.stages
    assert second.artifact is not None and not second.artifact.licensed
    assert second.report is not None and first.report is not None
    assert second.report.verification_id != first.report.verification_id
    assert "primary_floor_failed" in second.failures


async def test_failed_measurement_receipts_are_cleared_and_not_replayable(tmp_path: Path) -> None:
    from architect.floors import FloorError
    from architect.games.evaluation import ReceiptLedger
    from architect.games.types import GameScore

    service = runtime(tmp_path)
    ledger = ReceiptLedger()
    artifact = candidate_artifact(tmp_path, spec(output_schema={}))
    floor = floor_factory(artifact.spec.floor)
    captured: list[tuple[JsonValue, JsonValue]] = []

    class IssueThenFail(Backend):
        async def run(
            self,
            context: RunContext,
            task: JsonValue,
            *,
            preset: str,
            params: dict[str, JsonValue],
            components: tuple[str, ...],
        ) -> JsonValue:
            receipt = ledger.issue(
                context,
                task,
                session_id=context.run_id,
                score=GameScore(
                    normalized=1,
                    raw=1,
                    source="local",
                    metric="local-win",
                ),
            )
            captured.append((task, receipt))
            raise AdapterError("injected-after-receipt-failure")

    async with service.use_game_backend(IssueThenFail()):
        result = await evaluate_cases(
            artifact, floor, floor.tuning, lambda _: FakeAdapter(), service
        )
    assert not result.floor_passed and "runtime_error" in result.failures
    assert len(captured) == 2
    assert not ledger._receipts and not ledger._runs
    with scoped_evaluation(artifact.artifact_digest):
        for task, receipt in captured:
            with pytest.raises(FloorError):
                ledger.consume(task, receipt)
    assert current_evaluation() is None


async def test_inherited_task_can_use_live_scope_but_not_expired_scope(tmp_path: Path) -> None:
    service = runtime(tmp_path)
    backend = Backend()
    async with service.use_game_backend(backend):
        await asyncio.create_task(game_run(service, {"game": "same-scope"}))
    assert len(backend.seen) == 1
    assert backend.seen[0][1] == {"game": "same-scope"}


async def test_absorb_rejects_child_with_pending_game_work_and_drains(tmp_path: Path) -> None:
    service = runtime(tmp_path)
    backend = WaitingBackend()
    background: list[asyncio.Task[JsonValue]] = []

    async def unfinished_child(context: RunContext, task: JsonValue) -> JsonValue:
        child = context.fork(FakeAdapter(), Budget(cost_usd=1, compute_s=5))
        background.append(asyncio.create_task(play(child, task)))
        await backend.entered.wait()
        # Absorption cannot hide in-flight privileged work from parent shutdown.
        with pytest.raises(ValueError, match="unsettled"):
            context.absorb(child)
        return "must-not-deliver"

    async with service.use_game_backend(backend):
        with pytest.raises(BudgetExceeded, match="unfinished"):
            await service.evaluate(
                spec(),
                unfinished_child,
                None,
                FakeAdapter(),
                preset="explorer",
                components=("probe",),
            )
    assert backend.drained.is_set()
    assert all(job.done() for job in background)
    assert len(service.store.rows()) == 1
    assert not service.store.rows()[0].accepted


async def test_absorb_allows_settled_child_game_work(tmp_path: Path) -> None:
    service = runtime(tmp_path)
    backend = Backend()

    async def settled_child(context: RunContext, task: JsonValue) -> JsonValue:
        child = context.fork(FakeAdapter(), Budget(cost_usd=1, compute_s=5))
        output = await play(child, task)
        context.absorb(child)
        return output

    async with service.use_game_backend(backend):
        result = await service.evaluate(
            spec(),
            settled_child,
            {"game": "bound-child"},
            FakeAdapter(),
            preset="explorer",
            components=("probe",),
        )
    assert result.output == "ok"
    assert len(backend.seen) == 1
    assert service.store.rows()[0].accepted
