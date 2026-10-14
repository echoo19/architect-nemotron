"""MCP server configuration: ``config.toml`` tables and ``.mcp.json``.

Sources, lowest precedence first (a later source overrides an earlier one field by field):

1. ``<cwd>/.mcp.json`` in the format other harnesses share:
   ``{"mcpServers": {"name": {"command": ..., "args": [...], "env": {...}}}}`` or
   ``{"mcpServers": {"name": {"type": "http", "url": ..., "headers": {...}}}}``;
2. ``<workspace>/config.toml``;
3. ``<cwd>/.architect/config.toml`` (the project wins).

A TOML server is a table::

    [mcp.servers.files]
    command = "npx"
    args = ["-y", "@modelcontextprotocol/server-filesystem", "."]
    env = { DEBUG = "0" }
    cwd = "."            # relative to the working directory
    enabled = true
    timeout_s = 60

    [mcp.servers.docs]
    url = "https://example.com/mcp"
    headers = { Authorization = "Bearer ${DOCS_TOKEN}" }

``${VAR}`` and ``${VAR:-default}`` in strings are expanded from the environment.
``read_toml`` and ``config_paths`` are the small reusable API for the CLI's own config.
Edits (``add_server``, ``remove_server``, ``set_enabled``) rewrite only the server's own
sections, keep every other line, and are checked by re-parsing before the file is replaced.
"""

from __future__ import annotations

import json
import math
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

__all__ = [
    "CONFIG_FILE",
    "MCP_JSON",
    "ConfigError",
    "McpConfig",
    "ServerConfig",
    "add_server",
    "config_paths",
    "load_config",
    "read_toml",
    "remove_server",
    "set_enabled",
    "valid_name",
]

CONFIG_FILE = "config.toml"
MCP_JSON = ".mcp.json"
DEFAULT_TIMEOUT_S = 60.0
MAX_TIMEOUT_S = 3600.0
CONFIG_BYTES = 1024 * 1024
NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_FIELDS = ("command", "args", "env", "cwd", "url", "headers", "enabled", "timeout_s")


class ConfigError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ServerConfig:
    name: str
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: Path | None = None
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    timeout_s: float = DEFAULT_TIMEOUT_S
    sources: tuple[Path, ...] = ()

    @property
    def transport(self) -> Literal["stdio", "http"]:
        return "http" if self.url else "stdio"

    def target(self) -> str:
        """What the server runs or reaches, for listings (header values never shown)."""
        if self.url:
            return self.url
        return " ".join([self.command or "", *self.args]).strip()


@dataclass
class McpConfig:
    servers: dict[str, ServerConfig] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def valid_name(name: str) -> bool:
    return bool(NAME.match(name))


def config_paths(workspace: Path, cwd: Path) -> tuple[Path, Path]:
    """(workspace config.toml, project .architect/config.toml)."""
    return workspace / CONFIG_FILE, cwd / ".architect" / CONFIG_FILE


