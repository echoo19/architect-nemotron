"""BOOT, SETUP, READY and REVIEW handlers of the session (contract Sections 4, 5.2, 5.3, 6).

Every check here is on observable behaviour: the fixed code printed, the prompt shown, the
screen text, the state the session ends in, what was spent and what was written to disk.
Everything runs on the fake provider from ``session_support``; nothing touches a network,
a TTY or a live CLI.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from session_support import (
    FakeClock,
    QuotingFakeAdapter,
    dense,
    fake_profile,
    make_session,
    make_spec,
    response,
    run_session,
    spec_text,
    swapping_factory,
    unwrap,
)

from architect.compiler import CompileConfig
from architect.models import AdapterError, FakeAdapter, ModelAdapter, ModelRequest, UsageQuote
from architect.profiles import Profile, ProfileError, Services, build_services
from architect.registry import Registry
from architect.session import ScriptedConsole, Session, render
from architect.session import session as session_module
from architect.session.bootstrap import CLAUDE_MODELS, CODEX_MODELS, CliCandidate, ProbeResult
from architect.session.edit import set_field
from architect.session.state import Spend, State
from architect.spec import Budget, FloorDefinition, FloorKind, Spec

EXPIRE = make_spec().spec_hash[:8]
INSTALL = make_spec(lifecycle="install").spec_hash[:8]
INSTALL_FULL = make_spec(lifecycle="install").spec_hash
GOAL = "Return ok please"
HAIKU = "claude-haiku-4-5-20251001"


def _claude() -> tuple[CliCandidate, ...]:
    return (
        CliCandidate(provider="claude-code", executable="/opt/bin/claude", models=CLAUDE_MODELS),
    )


def _codex() -> tuple[CliCandidate, ...]:
    return (CliCandidate(provider="codex", executable="/opt/bin/codex", models=CODEX_MODELS),)


def _both() -> tuple[CliCandidate, ...]:
    return (*_claude(), *_codex())


def _edited_hash8() -> str:
    """The hash of the fixture Spec after ``set budget.cost_usd 0.5``."""
    budget = make_spec().budget.model_copy(update={"cost_usd": 0.5})
    return make_spec().model_copy(update={"budget": budget}).spec_hash[:8]


def _edit_json(path: Path, mutate: Callable[[dict[str, object]], None]) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


class CtrlCConsole(ScriptedConsole):
    """An interactive console that presses Ctrl-C at the first prompt after its lines."""

    def __init__(self, lines: list[str]) -> None:
        super().__init__(lines, interactive=True)
        self.main: asyncio.Task[int] | None = None
        self.interrupted_at: str | None = None

    async def read(self, prompt: str) -> str | None:
        if self._index >= len(self._lines):
            self.prompts.append(prompt)
            self.write(prompt)
            self.interrupted_at = prompt
            assert self.main is not None
            self.main.cancel()  # what asyncio.run's SIGINT handler does
            await asyncio.Event().wait()
        return await super().read(prompt)


async def _run_with_ctrl_c(
    console: CtrlCConsole, tmp_path: Path, **kw: object
) -> tuple[int, Session]:
    session = Session(
        console,
        cwd=tmp_path,
        workspace=tmp_path / "w",
        clock=FakeClock(),
        poll_s=0.01,
        **kw,  # type: ignore[arg-type]
    )
    console.main = asyncio.create_task(session.run())
    return await console.main, session


# ------------------------------------------------------------------------- BOOT / prompts


def test_blank_lines_are_ignored_at_the_prompt(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    code, transcript, session = run_session(["", "   ", "quit"], tmp_path)
    assert code == 0
    assert "error:" not in transcript
    assert "architect> \narchitect>    \narchitect> quit\n" in transcript
    assert session.session_spend.calls == 0 and session.state is State.EXIT


async def test_ctrl_c_at_the_ready_prompt_prints_interrupted_and_exits_130(
    tmp_path: Path,
) -> None:
    fake_profile(tmp_path)
    console = CtrlCConsole(["list"])
    code, session = await _run_with_ctrl_c(console, tmp_path)
    assert code == 130
    transcript = console.transcript()
    assert console.interrupted_at == "architect> "
    assert "registry: no installed artifacts" in transcript
    assert "error: interrupted\n  Interrupted at the prompt.\n" in transcript
    assert "spend  session $0.0000 | this compile - | calls 0" in transcript
    assert transcript.rstrip().endswith("bye")
    assert session.state is State.EXIT
    assert session.services is not None
    with pytest.raises(ValueError):
        session.services.runtime.store.rows()  # closed by Session.close()


async def test_ctrl_c_at_a_setup_prompt_exits_130_and_writes_nothing(tmp_path: Path) -> None:
    console = CtrlCConsole([])
    code, session = await _run_with_ctrl_c(console, tmp_path, detect=_claude)
    assert code == 130
    assert console.interrupted_at == "model> "
    assert "error:" not in console.transcript()
    assert not (tmp_path / "w" / "session-profile.json").exists()
    assert session.services is None and session.state is State.SETUP


def test_keyboard_interrupt_from_the_console_exits_130(tmp_path: Path) -> None:
    fake_profile(tmp_path)

    class InterruptingConsole(ScriptedConsole):
        async def read(self, prompt: str) -> str | None:
            self.write(prompt)
            raise KeyboardInterrupt

    console = InterruptingConsole([])
    session = Session(console, cwd=tmp_path, workspace=tmp_path / "w", clock=FakeClock())
    assert asyncio.run(session.run()) == 130
    assert console.transcript().endswith("architect> ")
    assert session.services is not None
    with pytest.raises(ValueError):
        session.services.runtime.store.rows()  # services closed exactly once


def test_handle_before_boot_answers_with_fixed_codes(tmp_path: Path) -> None:
    """``Session.handle`` before ``run``: no services, BOOT vocabulary, status still renders."""
    session, console = make_session([], tmp_path)
    asyncio.run(session.handle("profile"))
    asyncio.run(session.handle("list"))
    asyncio.run(session.handle("status"))
    transcript = console.transcript()
    assert "error: invalid_profile" in transcript
    assert "error: not_a_command" in transcript
    assert "allowed here: exit, help, profile, quit, spend, status" in transcript
    assert "state            boot" in transcript
    assert "model            none" in transcript and "inference_model  none" in transcript
    assert "floors           none" in transcript and "profile_path     none" in transcript
    assert session.state is State.BOOT


# ------------------------------------------------------------------------------- SETUP


def test_setup_menu_accepts_a_model_name_and_an_empty_default(tmp_path: Path) -> None:
    lines = ["claude-sonnet-5", "n", "", "y", "quit"]
    code, transcript, session = run_session(
        lines, tmp_path, detect=_both, probe=False, interactive=True
    )
    assert code == 0
    flat = unwrap(transcript)
    found = (
        "looking for logged-in CLIs on PATH: claude found (/opt/bin/claude), "
        "codex found (/opt/bin/codex)"
    )
    assert found in flat
    assert transcript.count("Choose a model for this session:") == 2
    assert f"  1) {HAIKU}  claude-code  min reservation/call $" in transcript
    assert "  2) claude-sonnet-5" in transcript and "  5) gpt-5.6-terra" in transcript
    assert f"can infer Specs: no (Stage 0 will use {HAIKU})" in flat
    assert "  q) quit" in transcript
    # The first choice, by name, needs the Haiku fallback; the note says so.
    note = f"Specs will be inferred with {HAIKU} and compiled for claude-sonnet-5"
    assert note in flat
    path = tmp_path / "w" / "session-profile.json"
    assert transcript.count(f"confirm> write {path}? [Y/n] ") == 2  # n, then the menu again
    assert "checking" not in transcript
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["compile"]["model_ladder"] == [HAIKU]  # the empty answer took the default
    assert payload["inference"]["model"] == HAIKU
    assert set(payload["models"]) == {HAIKU}
    assert f"Text you type here is sent to {HAIKU}; do not paste secrets." in flat
    assert session.state is State.EXIT and session.session_spend.calls == 0


def test_setup_eof_at_model_and_confirm_prompts_writes_nothing(tmp_path: Path) -> None:
    # Two candidates and a non-interactive console: the menu is asked, and EOF leaves.
    code, transcript, session = run_session([], tmp_path, detect=_both)
    assert code == 0
    assert "model> \n" in transcript and "confirm>" not in transcript
    assert "error:" not in transcript and transcript.rstrip().endswith("bye")
    assert not (tmp_path / "w" / "session-profile.json").exists()
    assert session.services is None
    # EOF at the confirm prompt after a choice.
    code, transcript, session = run_session(["1"], tmp_path, detect=_claude, interactive=True)
    assert code == 0
    assert "confirm> write " in transcript and "profile written" not in transcript
    assert "error:" not in transcript and transcript.rstrip().endswith("bye")
    assert not (tmp_path / "w" / "session-profile.json").exists()
    assert session.services is None


def test_setup_codex_only_has_no_inference_and_describe_is_unavailable(tmp_path: Path) -> None:
    # The single candidate is taken without a menu prompt; the empty line confirms the write.
    lines = ["", "status", f"describe {GOAL}", GOAL, "quit"]
    code, transcript, session = run_session(lines, tmp_path, detect=_codex, probe=False)
    assert code == 0
    flat = unwrap(transcript)
    assert "looking for logged-in CLIs on PATH: claude not found, codex found (/opt/bin/codex)" in (
        flat
    )
    assert "  1) gpt-5.6-terra  codex  min reservation/call $" in transcript
    assert "can infer Specs: no (describe unavailable; use spec <file>)   (default)" in flat
    assert "model> " not in transcript  # one candidate, non-interactive: default row taken
    assert "note:" not in transcript  # no claude-code CLI, so no Haiku fallback is promised
    path = tmp_path / "w" / "session-profile.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert "inference" not in payload
    assert payload["compile"]["model_ladder"] == ["gpt-5.6-terra"]
    assert "Text you type here is sent to gpt-5.6-terra; do not paste secrets." in flat
    assert "inference_model  none" in transcript
    assert transcript.count("error: no_inference_model") == 2
    assert transcript.count("next: spec <file|{json}>") == 2
    assert "infer? " not in transcript and "inferring Spec" not in transcript
    assert session.session_spend.calls == 0 and session.draft is None


def test_setup_over_budget_probe_skips_and_still_writes(tmp_path: Path) -> None:
    async def prober(adapter: object, runtime: object) -> ProbeResult:
        return ProbeResult("over_budget", Spend(calls=1, cost_usd=0.0))

    code, transcript, session = run_session(["quit"], tmp_path, detect=_claude, prober=prober)
    assert code == 0
    assert "checking claude-code login with one tiny metered call (about $" in transcript
    assert "claude: probe quote exceeds the $1.00 control budget; skipped" in transcript
    assert "claude: logged in" not in transcript and "error:" not in transcript
    path = tmp_path / "w" / "session-profile.json"
    assert dense(f"profile written {path}") in dense(transcript)
    assert path.is_file()
    assert session.session_spend.calls == 1  # the refused probe still counts as a call
    assert "Describe what the agent should do. I will propose a Spec" in transcript


def test_setup_unfit_probe_quote_skips_the_probe_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_rows = session_module.model_rows

    def unfit_rows(candidates: object) -> tuple[object, ...]:
        return tuple(dataclasses.replace(row, probe_fits=False) for row in real_rows(candidates))  # type: ignore[arg-type]

    monkeypatch.setattr(session_module, "model_rows", unfit_rows)

    async def prober(adapter: object, runtime: object) -> ProbeResult:
        raise AssertionError("an unfit probe quote must never reach the provider")

    code, transcript, session = run_session(["quit"], tmp_path, detect=_claude, prober=prober)
    assert code == 0
    assert "checking claude-code login with one tiny metered call" in transcript
    assert "claude: probe quote exceeds the $1.00 control budget; skipped" in transcript
    assert (tmp_path / "w" / "session-profile.json").is_file()
    assert session.session_spend.calls == 0 and session.state is State.EXIT


def test_setup_rebuild_failure_after_writing_exits_one(tmp_path: Path) -> None:
    def refusing(profile: Profile, workspace: Path) -> Services:
        raise ProfileError("adapter_unavailable")

    code, transcript, session = run_session(
        ["quit"], tmp_path, detect=_claude, probe=False, services_factory=refusing
    )
    assert code == 1
    path = tmp_path / "w" / "session-profile.json"
    assert dense(f"profile written {path}") in dense(transcript)
    assert path.is_file()  # written before the rebuild; the operator can fix and restart
    assert "error: adapter_unavailable" in transcript
    assert "Describe what the agent should do" not in transcript
    assert "spend  session $0.0000" in transcript and transcript.rstrip().endswith("bye")
    assert session.services is None


# ------------------------------------------------------------------------------- READY


def test_prose_confirmation_takes_empty_as_yes_and_eof_as_no(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    code, transcript, session = run_session([GOAL, "", "show", "quit"], tmp_path)
    assert code == 0
    prompt = "infer? infer a Spec from this text with fake (reserves $0.0000, typically a few "
    assert prompt + "cents)? [Y/n] \n" in transcript
    assert "inferring Spec with fake (one call, reserved $0.0000) ..." in transcript
    assert session.session_spend.calls == 1 and session.draft is not None
    assert session.draft.goal == GOAL

    code, transcript, session = run_session([GOAL], tmp_path)
    assert code == 0
    assert "not inferred; type help for commands" in transcript
    assert "inferring Spec" not in transcript
    assert session.session_spend.calls == 0 and session.draft is None


def test_inference_quote_failure_is_shown_as_zero(tmp_path: Path) -> None:
    fake_profile(tmp_path)

    class UnquotableAdapter(FakeAdapter):
        def quote(self, request: ModelRequest) -> UsageQuote:
            raise AdapterError("sk-secret quote failure")

    code, transcript, session = run_session(
        [GOAL, "n", "quit"],
        tmp_path,
        services_factory=swapping_factory(UnquotableAdapter(), inference=True),
    )
    assert code == 0
    assert "(reserves $0.0000, typically a few cents)? [Y/n] " in transcript
    assert "sk-secret" not in transcript
    assert session.session_spend.calls == 0


def test_ready_argument_errors_print_the_syntax_hint(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    lines = [
        "describe",
        "info",
        "run",
        "recompile",
        "floor",
        "use fake",
        "use model",
        "budget",
        "budget compile 1",
        "rollback",
        "spec",
        "load",
        "quit",
    ]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    assert transcript.count("error: not_a_command") == 10
    for hint in (
        "next: describe <text>",
        "next: info <name>",
        "next: run <name|dir> {json}",
        "next: recompile <name>",
        "next: floor <dataset> [kind]",
        "next: rollback <name> [rev]",
    ):
        assert transcript.count(hint) == 1, hint
    assert transcript.count("next: use model <id>") == 2
    assert transcript.count("next: budget compile <usd> <seconds>") == 2
    assert transcript.count("error: invalid_spec_file") == 2
    assert "allowed here:" not in transcript  # a known verb with bad arguments is not disallowed
    assert session.session_spend.calls == 0 and session.draft is None
    assert session.state is State.EXIT


def test_budget_compile_rejects_non_positive_and_non_finite_values(tmp_path: Path) -> None:
    profile = fake_profile(tmp_path)
    lines = [
        "budget compile 0 10",
        "budget compile 5 -1",
        "budget compile inf 10",
        "budget compile 5 nan",
        "quit",
    ]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    assert transcript.count("error: invalid_value_json") == 4
    assert "compile budget $" not in transcript.split("Describe what", 1)[1]
    assert "compile_budget" not in json.loads(profile.read_text(encoding="utf-8"))["compile"]
    assert session.services is not None
    assert session.services.profile.compile.compile_budget == CompileConfig().compile_budget


def test_use_model_and_budget_compile_create_the_compile_section(tmp_path: Path) -> None:
    # Each command creates the missing "compile" section on its own.
    profile = fake_profile(tmp_path)
    _edit_json(profile, lambda payload: payload.pop("compile"))
    code, transcript, session = run_session(["budget compile 2 120", "quit"], tmp_path)
    assert code == 0
    assert "compile budget $2.00 / 2m" in transcript
    payload = json.loads(profile.read_text(encoding="utf-8"))
    assert payload["compile"] == {"compile_budget": {"cost_usd": 2.0, "compute_s": 120.0}}
    assert session.services is not None
    assert session.services.profile.compile.compile_budget.cost_usd == 2.0

    _edit_json(profile, lambda payload: payload.pop("compile"))
    code, transcript, session = run_session(["use model fake", "quit"], tmp_path)
    assert code == 0
    assert "model fake selected for this session; profile rewritten" in transcript
    payload = json.loads(profile.read_text(encoding="utf-8"))
    assert payload["compile"] == {"model_ladder": ["fake"]}
    assert payload["inference"]["model"] == "fake" and payload["inference"]["provider"] == "fake"
    assert session.services is not None
    assert session.services.profile.compile.model_ladder == ("fake",)


def test_use_model_without_inference_capacity_drops_inference_and_says_so(
    tmp_path: Path,
) -> None:
    profile = fake_profile(tmp_path)
    lines = ["use model fake", f"describe {GOAL}", GOAL, "status", "quit"]
    code, transcript, session = run_session(
        lines, tmp_path, services_factory=swapping_factory(QuotingFakeAdapter(2.0))
    )
    assert code == 0
    note = (
        "note: fake cannot infer Specs under the fixed $1.00 Stage-0 budget; describe is "
        "unavailable until a claude-code model is present"
    )
    assert note in unwrap(transcript)
    assert "model fake selected for this session; profile rewritten" in transcript
    assert "inference" not in json.loads(profile.read_text(encoding="utf-8"))
    assert transcript.count("error: no_inference_model") == 2
    assert "infer? " not in transcript and "inferring Spec" not in transcript
    assert "inference_model  none" in transcript
    assert session.session_spend.calls == 0 and session.draft is None


def test_profile_rewrite_refuses_a_corrupted_profile_file(tmp_path: Path) -> None:
    profile = fake_profile(tmp_path)
    lines = ["use model fake", "budget compile 1 1", "list", "quit"]
    session, console = make_session(lines, tmp_path)

    # Corrupt the file after boot has loaded it: the next rewrite must refuse, not crash.
    original_read = console.read

    async def read(prompt: str) -> str | None:
        profile.write_text("{not json", encoding="utf-8")
        return await original_read(prompt)

    console.read = read  # type: ignore[method-assign]
    assert asyncio.run(session.run()) == 0
    transcript = console.transcript()
    assert transcript.count("error: invalid_profile") == 2
    assert "profile rewritten" not in transcript and "compile budget $1.00" not in transcript
    assert "registry: no installed artifacts" in transcript  # the loaded services survive
    assert profile.read_text(encoding="utf-8") == "{not json"  # never overwritten
    assert not list((tmp_path / "w").glob("*.tmp"))


def test_initial_overrides_bind_the_only_ladder_model_but_never_a_floor(tmp_path: Path) -> None:
    profile = fake_profile(tmp_path)

    def widen(payload: dict[str, object]) -> None:
        models = payload["models"]
        floors = payload["floors"]
        assert isinstance(models, dict) and isinstance(floors, dict)
        # A second fake-provider model needs a priced id; it answers "ok" without a fixture.
        models["claude-sonnet-4-20250514"] = {"provider": "fake"}
        floors["other"] = {"kind": "held_out_tasks", "path": "cases.json"}
        payload["compile"] = {
            "model_ladder": ["fake", "claude-sonnet-4-20250514"],
            "max_candidates": 1,
        }

    _edit_json(profile, widen)
    # "show" is held while Stage 0 runs, so quit is read only once the Spec is on screen.
    code, transcript, session = run_session([f"describe {GOAL}", "show", "quit"], tmp_path)
    assert code == 0
    assert "  model_target   ~ fake" in transcript  # inferred, not marked as operator-set
    assert "  floor          ~ held_out_tasks  dataset tasks  limit >= 1.00" in transcript
    assert "configured: yes, 6 cases -> 3 tuning / 3 held-out" in transcript
    assert session.draft is not None and session.draft.user_fields == {}
    assert session.draft.spec.provenance.get("model_target") != "user"

    # Back to one ladder model and one floor: the model is bound and marked; the floor the
    # model proposed stays inferred even though it happens to name the only floor.
    fake_profile(tmp_path)
    _, transcript, session = run_session([f"describe {GOAL}", "show", "quit"], tmp_path)
    assert "  model_target   * fake" in transcript
    assert "  floor          ~ held_out_tasks  dataset tasks  limit >= 1.00" in transcript
    assert "configured: yes, 6 cases -> 3 tuning / 3 held-out" in transcript
    assert session.draft is not None
    assert session.draft.user_fields == {"model_target": "fake"}
    assert session.draft.spec.provenance["floor"] == "inferred"


def test_inferred_dataset_is_never_rebound_to_the_only_floor(tmp_path: Path) -> None:
    """A goal about tickets-v1 is not verified on the profile's only floor behind your back.

    The Spec keeps the dataset the model named, ``floor_not_configured`` blocks with
    ``floor`` and offers the configured names as a second exact command on every
    surface (spec screen, ``blockers``, the approve refusal).
    """
    proposed = make_spec(dataset="tickets-v1")
    token = proposed.spec_hash[:8]
    profile = fake_profile(tmp_path, inference_text=proposed.model_dump_json())
    lines = [
        f"describe {GOAL}",
        "blockers",
        "compile",
        "set floor.dataset tasks",
        "blockers",
        "quit",
    ]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    assert (
        "  floor          ~ held_out_tasks  dataset tickets-v1  limit >= 1.00\n"
        "                   configured: no\n"
    ) in transcript
    assert (
        "! floor_not_configured dataset 'tickets-v1' is not in the profile\n"
        "                       fix: floor\n"
        "                       or: set floor.dataset tasks\n"
        f"spec_hash {proposed.spec_hash}\n"
    ) in transcript
    assert (
        f"review {token}> blockers\n"
        "! floor_not_configured  blocking  fix: floor\n"
        "  or: set floor.dataset tasks\n"
        f"review {token}> compile\n"
        "cannot approve: floor_not_configured\n"
        "  fix: floor\n"
        "  or: set floor.dataset tasks\n"
        f"review {token}> set floor.dataset tasks\n"
        '- floor.dataset "tickets-v1"\n'
        '+ floor.dataset "tasks"\n'
        f"spec is now {EXPIRE}\n"
        f"review {EXPIRE}> blockers\n"
        "no blockers\n"
    ) in transcript
    assert "compiling " not in transcript and session.outcome is None
    # set carries the whole floor sub-model into user_fields, exactly as `set` always does.
    assert session.draft is not None and session.draft.user_fields == {
        "model_target": "fake",
        "floor": make_spec().floor.model_dump(mode="json"),
    }
    assert session.session_spend.calls == 1

    # Several configured floors are listed as a choice, still after the fix line.
    def add_floor(payload: dict[str, object]) -> None:
        floors = payload["floors"]
        assert isinstance(floors, dict)
        floors["sentiment-v1"] = {"kind": "held_out_tasks", "path": "cases.json"}

    _edit_json(profile, add_floor)
    code, transcript, _ = run_session([f"describe {GOAL}", "blockers", "quit"], tmp_path)
    assert code == 0
    assert (
        "                       fix: floor\n"
        "                       or: set floor.dataset <tasks|sentiment-v1>\n"
    ) in transcript
    assert (
        "! floor_not_configured  blocking  fix: floor\n"
        "  or: set floor.dataset <tasks|sentiment-v1>\n"
    ) in transcript


def test_inferred_budget_below_quote_is_lifted_once_on_a_session_profile(tmp_path: Path) -> None:
    """A model's $0.10 against a $0.35 reservation becomes two calls, said once, kept on revise."""
    fake_profile(tmp_path)
    lifted = make_spec().model_copy(update={"budget": Budget(cost_usd=0.7, compute_s=60)})
    token = lifted.spec_hash[:8]
    lines = [f"describe {GOAL}", "blockers", "revise be terse", "show", "quit"]
    code, transcript, session = run_session(
        lines, tmp_path, services_factory=swapping_factory(QuotingFakeAdapter(0.35))
    )
    assert code == 0
    note = (
        "budget.cost_usd set to $0.7000 so 2 calls of fake fit; "
        "change with set budget.cost_usd <usd>"
    )
    assert transcript.count(note) == 1
    # Exactly one note, directly after the Stage 0 line and before the Spec screen.
    assert re.search(
        r"^stage 0 done in [^\n]+\n" + re.escape(note) + r"\nSpec " + token, transcript, re.M
    )
    assert "  budget         * $0.70 per run, 60s" in transcript
    assert "                   min reservation fake $0.35/call -> 2 calls fit" in transcript
    assert "budget_below_quote" not in transcript
    assert f"review {token}> blockers\nno blockers\n" in transcript
    # The revise proposes $0.10 again; the lifted value is an override, so nothing changes.
    assert transcript.count("stage 0 done in") == 2
    assert f"no changes\nspec is now {token}\n" in transcript
    draft = session.draft
    assert draft is not None and draft.origin == "inferred" and draft.hash8 == token
    assert draft.spec.budget.cost_usd == 0.7 and draft.spec.provenance["budget"] == "user"
    assert draft.user_fields == {
        "model_target": "fake",
        "budget": {"cost_usd": 0.7, "compute_s": 60.0},
    }
    assert session.session_spend.calls == 2


