"""Per-turn checkpoints of the coder's file changes (Phase 13, Section 5 item 4).

Before a coder run, ``capture`` reads the working directory (bounded: ``SNAPSHOT_FILES``
files, ``FILE_BYTES`` each, ``TOTAL_BYTES`` in all; the same directories the coder's own
snapshot skips). After the run, every file whose content changed, plus whatever the coder
reported in ``changed_files``, is recorded with its pre-run content under
``<workspace>/checkpoints/<session>/<turn>/``: a ``manifest.json`` and one blob per file.
Several coder runs in one turn merge into one checkpoint; the earliest content wins.

``/undo`` restores the newest checkpoint that is not undone yet (files that did not exist
before are deleted; files too large to snapshot are reported, never guessed). ``/diff``
outside a git repository shows the session's changes against the earliest checkpoints.

The binding is a wrapper around the coder (``checkpointed``) installed in
``ctx.extras["coder"]`` by the app, so the coder module itself is unchanged. The store and
the current turn come from ``ctx.extras["checkpoints"]`` and ``ctx.extras["checkpoint_key"]``
(``(session_id, turn)``), which the REPL keeps current.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

from pydantic import JsonValue

from .tools import SKIP_DIRS, ToolContext

__all__ = [
    "FILE_BYTES",
    "SNAPSHOT_FILES",
    "TOTAL_BYTES",
    "Checkpoint",
    "CheckpointStore",
    "FileRecord",
    "Restored",
    "Snapshot",
    "capture",
    "changed_since",
    "checkpointed",
]

SNAPSHOT_FILES = 5000
FILE_BYTES = 2 * 1024 * 1024
TOTAL_BYTES = 64 * 1024 * 1024
DIFF_LINES_PER_FILE = 400
MANIFEST = "manifest.json"
_SAFE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
State = Literal["file", "absent", "unsaved"]
Coder = Callable[[str, ToolContext], Awaitable[JsonValue]]


@dataclass
class Snapshot:
    """The working directory before a coder run."""

    root: Path
    content: dict[str, bytes] = field(default_factory=dict)
    stats: dict[str, tuple[int, int]] = field(default_factory=dict)  # every walked file
    complete: bool = True  # False when the file limit cut the walk short

    def before(self, rel: str) -> tuple[State, bytes | None]:
        if rel in self.content:
            return "file", self.content[rel]
        if rel in self.stats or not self.complete:
            return "unsaved", None  # existed but too large, or beyond the walk
        return "absent", None


def _walk(root: Path) -> tuple[dict[str, tuple[int, int]], bool]:
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
                return found, False
    return found, True


def capture(root: Path) -> Snapshot:
    """Read the working directory within the limits (blocking; run it in a thread)."""
    root = root.resolve()
    stats, complete = _walk(root)
    snapshot = Snapshot(root, stats=stats, complete=complete)
    total = 0
    for rel, (_, size) in stats.items():
        if size > FILE_BYTES or total + size > TOTAL_BYTES:
            continue
        try:
            data = (root / rel).read_bytes()
        except OSError:
            continue
        snapshot.content[rel] = data
        total += len(data)
    return snapshot


def changed_since(snapshot: Snapshot) -> set[str]:
    """Files created, deleted or modified since ``snapshot`` (blocking)."""
    now, _ = _walk(snapshot.root)
    changed: set[str] = set()
    for rel in snapshot.stats.keys() | now.keys():
        before, after = snapshot.stats.get(rel), now.get(rel)
        if before == after:
            continue
        if before is not None and after is not None and rel in snapshot.content:
            try:
                if (snapshot.root / rel).read_bytes() == snapshot.content[rel]:
                    continue  # touched, same bytes
            except OSError:
                pass
        changed.add(rel)
    return changed


@dataclass
class FileRecord:
    path: str
    state: State
    blob: str | None = None

    def to_json(self) -> dict[str, JsonValue]:
        return {"path": self.path, "state": self.state, "blob": self.blob}


@dataclass
class Checkpoint:
    directory: Path
    session: str
    turn: int
    time: float
    request: str
    files: list[FileRecord] = field(default_factory=list)
    undone: bool = False

    def save(self) -> None:
        payload = {
            "session": self.session,
            "turn": self.turn,
            "time": self.time,
            "request": self.request,
            "undone": self.undone,
            "files": [record.to_json() for record in self.files],
        }
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.directory / (MANIFEST + ".tmp")
        temporary.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        os.replace(temporary, self.directory / MANIFEST)

    @classmethod
    def load(cls, directory: Path) -> Checkpoint:
        data = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("invalid_checkpoint")
        files = []
        for item in data.get("files") or []:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            state = item.get("state")
            if state not in ("file", "absent", "unsaved"):
                continue
            blob = item.get("blob")
            files.append(FileRecord(item["path"], cast(State, state),
                                    blob if isinstance(blob, str) else None))
        return cls(
            directory=directory,
            session=str(data.get("session", "")),
            turn=int(data.get("turn", 0)),
            time=float(data.get("time", 0.0)),
            request=str(data.get("request", "")),
            files=files,
            undone=data.get("undone") is True,
        )

    def blob(self, record: FileRecord) -> bytes | None:
        if record.blob is None:
            return None
        return (self.directory / "files" / record.blob).read_bytes()

    def when(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.time))


@dataclass
class Restored:
    restored: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


class CheckpointStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _session_dir(self, session: str) -> Path:
        if not _SAFE.match(session):
            raise ValueError("invalid_session_id")
        return self.root / session

    def list(self, session: str) -> list[Checkpoint]:
        directory = self._session_dir(session)
        if not directory.is_dir():
            return []
        found = []
        for child in directory.iterdir():
            if child.is_dir() and (child / MANIFEST).is_file():
                try:
                    found.append(Checkpoint.load(child))
                except (OSError, ValueError, TypeError):
                    continue
        return sorted(found, key=lambda cp: (cp.turn, cp.time))

    def next_turn(self, session: str) -> int:
        existing = self.list(session)
        return (existing[-1].turn + 1) if existing else 1

    def last(self, session: str) -> Checkpoint | None:
        """The newest checkpoint with files that has not been undone."""
        for checkpoint in reversed(self.list(session)):
            if not checkpoint.undone and checkpoint.files:
                return checkpoint
        return None

    def record(
        self, session: str, turn: int, snapshot: Snapshot, changed: set[str], request: str = ""
    ) -> Checkpoint | None:
        """Store the pre-run content of ``changed``; merge into this turn's checkpoint."""
        if not changed:
            return None
        directory = self._session_dir(session) / f"{turn:05d}"
        try:
            checkpoint = Checkpoint.load(directory)
        except (OSError, ValueError, TypeError):
            checkpoint = Checkpoint(directory, session, turn, time.time(), request[:200])
        known = {record.path for record in checkpoint.files}
        blobs = directory / "files"
        for rel in sorted(changed):
            if rel in known:
                continue  # an earlier run in this turn already holds the older content
            state, data = snapshot.before(rel)
            record = FileRecord(rel, state)
            if data is not None:
                blobs.mkdir(parents=True, exist_ok=True)
                record.blob = f"{len(checkpoint.files):05d}.bin"
                (blobs / record.blob).write_bytes(data)
            checkpoint.files.append(record)
        checkpoint.save()
        return checkpoint

    def restore(self, checkpoint: Checkpoint, cwd: Path) -> Restored:
        """Put the files back as they were before the checkpoint's turn."""
        root = cwd.resolve()
        done = Restored()
        for record in checkpoint.files:
            target = (root / record.path).resolve()
            if target == root or root not in target.parents:
                done.skipped.append(record.path)
                continue
            try:
                if record.state == "absent":
                    if target.is_file():
                        target.unlink()
                        done.deleted.append(record.path)
                    continue
                data = checkpoint.blob(record)
                if record.state != "file" or data is None:
                    done.skipped.append(record.path)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                done.restored.append(record.path)
            except OSError:
                done.skipped.append(record.path)
        checkpoint.undone = True
        checkpoint.save()
        return done

    def diff(self, session: str, cwd: Path) -> str:
        """Unified diff of every file the session's coder changed, earliest state vs now."""
        root = cwd.resolve()
        earliest: dict[str, tuple[Checkpoint, FileRecord]] = {}
        for checkpoint in self.list(session):
            if checkpoint.undone:
                continue
            for record in checkpoint.files:
                earliest.setdefault(record.path, (checkpoint, record))
        parts: list[str] = []
        for rel in sorted(earliest):
            checkpoint, record = earliest[rel]
            path = root / rel
            try:
                before = checkpoint.blob(record) if record.state == "file" else b""
                after = path.read_bytes() if path.is_file() else b""
            except OSError:
                continue
            if record.state == "unsaved":
                parts.append(f"{rel}: changed (too large to snapshot; no diff)\n")
                continue
            if before == after or before is None:
                continue
            if b"\0" in before[:4096] or b"\0" in after[:4096]:
                parts.append(f"Binary file {rel} changed\n")
                continue
            old = before.decode("utf-8", errors="replace").splitlines(keepends=True)
            new = after.decode("utf-8", errors="replace").splitlines(keepends=True)
            label_old = "/dev/null" if record.state == "absent" else f"a/{rel}"
            label_new = f"b/{rel}" if path.is_file() else "/dev/null"
            lines = list(difflib.unified_diff(old, new, label_old, label_new))
            if len(lines) > DIFF_LINES_PER_FILE:
                more = len(lines) - DIFF_LINES_PER_FILE
                lines = lines[:DIFF_LINES_PER_FILE] + [f"... {more} more diff lines\n"]
            parts.append("".join(line if line.endswith("\n") else line + "\n" for line in lines))
        return "".join(parts)


