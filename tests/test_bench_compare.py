"""Paired statistics and verdicts for bench compare (Phase 8 WP3)."""

from __future__ import annotations

import math
from typing import Any

import pytest
from test_bench_rows import make_row

from architect.bench.compare import (
    CellComparison,
    Comparison,
    align,
    compare,
    mcnemar_exact,
    overall,
    pct_text,
    verdict,
    wilson,
)
from architect.bench.rows import BenchError, BenchRow, encode_pass_bits

CAND_ID = "7d1f8e2a-9b3c-4e5d-8f6a-1b2c3d4e5f60"


# --- Wilson and McNemar against published values --------------------------------


@pytest.mark.parametrize(
    ("k", "n", "low", "high"),
    [
        # Newcombe (1998), Statistics in Medicine 17:857-872, Table I, method 3 (score).
        (81, 263, 0.2553, 0.3662),
        (15, 148, 0.0624, 0.1605),
        (0, 20, 0.0, 0.1611),
        (1, 29, 0.0061, 0.1718),
        (29, 29, 0.8830, 1.0),
    ],
)
def test_wilson_matches_newcombe(k: int, n: int, low: float, high: float) -> None:
    lower, upper = wilson(k, n)
    assert lower == pytest.approx(low, abs=5e-5)
    assert upper == pytest.approx(high, abs=5e-5)


def test_wilson_edges() -> None:
    assert wilson(0, 0) == (0.0, 1.0)
    narrow = wilson(17, 20, z=1.0)
    wide = wilson(17, 20)
    assert wide[0] < narrow[0] < 0.85 < narrow[1] < wide[1]
    with pytest.raises(ValueError):
        wilson(3, 2)
    with pytest.raises(ValueError):
        wilson(-1, 2)


@pytest.mark.parametrize(
    ("b", "c", "p"),
    [
        # Two-sided exact binomial on the discordant pairs, P(X <= min) * 2 at p = 1/2.
        (0, 5, 2 / 32),  # 0.0625: five-to-nothing is still not significant
        (0, 6, 2 / 64),  # 0.03125: the smallest all-one-way significant table
        (2, 8, 112 / 1024),  # 0.109375
        (1, 9, 22 / 1024),  # 0.021484375
        (5, 0, 2 / 32),  # symmetric
        (3, 3, 1.0),  # capped at 1
        (0, 0, 1.0),  # no discordant pairs, no evidence
    ],
)
def test_mcnemar_exact_small_tables(b: int, c: int, p: float) -> None:
    assert mcnemar_exact(b, c) == pytest.approx(p, rel=1e-12)


def test_mcnemar_exact_agresti_approval_table() -> None:
    # Agresti, Categorical Data Analysis, presidential approval: b = 86, c = 150.
    # The chi-square statistic is 17.36 (p ~ 3.1e-5); the exact test is slightly larger.
    p = mcnemar_exact(86, 150)
    assert 3.0e-5 < p < 5.0e-5
    with pytest.raises(ValueError):
        mcnemar_exact(-1, 2)


def _exact_reference(b: int, c: int) -> float:
    """Independent log-space reference: 2 * P(X <= min(b, c)), X ~ Binomial(b + c, 1/2)."""
    n, smaller = b + c, min(b, c)
    logs = [
        math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) - n * math.log(2.0)
        for i in range(smaller + 1)
    ]
    peak = max(logs)
    return min(1.0, 2.0 * math.exp(peak) * sum(math.exp(x - peak) for x in logs))


@pytest.mark.parametrize(
    ("b", "c"),
    [(500, 523), (512, 512), (600, 500), (1000, 1100), (1500, 1400), (3000, 3300), (0, 1100)],
)
def test_mcnemar_exact_does_not_overflow_past_1023_pairs(b: int, c: int) -> None:
    # 2.0 ** n overflows a float at n = 1024; --size 2048 and more reaches it.
    p = mcnemar_exact(b, c)
    assert 0.0 <= p <= 1.0
    assert p == mcnemar_exact(c, b)
    assert p == pytest.approx(_exact_reference(b, c), rel=1e-9, abs=1e-300)


