"""A pure, $0 simulation of the router and the role router (Phase 10 Section 14).

``simulate`` drives the real ``architect.routing.route`` and ``simulate_roles`` the
real ``RoleRouter.choose`` over in-memory fakes of the evidence index and the route
log, with Bernoulli outcomes drawn from a seeded ``random.Random``. Nothing here
reads or writes a file, calls a model or opens a socket, and nothing licenses: the
fakes hold numbers and ids only.

``roles_arm`` is the ``bench roles --policy pinned|evidence`` arm: it replays the
committed fake role-outcome logs under ``examples/bench/roles/*.jsonl`` through the
real ``RoleRouter``. ``simulation_suite`` runs the fixed scenarios behind pass
criterion 5 (Section 14.1); ``ci_assert.simulation_failures`` judges them.

Every number here is ``synthetic_fake``: it proves the decision rules and their
bounds, never routing quality.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from architect.routing.evidence import (
    InstalledRef,
    RoleStats,
    TemplateRef,
    VersionEvidence,
    wilson,
)
from architect.routing.labels import (
    PROVIDER_EVIDENCE,
    EvidenceClass,
    FloorOrigin,
    Provider,
    RoleName,
)
from architect.routing.log import RoleRecord, RouteAction
from architect.routing.roles import (
    RoleOutcome,
    RoleRouter,
    RoleRouting,
    RoleVariant,
    outcome_success,
    outcome_weight,
)
from architect.routing.router import RoutePolicy, TaskProbe, route
from architect.routing.signature import TaskSignature, signature
from architect.spec import Budget, FloorDefinition, FloorKind, Lifecycle, Spec

from .rows import BenchError

__all__ = [
    "ROLES_DIR",
    "SIM_MODEL",
    "STEPS_PER_DAY",
    "RolesArm",
    "RolesReplay",
    "SimInstalled",
    "SimResult",
    "SimStep",
    "SimTemplate",
    "SimVariant",
    "SimulationSuite",
    "dump_suite",
    "roles_arm",
    "roles_table",
    "simulate",
    "simulate_roles",
    "simulation_suite",
]

SIM_MODEL = "sim"
SIM_ROLE: RoleName = "rewrite"  # no price-quote budget filter, so only evidence decides
STEPS_PER_DAY = 100  # simulated decisions per UTC day, for the exploration cap
ROLES_DIR = Path(__file__).resolve().parents[2] / "examples" / "bench" / "roles"
_ID = r"^[a-z0-9][a-z0-9_-]{0,31}$"
_LIMIT = 0.5  # the simulated floor limit (held_out_tasks: higher is better)
_CLASS: EvidenceClass = "local_zero_cost"
_PROVIDER: Provider = "ollama"
Unit = Annotated[float, Field(ge=0, le=1)]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class SimTemplate(_Frozen):
    """A library template: warm starts from it pass Stage 4 with probability ``p``."""

    id: Annotated[str, Field(pattern=_ID)]
    p: Unit
    successes: Annotated[int, Field(ge=0)] = 0
    failures: Annotated[int, Field(ge=0)] = 0
    evicted: bool = False
    licensed: bool = True
    n: Annotated[int, Field(ge=1)] = 30  # held-out cases of its Stage 4 report
    cost_usd: Annotated[float, Field(ge=0)] = 0.01
    same_slot: bool = False  # a snapshot of the probe's own slot (a promotion rival)


class SimInstalled(_Frozen):
    """An installed entry; ``same_slot`` makes it Tier A for the probe."""

    id: Annotated[str, Field(pattern=_ID)]
    p: Unit
    licensed: bool = True
    consumed: bool = False
    drifting: bool = False
    n: Annotated[int, Field(ge=1)] = 30
    cost_usd: Annotated[float, Field(ge=0)] = 0.01
    same_slot: bool = True


class SimVariant(_Frozen):
    """A role variant with true success rate ``p`` and ``samples`` prior outcomes
    drawn afresh for every trial (weighted 1.0, an example-fixture floor)."""

    id: Annotated[str, Field(pattern=_ID)]
    model: Annotated[str, Field(pattern=_ID)]
    params: dict[str, int | str] = Field(default_factory=dict)
    p: Unit
    cost_usd: Annotated[float, Field(ge=0)]
    wall_s: Annotated[float, Field(ge=0)] = 1.0
    samples: Annotated[int, Field(ge=0)] = 20
    default: bool = False


class SimStep(_Frozen):
    step: Annotated[int, Field(ge=0)]
    day: Annotated[int, Field(ge=0)]
    action: RouteAction
    target: str | None
    explored: bool
    # Today's explored spend before this decision (the cap's input), and what it added.
    explore_spent_usd: Annotated[float, Field(ge=0)] = 0.0
    explore_cost_usd: Annotated[float, Field(ge=0)] = 0.0
    forbidden: bool = False  # the target was evicted, unlicensed or consumed when chosen
    promote: bool = False
    rollback: bool = False


class SimResult(_Frozen):
    kind: Literal["route", "roles"]
    seed: int
    steps: tuple[SimStep, ...]
    explore_every: Annotated[int, Field(ge=0)] = 0
    explore_usd_per_day: Annotated[float, Field(ge=0)] = 0.0
    default: str | None = None  # the pinned role variant (roles only)
    worse: tuple[str, ...] = ()  # role variants at least 0.10 below the default's p
    evidence: Literal["synthetic_fake"] = "synthetic_fake"

    @property
    def exploration_share(self) -> float:
        return sum(1 for s in self.steps if s.explored) / len(self.steps) if self.steps else 0.0

    @property
    def explored_over_cap(self) -> int:
        """Explored decisions taken although today's spend had reached the cap."""
        return sum(
            1
            for s in self.steps
            if s.explored and s.explore_spent_usd >= self.explore_usd_per_day
        )

    @property
    def forbidden_chosen(self) -> int:
        return sum(1 for s in self.steps if s.forbidden)

    @property
    def promote_rate(self) -> float:
        return sum(1 for s in self.steps if s.promote) / len(self.steps) if self.steps else 0.0

    def share(self, targets: Sequence[str]) -> float:
        if not self.steps:
            return 0.0
        return sum(1 for s in self.steps if s.target in targets) / len(self.steps)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_SCHEMA_IN: dict[str, object] = {
    "type": "object",
    "required": ["text"],
    "properties": {"text": {"type": "string"}},
    "additionalProperties": False,
}
_SCHEMA_OUT: dict[str, object] = {
    "type": "object",
    "required": ["label"],
    "properties": {"label": {"enum": ["a", "b"]}},
    "additionalProperties": False,
}


