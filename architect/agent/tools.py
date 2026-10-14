"""The conductor's tools (Phase 12, Section 3.1).

The conductor never writes a file. Its own tools only read the working directory and the
registry; file changes happen only inside the coder subagent (Pi), under the session's
permission mode. Every model call a tool makes is metered by the runtime and counted
against the session budget.
"""

from __future__ import annotations

import ast
import asyncio
import fnmatch
import ipaddress
import json
import os
import re
import socket
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from jsonschema import Draft202012Validator, ValidationError
from pydantic import JsonValue

from architect.models.relay import RelayRefusal, Unreachable
from architect.presets import PRESET_DOCSTRING, PresetError, get_preset
from architect.runtime import BudgetExceeded as BudgetExceededError
from architect.spec import Budget, FloorDefinition, FloorKind, HarnessArtifact, Spec
from architect.trust import private_key_path

from .events import UI, Notice, SubagentActivity

if TYPE_CHECKING:
    from architect.profiles import Services

__all__ = [
    "PRESET_OUTPUTS",
    "SPAWNABLE",
    "Permission",
    "Tool",
    "ToolContext",
    "ToolError",
    "default_tools",
    "html_to_text",
]

Permission = Literal["ask", "auto-edit", "full-auto", "plan"]
SPAWNABLE = (
    "extractor",
    "synthesizer",
    "planner",
    "verifier",
    "explorer",
    "recommender",
    "hypothesis_tester",
)
# The output schema an ephemeral preset subagent answers in when the caller gives none.
PRESET_OUTPUTS: dict[str, dict[str, JsonValue]] = {
    "extractor": {"type": "object"},
    "synthesizer": {"type": "string"},
    "planner": {"type": "object"},
    "verifier": {"type": "object"},
    "explorer": {"type": "object"},
    "recommender": {"type": "object"},
    "hypothesis_tester": {"type": "object"},
}
SKIP_DIRS = frozenset(
    {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
     ".pytest_cache", ".ruff_cache", ".architect", "dist", "build", ".next"}
)
READ_LIMIT_BYTES = 256 * 1024
READ_LINES = 400
LIST_LIMIT = 200
GREP_MATCHES = 100
GREP_FILE_BYTES = 1024 * 1024
WEB_FETCH_CHARS = 20_000
WEB_FETCH_MAX_CHARS = 100_000
WEB_FETCH_BYTES = 2 * 1024 * 1024
WEB_FETCH_TIMEOUT_S = 20.0
WEB_FETCH_REDIRECTS = 5
PLAN_MODE = (
    "Permission mode is plan (read-only): no files are changed and the coder is not "
    "called. Propose the change instead: list the files and the edits you would make and "
    "why, so the user can review the plan and switch modes (/permissions or Shift+Tab) to "
    "carry it out."
)


