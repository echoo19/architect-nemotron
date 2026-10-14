"""Golden, malformed-output, thinning, and disk-equivalence tests for all presets."""

import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import JsonValue, ValidationError

from architect.backends.pi import PiBackend
from architect.models import FakeAdapter, ModelRequest, ModelResponse
from architect.presets import (
    ParameterRange,
    PresetError,
    UnknownPresetError,
    all_presets,
    execute_preset,
    get_preset,
)
from architect.runtime import BudgetExceeded, RunContext, Runtime, load_harness
from architect.spec import Budget, FloorDefinition, FloorKind, PipelineStep, Spec
from architect.telemetry import TelemetryStore

NAMES = (
    "extractor",
    "synthesizer",
    "recommender",
    "coder",
    "explorer",
    "planner",
    "hypothesis_tester",
    "verifier",
)
HYPOTHESIS = '{"hypothesis":"left opens door","prediction":"door changes","test":"press left"}'
VERDICT = '{"passed": true, "reason": "matches evidence"}'
PROBE = '{"observations": ["grid"], "actions": ["left"]}'
SEARCH = '{"candidates": [{"steps": ["left"]}]}'


class RecordingAdapter(FakeAdapter):
    def __init__(self, texts: list[str]) -> None:
        super().__init__(
            responses=[
                ModelResponse(
                    text=text,
                    model="fake",
                    tokens_in=10,
                    tokens_out=5,
                    cost_usd=0.00001,
                )
                for text in texts
            ]
        )
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return await super().complete(request)


def specification(schema: dict[str, JsonValue] | None = None) -> Spec:
    return Spec(
        task="Use supplied evidence",
        input_schema={"type": "object"},
        output_schema={"type": "string"} if schema is None else schema,
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="synthetic"),
        budget=Budget(),
        tools=("read",),
    )


def minimal(name: str) -> tuple[str, ...]:
    preset = get_preset(name)
    return tuple(c for c in preset.components if c not in preset.removable_components)


def golden(name: str) -> tuple[Spec, list[str], JsonValue]:
    if name == "extractor":
        return (
            specification(
                {
                    "type": "object",
                    "required": ["value"],
                    "properties": {"value": {"type": "integer"}},
                    "additionalProperties": False,
                }
            ),
            ['{"value": 4}', VERDICT],
            {"value": 4},
        )
    if name == "planner":
        return (
            specification({"type": "object"}),
            [SEARCH, '{"steps": ["left"]}'],
            {"steps": ["left"]},
        )
    if name == "verifier":
        return (
            specification({"type": "object"}),
            [VERDICT],
            {
                "passed": True,
                "reason": "matches evidence",
            },
        )
    if name == "explorer":
        return specification(), [PROBE, "A grid interface."], "A grid interface."
    if name == "hypothesis_tester":
        return specification(), [HYPOTHESIS, "A tested hypothesis."], "A tested hypothesis."
    return specification(), ["A useful result."], "A useful result."


def test_exact_launch_set_and_small_grids() -> None:
    assert tuple(p.name for p in all_presets()) == NAMES
    for preset in all_presets():
        assert len(preset.tunable_params) <= 3
        assert preset.validate_params({})
        assert "max_output_tokens" not in preset.tunable_params
        with pytest.raises(PresetError, match="Unknown tunable"):
            preset.validate_params({"max_output_tokens": 64})
        assert set(preset.removable_components) < set(preset.components)
    with pytest.raises(UnknownPresetError):
        get_preset("not_registered")


@pytest.mark.parametrize("name", NAMES)
async def test_golden_runtime(name: str, tmp_path: Path) -> None:
    spec, texts, expected = golden(name)
    adapter = RecordingAdapter(texts)
    preset = get_preset(name)
    store = TelemetryStore(tmp_path / "telemetry")
    result = await Runtime(store).evaluate(
        spec,
        preset.build(spec),
        {"query": "grid", "candidates": ["grid", "other"]},
        adapter,
        preset=name,
        components=preset.components,
    )
    assert result.output == expected
    assert adapter.calls == len(texts)
    assert result.tokens_in == 10 * len(texts)
    assert all(request.max_output_tokens is None for request in adapter.requests)


