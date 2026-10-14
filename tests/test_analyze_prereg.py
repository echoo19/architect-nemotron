"""The pre-registered analysis skeleton (Phase 14 Section 13.3) on fixture rows, offline."""

from __future__ import annotations

import importlib.util
import json
import random
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from bench_public_support import public_row

from architect.bench import public_tasks
from architect.bench.audit import AuditRow
from architect.bench.rows import read_rows, write_rows

ROOT = Path(__file__).resolve().parents[1]
DIGEST = "c0ffee00" * 8


def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "analyze_prereg", ROOT / "scripts" / "analyze_prereg.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["analyze_prereg"] = module
    spec.loader.exec_module(module)
    return module


def arm_row(label: str, model: str, rate: float, cost: float, seed: int) -> Any:
    """Every delivery case, replayed in manifest order (so the test subset can join)."""
    public_tasks.clear_cache()
    manifest = public_tasks.load_public("receipts").manifests["delivery"]
    rng = random.Random(seed)
    passed = [rng.random() < rate for _ in range(manifest.count)]
    costs = tuple(round(cost * (0.5 + rng.random()), 8) for _ in passed)
    return public_row(
        label=label,
        model=model,
        suite_digest=DIGEST,
        passed=passed,
        costs=costs,
        delivery_cases_sha256=manifest.cases_sha256,
    )


def audit_row(
    index: int,
    recorded_at: str,
    exam: str,
    met: bool,
    *,
    origin: str = "public_dataset",
    evidence: str = "live_billed",
    floor: str | None = None,
    bar: float = 0.8,
) -> AuditRow:
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
        recorded_at=recorded_at,  # type: ignore[arg-type]
        artifact_digest="c" * 64,
        verdict="accepted",
        floor_limit=bar,
        tuning_score=0.9,
        tuning_correct=90,
        tuning_cases=100,
        tuning_invalid=0,
        met_bar_in_practice=met,
        exam=exam,  # type: ignore[arg-type]
        held_out_score=0.7 if exam == "below_bar" else 0.85,
        held_out_correct=70 if exam == "below_bar" else 85,
        held_out_cases=100,
        held_out_invalid=0,
        held_out_over_budget=0,
        practice_drop=0.2 if exam == "below_bar" else 0.05,
        floor_origin=origin,  # type: ignore[arg-type]
        evidence=evidence,  # type: ignore[arg-type]
        replay_cost_usd=0.01,
        floor_cases_sha256=floor,
    )


