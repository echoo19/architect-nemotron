"""The session state machine end to end on the fake provider (contract Section 13).

Transcripts are wrapped at 100 columns, so assertions that span a long path use
``unwrap``. A non-interactive script's lines are applied in order: a line read while
Stage 0 or a compile runs is held for the next prompt unless it is a busy-state command
(``cancel``, ``status``, ``spend``, ``help``, ``quit``, ``exit``). ``quit``, ``exit``
and EOF cancel running work on every console, so a script that wants a paid step to
finish puts a held line (``show``, ``report``, ...) between it and ``quit``.
``GatedConsole`` types lines interactively once the session reaches a given state.
"""

from __future__ import annotations

import asyncio
import gc
import io
import json
import os
import re
import warnings
from pathlib import Path

import pytest
from session_support import (
    BlockingFakeAdapter,
    FakeClock,
    GatedConsole,
    StubbornFakeAdapter,
    dense,
    fake_profile,
    make_session,
    make_spec,
    run_gated,
    run_script,
    run_session,
    swapping_factory,
    unwrap,
)

from architect.compiler import CompileConfig
from architect.compiler.inference import _INFERENCE_BUDGET
from architect.models import (
    AdapterError,
    ClaudeCodeAdapter,
    FakeAdapter,
    ModelRequest,
    ModelResponse,
)
from architect.profiles import Profile, ProfileError, Services, build_services
from architect.registry import Registry, RegistryError
from architect.session import ScriptedConsole, Session, main, render
from architect.session import session as session_module
from architect.session.bootstrap import (
    CLAUDE_MODELS,
    CliCandidate,
    ProbeResult,
    inference_quote,
    min_call_cost,
)
from architect.session.codes import explain
from architect.session.io import StdConsole
from architect.session.render import money
from architect.session.state import Spend, State
from architect.spec import Budget

EXPIRE = make_spec().spec_hash[:8]
INSTALL = make_spec(lifecycle="install").spec_hash[:8]
GOAL = "Return ok please"


