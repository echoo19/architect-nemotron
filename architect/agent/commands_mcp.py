"""``/mcp``: Model Context Protocol servers from the CLI (Phase 13, Package D).

``/mcp add`` writes ``[mcp.servers.<name>]`` into ``<workspace>/config.toml``; enable and
disable write ``enabled`` where the server is defined (the project's
``.architect/config.toml`` when it defines it, else the workspace file). After a change the
conductor's ``mcp__*`` tools are rebound so the model sees the new set.
"""

from __future__ import annotations

import shlex
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from architect.mcp import (
    ConfigError,
    MCPManager,
    add_server,
    config_paths,
    manager_for,
    remove_server,
    set_enabled,
)
from architect.mcp.config import valid_name
from architect.mcp.manager import PREFIX

from .commands import command
from .tools import Tool

if TYPE_CHECKING:
    from .repl import Repl

__all__ = ["mcp_manager", "rebind_tools"]

USAGE = (
    "/mcp [tools [server] | add <name> <command...> | add <name> --url <url> | "
    "remove <name> | enable <name> | disable <name> | restart <name>]"
)
DETAIL = (
    "Servers come from [mcp.servers.<name>] in <workspace>/config.toml and "
    "<project>/.architect/config.toml (the project wins) and from .mcp.json. Their tools are "
    "mcp__<server>__<tool>; in ask mode every call is approved first. Options for add: "
    "--env KEY=VALUE, --cwd DIR, --timeout SECONDS (before the command); with --url: "
    "--header 'Name: value'."
)


def mcp_manager(repl: Repl) -> MCPManager:
    """The manager the CLI bound (``conductor.extras["mcp"]``), else the shared one."""
    bound = repl.conductor.extras.get("mcp")
    if isinstance(bound, MCPManager):
        return bound
    return manager_for(repl.app.workspace, repl.conductor.cwd)


def rebind_tools(repl: Repl, bridged: Sequence[Tool]) -> None:
    """Replace the conductor's ``mcp__*`` tools with ``bridged``; other tools stay."""
    conductor = repl.conductor
    tools = tuple(t for t in conductor.tools if not t.name.startswith(PREFIX)) + tuple(bridged)
    setter = getattr(conductor, "set_tools", None)
    if callable(setter):
        setter(tools)
        return
    conductor.tools = tools
    conductor._by_name = {tool.name: tool for tool in tools}


def _split(text: str) -> list[str]:
    """Shell-like words; Windows paths keep their backslashes."""
    if sys.platform != "win32":
        return shlex.split(text, posix=True)
    words = shlex.split(text, posix=False)
    return [w[1:-1] if len(w) >= 2 and w[0] == w[-1] and w[0] in "\"'" else w for w in words]


def _status_line(row: dict[str, str | int]) -> str:
    status = str(row["status"])
    if status == "ready":
        head = f"ready, {row['tools']} tools"
    elif status == "stopped":
        head = "not started (starts when needed)"
    else:
        head = status
    line = f"{head}  [{row['transport']}] {row['target']}"
    if row.get("error"):
        line += f"  ({row['error']})"
    return line


async def _list(repl: Repl, manager: MCPManager) -> None:
    rows = manager.rows()
    if not rows:
        repl.say("No MCP servers. Add one with /mcp add <name> <command...> or "
                 "/mcp add <name> --url <url>.")
    else:
        repl.rows("MCP servers", [(str(row["name"]), _status_line(row)) for row in rows])
    for problem in manager.problems:
        repl.say(f"MCP config: {problem}", "warn")


async def _tools(repl: Repl, manager: MCPManager, words: list[str]) -> None:
    names = tuple(words) or None
    for name in words:
        if name not in manager.servers:
            repl.say(f"No MCP server {name}.", "error")
            return
    finished, bridged = await repl.work(manager.tools(names))
    if not finished or bridged is None:
        return
    rebind_tools(repl, manager.bridged())
    chosen = [n for n in sorted(manager.servers) if names is None or n in names]
    for name in chosen:
        state = manager.servers[name]
        if state.status != "ready":
            repl.say(f"{name}: {state.status}" + (f" ({state.error})" if state.error else ""),
                     "warn" if state.status == "error" else "info")
    prefixes = tuple(f"{PREFIX}{n}" for n in chosen)
    rows = [(tool.name, tool.description[:160]) for tool in manager.bridged()
            if names is None or any(tool.name.startswith(p) for p in prefixes)]
    if rows:
        repl.rows("MCP tools", rows)
    elif all(manager.servers[n].status == "ready" for n in chosen):
        repl.say("No MCP tools.")


async def _start(repl: Repl, manager: MCPManager, name: str, verb: str) -> None:
    finished, state = await repl.work(manager.start(name))
    rebind_tools(repl, manager.bridged())
    if not finished or state is None:
        return
    if state.status == "ready":
        repl.say(f"{verb} {name}: {len(state.tools)} tools.")
    elif state.status == "error":
        repl.say(f"{name} did not start ({state.error}).", "error")


