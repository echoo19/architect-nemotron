"""Phase 14 fair arms (WB).

The input limit removes confound 1 and reply parsing equal to Forge's ``extract_json``
removes confound 2 (report-evidence.md Section 3). The plain arm is pinned to one call,
and aliases with an output cap plus the model id rule let ``nvidia/...`` ids run in the
bench (docs/interfaces/phase14-nebius-evidence.md, Sections 9 and 15.2). Everything is
offline: fake adapters, scripted HTTP transports and the fake bench.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import JsonValue, TypeAdapter, ValidationError

from architect.bench.providers import check_models, provider_for, resolve_models
from architect.bench.rows import BenchError, ModelId, read_rows
from architect.bench.runner import BenchConfig, run_bench
from architect.compiler import tuning
from architect.compiler.selection import Selection, select_preset
from architect.compiler.tuning import SuccessiveHalving, tune
from architect.floors import EvaluationCase, HeldOutTasks
from architect.models import (
    AdapterError,
    FakeAdapter,
    ModelAdapter,
    ModelRequest,
    ModelResponse,
    OpenAICompatibleAdapter,
)
from architect.models.chat import ChatMessage, TextDeltaEvent
from architect.models.pricing import load_prices, price_for
from architect.presets import PresetError, get_preset
from architect.presets._execution import pack_description
from architect.presets._json import LAST_STARTS, extract_last_json_text, parse_reply
from architect.profiles import (
    AdapterConfig,
    NamedAdapterConfig,
    ProfileError,
    _adapter,
    load_profile,
    model_unpriced,
)
from architect.runtime import BudgetExceeded, RunContext, Runtime
from architect.runtime._sandbox_builtins import BUILTIN_FUNCTIONS, PARSE_LIMIT, Meter
from architect.spec import (
    PLAIN_PRESETS,
    Budget,
    FloorDefinition,
    FloorKind,
    HarnessArtifact,
    ReplyJson,
    Spec,
)
from architect.telemetry import TelemetryStore

ULTRA = "nvidia/Nemotron-3-Ultra-550b-a55b"
LIGHTNING = "nvidia/Nemotron-3_5-Lightning"
URL = "https://api.tokenfactory.nebius.com/v1"
HI = (ChatMessage("user", "x"),)
RECEIPT_IN: dict[str, JsonValue] = {
    "type": "object",
    "properties": {"receipt": {"type": "string"}},
    "required": ["receipt"],
    "additionalProperties": False,
}
RECEIPT_OUT: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "total": {"type": ["integer", "null"]},
        "subtotal": {"type": ["integer", "null"]},
        "tax": {"type": ["integer", "null"]},
        "item_count": {"type": "integer"},
    },
    "required": ["total", "subtotal", "tax", "item_count"],
    "additionalProperties": False,
}
ANSWER: dict[str, JsonValue] = {"total": 580965, "subtotal": 503000, "tax": 52815, "item_count": 6}
VERDICT: dict[str, JsonValue] = {
    "type": "object",
    "required": ["passed", "reason"],
    "properties": {"passed": {"type": "boolean"}, "reason": {"type": "string"}},
    "additionalProperties": False,
}


def receipt_spec(**fields: Any) -> Spec:
    """The receipts cell's shape (contract 10.4): extractor hint, the real schemas."""
    return Spec.model_validate({
        "task": "Return the four amounts printed on the receipt.",
        "input_schema": RECEIPT_IN,
        "output_schema": RECEIPT_OUT,
        "floor": {"kind": "held_out_tasks", "limit": 0.5, "dataset": "receipts-fixture"},
        "budget": {"cost_usd": 1.0, "compute_s": 60},
        "preset_hint": "extractor",
        **fields,
    })


def canonical(value: object) -> str:
    """Distinguishes 1 from 1.0 and key order from content, unlike ``==``."""
    return json.dumps(value, sort_keys=True)


class Recording(FakeAdapter):
    """Scripted replies; every request is kept whole."""

    def __init__(self, *texts: str, model: str = "fake") -> None:
        super().__init__(
            model=model,
            responses=[
                ModelResponse(text=text, model=model, tokens_in=10, tokens_out=5, cost_usd=1e-5)
                for text in texts
            ],
        )
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return await super().complete(request)


