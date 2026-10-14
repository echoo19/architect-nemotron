"""Adversarial checks of the Nebius Token Factory dialect, offline.

Every exchange goes through ``httpx.MockTransport``; retry waits are recorded, never
slept (except one deadline check), and no key or provider is ever used. Two replies are
trimmed live captures (2026-10-03, thinking on); the rest are shaped after them.
"""

from __future__ import annotations

import asyncio
import email.utils
import json
import re
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from architect import profiles
from architect.agent.build import unpriced_engineers
from architect.models import AdapterError, ModelRequest, OpenAICompatibleAdapter
from architect.models import http as http_module
from architect.models.chat import (
    ChatEvent,
    ChatMessage,
    DoneEvent,
    TextDeltaEvent,
    ToolCallEvent,
    ToolSpec,
    UsageEvent,
)
from architect.models.http import REASONING_CUT, RETRY_WAIT_S
from architect.models.pricing import (
    Price,
    load_prices,
    per_token_price,
    price_for,
    unpriced_price,
    write_price_entries,
)
from architect.profiles import build_services, load_profile
from architect.providers import credentials
from architect.providers.catalog import entry
from architect.providers.discovery import list_models
from architect.providers.host import Host
from architect.providers.onboarding import register_model
from architect.runtime import RunContext
from architect.session.codes import code_for, explain
from architect.spec import Budget

SUPER = "nvidia/nemotron-3-super-120b-a12b"
ULTRA = "nvidia/Nemotron-3-Ultra-550b-a55b"
NANO = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
LIGHTNING = "nvidia/Nemotron-3_5-Lightning"
URL = "https://api.tokenfactory.nebius.com/v1"
ENDPOINT = {"provider": "openai", "base_url": URL, "api_key_ref": "nebius"}
HI = (ChatMessage("user", "x"),)
TOOLS = (
    ToolSpec("read_file", "Read a file",
             {"type": "object", "properties": {"path": {"type": "string"}}}),
)
TOOL_ID = "chatcmpl-tool-0123456789abcdef"  # the live shape: chatcmpl-tool-<16 hex>
# email.utils.parsedate_to_datetime raises OverflowError (not ValueError) for this.
OVERFLOWING_DATE = "1 jan 99999999999999999999 00:00"

Handler = Callable[[httpx.Request], httpx.Response]


# -- helpers ------------------------------------------------------------------------


def reply(
    content: Any, *, model: str = SUPER, finish: str = "stop", completion: int = 15,
    reasoning_tokens: Any = 0, **message: Any,
) -> dict[str, Any]:
    """A non-streaming Token Factory reply; ``reasoning_tokens`` None leaves the split out."""
    details = None if reasoning_tokens is None else {"reasoning_tokens": reasoning_tokens}
    return {
        "id": "chatcmpl-1", "object": "chat.completion", "model": model,
        "choices": [{"index": 0, "finish_reason": finish,
                     "message": {"role": "assistant", "content": content, **message}}],
        "usage": {"prompt_tokens": 27, "completion_tokens": completion,
                  "completion_tokens_details": details},
    }


def answer(body: dict[str, Any]) -> Handler:
    return lambda request: httpx.Response(200, json=body)


def nebius(handler: Handler, model: str = SUPER, **options: Any) -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter(model, api_key="k", base_url=URL, dialect="nebius",
                                   transport=httpx.MockTransport(handler), **options)


def chunk(model: str = SUPER, finish: str | None = None, **delta: Any) -> dict[str, Any]:
    return {"model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def usage_chunk(completion: int, reasoning: int | None = None, *, model: str = SUPER,
                ) -> dict[str, Any]:
    usage: dict[str, Any] = {"prompt_tokens": 9, "completion_tokens": completion}
    if reasoning is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return {"model": model, "choices": [], "usage": usage}


def sse(*chunks: dict[str, Any], done: bool = True) -> bytes:
    body = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks)
    return body + (b"data: [DONE]\n\n" if done else b"")