class ToolError(Exception):
    """A tool failure with a fixed code the model can read and recover from."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass
class ToolContext:
    services: Services
    cwd: Path
    ui: UI
    model: str
    permission: Permission = "ask"
    # Actions the user allowed for the rest of the session ("s" at an approval prompt).
    allowed: set[str] = field(default_factory=set)
    per_call_usd: float = 1.0
    per_call_s: float = 600.0
    extras: dict[str, Any] = field(default_factory=dict)
    # The session's remaining dollars (set by the conductor); None means uncapped.
    budget_left: Callable[[], float] | None = None

    def cap(self, usd: float) -> float:
        """``usd`` capped by what is left of the session budget; none left is a ToolError."""
        left = self.budget_left() if self.budget_left is not None else usd
        amount = min(usd, left)
        if amount <= 0:
            raise ToolError("session_budget_exhausted")
        return amount

    def resolve(self, value: object, *, must_exist: bool = True) -> Path:
        """A path inside the working directory; anything else is ``path_outside_cwd``."""
        if not isinstance(value, str) or not value.strip():
            raise ToolError("invalid_path")
        root = self.cwd.resolve()
        path = (root / value).resolve()
        if path != root and root not in path.parents:
            raise ToolError("path_outside_cwd", value)
        if must_exist and not path.exists():
            raise ToolError("not_found", value)
        return path

    def relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.cwd.resolve()).as_posix() or "."
        except ValueError:
            return path.as_posix()


ToolRun = Callable[[dict[str, JsonValue], ToolContext], Awaitable[JsonValue]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, JsonValue]
    run: ToolRun
    spends: bool = False

    def listing(self) -> dict[str, JsonValue]:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


def _schema(properties: dict[str, JsonValue], required: Sequence[str] = ()) -> dict[str, JsonValue]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _str(args: dict[str, JsonValue], key: str, default: str | None = None) -> str:
    value = args.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ToolError("invalid_arguments", key)
    return value


def _int(args: dict[str, JsonValue], key: str, default: int) -> int:
    value = args.get(key, default)
    if type(value) is not int or value < 0:
        raise ToolError("invalid_arguments", key)
    return value


# -- read-only tools ------------------------------------------------------------

# Files the read-only tools never show, even when the working directory contains them
# (the workspace's API keys and price overrides, the signing keys, CLI logins).
WORKSPACE_SECRETS = ("credentials.json", "model-prices.json", "signing.key", "ed25519.key")


def secret_paths(ctx: ToolContext) -> frozenset[Path]:
    """Resolved paths of secret files: the workspace's, the signing key, CLI logins."""
    roots: list[Path] = []
    for value in (ctx.extras.get("workspace"), getattr(ctx.services, "workspace", None)):
        if isinstance(value, str | Path):
            roots.append(Path(value))
    candidates = [root / name for root in roots for name in WORKSPACE_SECRETS]
    signing = os.environ.get("ARCHITECT_SIGNING_KEY_FILE")
    if signing:
        candidates.append(Path(signing))
    candidates.append(private_key_path())  # the Ed25519 key, wherever it is configured
    home = Path.home()
    candidates += [home / ".claude" / ".credentials.json", home / ".codex" / "auth.json"]
    resolved: set[Path] = set()
    for path in candidates:
        try:
            resolved.add(path.resolve())
        except (OSError, RuntimeError):
            continue
    return frozenset(resolved)


def _is_secret(path: Path, hidden: frozenset[Path]) -> bool:
    """Whether resolved ``path`` names one of ``hidden``: the same path compared without
    case (APFS and NTFS, the macOS and Windows defaults, open ``.architect/ED25519.KEY`` as
    ``ed25519.key``), or the same file on disk (a short-name alias, a hard link)."""
    folded = os.fspath(path).casefold()
    for secret in hidden:
        if os.fspath(secret).casefold() == folded:
            return True
        try:
            if os.path.samefile(path, secret):
                return True
        except OSError:
            continue
    return False


def _visible(paths: list[Path], hidden: frozenset[Path]) -> list[Path]:
    if not hidden:
        return paths
    names = {path.name.casefold() for path in hidden}
    shown = []
    for path in paths:
        try:
            if path.name.casefold() in names and _is_secret(path.resolve(), hidden):
                continue
        except (OSError, RuntimeError):
            continue
        shown.append(path)
    return shown


async def _read_file(args: dict[str, JsonValue], ctx: ToolContext) -> JsonValue:
    path = ctx.resolve(args.get("path"))
    if _is_secret(path, secret_paths(ctx)):
        raise ToolError("denied_path", "secret files (keys, credentials) are never read")
    if not path.is_file():
        raise ToolError("not_a_file", ctx.relative(path))
    if path.stat().st_size > READ_LIMIT_BYTES * 8:
        raise ToolError("file_too_large", ctx.relative(path))
    data = path.read_bytes()
    if b"\0" in data[:4096]:
        raise ToolError("binary_file", ctx.relative(path))
    lines = data.decode("utf-8", errors="replace").splitlines()
    offset = _int(args, "offset", 0)
    limit = min(_int(args, "limit", READ_LINES) or READ_LINES, 2000)
    chunk = lines[offset : offset + limit]
    width = len(str(offset + len(chunk)))
    body = "\n".join(f"{offset + i + 1:>{width}}  {line}" for i, line in enumerate(chunk))
    return {
        "path": ctx.relative(path),
        "lines": len(lines),
        "shown": [offset + 1, offset + len(chunk)],
        "content": body[:READ_LIMIT_BYTES],
        "truncated": offset + len(chunk) < len(lines),
    }


