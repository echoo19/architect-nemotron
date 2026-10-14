"""Fake-only tests for budget gates, telemetry, and runtime lifecycle."""

import asyncio
from pathlib import Path

import pytest
from pydantic import JsonValue

from architect._authority import _sign_report
from architect.models import (
    AdapterError,
    FakeAdapter,
    ModelRequest,
    ModelResponse,
    UsageQuote,
)
from architect.runtime import (
    ArtifactExpiredError,
    BudgetExceeded,
    RunContext,
    Runtime,
    UnlicensedArtifactError,
    load_harness,
)
from architect.spec import (
    Budget,
    FloorDefinition,
    FloorKind,
    HarnessArtifact,
    Lifecycle,
    Spec,
    TestReport,
)
from architect.telemetry import TelemetryStore


def spec(*, budget: Budget | None = None, lifecycle: Lifecycle = Lifecycle.EXPIRE) -> Spec:
    return Spec(
        task="private-task-value",
        input_schema={},
        output_schema={},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.8, dataset="private-data"),
        budget=budget or Budget(),
        lifecycle=lifecycle,
    )


def response(*, cost: float = 0.01) -> ModelResponse:
    return ModelResponse(
        text="private-answer", tokens_in=3, tokens_out=2, cost_usd=cost, model="fake"
    )


async def one_call(context: RunContext, task: JsonValue) -> JsonValue:
    return await context.call(str(task))


DEFAULT_SOURCE = "async def run(context, task):\n    return await context.call(str(task))\n"


def artifact(
    tmp_path: Path, *, lifecycle: Lifecycle = Lifecycle.EXPIRE, source: str = DEFAULT_SOURCE
) -> HarnessArtifact:
    directory = tmp_path / "artifact"
    directory.mkdir()
    (directory / "harness.py").write_text(source, encoding="utf-8")
    value = HarnessArtifact(
        spec=spec(lifecycle=lifecycle),
        preset="toy",
        model="fake",
        components=("model-call",),
        directory=directory,
    )
    value.save()
    return value


def licensed_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *,
    lifecycle: Lifecycle = Lifecycle.EXPIRE, source: str = DEFAULT_SOURCE,
) -> HarnessArtifact:
    monkeypatch.setenv("ARCHITECT_SIGNING_KEY_FILE", str(tmp_path / "signing.key"))
    value = artifact(tmp_path, lifecycle=lifecycle, source=source)
    report = TestReport(
        spec_hash=value.spec.spec_hash,
        artifact_digest=value.artifact_digest,
        passed=True,
        floor_score=0.9,
        floor_limit=value.spec.floor.limit,
        floor_kind=value.spec.floor.kind,
        tokens=5,
        cost_usd=0.01,
        compute_s=0.01,
        component_count=value.component_count,
        verification_id="fixture-verifier",
    )
    signed = report.model_copy(
        update={"signature": _sign_report(report.model_dump(mode="json", exclude={"signature"}))}
    )
    value = value.model_copy(update={"report": signed})
    value.save()
    assert value.licensed
    return value


async def test_budget_reserves_before_second_call_and_logs(tmp_path: Path) -> None:
    store = TelemetryStore(tmp_path / "telemetry")
    adapter = FakeAdapter(responses=[response(), response()])

    async def twice(context: RunContext, task: JsonValue) -> JsonValue:
        await context.call("private-first")
        return await context.call("private-second")

    with pytest.raises(BudgetExceeded):
        await Runtime(store).evaluate(spec(budget=Budget(cost_usd=0.015)), twice, None, adapter)
    assert adapter.calls == 1
    rows = store.rows()
    assert len(rows) == 1
    assert rows[0].tokens_in == 3
    assert rows[0].tokens_out == 2
    assert not rows[0].accepted
    contents = (tmp_path / "telemetry" / "runs.jsonl").read_text(encoding="utf-8")
    assert "private-" not in contents


async def test_cost_budget_preflight() -> None:
    adapter = FakeAdapter(responses=[response(cost=0.02)])
    context = RunContext(adapter, Budget(cost_usd=0.01))
    with pytest.raises(BudgetExceeded):
        await context.call("private-input")
    assert adapter.calls == 0
    assert context.cost_usd == 0


