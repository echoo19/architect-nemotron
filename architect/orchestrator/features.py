"""A compiled builder requests a feature agent through a bounded trusted tool.

The builder never supplies evaluation labels or approval. The tool is unavailable
while candidates are evaluated. The user reviews an installed specification at a
separate checkpoint; only that exact approved specification can be compiled.

Phase 10 (``forge integrate``, Section 12.2): with ``integrate`` set, compile_agent
does not compile. It re-loads the already installed, licensed entry the options name
(the ``load_reuse_target`` checks) and the single-use ``place_harness_package`` tool
places the host-staged package; the feature stays behind its disabled flag.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import JsonValue

from architect.backends.pi import PiBackend
from architect.orchestrator import (
    OrchestrationError,
    OrchestrationResult,
    TaskResult,
    _nested,
)
from architect.registry import RegistryError
from architect.spec import HarnessArtifact, Lifecycle, Spec
from architect.spec.binding import check_subharness_tree

if TYPE_CHECKING:
    from architect.compiler import CompileResult
    from architect.forge_modes.integrate import IntegrateOptions
    from architect.profiles import Services


def _integrated_feature(
    services: Services, options: IntegrateOptions, approved: Spec
) -> dict[str, JsonValue]:
    """The installed entry of ``options``, re-loaded as ``load_reuse_target`` would.

    Licensed on a fresh load, child-checked, the referenced digest and spec_hash, the
    approved spec_hash, INSTALL lifecycle and not consumed; otherwise nothing is placed.
    """
    ref = options.entry
    try:
        artifact: HarnessArtifact = services.registry.get(ref.name, ref.revision)
        check_subharness_tree(artifact)
    except (RegistryError, OSError, ValueError, TypeError):
        return {"error": "feature_compile_failed"}
    if (
        not artifact.licensed
        or artifact.artifact_digest != ref.artifact_digest
        or artifact.spec.spec_hash != ref.spec_hash
        or artifact.spec.spec_hash != approved.spec_hash
        or artifact.spec.lifecycle != Lifecycle.INSTALL
        or (artifact.directory / ".consumed").exists()
    ):
        return {"error": "feature_compile_failed"}
    return {
        "licensed": True,
        "name": ref.name,
        "revision": ref.revision,
        "spec_hash": artifact.spec.spec_hash,
        "artifact_directory": str(artifact.directory),
        "human_review_required": True,
        "feature_flag_required": True,
        "code": "integrate_reused_installed",
    }


def _reused_feature(services: Services, compiled: CompileResult) -> dict[str, JsonValue]:
    """The installed entry a routed reuse returned (``load_reuse_target`` checks again)."""
    from architect.routing.router import InstalledRef, RouteError, load_reuse_target

    decision = compiled.route
    target = decision.target if decision is not None else None
    if decision is None or not isinstance(target, InstalledRef):
        return {"error": "feature_compile_failed"}
    try:
        artifact = load_reuse_target(decision, services.registry, compiled.spec)
    except (RouteError, OSError, ValueError, TypeError):
        return {"error": "feature_compile_failed"}
    if artifact.spec.lifecycle != Lifecycle.INSTALL:
        return {"error": "feature_compile_failed"}
    return {
        "licensed": True,
        "name": target.name,
        "revision": target.revision,
        "spec_hash": artifact.spec.spec_hash,
        "artifact_directory": str(artifact.directory),
        "human_review_required": True,
        "feature_flag_required": True,
        "code": "route_reuse_installed",
    }


def _builder_origin(compile_method: object) -> dict[str, Any]:
    """Phase 10: the builder's compiles route with origin ``builder`` (advice and warm
    starts only; reuse needs a profile grant). A compiler without the keyword gets none."""
    try:
        parameters = inspect.signature(compile_method).parameters  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return {}
    if "origin" in parameters or any(
        item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values()
    ):
        return {"origin": "builder"}
    return {}


class FeatureBuilder:
    def __init__(self, services: Services) -> None:
        self.services = services

    async def create(
        self,
        request: str,
        repo: Path,
        *,
        approved_spec: Spec | None = None,
        integrate: IntegrateOptions | None = None,
    ) -> OrchestrationResult:
        services = self.services
        profile = services.profile
        if not request.strip() or profile.builder_floor is None:
            raise OrchestrationError("builder_floor_required")
        if approved_spec is not None and approved_spec.lifecycle != Lifecycle.INSTALL:
            raise OrchestrationError("approved_feature_must_be_installed")
        if integrate is not None:
            from architect.forge_modes.errors import ExportError
            from architect.forge_modes.export import read_manifest

            if approved_spec is None:
                raise OrchestrationError("approval_required")
            try:
                staged, digest = await asyncio.to_thread(read_manifest, integrate.staged)
            except ExportError:
                raise OrchestrationError("export_integrity_mismatch") from None
            if digest != integrate.manifest_sha256 or staged.package != integrate.package:
                raise OrchestrationError("export_integrity_mismatch")
        calls = 0
        feature: dict[str, JsonValue] | None = None
        feature_accepted = False

        active = True
        pending: set[asyncio.Task[object]] = set()

        async def compile_agent(payload: dict[str, JsonValue]) -> JsonValue:
            if not active:
                return {"error": "factory_capability_expired"}
            owner = asyncio.current_task()
            assert owner is not None
            pending.add(owner)
            try:
                return await compile_once(payload)
            finally:
                pending.discard(owner)

        async def compile_once(payload: dict[str, JsonValue]) -> JsonValue:
            nonlocal calls, feature, feature_accepted
            calls += 1
            if calls > 1:
                return {"error": "factory_call_limit"}
            if set(payload) != {"request"} or payload.get("request") != request:
                feature = {"error": "factory_request_mismatch"}
                return feature
            with _nested() as depth:
                if depth > services.orchestrator.config.max_depth:
                    feature = {"error": "factory_depth_limit"}
                    return feature
                if integrate is not None:
                    assert approved_spec is not None
                    feature = await asyncio.to_thread(
                        _integrated_feature, services, integrate, approved_spec
                    )
                    feature_accepted = "error" not in feature
                    return feature
                if approved_spec is None:
                    compiled = await services.compiler.compile(
                        request,
                        overrides={"lifecycle": "install"},
                        **_builder_origin(services.compiler.compile),
                    )
                else:
                    compiled = await services.compiler.compile(
                        approved_spec, approved=True, **_builder_origin(services.compiler.compile)
                    )
                if compiled.approval_required:
                    directory = services.workspace / "proposals"
                    await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
                    path = directory / (compiled.spec.spec_hash + ".json")
                    await asyncio.to_thread(
                        path.write_text,
                        compiled.spec.model_dump_json(indent=2),
                        encoding="utf-8",
                    )
                    feature = {
                        "approval_required": True,
                        "spec_hash": compiled.spec.spec_hash,
                        "spec": compiled.spec.model_dump(mode="json"),
                        "proposal_path": str(path),
                    }
                    return feature
                artifact = compiled.artifact
                if artifact is None or not artifact.licensed:
                    feature = {"error": "feature_compile_failed"}
                    return feature
                if getattr(compiled, "reused", False) is True:
                    # Phase 10 Section 10.2: a routed reuse is already an installed,
                    # licensed entry and no Stage 4 ran in this call, so it is never
                    # installed again; the feature is that entry, re-loaded from disk.
                    feature = await asyncio.to_thread(_reused_feature, services, compiled)
                    feature_accepted = "error" not in feature
                    return feature
                name = "feature-" + artifact.spec.spec_hash[:16]
                try:
                    entry = services.registry.install(name, artifact)
                except RegistryError as exc:
                    if str(exc) != "not_better":
                        raise
                    existing = services.registry.get(name)
                    if not existing.licensed or existing.spec.spec_hash != artifact.spec.spec_hash:
                        raise OrchestrationError("feature_install_failed") from None
                    entry = services.registry.status(name)
                feature_accepted = True
                feature = {
                    "licensed": True,
                    "name": name,
                    "revision": entry.revision,
                    "spec_hash": artifact.spec.spec_hash,
                    "artifact_directory": str(entry.directory),
                    "human_review_required": True,
                    "feature_flag_required": True,
                }
                return feature

        tools = tuple(dict.fromkeys((*profile.pi.allowed_tools, "compile_agent")))
        if integrate is not None:
            tools = (*tools, "place_harness_package")
        config = profile.pi.model_copy(update={"allowed_tools": tools})
        if integrate is None:
            backend = PiBackend(config, compiler_tool=compile_agent)
        else:
            from architect.forge_modes.place_tool import place_package_tool

            place = place_package_tool(
                integrate.staged,
                repo,
                integrate.into,
                ready=lambda: feature_accepted,
                active=lambda: active,
                denied=profile.pi.denied_paths,
            )
            backend = PiBackend(
                config,
                compiler_tool=compile_agent,
                capabilities={"place_harness_package": place},
            )
        goal = (
            "Create and integrate an installed AI feature. Use compile_agent exactly once. "
            "Use the exact request below as its request argument. Do not write your own agent loop "
            "or claim licensing without that tool's success. If approval is required, stop and "
            "report the checkpoint. Keep application delivery behind a disabled feature flag "
            "and require human review. Feature request: " + json.dumps(request)
        )
        if integrate is not None:
            goal += (
                " After compile_agent succeeds, call place_harness_package exactly once with "
                "into equal to "
                + json.dumps(integrate.into)
                + ", then write only call-site glue that checks the package's enabled() flag."
            )
        try:
            with services.runtime.use_pi_backend(backend):
                result = await services.orchestrator.run(
                    goal,
                    repo=repo,
                    overrides={
                        "domain": "coder",
                        "tools": list(tools),
                        "model_target": profile.builder_model or next(iter(profile.models)),
                        "input_schema": {"type": "object"},
                        "output_schema": {"type": "string"},
                        "floor": profile.builder_floor.model_dump(mode="json"),
                    },
                )
        finally:
            active = False
            unfinished = tuple(task for task in pending if task is not asyncio.current_task())
            for pending_task in unfinished:
                pending_task.cancel()
            if unfinished:
                await asyncio.gather(*unfinished, return_exceptions=True)
        if feature is None:
            failure = "factory_not_called"
        elif feature.get("approval_required"):
            failure = "approval_required"
        elif not feature_accepted:
            failure = "feature_compile_failed"
        else:
            failure = ""
        task = TaskResult(
            id="feature_factory",
            accepted=feature_accepted,
            output=feature,
            spec_hash=str(feature["spec_hash"]) if feature and "spec_hash" in feature else None,
            failures=(failure,) if failure else (),
        )
        return OrchestrationResult(
            accepted=result.accepted and feature_accepted,
            tasks=(*result.tasks, task),
            failures=tuple(dict.fromkeys((*result.failures, *((failure,) if failure else ())))),
        )