def streamed(body: bytes, size: int = 5) -> httpx.Response:
    async def parts() -> AsyncIterator[bytes]:
        for start in range(0, len(body), size):
            yield body[start : start + size]

    return httpx.Response(200, content=parts())


async def collect(adapter: OpenAICompatibleAdapter, *, tools: tuple[ToolSpec, ...] = (),
                  cap: int | None = 64) -> list[ChatEvent]:
    return [event async for event in adapter.stream_chat("", HI, tools, cap)]


def shown(events: list[ChatEvent]) -> str:
    return "".join(event.text for event in events if isinstance(event, TextDeltaEvent))


def usage_of(events: list[ChatEvent]) -> UsageEvent:
    return next(event for event in events if isinstance(event, UsageEvent))


class Script:
    """Answers requests in order (a callable makes a fresh streamed response each time)
    and keeps every request body, so retries can be compared byte for byte."""

    def __init__(self, *answers: httpx.Response | Callable[[], httpx.Response]) -> None:
        self.answers = list(answers)
        self.bodies: list[bytes] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(request.content)
        answer = self.answers.pop(0)
        return answer() if callable(answer) else answer


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []

    async def sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(http_module, "_sleep", sleep)
    return recorded


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# -- complete(): what counts as the answer ------------------------------------------


