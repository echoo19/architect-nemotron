"""Phase 14 review: adversarial checks of the paired-arm statistics and the gate audit.

The first tests recompute the paired statistics with independent code (50-digit
decimals, exact rationals and Holm's step-down procedure run literally) over many
random tables, and check that ``pair_arms`` counts the 2x2 table from aligned bits in
any case order. The tests that record defects found in review assert the
pre-registered behaviour (docs/bench/prereg-nebius.md); each was a strict xfail until
its fix landed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import random
import shutil
import sys
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from bench_public_support import public_row

from architect.bench import public_tasks
from architect.bench.audit import (
    AuditRequest,
    AuditRow,
    practice_drop_exceeds,
    replay_audit,
)
from architect.bench.compare import (
    PairedComparison,
    holm,
    mcnemar_exact,
    newcombe_paired,
    pair_arms,
    wilson,
)
from architect.bench.render import pairs_table
from architect.bench.rows import decode_pass_bits, write_rows
from architect.compiler import Compiler
from architect.floors import EvaluationCase, HeldOutTasks
from architect.models import FakeAdapter
from architect.profiles import load_profile
from architect.runtime import Runtime
from architect.spec import Budget, FloorDefinition, FloorKind, HarnessArtifact, Spec
from architect.telemetry import TelemetryStore

ROOT = Path(__file__).resolve().parents[1]
DIGEST = "c0ffee00" * 8
SESSION = "d" * 32

# ------------------------------------------------------- independent statistics


def _wilson_decimal(k: int, n: int) -> tuple[Decimal, Decimal]:
    z = Decimal("1.96")
    p = Decimal(k) / n
    z2 = z * z
    denominator = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denominator
    half = z * (p * (1 - p) / n + z2 / (4 * n * n)).sqrt() / denominator
    return max(Decimal(0), centre - half), min(Decimal(1), centre + half)


def _newcombe_decimal(a: int, b: int, c: int, d: int) -> tuple[Decimal, Decimal]:
    """Newcombe (1998) method 10 in the paper's own notation: theta = p1 - p2 with
    p1 = (a + b) / n and p2 = (a + c) / n, so p1 is the candidate here."""
    n = a + b + c + d
    p1, p2 = Decimal(a + b) / n, Decimal(a + c) / n
    l1, u1 = _wilson_decimal(a + b, n)
    l2, u2 = _wilson_decimal(a + c, n)
    margins = (a + b) * (c + d) * (a + c) * (b + d)
    phi = Decimal(a * d - b * c) / Decimal(margins).sqrt() if margins else Decimal(0)
    zero = Decimal(0)
    delta = max(zero, (p1 - l1) ** 2 - 2 * phi * (p1 - l1) * (u2 - p2) + (u2 - p2) ** 2).sqrt()
    eps = max(zero, (u1 - p1) ** 2 - 2 * phi * (u1 - p1) * (p2 - l2) + (p2 - l2) ** 2).sqrt()
    theta = p1 - p2
    return max(Decimal(-1), theta - delta), min(Decimal(1), theta + eps)


def _mcnemar_fraction(b: int, c: int) -> Fraction:
    """Twice the smaller binomial(n, 1/2) tail, built from the pmf recursion."""
    n = b + c
    if n == 0:
        return Fraction(1)
    pmf = Fraction(1, 2**n)
    tail = Fraction(0)
    for i in range(min(b, c) + 1):
        tail += pmf
        pmf = pmf * (n - i) / (i + 1)
    return min(Fraction(1), 2 * tail)


def _holm_by_procedure(p_values: list[float]) -> list[float]:
    """Holm's adjusted p as the smallest level at which the step-down test rejects."""
    m = len(p_values)
    order = sorted(range(m), key=lambda index: (p_values[index], index))
    levels = sorted({min(1.0, (m - j) * p_values[order[j]]) for j in range(m)} | {1.0})
    adjusted = [1.0] * m
    for target in range(m):
        for level in levels:
            rejected: set[int] = set()
            for j, index in enumerate(order):
                if (m - j) * p_values[index] > level:
                    break
                rejected.add(index)
            if target in rejected:
                adjusted[target] = level
                break
    return adjusted


