"""``forge run``: build an ephemeral harness, let Stage 4 license it, run it once, expire.

Phase 10 Section 12.1. Forge is armed only here, by the operator's explicit command
(P8): the per-call Compiler is a copy of ``services.compiler`` with Forge enabled and
``trigger="always"``, and lifecycle is forced to ``expire``. Stage 4 is the only
licensing authority: delivery needs an independently re-loaded licensed artifact
(``deliverable_ephemeral``), exactly one ``Runtime.run`` (which consumes it), and the
generated code is then discarded while ``report.json``, ``forge.json`` and
``subharnesses/`` stay as evidence. Build (agent), compile and play cost are reported
separately (Phase 9 Section 16).
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import sys
import textwrap
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from architect.compiler.types import CompileResult, ForgeSettings
from architect.floors import EvaluationCase
from architect.runtime import ArtifactExpiredError, BudgetExceeded, UnlicensedArtifactError
from architect.runtime.ephemeral import discard_code
from architect.runtime.wallet import compilation_wallet
from architect.spec import Budget, FloorKind, HarnessArtifact, Lifecycle, Spec, TestReport
from architect.spec.binding import check_subharness_tree

from ._routing import (
    FloorOrigin,
    RouteDecision,
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
from .cases import cases_floor, check_cases, load_cases
from .errors import ForgeModeError

if TYPE_CHECKING:
    from architect.compiler import Compiler
    from architect.models import ModelAdapter
    from architect.profiles import Services

__all__ = [
    "FORGE_SUMMARY_KEYS",
    "AgentChoice",
    "ForgeDelivery",
    "ForgeRunResult",
    "agent_line",
    "deliverable_ephemeral",
    "forge_role_outcome",
    "forge_run",
    "forge_summary",
    "per_call_compiler",
    "prepare_agent",
    "resolve_agent",
    "summary_line",
    "wrapped",
]

ForgeDelivery = Literal["run", "integrate"]

# Content-free counts copied from ``forge.json`` (Section 12.1).
FORGE_SUMMARY_KEYS = ("checks", "dryruns", "measures", "submissions", "spawns", "agent_cost_usd")
_FORGE_JSON_LIMIT = 1_048_576
_DELIVERED = ("forge_run_delivered", "forge_run_reused_installed")
_WIDTH = 100


def wrapped(text: str, width: int = _WIDTH) -> str:
    """Each over-long line wrapped at ``width`` (the session's ``render.wrap`` rule), so
    the CLI's stderr explanations stay within 100 ASCII columns like the session's."""
    out: list[str] = []
    for line in text.split("\n"):
        if len(line) <= width:
            out.append(line)
            continue
        indent = line[: len(line) - len(line.lstrip(" "))]
        out.extend(
            textwrap.wrap(
                line,
                width=width,
                subsequent_indent=indent + "  ",
                break_long_words=True,
                break_on_hyphens=False,
            )
            or [line[:width]]
        )
    return "\n".join(out)


@dataclass(frozen=True)
class AgentChoice:
    """One ``forge_agent`` resolution (Section 11.3), made once before the consent line.

    A surface that must show the consent text before it calls ``forge_run`` or
    ``forge_integrate`` (the async session) resolves the role once with
    ``prepare_agent`` and passes this in; the role router is then never asked twice.
    """

    adapter: ModelAdapter | None
    settings: ForgeSettings
    choice: Any


class ForgeRunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    output: JsonValue | None
    spec_hash: str
    artifact_digest: str | None
    licensed: bool
    stages: tuple[int, ...]
    route: RouteDecision | None
    reused: bool
    # Content-free counts from forge.json: checks, dryruns, measures, submissions,
    # spawns, agent_cost_usd.
    forge_summary: dict[str, int | float]
    floor_origin: FloorOrigin
    build_cost_usd: float
    compile_cost_usd: float
    run_cost_usd: float
    codes: tuple[str, ...]
    # The delivery's ``run_id`` (a UUID), so a session can rate the run it just made.
    run_id: str | None = None


def _within(budget: Budget, cap: Budget) -> bool:
    return budget.cost_usd <= cap.cost_usd and budget.compute_s <= cap.compute_s


def deliverable_ephemeral(
    result: CompileResult, artifact: HarnessArtifact, cap: Budget, *, require_stage0: bool
) -> bool:
    """``Orchestrator._deliverable``, with Stage 0 required only for a prose source.

    Independent code checks on a freshly loaded artifact; a model-supplied field can
    never pass these.
    """
    return (
        (not require_stage0 or 0 in result.stages)
        and 4 in result.stages
        and result.report is not None
        and artifact.report == result.report
        and artifact.spec == result.spec
        and artifact.spec.lifecycle == Lifecycle.EXPIRE
        and _within(artifact.spec.budget, cap)
        and artifact.licensed
    )


def forge_summary(directory: Path) -> dict[str, int | float]:
    """The content-free Forge counts of ``<directory>/forge.json``; empty when absent."""
    path = Path(directory) / "forge.json"
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _FORGE_JSON_LIMIT:
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    summary: dict[str, int | float] = {}
    for key in FORGE_SUMMARY_KEYS:
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if not math.isfinite(value) or value < 0:
            continue
        summary[key] = value
    return summary


def _money(value: float) -> str:
    return f"${value:.4f}"


def summary_line(result: ForgeRunResult, report: TestReport | None) -> str:
    """The stderr summary of Section 12.1 (the JSON result goes to stdout)."""
    costs = (
        f"build {_money(result.build_cost_usd)} agent (API-equivalent), "
        f"compile {_money(result.compile_cost_usd)}, play {_money(result.run_cost_usd)}"
    )
    delivered = bool(result.codes) and result.codes[0] in _DELIVERED
    if report is None or not result.licensed or not delivered:
        reason = result.codes[0] if result.codes else "forge_run_unlicensed"
        return f"forge run: not delivered ({reason}); {costs}"
    op = "<=" if report.floor_kind == FloorKind.HUMAN_EDIT_RATE else ">="
    evidence = (
        f"held_out {report.held_out_count}, {report.floor_kind.value} "
        f"{report.floor_score:.2f}{op}{report.floor_limit:.2f}, "
        f"verification {report.verification_id[:8]}"
    )
    if result.reused:
        return f"forge run: reused installed ({evidence}) delivered once; {costs}"
    return f"forge run: licensed ({evidence}) delivered once, expired; {costs}"


def agent_line(model: str, settings: ForgeSettings) -> str:
    """Section 11.3's pre-line, printed immediately before the Phase 9 cap line."""
    return f"forge agent: {model} (mode {settings.mode}, turns {settings.pi_max_turns})"


def _ladder_model(compiler: Compiler, spec: Spec | None) -> str:
    if spec is not None and spec.model_target != "cheapest that passes":
        return spec.model_target
    ladder = compiler.config.model_ladder
    return ladder[0] if ladder else "-"


def resolve_agent(
    services: Services, compiler: Compiler, settings: ForgeSettings, domain: str | None
) -> tuple[ModelAdapter | None, ForgeSettings, Any]:
    """The ``forge_agent`` role (Section 11.3); pinned unless ``roles.policy == "evidence"``.

    An explicit ``agent_model`` is a pin: it is resolved exactly as ``build_services``
    does and routing is off for the role. Under ``pinned`` the adapter is the per-call
    compiler's own, so behaviour is byte-identical to a Phase 9 Forge compile.
    """
    adapter = compiler.forge_adapter
    if settings.agent_model is not None:
        from architect.profiles import ProfileError, resolve_forge_agent

        profile = getattr(services, "profile", None)
        if profile is not None:
            try:
                adapter = resolve_forge_agent(profile, settings, services.adapter_factory)
            except ProfileError as exc:
                raise ForgeModeError(str(exc)) from None
        return adapter, settings, None
    roles = getattr(services, "roles", None)
    if roles is None or getattr(roles, "policy", "pinned") != "evidence":
        return adapter, settings, None
    try:
        choice = roles.choose("forge_agent", domain=domain)
    except Exception:
        return adapter, settings, None  # P5: role routing never blocks Forge.
    params = dict(getattr(getattr(choice, "variant", None), "params", {}) or {})
    updates = {key: params[key] for key in ("mode", "pi_max_turns") if key in params}
    try:
        routed = ForgeSettings.model_validate({**settings.model_dump(), **updates})
    except ValidationError:
        return adapter, settings, None
    chosen = getattr(choice, "adapter", None)
    return (chosen if chosen is not None else adapter), routed, choice


def cap_text(compiler: Compiler, settings: ForgeSettings, spec_hash: str | None) -> str:
    """The unchanged Phase 9 cap line (``session.forge.cap_line``)."""
    from architect.session.forge import cap_line

    verifications: int | None = None
    if spec_hash is not None:
        counter = getattr(compiler.library, "verification_count", None)
        try:
            value = counter(spec_hash) if callable(counter) else 0
            verifications = value if type(value) is int and value >= 0 else None
        except (OSError, ValueError):
            verifications = None
    return cap_line(compiler.config.compile_budget.cost_usd, settings, verifications)


def consent_text(
    services: Services,
    compiler: Compiler,
    settings: ForgeSettings,
    spec: Spec | None,
    *,
    resolved: AgentChoice | None = None,
) -> tuple[str, ForgeSettings, Any]:
    """Resolve the role (or take the one already ``resolved``), arm the per-call compiler
    with it, and build the two lines."""
    if resolved is None:
        agent, routed, choice = resolve_agent(
            services, compiler, settings, spec.domain if spec is not None else None
        )
    else:
        agent, routed, choice = resolved.adapter, resolved.settings, resolved.choice
    compiler.forge_adapter = agent
    compiler.config = compiler.config.model_copy(update={"forge": routed})
    shown = agent or compiler.rewrite_adapter
    model = shown.model if shown is not None else _ladder_model(compiler, spec)
    text = (
        agent_line(model, routed)
        + "\n"
        + cap_text(compiler, routed, spec.spec_hash if spec is not None else None)
    )
    return text, routed, choice


def prepare_agent(
    services: Services, settings: ForgeSettings, spec: Spec | None
) -> tuple[str, AgentChoice]:
    """The consent text and the one ``forge_agent`` choice behind it, before any spend.

    Pass the choice to ``forge_run``/``forge_integrate`` as ``agent=``: their text is then
    byte-identical to this one and the role router is consulted exactly once.
    """
    compiler, armed = per_call_compiler(services, settings)
    text, routed, choice = consent_text(services, compiler, armed, spec)
    return text, AgentChoice(adapter=compiler.forge_adapter, settings=routed, choice=choice)


def forge_role_outcome(
    licensed: bool, forged: Sequence[object]
) -> Literal["success", "failure", "excluded"]:
    """Section 11.4 for a compile-path ``forge_agent`` choice.

    ``success`` when Forge produced an accepted submission and Stage 4 licensed the
    result; ``failure`` when Forge ran otherwise; ``excluded`` when Forge never ran in
    this compile (``trigger=failed`` and Stage 2 passed), so the variant never acted.
    """
    if not forged:
        return "excluded"
    accepted = any(
        type(getattr(item, "accepted", 0)) is int and getattr(item, "accepted", 0) > 0
        for item in forged
    )
    return "success" if licensed and accepted else "failure"


def with_exact(spec: Spec, overrides: Mapping[str, JsonValue]) -> Spec:
    """Apply trusted exact overrides to a Spec exactly as ``Compiler.compile`` does."""
    if not overrides:
        return spec
    payload = spec.model_dump(mode="json")
    payload.update(json.loads(json.dumps(dict(overrides), allow_nan=False)))
    provenance = dict(spec.provenance)
    provenance.update({key: "user" for key in overrides if key != "provenance"})
    payload["provenance"] = provenance
    try:
        return Spec.model_validate(payload)
    except ValidationError:
        raise ForgeModeError("invalid_spec") from None


def _json_object(value: object) -> dict[str, JsonValue]:
    if value is None:
        return {}
    try:
        if not isinstance(value, dict):
            raise TypeError("overrides")
        loaded = json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError):
        raise ForgeModeError("invalid_overrides") from None
    assert isinstance(loaded, dict)
    loaded.pop("provenance", None)
    return loaded


