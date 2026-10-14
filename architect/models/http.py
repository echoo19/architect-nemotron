"""Small HTTP adapters. Network access only occurs in ``complete``.

Image inputs are reserved in ``quote`` at the model's declared per-image
ceiling (``pricing.image_reservation``) before any request is built, so a
model without an image price refuses in ``quote`` and never reaches the
network. Text-only requests produce byte-identical bodies to the pre-image
adapters; the image shapes are added only when ``request.images`` is non-empty.

``request.response_schema`` (constrained decoding) is sent only when set: as
Ollama's ``format`` and as an OpenAI-compatible strict ``json_schema``
``response_format``. The Anthropic adapter ignores it. A request without one
produces exactly the body it produced before the option existed.

The OpenAI-compatible adapter's ``nebius`` dialect (Nebius Token Factory) is opt-in;
the default dialect's bodies are unchanged.
"""

import asyncio
import email.utils
import json
import math
import os
import re
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

import httpx
from pydantic import JsonValue, TypeAdapter, ValidationError

from .chat import (
    CHAT_MESSAGE_OVERHEAD_TOKENS,
    CHAT_TOOLS_OVERHEAD_TOKENS,
    ChatEvent,
    ChatMessage,
    DoneEvent,
    StopReason,
    TextDeltaEvent,
    ToolCallEvent,
    ToolSpec,
    UsageEvent,
    chat_input_bytes,
)
from .images import b64, data_url
from .pricing import image_reservation, load_prices, price_for
from .relay import OUTAGES, RelayRefusal, Unreachable, refusal
from .types import MODEL_ID_PATTERN, AdapterError, ModelRequest, ModelResponse, UsageQuote

# Local vision models need longer than the text default to ingest a page.
OLLAMA_IMAGE_TIMEOUT_S = 180.0
_CONTEXT_EXCEEDED = b"exceed_context_size_error"
# Fixed per adapter: a changed num_ctx makes Ollama reload the model.
OLLAMA_DEFAULT_NUM_CTX = 32_768
# Nebius Token Factory: reasoning models answer slowly, and a busy or rate-limited
# server (HTTP 429, 503, 529) says when to come back.
NEBIUS_TIMEOUT_S = 120.0
NEBIUS_RETRIES = 2
RETRY_STATUSES = frozenset({429, 503, 529})
RETRY_WAIT_S = 20.0  # the longest wait honoured before one retry
_THINK_OPEN, _THINK_CLOSE = "<think>", "</think>"
REASONING_CUT = "Provider reply ended inside its reasoning, before any answer"

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_sleep = asyncio.sleep  # a seam: tests wait no real time between retries