def _spec(task: str) -> Spec:
    """The simulated probe (and its sibling): a synthetic Spec, never compiled."""
    return Spec.model_validate(
        {
            "task": task,
            "input_schema": _SCHEMA_IN,
            "output_schema": _SCHEMA_OUT,
            "floor": FloorDefinition(
                kind=FloorKind.HELD_OUT_TASKS, limit=_LIMIT, dataset="sim", minimum_samples=15
            ).model_dump(mode="json"),
            "budget": Budget(cost_usd=1.0, compute_s=60.0).model_dump(mode="json"),
            "model_target": SIM_MODEL,
            "lifecycle": Lifecycle.EXPIRE.value,
        }
    )


_PROBE = _spec("Label a short simulated text as a or b.")
_SIBLING = _spec("Classify one short simulated text into a or b.")


class _State:
    """Mutable per-simulation template counters (warm-start outcomes, evictions)."""

    def __init__(self, templates: Sequence[SimTemplate]) -> None:
        self.successes = {t.id: t.successes for t in templates}
        self.failures = {t.id: t.failures for t in templates}
        self.evicted = {t.id: t.evicted for t in templates}

    def record(self, template: str, passed: bool) -> None:
        # The library rule: two cumulative failures evict (library/__init__.py).
        if passed:
            self.successes[template] += 1
        else:
            self.failures[template] += 1
            if self.failures[template] >= 2:
                self.evicted[template] = True


