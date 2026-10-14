"""Slash commands (Phase 12, Section 6).

Each command is a small async function over the running ``Repl``. Commands that need the
conductor's tools call them directly with the conductor's context, so a command and the
model share one implementation, one permission mode and one session budget. Overrides for
the next compile (``/floor``, ``/budget run``, ``/model``) live in
``conductor.extras["build_overrides"]``, set through build mode's ``set_override`` (keys
``floor_limit``, ``floor_file``, ``budget_usd``, ``budget_s``, ``compile_usd``,
``compile_s``, ``model``, ...); build mode reads them.

Phase 13 adds the session tools (``/context``, ``/undo``, ``/diff``, ``/config``,
``/doctor``, ``/memory``, ``/export``, ``/copy``, ``/review``, ``/title``, ``/image``) and
custom commands: every ``<cwd>/.architect/commands/<name>.md`` and
``<workspace>/commands/<name>.md`` is ``/<name>``; the file body is the prompt and
``$ARGUMENTS`` becomes what follows the command (the project's file wins).
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import inspect
import json
import math
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pydantic import JsonValue

from .conductor import Mode
from .tools import SPAWNABLE, Permission, ToolError
from .transcript import Transcript

if TYPE_CHECKING:
    from .repl import Repl

__all__ = [
    "COMMANDS",
    "Command",
    "CustomCommand",
    "all_command_names",
    "command_names",
    "custom_commands",
    "describe",
    "dispatch",
    "find",
]

Handler = Callable[["Repl", str], Awaitable[None]]
MODES: tuple[Mode, ...] = ("normal", "build")
# "forge" is build mode's old name, still accepted after the label became "build".
MODE_NAMES = {"normal": "normal", "build": "build", "forge": "build"}
PERMISSIONS: tuple[Permission, ...] = ("ask", "auto-edit", "full-auto", "plan")
CONSENT_USD = 0.50
RESULT_CHARS = 4000
INIT_REQUEST = (
    "Draft an ARCHITECT.md for this working directory: look around first (list_dir, "
    "read_file on the README and build files), then hand the writing to `code` with a "
    "complete description. ARCHITECT.md should say in under 60 lines what the project is, "
    "how to build, test and lint it, the layout, conventions, and anything an agent "
    "working here must not do."
)


@dataclass(frozen=True)
class Command:
    name: str
    usage: str
    summary: str
    handler: Handler
    aliases: tuple[str, ...] = ()
    detail: str = ""


COMMANDS: dict[str, Command] = {}


def command(
    name: str, usage: str, summary: str, *, aliases: tuple[str, ...] = (), detail: str = ""
) -> Callable[[Handler], Handler]:
    """Register a slash command; per-package modules (Phase 13) use this public name."""
    return _command(name, usage, summary, aliases=aliases, detail=detail)


def _command(
    name: str, usage: str, summary: str, *, aliases: tuple[str, ...] = (), detail: str = ""
) -> Callable[[Handler], Handler]:
    def register(handler: Handler) -> Handler:
        entry = Command(name, usage, summary, handler, aliases, detail)
        COMMANDS[name] = entry
        for alias in aliases:
            COMMANDS[alias] = entry
        return handler

    return register


def command_names() -> list[str]:
    return sorted(COMMANDS)


def find(name: str) -> Command | None:
    key = name if name.startswith("/") else "/" + name
    return COMMANDS.get(key)


async def dispatch(repl: Repl, line: str) -> None:
    name, _, arg = line.strip().partition(" ")
    entry = COMMANDS.get(name.lower())
    if entry is None:
        custom = custom_commands(repl).get(name.lower())
        if custom is not None:
            await repl.turn(custom.prompt(arg.strip()))
            return
        names = list(COMMANDS) + list(custom_commands(repl))
        close = difflib.get_close_matches(name.lower(), names, n=1, cutoff=0.5)
        hint = f" Did you mean {close[0]}?" if close else ""
        repl.say(f"Unknown command {name}.{hint} /help lists them.", "warn")
        return
    await entry.handler(repl, arg.strip())


# -- custom commands (Phase 13, Section 5 item 9) -------------------------------------------

_CUSTOM_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
CUSTOM_BYTES = 32 * 1024


@dataclass(frozen=True)
class CustomCommand:
    name: str
    body: str
    description: str
    path: Path

    def prompt(self, arguments: str) -> str:
        if "$ARGUMENTS" in self.body:
            return self.body.replace("$ARGUMENTS", arguments)
        return self.body + (f"\n\n{arguments}" if arguments else "")


def _parse_custom(path: Path) -> CustomCommand | None:
    name = "/" + path.stem.lower()
    if not _CUSTOM_NAME.match(path.stem.lower()):
        return None
    try:
        text = path.read_bytes()[:CUSTOM_BYTES].decode("utf-8", errors="replace")
    except OSError:
        return None
    text = text.replace("\r\n", "\n")
    description = ""
    if text.startswith("---\n"):
        head, sep, rest = text[4:].partition("\n---")
        if sep:
            for line in head.splitlines():
                key, _, value = line.partition(":")
                if key.strip().lower() == "description":
                    description = value.strip().strip("'\"")
            text = rest.lstrip("-").lstrip("\n")
    body = text.strip()
    if not body:
        return None
    if not description:
        first = body.splitlines()[0].lstrip("# ").strip()
        description = first if len(first) <= 70 else first[:67] + "..."
    return CustomCommand(name, body, description, path)


def custom_commands(repl: Repl) -> dict[str, CustomCommand]:
    """Custom commands by name; the project's directory wins; built-ins are never shadowed."""
    found: dict[str, CustomCommand] = {}
    directories = (repl.app.workspace / "commands", repl.conductor.cwd / ".architect" / "commands")
    for directory in directories:
        try:
            files = sorted(directory.glob("*.md"))
        except OSError:
            continue
        for path in files[:200]:
            command = _parse_custom(path)
            if command is not None and command.name not in COMMANDS:
                found[command.name] = command
    return found


def all_command_names(repl: Repl) -> list[str]:
    return sorted(set(COMMANDS) | set(custom_commands(repl)))


def describe(repl: Repl, name: str) -> str:
    entry = COMMANDS.get(name)
    if entry is not None:
        return entry.summary
    custom = custom_commands(repl).get(name)
    return f"{custom.description} (custom)" if custom is not None else ""


# -- helpers ------------------------------------------------------------------------------


def _overrides(repl: Repl) -> dict[str, Any]:
    extras = repl.conductor.extras
    value = extras.get("build_overrides")
    if not isinstance(value, dict):
        value = {}
        extras["build_overrides"] = value
    return value


def _money(value: float) -> str:
    if 0 < value < 0.0001:
        # A Lightning run costs millionths of a dollar: two significant digits, as the
        # desktop app shows it (fmtUsd), so it never reads "$0.0000".
        digits = 1 - math.floor(math.log10(value))
        return "$" + f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return f"${value:.4f}" if value < 1 else f"${value:.2f}"


def _number(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value > 0 and value == value and value != float("inf") else None


def _show(repl: Repl, value: JsonValue) -> None:
    text = value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)
    if len(text) > RESULT_CHARS:
        text = text[:RESULT_CHARS] + "\n... [clipped]"
    repl.text(text)