def _walk(root: Path) -> list[Path]:
    found: list[Path] = []
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            found.append(Path(directory) / name)
            if len(found) >= 20_000:
                return found
    return found


async def _list_dir(args: dict[str, JsonValue], ctx: ToolContext) -> JsonValue:
    path = ctx.resolve(args.get("path", ".") or ".")
    if not path.is_dir():
        raise ToolError("not_a_directory", ctx.relative(path))
    entries = []
    hidden = secret_paths(ctx)
    children = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    for child in _visible(children, hidden):
        if child.name in SKIP_DIRS:
            continue
        kind = "dir" if child.is_dir() else "file"
        size = child.stat().st_size if kind == "file" else None
        entries.append({"name": child.name, "type": kind, "bytes": size})
        if len(entries) >= LIST_LIMIT:
            break
    return {"path": ctx.relative(path), "entries": entries}  # type: ignore[dict-item]


async def _glob(args: dict[str, JsonValue], ctx: ToolContext) -> JsonValue:
    pattern = _str(args, "pattern")
    root = ctx.cwd.resolve()
    matches = [
        ctx.relative(path)
        for path in _visible(_walk(root), secret_paths(ctx))
        if fnmatch.fnmatch(ctx.relative(path), pattern) or fnmatch.fnmatch(path.name, pattern)
    ]
    return {"pattern": pattern, "matches": matches[:LIST_LIMIT], "total": len(matches)}  # type: ignore[dict-item]


async def _grep(args: dict[str, JsonValue], ctx: ToolContext) -> JsonValue:
    pattern = _str(args, "pattern")
    try:
        regex = re.compile(pattern)
    except re.error:
        raise ToolError("invalid_regex") from None
    base = ctx.resolve(args.get("path", ".") or ".")
    files = _visible([base] if base.is_file() else _walk(base), secret_paths(ctx))
    include = args.get("glob")
    hits: list[str] = []
    for path in files:
        if isinstance(include, str) and include and not fnmatch.fnmatch(path.name, include):
            continue
        try:
            if path.stat().st_size > GREP_FILE_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:4096]:
            continue
        for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            if regex.search(line):
                hits.append(f"{ctx.relative(path)}:{number}: {line.strip()[:200]}")
                if len(hits) >= GREP_MATCHES:
                    return {"pattern": pattern, "matches": hits, "truncated": True}  # type: ignore[dict-item]
    return {"pattern": pattern, "matches": hits, "truncated": False}  # type: ignore[dict-item]


# -- registry tools -------------------------------------------------------------


def _licensed(directory: Path) -> bool:
    try:
        return HarnessArtifact.load(directory).licensed
    except (OSError, ValueError, TypeError):
        return False


async def _agents(args: dict[str, JsonValue], ctx: ToolContext) -> JsonValue:
    rows = []
    for entry in ctx.services.registry.list():
        try:
            artifact = HarnessArtifact.load(entry.directory)
        except (OSError, ValueError, TypeError):
            continue
        report = artifact.report
        rows.append(
            {
                "name": entry.name,
                "revision": entry.revision,
                "task": artifact.spec.task[:160],
                "preset": artifact.preset,
                "model": artifact.model,
                "licensed": artifact.licensed,
                "floor": None
                if report is None
                else f"{report.floor_score:.2f}/{report.floor_limit:.2f}",
            }
        )
    return {"agents": rows}  # type: ignore[dict-item]


