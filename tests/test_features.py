"""Offline fixtures, not live-model evidence: real compiler, runtime and upstream Pi.

Only model responses are deterministic. The happy path does not replace compiler,
verification, registry, orchestration, runtime, Pi, or Pi filesystem tools.
"""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from architect.backends.pi import PiBackend
from architect.compiler import CompileConfig, Compiler
from architect.floors import EvaluationCase, HeldOutTasks
from architect.models import FakeAdapter, ModelRequest, ModelResponse, UsageQuote
from architect.orchestrator import OrchestrationResult, Orchestrator, OrchestratorConfig, _nested
from architect.orchestrator.features import FeatureBuilder
from architect.profiles import AdapterConfig, Profile, Services
from architect.registry import Registry
from architect.runtime import Runtime
from architect.spec import Budget, FloorDefinition, Spec, TestReport
from architect.telemetry import TelemetryStore

REQUEST = "Create an installed summary feature"


def response(text: str, model: str = "fake") -> ModelResponse:
    return ModelResponse(text=text, model=model, tokens_in=10, tokens_out=10, cost_usd=0)


def tool(name: str, **arguments: object) -> str:
    return json.dumps({"tool_calls": [{"id": name, "name": name, "arguments": arguments}]})


def specification(*, installed: bool = False) -> Spec:
    return Spec(
        task=REQUEST if installed else "Build a feature integration",
        input_schema={"type": "object"}, output_schema={"type": "string"},
        floor=FloorDefinition(
            kind="held_out_tasks", limit=1, dataset="feature" if installed else "builder",
        ),
        budget=Budget(cost_usd=1, compute_s=120),
        model_target="llama3.2" if installed else "fake",
        lifecycle="install" if installed else "expire",
        domain="synthesizer" if installed else "coder",
        tools=() if installed else ("read", "write", "edit", "compile_agent"),
    )


class OfflineBuilder(FakeAdapter):
    """Scripted model fixture; actual Pi executes every requested tool."""

    def __init__(self, log: list[dict]) -> None:
        super().__init__()
        self.log = log
        self.delivery = False
        self.feature_name = ""

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=10, tokens_out=10, cost_usd=0)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        assert request.max_output_tokens is None
        prompt = json.loads(request.prompt)
        context = prompt["pi_context"]
        self.log.append(context)
        if self.calls == 0:
            # The granted tools are listed once, at the top of the prompt.
            self.delivery = any(t["name"] == "compile_agent" for t in prompt["tools"])
            text = tool("compile_agent", request=REQUEST)
        elif not self.delivery:
            text = "ok"
        elif self.calls == 1:
            results = [m for m in context["messages"] if m["role"] == "toolResult"]
            raw = results[-1]["content"][0]["text"]
            # The bound coder context policy may thin lengthy proposal metadata.
            payload = {"approval_required": True} if raw.startswith(
                '{"approval_required":true'
            ) else json.loads(raw)
            if payload.get("approval_required") or not payload.get("licensed"):
                text = "ok"
            else:
                self.feature_name = payload["name"]
                content = (
                    "FEATURE_ENABLED = True\nHUMAN_REVIEW_REQUIRED = True\n"
                    f"FEATURE_NAME = {self.feature_name!r}\n"
                    "def feature_name():\n"
                    "    return FEATURE_NAME if FEATURE_ENABLED else None\n"
                )
                text = tool("write", path="feature.py", content=content)
        elif self.calls == 2:
            text = tool("edit", path="feature.py", edits=[{
                "oldText": "FEATURE_ENABLED = True", "newText": "FEATURE_ENABLED = False",
            }])
        elif self.calls == 3:
            text = tool("read", path="feature.py")
        else:
            text = "ok"
        self.calls += 1
        return response(text)


