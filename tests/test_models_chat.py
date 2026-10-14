"""Native chat streaming (Phase 13, Section 4): adapters and RunContext metering, offline.

Every HTTP exchange is a recorded SSE/NDJSON fixture served through httpx.MockTransport,
cut into small chunks so lines and JSON arrive split.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from architect.models import (
    AdapterError,
    AnthropicAdapter,
    ModelRequest,
    ModelResponse,
    OllamaAdapter,
    OpenAICompatibleAdapter,
    UsageQuote,
)
from architect.models import http as http_module
from architect.models.chat import (
    ChatEvent,
    ChatMessage,
    ChatStreaming,
    DoneEvent,
    TextDeltaEvent,
    ToolCallEvent,
    ToolSpec,
    UsageEvent,
)
from architect.models.pricing import load_prices, price_for
from architect.runtime import BudgetExceeded, RunContext
from architect.runtime.wallet import compilation_wallet
from architect.spec import Budget

FIXTURES = Path(__file__).parent / "fixtures" / "chat"
TOOLS = (
    ToolSpec(
        "read_file",
        "Read a file",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    ),
)
CONVERSATION = (
    ChatMessage("user", "what is in notes.txt?"),
    ChatMessage(
        "assistant",
        "Let me look.",
        tool_calls=(ToolCallEvent("toolu_01A", "read_file", {"path": "notes.txt"}),),
    ),
    ChatMessage("tool", '{"content": "alpha"}', tool_call_id="toolu_01A", name="read_file"),
    ChatMessage("user", "[Note] keep it short"),
)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes().replace(b"\r\n", b"\n")


def chunked(body: bytes, size: int = 7) -> httpx.Response:
    async def chunks() -> AsyncIterator[bytes]:
        for start in range(0, len(body), size):
            yield body[start : start + size]

    return httpx.Response(200, content=chunks())


class Recorder:
    """A MockTransport handler serving one body and keeping each request's JSON."""

    def __init__(self, body: bytes, *, status: int = 200, size: int = 7) -> None:
        self.body = body
        self.status = status
        self.size = size
        self.requests: list[dict[str, Any]] = []
        self.paths: list[str] = []
        self.headers: list[httpx.Headers] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        self.paths.append(request.url.path)
        self.headers.append(request.headers)
        if self.status != 200:
            return httpx.Response(self.status, content=self.body)
        return chunked(self.body, self.size)


async def collect(stream: AsyncIterator[ChatEvent]) -> list[ChatEvent]:
    return [event async for event in stream]


def cost(model: str, tokens_in: int, tokens_out: int, **cache: int) -> float:
    return price_for(load_prices(), model).cost(tokens_in, tokens_out, **cache)


# -- Anthropic ----------------------------------------------------------------------


def anthropic(handler: Callable[[httpx.Request], httpx.Response]) -> AnthropicAdapter:
    return AnthropicAdapter(api_key="test", transport=httpx.MockTransport(handler))


async def test_anthropic_streams_text_and_a_tool_call_split_across_chunks() -> None:
    recorder = Recorder(fixture("anthropic_tool_use.sse"), size=5)
    adapter = anthropic(recorder)
    assert isinstance(adapter, ChatStreaming)
    events = await collect(adapter.stream_chat("You are Architect.", CONVERSATION, TOOLS, 512))
    texts = [e.text for e in events if isinstance(e, TextDeltaEvent)]
    assert texts == ["Let me ", "look."]
    calls = [e for e in events if isinstance(e, ToolCallEvent)]
    assert calls == [ToolCallEvent("toolu_01A", "read_file", {"path": "notes.txt"})]
    usages = [e for e in events if isinstance(e, UsageEvent)]
    assert len(usages) == 2  # the partial one from message_start, then the final one
    assert (usages[0].tokens_in, usages[0].tokens_out) == (2120, 1)
    final = usages[-1]
    assert (final.tokens_in, final.tokens_out, final.cache_write_tokens) == (2120, 42, 2000)
    model = "claude-sonnet-4-20250514"
    assert final.cost_usd == pytest.approx(cost(model, 2120, 42, cache_write_tokens=2000))
    assert events[-1] == DoneEvent("tool_use")
    # The request: streamed, cacheable system, native tools, merged tool results.
    body = recorder.requests[0]
    assert recorder.paths == ["/v1/messages"] and recorder.headers[0]["x-api-key"] == "test"
    assert body["stream"] is True and body["max_tokens"] == 512
    assert body["system"] == [
        {"type": "text", "text": "You are Architect.", "cache_control": {"type": "ephemeral"}}
    ]
    assert body["tools"][0]["input_schema"] == TOOLS[0].parameters
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant", "user"]
    assistant = body["messages"][1]["content"]
    assert assistant[1] == {
        "type": "tool_use", "id": "toolu_01A", "name": "read_file", "input": {"path": "notes.txt"}
    }
    last = body["messages"][2]["content"]
    assert last[0]["type"] == "tool_result" and last[0]["tool_use_id"] == "toolu_01A"
    assert last[1]["type"] == "text" and last[1]["cache_control"] == {"type": "ephemeral"}


