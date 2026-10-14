"""Model Context Protocol wire format: JSON-RPC 2.0 messages, versions, errors and content.

Only the client side of the spec is here (https://modelcontextprotocol.io/specification).
Failures carry a fixed ``code`` and a short ``detail``; server text is clipped before it
reaches the model or the terminal.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from architect import __version__

__all__ = [
    "CLIENT_INFO",
    "JSONRPC",
    "MAX_MESSAGE_BYTES",
    "PROTOCOL_VERSION",
    "SUPPORTED_VERSIONS",
    "MCPError",
    "RPCError",
    "clip",
    "content_text",
    "encode",
    "notification",
    "request",
    "response",
]

JSONRPC = "2.0"
# The newest version first: the client offers it and accepts any listed one back.
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
CLIENT_INFO: dict[str, str] = {"name": "architect", "version": __version__}
# One JSON-RPC message (a line on stdio, a body or SSE event over HTTP).
MAX_MESSAGE_BYTES = 8 * 1024 * 1024
DETAIL_CHARS = 300


class MCPError(Exception):
    """A client-side failure with a fixed code (``timeout``, ``server_exited``, ...)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = clip(detail, DETAIL_CHARS)


class RPCError(MCPError):
    """A JSON-RPC error object the server returned."""

    def __init__(self, rpc_code: int, message: str, data: object = None) -> None:
        super().__init__("rpc_error", f"{rpc_code}: {message}")
        self.rpc_code = rpc_code
        self.message = clip(message, DETAIL_CHARS)
        self.data = data


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 15)] + " ... [clipped]"


def request(
    request_id: int, method: str, params: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": JSONRPC, "id": request_id, "method": method}
    if params is not None:
        message["params"] = dict(params)
    return message


def notification(method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": JSONRPC, "method": method}
    if params is not None:
        message["params"] = dict(params)
    return message


def response(
    request_id: object,
    result: Mapping[str, Any] | None = None,
    *,
    error: tuple[int, str] | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": JSONRPC, "id": request_id}
    if error is not None:
        message["error"] = {"code": error[0], "message": error[1]}
    else:
        message["result"] = dict(result or {})
    return message


def encode(message: Mapping[str, Any]) -> bytes:
    """One message as compact UTF-8 JSON (never contains a raw newline)."""
    data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_MESSAGE_BYTES:
        raise MCPError("message_too_large", f"outgoing {len(data)} bytes")
    return data


def _resource_text(resource: Mapping[str, Any]) -> str:
    uri = str(resource.get("uri", ""))
    if isinstance(resource.get("text"), str):
        return f"[resource {uri}]\n{resource['text']}"
    mime = str(resource.get("mimeType", "application/octet-stream"))
    blob = resource.get("blob")
    size = (len(blob) * 3) // 4 if isinstance(blob, str) else 0
    return f"[binary resource {uri} ({mime}, about {size} bytes) not shown]"


def content_text(content: object, limit: int) -> tuple[str, bool]:
    """A ``tools/call`` content list as bounded text; (text, truncated).

    Text blocks are kept, images and audio become a one-line placeholder, embedded text
    resources are inlined with their URI, binary resources and resource links are named.
    """
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, Mapping):
                continue
            kind = block.get("type")
            if kind == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif kind in ("image", "audio"):
                mime = str(block.get("mimeType", kind))
                data = block.get("data")
                size = (len(data) * 3) // 4 if isinstance(data, str) else 0
                parts.append(f"[{kind} {mime}, about {size} bytes, not shown]")
            elif kind == "resource" and isinstance(block.get("resource"), Mapping):
                parts.append(_resource_text(block["resource"]))
            elif kind == "resource_link":
                name = str(block.get("name") or block.get("uri", ""))
                parts.append(f"[resource link {name}: {block.get('uri', '')}]")
            else:
                parts.append(f"[{str(kind)[:40]} content not shown]")
    text = "\n".join(parts)
    if len(text) <= limit:
        return text, False
    return clip(text, limit), True
