"""Observation is numbers only: the meter, the journal cursor and the watcher never see content."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from architect.compiler import CompileConfig
from architect.models import AdapterError, FakeAdapter, ModelRequest, ModelResponse, UsageQuote
from architect.profiles import build_services, load_profile
from architect.session.observe import (
    CompileWatcher,
    JournalCursor,
    Meter,
    ObservedAdapter,
    Progress,
    ProgressPolicy,
    observe_services,
)
from architect.session.state import Spend
from architect.spec import Budget, FloorDefinition, FloorKind, Spec
from architect.telemetry import TelemetryStore, categorical


class FakeClock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Sentinel:
    """Anything that reads an attribute of the request has looked at content."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"observer touched request.{name}")


class PassThrough:
    """Inner adapter that returns a fixed usage without touching the request either."""

    model = "fake"

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.seen: list[object] = []

    def quote(self, request: object) -> UsageQuote:
        return UsageQuote(tokens_in=1, tokens_out=1, cost_usd=0.5)

    async def complete(self, request: object) -> ModelResponse:
        self.seen.append(request)
        if self.error is not None:
            raise self.error
        return ModelResponse(text="secret", tokens_in=10, tokens_out=3, cost_usd=0.25, model="fake")


def spec() -> Spec:
    return Spec(
        task="Return ok",
        input_schema={"type": "object"},
        output_schema={"type": "string"},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="tasks"),
        budget=Budget(),
    )


def fake_profile(tmp_path: Path) -> Path:
    (tmp_path / "cases.json").write_text(
        json.dumps([{"case_id": str(i), "task": {}, "expected": "ok"} for i in range(6)]),
        encoding="utf-8",
    )
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "workspace": "w",
                "models": {"fake": {"provider": "fake"}},
                "inference": {"provider": "fake", "model": "fake"},
                "floors": {"tasks": {"kind": "held_out_tasks", "path": "cases.json"}},
                "compile": {"max_candidates": 1, "enable_rewriting": False},
            }
        ),
        encoding="utf-8",
    )
    return profile


async def test_observed_adapter_meters_success_without_reading_content() -> None:
    clock = FakeClock()
    meter = Meter(clock)
    inner = PassThrough()
    adapter = ObservedAdapter(inner, meter)
    assert adapter.model == "fake"
    events = meter.events()
    request = Sentinel()
    response = await adapter.complete(request)  # type: ignore[arg-type]
    assert response.text == "secret" and inner.seen == [request]
    assert meter.calls == 1 and meter.in_flight == 0
    assert meter.snapshot().cost_usd == 0.25 and meter.snapshot().tokens_in == 10
    event = events.get_nowait()
    assert event.kind == "complete" and event.model == "fake" and event.tokens_out == 3
    assert adapter.quote(Sentinel()).cost_usd == 0.5  # type: ignore[arg-type]
    assert meter.calls == 1, "quotes are never metered"


async def test_observed_adapter_meters_errors_and_reraises_unchanged() -> None:
    meter = Meter(FakeClock())
    error = AdapterError("sk-secret-text")
    adapter = ObservedAdapter(PassThrough(error), meter)
    events = meter.events()
    with pytest.raises(AdapterError) as info:
        await adapter.complete(Sentinel())  # type: ignore[arg-type]
    assert info.value is error
    event = events.get_nowait()
    assert event.kind == "error" and event.cost_usd == 0 and event.tokens_in == 0
    assert meter.in_flight == 0 and meter.calls == 1 and meter.failed == 1
    meter.reset()
    assert meter.calls == 0 and meter.failed == 0
    assert meter.snapshot().cost_usd == 0 and events.empty()


