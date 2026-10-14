"""Console I/O for the session: a protocol, the stdin/stdout console and a scripted one.

``StdConsole`` reads stdin on one daemon thread and hands lines to the asyncio loop with
``call_soon_threadsafe``. That is deliberate: ``asyncio.to_thread`` would leave a worker
blocked in ``readline`` after the operator quits, so a session that just finished a
compile could hang at exit waiting for a keypress. ``write`` never raises on a legacy
Windows code page; the operator's terminal is not allowed to crash the session.

On a real win32 console the reader thread calls ``ReadConsoleW`` itself (ctypes) instead
of ``stdin.readline()``. Ctrl-C makes conhost complete the pending console read with zero
characters and ``ERROR_OPERATION_ABORTED``; CPython's console object turns that into
``KeyboardInterrupt`` only on the main thread, so on the reader thread ``readline`` would
return ``""`` and the session would take the operator's Ctrl-C for end of input. Reading
the console directly keeps the error code visible: an aborted read delivers nothing and
the pump re-arms, while SIGINT reaches the main thread through asyncio as usual. Only ^Z
ends console input. Every other stdin (pipes, files, POSIX terminals) keeps ``readline``,
reconfigured to replace undecodable bytes so one bad line cannot end a piped transcript.
"""

from __future__ import annotations

import asyncio
import functools
import sys
import threading
from collections.abc import Callable, Sequence
from typing import Protocol, TextIO

__all__ = ["Console", "ScriptedConsole", "StdConsole"]

_ERROR_OPERATION_ABORTED = 995
_CONSOLE_CHUNK = 4096
_CTRL_Z = "\x1a"


class _ConsoleReadAborted(Exception):
    """``ReadConsoleW`` finished with ``ERROR_OPERATION_ABORTED``: Ctrl-C, not end of input."""


def _console_reader(stdin: TextIO) -> Callable[[], str | None] | None:
    """A ``ReadConsoleW``-backed chunk reader when ``stdin`` is a real win32 console.

    None on every other platform and for every other stdin (no descriptor, a pipe, a file,
    ``NUL``), where ``readline`` behaves. The reader returns the characters of one console
    read (a whole line in cooked mode, or a full buffer), None at end of input or on any
    other failure, and raises ``_ConsoleReadAborted`` for a read Ctrl-C aborted.
    """
    if sys.platform != "win32":
        return None
    import ctypes
    import msvcrt
    from ctypes import wintypes

    try:
        handle = msvcrt.get_osfhandle(stdin.fileno())
    except (AttributeError, OSError, ValueError):
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
    kernel32.GetConsoleMode.restype = wintypes.BOOL
    mode = wintypes.DWORD(0)
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return None
    read_console = kernel32.ReadConsoleW
    read_console.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.LPDWORD,
        wintypes.LPVOID,
    ]
    read_console.restype = wintypes.BOOL
    buffer = ctypes.create_unicode_buffer(_CONSOLE_CHUNK)

    def read_chunk() -> str | None:
        count = wintypes.DWORD(0)
        ctypes.set_last_error(0)
        ok = read_console(handle, buffer, _CONSOLE_CHUNK, ctypes.byref(count), None)
        error = ctypes.get_last_error()
        if not ok or count.value == 0:
            if error == _ERROR_OPERATION_ABORTED:
                raise _ConsoleReadAborted
            return None
        text = ctypes.wstring_at(buffer, count.value)
        # Typed text is UTF-16; a lone surrogate must not poison a later utf-8 write.
        return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")

    return read_chunk


class Console(Protocol):
    @property
    def interactive(self) -> bool: ...

    async def read(self, prompt: str) -> str | None: ...

    def write(self, text: str) -> None: ...


