"""Router metrics over content-free bench rows: oracle rank, regret, learning curve.

Everything here is pure arithmetic over ``BenchRow`` columns (Phase 10 Section 14):
no I/O, no model, no router. A routed row's ``RouteFields`` already holds the
oracle rank and regret the runner measured; this module defines how they are
computed (``oracle``), reports them per replay round (``learning_curve``) and
sets them against a hindsight oracle over the fixed arms (``regret_table``).

Honesty: every aggregate is keyed by evidence class and never sums, averages or
compares across classes (P7): each round point and each regret cell holds one class,
and a regret is measured against the hindsight best of its own class only.
``synthetic_fake`` numbers are labelled "plumbing, not quality" (Section 17.2).
Regret is measured on the held-out split Stage 4 used; Stage 4 still decides
every licence.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from architect.telemetry.schema import NonnegativeInt

from .rows import BenchRow, Evidence, ModelId, TaskId, Variant

__all__ = [
    "PLUMBING_NOTE",
    "LearningCurve",
    "OracleOption",
    "RegretCell",
    "RegretTable",
    "RoundPoint",
    "learning_curve",
    "oracle",
    "regret_table",
]

# Section 17.2: synthetic_fake numbers prove plumbing and decision determinism only.
PLUMBING_NOTE = "synthetic_fake numbers are plumbing, not quality"
# The fixed arms the hindsight oracle chooses among (Section 14 "Summaries").
ORACLE_ARMS: tuple[Variant, ...] = ("default", "template", "forge")
# Costs are float sums of per-call prices: equal to 1e-9 dollars is a tie, so float
# noise never reorders two options that spent the same.
_COST_DIGITS = 9
_PROMOTE = "route_promote_proposed"
_ROLLBACK = "route_rollback_suggested"


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class OracleOption(_Frozen):
    """One executable option of a probe: a candidate or the cold compile.

    ``score`` is the realized held-out score (None when nothing was licensed or
    scored, which ranks last); ``cost_usd`` is the compile cost that option spent
    ($0 for an installed reuse) and breaks score ties.
    """

    score: Annotated[float, Field(ge=0, le=1)] | None
    cost_usd: Annotated[float, Field(ge=0)]


def oracle(options: Sequence[OracleOption], executed: int) -> tuple[int, float]:
    """``(oracle_rank, regret)`` of option ``executed`` among ``options``.

    Options are ordered by realized held-out score (higher first, None last), then
    by cost (lower first; equal to 1e-9 dollars is a tie), then by given order.
    ``regret`` is the best realized score minus the executed one (0 when nothing
    scored).
    """
    if not 0 <= executed < len(options):
        raise ValueError("executed must index options")
    order = sorted(
        range(len(options)),
        key=lambda i: (
            options[i].score is None,
            -(options[i].score or 0.0),
            round(options[i].cost_usd, _COST_DIGITS),
            i,
        ),
    )
    scores = [item.score for item in options if item.score is not None]
    best = max(scores) if scores else 0.0
    mine = options[executed].score
    regret = best - (mine if mine is not None else 0.0) if scores else 0.0
    return order.index(executed), max(0.0, regret)


class RoundPoint(_Frozen):
    """One replay round of one evidence class (P7: classes are never pooled)."""

    round: NonnegativeInt
    evidence: Evidence
    rows: NonnegativeInt
    licensed_rate: Annotated[float, Field(ge=0, le=1)]
    spend_usd: Annotated[float, Field(ge=0)]
    mean_regret: float | None
    exploration_share: Annotated[float, Field(ge=0, le=1)]
    promote_proposals: NonnegativeInt
    rollback_proposals: NonnegativeInt


class LearningCurve(_Frozen):
    points: tuple[RoundPoint, ...]
    evidence: tuple[Evidence, ...]
    note: str | None


def _note(evidence: Sequence[Evidence]) -> str | None:
    return PLUMBING_NOTE if "synthetic_fake" in evidence else None


def learning_curve(rows: Sequence[BenchRow]) -> LearningCurve:
    """Per replay round and evidence class of the routed arm: licensed rate, spend
    (the routed arm's compile cost), mean regret, exploration share and proposal
    counts.

    Only ``routed`` rows with ``RouteFields`` take part; points are ordered by round,
    then evidence class. Each point holds one class: no number mixes classes (P7).
    """
    routed = [row for row in rows if row.variant == "routed" and row.route is not None]
    rounds: dict[tuple[int, Evidence], list[BenchRow]] = {}
    for row in routed:
        assert row.route is not None
        rounds.setdefault((row.route.round, row.evidence), []).append(row)
    points: list[RoundPoint] = []
    for number, evidence_class in sorted(rounds):
        members = rounds[(number, evidence_class)]
        routes = [row.route for row in members if row.route is not None]
        regrets = [route.regret for route in routes if route.regret is not None]
        points.append(
            RoundPoint(
                round=number,
                evidence=evidence_class,
                rows=len(members),
                licensed_rate=sum(1 for row in members if row.licensed) / len(members),
                spend_usd=sum(row.cost_usd for row in members),
                mean_regret=sum(regrets) / len(regrets) if regrets else None,
                exploration_share=sum(1 for route in routes if route.explored) / len(routes),
                promote_proposals=sum(1 for route in routes if _PROMOTE in route.codes),
                rollback_proposals=sum(1 for route in routes if _ROLLBACK in route.codes),
            )
        )
    evidence = tuple(sorted({row.evidence for row in routed}))
    return LearningCurve(points=tuple(points), evidence=evidence, note=_note(evidence))


class RegretCell(_Frozen):
    """One arm of one task and one evidence class against the hindsight oracle over
    the fixed arms of that same class.

    ``arm`` is a fixed variant, or ``routed:<probe>`` for the routed arm's exact
    and sibling probes (the foreign probe is scored on another task's floor and is
    left out). ``mean_regret`` is the hindsight-best held-out score of the same
    (task, seed, repeat, size) and evidence class minus this arm's, averaged over
    its rows; a class never borrows another class's best (P7).
    """

    task_id: TaskId
    arm: str
    model: ModelId
    rows: NonnegativeInt
    licensed_rate: Annotated[float, Field(ge=0, le=1)]
    mean_held_out: float | None
    mean_cost_usd: Annotated[float, Field(ge=0)]
    mean_regret: float | None
    evidence: Evidence


class RegretTable(_Frozen):
    cells: tuple[RegretCell, ...]
    oracle_arms: tuple[Variant, ...]
    oracle_models: tuple[ModelId, ...]
    evidence: tuple[Evidence, ...]
    note: str | None


_Key = tuple[str, int, int, int, str]
_Arm = tuple[str, str, str, str]


def _key(row: BenchRow) -> _Key:
    return (row.task_id, row.seed, row.repeat, row.size, row.evidence)


def _arm(row: BenchRow) -> str:
    if row.variant == "routed":
        route = row.route
        if route is None or route.probe == "foreign":
            return "skip"
        return f"routed:{route.probe}"
    return row.variant


def regret_table(rows: Sequence[BenchRow]) -> RegretTable:
    """Every arm against the hindsight oracle over ``default``, ``template`` and
    ``forge`` and every ladder model present: the best held-out score any of them
    reached on the same (task, seed, repeat, size) in the same evidence class. Rows
    without a score count as 0. Cells are keyed by class, never pooled (P7).
    """
    best: dict[_Key, float] = {}
    for row in rows:
        if row.variant in ORACLE_ARMS:
            score = row.held_out_score if row.held_out_score is not None else 0.0
            best[_key(row)] = max(best.get(_key(row), 0.0), score)
    groups: dict[_Arm, list[BenchRow]] = {}
    for row in rows:
        arm = _arm(row)
        if arm == "skip":
            continue
        groups.setdefault((row.task_id, arm, row.model, row.evidence), []).append(row)
    cells: list[RegretCell] = []
    for (_task, arm, model, _evidence), members in groups.items():
        scores = [row.held_out_score for row in members if row.held_out_score is not None]
        regrets = [
            best[_key(row)] - (row.held_out_score or 0.0)
            for row in members
            if _key(row) in best
        ]
        cells.append(
            RegretCell(
                task_id=members[0].task_id,
                arm=arm,
                model=model,
                rows=len(members),
                licensed_rate=sum(1 for row in members if row.licensed) / len(members),
                mean_held_out=sum(scores) / len(scores) if scores else None,
                mean_cost_usd=sum(row.cost_usd for row in members) / len(members),
                mean_regret=sum(regrets) / len(regrets) if regrets else None,
                evidence=members[0].evidence,
            )
        )
    cells.sort(key=lambda cell: (cell.task_id, cell.arm, cell.model, cell.evidence))
    evidence = tuple(sorted({row.evidence for row in rows}))
    return RegretTable(
        cells=tuple(cells),
        oracle_arms=tuple(arm for arm in ORACLE_ARMS if any(r.variant == arm for r in rows)),
        oracle_models=tuple(sorted({r.model for r in rows if r.variant in ORACLE_ARMS})),
        evidence=evidence,
        note=_note(evidence),
    )
