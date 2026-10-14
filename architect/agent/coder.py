"""The coder subagent: Pi in the working directory, approved per permission mode.

Phase 12, Sections 4, 7 and 8. The app binds ``run_coder`` as ``ctx.extras["coder"]``;
the conductor's ``code`` tool calls it. The coder is the Pi backend (read, write, edit and,
unless disabled, bash) confined to the working directory, with the host's control-plane
paths denied (the profile's effective Pi config; Pi itself always denies the Architect
package and the interpreter prefix). Every guard in ``PiBackend`` runs first; the approver
here can only narrow what they allow.

Permission modes (Section 7):

- ``ask``: every write, edit and shell command is shown and approved.
- ``auto-edit``: writes and edits inside the working directory are allowed; shell asks.
- ``full-auto``: writes and edits are allowed; shell is allowed without asking only when
  the operator granted it in the profile (``pi.allow_shell``), otherwise it still asks.

An approval answer ``session`` adds the tool name (``write``, ``edit`` or ``bash``) to
``ctx.allowed`` so that kind is not asked again this session. Set
``ctx.extras["coder_shell"] = False`` to withhold the shell tool entirely.

Budget: one coder run is metered by ``Runtime.evaluate`` under its own per-run budget of
``max(ctx.per_call_usd, CODER_USD)`` dollars and ``max(ctx.per_call_s, CODER_S)`` seconds
(coding takes more calls and time than one conductor tool call; waiting for an approval
counts against the seconds). Money consent is separate from these file permissions.

Model calls: a model that streams native chats (``streaming_adapter``) drives Pi with
native tool calls, and the session meter still sees every call; any other model uses the
JSON envelope.

Notes: the reply's ``notes`` are facts the host checked itself after the run, whatever
the coder's own summary says: that no file changed, that a changed ``.json`` file no
longer parses, and that a changed file belongs to an agent Architect built, which is now
stopped until it passes its exam again or the change is undone.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import JsonValue

from architect.backends.pi import APPROVED_TOOLS, PiBackend, PiConfig, PiError
from architect.presets import PresetError, get_preset
from architect.profiles import effective_pi_config
from architect.runtime import BudgetExceeded

from .events import SubagentActivity
from .native import streaming_adapter
from .tools import SKIP_DIRS, ToolContext, control_spec

if TYPE_CHECKING:
    from architect.telemetry import TelemetryRow

__all__ = ["CODER_S", "CODER_USD", "coder_config", "run_coder", "shell_enabled"]

CODER_USD = 2.0
CODER_S = 900.0
SUMMARY_CHARS = 4000
DETAIL_CHARS = 1500
PREVIEW_LINES = 12
CHANGED_LIMIT = 200
NOTE_LIMIT = 12
JSON_CHECK_BYTES = 4 * 1_048_576
SNAPSHOT_FILES = 5000
FILE_TOOLS = frozenset({"write", "edit"})
DENIAL = "Tool denied by Architect policy"
CODER_TASK = (
    "Make the requested change in the repository (the working directory). Paths are "
    "relative to it. Verify the result, then reply with a short summary of what changed."
)


def shell_enabled(ctx: ToolContext) -> bool:
    """Whether the coder is offered ``bash`` at all (every command is still approved)."""
    return ctx.extras.get("coder_shell", True) is not False


def coder_config(ctx: ToolContext) -> PiConfig:
    """The profile's effective Pi config with the coder's tools and shell grant."""
    services = ctx.services
    base = effective_pi_config(services.profile, services.workspace)
    shell = shell_enabled(ctx)
    tools = ("read", "write", "edit", "bash") if shell else ("read", "write", "edit")
    return PiConfig.model_validate(
        {**base.model_dump(), "allowed_tools": tools, "allow_shell": shell}
    )


def _auto_allowed(ctx: ToolContext, tool: str, operator_shell: bool) -> bool:
    """Section 7 without asking: never in ``ask`` (or any unknown mode)."""
    if ctx.permission == "auto-edit":
        return tool in FILE_TOOLS
    if ctx.permission == "full-auto":
        return tool in FILE_TOOLS or (tool == "bash" and operator_shell)
    return False


def _printable(text: str) -> str:
    """Model text for a terminal: no escape sequences or other controls, keep newlines."""
    return "".join(
        char if char in "\n\t" or (char.isprintable() and ord(char) not in range(0x80, 0xA0))
        else " "
        for char in text
    )


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def approval_detail(tool: str, arguments: dict[str, JsonValue]) -> str:
    """What the user approves: a one-line summary, then a short bounded preview."""
    if tool == "bash":
        return _clip(_printable(str(arguments.get("command", ""))), DETAIL_CHARS)
    path = str(arguments.get("path", ""))
    lines: list[str] = []
    if tool == "write":
        content = arguments.get("content")
        text = content if isinstance(content, str) else ""
        body = text.splitlines()
        lines.append(f"{path} ({len(text.encode('utf-8'))} bytes, {len(body)} lines)")
        lines.extend("+ " + line for line in body[:PREVIEW_LINES])
        if len(body) > PREVIEW_LINES:
            lines.append(f"+ ... {len(body) - PREVIEW_LINES} more lines")
    else:
        edits = arguments.get("edits")
        if not isinstance(edits, list):
            edits = [{"oldText": arguments.get("oldText"), "newText": arguments.get("newText")}]
        lines.append(f"{path} ({len(edits)} edit{'s' if len(edits) != 1 else ''})")
        for item in edits[:3]:
            if not isinstance(item, dict):
                continue
            old, new = item.get("oldText"), item.get("newText")
            lines.extend("- " + line for line in str(old or "").splitlines()[:PREVIEW_LINES])
            lines.extend("+ " + line for line in str(new or "").splitlines()[:PREVIEW_LINES])
    return _clip(_printable("\n".join(lines)), DETAIL_CHARS)


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """``relative path -> (mtime_ns, size)`` for up to ``SNAPSHOT_FILES`` files."""
    found: dict[str, tuple[int, int]] = {}
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            path = Path(directory) / name
            try:
                stat = path.lstat()
            except OSError:
                continue
            found[path.relative_to(root).as_posix()] = (stat.st_mtime_ns, stat.st_size)
            if len(found) >= SNAPSHOT_FILES:
                return found
    return found


def _changed(before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]) -> set[str]:
    return {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}


@dataclass
class _Run:
    """What one coder run saw: events, approvals, and edited paths."""

    ctx: ToolContext
    operator_shell: bool
    tool_calls: int = 0
    asked: int = 0
    user_denied: int = 0
    policy_denied: int = 0
    edited: set[str] = field(default_factory=set)
    _started: tuple[str, str] | None = None

    async def approve(self, tool: str, arguments: dict[str, JsonValue]) -> bool:
        ctx = self.ctx
        if tool not in APPROVED_TOOLS:
            return False
        if _auto_allowed(ctx, tool, self.operator_shell) or tool in ctx.allowed:
            return True
        self.asked += 1
        answer = await ctx.ui.approve(tool, approval_detail(tool, arguments))
        if answer == "session":
            ctx.allowed.add(tool)
            return True
        if answer == "yes":
            return True
        self.user_denied += 1
        return False

    def on_event(self, event: dict[str, JsonValue]) -> None:
        kind, tool = event.get("event"), str(event.get("tool", ""))
        summary = str(event.get("summary", ""))
        if kind == "tool_start":
            self.tool_calls += 1
            self._started = (tool, summary)
            text = f"{tool} {summary}".strip()
        elif kind == "tool_end":
            # Pi runs one tool at a time, so an end closes the latest start.
            target = self._started[1] if self._started and self._started[0] == tool else ""
            self._started = None
            label = f"{tool} {target}".strip()
            if event.get("ok") is True:
                if tool in FILE_TOOLS and target:
                    self.edited.add(target)
                text = f"{label}: {summary or 'ok'}"
            elif summary.startswith(DENIAL):
                self.policy_denied += 1
                text = f"{label}: denied"
            else:
                text = f"{label}: failed" + (f" ({summary})" if summary else "")
        elif kind == "text":
            text = str(event.get("text", ""))
        else:
            return
        self.ctx.ui.emit(SubagentActivity("coder", text))

    def changed_files(self, before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]
                      ) -> list[str]:
        root = self.ctx.cwd.resolve()
        changed = _changed(before, after)
        for raw in self.edited:
            try:
                path = (root / raw).resolve()
                changed.add(path.relative_to(root).as_posix())
            except (OSError, ValueError):
                continue
        return sorted(changed)[:CHANGED_LIMIT]


def _agent_folder(root: Path, relative: str) -> Path | None:
    """The folder of the Architect-built agent that ``relative`` is a file of, if any: the
    nearest folder above it, inside ``root``, that holds a package manifest and an
    ``artifact`` folder."""
    from architect.forge_modes.export import _MANIFEST

    folder = (root / relative).parent
    while folder != root and root in folder.parents:
        if (folder / _MANIFEST).is_file() and (folder / "artifact").is_dir():
            return folder
        folder = folder.parent
    return None


def _notes(root: Path, changed: list[str], error: str | None) -> list[str]:
    """What the host saw for itself once the run ended; see the module docstring."""
    from architect.forge_modes.export import check_package, read_pin, stop_error

    if not changed:
        return ["No file changed."]
    notes: list[str] = []
    if error == "coder_budget_exceeded":
        notes.append("The run reached its budget before it finished. The files listed "
                     "changed before that.")
    agents: dict[Path, None] = {}
    for relative in changed:
        path = root / relative
        if relative.endswith(".json") and path.is_file():
            try:
                if path.stat().st_size <= JSON_CHECK_BYTES:
                    json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, RecursionError):
                notes.append(f"{relative} is not valid JSON now.")
        folder = _agent_folder(root, relative)
        if folder is not None:
            agents[folder] = None
    for folder in agents:
        try:
            check = check_package(folder, read_pin(folder), registry=None)
            if not check.codes:
                continue
            stop = stop_error(folder, check.codes[0])
        except (OSError, ValueError, TypeError):
            continue
        shown = folder.relative_to(root).as_posix()
        until = (
            " It stays stopped until it passes its exam again or the change is undone."
            if stop.reason == "changed"
            else ""
        )
        notes.append(f"{shown}: {stop.sentence} {stop.reason_line}{until}")
    return notes[:NOTE_LIMIT]


async def run_coder(request: str, ctx: ToolContext) -> JsonValue:
    """Run the Pi coder on ``request`` in ``ctx.cwd`` under ``ctx.permission``.

    Returns ``{"ok", "summary", "changed_files", "tool_calls", "denied", "cost_usd"}``
    (plus ``"error"`` when the run failed; files it changed before failing are listed, and
    ``"notes"`` when the host has something to add to the coder's summary).
    """
    services = ctx.services
    config = coder_config(ctx)
    run = _Run(ctx, operator_shell=services.profile.pi.allow_shell is True)
    backend = PiBackend(config, approver=run.approve, on_event=run.on_event)
    spec = control_spec(
        CODER_TASK,
        ctx.model,
        output_schema={"type": "string"},
        cost_usd=ctx.cap(max(ctx.per_call_usd, CODER_USD)),
        compute_s=max(ctx.per_call_s, CODER_S),
        tools=config.allowed_tools,
    )
    # No context policy: its 2048-character budget covers all tool results of a session,
    # too little for code. Each result stays bounded by ``max_tool_output_chars``.
    components = ("extension_pack",)
    harness = get_preset("coder").build(spec, components=components)
    root = ctx.cwd.resolve()
    task: JsonValue = {"repo": str(root), "request": request}
    adapter = services.adapter_factory(ctx.model)
    adapter = streaming_adapter(adapter) or adapter  # native tool calls when it streams
    ctx.ui.emit(SubagentActivity("coder", _clip(_printable(request).replace("\n", " "), 120)))
    runtime = services.runtime
    usage_before: TelemetryRow | None = runtime.last_usage
    before = await asyncio.to_thread(_snapshot, root)
    error: str | None = None
    summary = ""
    cost = 0.0
    try:
        with runtime.use_pi_backend(backend):
            result = await runtime.evaluate(
                spec, harness, task, adapter, preset="coder", components=components
            )
        output = result.output
        summary = output if isinstance(output, str) else str(output)
        cost = result.cost_usd
    except BudgetExceeded:
        error = "coder_budget_exceeded"
    except PiError:
        error = "coder_failed"
    except PresetError:
        error = "coder_invalid_output"
    if error is not None:
        usage = runtime.last_usage
        if usage is not None and usage is not usage_before:
            cost = usage.cost_usd
    after = await asyncio.to_thread(_snapshot, root)
    changed: list[JsonValue] = list(run.changed_files(before, after))
    reply: dict[str, JsonValue] = {
        "ok": error is None,
        "summary": _clip(summary, SUMMARY_CHARS),
        "changed_files": changed,
        "tool_calls": run.tool_calls,
        "denied": max(run.user_denied, run.policy_denied),
        "cost_usd": cost,
    }
    if error is not None:
        reply["error"] = error
    notes: list[JsonValue] = list(
        await asyncio.to_thread(_notes, root, [str(item) for item in changed], error)
    )
    if notes:
        reply["notes"] = notes
    return reply
