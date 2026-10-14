"""JSON lines over stdio between the desktop app and one Architect conversation.

One bridge process serves one conversation (the app starts one per open tab, so tabs run
in parallel). Every line is one JSON object.

The app sends requests ``{"id": n, "method": "...", "params": {...}}`` and gets exactly one
``{"id": n, "result": ...}`` or ``{"id": n, "error": {"code": "...", "message": "..."}}``
back. The bridge sends, unprompted:

* ``{"type": "event", "event": {...}, "at"}``: what the conductor or a command shows (the
  ``architect.agent.events`` values, plus ``text``, ``rows`` and ``diff`` blocks);
* ``{"type": "ask", "ask": {...}}``: a question, an approval or a secret the app must answer
  with the ``answer`` method (``None`` cancels, as Esc does in the terminal), then
  ``{"type": "ask_closed", "id", "answer", "at"}`` once it is settled (``answer`` is what
  came back, absent for a secret and when nothing came);
* ``{"type": "state", "state": {...}}``: the status the terminal shows in its toolbar,
  after anything that may have changed it, a build's progress lines included (its spend);
* ``{"type": "turn_started", "text", "quiet", "at"}`` and ``{"type": "turn_finished", "text",
  "quiet", "seconds", "at"}`` around each input; ``turn_started`` is the only user-message
  signal (``quiet`` inputs, e.g. slash commands run from controls, get no user bubble).
  ``at`` is Unix seconds: a page that re-attaches, or a reopened conversation, folds each
  message at its own time, so a build's fold line keeps the time from Build it, not from
  the message that asked for the build;
* ``{"type": "forge", "data": {...}, "at"}``: live compile and Forge data while a build runs
  (``kind`` ``stage``: the stage, "model k of n", the ladder and the rung's practice
  scores; ``kind`` ``tool``: a Forge tool's bounded, tuning-only result with the remaining
  budget and, when it changed, the harness draft it worked on, or ``forge_session``
  start/end). Nothing from the exam is in it: each rung's signed exam counts arrive with
  the result card (``plan_card`` ``data``) after the compile returns. ``at`` (Unix
  seconds, as above) is when the bridge saw it, so each rung's time survives a replay;
* ``{"type": "ready"}`` once, when the bridge can take requests.

Event kinds besides the conductor's: ``output`` (text a command printed to Python's
stdout, e.g. ``/bench``) and ``link`` (a URL to open, e.g. finishing the ChatGPT sign-in).

Input goes through ``Repl.handle``, so plain text is a turn, ``/name`` a slash command and
``!cmd`` a local shell command, exactly as in the terminal. Approvals keep the terminal's
rules: anything unclear is ``no``, and no answer in five minutes is ``no``.

The process's own stdout and stdin are taken for the protocol; file descriptors 1 and 0
are pointed at stderr and an empty input so that nothing else (a subprocess, a stray
print) can write into or read from the protocol stream. While serving, Python's
``sys.stdout`` is a line writer that turns prints into ``output`` events; the protocol
writer never goes through it.

Structured reads and writes (providers, models, config, MCP, presets, profile, build
overrides, agents, images, ARCHITECT.md, first-run setup) live in ``architect.desktop.data``.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import io
import json
import os
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TextIO, TypeVar, cast

from architect import __version__

from ..agent.app import App, AppOptions, BootError, agent_workspace, boot, close_mcp
from ..agent.config import load_config
from ..agent.events import (
    Approval,
    AssistantText,
    Event,
    Notice,
    PlanCard,
    Progress,
    SubagentActivity,
    TextDelta,
    Thinking,
    ToolFinished,
    ToolStarted,
)
from ..agent.render import Glyphs
from ..agent.repl import (
    APPROVAL_TIMEOUT_S,
    Repl,
    approval_answer,
    cycle_mode,
    cycle_state,
    set_permission,
)
from ..agent.sessions import clean_title
from ..agent.tools import Permission
from ..agent.transcript import Transcript
from .data import BridgeData, BridgeError, _Run, jsonable
from .views import ViewLog

__all__ = ["Bridge", "BridgeError", "DesktopFrontend", "DesktopRenderer", "encode_event",
           "main"]

_T = TypeVar("_T")
PERMISSIONS = ("ask", "auto-edit", "full-auto", "plan")
# The app's keys for the Shift+Tab states. Build mode keeps the key "forge", which the app
# and its saved conversations use, and the app labels it Build; "build" is accepted too.
STATES = ("normal", "forge", "plan")
STATE_LABELS = {"normal": "normal", "forge": "build", "build": "build", "plan": "plan"}
MENTION_LIMIT = 50
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
              ".mypy_cache", ".pytest_cache", ".ruff_cache", ".architect"}

Send = Callable[[dict[str, Any]], None]


def encode_event(event: Event) -> dict[str, Any]:
    """An ``architect.agent.events`` value as JSON: its fields plus ``kind``."""
    kinds: dict[type, str] = {
        Thinking: "thinking",
        TextDelta: "delta",
        AssistantText: "answer",
        ToolStarted: "tool_started",
        ToolFinished: "tool_finished",
        SubagentActivity: "subagent",
        Progress: "progress",
        Notice: "notice",
        PlanCard: "plan_card",
    }
    data = dataclasses.asdict(event)
    if isinstance(event, PlanCard):
        data["rows"] = [list(row) for row in event.rows]
    data["kind"] = kinds.get(type(event), type(event).__name__.lower())
    return data


def _state_key(label: str) -> str:
    """The app's key (``STATES``) for the mode label the terminal shows."""
    return "forge" if label == "build" else label