async def test_anthropic_text_reply_with_cache_read_and_crlf_lines() -> None:
    body = fixture("anthropic_text.sse").replace(b"\n", b"\r\n")
    adapter = anthropic(Recorder(body, size=3))
    events = await collect(adapter.stream_chat("", (ChatMessage("user", "hi"),), (), None))
    assert "".join(e.text for e in events if isinstance(e, TextDeltaEvent)) == "It says alpha."
    final = [e for e in events if isinstance(e, UsageEvent)][-1]
    assert (final.tokens_in, final.tokens_out, final.cache_read_tokens) == (2030, 5, 2000)
    assert events[-1] == DoneEvent("end_turn")


async def test_anthropic_quote_reserves_cache_writes_and_the_output_cap() -> None:
    adapter = anthropic(Recorder(b""))
    quote = adapter.quote_chat("system", CONVERSATION, TOOLS, 512)
    assert quote.tokens_out == 512
    assert quote.tokens_in > len(b"system") + 1024  # bytes, tool overhead and framing
    price = price_for(load_prices(), adapter.model)
    assert quote.cost_usd == pytest.approx(
        price.quote_cost(quote.tokens_in, 512, may_write_cache=True)
    )
    with pytest.raises(AdapterError):
        adapter.quote_chat("system", CONVERSATION, TOOLS, 0)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            b'event: error\ndata: {"type":"error","error":{"type":"overloaded_error",'
            b'"message":"Overloaded"}}\n\n',
            "overloaded_error",
        ),
        (fixture("anthropic_text.sse").split(b"event: message_stop")[0], "incomplete"),
        (b"event: message_start\ndata: {not json\n\n", "invalid JSON"),
        (
            fixture("anthropic_text.sse").replace(
                b'"model":"claude-sonnet-4-20250514"', b'"model":"claude-other"'
            ),
            "does not match",
        ),
    ],
)
async def test_anthropic_stream_errors(body: bytes, message: str) -> None:
    adapter = anthropic(Recorder(body))
    with pytest.raises(AdapterError, match=message):
        await collect(adapter.stream_chat("", (ChatMessage("user", "hi"),), (), None))


@pytest.mark.parametrize("status", [401, 429, 500])
async def test_http_status_errors_name_the_status(status: int) -> None:
    adapter = anthropic(Recorder(b'{"error": "nope"}', status=status))
    with pytest.raises(AdapterError, match=f"HTTP {status}"):
        await collect(adapter.stream_chat("", (ChatMessage("user", "hi"),), (), None))


async def test_invalid_tool_arguments_become_a_call_error() -> None:
    body = fixture("anthropic_tool_use.sse").replace(b'tes.txt\\"}', b"tes.txt")
    events = await collect(
        anthropic(Recorder(body)).stream_chat("", (ChatMessage("user", "x"),), TOOLS, None)
    )
    call = next(e for e in events if isinstance(e, ToolCallEvent))
    assert call.error == "invalid_arguments" and call.arguments == {}


async def test_size_limits_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http_module, "STREAM_LINE_BYTES", 64)
    adapter = anthropic(Recorder(fixture("anthropic_text.sse")))
    with pytest.raises(AdapterError, match="size limit"):
        await collect(adapter.stream_chat("", (ChatMessage("user", "hi"),), (), None))
    monkeypatch.setattr(http_module, "STREAM_LINE_BYTES", 4 * 1024 * 1024)
    monkeypatch.setattr(http_module, "STREAM_TOOL_ARGUMENT_BYTES", 8)
    adapter = anthropic(Recorder(fixture("anthropic_tool_use.sse")))
    with pytest.raises(AdapterError, match="size limit"):
        await collect(adapter.stream_chat("", (ChatMessage("user", "hi"),), TOOLS, None))


# -- OpenAI-compatible --------------------------------------------------------------


async def test_openai_accumulates_interleaved_tool_call_fragments() -> None:
    recorder = Recorder(fixture("openai_tool_calls.sse"), size=11)
    adapter = OpenAICompatibleAdapter(api_key="k", transport=httpx.MockTransport(recorder))
    assert isinstance(adapter, ChatStreaming)
    events = await collect(adapter.stream_chat("sys", CONVERSATION, TOOLS, 256))
    assert [e.text for e in events if isinstance(e, TextDeltaEvent)] == ["Checking"]
    assert [e for e in events if isinstance(e, ToolCallEvent)] == [
        ToolCallEvent("call_abc", "read_file", {"path": "a.md"}),
        ToolCallEvent("call_def", "grep", {"pattern": "x"}),
    ]
    usage = next(e for e in events if isinstance(e, UsageEvent))
    assert (usage.tokens_in, usage.tokens_out, usage.cache_read_tokens) == (300, 25, 128)
    assert usage.cost_usd == pytest.approx(cost("gpt-4o-mini", 300, 25, cache_read_tokens=128))
    assert not usage.estimated and events[-1] == DoneEvent("tool_use")
    body = recorder.requests[0]
    assert recorder.paths == ["/v1/chat/completions"]
    assert recorder.headers[0]["authorization"] == "Bearer k"
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    # api.openai.com takes max_completion_tokens (reasoning models reject max_tokens).
    assert body["max_completion_tokens"] == 256 and "max_tokens" not in body
    assert body["tools"][0] == {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file",
            "parameters": TOOLS[0].parameters,
        },
    }
    messages = body["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool", "user"]
    assert messages[2]["tool_calls"][0]["function"] == {
        "name": "read_file", "arguments": '{"path": "notes.txt"}'
    }
    assert messages[3] == {
        "role": "tool", "tool_call_id": "toolu_01A", "content": '{"content": "alpha"}'
    }


