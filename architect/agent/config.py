"""CLI configuration files (Phase 13, Section 5 item 6).

Two TOML files, read with ``tomllib``: ``<workspace>/config.toml`` (the user's defaults) and
``<cwd>/.architect/config.toml`` (this project; it wins key by key). Only the top-level keys
in ``KEYS`` are read here; tables such as ``[mcp.servers.<name>]`` belong to other packages
and are preserved untouched when ``/config set`` rewrites the workspace file.

Command-line flags win over both files. An invalid value is reported and ignored, never
fatal: a typo in a config file must not keep the CLI from starting.

This module also knows the conductor model's context window (``context_window``) for the
footer's context-fill meter, and which editor to open (``editor_command``).
"""

from __future__ import annotations

import os
import re
import shlex
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

__all__ = [
    "CHARS_PER_TOKEN",
    "CONFIG_FILE",
    "DEFAULT_CONTEXT_TOKENS",
    "KEYS",
    "Config",
    "ConfigError",
    "ConfigKey",
    "context_window",
    "editor_command",
    "load_config",
    "parse_value",
    "project_config_path",
    "set_value",
    "toml_value",
    "workspace_config_path",
]

CONFIG_FILE = "config.toml"
CHARS_PER_TOKEN = 4
DEFAULT_CONTEXT_TOKENS = 200_000
Kind = Literal["str", "float", "bool", "choice"]