def test_loaded_spec_below_quote_keeps_the_blocker(tmp_path: Path) -> None:
    """spec/load never lift the budget: the operator wrote that number."""
    fake_profile(tmp_path)
    path = tmp_path / "low.json"
    path.write_text(spec_text(), encoding="utf-8")
    lines = [f"spec {spec_text()}", "blockers", "discard", f"load {path}", "blockers", "quit"]
    code, transcript, session = run_session(
        lines, tmp_path, services_factory=swapping_factory(QuotingFakeAdapter(0.35))
    )
    assert code == 0
    assert "budget.cost_usd set to" not in transcript
    assert transcript.count("  budget         ~ $0.10 per run, 60s") == 2
    assert transcript.count("! budget_below_quote  blocking  fix: set budget.cost_usd 0.70") == 2
    assert session.draft is not None and session.draft.spec.budget.cost_usd == 0.1
    assert session.session_spend.calls == 0


def test_floors_and_status_with_no_or_broken_floor_files(tmp_path: Path) -> None:
    profile = fake_profile(tmp_path)
    _edit_json(profile, lambda payload: payload.pop("floors"))
    code, transcript, _ = run_session(["floors", "status", "quit"], tmp_path)
    assert code == 0
    assert "no floors configured" in transcript and "floors           none" in transcript

    fake_profile(tmp_path)
    (tmp_path / "w" / "cases.json").write_text("[{", encoding="utf-8")
    code, transcript, _ = run_session(["floors", "status", "profile", "quit"], tmp_path)
    assert code == 0
    assert "error: invalid_floor_file" in transcript
    assert "floors           none" in transcript  # status degrades to no summary, no crash
    assert "no floors configured" in transcript  # the profile screen likewise
    assert "tasks  held_out_tasks" not in transcript


