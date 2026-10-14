"""The deterministic evidence router over verified versions (Phase 10 Sections 6, 8, 10.1).

``route`` orders licensed versions; it never licenses. A decision holds references
only (P2), every execution re-loads from disk and re-checks the license, and any
failure to read evidence falls open to a normal compile (P5).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from architect.registry import Registry, RegistryError
from architect.routing.evidence import (
    EvidenceUnavailable,
    InstalledRef,
    TemplateRef,
    VersionEvidence,
)
from architect.routing.labels import EvidenceClass, EvidenceLabel, RouteCode
from architect.routing.log import (
    Origin,
    OutcomeRecord,
    RouteAction,
    RouteLog,
    RouteLogError,
    RouteRecord,
    category,
)
from architect.routing.proposals import Proposal, promote_proposals, rollback_proposals
from architect.routing.signature import TaskSignature, fit, jaccard, signature
from architect.spec import Budget, HarnessArtifact, Lifecycle, Spec
from architect.spec.binding import check_subharness_tree
from architect.telemetry import categorical

__all__ = [
    "CandidateView",
    "InstalledRef",
    "Origin",
    "Proposal",
    "RouteAction",
    "RouteDecision",
    "RouteError",
    "RoutePolicy",
    "RoutedLibrary",
    "Router",
    "TaskProbe",
    "TemplateRef",
    "load_reuse_target",
    "render_decision",
    "route",
    "route_record",
    "wrap_codes",
]

CHEAPEST = "cheapest that passes"
# ``Registry._beats`` fields: exact task identity (Section 2 "Slot").
BEATS_FIELDS = ("task", "input_schema", "output_schema", "domain", "tools", "floor",
                "secondary_floor")
_NO_EXPLORE: frozenset[str] = frozenset({"http", "forge_integrate"})
_ADVICE_ONLY: frozenset[str] = frozenset({"http"})
_TIER_ORDER = {"A": 0, "B": 1, "C": 2}


class RouteError(ValueError):
    """A routed execution was refused; ``code`` is a fixed Section 13.4 code."""

    def __init__(self, code: RouteCode) -> None:
        super().__init__(code)
        self.code: RouteCode = code


class TaskProbe(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    origin: Origin
    spec: Spec
    budget_cap: Budget  # CLI and session: spec.budget; orchestrator: the subtask cap
    ladder: tuple[str, ...]  # models the compile would try (Section 6.1)
    grant_reuse: bool = False  # set only by the surfaces listed in P3
    # A recompile (``architect recompile``, session ``recompile <name>``) is a fresh
    # Stage 4 attempt: it never reuses, whatever the grant, and may still warm-start.
    forbid_reuse: bool = False


class RoutePolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    reuse_origins: frozenset[Origin] = frozenset()
    min_template_reliability: float = Field(0.3, ge=0, le=1)
    explore_every: int = Field(0, ge=0, le=1000)
    explore_usd_per_day: float = Field(1.0, ge=0, le=100)
    max_candidates: int = Field(8, ge=1, le=32)
    promote_margin: float = Field(0.0, ge=0, le=1)
    child_warm_start: bool = False
    subtask_models: bool = False


class CandidateView(BaseModel):
    """Content-free apart from the host-side ref."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier: Literal["A", "B", "C"]
    origin: Literal["installed", "template"]
    ref: InstalledRef | TemplateRef
    model: str
    fit: float
    m_lcb: float | None
    sec_lcb: float | None
    r: float | None
    h: float | None
    cost_usd: float | None
    latency_s: float | None
    latency_basis: Literal["measured", "bound"] | None
    k: int | None
    evidence: tuple[EvidenceLabel, ...]
    codes: tuple[RouteCode, ...]
    eligible: bool
    advice: bool = False


class RouteDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: UUID
    origin: Origin
    action: RouteAction
    target: InstalledRef | TemplateRef | None
    signature: TaskSignature
    candidates: tuple[CandidateView, ...]  # ranked, <= max_candidates
    chosen_rank: int | None
    explored: bool
    codes: tuple[RouteCode, ...]
    proposals: tuple[Proposal, ...]
    decision_ms: float


