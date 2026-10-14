"""Paired comparison across arms on delivery cases (Phase 14 Section 12), offline.

The statistics match the contract's reference vectors and independent formulas; the
pairing refuses rows that did not measure the same delivery cases.
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path

import pytest
from bench_public_support import public_row, table_rows

from architect.bench.compare import (
    ArmPair,
    PairedComparison,
    bootstrap_ratio,
    compare,
    holm,
    mcnemar_exact,
    newcombe_paired,
    pair_arms,
)
from architect.bench.render import pairs_table
from architect.bench.rows import BenchError, write_rows
from architect.cli import main as cli_main

# (n11, n10, n01, n00) -> diff, Newcombe 95% and the exact McNemar p (Section 12.2).
REFERENCE = [
    ((300, 20, 45, 135), 0.05, (0.018680047579, 0.081241471210), 0.0026263768106840383),
    ((80, 5, 12, 3), 0.07, (-0.011602415443, 0.154723316867), 0.143463134765625),
    ((50, 0, 0, 50), 0.0, (0.0, 0.0), 1.0),
]


@pytest.mark.parametrize(("cells", "diff", "interval", "p"), REFERENCE)
def test_reference_vectors(
    cells: tuple[int, int, int, int], diff: float, interval: tuple[float, float], p: float
) -> None:
    n11, n10, n01, n00 = cells
    lower, upper = newcombe_paired(n11, n10, n01, n00)
    assert lower == pytest.approx(interval[0], abs=5e-13)
    assert upper == pytest.approx(interval[1], abs=5e-13)
    assert mcnemar_exact(n10, n01) == pytest.approx(p, rel=1e-12)
    base, cand = table_rows(*cells)
    rows = [
        public_row(label="A3", passed=base),
        public_row(label="A7", model="ultra", passed=cand, spec_hash="f" * 64),
    ]
    [pair] = pair_arms(rows, [("A3", "A7")]).pairs
    assert (pair.n11, pair.n10, pair.n01, pair.n00) == cells
    assert pair.n == sum(cells) and pair.diff == pytest.approx(diff, abs=1e-15)
    assert pair.diff_ci == pytest.approx(interval, abs=5e-13)
    assert pair.mcnemar_p == pytest.approx(p, rel=1e-12) and pair.holm_p == pair.mcnemar_p
    assert (pair.base_k, pair.cand_k) == (cells[0] + cells[1], cells[0] + cells[2])


def test_newcombe_edges_clamp_and_check() -> None:
    assert newcombe_paired(0, 0, 0, 10)[0] >= -1.0
    lower, upper = newcombe_paired(0, 0, 10, 0)  # every case flips to pass
    assert -1.0 <= lower <= 1.0 and upper == 1.0
    with pytest.raises(ValueError):
        newcombe_paired(-1, 0, 0, 0)


def test_holm_reference_vectors() -> None:
    assert holm([0.01, 0.04]) == pytest.approx([0.02, 0.04])
    assert holm([0.03, 0.02, 0.5]) == pytest.approx([0.06, 0.06, 0.5])
    assert holm([]) == []
    assert holm([0.04, 0.04]) == pytest.approx([0.08, 0.08])  # ties by input order
    assert holm([0.9, 0.8]) == [1.0, 1.0]  # capped at 1, never decreasing


def _reference_bootstrap(
    base: list[float], cand: list[float], resamples: int, seed: int
) -> tuple[float, float]:
    """The contract's words, with the float index formulas."""
    rng = random.Random(seed)
    n = len(base)
    ratios = []
    for _ in range(resamples):
        draws = [rng.randrange(n) for _ in range(n)]
        b = sum(base[i] for i in draws)
        c = sum(cand[i] for i in draws)
        ratios.append(1.0 if b == 0 and c == 0 else math.inf if b == 0 else c / b)
    ratios.sort()
    return ratios[math.floor(0.025 * resamples)], ratios[math.ceil(0.975 * resamples) - 1]


@pytest.mark.parametrize("resamples", [1000, 1001, 1040, 2000])
def test_bootstrap_matches_the_contract_formula_and_is_deterministic(resamples: int) -> None:
    base = [0.001 * (1 + (i % 7)) for i in range(60)]
    cand = [0.0002 * (1 + (i % 5)) for i in range(60)]
    got = bootstrap_ratio(base, cand, resamples=resamples, seed=3)
    assert got == _reference_bootstrap(base, cand, resamples, 3)
    assert got == bootstrap_ratio(base, cand, resamples=resamples, seed=3)
    assert got != bootstrap_ratio(base, cand, resamples=resamples, seed=4)
    assert got[0] <= sum(cand) / sum(base) <= got[1]


