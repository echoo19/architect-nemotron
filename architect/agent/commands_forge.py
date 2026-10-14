"""Build mode's slash commands (Phase 13, Section 7): ``/forge``, ``/export`` and ``/serve``.

``commands.py`` imports this module after its own registry is built, so ``/forge`` here
replaces the Phase 12 one (its ``/forge arc`` form is kept exactly):

- ``/forge <description>``: one request in build mode (like ``/make``, with Forge kept on
  even when the "preset" engineer was chosen).
- ``/forge @<spec.json>``: a frozen build (Phase 14) of exactly the Spec in that file,
  with Forge kept on unless the Spec declares checks (its plan then says Forge stays off).
- ``/forge arc [--run] [--model M]``: the ARC-AGI harness; nothing runs without ``--run``
  and an explicit yes (a local play model uses this machine's GPU).
- ``/export <agent> [path]``: put an installed agent into software (``agent.export``):
  staged by the guarded export, shown, approved, placed, verified, with call snippets.
  Any other ``/export`` argument goes to the ``/export`` registered before this module
  (the conversation export), when there is one.
- ``/serve [port|stop]``: the existing HTTP service (``python -m architect.server``) as a
  managed background process bound to 127.0.0.1 with a fresh bearer token; ``/serve stop``
  and the end of the process stop it. It runs out of process on purpose: the service owns
  its own Services, SQLite catalog and event loop, and must not share the CLI's.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import JsonValue

from .build import is_arc
from .commands import COMMANDS, Command, command, frozen_build, frozen_path
from .export import DEFAULT_PORT, KEY_ENV, TOKEN_ENV, export_agent
from .tools import ToolError

if TYPE_CHECKING:
    from .repl import Repl

__all__ = ["Served", "running_server", "server_command", "stop_servers"]

HOST = "127.0.0.1"
READY_S = 30.0
STOP_S = 5.0
RESULT_CHARS = 4000
_LOG_TAIL = 4096
_ARC_SUBJECT = re.compile(r"arc(?:-?agi(?:-?\d)?)?", re.IGNORECASE)
_ARC_NAMED = re.compile(r"\barc-?agi", re.IGNORECASE)
ROUTES = "POST /compile, POST /run, GET /status/{artifact_id}, POST /recompile, POST /route"


def _show(repl: Repl, value: JsonValue) -> None:
    text = value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)
    if len(text) > RESULT_CHARS:
        text = text[:RESULT_CHARS] + "\n... [clipped]"
    repl.text(text)


# -- /forge -------------------------------------------------------------------------------


def _arc_form(target: str, flags: bool) -> bool:
    """``/forge arc ...`` (today's ARC behaviour) versus ``/forge <description>``."""
    words = target.split()
    if not words or not is_arc(target):
        return False
    return flags or _ARC_SUBJECT.fullmatch(words[0]) is not None or bool(
        _ARC_NAMED.search(target)
    )


@command(
    "/forge",
    "/forge <description> | /forge @<spec.json> | /forge arc [--run] [--model M]",
    "Build mode for one request with Forge on: a specialized harness, tested and licensed",
    detail="Build mode (Shift+Tab) for one request with Forge on: a short interview (first: "
    "run it here, or put it into software), a plan card with the inferred floor and budget, "
    "then Forge engineers a harness for this one task that Stage 4 licenses. "
    "/forge @<spec.json> compiles exactly the Spec in that file with Forge on, unless it "
    "declares checks. /forge arc explains the ARC-AGI harness; --run builds and plays it "
    "after an explicit yes (a local play model uses this machine's GPU).",
)
async def _forge(repl: Repl, arg: str) -> None:
    path = frozen_path(arg)
    if path is not None:
        await frozen_build(repl, path, forge=True)
        return
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
        repl.say("Usage: /forge <what the agent should do>, or /forge arc [--run] [--model M]",
                 "warn")
        return
    if not _arc_form(target, run or model is not None):
        await _forge_request(repl, arg.strip())
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


async def _forge_request(repl: Repl, description: str) -> None:
    """One turn in build mode with Forge kept on."""
    extras = repl.conductor.extras
    extras["force_forge"] = True
    try:
        await repl.turn(description, mode="build")
    finally:
        extras.pop("force_forge", None)


# -- /export ------------------------------------------------------------------------------

# The /export registered before this module (Package C's conversation export), if any.
_PREVIOUS_EXPORT: Command | None = COMMANDS.get("/export")


def _installed(repl: Repl, name: str) -> bool:
    try:
        return any(entry.name == name for entry in repl.app.services.registry.list())
    except Exception:
        return False


@command(
    "/export",
    "/export <agent> [path]"
    + (f" | {_PREVIOUS_EXPORT.usage}" if _PREVIOUS_EXPORT is not None else ""),
    "Put an installed agent into software: a tested package in this repository"
    + (f" (or: {_PREVIOUS_EXPORT.summary.lower()})" if _PREVIOUS_EXPORT is not None else ""),
    detail="Stages the licensed agent with the guarded export, shows every file and the "
    "destination (default agents/<name>/; a path ending in / or an existing directory is "
    "the parent), and asks before writing anything. Then it verifies the placed package and "
    "prints a Python snippet, an HTTP snippet and the test command. The feature flag stays "
    "off until a human has reviewed the package; nothing existing is ever overwritten."
    + (
        f" Without an installed agent's name: {_PREVIOUS_EXPORT.usage} "
        f"({_PREVIOUS_EXPORT.summary.lower()})."
        if _PREVIOUS_EXPORT is not None
        else ""
    ),
)
async def _export(repl: Repl, arg: str) -> None:
    words = arg.split(maxsplit=1)
    if not words or not _installed(repl, words[0]):
        if _PREVIOUS_EXPORT is not None and (not words or not _looks_like_agent(words[0])):
            await _PREVIOUS_EXPORT.handler(repl, arg)
            return
        if not words:
            repl.say("Usage: /export <agent> [path]  (/agents lists installed agents)", "warn")
            return
        also = f"; {_PREVIOUS_EXPORT.usage} is the other form" if _PREVIOUS_EXPORT else ""
        repl.say(f"No installed agent {words[0]}. /agents lists them{also}.", "error")
        return
    name = words[0]
    path = words[1].strip() if len(words) > 1 else None
    try:
        finished, result = await repl.work(export_agent(name, repl.conductor.context, path))
    except ToolError as exc:
        detail = f": {exc.detail}" if exc.detail else ""
        repl.say(f"Not exported ({exc.code}{detail}).", "error")
        return
    if not finished or result is None:
        return
    summary = {key: result.get(key) for key in ("exported", "agent", "path", "codes")}
    repl.conductor.transcript.note(
        "The user ran /export: " + json.dumps(summary, ensure_ascii=False)[:1000]
    )
    if result.get("exported"):
        repl.say(f"Exported {name} to {result.get('path')}/ ({result.get('verified')}).")
        repl.say(f"Next: {result.get('next_step')}.")


def _looks_like_agent(word: str) -> bool:
    """A bare kebab/snake name (not a file such as notes.md) reads as an agent name."""
    return re.fullmatch(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", word) is not None and "." not in word


# -- /serve -------------------------------------------------------------------------------


@dataclass
class Served:
    """One managed ``architect.server`` process."""

    process: subprocess.Popen[bytes]
    port: int
    token: str
    log: Path
    command: list[str]
    started: float

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}"

    def alive(self) -> bool:
        return self.process.poll() is None


_LIVE: list[Served] = []


def server_command(profile: Path, port: int) -> list[str]:
    """The service as this interpreter runs it (tests replace this function)."""
    return [
        sys.executable, "-m", "architect.server", "--profile", str(profile),
        "--host", HOST, "--port", str(port),
    ]


def _end_tree(process: subprocess.Popen[bytes]) -> None:
    """End the service and its children. On Windows a virtual environment's
    ``python.exe`` is a launcher that runs the real interpreter as a child, so ending
    only the launcher would leave the service listening."""
    if os.name == "nt":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=STOP_S, check=False,
            )
    else:
        import signal

        killpg = getattr(os, "killpg", None)  # POSIX only
        if killpg is not None:
            with contextlib.suppress(OSError):
                killpg(process.pid, signal.SIGTERM)  # its own session: the whole group
    if process.poll() is None:
        process.terminate()