def _scalar(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.4g}"
    if isinstance(value, dict | list):
        text = json.dumps(value, ensure_ascii=False)
        return text if len(text) <= 100 else text[:97] + "..."
    return str(value)


async def _confirm(repl: Repl, what: str, bound: float) -> bool:
    """Money consent (Section 3.1): in ``ask`` mode a bound above $0.50 is confirmed."""
    if repl.conductor.context.permission != "ask" or bound <= CONSENT_USD:
        return True
    answer = await repl.frontend.ask(f"{what} can spend up to {_money(bound)}. Continue?",
                                     default="y", choices=("y", "n"))
    return answer is not None and answer.strip().lower() in ("y", "yes")


# -- help and modes -----------------------------------------------------------------------

KEYS = (
    ("Enter", "send"),
    ("Ctrl+J, or \\ at the end", "new line"),
    ("Shift+Tab (Alt+M)", "cycle normal / build / plan mode"),
    ("Tab", "complete commands (fuzzy, with descriptions), agent names and @paths"),
    ("Up, Ctrl+R", "history, and search it"),
    ("Ctrl+G, Ctrl+X Ctrl+E", "edit the message in your editor (config 'editor')"),
    ("typing during a turn", "queued and sent when the turn ends (Ctrl+U clears it)"),
    ("Esc or Ctrl+C", "interrupt the running turn (finished work is kept)"),
    ("Ctrl+C twice, Ctrl+D", "exit"),
    ("!<command>", "run a shell command yourself (not sent to the model)"),
    ("@path", "attach a file's text, or an image (.png .jpg .webp .gif)"),
)


@_command("/help", "/help [command]", "Commands, keys and modes")
async def _help(repl: Repl, arg: str) -> None:
    if arg:
        entry = find(arg.split()[0])
        if entry is None:
            key = arg.split()[0]
            custom = custom_commands(repl).get(key if key.startswith("/") else "/" + key)
            if custom is not None:
                repl.rows(custom.name, [("what", custom.description),
                                        ("file", str(custom.path))])
                return
            repl.say(f"No command {arg}; /help lists them.", "warn")
            return
        rows = [("usage", entry.usage), ("what", entry.summary)]
        if entry.aliases:
            rows.append(("also", ", ".join(entry.aliases)))
        if entry.detail:
            rows.append(("notes", entry.detail))
        repl.rows(entry.name, rows)
        return
    seen: set[str] = set()
    rows = []
    for name in sorted(COMMANDS):
        entry = COMMANDS[name]
        if entry.name in seen:
            continue
        seen.add(entry.name)
        rows.append((entry.usage, entry.summary))
    repl.rows("Commands", rows)
    customs = custom_commands(repl)
    if customs:
        repl.rows("Custom commands", [(f"{c.name} [args]", c.description)
                                      for c in customs.values()])
    else:
        repl.text("Custom commands: put a prompt in .architect/commands/<name>.md (this "
                  "project) or <workspace>/commands/<name>.md; $ARGUMENTS takes the rest of "
                  "the line.")
    repl.rows("Keys", list(KEYS))
    repl.text(
        "Modes: normal answers and delegates (the coder edits files, preset subagents do "
        "structured jobs); build turns a request into a tested, licensed agent or harness: "
        "a short interview, a plan card with the inferred floor and budget, then compile; "
        "plan is read-only: Architect proposes changes and the coder is not called."
    )


@_command("/mode", "/mode [normal|build]", "Show or switch mode (Shift+Tab cycles)",
          detail="Build mode builds agents and harnesses (interview, plan card, compile); "
          "/mode forge, its old name, still works. Plan mode is a permission mode: "
          "/permissions plan.")
async def _mode(repl: Repl, arg: str) -> None:
    from .render import mode_label

    if not arg:
        repl.say(f"Mode: {mode_label(repl.conductor.mode)}.")
        return
    mode = MODE_NAMES.get(arg.lower())
    if mode is None:
        repl.say("Modes: normal, build.", "warn")
        return
    repl.conductor.mode = mode  # type: ignore[assignment]
    repl.say(f"Mode: {mode_label(mode)}.")


_FROZEN_ARG = re.compile(r'@(?:"([^"]+)"|(\S+))')


def frozen_path(arg: str) -> str | None:
    """The path of a ``@<path>`` argument (one path, double quotes allowed), else None."""
    match = _FROZEN_ARG.fullmatch(arg.strip())
    if match is None:
        return None
    path = (match.group(1) or match.group(2) or "").strip()
    return path or None


async def frozen_build(repl: Repl, path: str, *, forge: bool = False) -> JsonValue | None:
    """Phase 14: ``/make @<path>`` and ``/forge @<path>`` call the ``make`` tool directly,
    never through the conductor or ``@`` mention expansion, with Forge kept on for /forge."""
    extras = repl.conductor.extras
    if forge:
        extras["force_forge"] = True
    try:
        result = await repl.call_tool("make", {"description": "", "spec_file": path})
    finally:
        if forge:
            extras.pop("force_forge", None)
    if isinstance(result, dict):
        if result.get("cancelled"):
            repl.say(f"Not compiled. Spec {result.get('spec_hash', '')}.")
        elif result.get("compiled") is False:
            failures = result.get("failures")
            codes = ", ".join(str(code) for code in failures) if isinstance(failures, list) else ""
            repl.say(f"Not compiled ({codes}). Next: {result.get('next_step', '')}", "warn")
    return result


@_command("/make", "/make <description> | /make @<spec.json>",
          "Build an agent for one request: interview, plan card, build, exam",
          detail="/make @<spec.json> compiles exactly the Spec in that file (no interview or "
          "inference) and shows its full hash on the plan card.")
async def _make(repl: Repl, arg: str) -> None:
    if not arg:
        repl.say("Usage: /make <what the agent should do>", "warn")
        return
    path = frozen_path(arg)
    if path is not None:
        await frozen_build(repl, path)
        return
    await repl.turn(arg, mode="build")


# -- subagents and agents -----------------------------------------------------------------


@_command("/spawn", "/spawn <preset> <task>", "Run one ephemeral preset subagent now",
          detail="Presets: " + ", ".join(SPAWNABLE) + ". Unlicensed and metered.")
async def _spawn(repl: Repl, arg: str) -> None:
    preset, _, task = arg.partition(" ")
    if not preset or not task.strip():
        repl.say("Usage: /spawn <preset> <task>  (presets: " + ", ".join(SPAWNABLE) + ")",
                 "warn")
        return
    result = await repl.call_tool("spawn", {"preset": preset, "task": task.strip()})
    if isinstance(result, dict):
        _show(repl, result.get("output"))
        repl.say(f"{preset}: unlicensed, {_money(float(result.get('cost_usd') or 0))}")  # type: ignore[arg-type]


@_command("/forge", "/forge <target> [--run] [--model M]",
          "Forge a harness for an environment (e.g. arc; --run builds and plays it)")
