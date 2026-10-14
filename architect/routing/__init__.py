"""Verified-version routing (Phase 10): the evidence router, its records and labels.

Routing orders licensed versions; it never licenses. Stage 4 remains the only
licensing authority. This package imports only ``architect.spec``, ``registry``,
``library``, ``telemetry``, ``models`` and ``presets``, the standard library and
pydantic.
"""

from architect.routing.evidence import (
    EvidenceIndex,
    EvidenceUnavailable,
    InstalledRef,
    RoleStats,
    TemplateRef,
    VersionEvidence,
    variant_category,
    wilson,
)
from architect.routing.labels import (
    PROVIDER_EVIDENCE,
    EvidenceClass,
    EvidenceLabel,
    FloorOrigin,
    Provider,
    RoleName,
    RouteCode,
    evidence_class,
)
from architect.routing.log import (
    Origin,
    OutcomeRecord,
    RatingRecord,
    RoleRecord,
    RouteAction,
    RouteLog,
    RouteLogError,
    RouteRecord,
)
from architect.routing.proposals import Proposal
from architect.routing.router import (
    CandidateView,
    RouteDecision,
    RoutedLibrary,
    RouteError,
    RoutePolicy,
    Router,
    TaskProbe,
    load_reuse_target,
    render_decision,
    route,
    route_record,
)
from architect.routing.signature import TaskSignature, fit, schema_skeleton, signature

__all__ = [
    "PROVIDER_EVIDENCE",
    "CandidateView",
    "EvidenceClass",
    "EvidenceIndex",
    "EvidenceLabel",
    "EvidenceUnavailable",
    "FloorOrigin",
    "InstalledRef",
    "Origin",
    "OutcomeRecord",
    "Proposal",
    "Provider",
    "RatingRecord",
    "RoleName",
    "RoleRecord",
    "RoleStats",
    "RouteAction",
    "RouteCode",
    "RouteDecision",
    "RouteError",
    "RouteLog",
    "RouteLogError",
    "RoutePolicy",
    "RouteRecord",
    "RoutedLibrary",
    "Router",
    "TaskProbe",
    "TaskSignature",
    "TemplateRef",
    "VersionEvidence",
    "evidence_class",
    "fit",
    "load_reuse_target",
    "render_decision",
    "route",
    "route_record",
    "schema_skeleton",
    "signature",
    "variant_category",
    "wilson",
]