def test_floor_from_ready_with_explicit_kind_and_bad_dataset_name(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    lines = ["floor x-v1 held_out_tasks", "back", "floor ../evil", "floor a b c", "quit"]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    assert "floor x-v1> back" in transcript
    assert "floor x-v1  held_out_tasks  cases 0 (operator 0, file 0, model_draft 0)" in transcript
    assert "error: invalid_floor_file" in transcript
    assert "error: floor_kind_unsupported_in_session" in transcript
    assert "architect> floor a b c" in transcript and "floor a> " not in transcript
    assert session.floor is None and session.state is State.EXIT


def test_profile_screen_marks_an_unbuildable_adapter(tmp_path: Path) -> None:
    fake_profile(tmp_path)

    def factory(profile: Profile, workspace: Path) -> Services:
        services = build_services(profile, workspace)

        def broken(model: str) -> ModelAdapter:
            raise ProfileError("adapter_unavailable")

        return dataclasses.replace(services, adapter_factory=broken)

    code, transcript, _ = run_session(["profile", "quit"], tmp_path, services_factory=factory)
    assert code == 0
    assert "model fake  provider fake  executable -  min reservation -  can infer no" in transcript
    assert "error:" not in transcript


# ------------------------------------------------------------- READY on a registry entry


def _install_demo(tmp_path: Path) -> Registry:
    fake_profile(tmp_path, lifecycle="install")
    lines = [f"describe {GOAL}", f"approve {INSTALL}", f"install demo {INSTALL}", "quit"]
    code, transcript, _ = run_session(lines, tmp_path)
    assert code == 0 and "installed demo revision 1 -> " in transcript
    return Registry(tmp_path / "w" / "registry")


def test_registry_entry_recompile_rollback_info_and_run_by_directory(tmp_path: Path) -> None:
    registry = _install_demo(tmp_path)
    first = registry.status("demo")
    lines = [
        "recompile demo",
        f"approve {INSTALL}",
        f"install demo {INSTALL}",
        "new",
        "rollback demo",
        "rollback demo x",
        "rollback demo 2",
        "rollback demo 1",
        "list",
        "info demo",
        f"run {first.directory} {{}}",
        "status",
        "quit",
    ]
    # A cheaper adapter makes the recompiled candidate rank above the incumbent.
    code, transcript, session = run_session(
        lines, tmp_path, services_factory=swapping_factory(QuotingFakeAdapter(0.00001))
    )
    assert code == 0
    assert f"Spec {INSTALL}  lifecycle install (approval required)  origin registry" in transcript
    assert "installed demo revision 2 -> " in transcript
    assert "To roll back demo from revision 2 to 1 type: rollback demo 1" in transcript
    assert "error: invalid_revision" in transcript
    assert "error: not_previous_revision" in transcript  # 2 is the current revision
    after = transcript.split("rollback demo 1\n", 1)[1]
    assert f"demo  revision 1  spec {INSTALL}  version 1  licensed yes" in after
    assert f"{'demo':<15} {1:>3}  {INSTALL:<8}  {1:>3}  yes" in after
    evidence = "  evidence   held_out_tasks  score 1.00 >= limit 1.00  held-out cases 3"
    assert after.count(evidence) == 2
    assert '\n"ok"\n' in after and "artifact consumed" not in after
    assert "last_entry       demo revision 1" in after
    assert registry.status("demo").revision == 1
    assert registry.status("demo").directory == first.directory
    assert session.state is State.EXIT


def test_review_registry_commands_are_read_only(tmp_path: Path) -> None:
    """list, info, floors and run <name> answer under review; the Draft is untouched."""
    _install_demo(tmp_path)
    lines = [
        f"spec {spec_text(lifecycle='install')}",
        "list",
        "info demo",
        "floors",
        "run demo {}",
        "run {}",
        "run nobody {}",
        "show",
        "quit",
    ]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    under_review = transcript.split(f"review {INSTALL}> list\n", 1)[1]
    assert f"{'demo':<15} {1:>3}  {INSTALL:<8}  {1:>3}  yes" in under_review
    assert f"demo  revision 1  spec {INSTALL}  version 1  licensed yes" in under_review
    assert "tasks  held_out_tasks  6 cases -> 3 tuning / 3 held-out" in unwrap(under_review)
    assert under_review.count('\n"ok"\n') == 1  # the installed artifact ran once
    assert render.failure("not_a_command", "run <name|dir> {json}") in under_review
    assert "error: unknown_name" in under_review
    assert transcript.count(f"review {INSTALL}> ") == 8  # nothing left REVIEW
    assert session.draft is not None and session.draft.hash8 == INSTALL
    assert session.draft.edits == 0 and session.session_spend.calls == 1
    assert session.state is State.EXIT


def test_run_by_directory_checks_the_license_on_disk(tmp_path: Path) -> None:
    registry = _install_demo(tmp_path)
    entry = registry.status("demo")
    tampered = tmp_path / "copy"
    shutil.copytree(entry.directory, tampered)
    report = tampered / "report.json"
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["floor_score"] = 0.5
    report.write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "empty").mkdir()
    lines = ["run copy {}", "run empty {}", "run demo {}", "quit"]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    assert transcript.count("error: unlicensed_artifact") == 2
    assert transcript.count("next: again") == 2
    assert transcript.count('\n"ok"\n') == 1  # only the registry entry ran
    assert session.session_spend.calls == 1