async def run(
    spec: Spec,
    adapter: FakeAdapter,
    task: JsonValue,
    *,
    name: str = "extractor",
    params: dict[str, JsonValue] | None = None,
    plain: bool = False,
) -> JsonValue:
    """Run one preset execution, in the plain arm's configuration when ``plain``."""
    preset = get_preset(name)
    components: tuple[str, ...] | None = None
    if plain:
        params, chosen = tuning._plain(preset)
        components = tuning._effective_components(preset, params, chosen)
    return await preset.build(spec, params, components)(RunContext(adapter, spec.budget), task)


def prompt_input(request: ModelRequest) -> JsonValue:
    prompt = TypeAdapter(dict[str, JsonValue]).validate_json(request.prompt)
    return prompt["input"]


def runtime(root: Path) -> Runtime:
    return Runtime(TelemetryStore(root / "telemetry"))


def receipt_floor(spec: Spec, count: int = 8) -> HeldOutTasks:
    return HeldOutTasks(spec.floor, [
        EvaluationCase(
            case_id=f"r{index:03}",
            task={"receipt": f"TOTAL {index + 1},000\nITEM 1"},
            expected={"total": (index + 1) * 1000, "subtotal": None, "tax": None, "item_count": 1},
        )
        for index in range(count)
    ])


def read_receipt(prompt: str) -> str:
    """The answer a perfect reader gives to a ``receipt_floor`` prompt."""
    data = json.loads(prompt)
    total = int(data["input"]["receipt"].split()[1].replace(",", ""))
    return json.dumps({"total": total, "subtotal": None, "tax": None, "item_count": 1})


