"""``bench audit`` (Phase 14 Section 13.2): replay audited Forge submissions on their exam.

``--audit DIR`` keeps every Forge submission and its tuning measurement in a
directory Forge cannot read. This replay counts the gate audit: harnesses that met
the bar in practice (a tuning measurement with no failure code and a score of at
least the bar) and then failed an exam they never saw.

For each selected submission it copies the submission into a fresh throwaway
workspace with its own signing key, loads it as an artifact, builds the profile
floor its spec names and calls Stage 4's ``verify()`` exactly once, as a compile
would. A floor whose practice cases are not the ones the submission was measured on
(their recorded hash, or for older records their number) is never replayed: that
row's exam is ``error``. The counts come from the report that call writes; the
workspace is then deleted. The audit directory is only ever read. Consent and money
work like ``bench run``: the bound is each submission's per-run budget times its
held-out cases, shown before any spend, with ``--max-usd`` and ``Continue? [y/N]``.

The gate audit counts only scored exams (passed or below the bar) on a floor that
matches its public dataset manifest, never a fake-provider replay; the submissions
that met the bar and were left out are counted by reason. Whether an exam fell more
than 5 points below practice is decided in exact fractions of the counted cases
(``practice_drop_exceeds``), never by a float difference.

Rows are content-free: counts, scores, dollars, labels and hashes. No verification
id, case id, prompt or output enters them. Beside ``audit.jsonl`` each replay writes
``replay.json``, its window and selection, so the pre-registered analysis can tell a
windowed replay from one that covers every build.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from architect.compiler.verification import verify
from architect.floors import EvaluationCase, FloorEvaluator, HeldOutAccessError, HeldOutTasks
from architect.floors.checkers import CHECKERS
from architect.floors.provenance import cases_sha256
from architect.models import ModelAdapter
from architect.models.images import SHA256_HEX
from architect.profiles import Profile
from architect.runtime import Runtime
from architect.spec import FloorDefinition, FloorKind, HarnessArtifact, Spec, TestReport
from architect.telemetry import TelemetryStore
from architect.telemetry.schema import NonnegativeFloat, Score

from . import public_tasks
from .providers import CallMeter, build_factory, evidence_label, instrument, provider_for
from .render import money, wrap
from .rows import BenchError, Code, Evidence, ModelId
from .runner import BenchConfig, _signing_key, _Workspace

__all__ = [
    "AUDIT_VERSION",
    "AuditReplay",
    "AuditRequest",
    "AuditResult",
    "AuditRow",
    "Exam",
    "audit_table",
    "classify",
    "exclusion",
    "practice_drop_exceeds",
    "read_submissions",
    "replay_audit",
]

AUDIT_VERSION: Literal[1] = 1
Exam = Literal["passed", "below_bar", "over_budget", "invalid_exceeded", "error"]
# A harness whose exam fell more than this below its own practice score is counted
# apart. The comparison is exact (``PRACTICE_DROP_LIMIT``); the float is for display.
PRACTICE_DROP = 0.05
PRACTICE_DROP_LIMIT = Fraction(1, 20)
_RECORD_LIMIT = 1_048_576


class _Counts(BaseModel):
    """A ``CaseCounts`` dump (Section 3.2) as the audit records it."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    cases: Annotated[int, Field(ge=0)]
    correct: Annotated[int, Field(ge=0)] | None = None
    invalid: Annotated[int, Field(ge=0)] = 0
    over_budget: Annotated[int, Field(ge=0)] = 0