async def _report(args: dict[str, JsonValue], ctx: ToolContext) -> JsonValue:
    name = _str(args, "name")
    try:
        artifact = ctx.services.registry.get(name)
    except Exception:
        raise ToolError("unknown_agent", name) from None
    report = artifact.report
    spec = artifact.spec
    return {
        "name": name,
        "task": spec.task,
        "preset": artifact.preset,
        "model": artifact.model,
        "licensed": artifact.licensed,
        "input_schema": spec.input_schema,
        "output_schema": spec.output_schema,
        "budget": {"usd_per_run": spec.budget.cost_usd, "seconds_per_run": spec.budget.compute_s},
        # The verification id stays with verify(): like the two signatures (the HMAC and
        # the W7 Ed25519 seal), it never leaves in a tool result (the model's context, the
        # CLI's rows or the desktop's replay log).
        "report": None if report is None else report.model_dump(
            mode="json", exclude={"signature", "ed25519", "verification_id"}
        ),
        "design": harness_design(artifact.directory),
    }


def harness_design(directory: Path) -> str | None:
    """The design a Forge-built harness states in its docstring (Forge's DESIGN.md asks it to).

    A preset harness carries the template's docstring, which describes no design (and,
    shown as one, would read as if a licensed agent had no license): None for it."""
    path = directory / "harness.py"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return None
    text = ast.get_docstring(tree)
    if text is None or text.strip() == PRESET_DOCSTRING:
        return None
    return text[:1200] if text else None


async def _run_agent(args: dict[str, JsonValue], ctx: ToolContext) -> JsonValue:
    name = _str(args, "name")
    task = args.get("input")
    try:
        entry = ctx.services.registry.status(name)
        artifact = HarnessArtifact.load(entry.directory)  # the license is re-read from disk
    except Exception:
        raise ToolError("unknown_agent", name) from None
    if not artifact.licensed:
        raise ToolError("unlicensed_agent", name)
    task = shape_input(task, artifact.spec.input_schema)
    try:
        Draft202012Validator(artifact.spec.input_schema).validate(task)
    except ValidationError as exc:
        where = "/".join(str(part) for part in exc.absolute_path) or "input"
        raise ToolError("invalid_input", f"{where}: {exc.validator} check failed") from None
    except Exception:
        pass  # a schema the validator cannot use: the runtime still validates
    ctx.ui.emit(SubagentActivity(name, f"running v{entry.revision} on {artifact.model}"))
    adapter = ctx.services.adapter_factory(artifact.model)
    try:
        result = await ctx.services.runtime.run(artifact, task, adapter)
    except (ToolError, BudgetExceededError, RelayRefusal, Unreachable):
        raise  # a relay refusal or an unreachable address has its own sentence (call_tool)
    except Exception as exc:
        # Harness failures carry fixed, content-free codes (sandbox_*, preset messages).
        raise ToolError("agent_run_failed", f"{type(exc).__name__}: {str(exc)[:160]}") from None
    return {
        "agent": name,
        "output": result.output,
        "cost_usd": result.cost_usd,
        "seconds": round(result.wall_time_s, 3),
        "calls": result.model_calls,
        "run_id": result.run_id,
        # The provider's id of the run's last completion, when it gives one.
        "completion_id": result.completion_ids[-1] if result.completion_ids else None,
    }


def shape_input(task: JsonValue, schema: dict[str, JsonValue]) -> JsonValue:
    """Plain text for an agent whose input is one required string field becomes that field."""
    if not isinstance(task, str):
        return task
    properties = schema.get("properties")
    required = schema.get("required")
    if (
        schema.get("type") == "object"
        and isinstance(properties, dict)
        and isinstance(required, list)
        and len(required) == 1
        and isinstance(required[0], str)
    ):
        field_schema = properties.get(required[0])
        if isinstance(field_schema, dict) and field_schema.get("type") == "string":
            return {required[0]: task}
    return task


# -- ephemeral subagents --------------------------------------------------------


def control_spec(
    task: str,
    model: str,
    *,
    output_schema: dict[str, JsonValue] | None = None,
    cost_usd: float = 1.0,
    compute_s: float = 600.0,
    tools: tuple[str, ...] = (),
) -> Spec:
    """A Spec for one unlicensed, metered run; its floor is never evaluated."""
    return Spec(
        task=task,
        input_schema={},
        output_schema=output_schema if output_schema is not None else {"type": "object"},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="agent-control"),
        budget=Budget(cost_usd=cost_usd, compute_s=compute_s),
        model_target=model,
        tools=tools,
    )