class Reader(Recording):
    """Answers every ``receipt_floor`` prompt correctly, once."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await super().complete(request)
        return response.model_copy(update={"text": read_receipt(request.prompt)})


# -- 9.1 input limit (confound 1) -------------------------------------------------------


@pytest.mark.parametrize(
    ("size", "limit", "whole"),
    [(5_000, None, False), (5_000, 8192, True), (10_000, 8192, False), (10_000, 16_384, True)],
)
async def test_a_long_input_reaches_the_plain_prompt_whole_up_to_the_spec_limit(
    size: int, limit: int | None, whole: bool
) -> None:
    text = ("SUBTOTAL 503,000 TAX 52,815 SVC 25,150 " * 300)[:size]
    fields: dict[str, Any] = {"plain": True}
    if limit is not None:
        fields["input_limit_chars"] = limit
    adapter = Recording(json.dumps(ANSWER))
    assert await run(receipt_spec(**fields), adapter, {"receipt": text}, plain=True) == ANSWER
    assert len(adapter.requests) == 1  # the plain arm: one call
    seen = prompt_input(adapter.requests[0])
    assert isinstance(seen, dict) and isinstance(seen["receipt"], str)
    if whole:
        assert seen == {"receipt": text}
        return
    allowance = 2048 if limit is None else limit
    kept = seen["receipt"]
    # Clipped exactly at the allowance in force: a prefix whose JSON fits it, no shorter.
    assert text.startswith(kept) and len(kept) < size
    assert allowance - 32 < len(json.dumps(seen, sort_keys=True, ensure_ascii=False)) <= allowance


async def test_with_an_input_limit_the_grid_stops_varying_context_window(tmp_path: Path) -> None:
    def spec(limit: int | None) -> Spec:
        return Spec(
            task="Summarize the supplied values",
            input_schema={"type": "object"},
            output_schema={"type": "string"},
            floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="synthetic"),
            budget=Budget(),
            input_limit_chars=limit,
        )

    async def measured(limit: int | None) -> list[dict[str, JsonValue]]:
        chosen = spec(limit)
        floor = HeldOutTasks(chosen.floor, [
            EvaluationCase(case_id=f"{index:03}", task={"index": index}, expected="ok")
            for index in range(4)
        ])
        result = await tune(
            chosen, get_preset("synthesizer"), "fake", floor, FakeAdapter,
            runtime(tmp_path / str(limit)), tmp_path / str(limit) / "artifacts", max_candidates=16,
        )
        return [candidate.artifact.params for candidate in result.candidates]

    free = await measured(None)
    assert len(free) == 8 and {params["context_window"] for params in free} == {512, 2048}
    pinned = await measured(8192)
    assert len(pinned) == 4 and {params["context_window"] for params in pinned} == {2048}
    assert len({canonical(params) for params in pinned}) == 4


def test_game_specs_keep_context_window_since_it_sizes_play_memory() -> None:
    preset = get_preset("explorer")
    params = preset.validate_params({"context_window": 512})
    text = Spec(
        task="Explore", input_schema={"type": "object"}, output_schema={"type": "string"},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="synthetic"),
        budget=Budget(), input_limit_chars=8192,
    )
    game = text.model_copy(update={"execution_config": {"kind": "game"}})
    assert tuning._pin_context(text, preset, params)["context_window"] == 2048
    assert tuning._pin_context(game, preset, params)["context_window"] == 512
    unlimited = text.model_copy(update={"input_limit_chars": None})
    assert tuning._pin_context(unlimited, preset, params) == params


def test_the_coder_pack_takes_the_spec_input_limit() -> None:
    spec = Spec(
        task="Edit the file", input_schema={"type": "object"}, output_schema={"type": "string"},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="synthetic"),
        budget=Budget(), tools=("read",),
    )
    components = get_preset("coder").components
    params: dict[str, JsonValue] = {"context_window": 512}
    assert pack_description(spec, params, components)["context_policy"] == {"max_characters": 512}
    limited = Spec.model_validate({**spec.model_dump(mode="json"), "input_limit_chars": 4096})
    assert pack_description(limited, params, components)["context_policy"] == {
        "max_characters": 4096
    }


# -- 9.2 reply parsing (confound 2) -----------------------------------------------------

_OBJECT = '{"total": 1, "subtotal": null, "tax": 3, "item_count": 2}'
CORPUS = [
    pytest.param("```json\n" + _OBJECT + "\n```", id="fence-tagged"),
    pytest.param("```\n" + _OBJECT + "\n```", id="fence-bare"),
    pytest.param("Here is the result:\n" + _OBJECT + "\nHope that helps!", id="prose-around"),
    pytest.param("<think>Total is 1, 2 items.</think>\n\n" + _OBJECT, id="after-think"),
    pytest.param('Result: {"a": {"b": [1, {"c": null}]}, "d": "}"} end', id="nested"),
    pytest.param("The values are [3, 1, 2], sorted later.", id="array"),
    pytest.param('One {"total": 1} and two {"total": 2}', id="two-objects"),
    pytest.param('Draft {"total": 1,} then ```json\n{"total": 2}\n```', id="bad-bare-good-fence"),
    pytest.param('```json\n{"a": NaN}\n``` but {"a": 1}', id="non-finite-fence"),
    pytest.param('Braces in strings: {"note": "a } and a {", "n": 1.5}', id="string-aware"),
    pytest.param('{"total": 1', id="unbalanced"),
    pytest.param("no JSON in this reply", id="none"),
]


def forge_extract(text: str) -> object:
    """Exactly what a Forge harness's ``extract_json(text)`` returns."""
    return BUILTIN_FUNCTIONS["extract_json"](Meter(10_000_000), [text])


@pytest.mark.parametrize("text", CORPUS)
@pytest.mark.parametrize("mode", ["first", "schema"])
def test_first_and_schema_read_a_reply_exactly_as_forge_extract_json(
    text: str, mode: ReplyJson
) -> None:
    with pytest.raises(ValueError):
        json.loads(text)  # every corpus reply needs the fallback
    expected = forge_extract(text)
    if expected is None:
        with pytest.raises(PresetError, match="not valid JSON"):
            parse_reply(text, mode)
    else:
        assert canonical(parse_reply(text, mode)) == canonical(expected)


@pytest.mark.parametrize("text", CORPUS)
def test_strict_is_unchanged_and_refuses_any_wrapped_reply(text: str) -> None:
    with pytest.raises(PresetError, match="Model response is not valid JSON"):
        parse_reply(text, "strict")


