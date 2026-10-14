"""Hermetic stage-zero coverage: models propose, code guards and records provenance."""

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import JsonValue, ValidationError

from architect.compiler import Compiler, inference
from architect.compiler.inference import InferenceError, Subtask, infer_spec
from architect.floors import HeldOutTasks
from architect.models import FakeAdapter, ModelRequest, ModelResponse
from architect.runtime import BudgetExceeded, Runtime
from architect.spec import Budget, FloorDefinition, FloorKind, Lifecycle, Spec
from architect.telemetry import TelemetryStore

FIXTURES = Path(__file__).parent / "fixtures" / "inference"


@pytest.fixture
def runtime(tmp_path: Path) -> Runtime:
    return Runtime(TelemetryStore(tmp_path / "telemetry"))


class RecordingFake(FakeAdapter):
    def __init__(self, name: str = "summary") -> None:
        super().__init__(fixture=FIXTURES / f"{name}.json")
        self.prompts: list[str] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.prompts.append(request.prompt)
        return await super().complete(request)


async def test_plain_request_is_model_driven(runtime: Runtime) -> None:
    adapter = RecordingFake()
    request = "summarize a table of numbers into two paragraphs for a parent"
    result = await infer_spec(request, adapter, runtime)
    assert adapter.calls == 1
    assert request in adapter.prompts[0]
    assert result.spec.domain == "synthesizer"
    assert result.spec.floor.kind == FloorKind.HUMAN_EDIT_RATE
    assert result.spec.floor.limit == 0.1
    assert not result.approval_required
    # Phase 14's host-only fields are never inferred, so provenance never names them.
    host_only = {"invalid_policy", "max_invalid_fraction", "input_limit_chars", "reply_json",
                 "plain"}
    assert result.spec.provenance == {
        field: "inferred"
        for field in Spec.model_fields
        if field != "provenance" and field not in host_only
    }
    assert runtime.store.rows()[0].cost_usd == 0.002
    assert runtime.store.rows()[0].floor_score is None


async def test_same_request_different_model_proposal(runtime: Runtime) -> None:
    result = await infer_spec("Choose a valid task", RecordingFake("environment"), runtime)
    assert result.spec.domain == "explorer"
    assert result.spec.tools == ("move",)


async def test_infers_heavy_pipeline_and_requires_approval(runtime: Runtime) -> None:
    adapter = RecordingFake("pipeline")
    result = await infer_spec(
        "I want an AI feature that turns uploaded tests into Bluebook-style online tests "
        "with answer keys.", adapter, runtime,
    )
    assert result.approval_required
    assert result.spec.lifecycle == Lifecycle.INSTALL
    assert [step.preset for step in result.spec.pipeline] == ["extractor", "planner", "verifier"]
    assert len({step.model for step in result.spec.pipeline}) == 3
    assert result.spec.floor.kind == FloorKind.HELD_OUT_TASKS
    assert result.spec.output_schema["additionalProperties"] is False
    assert "independent solving" in adapter.prompts[0]
    assert "MAXIMUM" in adapter.prompts[0]
    assert "calibration_dataset" in adapter.prompts[0]


async def test_user_precedence_deep_merge_and_owned_provenance(runtime: Runtime) -> None:
    overrides: dict[str, JsonValue] = {
        "task": "User task", "budget": {"cost_usd": 0.025},
        "input_schema": {"properties": {"label": {"type": "string"}}},
        "floor": {"limit": 0.05}, "provenance": {"domain": "user"},
        "lifecycle": "install",
    }
    original = json.dumps(overrides)
    result = await infer_spec("Draft something", RecordingFake("forged-provenance"),
                              runtime, overrides)
    spec = result.spec
    assert spec.task == "User task"
    assert spec.budget == Budget(cost_usd=0.025)
    assert spec.floor.limit == 0.05
    assert spec.floor.dataset == "summary-edits"
    assert spec.input_schema["properties"] == {
        "numbers": {"type": "array", "items": {"type": "number"}},
        "label": {"type": "string"},
    }
    assert spec.provenance["domain"] == "inferred"
    assert spec.provenance["task"] == spec.provenance["budget"] == "user"
    assert spec.provenance["floor"] == "user"
    assert "provenance" not in spec.provenance
    assert json.dumps(overrides) == original
    assert result.approval_required


