"""Forge cost controls and evidence (W3b): the agent's output cap, the required submission,
what the workspace shows, the schema-shaped dry run, and per-turn rows.

Offline: scripted Pi sessions, fake or scripted streaming agents, the real sandbox worker.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from forge_support import (
    REPAIR,
    ScriptedPi,
    cases,
    ensure_sandbox,
    floor_factory,
    forge_compiler,
    install_pi,
    play_factory,
    reply,
    spec,
)
from test_pi_native import COST, PARTIAL, QUOTE, Streaming, call
from test_pi_native import context as native_context
from test_pi_native import pack as native_pack
from test_pi_native import reply as native_reply

from architect.backends.pi import (
    _DROPPED_RESULT,
    _UNFINISHED,
    _UNFINISHED_AGAIN,
    PiBackend,
    PiConfig,
)
from architect.compiler.forge import (
    _RESULT_CHARS,
    FORGE_INSTRUCTIONS,
    _metered,
    forge_prompt,
)
from architect.compiler.forge_design import DESIGN_TEXT
from architect.compiler.forge_pack import pack_hash
from architect.compiler.forge_settings import ForgeSettings
from architect.compiler.forge_templates import TEMPLATE_NAMES, instantiate
from architect.compiler.forge_tools import (
    ForgeSession,
    add_result_observer,
    forge_tools,
    remove_result_observer,
)
from architect.compiler.forge_turns import TURNS_DIRECTORY, ForgeTurn
from architect.compiler.forge_workspace import (
    TUNING_MAX_BYTES,
    _tuning_markdown,
    language_text,
    prepare_workspace,
)
from architect.compiler.types import CandidateEvaluation, Measurement
from architect.floors import EvaluationCase, HeldOutTasks
from architect.library import HarnessLibrary
from architect.models import (
    AdapterError,
    AnthropicAdapter,
    FakeAdapter,
    ModelRequest,
    ModelResponse,
)
from architect.models.chat import ChatMessage, DoneEvent, TextDeltaEvent, UsageEvent
from architect.presets import get_preset
from architect.runtime import RunContext, Runtime
from architect.runtime.wallet import compilation_wallet
from architect.spec import Budget, FloorDefinition, FloorKind, HarnessArtifact
from architect.telemetry import TelemetryStore, categorical

ROOT = Path(__file__).parents[1]
PI_READY = (
    ROOT / "architect/backends/pi-node/node_modules/@mariozechner/pi-coding-agent/package.json"
).is_file()
RECEIPT_SCHEMA: dict[str, Any] = {
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


# -- the prompt -----------------------------------------------------------------------


def test_the_prompt_carries_the_card_names_the_templates_and_prefers_edit() -> None:
    text = " ".join(FORGE_INSTRUCTIONS.split())
    # The card is in the prompt, so no step asks the engineer to read LANGUAGE.md.
    assert "LANGUAGE.md holds the same card; you never need to read it" in text
    assert "1. Read SPEC.md, DESIGN.md, TUNING.md" in text and "Read LANGUAGE.md" not in text
    for name in TEMPLATE_NAMES:
        assert f"templates/{name}.py" in text
    assert "change it with edit instead of writing the whole file again" in text
    assert "only its 4 fixed imports" in text
    assert "It lists every refused line, each with its rule and the allowed form" in text
    prompt = forge_prompt()
    assert prompt.startswith(FORGE_INSTRUCTIONS) and prompt.endswith(language_text())
    assert prompt.isascii() and len(prompt) < 12_000


def test_design_md_stays_general() -> None:
    text = " ".join(DESIGN_TEXT.split())
    for strategy in (
        "Code before model.",
        "Enforce the rules the task states.",
        "Check and repair.",
        "Measure, then learn from failures, not answers.",
        "a submission that is not better than the preset is refused",
    ):
        assert strategy in text, strategy
    assert DESIGN_TEXT.isascii() and "\u2014" not in DESIGN_TEXT


@pytest.mark.skipif(not PI_READY, reason="Pi dependencies are not installed")
async def test_the_language_card_survives_context_trimming(tmp_path: Path) -> None:
    """Old tool results are dropped once the context passes its cap; the card, in the
    system prompt, reaches the model whole on every turn (even after a read of
    LANGUAGE.md itself was dropped)."""
    card = language_text()
    (tmp_path / "LANGUAGE.md").write_text(card, encoding="utf-8")
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text((name[0] * 60 + "\n") * 50, encoding="utf-8")
    adapter = Streaming(
        native_reply(call("read", "r0", path="LANGUAGE.md")),
        native_reply(call("read", "r1", path="a.txt")),
        native_reply(call("read", "r2", path="b.txt")),
        native_reply(call("read", "r3", path="c.txt")),
        native_reply(text="Done."),
    )
    backend = PiBackend(PiConfig(context_cap="parent"))
    bound = {
        **native_pack("read"),
        "prompt_fragment": forge_prompt(),
        "context_policy": {"max_characters": 4000},
    }
    assert await backend.run(native_context(tmp_path, adapter), {}, bound) == "Done."
    assert len(adapter.sent) == 5
    for system, _, _, _ in adapter.sent:
        assert card in system and FORGE_INSTRUCTIONS in system
    _, last, _, _ = adapter.sent[-1]
    results = [message.text for message in last if message.role == "tool"]
    # The read of LANGUAGE.md and the next old results were trimmed away ...
    assert results[0] == _DROPPED_RESULT and results[1] == _DROPPED_RESULT
    # ... while the newest stayed whole.
    assert results[-1].count("c" * 60) == 50


# -- the session the agent gets ---------------------------------------------------------


def priced(model: str) -> FakeAdapter:
    return FakeAdapter(model, responses=[reply("turn", model=model)] * 8)


@pytest.mark.parametrize(
    ("agent", "settings", "cap"),
    [
        # A priced agent whose capacity (64,000) is above the cap gets the cap.
        (lambda: priced("claude-sonnet-4-20250514"), {}, 12288),
        (lambda: priced("claude-sonnet-4-20250514"), {"agent_max_tokens": 8192}, 8192),
        # An agent whose own capacity is already lower keeps it (the fake: 2 tokens).
        (lambda: priced("fake"), {}, None),
        # A $0 local agent keeps its capacity: its server would reserve context for it.
        (lambda: priced("llama3.2"), {}, None),
        # A capacity the meter cannot see (a scripted stream) still gets the cap.
        (lambda: Streaming(), {}, 12288),
    ],
)
async def test_the_agent_reply_is_capped_and_a_submission_is_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent: Any, settings: dict[str, Any],
    cap: int | None,
) -> None:
    pi = install_pi(monkeypatch, ScriptedPi([]))
    compiler = forge_compiler(
        tmp_path, settings=ForgeSettings(enabled=True, trigger="always", **settings),
        agent=agent(),
    )
    await compiler.compile(spec())
    assert pi.config is not None and pi.pack is not None
    assert pi.config.final_requires == "forge_submit"
    assert pi.config.context_cap == "parent" and pi.config.repair_prose is True
    assert pi.pack.get("max_output_tokens") == cap
    assert pi.pack["context_policy"] == {"max_characters": 60000}


@pytest.mark.skipif(not PI_READY, reason="Pi dependencies are not installed")
async def test_a_native_session_that_stops_before_submitting_is_asked_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await ensure_sandbox(monkeypatch, tmp_path / "probe")
    agent = Streaming(
        native_reply(text="I have read the files and will continue later."),
        native_reply(call("write", "w1", path="harness.py", content=REPAIR)),
        native_reply(call("forge_submit", "s1", note="repair")),
        native_reply(text="Submitted."),  # submissions remain: asked once more
        native_reply(text="Submitted, and it is the best I can do."),
    )
    compiler = forge_compiler(
        tmp_path, settings=ForgeSettings(enabled=True), agent=agent,  # type: ignore[arg-type]
        library=HarnessLibrary(tmp_path / "library"),
    )
    result = await compiler.compile(spec())
    assert len(agent.sent) == 5
    _, nudged, _, cap = agent.sent[1]
    assert nudged[-1] == ChatMessage("user", _UNFINISHED.format(tool="forge_submit"))
    assert cap == 12288
    _, again, _, _ = agent.sent[4]
    assert again[-1] == ChatMessage("user", _UNFINISHED_AGAIN.format(tool="forge_submit"))
    artifact = result.artifact
    assert artifact is not None and artifact.licensed and artifact.forge is not None
    record = json.loads((artifact.directory / "forge.json").read_text(encoding="utf-8"))
    assert record["submissions"] == 1 and record["accepted"] == 1


# -- what the workspace shows -----------------------------------------------------------


def numbered(count: int, chars: int = 40) -> list[EvaluationCase]:
    return [
        EvaluationCase(case_id=f"c{i:03d}", task={"text": f"{i:03d}" + "x" * chars},
                       expected={"n": i})
        for i in range(count)
    ]


def test_tuning_md_shows_the_first_sixteen_cases() -> None:
    text = _tuning_markdown(numbered(40), ForgeSettings())
    assert "## Case 15" in text and "## Case 16" not in text
    assert "Cases 0 to 15 are shown here; forge_measure reaches all 40." in text
    assert text.startswith("# Tuning cases (40)\n")
    # With every case shown, the text has no such line.
    assert "are shown here" not in _tuning_markdown(numbered(12), ForgeSettings())
    assert "## Case 3" in _tuning_markdown(numbered(4), ForgeSettings(max_cases_shown=4))


def test_tuning_md_stays_within_its_byte_bound() -> None:
    text = _tuning_markdown(numbered(30, chars=1900), ForgeSettings())
    assert len(text.encode("utf-8")) <= TUNING_MAX_BYTES == 16_000
    shown = text.count("## Case ")
    assert 1 <= shown < 16 and f"Cases 0 to {shown - 1} are shown here" in text
    # One read returns all of it: a Forge tool result is cut at 16,384 characters.
    assert TUNING_MAX_BYTES < _RESULT_CHARS
    assert text[:_RESULT_CHARS].count("## Case ") == shown


def test_a_first_case_too_large_for_tuning_md_is_clipped_harder() -> None:
    # case_chars allows 20,000 characters of input and of expected output: the first case
    # is still shown, both halves of it, within the byte bound.
    settings = ForgeSettings(case_chars=20000)
    big = [EvaluationCase(case_id="c0", task={"t": "Q" * 20000}, expected={"e": "Z" * 20000})]
    text = _tuning_markdown(big, settings)
    assert len(text.encode("utf-8")) <= TUNING_MAX_BYTES
    assert text.count("## Case ") == 1 and "input:" in text and "expected:" in text
    assert text.count("... [truncated]") == 2 and text.count("Q") == text.count("Z") > 6000
    # Under the bound nothing changes: the same case at the default clip is shown as before.
    small = _tuning_markdown(big, ForgeSettings())
    assert small.count("Q") == ForgeSettings().case_chars - len('{\n  "t": "')


def test_receipt_practice_cases_fit_in_tuning_md() -> None:
    records = json.loads(
        (ROOT / "examples/receipts/floors/evidence.json").read_text(encoding="utf-8")
    )
    receipts = [EvaluationCase.model_validate(item) for item in records[:100]]
    text = _tuning_markdown(receipts, ForgeSettings())
    assert text.count("## Case ") == 16
    assert len(text.encode("utf-8")) < 24 * 1024


def test_spec_md_names_the_templates_that_hold_the_binding(tmp_path: Path) -> None:
    evaluation = extractor_candidate(tmp_path)
    floor = HeldOutTasks(evaluation.artifact.spec.floor, cases(6))
    root = prepare_workspace(tmp_path / "ws", evaluation, floor, ForgeSettings())
    text = (root / "SPEC.md").read_text(encoding="utf-8")
    assert (
        "- bound preset: extractor (templates/prompt_chain.py, templates/extract_verify.py and "
        "templates/vision_two_pass.py call it with the exact binding)"
    ) in text


# -- the dry run's default reply ----------------------------------------------------------


MEASUREMENT = Measurement(
    score=0.0, floor_passed=False, budget_passed=True, mean_cost_usd=0.001, max_tokens=20,
    max_cost_usd=0.001, max_compute_s=0.1, sample_count=3, failures=("floor_not_met",),
)


def extractor_candidate(tmp_path: Path) -> CandidateEvaluation:
    value = spec(
        task="Read the receipt text and return its totals.",
        output_schema=RECEIPT_SCHEMA,
        domain="extraction",
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="forge-tasks"),
    )
    preset = get_preset("extractor")
    directory = tmp_path / "stage2"
    preset.emit(directory, value, {}, preset.components)
    artifact = HarnessArtifact(
        spec=value, preset=preset.name, model="fake", directory=directory,
        components=preset.components, params=preset.validate_params({}),
        execution_mode="native",
    )
    return CandidateEvaluation(artifact=artifact, measurement=MEASUREMENT)


async def test_the_incumbent_passes_its_first_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await ensure_sandbox(monkeypatch, tmp_path / "probe")
    evaluation = extractor_candidate(tmp_path)
    definition = evaluation.artifact.spec.floor
    floor = HeldOutTasks(definition, cases(6))
    settings = ForgeSettings(enabled=True)
    workspace = prepare_workspace(tmp_path / "ws", evaluation, floor, settings)
    assert (workspace / "harness.py").read_text("utf-8") == instantiate(
        "extract_verify", evaluation
    )
    runtime = Runtime(TelemetryStore(tmp_path / "telemetry"))
    session = ForgeSession(
        candidate=evaluation, floor=floor, smoke_floor=HeldOutTasks(definition, cases(6)),
        adapter_factory=play_factory(), runtime=runtime, settings=settings,
        workspace=workspace, private=tmp_path / "private", floor_factory=floor_factory(),
    )
    tools = forge_tools(session)
    with compilation_wallet(Budget(cost_usd=1, compute_s=120)) as wallet:
        session.wallet = wallet
        default = await tools["forge_dryrun"].callback({"case": 0})
        old = await tools["forge_dryrun"].callback({"case": 0, "replies": ["{}"]})
    assert default["ok"] is True and default["schema_ok"] is True, default
    assert json.loads(default["output_head"]) == {
        "item_count": 0, "subtotal": 0, "tax": 0, "total": 0
    }
    assert old["ok"] is False  # the old default made the incumbent look broken
    assert wallet.cost_usd == 0


# -- per-turn rows --------------------------------------------------------------------------


def usage_reply(model: str = "fake") -> ModelResponse:
    return ModelResponse(
        text="turn", model=model, tokens_in=1000, tokens_out=200, cost_usd=0.0005,
        cache_read_tokens=600, reasoning_tokens=50,
    )


async def test_turn_rows_reach_the_result_forge_json_and_the_telemetry_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await ensure_sandbox(monkeypatch, tmp_path / "probe")
    install_pi(monkeypatch, ScriptedPi([
        {"write": {"path": "harness.py", "content": REPAIR}},
        {"tool": "forge_submit", "arguments": {"note": "repair"}},
        {"text": "done"},
    ]))
    agent = FakeAdapter("fake", responses=[usage_reply()] * 8)
    settings = ForgeSettings(enabled=True)
    compiler = forge_compiler(tmp_path, settings=settings, agent=agent)
    results: list[Any] = []
    compiler.forge_listener = results.append
    seen: list[dict[str, Any]] = []
    add_result_observer(seen.append)
    try:
        result = await compiler.compile(spec())
    finally:
        remove_result_observer(seen.append)
    expected = ForgeTurn(
        turn=1, prompt_tokens=1000, cached_tokens=600, reasoning_tokens=50,
        completion_tokens=200, cost_usd=0.0005, seconds=0.0, stop=None,
    )
    [forged] = results
    assert len(forged.turns) == 3
    for number, row in enumerate(forged.turns, start=1):
        assert row.model_copy(update={"seconds": 0.0}) == expected.model_copy(
            update={"turn": number}
        )
    assert forged.pack_sha256 == pack_hash(settings)
    artifact = result.artifact
    assert artifact is not None and artifact.licensed
    record = json.loads((artifact.directory / "forge.json").read_text(encoding="utf-8"))
    assert record["pack_sha256"] == pack_hash(settings)
    assert [row["turn"] for row in record["turns"]] == [1, 2, 3]
    assert record["turns"][0]["cached_tokens"] == 600
    # The session's end reaches the process-wide observers with the same numbers only.
    finished = [item for item in seen if item.get("event") == "finished"]
    assert len(finished[-1]["result"]["turns"]) == 3
    # And each row was logged as its call ended, outside any artifact.
    [log] = list((tmp_path / "telemetry" / TURNS_DIRECTORY).glob("*.jsonl"))
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [line["turn"] for line in lines] == [1, 2, 3]
    assert {line["session"] for line in lines} == {lines[0]["session"]}
    assert lines[0]["model"] == categorical("fake") and lines[0]["depth"] == 0
    assert lines[0]["spec_hash"] == spec().spec_hash
    assert set(lines[0]) == {
        "session", "depth", "spec_hash", "model", "time", "turn", "prompt_tokens",
        "cached_tokens", "reasoning_tokens", "completion_tokens", "cost_usd", "seconds",
        "stop", "estimated", "ok",
    }


async def test_streamed_turns_record_the_stop_reason_and_failed_calls() -> None:
    inner = Streaming(
        [TextDeltaEvent("Long."), UsageEvent(100, 20, COST, cache_read_tokens=60,
                                             reasoning_tokens=7), DoneEvent("max_tokens")],
    )
    agent = _metered(inner)  # type: ignore[arg-type]
    asked = [ChatMessage("user", "u")]
    events = [event async for event in agent.stream_chat("s", asked, (), 64)]  # type: ignore[attr-defined]
    assert events[-1] == DoneEvent("max_tokens")
    first = agent.turns[0]
    assert (first.prompt_tokens, first.cached_tokens, first.reasoning_tokens) == (100, 60, 7)
    assert (first.completion_tokens, first.stop, first.ok) == (20, "max_tokens", True)
    # A stream closed early is a failed turn at its quote, marked estimated: what it
    # reported before it stopped (PARTIAL) was not its final usage.
    inner.pause = True
    stream = agent.stream_chat("s", asked, (), None)  # type: ignore[attr-defined]
    assert await anext(stream) == PARTIAL
    await stream.aclose()
    second = agent.turns[1]
    assert (second.turn, second.prompt_tokens, second.ok, second.stop) == (2, 1000, False, None)
    assert second.estimated and second.cost_usd == pytest.approx(QUOTE)
    # A plain adapter's call has no stop reason; a failed one that cannot even be quoted
    # (the fake's script is used up) is a row of zeros.
    plain = _metered(FakeAdapter("fake", responses=[usage_reply()]))
    await plain.complete(ModelRequest(prompt="x"))
    with pytest.raises(Exception, match="exhausted"):
        await plain.complete(ModelRequest(prompt="y"))
    assert [(row.turn, row.ok, row.stop) for row in plain.turns] == [
        (1, True, None), (2, False, None)
    ]
    assert plain.turns[1].prompt_tokens == 0 and plain.turns[1].cost_usd == 0


class Failing(FakeAdapter):
    """Quotes like the fake, then fails before any usage is reported."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise AdapterError("Provider request failed")