@pytest.mark.parametrize("mode", ["strict", "first", "last", "schema"])
def test_a_whole_json_reply_parses_strictly_in_every_mode(mode: ReplyJson) -> None:
    assert parse_reply(" " + json.dumps(ANSWER) + "\n", mode) == ANSWER
    # A reply that is valid JSON is taken as it is, never searched for another value.
    assert parse_reply(json.dumps(json.dumps(ANSWER)), mode) == json.dumps(ANSWER)


@pytest.mark.parametrize("mode", ["first", "last", "schema"])
@pytest.mark.parametrize(
    "text", ['{"a": 1e999}', 'Answer: {"a": Infinity}', 'x {"a": 9223372036854775808}']
)
def test_fallback_values_stay_finite_json(text: str, mode: ReplyJson) -> None:
    with pytest.raises(PresetError):
        parse_reply(text, mode)


def test_last_takes_the_final_object_after_a_draft_and_skips_a_stray_brace() -> None:
    draft, final = {"total": 1, "item_count": 2}, {"total": 3, "item_count": 4}
    text = f"Draft: {json.dumps(draft)}\nChecking...\nFinal: {json.dumps(final)} and a stray {{"
    assert extract_last_json_text(text) == final
    assert parse_reply(text, "last") == final
    assert parse_reply(text, "first") == draft  # the documented difference


def test_last_needs_no_fence_rule_and_keeps_an_outer_object_whole() -> None:
    assert parse_reply("```json\n" + _OBJECT + "\n```", "last") == json.loads(_OBJECT)
    nested = 'prose {"outer": {"inner": [1, 2]}, "n": 3}'
    assert parse_reply(nested, "last") == {"outer": {"inner": [1, 2]}, "n": 3}
    thought = '<think>maybe {"total": 9}</think> {"total": 1}'
    assert parse_reply(thought, "last") == {"total": 1}
    assert parse_reply(thought, "first") == {"total": 9}


def test_last_tries_at_most_256_starts_and_skips_long_texts() -> None:
    obj = '{"a": 1}'
    assert LAST_STARTS == 256
    assert extract_last_json_text("[" * (LAST_STARTS - 1) + obj) == {"a": 1}
    assert extract_last_json_text("[" * LAST_STARTS + obj) is None
    assert extract_last_json_text(obj + " " * (PARSE_LIMIT - len(obj))) == {"a": 1}
    assert extract_last_json_text(obj + " " * (PARSE_LIMIT - len(obj) + 1)) is None


async def test_a_fenced_receipt_parses_for_presets_exactly_as_for_forge() -> None:
    reply = "Sure, here it is:\n```json\n" + json.dumps(ANSWER) + "\n```\nLet me know!"
    tolerant = receipt_spec(plain=True, reply_json="first")
    output = await run(tolerant, Recording(reply), {"receipt": "DUE 580,965"}, plain=True)
    assert canonical(output) == canonical(forge_extract(reply)) == canonical(ANSWER)
    strict = receipt_spec(plain=True)
    with pytest.raises(PresetError, match="not valid JSON"):
        await run(strict, Recording(reply), {"receipt": "DUE 580,965"}, plain=True)


async def test_schema_mode_constrains_the_answer_the_repair_and_the_self_check() -> None:
    fenced = "```json\n" + json.dumps(ANSWER) + "\n```"
    verdict = 'Checked. {"passed": true, "reason": "all four printed"}'
    adapter = Recording("not an answer", fenced, verdict)
    spec = receipt_spec(reply_json="schema")
    params: dict[str, JsonValue] = {"verifier_depth": 1, "retries": 1}
    assert await run(spec, adapter, {"receipt": "DUE 580,965"}, params=params) == ANSWER
    sent = [request.response_schema for request in adapter.requests]
    assert sent == [RECEIPT_OUT, RECEIPT_OUT, VERDICT]  # answer, repair, self-check
    recorded = [shape.response_schema_sha256 for shape in adapter.seen]
    assert recorded == [request.response_schema_sha256() for request in adapter.requests]
    assert all(digest is not None for digest in recorded)
    # Same script in ``first`` mode: parsed the same way, nothing constrained.
    loose = Recording("not an answer", fenced, verdict)
    first = receipt_spec(reply_json="first")
    assert await run(first, loose, {"receipt": "DUE 580,965"}, params=params) == ANSWER
    assert [request.response_schema for request in loose.requests] == [None, None, None]


