"""Independent attacks on rewrites, restricted execution, reuse, and installation."""

from __future__ import annotations

import ast
import asyncio
import difflib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import JsonValue, ValidationError

from architect.compiler import Compiler
from architect.compiler.rewriting import RewriteProposal, rewrite
from architect.compiler.types import CandidateEvaluation, CompileConfig
from architect.compiler.verification import verify
from architect.floors import EvaluationCase, HeldOutTasks
from architect.library import HarnessLibrary
from architect.models import FakeAdapter, ModelRequest, ModelResponse, UsageQuote
from architect.presets import get_preset
from architect.registry import Registry, RegistryError
from architect.runtime import ArtifactExpiredError, BudgetExceeded, Runtime
from architect.runtime.evaluation import evaluate_cases
from architect.runtime.sandbox import SandboxError, sandbox_harness, validate_source
from architect.spec import Budget, FloorDefinition, FloorKind, HarnessArtifact, Lifecycle, Spec
from architect.telemetry import TelemetryRow, TelemetryStore, categorical


@pytest.fixture(autouse=True)
def isolated_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCHITECT_SIGNING_KEY_FILE", str(tmp_path / "signing.key"))


def make_spec(*, lifecycle: Lifecycle = Lifecycle.EXPIRE, budget: Budget | None = None) -> Spec:
    return Spec(
        task="Produce a generic response",
        input_schema={"type": "object"},
        output_schema={"type": "string"},
        lifecycle=lifecycle,
        floor=FloorDefinition(
            kind=FloorKind.HELD_OUT_TASKS, limit=0.5, dataset="private-dataset", minimum_samples=2
        ),
        budget=budget or Budget(cost_usd=1, compute_s=10),
    )


def cases(*, prefix: str = "case", expected: str = "ok") -> list[EvaluationCase]:
    return [
        EvaluationCase(
            case_id=f"{prefix}-{index:03d}",
            task={"index": index, "payload": f"private-{prefix}-{index}"},
            expected=expected,
            metadata={"secret": "private-label-metadata"},
        )
        for index in range(8)
    ]


class AuditedFloor(HeldOutTasks):
    def __init__(self, definition: FloorDefinition, records: Sequence[EvaluationCase]) -> None:
        super().__init__(definition, records)
        self.grants = 0

    def _verification_cases(self, capability: object) -> tuple[EvaluationCase, ...]:
        self.grants += 1
        return super()._verification_cases(capability)


class Factory:
    def __init__(self, *, cost: float = 0.02, text: str = "ok") -> None:
        self.cost = cost
        self.text = text
        self.adapters: list[FakeAdapter] = []

    def __call__(self, model: str) -> FakeAdapter:
        assert model == "fake"
        response = ModelResponse(
            text=self.text, tokens_in=2, tokens_out=1, cost_usd=self.cost, model="fake"
        )
        adapter = FakeAdapter(responses=[response] * 100)
        self.adapters.append(adapter)
        return adapter

    @property
    def calls(self) -> int:
        return sum(adapter.calls for adapter in self.adapters)


ONE_CALL = "async def run(context, task):\n    return await context.call('private-prompt')\n"
TWO_CALLS = (
    "async def run(context, task):\n"
    "    draft = await context.call('private-prompt')\n"
    "    return await context.call(draft)\n"
)


def artifact_at(
    root: Path,
    *,
    version: int = 1,
    spec: Spec | None = None,
    code: str = ONE_CALL,
    sandbox: bool = True,
    components: tuple[str, ...] = ("synthesis",),
) -> HarnessArtifact:
    spec = spec or make_spec()
    directory = root / "artifacts" / spec.spec_hash / str(version)
    directory.mkdir(parents=True)
    (directory / "harness.py").write_text(code, encoding="utf-8")
    artifact = HarnessArtifact(
        spec=spec,
        version=version,
        preset="synthesizer",
        model="fake",
        components=components,
        params={"context_window": 512, "terseness": 0, "verifier_depth": 0},
        directory=directory,
        execution_mode="sandbox" if sandbox else "native",
    )
    artifact.save()
    return artifact


async def authenticated(
    root: Path,
    *,
    version: int = 1,
    quality: float = 1,
    cost: float = 0.02,
    lifecycle: Lifecycle = Lifecycle.INSTALL,
    budget: Budget | None = None,
) -> HarnessArtifact:
    wrong = tuple([1, 3, 5, 7][: round((1 - quality) * 4)])
    code = (
        "async def run(context, task):\n"
        "    answer = await context.call('private-prompt')\n"
        f"    if task['index'] in {wrong!r}:\n        return 'wrong'\n"
        "    return answer\n"
    )
    artifact = artifact_at(
        root,
        version=version,
        spec=make_spec(lifecycle=lifecycle, budget=budget),
        code=code,
        sandbox=False,
    )
    with TelemetryStore(root / "verification-telemetry") as store:
        artifact = await verify(
            artifact, AuditedFloor(artifact.spec.floor, cases()), Factory(cost=cost), Runtime(store)
        )
    assert artifact.licensed
    return artifact