def _random_table(rng: random.Random, n: int) -> tuple[int, int, int, int]:
    cuts = sorted(rng.randint(0, n) for _ in range(3))
    return cuts[0], cuts[1] - cuts[0], cuts[2] - cuts[1], n - cuts[2]


def test_newcombe_mcnemar_and_wilson_match_independent_recomputation() -> None:
    rng = random.Random(1914)
    tables = [(300, 20, 45, 135), (80, 5, 12, 3), (50, 0, 0, 50), (0, 0, 0, 7), (0, 7, 0, 0)]
    tables += [_random_table(rng, rng.randint(1, 600)) for _ in range(4000)]
    with localcontext() as context:
        context.prec = 50
        for n11, n10, n01, n00 in tables:
            n = n11 + n10 + n01 + n00
            lower, upper = _newcombe_decimal(n11, n01, n10, n00)
            got = newcombe_paired(n11, n10, n01, n00)
            assert abs(Decimal(got[0]) - lower) < Decimal("1e-12"), (n11, n10, n01, n00)
            assert abs(Decimal(got[1]) - upper) < Decimal("1e-12"), (n11, n10, n01, n00)
            exact = _mcnemar_fraction(n10, n01)
            assert mcnemar_exact(n10, n01) == pytest.approx(float(exact), rel=1e-13, abs=0)
            for k in (n11 + n10, n11 + n01):
                lo, hi = _wilson_decimal(k, n)
                w = wilson(k, n)
                assert abs(Decimal(w[0]) - lo) < Decimal("1e-13")
                assert abs(Decimal(w[1]) - hi) < Decimal("1e-13")


def test_holm_matches_the_step_down_procedure() -> None:
    rng = random.Random(7)
    for _ in range(3000):
        m = rng.randint(1, 9)
        pool = [rng.random(), rng.random() ** 6, 0.01, 0.025, 0.05, 1.0]
        p_values = [rng.choice(pool) for _ in range(m)]
        assert holm(p_values) == pytest.approx(_holm_by_procedure(p_values), abs=0), p_values


def test_pair_arms_counts_aligned_bits_in_any_case_order() -> None:
    """The 2x2 table comes from bits aligned case by case, whatever order the cases
    were replayed in, and every statistic follows from that table alone."""
    rng = random.Random(3)
    for _ in range(40):
        cells = _random_table(rng, rng.randint(1, 120))
        n11, n10, n01, n00 = cells
        base = [True] * n11 + [True] * n10 + [False] * n01 + [False] * n00
        cand = [True] * n11 + [False] * n10 + [True] * n01 + [False] * n00
        order = list(range(len(base)))
        rng.shuffle(order)
        rows = [
            public_row(label="A3", passed=[base[i] for i in order]),
            public_row(label="A7", model="ultra", passed=[cand[i] for i in order]),
        ]
        [pair] = pair_arms(rows, [("A3", "A7")], bootstrap=1000).pairs
        assert (pair.n11, pair.n10, pair.n01, pair.n00) == cells
        assert pair.diff_ci == newcombe_paired(*cells)
        assert pair.mcnemar_p == mcnemar_exact(n10, n01)
        assert pair.noninferior is (pair.diff_ci[0] > -0.05)


# ---------------------------------------------------------- the analysis script


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "analyze_prereg_review", ROOT / "scripts" / "analyze_prereg.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["analyze_prereg_review"] = module
    spec.loader.exec_module(module)
    return module


