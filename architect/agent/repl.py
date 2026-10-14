"""The interactive Architect CLI (Phase 12 Section 8, Phase 13 Section 5) and ``-p`` mode.

Two front ends implement the conductor's ``UI`` protocol:

- ``RichFrontend``: prompt_toolkit for input (history with Ctrl+R search, a fuzzy slash
  menu with descriptions, a coloured bottom toolbar, Shift+Tab mode cycle, Ctrl+J
  newlines, Ctrl+G or Ctrl+X Ctrl+E for an external editor, paste collapsing) and rich for
  output. The prompt is not active during a turn; a raw key listener cancels the turn on
  Esc or Ctrl+C and collects anything else typed into a message that is queued and sent
  when the turn ends (shown under the spinner). Output is printed inline into the normal
  scrollback. ``patch_stdout`` is never used, so nothing here writes the erase-line
  sequence while prompt_toolkit owns the screen.
- ``LineFrontend``: plain text over the session's ``Console`` (``StdConsole`` or a
  ``ScriptedConsole``) for pipes, CI, terminals prompt_toolkit cannot drive (Git Bash or
  mintty without winpty) and tests. On an interactive console it reads lines in the
  background, so lines typed while a turn runs are queued (``queue_input``).

``Repl`` is the loop shared by both: slash commands (built in and custom), ``!shell``,
``@path`` attachments (text files and images), queued messages, auto-compaction near the
context limit, per-turn checkpoints for ``/undo``, and the conversation saved after every
turn.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Sequence,
)
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, TextIO, TypeVar, cast

from pydantic import JsonValue

from architect import __version__
from architect.models import ModelAdapter
from architect.models.relay import RelayRefusal, Unreachable
from architect.runtime import BudgetExceeded
from architect.session.io import Console, StdConsole

from .app import (
    SIGN_IN_FIRST,
    SIGN_IN_FIRST_TEXT,
    App,
    AppOptions,
    BootError,
    agent_workspace,
    boot,
    close_mcp,
)
from .conductor import Mode, TurnResult
from .config import CHARS_PER_TOKEN, Config, editor_command, load_config
from .events import UI, Approval, Event, Notice, PlanCard, RecordingUI
from .render import (
    Glyphs,
    PlainRenderer,
    RichRenderer,
    glyphs_for,
    mode_label,
    tokens,
    toolbar_fragments,
    toolbar_text,
    unicode_ok,
)
from .tools import SPAWNABLE, Permission, ToolError

if TYPE_CHECKING:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import CompleteEvent, Completion
    from prompt_toolkit.document import Document
    from prompt_toolkit.input import Input
    from prompt_toolkit.output import Output

    from architect.models.images import ImageInput

__all__ = [
    "APPROVAL_TIMEOUT_S",
    "ArchitectCompleter",
    "Interrupted",
    "LineFrontend",
    "PrintUI",
    "Repl",
    "RichFrontend",
    "approval_answer",
    "attach_images",
    "attach_mentions",
    "cycle_mode",
    "edit_text",
    "fuzzy_rank",
    "main",
    "next_mode",
    "set_permission",
]

_T = TypeVar("_T")
APPROVAL_TIMEOUT_S = 300.0
ATTACH_BYTES = 64 * 1024
ATTACH_FILES = 8
ATTACH_IMAGES = 8
SHELL_TIMEOUT_S = 600.0
SHELL_OUTPUT_CHARS = 20_000
PASTE_LINES = 12
PERMISSIONS: tuple[Permission, ...] = ("ask", "auto-edit", "full-auto", "plan")
MODES: tuple[Mode, ...] = ("normal", "build")
# The Shift+Tab cycle as the user sees it: build is the conductor's build mode, plan is the
# read-only permission mode.
CYCLE = ("normal", "build", "plan")
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
_MENTION = re.compile(r"(?<![\w@])@([^\s@]+)")
PLAN_NOTE = (
    "The user switched to plan mode (read-only): do not call `code`; investigate with the "
    "read-only tools and answer with a concrete plan of the changes instead."
)
PLAN_OFF_NOTE = "The user left plan mode; changes through `code` are allowed again."
TOOL_HINTS = {
    "unknown_agent": "/agents lists the installed agents.",
    "unlicensed_agent": "/recompile <agent> builds a licensed version.",
    "invalid_input": "/report <agent> shows the input it expects.",
    "unknown_preset": "Presets: " + ", ".join(SPAWNABLE) + ".",
    "session_budget_exhausted": "Raise it with /budget session <usd>.",
    "coder_unavailable": "/doctor shows what the coder needs.",
    "build_mode_unavailable": "/doctor shows what is missing.",
    "forge_unavailable": "/doctor shows what is missing.",
    "plan_mode_read_only": "/permissions ask allows changes again.",
}
VISIBLE_SECRET = (
    "This line UI shows what you type; the key still stays out of history and logs "
    "(setting it in the environment avoids typing it)."
)


class Interrupted(Exception):
    """Ctrl+C at an empty prompt."""


# -- small pure helpers ------------------------------------------------------------------


def next_mode(mode: str) -> Mode:
    """normal -> build -> normal; the full Shift+Tab cycle is ``cycle_mode``."""
    index = MODES.index(mode) if mode in MODES else -1
    return MODES[(index + 1) % len(MODES)]


def _note(conductor: Any, text: str) -> None:
    transcript = getattr(conductor, "transcript", None)
    if transcript is not None:
        transcript.note(text)


def set_permission(conductor: Any, value: Permission) -> None:
    """Change the coder's permission mode; session allowances do not survive a change."""
    context = getattr(conductor, "context", None)
    if context is None:
        return
    previous = context.permission
    if previous == value:
        return
    if value == "plan":
        conductor.extras["plan_previous"] = previous
        _note(conductor, PLAN_NOTE)
    elif previous == "plan":
        _note(conductor, PLAN_OFF_NOTE)
    context.permission = value
    conductor.settings.permission = value
    context.allowed.clear()


def cycle_state(conductor: Any) -> str:
    context = getattr(conductor, "context", None)
    permission = getattr(context, "permission", "")
    return mode_label(str(conductor.mode), str(permission))


def cycle_mode(conductor: Any) -> str:
    """Shift+Tab: normal -> build -> plan -> normal. Returns the new label."""
    current = cycle_state(conductor)
    target = CYCLE[(CYCLE.index(current) + 1) % len(CYCLE)] if current in CYCLE else "normal"
    context = getattr(conductor, "context", None)
    if target == "build":
        conductor.mode = "build"
    elif target == "plan":
        conductor.mode = "normal"
        if context is None:
            return "normal"
        set_permission(conductor, "plan")
    else:
        conductor.mode = "normal"
        if context is not None and context.permission == "plan":
            previous = conductor.extras.pop("plan_previous", "ask")
            set_permission(conductor, previous if previous in PERMISSIONS[:3] else "ask")
    return cycle_state(conductor)