async def test_under_count_as_miss_a_failed_self_check_answers_with_the_last_valid_candidate(
) -> None:
    """Phase 14 review: the self-check is advisory under ``count_as_miss``. A candidate
    that never passes it is still the run's answer, scored like any answer (a miss when
    wrong), so a check-and-repair configuration is never pushed into the invalid-output
    ceiling for catching its own mistakes. ``fail_partition`` keeps the refusal."""
    task: dict[str, JsonValue] = {"receipt": "DUE 580,965"}
    params: dict[str, JsonValue] = {"verifier_depth": 1, "retries": 1}
    first, repaired = {**ANSWER, "item_count": 7}, {**ANSWER, "item_count": 8}
    failed = '{"passed": false, "reason": "the item count differs"}'
    miss = receipt_spec(invalid_policy="count_as_miss")
    script = (json.dumps(first), failed, json.dumps(repaired), failed)
    assert await run(miss, Recording(*script), task, params=params) == repaired
    # A repair that is not even valid JSON leaves the first candidate standing.
    broken = Recording(json.dumps(first), failed, "not an answer")
    assert await run(miss, broken, task, params=params) == first
    assert len(broken.requests) == 3
    # With no schema-valid candidate at all the run is still invalid output.
    with pytest.raises(PresetError, match="not valid JSON"):
        await run(miss, Recording("no", "still no"), task, params=params)
    with pytest.raises(PresetError, match="self-check"):
        await run(receipt_spec(), Recording(*script), task, params=params)


@pytest.mark.parametrize("mode", ["strict", "first", "last", "schema"])
async def test_a_string_reply_is_the_text_itself_in_every_mode(mode: ReplyJson) -> None:
    reply = '```json\n{"looks": "like JSON"}\n```'
    spec = Spec.model_validate({
        "task": "Summarize", "input_schema": {"type": "object"},
        "output_schema": {"type": "string"},
        "floor": {"kind": "held_out_tasks", "limit": 1, "dataset": "synthetic"},
        "budget": {}, "preset_hint": "synthesizer", "reply_json": mode,
    })
    adapter = Recording(reply)
    assert await run(spec, adapter, {}, name="synthesizer", plain=True) == reply
    assert adapter.requests[0].response_schema is None


# -- 9.3 the plain arm --------------------------------------------------------------------


@pytest.mark.parametrize("max_candidates", [1, 16])
@pytest.mark.parametrize("halving", [False, True])
async def test_the_plain_arm_measures_one_configuration_with_one_call_per_run(
    tmp_path: Path, max_candidates: int, halving: bool
) -> None:
    spec = receipt_spec(plain=True)
    floor = receipt_floor(spec)
    adapters: list[Reader] = []

    def factory(model: str) -> ModelAdapter:
        adapter = Reader("one reply only", model=model)  # a second call would fail
        adapters.append(adapter)
        return adapter

    # A warm start is ignored, even an unrelated one: plain never measures it.
    warm_dir = tmp_path / "warm"
    other = receipt_spec()
    get_preset("extractor").emit(warm_dir, other, {"verifier_depth": 1, "retries": 1})
    warm = HarnessArtifact(
        spec=other, preset="extractor", model="fake", components=("extraction", "self_check"),
        params={"verifier_depth": 1, "retries": 1}, directory=warm_dir,
    )
    warm.save()
    result = await tune(
        spec, get_preset("extractor"), "fake", floor, factory, runtime(tmp_path),
        tmp_path / "artifacts", max_candidates=max_candidates, warm_start=warm,
        strategy=SuccessiveHalving() if halving else None,
    )
    assert result.passed and not result.warm_started
    assert len(result.candidates) == 1 and result.best is not None
    artifact = result.best.artifact
    assert artifact.params == {"verifier_depth": 0, "retries": 0}
    assert artifact.components == ("extraction",)
    assert result.best.measurement.sample_count == len(floor.tuning)
    assert len(adapters) == len(floor.tuning) and all(len(a.requests) == 1 for a in adapters)


