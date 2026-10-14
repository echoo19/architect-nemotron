"""Console I/O: scripted lines, stdin on a daemon thread, and a stdout that never raises."""

from __future__ import annotations

import asyncio
import io
import os
import queue
import sys
import threading
from collections.abc import Callable
from typing import Any

import pytest

from architect.session import io as io_module
from architect.session.io import Console, ScriptedConsole, StdConsole


async def test_scripted_console_records_prompts_and_returns_none_at_exhaustion() -> None:
    console: Console = ScriptedConsole(["hello\n", "approve 3fa9c2d1"])
    assert console.interactive is False
    assert await console.read("architect> ") == "hello"
    assert await console.read("review 3fa9c2d1> ") == "approve 3fa9c2d1"
    assert await console.read("compiling 3fa9c2d1> ") is None
    assert await console.read("compiling 3fa9c2d1> ") is None
    scripted = console
    assert isinstance(scripted, ScriptedConsole)
    assert scripted.prompts == [
        "architect> ",
        "review 3fa9c2d1> ",
        "compiling 3fa9c2d1> ",
        "compiling 3fa9c2d1> ",
    ]
    console.write("bye\n")
    assert scripted.transcript() == (
        "architect> hello\nreview 3fa9c2d1> approve 3fa9c2d1\ncompiling 3fa9c2d1> \n"
        "compiling 3fa9c2d1> \nbye\n"
    )
    assert scripted.output[-1] == "bye\n"


async def test_scripted_console_interactive_does_not_echo() -> None:
    console = ScriptedConsole(["quit"], interactive=True)
    assert console.interactive is True
    assert await console.read("architect> ") == "quit"
    assert console.transcript() == "architect> "


async def test_std_console_reads_lines_echoes_and_returns_none_at_eof() -> None:
    stdout = io.StringIO()
    console = StdConsole(io.StringIO("hello\r\nworld\n"), stdout)
    assert console.interactive is False
    assert await console.read("architect> ") == "hello"
    assert await console.read("architect> ") == "world"
    assert await console.read("architect> ") is None
    assert await console.read("architect> ") is None
    assert stdout.getvalue() == "architect> hello\narchitect> world\narchitect> \n"
    console.write("bye\n")
    assert stdout.getvalue().endswith("bye\n")


async def test_std_console_uses_one_daemon_thread() -> None:
    console = StdConsole(io.StringIO("a\nb\n"), io.StringIO())
    before = {t.name for t in threading.enumerate()}
    assert await console.read("> ") == "a"
    assert await console.read("> ") == "b"
    readers = [t for t in threading.enumerate() if t.name == "architect-stdin"]
    assert len(readers) <= 1
    assert all(t.daemon for t in readers)
    assert "architect-stdin" not in before
    assert await console.read("> ") is None


def test_write_never_raises_on_a_strict_legacy_code_page(monkeypatch: pytest.MonkeyPatch) -> None:
    buffer = io.BytesIO()
    stdout = io.TextIOWrapper(buffer, encoding="cp1252", errors="strict", newline="")
    monkeypatch.setattr(sys, "stdout", stdout)
    console = StdConsole(io.StringIO(""), sys.stdout)
    with pytest.raises(UnicodeEncodeError):
        sys.stdout.write("\u2713")
    console.write("\u2713 caf\u00e9 \u4e2d\n")
    sys.stdout.flush()
    assert buffer.getvalue() == b"\\u2713 caf\xe9 \\u4e2d\n"


def test_write_survives_a_closed_stdout() -> None:
    stdout = io.StringIO()
    stdout.close()
    StdConsole(io.StringIO(""), stdout).write("still fine\n")


def test_write_with_an_unknown_encoding_falls_back_to_ascii() -> None:
    class Odd(io.StringIO):
        encoding = "no-such-codec"  # type: ignore[assignment]

    stdout = Odd()
    StdConsole(io.StringIO(""), stdout).write("caf\u00e9\n")
    assert stdout.getvalue() == "caf\\xe9\n"


# ------------------------------------------------------------ reviewer regressions


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


ABORT = object()