def read_toml(path: Path) -> dict[str, Any]:
    """A TOML file as a dict; a missing file is ``{}``; a broken one is ``ConfigError``."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigError("config_unreadable", f"{path}: {type(exc).__name__}") from None
    if len(data) > CONFIG_BYTES:
        raise ConfigError("config_too_large", str(path))
    try:
        return tomllib.loads(data.decode("utf-8-sig"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError("config_invalid", f"{path}: {exc}") from None


def _expand(value: str) -> str:
    def one(match: re.Match[str]) -> str:
        found = os.environ.get(match.group(1))
        if found is not None and (found or match.group(2) is None):
            return found
        return match.group(2) if match.group(2) is not None else ""

    return _VAR.sub(one, value)


def _strings(value: object, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str | int | float) for v in value):
        raise ConfigError("invalid_field", f"{key} must be a list of strings")
    return tuple(_expand(str(v)) for v in value)


def _table(value: object, key: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ConfigError("invalid_field", f"{key} must be a table of strings")
    out: dict[str, str] = {}
    for name, item in value.items():
        if not isinstance(name, str) or not isinstance(item, str | int | float | bool):
            raise ConfigError("invalid_field", f"{key} must be a table of strings")
        out[name] = _expand(str(item).lower() if isinstance(item, bool) else str(item))
    return out


def _server(
    name: str, raw: Mapping[str, Any], cwd: Path, sources: tuple[Path, ...]
) -> ServerConfig:
    command = raw.get("command")
    url = raw.get("url")
    if command is not None and not isinstance(command, str):
        raise ConfigError("invalid_field", "command must be a string")
    if url is not None and not isinstance(url, str):
        raise ConfigError("invalid_field", "url must be a string")
    command = _expand(command).strip() if command else None
    url = _expand(url).strip() if url else None
    if bool(command) == bool(url):
        raise ConfigError("invalid_server", "set exactly one of command or url")
    if url and not re.match(r"^https?://", url, re.IGNORECASE):
        raise ConfigError("invalid_field", "url must start with http:// or https://")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("invalid_field", "enabled must be true or false")
    timeout = raw.get("timeout_s", DEFAULT_TIMEOUT_S)
    if (
        not isinstance(timeout, int | float)
        or isinstance(timeout, bool)
        or not math.isfinite(timeout)
        or not 0 < timeout <= MAX_TIMEOUT_S
    ):
        raise ConfigError("invalid_field", f"timeout_s must be between 0 and {MAX_TIMEOUT_S:g}")
    where = raw.get("cwd")
    if where is not None and not isinstance(where, str):
        raise ConfigError("invalid_field", "cwd must be a string")
    return ServerConfig(
        name=name,
        command=command,
        args=_strings(raw.get("args", []), "args"),
        env=_table(raw.get("env", {}), "env"),
        cwd=(cwd / _expand(where)).resolve() if where else None,
        url=url,
        headers=_table(raw.get("headers", {}), "headers"),
        enabled=enabled,
        timeout_s=float(timeout),
        sources=sources,
    )


def _from_json(path: Path, problems: list[str]) -> dict[str, dict[str, Any]]:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError:
        problems.append(f"{path}: unreadable")
        return {}
    try:
        payload = json.loads(data.decode("utf-8-sig")) if len(data) <= CONFIG_BYTES else None
    except (ValueError, UnicodeDecodeError):
        payload = None
    servers = payload.get("mcpServers") if isinstance(payload, dict) else None
    if not isinstance(servers, dict):
        problems.append(f"{path}: expected {{\"mcpServers\": {{...}}}}")
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, raw in servers.items():
        if not isinstance(raw, dict):
            problems.append(f"{name}: not an object in {path.name}")
            continue
        kind = str(raw.get("type", "")).lower()
        if kind == "sse":
            problems.append(f"{name}: the legacy HTTP+SSE transport is not supported; "
                            "use a streamable HTTP url")
            continue
        entry = {key: raw[key] for key in _FIELDS if key in raw}
        if raw.get("disabled") is True:
            entry["enabled"] = False
        if "timeout" in raw and "timeout_s" not in raw and isinstance(raw["timeout"], int | float):
            entry["timeout_s"] = raw["timeout"] / 1000  # milliseconds in other harnesses
        out[str(name)] = entry
    return out


def _from_toml(path: Path, problems: list[str]) -> dict[str, dict[str, Any]]:
    try:
        data = read_toml(path)
    except ConfigError as exc:
        problems.append(f"{exc.code}: {exc.detail}")
        return {}
    mcp = data.get("mcp", {})
    servers = mcp.get("servers", {}) if isinstance(mcp, dict) else {}
    if not isinstance(servers, dict):
        problems.append(f"{path}: [mcp.servers] must be a table")
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, raw in servers.items():
        if isinstance(raw, dict):
            out[name] = dict(raw)
        else:
            problems.append(f"{name}: not a table in {path}")
    return out


def load_config(workspace: Path, cwd: Path) -> McpConfig:
    """Every configured server, merged by precedence; bad entries become ``problems``."""
    config = McpConfig()
    merged: dict[str, dict[str, Any]] = {}
    origins: dict[str, list[Path]] = {}
    global_toml, project_toml = config_paths(workspace, cwd)
    layers = (
        (cwd / MCP_JSON, _from_json),
        (global_toml, _from_toml),
        (project_toml, _from_toml),
    )
    for path, reader in layers:
        for name, raw in reader(path, config.problems).items():
            entry = merged.setdefault(name, {})
            if "url" in raw and "command" not in raw:
                entry.pop("command", None)  # a layer switching transports replaces it
                entry.pop("args", None)
            if "command" in raw and "url" not in raw:
                entry.pop("url", None)
                entry.pop("headers", None)
            entry.update(raw)
            origins.setdefault(name, []).append(path)
    for name, raw in merged.items():
        try:
            config.servers[name] = _server(name, raw, cwd, tuple(origins[name]))
        except ConfigError as exc:
            config.problems.append(f"{name}: {exc.detail or exc.code}")
    return config


# -- editing ----------------------------------------------------------------------------


def _key(name: str) -> str:
    return name if re.match(r"^[A-Za-z0-9_-]+$", name) else json.dumps(name)


def _value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)  # a valid TOML basic string
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_value(v) for v in value) + "]"
    if isinstance(value, Mapping):
        return "{ " + ", ".join(f"{_key(str(k))} = {_value(v)}" for k, v in value.items()) + " }"
    raise ConfigError("invalid_field", type(value).__name__)


def _header(name: str) -> re.Pattern[str]:
    quoted = "|".join(re.escape(form) for form in (name, json.dumps(name), f"'{name}'"))
    return re.compile(
        r"^\s*\[\s*mcp\s*\.\s*servers\s*\.\s*(?:" + quoted + r")\s*(\.[^\]]*)?\]\s*(#.*)?$"
    )


_ANY_HEADER = re.compile(r"^\s*\[")


def _sections(lines: list[str], name: str) -> list[tuple[int, int, bool]]:
    """(start, end, is_main) for each ``[mcp.servers.<name>...]`` section."""
    pattern = _header(name)
    found: list[tuple[int, int, bool]] = []
    index = 0
    while index < len(lines):
        match = pattern.match(lines[index])
        if match is None:
            index += 1
            continue
        end = index + 1
        while end < len(lines) and not _ANY_HEADER.match(lines[end]):
            end += 1
        found.append((index, end, match.group(1) is None))
        index = end
    return found


def _write(path: Path, text: str, check: Mapping[str, Any] | None, name: str) -> None:
    """Replace ``path`` after checking the new text parses and holds what was intended."""
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise ConfigError("config_edit_failed", f"{path}: edit it by hand") from None
    mcp = parsed.get("mcp", {})
    servers = mcp.get("servers", {}) if isinstance(mcp, dict) else {}
    entry = servers.get(name) if isinstance(servers, dict) else None
    if check is None:
        if entry is not None:
            raise ConfigError("config_edit_failed", f"{path}: edit it by hand")
    elif not isinstance(entry, dict) or any(entry.get(k) != v for k, v in check.items()):
        raise ConfigError("config_edit_failed", f"{path}: edit it by hand")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _lines(path: Path) -> list[str]:
    read_toml(path)  # refuse to edit a file that does not parse
    try:
        return path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return []


def _join(lines: list[str]) -> str:
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) + "\n" if lines else ""


def add_server(
    path: Path,
    name: str,
    *,
    command: str | None = None,
    args: tuple[str, ...] = (),
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    url: str | None = None,
    headers: Mapping[str, str] | None = None,
    timeout_s: float | None = None,
    enabled: bool = True,
) -> None:
    """Write ``[mcp.servers.<name>]`` into ``path``, replacing that server's sections only."""
    if not valid_name(name):
        raise ConfigError("invalid_name", "use letters, digits, - and _ (up to 64)")
    if bool(command) == bool(url):
        raise ConfigError("invalid_server", "set exactly one of command or url")
    table: dict[str, object] = {}
    if command:
        table["command"] = command
        table["args"] = list(args)
        if env:
            table["env"] = dict(env)
        if cwd:
            table["cwd"] = cwd
    else:
        table["url"] = url
        if headers:
            table["headers"] = dict(headers)
    if timeout_s is not None:
        table["timeout_s"] = timeout_s
    table["enabled"] = enabled
    _server(name, table, path.parent, (path,))  # the same checks loading applies
    lines = _lines(path)
    for start, end, _ in reversed(_sections(lines, name)):
        del lines[start:end]
    lines = _join(lines).splitlines()
    if lines:
        lines.append("")
    lines.append(f"[mcp.servers.{_key(name)}]")
    lines.extend(f"{key} = {_value(value)}" for key, value in table.items())
    check = {k: v for k, v in table.items() if k in ("command", "url", "enabled")}
    _write(path, _join(lines), check, name)


def remove_server(path: Path, name: str) -> bool:
    """Drop the server's sections from ``path``; False when it had none."""
    lines = _lines(path)
    sections = _sections(lines, name)
    if not sections:
        return False
    for start, end, _ in reversed(sections):
        del lines[start:end]
    _write(path, _join(lines), None, name)
    return True


def set_enabled(path: Path, name: str, enabled: bool) -> None:
    """Set ``enabled`` in the server's table in ``path`` (adding the table when absent)."""
    if not valid_name(name) and not _sections(_lines(path), name):
        raise ConfigError("invalid_name", name)
    lines = _lines(path)
    main = next(((s, e) for s, e, is_main in _sections(lines, name) if is_main), None)
    entry = f"enabled = {_value(enabled)}"
    if main is None:
        lines = _join(lines).splitlines()
        if lines:
            lines.append("")
        lines += [f"[mcp.servers.{_key(name)}]", entry]
    else:
        start, end = main
        for index in range(start + 1, end):
            if re.match(r"^\s*enabled\s*=", lines[index]):
                lines[index] = entry
                break
        else:
            lines.insert(start + 1, entry)
    _write(path, _join(lines), {"enabled": enabled}, name)