def test_mcnemar_exact_large_tables_known_values() -> None:
    assert mcnemar_exact(512, 512) == 1.0
    # A 5% split of 2100 discordant pairs is significant; a balanced one is not.
    assert mcnemar_exact(1000, 1100) < 0.05
    assert mcnemar_exact(1050, 1051) == 1.0
    # One-way tables far past the float range of 2 ** n underflow to 0, not raise.
    assert mcnemar_exact(0, 1100) == 0.0
    assert mcnemar_exact(0, 5000) == 0.0


def test_compare_with_more_than_1023_discordant_pairs_returns_a_verdict() -> None:
    cases = 1200
    base = make_row(
        size=2 * cases,
        held_out_count=cases,
        tuning_cases=cases,
        tuning_pass_bits=encode_pass_bits([True] * cases),
    )
    cand = make_row(
        bench_id=CAND_ID,
        size=2 * cases,
        held_out_count=cases,
        held_out_score=0.0,
        licensed=False,
        tuning_cases=cases,
        tuning_pass_bits=encode_pass_bits([False] * cases),
    )
    comparison = compare([base], [cand])
    cell = comparison.cells[0]
    assert (cell.discordant_b, cell.discordant_c) == (cases, 0)
    assert cell.mcnemar_p == 0.0
    assert cell.verdict == "regression"
    assert comparison.verdict == "regression"


# --- alignment -------------------------------------------------------------------


def _pair(**overrides: Any) -> tuple[BenchRow, BenchRow]:
    return make_row(), make_row(bench_id=CAND_ID, **overrides)


def test_align_pairs_by_cell_key() -> None:
    base = [make_row(), make_row(task_id="sentiment-24", kind="text")]
    cand = [make_row(bench_id=CAND_ID, task_id="sentiment-24", kind="text"),
            make_row(bench_id=CAND_ID)]
    pairs = align(base, cand)
    assert [(a.task_id, b.task_id) for a, b in pairs] == [
        ("exam-clean-40", "exam-clean-40"),
        ("sentiment-24", "sentiment-24"),
    ]


@pytest.mark.parametrize(
    "overrides",
    [
        {"suite_version": "v2"},
        {"suite_digest": "e" * 64},
        {"tuning_cases": 11, "tuning_pass_bits": encode_pass_bits([True] * 11)},
        {"pages_manifest_sha256": "f" * 64},
        {"size": 40},  # different size: nothing pairs
        {"seed": 11},
    ],
)
def test_align_refuses_mismatches(overrides: dict[str, Any]) -> None:
    base, cand = _pair(**overrides)
    with pytest.raises(BenchError) as caught:
        align([base], [cand])
    assert caught.value.code == "bench_compare_mismatch"


def test_align_refuses_empty_and_duplicate_rows() -> None:
    with pytest.raises(BenchError):
        align([], [make_row()])
    with pytest.raises(BenchError):
        align([make_row(), make_row()], [make_row(bench_id=CAND_ID)])


def test_pages_manifest_only_binds_fake_rows() -> None:
    live = {"provider": "anthropic", "evidence": "live_billed", "model": "claude-haiku-4-5"}
    base = make_row(**live)
    cand = make_row(bench_id=CAND_ID, pages_manifest_sha256="f" * 64, **live)
    assert len(align([base], [cand])) == 1


def test_fingerprint_mismatch_needs_the_flag() -> None:
    base, cand = _pair(preset_fingerprint="9" * 64)
    with pytest.raises(BenchError) as caught:
        align([base], [cand])
    assert caught.value.code == "bench_compare_fingerprint"
    with pytest.raises(BenchError):
        compare([base], [cand])
    assert compare([base], [cand], allow_fingerprint_mismatch=True).verdict == "identical"


