"""Shared compiler contracts; no private verification authority lives here."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from architect.compiler.forge_settings import ForgeSettings as ForgeSettings
from architect.floors import FloorEvaluator
from architect.models import ModelAdapter
from architect.routing.router import RouteDecision
from architect.spec import (
    Budget,
    CaseCounts,
    FloorDefinition,
    HarnessArtifact,
    PresetName,
    Spec,
    TestReport,
)

AdapterFactory = Callable[[str], ModelAdapter]
FloorFactory = Callable[[FloorDefinition], FloorEvaluator]


class _Result(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class CompileConfig(_Result):
    strategy: Literal["grid", "halving"] = "grid"
    model_ladder: tuple[str, ...] = ("fake",)
    allowed_presets: tuple[PresetName, ...] = ()
    max_candidates: int = Field(default=16, gt=0)
    # Stage 2 candidates per ladder rung, in ladder order (a pinned model is rung 0);
    # a rung past the end uses ``max_candidates``. With 1, Stage 2 measures only the
    # preset's first configuration (its plain prompt when nothing warm-starts it).
    # ``forge.max_rungs`` says which rungs Forge runs on; the others compile as without it.
    rung_candidates: tuple[Annotated[int, Field(gt=0)], ...] = ()
    max_rewrites: int = Field(default=3, ge=0)
    enable_rewriting: bool = True
    compile_budget: Budget = Field(
        default_factory=lambda: Budget(
            cost_usd=10,
            compute_s=120,
        )
    )
    # Opt-in Phase 9 Forge; the default runs no Forge code (Section 8.1).
    forge: ForgeSettings = Field(default_factory=ForgeSettings)

    def candidates_for(self, rung: int) -> int:
        """Stage 2 candidates for ladder rung ``rung`` (0-based)."""
        if 0 <= rung < len(self.rung_candidates):
            return self.rung_candidates[rung]
        return self.max_candidates


class Measurement(_Result):
    score: float = Field(ge=0, le=1)
    floor_passed: bool
    budget_passed: bool
    mean_cost_usd: float = Field(ge=0)
    mean_tokens: float | None = Field(default=None, ge=0)
    max_tokens: int = Field(ge=0)
    max_cost_usd: float = Field(ge=0)
    max_compute_s: float = Field(ge=0)
    sample_count: int = Field(ge=0)
    failures: tuple[str, ...] = ()
    # Phase 14: how the measured runs ended, filled on every measurement. Kept out of
    # dumps so BASELINE.json and rewrite prompts stay byte-identical; Stage 4 copies it
    # into the signed report and the Forge audit writes it explicitly.
    counts: CaseCounts | None = Field(default=None, exclude=True)


class CandidateEvaluation(_Result):
    artifact: HarnessArtifact
    measurement: Measurement


class TuneResult(_Result):
    best: CandidateEvaluation | None
    candidates: tuple[CandidateEvaluation, ...]
    warm_started: bool = False

    @property
    def passed(self) -> bool:
        return bool(
            self.best and self.best.measurement.floor_passed and self.best.measurement.budget_passed
        )


class RungOutcome(_Result):
    """One ladder rung as the compile left it, read from its signed Stage 4 report.

    Counts and aggregate scores only: no case, no per-case result and no verification
    id. It exists only in the ``CompileResult``, so it reaches a caller after
    ``compile()`` returns and never a process-wide observer. ``score``, ``limit`` and
    ``counts`` are None for a rung that stopped before Stage 4 (``codes`` says why);
    ``ships`` marks the licensed rung the compile returns. ``mean_cost_usd`` is the
    report's mean cost of one exam case (None when the report has none).
    """

    model: str
    preset: str | None = None
    licensed: bool = False
    ships: bool = False
    score: float | None = Field(default=None, ge=0, le=1)
    limit: float | None = Field(default=None, ge=0, le=1)
    cases: int = Field(default=0, ge=0)
    counts: CaseCounts | None = None
    codes: tuple[str, ...] = ()
    mean_cost_usd: float | None = Field(default=None, ge=0)


class CompileResult(_Result):
    spec: Spec
    artifact: HarnessArtifact | None = None
    report: TestReport | None = None
    approval_required: bool = False
    warm_started: bool = False
    stages: tuple[int, ...] = ()
    failures: tuple[str, ...] = ()
    # Phase 10 Section 10.2: the router's decision (advice or the executed action) and
    # whether the artifact is an installed entry reused instead of compiled. Routing
    # never licenses: a reused artifact is re-loaded and re-checked from disk.
    route: RouteDecision | None = None
    reused: bool = False
    # Every ladder rung of this compile in order (``RungOutcome``), for front ends that
    # draw the ladder. Kept out of dumps so a compile's JSON keeps its keys.
    rungs: tuple[RungOutcome, ...] = Field(default=(), exclude=True)


class LibraryLookup(Protocol):
    def lookup(self, spec: Spec, model: str) -> HarnessArtifact | None: ...


class NullLibrary:
    def lookup(self, spec: Spec, model: str) -> HarnessArtifact | None:
        return None


@runtime_checkable
class LibraryLifecycle(Protocol):
    def store(self, artifact: HarnessArtifact) -> str: ...

    def record_verification(self, source: HarnessArtifact, passed: bool) -> None: ...
