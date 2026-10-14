"""Routing Architect's own roles (Phase 10 Section 11).

A role is one of Architect's own model uses: Stage 0 inference, goal decomposition,
rewrite proposals, the Forge agent and floor drafting. The judge is never a role:
``FloorDefinition.judge_model`` is part of the Spec and ``spec_hash``, ``LLMJudge``
refuses other adapters, and calibration is a one-time gate.

The default policy ``pinned`` returns exactly today's adapters (identity, not
equality). The policy ``evidence`` switches a role away from its pinned default D
only when another declared variant is eligible and non-inferior to D (Section
11.4). Role routing chooses a model and closed parameters; it never touches a
Spec, a cap or a floor, and it never licenses anything.

Records are content-free: closed literals, numbers, UUIDs and ``h_`` categories.

The evidence index is typed by the structural protocol ``RoleEvidence``, which
``routing.evidence.EvidenceIndex`` satisfies and an in-memory simulation fake can
too. Evidence that cannot be read fails open to the pinned default with
``evidence_store_unavailable`` (P5).
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Literal, Protocol, Self, get_args
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SkipValidation,
    field_validator,
    model_validator,
)

from architect.models import ModelAdapter
from architect.routing.evidence import variant_category, wilson
from architect.routing.labels import (
    PROVIDER_EVIDENCE,
    ROLE_TASK_TYPES,
    EvidenceClass,
    FloorOrigin,
    Provider,
    RoleName,
    RouteCode,
    floor_origin_weight,
)
from architect.routing.log import RoleRecord, RouteLog, RouteLogError
from architect.telemetry.schema import categorical

__all__ = [
    "DECOMPOSITION_BUDGET_S",
    "DECOMPOSITION_BUDGET_USD",
    "INFERENCE_BUDGET_USD",
    "JUDGE_PRESET",
    "ROLE_NAMES",
    "ROLE_PARAMS",
    "ROLE_PRESETS",
    "RoleAdapters",
    "RoleChoice",
    "RoleEvidence",
    "RoleName",
    "RoleOutcome",
    "RoleRouter",
    "RoleRouting",
    "RoleRoutingError",
    "RoleStatsView",
    "RoleVariant",
    "check_params",
    "check_roles",
    "outcome_success",
    "outcome_weight",
    "wilson",
]

ROLE_NAMES: tuple[RoleName, ...] = get_args(RoleName)
RoleOutcome = Literal["success", "failure", "partial", "excluded"]

# Section 11.1: the telemetry preset label (``categorical(preset)``) of each role.
ROLE_PRESETS: Mapping[RoleName, str] = ROLE_TASK_TYPES
# The judge's preset label: reported by ``architect roles`` only, never routed.
JUDGE_PRESET = "verifier"

# Section 11.2: the closed parameter variants per role.
ROLE_PARAMS: Mapping[RoleName, Mapping[str, frozenset[int | str]]] = {
    "inference": {},
    "decomposition": {"max_tasks": frozenset({4, 8})},
    "rewrite": {"max_rewrites": frozenset({1, 3})},
    "forge_agent": {
        "mode": frozenset({"agent", "template"}),
        "pi_max_turns": frozenset({100, 200}),
    },
    "floor_draft": {},
}

# Section 11.4: the fixed role budgets checked by price quote. ``INFERENCE_BUDGET_USD``
# equals ``compiler.inference._INFERENCE_BUDGET.cost_usd`` (``routing`` may not import
# the compiler); a test pins the equality.
INFERENCE_BUDGET_USD = 1.0
DECOMPOSITION_BUDGET_USD = 1.0
DECOMPOSITION_BUDGET_S = 60.0

# Section 11.2: the roles whose pinned default without a host-built adapter the seam
# can reproduce. Stage 0 builds ``adapter_factory(D.model)`` itself (``"fake"``,
# ``compiler/__init__.py``). Every other role's call site keeps a fallback the seam
# cannot see: no rewrite, the Forge driver's ``adapter_factory(artifact.model)`` and
# floor drafting's ``spec.model_target``.
_FACTORY_DEFAULTS: frozenset[RoleName] = frozenset({"inference"})


class RoleRoutingError(ValueError):
    """A fixed-code role routing configuration error; the message is the code only."""

    def __init__(self, code: Literal["route_model_unknown", "route_param_invalid"]) -> None:
        super().__init__(code)
        self.code = code


def outcome_weight(floor_origin: FloorOrigin | None) -> float:
    """The weight one role outcome adds to its bucket's counts (Section 7.2): 1.0 for
    user and fixture floors (and no floor), 0.5 for generated, drafted or authored."""
    return floor_origin_weight(floor_origin)


def outcome_success(outcome: RoleOutcome, score: float | None = None) -> float | None:
    """1.0 success, 0.0 failure, None excluded (Section 11.4).

    ``partial`` (decomposition) counts as a success only when ``score == 1``.
    """
    if outcome == "excluded":
        return None
    if outcome == "partial":
        return 1.0 if score == 1 else 0.0
    return 1.0 if outcome == "success" else 0.0


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class RoleVariant(_Frozen):
    """A model (a ``profile.models`` key) plus closed per-role parameters.

    ``model`` is a plain string: an empty or unconfigured name is not a ``models`` key,
    so ``check_roles`` reports it as ``route_model_unknown`` (Section 11.2).
    """

    model: str
    params: dict[str, int | str] = Field(default_factory=dict)

    @field_validator("params", mode="before")
    @classmethod
    def exact_values(cls, value: object) -> object:
        # No coercion: true is not 1, 4.0 is not 4 and "4" is not 4.
        if isinstance(value, Mapping):
            for item in value.values():
                if type(item) not in (int, str):
                    raise RoleRoutingError("route_param_invalid")
        return value

    def key(self) -> str:
        """Canonical JSON identity; ``RoleRecord.variant`` is its ``categorical``."""
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )


def check_params(role: RoleName, variant: RoleVariant) -> None:
    """``route_param_invalid`` unless every parameter is in the role's closed set."""
    allowed = ROLE_PARAMS[role]
    for name, value in variant.params.items():
        values = allowed.get(name)
        if values is None or value not in values:
            raise RoleRoutingError("route_param_invalid")
        # 4 and "4" (and True for 1) are different values here.
        if not any(type(value) is type(option) for option in values):
            raise RoleRoutingError("route_param_invalid")