# --- cell verdicts ------------------------------------------------------------------

N = 20


def _bits(passes: int, total: int = N, *, offset: int = 0) -> str:
    return encode_pass_bits([offset <= i < offset + passes for i in range(total)])


def _row(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "held_out_count": N,
        "held_out_score": 0.9,
        "tuning_cases": N,
        "tuning_pass_bits": _bits(18),
        "licensed": True,
        "cost_usd": 0.01,
        "wall_s": 10.0,
    }
    values.update(overrides)
    return values


def _cell(base: dict[str, Any], cand: dict[str, Any]) -> CellComparison:
    comparison = compare([make_row(**_row(**base))], [make_row(bench_id=CAND_ID, **_row(**cand))])
    assert len(comparison.cells) == 1
    return comparison.cells[0]


def test_identical() -> None:
    cell = _cell({}, {})
    assert cell.verdict == "identical"
    assert (cell.discordant_b, cell.discordant_c, cell.mcnemar_p) == (0, 0, 1.0)
    assert (cell.base_k, cell.cand_k, cell.n_held_out) == (18, 18, N)
    assert cell.wilson_base == wilson(18, 20) == cell.wilson_cand
    assert cell.cost_delta_pct == 0.0 and cell.held_out_delta == 0.0


def test_regression_when_license_flips() -> None:
    # Even a significant paired win cannot outweigh losing the license.
    cell = _cell(
        {"tuning_pass_bits": _bits(10)}, {"licensed": False, "tuning_pass_bits": _bits(18)}
    )
    assert cell.discordant_c == 8 and cell.mcnemar_p is not None and cell.mcnemar_p < 0.05
    assert cell.verdict == "regression"


def test_regression_on_significant_held_out_loss() -> None:
    cell = _cell(
        {"licensed": False, "held_out_score": 0.85},
        {"licensed": False, "held_out_score": 0.75, "tuning_pass_bits": _bits(12)},
    )
    assert cell.held_out_delta == pytest.approx(-0.10)
    assert (cell.discordant_b, cell.discordant_c) == (6, 0)
    assert cell.verdict == "regression"


def test_held_out_loss_without_significance_is_inconclusive() -> None:
    cell = _cell(
        {"licensed": False, "held_out_score": 0.85},
        {"licensed": False, "held_out_score": 0.75, "tuning_pass_bits": _bits(14)},
    )
    assert (cell.discordant_b, cell.discordant_c) == (4, 0)  # fewer than 5 pairs
    assert cell.verdict == "inconclusive"


def test_regression_on_cost_rise_without_gain() -> None:
    assert _cell({}, {"cost_usd": 0.0125}).verdict == "regression"  # +25%
    assert _cell({}, {"cost_usd": 0.0119}).verdict == "inconclusive"  # +19%, scores equal
    # A held-out gain of 0.05 pays for the cost.
    gained = _cell({"held_out_score": 0.85}, {"held_out_score": 0.9, "cost_usd": 0.0125})
    assert gained.verdict == "inconclusive"


def test_improvement_on_significant_paired_win() -> None:
    cell = _cell({"tuning_pass_bits": _bits(10)}, {"tuning_pass_bits": _bits(17)})
    assert (cell.discordant_b, cell.discordant_c) == (0, 7)
    assert cell.verdict == "improvement"
    # Significant but the base wins the pairs: a held-out delta of 0 is not a regression.
    losing = _cell({"tuning_pass_bits": _bits(17)}, {"tuning_pass_bits": _bits(10)})
    assert losing.verdict == "inconclusive"


def test_improvement_on_large_held_out_gain_at_n_20() -> None:
    cell = _cell({"licensed": False, "held_out_score": 0.8}, {"held_out_score": 0.9})
    assert cell.held_out_delta == pytest.approx(0.10)
    assert cell.verdict == "improvement"
    small = _cell(
        {"licensed": False, "held_out_score": 0.8, "held_out_count": 10},
        {"held_out_score": 0.9, "held_out_count": 10},
    )
    assert small.verdict == "inconclusive"  # n_held_out < 20