def _scripted_console_reader(chunks: list[object]) -> object:
    """A stand-in for the win32 ReadConsoleW primitive: text chunks, Ctrl-C aborts, ^Z."""
    items = iter(chunks)

    def reader(stdin: object) -> object:
        def read_chunk() -> str | None:
            item = next(items, None)
            if item is ABORT:
                raise io_module._ConsoleReadAborted
            assert item is None or isinstance(item, str)
            return item

        return read_chunk

    return reader


async def test_ctrl_c_aborted_console_read_is_not_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reviewer defect 1: on a win32 console Ctrl-C completes the pending ReadConsoleW with
    ERROR_OPERATION_ABORTED. That is not end of input: the pump re-arms, delivers nothing
    (SIGINT reaches the main thread on its own) and drops the partial line conhost flushed.
    Only ^Z ends the input, and only then is the EOF sticky."""
    chunks: list[object] = ["hello\r\n", ABORT, "par", ABORT, "wor", "ld\r\n", "\x1a\r\n"]
    monkeypatch.setattr(io_module, "_console_reader", _scripted_console_reader(chunks))
    out = io.StringIO()
    console = StdConsole(_Tty(""), out)
    assert console.interactive
    assert await console.read("architect> ") == "hello"
    assert await console.read("compiling 3fa9c2d1> ") == "world"
    assert await console.read("review 3fa9c2d1> ") is None
    assert await console.read("review 3fa9c2d1> ") is None
    assert out.getvalue() == "architect> compiling 3fa9c2d1> review 3fa9c2d1> \n"


async def test_console_chunks_are_split_into_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks: list[object] = ["a\r\nb\r\n", "c", None]
    monkeypatch.setattr(io_module, "_console_reader", _scripted_console_reader(chunks))
    console = StdConsole(_Tty(""), io.StringIO())
    assert await console.read("> ") == "a"
    assert await console.read("> ") == "b"
    assert await console.read("> ") == "c"  # an unterminated final line, like readline()
    assert await console.read("> ") is None


def test_console_reader_exists_only_for_a_real_console() -> None:
    assert io_module._console_reader(io.StringIO("")) is None  # no file descriptor at all
    reader, writer = os.pipe()
    try:
        with os.fdopen(reader, encoding="utf-8") as piped:
            assert io_module._console_reader(piped) is None  # a descriptor, not a console
    finally:
        os.close(writer)


async def test_undecodable_stdin_bytes_are_replaced_not_eof() -> None:
    """Reviewer defect 2: a line that does not decode under the locale code page is not the
    end of the transcript. The wrapper is reconfigured to replace before the first read."""
    stdin = io.TextIOWrapper(
        io.BytesIO(b"list\n\xff\xfe bad\nquit\n"), encoding="utf-8", errors="strict"
    )
    console = StdConsole(stdin, io.StringIO())
    assert await console.read("> ") == "list"
    assert await console.read("> ") == "�� bad"
    assert await console.read("> ") == "quit"
    assert await console.read("> ") is None


class _Strict:
    """A stdin without ``reconfigure`` whose second line does not decode."""

    encoding = "utf-8"

    def __init__(self) -> None:
        self._items: list[object] = [
            "list\n",
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
            "quit\n",
            "",
        ]

    def readline(self) -> str:
        item = self._items.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, str)
        return item

    def isatty(self) -> bool:
        return False


async def test_decode_error_without_reconfigure_is_an_empty_line() -> None:
    console = StdConsole(_Strict(), io.StringIO())  # type: ignore[arg-type]
    assert await console.read("> ") == "list"
    assert await console.read("> ") == ""
    assert await console.read("> ") == "quit"
    assert await console.read("> ") is None


# ------------------------------------------------------------ coverage: stdin edge cases


class _NoIsatty:
    """A stdin that has no ``isatty`` at all; the contract says that is non-interactive."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def readline(self) -> str:
        return self._lines.pop(0) if self._lines else ""


async def test_stdin_without_isatty_is_non_interactive_and_echoes() -> None:
    out = io.StringIO()
    console = StdConsole(_NoIsatty(["list\n"]), out)  # type: ignore[arg-type]
    assert console.interactive is False
    assert await console.read("> ") == "list"
    assert await console.read("> ") is None
    assert out.getvalue() == "> list\n> \n"