def approval_answer(text: str | None) -> Approval:
    """``y``/``yes`` allow once, ``s``/``a``/``session`` allow this kind; anything else no."""
    if text is None:
        return "no"
    word = text.strip().lower()
    if word in ("y", "yes"):
        return "yes"
    if word in ("s", "session", "a", "always"):
        return "session"
    return "no"


def _mentions(text: str) -> list[str]:
    found: list[str] = []
    for match in _MENTION.finditer(text):
        raw = match.group(1).rstrip(".,;:!?)")
        if raw and raw not in found:
            found.append(raw)
    return found


def _inside(root: Path, raw: str) -> Path | None:
    path = (root / raw).resolve()
    return path if path == root or root in path.parents else None


def attach_mentions(text: str, cwd: Path) -> tuple[str, list[str], list[str]]:
    """Append the text of every ``@path`` inside ``cwd``; return (message, attached, skipped).

    Image paths are left to ``attach_images``.
    """
    root = cwd.resolve()
    attached: list[str] = []
    skipped: list[str] = []
    blocks: list[str] = []
    for raw in _mentions(text):
        if Path(raw).suffix.lower() in IMAGE_SUFFIXES:
            continue
        path = _inside(root, raw)
        if path is None or not path.is_file() or len(attached) >= ATTACH_FILES:
            skipped.append(raw)
            continue
        try:
            data = path.read_bytes()
        except OSError:
            skipped.append(raw)
            continue
        if b"\0" in data[:4096]:
            skipped.append(raw)
            continue
        body = data[:ATTACH_BYTES].decode("utf-8", errors="replace")
        more = len(data) - ATTACH_BYTES
        if more > 0:
            body += f"\n... [{more} more bytes not attached]"
        rel = path.relative_to(root).as_posix()
        blocks.append(f'<attached file="{rel}">\n{body}\n</attached>')
        attached.append(raw)
    if not blocks:
        return text, attached, skipped
    return text + "\n\n" + "\n\n".join(blocks), attached, skipped


def load_image(path: Path) -> ImageInput | None:
    """A validated image (header sniffed, size bounded), or None."""
    from architect.models.images import MAX_IMAGE_BYTES, ImageInput

    try:
        if path.stat().st_size > MAX_IMAGE_BYTES:
            return None
        return ImageInput.from_bytes(path.read_bytes())
    except (OSError, ValueError):
        return None


def attach_images(text: str, cwd: Path) -> tuple[list[tuple[str, ImageInput]], list[str]]:
    """Every ``@image.png|jpg|webp|gif`` inside ``cwd``: (attached, skipped)."""
    root = cwd.resolve()
    attached: list[tuple[str, ImageInput]] = []
    skipped: list[str] = []
    for raw in _mentions(text):
        if Path(raw).suffix.lower() not in IMAGE_SUFFIXES:
            continue
        path = _inside(root, raw)
        image = load_image(path) if path is not None and path.is_file() else None
        if image is None or len(attached) >= ATTACH_IMAGES:
            skipped.append(raw)
            continue
        attached.append((path.relative_to(root).as_posix() if path else raw, image))
    return attached, skipped


def image_marker(rel: str, image: ImageInput) -> str:
    return (f'<attached image="{rel}" media_type="{image.media_type}" '
            f'size="{image.width}x{image.height}" sha256="{image.sha256[:16]}" />')


def fuzzy_rank(query: str, names: Iterable[str]) -> list[str]:
    """Prefix matches, then substring matches, then in-order letter matches."""
    query = query.lower()
    prefix, inner, loose = [], [], []
    for name in names:
        lowered = name.lower()
        if lowered.startswith(query):
            prefix.append(name)
        elif query.lstrip("/") and query.lstrip("/") in lowered:
            inner.append(name)
        else:
            letters = iter(lowered)
            if all(char in letters for char in query):
                loose.append(name)
    return sorted(prefix) + sorted(inner) + sorted(loose)


def edit_text(text: str, command: Sequence[str], *, suffix: str = ".md") -> str | None:
    """Open ``text`` in an external editor and return the result (None when it failed)."""
    handle, name = tempfile.mkstemp(prefix="architect-", suffix=suffix)
    path = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        try:
            code = subprocess.call([*command, str(path)])
        except OSError:
            return None
        if code != 0:
            return None
        return path.read_text(encoding="utf-8", errors="replace").rstrip("\n")
    finally:
        with contextlib.suppress(OSError):
            path.unlink()


@contextlib.contextmanager
def _echo_off(stream: TextIO | None) -> Iterator[bool]:
    """Hide typing on a POSIX terminal (works with a read already pending); False elsewhere.

    A Windows console read that is already waiting keeps the echo mode it started with, so
    the line UI cannot hide typing there; the default UI (prompt_toolkit) can.
    """
    if stream is None or sys.platform == "win32":
        yield False
        return
    import termios

    try:
        fd = stream.fileno()
        old = termios.tcgetattr(fd)
        new = list(old)
        new[3] &= ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSADRAIN, new)
    except (OSError, ValueError, termios.error, AttributeError):
        yield False
        return
    try:
        yield True
    finally:
        with contextlib.suppress(OSError, ValueError, termios.error):
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


# -- front ends --------------------------------------------------------------------------


class Frontend(UI, Protocol):
    renderer: PlainRenderer | RichRenderer

    def attach(self, repl: Repl) -> None: ...

    async def read_line(self) -> str | None:
        """One submitted input; None at end of input. Raises ``Interrupted`` on Ctrl+C."""
        ...

    async def watch(self, task: asyncio.Task[_T]) -> None:
        """Wait for ``task``; Esc or Ctrl+C cancels it."""
        ...


def _uncancel() -> None:
    task = asyncio.current_task()
    if task is not None:
        task.uncancel()


async def _await_cancellable[T](task: asyncio.Task[T]) -> None:
    """Wait for ``task``; a Ctrl+C that cancels the caller cancels ``task`` instead."""
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            _uncancel()  # asyncio.run turned SIGINT into this; the turn is what stops
            task.cancel()


