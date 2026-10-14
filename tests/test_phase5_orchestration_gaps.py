"""Phase 5 gate regressions using real compilation, licenses, wallets, and calls."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import JsonValue

from architect.compiler import CompileConfig, Compiler, CompileResult
from architect.compiler.inference import Subtask
from architect.floors import EvaluationCase, HeldOutTasks
from architect.models import AdapterError, FakeAdapter, ModelRequest, ModelResponse, UsageQuote
from architect.orchestrator import Orchestrator, OrchestratorConfig
from architect.presets import Preset
from architect.runtime import BudgetExceeded, Harness, RunContext, Runtime
from architect.runtime.pipeline import PipelineError, run_pipeline
from architect.runtime.wallet import active_wallets, compilation_wallet
from architect.spec import Budget, FloorDefinition, Lifecycle, PipelineStep, Spec
from architect.telemetry import TelemetryStore

FIXTURES = Path(__file__).parent / "fixtures" / "orchestration"
PLANS = json.loads((FIXTURES / "plans.json").read_text(encoding="utf-8"))
SPECS = json.loads((FIXTURES / "specs.json").read_text(encoding="utf-8"))
SECRET = "private provider detail: do not disclose"


class FailingAdapter(FakeAdapter):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise AdapterError(SECRET)


class PendingAdapter(FakeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = False

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=100_000, tokens_out=0, cost_usd=0.001)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled = True
        raise AssertionError("Blocked provider must be cancelled")


def response(text: str) -> ModelResponse:
    return ModelResponse(
        text=text, tokens_in=100, tokens_out=100, cost_usd=0.0002, model="fake",
    )


def floor_factory(definition: FloorDefinition) -> HeldOutTasks:
    return HeldOutTasks(definition, [
        EvaluationCase(case_id=f"c{i}", task={"action": i}, expected="ok") for i in range(4)
    ])


@pytest.fixture
def compiler(tmp_path: Path) -> Iterator[Compiler]:
    store = TelemetryStore(tmp_path / "telemetry")
    try:
        yield Compiler(
            tmp_path / "artifacts", Runtime(store), lambda model: FakeAdapter(model=model),
            floor_factory, config=CompileConfig(max_candidates=1, enable_rewriting=False),
            inference_adapter=FakeAdapter(responses=[response(json.dumps(SPECS["ok"]))]),
        )
    finally:
        store.close()


def orchestrator(compiler: Compiler, *, budget: Budget | None = None) -> Orchestrator:
    return Orchestrator(
        compiler, FakeAdapter(responses=[response(json.dumps(PLANS["single"]))]),
        config=OrchestratorConfig(budget=budget or Budget(cost_usd=10, compute_s=300)),
    )


async def licensed_result(compiler: Compiler) -> CompileResult:
    result = await compiler.compile(
        Subtask(task="Summarize numbers", domain="synthesizer"),
        overrides={"lifecycle": "expire", "model_target": "fake"},
    )
    assert result.artifact is not None and result.artifact.licensed
    assert result.report is not None and not result.failures
    assert 0 in result.stages and 4 in result.stages
    return result


async def test_decomposition_provider_exception_fails_closed_without_content(
    compiler: Compiler,
) -> None:
    service = Orchestrator(compiler, FailingAdapter())
    result = await service.run("goal")
    assert not result.accepted and result.tasks == ()
    assert result.failures == ("decomposition_failed",)
    assert SECRET not in result.model_dump_json()
    assert not compiler.artifact_root.exists()
    rows = compiler.runtime.store.rows()
    assert len(rows) == 1 and not rows[0].accepted
    assert active_wallets() == ()


@pytest.mark.parametrize("scope", ["local_dollars", "overall_dollars", "local_deadline"])
async def test_compile_budget_exceptions_use_real_wallet_state(
    compiler: Compiler, monkeypatch: pytest.MonkeyPatch, scope: str,
) -> None:
    pending = PendingAdapter()
    calls = 0

    async def limited_compile(source: object, **kwargs: object) -> CompileResult:
        nonlocal calls
        calls += 1
        # This compiler seam does actual guarded work, not a fabricated budget exception.
        adapter = pending if scope == "local_deadline" else FakeAdapter(
            responses=[ModelResponse(
                text=SECRET, tokens_in=10, tokens_out=10, cost_usd=2, model="fake",
            )],
        )
        budget = Budget(
            cost_usd=10 if scope == "overall_dollars" else 0.1,
            compute_s=0.03 if scope == "local_deadline" else 60,
        )
        context = RunContext(adapter, budget)
        try:
            await context.call("compiler work")
        finally:
            await context._close()
        raise AssertionError("Compiler work must exceed its dollar or time cap")

    monkeypatch.setattr(compiler, "compile", limited_compile)
    result = await orchestrator(compiler, budget=Budget(cost_usd=1, compute_s=60)).run("goal")
    expected = "budget_exhausted" if scope == "overall_dollars" else "compile_budget"
    assert not result.accepted
    assert result.tasks[0].failures == (expected,)
    assert result.tasks[0].output is None
    assert result.tasks[0].artifact_directory is None
    assert result.failures == (
        ("budget_exhausted", "task_failed") if scope == "overall_dollars" else ("task_failed",)
    )
    assert calls == 1 and not compiler.artifact_root.exists()
    if scope == "local_deadline":
        assert pending.started.is_set() and pending.cancelled
    assert SECRET not in result.model_dump_json()
    assert active_wallets() == ()


@pytest.mark.parametrize("forgery", [
    "approval", "stage0", "stage4", "missing_artifact", "missing_report", "report_mismatch",
    "spec_mismatch", "installed", "cost_cap", "time_cap", "code_changed", "failure_codes",
])
async def test_compiler_result_cannot_bypass_independent_delivery_gates(
    compiler: Compiler, monkeypatch: pytest.MonkeyPatch, forgery: str,
) -> None:
    genuine = await licensed_result(compiler)
    artifact = genuine.artifact
    assert artifact is not None and genuine.report is not None
    forged = genuine
    overrides: dict[str, JsonValue] = {}
    if forgery == "approval":
        forged = genuine.model_copy(update={"approval_required": True})
    elif forgery in {"stage0", "stage4"}:
        omitted = 0 if forgery == "stage0" else 4
        forged = genuine.model_copy(update={
            "stages": tuple(stage for stage in genuine.stages if stage != omitted),
        })
    elif forgery == "missing_artifact":
        forged = genuine.model_copy(update={"artifact": None})
    elif forgery == "missing_report":
        forged = genuine.model_copy(update={"report": None})
    elif forgery == "report_mismatch":
        forged = genuine.model_copy(update={
            "report": genuine.report.model_copy(update={"verification_id": "forged"}),
        })
    elif forgery == "spec_mismatch":
        forged = genuine.model_copy(update={
            "spec": genuine.spec.model_copy(update={"task": "unverified task"}),
        })
    elif forgery == "installed":
        spec = genuine.spec.model_copy(update={"lifecycle": Lifecycle.INSTALL})
        forged = genuine.model_copy(update={
            "spec": spec, "artifact": artifact.model_copy(update={"spec": spec}),
        })
    elif forgery == "cost_cap":
        # Keep a genuinely licensed artifact, but ignore the host's lower dollar cap.
        overrides = {"budget": {"cost_usd": artifact.spec.budget.cost_usd / 2}}
    elif forgery == "time_cap":
        overrides = {"budget": {"compute_s": artifact.spec.budget.compute_s / 2}}
    elif forgery == "code_changed":
        (artifact.directory / "harness.py").write_text("# tampered after verification\n")
        assert not artifact.licensed
    else:
        forged = genuine.model_copy(update={
            "failures": ("quality_failed", SECRET, "quality_failed"),
        })

    async def forged_compile(source: object, **kwargs: object) -> CompileResult:
        return forged

    monkeypatch.setattr(compiler, "compile", forged_compile)
    before = len(compiler.runtime.store.rows())
    result = await orchestrator(compiler).run("goal", overrides=overrides)
    expected: tuple[str, ...] = (
        ("approval_required",) if forgery == "approval" else ("compile_failed",)
    )
    if forgery == "failure_codes":
        expected += ("quality_failed",)
    assert not result.accepted and result.failures == ("task_failed",)
    assert result.tasks[0].failures == expected
    assert result.tasks[0].output is None
    assert SECRET not in result.model_dump_json()
    # The only new runtime row is decomposition: rejected compiles never reach delivery.
    assert len(compiler.runtime.store.rows()) == before + 1
    assert not (artifact.directory / ".consumed").exists()
    assert (artifact.directory / "spec.json").is_file()
    assert (artifact.directory / "report.json").is_file()
    if forgery != "missing_artifact":
        assert not (artifact.directory / "harness.py").exists()
        assert not (artifact.directory / "preset.json").exists()


@pytest.mark.parametrize("failure", ["unlicensed", "expired", "run_failed"])
async def test_delivery_rechecks_license_consumption_and_provider_failure(
    compiler: Compiler, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    genuine = await licensed_result(compiler)
    artifact = genuine.artifact
    assert artifact is not None
    if failure == "expired":
        (artifact.directory / ".consumed").write_text("earlier-run\n")

    async def cached_compile(source: object, **kwargs: object) -> CompileResult:
        assert artifact.licensed
        return genuine

    monkeypatch.setattr(compiler, "compile", cached_compile)
    if failure == "unlicensed":
        # Independent delivery rejects a model switch even after _deliverable succeeds.
        wrong_model = FakeAdapter()
        wrong_model.model = "other"
        monkeypatch.setattr(compiler, "adapter_factory", lambda _: wrong_model)
    elif failure == "run_failed":
        monkeypatch.setattr(compiler, "adapter_factory", lambda _: FailingAdapter())
    before = len(compiler.runtime.store.rows())
    result = await orchestrator(compiler).run("goal")
    assert not result.accepted and result.failures == ("task_failed",)
    assert result.tasks[0].failures == (failure,)
    assert result.tasks[0].output is None
    assert result.tasks[0].spec_hash == genuine.spec.spec_hash
    assert SECRET not in result.model_dump_json()
    rows = compiler.runtime.store.rows()
    assert len(rows) == before + 2 and not rows[-1].accepted
    assert rows[-1].spec_hash == genuine.spec.spec_hash
    assert not (artifact.directory / "harness.py").exists()
    assert (artifact.directory / "spec.json").is_file()
    assert (artifact.directory / "report.json").is_file()
    if failure == "expired":
        assert (artifact.directory / ".consumed").read_text() == "earlier-run\n"
    else:
        assert (artifact.directory / ".consumed").exists() == (failure == "run_failed")
    assert active_wallets() == ()


def pipeline_spec() -> Spec:
    return Spec.model_validate(SPECS["ok"]).model_copy(update={
        "output_schema": {"type": "object"},
        "pipeline": (PipelineStep(
            name="first", preset="extractor", model="fake", budget=Budget(),
        ),),
    })


async def execute_pipeline(context: RunContext, spec: Spec) -> JsonValue:
    return await run_pipeline(
        context, spec, {}, preset_name="extractor", params={"verifier_depth": 0},
        components=("extraction",),
    )


@pytest.mark.parametrize("invalid", ["duplicate_names", "too_many"])
async def test_pipeline_revalidates_forged_stage_names_and_count(invalid: str) -> None:
    spec = pipeline_spec()
    stage = spec.pipeline[0]
    stages = ((stage, stage) if invalid == "duplicate_names" else tuple(
        stage.model_copy(update={"name": f"stage{i}"}) for i in range(17)
    ))
    # model_copy intentionally bypasses Spec validation: the runtime must not trust it.
    forged = spec.model_copy(update={"pipeline": stages})
    adapter = FakeAdapter()
    context = RunContext(adapter, forged.budget)
    with pytest.raises(PipelineError, match="distinct names and number at most 16"):
        await execute_pipeline(context, forged)
    assert adapter.calls == 0 and context.cost_usd == 0 and context.tokens_in == 0
    assert not context._children and active_wallets() == ()


async def test_unfinished_stage_call_is_cancelled_billed_and_blocks_next_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = pipeline_spec()
    spec = spec.model_copy(update={
        "pipeline": (*spec.pipeline, spec.pipeline[0].model_copy(update={"name": "never"})),
    })
    adapter = PendingAdapter()
    context = RunContext(adapter, spec.budget)
    children: list[RunContext] = []
    pending: list[asyncio.Task[str]] = []

    def faulty_build(
        self: Preset, stage_spec: Spec, params: dict[str, JsonValue] | None = None,
        components: tuple[str, ...] | None = None,
    ) -> Harness:
        async def faulty_harness(context: RunContext, task: JsonValue) -> JsonValue:
            children.append(context)
            pending.append(asyncio.create_task(context.call("unfinished provider request")))
            await asyncio.wait_for(adapter.started.wait(), timeout=1)
            return {"unverified": "must never escape"}
        return faulty_harness

    monkeypatch.setattr(Preset, "build", faulty_build)
    try:
        with compilation_wallet(spec.budget) as wallet:
            with pytest.raises(BudgetExceeded, match="Pipeline stage left model calls unfinished"):
                await execute_pipeline(context, spec)
            assert wallet.cost_usd == pytest.approx(0.001)
            assert wallet.tokens == 100_000
        # Check production cleanup BEFORE the test's emergency cleanup can mask a leak.
        assert len(children) == len(pending) == 1  # No following stage executes.
        assert adapter.cancelled and pending[0].cancelled()
        assert children[0]._closed and not children[0]._calls
        assert not context._children and not context._calls
        assert context.tokens_in == 100_000 and context.cost_usd == pytest.approx(0.001)
        assert active_wallets() == ()
    finally:
        await context._close()
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
