"""Real pinned Pi SDK process behind the parent's guarded model capability.

File tools are repository-confined. ``allow_shell`` is an explicit operator
trust grant: shell runs checkout/host code, NOT an OS or network sandbox.
Concurrent hostile host filesystem mutation is outside this trust boundary.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Awaitable, Callable, Collection, Mapping
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from architect import trust
from architect.models.chat import ChatMessage, ChatResult, ChatStreaming, ToolCallEvent, ToolSpec
from architect.runtime.wallet import active_wallets

if TYPE_CHECKING:
    from architect.runtime import RunContext


class PiError(ValueError):
    """Pi setup, protocol, or policy failure (without transcript contents)."""


FORGE_TOOL_NAMES: Final = frozenset(
    {"forge_check", "forge_dryrun", "forge_measure", "forge_submit", "forge_spawn"}
)
_TRUSTED_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_BUILTIN_TOOLS: Final = frozenset({"read", "write", "edit", "bash", "compile_agent"})
# The mark of an 8.3 short name (TRUSTE~1.JSO): never part of a granted tool path.
_SHORT_NAME: Final = re.compile(r"~\d")


@dataclass(frozen=True)
class TrustedTool:
    """A host-trusted Pi tool; Pi reaches it only through the ``capability`` RPC."""

    name: str  # ^[a-z][a-z0-9_]{0,63}$ (provider tool-name charset)
    description: str  # <= 1024 chars, fixed text
    parameters: dict[str, JsonValue]  # JSON Schema 2020-12, additionalProperties false
    callback: Callable[[dict[str, JsonValue]], Awaitable[JsonValue]]
    max_calls: int
    max_result_chars: int = 16384
    requires_build: bool = True

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or _TRUSTED_TOOL_NAME.fullmatch(self.name) is None
            or not isinstance(self.description, str)
            or len(self.description) > 1024
            or not isinstance(self.parameters, dict)
            or self.parameters.get("type") != "object"
            or self.parameters.get("additionalProperties") is not False
            or not callable(self.callback)
            or type(self.max_calls) is not int
            or self.max_calls < 0
            or type(self.max_result_chars) is not int
            or self.max_result_chars < 1
            or type(self.requires_build) is not bool
        ):
            raise ValueError("Invalid trusted tool")


class PiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    node_executable: str = Field(default="node", min_length=1)
    allowed_tools: tuple[str, ...] = ("read", "write", "edit")
    allow_shell: bool = False
    # Host-owned state roots/files, resolved against the host cwd (never model text).
    denied_paths: tuple[Path, ...] = ()
    max_turns: int = Field(default=64, ge=1, le=1024)
    max_protocol_bytes: int = Field(default=1048576, ge=1024, le=16777216)
    max_tool_output_chars: int = Field(default=32768, ge=1, le=1048576)
    # Re-ask any non-envelope reply while tools are listed, not only one naming a tool
    # or ``tool_calls``. Forge sets it: its work is unfinished until a submission, and a
    # live model once answered prose mid-session and ended the work with none. JSON
    # envelope only: plain text is how a native tool-calling reply finishes.
    repair_prose: bool = False
    # A capability the session exists to call (Forge: ``forge_submit``). A final answer
    # that arrives while it is granted and can still be called is re-asked, in either
    # transport: once before its first call, then once after each call. A live session
    # once ended on a progress note with every submission left.
    final_requires: str | None = None
    # Who enforces ``pack.context_policy.max_characters``. "extension": the bridge keeps
    # the newest tool-result text and slices older text. "parent": ``_ContextCap``'s rule
    # drops whole old tool-result bodies and trims old write and edit texts, oldest
    # first, the same way every time. The extension applies that rule before sending a
    # context (``init.context_cap``), and the parent applies it again to what arrives.
    # With it, a write or edit whose text holds one of its notes is refused.
    context_cap: Literal["extension", "parent"] = "extension"

    @field_validator("allowed_tools")
    @classmethod
    def known_tools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or set(value) - (_BUILTIN_TOOLS | FORGE_TOOL_NAMES):
            raise ValueError("Unsupported or duplicate Pi tools")
        return value

    @field_validator("final_requires")
    @classmethod
    def capability_name(cls, value: str | None) -> str | None:
        if value is not None and _TRUSTED_TOOL_NAME.fullmatch(value) is None:
            raise ValueError("Invalid required capability")
        return value


class PiEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    pid: int
    exit_code: int
    extension_loaded: bool
    model_calls: int
    tool_calls: int
    versions: dict[str, str]
    # How parent-model turns reached the provider, and how many replies were re-asked.
    transport: Literal["envelope", "native"] = "envelope"
    repairs: int = 0


_EVIDENCE: ContextVar[tuple[int, PiEvidence] | None] = ContextVar("pi_evidence", default=None)
_NODE = Path(__file__).with_name("pi-node")
_PACKAGE = Path(__file__).resolve().parents[1]


def _resolve_node(configured: str, repo: Path) -> Path:
    """Resolve a trusted host executable without searching the coding checkout.

    ``ARCHITECT_NODE`` replaces the default ``node``: the desktop app sets it to its own
    Electron binary, which ``run`` starts as Node, so a packaged app needs no Node install.
    """
    if configured == "node" and os.environ.get("ARCHITECT_NODE"):
        configured = os.environ["ARCHITECT_NODE"]
    if Path(configured).parent != Path(".") or Path(configured).is_absolute():
        candidates = [Path(configured).expanduser()]
    else:
        candidates = []
        for entry in os.get_exec_path():
            directory = Path(entry)
            if not entry or not directory.is_absolute():
                continue
            try:
                directory = directory.resolve()
                if directory.is_relative_to(repo):
                    continue
                # An absolute candidate makes Windows which skip implicit cwd search.
                found = shutil.which(str(directory / configured))
                if found:
                    candidates.append(Path(found))
            except (OSError, ValueError):
                continue
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            if (
                not resolved.is_relative_to(repo)
                and resolved.is_file()
                and os.access(resolved, os.X_OK)
            ):
                return resolved
        except (OSError, ValueError):
            continue
    raise PiError("Cannot resolve a host Node executable outside the granted repository")


def _windows_job(pid: int) -> int | None:
    """Put the still-idle bridge in a kill-on-close job, including descendants."""
    if os.name != "nt":
        return None
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_int64),
            ("job_time", ctypes.c_int64),
            ("flags", wintypes.DWORD),
            ("minimum", ctypes.c_size_t),
            ("maximum", ctypes.c_size_t),
            ("active", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class Extended(ctypes.Structure):
        _fields_ = [
            ("basic", Basic),
            ("io", ctypes.c_uint64 * 6),
            ("process_memory", ctypes.c_size_t),
            ("job_memory", ctypes.c_size_t),
            ("peak_process", ctypes.c_size_t),
            ("peak_job", ctypes.c_size_t),
        ]

    kernel = cast(Any, ctypes).WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise PiError("Cannot create Pi process job")
    info = Extended()
    info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, no breakaway.
    process = kernel.OpenProcess(0x0100 | 0x0001 | 0x0400, False, pid)
    try:
        if (
            not process
            or not kernel.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
            or not kernel.AssignProcessToJobObject(job, process)
        ):
            kernel.CloseHandle(job)
            raise PiError("Cannot confine Pi process tree to job")
    finally:
        if process:
            kernel.CloseHandle(process)
    return cast(int, job)


def _close_job(job: int | None) -> None:
    if job is not None:
        kernel = cast(Any, ctypes).WinDLL("kernel32", use_last_error=True)
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle(job)


def _process_table() -> dict[int, tuple[int, int | None, int | None]] | None:
    """Return ``pid -> (ppid, pgid, sid)`` for visible processes, or None."""
    if sys.platform == "win32":
        return None
    table: dict[int, tuple[int, int | None, int | None]] = {}
    if os.path.exists("/proc/self/stat"):
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/stat", "rb") as handle:
                    stat = handle.read()
                # Fields after the parenthesised command: state ppid pgrp session ...
                fields = stat[stat.rindex(b")") + 2 :].split()
                table[int(name)] = (int(fields[1]), int(fields[2]), int(fields[3]))
            except (OSError, ValueError, IndexError):
                continue
        return table or None
    ps = shutil.which("ps", path="/bin:/usr/bin")
    if ps is None:
        return None
    try:
        listing = subprocess.run(
            [ps, "-A", "-o", "pid=,ppid=,pgid="],
            capture_output=True,
            check=True,
            timeout=10,
            env={"LC_ALL": "C", "PATH": "/bin:/usr/bin"},
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in listing.splitlines():
        try:
            pid, ppid, pgid = (int(value) for value in line.split())
        except ValueError:
            continue
        try:
            sid: int | None = os.getsid(pid)
        except OSError:
            sid = None
        table[pid] = (ppid, pgid, sid)
    return table or None


def _tree_members(
    table: dict[int, tuple[int, int | None, int | None]], root: int, *, root_alive: bool = True
) -> set[int]:
    """Processes in the root's session/group plus every live descendant by parentage.

    Session and group ids stay valid after the leader exits: the kernel does
    not reuse a PID while a session or group still carries it. The root PID
    itself can be reused once reaped, so parent links are followed from the
    root only while it is known to be alive (``root_alive``). Processes in the
    host's own session or group are never members: a descendant of a
    ``start_new_session`` child cannot join them.
    """
    if sys.platform == "win32":
        return set()
    host = {os.getpid(), os.getppid()}
    try:
        host_sid: int | None = os.getsid(0)
    except OSError:
        host_sid = None
    host_pgid = os.getpgrp()

    def foreign(pid: int, pgid: int | None, sid: int | None) -> bool:
        return (
            pid > 1
            and pid not in host
            and pgid != host_pgid
            and (host_sid is None or sid != host_sid)
        )

    members = {
        pid
        for pid, (_, pgid, sid) in table.items()
        if pid != root and root in (pgid, sid) and foreign(pid, pgid, sid)
    }
    frontier = members | {root} if root_alive else set(members)
    while frontier:
        found = {
            pid
            for pid, (ppid, pgid, sid) in table.items()
            if ppid in frontier and pid != root and pid not in members and foreign(pid, pgid, sid)
        }
        members |= found
        frontier = found
    return members


def _reap_posix_tree(root: int, *, root_alive: bool = True) -> None:
    """Freeze then kill the root's descendants, including re-sessioned ones.

    ``killpg`` alone misses a descendant that called ``setsid``/``setpgid``
    (for example Node ``detached: true``). A ``setpgid`` descendant keeps the
    root's session id even after the root exits, and parent links reach a
    ``setsid`` one while its ancestors live, so collect both, SIGSTOP each
    member so none can fork further, repeat until the set is stable, then
    SIGKILL. The root itself is left to the caller. Pass ``root_alive=False``
    once the root has been reaped so its possibly reused PID is not walked. A
    process that has both left the session and been orphaned before this runs
    has no remaining link and cannot be found.
    """
    if sys.platform == "win32":
        return
    stopped: set[int] = set()
    for _ in range(64):
        table = _process_table()
        if table is None:
            break
        found = _tree_members(table, root, root_alive=root_alive) - stopped
        if not found:
            break
        for pid in found:
            try:
                os.kill(pid, signal.SIGSTOP)
            except OSError:
                pass
        stopped |= found
    for pid in stopped:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _group_has_live_member(pgid: int) -> bool:
    """Whether any non-zombie process is in group ``pgid`` (True if unknown)."""
    if sys.platform == "win32":
        return True
    if os.path.exists("/proc/self/stat"):
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/stat", "rb") as handle:
                    stat = handle.read()
                fields = stat[stat.rindex(b")") + 2 :].split()
                if int(fields[2]) == pgid and fields[0] not in (b"Z", b"X"):
                    return True
            except (OSError, ValueError, IndexError):
                continue
        return False
    ps = shutil.which("ps", path="/bin:/usr/bin")
    if ps is None:
        return True
    try:
        listing = subprocess.run(
            [ps, "-A", "-o", "pgid=,stat="],
            capture_output=True,
            check=True,
            timeout=10,
            env={"LC_ALL": "C", "PATH": "/bin:/usr/bin"},
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            group = int(parts[0])
        except ValueError:
            continue
        if group == pgid and not parts[1].startswith(b"Z"):
            return True
    return False


def _kill_posix_group(pgid: int) -> None:
    """SIGKILL the group; a gone or zombie-only group is not an error.

    macOS answers ``killpg`` with EPERM for a group whose members are all
    zombies awaiting collection by launchd. That is benign only when no live
    member remains; anything else is a fixed failure, never a raw OSError.
    """
    try:
        cast(Any, os).killpg(pgid, cast(Any, signal).SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        if not _group_has_live_member(pgid):
            return
        raise PiError("Pi process group could not be terminated") from None
    except OSError:
        raise PiError("Pi process group could not be terminated") from None


def _guarded(
    repo: Path, raw: object, denied_paths: tuple[Path, ...], *, root: bool = False
) -> tuple[Path, Path] | None:
    """``(path, resolved)`` for a model-supplied path every guard allows, else None.

    Inside the repository (the repository itself only with ``root``), never under a
    denied path or an ``.architect`` spelling, and no symlink or junction on the way.
    Nothing here looks at what is at the path.
    """
    if not isinstance(raw, str) or not raw or raw.startswith(("~", "@")):
        return None
    # Reject upstream alternate-spelling expansion, device paths and Windows ADS.
    if any(ord(char) < 32 or (char.isspace() and char != " ") for char in raw):
        return None
    candidate = Path(raw)
    if os.name == "nt" and any(part.endswith((".", " ")) for part in candidate.parts):
        return None
    if ".." in candidate.parts or ":" in raw.replace(candidate.drive, "", 1):
        return None
    # A ~digit segment may be an 8.3 alias of a protected name (FAT and NTFS volumes).
    if any(_SHORT_NAME.search(part) for part in candidate.parts):
        return None
    path = repo / candidate
    try:
        resolved = path.resolve()
        if not resolved.is_relative_to(repo) or (resolved == repo and not root):
            return None
        # Without case: APFS and NTFS, the macOS and Windows defaults, open a case
        # variant (OPERATOR/TRUSTED-KEYS.JSON) as the protected file itself.
        if any(trust.within(resolved, denied) for denied in denied_paths):
            return None
        # Also refuse spelling aliases of conventional private control-plane state.
        if any(part.casefold() == ".architect" for part in resolved.relative_to(repo).parts):
            return None
        current = path
        while current != repo:
            if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
                return None
            if current == current.parent:
                return None
            current = current.parent
    except (OSError, ValueError, RuntimeError):
        return None
    return path, resolved


def _granted_path(
    repo: Path, raw: object, name: str, maximum: int, denied_paths: tuple[Path, ...]
) -> bool:
    guarded = _guarded(repo, raw, denied_paths)
    if guarded is None:
        return False
    path, _ = guarded
    try:
        if path.exists():
            stat = path.stat()
            if not path.is_file() or stat.st_nlink > 1:
                return False
            if name in {"read", "edit"} and stat.st_size > maximum:
                return False
        elif name in {"read", "edit"}:
            # Avoid Pi read's fallback to NFD/curly-quote alternate paths.
            return False
        return True
    except (OSError, ValueError, RuntimeError):
        return False


_LISTED_ENTRIES: Final = 64
_SCANNED_ENTRIES: Final = 512
_ENTRY_CHARS: Final = 200
_READ_MISSING: Final = "Not found: {path}"
_READ_DIRECTORY: Final = "{path} is a directory, not a file."
_READ_ROOT: Final = "That is the working directory, not a file."
_READ_HERE: Final = "The working directory"  # the listed place when it is the repository
_READ_HOLDS: Final = "{place} holds:"
_READ_EMPTY: Final = "{place} is empty."
_READ_MORE: Final = "... and more"  # past _SCANNED_ENTRIES: no count
_READ_REST: Final = "... and {count} more"


def _entries(directory: Path, repo: Path, denied_paths: tuple[Path, ...]) -> list[str]:
    """A bounded listing of ``directory`` as repository-relative paths (``name/`` for a
    directory). Symlinks, junctions, ``.architect`` spellings and denied paths are left out.

    At most ``_SCANNED_ENTRIES`` entries are examined (this runs on the event loop), so a
    huge directory ends with "... and more" rather than a count.
    """
    listed: list[str] = []
    skipped = 0
    names = sorted(directory.iterdir(), key=lambda item: item.name)
    for child in names[:_SCANNED_ENTRIES]:
        if child.is_symlink() or (hasattr(child, "is_junction") and child.is_junction()):
            continue
        if child.name.casefold() == ".architect":
            continue
        target = child.resolve()
        if any(trust.within(target, denied) for denied in denied_paths):
            continue
        if len(listed) >= _LISTED_ENTRIES:
            skipped += 1
            continue
        relative = child.relative_to(repo).as_posix()
        listed.append(_plain(relative, _ENTRY_CHARS) + ("/" if child.is_dir() else ""))
    if len(names) > _SCANNED_ENTRIES:
        listed.append(_READ_MORE)
    elif skipped:
        listed.append(_READ_REST.format(count=skipped))
    return listed


def _read_refusal(repo: Path, raw: object, denied_paths: tuple[Path, ...]) -> str | None:
    """Why a guarded ``read`` cannot run when the path is missing or a directory.

    Such a read is still refused (Pi's read would try alternate spellings of a missing
    name), but the model is told what happened and what is there: a missing file comes
    with its nearest existing directory's entries, a directory with its own. Only paths
    every guard allows (``_guarded``) get this; any other refusal keeps the policy text,
    so nothing is said about anything outside the granted repository.
    """
    guarded = _guarded(repo, raw, denied_paths, root=True)
    if guarded is None:
        return None
    _, resolved = guarded  # no link on the way: the resolved path is the same place
    try:
        if resolved.exists() and not resolved.is_dir():
            return None  # a file another guard refused: the policy text stays
        shown = resolved.relative_to(repo).as_posix()
        if resolved.is_dir():
            lines = [_READ_ROOT if resolved == repo else _READ_DIRECTORY.format(
                path=_plain(shown, _ENTRY_CHARS) + "/"
            )]
            directory = resolved
        else:
            lines = [_READ_MISSING.format(path=_plain(shown, _ENTRY_CHARS))]
            directory = resolved.parent
            while directory != repo and not directory.exists():
                directory = directory.parent
            if not directory.is_dir():
                return None
        entries = _entries(directory, repo, denied_paths)
    except (OSError, ValueError, RuntimeError):
        return None
    place = _READ_HERE if directory == repo else directory.relative_to(repo).as_posix() + "/"
    shown_place = _plain(place, _ENTRY_CHARS)
    if not entries:
        return "\n".join([*lines, _READ_EMPTY.format(place=shown_place)])
    return "\n".join([*lines, _READ_HOLDS.format(place=shown_place), *entries])


def _check_parent(context: RunContext) -> None:
    """A swallowed nested compiler denial must still stop the current tool batch."""
    from architect.runtime import BudgetExceeded

    context._check()
    if any(wallet.exhausted or wallet.remaining_s <= 0 for wallet in active_wallets()):
        raise BudgetExceeded("Parent compilation budget exhausted")


_COMPILE_AGENT_SCHEMA: Final[dict[str, JsonValue]] = {
    "type": "object",
    "properties": {"request": {"type": "string"}},
    "required": ["request"],
    "additionalProperties": False,
}
_COMPILE_AGENT_DESCRIPTION: Final = (
    "Ask the trusted bounded compiler to propose or compile a feature. "
    "Cannot grant approval or supply reports."
)
# Bytes reserved for the ``{"id": n, "value": ...}`` reply envelope.
_REPLY_ENVELOPE: Final = 64


def _soft(code: str) -> dict[str, JsonValue]:
    """A fixed, content-free tool failure the agent may read and recover from."""
    return {"ok": False, "code": code}


# --- Phase 12 hooks: interactive approvals and one-way informational events ---
Approver = Callable[[str, dict[str, JsonValue]], Awaitable[bool]]
EventSink = Callable[[dict[str, JsonValue]], None]
# The tools whose requests the approver sees, after every existing guard passed.
APPROVED_TOOLS: Final = frozenset({"write", "edit", "bash"})
_EVENT_SUMMARY_CHARS: Final = 200
_EVENT_TEXT_CHARS: Final = 400
# Events are informational: past this many in one run the rest are dropped, not errors.
_MAX_EVENTS: Final = 4096
# C0/C1 controls and bidirectional overrides never reach a terminal from model text.
_UNPRINTABLE: Final = re.compile(r"[\x00-\x1f\x7f-\x9f‎‏‪-‮⁦-⁩]+")


def _plain(text: str, limit: int) -> str:
    return _UNPRINTABLE.sub(" ", text[: limit * 4]).strip()[:limit]


def _event(message: Mapping[str, object]) -> dict[str, JsonValue] | None:
    """The sanitized, bounded form of one bridge event, or None for an unknown shape.

    ``{"event": "tool_start", "tool", "summary"}``, ``{"event": "tool_end", "tool",
    "ok", "summary"}`` or ``{"event": "text", "text"}``; other keys are never copied.
    """
    name = message.get("event")
    if name in ("tool_start", "tool_end"):
        tool, summary = message.get("tool"), message.get("summary", "")
        if (
            not isinstance(tool, str)
            or _TRUSTED_TOOL_NAME.fullmatch(tool) is None
            or not isinstance(summary, str)
        ):
            return None
        event: dict[str, JsonValue] = {
            "event": name,
            "tool": tool,
            "summary": _plain(summary, _EVENT_SUMMARY_CHARS),
        }
        if name == "tool_end":
            ok = message.get("ok")
            if type(ok) is not bool:
                return None
            event["ok"] = ok
        return event
    if name == "text":
        text = message.get("text")
        if not isinstance(text, str):
            return None
        text = _plain(text, _EVENT_TEXT_CHARS)
        return {"event": "text", "text": text} if text else None
    return None


# --- Guarded model protocol (the parent side of the bridge's control envelope) ---
_PROTOCOL_INSTRUCTION: Final = (
    "You are the model inside a Pi coding agent. EVERY reply must be exactly one JSON "
    "object and nothing else: no prose, no markdown, no code fences. To use tools, reply "
    '{"tool_calls":[{"id":"call-1","name":"<tool name>","arguments":{...}}]} with one or '
    "more calls, unique ids and arguments matching the tool's parameters. When finished, "
    'reply {"text":"<final answer>"}. Only the tools listed in "tools" exist. '
    'Example tool use: {"tool_calls":[{"id":"call-1","name":"<tool name>",'
    '"arguments":{"<parameter>":"<value>"}}]}. Example finish: {"text":"Done."}. '
    'The conversation so far, including tool results, is in "pi_context".'
)
_NO_TOOLS_INSTRUCTION: Final = (
    "You are the model inside a Pi agent. No tools are available. EVERY reply must be "
    'exactly one JSON object and nothing else: {"text":"<final answer>"}. '
    'Example: {"text":"Done."}. The conversation so far is in "pi_context".'
)
_REPAIR_MESSAGE: Final = (
    "Your last reply was not a JSON object in the required format. Continue the task "
    'set by the system prompt and the first user message in "pi_context". Reply again '
    'with exactly one JSON object and nothing else: {"tool_calls":[{"id":"call-1",'
    '"name":"<tool name>","arguments":{...}}]} to use a listed tool, or '
    '{"text":"<final answer>"} only if the task is finished. No prose, no code fences.'
)
# Last in every prompt: a long context pushes the opening instruction far back, and a
# live model once answered prose ("I don't see a specific request yet") mid-session.
_REMINDER: Final = (
    'Continue the task set by the system prompt and the first user message in '
    '"pi_context", taking the tool results so far into account. Reply now with exactly '
    'one JSON object: {"tool_calls":[...]} to use a listed tool, or {"text":"..."} only '
    "when the task is finished."
)
# Message keys the model needs; the rest (usage, timestamps, provider labels) is noise.
_MESSAGE_KEYS: Final = ("role", "content", "toolCallId", "toolName", "isError")
_TOOL_ATTEMPT_SCAN_CHARS: Final = 4096
_MAX_REPAIRS: Final = 2
_TOOL_LISTING_CHARS: Final = 12000
_TOOL_DESCRIPTION_CHARS: Final = 300
_REPLY_ECHO_CHARS: Final = 2000
_ENVELOPE_CANDIDATES: Final = 64
_CONTROL_KEYS: Final = frozenset({"text", "tool_calls"})
# A reply cut at the output limit is never shown to the model again, in either transport:
# it is re-asked once with this short instruction alone. A live repair that echoed a cut
# reply back ran to the output cap twice, and the model nested the echo in its answer.
_CUT_REPLY: Final = (
    "Your last reply reached the output limit, so it was dropped and none of it ran. "
    "Take a smaller step: edit part of a file, or write a shorter one."
)
# A final answer before the session's required capability was ever called
# (``PiConfig.final_requires``) is re-asked once with this.
_UNFINISHED: Final = (
    "The task is not finished: {tool} has not been called yet, and a reply without a tool "
    "call ends the session. Keep working with the tools, and call {tool} when the work is "
    "ready."
)
# After each call of the required capability, a final answer while it still has calls
# left is re-asked once with this: a live session once ended on a progress note.
_UNFINISHED_AGAIN: Final = (
    "A reply without a tool call ends the session, and {tool} can still be called. If the "
    "work is finished, send the final answer again. Otherwise keep working with the tools "
    "and call {tool} when the work is better."
)
_ENVELOPE_TOOL_FORMAT: Final = (
    ' Continue the task set by the system prompt and the first user message in "pi_context".'
    ' Reply with exactly one JSON object and nothing else: {"tool_calls":[{"id":"call-1",'
    '"name":"<tool name>","arguments":{...}}]} to use a listed tool. No prose, no code fences.'
)
_ENVELOPE_EITHER_FORMAT: Final = (
    ' Continue the task set by the system prompt and the first user message in "pi_context".'
    ' Reply with exactly one JSON object and nothing else: {"tool_calls":[{"id":"call-1",'
    '"name":"<tool name>","arguments":{...}}]} to use a listed tool, or {"text":"<final '
    'answer>"} if the work is finished. No prose, no code fences.'
)
_CUT_CORRECTION: Final = _CUT_REPLY + _ENVELOPE_TOOL_FORMAT
_UNFINISHED_CORRECTION: Final = _UNFINISHED + _ENVELOPE_TOOL_FORMAT  # {tool} in the first part
_UNFINISHED_AGAIN_CORRECTION: Final = _UNFINISHED_AGAIN + _ENVELOPE_EITHER_FORMAT


def _unfinished_text(tool: str, called: bool) -> str:
    """The re-ask after a final answer that ``final_requires`` stops (either transport)."""
    return (_UNFINISHED_AGAIN if called else _UNFINISHED).format(tool=tool)


def _unfinished_correction(tool: str, called: bool = False) -> str:
    template = _UNFINISHED_AGAIN_CORRECTION if called else _UNFINISHED_CORRECTION
    return template.replace("{tool}", tool)


def _compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def _tool_listing(pi_context: object, granted: Collection[str]) -> list[dict[str, JsonValue]]:
    """Granted tools from the Pi context, in its order, bounded to a fixed size.

    Names are always listed; a schema that would overflow the bound is left out.
    """
    raw = pi_context.get("tools") if isinstance(pi_context, dict) else None
    if not isinstance(raw, list):
        return []
    listing: list[dict[str, JsonValue]] = []
    seen: set[str] = set()
    used = 2
    for item in raw:
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(name, str) or name not in granted or name in seen:
            continue
        assert isinstance(item, dict)
        seen.add(name)
        entry: dict[str, JsonValue] = {"name": name}
        description = item.get("description")
        if isinstance(description, str) and description:
            entry["description"] = description[:_TOOL_DESCRIPTION_CHARS]
        parameters = item.get("parameters")
        if isinstance(parameters, dict):
            try:
                size = len(_compact({**entry, "parameters": parameters}))
            except (TypeError, ValueError, RecursionError):
                size = _TOOL_LISTING_CHARS + 1
            if used + size + 1 <= _TOOL_LISTING_CHARS:
                entry["parameters"] = cast(JsonValue, parameters)
        used += len(_compact(entry)) + 1
        listing.append(entry)
    return listing


def _reject_constant(name: str) -> object:
    raise ValueError("Non-standard JSON constant")


def _bridge_control(value: object) -> bool:
    """Exactly the bridge's ``isControl`` test for a whole-reply JSON value."""
    return (
        isinstance(value, dict)
        and set(value) <= _CONTROL_KEYS
        and ("tool_calls" in value or isinstance(value.get("text"), str))
    )