class _Questions:
    """``ask`` and ``approve`` over one ``_answer(prompt, timeout)`` primitive."""

    approve_timeout_s: float = APPROVAL_TIMEOUT_S

    async def _answer(self, prompt: str, wait_s: float | None) -> str | None:
        raise NotImplementedError

    def emit(self, event: Event) -> None:
        raise NotImplementedError

    async def ask(
        self, question: str, *, default: str | None = None, choices: Sequence[str] = ()
    ) -> str | None:
        options = f" ({'/'.join(choices)})" if choices and len(choices) <= 6 else ""
        shown = f" [{default}]" if default else ""
        answer = await self._answer(f"{question}{options}{shown} ", None)
        if answer is None:
            return None
        return answer.strip() or (default if default is not None else "")

    async def approve(self, action: str, detail: str) -> Approval:
        prompt = f"Allow {action} {detail}? [y]es / [s]ession / [n]o "
        try:
            answer = await self._answer(prompt, self.approve_timeout_s)
        except TimeoutError:
            self.emit(Notice("No answer in 5 minutes: not allowed.", "warn"))
            return "no"
        return approval_answer(answer)


def _prompt_for(repl: Repl | None) -> str:
    if repl is None:
        return "> "
    label = cycle_state(repl.conductor)
    return "> " if label == "normal" else f"{label}> "


class LineFrontend(_Questions):
    """Plain line UI over a ``Console`` (``StdConsole``, ``ScriptedConsole``).

    With ``queue_input`` (an interactive console), one background reader owns the
    console: a line typed while a turn runs is queued as the next message, unless a
    question is waiting for it.
    """

    def __init__(
        self,
        console: Console,
        *,
        glyphs: Glyphs | None = None,
        queue_input: bool = False,
        tty: TextIO | None = None,
    ) -> None:
        self.console = console
        self.tty = tty  # the real terminal behind the console, for hidden input
        self.renderer: PlainRenderer | RichRenderer = PlainRenderer(
            console.write, glyphs or glyphs_for(False)
        )
        self.repl: Repl | None = None
        self.queue_input = queue_input
        self._lines: asyncio.Queue[str | None] | None = None
        self._pump: asyncio.Task[None] | None = None
        self._busy = False
        self._waiting = 0

    def attach(self, repl: Repl) -> None:
        self.repl = repl

    def emit(self, event: Event) -> None:
        self.renderer.show(event)

    # -- the background reader (queue_input) -------------------------------------------

    def _start_pump(self) -> asyncio.Queue[str | None]:
        if self._lines is None:
            self._lines = asyncio.Queue()
            self._pump = asyncio.ensure_future(self._run_pump(self._lines))
        return self._lines

    async def _run_pump(self, lines: asyncio.Queue[str | None]) -> None:
        while True:
            try:
                line = await self.console.read("")
            except asyncio.CancelledError:
                return
            except Exception:
                line = None
            if line is not None and self._busy and not self._waiting and self.repl is not None:
                self.repl.queue.append(line)
                count = len(self.repl.queue)
                self.renderer.text(f"(queued: sent after this turn; {count} waiting)")
                continue
            lines.put_nowait(line)
            if line is None:
                return

    async def _pumped(self, prompt: str) -> str | None:
        lines = self._start_pump()
        self.console.write(prompt)
        self._waiting += 1
        try:
            return await lines.get()
        finally:
            self._waiting -= 1

    # -- reading ------------------------------------------------------------------------

    async def _read(self, prompt: str) -> str | None:
        try:
            if self.queue_input:
                return await self._pumped(prompt)
            return await self.console.read(prompt)
        except asyncio.CancelledError:
            _uncancel()
            raise Interrupted from None

    async def _answer(self, prompt: str, wait_s: float | None) -> str | None:
        # A separate task, so a timeout never looks like the user's Ctrl+C; a cancelled
        # turn (Ctrl+C while a question is open) still cancels through here.
        source = self._pumped(prompt) if self.queue_input else self.console.read(prompt)
        reader = asyncio.ensure_future(source)
        try:
            done, _ = await asyncio.wait({reader}, timeout=wait_s)
        except asyncio.CancelledError:
            reader.cancel()
            raise
        if not done:
            reader.cancel()
            raise TimeoutError
        return reader.result()

    async def ask_secret(self, question: str) -> str | None:
        """A secret (an API key): never echoed where the terminal allows, never kept."""
        with _echo_off(self.tty) as hidden:
            if not hidden:
                self.emit(Notice(VISIBLE_SECRET, "warn"))
            answer = await self._answer(question, None)
        if hidden:
            self.console.write("\n")
        return answer

    async def read_line(self) -> str | None:
        line = await self._read(_prompt_for(self.repl))
        if line is None:
            return None
        while line.endswith("\\"):
            more = await self._read("... ")
            if more is None:
                return line[:-1]
            line = line[:-1] + "\n" + more
        return line

    async def watch(self, task: asyncio.Task[_T]) -> None:
        self._busy = True
        try:
            await _await_cancellable(task)
        finally:
            self._busy = False

    def close(self) -> None:
        if self._pump is not None:
            self._pump.cancel()