class _Index(Protocol):
    """What ``route`` reads; ``EvidenceIndex`` and in-memory simulation fakes satisfy it."""

    def evidence_class(self, model: str) -> EvidenceClass | None: ...

    def installed(self, sig: TaskSignature) -> tuple[VersionEvidence, ...]: ...

    def templates(
        self, sig: TaskSignature, models: tuple[str, ...], limit: int
    ) -> tuple[VersionEvidence, ...]: ...

    def stage4_history(self, sig: TaskSignature) -> tuple[bool, int, int]: ...

    def earlier_revisions(self, name: str, current: int) -> tuple[VersionEvidence, ...]: ...


# --------------------------------------------------------------------- ranking


def _low(value: float | None) -> float:
    """A missing number sorts last when minimized."""
    return math.inf if value is None else value


def _high(value: float | None) -> float:
    """A missing number sorts last when maximized (the key holds its negation)."""
    return math.inf if value is None else -value


# Latency is compared at this resolution (seconds). A median wall time is clock-tick
# noise below it (15.6 ms ticks on Windows, sub-millisecond fake runs elsewhere), and a
# preference decided by that noise would make two identical runs choose differently.
LATENCY_RESOLUTION_S = 0.1


def _latency_bucket(value: float | None) -> float | None:
    """``value`` floored to ``LATENCY_RESOLUTION_S``; None stays None (sorts last)."""
    if value is None:
        return None
    return math.floor(value / LATENCY_RESOLUTION_S + 1e-9) * LATENCY_RESOLUTION_S


def _key_a(item: VersionEvidence) -> tuple[float, float, float, float, float, float, str, int]:
    ref = item.ref
    name, revision = (ref.name, ref.revision) if isinstance(ref, InstalledRef) else ("", 0)
    return (
        _high(item.m_lcb), _high(item.sec_lcb), _high(item.h), _low(item.cost_usd),
        _low(_latency_bucket(item.latency_s)), _low(None if item.k is None else float(item.k)),
        categorical(name) if name else "", revision,
    )


def _key_b(
    fitness: float, item: VersionEvidence
) -> tuple[float, float, float, float, float, float, float]:
    return (
        -fitness, _high(item.r), -float(item.promoted), _high(item.m_lcb),
        _low(item.cost_usd), _low(None if item.k is None else float(item.k)),
        _low(None if item.sequence is None else float(item.sequence)),
    )


class _Row:
    """One candidate while deciding: evidence, tier, fit and its filter result."""

    __slots__ = ("advice", "code", "evidence", "fit", "key", "tier")

    def __init__(self, evidence: VersionEvidence, tier: Literal["A", "B", "C"], fitness: float):
        self.evidence = evidence
        self.tier: Literal["A", "B", "C"] = tier
        self.fit = fitness
        self.code: RouteCode | None = None
        self.advice = False
        self.key: tuple[object, ...] = ()

    @property
    def eligible(self) -> bool:
        return self.code is None


def _tier(
    probe: TaskProbe, sig: TaskSignature, item: VersionEvidence
) -> tuple[Literal["A", "B", "C", "none"], float]:
    if item.origin == "template":
        # Templates are warm starts only; a slot-equal snapshot is still Tier B.
        if item.signature is None or item.spec is None:
            return "B", 0.5
        same_kind = 1.0 if item.spec.floor.kind == probe.spec.floor.kind else 0.0
        return "B", 0.5 + 0.4 * jaccard(sig.tokens, item.signature.tokens) + 0.1 * same_kind
    if item.signature is None or item.spec is None:
        fallback = {"slot": ("A", 1.0), "shape": ("B", 0.5), "family": ("C", 0.2)}
        tier, fitness = fallback.get(item.match or "", ("none", 0.0))
        return tier, fitness  # type: ignore[return-value]
    tier, fitness = fit(sig, probe.spec, item.signature, item.spec)
    if tier == "B" and item.model not in probe.ladder:
        return "none", 0.0  # Tier B is same-model only (Section 7.1).
    return tier, fitness