class ConfigError(Exception):
    """A key or value ``/config set`` refuses; ``code`` is a fixed word."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ConfigKey:
    name: str
    kind: Kind
    default: str | float | bool | None
    help: str
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None


KEYS: dict[str, ConfigKey] = {
    key.name: key
    for key in (
        ConfigKey("model", "str", None, "the conductor's model (a profile model name)"),
        ConfigKey("provider", "str", None, "preferred provider for the model picker"),
        ConfigKey("permission", "choice", "ask", "coder approval mode",
                  choices=("ask", "auto-edit", "full-auto", "plan")),
        ConfigKey("session_budget_usd", "float", 25.0, "dollars per conversation",
                  minimum=0.01, maximum=10_000),
        ConfigKey("session_minutes", "float", 60.0, "minutes per conversation",
                  minimum=1, maximum=7 * 24 * 60),
        ConfigKey("auto_compact_percent", "float", 85.0,
                  "compact old tool results at this context fill (0 turns it off)",
                  minimum=0, maximum=99),
        ConfigKey("theme", "choice", "rich", "rich (prompt_toolkit and colour) or plain",
                  choices=("rich", "plain")),
        ConfigKey("editor", "str", None, "editor for Ctrl+G and /memory (else $VISUAL, $EDITOR)"),
        ConfigKey("web_fetch_allow_private", "bool", False,
                  "let web_fetch reach localhost and private network addresses"),
    )
}


def workspace_config_path(workspace: Path) -> Path:
    return workspace / CONFIG_FILE


def project_config_path(cwd: Path) -> Path:
    return cwd / ".architect" / CONFIG_FILE


def parse_value(key: str, raw: object) -> str | float | bool | None:
    """A typed, checked value for ``key`` from TOML or from text the user typed."""
    spec = KEYS.get(key)
    if spec is None:
        raise ConfigError("unknown_key", key)
    if isinstance(raw, str) and raw.strip().lower() in ("", "none", "default", "unset"):
        return None
    if spec.kind == "bool":
        if isinstance(raw, bool):
            return raw
        word = str(raw).strip().lower()
        if word in ("true", "yes", "on", "1"):
            return True
        if word in ("false", "no", "off", "0"):
            return False
        raise ConfigError("invalid_value", f"{key} is true or false")
    if spec.kind == "float":
        if isinstance(raw, bool):
            raise ConfigError("invalid_value", f"{key} is a number")
        try:
            number = float(str(raw).strip().lstrip("$").rstrip("%"))
        except ValueError:
            raise ConfigError("invalid_value", f"{key} is a number") from None
        if number != number or number in (float("inf"), float("-inf")):
            raise ConfigError("invalid_value", f"{key} is a number")
        low, high = spec.minimum, spec.maximum
        if (low is not None and number < low) or (high is not None and number > high):
            raise ConfigError("out_of_range", f"{key} is between {low:g} and {high:g}")
        return number
    text = str(raw).strip()
    if spec.kind == "choice" and text not in spec.choices:
        raise ConfigError("invalid_value", f"{key} is one of {', '.join(spec.choices)}")
    if any(ord(char) < 32 for char in text):
        raise ConfigError("invalid_value", key)
    return text


@dataclass
class Config:
    """Effective values: project file over workspace file over defaults."""

    values: dict[str, str | float | bool | None] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    workspace_path: Path | None = None
    project_path: Path | None = None
    # Tables and keys this module does not own (mcp servers, ...), merged as read.
    extra: dict[str, object] = field(default_factory=dict)

    def get(self, key: str) -> str | float | bool | None:
        if key in self.values:
            return self.values[key]
        return KEYS[key].default

    def source(self, key: str) -> str:
        return self.sources.get(key, "default")

    def text(self, key: str) -> str | None:
        value = self.get(key)
        return value if isinstance(value, str) else None

    def number(self, key: str) -> float:
        value = self.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
        default = KEYS[key].default
        return float(default) if isinstance(default, int | float) else 0.0

    def flag(self, key: str) -> bool:
        return self.get(key) is True

    def rows(self) -> list[tuple[str, str]]:
        shown = []
        for key in KEYS:
            value = self.get(key)
            text = "(unset)" if value is None else toml_value(value).strip('"')
            shown.append((key, f"{text}  [{self.source(key)}]"))
        return shown


def _read(path: Path, config: Config, label: str) -> None:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, UnicodeDecodeError):
        config.errors.append(f"{path}: could not be read")
        return
    except tomllib.TOMLDecodeError as exc:
        config.errors.append(f"{path}: not valid TOML ({str(exc)[:80]})")
        return
    for key, raw in data.items():
        if key not in KEYS:
            previous = config.extra.get(key)
            if isinstance(previous, dict) and isinstance(raw, dict):
                config.extra[key] = {**previous, **raw}
            else:
                config.extra[key] = raw
            continue
        try:
            value = parse_value(key, raw)
        except ConfigError as exc:
            config.errors.append(f"{path}: {exc.detail or exc.code} (ignored)")
            continue
        config.values[key] = value
        config.sources[key] = label


def load_config(workspace: Path, cwd: Path | None = None) -> Config:
    """The effective configuration; never raises for a bad file (see ``Config.errors``)."""
    path = workspace_config_path(workspace)
    config = Config(workspace_path=path)
    _read(path, config, "workspace")
    if cwd is not None:
        project = project_config_path(cwd)
        config.project_path = project
        if project.resolve() != path.resolve():
            _read(project, config, "project")
    return config


# -- writing --------------------------------------------------------------------------

_BARE_KEY = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")
_TABLE = re.compile(r"^\s*\[")


def toml_value(value: str | float | bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(int(value)) if float(value).is_integer() else repr(float(value))
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def set_value(path: Path, key: str, raw: object) -> str | float | bool | None:
    """Write ``key = value`` into the top level of ``path`` (None removes it).

    Line-based, so comments, other keys and tables (``[mcp.servers.x]``) stay as they are.
    """
    value = parse_value(key, raw)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except (OSError, UnicodeDecodeError):
        raise ConfigError("unreadable", str(path)) from None
    lines = text.splitlines()
    first_table = next((i for i, line in enumerate(lines) if _TABLE.match(line)), len(lines))
    index = next(
        (i for i, line in enumerate(lines[:first_table])
         if (m := _BARE_KEY.match(line)) is not None and m.group(1) == key),
        None,
    )
    entry = None if value is None else f"{key} = {toml_value(value)}"
    if index is not None:
        if entry is None:
            del lines[index]
        else:
            lines[index] = entry
    elif entry is not None:
        insert = first_table
        while insert > 0 and not lines[insert - 1].strip():
            insert -= 1  # keep the blank line before the first table
        lines.insert(insert, entry)
    body = "\n".join(lines).rstrip("\n") + "\n" if lines else ""
    try:
        tomllib.loads(body)
    except tomllib.TOMLDecodeError:
        raise ConfigError("would_break_file", str(path)) from None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(body, encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        raise ConfigError("unwritable", str(path)) from None
    return value


# -- context windows and editors ------------------------------------------------------

# (prefix, tokens), most specific first. Unknown models get DEFAULT_CONTEXT_TOKENS.
_WINDOWS: tuple[tuple[str, int], ...] = (
    ("claude", 200_000),
    ("gpt-4.1", 1_000_000),
    ("gpt-4o", 128_000),
    ("gpt-5", 400_000),
    ("codex", 400_000),
    ("o1", 200_000),
    ("o3", 200_000),
    ("o4", 200_000),
    ("gemini", 1_000_000),
    ("grok", 256_000),
    ("deepseek", 128_000),
    ("llama3", 128_000),
    ("llama-3", 128_000),
    ("qwen", 32_768),
    ("mistral", 32_000),
    ("mixtral", 32_000),
    ("gemma", 128_000),
    ("phi", 128_000),
    # NVIDIA Nemotron on Nebius Token Factory, matched on the part after ``nvidia/``.
    ("nvidia-nemotron-3-nano", 262_144),
    ("nemotron-3-super", 256_000),
    ("nemotron-3-ultra", 1_000_000),
    ("nemotron-3_5-lightning", 1_000_000),
)


def context_window(model: str, overrides: Mapping[str, int] | None = None) -> int:
    """The model's context window in tokens (a documented guess; 200k when unknown)."""
    if overrides and model in overrides:
        return overrides[model]
    name = model.lower().rsplit("/", 1)[-1]
    for prefix, tokens in _WINDOWS:
        if name.startswith(prefix):
            return tokens
    return DEFAULT_CONTEXT_TOKENS


def editor_command(configured: str | None = None) -> list[str]:
    """The editor to run: the config, then $VISUAL, $EDITOR, then notepad or vi."""
    for candidate in (configured, os.environ.get("VISUAL"), os.environ.get("EDITOR")):
        if candidate and candidate.strip():
            windows = sys.platform == "win32"
            try:
                words = shlex.split(candidate, posix=not windows)
            except ValueError:
                return [candidate]
            if windows:  # non-POSIX splitting keeps the quotes around "C:\Program Files\..."
                words = [w[1:-1] if len(w) >= 2 and w[0] == w[-1] == '"' else w for w in words]
            return words
    return ["notepad"] if sys.platform == "win32" else ["vi"]