class _SimIndex:
    """An in-memory ``routing.router._Index``: one draw of every candidate's Stage 4
    held-out score per step, template counters from the simulation state."""

    def __init__(
        self,
        templates: Sequence[SimTemplate],
        installed: Sequence[SimInstalled],
        state: _State,
        rng: random.Random,
    ) -> None:
        self._templates, self._installed, self._state, self._rng = templates, installed, state, rng
        self._sig = {True: signature(_PROBE), False: signature(_SIBLING)}
        self.drawn: dict[str, tuple[float, float]] = {}

    def _listed(self, limit: int) -> list[tuple[int, SimTemplate]]:
        """The templates ``HarnessLibrary.candidates`` would list, in sequence order:
        the first ``limit`` non-evicted rows plus at most ``limit`` evicted ones, so an
        evicted template never holds a place a fresh one could take."""
        out: list[tuple[int, SimTemplate]] = []
        loaded = unloaded = 0
        for sequence, item in enumerate(self._templates):
            if self._state.evicted[item.id]:
                if unloaded >= limit:
                    continue
                unloaded += 1
            else:
                if loaded >= limit:
                    continue
                loaded += 1
            out.append((sequence, item))
        return out

    def draw(self, limit: int) -> None:
        """Fresh Stage 4 results for this step: m and m_lcb per candidate the index
        lists (every template the library would list at ``limit``, every entry)."""
        self.drawn = {}
        listed = [item for _, item in self._listed(limit)]
        items: list[SimTemplate | SimInstalled] = [*listed, *self._installed]
        for item in items:
            k = sum(1 for _ in range(item.n) if self._rng.random() < item.p)
            m = k / item.n - _LIMIT
            self.drawn[item.id] = (m, m - 0.5 / math.sqrt(max(item.n, 1)))

    def evidence_class(self, model: str) -> EvidenceClass | None:
        return _CLASS if model == SIM_MODEL else None

    def _common(self, item_id: str, same_slot: bool, n: int, cost: float) -> dict[str, object]:
        m, m_lcb = self.drawn.get(item_id, (0.0, 0.0))
        return {
            "model": SIM_MODEL,
            "spec": _PROBE if same_slot else _SIBLING,
            "signature": self._sig[same_slot],
            "evidence_class": _CLASS,
            "m": m,
            "m_lcb": m_lcb,
            "sec_lcb": 0.0,
            "cost_usd": cost,
            "compute_s": 1.0,
            "report_cost_usd": cost,
            "latency_s": 1.0,
            "latency_basis": "bound",
            "k": 1,
            "held_out_count": n,
            "evidence": ("stage4_signed", _CLASS, "latency_bound"),
        }

    def installed(self, sig: TaskSignature) -> tuple[VersionEvidence, ...]:
        out: list[VersionEvidence] = []
        for item in self._installed:
            ref = InstalledRef(
                name=item.id,
                revision=1,
                artifact_digest=_digest(f"installed:{item.id}"),
                spec_hash=_digest(f"spec:{item.id}"),
            )
            out.append(
                VersionEvidence.model_validate(
                    {
                        "origin": "installed",
                        "ref": ref,
                        "licensed": item.licensed,
                        "consumed": item.consumed,
                        "drift": "drifting" if item.drifting else "no_window",
                        **self._common(item.id, item.same_slot, item.n, item.cost_usd),
                    }
                )
            )
        return tuple(out)

    def templates(
        self, sig: TaskSignature, models: tuple[str, ...], limit: int
    ) -> tuple[VersionEvidence, ...]:
        out: list[VersionEvidence] = []
        for sequence, item in self._listed(limit):
            if SIM_MODEL not in models:
                continue
            s, f = self._state.successes[item.id], self._state.failures[item.id]
            ref = TemplateRef(
                key=_digest(f"template:{item.id}"),
                artifact_digest=_digest(f"template:{item.id}"),
                model=SIM_MODEL,
            )
            out.append(
                VersionEvidence.model_validate(
                    {
                        "origin": "template",
                        "ref": ref,
                        "licensed": item.licensed,
                        "evicted": self._state.evicted[item.id],
                        "r": wilson(s, s + f, 1.0),
                        "successes": s,
                        "failures": f,
                        "promoted": s >= 3,
                        "sequence": sequence,
                        **self._common(item.id, item.same_slot, item.n, item.cost_usd),
                    }
                )
            )
        return tuple(out)

    def stage4_history(self, sig: TaskSignature) -> tuple[bool, int, int]:
        return (False, 0, 0)

    def earlier_revisions(self, name: str, current: int) -> tuple[VersionEvidence, ...]:
        return ()


