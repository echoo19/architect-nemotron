"""Orchestrator contract on deterministic fake adapters, with the real compiler.

Every subtask goes through Compiler.compile (Stage 0 inference, library lookup,
tuning and a fresh Stage 4) and Runtime.run. Nothing here fakes a task result.
"""

import ast
import asyncio
import json
import time
from pathlib import Path

import pytest
from pydantic import JsonValue, ValidationError

import architect.orchestrator as orchestration
from architect.compiler import CompileConfig, Compiler, CompileResult
from architect.floors import EvaluationCase, HeldOutTasks
from architect.library import HarnessLibrary
from architect.models import FakeAdapter, ModelAdapter, ModelRequest, ModelResponse, UsageQuote
from architect.orchestrator import (
    GoalPlan,
    OrchestrationError,
    Orchestrator,
    OrchestratorConfig,
    PlannedTask,
)
from architect.runtime import RunResult, Runtime
from architect.spec import Budget, FloorDefinition, HarnessArtifact, Spec, TestReport
from architect.telemetry import TelemetryStore, categorical

FIXTURES = Path(__file__).parent / "fixtures" / "orchestration"
PLANS: dict[str, dict[str, JsonValue]] = json.loads((FIXTURES / "plans.json").read_text())
SPECS: dict[str, dict[str, JsonValue]] = json.loads((FIXTURES / "specs.json").read_text())
INVALID: dict[str, str] = json.loads((FIXTURES / "invalid_plans.json").read_text())
HOST: dict[str, JsonValue] = {"output_schema": {"type": "string"}}


def response(text: str, tokens: int = 100) -> ModelResponse:
    return ModelResponse(
        text=text, tokens_in=tokens, tokens_out=tokens, cost_usd=tokens * 2e-6, model="fake"
    )


def scripted(*texts: str, tokens: int = 100) -> FakeAdapter:
    return FakeAdapter(responses=[response(text, tokens) for text in texts])


def floor_factory(definition: FloorDefinition) -> HeldOutTasks:
    expected = "never" if definition.dataset == "unreachable-cases" else "ok"
    return HeldOutTasks(
        definition,
        [EvaluationCase(case_id=f"c{i}", task={"action": i}, expected=expected) for i in range(4)],
    )


class DeliveryLog:
    """Adapter factory for compiled agents; records only this test's prompts."""

    def __init__(self, delay: float = 0.0) -> None:
        self.prompts: list[str] = []
        self.delay = delay
        self.active = 0
        self.peak = 0

    def __call__(self, model: str) -> ModelAdapter:
        log = self

        class Recording(FakeAdapter):
            async def complete(self, request: ModelRequest) -> ModelResponse:
                log.prompts.append(request.prompt)
                if '"dependencies"' in request.prompt and log.delay:
                    log.active += 1
                    log.peak = max(log.peak, log.active)
                    try:
                        await asyncio.sleep(log.delay)
                    finally:
                        log.active -= 1
                return await super().complete(request)

        return Recording(model=model)

    def deliveries(self) -> list[str]:
        return [prompt for prompt in self.prompts if '"dependencies"' in prompt]


class RecordingRuntime(Runtime):
    def __init__(self, store: TelemetryStore) -> None:
        super().__init__(store)
        self.delivered: list[JsonValue] = []

    async def run(
        self, artifact: HarnessArtifact, task: JsonValue, adapter: ModelAdapter
    ) -> RunResult:
        self.delivered.append(task)
        return await super().run(artifact, task, adapter)


class Setup:
    def __init__(
        self,
        tmp_path: Path,
        plan: str,
        specs: list[str],
        *,
        config: OrchestratorConfig | None = None,
        delivery: DeliveryLog | None = None,
        decomposition: ModelAdapter | None = None,
        inference_tokens: list[int] | None = None,
    ) -> None:
        self.runtime = RecordingRuntime(TelemetryStore(tmp_path / "telemetry"))
        self.library = HarnessLibrary(tmp_path / "library")
        tokens = inference_tokens or [100] * len(specs)
        self.inference = FakeAdapter(
            responses=[
                response(json.dumps(SPECS[name]), count)
                for name, count in zip(specs, tokens, strict=True)
            ]
        )
        self.delivery = delivery or DeliveryLog()
        self.compiler = Compiler(
            tmp_path / "artifacts",
            self.runtime,
            self.delivery,
            floor_factory,
            config=CompileConfig(max_candidates=1, enable_rewriting=False),
            library=self.library,
            inference_adapter=self.inference,
        )
        self.decomposition = decomposition or scripted(plan)
        self.orchestrator = Orchestrator(self.compiler, self.decomposition, config=config)