def _arm_row(label: str, rate: float, seed: int, **overrides: Any) -> Any:
    """Every delivery receipt replayed in manifest order, so the test subset joins."""
    public_tasks.clear_cache()
    manifest = public_tasks.load_public("receipts").manifests["delivery"]
    rng = random.Random(seed)
    passed = [rng.random() < rate for _ in range(manifest.count)]
    return public_row(
        label=label,
        suite_digest=DIGEST,
        passed=passed,
        costs=tuple(0.0001 * (1 + i % 3) for i in range(manifest.count)),
        delivery_cases_sha256=manifest.cases_sha256,
        **overrides,
    )


def _audit_row(index: int, *, exam: str, origin: str) -> AuditRow:
    return AuditRow(
        audit_version=1,
        replay_id="0f0e0d0c-0b0a-4908-8706-050403020100",
        spec_hash="a" * 64,
        session_id="b" * 32,
        submission=index,
        depth=0,
        mode="agent",
        model="lightning",
        agent_model="ultra",
        recorded_at="2026-10-12T10:00:00Z",  # type: ignore[arg-type]
        artifact_digest="c" * 64,
        verdict="accepted",
        floor_limit=0.8,
        tuning_score=0.9,
        tuning_correct=90,
        tuning_cases=100,
        tuning_invalid=0,
        met_bar_in_practice=True,
        exam=exam,  # type: ignore[arg-type]
        held_out_score=0.7,
        held_out_correct=70,
        held_out_cases=100,
        held_out_invalid=0,
        held_out_over_budget=0,
        practice_drop=0.2,
        floor_origin=origin,  # type: ignore[arg-type]
        evidence="live_billed",
        replay_cost_usd=0.01,
    )


def _config(
    root: Path,
    rows: dict[str, Any],
    *,
    audit: list[AuditRow] | None = None,
    missing: tuple[str, ...] = (),
) -> Path:
    arms: dict[str, str] = {}
    for name, row in rows.items():
        path = root / "rows" / f"{name}.jsonl"
        if name not in missing:
            write_rows(path, [row])
        arms[name] = str(path)
    data: dict[str, Any] = {
        "prereg_version": 1,
        "suite_digest": DIGEST,
        "task_id": "receipts",
        "arms": arms,
        "hypotheses": {
            "H1a": {"test": "superiority", "base": "A3", "cand": "A7"},
            "H2": {"test": "noninferiority", "base": "A1", "cand": "A7", "margin": 0.05},
        },
        "holm": ["H1a"],
        "replicate": {"A7": "A7b"} if "A7b" in rows else {},
        "bootstrap": 1000,
        "seed": 7,
    }
    if audit is not None:
        audit_path = root / "audit.jsonl"
        audit_path.write_text("".join(row.model_dump_json() + "\n" for row in audit), "utf-8")
        data["audit"] = {
            "rows": str(audit_path),
            "since": "2026-10-11T00:00:00Z",
            "until": "2026-10-20T07:00:00Z",
        }
    path = root / "prereg.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _analysis(config: Path, out: Path) -> dict[str, Any]:
    assert _script().main(["--config", str(config), "--out", str(out)]) == 0
    result: dict[str, Any] = json.loads((out / "analysis.json").read_text(encoding="ascii"))
    return result


def test_the_gate_audit_counts_only_replays_on_the_public_evidence_floor(
    tmp_path: Path,
) -> None:
    rows = {name: _arm_row(name, rate, seed) for name, rate, seed in
            (("A1", 0.9, 1), ("A3", 0.7, 3), ("A7", 0.85, 7))}
    audit = [
        _audit_row(1, exam="below_bar", origin="public_dataset"),
        _audit_row(2, exam="below_bar", origin="synthetic"),  # a tampered or other floor
        _audit_row(3, exam="below_bar", origin="no_manifest"),  # a floor with no manifest
    ]
    analysis = _analysis(_config(tmp_path, rows, audit=audit), tmp_path / "out")
    window = analysis["gate_audit"]["window"]
    assert (window["met_bar_in_practice"], window["failed_exam"]) == (1, 1)