class OfflineSetup:
    def __init__(self, path: Path, *, max_depth: int = 2, child_cost: float = 0) -> None:
        self.repo = path / "delivery-checkout"
        self.repo.mkdir()
        self.evaluation_repo = path / "disposable-evaluation-checkout"
        self.evaluation_repo.mkdir()
        self.log: list[dict] = []
        self.floor_calls: list[str] = []
        self.adapters: list[FakeAdapter] = []

        def adapters(model: str) -> FakeAdapter:
            adapter = OfflineBuilder(self.log) if model == "fake" else FakeAdapter(
                model=model, responses=[response("ok", model).model_copy(
                    update={"cost_usd": child_cost},
                )] * 16,
            )
            self.adapters.append(adapter)
            return adapter

        def floors(definition: FloorDefinition) -> HeldOutTasks:
            self.floor_calls.append(definition.dataset)
            return HeldOutTasks(definition, [
                EvaluationCase(case_id=f"offline-{i}",
                               task={"repo": str(self.evaluation_repo), "case": i}, expected="ok")
                for i in range(4)
            ])

        builder = specification()
        child = specification(installed=True)
        # First invocation: builder inference, then installed proposal. Subsequent
        # invocations infer only builders: exact approved child Specs bypass Stage 0.
        self.inference = FakeAdapter(responses=[response(s.model_dump_json(exclude={"provenance"}))
                                               for s in (builder, child, builder, builder)])
        plan = json.dumps({"tasks": [{"id": "builder", "request": "Build feature",
                                    "input": {}, "domain": "coder", "model": "fake",
                                    "budget": {"cost_usd": 1, "compute_s": 120},
                                    "tools": list(builder.tools)}]})
        decomposition = FakeAdapter(responses=[response(plan)] * 3)
        runtime = Runtime(TelemetryStore(path / "telemetry"), adapter_factory=adapters,
                          pi_backend=PiBackend())
        config = CompileConfig(max_candidates=1, enable_rewriting=False,
                               compile_budget=Budget(cost_usd=10, compute_s=300))
        compiler = Compiler(path / "artifacts", runtime, adapters, floors,
                            config=config, inference_adapter=self.inference)
        profile = Profile(models={"fake": AdapterConfig(provider="fake"),
                                  "llama3.2": AdapterConfig(provider="fake")},
                          builder_floor=builder.floor, builder_model="fake", compile=config)
        orchestrator = Orchestrator(compiler, decomposition,
                                   config=OrchestratorConfig(max_depth=max_depth))
        self.services = Services(compiler=compiler, orchestrator=orchestrator,
                                 registry=Registry(path / "registry"), runtime=runtime,
                                 profile=profile, workspace=path, adapter_factory=adapters)


def read_report(directory: Path) -> TestReport:
    return TestReport.model_validate_json((directory / "report.json").read_text())