def _strict_envelope(value: object, max_calls: int) -> dict[str, JsonValue] | None:
    """A control envelope the bridge will accept without error, or None."""
    if not _bridge_control(value):
        return None
    assert isinstance(value, dict)
    envelope: dict[str, JsonValue] = {}
    if "text" in value:
        if not isinstance(value["text"], str):
            return None
        envelope["text"] = value["text"]
    if "tool_calls" in value:
        calls = value["tool_calls"]
        if not isinstance(calls, list) or len(calls) > max_calls:
            return None
        ids: set[str] = set()
        normalized: list[JsonValue] = []
        for call in calls:
            if not isinstance(call, dict):
                return None
            call_id, name, arguments = call.get("id"), call.get("name"), call.get("arguments")
            if (
                not isinstance(call_id, str)
                or not call_id
                or call_id in ids
                or not isinstance(name, str)
                or not name
                or not isinstance(arguments, dict)
            ):
                return None
            ids.add(call_id)
            normalized.append({"id": call_id, "name": name, "arguments": arguments})
        envelope["tool_calls"] = normalized
    return envelope


def _control_reply(reply: str, max_calls: int) -> tuple[str, bool] | None:
    """The envelope JSON to hand the bridge and whether it calls tools, or None.

    A reply that is wholly an envelope is returned byte-identically (the bridge
    keeps its own checks). Otherwise the first balanced top-level JSON object that
    is a valid envelope (inside a code fence or next to short prose) is normalized.
    """
    decoder = json.JSONDecoder(parse_constant=_reject_constant)
    try:
        whole = json.loads(reply, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        pass
    else:
        if _bridge_control(whole):
            assert isinstance(whole, dict)
            calls = whole.get("tool_calls")
            return reply, isinstance(calls, list) and bool(calls)
    index = 0
    for _ in range(_ENVELOPE_CANDIDATES):
        start = reply.find("{", index)
        if start < 0:
            break
        try:
            value, end = decoder.raw_decode(reply, start)
        except (ValueError, RecursionError):
            index = start + 1
            continue
        envelope = _strict_envelope(value, max_calls)
        if envelope is not None:
            try:
                return _compact(envelope), bool(envelope.get("tool_calls"))
            except (TypeError, ValueError, RecursionError):
                return None
        index = end  # Top-level objects only: never an envelope nested in other JSON.
    return None


def _tool_attempt(reply: str, names: Collection[str], *, used_tools: bool) -> bool:
    """Whether a non-envelope reply is a missed tool call rather than a final answer.

    Any reply naming the envelope key is one. Before the first tool use, so is a reply
    naming an available tool (a live model once answered just ``read``). After tool
    use, a plain reply is final even if it names a tool, and ordinary prose is final
    unless the backend sets ``repair_prose`` (Forge does).
    """
    words = sorted({"tool_calls"} if used_tools else {*names, "tool_calls"})
    pattern = r"(?<![A-Za-z0-9_])(?:" + "|".join(map(re.escape, words)) + r")(?![A-Za-z0-9_])"
    return re.search(pattern, reply[:_TOOL_ATTEMPT_SCAN_CHARS], re.IGNORECASE) is not None


def _model_view(pi_context: object) -> JsonValue:
    """The Pi context as the model sees it: the system prompt and the messages with
    only the keys a model needs, in a fixed order. Tools are listed separately.

    Deterministic, so the prompt prefix stays byte-stable across a session.
    """
    if not isinstance(pi_context, dict):
        return cast(JsonValue, pi_context)
    view: dict[str, JsonValue] = {}
    if "systemPrompt" in pi_context:
        view["systemPrompt"] = cast(JsonValue, pi_context["systemPrompt"])
    messages = pi_context.get("messages")
    if isinstance(messages, list):
        view["messages"] = [
            {key: item[key] for key in _MESSAGE_KEYS if key in item}
            if isinstance(item, dict)
            else item
            for item in messages
        ]
    elif messages is not None:
        view["messages"] = cast(JsonValue, messages)
    return view


# --- Native tool calls (P3): Pi's tools as the provider's own tool definitions ---
#
# When the bound adapter streams native chats (``ChatStreaming``), each parent turn is one
# ``RunContext.stream_call``: the granted tools go to the provider as tool definitions and
# Pi's context as chat messages. The reply reaches the bridge as the same control envelope
# a JSON reply would be, so bridge.mjs is unchanged. Adapters without native tools (the
# coding-agent CLIs, plain completion adapters) keep the envelope. A live A/B on
# 2026-10-03 gave 8 of 8 usable code writes as native calls and 2 of 8 through the envelope.
_NATIVE_INSTRUCTION: Final = (
    "You are the model inside a Pi coding agent. Do the work by calling the offered tools; "
    "no other tools exist. A reply without a tool call ends the session, so answer in plain "
    "text only when the task is finished."
)
_NATIVE_NO_TOOLS: Final = (
    "You are the model inside a Pi agent. No tools are available. Reply with the final "
    "answer in plain text."
)
_NATIVE_REPAIRS: Final = 1
_NATIVE_DESCRIPTION_CHARS: Final = 1024  # a trusted tool's own description limit
_NATIVE_CALL_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
# Fixed tool results for a re-asked reply. Its malformed arguments are never sent back,
# and a reply cut at the output limit is not shown at all (``_CUT_REPLY``).
_BAD_ARGUMENTS: Final = _compact(
    {
        "error": "invalid_arguments",
        "detail": "These arguments were not one complete JSON object, so no call in this "
        "reply ran. Call the tools again with valid arguments.",
    }
)
_NOT_RUN: Final = _compact(
    {
        "error": "not_run",
        "detail": "Another call in this reply had invalid arguments, so this one did not run "
        "either. Call it again if it is still needed.",
    }
)
_UNANSWERED: Final = _compact({"error": "not_run"})


def _text_parts(content: object, separator: str) -> str:
    """The text of a Pi message ``content``: a string, or a list of typed parts."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return separator.join(
        item["text"]
        for item in content
        if isinstance(item, dict)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
    )


def _native_tools(pi_context: object, granted: Collection[str]) -> tuple[ToolSpec, ...]:
    """Granted tools from the Pi context as native tool definitions, in its order.

    Descriptions are kept whole up to a trusted tool's limit. A tool whose parameters are
    not an object schema is offered with an open one; Pi validates every call anyway.
    """
    raw = pi_context.get("tools") if isinstance(pi_context, dict) else None
    specs: list[ToolSpec] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(name, str) or name not in granted or name in seen:
            continue
        seen.add(name)
        description, parameters = item.get("description"), item.get("parameters")
        specs.append(
            ToolSpec(
                name,
                description[:_NATIVE_DESCRIPTION_CHARS] if isinstance(description, str) else "",
                parameters
                if isinstance(parameters, dict) and parameters.get("type") == "object"
                else {"type": "object"},
            )
        )
    return tuple(specs)


def _native_system(pi_context: object, tools: bool) -> str:
    """The native instruction, then Pi's own system prompt (byte-stable in a session)."""
    prompt = pi_context.get("systemPrompt") if isinstance(pi_context, dict) else None
    head = _NATIVE_INSTRUCTION if tools else _NATIVE_NO_TOOLS
    return f"{head}\n\n{prompt}" if isinstance(prompt, str) and prompt else head


def _native_messages(pi_context: object) -> list[ChatMessage]:
    """The Pi conversation as provider-valid chat messages, built deterministically.

    Pi answers every tool call in order, so this is normally a plain mapping (thinking
    parts and bookkeeping keys are dropped). Defensively, a call left unanswered gets an
    error result before the next message, and a result without an open call becomes a
    user note, so a provider never sees a broken pairing.
    """
    raw = pi_context.get("messages") if isinstance(pi_context, dict) else None
    out: list[ChatMessage] = []
    waiting: dict[str, str] = {}  # call id -> tool name, unanswered, in call order

    def close() -> None:
        for call_id, name in waiting.items():
            out.append(
                ChatMessage("tool", _UNANSWERED, tool_call_id=call_id, name=name, is_error=True)
            )
        waiting.clear()

    for message in raw if isinstance(raw, list) else []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "toolResult":
            text = _text_parts(message.get("content"), "\n")
            call_id = message.get("toolCallId")
            if isinstance(call_id, str) and call_id in waiting:
                is_error = message.get("isError") is True
                name = waiting.pop(call_id)
                out.append(
                    ChatMessage("tool", text, tool_call_id=call_id, name=name, is_error=is_error)
                )
                continue
            close()
            label = message.get("toolName")
            label = label if isinstance(label, str) and label else "a tool"
            out.append(ChatMessage("user", f"[Earlier result of {label}]\n{text}"))
            continue
        close()
        if role == "user":
            text = _text_parts(message.get("content"), "\n")
            if text:
                out.append(ChatMessage("user", text))
        elif role == "assistant":
            content = message.get("content")
            parts = content if isinstance(content, list) else []
            calls: list[ToolCallEvent] = []
            for item in parts:
                if not isinstance(item, dict) or item.get("type") != "toolCall":
                    continue
                ident, tool, arguments = item.get("id"), item.get("name"), item.get("arguments")
                if (
                    isinstance(ident, str)
                    and ident
                    and ident not in waiting
                    and isinstance(tool, str)
                    and tool
                    and isinstance(arguments, dict)
                ):
                    waiting[ident] = tool
                    calls.append(ToolCallEvent(ident, tool, arguments))
            text = _text_parts(parts, "")
            if text or calls:
                out.append(ChatMessage("assistant", text, tool_calls=tuple(calls)))
    close()
    return out


def _malformed(call: ToolCallEvent) -> bool:
    """A call that must not run: its arguments were not a JSON object, or it has no name."""
    return call.error is not None or not call.name


def _call_id(original: str, used: set[str]) -> str:
    """A provider-safe call id not yet used in this session (``call_<n>`` when needed)."""
    if _NATIVE_CALL_ID.fullmatch(original) is None or original in used:
        number = len(used)
        while f"call_{number}" in used:
            number += 1
        original = f"call_{number}"
    used.add(original)
    return original


def _native_envelope(result: ChatResult, used: set[str]) -> str:
    """The control envelope the bridge expects for one native reply.

    Calls keep their order, and their ids are unique within the session (Pi and every
    provider need that). Text beside calls is narration; without calls it is the answer.
    """
    envelope: dict[str, JsonValue] = {}
    if result.text.strip() or not result.tool_calls:
        envelope["text"] = result.text
    if result.tool_calls:
        envelope["tool_calls"] = [
            {"id": _call_id(call.id, used), "name": call.name, "arguments": call.arguments}
            for call in result.tool_calls
        ]
    try:
        return _compact(envelope)
    except (TypeError, ValueError, RecursionError):
        raise PiError("Invalid native tool call") from None


def _native_repair(result: ChatResult, used: Collection[str]) -> list[ChatMessage]:
    """The re-asked reply and why none of its calls ran, as messages for one more call.

    Each malformed call is shown with empty arguments and a fixed error, and every other
    call gets ``not_run``. A reply cut at the output limit never comes here: it is not
    shown again at all (``_native_cut``).
    """
    taken = set(used)
    calls: list[ToolCallEvent] = []
    results: list[ChatMessage] = []
    for call in result.tool_calls:
        bad = _malformed(call)
        name = call.name or "unknown_tool"
        shown = ToolCallEvent(_call_id(call.id, taken), name, {} if bad else call.arguments)
        calls.append(shown)
        results.append(
            ChatMessage(
                "tool",
                _BAD_ARGUMENTS if bad else _NOT_RUN,
                tool_call_id=shown.id,
                name=name,
                is_error=True,
            )
        )
    return [ChatMessage("assistant", result.text, tool_calls=tuple(calls)), *results]


def _native_cut() -> list[ChatMessage]:
    """The re-ask after a reply cut at the output limit: one short instruction, never the
    reply itself (its text or calls)."""
    return [ChatMessage("user", _CUT_REPLY)]


def _native_unfinished(text: str, tool: str, called: bool = False) -> list[ChatMessage]:
    """The re-ask after a final answer that ``final_requires`` stops: before ``tool`` was
    ever called, or (``called``) while it still has calls left."""
    note = ChatMessage("user", _unfinished_text(tool, called))
    return [ChatMessage("assistant", text), note] if text.strip() else [note]


# --- Parent-side context cap (``PiConfig.context_cap == "parent"``) ------------------
_DROPPED_RESULT: Final = (
    "[This tool result was removed to keep the conversation under its size limit. Call "
    "the tool again if you still need it.]"
)
# What stands in for an old write's or edit's file text once it is trimmed. It reads as a
# note, never as text for a file (a live engineer pasted the older "[N characters, ...]"
# form into harness.py 43 times), and a write or edit that carries it is refused
# (``_NOTE_REFUSED``).
_TRIMMED_WRITE: Final = (
    "(Old file text not shown here. This note is not file content: read the file for its "
    "current content.)"
)
_TRIMMED_ARGUMENT: Final = (
    "[{chars} characters, removed to keep the conversation under its size limit.]"
)
# Text from a context note, compared without case and with each run of whitespace as one
# space. The first is in the dropped-result and argument notes and in the write note
# before Forge pack v4; the second is in the write note now.
_NOTE_MARKERS: Final = (
    "removed to keep the conversation under its size limit",
    "this note is not file content",
)
_NOTE_REFUSED: Final = (
    "Not written: this text holds the note that stands in for old file text, and that note "
    "is not file content. Read the file for its current content, then write or edit it "
    "again with the text it should hold."
)
_CONTEXT_FLOOR: Final = 0.75  # past the limit, remove down to this share of it
_EDIT_TEXTS: Final = ("oldText", "newText")
# Other calls whose arguments can be large: each listed value counts whole, a string by
# its length and anything else by its compact JSON, and is replaced by one note.
# forge_dryrun takes up to 8 replies of 2,000 characters, forge_spawn up to 64 authored
# cases of any size, and both are resent with every later turn.
_BULKY_ARGUMENTS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "forge_dryrun": ("replies",),
        "forge_spawn": ("goal", "input_schema", "output_schema", "cases"),
    }
)

