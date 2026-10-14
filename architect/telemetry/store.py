"""Durable JSONL source of truth with a recoverable SQLite metadata index."""

from __future__ import annotations

import math
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from threading import RLock
from typing import BinaryIO, Self

from pydantic import ValidationError

from architect.telemetry.schema import TelemetryRow

DEFAULT_LOCK_TIMEOUT_S = 30.0
_LOCK_WAIT: ContextVar[float | None] = ContextVar("telemetry_lock_wait", default=None)


@contextmanager
def lock_wait(seconds: float) -> Iterator[None]:
    """Bound every telemetry write-lock wait made from this context.

    The bound follows the context into ``asyncio.to_thread`` workers and child
    tasks. An expired wait raises the SQLite busy ``OperationalError`` unchanged,
    so a caller can tell contention from damage. Other contexts keep the default.
    """
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, (int, float))
        or not math.isfinite(seconds)
        or seconds < 0
    ):
        raise ValueError("invalid telemetry lock wait")
    token = _LOCK_WAIT.set(float(seconds))
    try:
        yield
    finally:
        _LOCK_WAIT.reset(token)


class TelemetryStore:
    """Serialize writers using SQLite, fsync JSONL first, then commit its index.

    Reopening reconstructs the index from JSONL. A torn, non-JSON final write is
    discarded; complete committed lines are never modified. A valid final record
    missing only its newline is preserved. Corrupt complete records fail closed.
    All users of a directory must write through this class, not directly to JSONL.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._path = self.directory / "runs.jsonl"
        self._mutex = RLock()
        self._closed = False
        self._connection = sqlite3.connect(
            self.directory / "index.sqlite3",
            timeout=DEFAULT_LOCK_TIMEOUT_S,
            check_same_thread=False,
        )
        self._connection.execute("PRAGMA synchronous = FULL")
        try:
            with self._transaction():
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS runs ("
                    "run_id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, "
                    "spec_hash TEXT NOT NULL, harness_version INTEGER NOT NULL, "
                    "accepted INTEGER NOT NULL, byte_offset INTEGER NOT NULL, "
                    "byte_length INTEGER NOT NULL)"
                )
                self._connection.execute(
                    "CREATE INDEX IF NOT EXISTS runs_spec_hash ON runs(spec_hash)"
                )
                self._connection.execute(
                    "CREATE INDEX IF NOT EXISTS runs_timestamp ON runs(timestamp)"
                )
                self._connection.execute(
                    "CREATE INDEX IF NOT EXISTS runs_offset ON runs(byte_offset)"
                )
                self._reconcile(full=True)
        except BaseException:
            self.close()
            raise

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._mutex:
            if self._closed:
                raise ValueError("telemetry store is closed")
            wait = _LOCK_WAIT.get()
            wait = DEFAULT_LOCK_TIMEOUT_S if wait is None else wait
            self._connection.execute(f"PRAGMA busy_timeout = {int(wait * 1000)}")
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    @staticmethod
    def _sync(stream: BinaryIO) -> None:
        stream.flush()
        os.fsync(stream.fileno())

    def _index(self, row: TelemetryRow, offset: int, length: int) -> None:
        self._connection.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                row.run_id,
                row.timestamp.isoformat(),
                row.spec_hash,
                row.harness_version,
                int(row.accepted),
                offset,
                length,
            ),
        )

    def _reconcile(self, *, full: bool = False) -> None:
        # Startup distrusts the index and replays the journal. Normal operations
        # seek directly to its indexed end, so each durable record is indexed once.
        if full:
            self._connection.execute("DELETE FROM runs")
        last = self._connection.execute(
            "SELECT byte_offset, byte_length FROM runs ORDER BY byte_offset DESC LIMIT 1"
        ).fetchone()
        indexed_end = 0 if last is None else int(last[0]) + int(last[1])
        with self._path.open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() < indexed_end:
                raise ValueError("telemetry journal is shorter than its index")
            stream.seek(indexed_end)
            while True:
                offset = stream.tell()
                line = stream.readline()
                if not line:
                    break
                terminated = line.endswith(b"\n")
                try:
                    row = TelemetryRow.model_validate_json(line)
                except ValidationError:
                    if terminated:
                        raise ValueError("invalid complete telemetry record") from None
                    # Only a physically incomplete final JSON object is repairable.
                    # A syntactically valid but privacy-invalid row must fail closed.
                    import json

                    try:
                        json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        stream.truncate(offset)
                        self._sync(stream)
                        break
                    raise ValueError("invalid telemetry metadata") from None
                if self._connection.execute(
                    "SELECT 1 FROM runs WHERE run_id = ?", (row.run_id,)
                ).fetchone():
                    raise ValueError("duplicate telemetry run_id in journal")
                if not terminated:
                    stream.write(b"\n")
                    self._sync(stream)
                    line += b"\n"
                self._index(row, offset, len(line))

    def append(self, row: TelemetryRow) -> None:
        """Persist a validated row exactly once; duplicate UUIDs raise ValueError.

        The JSONL write is the durability point. If indexing fails after that
        write, reopening (or the next operation) will recover the saved record.
        """
        # Frozen models can still contain mutable dictionaries or be constructed
        # without validation; repeat validation at the persistence boundary.
        checked = TelemetryRow.model_validate(row.model_dump(mode="python", warnings=False))
        payload = (checked.model_dump_json() + "\n").encode("utf-8")
        with self._transaction():
            self._reconcile()
            if self._connection.execute(
                "SELECT 1 FROM runs WHERE run_id = ?", (checked.run_id,)
            ).fetchone():
                raise ValueError("duplicate telemetry run_id")
            with self._path.open("ab") as stream:
                offset = stream.tell()
                stream.write(payload)
                self._sync(stream)
            self._index(checked, offset, len(payload))

    def probe(self) -> None:
        """Take and release the write lock, so a caller can refuse work up front.

        This proves only that logging was possible a moment ago; ``append`` can
        still fail later and must still be treated as mandatory.
        """
        with self._transaction():
            pass

    def rows(self) -> list[TelemetryRow]:
        """Read durable rows in journal order, including recovered writes."""
        with self._transaction():
            self._reconcile()
            with self._path.open("rb") as stream:
                return [TelemetryRow.model_validate_json(line) for line in stream]

    def close(self) -> None:
        with self._mutex:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def __enter__(self) -> Self:
        if self._closed:
            raise ValueError("telemetry store is closed")
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