async def _forge(repl: Repl, arg: str) -> None:
    words = arg.split()
    run = "--run" in words
    model: str | None = None
    if "--model" in words:
        index = words.index("--model")
        model = words[index + 1] if index + 1 < len(words) else None
        if model is None:
            repl.say("Usage: /forge arc --run --model <profile model>", "warn")
            return
        del words[index : index + 2]
    target = " ".join(word for word in words if word != "--run")
    if not target:
        repl.say("Usage: /forge <target> [--run] [--model M], e.g. /forge arc --run", "warn")
        return
    extras = repl.conductor.extras
    if run:
        # ARC runs only on an explicit request; a local play model uses this machine's GPU.
        shown = model or "the /model choice"
        answer = await repl.frontend.ask(
            f"Build and play an ARC harness with {shown} (a local model runs on this machine's "
            "GPU)? Continue?", default="n", choices=("y", "n"))
        if answer is None or answer.strip().lower() not in ("y", "yes"):
            repl.say("Not run.")
            return
        extras["allow_arc_run"] = True
        if model is not None:
            extras["arc_play"] = {"model": model}
    try:
        result = await repl.call_tool("forge", {"target": target})
    finally:
        extras.pop("allow_arc_run", None)
        if model is not None:
            extras.pop("arc_play", None)
    if result is not None:
        _show(repl, result)


@_command("/agents", "/agents", "Installed agents, versions, license and floor")
async def _agents(repl: Repl, arg: str) -> None:
    result = await repl.call_tool("agents", {}, note=False)
    if not isinstance(result, dict):
        return
    rows = []
    for row in result.get("agents") or []:  # type: ignore[union-attr]
        if not isinstance(row, dict):
            continue
        license_ = "licensed" if row.get("licensed") else "UNLICENSED"
        floor = row.get("floor") or "-"
        rows.append((f"{row.get('name')} v{row.get('revision')}",
                     f"{license_}  floor {floor}  {row.get('preset')} on {row.get('model')}"))
    repl.rows("Installed agents", rows)


@_command("/run", "/run <agent> [input]", "Run an installed agent",
          detail="The input is JSON when it parses, otherwise a string.")
async def _run(repl: Repl, arg: str) -> None:
    name, _, raw = arg.partition(" ")
    if not name:
        repl.say("Usage: /run <agent> [json or text input]", "warn")
        return
    task: JsonValue = raw.strip()
    if raw.strip():
        try:
            task = json.loads(raw)
        except ValueError:
            if raw.strip()[:1] in "{[":
                repl.say("That input looks like JSON but does not parse; check quotes and "
                         "brackets, or pass plain text.", "error")
                return
            task = raw.strip()
    result = await repl.call_tool("run_agent", {"name": name, "input": task})
    if isinstance(result, dict):
        _show(repl, result.get("output"))
        repl.say(_run_line(name, result))


def _run_line(name: str, result: dict[str, Any]) -> str:
    """``invoice-reader: $0.0018, 3.6s, 2 model calls, chatcmpl-...`` (what was known)."""
    parts = [_money(float(result.get("cost_usd") or 0))]
    seconds = result.get("seconds")
    if isinstance(seconds, int | float) and not isinstance(seconds, bool):
        parts.append(f"{seconds:.1f}s")
    calls = result.get("calls")
    if isinstance(calls, int) and not isinstance(calls, bool):
        parts.append(f"{calls} model {'call' if calls == 1 else 'calls'}")
    completion = result.get("completion_id")
    if isinstance(completion, str) and completion:
        parts.append(completion)
    return f"{name}: {', '.join(parts)}"


@_command("/report", "/report <agent>", "An agent's test report and license")
async def _report(repl: Repl, arg: str) -> None:
    if not arg:
        repl.say("Usage: /report <agent>", "warn")
        return
    result = await repl.call_tool("report", {"name": arg.split()[0]}, note=False)
    if not isinstance(result, dict):
        return
    rows = [(key, _scalar(value)) for key, value in result.items() if key != "report"]
    repl.rows(f"Agent {arg.split()[0]}", rows)
    report = result.get("report")
    if isinstance(report, dict):
        repl.rows("Test report", [(key, _scalar(value)) for key, value in report.items()])
    else:
        repl.say("No test report.", "warn")


@_command("/recompile", "/recompile <agent>", "Force a rebuild (a fresh Stage 4)")
async def _recompile(repl: Repl, arg: str) -> None:
    from architect.spec import Lifecycle

    name = arg.split()[0] if arg else ""
    if not name:
        repl.say("Usage: /recompile <agent>", "warn")
        return
    services = repl.app.services
    try:
        incumbent = services.registry.get(name)
    except Exception:
        repl.say(f"No installed agent {name}.", "error")
        return
    bound = services.profile.compile.compile_budget.cost_usd
    if not await _confirm(repl, f"Recompiling {name}", bound):
        repl.say("Not recompiled.")
        return
    repl.say(f"Recompiling {name} (bound {_money(bound)})...")
    try:
        finished, result = await repl.work(
            services.compiler.compile(incumbent.spec, approved=True, allow_reuse=False)
        )
    except Exception as exc:
        from architect.session.codes import code_for

        repl.say(f"Recompile failed ({code_for(exc)}).", "error")
        return
    if not finished or result is None:
        return
    artifact = result.artifact
    accepted = artifact is not None and artifact.licensed and not result.failures
    if not accepted or artifact is None:
        codes = ", ".join(result.failures) or "not_licensed"
        repl.say(f"Not licensed ({codes}); the installed version is unchanged.", "warn")
        return
    if artifact.spec.lifecycle == Lifecycle.INSTALL and not result.reused:
        try:
            entry = services.registry.install(name, artifact)
        except Exception:
            repl.say("Licensed, but the registry rejected it (no improvement over the "
                     "installed version).", "warn")
            return
        repl.say(f"Installed {name} v{entry.revision}.")
    else:
        repl.say(f"Licensed ({artifact.spec.lifecycle.value}); nothing to install.")


@_command("/library", "/library", "Cached harnesses and reuse counts")
async def _library(repl: Repl, arg: str) -> None:
    library = getattr(repl.app.services.compiler, "library", None)
    root = getattr(library, "root", None)
    database = Path(root) / "library.sqlite3" if root is not None else None
    if database is None or not database.is_file():
        repl.say("The harness library is empty.")
        return
    try:
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT key, COALESCE(shape, ''), successes, failures, evicted FROM templates "
                "ORDER BY evicted, successes DESC, sequence DESC LIMIT 20"
            ).fetchall()
            total = connection.execute("SELECT COUNT(*) FROM templates").fetchone()[0]
        finally:
            connection.close()
    except sqlite3.Error:
        repl.say("The harness library could not be read.", "error")
        return
    shown = [
        (str(key)[:16], f"reused {ok}, failed {bad}{'  (evicted)' if evicted else ''}"
         + (f"  {str(shape)[:16]}" if shape else ""))
        for key, shape, ok, bad, evicted in rows
    ]
    repl.rows(f"Harness library: {total} cached", shown)


# -- compile defaults ---------------------------------------------------------------------
# Build mode owns the override rules (``architect.agent.build.set_override``); without it
# the values are kept with the same keys and minimal checks.

_FALLBACK_KEYS = ("floor_limit", "floor_file", "budget_usd", "budget_s", "compile_usd",
                  "compile_s", "model")


def _build(name: str) -> Callable[..., Any] | None:
    from .app import _optional

    return cast(Callable[..., Any] | None, _optional("architect.agent.build", name))