async def test_an_observed_streaming_adapter_streams_and_meters() -> None:
    """Around a model that streams native chats, the session's wrapper streams too, so
    Pi, the coder and Forge get native tool calls from whatever adapter a factory hands
    out, and the meter still sees every call."""
    from test_pi_native import COST, Streaming, reply

    from architect.models.chat import ChatMessage, ChatStreaming
    from architect.session.observe import ObservedChat, _MeteredFactory

    meter = Meter(FakeClock())
    plain = ObservedAdapter(PassThrough(), meter)
    assert type(plain) is ObservedAdapter and not isinstance(plain, ChatStreaming)
    inner = Streaming(reply(text="Hi."))
    made = _MeteredFactory(lambda model: inner, meter)("fake")  # type: ignore[arg-type]
    assert isinstance(made, ObservedChat) and isinstance(made, ChatStreaming)
    assert made.quote_chat("s", [ChatMessage("user", "x")], (), 64).cost_usd > 0
    events = [event async for event in made.stream_chat("s", [ChatMessage("user", "x")], (), 64)]
    assert len(events) == 3 and len(inner.sent) == 1
    assert meter.calls == 1 and meter.in_flight == 0
    assert meter.snapshot().cost_usd == pytest.approx(COST)


def test_progress_policy_prints_stages_failures_fifth_calls_and_stale_lines() -> None:
    """The four reasons for a line, and nothing for an ordinary call start or end."""
    policy = ProgressPolicy()

    def snap(elapsed: float, stage: int | None, calls: int, failed: int = 0) -> Progress:
        return Progress(elapsed, stage, "", calls, 0, 0, 0, 0, 0, Spend(), Budget(), 1, 1, failed)

    assert not policy.should_print(snap(0, None, 0))  # no stage yet
    assert policy.should_print(snap(0, 1, 0))  # stage 1
    assert not policy.should_print(snap(1, 1, 1))  # a call: no line
    assert not policy.should_print(snap(2, 1, 4))
    assert policy.should_print(snap(3, 1, 5))  # fifth completed call
    assert not policy.should_print(snap(4, 1, 6))
    assert policy.should_print(snap(5, 2, 6))  # stage 2
    assert not policy.should_print(snap(5, 2, 6))  # the same stage again: no line
    assert policy.should_print(snap(6, 2, 7, failed=1))  # one call failed
    assert not policy.should_print(snap(7, 2, 8, failed=1))
    assert not policy.should_print(snap(10, 2, 10, failed=1))  # 9 completed: same bucket
    assert policy.should_print(snap(11, 2, 11, failed=1))  # 10 completed
    assert not policy.should_print(snap(30, 2, 11, failed=1))  # 19 s since the last line
    assert policy.should_print(snap(31, 2, 11, failed=1))  # 20 s
    assert not policy.should_print(snap(50, 2, 11, failed=1))
    assert policy.should_print(snap(51, 2, 11, failed=1))
    assert policy.should_print(snap(52, 4, 11, failed=1))  # stage 4
    assert ProgressPolicy(every_calls=0).should_print(snap(0, None, 1))  # never divides by 0


def test_observe_services_wraps_every_factory_once(tmp_path: Path) -> None:
    profile = load_profile(fake_profile(tmp_path))
    services = build_services(profile, profile.workspace)
    try:
        meter = Meter()
        observed = observe_services(services, meter)
        assert isinstance(observed.adapter_factory("fake"), ObservedAdapter)
        assert isinstance(observed.compiler.adapter_factory("fake"), ObservedAdapter)
        assert observed.runtime.adapter_factory is not None
        assert isinstance(observed.runtime.adapter_factory("fake"), ObservedAdapter)
        assert isinstance(observed.compiler.inference_adapter, ObservedAdapter)
        assert observed.compiler.rewrite_adapter is None
        assert observed.compiler.adapter_factory._architect_meter is meter
        again = observe_services(observed, meter)
        assert again.adapter_factory is observed.adapter_factory
        assert again.compiler.adapter_factory is observed.adapter_factory
        assert again.compiler.inference_adapter is observed.compiler.inference_adapter
        assert again.compiler is services.compiler and again.registry is services.registry
    finally:
        services.close()