async def test_successful_internal_evaluation(tmp_path: Path) -> None:
    store = TelemetryStore(tmp_path / "telemetry")
    result = await Runtime(store).evaluate(
        spec(), one_call, "private-input", FakeAdapter(responses=[response()])
    )
    assert result.output == "private-answer"
    assert result.tokens_in + result.tokens_out == 5
    assert result.cost_usd == pytest.approx(0.01)
    assert store.rows()[0].accepted
    assert store.rows()[0].floor_score is None


async def test_unlicensed_rejection_is_logged(tmp_path: Path) -> None:
    store = TelemetryStore(tmp_path / "telemetry")
    adapter = FakeAdapter()
    value = artifact(tmp_path)
    with pytest.raises(UnlicensedArtifactError):
        await Runtime(store).run(value, "private-input", adapter)
    assert adapter.calls == 0
    assert not (value.directory / ".consumed").exists()
    assert len(store.rows()) == 1
    assert not store.rows()[0].accepted


async def test_total_wall_timeout_without_model_call(tmp_path: Path) -> None:
    store = TelemetryStore(tmp_path / "telemetry")

    async def slow(context: RunContext, task: JsonValue) -> JsonValue:
        await asyncio.Event().wait()
        return None

    with pytest.raises(BudgetExceeded):
        await Runtime(store).evaluate(
            spec(budget=Budget(compute_s=0.01)), slow, None, FakeAdapter()
        )
    assert len(store.rows()) == 1
    assert not store.rows()[0].accepted


async def test_caught_budget_error_cannot_permit_delivery(tmp_path: Path) -> None:
    store = TelemetryStore(tmp_path / "telemetry")

    async def swallow(context: RunContext, task: JsonValue) -> JsonValue:
        try:
            await context.call("private-input")
        except BudgetExceeded:
            return "must-not-deliver"
        return None

    with pytest.raises(BudgetExceeded):
        await Runtime(store).evaluate(
            spec(budget=Budget(cost_usd=0.005)), swallow, None, FakeAdapter(responses=[response()])
        )
    assert not store.rows()[0].accepted


class UnderquotingAdapter:
    model = "fake"

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=1, tokens_out=1, cost_usd=0.001)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return response()


async def test_underquoted_actual_cost_is_recorded_and_rejected(tmp_path: Path) -> None:
    store = TelemetryStore(tmp_path / "telemetry")
    with pytest.raises(BudgetExceeded):
        await Runtime(store).evaluate(spec(), one_call, None, UnderquotingAdapter())
    row = store.rows()[0]
    assert row.tokens_in + row.tokens_out == 5
    assert row.cost_usd == pytest.approx(0.01)
    assert not row.accepted


@pytest.mark.parametrize("underestimate_tokens", [False, True])
async def test_high_actual_tokens_are_observed_not_limited(
    tmp_path: Path, underestimate_tokens: bool,
) -> None:
    class HighUsageAdapter(FakeAdapter):
        def quote(self, request: ModelRequest) -> UsageQuote:
            assert request.max_output_tokens is None
            quote = super().quote(request)
            if underestimate_tokens:
                return quote.model_copy(update={"tokens_in": 1, "tokens_out": 1})
            return quote

    budget = Budget.model_validate({"tokens": 1, "cost_usd": 0.01, "compute_s": 5})
    assert not hasattr(budget, "tokens")
    assert "tokens" not in budget.model_dump()
    assert "tokens" not in Budget.model_json_schema()["properties"]
    adapter = HighUsageAdapter(responses=[
        response().model_copy(update={"tokens_in": 2_000_000, "tokens_out": 1_000_000}),
    ])
    store = TelemetryStore(tmp_path / "telemetry")
    result = await Runtime(store).evaluate(spec(budget=budget), one_call, None, adapter)
    assert result.output == "private-answer"
    assert result.tokens_in + result.tokens_out == 3_000_000
    assert result.cost_usd == pytest.approx(0.01)
    row = store.rows()[0]
    assert row.accepted and row.tokens_in + row.tokens_out == 3_000_000
    assert row.cost_usd == pytest.approx(0.01)


class FailedAdapter(UnderquotingAdapter):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise AdapterError("private-error-message")


