"""Append-only, content-free route records (Phase 10 Section 9.1).

Four JSONL journals under ``workspace/routes/`` are the source of truth. Every line
is fsynced before its SQLite index row (``routes/index.sqlite``, WAL) commits, and
the index is rebuilt from the journal byte offsets, so losing it loses nothing.
Records hold only closed literals, numbers, UUIDs and ``h_`` categories (P6).
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from architect.routing.labels import EvidenceClass, EvidenceLabel, FloorOrigin, RoleName, RouteCode
from architect.telemetry import categorical
from architect.telemetry.schema import Category

__all__ = [
    "Origin",
    "OutcomeRecord",
    "RatingRecord",
    "RoleRecord",
    "RouteAction",
    "RouteLog",
    "RouteLogError",
    "RouteRecord",
    "category",
]

Origin = Literal[
    "session",
    "cli",
    "http",
    "orchestrator",
    "forge_spawn",
    "builder",
    "forge_run",
    "forge_integrate",
    "bench",
]
RouteAction = Literal["reuse_installed", "warm_start", "compile", "refuse"]
NonnegativeInt = Annotated[int, Field(ge=0, strict=True)]
Finite = Annotated[float, Field(allow_inf_nan=False)]
Money = Annotated[float, Field(ge=0, allow_inf_nan=False)]
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")


def category(value: str) -> str:
    """A 32-hex signature field as an ``h_`` category; anything else via ``categorical``."""
    return "h_" + value if _HEX32.fullmatch(value) else categorical(value)


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class RouteRecord(_Record):
    decision_id: UUID
    timestamp: AwareDatetime
    origin: Origin
    action: RouteAction
    tier: Literal["A", "B", "C", "none"]
    slot: Category
    shape: Category
    family: Category
    token_count: NonnegativeInt
    candidate_count: NonnegativeInt
    chosen_rank: NonnegativeInt | None
    target_digest: Category | None  # categorical(artifact_digest)
    model: Category | None
    fit: Finite
    m_lcb: Finite | None
    r: Finite | None
    expected_cost_usd: Finite | None
    latency_s: Finite | None
    latency_basis: Literal["measured", "bound"] | None
    evidence: tuple[EvidenceLabel, ...]
    codes: tuple[RouteCode, ...]
    explored: bool
    decision_ms: Finite


class OutcomeRecord(_Record):
    decision_id: UUID
    timestamp: AwareDatetime
    outcome: Literal["licensed", "unlicensed", "delivered", "failed", "discarded", "cancelled"]
    stage4_passed: bool | None
    evidence_class: EvidenceClass | None
    compile_cost_usd: Money = 0
    build_cost_usd: Money = 0
    delivery_cost_usd: Money = 0


class RoleRecord(_Record):
    decision_id: UUID
    timestamp: AwareDatetime
    role: RoleName
    model: Category
    variant: Category
    domain: Category
    evidence_class: EvidenceClass
    floor_origin: FloorOrigin | None
    chosen_rank: NonnegativeInt
    n: Finite
    p_lcb: Finite | None
    explored: bool
    outcome: Literal["success", "failure", "partial", "excluded"] | None
    score: Finite | None
    codes: tuple[RouteCode, ...]


class RatingRecord(_Record):
    run_id: UUID
    timestamp: AwareDatetime
    target_digest: Category
    rating: Literal["good", "bad"]


class RouteLogError(RuntimeError):
    """The route log could not be read or written (``evidence_store_unavailable``)."""


AnyRecord = RouteRecord | OutcomeRecord | RoleRecord | RatingRecord
_FILES: dict[type[_Record], str] = {
    RouteRecord: "decisions.jsonl",
    OutcomeRecord: "outcomes.jsonl",
    RoleRecord: "roles.jsonl",
    RatingRecord: "ratings.jsonl",
}


class RouteLog:
    """Append-only journals plus a recoverable SQLite index.

    Construction touches nothing, so an unreadable ``routes/`` surfaces only when the
    router reads it, and the router then fails open (P5).
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # ----------------------------------------------------------------- storage

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db: sqlite3.Connection | None = None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.root / "index.sqlite", timeout=5.0)
            db.execute("PRAGMA busy_timeout = 5000")
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("BEGIN IMMEDIATE")
            self._schema(db)
            self._reconcile(db)
            yield db
            db.commit()
        except (sqlite3.Error, OSError, ValueError, ValidationError) as exc:
            raise RouteLogError("evidence_store_unavailable") from exc
        finally:
            if db is not None:
                db.close()  # Uncommitted work rolls back on every exceptional exit.

    @staticmethod
    def _schema(db: sqlite3.Connection) -> None:
        db.execute("CREATE TABLE IF NOT EXISTS offsets (file TEXT PRIMARY KEY, offset INTEGER)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS decisions (decision_id TEXT PRIMARY KEY, "
            "slot TEXT, shape TEXT, explored INTEGER, line TEXT)"
        )
        db.execute("CREATE INDEX IF NOT EXISTS decisions_shape ON decisions(shape)")
        db.execute("CREATE INDEX IF NOT EXISTS decisions_slot ON decisions(slot)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS outcomes (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
            "decision_id TEXT, day TEXT, compile_cost REAL, line TEXT)"
        )
        db.execute("CREATE INDEX IF NOT EXISTS outcomes_decision ON outcomes(decision_id)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS roles (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
            "role TEXT, line TEXT)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS ratings (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
            "target TEXT, rating TEXT)"
        )

    def _reconcile(self, db: sqlite3.Connection) -> None:
        """Index every complete journal line past the stored offset, exactly once."""
        for kind, name in _FILES.items():
            path = self.root / name
            if not path.exists():
                continue
            row = db.execute("SELECT offset FROM offsets WHERE file=?", (name,)).fetchone()
            offset = 0 if row is None else int(row[0])
            with path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                if stream.tell() < offset:
                    raise ValueError("route journal is shorter than its index")
                stream.seek(offset)
                while True:
                    line = stream.readline()
                    if not line.endswith(b"\n"):
                        break  # A torn final write is dropped by the next append.
                    self._index(db, kind.model_validate_json(line), line.decode("utf-8"))
                    offset = stream.tell()
            self._offset(db, name, offset)

    @staticmethod
    def _offset(db: sqlite3.Connection, name: str, offset: int) -> None:
        db.execute(
            "INSERT INTO offsets VALUES (?, ?) ON CONFLICT(file) DO UPDATE "
            "SET offset=excluded.offset",
            (name, offset),
        )

    @staticmethod
    def _index(db: sqlite3.Connection, record: _Record, line: str) -> None:
        if isinstance(record, RouteRecord):
            db.execute(
                "INSERT OR IGNORE INTO decisions VALUES (?, ?, ?, ?, ?)",
                (str(record.decision_id), record.slot, record.shape, int(record.explored), line),
            )
        elif isinstance(record, OutcomeRecord):
            day = record.timestamp.astimezone(UTC).date().isoformat()
            db.execute(
                "INSERT INTO outcomes (decision_id, day, compile_cost, line) VALUES (?, ?, ?, ?)",
                (str(record.decision_id), day, record.compile_cost_usd, line),
            )
        elif isinstance(record, RoleRecord):
            db.execute("INSERT INTO roles (role, line) VALUES (?, ?)", (record.role, line))
        elif isinstance(record, RatingRecord):
            db.execute(
                "INSERT INTO ratings (target, rating) VALUES (?, ?)",
                (record.target_digest, record.rating),
            )

    # --------------------------------------------------------------- interface

    def append(self, record: AnyRecord) -> None:
        """Validate again at the boundary, fsync the JSONL line, then index it."""
        kind = type(record)
        if kind not in _FILES:
            raise RouteLogError("invalid_route_record")
        try:
            checked = kind.model_validate(record.model_dump(mode="python"))
        except ValidationError as exc:
            raise RouteLogError("invalid_route_record") from exc
        payload = (checked.model_dump_json() + "\n").encode("utf-8")
        name = _FILES[kind]
        with self._transaction() as db:
            row = db.execute("SELECT offset FROM offsets WHERE file=?", (name,)).fetchone()
            indexed = 0 if row is None else int(row[0])
            with (self.root / name).open("ab") as stream:
                stream.truncate(indexed)  # Drop a torn tail; complete lines are indexed.
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            self._index(db, checked, payload.decode("utf-8"))
            self._offset(db, name, indexed + len(payload))

    def counter(self, shape: str) -> int:
        """Prior decisions for this shape: the deterministic exploration counter."""
        with self._transaction() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM decisions WHERE shape=?", (category(shape),)
            ).fetchone()
            return int(row[0])

    def explore_spent_today(self) -> float:
        """Compile spend today (UTC) on outcomes whose decision explored."""
        today = datetime.now(UTC).date().isoformat()
        with self._transaction() as db:
            row = db.execute(
                "SELECT COALESCE(SUM(o.compile_cost), 0) FROM outcomes o JOIN decisions d "
                "ON o.decision_id = d.decision_id WHERE d.explored = 1 AND o.day = ?",
                (today,),
            ).fetchone()
            return float(row[0])

    def outcomes(
        self, *, slot: str | None = None, shape: str | None = None
    ) -> Iterator[tuple[RouteRecord, OutcomeRecord]]:
        """Decision and outcome pairs in append order, filtered by slot and/or shape."""
        clauses: list[str] = []
        values: list[str] = []
        if slot is not None:
            clauses.append("d.slot = ?")
            values.append(category(slot))
        if shape is not None:
            clauses.append("d.shape = ?")
            values.append(category(shape))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._transaction() as db:
            rows = db.execute(
                "SELECT d.line, o.line FROM outcomes o JOIN decisions d "
                f"ON o.decision_id = d.decision_id{where} ORDER BY o.sequence",
                values,
            ).fetchall()
        try:
            pairs = [
                (RouteRecord.model_validate_json(left), OutcomeRecord.model_validate_json(right))
                for left, right in rows
            ]
        except ValidationError as exc:
            raise RouteLogError("evidence_store_unavailable") from exc
        return iter(pairs)

    def roles(self, role: RoleName) -> Iterator[RoleRecord]:
        with self._transaction() as db:
            rows = db.execute(
                "SELECT line FROM roles WHERE role=? ORDER BY sequence", (role,)
            ).fetchall()
        try:
            return iter([RoleRecord.model_validate_json(line) for (line,) in rows])
        except ValidationError as exc:
            raise RouteLogError("evidence_store_unavailable") from exc

    def ratings(self, target_digest: str) -> tuple[int, int]:
        """(good, bad) for ``categorical(artifact_digest)``; a raw digest is hashed first."""
        target = categorical(target_digest)
        with self._transaction() as db:
            counts = {
                str(rating): int(count)
                for rating, count in db.execute(
                    "SELECT rating, COUNT(*) FROM ratings WHERE target=? GROUP BY rating",
                    (target,),
                ).fetchall()
            }
        return counts.get("good", 0), counts.get("bad", 0)