def test_improvement_on_cheaper_or_faster_with_equal_scores() -> None:
    assert _cell({}, {"cost_usd": 0.0085}).verdict == "improvement"  # -15%
    assert _cell({}, {"wall_s": 8.5}).verdict == "improvement"  # -15%
    assert _cell({}, {"cost_usd": 0.009}).verdict == "inconclusive"  # -10%
    changed = _cell({}, {"cost_usd": 0.005, "tuning_pass_bits": _bits(18, offset=1)})
    assert (changed.discordant_b, changed.discordant_c) == (1, 1)
    assert changed.verdict == "inconclusive"  # scores not equal


def test_missing_scores_and_bits() -> None:
    # No artifact on either side: nothing to compare, and nothing differs.
    both = _cell(
        {"licensed": False, "held_out_score": None, "tuning_pass_bits": None},
        {"licensed": False, "held_out_score": None, "tuning_pass_bits": None},
    )
    assert both.held_out_delta == 0.0 and both.verdict == "identical"
    # Candidate lost its artifact: its tuning cases all count as failures.
    lost = _cell({"licensed": False}, {"licensed": False, "held_out_score": None,
                                       "tuning_pass_bits": None})
    assert lost.held_out_delta is None
    assert (lost.discordant_b, lost.discordant_c) == (18, 0)
    assert lost.verdict == "inconclusive"
    # Zero-cost base (local models): equal zero cost is no change; a new cost is +inf.
    free = _cell({"cost_usd": 0.0}, {"cost_usd": 0.0})
    assert free.cost_delta_pct == 0.0 and free.verdict == "identical"


def test_delivery_delta_and_percentages() -> None:
    cell = _cell({"delivery_score": 0.5, "delivery_cases": 4}, {"delivery_score": 0.75,
                                                                 "delivery_cases": 4,
                                                                 "wall_s": 12.0})
    assert cell.delivery_delta == pytest.approx(0.25)
    assert cell.wall_delta_pct == pytest.approx(20.0)
    assert _cell({}, {}).delivery_delta is None  # no delivery replay on either side


def test_verdict_is_a_pure_function_of_the_cell() -> None:
    cell = _cell({}, {})
    flipped = cell.model_copy(update={"licensed_cand": False})
    assert verdict(flipped) == "regression"
    assert verdict(cell) == "identical"


# --- overall and model agnosticism -----------------------------------------------------


def _verdict_cell(model: str, value: str) -> CellComparison:
    return _cell({}, {}).model_copy(update={"model": model, "verdict": value})


@pytest.mark.parametrize(
    ("verdicts", "per_model", "run"),
    [
        ({"a": ["improvement"], "b": ["regression"]}, {"a": "improvement", "b": "regression"},
         "regression"),  # helps one model, hurts another: rejected
        ({"a": ["improvement"], "b": ["identical"]}, {"a": "improvement", "b": "identical"},
         "improvement"),
        ({"a": ["improvement"], "b": ["inconclusive"]},
         {"a": "improvement", "b": "inconclusive"}, "improvement"),
        ({"a": ["identical"], "b": ["identical"]}, {"a": "identical", "b": "identical"},
         "identical"),
        ({"a": ["identical"], "b": ["inconclusive"]}, {"a": "identical", "b": "inconclusive"},
         "inconclusive"),
        ({"a": ["improvement", "regression"]}, {"a": "regression"}, "regression"),
        ({"a": ["improvement", "inconclusive"]}, {"a": "improvement"}, "improvement"),
        ({"a": ["identical", "inconclusive"]}, {"a": "inconclusive"}, "inconclusive"),
    ],
)
def test_overall(verdicts: dict[str, list[str]], per_model: dict[str, str], run: str) -> None:
    cells = [_verdict_cell(model, value) for model, values in verdicts.items() for value in values]
    assert overall(cells) == (per_model, run)