async def test_openai_compatible_servers_without_usage_get_an_estimate() -> None:
    recorder = Recorder(fixture("openai_text_no_usage.sse"))
    adapter = OpenAICompatibleAdapter(
        base_url="http://localhost:1234/v1", transport=httpx.MockTransport(recorder)
    )
    messages = (ChatMessage("user", "hi"),)
    events = await collect(adapter.stream_chat("", messages, (), 64))
    assert "".join(e.text for e in events if isinstance(e, TextDeltaEvent)) == "Hello there"
    usage = next(e for e in events if isinstance(e, UsageEvent))
    quote = adapter.quote_chat("", messages, (), 64)
    assert usage.estimated and usage.tokens_in == quote.tokens_in
    assert usage.tokens_out == len(b"Hello there") and usage.cost_usd <= quote.cost_usd
    assert events[-1] == DoneEvent("end_turn")
    body = recorder.requests[0]
    assert body["max_tokens"] == 64 and "tools" not in body and "authorization" not in (
        recorder.headers[0]
    )


async def test_openai_tool_calls_without_index_are_keyed_by_id() -> None:
    chunks = [
        {"model": "gpt-4o-mini", "choices": [{"index": 0, "delta": {"tool_calls": [
            {"id": "a1", "function": {"name": "read_file", "arguments": '{"path": "x"}'}},
            {"id": "b2", "function": {"name": "grep", "arguments": {"pattern": "y"}}},
        ]}, "finish_reason": "tool_calls"}]},
        {"model": "gpt-4o-mini", "choices": [],
         "x_groq": {"usage": {"prompt_tokens": 10, "completion_tokens": 3}}},
    ]
    body = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks)
    adapter = OpenAICompatibleAdapter(transport=httpx.MockTransport(Recorder(body)))
    events = await collect(adapter.stream_chat("", (ChatMessage("user", "x"),), TOOLS, None))
    assert [(e.id, e.name, e.arguments) for e in events if isinstance(e, ToolCallEvent)] == [
        ("a1", "read_file", {"path": "x"}), ("b2", "grep", {"pattern": "y"})
    ]
    usage = next(e for e in events if isinstance(e, UsageEvent))
    assert (usage.tokens_in, usage.tokens_out, usage.estimated) == (10, 3, False)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b'data: {"error": {"type": "rate_limit_exceeded"}}\n\n', "rate_limit_exceeded"),
        (
            b'data: {"model":"gpt-4o-mini","choices":[{"index":0,"delta":{"content":"x"}}]}\n\n',
            "incomplete",
        ),
        (b'data: {"model":"other-model","choices":[]}\n\n', "does not match"),
        (b"data: [1, 2]\n\n", "invalid JSON"),
    ],
)
async def test_openai_stream_errors(body: bytes, message: str) -> None:
    adapter = OpenAICompatibleAdapter(transport=httpx.MockTransport(Recorder(body)))
    with pytest.raises(AdapterError, match=message):
        await collect(adapter.stream_chat("", (ChatMessage("user", "x"),), (), None))


# -- Ollama -------------------------------------------------------------------------


async def test_ollama_ndjson_text_tool_call_and_usage() -> None:
    recorder = Recorder(fixture("ollama_tool_call.ndjson"), size=13)
    adapter = OllamaAdapter(transport=httpx.MockTransport(recorder), think=False)
    assert isinstance(adapter, ChatStreaming)
    events = await collect(adapter.stream_chat("sys", CONVERSATION, TOOLS, 300))
    assert "".join(e.text for e in events if isinstance(e, TextDeltaEvent)) == "Sure, reading."
    assert [e for e in events if isinstance(e, ToolCallEvent)] == [
        ToolCallEvent("call_0", "read_file", {"path": "a.md"})
    ]
    usage = next(e for e in events if isinstance(e, UsageEvent))
    assert (usage.tokens_in, usage.tokens_out, usage.cost_usd) == (210, 18, 0.0)
    assert events[-1] == DoneEvent("tool_use")
    body = recorder.requests[0]
    assert recorder.paths == ["/api/chat"]
    assert body["stream"] is True and body["truncate"] is False and body["think"] is False
    assert body["options"] == {"num_ctx": adapter.num_ctx, "num_predict": 300}
    assert body["tools"][0]["function"]["name"] == "read_file"
    messages = body["messages"]
    assert messages[2]["tool_calls"] == [
        {"function": {"name": "read_file", "arguments": {"path": "notes.txt"}}}
    ]
    assert messages[3] == {
        "role": "tool", "content": '{"content": "alpha"}', "tool_name": "read_file"
    }