def plan_text(name: str) -> str:
    return json.dumps(PLANS[name])


def spec_of(directory: Path | None) -> Spec:
    assert directory is not None
    return Spec.model_validate_json((directory / "spec.json").read_text(encoding="utf-8"))


def report_of(directory: Path | None) -> TestReport:
    assert directory is not None
    return TestReport.model_validate_json((directory / "report.json").read_text(encoding="utf-8"))


async def test_goal_compiles_verifies_and_delivers_every_task_with_library_reuse(
    tmp_path: Path,
) -> None:
    setup = Setup(tmp_path, plan_text("similar"), ["ok", "ok", "ok"])
    result = await setup.orchestrator.run("summarize my tables", overrides=HOST)

    assert result.accepted and result.failures == ()
    assert [task.id for task in result.tasks] == ["summary-a", "summary-b", "combine"]
    assert all(task.accepted and task.output == "ok" for task in result.tasks)
    # Stage 0 inference ran once per subtask through the compiler.
    assert setup.inference.calls == 3
    # The first compile had no template; later similar subtasks warm-started from it.
    assert [task.warm_started for task in result.tasks] == [False, True, True]
    reports = [report_of(task.artifact_directory) for task in result.tasks]
    assert all(report.passed and report.held_out_count == 2 for report in reports)
    # Every reuse still produced its own fresh Stage 4 verification.
    assert len({report.verification_id for report in reports}) == 3
    for task in result.tasks:
        directory = task.artifact_directory
        assert directory is not None
        spec = spec_of(directory)
        assert task.spec_hash == spec.spec_hash
        assert spec.lifecycle == "expire" and spec.provenance["task"] == "user"
        assert (directory / ".consumed").is_file()
        assert not (directory / "harness.py").exists()
    # The library keeps its non-runnable template after delivered code is removed.
    template = setup.library.lookup(spec_of(result.tasks[0].artifact_directory), "fake")
    assert template is not None and (template.directory / "harness.py").is_file()

    deliveries = setup.delivery.deliveries()
    assert len(deliveries) == 3
    combined = json.loads(deliveries[2])["input"]
    assert combined == {
        "dependencies": {"summary-a": "ok", "summary-b": "ok"},
        "input": {"style": "brief"},
    }
    first = json.loads(deliveries[0])["input"]
    assert first == {"dependencies": {}, "input": {"numbers": [1, 2, 3]}}


async def test_decomposition_is_one_guarded_model_call_through_evaluate(tmp_path: Path) -> None:
    class Recorder(FakeAdapter):
        prompts: list[str] = []

        async def complete(self, request: ModelRequest) -> ModelResponse:
            self.prompts.append(request.prompt)
            return await super().complete(request)

    decomposition = Recorder(responses=[response(plan_text("single"))])
    setup = Setup(tmp_path, "", ["ok"], decomposition=decomposition)
    result = await setup.orchestrator.run("a goal nobody keyword-routes", overrides=HOST)
    assert result.accepted
    assert decomposition.calls == 1
    assert "a goal nobody keyword-routes" in decomposition.prompts[0]
    assert '"max_tasks": 8' in decomposition.prompts[0]
    rows = [
        row for row in setup.runtime.store.rows() if row.task_type == categorical("orchestrator")
    ]
    assert len(rows) == 1 and rows[0].accepted
    assert rows[0].tokens_in == 100 and rows[0].component_set == (categorical("decomposition"),)