@pytest.mark.parametrize("name", NAMES)
async def test_every_preset_rejects_invalid_output(name: str) -> None:
    spec = specification({"type": "object", "required": ["must_exist"]})
    adapter = RecordingAdapter(["{}"])
    with pytest.raises(PresetError):
        await get_preset(name).build(spec, components=minimal(name))(
            RunContext(adapter, spec.budget),
            {},
        )
    assert adapter.calls == 1


def test_emitted_docstring_is_unchanged_and_states_no_design(tmp_path: Path) -> None:
    """harness.py's bytes are part of the artifact digest, so the template's docstring stays
    byte for byte; the agent's design reader treats it as no stated design."""
    from architect.agent.tools import harness_design

    spec, _texts, _expected = golden("extractor")
    get_preset("extractor").emit(tmp_path, spec)
    source = (tmp_path / "harness.py").read_text(encoding="utf-8")
    assert source.startswith(
        '"""Generated candidate harness. Execution does not confer a license."""\n'
    )
    assert harness_design(tmp_path) is None
    (tmp_path / "harness.py").write_text('"""Reads the total line first."""\n', encoding="utf-8")
    assert harness_design(tmp_path) == "Reads the total line first."


@pytest.mark.parametrize("name", NAMES)
async def test_emission_matches_build_and_ignores_mutable_sidecar(
    name: str, tmp_path: Path
) -> None:
    spec, texts, expected = golden(name)
    preset = get_preset(name)
    preset.emit(tmp_path, spec)
    metadata = json.loads((tmp_path / "preset.json").read_text())
    assert metadata["name"] == name
    assert "max_output_tokens" not in metadata["params"]
    (tmp_path / "preset.json").write_text('{"params": "malicious"}')
    if name == "coder":
        pack = json.loads((tmp_path / "pi-pack.json").read_text())
        assert pack["backend"] == "pi"
        assert pack["tools"] == ["read"]
        assert "max_output_tokens" not in pack
        (tmp_path / "pi-pack.json").write_text(
            '{"tools": ["bash"], "prompt_fragment": "UNBOUND_SIDECAR_MARKER"}'
        )
    before, after = RecordingAdapter(texts), RecordingAdapter(texts)
    assert (
        await preset.build(spec)(RunContext(before, spec.budget, repository=tmp_path), {})
        == expected
    )
    assert (
        await load_harness(tmp_path)(RunContext(after, spec.budget, repository=tmp_path), {})
        == expected
    )
    if name == "coder":
        # The SDK records real wall-clock message timestamps; policy/content must match.
        def stable_pi_context(request: ModelRequest) -> dict[str, JsonValue]:
            payload = json.loads(request.prompt)
            for message in payload["pi_context"]["messages"]:
                message.pop("timestamp", None)
            return cast(dict[str, JsonValue], payload)

        assert [stable_pi_context(request) for request in before.requests] == [
            stable_pi_context(request) for request in after.requests
        ]
    else:
        assert before.requests == after.requests
    assert all(request.max_output_tokens is None for request in after.requests)
    assert all("UNBOUND_SIDECAR_MARKER" not in request.prompt for request in after.requests)


@pytest.mark.parametrize("name", NAMES)
async def test_all_presets_honor_string_schema(name: str) -> None:
    spec = specification()
    texts = [HYPOTHESIS, '"raw prose"'] if name == "hypothesis_tester" else ['"raw prose"']
    adapter = RecordingAdapter(texts)
    result = await get_preset(name).build(spec, components=minimal(name))(
        RunContext(adapter, spec.budget),
        {},
    )
    assert result == '"raw prose"'


@pytest.mark.parametrize(
    "schema,response,expected",
    [
        ({"type": "integer"}, "42", 42),
        ({"type": "number"}, "0.5", 0.5),
        ({"type": "boolean"}, "true", True),
        ({"type": "null"}, "null", None),
        ({"type": "array", "items": {"type": "integer"}}, "[1, 2]", [1, 2]),
        ({"type": "string", "minLength": 1}, "  raw text  ", "  raw text  "),
    ],
)
async def test_schema_types(
    schema: dict[str, JsonValue],
    response: str,
    expected: JsonValue,
) -> None:
    spec = specification(schema)
    result = await get_preset("extractor").build(spec, {"verifier_depth": 0})(
        RunContext(RecordingAdapter([response]), spec.budget),
        {},
    )
    assert result == expected