_PLAIN_CASES: dict[str, tuple[dict[str, JsonValue], JsonValue, str]] = {
    "extractor": (RECEIPT_OUT, {"receipt": "DUE 580,965"}, json.dumps(ANSWER)),
    "synthesizer": ({"type": "string"}, {"text": "values"}, "A summary."),
    "recommender": (
        {"type": "string"}, {"query": "tea", "candidates": ["tea", "coffee"]}, "tea",
    ),
    "verifier": ({"type": "object"}, {"claim": "x"}, '{"passed": true, "reason": "ok"}'),
    "explorer": ({"type": "string"}, {"observations": []}, "An interface with two buttons."),
    "planner": ({"type": "object"}, {"rules": "reach the door"}, '{"steps": ["left"]}'),
}


@pytest.mark.parametrize("name", sorted(PLAIN_PRESETS))
async def test_every_plain_preset_makes_exactly_one_model_call(name: str) -> None:
    schema, task, reply = _PLAIN_CASES[name]
    spec = Spec.model_validate({
        "task": "Do the job", "input_schema": {"type": "object"}, "output_schema": schema,
        "floor": {"kind": "held_out_tasks", "limit": 1, "dataset": "synthetic"},
        "budget": {}, "preset_hint": name, "plain": True,
    })
    adapter = Recording(reply)
    await run(spec, adapter, task, name=name, plain=True)
    assert adapter.calls == 1
    params, _ = tuning._plain(get_preset(name))
    assert params.get("verifier_depth") == 0 and params.get("retries", 0) == 0


def test_a_plain_spec_never_consults_the_library_and_hashes_apart() -> None:
    calls: list[str] = []

    class Library:
        def lookup(self, spec: Spec, model: str) -> HarnessArtifact | None:
            calls.append(model)
            return None

    assert select_preset(receipt_spec(plain=True), "fake", Library()) == Selection(
        preset="extractor"
    )
    assert calls == []
    select_preset(receipt_spec(), "fake", Library())
    assert calls == ["fake"]
    assert receipt_spec(plain=True).spec_hash != receipt_spec().spec_hash


# -- 9.4 model aliases and the output cap -------------------------------------------------


def completion(content: str, *, model: str = ULTRA) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1", "object": "chat.completion", "model": model,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 27, "completion_tokens": 15},
    }


Handler = Callable[[httpx.Request], httpx.Response]


def ultra(handler: Handler, **options: Any) -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter(
        "ultra", api_key="k", base_url=URL, dialect="nebius", api_model=ULTRA,
        transport=httpx.MockTransport(handler), **options,
    )


def recorder(bodies: list[dict[str, Any]], reply: dict[str, Any]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=reply)

    return handler


async def test_an_alias_sends_prices_and_checks_the_provider_id() -> None:
    bodies: list[dict[str, Any]] = []
    adapter = ultra(recorder(bodies, completion("ok")))
    assert adapter.model == "ultra" and adapter.api_model == ULTRA
    response = await RunContext(adapter, Budget(cost_usd=1.0)).call("hi")  # the runtime check
    assert response == "ok" and bodies[0]["model"] == ULTRA
    priced = await adapter.complete(ModelRequest(prompt="hi"))
    assert priced.model == "ultra"
    assert priced.cost_usd == pytest.approx(price_for(load_prices(), ULTRA).cost(27, 15))
    for reported in ("ultra", "nvidia/nemotron-3-super-120b-a12b"):
        wrong = ultra(recorder([], completion("ok", model=reported)))
        with pytest.raises(AdapterError, match="model does not match"):
            await wrong.complete(ModelRequest(prompt="hi"))
    with pytest.raises(AdapterError, match="No configured price"):
        OpenAICompatibleAdapter("ultra", api_key="k", base_url=URL, dialect="nebius")


