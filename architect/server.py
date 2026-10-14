"""Trusted local HTTP control plane over caller-owned Services.

Exactly five operations: compile, run, status, recompile and route (Phase 10). The
bearer token is checked before any body byte is read. Errors are fixed codes; request content,
exception text, private paths, signatures, profiles and licenses never appear in
responses or logs. Compiler Stage 4 and the Registry remain the only license
and installation authorities. The opaque-ID catalog under ``workspace/server``
is private host state; every entry it names is re-authenticated before use.

``POST /route`` is advice only: it takes an explicit Spec (never prose), spends $0,
runs no Stage 0, no execution and no exploration, never grants reuse, and answers with
closed labels, numbers and fixed codes only. Routing orders licensed versions; Stage 4
alone licenses, and ``/run`` still needs ``enabled`` and ``human_reviewed``.

This API serves a trusted backend, not direct untrusted browser access. Run it
with ``python -m architect.server --profile PROFILE`` (loopback by default).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import stat
import sys
from collections.abc import Awaitable, Callable, Iterator, MutableMapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, NoReturn

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictBool, StringConstraints
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from architect.compiler import CompileResult
from architect.profiles import ProfileError, Services
from architect.registry import RegistryError
from architect.runtime import ArtifactExpiredError, BudgetExceeded, UnlicensedArtifactError
from architect.spec import HarnessArtifact, Lifecycle, Spec, TestReport
from architect.telemetry.store import lock_wait

__all__ = [
    "CompileRequest",
    "CompileResponse",
    "RecompileRequest",
    "RouteRequest",
    "RouteResponse",
    "RunRequest",
    "RunResponse",
    "StatusResponse",
    "create_app",
    "main",
]

MAX_BODY_BYTES = 1 << 20
MAX_REQUEST_CHARS = 20_000
MAX_JSON_DEPTH = 64
# Server-only SQLite write-lock wait for the catalog, registry and telemetry.
# Handlers run storage work off the event loop, and a lock held longer than this
# (for example by a CLI install or another process logging a run) answers with
# the fixed 503 ``storage_busy`` instead of pinning a worker thread for 30 seconds.
LOCK_TIMEOUT_S = 5.0
MIN_TOKEN_CHARS = 32
MAX_TOKEN_CHARS = 512
_ID = r"^[A-Za-z0-9_-]{16,64}$"
_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
_FAILURE_CODES = frozenset(
    {
        "approval_required",
        "artifact_changed",
        "artifact_unreadable",
        "budget_exceeded",
        "compilation_failed",
        "compile_budget",
        "compile_failure",
        "cost_not_lower",
        "empty_model_ladder",
        "evaluation_failed",
        "floor_definition_error",
        "floor_error",
        "floor_mismatch",
        "floor_not_met",
        "floor_unavailable",
        "invalid_components",
        "invalid_diff",
        "invalid_fraction_exceeded",
        "invalid_proposal",
        "load_error",
        "no_candidates",
        "no_cases",
        "not_better",
        "persistence_failed",
        "primary_budget_failed",
        "primary_evaluation_error",
        "primary_evaluation_failed",
        "primary_floor_failed",
        "primary_floor_mismatch",
        "primary_invalid_exceeded",
        "primary_sample_count",
        "proposal_failed",
        "registry_rejected",
        "rewrite_cap",
        "rewrite_failed",
        "runtime_error",
        "schema_error",
        "secondary_budget_failed",
        "secondary_evaluation_error",
        "secondary_evaluation_failed",
        "secondary_floor_failed",
        "secondary_floor_mismatch",
        "secondary_floor_missing",
        "secondary_sample_count",
        "selection_failed",
        "smoke_failed",
        "spec_changed",
        "tuning_configuration_failed",
        "tuning_failed",
        "unexpected_secondary_floor",
        "unsafe_source",
        "unverified",
        # Phase 10 Section 13.3: routing codes.
        "route_spec_required",
        "route_no_candidate",
        "route_unlicensed",
        "route_budget",
        "route_drift",
        "route_model_unconfigured",
        "route_not_catalogued",
        "evidence_store_unavailable",
    }
)
_ROUTE_CODE = r"^[a-z0-9_]{1,64}$"
_ARTIFACT_FILES = (
    "harness.py", "spec.json", "config.json", "report.json", ".consumed",
    "pi-pack.json", "preset.json", "rewrite.diff",
)
ArtifactId = Annotated[str, StringConstraints(pattern=_ID)]
Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
_LOG = logging.getLogger("architect.server")
_BUSY = frozenset({sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})


class ServerError(Exception):
    """A fixed, content-free error code with its HTTP status."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


class _DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class CompileRequest(_DTO):
    request: (
        Annotated[str, StringConstraints(min_length=1, max_length=MAX_REQUEST_CHARS, pattern=r"\S")]
        | None
    ) = None
    spec: Spec | None = None
    approved: StrictBool = False


class RunRequest(_DTO):
    artifact_id: ArtifactId
    input: JsonValue
    enabled: StrictBool = False
    human_reviewed: StrictBool = False


class RecompileRequest(_DTO):
    artifact_id: ArtifactId


class CompileResponse(_DTO):
    accepted: bool
    approval_required: bool
    spec: Spec
    artifact_id: str | None
    failures: tuple[str, ...]
    stages: tuple[int, ...]
    warm_started: bool


class RunResponse(_DTO):
    run_id: str
    output: JsonValue
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    cost_usd: float = Field(ge=0)
    wall_time_s: float = Field(ge=0)