@pytest.mark.parametrize("response", ["not json", "NaN", "Infinity", "1e999", '"42"', "true"])
async def test_invalid_numbers_are_not_accepted(response: str) -> None:
    spec = specification({"type": "number"})
    with pytest.raises(PresetError):
        await get_preset("extractor").build(spec, {"verifier_depth": 0})(
            RunContext(RecordingAdapter([response]), spec.budget),
            {},
        )


@pytest.mark.parametrize("value", [-1, 2, True, "1", None, [], {}])
def test_invalid_params(value: JsonValue) -> None:
    with pytest.raises(PresetError):
        get_preset("extractor").validate_params({"verifier_depth": value})


def test_range_validation_and_registry_isolation() -> None:
    for values, default in [((), 0), ((1, 1), 1), ((0, 1), 2)]:
        with pytest.raises(ValidationError):
            ParameterRange(values=values, default=default)
    with pytest.raises(PresetError):
        get_preset("extractor").validate_params({"unknown": 1})
    one = get_preset("extractor")
    one.tunable_params.clear()
    assert get_preset("extractor").tunable_params


@pytest.mark.parametrize("name", ["explorer", "planner", "hypothesis_tester"])
def test_game_presets_share_the_common_retries_grid(name: str) -> None:
    # These presets also compile non-game tasks, so their grid is not reordered
    # for games: the game backend itself guarantees one decision repair even at
    # retries=0, and the first tuned candidate stays identical to every other preset.
    from architect.compiler.tuning import GridSearch

    preset = get_preset(name)
    assert preset.tunable_params["retries"] == ParameterRange(values=(0, 1), default=0)
    assert preset.validate_params({})["retries"] == 0
    grid = GridSearch().configurations(preset)
    assert grid[0]["retries"] == 0
    assert sorted({cast(int, params["retries"]) for params in grid}) == [0, 1]


@pytest.mark.parametrize("components", [(), ("fake",), ("extraction", "extraction")])
def test_reject_invalid_component_sets(components: tuple[str, ...]) -> None:
    with pytest.raises(PresetError):
        get_preset("extractor").build(specification(), components=components)


@pytest.mark.parametrize(
    "name,component",
    [
        ("extractor", "self_check"),
        ("synthesizer", "review"),
        ("recommender", "review"),
        ("explorer", "analysis_check"),
        ("planner", "review"),
        ("verifier", "evidence_check"),
    ],
)
async def test_verifier_depth_and_component_removal(name: str, component: str) -> None:
    spec = specification()
    preset = get_preset(name)
    enabled = (*minimal(name), component)
    for params, components, responses in [
        ({"verifier_depth": 0}, enabled, ["result"]),
        ({"verifier_depth": 1}, minimal(name), ["result"]),
        ({"verifier_depth": 1}, enabled, ["result", VERDICT]),
    ]:
        adapter = RecordingAdapter(responses)
        assert (
            await preset.build(spec, params, components)(
                RunContext(adapter, spec.budget),
                {},
            )
            == "result"
        )
        assert adapter.calls == len(responses)


@pytest.mark.parametrize("verdict", ['{"passed":false,"reason":"wrong"}', "ok", "{}"])
async def test_self_check_verdict_is_not_ignored(verdict: str) -> None:
    spec = specification()
    with pytest.raises(PresetError):
        await get_preset("extractor").build(spec)(
            RunContext(RecordingAdapter(["result", verdict]), spec.budget),
            {},
        )


async def test_retry_repairs_schema_and_failed_self_check() -> None:
    spec = specification({"type": "object", "required": ["value"]})
    adapter = RecordingAdapter(["{}", '{"value": 1}', VERDICT])
    result = await get_preset("extractor").build(spec, {"retries": 1})(
        RunContext(adapter, spec.budget),
        {},
    )
    assert result == {"value": 1}
    assert "Repair" in adapter.requests[1].prompt
    adapter = RecordingAdapter(
        ['{"value": 1}', '{"passed":false,"reason":"wrong"}', '{"value": 2}', VERDICT]
    )
    result = await get_preset("extractor").build(spec, {"retries": 1})(
        RunContext(adapter, spec.budget),
        {},
    )
    assert result == {"value": 2}


async def test_budget_guard_stops_optional_check() -> None:
    spec = specification().model_copy(update={"budget": Budget(cost_usd=0.000015)})
    adapter = RecordingAdapter(["result", VERDICT])
    with pytest.raises(BudgetExceeded):
        await get_preset("extractor").build(spec)(RunContext(adapter, spec.budget), {})
    assert adapter.calls == 1