async def test_high_decomposition_tokens_are_metadata_not_a_quota(tmp_path: Path) -> None:
    class HighUsage(FakeAdapter):
        def quote(self, request: ModelRequest) -> UsageQuote:
            assert request.max_output_tokens is None
            return UsageQuote(tokens_in=1, tokens_out=1, cost_usd=0.0002)

    plan = json.loads(plan_text("single"))
    plan["tasks"][0]["budget"] = {"tokens": -1, "cost_usd": 0.1, "compute_s": 60}
    decomposition = HighUsage(responses=[
        response(json.dumps(plan)).model_copy(update={
            "tokens_in": 2_000_000, "tokens_out": 1_000_000,
        }),
    ])
    setup = Setup(tmp_path, "", ["ok"], decomposition=decomposition)
    result = await setup.orchestrator.run("goal", overrides=HOST)
    assert result.accepted and result.tasks[0].output == "ok"
    budget = spec_of(result.tasks[0].artifact_directory).budget
    assert not hasattr(budget, "tokens") and "tokens" not in budget.model_dump()
    rows = [row for row in setup.runtime.store.rows()
            if row.task_type == categorical("orchestrator")]
    assert len(rows) == 1 and rows[0].accepted
    assert rows[0].tokens_in + rows[0].tokens_out == 3_000_000
    assert rows[0].cost_usd == pytest.approx(0.0002)


async def test_plan_comes_only_from_the_adapter(tmp_path: Path) -> None:
    first = Setup(tmp_path / "a", plan_text("single"), ["ok"])
    second = Setup(tmp_path / "b", plan_text("independent_three"), ["ok", "ok", "ok"])
    goal = "identical goal"
    left = await first.orchestrator.run(goal, overrides=HOST)
    right = await second.orchestrator.run(goal, overrides=HOST)
    assert [task.id for task in left.tasks] == ["only"]
    assert [task.id for task in right.tasks] == ["one", "two", "three"]


@pytest.mark.parametrize("case", sorted(INVALID))
async def test_invalid_or_forged_plans_are_rejected_before_any_compile(
    tmp_path: Path, case: str
) -> None:
    setup = Setup(
        tmp_path,
        INVALID[case],
        [],
        config=OrchestratorConfig(max_tasks=4),
    )
    overrides: dict[str, JsonValue] = {**HOST, "tools": ["read"]}
    result = await setup.orchestrator.run("goal", overrides=overrides)
    assert not result.accepted
    assert result.tasks == ()
    assert result.failures == ("invalid_plan",)
    assert setup.inference.calls == 0 and setup.runtime.delivered == []
    assert not (tmp_path / "artifacts").exists()
    rows = setup.runtime.store.rows()
    assert len(rows) == 1 and rows[0].task_type == categorical("orchestrator")


def test_plan_models_are_strict_frozen_dags() -> None:
    task = PlannedTask(id="a", request="Do a", input={"x": 1})
    assert task.budget == Budget() and task.domain == "general" and task.model == "fake"
    with pytest.raises(ValidationError):
        PlannedTask(id="a", request="Do a", input=None, licensed=True)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        task.request = "changed"
    with pytest.raises(ValidationError):
        GoalPlan(tasks=())
    with pytest.raises(ValidationError):
        GoalPlan(tasks=(task.model_copy(update={"depends_on": ("a",)}),))
    config = OrchestratorConfig()
    assert config.budget == Budget(cost_usd=10, compute_s=300)
    assert (config.max_tasks, config.max_depth, config.parallelism) == (8, 2, 1)
    for bad in ({"max_tasks": 0}, {"max_depth": 0}, {"parallelism": 0}, {"extra": 1}):
        with pytest.raises(ValidationError):
            OrchestratorConfig.model_validate(bad)


async def test_failed_dependency_blocks_downstream_but_not_independent_work(
    tmp_path: Path,
) -> None:
    setup = Setup(tmp_path, plan_text("chain"), ["unreachable", "ok"])
    result = await setup.orchestrator.run("goal", overrides=HOST)
    hard, after, independent = result.tasks
    assert not result.accepted and result.failures == ("task_failed",)
    assert not hard.accepted and "compile_failed" in hard.failures
    assert hard.output is None
    report = report_of(hard.artifact_directory)
    assert not report.passed
    # A rejected compile is never delivered, and its generated code does not linger.
    assert hard.artifact_directory is not None
    assert not (hard.artifact_directory / ".consumed").exists()
    assert not (hard.artifact_directory / "harness.py").exists()
    assert after == after.model_copy(
        update={"accepted": False, "failures": ("dependency_failed",), "output": None}
    )
    assert after.spec_hash is None and after.artifact_directory is None
    assert independent.accepted and independent.output == "ok"
    # The blocked task was never compiled: only two Stage 0 inferences.
    assert setup.inference.calls == 2
    assert len(setup.runtime.delivered) == 1