UNSAFE_SOURCES = [
    "import os\nasync def run(context, task):\n    return os.environ\n",
    "from pathlib import Path\nasync def run(context, task):\n    return Path('.').read_text()\n",
    "async def run(context, task):\n    return task.__class__.__mro__\n",
    "async def run(context, task):\n    return getattr(task, '__class__')\n",
    "async def run(context, task):\n    return globals()\n",
    "async def run(context, task):\n    return __builtins__\n",
    "async def run(context, task):\n    return __import__('os').environ\n",
    "async def run(context, task):\n    return eval(task['payload'])\n",
    "async def run(context, task):\n    exec(task['payload'])\n    return 'ok'\n",
    "async def run(context, task):\n    return open('signing.key').read()\n",
    "async def run(context, task):\n    return context.adapter.complete(task)\n",
    "async def run(context, task):\n    method = context.call\n    return await method('x')\n",
    "async def run(context, task):\n    return await context.call(**task)\n",
    "async def run(context, task):\n    return [x for x in task]\n",
    "async def run(context, task):\n    while True:\n        pass\n",
    "async def run(context, task):\n    for x in task:\n        pass\n    return 'ok'\n",
    "async def run(context, task=__import__('os')):\n    return task\n",
    "async def run(context, task) -> __import__('os'):\n    return task\n",
    "@__import__('os').system\nasync def run(context, task):\n    return task\n",
    "class Escape:\n    pass\nasync def run(context, task):\n    return Escape()\n",
    "async def run(context, task):\n    async def hidden():\n"
    "        return task\n    return await hidden()\n",
    "async def run(context, task):\n    return (lambda: task)()\n",
    "from architect.runtime import RunContext as __builtins__\n"
    "async def run(context, task):\n    return task\n",
    "from architect.floors._access import _VERIFIER_CAPABILITY\n"
    "async def run(context, task):\n    return 'ok'\n",
    "from architect._authority import _sign_report\n"
    "async def run(context, task):\n    return 'ok'\n",
    "from architect.presets import execute_preset\n"
    "async def run(context, task):\n    return execute_preset.__globals__\n",
]


@pytest.mark.parametrize("source", UNSAFE_SOURCES)
def test_sandbox_rejects_capability_escapes_before_execution(tmp_path: Path, source: str) -> None:
    artifact = artifact_at(tmp_path)
    with pytest.raises(SandboxError):
        validate_source(source, artifact)


@pytest.mark.parametrize("oversize", ["source", "nodes", "unicode-bytes"])
def test_sandbox_resource_bounds_apply_before_execution(tmp_path: Path, oversize: str) -> None:
    artifact = artifact_at(tmp_path)
    if oversize == "source":
        source = "#" + "x" * 65536 + "\n" + ONE_CALL
    elif oversize == "unicode-bytes":
        source = "#" + "界" * 23000 + "\n" + ONE_CALL
    else:
        source = "async def run(context, task):\n    return [" + ",".join(["0"] * 2100) + "]\n"
    with pytest.raises(SandboxError):
        validate_source(source, artifact)


async def test_sandbox_treats_executable_looking_task_as_data(tmp_path: Path) -> None:
    artifact = artifact_at(tmp_path, code="async def run(context, task):\n    return task\n")
    factory = Factory()
    marker = tmp_path / "escaped"
    task: JsonValue = {"payload": f"__import__('pathlib').Path({str(marker)!r}).touch()"}
    with TelemetryStore(tmp_path / "telemetry") as store:
        result = await Runtime(store).evaluate(
            artifact.spec, sandbox_harness(artifact), task, factory("fake")
        )
        assert result.output == task
        assert len(store.rows()) == 1
    assert factory.calls == 0
    assert not marker.exists()


async def test_sandbox_rpc_uses_trusted_preflight_budget(tmp_path: Path) -> None:
    spec = make_spec(budget=Budget(cost_usd=0.01, compute_s=10))
    artifact = artifact_at(tmp_path, spec=spec)
    factory = Factory()
    with TelemetryStore(tmp_path / "telemetry") as store:
        with pytest.raises(BudgetExceeded):
            await Runtime(store).evaluate(spec, sandbox_harness(artifact), {}, factory("fake"))
        assert len(store.rows()) == 1
        assert not store.rows()[0].accepted
    assert factory.calls == 0


async def test_sandbox_enforces_maximum_model_rpc_count(tmp_path: Path) -> None:
    spec = make_spec(budget=Budget(cost_usd=10, compute_s=20))
    source = (
        "async def run(context, task):\n"
        + ("    await context.call('x')\n" * 65)
        + "    return 'ok'\n"
    )
    artifact = artifact_at(tmp_path, spec=spec, code=source)
    factory = Factory(cost=0.001)
    try:
        validate_source(source, artifact)
    except SandboxError:
        assert factory.calls == 0
        return
    with TelemetryStore(tmp_path / "telemetry") as store:
        with pytest.raises((SandboxError, BudgetExceeded)):
            await Runtime(store).evaluate(spec, sandbox_harness(artifact), {}, factory("fake"))
    assert factory.calls <= 64


