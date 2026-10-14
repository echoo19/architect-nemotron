"""Phase 9: tuning measurement with per-case detail, $0 dry runs and ScriptedAdapter.

Everything is offline: FakeAdapter, ScriptedAdapter and the local game fixtures.
Harnesses are trusted native Python on disk (the sandbox path is WP1's).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import JsonValue

from architect.floors import (
    EvaluationCase,
    FloorEvaluator,
    HeldOutAccessError,
    HeldOutTasks,
    HumanEditRate,
)
from architect.floors._access import _VERIFIER_CAPABILITY
from architect.floors.base import EditLogRow
from architect.floors.evaluators import HeldOutGameScore
from architect.games.evaluation import ReceiptLedger, game_floor
from architect.games.execution import GameBackend, GameExecutionPolicy
from architect.games.local import LocalGameProvider
from architect.games.types import GameLimits, GameRef, GameScore, GameSession
from architect.models import AdapterError, FakeAdapter, ModelRequest
from architect.models.fake import ScriptedAdapter
from architect.runtime import BudgetExceeded, Runtime
from architect.runtime.evaluation import (
    CaseDetail,
    DryRun,
    dry_run_case,
    evaluate_cases,
    measure_cases,
)
from architect.spec import Budget, FloorDefinition, FloorKind, HarnessArtifact, Spec
from architect.telemetry import TelemetryStore, categorical

ECHO = (
    "async def run(context, task):\n"
    "    await context.call('look at ' + task)\n"
    "    return 'wrong' if task == 'task-2' else task\n"
)


def text_setup(
    tmp_path: Path, source: str = ECHO, *, output_schema: dict[str, JsonValue] | None = None
) -> tuple[HarnessArtifact, HeldOutTasks, Runtime]:
    definition = FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1.0, dataset="data")
    spec = Spec(
        task="Echo",
        input_schema={"type": "string"},
        output_schema=output_schema or {"type": "string"},
        floor=definition,
        budget=Budget(cost_usd=1, compute_s=30),
    )
    directory = tmp_path / "artifact"
    directory.mkdir(parents=True)
    (directory / "harness.py").write_text(source, encoding="utf-8")
    artifact = HarnessArtifact(
        spec=spec, preset="toy", model="fake", components=(), directory=directory
    )
    artifact.save()
    records = [
        EvaluationCase(
            case_id=str(i), task=f"task-{i}", expected=f"task-{i}", metadata={"secret": "meta"}
        )
        for i in range(6)
    ]
    return artifact, HeldOutTasks(definition, records), Runtime(TelemetryStore(tmp_path / "runs"))


def never_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(self: FloorEvaluator, capability: object) -> tuple[EvaluationCase, ...]:
        raise AssertionError("measure_cases must never request verification cases")

    monkeypatch.setattr(FloorEvaluator, "_verification_cases", refuse)


async def test_per_case_detail_on_tuning_and_aggregate_matches_evaluate_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact, floor, runtime = text_setup(tmp_path)
    tuning = floor.tuning
    assert [case.task for case in tuning] == ["task-0", "task-2", "task-4"]
    reference = await evaluate_cases(artifact, floor, (tuning[0], tuning[1]), FakeAdapter, runtime)
    never_verification(monkeypatch)
    measurement, details = await measure_cases(artifact, floor, [1, 0], FakeAdapter, runtime)
    for field in ("score", "floor_passed", "budget_passed", "sample_count", "failures"):
        assert getattr(measurement, field) == getattr(reference, field)
    assert measurement.mean_cost_usd == pytest.approx(reference.mean_cost_usd)
    assert [detail.index for detail in details] == [1, 0]
    wrong, right = details
    assert (wrong.passed, wrong.score, right.passed, right.score) == (False, 0.0, True, 1.0)
    assert wrong.output_excerpt == "wrong" and wrong.expected_excerpt == "task-2"
    assert right.output_excerpt == "task-0" and right.failure is None
    assert all(detail.calls == 1 and detail.game is None for detail in details)
    assert sum(detail.cost_usd for detail in details) == pytest.approx(
        measurement.mean_cost_usd * 2
    )
    # The aggregate quality row is emitted exactly as evaluate_cases emits it.
    row = runtime.store.rows()[-1]
    assert row.task_type == categorical("quality_evaluation") and row.floor_score == 0.5
    journal = (runtime.store.directory / "runs.jsonl").read_text(encoding="utf-8")
    assert "task-" not in journal and "meta" not in journal


async def test_secondary_floor_is_measured_exactly_as_evaluate_cases(tmp_path: Path) -> None:
    # Regression: measure_cases once forced floor_role="primary", so a spec's
    # secondary floor always failed with floor_mismatch and rows said "primary".
    artifact, primary, runtime = text_setup(tmp_path)
    secondary_definition = FloorDefinition(
        kind=FloorKind.HELD_OUT_TASKS, limit=0.5, dataset="secondary"
    )
    spec = artifact.spec.model_copy(update={"secondary_floor": secondary_definition})
    artifact = HarnessArtifact(
        spec=spec, preset="toy", model="fake", components=(), directory=artifact.directory
    )
    artifact.save()
    records = [
        EvaluationCase(case_id=str(i), task=f"task-{i}", expected=f"task-{i}") for i in range(6)
    ]
    secondary = HeldOutTasks(secondary_definition, records)
    reference = await evaluate_cases(
        artifact, secondary, (secondary.tuning[0], secondary.tuning[1]), FakeAdapter, runtime
    )
    reference_row = runtime.store.rows()[-1]
    assert reference.failures == () and reference_row.floor_role == "secondary"
    measurement, details = await measure_cases(artifact, secondary, [0, 1], FakeAdapter, runtime)
    assert "floor_mismatch" not in measurement.failures
    for field in ("score", "floor_passed", "budget_passed", "sample_count", "failures"):
        assert getattr(measurement, field) == getattr(reference, field)
    assert runtime.store.rows()[-1].floor_role == "secondary"
    assert [detail.score for detail in details] == [1.0, 0.0]
    # The primary floor still measures as primary.
    await measure_cases(artifact, primary, [0], FakeAdapter, runtime)
    assert runtime.store.rows()[-1].floor_role == "primary"


async def test_aggregate_feedback_mode_hides_per_case_values(tmp_path: Path) -> None:
    artifact, floor, runtime = text_setup(tmp_path)
    measurement, details = await measure_cases(
        artifact, floor, [0, 1, 2], FakeAdapter, runtime, per_case=False
    )
    assert measurement.sample_count == 3 and measurement.score == pytest.approx(2 / 3)
    for detail in details:
        assert detail.passed is None and detail.score is None
        assert detail.output_excerpt is None and detail.expected_excerpt is None
        assert detail.calls == 1


async def test_excerpts_are_truncated(tmp_path: Path) -> None:
    artifact, floor, runtime = text_setup(tmp_path)
    _, details = await measure_cases(artifact, floor, [0], FakeAdapter, runtime, excerpt_chars=3)
    assert details[0].output_excerpt == "tas" and details[0].expected_excerpt == "tas"


@pytest.mark.parametrize("indexes", [[3], [-1], [0, 0], [True], ["0"]])
async def test_invalid_indexes_are_refused_before_any_run(
    tmp_path: Path, indexes: list[object]
) -> None:
    artifact, floor, runtime = text_setup(tmp_path)
    with pytest.raises(ValueError, match="invalid_case"):
        await measure_cases(artifact, floor, indexes, FakeAdapter, runtime)  # type: ignore[arg-type]
    assert runtime.store.rows() == []


async def test_verification_cases_are_refused(tmp_path: Path) -> None:
    artifact, honest, runtime = text_setup(tmp_path)

    class Tampered(HeldOutTasks):
        @property
        def tuning(self) -> tuple[EvaluationCase, ...]:
            # A floor that hands out held-out objects as if they were tuning.
            return self._verification_cases(_VERIFIER_CAPABILITY)

    definition = honest.definition
    records = [
        EvaluationCase(case_id=str(i), task=f"task-{i}", expected=f"task-{i}") for i in range(6)
    ]
    floor = Tampered(definition, records)
    for per_case in (True, False):
        with pytest.raises(HeldOutAccessError):
            await measure_cases(artifact, floor, [0], FakeAdapter, runtime, per_case=per_case)


async def test_human_edit_rate_has_no_per_case_scores(tmp_path: Path) -> None:
    definition = FloorDefinition(
        kind=FloorKind.HUMAN_EDIT_RATE, limit=0.5, dataset="edits", minimum_samples=2
    )
    rows = [
        EditLogRow(
            case_id=str(i), task=f"task-{i}", proposed="x", shipped=f"task-{i}", changed=True
        )
        for i in range(4)
    ]
    floor = HumanEditRate(definition, rows)
    artifact, _, runtime = text_setup(tmp_path)
    artifact = artifact.model_copy(
        update={"spec": artifact.spec.model_copy(update={"floor": definition})}
    )
    with pytest.raises(ValueError, match="forge_measure_too_small"):
        await measure_cases(artifact, floor, [0], FakeAdapter, runtime)
    measurement, details = await measure_cases(artifact, floor, [0, 1], FakeAdapter, runtime)
    assert measurement.sample_count == 2 and measurement.score == 0.5
    assert all(detail.passed is None and detail.score is None for detail in details)
    assert details[0].output_excerpt == "task-0"


async def test_a_failed_case_keeps_its_code_and_others_are_still_scored(tmp_path: Path) -> None:
    source = (
        "from architect.runtime.sandbox import SandboxError\n"
        "async def run(context, task):\n"
        "    if task == 'task-2':\n"
        "        error = SandboxError('sandbox_key')\n"
        "        error.line = 7\n"
        "        raise error\n"
        "    if task == 'task-4':\n"
        "        raise RuntimeError('private text never copied')\n"
        "    return task\n"
    )
    artifact, floor, runtime = text_setup(tmp_path, source)
    measurement, details = await measure_cases(artifact, floor, [0, 1, 2], FakeAdapter, runtime)
    assert not measurement.floor_passed and "runtime_error" in measurement.failures
    ok, keyed, other = details
    assert (ok.passed, ok.score, ok.failure) == (True, 1.0, None)
    assert (keyed.failure, keyed.sandbox_line, keyed.score) == ("sandbox_key", 7, None)
    assert (other.failure, other.sandbox_line) == ("runtime_error", None)
    assert keyed.output_excerpt is None and keyed.expected_excerpt == "task-2"


# ---------------------------------------------------------------------------
# Games: per-case process metrics from the host counters.
# ---------------------------------------------------------------------------

TURN_SOURCE = (
    "async def run(context, task):\n"
    "    observation = await context.game_observe()\n"
    "    while observation['actions_left'] > 0:\n"
    "        observation = await context.game_step({'name': 'ACTION1'})\n"
    "    return await context.game_finish()\n"
)


def game_setup(
    tmp_path: Path, source: str = TURN_SOURCE, provider: LocalGameProvider | None = None
) -> tuple[HarnessArtifact, FloorEvaluator, Runtime, GameBackend]:
    provider = provider or LocalGameProvider()
    manifest = provider.manifest
    policy = GameExecutionPolicy(
        small_model="fake",
        manifest_digest=manifest.digest,
        support_dataset="support",
        limits=GameLimits(max_actions=4, max_turns=4),
    )
    definition = FloorDefinition(kind=FloorKind.HELD_OUT_GAME_SCORE, limit=1, dataset="support")
    ledger = ReceiptLedger()
    floor = game_floor(definition, manifest, ledger)
    support = (*manifest.refs("support_tuning"), *manifest.refs("support_held_out"))
    backend = GameBackend(
        provider, policy, ledger, evaluation_refs=support, target=manifest.refs("delivery")[0]
    )
    spec = Spec(
        task="Play",
        input_schema={},
        output_schema={"type": "object"},
        domain="game",
        model_target="fake",
        budget=Budget(cost_usd=1, compute_s=30),
        preset_hint="explorer",
        execution_config=policy.model_dump(mode="json"),
        floor=definition,
    )
    directory = tmp_path / "game-artifact"
    directory.mkdir(parents=True)
    (directory / "harness.py").write_text(source, encoding="utf-8")
    artifact = HarnessArtifact(
        spec=spec, preset="explorer", model="fake", components=(), directory=directory
    )
    return artifact, floor, Runtime(TelemetryStore(tmp_path / "runs")), backend


async def test_game_cases_carry_process_metrics_and_trace(tmp_path: Path) -> None:
    artifact, floor, runtime, backend = game_setup(tmp_path)
    async with runtime.use_game_backend(backend):
        measurement, details = await measure_cases(
            artifact, floor, [0, 1], FakeAdapter, runtime, trace=True
        )
        _, untraced = await measure_cases(artifact, floor, [2], FakeAdapter, runtime)
    await backend.close()
    assert measurement.sample_count == 2 and measurement.failures == ("floor_not_met",)
    for detail in details:
        assert detail.score == 0.0 and detail.passed is False and detail.calls == 0
        game = detail.game
        assert game is not None and game.trace is not None
        assert game.actions == len(game.trace) == 4
        assert all(item[0] == "ACTION1" for item in game.trace)
        assert game.calls_per_action == 0.0 and 0.0 <= game.revisit_rate <= 1.0
        assert game.final_state in {"NOT_FINISHED", "WIN", "GAME_OVER"}
        assert game.distinct_states >= 1
    assert untraced[0].game is not None and untraced[0].game.trace is None


# ---------------------------------------------------------------------------
# dry_run_case and ScriptedAdapter
# ---------------------------------------------------------------------------

DRY_SOURCE = (
    "async def run(context, task):\n"
    "    first = await context.call('hello ' + task, 50, system='SYS')\n"
    "    second = await context.call('again')\n"
    "    return first + '|' + second\n"
)


async def test_dry_run_spends_nothing_and_records_call_shapes(tmp_path: Path) -> None:
    artifact, floor, runtime = text_setup(tmp_path, DRY_SOURCE)
    result = await dry_run_case(artifact, floor, 1, ["A"], runtime, prompt_head_chars=7)
    assert isinstance(result, DryRun)
    assert result.ok and result.code is None and result.line is None
    assert result.output_head == "A|A" and result.schema_ok
    assert result.calls == [
        {
            "prompt_head": "hello t",
            "prompt_chars": 12,
            "system_chars": 3,
            "images": 0,
            "tokens": 50,
        },
        {"prompt_head": "again", "prompt_chars": 5, "system_chars": 0, "images": 0, "tokens": None},
    ]
    assert result.steps == 0 and result.fuel_used == 0
    rows = runtime.store.rows()
    assert len(rows) == 1
    assert rows[0].cost_usd == 0 and rows[0].tokens_in == 0 and rows[0].model_calls == 2
    assert rows[0].preset_origin == categorical("forge_dryrun")


async def test_dry_run_reports_codes_and_schema(tmp_path: Path) -> None:
    source = (
        "from architect.runtime.sandbox import SandboxError\n"
        "async def run(context, task):\n"
        "    error = SandboxError('sandbox_fuel')\n"
        "    error.line = 3\n"
        "    raise error\n"
    )
    artifact, floor, runtime = text_setup(tmp_path, source)
    failed = await dry_run_case(artifact, floor, 0, [], runtime)
    assert (failed.ok, failed.code, failed.line, failed.output_head) == (
        False,
        "sandbox_fuel",
        3,
        "",
    )
    artifact, floor, runtime = text_setup(
        tmp_path / "schema", "async def run(context, task):\n    return 5\n"
    )
    wrong = await dry_run_case(artifact, floor, 0, [], runtime, output_head_chars=10)
    assert wrong.ok and not wrong.schema_ok and wrong.output_head == "5"
    with pytest.raises(ValueError, match="invalid_case"):
        await dry_run_case(artifact, floor, 3, [], runtime)


class NeverScored(LocalGameProvider):
    def __init__(self) -> None:
        super().__init__()
        self.sessions: list[GameSession] = []

    async def open(self, ref: GameRef, *, limits: GameLimits) -> GameSession:
        session = await super().open(ref, limits=limits)

        async def refuse() -> GameScore:
            raise AssertionError("a dry run must never reach the score path")

        session.finalize = refuse  # type: ignore[method-assign]
        self.sessions.append(session)
        return session


async def test_offline_game_dry_run_is_unscored_and_stops(tmp_path: Path) -> None:
    source = (
        "async def run(context, task):\n"
        "    for _ in range(10):\n"
        "        await context.game_step({'name': 'ACTION1'})\n"
        "    return {'unused': True}\n"
    )
    provider = NeverScored()
    artifact, floor, runtime, backend = game_setup(tmp_path, source, provider)
    async with runtime.use_game_backend(backend):
        result = await dry_run_case(artifact, floor, 0, [], runtime, game_steps=3)
    assert result.ok and result.steps == 3 and result.schema_ok
    assert result.output_head == '{"scored": false}'
    assert provider.sessions and provider.sessions[0].actions_used == 3
    assert not backend.completed and not backend.ledger._receipts
    assert runtime.store.rows()[-1].cost_usd == 0
    await backend.close()


async def test_game_dry_run_without_an_offline_backend_is_unavailable(tmp_path: Path) -> None:
    artifact, floor, runtime, backend = game_setup(tmp_path)
    with pytest.raises(ValueError, match="forge_dryrun_unavailable"):
        await dry_run_case(artifact, floor, 0, [], runtime)
    await backend.close()


async def test_scripted_adapter_is_zero_priced_and_cycles() -> None:
    adapter = ScriptedAdapter(["a", "b"], model="granite", max_recorded=2)
    request = ModelRequest(prompt="SYSprompt", cache_prefix="SYS", max_output_tokens=9)
    quote = adapter.quote(request)
    assert (quote.tokens_in, quote.tokens_out, quote.cost_usd) == (0, 0, 0.0)
    replies = [(await adapter.complete(request)).text for _ in range(3)]
    assert replies == ["a", "b", "a"] and adapter.calls == 3
    assert len(adapter.requests) == 2
    assert adapter.requests[0].prompt == "prompt" and adapter.requests[0].system_chars == 3
    response = await adapter.complete(request)
    assert response.cost_usd == 0 and response.model == "granite"
    assert (await ScriptedAdapter([]).complete(ModelRequest(prompt="x"))).text == "{}"
    with pytest.raises(AdapterError):
        ScriptedAdapter("abc")
    with pytest.raises(AdapterError):
        ScriptedAdapter([1])  # type: ignore[list-item]


def test_case_detail_is_frozen_and_closed() -> None:
    detail = CaseDetail(
        index=0,
        passed=None,
        score=None,
        failure=None,
        sandbox_line=None,
        calls=0,
        cost_usd=0.0,
        output_excerpt=None,
        expected_excerpt=None,
        game=None,
    )
    with pytest.raises(ValueError):
        CaseDetail.model_validate({**detail.model_dump(), "note": "x"})


# ---------------------------------------------------------------------------
# Integration with the real sandbox language v2 worker (WP1): a Forge game
# artifact plays per turn; the host, not the harness, produces the output.
# ---------------------------------------------------------------------------

V2_SOURCE = (
    "from pydantic import JsonValue\n"
    "\n"
    "from architect.runtime import RunContext\n"
    "\n"
    "\n"
    "async def run(context: RunContext, task: JsonValue) -> JsonValue:\n"
    "    observation = await context.observe()\n"
    "    seen = []\n"
    "    while observation['actions_left'] > 0:\n"
    "        append(seen, digest(observation['frame']))\n"
    "        observation = await context.step({'name': 'ACTION1'})\n"
    "    return {'ignored': len(seen)}\n"
)


def forge_game(
    tmp_path: Path, provider: LocalGameProvider | None = None
) -> tuple[HarnessArtifact, FloorEvaluator, Runtime, GameBackend]:
    from architect.spec import ForgeBinding

    artifact, floor, runtime, backend = game_setup(tmp_path, V2_SOURCE, provider)
    forged = artifact.model_copy(
        update={
            "execution_mode": "sandbox",
            "components": ("interface_analysis", "probe", "analysis_check"),
            "forge": ForgeBinding(call_limit=16, fuel=1_000_000),
        }
    )
    forged.save()
    return forged, floor, runtime, backend


async def test_v2_forge_game_is_measured_with_host_output(tmp_path: Path) -> None:
    artifact, floor, runtime, backend = forge_game(tmp_path)
    async with runtime.use_game_backend(backend):
        measurement, details = await measure_cases(
            artifact, floor, [0, 1], FakeAdapter, runtime, trace=True
        )
    await backend.close()
    # Scored from host receipts although the harness returned something else.
    assert measurement.failures == ("floor_not_met",) and measurement.sample_count == 2
    assert [detail.score for detail in details] == [0.0, 0.0]
    for detail in details:
        assert detail.failure is None and detail.game is not None
        assert detail.game.actions == 4 and detail.game.trace is not None
        assert detail.output_excerpt is not None and "receipt" in detail.output_excerpt


async def test_v2_forge_game_dry_run_stops_unscored(tmp_path: Path) -> None:
    provider = NeverScored()
    artifact, floor, runtime, backend = forge_game(tmp_path, provider)
    async with runtime.use_game_backend(backend):
        result = await dry_run_case(artifact, floor, 0, [], runtime, game_steps=2)
    await backend.close()
    assert result.ok and result.steps == 2 and result.output_head == '{"scored": false}'
    assert provider.sessions[0].actions_used == 2
    assert not backend.completed and runtime.store.rows()[-1].cost_usd == 0


# ---------------------------------------------------------------------------
# Regression: a scorer's BudgetExceeded is a budget failure, exactly as in
# evaluate_cases, for the floors whose aggregate reuses per-case scores.
# ---------------------------------------------------------------------------


class DeniedScorer:
    """A game environment (or judge) whose scoring wallet is exhausted."""

    def __init__(self) -> None:
        self.calls = 0

    async def score(self, task: JsonValue, output: JsonValue) -> float:
        self.calls += 1
        raise BudgetExceeded("scoring wallet exhausted", required_cost_usd=0.25)


class DeniedJudge(FloorEvaluator):
    """An LLM_JUDGE-kind floor whose judge run is refused by the budget."""

    def __init__(self, definition: FloorDefinition, records: list[EvaluationCase]) -> None:
        super().__init__(definition, records)
        self.scorer = DeniedScorer()

    async def score(self, cases: Sequence[EvaluationCase], outputs: Sequence[JsonValue]) -> float:
        self._validate_score_inputs(cases, outputs)
        for case, output in zip(cases, outputs, strict=True):
            await self.scorer.score(case.task, output)
        return 1.0


def denied_setup(
    tmp_path: Path, kind: FloorKind
) -> tuple[HarnessArtifact, FloorEvaluator, DeniedScorer, Runtime]:
    if kind == FloorKind.LLM_JUDGE:
        definition = FloorDefinition(
            kind=kind,
            limit=0.5,
            dataset="data",
            rubric="Exact echo",
            judge_model="judge",
            calibration_dataset="calibration",
        )
    else:
        definition = FloorDefinition(kind=kind, limit=0.5, dataset="data")
    spec = Spec(
        task="Echo",
        input_schema={"type": "string"},
        output_schema={"type": "string"},
        floor=definition,
        budget=Budget(cost_usd=1, compute_s=30),
    )
    directory = tmp_path / "artifact"
    directory.mkdir(parents=True)
    (directory / "harness.py").write_text(
        "async def run(context, task):\n    return task\n", encoding="utf-8"
    )
    artifact = HarnessArtifact(
        spec=spec, preset="toy", model="fake", components=(), directory=directory
    )
    artifact.save()
    records = [
        EvaluationCase(case_id=str(i), task=f"task-{i}", expected=f"task-{i}") for i in range(8)
    ]
    floor: FloorEvaluator
    if kind == FloorKind.LLM_JUDGE:
        judge = DeniedJudge(definition, records)
        floor, scorer = judge, judge.scorer
    else:
        scorer = DeniedScorer()
        floor = HeldOutGameScore(definition, records, scorer)
    return artifact, floor, scorer, Runtime(TelemetryStore(tmp_path / "runs"))


@pytest.mark.parametrize("kind", [FloorKind.HELD_OUT_GAME_SCORE, FloorKind.LLM_JUDGE])
async def test_scoring_budget_denial_matches_evaluate_cases(
    tmp_path: Path, kind: FloorKind
) -> None:
    artifact, floor, scorer, runtime = denied_setup(tmp_path, kind)
    tuning = floor.tuning
    reference = await evaluate_cases(
        artifact, floor, (tuning[0], tuning[1], tuning[2]), FakeAdapter, runtime
    )
    assert reference.failures == ("budget_exceeded",) and not reference.budget_passed
    scorer.calls = 0
    measurement, details = await measure_cases(artifact, floor, [0, 1, 2], FakeAdapter, runtime)
    for field in (
        "score",
        "floor_passed",
        "budget_passed",
        "sample_count",
        "failures",
        "max_cost_usd",
    ):
        assert getattr(measurement, field) == getattr(reference, field), field
    # The denial's required reservation is applied, as evaluate_cases applies it.
    assert measurement.max_cost_usd == pytest.approx(0.25)
    # Scoring stops at the first denial: nothing is spent past the refused wallet.
    assert scorer.calls == 1
    first, *rest = details
    assert (first.failure, first.score, first.passed) == ("budget_exceeded", None, None)
    assert all(d.score is None and d.passed is None and d.failure is None for d in rest)
    assert all(d.output_excerpt == f"task-{2 * d.index}" for d in details)
    row = runtime.store.rows()[-1]
    assert row.task_type == categorical("quality_evaluation")


async def test_scoring_denial_after_a_failed_case_stops_detail_scoring(tmp_path: Path) -> None:
    artifact, floor, scorer, runtime = denied_setup(tmp_path, FloorKind.HELD_OUT_GAME_SCORE)
    (artifact.directory / "harness.py").write_text(
        "async def run(context, task):\n"
        "    if task == 'task-2':\n"
        "        raise RuntimeError('boom')\n"
        "    return task\n",
        encoding="utf-8",
    )
    artifact.save()
    measurement, details = await measure_cases(artifact, floor, [0, 1, 2], FakeAdapter, runtime)
    # A partial run is never aggregate-scored; the per-case pass stops at the denial.
    assert measurement.failures == ("runtime_error",)
    assert scorer.calls == 1
    assert [d.failure for d in details] == ["budget_exceeded", "runtime_error", None]


class FailThenDeniedScorer(DeniedScorer):
    """The first score fails outright; every later score is refused by the wallet."""

    async def score(self, task: JsonValue, output: JsonValue) -> float:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("receipt invalid")
        raise BudgetExceeded("scoring wallet exhausted", required_cost_usd=0.25)


@pytest.mark.parametrize("kind", [FloorKind.HELD_OUT_GAME_SCORE, FloorKind.LLM_JUDGE])
async def test_scoring_error_before_a_denial_matches_evaluate_cases(
    tmp_path: Path, kind: FloorKind
) -> None:
    artifact, floor, _, runtime = denied_setup(tmp_path, kind)
    scorer = FailThenDeniedScorer()
    if isinstance(floor, DeniedJudge):
        floor.scorer = scorer
    else:
        floor._environment = scorer
    tuning = floor.tuning
    reference = await evaluate_cases(
        artifact, floor, (tuning[0], tuning[1], tuning[2]), FakeAdapter, runtime
    )
    assert reference.failures == ("floor_error",) and reference.budget_passed
    assert reference.max_cost_usd == 0.0 and scorer.calls == 1
    scorer.calls = 0
    measurement, details = await measure_cases(artifact, floor, [0, 1, 2], FakeAdapter, runtime)
    for field in (
        "score",
        "floor_passed",
        "budget_passed",
        "sample_count",
        "failures",
        "max_cost_usd",
        "mean_cost_usd",
    ):
        assert getattr(measurement, field) == getattr(reference, field), field
    # The first failing case decides the aggregate and ends scoring: no second call.
    assert scorer.calls == 1
    assert [(d.failure, d.score, d.passed) for d in details] == [
        ("floor_error", None, None),
        (None, None, None),
        (None, None, None),
    ]


# ---------------------------------------------------------------------------
# Regression: the aggregate is exactly evaluate_cases' aggregate for every floor
# kind and every ordering of scoring outcomes. Scoring wallets and receipts are
# stateful (a denial depends on what was scored before), so the scripted scorer
# answers by call order, not by case.
# ---------------------------------------------------------------------------

OK_VALUES = (0.25, 1.0, 0.5, 0.75)


class ScriptedScorer:
    """Answers the n-th scoring call from a script; calls past the script succeed."""

    def __init__(self, script: tuple[str, ...]) -> None:
        self.script = script
        self.calls = 0

    async def score(self, task: JsonValue, output: JsonValue) -> float:
        self.calls += 1
        step = self.script[self.calls - 1] if self.calls <= len(self.script) else "ok"
        if step == "error":
            raise RuntimeError("receipt invalid")
        if step == "denied":
            raise BudgetExceeded("scoring wallet exhausted", required_cost_usd=0.25)
        return OK_VALUES[(self.calls - 1) % len(OK_VALUES)]


class ScriptedFloor(FloorEvaluator):
    """Any floor kind that scores case by case and stops at the first failure."""

    def __init__(
        self, definition: FloorDefinition, records: list[EvaluationCase], scorer: ScriptedScorer
    ) -> None:
        super().__init__(definition, records)
        self.scorer = scorer

    async def score(self, cases: Sequence[EvaluationCase], outputs: Sequence[JsonValue]) -> float:
        self._validate_score_inputs(cases, outputs)
        values = [
            await self.scorer.score(case.task, output)
            for case, output in zip(cases, outputs, strict=True)
        ]
        return sum(values) / len(values)


PARITY_KINDS = [
    pytest.param(FloorKind.HELD_OUT_TASKS, 1, id="held_out_tasks"),
    pytest.param(FloorKind.HUMAN_EDIT_RATE, 1, id="human_edit_rate-per_case"),
    pytest.param(FloorKind.HUMAN_EDIT_RATE, 3, id="human_edit_rate-aggregate_only"),
    pytest.param(FloorKind.LLM_JUDGE, 1, id="llm_judge"),
    pytest.param(FloorKind.HELD_OUT_GAME_SCORE, 1, id="held_out_game_score"),
]
PARITY_SCRIPTS = [
    (),
    ("error",),
    ("denied",),
    ("error", "denied"),
    ("denied", "error"),
    ("ok", "error"),
    ("ok", "denied"),
    ("ok", "error", "denied"),
    ("ok", "denied", "error"),
    ("ok", "ok", "error"),
    ("ok", "ok", "denied"),
    ("error", "error", "denied"),
    ("error", "ok", "ok", "denied"),
]


def parity_floor(kind: FloorKind, minimum: int, scorer: ScriptedScorer) -> FloorEvaluator:
    extra: dict[str, str] = (
        {"rubric": "Exact echo", "judge_model": "judge", "calibration_dataset": "calibration"}
        if kind == FloorKind.LLM_JUDGE
        else {}
    )
    definition = FloorDefinition(
        kind=kind, limit=0.5, dataset="data", minimum_samples=minimum, **extra
    )
    records = [
        EvaluationCase(case_id=str(i), task=f"task-{i}", expected=f"task-{i}") for i in range(8)
    ]
    if kind == FloorKind.HELD_OUT_GAME_SCORE:
        return HeldOutGameScore(definition, records, scorer)
    return ScriptedFloor(definition, records, scorer)


@pytest.mark.parametrize("per_case", [True, False], ids=["per_case", "aggregate_mode"])
@pytest.mark.parametrize("script", PARITY_SCRIPTS, ids=lambda s: "-".join(s) or "clean")
@pytest.mark.parametrize(("kind", "minimum"), PARITY_KINDS)
async def test_aggregate_parity_over_floor_kinds_and_error_orderings(
    tmp_path: Path, kind: FloorKind, minimum: int, script: tuple[str, ...], per_case: bool
) -> None:
    spec = Spec(
        task="Echo",
        input_schema={"type": "string"},
        output_schema={"type": "string"},
        floor=parity_floor(kind, minimum, ScriptedScorer(())).definition,
        budget=Budget(cost_usd=1, compute_s=30),
    )
    directory = tmp_path / "artifact"
    directory.mkdir(parents=True)
    (directory / "harness.py").write_text(
        "async def run(context, task):\n    return task\n", encoding="utf-8"
    )
    artifact = HarnessArtifact(
        spec=spec, preset="toy", model="fake", components=(), directory=directory
    )
    artifact.save()
    indexes = [0, 1, 2]

    reference_scorer = ScriptedScorer(script)
    reference_floor = parity_floor(kind, minimum, reference_scorer)
    tuning = reference_floor.tuning
    reference_runtime = Runtime(TelemetryStore(tmp_path / "reference"))
    reference = await evaluate_cases(
        artifact, reference_floor, tuple(tuning[i] for i in indexes), FakeAdapter, reference_runtime
    )

    scorer = ScriptedScorer(script)
    floor = parity_floor(kind, minimum, scorer)
    runtime = Runtime(TelemetryStore(tmp_path / "measured"))
    measurement, details = await measure_cases(
        artifact, floor, indexes, FakeAdapter, runtime, per_case=per_case
    )
    for field in (
        "score",
        "floor_passed",
        "budget_passed",
        "sample_count",
        "failures",
        "max_cost_usd",
        "mean_cost_usd",
        "max_tokens",
        "mean_tokens",
    ):
        assert getattr(measurement, field) == getattr(reference, field), field
    row, reference_row = runtime.store.rows()[-1], reference_runtime.store.rows()[-1]
    assert row.task_type == reference_row.task_type == categorical("quality_evaluation")
    assert (row.floor_score, row.accepted) == (reference_row.floor_score, reference_row.accepted)
    # Nothing is scored past a refused wallet, in the aggregate or the detail.
    if "denied" in script:
        assert scorer.calls <= script.index("denied") + 1
    assert len(details) == len(indexes)
    if not per_case:
        assert all(d.score is None and d.passed is None for d in details)


async def test_partial_game_run_still_scores_every_case_that_ran(tmp_path: Path) -> None:
    # A reused floor is never aggregate-scored on a partial run, so the detail
    # pass scores each case that ran, skipping only the failed one.
    artifact, floor, _, runtime = denied_setup(tmp_path, FloorKind.HELD_OUT_GAME_SCORE)
    assert isinstance(floor, HeldOutGameScore)
    scorer = ScriptedScorer(())
    floor._environment = scorer
    (artifact.directory / "harness.py").write_text(
        "async def run(context, task):\n"
        "    if task == 'task-2':\n"
        "        raise RuntimeError('boom')\n"
        "    return task\n",
        encoding="utf-8",
    )
    artifact.save()
    measurement, details = await measure_cases(artifact, floor, [0, 1, 2], FakeAdapter, runtime)
    assert measurement.failures == ("runtime_error",)
    assert scorer.calls == 2
    assert [(d.failure, d.score) for d in details] == [
        (None, OK_VALUES[0]),
        ("runtime_error", None),
        (None, OK_VALUES[1]),
    ]