def _fallback_set(repl: Repl, key: str, value: object) -> str:
    if key not in _FALLBACK_KEYS:
        raise ToolError("unknown_override", key)
    overrides = _overrides(repl)
    if value is None:
        overrides.pop(key, None)
        return f"{key}: default"
    if key == "model":
        if value not in repl.app.services.profile.models:
            raise ToolError("model_not_configured", str(value))
    elif key == "floor_file":
        path = repl.conductor.context.resolve(value)
        if not path.is_file():
            raise ToolError("not_a_file", str(value))
        value = str(path)
    else:
        number = _number(str(value))
        if number is None or (key == "floor_limit" and number > 1):
            raise ToolError("invalid_override", key)
        value = number
    overrides[key] = value
    return f"{key.replace('_', ' ')} {value}"


def _set(repl: Repl, key: str, value: object) -> bool:
    """One override for the next compile; a refused value changes nothing."""
    setter = _build("set_override")
    try:
        if setter is not None:
            text = str(setter(repl.conductor.context, key, value))
        else:
            text = _fallback_set(repl, key, value)
    except ToolError as exc:
        detail = f": {exc.detail}" if exc.detail else ""
        repl.say(f"Not changed ({exc.code}{detail}).", "error")
        return False
    repl.say(f"Next compile: {text}.")
    return True


def _show_overrides(repl: Repl, title: str, prefixes: tuple[str, ...]) -> None:
    lines = _build("override_lines")
    if lines is not None:
        shown = [str(line) for line in lines(repl.conductor.context)]
        chosen = [line for line in shown if line.startswith(prefixes)] or shown
        rows = []
        for line in chosen:
            label, _, value = line.partition(": ")
            rows.append((label, value))
        repl.rows(title, rows)
        return
    overrides = _overrides(repl)
    heads = tuple(prefix.split(" ")[0] for prefix in prefixes)
    repl.rows(title, [(key, str(overrides.get(key, "inferred"))) for key in _FALLBACK_KEYS
                      if key.startswith(heads)])


@_command("/floor", "/floor [limit|add <file>|clear]", "Show or override the next floor",
          detail="Floors are inferred per request and shown on the plan card (SYNTHETIC "
          "cases are drafted when you have none); a limit between 0 and 1 (or 90%) or a "
          "JSON file of real labeled cases overrides the next compile only.")
async def _floor(repl: Repl, arg: str) -> None:
    words = arg.split(maxsplit=1)
    if not words:
        _show_overrides(repl, "Next compile floor", ("floor",))
        return
    if words[0] == "clear":
        _set(repl, "floor_limit", None)
        _set(repl, "floor_file", None)
        return
    if words[0] == "add":
        if len(words) < 2:
            repl.say("Usage: /floor add <cases file>", "warn")
            return
        _set(repl, "floor_file", words[1].strip().strip('"'))
        return
    _set(repl, "floor_limit", words[0])


@_command("/budget", "/budget [session <usd> [min] | run <usd> <s> | compile <usd> [min]]",
          "Show or change the session, per-run or compile budget",
          detail="The session budget covers every model call in this conversation (default "
          "$25 and 60 minutes). run and compile (or build) set the next build's per-run "
          "budget and its compile wallet; unset, they are inferred and shown on the plan card.")
async def _budget(repl: Repl, arg: str) -> None:
    conductor = repl.conductor
    settings = conductor.settings
    words = arg.split()
    if not words:
        elapsed = time.monotonic() - conductor.started
        repl.rows("Session budget", [
            ("spent", f"{_money(conductor.spent_usd)} of {_money(settings.session_usd)}"),
            ("time", f"{int(elapsed // 60)} of {int(settings.session_s // 60)} min"),
        ])
        _show_overrides(repl, "Next compile", ("per run", "compile", "budget"))
        return
    kind = words[0]
    if kind == "session" and len(words) in (2, 3):
        usd = _number(words[1].lstrip("$"))
        minutes = _number(words[2]) if len(words) == 3 else settings.session_s / 60
        if usd is None or minutes is None:
            repl.say("Usage: /budget session <usd> [minutes]", "warn")
            return
        settings.session_usd = usd
        settings.session_s = minutes * 60
        repl.say(f"Session budget: {_money(usd)} and {minutes:g} minutes.")
        return
    if kind == "run" and len(words) == 3:
        if _set(repl, "budget_usd", words[1]):
            _set(repl, "budget_s", words[2])
        return
    if kind in ("compile", "build") and len(words) in (2, 3):
        if _set(repl, "compile_usd", words[1]) and len(words) == 3:
            minutes = _number(words[2])
            _set(repl, "compile_s", None if minutes is None else minutes * 60)
        return
    repl.say("Usage: /budget [session <usd> [min] | run <usd> <s> | compile <usd> [min]]",
             "warn")


@_command("/model", "/model [name]", "Show or set the model (conductor and next compile)")
async def _model(repl: Repl, arg: str) -> None:
    conductor = repl.conductor
    services = repl.app.services
    models = list(services.profile.models)
    if not arg:
        overrides = _overrides(repl)
        repl.rows("Models", [
            ("conductor", conductor.adapter.model),
            ("next compile", str(overrides.get("model", "cheapest that passes"))),
            ("profile models", ", ".join(models)),
        ])
        return
    name = arg.split()[0]
    if name not in models:
        from .app import add_detected_model

        reload = conductor.extras.get("reload")
        if add_detected_model(repl.app.profile_path, name) and callable(reload):
            reloaded = reload()
            if inspect.isawaitable(reloaded):
                await reloaded
            services = repl.app.services
            repl.say(f"Added {name} to the profile.")
        else:
            repl.say(f"{name} is not in the profile. Models: {', '.join(models)}.", "error")
            return
    try:
        adapter = services.adapter_factory(name)
    except Exception:
        repl.say(f"{name} could not be loaded.", "error")
        return
    conductor.adapter = adapter
    conductor.context.model = name
    repl.say(f"Conductor model: {name}.")
    _set(repl, "model", name)


@_command("/telemetry", "/telemetry", "Cost and quality over time (content-free)")
async def _telemetry(repl: Repl, arg: str) -> None:
    try:
        rows = repl.app.services.runtime.store.rows()
    except Exception:
        repl.say("Telemetry could not be read.", "error")
        return
    if not rows:
        repl.say("No telemetry yet.")
        return
    now = time.time()
    shown = []
    for label, days in (("24 hours", 1), ("7 days", 7), ("30 days", 30), ("all", None)):
        window = [r for r in rows
                  if days is None or now - r.timestamp.timestamp() <= days * 86400]
        if not window:
            continue
        cost = sum(r.cost_usd for r in window)
        accepted = sum(1 for r in window if r.accepted)
        drift = sum(1 for r in window if r.drift_flag)
        shown.append((label, f"{len(window)} runs, {_money(cost)}, accepted "
                      f"{accepted}/{len(window)}" + (f", drift {drift}" if drift else "")))
    repl.rows("Telemetry", shown)


# -- session ------------------------------------------------------------------------------


@_command("/permissions", "/permissions [ask|auto-edit|full-auto|plan]",
          "Approval mode for the coder (plan: read-only)",
          detail="ask: every write, edit and shell command is approved. auto-edit: file "
          "writes in the working directory are allowed, shell asks. full-auto: everything "
          "inside the working directory is allowed. plan: read-only; the coder is not "
          "called and Architect proposes the change instead. Control-plane paths stay "
          "denied.")