class ArchitectCompleter:
    """Tab completion: a fuzzy slash menu with descriptions, arguments, agents, ``@paths``."""

    def __init__(
        self,
        commands: Callable[[], Sequence[str]],
        agents: Callable[[], Sequence[str]],
        cwd: Path,
        describe: Callable[[str], str] | None = None,
    ) -> None:
        self.commands = commands
        self.agents = agents
        self.cwd = cwd
        self.describe = describe

    def _arguments(self, command: str) -> Sequence[str]:
        if command in ("/run", "/report", "/recompile"):
            try:
                return self.agents()
            except Exception:
                return ()
        if command == "/config":
            from .config import KEYS

            return ("set", *KEYS)
        return {
            "/mode": ("normal", "build"),
            "/permissions": PERMISSIONS,
            "/spawn": SPAWNABLE,
            "/help": tuple(name.lstrip("/") for name in self.commands()),
            "/budget": ("session", "run", "compile"),
            "/floor": ("add", "clear"),
            "/forge": ("arc",),
            "/memory": ("edit", "global"),
        }.get(command, ())

    def _paths(self, fragment: str) -> Iterable[str]:
        root = self.cwd.resolve()
        head, _, tail = fragment.rpartition("/")
        base = (root / head).resolve() if head else root
        if not (base == root or root in base.parents) or not base.is_dir():
            return
        try:
            children = sorted(base.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        for child in children[:500]:
            if child.name.startswith(".") and not tail.startswith("."):
                continue
            if child.name.lower().startswith(tail.lower()):
                suffix = "/" if child.is_dir() else ""
                yield (f"{head}/" if head else "") + child.name + suffix

    def _meta(self, name: str) -> str:
        if self.describe is None:
            return ""
        try:
            return self.describe(name)
        except Exception:
            return ""

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        from prompt_toolkit.completion import Completion

        text = document.text_before_cursor
        word = text.split(" ")[-1] if text else ""
        if word.startswith("@"):
            fragment = word[1:]
            for path in self._paths(fragment):
                yield Completion("@" + path, start_position=-len(word))
            return
        if not text.startswith("/"):
            return
        if " " not in text:
            for name in fuzzy_rank(text, self.commands()):
                yield Completion(name, start_position=-len(text), display_meta=self._meta(name))
            return
        command, _, rest = text.partition(" ")
        if command == "/config" and rest.startswith("set ") and rest.count(" ") == 1:
            from .config import KEYS

            key = rest[4:]
            for name in KEYS:
                if name.startswith(key):
                    yield Completion(name, start_position=-len(key),
                                     display_meta=KEYS[name].help)
            return
        if " " in rest:
            return
        for value in self._arguments(command):
            if value.startswith(rest):
                yield Completion(value, start_position=-len(rest))


class _Pastes:
    """Large pastes shown as ``[pasted N lines #k]`` and expanded on submit."""

    def __init__(self) -> None:
        self.items: dict[str, str] = {}

    def add(self, data: str) -> str:
        data = data.replace("\r\n", "\n").replace("\r", "\n")
        lines = data.count("\n") + 1
        if lines < PASTE_LINES:
            return data
        key = f"[pasted {lines} lines #{len(self.items) + 1}]"
        self.items[key] = data
        return key

    def expand(self, text: str) -> str:
        for key, value in self.items.items():
            text = text.replace(key, value)
        self.items.clear()
        return text


class _Typing:
    """What the user types while a turn runs (the prompt is not active then)."""

    def __init__(self) -> None:
        self.text = ""

    def feed(self, pressed: Sequence[Any]) -> list[str]:
        """Apply key presses; return the lines submitted with Enter."""
        from prompt_toolkit.keys import Keys

        submitted: list[str] = []
        for press in pressed:
            key = press.key
            if key in (Keys.ControlM, Keys.ControlJ, Keys.Enter):
                if self.text.strip():
                    submitted.append(self.text)
                self.text = ""
            elif key in (Keys.Backspace, Keys.ControlH):
                self.text = self.text[:-1]
            elif key == Keys.ControlU:
                self.text = ""
            elif key == Keys.BracketedPaste:
                self.text += str(press.data).replace("\r\n", "\n").replace("\r", "\n")
            elif isinstance(key, str) and len(key) == 1 and key.isprintable():
                self.text += key
        return submitted


class RichFrontend(_Questions):
    """prompt_toolkit input and rich output on a real terminal."""

    def __init__(
        self,
        stdout: TextIO | None = None,
        *,
        pt_input: Input | None = None,
        pt_output: Output | None = None,
    ) -> None:
        from prompt_toolkit import PromptSession
        from rich.console import Console as RichConsole

        stream = stdout if stdout is not None else sys.stdout
        self.glyphs = glyphs_for(unicode_ok(stream))
        self.console = RichConsole(file=stream, highlight=False, soft_wrap=False)
        self.renderer: PlainRenderer | RichRenderer = RichRenderer(self.console, self.glyphs)
        self.repl: Repl | None = None
        self.pastes = _Pastes()
        self.typing = _Typing()
        # Creating the session probes the console: NoConsoleScreenBufferError surfaces here.
        self.session: PromptSession[str] = PromptSession(input=pt_input, output=pt_output)
        self._questions: PromptSession[str] = PromptSession(
            input=self.session.input, output=self.session.output
        )
        from prompt_toolkit.history import DummyHistory

        self._secrets: PromptSession[str] = PromptSession(
            input=self.session.input, output=self.session.output, history=DummyHistory()
        )
        self._listening = False
        self._task: asyncio.Task[Any] | None = None
        self._stack: contextlib.ExitStack | None = None

    def attach(self, repl: Repl) -> None:
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.styles import Style

        self.repl = repl
        history_path = repl.app.workspace / "history"
        history = None
        with contextlib.suppress(OSError):
            history_path.parent.mkdir(parents=True, exist_ok=True)
            history = FileHistory(str(history_path))
        completer = ArchitectCompleter(repl.command_names, repl.agent_names,
                                       repl.conductor.cwd, repl.describe_command)
        session = self.session
        if history is not None:
            session.history = history
            session.default_buffer.history = history  # the buffer was built with the old one
        session.completer = cast(Any, _as_completer(completer))
        session.complete_while_typing = True
        session.multiline = True
        session.key_bindings = self._bindings()
        session.bottom_toolbar = self._toolbar
        session.prompt_continuation = lambda width, line, wrap: "." * max(0, width - 1) + " "
        session.style = Style.from_dict({
            "bottom-toolbar": "noreverse bg:#222222 #aaaaaa",
            "completion-menu.meta.completion": "bg:#333333 #aaaaaa",
            "completion-menu.meta.completion.current": "bg:#444444 #ffffff",
        })
        renderer = cast(RichRenderer, self.renderer)
        renderer.status_suffix = repl.spend_line
        renderer.queue_lines = self._queue_lines

    def _queue_lines(self) -> list[str]:
        if self.repl is None:
            return []
        lines = [f"queued> {line.splitlines()[0] if line else ''}" for line in self.repl.queue]
        if self.typing.text:
            lines.append(f"> {self.typing.text}_  (Enter queues it for after this turn)")
        return lines

    # -- input ------------------------------------------------------------------------

    def _bindings(self) -> Any:
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.keys import Keys

        bindings = KeyBindings()
        pastes = self.pastes

        @bindings.add("enter")
        def _(event: Any) -> None:
            buffer = event.current_buffer
            state = buffer.complete_state
            if state is not None and state.current_completion is not None:
                before = buffer.text
                buffer.apply_completion(state.current_completion)
                if buffer.text != before:
                    return  # Enter picked a completion; the next Enter sends
            buffer.complete_state = None
            if buffer.document.text_before_cursor.endswith("\\"):
                buffer.delete_before_cursor(1)
                buffer.insert_text("\n")
                return
            buffer.validate_and_handle()

        @bindings.add("c-j")
        def _(event: Any) -> None:
            event.current_buffer.insert_text("\n")

        @bindings.add("s-tab")
        @bindings.add("escape", "m")
        def _(event: Any) -> None:
            if self.repl is not None:
                cycle_mode(self.repl.conductor)
            event.app.invalidate()

        @bindings.add("c-c")
        def _(event: Any) -> None:
            buffer = event.current_buffer
            if buffer.text:
                buffer.reset()
            else:
                event.app.exit(exception=KeyboardInterrupt())

        @bindings.add("c-g")
        @bindings.add("c-x", "c-e")
        async def _(event: Any) -> None:
            from prompt_toolkit.application import run_in_terminal

            buffer = event.current_buffer
            command = self._editor()

            def run() -> str | None:
                return edit_text(buffer.text, command)

            edited = await run_in_terminal(run, in_executor=True)
            if edited is None:
                return
            buffer.text = edited
            buffer.cursor_position = len(edited)

        @bindings.add(Keys.BracketedPaste)
        def _(event: Any) -> None:
            event.current_buffer.insert_text(pastes.add(event.data))

        return bindings

    def _editor(self) -> list[str]:
        configured = self.repl.app.config.text("editor") if self.repl is not None else None
        return editor_command(configured)

    def _toolbar(self) -> Any:
        if self.repl is None:
            return ""
        from prompt_toolkit.application import get_app

        try:
            width = get_app().output.get_size().columns
        except Exception:
            width = 100
        return self.repl.toolbar_fragments(width, self.glyphs)

    def _message(self) -> str:
        return _prompt_for(self.repl)

    async def read_line(self) -> str | None:
        default, self.typing.text = self.typing.text, ""
        try:
            text = await self.session.prompt_async(self._message, default=default)
        except KeyboardInterrupt:
            raise Interrupted from None
        except asyncio.CancelledError:
            _uncancel()  # SIGINT outside raw mode: the same as Ctrl+C at the prompt
            raise Interrupted from None
        except EOFError:
            return None
        return self.pastes.expand(text)

    async def _answer(self, prompt: str, wait_s: float | None) -> str | None:
        async with self._suspended():
            try:
                coroutine = self._questions.prompt_async(prompt)
                if wait_s is None:
                    return await coroutine
                return await asyncio.wait_for(coroutine, wait_s)
            except (KeyboardInterrupt, EOFError):
                return None

    async def ask_secret(self, question: str) -> str | None:
        """A secret (an API key): typed hidden, kept in no history."""
        async with self._suspended():
            try:
                return await self._secrets.prompt_async(question, is_password=True)
            except (KeyboardInterrupt, EOFError):
                return None

    # -- output -----------------------------------------------------------------------

    def emit(self, event: Event) -> None:
        self.renderer.show(event)

    # -- turns ------------------------------------------------------------------------

    def _input(self) -> Input:
        return self.session.input

    def _listen(self, on_cancel: Callable[[], None]) -> contextlib.ExitStack:
        from prompt_toolkit.keys import Keys

        stack = contextlib.ExitStack()
        source = self._input()

        def keys_ready() -> None:
            pressed = source.read_keys() + source.flush_keys()
            if any(press.key in (Keys.Escape, Keys.ControlC) for press in pressed):
                on_cancel()
                return
            submitted = self.typing.feed(pressed)
            if self.repl is not None:
                self.repl.queue.extend(submitted)
            cast(RichRenderer, self.renderer).tick()

        try:
            stack.enter_context(source.raw_mode())
            stack.enter_context(source.attach(keys_ready))
        except Exception:
            stack.close()  # no listener: Ctrl+C still reaches the loop as SIGINT
        return stack

    @contextlib.asynccontextmanager
    async def _suspended(self) -> AsyncIterator[None]:
        """Stop the live region and the key listener while a question is on screen."""
        renderer = cast(RichRenderer, self.renderer)
        renderer.stop_status()
        stack = self._stack
        if stack is not None:
            stack.close()
        try:
            yield
        finally:
            if stack is not None and self._listening:
                self._stack = self._listen(self._cancel)

    def _cancel(self) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()

    async def watch(self, task: asyncio.Task[_T]) -> None:
        renderer = cast(RichRenderer, self.renderer)
        self._task = task
        self._listening = True
        self._stack = self._listen(self._cancel)

        async def tick() -> None:
            while True:
                await asyncio.sleep(0.5)
                renderer.tick()

        ticker = asyncio.create_task(tick())
        try:
            await _await_cancellable(task)
        finally:
            self._listening = False
            ticker.cancel()
            renderer.stop_status()
            if self._stack is not None:
                self._stack.close()
            self._stack = None
            self._task = None


def _as_completer(completer: ArchitectCompleter) -> Any:
    """Wrap ``ArchitectCompleter`` in prompt_toolkit's ``Completer`` base class."""
    from prompt_toolkit.completion import Completer

    class _Wrapped(Completer):
        def get_completions(
            self, document: Document, complete_event: CompleteEvent
        ) -> Iterable[Completion]:
            return completer.get_completions(document, complete_event)

    return _Wrapped()


# -- the loop ----------------------------------------------------------------------------


class Repl:
    def __init__(self, app: App, frontend: Frontend) -> None:
        self.app = app
        self.frontend = frontend
        self.conductor = app.conductor
        self.running = True
        self.last_turn: TurnResult | None = None
        self.queue: list[str] = []  # messages typed during a turn, sent in order after it
        self.pending_images: list[tuple[str, ImageInput]] = []  # from /image
        frontend.attach(self)

    # -- output helpers ---------------------------------------------------------------

    def say(self, text: str, level: Literal["info", "warn", "error"] = "info") -> None:
        self.frontend.emit(Notice(text, level))

    def text(self, text: str) -> None:
        self.frontend.renderer.text(text)

    def rows(self, title: str, rows: Sequence[tuple[str, str]]) -> None:
        self.frontend.renderer.rows(title, rows)

    def diff(self, text: str) -> None:
        self.frontend.renderer.diff(text)

    # -- state for the toolbar, completer and commands --------------------------------

    def command_names(self) -> list[str]:
        from .commands import all_command_names

        return all_command_names(self)

    def describe_command(self, name: str) -> str:
        from .commands import describe

        return describe(self, name)

    def agent_names(self) -> list[str]:
        try:
            return [entry.name for entry in self.app.services.registry.list()]
        except Exception:
            return []

    def spend_line(self) -> str:
        conductor = self.conductor
        spend = self.app.meter.spend
        glyphs = getattr(self.frontend.renderer, "glyphs", None) or glyphs_for(False)
        money = ("cost unknown" if self.app.unpriced() else
                 f"${conductor.spent_usd:.4f} of ${conductor.settings.session_usd:.2f}")
        return (f"{glyphs.up} {tokens(spend.tokens_in)} {glyphs.down} {tokens(spend.tokens_out)}"
                f"  {money}")

    def _toolbar_values(self, width: int) -> dict[str, Any]:
        conductor = self.conductor
        meter = self.app.meter
        try:
            percent: float | None = self.app.context_percent()
        except Exception:
            percent = None
        return {
            "mode": conductor.mode,
            "permission": conductor.context.permission,
            "model": conductor.adapter.model,
            "spent_usd": conductor.spent_usd,
            "budget_usd": conductor.settings.session_usd,
            "elapsed_s": time.monotonic() - conductor.started,
            "budget_s": conductor.settings.session_s,
            "running": f"{meter.in_flight} call(s) running" if meter.in_flight else "",
            "width": width,
            "context_percent": percent,
            "queued": len(self.queue),
            "unpriced": self.app.unpriced(),
        }

    def toolbar(self, width: int, glyphs: Glyphs) -> str:
        return toolbar_text(glyphs=glyphs, **self._toolbar_values(width))

    def toolbar_fragments(self, width: int, glyphs: Glyphs) -> list[tuple[str, str]]:
        return toolbar_fragments(glyphs, **self._toolbar_values(width))

    def banner(self) -> None:
        conductor = self.conductor
        app = self.app
        window = app.context_tokens()
        rows = [
            ("model", f"{conductor.adapter.model} ({window // 1000}k context)"),
            ("cwd", str(conductor.cwd)),
            ("workspace", str(app.workspace)),
            ("mode", f"{cycle_state(conductor)} (Shift+Tab: normal, build, plan)"),
            ("permissions", conductor.context.permission),
            ("session budget", f"${conductor.settings.session_usd:.2f} / "
             f"{int(conductor.settings.session_s // 60)} min"),
        ]
        if app.unpriced():
            rows.append(("cost", "unknown for this model: time is the budget (/model add "
                         "<id> --price in/out declares one)"))
        mcp = sum(1 for tool in conductor.tools if tool.name.startswith("mcp__"))
        if mcp:
            rows.append(("mcp", f"{mcp} tool(s) from MCP servers (/mcp)"))
        if app.unavailable:
            rows.append(("not installed", ", ".join(app.unavailable)))
        self.rows(f"Architect {__version__}: a terminal agent that builds agents", rows)
        self.text(
            "Floors and budgets are inferred with defaults and shown before any compile. "
            "/help for commands, @file to attach, !cmd for a shell command, Ctrl+D to exit."
        )
        if not conductor.instructions:
            self.text("Tip: no ARCHITECT.md here yet; /init drafts one for this project.")

    # -- the loop ---------------------------------------------------------------------

    async def run(self) -> int:
        self.banner()
        armed = False
        try:
            while self.running:
                if self.queue:
                    line = self.queue.pop(0)
                    self.text(f"{_prompt_for(self)}{line}")
                else:
                    try:
                        line_read = await self.frontend.read_line()
                    except Interrupted:
                        if armed:
                            break
                        armed = True
                        self.say("(Ctrl+C again to exit, or /exit)")
                        continue
                    armed = False
                    if line_read is None:
                        break
                    line = line_read
                try:
                    await self.handle(line)
                except asyncio.CancelledError:
                    _uncancel()  # a Ctrl+C that landed outside a cancellable task
                    self.say("Interrupted.", "warn")
        finally:
            close = getattr(self.frontend, "close", None)
            if callable(close):
                close()
        self.app.save_session()
        return 0

    async def handle(self, line: str) -> None:
        text = line.strip()
        if not text:
            return
        if text.startswith("!"):
            await self.shell(text[1:].strip())
            return
        if text.startswith("/") and not text.startswith("//"):
            from .commands import dispatch

            await dispatch(self, text)
            return
        await self.turn(line.strip())

    async def work(self, awaitable: Awaitable[_T]) -> tuple[bool, _T | None]:
        """Run ``awaitable`` as a cancellable task; (finished, result)."""

        async def run() -> _T:
            return await awaitable

        task = asyncio.create_task(run())
        await self.frontend.watch(task)
        if task.cancelled():
            self.say("Interrupted.", "warn")
            return False, None
        return True, task.result()

    def auto_compact(self) -> None:
        """Compact old tool results when the context is nearly full (config percent)."""
        threshold = self.app.config.number("auto_compact_percent")
        if threshold <= 0:
            return
        before = self.app.context_percent()
        if before < threshold:
            return
        conductor = self.conductor
        changed = conductor.transcript.compact()
        if self.app.context_percent() >= threshold * 0.7:
            total = self.app.context_tokens() * CHARS_PER_TOKEN
            target = int(total * threshold / 200) - len(conductor.system_prompt())
            changed += conductor.transcript.fit(max(10_000, target))
        after = self.app.context_percent()
        self.say(f"Context {before:.0f}% full: compacted older tool results ({changed} "
                 f"change(s)); now {after:.0f}%. /context shows the breakdown.")
        self.app.save_session()

    def _checkpoint_key(self) -> None:
        store = self.app.checkpoints
        session = self.conductor.session_id
        turn = store.next_turn(session) if store is not None else 1
        self.conductor.extras["checkpoint_key"] = (session, turn)

    def _attach(self, text: str) -> str:
        """``@path`` text files and images, plus images queued with /image."""
        cwd = self.conductor.cwd
        message, _, skipped = attach_mentions(text, cwd)
        images, bad_images = attach_images(text, cwd)
        skipped += bad_images
        if skipped:
            self.say("Not attached (missing, binary, too large or outside the working "
                     "directory): " + ", ".join(skipped), "warn")
        images = self.pending_images + images
        self.pending_images = []
        if images:
            markers = "\n".join(image_marker(rel, image) for rel, image in images)
            message += "\n\n" + markers
            self.conductor.extras["pending_images"] = [image for _, image in images]
            if not self.conductor.supports_images():
                self.say("Attached " + ", ".join(rel for rel, _ in images) + " as a reference "
                         "only: the current model cannot be shown images here (pick a vision "
                         "model through a CLI or a priced image entry). Describe what matters "
                         "in words.", "warn")
        return message

    async def turn(self, text: str, *, mode: Mode | None = None) -> TurnResult | None:
        """One conductor turn with ``@path`` attachments; the conversation is saved after."""
        message = self._attach(text)
        self.app.sync_context_limit()
        self.auto_compact()
        self._checkpoint_key()
        previous = self.conductor.mode
        if mode is not None:
            self.conductor.mode = mode
        task = asyncio.create_task(self.conductor.turn(message))
        try:
            await self.frontend.watch(task)
        finally:
            if mode is not None:
                self.conductor.mode = previous
            self.conductor.extras.pop("pending_images", None)
        result: TurnResult | None = None
        if task.cancelled():
            self.say("Interrupted. Finished work is kept in the conversation.", "warn")
        elif task.exception() is not None:
            from architect.session.codes import code_for

            exc = task.exception()
            assert exc is not None
            if isinstance(exc, RelayRefusal | Unreachable):
                # The judges' relay said why, or nothing answered: one sentence either way.
                self.say(str(exc), "error")
            else:
                code = exc.code if isinstance(exc, ToolError) else code_for(exc)
                self.say(f"The turn failed ({code}). Try again, or /doctor to check the "
                         "setup.", "error")
        else:
            result = task.result()
            self.last_turn = result
        self.app.save_session()
        return result

    async def call_tool(
        self, name: str, args: dict[str, JsonValue], *, note: bool = True
    ) -> JsonValue | None:
        """Run one conductor tool directly (a slash command); errors become notices."""
        tool = next((t for t in self.conductor.tools if t.name == name), None)
        if tool is None:
            self.say(f"The {name} tool is not available; /doctor shows what is missing.",
                     "error")
            return None
        try:
            finished, result = await self.work(tool.run(args, self.conductor.context))
        except ToolError as exc:
            detail = f": {exc.detail}" if exc.detail else ""
            hint = TOOL_HINTS.get(exc.code, "")
            self.say(f"{name} failed ({exc.code}{detail}).{' ' + hint if hint else ''}",
                     "error")
            return None
        except BudgetExceeded:
            self.say("The session budget is used up; raise it with /budget session <usd>.",
                     "error")
            return None
        except (RelayRefusal, Unreachable) as exc:
            self.say(str(exc), "error")
            return None
        except Exception as exc:
            self.say(f"{name} failed ({type(exc).__name__}); /doctor checks the setup.",
                     "error")
            return None
        if not finished:
            return None
        if note:
            # The model sees what the user did by hand, so a follow-up question works.
            summary = json.dumps(result, ensure_ascii=False)
            self.conductor.transcript.note(f"The user ran the {name} tool directly: "
                                           f"{summary[:2000]}")
        return result

    async def shell(self, command: str) -> None:
        """``!cmd``: run locally, show the output; nothing is sent to the model."""
        if not command:
            self.say("Usage: !<shell command>", "warn")
            return

        async def run() -> tuple[int, str]:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=str(self.conductor.cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
            )
            try:
                output, _ = await asyncio.wait_for(process.communicate(), SHELL_TIMEOUT_S)
            except (asyncio.CancelledError, TimeoutError):
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                raise
            return process.returncode or 0, output.decode("utf-8", errors="replace")

        try:
            finished, result = await self.work(run())
        except TimeoutError:
            self.say("The command timed out.", "error")
            return
        except OSError:
            self.say("The command could not start.", "error")
            return
        if not finished or result is None:
            return
        code, output = result
        if len(output) > SHELL_OUTPUT_CHARS:
            output = output[:SHELL_OUTPUT_CHARS] + "\n... [output clipped]"
        if output.strip():
            self.text(output.rstrip("\n"))
        if code:
            self.say(f"exit code {code}", "warn")

    def resume(self, key: str | None) -> bool:
        info = self.app.store.find(key)
        if info is None:
            self.say("No saved conversation matches; /sessions lists them." if key
                     else "No saved conversations yet.", "warn")
            return False
        try:
            transcript = self.app.store.load(info.id)
        except (OSError, ValueError):
            self.say("That conversation could not be read.", "error")
            return False
        self.app.save_session()
        self.conductor.transcript = transcript
        self.conductor.session_id = info.id
        self.conductor.extras.pop("session_title", None)
        self.say(f"Resumed {info.id}: {info.title} ({len(transcript.messages)} messages).")
        return True


# -- -p mode -----------------------------------------------------------------------------


class PrintUI(RecordingUI):
    """Non-interactive: questions take their defaults, approvals are fixed, notices go to
    stderr so stdout carries only the answer."""

    def __init__(self, *, approval: Approval, stderr: TextIO) -> None:
        super().__init__(default_approval=approval)
        self._stderr = stderr

    def emit(self, event: Event) -> None:
        super().emit(event)
        if isinstance(event, Notice) and event.level != "info":
            with contextlib.suppress(OSError, ValueError):
                self._stderr.write(f"{event.level}: {event.text}\n")


class FrozenPrintUI(PrintUI):
    """``-p "/make @<spec.json>"`` (Phase 14, Section 7.1): plan and result cards go to
    stderr, and every question is answered no unless ``--permission full-auto`` (then each
    takes its default), so without it the card shows the hash and nothing compiles."""

    def __init__(self, *, approval: Approval, stderr: TextIO) -> None:
        super().__init__(approval=approval, stderr=stderr)
        self._cards = PlainRenderer(self._card_line, glyphs_for(unicode_ok(stderr)))

    def _card_line(self, text: str) -> None:
        with contextlib.suppress(OSError, ValueError):
            self._stderr.write(text)

    def emit(self, event: Event) -> None:
        super().emit(event)
        if isinstance(event, PlanCard):
            self._cards.show(event)

    async def ask(
        self, question: str, *, default: str | None = None, choices: Sequence[str] = ()
    ) -> str | None:
        self.questions.append(question)
        return default if self.default_approval == "yes" else "no"


_FROZEN_TASK = re.compile(r"/(make|forge)\s+(@.+)", re.DOTALL)


def frozen_task(task: str) -> tuple[bool, str] | None:
    """``/make @<path>`` or ``/forge @<path>`` as ``(forge, path)``, else None."""
    from .commands import frozen_path

    match = _FROZEN_TASK.fullmatch(task.strip())
    path = frozen_path(match.group(2)) if match is not None else None
    if match is None or path is None:
        return None
    return match.group(1) == "forge", path


async def _print_frozen(app: App, forge: bool, path: str) -> JsonValue:
    """Run the ``make`` tool on a frozen spec directly, as the slash command does."""
    conductor = app.conductor
    tool = next((item for item in conductor.tools if item.name == "make"), None)
    if tool is None:
        raise BootError("build_mode_unavailable")
    extras = conductor.extras
    if forge:
        extras["force_forge"] = True
    try:
        result = await tool.run({"description": "", "spec_file": path}, conductor.context)
    except ToolError as exc:
        raise BootError(exc.code) from None
    finally:
        if forge:
            extras.pop("force_forge", None)
    summary = json.dumps(result, ensure_ascii=False)
    conductor.transcript.note(f"The user ran the make tool directly: {summary[:2000]}")
    return result


async def _print_mode(
    options: AppOptions,
    task: str,
    *,
    as_json: bool,
    resume: str | None,
    continue_latest: bool,
    adapter: ModelAdapter | None,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    approval: Approval = "yes" if options.permission == "full-auto" else "no"
    frozen = frozen_task(task)
    ui = (
        FrozenPrintUI(approval=approval, stderr=stderr)
        if frozen is not None
        else PrintUI(approval=approval, stderr=stderr)
    )
    app = await boot(options, ui, adapter=adapter)
    built: JsonValue = None
    try:
        conductor = app.conductor
        if continue_latest or resume:
            info = app.store.find(resume)
            if info is None:
                raise BootError("session_not_found")
            conductor.transcript = app.store.load(info.id)
            conductor.session_id = info.id
        if frozen is not None:
            built = await _print_frozen(app, *frozen)
        else:
            message, _, _ = attach_mentions(task, options.cwd)
            result = await conductor.turn(message)
        app.save_session()
    finally:
        await close_mcp(app, options.cwd)
        app.close()
    if frozen is not None:
        # The make tool's result as one JSON object (it holds the full spec hash).
        stdout.write(json.dumps(built, ensure_ascii=False) + "\n")
        stdout.flush()
        return 0
    if as_json:
        payload = {
            "answer": result.text,
            "cost_usd": round(result.cost_usd, 6),
            "tool_calls": result.tool_calls,
            "session": conductor.session_id,
        }
        stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    else:
        stdout.write(result.text.rstrip("\n") + "\n")
    stdout.flush()
    return 0


# -- entry -------------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:
        raise BootError("invalid_arguments")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="architect",
        description="Architect: a terminal agent that builds agents. Run with no arguments "
        "for the interactive CLI; 'architect session' is the step-by-step session.",
    )
    parser.add_argument("-p", "--print", dest="print_task", metavar="TASK",
                        help="run one turn non-interactively and print the answer ('-' reads "
                        "the task from stdin)")
    parser.add_argument("--json", action="store_true", help="with -p: print a JSON object")
    parser.add_argument("-c", "--continue", dest="continue_latest", action="store_true",
                        help="continue the most recent conversation")
    parser.add_argument("-r", "--resume", metavar="ID", help="resume a saved conversation")
    parser.add_argument("--model", help="the conductor's model (a profile model)")
    parser.add_argument("--workspace", type=Path, help="workspace (default ~/.architect)")
    parser.add_argument("--profile", type=Path, help="profile file (default: discovered)")
    parser.add_argument("--permission", choices=PERMISSIONS,
                        help="coder approval mode (plan is read-only)")
    parser.add_argument("--no-probe", action="store_true", help="skip the first-run login probe")
    parser.add_argument("--plain", action="store_true", help="plain line UI (no prompt_toolkit)")
    return parser


def _utf8(stream: TextIO) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        with contextlib.suppress(OSError, ValueError, TypeError, AttributeError):
            reconfigure(encoding="utf-8", errors="replace")


def _is_tty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def choose_frontend(*, plain: bool, stdin: TextIO, stdout: TextIO) -> Frontend:
    """The prompt_toolkit UI on a real console; the line UI everywhere else."""
    if not plain and _is_tty(stdin) and _is_tty(stdout):
        try:
            return RichFrontend(stdout)
        except Exception:  # NoConsoleScreenBufferError and friends: fall back, never show
            pass
    tty = _is_tty(stdin)
    return LineFrontend(StdConsole(stdin, stdout), glyphs=glyphs_for(unicode_ok(stdout)),
                        queue_input=tty, tty=stdin if tty else None)


async def _interactive(
    options: AppOptions,
    frontend: Frontend,
    *,
    resume: str | None,
    continue_latest: bool,
    adapter: ModelAdapter | None = None,
) -> int:
    try:
        app = await boot(options, frontend, adapter=adapter)
    except BootError as exc:
        if exc.code == SIGN_IN_FIRST:
            frontend.emit(Notice(SIGN_IN_FIRST_TEXT, "error"))
            return 1
        hint = {
            "no_provider": "Log in with 'claude' or 'codex', or set an API key, then run "
                           "'architect' again.",
            "setup_cancelled": "Run 'architect' again when you are ready.",
            "invalid_profile": "Check the profile file, or move it aside to run setup again.",
            "model_not_configured": "Pick a model from the profile (--model), or omit it.",
        }.get(exc.code, "Run 'architect --plain' for details, or check docs/usage.md.")
        frontend.emit(Notice(f"Architect could not start ({exc.code}). {hint}", "error"))
        return 1
    try:
        repl = Repl(app, frontend)
        if continue_latest or resume:
            repl.resume(resume)
        with _sigint_cancels_main():
            return await repl.run()
    finally:
        await close_mcp(app, options.cwd)
        app.close()


@contextlib.contextmanager
def _sigint_cancels_main() -> Iterator[None]:
    """Every Ctrl+C cancels what the loop is waiting on (a turn, a prompt), never the process.

    asyncio.run's own handler turns the second SIGINT of the whole run into a hard
    KeyboardInterrupt, so a turn interrupted early would make the next Ctrl+C at the
    prompt kill the CLI. The REPL decides instead: it cancels the running turn, and two
    in a row at an empty prompt exit. Setup keeps asyncio's handler (Ctrl+C there quits).
    """
    task = asyncio.current_task()
    if task is None or threading.current_thread() is not threading.main_thread():
        yield
        return
    loop = asyncio.get_running_loop()

    def handler(signum: int, frame: object) -> None:
        loop.call_soon_threadsafe(task.cancel)

    try:
        previous = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, handler)
    except (ValueError, OSError):
        yield
        return
    try:
        yield
    finally:
        with contextlib.suppress(ValueError, OSError, TypeError):
            signal.signal(signal.SIGINT, previous)