async def _spawn(args: dict[str, JsonValue], ctx: ToolContext) -> JsonValue:
    preset = _str(args, "preset")
    if preset not in SPAWNABLE:
        raise ToolError("unknown_preset", preset)
    task = _str(args, "task")
    schema = args.get("output_schema")
    output_schema = schema if isinstance(schema, dict) else PRESET_OUTPUTS[preset]
    spec = control_spec(
        task, ctx.model, output_schema=output_schema, cost_usd=ctx.cap(ctx.per_call_usd),
        compute_s=ctx.per_call_s,
    )
    harness = get_preset(preset).build(spec)
    ctx.ui.emit(SubagentActivity(preset, task[:120]))
    adapter = ctx.services.adapter_factory(ctx.model)
    try:
        result = await ctx.services.runtime.evaluate(
            spec, harness, args.get("input", {}), adapter, preset=preset
        )
    except PresetError as exc:  # fixed, content-free messages: safe to show the model
        raise ToolError("subagent_failed", str(exc)) from None
    return {"preset": preset, "output": result.output, "licensed": False,
            "cost_usd": result.cost_usd}


async def _code(args: dict[str, JsonValue], ctx: ToolContext) -> JsonValue:
    request = _str(args, "task")
    if ctx.permission == "plan":
        raise ToolError("plan_mode_read_only", PLAN_MODE)
    coder: Callable[[str, ToolContext], Awaitable[JsonValue]] | None = ctx.extras.get("coder")
    if coder is None:
        raise ToolError("coder_unavailable")
    return await coder(request, ctx)


# -- the web ----------------------------------------------------------------------