def test_bootstrap_edge_ratios() -> None:
    same = [0.002, 0.004, 0.001]
    assert bootstrap_ratio(same, list(same), resamples=1000, seed=0) == (1.0, 1.0)
    assert bootstrap_ratio([0.0, 0.0], [0.0, 0.0], resamples=1000, seed=0) == (1.0, 1.0)
    assert bootstrap_ratio([0.0, 0.0], [0.1, 0.0], resamples=1000, seed=0)[1] == math.inf
    with pytest.raises(ValueError):
        bootstrap_ratio([], [], resamples=1000, seed=0)
    with pytest.raises(ValueError):
        bootstrap_ratio([1.0], [1.0, 2.0], resamples=1000, seed=0)


def test_equal_costs_give_a_ratio_of_one() -> None:
    base, cand = table_rows(10, 2, 3, 5)
    costs = tuple(0.001 * (1 + i % 3) for i in range(20))
    rows = [
        public_row(label="A3", passed=base, costs=costs),
        public_row(label="A7", passed=cand, costs=costs),
    ]
    [pair] = pair_arms(rows, [("A3", "A7")], bootstrap=1000).pairs
    assert pair.cost_ratio == 1.0 and pair.cost_ratio_ci == (1.0, 1.0)
    assert pair.base_cost_per_run == pytest.approx(sum(costs) / 20)
    assert pair.cand_cost_per_run == pair.base_cost_per_run


def test_holm_spans_every_pair_and_decides_better_and_worse() -> None:
    base, cand = table_rows(300, 20, 45, 135)
    rows = [
        public_row(label="A3", passed=base),
        public_row(label="A5", passed=base),
        public_row(label="A7", passed=cand),
    ]
    paired = pair_arms(rows, [("A3", "A7"), ("A7", "A5")], bootstrap=1000)
    first, second = paired.pairs
    p = mcnemar_exact(20, 45)
    assert first.mcnemar_p == second.mcnemar_p == pytest.approx(p)
    assert first.holm_p == second.holm_p == pytest.approx(2 * p)
    assert first.better and not first.worse  # the candidate passes more discordant cases
    assert second.worse and not second.better
    assert first.noninferior and not second.noninferior
    assert paired.alpha == 0.05 and paired.margin == 0.05 and paired.bootstrap == 1000
    assert paired.evidence == ("live_billed",) and paired.floor_origins == ("public_dataset",)


def test_selectors_are_model_variant_or_label() -> None:
    base, cand = table_rows(5, 1, 3, 1)
    rows = [
        public_row(label="", model="granite4.2:8b", passed=base),
        public_row(label="", model="lightning", variant="forge", passed=cand),
    ]
    [pair] = pair_arms(rows, [("granite4.2:8b:default", "lightning:forge")], bootstrap=1000).pairs
    assert (pair.base, pair.cand) == ("granite4.2:8b:default", "lightning:forge")
    assert (pair.n10, pair.n01) == (1, 3)
    with pytest.raises(BenchError) as caught:
        pair_arms(rows, [("granite4.2:8b", "lightning:forge")])  # "8b" is no variant
    assert caught.value.code == "bench_variant_unknown"


@pytest.mark.parametrize(
    ("update", "code"),
    [
        ({"delivery_pass_bits": None}, "bench_compare_mismatch"),
        ({"delivery_cases_sha256": "9" * 64}, "bench_compare_mismatch"),
        ({"delivery_cases_sha256": None}, "bench_compare_mismatch"),
        ({"suite_digest": "8" * 64}, "bench_compare_mismatch"),
        ({"suite_version": "v1"}, "bench_compare_mismatch"),
        ({"preset_fingerprint": "7" * 64}, "bench_compare_fingerprint"),
    ],
)
def test_pairs_that_did_not_measure_the_same_cases_are_refused(
    update: dict[str, object], code: str
) -> None:
    base, cand = table_rows(5, 1, 3, 1)
    rows = [public_row(label="A3", passed=base), public_row(label="A7", passed=cand, **update)]
    with pytest.raises(BenchError) as caught:
        pair_arms(rows, [("A3", "A7")], bootstrap=1000)
    assert caught.value.code == code


def test_other_refusals_and_what_may_differ() -> None:
    base, cand = table_rows(5, 1, 3, 1)
    a3, a7 = public_row(label="A3", passed=base), public_row(label="A7", passed=cand)
    shorter = public_row(label="A7", passed=cand[:-1])
    for rows, hypotheses in (
        ([a3, a7, a7.model_copy(update={"bench_id": "11111111-2222-4333-8444-555555555555"})],
         [("A3", "A7")]),  # two candidate rows on one (task, repeat)
        ([a3, a7.model_copy(update={"repeat": 1})], [("A3", "A7")]),  # no common cell
        ([a3, shorter], [("A3", "A7")]),  # another number of delivery cases
        ([a3, a7], []),
    ):
        with pytest.raises(BenchError) as caught:
            pair_arms(rows, hypotheses, bootstrap=1000)
        assert caught.value.code == "bench_compare_mismatch"
    # spec_hash, model and evidence may differ between arms; presets only with the flag.
    other = a7.model_copy(update={"spec_hash": "1" * 64, "preset_fingerprint": "2" * 64})
    with pytest.raises(BenchError, match="bench_compare_fingerprint"):
        pair_arms([a3, other], [("A3", "A7")], bootstrap=1000)
    paired = pair_arms(
        [a3, other], [("A3", "A7")], bootstrap=1000, allow_fingerprint_mismatch=True
    )
    assert paired.pairs[0].n == 10