async def test_retrieval_ranking_and_preference_memory_are_real() -> None:
    spec = specification()
    task: JsonValue = {
        "query": "orange",
        "candidates": ["apple", "orange"],
        "preferences": {"taste": "sweet"},
        "history": ["pear"],
    }
    preset = get_preset("recommender")
    full, thin = RecordingAdapter(["orange"]), RecordingAdapter(["unknown"])
    await preset.build(spec)(RunContext(full, spec.budget), task)
    await preset.build(spec, components=minimal("recommender"))(
        RunContext(thin, spec.budget),
        task,
    )
    full_payload = json.loads(full.requests[0].prompt)["input"]
    thin_payload = json.loads(thin.requests[0].prompt)["input"]
    assert full_payload["retrieved"] == ["orange", "apple"]
    assert full_payload["preference_memory"]["preferences"] == {"taste": "sweet"}
    assert "retrieved" not in thin_payload
    assert "preference_memory" not in thin_payload
    assert "sweet" not in thin.requests[0].prompt


@pytest.mark.parametrize(
    "name,component,response,key",
    [
        ("explorer", "probe", PROBE, "probe_analysis"),
        ("planner", "search", SEARCH, "search_candidates"),
    ],
)
async def test_intermediate_calls_feed_final_output(
    name: str,
    component: str,
    response: str,
    key: str,
) -> None:
    spec = specification()
    full = RecordingAdapter([response, "final"])
    thin = RecordingAdapter(["final"])
    await get_preset(name).build(spec)(RunContext(full, spec.budget), {})
    await get_preset(name).build(spec, components=minimal(name))(
        RunContext(thin, spec.budget),
        {},
    )
    assert key in json.loads(full.requests[1].prompt)
    assert key not in json.loads(thin.requests[0].prompt)
    assert component in get_preset(name).removable_components
    bad = RecordingAdapter(["not json"])
    with pytest.raises(PresetError):
        await get_preset(name).build(spec)(RunContext(bad, spec.budget), {})
    assert bad.calls == 1


async def test_context_window_and_terseness_change_requests_without_output_caps() -> None:
    spec = specification()
    task: JsonValue = {"source": "a" * 3000}
    small, large, unbounded = [RecordingAdapter(["ok"]) for _ in range(3)]
    preset = get_preset("synthesizer")
    await preset.build(spec, {"context_window": 512, "terseness": 1})(
        RunContext(small, spec.budget),
        task,
    )
    await preset.build(spec, {"context_window": 2048})(RunContext(large, spec.budget), task)
    await preset.build(spec, components=minimal("synthesizer"))(
        RunContext(unbounded, spec.budget),
        task,
    )
    assert len(small.requests[0].prompt) < len(large.requests[0].prompt)
    assert len(large.requests[0].prompt) < len(unbounded.requests[0].prompt)
    assert "Be concise" in small.requests[0].prompt
    assert all(
        request.max_output_tokens is None
        for adapter in (small, large, unbounded)
        for request in adapter.requests
    )


async def test_coder_pack_components_bind_actual_pi_execution(tmp_path: Path) -> None:
    spec = specification().model_copy(update={"tools": ("write",)})
    preset = get_preset("coder")
    preset.emit(tmp_path, spec, components=minimal("coder"))
    pack = json.loads((tmp_path / "pi-pack.json").read_text())
    assert pack["context_policy"] == {}
    assert pack["subagent_rules"] == []
    assert "max_output_tokens" not in pack
    adapter = RecordingAdapter(
        [
            json.dumps(
                {
                    "tool_calls": [
                        {
                            "id": "write1",
                            "name": "write",
                            "arguments": {"path": "result.txt", "content": "actual Pi write"},
                        }
                    ]
                }
            ),
            "implemented",
        ]
    )
    backend = PiBackend()
    context = RunContext(adapter, spec.budget, repository=tmp_path, pi_backend=backend)
    assert await load_harness(tmp_path)(context, {"request": "write result.txt"}) == "implemented"
    assert (tmp_path / "result.txt").read_text() == "actual Pi write"
    evidence = backend.last_evidence
    assert evidence is not None and evidence.extension_loaded and evidence.exit_code == 0
    assert evidence.model_calls == 2 and evidence.tool_calls == 1
    assert pack["prompt_fragment"] in adapter.requests[0].prompt
    assert all(request.max_output_tokens is None for request in adapter.requests)


