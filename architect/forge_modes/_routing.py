"""Seams to the Phase 10 routing core (WP1), role routing (WP2) and compiler (WP5).

Forge modes code against the signatures fixed in ``docs/interfaces/phase10-router.md``.
Each seam is fail-open (P5): when a routing piece is absent or fails, the Forge mode
behaves exactly as a plain Phase 9 compile and delivery, and never licenses anything.
"""

from __future__ import annotations

import importlib
import inspect
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal, cast, get_args

from architect.routing.labels import EvidenceClass, FloorOrigin, Provider, evidence_class

try:
    from architect.routing.router import (  # type: ignore[import-not-found, unused-ignore]
        InstalledRef,
        RouteDecision,
    )
except ImportError:  # WP1's router has not landed: structural mirrors (``_fallback``).
    from architect.forge_modes._fallback import (  # type: ignore[assignment, unused-ignore]
        InstalledRef,
        RouteDecision,
    )

__all__ = [
    "EvidenceClass",
    "FloorOrigin",
    "InstalledRef",
    "RouteDecision",
    "compile_kwargs",
    "evidence_class_of",
    "fixed_codes",
    "provider_of",
    "record_outcome",
    "record_role",
    "reused",
    "route_of",
    "standing_grant",
]

_CODE = re.compile(r"^[a-z0-9_]{1,64}$")
_PROVIDERS = frozenset(get_args(Provider))

Outcome = Literal["licensed", "unlicensed", "delivered", "failed", "discarded", "cancelled"]


def fixed_codes(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Only fixed categorical codes, in order, without repeats; never free text."""
    return tuple(dict.fromkeys(value for value in values if _CODE.fullmatch(value)))


def compile_kwargs(compile_method: Callable[..., Any], **wanted: Any) -> dict[str, Any]:
    """The Phase 10 ``Compiler.compile`` keywords (Section 10.2) the compiler accepts.

    Before WP5 lands, ``origin``/``reuse_installed``/``budget_cap`` are not parameters;
    they are then simply not passed and the compile is exactly today's.
    """
    try:
        parameters = inspect.signature(compile_method).parameters
    except (TypeError, ValueError):
        return {}
    if any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values()):
        return dict(wanted)
    return {key: value for key, value in wanted.items() if key in parameters}


def route_of(result: object) -> RouteDecision | None:
    """``CompileResult.route`` (WP5) when present and well formed, else None."""
    value = getattr(result, "route", None)
    if value is None:
        return None
    if isinstance(value, RouteDecision):
        return value
    try:
        return RouteDecision.model_validate(value, from_attributes=True)
    except Exception:
        return None


def reused(result: object) -> bool:
    """``CompileResult.reused`` (WP5): only a literal True counts."""
    return getattr(result, "reused", False) is True


def standing_grant(compiler: object, origin: str) -> bool:
    """P3: the profile's ``routing.reuse_origins`` grant for ``origin``.

    The router reuses an installed entry when ``probe.grant_reuse`` or the origin is in
    ``policy.reuse_origins``; a delivery step must honour exactly the same grant, read
    from the router this compile used (``--no-route`` removes it, and so the grant).
    """
    router = getattr(compiler, "router", None)
    policy = getattr(router, "policy", None)
    origins = getattr(policy, "reuse_origins", None)
    try:
        return origin != "http" and origin in (origins or ())
    except TypeError:
        return False


def evidence_class_of(provider: str | None) -> EvidenceClass | None:
    """Section 2's provider-to-class rule (``routing.labels``); None for an unknown provider."""
    if provider not in _PROVIDERS:
        return None
    return evidence_class(cast(Provider, provider))


def provider_of(services: object, model: str) -> str | None:
    """The configured provider of ``model`` in the services' profile, if any."""
    profile = getattr(services, "profile", None)
    models = getattr(profile, "models", None)
    config = models.get(model) if isinstance(models, dict) else None
    provider = getattr(config, "provider", None)
    if isinstance(provider, str):
        return provider
    for name in ("inference", "decomposition", "rewrite", "forge_agent"):
        named = getattr(profile, name, None)
        if getattr(named, "model", None) == model:
            value = getattr(named, "provider", None)
            return value if isinstance(value, str) else None
    return None


def record_outcome(
    services: object,
    decision: RouteDecision | None,
    outcome: Outcome,
    *,
    stage4_passed: bool | None,
    evidence_class: EvidenceClass | None,
    compile_cost_usd: float = 0.0,
    build_cost_usd: float = 0.0,
    delivery_cost_usd: float = 0.0,
) -> None:
    """Append one content-free ``OutcomeRecord`` through ``Services.router`` (fail-open)."""
    router = getattr(services, "router", None)
    if router is None or decision is None:
        return
    try:
        log: Any = importlib.import_module("architect.routing.log")
        record = log.OutcomeRecord(
            decision_id=decision.decision_id,
            timestamp=datetime.now(UTC),
            outcome=outcome,
            stage4_passed=stage4_passed,
            evidence_class=evidence_class,
            compile_cost_usd=max(0.0, compile_cost_usd),
            build_cost_usd=max(0.0, build_cost_usd),
            delivery_cost_usd=max(0.0, delivery_cost_usd),
        )
        router.outcome(decision, record)
    except Exception:
        return  # P5: routing records never break a delivery.


def record_role(
    services: object,
    choice: object | None,
    outcome: Literal["success", "failure", "partial", "excluded"],
    *,
    floor_origin: FloorOrigin | None = None,
) -> None:
    """``RoleAdapters.record`` for the ``forge_agent`` choice (fail-open, WP2)."""
    roles = getattr(services, "roles", None)
    record = getattr(roles, "record", None)
    if choice is None or not callable(record):
        return
    try:
        record(choice, outcome, None, floor_origin)
    except Exception:
        return