@pytest.fixture
def config(tmp_path: Path) -> Path:
    rows = {
        "A1": arm_row("A1", "ultra", 0.92, 0.0015, 1),
        "A3": arm_row("A3", "lightning", 0.70, 0.0001, 3),
        "A5": arm_row("A5", "lightning", 0.75, 0.0003, 5),
        "A7": arm_row("A7", "lightning", 0.90, 0.0002, 7),
        "A7b": arm_row("A7b", "lightning", 0.89, 0.0002, 8),
    }
    arms: dict[str, str] = {}
    for name, row in rows.items():
        path = tmp_path / "rows" / f"{name}.jsonl"
        write_rows(path, [row])
        arms[name] = str(path)
    audit = tmp_path / "audit.jsonl"
    audit.write_text(
        "".join(
            row.model_dump_json() + "\n"
            for row in (
                audit_row(1, "2026-10-12T10:00:00Z", "below_bar", True),
                audit_row(2, "2026-10-13T10:00:00Z", "passed", True),
                audit_row(3, "2026-10-08T10:00:00Z", "below_bar", True),  # before the tag
                audit_row(4, "2026-10-14T10:00:00Z", "over_budget", True),
            )
        ),
        encoding="utf-8",
    )
    path = tmp_path / "prereg.json"
    path.write_text(
        json.dumps(
            {
                "prereg_version": 1,
                "suite_digest": DIGEST,
                "task_id": "receipts",
                "arms": arms,
                "hypotheses": {
                    "H1a": {"test": "superiority", "base": "A3", "cand": "A7"},
                    "H1b": {"test": "superiority", "base": "A5", "cand": "A7"},
                    "H2": {"test": "noninferiority", "base": "A1", "cand": "A7",
                           "margin": 0.05},
                    "H3": {"test": "cost_ratio", "base": "A1", "cand": "A7", "max_ratio": 0.25},
                },
                "holm": ["H1a", "H1b"],
                "replicate": {"A7": "A7b"},
                "alpha": 0.05,
                "bootstrap": 1000,
                "seed": 0,
                "audit": {
                    "rows": str(audit),
                    "since": "2026-10-11T00:00:00Z",
                    "until": "2026-10-19T23:59:59Z",
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def test_two_runs_are_byte_identical_and_check_catches_one_changed_byte(
    config: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    analyze = script()
    first, second = tmp_path / "out1", tmp_path / "out2"
    assert analyze.main(["--config", str(config), "--out", str(first)]) == 0
    assert analyze.main(["--config", str(config), "--out", str(second)]) == 0
    for name in ("analysis.json", "analysis.md"):
        assert (first / name).read_bytes() == (second / name).read_bytes(), name
    assert analyze.main(["--config", str(config), "--out", str(first), "--check"]) == 0
    data = bytearray((first / "analysis.json").read_bytes())
    data[-2] = ord(" ") if data[-2] != ord(" ") else ord("}")
    (first / "analysis.json").write_bytes(bytes(data))
    capsys.readouterr()
    assert analyze.main(["--config", str(config), "--out", str(first), "--check"]) == 1
    assert json.loads(capsys.readouterr().err) == {"changed": ["analysis.json"]}


def test_the_analysis_holds_the_tests_the_subset_and_the_gate_audit(
    config: Path, tmp_path: Path
) -> None:
    analyze = script()
    out = tmp_path / "out"
    assert analyze.main(["--config", str(config), "--out", str(out)]) == 0
    raw = (out / "analysis.json").read_bytes()
    assert raw.endswith(b"}\n") and raw.isascii() and b"\n" not in raw[:-1]
    analysis = json.loads(raw)
    assert analysis["H4"] is None and analysis["outcome"] is None
    assert analysis["suite_digest"] == DIGEST and analysis["bootstrap"] == 1000
    hypotheses = analysis["hypotheses"]
    assert set(hypotheses) == {"H1a", "H1b", "H2", "H3"}
    h1a, h1b = hypotheses["H1a"]["pair"], hypotheses["H1b"]["pair"]
    # Holm across H1a and H1b only: each adjusted p is at most twice its raw p.
    for pair in (h1a, h1b):
        assert pair["mcnemar_p"] <= pair["holm_p"] <= min(1.0, 2 * pair["mcnemar_p"]) + 1e-15
    assert hypotheses["H2"]["pair"]["holm_p"] == hypotheses["H2"]["pair"]["mcnemar_p"]
    assert hypotheses["H1a"]["holds"] is h1a["better"]
    lower = hypotheses["H2"]["pair"]["diff_ci"][0]
    assert hypotheses["H2"]["holds"] is (lower > -0.05)
    upper = hypotheses["H3"]["pair"]["cost_ratio_ci"][1]
    assert hypotheses["H3"]["holds"] is (upper <= 0.25)
    replicates = analysis["replicates"]
    assert replicates["H1a"]["pair"]["cand"] == "A7b" and replicates["H1a"]["cand"] == "A7"
    subset = analysis["test_subset"]
    public_tasks.clear_cache()
    manifest = public_tasks.load_public("receipts").manifests["delivery"]
    tests = sum(1 for case in manifest.cases if (case.source or "").startswith("test/"))
    assert subset is not None and 0 < tests == subset["H1a"]["pair"]["n"]  # CORD test receipts
    audit = analysis["gate_audit"]
    assert audit["window"] == {
        "replayed": 3,
        "met_bar_in_practice": 2,
        "failed_exam": 1,
        "over_budget": 1,
        "invalid_exceeded": 0,
        "not_countable": 0,
        "practice_drop_over_0_05": 1,
        "excluded": {"floor_origin": {}, "evidence": {}, "other_exam": 0},
    }
    assert audit["floor_cases_sha256"] is None and audit["floor_limit"] is None
    assert audit["all_builds_secondary"]["failed_exam"] == 2
    text = raw.decode("ascii") + (out / "analysis.md").read_text(encoding="utf-8")
    assert str(tmp_path) not in text and str(tmp_path).replace("\\", "/") not in text
    markdown = (out / "analysis.md").read_text(encoding="utf-8")
    assert markdown.startswith("## receipts\n") and "## Paired arms: receipts" in markdown


def test_rows_of_another_suite_are_refused(
    config: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    analyze = script()
    data = json.loads(config.read_text(encoding="utf-8"))
    data["suite_digest"] = "0" * 64
    config.write_text(json.dumps(data), encoding="utf-8")
    assert analyze.main(["--config", str(config), "--out", str(tmp_path / "o")]) == 1
    assert json.loads(capsys.readouterr().err) == {"error": "bench_compare_mismatch"}
    assert not (tmp_path / "o").exists()


def test_a_bad_config_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    analyze = script()
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "prereg_version": 1,
                "suite_digest": DIGEST,
                "task_id": "receipts",
                "arms": {"A1": "x.jsonl"},
                "hypotheses": {"H2": {"test": "noninferiority", "base": "A1", "cand": "A7"}},
                "holm": [],
            }
        ),
        encoding="utf-8",
    )
    assert analyze.main(["--config", str(bad), "--out", str(tmp_path / "o")]) == 1
    assert json.loads(capsys.readouterr().err) == {"error": "invalid_arguments"}


# -- the gate audit: only the pre-registered exam counts (Phase 14 review) ----------------

PINNED = "e" * 64


def gate_audit(
    analyze: ModuleType,
    tmp_path: Path,
    rows: list[AuditRow],
    *,
    pins: dict[str, Any] | None = None,
    replay: dict[str, Any] | None = None,
) -> Any:
    """The analysis's gate-audit block for ``rows``, with ``replay`` as the replay's
    ``replay.json`` (none when None) and ``pins`` added to the config's window."""
    directory = tmp_path / "replay"
    directory.mkdir(exist_ok=True)
    path = directory / "audit.jsonl"
    path.write_text("".join(row.model_dump_json() + "\n" for row in rows), encoding="utf-8")
    sidecar = directory / "replay.json"
    sidecar.unlink(missing_ok=True)
    if replay is not None:
        sidecar.write_text(
            json.dumps({"audit_version": 1, "replay_id": "r", "include_all": False, **replay}),
            encoding="utf-8",
        )
    config = analyze.PreregConfig.model_validate(
        {
            "prereg_version": 1,
            "suite_digest": DIGEST,
            "task_id": "receipts",
            "arms": {"A7": "rows.jsonl"},
            "hypotheses": {},
            "holm": [],
            "audit": {
                "rows": str(path),
                "since": "2026-10-11T00:00:00Z",
                "until": "2026-10-20T07:00:00Z",
                **(pins or {}),
            },
        }
    )
    return analyze._gate_audit(config)


def test_only_the_pinned_exam_on_a_public_floor_with_a_live_model_counts(
    tmp_path: Path,
) -> None:
    rows = [
        audit_row(1, "2026-10-12T10:00:00Z", "below_bar", True, floor=PINNED),
        audit_row(2, "2026-10-12T11:00:00Z", "passed", True, floor=PINNED),
        audit_row(3, "2026-10-12T12:00:00Z", "invalid_exceeded", True, floor=PINNED),
        audit_row(4, "2026-10-12T13:00:00Z", "error", True, floor=PINNED),
        # Left out: other floors, the fake provider, another floor or another bar.
        audit_row(5, "2026-10-13T10:00:00Z", "below_bar", True, origin="synthetic"),
        audit_row(6, "2026-10-13T11:00:00Z", "error", True, origin="no_manifest"),
        audit_row(7, "2026-10-13T12:00:00Z", "below_bar", True, evidence="synthetic_fake",
                  floor=PINNED),
        audit_row(8, "2026-10-13T13:00:00Z", "below_bar", True, floor="f" * 64),
        audit_row(9, "2026-10-13T14:00:00Z", "below_bar", True, floor=PINNED, bar=0.75),
        audit_row(10, "2026-10-13T15:00:00Z", "below_bar", True),  # no floor hash recorded
        audit_row(11, "2026-10-14T10:00:00Z", "below_bar", False, floor=PINNED),
        audit_row(12, "2026-10-08T10:00:00Z", "below_bar", True, floor="f" * 64),  # pre-tag
    ]
    analyze = script()
    pins = {"floor_cases_sha256": PINNED, "floor_limit": 0.8}
    audit = gate_audit(analyze, tmp_path, rows, pins=pins)
    assert (audit["floor_cases_sha256"], audit["floor_limit"]) == (PINNED, 0.8)
    assert audit["window"] == {
        "replayed": 11,
        "met_bar_in_practice": 2,
        "failed_exam": 1,
        "over_budget": 0,
        "invalid_exceeded": 1,
        "not_countable": 1,
        "practice_drop_over_0_05": 1,
        "excluded": {
            "floor_origin": {"no_manifest": 1, "synthetic": 1},
            "evidence": {"synthetic_fake": 1},
            "other_exam": 3,
        },
    }
    # The secondary count takes every build on a public floor, each at its own bar.
    secondary = audit["all_builds_secondary"]
    assert (secondary["replayed"], secondary["met_bar_in_practice"]) == (12, 6)
    assert secondary["failed_exam"] == 5 and secondary["excluded"]["other_exam"] == 0
    # Without pins the window keeps every public, live, scored exam.
    unpinned = gate_audit(analyze, tmp_path, rows)["window"]
    assert (unpinned["met_bar_in_practice"], unpinned["failed_exam"]) == (5, 4)


def test_a_repeated_submission_is_refused(tmp_path: Path) -> None:
    analyze = script()
    row = audit_row(1, "2026-10-12T10:00:00Z", "below_bar", True)
    again = row.model_copy(update={"replay_id": "another"})
    with pytest.raises(analyze.AnalysisError, match="^bench_audit_unreadable$"):
        gate_audit(analyze, tmp_path, [row, again])


def test_a_windowed_replay_has_no_all_builds_count(tmp_path: Path) -> None:
    analyze = script()
    rows = [audit_row(1, "2026-10-12T10:00:00Z", "below_bar", True)]
    unwindowed = gate_audit(analyze, tmp_path, rows, replay={"since": None, "until": None})
    assert unwindowed["all_builds_secondary"] == unwindowed["window"]
    wider = gate_audit(
        analyze, tmp_path, rows,
        replay={"since": "2026-10-01T00:00:00Z", "until": "2026-10-21T00:00:00Z"},
    )
    assert wider["window"]["failed_exam"] == 1 and wider["all_builds_secondary"] is None
    # A replay window that opens late or closes early misses builds the count must hold.
    late = {"since": "2026-10-12T00:00:00Z", "until": None}
    early = {"since": None, "until": "2026-10-19T00:00:00Z"}
    for window in (late, early):
        with pytest.raises(analyze.AnalysisError, match="^bench_audit_unreadable$"):
            gate_audit(analyze, tmp_path, rows, replay=window)


# -- a missing arm fails only the hypotheses that need it (prereg 7.5 and 11.3) ----------


def run(config: Path, out: Path) -> dict[str, Any]:
    assert script().main(["--config", str(config), "--out", str(out)]) == 0
    result: dict[str, Any] = json.loads((out / "analysis.json").read_text(encoding="ascii"))
    return result


def test_a_missing_arm_fails_only_its_hypotheses_and_holm_still_counts_two(
    config: Path, tmp_path: Path
) -> None:
    full = run(config, tmp_path / "full")
    assert full["missing_arms"] == {}
    (tmp_path / "rows" / "A5.jsonl").unlink()  # A5 crashed twice: its cell is missing
    analysis = run(config, tmp_path / "out")
    assert analysis["missing_arms"] == {"A5": "no_rows"}
    h1b = analysis["hypotheses"]["H1b"]
    assert (h1b["holds"], h1b["pair"], h1b["missing"]) == (False, None, ["A5"])
    # H1a is still corrected as one of the two pre-registered H1 tests.
    h1a = analysis["hypotheses"]["H1a"]
    assert h1a["missing"] == [] and h1a["pair"]["mcnemar_p"] == (
        full["hypotheses"]["H1a"]["pair"]["mcnemar_p"]
    )
    assert h1a["pair"]["holm_p"] == min(1.0, 2 * h1a["pair"]["mcnemar_p"])
    assert h1a["holds"] is h1a["pair"]["better"]
    for name in ("H2", "H3"):  # tests that do not need A5 are unchanged
        assert analysis["hypotheses"][name] == full["hypotheses"][name]
    assert analysis["replicates"]["H1b"]["missing"] == ["A5"]
    assert analysis["replicates"]["H2"] == full["replicates"]["H2"]
    assert analysis["test_subset"]["H1b"]["pair"] is None
    assert analysis["gate_audit"] == full["gate_audit"]
    markdown = (tmp_path / "out" / "analysis.md").read_text(encoding="utf-8")
    assert "- A5: no rows file" in markdown and "## Missing arms" not in (
        tmp_path / "full" / "analysis.md"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("update", "reason"),
    [
        ({"failures": ("cancelled",)}, "crashed"),
        ({"failures": ("host_memory_low",)}, "crashed"),
        ({"delivery_pass_bits": None}, "no_delivery"),
        ({"label": "A7c"}, "no_row"),
    ],
)
def test_a_crashed_cell_or_one_with_nothing_to_pair_is_missing(
    config: Path, tmp_path: Path, update: dict[str, Any], reason: str
) -> None:
    path = tmp_path / "rows" / "A7b.jsonl"
    [row] = read_rows(path)
    write_rows(path, [row.model_copy(update=update)])
    analysis = run(config, tmp_path / "out")
    assert analysis["missing_arms"] == {"A7b": reason}
    assert all(result["holds"] is False for result in analysis["replicates"].values())
    assert all(result["missing"] == [] for result in analysis["hypotheses"].values())


def test_unreadable_rows_and_an_analysis_with_no_arm_are_still_refused(
    config: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    analyze = script()
    (tmp_path / "rows" / "A7b.jsonl").write_text("not a row\n", encoding="utf-8")
    assert analyze.main(["--config", str(config), "--out", str(tmp_path / "a")]) == 1
    assert json.loads(capsys.readouterr().err) == {"error": "bench_rows_unreadable"}
    for path in (tmp_path / "rows").iterdir():
        path.unlink()
    assert analyze.main(["--config", str(config), "--out", str(tmp_path / "b")]) == 1
    assert json.loads(capsys.readouterr().err) == {"error": "bench_compare_mismatch"}
    assert not (tmp_path / "a").exists() and not (tmp_path / "b").exists()