class _Tuning(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    score: float | None
    sample_count: Annotated[int, Field(ge=0)] = 0
    counts: _Counts | None = None
    failures: tuple[str, ...] = ()
    cases_sha256: SHA256_HEX | None = None  # the practice cases measured, when recorded


class _Submission(BaseModel):
    """``submission.json`` (Section 8.4): the keys this replay reads."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    audit_version: Literal[1]
    spec_hash: SHA256_HEX
    session_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    submission: Annotated[int, Field(ge=1)]
    depth: Annotated[int, Field(ge=0)]
    mode: Literal["agent", "template"]
    model: ModelId
    agent_model: ModelId | None
    code_sha256: SHA256_HEX
    artifact_digest: SHA256_HEX | None
    verdict: Code
    floor_limit: Score
    tuning: _Tuning | None
    recorded_at: datetime

    @property
    def met_bar(self) -> bool:
        """Met the bar in practice: a tuning measurement with no failure code and a
        score of at least the bar."""
        tuning = self.tuning
        return (
            tuning is not None
            and not tuning.failures
            and tuning.score is not None
            and tuning.score >= self.floor_limit
        )


@dataclass(frozen=True)
class Submission:
    """One audited submission and the directory that holds its files."""

    record: _Submission
    directory: Path


class AuditRow(BaseModel):
    """One replayed submission; content-free (Section 13.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    audit_version: Literal[1]
    replay_id: str
    spec_hash: SHA256_HEX
    session_id: str
    submission: int
    depth: int
    mode: Literal["agent", "template"]
    model: ModelId
    agent_model: ModelId | None
    recorded_at: datetime
    artifact_digest: SHA256_HEX
    verdict: Code
    floor_limit: Score
    tuning_score: Score | None
    tuning_correct: int | None
    tuning_cases: int
    tuning_invalid: int
    met_bar_in_practice: bool
    exam: Exam
    held_out_score: Score
    held_out_correct: int | None
    held_out_cases: int
    held_out_invalid: int
    held_out_over_budget: int
    # Practice accuracy minus exam accuracy when both were scored: the difference of the
    # exact fractions of the counted cases, as a float (``practice_drop_exceeds``).
    practice_drop: float | None
    floor_origin: Literal["public_dataset", "synthetic", "no_manifest"]
    evidence: Evidence
    replay_cost_usd: NonnegativeFloat
    # ``cases_sha256`` of the replayed profile floor's cases in file order (a public
    # floor's equals its manifest's); None when no floor was built.
    floor_cases_sha256: SHA256_HEX | None = None


class AuditReplay(BaseModel):
    """``replay.json`` beside ``audit.jsonl``: how one replay selected its submissions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    audit_version: Literal[1]
    replay_id: str
    since: datetime | None
    until: datetime | None
    include_all: bool


class AuditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    directory: Path
    since: datetime | None = None
    until: datetime | None = None
    include_all: bool = False
    max_usd: Annotated[float, Field(gt=0)] | None = None
    out: Path = Path(".architect") / "bench" / "audits"

    @classmethod
    def from_flags(
        cls,
        directory: Path,
        *,
        since: str | None,
        until: str | None,
        include_all: bool,
        max_usd: float | None,
        out: Path,
    ) -> AuditRequest:
        """The CLI flags; a time without a zone is UTC. Bad values are ``ValueError``."""
        try:
            return cls(
                directory=directory,
                since=_instant(since),
                until=_instant(until),
                include_all=include_all,
                max_usd=max_usd,
                out=out,
            )
        except ValidationError:
            raise ValueError("Invalid audit request") from None


class AuditResult(BaseModel):
    """What one replay counted; the rows are in ``rows_path``.

    ``met_bar`` (m) and ``gate_audit`` (n of them scored below the bar on the exam)
    count only the rows that met the bar in practice and that ``exclusion`` keeps; the
    rows it leaves out are counted by reason in the three ``excluded_*`` fields. The
    exam classes and ``practice_drops`` cover every replayed row.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    replay_id: str
    rows_path: str
    bound_usd: float
    submissions: int
    met_bar: int
    gate_audit: int  # of met_bar: scored below the bar on the exam
    excluded_floor: int  # met the bar; the floor is not a matched public dataset
    excluded_fake: int  # met the bar; replayed with the fake provider
    excluded_unscored: int  # met the bar; the exam was over budget, invalid or an error
    passed: int
    below_bar: int
    over_budget: int
    invalid_exceeded: int
    errors: int
    practice_drops: int  # rows whose exam fell more than PRACTICE_DROP below practice
    replay_cost_usd: float
    evidence: tuple[str, ...]
    floor_origins: tuple[str, ...]


def exclusion(row: AuditRow) -> Literal["floor", "fake", "unscored"] | None:
    """Why the gate audit leaves ``row`` out, first reason wins, or None when it counts:
    its floor is not a matched public dataset (``floor``), it was replayed with the fake
    provider (``fake``), or its exam was not scored as passed or below the bar."""
    if row.floor_origin != "public_dataset":
        return "floor"
    if row.evidence == "synthetic_fake":
        return "fake"
    if row.exam not in ("passed", "below_bar"):
        return "unscored"
    return None


def _share(correct: int | None, cases: int, score: float | None) -> Fraction | None:
    """``correct / cases`` as an exact fraction. A record without counts has only its
    score, the float of that ratio; the nearest fraction whose denominator is at most
    the case count (a million when the count is unknown) is the ratio itself."""
    if correct is not None and cases > 0:
        return Fraction(correct, cases)
    if score is None:
        return None
    return Fraction(score).limit_denominator(cases if cases > 0 else 1_000_000)


def _drop(
    tuning_correct: int | None,
    tuning_cases: int,
    tuning_score: float | None,
    held_out_correct: int | None,
    held_out_cases: int,
    held_out_score: float | None,
) -> Fraction | None:
    """Practice accuracy minus exam accuracy, exactly; None when either is unknown."""
    practice = _share(tuning_correct, tuning_cases, tuning_score)
    exam = _share(held_out_correct, held_out_cases, held_out_score)
    return None if practice is None or exam is None else practice - exam


def practice_drop_exceeds(row: AuditRow) -> bool:
    """Whether ``row``'s exam fell more than ``PRACTICE_DROP`` below its practice score.

    The two accuracies are exact fractions of the counted cases, so an exact 5-point
    drop is never "more than 5 points": in floats 0.90 - 0.85 is 0.05000000000000004
    and 0.85 - 0.80 is 0.04999999999999993. A row with no practice drop never counts.
    """
    if row.practice_drop is None:
        return False
    drop = _drop(
        row.tuning_correct,
        row.tuning_cases,
        row.tuning_score,
        row.held_out_correct,
        row.held_out_cases,
        row.held_out_score,
    )
    return drop is not None and drop > PRACTICE_DROP_LIMIT


def _instant(text: str | None) -> datetime | None:
    if text is None:
        return None
    value = datetime.fromisoformat(text)
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# ---------------------------------------------------------------------- reading


def read_submissions(directory: Path) -> list[Submission]:
    """Every ``<spec_hash>/<session_id>/<n>/submission.json`` under ``directory``.

    A missing directory, an unreadable or invalid record, or a record whose ids
    differ from its own path is ``bench_audit_unreadable``.
    """
    if not directory.is_dir():
        raise BenchError("bench_audit_unreadable")
    found: list[Submission] = []
    try:
        paths = sorted(directory.glob("*/*/*/submission.json"))
        for path in paths:
            if path.stat().st_size > _RECORD_LIMIT:
                raise BenchError("bench_audit_unreadable")
            record = _Submission.model_validate_json(path.read_text(encoding="utf-8"))
            number, session, spec_hash = path.parent, path.parent.parent, path.parent.parent.parent
            if (
                number.name != str(record.submission)
                or session.name != record.session_id
                or spec_hash.name != record.spec_hash
            ):
                raise BenchError("bench_audit_unreadable")
            found.append(Submission(record=record, directory=number))
    except (OSError, UnicodeError, ValidationError):
        raise BenchError("bench_audit_unreadable") from None
    return found


def _stamp(record: _Submission) -> datetime:
    stamp = record.recorded_at
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


def _selected(request: AuditRequest, submissions: Sequence[Submission]) -> list[Submission]:
    """Submissions in the time window (both ends included): those that met the bar
    in practice, or with ``include_all`` every one with a tuning measurement;
    oldest first."""
    chosen: list[Submission] = []
    for item in submissions:
        record = item.record
        stamp = _stamp(record)
        if request.since is not None and stamp < request.since:
            continue
        if request.until is not None and stamp > request.until:
            continue
        if record.tuning is None or not (request.include_all or record.met_bar):
            continue
        if record.artifact_digest is None:
            raise BenchError("bench_audit_unreadable")  # measured but never written
        chosen.append(item)
    return sorted(
        chosen,
        key=lambda item: (
            _stamp(item.record),
            item.record.spec_hash,
            item.record.session_id,
            item.record.submission,
        ),
    )


def _spec(item: Submission) -> Spec:
    try:
        return Spec.model_validate_json((item.directory / "spec.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise BenchError("bench_audit_unreadable") from None


# ------------------------------------------------------------------------ floors


_CASES: TypeAdapter[list[EvaluationCase]] = TypeAdapter(list[EvaluationCase])


@dataclass(frozen=True)
class _Floor:
    """A profile floor for one definition, or the reason it cannot be built."""

    evaluator: FloorEvaluator | None
    records: tuple[EvaluationCase, ...]
    origin: Literal["public_dataset", "synthetic", "no_manifest"]

    @property
    def cases_sha256(self) -> str | None:
        return cases_sha256(self.records) if self.records else None


def _profile_floor(profile: Profile, definition: FloorDefinition) -> _Floor:
    """The profile floor named by ``definition.dataset`` with the same kind.

    Only held-out-task floors are replayed here; any other kind, a missing entry or
    an unreadable file leaves no evaluator (that row's exam is ``error``).
    """
    config = profile.floors.get(definition.dataset)
    if config is None or config.kind != definition.kind:
        return _Floor(None, (), "no_manifest")
    if definition.kind != FloorKind.HELD_OUT_TASKS:
        return _Floor(None, (), "no_manifest")
    try:
        records = tuple(
            _CASES.validate_json(profile.resolve(config.path).read_text(encoding="utf-8"))
        )
        checker = CHECKERS[config.checker] if config.checker != "equality" else None
        evaluator = HeldOutTasks(definition, list(records), checker)
    except (OSError, UnicodeError, ValueError):
        return _Floor(None, (), "no_manifest")
    manifest = getattr(config, "manifest", None)  # FloorConfig.manifest (Section 3.4)
    origin: Literal["public_dataset", "synthetic", "no_manifest"] = "no_manifest"
    if isinstance(manifest, Path):
        origin = public_tasks.manifest_origin(records, profile.resolve(manifest))
    return _Floor(evaluator, records, origin)


def _practised_on(record: _Submission, floor: _Floor) -> bool:
    """Whether ``floor``'s practice cases are the ones the submission was measured on:
    their recorded hash, or, for a record written before the hash, their number. A
    floor that fails this is another exam than the build's own, whatever its name."""
    tuning, evaluator = record.tuning, floor.evaluator
    if tuning is None or evaluator is None:
        return False
    practice = evaluator.tuning
    if tuning.cases_sha256 is not None:
        return cases_sha256(practice) == tuning.cases_sha256
    return len(practice) == tuning.sample_count


# ----------------------------------------------------------------- classifying


def _counts(report: TestReport) -> _Counts | None:
    """The signed report's ``held_out_counts`` (Section 3.2), when it carries them."""
    counts = getattr(report, "held_out_counts", None)
    if counts is None:
        return None
    try:
        return _Counts.model_validate(counts, from_attributes=True)
    except ValidationError:
        return None


def classify(report: TestReport, floor_limit: float) -> Exam:
    """``passed``; ``over_budget`` (``primary_budget_failed``); ``invalid_exceeded``
    (``primary_invalid_exceeded``); ``below_bar`` when the partition was scored below
    the bar; ``error`` otherwise, including a report without held-out counts, whose
    score cannot be told apart from a partition that was never scored."""
    if report.passed:
        return "passed"
    if "primary_budget_failed" in report.failures:
        return "over_budget"
    if "primary_invalid_exceeded" in report.failures:
        return "invalid_exceeded"
    counts = _counts(report)
    if counts is not None and counts.correct is not None and report.floor_score < floor_limit:
        return "below_bar"
    return "error"


def _row(
    replay_id: str,
    item: Submission,
    *,
    report: TestReport | None,
    origin: Literal["public_dataset", "synthetic", "no_manifest"],
    evidence: Evidence,
    cost_usd: float,
    floor_cases_sha256: str | None = None,
) -> AuditRow:
    record = item.record
    tuning = record.tuning
    assert tuning is not None and record.artifact_digest is not None  # _selected
    exam: Exam = "error" if report is None else classify(report, record.floor_limit)
    counts = _counts(report) if report is not None else None
    tuning_counts = tuning.counts
    tuning_score = tuning.score if tuning.score is not None and 0 <= tuning.score <= 1 else None
    held_out_score = report.floor_score if report is not None else 0.0
    scored = counts is not None and counts.correct is not None
    tuning_scored = tuning_score is not None and not (
        tuning_counts is not None and tuning_counts.correct is None
    )
    tuning_correct = tuning_counts.correct if tuning_counts is not None else None
    tuning_cases = tuning_counts.cases if tuning_counts is not None else tuning.sample_count
    held_out_correct = counts.correct if counts is not None else None
    held_out_cases = counts.cases if counts is not None else report.held_out_count if report else 0
    drop = (
        _drop(
            tuning_correct,
            tuning_cases,
            tuning_score,
            held_out_correct,
            held_out_cases,
            held_out_score,
        )
        if scored and tuning_scored
        else None
    )
    return AuditRow(
        audit_version=AUDIT_VERSION,
        replay_id=replay_id,
        spec_hash=record.spec_hash,
        session_id=record.session_id,
        submission=record.submission,
        depth=record.depth,
        mode=record.mode,
        model=record.model,
        agent_model=record.agent_model,
        recorded_at=record.recorded_at,
        artifact_digest=record.artifact_digest,
        verdict=record.verdict,
        floor_limit=record.floor_limit,
        tuning_score=tuning_score,
        tuning_correct=tuning_correct,
        tuning_cases=tuning_cases,
        tuning_invalid=tuning_counts.invalid if tuning_counts is not None else 0,
        met_bar_in_practice=record.met_bar,
        exam=exam,
        held_out_score=held_out_score,
        held_out_correct=held_out_correct,
        held_out_cases=held_out_cases,
        held_out_invalid=counts.invalid if counts is not None else 0,
        held_out_over_budget=counts.over_budget if counts is not None else 0,
        practice_drop=float(drop) if drop is not None else None,
        floor_origin=origin,
        evidence=evidence,
        replay_cost_usd=max(0.0, cost_usd),
        floor_cases_sha256=floor_cases_sha256,
    )


# --------------------------------------------------------------------- replaying


# The provider seam of a profile run; only its ``provider`` is read.
_PROFILE_RUN = BenchConfig(tier="public", provider="profile")


def _bound(
    profile: Profile, chosen: Sequence[Submission]
) -> tuple[float, int, list[tuple[Submission, Spec, _Floor]]]:
    """The per-run budget times the held-out cases of every selected submission."""
    plans: list[tuple[Submission, Spec, _Floor]] = []
    total, runs = 0.0, 0
    for item in chosen:
        spec = _spec(item)
        if spec.spec_hash != item.record.spec_hash:
            raise BenchError("bench_audit_unreadable")
        if item.record.model not in profile.models:
            raise BenchError("bench_model_unconfigured")
        floor = _profile_floor(profile, spec.floor)
        if floor.evaluator is not None and not _practised_on(item.record, floor):
            floor = replace(floor, evaluator=None)  # another exam: never replayed or bound
        held_out = len(floor.records) // 2 if floor.evaluator is not None else 0
        total += spec.budget.cost_usd * held_out
        runs += held_out
        plans.append((item, spec, floor))
    return total, runs, plans


def _artifact(item: Submission, directory: Path) -> HarnessArtifact | None:
    """The submission copied into ``directory`` and loaded; None when it is not the
    exact candidate the audit recorded, or when it binds sub-harnesses: their
    licences were signed by the build host's key, which a throwaway key cannot
    verify, so such a harness is not replayed here."""
    target = directory / "candidate"
    shutil.copytree(
        item.directory, target, ignore=shutil.ignore_patterns("submission.json", "report.json")
    )
    try:
        artifact = HarnessArtifact.load(target)
        digest = artifact.artifact_digest
    except (OSError, ValueError, TypeError):
        return None
    record = item.record
    if digest != record.artifact_digest or artifact.spec.spec_hash != record.spec_hash:
        return None
    if artifact.forge is not None and artifact.forge.subharnesses:
        return None
    return artifact


async def _replay_one(
    item: Submission,
    spec: Spec,
    floor: _Floor,
    factory: Callable[[str], ModelAdapter],
    workspace_root: Path | None,
) -> tuple[TestReport | None, float]:
    """One ``verify()`` in a throwaway workspace; the report and the dollars it spent."""
    if floor.evaluator is None or spec.secondary_floor is not None:
        return None, 0.0  # no profile floor, or a second floor this replay does not build
    meter = CallMeter()
    try:
        workspace = _Workspace(workspace_root)
    except OSError:
        raise BenchError("bench_workspace_error") from None
    try:
        with _signing_key(workspace) as root:
            artifact = _artifact(item, root)
            if artifact is None:
                return None, 0.0
            store = TelemetryStore(root / "telemetry")
            try:

                def metered(model: str) -> ModelAdapter:
                    return instrument(factory(model), meter)

                runtime = Runtime(store, adapter_factory=metered)
                verified = await verify(artifact, floor.evaluator, metered, runtime)
            finally:
                store.close()
            return verified.report, meter.cost_usd
    except HeldOutAccessError:
        raise BenchError("bench_held_out_access") from None
    except (asyncio.CancelledError, BenchError):
        raise
    except Exception:
        return None, meter.cost_usd
    finally:
        workspace.remove()


async def replay_audit(
    request: AuditRequest,
    profile: Profile,
    *,
    consent: Callable[[str], bool],
    factory: Callable[[str], ModelAdapter] | None = None,
    workspace_root: Path | None = None,
) -> AuditResult:
    """Replay the selected submissions and write ``<out>/<replay_id>/audit.jsonl``.

    ``consent`` gets the bound line before any spend and returns whether to go on
    (``bench_cancelled`` when it does not); a bound above ``max_usd`` is
    ``bench_over_max_usd`` before it is asked. ``factory`` overrides the profile's
    adapters (tests).
    """
    chosen = _selected(request, read_submissions(request.directory))
    bound_usd, runs, plans = _bound(profile, chosen)
    if request.max_usd is not None and bound_usd > request.max_usd:
        raise BenchError("bench_over_max_usd")
    line = (
        f"bench audit on {len(plans)} submissions: <={money(bound_usd)} API-equivalent "
        f"bound, ~{runs} runs, not a bill."
    )
    if plans and not consent(line):
        raise BenchError("bench_cancelled")
    adapters = factory or build_factory(_PROFILE_RUN, profile, None)
    replay_id = str(uuid.uuid4())
    directory = request.out / replay_id
    replay = AuditReplay(
        audit_version=AUDIT_VERSION,
        replay_id=replay_id,
        since=request.since,
        until=request.until,
        include_all=request.include_all,
    )
    try:
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "replay.json").write_text(
            json.dumps(replay.model_dump(mode="json"), sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        rows_path = directory / "audit.jsonl"
        rows_path.touch()
    except OSError:
        raise BenchError("bench_out_unwritable") from None
    rows: list[AuditRow] = []
    for item, spec, floor in plans:
        report, cost = await _replay_one(item, spec, floor, adapters, workspace_root)
        provider = provider_for(_PROFILE_RUN, profile, item.record.model)
        row = _row(
            replay_id,
            item,
            report=report,
            origin=floor.origin,
            evidence=evidence_label(provider),
            cost_usd=cost,
            floor_cases_sha256=floor.cases_sha256,
        )
        rows.append(row)
        try:
            with rows_path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(row.model_dump_json() + "\n")
        except OSError:
            raise BenchError("bench_out_unwritable") from None
    return _result(replay_id, rows_path, bound_usd, rows)


def _result(
    replay_id: str, rows_path: Path, bound_usd: float, rows: Sequence[AuditRow]
) -> AuditResult:
    def count(exam: Exam) -> int:
        return sum(1 for row in rows if row.exam == exam)

    met = [row for row in rows if row.met_bar_in_practice]
    reasons = [exclusion(row) for row in met]
    counted = [row for row, reason in zip(met, reasons, strict=True) if reason is None]
    return AuditResult(
        replay_id=replay_id,
        rows_path=str(rows_path),
        bound_usd=bound_usd,
        submissions=len(rows),
        met_bar=len(counted),
        gate_audit=sum(1 for row in counted if row.exam == "below_bar"),
        excluded_floor=reasons.count("floor"),
        excluded_fake=reasons.count("fake"),
        excluded_unscored=reasons.count("unscored"),
        passed=count("passed"),
        below_bar=count("below_bar"),
        over_budget=count("over_budget"),
        invalid_exceeded=count("invalid_exceeded"),
        errors=count("error"),
        practice_drops=sum(1 for row in rows if practice_drop_exceeds(row)),
        replay_cost_usd=sum(row.replay_cost_usd for row in rows),
        evidence=tuple(sorted({row.evidence for row in rows})),
        floor_origins=tuple(sorted({row.floor_origin for row in rows})),
    )


def audit_table(result: AuditResult) -> str:
    """The counts of one replay as plain lines of at most 100 columns."""
    lines = [
        f"bench audit {result.replay_id}: {result.submissions} submissions replayed on "
        f"their exam, {money(result.replay_cost_usd)} API-equivalent, not a bill.",
        f"Gate audit: {result.gate_audit} of {result.met_bar} harnesses that met the bar "
        "in practice scored below it on the exam.",
        "It counts only scored exams on a floor that matches its public dataset manifest, "
        f"never the fake provider. Left out: {result.excluded_floor} on other floors, "
        f"{result.excluded_fake} replayed with the fake provider, "
        f"{result.excluded_unscored} with no exam score.",
        f"Exam: {result.passed} passed, {result.below_bar} below the bar, "
        f"{result.over_budget} over budget, {result.invalid_exceeded} with too many invalid "
        f"outputs, {result.errors} not replayed or not countable.",
        f"Exam more than {PRACTICE_DROP} below practice: {result.practice_drops}.",
    ]
    if "synthetic_fake" in result.evidence:
        lines.append("Fake provider rows: plumbing checks, not model quality.")
    if "synthetic" in result.floor_origins:
        lines.append("Synthetic floor: not evidence of quality.")
    if "no_manifest" in result.floor_origins:
        lines.append("Some floors have no manifest, so they are not counted as public data.")
    lines.append(f"rows written to {result.rows_path}")
    return "".join(wrap(line) for line in lines)