def test_overall_of_nothing_is_inconclusive() -> None:
    assert overall([]) == ({}, "inconclusive")


def test_compare_across_models_end_to_end() -> None:
    base = [
        make_row(**_row(model="haiku")),
        make_row(**_row(model="sonnet")),
    ]
    cand = [
        make_row(bench_id=CAND_ID, **_row(model="haiku", cost_usd=0.008)),
        make_row(bench_id=CAND_ID, **_row(model="sonnet", licensed=False)),
    ]
    comparison = compare(base, cand)
    assert comparison.per_model == {"haiku": "improvement", "sonnet": "regression"}
    assert comparison.verdict == "regression"
    assert comparison.base_id == make_row().bench_id and comparison.candidate_id == CAND_ID
    assert comparison.suite_version == "v1"
    assert comparison.evidence == ("synthetic_fake",)


# --- expect identical-scores -------------------------------------------------------------


def test_expect_identical_scores_passes_on_cost_only_changes() -> None:
    comparison = compare(
        [make_row(**_row())],
        [make_row(bench_id=CAND_ID, **_row(cost_usd=0.02, wall_s=1.0))],
        expect="identical-scores",
    )
    assert comparison.cells[0].verdict == "regression"  # the verdict is still data


@pytest.mark.parametrize(
    "overrides",
    [
        {"held_out_score": 0.85},
        {"tuning_pass_bits": _bits(17)},
        {"licensed": False},
    ],
)
def test_expect_identical_scores_fails(overrides: dict[str, Any]) -> None:
    with pytest.raises(BenchError) as caught:
        compare(
            [make_row(**_row())],
            [make_row(bench_id=CAND_ID, **_row(**overrides))],
            expect="identical-scores",
        )
    assert caught.value.code == "bench_expectation_failed"


def test_expect_identical_scores_needs_every_cell_paired() -> None:
    base = [make_row(**_row()), make_row(**_row(task_id="sentiment-24", kind="text"))]
    cand = [make_row(bench_id=CAND_ID, **_row())]
    assert compare(base, cand).verdict == "identical"
    with pytest.raises(BenchError) as caught:
        compare(base, cand, expect="identical-scores")
    assert caught.value.code == "bench_expectation_failed"


def test_new_cost_on_a_zero_base_is_an_unbounded_rise() -> None:
    # Same scores, a zero-cost base that starts costing money: the > 20% rule fires.
    paid = _cell({"cost_usd": 0.0}, {"cost_usd": 0.01})
    assert paid.cost_delta_pct == math.inf
    assert paid.verdict == "regression"
    assert pct_text(paid.cost_delta_pct) == "new cost"
    # ... unless the held-out gain rule applies (+0.05 or more).
    gain = _cell({"cost_usd": 0.0, "held_out_score": 0.5},
                 {"cost_usd": 0.01, "held_out_score": 0.75})
    assert gain.cost_delta_pct == math.inf and gain.verdict != "regression"
    # Wall time from zero is +inf too, and is never "faster".
    slow = _cell({"wall_s": 0.0}, {"wall_s": 1.0})
    assert slow.wall_delta_pct == math.inf and slow.verdict != "improvement"
    # The comparison serialises +inf as "Infinity" and parses it back; NaN is refused.
    assert '"cost_delta_pct":"Infinity"' in paid.model_dump_json()
    assert CellComparison.model_validate_json(paid.model_dump_json()) == paid
    with pytest.raises(ValueError):
        CellComparison.model_validate({**paid.model_dump(), "cost_delta_pct": math.nan})
    with pytest.raises(ValueError):
        CellComparison.model_validate({**paid.model_dump(), "held_out_delta": math.inf})
    assert pct_text(None) == "n/a" and pct_text(12.345) == "+12.3%" and pct_text(-15.0) == "-15.0%"


