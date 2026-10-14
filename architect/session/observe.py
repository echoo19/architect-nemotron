"""Content-free observation of a running compile (contract Section 3.9).

The session shows live spend while Stages 1-4 run, but the runtime's wallet is
ContextVar-scoped and the compiler exposes no progress API. Two observers fill
that gap without touching content: ``ObservedAdapter`` wraps every adapter the
services hand out and records numbers only (tokens, cost, wall time, model id),
and ``JournalCursor`` counts new telemetry rows for the Spec being compiled by
comparing hashed categories. ``CompileWatcher`` folds both, plus the compiler's
``stage_listener`` events, into ``Progress`` snapshots; ``ProgressPolicy`` decides
which of those snapshots the COMPILING screen prints as a line.

Nothing here reads a prompt, a response or a task body, and nothing here can
fail a compile: an observer error is swallowed, a metering error is re-raised
unchanged.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import replace
from typing import cast

from architect.models import ModelAdapter, ModelRequest, ModelResponse, UsageQuote
from architect.models.chat import ChatEvent, ChatMessage, ChatStreaming, ToolSpec, UsageEvent
from architect.profiles import Services
from architect.spec import Budget
from architect.telemetry import TelemetryRow, TelemetryStore, categorical

from .state import MeterEvent, Progress, Spend

__all__ = [
    "STAGE_TEXT",
    "CompileWatcher",
    "JournalCursor",
    "Meter",
    "MeterEvent",
    "ObservedAdapter",
    "ObservedChat",
    "Progress",
    "ProgressPolicy",
    "observe_services",
]

STAGE_TEXT = {
    1: "stage 1 selection",
    2: "stage 2 tuning",
    3: "stage 3 rewriting",
    4: "stage 4 verification",
}


class Meter:
    """Running totals of observed model calls plus a queue of per-call events."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.calls = 0
        self.failed = 0
        self.in_flight = 0
        self.spend = Spend()
        self._events: asyncio.Queue[MeterEvent] | None = None

    def events(self) -> asyncio.Queue[MeterEvent]:
        """Create the queue lazily so a Meter can be built before a loop runs."""
        if self._events is None:
            self._events = asyncio.Queue()
        return self._events

    def snapshot(self) -> Spend:
        return replace(self.spend)

    def reset(self) -> None:
        """Start a new measurement window; pending events from the old one are dropped."""
        self.calls = 0
        self.failed = 0
        self.in_flight = 0
        self.spend = Spend()
        if self._events is not None:
            while not self._events.empty():
                self._events.get_nowait()

    def record(self, event: MeterEvent) -> None:
        self.calls += 1
        if event.kind == "error":
            self.failed += 1
        self.spend.add(
            Spend(
                calls=1,
                tokens_in=event.tokens_in,
                tokens_out=event.tokens_out,
                cost_usd=event.cost_usd,
                wall_s=event.wall_s,
            )
        )
        if self._events is not None:
            self._events.put_nowait(event)


class ObservedAdapter:
    """Delegate to ``inner`` and record numbers only; never the text either way.

    Built around an adapter that streams native chats (``ChatStreaming``), it is an
    ``ObservedChat``, which streams too: whatever is handed a session's adapters (the
    conductor, the coder, Pi in Stage 2, Stage 4 and the run tool, Forge's agent) sees
    one that streams whenever the model does, and every streamed call still reaches
    the meter.
    """

    model: str

    def __new__(
        cls, inner: ModelAdapter | None = None, meter: Meter | None = None
    ) -> ObservedAdapter:
        # Defaults only so copy and pickle, which call __new__ with the class alone, work.
        kind: type[ObservedAdapter] = cls
        if cls is ObservedAdapter and isinstance(inner, ChatStreaming):
            kind = ObservedChat
        return super().__new__(kind)

    def __init__(self, inner: ModelAdapter, meter: Meter) -> None:
        self.model = inner.model
        self._inner = inner
        self._meter = meter

    def quote(self, request: ModelRequest) -> UsageQuote:
        return self._inner.quote(request)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        meter = self._meter
        started = meter.clock()
        meter.in_flight += 1
        try:
            response = await self._inner.complete(request)
        except BaseException:
            meter.in_flight -= 1
            meter.record(
                MeterEvent(
                    kind="error",
                    model=self.model,
                    tokens_in=0,
                    tokens_out=0,
                    cost_usd=0.0,
                    wall_s=max(0.0, meter.clock() - started),
                )
            )
            raise
        meter.in_flight -= 1
        meter.record(
            MeterEvent(
                kind="complete",
                model=self.model,
                tokens_in=response.tokens_in,
                tokens_out=response.tokens_out,
                cost_usd=response.cost_usd,
                wall_s=max(0.0, meter.clock() - started),
            )
        )
        return response


