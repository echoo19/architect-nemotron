"""``forge integrate``: build, license, install, export, and place behind four gates.

Phase 10 Section 12.2 and P9. The gates run in this order and nothing is written into
the repository before all four have passed:

1. spec approval (without ``approved_spec`` only Stage 0 runs and the proposal is
   written, exactly as ``cli._compiled`` does; no Forge line prints);
2. Forge spend consent (the ``forge agent:`` line and the unchanged Phase 9 line);
3. a Stage 4 license (Forge armed with ``trigger="always"``), installed as
   ``"feature-" + spec_hash[:16]`` and re-loaded licensed, child-checked, digest-equal;
4. export consent for the staged package.

Placement is then exactly one of: ``stage_only`` (nothing in the repository),
``package_only`` (host copy of one new directory) or the guarded FeatureBuilder/Pi
path with the single-use ``place_harness_package`` tool. The Pi path needs
``profile.builder_floor``, which is checked with the destination before any spend.
Every placement is re-hashed against the manifest afterwards; a mismatch is
reported, never deleted. A package the builder never placed is reported as
``staged`` (it is still in staging, intact), not as an integrity mismatch.
"""

from __future__ import annotations

import asyncio
import math
import os
import sys
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, JsonValue

from architect.compiler.types import CompileResult, ForgeSettings
from architect.registry import Registry, RegistryEntry, RegistryError
from architect.runtime.wallet import compilation_wallet
from architect.spec import HarnessArtifact, Lifecycle, Spec
from architect.spec.binding import check_subharness_tree

from ._routing import (
    InstalledRef,
    compile_kwargs,
    evidence_class_of,
    fixed_codes,
    provider_of,
    record_outcome,
    record_role,
    reused,
    route_of,
    standing_grant,
)
from .errors import ExportError, ForgeModeError
from .export import (
    ExportManifest,
    _check_name,
    _linked,
    _target,
    default_package,
    evidence_label_of,
    export_package,
    place_package,
    read_manifest,
    verify_export,
)
from .run import (
    AgentChoice,
    consent_text,
    forge_summary,
    meter_budget,
    per_call_compiler,
    wrapped,
)

if TYPE_CHECKING:
    from architect.profiles import Services

__all__ = [
    "IntegrateOptions",
    "IntegrateResult",
    "cost_line",
    "export_line",
    "forge_integrate",
    "placement_codes",
]

Placement = Literal["pi", "host", "staged"]