async def test_ollama_errors_and_context_overflow() -> None:
    overflow = Recorder(b'{"error":"exceed_context_size_error"}', status=400)
    adapter = OllamaAdapter(transport=httpx.MockTransport(overflow))
    with pytest.raises(AdapterError, match="context"):
        await collect(adapter.stream_chat("", (ChatMessage("user", "x"),), (), None))
    big = fixture("ollama_tool_call.ndjson").replace(
        b'"prompt_eval_count":210', b'"prompt_eval_count":32700'
    )
    adapter = OllamaAdapter(transport=httpx.MockTransport(Recorder(big)))
    with pytest.raises(AdapterError, match="context"):
        await collect(adapter.stream_chat("", (ChatMessage("user", "x"),), (), 100))
    failing = Recorder(b'{"error":"model not found"}\n')
    adapter = OllamaAdapter(transport=httpx.MockTransport(failing))
    with pytest.raises(AdapterError, match="reported an error"):
        await collect(adapter.stream_chat("", (ChatMessage("user", "x"),), (), None))
    cut = fixture("ollama_tool_call.ndjson").rsplit(b"\n{", 1)[0]
    adapter = OllamaAdapter(transport=httpx.MockTransport(Recorder(cut)))
    with pytest.raises(AdapterError, match="incomplete"):
        await collect(adapter.stream_chat("", (ChatMessage("user", "x"),), (), None))


def test_cli_and_fake_adapters_do_not_stream() -> None:
    from architect.models import ClaudeCodeAdapter, FakeAdapter

    assert not isinstance(FakeAdapter(), ChatStreaming)
    assert not hasattr(ClaudeCodeAdapter, "stream_chat")


# -- RunContext.stream_call metering ------------------------------------------------


class Scripted:
    """A ChatStreaming fake: fixed quote, scripted events, optional pause."""

    model = "fake"

    def __init__(
        self, events: Sequence[ChatEvent], *, quote_usd: float = 0.01,
        gate: asyncio.Event | None = None,
    ) -> None:
        self.events = list(events)
        self.quote_usd = quote_usd
        self.gate = gate
        self.opened = 0
        self.closed = 0

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=1, tokens_out=1, cost_usd=self.quote_usd)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise AssertionError("not used")

    def quote_chat(
        self, system: str, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec],
        max_output_tokens: int | None,
    ) -> UsageQuote:
        return UsageQuote(tokens_in=100, tokens_out=50, cost_usd=self.quote_usd)

    async def stream_chat(
        self, system: str, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec],
        max_output_tokens: int | None,
    ) -> AsyncIterator[ChatEvent]:
        self.opened += 1
        try:
            for event in self.events:
                if event is PAUSE and self.gate is not None:
                    self.gate.set()
                    await asyncio.sleep(3600)
                yield event
        finally:
            self.closed += 1


PAUSE = TextDeltaEvent("<pause>")
DONE = (
    TextDeltaEvent("Hi "),
    TextDeltaEvent("there."),
    UsageEvent(80, 20, 0.004),
    DoneEvent("end_turn"),
)
HI = (ChatMessage("user", "hi"),)


async def test_stream_call_reserves_then_reconciles_with_reported_usage() -> None:
    adapter = Scripted(DONE)
    seen: list[ChatEvent] = []
    with compilation_wallet(Budget(cost_usd=1.0, compute_s=60)) as wallet:
        context = RunContext(adapter, Budget(cost_usd=1.0, compute_s=60))
        result = await context.stream_call("sys", HI, (), 128, on_event=seen.append)
    assert seen == list(DONE)
    assert result.text == "Hi there." and result.stop_reason == "end_turn"
    assert result.usage == UsageEvent(80, 20, 0.004)
    assert (context.tokens_in, context.tokens_out) == (80, 20)
    assert context.cost_usd == pytest.approx(0.004) and context.model_calls == 1
    assert wallet.cost_usd == pytest.approx(0.004) and wallet.tokens_in == 80
    assert context.model_params["max_output_tokens"] == 128
    assert adapter.closed == 1


async def test_a_stream_over_its_quote_raises_budget_exceeded() -> None:
    adapter = Scripted((UsageEvent(10, 10, 0.05), DoneEvent("end_turn")), quote_usd=0.01)
    context = RunContext(adapter, Budget(cost_usd=1.0, compute_s=60))
    with pytest.raises(BudgetExceeded, match="reserved usage"):
        await context.stream_call("", HI)
    assert context.cost_usd == pytest.approx(0.05)  # measured usage is still charged


async def test_a_quote_over_the_budget_never_opens_the_stream() -> None:
    adapter = Scripted(DONE, quote_usd=2.0)
    context = RunContext(adapter, Budget(cost_usd=1.0, compute_s=60))
    with pytest.raises(BudgetExceeded, match="exceed run budget"):
        await context.stream_call("", HI)
    assert adapter.opened == 0 and context.cost_usd == 0.0
    with compilation_wallet(Budget(cost_usd=0.001, compute_s=60)) as wallet:
        context = RunContext(Scripted(DONE), Budget(cost_usd=1.0, compute_s=60))
        with pytest.raises(BudgetExceeded, match="Compilation budget"):
            await context.stream_call("", HI)
    assert wallet.cost_usd == 0.0