async def test_expire_override_removes_install_approval(runtime: Runtime) -> None:
    result = await infer_spec("persistent feature", RecordingFake("pipeline"), runtime,
                              {"lifecycle": "expire", "pipeline": []})
    assert not result.approval_required
    assert result.spec.pipeline == ()
    assert result.spec.provenance["lifecycle"] == "user"


async def test_subtask_fields_are_user_overrides(runtime: Runtime) -> None:
    task = Subtask(task="Orchestrated job", domain="coder", tools=("read",),
                   budget=Budget(compute_s=75))
    result = await infer_spec(task, RecordingFake(), runtime, {"budget": {"cost_usd": 0.03}})
    assert result.spec.task == task.task
    assert result.spec.domain == "coder"
    assert result.spec.tools == ("read",)
    assert result.spec.budget == Budget(compute_s=75, cost_usd=0.03)
    assert all(result.spec.provenance[field] == "user" for field in task.model_fields_set)
    assert result.spec.provenance["input_schema"] == "inferred"
    assert result.spec.provenance["output_schema"] == "inferred"
    assert result.spec.provenance["floor"] == "inferred"
    with pytest.raises(ValidationError):
        task.task = "changed"


async def test_subtask_defaults_preserve_actual_inferred_schemas(runtime: Runtime) -> None:
    adapter = RecordingFake()
    inferred = json.loads(json.loads((FIXTURES / "summary.json").read_text())[0]["text"])
    result = await infer_spec(Subtask(task="Orchestrated summary"), adapter, runtime)
    assert adapter.calls == 1
    assert result.spec.input_schema == inferred["input_schema"]
    assert result.spec.output_schema == inferred["output_schema"]
    assert result.spec.domain == "synthesizer"
    assert result.spec.budget == Budget(cost_usd=0.1, compute_s=60)
    assert result.spec.provenance["task"] == "user"
    assert all(result.spec.provenance[field] == "inferred" for field in (
        "input_schema", "output_schema", "domain", "budget", "tools",
    ))


@pytest.mark.parametrize("from_subtask", [True, False])
async def test_explicit_empty_schemas_override_inference(
    runtime: Runtime, from_subtask: bool,
) -> None:
    task = Subtask(task="Orchestrated summary", input_schema={}, output_schema={})
    if from_subtask:
        result = await infer_spec(task, RecordingFake(), runtime)
    else:
        result = await infer_spec(Subtask(task=task.task), RecordingFake(), runtime,
                                  {"input_schema": {}, "output_schema": {}})
    assert result.spec.input_schema == result.spec.output_schema == {}
    assert result.spec.provenance["input_schema"] == "user"
    assert result.spec.provenance["output_schema"] == "user"


async def test_explicit_user_override_wins_over_subtask_and_model(runtime: Runtime) -> None:
    task = Subtask(task="Subtask title", domain="coder", output_schema={"type": "number"},
                   budget=Budget(cost_usd=0.04, compute_s=75))
    result = await infer_spec(task, RecordingFake(), runtime, {
        "task": "User title", "domain": "verifier", "output_schema": {"type": "boolean"},
        "budget": {"cost_usd": 0.03},
    })
    assert result.spec.task == "User title"
    assert result.spec.domain == "verifier"
    assert result.spec.output_schema == {"type": "boolean"}
    assert result.spec.budget == Budget(cost_usd=0.03, compute_s=75)
    assert all(result.spec.provenance[field] == "user" for field in (
        "task", "domain", "output_schema", "budget",
    ))


