"""MCP transports: spawned stdio servers and streamable HTTP.

A transport moves JSON-RPC messages; the client correlates them. Both hand every incoming
message to a ``Sink``: ``deliver`` for a message, ``error`` for a failure that does not end
the connection (an oversized message), ``closed`` when the connection is gone.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

import httpx

from .process import Job, kill_tree, spawn_options, terminate_tree
from .protocol import MAX_MESSAGE_BYTES, MCPError, encode

__all__ = ["HttpTransport", "Sink", "StdioTransport", "Transport", "resolve_command"]

STDERR_TAIL = 4096
CHUNK = 64 * 1024
CLOSE_GRACE_S = 2.0


class Sink(Protocol):
    def deliver(self, message: dict[str, Any]) -> None: ...

    def error(self, error: MCPError) -> None: ...

    def closed(self, error: MCPError) -> None: ...


class Transport(Protocol):
    kind: str

    async def start(self, sink: Sink) -> None: ...

    async def send(self, message: Mapping[str, Any]) -> None: ...

    def negotiated(self, version: str) -> None: ...

    async def close(self) -> None: ...

    def kill_now(self) -> None:
        """Best-effort synchronous stop (interpreter exit); never raises."""
        ...

    def diagnostics(self) -> str: ...


def _deliver_raw(sink: Sink, raw: bytes) -> list[dict[str, Any]]:
    """Parse one JSON-RPC message or batch and deliver it; non-JSON noise is dropped."""
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return []
    items = value if isinstance(value, list) else [value]
    messages = [item for item in items if isinstance(item, dict)]
    for message in messages:
        sink.deliver(message)
    return messages


def _answers(message: Mapping[str, Any], want: object) -> bool:
    return message.get("id") == want and ("result" in message or "error" in message)


# -- stdio ------------------------------------------------------------------------------


def resolve_command(command: str, env: Mapping[str, str], cwd: Path | None) -> str:
    """The executable for ``command`` (PATH and, on Windows, PATHEXT such as ``npx.cmd``)."""
    if not command.strip():
        raise MCPError("command_not_found", "empty command")
    candidate = Path(command)
    if candidate.is_absolute() or os.sep in command or (os.altsep and os.altsep in command):
        if not candidate.is_absolute() and cwd is not None:
            candidate = cwd / candidate
        found = shutil.which(str(candidate))
        if found:
            return found
        if candidate.is_file():
            return str(candidate)
        raise MCPError("command_not_found", command)
    search = env.get("PATH") or env.get("Path") or os.environ.get("PATH")
    found = shutil.which(command, path=search)
    if found is None:
        raise MCPError("command_not_found", command)
    return found


class StdioTransport:
    """A server process speaking newline-delimited JSON on stdin/stdout."""

    kind = "stdio"

    def __init__(
        self,
        command: str,
        args: tuple[str, ...] = (),
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        *,
        max_bytes: int = MAX_MESSAGE_BYTES,
    ) -> None:
        self.command = command
        self.args = args
        self.env = dict(env or {})
        self.cwd = cwd
        self.max_bytes = max_bytes
        self.process: asyncio.subprocess.Process | None = None
        self._job: Job | None = None
        self._sink: Sink | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._write = asyncio.Lock()
        self._stderr = bytearray()
        self._closing = False
        self.noise_lines = 0

    async def start(self, sink: Sink) -> None:
        self._sink = sink
        env = {**os.environ, **self.env}
        if self.cwd is not None and not self.cwd.is_dir():
            raise MCPError("cwd_not_found", str(self.cwd))
        executable = resolve_command(self.command, env, self.cwd)
        try:
            self.process = await asyncio.create_subprocess_exec(
                executable,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(self.cwd) if self.cwd is not None else None,
                **spawn_options(),
            )
        except OSError as exc:
            raise MCPError("spawn_failed", f"{type(exc).__name__}: {exc.strerror or ''}") from None
        self._job = Job(self.process.pid)
        self._tasks = [
            asyncio.create_task(self._read_stdout()),
            asyncio.create_task(self._drain_stderr()),
        ]

    def _line(self, sink: Sink, line: bytes) -> None:
        if len(line) > self.max_bytes:
            sink.error(MCPError("message_too_large", f"over {self.max_bytes} bytes"))
        elif line and not _deliver_raw(sink, line):
            self.noise_lines += 1  # a server that logs to stdout; not a message

    async def _read_stdout(self) -> None:
        process, sink = self.process, self._sink
        assert process is not None and process.stdout is not None and sink is not None
        buffer = bytearray()
        skipping = False  # inside an oversized line: drop bytes up to its newline
        try:
            while True:
                chunk = await process.stdout.read(CHUNK)
                if not chunk:
                    break
                start = 0
                while True:
                    end = chunk.find(b"\n", start)
                    if end < 0:
                        if not skipping:
                            buffer += chunk[start:]
                            if len(buffer) > self.max_bytes:
                                buffer.clear()
                                skipping = True
                                sink.error(
                                    MCPError("message_too_large", f"over {self.max_bytes} bytes")
                                )
                        break
                    piece = chunk[start:end]
                    start = end + 1
                    if skipping:
                        skipping = False
                        continue
                    buffer += piece
                    line = bytes(buffer).strip()
                    buffer.clear()
                    self._line(sink, line)
        except (OSError, ValueError):
            pass
        if not self._closing:
            code = None
            with contextlib.suppress(TimeoutError):
                code = await asyncio.wait_for(process.wait(), 1.0)
            sink.closed(MCPError("server_exited", f"exit code {code}{self._tail_hint()}"))

    async def _drain_stderr(self) -> None:
        process = self.process
        assert process is not None and process.stderr is not None
        with contextlib.suppress(OSError, ValueError):
            while True:
                chunk = await process.stderr.read(CHUNK)
                if not chunk:
                    break
                self._stderr += chunk
                if len(self._stderr) > STDERR_TAIL:
                    del self._stderr[: len(self._stderr) - STDERR_TAIL]

    def _tail_hint(self) -> str:
        tail = self.diagnostics().strip().splitlines()
        return f"; stderr: {tail[-1][:160]}" if tail else ""

    def diagnostics(self) -> str:
        """The last few KB the server wrote to stderr (kept for /mcp, never logged)."""
        return self._stderr.decode("utf-8", errors="replace")

    async def send(self, message: Mapping[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.returncode is not None:
            raise MCPError("server_exited", "not running")
        data = encode(message) + b"\n"
        async with self._write:
            try:
                process.stdin.write(data)
                await process.stdin.drain()
            except (OSError, RuntimeError):
                raise MCPError("server_exited", "stdin closed") from None

    def negotiated(self, version: str) -> None:
        return None

    async def close(self) -> None:
        """Close stdin, give the server a moment, then stop its whole tree."""
        self._closing = True
        process = self.process
        if process is not None:
            if process.stdin is not None:
                with contextlib.suppress(OSError, RuntimeError):
                    process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), CLOSE_GRACE_S)
            except TimeoutError:
                await asyncio.to_thread(terminate_tree, process.pid)
                try:
                    await asyncio.wait_for(process.wait(), CLOSE_GRACE_S)
                except TimeoutError:
                    await asyncio.to_thread(kill_tree, process.pid)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(process.wait(), 5.0)
            if sys.platform != "win32":
                await asyncio.to_thread(kill_tree, process.pid)  # leftover group members
        if self._job is not None:
            self._job.close()  # Windows: ends anything still in the tree
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []

    def kill_now(self) -> None:
        self._closing = True
        process = self.process
        try:
            if process is not None and process.returncode is None:
                kill_tree(process.pid)
        except Exception:
            pass
        if self._job is not None:
            with contextlib.suppress(Exception):
                self._job.close()


# -- streamable HTTP --------------------------------------------------------------------


class HttpTransport:
    """POST each message; the reply is JSON or an SSE stream; ``Mcp-Session-Id`` is kept."""

    kind = "http"

    def __init__(
        self,
        url: str,
        headers: Mapping[str, str] | None = None,
        *,
        timeout_s: float = 60.0,
        max_bytes: int = MAX_MESSAGE_BYTES,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url
        self.headers = dict(headers or {})
        self.timeout_s = timeout_s
        self.max_bytes = max_bytes
        self.http_transport = http_transport
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        self._client: httpx.AsyncClient | None = None
        self._sink: Sink | None = None
        self._last_status = ""

    async def start(self, sink: Sink) -> None:
        self._sink = sink
        self._client = httpx.AsyncClient(
            transport=self.http_transport,
            timeout=httpx.Timeout(self.timeout_s, connect=min(10.0, self.timeout_s)),
            follow_redirects=False,
        )

    def negotiated(self, version: str) -> None:
        self.protocol_version = version

    def diagnostics(self) -> str:
        return self._last_status

    def _headers(self) -> dict[str, str]:
        headers = {
            **self.headers,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    async def send(self, message: Mapping[str, Any]) -> None:
        client, sink = self._client, self._sink
        if client is None or sink is None:
            raise MCPError("closed", "not started")
        want = message.get("id") if "method" in message and "id" in message else None
        body = encode(message)
        try:
            async with client.stream(
                "POST", self.url, content=body, headers=self._headers()
            ) as reply:
                status = reply.status_code
                self._last_status = f"HTTP {status}"
                session = reply.headers.get("mcp-session-id")
                if session and len(session) <= 1024 and session.isprintable():
                    self.session_id = session
                if status == 404 and self.session_id and message.get("method") != "initialize":
                    raise MCPError("session_expired", "HTTP 404")
                if status in (401, 403):
                    raise MCPError("unauthorized", f"HTTP {status} (check the headers)")
                if status >= 400:
                    raise MCPError("http_error", f"HTTP {status}")
                if status == 202 or want is None:
                    return
                kind = reply.headers.get("content-type", "").split(";")[0].strip().lower()
                if kind == "text/event-stream":
                    await self._events(reply, sink, want)
                elif kind == "application/json":
                    raw = await self._body(reply)
                    if not any(_answers(m, want) for m in _deliver_raw(sink, raw)):
                        raise MCPError("protocol_error", "no response in the reply")
                else:
                    raise MCPError("protocol_error", f"content type {kind or 'none'}")
        except httpx.TimeoutException:
            raise MCPError("timeout", "HTTP read") from None
        except httpx.HTTPError as exc:
            raise MCPError("http_error", type(exc).__name__) from None

    async def _body(self, reply: httpx.Response) -> bytes:
        data = bytearray()
        async for chunk in reply.aiter_bytes():
            data += chunk
            if len(data) > self.max_bytes:
                raise MCPError("message_too_large", f"over {self.max_bytes} bytes")
        return bytes(data)

    async def _events(self, reply: httpx.Response, sink: Sink, want: object) -> None:
        """Read SSE events until the response to ``want`` arrives."""
        buffer = bytearray()
        data: list[bytes] = []
        size = 0
        event = ""
        async for chunk in reply.aiter_bytes():
            buffer += chunk
            if len(buffer) > self.max_bytes:
                raise MCPError("message_too_large", f"over {self.max_bytes} bytes")
            while True:
                end = buffer.find(b"\n")
                if end < 0:
                    break
                line = bytes(buffer[:end]).rstrip(b"\r")
                del buffer[: end + 1]
                if not line:
                    if data and event in ("", "message"):
                        messages = _deliver_raw(sink, b"\n".join(data))
                        if any(_answers(m, want) for m in messages):
                            return  # the response arrived; the server closes the stream
                    data, size, event = [], 0, ""
                    continue
                if line.startswith(b":"):
                    continue
                name, _, value = line.partition(b":")
                value = value[1:] if value.startswith(b" ") else value
                if name == b"data":
                    data.append(value)
                    size += len(value)
                    if size > self.max_bytes:
                        raise MCPError("message_too_large", f"over {self.max_bytes} bytes")
                elif name == b"event":
                    event = value.decode("utf-8", errors="replace")
        if data and event in ("", "message"):
            if any(_answers(m, want) for m in _deliver_raw(sink, b"\n".join(data))):
                return
        raise MCPError("protocol_error", "the event stream ended without a response")

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        if self.session_id:  # end the session on the server (best effort; 405 is fine)
            with contextlib.suppress(httpx.HTTPError):
                await client.request("DELETE", self.url, headers=self._headers(), timeout=5.0)
        await client.aclose()

    def kill_now(self) -> None:
        self._client = None
