"""Phase 14 review: the gate-audit count can be inflated by exams that are not the
pre-registered one.

The pre-registration (docs/bench/prereg-nebius.md, section 10) fixes the count as
"n of m" over submissions replayed on the evidence floor: "A replay counts only when its
floor matches that manifest (floor_origin is public_dataset)", and errored replays are
counted apart. The count is the always-allowed headline (PLAN section 0) and the first
number in the video, so nothing outside that rule may enter n or m.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from architect.bench import public_tasks
from architect.bench.audit import AuditRequest, AuditRow, replay_audit
from architect.compiler import Compiler
from architect.floors import EvaluationCase, HeldOutTasks
from architect.models import FakeAdapter
from architect.profiles import load_profile
from architect.runtime import Runtime
from architect.spec import Budget, FloorDefinition, FloorKind, HarnessArtifact, Spec
from architect.telemetry import TelemetryStore

ROOT = Path(__file__).resolve().parents[2]
SESSION = "d" * 32


def analysis_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "analyze_prereg_review", ROOT / "scripts" / "analyze_prereg.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["analyze_prereg_review"] = module
    spec.loader.exec_module(module)
    return module


def audit_row(
    index: int, *, exam: str, origin: str, evidence: str = "live_billed", met: bool = True
) -> AuditRow:
    below = exam == "below_bar"
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
        artifact_digest=f"{index:064x}",
        verdict="accepted",
        floor_limit=0.8,
        tuning_score=0.9,
        tuning_correct=90,
        tuning_cases=100,
        tuning_invalid=0,
        met_bar_in_practice=met,
        exam=exam,  # type: ignore[arg-type]
        held_out_score=0.7 if below else 0.0 if exam == "error" else 0.85,
        held_out_correct=70 if below else None if exam == "error" else 85,
        held_out_cases=100,
        held_out_invalid=0,
        held_out_over_budget=0,
        practice_drop=0.2 if below else None if exam == "error" else 0.05,
        floor_origin=origin,  # type: ignore[arg-type]
        evidence=evidence,  # type: ignore[arg-type]
        replay_cost_usd=0.01,
    )


def window_counts(tmp_path: Path, rows: list[AuditRow]) -> Any:
    analyze = analysis_script()
    path = tmp_path / "audit.jsonl"
    path.write_text("".join(row.model_dump_json() + "\n" for row in rows), encoding="utf-8")
    config = analyze.PreregConfig.model_validate(
        {
            "prereg_version": 1,
            "suite_digest": "c" * 64,
            "task_id": "receipts",
            "arms": {"A7": "rows.jsonl"},
            "hypotheses": {},
            "holm": [],
            "audit": {
                "rows": str(path),
                "since": "2026-10-11T00:00:00Z",
                "until": "2026-10-20T07:00:00Z",
            },
        }
    )
    return analyze._gate_audit(config)


def test_only_replays_on_a_public_dataset_floor_enter_the_preregistered_count(
    tmp_path: Path,
) -> None:
    rows = [
        audit_row(1, exam="below_bar", origin="public_dataset"),
        audit_row(2, exam="passed", origin="public_dataset"),
        # A floor that failed its manifest, a floor with no manifest, and a submission
        # that was never replayed: none of them is an exam on the evidence floor.
        audit_row(3, exam="below_bar", origin="synthetic"),
        audit_row(4, exam="below_bar", origin="no_manifest"),
        audit_row(5, exam="error", origin="no_manifest"),
    ]
    window = window_counts(tmp_path, rows)["window"]
    # The pre-registered n of m: 1 of 2.
    assert (window["failed_exam"], window["met_bar_in_practice"]) == (1, 2), window


def test_fake_provider_replays_never_enter_the_preregistered_count(tmp_path: Path) -> None:
    analyze = analysis_script()
    rows = [
        audit_row(1, exam="below_bar", origin="public_dataset"),
        audit_row(2, exam="below_bar", origin="public_dataset", evidence="synthetic_fake"),
        audit_row(3, exam="passed", origin="public_dataset", evidence="synthetic_fake"),
    ]
    try:
        window = window_counts(tmp_path, rows)["window"]
    except analyze.AnalysisError:
        return  # refusing a fake replay outright is also acceptable
    assert (window["failed_exam"], window["met_bar_in_practice"]) == (1, 1), window


# -- the replay's exam is whatever floor the replay profile names --------------------------


def floor_cases(expected: list[str]) -> list[EvaluationCase]:
    return [
        EvaluationCase(case_id=f"case-{index:02d}", task={"n": index}, expected=value)
        for index, value in enumerate(expected)
    ]


async def licensed_on_eight_cases(root: Path) -> HarnessArtifact:
    spec = Spec(
        task="Return ok",
        input_schema={"type": "object"},
        output_schema={"type": "string"},
        domain="synthesizer",
        floor=FloorDefinition(
            kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="audit-floor", minimum_samples=4
        ),
        budget=Budget(cost_usd=0.1, compute_s=60),
    )

    def factory(definition: FloorDefinition) -> HeldOutTasks:
        return HeldOutTasks(definition, floor_cases(["ok"] * 8))

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


def lay_out_submission(artifact: HarnessArtifact, audit: Path) -> None:
    """Submission 1 as the Forge audit records it: practised on 4 tuning cases, all right."""
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
        "floor_limit": 1.0,
        "tuning": {
            "score": 1.0,
            "floor_passed": True,
            "budget_passed": True,
            "mean_cost_usd": 0.0001,
            "max_cost_usd": 0.0001,
            "max_compute_s": 0.1,
            "sample_count": 4,
            "counts": {"cases": 4, "correct": 4, "invalid": 0, "over_budget": 0},
            "failures": [],
        },
        "smoke_passed": True,
        "recorded_at": "2026-10-12T10:00:00+00:00",
    }
    (directory / "submission.json").write_text(
        json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )


def profile_with_manifest(root: Path, cases: list[EvaluationCase]) -> Path:
    """A profile naming ``audit-floor`` with a manifest that matches ``cases`` exactly."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "floor.json").write_text(
        json.dumps([case.model_dump(mode="json") for case in cases]), encoding="utf-8"
    )
    manifest = {
        "manifest_version": 1,
        "dataset": "naver-clova-ix/cord-v2",
        "dataset_revision": "7f0115a4b758a71d6473b8d085751692da2fef98",
        "license": "CC-BY-4.0",
        "rule_version": "receipts-v1",
        "count": len(cases),
        "cases": [
            {"case_id": case.case_id, "case_sha256": public_tasks.case_sha256(case)}
            for case in cases
        ],
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    path = root / "profile.json"
    path.write_text(
        json.dumps(
            {
                "models": {"fake": {"provider": "fake"}},
                "floors": {
                    "audit-floor": {
                        "kind": "held_out_tasks",
                        "path": "floor.json",
                        "manifest": "manifest.json",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def replayed_rows(rows_path: str) -> list[AuditRow]:
    lines = Path(rows_path).read_text(encoding="utf-8").splitlines()
    return [AuditRow.model_validate_json(line) for line in lines]


async def test_the_replay_refuses_a_floor_the_harness_did_not_practise_on(
    tmp_path: Path,
) -> None:
    artifact = await licensed_on_eight_cases(tmp_path / "build")
    audit = tmp_path / "audit"
    lay_out_submission(artifact, audit)
    # The same dataset name now points at another case set (12 cases, so 6 practice
    # cases where the harness practised on 4) with a manifest that matches it, and an
    # exam the harness fails.
    other = floor_cases(["ok", "nope"] * 6)
    swapped = load_profile(profile_with_manifest(tmp_path / "swapped", other))
    request = AuditRequest(directory=audit, out=tmp_path / "replays")
    result = await replay_audit(
        request, swapped, consent=lambda line: True, workspace_root=tmp_path / "ws"
    )
    [row] = replayed_rows(result.rows_path)
    # Without the practice check this row came back public_dataset and below_bar, and
    # the gate audit counted it as a harness that failed an exam it never saw.
    assert row.exam == "error", (row.floor_origin, row.exam, row.held_out_cases)