def main(
    argv: Sequence[str] | None = None,
    *,
    adapter: ModelAdapter | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    _utf8(stdout)
    try:
        args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
        cwd = Path.cwd()
        config: Config = load_config(agent_workspace(cwd, args.workspace), cwd)
        options = AppOptions(
            cwd=cwd,
            workspace=args.workspace,
            profile=args.profile,
            model=args.model,
            permission=cast(Permission | None, args.permission),
            probe=not args.no_probe,
            config=config,
        )
        if args.print_task is not None:
            task = stdin.read() if args.print_task == "-" else args.print_task
            if not task.strip():
                raise BootError("invalid_arguments")
            return asyncio.run(_print_mode(
                options, task, as_json=args.json, resume=args.resume,
                continue_latest=args.continue_latest, adapter=adapter,
                stdout=stdout, stderr=stderr,
            ))
        plain = args.plain or config.text("theme") == "plain"
        frontend = choose_frontend(plain=plain, stdin=stdin, stdout=stdout)
        return asyncio.run(_interactive(
            options, frontend, resume=args.resume, continue_latest=args.continue_latest,
            adapter=adapter,
        ))
    except SystemExit as exc:
        return int(exc.code or 0)
    except BootError as exc:
        stderr.write(json.dumps({"error": exc.code}) + "\n")
        return 1
    except KeyboardInterrupt:
        stderr.write(json.dumps({"error": "interrupted"}) + "\n")
        return 130
    except Exception as exc:
        # Provider and filesystem exceptions can carry secrets: a fixed code only.
        from architect.session.codes import code_for

        stderr.write(json.dumps({"error": code_for(exc)}) + "\n")
        return 1
