"""Phase 10 Sections 12.2 and 12.5 (WP3): ``forge integrate`` and its four gates (P9).

Offline only (fake adapters, sockets blocked, Forge in template mode). The Pi path is
exercised through ``FeatureBuilder`` with a capturing runtime seam (as
``tests/test_features.py`` does) and through the real ``PiBackend`` protocol
boundary with an inert IPC peer; no Pi process, model CLI or network is used.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from forge_modes_support import (
    TEMPLATE,
    Consent,
    artifact_dirs,
    block_sockets,
    forge_repairs,
    make_profile,
    services_for,
    spec,
    tree_digest,
)

from architect.backends.pi import PiBackend, PiConfig, PiError
from architect.compiler.types import CompileResult
from architect.forge_modes import (
    ExportError,
    ForgeModeError,
    IntegrateOptions,
    IntegrateResult,
    export_package,
    forge_integrate,
    place_package,
    place_package_tool,
    verify_export,
)
from architect.forge_modes._routing import InstalledRef
from architect.forge_modes.export import default_package, read_manifest
from architect.models import FakeAdapter
from architect.orchestrator import (
    OrchestrationError,
    OrchestrationResult,
    OrchestratorConfig,
)
from architect.orchestrator.features import FeatureBuilder
from architect.profiles import AdapterConfig, Profile
from architect.runtime import RunContext
from architect.spec import Budget, FloorDefinition, FloorKind, Lifecycle, Spec
from architect.telemetry import categorical

REQUEST = "Build an ok feature"
BUILDER_FLOOR = {"kind": "held_out_tasks", "limit": 1, "dataset": "builder"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    block_sockets(monkeypatch)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "delivery-repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("print('app')\n", encoding="utf-8")
    return repo


def _files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _approved() -> Spec:
    return spec(lifecycle=Lifecycle.INSTALL)


# ------------------------------------------------------------------ gate 1: approval


async def test_without_approval_only_stage_0_runs_and_the_proposal_is_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inferred = spec()
    response = {"text": inferred.model_dump_json(exclude={"provenance"}), "model": "fake",
                "tokens_in": 10, "tokens_out": 10, "cost_usd": 0.0}
    (tmp_path / "inference.json").write_text(json.dumps([response]), encoding="utf-8")
    profile = make_profile(
        tmp_path, inference={"provider": "fake", "model": "fake", "fixture": "inference.json"}
    )
    services = services_for(profile)
    repo = _repo(tmp_path)
    before = tree_digest(repo)
    consent = Consent()
    try:
        result = await forge_integrate(
            REQUEST, repo, services, TEMPLATE, into="src", package=None, approved_spec=None,
            consent=consent,
        )
        rows = services.runtime.store.rows()
    finally:
        services.close()
    assert isinstance(result, CompileResult) and result.approval_required
    assert result.stages == (0,) and result.spec.lifecycle == Lifecycle.INSTALL
    proposal = tmp_path / "work" / "proposals" / f"{result.spec.spec_hash}.json"
    assert Spec.model_validate_json(proposal.read_text("utf-8")) == result.spec
    assert proposal.read_text("utf-8") == result.spec.model_dump_json(indent=2) + "\n"
    # No Forge line, no consent, no spend beyond Stage 0, nothing in the repository.
    assert consent.texts == [] and "forge" not in capsys.readouterr().err
    assert [row.task_type for row in rows] == [categorical("inference")]
    assert artifact_dirs(tmp_path / "work" / "artifacts") == []
    assert tree_digest(repo) == before


async def test_an_expiring_approved_spec_is_refused(tmp_path: Path) -> None:
    services = services_for(make_profile(tmp_path))
    try:
        with pytest.raises(ForgeModeError, match="^approved_feature_must_be_installed$"):
            await forge_integrate(
                REQUEST, _repo(tmp_path), services, TEMPLATE, into="src", package=None,
                approved_spec=spec(), consent=Consent(),
            )
    finally:
        services.close()


@pytest.mark.parametrize(
    ("into", "package", "code"),
    [("../out", None, "export_path_invalid"), (".architect", None, "export_path_invalid"),
     ("src", "Bad-Name", "export_package_invalid"), ("src", "taken", "export_target_exists")],
)
async def test_bad_destinations_are_refused_before_any_spend(
    tmp_path: Path, into: str, package: str | None, code: str
) -> None:
    services = services_for(make_profile(tmp_path))
    repo = _repo(tmp_path)
    (repo / "src" / "taken").mkdir()
    consent = Consent()
    try:
        with pytest.raises(ForgeModeError, match=f"^{code}$"):
            await forge_integrate(
                REQUEST, repo, services, TEMPLATE, into=into, package=package,
                approved_spec=_approved(), consent=consent,
            )
        assert services.runtime.store.rows() == []
    finally:
        services.close()
    assert consent.texts == []
    assert artifact_dirs(tmp_path / "work" / "artifacts") == []


# ------------------------------------------------------------------ gates 2-4 and placement


async def test_spend_consent_refused_is_forge_cancelled(tmp_path: Path) -> None:
    services = services_for(make_profile(tmp_path, builder_floor=BUILDER_FLOOR))
    repo = _repo(tmp_path)
    before = tree_digest(repo)
    try:
        with pytest.raises(ForgeModeError, match="^forge_cancelled$"):
            await forge_integrate(
                REQUEST, repo, services, TEMPLATE, into="src", package=None,
                approved_spec=_approved(), consent=Consent(False),
            )
        assert services.registry.list() == ()
        assert services.runtime.store.rows() == []
    finally:
        services.close()
    assert tree_digest(repo) == before
    assert not (tmp_path / "work" / "exports").exists()


async def test_package_only_licenses_installs_exports_and_places_one_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forge_repairs(monkeypatch)
    services = services_for(make_profile(tmp_path, reply="nope"))
    repo = _repo(tmp_path)
    before = _files(repo)
    consent = Consent()
    approved = _approved()
    try:
        result = await forge_integrate(
            REQUEST, repo, services, TEMPLATE, into="src", package=None,
            approved_spec=approved, consent=consent, package_only=True,
        )
        entry = services.registry.status("feature-" + approved.spec_hash[:16])
        installed = services.registry.get(entry.name)
    finally:
        services.close()
    assert isinstance(result, IntegrateResult) and result.codes == ()
    assert result.licensed and result.human_review_required and result.feature_flag_required
    assert (result.name, result.revision, result.spec_hash) == (
        entry.name, entry.revision, approved.spec_hash
    )
    package = default_package(approved.spec_hash)
    placed = repo / "src" / package
    assert result.export["placement"] == "host"
    assert result.export["package_path"] == str(placed)
    assert result.export["evidence_label"] == "host_attested"
    manifest, digest = read_manifest(placed)
    assert result.export["manifest_sha256"] == digest
    assert result.export["files"] == len(manifest.files) + 1
    assert manifest.artifact_digest == installed.artifact_digest
    assert installed.forge is not None  # a Forge-built harness
    # Only the new directory changed.
    after = _files(repo)
    assert {k: v for k, v in after.items() if not k.startswith(f"src/{package}/")} == before
    assert verify_export(placed, registry=None).codes == ()
    # Consent order: role + Phase 9 line, then the export line.
    spend, export = consent.texts
    assert spend.splitlines()[0] == "forge agent: fake (mode template, turns 200)"
    assert spend.splitlines()[1].startswith("forge: compile cap $10.00 + forge cap $5.00")
    kb = -(-result.export["bytes"] // 1024)  # type: ignore[operator]
    assert export == (
        f"export: {package} ({result.export['files']} files, {kb} KB) -> src/{package}; "
        "feature flag OFF; human review required"
    )


@pytest.mark.parametrize("race", ["directory", "file"])
async def test_package_only_placement_that_fails_reports_staged_not_host(
    tmp_path: Path, race: str
) -> None:
    """The target appears after the pre-spend check (during the build): nothing is placed."""
    services = services_for(make_profile(tmp_path))
    repo = _repo(tmp_path)
    target = repo / "src" / "raced"

    def consent(text: str) -> bool:
        assert text.isascii()
        if text.startswith("export: "):
            # Someone else's work lands at the destination while the build ran.
            if race == "directory":
                target.mkdir()
                (target / "theirs.py").write_bytes(b"x = 1\n")
            else:
                target.write_bytes(b"x = 1\n")
        return True

    try:
        result = await forge_integrate(
            REQUEST, repo, services, TEMPLATE, into="src", package="raced",
            approved_spec=_approved(), consent=consent, package_only=True,
        )
        assert len(services.registry.list()) == 1  # the paid build stays installed
    finally:
        services.close()
    assert isinstance(result, IntegrateResult)
    assert result.codes == ("export_target_exists",)
    assert result.export["placement"] == "staged"
    staged = Path(str(result.export["package_path"]))
    assert staged.parent.parent == tmp_path / "work" / "exports" and staged.name == "raced"
    assert read_manifest(staged)[1] == result.export["manifest_sha256"]
    assert verify_export(staged, registry=None).codes == ()
    # The other writer's work is untouched.
    if race == "directory":
        assert _files(target) == {"theirs.py": b"x = 1\n"}
    else:
        assert target.read_bytes() == b"x = 1\n"


async def test_package_only_copy_that_fails_its_re_hash_is_reported_where_it_was_placed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one refusal that leaves this call's directory behind: report it, delete nothing."""
    import architect.forge_modes.export as export_module

    genuine = export_module._integrity
    repo = _repo(tmp_path)
    target = repo / "src" / "rehash"

    def integrity(root: Path, manifest: Any) -> bool:
        return genuine(root, manifest) and Path(root).resolve() != target.resolve()

    monkeypatch.setattr(export_module, "_integrity", integrity)
    services = services_for(make_profile(tmp_path))
    try:
        result = await forge_integrate(
            REQUEST, repo, services, TEMPLATE, into="src", package="rehash",
            approved_spec=_approved(), consent=Consent(), package_only=True,
        )
    finally:
        services.close()
    assert isinstance(result, IntegrateResult)
    assert result.codes == ("export_integrity_mismatch",)
    assert result.export["placement"] == "host"
    assert result.export["package_path"] == str(target)
    assert read_manifest(target)[1] == result.export["manifest_sha256"]


