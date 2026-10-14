"""Phase 13 Package D: the MCP client, its config, the tool bridge and ``/mcp`` (offline).

Stdio servers are ``tests/fixtures/mcp_server.py`` run with this interpreter; the
streamable HTTP transport talks to an ``httpx.MockTransport``. Nothing touches the network.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_agent_conductor import Scripted, calls

from architect.agent import Conductor, RecordingUI, ToolContext, ToolError, default_tools
from architect.agent.events import ToolFinished
from architect.mcp import (
    PROTOCOL_VERSION,
    ConfigError,
    HttpTransport,
    MCPClient,
    MCPError,
    MCPManager,
    StdioTransport,
    add_server,
    close_mcp,
    load_config,
    manager_for,
    mcp_tools,
    read_toml,
    remove_server,
    set_enabled,
    tool_name,
)
from architect.mcp.protocol import content_text

SERVER = Path(__file__).resolve().parent / "fixtures" / "mcp_server.py"
PY = sys.executable


def stdio(env: dict[str, str] | None = None, **kwargs: Any) -> StdioTransport:
    return StdioTransport(PY, (str(SERVER),), env or {}, **kwargs)


def logged(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def context(tmp_path: Path, permission: str = "ask", **ui: Any) -> ToolContext:
    return ToolContext(services=None, cwd=tmp_path, ui=RecordingUI(**ui), model="fake",  # type: ignore[arg-type]
                       permission=permission)  # type: ignore[arg-type]


def write_config(workspace: Path, text: str) -> Path:
    workspace.mkdir(parents=True, exist_ok=True)
    path = workspace / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def fake_server_toml(name: str = "fake", extra: str = "") -> str:
    return (f"[mcp.servers.{name}]\ncommand = {json.dumps(PY)}\n"
            f"args = [{json.dumps(str(SERVER))}]\ntimeout_s = 20\n{extra}")


# -- stdio client ---------------------------------------------------------------------------


async def test_stdio_handshake_paginates_tools_and_calls(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    client = MCPClient(stdio({"MCP_FAKE_LOG": str(log)}), timeout_s=20)
    try:
        info = await client.connect()
        assert (info.name, info.protocol_version) == ("fake", PROTOCOL_VERSION)
        assert info.instructions == "Use echo." and "tools" in info.capabilities
        tools = await client.list_tools()
        assert [t.name for t in tools][:3] == ["echo", "media", "big"]
        assert len(tools) == 7 and tools[0].read_only and not tools[1].read_only
        assert tools[-1].input_schema == {"type": "object"}  # missing schema
        result = await client.call_tool("echo", {"text": "hello"})
        assert (result.text, result.is_error, result.truncated) == ("hello", False, False)
        await client.ping()
    finally:
        await client.close()
    methods = [m.get("method") for m in logged(log)]
    assert methods[:2] == ["initialize", "notifications/initialized"]
    assert methods.count("tools/list") == 2  # the cursor was followed
    first = logged(log)[0]
    assert first["params"]["clientInfo"]["name"] == "architect"
    assert first["params"]["protocolVersion"] == PROTOCOL_VERSION


async def test_version_negotiation_accepts_older_and_refuses_unknown(tmp_path: Path) -> None:
    older = MCPClient(stdio({"MCP_FAKE_VERSION": "2024-11-05"}), timeout_s=20)
    try:
        assert (await older.connect()).protocol_version == "2024-11-05"
    finally:
        await older.close()
    unknown = MCPClient(stdio({"MCP_FAKE_VERSION": "1999-01-01"}), timeout_s=20)
    try:
        with pytest.raises(MCPError) as caught:
            await unknown.connect()
        assert caught.value.code == "unsupported_version"
    finally:
        await unknown.close()


async def test_content_mapping_and_clipping(tmp_path: Path) -> None:
    client = MCPClient(stdio(), timeout_s=20, result_chars=200)
    try:
        await client.connect()
        media = await client.call_tool("media", {})
        assert media.text.startswith("caption\n[image image/png, about 12 bytes, not shown]")
        assert "[resource file:///notes.txt]\nnote body" in media.text
        assert "[binary resource file:///x.bin (application/zip" in media.text
        big = await client.call_tool("big", {"n": 5000})
        assert big.truncated and len(big.text) <= 200 and big.text.endswith("[clipped]")
        failed = await client.call_tool("fail", {})
        assert failed.is_error and failed.text == "the tool failed on purpose"
    finally:
        await client.close()
    text, cut = content_text([{"type": "resource_link", "uri": "file:///a", "name": "a"},
                              {"type": "weird"}, "junk"], 1000)
    assert text == "[resource link a: file:///a]\n[weird content not shown]" and not cut


async def test_timeout_withdraws_the_request_and_the_session_survives(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    client = MCPClient(stdio({"MCP_FAKE_LOG": str(log)}), timeout_s=20)
    try:
        await client.connect()
        with pytest.raises(MCPError) as caught:
            await client.call_tool("slow", {"seconds": 3}, timeout_s=0.3)
        assert caught.value.code == "timeout"
        assert (await client.call_tool("echo", {"text": "still here"})).text == "still here"
    finally:
        await client.close()
    cancelled = [m for m in logged(log) if m.get("method") == "notifications/cancelled"]
    assert cancelled and cancelled[0]["params"]["reason"] == "timeout"


async def test_cancelling_a_call_sends_cancelled(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    client = MCPClient(stdio({"MCP_FAKE_LOG": str(log)}), timeout_s=20)
    try:
        await client.connect()
        task = asyncio.create_task(client.call_tool("slow", {"seconds": 3}))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await client.call_tool("echo", {"text": "ok"})).text == "ok"
    finally:
        await client.close()
    reasons = [m["params"]["reason"] for m in logged(log)
               if m.get("method") == "notifications/cancelled"]
    assert reasons == ["cancelled by the user"]


async def test_crash_noise_and_stderr(tmp_path: Path) -> None:
    transport = stdio({"MCP_FAKE_NOISE": "1", "MCP_FAKE_STDERR": "booting fake"})
    client = MCPClient(transport, timeout_s=20)
    try:
        await client.connect()  # the non-JSON stdout line is skipped
        assert transport.noise_lines == 1
        with pytest.raises(MCPError) as caught:
            await client.call_tool("crash", {})
        assert caught.value.code == "server_exited"
        assert "exit code 3" in caught.value.detail and not client.alive
        assert "booting fake" in transport.diagnostics()
        with pytest.raises(MCPError):
            await client.ping()
    finally:
        await client.close()


async def test_oversized_message_fails_the_request(tmp_path: Path) -> None:
    client = MCPClient(stdio(max_bytes=4096), timeout_s=20)
    try:
        await client.connect()
        with pytest.raises(MCPError) as caught:
            await client.call_tool("big", {"n": 100_000})
        assert caught.value.code == "message_too_large"
        assert (await client.call_tool("echo", {"text": "after"})).text == "after"
    finally:
        await client.close()


async def test_close_stops_a_busy_server(tmp_path: Path) -> None:
    transport = stdio()
    client = MCPClient(transport, timeout_s=20)
    await client.connect()
    with pytest.raises(MCPError):
        await client.call_tool("slow", {"seconds": 30}, timeout_s=0.2)
    await client.close()  # stdin closed; the pending timer keeps it alive, so it is killed
    assert transport.process is not None and transport.process.returncode is not None


async def test_missing_command_is_a_fixed_error(tmp_path: Path) -> None:
    client = MCPClient(StdioTransport("definitely-not-a-real-mcp-server-xyz"), timeout_s=5)
    with pytest.raises(MCPError) as caught:
        await client.connect()
    assert caught.value.code == "command_not_found"
    await client.close()


# -- streamable HTTP ------------------------------------------------------------------------


class HttpServer:
    """A scripted streamable-HTTP MCP endpoint behind httpx.MockTransport."""

    def __init__(self, *, sse: bool = False, status: int | None = None) -> None:
        self.sse = sse
        self.status = status
        self.requests: list[httpx.Request] = []
        self.bodies: list[Any] = []
        self.sessions = 0
        self.expire_next = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def reply(self, payload: dict[str, Any], extra: list[dict[str, Any]] | None = None,
              headers: dict[str, str] | None = None) -> httpx.Response:
        if not self.sse:
            return httpx.Response(200, json=payload, headers=headers)
        events = [*(extra or []), payload]
        body = ": keep-alive\n\n" + "".join(
            f"event: message\ndata: {json.dumps(e)}\n\n" for e in events
        )
        return httpx.Response(200, content=body.encode(),
                              headers={"content-type": "text/event-stream", **(headers or {})})

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(200)
        if self.status is not None:
            return httpx.Response(self.status)
        message = json.loads(request.content)
        self.bodies.append(message)
        if "id" not in message:
            return httpx.Response(202)
        method, rid = message["method"], message["id"]
        if method == "initialize":
            self.sessions += 1
            return self.reply(
                {"jsonrpc": "2.0", "id": rid, "result": {
                    "protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                    "serverInfo": {"name": "web", "version": "2"}}},
                headers={"Mcp-Session-Id": f"session-{self.sessions}"},
            )
        if self.expire_next:
            self.expire_next = False
            return httpx.Response(404)
        if method == "tools/list":
            return self.reply({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": "search", "description": "Search docs.",
                 "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}}]}})
        if method == "tools/call":
            note = {"jsonrpc": "2.0", "method": "notifications/progress",
                    "params": {"progressToken": 1, "progress": 1}}
            q = message["params"]["arguments"].get("q", "")
            return self.reply({"jsonrpc": "2.0", "id": rid,
                               "result": {"content": [{"type": "text", "text": f"found {q}"}]}},
                              extra=[note])
        return self.reply({"jsonrpc": "2.0", "id": rid,
                           "error": {"code": -32601, "message": "nope"}})


@pytest.mark.parametrize("sse", [False, True])
async def test_http_json_and_sse_responses_with_session_headers(sse: bool) -> None:
    server = HttpServer(sse=sse)
    transport = HttpTransport("https://mcp.test/mcp", {"Authorization": "Bearer t"},
                              http_transport=server.transport())
    client = MCPClient(transport, timeout_s=5)
    try:
        info = await client.connect()
        assert (info.name, info.protocol_version) == ("web", "2025-03-26")
        assert [t.name for t in await client.list_tools()] == ["search"]
        result = await client.call_tool("search", {"q": "mcp"})
        assert result.text == "found mcp" and not result.is_error
    finally:
        await client.close()
    first, rest = server.requests[0], server.requests[1:]
    assert "Mcp-Session-Id" not in first.headers
    assert "text/event-stream" in first.headers["accept"]
    assert first.headers["authorization"] == "Bearer t"
    assert all(r.headers["mcp-session-id"] == "session-1" for r in rest)
    assert all(r.headers["mcp-protocol-version"] == "2025-03-26" for r in rest)
    assert server.bodies[1]["method"] == "notifications/initialized"
    assert server.requests[-1].method == "DELETE"  # the session is ended on close


async def test_http_errors_are_fixed_codes() -> None:
    client = MCPClient(HttpTransport("https://mcp.test/mcp",
                                     http_transport=HttpServer(status=401).transport()),
                       timeout_s=5)
    with pytest.raises(MCPError) as caught:
        await client.connect()
    assert caught.value.code == "unauthorized"
    await client.close()

    def truncated(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"event: message\ndata: {\"jsonrpc\": \"2.0\"}\n\n",
                              headers={"content-type": "text/event-stream"})

    client = MCPClient(HttpTransport("https://mcp.test/mcp",
                                     http_transport=httpx.MockTransport(truncated)), timeout_s=5)
    with pytest.raises(MCPError) as caught:
        await client.connect()
    assert caught.value.code == "protocol_error"
    await client.close()


async def test_manager_reinitializes_an_expired_http_session(tmp_path: Path) -> None:
    server = HttpServer()
    write_config(tmp_path / "w", '[mcp.servers.web]\nurl = "https://mcp.test/mcp"\n')
    manager = MCPManager(tmp_path / "w", tmp_path, http_transport=server.transport())
    try:
        tools = await manager.tools()
        assert [t.name for t in tools] == ["mcp__web__search"]
        server.expire_next = True
        result = await tools[0].run({"q": "again"}, context(tmp_path, "full-auto"))
        assert isinstance(result, dict) and result["content"] == "found again"
        assert server.sessions == 2
    finally:
        await manager.close()


# -- the bridge -----------------------------------------------------------------------------


def test_tool_names_are_sanitized_and_bounded() -> None:
    assert tool_name("files", "read_file") == "mcp__files__read_file"
    assert tool_name("My-Server", "Weird Name.v2!") == "mcp__my_server__weird_name_v2"
    long = tool_name("s" * 40, "t" * 80)
    assert len(long) == 64 and long.startswith("mcp__sss")
    assert long != tool_name("s" * 40, "t" * 81)  # the hash keeps long names distinct
    import re

    assert re.match(r"^[a-z0-9_]+$", long)


async def test_bridged_tools_approvals_clipping_and_errors(tmp_path: Path) -> None:
    write_config(tmp_path / "w", fake_server_toml())
    manager = MCPManager(tmp_path / "w", tmp_path, result_chars=300)
    try:
        tools = {t.name: t for t in await manager.tools()}
        assert set(tools) >= {"mcp__fake__echo", "mcp__fake__big", "mcp__fake__weird_name_v2"}
        echo = tools["mcp__fake__echo"]
        assert echo.description.startswith("[MCP server fake: echo] Echo the text back.")
        assert echo.parameters["required"] == ["text"]

        ask = context(tmp_path, approvals=["no"])
        with pytest.raises(ToolError) as caught:
            await echo.run({"text": "x"}, ask)
        assert caught.value.code == "denied_by_user"

        session = context(tmp_path, approvals=["session"])
        assert (await echo.run({"text": "hi"}, session))["content"] == "hi"  # type: ignore[index]
        assert "mcp fake.echo" in session.allowed
        assert (await echo.run({"text": "again"}, session))["content"] == "again"  # type: ignore[index]
        ui = session.ui
        assert isinstance(ui, RecordingUI) and ui.approvals == []  # asked once only

        yes = context(tmp_path, approvals=["yes"])
        big = await tools["mcp__fake__big"].run({"n": 10_000}, yes)
        assert isinstance(big, dict) and big["truncated"] is True
        assert len(str(big["content"])) <= 300

        auto = context(tmp_path, "full-auto")  # no approval prompt at all
        with pytest.raises(ToolError) as caught:
            await tools["mcp__fake__fail"].run({}, auto)
        assert caught.value.code == "mcp_tool_error"
        assert caught.value.detail == "the tool failed on purpose"

        plan = context(tmp_path, "plan", approvals=["yes"])
        with pytest.raises(ToolError) as caught:
            await tools["mcp__fake__fail"].run({}, plan)
        assert caught.value.code == "plan_mode_read_only"
        assert (await echo.run({"text": "ro"}, plan))["content"] == "ro"  # type: ignore[index]
    finally:
        await manager.close()


async def test_a_crashed_server_restarts_on_the_next_call(tmp_path: Path) -> None:
    write_config(tmp_path / "w", fake_server_toml())
    manager = MCPManager(tmp_path / "w", tmp_path)
    auto = context(tmp_path, "full-auto")
    try:
        tools = {t.name: t for t in await manager.tools()}
        with pytest.raises(ToolError) as caught:
            await tools["mcp__fake__crash"].run({}, auto)
        assert caught.value.code == "mcp_server_exited"
        assert manager.servers["fake"].status == "error"
        result = await tools["mcp__fake__echo"].run({"text": "back"}, auto)
        assert isinstance(result, dict) and result["content"] == "back"
        assert manager.servers["fake"].status == "ready"
    finally:
        await manager.close()


async def test_call_timeout_is_a_tool_error(tmp_path: Path) -> None:
    write_config(tmp_path / "w", fake_server_toml(extra="").replace("timeout_s = 20",
                                                                     "timeout_s = 0.5"))
    manager = MCPManager(tmp_path / "w", tmp_path)
    try:
        tools = {t.name: t for t in await manager.tools()}
        with pytest.raises(ToolError) as caught:
            await tools["mcp__fake__slow"].run({"seconds": 5}, context(tmp_path, "full-auto"))
        assert caught.value.code == "mcp_timeout"
    finally:
        await manager.close()


async def test_broken_servers_never_block_the_others(tmp_path: Path) -> None:
    write_config(tmp_path / "w", fake_server_toml() + (
        '\n[mcp.servers.ghost]\ncommand = "definitely-not-a-real-mcp-server-xyz"\n'
        '\n[mcp.servers.off]\ncommand = "x"\nenabled = false\n'))
    try:
        tools = await mcp_tools(tmp_path / "w", tmp_path)
        assert tools and all(t.name.startswith("mcp__fake__") for t in tools)
        manager = manager_for(tmp_path / "w", tmp_path)
        rows = {row["name"]: row for row in manager.rows()}
        assert rows["ghost"]["status"] == "error"
        assert str(rows["ghost"]["error"]).startswith("command_not_found")
        assert rows["off"]["status"] == "disabled" and rows["fake"]["tools"] == 7
    finally:
        await close_mcp()
    assert manager.servers["fake"].client is None


async def test_conductor_calls_an_mcp_tool_end_to_end(tmp_path: Path) -> None:
    from session_support import fake_profile

    from architect.profiles import build_services, load_profile

    profile = load_profile(fake_profile(tmp_path))
    services = build_services(profile, profile.workspace)
    write_config(tmp_path / "w", fake_server_toml())
    manager = MCPManager(tmp_path / "w", tmp_path)
    try:
        bridged = await manager.tools()
        ui = RecordingUI(approvals=["yes"])
        adapter = Scripted(calls(("mcp__fake__echo", {"text": "via conductor"})),
                           '{"text": "done"}')
        conductor = Conductor(services, adapter, tmp_path, ui,  # type: ignore[arg-type]
                              tools=(*default_tools(), *bridged))
        assert "mcp__fake__echo" in conductor.system_prompt()
        result = await conductor.turn("echo something")
        assert result.text == "done"
        finished = [e for e in ui.events if isinstance(e, ToolFinished)]
        assert finished and finished[0].ok and "via conductor" in finished[0].summary
    finally:
        await manager.close()
        services.close()


# -- configuration --------------------------------------------------------------------------


def test_config_layers_merge_with_the_project_winning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_TEST_TOKEN", "sekrit")
    monkeypatch.delenv("MCP_TEST_MISSING", raising=False)
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "shared": {"command": "npx", "args": ["-y", "pkg"], "env": {"A": "${MCP_TEST_TOKEN}"}},
        "web": {"type": "http", "url": "https://x.test/mcp",
                "headers": {"Authorization": "Bearer ${MCP_TEST_TOKEN}"}},
        "legacy": {"type": "sse", "url": "https://x.test/sse"},
        "off": {"command": "y", "disabled": True, "timeout": 5000},
    }}), encoding="utf-8")
    write_config(tmp_path / "w", 'model = "keep"\n[mcp.servers.shared]\ntimeout_s = 5\n'
                 '[mcp.servers.mine]\ncommand = "tool"\ncwd = "sub"\n'
                 'args = ["${MCP_TEST_MISSING:-fallback}"]\n')
    project = tmp_path / ".architect"
    project.mkdir()
    (project / "config.toml").write_text(
        '[mcp.servers.shared]\nenabled = false\n[mcp.servers.bad]\nurl = "ftp://nope"\n'
        '[mcp.servers.both]\ncommand = "a"\nurl = "https://b.test"\n', encoding="utf-8")
    config = load_config(tmp_path / "w", tmp_path)
    shared = config.servers["shared"]
    assert (shared.command, shared.args, shared.env) == ("npx", ("-y", "pkg"), {"A": "sekrit"})
    assert shared.timeout_s == 5 and shared.enabled is False
    assert len(shared.sources) == 3 and shared.sources[-1] == project / "config.toml"
    web = config.servers["web"]
    assert web.transport == "http" and web.headers["Authorization"] == "Bearer sekrit"
    assert config.servers["off"].enabled is False and config.servers["off"].timeout_s == 5
    mine = config.servers["mine"]
    assert mine.args == ("fallback",) and mine.cwd == (tmp_path / "sub").resolve()
    assert "legacy" not in config.servers and "bad" not in config.servers
    problems = " | ".join(config.problems)
    assert "legacy HTTP+SSE" in problems and "bad: url must start" in problems
    assert "both: set exactly one of command or url" in problems
    assert read_toml(tmp_path / "w" / "config.toml")["model"] == "keep"
    assert read_toml(tmp_path / "missing.toml") == {}


def test_broken_config_files_are_problems_not_crashes(tmp_path: Path) -> None:
    write_config(tmp_path / "w", "[mcp.servers.x\n")
    (tmp_path / ".mcp.json").write_text("{not json", encoding="utf-8")
    config = load_config(tmp_path / "w", tmp_path)
    assert config.servers == {} and len(config.problems) == 2
    with pytest.raises(ConfigError):
        read_toml(tmp_path / "w" / "config.toml")


def test_config_edits_keep_other_content(tmp_path: Path) -> None:
    original = ('# my settings\nmodel = "claude-sonnet"\n\n[ui]\ntheme = "dark"\n\n'
                '[mcp.servers.old]\ncommand = "old"\n[mcp.servers.old.env]\nK = "v"\n\n'
                '[other]\nkeep = 1\n')
    path = write_config(tmp_path / "w", original)
    add_server(path, "files", command="npx", args=("-y", "server files"), env={"DEBUG": "1"},
               timeout_s=30)
    add_server(path, "web", url="https://x.test/mcp", headers={"X-Key": "abc"})
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["model"] == "claude-sonnet" and data["ui"] == {"theme": "dark"}
    assert data["other"] == {"keep": 1}
    assert data["mcp"]["servers"]["files"] == {"command": "npx", "args": ["-y", "server files"],
                                              "env": {"DEBUG": "1"}, "timeout_s": 30,
                                              "enabled": True}
    assert data["mcp"]["servers"]["web"]["headers"] == {"X-Key": "abc"}
    assert path.read_text(encoding="utf-8").startswith("# my settings\n")

    add_server(path, "old", command="new")  # replaces the table and its sub-table
    assert read_toml(path)["mcp"]["servers"]["old"] == {"command": "new", "args": [],
                                                        "enabled": True}
    set_enabled(path, "old", False)
    set_enabled(path, "files", False)
    set_enabled(path, "files", True)
    servers = read_toml(path)["mcp"]["servers"]
    assert servers["old"]["enabled"] is False and servers["files"]["enabled"] is True
    set_enabled(path, "fromjson", False)  # a server defined elsewhere gets an override table
    assert read_toml(path)["mcp"]["servers"]["fromjson"] == {"enabled": False}

    assert remove_server(path, "old") and not remove_server(path, "old")
    data = read_toml(path)
    assert "old" not in data["mcp"]["servers"] and data["other"] == {"keep": 1}

    with pytest.raises(ConfigError) as caught:
        add_server(path, "bad name", command="x")
    assert caught.value.code == "invalid_name"
    with pytest.raises(ConfigError):
        add_server(path, "x", url="ftp://nope")
    inline = write_config(tmp_path / "v", '[mcp]\nservers = { inl = { command = "a" } }\n')
    with pytest.raises(ConfigError) as caught:
        remove_server(inline, "inl") or set_enabled(inline, "inl", False)
    assert caught.value.code == "config_edit_failed"
    assert "inl" in inline.read_text(encoding="utf-8")  # unchanged


# -- /mcp ---------------------------------------------------------------------------------


async def test_mcp_commands(tmp_path: Path) -> None:
    from test_agent_repl import start

    command = f'"{PY}" "{SERVER}"'
    lines = [
        "/mcp",
        f"/mcp add fake --timeout 20 {command}",
        "/mcp",
        "/mcp tools fake",
        "/mcp disable fake",
        "/mcp enable fake",
        "/mcp restart fake",
        "/mcp add web --url https://mcp.test/mcp --header 'X-Key: abc'",
        "/mcp add",
        "/mcp frobnicate",
        "/mcp remove nothing",
        "/mcp remove fake",
    ]
    repl, console, _ = await start(tmp_path, lines)
    web = HttpServer()  # the REPL's manager reaches HTTP servers only through the mock
    repl.conductor.extras["mcp"] = MCPManager(tmp_path / "w", tmp_path,
                                              http_transport=web.transport())
    names: Callable[[], list[str]] = lambda: [  # noqa: E731
        t.name for t in repl.conductor.tools if t.name.startswith("mcp__")]
    snapshots: list[list[str]] = []
    original = repl.handle

    async def handle(line: str) -> None:
        await original(line)
        snapshots.append(names())

    repl.handle = handle  # type: ignore[method-assign]
    try:
        assert await repl.run() == 0
    finally:
        repl.app.close()
        manager = repl.conductor.extras["mcp"]
        assert isinstance(manager, MCPManager)
        await manager.close()
    out = console.transcript()
    assert "No MCP servers." in out
    config = read_toml(tmp_path / "w" / "config.toml")["mcp"]["servers"]
    assert "fake" not in config and config["web"]["headers"] == {"X-Key": "abc"}
    assert "Started fake: 7 tools." in out and "ready, 7 tools" in out
    assert "mcp__fake__echo" in out  # the /mcp tools listing
    assert snapshots[1] and "mcp__fake__echo" in snapshots[1]  # bound after /mcp add
    assert snapshots[4] == []  # disabled
    assert "Enabled fake: 7 tools." in out and "mcp__fake__echo" in snapshots[5]
    assert "Restarted fake: 7 tools." in out
    assert "Started web: 1 tools." in out and web.sessions == 1
    assert "Usage: /mcp add" in out and "Usage: /mcp [tools" in out
    assert "No MCP server nothing." in out
    assert "Removed MCP server fake." in out and snapshots[-1] == ["mcp__web__search"]
    assert "/mcp" in __import__("architect.agent.commands", fromlist=["COMMANDS"]).COMMANDS
