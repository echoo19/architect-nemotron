"""Authenticated disk templates, never a license for a newly compiled task.

Matching is deliberately conservative: model, domain, complete schemas, tool set,
and pipeline must agree. Task text only ranks these compatible candidates. A
returned artifact retains its *original* spec and report; the compiler must rebind
without that report and run Stage 4. Expire snapshots are already consumed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from fractions import Fraction
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from architect.spec import HarnessArtifact, Lifecycle, Spec
from architect.spec.binding import check_subharness_tree, copy_subharness_tree

__all__ = ["HarnessLibrary", "LibraryError", "ReadOnlyLibrary", "TemplateEvidence"]


class LibraryError(ValueError):
    """The library could not authenticate or persist an artifact."""


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _compatibility(spec: Spec, model: str) -> str:
    # Exact schema comparison is conservative, including constraints, not just type.
    value = {
        "domain": spec.domain,
        "input": spec.input_schema,
        "output": spec.output_schema,
        "tools": sorted(set(spec.tools)),
        "model": model,
        "pipeline": [step.model_dump(mode="json") for step in spec.pipeline],
    }
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _shape(spec: Spec) -> str:
    """The routing ``shape`` (Phase 10 Section 5): ``_compatibility`` without the model."""
    # Imported here: ``architect.routing`` imports this module, never the reverse at load.
    from architect.routing.signature import signature

    return signature(spec).shape


class TemplateEvidence(BaseModel):
    """One library row for routing; ``artifact`` is authenticated, or None if not loaded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    artifact: HarnessArtifact | None
    successes: int
    failures: int
    evicted: bool
    sequence: int
    promoted: bool


def _distance(source: Spec, target: Spec) -> tuple[bool, Fraction]:
    left = set(re.findall(r"\w+", source.task.casefold()))
    right = set(re.findall(r"\w+", target.task.casefold()))
    union = left | right
    return (
        source.spec_hash != target.spec_hash,
        Fraction(len(left ^ right), len(union)) if union else Fraction(0),
    )


_DIGEST = re.compile(r"[0-9a-f]{64}")
# Moved to ``architect.spec.binding`` (Phase 10 Section 4); re-imported under the
# old private names so snapshot behaviour is unchanged.
_check_subharnesses = check_subharness_tree


def _copy_subharnesses(source: Path, destination: Path) -> None:
    """Copy ``subharnesses/`` recursively; links anywhere in the tree are refused."""
    try:
        copy_subharness_tree(source, destination)
    except ValueError as exc:
        # The shared helper raises a plain ValueError; the library's code stays LibraryError.
        raise LibraryError(str(exc)) from exc