async def test_stage_only_writes_nothing_into_the_repository(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    services = services_for(make_profile(tmp_path))
    repo = _repo(tmp_path)
    before = tree_digest(repo)
    consent = Consent()
    try:
        result = await forge_integrate(
            REQUEST, repo, services, TEMPLATE, into="src", package="staged_pkg",
            approved_spec=_approved(), consent=consent, stage_only=True,
        )
    finally:
        services.close()
    assert tree_digest(repo) == before
    assert isinstance(result, IntegrateResult) and result.export["placement"] == "staged"
    staged = Path(str(result.export["package_path"]))
    assert staged.parent.parent == tmp_path / "work" / "exports"
    assert read_manifest(staged)[1] == result.export["manifest_sha256"]
    assert len(consent.texts) == 1  # no export consent: nothing goes into the repository
    assert f"export staged: {staged}" in capsys.readouterr().err


async def test_export_consent_refused_keeps_entry_and_staging_and_writes_nothing(
    tmp_path: Path,
) -> None:
    services = services_for(make_profile(tmp_path, builder_floor=BUILDER_FLOOR))
    repo = _repo(tmp_path)
    before = tree_digest(repo)
    try:
        result = await forge_integrate(
            REQUEST, repo, services, TEMPLATE, into="src", package="kept",
            approved_spec=_approved(), consent=Consent(True, False),
        )
        assert len(services.registry.list()) == 1
    finally:
        services.close()
    assert isinstance(result, IntegrateResult) and result.codes == ("export_cancelled",)
    assert tree_digest(repo) == before
    assert read_manifest(Path(str(result.export["package_path"])))[1] == (
        result.export["manifest_sha256"]
    )


async def test_post_run_tampering_is_reported_and_nothing_is_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[IntegrateOptions] = []

    async def pi_path(self: FeatureBuilder, request: str, repo: Path, *, approved_spec: Spec,
                      integrate: IntegrateOptions) -> OrchestrationResult:
        seen.append(integrate)
        place_package(integrate.staged, repo, integrate.into)
        tampered = repo / integrate.into / integrate.package / "README.md"
        tampered.write_text("rewritten by the builder\n", encoding="utf-8")
        return OrchestrationResult(accepted=True, tasks=())

    monkeypatch.setattr(FeatureBuilder, "create", pi_path)
    services = services_for(make_profile(tmp_path, builder_floor=BUILDER_FLOOR))
    repo = _repo(tmp_path)
    approved = _approved()
    try:
        result = await forge_integrate(
            REQUEST, repo, services, TEMPLATE, into="src", package="pkg",
            approved_spec=approved, consent=Consent(),
        )
        installed = services.registry.get("feature-" + approved.spec_hash[:16])
    finally:
        services.close()
    assert isinstance(result, IntegrateResult)
    assert result.export["placement"] == "pi"
    assert result.codes == ("export_integrity_mismatch",)
    assert result.human_review_required is True
    assert (repo / "src" / "pkg" / "README.md").read_text("utf-8") == "rewritten by the builder\n"
    [options] = seen
    assert options.into == "src" and options.package == "pkg"
    assert options.entry.name == "feature-" + approved.spec_hash[:16]
    assert options.entry.artifact_digest == installed.artifact_digest
    assert options.manifest_sha256 == result.export["manifest_sha256"]


async def test_pi_placement_without_a_builder_floor_is_refused_before_any_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def never(*args: Any, **kwargs: Any) -> OrchestrationResult:
        pytest.fail("the builder must not be reached")

    monkeypatch.setattr(FeatureBuilder, "create", never)
    services = services_for(make_profile(tmp_path))  # no builder_floor
    repo = _repo(tmp_path)
    before = tree_digest(repo)
    consent = Consent()
    try:
        with pytest.raises(ForgeModeError, match="^builder_floor_required$"):
            await forge_integrate(
                REQUEST, repo, services, TEMPLATE, into="src", package="pkg",
                approved_spec=_approved(), consent=consent,
            )
        assert services.runtime.store.rows() == []  # not one model call
        assert services.registry.list() == ()
    finally:
        services.close()
    assert consent.texts == []  # no Forge spend consent was even asked
    assert artifact_dirs(tmp_path / "work" / "artifacts") == []
    assert not (tmp_path / "work" / "exports").exists()
    assert tree_digest(repo) == before


@pytest.mark.parametrize("builder", ["skips_the_tool", "fails", "raises"])
async def test_a_package_the_builder_never_placed_is_reported_as_staged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, builder: str
) -> None:
    async def pi_path(self: FeatureBuilder, request: str, repo: Path, *, approved_spec: Spec,
                      integrate: IntegrateOptions) -> OrchestrationResult:
        if builder == "raises":
            raise OrchestrationError("factory_depth_limit")
        if builder == "fails":
            return OrchestrationResult(accepted=False, tasks=(), failures=("max_depth",))
        return OrchestrationResult(accepted=True, tasks=())

    monkeypatch.setattr(FeatureBuilder, "create", pi_path)
    services = services_for(make_profile(tmp_path, builder_floor=BUILDER_FLOOR))
    repo = _repo(tmp_path)
    before = tree_digest(repo)
    try:
        result = await forge_integrate(
            REQUEST, repo, services, TEMPLATE, into="src", package="pkg",
            approved_spec=_approved(), consent=Consent(),
        )
    finally:
        services.close()
    assert isinstance(result, IntegrateResult) and result.licensed
    # Not placed is not tampered: the package is where it was staged, and it is intact.
    assert "export_integrity_mismatch" not in result.codes
    assert result.export["placement"] == "staged"
    staged = Path(str(result.export["package_path"]))
    assert staged.parent.parent == tmp_path / "work" / "exports" and staged.name == "pkg"
    assert verify_export(staged, registry=None).codes == ()
    expected = {"skips_the_tool": (), "fails": ("max_depth",),
                "raises": ("factory_depth_limit",)}[builder]
    assert result.codes == expected
    assert result.human_review_required is True
    assert tree_digest(repo) == before and not (repo / "src" / "pkg").exists()


