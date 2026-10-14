"""Closed routing labels shared by the router, the role router and the bench.

The evidence labels moved here from the bench (Phase 10 Section 7.2) so the bench
stays a leaf: ``architect.bench`` re-imports ``Provider``, ``EvidenceClass`` and
the provider-to-class map from this module, never the other way round.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal, get_args

__all__ = [
    "EVIDENCE_CLASSES",
    "PROVIDER_EVIDENCE",
    "ROLE_TASK_TYPES",
    "ROUTE_CODES",
    "EvidenceClass",
    "EvidenceLabel",
    "FloorOrigin",
    "Provider",
    "RoleName",
    "RouteCode",
    "evidence_class",
    "floor_origin_weight",
]

Provider = Literal["fake", "anthropic", "openai", "ollama", "claude-code", "codex"]
EvidenceClass = Literal["synthetic_fake", "local_zero_cost", "live_api_equivalent", "live_billed"]
FloorOrigin = Literal[
    "user_dataset",
    "example_fixture",
    "synthetic_generated",
    "model_draft",
    "authored_cases",
    # Phase 14: a floor whose cases match a pinned public manifest; never down-weighted.
    "public_dataset",
]
EvidenceLabel = (
    EvidenceClass
    | FloorOrigin
    | Literal[
        "stage4_signed",
        "latency_measured",
        "latency_bound",
        "drift_window_none",
        "drift_stable",
        "bench_joined",
        "human_rated",
        "template_promoted",
    ]
)
# Section 11.1. ``RoleName`` is re-exported by ``routing/roles.py`` (WP2); it lives
# here because the content-free ``RoleRecord`` in ``routing/log.py`` names it.
RoleName = Literal["inference", "decomposition", "rewrite", "forge_agent", "floor_draft"]

# The routing and role codes of Section 13.4 (one sentence each in session/codes.py).
RouteCode = Literal[
    "route_reuse_installed",
    "route_warm_start",
    "route_compile",
    "route_forge_suggested",
    "route_no_candidate",
    "route_unlicensed",
    "route_evicted",
    "route_consumed",
    "route_budget",
    "route_drift",
    "route_model_unconfigured",
    "route_synthetic_excluded",
    "route_reuse_not_granted",
    "route_no_reuse",
    "route_target_changed",
    "route_spec_required",
    "route_not_catalogued",
    "route_explore",
    "route_explore_cap",
    "route_promote_proposed",
    "route_rollback_suggested",
    "route_model_unknown",
    "route_param_invalid",
    "fit_slot",
    "fit_shape",
    "fit_family",
    "fit_none",
    "evidence_synthetic_only",
    "evidence_insufficient",
    "evidence_store_unavailable",
    "role_evidence_insufficient",
    "role_explore",
    "role_pinned",
]
ROUTE_CODES: frozenset[str] = frozenset(get_args(RouteCode))

# fake -> synthetic_fake; ollama -> local_zero_cost; HTTP APIs -> live_billed;
# CLI subscriptions -> live_api_equivalent. The bench's ``_EVIDENCE`` aliases this.
PROVIDER_EVIDENCE: dict[str, EvidenceClass] = {
    "fake": "synthetic_fake",
    "ollama": "local_zero_cost",
    "anthropic": "live_billed",
    "openai": "live_billed",
    "claude-code": "live_api_equivalent",
    "codex": "live_api_equivalent",
}
EVIDENCE_CLASSES: tuple[EvidenceClass, ...] = get_args(EvidenceClass)

# Telemetry ``task_type`` preset label per role (Section 11.1), before ``categorical``.
ROLE_TASK_TYPES: Mapping[RoleName, str] = MappingProxyType(
    {
        "inference": "inference",
        "decomposition": "orchestrator",
        "rewrite": "rewrite_proposal",
        "forge_agent": "forge_agent",
        "floor_draft": "floor_draft",
    }
)

_DOWN_WEIGHTED: frozenset[str] = frozenset(
    {"synthetic_generated", "model_draft", "authored_cases"}
)


def evidence_class(provider: Provider) -> EvidenceClass:
    """The evidence class a provider's measurements carry (Section 2)."""
    return PROVIDER_EVIDENCE[provider]


def floor_origin_weight(origin: FloorOrigin | None) -> float:
    """Section 7.2: user and fixture floors weigh 1.0; generated or drafted ones 0.5."""
    return 0.5 if origin in _DOWN_WEIGHTED else 1.0