# Where a bulky value sits in one tool call's arguments: keys and list indexes.
_ArgumentPath = tuple[str | int, ...]


def _argument_size(value: object) -> int:
    """A counted value's characters: a string's own length, anything else the length of
    its compact JSON (as ``JSON.stringify`` writes it in the extension)."""
    if isinstance(value, str):
        return len(value)
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError, RecursionError):
        return 0


def _bulky_strings(
    message: Mapping[str, object],
) -> list[tuple[int, _ArgumentPath, int, str]]:
    """``(part index, path, characters, note)`` of every bulky value an assistant
    message's calls carry, with the note that replaces it once trimmed.

    File texts (``_TRIMMED_WRITE``): a ``write`` call's ``content``, and an ``edit`` call's
    ``edits`` (a list of ``oldText``/``newText`` pairs, or the string some models send)
    and its legacy top-level ``oldText``/``newText``. Whole argument values
    (``_TRIMMED_ARGUMENT``): those ``_BULKY_ARGUMENTS`` lists for the Forge tools. The
    order is fixed: parts in order, then paths as listed.

    ``extension.ts`` finds the same values in the same order (``bulkyStrings``).
    """
    content = message.get("content")
    found: list[tuple[int, _ArgumentPath, int, str]] = []
    for index, part in enumerate(content if isinstance(content, list) else []):
        if not isinstance(part, dict) or part.get("type") != "toolCall":
            continue
        arguments = part.get("arguments")
        if not isinstance(arguments, dict):
            continue
        name = part.get("name")
        if name == "write":
            text = arguments.get("content")
            if isinstance(text, str):
                found.append((index, ("content",), len(text), _TRIMMED_WRITE))
        elif name == "edit":
            edits = arguments.get("edits")
            if isinstance(edits, str):
                found.append((index, ("edits",), len(edits), _TRIMMED_WRITE))
            elif isinstance(edits, list):
                for position, edit in enumerate(edits):
                    for key in _EDIT_TEXTS:
                        value = edit.get(key) if isinstance(edit, dict) else None
                        if isinstance(value, str):
                            found.append(
                                (index, ("edits", position, key), len(value), _TRIMMED_WRITE)
                            )
            for key in _EDIT_TEXTS:
                value = arguments.get(key)
                if isinstance(value, str):
                    found.append((index, (key,), len(value), _TRIMMED_WRITE))
        elif isinstance(name, str) and name in _BULKY_ARGUMENTS:
            for key in _BULKY_ARGUMENTS[name]:
                if key in arguments:
                    size = _argument_size(arguments[key])
                    found.append((index, (key,), size, _TRIMMED_ARGUMENT))
    return found


