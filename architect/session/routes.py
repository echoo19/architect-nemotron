"""Route and role screens shared by the CLI and the session (Phase 10 Sections 8.4, 13).

Everything drawn here is fixed text, closed labels, codes, numbers and the host-side
candidate reference (a registry ``name@revision`` or a library key prefix). No task
text, prompt, case or model output reaches these lines. Every surface that shows a
route also shows that Stage 4 decides (honesty rule 1), labels ``synthetic_fake``
numbers as plumbing (rule 2), names ``latency_bound`` (rule 3), ``drift_window_none``
(rule 4) and ``proxy_completed`` (rule 5).
"""

from __future__ import annotations

import statistics
import textwrap
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal, TypedDict

from architect.routing.evidence import wilson
from architect.routing.labels import PROVIDER_EVIDENCE, ROLE_TASK_TYPES
from architect.routing.roles import JUDGE_PRESET, ROLE_NAMES, RoleVariant
from architect.routing.router import (
    InstalledRef,
    RouteDecision,
    RoutePolicy,
    TaskProbe,
    TemplateRef,
    render_decision,
    route,
    wrap_codes,
)
from architect.spec import Lifecycle, Spec
from architect.telemetry import TelemetryRow, categorical

from .codes import explain

if TYPE_CHECKING:
    from architect.profiles import Services
    from architect.routing.labels import RoleName

__all__ = [
    "STAGE4_LINE",
    "advice",
    "advice_lines",
    "deliver_command",
    "installed_match",
    "probe_for",
    "role_evidence",
    "roles_table",
    "route_line",
    "route_screen",
    "target_label",
]

WIDTH = 100
CHEAPEST = "cheapest that passes"
STAGE4_LINE = "routing orders licensed versions; Stage 4 alone decides every license"
PLUMBING_LINE = "evidence synthetic_fake: plumbing, not quality"
LATENCY_LINE = "lat_s b = latency_bound: a per-case maximum, not a measurement"
DRIFT_LINE = "drift_window_none: no drift window exists for this entry; none is invented"
Origin = Literal["session", "cli", "http", "orchestrator"]
Surface = Literal["session", "cli"]
# Codes whose fixed sentence tells the operator what to do next (Section 13.4).
_ACTIONABLE = ("route_reuse_installed", "route_reuse_not_granted", "route_forge_suggested")


def _ascii(text: str) -> str:
    return text.encode("ascii", "backslashreplace").decode("ascii")[:WIDTH]


def _wrapped(text: str, indent: str = "  ") -> list[str]:
    """An explanation line wrapped at 100 columns; nothing is cut off."""
    clean = text.encode("ascii", "backslashreplace").decode("ascii")
    return textwrap.wrap(
        clean,
        width=WIDTH,
        subsequent_indent=indent,
        break_long_words=True,
        break_on_hyphens=False,
    ) or [clean[:WIDTH]]


def target_label(decision: RouteDecision) -> str:
    """``name@rev`` or the 12-character library key prefix of the decision's target."""
    target = decision.target
    if isinstance(target, InstalledRef):
        return f"{target.name}@{target.revision}"
    if isinstance(target, TemplateRef):
        return target.key[:12]
    return "-"


def installed_match(decision: RouteDecision) -> InstalledRef | None:
    """The exact-slot installed entry kept as advice when reuse was not granted."""
    if isinstance(decision.target, InstalledRef):
        return decision.target
    views = [view for view in decision.candidates if isinstance(view.ref, InstalledRef)]
    advised = [view for view in views if view.advice] or [
        view for view in views if view.tier == "A" and view.eligible
    ]
    ref = advised[0].ref if advised else None
    return ref if isinstance(ref, InstalledRef) else None


def _next(
    decision: RouteDecision,
    surface: Surface,
    hash8: str | None,
    profile: str | None,
    deliver: str | None = None,
) -> list[str]:
    """The command that acts on an actionable advice code, per surface."""
    token = hash8 or "<hash8>"
    shown = profile or "<profile>"
    commands: list[str] = []
    target = decision.target
    if (
        surface == "session"
        and decision.action == "reuse_installed"
        and isinstance(target, InstalledRef)
    ):
        commands.append(
            f"{deliver or f'approve {token}'} delivers {target.name}@{target.revision} as "
            "installed: no Stage 1 to 4 and no compile spend; a recompile never reuses"
        )
    match = installed_match(decision) if "route_reuse_not_granted" in decision.codes else None
    if match is not None:
        if surface == "session":
            commands.append(f"reuse {match.name} {token} {{json}}")
        else:
            commands.append(
                f"architect route <spec.json> --profile {shown} --run INPUT_JSON --yes"
            )
    if "route_forge_suggested" in decision.codes:
        if surface == "session" and deliver == "compile":
            # An expire Spec needs no approval (and cannot take one): compile delivers it,
            # and integrate needs lifecycle install first.
            commands.append(
                f"forge on, then compile | forge run {{json}} | forge integrate <repo> <into> "
                f"(set lifecycle install, then approve {token})"
            )
        elif surface == "session":
            commands.append(
                f"forge on, then approve {token} | forge run {{json}} (lifecycle expire) | "
                "forge integrate <repo> <into> (after approve)"
            )
        else:
            commands.append(
                "rerun the compile with --forge, or architect forge run <spec.json> "
                f"--profile {shown} --input JSON"
            )
    return [line for command in commands for line in _wrapped(f"next: {command}")]


