"""The desktop bridge's structured methods, quiet input, stdout capture, the terminal-free
login runner, first-run setup before boot and the live Forge observer messages.

Offline: the scripted conductor model, the fake session profile, a fake provider host
(``httpx.MockTransport``, fake PATH) and real Python child processes only.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
from image_support import png_bytes
from session_support import make_spec
from test_agent_conductor import Scripted
from test_desktop_bridge import Wire, asked, call, idle, started

from architect.compiler import _STAGE_OBSERVERS
from architect.compiler.forge_tools import _RESULT_OBSERVERS, notify_result_observers
from architect.desktop.bridge import Bridge, EventStdout
from architect.providers.host import Host
from architect.session.bootstrap import SESSION_PROFILE

KEY = "sk-desktop-SECRET-0123456789"
OPENAI_MODELS = {"data": [{"id": "gpt-4o-mini"}, {"id": "gpt-4.1"}]}


def make_host(tmp_path: Path, *, which: dict[str, str] | None = None,
              env: dict[str, str] | None = None, routes: dict[str, Any] | None = None,
              runner: Callable[[Sequence[str]], int] | None = None) -> Host:
    table = routes or {}

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for prefix, answer in table.items():
            if url.startswith(prefix):
                if isinstance(answer, int):
                    return httpx.Response(answer, json={})
                return httpx.Response(200, json=answer)
        raise httpx.ConnectError("refused", request=request)

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    found = which or {}
    return Host(which=lambda name: found.get(name), env=env or {}, home=home,
                transport=httpx.MockTransport(handle), runner=runner or (lambda argv: 99))


def ok(wire: Wire, request_id: int) -> Any:
    line = wire.result(request_id)
    assert "result" in line, line
    return line["result"]


def error(wire: Wire, request_id: int) -> str:
    line = wire.result(request_id)
    assert "error" in line, line
    return str(line["error"]["code"])


def forge_messages(wire: Wire, kind: str | None = None) -> list[dict[str, Any]]:
    found = [line["data"] for line in wire.lines if line.get("type") == "forge"]
    return [d for d in found if kind is None or d["kind"] == kind]


async def answering(bridge: Bridge, answers: list[str | None]) -> None:
    """Answer each ``ask`` as it arrives, in order."""
    for value in answers:
        await asked(bridge.frontend)
        bridge.frontend.answer(bridge.frontend.pending()[0], value)
        await asyncio.sleep(0.01)


async def install(bridge: Bridge, name: str) -> None:
    assert bridge.app is not None
    services = bridge.app.services
    compiled = await services.compiler.compile(make_spec(lifecycle="install"), approved=True)
    assert compiled.artifact is not None and compiled.artifact.licensed
    services.registry.install(name, compiled.artifact)


# -- input -------------------------------------------------------------------------------


async def test_quiet_input_is_marked_and_queued_input_sends_no_user_event(
    tmp_path: Path,
) -> None:
    bridge, wire = await started(tmp_path, '{"text": "one"}', '{"text": "two"}')
    try:
        await call(bridge, 2, "send", text="first")
        await call(bridge, 3, "send", text="/status", quiet=True)
        await call(bridge, 4, "send", text="second")
        await idle(bridge)
        turns = [line for line in wire.lines if line.get("type") == "turn_started"]
        assert [(t["text"], t["quiet"]) for t in turns] == [
            ("first", False), ("/status", True), ("second", False)]
        finished = [line for line in wire.lines if line.get("type") == "turn_finished"]
        assert [t["quiet"] for t in finished] == [False, True, False]
        assert not wire.events("user")
        assert bridge._quiet_queued == {}
    finally:
        await bridge.close()


def test_event_stdout_turns_lines_into_output_events() -> None:
    sent: list[dict[str, Any]] = []
    stream = EventStdout(sent.append)
    print("bench: 3 rows", file=stream)
    stream.write("partial")
    assert [m["event"]["text"] for m in sent] == ["bench: 3 rows"]
    stream.flush()
    assert sent[-1]["event"] == {"kind": "output", "text": "partial"}
    assert not stream.isatty() and stream.encoding == "utf-8"


def test_prints_while_serving_become_output_events(tmp_path: Path) -> None:
    lines = '{"id":1,"method":"ping"}\n{"id":2,"method":"shutdown"}\n'
    code = (
        "from architect.desktop import bridge\n"
        "async def ping(self):\n"
        "    print('from a command')\n"
        "    return {'ok': True}\n"
        "bridge.Bridge.m_ping = ping\n"
        "raise SystemExit(bridge.main())\n"
    )
    done = subprocess.run([sys.executable, "-c", code], input=lines, capture_output=True,
                          text=True, timeout=60, cwd=tmp_path)
    out = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
    outputs = [m["event"] for m in out if m.get("type") == "event"]
    assert {"kind": "output", "text": "from a command"} in outputs
    assert [m for m in out if m.get("id") == 1][0]["result"] == {"ok": True}


def test_the_login_runner_needs_no_terminal_and_reports_links() -> None:
    sent: list[dict[str, Any]] = []
    bridge = Bridge(lambda text: sent.append(json.loads(text)))
    script = ("import sys; print('Open https://auth.example.com/device?code=AB12.'); "
              "print('waiting'); sys.stdout.flush(); data = sys.stdin.read(); "
              "raise SystemExit(0 if data == '' else 5)")
    code = bridge.run_login([sys.executable, "-c", script])
    assert code == 0  # stdin is empty: nothing can wait on a terminal
    events = [m["event"] for m in sent if m.get("type") == "event"]
    links = [e for e in events if e["kind"] == "link"]
    assert links == [{"kind": "link", "url": "https://auth.example.com/device?code=AB12",
                      "text": "Finish signing in in your browser"}]
    assert any(e["kind"] == "output" and e["text"] == "waiting" for e in events)
    assert bridge.run_login(["definitely-not-a-command-xyz"]) == 127


async def test_boot_hands_the_providers_host_to_the_commands(tmp_path: Path) -> None:
    bridge, _ = await started(tmp_path)
    try:
        assert bridge.repl is not None
        assert bridge.repl.conductor.extras["providers_host"] is bridge.host
        assert bridge.host.runner == bridge.run_login
    finally:
        await bridge.close()


# -- first run before boot ---------------------------------------------------------------


async def test_first_run_as_structured_steps_then_boot_finds_the_profile(
    tmp_path: Path,
) -> None:
    wire = Wire()
    bridge = Bridge(wire.write, adapter=Scripted())
    bridge.host = make_host(tmp_path, routes={"https://api.openai.com/v1/models": OPENAI_MODELS})
    ws = tmp_path / "w"
    where = {"workspace": str(ws), "cwd": str(tmp_path)}
    await call(bridge, 1, "onboarding", probe=False, **where)
    first = ok(wire, 1)
    assert first["profile_exists"] is False and first["workspace"] == str(ws.resolve())
    ids = [c["id"] for c in first["choices"]]
    assert "openai" in ids and "claude" in ids
    assert not any(c["recommended"] for c in first["choices"])  # nothing is ready

    helper = asyncio.create_task(answering(bridge, [KEY]))
    await call(bridge, 2, "sign_in", provider="openai", **where)
    await helper
    signed = ok(wire, 2)
    assert signed["ok"] is True and signed["code"] is None
    assert sorted(m["id"] for m in signed["models"]) == ["gpt-4.1", "gpt-4o-mini"]
    asks = wire.asks()
    assert asks and asks[0]["kind"] == "secret"
    assert KEY not in json.dumps([line for line in wire.lines if line.get("type") != "ask"])

    await call(bridge, 3, "choose_model", provider="openai", model="gpt-4o-mini", **where)
    chosen = ok(wire, 3)
    assert Path(chosen["profile_path"]) == ws / SESSION_PROFILE and chosen["model"] == "gpt-4o-mini"
    await call(bridge, 4, "onboarding", probe=False, **where)
    assert ok(wire, 4)["profile_exists"] is True
    asked = len(wire.asks())
    await call(bridge, 5, "boot", **where)
    state = ok(wire, 5)
    try:
        assert state["started"] is True and state["profile_path"] == str(ws / SESSION_PROFILE)
        assert len(wire.asks()) == asked  # no first-run questions
    finally:
        await bridge.close()


async def test_each_provider_says_whether_it_holds_a_key_or_a_judge_code(tmp_path: Path) -> None:
    from architect.providers import credentials

    ws = tmp_path / "w"
    credentials.set_key("nebius", "rc_" + "Ab3-dE6_gH9jK2mN5pQ8sT1vW4yZ7aB2", ws)
    credentials.set_key("openai", KEY, ws)
    wire = Wire()
    bridge = Bridge(wire.write, adapter=Scripted())
    bridge.host = make_host(tmp_path, env={"GROQ_API_KEY": "g"})
    await call(bridge, 1, "onboarding", probe=False, workspace=str(ws), cwd=str(tmp_path))
    rows = {row["id"]: row for row in ok(wire, 1)["choices"]}
    # The words the CLI uses, and the kind as a value the app can test.
    assert (rows["nebius"]["detail"], rows["nebius"]["credential"]) == ("judge code stored",
                                                                       "judge_code")
    assert (rows["openai"]["detail"], rows["openai"]["credential"]) == ("key stored", "key")
    assert rows["groq"]["state"] == "key_env" and rows["groq"]["credential"] == "key"
    assert rows["anthropic"]["state"] == "no_key" and rows["anthropic"]["credential"] is None
    assert KEY not in json.dumps(wire.lines)


async def test_unknown_providers_and_failed_sign_ins_are_codes(tmp_path: Path) -> None:
    wire = Wire()
    bridge = Bridge(wire.write)
    bridge.host = make_host(tmp_path)
    where = {"workspace": str(tmp_path / "w"), "cwd": str(tmp_path)}
    await call(bridge, 1, "sign_in", provider="nope", **where)
    assert error(wire, 1) == "unknown_provider"
    await call(bridge, 2, "sign_in", provider="chatgpt", **where)
    assert ok(wire, 2) == {"ok": False, "code": "cli_missing", "message": None,
                           "base_url": None, "default_model": "gpt-5.6-terra", "models": []}
    await call(bridge, 3, "choose_model", provider="openai", model="bad model", **where)
    assert error(wire, 3) == "invalid_model"


# -- providers, models, config -----------------------------------------------------------


async def test_providers_and_models_as_data(tmp_path: Path) -> None:
    bridge, wire = await started(tmp_path)
    bridge.host = make_host(tmp_path, which={"claude": "/bin/claude"}, env={"GROQ_API_KEY": "g"})
    assert bridge.repl is not None
    bridge.repl.conductor.extras["providers_host"] = bridge.host
    try:
        await call(bridge, 2, "providers", probe=False)
        rows = {row["id"]: row for row in ok(wire, 2)["providers"]}
        assert rows["claude"]["ready"] is True and rows["claude"]["kind"] == "cli"
        assert rows["claude"]["recommended"] is True
        assert "claude-sonnet-5" in rows["claude"]["models"]
        assert rows["groq"]["state"] == "key_env" and rows["openai"]["ready"] is False
        assert set(rows["ollama"]) >= {"id", "label", "kind", "sign_in", "state", "ready",
                                       "detail", "models", "recommended"}
        await call(bridge, 3, "models")
        models = ok(wire, 3)
        assert models["current"] == "fake"
        fake = [m for m in models["models"] if m["name"] == "fake"]
        assert fake and fake[0]["current"] is True and fake[0]["configured"] is True
        assert all(set(m) >= {"name", "provider", "label", "price", "context_tokens"}
                   for m in models["models"])
        assert any(m["provider"] == "claude" and not m["configured"] for m in models["models"])
    finally:
        await bridge.close()


async def test_config_reads_and_writes_like_the_command(tmp_path: Path) -> None:
    bridge, wire = await started(tmp_path)
    try:
        await call(bridge, 2, "config")
        config = ok(wire, 2)
        assert config["values"]["permission"] == "ask"
        assert config["kinds"]["session_budget_usd"] == "float"
        assert config["choices"]["permission"] == ["ask", "auto-edit", "full-auto", "plan"]
        assert config["defaults"]["theme"] == "rich" and config["errors"] == []
        assert config["files"][0] == str(tmp_path / "w" / "config.toml")
        await call(bridge, 3, "set_config", key="permission", value="full-auto")
        assert ok(wire, 3)["values"]["permission"] == "full-auto"
        await call(bridge, 4, "state")
        assert ok(wire, 4)["permission"] == "full-auto"
        await call(bridge, 5, "set_config", key="session_budget_usd", value=12)
        assert ok(wire, 5)["values"]["session_budget_usd"] == 12.0
        assert bridge.repl is not None and bridge.repl.conductor.settings.session_usd == 12.0
        assert "session_budget_usd = 12" in (tmp_path / "w" / "config.toml").read_text("utf-8")
        await call(bridge, 6, "set_config", key="session_budget_usd", value=None)
        assert ok(wire, 6)["sources"]["session_budget_usd"] == "default"
        await call(bridge, 7, "set_config", key="theme", value="neon")
        assert error(wire, 7) == "invalid_value"
        await call(bridge, 8, "set_config", key="colour", value="x")
        assert error(wire, 8) == "unknown_key"
    finally:
        await bridge.close()


async def test_mcp_and_presets_as_data(tmp_path: Path) -> None:
    bridge, wire = await started(tmp_path)
    try:
        await call(bridge, 2, "mcp")
        assert ok(wire, 2) == {"servers": [], "problems": []}
        await call(bridge, 3, "presets")
        presets = {p["name"]: p for p in ok(wire, 3)["presets"]}
        assert presets["extractor"]["spawnable"] is True and presets["coder"]["spawnable"] is False
        assert presets["extractor"]["description"].startswith("Extract facts")
        assert presets["extractor"]["tunable_params"]["verifier_depth"] == {
            "values": [0, 1], "default": 1}
        assert "self_check" in presets["extractor"]["components"]
    finally:
        await bridge.close()


# -- profile and overrides ---------------------------------------------------------------


async def test_profile_edits_validate_and_restore(tmp_path: Path) -> None:
    bridge, wire = await started(tmp_path)
    profile = tmp_path / "w" / SESSION_PROFILE
    try:
        await call(bridge, 2, "profile")
        data = ok(wire, 2)
        assert data["path"] == str(profile) and data["editable"] is True
        assert data["models"]["fake"]["adapter"] == "fake"
        assert data["compile"]["max_candidates"] == 1
        assert data["forge"]["enabled"] is False and data["forge"]["max_submissions"] == 6
        assert data["roles"]["policy"] == "pinned"
        assert data["roles"]["models"]["inference"] == "fake"
        assert isinstance(data["routing"], dict) and data["routing"]
        await call(bridge, 3, "set_profile", path=["compile", "forge", "max_usd"], value=2.5)
        assert ok(wire, 3)["forge"]["max_usd"] == 2.5
        assert json.loads(profile.read_text("utf-8"))["compile"]["forge"]["max_usd"] == 2.5
        before = profile.read_text("utf-8")
        await call(bridge, 4, "set_profile", path=["compile", "max_candidates"], value=0)
        assert error(wire, 4) == "invalid_profile"
        assert profile.read_text("utf-8") == before
        await call(bridge, 5, "set_profile", path=["models", "fake", "provider", "x"], value=1)
        assert error(wire, 5) == "invalid_path"
    finally:
        await bridge.close()


async def test_build_overrides_include_the_keys_the_cli_cannot_set(tmp_path: Path) -> None:
    bridge, wire = await started(tmp_path)
    try:
        await call(bridge, 2, "set_override", key="engineer", value="preset")
        result = ok(wire, 2)
        assert result["overrides"] == {"engineer": "preset"} and "preset" in result["text"]
        await call(bridge, 3, "set_override", key="forge_usd", value=3)
        await call(bridge, 4, "set_override", key="forge_s", value="10m")
        overrides = ok(wire, 4)["overrides"]
        assert overrides["forge_usd"] == 3.0 and overrides["forge_s"] == 600.0
        await call(bridge, 5, "overrides")
        listing = ok(wire, 5)
        assert listing["lines"] and listing["override_lines"] == "\n".join(listing["lines"])
        assert "forge_usd" in listing["keys"]
        await call(bridge, 6, "set_override", key="floor_limit", value=7)
        assert error(wire, 6) == "invalid_override"
        await call(bridge, 7, "clear_overrides")
        assert ok(wire, 7)["overrides"] == {}
    finally:
        await bridge.close()


# -- agents ------------------------------------------------------------------------------


async def test_agents_rows_survive_a_damaged_entry_and_pages_show_everything(
    tmp_path: Path,
) -> None:
    bridge, wire = await started(tmp_path)
    try:
        await install(bridge, "alpha")
        await install(bridge, "beta")
        assert bridge.app is not None
        broken = bridge.app.services.registry.status("beta").directory
        (broken / "report.json").write_text("{}", encoding="utf-8")
        await call(bridge, 2, "agents")
        rows = {row["name"]: row for row in ok(wire, 2)["agents"]}
        assert rows["alpha"]["ok"] is True and rows["alpha"]["licensed"] is True
        assert rows["alpha"]["revision"] == 1 and rows["alpha"]["floor_kind"] == "held_out_tasks"
        assert rows["alpha"]["floor_score"] == 1.0 and rows["alpha"]["task"] == "Return ok"
        assert rows["beta"]["ok"] is False and rows["beta"]["error"] == "invalid_license"
        assert rows["beta"]["revision"] == 1 and rows["beta"]["spec_hash"]

        await call(bridge, 3, "agent", name="alpha")
        page = ok(wire, 3)
        assert page["licensed"] is True and page["status"] == "ok"
        assert page["spec"]["input_schema"]["type"] == "object"
        assert page["spec"]["output_schema"] == {"type": "string"}
        assert page["spec_hash"] == rows["alpha"]["spec_hash"]
        assert page["report"]["passed"] is True and "signature" not in page["report"]
        assert [h["revision"] for h in page["history"]] == [1] and page["history"][0]["current"]
        assert page["drift"] == "no_window"
        assert {"harness.py", "spec.json", "report.json"} <= {f["path"] for f in page["files"]}
        assert page["subharnesses"] == [] and page["forge"] is None
        await call(bridge, 4, "agent", name="nobody")
        assert error(wire, 4) == "unknown_agent"

        await call(bridge, 5, "run_agent", name="alpha", input={"text": "hi"})
        assert ok(wire, 5)["output"] == "ok"
        assert ok(wire, 5)["calls"] == 1 and ok(wire, 5)["run_id"]
        # A quiet turn around the run, so its line in the conversation folds.
        bracket = [m for m in wire.lines if m.get("text") == "/run alpha"]
        assert [m["type"] for m in bracket] == ["turn_started", "turn_finished"]
        assert all(m["quiet"] is True for m in bracket)
        await call(bridge, 6, "run_agent", name="nobody", input="x")
        assert error(wire, 6) == "run_failed"
        # The sentence Try it shows names the line the reason is under.
        assert wire.result(6)["error"]["message"] == (
            "The reason is in the conversation, under /run nobody."
        )
        await call(bridge, 7, "rollback", name="alpha")
        assert error(wire, 7) == "no_previous_revision"
    finally:
        await bridge.close()


# -- images and instructions -------------------------------------------------------------


async def test_images_attach_from_any_location(tmp_path: Path) -> None:
    project = tmp_path / "p"
    project.mkdir()
    bridge, wire = await started(project)
    elsewhere = tmp_path / "pictures"
    elsewhere.mkdir()
    (elsewhere / "shot.png").write_bytes(png_bytes(3, 2))
    (elsewhere / "fake.png").write_bytes(b"not an image")
    try:
        await call(bridge, 2, "attach_image", path=str(elsewhere / "shot.png"))
        pending = ok(wire, 2)["pending"]
        assert pending[0]["name"] == "shot.png" and pending[0]["media_type"] == "image/png"
        assert (pending[0]["width"], pending[0]["height"]) == (3, 2)
        await call(bridge, 3, "state")
        assert ok(wire, 3)["pending_images"] == ["shot.png"]
        await call(bridge, 4, "attach_image", path=str(elsewhere / "missing.png"))
        assert error(wire, 4) == "not_found"
        await call(bridge, 5, "attach_image", path=str(elsewhere / "fake.png"))
        assert error(wire, 5) == "invalid_image"
        await call(bridge, 6, "detach_image", index=0)
        assert ok(wire, 6)["pending"] == []
    finally:
        await bridge.close()


async def test_memory_reads_and_saves_architect_md(tmp_path: Path) -> None:
    bridge, wire = await started(tmp_path)
    try:
        await call(bridge, 2, "memory")
        assert ok(wire, 2) == {"scope": "project", "path": str(tmp_path / "ARCHITECT.md"),
                               "text": "", "exists": False, "in_use": None}
        await call(bridge, 3, "save_memory", scope="project", text="# Rules\nUse tabs.\n")
        saved = ok(wire, 3)
        assert saved["exists"] is True and saved["text"] == "# Rules\nUse tabs.\n"
        assert bridge.repl is not None and "Use tabs." in bridge.repl.conductor.instructions
        await call(bridge, 4, "save_memory", scope="global", text="Be brief.")
        assert ok(wire, 4)["path"] == str(tmp_path / "w" / "ARCHITECT.md")
        assert "Be brief." in bridge.repl.conductor.instructions
        await call(bridge, 5, "memory", scope="team")
        assert error(wire, 5) == "invalid_scope"
    finally:
        await bridge.close()


# -- live Forge data ---------------------------------------------------------------------


async def test_compile_stages_and_forge_results_reach_the_app(tmp_path: Path) -> None:
    bridge, wire = await started(tmp_path)
    try:
        assert bridge._on_stage in _STAGE_OBSERVERS
        await install(bridge, "alpha")
        stages = forge_messages(wire, "stage")
        assert [s["stage"] for s in stages] == [1, 2, 4]
        assert stages[0]["model"] == "fake"
        assert (stages[0]["model_index"], stages[0]["model_count"]) == (1, 1)
        assert stages[-1]["stages"] == [1, 2, 4] and stages[-1]["depth"] == 0
        notify_result_observers({"tool": "forge_measure", "depth": 0,
                                 "result": {"ok": True, "left": {"usd": 1.5}, "x": Path("a")}})
        tool = forge_messages(wire, "tool")[-1]
        assert tool["tool"] == "forge_measure" and tool["result"]["left"] == {"usd": 1.5}
        assert tool["result"]["x"] == "a"  # made JSON-able
    finally:
        await bridge.close()
    assert bridge._on_stage not in _STAGE_OBSERVERS
    assert bridge._on_forge_result not in _RESULT_OBSERVERS


def test_requests_that_need_boot_fail_with_a_code() -> None:
    async def run() -> list[str]:
        wire = Wire()
        bridge = Bridge(wire.write)
        codes = []
        for index, method in enumerate(("models", "config", "mcp", "profile", "overrides",
                                        "agents", "memory"), start=1):
            await call(bridge, index, method)
            codes.append(error(wire, index))
        await call(bridge, 99, "presets")
        assert ok(wire, 99)["presets"]
        return codes

    assert set(asyncio.run(run())) == {"not_started"}


async def test_capture_returns_command_output_without_adding_it(tmp_path: Path) -> None:
    from test_desktop_bridge import call, started

    bridge, wire = await started(tmp_path)
    try:
        before = len(wire.events())
        await call(bridge, 2, "capture", text="/status")
        result = wire.result(2)["result"]
        assert any(event["kind"] == "rows" for event in result["events"])
        assert len(wire.events()) == before
        await call(bridge, 3, "capture", text="hello")
        assert wire.result(3)["error"]["code"] == "not_a_command"
    finally:
        await bridge.close()


async def test_set_role_copies_the_model_entry_and_clears(tmp_path: Path) -> None:
    from test_desktop_bridge import call, started

    bridge, wire = await started(tmp_path)
    try:
        await call(bridge, 2, "set_role", role="rewrite", model="fake")
        roles = wire.result(2)["result"]["roles"]["models"]
        assert roles["rewrite"] == "fake"
        saved = json.loads((tmp_path / "w" / SESSION_PROFILE).read_text(encoding="utf-8"))
        assert saved["rewrite"]["provider"] == "fake" and saved["rewrite"]["model"] == "fake"
        await call(bridge, 3, "set_role", role="rewrite")
        assert wire.result(3)["result"]["roles"]["models"]["rewrite"] is None
        await call(bridge, 4, "set_role", role="rewrite", model="nope")
        assert wire.result(4)["error"]["code"] == "model_not_configured"
        await call(bridge, 5, "set_role", role="boss", model="fake")
        assert wire.result(5)["error"]["code"] == "invalid_role"
    finally:
        await bridge.close()


async def test_search_and_delete_saved_conversations(tmp_path: Path) -> None:
    from test_desktop_bridge import call, idle, started

    bridge, wire = await started(tmp_path, '{"text": "The answer mentions zebra."}')
    try:
        await call(bridge, 2, "send", text="tell me about animals")
        await idle(bridge)
        first = bridge.repl.conductor.session_id  # type: ignore[union-attr]
        await call(bridge, 3, "search_sessions", query="ZEBRA")
        results = wire.result(3)["result"]["results"]
        assert [r["id"] for r in results] == [first] and "zebra" in results[0]["excerpt"]
        await call(bridge, 4, "delete_session", id=first)
        assert wire.result(4)["error"]["code"] == "session_open"
        await call(bridge, 5, "send", text="/clear")
        await idle(bridge)
        await call(bridge, 6, "delete_session", id=first)
        assert wire.result(6)["result"] == {"deleted": first}
        await call(bridge, 7, "sessions")
        assert all(s["id"] != first for s in wire.result(7)["result"]["sessions"])
        await call(bridge, 8, "delete_session", id=first)
        assert wire.result(8)["error"]["code"] == "not_found"
    finally:
        await bridge.close()


async def test_fork_copies_a_conversation_whole_or_from_an_earlier_message(tmp_path: Path) -> None:
    from test_desktop_bridge import call, idle, started

    bridge, wire = await started(
        tmp_path, '{"text": "First answer."}', '{"text": "Second answer."}'
    )
    try:
        await call(bridge, 2, "send", text="first question")
        await idle(bridge)
        await call(bridge, 3, "send", text="second question")
        await idle(bridge)
        original = bridge.repl.conductor.session_id  # type: ignore[union-attr]

        await call(bridge, 4, "fork_session", id=original)
        whole = wire.result(4)["result"]
        assert whole["id"] != original and whole["title"].endswith("(fork)")
        await call(bridge, 5, "fork_session", id=original, upto=1)
        early = wire.result(5)["result"]
        assert early["messages"] < whole["messages"]

        store = bridge.repl.app.store  # type: ignore[union-attr]
        early_text = store.path(early["id"]).read_text(encoding="utf-8")
        assert "first question" in early_text and "second question" not in early_text
        assert "second question" in store.path(original).read_text(encoding="utf-8")

        await call(bridge, 6, "fork_session", id="nope00000000")
        assert wire.result(6)["error"]["code"] == "not_found"
        await call(bridge, 7, "fork_session", id=original, upto=0)
        assert wire.result(7)["error"]["code"] == "invalid_params"
    finally:
        await bridge.close()


async def test_boot_says_when_a_conversation_to_resume_is_gone(tmp_path: Path) -> None:
    from session_support import fake_profile
    from test_agent_conductor import Scripted
    from test_desktop_bridge import Wire, call

    from architect.desktop.bridge import Bridge

    fake_profile(tmp_path)
    wire = Wire()
    bridge = Bridge(wire.write, adapter=Scripted())
    try:
        await call(
            bridge, 1, "boot", cwd=str(tmp_path), workspace=str(tmp_path / "w"),
            resume="abcdef123456",
        )
        result = wire.result(1)["result"]
        assert result["resume_failed"] == "abcdef123456"
        assert result["started"] is True
    finally:
        await bridge.close()


async def test_a_message_can_steer_the_running_turn(tmp_path: Path) -> None:
    from test_desktop_bridge import call, started

    bridge, wire = await started(tmp_path)
    try:
        bridge.busy = True  # a turn is running
        await call(bridge, 2, "send", text="also check the tests", steer=True)
        assert wire.result(2)["result"] == {"steered": True, "busy": True}
        conductor = bridge.repl.conductor  # type: ignore[union-attr]
        assert conductor.steering == ["also check the tests"]
        conductor.take_steering()
        steered = [m for m in wire.lines if m.get("type") == "steered"]
        assert [m["text"] for m in steered] == ["also check the tests"]
        # Commands never steer; they wait their turn.
        await call(bridge, 3, "send", text="/model", steer=True)
        assert wire.result(3)["result"]["queued"] == 1
    finally:
        bridge.busy = False
        await bridge.close()


async def test_a_side_question_uses_the_conversation_and_adds_nothing(tmp_path: Path) -> None:
    from test_desktop_bridge import call, idle, started

    bridge, wire = await started(
        tmp_path, '{"text": "The port is 8080."}', '{"text": "Side: it is 8080, set in start()."}'
    )
    try:
        await call(bridge, 2, "send", text="what port does it use?")
        await idle(bridge)
        conductor = bridge.repl.conductor  # type: ignore[union-attr]
        before = list(conductor.transcript.messages)

        await call(bridge, 3, "side", text="where is that set?")
        side_id = wire.result(3)["result"]["id"]
        for _ in range(200):
            done = [m for m in wire.lines if m.get("type") == "side_done"]
            if done:
                break
            await asyncio.sleep(0.01)
        assert done[0]["id"] == side_id and done[0]["text"] == "Side: it is 8080, set in start()."
        assert conductor.transcript.messages == before  # nothing added back
        side_events = [m for m in wire.lines if m.get("type") == "side"]
        assert any(m["event"].get("kind") == "answer" for m in side_events)
    finally:
        await bridge.close()