def _replaced(value: object, path: _ArgumentPath, note: str) -> object:
    """``value`` (a call's arguments) with the string at ``path`` replaced by ``note``; the
    containers on the way are copied, never changed in place."""
    if not path:
        return note
    head, rest = path[0], path[1:]
    if isinstance(head, str) and isinstance(value, dict) and head in value:
        return {**value, head: _replaced(value[head], rest, note)}
    if isinstance(head, int) and isinstance(value, list) and 0 <= head < len(value):
        return [*value[:head], _replaced(value[head], rest, note), *value[head + 1 :]]
    return value


def _carries_note(name: object, arguments: Mapping[str, object]) -> bool:
    """Whether a ``write`` or ``edit`` call's file text holds a context note
    (``_NOTE_MARKERS``): the texts ``_bulky_strings`` trims, a ``write``'s content and an
    ``edit``'s old and new texts in any of the shapes it takes."""
    texts: list[object] = []
    if name == "write":
        texts.append(arguments.get("content"))
    elif name == "edit":
        edits = arguments.get("edits")
        if isinstance(edits, str):
            texts.append(edits)  # as sent, and as the JSON list some models encode it in
            try:
                edits = json.loads(edits)
            except (ValueError, RecursionError):
                edits = None
        if isinstance(edits, list):
            for edit in edits:
                if isinstance(edit, dict):
                    texts += [edit.get(key) for key in _EDIT_TEXTS]
        texts += [arguments.get(key) for key in _EDIT_TEXTS]
    for text in texts:
        if isinstance(text, str):
            folded = " ".join(text.split()).casefold()
            if any(marker in folded for marker in _NOTE_MARKERS):
                return True
    return False