def unified(
    old: str, new: str, *, before: str = "a/harness.py", after: str = "b/harness.py"
) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=before,
            tofile=after,
        )
    )


def proposal(diff: str, components: tuple[str, ...] = ("synthesis",)) -> ModelResponse:
    return ModelResponse(
        text=json.dumps({"diff": diff, "components": components}),
        tokens_in=2,
        tokens_out=2,
        cost_usd=0.001,
        model="fake",
    )


async def baseline(
    tmp_path: Path,
    *,
    factory: Factory | None = None,
) -> tuple[CandidateEvaluation, AuditedFloor, AuditedFloor, Runtime, Factory]:
    factory = factory or Factory()
    artifact = artifact_at(tmp_path, code=TWO_CALLS, components=("synthesis", "review"))
    floor = AuditedFloor(artifact.spec.floor, cases())
    smoke = AuditedFloor(artifact.spec.floor, cases(prefix="smoke"))
    runtime = Runtime(TelemetryStore(tmp_path / "telemetry"))
    measurement = await evaluate_cases(artifact, floor, floor.tuning, factory, runtime)
    assert measurement.floor_passed and measurement.budget_passed
    return (
        CandidateEvaluation(artifact=artifact, measurement=measurement),
        floor,
        smoke,
        runtime,
        factory,
    )


@pytest.mark.parametrize("cap", [0, 1, 3])
async def test_rewrite_cap_counts_malformed_proposals_and_zero_disables(
    tmp_path: Path, cap: int
) -> None:
    candidate, floor, smoke, runtime, factory = await baseline(tmp_path)
    bad = ModelResponse(
        text="private-invalid-proposal", tokens_in=1, tokens_out=1, cost_usd=0.001, model="fake"
    )
    proposer = FakeAdapter(responses=[bad] * 10)
    result = await rewrite(
        candidate,
        get_preset("synthesizer"),
        floor,
        smoke,
        factory,
        runtime,
        tmp_path,
        proposer,
        max_iterations=cap,
    )
    assert result.attempts == cap
    assert proposer.calls == cap
    assert result.accepted == 0
    assert result.best.artifact.artifact_digest == candidate.artifact.artifact_digest
    assert floor.grants == smoke.grants == 0
    rows = [row for row in runtime.store.rows() if row.task_type == categorical("rewrite")]
    assert len(rows) == cap
    assert all(not row.accepted for row in rows)
    assert "private-invalid-proposal" not in repr(result.failures)


@pytest.mark.parametrize(
    "path", ["../escape.py", "/tmp/escape.py", "b/other.py", "b/../harness.py"]
)
async def test_rewrite_diff_cannot_write_another_path(tmp_path: Path, path: str) -> None:
    candidate, floor, smoke, runtime, factory = await baseline(tmp_path)
    before = candidate.artifact.artifact_digest
    diff = unified(TWO_CALLS, ONE_CALL, after=path)
    proposer = FakeAdapter(responses=[proposal(diff)])
    result = await rewrite(
        candidate,
        get_preset("synthesizer"),
        floor,
        smoke,
        factory,
        runtime,
        tmp_path,
        proposer,
        max_iterations=1,
    )
    assert result.accepted == 0
    assert result.attempts == proposer.calls == 1
    assert candidate.artifact.artifact_digest == before
    assert not (tmp_path / "escape.py").exists()


@pytest.mark.parametrize("attack", ["unsafe", "smoke", "floor", "cost", "components"])
async def test_rewrite_requires_safe_code_floor_smoke_and_strict_saving(
    tmp_path: Path,
    attack: str,
) -> None:
    candidate, floor, smoke, runtime, factory = await baseline(tmp_path)
    source = ONE_CALL
    components = ("synthesis",)
    if attack == "unsafe":
        source = "import os\n" + ONE_CALL
    elif attack == "smoke":
        smoke = AuditedFloor(
            candidate.artifact.spec.floor, cases(prefix="smoke", expected="different")
        )
    elif attack == "floor":
        source = "async def run(context, task):\n    return 'wrong'\n"
    elif attack == "cost":
        source = TWO_CALLS + "# equal usage is not an improvement\n"
    else:
        components = ("review",)
    proposer = FakeAdapter(responses=[proposal(unified(TWO_CALLS, source), components)])
    result = await rewrite(
        candidate,
        get_preset("synthesizer"),
        floor,
        smoke,
        factory,
        runtime,
        tmp_path,
        proposer,
        max_iterations=1,
    )
    assert result.accepted == 0
    assert result.best.artifact.artifact_digest == candidate.artifact.artifact_digest
    assert floor.grants == smoke.grants == 0
    assert result.failures


