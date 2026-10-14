"""Durable installed licenses and metadata-only drift monitoring.

SQLite serializes acceptance and pointer publication. Each revision owns a copy
of the authenticated disk files; caller directories and signed bytes never change.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import math
import os
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from architect.spec import FloorKind, HarnessArtifact, Lifecycle, Spec
from architect.spec.binding import check_subharness_tree, copy_subharness_tree
from architect.telemetry import TelemetryRow, categorical


class RegistryError(ValueError):
    """Invalid registry key, storage operation, or license acceptance."""


class RegistryEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    revision: int
    spec_hash: str
    artifact_version: int
    directory: Path


class DriftEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    reason: Literal["floor_drift", "model_change"]
    score: float | None


class DriftResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event: DriftEvent | None
    accepted: bool = False
    failure: str | None = None


_PLATFORM = sys.platform
# Entries live at <root>/versions/<32 hex>/<revision>/artifacts/<64 hex>/<version>. The
# deepest file, pi-pack.json, must fit Windows MAX_PATH (259 usable characters) and the
# directory itself the 247 that CreateDirectory accepts, so an entry directory may not
# exceed 246 characters. Construction reserves two digits for revision and version.
_MAX_ENTRY_DIRECTORY = 246
_PROBE_ENTRY = Path("versions") / ("0" * 32) / "99" / "artifacts" / ("0" * 64) / "99"


def _long_paths_enabled() -> bool:
    """Windows honours paths beyond MAX_PATH only under the LongPathsEnabled policy."""
    if _PLATFORM != "win32":
        return True
    winreg: Any = importlib.import_module("winreg")
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
            value, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
    except OSError:
        return False
    return bool(value == 1)


def _entry_fits(directory: Path) -> None:
    """Name path-length refusal instead of an opaque storage error."""
    if len(str(directory)) > _MAX_ENTRY_DIRECTORY and not _long_paths_enabled():
        raise RegistryError("workspace_path_too_long")


def _path_capacity(root: Path) -> None:
    """Refuse a root whose installs would fail on path length after Stage 4 spend."""
    _entry_fits(root / _PROBE_ENTRY)


def _name(name: str) -> str:
    if not isinstance(name, str) or not name.strip() or len(name) > 1024 or "\0" in name:
        raise RegistryError("invalid_name")
    return name


def _revision(revision: int | None) -> None:
    if revision is not None and (type(revision) is not int or revision < 1):
        raise RegistryError("invalid_revision")


def _authenticated(directory: Path) -> HarnessArtifact:
    try:
        artifact = HarnessArtifact.load(directory)
        if artifact.spec.lifecycle != Lifecycle.INSTALL or not artifact.licensed:
            raise RegistryError("invalid_license")
        return artifact
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        # RecursionError: a pathologically nested JSON file is damage, not a crash.
        raise RegistryError("invalid_license") from exc


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def _copy_forge_children(source: Path, destination: Path) -> None:
    """Carry a Forge parent's ``forge.json`` and ``subharnesses/`` tree (Phase 10, Section 4).

    Play time reloads each bound child from ``<entry>/subharnesses/<name>/``, so an
    install that dropped the tree would license a parent whose first child call fails.
    """
    try:
        record = source / "forge.json"
        if record.is_symlink():
            raise ValueError("invalid_forge_record")
        if record.exists():
            shutil.copyfile(record, destination / "forge.json")
            _fsync_file(destination / "forge.json")
        copy_subharness_tree(source, destination)
        for path in sorted((destination / "subharnesses").rglob("*")):
            if path.is_file():
                _fsync_file(path)
    except (OSError, ValueError) as exc:
        raise RegistryError("registry_subharness_missing") from exc


def _child_directories(directory: Path) -> tuple[Path, ...]:
    """Every copied directory below ``subharnesses/``, relative to ``directory``."""
    tree = directory / "subharnesses"
    if not tree.is_dir():
        return ()
    return tuple(path.relative_to(directory) for path in (tree, *tree.rglob("*"))
                 if path.is_dir())


def _quality(kind: FloorKind, score: float) -> float:
    return -score if kind == FloorKind.HUMAN_EDIT_RATE else score


def _rank(artifact: HarnessArtifact) -> tuple[float, float, float, int]:
    report = artifact.report
    assert report is not None  # Only independently authenticated artifacts enter acceptance.
    secondary = artifact.spec.secondary_floor
    secondary_quality = 0.0
    if secondary is not None:
        assert report.secondary_floor_score is not None
        secondary_quality = _quality(secondary.kind, report.secondary_floor_score)
    cost = report.mean_cost_usd if report.mean_cost_usd is not None else report.cost_usd
    return (_quality(artifact.spec.floor.kind, report.floor_score), secondary_quality,
            -cost, -report.component_count)


def _signature_fields(artifact: HarnessArtifact) -> tuple[str, str, str]:
    """``(slot, shape, family)`` of an entry's Spec (Phase 10 Section 5)."""
    # Imported here: ``architect.routing`` imports this module, never the reverse at load.
    from architect.routing.signature import signature

    sig = signature(artifact.spec)
    return sig.slot, sig.shape, sig.family