def advice_lines(
    decision: RouteDecision,
    *,
    surface: Surface,
    hash8: str | None = None,
    profile: str | None = None,
    every_code: bool = False,
    deliver: str | None = None,
) -> list[str]:
    """The fixed Section 13.4 sentence of each code, then the next command.

    ``every_code`` explains every code (the ``route`` screen); otherwise only the
    actionable ones (the automatic REVIEW line). An exact installed match that was not
    granted is always named, so a warm start never hides it. A granted reuse (a standing
    grant in ``routing.reuse_origins``) is always explained before anything runs: what
    the delivering command does, that Stage 4 issued the license (rule 1), and that
    ``synthetic_fake`` evidence is plumbing (rule 2). ``deliver`` names that command
    (``approve <hash8>``, or ``compile`` for an expire Spec).
    """
    lines: list[str] = []
    match = installed_match(decision) if "route_reuse_not_granted" in decision.codes else None
    if match is not None and decision.action != "reuse_installed":
        lines.extend(
            _wrapped(
                f"route: installed match {match.name}@{match.revision} exists for exactly this "
                "task slot; reuse was not granted for this request"
            )
        )
    codes = decision.codes if every_code else tuple(
        code for code in decision.codes if code in _ACTIONABLE
    )
    for code in codes:
        lines.extend(_wrapped(f"  {code}: {explain(code)}", indent="    "))
    lines.extend(_next(decision, surface, hash8, profile, deliver))
    if decision.action == "reuse_installed" and not every_code:
        # The ``route`` screen prints these in its notes; the REVIEW line must too.
        lines.extend(line for note in _notes(decision) for line in _wrapped(note))
    return lines


def route_line(
    decision: RouteDecision, *, hash8: str | None = None, deliver: str | None = None
) -> str:
    """The automatic REVIEW line (Section 13.2), plus the advice it must not hide."""
    if decision.action == "warm_start" and isinstance(decision.target, TemplateRef):
        lines = [_ascii(f"route: warm start from {decision.target.key[:12]} (still verified)")]
    else:
        head = f"route: {decision.action}"
        if decision.target is not None:
            head += f" -> {target_label(decision)[:12]}"
        lines = wrap_codes(head, decision.codes or ("-",))
    lines.extend(advice_lines(decision, surface="session", hash8=hash8, deliver=deliver))
    return "\n".join(lines)


def _notes(decision: RouteDecision) -> list[str]:
    lines = [STAGE4_LINE]
    views = decision.candidates
    if "evidence_synthetic_only" in decision.codes or any(
        "synthetic_fake" in view.evidence for view in views
    ):
        lines.append(PLUMBING_LINE)
    if any(view.latency_basis == "bound" for view in views):
        lines.append(LATENCY_LINE)
    if any("drift_window_none" in view.evidence for view in views):
        lines.append(DRIFT_LINE)
    return lines


def deliver_command(spec: Spec) -> str:
    """The session command that delivers ``spec``: ``approve <hash8>`` or ``compile``."""
    return f"approve {spec.spec_hash[:8]}" if spec.lifecycle is Lifecycle.INSTALL else "compile"


def route_screen(
    decision: RouteDecision,
    *,
    profile: str | None = None,
    surface: Surface = "cli",
    hash8: str | None = None,
    deliver: str | None = None,
) -> str:
    """The Section 8.4 table, each code's fixed sentence, the next command and the honesty
    lines; every line at most 100 columns (explanations wrap, never truncate). ``deliver``
    is the session's delivering command (``deliver_command``)."""
    table = [_ascii(line) for line in render_decision(decision, profile=profile).split("\n")]
    explained = advice_lines(
        decision, surface=surface, hash8=hash8, profile=profile, every_code=True,
        deliver=deliver,
    )
    notes = [line for note in _notes(decision) for line in _wrapped(note)]
    return "\n".join([*table, *explained, *notes]) + "\n"


