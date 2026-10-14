"""MCP servers as conductor tools.

``MCPManager`` owns the configured servers of one workspace and working directory. A
server starts the first time its tools are needed (``tools()``) or called, restarts on the
next call after it crashed, and stops on ``close``. Each server tool becomes a conductor
``Tool`` named ``mcp__<server>__<tool>`` whose run calls ``tools/call``:

- permission ``ask``: every call is approved first (``mcp <server>.<tool>``) unless the user
  allowed it for the session;
- permission ``plan``: only tools the server marks read-only run (approved like ``ask``);
- ``auto-edit`` and ``full-auto``: calls run without asking.

Results are clipped; an ``isError`` result is a ``ToolError`` the model can read.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import JsonValue

from architect.agent.tools import Tool, ToolContext, ToolError

from .client import CallResult, MCPClient, ServerInfo, ToolInfo
from .config import McpConfig, ServerConfig, load_config
from .protocol import MCPError, clip
from .transport import HttpTransport, StdioTransport, Transport

__all__ = [
    "PREFIX",
    "MCPManager",
    "ServerState",
    "close_mcp",
    "close_mcp_now",
    "manager_for",
    "mcp_tools",
    "tool_name",
]

PREFIX = "mcp__"
NAME_LIMIT = 64  # the strictest provider limit on function names
STARTUP_CAP_S = 30.0
RESULT_CHARS = 20_000
ERROR_CHARS = 2_000
SUMMARY_CHARS = 600
Status = Literal["stopped", "starting", "ready", "error", "disabled"]
TransportFactory = Callable[[ServerConfig], Transport]


def _clean(text: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9_]", "_", text.lower())).strip("_") or "x"


def tool_name(server: str, tool: str) -> str:
    """``mcp__<server>__<tool>`` matching ``^[a-z0-9_]+$``, at most 64 characters."""
    name = f"{PREFIX}{_clean(server)}__{_clean(tool)}"
    if len(name) <= NAME_LIMIT:
        return name
    digest = hashlib.sha1(f"{server}\0{tool}".encode()).hexdigest()[:8]
    return name[: NAME_LIMIT - 9].rstrip("_") + "_" + digest


@dataclass
class ServerState:
    config: ServerConfig
    status: Status = "stopped"
    error: str = ""
    client: MCPClient | None = None
    info: ServerInfo | None = None
    tools: list[ToolInfo] = field(default_factory=list)
    stale: bool = False  # the server said its tool list changed
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def diagnostics(self) -> str:
        if self.client is None:
            return ""
        return self.client.transport.diagnostics()


class MCPManager:
    def __init__(
        self,
        workspace: Path,
        cwd: Path,
        *,
        http_transport: httpx.AsyncBaseTransport | None = None,
        transport_factory: TransportFactory | None = None,
        result_chars: int = RESULT_CHARS,
    ) -> None:
        self.workspace = workspace
        self.cwd = cwd
        self.http_transport = http_transport
        self.transport_factory = transport_factory
        self.result_chars = result_chars
        self.servers: dict[str, ServerState] = {}
        self.problems: list[str] = []
        self._configured: set[str] = set()
        self.reload()

    # -- configuration ---------------------------------------------------------------

    def reload(self) -> list[str]:
        """Re-read the config; returns servers whose definition changed or vanished.

        Changed and removed servers keep their running client until ``apply`` stops them.
        """
        config: McpConfig = load_config(self.workspace, self.cwd)
        self.problems = config.problems
        self._configured = set(config.servers)
        changed: list[str] = []
        for name in list(self.servers):
            if name not in config.servers:
                changed.append(name)
        for name, server in config.servers.items():
            state = self.servers.get(name)
            if state is None:
                status: Status = "stopped" if server.enabled else "disabled"
                self.servers[name] = ServerState(server, status)
                continue
            if _definition(state.config) != _definition(server):
                changed.append(name)
            state.config = server
            if not server.enabled:
                changed.append(name)
            elif state.status == "disabled":
                state.status = "stopped"
        return sorted(set(changed))

    async def apply(self) -> None:
        """Reload the config and stop servers that changed, were disabled or removed."""
        for name in self.reload():
            state = self.servers.get(name)
            if state is None:
                continue
            await self.stop(name)
            if name not in self._configured:
                self.servers.pop(name, None)

    # -- lifecycle --------------------------------------------------------------------

    def _transport(self, server: ServerConfig) -> Transport:
        if self.transport_factory is not None:
            return self.transport_factory(server)
        if server.url:
            return HttpTransport(server.url, server.headers, timeout_s=server.timeout_s,
                                 http_transport=self.http_transport)
        assert server.command is not None
        return StdioTransport(server.command, server.args, server.env, server.cwd or self.cwd)

    async def start(self, name: str) -> ServerState:
        """Connect and list tools unless already running; failures land in the state."""
        state = self.servers.get(name)
        if state is None:
            raise MCPError("unknown_server", name)
        async with state.lock:
            if not state.config.enabled:
                state.status = "disabled"
                return state
            client = state.client
            if client is not None and client.alive and state.status == "ready":
                if state.stale:
                    await self._list(state, client)
                return state
            if client is not None:
                await client.close()
                state.client = None
            state.status, state.error = "starting", ""

            def changed() -> None:
                state.stale = True

            client = MCPClient(self._transport(state.config), timeout_s=state.config.timeout_s,
                               result_chars=self.result_chars, on_tools_changed=changed)
            state.client = client
            try:
                state.info = await client.connect(
                    timeout_s=min(state.config.timeout_s, STARTUP_CAP_S)
                )
                await self._list(state, client)
            except (MCPError, OSError) as exc:
                await client.close()
                state.client = None
                state.status = "error"
                state.error = _describe(exc)
                return state
            except asyncio.CancelledError:
                await client.close()
                state.client, state.status, state.error = None, "stopped", ""
                raise
            state.status = "ready"
            return state

    async def _list(self, state: ServerState, client: MCPClient) -> None:
        state.stale = False
        state.tools = await client.list_tools()

    async def stop(self, name: str) -> None:
        state = self.servers.get(name)
        if state is None:
            return
        async with state.lock:
            if state.client is not None:
                await state.client.close()
            state.client = None
            state.tools = []
            state.status = "stopped" if state.config.enabled else "disabled"
            state.error = ""

    async def restart(self, name: str) -> ServerState:
        await self.stop(name)
        return await self.start(name)

    async def close(self) -> None:
        await asyncio.gather(*(self.stop(name) for name in list(self.servers)),
                             return_exceptions=True)

    def kill_now(self) -> None:
        """Synchronous best-effort stop of every server (interpreter exit)."""
        for state in self.servers.values():
            if state.client is not None:
                with contextlib.suppress(Exception):
                    state.client.kill_now()
                state.client = None
                state.status = "stopped" if state.config.enabled else "disabled"

    # -- tools ------------------------------------------------------------------------

    async def tools(self, names: tuple[str, ...] | None = None) -> tuple[Tool, ...]:
        """Start the enabled servers (concurrently) and bridge every tool they list."""
        chosen = [n for n in (names or tuple(self.servers)) if n in self.servers]
        await asyncio.gather(
            *(self.start(n) for n in chosen if self.servers[n].config.enabled),
            return_exceptions=True,
        )
        return self.bridged()

    def bridged(self) -> tuple[Tool, ...]:
        """Conductor tools for the servers that are ready now (no I/O)."""
        out: list[Tool] = []
        taken: set[str] = set()
        for server in sorted(self.servers):
            state = self.servers[server]
            if state.status != "ready":
                continue
            for info in state.tools:
                name = tool_name(server, info.name)
                if name in taken:
                    digest = hashlib.sha1(f"{server}\0{info.name}".encode()).hexdigest()[:8]
                    name = name[: NAME_LIMIT - 9].rstrip("_") + "_" + digest
                taken.add(name)
                out.append(self._bridge(name, server, info))
        return tuple(out)

    def _bridge(self, name: str, server: str, info: ToolInfo) -> Tool:
        label = info.title or info.name
        description = f"[MCP server {server}: {label}] {info.description}".strip()
        parameters: dict[str, JsonValue] = dict(info.input_schema)
        parameters.setdefault("type", "object")
        read_only = info.read_only

        async def run(args: dict[str, JsonValue], ctx: ToolContext) -> JsonValue:
            await approve(ctx, server, info.name, args, read_only=read_only)
            result = await self.call(server, info.name, args)
            if result.is_error:
                raise ToolError("mcp_tool_error", clip(result.text, ERROR_CHARS))
            return {"server": server, "tool": info.name, "content": result.text,
                    "truncated": result.truncated}

        return Tool(name, clip(description, 1_200), parameters, run)

    async def call(self, server: str, tool: str, args: Mapping[str, Any]) -> CallResult:
        """``tools/call`` on a running server, starting (or restarting) it first."""
        state = self.servers.get(server)
        if state is None:
            raise ToolError("mcp_unknown_server", server)
        if not state.config.enabled:
            raise ToolError("mcp_server_disabled", server)
        for attempt in range(2):
            await self.start(server)
            client = state.client
            if state.status != "ready" or client is None:
                raise ToolError("mcp_server_unavailable", f"{server}: {state.error}")
            try:
                return await client.call_tool(tool, args, timeout_s=state.config.timeout_s)
            except MCPError as exc:
                if exc.code in ("server_exited", "closed") or not client.alive:
                    state.status, state.error = "error", _describe(exc)
                if exc.code == "session_expired" and attempt == 0:
                    await self.stop(server)  # the request was not processed: retry once
                    continue
                raise ToolError(f"mcp_{exc.code}", exc.detail) from None
        raise ToolError("mcp_session_expired", server)

    # -- listings ---------------------------------------------------------------------

    def rows(self) -> list[dict[str, str | int]]:
        out: list[dict[str, str | int]] = []
        for name in sorted(self.servers):
            state = self.servers[name]
            out.append({
                "name": name,
                "status": state.status,
                "transport": state.config.transport,
                "target": state.config.target(),
                "tools": len(state.tools) if state.status == "ready" else 0,
                "error": state.error,
            })
        return out


def _definition(server: ServerConfig) -> tuple[object, ...]:
    return (server.command, server.args, tuple(sorted(server.env.items())), server.cwd,
            server.url, tuple(sorted(server.headers.items())), server.timeout_s)


def _describe(exc: BaseException) -> str:
    if isinstance(exc, MCPError):
        return f"{exc.code}: {exc.detail}" if exc.detail else exc.code
    return type(exc).__name__


def arguments_summary(args: Mapping[str, Any]) -> str:
    try:
        text = json.dumps(args, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = str(args)
    return clip(text, SUMMARY_CHARS)


async def approve(
    ctx: ToolContext, server: str, tool: str, args: Mapping[str, Any], *, read_only: bool = False
) -> None:
    """The permission check before an MCP call; a refusal is a ``ToolError``."""
    mode = str(ctx.permission)
    if mode == "plan" and not read_only:
        raise ToolError("plan_mode_read_only",
                        "Permission mode is plan: MCP tools that can change things do not run.")
    if mode not in ("ask", "plan"):
        return
    action = f"mcp {server}.{tool}"
    if action in ctx.allowed:
        return
    answer = await ctx.ui.approve(action, arguments_summary(args))
    if answer == "session":
        ctx.allowed.add(action)
    elif answer != "yes":
        raise ToolError("denied_by_user", action)


# -- one manager per (workspace, cwd) -------------------------------------------------------

_MANAGERS: dict[tuple[str, str], MCPManager] = {}


def _key(workspace: Path, cwd: Path) -> tuple[str, str]:
    return (str(workspace.resolve()), str(cwd.resolve()))


def manager_for(workspace: Path, cwd: Path) -> MCPManager:
    """The shared manager for this workspace and working directory (created on first use)."""
    key = _key(workspace, cwd)
    manager = _MANAGERS.get(key)
    if manager is None:
        manager = MCPManager(workspace, cwd)
        _MANAGERS[key] = manager
    return manager


async def mcp_tools(workspace: Path, cwd: Path) -> tuple[Tool, ...]:
    """Start the enabled servers and return their tools; failures never raise."""
    manager = manager_for(workspace, cwd)
    try:
        return await manager.tools()
    except Exception:
        return manager.bridged()


async def close_mcp(workspace: Path | None = None, cwd: Path | None = None) -> None:
    """Stop the servers of one manager, or of every manager when no paths are given."""
    if workspace is not None and cwd is not None:
        managers = [m for m in [_MANAGERS.pop(_key(workspace, cwd), None)] if m is not None]
    else:
        managers = list(_MANAGERS.values())
        _MANAGERS.clear()
    for manager in managers:
        await manager.close()


def close_mcp_now() -> None:
    """Synchronous best-effort stop of every server (for sync exits and ``atexit``)."""
    for manager in list(_MANAGERS.values()):
        manager.kill_now()
    _MANAGERS.clear()
