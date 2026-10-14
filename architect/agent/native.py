"""The conductor's native loop (Phase 13, Section 4): provider tool calls, streamed text.

When the conductor's adapter can stream a chat with native tools (``ChatStreaming``:
the Anthropic, OpenAI-compatible and Ollama HTTP adapters), ``Conductor.turn`` runs
here instead of the JSON envelope loop. Everything else is the conductor's own: the
same tools (listed to the provider as ``ToolSpec``), the same ``Transcript`` (converted
to chat messages on every call), the same dispatch, events, session budget, step limit
and cancellation. Each model call is still one metered ``Runtime.evaluate`` run; inside
it ``RunContext.stream_call`` reserves the quote before the stream opens and reconciles
it with the provider's reported usage.

Events per model call: ``Thinking(True)``, one ``TextDelta`` per streamed piece of
text, ``Thinking(False)``; then either the tools run (``ToolStarted``/``ToolFinished``;
any streamed text was a preamble) or the turn ends with ``AssistantText`` holding the
whole answer. A build the user cancelled ends the turn as soon as it returns, with
``Conductor.set_aside``'s fixed answer and no further model call, as in the envelope loop.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from typing import Protocol, cast

from pydantic import JsonValue

from architect.models import ModelAdapter
from architect.models.chat import (
    ChatEvent,
    ChatMessage,
    ChatResult,
    ChatStreaming,
    TextDeltaEvent,
    ToolCallEvent,
    ToolSpec,
)
from architect.runtime import BudgetExceeded, RunContext
from architect.session.observe import ObservedChat

from .conductor import BUILD_MODE, SYSTEM, Conductor, TurnResult
from .events import AssistantText, Notice, TextDelta, Thinking, ToolStarted
from .tools import Tool, control_spec, tool_summary
from .transcript import Transcript

__all__ = [
    "NATIVE_SYSTEM",
    "NativeAdapter",
    "ObservedChat",
    "chat_messages",
    "native_system",
    "native_turn",
    "streaming_adapter",
    "tool_specs",
]

# The envelope prompt's guidance without its reply protocol: tools are native here.
NATIVE_SYSTEM = (
    SYSTEM.split("Reply protocol:")[0].rstrip()
    + "\n- Use tools by calling them. When you are done, reply with the answer for the "
    "user in markdown."
)
EMPTY_ANSWER = "(The model returned an empty answer.)"
_CALL_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


class NativeAdapter(ModelAdapter, ChatStreaming, Protocol):
    """A model adapter that also streams native chats."""


def streaming_adapter(adapter: ModelAdapter) -> NativeAdapter | None:
    """The adapter as a native chat adapter, or ``None`` (use the envelope loop).

    An adapter with ``stream_chat`` is used as is. A session ``ObservedAdapter`` around
    one is an ``ObservedChat``, which streams and still reaches the session meter. Any
    other wrapper is opaque: unwrapping it could bypass its accounting.
    """
    if isinstance(adapter, ChatStreaming):
        return cast(NativeAdapter, adapter)
    return None


def tool_specs(tools: Sequence[Tool]) -> tuple[ToolSpec, ...]:
    return tuple(ToolSpec(tool.name, tool.description, tool.parameters) for tool in tools)


def native_system(conductor: Conductor) -> str:
    parts = [NATIVE_SYSTEM, f"Working directory: {conductor.cwd}"]
    if conductor.instructions:
        parts.append("Project instructions:\n" + conductor.instructions)
    if conductor.mode != "normal":
        parts.append(BUILD_MODE)
    return "\n\n".join(parts)


def chat_messages(transcript: Transcript) -> list[ChatMessage]:
    """The transcript as provider-valid chat messages.

    Providers require every tool call to be answered right after the message that
    made it, with ids that are unique and simple. The transcript does not guarantee
    that (an interrupted turn leaves calls unanswered, ``fit`` can drop a call's
    message, envelope models reuse ids like ``call-1``), so this conversion:
    re-ids calls that are invalid or already used (results follow their call),
    answers each unanswered call with ``{"error": "not_run"}``, turns a result
    whose call is gone into a user-side note, and sends host notes as user text.
    The conversion is deterministic, so provider prompt caches keep hitting.
    """
    out: list[ChatMessage] = []
    used: set[str] = set()
    # (id in the transcript, id sent to the provider, tool name), awaiting results.
    group: list[tuple[str, str, str]] = []
    answered: set[int] = set()

    def fresh(original: str) -> str:
        if _CALL_ID.fullmatch(original) and original not in used:
            used.add(original)
            return original
        number = len(used)
        while f"call_{number}" in used:
            number += 1
        used.add(f"call_{number}")
        return f"call_{number}"

    def close() -> None:
        for position, (_, sent, name) in enumerate(group):
            if position not in answered:
                out.append(
                    ChatMessage(
                        "tool",
                        json.dumps({"error": "not_run"}),
                        tool_call_id=sent,
                        name=name,
                        is_error=True,
                    )
                )
        group.clear()
        answered.clear()

    for message in transcript.messages:
        if message.role == "tool":
            match = next(
                (
                    position
                    for position, (original, _, _) in enumerate(group)
                    if original == message.call_id and position not in answered
                ),
                None,
            )
            if match is not None:
                answered.add(match)
                _, sent, name = group[match]
                out.append(
                    ChatMessage(
                        "tool",
                        message.text,
                        tool_call_id=sent,
                        name=message.name or name,
                        is_error=not message.ok,
                    )
                )
                continue
            close()
            label = message.name or "a tool"
            out.append(ChatMessage("user", f"[Earlier result of {label}]\n{message.text}"))
            continue
        close()
        if message.role == "user":
            if message.text.strip():
                out.append(ChatMessage("user", message.text))
        elif message.role == "note":
            out.append(ChatMessage("user", f"[Note from Architect, not the user] {message.text}"))
        else:
            calls: list[ToolCallEvent] = []
            for call in message.calls:
                raw_name, arguments = call.get("name"), call.get("arguments")
                name = raw_name if isinstance(raw_name, str) and raw_name else "unknown_tool"
                sent = fresh(str(call.get("id", "")))
                calls.append(
                    ToolCallEvent(sent, name, arguments if isinstance(arguments, dict) else {})
                )
                group.append((str(call.get("id", "")), sent, name))
            if message.text.strip() or calls:
                out.append(ChatMessage("assistant", message.text, tool_calls=tuple(calls)))
    close()
    return out


async def _step(
    conductor: Conductor, adapter: NativeAdapter, tools: tuple[ToolSpec, ...]
) -> ChatResult:
    """One metered, streamed conductor call against the session budget."""
    settings = conductor.settings
    budget = min(settings.call_usd, conductor.remaining_usd())
    if budget <= 0:
        raise BudgetExceeded("session_budget_exhausted")
    spec = control_spec(
        "Architect conductor turn", adapter.model, output_schema={"type": "string"},
        cost_usd=budget, compute_s=settings.call_s,
    )
    conductor.transcript.fit(settings.context_chars)
    system, messages = native_system(conductor), chat_messages(conductor.transcript)
    ui = conductor.ui
    replies: list[ChatResult] = []

    def on_event(event: ChatEvent) -> None:
        if isinstance(event, TextDeltaEvent):
            ui.emit(TextDelta(event.text))

    async def one(context: RunContext, task: JsonValue) -> JsonValue:
        reply = await context.stream_call(
            system, messages, tools, settings.max_output_tokens, on_event=on_event
        )
        replies.append(reply)
        return reply.text

    ui.emit(Thinking(True))
    try:
        result = await conductor.services.runtime.evaluate(  # type: ignore[attr-defined]
            spec, one, None, adapter, preset="conductor", components=("conductor",)
        )
    finally:
        ui.emit(Thinking(False))
    conductor._charge(result.cost_usd)
    return replies[0]


async def native_turn(conductor: Conductor, adapter: NativeAdapter, text: str) -> TurnResult:
    """``Conductor.turn`` over native tool calls; same results, events and failures."""
    conductor.transcript.user(text)
    spent_before = conductor.spent_usd
    settings = conductor.settings
    tools = tool_specs(conductor.tools)
    calls = 0
    try:
        if conductor.routes_to_make(text):
            calls += 1
            await conductor.route_make(text)
            if conductor.build_set_aside:
                return conductor.set_aside(calls, spent_before)
        for _ in range(settings.max_steps):
            conductor.take_steering()
            reply = await _step(conductor, adapter, tools)
            if not reply.tool_calls:
                answer = reply.text if reply.text.strip() else EMPTY_ANSWER
                conductor.transcript.assistant(answer)
                if reply.stop_reason == "max_tokens":
                    conductor.ui.emit(Notice("The answer was cut at the output limit.", "warn"))
                conductor.ui.emit(AssistantText(answer))
                return TurnResult(answer, calls, conductor.spent_usd - spent_before)
            if conductor.build_ended is not None:
                # The build of this turn ended without an agent and the model asks for
                # tools in place of an answer: none runs (``Conductor.ended``).
                return conductor.ended(calls, spent_before)
            requested: list[dict[str, JsonValue]] = [
                {"id": call.id, "name": call.name, "arguments": call.arguments}
                for call in reply.tool_calls
            ]
            conductor.transcript.assistant(reply.text, calls=requested)
            for position, call in enumerate(reply.tool_calls):
                calls += 1
                refusal: dict[str, JsonValue] | None = None
                if position >= settings.max_calls_per_step:
                    refusal = {"error": "too_many_calls", "limit": settings.max_calls_per_step}
                elif call.error is not None:
                    refusal = {"error": call.error}
                if refusal is None:
                    await conductor._dispatch(requested[position])
                    if conductor.build_set_aside:
                        # Later calls of this reply stay unanswered; chat_messages answers
                        # them with "not_run" when the next turn converts the transcript.
                        return conductor.set_aside(calls, spent_before)
                    if conductor.build_ended is not None:
                        break  # later calls of this reply are answered "not_run"
                else:
                    conductor.ui.emit(
                        ToolStarted(call.id, call.name, tool_summary(call.name, call.arguments))
                    )
                    conductor._finish(call.id, call.name, refusal, False)
        answer = "I stopped after the step limit for one turn; say 'continue' to go on."
        conductor.transcript.assistant(answer)
        conductor.ui.emit(Notice(answer, "warn"))
        return TurnResult(answer, calls, conductor.spent_usd - spent_before)
    except asyncio.CancelledError:
        conductor.transcript.note(
            "The user interrupted this turn; finished tool results are kept."
        )
        raise
    except BudgetExceeded:
        answer = (
            f"The session budget (${settings.session_usd:.2f}) is used up; raise it with "
            "/budget session <usd>."
        )
        conductor.transcript.note(answer)
        conductor.ui.emit(Notice(answer, "error"))
        return TurnResult(answer, calls, conductor.spent_usd - spent_before)