def _target_id(
    ref: object, templates: Sequence[SimTemplate], installed: Sequence[SimInstalled]
) -> str | None:
    if isinstance(ref, TemplateRef):
        for item in templates:
            if ref.key == _digest(f"template:{item.id}"):
                return item.id
    if isinstance(ref, InstalledRef):
        return ref.name
    return None


def simulate(
    templates: Sequence[SimTemplate],
    installed: Sequence[SimInstalled],
    steps: int,
    seed: int,
    policy: RoutePolicy,
) -> SimResult:
    """``steps`` decisions of the real ``route()`` on one probe shape (origin
    ``bench``, reuse granted), with a persisted exploration counter and a daily
    explored-spend cap of ``STEPS_PER_DAY`` decisions per day. A warm start draws a
    Bernoulli(p) Stage 4 outcome into its template's counters (two failures evict).
    """
    if steps < 0:
        raise ValueError("steps must be nonnegative")
    rng = random.Random(seed)
    state = _State(templates)
    index = _SimIndex(templates, installed, state, rng)
    by_id = {t.id: t for t in templates}
    forbidden_installed = {i.id for i in installed if not i.licensed or i.consumed}
    spent: dict[int, float] = {}
    out: list[SimStep] = []
    for step in range(steps):
        day = step // STEPS_PER_DAY
        today = spent.get(day, 0.0)
        index.draw(policy.max_candidates)
        probe = TaskProbe(
            origin="bench",
            spec=_PROBE,
            budget_cap=_PROBE.budget,
            ladder=(SIM_MODEL,),
            grant_reuse=True,
        )
        decision = route(
            probe, index=index, policy=policy, counter=step, explore_spent_usd=today
        )
        target = _target_id(decision.target, templates, installed)
        forbidden = target in forbidden_installed or (
            target in by_id and (state.evicted[target] or not by_id[target].licensed)
        )
        cost = 0.0
        if decision.action == "warm_start" and target in by_id:
            template = by_id[target]
            state.record(target, rng.random() < template.p)
            if decision.explored:
                cost = template.cost_usd
                spent[day] = today + cost
        out.append(
            SimStep(
                step=step,
                day=day,
                action=decision.action,
                target=target,
                explored=decision.explored,
                explore_spent_usd=today,
                explore_cost_usd=cost,
                forbidden=forbidden,
                promote=any(p.kind == "promote" for p in decision.proposals),
                rollback=any(p.kind == "rollback" for p in decision.proposals),
            )
        )
    return SimResult(
        kind="route",
        seed=seed,
        steps=tuple(out),
        explore_every=policy.explore_every,
        explore_usd_per_day=policy.explore_usd_per_day,
    )