async def test_subtask_budgets_are_clamped_to_trusted_caps(tmp_path: Path) -> None:
    setup = Setup(tmp_path, plan_text("greedy"), ["ok"])
    overrides: dict[str, JsonValue] = {**HOST, "budget": {"cost_usd": 0.01, "compute_s": 120}}
    result = await setup.orchestrator.run("goal", overrides=overrides)
    assert result.accepted
    spec = spec_of(result.tasks[0].artifact_directory)
    assert spec.budget == Budget(cost_usd=0.01, compute_s=120)
    assert spec.provenance["budget"] == "user"


async def test_trusted_overrides_cannot_raise_the_operator_cap(tmp_path: Path) -> None:
    config = OrchestratorConfig(budget=Budget(cost_usd=1, compute_s=120))
    setup = Setup(tmp_path, plan_text("greedy"), ["ok"], config=config)
    overrides: dict[str, JsonValue] = {**HOST, "budget": {"cost_usd": 99.0, "compute_s": 9999}}
    result = await setup.orchestrator.run("goal", overrides=overrides)
    assert result.accepted
    assert spec_of(result.tasks[0].artifact_directory).budget == config.budget


async def test_model_target_override_and_host_fields_apply(tmp_path: Path) -> None:
    setup = Setup(tmp_path, plan_text("single"), ["ok"])
    overrides: dict[str, JsonValue] = {**HOST, "model_target": "fake", "lifecycle": "expire"}
    result = await setup.orchestrator.run("goal", overrides=overrides)
    spec = spec_of(result.tasks[0].artifact_directory)
    assert spec.model_target == "fake" and spec.provenance["model_target"] == "user"
    assert spec.output_schema == {"type": "string"}


async def test_inferred_install_lifecycle_is_forced_ephemeral(tmp_path: Path) -> None:
    setup = Setup(tmp_path, plan_text("single"), ["install"])
    result = await setup.orchestrator.run("goal", overrides=HOST)
    assert result.accepted
    assert spec_of(result.tasks[0].artifact_directory).lifecycle == "expire"


async def test_repository_grant_comes_only_from_the_trusted_argument(tmp_path: Path) -> None:
    repo = tmp_path / "checkout"
    repo.mkdir()
    setup = Setup(tmp_path, plan_text("repo"), ["ok"])
    result = await setup.orchestrator.run("goal", repo=repo, overrides=HOST)
    assert result.accepted
    (delivered,) = setup.runtime.delivered
    assert isinstance(delivered, dict)
    assert delivered["repo"] == str(repo.resolve())
    assert delivered["input"] == {"repo": "/somewhere/else", "numbers": [1]}
    assert repo.is_dir()  # A user repository is never removed.

    other = Setup(tmp_path / "none", plan_text("repo"), ["ok"])
    await other.orchestrator.run("goal", overrides=HOST)
    (ungranted,) = other.runtime.delivered
    assert isinstance(ungranted, dict) and "repo" not in ungranted


async def test_operator_input_errors_raise(tmp_path: Path) -> None:
    setup = Setup(tmp_path, plan_text("single"), [])
    bad: list[dict[str, JsonValue]] = [
        {"unknown": 1},
        {"provenance": {}},
        {"lifecycle": "install"},
        {"budget": 5},
        {"budget": {"cost_usd": -1}},
        {"tools": "read"},
    ]
    for overrides in bad:
        with pytest.raises(OrchestrationError):
            await setup.orchestrator.run("goal", overrides=overrides)
    for goal in ("", "   ", "x" * 70_000):
        with pytest.raises(OrchestrationError):
            await setup.orchestrator.run(goal)
    with pytest.raises(OrchestrationError):
        await setup.orchestrator.run("goal", repo=tmp_path / "missing")
    assert setup.decomposition.calls == 0  # type: ignore[attr-defined]


