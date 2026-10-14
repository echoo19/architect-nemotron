"""The real entry point: ``python -m architect session`` enters the step-by-step session
(Phase 12 moved the bare ``architect`` to the conversational CLI)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from session_support import fake_profile
from test_cli import setup_profile

ROOT = Path(__file__).resolve().parents[1]


def native(
    *args: str, cwd: Path, stdin: str = "", extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(ROOT), **(extra_env or {})}
    return subprocess.run(
        [sys.executable, "-m", "architect", *args],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
    )


def test_no_arguments_reads_stdin_script_and_exits_zero(tmp_path: Path) -> None:
    profile = fake_profile(tmp_path)
    payload = json.loads(profile.read_text(encoding="utf-8"))
    payload["workspace"] = "w"  # architect.json sits in cwd; its fixtures stay under w/
    payload["models"]["fake"]["fixture"] = "w/responses.json"
    payload["inference"]["fixture"] = "w/inference.json"
    payload["floors"]["tasks"]["path"] = "w/cases.json"
    (tmp_path / "architect.json").write_text(json.dumps(payload), encoding="utf-8")
    result = native("session", cwd=tmp_path, stdin="help\nlist\nquit\n")
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("architect 0.1.0  workspace ")
    assert "profile " in result.stdout.split("\n", 2)[0] + result.stdout.split("\n", 2)[1]
    assert "Describe what the agent should do, or type help. Registry: 0 installed." in (
        result.stdout
    )
    assert "architect> help" in result.stdout and "architect> list" in result.stdout
    assert "registry: no installed artifacts" in result.stdout
    assert "spend  session $0.0000 | this compile - | calls 0" in result.stdout
    assert result.stdout.rstrip().endswith("bye")
    assert result.stderr == ""


def test_eof_without_profile_or_cli_exits_one(tmp_path: Path) -> None:
    empty = tmp_path / "bin"
    empty.mkdir()
    result = native("session", cwd=tmp_path, stdin="", extra_env={"PATH": str(empty)})
    assert result.returncode == 1
    assert "No profile and no logged-in CLI found." in result.stdout
    assert not (tmp_path / ".architect" / "session-profile.json").exists()


def test_session_help_and_bogus_flags(tmp_path: Path) -> None:
    result = native("session", "--help", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "--workspace" in result.stdout and "describe <text>" in result.stdout
    bogus = native("--bogus", cwd=tmp_path)
    assert bogus.returncode == 1
    assert json.loads(bogus.stderr) == {"error": "invalid_arguments"}
    unknown = native("session", "--bogus", cwd=tmp_path)
    assert unknown.returncode == 1
    assert json.loads(unknown.stderr) == {"error": "invalid_arguments"}


def test_existing_subcommands_are_unchanged(tmp_path: Path) -> None:
    profile, spec = setup_profile(tmp_path)
    compiled = native("compile", str(spec), "--profile", str(profile), cwd=ROOT)
    assert compiled.returncode == 0, compiled.stderr
    result = json.loads(compiled.stdout)
    assert result["accepted"] and result["stages"] == [1, 2, 4]
    listing = native("registry", "--workspace", str(tmp_path / "work"), cwd=ROOT)
    assert listing.returncode == 0 and json.loads(listing.stdout) == []
    missing = native("compile", "request", cwd=ROOT)
    assert missing.returncode == 1
    assert json.loads(missing.stderr) == {"error": "invalid_arguments"}