# ------------------------------------------------------------------ FeatureBuilder hook


class CaptureRuntime:
    def __init__(self) -> None:
        self.backend: Any = None

    def use_pi_backend(self, backend: Any) -> Any:
        @contextmanager
        def capture() -> Any:
            self.backend = backend
            yield

        return capture()


async def _staged_entry(tmp_path: Path) -> tuple[Any, IntegrateOptions]:
    services = services_for(make_profile(tmp_path))
    try:
        approved = _approved()
        compiled = await services.compiler.compile(approved, approved=True)
        assert compiled.artifact is not None
        name = "feature-" + approved.spec_hash[:16]
        services.registry.install(name, compiled.artifact)
        entry = services.registry.status(name)
        artifact = services.registry.get(name)
        manifest, digest = export_package(entry, services.workspace / "exports", "pkg")
    finally:
        services.close()
    staged = services.workspace / "exports" / manifest.artifact_digest[:16] / "pkg"
    options = IntegrateOptions(
        entry=InstalledRef(name=name, revision=entry.revision,
                           artifact_digest=artifact.artifact_digest,
                           spec_hash=artifact.spec.spec_hash),
        staged=staged, into="src", package="pkg", manifest_sha256=digest,
    )
    return services.registry, options


def _builder_services(tmp_path: Path, registry: Any, run: Any) -> Any:
    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("integrate must not compile inside compile_agent")

    return SimpleNamespace(
        runtime=CaptureRuntime(), compiler=SimpleNamespace(compile=forbidden),
        orchestrator=SimpleNamespace(run=run, config=OrchestratorConfig()),
        profile=Profile(
            models={"fake": AdapterConfig(provider="fake")},
            builder_floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1,
                                          dataset="builder"),
            pi=PiConfig(denied_paths=(tmp_path / "work" / "exports",)),
        ),
        workspace=tmp_path / "work", registry=registry,
    )