def checkpointed(coder: Coder) -> Coder:
    """Wrap the coder so each run's file changes are checkpointed for ``/undo``."""

    async def run(request: str, ctx: ToolContext) -> JsonValue:
        store = ctx.extras.get("checkpoints")
        if not isinstance(store, CheckpointStore):
            return await coder(request, ctx)
        key = ctx.extras.get("checkpoint_key")
        if (isinstance(key, tuple) and len(key) == 2 and isinstance(key[0], str)
                and isinstance(key[1], int)):
            session, turn = key
        else:
            session, turn = "default", 0
        try:
            if turn <= 0:
                turn = store.next_turn(session)
            snapshot = await asyncio.to_thread(capture, ctx.cwd)
        except (OSError, ValueError):
            return await coder(request, ctx)  # no checkpoint is better than no coder
        result: JsonValue = None
        cancelled = False
        try:
            result = await coder(request, ctx)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            reported: set[str] = set()
            if isinstance(result, dict) and isinstance(result.get("changed_files"), list):
                reported = {str(p) for p in result["changed_files"]}  # type: ignore[union-attr]
            try:
                # On cancellation the walk runs inline: awaiting here could be cut short.
                found = (changed_since(snapshot) if cancelled
                         else await asyncio.to_thread(changed_since, snapshot))
                store.record(session, turn, snapshot, found | reported, request)
            except (OSError, ValueError):
                pass
        return result

    run.__name__ = getattr(coder, "__name__", "run_coder")
    return run
