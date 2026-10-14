"""Native chat with tools and streamed replies (Phase 13, Section 4).

An adapter that can hold a real multi-turn conversation with native tool calling
implements ``ChatStreaming``: ``quote_chat`` reserves a conservative upper bound
before any I/O (like ``ModelAdapter.quote``) and ``stream_chat`` yields
``ChatEvent`` values as the provider streams them. CLI adapters do not implement
it; callers fall back to the text envelope protocol.

Event order from one ``stream_chat`` call:

- ``TextDeltaEvent`` for each piece of answer text, in order;
- ``ToolCallEvent`` for each complete tool call (arguments already parsed);
- ``UsageEvent`` one or more times; each one carries the call's cumulative usage
  so far, and the last one, right before ``DoneEvent``, is the final usage. An
  earlier one is partial: Anthropic reports the prompt up front, before the reply;
- exactly one ``DoneEvent`` last, with a normalized ``stop_reason``.

A stream that ends without its terminal event raises
``AdapterError("Provider response is incomplete")``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from pydantic import JsonValue

from .types import UsageQuote

__all__ = [
    "CHAT_MESSAGE_OVERHEAD_TOKENS",
    "CHAT_TOOLS_OVERHEAD_TOKENS",
    "ChatEvent",
    "ChatMessage",
    "ChatResult",
    "ChatRole",
    "ChatStreaming",
    "DoneEvent",
    "StopReason",
    "TextDeltaEvent",
    "ToolCallEvent",
    "ToolSpec",
    "UsageEvent",
    "chat_input_bytes",
]

ChatRole = Literal["system", "user", "assistant", "tool"]
# Normalized across providers: end_turn (a final answer), tool_use (the reply calls
# tools), max_tokens (cut at the output cap), refusal (a content filter), other.
StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal", "other"]

# Framing a provider adds per message and, when tools are offered, its own tool-use
# instructions. Quotes add these on top of UTF-8 bytes, which already bound text tokens.
CHAT_MESSAGE_OVERHEAD_TOKENS = 16
CHAT_TOOLS_OVERHEAD_TOKENS = 1024


@dataclass(frozen=True)
class ToolCallEvent:
    """One complete tool call. ``error`` is ``"invalid_arguments"`` when the provider's
    arguments were not a JSON object; ``arguments`` is then empty and the call must
    not run (its result reports the error so the model can retry)."""

    id: str
    name: str
    arguments: dict[str, JsonValue]
    error: str | None = None


@dataclass(frozen=True)
class ChatMessage:
    """One conversation message.

    ``user`` and ``system`` carry ``text``; ``assistant`` carries ``text`` and/or
    ``tool_calls``; ``tool`` answers the call ``tool_call_id`` (tool ``name``) with
    ``text`` and ``is_error`` when the tool failed.
    """

    role: ChatRole
    text: str = ""
    tool_calls: tuple[ToolCallEvent, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None
    is_error: bool = False


@dataclass(frozen=True)
class ToolSpec:
    """A tool offered to the model: ``parameters`` is a JSON Schema object."""

    name: str
    description: str
    parameters: dict[str, JsonValue]


@dataclass(frozen=True)
class TextDeltaEvent:
    text: str


@dataclass(frozen=True)
class UsageEvent:
    """Cumulative usage of the call so far. Cache counts are subsets of ``tokens_in``.

    ``estimated`` marks usage the provider did not report (an OpenAI-compatible
    server without ``stream_options``): input from the quote, output from the
    streamed bytes, capped at the quoted output. ``reasoning_tokens`` is the part of
    ``tokens_out`` the provider reports as reasoning (0 when it reports none).
    """

    tokens_in: int
    tokens_out: int
    cost_usd: float
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    estimated: bool = False
    reasoning_tokens: int = 0


@dataclass(frozen=True)
class DoneEvent:
    stop_reason: StopReason


ChatEvent = TextDeltaEvent | ToolCallEvent | UsageEvent | DoneEvent


@dataclass(frozen=True)
class ChatResult:
    """A settled streamed call: the whole text, the tool calls, final usage, stop reason."""

    text: str
    tool_calls: tuple[ToolCallEvent, ...]
    usage: UsageEvent
    stop_reason: StopReason


@runtime_checkable
class ChatStreaming(Protocol):
    """Optional adapter capability: native tools and streamed text."""

    model: str

    def quote_chat(
        self,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        max_output_tokens: int | None,
    ) -> UsageQuote:
        """A conservative reservation for this call; raises before any I/O."""
        ...

    def stream_chat(
        self,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        max_output_tokens: int | None,
    ) -> AsyncIterator[ChatEvent]:
        """Stream one assistant reply. Network access only happens while iterating."""
        ...


def _compact(value: JsonValue) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def chat_input_bytes(
    system: str, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec]
) -> int:
    """UTF-8 bytes of everything the model reads: system, messages, calls and tool specs."""
    total = len(system.encode("utf-8"))
    for message in messages:
        total += len(message.text.encode("utf-8"))
        total += len((message.tool_call_id or "").encode("utf-8"))
        total += len((message.name or "").encode("utf-8"))
        for call in message.tool_calls:
            total += len(call.id.encode("utf-8")) + len(call.name.encode("utf-8"))
            total += _compact(call.arguments)
    for tool in tools:
        total += len(tool.name.encode("utf-8")) + len(tool.description.encode("utf-8"))
        total += _compact(tool.parameters)
    return total