class StdConsole:
    def __init__(self, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._queue: asyncio.Queue[str | None] | None = None
        self._thread: threading.Thread | None = None
        self._eof = False
        try:
            self._interactive = bool(stdin.isatty())
        except (AttributeError, ValueError):
            self._interactive = False

    @property
    def interactive(self) -> bool:
        return self._interactive

    def _start(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[str | None]) -> None:
        def deliver(item: str | None) -> bool:
            """Hand one line (or the EOF) to the loop; False once nobody is left to read."""
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:
                return False  # The loop closed first.
            return item is not None

        def pump_lines() -> None:
            while True:
                try:
                    line = self._stdin.readline()
                except UnicodeDecodeError:
                    # A wrapper that could not be reconfigured: the undecodable input is
                    # an empty line, never the end of the transcript.
                    line = "\n"
                except (OSError, ValueError):
                    line = ""
                if not deliver(None if line == "" else line.rstrip("\r\n")):
                    return

        def pump_console(read_chunk: Callable[[], str | None]) -> None:
            pending = ""
            while True:
                try:
                    chunk = read_chunk()
                except _ConsoleReadAborted:
                    pending = ""  # Ctrl-C: conhost flushed the line; SIGINT is on its way.
                    continue
                except (OSError, ValueError):
                    chunk = None
                eof = chunk is None
                if chunk is not None and _CTRL_Z in chunk:
                    chunk, eof = chunk.split(_CTRL_Z, 1)[0], True
                pending += chunk or ""
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    if not deliver(line.rstrip("\r")):
                        return
                if eof:
                    if pending:
                        deliver(pending)  # an unterminated last line, like readline()
                    deliver(None)
                    return

        target: Callable[[], None] = pump_lines
        read_chunk = _console_reader(self._stdin) if self._interactive else None
        if read_chunk is not None:
            target = functools.partial(pump_console, read_chunk)
        else:
            reconfigure = getattr(self._stdin, "reconfigure", None)
            if callable(reconfigure):
                try:
                    # A Windows pipe decodes with the locale code page and errors="strict";
                    # one undecodable byte must not end the session silently.
                    reconfigure(errors="replace")
                except (OSError, ValueError, TypeError):
                    pass
        self._thread = threading.Thread(target=target, name="architect-stdin", daemon=True)
        self._thread.start()

    async def read(self, prompt: str) -> str | None:
        """Print the prompt and wait for a line; None at EOF, never an exception."""
        if self._eof:
            return None
        if self._queue is None:
            self._queue = asyncio.Queue()
            self._start(asyncio.get_running_loop(), self._queue)
        self.write(prompt)
        line = await self._queue.get()
        if line is None:
            self._eof = True
            self.write("\n")
            return None
        if not self._interactive:
            # Piped transcripts read naturally when the command follows its prompt.
            self.write(line + "\n")
        return line

    def write(self, text: str) -> None:
        encoding = getattr(self._stdout, "encoding", None) or "utf-8"
        try:
            safe = text.encode(encoding, errors="backslashreplace").decode(encoding, "replace")
        except LookupError:
            safe = text.encode("ascii", errors="backslashreplace").decode("ascii")
        try:
            self._stdout.write(safe)
            self._stdout.flush()
        except (OSError, ValueError):
            pass  # A closed or broken stdout must not take the session down.


class ScriptedConsole:
    """Feeds fixed lines and records everything written; the test double and --script driver."""

    def __init__(self, lines: Sequence[str], *, interactive: bool = False) -> None:
        self._lines = [line.rstrip("\r\n") for line in lines]
        self._index = 0
        self._interactive = interactive
        self.prompts: list[str] = []
        self.output: list[str] = []

    @property
    def interactive(self) -> bool:
        return self._interactive

    async def read(self, prompt: str) -> str | None:
        self.prompts.append(prompt)
        self.write(prompt)
        if self._index >= len(self._lines):
            self.write("\n")
            return None
        line = self._lines[self._index]
        self._index += 1
        if not self._interactive:
            self.write(line + "\n")
        return line

    def write(self, text: str) -> None:
        self.output.append(text)

    def transcript(self) -> str:
        return "".join(self.output)