def _object(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise AdapterError("Provider response must contain an object")
    return value


def _array(value: JsonValue) -> list[JsonValue]:
    if not isinstance(value, list):
        raise AdapterError("Provider response must contain an array")
    return value


def _tokens(value: JsonValue) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AdapterError("Provider response is missing valid token usage")
    return value


def _optional_tokens(usage: dict[str, JsonValue], field: str) -> int:
    value = usage.get(field)
    return 0 if value is None else _tokens(value)


def _text(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise AdapterError("Provider response is missing text")
    return value


_COMPLETION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def completion_identifier(value: object) -> str | None:
    """The provider's id for a completion (``chatcmpl-...``, ``msg_...``) when it is a
    short plain token; anything else is dropped, never an error."""
    if isinstance(value, str) and _COMPLETION_ID.fullmatch(value):
        return value
    return None


def _retry_wait(response: httpx.Response, attempt: int) -> float:
    """Seconds before retry ``attempt`` (0-based): ``Retry-After`` (seconds or an HTTP
    date), else 1 s then 2 s; never more than ``RETRY_WAIT_S``. A value that is neither
    (an unreadable date, or a year too large for a date) leaves the backoff."""
    wait = float(2**attempt)
    value = response.headers.get("retry-after", "").strip()
    if value:
        try:
            wait = float(value)
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(value)
            except (TypeError, ValueError, OverflowError):
                pass
            else:
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
                wait = (when - datetime.now(UTC)).total_seconds()
    if not math.isfinite(wait):
        wait = RETRY_WAIT_S
    return min(max(wait, 0.0), RETRY_WAIT_S)


def _retried(status: int, refused: str | None) -> bool:
    """Whether an answer earns the same request again after a wait: a busy or rate-limited
    server, or the judges' relay out of reach of its ledger for a moment (``OUTAGES``).
    Any other relay refusal (``refused`` is its code) will not change, so it is never
    retried."""
    return status in RETRY_STATUSES and (refused is None or refused in OUTAGES)


# ``_Visible`` states: the reply's start is undecided; inside reasoning a template opened
# (its text is the answer if no closing tag ever comes); inside a leading ``<think>``
# block (never the answer); answer text is flowing.
_START, _INLINE, _BLOCK, _SHOWN = range(4)


class _Visible:
    """The answer part of a Nebius reply, fed whole or delta by delta as it streams.

    Leading whitespace goes, and so does reasoning a server without a reasoning parser
    leaves inline (Token Factory sends it beside the content): one leading
    ``<think>...</think>`` block or, when ``opened``, everything up to the first
    ``</think>``. ``opened`` is thinking on: Nemotron 3's chat templates end the prompt
    with ``<think>``, so such a reply carries only the closing tag. ``separated`` ends
    that wait (the reasoning arrives in its own field, so no closing tag will come).
    Unclosed reasoning is never part of the answer. Trailing whitespace is held back until
    more text follows, so ``finish`` can drop it before tool calls. Work is linear in the
    reply: a closing tag split across deltas is found from a short tail.
    """

    def __init__(self, *, opened: bool = False) -> None:
        self._state = _INLINE if opened else _START
        self._head = ""  # start: text kept while it may still open a block ("<th")
        self._kept: list[str] = []  # inline: the text, in case no closing tag comes
        self._tail = ""  # inline or block: the last characters seen, for a split tag
        self._skipped = False  # the leading reasoning is gone
        self._space = ""  # trailing whitespace of the text shown so far

    def separated(self) -> None:
        if self._state == _INLINE:
            self._state, self._head = _START, "".join(self._kept)
            self._kept, self._tail = [], ""

    def feed(self, text: str) -> str:
        shown = self._advance(text)
        body = shown.rstrip()
        if not body:  # nothing, or whitespace after text: wait for what follows
            self._space += shown
            return ""
        shown, self._space = self._space + body, shown[len(body) :]
        return shown

    def _advance(self, text: str) -> str:
        while True:
            if self._state == _SHOWN:
                return text
            if self._state != _START:  # inside reasoning: wait for its closing tag
                probe = self._tail + text
                end = probe.find(_THINK_CLOSE)
                if end < 0:
                    if self._state == _INLINE:
                        self._kept.append(text)
                    self._tail = probe[1 - len(_THINK_CLOSE) :]
                    return ""
                text = probe[end + len(_THINK_CLOSE) :]
                self._state, self._kept, self._tail, self._skipped = _START, [], "", True
            head, self._head = (self._head + text).lstrip(), ""
            if not self._skipped and head.startswith(_THINK_OPEN):
                self._state, text = _BLOCK, head[len(_THINK_OPEN) :]
                continue
            if not self._skipped and head and _THINK_OPEN.startswith(head):
                self._head = head  # "<", "<th": may still open a block
                return ""
            if head:
                self._state = _SHOWN
            return head

    def finish(self, *, cut: bool, calls: bool) -> str:
        """What is still held when the reply ends. A partial tag is text and a block is
        not; text still waiting for its closing tag is the answer only when the reply was
        not ``cut`` at the cap (it then never reasoned inline). Trailing whitespace goes
        before tool ``calls``."""
        if self._state == _INLINE and not cut:
            self.separated()
        held = self._head.lstrip() if self._state == _START else ""
        if held.startswith(_THINK_OPEN):
            held = ""
        space, self._head, self._space = self._space, "", ""
        return held.rstrip() if calls else space + held


def _usage_details(usage: dict[str, JsonValue], *, nebius: bool) -> tuple[int, int]:
    """``(cache-hit, reasoning)`` tokens of an OpenAI-shaped usage object, 0 when absent.

    Cache hits come from ``prompt_tokens_details.cached_tokens``. Nebius reports them on
    Lightning only as ``prompt_cache_hit_tokens`` (live, 2026-10-03), so that dialect also
    reads it, at most the prompt tokens. Reasoning comes from
    ``completion_tokens_details.reasoning_tokens``; a malformed count is 0, never an error.
    """
    details = usage.get("prompt_tokens_details")
    cached = _optional_tokens(details, "cached_tokens") if isinstance(details, dict) else 0
    if nebius and not cached:
        hit = usage.get("prompt_cache_hit_tokens")
        prompt = usage.get("prompt_tokens")
        if _count(hit) and _count(prompt):
            cached = min(cast(int, hit), cast(int, prompt))
    spent = usage.get("completion_tokens_details")
    reasoning = spent.get("reasoning_tokens") if isinstance(spent, dict) else None
    return cached, cast(int, reasoning) if _count(reasoning) else 0


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _cut_in_reasoning(
    answered: bool, finish: JsonValue, usage: dict[str, JsonValue] | None
) -> bool:
    """Whether a Nebius reply ended inside its reasoning, so it holds no answer.

    Cut at the cap with no answer to show (``_Visible`` drops unclosed reasoning), or
    every completion token counted as reasoning (``completion_tokens_details``, when
    reported).
    """
    if finish == "length" and not answered:
        return True
    details = usage.get("completion_tokens_details") if usage is not None else None
    reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
    completion = usage.get("completion_tokens") if usage is not None else None
    return (
        isinstance(reasoning, int)
        and isinstance(completion, int)
        and not isinstance(reasoning, bool)
        and reasoning >= completion > 0
    )


# -- native chat streaming (Phase 13) ---------------------------------------------
#
# ``stream_chat`` is additive: ``complete`` and ``quote`` are untouched. Every limit
# below fails closed with an ``AdapterError``; nothing is truncated silently.

STREAM_MAX_BYTES = 32 * 1024 * 1024  # all bytes of one streamed reply
STREAM_LINE_BYTES = 4 * 1024 * 1024  # one SSE event or NDJSON line
STREAM_ERROR_BYTES = 64 * 1024  # how much of an error body is read
STREAM_MAX_TOOL_CALLS = 64  # tool calls in one reply
STREAM_TOOL_ARGUMENT_BYTES = 1024 * 1024  # one tool call's argument JSON
STREAM_TOOL_NAME_CHARS = 256
_INVALID = "Provider request failed or returned invalid JSON"
_INCOMPLETE = "Provider response is incomplete"
_TOO_LARGE = "Provider stream exceeded its size limit"
_BAD_CALL = "Provider response has an invalid tool call"
_CALL_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_ERROR_KIND = re.compile(r"[a-z_]{1,40}")
_STOPS: dict[str, StopReason] = {
    "end_turn": "end_turn",
    "stop_sequence": "end_turn",
    "stop": "end_turn",
    "tool_use": "tool_use",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "max_tokens": "max_tokens",
    "length": "max_tokens",
    "refusal": "refusal",
    "content_filter": "refusal",
}


def _stop(reason: str, has_calls: bool) -> StopReason:
    mapped = _STOPS.get(reason, "other")
    return "tool_use" if has_calls and mapped in ("end_turn", "other") else mapped


def _event_json(data: str | bytes) -> dict[str, JsonValue]:
    try:
        return _JSON_OBJECT.validate_json(data)
    except ValueError as exc:  # pydantic's ValidationError is a ValueError
        raise AdapterError(_INVALID) from exc


def _stream_error(body: dict[str, JsonValue]) -> str:
    """A content-free message for an in-stream error: only a short error category."""
    error = body.get("error")
    kind = error.get("type") if isinstance(error, dict) else None
    if isinstance(kind, str) and _ERROR_KIND.fullmatch(kind):
        return f"Provider stream reported an error ({kind})"
    return "Provider stream reported an error"


def _index(value: JsonValue) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AdapterError(_INVALID)
    return value


def _reject_constant(name: str) -> None:
    raise ValueError(name)


def _tool_call(call_id: str, name: str, raw: JsonValue, position: int) -> ToolCallEvent:
    """A complete tool call; arguments that are not a JSON object become an error."""
    if not name or len(name) > STREAM_TOOL_NAME_CHARS:
        raise AdapterError(_BAD_CALL)
    ident = call_id if _CALL_ID.fullmatch(call_id) else f"call_{position}"
    value: JsonValue = raw
    if isinstance(raw, str):
        if not raw.strip():
            return ToolCallEvent(ident, name, {})
        try:
            value = cast(JsonValue, json.loads(raw, parse_constant=_reject_constant))
        except (ValueError, RecursionError):
            return ToolCallEvent(ident, name, {}, "invalid_arguments")
    if value is None:
        return ToolCallEvent(ident, name, {})
    if not isinstance(value, dict):
        return ToolCallEvent(ident, name, {}, "invalid_arguments")
    return ToolCallEvent(ident, name, value)


class _PendingCall:
    """A tool call whose argument JSON arrives in pieces."""

    def __init__(self, call_id: str = "", name: str = "") -> None:
        self.id = call_id
        self.name = name
        self.parts: list[str] = []
        self.size = 0

    def add(self, text: str) -> None:
        self.size += len(text.encode("utf-8"))
        if self.size > STREAM_TOOL_ARGUMENT_BYTES:
            raise AdapterError(_TOO_LARGE)
        self.parts.append(text)

    def event(self, position: int) -> ToolCallEvent:
        return _tool_call(self.id, self.name, "".join(self.parts), position)


async def _sse(lines: AsyncIterator[bytes]) -> AsyncIterator[tuple[str, str]]:
    """Server-sent events as ``(event, data)``; comments and unknown fields are skipped."""
    event = ""
    data: list[str] = []
    size = 0
    async for raw in lines:
        try:
            line = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AdapterError(_INVALID) from exc
        if not line:
            if data:
                yield event or "message", "\n".join(data)
            event, data, size = "", [], 0
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if name == "event":
            event = value
        elif name == "data":
            size += len(value)
            if size > STREAM_LINE_BYTES:
                raise AdapterError(_TOO_LARGE)
            data.append(value)
    if data:
        yield event or "message", "\n".join(data)


def _function_tools(tools: Sequence[ToolSpec]) -> list[JsonValue]:
    """OpenAI-style function tools (Chat Completions and Ollama)."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in tools
    ]


def _anthropic_messages(messages: Sequence[ChatMessage]) -> list[JsonValue]:
    """Messages API turns: same-role neighbours merge, tool results ride on user turns.

    The last block carries ``cache_control`` so a multi-step tool loop reads the
    conversation so far from the prompt cache instead of paying for it again.
    """
    turns: list[tuple[str, list[dict[str, JsonValue]]]] = []

    def add(role: str, blocks: list[dict[str, JsonValue]]) -> None:
        if not blocks:
            return
        if turns and turns[-1][0] == role:
            turns[-1][1].extend(blocks)
        else:
            turns.append((role, blocks))

    for message in messages:
        if message.role in ("user", "system"):
            if message.text.strip():
                add("user", [{"type": "text", "text": message.text}])
        elif message.role == "assistant":
            blocks: list[dict[str, JsonValue]] = []
            if message.text.strip():
                blocks.append({"type": "text", "text": message.text})
            for call in message.tool_calls:
                blocks.append(
                    {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
                )
            add("assistant", blocks)
        else:
            result: dict[str, JsonValue] = {
                "type": "tool_result",
                "tool_use_id": message.tool_call_id or "",
                "content": message.text or "(no output)",
            }
            if message.is_error:
                result["is_error"] = True
            add("user", [result])
    if turns and turns[0][0] != "user":
        turns.insert(0, ("user", [{"type": "text", "text": "(conversation continues)"}]))
    if turns and turns[-1][1][-1].get("type") in ("text", "tool_result"):
        turns[-1][1][-1]["cache_control"] = {"type": "ephemeral"}
    return [{"role": role, "content": list[JsonValue](blocks)} for role, blocks in turns]


def _openai_messages(system: str, messages: Sequence[ChatMessage]) -> list[JsonValue]:
    out: list[JsonValue] = [{"role": "system", "content": system}] if system else []
    for message in messages:
        if message.role in ("user", "system"):
            if message.text:
                out.append({"role": message.role, "content": message.text})
        elif message.role == "assistant":
            if not message.text and not message.tool_calls:
                continue
            item: dict[str, JsonValue] = {"role": "assistant", "content": message.text or None}
            if message.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                    for call in message.tool_calls
                ]
            out.append(item)
        else:
            out.append({
                "role": "tool",
                "tool_call_id": message.tool_call_id or "",
                "content": message.text or "(no output)",
            })
    return out


def _ollama_messages(system: str, messages: Sequence[ChatMessage]) -> list[JsonValue]:
    out: list[JsonValue] = [{"role": "system", "content": system}] if system else []
    for message in messages:
        if message.role in ("user", "system"):
            if message.text:
                out.append({"role": message.role, "content": message.text})
        elif message.role == "assistant":
            if not message.text and not message.tool_calls:
                continue
            item: dict[str, JsonValue] = {"role": "assistant", "content": message.text}
            if message.tool_calls:
                item["tool_calls"] = [
                    {"function": {"name": call.name, "arguments": call.arguments}}
                    for call in message.tool_calls
                ]
            out.append(item)
        else:
            out.append({
                "role": "tool",
                "content": message.text or "(no output)",
                "tool_name": message.name or "",
            })
    return out


class _HTTPAdapter:
    # Whether a request carrying ``cache_prefix`` may be billed a cache write.
    _writes_cache = False
    # Retries of a ``RETRY_STATUSES`` answer before any byte of the reply (Nebius only).
    _retries = 0

    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        prices_path: Path | None,
        transport: httpx.AsyncBaseTransport | None,
        timeout: float,
        api_model: str | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self.model = model
        # The provider's id when ``model`` is a profile alias: what is sent, priced and
        # checked against the reply. ``model`` stays what every record carries.
        self.api_model = api_model
        self._wire = model if api_model is None else api_model
        self._price = price_for(load_prices(prices_path), self._wire)
        if max_tokens is not None and (type(max_tokens) is not int or max_tokens <= 0):
            raise AdapterError("Invalid output token cap")
        self._max_tokens = max_tokens  # a ceiling on every call's cap and reservation
        if not math.isfinite(timeout) or timeout <= 0:
            raise AdapterError("Timeout must be finite and positive")
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._timeout = timeout

    def quote(self, request: ModelRequest) -> UsageQuote:
        # UTF-8 bytes, not characters/4: the latter underestimates many inputs.
        # Images reserve the configured ceiling per image; an unconfigured
        # model raises here, before any I/O.
        tokens_in = (
            len(request.prompt.encode("utf-8"))
            + self._price.input_overhead_tokens
            + image_reservation(self._price, request.images)
        )
        tokens_out = self._output_capacity(request)
        # A cached prefix and a ``cache=True`` image block both carry
        # ``cache_control``, so either may bill everything up to it as a write.
        may_write_cache = self._writes_cache and (
            request.cache_prefix is not None or any(image.cache for image in request.images)
        )
        return UsageQuote(
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=self._price.quote_cost(tokens_in, tokens_out, may_write_cache=may_write_cache),
        )

    def _check_images(self, request: ModelRequest) -> None:
        # ``quote`` already refused unconfigured models; fail closed in ``complete`` too.
        image_reservation(self._price, request.images)

    def _output_capacity(self, request: ModelRequest) -> int:
        # Reserve for the configured provider capacity, never a factory quota.
        return self._capped(request.max_output_tokens)

    def _capped(self, requested: int | None) -> int:
        """The request's cap, else the price entry's capacity, never above ``max_tokens``."""
        capacity = self._price.output_capacity_tokens if requested is None else requested
        return capacity if self._max_tokens is None else min(capacity, self._max_tokens)

    async def _post(
        self,
        path: str,
        payload: dict[str, JsonValue],
        headers: dict[str, str],
        *,
        deadline_s: float | None = None,
    ) -> dict[str, JsonValue]:
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout if deadline_s is None else deadline_s,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(self._base_url + path, json=payload, headers=headers)
                refused = refusal(response.status_code, response.content)
                for attempt in range(self._retries):
                    if not _retried(response.status_code, refused):
                        break
                    await _sleep(_retry_wait(response, attempt))
                    response = await client.post(
                        self._base_url + path, json=payload, headers=headers
                    )
                    refused = refusal(response.status_code, response.content)
                if refused is not None:
                    # The judges' relay said why (a code's limit, the kill switch, or its
                    # ledger still out of reach after the retries): its code.
                    raise RelayRefusal(refused)
                if response.status_code == 400 and _CONTEXT_EXCEEDED in response.content:
                    # Ollama (llama.cpp) refuses a prompt larger than num_ctx explicitly.
                    raise AdapterError("Prompt exceeds the configured context")
                if response.status_code in RETRY_STATUSES:
                    # Rate limited or overloaded (after any retries): named as a stream
                    # names it, so it is never mistaken for a login problem.
                    raise AdapterError(f"Provider request failed (HTTP {response.status_code})")
                response.raise_for_status()
                data = _JSON_OBJECT.validate_json(response.content)
                if "model" in data:
                    reported_model = _text(data["model"])
                    if not self._price.accepts_response_model(reported_model, self._wire):
                        raise AdapterError("Provider response model does not match adapter")
                return data
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # Nothing answered at the address: say so, never "not logged in".
            raise Unreachable(self._base_url) from exc
        except (httpx.HTTPError, ValueError, ValidationError) as exc:
            raise AdapterError("Provider request failed or returned invalid JSON") from exc

    def _response(
        self,
        text: str,
        tokens_in: int,
        tokens_out: int,
        *,
        cache_write_tokens: int = 0,
        cache_read_tokens: int = 0,
        reasoning_tokens: int = 0,
        completion_id: object = None,
    ) -> ModelResponse:
        if cache_write_tokens + cache_read_tokens > tokens_in:
            raise AdapterError("Provider response is missing valid token usage")
        return ModelResponse(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=self._price.cost(
                tokens_in,
                tokens_out,
                cache_write_tokens=cache_write_tokens,
                cache_read_tokens=cache_read_tokens,
            ),
            model=self.model,
            cache_write_tokens=cache_write_tokens,
            cache_read_tokens=cache_read_tokens,
            reasoning_tokens=min(reasoning_tokens, tokens_out),
            completion_id=completion_identifier(completion_id),
        )

    # -- native chat streaming (Phase 13) -----------------------------------------

    def quote_chat(
        self,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        max_output_tokens: int | None,
    ) -> UsageQuote:
        """Reserve UTF-8 bytes of everything sent plus framing, and the output cap.

        Adapters that mark cache breakpoints (Anthropic) reserve every input token at
        the cache-write rate; a quote never assumes a cache hit.
        """
        if max_output_tokens is not None and (
            type(max_output_tokens) is not int or max_output_tokens <= 0
        ):
            raise AdapterError("Invalid output token cap")
        tokens_in = (
            chat_input_bytes(system, messages, tools)
            + self._price.input_overhead_tokens
            + CHAT_MESSAGE_OVERHEAD_TOKENS * (len(messages) + 1)
            + (CHAT_TOOLS_OVERHEAD_TOKENS if tools else 0)
        )
        tokens_out = self._capped(max_output_tokens)
        return UsageQuote(
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=self._price.quote_cost(
                tokens_in, tokens_out, may_write_cache=self._writes_cache
            ),
        )

    def _check_reported_model(self, value: JsonValue) -> None:
        if not self._price.accepts_response_model(_text(value), self._wire):
            raise AdapterError("Provider response model does not match adapter")

    def _usage(
        self,
        tokens_in: int,
        tokens_out: int,
        *,
        cache_write_tokens: int = 0,
        cache_read_tokens: int = 0,
        estimated: bool = False,
        reasoning_tokens: int = 0,
    ) -> UsageEvent:
        if cache_write_tokens + cache_read_tokens > tokens_in:
            raise AdapterError("Provider response is missing valid token usage")
        return UsageEvent(
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=self._price.cost(
                tokens_in,
                tokens_out,
                cache_write_tokens=cache_write_tokens,
                cache_read_tokens=cache_read_tokens,
            ),
            cache_write_tokens=cache_write_tokens,
            cache_read_tokens=cache_read_tokens,
            estimated=estimated,
            reasoning_tokens=min(reasoning_tokens, tokens_out),
        )

    async def _stream_lines(
        self,
        path: str,
        payload: dict[str, JsonValue],
        headers: dict[str, str],
        *,
        deadline_s: float | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """POST and yield the response body line by line (no newline), size-bounded.

        A non-2xx status reads at most ``STREAM_ERROR_BYTES`` of the body (for the
        context-size check and a judges' relay refusal, ``relay.RelayRefusal``) and raises
        ``Provider request failed (HTTP <status>)``; with ``_retries`` a ``RETRY_STATUSES``
        answer other than a relay refusal is first retried, as is the relay's ledger out
        of reach (``relay.OUTAGES``, raised as its refusal once the retries are spent),
        which can only happen before any byte of a reply was yielded. The read timeout
        applies between chunks; the caller's run deadline bounds the whole stream.
        """
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout if deadline_s is None else deadline_s,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                attempt = 0
                while True:
                    async with client.stream(
                        "POST", self._base_url + path, json=payload, headers=headers
                    ) as response:
                        if response.is_success:
                            total = 0
                            buffer = bytearray()
                            async for chunk in response.aiter_bytes():
                                total += len(chunk)
                                if total > STREAM_MAX_BYTES:
                                    raise AdapterError(_TOO_LARGE)
                                buffer += chunk
                                start = 0
                                while (end := buffer.find(b"\n", start)) >= 0:
                                    yield bytes(buffer[start:end]).rstrip(b"\r")
                                    start = end + 1
                                del buffer[:start]
                                if len(buffer) > STREAM_LINE_BYTES:
                                    raise AdapterError(_TOO_LARGE)
                            if buffer:
                                yield bytes(buffer).rstrip(b"\r")
                            return
                        status = response.status_code
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body += chunk
                            if len(body) >= STREAM_ERROR_BYTES:
                                break
                        refused = refusal(status, bytes(body))
                        if attempt >= self._retries or not _retried(status, refused):
                            if refused is not None:
                                raise RelayRefusal(refused)
                            if status == 400 and _CONTEXT_EXCEEDED in body:
                                raise AdapterError("Prompt exceeds the configured context")
                            raise AdapterError(f"Provider request failed (HTTP {status})")
                        wait = _retry_wait(response, attempt)
                    attempt += 1
                    await _sleep(wait)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise Unreachable(self._base_url) from exc
        except httpx.HTTPError as exc:
            raise AdapterError(_INVALID) from exc


class AnthropicAdapter(_HTTPAdapter):
    """Messages API adapter.

    A request's ``cache_prefix`` is sent as the first text block of the same
    user message, marked ``cache_control: ephemeral`` (5-minute TTL); the rest
    of the prompt follows as a second text block. The role and the text are
    unchanged, only the block boundary is new. Prefixes below the model's
    minimum cacheable length (512-4096 tokens) are silently not cached.

    Images go between the cached prefix and the rest of the prompt, so the
    prefix stays a byte-stable leading block and cache reads are unaffected;
    an image with ``cache=True`` carries its own ``cache_control``.

    ``response_schema`` is ignored: the body is the same with or without it, and
    the caller's own parser remains the only check on the reply.
    """

    _writes_cache = True

    def __init__(
        self,
        model: str = "claude-sonnet-4-20250514",
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        prices_path: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        super().__init__(
            model,
            base_url=base_url or os.getenv("ANTHROPIC_BASE_URL") or "https://api.anthropic.com",
            prices_path=prices_path,
            transport=transport,
            timeout=timeout,
        )
        self._api_key = api_key if api_key is not None else os.getenv("ANTHROPIC_API_KEY", "")
        if not self._api_key:
            raise AdapterError("ANTHROPIC_API_KEY is required")

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not self._api_key:
            raise AdapterError("ANTHROPIC_API_KEY is required")
        self._check_images(request)
        prefix, rest = request.split()
        content: JsonValue = request.prompt
        if prefix or request.images:
            blocks: list[JsonValue] = []
            if prefix:
                blocks.append(
                    {"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}}
                )
            for image in request.images:
                block: dict[str, JsonValue] = {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image.media_type,
                        "data": b64(image),
                    },
                }
                if image.cache:
                    block["cache_control"] = {"type": "ephemeral"}
                blocks.append(block)
            if rest:
                blocks.append({"type": "text", "text": rest})
            content = blocks
        data = await self._post(
            "/v1/messages",
            {
                "model": self.model,
                "messages": [{"role": "user", "content": content}],
                "max_tokens": self._output_capacity(request),
                "temperature": request.temperature,
                "stream": False,
            },
            {"x-api-key": self._api_key, "anthropic-version": "2023-06-01"},
        )
        usage = _object(data.get("usage"))
        # input_tokens excludes cache categories; tokens_in counts every prompt token.
        written = _optional_tokens(usage, "cache_creation_input_tokens")
        read = _optional_tokens(usage, "cache_read_input_tokens")
        tokens_in = _tokens(usage.get("input_tokens")) + written + read
        texts = [
            _text(block.get("text"))
            for item in _array(data.get("content"))
            if (block := _object(item)).get("type") == "text"
        ]
        if not texts:
            raise AdapterError("Provider response is missing text")
        return self._response(
            "".join(texts),
            tokens_in,
            _tokens(usage.get("output_tokens")),
            cache_write_tokens=written,
            cache_read_tokens=read,
            completion_id=data.get("id"),
        )

    async def stream_chat(
        self,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        max_output_tokens: int | None,
    ) -> AsyncIterator[ChatEvent]:
        """Messages API with ``stream: true`` and native ``tool_use`` blocks.

        The system prompt and the last message are cache breakpoints. Usage from
        ``message_start`` is yielded at once (partial), the final usage after
        ``message_stop``; ``message_delta`` usage is cumulative.
        """
        if not self._api_key:
            raise AdapterError("ANTHROPIC_API_KEY is required")
        quote = self.quote_chat(system, messages, tools, max_output_tokens)
        payload: dict[str, JsonValue] = {
            "model": self.model,
            "max_tokens": quote.tokens_out,
            "messages": _anthropic_messages(messages),
            "stream": True,
        }
        if system:
            payload["system"] = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]
        if tools:
            payload["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.parameters,
                }
                for tool in tools
            ]
        headers = {"x-api-key": self._api_key, "anthropic-version": "2023-06-01"}
        fresh = written = read = tokens_out = 0
        started = finished = False
        reason = ""
        calls = 0
        pending: dict[int, _PendingCall] = {}
        lines = self._stream_lines("/v1/messages", payload, headers)
        try:
            async for _, data in _sse(lines):
                body = _event_json(data)
                kind = body.get("type")
                if kind == "message_start":
                    message = _object(body.get("message"))
                    if "model" in message:
                        self._check_reported_model(message["model"])
                    usage = _object(message.get("usage"))
                    written = _optional_tokens(usage, "cache_creation_input_tokens")
                    read = _optional_tokens(usage, "cache_read_input_tokens")
                    fresh = _tokens(usage.get("input_tokens"))
                    tokens_out = _optional_tokens(usage, "output_tokens")
                    started = True
                    yield self._usage(
                        fresh + written + read, tokens_out,
                        cache_write_tokens=written, cache_read_tokens=read,
                    )
                elif kind == "content_block_start":
                    index = _index(body.get("index"))
                    block = _object(body.get("content_block"))
                    if block.get("type") == "tool_use":
                        calls += 1
                        if calls > STREAM_MAX_TOOL_CALLS:
                            raise AdapterError(_TOO_LARGE)
                        call = _PendingCall(_text(block.get("id")), _text(block.get("name")))
                        initial = block.get("input")
                        if isinstance(initial, dict) and initial:
                            call.add(json.dumps(initial, ensure_ascii=False))
                        pending[index] = call
                    elif block.get("type") == "text":
                        text = block.get("text")
                        if isinstance(text, str) and text:
                            yield TextDeltaEvent(text)
                elif kind == "content_block_delta":
                    delta = _object(body.get("delta"))
                    if delta.get("type") == "text_delta":
                        text = _text(delta.get("text"))
                        if text:
                            yield TextDeltaEvent(text)
                    elif delta.get("type") == "input_json_delta":
                        partial = pending.get(_index(body.get("index")))
                        if partial is None:
                            raise AdapterError(_INVALID)
                        partial.add(_text(delta.get("partial_json")))
                elif kind == "content_block_stop":
                    index = _index(body.get("index"))
                    done_call = pending.pop(index, None)
                    if done_call is not None:
                        yield done_call.event(index)
                elif kind == "message_delta":
                    final = body.get("delta")
                    if isinstance(final, dict) and isinstance(final.get("stop_reason"), str):
                        reason = str(final["stop_reason"])
                    counted = body.get("usage")
                    if isinstance(counted, dict):
                        if counted.get("output_tokens") is not None:
                            tokens_out = _tokens(counted["output_tokens"])
                        if counted.get("input_tokens") is not None:
                            fresh = _tokens(counted["input_tokens"])
                        if counted.get("cache_creation_input_tokens") is not None:
                            written = _tokens(counted["cache_creation_input_tokens"])
                        if counted.get("cache_read_input_tokens") is not None:
                            read = _tokens(counted["cache_read_input_tokens"])
                elif kind == "message_stop":
                    finished = True
                    break
                elif kind == "error":
                    raise AdapterError(_stream_error(body))
        finally:
            await lines.aclose()
        if not (started and finished) or pending:
            raise AdapterError(_INCOMPLETE)
        yield self._usage(
            fresh + written + read, tokens_out, cache_write_tokens=written, cache_read_tokens=read
        )
        yield DoneEvent(_stop(reason, calls > 0))


class OpenAICompatibleAdapter(_HTTPAdapter):
    """Chat Completions adapter.

    The whole prompt stays one user message: OpenAI caches long shared prompt
    prefixes automatically, so the leading ``cache_prefix`` is cached without
    any change to what the model is given. Reported ``cached_tokens`` are
    priced at the configured cache-read rate. With images the single user
    message becomes a parts list: the prompt text, then one ``image_url`` part
    per image as a data URL with ``request.image_detail``.

    A ``response_schema`` is sent as ``response_format`` of type ``json_schema``
    with ``strict: true``, appended after every other field.

    ``temperature`` and ``top_p``, when set, replace each request's sampling. The
    ``nebius`` dialect (Nebius Token Factory; ``profiles`` picks it for the ``nebius``
    credential, so dedicated endpoints get it too) also:

    - always sends ``max_tokens``: the request's cap, else the price entry's capacity,
      which is exactly what ``quote`` reserved;
    - sends ``chat_template_kwargs.enable_thinking`` from ``think``: off unless set, and
      off for a ``response_schema``. NVIDIA Nemotron reasons by default and its
      reasoning counts against ``max_tokens``; ``reasoning_effort`` is never sent, since
      ``"none"`` leaves Nano's content empty;
    - drops a reply's leading whitespace and inline reasoning (one leading ``<think>``
      block, or with thinking on everything up to ``</think>``), and its trailing
      whitespace before tool calls; accepts null content beside tool calls, never
      returns reasoning as the answer, and raises ``REASONING_CUT`` for a reply that
      ended inside its reasoning (a stream cut there instead ends as a cut reply with
      nothing shown, ``DoneEvent("max_tokens")``);
    - retries HTTP 429, 503 and 529 at most twice before any byte of a reply arrives,
      waiting what ``Retry-After`` says (at most ``RETRY_WAIT_S``). Any adapter names
      such a status once it gives up (``Provider request failed (HTTP 429)``), except
      the judges' relay's 503 for its ledger out of reach, which is retried the same way
      and then raised as that refusal (``relay.OUTAGES``). Other relay refusals are
      raised at once.

    ``api_model`` makes ``model`` an alias (a profile key such as ``ultra``): the provider
    id is what each request sends as ``model``, the price entry's key, and the name a reply
    must report (or one its entry allows). ``max_tokens`` caps every call's ``max_tokens``
    and its reservation, and once set it is always sent.

    Timeouts default to 60 s, or ``NEBIUS_TIMEOUT_S`` in the ``nebius`` dialect.
    """

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        prices_path: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float | None = None,
        dialect: Literal["openai", "nebius"] = "openai",
        think: bool | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        api_model: str | None = None,
        max_tokens: int | None = None,
    ) -> None:
        if dialect not in ("openai", "nebius"):
            raise AdapterError("Unknown OpenAI-compatible dialect")
        if think is not None and dialect != "nebius":
            raise AdapterError("think applies to the Nebius dialect only")
        if (temperature is not None and not 0 <= temperature <= 2) or (
            top_p is not None and not 0 < top_p <= 1
        ):
            raise AdapterError("Invalid sampling settings")
        if api_model is not None and (
            len(api_model) > 128 or re.fullmatch(MODEL_ID_PATTERN, api_model) is None
        ):
            raise AdapterError("Invalid provider model id")
        if timeout is None:
            timeout = NEBIUS_TIMEOUT_S if dialect == "nebius" else 60.0
        super().__init__(
            model,
            base_url=base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1",
            prices_path=prices_path,
            transport=transport,
            timeout=timeout,
            api_model=api_model,
            max_tokens=max_tokens,
        )
        # Empty key is valid for private compatible endpoints; hosted providers
        # still enforce their own authentication. No credential is sent by default.
        self._api_key = api_key if api_key is not None else os.getenv("OPENAI_API_KEY", "")
        self.dialect = dialect
        self.think = think
        self.temperature = temperature
        self.top_p = top_p
        self._retries = NEBIUS_RETRIES if dialect == "nebius" else 0

    def _options(self, payload: dict[str, JsonValue], *, schema: bool) -> None:
        """The profile's sampling overrides, then the Nebius thinking switch (appended).

        A live check (2026-10-03) found Lightning ignoring a strict ``json_schema``
        while thinking and honouring it with thinking off (Nano, Super and Ultra honour
        it either way), so a schema call never thinks.
        """
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.top_p is not None:
            payload["top_p"] = self.top_p
        if self.dialect == "nebius":
            payload["chat_template_kwargs"] = {"enable_thinking": self._thinks(schema)}

    def _thinks(self, schema: bool) -> bool:
        return self.dialect == "nebius" and bool(self.think) and not schema

    def _answer(
        self,
        message: dict[str, JsonValue],
        finish: JsonValue,
        usage: dict[str, JsonValue],
        *,
        thinking: bool,
    ) -> str:
        """The answer text of a Nebius reply (``_Visible``); reasoning fields are ignored.

        Content may be null beside tool calls, or when reasoning used up the output cap.
        Cut inside its reasoning, Super and Nano leave content empty, but Ultra and
        Lightning return the unparsed reasoning as content with no reasoning field (live,
        2026-10-03). Thinking without a reasoning field is read as a template-opened block:
        text before ``</think>`` is dropped, and text with no closing tag is the answer only
        if the reply was not cut at the cap (cut, it is ``REASONING_CUT``).
        """
        content = message.get("content")
        calls = bool(message.get("tool_calls"))
        if content is None and (calls or finish == "length"):
            content = ""
        separated = any(message.get(key) for key in ("reasoning_content", "reasoning"))
        visible = _Visible(opened=thinking and not separated)
        text = visible.feed(_text(content))
        text += visible.finish(cut=finish == "length", calls=calls)
        if _cut_in_reasoning(bool(text), finish, usage):
            raise AdapterError(REASONING_CUT)
        return text

    def _billed(self, usage: dict[str, JsonValue]) -> ModelResponse | None:
        """The reported usage of a reply the adapter refuses, priced like an answer's,
        with empty text; None when the usage itself is unreadable."""
        try:
            cached, reasoning = _usage_details(usage, nebius=self.dialect == "nebius")
            return self._response(
                "",
                _tokens(usage.get("prompt_tokens")),
                _tokens(usage.get("completion_tokens")),
                cache_read_tokens=cached,
                reasoning_tokens=reasoning,
            )
        except (AdapterError, ValidationError):
            return None

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self._check_images(request)
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        content: JsonValue = request.prompt
        if request.images:
            parts: list[JsonValue] = [{"type": "text", "text": request.prompt}]
            for image in request.images:
                parts.append({
                    "type": "image_url",
                    "image_url": {"url": data_url(image), "detail": request.image_detail},
                })
            content = parts
        payload: dict[str, JsonValue] = {
            "model": self._wire,
            "messages": [{"role": "user", "content": content}],
            "temperature": request.temperature,
            "stream": False,
            "n": 1,
        }
        if (
            request.max_output_tokens is not None
            or self.dialect == "nebius"
            or self._max_tokens is not None
        ):
            payload["max_tokens"] = self._output_capacity(request)
        if request.response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "response",
                    "strict": True,
                    "schema": request.response_schema,
                },
            }
        self._options(payload, schema=request.response_schema is not None)
        data = await self._post("/chat/completions", payload, headers)
        usage = _object(data.get("usage"))
        choices = _array(data.get("choices"))
        if len(choices) != 1:
            raise AdapterError("Expected exactly one provider completion")
        choice = _object(choices[0])
        message = _object(choice.get("message"))
        try:
            text = (
                self._answer(
                    message,
                    choice.get("finish_reason"),
                    usage,
                    thinking=self._thinks(request.response_schema is not None),
                )
                if self.dialect == "nebius"
                else _text(message.get("content"))
            )
        except AdapterError as exc:
            # The provider answered and billed this reply (``REASONING_CUT`` above all);
            # the error carries the usage it reported, so a meter can count it.
            exc.usage = self._billed(usage)
            raise
        cached, reasoning = _usage_details(usage, nebius=self.dialect == "nebius")
        return self._response(
            text,
            _tokens(usage.get("prompt_tokens")),
            _tokens(usage.get("completion_tokens")),
            cache_read_tokens=cached,
            reasoning_tokens=reasoning,
            completion_id=data.get("id"),
        )

    def _max_tokens_field(self) -> str:
        # api.openai.com rejects ``max_tokens`` for reasoning models; most compatible
        # servers (OpenRouter, Gemini, Groq, local servers) take ``max_tokens``.
        host = httpx.URL(self._base_url).host
        return "max_completion_tokens" if host == "api.openai.com" else "max_tokens"

    async def stream_chat(
        self,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        max_output_tokens: int | None,
    ) -> AsyncIterator[ChatEvent]:
        """Chat Completions with ``stream: true`` and function ``tools``.

        ``delta.tool_calls`` fragments are joined by ``index`` (by ``id`` when a
        server omits the index); calls are yielded once the stream ends. Usage comes
        from the ``stream_options.include_usage`` chunk (or Groq's ``x_groq.usage``);
        a server that reports none gets an ``estimated`` usage from the quote.

        In the ``nebius`` dialect reasoning deltas (``reasoning_content`` or
        ``reasoning``) are never shown; they are billed inside ``completion_tokens`` and
        count toward an estimate. Content is filtered by ``_Visible``: with thinking on
        and no reasoning delta yet, it is held until ``</think>`` or the end. A content
        delta that repeats the whole reasoning is the reasoning again (Ultra and
        Lightning send that when cut inside it, live 2026-10-03), so it is never shown. A
        reply cut at the cap inside its reasoning shows nothing and ends with its usage and
        ``DoneEvent("max_tokens")``, like any cut reply, so a caller can ask again. A
        finished reply without tool calls whose every token was reasoning
        (``_cut_in_reasoning``) raises ``REASONING_CUT``.
        """
        quote = self.quote_chat(system, messages, tools, max_output_tokens)
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        payload: dict[str, JsonValue] = {
            "model": self._wire,
            "messages": _openai_messages(system, messages),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = _function_tools(tools)
        nebius = self.dialect == "nebius"
        if max_output_tokens is not None or nebius or self._max_tokens is not None:
            payload[self._max_tokens_field()] = quote.tokens_out
        self._options(payload, schema=False)
        order: list[_PendingCall] = []
        by_key: dict[tuple[str, int | str], _PendingCall] = {}
        last_key: tuple[str, int | str] | None = None
        usage: dict[str, JsonValue] | None = None
        reason = ""
        finished = model_checked = shown = False
        out_bytes = 0
        visible = _Visible(opened=self._thinks(False)) if nebius else None
        thinking: list[str] = []  # the reasoning so far (nebius): repeated, it is no answer
        thought = 0  # its characters
        lines = self._stream_lines("/chat/completions", payload, headers)
        try:
            async for _, data in _sse(lines):
                if data.strip() == "[DONE]":
                    finished = True
                    break
                chunk = _event_json(data)
                if chunk.get("error") is not None:
                    raise AdapterError(_stream_error(chunk))
                reported = chunk.get("model")
                if not model_checked and isinstance(reported, str) and reported:
                    self._check_reported_model(reported)
                    model_checked = True
                counted = chunk.get("usage")
                if not isinstance(counted, dict):
                    groq = chunk.get("x_groq")
                    counted = groq.get("usage") if isinstance(groq, dict) else None
                if isinstance(counted, dict):
                    usage = counted
                choices = chunk.get("choices")
                if not isinstance(choices, list) or not choices:
                    continue
                choice = _object(choices[0])
                delta = choice.get("delta")
                if isinstance(delta, dict):
                    if visible is not None:  # Super sends both fields with the same text
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        if isinstance(reasoning, str) and reasoning:
                            visible.separated()
                            thinking.append(reasoning)
                            thought += len(reasoning)
                            out_bytes += len(reasoning.encode("utf-8"))  # billed, never shown
                    content = delta.get("content")
                    if isinstance(content, str) and content and not (
                        thought
                        and 2 * len(content) >= thought
                        and content.strip() == "".join(thinking).strip()
                    ):
                        out_bytes += len(content.encode("utf-8"))
                        if visible is not None:
                            content = visible.feed(content)
                        if content:
                            shown = True
                            yield TextDeltaEvent(content)
                    fragments = delta.get("tool_calls")
                    for fragment in fragments if isinstance(fragments, list) else []:
                        item = _object(fragment)
                        index, ident = item.get("index"), item.get("id")
                        key: tuple[str, int | str]
                        if isinstance(index, int) and not isinstance(index, bool):
                            key = ("index", index)
                        elif isinstance(ident, str) and ident:
                            key = ("id", ident)
                        else:
                            key = last_key or ("index", 0)
                        call = by_key.get(key)
                        if call is None:
                            if len(order) >= STREAM_MAX_TOOL_CALLS:
                                raise AdapterError(_TOO_LARGE)
                            call = by_key[key] = _PendingCall()
                            order.append(call)
                        last_key = key
                        if isinstance(ident, str) and ident and not call.id:
                            call.id = ident
                        function = item.get("function")
                        if isinstance(function, dict):
                            name = function.get("name")
                            if isinstance(name, str) and name and not call.name:
                                call.name = name
                            arguments = function.get("arguments")
                            if isinstance(arguments, str):
                                call.add(arguments)
                            elif isinstance(arguments, dict):
                                call.add(json.dumps(arguments, ensure_ascii=False))
                finish = choice.get("finish_reason")
                if isinstance(finish, str) and finish:
                    reason = finish
        finally:
            await lines.aclose()
        if not finished and not reason:
            raise AdapterError(_INCOMPLETE)
        if visible is not None:
            rest = visible.finish(cut=reason == "length", calls=bool(order))
            if rest:
                shown = True
                yield TextDeltaEvent(rest)
            # Cut at the cap inside its reasoning, a stream ends as any cut reply does:
            # nothing shown, its usage, then ``max_tokens``, so the caller can re-ask it
            # (``complete`` has no stop reason, so there it stays ``REASONING_CUT``). A
            # finished reply whose every token was reasoning still has no answer at all.
            cut_in_reasoning = reason == "length" and not shown
            if not order and not cut_in_reasoning and _cut_in_reasoning(shown, reason, usage):
                raise AdapterError(REASONING_CUT)
        for position, call in enumerate(order):
            out_bytes += call.size
            yield call.event(position)
        if usage is not None:
            cached, reasoning = _usage_details(usage, nebius=nebius)
            yield self._usage(
                _tokens(usage.get("prompt_tokens")),
                _tokens(usage.get("completion_tokens")),
                cache_read_tokens=cached,
                reasoning_tokens=reasoning,
            )
        else:
            yield self._usage(quote.tokens_in, min(out_bytes, quote.tokens_out), estimated=True)
        yield DoneEvent(_stop(reason, bool(order)))


class OllamaAdapter(_HTTPAdapter):
    """Local Ollama models through the chat API, so the model's own template applies.

    Instruction-tuned local models only follow instructions inside their chat
    template; the stable ``cache_prefix`` becomes the system message and the rest
    the user message. ``raw=True`` keeps the old ``/api/generate`` raw path, where
    the model sees exactly ``prompt``, for base completion models.

    Ollama's default context is small (2,048 tokens measured on 0.34.3) and it
    silently drops the start of a longer prompt, so every call sends a fixed
    ``num_ctx``. It is fixed per adapter because changing it reloads the model.
    A reply whose evaluated prompt plus output reserve reaches ``num_ctx`` is
    refused (``Prompt exceeds the configured context``): Ollama never reports more
    tokens than it read, so this check can miss a truncation but never invents one.
    Ollama reuses its KV cache for a shared leading prefix on its own, so the
    stable prefix needs no separate transport and costs nothing extra here.

    ``think`` is sent only when set, for thinking-capable models; thinking text is
    not part of the reply. Images ride on the user message as raw base64; the
    timeout for an image request is ``max(timeout, OLLAMA_IMAGE_TIMEOUT_S)``.

    A ``response_schema`` is sent as ``format`` (a JSON Schema, constrained
    decoding) on both the chat and the raw ``/api/generate`` path, appended
    after every other field; without one both bodies are unchanged.
    """

    def __init__(
        self,
        model: str = "llama3.2",
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        prices_path: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 60.0,
        num_ctx: int = OLLAMA_DEFAULT_NUM_CTX,
        think: bool | None = None,
        raw: bool = False,
    ) -> None:
        if type(num_ctx) is not int or not 512 <= num_ctx <= 1_048_576:
            raise AdapterError("Invalid Ollama context size")
        super().__init__(
            model,
            base_url=base_url or os.getenv("OLLAMA_HOST") or "http://localhost:11434",
            prices_path=prices_path,
            transport=transport,
            timeout=timeout,
        )
        self._api_key = api_key if api_key is not None else os.getenv("OLLAMA_API_KEY", "")
        self.num_ctx = num_ctx
        self.think = think
        self.raw = raw

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self._check_images(request)
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        options: dict[str, JsonValue] = {"temperature": request.temperature}
        if request.max_output_tokens is not None:
            options["num_predict"] = request.max_output_tokens
        if self.raw and not request.images:
            generate: dict[str, JsonValue] = {
                "model": self.model,
                "prompt": request.prompt,
                "raw": True,
                "stream": False,
                "options": options,
            }
            if request.response_schema is not None:
                generate["format"] = request.response_schema
            data = await self._post("/api/generate", generate, headers)
            if data.get("done") is not True:
                raise AdapterError("Provider response is incomplete")
            return self._response(
                _text(data.get("response")),
                _tokens(data.get("prompt_eval_count")),
                _tokens(data.get("eval_count")),
            )
        return await self._chat(request, options, headers)

    async def _chat(
        self, request: ModelRequest, options: dict[str, JsonValue], headers: dict[str, str]
    ) -> ModelResponse:
        options = {**options, "num_ctx": self.num_ctx}
        prefix, rest = request.split()
        user: dict[str, JsonValue] = {"role": "user", "content": rest}
        if request.images:
            user["images"] = [b64(image) for image in request.images]
        messages: list[JsonValue] = [{"role": "system", "content": prefix}] if prefix else []
        messages.append(user)
        payload: dict[str, JsonValue] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            # Without this, /api/chat keeps only the tail of an over-long chat, silently
            # (measured: 228,913 tokens read as 32,771), so the system message is lost.
            "truncate": False,
            "options": options,
        }
        if self.think is not None:
            payload["think"] = self.think
        if request.response_schema is not None:
            payload["format"] = request.response_schema
        data = await self._post(
            "/api/chat",
            payload,
            headers,
            deadline_s=max(self._timeout, OLLAMA_IMAGE_TIMEOUT_S) if request.images else None,
        )
        if data.get("done") is not True:
            raise AdapterError("Provider response is incomplete")
        tokens_in = _tokens(data.get("prompt_eval_count"))
        reserve = request.max_output_tokens or 0
        if tokens_in + reserve >= self.num_ctx:
            raise AdapterError("Prompt exceeds the configured context")
        message = _object(data.get("message"))
        return self._response(
            _text(message.get("content")),
            tokens_in,
            _tokens(data.get("eval_count")),
        )

    async def stream_chat(
        self,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        max_output_tokens: int | None,
    ) -> AsyncIterator[ChatEvent]:
        """``/api/chat`` with ``stream: true`` and ``tools``: NDJSON, one object per line.

        Same fixed ``num_ctx``, ``truncate: false`` and context check as ``complete``.
        Tool calls arrive whole (arguments as an object); Ollama gives no call ids,
        so ids are ``call_<n>``. A fully cached prompt may omit ``prompt_eval_count``
        (counted as 0; local models are priced at $0).
        """
        self.quote_chat(system, messages, tools, max_output_tokens)  # validates the cap
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        options: dict[str, JsonValue] = {"num_ctx": self.num_ctx}
        if max_output_tokens is not None:
            options["num_predict"] = max_output_tokens
        payload: dict[str, JsonValue] = {
            "model": self.model,
            "messages": _ollama_messages(system, messages),
            "stream": True,
            "truncate": False,
            "options": options,
        }
        if tools:
            payload["tools"] = _function_tools(tools)
        if self.think is not None:
            payload["think"] = self.think
        calls = tokens_in = tokens_out = 0
        reason = ""
        finished = False
        lines = self._stream_lines("/api/chat", payload, headers)
        try:
            async for line in lines:
                if not line.strip():
                    continue
                chunk = _event_json(line)
                if chunk.get("error") is not None:
                    raise AdapterError("Provider stream reported an error")
                if "model" in chunk:
                    self._check_reported_model(chunk["model"])
                message = chunk.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str) and content:
                        yield TextDeltaEvent(content)
                    requested = message.get("tool_calls")
                    for item in requested if isinstance(requested, list) else []:
                        if calls >= STREAM_MAX_TOOL_CALLS:
                            raise AdapterError(_TOO_LARGE)
                        entry = _object(item)
                        function = _object(entry.get("function"))
                        arguments = function.get("arguments")
                        if (
                            len(json.dumps(arguments, ensure_ascii=False).encode("utf-8"))
                            > STREAM_TOOL_ARGUMENT_BYTES
                        ):
                            raise AdapterError(_TOO_LARGE)
                        ident = entry.get("id")
                        yield _tool_call(
                            ident if isinstance(ident, str) else "",
                            _text(function.get("name")),
                            arguments,
                            calls,
                        )
                        calls += 1
                if chunk.get("done") is True:
                    tokens_in = _optional_tokens(chunk, "prompt_eval_count")
                    tokens_out = _optional_tokens(chunk, "eval_count")
                    done_reason = chunk.get("done_reason")
                    reason = done_reason if isinstance(done_reason, str) else ""
                    finished = True
                    break
        finally:
            await lines.aclose()
        if not finished:
            raise AdapterError(_INCOMPLETE)
        if tokens_in + (max_output_tokens or 0) >= self.num_ctx:
            raise AdapterError("Prompt exceeds the configured context")
        yield self._usage(tokens_in, tokens_out)
        yield DoneEvent(_stop(reason, calls > 0))
