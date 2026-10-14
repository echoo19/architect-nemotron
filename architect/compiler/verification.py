"""Stage 4: full held-out verification and artifact-bound report authority.

This is the only production consumer of the private verification credentials (the
held-out capability and the report signers). Held-out cases and outputs never leave the
trusted evaluation path.

Retake: ``verify(..., retake=RetakeExam(...))`` is the same exam taken again by a changed
agent, under the same rule. Where the floor holds cases no earlier attempt used it runs
on those, else on the exam's own cases, and the signed report says which and names the
attempt.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from architect._authority import _seal_report, _sign_report, seal_ready
from architect.compiler.types import AdapterFactory, Measurement
from architect.floors import FloorEvaluator, HeldOutAccessError
from architect.floors._access import _VERIFIER_CAPABILITY
from architect.runtime import Runtime
from architect.runtime.evaluation import evaluate_cases
from architect.spec import (
    CaseCounts,
    FloorDefinition,
    FloorKind,
    HarnessArtifact,
    RetakeCases,
    TestReport,
)


@dataclass(frozen=True)
class RetakeExam:
    """One retake of an exam: its attempt number (2 or more) and the ids of the cases
    earlier attempts used. ``record`` is called once, before the first model call, with
    which cases this attempt runs on and their ids (none for the same exam), so an attempt
    that is cancelled or crashes still counts and still uses its cases."""

    attempt: int
    used: Collection[str] = ()
    record: Callable[[RetakeCases, tuple[str, ...]], None] | None = None
    # The caller cannot say which cases earlier attempts used (its record of them is
    # incomplete), so this attempt is the same exam and never claims unused cases.
    same_exam: bool = False


def _worst_score(definition: FloorDefinition) -> float:
    return 1.0 if definition.kind == FloorKind.HUMAN_EDIT_RATE else 0.0


def _passes(definition: FloorDefinition, score: float) -> bool:
    if definition.kind == FloorKind.HUMAN_EDIT_RATE:
        return score <= definition.limit
    return score >= definition.limit


def _signed(report: TestReport) -> TestReport:
    """The HMAC always; the Ed25519 seal too on a host holding an Ed25519 key (W7).
    The seal covers the HMAC's payload and the HMAC, so either one alone
    authenticates the report."""
    payload = report.signed_payload()
    signature = _sign_report(payload)
    return report.model_copy(
        update={"signature": signature, "ed25519": _seal_report(payload, signature)}
    )


def _counter(on_case: Callable[[int, int], None], total: int) -> Callable[[], None]:
    """Counts finished cases for ``on_case(done, total)``; a listener's error never
    reaches the exam."""
    done = 0

    def count() -> None:
        nonlocal done
        done += 1
        try:
            on_case(done, total)
        except Exception:
            pass

    return count


def _persist_failure(directory: Path, report: TestReport) -> None:
    """Persist only negative evidence when normal artifact saving is impossible.

    Never manufacture absent source or overwrite its configuration. Storage
    errors propagate: a failure that cannot be recorded is not deliverable.
    """
    descriptor, temporary = tempfile.mkstemp(prefix=".report.", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(report.model_dump_json(indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / "report.json")
    finally:
        Path(temporary).unlink(missing_ok=True)


async def verify(
    artifact: HarnessArtifact,
    floor: FloorEvaluator,
    adapter_factory: AdapterFactory,
    runtime: Runtime,
    *,
    secondary_floor: FloorEvaluator | None = None,
    tuning_evidence: Measurement | None = None,
    retake: RetakeExam | None = None,
    on_case: Callable[[int, int], None] | None = None,
) -> HarnessArtifact:
    """Always issue a new report, including on verification failures.

    ``held_out_count`` is the total size of the authorized full partitions.
    Budget fields are maximum per-run usage across both floors; mean cost is
    the primary held-out mean used for measured model selection. No old
    report is used as evidence. Unreadable code or failed persistence yields an
    unlicensed result; absent source is never replaced just to persist a report.
    A failed normal save writes only report.json as negative evidence. If that
    also fails, the storage error propagates instead of delivering an artifact.

    Phase 14: ``held_out_counts`` is the primary measurement's run counts (None when
    the primary floor was never measured), and a primary measurement that failed with
    ``invalid_fraction_exceeded`` adds ``primary_invalid_exceeded``.

    W7: on a host holding an Ed25519 key the report is sealed too; a key file that is
    not a usable key raises ``trust.TrustError("key_invalid")`` before anything runs.

    Retake: with ``retake`` the primary floor's cases are ones no earlier attempt used
    when a full exam of them is left, else the exam's own cases again, and the report
    carries ``attempt`` and ``retake_cases``. The rule that decides is unchanged: the pass
    mark, the invalid allowance and the per-run budget, and nothing about any earlier
    score. A floor that does not allow retakes raises ``HeldOutAccessError`` before any
    model call. ``on_case(done, total)`` is called after each primary case and carries
    counts only.
    """
    seal_ready()  # W7: an unusable Ed25519 key raises here, before any held-out spend
    if retake is not None and (retake.attempt < 2 or not floor.retakes_allowed):
        raise HeldOutAccessError("This exam cannot be retaken")
    candidate = artifact.model_copy(deep=True, update={"report": None})
    spec = candidate.spec.model_copy(deep=True)
    failures: list[str] = []
    digest = "0" * 64
    try:
        digest = candidate.artifact_digest
    except (OSError, ValueError, TypeError):
        failures.append("artifact_unreadable")

    try:
        if floor.definition != spec.floor:
            failures.append("primary_floor_mismatch")
        if spec.secondary_floor is None:
            if secondary_floor is not None:
                failures.append("unexpected_secondary_floor")
        elif secondary_floor is None:
            failures.append("secondary_floor_missing")
        elif secondary_floor.definition != spec.secondary_floor:
            failures.append("secondary_floor_mismatch")
    except Exception:
        failures.append("floor_definition_error")

    primary_score = _worst_score(spec.floor)
    mean_cost_usd: float | None = None
    secondary_score = (
        _worst_score(spec.secondary_floor) if spec.secondary_floor is not None else None
    )
    held_out_count = 0
    held_out_counts: CaseCounts | None = None
    retake_cases: RetakeCases | None = None
    measurements: list[Measurement] = []
    if not failures:
        floors = [("primary", floor, spec.floor)]
        if secondary_floor is not None and spec.secondary_floor is not None:
            floors.append(("secondary", secondary_floor, spec.secondary_floor))
        for label, evaluator, definition in floors:
            try:
                # Issue exactly once, retain the exact authorized tuple, and do
                # not let tuning/search choose any subset of verification data.
                cases = evaluator._verification_cases(_VERIFIER_CAPABILITY)
                progress: Callable[[], None] | None = None
                if label == "primary":
                    if retake is not None:
                        unused = (
                            None
                            if retake.same_exam
                            else evaluator._retake_cases(
                                _VERIFIER_CAPABILITY, retake.used, len(cases)
                            )
                        )
                        retake_cases = "same_exam" if unused is None else "unused"
                        if unused is not None:
                            cases = unused
                        if retake.record is not None:
                            # Before the first model call: the attempt and its cases
                            # are used from here on, whatever happens next.
                            retake.record(
                                retake_cases,
                                tuple(case.case_id for case in unused or ()),
                            )
                    if on_case is not None:
                        progress = _counter(on_case, len(cases))
                held_out_count += len(cases)
                measurement = await evaluate_cases(
                    candidate,
                    evaluator,
                    cases,
                    adapter_factory,
                    runtime,
                    floor_role="primary" if label == "primary" else "secondary",
                    **({} if progress is None else {"on_case": progress}),
                )
                measurements.append(measurement)
                if label == "primary":
                    primary_score = measurement.score
                    mean_cost_usd = measurement.mean_cost_usd
                    held_out_counts = measurement.counts
                else:
                    secondary_score = measurement.score
                minimum = (
                    definition.minimum_samples
                    if definition.kind == FloorKind.HUMAN_EDIT_RATE
                    else 1
                )
                if len(cases) < minimum or measurement.sample_count != len(cases):
                    failures.append(label + "_sample_count")
                if not measurement.floor_passed or not _passes(definition, measurement.score):
                    failures.append(label + "_floor_failed")
                if (
                    not measurement.budget_passed
                    or measurement.max_cost_usd > spec.budget.cost_usd
                    or measurement.max_compute_s > spec.budget.compute_s
                ):
                    failures.append(label + "_budget_failed")
                if measurement.failures:
                    # Never copy arbitrary strings from evaluator exceptions or
                    # implementation-specific failure details into a report.
                    failures.append(label + "_evaluation_failed")
                if label == "primary" and "invalid_fraction_exceeded" in measurement.failures:
                    failures.append("primary_invalid_exceeded")
            except Exception:
                failures.append(label + "_evaluation_error")

    try:
        if candidate.artifact_digest != digest:
            failures.append("artifact_changed")
    except (OSError, ValueError, TypeError):
        failures.append("artifact_unreadable")

    if tuning_evidence is not None and (
        not tuning_evidence.floor_passed
        or not tuning_evidence.budget_passed
        or tuning_evidence.failures
    ):
        failures.append("tuning_failed")

    report = _signed(
        TestReport(
            spec_hash=spec.spec_hash,
            artifact_digest=digest,
            passed=not failures,
            floor_score=primary_score,
            floor_limit=spec.floor.limit,
            floor_kind=spec.floor.kind,
            secondary_floor_score=secondary_score,
            secondary_floor_limit=(
                spec.secondary_floor.limit if spec.secondary_floor is not None else None
            ),
            secondary_floor_kind=(
                spec.secondary_floor.kind if spec.secondary_floor is not None else None
            ),
            tokens=max((item.max_tokens for item in measurements), default=0),
            cost_usd=max((item.max_cost_usd for item in measurements), default=0),
            mean_cost_usd=mean_cost_usd,
            compute_s=max((item.max_compute_s for item in measurements), default=0),
            component_count=candidate.component_count,
            failures=tuple(dict.fromkeys(failures)),
            verification_id=uuid4().hex,
            held_out_count=held_out_count,
            held_out_counts=held_out_counts,
            attempt=retake.attempt if retake is not None else None,
            # A retake that never reached its cases still names what it was to run on.
            retake_cases=(retake_cases or "same_exam") if retake is not None else None,
            # Which exam this was, where the exam may be retaken: a retake checks it.
            exam_sha256=floor.exam_digest if floor.retakes_allowed else None,
        )
    )
    result = candidate.model_copy(update={"report": report})
    try:
        result.save()
    except (OSError, ValueError, TypeError):
        failed = report.model_copy(
            update={
                "passed": False,
                "failures": (*report.failures, "artifact_save_failed"),
            }
        )
        failed = _signed(failed)
        _persist_failure(candidate.directory, failed)
        result = candidate.model_copy(update={"report": failed})
    return result