def _index_slot(db: sqlite3.Connection, name: str, revision: int,
                artifact: HarnessArtifact) -> None:
    slot, shape, family = _signature_fields(artifact)
    db.execute("INSERT OR REPLACE INTO slots VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
               (name, revision, artifact.spec.spec_hash, slot, shape, family,
                artifact.model, artifact.spec.lifecycle.value))


def _drift_rule(kind: FloorKind, limit: float, scores: Sequence[float], margin: float) -> bool:
    """The ``observe`` floor-drift rule on one stored window."""
    quality = _quality(kind, sum(scores) / len(scores))
    bound = _quality(kind, limit)
    # Least-squares slope has the sign of this covariance numerator.
    midpoint = (len(scores) - 1) / 2
    slope = sum((i - midpoint) * _quality(kind, value) for i, value in enumerate(scores))
    return quality < bound or (quality <= bound + margin and slope < 0)


def _beats(candidate: HarnessArtifact, incumbent: HarnessArtifact) -> bool:
    # Budget/model may change. Changing the task or its evaluation is not an improvement.
    fields = ("task", "input_schema", "output_schema", "domain", "tools",
              "floor", "secondary_floor")
    if any(getattr(candidate.spec, key) != getattr(incumbent.spec, key) for key in fields):
        return False
    return _rank(candidate) > _rank(incumbent)