async def test_integrate_places_through_the_single_use_tool_after_compile_agent(
    tmp_path: Path,
) -> None:
    registry, options = await _staged_entry(tmp_path)
    repo = _repo(tmp_path)
    replies: list[Any] = []
    captured: dict[str, Any] = {}

    async def run(goal: str, *, repo: Path, overrides: dict[str, Any]) -> OrchestrationResult:
        backend = services.runtime.backend
        captured.update(goal=goal, overrides=overrides, backend=backend)
        tool = backend.capabilities["place_harness_package"]
        replies.append(await tool.callback({"into": "src"}))  # before compile_agent
        replies.append(await backend.compiler_tool({"request": REQUEST}))
        replies.append(await tool.callback({"into": "elsewhere"}))
        replies.append(await tool.callback({"into": "src"}))
        replies.append(await tool.callback({"into": "src"}))
        return OrchestrationResult(accepted=True, tasks=())

    services = _builder_services(tmp_path, registry, run)
    result = await FeatureBuilder(services).create(
        REQUEST, repo, approved_spec=_approved(), integrate=options
    )
    assert result.accepted and result.failures == ()
    backend = captured["backend"]
    assert isinstance(backend, PiBackend)
    assert "place_harness_package" in backend.config.allowed_tools
    assert "place_harness_package" in captured["overrides"]["tools"]
    tool = backend.capabilities["place_harness_package"]
    assert tool.max_calls == 1 and tool.requires_build is False
    assert captured["goal"].endswith(
        ' After compile_agent succeeds, call place_harness_package exactly once with into '
        'equal to "src", then write only call-site glue that checks the package\'s '
        "enabled() flag."
    )
    feature = replies[1]
    assert feature["code"] == "integrate_reused_installed" and feature["licensed"] is True
    assert feature["name"] == options.entry.name and feature["spec_hash"] == _approved().spec_hash
    assert replies[0] == {"error": "integrate_not_ready"}
    assert replies[2] == {"error": "export_path_invalid"}
    assert replies[3] == {"path": "src/pkg", "files": len(read_manifest(options.staged)[0].files)
                          + 1, "manifest_sha256": options.manifest_sha256}
    assert replies[4] == {"error": "factory_call_limit"}
    assert result.tasks[-1].output == feature
    assert verify_export(repo / "src" / "pkg", registry=None).codes == ()
    # After create returns, a retained callback is expired.
    assert await tool.callback({"into": "src"}) == {"error": "factory_capability_expired"}


