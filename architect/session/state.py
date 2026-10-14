"""Session state: the state enum and the plain data the handlers and screens pass around.

Everything here is data the session owns. None of it is a license: ``Outcome.licensed``
re-reads the artifact from disk every time it is asked, because Stage 4 is the only
licensing authority and the session must never cache or copy its verdict.

``FloorSummary``, ``ModelRow``, ``Progress`` and ``MeterEvent`` are defined here rather
than in ``floors.py``/``bootstrap.py``/``observe.py`` so that ``render.py`` (which draws
them) and ``StatusView`` (which carries floor summaries) have no import dependency on the
modules that produce them; those modules import the dataclasses from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import JsonValue

from architect.compiler import CompileResult
from architect.floors import EvaluationCase
from architect.registry import RegistryEntry
from architect.spec import Budget, FloorKind, HarnessArtifact, Spec

__all__ = [
    "Draft",
    "DraftCase",
    "FloorDraft",
    "FloorSummary",
    "MeterEvent",
    "ModelRow",
    "Outcome",
    "Progress",
    "Spend",
    "State",
    "StatusView",
]


class State(StrEnum):
    BOOT = "boot"
    SETUP = "setup"
    READY = "ready"
    INFERRING = "inferring"
    REVIEW = "review"
    FLOOR = "floor"
    COMPILING = "compiling"
    RESULT = "result"
    BENCHING = "benching"
    EXIT = "exit"


@dataclass
class Spend:
    """Measured usage. Tokens are shown next to cost and are never a limit."""

    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    wall_s: float = 0.0

    def add(self, other: Spend) -> None:
        self.calls += other.calls
        self.tokens_in += other.tokens_in
        self.tokens_out += other.tokens_out
        self.cost_usd += other.cost_usd
        self.wall_s += other.wall_s


@dataclass
class Draft:
    """The Spec under review, with what is needed to revise and diff it."""

    spec: Spec
    origin: Literal["inferred", "file", "registry"]
    goal: str | None
    user_fields: dict[str, JsonValue]
    original: Spec
    edits: int = 0

    @property
    def hash8(self) -> str:
        return self.spec.spec_hash[:8]


@dataclass
class Outcome:
    """A finished compile. The license is always re-read from disk, never stored."""

    result: CompileResult
    approved_hash: str | None

    @property
    def licensed(self) -> bool:
        artifact = self.result.artifact
        if artifact is None or self.result.failures:
            return False
        try:
            return HarnessArtifact.load(artifact.directory).licensed
        except (OSError, ValueError, TypeError):
            return False


@dataclass
class DraftCase:
    case: EvaluationCase
    origin: Literal["operator", "file", "model_draft"]
    reviewed: bool


@dataclass
class FloorDraft:
    dataset: str
    kind: FloorKind
    cases: list[DraftCase]
    drafted_by_model: str | None
    return_state: State


@dataclass(frozen=True)
class FloorSummary:
    """Counts and labels of a configured floor; never its case contents."""

    dataset: str
    kind: FloorKind
    path: Path
    count: int
    tuning: int
    held_out: int
    synthetic: int
    drafted_by_model: str | None
    misses_allowed: int | None


@dataclass(frozen=True)
class ModelRow:
    """One line of the SETUP model menu, computed from quotes only."""

    index: int
    model: str
    provider: str
    executable: str
    min_call_cost: float
    can_infer: bool
    probe_cost: float
    probe_fits: bool


@dataclass(frozen=True)
class MeterEvent:
    kind: Literal["complete", "error"]
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    wall_s: float


@dataclass(frozen=True)
class Progress:
    """A content-free snapshot of a running compile for one progress line."""

    elapsed_s: float
    stage: int | None
    stage_text: str
    calls: int
    in_flight: int
    case_rows: int
    quality_rows: int
    rewrite_rows: int
    accepted_rows: int
    spend: Spend
    budget: Budget
    left_usd: float
    left_s: float
    failed: int = 0


@dataclass(frozen=True)
class StatusView:
    state: State
    workspace: Path
    profile_path: Path | None
    model: str | None
    inference_model: str | None
    floors: tuple[FloorSummary, ...]
    compile_budget: Budget
    draft: Draft | None
    outcome: Outcome | None
    last_entry: RegistryEntry | None
    session_spend: Spend
    compile_spend: Spend | None
    signing_key: Path
    # "benching <done>/<total> <task> <model>" while a bench runs; counts and labels only.
    bench: str | None = None
    # The last Forge progress line of this session's compile (Phase 9); counts only.
    forge: str | None = None