class RoleRouting(_Frozen):
    """``Profile.roles``: the role policy and each role's declared variants."""

    policy: Literal["pinned", "evidence"] = "pinned"
    candidates: dict[RoleName, tuple[RoleVariant, ...]] = Field(default_factory=dict)
    min_samples: int = Field(default=20, ge=5, le=10000)
    non_inferiority: float = Field(default=0.02, ge=0, le=0.2)
    z: float = Field(default=1.64, gt=0, le=4)
    explore_every: int = Field(default=0, ge=0, le=1000)

    @model_validator(mode="after")
    def closed_params(self) -> Self:
        for role, variants in self.candidates.items():
            for variant in variants:
                check_params(role, variant)
        return self


def check_roles(routing: RoleRouting, models: Mapping[str, object]) -> None:
    """Profile-level checks: ``route_model_unknown`` then ``route_param_invalid``."""
    for variants in routing.candidates.values():
        for variant in variants:
            if variant.model not in models:
                raise RoleRoutingError("route_model_unknown")
    for role, variants in routing.candidates.items():
        for variant in variants:
            check_params(role, variant)


class RoleChoice(_Frozen):
    """One role resolution: the adapter to call and the content-free decision.

    ``adapter`` is None only when the pinned default D is chosen and today's binding
    has no adapter the seam can reproduce (see ``RoleAdapters.adapter``): the call
    site then applies its own fallback, exactly as today.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    adapter: SkipValidation[ModelAdapter | None]
    variant: RoleVariant
    decision_id: UUID
    explored: bool
    codes: tuple[RouteCode, ...]


class RoleStatsView(Protocol):
    """The read side of ``routing.evidence.RoleStats``."""

    @property
    def n(self) -> float: ...
    @property
    def successes(self) -> float: ...
    @property
    def mean_cost_usd(self) -> float | None: ...
    @property
    def median_wall_s(self) -> float | None: ...
    @property
    def evidence_class(self) -> EvidenceClass: ...


class RoleEvidence(Protocol):
    """The part of ``routing.evidence.EvidenceIndex`` role routing reads."""

    def role_stats(
        self, role: RoleName, variant: RoleVariant, domain: str | None
    ) -> RoleStatsView: ...


def _domain_category(domain: str | None) -> str:
    return "none" if domain is None else categorical(domain)


class _Arm:
    """One candidate's evidence for one call; memory only."""

    __slots__ = ("order", "variant", "evidence", "n", "successes", "cost", "wall", "budget_ok")

    def __init__(
        self,
        order: int,
        variant: RoleVariant,
        evidence: EvidenceClass | None,
        stats: RoleStatsView | None,
        budget_ok: bool,
    ) -> None:
        self.order = order
        self.variant = variant
        self.evidence = evidence
        usable = stats is not None and evidence is not None and stats.evidence_class == evidence
        # P7: evidence of another class (a provider change) counts as no evidence.
        self.n = float(stats.n) if usable and stats is not None else 0.0
        self.successes = float(stats.successes) if usable and stats is not None else 0.0
        self.cost = stats.mean_cost_usd if usable and stats is not None else None
        self.wall = stats.median_wall_s if usable and stats is not None else None
        self.budget_ok = budget_ok

    def sort_key(self) -> tuple[float, float, int]:
        # Unknown cost or wall sorts last; candidate order (D first) breaks ties.
        cost = math.inf if self.cost is None else self.cost
        wall = math.inf if self.wall is None else self.wall
        return (cost, wall, self.order)