async def test_failed_call_retains_reserved_usage(tmp_path: Path) -> None:
    store = TelemetryStore(tmp_path / "telemetry")
    with pytest.raises(AdapterError):
        await Runtime(store).evaluate(spec(), one_call, None, FailedAdapter())
    row = store.rows()[0]
    assert row.tokens_in + row.tokens_out == 2
    assert row.cost_usd == pytest.approx(0.001)
    assert "private-error-message" not in (
        tmp_path / "telemetry" / "runs.jsonl"
    ).read_text(encoding="utf-8")


class SlowAdapter(UnderquotingAdapter):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        await asyncio.Event().wait()
        return response()


async def test_model_timeout_retains_reserved_usage(tmp_path: Path) -> None:
    store = TelemetryStore(tmp_path / "telemetry")
    with pytest.raises(BudgetExceeded):
        await Runtime(store).evaluate(
            spec(budget=Budget(compute_s=0.01)), one_call, None, SlowAdapter()
        )
    row = store.rows()[0]
    assert row.tokens_in + row.tokens_out == 2
    assert not row.accepted


async def test_exception_is_logged_without_content(tmp_path: Path) -> None:
    store = TelemetryStore(tmp_path / "telemetry")

    async def failure(context: RunContext, task: JsonValue) -> JsonValue:
        raise ValueError("private-error-message")

    with pytest.raises(ValueError, match="private-error-message"):
        await Runtime(store).evaluate(spec(), failure, None, FakeAdapter())
    assert len(store.rows()) == 1
    assert not store.rows()[0].accepted


async def test_expire_attempt_persists_across_runtime_instances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = licensed_artifact(tmp_path, monkeypatch)
    store = TelemetryStore(tmp_path / "telemetry")
    adapter = FakeAdapter(responses=[response(), response()])
    await Runtime(store).run(value, None, adapter)
    loaded = HarnessArtifact.load(value.directory)
    with pytest.raises(ArtifactExpiredError):
        await Runtime(store).run(loaded, None, adapter)
    assert adapter.calls == 1
    assert len(store.rows()) == 2


async def test_failed_expire_attempt_is_still_consumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = licensed_artifact(tmp_path, monkeypatch)
    store = TelemetryStore(tmp_path / "telemetry")
    with pytest.raises(AdapterError):
        await Runtime(store).run(value, None, FailedAdapter())
    with pytest.raises(ArtifactExpiredError):
        await Runtime(store).run(value, None, FakeAdapter())
    assert len(store.rows()) == 2
    assert all(not row.accepted for row in store.rows())


async def test_install_artifact_can_repeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = licensed_artifact(tmp_path, monkeypatch, lifecycle=Lifecycle.INSTALL)
    store = TelemetryStore(tmp_path / "telemetry")
    adapter = FakeAdapter(responses=[response(), response()])
    await Runtime(store).run(value, None, adapter)
    await Runtime(store).run(value, None, adapter)
    assert adapter.calls == 2
    assert len(store.rows()) == 2


async def test_model_binding_rejects_before_consumption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = licensed_artifact(tmp_path, monkeypatch)
    store = TelemetryStore(tmp_path / "telemetry")
    adapter = FakeAdapter()
    adapter.model = "different"
    with pytest.raises(UnlicensedArtifactError):
        await Runtime(store).run(value, None, adapter)
    assert adapter.calls == 0
    assert not (value.directory / ".consumed").exists()
    assert not store.rows()[0].accepted


async def test_tampered_code_rejected_with_telemetry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = licensed_artifact(tmp_path, monkeypatch)
    (value.directory / "harness.py").write_text("# modified\n", encoding="utf-8")
    store = TelemetryStore(tmp_path / "telemetry")
    adapter = FakeAdapter()
    with pytest.raises(UnlicensedArtifactError):
        await Runtime(store).run(value, None, adapter)
    assert adapter.calls == 0
    assert len(store.rows()) == 1


async def test_code_mutation_during_run_blocks_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = (
        "from pathlib import Path\n"
        "async def run(context, task):\n"
        "    Path(__file__).write_text('# modified\\n', encoding='utf-8')\n"
        "    return 'must-not-deliver'\n"
    )
    value = licensed_artifact(tmp_path, monkeypatch, source=source)
    store = TelemetryStore(tmp_path / "telemetry")
    with pytest.raises(UnlicensedArtifactError):
        await Runtime(store).run(value, None, FakeAdapter())
    assert not store.rows()[0].accepted
    assert (value.directory / ".consumed").exists()