def _filter(
    probe: TaskProbe, item: VersionEvidence, configured: Callable[[str], bool], fake_only: bool
) -> RouteCode | None:
    """Section 6.2 hard filters, in order; the first failure is the candidate's code."""
    if item.licensed is False:
        return "route_unlicensed"
    if item.origin == "installed" and item.consumed:
        return "route_consumed"
    if item.origin == "template" and item.evicted:
        return "route_evicted"
    if not configured(item.model) or (
        probe.spec.model_target != CHEAPEST and item.model != probe.spec.model_target
    ):
        return "route_model_unconfigured"
    cap = probe.budget_cap
    if (
        item.report_cost_usd is None
        or item.compute_s is None
        or item.report_cost_usd > cap.cost_usd
        or item.compute_s > cap.compute_s
    ):
        return "route_budget"
    if item.origin == "installed" and item.drift == "drifting":
        return "route_drift"
    if item.evidence_class == "synthetic_fake" and not fake_only:
        return "route_synthetic_excluded"
    return None


def _view(row: _Row, extra: tuple[RouteCode, ...] = ()) -> CandidateView:
    item = row.evidence
    codes: tuple[RouteCode, ...] = ((row.code,) if row.code is not None else ()) + extra
    return CandidateView(
        tier=row.tier,
        origin=item.origin,
        ref=item.ref,
        model=item.model,
        fit=round(row.fit, 6),
        m_lcb=item.m_lcb,
        sec_lcb=item.sec_lcb,
        r=item.r,
        h=item.h,
        cost_usd=item.cost_usd,
        latency_s=item.latency_s,
        latency_basis=item.latency_basis,
        k=item.k,
        evidence=item.evidence,
        codes=codes,
        eligible=row.eligible,
        advice=row.advice,
    )


def _dedupe(codes: Sequence[RouteCode]) -> tuple[RouteCode, ...]:
    return tuple(dict.fromkeys(codes))


# ---------------------------------------------------------------------- route