def _parse_add(words: list[str]) -> tuple[str, dict[str, object]]:
    if not words:
        raise ConfigError("usage", "")
    name, rest = words[0], words[1:]
    options: dict[str, object] = {}
    env: dict[str, str] = {}
    headers: dict[str, str] = {}
    while rest and rest[0].startswith("--"):
        flag = rest.pop(0)
        if not rest:
            raise ConfigError("usage", f"{flag} needs a value")
        value = rest.pop(0)
        if flag == "--url":
            options["url"] = value
        elif flag == "--env":
            key, sep, item = value.partition("=")
            if not sep or not key:
                raise ConfigError("usage", "--env KEY=VALUE")
            env[key] = item
        elif flag == "--header":
            key, sep, item = value.partition(":")
            if not sep or not key.strip():
                raise ConfigError("usage", "--header 'Name: value'")
            headers[key.strip()] = item.strip()
        elif flag == "--cwd":
            options["cwd"] = value
        elif flag == "--timeout":
            try:
                options["timeout_s"] = float(value)
            except ValueError:
                raise ConfigError("usage", "--timeout SECONDS") from None
        else:
            raise ConfigError("usage", f"unknown option {flag}")
    if "url" in options:
        if rest or env or "cwd" in options:
            raise ConfigError("usage", "--url takes no command, --env or --cwd")
        options["headers"] = headers
    else:
        if not rest:
            raise ConfigError("usage", "give the server's command")
        if headers:
            raise ConfigError("usage", "--header needs --url")
        options["command"], options["args"] = rest[0], tuple(rest[1:])
        options["env"] = env
    return name, options


async def _add(repl: Repl, manager: MCPManager, words: list[str]) -> None:
    try:
        name, options = _parse_add(words)
    except ConfigError as exc:
        repl.say("Usage: /mcp add <name> <command...> or /mcp add <name> --url <url>"
                 + (f" ({exc.detail})" if exc.detail else ""), "warn")
        return
    path = config_paths(repl.app.workspace, repl.conductor.cwd)[0]
    try:
        add_server(path, name, **options)  # type: ignore[arg-type]
    except ConfigError as exc:
        repl.say(f"Not added ({exc.code}{': ' + exc.detail if exc.detail else ''}).", "error")
        return
    repl.say(f"Added MCP server {name} to {path}.")
    await manager.apply()
    state = manager.servers.get(name)
    if state is not None and state.config.enabled:
        await _start(repl, manager, name, "Started")
    else:
        repl.say(f"{name} is disabled by another config file.", "warn")


def _where(repl: Repl, manager: MCPManager, name: str) -> Path:
    workspace_file, project_file = config_paths(repl.app.workspace, repl.conductor.cwd)
    state = manager.servers.get(name)
    if state is not None and project_file in state.config.sources:
        return project_file
    return workspace_file


async def _remove(repl: Repl, manager: MCPManager, name: str) -> None:
    path = config_paths(repl.app.workspace, repl.conductor.cwd)[0]
    try:
        removed = remove_server(path, name)
    except ConfigError as exc:
        repl.say(f"Not removed ({exc.code}{': ' + exc.detail if exc.detail else ''}).", "error")
        return
    await manager.apply()
    rebind_tools(repl, manager.bridged())
    state = manager.servers.get(name)
    if state is not None:
        where = ", ".join(str(p) for p in state.config.sources)
        prefix = f"Removed {name} from {path}; it" if removed else name
        repl.say(f"{prefix} is still defined in {where}. Edit that file, or "
                 f"/mcp disable {name}.", "warn")
    elif removed:
        repl.say(f"Removed MCP server {name}.")
    else:
        repl.say(f"No MCP server {name}.", "error")


async def _toggle(repl: Repl, manager: MCPManager, name: str, enabled: bool) -> None:
    if name not in manager.servers:
        repl.say(f"No MCP server {name}.", "error")
        return
    path = _where(repl, manager, name)
    try:
        set_enabled(path, name, enabled)
    except ConfigError as exc:
        repl.say(f"Not changed ({exc.code}{': ' + exc.detail if exc.detail else ''}).", "error")
        return
    await manager.apply()
    if enabled:
        await _start(repl, manager, name, "Enabled")
    else:
        rebind_tools(repl, manager.bridged())
        repl.say(f"Disabled {name} ({path}).")


async def _restart(repl: Repl, manager: MCPManager, name: str) -> None:
    state = manager.servers.get(name)
    if state is None:
        repl.say(f"No MCP server {name}.", "error")
        return
    if not state.config.enabled:
        repl.say(f"{name} is disabled; /mcp enable {name} first.", "warn")
        return
    await manager.stop(name)
    await _start(repl, manager, name, "Restarted")


@command("/mcp", USAGE, "MCP servers: status, tools, add, remove, enable, disable",
         detail=DETAIL)
async def _mcp(repl: Repl, arg: str) -> None:
    manager = mcp_manager(repl)
    try:
        words = _split(arg)
    except ValueError:
        repl.say("Could not parse the arguments (check the quotes).", "error")
        return
    verb = words[0].lower() if words else "list"
    rest = words[1:]
    await manager.apply()  # pick up config edits made outside this session
    if verb in ("list", "status"):
        await _list(repl, manager)
        return
    if verb == "tools":
        await _tools(repl, manager, rest)
        return
    if verb == "add":
        await _add(repl, manager, rest)
        return
    if verb in ("remove", "enable", "disable", "restart"):
        if len(rest) != 1 or not (valid_name(rest[0]) or rest[0] in manager.servers):
            repl.say(f"Usage: /mcp {verb} <name>", "warn")
            return
        name = rest[0]
        if verb == "remove":
            await _remove(repl, manager, name)
        elif verb == "restart":
            await _restart(repl, manager, name)
        else:
            await _toggle(repl, manager, name, verb == "enable")
        return
    repl.say("Usage: " + USAGE, "warn")