def per_call_compiler(
    services: Services, settings: ForgeSettings
) -> tuple[Compiler, ForgeSettings]:
    """A copy of ``services.compiler`` (same root, runtime, factories, library, router,
    roles) with Forge armed for this call only, as ``forge_tools._spawn`` builds one."""
    base = services.compiler
    compiler = copy.copy(base)
    armed = settings.model_copy(update={"enabled": True, "trigger": "always"})
    compiler.config = base.config.model_copy(update={"forge": armed})
    return compiler, armed


def meter_budget(compiler: Compiler, settings: ForgeSettings) -> Budget:
    """A metering wallet looser than the compile's own (which still enforces the caps)."""
    budget = compiler.config.compile_budget
    return Budget(
        cost_usd=budget.cost_usd + settings.max_usd + 1.0,
        compute_s=budget.compute_s + settings.max_s + 60.0,
    )


async def forge_run(
    source: Spec | str,
    task_input: JsonValue,
    services: Services,
    settings: ForgeSettings,
    *,
    consent: Callable[[str], bool],
    cases: Path | None = None,
    reuse_installed: bool = False,
    overrides: dict[str, JsonValue] | None = None,
    agent: AgentChoice | None = None,
) -> ForgeRunResult:
    """Section 12.1. ``consent`` shows the two lines and returns the operator's answer.

    ``agent`` is a ``forge_agent`` choice already made by ``prepare_agent`` (the role is
    then not chosen again). Refusals before any spend raise ``ForgeModeError`` with a
    fixed code (``forge_cancelled``, ``forge_disabled``, ``forge_cases_*``...).
    """
    try:
        json.dumps(task_input, allow_nan=False)
    except (TypeError, ValueError):
        raise ForgeModeError("invalid_input_json") from None
    user = _json_object(overrides)
    prose = isinstance(source, str)
    if isinstance(source, str) and not source.strip():
        raise ForgeModeError("invalid_request")
    compiler, armed = per_call_compiler(services, settings)
    floor_origin: FloorOrigin = "user_dataset"
    case_rows: tuple[EvaluationCase, ...] = ()
    tag: str | None = None
    target: Spec | str
    compile_overrides: dict[str, JsonValue] | None
    exact = True
    if isinstance(source, Spec):
        effective = with_exact(source, {**user, "lifecycle": Lifecycle.EXPIRE.value})
        if cases is not None:
            case_rows, tag = load_cases(cases, effective)
            floor = effective.floor.model_dump(mode="json")
            floor["dataset"] = tag
            effective = with_exact(effective, {"floor": floor})
        target, compile_overrides = effective, None
    else:
        compile_overrides = {**user, "lifecycle": Lifecycle.EXPIRE.value}
        if cases is not None:
            case_rows, tag = load_cases(cases, None)
            # Stage 0 fixes kind and limit; only the dataset is pinned (deep merge).
            requested = user.get("floor")
            merged: dict[str, JsonValue] = dict(requested) if isinstance(requested, dict) else {}
            merged["dataset"] = tag
            compile_overrides["floor"] = merged
            exact = False
        target = source
    if tag is not None:
        from architect.backends.pi import PiConfig

        floor_origin = "authored_cases"
        match = target.floor if isinstance(target, Spec) else tag
        base = services.compiler
        compiler.floor_factory = cases_floor(match, case_rows, base.floor_factory)
        compiler.smoke_factory = cases_floor(match, case_rows, base.smoke_factory)
        assert cases is not None
        base_pi = base.pi_config or PiConfig()
        denied = (*base_pi.denied_paths, await asyncio.to_thread(Path(cases).resolve))
        compiler.pi_config = base_pi.model_copy(update={"denied_paths": denied})
    if compiler.smoke_factory is None:
        # Forge runs only with a smoke floor (Phase 9 Section 8.2); never silently not run.
        raise ForgeModeError("forge_disabled")

    # 1. Role, 2. consent: both before any spend.
    known = target if isinstance(target, Spec) else None
    text, armed, choice = consent_text(services, compiler, armed, known, resolved=agent)
    if not consent(text):
        record_role(services, choice, "excluded", floor_origin=floor_origin)
        raise ForgeModeError("forge_cancelled")

    # 3.-5. Stage 0, route and compile (Stage 3 is Forge), metered for the cost split.
    extra = compile_kwargs(compiler.compile, origin="forge_run", reuse_installed=reuse_installed)
    if known is not None:
        extra.update(compile_kwargs(compiler.compile, budget_cap=known.budget))
    try:
        with compilation_wallet(meter_budget(compiler, armed)) as meter:
            result = await compiler.compile(
                target, overrides=compile_overrides, exact_overrides=exact, **extra
            )
    except BudgetExceeded:
        refused = ForgeRunResult(
            output=None,
            spec_hash=known.spec_hash if known is not None else "",
            artifact_digest=None,
            licensed=False,
            stages=(),
            route=None,
            reused=False,
            forge_summary={},
            floor_origin=floor_origin,
            build_cost_usd=0.0,
            compile_cost_usd=0.0,
            run_cost_usd=0.0,
            codes=("forge_run_unlicensed", "compile_budget"),
        )
        record_role(services, choice, "failure", floor_origin=floor_origin)
        print(wrapped(summary_line(refused, None)), file=sys.stderr, flush=True)
        return refused
    return await _deliver(
        services,
        compiler,
        result,
        task_input,
        prose=prose,
        spent=meter.cost_usd,
        # P3: the explicit grant or the profile's standing ``forge_run`` grant, exactly
        # the grant the router reused under.
        reuse_granted=reuse_installed or standing_grant(compiler, "forge_run"),
        case_rows=case_rows,
        tag=tag,
        floor_origin=floor_origin,
        choice=choice,
    )