async def test_null_content_beside_tool_calls_is_an_empty_billed_answer() -> None:
    call = {"id": TOOL_ID, "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "a"}'}}
    body = reply(None, finish="tool_calls", reasoning_tokens=9, tool_calls=[call],
                 reasoning_content="I should read it.", reasoning="I should read it.")
    response = await nebius(answer(body), think=True).complete(ModelRequest(prompt="hi"))
    assert response.text == ""
    assert (response.tokens_in, response.tokens_out) == (27, 15)
    assert response.cost_usd == pytest.approx(price_for(load_prices(), SUPER).cost(27, 15))


@pytest.mark.parametrize("field", ["reasoning", "reasoning_content"])
async def test_either_reasoning_field_is_hidden_and_counts_as_separated(field: str) -> None:
    # Nano names it ``reasoning``, Ultra ``reasoning_content``, Super sends both (live).
    secret: dict[str, Any] = {field: "private plan: 17 times 3"}
    plain = nebius(answer(reply("\n\n51", **secret)))
    assert (await plain.complete(ModelRequest(prompt="hi"))).text == "51"
    # Thinking on, cut after separated reasoning: the partial answer stays an answer.
    cut = nebius(answer(reply("There are 5", finish="length", reasoning_tokens=10, **secret)),
                 think=True)
    assert (await cut.complete(ModelRequest(prompt="hi"))).text == "There are 5"
    # Cut with nothing to show: the distinct error, which never carries the reasoning.
    empty = nebius(answer(reply("", finish="length", reasoning_tokens=15, **secret)),
                   think=True)
    with pytest.raises(AdapterError) as caught:
        await empty.complete(ModelRequest(prompt="hi"))
    assert str(caught.value) == REASONING_CUT and "private" not in str(caught.value)


@pytest.mark.parametrize("content", ["", " \n\n ", None])
async def test_a_blank_reply_cut_at_the_cap_is_reasoning_cut_even_with_thinking_off(
    content: Any,
) -> None:
    adapter = nebius(answer(reply(content, finish="length", reasoning_tokens=None)))
    with pytest.raises(AdapterError, match=f"^{re.escape(REASONING_CUT)}$"):
        await adapter.complete(ModelRequest(prompt="hi", max_output_tokens=64))


@pytest.mark.parametrize(
    ("reasoning_tokens", "cut"),
    [(14, False), (15, True), (16, True), (True, False), (15.0, False), ("15", False)],
)
async def test_the_usage_cut_needs_an_integer_reasoning_count_covering_every_token(
    reasoning_tokens: Any, cut: bool
) -> None:
    adapter = nebius(answer(reply("51", reasoning_tokens=reasoning_tokens)))
    if cut:
        with pytest.raises(AdapterError, match=f"^{re.escape(REASONING_CUT)}$"):
            await adapter.complete(ModelRequest(prompt="hi"))
    else:
        assert (await adapter.complete(ModelRequest(prompt="hi"))).text == "51"


async def test_leading_blank_lines_and_one_think_block_are_dropped() -> None:
    body = reply('\n\n<think>\nweigh it\n</think>\n\n{"a": 1}', reasoning_tokens=None)
    assert (await nebius(answer(body)).complete(ModelRequest(prompt="hi"))).text == '{"a": 1}'


async def test_reasoning_after_a_template_opened_think_tag_is_not_the_answer() -> None:
    """Nemotron 3's chat templates (Nano and Ultra, checked 2026-10-03) end the prompt
    with ``<think>\\n`` when thinking is on, so a server without a reasoning parser (the
    case ``_Visible`` exists for) returns the reasoning with only its closing tag."""
    content = "The user wants 17*3, which is 51.\n</think>\n\n51"
    adapter = nebius(answer(reply(content, model=ULTRA, reasoning_tokens=None)), ULTRA,
                     think=True)
    assert (await adapter.complete(ModelRequest(prompt="hi"))).text == "51"


async def test_live_ultra_reply_is_its_answer_and_its_reported_usage() -> None:
    live = {  # trimmed from a live reply (2026-10-03, thinking on, max_tokens 512)
        "id": "chatcmpl-9716e1f60c1cfef5", "object": "chat.completion", "model": ULTRA,
        "choices": [{"finish_reason": "stop", "index": 0, "message": {
            "content": "51", "refusal": None, "role": "assistant", "tool_calls": None,
            "reasoning_content": "The user is asking for the product of 17 and 3.\n"
                                 "17 * 3 = 51.\nThe user requested \"just the number\"."}}],
        "usage": {"completion_tokens": 37, "prompt_tokens": 30, "total_tokens": 67,
                  "completion_tokens_details": {"reasoning_tokens": 33},
                  "prompt_tokens_details": {"cached_tokens": 0},
                  "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 30},
    }
    response = await nebius(answer(live), ULTRA, think=True).complete(
        ModelRequest(prompt="What is 17*3? Reply with just the number."))
    assert (response.text, response.tokens_in, response.tokens_out) == ("51", 30, 37)
    assert response.cost_usd == pytest.approx((30 * 1.0 + 37 * 3.0) / 1e6)


# -- the request body ----------------------------------------------------------------


@pytest.mark.parametrize("think", [None, False, True])
async def test_every_body_carries_the_thinking_switch_and_the_quoted_cap(
    think: bool | None,
) -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if body["stream"]:
            return streamed(sse(chunk(ULTRA, "stop", content="ok"), usage_chunk(2, model=ULTRA)))
        return httpx.Response(200, json=reply("ok", model=ULTRA))

    adapter = nebius(handler, ULTRA, think=think)
    await adapter.complete(ModelRequest(prompt="hi"))
    await adapter.complete(ModelRequest(prompt="hi", response_schema={"type": "object"}))
    await collect(adapter, cap=None)
    plain, schema, stream = bodies
    thinking = think is True
    assert plain["chat_template_kwargs"] == {"enable_thinking": thinking}
    assert schema["chat_template_kwargs"] == {"enable_thinking": False}  # never with a schema
    assert stream["chat_template_kwargs"] == {"enable_thinking": thinking}
    capacity = adapter.quote(ModelRequest(prompt="hi")).tokens_out
    assert plain["max_tokens"] == schema["max_tokens"] == stream["max_tokens"] == capacity
    for body in bodies:
        assert not {"reasoning_effort", "guided_json", "max_completion_tokens"} & set(body)


async def test_a_schema_call_with_thinking_on_keeps_its_cut_json_as_text() -> None:
    # The schema call ran with thinking off, so its cut reply is a cut answer.
    body = reply('{"color": "bl', model=ULTRA, finish="length")
    adapter = nebius(answer(body), ULTRA, think=True)
    response = await adapter.complete(
        ModelRequest(prompt="hi", response_schema={"type": "object"}))
    assert response.text == '{"color": "bl'


# -- retries ---------------------------------------------------------------------------


async def test_a_retry_after_date_that_overflows_falls_back_to_the_backoff(
    waits: list[float],
) -> None:
    script = Script(httpx.Response(429, headers={"Retry-After": OVERFLOWING_DATE}),
                    httpx.Response(200, json=reply("ok")))
    response = await nebius(script).complete(ModelRequest(prompt="hi"))
    assert response.text == "ok" and waits == [1.0]


async def test_stream_retry_after_date_that_overflows_falls_back_to_the_backoff(
    waits: list[float],
) -> None:
    body = sse(chunk(finish="stop", content="ok"), usage_chunk(2))
    script = Script(httpx.Response(429, headers={"Retry-After": OVERFLOWING_DATE}),
                    lambda: streamed(body))
    events = await collect(nebius(script), cap=None)
    assert shown(events) == "ok" and waits == [1.0]


@pytest.mark.parametrize(
    ("value", "wait"),
    [("-5", 0.0), ("0", 0.0), ("1.5", 1.5), ("soon", 1.0), ("", 1.0), ("   ", 1.0),
     ("inf", RETRY_WAIT_S), ("1e309", RETRY_WAIT_S), ("999", RETRY_WAIT_S)],
)
def test_retry_after_values_are_bounded(value: str, wait: float) -> None:
    response = httpx.Response(429, headers={"Retry-After": value})
    assert http_module._retry_wait(response, 0) == wait


def test_retry_after_dates_in_the_past_or_far_ahead_are_bounded() -> None:
    now = datetime.now(UTC)

    def wait(value: str) -> float:
        return http_module._retry_wait(httpx.Response(429, headers={"Retry-After": value}), 1)

    assert wait(email.utils.format_datetime(now - timedelta(hours=1), usegmt=True)) == 0.0
    assert wait(email.utils.format_datetime(now + timedelta(hours=1), usegmt=True)) == (
        RETRY_WAIT_S)
    # No zone: read as UTC, 30 s ahead, so capped.
    assert wait((now + timedelta(seconds=30)).strftime("%a, %d %b %Y %H:%M:%S")) == RETRY_WAIT_S


async def test_one_busy_answer_without_retry_after_then_success(waits: list[float]) -> None:
    once = Script(httpx.Response(429), httpx.Response(200, json=reply("ok")))
    assert (await nebius(once).complete(ModelRequest(prompt="hi"))).text == "ok"
    body = sse(chunk(finish="stop", content="ok"), usage_chunk(2))
    stream = Script(httpx.Response(503), lambda: streamed(body))
    assert shown(await collect(nebius(stream), cap=None)) == "ok"
    assert waits == [1.0, 1.0]
    assert once.bodies[0] == once.bodies[1] and stream.bodies[0] == stream.bodies[1]


async def test_three_busy_answers_fail_cleanly_after_two_identical_retries(
    waits: list[float],
) -> None:
    script = Script(*(httpx.Response(429, text="quota for project-77 exhausted")
                      for _ in range(3)))
    with pytest.raises(AdapterError) as caught:
        await nebius(script).complete(ModelRequest(prompt="hi"))
    assert len(script.bodies) == 3 and len(set(script.bodies)) == 1
    assert waits == [1.0, 2.0] and "project-77" not in str(caught.value)
    waits.clear()
    stream = Script(*(httpx.Response(529, content=b"project-77 overloaded") for _ in range(3)))
    with pytest.raises(AdapterError, match=r"^Provider request failed \(HTTP 529\)$"):
        await collect(nebius(stream), cap=None)
    assert len(stream.bodies) == 3 and len(set(stream.bodies)) == 1 and waits == [1.0, 2.0]


async def test_exhausted_rate_limit_retries_are_not_reported_as_a_login_problem(
    waits: list[float],
) -> None:
    script = Script(*(httpx.Response(429) for _ in range(3)))
    with pytest.raises(AdapterError) as caught:
        await nebius(script).complete(ModelRequest(prompt="hi"))
    code = code_for(caught.value)
    assert code != "provider_not_logged_in", (code, explain(code))


@pytest.mark.parametrize("status", [400, 401, 402, 404, 408, 500, 502, 504])
async def test_other_statuses_are_never_retried(status: int, waits: list[float]) -> None:
    script = Script(httpx.Response(status), httpx.Response(200, json=reply("ok")))
    with pytest.raises(AdapterError):
        await nebius(script).complete(ModelRequest(prompt="hi"))
    stream = Script(httpx.Response(status), httpx.Response(200, json=reply("ok")))
    with pytest.raises(AdapterError, match=rf"HTTP {status}\)$"):
        await collect(nebius(stream), cap=None)
    assert len(script.bodies) == len(stream.bodies) == 1 and waits == []


async def test_no_retry_once_a_stream_has_started(waits: list[float]) -> None:
    broken = sse(chunk(content="Hel"), {"error": {"type": "rate_limit_error"}}, done=False)
    script = Script(lambda: streamed(broken),
                    lambda: streamed(sse(chunk(finish="stop", content="ok"))))
    with pytest.raises(AdapterError, match=r"\(rate_limit_error\)$"):
        await collect(nebius(script), cap=None)
    assert len(script.bodies) == 1 and waits == []


async def test_a_read_timeout_is_an_adapter_error_and_is_never_retried(
    waits: list[float],
) -> None:
    calls: list[str] = []

    def slow(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(AdapterError):
        await nebius(slow).complete(ModelRequest(prompt="hi"))
    with pytest.raises(AdapterError):
        await collect(nebius(slow), cap=None)
    assert len(calls) == 2 and waits == []


async def test_a_retry_wait_is_cut_short_by_the_callers_deadline() -> None:
    script = Script(httpx.Response(429, headers={"Retry-After": "5"}),
                    httpx.Response(200, json=reply("ok")))
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(nebius(script).complete(ModelRequest(prompt="hi")), 0.2)
    assert time.monotonic() - started < 2.0 and len(script.bodies) == 1


async def test_a_reasoning_cut_call_keeps_its_reservation_as_spend() -> None:
    adapter = nebius(answer(reply("", finish="length", reasoning_tokens=64)), think=True)
    quote = adapter.quote(ModelRequest(prompt="hi", max_output_tokens=64))
    context = RunContext(adapter, Budget(cost_usd=1.0, compute_s=60))
    with pytest.raises(AdapterError) as caught:
        await context.call("hi", 64)
    assert str(caught.value) == REASONING_CUT and code_for(caught.value) == (
        "provider_reasoning_cut")
    assert context.cost_usd == pytest.approx(quote.cost_usd)  # billed by Nebius, kept here


# -- stream_chat --------------------------------------------------------------------------


async def test_stream_hides_every_reasoning_field_and_estimates_each_once() -> None:
    body = sse(
        chunk(content=""),
        chunk(reasoning="abcde", reasoning_content="abcde"),  # Super: the same text twice
        chunk(reasoning_content="fghij"),  # Ultra
        chunk(reasoning="klmno"),  # Nano
        chunk(finish="stop", content="\n\n51"),
    )  # no usage chunk: the estimate counts what was generated
    events = await collect(nebius(lambda request: streamed(body), think=True))
    assert shown(events) == "51"
    usage = usage_of(events)
    assert usage.estimated and usage.tokens_out == len("abcdefghijklmno") + len("\n\n51")


async def test_live_super_stream_replays_to_its_answer_and_reported_usage() -> None:
    def thought(text: str) -> dict[str, Any]:
        return chunk(reasoning=text, reasoning_content=text)

    body = sse(  # trimmed from a live stream (2026-10-03, thinking on, max_tokens 512)
        chunk(role="assistant", content=""),
        thought("User"), thought(' asks: "What'), thought(" is 17*3?"),
        chunk(content="\n\n51"),
        chunk(finish="stop"),
        {"model": SUPER, "choices": [], "usage": {
            "prompt_tokens": 34, "total_tokens": 69, "completion_tokens": 35,
            "prompt_tokens_details": {"cached_tokens": 0, "created_cache_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 30},
            "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 34}},
    )
    events = await collect(nebius(lambda request: streamed(body, size=11), think=True), cap=512)
    assert shown(events) == "51" and events[-1] == DoneEvent("end_turn")
    usage = usage_of(events)
    assert (usage.tokens_in, usage.tokens_out, usage.estimated) == (34, 35, False)


async def test_stream_reasoning_then_the_cap_with_no_answer_shows_nothing() -> None:
    body = sse(chunk(ULTRA, reasoning_content="step one"),
               chunk(ULTRA, "length", reasoning_content=" step two"),
               usage_chunk(64, 64, model=ULTRA))
    adapter = nebius(lambda request: streamed(body), ULTRA, think=True)
    seen = [event async for event in adapter.stream_chat("", HI, (), 64)]
    # A cut reply with nothing to show: its reported usage, then max_tokens.
    assert [type(event) for event in seen] == [UsageEvent, DoneEvent]
    assert seen[-1] == DoneEvent("max_tokens") and usage_of(seen).tokens_out == 64


async def test_stream_tool_calls_survive_usage_that_counts_every_token_as_reasoning() -> None:
    call = {"index": 0, "id": TOOL_ID, "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "a"}'}}
    body = sse(chunk(reasoning="read it"), chunk(tool_calls=[call]),
               chunk(finish="tool_calls", content=None), usage_chunk(20, 20))
    events = await collect(nebius(lambda request: streamed(body), think=True), tools=TOOLS)
    assert [e for e in events if isinstance(e, ToolCallEvent)] == [
        ToolCallEvent(TOOL_ID, "read_file", {"path": "a"})]
    assert events[-1] == DoneEvent("tool_use")


async def test_stream_think_block_split_one_character_at_a_time() -> None:
    text = "\n<think>plan</think>\n\nHi there"
    body = sse(*(chunk(content=char) for char in text), chunk(finish="stop"), usage_chunk(9))
    events = await collect(nebius(lambda request: streamed(body, size=3)))
    assert shown(events) == "Hi there"


@pytest.mark.parametrize(
    ("parts", "text"),
    [(["<th"], "<th"), (["<", "p>Hi"], "<p>Hi"), (["<thinking>", " aloud"], "<thinking> aloud"),
     (["\n", "  ", "<", "t", "h"], "<th")],
)
async def test_stream_text_that_only_looks_like_a_think_tag_is_kept(
    parts: list[str], text: str
) -> None:
    body = sse(*(chunk(content=part) for part in parts), chunk(finish="stop"), usage_chunk(4))
    assert shown(await collect(nebius(lambda request: streamed(body)))) == text


async def test_stream_reasoning_after_a_template_opened_think_tag_is_not_shown() -> None:
    body = sse(chunk(ULTRA, content="The user wants 17*3,"),
               chunk(ULTRA, content=" which is 51.\n</"), chunk(ULTRA, content="think>\n\n51"),
               chunk(ULTRA, "stop"), usage_chunk(20, model=ULTRA))
    events = await collect(nebius(lambda request: streamed(body), ULTRA, think=True))
    assert shown(events) == "51"


# -- timeouts from the profile ------------------------------------------------------------


async def test_each_role_timeout_reaches_its_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEBIUS_API_KEY", "fake-nebius-key")
    profile = load_profile(write_json(tmp_path / "p.json", {
        "workspace": "w",
        "models": {SUPER: ENDPOINT, LIGHTNING: {**ENDPOINT, "timeout_s": 45}},
        "forge_agent": {**ENDPOINT, "model": ULTRA, "think": True, "timeout_s": 600},
    }))
    read: dict[str, float] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        read[model] = request.extensions["timeout"]["read"]
        assert request.headers["Authorization"] == "Bearer fake-nebius-key"
        return httpx.Response(200, json=reply("ok", model=model))

    assert profile.forge_agent is not None
    for model, config in ((SUPER, profile.models[SUPER]), (LIGHTNING, profile.models[LIGHTNING]),
                          (ULTRA, profile.forge_agent)):
        adapter = profiles._adapter(profile, model, config)
        assert isinstance(adapter, OpenAICompatibleAdapter) and adapter.dialect == "nebius"
        adapter._transport = httpx.MockTransport(handler)
        await adapter.complete(ModelRequest(prompt="hi"))
    assert read == {SUPER: 120.0, LIGHTNING: 45.0, ULTRA: 600.0}


# -- discovery ------------------------------------------------------------------------------


async def test_nebius_listing_survives_odd_rows(tmp_path: Path) -> None:
    def priced(prompt: Any, completion: Any, modality: str = "text->text") -> dict[str, Any]:
        return {"architecture": {"modality": modality},
                "pricing": {"prompt": prompt, "completion": completion}}

    rows: list[Any] = [
        {"id": SUPER, "context_length": 262144, **priced(3e-7, 9e-7)},  # numbers, not strings
        {"id": NANO, **priced("0.00000006", "0.00000024")},
        {"id": "a/variable", **priced("-1", "-1")},  # a router's "varies": never guessed
        {"id": "a/nan", **priced("nan", "0.000001")},
        {"id": "a/inf", **priced("Infinity", "0.000001")},
        {"id": "a/blank", **priced("  ", "0.000001")},
        {"id": "a/no-pricing", "architecture": {"modality": "text->text"}},
        {"id": "a/list-pricing", "pricing": ["0.1", "0.2"]},
        {"id": "a/vision", **priced("0.000001", "0.000002", "text+image->text")},
        {"id": "a/arch-string", "architecture": "text->embedding"},  # unreadable: kept
        {"id": "a/image-gen", **priced("0.000001", "0", "text->image")},
        {"id": "a/embedder", **priced("0.00000001", "0", "text->embedding")},
        {"id": "a/ctx-string", "context_length": "262144"},
        {"id": "a/ctx-bool", "context_length": True},
        {"id": "a/ctx-zero", "context_length": 0},
        {"id": "  a/padded  "},
        {"id": "a/dup", **priced("0.000001", "0.000002")},
        {"id": "a/dup", **priced("0.000003", "0.000004")},
        {"id": ""}, {"id": "   "}, {"id": 42}, {"id": None}, {"id": "x" * 300},
        "not a row", ["x"], None,
    ]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"object": "list", "data": rows})

    host = Host(which=lambda name: None, env={}, home=tmp_path,
                transport=httpx.MockTransport(handler))
    found = await list_models(entry("nebius"), api_key="k", host=host,
                              base_url="https://api.tokenfactory.us-central1.nebius.com/v1/")
    assert found.ok
    assert str(seen[0].url) == (
        "https://api.tokenfactory.us-central1.nebius.com/v1/models?verbose=true")
    by_id = {model.id: model for model in found.models}
    assert set(by_id) == {
        SUPER, NANO, "a/variable", "a/nan", "a/inf", "a/blank", "a/no-pricing",
        "a/list-pricing", "a/vision", "a/arch-string", "a/ctx-string", "a/ctx-bool",
        "a/ctx-zero", "a/padded", "a/dup",
    }
    rates = {name: (m.price.input_per_million, m.price.output_per_million)
             for name, m in by_id.items() if m.price is not None}
    assert rates == {SUPER: (0.3, 0.9), NANO: (0.06, 0.24), "a/vision": (1.0, 2.0),
                     "a/dup": (3.0, 4.0)}
    assert by_id[SUPER].context == 262144
    assert all(by_id[name].context is None for name in ("a/ctx-string", "a/ctx-bool",
                                                         "a/ctx-zero"))


def test_openrouter_keeps_its_own_list_price_for_an_id_nebius_also_serves(
    tmp_path: Path,
) -> None:
    # OpenRouter's public list has this exact id at $0.08/$0.45 per M with 235,929 output
    # tokens (checked 2026-10-03); the bundled entry is Nebius's price on Nebius's endpoint.
    ws = tmp_path / "ws"
    path = ws / "session-profile.json"
    credentials.set_key("openrouter", "or-test-key", ws)
    listed = per_token_price(8e-8, 4.5e-7, output_capacity_tokens=235929,
                             allowed_response_models=(SUPER,))
    assert listed is not None
    host = Host(which=lambda name: None, env={}, home=tmp_path,
                transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    register_model(path, "openrouter", SUPER, price=listed, host=host)
    profile = load_profile(path)
    adapter = profiles._adapter(profile, SUPER, profile.models[SUPER])
    assert isinstance(adapter, OpenAICompatibleAdapter) and adapter.dialect == "openai"
    price = adapter._price
    assert (price.input_per_million, price.output_per_million) == (0.08, 0.45)
    assert price.output_capacity_tokens == 235929


# -- unpriced engineers ---------------------------------------------------------------------


def _pack(tmp_path: Path, **updates: Any) -> Path:
    payload: dict[str, Any] = {
        "workspace": "w",
        "models": {model: dict(ENDPOINT) for model in (NANO, LIGHTNING, SUPER, ULTRA)},
        "inference": {**ENDPOINT, "model": SUPER},
        "forge_agent": {**ENDPOINT, "model": ULTRA, "think": True, "timeout_s": 600},
        "compile": {"model_ladder": [LIGHTNING, SUPER]},
    }
    payload.update(updates)
    return write_json(tmp_path / "p.json", payload)


def test_the_nebius_role_pack_engineers_are_priced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEBIUS_API_KEY", "fake-nebius-key")
    profile = load_profile(_pack(tmp_path))
    services = build_services(profile, profile.workspace)
    try:
        engineer, stage0 = services.forge_adapter, services.compiler.inference_adapter
        assert engineer is not None and stage0 is not None
        assert (engineer.model, stage0.model) == (ULTRA, SUPER)
        assert unpriced_engineers(services, engineer, stage0) == []
    finally:
        services.close()


def test_an_unbundled_nebius_engineer_is_named_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEBIUS_API_KEY", "fake-nebius-key")
    retired = "nvidia/Llama-3_1-Nemotron-Ultra-253B-v1"
    write_price_entries(tmp_path / "unpriced.json", {retired: unpriced_price()})
    role = {**ENDPOINT, "model": retired, "prices": "unpriced.json"}
    profile = load_profile(_pack(tmp_path, inference=role, forge_agent=role))
    services = build_services(profile, profile.workspace)
    try:
        engineer, stage0 = services.forge_adapter, services.compiler.inference_adapter
        assert engineer is not None and stage0 is not None
        assert unpriced_engineers(services, engineer, stage0) == [retired]
    finally:
        services.close()


def test_an_engineer_is_judged_by_the_entry_its_adapter_was_built_from(
    tmp_path: Path,
) -> None:
    # The conductor's model engineers when no forge_agent is set (``build._forge_agent``);
    # its ``models`` entry is unpriced, while an unrelated ``rewrite`` entry of the same
    # name declares a price.
    declared = Price(input_per_million=1, output_per_million=2, output_capacity_tokens=4096,
                     input_overhead_tokens=0)
    write_price_entries(tmp_path / "unpriced.json", {"mystery": unpriced_price()})
    write_price_entries(tmp_path / "declared.json", {"mystery": declared})
    remote = {"provider": "openai", "base_url": "http://127.0.0.1:9/v1"}
    profile = load_profile(write_json(tmp_path / "p.json", {
        "workspace": "w",
        "models": {"mystery": {**remote, "prices": "unpriced.json"}},
        "rewrite": {**remote, "model": "mystery", "prices": "declared.json"},
    }))
    services = build_services(profile, profile.workspace)
    try:
        engineer = services.adapter_factory("mystery")  # what the Forge session runs on
        assert engineer.quote(ModelRequest(prompt="x")).cost_usd == 0.0  # no dollar bound
        assert unpriced_engineers(services, engineer, None) == ["mystery"]
    finally:
        services.close()