async def test_integrate_refuses_a_changed_entry_or_a_tampered_staging_copy(
    tmp_path: Path,
) -> None:
    registry, options = await _staged_entry(tmp_path)
    repo = _repo(tmp_path)
    replies: list[Any] = []

    async def run(goal: str, *, repo: Path, overrides: dict[str, Any]) -> OrchestrationResult:
        backend = services.runtime.backend
        replies.append(await backend.compiler_tool({"request": REQUEST}))
        replies.append(await backend.capabilities["place_harness_package"].callback(
            {"into": "src"}
        ))
        return OrchestrationResult(accepted=True, tasks=())

    services = _builder_services(tmp_path, registry, run)
    moved = options.model_copy(update={"entry": options.entry.model_copy(
        update={"artifact_digest": "0" * 64}
    )})
    result = await FeatureBuilder(services).create(
        REQUEST, repo, approved_spec=_approved(), integrate=moved
    )
    assert not result.accepted and result.failures == ("feature_compile_failed",)
    assert replies == [{"error": "feature_compile_failed"}, {"error": "integrate_not_ready"}]
    assert not (repo / "src" / "pkg").exists()
    # A staging manifest that is not the one the operator consented to is refused up front.
    with pytest.raises(OrchestrationError, match="^export_integrity_mismatch$"):
        await FeatureBuilder(services).create(
            REQUEST, repo, approved_spec=_approved(),
            integrate=options.model_copy(update={"manifest_sha256": "f" * 64}),
        )
    with pytest.raises(OrchestrationError, match="^approval_required$"):
        await FeatureBuilder(services).create(REQUEST, repo, integrate=options)
    assert len(replies) == 2


