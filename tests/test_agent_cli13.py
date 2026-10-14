"""Phase 13 Package C: the CLI experience (streaming, context, checkpoints, config, tools).

Everything is offline: scripted adapters and consoles, httpx.MockTransport for web_fetch,
and every probe of a local server or clipboard replaced. Git is used only on a scratch
repository when it is installed.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from session_support import fake_profile
from test_agent_conductor import Scripted, calls
from test_agent_repl import run, start

from architect.agent import RecordingUI
from architect.agent import commands as commands_module
from architect.agent import tools as tools_module
from architect.agent.app import AppOptions, BootError, agent_workspace, boot, first_run
from architect.agent.checkpoints import (
    CheckpointStore,
    capture,
    changed_since,
    checkpointed,
)
from architect.agent.conductor import TurnResult
from architect.agent.config import (
    ConfigError,
    context_window,
    editor_command,
    load_config,
    set_value,
)
from architect.agent.events import AssistantText, Notice, TextDelta, Thinking, ToolStarted
from architect.agent.render import (
    PlainRenderer,
    RichRenderer,
    context_style,
    split_blocks,
    toolbar_fragments,
    toolbar_text,
)
from architect.agent.repl import (
    ArchitectCompleter,
    LineFrontend,
    Repl,
    _Typing,
    edit_text,
    fuzzy_rank,
)
from architect.agent.tools import ToolContext, ToolError, html_to_text
from architect.models.images import encode_png_gray
from architect.session import ScriptedConsole
from architect.session.bootstrap import SESSION_PROFILE

# -- streaming ----------------------------------------------------------------------------


def test_plain_renderer_streams_deltas_and_does_not_repeat_the_answer() -> None:
    out: list[str] = []
    renderer = PlainRenderer(out.append)
    renderer.show(Thinking(True))
    renderer.show(TextDelta("Hel"))
    renderer.show(TextDelta("lo there"))
    assert "".join(out) == "Hello there"  # written as it arrives
    renderer.show(Thinking(False))
    renderer.show(AssistantText("Hello there"))
    assert "".join(out) == "Hello there\n"
    # Streamed text before a tool call ends its line; the next answer prints normally.
    renderer.show(Thinking(True))
    renderer.show(TextDelta("Let me look"))
    renderer.show(ToolStarted("c", "read_file", "read_file(x)"))
    renderer.show(Thinking(True))
    renderer.show(AssistantText("Done."))
    text = "".join(out)
    assert "Let me look\n* read_file(x)\n" in text and text.endswith("Done.\n")
    # A final answer that extends the stream prints only the missing tail.
    out.clear()
    renderer.show(Thinking(True))
    renderer.show(TextDelta("Part one"))
    renderer.show(AssistantText("Part one and two"))
    assert "".join(out) == "Part one\n and two\n"


def test_split_blocks_keeps_open_fences_pending() -> None:
    assert split_blocks("one\n\ntwo") == ("one\n\n", "two")
    assert split_blocks("```py\nx = 1\n\ny = 2\n") == ("", "```py\nx = 1\n\ny = 2\n")
    assert split_blocks("```py\nx = 1\n```\nafter") == ("```py\nx = 1\n```\n", "after")
    assert split_blocks("no newline yet") == ("", "no newline yet")


def _rich(width: int = 80) -> tuple[RichRenderer, io.StringIO]:
    from rich.console import Console

    stream = io.StringIO()
    console = Console(file=stream, width=width, force_terminal=False, color_system=None)
    return RichRenderer(console), stream


def test_rich_renderer_prints_streamed_markdown_once() -> None:
    renderer, stream = _rich()
    renderer.show(Thinking(True))
    renderer.show(TextDelta("First **para**.\n\nSecond"))
    assert "First para." in stream.getvalue()  # the finished block is printed already
    assert "Second" not in stream.getvalue()  # the open block is in the live region
    renderer.show(TextDelta(" para."))
    renderer.show(Thinking(False))
    renderer.show(AssistantText("First **para**.\n\nSecond para."))
    text = stream.getvalue()
    assert text.count("First para.") == 1 and text.count("Second para.") == 1
    assert "\x1b[K" not in text


def test_rich_renderer_wraps_a_long_path_on_a_card_instead_of_cutting_it() -> None:
    from architect.agent.events import PlanCard

    path = "C:\\t\\home\\.architect\\registry\\versions\\" + "cd018c5bdbf3" * 8 + "\\artifacts"
    renderer, stream = _rich(width=60)
    renderer.show(PlanCard("Result: r v1", (("next", f'shell: uv run architect run "{path}"'),)))
    renderer.rows("Agents", [("r", path)])
    text = stream.getvalue()
    assert "\u2026" not in text  # no ellipsis
    joined = "".join(line.strip(" |\u2502") for line in text.splitlines())
    assert joined.count("cd018c5bdbf3" * 8) == 2  # each copy of the path is printed whole


def test_rich_renderer_diff_and_notices() -> None:
    renderer, stream = _rich()
    renderer.diff("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n")
    renderer.show(Notice("careful", "warn"))
    text = stream.getvalue()
    assert "-old" in text and "+new" in text and "careful" in text


async def test_a_streamed_turn_in_the_line_ui_prints_the_answer_once(tmp_path: Path) -> None:
    repl, console, _ = await start(tmp_path, ["hello"])
    ui = repl.frontend

    async def streamed(message: str) -> TurnResult:
        ui.emit(Thinking(True))
        for piece in ("Hi ", "from ", "a stream."):
            ui.emit(TextDelta(piece))
            await asyncio.sleep(0)
        ui.emit(Thinking(False))
        ui.emit(AssistantText("Hi from a stream."))
        return TurnResult("Hi from a stream.", 0, 0.0)

    repl.conductor.turn = streamed  # type: ignore[method-assign]
    try:
        await repl.run()
    finally:
        repl.app.close()
    assert console.transcript().count("Hi from a stream.") == 1


# -- status line and context --------------------------------------------------------------


def test_toolbar_shows_context_fill_with_colour_thresholds() -> None:
    text = toolbar_text(mode="build", permission="plan", model="m", spent_usd=0.0,
                        budget_usd=5.0, context_percent=42.4, queued=2, width=160)
    assert text.startswith("build mode") and "ctx 42%" in text and "2 queued" in text
    build = toolbar_fragments(mode="build", permission="ask", model="m", spent_usd=0.0,
                              budget_usd=5.0, width=160)
    assert ("fg:ansimagenta bold", "build mode (shift+tab)") in build
    plan = toolbar_text(mode="normal", permission="plan", model="m", spent_usd=0.0,
                        budget_usd=5.0, width=160)
    assert plan.startswith("plan mode")
    assert [context_style(p) for p in (10, 60, 90)] == ["green", "yellow", "red"]
    fragments = toolbar_fragments(mode="normal", permission="ask", model="m", spent_usd=0.0,
                                  budget_usd=5.0, context_percent=91.0, width=160)
    assert ("fg:ansired bold", "ctx 91%") in fragments


async def test_context_command_and_auto_compact(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    (tmp_path / "w" / "config.toml").write_text("auto_compact_percent = 1\n", encoding="utf-8")
    (tmp_path / "big.txt").write_text("x" * 40 + "\n" * 3000, encoding="utf-8")
    out, _, repl = await run(
        tmp_path, ["read big", "/context", "again"],
        calls(("read_file", {"path": "big.txt"})), '{"text": "big"}', '{"text": "ok"}',
    )
    assert "Context:" in out and "tool results (1)" in out and "free" in out
    assert "Auto-compact at 1%" in out
    assert "compacted older tool results" in out
    assert repl.app.context_tokens() == 200_000  # unknown models get the 200k default
    assert context_window("claude-sonnet-5") == 200_000
    assert context_window("gemini-2.5-pro") == 1_000_000


def test_context_windows_know_nemotron_on_nebius() -> None:
    assert context_window("nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B") == 262_144
    assert context_window("nvidia/nemotron-3-super-120b-a12b") == 256_000
    assert context_window("nvidia/Nemotron-3-Ultra-550b-a55b") == 1_000_000
    assert context_window("nvidia/Nemotron-3_5-Lightning") == 1_000_000


# -- checkpoints, /undo and /diff ---------------------------------------------------------


def test_checkpoint_store_records_restores_and_diffs(tmp_path: Path) -> None:
    work = tmp_path / "repo"
    work.mkdir()
    (work / "a.txt").write_text("old\n", encoding="utf-8")
    (work / "gone.txt").write_text("keep me\n", encoding="utf-8")
    snapshot = capture(work)
    (work / "a.txt").write_text("new\n", encoding="utf-8")
    # Same size edit: on a coarse-clock filesystem it can land in the same mtime tick
    stamp = (work / "a.txt").stat().st_mtime_ns + 2_000_000_000
    os.utime(work / "a.txt", ns=(stamp, stamp))
    (work / "b.txt").write_text("created\n", encoding="utf-8")
    (work / "gone.txt").unlink()
    changed = changed_since(snapshot)
    assert changed == {"a.txt", "b.txt", "gone.txt"}
    store = CheckpointStore(tmp_path / "checkpoints")
    checkpoint = store.record("s1", 1, snapshot, changed, "edit things")
    assert checkpoint is not None
    states = {record.path: record.state for record in checkpoint.files}
    assert states == {"a.txt": "file", "b.txt": "absent", "gone.txt": "file"}
    diff = store.diff("s1", work)
    assert "-old" in diff and "+new" in diff and "+created" in diff and "-keep me" in diff
    done = store.restore(checkpoint, work)
    assert sorted(done.restored) == ["a.txt", "gone.txt"] and done.deleted == ["b.txt"]
    assert (work / "a.txt").read_text(encoding="utf-8") == "old\n"
    assert not (work / "b.txt").exists()
    assert store.last("s1") is None  # undone
    assert store.diff("s1", work) == ""


async def test_checkpointed_coder_records_even_when_cancelled(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("before", encoding="utf-8")
    started = asyncio.Event()

    async def coder(request: str, ctx: ToolContext) -> Any:
        (ctx.cwd / "f.txt").write_text("after", encoding="utf-8")
        started.set()
        await asyncio.sleep(3600)

    store = CheckpointStore(tmp_path / "cp")
    ctx = ToolContext(services=None, cwd=tmp_path, ui=RecordingUI(), model="m",  # type: ignore[arg-type]
                      extras={"checkpoints": store, "checkpoint_key": ("sess", 3)})
    task = asyncio.create_task(checkpointed(coder)("edit", ctx))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    checkpoint = store.last("sess")
    assert checkpoint is not None and checkpoint.turn == 3
    assert [record.path for record in checkpoint.files] == ["f.txt"]


async def test_undo_restores_the_last_turns_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def not_git(repl: Repl) -> bool:
        return False

    monkeypatch.setattr(commands_module, "_in_git_repo", not_git)
    (tmp_path / "app.py").write_text("old\n", encoding="utf-8")

    async def coder(request: str, ctx: ToolContext) -> Any:
        (ctx.cwd / "app.py").write_text("new\n", encoding="utf-8")
        (ctx.cwd / "extra.py").write_text("x = 1\n", encoding="utf-8")
        return {"ok": True, "summary": "edited", "changed_files": ["app.py", "extra.py"],
                "tool_calls": 2, "denied": 0, "cost_usd": 0.0}

    repl, console, _ = await start(
        tmp_path, ["/undo", "/diff", "change it", "/diff", "/undo", "y", "/undo"],
        calls(("code", {"task": "change app.py"})), '{"text": "Changed."}',
    )
    repl.conductor.extras["coder"] = checkpointed(coder)
    try:
        await repl.run()
    finally:
        repl.app.close()
    out = console.transcript()
    assert "Nothing to undo: the coder has not changed files" in out
    assert "No changes yet" in out
    assert "+new" in out and "-old" in out  # /diff from the checkpoint
    assert "app.py" in out and "delete (new file)" in out
    assert "Undone: restored 1, deleted 1." in out
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "old\n"
    assert not (tmp_path / "extra.py").exists()
    assert out.count("Nothing to undo") == 2
    assert any("undid" in m.text for m in repl.conductor.transcript.messages if m.role == "note")


def _git_repo(repo: Path) -> None:
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-c",
           "commit.gpgsign=false"]
    subprocess.run([*git, "init", "-q"], cwd=repo, check=True)
    subprocess.run([*git, "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], cwd=repo, check=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
async def test_diff_uses_git_in_a_repository(tmp_path: Path) -> None:
    repo = tmp_path
    _git_repo(repo)
    (repo / "tracked.txt").write_text("two\n", encoding="utf-8")
    (repo / "new.txt").write_text("n\n", encoding="utf-8")
    out, _, _ = await run(tmp_path, ["/diff"])
    assert "-one" in out and "+two" in out
    assert "Untracked files:" in out and "new.txt" in out


# -- plan mode ----------------------------------------------------------------------------


async def test_plan_mode_refuses_the_coder_and_asks_for_a_proposal(tmp_path: Path) -> None:
    called: list[str] = []

    async def coder(request: str, ctx: ToolContext) -> Any:
        called.append(request)
        return {"ok": True}

    repl, console, adapter = await start(
        tmp_path, ["/permissions plan", "fix it", "/permissions ask"],
        calls(("code", {"task": "edit"})), '{"text": "Plan: edit app.py"}',
        permission="auto-edit",
    )
    repl.conductor.extras["coder"] = coder
    try:
        await repl.run()
    finally:
        repl.app.close()
    out = console.transcript()
    assert called == []
    assert "failed: " in out and "plan_mode_read_only" in adapter.prompts[1]
    assert "Propose the change instead" in adapter.prompts[1]
    assert "Permissions: plan." in out and "Permissions: ask." in out
    assert "plan> fix it" in out


# -- config -------------------------------------------------------------------------------


def test_config_files_merge_and_report_bad_values(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "config.toml").write_text(
        'permission = "auto-edit"\nsession_budget_usd = 3\nsession_minutes = "soon"\n'
        '[mcp.servers.files]\ncommand = "npx"\n', encoding="utf-8")
    project = tmp_path / "proj"
    (project / ".architect").mkdir(parents=True)
    (project / ".architect" / "config.toml").write_text('permission = "plan"\n',
                                                        encoding="utf-8")
    config = load_config(workspace, project)
    assert config.get("permission") == "plan" and config.source("permission") == "project"
    assert config.number("session_budget_usd") == 3 and config.source("session_budget_usd") \
        == "workspace"
    assert config.number("session_minutes") == 60  # the bad value is ignored, with a note
    assert any("session_minutes" in error for error in config.errors)
    assert config.extra["mcp"] == {"servers": {"files": {"command": "npx"}}}
    assert config.number("auto_compact_percent") == 85
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "config.toml").write_text("this is = = not toml", encoding="utf-8")
    assert "not valid TOML" in load_config(broken).errors[0]


def test_set_value_keeps_tables_and_comments(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('# my settings\ntheme = "rich"\n\n[mcp.servers.x]\ncommand = "y"\n',
                    encoding="utf-8")
    assert set_value(path, "theme", "plain") == "plain"
    assert set_value(path, "session_budget_usd", "$12.5") == 12.5
    assert set_value(path, "editor", 'code --wait "x"') == 'code --wait "x"'
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# my settings\ntheme = \"plain\"\n")
    assert 'session_budget_usd = 12.5\neditor = "code --wait \\"x\\""\n\n[mcp.servers.x]' in text
    assert set_value(path, "theme", "default") is None
    assert "theme" not in path.read_text(encoding="utf-8")
    config = load_config(tmp_path)
    assert config.text("editor") == 'code --wait "x"'
    for key, value in (("nope", "1"), ("permission", "sometimes"),
                       ("auto_compact_percent", "120"), ("web_fetch_allow_private", "maybe")):
        with pytest.raises(ConfigError):
            set_value(path, key, value)
    assert editor_command("nano -w") == ["nano", "-w"]


async def test_config_command_shows_and_sets(tmp_path: Path) -> None:
    out, _, repl = await run(
        tmp_path, ["/config", "/config set session_budget_usd 7", "/config set nope 1",
                   "/config set permission sometimes", "/config set permission full-auto",
                   "/config set web_fetch_allow_private true", "/config bogus"],
    )
    assert "Settings ([source])" in out and "auto_compact_percent" in out
    assert "session_budget_usd = 7 (saved" in out and repl.conductor.settings.session_usd == 7
    assert "Unknown setting nope." in out and "Not changed (invalid_value" in out
    assert repl.conductor.context.permission == "full-auto"
    assert repl.conductor.extras["web_fetch_allow_private"] is True
    assert "Usage: /config" in out
    saved = (tmp_path / "w" / "config.toml").read_text(encoding="utf-8")
    assert "session_budget_usd = 7" in saved and 'permission = "full-auto"' in saved


async def test_project_config_sets_the_permission_at_boot(tmp_path: Path) -> None:
    (tmp_path / ".architect").mkdir()
    (tmp_path / ".architect" / "config.toml").write_text(
        'permission = "plan"\nsession_minutes = 5\n', encoding="utf-8")
    out, _, repl = await run(tmp_path, [])
    assert repl.conductor.context.permission == "plan"
    assert repl.conductor.settings.session_s == 300
    assert "mode            plan" in out


# -- web_fetch ----------------------------------------------------------------------------


PAGE = (
    "<html><head><title>Docs  Page</title><style>p{}</style><script>alert(1)</script></head>"
    "<body><h1>Install</h1><p>Run <code>uv sync</code> first.</p><ul><li>one</li>"
    "<li>two</li></ul><pre>  keep\n    spacing</pre></body></html>"
)


def test_html_to_text() -> None:
    title, text = html_to_text(PAGE)
    assert title == "Docs Page"
    assert "# Install" in text and "Run uv sync first." in text
    assert "- one\n" in text and "alert" not in text and "p{}" not in text
    assert "keep" in text and "spacing" in text


def _web_ctx(tmp_path: Path, handler: Any, **extras: Any) -> ToolContext:
    return ToolContext(services=None, cwd=tmp_path, ui=RecordingUI(), model="m",  # type: ignore[arg-type]
                       extras={"web_transport": httpx.MockTransport(handler), **extras})


async def test_web_fetch_reads_pages_and_refuses_private_addresses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def resolve(host: str) -> list[str]:
        return {"docs.example.com": ["93.184.216.34"], "localhost": ["127.0.0.1"],
                "intranet": ["10.1.2.3"]}[host]

    monkeypatch.setattr(tools_module, "_resolve", resolve)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/page":
            return httpx.Response(200, html=PAGE)
        if request.url.path == "/hop":
            return httpx.Response(302, headers={"location": "http://intranet/secret"})
        if request.url.path == "/bin":
            return httpx.Response(200, content=b"\x00\x01",
                                  headers={"content-type": "application/octet-stream"})
        if request.url.path == "/long":
            return httpx.Response(200, text="y" * 5000)
        return httpx.Response(404, text="missing")

    ctx = _web_ctx(tmp_path, handler)
    result = await tools_module._web_fetch({"url": "https://docs.example.com/page"}, ctx)
    assert isinstance(result, dict)
    assert result["title"] == "Docs Page" and "uv sync" in str(result["text"])
    assert result["status"] == 200 and result["truncated"] is False
    long = await tools_module._web_fetch(
        {"url": "https://docs.example.com/long", "max_chars": 1000}, ctx)
    assert isinstance(long, dict) and len(str(long["text"])) == 1000 and long["truncated"]
    missing = await tools_module._web_fetch({"url": "https://docs.example.com/x"}, ctx)
    assert isinstance(missing, dict) and missing["error"] == "http_404"
    for url, code in (("http://localhost:8080/", "private_address"),
                      ("http://127.0.0.1/", "private_address"),
                      ("http://[::1]/", "private_address"),
                      ("https://docs.example.com/hop", "private_address"),
                      ("file:///etc/passwd", "unsupported_scheme"),
                      ("https://docs.example.com/bin", "unsupported_content_type")):
        with pytest.raises(ToolError) as failure:
            await tools_module._web_fetch({"url": url}, ctx)
        assert failure.value.code == code, url
    allowed = _web_ctx(tmp_path, handler, web_fetch_allow_private=True)
    local = await tools_module._web_fetch({"url": "http://localhost/page"}, allowed)
    assert isinstance(local, dict) and local["title"] == "Docs Page"
    assert "web_fetch" in [tool.name for tool in tools_module.default_tools()]


# -- custom commands, completion and input ------------------------------------------------


async def test_custom_commands_run_as_prompts_and_appear_in_help(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    project = tmp_path / ".architect" / "commands"
    project.mkdir(parents=True)
    (project / "fix.md").write_text("---\ndescription: Fix a bug\n---\nFix this bug: $ARGUMENTS",
                                    encoding="utf-8")
    (project / "help.md").write_text("never shadows /help", encoding="utf-8")
    (project / "Bad Name.md").write_text("ignored", encoding="utf-8")
    workspace = tmp_path / "w" / "commands"
    workspace.mkdir(parents=True)
    (workspace / "hello.md").write_text("# Say hello\nSay hello to the user.", encoding="utf-8")
    out, adapter, repl = await run(
        tmp_path, ["/fix the login page", "/hello bob", "/help", "/fxi"],
        '{"text": "fixed"}', '{"text": "hi"}',
    )
    assert "Fix this bug: the login page" in adapter.prompts[0]
    assert "Say hello to the user.\\n\\nbob" in adapter.prompts[1]
    assert "Custom commands" in out and "/fix [args]" in out and "Fix a bug" in out
    assert "never shadows" not in out
    assert "Did you mean /fix?" in out
    names = repl.command_names()
    assert "/fix" in names and "/hello" in names and "/bad name" not in names
    assert repl.describe_command("/fix") == "Fix a bug (custom)"
    completer = ArchitectCompleter(repl.command_names, lambda: [], tmp_path,
                                   repl.describe_command)
    found = list(completer.get_completions(Document("/fi"), CompleteEvent()))
    assert found[0].text == "/fix" and found[0].display_meta_text == "Fix a bug (custom)"
    keys = [c.text for c in completer.get_completions(Document("/config set sess"),
                                                      CompleteEvent())]
    assert keys == ["session_budget_usd", "session_minutes"]


def test_fuzzy_rank_puts_prefix_matches_first() -> None:
    names = ["/model", "/mode", "/memory", "/compact", "/permissions"]
    assert fuzzy_rank("/mo", names) == ["/mode", "/model", "/memory", "/permissions"]
    assert fuzzy_rank("/prm", names) == ["/permissions"]


def test_typing_during_a_turn_is_collected() -> None:
    from prompt_toolkit.key_binding import KeyPress
    from prompt_toolkit.keys import Keys

    typing = _Typing()
    pressed = [KeyPress(c, c) for c in "nexx"] + [KeyPress(Keys.Backspace, "")]
    pressed += [KeyPress("t", "t"), KeyPress(Keys.ControlM, "\r"), KeyPress("a", "a")]
    assert typing.feed(pressed) == ["next"]
    assert typing.text == "a"
    assert typing.feed([KeyPress(Keys.ControlM, "\r")]) == ["a"]
    assert typing.feed([KeyPress(Keys.ControlM, "\r")]) == []  # an empty line is not sent


def test_edit_text_runs_the_editor(tmp_path: Path) -> None:
    script = tmp_path / "fake_editor.py"
    script.write_text(
        "import sys\nfrom pathlib import Path\np = Path(sys.argv[1])\n"
        "p.write_text(p.read_text(encoding='utf-8') + ' edited\\n', encoding='utf-8')\n",
        encoding="utf-8")
    assert edit_text("draft", [sys.executable, str(script)]) == "draft edited"
    assert edit_text("draft", [sys.executable, "-c", "raise SystemExit(3)"]) is None
    assert edit_text("draft", [str(tmp_path / "no-such-editor")]) is None


class _Feed:
    """An interactive console whose lines arrive when the test sends them."""

    interactive = True

    def __init__(self) -> None:
        self.lines: asyncio.Queue[str | None] = asyncio.Queue()
        self.output: list[str] = []

    async def read(self, prompt: str) -> str | None:
        self.output.append(prompt)
        return await self.lines.get()

    def write(self, text: str) -> None:
        self.output.append(text)


async def test_line_ui_queues_messages_typed_during_a_turn(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    feed = _Feed()
    frontend = LineFrontend(feed, queue_input=True)
    release = asyncio.Event()
    started = asyncio.Event()

    class Slow(Scripted):
        async def complete(self, request):  # type: ignore[no-untyped-def]
            if not started.is_set():
                started.set()
                await release.wait()
            return await super().complete(request)

    adapter = Slow('{"text": "first answer"}', '{"text": "second answer"}')
    app = await boot(AppOptions(cwd=tmp_path, workspace=tmp_path / "w"), frontend,
                     adapter=adapter)
    repl = Repl(app, frontend)
    runner = asyncio.create_task(repl.run())
    try:
        await feed.lines.put("first")
        await asyncio.wait_for(started.wait(), 10)
        await feed.lines.put("second")
        for _ in range(100):
            if repl.queue:
                break
            await asyncio.sleep(0.01)
        assert repl.queue == ["second"]
        release.set()
        for _ in range(500):
            if len(adapter.prompts) == 2:
                break
            await asyncio.sleep(0.01)
        await feed.lines.put(None)
        assert await asyncio.wait_for(runner, 10) == 0
    finally:
        app.close()
    out = "".join(feed.output)
    assert "(queued: sent after this turn; 1 waiting)" in out
    assert "> second" in out and "second answer" in out
    assert "second" in adapter.prompts[1]


async def test_rich_ui_queues_typing_during_a_turn(tmp_path: Path) -> None:
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from architect.agent.repl import RichFrontend

    fake_profile(tmp_path)
    with create_pipe_input() as pipe:
        frontend = RichFrontend(io.StringIO(), pt_input=pipe, pt_output=DummyOutput())
        adapter = Scripted()
        gate = asyncio.Event()

        async def slow(request):  # type: ignore[no-untyped-def]
            gate.set()
            await asyncio.sleep(3600)

        adapter.complete = slow  # type: ignore[method-assign]
        app = await boot(AppOptions(cwd=tmp_path, workspace=tmp_path / "w"), frontend,
                         adapter=adapter)
        repl = Repl(app, frontend)
        try:
            async def type_then_escape() -> None:
                await gate.wait()
                await asyncio.sleep(0.2)
                pipe.send_text("next one\r")
                await asyncio.sleep(0.3)
                pipe.send_text("draf")
                await asyncio.sleep(0.3)
                pipe.send_text("\x1b")

            typist = asyncio.create_task(type_then_escape())
            assert await asyncio.wait_for(repl.turn("long job"), 30) is None
            await typist
            assert repl.queue == ["next one"]
            assert frontend.typing.text == "draf"
            fragments = repl.toolbar_fragments(160, frontend.glyphs)
            assert any("1 queued" in text for _, text in fragments)
            pipe.send_text("t\r")  # the unfinished text becomes the next prompt's start
            assert await frontend.read_line() == "draft"
        finally:
            app.close()


# -- images -------------------------------------------------------------------------------


async def test_image_mentions_attach_a_reference(tmp_path: Path) -> None:
    png = encode_png_gray([b"\x00" * 4] * 3, 4, 3)
    (tmp_path / "pic.png").write_bytes(png)
    (tmp_path / "fake.png").write_bytes(b"not an image")
    out, adapter, repl = await run(
        tmp_path, ["what is in @pic.png and @fake.png", "/image pic.png", "describe it",
                   "/image missing.png"],
        '{"text": "a 4x3 image"}', '{"text": "grey"}',
    )
    first = repl.conductor.transcript.messages[0].text
    assert '<attached image="pic.png" media_type="image/png" size="4x3"' in first
    assert "Not attached" in out and "fake.png" in out
    assert "reference only" in out
    second = [m.text for m in repl.conductor.transcript.messages if m.role == "user"][1]
    assert second.startswith("describe it") and 'attached image="pic.png"' in second
    assert "missing.png is not a PNG" in out
    assert "pending_images" not in repl.conductor.extras


# -- export, copy, review, title, memory, doctor ------------------------------------------


async def test_export_copy_and_title(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copied: list[str] = []

    def fake_copy(text: str) -> bool:
        copied.append(text)
        return True

    monkeypatch.setattr(commands_module, "copy_to_clipboard", fake_copy)
    out, _, repl = await run(
        tmp_path, ["/export", "/copy", "/title", "hello there", "/title My work", "/copy",
                   "/export notes/out.md", "/sessions", "/export notes/out.md", "n"],
        '{"text": "General **Kenobi**"}',
    )
    assert "Nothing to export yet." in out and "No answer to copy yet." in out
    assert "Untitled" in out and "Title: My work." in out
    assert copied == ["General **Kenobi**"]
    exported = (tmp_path / "notes" / "out.md").read_text(encoding="utf-8")
    assert exported.startswith("# My work\n")
    assert "## You\n\nhello there" in exported and "## Architect\n\nGeneral **Kenobi**" in exported
    assert "My work" in out.split("Saved conversations", 1)[1]
    assert "Not exported." in out
    info = repl.app.store.find(repl.conductor.session_id)
    assert info is not None and info.title == "My work" and info.named
    monkeypatch.setattr(commands_module, "copy_to_clipboard", lambda text: False)
    out, _, _ = await run(tmp_path, ["/resume", "/copy"])
    assert "No clipboard tool found" in out


async def test_review_sends_the_diff_to_the_conductor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    diffs = iter([("git", ""), ("git", "diff --git a/x b/x\n+added line\n")])

    async def fake_diff(repl: Repl) -> tuple[str, str]:
        return next(diffs)

    monkeypatch.setattr(commands_module, "working_diff", fake_diff)
    out, adapter, _ = await run(tmp_path, ["/review", "/review security"],
                                '{"text": "Looks good."}')
    assert "No changes to review." in out
    assert "Review these working-tree changes" in adapter.prompts[0]
    assert "+added line" in adapter.prompts[0] and "Focus on: security" in adapter.prompts[0]


async def test_memory_shows_and_edits_instructions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_editor(command: list[str], path: Path) -> int:
        path.write_text("# ARCHITECT.md\nAlways run the tests.\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(commands_module, "run_editor", fake_editor)
    out, _, repl = await run(tmp_path, ["/memory", "/memory edit", "/memory"])
    assert "none (/init drafts ARCHITECT.md)" in out
    assert "Reloaded instructions" in out
    assert "Always run the tests." in repl.conductor.instructions
    assert "Tip: no ARCHITECT.md here yet" in out


async def test_doctor_reports_every_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_ollama(*args: object, **kwargs: object) -> None:
        return None

    async def providers(repl: Repl) -> list[tuple[str, str, bool]]:
        return [("Claude (claude CLI)", "logged in", True),
                ("not set up", "openai, groq (/login <provider>)", True)]

    monkeypatch.setattr(commands_module, "ollama_version", no_ollama)
    monkeypatch.setattr(commands_module, "provider_status", providers)
    out, adapter, _ = await run(tmp_path, ["/doctor"])
    for label in ("Claude (claude CLI)", "not set up", "node", "pi bridge", "ollama",
                  "workspace", "profile", "disk"):
        assert label in out, label
    assert "not running at localhost:11434" in out
    assert adapter.prompts == []


async def test_ollama_probe_is_bounded_and_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "localhost" and request.url.path == "/api/version"
        return httpx.Response(200, json={"version": "0.9.1"})

    real = httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.AsyncClient:
        assert kwargs["timeout"] == 1.0
        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    assert await commands_module.ollama_version() == "0.9.1"

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(refused), **kw))
    assert await commands_module.ollama_version() is None


# -- first run ----------------------------------------------------------------------------


async def test_first_run_uses_the_providers_sign_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from architect.providers import signin
    from architect.providers.onboarding import ProviderError

    calls_made: list[Path] = []

    async def first_run(ui: object, workspace: Path) -> Path:
        calls_made.append(workspace)
        return fake_profile(tmp_path)  # writes <tmp>/w/session-profile.json

    monkeypatch.setattr(signin, "first_run", first_run)
    app = await boot(AppOptions(cwd=tmp_path, workspace=tmp_path / "w"), RecordingUI(),
                     adapter=Scripted())
    try:
        assert app.profile_path == tmp_path / "w" / SESSION_PROFILE
    finally:
        app.close()
    assert calls_made == [tmp_path / "w"]

    async def cancelled(ui: object, workspace: Path) -> Path:
        raise ProviderError("setup_cancelled")

    monkeypatch.setattr(signin, "first_run", cancelled)
    with pytest.raises(BootError) as failure:
        await boot(AppOptions(cwd=tmp_path, workspace=tmp_path / "empty"), RecordingUI())
    assert failure.value.code == "setup_cancelled"


async def _first_run_naming_a_model(ui: object, workspace: Path) -> Path:
    """What the Nebius role pack does: the first run writes the profile and names the
    conversation model in config.toml, after the config was read."""
    path = fake_profile(workspace.parent)  # writes <parent>/w, this workspace
    payload = json.loads(path.read_text(encoding="utf-8"))
    # A second priced model (a fake adapter needs a price entry), not first in the ladder.
    payload["models"]["gpt-4o-mini"] = {"provider": "fake"}
    payload["compile"]["model_ladder"] = ["fake", "gpt-4o-mini"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    (workspace / "config.toml").write_text('model = "gpt-4o-mini"\n', encoding="utf-8")
    return path


@pytest.mark.parametrize("preloaded", [False, True], ids=["boot_reads", "caller_reads"])
async def test_a_first_run_that_names_the_conversation_model_is_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preloaded: bool
) -> None:
    """Whoever read the config first: boot, or its caller (``repl.main`` and the desktop
    bridge read it before boot)."""
    from architect.providers import signin

    monkeypatch.setattr(signin, "first_run", _first_run_naming_a_model)
    workspace = tmp_path / "w"
    config = load_config(agent_workspace(tmp_path, workspace), tmp_path) if preloaded else None
    app = await boot(AppOptions(cwd=tmp_path, workspace=workspace, config=config),
                     RecordingUI())
    try:
        assert app.conductor.adapter.model == "gpt-4o-mini"  # not the first ladder model
        assert app.config.text("model") == "gpt-4o-mini"
    finally:
        app.close()


def test_the_cli_talks_to_the_model_its_first_run_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from architect.agent import repl as repl_module
    from architect.providers import signin

    monkeypatch.setattr(signin, "first_run", _first_run_naming_a_model)
    models: list[str] = []
    real_boot = repl_module.boot

    async def recording_boot(options: AppOptions, ui: Any, **kwargs: Any) -> Any:
        app = await real_boot(options, ui, **kwargs)
        models.append(app.conductor.adapter.model)
        return app

    monkeypatch.setattr(repl_module, "boot", recording_boot)
    monkeypatch.chdir(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    code = repl_module.main(["-p", "hi", "--workspace", "w", "--json"], stdout=out, stderr=err)
    assert code == 0, err.getvalue()
    assert models == ["gpt-4o-mini"]


async def test_first_run_falls_back_without_the_providers_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(sys.modules, "architect.providers.signin", None)
    assert await first_run(RecordingUI(), tmp_path) is None


def test_print_mode_accepts_plan_permission(tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    from architect.agent.repl import main

    fake_profile(tmp_path)
    monkeypatch.chdir(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    code = main(["-p", "plan it", "--permission", "plan", "--workspace", "w", "--json"],
                adapter=Scripted('{"text": "the plan"}'), stdout=out, stderr=err)
    assert code == 0, err.getvalue()
    assert json.loads(out.getvalue())["answer"] == "the plan"


# -- integration with the providers and MCP packages --------------------------------------


async def test_read_tools_never_show_workspace_secrets(tmp_path: Path) -> None:
    repl, _, _ = await start(tmp_path, [])
    workspace = tmp_path / "w"  # the working directory contains the workspace
    (workspace / "credentials.json").write_text('{"openai": "sk-SECRET"}', encoding="utf-8")
    (workspace / "model-prices.json").write_text('{"SECRET": 1}', encoding="utf-8")
    (workspace / "notes.json").write_text('{"SECRET": "visible"}', encoding="utf-8")
    tools = {tool.name: tool for tool in repl.conductor.tools}
    context = repl.conductor.context
    try:
        for name in ("w/credentials.json", "w/model-prices.json"):
            with pytest.raises(ToolError) as failure:
                await tools["read_file"].run({"path": name}, context)
            assert failure.value.code == "denied_path"
        listing = await tools["list_dir"].run({"path": "w"}, context)
        names = [entry["name"] for entry in listing["entries"]]  # type: ignore[index, union-attr]
        assert "notes.json" in names and "credentials.json" not in names
        assert "model-prices.json" not in names
        found = await tools["glob"].run({"pattern": "*.json"}, context)
        assert "w/notes.json" in found["matches"]  # type: ignore[index, operator]
        assert not any("credentials" in m or "prices" in m
                       for m in found["matches"])  # type: ignore[index, union-attr]
        hits = await tools["grep"].run({"pattern": "SECRET"}, context)
        assert hits["matches"] and all(  # type: ignore[index]
            m.startswith("w/notes.json") for m in hits["matches"])  # type: ignore[index, union-attr]
    finally:
        repl.app.close()


async def test_secrets_are_asked_hidden_where_possible(tmp_path: Path) -> None:
    console = ScriptedConsole(["sk-typed"])
    frontend = LineFrontend(console)
    assert await frontend.ask_secret("API key? ") == "sk-typed"
    assert "shows what you type" in console.transcript()  # a pipe cannot hide input
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from architect.agent.repl import RichFrontend

    with create_pipe_input() as pipe:
        rich = RichFrontend(io.StringIO(), pt_input=pipe, pt_output=DummyOutput())
        pipe.send_text("sk-hidden\r")
        assert await rich.ask_secret("API key? ") == "sk-hidden"
        assert list(rich._secrets.history.get_strings()) == []
        pipe.send_text("\x03")
        assert await rich.ask_secret("API key? ") is None


async def test_unpriced_models_show_cost_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from architect.agent.app import App

    monkeypatch.setattr(App, "unpriced", lambda self: True)
    out, _, repl = await run(tmp_path, ["/cost"])
    assert "cost unknown" in out and "time is the budget" in out
    assert "cost unknown" in repl.toolbar(160, repl.frontend.renderer.glyphs)
    assert "cost unknown" in repl.spend_line()


async def test_mcp_tools_are_bridged_and_servers_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import architect.mcp as mcp
    from architect.agent.repl import _interactive
    from architect.agent.tools import Tool

    async def echo(args: dict[str, Any], ctx: ToolContext) -> Any:
        return {"echo": args}

    bridged = (Tool("mcp__files__echo", "Echo.", {"type": "object"}, echo),)
    closed: list[tuple[Path, Path]] = []

    async def fake_tools(workspace: Path, cwd: Path) -> tuple[Tool, ...]:
        return bridged

    async def fake_close(workspace: Path | None = None, cwd: Path | None = None) -> None:
        closed.append((workspace, cwd))  # type: ignore[arg-type]

    monkeypatch.setattr(mcp, "mcp_tools", fake_tools)
    monkeypatch.setattr(mcp, "close_mcp", fake_close)
    fake_profile(tmp_path)
    console = ScriptedConsole(["/status"])
    frontend = LineFrontend(console)
    code = await _interactive(AppOptions(cwd=tmp_path, workspace=tmp_path / "w"), frontend,
                              resume=None, continue_latest=False, adapter=Scripted())
    assert code == 0
    assert "1 tool(s) from MCP servers" in console.transcript()
    assert closed and closed[0][1] == tmp_path


async def test_doctor_lists_ready_providers_from_the_status_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from architect.providers import status as status_module
    from architect.providers.status import ProviderStatus

    async def statuses(workspace: Path, **kwargs: object) -> tuple[ProviderStatus, ...]:
        assert kwargs.get("probe") is False  # no local probes beyond Ollama's own row
        return (ProviderStatus("claude", "cli_found", True, "claude found"),
                ProviderStatus("openai", "no_key", False, "no key (/login openai)"),
                ProviderStatus("ollama", "not_checked", False, "not checked"))

    monkeypatch.setattr(status_module, "all_statuses", statuses)
    repl, _, _ = await start(tmp_path, [])
    try:
        rows = await commands_module.provider_status(repl)
    finally:
        repl.app.close()
    labels = [label for label, _, _ in rows]
    assert rows[0][1:] == ("claude found", True)
    assert rows[-1] == ("not set up", "openai (/login <provider>)", True)
    assert not any("ollama" in label.lower() for label in labels)


async def test_ctrl_g_edits_the_prompt_in_the_configured_editor(tmp_path: Path) -> None:
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from architect.agent.repl import RichFrontend

    script = tmp_path / "fake_editor.py"
    script.write_text(
        "import sys\nfrom pathlib import Path\np = Path(sys.argv[1])\n"
        "p.write_text(p.read_text(encoding='utf-8').upper(), encoding='utf-8')\n",
        encoding="utf-8")
    fake_profile(tmp_path)
    (tmp_path / "w" / "config.toml").write_text(
        f"editor = '\"{sys.executable}\" \"{script}\"'\n", encoding="utf-8")
    with create_pipe_input() as pipe:
        frontend = RichFrontend(io.StringIO(), pt_input=pipe, pt_output=DummyOutput())
        app = await boot(AppOptions(cwd=tmp_path, workspace=tmp_path / "w"), frontend,
                         adapter=Scripted())
        Repl(app, frontend)
        try:
            pipe.send_text("draft message\x07")  # Ctrl+G
            reader = asyncio.create_task(frontend.read_line())
            for _ in range(400):  # the editor runs in a thread; wait for its result
                if frontend.session.default_buffer.text == "DRAFT MESSAGE":
                    break
                await asyncio.sleep(0.05)
            pipe.send_text("\r")
            assert await asyncio.wait_for(reader, 20) == "DRAFT MESSAGE"
        finally:
            app.close()