def probe_for(
    services: Services,
    spec: Spec,
    *,
    origin: Origin,
    grant: bool = False,
    forbid_reuse: bool = False,
) -> TaskProbe:
    """The probe a compile of ``spec`` would route (Section 6.1 ladder, spec budget)."""
    ladder = services.compiler.config.model_ladder
    models = tuple(ladder) if spec.model_target == CHEAPEST else (spec.model_target,)
    return TaskProbe(
        origin=origin,
        spec=spec,
        budget_cap=spec.budget,
        ladder=models,
        grant_reuse=grant and not forbid_reuse,
        forbid_reuse=forbid_reuse,
    )


def advice(
    services: Services, spec: Spec, *, origin: Origin, forbid_reuse: bool = False
) -> RouteDecision:
    """A pure, unrecorded decision for display (session ``route``, HTTP, CLI advice).

    It spends nothing, runs nothing, grants no reuse and explores nothing. With routing
    disabled (``services.router`` is None) the decision is the plain ``compile``.
    ``forbid_reuse`` is the recompile rule: the decision a recompile would get.
    """
    router = services.router
    probe = probe_for(services, spec, origin=origin, forbid_reuse=forbid_reuse)
    if router is None:
        return route(probe, index=_NoIndex(), policy=RoutePolicy.model_validate({"enabled": False}))
    return route(probe, index=router.index, policy=router.policy)


class _NoIndex:
    """Never read: a disabled policy decides ``compile`` before any evidence read."""

    def evidence_class(self, model: str) -> None:
        return None

    def installed(self, sig: object) -> tuple[()]:
        return ()

    def templates(self, sig: object, models: tuple[str, ...], limit: int) -> tuple[()]:
        return ()

    def stage4_history(self, sig: object) -> tuple[bool, int, int]:
        return False, 0, 0

    def earlier_revisions(self, name: str, current: int) -> tuple[()]:
        return ()


# ------------------------------------------------------------------ roles


def _money(value: float | None) -> str:
    return "-" if value is None else f"{value:.4f}"


