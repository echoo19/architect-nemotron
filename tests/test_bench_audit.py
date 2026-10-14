"""``bench audit`` (Phase 14 Section 13.2) on a fake audit directory, offline.

One licensed artifact is compiled with the fake model and laid out as Forge audit
submissions; the replay copies each into a throwaway workspace, calls Stage 4's
``verify()`` once and records content-free rows. The audit directory is only read.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from architect.bench import public_tasks
from architect.bench.audit import (
    AuditReplay,
    AuditRequest,
    AuditResult,
    AuditRow,
    audit_table,
    classify,
    exclusion,
    read_submissions,
    replay_audit,
)
from architect.bench.rows import BenchError
from architect.cli import main as cli_main
from architect.compiler import Compiler
from architect.floors import EvaluationCase, HeldOutTasks
from architect.floors.provenance import cases_sha256
from architect.models import FakeAdapter
from architect.profiles import load_profile
from architect.runtime import Runtime
from architect.spec import Budget, FloorDefinition, FloorKind, HarnessArtifact, Spec, TestReport
from architect.telemetry import TelemetryStore

COUNTS_DECLARED = "held_out_counts" in TestReport.model_fields  # WA's counts (Section 3.2)
SESSION = "a" * 32
EVIDENCE = Path(__file__).resolve().parents[1] / "examples" / "receipts"


def floor_cases(expected: list[str]) -> list[EvaluationCase]:
    return [
        EvaluationCase(case_id=f"case-{index:02d}", task={"n": index}, expected=value)
        for index, value in enumerate(expected)
    ]


async def compiled(root: Path) -> HarnessArtifact:
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


def submission(
    artifact: HarnessArtifact,
    audit: Path,
    number: int,
    *,
    session: str = SESSION,
    tuning_score: float | None = 1.0,
    failures: tuple[str, ...] = (),
    recorded_at: str = "2026-10-12T10:00:00+00:00",
    model: str = "fake",
    practice_sha256: str | None = None,
) -> Path:
    """Lay ``artifact`` out as Forge audit submission ``number`` (Section 8.4)."""
    spec_hash = artifact.spec.spec_hash
    directory = audit / spec_hash / session / str(number)
    directory.mkdir(parents=True)
    for name in ("harness.py", "spec.json", "config.json"):
        shutil.copy2(artifact.directory / name, directory / name)
    code = hashlib.sha256((directory / "harness.py").read_bytes()).hexdigest()
    tuning: dict[str, Any] | None = None
    if tuning_score is not None:
        tuning = {
            "score": tuning_score,
            "floor_passed": not failures,
            "budget_passed": True,
            "mean_cost_usd": 0.0001,
            "max_cost_usd": 0.0001,
            "max_compute_s": 0.1,
            "sample_count": 4,
            "counts": {"cases": 4, "correct": round(tuning_score * 4), "invalid": 0,
                       "over_budget": 0},
            "failures": list(failures),
        }
        if practice_sha256 is not None:
            tuning["cases_sha256"] = practice_sha256
    record = {
        "audit_version": 1,
        "spec_hash": spec_hash,
        "session_id": session,
        "submission": number,
        "depth": 0,
        "mode": "agent",
        "model": model,
        "agent_model": "fake",
        "code_sha256": code,
        "artifact_digest": artifact.artifact_digest,
        "verdict": "accepted" if not failures else "not_better",
        "floor_limit": 1.0,
        "tuning": tuning,
        "smoke_passed": True,
        "recorded_at": recorded_at,
    }
    (directory / "submission.json").write_text(
        json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return directory


def profile(
    root: Path,
    expected: list[str],
    name: str = "profile.json",
    *,
    manifest: bool = False,
    provider: str = "fake",
) -> Path:
    """A profile naming ``audit-floor``; ``manifest`` adds a sidecar manifest that the
    floor matches, and ``provider`` labels the model (its calls are faked in tests)."""
    cases = floor_cases(expected)
    (root / f"{name}.floor.json").write_text(
        json.dumps([case.model_dump(mode="json") for case in cases]), encoding="utf-8"
    )
    floor: dict[str, Any] = {"kind": "held_out_tasks", "path": f"{name}.floor.json"}
    if manifest:
        sidecar = {
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
        (root / f"{name}.manifest.json").write_text(json.dumps(sidecar), encoding="utf-8")
        floor["manifest"] = f"{name}.manifest.json"
    model: dict[str, Any] = {"provider": provider}
    if provider == "openai":
        model.update({"base_url": "https://relay.invalid/v1", "api_key_ref": "offline"})
    path = root / name
    path.write_text(
        json.dumps({"models": {"fake": model}, "floors": {"audit-floor": floor}}),
        encoding="utf-8",
    )
    return path


def practice(expected: list[str]) -> str:
    """The ``cases_sha256`` of the practice partition of a floor of ``expected``."""
    definition = FloorDefinition(
        kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="audit-floor", minimum_samples=4
    )
    return cases_sha256(HeldOutTasks(definition, floor_cases(expected)).tuning)


def fake_calls(model: str) -> FakeAdapter:
    return FakeAdapter(model=model)


def rows_text(result: AuditResult) -> str:
    return Path(result.rows_path).read_text(encoding="utf-8")


def audit_rows(result: AuditResult) -> list[AuditRow]:
    return [AuditRow.model_validate_json(line) for line in rows_text(result).splitlines()]


def snapshot(directory: Path) -> dict[str, tuple[int, bytes]]:
    return {
        path.relative_to(directory).as_posix(): (path.stat().st_mtime_ns, path.read_bytes())
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


@pytest.fixture
async def audit(tmp_path: Path) -> tuple[Path, HarnessArtifact]:
    artifact = await compiled(tmp_path / "build")
    directory = tmp_path / "audit"
    submission(artifact, directory, 1)  # met the bar in practice
    submission(artifact, directory, 2, tuning_score=0.75, failures=("floor_not_met",))
    submission(artifact, directory, 1, session="b" * 32, recorded_at="2026-10-02T09:00:00Z")
    submission(artifact, directory, 3, tuning_score=None)  # never measured
    return directory, artifact


async def test_the_replay_selects_verifies_and_counts(
    tmp_path: Path, audit: tuple[Path, HarnessArtifact]
) -> None:
    directory, artifact = audit
    before = snapshot(directory)
    lines: list[str] = []
    request = AuditRequest(
        directory=directory,
        since=datetime(2026, 10, 11, tzinfo=UTC),
        out=tmp_path / "replays",
    )
    passing = load_profile(profile(tmp_path, ["ok"] * 8))

    def agree(line: str) -> bool:
        lines.append(line)
        return True

    result = await replay_audit(
        request, passing, consent=agree, workspace_root=tmp_path / "ws"
    )
    assert lines == [
        "bench audit on 1 submissions: <=$0.4000 API-equivalent bound, ~4 runs, not a bill."
    ]
    assert result.submissions == 1 and result.passed == 1 and result.errors == 0
    # A fake-provider replay on a floor with no manifest never enters the gate audit.
    assert (result.met_bar, result.gate_audit, result.excluded_floor) == (0, 0, 1)
    assert (result.excluded_fake, result.excluded_unscored) == (0, 0)
    assert result.evidence == ("synthetic_fake",) and result.floor_origins == ("no_manifest",)
    [row] = audit_rows(result)
    assert (row.session_id, row.submission, row.exam) == (SESSION, 1, "passed")
    assert row.artifact_digest == artifact.artifact_digest and row.met_bar_in_practice
    assert row.tuning_score == 1.0 and row.tuning_correct == 4 and row.tuning_cases == 4
    assert row.held_out_score == 1.0 and row.held_out_cases == 4
    assert row.evidence == "synthetic_fake" and row.floor_origin == "no_manifest"
    assert row.floor_cases_sha256 == cases_sha256(floor_cases(["ok"] * 8))
    assert row.replay_cost_usd > 0
    replay = AuditReplay.model_validate_json(
        (Path(result.rows_path).parent / "replay.json").read_text(encoding="utf-8")
    )
    assert (replay.replay_id, replay.since, replay.until, replay.include_all) == (
        result.replay_id, datetime(2026, 10, 11, tzinfo=UTC), None, False
    )
    text = rows_text(result)
    assert "verification_id" not in text and "case-0" not in text
    assert snapshot(directory) == before  # the audit directory is only read
    assert list((tmp_path / "ws").iterdir()) == []  # every workspace was removed
    table = audit_table(result)
    joined = " ".join(table.splitlines())
    assert "Gate audit: 0 of 0 harnesses that met the bar in practice" in joined
    assert (
        "Left out: 1 on other floors, 0 replayed with the fake provider, 0 with no exam "
        "score." in joined
    )
    assert all(len(line) <= 100 and line.isascii() for line in table.splitlines())


async def test_all_takes_every_measured_submission_and_a_failed_exam_is_counted(
    tmp_path: Path, audit: tuple[Path, HarnessArtifact]
) -> None:
    directory, _ = audit
    expected = ["ok", "nope"] * 4
    # A live model on a floor that matches its manifest: the gate audit's own rows.
    failing = load_profile(
        profile(tmp_path, expected, name="hard.json", manifest=True, provider="openai")
    )
    request = AuditRequest(directory=directory, include_all=True, out=tmp_path / "replays")
    result = await replay_audit(
        request, failing, consent=lambda line: True, factory=fake_calls,
        workspace_root=tmp_path / "ws",
    )
    rows = audit_rows(result)
    # Oldest first; the never-measured submission is never selected.
    assert [(row.session_id[0], row.submission) for row in rows] == [
        ("b", 1), ("a", 1), ("a", 2)
    ]
    assert [row.met_bar_in_practice for row in rows] == [True, True, False]
    assert all(row.held_out_score == 0.0 for row in rows)  # every held-out case answers "ok"
    assert {(row.floor_origin, row.evidence) for row in rows} == {
        ("public_dataset", "live_billed")
    }
    if COUNTS_DECLARED:
        assert {row.exam for row in rows} == {"below_bar"}
        assert (result.gate_audit, result.met_bar, result.below_bar) == (2, 2, 3)
        assert all(row.practice_drop is not None for row in rows[:2])
    else:  # without signed counts a missed floor cannot be told from an unscored one
        assert {row.exam for row in rows} == {"error"}
        assert (result.gate_audit, result.met_bar, result.excluded_unscored) == (0, 0, 2)
        assert result.errors == 3
    # The same exams replayed on the fake provider, or on a floor with no manifest,
    # never enter the count: they are left out by reason.
    for options, field in (({"manifest": True}, "excluded_fake"), ({}, "excluded_floor")):
        name = f"{field}.json"
        other = load_profile(profile(tmp_path, expected, name=name, **options))
        again = await replay_audit(
            request, other, consent=lambda line: True, workspace_root=tmp_path / "ws"
        )
        assert (again.gate_audit, again.met_bar, getattr(again, field)) == (0, 0, 2), field


async def test_consent_and_money_come_before_any_spend(
    tmp_path: Path, audit: tuple[Path, HarnessArtifact]
) -> None:
    directory, _ = audit
    passing = load_profile(profile(tmp_path, ["ok"] * 8))
    out = tmp_path / "replays"
    with pytest.raises(BenchError, match="bench_cancelled"):
        await replay_audit(AuditRequest(directory=directory, out=out), passing,
                           consent=lambda line: False)
    with pytest.raises(BenchError, match="bench_over_max_usd"):
        await replay_audit(AuditRequest(directory=directory, max_usd=0.01, out=out), passing,
                           consent=lambda line: True)
    assert not out.exists()
    window = AuditRequest(
        directory=directory, until=datetime(2026, 10, 1, tzinfo=UTC), out=out
    )
    result = await replay_audit(window, passing, consent=lambda line: False)
    assert result.submissions == 0  # nothing selected: nothing to consent to or spend
    assert rows_text(result) == ""


async def test_a_missing_floor_or_a_changed_harness_is_an_error_row(
    tmp_path: Path, audit: tuple[Path, HarnessArtifact]
) -> None:
    directory, artifact = audit
    path = tmp_path / "nofloor.json"
    path.write_text(json.dumps({"models": {"fake": {"provider": "fake"}}}), encoding="utf-8")
    request = AuditRequest(directory=directory, since=datetime(2026, 10, 11, tzinfo=UTC),
                           out=tmp_path / "replays")
    result = await replay_audit(request, load_profile(path), consent=lambda line: True)
    assert result.errors == 1 and result.passed == 0
    tampered = tmp_path / "tampered"
    shutil.copytree(directory, tampered)
    harness = tampered / artifact.spec.spec_hash / SESSION / "1" / "harness.py"
    harness.write_text(harness.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    passing = load_profile(profile(tmp_path, ["ok"] * 8))
    result = await replay_audit(
        request.model_copy(update={"directory": tampered}), passing, consent=lambda line: True
    )
    assert result.errors == 1 and result.replay_cost_usd == 0.0


async def test_unreadable_audits_and_unknown_models_are_refused(
    tmp_path: Path, audit: tuple[Path, HarnessArtifact]
) -> None:
    directory, artifact = audit
    passing = load_profile(profile(tmp_path, ["ok"] * 8))
    with pytest.raises(BenchError, match="bench_audit_unreadable"):
        read_submissions(tmp_path / "nowhere")
    broken = tmp_path / "broken"
    shutil.copytree(directory, broken)
    record = broken / artifact.spec.spec_hash / SESSION / "1" / "submission.json"
    record.write_text("{not json", encoding="utf-8")
    with pytest.raises(BenchError, match="bench_audit_unreadable"):
        read_submissions(broken)
    moved = tmp_path / "moved"
    shutil.copytree(directory, moved)
    (moved / artifact.spec.spec_hash / SESSION).rename(
        moved / artifact.spec.spec_hash / ("c" * 32)
    )
    with pytest.raises(BenchError, match="bench_audit_unreadable"):
        read_submissions(moved)  # the ids no longer match their own path
    other = tmp_path / "other"
    submission(artifact, other, 1, model="claude-haiku-4-5-20251001")
    with pytest.raises(BenchError, match="bench_model_unconfigured"):
        await replay_audit(AuditRequest(directory=other, out=tmp_path / "r"), passing,
                           consent=lambda line: True)


def report_of(**values: Any) -> Any:
    defaults: dict[str, Any] = {"passed": False, "failures": (), "floor_score": 0.5}
    return SimpleNamespace(**{**defaults, **values})


def test_exam_classes() -> None:
    counts = SimpleNamespace(cases=100, correct=50, invalid=1, over_budget=0)
    assert classify(report_of(passed=True), 0.8) == "passed"
    assert classify(report_of(failures=("primary_budget_failed",)), 0.8) == "over_budget"
    assert (
        classify(report_of(failures=("primary_invalid_exceeded", "primary_floor_failed")), 0.8)
        == "invalid_exceeded"
    )
    scored = report_of(failures=("primary_floor_failed",), held_out_counts=counts)
    assert classify(scored, 0.8) == "below_bar"
    unscored = SimpleNamespace(cases=100, correct=None, invalid=3, over_budget=0)
    assert classify(report_of(held_out_counts=unscored), 0.8) == "error"
    assert classify(report_of(failures=("primary_floor_failed",)), 0.8) == "error"  # no counts
    assert classify(report_of(failures=("artifact_changed",), held_out_counts=counts,
                              floor_score=0.9), 0.8) == "error"


def test_the_manifest_origin_of_a_floor() -> None:
    public_tasks.clear_cache()
    evidence = list(public_tasks.load_public("receipts").splits["evidence"])
    manifest = EVIDENCE / "manifests" / "evidence.json"
    assert public_tasks.manifest_origin(evidence, manifest) == "public_dataset"
    changed = [*evidence[:-1], evidence[-1].model_copy(update={"expected": {"x": 1}})]
    assert public_tasks.manifest_origin(changed, manifest) == "synthetic"
    stamped = [evidence[0].model_copy(update={"metadata": {"synthetic": True}}), *evidence[1:]]
    assert public_tasks.manifest_origin(stamped, manifest) == "synthetic"
    assert public_tasks.manifest_origin(evidence[:-1], manifest) == "synthetic"
    assert public_tasks.manifest_origin(evidence, EVIDENCE / "missing.json") == "synthetic"


def test_cli_audit(
    tmp_path: Path,
    audit: tuple[Path, HarnessArtifact],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory, _ = audit
    path = profile(tmp_path, ["ok"] * 8)
    out = tmp_path / "replays"

    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # not a console: no consent
    code = cli_main(["bench", "audit", str(directory), "--profile", str(path), "--out", str(out)])
    captured = capsys.readouterr()
    assert code == 1 and json.loads(captured.err.strip().splitlines()[-1]) == {
        "error": "bench_cancelled"
    }
    assert captured.out.startswith("bench audit on 2 submissions: <=$0.8000")  # both met the bar
    code = cli_main(
        ["bench", "audit", str(directory), "--profile", str(path), "--out", str(out),
         "--since", "2026-10-11", "--yes", "--json"]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    result = AuditResult.model_validate_json(captured.out)
    assert result.submissions == 1 and result.passed == 1
    code = cli_main(["bench", "audit", str(tmp_path / "none"), "--profile", str(path), "--yes"])
    assert code == 1 and json.loads(capsys.readouterr().err) == {
        "error": "bench_audit_unreadable"
    }


async def test_a_floor_the_submission_never_practised_on_is_not_replayed(
    tmp_path: Path, audit: tuple[Path, HarnessArtifact]
) -> None:
    """Phase 14 review: the floor name alone does not make an exam the build's own. A
    profile floor whose practice cases differ from the measured ones (by the recorded
    hash, or for older records by their number) is an error row with no spend."""
    directory, artifact = audit
    measured = ["ok"] * 8
    hashed = tmp_path / "hashed"
    submission(artifact, hashed, 1, practice_sha256=practice(measured))
    request = AuditRequest(directory=hashed, out=tmp_path / "replays")
    # Same size, other practice cases: only the recorded hash tells them apart.
    swapped = [*measured]
    swapped[0] = "nope"
    lines: list[str] = []
    result = await replay_audit(
        request, load_profile(profile(tmp_path, swapped, name="swapped.json")),
        consent=lambda line: lines.append(line) or True, workspace_root=tmp_path / "ws",
    )
    [row] = audit_rows(result)
    assert row.exam == "error" and result.replay_cost_usd == 0.0
    assert row.floor_cases_sha256 == cases_sha256(floor_cases(swapped))
    assert lines == [
        "bench audit on 1 submissions: <=$0.0000 API-equivalent bound, ~0 runs, not a bill."
    ]
    # The floor it practised on replays as usual.
    result = await replay_audit(
        request, load_profile(profile(tmp_path, measured)), consent=lambda line: True,
        workspace_root=tmp_path / "ws",
    )
    [row] = audit_rows(result)
    assert row.exam == "passed" and result.replay_cost_usd > 0
    # A record without the hash still needs as many practice cases as it measured.
    longer = load_profile(profile(tmp_path, ["ok"] * 12, name="longer.json"))
    result = await replay_audit(
        AuditRequest(directory=directory, since=datetime(2026, 10, 11, tzinfo=UTC),
                     out=tmp_path / "replays"),
        longer, consent=lambda line: True, workspace_root=tmp_path / "ws",
    )
    [row] = audit_rows(result)
    assert row.exam == "error" and result.replay_cost_usd == 0.0


def test_the_exclusion_reasons_come_in_order() -> None:
    base = {
        "audit_version": 1, "replay_id": "r", "spec_hash": "a" * 64, "session_id": "b" * 32,
        "submission": 1, "depth": 0, "mode": "agent", "model": "fake", "agent_model": None,
        "recorded_at": "2026-10-12T10:00:00Z", "artifact_digest": "c" * 64,
        "verdict": "accepted", "floor_limit": 0.8, "tuning_score": 0.9, "tuning_correct": 9,
        "tuning_cases": 10, "tuning_invalid": 0, "met_bar_in_practice": True,
        "exam": "below_bar", "held_out_score": 0.7, "held_out_correct": 7,
        "held_out_cases": 10, "held_out_invalid": 0, "held_out_over_budget": 0,
        "practice_drop": 0.2, "floor_origin": "public_dataset", "evidence": "live_billed",
        "replay_cost_usd": 0.01,
    }

    def row(**update: Any) -> AuditRow:
        return AuditRow.model_validate({**base, **update})

    assert exclusion(row()) is None and exclusion(row(exam="passed")) is None
    assert exclusion(row(floor_origin="synthetic", evidence="synthetic_fake")) == "floor"
    assert exclusion(row(floor_origin="no_manifest")) == "floor"
    assert exclusion(row(evidence="synthetic_fake", exam="error")) == "fake"
    for exam in ("over_budget", "invalid_exceeded", "error"):
        assert exclusion(row(exam=exam)) == "unscored"
    for evidence in ("local_zero_cost", "live_api_equivalent"):
        assert exclusion(row(evidence=evidence)) is None


@pytest.mark.parametrize("split", ["evidence", "rehearsal"])
def test_a_bench_build_and_its_replay_agree_on_the_committed_floors(split: str) -> None:
    """What a bench cell's Forge audit records (the practice hash of the split it built
    on) is what a replay on the committed profile floor file checks, and that file's
    ``floor_cases_sha256`` is the manifest's ``cases_sha256`` the analysis pins."""
    from pydantic import TypeAdapter

    public_tasks.clear_cache()
    data = public_tasks.load_public("receipts")
    definition = FloorDefinition(
        kind=FloorKind.HELD_OUT_TASKS, limit=0.8, dataset=f"bench-receipts-v2-{split}",
        minimum_samples=1,
    )
    built = HeldOutTasks(definition, list(data.splits[split]))
    text = (EVIDENCE / "floors" / f"{split}.json").read_text(encoding="utf-8")
    replayed = TypeAdapter(list[EvaluationCase]).validate_json(text)
    assert cases_sha256(built.tuning) == cases_sha256(HeldOutTasks(definition, replayed).tuning)
    assert cases_sha256(replayed) == data.manifests[split].cases_sha256
    manifest = EVIDENCE / "manifests" / f"{split}.json"
    assert public_tasks.manifest_origin(replayed, manifest) == "public_dataset"