# The window draws its own structure, so command text keeps only glyphs that carry
# meaning (a check, a cross, arrows) and never a decorative dot.
DESKTOP_GLYPHS = Glyphs(tool="", result="", sub="-", sep="  ", ellipsis="…", ok="✓", fail="✗",
                        warn="!", answer="", up="↑", down="↓")


class DesktopRenderer:
    """What commands print, as structured events (the terminal renderers' interface)."""

    def __init__(self, send: Send) -> None:
        self._send = send
        self.glyphs = DESKTOP_GLYPHS

    def _event(self, data: dict[str, Any]) -> None:
        self._send({"type": "event", "event": data, "at": round(time.time(), 3)})

    def show(self, event: Event) -> None:
        self._event(encode_event(event))

    def rows(self, title: str, rows: Sequence[tuple[str, str]]) -> None:
        self._event({"kind": "rows", "title": title, "rows": [[str(a), str(b)] for a, b in rows]})

    def text(self, text: str, style: str = "") -> None:
        self._event({"kind": "text", "text": text, "style": style})

    def diff(self, text: str) -> None:
        self._event({"kind": "diff", "text": text})


SIDE_TOOLS = frozenset({"read_file", "list_dir", "glob", "grep"})
SIDE_NOTE = (
    "The user is asking a side question about this conversation. Answer it briefly from what "
    "is above, reading files if needed. Change nothing, and do not continue the main task."
)


class SideUI:
    """The UI of a side question: its events go to the side panel, and it never asks."""

    def __init__(self, send: Send, side_id: int) -> None:
        self._send = send
        self.side_id = side_id

    def emit(self, event: Event) -> None:
        self._send({"type": "side", "id": self.side_id, "event": encode_event(event)})

    async def ask(
        self, question: str, *, default: str | None = None, choices: Sequence[str] = ()
    ) -> str | None:
        return None

    async def approve(self, action: str, detail: str) -> Approval:
        return "no"