async def test_a_failed_stream_keeps_its_quote_like_a_failed_call() -> None:
    adapter = Scripted((TextDeltaEvent("x"), DoneEvent("end_turn")))  # never reports usage
    context = RunContext(adapter, Budget(cost_usd=1.0, compute_s=60))
    with pytest.raises(AdapterError, match="incomplete"):
        await context.stream_call("", HI)
    assert context.cost_usd == pytest.approx(0.01)


async def test_streaming_requires_the_capability() -> None:
    from architect.models import FakeAdapter

    context = RunContext(FakeAdapter(), Budget(cost_usd=1.0, compute_s=60))
    with pytest.raises(AdapterError, match="streaming"):
        await context.stream_call("", HI)


@pytest.mark.parametrize("reported", [True, False])
async def test_a_cancelled_stream_keeps_its_quote_until_its_final_usage(reported: bool) -> None:
    gate = asyncio.Event()
    # An early partial usage, like Anthropic's message_start: the prompt, no reply yet.
    head: list[ChatEvent] = [UsageEvent(70, 1, 0.002)] if reported else []
    adapter = Scripted((*head, TextDeltaEvent("a"), PAUSE, *DONE), gate=gate)
    with compilation_wallet(Budget(cost_usd=1.0, compute_s=60)) as wallet:
        context = RunContext(adapter, Budget(cost_usd=1.0, compute_s=60))
        task = asyncio.create_task(context.stream_call("", HI))
        await asyncio.wait_for(gate.wait(), 30)
        assert wallet.cost_usd == pytest.approx(0.01)  # the quote is held while streaming
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    # The provider has read the prompt and written part of the reply, and no usage it
    # reported so far counts that reply (an OpenAI-compatible stream reports none before
    # its end), so the quote is kept, as for a cancelled call.
    assert wallet.cost_usd == pytest.approx(0.01)
    assert context.cost_usd == pytest.approx(0.01)
    assert (context.tokens_in, context.tokens_out) == (100, 50)
    assert adapter.closed == 1 and not context._calls


@pytest.mark.parametrize("ending", ["deadline", "cancelled"])
async def test_a_stream_costs_its_quote_whichever_deadline_stops_it(ending: str) -> None:
    # Forge runs a session under its own deadline and each run under one of the same
    # length: the run's ends the stream with BudgetExceeded, the session's cancels it.
    gate = asyncio.Event()
    adapter = Scripted((UsageEvent(70, 1, 0.002), PAUSE, *DONE), gate=gate)
    compute_s = 0.5 if ending == "deadline" else 60.0
    expected: type[BaseException] = (
        BudgetExceeded if ending == "deadline" else asyncio.CancelledError
    )
    with compilation_wallet(Budget(cost_usd=1.0, compute_s=60)) as wallet:
        context = RunContext(adapter, Budget(cost_usd=1.0, compute_s=compute_s))
        task = asyncio.create_task(context.stream_call("", HI))
        await asyncio.wait_for(gate.wait(), 30)
        if ending == "cancelled":
            task.cancel()
        with pytest.raises(expected):
            await task
    assert wallet.cost_usd == pytest.approx(0.01) and context.cost_usd == pytest.approx(0.01)


async def test_a_stream_stopped_after_its_done_event_is_settled_at_its_final_usage() -> None:
    # Cancelled once the DoneEvent arrived (a pause no real adapter makes there):
    gate = asyncio.Event()
    adapter = Scripted((*DONE, PAUSE), gate=gate)
    with compilation_wallet(Budget(cost_usd=1.0, compute_s=60)) as wallet:
        context = RunContext(adapter, Budget(cost_usd=1.0, compute_s=60))
        task = asyncio.create_task(context.stream_call("", HI))
        await asyncio.wait_for(gate.wait(), 30)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert wallet.cost_usd == pytest.approx(0.004) and context.cost_usd == pytest.approx(0.004)

    # or failed there by the caller's own event hook.
    def hook(event: ChatEvent) -> None:
        if isinstance(event, DoneEvent):
            raise RuntimeError("the hook failed")

    with compilation_wallet(Budget(cost_usd=1.0, compute_s=60)) as wallet:
        context = RunContext(Scripted(DONE), Budget(cost_usd=1.0, compute_s=60))
        with pytest.raises(RuntimeError, match="hook"):
            await context.stream_call("", HI, on_event=hook)
    assert wallet.cost_usd == pytest.approx(0.004) and context.cost_usd == pytest.approx(0.004)


async def test_cancelling_a_real_http_stream_closes_it_and_keeps_its_quote() -> None:
    gate = asyncio.Event()
    closed = asyncio.Event()
    start = fixture("anthropic_text.sse").split(b"event: content_block_start")[0]

    async def body() -> AsyncIterator[bytes]:
        try:
            yield start
            gate.set()
            await asyncio.sleep(3600)
            yield b""
        finally:
            closed.set()

    adapter = anthropic(lambda request: httpx.Response(200, content=body()))
    quote = adapter.quote_chat("", HI, (), 100)
    seen: list[ChatEvent] = []
    with compilation_wallet(Budget(cost_usd=5.0, compute_s=60)) as wallet:
        context = RunContext(adapter, Budget(cost_usd=5.0, compute_s=60))
        task = asyncio.create_task(context.stream_call("", HI, (), 100, on_event=seen.append))
        await asyncio.wait_for(gate.wait(), 30)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await asyncio.wait_for(closed.wait(), 30)
    # message_start reported the prompt and one output token. That is not the final
    # usage: the reply the provider was writing is not in it, so the quote stays.
    partial = cost("claude-sonnet-4-20250514", 2030, 1, cache_read_tokens=2000)
    assert seen == [UsageEvent(2030, 1, partial, cache_read_tokens=2000)]
    assert wallet.cost_usd == pytest.approx(quote.cost_usd) and partial < quote.cost_usd
    assert context.cost_usd == pytest.approx(quote.cost_usd)