class ObservedChat(ObservedAdapter):
    """An ``ObservedAdapter`` whose inner adapter streams: it streams too, and records
    numbers only. ``ObservedAdapter(inner, meter)`` makes one when ``inner`` streams.

    A streamed call records the last usage the stream reported, so a cancelled stream
    is charged what the provider reported before it stopped. The meter is a display:
    budgets and wallets keep a failed or cancelled stream's quote (``RunContext``).

    The caller's output limit (``ChatStreaming``'s fourth argument, always passed by
    position) is handed on unchanged: the session sets no output limit of its own (the
    token policy; only bootstrap's 16-token probe does).
    """

    def quote_chat(
        self,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        limit: int | None,
    ) -> UsageQuote:
        inner = cast(ChatStreaming, self._inner)
        return inner.quote_chat(system, messages, tools, limit)

    async def stream_chat(
        self,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        limit: int | None,
    ) -> AsyncIterator[ChatEvent]:
        meter = self._meter
        started = meter.clock()
        meter.in_flight += 1
        usage: UsageEvent | None = None
        finished = False
        stream = cast(ChatStreaming, self._inner).stream_chat(system, messages, tools, limit)
        try:
            async for event in stream:
                if isinstance(event, UsageEvent):
                    usage = event
                yield event
            finished = True
        finally:
            try:
                close = getattr(stream, "aclose", None)
                if close is not None:
                    await close()
            finally:
                meter.in_flight -= 1
                meter.record(
                    MeterEvent(
                        kind="complete" if finished else "error",
                        model=self.model,
                        tokens_in=usage.tokens_in if usage is not None else 0,
                        tokens_out=usage.tokens_out if usage is not None else 0,
                        cost_usd=usage.cost_usd if usage is not None else 0.0,
                        wall_s=max(0.0, meter.clock() - started),
                    )
                )


class _MeteredFactory:
    """An adapter factory whose adapters report to one meter.

    The ``_architect_meter`` attribute is the idempotence marker
    ``observe_services`` checks so a rebuilt Services is never double-wrapped.
    """

    def __init__(self, inner: Callable[[str], ModelAdapter], meter: Meter) -> None:
        self._inner = inner
        self._architect_meter = meter

    def __call__(self, model: str) -> ModelAdapter:
        return ObservedAdapter(self._inner(model), self._architect_meter)


def _wrap_factory(
    factory: Callable[[str], ModelAdapter],
    meter: Meter,
    cache: dict[int, Callable[[str], ModelAdapter]],
) -> Callable[[str], ModelAdapter]:
    if getattr(factory, "_architect_meter", None) is not None:
        return factory
    wrapped = cache.get(id(factory))
    if wrapped is None:
        wrapped = _MeteredFactory(factory, meter)
        cache[id(factory)] = wrapped
    return wrapped


def _wrap_adapter(adapter: ModelAdapter | None, meter: Meter) -> ModelAdapter | None:
    if adapter is None or isinstance(adapter, ObservedAdapter):
        return adapter
    return ObservedAdapter(adapter, meter)


def observe_services(services: Services, meter: Meter) -> Services:
    """Route every adapter the services hand out through ``meter``.

    The compiler and runtime factories are usually the same object as
    ``services.adapter_factory``; the cache keeps them one wrapper so the
    identity relationship survives. Known limit: the floor_factory closure inside
    ``build_services`` captured the original factory, so LLM-judge calls show up
    in telemetry rows but not in the meter.
    """
    cache: dict[int, Callable[[str], ModelAdapter]] = {}
    wrapped = _wrap_factory(services.adapter_factory, meter, cache)
    compiler = services.compiler
    compiler.adapter_factory = _wrap_factory(compiler.adapter_factory, meter, cache)
    runtime = services.runtime
    if runtime.adapter_factory is not None:
        runtime.adapter_factory = _wrap_factory(runtime.adapter_factory, meter, cache)
    compiler.inference_adapter = _wrap_adapter(compiler.inference_adapter, meter)
    compiler.rewrite_adapter = _wrap_adapter(compiler.rewrite_adapter, meter)
    return dataclasses.replace(services, adapter_factory=wrapped)


class JournalCursor:
    """Count telemetry rows for one Spec written after ``mark()``.

    The journal is a local file and rows are content-free, so a full re-read is
    both cheap and safe; only ``spec_hash`` (already a digest) is compared.
    """

    def __init__(self, store: TelemetryStore, spec_hash: str) -> None:
        self._store = store
        self._spec_hash = spec_hash.lower()
        self._mark = 0

    def mark(self) -> None:
        self._mark = len(self._store.rows())

    def since(self) -> list[TelemetryRow]:
        rows = self._store.rows()
        return [row for row in rows[self._mark :] if row.spec_hash == self._spec_hash]