def route(
    probe: TaskProbe,
    *,
    index: _Index,
    policy: RoutePolicy,
    counter: int = 0,
    explore_spent_usd: float = 0.0,
    decision_id: UUID | None = None,
) -> RouteDecision:
    """Pure and synchronous: reads evidence, writes nothing, licenses nothing."""
    started = time.perf_counter()
    decision_id = decision_id or uuid4()
    sig = signature(probe.spec)

    def decided(
        action: RouteAction,
        codes: Sequence[RouteCode],
        *,
        target: InstalledRef | TemplateRef | None = None,
        candidates: tuple[CandidateView, ...] = (),
        chosen_rank: int | None = None,
        explored: bool = False,
        proposals: tuple[Proposal, ...] = (),
    ) -> RouteDecision:
        return RouteDecision(
            decision_id=decision_id,
            origin=probe.origin,
            action=action,
            target=target,
            signature=sig,
            candidates=candidates,
            chosen_rank=chosen_rank,
            explored=explored,
            codes=_dedupe(codes),
            proposals=proposals,
            decision_ms=(time.perf_counter() - started) * 1000,
        )

    if not policy.enabled:
        return decided("compile", ("route_compile",))
    configured_ladder = tuple(model for model in probe.ladder if index.evidence_class(model))
    if not configured_ladder:
        return decided("refuse", ("route_no_candidate",))
    try:
        installed = index.installed(sig)
        templates = index.templates(sig, probe.ladder, policy.max_candidates)
        open_failure, shape_unlicensed, shape_licensed = index.stage4_history(sig)
    except EvidenceUnavailable:
        return decided("compile", ("evidence_store_unavailable", "route_compile"))

    fake_only = all(index.evidence_class(m) == "synthetic_fake" for m in configured_ladder)

    def configured(model: str) -> bool:
        return index.evidence_class(model) is not None

    template_digests = {item.ref.artifact_digest for item in templates}
    rows: list[_Row] = []
    for item in (*installed, *templates):
        tier, fitness = _tier(probe, sig, item)
        if tier == "none":
            continue
        if (
            item.origin == "installed"
            and tier == "B"
            and item.ref.artifact_digest in template_digests
        ):
            continue  # Its library snapshot stands for it.
        row = _Row(item, tier, fitness)
        row.code = _filter(probe, item, configured, fake_only)
        rows.append(row)

    tier_a = sorted(
        (row for row in rows if row.tier == "A" and row.eligible), key=lambda r: _key_a(r.evidence)
    )
    tier_b = sorted(
        (
            row for row in rows
            if row.tier == "B" and row.eligible and row.evidence.origin == "template"
        ),
        key=lambda r: _key_b(r.fit, r.evidence),
    )
    codes: list[RouteCode] = []
    best = min((_TIER_ORDER[row.tier] for row in rows), default=None)
    fit_codes: dict[int | None, RouteCode] = {
        0: "fit_slot", 1: "fit_shape", 2: "fit_family", None: "fit_none"
    }
    codes.append(fit_codes[best])

    action: RouteAction = "compile"
    chosen: _Row | None = None
    explored = False
    advice: _Row | None = None
    granted = not probe.forbid_reuse and probe.origin not in _ADVICE_ONLY and (
        probe.grant_reuse or probe.origin in policy.reuse_origins
    )
    if tier_a and granted:
        action, chosen = "reuse_installed", tier_a[0]
        codes.append("route_reuse_installed")
    else:
        if tier_a:
            advice = tier_a[0]
            advice.advice = True
            codes.append("route_reuse_not_granted")
        if tier_b and _low(tier_b[0].evidence.r) >= policy.min_template_reliability:
            action, chosen = "warm_start", tier_b[0]
            codes.append("route_warm_start")
            k = policy.explore_every
            if (
                k > 0
                and probe.origin not in _NO_EXPLORE
                and probe.spec.lifecycle != Lifecycle.INSTALL
                and counter % k == k - 1
            ):
                pool = [
                    row for row in tier_b
                    if row.evidence.successes + row.evidence.failures < 3
                ]
                if pool:
                    if explore_spent_usd < policy.explore_usd_per_day:
                        chosen = min(
                            pool,
                            key=lambda r: (
                                r.evidence.successes + r.evidence.failures,
                                _key_b(r.fit, r.evidence),
                            ),
                        )
                        explored = True
                        codes.append("route_explore")
                    else:
                        codes.append("route_explore_cap")
        else:
            codes.append("route_compile")

    eligible = [row for row in rows if row.eligible and row.tier in ("A", "B")]
    if eligible and all(row.evidence.evidence_class == "synthetic_fake" for row in eligible):
        codes.append("evidence_synthetic_only")
    if eligible and all(
        row.evidence.held_out_count < 5
        and (
            row.evidence.origin == "installed"
            or row.evidence.successes + row.evidence.failures == 0
        )
        for row in eligible
    ):
        codes.append("evidence_insufficient")
    if open_failure or (not eligible and shape_unlicensed >= 3 and shape_licensed == 0):
        codes.append("route_forge_suggested")

    promote = promote_proposals(
        tier_a[0].evidence if tier_a else None,
        [row.evidence for row in eligible if row is not (tier_a[0] if tier_a else None)],
        slot=sig.slot,
        margin=policy.promote_margin,
    )
    rollback = rollback_proposals(
        index,
        [row.evidence for row in rows if row.tier == "A" and row.evidence.origin == "installed"],
    )
    if promote:
        codes.append("route_promote_proposed")
    if rollback:
        codes.append("route_rollback_suggested")

    ordered = _ordered(rows)
    kept = _keep(ordered, policy.max_candidates, [row for row in (chosen, advice) if row])
    views = tuple(
        _view(row, ("route_reuse_not_granted",) if row.advice else ()) for row in kept
    )
    chosen_rank = next((i for i, row in enumerate(kept) if row is chosen), None)
    target = None if chosen is None else chosen.evidence.ref
    return decided(
        action,
        codes,
        target=target,
        candidates=views,
        chosen_rank=chosen_rank,
        explored=explored,
        proposals=promote + rollback,
    )


