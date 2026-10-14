"""Real preset pipelines: routing, evidence flow, verification, and hard caps."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import JsonValue

from architect.models import AdapterError, ModelRequest, ModelResponse, UsageQuote
from architect.presets import get_preset
from architect.runtime import BudgetExceeded, RunContext, Runtime
from architect.runtime.pipeline import PipelineError, run_pipeline
from architect.runtime.wallet import active_wallets, compilation_wallet
from architect.spec import Budget, FloorDefinition, FloorKind, PipelineStep, Spec
from architect.telemetry import TelemetryStore


class ScriptedAdapter:
    """Deterministic model seam; pipeline and preset execution remain production code."""

    def __init__(
        self,
        model: str,
        texts: list[str],
        *,
        tokens: int = 10,
        cost: float = 0.001,
        failure: bool = False,
        wait: bool = False,
        quote_tokens: int | None = None,
        quote_cost: float | None = None,
    ) -> None:
        self.model = model
        self.texts = texts
        self.tokens = tokens
        self.cost = cost
        self.failure = failure
        self.wait = wait
        self.quote_tokens = tokens if quote_tokens is None else quote_tokens
        self.quote_cost = cost if quote_cost is None else quote_cost
        self.requests: list[ModelRequest] = []
        self.started = asyncio.Event()
        self.cancelled = False

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=self.quote_tokens, tokens_out=0, cost_usd=self.quote_cost)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        index = len(self.requests)
        self.requests.append(request)
        self.started.set()
        if self.wait:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if self.failure:
            raise AdapterError("scripted failure")
        return ModelResponse(
            text=self.texts[index],
            tokens_in=self.tokens,
            tokens_out=0,
            cost_usd=self.cost,
            model=self.model,
        )


def step(
    name: str,
    *,
    preset: str = "extractor",
    model: str = "fake",
    budget: Budget | None = None,
    output: dict[str, JsonValue] | None = None,
    input_schema: dict[str, JsonValue] | None = None,
    params: dict[str, JsonValue] | None = None,
) -> PipelineStep:
    return PipelineStep(
        name=name,
        preset=preset,
        model=model,
        budget=budget or Budget(),
        input_schema=input_schema or {},
        output_schema=output or {},
        params={"verifier_depth": 0} if params is None else params,
    )


def specification(
    stages: tuple[PipelineStep, ...],
    *,
    budget: Budget | None = None,
    output: dict[str, JsonValue] | None = None,
) -> Spec:
    return Spec(
        task="Extract questions, independently solve, then diff answers",
        input_schema={"type": "object"},
        output_schema={"type": "object"} if output is None else output,
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="synthetic"),
        budget=budget or Budget(),
        pipeline=stages,
    )


async def execute(context: RunContext, spec: Spec, task: JsonValue = None) -> JsonValue:
    return await run_pipeline(
        context,
        spec,
        {} if task is None else task,
        preset_name="extractor",
        params={"verifier_depth": 0},
        components=("extraction",),
    )


async def test_three_models_run_in_order_with_full_evidence_and_gate() -> None:
    adapters = {
        "vision": ScriptedAdapter("vision", ['{"answer": 4}']),
        "solver": ScriptedAdapter("solver", ['{"answer": 4, "derived": true}']),
        "diff": ScriptedAdapter("diff", ['{"passed": true, "reason": "agrees"}']),
    }
    requested: list[str] = []

    def factory(model: str) -> ScriptedAdapter:
        requested.append(model)
        return adapters[model]

    spec = specification((
        step("extract", model="vision"),
        step("solve", preset="synthesizer", model="solver"),
        step("diff", preset="verifier", model="diff"),
    ))
    original: JsonValue = {"question": "2+2"}
    context = RunContext(ScriptedAdapter("fake", []), spec.budget, adapter_factory=factory)
    output = await execute(context, spec, original)
    assert output == {"answer": 4, "derived": True}
    assert requested == ["vision", "solver", "diff"]
    assert json.loads(adapters["vision"].requests[0].prompt)["input"] == original
    assert json.loads(adapters["diff"].requests[0].prompt)["input"] == {
        "original": original,
        "previous": output,
        "steps": {"extract": {"answer": 4}, "solve": output},
    }
    assert context.tokens_in == 30
    assert context.cost_usd == pytest.approx(0.003)
    assert {"vision", "solver", "diff"} <= context.models_used
    assert active_wallets() == ()


async def test_large_stage_evidence_is_not_silently_dropped() -> None:
    first: JsonValue = {"layout": "x" * 5000, "extracted_answer": 4}
    second: JsonValue = {"independent_answer": 4}
    adapter = ScriptedAdapter("fake", [
        json.dumps(first), json.dumps(second), '{"passed": true, "reason": "diff checked"}',
    ])
    spec = specification((step("extract"), step("solve"), step("diff", preset="verifier")))
    assert await execute(RunContext(adapter, spec.budget), spec) == second
    payload = json.loads(adapter.requests[-1].prompt)["input"]
    assert payload["steps"] == {"extract": first, "solve": second}
    assert payload["previous"] == second


@pytest.mark.parametrize("verdict", [
    '{"passed": false, "reason": "disagrees"}',
    '{"passed": "true", "reason": "bad type"}',
    '{"passed": true}',
    '{"passed": true, "reason": "ok", "licensed": true}',
    'not json',
])
async def test_final_verifier_failure_never_releases_payload(verdict: str) -> None:
    adapter = ScriptedAdapter("fake", ['{"answer": 4}', verdict])
    spec = specification((step("extract"), step("diff", preset="verifier")))
    context = RunContext(adapter, spec.budget)
    with pytest.raises(PipelineError):
        await execute(context, spec)
    assert context.tokens_in == 20
    assert len(adapter.requests) == 2


async def test_explicit_verifier_schema_is_normal_final_output_not_implicit_gate() -> None:
    adapter = ScriptedAdapter("fake", ['{"answer": 4}', '{"passed": false, "reason": "no"}'])
    verdict_schema: dict[str, JsonValue] = {
        "type": "object", "required": ["passed", "reason"],
        "properties": {"passed": {"type": "boolean"}, "reason": {"type": "string"}},
    }
    spec = specification((
        step("extract"), step("diff", preset="verifier", output=verdict_schema),
    ), output=verdict_schema)
    output = await execute(RunContext(adapter, spec.budget), spec)
    assert output == {"passed": False, "reason": "no"}


async def test_explicit_custom_verifier_output_schema_is_honored() -> None:
    adapter = ScriptedAdapter("fake", ['{"answer": 4}', '{"difference": 0}'])
    schema: dict[str, JsonValue] = {
        "type": "object", "required": ["difference"],
        "properties": {"difference": {"type": "integer"}},
    }
    spec = specification((step("extract"), step("diff", preset="verifier", output=schema)))
    assert await execute(RunContext(adapter, spec.budget), spec) == {"difference": 0}


@pytest.mark.parametrize("gate", [False, True])
async def test_outer_output_schema_still_gates_stage_payload(gate: bool) -> None:
    stages: tuple[PipelineStep, ...] = (step("extract", output={"type": "object"}),)
    texts = ['{"answer": 4}']
    if gate:
        stages += (step("diff", preset="verifier"),)
        texts.append('{"passed": true, "reason": "ok"}')
    spec = specification(stages, output={"type": "object", "required": ["missing"]})
    adapter = ScriptedAdapter("fake", texts)
    context = RunContext(adapter, spec.budget)
    with pytest.raises(PipelineError):
        await execute(context, spec)
    assert context.tokens_in == len(texts) * 10


async def test_first_primary_stage_uses_outer_params_then_explicit_stage_params() -> None:
    adapter = ScriptedAdapter("fake", ['{}', '{}', '{}', '{"passed": true, "reason": "ok"}'])
    spec = specification((
        step("intro", preset="synthesizer"),
        step("first", params={"verifier_depth": 1}),
        step("second", params={"verifier_depth": 1}),
    ))
    assert await run_pipeline(
        RunContext(adapter, spec.budget), spec, {}, preset_name="extractor",
        params={"verifier_depth": 0}, components=("extraction",),
    ) == {}
    assert len(adapter.requests) == 4
    assert all(request.max_output_tokens is None for request in adapter.requests)
    assert json.loads(adapter.requests[-1].prompt)["operation"] == "verify_candidate"


@pytest.mark.parametrize("scope", ["step", "run", "compile"])
async def test_step_run_and_compile_dollar_caps_deny_call_before_spend(scope: str) -> None:
    cap = Budget(cost_usd=0.0015)
    stage_budget = Budget(cost_usd=0.0005)
    spec = specification((
        step("first"), step("second", budget=stage_budget if scope == "step" else Budget()),
    ), budget=cap if scope == "run" else Budget())
    adapter = ScriptedAdapter("fake", ['{}', '{}'])
    context = RunContext(adapter, spec.budget)
    with compilation_wallet(cap if scope == "compile" else Budget()) as wallet:
        with pytest.raises(BudgetExceeded):
            await execute(context, spec)
        assert wallet.tokens == 10
        assert wallet.cost_usd == pytest.approx(0.001)
    assert len(adapter.requests) == 1
    assert context.tokens_in == 10
    assert context.cost_usd == pytest.approx(0.001)
    assert active_wallets() == ()


async def test_spend_before_pipeline_reduces_aggregate_allowance() -> None:
    spec = specification((step("first"), step("second")), budget=Budget(cost_usd=0.0025))
    adapter = ScriptedAdapter("fake", ['before', '{}', '{}'])
    context = RunContext(adapter, spec.budget)
    assert await context.call("prior work") == "before"
    with pytest.raises(BudgetExceeded):
        await execute(context, spec)
    assert len(adapter.requests) == 2
    assert context.tokens_in == 20


@pytest.mark.parametrize("kind", ["model", "schema", "underquote"])
async def test_failed_stage_usage_is_absorbed_exactly_once(kind: str) -> None:
    adapter = ScriptedAdapter(
        "fake", ["not-json"], failure=kind == "model",
        quote_cost=0.0005 if kind == "underquote" else 0.001,
    )
    spec = specification((step("first"), step("never")))
    context = RunContext(adapter, spec.budget)
    error = {"model": AdapterError, "schema": PipelineError, "underquote": BudgetExceeded}[kind]
    with pytest.raises(error):
        await execute(context, spec)
    assert len(adapter.requests) == 1
    assert context.tokens_in == 10
    assert context.cost_usd == pytest.approx(0.001)


async def test_step_timeout_cancels_and_absorbs_reserved_usage() -> None:
    spec = specification((step("slow", budget=Budget(compute_s=0.03)), step("never")))
    adapter = ScriptedAdapter("fake", [], wait=True)
    context = RunContext(adapter, spec.budget)
    with pytest.raises(BudgetExceeded):
        await execute(context, spec)
    assert adapter.cancelled
    assert context.tokens_in == 10
    assert context.cost_usd == pytest.approx(0.001)


async def test_pipeline_cancellation_drains_and_bills_stage() -> None:
    spec = specification((step("slow"), step("never")))
    adapter = ScriptedAdapter("fake", [], wait=True)
    context = RunContext(adapter, spec.budget)
    pending = asyncio.create_task(execute(context, spec))
    try:
        await asyncio.wait_for(adapter.started.wait(), timeout=1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
    assert adapter.cancelled
    assert context.tokens_in == 10
    assert context.cost_usd == pytest.approx(0.001)


@pytest.mark.parametrize("bad", ["unknown", "params", "schema", "model", "input"])
async def test_preflight_rejects_before_first_call(bad: str) -> None:
    tail = step("tail")
    if bad == "unknown":
        tail = step("tail", preset="unknown")
    elif bad == "params":
        tail = step("tail", params={"bogus": 1})
    elif bad == "schema":
        tail = step("tail", output={"$ref": "https://example.invalid/schema"})
    elif bad == "model":
        tail = step("tail", model="other")
    spec = specification((step("first"), tail))
    adapter = ScriptedAdapter("fake", [])
    with pytest.raises(PipelineError):
        await execute(RunContext(adapter, spec.budget), spec, [] if bad == "input" else {})
    assert not adapter.requests


async def test_stage_input_schema_is_checked_before_its_model_call() -> None:
    spec = specification((step("first"), step("tail", input_schema={"type": "array"})))
    adapter = ScriptedAdapter("fake", ['{}'])
    context = RunContext(adapter, spec.budget)
    with pytest.raises(PipelineError):
        await execute(context, spec)
    assert context.tokens_in == 10
    assert len(adapter.requests) == 1


async def test_factory_wrong_model_is_rejected_without_call() -> None:
    spec = specification((step("first", model="other"),))
    adapter = ScriptedAdapter("wrong", [])
    context = RunContext(
        ScriptedAdapter("fake", []), spec.budget, adapter_factory=lambda _: adapter,
    )
    with pytest.raises(PipelineError, match="declared model"):
        await execute(context, spec)
    assert context.tokens_in == 0
    assert not adapter.requests


@pytest.mark.parametrize("stages", [(), (step("alone", preset="verifier"),)])
async def test_no_stage_or_payload_is_explicit_error(stages: tuple[PipelineStep, ...]) -> None:
    spec = specification(stages)
    with pytest.raises(PipelineError):
        await execute(RunContext(ScriptedAdapter("fake", []), spec.budget), spec)


async def test_runtime_emits_one_aggregate_row_not_per_stage(tmp_path: Path) -> None:
    adapters = {
        "vision": ScriptedAdapter("vision", ['{}']),
        "solver": ScriptedAdapter("solver", ['{}']),
    }
    spec = specification((step("first", model="vision"), step("second", model="solver")))
    store = TelemetryStore(tmp_path)
    try:
        runtime = Runtime(store, adapter_factory=adapters.__getitem__)

        async def harness(context: RunContext, task: JsonValue) -> JsonValue:
            return await execute(context, spec, task)

        result = await runtime.evaluate(spec, harness, {}, ScriptedAdapter("fake", []))
        rows = store.rows()
        assert result.tokens_in == 20
        assert len(rows) == 1
        assert rows[0].tokens_in == 20
        assert rows[0].cost_usd == pytest.approx(0.002)
        assert rows[0].accepted
        assert rows[0].model != "fake"
    finally:
        store.close()


async def test_public_preset_path_executes_declared_pipeline() -> None:
    spec = specification((step("first"), step("second")))
    adapter = ScriptedAdapter("fake", ['{"value": 1}', '{"value": 2}'])
    harness = get_preset("extractor").build(spec, {"verifier_depth": 0}, ("extraction",))
    assert await harness(RunContext(adapter, spec.budget), {}) == {"value": 2}
    assert len(adapter.requests) == 2


async def test_intermediate_output_schema_failure_stops_remaining_models() -> None:
    spec = specification((
        step("first", output={"type": "object", "required": ["answer"]}), step("never"),
    ))
    adapter = ScriptedAdapter("fake", ['{}'])
    context = RunContext(adapter, spec.budget)
    with pytest.raises(PipelineError):
        await execute(context, spec)
    assert len(adapter.requests) == 1
    assert context.tokens_in == 10


async def test_actual_usage_reconciliation_releases_unused_reservation() -> None:
    spec = specification((step("first"), step("second")), budget=Budget(cost_usd=0.0025))
    adapter = ScriptedAdapter(
        "fake", ['{}', '{}'], tokens=5, quote_tokens=20, cost=0.0005, quote_cost=0.002,
    )
    context = RunContext(adapter, spec.budget)
    with compilation_wallet(Budget(cost_usd=0.0025)) as wallet:
        assert await execute(context, spec) == {}
        assert wallet.tokens == 10
        assert wallet.cost_usd == pytest.approx(0.001)
    assert context.tokens_in == 10
    assert context.cost_usd == pytest.approx(0.001)
    assert len(adapter.requests) == 2


@pytest.mark.parametrize("scope", ["run", "compile"])
async def test_aggregate_deadline_applies_inside_slow_stage(scope: str) -> None:
    short = Budget(compute_s=0.03)
    spec = specification((step("slow"), step("never")),
                         budget=short if scope == "run" else Budget())
    adapter = ScriptedAdapter("fake", [], wait=True)
    context = RunContext(adapter, spec.budget)
    with compilation_wallet(short if scope == "compile" else Budget()) as wallet:
        with pytest.raises(BudgetExceeded):
            await execute(context, spec)
        assert wallet.tokens == 10
    assert adapter.cancelled
    assert len(adapter.requests) == 1
    assert context.tokens_in == 10


async def test_exhausted_parent_budget_is_not_reset_by_pipeline() -> None:
    spec = specification((step("never"),), budget=Budget(cost_usd=0.001))
    adapter = ScriptedAdapter("fake", ['prior'])
    context = RunContext(adapter, spec.budget)
    await context.call("prior")
    with pytest.raises(BudgetExceeded):
        await execute(context, spec)
    assert len(adapter.requests) == 1
    assert context.tokens_in == 10


async def test_high_token_usage_and_underestimate_are_metrics_not_stop_conditions() -> None:
    legacy = Budget.model_validate({"tokens": 1, "cost_usd": 1, "compute_s": 10})
    spec = specification((step("first", budget=legacy), step("second", budget=legacy)),
                         budget=legacy)
    adapter = ScriptedAdapter("fake", ['before', '{}', '{}'], tokens=100000, quote_tokens=1)
    context = RunContext(adapter, spec.budget)
    with compilation_wallet(legacy) as wallet:
        assert await context.call("earlier high-token work") == "before"
        assert await execute(context, spec) == {}
        assert wallet.tokens == 300000
        assert not wallet.exhausted
    assert context.tokens_in == 300000
    assert context.cost_usd == pytest.approx(0.003)
    assert len(adapter.requests) == 3
    assert all(request.max_output_tokens is None for request in adapter.requests)


# --- Phase 8: images lifted once, attached per stage policy ----------------------------

import base64  # noqa: E402

from image_support import png_bytes  # noqa: E402

from architect.models.images import ImageInput  # noqa: E402

PAGE_BYTES = png_bytes(48, 64, shade=0x40)
KEY_BYTES = png_bytes(48, 64, shade=0xC0)


def image_literal(data: bytes) -> dict[str, JsonValue]:
    return {"$image": {"media_type": "image/png", "base64": base64.b64encode(data).decode()}}


def digitizer(images: JsonValue = None) -> Spec:
    spec = specification((
        step("vision-extract", model="vision"),
        step("solver-rederive", model="solver"),
        step("verifier-diff", preset="verifier", model="diff"),
    ))
    if images is None:
        return spec
    return spec.model_copy(update={"execution_config": {"images": images}})


def digitizer_adapters() -> dict[str, ScriptedAdapter]:
    return {
        "vision": ScriptedAdapter("vision", ['{"answers": ["B"]}']),
        "solver": ScriptedAdapter("solver", ['{"answers": ["B"], "derived": true}']),
        "diff": ScriptedAdapter("diff", ['{"passed": true, "reason": "agrees"}']),
    }


async def run_digitizer(
    spec: Spec, adapters: dict[str, ScriptedAdapter], task: JsonValue
) -> RunContext:
    context = RunContext(
        ScriptedAdapter("fake", []), spec.budget, adapter_factory=adapters.__getitem__
    )
    assert await execute(context, spec, task) == {"answers": ["B"], "derived": True}
    return context


def assert_no_bytes(adapters: dict[str, ScriptedAdapter]) -> None:
    for adapter in adapters.values():
        for request in adapter.requests:
            for data in (PAGE_BYTES, KEY_BYTES):
                assert base64.b64encode(data).decode()[:32] not in request.prompt
            assert "base64" not in request.prompt


async def test_only_the_configured_first_stage_attaches_and_original_carries_stubs() -> None:
    adapters = digitizer_adapters()
    task: JsonValue = {"pages": [image_literal(PAGE_BYTES), image_literal(KEY_BYTES)]}
    context = await run_digitizer(digitizer(), adapters, task)
    page, key = ImageInput.from_bytes(PAGE_BYTES), ImageInput.from_bytes(KEY_BYTES)
    assert [image.sha256 for image in adapters["vision"].requests[0].images] == [
        page.sha256, key.sha256,
    ]
    assert adapters["solver"].requests[0].images == ()
    assert adapters["diff"].requests[0].images == ()
    assert context.images_sent == 2
    stubbed = {"pages": [{"$image": page.stub(1).model_dump(mode="json")},
                         {"$image": key.stub(2).model_dump(mode="json")}]}
    assert json.loads(adapters["vision"].requests[0].prompt)["input"] == stubbed
    later = json.loads(adapters["solver"].requests[0].prompt)
    assert later["input"]["original"] == stubbed
    assert "attached_images" not in later
    assert json.loads(adapters["vision"].requests[0].prompt)["attached_images"] == 2
    assert_no_bytes(adapters)


async def test_images_all_stages_policy_reattaches_every_named_stage() -> None:
    adapters = digitizer_adapters()
    policy = {"stages": ["vision-extract", "solver-rederive", "verifier-diff"]}
    context = await run_digitizer(
        digitizer(policy), adapters, {"pages": [image_literal(PAGE_BYTES)]}
    )
    assert [len(adapters[name].requests[0].images) for name in ("vision", "solver", "diff")] == [
        1, 1, 1,
    ]
    assert context.images_sent == 3
    assert_no_bytes(adapters)


async def test_empty_policy_attaches_nowhere() -> None:
    adapters = digitizer_adapters()
    context = await run_digitizer(
        digitizer({"stages": []}), adapters, {"pages": [image_literal(PAGE_BYTES)]}
    )
    assert context.images_sent == 0
    assert all(not a.requests[0].images for a in adapters.values())


@pytest.mark.parametrize("policy", [{"stages": ["unknown-stage"]}, {"stages": "vision-extract"}])
async def test_invalid_attach_policy_fails_before_any_call(policy: JsonValue) -> None:
    adapters = digitizer_adapters()
    spec = digitizer(policy)
    context = RunContext(
        ScriptedAdapter("fake", []), spec.budget, adapter_factory=adapters.__getitem__
    )
    with pytest.raises(PipelineError):
        await execute(context, spec, {"pages": [image_literal(PAGE_BYTES)]})
    assert all(not adapter.requests for adapter in adapters.values())


async def test_pipeline_image_counts_reach_telemetry(tmp_path: Path) -> None:
    adapters = digitizer_adapters()
    spec = digitizer()
    fake = ScriptedAdapter("fake", [])
    with TelemetryStore(tmp_path / "telemetry") as store:
        runtime = Runtime(store, adapter_factory=adapters.__getitem__)
        harness = get_preset("extractor").build(spec, {"verifier_depth": 0}, ("extraction",))
        await runtime.evaluate(
            spec, harness, {"pages": [image_literal(PAGE_BYTES)]}, fake
        )
        row = store.rows()[0]
    assert row.image_inputs == 1 and row.accepted