@pytest.mark.parametrize("limit", [512, 2048, None])
async def test_coder_context_policy_and_prompt_are_real(tmp_path: Path, limit: int | None) -> None:
    spec = specification()
    (tmp_path / "source.txt").write_text("CONTEXT_MARKER" * 250)
    preset = get_preset("coder")
    components = preset.components if limit is not None else minimal("coder")
    params: dict[str, JsonValue] = {"terseness": 1, "context_window": limit or 2048}
    adapter = RecordingAdapter(
        [
            json.dumps(
                {
                    "tool_calls": [
                        {
                            "id": "read1",
                            "name": "read",
                            "arguments": {"path": "source.txt"},
                        }
                    ]
                }
            ),
            "read",
        ]
    )
    await preset.build(spec, params, components)(
        RunContext(adapter, spec.budget, repository=tmp_path), {}
    )
    request = json.loads(adapter.requests[1].prompt)["pi_context"]
    assert "Make the requested change. Be concise." in adapter.requests[0].prompt
    assert ("Request child agents only through" in adapter.requests[0].prompt) == (
        limit is not None
    )
    tool_results = [message for message in request["messages"] if message["role"] == "toolResult"]
    assert len(tool_results) == 1
    content = tool_results[0]["content"][0]["text"]
    assert len(content) == (limit if limit is not None else 3500)


@pytest.mark.parametrize(
    "name,response",
    [
        ("planner", '{"steps": []}'),
        ("planner", json.dumps({"steps": ["left"] * 17})),
        ("verifier", '{"licensed": true}'),
        ("verifier", '{"passed": "yes", "reason": "ok"}'),
    ],
)
async def test_structured_plan_and_verdict_limits(name: str, response: str) -> None:
    spec = specification({"type": "object"})
    with pytest.raises(PresetError):
        await get_preset(name).build(spec, components=minimal(name))(
            RunContext(RecordingAdapter([response]), spec.budget),
            {},
        )


async def test_pipeline_dispatch_runs_declared_stages_and_implicit_verdict() -> None:
    spec = specification().model_copy(
        update={
            "pipeline": (
                PipelineStep(
                    name="draft",
                    preset="synthesizer",
                    model="fake",
                    budget=Budget(),
                    output_schema={"type": "string"},
                ),
                PipelineStep(name="check", preset="verifier", model="fake", budget=Budget()),
            )
        }
    )
    adapter = RecordingAdapter(["draft output", VERDICT])
    context = RunContext(adapter, spec.budget)
    output = await get_preset("synthesizer").build(spec)(context, {"source": "evidence"})
    assert output == "draft output"
    assert adapter.calls == 2
    assert json.loads(adapter.requests[1].prompt)["input"] == {
        "original": {"source": "evidence"},
        "previous": "draft output",
        "steps": {"draft": "draft output"},
    }
    assert context.tokens_in == 20 and context.tokens_out == 10
    assert all(request.max_output_tokens is None for request in adapter.requests)


def test_invalid_schemas_fail_before_call() -> None:
    for schema in [{"type": "bogus"}, {"$ref": "https://example.invalid/schema"}]:
        with pytest.raises(PresetError):
            get_preset("extractor").build(specification(schema))


@pytest.mark.parametrize("name", ["planner", "verifier"])
@pytest.mark.parametrize(
    "schema",
    [
        {
            "type": "object",
            "properties": {"custom": {"type": "integer"}},
            "required": ["custom"],
            "additionalProperties": False,
        },
        {"type": "object", "required": ["custom"]},
        {"type": "object", "additionalProperties": {"type": "integer"}},
        {"type": "object", "minProperties": 1, "maxProperties": 1},
    ],
)
async def test_explicit_structured_schema_overrides_default_shape(
    name: str,
    schema: dict[str, JsonValue],
) -> None:
    spec = specification(schema)
    adapter = RecordingAdapter(['{"custom": 42}'])
    assert await get_preset(name).build(spec, components=minimal(name))(
        RunContext(adapter, spec.budget), {}
    ) == {"custom": 42}
    prompt = json.loads(adapter.requests[0].prompt)
    assert "plan_schema" not in prompt and "verdict_schema" not in prompt