def _stop(served: Served) -> None:
    process = served.process
    if process.poll() is None:
        _end_tree(process)
        try:
            process.wait(timeout=STOP_S)
        except subprocess.TimeoutExpired:
            process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=STOP_S)
    if served in _LIVE:
        _LIVE.remove(served)


def stop_servers() -> int:
    """Stop every service this process started; returns how many were running."""
    count = 0
    for served in list(_LIVE):
        count += served.alive()
        _stop(served)
    return count


atexit.register(stop_servers)


def running_server(repl: Repl) -> Served | None:
    served = repl.conductor.extras.get("serve")
    return served if isinstance(served, Served) else None


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((HOST, port))
        except OSError:
            return False
    return True


def _connects(port: int) -> bool:
    try:
        with socket.create_connection((HOST, port), timeout=0.25):
            return True
    except OSError:
        return False


def _token() -> str:
    """``ARCHITECT_API_TOKEN`` when it is a valid token, else a fresh random one."""
    supplied = os.environ.get(TOKEN_ENV, "")
    valid = (
        32 <= len(supplied) <= 512
        and supplied.isascii()
        and supplied.isprintable()
        and not any(character.isspace() for character in supplied)
    )
    return supplied if valid else secrets.token_urlsafe(32)


def _failure(log: Path) -> str:
    """The service's own fixed code from its log (``{"error": code}``), else a fallback."""
    try:
        data = log.read_bytes()[-_LOG_TAIL:].decode("utf-8", errors="replace")
    except OSError:
        return "server_failed"
    for line in reversed(data.splitlines()):
        with contextlib.suppress(ValueError):
            value = json.loads(line)
            if isinstance(value, dict) and isinstance(value.get("error"), str):
                code = str(value["error"])
                if re.fullmatch(r"[a-z0-9_]{1,64}", code):
                    return code
    return "server_failed"