# -- Nebius Token Factory (the nebius dialect) --------------------------------------

SUPER = "nvidia/nemotron-3-super-120b-a12b"
NEBIUS_CALL = "chatcmpl-tool-5c2b8f0e9a7d4b1c8e3f6a9d0b2c4e7f"


def nebius(
    handler: Callable[[httpx.Request], httpx.Response], **options: Any
) -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter(
        SUPER, api_key="k", base_url="https://api.tokenfactory.nebius.com/v1",
        dialect="nebius", transport=httpx.MockTransport(handler), **options,
    )


def sse(*chunks: dict[str, Any]) -> bytes:
    return b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + (
        b"data: [DONE]\n\n")


async def test_nebius_stream_hides_reasoning_and_keeps_tool_call_ids() -> None:
    recorder = Recorder(fixture("nebius_reasoning_tool_calls.sse"), size=9)
    events = await collect(nebius(recorder).stream_chat("sys", CONVERSATION, TOOLS, 512))
    # Reasoning deltas are never shown, and the reply's leading blank lines go.
    assert [e.text for e in events if isinstance(e, TextDeltaEvent)] == ["Reading it now."]
    assert [e for e in events if isinstance(e, ToolCallEvent)] == [
        ToolCallEvent(NEBIUS_CALL, "read_file", {"path": "notes.txt"})
    ]
    usage = next(e for e in events if isinstance(e, UsageEvent))
    # completion_tokens already holds the 21 reasoning tokens: billed once, as reported.
    assert (usage.tokens_in, usage.tokens_out, usage.estimated) == (412, 58, False)
    assert usage.cost_usd == pytest.approx(cost(SUPER, 412, 58))
    assert events[-1] == DoneEvent("tool_use")
    body = recorder.requests[0]
    assert body["max_tokens"] == 512 and "max_completion_tokens" not in body
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert "reasoning_effort" not in body and "temperature" not in body
    assert body["tools"][0]["function"]["name"] == "read_file"
    # Without a cap the price entry's capacity is sent, as quoted.
    recorder = Recorder(fixture("nebius_reasoning_tool_calls.sse"))
    adapter = nebius(recorder)
    await collect(adapter.stream_chat("", (ChatMessage("user", "x"),), TOOLS, None))
    assert recorder.requests[0]["max_tokens"] == 16384
    assert adapter.quote_chat("", (ChatMessage("user", "x"),), TOOLS, None).tokens_out == 16384


async def test_nebius_stream_drops_an_inline_think_block_and_estimates_with_reasoning() -> None:
    body = sse(
        {"model": SUPER, "choices": [{"index": 0, "delta": {"reasoning": "abcdefghij"}}]},
        {"model": SUPER, "choices": [{"index": 0, "delta": {"content": "<thi"}}]},
        {"model": SUPER, "choices": [{"index": 0, "delta": {"content": "nk>plan</th"}}]},
        {"model": SUPER, "choices": [{"index": 0, "delta": {"content": "ink>\n Hi"}}]},
        {"model": SUPER, "choices": [{"index": 0, "delta": {"content": " there"},
                                      "finish_reason": "stop"}]},
    )
    messages = (ChatMessage("user", "x"),)
    events = await collect(nebius(Recorder(body, size=5)).stream_chat("", messages, (), 64))
    assert "".join(e.text for e in events if isinstance(e, TextDeltaEvent)) == "Hi there"
    usage = next(e for e in events if isinstance(e, UsageEvent))
    # No usage chunk: every generated byte counts, the hidden ones included.
    generated = len(b"<think>plan</think>\n Hi there") + len(b"abcdefghij")
    assert usage.estimated and usage.tokens_out == generated
    assert events[-1] == DoneEvent("end_turn")