@pytest.mark.parametrize("name", [name for name in NAMES if name != "coder"])
async def test_pipeline_envelope_keeps_all_evidence(name: str) -> None:
    spec = specification()
    task: JsonValue = {
        "original": {"source": "a" * 4000},
        "previous": "b" * 4000,
        "steps": {"first": "c" * 4000},
    }
    adapter = RecordingAdapter(
        [HYPOTHESIS, "result"] if name == "hypothesis_tester" else ["result"]
    )
    await get_preset(name).build(spec, components=minimal(name))(
        RunContext(adapter, spec.budget), task
    )
    assert json.loads(adapter.requests[0].prompt)["input"] == task


async def test_long_output_is_not_clipped_to_manufacture_efficiency() -> None:
    spec = specification()
    text = "complete evidence " * 1000
    adapter = FakeAdapter(
        responses=[
            ModelResponse(
                text=text,
                model="fake",
                tokens_in=2_000_000,
                tokens_out=1_000_000,
                cost_usd=0,
            )
        ]
    )
    context = RunContext(adapter, spec.budget)
    assert await get_preset("synthesizer").build(spec)(context, {}) == text
    assert context.tokens_in == 2_000_000 and context.tokens_out == 1_000_000


async def test_inputs_and_direct_execution_are_checked() -> None:
    spec = specification()
    adapter = RecordingAdapter(["ok"])
    with pytest.raises(PresetError):
        await get_preset("synthesizer").build(spec)(RunContext(adapter, spec.budget), [])
    with pytest.raises(PresetError):
        await execute_preset(
            "synthesizer",
            {},
            (),
            spec.model_dump(mode="json"),
            RunContext(adapter, spec.budget),
            {},
        )
    assert adapter.calls == 0


async def test_build_captures_spec_and_params_snapshots() -> None:
    spec = specification()
    params: dict[str, JsonValue] = {"verifier_depth": 0}
    harness = get_preset("extractor").build(spec, params)
    params["verifier_depth"] = 1
    spec.output_schema["type"] = "number"
    adapter = RecordingAdapter(["original string"])
    assert await harness(RunContext(adapter, spec.budget), {}) == "original string"
    assert adapter.calls == 1


# --- Phase 8: image literals, stubs and attachment -------------------------------------

import base64  # noqa: E402
import random  # noqa: E402

from architect.models.images import ImageInput, ImageStub, encode_png_gray  # noqa: E402
from architect.presets._execution import _bounded, execute  # noqa: E402
from architect.presets.images import attach_stages, lift_images  # noqa: E402

PAGE_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "required": ["images"],
    "properties": {
        "images": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["$image"],
                "properties": {"$image": {"type": "object", "required": ["media_type"]}},
            },
        }
    },
}
ATTACHED = " 1 image(s) are attached to this message; extract from them, not from the stub."


def page_png(seed: int = 7, width: int = 64, height: int = 48) -> bytes:
    rng = random.Random(seed)
    return encode_png_gray([rng.randbytes(width) for _ in range(height)], width, height)


def literal(data: bytes, media_type: str = "image/png") -> dict[str, JsonValue]:
    encoded = base64.b64encode(data).decode("ascii")
    return {"$image": {"media_type": media_type, "base64": encoded}}


def page_spec() -> Spec:
    return Spec(
        task="Extract the page number",
        input_schema=PAGE_SCHEMA,
        output_schema={
            "type": "object",
            "required": ["page"],
            "properties": {"page": {"type": "integer"}},
        },
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="pages"),
        budget=Budget(),
    )


def test_lift_images_stubs_literals_in_walk_order_and_keeps_key_order() -> None:
    first, second = page_png(1), page_png(2)
    task: dict[str, JsonValue] = {
        "z": [literal(second)],
        "a": {"inner": literal(first)},
        "text": "kept",
    }
    lifted, refs = lift_images(task)
    assert [type(ref) for ref in refs] == [ImageInput, ImageInput]
    assert [ref.sha256 for ref in refs] == [
        ImageInput.from_bytes(first).sha256,
        ImageInput.from_bytes(second).sha256,
    ]
    assert isinstance(lifted, dict) and list(lifted) == ["z", "a", "text"]
    assert lifted["a"] == {"inner": {"$image": refs[0].stub(1).model_dump(mode="json")}}
    assert lifted["z"] == [{"$image": refs[1].stub(2).model_dump(mode="json")}]
    assert lifted["text"] == "kept"
    dumped = json.dumps(lifted)
    for data in (first, second):
        assert base64.b64encode(data).decode("ascii")[:40] not in dumped
    # The caller's task is not mutated.
    assert "base64" in json.dumps(task)