async def test_high_token_inference_is_allowed_and_metered(runtime: Runtime) -> None:
    adapter = RecordingFake("over-budget")
    result = await infer_spec("Valid task", adapter, runtime, {"budget": {"tokens": 0}})
    assert adapter.calls == 1
    assert result.spec.domain == "synthesizer"
    assert "tokens" not in result.spec.budget.model_dump()
    row = runtime.store.rows()[0]
    assert row.accepted
    assert row.tokens_in == 50_000 and row.tokens_out == 450
    assert row.cost_usd == pytest.approx(0.002)


class GridEnvironment:
    def __init__(self) -> None:
        self.actions: list[int] = []

    async def probe(self, max_actions: int = 3) -> dict[str, JsonValue]:
        self.actions.append(max_actions)
        return {"observation": [[0, 1], [0, 0]], "actions": ["move"], "private": "probe-only"}

    @property
    def hidden_state(self) -> None:
        raise AssertionError("Inference accessed hidden environment information")


async def test_public_environment_probe_only(runtime: Runtime) -> None:
    env = GridEnvironment()
    adapter = RecordingFake("environment")
    result = await infer_spec(env, adapter, runtime)
    assert env.actions == [3]
    assert "probe-only" in adapter.prompts[0]
    assert result.spec.domain == "explorer"
    assert result.spec.floor.kind == FloorKind.HELD_OUT_GAME_SCORE
    assert all(value == "inferred" for value in result.spec.provenance.values())
    assert "probe-only" not in str([row.model_dump() for row in runtime.store.rows()])