async def test_concurrent_calls_reserve_once_before_execution() -> None:
    adapter = FakeAdapter(responses=[response(), response()])
    context = RunContext(adapter, Budget(cost_usd=0.015))
    results = await asyncio.gather(
        context.call("first"), context.call("second"), return_exceptions=True
    )
    assert adapter.calls == 1
    assert sum(isinstance(item, BudgetExceeded) for item in results) == 1


async def test_concurrent_expire_attempts_only_run_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = licensed_artifact(tmp_path, monkeypatch)
    store = TelemetryStore(tmp_path / "telemetry")
    adapter = FakeAdapter(responses=[response(), response()])
    results = await asyncio.gather(
        Runtime(store).run(value, None, adapter),
        Runtime(store).run(value, None, adapter),
        return_exceptions=True,
    )
    assert adapter.calls == 1
    assert sum(isinstance(item, ArtifactExpiredError) for item in results) == 1
    assert len(store.rows()) == 2


async def test_external_cancellation_still_emits_one_row(tmp_path: Path) -> None:
    store = TelemetryStore(tmp_path / "telemetry")
    entered = asyncio.Event()

    async def wait(context: RunContext, task: JsonValue) -> JsonValue:
        entered.set()
        await asyncio.Event().wait()
        return None

    execution = asyncio.create_task(Runtime(store).evaluate(spec(), wait, None, FakeAdapter()))
    await entered.wait()
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution
    assert len(store.rows()) == 1
    assert not store.rows()[0].accepted


async def test_evaluation_allows_ladder_target(tmp_path: Path) -> None:
    candidate = spec().model_copy(update={"model_target": "cheapest that passes"})
    store = TelemetryStore(tmp_path / "telemetry")
    result = await Runtime(store).evaluate(candidate, one_call, None, FakeAdapter())
    assert result.output == "ok"
    assert store.rows()[0].accepted


async def test_evaluation_rejects_wrong_explicit_model(tmp_path: Path) -> None:
    candidate = spec().model_copy(update={"model_target": "different"})
    store = TelemetryStore(tmp_path / "telemetry")
    adapter = FakeAdapter()
    with pytest.raises(AdapterError):
        await Runtime(store).evaluate(candidate, one_call, None, adapter)
    assert adapter.calls == 0
    assert len(store.rows()) == 1
    assert not store.rows()[0].accepted


async def test_only_authenticated_persisted_source_executes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = licensed_artifact(
        tmp_path, monkeypatch, source="async def run(context, task):\n    return 'disk-result'\n"
    )
    store = TelemetryStore(tmp_path / "telemetry")
    adapter = FakeAdapter()
    result = await Runtime(store).run(value, None, adapter)
    assert result.output == "disk-result"
    assert adapter.calls == 0


async def test_loader_supports_internal_evaluation(tmp_path: Path) -> None:
    value = artifact(tmp_path)
    loaded = load_harness(value.directory)
    store = TelemetryStore(tmp_path / "telemetry")
    result = await Runtime(store).evaluate(value.spec, loaded, None, FakeAdapter())
    assert result.output == "ok"


@pytest.mark.parametrize("source", ["# no entrypoint\n", "def run(context, task): return None\n"])
async def test_bad_entrypoint_consumes_licensed_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    value = licensed_artifact(tmp_path, monkeypatch, source=source)
    store = TelemetryStore(tmp_path / "telemetry")
    with pytest.raises(TypeError, match="async run"):
        await Runtime(store).run(value, None, FakeAdapter())
    assert len(store.rows()) == 1
    assert not store.rows()[0].accepted
    assert (value.directory / ".consumed").exists()


async def test_unlicensed_source_is_not_loaded(tmp_path: Path) -> None:
    value = artifact(tmp_path, source="raise AssertionError('must-not-load')\n")
    store = TelemetryStore(tmp_path / "telemetry")
    with pytest.raises(UnlicensedArtifactError):
        await Runtime(store).run(value, None, FakeAdapter())
    assert len(store.rows()) == 1