async def test_accepted_rewrite_is_cheaper_sandboxed_unlicensed_and_not_heldout(
    tmp_path: Path,
) -> None:
    candidate, floor, smoke, runtime, factory = await baseline(tmp_path)
    original_source = (candidate.artifact.directory / "harness.py").read_text()
    proposer = FakeAdapter(responses=[proposal(unified(TWO_CALLS, ONE_CALL))])
    result = await rewrite(
        candidate,
        get_preset("synthesizer"),
        floor,
        smoke,
        factory,
        runtime,
        tmp_path,
        proposer,
        max_iterations=3,
    )
    assert result.attempts == result.accepted == proposer.calls == 1
    assert result.best.measurement.floor_passed and result.best.measurement.budget_passed
    assert result.best.measurement.mean_cost_usd < candidate.measurement.mean_cost_usd
    assert result.best.artifact.execution_mode == "sandbox"
    assert result.best.artifact.report is None
    assert not result.best.artifact.licensed
    assert result.best.artifact.directory != candidate.artifact.directory
    assert (candidate.artifact.directory / "harness.py").read_text() == original_source
    assert (result.best.artifact.directory / "rewrite.diff").is_file()
    assert floor.grants == smoke.grants == 0
    rows = [row for row in runtime.store.rows() if row.task_type == categorical("rewrite")]
    assert len(rows) == 1 and rows[0].accepted
    assert rows[0].cost_usd == rows[0].tokens_in == rows[0].tokens_out == 0


def test_proposal_rejects_oversized_diff_and_unknown_authority() -> None:
    with pytest.raises(ValidationError):
        RewriteProposal(diff="x" * 65537, components=("synthesis",))
    with pytest.raises(ValidationError):
        RewriteProposal.model_validate({"diff": "x", "components": ["synthesis"], "licensed": True})


async def test_registry_rejects_worse_quality_even_when_cheaper(tmp_path: Path) -> None:
    incumbent = await authenticated(tmp_path / "incumbent", quality=0.75, cost=0.04)
    worse = await authenticated(tmp_path / "worse", version=2, quality=0.5, cost=0.001)
    registry = Registry(tmp_path / "registry")
    first = registry.install("agent", incumbent)
    with pytest.raises(RegistryError):
        registry.install("agent", worse)
    assert registry.status("agent") == first
    assert registry.get("agent").report == incumbent.report


async def test_registry_exact_tie_cannot_replace_incumbent(tmp_path: Path) -> None:
    incumbent = await authenticated(tmp_path / "incumbent")
    tie = await authenticated(tmp_path / "tie", version=2)
    registry = Registry(tmp_path / "registry")
    first = registry.install("agent", incumbent)
    with pytest.raises(RegistryError):
        registry.install("agent", tie)
    assert registry.status("agent") == first


@pytest.mark.parametrize("invalid", ["expire", "forged", "code"])
async def test_registry_accepts_only_current_authenticated_installed_artifacts(
    tmp_path: Path,
    invalid: str,
) -> None:
    artifact = await authenticated(
        tmp_path / "candidate",
        lifecycle=(Lifecycle.EXPIRE if invalid == "expire" else Lifecycle.INSTALL),
    )
    if invalid == "forged":
        assert artifact.report is not None
        artifact = artifact.model_copy(
            update={
                "report": artifact.report.model_copy(
                    update={
                        "signature": "model-approved",
                    }
                )
            }
        )
    elif invalid == "code":
        (artifact.directory / "harness.py").write_text("# changed\n", encoding="utf-8")
    registry = Registry(tmp_path / "registry")
    with pytest.raises(RegistryError):
        registry.install("agent", artifact)
    assert registry.list() == ()


async def test_registry_snapshot_survives_source_changes_and_rollback_is_persistent(
    tmp_path: Path,
) -> None:
    first = await authenticated(tmp_path / "first", quality=0.75)
    second = await authenticated(tmp_path / "second", version=2, quality=1)
    registry_root = tmp_path / "registry"
    registry = Registry(registry_root)
    old = registry.install("agent", first)
    newest = registry.install("agent", second)
    assert newest.revision > old.revision
    (first.directory / "harness.py").write_text("# original removed\n", encoding="utf-8")
    (second.directory / "harness.py").write_text("# original removed\n", encoding="utf-8")
    assert registry.get("agent").licensed
    rolled_back = registry.rollback("agent", old.revision)
    assert rolled_back.revision == old.revision
    restored = Registry(registry_root)
    assert restored.get("agent").licensed
    assert restored.get("agent").report == first.report


async def test_registry_retrieval_and_rollback_recheck_owned_code(tmp_path: Path) -> None:
    artifact = await authenticated(tmp_path / "source")
    registry = Registry(tmp_path / "registry")
    entry = registry.install("agent", artifact)
    owned = registry.get("agent")
    (owned.directory / "harness.py").write_text("# corrupt owned copy\n", encoding="utf-8")
    with pytest.raises(RegistryError):
        registry.get("agent")
    with pytest.raises(RegistryError):
        registry.rollback("agent", entry.revision)