async def test_probe_has_same_compute_guard(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(inference, "_INFERENCE_BUDGET", Budget(compute_s=0.01))
    cancelled = asyncio.Event()

    class StuckEnvironment:
        async def probe(self, max_actions: int = 3) -> dict[str, JsonValue]:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            return {}

    adapter = RecordingFake()
    # The guard still cancels the probe; an exhausted compute budget is reported
    # as budget (never InferenceError) so callers classify it as compile_budget.
    with pytest.raises(BudgetExceeded):
        await infer_spec(StuckEnvironment(), adapter, runtime)
    assert cancelled.is_set()
    assert adapter.calls == 0
    assert not runtime.store.rows()[0].accepted


async def test_malformed_model_json_never_falls_back(runtime: Runtime) -> None:
    adapter = FakeAdapter.from_fixture(FIXTURES / "invalid.json")
    count = len(json.loads((FIXTURES / "invalid.json").read_text()))
    for _ in range(count):
        with pytest.raises(InferenceError):
            await infer_spec("Valid task", adapter, runtime)
    assert adapter.calls == count
    assert not any(row.accepted for row in runtime.store.rows())


@pytest.mark.parametrize("request_text", ["", "  ", "x" * 65_537],
                         ids=["empty", "whitespace", "oversize"])
async def test_invalid_requests_skip_model(runtime: Runtime, request_text: str) -> None:
    adapter = RecordingFake()
    with pytest.raises(InferenceError):
        await infer_spec(request_text, adapter, runtime)
    assert adapter.calls == 0


@pytest.mark.parametrize("overrides", [
    {"licensed": True}, {"report": {}}, {"approval_required": False},
    {"budget": {"cost_usd": -1}}, {"domain": ""}, {"model_target": 1},
])
async def test_invalid_overrides_fail_closed(runtime: Runtime,
                                           overrides: dict[str, JsonValue]) -> None:
    with pytest.raises(InferenceError):
        await infer_spec("Valid request", RecordingFake(), runtime, overrides)


async def test_inference_model_budget_preflight(runtime: Runtime) -> None:
    adapter = RecordingFake("over-cost")
    # A reservation the inference wallet cannot cover is a budget refusal, made
    # before the call is sent; it is not a proposal failure.
    with pytest.raises(BudgetExceeded):
        await infer_spec("Valid task", adapter, runtime)
    assert adapter.calls == 0
    assert not runtime.store.rows()[0].accepted
    assert runtime.store.rows()[0].tokens_in == 0


async def test_probe_failure_and_oversize_context_are_closed(runtime: Runtime) -> None:
    class BrokenEnvironment:
        async def probe(self, max_actions: int = 3) -> dict[str, JsonValue]:
            raise RuntimeError("sensitive environment state")

    class LargeEnvironment:
        async def probe(self, max_actions: int = 3) -> dict[str, JsonValue]:
            return {"observation": "x" * 65_537}

    adapter = RecordingFake()
    for env in (BrokenEnvironment(), LargeEnvironment()):
        with pytest.raises(InferenceError) as exc:
            await infer_spec(env, adapter, runtime)
        assert "sensitive" not in str(exc.value)
    assert adapter.calls == 0
    assert not any(row.accepted for row in runtime.store.rows())



class SlowFake(RecordingFake):
    """A CLI-like model that answers correctly, but only after a delay."""

    def __init__(self, delay_s: float = 0.5) -> None:
        super().__init__()
        self.delay_s = delay_s

    async def complete(self, request: ModelRequest) -> ModelResponse:
        await asyncio.sleep(self.delay_s)
        return await super().complete(request)


async def test_inference_compute_exhaustion_is_a_budget_signal(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(inference, "_INFERENCE_BUDGET", Budget(cost_usd=1.0, compute_s=0.2))
    with pytest.raises(BudgetExceeded):
        await infer_spec("summarize a table of numbers", SlowFake(), runtime)
    rows = runtime.store.rows()
    assert len(rows) == 1 and not rows[0].accepted


async def test_compile_reports_inference_budget_not_a_bad_proposal(
    tmp_path: Path, runtime: Runtime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(inference, "_INFERENCE_BUDGET", Budget(cost_usd=1.0, compute_s=0.2))

    def floors(definition: FloorDefinition) -> HeldOutTasks:
        return HeldOutTasks(definition, [])

    service = Compiler(
        tmp_path / "artifacts", runtime, lambda model: FakeAdapter(model=model), floors,
        inference_adapter=SlowFake(),
    )
    # No proposal exists yet, so there is no CompileResult to attach
    # ("compile_budget",) to; the compiler raises the budget signal itself, which
    # a game child records as game_budget_exceeded rather than game_execution_failed.
    with pytest.raises(BudgetExceeded) as exc:
        await service.compile("summarize a table of numbers")
    assert not isinstance(exc.value, InferenceError)
    assert not any(row.accepted for row in runtime.store.rows())


async def test_probe_timeout_is_a_budget_signal_not_an_inference_error(
    runtime: Runtime,
) -> None:
    class StalledEnvironment:
        async def probe(self, max_actions: int = 3) -> dict[str, JsonValue]:
            raise TimeoutError("environment stalled")

    adapter = RecordingFake()
    with pytest.raises(BudgetExceeded) as exc:
        await infer_spec(StalledEnvironment(), adapter, runtime)
    assert "stalled" not in str(exc.value)
    assert adapter.calls == 0


class PrefixRecordingFake(FakeAdapter):
    """Keeps the fixed instruction prefix (the system text) of every request."""

    def __init__(self) -> None:
        super().__init__(fixture=FIXTURES / "summary.json")
        self.prefixes: list[str | None] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.prefixes.append(request.cache_prefix)
        return await super().complete(request)


async def test_instructions_require_complete_json_schema_objects(runtime: Runtime) -> None:
    """The fixed prefix asks for full JSON Schema objects and is byte-stable across calls."""
    first_adapter, second_adapter = PrefixRecordingFake(), PrefixRecordingFake()
    await infer_spec("summarize a table", first_adapter, runtime)
    await infer_spec("summarize another table", second_adapter, runtime)
    (first,), (second,) = first_adapter.prefixes, second_adapter.prefixes
    assert first is not None and first == second
    assert (
        "input_schema and output_schema must each be a complete JSON Schema object of the form\n"
        '{"type":"object","properties":{...},"required":[...],"additionalProperties":false};\n'
        "never an empty object and never a bare map of property names to types."
    ) in first
    assert first.index("never an empty object") < first.index("Spec JSON schema:")


async def test_instructions_ask_for_plain_types_unless_the_request_states_a_format(
    runtime: Runtime,
) -> None:
    """The walkthrough's invoice build: Stage 0 limited invoice_number to letters and digits
    the request never asked for, and the drafted INV-4567 broke it."""
    adapter = PrefixRecordingFake()
    await infer_spec("Pull the invoice number, date, vendor and total out of invoice text",
                     adapter, runtime)
    (prefix,) = adapter.prefixes
    assert prefix is not None
    assert (
        "Give each property a plain type. Add a pattern, format, minLength, maxLength or enum "
        "only\nwhen the request states that exact format or that exact list of values"
    ) in prefix
    assert prefix.index("plain type") < prefix.index("Spec JSON schema:")


@pytest.mark.parametrize("overrides", [None, {"task": "Explore the grid"}])
async def test_probe_context_chars_is_the_exact_probe_budget(
    runtime: Runtime, overrides: dict[str, JsonValue] | None
) -> None:
    budget = inference.probe_context_chars(overrides)
    assert 0 < budget <= inference.MAX_CONTEXT_CHARS

    class Probe:
        def __init__(self, chars: int) -> None:
            self.payload: dict[str, JsonValue] = {"o": "x" * (chars - len(json.dumps({"o": ""})))}
            assert len(json.dumps(self.payload)) == chars

        async def probe(self, max_actions: int = 3) -> dict[str, JsonValue]:
            return self.payload

    adapter = RecordingFake("environment")
    result = await infer_spec(Probe(budget), adapter, runtime, overrides)
    assert adapter.calls == 1 and result.spec.domain == "explorer"
    with pytest.raises(InferenceError):
        await infer_spec(Probe(budget + 1), adapter, runtime, overrides)
    assert adapter.calls == 1


def _proposing(execution_config: dict[str, JsonValue]) -> FakeAdapter:
    """A model whose proposal is the summary fixture's, with this ``execution_config``."""
    proposal = json.loads(json.loads((FIXTURES / "summary.json").read_text())[0]["text"])
    proposal["execution_config"] = execution_config
    return FakeAdapter(responses=[ModelResponse(
        text=json.dumps(proposal), model="fake", tokens_in=1, tokens_out=1, cost_usd=0.000002,
    )])


@pytest.mark.parametrize("exact", [False, True])
async def test_checks_come_only_from_the_users_own_spec(runtime: Runtime, exact: bool) -> None:
    """A drafted declaration never reaches the plan card or the preset: the checks a plan
    shows are the user's own rules, never ones a model wrote."""
    drafted: dict[str, JsonValue] = {"checks": ["x"], "mode": "fast"}
    result = await infer_spec("Valid task", _proposing(drafted), runtime, exact_overrides=exact)
    assert result.spec.execution_config == {"mode": "fast"}
    # A user field without checks keeps them out too; one with checks keeps the user's.
    pinned: dict[str, JsonValue] = {"execution_config": {"mode": "slow"}}
    result = await infer_spec("Valid task", _proposing(drafted), runtime, pinned,
                              exact_overrides=exact)
    assert "checks" not in result.spec.execution_config
    assert result.spec.execution_config["mode"] == "slow"
    own: dict[str, JsonValue] = {"execution_config": {"checks": ["mine"]}}
    result = await infer_spec("Valid task", _proposing(drafted), runtime, own,
                              exact_overrides=exact)
    assert result.spec.execution_config["checks"] == ["mine"]
    assert result.spec.provenance["execution_config"] == "user"
