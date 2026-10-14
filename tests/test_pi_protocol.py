"""The guarded model protocol: explicit envelope prompt, tolerant parsing, bounded repairs.

A live model once answered a Forge session with just ``read`` (the tool's name) and the
session ended with zero tool calls. These fake-adapter tests pin the parent-side fix.
Nothing here calls a live provider, a CLI agent or Ollama.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_pi import _READY, ProtocolProcess, install_protocol_peer

from architect.backends.pi import (
    _MAX_REPAIRS,
    _REMINDER,
    _REPAIR_MESSAGE,
    _TOOL_LISTING_CHARS,
    PiBackend,
    PiConfig,
    PiError,
    _control_reply,
    _model_view,
    _tool_listing,
)
from architect.models import FakeAdapter, ModelRequest, ModelResponse
from architect.runtime import BudgetExceeded, RunContext
from architect.spec import Budget


def response(text: str, cost: float = 0) -> ModelResponse:
    return ModelResponse(text=text, model="fake", tokens_in=10, tokens_out=10, cost_usd=cost)


def calls(name: str, **args: object) -> str:
    return json.dumps({"tool_calls": [{"id": "call1", "name": name, "arguments": args}]})


class Recording(FakeAdapter):
    def __init__(self, texts: list[str], cost: float = 0) -> None:
        super().__init__(responses=[response(text, cost) for text in texts])
        self.prompts: list[str] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.prompts.append(request.prompt)
        return await super().complete(request)


def context(repo: Path, texts: list[str], *, cost: float = 0, budget: float = 1) -> RunContext:
    return RunContext(
        Recording(texts, cost), Budget(cost_usd=budget, compute_s=60), repository=repo
    )


def pack(*tools: str) -> dict[str, Any]:
    return {
        "tools": list(tools),
        "prompt_fragment": "Implement and verify the task.",
        "context_policy": {"max_characters": 2048},
        "subagent_rules": [],
    }


def prompts(ctx: RunContext) -> list[dict[str, Any]]:
    assert isinstance(ctx.adapter, Recording)
    return [json.loads(prompt) for prompt in ctx.adapter.prompts]


# -- parsing (unit) ------------------------------------------------------------


ENVELOPE = calls("write", path="a.txt", content="x")
COMPACT = json.dumps(json.loads(ENVELOPE), separators=(",", ":"))


@pytest.mark.parametrize(
    "reply",
    [
        ENVELOPE,
        '{"text": "final"}',
        '  {"tool_calls": [{"id": "c", "name": "read", "arguments": {}}], "text": ""}\n',
        # The bridge's own checks still decide these exact envelopes (as before).
        '{"tool_calls": "not a list"}',
        '{"tool_calls": [{"name": "read", "arguments": {}}]}',
    ],
)
def test_exact_envelopes_pass_through_byte_identically(reply: str) -> None:
    control = _control_reply(reply, 64)
    assert control is not None and control[0] == reply


@pytest.mark.parametrize(
    "reply",
    [
        f"```json\n{ENVELOPE}\n```",
        f"```\n{ENVELOPE}\n```",
        f"I'll write the file now.\n{ENVELOPE}",
        f"{ENVELOPE}\nThat should do it.",
        f'Plan: {{not json}} then {{"ok": true}} and finally ```json\n{ENVELOPE}\n```',
    ],
)
def test_fenced_or_prose_wrapped_envelope_is_normalized(reply: str) -> None:
    assert _control_reply(reply, 64) == (COMPACT, True)


def test_wrapped_final_text_envelope_is_normalized() -> None:
    assert _control_reply('Done.\n```json\n{"text": "all good"}\n```', 64) == (
        '{"text":"all good"}',
        False,
    )


@pytest.mark.parametrize(
    "reply",
    [
        "read",
        "",
        "I will read the file.",
        '{"ok": true}',
        '{"text": "ordinary", "ok": true}',
        # Only top-level objects: an envelope nested in other JSON is task output.
        '{"result": {"text": "nested"}}',
        'prefix {"result": {"tool_calls": []}}',
        # Wrapped candidates must be fully valid: bad ids, shapes, limits, constants.
        'x {"tool_calls": [{"name": "read", "arguments": {}}]}',
        'x {"tool_calls": [{"id": "a", "name": "read", "arguments": []}]}',
        'x {"tool_calls": [{"id": "a", "name": "r", "arguments": {}}, '
        '{"id": "a", "name": "r", "arguments": {}}]}',
        'x {"text": 5}',
        'x {"text": "a", "extra": 1}',
        'x {"tool_calls": [{"id": "a", "name": "r", "arguments": {"n": NaN}}]}',
    ],
)
def test_non_envelopes_are_not_control(reply: str) -> None:
    assert _control_reply(reply, 64) is None


def test_wrapped_envelope_respects_the_call_limit() -> None:
    many = json.dumps(
        {"tool_calls": [{"id": f"c{i}", "name": "read", "arguments": {}} for i in range(3)]}
    )
    assert _control_reply(f"go {many}", 2) is None
    assert _control_reply(f"go {many}", 3) is not None


def test_tool_listing_is_granted_ordered_and_bounded() -> None:
    huge = {"type": "object", "description": "x" * (_TOOL_LISTING_CHARS * 2)}
    pi_context = {
        "tools": [
            {"name": "write", "description": "Write a file", "parameters": {"type": "object"}},
            {"name": "bash", "description": "Not granted", "parameters": {}},
            {"name": "read", "description": "d" * 5000, "parameters": huge},
            "not a tool",
            {"name": "write", "description": "duplicate"},
        ]
    }
    listing = _tool_listing(pi_context, ["read", "write"])
    assert listing == [
        {"name": "write", "description": "Write a file", "parameters": {"type": "object"}},
        {"name": "read", "description": "d" * 300},
    ]
    assert _tool_listing({"tools": None}, ["read"]) == []
    assert _tool_listing(None, ["read"]) == []


# -- the wire value the bridge receives (inert IPC peer) ----------------------


PEER_TOOLS = [
    {
        "name": "read",
        "description": "Read a file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    }
]


def model_request(tools: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "kind": "model",
        "id": 1,
        "context": {"systemPrompt": "s", "messages": [], "tools": tools},
    }


async def peer_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, texts: list[str], *, tools: bool = True
) -> tuple[Any, RunContext]:
    peer = ProtocolProcess([_READY, model_request(PEER_TOOLS if tools else [])])
    install_protocol_peer(monkeypatch, peer)
    ctx = context(tmp_path, texts)
    with pytest.raises(PiError, match="Invalid Pi protocol"):
        await PiBackend().run(ctx, {}, pack("read"))
    replies = [item for item in peer.sent if item.get("id") == 1]
    assert len(replies) == 1
    return replies[0]["value"], ctx


async def test_exact_envelope_reaches_bridge_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exact = calls("read", path="a.txt")
    value, ctx = await peer_value(tmp_path, monkeypatch, [exact])
    assert value == exact and ctx.adapter.calls == 1


async def test_fenced_envelope_reaches_bridge_normalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value, ctx = await peer_value(tmp_path, monkeypatch, [f"```json\n{ENVELOPE}\n```"])
    assert value == COMPACT and ctx.adapter.calls == 1


async def test_bare_tool_name_is_repaired_then_final_after_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value, ctx = await peer_value(tmp_path, monkeypatch, ["read"] * 3)
    assert ctx.adapter.calls == 1 + _MAX_REPAIRS
    assert value == json.dumps({"text": "read"})


async def test_no_tools_means_no_repair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value, ctx = await peer_value(tmp_path, monkeypatch, ["read"], tools=False)
    assert ctx.adapter.calls == 1 and value == json.dumps({"text": "read"})


# -- the real pinned Pi SDK behind the parent ---------------------------------


async def test_prompt_states_the_protocol_and_lists_granted_tools(tmp_path: Path) -> None:
    ctx = context(tmp_path, ['{"text": "done"}'])
    assert await PiBackend().run(ctx, {}, pack("read", "write")) == "done"
    (first,) = prompts(ctx)
    assert list(first) == ["instruction", "tools", "pi_context", "reminder"]
    assert first["reminder"] == _REMINDER  # last, after the growing context
    # The context carries the system prompt and trimmed messages; tools are listed once.
    assert set(first["pi_context"]) <= {"systemPrompt", "messages"}
    for message in first["pi_context"]["messages"]:
        assert set(message) <= {"role", "content", "toolCallId", "toolName", "isError"}
    assert "EVERY reply must be exactly one JSON object" in first["instruction"]
    assert '{"text":"<final answer>"}' in first["instruction"]
    assert '"tool_calls"' in first["instruction"]
    listed = {item["name"]: item for item in first["tools"]}
    assert set(listed) == {"read", "write"}  # granted only; edit is not in the pack
    assert listed["read"]["parameters"]["type"] == "object"


async def test_read_style_reply_is_repaired_and_the_tool_call_proceeds(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("CONTENT_MARKER", encoding="utf-8")
    ctx = context(tmp_path, ["read", calls("read", path="a.txt"), "done"])
    backend = PiBackend()
    assert await backend.run(ctx, {}, pack("read")) == "done"
    evidence = backend.last_evidence
    assert evidence is not None
    assert evidence.model_calls == 3 and evidence.tool_calls == 1
    assert ctx.tokens_in == 30 and ctx.tokens_out == 30
    first, repair, after = prompts(ctx)
    assert "correction" not in first and "previous_reply" not in first
    assert repair["previous_reply"] == "read" and repair["correction"] == _REPAIR_MESSAGE
    assert repair["pi_context"] == first["pi_context"]
    # The cache-friendly prefix is byte-stable across every call in the session.
    raw = ctx.adapter.prompts  # type: ignore[attr-defined]
    prefix = raw[0][: raw[0].index('"pi_context"')]
    assert all(prompt.startswith(prefix) for prompt in raw)
    assert "correction" not in after
    assert "CONTENT_MARKER" in json.dumps(after["pi_context"])


async def test_fenced_and_prose_wrapped_tool_calls_execute(tmp_path: Path) -> None:
    ctx = context(
        tmp_path,
        [
            "```json\n" + calls("write", path="one.txt", content="1") + "\n```",
            "Now the second file:\n" + calls("write", path="two.txt", content="2") + "\nDone.",
            "finished",
        ],
    )
    backend = PiBackend()
    assert await backend.run(ctx, {}, pack("write")) == "finished"
    assert (tmp_path / "one.txt").read_text() == "1"
    assert (tmp_path / "two.txt").read_text() == "2"
    assert backend.last_evidence is not None and backend.last_evidence.model_calls == 3


async def test_repairs_are_bounded_and_the_last_reply_is_final(tmp_path: Path) -> None:
    ctx = context(tmp_path, ["read", "read it", "use read"])
    backend = PiBackend()
    assert await backend.run(ctx, {}, pack("read")) == "use read"
    assert ctx.adapter.calls == 3  # the original call plus two repairs, never a fourth
    assert backend.last_evidence is not None and backend.last_evidence.model_calls == 3
    assert backend.last_evidence.tool_calls == 0


async def test_repairs_count_against_the_turn_limit(tmp_path: Path) -> None:
    ctx = context(tmp_path, ["read", "read", "read"])
    backend = PiBackend(PiConfig(max_turns=2))
    with pytest.raises(PiError, match="turn limit"):
        await backend.run(ctx, {}, pack("read"))
    assert ctx.adapter.calls == 2


async def test_repairs_are_charged_to_the_wallet(tmp_path: Path) -> None:
    ctx = context(
        tmp_path, ["read", calls("write", path="bad", content="no")], cost=0.06, budget=0.1
    )
    with pytest.raises(BudgetExceeded):
        await PiBackend().run(ctx, {}, pack("read", "write"))
    assert not (tmp_path / "bad").exists()
    assert ctx.cost_usd <= 0.1


async def test_envelope_key_in_prose_after_tool_use_is_repaired(tmp_path: Path) -> None:
    ctx = context(
        tmp_path,
        [
            calls("write", path="a.txt", content="a"),
            "tool_calls: write b.txt",
            calls("write", path="b.txt", content="b"),
            "done",
        ],
    )
    backend = PiBackend()
    assert await backend.run(ctx, {}, pack("write")) == "done"
    assert (tmp_path / "b.txt").read_text() == "b"
    assert backend.last_evidence is not None and backend.last_evidence.model_calls == 4


async def test_plain_prose_final_answers_are_not_repaired(tmp_path: Path) -> None:
    # A first-turn answer that names no tool, and a final after tool use, both end at once.
    first = context(tmp_path, ["A useful result."])
    assert await PiBackend().run(first, {}, pack("read")) == "A useful result."
    assert first.adapter.calls == 1
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    after = context(tmp_path, [calls("read", path="a.txt"), "I read it."])
    assert await PiBackend().run(after, {}, pack("read")) == "I read it."
    assert after.adapter.calls == 2


# -- Forge sets repair_prose: stray prose never ends unfinished work ----------


async def test_repair_prose_re_asks_a_prose_reply_mid_session(tmp_path: Path) -> None:
    # The live failure: after two reads the model answered "I don't see a specific
    # request yet" and the Forge session ended with no submission.
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    ctx = context(
        tmp_path,
        [
            calls("read", path="a.txt"),
            "I don't see a specific request yet. What would you like me to help with?",
            calls("write", path="b.txt", content="b"),
            '{"text": "done"}',
        ],
    )
    backend = PiBackend(PiConfig(repair_prose=True))
    assert await backend.run(ctx, {}, pack("read", "write")) == "done"
    assert (tmp_path / "b.txt").read_text() == "b"
    assert backend.last_evidence is not None and backend.last_evidence.model_calls == 4
    _, stray, repair, _ = prompts(ctx)
    assert stray["reminder"] == _REMINDER and "correction" not in stray
    assert "reminder" not in repair and repair["correction"] == _REPAIR_MESSAGE
    assert repair["previous_reply"].startswith("I don't see a specific request")


async def test_repair_prose_is_bounded_and_off_by_default(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    ctx = context(tmp_path, [calls("read", path="a.txt"), "prose", "prose", "last prose"])
    backend = PiBackend(PiConfig(repair_prose=True))
    assert await backend.run(ctx, {}, pack("read")) == "last prose"
    assert ctx.adapter.calls == 2 + _MAX_REPAIRS
    default = context(tmp_path, [calls("read", path="a.txt"), "prose"])
    assert await PiBackend().run(default, {}, pack("read")) == "prose"
    assert default.adapter.calls == 2
    assert "repair_prose" not in PiConfig().model_dump(exclude={"repair_prose"})


def test_model_view_drops_bookkeeping_and_is_deterministic() -> None:
    context_value = {
        "systemPrompt": "sys",
        "tools": [{"name": "read"}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "go"}], "timestamp": 1},
            {
                "role": "assistant",
                "content": [{"type": "toolCall", "id": "c", "name": "read", "arguments": {}}],
                "api": "architect-rpc",
                "usage": {"input": 0},
                "stopReason": "toolUse",
            },
            {"role": "toolResult", "toolCallId": "c", "toolName": "read", "isError": False,
             "content": [{"type": "text", "text": "x"}], "timestamp": 2},
        ],
    }
    view = _model_view(context_value)
    assert view == {
        "systemPrompt": "sys",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "go"}]},
            {
                "role": "assistant",
                "content": [{"type": "toolCall", "id": "c", "name": "read", "arguments": {}}],
            },
            {"role": "toolResult", "content": [{"type": "text", "text": "x"}],
             "toolCallId": "c", "toolName": "read", "isError": False},
        ],
    }
    assert json.dumps(view) == json.dumps(_model_view(context_value))
    assert _model_view(None) is None