async def test_closed_stdin_is_non_interactive_and_reads_as_a_sticky_eof() -> None:
    stdin = io.StringIO("never read\n")
    stdin.close()  # isatty() and readline() both raise ValueError now
    out = io.StringIO()
    console = StdConsole(stdin, out)
    assert console.interactive is False
    assert await console.read("architect> ") is None
    assert out.getvalue() == "architect> \n"
    # EOF is sticky: the second read answers None without printing anything more.
    assert await console.read("architect> ") is None
    assert out.getvalue() == "architect> \n"


class _Broken:
    encoding = "utf-8"

    def __init__(self) -> None:
        self._items: list[object] = ["list\n", OSError(5, "Input/output error")]

    def readline(self) -> str:
        item = self._items.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, str)
        return item

    def isatty(self) -> bool:
        return False


async def test_readline_os_error_is_end_of_input_not_an_exception() -> None:
    out = io.StringIO()
    console = StdConsole(_Broken(), out)  # type: ignore[arg-type]
    assert await console.read("> ") == "list"
    assert await console.read("> ") is None
    assert await console.read("> ") is None
    assert out.getvalue() == "> list\n> \n"


class _Unreconfigurable(io.StringIO):
    """A wrapper whose ``reconfigure`` refuses; the pump must still read every line."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.reconfigure_calls: list[dict[str, object]] = []

    def reconfigure(self, **kw: object) -> None:  # type: ignore[override]
        self.reconfigure_calls.append(kw)
        raise TypeError("reconfigure() got an unexpected keyword argument")


async def test_a_failing_reconfigure_is_ignored_and_lines_still_flow() -> None:
    stdin = _Unreconfigurable("list\nquit\n")
    out = io.StringIO()
    console = StdConsole(stdin, out)
    assert await console.read("> ") == "list"
    assert await console.read("> ") == "quit"
    assert await console.read("> ") is None
    assert stdin.reconfigure_calls == [{"errors": "replace"}]
    assert out.getvalue() == "> list\n> quit\n> \n"


async def test_a_real_pipe_is_read_echoed_and_ended_by_eof() -> None:
    reader, writer = os.pipe()
    with os.fdopen(writer, "wb") as sink:
        sink.write(b"list\r\ncaf\xc3\xa9 \xff\nquit\n")
    with os.fdopen(reader, encoding="utf-8", errors="strict") as stdin:
        out = io.StringIO()
        console = StdConsole(stdin, out)
        assert console.interactive is False
        assert await console.read("> ") == "list"
        assert await console.read("> ") == "caf\u00e9 \ufffd"  # undecodable byte replaced
        assert await console.read("> ") == "quit"
        assert await console.read("> ") is None
    assert out.getvalue() == "> list\n> caf\u00e9 \ufffd\n> quit\n> \n"


def test_write_on_a_strict_ascii_stdout_escapes_instead_of_raising() -> None:
    buffer = io.BytesIO()
    stdout = io.TextIOWrapper(buffer, encoding="ascii", errors="strict", newline="")
    with pytest.raises(UnicodeEncodeError):
        stdout.write("\u00e9")
    StdConsole(io.StringIO(""), stdout).write("caf\u00e9 \u2713 \U0001f600\n")
    stdout.flush()
    assert buffer.getvalue() == b"caf\\xe9 \\u2713 \\U0001f600\n"


# ------------------------------------------------------------ coverage: the loop closes first


class _Gated:
    """A stdin whose ``readline`` blocks on a queue, so the test controls when lines arrive."""

    def __init__(self) -> None:
        self.lines: queue.Queue[str] = queue.Queue()

    def readline(self) -> str:
        return self.lines.get()

    def isatty(self) -> bool:
        return False


def _new_reader_threads(before: set[threading.Thread]) -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "architect-stdin" and t not in before]


def test_line_reader_thread_ends_once_the_loop_is_closed() -> None:
    """The daemon reader must not spin forever when the loop is gone: the first line that
    cannot be delivered ends the thread, and the line itself is dropped, not raised."""
    before = set(threading.enumerate())
    stdin = _Gated()
    console = StdConsole(stdin, io.StringIO())  # type: ignore[arg-type]
    loop = asyncio.new_event_loop()
    try:
        stdin.lines.put("a\n")
        assert loop.run_until_complete(console.read("> ")) == "a"
    finally:
        loop.close()
    (thread,) = _new_reader_threads(before)
    assert thread.daemon and thread.is_alive()
    stdin.lines.put("b\n")
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_console_reader_thread_ends_once_the_loop_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks: queue.Queue[str] = queue.Queue()

    def reader(stdin: object) -> object:
        return chunks.get

    monkeypatch.setattr(io_module, "_console_reader", reader)
    before = set(threading.enumerate())
    console = StdConsole(_Tty(""), io.StringIO())
    loop = asyncio.new_event_loop()
    try:
        chunks.put("a\r\n")
        assert loop.run_until_complete(console.read("> ")) == "a"
    finally:
        loop.close()
    (thread,) = _new_reader_threads(before)
    assert thread.daemon and thread.is_alive()
    chunks.put("b\r\nc\r\n")
    thread.join(timeout=5)
    assert not thread.is_alive()


# ------------------------------------------------------------ coverage: the win32 console pump


def _failing_console_reader(chunks: list[object]) -> object:
    """Like ``_scripted_console_reader`` but an exception instance is raised by the read."""
    items = iter(chunks)

    def reader(stdin: object) -> object:
        def read_chunk() -> str | None:
            item = next(items, None)
            if isinstance(item, BaseException):
                raise item
            assert item is None or isinstance(item, str)
            return item

        return read_chunk

    return reader


async def test_a_failing_console_read_is_end_of_input_after_the_pending_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks: list[object] = ["first\r\n", "unterminated", OSError(6, "The handle is invalid")]
    monkeypatch.setattr(io_module, "_console_reader", _failing_console_reader(chunks))
    out = io.StringIO()
    console = StdConsole(_Tty(""), out)
    assert await console.read("> ") == "first"
    assert await console.read("> ") == "unterminated"
    assert await console.read("> ") is None
    assert await console.read("> ") is None
    assert out.getvalue() == "> > > \n"  # the sticky EOF prints no further prompt


async def test_a_value_error_from_the_console_read_is_end_of_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks: list[object] = ["a\r\n", ValueError("closed")]
    monkeypatch.setattr(io_module, "_console_reader", _failing_console_reader(chunks))
    console = StdConsole(_Tty(""), io.StringIO())
    assert await console.read("> ") == "a"
    assert await console.read("> ") is None


async def test_ctrl_z_ends_console_input_even_mid_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks: list[object] = ["last\r\n\x1aignored\r\n", "never\r\n"]
    monkeypatch.setattr(io_module, "_console_reader", _failing_console_reader(chunks))
    console = StdConsole(_Tty(""), io.StringIO())
    assert await console.read("> ") == "last"
    assert await console.read("> ") is None
    assert await console.read("> ") is None


def test_console_reader_is_absent_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    reader, writer = os.pipe()
    try:
        with os.fdopen(reader, encoding="utf-8") as piped:
            assert io_module._console_reader(piped) is None
    finally:
        os.close(writer)


async def test_a_posix_terminal_uses_readline_without_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    out = io.StringIO()
    console = StdConsole(_Tty("list\nquit\n"), out)
    assert console.interactive is True
    assert await console.read("> ") == "list"
    assert await console.read("> ") == "quit"
    assert await console.read("> ") is None
    assert out.getvalue() == "> > > \n"


class _Proc:
    """A stand-in for one kernel32 entry point: callable, and accepts argtypes/restype."""

    def __init__(self, fn: Callable[..., object]) -> None:
        self._fn = fn

    def __call__(self, *args: object) -> object:
        return self._fn(*args)


class _FakeKernel32:
    """``GetConsoleMode``/``ReadConsoleW`` driven by a script of (text, ok, last_error)."""

    def __init__(self, reads: list[tuple[str, int, int]], *, console: bool = True) -> None:
        self._reads = list(reads)
        self._console = console
        self.handles: list[object] = []
        self.sizes: list[int] = []
        self.GetConsoleMode = _Proc(self._get_console_mode)
        self.ReadConsoleW = _Proc(self._read_console)

    def _get_console_mode(self, handle: object, mode_ref: object) -> int:
        self.handles.append(handle)
        return 1 if self._console else 0

    def _read_console(
        self, handle: object, buffer: Any, size: int, count_ref: Any, reserved: object
    ) -> int:
        import ctypes

        self.handles.append(handle)
        self.sizes.append(size)
        text, ok, error = self._reads.pop(0)
        buffer.value = text
        count_ref._obj.value = len(text)
        ctypes.set_last_error(error)
        return ok


win32_only = pytest.mark.skipif(sys.platform != "win32", reason="ReadConsoleW is win32 only")


@win32_only
def test_console_reader_reads_chunks_aborts_and_ends_via_kernel32(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ReadConsoleW seam itself, with kernel32 replaced: a chunk is returned as typed, a lone
    surrogate is replaced before it can poison a utf-8 write, ERROR_OPERATION_ABORTED with zero
    characters is a Ctrl-C (raised, not EOF), and any other empty or failed read is the end."""
    import ctypes
    import msvcrt

    kernel32 = _FakeKernel32(
        [
            ("hello\r\n", 1, 0),
            ("\ud800x\r\n", 1, 0),
            ("", 0, io_module._ERROR_OPERATION_ABORTED),
            ("", 1, 0),
            ("", 0, 6),
        ]
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, use_last_error=False: kernel32)
    reader, writer = os.pipe()
    try:
        with os.fdopen(reader, encoding="utf-8") as stdin:
            read_chunk = io_module._console_reader(stdin)
            assert read_chunk is not None
            expected_handle = msvcrt.get_osfhandle(stdin.fileno())
            assert read_chunk() == "hello\r\n"
            assert read_chunk() == "\ufffdx\r\n"
            with pytest.raises(io_module._ConsoleReadAborted):
                read_chunk()
            assert read_chunk() is None  # zero characters without the abort code: ^Z / EOF
            assert read_chunk() is None  # a failed call is the end of input too
    finally:
        os.close(writer)
    assert kernel32.handles == [expected_handle] * 6
    assert kernel32.sizes == [io_module._CONSOLE_CHUNK] * 5