class _RoleIndex:
    """An in-memory ``RoleEvidence``: weighted counts per variant from outcomes, in
    the evidence class of the variant's provider (P7)."""

    def __init__(self, providers: Mapping[str, Provider]) -> None:
        self.providers = providers
        self.counts: dict[str, list[float]] = {}  # variant key -> [n, successes, cost, wall]

    def reset(self) -> None:
        self.counts = {}

    def add(
        self, variant: RoleVariant, success: float, weight: float, cost: float, wall: float
    ) -> None:
        bucket = self.counts.setdefault(variant.key(), [0.0, 0.0, 0.0, 0.0])
        bucket[0] += weight
        bucket[1] += success * weight
        bucket[2] += cost * weight
        bucket[3] += wall * weight

    def role_stats(self, role: RoleName, variant: RoleVariant, domain: str | None) -> RoleStats:
        n, successes, cost, wall = self.counts.get(variant.key(), [0.0, 0.0, 0.0, 0.0])
        return RoleStats(
            n=n,
            successes=successes,
            mean_cost_usd=cost / n if n else None,
            median_wall_s=wall / n if n else None,
            evidence_class=PROVIDER_EVIDENCE[self.providers.get(variant.model, _PROVIDER)],
        )


class _RoleLog:
    """An in-memory route log for ``RoleRouter``: decisions only, never a file."""

    def __init__(self) -> None:
        self.records: list[RoleRecord] = []

    def append(self, record: object) -> None:
        if isinstance(record, RoleRecord):
            self.records.append(record)

    def roles(self, role: RoleName) -> Iterator[RoleRecord]:
        return iter([record for record in self.records if record.role == role])


def _role_router(
    routing: RoleRouting,
    variants: Sequence[RoleVariant],
    index: _RoleIndex,
    log: _RoleLog,
    role: RoleName,
    providers: Mapping[str, Provider],
    prices: Mapping[str, float],
) -> RoleRouter:
    declared = routing.model_copy(update={"candidates": {role: tuple(variants[1:])}})
    return RoleRouter(
        declared,
        index,
        log,  # type: ignore[arg-type]  # an in-memory RouteLog stand-in (roles/append)
        providers=providers,
        quote=lambda model: prices.get(model, math.inf),
    )


def simulate_roles(
    variants: Sequence[SimVariant], steps: int, seed: int, routing: RoleRouting
) -> SimResult:
    """``steps`` independent trials of the real ``RoleRouter.choose``: each trial
    draws every variant's ``samples`` prior outcomes from Bernoulli(p), then chooses
    once. The pinned default is the variant marked ``default`` (else the first)."""
    if not variants:
        raise ValueError("at least one variant")
    ordered = sorted(variants, key=lambda v: not v.default)
    default = ordered[0]
    role_variants = [RoleVariant(model=v.model, params=v.params) for v in ordered]
    ids = {rv.key(): v.id for rv, v in zip(role_variants, ordered, strict=True)}
    rng = random.Random(seed)
    providers: dict[str, Provider] = {v.model: _PROVIDER for v in ordered}
    index, log = _RoleIndex(providers), _RoleLog()
    router = _role_router(routing, role_variants, index, log, SIM_ROLE, providers, {})
    out: list[SimStep] = []
    for step in range(steps):
        index.reset()
        for rv, v in zip(role_variants, ordered, strict=True):
            for _ in range(v.samples):
                index.add(rv, 1.0 if rng.random() < v.p else 0.0, 1.0, v.cost_usd, v.wall_s)
        chosen, _decision, explored, _codes = router.choose(
            SIM_ROLE, domain=None, default=role_variants[0]
        )
        out.append(
            SimStep(
                step=step,
                day=step // STEPS_PER_DAY,
                action="compile",
                target=ids.get(chosen.key()),
                explored=explored,
            )
        )
    return SimResult(
        kind="roles",
        seed=seed,
        steps=tuple(out),
        explore_every=routing.explore_every,
        default=default.id,
        worse=tuple(v.id for v in ordered if v.p <= default.p - 0.10 + 1e-12),
    )


# ---------------------------------------------------------------- roles arm


class _Scenario(_Frozen):
    kind: Literal["scenario"]
    role: RoleName
    domain: str | None = None
    variants: tuple[RoleVariant, ...] = Field(min_length=1)
    providers: dict[str, Provider]
    prices: dict[str, float] = Field(default_factory=dict)