# ------------------------------------------------------------------------------ REVIEW


def test_review_argument_errors_and_edit_diagnostics(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    lines = [
        f"spec {spec_text()}",
        "set",
        "set nope 1",
        "set budget.cost_usd",
        "set budget.cost_usd abc",
        "unset",
        "unset a b",
        "unset task",
        "revise",
        "budget compile 1 1",
        "quit",
    ]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    assert "next: set <path> <value>" in transcript
    assert "error: unknown_field" in transcript
    assert "error: invalid_value_json" in transcript
    assert "error: invalid_edit\n  The edited Spec did not validate" in transcript
    assert "  fields: budget.cost_usd\n" in transcript
    assert transcript.count("next: unset <field>") == 2
    assert "  fields: task\n" in transcript  # a required field cannot be unset
    assert "next: revise <text>" in transcript
    assert "next: budget (the wallet is set from READY: budget compile <usd> <seconds>)" in (
        unwrap(transcript)
    )
    assert transcript.count("error: invalid_edit") == 2
    assert "spec is now" not in transcript
    assert transcript.count(f"review {EXPIRE}> ") == 10
    assert session.draft is not None and session.draft.edits == 0
    assert session.draft.spec == make_spec()


def test_unset_restores_the_default_and_prints_the_diff(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    other = make_spec().model_copy(update={"model_target": "other"})
    new = other.spec_hash[:8]
    lines = [f"spec {spec_text()}", "set model_target other", "unset model_target", "diff", "quit"]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    assert f'- model_target "fake"\n+ model_target "other"\nspec is now {new}\n' in transcript
    reverted = f'- model_target "other"\n+ model_target "fake"\nspec is now {EXPIRE}\n'
    assert f"review {new}> unset model_target\n" + reverted in transcript
    assert f"review {EXPIRE}> diff\nno changes\nspec is now {EXPIRE}\n" in transcript
    assert session.draft is not None and session.draft.edits == 2
    assert session.draft.spec.provenance["model_target"] == "user"
    assert session.draft.user_fields == {"model_target": "fake"}


def test_blockers_refuse_compile_and_approve_with_the_fix(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    missing = make_spec(dataset="nope")
    lines = [f"spec {missing.model_dump_json()}", "blockers", "compile", "quit"]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    assert "! floor_not_configured dataset 'nope' is not in the profile" in transcript
    assert "! floor_not_configured  blocking  fix: floor" in transcript
    assert "cannot approve: floor_not_configured\n  fix: floor\n" in transcript
    assert "compiling exactly Spec" not in transcript and "compiling " not in transcript
    assert session.outcome is None and session.session_spend.calls == 0

    install = make_spec(lifecycle="install", dataset="nope")
    token = install.spec_hash[:8]
    lines = [f"spec {install.model_dump_json()}", f"approve {token}", "show", "quit"]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    assert "cannot approve: floor_not_configured\n  fix: floor\n" in transcript
    assert "approved" not in transcript and "stale_approval_token" not in transcript
    assert session.draft is not None and not session.approved(session.draft.spec)
    assert session.outcome is None


def test_blockers_list_none_and_a_warning(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    lines = [f"spec {spec_text()}", "blockers", "set budget.compute_s 100", "blockers", "quit"]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    assert f"review {EXPIRE}> blockers\nno blockers\n" in transcript
    warning = "! compile_deadline_short  warning  fix: budget compile 10 400"
    assert warning in transcript
    assert "cannot approve" not in transcript
    assert session.draft is not None and session.draft.spec.budget.compute_s == 100


def test_model_not_in_profile_shows_unknown_reservation(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    lines = [f"spec {spec_text()}", "set model_target other", "budget", "show", "quit"]
    code, transcript, _ = run_session(lines, tmp_path)
    assert code == 0
    assert "budget for other\n  min reservation per call   unknown (model not in profile)\n" in (
        transcript
    )
    assert "per-run budget for 2 calls" not in transcript
    assert "compile reservation" not in transcript
    flat = unwrap(transcript)
    assert "! model_not_configured model 'other' is not in the profile" in flat
    assert "fix: set model_target fake" in transcript
    assert "min reservation other" not in transcript
    assert "  model_target   * other" in transcript


def test_approve_challenge_stale_token_and_expire_note(tmp_path: Path) -> None:
    fake_profile(tmp_path, lifecycle="install")
    lines = [f"spec {spec_text('install')}", "approve", "approve nope", "quit"]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    challenge = (
        f"To approve and compile exactly this Spec (spec_hash {INSTALL_FULL}), type: "
        f"approve {INSTALL}"
    )
    assert unwrap(transcript).count(challenge) == 2
    assert transcript.count("error: stale_approval_token") == 1
    assert "next:" not in transcript.split("approve nope", 1)[1].split("bye")[0]
    assert "approved" not in transcript and session.outcome is None
    assert session.draft is not None and not session.approved(session.draft.spec)

    fake_profile(tmp_path)
    lines = [f"spec {spec_text()}", f"approve {EXPIRE}", "quit"]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    assert "this Spec has lifecycle expire and needs no approval; type compile" in transcript
    assert "compiling" not in transcript and session.outcome is None
    assert session.session_spend.calls == 0


def test_revise_failure_keeps_the_draft_and_review_state(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    (tmp_path / "w" / "inference.json").write_text(
        json.dumps([response(spec_text()), response("null")]), encoding="utf-8"
    )
    lines = [f"describe {GOAL}", "revise be terse", "show", "quit"]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    assert transcript.count("stage 0 done in") == 1
    assert transcript.count("error: inference_failed") == 1
    assert "clarify> " not in transcript  # no retry loop for revise
    assert f"review {EXPIRE}> show\nSpec {EXPIRE}  lifecycle expire" in transcript
    assert "spec is now" not in transcript
    assert session.draft is not None and session.draft.edits == 0
    assert session.draft.hash8 == EXPIRE  # the failed revise never replaced the Spec
    assert session.session_spend.calls == 2


def test_floor_from_review_refuses_unsupported_kinds(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    judged = make_spec().model_copy(
        update={"floor": FloorDefinition(kind=FloorKind.LLM_JUDGE, limit=1, dataset="judged-v1")}
    )
    token = judged.spec_hash[:8]
    lines = [f"spec {judged.model_dump_json()}", "floor", "show", "quit"]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    flat = unwrap(transcript)
    assert "! floor_not_configured dataset 'judged-v1' is not in the profile" in flat
    assert f"review {token}> floor\nerror: floor_kind_unsupported_in_session" in transcript
    assert "human_edit_rate needs 60 real edit-log rows and llm_judge needs calibrations" in flat
    assert f"review {token}> show" in transcript and "floor judged-v1> " not in transcript
    assert session.floor is None and session.draft is not None

    # A configured dataset of another kind is a mismatch blocker; floor shows it read-only.
    mismatch = judged.model_copy(
        update={"floor": FloorDefinition(kind=FloorKind.LLM_JUDGE, limit=1, dataset="tasks")}
    )
    token = mismatch.spec_hash[:8]
    lines = [f"spec {mismatch.model_dump_json()}", "floor", "back", "quit"]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    flat = unwrap(transcript)
    assert (
        "! floor_kind_mismatch  dataset 'tasks' is configured as held_out_tasks; the Spec says "
        "llm_judge" in flat
    )
    assert "fix: set floor.kind held_out_tasks" in transcript
    assert "floor 'tasks' is already configured; editing it here is not supported. Type back." in (
        flat
    )
    assert f"floor tasks> back\nSpec {token}  lifecycle expire" in transcript
    assert transcript.count("floor_kind_unsupported_in_session") == 0
    assert session.floor is None and session.state is State.EXIT


def test_save_creates_parent_directories(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    lines = [f"spec {spec_text()}", "save out/sub/a.json", "quit"]
    code, transcript, _ = run_session(lines, tmp_path)
    assert code == 0
    target = tmp_path / "out" / "sub" / "a.json"
    assert dense(f"saved {target}") in dense(transcript)
    assert target.read_text(encoding="utf-8") == make_spec().model_dump_json(indent=2) + "\n"


def test_discard_asks_only_after_edits_and_defaults_to_no(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    lines = [f"spec {spec_text()}", "discard", "list", "quit"]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    assert "confirm>" not in transcript
    ready = "Describe what the agent should do, or type help"
    assert f"review {EXPIRE}> discard\n" + ready in transcript
    assert session.draft is None

    new = _edited_hash8()
    lines = [f"spec {spec_text()}", "set budget.cost_usd 0.5", "discard", "", "show", "discard"]
    lines += ["y", "list", "quit"]
    session, console = make_session(lines, tmp_path)
    assert asyncio.run(session.run()) == 0
    transcript = console.transcript()
    assert console.prompts.count(f"confirm> discard Spec {new}? [y/N] ") == 2
    assert f"confirm> discard Spec {new}? [y/N] \nreview {new}> show\nSpec {new}" in transcript
    assert f"confirm> discard Spec {new}? [y/N] y\nDescribe what the agent should do" in transcript
    assert "registry: no installed artifacts" in transcript
    assert session.draft is None and session.state is State.EXIT


def test_compile_exception_returns_to_review_with_a_code(tmp_path: Path) -> None:
    fake_profile(tmp_path)

    def factory(profile: Profile, workspace: Path) -> Services:
        services = build_services(profile, workspace)

        async def failing(source: object, **kw: object) -> object:  # noqa: ANN401
            raise AdapterError("Provider CLI timed out")

        services.compiler.compile = failing  # type: ignore[method-assign]
        return services

    lines = [f"describe {GOAL}", "compile", "show", "quit"]
    code, transcript, session = run_session(lines, tmp_path, services_factory=factory)
    assert code == 0
    assert f"lifecycle expire: the compiler needs no approval; compiling exactly Spec {EXPIRE}" in (
        transcript
    )
    assert "error: provider_timeout\n  The provider CLI did not answer in time.\n" in transcript
    assert "timed out" not in transcript.replace("did not answer in time", "")
    assert "compile finished" not in transcript and "cancelled" not in transcript
    assert transcript.count(f"Spec {EXPIRE}  lifecycle expire") == 3  # entry, re-render, show
    assert f"review {EXPIRE}> show" in transcript
    assert session.draft is not None and session.outcome is None
    assert session.state is State.EXIT


# ------------------------------------------------------- Stage 0 schema shapes (Section 4)

SHAPE = '{"type":"object","properties":{...},"required":[...],"additionalProperties":false}'


def test_bare_property_maps_from_stage0_are_wrapped_into_json_schema(tmp_path: Path) -> None:
    """A model's ``{"review": {"type": "string"}}`` becomes a JSON Schema object, said per field.

    The wrapped field stays inferred (``~``), the token is the hash of the wrapped Spec, and
    a revise that proposes the bare map again is wrapped again with no change to the hash.
    """
    proposal = json.loads(make_spec(lifecycle="install").model_dump_json())
    proposal["input_schema"] = {"review": {"type": "string"}}
    proposal["output_schema"] = {"score": {"type": "number"}, "label": {"type": "string"}}
    fake_profile(tmp_path, inference_text=json.dumps(proposal))
    wrapped_input = {
        "type": "object",
        "properties": {"review": {"type": "string"}},
        "required": ["review"],
        "additionalProperties": False,
    }
    wrapped_output = {
        "type": "object",
        "properties": {"score": {"type": "number"}, "label": {"type": "string"}},
        "required": ["label", "score"],
        "additionalProperties": False,
    }
    expected = Spec.model_validate(
        {
            **proposal,
            "input_schema": wrapped_input,
            "output_schema": wrapped_output,
            "model_target": "fake",  # the initial override binds the only ladder model
        }
    )
    token = expected.spec_hash[:8]
    lines = [
        f"describe {GOAL}",
        "revise be terse",
        "blockers",
        "approve",
        f"approve {token}",
        "quit",
    ]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    notes = (
        "normalized input_schema into a JSON Schema object (properties: review)\n"
        "normalized output_schema into a JSON Schema object (properties: label, score)\n"
    )
    assert re.search(
        r"^stage 0 done in [^\n]+\n" + re.escape(notes) + rf"Spec {token}  lifecycle install",
        transcript,
        re.M,
    )
    # The wrapped schema is shown as inferred, in full, on the screen the token binds.
    assert (
        "  input_schema   ~\n"
        "                   {\n"
        '                     "additionalProperties": false,\n'
        '                     "properties": {\n'
        '                       "review": {\n'
        '                         "type": "string"\n'
        "                       }\n"
        "                     },\n"
        '                     "required": [\n'
        '                       "review"\n'
        "                     ],\n"
        '                     "type": "object"\n'
        "                   }\n"
        "  output_schema  ~\n"
    ) in transcript
    # The revise proposes the bare maps again; they are wrapped again and nothing changes.
    assert transcript.count(notes) == 2
    assert f"no changes\nspec is now {token}\n" in transcript
    assert f"review {token}> blockers\nno blockers\n" in transcript
    assert (
        f"To approve and compile exactly this Spec (spec_hash {expected.spec_hash}), "
        f"type: approve {token}\n"
    ) in unwrap(transcript)
    assert f"approved {token} - compiling." in transcript
    draft = session.draft
    assert draft is not None and draft.origin == "inferred"
    assert draft.spec.input_schema == wrapped_input
    assert draft.spec.output_schema == wrapped_output
    assert draft.spec.provenance["input_schema"] == "inferred"
    assert draft.spec.provenance["output_schema"] == "inferred"
    assert draft.user_fields == {"model_target": "fake"}
    assert session.session_spend.calls >= 2


def test_empty_schemas_block_approve_with_the_full_shape_as_the_fix(tmp_path: Path) -> None:
    """{} and a property-less object are not wrapped: they block until the operator sets them."""
    proposal = json.loads(make_spec().model_dump_json())
    proposal["input_schema"] = {}
    proposal["output_schema"] = {"type": "object"}
    fake_profile(tmp_path, inference_text=json.dumps(proposal))
    proposed = Spec.model_validate({**proposal, "model_target": "fake"})
    token = proposed.spec_hash[:8]
    full = (
        '{"type":"object","properties":{"label":{"type":"string"}},"required":["label"],'
        '"additionalProperties":false}'
    )
    fixed, _ = set_field(proposed, "output_schema", json.loads(full))
    lines = [
        f"describe {GOAL}",
        "blockers",
        "compile",
        f"set output_schema {full}",
        "blockers",
        "quit",
    ]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    assert "normalized" not in transcript
    assert "  input_schema   ~ {}\n" in transcript
    assert '  output_schema  ~ {"type":"object"}\n' in transcript
    assert (
        "! input_schema_missing input_schema is {}; no input is checked\n"
        + render.wrap(f"                       fix: set input_schema {SHAPE}")
        + "\n! output_schema_missing output_schema is type object with no properties; any output "
        "passes\n"
        + render.wrap(f"                       fix: set output_schema {SHAPE}")
        + f"\nspec_hash {proposed.spec_hash}\n"
    ) in transcript
    assert (
        f"review {token}> blockers\n"
        + render.wrap(f"! input_schema_missing  blocking  fix: set input_schema {SHAPE}")
        + "\n"
        + render.wrap(f"! output_schema_missing  blocking  fix: set output_schema {SHAPE}")
        + f"\nreview {token}> compile\n"
        "cannot approve: input_schema_missing, output_schema_missing\n"
        + render.wrap(f"  fix: set input_schema {SHAPE}")
        + "\n"
        + render.wrap(f"  fix: set output_schema {SHAPE}")
        + f"\nreview {token}> set output_schema {full}\n"
        '+ output_schema.properties.label.type "string"\n'
        '+ output_schema.required ["label"]\n'
        "+ output_schema.additionalProperties false\n"
        f"spec is now {fixed.spec_hash[:8]}\n"
        f"review {fixed.spec_hash[:8]}> blockers\n"
        + render.wrap(f"! input_schema_missing  blocking  fix: set input_schema {SHAPE}")
        + f"\nreview {fixed.spec_hash[:8]}> quit\n"
    ) in transcript
    assert "compiling " not in transcript and session.outcome is None
    assert session.draft is not None and session.draft.spec.provenance["output_schema"] == "user"


def test_spec_and_load_keep_bare_property_maps_as_written(tmp_path: Path) -> None:
    """Only Stage 0 output is wrapped; a file the operator wrote is shown exactly as written."""
    fake_profile(tmp_path)
    payload = json.loads(make_spec().model_dump_json())
    payload["input_schema"] = {"review": {"type": "string"}}
    text = json.dumps(payload)
    path = tmp_path / "bare.json"
    path.write_text(text, encoding="utf-8")
    lines = [f"spec {text}", "blockers", "discard", f"load {path}", "blockers", "quit"]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    assert "normalized" not in transcript and "schema_missing" not in transcript
    assert transcript.count('  input_schema   ~ {"review":{"type":"string"}}\n') == 2
    assert transcript.count("no blockers\n") == 2
    assert session.draft is not None and session.draft.origin == "file"
    assert session.draft.spec.input_schema == {"review": {"type": "string"}}