async def test_module_mutation_during_load_blocks_entrypoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = (
        "from pathlib import Path\n"
        "Path(__file__).write_text('# changed\\n', encoding='utf-8')\n"
        "async def run(context, task):\n"
        "    raise AssertionError('must-not-run')\n"
    )
    value = licensed_artifact(tmp_path, monkeypatch, source=source)
    store = TelemetryStore(tmp_path / "telemetry")
    with pytest.raises(UnlicensedArtifactError, match="while loading"):
        await Runtime(store).run(value, None, FakeAdapter())
    assert not store.rows()[0].accepted


async def test_closed_context_cannot_call_again(tmp_path: Path) -> None:
    store = TelemetryStore(tmp_path / "telemetry")
    captured: list[RunContext] = []

    async def capture(context: RunContext, task: JsonValue) -> JsonValue:
        captured.append(context)
        return None

    adapter = FakeAdapter()
    await Runtime(store).evaluate(spec(), capture, None, adapter)
    with pytest.raises(BudgetExceeded, match="closed"):
        await captured[0].call("must-not-send")
    assert adapter.calls == 0


async def test_expired_context_rejects_before_quote(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = FakeAdapter()
    context = RunContext(adapter, Budget(compute_s=1))
    start = context._started
    monkeypatch.setattr("architect.runtime.time.monotonic", lambda: start + 2)
    with pytest.raises(BudgetExceeded, match="Run budget exceeded"):
        await context.call("must-not-send")
    assert adapter.calls == 0


async def test_direct_model_timeout_poisoned_context() -> None:
    context = RunContext(SlowAdapter(), Budget(compute_s=0.01))
    with pytest.raises(BudgetExceeded):
        await context.call("private-input")
    with pytest.raises(BudgetExceeded):
        await context.call("must-not-send")


class WrongResponseModelAdapter(UnderquotingAdapter):
    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=3, tokens_out=2, cost_usd=0.01)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return response().model_copy(update={"model": "wrong"})


async def test_response_model_mismatch_blocks_delivery(tmp_path: Path) -> None:
    store = TelemetryStore(tmp_path / "telemetry")
    with pytest.raises(AdapterError, match="Response model"):
        await Runtime(store).evaluate(spec(), one_call, None, WrongResponseModelAdapter())
    assert not store.rows()[0].accepted
    assert store.rows()[0].tokens_in == 3


class BackgroundAdapter(UnderquotingAdapter):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.finished = False

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()
            self.finished = True
        return response().model_copy(update={"tokens_out": 100})


async def test_unfinished_model_call_blocks_delivery_and_drains_before_logging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = (
        "import asyncio\n"
        "async def run(context, task):\n"
        "    asyncio.create_task(context.call('private-input'))\n"
        "    await context.adapter.started.wait()\n"
        "    return 'must-not-deliver'\n"
    )
    value = licensed_artifact(tmp_path, monkeypatch, source=source)
    store = TelemetryStore(tmp_path / "telemetry")
    adapter = BackgroundAdapter()
    with pytest.raises(BudgetExceeded, match="unfinished"):
        await Runtime(store).run(value, None, adapter)
    assert adapter.finished
    assert adapter.cancelled.is_set()
    rows = store.rows()
    assert len(rows) == 1
    assert not rows[0].accepted
    assert rows[0].tokens_in + rows[0].tokens_out == 2
    assert rows[0].cost_usd == pytest.approx(0.001)


async def test_failed_harness_drains_background_call_before_telemetry(tmp_path: Path) -> None:
    store = TelemetryStore(tmp_path / "telemetry")
    adapter = BackgroundAdapter()
    background: list[asyncio.Task[str]] = []

    async def failure(context: RunContext, task: JsonValue) -> JsonValue:
        background.append(asyncio.create_task(context.call("private-input")))
        await adapter.started.wait()
        raise ValueError("private-failure")

    with pytest.raises(ValueError, match="private-failure"):
        await Runtime(store).evaluate(spec(), failure, None, adapter)
    assert all(task.done() for task in background)
    assert adapter.finished
    assert len(store.rows()) == 1
    assert not store.rows()[0].accepted
    assert store.rows()[0].tokens_in + store.rows()[0].tokens_out == 2


