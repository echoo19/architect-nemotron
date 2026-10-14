"""What a desktop conversation showed, kept beside its saved transcript.

A saved conversation (``<workspace>/sessions/<id>.jsonl``, ``architect.agent.sessions``)
is its transcript: what the model reads. A slash command's turn (``/make @spec.json``,
``/export``), the plan and result cards, a build's progress and Forge feed, the questions
and their answers, and Try it's runs are not in it, so a conversation reopened from the
transcript alone shows only the turns the model answered. ``ViewLog`` keeps the bridge
messages the app folds into the conversation, one JSON line each, in
``<workspace>/sessions/views/<id>.jsonl``, and a reopened conversation folds them again,
as a page that re-attaches folds the hub's replay.

Turns the record never saw (from before it existed, or from the terminal, which keeps
none) are still in the transcript. Each kept ``turn_finished`` says how many transcript
messages the conversation then had (``transcript``). When a process starts keeping the
record of a conversation whose transcript holds more than that, it first writes
``{"type": "view", "from": a, "to": b}``: the app shows transcript messages ``a`` to ``b``
there, from the transcript. Streamed text is joined into one piece; the thinking flag,
links, command output and the status line are not kept, since nothing shows them once
the turn is over. Lines wait in memory until the transcript is saved, so a conversation
that never is (one ``/status`` and nothing else) leaves no file. A write that fails is
dropped: keeping the record never fails the conversation.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..agent.conductor import Conductor
    from ..agent.sessions import SessionStore

__all__ = ["KEPT", "ViewLog", "copy_view", "remove_view", "view_path"]

FOLDER = "views"
# What the app folds into a conversation (desktop/src/lib/timeline.ts `fold`, lib/forge.ts
# `feedMessage`); the status line (`state`) and request replies are not part of it.
KEPT = frozenset({"event", "ask", "ask_closed", "turn_started", "turn_finished", "forge",
                  "steered"})
SKIPPED_EVENTS = frozenset({"thinking", "link", "output"})
WAITING_LIMIT = 20_000  # lines held for a conversation that is not saved yet


def view_path(store: SessionStore, session_id: str) -> Path:
    """Where ``session_id``'s record is kept (``ValueError`` for an invalid id)."""
    return store.root / FOLDER / store.path(session_id).name


def copy_view(store: SessionStore, source: str, target: str) -> None:
    """Give a copied conversation (a fork of all of it) the record of the original."""
    with contextlib.suppress(OSError, ValueError):
        origin = view_path(store, source)
        if origin.is_file():
            shutil.copyfile(origin, view_path(store, target))


def remove_view(store: SessionStore, session_id: str) -> None:
    with contextlib.suppress(OSError, ValueError):
        view_path(store, session_id).unlink(missing_ok=True)


def _line(message: dict[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=False, default=str)


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _parse(lines: list[str]) -> list[dict[str, Any]]:
    """The record's messages; a line cut short when a process stopped is passed over."""
    found: list[dict[str, Any]] = []
    for raw in lines:
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        if isinstance(item, dict):
            found.append(item)
    return found


def covered(messages: list[dict[str, Any]]) -> int:
    """How many transcript messages a record accounts for: its last count."""
    count = 0
    for item in messages:
        kind = item.get("type")
        value = _count(item.get("to")) if kind == "view" else (
            _count(item.get("transcript")) if kind == "turn_finished" else None)
        if value is not None:
            count = value
    return count


class ViewLog:
    """The record of one bridge process, kept for whichever conversation it is in."""

    def __init__(self, store: SessionStore, conductor: Conductor) -> None:
        self.store = store
        self.conductor = conductor
        self._lock = threading.Lock()
        self._waiting: dict[str, list[str]] = {}  # not saved yet
        self._writing: set[str] = set()  # saved, and its record begun in this process
        self._text: tuple[str, str] | None = None  # (session, streamed text not kept yet)

    def record(self, message: dict[str, Any], line: str | None = None) -> None:
        """Keep ``message`` when the conversation shows it; ``line`` is its JSON, if made.

        Safe from any thread (the bridge sends from the compiler's observers too)."""
        if message.get("type") not in KEPT:
            return
        event = message.get("event")
        kind = event.get("kind") if isinstance(event, dict) else None
        if kind in SKIPPED_EVENTS:
            return
        session = self.conductor.session_id
        if message.get("type") == "turn_finished":
            # Where the transcript stood, so turns the record never sees can be placed.
            line = _line({**message, "transcript": len(self.conductor.transcript.messages)})
        with self._lock:
            if kind == "delta" and isinstance(event, dict):
                text = str(event.get("text") or "")
                if self._text is not None and self._text[0] == session:
                    self._text = (session, self._text[1] + text)
                else:
                    self._keep_text()
                    self._text = (session, text)
                return
            self._keep_text()
            self._keep(session, [line if line is not None else _line(message)])

    def flush(self) -> None:
        """Write what waited for a conversation that has been saved since."""
        with self._lock:
            self._keep_text()
            for session in list(self._waiting):
                if self._saved(session):
                    self._write(session, self._waiting.pop(session))
                    self._writing.add(session)

    def load(self, session_id: str) -> list[dict[str, Any]] | None:
        """What ``session_id`` showed, in order, with its ``view`` lines; None before any."""
        with self._lock:
            waiting = list(self._waiting.get(session_id, ()))
            if self._text is not None and self._text[0] == session_id and self._text[1]:
                waiting.append(_delta(self._text[1]))
        messages = _parse([*self._read(session_id), *waiting])
        return messages if any(m.get("type") != "view" for m in messages) else None

    # -- inside the lock ------------------------------------------------------------------

    def _keep_text(self) -> None:
        if self._text is None:
            return
        session, text = self._text
        self._text = None
        if text:
            self._keep(session, [_delta(text)])

    def _keep(self, session: str, lines: list[str]) -> None:
        waiting = self._waiting.get(session)
        if waiting is None and session not in self._writing:
            # This process's first line for it: the transcript may hold turns the record
            # never saw (saved before it existed, or added in the terminal).
            known = covered(_parse(self._read(session)))
            held = len(self.conductor.transcript.messages)
            if held > known:
                lines = [_line({"type": "view", "from": known, "to": held}), *lines]
            if self._saved(session):
                self._writing.add(session)
            else:
                waiting = self._waiting[session] = []
        if waiting is not None:
            waiting.extend(lines)
            if len(waiting) > WAITING_LIMIT:
                del waiting[: len(waiting) - WAITING_LIMIT]
            return
        self._write(session, lines)

    def _read(self, session: str) -> list[str]:
        try:
            return view_path(self.store, session).read_text(encoding="utf-8").splitlines()
        except (OSError, ValueError):
            return []

    def _saved(self, session: str) -> bool:
        try:
            return self.store.path(session).is_file()
        except (OSError, ValueError):
            return False

    def _write(self, session: str, lines: list[str]) -> None:
        with contextlib.suppress(OSError, ValueError):
            path = view_path(self.store, session)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write("".join(f"{line}\n" for line in lines))


def _delta(text: str) -> str:
    return _line({"type": "event", "event": {"kind": "delta", "text": text}})