async def test_shared_wallet_counts_decomposition_and_stops_remaining_work(
    tmp_path: Path,
) -> None:
    config = OrchestratorConfig(budget=Budget(cost_usd=0.005, compute_s=300))
    setup = Setup(
        tmp_path,
        plan_text("independent_three"),
        ["ok", "ok", "ok"],
        config=config,
        inference_tokens=[100, 3000, 100],
    )
    result = await setup.orchestrator.run("goal", overrides=HOST)
    one, two, three = result.tasks
    assert one.accepted
    # Stage 0 of the second agent would push the shared wallet past its cap.
    assert not two.accepted and "budget_exhausted" in two.failures
    assert three.failures == ("budget_exhausted",)
    assert setup.inference.calls == 1
    assert not result.accepted and "budget_exhausted" in result.failures
    assert len(setup.runtime.delivered) == 1


async def test_decomposition_over_cap_is_a_budget_failure(tmp_path: Path) -> None:
    config = OrchestratorConfig(budget=Budget(cost_usd=0.00015, compute_s=300))
    setup = Setup(tmp_path, plan_text("single"), [], config=config)
    result = await setup.orchestrator.run("goal", overrides=HOST)
    assert result == result.model_copy(
        update={"accepted": False, "tasks": (), "failures": ("budget_exhausted",)}
    )


async def test_per_agent_compute_budget_is_a_hard_stop(tmp_path: Path) -> None:
    plan = json.loads(plan_text("single"))
    plan["tasks"][0]["budget"] = {"cost_usd": 0.1, "compute_s": 1.0}
    delivery = DeliveryLog(delay=30)
    setup = Setup(tmp_path, json.dumps(plan), ["ok"], delivery=delivery)
    started = time.monotonic()
    result = await setup.orchestrator.run("goal", overrides=HOST)
    assert time.monotonic() - started < 15
    (task,) = result.tasks
    assert not task.accepted and task.failures == ("run_budget",)
    assert task.artifact_directory is not None
    assert (task.artifact_directory / ".consumed").is_file()
    assert not (task.artifact_directory / "harness.py").exists()
    assert result.failures == ("task_failed",)


async def test_overall_deadline_stops_delivery(tmp_path: Path) -> None:
    config = OrchestratorConfig(budget=Budget(cost_usd=10, compute_s=3))
    delivery = DeliveryLog(delay=60)
    setup = Setup(
        tmp_path,
        plan_text("independent_three"),
        ["ok", "ok", "ok"],
        config=config,
        delivery=delivery,
    )
    started = time.monotonic()
    result = await setup.orchestrator.run("goal", overrides=HOST)
    assert time.monotonic() - started < 15
    assert not result.accepted and "deadline" in result.failures
    assert not any(task.accepted for task in result.tasks)
    assert all("deadline" in task.failures for task in result.tasks[1:])


class SlowAdapter:
    model = "fake"

    def __init__(self) -> None:
        self.started = asyncio.Event()

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=1, tokens_out=1, cost_usd=0.0)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.started.set()
        await asyncio.sleep(60)
        raise AssertionError("unreachable")


async def test_slow_decomposition_hits_the_overall_deadline(tmp_path: Path) -> None:
    config = OrchestratorConfig(budget=Budget(cost_usd=10, compute_s=0.5))
    setup = Setup(tmp_path, "", [], config=config, decomposition=SlowAdapter())
    started = time.monotonic()
    result = await setup.orchestrator.run("goal")
    assert time.monotonic() - started < 10
    assert result.failures == ("deadline",) and result.tasks == ()


async def test_cancellation_propagates_after_cleanup(tmp_path: Path) -> None:
    adapter = SlowAdapter()
    setup = Setup(tmp_path, "", [], decomposition=adapter)
    running = asyncio.create_task(setup.orchestrator.run("goal"))
    await adapter.started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    (row,) = setup.runtime.store.rows()
    assert not row.accepted
    assert orchestration._DEPTH.get() == 0