def _failing_arm_row(label: str, rate: float, seed: int) -> Any:
    """``_arm_row`` whose failed runs alternate between invalid and over budget, with
    the outcome bits the runner records."""
    row = _arm_row(label, rate, seed)
    passed = decode_pass_bits(row.delivery_pass_bits, row.delivery_cases)
    return _arm_row(
        label,
        rate,
        seed,
        invalid=[not ok and index % 2 == 0 for index, ok in enumerate(passed)],
        over_budget=[not ok and index % 2 == 1 for index, ok in enumerate(passed)],
    )


def _test_positions() -> list[int]:
    delivery = public_tasks.load_public("receipts").manifests["delivery"]
    return [i for i, case in enumerate(delivery.cases) if (case.source or "").startswith("test/")]


def test_test_subset_pairs_never_report_more_invalid_runs_than_cases(tmp_path: Path) -> None:
    rows = {
        "A1": _arm_row("A1", 0.9, 1),
        "A3": _failing_arm_row("A3", 0.3, 3),
        "A7": _arm_row("A7", 0.85, 7),
    }
    analysis = _analysis(_config(tmp_path, rows), tmp_path / "out")
    pair = analysis["test_subset"]["H1a"]["pair"]
    delivery = public_tasks.load_public("receipts").manifests["delivery"]
    assert pair["n"] < delivery.count  # the CORD test receipts only
    assert pair["base_invalid"] <= pair["n"] and pair["base_over_budget"] <= pair["n"]
    # The whole set has more of each than the subset has receipts, and the subset's
    # counts are exactly the runs at its positions.
    a3 = rows["A3"]
    assert a3.delivery_invalid > pair["n"] and a3.delivery_over_budget > pair["n"]
    positions = _test_positions()
    for name in ("invalid", "over_budget"):
        bits = decode_pass_bits(getattr(a3, f"delivery_{name}_bits"), a3.delivery_cases)
        assert pair[f"base_{name}"] == sum(bits[i] for i in positions) > 0
        assert pair[f"cand_{name}"] == 0
    full = analysis["hypotheses"]["H1a"]["pair"]
    assert (full["base_invalid"], full["base_over_budget"]) == (
        a3.delivery_invalid,
        a3.delivery_over_budget,
    )


def test_an_older_row_reports_unknown_subset_counts_not_the_whole_sets(
    tmp_path: Path,
) -> None:
    """A row without outcome bits kept only its totals: the subset shows 0 when the
    total is 0 and null (a dash in the table) otherwise, never the whole set's count."""
    rows = {
        "A1": _arm_row("A1", 0.9, 1),
        "A3": _arm_row("A3", 0.7, 3, delivery_invalid=12, delivery_over_budget=0),
        "A7": _arm_row("A7", 0.85, 7),
    }
    analysis = _analysis(_config(tmp_path, rows), tmp_path / "out")
    pair = analysis["test_subset"]["H1a"]["pair"]
    assert pair["base_invalid"] is None and pair["base_over_budget"] == 0
    assert (pair["cand_invalid"], pair["cand_over_budget"]) == (0, 0)
    full = analysis["hypotheses"]["H1a"]["pair"]
    assert (full["base_invalid"], full["base_over_budget"]) == (12, 0)
    shown = pairs_table(
        PairedComparison.model_validate(
            {
                "suite_version": "v2",
                "suite_digest": DIGEST,
                "alpha": 0.05,
                "margin": 0.05,
                "bootstrap": 1000,
                "seed": 7,
                "pairs": [pair],
                "evidence": ["live_billed"],
                "floor_origins": ["public_dataset"],
            }
        )
    )
    assert "invalid -/0, over budget 0/0" in shown