class DesktopFrontend:
    """``Frontend`` for the bridge: questions become ``ask`` messages the app answers."""

    approve_timeout_s: float = APPROVAL_TIMEOUT_S

    def __init__(self, send: Send) -> None:
        self._send = send
        self.renderer = DesktopRenderer(send)
        self.repl: Repl | None = None
        self.current: asyncio.Task[Any] | None = None
        self._asks: dict[int, asyncio.Future[str | None]] = {}
        self._next = 0
        # Called after each progress line (the bridge sends the state then).
        self.on_progress: Callable[[], None] | None = None

    def attach(self, repl: Repl) -> None:
        self.repl = repl

    def emit(self, event: Event) -> None:
        self.renderer.show(event)
        if isinstance(event, Progress) and self.on_progress is not None:
            self.on_progress()

    # -- questions ----------------------------------------------------------------------

    async def _ask(self, payload: dict[str, Any], wait_s: float | None = None) -> str | None:
        self._next += 1
        ask_id = self._next
        future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        self._asks[ask_id] = future
        self._send({"type": "ask", "ask": {"id": ask_id, **payload}})
        try:
            if wait_s is None:
                return await future
            return await asyncio.wait_for(asyncio.shield(future), wait_s)
        finally:
            self._asks.pop(ask_id, None)
            closed: dict[str, Any] = {"type": "ask_closed", "id": ask_id,
                                      "at": round(time.time(), 3)}
            # What was answered, so a page that re-attaches keeps it on the closed card;
            # never a secret's value, and nothing when no answer came (a timeout).
            if payload.get("kind") != "secret" and future.done() and not future.cancelled():
                closed["answer"] = future.result()
            self._send(closed)

    def answer(self, ask_id: int, value: str | None) -> bool:
        future = self._asks.get(ask_id)
        if future is None or future.done():
            return False
        future.set_result(value)
        return True

    def cancel_asks(self) -> None:
        for future in list(self._asks.values()):
            if not future.done():
                future.set_result(None)

    def pending(self) -> list[int]:
        return sorted(self._asks)

    async def ask(
        self, question: str, *, default: str | None = None, choices: Sequence[str] = ()
    ) -> str | None:
        answer = await self._ask({"kind": "question", "question": question,
                                  "default": default, "choices": list(choices)})
        if answer is None:
            return None
        return answer.strip() or (default if default is not None else "")

    async def approve(self, action: str, detail: str) -> Approval:
        try:
            answer = await self._ask({"kind": "approval", "action": action, "detail": detail},
                                     self.approve_timeout_s)
        except TimeoutError:
            self.emit(Notice("No answer in 5 minutes: not allowed.", "warn"))
            return "no"
        return approval_answer(answer)

    async def ask_secret(self, question: str) -> str | None:
        """A secret (an API key): sent once, never echoed into the conversation."""
        return await self._ask({"kind": "secret", "question": question})

    # -- the running task -----------------------------------------------------------------

    async def read_line(self) -> str | None:  # the bridge drives Repl.handle directly
        return None

    async def watch(self, task: asyncio.Task[_T]) -> None:
        """Wait for ``task``; the ``cancel`` request cancels it (Esc in the terminal)."""
        previous = self.current
        self.current = task
        try:
            while not task.done():
                try:
                    await asyncio.wait({task})
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None:
                        current.uncancel()
                    task.cancel()
        finally:
            self.current = previous

    def cancel(self) -> bool:
        self.cancel_asks()
        if self.current is not None and not self.current.done():
            self.current.cancel()
            return True
        return False


# -- the protocol loop ------------------------------------------------------------------