def _ordered(rows: Sequence[_Row]) -> list[_Row]:
    """Eligible before filtered, then Tier A, B templates, B installed, C, each by key."""

    def key(row: _Row) -> tuple[object, ...]:
        item = row.evidence
        if row.tier == "A":
            inner: tuple[object, ...] = _key_a(item)
            group = 0
        elif row.tier == "B" and item.origin == "template":
            inner, group = _key_b(row.fit, item), 1
        elif row.tier == "B":
            inner, group = _key_a(item), 2
        else:
            inner, group = (-row.fit, *_key_a(item)), 3
        return (not row.eligible, group, inner)

    return sorted(rows, key=key)


def _keep(ordered: list[_Row], limit: int, required: Sequence[_Row]) -> list[_Row]:
    """The first ``limit`` rows, keeping the required rows first, in priority order.

    ``required`` is ``[chosen, advice]``: when there is room for only one of them, the
    chosen row wins, so a decision's target is always among its candidates.
    """
    kept = list(dict.fromkeys(required))[:limit]
    for row in ordered:
        if len(kept) >= limit:
            break
        if row not in kept:
            kept.append(row)
    rank = {id(row): i for i, row in enumerate(ordered)}
    return sorted(kept, key=lambda row: rank[id(row)])


# ---------------------------------------------------------------- records


def route_record(decision: RouteDecision) -> RouteRecord:
    """The content-free ``RouteRecord`` of a decision (P6)."""
    sig = decision.signature
    chosen = (
        decision.candidates[decision.chosen_rank] if decision.chosen_rank is not None else None
    )
    tiers = [view.tier for view in decision.candidates]
    tier: Literal["A", "B", "C", "none"] = min(tiers, key=_TIER_ORDER.__getitem__, default="none")
    return RouteRecord(
        decision_id=decision.decision_id,
        timestamp=datetime.now(UTC),
        origin=decision.origin,
        action=decision.action,
        tier=tier,
        slot=category(sig.slot),
        shape=category(sig.shape),
        family=category(sig.family),
        token_count=sig.token_count,
        candidate_count=len(decision.candidates),
        chosen_rank=decision.chosen_rank,
        target_digest=(
            categorical(decision.target.artifact_digest) if decision.target is not None else None
        ),
        model=categorical(chosen.model) if chosen is not None else None,
        fit=chosen.fit if chosen is not None else 0.0,
        m_lcb=chosen.m_lcb if chosen is not None else None,
        r=chosen.r if chosen is not None else None,
        expected_cost_usd=chosen.cost_usd if chosen is not None else None,
        latency_s=chosen.latency_s if chosen is not None else None,
        latency_basis=chosen.latency_basis if chosen is not None else None,
        evidence=chosen.evidence if chosen is not None else (),
        codes=decision.codes,
        explored=decision.explored,
        decision_ms=decision.decision_ms,
    )


class Router:
    """Routes one probe at a time and records every decision and outcome."""

    def __init__(self, index: _Index, policy: RoutePolicy, log: RouteLog) -> None:
        self.index = index
        self.policy = policy
        self.log = log

    def route(self, probe: TaskProbe) -> RouteDecision:
        """Reads the exploration counter and today's spend, routes, appends a record."""
        try:
            counter = self.log.counter(signature(probe.spec).shape)
            spent = self.log.explore_spent_today()
        except RouteLogError:
            decision = route(probe, index=_Unavailable(self.index), policy=self.policy)
        else:
            decision = route(
                probe, index=self.index, policy=self.policy, counter=counter,
                explore_spent_usd=spent,
            )
        try:
            self.log.append(route_record(decision))
        except RouteLogError:
            pass  # Fail-open (P5): an unrecorded decision never blocks a compile.
        return decision

    def outcome(self, decision: RouteDecision, record: OutcomeRecord) -> None:
        if record.decision_id != decision.decision_id:
            raise ValueError("route_outcome_mismatch")
        try:
            self.log.append(record)
        except RouteLogError:
            pass