@win32_only
def test_console_reader_is_absent_when_get_console_mode_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes

    kernel32 = _FakeKernel32([("never\r\n", 1, 0)], console=False)
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, use_last_error=False: kernel32)
    reader, writer = os.pipe()
    try:
        with os.fdopen(reader, encoding="utf-8") as stdin:
            assert io_module._console_reader(stdin) is None
    finally:
        os.close(writer)
    assert kernel32.sizes == []


class _ConsoleLike:
    """A tty stdin with a descriptor, so ``StdConsole`` takes the ReadConsoleW path."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def fileno(self) -> int:
        return self._fd

    def isatty(self) -> bool:
        return True

    def readline(self) -> str:
        raise AssertionError("a real console is never read with readline()")


@win32_only
async def test_std_console_on_a_win32_console_uses_kernel32_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes

    kernel32 = _FakeKernel32(
        [
            ("hel", 1, 0),
            ("lo\r\n", 1, 0),
            ("dropped", 0, io_module._ERROR_OPERATION_ABORTED),
            ("\ud800 ok\r\n", 1, 0),
            ("\x1a\r\n", 1, 0),
        ]
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, use_last_error=False: kernel32)
    reader, writer = os.pipe()
    try:
        out = io.StringIO()
        console = StdConsole(_ConsoleLike(reader), out)  # type: ignore[arg-type]
        assert console.interactive is True
        assert await console.read("architect> ") == "hello"
        assert await console.read("architect> ") == "\ufffd ok"
        assert await console.read("architect> ") is None
        assert await console.read("architect> ") is None
    finally:
        os.close(reader)
        os.close(writer)
    assert out.getvalue() == "architect> architect> architect> \n"
    assert kernel32.sizes == [io_module._CONSOLE_CHUNK] * 5