class _OutcomeLine(_Frozen):
    kind: Literal["outcome"]
    variant: Annotated[int, Field(ge=0)]
    outcome: RoleOutcome
    score: float | None = None
    cost_usd: Annotated[float, Field(ge=0)]
    wall_s: Annotated[float, Field(ge=0)]
    floor_origin: FloorOrigin | None = None


class RolesReplay(_Frozen):
    """One committed log replayed through ``RoleRouter`` (content-free).

    ``choices[i]`` is how many replay steps chose variant ``i`` (0 is the pinned
    default); ``final`` is the variant chosen after the whole log.
    """

    file: str
    role: RoleName
    outcomes: Annotated[int, Field(ge=0)]
    choices: tuple[int, ...]
    final: Annotated[int, Field(ge=0)]
    codes: tuple[str, ...]


class RolesArm(_Frozen):
    policy: Literal["pinned", "evidence"]
    replays: tuple[RolesReplay, ...]
    evidence: Literal["synthetic_fake"] = "synthetic_fake"
    note: str = "plumbing and decision determinism, not quality"


def _read_log(path: Path) -> tuple[_Scenario, list[_OutcomeLine]]:
    try:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not lines:
            raise ValueError("empty")
        scenario = _Scenario.model_validate_json(lines[0])
        outcomes = [_OutcomeLine.model_validate_json(line) for line in lines[1:]]
    except (OSError, UnicodeError, ValueError, ValidationError):
        raise BenchError("bench_rows_unreadable") from None
    if any(line.variant >= len(scenario.variants) for line in outcomes):
        raise BenchError("bench_rows_unreadable")
    return scenario, outcomes


def roles_arm(
    policy: Literal["pinned", "evidence"],
    directory: Path = ROLES_DIR,
    *,
    min_samples: int = 20,
) -> RolesArm:
    """``bench roles --policy``: replay every ``*.jsonl`` log in ``directory``.

    Before each committed outcome the real ``RoleRouter`` chooses with the evidence
    of the outcomes before it; the outcome then joins the evidence (weighted by its
    floor origin). Under ``pinned`` every choice is the default by construction.
    """
    files = sorted(directory.glob("*.jsonl"))
    if not files:
        raise BenchError("bench_rows_unreadable")
    replays: list[RolesReplay] = []
    for path in files:
        scenario, outcomes = _read_log(path)
        variants = list(scenario.variants)
        index, log = _RoleIndex(scenario.providers), _RoleLog()
        routing = RoleRouting(policy=policy, min_samples=min_samples)
        router = _role_router(
            routing, variants, index, log, scenario.role, scenario.providers, scenario.prices
        )
        counts = [0] * len(variants)
        codes: list[str] = []
        final = 0
        keys = [variant.key() for variant in variants]
        for line in [*outcomes, None]:
            chosen, _decision, _explored, step_codes = router.choose(
                scenario.role, domain=scenario.domain, default=variants[0]
            )
            final = keys.index(chosen.key()) if chosen.key() in keys else 0
            if line is None:
                codes = list(step_codes)  # the codes of the decision after the whole log
                break
            counts[final] += 1
            success = outcome_success(line.outcome, line.score)
            if success is not None:
                index.add(
                    variants[line.variant],
                    success,
                    outcome_weight(line.floor_origin),
                    line.cost_usd,
                    line.wall_s,
                )
        replays.append(
            RolesReplay(
                file=path.name,
                role=scenario.role,
                outcomes=len(outcomes),
                choices=tuple(counts),
                final=final,
                codes=tuple(codes),
            )
        )
    return RolesArm(policy=policy, replays=tuple(replays))


def roles_table(arm: RolesArm) -> str:
    """ASCII, at most 100 columns: one line per replayed log."""
    lines = [f"roles arm, policy {arm.policy}; {arm.evidence}: {arm.note}"]
    lines.append(f"{'log':<36} {'role':<14} {'outcomes':>8} {'choices':<24} {'final':>5}")
    for replay in arm.replays:
        choices = "/".join(str(count) for count in replay.choices)
        lines.append(
            f"{replay.file[:36]:<36} {replay.role:<14} {replay.outcomes:>8} "
            f"{choices[:24]:<24} {replay.final:>5}"
        )
    return "\n".join(line[:100] for line in lines) + "\n"