def test_eof_at_ready_exits_zero_with_footer(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    code, transcript = run_script([], tmp_path)
    assert code == 0
    flat = unwrap(transcript)
    assert flat.startswith(f"architect 0.1.0  workspace {tmp_path / 'w'}")
    banner = dense(transcript.split("model fake via fake", 1)[0])
    assert dense(f"profile {tmp_path / 'w' / 'session-profile.json'}") in banner
    assert "model fake via fake  inference fake  compile budget $10.00 / 2m" in flat
    assert "Describe what the agent should do, or type help. Registry: 0 installed." in flat
    assert "spend  session $0.0000 | this compile - | calls 0 | wall 0m00s" in flat
    assert transcript.rstrip().endswith("bye")


def test_prose_confirmed_infers_and_declined_spends_nothing(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    code, transcript, session = run_session([GOAL, "Y", "show", "quit"], tmp_path)
    assert code == 0
    assert "infer? infer a Spec from this text with fake (reserves $" in transcript
    header = f"Spec {EXPIRE}  lifecycle expire (single use, no approval needed)  stage 0: $"
    assert header in transcript and "origin inferred" in unwrap(transcript)
    assert "  model_target   * fake" in transcript  # the ladder override is marked as operator-set
    assert "  task           ~ Return ok" in transcript
    assert f"spec_hash {make_spec().spec_hash}" in transcript
    assert session.session_spend.calls == 1

    _, declined, session = run_session([GOAL, "n", "quit"], tmp_path)
    assert "not inferred; type help for commands" in declined
    assert session.session_spend.calls == 0 and session.draft is None


def test_set_changes_hash_and_stale_token_is_refused(tmp_path: Path) -> None:
    fake_profile(tmp_path, lifecycle="install")
    edited = make_spec(lifecycle="install").model_copy(
        update={"budget": Budget(cost_usd=0.5, compute_s=60)}
    )
    new = edited.spec_hash[:8]
    lines = [
        f"describe {GOAL}",
        "set budget.cost_usd 0.5",
        f"approve {INSTALL}",
        f"approve {new}",
        "report",
        "quit",
    ]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    assert "- budget.cost_usd 0.1\n+ budget.cost_usd 0.5\nspec is now " + new in transcript
    assert f"review {new}> approve {INSTALL}" in transcript
    assert "error: stale_approval_token" in transcript
    assert f"approved {new} - compiling. Type cancel or press Ctrl-C to stop." in transcript
    assert "licensed: YES (recomputed from disk)" in transcript
    assert session.outcome is not None and session.outcome.approved_hash == edited.spec_hash


def test_install_lifecycle_end_to_end(tmp_path: Path) -> None:
    fake_profile(tmp_path, lifecycle="install")
    lines = [GOAL, "Y", f"approve {INSTALL}", "install demo", f"install demo {INSTALL}", "run {}",
             "quit"]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    assert "approved by operator  (cancel or Ctrl-C to stop)" in transcript
    assert "stage 1 selection" in transcript and "stage 4 verification" in transcript
    assert "licensed: YES (recomputed from disk)" in transcript
    assert "stages 1,2,4" in transcript and "held-out cases 3" in transcript
    assert session.outcome is not None and session.outcome.result.report is not None
    assert session.outcome.result.report.held_out_count == 3
    assert f'To install exactly this licensed artifact as "demo", type: install demo {INSTALL}' in (
        transcript
    )
    assert "installed demo revision 1 -> " in transcript
    after_install = transcript.split("installed demo revision 1 -> ", 1)[1]
    assert "Next: run {json} | list | new" in after_install
    entries = Registry(tmp_path / "w" / "registry").list()
    assert [entry.name for entry in entries] == ["demo"] and entries[0].revision == 1
    assert '\n"ok"\n' in transcript and "artifact consumed" not in transcript


def test_expire_compiles_without_approval_and_runs_once(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    lines = [f"describe {GOAL}", "approve", "compile", "run {}", "run {}", "quit"]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    assert "this Spec has lifecycle expire and needs no approval; type compile" in transcript
    assert (
        f"lifecycle expire: the compiler needs no approval; compiling exactly Spec {EXPIRE}"
        in transcript
    )
    assert "lifecycle expire, no approval needed  (cancel or Ctrl-C to stop)" in transcript
    assert "this artifact expires after this one attempt, even on failure" in transcript
    assert "artifact consumed (single use)" in transcript
    assert "error: artifact_expired" in transcript
    assert session.outcome is not None and session.outcome.approved_hash is None


def test_revise_keeps_user_fields_as_overrides(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    lines = [f"describe {GOAL}", "set budget.cost_usd 0.5", "revise be terse", "show", "quit"]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    assert session.session_spend.calls == 2
    draft = session.draft
    assert draft is not None and draft.origin == "inferred"
    assert draft.spec.budget.cost_usd == 0.5  # the fixture proposes 0.1 again; the override wins
    assert draft.spec.provenance["budget"] == "user"
    assert draft.goal == GOAL  # Section 3.3: the original prose, never the revised text
    assert transcript.count("stage 0 done in") == 2
    assert "no changes\nspec is now " in transcript


def test_unlicensed_result_shows_codes_and_refuses_install(tmp_path: Path) -> None:
    fake_profile(tmp_path, lifecycle="install", held_out_expected="bad")
    lines = [f"describe {GOAL}", f"approve {INSTALL}", "install demo", "quit"]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    assert "licensed: NO (recomputed from disk)" in transcript
    assert "score 0.00 < limit 1.00" in transcript
    assert "primary_floor_failed" in transcript
    assert "next: fix wrong expected values (floor), lower the limit (set floor.limit 0.9)" in (
        unwrap(transcript)
    )
    assert "Next: again (edit and recompile) | report | new" in transcript
    assert "error: unlicensed_artifact" in transcript
    assert session.outcome is not None and not session.outcome.licensed
    assert Registry(tmp_path / "w" / "registry").list() == ()


def test_cancel_during_compile_returns_to_review(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    adapter = BlockingFakeAdapter()
    lines = [f"describe {GOAL}", "compile", "cancel", "show", "quit"]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        code, transcript, session = run_session(
            lines, tmp_path, services_factory=swapping_factory(adapter)
        )
        gc.collect()
    assert code == 0
    assert adapter.started.is_set()
    flat = unwrap(transcript)
    assert "cancelled after $0.0000, 0.0s; unlicensed candidate directories may remain under " in (
        flat
    )
    assert f"artifacts/{make_spec().spec_hash}/ and are never licensed" in flat
    assert transcript.count(f"Spec {EXPIRE}  lifecycle expire") == 3  # entry, re-render, show
    assert session.outcome is None and session.draft is not None
    assert Registry(tmp_path / "w" / "registry").list() == ()
    assert session.services is not None
    with pytest.raises(ValueError):
        session.services.runtime.store.rows()  # closed exactly once by Session.close()
    assert not [w for w in caught if issubclass(w.category, ResourceWarning)]


async def test_ctrl_c_during_compile_cancels_only_the_compile(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    adapter = BlockingFakeAdapter()
    # An operator at a terminal: the lines are typed in their states and the terminal is
    # closed (EOF) only once the session is back in REVIEW after the Ctrl-C.
    console = GatedConsole(
        [(f"describe {GOAL}", State.READY), ("compile", State.REVIEW)], eof_at=State.REVIEW
    )
    session = Session(
        console,
        cwd=tmp_path,
        workspace=tmp_path / "w",
        clock=FakeClock(),
        poll_s=0.01,
        services_factory=swapping_factory(adapter),
    )
    console.session = session
    task = asyncio.create_task(session.run())
    await asyncio.wait_for(adapter.started.wait(), timeout=30)
    assert session.state is State.COMPILING
    task.cancel()  # what asyncio.run's SIGINT handler does on Ctrl-C
    assert await task == 0
    transcript = console.transcript()
    assert "error: cancelled" in transcript and "cancelled after" in transcript
    assert transcript.rstrip().endswith("bye")
    assert "error: interrupted" not in transcript
    assert transcript.count(f"Spec {EXPIRE}  lifecycle expire") == 2  # entry and after cancel
    assert session.state is State.EXIT and session.outcome is None


def test_inference_failure_asks_once_then_gives_up(tmp_path: Path) -> None:
    fake_profile(tmp_path, inference_text="null")
    lines = [f"describe {GOAL}", "the input is a review and the output a label", "list", "quit"]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    assert transcript.count("error: inference_failed") == 2
    assert transcript.count("clarify> ") == 1
    assert "Tell me the input you will give the agent and the output you expect" in transcript
    assert session.session_spend.calls == 2 and session.draft is None
    assert "Describe what the agent should do, or type help." in transcript.split("clarify> ")[1]


def test_empty_clarification_gives_up_after_one_call(tmp_path: Path) -> None:
    fake_profile(tmp_path, inference_text="null")
    _, transcript, session = run_session([f"describe {GOAL}", "", "quit"], tmp_path)
    assert transcript.count("error: inference_failed") == 1
    assert session.session_spend.calls == 1 and session.state is State.EXIT


def test_disallowed_verbs_print_code_and_allowed_list(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    # READY: review verbs need a draft, result verbs need a result, others are not commands.
    _, ready = run_script(["approve", "install demo", "add {}"], tmp_path)
    assert "error: no_draft" in ready and "error: no_result" in ready
    assert ready.count("error: not_a_command") == 1
    assert ready.count(
        "allowed here: bench, budget, describe, exit, floor, floors, help, info, list"
    ) == 3
    # REVIEW and RESULT (list is a read-only registry command in both, so it answers).
    _, review = run_script(
        [f"describe {GOAL}", "list", "again", "just some prose", "compile", "set a b", "quit"],
        tmp_path,
    )
    assert "error: not_a_command" in review and "error: no_result" in review
    assert "registry: no installed artifacts" in review
    assert "next: revise <text>" in review
    assert "allowed here: approve, blockers, budget, compile, diff, discard" in review
    # Phase 10 added the RESULT verbs export and rate (Section 13.2).
    assert "allowed here: again, discard, exit, export, floors, help, info, install" in review
    # FLOOR.
    _, floor = run_script(["floor f-v1", "approve", "back"], tmp_path)
    assert "error: not_a_command" in floor
    assert "allowed here: accept, add, back, draft, exit, help, paste" in floor
    # INFERRING and COMPILING: typed interactively while the work is blocked.
    blocking = BlockingFakeAdapter()
    _, inferring, _ = run_gated(
        [
            (f"describe {GOAL}", State.READY),
            ("list", State.INFERRING),
            ("cancel", State.INFERRING),
            ("quit", State.READY),
        ],
        tmp_path,
        services_factory=swapping_factory(blocking, inference=True),
    )
    assert "error: busy_inferring" in inferring
    assert "allowed here: cancel, exit, help, quit, spend, status" in inferring
    assert "error: cancelled" in inferring
    compiling = BlockingFakeAdapter()
    _, during, _ = run_gated(
        [
            (f"describe {GOAL}", State.READY),
            ("compile", State.REVIEW),
            ("set x 1", State.COMPILING),
            ("status", State.COMPILING),
            ("cancel", State.COMPILING),
            ("quit", State.REVIEW),
        ],
        tmp_path,
        services_factory=swapping_factory(compiling),
    )
    assert "error: busy_compiling" in during
    assert "state            compiling" in during
    assert "allowed here: cancel, exit, help, quit, spend, status" in during
    assert "cancelled after" in during


def test_quit_while_compiling_interactively_cancels_and_exits(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    adapter = BlockingFakeAdapter()
    code, transcript, session = run_gated(
        [(f"describe {GOAL}", State.READY), ("compile", State.REVIEW), ("quit", State.COMPILING)],
        tmp_path,
        services_factory=swapping_factory(adapter),
    )
    assert code == 0 and session.state is State.EXIT
    assert "error: cancelled" in transcript and transcript.rstrip().endswith("bye")
    assert session.outcome is None and Registry(tmp_path / "w" / "registry").list() == ()


def test_recompile_loads_incumbent_and_requires_approval(tmp_path: Path) -> None:
    fake_profile(tmp_path, lifecycle="install")
    lines = [
        f"describe {GOAL}",
        f"approve {INSTALL}",
        f"install demo {INSTALL}",
        "new",
        "recompile demo",
        "compile",
        f"approve {INSTALL}",
        f"install demo {INSTALL}",
        "quit",
    ]
    code, transcript, _ = run_session(lines, tmp_path)
    assert code == 0
    assert f"Spec {INSTALL}  lifecycle install (approval required)  origin registry" in transcript
    after = transcript.split("origin registry", 1)[1]
    assert "error: approval_token_required" in after
    assert f"approved {INSTALL} - compiling" in after
    assert "error: not_better" in after  # equal quality and cost never replaces the incumbent
    assert Registry(tmp_path / "w" / "registry").status("demo").revision == 1


def test_use_model_and_budget_compile_rewrite_the_profile(tmp_path: Path) -> None:
    profile = fake_profile(tmp_path)
    lines = ["use model fake", "budget compile 10 3600", "use model nope", "budget compile x 1",
             "quit"]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    assert "model fake selected for this session; profile rewritten" in transcript
    assert "compile budget $10.00 / 1h00m" in transcript
    assert "error: model_not_configured" in transcript
    assert "error: invalid_value_json" in transcript
    payload = json.loads(profile.read_text(encoding="utf-8"))
    assert payload["compile"]["model_ladder"] == ["fake"]
    assert payload["compile"]["compile_budget"] == {"cost_usd": 10.0, "compute_s": 3600.0}
    assert payload["inference"]["model"] == "fake"
    assert session.services is not None
    assert session.services.profile.compile.compile_budget.cost_usd == 10.0
    assert not list((tmp_path / "w").glob("*.tmp"))


def test_hand_written_profiles_are_not_editable(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    hand = tmp_path / "architect.json"
    hand.write_text(
        json.dumps(
            {
                "workspace": "w",
                "models": {"fake": {"provider": "fake", "fixture": "w/responses.json"}},
                "floors": {"tasks": {"kind": "held_out_tasks", "path": "w/cases.json"}},
            }
        ),
        encoding="utf-8",
    )
    lines = ["use model fake", "budget compile 1 1", "floor x-v1", 'add {"task": 1, "expected": 1}',
             'add {"task": 2, "expected": 2}', "accept", "quit"]
    _, transcript, _ = run_session(lines, tmp_path)
    assert transcript.count("error: profile_not_editable") == 3
    assert dense(f"profile {hand}") in dense(transcript)
    assert "x-v1" not in json.loads(hand.read_text(encoding="utf-8")).get("floors", {})


def test_script_file_transcript_matches_scripted_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_profile(tmp_path)
    lines = ["help", "help set", "floors", "list", "status", "spend", "profile", "quit"]
    script = tmp_path / "script.txt"
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["--script", str(script), "--workspace", "w", "--no-probe"]) == 0
    printed = capsys.readouterr().out
    _, transcript = run_script(lines, tmp_path)
    assert printed == transcript
    assert "Type what the agent should do (I will ask before spending), or:" in printed
    assert "  set <path> <value>" in printed
    assert "signing key " in printed
    assert "tasks  held_out_tasks  6 cases -> 3 tuning / 3 held-out" in printed


def test_session_flags_and_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "--workspace" in out and "--script" in out and "--no-probe" in out
    assert "describe <text>" in out
    assert main(["--bogus"]) == 1
    assert json.loads(capsys.readouterr().err) == {"error": "invalid_arguments"}


def test_invalid_profile_exits_one_without_contents(tmp_path: Path) -> None:
    (tmp_path / "w").mkdir()
    (tmp_path / "w" / "session-profile.json").write_text(
        '{"api_key": "PRIVATE_SENTINEL"}', encoding="utf-8"
    )
    code, transcript = run_script(["quit"], tmp_path)
    assert code == 1
    assert "error: invalid_profile" in transcript and "PRIVATE_SENTINEL" not in transcript


def test_spec_from_file_and_json_and_save(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    spec = make_spec()
    (tmp_path / "spec.json").write_text(spec.model_dump_json(), encoding="utf-8")
    lines = [
        "spec spec.json",
        "json",
        "save",
        "save copy.json",
        "revise please",
        "discard",
        "spec {",
        "load missing.json",
        f"spec {spec.model_dump_json()}",
        "diff",
        "quit",
    ]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    flat = unwrap(transcript)
    assert f"Spec {EXPIRE}  lifecycle expire (single use, no approval needed)  origin file" in flat
    assert spec.model_dump_json(indent=2) in transcript
    proposal = tmp_path / "w" / "proposals" / f"{spec.spec_hash}.json"
    assert dense(f"saved {proposal}") in dense(transcript) and proposal.is_file()
    assert (tmp_path / "copy.json").read_text(encoding="utf-8").strip() == spec.model_dump_json(
        indent=2
    )
    assert "error: no_goal" in transcript and "next: describe the task again, or use set" in (
        transcript
    )
    assert "error: invalid_spec" in transcript and "error: invalid_spec_file" in transcript
    assert flat.count("origin file") == 2 and "no changes" in transcript
    assert session.session_spend.calls == 0


def test_registry_commands_list_info_rollback_and_run_by_name(tmp_path: Path) -> None:
    fake_profile(tmp_path, lifecycle="install")
    lines = [
        f"describe {GOAL}",
        f"approve {INSTALL}",
        f"install demo {INSTALL}",
        "new",
        "list",
        "info demo",
        "rollback demo",
        "rollback demo 1",
        "run demo {}",
        "run demo not json",
        "run nobody {}",
        "quit",
    ]
    code, transcript, _ = run_session(lines, tmp_path)
    assert code == 0
    assert "name            rev  spec      ver  licensed  directory" in transcript
    assert f"{'demo':<15} {1:>3}  {INSTALL:<8}  {1:>3}  yes" in transcript
    assert f"demo  revision 1  spec {INSTALL}  version 1  licensed yes" in transcript
    assert "  evidence   held_out_tasks  score 1.00 >= limit 1.00  held-out cases 3" in transcript
    assert "error: no_previous_revision" in transcript
    assert "error: not_previous_revision" in transcript
    assert '\n"ok"\n' in transcript
    assert "error: invalid_input_json" in transcript
    assert "error: unknown_name" in transcript


def test_floor_from_ready_with_typed_cases(tmp_path: Path) -> None:
    profile = fake_profile(tmp_path)
    lines = [
        "floor demo-v1",
        "accept",
        'add {"task": {"review": "great"}, "expected": "pos"}',
        'add {"task": {"review": "awful"}, "expected": "neg"}',
        'add {"task": "dup", "expected": 1, "case_id": "demo-v1-001"}',
        "split",
        "accept",
        "floors",
        "floor demo-v1",
        "add {}",
        "back",
        "quit",
    ]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    assert "floor demo-v1  held_out_tasks  cases 0 (operator 0, file 0, model_draft 0)" in (
        transcript
    )
    assert "blocked: below minimum: 0 cases, need 2" in transcript
    assert "error: floor_too_small" in transcript
    assert "error: duplicate_case_id" in transcript
    assert "tuning (harness sees during Stage 2): demo-v1-001" in transcript
    assert "held-out (harness never sees): demo-v1-002" in transcript
    assert "ready: type accept" in transcript
    assert "floor 'demo-v1' configured: 2 cases -> 1 tuning / 1 held-out" in transcript
    assert "[SYNTHETIC" not in transcript
    dataset = tmp_path / "w" / "floors" / "demo-v1.json"
    rows = json.loads(dataset.read_text(encoding="utf-8"))
    assert [row["metadata"]["synthetic"] for row in rows] == [False, False]
    assert json.loads(profile.read_text(encoding="utf-8"))["floors"]["demo-v1"] == {
        "kind": "held_out_tasks",
        "path": "floors/demo-v1.json",
        "retakes": True,  # an exam the session wrote may be retaken by a changed agent
    }
    assert session.services is not None and "demo-v1" in session.services.profile.floors
    assert dataset.resolve() in session.services.profile.pi.denied_paths
    # A configured floor is shown, not edited.
    assert "floor 'demo-v1' is already configured" in transcript
    assert transcript.count("error: not_a_command") == 1
    assert session.state is State.EXIT


def test_floor_from_review_with_model_draft_and_review(tmp_path: Path) -> None:
    spec = make_spec(dataset="new-v1")
    fake_profile(tmp_path)
    (tmp_path / "new.json").write_text(spec.model_dump_json(), encoding="utf-8")
    drafted = json.dumps([{"task": {"review": "r1"}, "expected": "ok"},
                          {"task": {"review": "r2"}, "expected": "ok"}])
    adapter = FakeAdapter(responses=[
        ModelResponse(text=drafted, tokens_in=8, tokens_out=12, cost_usd=0.0002, model="fake")
    ])
    lines = [
        "spec new.json",
        "floor",
        "draft 2",
        "accept",
        "review new-v1-s001 ok",
        'review new-v1-s002 expected "fixed"',
        "review nope ok",
        "accept",
        "blockers",
        "quit",
    ]
    code, transcript, session = run_session(
        lines, tmp_path, services_factory=swapping_factory(adapter)
    )
    assert code == 0
    assert "! floor_not_configured dataset 'new-v1' is not in the profile" in transcript
    assert "Drafted cases were written by the same model that will be tuned on them." in transcript
    assert "drafted 2 cases (SYNTHETIC, unreviewed): review each with 'review <id> ok'" in (
        transcript
    )
    # The screen shows what is to be reviewed: id, flag, task and the drafted label.
    assert '  new-v1-s001  [draft ?]  task: {"review":"r1"}\n    expected: "ok"\n' in transcript
    assert "Next: review new-v1-s001 ok|expected <json>|drop | accept (after review) | back\n" in (
        transcript
    )
    assert "error: unreviewed_cases" in transcript
    assert "blocked: 2 model-drafted cases are unreviewed" in transcript
    # Each review prints one decision line and the next step, never the whole screen.
    assert (
        'reviewed new-v1-s001 ok  task: {"review":"r1"}\nunreviewed: 1\n'
        "next: review new-v1-s002 ok|expected <json>|drop\n"
    ) in transcript
    assert 'reviewed new-v1-s002 expected -> "fixed"\nunreviewed: 0\nready: type accept\n' in (
        transcript
    )
    assert "[draft fixed]" not in transcript
    assert transcript.count("floor new-v1  held_out_tasks") == 2  # entry and after draft
    assert "error: unknown_case_id" in transcript
    assert (
        "floor 'new-v1' configured: 2 cases -> 1 tuning / 1 held-out [SYNTHETIC: 2 of 2 cases "
        "model-drafted by fake, accepted by operator; not production evidence]"
    ) in unwrap(transcript)
    tail = transcript.split("configured: 2 cases", 1)[1]
    assert "floor_not_configured" not in tail and "no blockers" in tail
    assert f"review {spec.spec_hash[:8]}> blockers" in tail
    assert session.session_spend.calls == 1
    rows = json.loads((tmp_path / "w" / "floors" / "new-v1.json").read_text(encoding="utf-8"))
    assert rows[1]["expected"] == "fixed" and rows[1]["metadata"]["corrected"] is True
    assert all(row["metadata"]["drafted_by_model"] == "fake" for row in rows)


def test_floor_use_file_paste_and_back_confirmation(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    (tmp_path / "in.json").write_text(
        json.dumps([{"case_id": "f1", "task": "a", "expected": 1}]), encoding="utf-8"
    )
    lines = [
        "floor p-v1",
        "use in.json",
        "use missing.json",
        "paste",
        '{"task": "b", "expected": 2}',
        '{"task": "c", "expected": 3}',
        ".",
        "back",
        "n",
        "show",
        "back",
        "y",
        "quit",
    ]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    assert "loaded 1 cases from in.json" in transcript
    assert "error: invalid_floor_file" in transcript
    assert "added 2 cases" in transcript
    assert "cases 3 (operator 2, file 1, model_draft 0)" in transcript
    assert transcript.count("confirm> discard 3 unsaved cases? [y/N] ") == 2
    assert session.floor is None and session.state is State.EXIT
    assert not (tmp_path / "w" / "floors").exists()


def test_floor_unsupported_kinds_are_refused(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    _, transcript, _ = run_session(
        ["floor j-v1 llm_judge", "floor e-v1 human_edit_rate", "floor g-v1 nonsense"], tmp_path
    )
    assert transcript.count("error: floor_kind_unsupported_in_session") == 3
    assert "human_edit_rate needs 60 real edit-log rows and llm_judge needs calibrations" in (
        unwrap(transcript)
    )


def test_setup_without_cli_prints_exact_text_and_exits_one(tmp_path: Path) -> None:
    code, transcript, session = run_session([], tmp_path, detect=lambda: ())
    assert code == 1
    assert (
        "No profile and no logged-in CLI found. Log in with 'claude' or 'codex' once, or write "
        "architect.json (see docs/usage.md), then run 'architect' again."
    ) in unwrap(transcript)
    assert "error: no_provider" in transcript
    assert not (tmp_path / "w" / "session-profile.json").exists()
    assert session.services is None


def _claude() -> tuple[CliCandidate, ...]:
    return (
        CliCandidate(provider="claude-code", executable="/opt/bin/claude", models=CLAUDE_MODELS),
    )


def test_setup_one_cli_writes_profile_after_probe(tmp_path: Path) -> None:
    seen: list[str] = []

    async def prober(adapter: object, runtime: object) -> ProbeResult:
        seen.append(getattr(adapter, "model", "?"))
        return ProbeResult("ok", Spend(calls=1, tokens_in=8, tokens_out=1, cost_usd=0.01, wall_s=2))

    lines = ["", "status", "quit"]
    code, transcript, session = run_session(lines, tmp_path, detect=_claude, prober=prober)
    assert code == 0
    flat = unwrap(transcript)
    assert dense(f"architect 0.1.0  workspace {tmp_path / 'w'} (new)  profile none") in dense(
        transcript
    )
    found = "looking for logged-in CLIs on PATH: claude found (/opt/bin/claude), codex not found"
    assert found in flat
    assert "  1) claude-haiku-4-5-20251001  claude-code  min reservation/call $" in transcript
    assert "model> " not in transcript  # one candidate, non-interactive: the default row is taken
    path = tmp_path / "w" / "session-profile.json"
    assert f"confirm> write {path}? [Y/n] " in flat
    summary = (
        "model claude-haiku-4-5-20251001 via claude-code (claude)\n"
        "inference model claude-haiku-4-5-20251001\n"
        "compile budget $5.00 / 30m  candidates 2  rewriting off\n"
        f"full profile will be written to {path}\n"
        "  CLI models take 15-30 s per call; 2 candidates x 12 tuning cases + 12 held-out is "
        "about 36 calls\n"
        f"confirm> write {path}? [Y/n] "
    )
    assert dense(summary) in dense(transcript)  # the four-line summary, then the prompt
    assert '"model_ladder"' not in transcript and '"workspace"' not in transcript
    assert "checking claude-code login with one tiny metered call (about $" in transcript
    assert "claude: logged in (2.0s, $0.0100)" in transcript
    written = f"profile written {path} (edit it to add models, floors or budgets)"
    assert dense(written) in dense(transcript)
    assert "Describe what the agent should do. I will propose a Spec; nothing is compiled" in (
        transcript
    )
    assert seen == ["claude-haiku-4-5-20251001"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["workspace"] == "." and payload["compile"]["model_ladder"] == [
        "claude-haiku-4-5-20251001"
    ]
    assert session.session_spend.calls == 1 and session.session_spend.cost_usd == 0.01
    assert "state            ready" in transcript and session.state is State.EXIT


@pytest.mark.parametrize(
    ("status", "code", "line"),
    [
        ("not_logged_in", "provider_not_logged_in", "claude: not logged in - run 'claude' once"),
        ("unavailable", "provider_unavailable", "claude: unavailable"),
        ("error", "provider_failed", "error: provider_failed"),
    ],
)
def test_setup_probe_failures_write_nothing(
    tmp_path: Path, status: str, code: str, line: str
) -> None:
    async def prober(adapter: object, runtime: object) -> ProbeResult:
        return ProbeResult(status, Spend(calls=1))  # type: ignore[arg-type]

    exit_code, transcript, _ = run_session([""], tmp_path, detect=_claude, prober=prober)
    assert exit_code == 1
    assert line in transcript and f"error: {code}" in transcript
    assert not (tmp_path / "w" / "session-profile.json").exists()
    assert "spend  session $" in transcript and transcript.rstrip().endswith("bye")


def test_setup_quit_and_no_probe(tmp_path: Path) -> None:
    code, transcript, _ = run_session(["2", "n", "q"], tmp_path, detect=_claude, interactive=True)
    assert code == 0
    assert transcript.count("Choose a model for this session:") == 2
    assert "note: claude-sonnet-5 reserves $" in transcript
    note = "Specs will be inferred with claude-haiku-4-5-20251001 and compiled for claude-sonnet-5"
    assert note in unwrap(transcript)
    assert "model claude-sonnet-5 via claude-code (claude)\n" in transcript
    assert "inference model claude-haiku-4-5-20251001\n" in transcript
    assert not (tmp_path / "w" / "session-profile.json").exists()

    async def prober(adapter: object, runtime: object) -> ProbeResult:
        raise AssertionError("--no-probe must skip the probe")

    code, transcript, _ = run_session(
        ["x", "1", "y", "quit"], tmp_path, detect=_claude, prober=prober, probe=False,
        interactive=True,
    )
    assert code == 0 and "error: model_not_configured" in transcript
    assert "checking" not in transcript
    assert (tmp_path / "w" / "session-profile.json").is_file()


def test_status_spend_help_and_profile_screens(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    lines = [f"describe {GOAL}", "show", "status", "spend", "help", "help approve", "profile",
             "budget", "quit"]
    code, transcript, _ = run_session(lines, tmp_path)
    assert code == 0
    assert f"draft            {EXPIRE}  origin inferred  edits 0" in transcript
    assert "Check the Spec; nothing is compiled until you type the token shown:" in transcript
    assert "  approve [<hash8>]" in transcript
    assert "model fake  provider fake  executable -  min reservation $0.0000  can infer yes" in (
        transcript
    )
    assert "budget for fake" in transcript
    assert "note: measured live cost is typically about 1% of the reservation" in transcript


def test_status_during_compile_shows_progress(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    lines = [f"describe {GOAL}", "compile", "status", "report", "quit"]
    code, transcript, _ = run_session(lines, tmp_path)
    assert code == 0
    # status is a busy-state command, so a script applies it while the compile runs;
    # report is held, so the compile finishes before quit is read.
    assert "state            compiling" in transcript
    assert "compile finished in" in transcript and "licensed: YES" in transcript


def test_fake_clock_and_console_doubles_behave() -> None:
    clock = FakeClock(5.0)
    clock.advance(2.5)
    assert clock() == 7.5
    console = ScriptedConsole(["a"])
    assert asyncio.run(console.read("p> ")) == "a" and asyncio.run(console.read("p> ")) is None
    assert console.prompts == ["p> ", "p> "]
    assert isinstance(Session, type)


# ------------------------------------------------------------- reviewer regressions


def test_budget_compile_is_a_ready_only_verb(tmp_path: Path) -> None:
    """Contract 5.3: in REVIEW ``budget`` shows the screen; the wallet is rewritten from READY."""
    profile = fake_profile(tmp_path)
    lines = [f"describe {GOAL}", "budget compile 7 3600", "budget", "quit"]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    assert "error: not_a_command" in transcript
    assert "$7.00" not in transcript and "1h00m" not in transcript
    assert "budget for fake" in transcript  # the plain form still renders budget_screen
    payload = json.loads(profile.read_text(encoding="utf-8"))
    assert "compile_budget" not in payload["compile"]
    assert session.services is not None
    default = CompileConfig().compile_budget
    assert session.services.profile.compile.compile_budget == default
    assert session.state is State.EXIT


@pytest.mark.parametrize(
    ("held", "expected"),
    [
        (f"approve {INSTALL}", "error: no_draft"),
        ("list", "registry: no installed artifacts"),
    ],
)
def test_held_command_line_is_not_a_clarification(
    tmp_path: Path, held: str, expected: str
) -> None:
    """A command a script typed ahead is never sent to the model as a clarification."""
    fake_profile(tmp_path, inference_text="null", lifecycle="install")
    code, transcript, session = run_session([f"describe {GOAL}", held, "quit"], tmp_path)
    assert code == 0
    assert "clarify> " not in transcript
    assert transcript.count("error: inference_failed") == 1
    assert session.session_spend.calls == 1
    # Read while inferring (marked held), then replayed at the prompt it was meant for.
    assert f"architect> {held}\n  held until the running work finishes\n" in transcript
    assert transcript.count(f"architect> {held}\n") == 2
    assert expected in transcript
    assert session.draft is None and session.state is State.EXIT


def test_setup_note_quotes_the_inference_reservation(tmp_path: Path) -> None:
    """Contract Section 6: the note carries the per-inference-call reservation, not the min."""
    _, transcript, _ = run_session(["2", "q"], tmp_path, detect=_claude, interactive=True)
    adapter = ClaudeCodeAdapter("claude-sonnet-5", executable="/opt/bin/claude")
    quote = inference_quote(adapter)
    assert quote > _INFERENCE_BUDGET.cost_usd  # otherwise no note would be printed
    flat = unwrap(transcript)
    assert f"note: claude-sonnet-5 reserves {money(quote)} per inference call" in flat
    minimum = money(min_call_cost(adapter))
    if minimum != money(quote):
        assert f"reserves {minimum} per inference call" not in flat


def test_compile_finished_line_carries_the_clock(tmp_path: Path) -> None:
    """Contract 8.2: the end-of-compile line starts with the same [mm:ss] clock as the watcher."""
    fake_profile(tmp_path)
    clock = FakeClock()

    class TickingAdapter(FakeAdapter):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            clock.advance(10)
            return await super().complete(request)

    console = ScriptedConsole([f"describe {GOAL}", "compile", "report", "quit"])
    session = Session(
        console,
        cwd=tmp_path,
        workspace=tmp_path / "w",
        clock=clock,
        poll_s=0.01,
        services_factory=swapping_factory(TickingAdapter()),
    )
    assert asyncio.run(session.run()) == 0
    transcript = console.transcript()
    match = re.search(r"^(\[\d\d:\d\d\]) compile finished in (\S+): (\d+) calls,", transcript, re.M)
    assert match is not None, transcript
    calls = int(match.group(3))
    assert calls > 0
    assert match.group(1) == render.clock(calls * 10.0)
    assert match.group(2) == render.duration(calls * 10.0)


# ------------------------------------------------------ reviewer regressions, round two


@pytest.mark.parametrize("verb", ["quit", "exit"])
def test_scripted_quit_cancels_a_running_compile(tmp_path: Path, verb: str) -> None:
    """Contract 5.1/5.5: quit is a COMPILING verb on every console, so a script's quit read
    during a compile cancels it instead of letting it finish and keep spending."""
    fake_profile(tmp_path)
    adapter = BlockingFakeAdapter()  # never completes: the run ends only if it is cancelled
    code, transcript, session = run_session(
        [f"describe {GOAL}", "compile", verb], tmp_path, services_factory=swapping_factory(adapter)
    )
    assert code == 0
    assert f"compiling {EXPIRE}> {verb}\nerror: cancelled\n" in transcript
    assert transcript.count("error: cancelled") == 1
    assert "cancelled after $0.0000" in transcript and transcript.rstrip().endswith("bye")
    assert f"review {EXPIRE}> {verb}" not in transcript  # answered as it arrived, not replayed
    assert session.state is State.EXIT and session.outcome is None
    assert Registry(tmp_path / "w" / "registry").list() == ()


def test_scripted_quit_cancels_a_running_inference(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    adapter = BlockingFakeAdapter()
    code, transcript, session = run_session(
        [f"describe {GOAL}", "quit"],
        tmp_path,
        services_factory=swapping_factory(adapter, inference=True),
    )
    assert code == 0
    assert "architect> quit\nerror: cancelled\n" in transcript
    assert transcript.count("error: cancelled") == 1 and transcript.count("architect> ") == 2
    assert session.draft is None and session.state is State.EXIT
    assert transcript.rstrip().endswith("bye")


def test_eof_while_compiling_cancels_and_says_so(tmp_path: Path) -> None:
    """Contract 5.1 and Section 4: EOF cancels the compile with error: cancelled and the
    'cancelled after' line, on a closed terminal and on a script that ends mid-compile."""
    fake_profile(tmp_path)
    terminal = BlockingFakeAdapter()
    code, transcript, session = run_gated(
        [(f"describe {GOAL}", State.READY), ("compile", State.REVIEW)],
        tmp_path,
        services_factory=swapping_factory(terminal),
    )
    assert code == 0 and session.state is State.EXIT and session.outcome is None
    assert "error: cancelled" in transcript and "cancelled after $0.0000" in transcript
    assert transcript.rstrip().endswith("bye")

    piped = BlockingFakeAdapter()
    session, console = make_session(
        [f"describe {GOAL}", "compile"], tmp_path, services_factory=swapping_factory(piped)
    )
    assert asyncio.run(session.run()) == 0
    transcript = console.transcript()
    assert transcript.count("error: cancelled") == 1 and "cancelled after $0.0000" in transcript
    assert console.prompts.count(f"compiling {EXPIRE}> ") == 1  # EOF is read once, no re-arm
    assert session.state is State.EXIT and transcript.rstrip().endswith("bye")


def test_eof_while_inferring_cancels_without_spinning(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    adapter = BlockingFakeAdapter()
    session, console = make_session(
        [f"describe {GOAL}"], tmp_path, services_factory=swapping_factory(adapter, inference=True)
    )
    assert asyncio.run(session.run()) == 0
    transcript = console.transcript()
    assert transcript.count("error: cancelled") == 1
    assert console.prompts == ["architect> ", "architect> "]  # the describe line, then EOF once
    assert "inferring> " not in transcript
    assert session.draft is None and transcript.rstrip().endswith("bye")


def test_tty_eof_while_inferring_prints_cancelled(tmp_path: Path) -> None:
    """The StdConsole case: a terminal closed during Stage 0 cancels it audibly."""
    fake_profile(tmp_path)
    adapter = BlockingFakeAdapter()

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    out = io.StringIO()
    console = StdConsole(Tty(f"describe {GOAL}\n"), out)
    assert console.interactive
    session = Session(
        console,
        cwd=tmp_path,
        workspace=tmp_path / "w",
        clock=FakeClock(),
        poll_s=0.01,
        services_factory=swapping_factory(adapter, inference=True),
    )
    assert asyncio.run(session.run()) == 0
    transcript = out.getvalue()
    assert transcript.count("error: cancelled") == 1
    assert transcript.count("architect> ") == 2 and transcript.rstrip().endswith("bye")


def test_setup_exits_print_footer_and_bye(tmp_path: Path) -> None:
    """Contract Section 4 EXIT: footer then bye on every EXIT edge, SETUP's included."""
    code, transcript, _ = run_session([], tmp_path, detect=lambda: ())
    assert code == 1 and "error: no_provider" in transcript
    assert "spend  session $0.0000 | this compile - | calls 0 | wall 0m00s" in transcript
    assert transcript.rstrip().endswith("bye")
    code, transcript, _ = run_session(["q"], tmp_path, detect=_claude, interactive=True)
    assert code == 0
    assert transcript.endswith("| API-equivalent, not a bill\nbye\n")
    assert not (tmp_path / "w" / "session-profile.json").exists()


PROMPTS = re.compile(
    r"architect> |setup> |model> |confirm> .*\? \[(Y/n|y/N)\] |clarify> |infer\? .*\? \[Y/n\] |"
    r"review [0-9a-f]{8}> |floor \S+> |compiling [0-9a-f]{8}> |result [0-9a-f]{8}> "
)


def test_every_prompt_is_from_the_closed_list(tmp_path: Path) -> None:
    """Contract Section 4 fixes ten prompts: no inferring> and no paste> anywhere."""
    fake_profile(tmp_path, lifecycle="install")
    lines = [
        "floor p-v1",
        "paste",
        '{"task": "b", "expected": 2}',
        '{"task": "c", "expected": 3}',
        ".",
        "back",
        "y",
        f"describe {GOAL}",
        "revise be terse",
        "discard",
        "n",
        f"approve {INSTALL}",
        "report",
        "quit",
    ]
    session, console = make_session(lines, tmp_path)
    assert asyncio.run(session.run()) == 0
    transcript = console.transcript()
    assert "inferring> " not in transcript and "paste> " not in transcript
    assert all(PROMPTS.fullmatch(prompt) for prompt in console.prompts), console.prompts
    assert console.prompts == [
        "architect> ",
        "floor p-v1> ",
        "floor p-v1> ",  # paste continuation lines keep the FLOOR prompt
        "floor p-v1> ",
        "floor p-v1> ",
        "floor p-v1> ",
        "confirm> discard 2 unsaved cases? [y/N] ",
        "architect> ",
        "architect> ",  # read while inferring from READY: the READY prompt
        f"review {INSTALL}> ",  # read while revising: the REVIEW prompt of the Spec on screen
        f"confirm> discard Spec {INSTALL}? [y/N] ",
        f"review {INSTALL}> ",
        f"compiling {INSTALL}> ",
        f"result {INSTALL}> ",
    ]
    assert "added 2 cases" in transcript and "licensed: YES" in transcript


def test_held_lines_are_marked_where_read_and_echoed_where_applied(tmp_path: Path) -> None:
    """Contract Section 12: a transcript shows where a typed-ahead line was held and where
    it took effect, instead of the same command silently appearing at two prompts."""
    fake_profile(tmp_path)
    code, transcript, _ = run_session([f"describe {GOAL}", "compile", "run {}", "quit"], tmp_path)
    assert code == 0
    assert "architect> compile\n  held until the running work finishes\n" in transcript
    assert f"review {EXPIRE}> compile\nlifecycle expire: the compiler needs no approval" in (
        transcript
    )
    assert f"compiling {EXPIRE}> run {{}}\n" in transcript
    assert f"result {EXPIRE}> run {{}}\nthis artifact expires after this one attempt" in transcript
    assert transcript.count("held until the running work finishes") == 2
    assert transcript.count("> compile\n") == 2 and transcript.count("> run {}\n") == 2


def test_revise_always_sends_the_original_prose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract 3.3/5.3: goal is the original prose; each revise sends goal + one revision."""
    fake_profile(tmp_path)
    sent: list[str] = []
    original = session_module.infer_spec

    async def spy(text: str, *args: object, **kw: object) -> object:
        sent.append(text)
        return await original(text, *args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(session_module, "infer_spec", spy)
    lines = [f"describe {GOAL}", "revise make it b", "revise make it c", "show", "quit"]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0 and transcript.count("stage 0 done in") == 3
    assert sent == [GOAL, f"{GOAL}\n\nRevision: make it b", f"{GOAL}\n\nRevision: make it c"]
    assert session.draft is not None and session.draft.goal == GOAL
    assert session.draft.edits == 2


def test_failed_saves_leave_no_temporary_and_refuse_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A save whose target is a directory is refused before anything is written; a save
    whose replace fails removes its temporary (same for the profile rewrite)."""
    fake_profile(tmp_path)
    real_replace = os.replace

    def replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if Path(dst).name in {"copy.json", "session-profile.json"}:
            raise PermissionError("locked")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    lines = ["budget compile 1 1", f"describe {GOAL}", "save .", "save w", "save copy.json", "quit"]
    code, transcript, _ = run_session(lines, tmp_path)
    assert code == 0
    assert "error: profile_write_failed" in transcript
    assert transcript.count("error: storage_error") == 3
    assert "locked" not in transcript
    assert not (tmp_path.parent / (tmp_path.name + ".tmp")).exists()
    assert not (tmp_path / "w.tmp").exists() and not (tmp_path / "copy.json").exists()
    assert not list(tmp_path.glob("*.tmp")) and not list((tmp_path / "w").glob("*.tmp"))
    assert (tmp_path / "w" / "session-profile.json").is_file()


# ---------------------------------------------------- reviewer regressions, round three


class CtrlCConsole(GatedConsole):
    """An interactive console that answers the first COMPILING prompt with EOF and a Ctrl-C.

    That pair is what a win32 console delivered before ``io.py`` read it through
    ``ReadConsoleW`` (the aborted read looked like EOF while SIGINT cancelled the main
    task), and what the session-side guard must survive on any console: the compile is
    cancelled once and the session returns to ``review <hash8>> `` with the approval kept.
    With ``adapter`` None the Ctrl-C lands first, in the same tick as the EOF; with a
    ``StubbornFakeAdapter`` the EOF lands first and the Ctrl-C while the work is still
    cancelling.
    """

    def __init__(
        self, lines: list[tuple[str, State]], *, adapter: StubbornFakeAdapter | None
    ) -> None:
        super().__init__(lines)
        self.adapter = adapter
        self.main: asyncio.Task[int] | None = None
        self.fired = False
        self._late: asyncio.Task[None] | None = None

    async def read(self, prompt: str) -> str | None:
        session = self.session
        if session is not None and session.state is State.COMPILING and not self.fired:
            self.fired = True
            self.prompts.append(prompt)
            self.write(prompt)
            assert self.main is not None
            if self.adapter is None:
                self.main.cancel()  # Ctrl-C, delivered together with the EOF below
            else:
                self._late = asyncio.get_running_loop().create_task(self._ctrl_c_later())
            return None
        return await super().read(prompt)

    async def _ctrl_c_later(self) -> None:
        assert self.adapter is not None and self.main is not None
        await self.adapter.cancel_seen.wait()
        self.main.cancel()
        self.adapter.cancel_release.set()


@pytest.mark.parametrize("order", ["ctrl_c_first", "eof_first"])
async def test_eof_paired_with_ctrl_c_during_compile_returns_to_review(
    tmp_path: Path, order: str
) -> None:
    fake_profile(tmp_path, lifecycle="install")
    stubborn = StubbornFakeAdapter() if order == "eof_first" else None
    adapter = stubborn if stubborn is not None else BlockingFakeAdapter()
    console = CtrlCConsole(
        [
            (f"describe {GOAL}", State.READY),
            (f"approve {INSTALL}", State.REVIEW),
            ("show", State.REVIEW),
            ("quit", State.REVIEW),
        ],
        adapter=stubborn,
    )
    session = Session(
        console,
        cwd=tmp_path,
        workspace=tmp_path / "w",
        clock=FakeClock(),
        poll_s=0.01,
        services_factory=swapping_factory(adapter),
    )
    console.session = session
    console.main = asyncio.create_task(session.run())
    assert await console.main == 0
    transcript = console.transcript()
    assert console.fired and adapter.started.is_set()
    assert transcript.count("error: cancelled") == 1
    assert "cancelled after $0.0000" in transcript
    assert "error: interrupted" not in transcript
    assert console.prompts[-3:] == [
        f"compiling {INSTALL}> ",
        f"review {INSTALL}> ",
        f"review {INSTALL}> ",
    ]
    # `show` at the review prompt: the Spec is still the approved one.
    assert transcript.count(f"Spec {INSTALL}  lifecycle install") == 3
    assert transcript.count("  approved\n") == 2  # re-rendered after the cancel, and on show
    assert session.draft is not None and session.approved(session.draft.spec)
    assert session.state is State.EXIT and session.outcome is None
    assert Registry(tmp_path / "w" / "registry").list() == ()
    assert transcript.rstrip().endswith("bye")


async def test_ctrl_c_after_quit_prints_cancelled_once(tmp_path: Path) -> None:
    """A Ctrl-C while the compile a typed quit cancelled is still unwinding does not print a
    second ``error: cancelled``."""
    fake_profile(tmp_path)
    adapter = StubbornFakeAdapter()
    console = GatedConsole(
        [(f"describe {GOAL}", State.READY), ("compile", State.REVIEW), ("quit", State.COMPILING)]
    )
    session = Session(
        console,
        cwd=tmp_path,
        workspace=tmp_path / "w",
        clock=FakeClock(),
        poll_s=0.01,
        services_factory=swapping_factory(adapter),
    )
    console.session = session
    task = asyncio.create_task(session.run())
    await asyncio.wait_for(adapter.cancel_seen.wait(), timeout=30)
    task.cancel()  # Ctrl-C while the cancelled compile is still unwinding
    adapter.cancel_release.set()
    assert await task == 0
    transcript = console.transcript()
    assert transcript.count("error: cancelled") == 1
    assert "cancelled after $0.0000" in transcript
    assert "error: interrupted" not in transcript
    assert session.state is State.EXIT and transcript.rstrip().endswith("bye")


def test_loop_exception_handler_prints_only_a_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reviewer defect 3: asyncio.run's shutdown reports every task that ended with an
    exception through the loop's exception handler (a second Ctrl-C is the usual trigger).
    The session's handler prints a code; the default one would print the provider text."""
    fake_profile(tmp_path)
    session, console = make_session(["quit"], tmp_path)

    async def scenario() -> int:
        code = await session.run()

        async def stubborn() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise AdapterError("sk-secret-4242 provider text") from None

        task = asyncio.get_running_loop().create_task(stubborn())
        await asyncio.sleep(0)
        assert not task.done()  # left pending for Runner.close() -> _cancel_all_tasks()
        return code

    assert asyncio.run(scenario()) == 0
    captured = capsys.readouterr()
    transcript = console.transcript()
    assert "sk-secret" not in transcript + captured.out + captured.err
    assert "unhandled exception" not in captured.err
    tail = "bye\nerror: provider_failed\n  " + explain("provider_failed")
    assert transcript.rstrip().endswith(tail)


def test_failed_rebuild_keeps_the_previous_services(tmp_path: Path) -> None:
    """Reviewer defect 4: a rebuild that fails after a profile rewrite leaves the session on
    its previous Services instead of answering invalid_profile until quit."""
    fake_profile(tmp_path)
    builds = 0

    def factory(profile: Profile, workspace: Path) -> Services:
        nonlocal builds
        builds += 1
        if builds == 2:
            raise ProfileError("adapter_unavailable")
        return build_services(profile, workspace)

    lines = ["use model fake", "list", "budget compile 10 3600", "list", "quit"]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        code, transcript, session = run_session(lines, tmp_path, services_factory=factory)
        gc.collect()
    assert code == 0 and builds == 3
    assert transcript.count("error: adapter_unavailable") == 1
    assert "error: invalid_profile" not in transcript
    assert "model fake selected for this session" not in transcript
    assert transcript.count("registry: no installed artifacts") == 2
    assert "compile budget $10.00 / 1h00m" in transcript
    assert session.services is not None
    assert session.services.profile.compile.compile_budget.cost_usd == 10.0
    with pytest.raises(ValueError):
        session.services.runtime.store.rows()  # the last Services closed exactly once
    assert not [w for w in caught if issubclass(w.category, ResourceWarning)]


def test_registry_refusal_at_boot_is_a_fixed_code(tmp_path: Path) -> None:
    fake_profile(tmp_path)

    def refusing(profile: Profile, workspace: Path) -> Services:
        raise RegistryError("workspace_path_too_long")

    code, transcript = run_script(["quit"], tmp_path, services_factory=refusing)
    assert code == 1
    assert "error: workspace_path_too_long" in transcript
    assert "shorter --workspace" in transcript and "command_failed" not in transcript


def test_registry_refusal_during_setup_probe_writes_nothing(tmp_path: Path) -> None:
    def refusing(profile: Profile, workspace: Path) -> Services:
        raise RegistryError("workspace_path_too_long")

    candidate = CliCandidate(
        provider="claude-code",
        executable=str(tmp_path / "claude"),
        models=("claude-haiku-4-5-20251001",),
    )
    code, transcript = run_script(
        ["1", "Y", "quit"], tmp_path, services_factory=refusing, detect=lambda: (candidate,)
    )
    assert code == 1
    assert "error: workspace_path_too_long" in transcript
    assert not (tmp_path / "w" / "session-profile.json").exists()