def _spawn(
    command: list[str], env: dict[str, str], log: Path, cwd: Path
) -> subprocess.Popen[bytes]:
    options: dict[str, Any] = {}
    if os.name == "nt":
        # Its own process group: a Ctrl+C in the CLI interrupts a turn, not the service.
        options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )
    else:
        options["start_new_session"] = True
    with log.open("wb") as handle:
        return subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
            env=env, cwd=str(cwd), **options,
        )


async def _wait_ready(served: Served, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not served.alive():
            return False
        if await asyncio.to_thread(_connects, served.port):
            return True
        await asyncio.sleep(0.1)
    return False


def _agent_names(repl: Repl) -> list[str]:
    try:
        return [entry.name for entry in repl.app.services.registry.list()]
    except Exception:
        return []


def _serve_rows(repl: Repl, served: Served) -> list[tuple[str, str]]:
    names = _agent_names(repl)
    profile = repl.app.profile_path
    first = names[0] if names else "<agent>"
    try:
        spec = str(repl.app.services.registry.status(first).directory / "spec.json")
    except Exception:
        spec = f"<{first} directory>/spec.json"
    state = "running" if served.alive() else f"stopped (exit {served.process.poll()})"
    return [
        ("url", f"{served.url}  ({state}, pid {served.process.pid}, loopback only)"),
        ("token", served.token),
        ("auth", f"every request: Authorization: Bearer <token> (or ${TOKEN_ENV})"),
        ("routes", ROUTES),
        ("agents", ", ".join(names) if names else "none installed yet (/make or /forge)"),
        (
            "first call",
            "the service runs agents it licensed itself: POST /compile with "
            f'{{"spec": <{spec}>, "approved": true}} once per agent (a fresh Stage 4 on this '
            'host) returns an artifact_id; then POST /run {"artifact_id": ..., "input": ..., '
            '"enabled": true, "human_reviewed": true}',
        ),
        ("by hand", f'{TOKEN_ENV}=<token> uv run python -m architect.server --profile '
                    f'"{profile}" --port {served.port}'),
        ("log", str(served.log)),
        ("stop", "/serve stop (it also stops when Architect exits)"),
    ]


@command(
    "/serve",
    "/serve [port|stop]",
    "Serve installed agents over local HTTP (127.0.0.1, bearer token)",
    detail=f"Starts the Architect HTTP service ({ROUTES}) for this profile as a background "
    f"process on 127.0.0.1 (default port {DEFAULT_PORT}) with a fresh bearer token, shows "
    "the token and how to call it, and stops it on /serve stop or when Architect exits. "
    "/run keeps working in the CLI.",
)
async def _serve(repl: Repl, arg: str) -> None:
    word = arg.split()[0].lower() if arg.split() else ""
    served = running_server(repl)
    if word == "stop":
        if served is None:
            repl.say("The HTTP service is not running.")
            return
        await asyncio.to_thread(_stop, served)
        repl.conductor.extras.pop("serve", None)
        repl.say(f"Stopped the HTTP service on port {served.port}.")
        return
    if served is not None:
        if served.alive():
            if word and word != str(served.port):
                repl.say(f"Already serving on port {served.port}; /serve stop first.", "warn")
                return
            repl.rows("HTTP service", _serve_rows(repl, served))
            return
        repl.say(f"The HTTP service on port {served.port} had stopped "
                 f"({_failure(served.log)}); starting it again.", "warn")
        repl.conductor.extras.pop("serve", None)
        _stop(served)
    port = DEFAULT_PORT
    if word:
        if not word.isdigit() or not 1024 <= int(word) <= 65535:
            repl.say("Usage: /serve [port|stop]  (a port from 1024 to 65535)", "warn")
            return
        port = int(word)
    await _start(repl, port)


async def _start(repl: Repl, port: int) -> None:
    if not await asyncio.to_thread(_port_free, port):
        repl.say(f"Port {port} on {HOST} is in use; try /serve <other port>.", "error")
        return
    profile = repl.app.profile_path
    workspace = repl.app.workspace
    directory = workspace / "serve"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        repl.say(f"Could not create {directory}.", "error")
        return
    log = directory / f"server-{port}.log"
    token = _token()
    env = {**os.environ, TOKEN_ENV: token}
    key = os.environ.get(KEY_ENV)
    if key:
        env[KEY_ENV] = key
    command_line = server_command(profile, port)
    try:
        process = await asyncio.to_thread(_spawn, command_line, env, log, repl.conductor.cwd)
    except OSError:
        repl.say("The HTTP service could not be started (server_failed).", "error")
        return
    served = Served(process, port, token, log, command_line, time.monotonic())
    _LIVE.append(served)
    repl.say(f"Starting the HTTP service on {served.url} ...")
    if not await _wait_ready(served, READY_S):
        code = "server_not_ready" if served.alive() else _failure(log)
        await asyncio.to_thread(_stop, served)
        repl.say(f"The HTTP service did not start ({code}); log: {log}", "error")
        return
    repl.conductor.extras["serve"] = served
    repl.rows("HTTP service", _serve_rows(repl, served))
