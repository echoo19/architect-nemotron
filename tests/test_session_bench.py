"""The session verb ``bench`` and the BENCHING state (Phase 8 Section 11).

Offline: ``bench smoke`` runs the real frozen smoke tier on the fake provider with
oracles; paid tiers are only confirmed or declined here (a stub replaces the run
when a test needs to see that it started). The clock is a counter, so progress
lines and wall times are deterministic.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from session_support import fake_profile, run_gated, run_script, run_session

from architect.bench import CODES as BENCH_CODES
from architect.bench import read_rows
from architect.session import bench as session_bench
from architect.session.codes import CODES, explain, hint_for
from architect.session.commands import (
    GLOBAL,
    HELP,
    VOCABULARY,
    allowed,
    globals_for,
    help_for,
    parse,
)
from architect.session.render import status_screen
from architect.session.state import State


def rows_dirs(tmp_path: Path) -> list[Path]:
    root = tmp_path / "w" / "bench"
    return sorted(root.iterdir()) if root.is_dir() else []


def test_verb_and_states() -> None:
    cmd = parse("bench quick claude-haiku-4-5-20251001", State.READY)
    assert cmd is not None and cmd.verb == "bench"
    assert cmd.args == ("quick", "claude-haiku-4-5-20251001")
    assert allowed(cmd, State.READY)
    for state in (State.REVIEW, State.RESULT, State.FLOOR, State.COMPILING):
        assert not allowed(cmd, state)
    assert VOCABULARY[State.BENCHING] == frozenset({"cancel"})
    assert globals_for(State.BENCHING) == GLOBAL - {"profile"}
    assert "stop the bench; rows so far are kept" in HELP[State.BENCHING]
    text = help_for("bench")
    assert text.startswith("  bench smoke | bench quick [model,model] | bench full [model,model]")
    assert "bench compare <id> <id> | bench resume <id> | bench list" in text
    assert "(smoke, free) or on profile models" in " ".join(text.split())
    assert all(len(line) <= 100 for line in text.splitlines())


def test_codes_mirror_the_bench_table() -> None:
    assert BENCH_CODES <= CODES and "busy_benching" in CODES
    for code in (*BENCH_CODES, "busy_benching"):
        assert explain(code) != "No explanation on file for this code.", code
        assert explain(code).isascii() and len(explain(code)) < 200
    assert explain("busy_benching") == (
        "A bench is running; wait for it, type status, or type cancel."
    )
    assert hint_for("bench_over_max_usd", None) == (
        "raise --max-usd or drop models with: bench quick <model>"
    )


def test_bench_smoke_transcript(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    # "list" is not a BENCHING verb, so the scripted console holds it (and every line after
    # it) until the bench has finished; "quit" typed while benching would cancel the run.
    code, transcript, session = run_session(["bench smoke", "list", "status", "quit"], tmp_path)
    assert code == 0 and session.state is State.EXIT
    assert "architect> bench smoke\nbench smoke on 1 models: <=$" in transcript
    assert "API-equivalent bound, ~" in transcript and "Continue?" not in transcript
    assert "[00:00] bench 0/9 sentiment-24 fake: stage -, 0 calls, $0.0000" in transcript
    assert "noisy rows are the fake-only negative control" in transcript
    assert "task                   model         var     lic held-out (k/n)" in transcript
    for task in ("sentiment-24", "exam-clean-40", "digitizer-24", "arc-local-3"):
        assert f"\n{task}" in transcript
    assert "money is API-equivalent, not a bill; scores are synthetic_fake" in transcript
    assert "floors: example_fixture, synthetic_generated" in transcript
    assert "rows written to" in transcript
    assert all(len(line) <= 100 and line.isascii() for line in transcript.splitlines())
    [directory] = rows_dirs(tmp_path)
    rows = read_rows(directory / "rows.jsonl")
    assert len(rows) == 9
    # Without the optional Pillow extra the scan cell is recorded as extra_missing.
    for row in rows:
        if row.variant == "noisy":
            assert not row.licensed and "primary_floor_failed" in row.failures
        else:
            assert row.licensed or row.failures == ("extra_missing",)
    assert session.session_spend.calls == sum(row.calls for row in rows) > 0
    # Back in READY: status is the plain screen again and shows no bench line.
    after = transcript.split("architect> status", 1)[1]
    assert "state            ready" in after and "benching" not in after


def test_status_screen_shows_the_bench_line(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    _, _, session = run_session(["quit"], tmp_path)
    session.state = State.BENCHING
    session.bench_status = "benching 3/7 exam-scan-40 fake"
    screen = status_screen(session._status_view())
    assert "state            benching\nbench            benching 3/7 exam-scan-40 fake" in screen


def test_paid_tier_is_confirmed_before_any_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_profile(tmp_path)
    started: list[object] = []

    async def stub(config: object, **kwargs: object) -> object:
        started.append(config)
        raise AssertionError("declined runs never start")

    monkeypatch.setattr(session_bench, "run_bench", stub)
    code, transcript = run_script(["bench quick", "n", "quit"], tmp_path)
    assert code == 0 and not started
    assert "bench quick on 1 models: <=$" in transcript
    assert "Continue? [y/N] n" in transcript
    assert "error: bench_cancelled" in transcript
    assert rows_dirs(tmp_path) == []


def test_paid_tier_runs_after_y(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_profile(tmp_path)
    seen: list[object] = []

    async def stub(config: object, **kwargs: object) -> object:
        seen.append(config)
        raise session_bench.BenchError("bench_provider_failed")

    monkeypatch.setattr(session_bench, "run_bench", stub)
    code, transcript, session = run_session(["bench quick fake", "y", "quit"], tmp_path)
    assert code == 0 and len(seen) == 1
    config = seen[0]
    assert isinstance(config, session_bench.BenchConfig)
    assert config.provider == "profile" and config.models == ("fake",)
    assert config.tier == "quick" and config.out == tmp_path / "w" / "bench"
    assert "error: bench_provider_failed" in transcript


@pytest.mark.parametrize(
    ("line", "code"),
    [
        ("bench quick not-in-profile", "bench_model_unconfigured"),
        ("bench arc a,b", "bench_model_unconfigured"),
        ("bench smoke extra", "bench_model_unconfigured"),
        ("bench compare a b", "bench_rows_unreadable"),
    ],
)
def test_bench_failures_are_codes(tmp_path: Path, line: str, code: str) -> None:
    fake_profile(tmp_path)
    _, transcript = run_script([line, "quit"], tmp_path)
    assert f"error: {code}" in transcript
    assert "Traceback" not in transcript


def test_bench_without_a_subcommand_or_with_an_unknown_one(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    _, transcript = run_script(["bench", "bench nonsense", "quit"], tmp_path)
    assert "error: not_a_command" in transcript
    assert "next: bench smoke" in transcript and "next: help bench" in transcript


def test_cancel_returns_to_ready_and_keeps_rows(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    code, transcript, session = run_gated(
        [
            ("bench smoke", State.READY),
            ("list", State.BENCHING),
            ("cancel", State.BENCHING),
            ("status", State.READY),
            ("quit", State.READY),
        ],
        tmp_path,
    )
    assert code == 0
    assert "error: busy_benching" in transcript
    assert "allowed here: cancel, exit, help, quit, spend, status" in transcript
    assert "error: cancelled" in transcript
    assert "bench cancelled after $" in transcript and "rows so far are kept" in transcript
    assert "state            ready" in transcript.split("rows so far are kept", 1)[1]
    [directory] = rows_dirs(tmp_path)
    rows = read_rows(directory / "rows.jsonl")
    assert rows and rows[-1].failures[-1] == "cancelled" and not rows[-1].licensed
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    assert summary["cancelled"] is True
    assert "rows written to" in transcript


def test_compare_and_list_are_ready_reads(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    _, _, _ = run_session(["bench smoke", "list", "quit"], tmp_path)
    _, _, _ = run_session(["bench smoke", "list", "quit"], tmp_path)
    first, second = rows_dirs(tmp_path)
    code, transcript, session = run_session(
        [f"bench compare {first.name} {second.name}", "bench list", "quit"], tmp_path
    )
    assert code == 0 and session.state is State.EXIT
    assert "held-out base->cand [wilson]" in transcript and "overall: identical" in transcript
    assert "paired on tuning cases (selection-biased)" in transcript
    assert first.name in transcript.split("architect> bench list", 1)[1]
    assert "bound quick on fake: <=$" in transcript
    assert all(len(line) <= 100 for line in transcript.splitlines())


def test_rows_and_summary_are_content_free(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    _, transcript, _ = run_session(["bench smoke", "list", "quit"], tmp_path)
    [directory] = rows_dirs(tmp_path)
    for name in ("rows.jsonl", "summary.json"):
        text = (directory / name).read_text(encoding="utf-8")
        for forbidden in ("base64", "$image", "exam-clean-7-", "digitizer-7-", "review", "Q1"):
            assert forbidden not in text, (name, forbidden)
    assert "base64" not in transcript and "exam-clean-7-" not in transcript
    # The session's own telemetry journal gained nothing from the bench.
    journal = tmp_path / "w" / "telemetry"
    assert not journal.exists() or not any(
        "bench" in path.name for path in journal.rglob("*")
    )


def test_status_while_benching_names_the_sessions_own_signing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bench cell points ARCHITECT_SIGNING_KEY_FILE at its throwaway key for the whole
    cell; status (allowed in BENCHING) must keep naming the key that signs the session's
    artifacts, and the pin is dropped once the bench ends."""
    fake_profile(tmp_path)
    session_key = Path(os.environ["ARCHITECT_SIGNING_KEY_FILE"])  # set by conftest
    throwaway = tmp_path / "arch-cell" / "signing.key"
    swapped: list[str | None] = []

    def cell_holding_its_key(config: object, **kwargs: object) -> object:
        # Swapped synchronously when the run is created, as a running cell does, so the
        # status line typed next is guaranteed to see the throwaway key in the env.
        previous = os.environ.get("ARCHITECT_SIGNING_KEY_FILE")
        os.environ["ARCHITECT_SIGNING_KEY_FILE"] = str(throwaway)

        async def hold() -> object:
            try:
                await asyncio.Event().wait()
            finally:
                swapped.append(os.environ.get("ARCHITECT_SIGNING_KEY_FILE"))
                if previous is None:
                    os.environ.pop("ARCHITECT_SIGNING_KEY_FILE", None)
                else:
                    os.environ["ARCHITECT_SIGNING_KEY_FILE"] = previous
            raise AssertionError("cancelled before this")

        return hold()

    monkeypatch.setattr(session_bench, "run_bench", cell_holding_its_key)
    code, transcript, session = run_gated(
        [
            ("bench smoke", State.READY),
            ("status", State.BENCHING),
            ("cancel", State.BENCHING),
            ("status", State.READY),
            ("quit", State.READY),
        ],
        tmp_path,
    )
    assert code == 0 and swapped == [str(throwaway)]
    during = transcript.split("bench> state ", 1)[1].split("architect> state ", 1)[0]
    assert "benching 0/9" in during
    assert f"signing_key      {session_key}" in during
    assert str(throwaway) not in transcript
    after = transcript.split("architect> state ", 1)[1]
    assert f"signing_key      {session_key}" in after
    assert session.pinned_signing_key is None
    assert session._signing_key() == session_key