async def test_journal_cursor_filters_by_hash_after_mark(tmp_path: Path) -> None:
    profile = load_profile(fake_profile(tmp_path))
    services = build_services(profile, profile.workspace)
    try:
        first = spec()
        other = spec().model_copy(update={"task": "Return something else"})
        assert first.spec_hash != other.spec_hash
        cursor = JournalCursor(services.runtime.store, first.spec_hash)
        await services.compiler.compile(first)
        cursor.mark()
        assert cursor.since() == []
        await services.compiler.compile(other)
        assert cursor.since() == []
        await services.compiler.compile(first)
        rows = cursor.since()
        assert rows and all(row.spec_hash == first.spec_hash for row in rows)
    finally:
        services.close()


async def test_watcher_reports_stages_in_order_then_polls(tmp_path: Path) -> None:
    clock = FakeClock()
    meter = Meter(clock)
    store = TelemetryStore(tmp_path / "telemetry")
    try:
        stages: asyncio.Queue[int] = asyncio.Queue()
        watcher = CompileWatcher(
            meter=meter,
            cursor=JournalCursor(store, "a" * 64),
            budget=Budget(cost_usd=5, compute_s=1800),
            stages=stages,
            preset_names=("extractor",),
            clock=clock,
            poll_s=0.01,
        )
        seen: list[Progress] = []
        task = asyncio.create_task(watcher.watch(seen.append))
        for stage in (1, 2, 4):
            stages.put_nowait(stage)
            await asyncio.sleep(0.03)
        before = len(seen)
        clock.advance(5.0)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        store.close()
    ordered = [p.stage for p in seen if p.stage is not None]
    assert ordered[:1] == [1] and [
        s for i, s in enumerate(ordered) if i == 0 or ordered[i - 1] != s
    ] == [1, 2, 4]
    assert seen[0].stage_text == "stage 1 selection"
    assert len(seen) > before, "a poll line is emitted when nothing changed"
    last = seen[-1]
    assert last.stage == 4 and last.stage_text == "stage 4 verification"
    assert last.elapsed_s >= 5.0 and last.left_s <= 1795.0 and last.left_usd == 5.0
    assert last.calls == 0 and last.in_flight == 0


async def test_watcher_counts_rows_of_a_real_compile(tmp_path: Path) -> None:
    profile = load_profile(fake_profile(tmp_path))
    services = observe_services(build_services(profile, profile.workspace), Meter())
    try:
        target = spec()
        meter = services.compiler.adapter_factory  # wrapped
        assert meter._architect_meter is not None
        live: Meter = meter._architect_meter
        cursor = JournalCursor(services.runtime.store, target.spec_hash)
        cursor.mark()
        stages: asyncio.Queue[int] = asyncio.Queue()
        watcher = CompileWatcher(
            meter=live,
            cursor=cursor,
            budget=CompileConfig().compile_budget,
            stages=stages,
            preset_names=("synthesizer", "extractor"),
            poll_s=0.01,
        )
        seen: list[Progress] = []
        services.compiler.stage_listener = stages.put_nowait
        try:
            compile_task = asyncio.create_task(services.compiler.compile(target))
            watch_task = asyncio.create_task(watcher.watch(seen.append))
            result = await compile_task
            await asyncio.sleep(0.05)
            watch_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watch_task
        finally:
            services.compiler.stage_listener = None
        assert result.artifact is not None and result.stages == (1, 2, 4)
        last = seen[-1]
        assert last.calls == live.calls > 0
        assert last.case_rows > 0 and last.accepted_rows > 0
        assert last.spend.cost_usd == pytest.approx(sum(row.cost_usd for row in cursor.since()))
        assert categorical(result.artifact.preset) in {
            categorical("synthesizer"),
            categorical("extractor"),
        }
    finally:
        services.close()


async def test_observed_factory_adapter_reports_tokens_from_fake(tmp_path: Path) -> None:
    meter = Meter()
    adapter = ObservedAdapter(FakeAdapter(), meter)
    await adapter.complete(ModelRequest(prompt="hello"))
    assert meter.spend.tokens_in == 8 and meter.spend.tokens_out == 2 and meter.spend.calls == 1
