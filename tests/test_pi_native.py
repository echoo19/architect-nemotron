"""Pi parent turns as native tool calls (P3), driven by a scripted streaming adapter.

When the bound adapter streams native chats, ``PiBackend`` sends Pi's granted tools as the
provider's own tool definitions, reads the tool calls it returns and hands the bridge the
same control envelope a JSON reply would produce, so bridge.mjs is unchanged. Adapters
without ``stream_chat`` keep the JSON envelope (``test_pi_protocol``). Offline: nothing here
calls a live provider, a CLI agent or Ollama.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from session_support import fake_profile
from test_pi import _READY, ProtocolProcess, install_protocol_peer

from architect.agent.build import _forge_agent
from architect.agent.coder import run_coder
from architect.agent.events import RecordingUI
from architect.agent.tools import ToolContext
from architect.backends.pi import (
    _BAD_ARGUMENTS,
    _CUT_REPLY,
    _NATIVE_INSTRUCTION,
    _NATIVE_NO_TOOLS,
    _NOT_RUN,
    _UNANSWERED,
    PiBackend,
    PiConfig,
    PiError,
    TrustedTool,
    _bridge_control,
    _native_envelope,
    _native_messages,
    _native_tools,
)
from architect.compiler.forge import _Metered, _metered
from architect.models import FakeAdapter, ModelRequest, ModelResponse, UsageQuote
from architect.models.chat import (
    ChatEvent,
    ChatMessage,
    ChatResult,
    ChatStreaming,
    DoneEvent,
    StopReason,
    TextDeltaEvent,
    ToolCallEvent,
    ToolSpec,
    UsageEvent,
)
from architect.profiles import build_services, load_profile
from architect.runtime import BudgetExceeded, RunContext
from architect.runtime.wallet import compilation_wallet
from architect.session.observe import Meter, ObservedAdapter
from architect.spec import Budget

QUOTE = 0.01  # reserved before each call
COST = 0.002  # reported by each call
PARTIAL = UsageEvent(50, 0, 0.0005)  # what a paused stream reported before it stopped


def reply(
    *calls: ToolCallEvent,
    text: str = "",
    usage: UsageEvent | None = None,
    stop: StopReason | None = None,
) -> list[ChatEvent]:
    events: list[ChatEvent] = [TextDeltaEvent(text)] if text else []
    events.extend(calls)
    events.append(usage or UsageEvent(100, 20, COST))
    events.append(DoneEvent(stop or ("tool_use" if calls else "end_turn")))
    return events


def call(name: str, ident: str = "", **arguments: Any) -> ToolCallEvent:
    return ToolCallEvent(ident or f"call-{name}", name, arguments)


def malformed(name: str, ident: str = "bad") -> ToolCallEvent:
    return ToolCallEvent(ident, name, {}, "invalid_arguments")


Sent = tuple[str, tuple[ChatMessage, ...], tuple[ToolSpec, ...], int | None]


class Streaming:
    """A ``ChatStreaming`` fake: one scripted reply per call; records what each call got."""

    def __init__(self, *replies: list[ChatEvent], quote: float = QUOTE) -> None:
        self.model = "fake"
        self.replies = list(replies)
        self.quote_usd = quote
        self.sent: list[Sent] = []
        self.pause = False
        self.paused = asyncio.Event()
        self.closed = 0

    def quote(self, request: ModelRequest) -> UsageQuote:
        raise AssertionError("native turns never quote a completion")

    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise AssertionError("native turns never call complete")

    def quote_chat(
        self,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        max_output_tokens: int | None,
    ) -> UsageQuote:
        return UsageQuote(tokens_in=1000, tokens_out=100, cost_usd=self.quote_usd)

    async def stream_chat(
        self,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        max_output_tokens: int | None,
    ) -> AsyncIterator[ChatEvent]:
        self.sent.append((system, tuple(messages), tuple(tools), max_output_tokens))
        try:
            if self.pause:
                yield PARTIAL
                self.paused.set()
                await asyncio.Event().wait()
            for event in self.replies.pop(0) if self.replies else reply(text="done"):
                yield event
        finally:
            self.closed += 1


def context(repo: Path, adapter: Streaming, *, budget: float = 1.0, **kwargs: Any) -> RunContext:
    return RunContext(
        adapter, Budget(cost_usd=budget, compute_s=120), repository=repo, **kwargs
    )


def pack(*tools: str, **extra: Any) -> dict[str, Any]:
    return {
        "tools": list(tools),
        "prompt_fragment": "Implement and verify the task.",
        "context_policy": {"max_characters": 60000},
        "subagent_rules": [],
        **extra,
    }


def paired(messages: Sequence[ChatMessage]) -> bool:
    """Every tool call is answered right after its message; call ids are unique."""
    ids: set[str] = set()
    index = 0
    while index < len(messages):
        message = messages[index]
        index += 1
        if message.role == "tool":
            return False  # a result that does not follow its call
        expected = [item.id for item in message.tool_calls]
        if len(set(expected)) != len(expected) or ids & set(expected):
            return False
        ids.update(expected)
        answered = [m.tool_call_id for m in messages[index : index + len(expected)]]
        if sorted(answered, key=str) != sorted(expected):
            return False
        index += len(expected)
    return True


# -- converting Pi's context and the reply (unit) -----------------------------------


def test_pi_context_becomes_paired_chat_messages() -> None:
    pi_context = {
        "systemPrompt": "sys",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": '{"task":1}'}], "timestamp": 1},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "never sent"},
                    {"type": "text", "text": "Reading."},
                    {"type": "toolCall", "id": "a", "name": "read", "arguments": {"path": "x"}},
                    {"type": "toolCall", "id": "b", "name": "read", "arguments": {"path": "y"}},
                ],
                "usage": {"input": 0},
                "stopReason": "toolUse",
            },
            {
                "role": "toolResult",
                "toolCallId": "b",
                "toolName": "read",
                "content": [{"type": "text", "text": "Y"}],
                "isError": True,
            },
            {"role": "user", "content": "go on"},
            {
                "role": "toolResult",
                "toolCallId": "gone",
                "toolName": "write",
                "content": [{"type": "text", "text": "late"}],
                "isError": False,
            },
            {"role": "assistant", "content": [], "stopReason": "error"},
            "not a message",
        ],
    }
    messages = _native_messages(pi_context)
    assert messages == [
        ChatMessage("user", '{"task":1}'),
        ChatMessage(
            "assistant",
            "Reading.",
            tool_calls=(
                ToolCallEvent("a", "read", {"path": "x"}),
                ToolCallEvent("b", "read", {"path": "y"}),
            ),
        ),
        ChatMessage("tool", "Y", tool_call_id="b", name="read", is_error=True),
        # Defensive: an unanswered call is answered before the next message...
        ChatMessage("tool", _UNANSWERED, tool_call_id="a", name="read", is_error=True),
        ChatMessage("user", "go on"),
        # ...and a result without an open call becomes a note.
        ChatMessage("user", "[Earlier result of write]\nlate"),
    ]
    assert paired(messages)
    assert _native_messages(pi_context) == messages  # deterministic: caches keep hitting
    assert _native_messages(None) == [] and _native_messages({"messages": "x"}) == []


def test_native_tools_are_the_granted_ones_whole_and_in_order() -> None:
    schema: dict[str, Any] = {"type": "object", "properties": {"path": {"type": "string"}}}
    pi_context = {
        "tools": [
            {"name": "write", "description": "Write a file", "parameters": schema},
            {"name": "bash", "description": "Not granted", "parameters": {"type": "object"}},
            {"name": "read", "description": "d" * 2000, "parameters": {"type": "string"}},
            "not a tool",
            {"name": "write", "description": "duplicate", "parameters": {"type": "object"}},
            {"name": "edit"},
        ]
    }
    assert _native_tools(pi_context, ["read", "write", "edit"]) == (
        ToolSpec("write", "Write a file", schema),
        ToolSpec("read", "d" * 1024, {"type": "object"}),
        ToolSpec("edit", "", {"type": "object"}),
    )
    assert _native_tools({"tools": None}, ["read"]) == () and _native_tools(None, ["read"]) == ()


def test_the_envelope_is_a_bridge_control_with_session_unique_ids() -> None:
    used: set[str] = set()
    usage = UsageEvent(1, 1, 0.0)
    first = ChatResult(
        "  \n",
        (
            call("write", "dup", path="a", content="1"),
            call("write", "dup", path="b", content="2"),
            call("read", "not valid!", path="a"),
        ),
        usage,
        "tool_use",
    )
    envelope = json.loads(_native_envelope(first, used))
    assert envelope == {
        "tool_calls": [
            {"id": "dup", "name": "write", "arguments": {"path": "a", "content": "1"}},
            {"id": "call_1", "name": "write", "arguments": {"path": "b", "content": "2"}},
            {"id": "call_2", "name": "read", "arguments": {"path": "a"}},
        ]
    }
    assert _bridge_control(envelope)
    # A later reply never reuses an id Pi already holds; its text is narration.
    later = ChatResult("Next.", (call("read", "dup", path="b"),), usage, "tool_use")
    assert json.loads(_native_envelope(later, used)) == {
        "text": "Next.",
        "tool_calls": [{"id": "call_3", "name": "read", "arguments": {"path": "b"}}],
    }
    assert _native_envelope(ChatResult("All done.", (), usage, "end_turn"), used) == (
        '{"text":"All done."}'
    )
    assert _native_envelope(ChatResult("", (), usage, "end_turn"), used) == '{"text":""}'


# -- the wire value the bridge receives (inert IPC peer) ------------------------------


PEER_CONTEXT: dict[str, Any] = {
    "systemPrompt": "You are a coding agent.",
    "messages": [{"role": "user", "content": [{"type": "text", "text": '{"task":"t"}'}]}],
    "tools": [
        {
            "name": "read",
            "description": "Read a file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        {"name": "write", "description": "Write a file", "parameters": {"type": "object"}},
        {"name": "bash", "description": "Not granted", "parameters": {"type": "object"}},
    ],
}


async def peer_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ctx: RunContext,
    bound: dict[str, Any],
    pi_context: dict[str, Any] = PEER_CONTEXT,
) -> tuple[list[Any], PiBackend]:
    peer = ProtocolProcess([_READY, {"kind": "model", "id": 1, "context": pi_context}])
    install_protocol_peer(monkeypatch, peer)
    backend = PiBackend()
    with pytest.raises(PiError, match="Invalid Pi protocol"):  # the peer's stream ends
        await backend.run(ctx, {}, bound)
    return [item["value"] for item in peer.sent if item.get("id") == 1], backend


async def test_the_bridge_gets_a_synthesized_envelope_for_native_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = 'TOTAL = re.compile(r"\\$\\s*(\\d+)")\n'  # what broke the envelope live
    adapter = Streaming(reply(call("write", "chatcmpl-tool-1", path="a.py", content=content),
                              text="Writing."))
    ctx = context(tmp_path, adapter)
    [value], backend = await peer_values(
        tmp_path, monkeypatch, ctx, pack("read", "write", max_output_tokens=256)
    )
    assert json.loads(value) == {
        "text": "Writing.",
        "tool_calls": [
            {"id": "chatcmpl-tool-1", "name": "write",
             "arguments": {"path": "a.py", "content": content}}
        ],
    }
    [(system, messages, specs, cap)] = adapter.sent
    assert system == _NATIVE_INSTRUCTION + "\n\nYou are a coding agent."
    assert messages == (ChatMessage("user", '{"task":"t"}'),)
    assert [spec.name for spec in specs] == ["read", "write"]  # granted only, Pi's order
    assert specs[0].parameters["required"] == ["path"] and cap == 256
    # Reserved at the quote, then settled at the usage the provider reported.
    assert ctx.cost_usd == pytest.approx(COST) and (ctx.tokens_in, ctx.tokens_out) == (100, 20)
    evidence = backend.last_evidence
    assert evidence is not None and evidence.transport == "native"
    assert evidence.model_calls == 1 and evidence.repairs == 0


async def test_adapters_without_native_chat_keep_the_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exact = json.dumps({"tool_calls": [{"id": "c1", "name": "read", "arguments": {"path": "a"}}]})
    fake = FakeAdapter(
        responses=[ModelResponse(text=exact, model="fake", tokens_in=1, tokens_out=1, cost_usd=0)]
    )
    assert not isinstance(fake, ChatStreaming)
    ctx = RunContext(fake, Budget(cost_usd=1, compute_s=60), repository=tmp_path)
    [value], backend = await peer_values(tmp_path, monkeypatch, ctx, pack("read"))
    assert value == exact and fake.calls == 1
    assert backend.last_evidence is not None and backend.last_evidence.transport == "envelope"


async def test_a_model_request_without_messages_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = Streaming()
    ctx = context(tmp_path, adapter)
    peer = ProtocolProcess([_READY, {"kind": "model", "id": 1, "context": {"tools": []}}])
    install_protocol_peer(monkeypatch, peer)
    with pytest.raises(PiError, match="Invalid Pi model context"):
        await PiBackend().run(ctx, {}, pack("read"))
    assert adapter.sent == [] and ctx.cost_usd == 0


# -- the real pinned Pi SDK behind native calls ---------------------------------------


async def test_native_tool_calls_round_trip_through_the_real_sdk(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("CONTENT_MARKER\n", encoding="utf-8")
    seen: list[dict[str, Any]] = []
    adapter = Streaming(
        reply(call("read", "chatcmpl-tool-a", path="notes.txt"), text="Reading the notes."),
        reply(call("write", "chatcmpl-tool-b", path="out.txt", content="CONTENT_MARKER\n")),
        reply(text="Copied."),
    )
    backend = PiBackend(on_event=seen.append)
    ctx = context(tmp_path, adapter)
    assert await backend.run(ctx, "copy the notes", pack("read", "write")) == "Copied."
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "CONTENT_MARKER\n"
    system, first, specs, _ = adapter.sent[0]
    assert system.startswith(_NATIVE_INSTRUCTION + "\n\n")
    assert "Implement and verify the task." in system  # Pi's own prompt with the pack
    assert [m.role for m in first] == ["user"] and {s.name for s in specs} == {"read", "write"}
    _, second, _, _ = adapter.sent[1]
    assert second[-2] == ChatMessage(
        "assistant",
        "Reading the notes.",
        tool_calls=(ToolCallEvent("chatcmpl-tool-a", "read", {"path": "notes.txt"}),),
    )
    result = second[-1]
    assert (result.role, result.tool_call_id, result.name) == ("tool", "chatcmpl-tool-a", "read")
    assert "CONTENT_MARKER" in result.text and not result.is_error
    _, third, _, _ = adapter.sent[2]
    assert len(third) == 5 and paired(third)
    assert third[:3] == second  # the conversation only grows: a byte-stable prefix
    evidence = backend.last_evidence
    assert evidence is not None and evidence.transport == "native"
    assert (evidence.model_calls, evidence.tool_calls, evidence.repairs) == (3, 2, 0)
    assert ctx.cost_usd == pytest.approx(3 * COST)
    assert (ctx.tokens_in, ctx.tokens_out) == (300, 60)
    assert {"event": "text", "text": "Reading the notes."} in seen  # narration still shows


async def test_several_calls_in_one_reply_all_run_in_order(tmp_path: Path) -> None:
    adapter = Streaming(
        reply(
            call("write", "same", path="one.txt", content="1"),
            call("write", "same", path="two.txt", content="2"),
            call("read", "not a valid id", path="one.txt"),
        ),
        reply(text="Both written."),
    )
    backend = PiBackend()
    assert await backend.run(context(tmp_path, adapter), {}, pack("read", "write")) == (
        "Both written."
    )
    assert (tmp_path / "one.txt").read_text(encoding="utf-8") == "1"
    assert (tmp_path / "two.txt").read_text(encoding="utf-8") == "2"
    _, after, _, _ = adapter.sent[1]
    assert paired(after)
    assert [item.id for item in after[1].tool_calls] == ["same", "call_1", "call_2"]
    assert [m.tool_call_id for m in after[2:]] == ["same", "call_1", "call_2"]
    assert not any(m.is_error for m in after[2:]) and "1" in after[4].text
    assert backend.last_evidence is not None and backend.last_evidence.tool_calls == 3


async def test_a_text_only_reply_is_the_final_answer(tmp_path: Path) -> None:
    # Unlike an envelope reply, plain text is how a native turn finishes: Forge's
    # ``repair_prose`` never re-asks it.
    adapter = Streaming(reply(text="Nothing to change."))
    backend = PiBackend(PiConfig(repair_prose=True))
    assert await backend.run(context(tmp_path, adapter), {}, pack("read")) == (
        "Nothing to change."
    )
    assert len(adapter.sent) == 1
    bare = Streaming(reply(text="An answer."))
    assert await PiBackend().run(context(tmp_path, bare), {}, pack()) == "An answer."
    system, _, specs, _ = bare.sent[0]
    assert specs == () and system.startswith(_NATIVE_NO_TOOLS)


async def test_malformed_arguments_are_re_asked_once_then_the_calls_run(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("A", encoding="utf-8")
    adapter = Streaming(
        reply(call("read", "r1", path="a.txt"), malformed("write", "w1")),
        reply(call("read", "r1", path="a.txt"), call("write", "w1", path="b.txt", content="B")),
        reply(text="done"),
    )
    backend = PiBackend()
    ctx = context(tmp_path, adapter)
    assert await backend.run(ctx, {}, pack("read", "write")) == "done"
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "B"
    evidence = backend.last_evidence
    assert evidence is not None and evidence.repairs == 1
    # Nothing in the rejected reply ran, not even its valid read.
    assert (evidence.model_calls, evidence.tool_calls) == (3, 2)
    _, first, _, _ = adapter.sent[0]
    _, repair, _, _ = adapter.sent[1]
    assert repair[: len(first)] == first and paired(repair)
    shown, *results = repair[len(first) :]
    assert [(c.id, c.name, c.arguments) for c in shown.tool_calls] == [
        ("r1", "read", {"path": "a.txt"}),
        ("w1", "write", {}),  # the malformed arguments are never sent back
    ]
    assert [(m.tool_call_id, m.text, m.is_error) for m in results] == [
        ("r1", _NOT_RUN, True),
        ("w1", _BAD_ARGUMENTS, True),
    ]
    # Pi's own context never holds the rejected attempt.
    _, after, _, _ = adapter.sent[2]
    assert [m.role for m in after] == ["user", "assistant", "tool", "tool"] and paired(after)
    # The repair is a metered call like any other.
    assert ctx.cost_usd == pytest.approx(3 * COST)


async def test_still_malformed_after_one_repair_the_reply_is_final(tmp_path: Path) -> None:
    adapter = Streaming(
        reply(malformed("write"), stop="max_tokens"),
        reply(malformed("write"), text="I could not finish.", stop="max_tokens"),
        reply(text="never asked"),
    )
    backend = PiBackend(PiConfig(repair_prose=True))
    assert await backend.run(context(tmp_path, adapter), {}, pack("write")) == (
        "I could not finish."
    )
    assert len(adapter.sent) == 2
    assert not await asyncio.to_thread(lambda: list(tmp_path.iterdir()))
    _, first, _, _ = adapter.sent[0]
    _, repair, _, _ = adapter.sent[1]
    # Cut at the output limit: the reply is never sent back, only a short instruction.
    assert repair == (*first, ChatMessage("user", _CUT_REPLY))
    evidence = backend.last_evidence
    assert evidence is not None and (evidence.repairs, evidence.tool_calls) == (1, 0)


async def test_native_calls_and_repairs_count_against_the_turn_limit(tmp_path: Path) -> None:
    adapter = Streaming(reply(malformed("write")), reply(call("write", path="a", content="a")))
    with pytest.raises(PiError, match="turn limit"):
        await PiBackend(PiConfig(max_turns=1)).run(context(tmp_path, adapter), {}, pack("write"))
    assert len(adapter.sent) == 1 and not (tmp_path / "a").exists()
    many = Streaming(reply(*(call("read", f"c{i}", path="x") for i in range(3))))
    with pytest.raises(PiError, match="tool limit"):
        await PiBackend(PiConfig(max_turns=2)).run(context(tmp_path, many), {}, pack("read"))


async def test_trusted_capabilities_are_offered_as_native_tools(tmp_path: Path) -> None:
    seen: list[Any] = []

    async def check(arguments: dict[str, Any]) -> Any:
        seen.append(arguments)
        return {"ok": True, "code": None}

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "additionalProperties": False,
    }
    tool = TrustedTool(
        name="forge_check",
        description="Validate the draft harness.",
        parameters=schema,
        callback=check,
        max_calls=5,
    )
    backend = PiBackend(
        PiConfig(allowed_tools=("read", "forge_check")), capabilities={"forge_check": tool}
    )
    adapter = Streaming(reply(call("forge_check", path="harness.py")), reply(text="checked"))
    ctx = context(tmp_path, adapter, is_evaluation=False, is_build=True)
    assert await backend.run(ctx, {}, pack("read", "forge_check")) == "checked"
    assert seen == [{"path": "harness.py"}]
    _, _, specs, _ = adapter.sent[0]
    assert ToolSpec("forge_check", "Validate the draft harness.", schema) in specs
    _, after, _, _ = adapter.sent[1]
    assert json.loads(after[-1].text) == {"ok": True, "code": None}


# -- budgets and cancellation ---------------------------------------------------------


async def test_a_call_over_budget_is_refused_before_any_stream_or_tool(tmp_path: Path) -> None:
    adapter = Streaming(reply(call("write", path="bad", content="no")), quote=0.2)
    backend = PiBackend()
    ctx = context(tmp_path, adapter, budget=0.1)
    with pytest.raises(BudgetExceeded, match="exceed run budget"):
        await backend.run(ctx, {}, pack("write"))
    assert adapter.sent == [] and ctx.cost_usd == 0 and not (tmp_path / "bad").exists()
    assert backend.last_evidence is not None and backend.last_evidence.tool_calls == 0
    # A compile wallet refuses the reservation the same way.
    walleted = Streaming(reply(call("write", path="bad", content="no")))
    with compilation_wallet(Budget(cost_usd=QUOTE / 2, compute_s=60)) as wallet:
        with pytest.raises(BudgetExceeded, match="Compilation budget exhausted"):
            await PiBackend().run(context(tmp_path, walleted), {}, pack("write"))
    assert walleted.sent == [] and wallet.cost_usd == 0


async def test_usage_above_the_quote_fails_before_any_tool_runs(tmp_path: Path) -> None:
    adapter = Streaming(
        reply(call("write", path="bad", content="no"), usage=UsageEvent(100, 20, 2 * QUOTE))
    )
    with pytest.raises(BudgetExceeded, match="reserved usage"):
        await PiBackend().run(context(tmp_path, adapter), {}, pack("write"))
    assert not (tmp_path / "bad").exists()


async def test_cancelling_a_native_turn_keeps_its_quote_and_stops_pi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned: list[asyncio.subprocess.Process] = []
    spawn = asyncio.create_subprocess_exec

    async def spy(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await spawn(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr("architect.backends.pi.asyncio.create_subprocess_exec", spy)
    adapter = Streaming()
    adapter.pause = True
    ctx = context(tmp_path, adapter)
    with compilation_wallet(Budget(cost_usd=1, compute_s=120)) as wallet:
        task = asyncio.create_task(PiBackend().run(ctx, {}, pack("read")))
        await asyncio.wait_for(adapter.paused.wait(), 60)
        # In flight, the whole quote is reserved.
        assert ctx.cost_usd == pytest.approx(QUOTE) and wallet.cost_usd == pytest.approx(QUOTE)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    # The stream reported PARTIAL first (as Anthropic's message_start does), but no final
    # usage: the reply it was writing is not in PARTIAL, so the quote stays, as it does
    # for a cancelled envelope call.
    assert ctx.cost_usd == pytest.approx(QUOTE) and wallet.cost_usd == pytest.approx(QUOTE)
    assert (ctx.tokens_in, ctx.tokens_out) == (1000, 100)
    assert adapter.closed == 1
    assert spawned and spawned[0].returncode is not None  # the bridge is gone
    with pytest.raises(BudgetExceeded, match="cancelled"):
        ctx._check()


# -- Forge's meter, the coder and build mode ------------------------------------------


async def test_forge_meter_forwards_native_chats_with_metering_intact() -> None:
    plain = _metered(FakeAdapter())
    assert type(plain) is _Metered and not isinstance(plain, ChatStreaming)
    inner = Streaming(reply(call("read", path="a"), text="t"))
    agent = _metered(inner)  # type: ignore[arg-type]
    assert isinstance(agent, ChatStreaming) and agent.model == "fake"
    asked = [ChatMessage("user", "u")]
    assert agent.quote_chat("s", asked, (), None).cost_usd == QUOTE
    events = [event async for event in agent.stream_chat("s", asked, (), 64)]
    assert events == reply(call("read", path="a"), text="t")
    assert (agent.tokens_in, agent.tokens_out, agent.cost_usd) == (100, 20, COST)
    assert inner.sent[0][3] == 64
    # A stream closed before its final usage is metered at its quote, marked estimated,
    # as the runtime and the wallets keep it: PARTIAL leaves out the reply.
    inner.pause = True
    stream = agent.stream_chat("s", asked, (), None)
    assert await anext(stream) == PARTIAL
    await stream.aclose()
    assert (agent.tokens_in, agent.tokens_out) == (1100, 120)
    assert agent.cost_usd == pytest.approx(COST + QUOTE)
    assert agent.turns[-1].estimated and not agent.turns[-1].ok
    assert inner.closed == 2


@pytest.fixture
def services(tmp_path: Path):  # type: ignore[no-untyped-def]
    profile = load_profile(fake_profile(tmp_path))
    built = build_services(profile, profile.workspace)
    yield built
    built.close()


def tool_context(  # type: ignore[no-untyped-def]
    services, tmp_path: Path, adapter: object, approvals: list[Any]
) -> ToolContext:
    wired = dataclasses.replace(services, adapter_factory=lambda model: adapter)
    cwd = tmp_path / "project"
    cwd.mkdir(exist_ok=True)
    ui = RecordingUI(approvals=approvals)
    ctx = ToolContext(services=wired, cwd=cwd, ui=ui, model="fake", permission="ask")
    ctx.extras["coder"] = run_coder
    return ctx


async def test_the_coder_drives_pi_with_native_calls_through_the_session_meter(  # type: ignore[no-untyped-def]
    services, tmp_path: Path
) -> None:
    adapter = Streaming(
        reply(call("write", path="hello.txt", content="hello\n"), text="Writing it."),
        reply(text="Wrote hello.txt."),
    )
    meter = Meter()
    ctx = tool_context(services, tmp_path, ObservedAdapter(adapter, meter), ["yes"])  # type: ignore[arg-type]
    result = await run_coder("write hello.txt", ctx)
    assert (ctx.cwd / "hello.txt").read_text(encoding="utf-8") == "hello\n"
    assert isinstance(result, dict) and result["ok"] is True
    assert result["summary"] == "Wrote hello.txt." and result["changed_files"] == ["hello.txt"]
    assert result["cost_usd"] == pytest.approx(2 * COST)
    # Native: Pi's tools went to the model as definitions, and the meter saw both calls.
    assert len(adapter.sent) == 2
    assert {"read", "write", "edit", "bash"} <= {spec.name for spec in adapter.sent[0][2]}
    assert meter.calls == 2 and meter.spend.cost_usd == pytest.approx(2 * COST)


def test_build_mode_hands_forge_a_streaming_conductor_model(  # type: ignore[no-untyped-def]
    services, tmp_path: Path
) -> None:
    assert services.forge_adapter is None and services.compiler.forge_adapter is None
    observed = ObservedAdapter(Streaming(), Meter())  # type: ignore[arg-type]
    agent = _forge_agent(tool_context(services, tmp_path, observed, []))
    assert isinstance(agent, ChatStreaming) and agent.model == "fake"
    plain = FakeAdapter()
    assert _forge_agent(tool_context(services, tmp_path, plain, [])) is plain