class Bridge(BridgeData):
    def __init__(self, write: Callable[[str], None], *, adapter: Any = None) -> None:
        self._write = write
        self._adapter = adapter  # tests: a scripted model in place of the profile's
        self._lock = threading.Lock()
        # What the conversation shows, saved beside its transcript (from boot on).
        self.view: ViewLog | None = None
        self.frontend = DesktopFrontend(self.send)
        self.app: App | None = None
        self.repl: Repl | None = None
        self.cwd = Path.cwd()
        self.inbox: asyncio.Queue[tuple[str, bool]] = asyncio.Queue()
        self._sides: dict[int, asyncio.Task[None]] = {}
        # A running retake or practice run, by agent folder, and what the last one of
        # each said, until the files change.
        self._retakes: dict[str, _Run] = {}
        self._retake_results: dict[str, dict[str, Any]] = {}
        self._side_next = 0
        self.busy = False
        self.stopping = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._booting = False
        self._quiet_queued: Counter[str] = Counter()  # queued texts sent with quiet=True
        self._signed = {}
        self.host = self.make_host()  # provider seams; the login runner needs no terminal
        self._observing = False
        self.frontend.on_progress = self._progress

    # -- output ---------------------------------------------------------------------------

    def send(self, message: dict[str, Any]) -> None:
        """Write one protocol line. Safe from any thread (one locked write per line)."""
        line = json.dumps(message, ensure_ascii=False, default=str)
        with self._lock:
            self._write(line + "\n")
        if self.view is not None:
            self.view.record(message, line)

    def _on_stage(self, data: dict[str, Any]) -> None:
        self.send({"type": "forge", "data": jsonable({"kind": "stage", **data}),
                   "at": round(time.time(), 3)})

    def _on_forge_result(self, data: dict[str, Any]) -> None:
        self.send({"type": "forge", "data": jsonable({"kind": "tool", **data}),
                   "at": round(time.time(), 3)})

    def _observe(self, on: bool) -> None:
        """Subscribe to (or leave) the compiler's stage and Forge result observers."""
        from ..compiler import add_stage_observer, remove_stage_observer
        from ..compiler.forge_tools import add_result_observer, remove_result_observer

        if on and not self._observing:
            add_stage_observer(self._on_stage)
            add_result_observer(self._on_forge_result)
        elif not on and self._observing:
            remove_stage_observer(self._on_stage)
            remove_result_observer(self._on_forge_result)
        self._observing = on

    def push_state(self) -> None:
        if self.repl is not None:
            self.send({"type": "state", "state": self.snapshot()})

    def _progress(self) -> None:
        """After a progress line, the state too: a build's stage lines (one per stage and
        every 30 s) come while the session meter counts its calls, so the conversation's
        spend in the app follows the build instead of jumping when it ends. A state that
        cannot be read now is skipped; the build never sees the error."""
        with contextlib.suppress(Exception):
            self.push_state()

    # -- requests -------------------------------------------------------------------------

    async def handle(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            self.send({"id": None, "error": {"code": "invalid_json", "message": ""}})
            return
        if not isinstance(message, dict):
            return
        request_id = message.get("id")
        method = str(message.get("method", ""))
        params = message.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        handler = getattr(self, "m_" + method.replace("-", "_").replace(".", "_"), None)
        if handler is None:
            self.send({"id": request_id, "error": {"code": "unknown_method", "message": method}})
            return
        try:
            result = await handler(**params)
        except BridgeError as exc:
            self.send({"id": request_id, "error": {"code": exc.code, "message": exc.message}})
            return
        except BootError as exc:
            self.send({"id": request_id, "error": {"code": exc.code, "message": ""}})
            return
        except TypeError as exc:
            self.send({"id": request_id, "error": {"code": "invalid_params",
                                                   "message": str(exc)[:200]}})
            return
        except Exception as exc:  # provider and file errors may carry secrets: a code only
            from architect.session.codes import code_for

            self.send({"id": request_id, "error": {"code": code_for(exc), "message": ""}})
            return
        self.send({"id": request_id, "result": result})

    def _need(self) -> Repl:
        if self.repl is None:
            raise BridgeError("not_started", "Call boot first.")
        return self.repl

    async def m_ping(self) -> dict[str, Any]:
        return {"version": __version__, "pid": os.getpid()}

    async def m_boot(
        self,
        cwd: str | None = None,
        workspace: str | None = None,
        profile: str | None = None,
        model: str | None = None,
        permission: str | None = None,
        resume: str | None = None,
        continue_latest: bool = False,
        probe: bool = True,
    ) -> dict[str, Any]:
        """Start the conversation (first-run setup asks through ``ask`` messages)."""
        if self.repl is not None:
            return self.snapshot()
        if self._booting:
            raise BridgeError("busy", "Already starting.")
        self._booting = True
        try:
            self.cwd = _resolve(cwd) if cwd else Path.cwd()
            if not self.cwd.is_dir():
                raise BridgeError("cwd_missing", str(self.cwd))
            os.chdir(self.cwd)
            ws = Path(workspace) if workspace else None
            config = load_config(agent_workspace(self.cwd, ws), self.cwd)
            options = AppOptions(
                cwd=self.cwd,
                workspace=ws,
                profile=Path(profile) if profile else None,
                model=model or None,
                permission=cast(Permission | None,
                                permission if permission in PERMISSIONS else None),
                probe=probe,
                config=config,
            )
            self.app = await boot(options, self.frontend, adapter=self._adapter)
            self.app.conductor.extras["providers_host"] = self.host
            self.repl = Repl(self.app, cast(Any, self.frontend))
            self._observe(True)
            resumed = True
            if resume or continue_latest:
                resumed = bool(self.repl.resume(resume))
        finally:
            self._booting = False
        # Kept from here on: what boot said ("Resumed ...") is not part of the conversation.
        self.view = ViewLog(self.app.store, self.repl.conductor)
        self._worker = asyncio.create_task(self._work())
        self.push_state()
        # A conversation that could not be found starts fresh; the app says so.
        return {**self.snapshot(), **({} if resumed else {"resume_failed": resume or "latest"})}

    async def m_send(self, text: str, quiet: bool = False, steer: bool = False) -> dict[str, Any]:
        """Queue input (a message, ``/command`` or ``!shell``); it runs after the current one.

        ``quiet`` (e.g. a slash command run from a control) is carried in ``turn_started``
        and ``turn_finished`` so the app shows no user bubble for it. ``steer`` adds a plain
        message to the running turn instead: the model reads it after its current step
        (``{"type": "steered", "text", "at"}`` says when) and the turn goes on.
        """
        repl = self._need()
        if not text.strip():
            raise BridgeError("empty", "Nothing to send.")
        quiet = quiet is True
        if steer is True and self.busy and not quiet and not text.lstrip().startswith(("/", "!")):
            repl.conductor.on_steered = self._steered
            repl.conductor.steer(text)
            return {"steered": True, "busy": True}
        if self.busy:
            repl.queue.append(text)
            if quiet:
                self._quiet_queued[text] += 1
        else:
            self.inbox.put_nowait((text, quiet))
        self.push_state()
        return {"queued": len(repl.queue), "busy": self.busy}

    async def m_side(self, text: str) -> dict[str, Any]:
        """A side question (Claude Code's /btw): answered from a copy of this conversation
        with read-only tools, alongside any running turn; nothing is added back or saved.

        Streams ``{"type": "side", "id", "event"}`` and ends with
        ``{"type": "side_done", "id", "text", "cost", "error"}``.
        """
        repl = self._need()
        if not text.strip():
            raise BridgeError("empty", "Nothing to ask.")
        base = repl.conductor
        self._side_next += 1
        side_id = self._side_next
        transcript = Transcript(messages=list(base.transcript.messages))
        transcript.note(SIDE_NOTE)
        side = dataclasses.replace(
            base,
            transcript=transcript,
            tools=[tool for tool in base.tools if tool.name in SIDE_TOOLS],
            ui=SideUI(self.send, side_id),
            steering=[],
            on_steered=None,
        )

        async def run() -> None:
            spent = side.spent_usd
            try:
                result = await side.turn(text)
                self.send({"type": "side_done", "id": side_id, "text": result.text,
                           "cost": round(side.spent_usd - spent, 6)})
            except asyncio.CancelledError:
                self.send({"type": "side_done", "id": side_id, "error": "stopped"})
            except Exception as exc:
                from architect.session.codes import code_for

                self.send({"type": "side_done", "id": side_id, "error": code_for(exc)})
            finally:
                self._sides.pop(side_id, None)

        self._sides[side_id] = asyncio.create_task(run())
        return {"id": side_id}

    async def m_side_cancel(self, id: int) -> dict[str, Any]:
        task = self._sides.get(id)
        if task is not None:
            task.cancel()
        return {"stopped": task is not None}

    def _steered(self, text: str) -> None:
        self.send({"type": "steered", "text": text, "at": round(time.time(), 3)})

    async def m_cancel(self) -> dict[str, Any]:
        """Stop the running turn or command (Esc); queued messages are kept."""
        stopped = self.frontend.cancel()
        return {"stopped": stopped}

    async def m_unqueue(self, index: int | None = None) -> dict[str, Any]:
        """Take queued messages back (one, or all), e.g. to edit them."""
        repl = self._need()
        if index is None:
            taken, repl.queue[:] = list(repl.queue), []
        elif 0 <= index < len(repl.queue):
            taken = [repl.queue.pop(index)]
        else:
            taken = []
        for text in taken:
            self._take_quiet(text)
        self.push_state()
        return {"taken": taken}

    async def m_answer(self, id: int, value: str | None) -> dict[str, Any]:
        return {"accepted": self.frontend.answer(int(id), value)}

    async def m_state(self) -> dict[str, Any]:
        return self.snapshot()

    async def m_set_state(self, state: str) -> dict[str, Any]:
        """normal, forge (build mode, also "build") or plan: what Shift+Tab cycles through
        in the terminal."""
        repl = self._need()
        label = STATE_LABELS.get(state) if isinstance(state, str) else None
        if label is None:
            raise BridgeError("invalid_state", state)
        for _ in range(len(STATES)):
            if cycle_state(repl.conductor) == label:
                break
            cycle_mode(repl.conductor)
        self.push_state()
        return self.snapshot()

    async def m_set_permission(self, permission: str) -> dict[str, Any]:
        repl = self._need()
        if permission not in PERMISSIONS:
            raise BridgeError("invalid_permission", permission)
        set_permission(repl.conductor, cast(Permission, permission))
        self.push_state()
        return self.snapshot()

    async def m_history(self) -> dict[str, Any]:
        """The saved conversation: its transcript (``messages``) and, in ``view``, what it
        showed, in order (``architect.desktop.views``; None before anything was shown)."""
        repl = self._need()
        session = repl.conductor.session_id
        return {"session_id": session,
                "messages": [m.to_json() for m in repl.conductor.transcript.messages],
                "view": self.view.load(session) if self.view is not None else None}

    async def m_commands(self) -> dict[str, Any]:
        from ..agent.commands import COMMANDS, custom_commands

        seen: set[str] = set()
        rows: list[dict[str, Any]] = []
        for name in sorted(COMMANDS):
            entry = COMMANDS[name]
            if entry.name in seen:
                continue
            seen.add(entry.name)
            rows.append({"name": entry.name, "usage": entry.usage, "summary": entry.summary,
                         "aliases": list(entry.aliases), "detail": entry.detail,
                         "custom": False})
        if self.repl is not None:
            for custom in custom_commands(self.repl).values():
                rows.append({"name": custom.name, "usage": f"{custom.name} [args]",
                             "summary": custom.description, "aliases": [], "detail": "",
                             "custom": True})
        return {"commands": rows}

    async def m_sessions(self) -> dict[str, Any]:
        repl = self._need()
        return {"sessions": [dataclasses.asdict(info) for info in repl.app.store.list()]}

    async def m_resume(self, key: str | None = None) -> dict[str, Any]:
        repl = self._need()
        if self.busy:
            raise BridgeError("busy", "Wait for the current turn to finish.")
        if not repl.resume(key):
            raise BridgeError("not_found", key or "")
        self.push_state()
        return await self.m_history()

    async def m_mentions(self, query: str = "") -> dict[str, Any]:
        """Files under the working directory for ``@`` completion, best matches first."""
        from ..agent.repl import fuzzy_rank

        names: list[str] = []
        for root, dirs, files in os.walk(self.cwd):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
            rel = Path(root).relative_to(self.cwd)
            for name in files:
                names.append((rel / name).as_posix() if str(rel) != "." else name)
            if len(names) > 20_000:
                break
        ranked = fuzzy_rank(query, names) if query else sorted(names)
        return {"paths": ranked[:MENTION_LIMIT]}

    async def m_capture(self, text: str) -> dict[str, Any]:
        """Run a slash command and return what it shows instead of adding it to the
        conversation (the agents and settings screens read ``/library``, ``/telemetry``,
        ``/serve``, ``/doctor`` this way). Questions it asks still reach the app."""
        repl = self._need()
        if not text.startswith("/"):
            raise BridgeError("not_a_command", text[:40])
        if self.busy:
            raise BridgeError("busy", "Wait for the current turn to finish.")
        events: list[dict[str, Any]] = []
        renderer = self.frontend.renderer
        original = renderer._send

        def collect(message: dict[str, Any]) -> None:
            if message.get("type") == "event":
                events.append(message["event"])
            else:
                original(message)

        renderer._send = collect
        self.busy = True
        try:
            await repl.handle(text)
        finally:
            renderer._send = original
            self.busy = False
            self.push_state()
        return {"events": events}

    async def m_shutdown(self) -> dict[str, Any]:
        self.stopping.set()
        return {"ok": True}

    # -- the worker -----------------------------------------------------------------------

    def _take_quiet(self, text: str) -> bool:
        if self._quiet_queued[text] > 0:
            self._quiet_queued[text] -= 1
            if not self._quiet_queued[text]:
                del self._quiet_queued[text]
            return True
        return False

    async def _work(self) -> None:
        repl = self._need()
        while True:
            text, quiet = await self.inbox.get()
            self.busy = True
            self.push_state()
            try:
                await self._run(repl, text, quiet)
                while repl.queue and repl.running:
                    queued = repl.queue.pop(0)
                    await self._run(repl, queued, self._take_quiet(queued))
            finally:
                self.busy = False
                self.push_state()
            if not repl.running:  # /exit
                self.stopping.set()

    async def _run(self, repl: Repl, text: str, quiet: bool = False) -> None:
        started = time.monotonic()
        before = (repl.conductor.session_id, len(repl.conductor.transcript.messages))
        self.send(
            {"type": "turn_started", "text": text, "quiet": quiet, "at": round(time.time(), 3)}
        )
        try:
            await repl.handle(text)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            repl.say("Interrupted.", "warn")
        except Exception as exc:
            from architect.session.codes import code_for

            repl.say(f"That failed ({code_for(exc)}). /doctor checks the setup.", "error")
        # Steering the turn ended (or was stopped) before reading runs next, first in line.
        if repl.conductor.steering:
            repl.queue[0:0] = repl.conductor.steering
            repl.conductor.steering.clear()
        command = text.strip()
        if command.startswith("/") and not command.startswith("//"):
            self._save_command(repl, before, None if quiet else command)
        self.send({"type": "turn_finished", "text": text, "quiet": quiet,
                   "seconds": round(time.monotonic() - started, 2), "at": round(time.time(), 3)})
        if self.view is not None:
            self.view.flush()

    def _save_command(self, repl: Repl, before: tuple[str, int], typed: str | None) -> None:
        """Save the conversation after a command that added to it.

        A conversational turn saves itself. A command that runs a tool directly
        (``/make @spec.json``, ``/export <agent>``) only adds a note, which would otherwise
        wait for the app to close; saved now, the conversation has an id the app keeps, so
        a restart reopens it. A conversation the command began has no message of the
        user's to take its title from, so it is named after the command as typed, as
        ``/title`` would name it, rather than "(empty)"."""
        conductor = repl.conductor
        session, count = before
        if conductor.session_id != session or len(conductor.transcript.messages) == count:
            return
        saved = repl.app.store.find(session)
        if (typed and not conductor.extras.get("session_title")
                and not any(m.role == "user" for m in conductor.transcript.messages)
                and (saved is None or not saved.named)):
            conductor.extras["session_title"] = clean_title(typed)
        repl.app.save_session()

    # -- state ----------------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        if self.repl is None or self.app is None:
            return {"started": False, "version": __version__}
        repl, app = self.repl, self.app
        conductor = repl.conductor
        values = repl._toolbar_values(0)
        try:
            models = sorted(app.services.profile.models)
        except Exception:
            models = []
        try:
            tokens_in = app.meter.spend.tokens_in
            tokens_out = app.meter.spend.tokens_out
        except Exception:
            tokens_in = tokens_out = 0
        title = conductor.extras.get("session_title") or app.store.title(conductor.session_id)
        return {
            "started": True,
            "version": __version__,
            "session_id": conductor.session_id,
            "title": title if isinstance(title, str) else None,
            "state": _state_key(cycle_state(conductor)),
            "mode": values["mode"],
            "permission": values["permission"],
            "model": values["model"],
            "models": models,
            "spent_usd": values["spent_usd"],
            "budget_usd": values["budget_usd"],
            "elapsed_s": values["elapsed_s"],
            "budget_s": values["budget_s"],
            "context_percent": values["context_percent"],
            "context_tokens": _safe(app.context_tokens),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "unpriced": values["unpriced"],
            "in_flight": app.meter.in_flight,
            "busy": self.busy,
            "queue": list(repl.queue),
            "asks": self.frontend.pending(),
            "cwd": str(conductor.cwd),
            "workspace": str(app.workspace),
            "profile_path": str(app.profile_path),
            "unavailable": list(app.unavailable),
            "has_instructions": bool(conductor.instructions),
            "mcp_tools": sum(1 for t in conductor.tools if t.name.startswith("mcp__")),
            "supports_images": _safe(conductor.supports_images),
            "pending_images": [rel for rel, _ in repl.pending_images],
        }

    # -- lifetime -------------------------------------------------------------------------

    async def close(self) -> None:
        self._observe(False)
        self.frontend.cancel()
        for run in list(self._retakes.values()):
            if run.task is not None:
                run.task.cancel()
                with contextlib.suppress(BaseException):
                    await run.task
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(BaseException):
                await self._worker
        if self.app is not None:
            self.app.save_session()
            if self.view is not None:
                self.view.flush()
            await close_mcp(self.app, self.cwd)
            with contextlib.suppress(Exception):
                self.app.close()


def _resolve(path: str) -> Path:
    return Path(path).resolve()


def _safe(call: Callable[[], Any]) -> Any:
    try:
        return call()
    except Exception:
        return None


_jsonable = jsonable  # the name earlier code used


class EventStdout(io.TextIOBase):
    """``sys.stdout`` while serving: each printed line becomes an ``output`` event.

    Line-buffered and thread-safe (``/bench`` prints from a worker thread). The protocol
    writer never goes through this object; fd 1 still points at stderr for children.
    """

    def __init__(self, send: Send) -> None:
        super().__init__()
        self._send = send
        self._buffer = ""
        self._lock = threading.Lock()

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return "utf-8"

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            raise TypeError("write() argument must be str")
        with self._lock:
            self._buffer += text
            *lines, self._buffer = self._buffer.split("\n")
        for line in lines:
            self._emit(line)
        return len(text)

    def flush(self) -> None:
        with self._lock:
            rest, self._buffer = self._buffer, ""
        if rest:
            self._emit(rest)

    def _emit(self, line: str) -> None:
        self._send({"type": "event", "event": {"kind": "output", "text": line.rstrip("\r")}})


# -- process plumbing -------------------------------------------------------------------


def _take_stdio() -> tuple[TextIO, TextIO]:
    """Private copies of stdin/stdout for the protocol; fd 0 and 1 go elsewhere."""
    proto_in = os.fdopen(os.dup(0), "r", encoding="utf-8", newline="\n")
    proto_out = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n")
    sys.stdout.flush()
    os.dup2(2, 1)  # anything printed, by us or a child process, lands on stderr
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)
    sys.stdout = sys.stderr
    sys.stdin = io.StringIO("")
    return proto_in, proto_out