class RouteRequest(_DTO):
    """``{"spec": Spec}``. A prose ``request`` is accepted only to be refused with the
    fixed ``route_spec_required`` (routing over HTTP needs a Spec, not prose)."""

    spec: Spec | None = None
    request: str | None = None


class RouteResponse(_DTO):
    """Content-free: closed labels, numbers and fixed codes; no names, paths or text."""

    action: Literal["reuse_installed", "warm_start", "compile", "refuse"]
    artifact_id: str | None
    fit_tier: Literal["A", "B", "C", "none"]
    fit: float
    quality_lcb: float | None
    expected_cost_usd: float | None
    latency_s: float | None
    latency_basis: Literal["measured", "bound"] | None
    evidence: tuple[str, ...]
    codes: tuple[str, ...]


class StatusResponse(_DTO):
    artifact_id: str
    licensed: bool
    expired: bool
    lifecycle: Lifecycle
    spec_hash: str
    version: int
    registry_name: str | None = None


def _json_response(status: int, payload: object) -> Response:
    try:
        body = json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError):
        status, body = 500, '{"error":"invalid_output"}'
    return Response(
        body,
        status_code=status,
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


def _error(status: int, code: str) -> Response:
    return _json_response(status, {"error": code})


def _busy(exc: BaseException | None) -> bool:
    """True when SQLite gave up waiting for a lock, directly or as the cause."""
    while exc is not None:
        if isinstance(exc, sqlite3.OperationalError) and (
            (getattr(exc, "sqlite_errorcode", -1) & 0xFF) in _BUSY
        ):
            return True
        exc = exc.__cause__
    return False


def _storage_busy() -> ServerError:
    return ServerError(503, "storage_busy")


# ---------------------------------------------------------------- input parsing


def _reject_constant(_: str) -> NoReturn:
    raise ValueError("nonfinite")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("nonfinite")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _within_depth(value: object) -> bool:
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            return False
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
    return True


async def _read_body(request: Request) -> bytes:
    media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        raise ServerError(415, "unsupported_media_type")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise ServerError(413, "request_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _parse[M: BaseModel](body: bytes, model: type[M]) -> M:
    """Reject nonfinite, duplicate-key, deep, extra or mistyped authority data."""
    try:
        text = body.decode("utf-8")
        value = json.loads(
            text,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
            object_pairs_hook=_unique_object,
        )
        if not isinstance(value, dict) or not _within_depth(value):
            raise ValueError("invalid_shape")
        return model.model_validate_json(text, strict=True)
    except (ValueError, RecursionError):
        raise ServerError(400, "invalid_request") from None


def _failure_codes(values: Sequence[str]) -> tuple[str, ...]:
    """Pass through compact compiler codes only; never arbitrary text."""
    codes = [value if value in _FAILURE_CODES else "compile_failure" for value in values]
    return tuple(dict.fromkeys(codes))[:32]


# ------------------------------------------------------------------ path safety


def _private_file(path: Path, *, optional: bool = False) -> None:
    """Reject aliases and non-regular files before reading private host state."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        if optional:
            return
        raise
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ServerError(403, "unlicensed_artifact")


def _no_links(path: Path, root: Path) -> None:
    """Reject links between the trusted root and the artifact, including the root."""
    current = path
    while True:
        if current.is_symlink() or current.is_junction():
            raise ServerError(403, "unlicensed_artifact")
        if current == root or current.parent == current:
            break
        current = current.parent


def _contained(root: Path, directory: Path) -> Path:
    """Return an existing artifact directory strictly inside root, without links."""
    try:
        base = Path(os.path.abspath(root))
        candidate = Path(os.path.abspath(directory))
        if candidate == base or not candidate.is_relative_to(base):
            raise ServerError(403, "unlicensed_artifact")
        _no_links(candidate, base)
        resolved_base = base.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        if (
            os.path.normcase(resolved)
            != os.path.normcase(resolved_base / candidate.relative_to(base))
            or not resolved.is_relative_to(resolved_base)
            or not resolved.is_dir()
        ):
            raise ServerError(403, "unlicensed_artifact")
        for name in _ARTIFACT_FILES:
            # Consumed artifacts intentionally retain metadata but not executable files.
            # Load/license checks require live files; containment also serves expiry status.
            _private_file(resolved / name, optional=True)
        return resolved
    except OSError:
        raise ServerError(403, "unlicensed_artifact") from None


def _relative(root: Path, directory: Path) -> str:
    base = root.resolve(strict=True)
    resolved = _contained(base, directory)
    return resolved.relative_to(base).as_posix()


def _join(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative).parts
    if (
        not parts
        or relative.startswith("/")
        or "\\" in relative
        or ":" in relative
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise ServerError(403, "unlicensed_artifact")
    return root.joinpath(*parts)


# --------------------------------------------------------------------- catalog


class _Entry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: str
    kind: Literal["ephemeral", "installed"]
    location: str
    spec_hash: str
    version: int
    digest: str
    approved_spec: str


MAX_RESERVED_CANDIDATES = 16


def _fingerprint(approved: Spec) -> str:
    """Canonical identity of an exact approved Spec (not just its hash)."""
    canonical = json.dumps(approved.model_dump(mode="json"), sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _encode_claims(claims: Sequence[tuple[str, str]]) -> str:
    return json.dumps([list(item) for item in claims], separators=(",", ":"))


class Catalog:
    """Opaque IDs in private SQLite. A catalog row is a pointer, never a license.

    An installed row with version 0 is a reservation: the server approved that
    Spec and is about to install a candidate. ``digest`` keeps the Stage 4
    ``verification_id`` of each candidate reserved for the row, paired with the
    exact approved Spec it was reserved for. A reservation is never served by
    ID; it only lets a retry prove that the registry's current revision is the
    server's own install.
    """

    def __init__(self, directory: Path, *, lock_timeout: float = LOCK_TIMEOUT_S) -> None:
        self.directory = Path(os.path.abspath(directory))
        self.lock_timeout = lock_timeout
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._check_files(optional=True)
            # Only startup may create the database. Every later open is mode=rw.
            with self._transaction(create=True) as db:
                db.execute("""CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('ephemeral', 'installed')),
                    location TEXT NOT NULL, spec_hash TEXT NOT NULL,
                    version INTEGER NOT NULL, digest TEXT NOT NULL,
                    approved_spec TEXT NOT NULL, created TEXT NOT NULL)""")
                db.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS installed_name ON artifacts "
                    "(location) WHERE kind = 'installed'"
                )
        except OSError:
            raise ServerError(500, "catalog_unavailable") from None

    def _check_files(self, *, optional: bool = False) -> None:
        try:
            _no_links(self.directory, self.directory.parent)
            if not self.directory.is_dir():
                raise OSError
            _private_file(self.directory / "catalog.sqlite3", optional=optional)
            for suffix in ("-journal", "-wal", "-shm"):
                _private_file(self.directory / ("catalog.sqlite3" + suffix), optional=True)
        except (OSError, ServerError):
            raise ServerError(500, "catalog_unavailable") from None

    @contextmanager
    def _transaction(self, *, create: bool = False) -> Iterator[sqlite3.Connection]:
        db: sqlite3.Connection | None = None
        try:
            self._check_files(optional=create)
            mode = "rwc" if create else "rw"
            uri = (self.directory / "catalog.sqlite3").as_uri() + "?mode=" + mode
            db = sqlite3.connect(uri, uri=True, timeout=self.lock_timeout)
            self._check_files()
            db.execute("PRAGMA synchronous = FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except sqlite3.Error as exc:
            if _busy(exc):
                raise _storage_busy() from None
            raise ServerError(500, "catalog_unavailable") from None
        finally:
            if db is not None:
                db.close()

    _COLUMNS = "artifact_id, kind, location, spec_hash, version, digest, approved_spec"

    def get(self, artifact_id: str) -> _Entry:
        with self._transaction() as db:
            row = db.execute(
                f"SELECT {self._COLUMNS} FROM artifacts WHERE artifact_id=?", (artifact_id,)
            ).fetchone()
        if row is None:
            raise ServerError(404, "unknown_artifact")
        entry = self._entry(row)
        if entry.kind == "installed" and entry.version == 0:
            raise ServerError(404, "unknown_artifact")  # A reservation was never issued.
        return entry

    def find_installed(self, name: str) -> _Entry | None:
        with self._transaction() as db:
            row = db.execute(
                f"SELECT {self._COLUMNS} FROM artifacts WHERE kind='installed' AND location=?",
                (name,),
            ).fetchone()
        return None if row is None else self._entry(row)

    @staticmethod
    def _entry(row: Sequence[Any]) -> _Entry:
        try:
            return _Entry(
                artifact_id=row[0],
                kind=row[1],
                location=row[2],
                spec_hash=row[3],
                version=row[4],
                digest=row[5],
                approved_spec=row[6],
            )
        except ValueError:
            raise ServerError(500, "catalog_unavailable") from None

    def add_ephemeral(self, location: str, artifact: HarnessArtifact) -> str:
        artifact_id = secrets.token_urlsafe(24)
        with self._transaction() as db:
            db.execute(
                "INSERT INTO artifacts VALUES (?, 'ephemeral', ?, ?, ?, ?, '', ?)",
                (
                    artifact_id,
                    location,
                    artifact.spec.spec_hash,
                    artifact.version,
                    artifact.artifact_digest,
                    datetime.now(UTC).isoformat(),
                ),
            )
        return artifact_id

    def reserve(self, name: str, approved: Spec, verification_id: str) -> None:
        """Record an approval before the registry install it precedes.

        A live row is left alone. A reservation remembers every candidate the
        server itself is about to install (bounded, newest last), each bound to
        the exact approved Spec it was reserved for, so an overlapping approval
        of the same Spec cannot erase the one whose install actually committed.
        """
        claim = (verification_id, _fingerprint(approved))
        with self._transaction() as db:
            row = db.execute(
                "SELECT version, digest, approved_spec FROM artifacts "
                "WHERE kind='installed' AND location=?",
                (name,),
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO artifacts VALUES (?, 'installed', ?, ?, 0, ?, ?, ?)",
                    (
                        secrets.token_urlsafe(24),
                        name,
                        approved.spec_hash,
                        _encode_claims([claim]),
                        approved.model_dump_json(),
                        datetime.now(UTC).isoformat(),
                    ),
                )
            elif row[0] == 0:
                claims = [item for item in self.claims(row[1], row[2]) if item != claim]
                claims = [*claims, claim][-MAX_RESERVED_CANDIDATES:]
                db.execute(
                    "UPDATE artifacts SET digest=?, spec_hash=?, approved_spec=? "
                    "WHERE kind='installed' AND location=?",
                    (_encode_claims(claims), approved.spec_hash, approved.model_dump_json(), name),
                )

    @staticmethod
    def claims(digest: str, approved_spec: str) -> list[tuple[str, str]]:
        """Reserved ``(verification_id, approved Spec fingerprint)`` pairs of a row."""
        if not digest:
            return []
        if not digest.startswith("["):
            # A single-candidate reservation written before claims were kept.
            try:
                legacy = Spec.model_validate_json(approved_spec)
            except ValueError:
                return []
            return [(digest, _fingerprint(legacy))]
        try:
            raw = json.loads(digest)
        except ValueError:
            raise ServerError(500, "catalog_unavailable") from None
        if not isinstance(raw, list) or not all(
            isinstance(item, list)
            and len(item) == 2
            and all(isinstance(part, str) and part for part in item)
            for item in raw
        ):
            raise ServerError(500, "catalog_unavailable")
        return [(item[0], item[1]) for item in raw]

    def upsert_installed(self, name: str, approved: Spec, version: int) -> str:
        """Keep one stable ID per registry name; bind the exact approved Spec.

        Setting a positive version also turns a reservation into a live row.
        """
        with self._transaction() as db:
            row = db.execute(
                "SELECT artifact_id FROM artifacts WHERE kind='installed' AND location=?", (name,)
            ).fetchone()
            if row is not None:
                # Promoting a reservation binds the Spec this install approved.
                db.execute(
                    "UPDATE artifacts SET version=?, approved_spec=CASE WHEN version=0 "
                    "THEN ? ELSE approved_spec END WHERE artifact_id=?",
                    (version, approved.model_dump_json(), row[0]),
                )
                return str(row[0])
            artifact_id = secrets.token_urlsafe(24)
            db.execute(
                "INSERT INTO artifacts VALUES (?, 'installed', ?, ?, ?, '', ?, ?)",
                (
                    artifact_id,
                    name,
                    approved.spec_hash,
                    version,
                    approved.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )
            return artifact_id


# ------------------------------------------------------------------- the service


class _Snapshot(BaseModel):
    """Verification IDs seen before a compile, plus paths that could not be read.

    ``untrusted`` holds absolute, lexical paths whose reports were skipped, and
    ``identities`` their filesystem identities ``(st_dev, st_ino)``: a result
    located at or below one of them is never accepted as fresh. Identities
    catch aliases a string cannot, such as a differently cased path on a
    case-insensitive volume.
    """

    model_config = ConfigDict(frozen=True)

    identifiers: frozenset[str]
    untrusted: tuple[str, ...]
    identities: frozenset[tuple[int, int]] = frozenset()

    def stale(self, result: CompileResult) -> bool:
        if result.report is not None and result.report.verification_id in self.identifiers:
            return True
        if result.artifact is None:
            return False
        absolute = os.path.abspath(result.artifact.directory)
        location = os.path.normcase(absolute)
        if any(
            location == path or location.startswith(path.rstrip(os.sep) + os.sep)
            for path in self.untrusted
        ):
            return True
        if not self.identities:
            return False
        current = Path(absolute)
        for candidate in (current, *current.parents):
            try:
                info = candidate.lstat()
            except OSError:
                continue  # A missing location cannot load and is refused later.
            if (info.st_dev, info.st_ino) in self.identities:
                return True
        return False


class _Service:
    """Sync methods touch disk or SQLite; the async ones run them via to_thread."""

    def __init__(self, services: Services) -> None:
        self.services = services
        self.catalog = Catalog(services.workspace / "server", lock_timeout=LOCK_TIMEOUT_S)
        # A server-local view: the CLI and the caller keep the registry's own wait.
        self.registry = services.registry.with_lock_timeout(LOCK_TIMEOUT_S)

    @property
    def artifact_root(self) -> Path:
        return Path(os.path.abspath(self.services.compiler.artifact_root))

    @property
    def registry_root(self) -> Path:
        return Path(os.path.abspath(self.services.registry.root))

    # compile ---------------------------------------------------------------

    async def compile(self, body: CompileRequest) -> CompileResponse:
        if (body.request is None) == (body.spec is None):
            raise ServerError(400, "exactly_one_source")
        if body.approved and body.spec is None:
            raise ServerError(400, "approval_requires_spec")
        source: Spec | str = body.spec if body.spec is not None else body.request or ""
        result = await self._compile(source, approved=body.approved)
        return await asyncio.to_thread(self._conclude, body, result)

    def _conclude(self, body: CompileRequest, result: CompileResult) -> CompileResponse:
        if body.spec is not None and result.spec != body.spec:
            return self._rejected(result, "spec_changed")
        if result.approval_required:
            return CompileResponse(
                accepted=False,
                approval_required=True,
                spec=result.spec,
                artifact_id=None,
                failures=(),
                stages=result.stages,
                warm_started=False,
            )
        artifact = self._verified(result)
        if artifact is None:
            return self._rejected(result)
        if artifact.spec.lifecycle == Lifecycle.INSTALL:
            if not body.approved or body.spec is None:
                # Unreachable through the Compiler; an inferred Spec never installs.
                return self._rejected(result, "approval_required")
            name = artifact.spec.spec_hash
            self._heal(name, body.spec)
            response = self._install(result, artifact, name, body.spec)
            if response.failures == ("not_better",):
                return self._existing(response, body.spec)
            return response
        location = _relative(self.artifact_root, artifact.directory)
        artifact_id = self.catalog.add_ephemeral(location, artifact)
        return self._accepted(result, artifact_id)

    def _existing(self, response: CompileResponse, approved: Spec) -> CompileResponse:
        """A retried approval of the exact installed Spec gets its stable ID back.

        Nothing is replaced: ``accepted`` stays false and ``not_better`` stays.
        The ID is returned only for the same approved Spec whose incumbent still
        authenticates, which the caller could already install and run.
        """
        entry = self.catalog.find_installed(approved.spec_hash)
        if entry is None or entry.version == 0:
            return response  # A reservation that did not heal was never this install.
        try:
            if self._approved_spec(entry) != approved:
                return response
            self._installed(entry)
        except ServerError as exc:
            if exc.code == "storage_busy":
                raise
            return response
        return response.model_copy(update={"artifact_id": entry.artifact_id})

    def _heal(self, name: str, approved: Spec) -> None:
        """Finish an approved install whose catalog write was lost after it committed.

        ``_install`` reserves the row before the registry commits and promotes it
        after; contention in between leaves a reservation. It is promoted only
        when the registry's current revision authenticates, is licensed, carries
        exactly this approved Spec and is one of the candidates the server
        reserved for this exact Spec (by Stage 4 ``verification_id``), even if
        an overlapping approval reserved a newer one since. An installation made
        outside the server, even of the same Spec, never matches and never gets
        an ID.
        """
        entry = self.catalog.find_installed(name)
        if entry is None or entry.version != 0 or not entry.digest:
            return
        fingerprint = _fingerprint(approved)
        try:
            claims = [
                verification_id
                for verification_id, bound in Catalog.claims(entry.digest, entry.approved_spec)
                if hmac.compare_digest(bound, fingerprint)
            ]
            if not claims:
                return
            artifact = self._installed(entry)
        except ServerError as exc:
            if exc.code == "storage_busy":
                raise
            return
        report = artifact.report
        if (
            artifact.spec != approved
            or report is None
            or not any(hmac.compare_digest(report.verification_id, item) for item in claims)
        ):
            return
        self.catalog.upsert_installed(name, approved, artifact.version)

    def _verification_ids(self) -> _Snapshot:
        """Snapshot persisted evidence before invoking the trusted compiler.

        This rejects stale-result integration regressions, not a privileged writer
        swapping files during compilation. A catalog pointer is not evidence of
        freshness; include reports from direct compiler calls and old revisions.

        One damaged entry must not deny every compile, and must never be trusted
        either: dot-prefixed entries (``.install-*`` crash remnants, temp files),
        links, unreadable directories and reports that are aliased, hard-linked or
        unparsable are skipped, counted and recorded as untrusted locations. A
        root that is itself an alias or cannot be listed still fails closed.
        """
        identifiers: set[str] = set()
        untrusted: list[str] = []
        identities: set[tuple[int, int]] = set()

        def skip(path: Path) -> None:
            untrusted.append(os.path.normcase(os.path.abspath(path)))
            try:
                info = path.lstat()
            except OSError:
                return  # Unstattable now; the lexical rule still covers it.
            identities.add((info.st_dev, info.st_ino))

        try:
            for root in (self.artifact_root, self.registry_root):
                _no_links(root, root)
                if not root.exists():
                    continue
                pending = [list(root.iterdir())]
                while pending:
                    for path in pending.pop():
                        try:
                            if path.name.startswith(".") or path.is_symlink() or path.is_junction():
                                skip(path)
                            elif path.is_dir():
                                pending.append(list(path.iterdir()))
                            elif path.name == "report.json":
                                _private_file(path)
                                report = TestReport.model_validate_json(
                                    path.read_text(encoding="utf-8")
                                )
                                identifiers.add(report.verification_id)
                        except (OSError, ValueError, ServerError):
                            skip(path.parent if path.name == "report.json" else path)
        except (OSError, ValueError, ServerError):
            raise ServerError(500, "compile_failure") from None
        if untrusted:
            _LOG.warning("freshness snapshot skipped %d untrusted entries", len(untrusted))
        return _Snapshot(
            identifiers=frozenset(identifiers),
            untrusted=tuple(untrusted),
            identities=frozenset(identities),
        )

    async def _compile(self, source: Spec | str, *, approved: bool) -> CompileResult:
        previous = await asyncio.to_thread(self._verification_ids)
        try:
            with lock_wait(LOCK_TIMEOUT_S):
                # Phase 10: the http origin is advice-only for reuse and never explores.
                result = await self.services.compiler.compile(
                    source, approved=approved, origin="http"
                )
        except BudgetExceeded:
            raise ServerError(422, "budget_exceeded") from None
        except (ProfileError, ImportError):
            raise ServerError(503, "backend_unavailable") from None
        except Exception as exc:
            if _busy(exc):
                raise _storage_busy() from None
            raise
        if previous.stale(result):
            return result.model_copy(update={"artifact": None, "failures": ("unverified",)})
        return result

    def _verified(self, result: CompileResult) -> HarnessArtifact | None:
        """Accept only a fresh Stage 4 license for exactly the compiled Spec."""
        artifact = result.artifact
        if (
            artifact is None
            or result.failures
            or 4 not in result.stages
            or result.report is None
            or result.report != artifact.report
            or artifact.spec != result.spec
        ):
            return None
        try:
            directory = _contained(self.artifact_root, artifact.directory)
            persisted = HarnessArtifact.load(directory)
            if (
                persisted != artifact.model_copy(update={"directory": directory})
                or persisted.report != result.report
                or not persisted.licensed
            ):
                return None
        except (ServerError, OSError, ValueError, TypeError):
            return None
        return persisted

    def _registry_files(self) -> None:
        """Check registry control files before its transaction can write them."""
        try:
            _no_links(self.registry_root, self.registry_root)
            _private_file(self.registry_root / "registry.sqlite3")
            for suffix in ("-journal", "-wal", "-shm"):
                _private_file(self.registry_root / ("registry.sqlite3" + suffix), optional=True)
        except OSError:
            raise ServerError(403, "unlicensed_artifact") from None

    def _install(
        self, result: CompileResult, artifact: HarnessArtifact, name: str, approved: Spec
    ) -> CompileResponse:
        self._registry_files()
        assert artifact.report is not None  # _verified binds the Stage 4 report.
        self.catalog.reserve(name, approved, artifact.report.verification_id)
        try:
            entry = self.registry.install(name, artifact)
        except RegistryError as exc:
            if _busy(exc.__cause__):
                raise _storage_busy() from None
            code = "not_better" if str(exc) == "not_better" else "registry_rejected"
            return self._rejected(result, code)
        artifact_id = self.catalog.upsert_installed(name, approved, entry.artifact_version)
        return self._accepted(result, artifact_id)

    @staticmethod
    def _accepted(result: CompileResult, artifact_id: str) -> CompileResponse:
        return CompileResponse(
            accepted=True,
            approval_required=False,
            spec=result.spec,
            artifact_id=artifact_id,
            failures=(),
            stages=result.stages,
            warm_started=result.warm_started,
        )

    @staticmethod
    def _rejected(result: CompileResult, *extra: str) -> CompileResponse:
        failures = _failure_codes([*result.failures, *extra]) or ("unverified",)
        return CompileResponse(
            accepted=False,
            approval_required=False,
            spec=result.spec,
            artifact_id=None,
            failures=failures,
            stages=result.stages,
            warm_started=result.warm_started,
        )

    # resolution -------------------------------------------------------------

    def _approved_spec(self, entry: _Entry) -> Spec:
        try:
            spec = Spec.model_validate_json(entry.approved_spec)
        except ValueError:
            raise ServerError(500, "catalog_unavailable") from None
        if spec.spec_hash != entry.spec_hash or spec.lifecycle != Lifecycle.INSTALL:
            raise ServerError(500, "catalog_unavailable")
        return spec

    def _installed(self, entry: _Entry) -> HarnessArtifact:
        """Registry-authenticated current revision bound to the approved Spec."""
        self._registry_files()
        try:
            registry_entry = self.registry.status(entry.location)
            artifact = self.registry.get(entry.location)
        except RegistryError as exc:
            if _busy(exc.__cause__):
                raise _storage_busy() from None
            raise ServerError(403, "unlicensed_artifact") from None
        _contained(self.registry_root, registry_entry.directory)
        _contained(self.registry_root, artifact.directory)
        if (
            artifact.spec.spec_hash != entry.spec_hash
            or artifact.spec.lifecycle != Lifecycle.INSTALL
            or not artifact.licensed
        ):
            raise ServerError(403, "unlicensed_artifact")
        return artifact

    def _ephemeral_directory(self, entry: _Entry) -> Path:
        return _contained(self.artifact_root, _join(self.artifact_root, entry.location))

    def _ephemeral(self, entry: _Entry) -> HarnessArtifact:
        directory = self._ephemeral_directory(entry)
        if (directory / ".consumed").exists():
            raise ServerError(410, "artifact_expired")
        try:
            artifact = HarnessArtifact.load(directory)
            digest = artifact.artifact_digest
        except (OSError, ValueError, TypeError):
            raise ServerError(403, "unlicensed_artifact") from None
        if (
            artifact.spec.lifecycle != Lifecycle.EXPIRE
            or artifact.spec.spec_hash != entry.spec_hash
            or artifact.version != entry.version
            or not hmac.compare_digest(digest, entry.digest)
            or not artifact.licensed
        ):
            raise ServerError(403, "unlicensed_artifact")
        return artifact

    # run ---------------------------------------------------------------------

    def _deliverable(self, body: RunRequest) -> HarnessArtifact:
        entry = self.catalog.get(body.artifact_id)
        if entry.kind == "installed":
            if not (body.enabled and body.human_reviewed):
                raise ServerError(403, "delivery_not_enabled")
            return self._installed(entry)
        return self._ephemeral(entry)

    async def run(self, body: RunRequest) -> RunResponse:
        artifact = await asyncio.to_thread(self._deliverable, body)
        try:
            adapter = self.services.adapter_factory(artifact.model)
        except (ProfileError, ImportError):
            raise ServerError(503, "backend_unavailable") from None
        try:
            # Telemetry waits run in worker threads and are bounded here. A busy
            # log is 503 storage_busy and never a delivery: the output is dropped.
            with lock_wait(LOCK_TIMEOUT_S):
                result = await self.services.runtime.run(artifact, body.input, adapter)
        except ArtifactExpiredError:
            raise ServerError(410, "artifact_expired") from None
        except UnlicensedArtifactError:
            raise ServerError(403, "unlicensed_artifact") from None
        except BudgetExceeded:
            raise ServerError(422, "budget_exceeded") from None
        except ImportError:
            raise ServerError(503, "backend_unavailable") from None
        except Exception as exc:
            if _busy(exc):
                raise _storage_busy() from None
            raise ServerError(500, "run_failed") from None
        return RunResponse(
            run_id=result.run_id,
            output=result.output,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cost_usd=result.cost_usd,
            wall_time_s=result.wall_time_s,
        )

    # status ------------------------------------------------------------------

    def status(self, artifact_id: str) -> StatusResponse:
        entry = self.catalog.get(artifact_id)
        if entry.kind == "installed":
            # HTTP installations use the exact approved Spec hash as the name.
            # Never reflect an arbitrary private catalog location into a DTO.
            approved = self._approved_spec(entry)
            if entry.location != approved.spec_hash:
                raise ServerError(500, "catalog_unavailable")
            try:
                artifact = self._installed(entry)
                licensed, version = True, artifact.version
            except ServerError as exc:
                if exc.code == "storage_busy":
                    raise  # Contention is not evidence that the license is gone.
                licensed, version = False, entry.version
            return StatusResponse(
                artifact_id=artifact_id,
                licensed=licensed,
                expired=False,
                lifecycle=Lifecycle.INSTALL,
                spec_hash=entry.spec_hash,
                version=version,
                registry_name=entry.location,
            )
        expired = False
        try:
            expired = (self._ephemeral_directory(entry) / ".consumed").exists()
            licensed = not expired and self._ephemeral(entry) is not None
        except ServerError:
            licensed = False
        return StatusResponse(
            artifact_id=artifact_id,
            licensed=licensed,
            expired=expired,
            lifecycle=Lifecycle.EXPIRE,
            spec_hash=entry.spec_hash,
            version=entry.version,
        )

    # recompile ---------------------------------------------------------------

    def _incumbent(self, artifact_id: str) -> tuple[_Entry, Spec]:
        entry = self.catalog.get(artifact_id)
        if entry.kind != "installed":
            raise ServerError(409, "not_installed")
        approved = self._approved_spec(entry)
        self._installed(entry)  # The incumbent must still be authentic to be replaced.
        return entry, approved

    def _replace(self, entry: _Entry, approved: Spec, result: CompileResult) -> CompileResponse:
        if result.spec != approved or result.approval_required:
            return self._rejected(result, "spec_changed")
        artifact = self._verified(result)
        if artifact is None:
            return self._rejected(result)
        # Registry acceptance is strict: tied/worse or broader budgets never replace.
        return self._install(result, artifact, entry.location, approved)

    async def recompile(self, body: RecompileRequest) -> CompileResponse:
        entry, approved = await asyncio.to_thread(self._incumbent, body.artifact_id)
        result = await self._compile(approved, approved=True)
        return await asyncio.to_thread(self._replace, entry, approved, result)


    # route -------------------------------------------------------------------

    def route(self, body: RouteRequest) -> RouteResponse:
        """Section 13.3: pure advice over an explicit Spec; $0, nothing runs."""
        if body.spec is None or body.request is not None:
            raise ServerError(400, "route_spec_required")
        from architect.routing.router import RoutePolicy, TaskProbe, route

        spec = body.spec
        compiler = self.services.compiler
        ladder = (
            compiler.config.model_ladder
            if spec.model_target == "cheapest that passes"
            else (spec.model_target,)
        )
        probe = TaskProbe(origin="http", spec=spec, budget_cap=spec.budget, ladder=ladder)
        router = self.services.router
        try:
            with lock_wait(LOCK_TIMEOUT_S):
                if router is None:
                    policy = RoutePolicy.model_validate({"enabled": False})
                    decision = route(probe, index=_NoEvidence(), policy=policy)
                else:
                    decision = route(probe, index=router.index, policy=router.policy)
        except Exception as exc:
            if _busy(exc):
                raise _storage_busy() from None
            raise ServerError(500, "evidence_store_unavailable") from None
        views = decision.candidates
        chosen = views[decision.chosen_rank] if decision.chosen_rank is not None else None
        shown = chosen or next((view for view in views if view.advice), None)
        if shown is None and views:
            shown = min(views, key=lambda view: "ABC".index(view.tier))
        codes = [code for code in decision.codes if re.fullmatch(_ROUTE_CODE, code)]
        artifact_id: str | None = None
        tier_a = next(
            (view for view in views if view.tier == "A" and view.origin == "installed"), None
        )
        if tier_a is not None:
            name = getattr(tier_a.ref, "name", None)
            entry = self.catalog.find_installed(name) if isinstance(name, str) else None
            if entry is not None and entry.version != 0:
                artifact_id = entry.artifact_id
            else:
                codes.append("route_not_catalogued")
        return RouteResponse(
            action=decision.action,
            artifact_id=artifact_id,
            fit_tier=shown.tier if shown is not None else "none",
            fit=shown.fit if shown is not None else 0.0,
            quality_lcb=shown.m_lcb if shown is not None else None,
            expected_cost_usd=shown.cost_usd if shown is not None else None,
            latency_s=shown.latency_s if shown is not None else None,
            latency_basis=shown.latency_basis if shown is not None else None,
            evidence=tuple(shown.evidence) if shown is not None else (),
            codes=tuple(dict.fromkeys(codes))[:32],
        )


class _NoEvidence:
    """Never read: a disabled policy decides ``compile`` before any evidence read."""

    def evidence_class(self, model: str) -> None:
        return None

    def installed(self, sig: object) -> tuple[()]:
        return ()

    def templates(self, sig: object, models: tuple[str, ...], limit: int) -> tuple[()]:
        return ()

    def stage4_history(self, sig: object) -> tuple[bool, int, int]:
        return False, 0, 0

    def earlier_revisions(self, name: str, current: int) -> tuple[()]:
        return ()


# ------------------------------------------------------------------- ASGI layer


class _Guard:
    """Authenticate and bound requests before routing or reading any body byte."""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]], token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode("ascii")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            if scope["type"] == "lifespan":
                await self.app(scope, receive, send)
            return  # WebSockets and other protocols are not part of the contract.
        headers = [value for key, value in scope.get("headers", ()) if key == b"authorization"]
        supplied = headers[0] if len(headers) == 1 else b""
        if not hmac.compare_digest(supplied, self.expected):
            await _error(401, "unauthorized")(scope, receive, send)
            return
        lengths = [value for key, value in scope.get("headers", ()) if key == b"content-length"]
        if len(lengths) > 1 or (lengths and (not lengths[0].isdigit() or len(lengths[0]) > 12)):
            await _error(400, "invalid_request")(scope, receive, send)
            return
        if lengths and int(lengths[0]) > MAX_BODY_BYTES:
            await _error(413, "request_too_large")(scope, receive, send)
            return
        started = False

        async def tracked(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracked)
        except Exception:
            # Never let exception text reach server logs or clients.
            if not started:
                await _error(500, "internal_error")(scope, receive, send)


def _valid_token(token: object) -> bool:
    return (
        isinstance(token, str)
        and MIN_TOKEN_CHARS <= len(token) <= MAX_TOKEN_CHARS
        and token.isascii()
        and token.isprintable()
        and not any(character.isspace() for character in token)
    )


def create_app(services: Services, *, api_token: str) -> FastAPI:
    """Build the five-operation service. Services stay caller-owned and open."""
    if not _valid_token(api_token):
        raise ValueError("invalid_api_token")
    service = _Service(services)
    app = FastAPI(
        docs_url=None, redoc_url=None, openapi_url=None, swagger_ui_oauth2_redirect_url=None
    )

    async def handle(work: Callable[[], Awaitable[BaseModel]]) -> Response:
        try:
            payload = await work()
        except ServerError as exc:
            return _error(exc.status, exc.code)
        except Exception:
            return _error(500, "internal_error")
        return _json_response(
            200, payload.model_dump(mode="json", exclude_none=isinstance(payload, StatusResponse))
        )

    @app.post("/compile", include_in_schema=False)
    async def compile_(request: Request) -> Response:
        async def work() -> BaseModel:
            return await service.compile(_parse(await _read_body(request), CompileRequest))

        return await handle(work)

    @app.post("/run", include_in_schema=False)
    async def run(request: Request) -> Response:
        async def work() -> BaseModel:
            return await service.run(_parse(await _read_body(request), RunRequest))

        return await handle(work)

    @app.get("/status/{artifact_id}", include_in_schema=False)
    async def status(artifact_id: str) -> Response:
        async def work() -> BaseModel:
            if not 16 <= len(artifact_id) <= 64 or not set(artifact_id) <= _ID_CHARS:
                raise ServerError(400, "invalid_request")
            return await asyncio.to_thread(service.status, artifact_id)

        return await handle(work)

    @app.post("/recompile", include_in_schema=False)
    async def recompile(request: Request) -> Response:
        async def work() -> BaseModel:
            return await service.recompile(_parse(await _read_body(request), RecompileRequest))

        return await handle(work)

    @app.post("/route", include_in_schema=False)
    async def route_(request: Request) -> Response:
        async def work() -> BaseModel:
            body = _parse(await _read_body(request), RouteRequest)
            return await asyncio.to_thread(service.route, body)

        return await handle(work)

    async def http_error(request: Request, exc: Exception) -> Response:
        # Replace default detail bodies, which can echo request content.
        code = exc.status_code if isinstance(exc, HTTPException) else 400
        fixed = {404: "not_found", 405: "method_not_allowed"}
        if code in fixed:
            return _error(code, fixed[code])
        return _error(500, "internal_error") if code >= 500 else _error(400, "invalid_request")

    app.add_exception_handler(HTTPException, http_error)
    app.add_exception_handler(RequestValidationError, http_error)
    app.add_middleware(_Guard, token=api_token)
    return app


# --------------------------------------------------------------- local helper


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError("invalid_arguments")


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv: Sequence[str] | None = None) -> int:
    """Serve a trusted profile on loopback; this helper owns Services cleanup."""
    import uvicorn

    from architect.profiles import build_services, load_profile

    parser = _Parser(prog="python -m architect.server")
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token-env", default="ARCHITECT_API_TOKEN")
    parser.add_argument("--allow-non-loopback", action="store_true")
    try:
        args = parser.parse_args(argv)
        if not 0 < args.port < 65536:
            raise ValueError("invalid_arguments")
        if not _loopback(args.host) and not args.allow_non_loopback:
            raise ValueError("non_loopback_host")
        if not args.token_env.isidentifier():
            raise ValueError("invalid_arguments")
        token = os.environ.get(args.token_env, "")
        if not _valid_token(token):
            raise ValueError("missing_api_token")
        profile = load_profile(args.profile)
        services = build_services(profile, profile.workspace)
    except SystemExit as exc:
        return int(exc.code or 0)
    except (ValueError, ProfileError) as exc:
        known = {"invalid_arguments", "non_loopback_host", "missing_api_token"}
        code = str(exc) if str(exc) in known else "invalid_profile"
        print(json.dumps({"error": code}), file=sys.stderr)
        return 1
    try:
        app = create_app(services, api_token=token)
        config = uvicorn.Config(
            app,
            host=args.host,
            port=args.port,
            access_log=False,
            server_header=False,
            proxy_headers=False,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        try:
            asyncio.run(server.serve())
        except KeyboardInterrupt:
            return 130
        return 0
    except Exception:
        print(json.dumps({"error": "server_failed"}), file=sys.stderr)
        return 1
    finally:
        services.close()


if __name__ == "__main__":
    raise SystemExit(main())