class _Unavailable:
    """An index whose stores are unreadable: every read fails open (P5)."""

    def __init__(self, index: _Index) -> None:
        self._index = index

    def evidence_class(self, model: str) -> EvidenceClass | None:
        return self._index.evidence_class(model)

    def installed(self, sig: TaskSignature) -> tuple[VersionEvidence, ...]:
        raise EvidenceUnavailable("evidence_store_unavailable")

    def templates(
        self, sig: TaskSignature, models: tuple[str, ...], limit: int
    ) -> tuple[VersionEvidence, ...]:
        raise EvidenceUnavailable("evidence_store_unavailable")

    def stage4_history(self, sig: TaskSignature) -> tuple[bool, int, int]:
        raise EvidenceUnavailable("evidence_store_unavailable")

    def earlier_revisions(self, name: str, current: int) -> tuple[VersionEvidence, ...]:
        return ()


# ------------------------------------------------------------- execution


def load_reuse_target(
    decision: RouteDecision, registry: Registry, probe_spec: Spec
) -> HarnessArtifact:
    """Re-load the decision's installed target and re-check it (P2, Section 10.1)."""
    target = decision.target
    if decision.action != "reuse_installed" or not isinstance(target, InstalledRef):
        raise RouteError("route_no_reuse")
    try:
        artifact = registry.get(target.name, target.revision)
    except RegistryError:
        raise RouteError("route_unlicensed") from None
    if not artifact.licensed:
        raise RouteError("route_unlicensed")
    try:
        check_subharness_tree(artifact)
    except (OSError, ValueError, TypeError, RecursionError):
        raise RouteError("route_unlicensed") from None
    if (
        artifact.artifact_digest != target.artifact_digest
        or artifact.spec.spec_hash != target.spec_hash
    ):
        raise RouteError("route_target_changed")
    if any(getattr(artifact.spec, key) != getattr(probe_spec, key) for key in BEATS_FIELDS):
        raise RouteError("route_target_changed")
    if (artifact.directory / ".consumed").exists():
        raise RouteError("route_consumed")
    return artifact


class _Lookup(Protocol):
    def lookup(self, spec: Spec, model: str) -> HarnessArtifact | None: ...


class RoutedLibrary:
    """A per-call library that returns the chosen warm-start template (Section 10.2).

    ``lookup`` returns the chosen template, freshly re-authenticated, when the model
    and I/O shape match; everything else delegates unchanged to ``fallback``.
    """

    def __init__(self, chosen_template: TemplateRef | HarnessArtifact,
                 fallback: _Lookup | None = None) -> None:
        self.chosen = chosen_template
        self.fallback = fallback

    def _template(self) -> HarnessArtifact | None:
        chosen = self.chosen
        if isinstance(chosen, HarnessArtifact):
            try:
                fresh: HarnessArtifact | None = HarnessArtifact.load(chosen.directory)
            except (OSError, ValueError, TypeError, RecursionError):
                return None
            digest, model = chosen.artifact_digest, chosen.model
        else:
            loader = getattr(self.fallback, "template", None)
            if not callable(loader):
                return None
            fresh = loader(chosen.key)
            digest, model = chosen.artifact_digest, chosen.model
        if (
            not isinstance(fresh, HarnessArtifact)
            or not fresh.licensed
            or fresh.artifact_digest != digest
            or fresh.model != model
        ):
            return None
        try:
            check_subharness_tree(fresh)
        except (OSError, ValueError, TypeError, RecursionError):
            return None
        return fresh

    def lookup(self, spec: Spec, model: str) -> HarnessArtifact | None:
        chosen = self._template()
        if (
            chosen is not None
            and model == chosen.model
            and signature(chosen.spec).shape == signature(spec).shape
        ):
            return chosen
        return self.fallback.lookup(spec, model) if self.fallback is not None else None

    def store(self, artifact: HarnessArtifact) -> str:
        store = getattr(self.fallback, "store", None)
        return str(store(artifact)) if callable(store) else ""

    def record_verification(self, source: HarnessArtifact, passed: bool) -> None:
        record = getattr(self.fallback, "record_verification", None)
        if callable(record):
            record(source, passed)

    def verification_count(self, spec_hash: str) -> int:
        counter = getattr(self.fallback, "verification_count", None)
        if not callable(counter):
            return 0
        count = counter(spec_hash)
        return count if type(count) is int else 0

    def record_forge_verification(self, spec_hash: str) -> None:
        record = getattr(self.fallback, "record_forge_verification", None)
        if callable(record):
            record(spec_hash)