def _case_codes(
    result: CompileResult, case_rows: tuple[EvaluationCase, ...], tag: str | None
) -> list[str]:
    """For a prose request the case checks run once Stage 0 has fixed the Spec."""
    spec = result.spec
    if tag is None or spec.floor.dataset != tag:
        return []
    if spec.floor.kind != FloorKind.HELD_OUT_TASKS:
        return ["forge_cases_floor_kind"]
    try:
        check_cases(case_rows, spec)
    except ForgeModeError as exc:
        return [exc.code]
    return []


async def _deliver(
    services: Services,
    compiler: Compiler,
    result: CompileResult,
    task_input: JsonValue,
    *,
    prose: bool,
    spent: float,
    reuse_granted: bool,
    case_rows: tuple[EvaluationCase, ...],
    tag: str | None,
    floor_origin: FloorOrigin,
    choice: Any,
) -> ForgeRunResult:
    route = route_of(result)
    was_reused = reused(result)
    artifact = result.artifact
    summary: dict[str, int | float] = {}
    digest: str | None = None
    fresh: HarnessArtifact | None = None
    output: JsonValue | None = None
    run_id: str | None = None
    run_cost = 0.0
    outcome: Literal["delivered", "failed", "unlicensed"] = "unlicensed"
    codes = _case_codes(result, case_rows, tag) if prose else []
    licensed = False
    try:
        if artifact is not None:
            try:
                fresh = HarnessArtifact.load(artifact.directory)
                digest = fresh.artifact_digest
            except (OSError, ValueError, TypeError):
                fresh = None
            summary = forge_summary(artifact.directory)
        if was_reused:
            deliver = (
                reuse_granted
                and fresh is not None
                and fresh.spec.lifecycle == Lifecycle.INSTALL
                and not (fresh.directory / ".consumed").exists()
                and _reuse_target_matches(route, fresh)
                and _children_clean(fresh)
                and fresh.licensed
            )
            first = "forge_run_reused_installed"
        else:
            deliver = (
                fresh is not None
                and not result.failures
                and deliverable_ephemeral(result, fresh, result.spec.budget, require_stage0=prose)
                and _children_clean(fresh)
            )
            first = "forge_run_delivered"
        deliver = deliver and not codes
        if not deliver:
            codes.insert(0, "forge_run_unlicensed")
        else:
            assert fresh is not None
            licensed = True
            adapter = services.adapter_factory(fresh.model)
            # The delivered code is set only once Runtime.run has returned; a failed run
            # leads with its own code and is never reported as delivered.
            try:
                run = await services.runtime.run(fresh, task_input, adapter)
                output, run_cost, outcome = run.output, run.cost_usd, "delivered"
                run_id = str(run.run_id)
                codes.insert(0, first)
            except BudgetExceeded:
                codes.insert(0, "run_budget")
            except UnlicensedArtifactError:
                licensed = False  # Runtime.run's own license re-check refused it
                codes.insert(0, "unlicensed")
            except ArtifactExpiredError:
                codes.insert(0, "expired")
            except Exception:
                codes.insert(0, "run_failed")
            if outcome != "delivered":
                outcome = "failed"
                usage = getattr(services.runtime, "last_usage", None)
                run_cost = float(getattr(usage, "cost_usd", 0.0) or 0.0)
    finally:
        # 7. Keep report.json, forge.json and subharnesses/ as evidence; an installed
        # entry (reuse) lives outside the artifact root and is never touched.
        if artifact is not None and not was_reused:
            discard_code(compiler.artifact_root, artifact.directory)
    build = float(summary.get("agent_cost_usd", 0.0))
    compile_cost = max(0.0, spent - build)
    run_result = ForgeRunResult(
        output=output,
        spec_hash=result.spec.spec_hash,
        artifact_digest=digest,
        licensed=licensed,
        stages=result.stages,
        route=route,
        reused=was_reused and licensed,
        forge_summary=summary,
        floor_origin=floor_origin,
        build_cost_usd=round(build, 9),
        compile_cost_usd=round(compile_cost, 9),
        run_cost_usd=round(run_cost, 9),
        codes=fixed_codes([*codes, *result.failures]),
        run_id=run_id,
    )
    # 8. Records (content-free, fail-open).
    provider = provider_of(services, fresh.model) if fresh is not None else None
    record_outcome(
        services,
        route,
        outcome,
        stage4_passed=None if was_reused else bool(fresh is not None and fresh.licensed),
        evidence_class=evidence_class_of(provider),
        compile_cost_usd=compile_cost,
        build_cost_usd=build,
        delivery_cost_usd=run_cost,
    )
    record_role(
        services,
        choice,
        "excluded" if was_reused else "success" if licensed and summary else "failure",
        floor_origin=floor_origin,
    )
    # 9. The summary line on stderr.
    report = fresh.report if fresh is not None and licensed else None
    print(wrapped(summary_line(run_result, report)), file=sys.stderr, flush=True)
    return run_result


def _reuse_target_matches(route: RouteDecision | None, artifact: HarnessArtifact) -> bool:
    """P2: a reused artifact must still be the version the decision referenced."""
    target = getattr(route, "target", None) if route is not None else None
    if target is None:
        return False
    return (
        getattr(target, "artifact_digest", None) == artifact.artifact_digest
        and getattr(target, "spec_hash", None) == artifact.spec.spec_hash
    )


def _children_clean(artifact: HarnessArtifact) -> bool:
    try:
        check_subharness_tree(artifact)
    except (OSError, ValueError, TypeError):
        return False
    return True
