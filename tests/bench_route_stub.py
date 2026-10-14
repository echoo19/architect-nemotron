"""A test-local stand-in for ``architect.routing`` behind the bench's routing seam.

It is used only until the real router (WP1) lands in this checkout; it follows
the Section 8.1 decision rule in its simplest form over the routed cell's own
registry and library: an installed entry of the same slot is reused (the bench
always grants reuse), else a library template of the same shape warm-starts,
else a normal compile. It never licenses, installs or signs anything; the
licence is re-read from disk on every reuse, as ``load_reuse_target`` does.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Literal

from architect.bench.rows import Evidence
from architect.bench.runner import (
    RouteContext,
    RouteOption,
    RouteOutcome,
    RouteSession,
    RouteView,
)
from architect.compiler.types import LibraryLookup
from architect.routing.signature import signature
from architect.spec import Budget, HarnessArtifact, Spec

_BEATS = ("task", "input_schema", "output_schema", "domain", "tools", "floor", "secondary_floor")


def beats_fields(spec: Spec) -> dict[str, object]:
    return {name: getattr(spec, name) for name in _BEATS}


class StubRouteError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _Pinned:
    """``RoutedLibrary`` in miniature: the chosen template for its model, then the
    fallback library for store and verification."""

    def __init__(self, template: HarnessArtifact, fallback: object) -> None:
        self.template = template
        self.fallback = fallback

    def lookup(self, spec: Spec, model: str) -> HarnessArtifact | None:
        return self.template if model == self.template.model else None

    def store(self, artifact: HarnessArtifact) -> str:
        return self.fallback.store(artifact)  # type: ignore[attr-defined, no-any-return]

    def record_verification(self, source: HarnessArtifact, passed: bool) -> None:
        self.fallback.record_verification(source, passed)  # type: ignore[attr-defined]

    def verification_count(self, spec_hash: str) -> int:
        return self.fallback.verification_count(spec_hash)  # type: ignore[attr-defined, no-any-return]


@dataclass
class StubLog:
    """What every stub session of one bench appended, in order (content-free)."""

    decisions: list[tuple[str, str]] = field(default_factory=list)
    outcomes: list[tuple[str, bool | None, float]] = field(default_factory=list)


@dataclass
class StubSession:
    context: RouteContext
    log: StubLog
    empty: bool = False
    action: Literal["reuse_installed", "warm_start", "compile", "refuse"] | None = None
    min_template_reliability: float = 0.3
    _templates: dict[str, HarnessArtifact] = field(default_factory=dict)

    def route(self, spec: Spec, *, ladder: tuple[str, ...], budget_cap: Budget) -> RouteView:
        started = time.perf_counter()
        probe = signature(spec)
        options: list[RouteOption] = []
        if not self.empty:
            for entry in self.context.registry.list():
                artifact = self.context.registry.get(entry.name)
                other = signature(artifact.spec)
                if other.slot == probe.slot:
                    tier: Literal["A", "B", "C"] = "A"
                elif other.shape == probe.shape:
                    tier = "B"
                else:
                    continue
                options.append(
                    RouteOption(
                        origin="installed",
                        tier=tier,
                        eligible=artifact.licensed and artifact.model in ladder,
                        model=artifact.model,
                        name=entry.name,
                        revision=entry.revision,
                    )
                )
            template = self.context.library.lookup(spec, ladder[0])
            if template is not None:
                key = template.artifact_digest
                self._templates[key] = template
                options.append(
                    RouteOption(
                        origin="template", tier="B", eligible=True, model=template.model,
                        key=key, r=0.5,
                    )
                )
        installed = [i for i, o in enumerate(options) if o.tier == "A" and o.eligible]
        warm = [i for i, o in enumerate(options) if o.origin == "template" and o.eligible]
        codes: list[str] = []
        if installed:
            action, target, codes = "reuse_installed", installed[0], ["fit_slot"]
        elif warm:
            action, target, codes = "warm_start", warm[0], ["fit_shape"]
        else:
            action, target, codes = "compile", None, ["fit_none", "route_compile"]
        if self.action is not None:
            action = self.action
            target = None if action in ("compile", "refuse") else target
        eligible = [i for i, o in enumerate(options) if o.eligible]
        chosen = eligible.index(target) if target is not None else None
        self.log.decisions.append((action, codes[0]))
        return RouteView(
            action=action,
            tier={"fit_slot": "A", "fit_shape": "B"}.get(codes[0], "none"),  # type: ignore[arg-type]
            options=tuple(options),
            target=target,
            chosen_rank=chosen,
            explored=False,
            codes=("evidence_synthetic_only", *codes),
            decision_ms=(time.perf_counter() - started) * 1000.0,
            decision=None,
        )

    def reuse(self, view: RouteView, spec: Spec) -> HarnessArtifact:
        assert view.target is not None
        option = view.options[view.target]
        assert option.name is not None
        artifact = self.context.registry.get(option.name, option.revision)
        if not HarnessArtifact.load(artifact.directory).licensed:
            raise StubRouteError("route_unlicensed")
        if beats_fields(artifact.spec) != beats_fields(spec):
            raise StubRouteError("route_target_changed")
        return artifact

    def warm(self, view: RouteView, option: RouteOption) -> LibraryLookup | None:
        template = self._templates.get(option.key or "")
        return None if template is None else _Pinned(template, self.context.library)

    def outcome(
        self,
        view: RouteView,
        *,
        outcome: RouteOutcome,
        stage4_passed: bool | None,
        evidence: Evidence,
        compile_cost_usd: float,
    ) -> None:
        assert math.isfinite(compile_cost_usd)
        self.log.outcomes.append((outcome, stage4_passed, compile_cost_usd))


def stub_opener(
    log: StubLog,
    *,
    empty: bool = False,
    action: Literal["reuse_installed", "warm_start", "compile", "refuse"] | None = None,
) -> object:
    def opener(context: RouteContext) -> RouteSession:
        return StubSession(context, log, empty=empty, action=action)

    return opener
