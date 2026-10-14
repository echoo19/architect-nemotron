"""Independent adversarial gate tests. All model activity is local and fake."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from architect.models import FakeAdapter, ModelRequest, ModelResponse, UsageQuote
from architect.runtime import BudgetExceeded, RunContext, Runtime, UnlicensedArtifactError
from architect.spec import Budget, FloorDefinition, FloorKind, HarnessArtifact, Lifecycle, Spec
from architect.spec import TestReport as VerificationReport
from architect.telemetry import TelemetryRow, TelemetryStore, categorical


@pytest.fixture(autouse=True)
def isolated_signing_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCHITECT_SIGNING_KEY_FILE", str(tmp_path / "signing.key"))


def make_spec(*, budget: Budget | None = None, domain: str = "general") -> Spec:
    return Spec(
        task="Private task text must never become telemetry",
        input_schema={},
        output_schema={},
        floor=FloorDefinition(
            kind=FloorKind.HELD_OUT_TASKS,
            limit=0.8,
            dataset="private-dataset",
        ),
        budget=budget or Budget(cost_usd=0.1, compute_s=1),
        domain=domain,
        lifecycle=Lifecycle.INSTALL,
    )


def make_artifact(tmp_path: Path, *, spec: Spec | None = None) -> HarnessArtifact:
    directory = tmp_path / "artifact"
    directory.mkdir(exist_ok=True)
    (directory / "harness.py").write_text(
        "async def run(context, task):\n    return task\n", encoding="utf-8"
    )
    return HarnessArtifact(
        spec=spec or make_spec(),
        preset="toy",
        model="fake",
        components=("private-component",),
        directory=directory,
    )


def passing_report(artifact: HarnessArtifact, *, signature: str = "") -> VerificationReport:
    return VerificationReport(
        spec_hash=artifact.spec.spec_hash,
        artifact_digest=artifact.artifact_digest,
        passed=True,
        floor_score=1,
        floor_limit=artifact.spec.floor.limit,
        floor_kind=artifact.spec.floor.kind,
        tokens=1,
        cost_usd=0.001,
        compute_s=0.001,
        component_count=len(artifact.components),
        verification_id=str(uuid4()),
        signature=signature,
    )


def scripted_response(*, tokens: int = 6, cost: float = 0.01) -> ModelResponse:
    return ModelResponse(
        text="private-result", tokens_in=tokens, tokens_out=0, cost_usd=cost, model="fake"
    )


def row_data() -> dict[str, object]:
    return {
        "run_id": str(uuid4()),
        "timestamp": datetime.now(UTC),
        "domain": categorical("general"),
        "task_type": categorical("toy"),
        "spec_hash": "a" * 64,
        "harness_version": 1,
        "lifecycle": Lifecycle.INSTALL,
        "preset_origin": categorical("toy"),
        "component_set": (categorical("toy"),),
        "model": categorical("fake"),
        "model_params": {"temperature": 0.0},
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_usd": 0.0,
        "wall_time_s": 0.0,
        "floor_type": FloorKind.HELD_OUT_TASKS,
        "floor_score": None,
        "floor_limit": 0.8,
        "accepted": False,
    }


@pytest.mark.parametrize("signature", ["", "model-says-approved", "0" * 64])
async def test_model_forged_report_never_executes_and_logs_rejection(
    tmp_path: Path,
    signature: str,
) -> None:
    secret = "sk-private-7654321"
    artifact = make_artifact(tmp_path, spec=make_spec(domain=secret))
    import_marker = tmp_path / "unsigned-imported"
    execute_marker = tmp_path / "unsigned-executed"
    (artifact.directory / "harness.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(import_marker)!r}).write_text('imported')\n"
        "async def run(context, task):\n"
        f"    Path({str(execute_marker)!r}).write_text('executed')\n"
        "    return task\n",
        encoding="utf-8",
    )
    report = passing_report(artifact, signature=signature)
    artifact = artifact.model_copy(update={"report": report})
    artifact.save()
    artifact = HarnessArtifact.load(artifact.directory)
    assert not artifact.licensed

    store = TelemetryStore(tmp_path / "telemetry")
    adapter = FakeAdapter()
    with pytest.raises(UnlicensedArtifactError):
        await Runtime(store).run(artifact, {"secret": secret}, adapter)
    assert not import_marker.exists()
    assert not execute_marker.exists()
    assert adapter.calls == 0
    rows = store.rows()
    assert len(rows) == 1
    assert rows[0].accepted is False
    assert rows[0].tokens_in == rows[0].tokens_out == 0
    for path in (tmp_path / "telemetry").iterdir():
        if path.is_file():
            assert secret.encode() not in path.read_bytes()
            assert b"private-component" not in path.read_bytes()
            assert b"Private task text" not in path.read_bytes()


def test_license_cannot_be_supplied_as_constructor_authority(tmp_path: Path) -> None:
    payload = make_artifact(tmp_path).model_dump()
    payload["licensed"] = True
    with pytest.raises(ValidationError):
        HarnessArtifact.model_validate(payload)


def test_artifact_code_and_nested_config_changes_change_digest(tmp_path: Path) -> None:
    artifact = make_artifact(tmp_path)
    original = artifact.artifact_digest
    (artifact.directory / "harness.py").write_text("# substituted code\n", encoding="utf-8")
    assert artifact.artifact_digest != original
    original = artifact.artifact_digest
    artifact.params["nested"] = {"secret": "changed"}
    assert artifact.artifact_digest != original
    original = artifact.artifact_digest
    artifact.spec.input_schema["extra"] = {"type": "string"}
    assert artifact.artifact_digest != original


@pytest.mark.parametrize("limit", ["tokens", "cost"])
async def test_quotes_enforce_money_but_not_token_quotas(limit: str) -> None:
    response = scripted_response(
        tokens=6 if limit == "tokens" else 1, cost=0.06 if limit == "cost" else 0.001
    )
    adapter = FakeAdapter(responses=[response, response])
    context = RunContext(adapter=adapter, budget=Budget(cost_usd=0.1, compute_s=1))
    assert await context.call("first") == "private-result"
    if limit == "tokens":
        assert await context.call("second") == "private-result"
        assert adapter.calls == 2 and context.tokens_in == 12
        assert context.cost_usd == pytest.approx(0.002)
    else:
        with pytest.raises(BudgetExceeded):
            await context.call("second")
        assert adapter.calls == 1
        assert context.tokens_in == response.tokens_in
        assert context.cost_usd == pytest.approx(response.cost_usd)


@pytest.mark.parametrize("limit", ["tokens", "cost"])
async def test_first_quote_refuses_dollars_but_allows_high_tokens(limit: str) -> None:
    adapter = FakeAdapter(
        responses=[
            scripted_response(
                tokens=11 if limit == "tokens" else 1,
                cost=0.11 if limit == "cost" else 0.001,
            )
        ]
    )
    context = RunContext(adapter=adapter, budget=Budget(cost_usd=0.1, compute_s=1))
    if limit == "tokens":
        assert await context.call("large token use is not a quota breach") == "private-result"
        assert adapter.calls == 1 and context.tokens_in == 11
    else:
        with pytest.raises(BudgetExceeded):
            await context.call("must not leave process")
        assert adapter.calls == 0


class UnderquotingAdapter:
    model = "fake"

    def __init__(self, *, tokens: int, cost: float) -> None:
        self.calls = 0
        self.tokens = tokens
        self.cost = cost

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=1, tokens_out=0, cost_usd=0.001)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return scripted_response(tokens=self.tokens, cost=self.cost)


@pytest.mark.parametrize("limit", ["tokens", "cost"])
async def test_underquote_reconciles_tokens_but_dollar_overrun_poisons_context(limit: str) -> None:
    adapter = UnderquotingAdapter(
        tokens=11 if limit == "tokens" else 1, cost=0.11 if limit == "cost" else 0.001
    )
    context = RunContext(adapter=adapter, budget=Budget(cost_usd=0.1, compute_s=1))
    if limit == "tokens":
        assert await context.call("first") == "private-result"
        assert context.tokens_in == adapter.tokens
        assert await context.call("token estimate may be exceeded") == "private-result"
        assert adapter.calls == 2 and context.tokens_in == 22
        assert context.cost_usd == pytest.approx(adapter.cost * 2)
    else:
        with pytest.raises(BudgetExceeded):
            await context.call("first")
        assert context.tokens_in == adapter.tokens
        assert context.cost_usd == pytest.approx(adapter.cost)
        with pytest.raises(BudgetExceeded):
            await context.call("retry after dollar overrun")
        assert adapter.calls == 1


class YieldingAdapter:
    model = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=6, tokens_out=0, cost_usd=0.06)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        # Yield to a concurrent caller while its sibling has reserved budget.
        await asyncio.sleep(0)
        return scripted_response(tokens=6, cost=0.06)


async def test_concurrent_callers_cannot_double_spend_reservation() -> None:
    adapter = YieldingAdapter()
    context = RunContext(adapter=adapter, budget=Budget(cost_usd=0.1, compute_s=1))
    results = await asyncio.gather(context.call("one"), context.call("two"), return_exceptions=True)
    assert adapter.calls == 1
    assert sum(isinstance(result, BudgetExceeded) for result in results) >= 1
    assert sum(isinstance(result, str) for result in results) <= 1


class NeverCompletingAdapter:
    model = "fake"

    def __init__(self) -> None:
        self.calls = 0
        self.cancelled = False

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=1, tokens_out=0, cost_usd=0.001)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unset event returned")


async def test_compute_budget_cancels_call_and_prevents_retry() -> None:
    adapter = NeverCompletingAdapter()
    context = RunContext(adapter=adapter, budget=Budget(cost_usd=0.1, compute_s=0.03))
    with pytest.raises(BudgetExceeded):
        await asyncio.wait_for(context.call("blocked"), timeout=2)
    assert adapter.cancelled
    with pytest.raises(BudgetExceeded):
        await context.call("retry")
    assert adapter.calls == 1


@pytest.mark.parametrize("secret", ["pin7", "a" * 300])
@pytest.mark.parametrize("field", ["domain", "task_type", "preset_origin", "model"])
def test_telemetry_rejects_raw_secrets_even_when_short(field: str, secret: str) -> None:
    data = row_data()
    data[field] = secret
    with pytest.raises(ValidationError):
        TelemetryRow.model_validate(data)


@pytest.mark.parametrize("secret", ["pin7", "a" * 300])
def test_telemetry_rejects_nested_component_secret(secret: str) -> None:
    data = row_data()
    data["component_set"] = (secret,)
    with pytest.raises(ValidationError):
        TelemetryRow.model_validate(data)


@pytest.mark.parametrize(
    "params",
    [
        {"prompt": "pin7"},
        {"temperature": "pin7"},
        {"temperature": "7"},
        {"temperature": {"secret": "pin7"}},
        {"temperature": ["pin7"]},
        {"prompt": {"deep": {"secret": "x" * 300}}},
        {"temperature": float("nan")},
        {"temperature": float("inf")},
    ],
)
def test_telemetry_model_params_cannot_carry_nested_or_coerced_content(
    params: dict[str, object],
) -> None:
    data = row_data()
    data["model_params"] = params
    with pytest.raises(ValidationError):
        TelemetryRow.model_validate(data)


@pytest.mark.parametrize("field", ["prompt", "result", "exception", "metadata"])
def test_telemetry_rejects_extra_content_fields(field: str) -> None:
    data = row_data()
    data[field] = {"nested": "pin7"}
    with pytest.raises(ValidationError):
        TelemetryRow.model_validate(data)


def test_categorical_hashes_unknown_short_and_long_secrets() -> None:
    for secret in ("pin7", "account-secret-" * 100):
        encoded = categorical(secret)
        assert encoded.startswith("h_")
        assert len(encoded) == 34
        assert all(char in "0123456789abcdef" for char in encoded[2:])
        assert encoded == categorical(secret)
        assert secret not in encoded


def test_telemetry_persistence_contains_only_hashed_categories(tmp_path: Path) -> None:
    secret = "pin7"
    data = row_data()
    data["domain"] = categorical(secret)
    data["component_set"] = (categorical(secret),)
    store = TelemetryStore(tmp_path / "telemetry")
    store.append(TelemetryRow.model_validate(data))
    assert len(store.rows()) == 1
    for path in (tmp_path / "telemetry").iterdir():
        if path.is_file():
            assert secret.encode() not in path.read_bytes()


# Credentials below are test setup only. Production authority belongs to stage 4.
def authenticated(artifact: HarnessArtifact) -> HarnessArtifact:
    from architect._authority import _sign_report

    report = passing_report(artifact)
    signature = _sign_report(report.model_dump(mode="json", exclude={"signature"}))
    report = report.model_copy(update={"signature": signature})
    return artifact.model_copy(update={"report": report})


@pytest.mark.parametrize("mutation", ["code", "params", "spec", "missing_code"])
async def test_authentic_report_does_not_license_mutated_artifact(
    tmp_path: Path,
    mutation: str,
) -> None:
    artifact = authenticated(make_artifact(tmp_path))
    assert artifact.licensed
    artifact.save()
    artifact = HarnessArtifact.load(artifact.directory)
    assert artifact.licensed
    import_marker = tmp_path / "mutated-imported"
    if mutation == "code":
        (artifact.directory / "harness.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(import_marker)!r}).write_text('imported')\n"
            "async def run(context, task):\n    return task\n",
            encoding="utf-8",
        )
    elif mutation == "missing_code":
        (artifact.directory / "harness.py").unlink()
    elif mutation == "params":
        artifact.params["nested"] = {"changed": True}
    else:
        artifact.spec.output_schema["changed"] = True
    assert not artifact.licensed

    store = TelemetryStore(tmp_path / "telemetry")
    with pytest.raises(UnlicensedArtifactError):
        await Runtime(store).run(artifact, None, FakeAdapter())
    assert not import_marker.exists()
    assert len(store.rows()) == 1
    assert not store.rows()[0].accepted


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("spec_hash", "b" * 64),
        ("artifact_digest", "c" * 64),
        ("passed", False),
        ("floor_score", 0.9),
        ("floor_limit", 0.7),
        ("floor_kind", FloorKind.HUMAN_EDIT_RATE),
        ("tokens", 2),
        ("cost_usd", 0.002),
        ("compute_s", 0.002),
        ("component_count", 2),
        ("failures", ("forged",)),
        ("verification_id", "substituted"),
    ],
)
def test_every_report_value_is_authenticated(
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    artifact = authenticated(make_artifact(tmp_path))
    assert artifact.licensed
    assert artifact.report is not None
    payload = artifact.report.model_dump()
    payload[field] = replacement
    changed = VerificationReport.model_validate(payload)
    artifact = artifact.model_copy(update={"report": changed})
    assert not artifact.licensed


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("floor_score", 0.1),
        ("cost_usd", 0.11),
        ("compute_s", 1.1),
        ("component_count", 0),
        ("passed", False),
        ("failures", ("failed",)),
    ],
)
def test_valid_signature_cannot_override_code_gates(
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    from architect._authority import _sign_report

    artifact = make_artifact(tmp_path)
    payload = passing_report(artifact).model_dump()
    payload[field] = replacement
    report = VerificationReport.model_validate(payload)
    report = report.model_copy(
        update={
            "signature": _sign_report(report.model_dump(mode="json", exclude={"signature"})),
        }
    )
    assert not artifact.model_copy(update={"report": report}).licensed


async def test_harness_without_model_calls_still_has_total_compute_deadline(
    tmp_path: Path,
) -> None:
    cancelled = False

    async def blocked(context: RunContext, task: object) -> str:
        nonlocal cancelled
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled = True
            raise
        raise AssertionError("unset event returned")

    store = TelemetryStore(tmp_path / "telemetry")
    spec = make_spec(budget=Budget(cost_usd=0.1, compute_s=0.03))
    adapter = FakeAdapter()
    with pytest.raises(BudgetExceeded):
        await asyncio.wait_for(Runtime(store).evaluate(spec, blocked, None, adapter), timeout=2)
    assert cancelled
    assert adapter.calls == 0
    assert len(store.rows()) == 1
    assert not store.rows()[0].accepted


async def test_catching_actual_overrun_cannot_deliver_output(tmp_path: Path) -> None:
    async def swallow(context: RunContext, task: object) -> str:
        try:
            await context.call("private-prompt")
        except BudgetExceeded:
            return "unbudgeted-result"
        pytest.fail("underquote was not detected")

    store = TelemetryStore(tmp_path / "telemetry")
    adapter = UnderquotingAdapter(tokens=11, cost=0.11)
    with pytest.raises(BudgetExceeded):
        await Runtime(store).evaluate(make_spec(), swallow, None, adapter)
    assert adapter.calls == 1
    assert len(store.rows()) == 1
    row = store.rows()[0]
    assert not row.accepted
    assert row.tokens_in == 11
    assert row.cost_usd == pytest.approx(0.11)


async def test_private_exception_is_logged_without_message_or_task(tmp_path: Path) -> None:
    secret = "pin7"

    async def fail(context: RunContext, task: object) -> str:
        await context.call(secret)
        raise RuntimeError(secret)

    store = TelemetryStore(tmp_path / "telemetry")
    adapter = FakeAdapter(responses=[scripted_response(tokens=1)])
    with pytest.raises(RuntimeError, match=secret):
        await Runtime(store).evaluate(make_spec(domain=secret), fail, {secret: secret}, adapter)
    assert len(store.rows()) == 1
    row = store.rows()[0]
    assert not row.accepted
    assert row.tokens_in == 1
    for path in (tmp_path / "telemetry").iterdir():
        if path.is_file():
            assert secret.encode() not in path.read_bytes()
            assert b"private-result" not in path.read_bytes()


async def test_expire_is_consumed_even_when_licensed_execution_fails(tmp_path: Path) -> None:
    from architect.runtime import ArtifactExpiredError

    spec = make_spec().model_copy(update={"lifecycle": Lifecycle.EXPIRE})
    artifact = make_artifact(tmp_path, spec=spec)
    attempts = tmp_path / "attempts"
    (artifact.directory / "harness.py").write_text(
        "from pathlib import Path\n"
        "async def run(context, task):\n"
        f"    with Path({str(attempts)!r}).open('a') as stream:\n"
        "        stream.write('attempt\\n')\n"
        "    raise ValueError('private-failure')\n",
        encoding="utf-8",
    )
    artifact = authenticated(artifact)
    artifact.save()

    store = TelemetryStore(tmp_path / "telemetry")
    runtime = Runtime(store)
    with pytest.raises(ValueError, match="private-failure"):
        await runtime.run(artifact, None, FakeAdapter())
    artifact = HarnessArtifact.load(artifact.directory)
    with pytest.raises(ArtifactExpiredError):
        await Runtime(store).run(artifact, None, FakeAdapter())
    assert attempts.read_text().splitlines() == ["attempt"]
    assert len(store.rows()) == 2
    assert all(not row.accepted for row in store.rows())


async def test_valid_license_cannot_be_used_with_another_model(tmp_path: Path) -> None:
    artifact = authenticated(make_artifact(tmp_path))
    assert artifact.licensed

    adapter = UnderquotingAdapter(tokens=1, cost=0.001)
    adapter.model = "other-fake"
    store = TelemetryStore(tmp_path / "telemetry")
    with pytest.raises(UnlicensedArtifactError):
        await Runtime(store).run(artifact, None, adapter)
    assert adapter.calls == 0
    assert len(store.rows()) == 1
    assert not store.rows()[0].accepted


async def test_input_and_output_tokens_are_metered_without_a_quota() -> None:
    response = ModelResponse(
        text="private-result", tokens_in=6, tokens_out=5, cost_usd=0.001, model="fake"
    )
    adapter = FakeAdapter(responses=[response])
    context = RunContext(adapter, Budget(cost_usd=0.1, compute_s=1))
    assert await context.call("input plus output are efficiency metrics") == "private-result"
    assert adapter.calls == 1 and context.tokens_in == 6 and context.tokens_out == 5


async def test_underquote_is_not_accepted_even_when_actual_usage_fits_total_budget() -> None:
    adapter = UnderquotingAdapter(tokens=2, cost=0.002)
    context = RunContext(adapter, Budget(cost_usd=0.1, compute_s=1))
    with pytest.raises(BudgetExceeded):
        await context.call("adapter violated reservation")
    assert context.tokens_in == 2
    assert context.cost_usd == pytest.approx(0.002)


@pytest.mark.parametrize("secret", ["pin7", "secret-" * 100])
def test_store_revalidates_nested_mutation_before_persistence(tmp_path: Path, secret: str) -> None:
    row = TelemetryRow.model_validate(row_data())
    # model_copy bypasses validation, as can a mutable mapping in a frozen model.
    changed = row.model_copy(update={"model_params": {"temperature": {"secret": secret}}})
    store = TelemetryStore(tmp_path / "telemetry")
    with pytest.raises(ValidationError):
        store.append(changed)
    assert store.rows() == []
    for path in (tmp_path / "telemetry").iterdir():
        if path.is_file():
            assert secret.encode() not in path.read_bytes()


async def test_runtime_executes_persisted_code_and_has_no_callback_override(tmp_path: Path) -> None:
    import inspect

    artifact = make_artifact(tmp_path)
    (artifact.directory / "harness.py").write_text(
        "async def run(context, task):\n    return 'persisted-code-result'\n",
        encoding="utf-8",
    )
    artifact = authenticated(artifact)
    artifact.save()
    store = TelemetryStore(tmp_path / "telemetry")
    runtime = Runtime(store)
    assert list(inspect.signature(runtime.run).parameters) == ["artifact", "task", "adapter"]
    called = False

    async def substitute(context: RunContext, task: object) -> str:
        nonlocal called
        called = True
        return "unverified-callback-result"

    # Deliberately call dynamically to probe the forbidden former API at runtime.
    invoke: Any = runtime.run
    with pytest.raises(TypeError):
        await invoke(artifact, substitute, None, FakeAdapter())
    with pytest.raises(TypeError):
        await invoke(artifact, None, FakeAdapter(), harness=substitute)
    assert not called
    result = await runtime.run(artifact, None, FakeAdapter())
    assert result.output == "persisted-code-result"
    assert len(store.rows()) == 1
    assert store.rows()[0].accepted


class DetachedAdapter:
    model = "fake"

    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.active = False
        self.cancelled = False
        self.finished = asyncio.Event()

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=1, tokens_out=0, cost_usd=0.001)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        self.active = True
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.active = False
            self.finished.set()
        return scripted_response(tokens=11, cost=0.11)


@pytest.mark.parametrize("entrypoint", ["evaluate", "disk"])
@pytest.mark.parametrize("ending", ["return", "raise"])
async def test_detached_model_calls_cannot_outlive_run_or_its_final_telemetry(
    tmp_path: Path,
    entrypoint: str,
    ending: str,
) -> None:
    adapter = DetachedAdapter()
    spawned: list[asyncio.Task[str]] = []

    async def detach(context: RunContext, task: object) -> str:
        spawned.append(asyncio.create_task(context.call("private-detached-prompt")))
        await adapter.started.wait()
        if ending == "raise":
            raise ValueError("private-detached-error")
        return "premature-result"

    store = TelemetryStore(tmp_path / "telemetry")
    runtime = Runtime(store)
    expected = ValueError if ending == "raise" else BudgetExceeded
    if entrypoint == "disk":
        artifact = make_artifact(tmp_path)
        finish = (
            "raise ValueError('private-detached-error')"
            if ending == "raise"
            else ("return 'premature-result'")
        )
        (artifact.directory / "harness.py").write_text(
            "import asyncio\n"
            "async def run(context, task):\n"
            "    pending = asyncio.create_task(context.call('private-detached-prompt'))\n"
            "    await context.adapter.started.wait()\n"
            f"    {finish}\n",
            encoding="utf-8",
        )
        artifact = authenticated(artifact)
        artifact.save()
        operation = runtime.run(artifact, None, adapter)
    else:
        operation = runtime.evaluate(make_spec(), detach, None, adapter)
    try:
        with pytest.raises(expected):
            await asyncio.wait_for(operation, timeout=2)
        assert adapter.calls == 1
        assert adapter.cancelled
        assert adapter.finished.is_set()
        assert not adapter.active
        assert all(task.done() for task in spawned)
        rows = store.rows()
        assert len(rows) == 1
        assert not rows[0].accepted
        assert rows[0].tokens_in == 1
        assert rows[0].tokens_out == 0
        assert rows[0].cost_usd == pytest.approx(0.001)
    finally:
        # Clean up a regression without leaving test-owned work behind.
        for pending in spawned:
            if not pending.done():
                pending.cancel()
        if spawned:
            await asyncio.gather(*spawned, return_exceptions=True)


@pytest.mark.parametrize("legacy", [1, 1000000])
def test_legacy_budget_tokens_are_ignored_and_absent_from_public_schema(legacy: int) -> None:
    budget = Budget.model_validate({"tokens": legacy, "cost_usd": 1, "compute_s": 30})
    assert not hasattr(budget, "tokens")
    assert "tokens" not in budget.model_dump()
    assert "tokens" not in Budget.model_json_schema()["properties"]


def test_authentic_high_token_measurement_does_not_revoke_license(tmp_path: Path) -> None:
    from architect._authority import _sign_report

    artifact = make_artifact(tmp_path)
    report = passing_report(artifact).model_copy(update={"tokens": 10000000})
    report = report.model_copy(
        update={
            "signature": _sign_report(report.model_dump(mode="json", exclude={"signature"})),
        }
    )
    assert artifact.model_copy(update={"report": report}).licensed


@pytest.mark.parametrize("value", [None, [], "not-a-budget", 1, True])
def test_nonmapping_budget_input_is_rejected(value: object) -> None:
    with pytest.raises(ValidationError):
        Budget.model_validate(value)