# --- Phase 9: --pair-variants BASE:CAND --------------------------------------------


def _arms(**changes: Any) -> list[BenchRow]:
    """default, template and forge rows of the same cells (same spec, seed and size)."""
    rows = []
    for variant, bits in (
        ("default", [True] * 6 + [False] * 6),
        ("template", [True] * 9 + [False] * 3),
        ("forge", [True] * 12),
    ):
        for task in ("sentiment-24", "tickets-40"):
            rows.append(
                make_row(
                    task_id=task,
                    kind="text",
                    variant=variant,
                    tuning_cases=12,
                    tuning_pass_bits=encode_pass_bits(bits),
                    **changes,
                )
            )
    return rows


def test_pair_variants_aligns_two_arms_within_one_file() -> None:
    rows = _arms()
    pairs = align(rows, rows, pair_variants=("default", "forge"))
    assert len(pairs) == 2
    assert all(base.variant == "default" and cand.variant == "forge" for base, cand in pairs)
    assert [base.task_id for base, _ in pairs] == ["sentiment-24", "tickets-40"]
    comparison = compare(rows, rows, pair_variants=("default", "forge"))
    assert comparison.pair_variants == ("default", "forge")
    # Six discordant pairs one way on each cell: a significant paired improvement.
    assert {cell.discordant_c for cell in comparison.cells} == {6}
    assert comparison.verdict == "improvement"
    assert Comparison.model_validate_json(comparison.model_dump_json()) == comparison


def test_pair_variants_across_two_runs_filters_each_side() -> None:
    a = _arms()
    b = [row.model_copy(update={"bench_id": CAND_ID}) for row in _arms()]
    comparison = compare(a, b, pair_variants=("template", "forge"))
    assert len(comparison.cells) == 2
    assert {cell.discordant_c for cell in comparison.cells} == {3}
    assert comparison.candidate_id == CAND_ID


def test_without_pair_variants_the_variant_stays_in_the_key() -> None:
    rows = _arms()
    assert len(align(rows, rows)) == 6
    assert compare(rows, rows).pair_variants is None


@pytest.mark.parametrize(
    "pair", [("default", "default"), ("default", "noisy"), ("noisy", "forge")]
)
def test_pair_variants_without_common_cells_or_same_arm_is_a_mismatch(
    pair: tuple[Any, Any],
) -> None:
    with pytest.raises(BenchError, match="bench_compare_mismatch"):
        compare(_arms(), _arms(), pair_variants=pair)


def test_pair_variants_refuses_arms_of_different_specs() -> None:
    rows = _arms()
    other = "f" * 64
    changed = [
        row.model_copy(update={"spec_hash": other}) if row.variant == "forge" else row
        for row in rows
    ]
    with pytest.raises(BenchError, match="bench_compare_mismatch"):
        compare(changed, changed, pair_variants=("default", "forge"))


def test_pair_variants_expectation_uses_only_the_paired_arms() -> None:
    rows = _arms()
    with pytest.raises(BenchError, match="bench_expectation_failed"):
        compare(rows, rows, pair_variants=("default", "forge"), expect="identical-scores")
    same = [row.model_copy(update={"tuning_pass_bits": encode_pass_bits([True] * 12)})
            for row in rows]
    result = compare(same, same, pair_variants=("default", "template"), expect="identical-scores")
    assert result.verdict == "identical"


def test_compare_table_names_the_paired_variants() -> None:
    from architect.bench.render import compare_table

    rows = _arms()
    text = compare_table(compare(rows, rows, pair_variants=("template", "forge")))
    assert "paired variants: template (base) -> forge (candidate), same spec" in text
    assert text.isascii() and all(len(line) <= 100 for line in text.splitlines())
    assert "paired variants" not in compare_table(compare(rows, rows))