class _ContextCap:
    """``pack.context_policy.max_characters`` enforced by the parent, the same way each time.

    Counted: every tool-result body, every file text a ``write`` or ``edit`` call carries,
    and the bulky arguments of ``forge_dryrun`` and ``forge_spawn`` calls
    (``_bulky_strings``). Past the limit, the oldest of them are replaced with a short
    note, in message order, until the count is back under ``_CONTEXT_FLOOR`` of the
    limit: tool results are dropped and call values trimmed, oldest first. The latest
    assistant turn and its results are never touched, and an item no longer than its note
    is left alone. Pi's context only grows (compaction is off), so message positions are
    stable; a removal holds for the rest of the session, and the prompt prefix stays the
    same until the next one.

    The bridge's extension applies this same rule before it sends a context (``init``
    carries ``context_cap``), so the wire stays as small as what the model sees; here the
    rule is applied again to whatever arrives, so the limit holds either way.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.floor = int(limit * _CONTEXT_FLOOR)
        self.dropped: set[int] = set()  # tool-result message positions
        # (message position, part index, path in the call's arguments)
        self.trimmed: set[tuple[int, int, _ArgumentPath]] = set()

    def bridge_rule(self) -> dict[str, JsonValue]:
        """The rule as ``init`` sends it to the extension: the same numbers and notes."""
        return {
            "max_characters": self.limit,
            "floor": self.floor,
            "dropped": _DROPPED_RESULT,
            "trimmed": _TRIMMED_WRITE,
            "trimmed_argument": _TRIMMED_ARGUMENT,
        }

    def apply(self, pi_context: object) -> object:
        """The context as the model should see it now (``pi_context`` itself if untouched)."""
        if not isinstance(pi_context, dict) or not isinstance(pi_context.get("messages"), list):
            return pi_context
        messages: list[object] = pi_context["messages"]
        latest = max(
            (
                position
                for position, message in enumerate(messages)
                if isinstance(message, dict) and message.get("role") == "assistant"
            ),
            default=-1,
        )
        total = 0
        # (position, part or -1 for a tool result, path, size, note size), oldest first
        removable: list[tuple[int, int, _ArgumentPath, int, int]] = []
        for position, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            if message.get("role") == "toolResult":
                size = len(_text_parts(message.get("content"), "\n"))
                if position in self.dropped:
                    total += len(_DROPPED_RESULT)
                    continue
                total += size
                if position < latest and size > len(_DROPPED_RESULT):
                    removable.append((position, -1, (), size, len(_DROPPED_RESULT)))
            elif message.get("role") == "assistant":
                for index, path, size, template in _bulky_strings(message):
                    note = len(template.format(chars=size))
                    if (position, index, path) in self.trimmed:
                        total += note
                        continue
                    total += size
                    if position < latest and size > note:
                        removable.append((position, index, path, size, note))
        if total > self.limit:
            for position, index, path, size, note in removable:
                if total <= self.floor:
                    break
                if index < 0:
                    self.dropped.add(position)
                else:
                    self.trimmed.add((position, index, path))
                total -= size - note
        if not self.dropped and not self.trimmed:
            return pi_context
        return {
            **pi_context,
            "messages": [self._thin(position, item) for position, item in enumerate(messages)],
        }

    def _thin(self, position: int, message: object) -> object:
        if not isinstance(message, dict):
            return message
        if message.get("role") == "toolResult" and position in self.dropped:
            return {**message, "content": [{"type": "text", "text": _DROPPED_RESULT}]}
        content = message.get("content")
        if message.get("role") != "assistant" or not isinstance(content, list):
            return message
        trimmed = [
            (index, path, template.format(chars=size))
            for index, path, size, template in _bulky_strings(message)
            if (position, index, path) in self.trimmed
        ]
        if not trimmed:
            return message
        parts = list(content)
        for index, path, note in trimmed:
            part = parts[index]
            assert isinstance(part, dict)
            parts[index] = {**part, "arguments": _replaced(part.get("arguments"), path, note)}
        return {**message, "content": parts}


# Every fixed text the parent shows a model in a Pi session. Forge's pack hash covers them
# (``architect.compiler.forge_pack``): changing one changes what Forge's engineer reads.
PARENT_TEXTS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "native": _NATIVE_INSTRUCTION,
        "native_no_tools": _NATIVE_NO_TOOLS,
        "native_bad_arguments": _BAD_ARGUMENTS,
        "native_not_run": _NOT_RUN,
        "native_unanswered": _UNANSWERED,
        "envelope": _PROTOCOL_INSTRUCTION,
        "envelope_no_tools": _NO_TOOLS_INSTRUCTION,
        "envelope_repair": _REPAIR_MESSAGE,
        "envelope_reminder": _REMINDER,
        "cut": _CUT_REPLY,
        "cut_envelope": _CUT_CORRECTION,
        "unfinished": _UNFINISHED,
        "unfinished_envelope": _UNFINISHED_CORRECTION,
        "unfinished_again": _UNFINISHED_AGAIN,
        "unfinished_again_envelope": _UNFINISHED_AGAIN_CORRECTION,
        "context_dropped": _DROPPED_RESULT,
        "context_trimmed": _TRIMMED_WRITE,
        "context_trimmed_argument": _TRIMMED_ARGUMENT,
        "context_note_refused": _NOTE_REFUSED,
        "read_missing": _READ_MISSING,
        "read_directory": _READ_DIRECTORY,
        "read_root": _READ_ROOT,
        "read_here": _READ_HERE,
        "read_holds": _READ_HOLDS,
        "read_empty": _READ_EMPTY,
        "read_more": _READ_MORE,
        "read_rest": _READ_REST,
    }
)


async def _invoke_capability(
    tool: TrustedTool,
    validator: Draft202012Validator,
    arguments: object,
    calls: dict[str, int],
    protocol_room: int,
) -> JsonValue:
    """Run one granted capability (contract 7, steps 3-6); soft failures carry codes only."""
    from architect.runtime import BudgetExceeded

    if not isinstance(arguments, dict) or not validator.is_valid(arguments):
        return _soft("invalid_arguments")
    calls[tool.name] = calls.get(tool.name, 0) + 1
    if calls[tool.name] > tool.max_calls:
        return _soft("forge_tool_cap")
    try:
        result = await tool.callback(cast(dict[str, JsonValue], arguments))
    except (BudgetExceeded, asyncio.CancelledError):
        raise
    except Exception:  # noqa: BLE001 - exception text never reaches the agent
        return _soft("tool_failed")
    try:
        text = json.dumps(result, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return _soft("tool_failed")
    # A raw oversized result is never written, so the wire limit can never trip (step 6).
    if len(text) > protocol_room:
        return _soft("result_too_large")
    if len(text) > tool.max_result_chars and not (
        # The callback's own truncation is trusted, bounded above by the wire limit.
        isinstance(result, dict) and result.get("truncated") is True
    ):
        return _soft("result_too_large")
    return cast(JsonValue, json.loads(text))


class PiBackend:
    def __init__(
        self,
        config: PiConfig | None = None,
        *,
        compiler_tool: Callable[[dict[str, JsonValue]], Awaitable[JsonValue]] | None = None,
        capabilities: Mapping[str, TrustedTool] = MappingProxyType({}),
        approver: Approver | None = None,
        on_event: EventSink | None = None,
    ) -> None:
        """``approver`` and ``on_event`` are the Phase 12 coder hooks (default: none).

        ``approver(tool, arguments)`` is consulted for ``write``, ``edit`` and ``bash``
        only after every existing guard (grant, shell grant, ``_granted_path``) allowed
        the request; anything but ``True`` (including an exception) denies it exactly as
        a guard denial. It can only narrow what the guards allow, never widen it.
        ``on_event(event)`` receives sanitized, bounded informational events (see
        ``_event``); the bridge sends them only when this hook is set, they are never
        replied to, and an exception in the hook is ignored.
        """
        self.config = config or PiConfig()
        self.compiler_tool = compiler_tool
        self.approver = approver
        self.on_event = on_event
        granted = dict(capabilities)
        allowed = set(self.config.allowed_tools)
        if (
            any(
                not isinstance(key, str) or not isinstance(tool, TrustedTool) or tool.name != key
                for key, tool in granted.items()
            )
            or set(granted) - allowed
            or set(granted) & _BUILTIN_TOOLS
            or (allowed & FORGE_TOOL_NAMES) - set(granted)
        ):
            raise ValueError("Unsupported or duplicate Pi tools")
        validators: dict[str, Draft202012Validator] = {}
        for name, tool in granted.items():
            try:
                Draft202012Validator.check_schema(tool.parameters)
            except SchemaError:
                raise ValueError("Invalid trusted tool") from None
            validators[name] = Draft202012Validator(tool.parameters)
        self.capabilities: Mapping[str, TrustedTool] = MappingProxyType(granted)
        self._validators: Mapping[str, Draft202012Validator] = MappingProxyType(validators)
        # The nested compiler is the one pre-Phase-9 trusted tool. It keeps its own
        # ``compile`` RPC and gate (not a build capability: requires_build=False).
        self.compile_agent: TrustedTool | None = (
            None
            if compiler_tool is None
            else TrustedTool(
                name="compile_agent",
                description=_COMPILE_AGENT_DESCRIPTION,
                parameters=_COMPILE_AGENT_SCHEMA,
                callback=compiler_tool,
                max_calls=self.config.max_turns,
                requires_build=False,
            )
        )

    async def _approve(self, name: str, arguments: dict[str, Any]) -> bool:
        """The approver's answer for one guarded-allowed request; only ``True`` allows."""
        from architect.runtime import BudgetExceeded

        assert self.approver is not None
        try:
            # A private copy: the hook can neither see nor change what Pi executes.
            answer = await self.approver(name, cast(dict[str, JsonValue], deepcopy(arguments)))
        except BudgetExceeded:
            raise
        except Exception:  # noqa: BLE001 - an unclear answer is a denial
            return False
        return answer is True

    @property
    def last_evidence(self) -> PiEvidence | None:
        entry = _EVIDENCE.get()
        return entry[1] if entry is not None and entry[0] == id(self) else None

    async def run(self, context: RunContext, task: JsonValue, pack: dict[str, JsonValue]) -> str:
        """Run the SDK loop; model text never grants tools, budgets or a repository."""
        _EVIDENCE.set(None)
        _check_parent(context)
        if not (_NODE / "node_modules/@mariozechner/pi-coding-agent/package.json").is_file():
            raise PiError("Pi dependencies missing; run npm ci in architect/backends/pi-node")
        requested = pack.get("tools", [])
        if not isinstance(requested, list) or any(not isinstance(item, str) for item in requested):
            raise PiError("Invalid bound Pi tools")
        tools = sorted(set(cast(list[str], requested)) & set(self.config.allowed_tools))
        if not self.config.allow_shell and "bash" in tools:
            tools.remove("bash")
        compile_tool = self.compile_agent
        if (
            compile_tool is None or context.is_evaluation or not pack.get("subagent_rules")
        ) and "compile_agent" in tools:
            tools.remove("compile_agent")
        # Build capabilities exist only in a build context (``Runtime.build``).
        # ``Runtime.run``/``evaluate`` never set it, so play time never sees them.
        is_build = getattr(context, "is_build", False) is True
        granted = {
            name: tool
            for name, tool in self.capabilities.items()
            if name in tools and (is_build if tool.requires_build else not context.is_evaluation)
        }
        tools = [name for name in tools if name not in self.capabilities or name in granted]
        capability_calls: dict[str, int] = {}
        # Normal requests use the parent provider capacity, not a factory token quota.
        # Explicit legacy positive limits remain low-level provider request options.
        max_output_tokens = pack.get("max_output_tokens")
        if max_output_tokens is not None and (
            type(max_output_tokens) is not int or max_output_tokens < 1
        ):
            raise PiError("Invalid Pi provider output option")
        # Native tool calls whenever the bound adapter streams them; else the JSON envelope.
        native = isinstance(context.adapter, ChatStreaming)
        # With a parent-side cap the bridge gets ``_ContextCap``'s rule in place of the
        # policy (``init.context_cap``), so its own slicing never runs.
        policy = pack.get("context_policy")
        limit = policy.get("max_characters") if isinstance(policy, dict) else None
        parent_cap = self.config.context_cap == "parent"
        cap = _ContextCap(limit) if parent_cap and type(limit) is int and limit >= 1 else None
        bridge_pack = (
            {key: value for key, value in pack.items() if key != "context_policy"}
            if parent_cap
            else pack
        )
        required = self.config.final_requires
        with tempfile.TemporaryDirectory(prefix="architect-pi-") as scratch:
            home = Path(scratch)
            repo = context.repository
            if repo is None:
                repo = home / "repo"
                repo.mkdir()
            repo = repo.resolve(strict=True)
            if not repo.is_dir():
                raise PiError("Pi repository must be a directory")
            executable = _resolve_node(self.config.node_executable, repo)
            # Resolve only paths, never signing-key bytes. Coding grants never
            # include active host enforcement code or interpreter installations, the
            # Ed25519 private key or the trust list (a written trust list would let a
            # model trust its own key).
            protected = (
                *self.config.denied_paths,
                Path(os.environ.get("ARCHITECT_SIGNING_KEY_FILE", ".architect/signing.key")),
                *trust.protected_paths(),
                repo / ".architect",
                _PACKAGE,
                Path(sys.prefix),
                Path(sys.base_prefix),
                executable,
            )
            denied_paths = tuple(path.resolve() for path in protected)
            env = {
                key: value
                for key, value in os.environ.items()
                if key.upper()
                in {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE"}
            }
            env.update(
                {
                    "HOME": scratch,
                    "USERPROFILE": scratch,
                    "TMP": scratch,
                    "TEMP": scratch,
                    "TMPDIR": scratch,
                    "PI_CODING_AGENT_DIR": scratch,
                    "XDG_CONFIG_HOME": scratch,
                    "APPDATA": scratch,
                    "LOCALAPPDATA": scratch,
                    "NO_COLOR": "1",
                }
            )
            if os.environ.get("ARCHITECT_NODE"):
                # Electron runs as Node only with this set; bridge.mjs unsets it for tools.
                env["ELECTRON_RUN_AS_NODE"] = "1"
            try:
                process = await asyncio.create_subprocess_exec(
                    str(executable),
                    str(_NODE / "bridge.mjs"),
                    cwd=repo,
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=self.config.max_protocol_bytes + 1,
                    start_new_session=os.name != "nt",
                )
            except OSError as exc:
                raise PiError("Cannot start Pi Node process") from exc
            job: int | None = None
            model_calls = 0
            tool_calls = 0
            re_asked = 0
            # ``final_requires``: the required capability's call count at the last re-ask.
            nudged_at: int | None = None
            call_ids: set[str] = set()  # native: every tool call id handed to Pi so far
            versions: dict[str, str] = {}
            loaded = False
            assert process.stdin and process.stdout and process.stderr

            def unfinished() -> bool:
                """A final answer now would end the session while its required capability
                (``final_requires``) can still be called, and no re-ask was spent since that
                capability's last call: one before its first call, then one after each."""
                if required is None or required not in granted:
                    return False
                calls = capability_calls.get(required, 0)
                return calls < granted[required].max_calls and calls != nudged_at

            def nudge() -> tuple[str, bool]:
                """Spend the re-ask ``unfinished`` allowed: (the capability, called before)."""
                nonlocal nudged_at, re_asked
                assert required is not None
                nudged_at = capability_calls.get(required, 0)
                re_asked += 1
                return required, nudged_at > 0

            async def native_reply(pi_context: object) -> str:
                """One parent turn as a native chat call; returns the bridge's envelope.

                A reply whose tool arguments are not a JSON object is re-asked once, with
                fixed errors in place of those arguments, on the same turn limit and
                wallets. A reply cut at the output limit runs none of its calls: it is
                re-asked once with ``_CUT_REPLY`` alone, never shown again. Its last call
                may be cut inside its arguments, and those can still parse: a server ends
                every reply by sending the rest of its tool parser's partial parse (vLLM),
                and a call cut right after its name parses as ``{}``. Still malformed or
                cut, its text is the final answer, as after the envelope's bounded
                repairs: nothing the model did not send ever runs. A final answer that
                ``unfinished`` stops is re-asked as it allows.
                """
                nonlocal model_calls, re_asked
                specs = _native_tools(pi_context, tools)
                system = _native_system(pi_context, bool(specs))
                messages = _native_messages(pi_context)
                if not messages:
                    raise PiError("Invalid Pi model context")
                attempt = 0
                while True:
                    model_calls += 1
                    if model_calls > self.config.max_turns:
                        raise PiError("Pi turn limit exceeded")
                    # Reserved before the stream opens, reconciled with the reported usage.
                    result = await context.stream_call(
                        system, messages, specs, max_output_tokens
                    )
                    _check_parent(context)
                    if len(result.tool_calls) > self.config.max_turns:
                        raise PiError("Pi tool limit exceeded")
                    cut = result.stop_reason == "max_tokens"
                    # A cut call's arguments can parse yet lack what the model was still
                    # writing ({} for forge_measure measures its default cases), so nothing
                    # from a cut reply runs.
                    malformed = any(_malformed(call) for call in result.tool_calls)
                    if cut or malformed:
                        if attempt >= _NATIVE_REPAIRS:
                            return _compact({"text": result.text})
                        attempt += 1
                        re_asked += 1
                        retry = _native_cut() if cut else _native_repair(result, call_ids)
                        messages = [*messages, *retry]
                        continue
                    if not result.tool_calls and unfinished():
                        tool, called = nudge()
                        messages = [*messages, *_native_unfinished(result.text, tool, called)]
                        continue
                    return _native_envelope(result, call_ids)

            async def write(message: dict[str, Any]) -> None:
                data = (json.dumps(message, ensure_ascii=True, allow_nan=False) + "\n").encode()
                if len(data) > self.config.max_protocol_bytes:
                    raise PiError("Pi protocol limit exceeded")
                assert process.stdin
                try:
                    process.stdin.write(data)
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    raise PiError("Pi exited before completion") from None

            async def drain_errors() -> None:
                assert process.stderr
                total = 0
                while chunk := await process.stderr.read(8192):
                    total += len(chunk)
                    if total > self.config.max_protocol_bytes:
                        raise PiError("Pi stderr limit exceeded")
                # Deliberately discard content-bearing diagnostics.

            async def conversation() -> str:
                nonlocal model_calls, tool_calls, versions, loaded, re_asked
                capability_total = 0
                used_tools = False
                await write(
                    {
                        "kind": "init",
                        "repo": str(repo),
                        "agent_dir": scratch,
                        "tools": tools,
                        "capabilities": [
                            {
                                "name": name,
                                "description": granted[name].description,
                                "parameters": granted[name].parameters,
                            }
                            for name in sorted(granted)
                        ],
                        "task": task,
                        "pack": bridge_pack,
                        **self.config.model_dump(
                            exclude={
                                "allowed_tools",
                                "node_executable",
                                "denied_paths",
                                "repair_prose",
                                "final_requires",
                                "context_cap",
                            }
                        ),
                        "denied_paths": [str(path) for path in denied_paths],
                        # Only with the hook: default init messages stay byte-identical.
                        **({"events": True} if self.on_event is not None else {}),
                        # The parent-side cap's rule, so the extension thins the context
                        # the same way before sending it: the wire stays as small as what
                        # the model sees. Without a parent cap the message is unchanged.
                        **({"context_cap": cap.bridge_rule()} if cap is not None else {}),
                    }
                )
                previous_id = 0
                forwarded = 0
                while True:
                    _check_parent(context)
                    assert process.stdout
                    try:
                        line = await process.stdout.readline()
                        if not line or len(line) > self.config.max_protocol_bytes:
                            raise PiError("Pi protocol ended or exceeded limit")
                        message = json.loads(line)
                    except (ValueError, UnicodeError) as exc:
                        raise PiError("Invalid Pi protocol") from exc
                    if not isinstance(message, dict):
                        raise PiError("Invalid Pi message")
                    kind = message.get("kind")
                    if kind == "ready":
                        versions = message.get("versions", {})
                        loaded = message.get("extension_loaded") is True
                        if not loaded or versions != {
                            name: "0.73.1" for name in ("pi-ai", "pi-agent-core", "pi-coding-agent")
                        }:
                            raise PiError("Pi version or extension mismatch")
                        continue
                    if kind == "done":
                        if not loaded or not isinstance(message.get("text"), str):
                            raise PiError("Invalid Pi completion")
                        return cast(str, message["text"])
                    if kind == "failed":
                        raise PiError("Pi execution failed")
                    if kind == "event" and self.on_event is not None:
                        # One-way and informational: no reply, no request id, no effect
                        # on ordering. Before ``ready``, past the cap or of an unknown
                        # shape, it is dropped. Without the hook it stays unknown.
                        event = _event(message) if loaded and forwarded < _MAX_EVENTS else None
                        if event is not None:
                            forwarded += 1
                            try:
                                self.on_event(event)
                            except Exception:  # noqa: BLE001 - a display failure never stops Pi
                                pass
                        continue
                    request_id = message.get("id")
                    if type(request_id) is not int or request_id <= previous_id:
                        raise PiError("Invalid Pi request identity")
                    previous_id = request_id
                    value: JsonValue
                    pi_context = message.get("context")
                    if kind == "model" and cap is not None:
                        pi_context = cap.apply(pi_context)
                    if kind == "model" and native:
                        value = await native_reply(pi_context)
                    elif kind == "model":
                        listing = _tool_listing(pi_context, tools)
                        names = [cast(str, entry["name"]) for entry in listing]
                        request: dict[str, JsonValue] = {
                            "instruction": (
                                _PROTOCOL_INSTRUCTION if listing else _NO_TOOLS_INSTRUCTION
                            ),
                            "tools": cast(JsonValue, listing),
                            "pi_context": _model_view(pi_context),
                        }
                        if listing:
                            request["reminder"] = _REMINDER
                        repairs = 0
                        while True:
                            model_calls += 1
                            if model_calls > self.config.max_turns:
                                raise PiError("Pi turn limit exceeded")
                            # The byte-stable prefix is shared by every call in a session;
                            # a repair only appends the bounded reply and a fixed message.
                            prompt = json.dumps(request, ensure_ascii=True)
                            # Reconcile parent usage before returning assistant/tool events.
                            before = context.tokens_out
                            reply = await context.call(prompt, max_output_tokens=max_output_tokens)
                            _check_parent(context)
                            # Settled at the reported usage: a reply that used the whole
                            # explicit output cap stopped at it.
                            cut = (
                                max_output_tokens is not None
                                and context.tokens_out - before >= max_output_tokens
                            )
                            control = _control_reply(reply, self.config.max_turns)
                            if control is not None:
                                value, calls_tools = control
                                if not calls_tools and unfinished():
                                    # A final answer the required capability stops: re-asked
                                    # as ``unfinished`` allows, without echoing the reply.
                                    tool, called = nudge()
                                    request.pop("reminder", None)
                                    request.pop("previous_reply", None)
                                    request["correction"] = _unfinished_correction(tool, called)
                                    continue
                                used_tools = used_tools or calls_tools
                                break
                            # A missed tool call (or, with ``repair_prose``, any
                            # non-envelope reply) is re-asked, at most twice, on the
                            # same turn limit and wallets. Anything else, or a reply
                            # after the bounded repairs, is the final answer.
                            if (
                                repairs >= _MAX_REPAIRS
                                or not names
                                or not (
                                    self.config.repair_prose
                                    or _tool_attempt(reply, names, used_tools=used_tools)
                                )
                            ):
                                value = json.dumps({"text": reply}, ensure_ascii=True)
                                break
                            repairs += 1
                            re_asked += 1
                            request.pop("reminder", None)
                            if cut:
                                # Never echoed back: one short instruction instead.
                                request.pop("previous_reply", None)
                                request["correction"] = _CUT_CORRECTION
                            else:
                                request["previous_reply"] = reply[:_REPLY_ECHO_CHARS]
                                request["correction"] = _REPAIR_MESSAGE
                    elif kind == "tool":
                        # A sibling can exhaust a shared wallet while readline waits.
                        _check_parent(context)
                        tool_calls += 1
                        if tool_calls > self.config.max_turns:
                            raise PiError("Pi tool limit exceeded")
                        name = message.get("name")
                        args = message.get("arguments")
                        value = name in tools and isinstance(args, dict)
                        if value and isinstance(args, dict) and name in {"read", "write", "edit"}:
                            value = _granted_path(
                                repo,
                                args.get("path"),
                                name,
                                self.config.max_protocol_bytes,
                                denied_paths,
                            )
                            if value is False and name == "read":
                                # Still refused, but a missing file or a directory says so
                                # (with what is there) instead of the policy text.
                                value = _read_refusal(repo, args.get("path"), denied_paths) or False
                        if (
                            value is True
                            and cap is not None
                            and isinstance(args, dict)
                            and _carries_note(name, args)
                        ):
                            # The context cap's note in place of file text: never written.
                            value = _NOTE_REFUSED
                        if value is True and self.approver is not None and name in APPROVED_TOOLS:
                            assert isinstance(name, str) and isinstance(args, dict)
                            value = await self._approve(name, args)
                            # Waiting on a person can outlast a sibling's wallet.
                            _check_parent(context)
                    elif kind == "compile":
                        _check_parent(context)
                        if "compile_agent" not in tools or context.is_evaluation:
                            raise PiError("Compiler capability denied")
                        args = message.get("arguments")
                        if (
                            not isinstance(args, dict)
                            or set(args) != {"request"}
                            or not isinstance(args["request"], str)
                        ):
                            raise PiError("Compiler capability arguments denied")
                        assert compile_tool is not None
                        value = await compile_tool.callback(args)
                        _check_parent(context)
                    elif kind == "capability":
                        _check_parent(context)
                        name = message.get("name")
                        capability = granted.get(name) if isinstance(name, str) else None
                        if capability is None:
                            # Only a forged peer can name an ungranted capability.
                            raise PiError("Capability denied")
                        # Capability calls count toward max_turns like tool approvals.
                        capability_total += 1
                        if capability_total > self.config.max_turns:
                            raise PiError("Pi tool limit exceeded")
                        value = await _invoke_capability(
                            capability,
                            self._validators[capability.name],
                            message.get("arguments"),
                            capability_calls,
                            self.config.max_protocol_bytes - _REPLY_ENVELOPE,
                        )
                        _check_parent(context)
                    else:
                        raise PiError("Unknown Pi request")
                    await write({"id": request_id, "value": value})

            async def guarded() -> str:
                conversation_task = asyncio.create_task(conversation())
                stderr_task = asyncio.create_task(drain_errors())
                try:
                    done, _ = await asyncio.wait(
                        {conversation_task, stderr_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if stderr_task in done:
                        await stderr_task
                        if not conversation_task.done():
                            raise PiError("Pi exited before completion")
                    return await conversation_task
                finally:
                    conversation_task.cancel()
                    stderr_task.cancel()
                    await asyncio.gather(conversation_task, stderr_task, return_exceptions=True)

            async def cleanup(success: bool) -> None:
                nonlocal job
                if os.name != "nt":
                    # Always, even after the bridge exited: session ids still reach
                    # re-grouped work. Parent links are only walked while it lives.
                    alive = process.returncode is None
                    try:
                        await asyncio.to_thread(_reap_posix_tree, process.pid, root_alive=alive)
                    except Exception:  # noqa: BLE001 - the group kill below still runs
                        pass
                if success:
                    assert process.stdin
                    process.stdin.close()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=2)
                    except TimeoutError:
                        pass
                if os.name == "nt":
                    if job is not None:
                        _close_job(job)
                        job = None
                    elif process.returncode is None:
                        # Job assignment failed before init: no tools can have spawned.
                        process.kill()
                else:
                    try:
                        await asyncio.to_thread(_kill_posix_group, process.pid)
                    finally:
                        if process.returncode is None:
                            process.kill()
                        await process.wait()
                    return
                await process.wait()

            success = False
            try:
                job = _windows_job(process.pid)
                result = await context._within_deadline(guarded())
                _check_parent(context)
                success = True
                return result
            finally:
                cleanup_task = asyncio.create_task(cleanup(success))
                try:
                    try:
                        await asyncio.shield(cleanup_task)
                    except asyncio.CancelledError:
                        await asyncio.gather(cleanup_task, return_exceptions=True)
                        raise
                except Exception:
                    # A cleanup failure never masks the run's own error.
                    if success:
                        raise
                finally:
                    _EVIDENCE.set(
                        (
                            id(self),
                            PiEvidence(
                                pid=process.pid,
                                exit_code=process.returncode or 0,
                                extension_loaded=loaded,
                                model_calls=model_calls,
                                tool_calls=tool_calls,
                                versions=versions,
                                transport="native" if native else "envelope",
                                repairs=re_asked,
                            ),
                        )
                    )