async def test_library_template_isolated_and_expire_copy_not_deliverable(tmp_path: Path) -> None:
    artifact = await authenticated(tmp_path / "source", lifecycle=Lifecycle.EXPIRE)
    library = HarnessLibrary(tmp_path / "library")
    library.store(artifact)
    template = library.lookup(artifact.spec, "fake")
    assert template is not None and template.licensed
    assert template.directory != artifact.directory
    assert (template.directory / ".consumed").exists()
    assert not (artifact.directory / ".consumed").exists()
    with TelemetryStore(tmp_path / "delivery-telemetry") as store:
        with pytest.raises(ArtifactExpiredError):
            await Runtime(store).run(template, {"index": 0}, Factory()("fake"))
    (artifact.directory / "harness.py").write_text("# source corrupted\n", encoding="utf-8")
    assert library.lookup(template.spec, "fake") is not None


async def test_library_cumulative_two_verification_failures_evict_across_restart(
    tmp_path: Path,
) -> None:
    artifact = await authenticated(tmp_path / "source")
    root = tmp_path / "library"
    library = HarnessLibrary(root, promotion_reuses=2)
    key = library.store(artifact)
    template = library.lookup(artifact.spec, "fake")
    assert template is not None
    library.record_verification(template, False)
    library.record_verification(template, True)
    restarted = HarnessLibrary(root, promotion_reuses=2)
    assert restarted.lookup(artifact.spec, "fake") is not None
    restarted.record_verification(template, False)
    assert restarted.lookup(artifact.spec, "fake") is None
    assert key not in restarted.install_candidates()
    restarted.record_verification(template, False)  # Missing source is harmless.


async def test_library_corrupt_snapshot_is_evicted_instead_of_reused(tmp_path: Path) -> None:
    artifact = await authenticated(tmp_path / "source")
    library = HarnessLibrary(tmp_path / "library")
    library.store(artifact)
    template = library.lookup(artifact.spec, "fake")
    assert template is not None
    (template.directory / "harness.py").write_text("# forged code\n", encoding="utf-8")
    assert library.lookup(artifact.spec, "fake") is None


async def test_real_library_reuse_still_creates_fresh_stage_four_report(tmp_path: Path) -> None:
    spec = make_spec()
    floor = AuditedFloor(spec.floor, cases())
    library = HarnessLibrary(tmp_path / "library", promotion_reuses=1)
    compiler = Compiler(
        tmp_path / "compiled",
        Runtime(TelemetryStore(tmp_path / "telemetry")),
        Factory(),
        lambda _: floor,
        library=library,
        config=CompileConfig(max_candidates=1, enable_rewriting=False),
    )
    first = await compiler.compile(spec)
    assert first.artifact is not None and first.artifact.licensed and first.report is not None
    second = await compiler.compile(spec)
    assert second.artifact is not None and second.artifact.licensed and second.report is not None
    assert second.warm_started
    assert floor.grants == 2
    assert second.report.verification_id != first.report.verification_id
    assert second.artifact.directory != first.artifact.directory
    assert 4 in second.stages
    assert library.install_candidates()


class BlockingAdapter:
    model = "fake"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False
        self.calls = 0

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=2, tokens_out=1, cost_usd=0.001)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unset event completed")


