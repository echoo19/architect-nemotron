"""The Forge audit (Phase 14, Section 8): every submission, copied where Forge cannot read.

``forge_audit(directory)`` sets a context variable for the compiles started inside it
(a ``forge_spawn`` child compile inherits it from its parent's task); it is never a
process-wide observer. While it is set, ``forge()`` opens one ``AuditSession`` per
Forge session, keeps the directory out of the agent's reach, and ``submit_source``
records each submission once, after its verdict:

    <DIR>/<spec_hash>/<session_id>/session.json
    <DIR>/<spec_hash>/<session_id>/<n>/harness.py       the submitted source, exact bytes
    <DIR>/<spec_hash>/<session_id>/<n>/spec.json         copied when the candidate was written
    <DIR>/<spec_hash>/<session_id>/<n>/config.json       (with subharnesses/ when it has any,
                                                          each child without its report.json)
    <DIR>/<spec_hash>/<session_id>/<n>/submission.json   written last

Records hold the submitted code, its spec and tuning-side numbers and codes only,
including the hash of the practice cases a measurement ran on, so a replay can
refuse a floor the submission never practised on. Nothing comes from the held-out
partition, from Stage 4 or from a report: a bound child's signed report is left out
of its copy. No compile reads the directory; only the bench audit replay does, after
the fact, and it never replays a submission that binds children. A write that fails
raises ``AuditError`` and marks the session failed, so ``forge()`` promotes nothing
from it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from architect.compiler.types import Measurement
from architect.floors import EvaluationCase
from architect.floors.provenance import cases_sha256
from architect.spec import SHA256, CaseCounts, HarnessArtifact

__all__ = [
    "AUDIT_ENV",
    "AUDIT_VERSION",
    "AuditError",
    "AuditSession",
    "AuditSessionRecord",
    "AuditSubmission",
    "AuditTuning",
    "ForgeAudit",
    "audit_directory",
    "audit_scope",
    "current_audit",
    "forge_audit",
]

AUDIT_VERSION: Final = 1
AUDIT_ENV: Final = "ARCHITECT_FORGE_AUDIT_DIR"
# Files of a bound child that the audit leaves out: its signed Stage 4 report, and the
# workspace markers ``forge_tools._materialize`` already skips.
_CHILD_EXCLUDED: Final = ("report.json", ".consumed", ".pending-*")

SessionId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
Code = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,64}$")]
ModelKey = Annotated[str, StringConstraints(min_length=1, max_length=256)]
Mode = Literal["agent", "template"]
_Count = Annotated[int, Field(ge=0)]


class AuditError(OSError):
    """The audit directory or one of its records could not be written."""


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class AuditTuning(_Record):
    """The tuning ``Measurement`` of one submission: tuning-side numbers and codes."""

    score: Annotated[float, Field(ge=0, le=1)]
    floor_passed: bool
    budget_passed: bool
    mean_cost_usd: Annotated[float, Field(ge=0)]
    max_cost_usd: Annotated[float, Field(ge=0)]
    max_compute_s: Annotated[float, Field(ge=0)]
    sample_count: _Count
    counts: CaseCounts | None
    failures: tuple[str, ...]
    # ``cases_sha256`` of the practice cases the measurement ran on, in order (None in
    # records written before it existed). Tuning-side only: Forge sees these cases.
    cases_sha256: SHA256 | None = None

    @classmethod
    def of(
        cls, measurement: Measurement, cases: Sequence[EvaluationCase] = ()
    ) -> AuditTuning:
        return cls(
            score=measurement.score,
            floor_passed=measurement.floor_passed,
            budget_passed=measurement.budget_passed,
            mean_cost_usd=measurement.mean_cost_usd,
            max_cost_usd=measurement.max_cost_usd,
            max_compute_s=measurement.max_compute_s,
            sample_count=measurement.sample_count,
            counts=measurement.counts,
            failures=measurement.failures,
            cases_sha256=cases_sha256(cases) if cases else None,
        )


class AuditSubmission(_Record):
    """``submission.json``: one ``submit_source`` call and its verdict."""

    audit_version: Literal[1]
    spec_hash: SHA256
    session_id: SessionId
    submission: Annotated[int, Field(ge=1)]
    depth: _Count
    mode: Mode
    model: ModelKey
    agent_model: ModelKey | None
    code_sha256: SHA256
    artifact_digest: SHA256 | None  # None when the candidate was never written
    verdict: Code  # "accepted" or the code submit_source returned
    floor_limit: Annotated[float, Field(ge=0, le=1)]
    tuning: AuditTuning | None  # None when no tuning measurement was taken
    smoke_passed: bool | None
    recorded_at: str  # ISO 8601 UTC


class AuditSessionRecord(_Record):
    """``session.json``: one Forge session; rewritten after every record and at close."""

    audit_version: Literal[1]
    spec_hash: SHA256
    session_id: SessionId
    depth: _Count
    mode: Mode
    model: ModelKey
    agent_model: ModelKey | None
    started_at: str
    ended_at: str | None
    submissions: _Count
    promoted: Annotated[int, Field(ge=1)] | None  # Forge's chosen submission, by tuning rank
    failures: tuple[Code, ...]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _write_json(path: Path, record: BaseModel) -> None:
    """Sorted keys, two-space indent, ASCII, LF; replaced atomically."""
    text = json.dumps(
        record.model_dump(mode="json"), sort_keys=True, indent=2, ensure_ascii=True,
        allow_nan=False,
    )
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as stream:
            stream.write(text + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class ForgeAudit:
    """One audit directory; every Forge session opened under it gets its own folder."""

    def __init__(self, directory: Path) -> None:
        """Create the directory and write then remove a probe file; ``AuditError`` when
        either fails, so a caller can report ``audit_unwritable`` before any spend."""
        try:
            resolved = Path(directory).expanduser().resolve()
            resolved.mkdir(parents=True, exist_ok=True)
            probe = resolved / f".probe-{uuid.uuid4().hex}"
            probe.write_bytes(b"")
            probe.unlink()
        except (OSError, ValueError) as exc:
            raise AuditError("audit_unwritable") from exc
        self.directory = resolved

    def open_session(
        self,
        *,
        spec_hash: str,
        model: str,
        agent_model: str | None,
        mode: Literal["agent", "template"],
        depth: int,
    ) -> AuditSession:
        return AuditSession(
            self, spec_hash=spec_hash, model=model, agent_model=agent_model, mode=mode,
            depth=depth,
        )


class AuditSession:
    """The records of one Forge session. Any failed write raises ``AuditError`` and sets
    ``failed``; the caller then ends the session and promotes nothing from it."""

    def __init__(
        self,
        audit: ForgeAudit,
        *,
        spec_hash: str,
        model: str,
        agent_model: str | None,
        mode: Literal["agent", "template"],
        depth: int,
    ) -> None:
        self.root = audit.directory
        self.session_id = uuid.uuid4().hex
        self.failed = False
        try:
            self._state = AuditSessionRecord(
                audit_version=AUDIT_VERSION,
                spec_hash=spec_hash,
                session_id=self.session_id,
                depth=depth,
                mode=mode,
                model=model,
                agent_model=agent_model,
                started_at=_now(),
                ended_at=None,
                submissions=0,
                promoted=None,
                failures=(),
            )
            # The validated 64-hex spec hash and uuid hex are the only path components.
            self.directory = self.root / self._state.spec_hash / self.session_id
            self.directory.mkdir(parents=True)
            _write_json(self.directory / "session.json", self._state)
        except Exception as exc:
            self.failed = True
            raise AuditError("audit_unwritable") from exc

    def record(
        self,
        *,
        number: int,
        source: str,
        candidate: Path | None,
        verdict: str,
        measurement: Measurement | None,
        smoke_passed: bool | None,
        floor_limit: float,
        tuning_cases: Sequence[EvaluationCase] = (),
    ) -> None:
        """Copy one submission: its exact source, its spec and configuration when the
        candidate was written, and ``submission.json`` last. ``tuning_cases`` are the
        practice cases ``measurement`` ran on; only their hash is recorded."""
        try:
            target = self.directory / str(number)
            target.mkdir()  # each submission number is recorded exactly once
            code = source.encode("utf-8")
            (target / "harness.py").write_bytes(code)
            digest: str | None = None
            if candidate is not None:
                for name in ("spec.json", "config.json"):
                    shutil.copyfile(candidate / name, target / name)
                if (candidate / "subharnesses").is_dir():
                    # A bound child is a licensed artifact with its own signed Stage 4
                    # report (verification id, held-out score and counts). The report
                    # never enters the audit, at any depth; its code and spec do.
                    shutil.copytree(
                        candidate / "subharnesses",
                        target / "subharnesses",
                        ignore=shutil.ignore_patterns(*_CHILD_EXCLUDED),
                    )
                digest = HarnessArtifact.load(candidate).artifact_digest
            state = self._state
            _write_json(
                target / "submission.json",
                AuditSubmission(
                    audit_version=AUDIT_VERSION,
                    spec_hash=state.spec_hash,
                    session_id=self.session_id,
                    submission=number,
                    depth=state.depth,
                    mode=state.mode,
                    model=state.model,
                    agent_model=state.agent_model,
                    code_sha256=_sha256(code),
                    artifact_digest=digest,
                    verdict=verdict,
                    floor_limit=floor_limit,
                    tuning=(
                        AuditTuning.of(measurement, tuning_cases)
                        if measurement is not None
                        else None
                    ),
                    smoke_passed=smoke_passed,
                    recorded_at=_now(),
                ),
            )
            self._state = state.model_copy(update={"submissions": state.submissions + 1})
            _write_json(self.directory / "session.json", self._state)
        except Exception as exc:
            self.failed = True
            raise AuditError("audit_unwritable") from exc

    def close(self, *, promoted: int | None, failures: Sequence[str]) -> None:
        try:
            self._state = AuditSessionRecord.model_validate(
                {
                    **self._state.model_dump(),
                    "ended_at": _now(),
                    "promoted": promoted,
                    "failures": tuple(failures),
                }
            )
            _write_json(self.directory / "session.json", self._state)
        except Exception as exc:
            self.failed = True
            raise AuditError("audit_unwritable") from exc


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_CURRENT: ContextVar[ForgeAudit | None] = ContextVar("architect_forge_audit", default=None)


@contextmanager
def audit_scope(audit: ForgeAudit | None) -> Iterator[ForgeAudit | None]:
    """Audit the compiles started in this block with an existing ``ForgeAudit``;
    None leaves the current setting unchanged."""
    if audit is None:
        yield None
        return
    token = _CURRENT.set(audit)
    try:
        yield audit
    finally:
        _CURRENT.reset(token)


@contextmanager
def forge_audit(directory: Path | None) -> Iterator[ForgeAudit | None]:
    """Audit every Forge session of the compiles started in this block (a context
    variable: other tasks are unaffected). ``AuditError`` before the block runs when
    the directory cannot be created or written; None audits nothing."""
    if directory is None:
        yield None
        return
    with audit_scope(ForgeAudit(directory)) as audit:
        yield audit


def current_audit() -> ForgeAudit | None:
    return _CURRENT.get()


def audit_directory(flag: Path | None) -> Path | None:
    """The ``--audit`` flag, else ``ARCHITECT_FORGE_AUDIT_DIR``, else None."""
    if flag is not None:
        return flag
    value = os.environ.get(AUDIT_ENV, "").strip()
    return Path(value) if value else None