class IntegrateOptions(BaseModel):
    """What ``FeatureBuilder.create(..., integrate=...)`` places instead of compiling."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entry: InstalledRef
    staged: Path
    into: str
    package: str
    manifest_sha256: str


class IntegrateResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    licensed: bool
    name: str
    revision: int
    spec_hash: str
    human_review_required: Literal[True] = True
    feature_flag_required: Literal[True] = True
    # {package_path, files, bytes, manifest_sha256,
    #  evidence_label: "host_attested"|"ed25519_signed", placement: "pi"|"host"|"staged"}
    export: dict[str, JsonValue]
    codes: tuple[str, ...]
    # Honesty rule 8: the build (agent) and compile cost of this call, reported
    # separately; an integrate never plays the harness, so there is no play cost.
    build_cost_usd: float = 0.0
    compile_cost_usd: float = 0.0


def cost_line(name: str, revision: int, build: float, compile_cost: float, *, reused: bool) -> str:
    """The integrate cost line (honesty rule 8), printed once the build has finished."""
    how = "reused installed" if reused else "licensed and installed as"
    return (
        f"forge integrate: {how} {name}@{revision}; build ${build:.4f} agent "
        f"(API-equivalent), compile ${compile_cost:.4f}, play $0.0000 (nothing is run)"
    )


_W7_CODES = frozenset(
    {
        "artifact_changed",
        "unsigned",
        "key_untrusted",
        "trust_in_target",
        "trust_list_invalid",
        "trust_list_missing",
    }
)


def placement_codes(
    placed_dir: Path, manifest_sha256: str, registry: Registry | None
) -> list[str]:
    """Section 12.2 step 7 after any placement: re-hash every file against the manifest
    the operator consented to (``manifest_sha256``), and re-check the pins, the children
    and, with the host key or a trusted Ed25519 key, the license. Report only; nothing is
    ever deleted."""
    check = verify_export(placed_dir, registry=registry)
    try:
        placed_sha: str | None = read_manifest(placed_dir)[1]
    except ExportError:
        placed_sha = None
    # W7: bytes a trusted key signed changed, a seal-verified license is unsealed, no key
    # is trusted at all, or a key or trust list lies inside the placed package.
    codes: list[str] = [code for code in check.codes if code in _W7_CODES]
    if not check.integrity or placed_sha != manifest_sha256:
        codes.append("export_integrity_mismatch")
    if not check.pins:
        codes.append("export_pin_mismatch")
    if not check.children:
        codes.append("export_subharness_missing")
    if check.licensed is False:
        codes.append("export_unverified")
    return codes


def export_line(package: str, files: int, size: int, into: str) -> str:
    """The export consent line (Section 12.2 step 5); ``Continue? [y/N]`` follows it."""
    kb = max(1, math.ceil(size / 1024))
    return (
        f"export: {package} ({files} files, {kb} KB) -> {into}/{package}; "
        "feature flag OFF; human review required"
    )


def _write_proposal(spec: Spec, workspace: Path) -> Path:
    """``proposals/<spec_hash>.json`` exactly as ``cli._proposal`` writes it."""
    directory = workspace / "proposals"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{spec.spec_hash}.json"
    descriptor, temporary = tempfile.mkstemp(prefix=".proposal-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(spec.model_dump_json(indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target


def _repository(repo: Path) -> Path:
    try:
        resolved = repo.resolve(strict=True)
    except OSError:
        raise ForgeModeError("invalid_repository") from None
    if not resolved.is_dir():
        raise ForgeModeError("invalid_repository")
    return resolved


def _denied(services: Services) -> tuple[Path, ...]:
    """The effective Pi denied set (``build_services`` puts it on ``profile.pi``)."""
    profile = getattr(services, "profile", None)
    pi = getattr(profile, "pi", None)
    return tuple(getattr(pi, "denied_paths", ()) or ())


def _reload(
    services: Services, name: str, expected: HarnessArtifact | None, approved: Spec
) -> tuple[RegistryEntry, HarnessArtifact]:
    """The installed entry, re-loaded licensed, child-checked and digest-equal."""
    try:
        entry = services.registry.status(name)
        artifact = HarnessArtifact.load(entry.directory)
        check_subharness_tree(artifact)
    except (RegistryError, OSError, ValueError, TypeError):
        raise ForgeModeError("feature_compile_failed") from None
    if (
        not artifact.licensed
        or artifact.spec.lifecycle != Lifecycle.INSTALL
        or artifact.spec.spec_hash != approved.spec_hash
        or artifact.spec.spec_hash != entry.spec_hash
        or (expected is not None and artifact.artifact_digest != expected.artifact_digest)
    ):
        raise ForgeModeError("feature_compile_failed")
    return entry, artifact


def _install(services: Services, artifact: HarnessArtifact) -> tuple[str, HarnessArtifact | None]:
    """Install as ``feature-<hash16>`` with ``features.py``'s ``not_better`` handling.

    Returns the name and the artifact the entry must now equal (None: the incumbent,
    an equally good licensed revision of the same spec, is kept as is).
    """
    name = "feature-" + artifact.spec.spec_hash[:16]
    try:
        services.registry.install(name, artifact)
        return name, artifact
    except RegistryError as exc:
        if str(exc) != "not_better":
            raise ForgeModeError("feature_install_failed") from None
    try:
        existing = services.registry.get(name)
    except RegistryError:
        raise ForgeModeError("feature_install_failed") from None
    if not existing.licensed or existing.spec.spec_hash != artifact.spec.spec_hash:
        raise ForgeModeError("feature_install_failed")
    return name, None


def _size(root: Path, manifest: ExportManifest) -> int:
    names = [name for name, _ in manifest.files] + ["MANIFEST.json"]
    return sum((root / name).stat().st_size for name in names)


async def forge_integrate(
    request: str,
    repo: Path,
    services: Services,
    settings: ForgeSettings,
    *,
    into: str,
    package: str | None,
    approved_spec: Spec | None,
    consent: Callable[[str], bool],
    stage_only: bool = False,
    package_only: bool = False,
    reuse_installed: bool = False,
    agent: AgentChoice | None = None,
) -> IntegrateResult | CompileResult:
    if stage_only and package_only:
        raise ForgeModeError("invalid_arguments")
    if not isinstance(request, str) or not request.strip():
        raise ForgeModeError("invalid_request")
    repo = await asyncio.to_thread(_repository, Path(repo))

    # Gate 1: approval. Only Stage 0 runs; no Forge line prints; $0 beyond Stage 0.
    if approved_spec is None:
        base = services.compiler
        proposed = await base.compile(
            request,
            overrides={"lifecycle": Lifecycle.INSTALL.value},
            **compile_kwargs(base.compile, origin="forge_integrate"),
        )
        if proposed.approval_required:
            await asyncio.to_thread(_write_proposal, proposed.spec, services.workspace)
        return proposed
    if approved_spec.lifecycle != Lifecycle.INSTALL:
        raise ForgeModeError("approved_feature_must_be_installed")
    name_of_package = package if package is not None else default_package(approved_spec.spec_hash)
    _check_name(name_of_package)
    denied = _denied(services)
    if not stage_only:
        # Refuse an invalid or taken destination before any spend (checked again later).
        try:
            destination = _target(repo, into, name_of_package, denied)
        except ExportError:
            raise
        except (OSError, ValueError, RuntimeError):
            raise ExportError("export_path_invalid") from None
        if destination.exists():
            raise ExportError("export_target_exists")
    if not stage_only and not package_only:
        # The Pi path's builder is gated by ``profile.builder_floor`` (FeatureBuilder); a
        # profile without one could never place, so refuse before any spend.
        if getattr(getattr(services, "profile", None), "builder_floor", None) is None:
            raise ForgeModeError("builder_floor_required")

    # Gate 2: Forge spend consent.
    compiler, armed = per_call_compiler(services, settings)
    if compiler.smoke_factory is None:
        raise ForgeModeError("forge_disabled")
    text, armed, choice = consent_text(services, compiler, armed, approved_spec, resolved=agent)
    if not consent(text):
        record_role(services, choice, "excluded")
        raise ForgeModeError("forge_cancelled")

    # Gate 3: a Stage 4 license, installed and re-loaded; metered for the cost split.
    with compilation_wallet(meter_budget(compiler, armed)) as meter:
        result = await compiler.compile(
            approved_spec,
            approved=True,
            **compile_kwargs(
                compiler.compile,
                origin="forge_integrate",
                reuse_installed=reuse_installed,
                budget_cap=approved_spec.budget,
            ),
        )
    route = route_of(result)
    codes: list[str] = []
    artifact = result.artifact
    provider = provider_of(services, artifact.model) if artifact is not None else None
    evidence = evidence_class_of(provider)
    build = 0.0
    if artifact is not None and not reused(result):
        build = float(forge_summary(artifact.directory).get("agent_cost_usd", 0.0))
    compile_cost = max(0.0, meter.cost_usd - build)

    def spent(name: str, revision: int) -> None:
        line = cost_line(name, revision, build, compile_cost, reused=reused(result))
        print(wrapped(line), file=sys.stderr, flush=True)

    def failed() -> ForgeModeError:
        print(
            wrapped(
                f"forge integrate: not licensed; build ${build:.4f} agent (API-equivalent), "
                f"compile ${compile_cost:.4f}, play $0.0000 (nothing is run)"
            ),
            file=sys.stderr,
            flush=True,
        )
        return ForgeModeError("feature_compile_failed")

    # P3: the explicit grant or the profile's standing ``forge_integrate`` grant.
    granted = reuse_installed or standing_grant(compiler, "forge_integrate")
    if reused(result):
        target = getattr(route, "target", None)
        name = getattr(target, "name", None)
        if not granted or not isinstance(name, str) or artifact is None:
            raise failed()
        codes.append("integrate_reused_installed")
        entry, installed = _reload(services, name, artifact, approved_spec)
        if (
            getattr(target, "artifact_digest", None) != installed.artifact_digest
            or getattr(target, "revision", None) != entry.revision
        ):
            raise failed()
        record_role(services, choice, "excluded")  # the agent never ran for this call
    else:
        licensed = artifact is not None and artifact.licensed and not result.failures
        record_outcome(
            services,
            route,
            "licensed" if licensed else "unlicensed",
            stage4_passed=licensed,
            evidence_class=evidence,
        )
        forged = artifact is not None and bool(forge_summary(artifact.directory))
        record_role(services, choice, "success" if licensed and forged else "failure")
        if not licensed or artifact is None:
            raise failed()
        name, expected = _install(services, artifact)
        entry, installed = _reload(services, name, expected, approved_spec)
    spent(entry.name, entry.revision)
    ref = InstalledRef(
        name=entry.name,
        revision=entry.revision,
        artifact_digest=installed.artifact_digest,
        spec_hash=installed.spec.spec_hash,
    )

    # Export to host-owned staging (Pi-denied ``workspace/exports``).
    manifest, manifest_sha256 = await asyncio.to_thread(
        export_package,
        entry,
        services.workspace / "exports",
        name_of_package,
        model_class=evidence_class_of(provider_of(services, installed.model)),
        floor_origin="user_dataset",
    )
    staged = services.workspace / "exports" / manifest.artifact_digest[:16] / manifest.package
    files = len(manifest.files) + 1
    size = await asyncio.to_thread(_size, staged, manifest)

    def outcome(placement: Placement, path: Path, extra: Iterable[str] = ()) -> IntegrateResult:
        return IntegrateResult(
            licensed=True,
            name=entry.name,
            revision=entry.revision,
            spec_hash=installed.spec.spec_hash,
            export={
                "package_path": str(path),
                "files": files,
                "bytes": size,
                "manifest_sha256": manifest_sha256,
                "evidence_label": evidence_label_of(installed),
                "placement": placement,
            },
            codes=fixed_codes([*codes, *extra]),
            build_cost_usd=round(build, 9),
            compile_cost_usd=round(compile_cost, 9),
        )

    if stage_only:
        print(f"export staged: {staged}", file=sys.stderr, flush=True)
        return outcome("staged", staged)

    # Gate 4: export consent. A refusal keeps the entry and the staging directory.
    if not consent(export_line(manifest.package, files, size, into)):
        return outcome("staged", staged, ("export_cancelled",))

    placed_dir = repo / into / manifest.package
    placement: Placement
    if package_only:
        placement = "host"
        existed = placed_dir.exists() or _linked(placed_dir)
        try:
            await asyncio.to_thread(place_package, staged, repo, into, denied=denied)
        except ExportError as exc:
            # Only a failed re-hash of the copy it just made leaves this call's directory
            # in the repository. Any other refusal (a target that appeared during the
            # build, a path or copy failure) placed nothing: whatever is at the target is
            # not this package, so the result is the intact staging copy.
            if exc.code != "export_integrity_mismatch" or existed or not placed_dir.is_dir():
                return outcome("staged", staged, (exc.code,))
            codes.append(exc.code)
    else:
        from architect.orchestrator import OrchestrationError
        from architect.orchestrator.features import FeatureBuilder

        placement = "pi"
        options = IntegrateOptions(
            entry=ref,
            staged=staged,
            into=into,
            package=manifest.package,
            manifest_sha256=manifest_sha256,
        )
        try:
            orchestration = await FeatureBuilder(services).create(
                request, repo, approved_spec=approved_spec, integrate=options
            )
            codes.extend(orchestration.failures)
        except OrchestrationError as exc:
            # The build is paid for and installed: report the refusal, do not lose it.
            codes.append(str(exc))
        if not placed_dir.exists() and not _linked(placed_dir):
            # The builder never called place_harness_package (or it refused): nothing
            # is in the repository, so this is "not placed", not a tampered package.
            return outcome("staged", staged)

    # Post-placement verification: re-hash every file against the consented manifest;
    # report, never delete.
    codes.extend(
        await asyncio.to_thread(placement_codes, placed_dir, manifest_sha256, services.registry)
    )
    return outcome(placement, placed_dir)