class Registry:
    lock_timeout: float = 30.0

    def __init__(self, root: Path, *, window: int = 10, margin: float = .05) -> None:
        if type(window) is not int or window < 2:
            raise RegistryError("invalid_window")
        if (isinstance(margin, bool) or not isinstance(margin, (int, float))
                or not math.isfinite(margin) or not 0 <= margin <= 1):
            raise RegistryError("invalid_margin")
        self.root = root.resolve()
        _path_capacity(self.root)
        self.window = window
        self.margin = margin
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with self._transaction() as db:
                db.execute("""CREATE TABLE IF NOT EXISTS revisions (
                    name TEXT NOT NULL, revision INTEGER NOT NULL,
                    spec_hash TEXT NOT NULL, artifact_version INTEGER NOT NULL,
                    PRIMARY KEY(name, revision))""")
                db.execute("""CREATE TABLE IF NOT EXISTS current (
                    name TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    activated TEXT NOT NULL)""")
                current_columns = {str(row[1]) for row in db.execute("PRAGMA table_info(current)")}
                if "activated" not in current_columns:
                    db.execute("ALTER TABLE current ADD COLUMN activated TEXT NOT NULL DEFAULT ''")
                    db.execute("UPDATE current SET activated=?", (datetime.now(UTC).isoformat(),))
                db.execute("""CREATE TABLE IF NOT EXISTS observations (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL, revision INTEGER NOT NULL, run_id TEXT NOT NULL,
                    score REAL, role TEXT NOT NULL DEFAULT 'primary',
                    UNIQUE(name, revision, run_id))""")
                columns = {str(row[1]) for row in db.execute("PRAGMA table_info(observations)")}
                if "role" not in columns:
                    db.execute("ALTER TABLE observations ADD COLUMN "
                               "role TEXT NOT NULL DEFAULT 'primary'")
                if "activated" not in current_columns:
                    db.execute("UPDATE observations SET score=NULL WHERE EXISTS "
                               "(SELECT 1 FROM current WHERE current.name=observations.name "
                               "AND current.revision=observations.revision)")
                # Phase 10 Section 9.2: the routing index of task identities.
                db.execute("""CREATE TABLE IF NOT EXISTS slots (
                    name TEXT NOT NULL, revision INTEGER NOT NULL, spec_hash TEXT NOT NULL,
                    slot TEXT NOT NULL, shape TEXT NOT NULL, family TEXT NOT NULL,
                    model TEXT NOT NULL, lifecycle TEXT NOT NULL,
                    PRIMARY KEY(name, revision))""")
                db.execute("CREATE INDEX IF NOT EXISTS slots_slot ON slots(slot)")
                db.execute("CREATE INDEX IF NOT EXISTS slots_shape ON slots(shape)")
                db.execute("CREATE INDEX IF NOT EXISTS slots_family ON slots(family)")
            self._backfill_slots()
        except OSError as exc:
            raise RegistryError("storage_error") from exc

    def _backfill_slots(self) -> None:
        """Index current revisions installed before the ``slots`` table existed.

        One transaction: a crash mid-backfill rolls back and the next open retries.
        An entry that fails to load is skipped now and retried on the next open.
        """
        with self._transaction() as db:
            missing = db.execute(
                "SELECT c.name FROM current c LEFT JOIN slots s "
                "ON s.name = c.name AND s.revision = c.revision "
                "WHERE s.name IS NULL ORDER BY c.name").fetchall()
            for (name,) in missing:
                try:
                    entry = self._entry(db, str(name))
                    artifact = self._load(entry)
                except (RegistryError, OSError, ValueError, TypeError, RecursionError):
                    continue
                _index_slot(db, entry.name, entry.revision, artifact)

    def with_lock_timeout(self, seconds: float) -> Registry:
        """Same registry, but waiting at most ``seconds`` for the SQLite write lock.

        A lock wait that expires raises ``storage_error`` whose cause is the
        SQLite busy error, so a caller can tell contention from damage.
        """
        if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                or not math.isfinite(seconds) or seconds < 0):
            raise RegistryError("invalid_lock_timeout")
        view = copy.copy(self)
        view.lock_timeout = float(seconds)
        return view

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db: sqlite3.Connection | None = None
        try:
            db = sqlite3.connect(self.root / "registry.sqlite3", timeout=self.lock_timeout)
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except (sqlite3.Error, OSError) as exc:
            raise RegistryError("storage_error") from exc
        finally:
            if db is not None:
                db.close()  # Uncommitted work rolls back on every exceptional exit.

    def _entry(self, db: sqlite3.Connection, name: str,
               revision: int | None = None) -> RegistryEntry:
        _name(name)
        _revision(revision)
        if revision is None:
            row = db.execute("SELECT revision FROM current WHERE name=?", (name,)).fetchone()
            if row is None:
                raise RegistryError("unknown_name")
            revision = int(row["revision"])
        row = db.execute("SELECT * FROM revisions WHERE name=? AND revision=?",
                         (name, revision)).fetchone()
        if row is None:
            raise RegistryError("unknown_revision")
        spec_hash = str(row["spec_hash"])
        version = int(row["artifact_version"])
        directory = (self.root / "versions" / hashlib.sha256(name.encode()).hexdigest()[:32]
                     / str(revision) / "artifacts" / spec_hash / str(version))
        return RegistryEntry(name=name, revision=revision, spec_hash=spec_hash,
                             artifact_version=version, directory=directory)

    def _load(self, entry: RegistryEntry) -> HarnessArtifact:
        artifact = _authenticated(entry.directory)
        if artifact.spec.spec_hash != entry.spec_hash or artifact.version != entry.artifact_version:
            raise RegistryError("invalid_license")
        return artifact

    def install(self, name: str, artifact: HarnessArtifact, *,
                allow_budget_increase: bool = False) -> RegistryEntry:
        _name(name)
        if type(allow_budget_increase) is not bool:
            raise RegistryError("invalid_budget_permission")
        with self._transaction() as db:
            candidate = _authenticated(artifact.directory)
            if candidate != artifact:
                raise RegistryError("artifact_mismatch")
            current = db.execute("SELECT revision FROM current WHERE name=?", (name,)).fetchone()
            if current is not None:
                incumbent = self._load(self._entry(db, name))
                if not _beats(candidate, incumbent):
                    raise RegistryError("not_better")
                if not allow_budget_increase and any(
                    getattr(candidate.spec.budget, ceiling)
                    > getattr(incumbent.spec.budget, ceiling)
                    for ceiling in ("cost_usd", "compute_s")
                ):
                    raise RegistryError("budget_increase")
            latest = db.execute("SELECT MAX(revision) FROM revisions WHERE name=?",
                                (name,)).fetchone()[0]
            revision = 1 if latest is None else int(latest) + 1
            db.execute("INSERT INTO revisions VALUES (?, ?, ?, ?)",
                       (name, revision, candidate.spec.spec_hash, candidate.version))
            entry = self._entry(db, name, revision)
            # Preserve unpublished crash remnants, but do not let them block recovery.
            while entry.directory.parents[2].exists():
                db.execute("UPDATE revisions SET revision=? WHERE name=? AND revision=?",
                           (revision + 1, name, revision))
                revision += 1
                entry = self._entry(db, name, revision)
            _entry_fits(entry.directory)
            entry.directory.parent.mkdir(parents=True, exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix=".install-", dir=self.root))
            try:
                for filename in ("harness.py", "spec.json", "config.json", "report.json"):
                    shutil.copyfile(candidate.directory / filename, temporary / filename)
                    with (temporary / filename).open("r+b") as stream:
                        os.fsync(stream.fileno())
                _copy_forge_children(candidate.directory, temporary)
                # Every copied child directory must stay openable at its published depth.
                for relative in _child_directories(temporary):
                    _entry_fits(entry.directory / relative)
                copied = _authenticated(temporary)
                try:
                    check_subharness_tree(copied)
                except (OSError, ValueError, TypeError, RecursionError) as exc:
                    raise RegistryError("registry_subharness_missing") from exc
                if copied.model_copy(update={"directory": candidate.directory}) != candidate:
                    raise RegistryError("artifact_changed")
                # A crash after rename leaves an unpublished orphan. Never overwrite it.
                if entry.directory.exists():
                    raise RegistryError("revision_conflict")
                os.rename(temporary, entry.directory)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
            _index_slot(db, name, revision, candidate)
            db.execute("INSERT INTO current (name, revision, activated) VALUES (?, ?, ?) "
                       "ON CONFLICT(name) DO UPDATE SET revision=excluded.revision, "
                       "activated=excluded.activated",
                       (name, revision, datetime.now(UTC).isoformat()))
            return entry

    def get(self, name: str, revision: int | None = None) -> HarnessArtifact:
        with self._transaction() as db:
            return self._load(self._entry(db, name, revision))

    def status(self, name: str) -> RegistryEntry:
        with self._transaction() as db:
            entry = self._entry(db, name)
            self._load(entry)
            return entry

    def list(self) -> tuple[RegistryEntry, ...]:
        with self._transaction() as db:
            entries = tuple(self._entry(db, str(row[0])) for row in
                            db.execute("SELECT name FROM current ORDER BY name"))
            for entry in entries:
                self._load(entry)
            return entries

    def find(self, *, slot: str | None = None,
             shape: str | None = None) -> tuple[RegistryEntry, ...]:
        """Current revisions whose indexed slot or shape equals the given one (read-only)."""
        return self._find((("slot", slot), ("shape", shape)))

    def find_family(self, family: str) -> tuple[RegistryEntry, ...]:
        """Current revisions whose indexed family equals ``family`` (advisory Tier C)."""
        return self._find((("family", family),))

    def _find(self, keys: Sequence[tuple[str, str | None]]) -> tuple[RegistryEntry, ...]:
        clauses = [(f"s.{column} = ?", value) for column, value in keys if value is not None]
        if not clauses:
            return ()
        where = " OR ".join(clause for clause, _ in clauses)
        with self._transaction() as db:
            rows = db.execute(
                "SELECT s.name FROM slots s JOIN current c "
                f"ON c.name = s.name AND c.revision = s.revision WHERE {where} ORDER BY s.name",
                tuple(value for _, value in clauses)).fetchall()
            return tuple(self._entry(db, str(row[0])) for row in rows)

    def history(self, name: str) -> tuple[RegistryEntry, ...]:
        """Every published revision of ``name``, oldest first; nothing is loaded."""
        with self._transaction() as db:
            self._entry(db, name)
            rows = db.execute("SELECT revision FROM revisions WHERE name=? ORDER BY revision",
                              (name,)).fetchall()
            return tuple(self._entry(db, name, int(row[0])) for row in rows)

    def drift(self, name: str) -> Literal["no_window", "stable", "drifting"]:
        """Read-only floor-drift verdict on the stored window of the current revision.

        ``no_window`` when no floor has at least 2 scored observations, which is the
        norm outside games (label ``drift_window_none``). Nothing is written.
        """
        with self._transaction() as db:
            entry = self._entry(db, name)
            artifact = self._load(entry)
            floors = {"primary": artifact.spec.floor, "secondary": artifact.spec.secondary_floor}
            windowed = False
            for role, floor in floors.items():
                if floor is None:
                    continue
                scores = self._scores(db, entry, role)
                if len(scores) < 2:
                    continue
                windowed = True
                if _drift_rule(floor.kind, floor.limit, scores, self.margin):
                    return "drifting"
            return "stable" if windowed else "no_window"

    def rollback(self, name: str, revision: int | None = None) -> RegistryEntry:
        _revision(revision)
        with self._transaction() as db:
            current = self._entry(db, name)
            if revision is None:
                row = db.execute("SELECT MAX(revision) FROM revisions WHERE name=? AND revision<?",
                                 (name, current.revision)).fetchone()
                if row[0] is None:
                    raise RegistryError("no_previous_revision")
                revision = int(row[0])
            if revision >= current.revision:
                raise RegistryError("not_previous_revision")
            entry = self._entry(db, name, revision)
            _index_slot(db, name, revision, self._load(entry))
            db.execute("UPDATE current SET revision=?, activated=? WHERE name=?",
                       (revision, datetime.now(UTC).isoformat(), name))
            # Clear derived scores, not UUID identities or the append-only telemetry source.
            db.execute("UPDATE observations SET score=NULL WHERE name=? AND revision=?",
                       (name, revision))
            return entry

    def _scores(self, db: sqlite3.Connection, entry: RegistryEntry, role: str) -> Sequence[float]:
        return [float(row[0]) for row in db.execute(
            "SELECT score FROM observations WHERE name=? AND revision=? AND role=? "
            "AND score IS NOT NULL ORDER BY sequence DESC LIMIT ?",
            (entry.name, entry.revision, role, self.window))][::-1]

    def observe(self, name: str, rows: Sequence[TelemetryRow]) -> DriftEvent | None:
        with self._transaction() as db:
            entry = self._entry(db, name)
            artifact = self._load(entry)
            activated = datetime.fromisoformat(str(db.execute(
                "SELECT activated FROM current WHERE name=?", (name,),
            ).fetchone()[0]))
            floors = {"primary": artifact.spec.floor, "secondary": artifact.spec.secondary_floor}
            fresh: set[str] = set()
            model_changed: str | None = None
            for row in rows:
                floor = floors[row.floor_role]
                if (row.timestamp.tzinfo is None or row.timestamp < activated
                        or row.spec_hash != entry.spec_hash
                        or row.harness_version != entry.artifact_version
                        or row.lifecycle != Lifecycle.INSTALL or floor is None
                        or row.floor_type != floor.kind or row.floor_limit != floor.limit):
                    continue
                inserted = db.execute("INSERT OR IGNORE INTO observations "
                                      "(name, revision, run_id, score, role) "
                                      "VALUES (?, ?, ?, ?, ?)",
                                      (name, entry.revision, row.run_id, row.floor_score,
                                       row.floor_role))
                if inserted.rowcount == 0:
                    continue
                if row.floor_score is not None:
                    fresh.add(row.floor_role)
                if row.model != categorical(artifact.model):
                    model_changed = row.floor_role
            if model_changed is not None:
                scores = self._scores(db, entry, model_changed)
                return DriftEvent(name=name, reason="model_change",
                                  score=sum(scores) / len(scores) if scores else None)
            # Stable ordering gives the primary floor precedence when both drift.
            for role, floor in floors.items():
                if role not in fresh:
                    continue
                assert floor is not None  # Only configured roles enter the fresh set.
                scores = self._scores(db, entry, role)
                if _drift_rule(floor.kind, floor.limit, scores, self.margin):
                    return DriftEvent(name=name, reason="floor_drift",
                                      score=sum(scores) / len(scores))
            return None

    async def monitor(self, name: str, rows: Sequence[TelemetryRow],
                      recompile: Callable[[Spec], Awaitable[HarnessArtifact]]) -> DriftResult:
        event = self.observe(name, rows)
        if event is None:
            return DriftResult(event=None)
        spec = self.get(name).spec
        try:
            candidate = await recompile(spec)
        except Exception:
            return DriftResult(event=event, failure="recompile_failed")
        try:
            self.install(name, candidate)
        except RegistryError:
            return DriftResult(event=event, failure="acceptance_rejected")
        return DriftResult(event=event, accepted=True)