async def serve(proto_in: TextIO, proto_out: TextIO) -> int:
    def write(text: str) -> None:
        proto_out.write(text)
        proto_out.flush()

    bridge = Bridge(write)
    previous_stdout = sys.stdout
    sys.stdout = EventStdout(bridge.send)
    loop = asyncio.get_running_loop()
    lines: asyncio.Queue[str | None] = asyncio.Queue()

    def reader() -> None:
        for line in proto_in:
            loop.call_soon_threadsafe(lines.put_nowait, line)
        loop.call_soon_threadsafe(lines.put_nowait, None)

    threading.Thread(target=reader, name="bridge-stdin", daemon=True).start()
    bridge.send({"type": "ready", "version": __version__, "pid": os.getpid()})
    tasks: set[asyncio.Task[None]] = set()
    stop = asyncio.create_task(bridge.stopping.wait())
    try:
        while True:
            get = asyncio.create_task(lines.get())
            done, _ = await asyncio.wait({get, stop}, return_when=asyncio.FIRST_COMPLETED)
            if get not in done:
                get.cancel()
                break
            line = get.result()
            if line is None:
                break
            if not line.strip():
                continue
            task = asyncio.create_task(bridge.handle(line))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    finally:
        stop.cancel()
        await bridge.close()
        for task in list(tasks):
            task.cancel()
        with contextlib.suppress(Exception):
            sys.stdout.flush()
        sys.stdout = previous_stdout
    return 0


def main() -> int:
    proto_in, proto_out = _take_stdio()
    try:
        return asyncio.run(serve(proto_in, proto_out))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