class RoleRouter:
    """Section 11.4: pinned default D, or a non-inferior cheaper variant on evidence."""

    def __init__(
        self,
        routing: RoleRouting,
        index: RoleEvidence,
        log: RouteLog,
        *,
        providers: Mapping[str, Provider],
        quote: Callable[[str], float],
    ) -> None:
        self.routing = routing
        self.index = index
        self.log = log
        self.providers = providers
        self.quote = quote

    def _evidence(self, model: str) -> EvidenceClass | None:
        provider = self.providers.get(model)
        return None if provider is None else PROVIDER_EVIDENCE.get(provider)

    def _within_budget(self, role: RoleName, model: str, wall: float | None) -> bool:
        if role not in ("inference", "decomposition"):
            return True
        try:
            price = float(self.quote(model))
        except Exception:
            return False
        if not math.isfinite(price):
            return False
        if role == "inference":
            return price <= INFERENCE_BUDGET_USD
        return price <= DECOMPOSITION_BUDGET_USD and (
            wall is None or wall <= DECOMPOSITION_BUDGET_S
        )

    def _stats(
        self, role: RoleName, variant: RoleVariant, domain: str | None, evidence: EvidenceClass
    ) -> RoleStatsView:
        stats = self.index.role_stats(role, variant, domain)
        thin = stats.evidence_class != evidence or stats.n < self.routing.min_samples
        if domain is not None and thin:
            return self.index.role_stats(role, variant, None)
        return stats

    def _arms(
        self, role: RoleName, domain: str | None, default: RoleVariant, opaque: bool
    ) -> list[_Arm]:
        # An opaque D (``RoleAdapters._opaque``) stands for the call site's own fallback,
        # not for ``D.model``: a declared candidate equal to it is a separate arm, and
        # no evidence of that variant is D's (D's outcomes are never credited).
        variants = [default]
        for variant in self.routing.candidates.get(role, ()):
            seen = variants[1:] if opaque else variants
            if all(variant != other for other in seen):
                variants.append(variant)
        arms: list[_Arm] = []
        for order, variant in enumerate(variants):
            evidence = self._evidence(variant.model)
            stats = None
            if evidence is not None and not (opaque and order == 0):
                stats = self._stats(role, variant, domain, evidence)
            wall = None
            if stats is not None and stats.evidence_class == evidence:
                wall = stats.median_wall_s
            budget_ok = evidence is not None and self._within_budget(role, variant.model, wall)
            arms.append(_Arm(order, variant, evidence, stats, budget_ok))
        return arms

    def _prior_decisions(self, role: RoleName) -> int:
        return sum(1 for record in self.log.roles(role) if record.outcome is None)

    def _append(self, record: RoleRecord) -> None:
        # P5: a route log that cannot be written never blocks the role call.
        try:
            self.log.append(record)
        except (RouteLogError, OSError, ValueError):
            pass

    def choose(
        self, role: RoleName, *, domain: str | None, default: RoleVariant
    ) -> tuple[RoleVariant, UUID, bool, tuple[RouteCode, ...]]:
        """The variant for one call, its decision id, whether it explored, and codes.

        Under ``pinned`` this is always ``default`` and nothing is logged. Unreadable
        evidence also gives ``default``, with ``evidence_store_unavailable`` (P5).
        """
        variant, decision_id, explored, codes, _ = self._decide(
            role, domain=domain, default=default, opaque=False
        )
        return variant, decision_id, explored, codes

    def _decide(
        self, role: RoleName, *, domain: str | None, default: RoleVariant, opaque: bool
    ) -> tuple[RoleVariant, UUID, bool, tuple[RouteCode, ...], bool]:
        """``choose`` plus whether the pinned default arm D itself was chosen.

        ``opaque`` marks a D whose adapter the seam cannot reproduce (the call site's
        own fallback runs, so D's outcomes are never credited). Such a D keeps order 0
        and wins ties as always, but it is never an exploration target: it could never
        gain samples, so it would take every exploring call from the declared
        candidates for good (Section 11.4 step 6).
        """
        if role not in ROLE_NAMES:
            raise ValueError("unknown_role")
        decision_id = uuid4()
        if self.routing.policy == "pinned":
            return default, decision_id, False, (), True
        try:
            arms = self._arms(role, domain, default, opaque)
            prior = self._prior_decisions(role) if self.routing.explore_every > 0 else 0
        except Exception:
            return default, decision_id, False, ("evidence_store_unavailable",), True
        routing = self.routing
        pinned = arms[0]
        p_hat_d = pinned.successes / pinned.n if pinned.n > 0 else 0.5
        eligible = [
            arm
            for arm in arms[1:]
            if arm.evidence is not None and arm.budget_ok and arm.n >= routing.min_samples
        ]
        p_lcb = {arm.order: wilson(arm.successes, arm.n, routing.z) for arm in eligible}
        if pinned.n > 0:
            p_lcb[pinned.order] = wilson(pinned.successes, pinned.n, routing.z)
        non_inferior = [
            arm for arm in eligible if p_lcb[arm.order] >= p_hat_d - routing.non_inferiority
        ]
        ranked = sorted([pinned, *non_inferior], key=_Arm.sort_key)
        ranked += [arm for arm in arms if all(arm is not seen for seen in ranked)]
        codes: list[RouteCode] = []
        chosen = ranked[0]
        if not eligible:
            codes.append("role_evidence_insufficient")
        explored = False
        k = routing.explore_every
        if k > 0 and prior % k == k - 1:
            pool = [arm for arm in (arms[1:] if opaque else arms) if arm.budget_ok]
            if pool:
                chosen = min(pool, key=lambda arm: (arm.n, arm.order))
                explored = True
                codes.append("role_explore")
        if chosen.evidence is not None:
            self._append(
                RoleRecord(
                    decision_id=decision_id,
                    timestamp=datetime.now(UTC),
                    role=role,
                    model=categorical(chosen.variant.model),
                    variant=variant_category(chosen.variant),
                    domain=_domain_category(domain),
                    evidence_class=chosen.evidence,
                    floor_origin=None,
                    chosen_rank=ranked.index(chosen),
                    n=chosen.n,
                    p_lcb=p_lcb.get(chosen.order),
                    explored=explored,
                    outcome=None,
                    score=None,
                    codes=tuple(codes),
                )
            )
        return chosen.variant, decision_id, explored, tuple(codes), chosen is pinned

    def _record_outcome(
        self,
        role: RoleName,
        choice: RoleChoice,
        outcome: RoleOutcome,
        score: float | None,
        floor_origin: FloorOrigin | None,
        domain: str | None,
    ) -> None:
        evidence = self._evidence(choice.variant.model)
        if evidence is None:
            return
        self._append(
            RoleRecord(
                decision_id=choice.decision_id,
                timestamp=datetime.now(UTC),
                role=role,
                model=categorical(choice.variant.model),
                variant=variant_category(choice.variant),
                domain=_domain_category(domain),
                evidence_class=evidence,
                floor_origin=floor_origin,
                chosen_rank=0,
                n=outcome_weight(floor_origin),
                p_lcb=None,
                explored=choice.explored,
                outcome=outcome,
                score=score,
                codes=choice.codes,
            )
        )