async def _permissions(repl: Repl, arg: str) -> None:
    from .repl import set_permission

    context = repl.conductor.context
    if not arg:
        repl.say(f"Permissions: {context.permission}.")
        return
    if arg not in PERMISSIONS:
        repl.say("Permissions: ask, auto-edit, full-auto, plan.", "warn")
        return
    mode: Permission = arg
    set_permission(repl.conductor, mode)  # session allowances do not survive a change
    repl.say(f"Permissions: {mode}.")


@_command("/status", "/status", "Session state, model, workspace and spend")
async def _status(repl: Repl, arg: str) -> None:
    conductor = repl.conductor
    app = repl.app
    bound = [key for key in ("make", "forge", "coder") if key in conductor.extras]
    from .render import mode_label

    title = conductor.extras.get("session_title") or app.store.title(conductor.session_id)
    rows = [
        ("session", conductor.session_id + (f"  ({title})" if title else "")),
        ("mode", mode_label(conductor.mode)),
        ("permissions", conductor.context.permission),
        ("model", conductor.adapter.model),
        ("cwd", str(conductor.cwd)),
        ("workspace", str(app.workspace)),
        ("profile", str(app.profile_path)),
        ("spend", f"{_money(conductor.spent_usd)} of {_money(conductor.settings.session_usd)}"),
        ("messages", str(len(conductor.transcript.messages))),
        ("context", f"{app.context_percent():.0f}% of {app.context_tokens() // 1000}k tokens"),
        ("instructions", "loaded" if conductor.instructions else "none (/init drafts one)"),
        ("subagents", ", ".join(bound) or "none bound"),
    ]
    if app.unavailable:
        rows.append(("not installed", ", ".join(app.unavailable)))
    repl.rows("Status", rows)


@_command("/cost", "/cost", "Spend against the session budget")
async def _cost(repl: Repl, arg: str) -> None:
    conductor = repl.conductor
    spend = repl.app.meter.spend
    rows = [
        ("session", f"{_money(conductor.spent_usd)} of {_money(conductor.settings.session_usd)}"
         f" ({_money(conductor.remaining_usd())} left)"),
        ("calls", str(spend.calls)),
        ("tokens", f"{spend.tokens_in} in, {spend.tokens_out} out"),
    ]
    last = repl.last_turn
    if last is not None:
        rows.append(("last turn", f"{_money(last.cost_usd)}, {last.tool_calls} tool calls"))
    if repl.app.unpriced():
        rows.insert(0, ("model", f"{conductor.adapter.model}: cost unknown (no price; time is "
                        "the budget, and dollar figures leave its calls out)"))
    repl.rows("Cost (API-equivalent estimates)", rows)


@_command("/compact", "/compact", "Summarize old tool results to one line")
async def _compact(repl: Repl, arg: str) -> None:
    before = repl.app.context_percent()
    changed = repl.conductor.transcript.compact()
    repl.app.save_session()
    repl.say(f"Compacted {changed} tool result(s); context {before:.0f}% -> "
             f"{repl.app.context_percent():.0f}%.")


@_command("/clear", "/clear", "Start a fresh conversation (the old one stays saved)")
async def _clear(repl: Repl, arg: str) -> None:
    repl.app.save_session()
    repl.conductor.transcript = Transcript()
    repl.conductor.session_id = uuid.uuid4().hex[:12]
    repl.conductor.extras.pop("session_title", None)
    repl.last_turn = None
    repl.say("New conversation.")


@_command("/resume", "/resume [id]", "Continue a saved conversation (latest by default)")
async def _resume(repl: Repl, arg: str) -> None:
    repl.resume(arg.split()[0] if arg else None)


@_command("/sessions", "/sessions", "Saved conversations")
async def _sessions(repl: Repl, arg: str) -> None:
    current = repl.conductor.session_id
    rows = [
        (("* " if info.id == current else "  ") + info.id,
         f"{info.when()}  {info.messages} msgs  {info.title}")
        for info in repl.app.store.list()[:20]
    ]
    if not rows:
        repl.say("No saved conversations yet; they are saved after every turn.")
        return
    repl.rows("Saved conversations (/resume <id>)", rows)


@_command("/init", "/init", "Draft ARCHITECT.md for this directory (through the coder)")
async def _init(repl: Repl, arg: str) -> None:
    if (repl.conductor.cwd / "ARCHITECT.md").exists():
        repl.say("ARCHITECT.md exists; the coder will propose changes to it.")
    await repl.turn(INIT_REQUEST)


@_command("/bench", "/bench <verb> [args]", "The existing bench verbs (architect bench ...)")
async def _bench(repl: Repl, arg: str) -> None:
    if not arg:
        repl.say("Usage: /bench run|compare|list|pages|roles|probe ...  "
                 "(see 'architect bench --help')", "warn")
        return
    try:
        argv = shlex.split(arg, posix=True)
    except ValueError:
        repl.say("Could not parse the arguments.", "error")
        return
    if argv[0] in ("run", "list") and "--profile" not in argv:
        argv += ["--profile", str(repl.app.profile_path)]
    from architect import cli

    code = await asyncio.to_thread(cli.main, ["bench", *argv])
    if code:
        repl.say(f"bench exited with {code}.", "warn")


@_command("/exit", "/exit", "Quit (Ctrl+D too)", aliases=("/quit",))
async def _exit(repl: Repl, arg: str) -> None:
    repl.running = False


# -- Phase 13: context, checkpoints, config, diagnostics ------------------------------------

GIT_TIMEOUT_S = 15.0
DIFF_CHARS = 60_000
REVIEW_CHARS = 60_000
OLLAMA_URL = "http://localhost:11434/api/version"
ARCHITECT_TEMPLATE = (
    "# ARCHITECT.md\n\n"
    "Instructions for Architect in this project: what it is, how to build, test and lint\n"
    "it, conventions, and what an agent working here must not do.\n"
)
REVIEW_REQUEST = (
    "Review these working-tree changes like a careful senior engineer. Look for bugs, "
    "edge cases, security problems, missing tests and unclear code; cite file and line. "
    "Do not change any files: report findings, most important first, and say plainly if "
    "the change looks good."
)