class _HTMLText(HTMLParser):
    """HTML to readable text with the standard library: blocks become lines."""

    SKIP = frozenset({"script", "style", "noscript", "svg", "template", "iframe", "object",
                      "canvas", "head"})
    BLOCK = frozenset({"p", "div", "br", "li", "tr", "section", "article", "header",
                       "footer", "main", "nav", "aside", "pre", "blockquote", "table", "ul",
                       "ol", "hr", "dd", "dt", "dl", "form", "figure", "figcaption", "h1",
                       "h2", "h3", "h4", "h5", "h6"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._pre = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
            return
        if tag in self.SKIP:
            self._skip += 1
            return
        if tag == "pre":
            self._pre += 1
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")
        elif len(tag) == 2 and tag[0] == "h" and tag[1] in "123456":
            self.parts.append("#" * int(tag[1]) + " ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            return
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if tag == "pre":
            self._pre = max(0, self._pre - 1)
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("br", "hr"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skip:
            return
        self.parts.append(data if self._pre else re.sub(r"\s+", " ", data))


def html_to_text(html: str) -> tuple[str, str]:
    """``(title, text)`` of an HTML page: scripts and styles dropped, blocks on lines."""
    parser = _HTMLText()
    try:
        parser.feed(html)
        parser.close()
    except (AssertionError, ValueError):  # malformed markup: keep what was parsed
        pass
    lines = [line.strip() for line in "".join(parser.parts).splitlines()]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return re.sub(r"\s+", " ", parser.title).strip(), text.strip()


def _blocked_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified)


async def _resolve(host: str) -> list[str]:
    """Every address ``host`` resolves to (a thread: getaddrinfo blocks)."""
    infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    return sorted({str(info[4][0]) for info in infos})


async def _check_url(url: Any, allow_private: bool) -> None:
    if url.scheme not in ("http", "https"):
        raise ToolError("unsupported_scheme", "only http and https URLs are fetched")
    host = url.host
    if not host:
        raise ToolError("invalid_url")
    if allow_private:
        return
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            addresses = await _resolve(host)
        except (OSError, UnicodeError):
            raise ToolError("host_not_found", host) from None
    if not addresses or any(_blocked_address(address) for address in addresses):
        raise ToolError("private_address", f"{host} is a local or private address; allow it "
                        "with /config set web_fetch_allow_private true")


_TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml",
               "application/javascript", "application/ld+json", "application/rss",
               "application/atom")


async def _web_fetch(args: dict[str, JsonValue], ctx: ToolContext) -> JsonValue:
    import httpx

    from architect import __version__

    raw = _str(args, "url").strip()
    max_chars = _int(args, "max_chars", WEB_FETCH_CHARS) or WEB_FETCH_CHARS
    max_chars = max(500, min(max_chars, WEB_FETCH_MAX_CHARS))
    allow_private = ctx.extras.get("web_fetch_allow_private") is True
    try:
        url = httpx.URL(raw)
    except (httpx.InvalidURL, ValueError, TypeError):
        raise ToolError("invalid_url", raw[:200]) from None
    await _check_url(url, allow_private)

    async def check_hop(request: Any) -> None:  # every redirect is checked again
        await _check_url(request.url, allow_private)

    transport = ctx.extras.get("web_transport")  # tests inject httpx.MockTransport
    body = bytearray()
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            max_redirects=WEB_FETCH_REDIRECTS,
            timeout=WEB_FETCH_TIMEOUT_S,
            transport=transport,
            event_hooks={"request": [check_hop]},
            headers={"User-Agent": f"Architect/{__version__} (web_fetch)",
                     "Accept": "text/html, text/plain, application/json, */*;q=0.5"},
        ) as client:
            async with client.stream("GET", url) as response:
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) >= WEB_FETCH_BYTES:
                        break
                status = response.status_code
                final = str(response.url)
                content_type = response.headers.get("content-type", "")
                encoding = response.charset_encoding or "utf-8"
    except ToolError:
        raise
    except httpx.TooManyRedirects:
        raise ToolError("too_many_redirects") from None
    except httpx.TimeoutException:
        raise ToolError("fetch_timeout") from None
    except httpx.HTTPError as exc:
        raise ToolError("fetch_failed", type(exc).__name__) from None
    kind = content_type.split(";", 1)[0].strip().lower()
    if kind and not kind.startswith(_TEXT_TYPES) and not kind.endswith(("+json", "+xml")):
        raise ToolError("unsupported_content_type", kind)
    try:
        decoded = bytes(body).decode(encoding, errors="replace")
    except LookupError:
        decoded = bytes(body).decode("utf-8", errors="replace")
    title = ""
    if "html" in kind or (not kind and "<html" in decoded[:2000].lower()):
        title, text = html_to_text(decoded)
    else:
        text = decoded
    total = len(text)
    result: dict[str, JsonValue] = {
        "url": final,
        "status": status,
        "content_type": kind,
        "title": title,
        "text": text[:max_chars],
        "chars": total,
        "truncated": total > max_chars or len(body) >= WEB_FETCH_BYTES,
    }
    if status >= 400:
        result["error"] = f"http_{status}"
    return result


# -- build and forge (bound by the build package) ---------------------------------


async def _make(args: dict[str, JsonValue], ctx: ToolContext) -> JsonValue:
    handler = ctx.extras.get("make")
    if handler is None:
        raise ToolError("build_mode_unavailable")
    if "spec_file" in args:
        # Phase 14: a frozen build compiles the Spec in that file exactly as written.
        spec_file = _str(args, "spec_file").strip()
        description = args.get("description")
        text = description if isinstance(description, str) else ""
        return await handler(text, ctx, spec_file=spec_file)  # type: ignore[no-any-return]
    return await handler(_str(args, "description"), ctx)  # type: ignore[no-any-return]


async def _forge(args: dict[str, JsonValue], ctx: ToolContext) -> JsonValue:
    handler = ctx.extras.get("forge")
    if handler is None:
        raise ToolError("forge_unavailable")
    return await handler(_str(args, "target"), ctx)  # type: ignore[no-any-return]