class NestingAdapter:
    """A decomposition model whose call re-enters the orchestrator once."""

    model = "fake"

    def __init__(self) -> None:
        self.orchestrator: Orchestrator | None = None
        self.nested: list[tuple[str, ...]] = []
        self.depths: list[int] = []

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=1, tokens_out=1, cost_usd=0.0)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.depths.append(orchestration._DEPTH.get())
        if self.orchestrator is not None and not self.nested:
            self.nested.append(())
            inner = await self.orchestrator.run("nested goal")
            self.nested[-1] = inner.failures
        return ModelResponse(
            text="not a plan", tokens_in=1, tokens_out=1, cost_usd=0.0, model="fake"
        )


@pytest.mark.parametrize(("max_depth", "expected"), [(1, ("max_depth",)), (2, ("invalid_plan",))])
async def test_max_depth_spans_nested_factory_capability(
    tmp_path: Path, max_depth: int, expected: tuple[str, ...]
) -> None:
    adapter = NestingAdapter()
    setup = Setup(
        tmp_path,
        "",
        [],
        decomposition=adapter,
        config=OrchestratorConfig(max_depth=max_depth),
    )
    adapter.orchestrator = setup.orchestrator
    result = await setup.orchestrator.run("outer goal")
    assert result.failures == ("invalid_plan",)
    assert adapter.nested == [expected]
    assert adapter.depths[0] == 1
    assert orchestration._DEPTH.get() == 0


async def test_bounded_parallel_delivery(tmp_path: Path) -> None:
    delivery = DeliveryLog(delay=0.3)
    setup = Setup(
        tmp_path,
        plan_text("independent_three"),
        ["ok", "ok", "ok"],
        config=OrchestratorConfig(parallelism=2),
        delivery=delivery,
    )
    result = await setup.orchestrator.run("goal", overrides=HOST)
    assert result.accepted
    assert 1 <= delivery.peak <= 2
    assert [task.warm_started for task in result.tasks] == [False, True, True]


async def test_sequential_by_default(tmp_path: Path) -> None:
    delivery = DeliveryLog(delay=0.05)
    setup = Setup(tmp_path, plan_text("independent_three"), ["ok", "ok", "ok"], delivery=delivery)
    result = await setup.orchestrator.run("goal", overrides=HOST)
    assert result.accepted and delivery.peak == 1


async def test_unlicensed_compile_result_is_never_delivered(tmp_path: Path) -> None:
    setup = Setup(tmp_path, plan_text("single"), [])
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "harness.py").write_text("async def run(context, task):\n    return 'ok'\n")
    spec = Spec.model_validate(SPECS["ok"])
    forged = HarnessArtifact(
        spec=spec,
        preset="synthesizer",
        model="fake",
        components=("synthesis",),
        directory=outside,
    )

    async def compile_stub(source: object, **_: object) -> CompileResult:
        return CompileResult(spec=spec, artifact=forged, stages=(0, 1, 2, 4))

    setup.compiler.compile = compile_stub  # type: ignore[method-assign]
    result = await setup.orchestrator.run("goal", overrides=HOST)
    (task,) = result.tasks
    assert not task.accepted and task.failures == ("compile_failed",)
    assert setup.runtime.delivered == []
    # Cleanup is confined to the compiler's artifact root.
    assert (outside / "harness.py").is_file()


async def test_compile_exceptions_are_fixed_codes(tmp_path: Path) -> None:
    setup = Setup(tmp_path, plan_text("single"), [])

    async def explode(source: object, **_: object) -> CompileResult:
        raise RuntimeError("secret model text")

    setup.compiler.compile = explode  # type: ignore[method-assign]
    result = await setup.orchestrator.run("goal", overrides=HOST)
    assert result.tasks[0].failures == ("compile_failed",)
    assert "secret" not in result.model_dump_json()


def test_module_has_no_held_out_or_signing_capability() -> None:
    tree = ast.parse(Path(orchestration.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(alias.name for alias in node.names)
    for forbidden in (
        "architect.compiler.verification",
        "architect._authority",
        "architect.floors._access",
        "verify",
        "_sign_report",
    ):
        assert forbidden not in imported
    assert not {"held_out", "_verification_cases", "_sign_report"} & attributes