def test_help_cancel_names_the_work_the_current_state_would_stop(tmp_path: Path) -> None:
    """``help cancel`` typed while benching describes the bench, not an inference."""
    assert help_for("cancel", State.BENCHING) == (
        "  cancel                                   stop the bench; rows so far are kept\n"
    )
    assert "stop the inference" in help_for("cancel", State.INFERRING)
    assert "stop the compile" in help_for("cancel", State.COMPILING)
    assert help_for("CANCEL", State.BENCHING) == help_for("cancel", State.BENCHING)
    assert help_for("status", State.BENCHING) == help_for("status")
    assert help_for("nope", State.BENCHING) == "no such command; type help\n"
    fake_profile(tmp_path)
    code, transcript, _ = run_gated(
        [
            ("bench smoke", State.READY),
            ("help cancel", State.BENCHING),
            ("cancel", State.BENCHING),
            ("quit", State.READY),
        ],
        tmp_path,
    )
    assert code == 0
    shown = "bench>   cancel" + " " * 35 + "stop the bench; rows so far are kept\n"
    assert shown in transcript
    assert "stop the running inference or compile" not in transcript


def test_an_ollama_model_off_this_host_is_refused_before_the_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row labelled local must be local and $0: Ollama's cloud is refused with a code."""
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    path = fake_profile(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    root = Path(__file__).resolve().parents[1]
    data["models"]["granite4.2:8b"] = {
        "provider": "ollama",
        "base_url": "https://ollama.com",
        "prices": str(root / "examples" / "local" / "prices.json"),
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    started: list[object] = []

    async def stub(config: object, **kwargs: object) -> object:
        started.append(config)
        raise AssertionError("a refused model never starts a run")

    monkeypatch.setattr(session_bench, "run_bench", stub)
    code, transcript = run_script(["bench quick granite4.2:8b", "quit"], tmp_path)
    assert code == 0 and not started
    assert "error: bench_model_not_local" in transcript
    assert "Continue? [y/N]" not in transcript and rows_dirs(tmp_path) == []
    assert explain("bench_model_not_local").startswith("An Ollama bench model must run on this")
    assert "localhost" in hint_for("bench_model_not_local", None)
