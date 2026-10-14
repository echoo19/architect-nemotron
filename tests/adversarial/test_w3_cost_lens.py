"""W3 review, second pass: cost controls and the Forge pack (lens 2).

Lens 1 (held-out data on the new paths) found nothing new beyond ``test_w3_review``.
Each test here pinned a defect found in this pass. All are fixed, so they are regression
tests now:

1. A cancelled native turn of an agent whose stream reports usage early (Anthropic's
   ``message_start``) was settled at that partial usage: input plus one output token,
   though the provider already wrote part of the reply. A stream now keeps its quote
   until its final usage, in the wallets, the run, the Forge meter and the turn row.
2. The parent-side context cap counted tool-result bodies and write/edit texts only, so
   ``forge_dryrun`` replies and ``forge_spawn`` cases stayed in every later prompt. Their
   bulky arguments now count and are trimmed whole (``_BULKY_ARGUMENTS``).
3. The pack hash missed fixed texts the engineer reads: the read-refusal listing words
   (now in ``PARENT_TEXTS``) and the dry run's schema-shaped default values (now in the
   ``prompt/tool_texts`` part).
4. ``max_rungs`` only decides on which rungs Forge runs, like ``trigger``, yet it was
   hashed, so adding it to a profile changed a pre-registered pack hash. It is left out
   of the hash now, with ``enabled`` and ``trigger``.
5. The recorded ``pack_sha256`` was computed when the session ended, from the files on
   disk then. It is computed once, before the session starts.
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
from forge_support import ensure_sandbox, forge_compiler, spec
from test_pi_native import QUOTE, Streaming

from architect.backends.pi import _ContextCap, _native_messages, _read_refusal
from architect.compiler import forge as forge_module
from architect.compiler import forge_example
from architect.compiler.forge import _metered
from architect.compiler.forge_pack import pack_hash, pack_parts
from architect.compiler.forge_settings import ForgeSettings
from architect.models.chat import (
    ChatEvent,
    ChatMessage,
    TextDeltaEvent,
    ToolSpec,
    UsageEvent,
)
from architect.models.http import AnthropicAdapter
from architect.runtime import RunContext
from architect.runtime import wallet as wallet_module
from architect.runtime.wallet import compilation_wallet
from architect.spec import Budget
from architect.telemetry import categorical

ROOT = Path(__file__).parents[2]
PI_READY = (
    ROOT / "architect/backends/pi-node/node_modules/@mariozechner/pi-coding-agent/package.json"
).is_file()
needs_pi = pytest.mark.skipif(not PI_READY, reason="Pi dependencies are not installed")

ANTHROPIC = "claude-opus-5-5"


# -- 1. a cancelled stream that reported partial usage ------------------------------


def _event(kind: str, body: dict[str, Any]) -> bytes:
    return f"event: {kind}\ndata: {json.dumps(body)}\n\n".encode()


async def test_a_cancelled_anthropic_turn_keeps_its_quote() -> None:
    streamed = asyncio.Event()

    async def body() -> AsyncIterator[bytes]:
        start = {"model": ANTHROPIC, "usage": {"input_tokens": 3000, "output_tokens": 1}}
        yield _event("message_start", {"type": "message_start", "message": start})
        block = {"type": "text", "text": ""}
        opened = {"type": "content_block_start", "index": 0, "content_block": block}
        yield _event("content_block_start", opened)
        for _ in range(200):  # about 2,000 words of reply, written and billed
            text = {"type": "text_delta", "text": "word " * 10}
            yield _event(
                "content_block_delta", {"type": "content_block_delta", "index": 0, "delta": text}
            )
        streamed.set()
        await asyncio.Event().wait()  # the Forge deadline arrives here

    inner = AnthropicAdapter(
        ANTHROPIC,
        api_key="k",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body())),
    )
    agent = _metered(inner)  # the Forge agent's meter: native, since Anthropic streams
    context = RunContext(agent, Budget(cost_usd=5, compute_s=120))
    messages = [ChatMessage("user", "Write harness.py.")]
    quote = inner.quote_chat("s", messages, (), 12288)
    with compilation_wallet(Budget(cost_usd=5, compute_s=120)) as wallet:
        task = asyncio.create_task(context.stream_call("s", messages, (), 12288))
        await streamed.wait()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    [row] = agent.turns
    # It was $0.01202 (3,000 input and 1 output token) everywhere against a $0.328 quote,
    # and the row was not marked estimated. A cancelled envelope call keeps its quote.
    assert wallet.cost_usd >= quote.cost_usd
    assert context.cost_usd >= quote.cost_usd
    assert agent.cost_usd >= quote.cost_usd and row.cost_usd >= quote.cost_usd


class _PartialThenHang(Streaming):
    """A native agent that reports partial usage first (as Anthropic's ``message_start``
    does), streams some text and never finishes: the Forge deadline cancels it."""

    async def stream_chat(
        self,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        max_output_tokens: int | None,
    ) -> AsyncIterator[ChatEvent]:
        self.sent.append((system, tuple(messages), tuple(tools), max_output_tokens))
        try:
            yield UsageEvent(1000, 1, QUOTE / 20)
            yield TextDeltaEvent("Planning the harness")
            await asyncio.Event().wait()
        finally:
            self.closed += 1


@needs_pi
async def test_a_forge_turn_cut_off_after_partial_usage_keeps_its_quote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await ensure_sandbox(monkeypatch, tmp_path / "probe")
    wallets: list[wallet_module.CompileWallet] = []
    real = forge_module.compilation_wallet

    @contextmanager
    def spy(budget: Budget) -> Iterator[wallet_module.CompileWallet]:
        with real(budget) as opened:
            wallets.append(opened)
            yield opened

    monkeypatch.setattr(forge_module, "compilation_wallet", spy)
    seen: list[Any] = []
    agent = _PartialThenHang()
    compiler = forge_compiler(
        tmp_path,
        settings=ForgeSettings(enabled=True, max_s=10.0),
        agent=agent,  # type: ignore[arg-type]
    )
    compiler.forge_listener = seen.append
    await compiler.compile(spec())
    [result] = seen
    assert result.failures == ("forge_no_submission",) and len(agent.sent) == 1
    label = categorical("forge_agent")
    [run] = [row for row in compiler.runtime.store.rows() if row.preset_origin == label]
    [forge_wallet] = wallets
    # Each said $0.0005, a twentieth of the reserved $0.01.
    assert run.cost_usd >= QUOTE and forge_wallet.cost_usd >= QUOTE
    assert result.agent_cost_usd >= QUOTE


# -- 2. call arguments the context cap never counts ---------------------------------


def _session(name: str, arguments: dict[str, Any], turns: int) -> dict[str, Any]:
    """A Pi context of ``turns`` calls of ``name`` with ``arguments``, each answered by a
    short result, then a final ``read`` turn (the latest turn is never thinned)."""
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": '{"task":"t"}'}]}
    ]
    calls = [(name, arguments)] * turns + [("read", {"path": "SPEC.md"})]
    for index, (tool, values) in enumerate(calls):
        messages.append(
            {
                "role": "assistant",
                "content": [
                    {"type": "toolCall", "id": f"c{index}", "name": tool, "arguments": values}
                ],
            }
        )
        messages.append(
            {
                "role": "toolResult",
                "toolCallId": f"c{index}",
                "toolName": tool,
                "content": [{"type": "text", "text": '{"ok": true}'}],
                "isError": False,
            }
        )
    return {"systemPrompt": "s", "messages": messages, "tools": []}


_AUTHORED = [{"input": {"receipt": "r" * 600}, "expected": {"total": 1}}] * 64


@pytest.mark.parametrize(
    ("name", "arguments", "turns"),
    [
        # forge_dryrun takes up to 8 replies of 2,000 characters, and 100 dry runs.
        ("forge_dryrun", {"case": 0, "replies": ["y" * 2000] * 8}, 20),
        # forge_spawn takes 8 to 64 authored cases of any size, and 3 spawns.
        (
            "forge_spawn",
            {
                "name": "child",
                "goal": "g" * 2000,
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
                "cases": {"authored": _AUTHORED},
            },
            3,
        ),
    ],
    ids=["dryrun_replies", "spawn_cases"],
)
def test_the_parent_context_cap_bounds_every_call_argument(
    name: str, arguments: dict[str, Any], turns: int
) -> None:
    limit = ForgeSettings().context_chars
    view = _ContextCap(limit).apply(_session(name, arguments, turns))
    sent = sum(
        len(message.text) + sum(len(json.dumps(call.arguments)) for call in message.tool_calls)
        for message in _native_messages(view)
    )
    # It was 320,000+ (dry runs) and 131,000+ (spawns) characters, resent every turn.
    assert sent <= limit


# -- 3 and 4. the pack hash --------------------------------------------------------


def _covered(text: str) -> bool:
    parts = "\n".join(pack_parts(ForgeSettings()).values())
    return text in parts or json.dumps(text)[1:-1] in parts


def test_the_pack_covers_the_listing_and_dry_run_default_texts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path.resolve()
    (repo / "harness.py").write_text("x", encoding="utf-8")
    (repo / "drafts").mkdir()
    shown = "\n".join(
        _read_refusal(repo, raw, ()) or "" for raw in (".", "drafts", "missing.py")
    )
    words = ["The working directory", "holds:", "is empty."]
    assert all(word in shown for word in words)  # what the engineer reads after a read
    assert [word for word in words if not _covered(word)] == []
    before = pack_hash(ForgeSettings())
    monkeypatch.setitem(forge_example._FORMATS, "date", "1999-12-31")  # the dry run's reply
    assert pack_hash(ForgeSettings()) != before


def test_max_rungs_is_a_switch_like_trigger() -> None:
    # The module's own rule leaves out "the switches that decide whether Forge runs".
    assert pack_hash(ForgeSettings(trigger="always")) == pack_hash(ForgeSettings())
    assert pack_hash(ForgeSettings(max_rungs=1)) == pack_hash(ForgeSettings())


class _EditsTheBridge(Streaming):
    """An agent whose first turn coincides with an edit of the bridge files on disk (a
    merge into the checkout a long run is using), then ends the session."""

    def __init__(self, extension: Path) -> None:
        super().__init__()
        self.extension = extension

    async def stream_chat(
        self,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        max_output_tokens: int | None,
    ) -> AsyncIterator[ChatEvent]:
        if not self.sent:
            with self.extension.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write("// merged while the session ran\n")
        async for event in super().stream_chat(system, messages, tools, max_output_tokens):
            yield event


@needs_pi
async def test_the_recorded_pack_hash_is_the_one_the_session_started_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from architect.compiler import forge_pack

    await ensure_sandbox(monkeypatch, tmp_path / "probe")
    node = tmp_path / "pi-node"
    node.mkdir()
    for name in ("bridge.mjs", "extension.ts"):
        (node / name).write_bytes((forge_pack._PI_NODE / name).read_bytes())
    monkeypatch.setattr(forge_pack, "_PI_NODE", node)  # what pack_hash reads; Pi is unchanged
    settings = ForgeSettings(enabled=True)
    started = pack_hash(settings)
    seen: list[Any] = []
    compiler = forge_compiler(
        tmp_path,
        settings=settings,
        agent=_EditsTheBridge(node / "extension.ts"),  # type: ignore[arg-type]
    )
    compiler.forge_listener = seen.append
    await compiler.compile(spec())
    [result] = seen
    assert result.failures == ("forge_no_submission",)
    # The session's Pi bridge was spawned before the edit; its record names the edit.
    assert result.pack_sha256 == started
