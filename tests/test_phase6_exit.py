"""Exercise the installed native interpreter entrypoint, not a scripts import."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


async def invoke(workspace: Path, *flags: str) -> tuple[int, dict[str, object]]:
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(ROOT / "scripts/phase6_exit.py"),
        "--workspace", str(workspace), *flags,
        cwd=ROOT,
        env={**os.environ, "ARCHITECT_SIGNING_KEY_FILE": str(workspace.parent / "signing.key")},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(150):
            output, error = await process.communicate()
    except TimeoutError:
        process.kill()
        await process.communicate()
        raise
    assert not error, error.decode(errors="replace")
    assert process.returncode is not None
    return process.returncode, json.loads(output)


async def test_phase6_default_proposes_without_acceptance(tmp_path: Path) -> None:
    workspace = tmp_path / "proposal"
    code, summary = await invoke(workspace)
    assert code == 0 and summary["approval_required"] is True
    assert summary["accepted"] is False
    assert summary["telemetry_rows"] == 1
    assert summary["score_source"] == "local_fixture" and summary["compliance"] == "unverified"
    assert (workspace / "proposed-spec.json").is_file()
    assert "competition" not in summary and "games" not in summary
    assert not list(workspace.rglob("report.json"))
    assert json.loads((workspace / "summary.json").read_text()) == summary


async def test_phase6_native_exit_real_games_and_installed_competition(tmp_path: Path) -> None:
    workspace = tmp_path / "approved"
    code, summary = await invoke(workspace, "--approve-fixtures")
    assert code == 0, summary
    assert summary["accepted"] is True and summary["approval_required"] is False
    games = summary["games"]
    assert games["families"] == games["public_probes"] == 3
    assert games["wall_time_s"] < 60
    competition = summary["competition"]
    assert competition["wall_time_s"] < 60
    assert competition["exact_fixture_approved"] is True
    assert competition["stages"] == [1, 2, 4]
    assert competition["score"] == 1 and competition["held_out_count"] == 3
    assert competition["installed_revision"] == competition["rollback_revision"] == 1
    assert competition["replacement_revision"] == 2
    assert competition["drift_input"] == "synthetic_metadata"
    assert competition["drift_recompile"] and competition["equal_candidate_rejected"]
    assert competition["fresh_child_licenses"] == 6
    assert competition["verified_child_rollouts"] == competition["public_probes"] == 24
    assert len(competition["children"]) == len(competition["rollback_children"]) == 3
    for run in games["runs"] + competition["children"] + competition["rollback_children"]:
        assert run["accepted"] and run["licensed_before_play"] and run["fresh_verification"]
        assert run["full_rollout"] and run["actions_used"] > 0
        assert run["stages"] == [0, 1, 2, 4] and run["tokens_in"] > 0
    assert json.loads((workspace / "summary.json").read_text()) == summary
    public = json.dumps(summary)
    for domain in ("games", "competition"):
        telemetry = workspace / domain / "telemetry/runs.jsonl"
        rows = [json.loads(line) for line in telemetry.read_text().splitlines()]
        assert len(rows) == summary[domain]["telemetry_rows"]
        assert all(row["tokens_in"] >= 0 for row in rows)
        public += telemetry.read_text()
    for forbidden in ('"frames"', '"prompt"', '"response"', '"signature"',
                      '"verification_id"', '"report"', '"rules"', '"plan"', '"hypothesis"'):
        assert forbidden not in public


async def test_phase6_refuses_existing_workspace_without_overwriting(tmp_path: Path) -> None:
    workspace = tmp_path / "existing"
    workspace.mkdir()
    sentinel = workspace / "summary.json"
    sentinel.write_text("keep existing evidence", encoding="utf-8")
    code, summary = await invoke(workspace, "--approve-fixtures")
    assert code == 1 and summary["failure"] == "new_workspace_required"
    assert sentinel.read_text() == "keep existing evidence"
    assert len(list(workspace.iterdir())) == 1