def test_rows_without_cost_vectors_have_no_cost_fields() -> None:
    base, cand = table_rows(5, 1, 3, 1)
    rows = [
        public_row(label="A3", passed=base, delivery_run_costs_usd=None),
        public_row(label="A7", passed=cand),
    ]
    [pair] = pair_arms(rows, [("A3", "A7")], bootstrap=1000).pairs
    assert pair.cost_ratio is None and pair.cost_ratio_ci is None
    assert pair.base_cost_per_run is None and pair.cand_cost_per_run is None


def test_an_infinite_ratio_round_trips_as_a_string() -> None:
    base, cand = table_rows(2, 0, 0, 2)
    rows = [
        public_row(label="A3", passed=base, costs=(0.0,) * 4),
        public_row(label="A7", passed=cand, costs=(0.001,) * 4),
    ]
    paired = pair_arms(rows, [("A3", "A7")], bootstrap=1000)
    assert paired.pairs[0].cost_ratio == math.inf
    text = paired.model_dump_json()
    assert '"Infinity"' in text and "NaN" not in text
    assert PairedComparison.model_validate_json(text) == paired
    assert isinstance(paired.pairs[0], ArmPair)


def test_the_pairs_table_is_ascii_and_narrow() -> None:
    base, cand = table_rows(300, 20, 45, 135)
    rows = [
        public_row(label="A3", passed=base),
        public_row(label="A7-with-a-much-longer-label", passed=cand),
    ]
    table = pairs_table(pair_arms(rows, [("A3", "A7-with-a-much-longer-label")], bootstrap=1000))
    lines = table.splitlines()
    assert all(len(line) <= 100 and line.isascii() for line in lines)
    assert "A3 -> A7-with-a-much-longer-label: receipts repeat 0, n = 500" in table
    assert "diff +0.050 [+0.019, +0.081]; McNemar p 0.002626" in table
    assert "candidate better" in table and "non-inferior at 0.05: yes" in table
    assert lines[-1] == "money is API-equivalent, not a bill; scores are live_billed"


def test_a_row_without_the_tuning_replay_has_no_tuning_bits() -> None:
    """Section 11: the legacy compare never reads a missing replay as all failed."""
    row = public_row(tuning_replay=False, tuning_pass_bits=None, tuning_cases=100)
    [cell] = compare([row], [row]).cells
    assert cell.mcnemar_p is None and (cell.discordant_b, cell.discordant_c) == (0, 0)
    replayed = row.model_copy(update={"tuning_replay": True})
    [cell] = compare([replayed], [replayed]).cells
    assert cell.mcnemar_p == 1.0  # a replayed row without bits failed every tuning case


def _bench(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = cli_main(["bench", *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_compare_pair_arms(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base, cand = table_rows(300, 20, 45, 135)
    first, second = tmp_path / "a3.jsonl", tmp_path / "a7.jsonl"
    write_rows(first, [public_row(label="A3", passed=base)])
    write_rows(second, [public_row(label="A7", passed=cand)])
    code, out, err = _bench(
        capsys, "compare", str(first), str(second), "--pair-arms", "A3,A7", "--json",
        "--bootstrap", "1000", "--seed", "5",
    )
    assert code == 0, err
    paired = PairedComparison.model_validate_json(out)
    assert paired.seed == 5 and paired.bootstrap == 1000 and len(paired.pairs) == 1
    code, out, _ = _bench(
        capsys, "compare", str(first), str(second), "--pair-arms", "A3,A7", "--bootstrap", "1000"
    )
    assert code == 0 and "A3 -> A7" in out
    for extra in (
        ["--pair-variants", "default:forge"],
        ["--expect", "identical-scores"],
        ["--bootstrap", "999"],
        ["--bootstrap", "100001"],
        ["--alpha", "0"],
        ["--margin", "1"],
    ):
        code, out, err = _bench(
            capsys, "compare", str(first), str(second), "--pair-arms", "A3,A7", *extra
        )
        assert code == 2 and json.loads(err) == {"error": "invalid_arguments"}, extra
    for arms in ("A3", "A3,", ",A7", "A3,A3", "A3,A7,A5"):
        code, _, err = _bench(capsys, "compare", str(first), str(second), "--pair-arms", arms)
        assert code == 2 and json.loads(err) == {"error": "invalid_arguments"}, arms
    # Without --pair-arms, compare takes exactly two sources, as before.
    code, _, err = _bench(capsys, "compare", str(first), str(second), str(first))
    assert code == 2 and json.loads(err) == {"error": "invalid_arguments"}
    code, _, err = _bench(
        capsys, "compare", str(first), str(second), "--pair-arms", "A3,nope:forge"
    )
    assert code == 1 and json.loads(err) == {"error": "bench_compare_mismatch"}