def test_lift_images_reads_existing_stubs_idempotently() -> None:
    lifted, refs = lift_images({"pages": [literal(page_png(1)), literal(page_png(2))]})
    again, stubs = lift_images(lifted)
    assert again == lifted
    assert stubs == tuple(ref.stub(index) for index, ref in enumerate(refs, start=1))
    assert all(type(stub) is ImageStub for stub in stubs)
    assert isinstance(lifted, dict) and isinstance(lifted["pages"], list)
    echoed = {"original": lifted, "previous": {"echo": lifted["pages"][0]}}
    assert lift_images(echoed)[1] == stubs  # a repeated identical stub is listed once


def test_lift_images_leaves_text_tasks_unchanged() -> None:
    task: JsonValue = {"text": "hello", "items": [1, {"b": None, "a": True}]}
    assert lift_images(task) == (task, ())
    assert lift_images("plain") == ("plain", ())


@pytest.mark.parametrize(
    "value",
    [
        {"$image": {"media_type": "image/png"}},
        {"$image": {"media_type": "image/png", "base64": "!!!not-base64"}},
        {"$image": {"media_type": "image/jpeg", "base64": base64.b64encode(page_png()).decode()}},
        {"$image": {"media_type": "image/png", "base64": base64.b64encode(b"text").decode()}},
        {"$image": {"media_type": "image/png", "base64": 7}},
        {"$image": "data:image/png;base64,AAAA"},
        {"$image": {"media_type": "image/png", "base64": "", "extra": 1}},
        {**literal(page_png()), "caption": "sibling keys smuggle bytes"},
        {"$image": {"media_type": "image/png", "sha256": "0" * 63, "width": 1, "height": 1,
                    "attached": 1}},
    ],
)
def test_lift_images_fails_closed_on_malformed_references(value: dict[str, JsonValue]) -> None:
    with pytest.raises(PresetError, match="^Invalid image reference in task$"):
        lift_images({"nested": [value]})


def test_lift_images_caps_the_image_count() -> None:
    stub = ImageInput.from_bytes(page_png()).stub
    task: JsonValue = [{"$image": stub(index).model_dump(mode="json")} for index in range(1, 102)]
    with pytest.raises(PresetError, match="too many images"):
        lift_images(task)


async def test_extractor_prompt_carries_stubs_and_attaches_on_every_call() -> None:
    data = page_png(width=160, height=120)
    encoded = base64.b64encode(data).decode("ascii")
    adapter = RecordingAdapter(['{"page": 4}', VERDICT])
    context = RunContext(adapter, Budget())
    spec = page_spec()
    output = await execute_preset(
        "extractor",
        {"verifier_depth": 1},
        ("extraction", "self_check"),
        spec.model_dump(mode="json"),
        context,
        {"images": [literal(data)]},
    )
    assert output == {"page": 4}
    assert len(adapter.requests) == 2
    sha = ImageInput.from_bytes(data).sha256
    for request in adapter.requests:
        assert [image.sha256 for image in request.images] == [sha]
        assert encoded[:64] not in request.prompt and "base64" not in request.prompt
        assert sha in request.prompt  # the content-free stub is what the text carries
    extraction = json.loads(adapter.requests[0].prompt)
    assert extraction["attached_images"] == 1
    assert extraction["instruction"].endswith(ATTACHED)
    assert extraction["input"]["images"][0]["$image"]["attached"] == 1
    assert json.loads(adapter.requests[1].prompt)["operation"] == "verify_candidate"
    assert context.images_sent == 2
    assert [shape.image_count for shape in adapter.seen] == [1, 1]


async def test_retries_and_intermediate_calls_attach_the_same_images() -> None:
    data = page_png()
    adapter = RecordingAdapter(["not json", '{"page": 1}', VERDICT])
    context = RunContext(adapter, Budget())
    await execute_preset(
        "extractor", {"verifier_depth": 1, "retries": 1}, ("extraction", "self_check"),
        page_spec().model_dump(mode="json"), context, {"images": [literal(data)]},
    )
    assert [len(request.images) for request in adapter.requests] == [1, 1, 1]
    planner = RecordingAdapter([SEARCH, '{"steps": ["read"]}'])
    plan_spec = page_spec().model_copy(update={"output_schema": {"type": "object"}})
    await execute_preset(
        "planner", {"verifier_depth": 0}, ("plan", "search"),
        plan_spec.model_dump(mode="json"), RunContext(planner, Budget()),
        {"images": [literal(data)]},
    )
    assert [len(request.images) for request in planner.requests] == [1, 1]


