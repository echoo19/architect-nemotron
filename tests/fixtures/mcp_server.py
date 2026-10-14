"""A tiny MCP server over stdio for the offline tests (newline-delimited JSON-RPC).

Environment knobs: ``MCP_FAKE_LOG`` (append every received message as a JSON line),
``MCP_FAKE_VERSION`` (the protocolVersion to answer), ``MCP_FAKE_NOISE`` (print a non-JSON
line first), ``MCP_FAKE_STDERR`` (write this to stderr at start), ``MCP_FAKE_NAME``.
"""

from __future__ import annotations

import json
import os
import sys
import threading

LOCK = threading.Lock()
LOG = os.environ.get("MCP_FAKE_LOG")

TOOLS = [
    {"name": "echo", "description": "Echo the text back.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                     "required": ["text"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "media", "description": "Text, an image and a resource.",
     "inputSchema": {"type": "object"}},
    {"name": "big", "description": "A long text.",
     "inputSchema": {"type": "object", "properties": {"n": {"type": "integer"}}}},
    {"name": "fail", "description": "Always an error result.", "inputSchema": {"type": "object"}},
    {"name": "slow", "description": "Answers after a delay.",
     "inputSchema": {"type": "object", "properties": {"seconds": {"type": "number"}}}},
    {"name": "crash", "description": "Exits the server.", "inputSchema": {"type": "object"}},
    {"name": "Weird Name.v2!", "description": "A name that needs sanitizing."},
]


def send(message: dict[str, object]) -> None:
    with LOCK:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def reply(request_id: object, result: dict[str, object]) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


def text(value: str, *, error: bool = False) -> dict[str, object]:
    return {"content": [{"type": "text", "text": value}], "isError": error}


def call(request_id: object, name: str, args: dict[str, object]) -> None:
    if name == "echo":
        reply(request_id, text(str(args.get("text", ""))))
    elif name == "media":
        reply(request_id, {"content": [
            {"type": "text", "text": "caption"},
            {"type": "image", "mimeType": "image/png", "data": "aGVsbG8gd29ybGQh"},
            {"type": "resource", "resource": {"uri": "file:///notes.txt", "text": "note body"}},
            {"type": "resource", "resource": {"uri": "file:///x.bin", "blob": "AAAA",
                                              "mimeType": "application/zip"}},
        ]})
    elif name == "big":
        n = args.get("n", 50_000)
        reply(request_id, text("x" * int(n) if isinstance(n, int) else "x"))
    elif name == "fail":
        reply(request_id, text("the tool failed on purpose", error=True))
    elif name == "slow":
        seconds = args.get("seconds", 5)
        delay = float(seconds) if isinstance(seconds, int | float) else 5.0
        threading.Timer(delay, reply, (request_id, text("slow done"))).start()
    elif name == "crash":
        sys.stdout.flush()
        os._exit(3)
    elif name == "Weird Name.v2!":
        reply(request_id, text("weird ok"))
    else:
        send({"jsonrpc": "2.0", "id": request_id,
              "error": {"code": -32602, "message": f"unknown tool {name}"}})


def handle(message: dict[str, object]) -> None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params")
    params = params if isinstance(params, dict) else {}
    if "id" not in message:
        return  # a notification
    if method == "initialize":
        reply(request_id, {
            "protocolVersion": os.environ.get("MCP_FAKE_VERSION", params.get("protocolVersion")),
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": os.environ.get("MCP_FAKE_NAME", "fake"), "version": "1.0"},
            "instructions": "Use echo.",
        })
    elif method == "ping":
        reply(request_id, {})
    elif method == "tools/list":
        page = 1 if params.get("cursor") == "page-2" else 0
        half = 3
        tools = TOOLS[:half] if page == 0 else TOOLS[half:]
        result: dict[str, object] = {"tools": tools}
        if page == 0:
            result["nextCursor"] = "page-2"
        reply(request_id, result)
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments")
        call(request_id, str(name), args if isinstance(args, dict) else {})
    else:
        send({"jsonrpc": "2.0", "id": request_id,
              "error": {"code": -32601, "message": "method not found"}})


def main() -> None:
    if os.environ.get("MCP_FAKE_STDERR"):
        sys.stderr.write(os.environ["MCP_FAKE_STDERR"] + "\n")
        sys.stderr.flush()
    if os.environ.get("MCP_FAKE_NOISE"):
        sys.stdout.write("starting up, not json\n")
        sys.stdout.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        if LOG:
            with open(LOG, "a", encoding="utf-8") as handle_:
                handle_.write(json.dumps(message) + "\n")
        handle(message)


if __name__ == "__main__":
    main()