def _consume_template(directory: Path) -> None:
    try:
        descriptor = os.open(directory / ".consumed", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write("library-template: not a delivered execution\n")
        stream.flush()
        os.fsync(stream.fileno())


class HarnessLibrary:
    """SQLite-indexed isolated snapshots with persistent reuse/eviction history.

    Keys identify signed artifact content, not caller paths. Storing the same
    artifact is idempotent and cannot reset its counters or revive an evicted key.
    Writes use SQLite serialization and publish only fully authenticated copies.
    """

    def __init__(self, root: Path, *, promotion_reuses: int = 3) -> None:
        if type(promotion_reuses) is not int or promotion_reuses < 1:
            raise LibraryError("promotion_reuses must be a positive integer")
        self.root = root.resolve()
        self.promotion_reuses = promotion_reuses
        self._snapshots = self.root / "snapshots"
        try:
            self._snapshots.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LibraryError("library_storage_failed") from exc
        with self._connection() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS templates ("
                "sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
                "key TEXT NOT NULL UNIQUE, compatibility TEXT NOT NULL, "
                "successes INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0, "
                "evicted INTEGER NOT NULL DEFAULT 0)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS similarity ON templates(compatibility, evicted)"
            )
            # Phase 10 Section 9.2: the routing shape, filled on store, backfilled lazily.
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(templates)")}
            if "shape" not in columns:
                connection.execute("ALTER TABLE templates ADD COLUMN shape TEXT")
            connection.execute("CREATE INDEX IF NOT EXISTS shapes ON templates(shape, evicted)")
            # Per-spec count of Forge verifications (Stage 4 runs on promoted
            # candidates), bounding adaptive held-out overfitting across compiles.
            connection.execute(
                "CREATE TABLE IF NOT EXISTS forge_verifications ("
                "spec_hash TEXT PRIMARY KEY, count INTEGER NOT NULL DEFAULT 0)"
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        try:
            connection = sqlite3.connect(self.root / "library.sqlite3", timeout=30)
            try:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    yield connection
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise LibraryError("library_database_failed") from exc

    def _read(self, connection: sqlite3.Connection, key: str) -> HarnessArtifact | None:
        try:
            if re.fullmatch(r"[0-9a-f]{64}", key) is None:
                raise ValueError("invalid key")
            directory = self._snapshots / key
            if directory.is_symlink() or directory.resolve().parent != self._snapshots:
                raise ValueError("invalid snapshot path")
            if any((directory / name).is_symlink() for name in (
                "harness.py", "spec.json", "config.json", "report.json", ".consumed"
            )):
                raise ValueError("invalid snapshot file")
            artifact = HarnessArtifact.load(directory)
            if not artifact.licensed or artifact.artifact_digest != key:
                raise ValueError("invalid snapshot license")
            _check_subharnesses(artifact)
            if artifact.spec.lifecycle == Lifecycle.EXPIRE:
                _consume_template(directory)
            return artifact
        except (OSError, ValueError, TypeError, RecursionError):
            # RecursionError: a pathologically nested JSON file fails the read like any
            # other damage, so it is evicted instead of escaping to the caller.
            connection.execute("UPDATE templates SET evicted = 1 WHERE key = ?", (key,))
            return None

    def store(self, artifact: HarnessArtifact) -> str:
        """Copy an authenticated disk artifact, ignoring caller consumption markers."""
        temporary: Path | None = None
        try:
            source = HarnessArtifact.load(artifact.directory)
            if not artifact.licensed or not source.licensed:
                raise LibraryError("unlicensed_artifact")
            if source.model_dump(exclude={"directory"}) != artifact.model_dump(
                exclude={"directory"}
            ):
                raise LibraryError("artifact_disk_mismatch")
            key = source.artifact_digest
            with self._connection() as connection:
                existing = connection.execute(
                    "SELECT evicted FROM templates WHERE key = ?", (key,)
                ).fetchone()
                if existing is not None:
                    if not existing[0]:
                        self._read(connection, key)
                    return key
                temporary = Path(tempfile.mkdtemp(prefix=".pending-", dir=self._snapshots))
                for name in ("harness.py", "spec.json", "config.json", "report.json"):
                    shutil.copyfile(source.directory / name, temporary / name)
                _copy_subharnesses(source.directory, temporary)
                copied = HarnessArtifact.load(temporary)
                if not copied.licensed or copied.artifact_digest != key:
                    raise LibraryError("snapshot_authentication_failed")
                try:
                    _check_subharnesses(copied)
                except (OSError, ValueError, TypeError) as exc:
                    raise LibraryError("snapshot_authentication_failed") from exc
                if copied.model_dump(exclude={"directory"}) != source.model_dump(
                    exclude={"directory"}
                ):
                    raise LibraryError("snapshot_authentication_failed")
                if copied.spec.lifecycle == Lifecycle.EXPIRE:
                    _consume_template(temporary)
                destination = self._snapshots / key
                # A crash before index publication can leave an unindexed snapshot.
                if destination.exists():
                    raise LibraryError("snapshot_path_exists")
                os.replace(temporary, destination)
                temporary = None
                connection.execute(
                    "INSERT INTO templates(key, compatibility, shape) VALUES (?, ?, ?)",
                    (key, _compatibility(source.spec, source.model), _shape(source.spec)),
                )
                return key
        except (OSError, ValueError, TypeError) as exc:
            if isinstance(exc, LibraryError):
                raise
            raise LibraryError("artifact_storage_failed") from exc
        finally:
            if temporary is not None:
                shutil.rmtree(temporary, ignore_errors=True)

    def lookup(self, spec: Spec, model: str) -> HarnessArtifact | None:
        """Return the closest compatible original template, oldest on equal distance."""
        compatibility = _compatibility(spec, model)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT key FROM templates WHERE compatibility = ? AND evicted = 0 "
                "ORDER BY sequence", (compatibility,)
            ).fetchall()
            best: HarnessArtifact | None = None
            best_distance: tuple[bool, Fraction] | None = None
            for (key,) in rows:
                candidate = self._read(connection, key)
                if (candidate is None
                        or _compatibility(candidate.spec, candidate.model) != compatibility):
                    continue
                distance = _distance(candidate.spec, spec)
                if best_distance is None or distance < best_distance:
                    best, best_distance = candidate, distance
            return best

    def _backfill_shapes(self, connection: sqlite3.Connection) -> None:
        """Fill ``shape`` for rows stored before the column existed (lazy, idempotent).

        Only ``spec.json`` is read: the shape is a function of the Spec, not a license.
        A row whose Spec cannot be read keeps NULL and is retried next time.
        """
        rows = connection.execute("SELECT key FROM templates WHERE shape IS NULL").fetchall()
        for (key,) in rows:
            directory = self._snapshots / str(key)
            try:
                if re.fullmatch(r"[0-9a-f]{64}", str(key)) is None or directory.is_symlink():
                    continue
                spec = Spec.model_validate_json(
                    (directory / "spec.json").read_text(encoding="utf-8")
                )
            except (OSError, ValueError, RecursionError):
                continue
            connection.execute("UPDATE templates SET shape = ? WHERE key = ?", (_shape(spec), key))

    def _model(self, key: str) -> str | None:
        """The template model from ``config.json``, unauthenticated: display only."""
        try:
            config = json.loads((self._snapshots / key / "config.json").read_text("utf-8"))
        except (OSError, ValueError, RecursionError):
            return None
        model = config.get("model") if isinstance(config, dict) else None
        return model if isinstance(model, str) else None

    def candidates(self, shape: str, model: str, limit: int = 8) -> tuple[TemplateEvidence, ...]:
        """Library rows of this shape and model, in ``sequence`` order (Phase 10 Section 6.1).

        Only the first ``limit`` non-evicted rows are loaded, each with a fresh
        authenticated read; a row that fails it is evicted exactly as in ``lookup`` and
        is returned with ``artifact=None, evicted=False`` (the state this call saw).
        Already evicted rows (at most ``limit``) are returned unloaded so routing can
        show them.
        """
        if type(limit) is not int or limit < 1:
            raise LibraryError("invalid_limit")
        found: list[TemplateEvidence] = []
        with self._connection() as connection:
            self._backfill_shapes(connection)
            rows = connection.execute(
                "SELECT key, successes, failures, evicted, sequence FROM templates "
                "WHERE shape = ? ORDER BY sequence",
                (shape,),
            ).fetchall()
            loaded = 0
            unloaded = 0
            for key, successes, failures, evicted, sequence in rows:
                key = str(key)
                artifact: HarnessArtifact | None = None
                if not evicted:
                    if loaded >= limit or self._model(key) not in (model, None):
                        continue
                    artifact = self._read(connection, key)
                    if artifact is not None and artifact.model != model:
                        continue
                    # A failed read evicts the row, but ``evicted`` stays the state this
                    # call saw, so routing reports it as unlicensed (filter 1).
                    loaded += 1
                else:
                    if unloaded >= limit or self._model(key) != model:
                        continue
                    unloaded += 1
                found.append(
                    TemplateEvidence(
                        key=key,
                        artifact=artifact,
                        successes=int(successes),
                        failures=int(failures),
                        evicted=bool(evicted),
                        sequence=int(sequence),
                        promoted=int(successes) >= self.promotion_reuses,
                    )
                )
        return tuple(found)

    def template(self, key: str) -> HarnessArtifact | None:
        """A fresh authenticated read of one non-evicted snapshot, else None."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT evicted FROM templates WHERE key = ?", (key,)
            ).fetchone()
            if row is None or row[0]:
                return None
            return self._read(connection, key)

    def record_verification(self, source: HarnessArtifact, passed: bool) -> None:
        """Count new-task verification outcomes; two failures cumulatively evict."""
        if type(passed) is not bool:
            raise LibraryError("passed must be a boolean")
        if source.report is None:
            return
        key = source.report.artifact_digest
        with self._connection() as connection:
            row = connection.execute(
                "SELECT evicted FROM templates WHERE key = ?", (key,)
            ).fetchone()
            if row is None or row[0]:
                return
            stored = self._read(connection, key)
            if stored is None or stored.report != source.report or not source.licensed:
                return
            if passed:
                connection.execute(
                    "UPDATE templates SET successes = successes + 1 WHERE key = ?", (key,)
                )
            else:
                connection.execute(
                    "UPDATE templates SET failures = failures + 1, "
                    "evicted = CASE WHEN failures + 1 >= 2 THEN 1 ELSE 0 END WHERE key = ?",
                    (key,),
                )

    def verification_count(self, spec_hash: str) -> int:
        """Forge verifications recorded so far for ``spec_hash`` (0 if none)."""
        if not isinstance(spec_hash, str) or _DIGEST.fullmatch(spec_hash) is None:
            raise LibraryError("invalid_spec_hash")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT count FROM forge_verifications WHERE spec_hash = ?", (spec_hash,)
            ).fetchone()
            return int(row[0]) if row is not None else 0

    def record_forge_verification(self, spec_hash: str) -> None:
        """Persist one more Forge verification for ``spec_hash``; never reset."""
        if not isinstance(spec_hash, str) or _DIGEST.fullmatch(spec_hash) is None:
            raise LibraryError("invalid_spec_hash")
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO forge_verifications(spec_hash, count) VALUES (?, 1) "
                "ON CONFLICT(spec_hash) DO UPDATE SET count = count + 1",
                (spec_hash,),
            )

    def install_candidates(self) -> tuple[str, ...]:
        """Return authenticated promoted keys in insertion order, excluding evictions."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT key FROM templates WHERE evicted = 0 AND successes >= ? ORDER BY sequence",
                (self.promotion_reuses,),
            ).fetchall()
            return tuple(key for (key,) in rows if self._read(connection, key) is not None)


class ReadOnlyLibrary:
    """A lookup-only view of a library (Phase 10 Section 11.6).

    It exposes only ``lookup`` and ``verification_count``: no ``store`` and no
    ``record_verification``, so it is never a ``LibraryLifecycle`` and a Forge child
    compiled through it can warm-start but never writes to the library.
    """

    __slots__ = ("_library",)

    def __init__(self, library: HarnessLibrary) -> None:
        self._library = library

    def lookup(self, spec: Spec, model: str) -> HarnessArtifact | None:
        return self._library.lookup(spec, model)

    def verification_count(self, spec_hash: str) -> int:
        return self._library.verification_count(spec_hash)
