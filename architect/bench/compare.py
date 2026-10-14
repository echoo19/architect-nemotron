"""Paired comparison of two bench runs: Wilson intervals, exact McNemar, verdicts.

Held-out deltas are the licensing signal; paired tuning bits are the
comparison signal and are cases Stage 2 selected on (selection-biased, said
plainly). The verdict refuses to call a difference on fewer than 5 discordant
pairs. Verdicts advise the operator and change no registry, artifact or
preset.

``pair_variants=(BASE, CAND)`` (Phase 9) compares two arms of the same cells:
rows of variant BASE in ``a`` pair with rows of variant CAND in ``b`` on the key
without the variant, and ``a`` and ``b`` may be the same rows. Forge and template
cells share the default spec and seed, so these pairs are the same measurement
with a different Stage 3.

Routed rows (Phase 10) are one per probe and replay round, so their key also
carries ``(probe, round)``. Paired against another arm (``default:routed``), only
the routed arm's ``exact`` probe of round 0 takes part: it is the task's own spec,
the one the other arm measured.

``pair_arms`` (Phase 14 Section 12) pairs arms across models on the delivery
replay, which no build step saw: exact McNemar on the delivery pass bits with Holm
across every pair, task and repeat of one invocation, the paired Newcombe interval
for the accuracy difference (method 10) and a paired bootstrap interval for the
cost ratio. A row measured with ``--no-tuning-replay`` has no tuning bits, and the
legacy comparison treats it so, never as failing every tuning case.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from fractions import Fraction
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from .rows import (
    BenchError,
    BenchRow,
    Evidence,
    ModelId,
    SuiteVersion,
    TaskId,
    Variant,
    decode_pass_bits,
)

__all__ = [
    "ArmPair",
    "CellComparison",
    "Comparison",
    "PairedComparison",
    "Verdict",
    "align",
    "arm_label",
    "bootstrap_ratio",
    "compare",
    "holm",
    "mcnemar_exact",
    "newcombe_paired",
    "overall",
    "pair_arms",
    "pct_text",
    "verdict",
    "wilson",
]

Verdict = Literal["regression", "improvement", "inconclusive", "identical"]

_Key = tuple[str, str, str, int, int, int, str, int]
# "cost within 1e-9": a relative tolerance of 1e-9 expressed in percent, because a cell
# carries the percent change and not the two absolute costs.
_COST_TOLERANCE_PCT = 1e-7
# Thresholds compare differences of k/n fractions and percent changes, so 0.9 - 0.8 must
# count as the +0.10 it is and a 15% cut must not miss by a rounding error.
_EPS = 1e-9
NEW_COST = "new cost"
# A percent change is >= -100; +inf means "was zero, is now positive" (a new cost). It
# serialises as the JSON string "Infinity" and parses back; NaN is refused by ``ge``.
PctChange = Annotated[float, Field(ge=-100.0, allow_inf_nan=True)]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for ``k`` successes in ``n`` trials.

    ``n == 0`` carries no information and yields the full interval (0, 1).
    """
    if k < 0 or n < 0 or k > n:
        raise ValueError("Wilson interval needs 0 <= k <= n")
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    z2 = z * z
    denominator = 1.0 + z2 / n
    centre = (p + z2 / (2.0 * n)) / denominator
    half = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n)) / denominator
    return (max(0.0, centre - half), min(1.0, centre + half))


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact binomial test on the discordant pairs (``b`` vs ``c``).

    Under the null each discordant pair is a fair coin; the p-value is twice
    the one-sided tail of the smaller count, capped at 1. No pairs -> 1.0.
    """
    if b < 0 or c < 0:
        raise ValueError("Discordant counts must be nonnegative")
    n = b + c
    if n == 0:
        return 1.0
    smaller = min(b, c)
    # Exact rational arithmetic: 2.0**n and the float of the tail sum overflow once
    # n >= 1024, while the ratio itself is always in (0, 1] and converts cleanly.
    tail = Fraction(2 * sum(math.comb(n, i) for i in range(smaller + 1)), 1 << n)
    return min(1.0, float(tail))


class CellComparison(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, ser_json_inf_nan="strings"
    )

    task_id: TaskId
    model: ModelId
    variant: Variant
    seed: int
    repeat: int
    n_held_out: int
    base_k: int
    cand_k: int
    held_out_delta: float | None
    wilson_base: tuple[float, float]
    wilson_cand: tuple[float, float]
    licensed_base: bool
    licensed_cand: bool
    discordant_b: int
    discordant_c: int
    mcnemar_p: float | None
    cost_delta_pct: PctChange | None
    wall_delta_pct: PctChange | None
    delivery_delta: float | None
    verdict: Verdict


class Comparison(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, ser_json_inf_nan="strings"
    )

    base_id: str
    candidate_id: str
    suite_version: SuiteVersion
    suite_digest: str
    cells: list[CellComparison]
    per_model: dict[ModelId, Verdict]
    verdict: Verdict
    evidence: tuple[Evidence, ...]
    pair_variants: tuple[Variant, Variant] | None = None


def _key(row: BenchRow, *, ignore_variant: bool = False) -> _Key:
    variant = "" if ignore_variant else row.variant
    route = row.route
    probe, number = ("", 0) if route is None or ignore_variant else (route.probe, route.round)
    return (row.task_id, row.model, variant, row.seed, row.repeat, row.size, probe, number)


def _paired(row: BenchRow) -> bool:
    """A row that takes part in a variant pair: every row except a routed row other
    than the exact probe of round 0 (the only one measuring the task's own spec)."""
    if row.variant != "routed":
        return True
    return row.route is not None and row.route.probe == "exact" and row.route.round == 0


def _index(rows: Sequence[BenchRow], *, ignore_variant: bool = False) -> dict[_Key, BenchRow]:
    index: dict[_Key, BenchRow] = {}
    for row in rows:
        key = _key(row, ignore_variant=ignore_variant)
        if key in index:
            raise BenchError("bench_compare_mismatch")
        index[key] = row
    return index


def _select(
    a: Sequence[BenchRow],
    b: Sequence[BenchRow],
    pair_variants: tuple[Variant, Variant] | None,
) -> tuple[list[BenchRow], list[BenchRow]]:
    """Rows of variant BASE from ``a`` and CAND from ``b``; all rows without a pair."""
    if pair_variants is None:
        return list(a), list(b)
    base, cand = pair_variants
    if base == cand:
        raise BenchError("bench_compare_mismatch")
    return (
        [row for row in a if row.variant == base and _paired(row)],
        [row for row in b if row.variant == cand and _paired(row)],
    )


def align(
    a: Sequence[BenchRow],
    b: Sequence[BenchRow],
    *,
    allow_fingerprint_mismatch: bool = False,
    pair_variants: tuple[Variant, Variant] | None = None,
) -> list[tuple[BenchRow, BenchRow]]:
    """Pair rows by (task_id, model, variant, seed, repeat, size), plus (probe,
    round) for routed rows.

    With ``pair_variants=(BASE, CAND)`` only BASE rows of ``a`` and CAND rows of
    ``b`` take part, and the variant is left out of the key. Raises
    ``bench_compare_mismatch`` on different suite versions or digests, duplicate
    keys, no common cells, different ``tuning_cases`` in a pair, a different
    ``pages_manifest_sha256`` between two fake-provider rows, or a paired
    ``spec_hash`` that differs; raises ``bench_compare_fingerprint`` when
    ``preset_fingerprint`` differs unless ``allow_fingerprint_mismatch``.
    """
    a, b = _select(a, b, pair_variants)
    if not a or not b:
        raise BenchError("bench_compare_mismatch")
    versions = {row.suite_version for row in (*a, *b)}
    digests = {row.suite_digest for row in (*a, *b)}
    if len(versions) != 1 or len(digests) != 1:
        raise BenchError("bench_compare_mismatch")
    paired = pair_variants is not None
    left, right = _index(a, ignore_variant=paired), _index(b, ignore_variant=paired)
    pairs: list[tuple[BenchRow, BenchRow]] = []
    for key, base in left.items():
        cand = right.get(key)
        if cand is None:
            continue
        if base.tuning_cases != cand.tuning_cases:
            raise BenchError("bench_compare_mismatch")
        if paired and base.spec_hash != cand.spec_hash:
            # Two arms are comparable only when they measured the same spec.
            raise BenchError("bench_compare_mismatch")
        if (
            base.provider == "fake"
            and cand.provider == "fake"
            and base.pages_manifest_sha256 != cand.pages_manifest_sha256
        ):
            raise BenchError("bench_compare_mismatch")
        if base.preset_fingerprint != cand.preset_fingerprint and not allow_fingerprint_mismatch:
            raise BenchError("bench_compare_fingerprint")
        pairs.append((base, cand))
    if not pairs:
        raise BenchError("bench_compare_mismatch")
    return pairs


def _bits(row: BenchRow) -> list[bool] | None:
    """Decoded tuning bits; a row without bits (no artifact) failed every tuning case.

    A row measured without the tuning replay has no bits at all, never all failed.
    """
    if not row.tuning_replay:
        return None
    if row.tuning_pass_bits is None:
        return None if row.tuning_cases == 0 else [False] * row.tuning_cases
    try:
        return decode_pass_bits(row.tuning_pass_bits, row.tuning_cases)
    except ValueError as exc:
        raise BenchError("bench_compare_mismatch") from exc


def _pct(base: float, cand: float) -> float:
    """Percent change; 0.0 when both are zero, +inf when a zero base became positive.

    A zero-cost base (a local model, a cached run) that starts costing money is an
    unbounded rise, so the > 20% cost rule fires unless the held-out gain rule applies.
    """
    if base <= 0:
        return 0.0 if cand <= 0 else math.inf
    return (cand - base) / base * 100.0


def pct_text(value: float | None, *, new: str = NEW_COST) -> str:
    """Table text for a percent change: ``+inf`` renders as ``new`` ("new cost")."""
    if value is None:
        return "n/a"
    if math.isinf(value):
        return new
    return f"{value:+.1f}%"


def _delta(base: float | None, cand: float | None) -> float | None:
    """Candidate minus base; 0.0 when neither has a score, None when only one has."""
    if base is None and cand is None:
        return 0.0
    if base is None or cand is None:
        return None
    return cand - base


def _cell(base: BenchRow, cand: BenchRow) -> CellComparison:
    n = base.held_out_count
    base_k = round(base.held_out_score * n) if base.held_out_score is not None else 0
    cand_n = cand.held_out_count
    cand_k = round(cand.held_out_score * cand_n) if cand.held_out_score is not None else 0
    base_bits, cand_bits = _bits(base), _bits(cand)
    b = c = 0
    p: float | None = None
    if base_bits and cand_bits:
        for x, y in zip(base_bits, cand_bits, strict=True):
            if x and not y:
                b += 1
            elif y and not x:
                c += 1
        p = mcnemar_exact(b, c)
    provisional = CellComparison(
        task_id=base.task_id,
        model=base.model,
        variant=base.variant,
        seed=base.seed,
        repeat=base.repeat,
        n_held_out=n,
        base_k=base_k,
        cand_k=cand_k,
        held_out_delta=_delta(base.held_out_score, cand.held_out_score),
        wilson_base=wilson(base_k, n),
        wilson_cand=wilson(cand_k, cand_n),
        licensed_base=base.licensed,
        licensed_cand=cand.licensed,
        discordant_b=b,
        discordant_c=c,
        mcnemar_p=p,
        cost_delta_pct=_pct(base.cost_usd, cand.cost_usd),
        wall_delta_pct=_pct(base.wall_s, cand.wall_s),
        delivery_delta=(
            None
            if base.delivery_score is None or cand.delivery_score is None
            else cand.delivery_score - base.delivery_score
        ),
        verdict="inconclusive",
    )
    return provisional.model_copy(update={"verdict": verdict(provisional)})


def _scores_equal(cell: CellComparison) -> bool:
    return (
        cell.held_out_delta == 0.0
        and cell.discordant_b == 0
        and cell.discordant_c == 0
        and cell.licensed_base == cell.licensed_cand
    )


def verdict(cell: CellComparison) -> Verdict:
    """Apply the fixed cell rule, in order: regression, improvement, identical."""
    delta = cell.held_out_delta
    significant = (
        cell.mcnemar_p is not None
        and cell.mcnemar_p < 0.05
        and cell.discordant_b + cell.discordant_c >= 5
    )
    gain = delta if delta is not None else 0.0
    if cell.licensed_base and not cell.licensed_cand:
        return "regression"
    if delta is not None and delta <= -0.05 + _EPS and significant:
        return "regression"
    if cell.cost_delta_pct is not None and cell.cost_delta_pct > 20.0 + _EPS and gain < 0.05 - _EPS:
        return "regression"
    if significant and cell.discordant_c > cell.discordant_b:
        return "improvement"
    if delta is not None and delta >= 0.10 - _EPS and cell.n_held_out >= 20:
        return "improvement"
    cheaper = cell.cost_delta_pct is not None and cell.cost_delta_pct <= -15.0 + _EPS
    faster = cell.wall_delta_pct is not None and cell.wall_delta_pct <= -15.0 + _EPS
    if _scores_equal(cell) and (cheaper or faster):
        return "improvement"
    if (
        _scores_equal(cell)
        and cell.cost_delta_pct is not None
        and abs(cell.cost_delta_pct) <= _COST_TOLERANCE_PCT
    ):
        return "identical"
    return "inconclusive"


def _model_verdict(verdicts: Sequence[Verdict]) -> Verdict:
    if any(v == "regression" for v in verdicts):
        return "regression"
    if any(v == "improvement" for v in verdicts):
        return "improvement"
    if all(v == "identical" for v in verdicts):
        return "identical"
    return "inconclusive"


def overall(cells: Sequence[CellComparison]) -> tuple[dict[ModelId, Verdict], Verdict]:
    """Per-model verdicts and the model-agnostic run verdict."""
    by_model: dict[str, list[Verdict]] = {}
    for cell in cells:
        by_model.setdefault(cell.model, []).append(cell.verdict)
    per_model: dict[str, Verdict] = {
        model: _model_verdict(verdicts) for model, verdicts in by_model.items()
    }
    values = list(per_model.values())
    if not values:
        return per_model, "inconclusive"
    if any(v == "regression" for v in values):
        return per_model, "regression"
    if any(v == "improvement" for v in values) and all(
        v in {"improvement", "identical", "inconclusive"} for v in values
    ):
        return per_model, "improvement"
    if all(v == "identical" for v in values):
        return per_model, "identical"
    return per_model, "inconclusive"


def compare(
    a: Sequence[BenchRow],
    b: Sequence[BenchRow],
    *,
    allow_fingerprint_mismatch: bool = False,
    expect: Literal["identical-scores"] | None = None,
    pair_variants: tuple[Variant, Variant] | None = None,
) -> Comparison:
    """Compare base rows ``a`` with candidate rows ``b``.

    ``pair_variants=(BASE, CAND)`` compares variant BASE in ``a`` with CAND in
    ``b`` (Section 13); ``a`` and ``b`` may be the same run.
    """
    pairs = align(
        a, b, allow_fingerprint_mismatch=allow_fingerprint_mismatch, pair_variants=pair_variants
    )
    a, b = _select(a, b, pair_variants)
    cells = [_cell(base, cand) for base, cand in pairs]
    if expect == "identical-scores":
        # The CI gate: every cell on both sides must pair, and scores must match exactly.
        if len(pairs) != len(a) or len(pairs) != len(b):
            raise BenchError("bench_expectation_failed")
        for base, cand in pairs:
            if (
                base.held_out_score != cand.held_out_score
                or base.tuning_pass_bits != cand.tuning_pass_bits
                or base.licensed != cand.licensed
            ):
                raise BenchError("bench_expectation_failed")
    per_model, run_verdict = overall(cells)
    evidence = tuple(sorted({row.evidence for row in (*a, *b)}))
    return Comparison(
        base_id=a[0].bench_id,
        candidate_id=b[0].bench_id,
        suite_version=a[0].suite_version,
        suite_digest=a[0].suite_digest,
        cells=cells,
        per_model=per_model,
        verdict=run_verdict,
        evidence=evidence,
        pair_variants=pair_variants,
    )


# ------------------------------------------------------------- paired arms (Phase 14)


# A cost ratio is ``inf`` when a zero-cost base meets a costing candidate; it
# serialises as the JSON string "Infinity". NaN is refused by ``ge``.
Ratio = Annotated[float, Field(ge=0, allow_inf_nan=True)]
_VARIANTS: frozenset[str] = frozenset(get_args(Variant))


class ArmPair(BaseModel):
    """One ``(pair, task, repeat)`` cell of a paired comparison; counts and statistics."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, ser_json_inf_nan="strings"
    )

    base: str  # the selector as given
    cand: str
    task_id: TaskId
    repeat: int
    n: int
    base_k: int
    cand_k: int
    # Each row's invalid and over-budget runs over the paired cases; None when unknown
    # (a subset of an older row, which kept only its totals).
    base_invalid: int | None
    cand_invalid: int | None
    base_over_budget: int | None
    cand_over_budget: int | None
    n11: int
    n10: int
    n01: int
    n00: int
    diff: float
    diff_ci: tuple[float, float]
    wilson_base: tuple[float, float]
    wilson_cand: tuple[float, float]
    mcnemar_p: float
    holm_p: float
    better: bool
    worse: bool
    margin: float
    noninferior: bool
    base_cost_per_run: float | None
    cand_cost_per_run: float | None
    cost_ratio: Ratio | None
    cost_ratio_ci: tuple[Ratio, Ratio] | None


class PairedComparison(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, ser_json_inf_nan="strings"
    )

    suite_version: SuiteVersion
    suite_digest: str
    alpha: float
    margin: float
    bootstrap: int
    seed: int
    pairs: list[ArmPair]
    evidence: tuple[Evidence, ...]
    floor_origins: tuple[str, ...]


def newcombe_paired(
    n11: int, n10: int, n01: int, n00: int, z: float = 1.96
) -> tuple[float, float]:
    """Newcombe's paired interval (method 10, hybrid score, no continuity correction)
    for ``(cand_k - base_k) / n``, where the base passes in ``n11 + n10`` cases and
    the candidate in ``n11 + n01``; clamped to [-1, 1]."""
    if min(n11, n10, n01, n00) < 0:
        raise ValueError("Paired counts must be nonnegative")
    n = n11 + n10 + n01 + n00
    if n == 0:
        return (-1.0, 1.0)
    base_k, cand_k = n11 + n10, n11 + n01
    pb, pc = base_k / n, cand_k / n
    diff = (cand_k - base_k) / n
    lb, ub = wilson(base_k, n, z)
    lc, uc = wilson(cand_k, n, z)
    product = (n11 + n01) * (n10 + n00) * (n11 + n10) * (n01 + n00)
    phi = (n11 * n00 - n10 * n01) / math.sqrt(product) if product else 0.0
    lower = diff - math.sqrt(
        max(0.0, (pc - lc) ** 2 - 2 * phi * (pc - lc) * (ub - pb) + (ub - pb) ** 2)
    )
    upper = diff + math.sqrt(
        max(0.0, (pb - lb) ** 2 - 2 * phi * (pb - lb) * (uc - pc) + (uc - pc) ** 2)
    )
    return (max(-1.0, lower), min(1.0, upper))


def holm(p_values: Sequence[float]) -> list[float]:
    """Holm-adjusted p-values in input order: sorted ascending (ties by input order),
    rank ``i`` from 0 gets the running maximum of ``min(1, (m - i) * p)``."""
    m = len(p_values)
    order = sorted(range(m), key=lambda index: (p_values[index], index))
    adjusted = [0.0] * m
    running = 0.0
    for rank, index in enumerate(order):
        running = max(running, min(1.0, (m - rank) * p_values[index]))
        adjusted[index] = running
    return adjusted


def _ratio(cand: float, base: float) -> float:
    """``cand / base``; ``inf`` for a zero base and a costing candidate, 1.0 for two zeros."""
    if base == 0:
        return 1.0 if cand == 0 else math.inf
    return cand / base


def bootstrap_ratio(
    base: Sequence[float], cand: Sequence[float], *, resamples: int, seed: int
) -> tuple[float, float]:
    """The paired percentile bootstrap 95% interval of ``sum(cand) / sum(base)``.

    One fresh ``random.Random(seed)``; each resample draws ``n`` indexes with
    ``randrange(n)`` and sums both vectors in draw order with ``sum``. After sorting,
    the bounds are the values at ``floor(0.025 * B)`` and ``ceil(0.975 * B) - 1``,
    computed in exact integer arithmetic.
    """
    n = len(base)
    if n == 0 or len(cand) != n:
        raise ValueError("The bootstrap needs two cost vectors of one nonzero length")
    if resamples < 1:
        raise ValueError("The bootstrap needs at least one resample")
    rng = random.Random(seed)
    ratios: list[float] = []
    for _ in range(resamples):
        draws = [rng.randrange(n) for _ in range(n)]
        ratios.append(_ratio(sum(cand[i] for i in draws), sum(base[i] for i in draws)))
    ratios.sort()
    lower = (25 * resamples) // 1000  # floor(0.025 * B)
    upper = -(-975 * resamples // 1000) - 1  # ceil(0.975 * B) - 1
    return ratios[lower], ratios[upper]


def arm_label(row: BenchRow) -> str:
    """A row's arm: its run label, else ``model:variant``."""
    return row.label or f"{row.model}:{row.variant}"


_Selector = tuple[str | None, str | None, str | None]  # (label, model, variant)


def _selector(text: str) -> _Selector:
    """``MODEL:VARIANT`` split at the last ``:``, else a run label (labels hold no ``:``)."""
    if ":" in text:
        model, _, variant = text.rpartition(":")
        if variant not in _VARIANTS:
            raise BenchError("bench_variant_unknown")
        return None, model, variant
    return text, None, None


def _selected(row: BenchRow, selector: _Selector) -> bool:
    label, model, variant = selector
    if not _paired(row):
        return False  # a routed row other than the exact probe of round 0
    if label is not None:
        return row.label == label
    return row.model == model and row.variant == variant


def _one(rows: Sequence[BenchRow], selector: _Selector) -> dict[tuple[str, int], BenchRow]:
    """``(task_id, repeat) -> row`` of one arm; two rows on one key cannot pair."""
    found: dict[tuple[str, int], BenchRow] = {}
    for row in rows:
        if not _selected(row, selector):
            continue
        key = (row.task_id, row.repeat)
        if key in found:
            raise BenchError("bench_compare_mismatch")
        found[key] = row
    return found


def _delivery_bits(row: BenchRow) -> list[bool]:
    if row.delivery_pass_bits is None or row.delivery_cases < 1:
        raise BenchError("bench_compare_mismatch")
    try:
        return decode_pass_bits(row.delivery_pass_bits, row.delivery_cases)
    except ValueError:
        raise BenchError("bench_compare_mismatch") from None


def _costs(row: BenchRow) -> tuple[float, ...] | None:
    costs = row.delivery_run_costs_usd
    if costs is not None and len(costs) != row.delivery_cases:
        raise BenchError("bench_compare_mismatch")
    return costs


def _check_pair(base: BenchRow, cand: BenchRow, allow_fingerprint_mismatch: bool) -> None:
    """Section 12.1: one delivery set measured on both sides; spec_hash may differ."""
    if (
        base.delivery_pass_bits is None
        or cand.delivery_pass_bits is None
        or base.delivery_cases_sha256 is None
        or base.delivery_cases_sha256 != cand.delivery_cases_sha256
        or base.delivery_cases != cand.delivery_cases
    ):
        raise BenchError("bench_compare_mismatch")
    if base.preset_fingerprint != cand.preset_fingerprint and not allow_fingerprint_mismatch:
        raise BenchError("bench_compare_fingerprint")


def _arm_pair(
    base_text: str,
    cand_text: str,
    base: BenchRow,
    cand: BenchRow,
    *,
    margin: float,
    bootstrap: int,
    seed: int,
) -> ArmPair:
    """One cell's statistics; ``holm_p``, ``better`` and ``worse`` are set afterwards."""
    base_bits, cand_bits = _delivery_bits(base), _delivery_bits(cand)
    n = len(base_bits)
    n11 = sum(1 for b, c in zip(base_bits, cand_bits, strict=True) if b and c)
    n10 = sum(1 for b, c in zip(base_bits, cand_bits, strict=True) if b and not c)
    n01 = sum(1 for b, c in zip(base_bits, cand_bits, strict=True) if c and not b)
    n00 = n - n11 - n10 - n01
    base_k, cand_k = n11 + n10, n11 + n01
    diff_ci = newcombe_paired(n11, n10, n01, n00)
    base_costs, cand_costs = _costs(base), _costs(cand)
    cost_ratio: float | None = None
    cost_ratio_ci: tuple[float, float] | None = None
    base_cost: float | None = None
    cand_cost: float | None = None
    if base_costs is not None and cand_costs is not None:
        base_cost, cand_cost = sum(base_costs) / n, sum(cand_costs) / n
        cost_ratio = _ratio(sum(cand_costs), sum(base_costs))
        cost_ratio_ci = bootstrap_ratio(base_costs, cand_costs, resamples=bootstrap, seed=seed)
    p = mcnemar_exact(n10, n01)
    return ArmPair(
        base=base_text,
        cand=cand_text,
        task_id=base.task_id,
        repeat=base.repeat,
        n=n,
        base_k=base_k,
        cand_k=cand_k,
        base_invalid=base.delivery_invalid,
        cand_invalid=cand.delivery_invalid,
        base_over_budget=base.delivery_over_budget,
        cand_over_budget=cand.delivery_over_budget,
        n11=n11,
        n10=n10,
        n01=n01,
        n00=n00,
        diff=(cand_k - base_k) / n,
        diff_ci=diff_ci,
        wilson_base=wilson(base_k, n),
        wilson_cand=wilson(cand_k, n),
        mcnemar_p=p,
        holm_p=p,
        better=False,
        worse=False,
        margin=margin,
        noninferior=diff_ci[0] > -margin,
        base_cost_per_run=base_cost,
        cand_cost_per_run=cand_cost,
        cost_ratio=cost_ratio,
        cost_ratio_ci=cost_ratio_ci,
    )


def pair_arms(
    rows: Sequence[BenchRow],
    hypotheses: Sequence[tuple[str, str]],
    *,
    alpha: float = 0.05,
    margin: float = 0.05,
    bootstrap: int = 10_000,
    seed: int = 0,
    allow_fingerprint_mismatch: bool = False,
) -> PairedComparison:
    """Pair every ``(BASE, CAND)`` hypothesis on its common ``(task_id, repeat)`` cells.

    A selector is ``MODEL:VARIANT`` or a run label. Each cell needs exactly one row
    per side; every row used must share one suite version and digest; both rows of a
    cell need delivery pass bits over the same replayed cases (equal non-null
    ``delivery_cases_sha256`` and ``delivery_cases``), else ``bench_compare_mismatch``.
    Presets must match unless ``allow_fingerprint_mismatch``
    (``bench_compare_fingerprint``). A hypothesis with no common cell is a mismatch.
    """
    if not hypotheses:
        raise BenchError("bench_compare_mismatch")
    cells: list[ArmPair] = []
    used: list[BenchRow] = []
    for base_text, cand_text in hypotheses:
        base_rows = _one(rows, _selector(base_text))
        cand_rows = _one(rows, _selector(cand_text))
        keys = [key for key in base_rows if key in cand_rows]
        if not keys:
            raise BenchError("bench_compare_mismatch")
        for key in sorted(keys):
            base, cand = base_rows[key], cand_rows[key]
            used.extend((base, cand))
            if len({(row.suite_version, row.suite_digest) for row in used}) != 1:
                raise BenchError("bench_compare_mismatch")
            _check_pair(base, cand, allow_fingerprint_mismatch)
            cells.append(
                _arm_pair(
                    base_text, cand_text, base, cand,
                    margin=margin, bootstrap=bootstrap, seed=seed,
                )
            )
    adjusted = holm([cell.mcnemar_p for cell in cells])
    pairs = [
        cell.model_copy(
            update={
                "holm_p": value,
                "better": value < alpha and cell.n01 > cell.n10,
                "worse": value < alpha and cell.n10 > cell.n01,
            }
        )
        for cell, value in zip(cells, adjusted, strict=True)
    ]
    return PairedComparison(
        suite_version=used[0].suite_version,
        suite_digest=used[0].suite_digest,
        alpha=alpha,
        margin=margin,
        bootstrap=bootstrap,
        seed=seed,
        pairs=pairs,
        evidence=tuple(sorted({row.evidence for row in used})),
        floor_origins=tuple(sorted({row.floor_origin for row in used})),
    )