def test_a_missing_replicate_cell_fails_its_tests_without_aborting_the_analysis(
    tmp_path: Path,
) -> None:
    rows = {name: _arm_row(name, rate, seed) for name, rate, seed in
            (("A1", 0.9, 1), ("A3", 0.7, 3), ("A7", 0.85, 7), ("A7b", 0.84, 8))}
    analysis = _analysis(_config(tmp_path, rows, missing=("A7b",)), tmp_path / "out")
    assert analysis["hypotheses"]["H1a"]["pair"]["cand"] == "A7"
    assert all(result["holds"] is False for result in analysis["replicates"].values())
    # Named as missing, with no pair, and listed in the Markdown.
    assert analysis["missing_arms"] == {"A7b": "no_rows"}
    for result in analysis["replicates"].values():
        assert result["missing"] == ["A7b"] and result["pair"] is None
    assert all(result["missing"] == [] for result in analysis["hypotheses"].values())
    markdown = (tmp_path / "out" / "analysis.md").read_text(encoding="utf-8")
    assert "## Missing arms\n\n- A7b: no rows file\n" in markdown


# --------------------------------------------------------------- the audit replay


def _cases(expected: list[str]) -> list[EvaluationCase]:
    return [
        EvaluationCase(case_id=f"case-{index:02d}", task={"n": index}, expected=value)
        for index, value in enumerate(expected)
    ]


async def _licensed(root: Path) -> HarnessArtifact:
    """A fake-model harness licensed at a bar of 0.8 on 20 practice and 20 exam cases."""
    spec = Spec(
        task="Return ok",
        input_schema={"type": "object"},
        output_schema={"type": "string"},
        domain="synthesizer",
        floor=FloorDefinition(
            kind=FloorKind.HELD_OUT_TASKS, limit=0.8, dataset="audit-floor", minimum_samples=4
        ),
        budget=Budget(cost_usd=0.1, compute_s=60),
    )

    def factory(definition: FloorDefinition) -> HeldOutTasks:
        return HeldOutTasks(definition, _cases(["ok"] * 40))

    store = TelemetryStore(root / "runs")
    try:
        compiler = Compiler(
            root / "artifacts", Runtime(store), lambda model: FakeAdapter(model=model), factory
        )
        result = await compiler.compile(spec, approved=True)
    finally:
        store.close()
    assert result.artifact is not None and result.artifact.licensed
    return result.artifact


def _submit(artifact: HarnessArtifact, audit: Path, *, tuning_correct: int) -> None:
    """Forge audit submission 1 (Section 8.4) with a 20-case practice measurement."""
    directory = audit / artifact.spec.spec_hash / SESSION / "1"
    directory.mkdir(parents=True)
    for name in ("harness.py", "spec.json", "config.json"):
        shutil.copy2(artifact.directory / name, directory / name)
    record = {
        "audit_version": 1,
        "spec_hash": artifact.spec.spec_hash,
        "session_id": SESSION,
        "submission": 1,
        "depth": 0,
        "mode": "agent",
        "model": "fake",
        "agent_model": "fake",
        "code_sha256": hashlib.sha256((directory / "harness.py").read_bytes()).hexdigest(),
        "artifact_digest": artifact.artifact_digest,
        "verdict": "accepted",
        "floor_limit": 0.8,
        "tuning": {
            "score": tuning_correct / 20,
            "sample_count": 20,
            "counts": {"cases": 20, "correct": tuning_correct, "invalid": 0, "over_budget": 0},
            "failures": [],
        },
        "recorded_at": "2026-10-12T10:00:00+00:00",
    }
    (directory / "submission.json").write_text(json.dumps(record), encoding="utf-8")


def _profile(root: Path, exam_misses: int) -> Path:
    """The audit-floor: 40 cases; the first ``exam_misses`` exam cases expect "no"."""
    expected = ["ok"] * 40
    for index in range(exam_misses):
        expected[2 * index + 1] = "no"  # odd positions of the sorted ids are the exam
    (root / "floor.json").write_text(
        json.dumps([case.model_dump(mode="json") for case in _cases(expected)]),
        encoding="utf-8",
    )
    path = root / "profile.json"
    path.write_text(
        json.dumps(
            {
                "models": {"fake": {"provider": "fake"}},
                "floors": {"audit-floor": {"kind": "held_out_tasks", "path": "floor.json"}},
            }
        ),
        encoding="utf-8",
    )
    return path