class Silent(Streaming):
    """Streams text and then nothing: like an OpenAI-compatible stream, whose usage comes
    only in its last chunk, stopped before that chunk."""

    async def stream_chat(self, *args: Any, **kwargs: Any) -> Any:
        self.sent.append(args)
        try:
            yield TextDeltaEvent("Planning")
            await asyncio.Event().wait()
        finally:
            self.closed += 1


async def test_a_call_that_ends_before_reporting_usage_counts_its_quote() -> None:
    plain = _metered(Failing("fake", responses=[usage_reply()]))
    with pytest.raises(AdapterError):
        await plain.complete(ModelRequest(prompt="x"))
    [row] = plain.turns
    assert (row.ok, row.estimated, row.prompt_tokens, row.completion_tokens) == (
        False, True, 1000, 200
    )
    assert row.cost_usd == plain.cost_usd == pytest.approx(0.0005)
    # A stream stopped before its usage chunk: its quote too, as the wallets keep it.
    agent = _metered(Silent())  # type: ignore[arg-type]
    stream = agent.stream_chat("s", [ChatMessage("user", "u")], (), 64)  # type: ignore[attr-defined]
    assert await anext(stream) == TextDeltaEvent("Planning")
    await stream.aclose()
    [streamed] = agent.turns
    assert (streamed.ok, streamed.estimated, streamed.stop) == (False, True, None)
    assert (streamed.prompt_tokens, streamed.completion_tokens) == (1000, 100)
    assert streamed.cost_usd == agent.cost_usd == pytest.approx(QUOTE)