def _tokens(chars: int) -> str:
    from .config import CHARS_PER_TOKEN
    from .render import tokens

    return tokens(chars // CHARS_PER_TOKEN)


@_command("/context", "/context", "What fills the model's context, and how full it is",
          detail="Sizes are estimates (4 characters per token). Old tool results are "
          "compacted automatically at the config's auto_compact_percent (85% by default).")
async def _context(repl: Repl, arg: str) -> None:
    from .conductor import SYSTEM
    from .config import CHARS_PER_TOKEN

    app = repl.app
    conductor = repl.conductor
    window = app.context_tokens()
    total = window * CHARS_PER_TOKEN
    tools = len(json.dumps([tool.listing() for tool in conductor.tools], ensure_ascii=False))
    instructions = len(conductor.instructions)
    prefix = len(conductor.system_prompt())
    by_role: dict[str, int] = {"user": 0, "assistant": 0, "tool": 0, "note": 0}
    counts: dict[str, int] = {"user": 0, "assistant": 0, "tool": 0, "note": 0}
    for message in conductor.transcript.messages:
        size = len(json.dumps(message.to_json(), ensure_ascii=False))
        by_role[message.role] = by_role.get(message.role, 0) + size
        counts[message.role] = counts.get(message.role, 0) + 1
    used = app.context_used_chars()

    def share(chars: int) -> str:
        return f"{_tokens(chars):>7} tokens  {100 * chars / max(1, total):5.1f}%"

    rows = [
        ("system prompt", share(len(SYSTEM))),
        ("tool definitions", share(tools)),
        ("project instructions", share(instructions) if instructions else "none"),
        ("other prefix", share(max(0, prefix - len(SYSTEM) - tools - instructions))),
        (f"your messages ({counts['user']})", share(by_role["user"])),
        (f"answers and tool calls ({counts['assistant']})", share(by_role["assistant"])),
        (f"tool results ({counts['tool']})", share(by_role["tool"])),
        (f"notes ({counts['note']})", share(by_role["note"])),
        ("free", share(max(0, total - used))),
    ]
    threshold = app.config.number("auto_compact_percent")
    percent = app.context_percent()
    title = (f"Context: {percent:.0f}% of {window // 1000}k tokens "
             f"({conductor.adapter.model})")
    repl.rows(title, rows)
    if threshold > 0:
        repl.text(f"Auto-compact at {threshold:.0f}%; /compact shortens old tool results now, "
                  "/clear starts a fresh conversation.")


# -- checkpoints -----------------------------------------------------------------------------


async def _git(repl: Repl, *args: str) -> tuple[int, str] | None:
    """``git <args>`` in the working directory; None when git is missing or hangs."""
    if shutil.which("git") is None:
        return None
    try:
        process = await asyncio.create_subprocess_exec(
            "git", *args, cwd=str(repl.conductor.cwd),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None
    try:
        output, _ = await asyncio.wait_for(process.communicate(), GIT_TIMEOUT_S)
    except (TimeoutError, asyncio.CancelledError):
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        return None
    return process.returncode or 0, output.decode("utf-8", errors="replace")


async def _in_git_repo(repl: Repl) -> bool:
    result = await _git(repl, "rev-parse", "--is-inside-work-tree")
    return result is not None and result[0] == 0 and result[1].strip() == "true"


async def working_diff(repl: Repl) -> tuple[str, str]:
    """(source, diff text): ``git diff HEAD`` plus untracked files in a repository, else the
    session's checkpoint diff."""
    if await _in_git_repo(repl):
        result = await _git(repl, "diff", "HEAD", "--no-color")
        if result is None or result[0] != 0:
            result = await _git(repl, "diff", "--no-color")  # a repository without commits
        text = result[1] if result is not None else ""
        untracked = await _git(repl, "ls-files", "--others", "--exclude-standard")
        if untracked is not None and untracked[1].strip():
            names = untracked[1].strip().splitlines()
            more = f"\n... {len(names) - 50} more" if len(names) > 50 else ""
            text += "\nUntracked files:\n" + "\n".join("  " + n for n in names[:50]) + more
        return "git", text
    store = repl.app.checkpoints
    if store is None:
        return "checkpoints", ""
    text = await asyncio.to_thread(store.diff, repl.conductor.session_id, repl.conductor.cwd)
    return "checkpoints", text


@_command("/diff", "/diff", "This session's file changes (git diff in a repository)",
          detail="In a git repository: git diff HEAD plus untracked files. Elsewhere: what "
          "the coder changed in this conversation, from its checkpoints.")
async def _diff(repl: Repl, arg: str) -> None:
    source, text = await working_diff(repl)
    if not text.strip():
        repl.say("No changes." if source == "git" else
                 "No changes yet: the coder has not edited files in this conversation.")
        return
    if len(text) > DIFF_CHARS:
        text = text[:DIFF_CHARS] + f"\n... [diff clipped at {DIFF_CHARS} characters]"
    repl.diff(text)


@_command("/undo", "/undo", "Restore the files the coder changed in the last turn",
          detail="Each turn's coder changes are checkpointed before they happen. /undo shows "
          "the files and asks first; run it again to step further back.")
async def _undo(repl: Repl, arg: str) -> None:
    store = repl.app.checkpoints
    session = repl.conductor.session_id
    checkpoint = store.last(session) if store is not None else None
    if store is None or checkpoint is None:
        repl.say("Nothing to undo: the coder has not changed files in this conversation.")
        return
    labels = {"file": "restore", "absent": "delete (new file)", "unsaved": "cannot restore"}
    rows = [(record.path, labels[record.state]) for record in checkpoint.files]
    repl.rows(f"Turn {checkpoint.turn} ({checkpoint.when()}): {checkpoint.request[:60]}", rows)
    answer = await repl.frontend.ask(
        f"Restore these {len(rows)} file(s) to how they were before that turn?",
        default="n", choices=("y", "n"))
    if answer is None or answer.strip().lower() not in ("y", "yes"):
        repl.say("Nothing changed.")
        return
    done = store.restore(checkpoint, repl.conductor.cwd)
    parts = []
    if done.restored:
        parts.append(f"restored {len(done.restored)}")
    if done.deleted:
        parts.append(f"deleted {len(done.deleted)}")
    repl.say("Undone: " + (", ".join(parts) or "no files changed") + ".")
    if done.skipped:
        repl.say("Could not restore (too large to snapshot, or unwritable): "
                 + ", ".join(done.skipped), "warn")
    repl.conductor.transcript.note(
        f"The user undid the coder's changes from an earlier turn ({checkpoint.request[:120]}); "
        "files restored: " + ", ".join(done.restored + done.deleted)[:1500])
    repl.app.save_session()


# -- config ----------------------------------------------------------------------------------


def _reload_config(repl: Repl) -> None:
    from .config import load_config

    repl.app.config = load_config(repl.app.workspace, repl.conductor.cwd)
    repl.conductor.extras["web_fetch_allow_private"] = repl.app.config.flag(
        "web_fetch_allow_private")


@_command("/config", "/config [set <key> <value>]", "Show or change settings (config.toml)",
          detail="Effective values come from <cwd>/.architect/config.toml over "
          "<workspace>/config.toml over defaults. /config set writes the workspace file; "
          "'/config set <key> default' removes a key.")
async def _config(repl: Repl, arg: str) -> None:
    from .config import KEYS, ConfigError, set_value, workspace_config_path

    words = arg.split(maxsplit=2)
    if not words:
        config = repl.app.config
        repl.rows("Settings ([source])", config.rows())
        project = config.project_path
        here = ""
        if project is not None:
            here = (f" and {project} (wins)" if project.is_file() else
                    f"; a project file at {project} would win")
        repl.text(f"Files: {workspace_config_path(repl.app.workspace)}{here}")
        for problem in config.errors:
            repl.say(problem, "warn")
        return
    if words[0] != "set" or len(words) < 2:
        repl.say("Usage: /config [set <key> <value>]  (keys: " + ", ".join(KEYS) + ")", "warn")
        return
    key = words[1]
    raw = words[2] if len(words) == 3 else ""
    if key not in KEYS:
        close = difflib.get_close_matches(key, list(KEYS), n=1)
        hint = f" Did you mean {close[0]}?" if close else ""
        repl.say(f"Unknown setting {key}.{hint} /config lists them.", "error")
        return
    path = workspace_config_path(repl.app.workspace)
    try:
        value = set_value(path, key, raw)
    except ConfigError as exc:
        repl.say(f"Not changed ({exc.code}{': ' + exc.detail if exc.detail else ''}).", "error")
        return
    _reload_config(repl)
    from .config import toml_value

    shown = "default" if value is None else toml_value(value).strip('"')
    repl.say(f"{key} = {shown} (saved to {path}).")
    if repl.app.config.source(key) == "project":
        repl.say(f"This project's .architect/config.toml also sets {key}, and it wins here.",
                 "warn")
        return
    conductor = repl.conductor
    effective = repl.app.config.get(key)
    if key == "permission" and isinstance(effective, str):
        from .repl import set_permission

        set_permission(conductor, cast(Permission, effective))
    elif key == "session_budget_usd":
        conductor.settings.session_usd = repl.app.config.number(key)
    elif key == "session_minutes":
        conductor.settings.session_s = repl.app.config.number(key) * 60
    elif key == "theme":
        repl.say("The theme applies the next time Architect starts.")
    elif key == "model" and isinstance(effective, str):
        repl.say("The model applies the next time Architect starts; /model switches now.")


# -- doctor ----------------------------------------------------------------------------------


async def ollama_version(url: str = OLLAMA_URL, timeout_s: float = 1.0) -> str | None:
    """Ollama's version when it answers on localhost within ``timeout_s``; never starts it."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.get(url)
        if response.status_code != 200:
            return None
        version = response.json().get("version")
        return str(version) if version else "running"
    except (httpx.HTTPError, ValueError, AttributeError):
        return None


async def provider_status(repl: Repl) -> list[tuple[str, str, bool]]:
    """(label, status, ok) per provider: the providers package's cheap checks (no model
    call; local servers other than Ollama are not probed here), else the CLIs on PATH."""
    try:
        from architect.providers.catalog import entry
        from architect.providers.status import all_statuses
    except ImportError:
        return _fallback_status()
    try:
        statuses = await asyncio.wait_for(all_statuses(repl.app.workspace, probe=False), 10.0)
    except Exception:
        return _fallback_status()
    rows: list[tuple[str, str, bool]] = []
    idle: list[str] = []
    for status in statuses:
        if status.provider == "ollama":
            continue  # its own row below, from a real reachability check
        try:
            label = entry(status.provider).label
        except Exception:
            label = status.provider
        if status.ready:
            rows.append((label, status.detail or status.state, True))
        else:
            idle.append(status.provider)
    if not rows:
        rows.append(("providers", "none ready: /login connects one", False))
    if idle:
        rows.append(("not set up", ", ".join(idle) + " (/login <provider>)", True))
    return rows


def _fallback_status() -> list[tuple[str, str, bool]]:
    from architect.session.bootstrap import detect_clis

    found = {candidate.provider for candidate in detect_clis()}
    rows = [(label, "found on PATH" if provider in found else "not found", provider in found)
            for provider, label in (("claude-code", "claude CLI"), ("codex", "codex CLI"))]
    for variable, label in (("ANTHROPIC_API_KEY", "Anthropic API"),
                            ("OPENAI_API_KEY", "OpenAI API")):
        present = bool(os.environ.get(variable))
        rows.append((label, f"{variable} set" if present else "no key", present))
    return rows


def _pi_bridge() -> str:
    import architect.backends as backends

    root = Path(backends.__file__).parent / "pi-node"
    if not (root / "bridge.mjs").is_file():
        return "missing (reinstall Architect)"
    if not (root / "node_modules" / "@mariozechner" / "pi-coding-agent" / "package.json").is_file():
        return f"dependencies missing: run 'npm ci' in {root}"
    return "ready"


@_command("/doctor", "/doctor", "Check providers, local models, workspace and disk")
async def _doctor(repl: Repl, arg: str) -> None:
    app = repl.app
    glyphs = getattr(repl.frontend.renderer, "glyphs", None)
    ok = getattr(glyphs, "ok", "+")
    bad = getattr(glyphs, "warn", "!")
    neutral = getattr(glyphs, "sub", "-")
    rows: list[tuple[str, str]] = []

    def check(label: str, good: bool | None, text: str) -> None:
        mark = neutral if good is None else ok if good else bad
        rows.append((label, f"{mark} {text}"))

    for label, text, good in await provider_status(repl):
        check(label, None if label == "not set up" else good, text)
    # The desktop app runs Pi on its own Electron binary (ARCHITECT_NODE), not on a Node install.
    builtin = os.environ.get("ARCHITECT_NODE")
    node = builtin or shutil.which("node")
    check("node", node is not None, "built into the app" if builtin else
          node or "not found: install Node.js 20+ (the coder needs it)")
    bridge = _pi_bridge()
    check("pi bridge", bridge == "ready", bridge)
    version = await ollama_version()
    check("ollama", True if version else None,
          f"running at localhost:11434 ({version})" if version else
          "not running at localhost:11434 (only needed for local models)")
    workspace = app.workspace
    writable = os.access(workspace, os.W_OK) if workspace.exists() else False
    check("workspace", writable, f"{workspace}" + ("" if writable else " (not writable)"))
    try:
        models = len(app.services.profile.models)
        check("profile", models > 0, f"{app.profile_path} ({models} model(s))")
    except Exception:
        check("profile", False, f"{app.profile_path} (could not be read)")
    try:
        free = shutil.disk_usage(workspace if workspace.exists() else Path.cwd()).free
        check("disk", free > 1 << 30, f"{free / (1 << 30):.1f} GB free")
    except OSError:
        check("disk", False, "could not be measured")
    for problem in app.config.errors:
        check("config", False, problem)
    if app.unavailable:
        check("not installed", False, ", ".join(app.unavailable))
    repl.rows("Doctor", rows)
    problems = sum(1 for _, text in rows if text.startswith(bad + " "))
    repl.say("Everything looks ready." if not problems else
             f"{problems} item(s) to look at; each says what to do.")


# -- memory, export, copy, review, title, image ---------------------------------------------


def run_editor(command: Sequence[str], path: Path) -> int:
    """Open ``path`` in the editor and wait (tests replace this)."""
    return subprocess.call([*command, str(path)])


@_command("/memory", "/memory [edit [global]]",
          "Show ARCHITECT.md, or open it (or the global one) in your editor",
          detail="Project instructions come from ARCHITECT.md (else AGENTS.md or CLAUDE.md) "
          "in this directory plus <workspace>/ARCHITECT.md for every project.")
async def _memory(repl: Repl, arg: str) -> None:
    from .conductor import INSTRUCTION_FILES, project_instructions
    from .config import editor_command

    cwd = repl.conductor.cwd
    workspace = repl.app.workspace
    words = arg.split()
    if words and words[0] == "edit":
        path = workspace / "ARCHITECT.md" if "global" in words[1:] else cwd / "ARCHITECT.md"
        if not path.exists():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(ARCHITECT_TEMPLATE, encoding="utf-8")
            except OSError:
                repl.say(f"Could not create {path}.", "error")
                return
        command = editor_command(repl.app.config.text("editor"))
        try:
            code = await asyncio.to_thread(run_editor, command, path)
        except OSError:
            repl.say(f"Could not start the editor '{command[0]}'; set one with "
                     "/config set editor <command>.", "error")
            return
        if code:
            repl.say(f"The editor exited with {code}.", "warn")
        repl.conductor.instructions = project_instructions(cwd, workspace)
        repl.say(f"Reloaded instructions from {path}.")
        return
    rows = []
    found = next((cwd / name for name in INSTRUCTION_FILES if (cwd / name).is_file()), None)
    rows.append(("project", str(found) if found else "none (/init drafts ARCHITECT.md)"))
    global_file = workspace / "ARCHITECT.md"
    rows.append(("global", str(global_file) if global_file.is_file() else "none"))
    repl.rows("Instructions", rows)
    if repl.conductor.instructions:
        text = repl.conductor.instructions
        lines = text.splitlines()
        shown = "\n".join(lines[:40]) + (f"\n... {len(lines) - 40} more lines"
                                         if len(lines) > 40 else "")
        repl.text(shown)
    repl.text("/memory edit opens the project file; /memory edit global the global one.")


def transcript_markdown(repl: Repl) -> str:
    conductor = repl.conductor
    title = conductor.extras.get("session_title") or repl.app.store.title(conductor.session_id)
    lines = [f"# {title or 'Architect conversation'}", "",
             f"Session {conductor.session_id}, model {conductor.adapter.model}, "
             f"exported {time.strftime('%Y-%m-%d %H:%M')}.", ""]
    for message in conductor.transcript.messages:
        if message.role == "user":
            lines += ["## You", "", message.text, ""]
        elif message.role == "assistant":
            if message.text:
                lines += ["## Architect", "", message.text, ""]
            for call in message.calls:
                arguments = json.dumps(call.get("arguments", {}), ensure_ascii=False)
                if len(arguments) > 300:
                    arguments = arguments[:297] + "..."
                lines.append(f"- tool `{call.get('name')}` {arguments}")
            if message.calls:
                lines.append("")
        elif message.role == "tool":
            body = message.text if len(message.text) <= 1500 else message.text[:1497] + "..."
            state = "" if message.ok else " (failed)"
            lines += [f"<details><summary>{message.name} result{state}</summary>", "",
                      "```", body, "```", "", "</details>", ""]
        else:
            lines += [f"_{message.text}_", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


@_command("/export", "/export [file]", "Save this conversation as markdown")
async def _export(repl: Repl, arg: str) -> None:
    conductor = repl.conductor
    if not conductor.transcript.messages:
        repl.say("Nothing to export yet.")
        return
    name = arg.strip().strip('"') or (
        f"architect-{conductor.session_id}-{time.strftime('%Y%m%d-%H%M')}.md")
    path = (conductor.cwd / name).resolve()
    if path.exists():
        answer = await repl.frontend.ask(f"{path.name} exists. Overwrite?", default="n",
                                         choices=("y", "n"))
        if answer is None or answer.strip().lower() not in ("y", "yes"):
            repl.say("Not exported.")
            return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(transcript_markdown(repl), encoding="utf-8")
    except OSError:
        repl.say(f"Could not write {path}; try another path.", "error")
        return
    repl.say(f"Exported to {path}.")


def clipboard_command() -> list[str] | None:
    """The platform's clipboard writer, if one is installed."""
    if sys.platform == "win32":
        return ["clip"] if shutil.which("clip") else None
    if sys.platform == "darwin":
        return ["pbcopy"] if shutil.which("pbcopy") else None
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        return ["wl-copy"]
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard"]
    if shutil.which("xsel"):
        return ["xsel", "--clipboard", "--input"]
    return None


def copy_to_clipboard(text: str) -> bool:
    command = clipboard_command()
    if command is None:
        return False
    data = text.encode("utf-16") if command == ["clip"] else text.encode("utf-8")
    try:
        result = subprocess.run(command, input=data, timeout=10, check=False,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _last_answer(repl: Repl) -> str | None:
    if repl.last_turn is not None and repl.last_turn.text.strip():
        return repl.last_turn.text
    for message in reversed(repl.conductor.transcript.messages):
        if message.role == "assistant" and message.text.strip():
            return message.text
    return None


@_command("/copy", "/copy", "Copy the last answer to the clipboard")
async def _copy(repl: Repl, arg: str) -> None:
    text = _last_answer(repl)
    if text is None:
        repl.say("No answer to copy yet.")
        return
    if await asyncio.to_thread(copy_to_clipboard, text):
        repl.say(f"Copied the last answer ({len(text)} characters).")
    else:
        repl.say("No clipboard tool found (clip, pbcopy, wl-copy, xclip or xsel); "
                 "/export saves the conversation to a file instead.", "warn")


@_command("/review", "/review [focus]", "Ask Architect to review the working-tree changes")
async def _review(repl: Repl, arg: str) -> None:
    source, text = await working_diff(repl)
    if not text.strip():
        repl.say("No changes to review." if source == "git" else
                 "No changes to review: this is not a git repository and the coder has not "
                 "edited files in this conversation.")
        return
    if len(text) > REVIEW_CHARS:
        text = text[:REVIEW_CHARS] + f"\n... [diff clipped at {REVIEW_CHARS} characters]"
    focus = f"\nFocus on: {arg}" if arg else ""
    label = "git diff HEAD" if source == "git" else "changes since this conversation began"
    await repl.turn(f"{REVIEW_REQUEST}{focus}\n\n<diff source=\"{label}\">\n{text}\n</diff>")


@_command("/title", "/title [name]", "Show or name this conversation")
async def _title(repl: Repl, arg: str) -> None:
    from .sessions import clean_title

    conductor = repl.conductor
    if not arg:
        title = conductor.extras.get("session_title") or repl.app.store.title(
            conductor.session_id)
        repl.say(f"Title: {title}." if title else
                 "Untitled (the first message names it); /title <name> sets one.")
        return
    conductor.extras["session_title"] = clean_title(arg)
    repl.app.save_session()
    repl.say(f"Title: {conductor.extras['session_title']}.")


@_command("/image", "/image <path> [message]",
          "Attach an image to your next message (or send it now with a message)",
          detail="PNG, JPEG, WebP or GIF inside the working directory; @image.png in a "
          "message does the same.")
async def _image(repl: Repl, arg: str) -> None:
    from .repl import _inside, image_marker, load_image

    raw, _, message = arg.strip().partition(" ")
    if not raw:
        repl.say("Usage: /image <path> [message]", "warn")
        return
    raw = raw.strip('"').lstrip("@")
    root = repl.conductor.cwd.resolve()
    path = _inside(root, raw)
    image = load_image(path) if path is not None and path.is_file() else None
    if path is None or image is None:
        repl.say(f"{raw} is not a PNG, JPEG, WebP or GIF image inside the working directory.",
                 "error")
        return
    rel = path.relative_to(root).as_posix()
    repl.pending_images.append((rel, image))
    if message.strip():
        await repl.turn(message.strip())
        return
    repl.say(f"Attached {image_marker(rel, image)}; it goes with your next message.")


# Phase 13: per-package command modules register themselves when present, so the
# providers, Forge and MCP packages never edit this shared registry.
def _load_command_modules() -> None:
    import importlib

    for module in ("commands_providers", "commands_forge", "commands_mcp"):
        try:
            importlib.import_module(f"{__package__}.{module}")
        except ModuleNotFoundError as exc:
            if exc.name != f"{__package__}.{module}":
                raise


_load_command_modules()