def _rows(path: str) -> list[AuditRow]:
    text = Path(path).read_text(encoding="utf-8")
    return [AuditRow.model_validate_json(line) for line in text.splitlines()]


async def test_an_exact_five_point_drop_is_not_counted_as_more_than_five_points(
    tmp_path: Path,
) -> None:
    artifact = await _licensed(tmp_path / "build")
    counted: list[int] = []
    for tuning_correct, exam_misses in ((18, 3), (17, 4)):  # 0.90 -> 0.85, 0.85 -> 0.80
        root = tmp_path / f"t{tuning_correct}"
        root.mkdir()
        audit = root / "audit"
        _submit(artifact, audit, tuning_correct=tuning_correct)
        result = await replay_audit(
            AuditRequest(directory=audit, out=root / "replays"),
            load_profile(_profile(root, exam_misses)),
            consent=lambda line: True,
            workspace_root=root / "ws",
        )
        [row] = _rows(result.rows_path)
        drop = round((tuning_correct - (20 - exam_misses)) / 20, 12)
        assert drop == 0.05 and row.exam == "passed" and row.met_bar_in_practice
        assert row.practice_drop is not None and math.isclose(row.practice_drop, 0.05)
        counted.append(result.practice_drops)
    assert counted == [0, 0]  # neither exam fell more than 5 points below practice


def test_more_than_five_points_is_decided_exactly_for_every_count() -> None:
    """Every practice and exam count on 100-case halves: a drop counts exactly when it
    is 6 points or more, whether the record kept its practice counts or only its score.
    As a float difference, 39 of the 96 exact 5-point drops used to count."""
    base = _audit_row(1, exam="passed", origin="public_dataset")
    for practice in range(101):
        for exam in range(101):
            drop = practice / 100 - exam / 100  # what a float subtraction stores
            row = base.model_copy(
                update={
                    "tuning_score": practice / 100,
                    "tuning_correct": practice,
                    "tuning_cases": 100,
                    "held_out_score": exam / 100,
                    "held_out_correct": exam,
                    "held_out_cases": 100,
                    "practice_drop": drop,
                }
            )
            legacy = row.model_copy(update={"tuning_correct": None})  # a score, no counts
            expected = practice - exam > 5
            assert practice_drop_exceeds(row) is expected, (practice, exam)
            assert practice_drop_exceeds(legacy) is expected, (practice, exam)
    assert not practice_drop_exceeds(base.model_copy(update={"practice_drop": None}))


def test_the_analysis_never_counts_an_exact_five_point_drop(tmp_path: Path) -> None:
    """Audit rows written before the fix hold the float difference (0.9 - 0.85 is
    0.05000000000000004); the analysis decides from the counts."""
    rows = {name: _arm_row(name, rate, seed) for name, rate, seed in
            (("A1", 0.9, 1), ("A3", 0.7, 3), ("A7", 0.85, 7))}
    audit = []
    for index, (practice, exam) in enumerate(((18, 17), (17, 16), (18, 16)), start=1):
        row = _audit_row(index, exam="passed", origin="public_dataset")
        audit.append(
            row.model_copy(
                update={
                    "tuning_score": practice / 20,
                    "tuning_correct": practice,
                    "tuning_cases": 20,
                    "held_out_score": exam / 20,
                    "held_out_correct": exam,
                    "held_out_cases": 20,
                    "practice_drop": practice / 20 - exam / 20,
                }
            )
        )
    assert [row.practice_drop for row in audit] == [0.9 - 0.85, 0.85 - 0.8, 0.9 - 0.8]
    analysis = _analysis(_config(tmp_path, rows, audit=audit), tmp_path / "out")
    window = analysis["gate_audit"]["window"]
    assert window["met_bar_in_practice"] == 3
    assert window["practice_drop_over_0_05"] == 1  # only the 10-point drop
