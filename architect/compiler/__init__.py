"""The agent compiler. Model judgments never grant execution authority."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import JsonValue

from architect.floors import FloorError
from architect.models import ModelAdapter
from architect.runtime import BudgetExceeded, Runtime
from architect.runtime.wallet import compilation_wallet
from architect.spec import Budget, HarnessArtifact, Lifecycle, Spec

from .types import (
    AdapterFactory,
    CompileConfig,
    CompileResult,
    FloorFactory,
    LibraryLifecycle,
    LibraryLookup,
    Measurement,
    RungOutcome,
)

if TYPE_CHECKING:
    from architect.backends.pi import PiConfig
    from architect.floors import FloorEvaluator
    from architect.presets import Preset
    from architect.routing.log import Origin
    from architect.routing.roles import RoleAdapters, RoleChoice, RoleName
    from architect.routing.router import RouteDecision, Router

    from .forge import ForgeResult
    from .inference import Environment, Subtask
    from .types import CandidateEvaluation

__all__ = [
    "Compiler",
    "CompileConfig",
    "CompileResult",
    "RungOutcome",
    "add_stage_observer",
    "recompiling",
    "remove_stage_observer",
]

_CHEAPEST = "cheapest that passes"
# Phase 10 Section 10.2: the per-call library of a routed warm start. A context
# variable, not an attribute, so concurrent compiles on one Compiler (the HTTP
# server) never see each other's chosen template.
_CALL_LIBRARY: ContextVar[LibraryLookup | None] = ContextVar(
    "architect_routed_library", default=None
)
# Phase 10 Section 8.5: a recompile is a fresh Stage 4 attempt and never reuses. The
# session's single approval expression stays exactly as written, so it marks its task
# with ``recompiling()`` rather than a keyword; the next ``compile`` consumes the mark.
_CALL_NO_REUSE: ContextVar[bool] = ContextVar("architect_recompiling", default=False)


# Stage observers (the desktop's live Forge panel): process-wide, called from
# ``Compiler._enter`` next to ``stage_listener`` with a small JSON dict
# ``{"stage", "stages", "depth"}`` plus ``model``/``model_index``/``model_count`` and the
# whole ``ladder`` inside the ladder loop. From Stage 3 on a rung also carries its
# practice scores: ``tuned`` (Stage 2's best preset) and, entering Stage 4, ``examined``
# (what sits the exam), each ``{"score", "passed"}`` from the tuning partition. No-ops
# unless something subscribes; an observer can never break a compile. Nothing here comes
# from a floor's held-out partition: Stage 4's results reach the caller only in the
# ``CompileResult`` (``rungs``), after ``compile()`` returns.
_STAGE_OBSERVERS: list[Callable[[dict[str, Any]], None]] = []


def add_stage_observer(observer: Callable[[dict[str, Any]], None]) -> None:
    if observer not in _STAGE_OBSERVERS:
        _STAGE_OBSERVERS.append(observer)


def remove_stage_observer(observer: Callable[[dict[str, Any]], None]) -> None:
    if observer in _STAGE_OBSERVERS:
        _STAGE_OBSERVERS.remove(observer)


@contextmanager
def recompiling() -> Iterator[None]:
    """Compiles started in this block (including tasks created in it) never reuse an
    installed entry, whatever the grant; they still route, so they may warm-start."""
    token = _CALL_NO_REUSE.set(True)
    try:
        yield
    finally:
        _CALL_NO_REUSE.reset(token)


class Compiler:
    # Set per compile by an observer (the interactive session) to learn which
    # stage is running. A class attribute so existing constructors are unchanged.
    stage_listener: Callable[[int], None] | None = None
    # Set by an observer (the bench's Forge tally) to receive each Stage 3 session's
    # content-free ``ForgeResult``; copied onto a game factory's inner compiler.
    forge_listener: Callable[[ForgeResult], None] | None = None
    # Forge nesting depth of this compiler (0 for a top-level compile); a forge_spawn
    # child compiler is created at its parent's depth plus one (Section 11.3).
    _forge_depth: int = 0

    def __init__(
        self,
        artifact_root: Path,
        runtime: Runtime,
        adapter_factory: AdapterFactory,
        floor_factory: FloorFactory,
        *,
        config: CompileConfig | None = None,
        library: LibraryLookup | None = None,
        inference_adapter: ModelAdapter | None = None,
        rewrite_adapter: ModelAdapter | None = None,
        smoke_factory: FloorFactory | None = None,
        forge_adapter: ModelAdapter | None = None,
        pi_config: PiConfig | None = None,
        router: Router | None = None,
        roles: RoleAdapters | None = None,
    ) -> None:
        self.artifact_root = artifact_root
        self.runtime = runtime
        self.adapter_factory = adapter_factory
        if runtime.adapter_factory is None:
            runtime.adapter_factory = adapter_factory
        self.floor_factory = floor_factory
        self.config = config or CompileConfig.model_validate_json(
            Path(__file__).with_name("config.json").read_text(encoding="utf-8")
        )
        if library is None:
            from architect.library import HarnessLibrary

            library = HarnessLibrary(artifact_root.parent / "library")
        self.library = library
        self.inference_adapter = inference_adapter
        self.rewrite_adapter = rewrite_adapter
        self.smoke_factory = smoke_factory
        self.forge_adapter = forge_adapter
        self.pi_config = pi_config
        # Phase 10: the task router (None: today's behaviour exactly) and the role seam
        # (consulted only under ``roles.policy == "evidence"``; pinned is today's binding).
        self.router = router
        self.roles = roles
        # True: every compile routes. ``build_services`` sets it from the profile, so a
        # profile without a ``routing`` table compiles exactly as before Phase 10 and
        # routes only when the operator grants installed reuse for that call.
        self.route_all = True

    def _library(self) -> LibraryLookup:
        """This call's library: a routed warm start's ``RoutedLibrary``, else the shared one."""
        routed = _CALL_LIBRARY.get()
        return routed if routed is not None else self.library

    def _role(
        self, role: RoleName, domain: str | None, default: ModelAdapter | None
    ) -> tuple[ModelAdapter | None, RoleChoice | None]:
        """Section 11.3: the pinned default unless ``roles.policy == "evidence"`` (fail-open)."""
        roles = self.roles
        if roles is None or roles.policy != "evidence":
            return default, None
        try:
            choice = roles.choose(role, domain=domain)
        except Exception:
            return default, None  # P5: role routing never blocks a compile.
        return (choice.adapter if choice.adapter is not None else default), choice

    def _role_record(self, choice: RoleChoice | None, outcome: Any) -> None:
        if choice is None or self.roles is None:
            return
        try:
            self.roles.record(choice, outcome)
        except Exception:
            return

    def _ladder(self, spec: Spec) -> tuple[str, ...]:
        return self.config.model_ladder if spec.model_target == _CHEAPEST else (spec.model_target,)

    async def _route(
        self,
        spec: Spec,
        origin: Origin,
        grant: bool,
        budget_cap: Budget | None,
        *,
        forbid_reuse: bool = False,
    ) -> RouteDecision | None:
        """Section 10.2: route one probe; any routing failure is no decision (P5)."""
        router = self.router
        ladder = self._ladder(spec)
        if router is None or not ladder:
            return None
        from architect.routing.router import TaskProbe

        try:
            probe = TaskProbe(
                origin=origin,
                spec=spec,
                budget_cap=budget_cap if budget_cap is not None else spec.budget,
                ladder=ladder,
                grant_reuse=grant and not forbid_reuse,
                forbid_reuse=forbid_reuse,
            )
            return await asyncio.to_thread(router.route, probe)
        except Exception:
            return None

    def _subtask_model(self, spec: Spec) -> Spec:
        """Section 11.5 (``routing.subtask_models``): the best ladder model for which an
        eligible Tier A or B version exists, ranked by ``(-m_lcb, cost)``; else unchanged.

        Pure reads through ``route`` (nothing is logged); any failure keeps the Spec (P5).
        """
        router = self.router
        if router is None or not router.policy.subtask_models:
            return spec
        try:
            from architect.routing.router import TaskProbe, route

            best: tuple[float, float, int] | None = None
            chosen = spec
            for order, model in enumerate(self.config.model_ladder):
                candidate = Spec.model_validate(
                    {**spec.model_dump(mode="json"), "model_target": model}
                )
                probe = TaskProbe(
                    origin="orchestrator", spec=candidate, budget_cap=spec.budget, ladder=(model,)
                )
                decision = route(probe, index=router.index, policy=router.policy)
                for view in decision.candidates:
                    if not view.eligible or view.tier not in ("A", "B"):
                        continue
                    key = (
                        -view.m_lcb if view.m_lcb is not None else float("inf"),
                        view.cost_usd if view.cost_usd is not None else float("inf"),
                        order,
                    )
                    if best is None or key < best:
                        best, chosen = key, candidate
            return chosen
        except Exception:
            return spec

    async def _reuse(
        self, spec: Spec, stages: list[int], decision: RouteDecision
    ) -> CompileResult:
        """Execute a granted ``reuse_installed``: re-load and re-check, no Stage 1 to 4."""
        from architect.routing.router import RouteError, load_reuse_target

        registry = getattr(getattr(self.router, "index", None), "registry", None)
        if registry is None:
            return CompileResult(
                spec=spec, stages=tuple(stages), failures=("route_no_reuse",), route=decision
            )
        try:
            artifact = await asyncio.to_thread(load_reuse_target, decision, registry, spec)
        except RouteError as exc:
            return CompileResult(
                spec=spec, stages=tuple(stages), failures=(exc.code,), route=decision
            )
        return CompileResult(
            spec=spec,
            artifact=artifact,
            report=artifact.report,
            stages=tuple(stages),
            reused=True,
            route=decision,
        )

    def _outcome(
        self, decision: RouteDecision | None, result: CompileResult, cost_usd: float
    ) -> None:
        """After Stage 4: one content-free ``OutcomeRecord`` (fail-open)."""
        router = self.router
        if router is None or decision is None or 4 not in result.stages:
            return
        try:
            from architect.routing.log import OutcomeRecord

            artifact = result.artifact
            licensed = artifact is not None and artifact.licensed and not result.failures
            evidence = None
            if artifact is not None:
                evidence = router.index.evidence_class(artifact.model)
            router.outcome(
                decision,
                OutcomeRecord(
                    decision_id=decision.decision_id,
                    timestamp=datetime.now(UTC),
                    outcome="licensed" if licensed else "unlicensed",
                    stage4_passed=licensed,
                    evidence_class=evidence,
                    compile_cost_usd=max(0.0, cost_usd),
                ),
            )
        except Exception:
            return

    def _outer_budget(self) -> Budget:
        """The compile wallet: raised by the Forge budget only when Forge is enabled.

        The summed cap is shown to the operator for consent before any spend
        (Section 12); Forge's own nested wallet still caps the session itself.
        """
        budget = self.config.compile_budget
        forge = self.config.forge
        if not forge.enabled:
            return budget
        return Budget(
            cost_usd=budget.cost_usd + forge.max_usd,
            compute_s=budget.compute_s + forge.max_s,
        )

    def _enter(
        self,
        stages: list[int],
        stage: int,
        *,
        model: str | None = None,
        model_index: int | None = None,
        model_count: int | None = None,
        ladder: tuple[str, ...] | None = None,
        tuned: Measurement | None = None,
        examined: Measurement | None = None,
    ) -> None:
        """Record a stage once and tell the listener; an observer can never break compilation.

        ``tuned`` and ``examined`` are tuning-partition measurements (Stage 2's best preset,
        and what sits the exam); observers get only their score and whether they passed.
        """
        if stage not in stages:
            stages.append(stage)
        listener = self.stage_listener
        if listener is not None:
            try:
                listener(stage)
            except Exception:
                pass
        if _STAGE_OBSERVERS:
            data: dict[str, Any] = {
                "stage": stage,
                "stages": list(stages),
                "depth": self._forge_depth,
            }
            if model is not None:
                data.update(model=model, model_index=model_index, model_count=model_count)
            if ladder is not None:
                data["ladder"] = list(ladder)
            if tuned is not None:
                data["tuned"] = _practice(tuned)
            if examined is not None:
                data["examined"] = _practice(examined)
            for observer in tuple(_STAGE_OBSERVERS):
                try:
                    observer(copy.deepcopy(data))
                except Exception:
                    pass

    async def _forge(
        self,
        best: CandidateEvaluation,
        preset: Preset,
        floor: FloorEvaluator,
        smoke_floor: FloorEvaluator,
    ) -> ForgeResult:
        """Stage 3 as a Forge session; ``verify()`` still runs afterwards, exactly once."""
        from architect.backends.pi import PiConfig

        from .forge import forge
        from .forge_tools import ALLOWED_PRESETS, CHILD_LIBRARY, notify_result_observers

        settings = self.config.forge
        notify_result_observers({
            "tool": "forge_session",
            "event": "started",
            "depth": self._forge_depth,
            "result": {
                "mode": settings.mode,
                "baseline": {
                    "score": best.measurement.score,
                    "floor_passed": best.measurement.floor_passed,
                    "mean_cost_usd": best.measurement.mean_cost_usd,
                },
                "caps": {
                    "usd": settings.max_usd,
                    "seconds": settings.max_s,
                    "checks": settings.max_checks,
                    "dryruns": settings.max_dryruns,
                    "measures": settings.max_measures,
                    "submissions": settings.max_submissions,
                    "spawns": settings.max_spawns,
                },
            },
        })
        token = ALLOWED_PRESETS.set(tuple(self.config.allowed_presets))
        child = CHILD_LIBRARY.set(self._child_library())
        try:
            result = await forge(
                best,
                preset,
                floor,
                smoke_floor,
                self.adapter_factory,
                self.runtime,
                self.artifact_root,
                self.forge_adapter or self.rewrite_adapter,
                self.config.forge,
                pi_config=self.pi_config or PiConfig(),
                floor_factory=self.floor_factory,
                library=self._library(),
                depth=self._forge_depth,
            )
        finally:
            CHILD_LIBRARY.reset(child)
            ALLOWED_PRESETS.reset(token)
        listener = self.forge_listener
        if listener is not None:
            try:
                listener(result)
            except Exception:
                pass  # An observer can never break compilation.
        measurement = result.best.measurement
        notify_result_observers({
            "tool": "forge_session",
            "event": "finished",
            "depth": self._forge_depth,
            "result": {
                **result.model_dump(mode="json", exclude={"best"}),
                "best": {
                    "score": measurement.score,
                    "floor_passed": measurement.floor_passed,
                    "mean_cost_usd": measurement.mean_cost_usd,
                    "sample_count": measurement.sample_count,
                },
            },
        })
        return result

    def _child_library(self) -> LibraryLookup | None:
        """Section 11.6: ``ReadOnlyLibrary(parent)`` for Forge children, only when
        ``routing.child_warm_start``; None keeps the Phase 9 ``NullLibrary``."""
        router = self.router
        if router is None or not router.policy.child_warm_start:
            return None
        from architect.library import HarnessLibrary, ReadOnlyLibrary

        library = self.library
        return ReadOnlyLibrary(library) if isinstance(library, HarnessLibrary) else None

    async def compile(
        self,
        source: Spec | str | Environment | Subtask,
        *,
        overrides: dict[str, JsonValue] | None = None,
        approved: bool = False,
        exact_overrides: bool = False,
        origin: Origin = "cli",
        reuse_installed: bool = False,
        budget_cap: Budget | None = None,
        no_route: bool = False,
        allow_reuse: bool = True,
    ) -> CompileResult:
        """Compile ``source``.

        With a router (and not ``no_route``) the Spec is routed after Stage 0 and before
        the INSTALL approval check (Phase 10 Section 10.2): a granted ``reuse_installed``
        returns the re-checked installed entry with no Stage 1 to 4, ``warm_start`` seeds
        Stage 1 from the chosen template, and everything else compiles exactly as without
        a router. Routing never licenses; Stage 4 stays the only authority.

        ``allow_reuse=False`` (or a ``recompiling()`` block) is a recompile: a fresh
        Stage 4 attempt that never reuses an installed entry, neither by
        ``reuse_installed`` nor by a standing grant in ``routing.reuse_origins``; it
        still routes, so it may warm-start. Nested compiles (Forge children) are not
        marked: the mark applies to this compile only.
        """
        forbid_reuse = not allow_reuse or _CALL_NO_REUSE.get()
        mark = _CALL_NO_REUSE.set(False)
        try:
            return await self._compile_call(
                source,
                overrides=overrides,
                approved=approved,
                exact_overrides=exact_overrides,
                origin=origin,
                reuse_installed=reuse_installed,
                budget_cap=budget_cap,
                no_route=no_route,
                forbid_reuse=forbid_reuse,
            )
        finally:
            _CALL_NO_REUSE.reset(mark)

    async def _compile_call(
        self,
        source: Spec | str | Environment | Subtask,
        *,
        overrides: dict[str, JsonValue] | None,
        approved: bool,
        exact_overrides: bool,
        origin: Origin,
        reuse_installed: bool,
        budget_cap: Budget | None,
        no_route: bool,
        forbid_reuse: bool,
    ) -> CompileResult:
        from .inference import infer_spec

        stages: list[int] = []
        decision: RouteDecision | None = None
        inference_choice: RoleChoice | None = None
        spec: Spec | None = source if isinstance(source, Spec) else None
        with compilation_wallet(self._outer_budget()) as wallet:
            try:
                async with asyncio.timeout(wallet.remaining_s):
                    if isinstance(source, Spec):
                        if overrides:
                            payload = source.model_dump(mode="json")
                            if exact_overrides:
                                payload.update(json.loads(json.dumps(overrides)))
                            else:
                                _merge(payload, overrides)
                            provenance = dict(source.provenance)
                            provenance.update(
                                {key: "user" for key in overrides if key != "provenance"}
                            )
                            payload["provenance"] = provenance
                            spec = Spec.model_validate(payload)
                    else:
                        inference, inference_choice = self._role(
                            "inference", None, self.inference_adapter
                        )
                        inferred = await infer_spec(
                            source,
                            inference or self.adapter_factory("fake"),
                            self.runtime,
                            overrides,
                            exact_overrides=exact_overrides,
                        )
                        spec = inferred.spec
                        self._enter(stages, 0)
                    assert spec is not None
                    routed = not no_route and (self.route_all or reuse_installed)
                    if routed and origin == "orchestrator" and 0 in stages:
                        spec = await asyncio.to_thread(self._subtask_model, spec)
                    if routed:
                        decision = await self._route(
                            spec,
                            origin,
                            reuse_installed,
                            budget_cap,
                            forbid_reuse=forbid_reuse,
                        )
                    if spec.lifecycle == Lifecycle.INSTALL and not approved:
                        self._role_record(inference_choice, "excluded")
                        return CompileResult(
                            spec=spec,
                            approval_required=True,
                            stages=tuple(stages),
                            route=decision,
                        )
                    if decision is not None and decision.action == "reuse_installed":
                        self._role_record(inference_choice, "excluded")
                        return await self._reuse(spec, stages, decision)
                    result = await self._routed_compile(spec, stages, decision)
                    # ``forge integrate`` records its own licensed/unlicensed outcome.
                    if origin != "forge_integrate":
                        self._outcome(decision, result, wallet.cost_usd)
                    if 4 in result.stages:
                        licensed = result.artifact is not None and result.artifact.licensed
                        self._role_record(
                            inference_choice,
                            "success" if licensed and not result.failures else "failure",
                        )
                    if (
                        wallet.exhausted
                        and (result.artifact is None or not result.artifact.licensed)
                        and "compile_budget" not in result.failures
                    ):
                        # Stages fold this wallet's refusals into per-case
                        # failures; like the deadline, they are budget, not quality.
                        result = result.model_copy(
                            update={"failures": (*result.failures, "compile_budget")}
                        )
                    return result
            except (BudgetExceeded, TimeoutError):
                if spec is None:
                    raise BudgetExceeded("Inference exhausted compilation budget") from None
                return CompileResult(
                    spec=spec, stages=tuple(stages), failures=("compile_budget",), route=decision
                )

    async def _routed_compile(
        self, spec: Spec, stages: list[int], decision: RouteDecision | None
    ) -> CompileResult:
        """``warm_start`` compiles through a per-call ``RoutedLibrary``; ``compile`` and
        ``refuse`` keep the library unchanged (``refuse`` appends its route code to a
        failure). Without a decision this is exactly ``_compile_spec``."""
        if decision is None:
            return await self._compile_spec(spec, stages)
        from architect.routing.router import RoutedLibrary, TemplateRef

        routed: LibraryLookup | None = None
        if decision.action == "warm_start" and isinstance(decision.target, TemplateRef):
            routed = RoutedLibrary(decision.target, fallback=self.library)
        token = _CALL_LIBRARY.set(routed)
        try:
            result = await self._compile_spec(spec, stages)
        finally:
            _CALL_LIBRARY.reset(token)
        update: dict[str, object] = {"route": decision}
        licensed = result.artifact is not None and result.artifact.licensed
        if decision.action == "refuse" and (result.failures or not licensed):
            codes = tuple(code for code in decision.codes if code == "route_no_candidate")
            update["failures"] = tuple(dict.fromkeys((*result.failures, *codes)))
        return result.model_copy(update=update)

    async def _compile_spec(self, spec: Spec, stages: list[int]) -> CompileResult:
        # Import stages lazily to avoid a cycle with the shared trusted floor runner.
        from architect._authority import seal_ready
        from architect.presets import get_preset
        from architect.trust import TrustError

        from .selection import SelectionError, select_preset
        from .tuning import GridSearch, SuccessiveHalving, tune
        from .verification import verify

        models = (
            self.config.model_ladder
            if spec.model_target == "cheapest that passes"
            else (spec.model_target,)
        )
        if not models:
            return CompileResult(spec=spec, failures=("empty_model_ladder",), stages=tuple(stages))
        try:
            seal_ready()  # W7: an unusable Ed25519 key fails before any spend
        except TrustError as exc:
            return CompileResult(spec=spec, failures=(exc.code,), stages=tuple(stages))
        try:
            floor = self.floor_factory(spec.floor)
            secondary = self.floor_factory(spec.secondary_floor) if spec.secondary_floor else None
            if floor.definition != spec.floor or (
                secondary is not None and secondary.definition != spec.secondary_floor
            ):
                raise FloorError("Floor factory returned a different floor")
        except (FloorError, OSError, ValueError):
            return CompileResult(spec=spec, failures=("floor_unavailable",), stages=tuple(stages))
        library = self._library()
        rewrite_adapter, rewrite_choice = self._role("rewrite", spec.domain, self.rewrite_adapter)
        max_rewrites = self.config.max_rewrites
        if rewrite_choice is not None:
            chosen = rewrite_choice.variant.params.get("max_rewrites")
            if type(chosen) is int and chosen >= 0:
                max_rewrites = chosen
        rewrite_outcome: str | None = None
        artifacts: list[HarnessArtifact] = []
        # Each rung's end, in ladder order: its verified artifact, or the code it stopped on.
        ends: list[tuple[str, HarnessArtifact | str]] = []
        warmed = False
        failures: list[str] = []
        for index, model in enumerate(models):
            where: dict[str, Any] = {
                "model": model,
                "model_index": index + 1,
                "model_count": len(models),
                "ladder": tuple(models),
            }
            self._enter(stages, 1, **where)
            try:
                selection = select_preset(spec, model, library)
                if (
                    self.config.allowed_presets
                    and selection.preset not in self.config.allowed_presets
                ):
                    raise SelectionError("Preset excluded by host policy")
            except SelectionError:
                failures.append("selection_failed")
                ends.append((model, "selection_failed"))
                continue
            self._enter(stages, 2, **where)
            try:
                result = await tune(
                    spec,
                    get_preset(selection.preset),
                    model,
                    floor,
                    self.adapter_factory,
                    self.runtime,
                    self.artifact_root,
                    strategy=GridSearch()
                    if self.config.strategy == "grid"
                    else SuccessiveHalving(),
                    warm_start=selection.warm_start,
                    max_candidates=self.config.candidates_for(index),
                )
            except ValueError:
                failures.append("tuning_configuration_failed")
                ends.append((model, "tuning_configuration_failed"))
                continue
            warmed = warmed or result.warm_started
            if result.best is None:
                failures.append("no_candidates")
                ends.append((model, "no_candidates"))
                continue
            best = result.best
            tuned = best.measurement
            forge_settings = self.config.forge
            forge_rung = forge_settings.max_rungs is None or index < forge_settings.max_rungs
            if (
                forge_settings.enabled
                and forge_rung
                and self.smoke_factory is not None
                and (forge_settings.trigger == "always" or not result.passed)
            ):
                # Forge takes the Stage 3 slot; rewrite() and _cheaper are unchanged otherwise.
                self._enter(stages, 3, **where, tuned=tuned)
                try:
                    forged = await self._forge(
                        best, get_preset(selection.preset), floor, self.smoke_factory(spec.floor)
                    )
                    best = forged.best
                    failures.extend(forged.failures)
                except (ValueError, OSError):
                    failures.append("forge_failed")
            elif (
                not result.passed
                and self.config.enable_rewriting
                and max_rewrites > 0
                and rewrite_adapter is not None
                and self.smoke_factory is not None
            ):
                from .rewriting import rewrite

                self._enter(stages, 3, **where, tuned=tuned)
                try:
                    rewritten = await rewrite(
                        best,
                        get_preset(selection.preset),
                        floor,
                        self.smoke_factory(spec.floor),
                        self.adapter_factory,
                        self.runtime,
                        self.artifact_root,
                        rewrite_adapter,
                        max_iterations=max_rewrites,
                    )
                    best = rewritten.best
                    rewrite_outcome = "accepted" if rewritten.accepted else "failure"
                    if not rewritten.accepted:
                        failures.extend(rewritten.failures)
                        if rewritten.attempts >= max_rewrites:
                            failures.append("rewrite_cap")
                except (ValueError, OSError):
                    rewrite_outcome = "failure"
                    failures.append("rewrite_failed")
            self._enter(stages, 4, **where, tuned=tuned, examined=best.measurement)
            artifact = await verify(
                best.artifact,
                floor,
                self.adapter_factory,
                self.runtime,
                secondary_floor=secondary,
                tuning_evidence=best.measurement,
            )
            if isinstance(library, LibraryLifecycle):
                if selection.warm_start is not None:
                    library.record_verification(selection.warm_start, artifact.licensed)
                if artifact.licensed:
                    library.store(artifact)
            if rewrite_outcome is not None:
                # Section 11.4: an accepted proposal counts only when Stage 4 passed.
                success = rewrite_outcome == "accepted" and artifact.licensed
                self._role_record(rewrite_choice, "success" if success else "failure")
                rewrite_outcome = None
            artifacts.append(artifact)
            ends.append((model, artifact))
        licensed = [artifact for artifact in artifacts if artifact.licensed]
        if licensed:
            winner = min(licensed, key=_cost_key)
            return CompileResult(
                spec=spec,
                artifact=winner,
                report=winner.report,
                warm_started=warmed,
                stages=tuple(stages),
                rungs=_rungs(ends, winner),
            )
        if artifacts:
            failed = min(artifacts, key=_failure_key)
            return CompileResult(
                spec=spec,
                artifact=failed,
                report=failed.report,
                warm_started=warmed,
                stages=tuple(stages),
                failures=tuple(
                    dict.fromkeys(
                        [
                            *failures,
                            *(failed.report.failures if failed.report else ("unverified",)),
                        ]
                    )
                ),
                rungs=_rungs(ends, None),
            )
        return CompileResult(
            spec=spec,
            stages=tuple(stages),
            warm_started=warmed,
            failures=tuple(failures) or ("compilation_failed",),
            rungs=_rungs(ends, None),
        )


def _practice(measurement: Measurement) -> dict[str, Any]:
    """A tuning-partition measurement as a stage observer sees it: its score and verdict."""
    return {"score": measurement.score, "passed": measurement.floor_passed}


def _rungs(
    ends: list[tuple[str, HarnessArtifact | str]], winner: HarnessArtifact | None
) -> tuple[RungOutcome, ...]:
    """Each rung from its signed report (counts, the aggregate score and the mean cost
    of one exam case only)."""
    rungs: list[RungOutcome] = []
    for model, end in ends:
        if isinstance(end, str):
            rungs.append(RungOutcome(model=model, codes=(end,)))
            continue
        report = end.report
        if report is None:  # verify() always issues one; kept as the unverified code
            rungs.append(RungOutcome(model=model, preset=end.preset, codes=("unverified",)))
            continue
        rungs.append(
            RungOutcome(
                model=model,
                preset=end.preset,
                licensed=end.licensed,
                ships=end is winner,
                score=report.floor_score,
                limit=report.floor_limit,
                cases=report.held_out_count,
                counts=report.held_out_counts,
                codes=report.failures,
                mean_cost_usd=report.mean_cost_usd,
            )
        )
    return tuple(rungs)


def _cost_key(artifact: HarnessArtifact) -> tuple[float, int, int]:
    report = artifact.report
    assert report is not None
    expected_cost = report.mean_cost_usd if report.mean_cost_usd is not None else report.cost_usd
    return expected_cost, artifact.component_count, report.tokens


def _failure_key(artifact: HarnessArtifact) -> tuple[float, float, int]:
    report = artifact.report
    assert report is not None
    quality = (
        report.floor_score
        if report.floor_kind.value == "human_edit_rate"
        else 1 - report.floor_score
    )
    return quality, report.cost_usd, artifact.component_count


def _merge(base: dict[str, JsonValue], values: dict[str, JsonValue]) -> None:
    for key, value in values.items():
        if key == "provenance":
            continue
        previous = base.get(key)
        if isinstance(previous, dict) and isinstance(value, dict):
            _merge(previous, value)
        else:
            base[key] = json.loads(json.dumps(value))