async def test_sandbox_cancellation_reaps_worker_and_hides_parent_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRIVATE_SANDBOX_SECRET", "private-parent-secret")
    original = asyncio.create_subprocess_exec
    spawned: list[asyncio.subprocess.Process] = []
    environments: list[Any] = []

    async def capture(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        environments.append(kwargs.get("env"))
        process = await original(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    artifact = artifact_at(tmp_path)
    adapter = BlockingAdapter()
    store = TelemetryStore(tmp_path / "telemetry")
    job = asyncio.create_task(
        Runtime(store).evaluate(
            artifact.spec,
            sandbox_harness(artifact),
            {},
            adapter,
        )
    )
    try:
        await asyncio.wait_for(adapter.started.wait(), timeout=10)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert adapter.cancelled
        assert spawned and all(process.returncode is not None for process in spawned)
        for environment in environments:
            assert environment is not None
            assert "PRIVATE_SANDBOX_SECRET" not in environment
            assert "ARCHITECT_SIGNING_KEY_FILE" not in environment
            assert "HOME" not in environment
            assert "USERPROFILE" not in environment
        assert len(store.rows()) == 1
        assert not store.rows()[0].accepted
    finally:
        if not job.done():
            job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        for process in spawned:
            if process.returncode is None:
                process.kill()
                await process.wait()


async def test_sandbox_rejects_oversized_protocol_task(tmp_path: Path) -> None:
    artifact = artifact_at(tmp_path)
    factory = Factory()
    with TelemetryStore(tmp_path / "telemetry") as store:
        with pytest.raises(SandboxError):
            await Runtime(store).evaluate(
                artifact.spec, sandbox_harness(artifact), {"payload": "x" * 131073}, factory("fake")
            )
    assert factory.calls == 0


def test_sandbox_wrapper_cannot_substitute_another_spec(tmp_path: Path) -> None:
    artifact = artifact_at(tmp_path)
    altered = artifact.spec.model_copy(update={"task": "different-task"})
    other = tmp_path / "different-wrapper"
    get_preset("synthesizer").emit(other, altered, artifact.params, artifact.components)
    source = (other / "harness.py").read_text()
    with pytest.raises(SandboxError):
        validate_source(source, artifact)


def observed_rows(
    artifact: HarnessArtifact,
    scores: Sequence[float | None],
    *,
    model: str = "fake",
) -> list[TelemetryRow]:
    return [
        TelemetryRow(
            run_id=str(uuid4()),
            timestamp=datetime.now(UTC),
            domain=categorical(artifact.spec.domain),
            task_type=categorical(artifact.preset),
            spec_hash=artifact.spec.spec_hash,
            harness_version=artifact.version,
            lifecycle=artifact.spec.lifecycle,
            preset_origin=categorical(artifact.preset),
            component_set=tuple(categorical(item) for item in artifact.components),
            model=categorical(model),
            model_params={},
            tokens_in=0,
            tokens_out=0,
            cost_usd=0,
            wall_time_s=0,
            floor_type=artifact.spec.floor.kind,
            floor_score=score,
            floor_limit=artifact.spec.floor.limit,
            accepted=score is None or score >= artifact.spec.floor.limit,
        )
        for score in scores
    ]


async def test_drift_recompile_cannot_replace_with_worse_and_duplicate_rows_do_not_repeat(
    tmp_path: Path,
) -> None:
    incumbent = await authenticated(tmp_path / "incumbent", quality=1, cost=0.04)
    worse = await authenticated(tmp_path / "worse", version=2, quality=0.5, cost=0.001)
    registry = Registry(tmp_path / "registry", window=3, margin=0.05)
    entry = registry.install("agent", incumbent)
    rows = observed_rows(incumbent, [0.4, 0.4, 0.4])
    calls = 0

    async def recompile(spec: Spec) -> HarnessArtifact:
        nonlocal calls
        calls += 1
        assert spec.spec_hash == incumbent.spec.spec_hash
        return worse

    outcome = await registry.monitor("agent", rows, recompile)
    assert outcome.event is not None and outcome.event.reason == "floor_drift"
    assert not outcome.accepted and outcome.failure
    assert calls == 1
    assert registry.status("agent") == entry
    repeated = await registry.monitor("agent", rows, recompile)
    assert repeated.event is None
    assert calls == 1


async def test_drift_uses_observed_scores_not_saved_license_score(tmp_path: Path) -> None:
    artifact = await authenticated(tmp_path / "source", quality=0.5)
    registry = Registry(tmp_path / "registry", window=3, margin=0.05)
    registry.install("agent", artifact)
    assert registry.observe("agent", observed_rows(artifact, [None] * 5)) is None
    stale = [
        row.model_copy(update={"harness_version": artifact.version + 1})
        for row in observed_rows(artifact, [0.1] * 5)
    ]
    assert registry.observe("agent", stale) is None
    event = registry.observe("agent", observed_rows(artifact, [0.56, 0.53, 0.51]))
    assert event is not None and event.reason == "floor_drift"


async def test_model_change_triggers_drift_without_recycled_floor_score(tmp_path: Path) -> None:
    artifact = await authenticated(tmp_path / "source")
    registry = Registry(tmp_path / "registry", window=3)
    registry.install("agent", artifact)
    event = registry.observe("agent", observed_rows(artifact, [None], model="changed-model"))
    assert event is not None and event.reason == "model_change"
    assert event.score is None


async def test_library_lookup_does_not_count_unverified_reuse_or_transfer_license(
    tmp_path: Path,
) -> None:
    artifact = await authenticated(tmp_path / "source")
    library = HarnessLibrary(tmp_path / "library", promotion_reuses=1)
    library.store(artifact)
    changed = artifact.spec.model_copy(update={"task": "different-task"})
    for _ in range(4):
        template = library.lookup(changed, "fake")
        assert template is not None and template.licensed
        assert not template.model_copy(update={"spec": changed}).licensed
    assert library.install_candidates() == ()
    incompatible = artifact.spec.model_copy(update={"output_schema": {"type": "integer"}})
    assert library.lookup(incompatible, "fake") is None


@pytest.mark.parametrize("attack", ["new_budget_failure", "zero_consumed_baseline"])
async def test_rewrite_cannot_game_denied_reservations_or_budget_failure(
    tmp_path: Path,
    attack: str,
) -> None:
    spec = make_spec(budget=Budget(cost_usd=0.05, compute_s=10))
    artifact = artifact_at(tmp_path, spec=spec, code=TWO_CALLS, components=("synthesis", "review"))
    floor = AuditedFloor(spec.floor, cases())
    smoke = AuditedFloor(spec.floor, cases(prefix="smoke"))
    runtime = Runtime(TelemetryStore(tmp_path / "telemetry"))
    baseline_factory = Factory(cost=0.1 if attack == "zero_consumed_baseline" else 0.02)
    measured = await evaluate_cases(artifact, floor, floor.tuning, baseline_factory, runtime)
    if attack == "zero_consumed_baseline":
        assert baseline_factory.calls == 0
        assert measured.mean_cost_usd == 0
        assert not measured.budget_passed
        source = ONE_CALL
    else:
        assert measured.floor_passed and measured.budget_passed
        source = (
            "async def run(context, task):\n"
            "    first = await context.call('x')\n"
            "    second = await context.call(first)\n"
            "    return await context.call(second)\n"
        )
    candidate = CandidateEvaluation(artifact=artifact, measurement=measured)
    proposer = FakeAdapter(responses=[proposal(unified(TWO_CALLS, source))])
    result = await rewrite(
        candidate,
        get_preset("synthesizer"),
        floor,
        smoke,
        Factory(),
        runtime,
        tmp_path,
        proposer,
        max_iterations=1,
    )
    assert result.accepted == 0
    assert result.best.artifact.artifact_digest == artifact.artifact_digest
    assert floor.grants == smoke.grants == 0
    assert result.failures


async def test_zero_dollar_rewrite_requires_actual_token_reduction(tmp_path: Path) -> None:
    candidate, floor, smoke, runtime, factory = await baseline(tmp_path, factory=Factory(cost=0))
    assert candidate.measurement.mean_cost_usd == 0
    proposer = FakeAdapter(responses=[proposal(unified(TWO_CALLS, ONE_CALL))])
    result = await rewrite(
        candidate,
        get_preset("synthesizer"),
        floor,
        smoke,
        factory,
        runtime,
        tmp_path,
        proposer,
        max_iterations=1,
    )
    assert result.accepted == 1
    assert result.best.measurement.mean_cost_usd == 0
    assert result.best.measurement.mean_tokens is not None
    assert candidate.measurement.mean_tokens is not None
    assert result.best.measurement.mean_tokens < candidate.measurement.mean_tokens


async def test_evicted_library_entry_cannot_reset_failures_by_storing_again(tmp_path: Path) -> None:
    artifact = await authenticated(tmp_path / "source")
    library = HarnessLibrary(tmp_path / "library")
    key = library.store(artifact)
    source = library.lookup(artifact.spec, "fake")
    assert source is not None
    library.record_verification(source, False)
    library.record_verification(source, False)
    assert library.lookup(artifact.spec, "fake") is None
    assert library.store(artifact) == key
    assert library.lookup(artifact.spec, "fake") is None


async def test_compiler_reuses_reordered_equivalent_tool_sets_without_transferring_license(
    tmp_path: Path,
) -> None:
    first_spec = make_spec().model_copy(update={"tools": ("alpha", "beta")})
    reordered = first_spec.model_copy(update={"tools": ("beta", "alpha")})
    assert first_spec.spec_hash != reordered.spec_hash
    floor = AuditedFloor(first_spec.floor, cases())
    library = HarnessLibrary(tmp_path / "library")
    compiler = Compiler(
        tmp_path / "compiled",
        Runtime(TelemetryStore(tmp_path / "telemetry")),
        Factory(),
        lambda _: floor,
        library=library,
        config=CompileConfig(max_candidates=1, enable_rewriting=False),
    )
    first = await compiler.compile(first_spec)
    assert first.artifact is not None and first.artifact.licensed and first.report is not None
    assert library.lookup(reordered, "fake") is not None
    second = await compiler.compile(reordered)
    assert second.warm_started
    assert second.artifact is not None and second.artifact.licensed and second.report is not None
    assert second.spec.tools == ("beta", "alpha")
    assert second.artifact.spec.spec_hash == reordered.spec_hash
    assert second.report.verification_id != first.report.verification_id
    assert second.artifact.directory != first.artifact.directory
    assert floor.grants == 2


@pytest.mark.parametrize("breached_role", ["primary", "secondary"])
async def test_drift_windows_never_mix_primary_and_secondary_with_same_kind_and_limit(
    tmp_path: Path,
    breached_role: str,
) -> None:
    primary = FloorDefinition(
        kind=FloorKind.HELD_OUT_TASKS, limit=0.8, dataset="primary-data", minimum_samples=2
    )
    secondary = primary.model_copy(update={"dataset": "secondary-data"})
    spec = make_spec(lifecycle=Lifecycle.INSTALL).model_copy(
        update={
            "floor": primary,
            "secondary_floor": secondary,
        }
    )
    artifact = artifact_at(tmp_path / "source", spec=spec, sandbox=False)
    with TelemetryStore(tmp_path / "verification-telemetry") as store:
        artifact = await verify(
            artifact,
            AuditedFloor(primary, cases()),
            Factory(),
            Runtime(store),
            secondary_floor=AuditedFloor(secondary, cases(prefix="secondary")),
        )
    assert artifact.licensed
    registry_root = tmp_path / "registry"
    registry = Registry(registry_root, window=3)
    registry.install("agent", artifact)
    healthy = observed_rows(artifact, [1.0] * 3)
    healthy_secondary = [
        row.model_copy(update={"floor_role": "secondary"})
        for row in observed_rows(artifact, [1.0] * 3)
    ]
    assert registry.observe("agent", [*healthy, *healthy_secondary]) is None
    # Reopen to also exercise persistence of role-separated windows.
    registry = Registry(registry_root, window=3)
    primary_rows = observed_rows(artifact, [0.7 if breached_role == "primary" else 1.0] * 3)
    secondary_rows = [
        row.model_copy(update={"floor_role": "secondary"})
        for row in observed_rows(
            artifact,
            [0.7 if breached_role == "secondary" else 1.0] * 3,
        )
    ]
    mixed = [row for pair in zip(primary_rows, secondary_rows, strict=True) for row in pair]
    event = registry.observe("agent", mixed)
    assert event is not None and event.reason == "floor_drift"
    assert event.score == pytest.approx(0.7)
    assert registry.observe("agent", mixed) is None


@pytest.mark.parametrize("resource", ["cost_usd", "compute_s"])
async def test_registry_budget_ceiling_increase_requires_explicit_manual_allowance(
    tmp_path: Path,
    resource: str,
) -> None:
    incumbent = await authenticated(tmp_path / "old", quality=0.75)
    expanded = incumbent.spec.budget.model_copy(
        update={
            resource: getattr(incumbent.spec.budget, resource) * 2,
        }
    )
    better = await authenticated(tmp_path / "new", version=2, quality=1, budget=expanded)
    assert better.licensed
    registry = Registry(tmp_path / "registry")
    old = registry.install("agent", incumbent)
    with pytest.raises(RegistryError):
        registry.install("agent", better)
    assert registry.status("agent") == old
    installed = registry.install("agent", better, allow_budget_increase=True)
    assert installed.revision > old.revision
    assert registry.get("agent").report == better.report


async def test_manual_budget_allowance_cannot_override_worse_quality(tmp_path: Path) -> None:
    incumbent = await authenticated(tmp_path / "old", quality=1)
    expanded = incumbent.spec.budget.model_copy(update={"cost_usd": 2})
    worse = await authenticated(tmp_path / "new", version=2, quality=0.5, budget=expanded)
    registry = Registry(tmp_path / "registry")
    old = registry.install("agent", incumbent)
    with pytest.raises(RegistryError):
        registry.install("agent", worse, allow_budget_increase=True)
    assert registry.status("agent") == old


async def test_automatic_drift_recompile_never_grants_larger_budget(tmp_path: Path) -> None:
    incumbent = await authenticated(tmp_path / "old", quality=0.75)
    expanded = incumbent.spec.budget.model_copy(update={"cost_usd": 2})
    better = await authenticated(tmp_path / "new", version=2, quality=1, budget=expanded)
    registry = Registry(tmp_path / "registry", window=3)
    old = registry.install("agent", incumbent)
    calls = 0

    async def recompile(spec: Spec) -> HarnessArtifact:
        nonlocal calls
        calls += 1
        return better

    result = await registry.monitor("agent", observed_rows(incumbent, [0.4] * 3), recompile)
    assert result.event is not None
    assert not result.accepted and result.failure
    assert calls == 1
    assert registry.status("agent") == old


async def test_pre_activation_scores_and_model_changes_cannot_seed_live_drift(
    tmp_path: Path,
) -> None:
    artifact = await authenticated(tmp_path / "source")
    compile_rows = observed_rows(artifact, [0.1] * 3)
    old_model_rows = observed_rows(artifact, [None], model="old-compile-model")
    registry_root = tmp_path / "registry"
    registry = Registry(registry_root, window=3)
    registry.install("agent", artifact)
    assert registry.observe("agent", [*compile_rows, *old_model_rows]) is None
    registry = Registry(registry_root, window=3)
    assert registry.observe("agent", [*compile_rows, *old_model_rows]) is None
    # A single healthy live score must not inherit low scores from compilation.
    assert registry.observe("agent", observed_rows(artifact, [0.6])) is None
    event = registry.observe("agent", observed_rows(artifact, [0.4] * 3))
    assert event is not None and event.score == pytest.approx(0.4)


async def test_rollback_resets_derived_window_and_old_activation_rows_stay_ignored(
    tmp_path: Path,
) -> None:
    first = await authenticated(tmp_path / "first", quality=0.75)
    second = await authenticated(tmp_path / "second", version=2, quality=1)
    registry_root = tmp_path / "registry"
    registry = Registry(registry_root, window=3)
    old = registry.install("agent", first)
    old_rows = observed_rows(first, [0.4] * 3)
    assert registry.observe("agent", old_rows) is not None
    registry.install("agent", second)
    registry.rollback("agent", old.revision)
    registry = Registry(registry_root, window=3)
    assert registry.observe("agent", old_rows) is None
    assert registry.observe("agent", observed_rows(first, [0.6])) is None
    event = registry.observe("agent", observed_rows(first, [0.4] * 3))
    assert event is not None and event.score == pytest.approx(0.4)


def test_parser_memory_exhaustion_becomes_safe_sandbox_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = artifact_at(tmp_path)

    def exhausted(*args: Any, **kwargs: Any) -> Any:
        raise MemoryError("private-parser-detail")

    monkeypatch.setattr(ast, "parse", exhausted)
    with pytest.raises(SandboxError) as caught:
        validate_source(ONE_CALL, artifact)
    assert "private-parser-detail" not in str(caught.value)
