"""An MCP client session over one transport (JSON-RPC 2.0 request/response correlation).

``connect`` runs the handshake (``initialize`` with the newest protocol version, accepting
any supported version back, then ``notifications/initialized``). Every request has a
timeout; a timed-out or cancelled request is withdrawn with ``notifications/cancelled``.
Server-to-client requests are answered (``ping``) or refused (method not found): this
client offers no roots, sampling or elicitation.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .protocol import (
    CLIENT_INFO,
    PROTOCOL_VERSION,
    SUPPORTED_VERSIONS,
    MCPError,
    RPCError,
    clip,
    content_text,
    notification,
    request,
    response,
)
from .transport import Transport

__all__ = ["CallResult", "MCPClient", "ServerInfo", "ToolInfo"]

RESULT_CHARS = 20_000
MAX_PAGES = 50
MAX_TOOLS = 500
DESCRIPTION_CHARS = 1_000
CANCEL_SEND_S = 2.0
METHOD_NOT_FOUND = -32601


@dataclass(frozen=True)
class ServerInfo:
    name: str
    version: str
    protocol_version: str
    capabilities: dict[str, Any] = field(default_factory=dict)
    instructions: str = ""


@dataclass(frozen=True)
class ToolInfo:
    name: str
    description: str
    input_schema: dict[str, Any]
    title: str = ""
    read_only: bool = False  # the server's readOnlyHint annotation (a hint, not a guarantee)


@dataclass(frozen=True)
class CallResult:
    text: str
    is_error: bool = False
    truncated: bool = False


class MCPClient:
    """One connection to one server; safe for concurrent requests."""

    def __init__(
        self,
        transport: Transport,
        *,
        timeout_s: float = 60.0,
        result_chars: int = RESULT_CHARS,
        on_tools_changed: Callable[[], None] | None = None,
    ) -> None:
        self.transport = transport
        self.timeout_s = timeout_s
        self.result_chars = result_chars
        self.on_tools_changed = on_tools_changed
        self.info: ServerInfo | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._background: set[asyncio.Task[None]] = set()
        self._failure: MCPError | None = None
        self._started = False

    # -- Sink -------------------------------------------------------------------------

    def deliver(self, message: dict[str, Any]) -> None:
        if "id" in message and ("result" in message or "error" in message):
            key = message.get("id")
            future = self._pending.get(key) if isinstance(key, int) else None
            if future is not None and not future.done():
                future.set_result(message)
            return
        method = message.get("method")
        if not isinstance(method, str):
            return
        if "id" in message:  # a request from the server
            if method == "ping":
                self._background_send(response(message["id"], {}))
            else:
                self._background_send(
                    response(message["id"], error=(METHOD_NOT_FOUND, "method not supported"))
                )
        elif method == "notifications/tools/list_changed" and self.on_tools_changed:
            self.on_tools_changed()

    def error(self, error: MCPError) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(MCPError(error.code, error.detail))

    def closed(self, error: MCPError) -> None:
        self._failure = error
        self.error(error)

    # -- state ------------------------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._started and self._failure is None

    @property
    def failure(self) -> MCPError | None:
        return self._failure

    def _background_send(self, message: dict[str, Any]) -> None:
        async def send() -> None:
            with contextlib.suppress(MCPError, TimeoutError):
                await asyncio.wait_for(self.transport.send(message), CANCEL_SEND_S)

        task = asyncio.get_running_loop().create_task(send())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # -- requests ---------------------------------------------------------------------

    async def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """One request's ``result``; RPC errors raise ``RPCError``, others ``MCPError``."""
        if self._failure is not None:
            raise MCPError(self._failure.code, self._failure.detail)
        limit = self.timeout_s if timeout_s is None else timeout_s
        request_id = next(self._ids)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            async with asyncio.timeout(limit):
                await self.transport.send(request(request_id, method, params))
                message = await future
        except TimeoutError:
            if method != "initialize":
                self._cancel(request_id, "timeout")
            raise MCPError("timeout", f"{method} after {limit:g}s") from None
        except asyncio.CancelledError:
            if method != "initialize":
                self._cancel(request_id, "cancelled by the user")
            raise
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
        error = message.get("error")
        if error is not None:
            if isinstance(error, dict):
                code = error.get("code")
                raise RPCError(code if isinstance(code, int) else 0,
                               str(error.get("message", "")), error.get("data"))
            raise RPCError(0, "malformed error")
        result = message.get("result")
        if not isinstance(result, dict):
            raise MCPError("protocol_error", f"{method} result is not an object")
        return result

    def _cancel(self, request_id: int, reason: str) -> None:
        if self._failure is None:
            self._background_send(
                notification("notifications/cancelled", {"requestId": request_id, "reason": reason})
            )

    async def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        if self._failure is not None:
            raise MCPError(self._failure.code, self._failure.detail)
        await asyncio.wait_for(self.transport.send(notification(method, params)), self.timeout_s)

    # -- operations -------------------------------------------------------------------

    async def connect(self, *, timeout_s: float | None = None) -> ServerInfo:
        """Start the transport and run the initialize handshake."""
        await self.transport.start(self)
        self._started = True
        result = await self.request(
            "initialize",
            {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
            timeout_s=timeout_s,
        )
        version = result.get("protocolVersion")
        if not isinstance(version, str) or version not in SUPPORTED_VERSIONS:
            raise MCPError("unsupported_version", clip(str(version), 40))
        self.transport.negotiated(version)
        server = result.get("serverInfo")
        server = server if isinstance(server, dict) else {}
        capabilities = result.get("capabilities")
        instructions = result.get("instructions")
        self.info = ServerInfo(
            name=clip(str(server.get("name", "")), 80),
            version=clip(str(server.get("version", "")), 40),
            protocol_version=version,
            capabilities=capabilities if isinstance(capabilities, dict) else {},
            instructions=clip(instructions, 2000) if isinstance(instructions, str) else "",
        )
        await self.notify("notifications/initialized")
        return self.info

    async def ping(self, *, timeout_s: float | None = None) -> None:
        await self.request("ping", timeout_s=timeout_s)

    async def list_tools(self) -> list[ToolInfo]:
        """Every tool, following ``nextCursor`` (bounded pages and count)."""
        tools: list[ToolInfo] = []
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            try:
                result = await self.request("tools/list", {"cursor": cursor} if cursor else {})
            except RPCError as exc:
                if exc.rpc_code == METHOD_NOT_FOUND and not tools:
                    return []  # a server with no tools capability
                raise
            items = result.get("tools")
            for item in items if isinstance(items, list) else []:
                parsed = _tool_info(item)
                if parsed is not None:
                    tools.append(parsed)
            nxt = result.get("nextCursor")
            if not isinstance(nxt, str) or not nxt or nxt == cursor or len(tools) >= MAX_TOOLS:
                break
            cursor = nxt
        return tools[:MAX_TOOLS]

    async def call_tool(
        self, name: str, arguments: Mapping[str, Any], *, timeout_s: float | None = None
    ) -> CallResult:
        result = await self.request(
            "tools/call", {"name": name, "arguments": dict(arguments)}, timeout_s=timeout_s
        )
        text, truncated = content_text(result.get("content"), self.result_chars)
        structured = result.get("structuredContent")
        if not text and structured is not None:
            raw = json.dumps(structured, ensure_ascii=False)
            truncated = len(raw) > self.result_chars
            text = clip(raw, self.result_chars)
        return CallResult(text=text, is_error=result.get("isError") is True, truncated=truncated)

    async def close(self) -> None:
        """End the session: the transport stops (stdio: stdin closed, then the tree)."""
        if self._failure is None:
            self._failure = MCPError("closed", "client closed")
        self.error(MCPError("closed", "client closed"))
        for task in list(self._background):
            task.cancel()
        with contextlib.suppress(Exception):
            await self.transport.close()

    def kill_now(self) -> None:
        self._failure = MCPError("closed", "client closed")
        self.transport.kill_now()


def _tool_info(item: object) -> ToolInfo | None:
    if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]:
        return None
    schema = item.get("inputSchema")
    if not isinstance(schema, dict) or len(json.dumps(schema)) > 32_000:
        schema = {"type": "object"}
    description = item.get("description")
    title = item.get("title")
    annotations = item.get("annotations")
    hints = annotations if isinstance(annotations, dict) else {}
    return ToolInfo(
        name=clip(item["name"], 128),
        description=clip(description, DESCRIPTION_CHARS) if isinstance(description, str) else "",
        input_schema=schema,
        title=clip(title, 80) if isinstance(title, str) else "",
        read_only=hints.get("readOnlyHint") is True,
    )