async def test_offline_two_layer_checkpoint_exact_approval_and_real_pi_integration(
    tmp_path: Path,
) -> None:
    setup = OfflineSetup(tmp_path)
    services = setup.services
    builder = FeatureBuilder(services)
    try:
        proposed = await builder.create(REQUEST, setup.repo)
        assert not proposed.accepted and proposed.failures == ("approval_required",)
        parent, factory = proposed.tasks
        assert parent.accepted and not factory.accepted
        assert isinstance(factory.output, dict) and factory.output["approval_required"] is True
        proposal_path = Path(factory.output["proposal_path"])
        approved = Spec.model_validate_json(await asyncio.to_thread(proposal_path.read_text))
        assert proposal_path == tmp_path / "proposals" / (approved.spec_hash + ".json")
        assert approved.lifecycle == "install" and factory.spec_hash == approved.spec_hash
        assert setup.floor_calls == ["builder"]  # No child tuning/held-out evaluation yet.
        assert services.registry.list() == ()
        assert not (setup.repo / "feature.py").exists()
        assert setup.inference.calls == 2
        assert parent.artifact_directory is not None
        parent_report = read_report(parent.artifact_directory)
        assert parent_report.passed and parent_report.held_out_count == 2
        assert (parent.artifact_directory / ".consumed").is_file()
        assert not (parent.artifact_directory / "harness.py").exists()

        # Explicitly reviewed alteration must be compiled exactly, not reinferred.
        approved = approved.model_copy(update={"task": "Reviewed summary feature"})
        delivered = await builder.create(REQUEST, setup.repo, approved_spec=approved)
        assert delivered.accepted and delivered.failures == ()
        factory = delivered.tasks[-1]
        assert factory.accepted and factory.spec_hash == approved.spec_hash
        assert factory.output["human_review_required"] is True
        assert factory.output["feature_flag_required"] is True
        installed = services.registry.get(factory.output["name"])
        assert installed.licensed and installed.spec == approved
        assert installed.report is not None and installed.report.held_out_count == 2
        assert installed.report.verification_id != parent_report.verification_id
        assert setup.inference.calls == 3  # No extra child inference after explicit approval.
        assert setup.floor_calls == ["builder", "builder", "feature"]
        executed = await services.runtime.run(
            installed, {}, services.adapter_factory(installed.model),
        )
        assert executed.output == "ok"

        # Real upstream write/edit/read, checked independently by the native interpreter.
        check = await asyncio.create_subprocess_exec(
            sys.executable, "-c",
            "import feature; assert feature.FEATURE_ENABLED is False; "
            "assert feature.HUMAN_REVIEW_REQUIRED is True; "
            "assert feature.feature_name() is None; "
            f"assert feature.FEATURE_NAME == {factory.output['name']!r}",
            cwd=setup.repo, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await check.communicate()
        assert check.returncode == 0, (stdout, stderr)
        assert not (setup.evaluation_repo / "feature.py").exists()
        evaluations = [
            a for a in setup.adapters if isinstance(a, OfflineBuilder) and not a.delivery
        ]
        assert len(evaluations) >= 8 and all(a.calls == 2 for a in evaluations)
        # Evaluation models explicitly attempted compile_agent, but received no capability.
        assert sum(isinstance(a, OfflineBuilder) and a.delivery for a in setup.adapters) == 2

        # A warm child compile still receives a fresh Stage 4 report, even if the
        # equal-quality registry revision is retained rather than spuriously upgraded.
        before = {p: read_report(p.parent).verification_id for p in
                  (tmp_path / "artifacts").rglob("report.json")}
        repeated = await builder.create(REQUEST, setup.repo, approved_spec=approved)
        assert repeated.accepted
        after = [read_report(p.parent) for p in (tmp_path / "artifacts").rglob("report.json")
                 if p not in before or read_report(p.parent).verification_id != before[p]]
        new_child = [r for r in after if r.spec_hash == approved.spec_hash]
        assert new_child and all(r.passed for r in new_child)
        assert all(r.verification_id != installed.report.verification_id for r in new_child)
        assert services.registry.get(factory.output["name"]).licensed
        assert setup.inference.calls == 4
    finally:
        services.close()


class MarkerPi:
    """Small negative-test seam: identify which scoped capability was selected."""

    def __init__(self, marker: str) -> None:
        self.marker = marker

    async def run(self, context, task, pack):
        return self.marker


async def selected_backend(runtime: Runtime) -> str:
    async def harness(context, task):
        return await context.code(task, {})

    result = await runtime.evaluate(specification(), harness, {}, FakeAdapter())
    return result.output


async def test_backend_capability_isolated_across_concurrent_tasks(tmp_path: Path) -> None:
    runtime = Runtime(TelemetryStore(tmp_path), pi_backend=MarkerPi("default"))
    left_entered, right_entered = asyncio.Event(), asyncio.Event()

    async def use(marker, entered, other):
        with runtime.use_pi_backend(MarkerPi(marker)):
            entered.set()
            await asyncio.wait_for(other.wait(), 5)
            return await selected_backend(runtime)

    try:
        left, right = await asyncio.gather(
            use("left", left_entered, right_entered),
            use("right", right_entered, left_entered),
        )
        assert (left, right) == ("left", "right")
        assert await selected_backend(runtime) == "default"
    finally:
        runtime.store.close()


@pytest.mark.parametrize("exception", [False, True])
async def test_backend_capability_revoked_in_inherited_task_after_scope_exit(
    tmp_path: Path, exception: bool,
) -> None:
    runtime = Runtime(TelemetryStore(tmp_path), pi_backend=MarkerPi("default"))
    release = asyncio.Event()

    async def inherited():
        await release.wait()
        return await selected_backend(runtime)

    try:
        try:
            with runtime.use_pi_backend(MarkerPi("expired-compiler-capability")):
                task = asyncio.create_task(inherited())
                assert await selected_backend(runtime) == "expired-compiler-capability"
                if exception:
                    raise ValueError("fixture scope exit")
        except ValueError:
            pass
        release.set()
        assert await asyncio.wait_for(task, 5) == "default"
        assert await selected_backend(runtime) == "default"
    finally:
        runtime.store.close()


class CaptureRuntime:
    def __init__(self):
        self.backend = None

    def use_pi_backend(self, backend):
        from contextlib import contextmanager

        @contextmanager
        def capture():
            self.backend = backend
            yield

        return capture()


@pytest.mark.parametrize("payload", [
    {"request": "different request"},
    {"request": REQUEST, "approved": True},
    {"request": REQUEST, "report": {"passed": True}},
    {"request": REQUEST, "floor": {"limit": 0}},
    {"request": REQUEST, "budget": {"cost_usd": 9999}},
    {},
])
async def test_factory_rejects_forged_payload_without_child_compile(
    tmp_path: Path, payload,
) -> None:
    runtime = CaptureRuntime()
    replies = []

    async def forbidden(*args, **kwargs):
        pytest.fail("Untrusted factory payload reached compiler")

    async def run(*args, **kwargs):
        replies.append(await runtime.backend.compiler_tool(payload))
        # Invalid attempts also consume the single-call safety allowance.
        replies.append(await runtime.backend.compiler_tool({"request": REQUEST}))
        return OrchestrationResult(accepted=True, tasks=())

    services = SimpleNamespace(
        runtime=runtime, compiler=SimpleNamespace(compile=forbidden),
        orchestrator=SimpleNamespace(run=run, config=OrchestratorConfig()),
        profile=Profile(models={"fake": AdapterConfig(provider="fake")},
                        builder_floor=specification().floor), workspace=tmp_path,
    )
    result = await FeatureBuilder(services).create(REQUEST, tmp_path)
    assert not result.accepted and result.failures == ("feature_compile_failed",)
    assert replies == [{"error": "factory_request_mismatch"}, {"error": "factory_call_limit"}]


async def test_factory_depth_limit_stops_child_compiler(tmp_path: Path) -> None:
    runtime = CaptureRuntime()
    replies = []

    async def forbidden(*args, **kwargs):
        pytest.fail("Depth denial reached compiler")

    async def run(*args, **kwargs):
        with _nested():
            replies.append(await runtime.backend.compiler_tool({"request": REQUEST}))
        return OrchestrationResult(accepted=True, tasks=())

    services = SimpleNamespace(
        runtime=runtime, compiler=SimpleNamespace(compile=forbidden),
        orchestrator=SimpleNamespace(run=run, config=OrchestratorConfig(max_depth=1)),
        profile=Profile(models={"fake": AdapterConfig(provider="fake")},
                        builder_floor=specification().floor), workspace=tmp_path,
    )
    result = await FeatureBuilder(services).create(REQUEST, tmp_path)
    assert not result.accepted and replies == [{"error": "factory_depth_limit"}]


@pytest.mark.parametrize("case", ["missing_floor", "blank_request", "ephemeral_approval"])
async def test_factory_trusted_configuration_is_required(tmp_path: Path, case: str) -> None:
    from architect.orchestrator import OrchestrationError

    profile = Profile(models={"fake": AdapterConfig(provider="fake")},
                      builder_floor=None if case == "missing_floor" else specification().floor)
    services = SimpleNamespace(profile=profile)
    with pytest.raises(OrchestrationError, match=(
        "approved_feature_must_be_installed" if case == "ephemeral_approval"
        else "builder_floor_required"
    )):
        await FeatureBuilder(services).create(
            " " if case == "blank_request" else REQUEST, tmp_path,
            approved_spec=specification() if case == "ephemeral_approval" else None,
        )


async def test_nested_child_compilation_cannot_reset_parent_dollar_cap(tmp_path: Path) -> None:
    setup = OfflineSetup(tmp_path, child_cost=0.2)
    services = setup.services
    services.orchestrator.config = OrchestratorConfig(budget=Budget(cost_usd=0.1, compute_s=120))
    try:
        result = await FeatureBuilder(services).create(
            REQUEST, setup.repo, approved_spec=specification(installed=True),
        )
        assert not result.accepted
        assert not result.tasks[-1].accepted
        assert services.registry.list() == ()
        assert not (setup.repo / "feature.py").exists()
        # The child had a larger own budget, but not a fresh parent wallet.
        assert "feature" in setup.floor_calls
        child_adapters = [a for a in setup.adapters if a.model == "llama3.2"]
        assert child_adapters and all(a.calls == 0 for a in child_adapters)
        assert any("budget" in code for task in result.tasks for code in task.failures)
    finally:
        services.close()


@pytest.mark.parametrize("exit_kind", ["normal", "exception", "cancel"])
async def test_retained_factory_callback_cannot_compile_after_create_returns(
    tmp_path: Path, exit_kind: str,
) -> None:
    runtime = CaptureRuntime()

    async def forbidden(*args, **kwargs):
        pytest.fail("Expired feature compiler capability was invoked")

    async def run(*args, **kwargs):
        if exit_kind == "exception":
            raise ValueError("fixture orchestration failure")
        if exit_kind == "cancel":
            raise asyncio.CancelledError
        return OrchestrationResult(accepted=True, tasks=())

    services = SimpleNamespace(
        runtime=runtime, compiler=SimpleNamespace(compile=forbidden),
        orchestrator=SimpleNamespace(run=run, config=OrchestratorConfig()),
        profile=Profile(models={"fake": AdapterConfig(provider="fake")},
                        builder_floor=specification().floor), workspace=tmp_path,
    )
    if exit_kind == "normal":
        result = await FeatureBuilder(services).create(REQUEST, tmp_path)
        assert not result.accepted and result.failures == ("factory_not_called",)
    else:
        error = ValueError if exit_kind == "exception" else asyncio.CancelledError
        with pytest.raises(error):
            await FeatureBuilder(services).create(REQUEST, tmp_path)
    expired = await runtime.backend.compiler_tool({"request": REQUEST})
    assert isinstance(expired, dict) and "error" in expired


async def test_unfinished_feature_callback_is_cancelled_and_drained_before_exit(
    tmp_path: Path,
) -> None:
    runtime = CaptureRuntime()
    entered, drained, never = asyncio.Event(), asyncio.Event(), asyncio.Event()
    pending = []

    async def stalled_compile(*args, **kwargs):
        entered.set()
        try:
            await never.wait()
        finally:
            drained.set()

    async def run(*args, **kwargs):
        pending.append(asyncio.create_task(runtime.backend.compiler_tool({"request": REQUEST})))
        await asyncio.wait_for(entered.wait(), 5)
        return OrchestrationResult(accepted=True, tasks=())

    services = SimpleNamespace(
        runtime=runtime, compiler=SimpleNamespace(compile=stalled_compile),
        orchestrator=SimpleNamespace(run=run, config=OrchestratorConfig()),
        profile=Profile(models={"fake": AdapterConfig(provider="fake")},
                        builder_floor=specification().floor), workspace=tmp_path,
    )
    try:
        result = await asyncio.wait_for(FeatureBuilder(services).create(REQUEST, tmp_path), 5)
        assert not result.accepted
        assert drained.is_set() and pending[0].cancelled()
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.parametrize("failure", ["storage_error", "unlicensed_existing", "wrong_spec_existing"])
async def test_registry_failure_cannot_become_successful_feature_install(
    tmp_path: Path, failure: str,
) -> None:
    """Negative-only registry/compiler doubles; real happy-path coverage stays above."""
    from architect.orchestrator import OrchestrationError
    from architect.registry import RegistryError

    runtime = CaptureRuntime()
    approved = specification(installed=True)
    candidate = SimpleNamespace(licensed=True, spec=approved)
    existing_spec = approved.model_copy(update={"task": "Different installed feature"})
    existing = SimpleNamespace(
        licensed=failure != "unlicensed_existing",
        spec=existing_spec if failure == "wrong_spec_existing" else approved,
    )
    rejected = RegistryError("storage_unavailable" if failure == "storage_error" else "not_better")
    calls = []

    async def compile_approved(source, *, approved):
        assert source is candidate.spec and approved is True
        calls.append("compile")
        return SimpleNamespace(approval_required=False, artifact=candidate)

    def install(name, artifact):
        assert name == "feature-" + candidate.spec.spec_hash[:16]
        assert artifact is candidate
        calls.append("install")
        raise rejected

    def get(name):
        assert name == "feature-" + candidate.spec.spec_hash[:16]
        calls.append("get")
        return existing

    def forbidden_status(*args):
        pytest.fail("Rejected installation exposed a successful registry entry")

    async def run(*args, **kwargs):
        await runtime.backend.compiler_tool({"request": REQUEST})
        pytest.fail("Registry rejection escaped the factory as success")

    services = SimpleNamespace(
        runtime=runtime, compiler=SimpleNamespace(compile=compile_approved),
        registry=SimpleNamespace(install=install, get=get, status=forbidden_status),
        orchestrator=SimpleNamespace(run=run, config=OrchestratorConfig()),
        profile=Profile(models={"fake": AdapterConfig(provider="fake")},
                        builder_floor=specification().floor), workspace=tmp_path,
    )
    if failure == "storage_error":
        with pytest.raises(RegistryError, match="^storage_unavailable$") as caught:
            await FeatureBuilder(services).create(REQUEST, tmp_path, approved_spec=approved)
        assert caught.value is rejected
        assert calls == ["compile", "install"]
    else:
        with pytest.raises(OrchestrationError, match="^feature_install_failed$"):
            await FeatureBuilder(services).create(REQUEST, tmp_path, approved_spec=approved)
        assert calls == ["compile", "install", "get"]
    assert await runtime.backend.compiler_tool({"request": REQUEST}) == {
        "error": "factory_capability_expired",
    }
