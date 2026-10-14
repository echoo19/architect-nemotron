"""The Phase 12 terminal UI: the line front end, slash commands, sessions and ``-p`` mode.

Everything is offline: the conductor's model is a scripted adapter, the console is a
script, and the profile is the fake one the session tests use.
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import re
from pathlib import Path

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from session_support import fake_profile, make_spec, response
from test_agent_build import INTERVIEW, spec_json
from test_agent_conductor import DESCRIPTION, Scripted, calls

from architect import cli
from architect.agent import RecordingUI
from architect.agent.app import AppOptions, BootError, agent_workspace, boot, bootstrap_profile
from architect.agent.build import set_override
from architect.agent.commands import COMMANDS, command_names
from architect.agent.conductor import SET_ASIDE
from architect.agent.events import Notice, PlanCard, ToolFinished, ToolStarted
from architect.agent.render import PlainRenderer, toolbar_text
from architect.agent.repl import (
    ArchitectCompleter,
    LineFrontend,
    PrintUI,
    Repl,
    approval_answer,
    attach_mentions,
    cycle_mode,
    main,
    next_mode,
)
from architect.session import ScriptedConsole
from architect.session.bootstrap import CLAUDE_MODELS, SESSION_PROFILE, CliCandidate

SPEC_COMMANDS = (
    "/help", "/mode", "/make", "/spawn", "/forge", "/agents", "/run", "/report", "/recompile",
    "/library", "/floor", "/budget", "/model", "/telemetry", "/permissions", "/status",
    "/cost", "/compact", "/clear", "/resume", "/sessions", "/init", "/bench", "/exit", "/quit",
)


async def start(
    tmp_path: Path, lines: list[str], *replies: str, **options: object
) -> tuple[Repl, ScriptedConsole, Scripted]:
    if not (tmp_path / "w" / SESSION_PROFILE).exists():
        fake_profile(tmp_path)
    console = ScriptedConsole(lines)
    frontend = LineFrontend(console)
    adapter = Scripted(*replies)
    app = await boot(
        AppOptions(cwd=tmp_path, workspace=tmp_path / "w", **options),  # type: ignore[arg-type]
        frontend,
        adapter=adapter,
    )
    return Repl(app, frontend), console, adapter


async def run(
    tmp_path: Path, lines: list[str], *replies: str, **options: object
) -> tuple[str, Scripted, Repl]:
    repl, console, adapter = await start(tmp_path, lines, *replies, **options)
    try:
        assert await repl.run() == 0
    finally:
        repl.app.close()
    return console.transcript(), adapter, repl


# -- the loop -----------------------------------------------------------------------------


async def test_banner_and_a_plain_turn_print_the_answer(tmp_path: Path) -> None:
    out, adapter, repl = await run(tmp_path, ["hi there"], '{"text": "Hello from Architect."}')
    assert out.startswith("Architect 0.1.0: a terminal agent that builds agents")
    assert "normal (Shift+Tab: normal, build, plan)" in out
    assert "Floors and budgets are inferred" in out
    assert "> hi there\n" in out and "Hello from Architect.\n" in out
    assert len(adapter.prompts) == 1 and "hi there" in adapter.prompts[0]
    # The conversation was saved after the turn.
    saved = repl.app.store.list()
    assert [info.title for info in saved] == ["hi there"]
    assert repl.conductor.spent_usd > 0  # the injected adapter is metered


async def test_tool_lines_and_notices_render_as_plain_text(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("alpha\n", encoding="utf-8")
    out, _, _ = await run(
        tmp_path, ["read it"], calls(("read_file", {"path": "notes.txt"})), '{"text": "alpha"}'
    )
    assert "* read_file(notes.txt)\n" in out
    assert "  -> notes.txt (1 lines)\n" in out
    lines: list[str] = []
    renderer = PlainRenderer(lines.append)
    renderer.show(ToolFinished("c", "grep", False, "invalid_regex"))
    renderer.show(Notice("careful", "warn"))
    renderer.show(PlanCard("Plan", (("floor", "0.90 on 6 SYNTHETIC cases"),), "Enter compiles"))
    renderer.show(ToolStarted("c", "grep", "grep(x)"))
    text = "".join(lines)
    assert "  -> failed: invalid_regex" in text and "warning: careful" in text
    assert "== Plan ==" in text and "floor  0.90 on 6 SYNTHETIC cases" in text
    assert "Enter compiles" in text and "* grep(x)" in text


async def test_help_lists_every_spec_command(tmp_path: Path) -> None:
    out, adapter, _ = await run(tmp_path, ["/help", "/help mode"])
    for name in SPEC_COMMANDS:
        assert name in COMMANDS
    listing = out.split("Commands", 1)[1]
    for name in SPEC_COMMANDS:
        if name != "/quit":
            assert name in listing, name
    assert "Shift+Tab" in out and "Ctrl+J" in out
    assert "/mode [normal|build]" in out and "Show or switch mode" in out
    assert "cycle normal / build / plan mode" in out
    assert "build turns a request into a tested, licensed agent" in out
    assert "/mode forge, its old name, still works" in out  # /help mode
    # The mode is Build wherever it is named; Forge is only the harness engineer.
    assert "forge mode" not in out.lower() and "normal|forge" not in out
    assert adapter.prompts == []  # commands never call the model


async def test_unknown_command_suggests_the_closest(tmp_path: Path) -> None:
    out, _, _ = await run(tmp_path, ["/stauts", "/zzzz"])
    assert "Unknown command /stauts. Did you mean /status?" in out
    assert "Unknown command /zzzz. /help lists them." in out


async def test_mode_switches_and_steers_the_turn(tmp_path: Path) -> None:
    out, adapter, repl = await run(
        # A question: a first description in build mode would go straight to `make`.
        tmp_path, ["/mode", "/mode build", "what kinds of agents can you build?", "/mode nope",
                   "/mode normal"],
        '{"text": "ok"}',
    )
    assert "Mode: normal." in out and "Mode: build." in out
    assert "build> what kinds of agents can you build?" in out
    assert "BUILD MODE" in adapter.prompts[0]
    assert "Modes: normal, build." in out
    assert "Mode: forge." not in out and "forge> " not in out and "normal, forge" not in out
    assert repl.conductor.mode == "normal"


async def test_a_declined_plan_says_so_once_and_asks_the_model_nothing(tmp_path: Path) -> None:
    """Not now in the terminal: "no" to the plan ends the turn. The tool line shows the
    cancelled result and the one sentence follows it; nothing else in the terminal says
    it again, and the model (scripted to build again, as one did on film) is not asked."""
    profile = fake_profile(tmp_path)
    (profile.parent / "inference.json").write_text(
        json.dumps([response(INTERVIEW), response(spec_json())]), encoding="utf-8"
    )
    # Enter on the interview's three questions, then "no" to the plan.
    repl, console, adapter = await start(
        tmp_path, ["/mode build", DESCRIPTION, "", "", "", "no"],
        calls(("make", {"description": DESCRIPTION})),
    )
    set_override(repl.conductor.context, "engineer", "preset")
    try:
        assert await repl.run() == 0
    finally:
        repl.app.close()
    out = console.transcript()
    assert "Compile this agent? [yes] no" in out
    assert out.count(SET_ASIDE) == 1 and "Not compiled" not in out
    assert out.index('"cancelled": true') < out.index(SET_ASIDE)
    assert adapter.prompts == []


async def test_mode_forge_still_switches_to_build_mode(tmp_path: Path) -> None:
    out, _, repl = await run(tmp_path, ["/mode FORGE", "/status"])
    assert "Mode: build." in out and "Mode: forge." not in out
    assert re.search(r"\n +mode +build\n", out)  # the /status row
    assert repl.conductor.mode == "build"


async def test_make_runs_one_turn_in_build_mode(tmp_path: Path) -> None:
    out, adapter, repl = await run(tmp_path, ["/make an invoice extractor"], '{"text": "ok"}')
    assert "BUILD MODE" in adapter.prompts[0] and "an invoice extractor" in adapter.prompts[0]
    assert repl.conductor.mode == "normal"


def test_mode_cycle() -> None:
    assert next_mode("normal") == "build"
    assert next_mode("build") == "normal"
    assert next_mode("weird") == "normal"

    class Holder:
        mode = "normal"

    holder = Holder()  # no permission context: plan is skipped
    assert cycle_mode(holder) == "build" and holder.mode == "build"
    assert cycle_mode(holder) == "normal" and holder.mode == "normal"


async def test_shift_tab_cycles_normal_build_plan(tmp_path: Path) -> None:
    repl, _, _ = await start(tmp_path, [], permission="auto-edit")
    try:
        conductor = repl.conductor
        conductor.context.allowed.add("bash")
        assert cycle_mode(conductor) == "build" and conductor.mode == "build"
        assert cycle_mode(conductor) == "plan"
        assert conductor.mode == "normal" and conductor.context.permission == "plan"
        assert conductor.context.allowed == set()
        assert "plan mode" in conductor.transcript.messages[-1].text
        assert cycle_mode(conductor) == "normal"
        assert conductor.context.permission == "auto-edit"  # restored on leaving plan
        assert "left plan mode" in conductor.transcript.messages[-1].text
    finally:
        repl.app.close()


async def test_permissions(tmp_path: Path) -> None:
    repl, _, _ = await start(tmp_path, [])
    try:
        repl.conductor.context.allowed.add("bash")
        await repl.handle("/permissions full-auto")
        assert repl.conductor.context.permission == "full-auto"
        assert repl.conductor.settings.permission == "full-auto"
        assert repl.conductor.context.allowed == set()
        await repl.handle("/permissions sometimes")
        assert repl.conductor.context.permission == "full-auto"
    finally:
        repl.app.close()
    out, _, repl = await run(tmp_path, ["/permissions"], permission="auto-edit")
    assert "Permissions: auto-edit." in out


async def test_budget_session_and_next_compile(tmp_path: Path) -> None:
    out, _, repl = await run(
        tmp_path,
        ["/budget session 2 30", "/budget run 0.05 20", "/budget", "/budget session x",
         "/budget run 0.05"],
    )
    settings = repl.conductor.settings
    assert settings.session_usd == 2 and settings.session_s == 1800
    assert "Session budget: $2.00 and 30 minutes." in out
    overrides = repl.conductor.extras["build_overrides"]
    assert overrides["budget_usd"] == pytest.approx(0.05) and overrides["budget_s"] == 20
    assert "spent" in out and "of $2.00" in out
    assert "Usage: /budget session <usd> [minutes]" in out
    assert "Usage: /budget [session" in out


async def test_budget_build_is_the_compile_wallet_in_the_apps_words(tmp_path: Path) -> None:
    """The desktop says "build budget"; /budget build sets the same wallet as compile."""
    _, _, repl = await run(tmp_path, ["/budget build 3 20"])
    overrides = repl.conductor.extras["build_overrides"]
    assert overrides["compile_usd"] == pytest.approx(3) and overrides["compile_s"] == 1200
    _, _, repl = await run(tmp_path, ["/budget compile 4"])
    assert repl.conductor.extras["build_overrides"]["compile_usd"] == pytest.approx(4)


async def test_the_out_of_build_budget_step_is_a_command_budget_compile_reads(
    tmp_path: Path,
) -> None:
    """After a build ran out of its wallet, the next step doubles it in the units /budget
    compile reads: dollars, then minutes (the wallet itself holds seconds)."""
    from architect.agent.build import _next_step

    repl, _, _ = await start(tmp_path, [])
    try:
        wallet = repl.app.services.profile.compile.compile_budget
        step = _next_step(["compile_budget"], make_spec(), repl.app.services, None)
        command = re.search(r"/budget compile \S+ \S+", step)
        assert command is not None, step
        await repl.handle(command.group(0))
        overrides = repl.conductor.extras["build_overrides"]
        assert overrides["compile_usd"] == pytest.approx(wallet.cost_usd * 2)
        # Whole minutes, rounded up: twice the wallet's time, never less.
        assert overrides["compile_s"] == math.ceil(wallet.compute_s * 2 / 60) * 60
        assert overrides["compile_s"] >= wallet.compute_s * 2
    finally:
        repl.app.close()


async def test_floor_and_model_overrides(tmp_path: Path) -> None:
    (tmp_path / "cases.json").write_text("[]", encoding="utf-8")
    out, _, repl = await run(
        tmp_path,
        ["/floor 0.9", "/floor add cases.json", "/floor", "/floor 7", "/model", "/model fake",
         "/model nope"],
    )
    overrides = repl.conductor.extras["build_overrides"]
    assert overrides["floor_limit"] == pytest.approx(0.9)
    assert Path(overrides["floor_file"]).name == "cases.json"
    assert overrides["model"] == "fake"
    assert "Not changed (invalid_override" in out
    assert "Conductor model: fake." in out
    assert "nope is not in the profile. Models: fake." in out


async def test_status_cost_and_listings(tmp_path: Path) -> None:
    out, _, repl = await run(
        tmp_path, ["hello", "/status", "/cost", "/agents", "/library", "/telemetry"],
        '{"text": "hi"}',
    )
    assert f"session       {repl.conductor.session_id}" in out
    assert "model         fake" in out and "permissions   ask" in out
    assert "Cost (API-equivalent estimates)" in out and "last turn" in out
    assert "Installed agents" in out and "(none)" in out
    assert "harness library" in out.lower()
    assert "Telemetry" in out or "No telemetry yet." in out


async def test_sessions_and_resume_round_trip(tmp_path: Path) -> None:
    out, _, first = await run(tmp_path, ["remember the word kiwi"], '{"text": "Noted."}')
    first_id = first.conductor.session_id
    out, adapter, second = await run(
        tmp_path, ["/sessions", f"/resume {first_id[:6]}", "what was the word?"],
        '{"text": "kiwi"}',
    )
    assert first_id in out and "remember the word kiwi" in out
    assert f"Resumed {first_id}: remember the word kiwi (2 messages)." in out
    assert "remember the word kiwi" in adapter.prompts[0]
    assert second.conductor.session_id == first_id
    # /clear starts a new conversation; /resume with no id takes the latest.
    out, adapter, third = await run(tmp_path, ["/clear", "/resume", "/resume nothing-like-it"])
    assert "New conversation." in out and f"Resumed {first_id}" in out
    assert "No saved conversation matches" in out


async def test_continue_flag_reopens_the_latest(tmp_path: Path) -> None:
    await run(tmp_path, ["first message"], '{"text": "ok"}')
    fake_profile(tmp_path)  # unchanged profile; the sessions directory stays
    console = ScriptedConsole(["and again"])
    frontend = LineFrontend(console)
    from architect.agent.repl import _interactive

    adapter = Scripted('{"text": "again"}')
    code = await _interactive(
        AppOptions(cwd=tmp_path, workspace=tmp_path / "w"), frontend,
        resume=None, continue_latest=True, adapter=adapter,
    )
    assert code == 0
    assert "Resumed" in console.transcript()
    assert "first message" in adapter.prompts[0]


async def test_compact_and_clear(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("x\n" * 3000, encoding="utf-8")
    out, _, repl = await run(
        tmp_path, ["read big", "/compact"],
        calls(("read_file", {"path": "big.txt"})), '{"text": "big"}',
    )
    assert "Compacted" in out


async def test_at_path_attaches_file_text(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("the secret number is 42", encoding="utf-8")
    out, adapter, repl = await run(
        tmp_path, ["summarize @notes.txt and @../outside.txt"], '{"text": "42"}'
    )
    sent = repl.conductor.transcript.messages[0].text
    assert sent.startswith("summarize @notes.txt and @../outside.txt\n\n")
    assert '<attached file="notes.txt">\nthe secret number is 42\n</attached>' in sent
    assert "the secret number is 42" in adapter.prompts[0]
    assert "Not attached" in out and "../outside.txt" in out
    message, attached, skipped = attach_mentions("mail me@example.com", tmp_path)
    assert message == "mail me@example.com" and attached == [] and skipped == []


async def test_bang_runs_a_shell_command_without_the_model(tmp_path: Path) -> None:
    out, adapter, repl = await run(tmp_path, ["!echo hi-from-shell", "!"])
    assert "hi-from-shell" in out
    assert "Usage: !<shell command>" in out
    assert adapter.prompts == []
    assert repl.conductor.transcript.messages == []


async def test_trailing_backslash_continues_the_line(tmp_path: Path) -> None:
    _, adapter, repl = await run(tmp_path, ["first line\\", "second line"], '{"text": "ok"}')
    assert repl.conductor.transcript.messages[0].text == "first line\nsecond line"


async def test_an_interrupted_turn_keeps_the_message(tmp_path: Path) -> None:
    repl, console, adapter = await start(tmp_path, [])
    gate = asyncio.Event()
    original = adapter.complete

    async def slow(request):  # type: ignore[no-untyped-def]
        gate.set()
        await asyncio.sleep(3600)
        return await original(request)

    adapter.complete = slow  # type: ignore[method-assign]
    frontend = repl.frontend

    async def watch(task):  # type: ignore[no-untyped-def]
        await gate.wait()
        task.cancel()  # what Esc does
        await asyncio.wait({task})

    frontend.watch = watch  # type: ignore[method-assign]
    try:
        assert await repl.turn("do something long") is None
    finally:
        repl.app.close()
    assert "Interrupted. Finished work is kept" in console.transcript()
    assert [m.role for m in repl.conductor.transcript.messages] == ["user", "note"]


async def test_exit_stops_the_loop(tmp_path: Path) -> None:
    out, adapter, _ = await run(tmp_path, ["/exit", "never sent"])
    assert "never sent" not in out and adapter.prompts == []


async def test_spawn_command_runs_the_tool_and_notes_it(tmp_path: Path) -> None:
    out, _, repl = await run(tmp_path, ["/spawn synthesizer Summarize this", "/spawn"])
    assert "synthesizer: unlicensed" in out
    assert "Usage: /spawn <preset> <task>" in out
    notes = [m for m in repl.conductor.transcript.messages if m.role == "note"]
    assert notes and "spawn" in notes[0].text


# -- approvals and questions --------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "expected"),
    [([""], "no"), (["y"], "yes"), (["YES"], "yes"), (["s"], "session"), (["n"], "no"),
     (["maybe"], "no"), ([], "no")],
)
async def test_approvals_enter_is_no(typed: list[str], expected: str) -> None:
    console = ScriptedConsole(typed)
    frontend = LineFrontend(console)
    assert await frontend.approve("edit", "src/app.py") == expected
    assert console.prompts == ["Allow edit src/app.py? [y]es / [s]ession / [n]o "]


async def test_an_unanswered_approval_times_out_as_no() -> None:
    class Silent(ScriptedConsole):
        async def read(self, prompt: str) -> str | None:
            await asyncio.sleep(3600)
            return "y"

    console = Silent([])
    frontend = LineFrontend(console)
    frontend.approve_timeout_s = 0.05
    assert await frontend.approve("bash", "rm -rf build") == "no"
    assert "No answer in 5 minutes: not allowed." in console.transcript()


async def test_ask_shows_the_default_and_enter_accepts_it() -> None:
    console = ScriptedConsole(["", "custom"])
    frontend = LineFrontend(console)
    assert await frontend.ask("Outputs?", default="fields") == "fields"
    assert await frontend.ask("Outputs?", default="fields") == "custom"
    assert await frontend.ask("Outputs?", default="fields") is None  # end of input
    assert console.prompts[0] == "Outputs? [fields] "
    assert approval_answer(None) == "no" and approval_answer("always") == "session"


# -- prompt_toolkit pieces without a console ----------------------------------------------


def _complete(completer: ArchitectCompleter, text: str) -> list[str]:
    return [c.text for c in completer.get_completions(Document(text), CompleteEvent())]


def test_completer(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x", encoding="utf-8")
    completer = ArchitectCompleter(command_names, lambda: ["invoice-extractor"], tmp_path)
    assert _complete(completer, "/mo")[:2] == ["/mode", "/model"]  # prefix matches first
    assert "/permissions" in _complete(completer, "/per")
    assert _complete(completer, "/run ") == ["invoice-extractor"]
    assert _complete(completer, "/mode b") == ["build"]
    assert _complete(completer, "/mode f") == []  # the old name works but is not offered
    assert _complete(completer, "/permissions f") == ["full-auto"]
    assert _complete(completer, "look at @no") == ["@notes.txt"]
    assert _complete(completer, "@src/") == ["@src/app.py"]
    assert _complete(completer, "@../") == []
    assert _complete(completer, "plain words") == []


def test_toolbar_text_degrades_by_width() -> None:
    wide = toolbar_text(mode="build", permission="ask", model="claude-sonnet-5",
                        spent_usd=0.1234, budget_usd=5.0, elapsed_s=120, budget_s=3600,
                        running="1 call(s) running", width=160)
    assert wide.startswith("build mode (shift+tab)")
    for part in ("ask", "claude-sonnet-5", "$0.1234/$5.00", "2m/60m", "1 call(s) running"):
        assert part in wide
    narrow = toolbar_text(mode="normal", permission="full-auto", model="claude-sonnet-5",
                          spent_usd=0.5, budget_usd=5.0, elapsed_s=0, budget_s=3600, width=44)
    assert narrow.startswith("normal mode") and len(narrow) <= 42
    assert "$0.5000/$5.00" in narrow and "claude-sonnet-5" not in narrow


# -- -p mode and the entry point ----------------------------------------------------------


def test_print_mode_prints_the_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_profile(tmp_path)
    monkeypatch.chdir(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    code = main(["-p", "say hi", "--workspace", "w"], adapter=Scripted('{"text": "Hi!"}'),
                stdout=out, stderr=err)
    assert code == 0, err.getvalue()
    assert out.getvalue() == "Hi!\n"
    out = io.StringIO()
    code = main(["-p", "again", "--json", "-c", "--workspace", "w"],
                adapter=Scripted('{"text": "Again."}'), stdout=out, stderr=err)
    assert code == 0, err.getvalue()
    payload = json.loads(out.getvalue())
    assert payload["answer"] == "Again." and payload["tool_calls"] == 0
    assert payload["cost_usd"] > 0 and isinstance(payload["session"], str)
    # -c continued the first conversation.
    workspace = tmp_path / "w" / "sessions"
    saved = (workspace / f"{payload['session']}.jsonl").read_text(encoding="utf-8")
    assert "say hi" in saved and "again" in saved


def test_print_mode_through_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_profile(tmp_path)  # the fake model answers "ok", which is a plain answer
    monkeypatch.chdir(tmp_path)
    assert cli.main(["-p", "hello", "--workspace", str(tmp_path / "w")]) == 0
    captured = capsys.readouterr()
    assert captured.out == "ok\n"
    assert cli.main(["-p", "x", "--workspace", str(tmp_path / "w"), "-r", "missing"]) == 1
    assert json.loads(capsys.readouterr().err) == {"error": "session_not_found"}
    assert cli.main(["--bogus-flag"]) == 1


def test_print_ui_denies_approvals_unless_full_auto() -> None:
    denied = PrintUI(approval="no", stderr=io.StringIO())
    allowed = PrintUI(approval="yes", stderr=io.StringIO())
    assert asyncio.run(denied.approve("bash", "ls")) == "no"
    assert asyncio.run(allowed.approve("bash", "ls")) == "yes"
    stderr = io.StringIO()
    ui = PrintUI(approval="no", stderr=stderr)
    ui.emit(Notice("quiet"))
    ui.emit(Notice("loud", "error"))
    assert stderr.getvalue() == "error: loud\n"


def test_workspace_default_is_the_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ARCHITECT_WORKSPACE", raising=False)
    assert agent_workspace(tmp_path, None) == Path.home() / ".architect"
    monkeypatch.setenv("ARCHITECT_WORKSPACE", "ws")
    assert agent_workspace(tmp_path, None) == (tmp_path / "ws").resolve()
    assert agent_workspace(tmp_path, Path("other")) == (tmp_path / "other").resolve()


async def test_first_run_writes_a_profile_with_the_recommended_model(tmp_path: Path) -> None:
    candidate = CliCandidate(provider="claude-code", executable=str(tmp_path / "claude.exe"),
                             models=CLAUDE_MODELS)
    ui = RecordingUI(answers=[""])
    path = await bootstrap_profile(ui, tmp_path / "ws", probe=False, detect=lambda: (candidate,))
    assert path == tmp_path / "ws" / SESSION_PROFILE
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["compile"]["model_ladder"] == [CLAUDE_MODELS[0]]
    assert ui.questions and "Enter = recommended" in ui.questions[0]
    with pytest.raises(BootError) as failure:
        await bootstrap_profile(RecordingUI(), tmp_path / "none", probe=False, detect=lambda: ())
    assert failure.value.code == "no_provider"
    cancelled = RecordingUI(answers=[None])
    with pytest.raises(BootError):
        await bootstrap_profile(cancelled, tmp_path / "c", probe=False, detect=lambda: (candidate,))
    assert not (tmp_path / "c" / SESSION_PROFILE).exists()


async def test_optional_packages_bind_or_report_unavailable(tmp_path: Path) -> None:
    repl, _, _ = await start(tmp_path, [])
    try:
        extras = repl.conductor.extras
        assert {"meter", "profile_path", "reload", "build_overrides"} <= set(extras)
        for key in ("make", "forge", "coder"):
            assert (key in extras) != (key in repl.app.unavailable)
        old = repl.app.services
        fresh = extras["reload"]()  # type: ignore[operator]
        assert fresh is repl.conductor.services and fresh is repl.conductor.context.services
        assert fresh is not old
    finally:
        repl.app.close()


# -- the prompt_toolkit front end over a pipe (no console needed) --------------------------


async def rich_start(tmp_path: Path, pipe, *replies: str):  # type: ignore[no-untyped-def]
    from prompt_toolkit.output import DummyOutput

    from architect.agent.repl import RichFrontend

    fake_profile(tmp_path)
    out = io.StringIO()
    frontend = RichFrontend(out, pt_input=pipe, pt_output=DummyOutput())
    adapter = Scripted(*replies)
    app = await boot(AppOptions(cwd=tmp_path, workspace=tmp_path / "w"), frontend,
                     adapter=adapter)
    return Repl(app, frontend), frontend, adapter, out


async def test_rich_frontend_keys(tmp_path: Path) -> None:
    from prompt_toolkit.input import create_pipe_input

    from architect.agent.repl import Interrupted

    with create_pipe_input() as pipe:
        repl, frontend, _, _ = await rich_start(tmp_path, pipe)
        try:
            pipe.send_text("\x1b[Z")  # Shift+Tab
            pipe.send_text("line one\x0aline two\r")  # Ctrl+J, then Enter
            assert await frontend.read_line() == "line one\nline two"
            assert repl.conductor.mode == "build"
            pipe.send_text("ends with" + "\\" + "\rmore\r")  # a trailing \ is a newline
            assert await frontend.read_line() == "ends with\nmore"
            pipe.send_text("abc\x03xyz\r")  # Ctrl+C with text clears the line
            assert await frontend.read_line() == "xyz"
            pipe.send_text("\x03")  # Ctrl+C on an empty line
            with pytest.raises(Interrupted):
                await frontend.read_line()
            pipe.send_text("\x04")  # Ctrl+D
            assert await frontend.read_line() is None
            assert (tmp_path / "w" / "history").exists()
        finally:
            repl.app.close()


async def test_rich_frontend_esc_cancels_the_turn_and_answers_approvals(tmp_path: Path) -> None:
    from prompt_toolkit.input import create_pipe_input

    with create_pipe_input() as pipe:
        repl, frontend, adapter, out = await rich_start(tmp_path, pipe, '{"text": "**Hi**"}')
        try:
            await repl.turn("hello")
            assert "Hi" in out.getvalue()
            gate = asyncio.Event()

            async def slow(request):  # type: ignore[no-untyped-def]
                gate.set()
                await asyncio.sleep(3600)

            adapter.complete = slow  # type: ignore[method-assign]

            async def press_escape() -> None:
                await gate.wait()
                await asyncio.sleep(0.2)
                pipe.send_text("\x1b")

            presser = asyncio.create_task(press_escape())
            assert await asyncio.wait_for(repl.turn("long job"), 30) is None
            await presser
            assert "Interrupted" in out.getvalue()
            assert [m.role for m in repl.conductor.transcript.messages][-2:] == ["user", "note"]
            pipe.send_text("s\r")
            assert await frontend.approve("bash", "pytest") == "session"
            pipe.send_text("\r")
            assert await frontend.approve("bash", "pytest") == "no"
            pipe.send_text("\r")
            assert await frontend.ask("Where?", default="installed") == "installed"
        finally:
            repl.app.close()


async def test_forge_arc_runs_only_after_an_explicit_yes(tmp_path: Path) -> None:
    """ARC play may use this machine's GPU: /forge arc explains; --run asks first."""
    out, _, repl = await run(tmp_path, ["/forge arc", "/forge arc --run", "n"])
    assert "Build and play an ARC harness" in out and "Not run." in out
    assert "allow_arc_run" not in repl.conductor.extras
    assert "arc_harness.py" in out  # the plain /forge arc answer gives the commands