async def test_nebius_stream_cut_inside_its_reasoning_ends_as_a_cut_with_nothing_shown() -> None:
    from architect.models.http import REASONING_CUT

    body = sse(
        {"model": SUPER, "choices": [{"index": 0, "delta": {"reasoning_content": "step one"}}]},
        {"model": SUPER, "choices": [{"index": 0, "delta": {"content": "\n"},
                                      "finish_reason": "length"}]},
        {"model": SUPER, "choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 64}},
    )
    events = await collect(nebius(Recorder(body)).stream_chat(
        "", (ChatMessage("user", "x"),), (), 64))
    # Never the reasoning; the reported usage, then the cut, so a caller can ask again.
    assert not any(isinstance(e, TextDeltaEvent) for e in events)
    assert events[-2] == UsageEvent(9, 64, events[-2].cost_usd)  # type: ignore[union-attr]
    assert events[-1] == DoneEvent("max_tokens")
    # A visible answer cut at the cap is still an answer (stop reason max_tokens).
    cut = sse(
        {"model": SUPER, "choices": [{"index": 0, "delta": {"content": "Half an"},
                                      "finish_reason": "length"}]},
    )
    events = await collect(nebius(Recorder(cut)).stream_chat("", (ChatMessage("user", "x"),),
                                                             (), 64))
    assert events[-1] == DoneEvent("max_tokens")
    # Thinking on, cut, and no reasoning delta at all: what streamed was the reasoning.
    events = await collect(nebius(Recorder(cut), think=True).stream_chat(
        "", (ChatMessage("user", "x"),), (), 64))
    assert not any(isinstance(e, TextDeltaEvent) for e in events)
    assert events[-1] == DoneEvent("max_tokens")
    # A finished reply whose usage counts every token as reasoning had no answer at all:
    # still the distinct error.
    rambling = sse(
        {"model": SUPER, "choices": [{"index": 0, "delta": {"content": "Let me think"},
                                      "finish_reason": "stop"}]},
        {"model": SUPER, "choices": [], "usage": {
            "prompt_tokens": 9, "completion_tokens": 30,
            "completion_tokens_details": {"reasoning_tokens": 30}}},
    )
    with pytest.raises(AdapterError) as caught:
        await collect(nebius(Recorder(rambling)).stream_chat(
            "", (ChatMessage("user", "x"),), (), 64))
    assert str(caught.value) == REASONING_CUT
    # Thinking on with its reasoning separated: an answer cut at the cap is still text.
    answered = sse(
        {"model": SUPER, "choices": [{"index": 0, "delta": {"reasoning": "count them"}}]},
        {"model": SUPER, "choices": [{"index": 0, "delta": {"content": "There are 21"},
                                      "finish_reason": "length"}]},
    )
    events = await collect(nebius(Recorder(answered), think=True).stream_chat(
        "", (ChatMessage("user", "x"),), (), 64))
    assert [e.text for e in events if isinstance(e, TextDeltaEvent)] == ["There are 21"]
    assert events[-1] == DoneEvent("max_tokens")


async def test_nebius_stream_retries_a_busy_answer_before_any_byte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(http_module, "_sleep", sleep)
    answers = [
        httpx.Response(429, headers={"Retry-After": "2"}, content=b'{"detail": "slow down"}'),
        chunked(fixture("nebius_reasoning_tool_calls.sse")),
    ]
    events = await collect(nebius(lambda request: answers.pop(0)).stream_chat(
        "", (ChatMessage("user", "x"),), TOOLS, None))
    assert waits == [2.0] and events[-1] == DoneEvent("tool_use")
    # Still busy after two retries: the status is named, as for any failed stream.
    statuses: list[int] = []

    def busy(request: httpx.Request) -> httpx.Response:
        statuses.append(503)
        return httpx.Response(503, content=b"busy")

    with pytest.raises(AdapterError, match="HTTP 503"):
        await collect(nebius(busy).stream_chat("", (ChatMessage("user", "x"),), (), None))
    assert len(statuses) == 3 and waits == [2.0, 1.0, 2.0]
    # The default dialect never retries.
    statuses.clear()
    plain = OpenAICompatibleAdapter(api_key="k", transport=httpx.MockTransport(busy))
    with pytest.raises(AdapterError, match="HTTP 503"):
        await collect(plain.stream_chat("", (ChatMessage("user", "x"),), (), None))
    assert statuses == [503]


LIGHTNING = "nvidia/Nemotron-3_5-Lightning"
ULTRA = "nvidia/Nemotron-3-Ultra-550b-a55b"


def nebius_on(model: str, body: bytes) -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter(
        model, api_key="k", base_url="https://api.tokenfactory.nebius.com/v1",
        dialect="nebius", think=True, transport=httpx.MockTransport(lambda r: chunked(body)),
    )


def delta(model: str, finish: str | None = None, **fields: Any) -> dict[str, Any]:
    return {"model": model, "choices": [{"index": 0, "delta": fields, "finish_reason": finish}]}


def cut_usage(model: str) -> dict[str, Any]:
    return {"model": model, "choices": [], "usage": {
        "prompt_tokens": 37, "completion_tokens": 64,
        "completion_tokens_details": {"reasoning_tokens": 64}}}


@pytest.mark.parametrize("reported", [False, True])
async def test_nebius_reasoning_repeated_as_content_at_the_cap_is_never_shown(
    reported: bool,
) -> None:
    # Trimmed from live streams (2026-10-03, thinking on, max_tokens 64): cut inside its
    # reasoning, Lightning sends the whole reasoning again as one content delta, then
    # the stop; Ultra sends it in the last chunk, beside the reasoning's last piece.
    lightning = [
        delta(LIGHTNING, role="assistant"),
        *(delta(LIGHTNING, reasoning=part, reasoning_content=part)
          for part in ("Here", "'s a", " thinking process", ":\n\n1.")),
        delta(LIGHTNING, content="Here's a thinking process:\n\n1."),
        delta(LIGHTNING, "length"),
    ]
    ultra = [
        delta(ULTRA, role="assistant", content=""),
        *(delta(ULTRA, reasoning_content=part) for part in ("The", " user wants", " a count")),
        delta(ULTRA, "length", content="The user wants a count (div", reasoning_content=" (div"),
    ]
    for model, chunks in ((LIGHTNING, lightning), (ULTRA, ultra)):
        body = sse(*chunks, *([cut_usage(model)] if reported else []))
        seen = await collect(nebius_on(model, body).stream_chat(
            "", (ChatMessage("user", "x"),), (), 64
        ))
        # Nothing shown: a cut reply with its usage (reported, or estimated from the bytes).
        assert [type(e) for e in seen] == [UsageEvent, DoneEvent], model
        assert seen[-1] == DoneEvent("max_tokens")
        usage = seen[0]
        assert isinstance(usage, UsageEvent) and usage.estimated is not reported, model


async def test_nebius_text_that_merely_quotes_the_reasoning_is_still_the_answer() -> None:
    body = sse(
        delta(ULTRA, reasoning_content="17*3 is 51, so the answer is 51."),
        delta(ULTRA, content="51"),
        delta(ULTRA, "stop", content=" (17*3)"),
    )
    events = await collect(nebius_on(ULTRA, body).stream_chat(
        "", (ChatMessage("user", "x"),), (), 64))
    assert "".join(e.text for e in events if isinstance(e, TextDeltaEvent)) == "51 (17*3)"
    assert events[-1] == DoneEvent("end_turn")


async def test_nebius_trailing_whitespace_goes_before_tool_calls_only() -> None:
    call = {"index": 0, "id": NEBIUS_CALL, "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "notes.txt"}'}}
    # Super, thinking off, live (2026-10-03): "We need get_weather for Paris.\n\n\n" + a call.
    with_call = sse(
        delta(SUPER, content="We need"), delta(SUPER, content=" read_file for notes.txt."),
        delta(SUPER, content="\n\n\n"), delta(SUPER, tool_calls=[call]),
        delta(SUPER, "tool_calls"),
    )
    events = await collect(nebius(Recorder(with_call, size=5)).stream_chat(
        "", (ChatMessage("user", "x"),), TOOLS, 64))
    assert "".join(e.text for e in events if isinstance(e, TextDeltaEvent)) == (
        "We need read_file for notes.txt.")
    assert [e.name for e in events if isinstance(e, ToolCallEvent)] == ["read_file"]
    # Without a call the text is kept whole, inner whitespace included.
    plain = sse(delta(SUPER, content="Line one.\n\n"), delta(SUPER, content="Line two.\n"),
                delta(SUPER, "stop"))
    events = await collect(nebius(Recorder(plain, size=4)).stream_chat(
        "", (ChatMessage("user", "x"),), (), 64))
    assert "".join(e.text for e in events if isinstance(e, TextDeltaEvent)) == (
        "Line one.\n\nLine two.\n")


@pytest.mark.parametrize(
    ("chunks", "text"),
    [
        # Content before any reasoning delta: held, then read once the reasoning shows up.
        ([{"content": "\n"}, {"reasoning": "plan"}, {"content": "51"}], "51"),
        # No reasoning delta and no closing tag: the model did not reason inline.
        ([{"content": "5"}, {"content": "1"}], "51"),
        # An opening tag of its own inside the template's block, closed: dropped.
        ([{"content": "<think>weigh"}, {"content": "</think>\n51"}], "51"),
        # The closing tag one character at a time.
        ([{"content": c} for c in "17*3 is 51\n</think>\n\n51"], "51"),
        # A block that never closes is reasoning, cut or not.
        ([{"content": "<think>still weighing"}], ""),
    ],
)
async def test_nebius_thinking_stream_shows_only_what_follows_the_reasoning(
    chunks: list[dict[str, Any]], text: str
) -> None:
    body = sse(*(delta(SUPER, **fields) for fields in chunks), delta(SUPER, "stop"))
    adapter = nebius(Recorder(body, size=3), think=True)
    events = await collect(adapter.stream_chat("", (ChatMessage("user", "x"),), (), 64))
    assert "".join(e.text for e in events if isinstance(e, TextDeltaEvent)) == text
    assert events[-1] == DoneEvent("end_turn")


async def test_nebius_exhausted_retries_are_a_busy_provider_not_a_login() -> None:
    from architect.session.codes import code_for, explain

    async def no_wait(seconds: float) -> None:
        return None

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(http_module, "_sleep", no_wait)
        for status in (429, 503, 529):
            busy = Recorder(b"private detail", status=status)
            with pytest.raises(AdapterError) as caught:
                await collect(nebius(busy).stream_chat("", (ChatMessage("user", "x"),), (), 8))
            assert len(busy.requests) == 3 and code_for(caught.value) == "provider_busy"
            plain = OpenAICompatibleAdapter(api_key="k", transport=httpx.MockTransport(
                lambda request, status=status: httpx.Response(status, text="private")))
            with pytest.raises(AdapterError) as caught:
                await plain.complete(ModelRequest(prompt="x"))
            assert str(caught.value) == f"Provider request failed (HTTP {status})"
            assert code_for(caught.value) == "provider_busy"
    assert "rate limited or overloaded" in explain("provider_busy")