async def test_attach_images_false_keeps_stubs_but_sends_nothing() -> None:
    data = page_png()
    adapter = RecordingAdapter(['{"page": 2}'])
    output = await execute(
        "extractor", {"verifier_depth": 0, "retries": 0}, ("extraction",), page_spec(),
        RunContext(adapter, Budget()), {"images": [literal(data)]}, attach_images=False,
    )
    assert output == {"page": 2}
    request = adapter.requests[0]
    assert request.images == ()
    prompt = json.loads(request.prompt)
    assert "attached_images" not in prompt
    assert not prompt["instruction"].endswith(ATTACHED)
    assert prompt["input"]["images"][0]["$image"]["sha256"] == ImageInput.from_bytes(data).sha256


async def test_context_policy_bounds_the_stub_not_the_bytes() -> None:
    # A page whose base64 alone is far over the 2048-character context allowance.
    data = page_png(width=200, height=100)
    assert len(base64.b64encode(data)) > 10_000
    adapter = RecordingAdapter(['{"page": 3}'])
    await execute_preset(
        "extractor", {"verifier_depth": 0}, ("extraction",),
        page_spec().model_dump(mode="json"), RunContext(adapter, Budget()),
        {"images": [literal(data)]},
    )
    stub = json.loads(adapter.requests[0].prompt)["input"]["images"][0]["$image"]
    assert ImageStub.model_validate(stub).sha256 == ImageInput.from_bytes(data).sha256
    # Text-only payloads are bounded exactly as before.
    text_adapter = RecordingAdapter(['{"page": 3}'])
    long_task: JsonValue = {"images": [], "notes": "x" * 5000}
    await execute_preset(
        "extractor", {"verifier_depth": 0}, ("extraction",),
        page_spec().model_dump(mode="json"), RunContext(text_adapter, Budget()), long_task,
    )
    text_prompt = json.loads(text_adapter.requests[0].prompt)
    assert text_prompt["input"] == _bounded(long_task, 2048)
    assert "attached_images" not in text_prompt
    assert text_adapter.requests[0].images == ()


def test_attach_stages_policy() -> None:
    assert attach_stages(page_spec()) == frozenset()
    steps = (
        PipelineStep(name="vision-extract", preset="extractor", model="fake", budget=Budget()),
        PipelineStep(name="solve", preset="extractor", model="fake", budget=Budget()),
    )
    pipeline = page_spec().model_copy(update={"pipeline": steps})
    assert attach_stages(pipeline) == frozenset({"vision-extract"})

    def configured(images: JsonValue) -> Spec:
        return pipeline.model_copy(update={"execution_config": {"images": images}})

    assert attach_stages(configured({"stages": ["solve", "vision-extract"]})) == frozenset(
        {"solve", "vision-extract"}
    )
    assert attach_stages(configured({"stages": []})) == frozenset()
    assert attach_stages(configured({})) == frozenset({"vision-extract"})
    for invalid in ({"stages": ["missing"]}, {"stages": "solve"}, {"stages": [1]}, [],
                    {"stages": [], "detail": "high"}):
        with pytest.raises(PresetError):
            attach_stages(configured(invalid))


async def test_stage_attach_binding_withholds_images_only_inside_its_scope() -> None:
    from architect.presets.images import stage_attach, stage_attaches

    data = page_png()
    held = RecordingAdapter(['{"page": 1}'])
    with stage_attach(False):
        assert not stage_attaches()
        await execute(
            "extractor", {"verifier_depth": 0, "retries": 0}, ("extraction",), page_spec(),
            RunContext(held, Budget()), {"images": [literal(data)]},
        )
    assert stage_attaches()
    assert held.requests[0].images == ()
    sent = RecordingAdapter(['{"page": 1}'])
    with stage_attach(True):
        await execute(
            "extractor", {"verifier_depth": 0, "retries": 0}, ("extraction",), page_spec(),
            RunContext(sent, Budget()), {"images": [literal(data)]},
        )
    assert len(sent.requests[0].images) == 1