def default_tools() -> tuple[Tool, ...]:
    path: dict[str, JsonValue] = {
        "type": "string",
        "description": "A path relative to the working directory.",
    }
    return (
        Tool("read_file", "Read a text file in the working directory (numbered lines).",
             _schema({"path": path, "offset": {"type": "integer"}, "limit": {"type": "integer"}},
                     ["path"]), _read_file),
        Tool("list_dir", "List a directory in the working directory.",
             _schema({"path": path}), _list_dir),
        Tool("glob", "Find files by glob pattern, e.g. '**/*.py' or '*.md'.",
             _schema({"pattern": {"type": "string"}}, ["pattern"]), _glob),
        Tool("grep", "Search file contents with a regular expression.",
             _schema({"pattern": {"type": "string"}, "path": path, "glob": {"type": "string"}},
                     ["pattern"]), _grep),
        Tool("code", "Hand a code or file change to the coder subagent, which edits files in the "
             "working directory with the user's approval. Describe the change completely.",
             _schema({"task": {"type": "string"}}, ["task"]), _code, spends=True),
        Tool("spawn", "Run one ephemeral preset subagent now (unlicensed, metered) for a "
             "structured job: " + ", ".join(SPAWNABLE) + ". Give the full task and any input.",
             _schema({"preset": {"type": "string", "enum": list(SPAWNABLE)},
                      "task": {"type": "string"}, "input": {},
                      "output_schema": {"type": "object"}}, ["preset", "task"]),
             _spawn, spends=True),
        Tool("make", "Build mode: interview the user briefly, infer the spec, floor and budget, "
             "compile, test and install a licensed agent or harness. Use for any request to "
             "make, build or create an agent, harness or agent pipeline.",
             _schema({"description": {"type": "string"},
                      "spec_file": {"type": "string",
                                    "description": "A Spec JSON file to compile exactly as "
                                    "written, with no interview or inference."}},
                     ["description"]), _make, spends=True),
        Tool("forge", "Forge a harness for an environment or competition, e.g. 'arc'.",
             _schema({"target": {"type": "string"}}, ["target"]), _forge, spends=True),
        Tool("web_fetch", "Fetch a public http(s) URL and return its readable text "
             "(HTML is simplified; bounded). Use for documentation and pages the user names.",
             _schema({"url": {"type": "string"},
                      "max_chars": {"type": "integer", "minimum": 500,
                                    "maximum": WEB_FETCH_MAX_CHARS}}, ["url"]), _web_fetch),
        Tool("agents", "List installed agents with license and floor.", _schema({}), _agents),
        Tool("report", "An installed agent's spec summary and test report.",
             _schema({"name": {"type": "string"}}, ["name"]), _report),
        Tool("run_agent", "Run an installed, licensed agent on an input.",
             _schema({"name": {"type": "string"}, "input": {}}, ["name"]), _run_agent,
             spends=True),
    )


def tool_summary(name: str, args: dict[str, JsonValue]) -> str:
    """A one-line label for a tool call: ``read_file(src/app.py)``."""
    keys = ("path", "url", "pattern", "preset", "name", "target", "task", "description")
    parts = [str(args[k]) for k in keys if isinstance(args.get(k), str | int)]
    text = ", ".join(parts)[:100]
    return f"{name}({text})"


def result_summary(result: JsonValue) -> str:
    """A one-line result: counts for listings, the head of text otherwise."""
    if isinstance(result, dict):
        for key in ("matches", "entries", "agents"):
            value = result.get(key)
            if isinstance(value, list):
                return f"{len(value)} {key}"
        if "url" in result and "text" in result:
            label = str(result.get("title") or result["url"])[:80]
            return f"{label} ({result.get('chars', 0)} chars, HTTP {result.get('status')})"
        if "lines" in result and "path" in result:
            return f"{result['path']} ({result['lines']} lines)"
        if "output" in result:
            text = json.dumps(result["output"], ensure_ascii=False)
            return text[:100] + ("..." if len(text) > 100 else "")
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    return text[:100] + ("..." if len(text) > 100 else "")


def notice(ctx: ToolContext, text: str) -> None:
    ctx.ui.emit(Notice(text))