# ------------------------------------------------------------------ Pi protocol boundary


class ProtocolPeer:
    """An inert IPC peer that replays messages; never a coding loop (test_pi_capabilities)."""

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.pid = 987654321
        self.returncode: int | None = None
        self.sent: list[dict[str, Any]] = []
        self.stdin = self
        self.stdout = asyncio.StreamReader(limit=2 * 1048576)
        self.stderr = asyncio.StreamReader(limit=2 * 1048576)
        for message in messages:
            self.stdout.feed_data((json.dumps(message) + "\n").encode())
        self.stdout.feed_eof()

    def write(self, data: bytes) -> None:
        self.sent.append(json.loads(data))

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


READY = {
    "kind": "ready",
    "extension_loaded": True,
    "versions": {name: "0.73.1" for name in ("pi-ai", "pi-agent-core", "pi-coding-agent")},
}


def _install_peer(monkeypatch: pytest.MonkeyPatch, peer: ProtocolPeer) -> None:
    async def spawn(*args: object, **kwargs: object) -> ProtocolPeer:
        return peer

    monkeypatch.setattr("architect.backends.pi.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("architect.backends.pi._windows_job", lambda pid: None)
    if hasattr(os, "killpg"):
        monkeypatch.setattr(os, "killpg", lambda pid, sig: peer.kill())


def _call(identity: int) -> dict[str, Any]:
    return {"kind": "capability", "id": identity, "name": "place_harness_package",
            "arguments": {"into": "src"}}


def _context(repo: Path, *, evaluation: bool) -> RunContext:
    context = RunContext(FakeAdapter(), Budget(cost_usd=1, compute_s=60), repository=repo,
                         is_evaluation=False)
    context.is_evaluation = evaluation
    return context


def _granting_config() -> PiConfig:
    """For this run only, exactly as ``FeatureBuilder`` adds the tool (``model_copy``).

    ``PiConfig``'s own validator admits only built-in and Forge tool names, so a
    validated ``PiConfig(allowed_tools=(..., "place_harness_package"))`` is refused.
    """
    with pytest.raises(ValueError, match="Unsupported or duplicate Pi tools"):
        PiConfig(allowed_tools=("read", "place_harness_package"))
    return PiConfig().model_copy(update={"allowed_tools": ("read", "place_harness_package")})


PACK: dict[str, Any] = {"tools": ["place_harness_package"], "prompt_fragment": "x",
                        "subagent_rules": []}


async def test_pi_grants_the_tool_once_and_never_during_evaluation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from architect.backends import pi as pi_module

    if not (pi_module._NODE / "node_modules/@mariozechner/pi-coding-agent/package.json").is_file():
        pytest.skip("Pi dependencies are not installed")
    _, options = await _staged_entry(tmp_path)
    repo = _repo(tmp_path)
    tool = place_package_tool(options.staged, repo, "src", ready=lambda: True,
                              active=lambda: True)
    backend = PiBackend(_granting_config(), capabilities={"place_harness_package": tool})
    peer = ProtocolPeer([READY, _call(1), _call(2)])
    _install_peer(monkeypatch, peer)
    try:
        await backend.run(_context(repo, evaluation=False), {}, PACK)
    except PiError:
        pass  # the inert peer ends the session without a final reply
    values = [item["value"] for item in peer.sent if item.get("kind") != "init"]
    assert values[0]["path"] == "src/pkg" and values[0]["manifest_sha256"] == (
        options.manifest_sha256
    )
    assert values[1] == {"ok": False, "code": "forge_tool_cap"}  # max_calls=1
    assert (repo / "src" / "pkg").is_dir()
    # During evaluation the capability does not exist: a forged call is fatal.
    other = tmp_path / "other-repo"
    other.mkdir()
    fresh = place_package_tool(options.staged, other, "src", ready=lambda: True,
                               active=lambda: True)
    backend = PiBackend(_granting_config(), capabilities={"place_harness_package": fresh})
    peer = ProtocolPeer([READY, _call(1)])
    _install_peer(monkeypatch, peer)
    with pytest.raises(PiError, match="^Capability denied$"):
        await backend.run(_context(other, evaluation=True), {}, PACK)
    assert not (other / "src").exists()


def test_place_tool_refuses_an_existing_target(tmp_path: Path) -> None:
    registry, options = asyncio.run(_staged_entry(tmp_path))
    repo = _repo(tmp_path)
    (repo / "src" / "pkg").mkdir()
    tool = place_package_tool(options.staged, repo, "src", ready=lambda: True,
                              active=lambda: True)
    assert asyncio.run(tool.callback({"into": "src"})) == {"error": "export_target_exists"}
    with pytest.raises(ExportError, match="^export_target_exists$"):
        place_package(options.staged, repo, "src")


# ------------------------------------------------------------------ reuse (WP5 stub)


class _RoutedResult(CompileResult):
    """Test-local stand-in for WP5's ``CompileResult.route``/``.reused``."""

    route: Any = None
    reused: bool = False


async def test_reuse_installed_skips_the_build_and_exports_the_installed_entry(
    tmp_path: Path,
) -> None:
    import dataclasses
    from uuid import uuid4

    from architect.compiler import Compiler
    from architect.forge_modes._routing import RouteDecision

    services = services_for(make_profile(tmp_path))
    repo = _repo(tmp_path)
    approved = _approved()
    seen: list[dict[str, Any]] = []
    try:
        compiled = await services.compiler.compile(approved, approved=True)
        assert compiled.artifact is not None
        name = "feature-" + approved.spec_hash[:16]
        services.registry.install(name, compiled.artifact)
        installed = services.registry.get(name)
        decision = RouteDecision.model_construct(
            decision_id=uuid4(), origin="forge_integrate", action="reuse_installed",
            target=InstalledRef(name=name, revision=1,
                                artifact_digest=installed.artifact_digest,
                                spec_hash=installed.spec.spec_hash),
            codes=(), explored=False,
        )
        routed = _RoutedResult(spec=installed.spec, artifact=installed,
                               report=installed.report, route=decision, reused=True)

        class Reusing(Compiler):
            async def compile(self, source: Any, **kwargs: Any) -> CompileResult:  # type: ignore[override]
                seen.append(kwargs)
                return routed

        stub = Reusing.__new__(Reusing)
        stub.__dict__.update(services.compiler.__dict__)
        rows_before = len(services.runtime.store.rows())
        result = await forge_integrate(
            REQUEST, repo, dataclasses.replace(services, compiler=stub), TEMPLATE,
            into="src", package="reused", approved_spec=approved, consent=Consent(),
            package_only=True, reuse_installed=True,
        )
        assert len(services.runtime.store.rows()) == rows_before  # no build, no run
        assert [entry.revision for entry in services.registry.list()] == [1]
    finally:
        services.close()
    assert seen and seen[0]["origin"] == "forge_integrate" and seen[0]["reuse_installed"]
    assert isinstance(result, IntegrateResult)
    assert result.codes == ("integrate_reused_installed",)
    assert (result.name, result.revision) == (name, 1)
    assert verify_export(repo / "src" / "reused", registry=None).codes == ()
