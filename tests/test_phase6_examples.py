"""Synthetic consumer examples are external programs, not importable package logic."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from time import perf_counter

import pytest

from architect.floors import HumanEditRate
from architect.profiles import build_services, load_profile
from architect.spec import Spec
from architect.telemetry import TelemetryRow

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples/lectern"


def run_examples(workspace: Path, *args: str) -> dict:
    start = perf_counter()
    completed = subprocess.run(
        [sys.executable, str(EXAMPLES / "run.py"), "--workspace", str(workspace), *args],
        cwd=ROOT, capture_output=True, text=True, timeout=60, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert perf_counter() - start < 60
    result = json.loads(completed.stdout)
    assert result == json.loads((workspace / "summary.json").read_text())
    return result


def test_full_examples_native_subprocess_under_sixty_seconds(tmp_path: Path) -> None:
    summary = run_examples(tmp_path, "--approve-fixtures")
    assert summary["evidence"] == "synthetic-offline-only"
    assert summary["production_approved"] is False
    rows = {row["name"]: row for row in summary["examples"]}
    assert set(rows) == {"report-cards", "test-digitizer", "content-recommender"}
    for name, row in rows.items():
        assert row["licensed"] and row["registry_revision"] == 1
        assert row["stages"] == [1, 2, 4]
        assert row["application_gate_refusals"] == 2
        assert row["production_feature_enabled"] is False
        assert row["human_review_required"] is True
        assert row["real_monthly_cost_baseline"] is None
        assert row["report"]["passed"] and row["report"]["signature"]
        assert row["delivery_tokens"] > 0 and row["delivery_cost_usd"] > 0
        proposal = json.loads((tmp_path / name / "proposed-spec.json").read_text())
        reviewed = json.loads((tmp_path / name / "reviewed-spec.json").read_text())
        assert reviewed == proposal
        approval = json.loads((tmp_path / name / "fixture-approval.json").read_text())
        assert approval["spec_hash"] == Spec.model_validate(proposal).spec_hash
        assert approval["production_approved"] is False
        assert approval["human_review_performed"] is False
        telemetry_paths = list((tmp_path / name / "telemetry").glob("*.jsonl"))
        assert telemetry_paths
        telemetry = [TelemetryRow.model_validate_json(line) for path in telemetry_paths
                     for line in path.read_text().splitlines()]
        assert any(event.floor_score is not None and event.accepted for event in telemetry)
        assert any(event.run_id == row["run_id"] for event in telemetry)
    report = rows["report-cards"]
    assert report["report"]["floor_kind"] == "human_edit_rate"
    assert report["report"]["floor_score"] == 0
    assert report["tuning_count"] == report["report"]["held_out_count"] == 100
    digitizer = rows["test-digitizer"]
    assert digitizer["proposal_stages"] == [0]
    assert digitizer["inference_adapter"] == "scripted-synthetic-not-llm-quality"
    assert digitizer["delivery_model_calls"] == {
        "gpt-4o-mini": 1, "llama3.2": 1, "claude-sonnet-4-20250514": 1,
    }
    assert digitizer["delivery_tokens"] == 360
    proposal = json.loads((tmp_path / "test-digitizer/proposed-spec.json").read_text())
    assert proposal["provenance"]["pipeline"] == "inferred"
    assert [step["name"] for step in proposal["pipeline"]] == [
        "vision-extract", "solver-rederive", "verifier-diff",
    ]
    request = proposal["task"]
    assert request in (ROOT / "docs/build-prompt.md").read_text(encoding="utf-8")
    output = json.loads((tmp_path / "test-digitizer/synthetic-output.json").read_text())
    assert output == {"answer_keys": [{"id": "q00", "answer": 3}],
                      "disagreements": ["q00"], "review_required": True}
    assert rows["content-recommender"]["report"]["floor_score"] == 1


def test_installed_examples_default_to_proposal_only(tmp_path: Path) -> None:
    summary = run_examples(tmp_path)
    for row in summary["examples"]:
        assert row["approval_required"] and not row["licensed"]
        assert row["fixture_approval"] is False
        assert "stages" not in row
        directory = tmp_path / row["name"]
        assert (directory / "proposed-spec.json").exists()
        assert not (directory / "reviewed-spec.json").exists()
        assert not (directory / "registry").exists()
        assert not (directory / "artifacts").exists()


def test_schema_valid_wrong_outputs_fail_all_real_floors(tmp_path: Path) -> None:
    summary = run_examples(tmp_path, "--approve-fixtures", "--variant", "floor-failure")
    for row in summary["examples"]:
        assert not row["licensed"]
        assert row["stages"] == [1, 2, 4]
        assert row["report"]["passed"] is False
        assert "primary_floor_failed" in row["report"]["failures"]
        expected = 1 if row["name"] == "report-cards" else 0
        assert row["report"]["floor_score"] == expected
        # A report's worst-score fallback alone is not evidence that the
        # programmatic checker ran. Require actual numeric quality rows.
        telemetry = [json.loads(line)
                     for path in (tmp_path / row["name"] / "telemetry").glob("*.jsonl")
                     for line in path.read_text().splitlines()]
        scored = [event for event in telemetry if event["floor_score"] is not None]
        assert len(scored) >= 2
        assert all(event["floor_score"] == expected and not event["accepted"]
                   for event in scored)
        assert not (tmp_path / row["name"] / "registry").exists()
        assert not (tmp_path / row["name"] / "synthetic-output.json").exists()
        if row["name"] == "test-digitizer":
            assert len(row["model_calls"]) == 3
            assert all(count > 0 for count in row["model_calls"].values())


def test_edit_log_has_two_large_synthetic_partitions() -> None:
    spec = Spec.model_validate_json((EXAMPLES / "specs/report-cards.json").read_text())
    path = EXAMPLES / "fixtures/report-edits.jsonl"
    floor = HumanEditRate.from_jsonl(spec.floor, path)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(records) == 200
    assert len(floor.tuning) == 100
    assert spec.floor.minimum_samples >= 30
    assert all(row["task"]["synthetic"] for row in records)
    assert all(row["changed"] == (row["proposed"] != row["shipped"]) for row in records)
    assert any(row["changed"] for row in records)


@pytest.mark.asyncio
async def test_stock_profile_is_server_ready(tmp_path: Path) -> None:
    services = build_services(load_profile(EXAMPLES / "profiles/content-recommender.json"),
                              tmp_path)
    try:
        spec = Spec.model_validate_json(
            (EXAMPLES / "specs/content-recommender.json").read_text())
        proposed = await services.compiler.compile(spec)
        assert proposed.approval_required and proposed.artifact is None
        compiled = await services.compiler.compile(proposed.spec, approved=True)
        assert compiled.artifact is not None and compiled.artifact.licensed
        assert compiled.report is not None and compiled.report.held_out_count == 6
        services.registry.install("synthetic-content", compiled.artifact)
        artifact = services.registry.get("synthetic-content")
        task = json.loads((EXAMPLES / "fixtures/content-recommender-input.json").read_text())
        run = await services.runtime.run(artifact, task, services.adapter_factory(artifact.model))
        assert run.output == {"content_id": "fractions-practice", "review_required": True}
    finally:
        services.close()
