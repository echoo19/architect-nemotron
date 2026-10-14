"""Saved conversations (Phase 12, Section 9).

Each conversation is ``<workspace>/sessions/<id>.jsonl`` (the transcript, tool results
already clipped) plus one row in ``<workspace>/sessions/index.json``: id, title (the first
line of the first user message, or the name given with ``/title``), last save time,
working directory and message count. Writes are atomic; a damaged index is rebuilt from the
files that are there.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .transcript import Transcript

__all__ = ["SessionInfo", "SessionStore", "clean_title", "title_of"]

INDEX = "index.json"
TITLE_CHARS = 60
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class SessionInfo:
    id: str
    title: str
    time: float
    cwd: str
    messages: int = 0
    named: bool = False  # the title was set with /title, not taken from the first message

    def when(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.time))


def title_of(transcript: Transcript) -> str:
    for message in transcript.messages:
        if message.role == "user" and message.text.strip():
            line = message.text.strip().splitlines()[0].strip()
            return line if len(line) <= TITLE_CHARS else line[: TITLE_CHARS - 3] + "..."
    return "(empty)"


def clean_title(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= TITLE_CHARS else line[: TITLE_CHARS - 3] + "..."


def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


class SessionStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, session_id: str) -> Path:
        if not _ID.match(session_id):
            raise ValueError("invalid_session_id")
        return self.root / f"{session_id}.jsonl"

    # -- index ----------------------------------------------------------------------

    def _read_index(self) -> dict[str, SessionInfo]:
        try:
            data = json.loads((self.root / INDEX).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._rebuild()
        rows: dict[str, SessionInfo] = {}
        if isinstance(data, list):
            for item in data:
                try:
                    info = SessionInfo(
                        id=str(item["id"]),
                        title=str(item.get("title", "")),
                        time=float(item.get("time", 0.0)),
                        cwd=str(item.get("cwd", "")),
                        messages=int(item.get("messages", 0)),
                        named=item.get("named") is True,
                    )
                except (KeyError, TypeError, ValueError, AttributeError):
                    continue
                if (self.root / f"{info.id}.jsonl").is_file():
                    rows[info.id] = info
        return rows

    def _rebuild(self) -> dict[str, SessionInfo]:
        rows: dict[str, SessionInfo] = {}
        if not self.root.is_dir():
            return rows
        for path in self.root.glob("*.jsonl"):
            try:
                transcript = Transcript.from_jsonl(path.read_text(encoding="utf-8"))
                stamp = path.stat().st_mtime
            except (OSError, ValueError):
                continue
            rows[path.stem] = SessionInfo(
                path.stem, title_of(transcript), stamp, "", len(transcript.messages)
            )
        return rows

    def _write_index(self, rows: dict[str, SessionInfo]) -> None:
        ordered = sorted(rows.values(), key=lambda info: info.time, reverse=True)
        _atomic(self.root / INDEX, json.dumps([asdict(info) for info in ordered], indent=1))

    # -- public ---------------------------------------------------------------------

    def save(
        self, session_id: str, transcript: Transcript, cwd: Path, *, title: str | None = None
    ) -> SessionInfo | None:
        """Write the transcript and its index row; an empty conversation is not saved.

        ``title`` names the conversation; without one a name given earlier is kept.
        """
        if not transcript.messages:
            return None
        _atomic(self.path(session_id), transcript.to_jsonl())
        rows = self._read_index()
        previous = rows.get(session_id)
        named = bool(title) or (previous is not None and previous.named)
        shown = clean_title(title) if title else (
            previous.title if previous is not None and previous.named else title_of(transcript)
        )
        info = SessionInfo(
            session_id, shown, time.time(), str(cwd), len(transcript.messages), named
        )
        rows[session_id] = info
        self._write_index(rows)
        return info

    def title(self, session_id: str) -> str | None:
        info = self._read_index().get(session_id)
        return info.title if info is not None else None

    def list(self) -> list[SessionInfo]:
        return sorted(self._read_index().values(), key=lambda info: info.time, reverse=True)

    def find(self, key: str | None = None) -> SessionInfo | None:
        """The session with this id or unique id prefix; the latest one without a key."""
        rows = self.list()
        if not rows:
            return None
        if not key:
            return rows[0]
        exact = [info for info in rows if info.id == key]
        if exact:
            return exact[0]
        matches = [info for info in rows if info.id.startswith(key)]
        return matches[0] if len(matches) == 1 else None

    def load(self, session_id: str) -> Transcript:
        return Transcript.from_jsonl(self.path(session_id).read_text(encoding="utf-8"))