async def test_streaming_sends_the_provider_id_and_checks_the_reported_one() -> None:
    def sse(model: str) -> bytes:
        chunks = [
            {"model": model, "choices": [{"index": 0, "delta": {"content": "Hi"},
                                          "finish_reason": None}]},
            {"model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {"model": model, "choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 3}},
        ]
        return b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + (
            b"data: [DONE]\n\n"
        )

    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, content=sse(ULTRA))

    adapter = ultra(handler, max_tokens=512)
    events = [event async for event in adapter.stream_chat("", HI, (), None)]
    text = "".join(event.text for event in events if isinstance(event, TextDeltaEvent))
    assert text == "Hi" and bodies[0]["model"] == ULTRA and bodies[0]["max_tokens"] == 512
    wrong = ultra(lambda request: httpx.Response(200, content=sse("ultra")))
    with pytest.raises(AdapterError, match="model does not match"):
        async for _ in wrong.stream_chat("", HI, (), None):
            pass


@pytest.mark.parametrize(("requested", "sent"), [(None, 512), (4096, 512), (100, 100)])
async def test_max_tokens_caps_every_call_and_its_reservation(
    requested: int | None, sent: int
) -> None:
    bodies: list[dict[str, Any]] = []
    adapter = ultra(recorder(bodies, completion("ok")), max_tokens=512)
    request = ModelRequest(prompt="hi", max_output_tokens=requested)
    assert adapter.quote(request).tokens_out == sent
    assert adapter.quote_chat("", HI, (), requested).tokens_out == sent
    await adapter.complete(request)
    assert bodies[0]["max_tokens"] == sent


async def test_a_capped_openai_adapter_always_sends_its_cap() -> None:
    bodies: list[dict[str, Any]] = []
    reply = completion("ok", model="gpt-4o-mini")
    capped = OpenAICompatibleAdapter(
        api_key="k", transport=httpx.MockTransport(recorder(bodies, reply)), max_tokens=64
    )
    await capped.complete(ModelRequest(prompt="hi"))
    plain = OpenAICompatibleAdapter(
        api_key="k", transport=httpx.MockTransport(recorder(bodies, reply))
    )
    await plain.complete(ModelRequest(prompt="hi"))
    assert bodies[0]["max_tokens"] == 64 and "max_tokens" not in bodies[1]


async def test_without_the_cap_an_ultra_call_is_refused_before_spend() -> None:
    """Contract 9.4: Ultra's full 32,768-token reservation is above a $0.02 run budget."""
    budget = Budget(cost_usd=0.02, compute_s=60)
    bodies: list[dict[str, Any]] = []
    uncapped = ultra(recorder(bodies, completion("ok")))
    assert uncapped.quote(ModelRequest(prompt="hi")).cost_usd > 0.09
    with pytest.raises(BudgetExceeded):
        await RunContext(uncapped, budget).call("hi")
    assert bodies == []
    capped = ultra(recorder(bodies, completion("ok")), max_tokens=512)
    assert await RunContext(capped, budget).call("hi") == "ok"


async def test_a_plain_alias_cell_tunes_on_the_provider_id_with_its_cap(tmp_path: Path) -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        answer = read_receipt(body["messages"][0]["content"])
        return httpx.Response(200, json=completion("```json\n" + answer + "\n```"))

    def factory(model: str) -> ModelAdapter:
        return ultra(handler, max_tokens=512) if model == "ultra" else FakeAdapter(model)

    spec = receipt_spec(model_target="ultra", plain=True, reply_json="first",
                        input_limit_chars=8192)
    floor = receipt_floor(spec)
    result = await tune(
        spec, get_preset("extractor"), "ultra", floor, factory, runtime(tmp_path),
        tmp_path / "artifacts", max_candidates=4,
    )
    assert result.passed and result.best is not None and result.best.artifact.model == "ultra"
    assert len(bodies) == len(floor.tuning)
    assert {body["model"] for body in bodies} == {ULTRA}
    assert {body["max_tokens"] for body in bodies} == {512}


@pytest.mark.parametrize("provider", ["fake", "anthropic", "ollama", "claude-code", "codex"])
@pytest.mark.parametrize("field", [{"api_model": ULTRA}, {"max_tokens": 512}])
def test_alias_and_cap_are_openai_compatible_only(provider: str, field: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="OpenAI-compatible adapters only"):
        AdapterConfig.model_validate({"provider": provider, **field})
    assert AdapterConfig.model_validate({"provider": "openai", **field})


