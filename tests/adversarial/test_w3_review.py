"""W3 review (native Forge transport, pack and cost controls): two lenses.

1. Held-out isolation on the new paths: native tool calls, re-asks, the parent-side
   context cap, read refusals with directory listings, dry-run default replies and the
   per-turn telemetry rows. A full native session over a fingerprinted floor must never
   show a held-out fingerprint or private metadata to the agent model, its turn log or
   forge.json.
2. Cost: what a session is charged when its deadline cancels a streamed turn, what the
   context cap counts, what the pack hash covers and what a reply cut inside its
   reasoning does.

The tests that pinned defects found in review were strict xfails; the defects are fixed,
so they are regression tests now.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from forge_support import (
    HELD_OUT_MARK,
    REPAIR,
    SECRET,
    TUNING_MARK,
    ensure_sandbox,
    forge_compiler,
    spec,
)
from test_models_chat import ULTRA, chunked, cut_usage, delta, sse
from test_pi import _READY, ProtocolProcess, install_protocol_peer
from test_pi_native import PEER_CONTEXT, QUOTE, Streaming, call, malformed, pack, reply

from architect.backends.pi import PiBackend, PiError, _ContextCap, _native_messages
from architect.compiler import forge as forge_module
from architect.compiler.forge_pack import pack_parts
from architect.compiler.forge_settings import ForgeSettings
from architect.compiler.forge_tools import _PASSING_VERDICT
from architect.compiler.forge_turns import TURNS_DIRECTORY
from architect.compiler.forge_workspace import TUNING_MAX_BYTES, _tuning_markdown
from architect.floors import EvaluationCase
from architect.models import UsageQuote
from architect.models.chat import ChatEvent, ChatMessage, TextDeltaEvent, ToolSpec, UsageEvent
from architect.models.http import OpenAICompatibleAdapter
from architect.runtime import RunContext
from architect.runtime import wallet as wallet_module
from architect.spec import Budget
from architect.telemetry import categorical

ROOT = Path(__file__).parents[2]
PI_READY = (
    ROOT / "architect/backends/pi-node/node_modules/@mariozechner/pi-coding-agent/package.json"
).is_file()
needs_pi = pytest.mark.skipif(not PI_READY, reason="Pi dependencies are not installed")


@pytest.fixture(autouse=True)
async def working_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    await ensure_sandbox(monkeypatch, tmp_path / "probe")


Sent = tuple[str, tuple[ChatMessage, ...], tuple[ToolSpec, ...], Any]


def _seen_by_model(sent: Sequence[Sent]) -> str:
    """Everything the agent model was sent, as one string."""
    parts: list[str] = []
    for system, messages, tools, _ in sent:
        parts.append(system)
        for message in messages:
            parts.append(message.text)
            parts.extend(json.dumps(item.arguments) for item in message.tool_calls)
        parts.extend(json.dumps([tool.name, tool.description, tool.parameters]) for tool in tools)
    return "\n".join(parts)


# -- lens 1: held-out isolation on every new path --------------------------------------


@needs_pi
async def test_a_native_session_over_every_new_path_never_shows_held_out_data(
    tmp_path: Path,
) -> None:
    script = [
        reply(call("read", "c1", path=".")),  # the working directory's listing
        reply(call("read", "c2", path="templates")),  # a directory's listing
        reply(call("read", "c3", path="missing/deeper/file.txt")),  # nearest folder's list
        reply(
            call("read", "c4", path="TUNING.md"),
            call("read", "c5", path="SPEC.md"),
            call("read", "c6", path="BASELINE.json"),
            call("read", "c7", path="LANGUAGE.md"),
        ),
        reply(malformed("write", "c8")),  # re-asked once with fixed errors
        reply(call("write", "c9", path="harness.py", content=REPAIR)),
        reply(call("forge_check", "c10")),
        reply(call("forge_dryrun", "c11", case=0)),  # default replies: schema-shaped, verdict
        reply(call("forge_measure", "c12", cases=[0, 1, 2])),
        reply(text="Progress so far."),  # a final answer before any submission: nudged
        reply(call("forge_submit", "c13", note="three guarded calls")),
        reply(text="Submitted."),
    ]
    agent = Streaming(*script)
    compiler = forge_compiler(
        tmp_path,
        # The smallest context cap, so the parent-side cap drops old results.
        settings=ForgeSettings(enabled=True, context_chars=4096),
        agent=agent,  # type: ignore[arg-type]
    )
    result = await compiler.compile(spec())
    assert result.artifact is not None and result.artifact.forge is not None
    seen = _seen_by_model(agent.sent)
    # The new paths really ran: listings, the dry run, the nudge and the cap.
    assert "holds:" in seen and "Not found: missing/deeper/file.txt" in seen
    assert "invalid_arguments" in seen and '"schema_ok":true' in seen  # the dry run ran
    assert TUNING_MARK in seen and "forge_submit has not been called yet" in seen
    assert "removed to keep the conversation under its size limit" in seen
    # Nothing held out, and no private metadata, ever reached the agent model.
    assert HELD_OUT_MARK not in seen and SECRET not in seen
    rows = sorted((tmp_path / "telemetry" / TURNS_DIRECTORY).glob("*.jsonl"))
    logged = "\n".join(path.read_text(encoding="utf-8") for path in rows)
    record = (result.artifact.directory / "forge.json").read_text(encoding="utf-8")
    for text in (logged, record):
        assert HELD_OUT_MARK not in text and SECRET not in text and TUNING_MARK not in text
    assert len(logged.splitlines()) == len(agent.sent)  # one content-free row per call


# -- lens 2: cost --------------------------------------------------------------------


class _Hanging(Streaming):
    """Like ``OpenAICompatibleAdapter.stream_chat``: usage arrives only after the last
    chunk, so a stream stopped before its end has reported none. With ``partial``, like
    ``AnthropicAdapter.stream_chat``: ``message_start`` reports the prompt and one output
    token first, which is not the final usage either."""

    def __init__(self, *, partial: bool = False) -> None:
        super().__init__()
        self.partial = partial

    async def stream_chat(
        self,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        max_output_tokens: int | None,
    ) -> AsyncIterator[ChatEvent]:
        self.sent.append((system, tuple(messages), tuple(tools), max_output_tokens))
        try:
            if self.partial:
                yield UsageEvent(1000, 1, QUOTE / 20)
            yield TextDeltaEvent("Planning the harness")
            await asyncio.Event().wait()
        finally:
            self.closed += 1


class _HangingEnvelope:
    """A completion-only agent whose one call never returns (the JSON envelope)."""

    model = "fake"

    def quote(self, request: object) -> UsageQuote:
        return UsageQuote(tokens_in=1000, tokens_out=100, cost_usd=QUOTE)

    async def complete(self, request: object) -> Any:
        await asyncio.Event().wait()


async def _deadline_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent: object
) -> tuple[float, float, float]:
    """(agent run's telemetry $, the Forge wallet's $, ForgeResult.agent_cost_usd) for a
    Forge session whose deadline arrives while its only agent call is in flight."""
    wallets: list[wallet_module.CompileWallet] = []
    real = forge_module.compilation_wallet

    @contextmanager
    def spy(budget: Budget) -> Iterator[wallet_module.CompileWallet]:
        with real(budget) as opened:
            wallets.append(opened)  # the Forge session's own wallet
            yield opened

    monkeypatch.setattr(forge_module, "compilation_wallet", spy)
    seen: list[Any] = []
    compiler = forge_compiler(
        tmp_path,
        settings=ForgeSettings(enabled=True, max_s=10.0),
        agent=agent,  # type: ignore[arg-type]
    )
    compiler.forge_listener = seen.append
    await compiler.compile(spec())
    [forge_result] = seen
    assert forge_result.failures == ("forge_no_submission",)
    label = categorical("forge_agent")
    [row] = [row for row in compiler.runtime.store.rows() if row.preset_origin == label]
    assert row.model_calls == 1
    [forge_wallet] = wallets
    return row.cost_usd, forge_wallet.cost_usd, forge_result.agent_cost_usd


@needs_pi
async def test_on_the_envelope_a_turn_cut_off_by_the_deadline_keeps_its_quote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, wallet, _ = await _deadline_session(tmp_path, monkeypatch, _HangingEnvelope())
    assert run == pytest.approx(QUOTE) and wallet == pytest.approx(QUOTE)


@needs_pi
@pytest.mark.parametrize("partial", [False, True], ids=["no_usage", "partial_usage"])
async def test_on_native_calls_a_turn_cut_off_by_the_deadline_keeps_its_quote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, partial: bool
) -> None:
    agent = _Hanging(partial=partial)
    run, wallet, metered = await _deadline_session(tmp_path, monkeypatch, agent)
    assert len(agent.sent) == 1 and agent.closed == 1
    # Each was 0.0 with no usage reported, and QUOTE / 20 after the partial usage (the
    # meter's figure is the one forge.json and the turn row carry).
    assert run >= QUOTE and wallet >= QUOTE and metered >= QUOTE


def _edit_session(edits: int, size: int) -> dict[str, Any]:
    """A Pi context of ``edits`` turns, each one ``edit`` call replacing ``size`` chars."""
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": '{"task":"t"}'}]}
    ]
    for index in range(edits):
        arguments = {
            "path": "harness.py",
            "edits": [{"oldText": "o" * size, "newText": "n" * size}],
        }
        messages.append(
            {
                "role": "assistant",
                "content": [
                    {"type": "toolCall", "id": f"e{index}", "name": "edit", "arguments": arguments}
                ],
            }
        )
        messages.append(
            {
                "role": "toolResult",
                "toolCallId": f"e{index}",
                "toolName": "edit",
                "content": [{"type": "text", "text": "Successfully replaced text in harness.py."}],
                "isError": False,
            }
        )
    return {"systemPrompt": "s", "messages": messages, "tools": []}


def test_the_parent_context_cap_bounds_an_edit_heavy_session() -> None:
    limit = ForgeSettings().context_chars
    pi_context = _edit_session(edits=40, size=1500)  # 120,000 characters of edits
    view = _ContextCap(limit).apply(pi_context)
    sent = sum(
        len(message.text) + sum(len(json.dumps(c.arguments)) for c in message.tool_calls)
        for message in _native_messages(view)
    )
    assert sent <= limit  # it was 124,252, and ``view is pi_context``


def test_the_pack_hash_covers_the_fixed_texts_forge_reads() -> None:
    parts = "\n".join(pack_parts(ForgeSettings()).values())
    bridge = (ROOT / "architect/backends/pi-node/bridge.mjs").read_text(encoding="utf-8")
    extension = (ROOT / "architect/backends/pi-node/extension.ts").read_text(encoding="utf-8")
    sent = [
        "You are a coding agent. Use only granted tools.",  # bridge.mjs system prompt
        "No autonomous subagent spawning. compile_agent is the only nested compiler capability.",
    ]
    assert sent[0] in bridge and sent[1] in extension  # both reach every Forge system prompt
    missing = [
        text
        for text in (*sent, _PASSING_VERDICT, "every measurement uses this model")
        # A part may hold a text as written or JSON-encoded (the parts with fixed texts).
        if text not in parts and json.dumps(text)[1:-1] not in parts
    ]
    assert missing == []


def test_every_case_tuning_md_shows_fits_one_read() -> None:
    cases = [
        EvaluationCase(case_id=f"c{index:02d}", task={"text": "x" * 1400}, expected="y" * 300)
        for index in range(16)
    ]
    text = _tuning_markdown(cases, ForgeSettings())
    assert len(text.encode("utf-8")) <= TUNING_MAX_BYTES
    shown = text.count("## Case ")
    assert text[: forge_module._RESULT_CHARS].count("## Case ") == shown  # it was 10 of 13


async def test_a_native_reply_cut_inside_its_reasoning_is_re_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = sse(
        delta(ULTRA, role="assistant", content=""),
        *(delta(ULTRA, reasoning_content=part) for part in ("The", " user wants", " a plan")),
        delta(ULTRA, "length"),
        cut_usage(ULTRA),
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return chunked(body)

    adapter = OpenAICompatibleAdapter(
        ULTRA,
        api_key="k",
        base_url="https://api.tokenfactory.nebius.com/v1",
        dialect="nebius",
        think=True,
        transport=httpx.MockTransport(handler),
    )
    peer = ProtocolProcess([_READY, {"kind": "model", "id": 1, "context": PEER_CONTEXT}])
    install_protocol_peer(monkeypatch, peer)
    ctx = RunContext(adapter, Budget(cost_usd=1, compute_s=60), repository=tmp_path)
    # Fixed, the cut is re-asked once, its text is the final answer and the inert peer's
    # stream then ends. Before the fix, REASONING_CUT escaped on the first request.
    with pytest.raises(PiError, match="Invalid Pi protocol"):
        await PiBackend().run(ctx, {}, pack("read", "write", max_output_tokens=64))
    assert len(requests) == 2