async def test_first_run_lists_every_detected_model_and_adds_one_on_demand(
    tmp_path: Path,
) -> None:
    from architect.agent.app import add_detected_model


    candidate = CliCandidate(provider="claude-code", executable=str(tmp_path / "claude.exe"),
                             models=CLAUDE_MODELS)
    path = await bootstrap_profile(RecordingUI(), tmp_path / "ws", probe=False,
                                   detect=lambda: (candidate,))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert len(payload["models"]) > 1  # every detected model, the chosen one first in the ladder
    assert payload["compile"]["model_ladder"] == [next(iter(payload["models"]))]
    # An older profile with one model gains a detected model on demand.
    extra = [m for m in payload["models"] if m not in payload["compile"]["model_ladder"]][0]
    del payload["models"][extra]
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert add_detected_model(path, extra, detect=lambda: (candidate,))
    assert extra in json.loads(path.read_text(encoding="utf-8"))["models"]
    assert not add_detected_model(path, "no-such-model", detect=lambda: (candidate,))


async def test_run_rejects_broken_json_before_running(tmp_path: Path) -> None:
    out, adapter, _ = await run(tmp_path, ['/run some-agent {"invoice_text": "ACME"'])
    assert "looks like JSON but does not parse" in out
    assert adapter.prompts == []  # nothing ran, no model call