@pytest.mark.parametrize(
    "field",
    [{"api_model": "nvidia//x"}, {"api_model": "/x"}, {"api_model": "a" * 129},
     {"max_tokens": 15}, {"max_tokens": "512"}, {"max_tokens": True}],
)
def test_alias_and_cap_values_are_checked(field: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        AdapterConfig.model_validate({"provider": "openai", **field})


def test_profiles_build_aliases_price_them_by_the_provider_id_and_role_adapters_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TF_KEY", "k")
    entry = {"provider": "openai", "base_url": URL, "api_key_env": "TF_KEY"}
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({
        "models": {
            "ultra": {**entry, "api_model": ULTRA, "max_tokens": 512},
            "unknown": {**entry, "api_model": "nvidia/not-a-listed-model"},
            "bare": entry,
        },
        "forge_agent": {**entry, "model": "builder", "api_model": ULTRA, "max_tokens": 4096},
    }), encoding="utf-8")
    profile = load_profile(path)
    adapter = _adapter(profile, "ultra", profile.models["ultra"])
    assert isinstance(adapter, OpenAICompatibleAdapter)
    assert (adapter.model, adapter.api_model) == ("ultra", ULTRA)
    assert adapter.quote(ModelRequest(prompt="x")).tokens_out == 512
    assert not model_unpriced(profile, "ultra")
    assert model_unpriced(profile, "unknown") and model_unpriced(profile, "bare")
    named = profile.forge_agent
    assert isinstance(named, NamedAdapterConfig)
    agent = _adapter(profile, named.model, named)
    assert agent.model == "builder" and agent.quote(ModelRequest(prompt="x")).tokens_out == 4096
    assert not model_unpriced(profile, "builder")
    path.write_text(json.dumps({"models": {"m": {"provider": "ollama", "api_model": ULTRA}}}),
                    encoding="utf-8")
    with pytest.raises(ProfileError, match="invalid_profile"):
        load_profile(path)


# -- 9.5 the model id rule ----------------------------------------------------------------

VALID_IDS = [ULTRA, LIGHTNING, "granite4.2:8b", "team/granite:8b", "hf.co/org/model:q4", "a" * 128]
INVALID_IDS = ["/x", "x/", "a//b", "-lead", "x/-y", "", "a" * 129, "two words", "x\n"]


@pytest.mark.parametrize("name", VALID_IDS)
def test_namespaced_ids_are_valid_model_ids(name: str) -> None:
    assert TypeAdapter(ModelId).validate_python(name) == name
    check_models(BenchConfig(tier="quick", provider="fake"), None, (name,))


@pytest.mark.parametrize("name", INVALID_IDS)
def test_malformed_ids_are_refused_by_rows_and_before_spend(name: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(ModelId).validate_python(name)
    with pytest.raises(BenchError, match="bench_model_unconfigured"):
        check_models(BenchConfig(tier="quick", provider="fake"), None, (name,))


def test_a_profile_bench_resolves_full_ids_and_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TF_KEY", "k")
    entry = {"provider": "openai", "base_url": URL, "api_key_env": "TF_KEY"}
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"models": {
        ULTRA: entry, "lightning": {**entry, "api_model": LIGHTNING},
    }}), encoding="utf-8")
    profile = load_profile(path)
    config = BenchConfig(tier="quick", provider="profile", models=(ULTRA, "lightning"))
    assert resolve_models(config, profile) == (ULTRA, "lightning")
    assert provider_for(config, profile, ULTRA) == "openai"


def test_a_fake_bench_records_a_namespaced_id_and_never_makes_it_a_path(tmp_path: Path) -> None:
    """Plumbing only (fake provider): the row holds the id; no file or directory is named
    after it."""
    config = BenchConfig(
        tier="quick", provider="fake", models=(LIGHTNING,), size=4, out=tmp_path / "out"
    )
    result = asyncio.run(run_bench(config, workspace_root=tmp_path / "ws"))
    rows = read_rows(result.rows_path)
    assert rows == list(result.rows) and {row.model for row in rows} == {LIGHTNING}
    assert any(row.licensed for row in rows)
    assert all(row.evidence == "synthetic_fake" for row in rows)
    names = [path.name for path in tmp_path.rglob("*")]
    assert not any("Nemotron" in name or "nvidia" in name for name in names)