class RoleAdapters:
    """The resolution seam every role call site consults (Section 11.3).

    ``defaults`` holds today's binding per role: the adapter the host already built
    (None where today's call site applies its own fallback) and the pinned variant D.
    Under ``pinned``, without a router, or when ``pins(role)`` is true, ``adapter``
    returns exactly that default object.

    The seam never makes up D. Where today's binding is None, a chosen D carries
    today's Stage 0 fallback for ``inference`` (``adapter_factory(D.model)``) and no
    adapter for every other role, so the call site keeps its own fallback; and an
    outcome of such a choice is not credited to D, whose model may not be the one
    the call site ran.

    ``defaults`` and ``adapter_factory`` are kept as given, never copied, and read on
    every call: the host passes live views of the Compiler's ``inference_adapter``,
    ``rewrite_adapter`` and ``forge_adapter`` and of its ``adapter_factory``, so a
    session's metered wrappers and ``Session._arm_forge`` swaps reach every role call.
    """

    def __init__(
        self,
        defaults: Mapping[RoleName, tuple[ModelAdapter | None, RoleVariant]],
        router: RoleRouter | None,
        adapter_factory: Callable[[str], ModelAdapter],
        pins: Callable[[RoleName], bool],
    ) -> None:
        self.defaults = defaults
        self.router = router
        self.adapter_factory = adapter_factory
        self.pins = pins
        # decision_id -> (role, domain) for outcomes recorded later; memory only.
        self._pending: dict[UUID, tuple[RoleName, str | None]] = {}

    @property
    def policy(self) -> Literal["pinned", "evidence"]:
        return "pinned" if self.router is None else self.router.routing.policy

    def _default(self, role: RoleName) -> tuple[ModelAdapter | None, RoleVariant]:
        if role not in ROLE_NAMES or role not in self.defaults:
            raise ValueError("unknown_role")
        return self.defaults[role]

    def _routed(self, role: RoleName) -> bool:
        return self.router is not None and self.policy == "evidence" and not self.pins(role)

    @staticmethod
    def _opaque(role: RoleName, adapter: ModelAdapter | None) -> bool:
        """D has no adapter the seam can reproduce: the call site's fallback runs."""
        return adapter is None and role not in _FACTORY_DEFAULTS

    def _default_adapter(
        self, role: RoleName, adapter: ModelAdapter | None, default: RoleVariant
    ) -> ModelAdapter | None:
        # Today's binding, else only a fallback the seam reproduces exactly (Stage 0).
        if adapter is not None or role not in _FACTORY_DEFAULTS:
            return adapter
        return self.adapter_factory(default.model)

    def choose(self, role: RoleName, *, domain: str | None = None) -> RoleChoice:
        """Resolve one role call. A switched variant is built by ``adapter_factory``."""
        adapter, default = self._default(role)
        if not self._routed(role):
            codes: tuple[RouteCode, ...] = ("role_pinned",) if self.pins(role) else ()
            return RoleChoice(
                adapter=self._default_adapter(role, adapter, default),
                variant=default,
                decision_id=uuid4(),
                explored=False,
                codes=codes,
            )
        assert self.router is not None
        variant, decision_id, explored, routed_codes, is_default = self.router._decide(
            role, domain=domain, default=default, opaque=self._opaque(role, adapter)
        )
        if is_default:
            chosen = self._default_adapter(role, adapter, default)
        else:
            chosen = self.adapter_factory(variant.model)
        self._pending[decision_id] = (role, domain)
        return RoleChoice(
            adapter=chosen,
            variant=variant,
            decision_id=decision_id,
            explored=explored,
            codes=routed_codes,
        )

    def adapter(self, role: RoleName, *, domain: str | None = None) -> ModelAdapter | None:
        """Today's adapter under ``pinned`` (identity); the routed one under ``evidence``.

        A chosen D is today's binding itself (None keeps the call site's fallback);
        only a switched variant is built, including a declared candidate whose model
        equals an opaque D's placeholder model. No outcome can be recorded without a
        ``RoleChoice``, so nothing stays pending.
        """
        adapter, default = self._default(role)
        if not self._routed(role):
            return adapter
        assert self.router is not None
        variant, _decision_id, _explored, _codes, is_default = self.router._decide(
            role, domain=domain, default=default, opaque=self._opaque(role, adapter)
        )
        return adapter if is_default else self.adapter_factory(variant.model)

    def record(
        self,
        choice: RoleChoice,
        outcome: RoleOutcome,
        score: float | None = None,
        floor_origin: FloorOrigin | None = None,
    ) -> None:
        """Log one content-free outcome joined by ``decision_id``; a no-op when unrouted
        and for a D without an adapter (the call site's own fallback ran)."""
        pending = self._pending.pop(choice.decision_id, None)
        if self.router is None or pending is None or "role_pinned" in choice.codes:
            return
        if choice.adapter is None:
            return
        role, domain = pending
        self.router._record_outcome(role, choice, outcome, score, floor_origin, domain)