# ------------------------------------------------------------ scenarios


class SimulationSuite(_Frozen):
    """The fixed scenarios of pass criterion 5, each over ``trials`` seeded steps."""

    trials: Annotated[int, Field(ge=1)]
    exploration: SimResult  # the every-k-th rule binds, the cap never does
    cap: SimResult  # the daily cap binds
    filters: SimResult
    promote: tuple[tuple[float, SimResult], ...]
    roles_evidence: SimResult
    roles_pinned: SimResult


def _policy(**values: object) -> RoutePolicy:
    return RoutePolicy.model_validate(values)


def simulation_suite(trials: int = 2000, seed: int = 7) -> SimulationSuite:
    """Run the Section 14.1 (5) scenarios; ``ci_assert.simulation_failures`` judges."""
    # Exploration: one reliable template takes every ordinary warm start, and a
    # supply of never-passing ones keeps an untried template (s + f < 3) listed at
    # every step: each is explored at most twice before two failures evict it and
    # the library lists the next. The cap ($100/day against $0.20/day) never binds,
    # so only the every-5th rule holds the share at 1/5; a router that explored more
    # often would find a template to explore and break the bound.
    flood = [
        SimTemplate(id="anchor", p=1.0, cost_usd=0.01),
        *(SimTemplate(id=f"x{i}", p=0.0, n=5, cost_usd=0.01) for i in range(300)),
    ]
    exploration = simulate(
        flood,
        [],
        trials,
        seed,
        _policy(explore_every=5, explore_usd_per_day=100.0, max_candidates=32),
    )
    # The cap: never-failing templates, and a $0.05/day cap (5 explored $0.01
    # compiles) that binds long before the every-5th rule runs out (20 a day).
    fresh = [SimTemplate(id=f"t{i}", p=1.0, cost_usd=0.01) for i in range(32)]
    cap = simulate(
        fresh,
        [],
        trials,
        seed,
        _policy(explore_every=5, explore_usd_per_day=0.05, max_candidates=32),
    )
    filters = simulate(
        [
            SimTemplate(id="evicted", p=0.99, successes=5, evicted=True),
            SimTemplate(id="unlicensed", p=0.99, successes=5, licensed=False),
            SimTemplate(id="ok", p=0.7),
        ],
        [
            SimInstalled(id="consumed", p=0.99, consumed=True),
            SimInstalled(id="revoked", p=0.99, licensed=False),
        ],
        trials,
        seed + 1,
        _policy(explore_every=3),
    )
    promote = tuple(
        (
            p,
            simulate(
                [SimTemplate(id="rival", p=p, same_slot=True)],
                [SimInstalled(id="incumbent", p=p)],
                trials,
                seed + 2 + index,
                _policy(),
            ),
        )
        for index, p in enumerate((0.5, 0.8, 0.95))
    )
    variants = [
        SimVariant(id="pinned", model="sim-large", p=0.9, cost_usd=0.02, default=True),
        SimVariant(id="worse", model="sim-small", p=0.8, cost_usd=0.002),
    ]
    roles_evidence = simulate_roles(
        variants, trials, seed + 10, RoleRouting(policy="evidence", min_samples=20)
    )
    roles_pinned = simulate_roles(variants, trials, seed + 11, RoleRouting(policy="pinned"))
    return SimulationSuite(
        trials=trials,
        exploration=exploration,
        cap=cap,
        filters=filters,
        promote=promote,
        roles_evidence=roles_evidence,
        roles_pinned=roles_pinned,
    )


def dump_suite(suite: SimulationSuite) -> str:
    """Canonical JSON of a suite (content-free numbers and ids), for CI artifacts."""
    return json.dumps(suite.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