async def test_cancelled_record_still_persists_queued_row(tmp_path: Path) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from uuid import uuid4

    source = TelemetryStore(tmp_path / "source")
    await Runtime(source).evaluate(spec(), one_call, "task", FakeAdapter(responses=[response()]))
    row = source.rows()[0].model_copy(update={"run_id": str(uuid4())})

    store = TelemetryStore(tmp_path / "telemetry")
    runtime = Runtime(store)
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(executor)
    release = threading.Event()
    busy = loop.run_in_executor(None, release.wait)
    try:
        recording = asyncio.create_task(runtime.record(row))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not recording.done()
        recording.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not recording.done()  # still waiting for the queued append
        release.set()
        await busy
        with pytest.raises(asyncio.CancelledError):
            await recording
        assert [item.run_id for item in store.rows()] == [row.run_id]
    finally:
        release.set()
        executor.shutdown(wait=True)


# --- Phase 8: images threaded through RunContext.call ---------------------------------

from image_support import png_bytes  # noqa: E402

from architect.models.images import ImageInput  # noqa: E402

PAGE = ImageInput.from_bytes(png_bytes(32, 40, shade=0x80))
OTHER = ImageInput.from_bytes(png_bytes(24, 24, shade=0x10))


class QuoteRecordingAdapter(FakeAdapter):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.quotes: list[UsageQuote] = []
        self.requests: list[ModelRequest] = []

    def quote(self, request: ModelRequest) -> UsageQuote:
        quote = super().quote(request)
        self.quotes.append(quote)
        return quote

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return await super().complete(request)


async def test_call_with_images_reserves_ceiling_per_image() -> None:
    adapter = QuoteRecordingAdapter(responses=[response(cost=0.0)])
    context = RunContext(adapter, Budget(cost_usd=1))
    context.attach_images([PAGE])
    assert await context.call("read", images=[PAGE.stub(1), OTHER]) == "private-answer"
    # The fake's image ceiling is 100 tokens per image at $1/M, reserved before I/O.
    assert adapter.quotes[0].tokens_in == 3 + 2 * 100
    assert adapter.quotes[0].cost_usd == pytest.approx(200 / 1_000_000)
    request = adapter.requests[0]
    assert [image.sha256 for image in request.images] == [PAGE.sha256, OTHER.sha256]
    assert context.images_sent == 2
    assert context.tokens_in == 203


async def test_text_only_call_keeps_its_exact_request_shape() -> None:
    adapter = QuoteRecordingAdapter(responses=[response()])
    context = RunContext(adapter, Budget(cost_usd=1))
    context.attach_images([PAGE])
    await context.call("plain", max_output_tokens=7, system="sys:")
    assert adapter.requests[0] == ModelRequest(
        prompt="sys:plain", max_output_tokens=7, cache_prefix="sys:"
    )
    assert context.images_sent == 0


async def test_unattached_or_mismatched_stub_is_unresolved_before_reservation() -> None:
    adapter = QuoteRecordingAdapter(responses=[response()])
    context = RunContext(adapter, Budget(cost_usd=1))
    with pytest.raises(AdapterError, match="^Image reference is not attached to this run$"):
        await context.call("read", images=[PAGE.stub(1)])
    assert adapter.calls == 0 and adapter.quotes == []
    assert context.cost_usd == 0 and context.images_sent == 0

    lying = RunContext(QuoteRecordingAdapter(responses=[response()]), Budget(cost_usd=1))
    lying.attach_images([PAGE])
    forged = PAGE.stub(1).model_copy(update={"width": PAGE.width + 1})
    with pytest.raises(AdapterError, match="^Image reference is not attached to this run$"):
        await lying.call("read", images=[forged])


def test_attach_images_is_idempotent_and_typed() -> None:
    context = RunContext(FakeAdapter(), Budget())
    context.attach_images([PAGE, PAGE])
    context.attach_images([PAGE])
    assert context._attached_image(PAGE.sha256) is PAGE
    assert context._attached_image(OTHER.sha256) is None
    with pytest.raises(TypeError):
        context.attach_images([PAGE.stub(1)])  # type: ignore[list-item]


async def test_descendants_resolve_through_lineage_and_count_on_every_ancestor() -> None:
    parent = RunContext(FakeAdapter(responses=[response(cost=0.0)]), Budget(cost_usd=1))
    parent.attach_images([PAGE])
    child = parent.fork(FakeAdapter(responses=[response(cost=0.0)]), Budget(cost_usd=1))
    await child.call("read", images=[PAGE.stub(1)])
    assert child.images_sent == 1 and parent.images_sent == 1
    # An image attached to a child is not visible to its parent.
    child.attach_images([OTHER])
    assert parent._attached_image(OTHER.sha256) is None


