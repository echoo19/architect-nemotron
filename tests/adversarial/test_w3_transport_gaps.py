"""W3 review of the native Pi transport and the Forge pack: gaps the other W3 review files
do not pin. Each test pinned the behaviour the branch claims or needs. All are fixed, so
they are regression tests now.

1. Forge's streaming meter recorded a failed stream at its partial usage (Anthropic's
   ``message_start``), while the run and the wallets kept its quote, so ``forge.json``
   and the turn row reported less than the session was charged. The meter, like the
   wallets, now keeps a stream's quote until its final usage.
2. A reply cut at the output limit ran its last call whenever that call's arguments
   parsed. vLLM ends every reply, a cut one included, by streaming whatever its tool
   parser has parsed but not yet sent (``finalize_generation`` and
   ``get_remaining_unstreamed_args``). With a partial-JSON parser such as vLLM's llama
   one, that is the rest of the auto-completed JSON, so a call cut inside its arguments
   arrives as valid JSON that lacks what the model was still writing. Now nothing from a
   cut reply runs: it is re-asked once with ``_CUT_REPLY``.
3. ``TUNING.md`` could pass ``TUNING_MAX_BYTES``: the first case was always shown whole,
   and ``case_chars`` allows 20,000 characters of input and of expected output. The first
   case is now clipped harder when it alone would not fit.
4. Pi picked native calls only when the context's own adapter streamed. The agent CLI
   hands every factory adapter out inside the session's ``ObservedAdapter``, so its Stage
   2 and Stage 4 runs and its run tool kept the JSON envelope for a model that
   ``architect run`` and the coder drive with native calls. An ``ObservedAdapter`` around
   a streaming adapter is now an ``ObservedChat``, which streams and meters.

Offline: MockTransport streams, scripted adapters, the real pinned Pi SDK and an inert
protocol peer. No live provider is called.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_pi import _READY, ProtocolProcess, install_protocol_peer
from test_pi_native import PEER_CONTEXT, QUOTE, Streaming, pack
from test_w3_native_transport import NEBIUS_URL, SUPER, Scripted, final, measuring_backend, sse

from architect.backends.pi import PiBackend, PiError
from architect.compiler.forge import _metered
from architect.compiler.forge_settings import ForgeSettings
from architect.compiler.forge_workspace import TUNING_MAX_BYTES, _tuning_markdown
from architect.floors import EvaluationCase
from architect.models import ModelRequest, ModelResponse, UsageQuote
from architect.models.chat import ChatMessage
from architect.models.http import AnthropicAdapter, OpenAICompatibleAdapter
from architect.models.types import AdapterError
from architect.runtime import RunContext
from architect.runtime.wallet import compilation_wallet
from architect.session.observe import Meter, ObservedAdapter
from architect.spec import Budget

ROOT = Path(__file__).parents[2]
PI_READY = (
    ROOT / "architect/backends/pi-node/node_modules/@mariozechner/pi-coding-agent/package.json"
).is_file()
needs_pi = pytest.mark.skipif(not PI_READY, reason="Pi dependencies are not installed")

ANTHROPIC = "claude-sonnet-4-20250514"


# -- 1. a failed stream that reported partial usage -----------------------------------


def _event(kind: str, body: dict[str, Any]) -> bytes:
    return f"event: {kind}\ndata: {json.dumps(body)}\n\n".encode()


async def test_a_failed_streamed_turn_is_metered_as_the_wallets_keep_it() -> None:
    async def body() -> AsyncIterator[bytes]:
        start = {"model": ANTHROPIC, "usage": {"input_tokens": 3000, "output_tokens": 1}}
        yield _event("message_start", {"type": "message_start", "message": start})
        block = {"type": "content_block_start", "index": 0,
                 "content_block": {"type": "text", "text": ""}}
        yield _event("content_block_start", block)
        for _ in range(50):
            text = {"type": "text_delta", "text": "word " * 10}
            yield _event(
                "content_block_delta", {"type": "content_block_delta", "index": 0, "delta": text}
            )
        error = {"type": "overloaded_error", "message": "Overloaded"}
        yield _event("error", {"type": "error", "error": error})

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
        with pytest.raises(AdapterError, match="overloaded_error"):
            await context.stream_call("s", messages, (), 12288)
    [row] = agent.turns
    assert row.ok is False
    # A failed stream keeps its quote, in the run and in every wallet (stream_call).
    assert wallet.cost_usd == pytest.approx(quote.cost_usd)
    assert context.cost_usd == pytest.approx(quote.cost_usd)
    # The meter, forge.json's agent_cost_usd and the row said $0.009015 (3,000 input
    # tokens and 1 output token) against $0.188 charged, and the row was not marked
    # estimated.
    assert agent.cost_usd == pytest.approx(wallet.cost_usd)
    assert row.cost_usd == pytest.approx(wallet.cost_usd)


# -- 2. a cut call whose arguments the server closed ---------------------------------


@needs_pi
async def test_a_cut_call_the_server_closed_never_runs(tmp_path: Path) -> None:
    # The reply was cut inside "cases". At the end of the reply a vLLM server streams the
    # rest of its tool parser's partial parse ("]}"), so the call reads {"cases": [0, 1]}:
    # valid JSON, but not what the model was writing.
    cut = sse(
        {"model": SUPER, "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
            {"index": 0, "id": "chatcmpl-tool-1", "type": "function",
             "function": {"name": "forge_measure", "arguments": '{"cases": [0, 1'}},
        ]}}]},
        {"model": SUPER, "choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "]}"}},
        ]}, "finish_reason": "length"}]},
        {"model": SUPER, "choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 256}},
    )
    script = Scripted(cut, final(SUPER), final(SUPER))
    adapter = OpenAICompatibleAdapter(
        SUPER, api_key="k", base_url=NEBIUS_URL, dialect="nebius",
        transport=httpx.MockTransport(script),
    )
    seen: list[Any] = []
    ctx = RunContext(
        adapter, Budget(cost_usd=1, compute_s=120), repository=tmp_path,
        is_evaluation=False, is_build=True,
    )
    bound = pack("read", "forge_measure", max_output_tokens=256)
    assert await measuring_backend(seen).run(ctx, {}, bound) == "Done."
    # forge_measure ran on cases [0, 1] (real spend) from a reply cut inside them.
    assert seen == []


# -- 3. TUNING.md's size bound -------------------------------------------------------


def test_tuning_md_stays_within_its_byte_bound_with_large_cases() -> None:
    settings = ForgeSettings(case_chars=12000)  # inside the contract, which allows 20,000
    cases = [
        EvaluationCase(
            case_id=f"c{index}", task={"text": "x" * 12000}, expected={"text": "y" * 12000}
        )
        for index in range(3)
    ]
    text = _tuning_markdown(cases, settings)
    # It was 24,303 bytes. One read is cut at 16,384 characters with no marker, so the
    # engineer silently lost the first case's expected output.
    assert len(text.encode("utf-8")) <= TUNING_MAX_BYTES


# -- 4. transport selection behind the session meter ---------------------------------


class _Both(Streaming):
    """A model that streams native chats and can also complete (an envelope turn)."""

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=1000, tokens_out=100, cost_usd=QUOTE)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            text='{"text":"done"}', tokens_in=100, tokens_out=20, cost_usd=0.002, model=self.model
        )


async def test_a_session_metered_streaming_model_drives_pi_with_native_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inner = _Both()
    peer = ProtocolProcess([_READY, {"kind": "model", "id": 1, "context": PEER_CONTEXT}])
    install_protocol_peer(monkeypatch, peer)
    # What observe_services' factories hand the agent CLI's Stage 2 and Stage 4 runs and
    # its run tool. The conductor and the coder unwrap it (streaming_adapter) and stream.
    observed = ObservedAdapter(inner, Meter())  # type: ignore[arg-type]
    ctx = RunContext(observed, Budget(cost_usd=1, compute_s=60), repository=tmp_path)
    backend = PiBackend()
    with pytest.raises(PiError, match="Invalid Pi protocol"):  # the inert peer's stream ends
        await backend.run(ctx, {}, pack("read", "write"))
    evidence = backend.last_evidence
    assert evidence is not None and evidence.model_calls == 1
    # It was "envelope", though the same model through `architect run` gets native calls.
    assert evidence.transport == "native" and len(inner.sent) == 1