def _seconds(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def _variant(variant: RoleVariant) -> str:
    params = ",".join(f"{key}={value}" for key, value in sorted(variant.params.items()))
    return variant.model + (f"[{params}]" if params else "")


def _cell(text: str, width: int) -> str:
    return _ascii(text)[:width].ljust(width)


# Widths plus one separator per gap sum to exactly 100, so the header keeps the
# honesty label ``proxy_completed`` whole (rule 5).
_COLUMNS = (("role", 13), ("variant", 20), ("n", 6), ("p_lcb", 5), ("$mean", 8),
            ("wall_s", 7), ("class", 19), ("proxy_completed", 15))


def _row(cells: tuple[str, ...]) -> str:
    text = " ".join(_cell(cell, width) for cell, (_, width) in zip(cells, _COLUMNS, strict=True))
    return text.rstrip()[:WIDTH]


Stats = tuple[int, int, float | None, float | None]


def _summary(found: list[TelemetryRow]) -> Stats:
    if not found:
        return 0, 0, None, None
    cost = sum(row.cost_usd for row in found) / len(found)
    wall = float(statistics.median(row.wall_time_s for row in found))
    return len(found), sum(1 for row in found if row.accepted), cost, wall


def _rows(services: Services) -> list[TelemetryRow]:
    try:
        return services.runtime.store.rows()
    except Exception:
        return []


def _telemetry(rows: list[TelemetryRow]) -> Callable[[str, str], Stats]:
    """``(task_type, model) -> (runs, accepted, mean $, median wall)`` from telemetry rows."""

    def stats(task_type: str, model: str) -> Stats:
        wanted = (categorical(task_type), categorical(model))
        return _summary([row for row in rows if (row.task_type, row.model) == wanted])

    return stats


class Completed(TypedDict):
    accepted: int
    runs: int


class RoleEntry(TypedDict):
    variant: str
    model: str
    params: dict[str, int | str]
    pinned_default: bool
    n: float | None
    p_lcb: float | None
    mean_cost_usd: float | None
    median_wall_s: float | None
    evidence_class: str | None
    proxy_completed: Completed


class JudgeEntry(TypedDict):
    model: str
    mean_cost_usd: float | None
    median_wall_s: float | None
    proxy_completed: Completed
    report_only: bool


class RoleEvidence(TypedDict):
    policy: str
    min_samples: int
    non_inferiority: float
    explore_every: int
    roles: dict[str, list[RoleEntry]]
    judge: list[JudgeEntry]
    notes: list[str]


def role_evidence(services: Services) -> RoleEvidence:
    """The ``architect roles`` content as data (Section 13.1), shared by the table and
    ``--json``: the policy, each role's variants with ``n``, ``p_lcb``, mean ``$``,
    median wall, evidence class and ``proxy_completed``, and the judge rows marked
    report only. Content-free: closed labels, model keys and numbers."""
    profile = services.profile
    routing = profile.roles
    index = services.router.index if services.router is not None else None
    roles = services.roles
    providers: dict[str, str] = {
        model: config.provider for model, config in profile.models.items()
    }
    rows = _rows(services)
    telemetry = _telemetry(rows)
    table: dict[str, list[RoleEntry]] = {}
    for role in ROLE_NAMES:
        default = roles.defaults[role][1] if roles is not None else RoleVariant(model="-")
        variants = [default]
        for variant in routing.candidates.get(role, ()):
            if variant not in variants:
                variants.append(variant)
        table[role] = [
            _role_entry(role, variant, index, providers, telemetry, routing.z, order == 0)
            for order, variant in enumerate(variants)
        ]
    judge = categorical(JUDGE_PRESET)
    judges: list[JudgeEntry] = []
    for model in sorted({row.model for row in rows if row.task_type == judge}):
        found = [row for row in rows if row.task_type == judge and row.model == model]
        runs, accepted, cost, wall = _summary(found)
        judges.append({
            "model": model,
            "mean_cost_usd": cost,
            "median_wall_s": wall,
            "proxy_completed": {"accepted": accepted, "runs": runs},
            "report_only": True,
        })
    return {
        "policy": routing.policy,
        "min_samples": routing.min_samples,
        "non_inferiority": routing.non_inferiority,
        "explore_every": routing.explore_every,
        "roles": table,
        "judge": judges,
        "notes": [
            *(
                [PLUMBING_LINE]
                if any(
                    entry["evidence_class"] == "synthetic_fake"
                    for entries in table.values()
                    for entry in entries
                )
                else []
            ),
            "proxy_completed = telemetry accepted: the run completed, never that it was "
            "correct",
            "the judge is part of the Spec and is never routed; its rows are report only",
        ],
    }


def roles_table(services: Services) -> str:
    """``architect roles`` / session ``roles`` (Section 13.1): policy, variants, judge."""
    data = role_evidence(services)
    lines = [
        _ascii(f"roles policy {data['policy']}  min_samples {data['min_samples']}  "
               f"non_inferiority {data['non_inferiority']}  explore_every "
               f"{data['explore_every']}"),
        _row(tuple(name for name, _ in _COLUMNS)),
    ]
    for role, entries in data["roles"].items():
        for entry in entries:
            n, p_lcb = entry["n"], entry["p_lcb"]
            lines.append(_row((
                role,
                entry["variant"],
                "-" if n is None else f"{n:g}",
                "-" if p_lcb is None else f"{p_lcb:.2f}",
                _money(entry["mean_cost_usd"]),
                _seconds(entry["median_wall_s"]),
                entry["evidence_class"] or "-",
                _completed(entry["proxy_completed"]),
            )))
    for judge in data["judge"]:
        lines.append(_row(("judge", judge["model"], "-", "-",
                           _money(judge["mean_cost_usd"]), _seconds(judge["median_wall_s"]),
                           "-", _completed(judge["proxy_completed"])))[: WIDTH - 13]
                     + "  report only")
    if any(
        entry["evidence_class"] == "synthetic_fake"
        for entries in data["roles"].values()
        for entry in entries
    ):
        lines.append(_ascii(PLUMBING_LINE))  # honesty rule 2: p_lcb of a fake is not quality
    lines.append(_ascii("proxy_completed = telemetry accepted: the run completed, never that it "
                        "was correct"))
    lines.append(_ascii("the judge is part of the Spec and is never routed; its row is a report"))
    return "\n".join(line[:WIDTH] for line in lines) + "\n"


def _completed(value: Completed) -> str:
    return f"{value['accepted']}/{value['runs']}"


def _role_entry(
    role: RoleName,
    variant: RoleVariant,
    index: object,
    providers: dict[str, str],
    telemetry: Callable[[str, str], Stats],
    z: float,
    pinned_default: bool,
) -> RoleEntry:
    n: float | None = None
    p_lcb: float | None = None
    stats_fn = getattr(index, "role_stats", None)
    if callable(stats_fn):
        try:
            stats = stats_fn(role, variant, None)
            n = float(stats.n)
            p_lcb = wilson(stats.successes, stats.n, z) if stats.n else None
        except Exception:
            n = p_lcb = None
    runs, accepted, cost, wall = telemetry(ROLE_TASK_TYPES[role], variant.model)
    return {
        "variant": _variant(variant),
        "model": variant.model,
        "params": dict(variant.params),
        "pinned_default": pinned_default,
        "n": n,
        "p_lcb": p_lcb,
        "mean_cost_usd": cost,
        "median_wall_s": wall,
        "evidence_class": PROVIDER_EVIDENCE.get(providers.get(variant.model, "")),
        "proxy_completed": {"accepted": accepted, "runs": runs},
    }