# ------------------------------------------------------------- explanation

_ABBREVIATIONS: dict[str, str] = {
    "stage4_signed": "stage4",
    "synthetic_fake": "fake",
    "local_zero_cost": "local",
    "live_api_equivalent": "live_ae",
    "live_billed": "live_b",
    "drift_stable": "nodrift",
    "bench_joined": "bench",
    "human_rated": "human",
    "template_promoted": "promo",
}
_WIDTHS = (4, 16, 12, 4, 6, 4, 8, 7, 2, 14)
_WIDTH = 100


def _ascii(text: str) -> str:
    return text.encode("ascii", "backslashreplace").decode("ascii")


def _cell(text: str, width: int) -> str:
    return _ascii(text)[:width].ljust(width)


def _label(ref: InstalledRef | TemplateRef) -> str:
    return f"{ref.name}@{ref.revision}" if isinstance(ref, InstalledRef) else ref.key[:12]


def _line(cells: Sequence[str], codes: str) -> str:
    text = " ".join(_cell(cell, width) for cell, width in zip(cells, _WIDTHS, strict=True))
    text = f"{text} {_ascii(codes)}"
    return text[:_WIDTH].rstrip()


def _number(value: float | None, pattern: str) -> str:
    return "-" if value is None else format(value, pattern)


def wrap_codes(
    head: str, codes: Sequence[str], *, width: int = _WIDTH, indent: str = "  "
) -> list[str]:
    """``head (code,code,...)`` wrapped at ``width`` between whole codes, never inside one.

    A decision's summary line is an explanation, not a table column: no code is cut off.
    """
    text = _ascii(head)
    if not codes:
        return [text]
    lines: list[str] = []
    current = text + " ("
    for number, code in enumerate(codes):
        piece = _ascii(code) + ("," if number < len(codes) - 1 else ")")
        if len(current) + len(piece) > width and current.strip():
            lines.append(current)
            current = indent + piece
        else:
            current += piece
    lines.append(current)
    return lines


def render_decision(decision: RouteDecision, *, profile: str | None = None) -> str:
    """The fixed-column ASCII table (Section 8.4), at most 100 columns wide."""
    lines = [
        _line(
            ("tier", "candidate", "model", "F", "m_lcb", "r", "$cost", "lat_s", "k", "evidence"),
            "codes",
        )
    ]
    for view in decision.candidates:
        latency = "-"
        if view.latency_s is not None:
            suffix = "m" if view.latency_basis == "measured" else "b"
            latency = f"{view.latency_s:.1f}{suffix}"
        evidence = ",".join(
            _ABBREVIATIONS[label] for label in view.evidence if label in _ABBREVIATIONS
        )
        lines.append(
            _line(
                (
                    view.tier,
                    _label(view.ref),
                    view.model,
                    f"{view.fit:.2f}",
                    _number(view.m_lcb, "+.3f"),
                    _number(view.r, ".2f"),
                    _number(view.cost_usd, ".4f"),
                    latency,
                    "-" if view.k is None else str(view.k),
                    evidence or "-",
                ),
                ",".join(view.codes) or "-",
            )
        )
    head = f"route: {decision.action}"
    if decision.target is not None:
        head += f" -> {_label(decision.target)}"
    lines.extend(wrap_codes(head, decision.codes))
    for proposal in decision.proposals:
        command = proposal.command
        if profile is not None:
            command = command.replace("<profile>", profile)
        lines.append(_ascii(f"proposal: {command}"))
    return "\n".join(lines)