def _sse(kind: str, body: dict[str, Any]) -> bytes:
    return f"event: {kind}\ndata: {json.dumps(body)}\n\n".encode()


@pytest.mark.parametrize("ending", ["cancelled", "failed"])
async def test_an_anthropic_turn_stopped_after_message_start_counts_its_quote(
    ending: str,
) -> None:
    # message_start reports the prompt and one output token before any reply. A turn the
    # Forge deadline cancels, or that fails, after 2,000 words of reply keeps its quote
    # in the run, the wallet, the meter and the turn row alike.
    model = "claude-sonnet-4-20250514"
    streamed = asyncio.Event()

    async def body() -> AsyncIterator[bytes]:
        start = {"model": model, "usage": {"input_tokens": 3000, "output_tokens": 1}}
        yield _sse("message_start", {"type": "message_start", "message": start})
        block = {"type": "text", "text": ""}
        yield _sse("content_block_start", {"type": "content_block_start", "index": 0,
                                           "content_block": block})
        for _ in range(200):
            text = {"type": "text_delta", "text": "word " * 10}
            yield _sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                                               "delta": text})
        if ending == "failed":
            error = {"type": "overloaded_error", "message": "Overloaded"}
            yield _sse("error", {"type": "error", "error": error})
            return
        streamed.set()
        await asyncio.Event().wait()

    inner = AnthropicAdapter(
        model, api_key="k",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body())),
    )
    agent = _metered(inner)  # native, since Anthropic streams
    context = RunContext(agent, Budget(cost_usd=5, compute_s=120))
    messages = [ChatMessage("user", "Write harness.py.")]
    quote = inner.quote_chat("s", messages, (), 12288)
    with compilation_wallet(Budget(cost_usd=5, compute_s=120)) as wallet:
        if ending == "failed":
            with pytest.raises(AdapterError, match="overloaded_error"):
                await context.stream_call("s", messages, (), 12288)
        else:
            task = asyncio.create_task(context.stream_call("s", messages, (), 12288))
            await asyncio.wait_for(streamed.wait(), 30)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    [row] = agent.turns
    assert (row.ok, row.estimated, row.stop) == (False, True, None)
    assert (row.prompt_tokens, row.completion_tokens) == (quote.tokens_in, quote.tokens_out)
    for charged in (wallet.cost_usd, context.cost_usd, agent.cost_usd, row.cost_usd):
        assert charged == pytest.approx(quote.cost_usd)


def test_pi_config_defaults_keep_the_old_behaviour() -> None:
    config = PiConfig()
    assert config.final_requires is None and config.context_cap == "extension"


@pytest.mark.parametrize(("ceiling", "cap"), [(None, 12288), (20000, 12288), (8192, None)])
def test_a_profile_ceiling_at_or_below_the_cap_leaves_the_request_alone(
    ceiling: int | None, cap: int | None
) -> None:
    from architect.compiler.forge import _agent_output_cap
    from architect.models.http import OpenAICompatibleAdapter

    def never(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request is made")

    ultra = OpenAICompatibleAdapter(
        "nvidia/Nemotron-3-Ultra-550b-a55b", api_key="k",
        base_url="https://api.tokenfactory.nebius.com/v1", dialect="nebius",
        max_tokens=ceiling, transport=httpx.MockTransport(never),
    )
    # Through Forge's meter, as the session sees it; Ultra's price capacity is 32,768.
    assert _agent_output_cap(_metered(ultra), ForgeSettings()) == cap