async def test_images_sent_is_written_to_telemetry(tmp_path: Path) -> None:
    store = TelemetryStore(tmp_path / "telemetry")
    adapter = FakeAdapter(responses=[response(cost=0.0), response(cost=0.0)])

    async def two_image_calls(context: RunContext, task: JsonValue) -> JsonValue:
        context.attach_images([PAGE])
        await context.call("private-first", images=[PAGE.stub(1)])
        return await context.call("private-second", images=[PAGE.stub(1), OTHER])

    await Runtime(store).evaluate(spec(), two_image_calls, None, adapter)
    row = store.rows()[0]
    assert row.image_inputs == 3
    assert row.accepted
    journal = (tmp_path / "telemetry" / "runs.jsonl").read_text(encoding="utf-8")
    assert PAGE.sha256 not in journal and "image/png" not in journal
    await Runtime(store).evaluate(spec(), one_call, None, FakeAdapter(responses=[response()]))
    assert store.rows()[1].image_inputs == 0


class OverReportingImageAdapter(FakeAdapter):
    """Quotes the image ceiling, then reports more image tokens than it reserved."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        measured = await super().complete(request)
        return measured.model_copy(update={"cost_usd": measured.cost_usd + 0.001})


async def test_adapter_reporting_more_than_reserved_is_budget_exceeded(tmp_path: Path) -> None:
    store = TelemetryStore(tmp_path / "telemetry")

    async def page_call(context: RunContext, task: JsonValue) -> JsonValue:
        return await context.call("read", images=[PAGE])

    with pytest.raises(BudgetExceeded, match="^Adapter exceeded its reserved usage$"):
        await Runtime(store).evaluate(spec(), page_call, None, OverReportingImageAdapter())
    row = store.rows()[0]
    assert not row.accepted and row.image_inputs == 1


async def test_model_without_image_price_refuses_before_io(tmp_path: Path) -> None:
    import json

    prices = json.loads(
        (Path(__file__).parents[1] / "architect" / "models" / "prices.json").read_text(
            encoding="utf-8"
        )
    )
    del prices["models"]["fake"]["image"]
    path = tmp_path / "prices.json"
    path.write_text(json.dumps(prices), encoding="utf-8")
    adapter = FakeAdapter(prices_path=path)
    context = RunContext(adapter, Budget(cost_usd=1))
    with pytest.raises(AdapterError, match="^Model is not configured for image input$"):
        await context.call("read", images=[PAGE])
    assert adapter.calls == 0 and context.images_sent == 0


async def test_image_detail_comes_from_the_model_price(tmp_path: Path) -> None:
    import json

    prices = json.loads(
        (Path(__file__).parents[1] / "architect" / "models" / "prices.json").read_text(
            encoding="utf-8"
        )
    )
    prices["models"]["fake"]["image"]["detail"] = "low"
    path = tmp_path / "prices.json"
    path.write_text(json.dumps(prices), encoding="utf-8")
    adapter = QuoteRecordingAdapter(prices_path=path)
    context = RunContext(adapter, Budget(cost_usd=1))
    await context.call("read", images=[PAGE])
    await context.call("text only")
    assert adapter.requests[0].image_detail == "low"
    assert adapter.requests[1].image_detail == "auto"


async def test_image_detail_survives_a_delegating_wrapper(tmp_path: Path) -> None:
    import json

    prices = json.loads(
        (Path(__file__).parents[1] / "architect" / "models" / "prices.json").read_text(
            encoding="utf-8"
        )
    )
    prices["models"]["fake"]["image"]["detail"] = "high"
    path = tmp_path / "prices.json"
    path.write_text(json.dumps(prices), encoding="utf-8")
    inner = QuoteRecordingAdapter(prices_path=path)

    class Wrapper:
        model = "fake"

        def __init__(self) -> None:
            self._inner = inner

        def quote(self, request: ModelRequest) -> UsageQuote:
            return inner.quote(request)

        async def complete(self, request: ModelRequest) -> ModelResponse:
            return await inner.complete(request)

    await RunContext(Wrapper(), Budget(cost_usd=1)).call("read", images=[PAGE])
    assert inner.requests[0].image_detail == "high"