class CompileWatcher:
    """Turn stage events, meter events and journal rows into ``Progress`` snapshots."""

    def __init__(
        self,
        *,
        meter: Meter,
        cursor: JournalCursor,
        budget: Budget,
        stages: asyncio.Queue[int],
        preset_names: Sequence[str],
        clock: Callable[[], float] = time.monotonic,
        poll_s: float = 2.0,
    ) -> None:
        self._meter = meter
        self._cursor = cursor
        self._budget = budget
        self._stages = stages
        self._case_types = frozenset(categorical(name) for name in preset_names)
        self._quality_type = categorical("quality_evaluation")
        self._rewrite_types = frozenset({categorical("rewrite"), categorical("rewrite_proposal")})
        self._clock = clock
        self._poll_s = poll_s

    def _progress(self, started: float, stage: int | None) -> Progress:
        rows = self._cursor.since()
        case_rows = quality_rows = rewrite_rows = accepted_rows = 0
        for row in rows:
            if row.task_type in self._case_types:
                case_rows += 1
            elif row.task_type == self._quality_type:
                quality_rows += 1
            elif row.task_type in self._rewrite_types:
                rewrite_rows += 1
            if row.accepted:
                accepted_rows += 1
        spend = self._meter.snapshot()
        elapsed = max(0.0, self._clock() - started)
        return Progress(
            elapsed_s=elapsed,
            stage=stage,
            stage_text=STAGE_TEXT.get(stage, "") if stage is not None else "",
            calls=self._meter.calls,
            in_flight=self._meter.in_flight,
            case_rows=case_rows,
            quality_rows=quality_rows,
            rewrite_rows=rewrite_rows,
            accepted_rows=accepted_rows,
            spend=spend,
            budget=self._budget,
            left_usd=max(0.0, self._budget.cost_usd - spend.cost_usd),
            left_s=max(0.0, self._budget.compute_s - elapsed),
            failed=self._meter.failed,
        )

    async def watch(self, on_update: Callable[[Progress], None]) -> None:
        """Run until cancelled; report whenever something changed or ``poll_s`` passed.

        Two getter tasks stay armed across iterations so no queue item is lost
        between wake-ups; cancelling a pending ``Queue.get`` never drops an item.
        """
        started = self._clock()
        events = self._meter.events()
        stage: int | None = None
        stage_task: asyncio.Task[int] = asyncio.create_task(self._stages.get())
        event_task: asyncio.Task[MeterEvent] = asyncio.create_task(events.get())
        last: Progress | None = None
        last_report = started
        try:
            while True:
                done, _ = await asyncio.wait(
                    {stage_task, event_task},
                    timeout=self._poll_s,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                changed = False
                if stage_task in done:
                    stage = stage_task.result()
                    stage_task = asyncio.create_task(self._stages.get())
                    changed = True
                if event_task in done:
                    event_task.result()
                    event_task = asyncio.create_task(events.get())
                    changed = True
                progress = self._progress(started, stage)
                now = self._clock()
                if (
                    changed
                    or last is None
                    or _counts(progress) != _counts(last)
                    or now - last_report >= self._poll_s
                ):
                    last = progress
                    last_report = now
                    on_update(progress)
        finally:
            for task in (stage_task, event_task):
                task.cancel()
            await asyncio.gather(stage_task, event_task, return_exceptions=True)


def _counts(progress: Progress) -> tuple[int, int, int, int, int, int, int]:
    return (
        progress.stage or 0,
        progress.calls,
        progress.in_flight,
        progress.case_rows,
        progress.quality_rows,
        progress.rewrite_rows,
        progress.accepted_rows,
    )


class ProgressPolicy:
    """Which ``Progress`` snapshots become a printed line (contract Section 8.2).

    The watcher reports every change, which is one snapshot when a call starts and one
    when it ends: two lines per model call, seventy-odd for one compile. A line is
    printed only when the stage changed, when a call failed, when another
    ``every_calls`` calls completed and when ``every_s`` of compile time passed since
    the last printed line. Every other snapshot is kept for ``status`` only. The
    policy holds no content: stage numbers, counts and elapsed seconds.
    """

    def __init__(self, *, every_calls: int = 5, every_s: float = 20.0) -> None:
        self._every_calls = max(1, every_calls)
        self._every_s = every_s
        self._stage: int | None = None
        self._completed = 0
        self._failed = 0
        self._printed_at = 0.0

    def should_print(self, progress: Progress) -> bool:
        """True when ``progress`` deserves a line; the decision is recorded when it is."""
        completed = max(0, progress.calls - progress.failed)
        stage_changed = progress.stage is not None and progress.stage != self._stage
        failed = progress.failed > self._failed
        fifth = completed // self._every_calls > self._completed // self._every_calls
        stale = progress.elapsed_s - self._printed_at >= self._every_s
        if not (stage_changed or failed or fifth or stale):
            return False
        self._stage = progress.stage if progress.stage is not None else self._stage
        self._completed = completed
        self._failed = progress.failed
        self._printed_at = progress.elapsed_s
        return True
