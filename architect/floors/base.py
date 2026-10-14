"""Floor data and the common partition gate.

All dataset state and issued verification provenance live outside evaluator and
case instance dictionaries. Tuning copies carry no authority. A verification
case is authorized only while the exact issued object survives and its content
matches the sealed held-out dataset. Copying, reconstructing, or changing it
loses authority. This is a private trusted-Python capability boundary, not a
sandbox against reflective code or a verifier that deliberately leaks data.

Retake: a floor may hold extra exam cases for retakes (``_allow_retakes``). They are
read only when a retake asks for them, never by a build or a practice run, and are issued
through the same private capability as the held-out cases. A floor refuses retakes
unless it was told to allow them.
"""

from __future__ import annotations

import hashlib
import json
import math
from abc import ABC, abstractmethod
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Literal, Self
from weakref import WeakKeyDictionary, WeakValueDictionary

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator

from architect.spec import FloorDefinition, FloorKind

from . import _access


class FloorError(ValueError):
    """Invalid dataset, candidate result, or floor configuration."""


class HeldOutAccessError(PermissionError):
    """Verification data requires the private stage-4 capability."""


class CalibrationError(FloorError):
    """A judge lacks trustworthy human calibration."""


class EvaluationCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    case_id: Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
    task: JsonValue
    expected: JsonValue
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class EditLogRow(BaseModel):
    """One UTF-8 JSON object per nonblank line (no header).

    Required keys: case_id (unique string), task (JSON input), proposed (JSON
    original output), shipped (JSON human-approved output), changed (boolean).
    changed must equal proposed != shipped. Example:
    {"case_id":"1","task":{"x":1},"proposed":"a","shipped":"b","changed":true}
    Historic changed is checked for log integrity but NEVER used as the replay
    score: the candidate's new output is compared with shipped.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    case_id: Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
    task: JsonValue
    proposed: JsonValue
    shipped: JsonValue
    changed: Annotated[bool, Field(strict=True)]

    @model_validator(mode="after")
    def consistent_edit(self) -> Self:
        if self.changed != (self.proposed != self.shipped):
            raise ValueError("changed must equal proposed != shipped")
        return self


class CalibrationRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    case_id: Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
    task: JsonValue
    output: JsonValue
    human_score: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


def unit_score(value: object) -> float:
    """Reject booleans, strings, NaN and infinity instead of coercing scores."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FloorError("Score must be numeric")
    if not 0 <= value <= 1 or not math.isfinite(value):
        raise FloorError("Score must be finite and in [0, 1]")
    return float(value)


def _fingerprint(case: EvaluationCase) -> str:
    return json.dumps(case.model_dump(mode="json"), sort_keys=True, allow_nan=False)


def _content(case: EvaluationCase) -> str:
    """A case without its id: the same task and answer under another id is the same case."""
    return json.dumps(
        {"task": case.task, "expected": case.expected}, sort_keys=True, allow_nan=False
    )


@dataclass(repr=False)
class _Partitions:
    definition: FloorDefinition
    tuning: tuple[EvaluationCase, ...]
    held_out: tuple[EvaluationCase, ...]
    tuning_digests: frozenset[str]
    held_out_digests: frozenset[str]
    issued: WeakValueDictionary[int, EvaluationCase] = field(default_factory=WeakValueDictionary)
    # Retake: off unless ``_allow_retakes`` turned it on. ``retake_source`` reads the
    # extra exam cases on first use; ``retake_pool`` keeps them in their fixed order.
    retakes: bool = False
    retake_source: Callable[[], Sequence[EvaluationCase]] | None = None
    retake_pool: tuple[EvaluationCase, ...] | None = None
    retake_digests: frozenset[str] = frozenset()


_PARTITIONS: WeakKeyDictionary[FloorEvaluator, _Partitions] = WeakKeyDictionary()

# The fixed order retakes draw their cases in. Changing the tag reorders every floor's
# retake cases, so it never changes.
RETAKE_ORDER_TAG = "retake-v1:"


def retake_order(case_id: str) -> str:
    return hashlib.sha256((RETAKE_ORDER_TAG + case_id).encode("utf-8")).hexdigest()


class FloorEvaluator(ABC):
    """Deterministic sorted-ID even/odd split; callers cannot choose partitions."""

    def __init__(self, definition: FloorDefinition, records: Sequence[EvaluationCase]) -> None:
        definition = FloorDefinition.model_validate(definition.model_dump(mode="json"))
        try:
            sealed = tuple(
                EvaluationCase.model_validate(row.model_dump(mode="json")) for row in records
            )
            ids = [row.case_id for row in sealed]
            if len(set(ids)) != len(ids):
                raise FloorError("Duplicate dataset case IDs")
            ordered = tuple(sorted(sealed, key=lambda row: row.case_id))
            tuning, held_out = ordered[::2], ordered[1::2]
            minimum = (
                definition.minimum_samples if definition.kind == FloorKind.HUMAN_EDIT_RATE else 1
            )
            if min(len(tuning), len(held_out)) < minimum:
                raise FloorError("Insufficient samples in tuning or held-out partition")
            _PARTITIONS[self] = _Partitions(
                definition=definition,
                tuning=tuning,
                held_out=held_out,
                tuning_digests=frozenset(_fingerprint(case) for case in tuning),
                held_out_digests=frozenset(_fingerprint(case) for case in held_out),
            )
        except (TypeError, ValueError) as exc:
            raise FloorError("Invalid evaluation dataset") from exc

    @property
    def definition(self) -> FloorDefinition:
        return _PARTITIONS[self].definition.model_copy(deep=True)

    @property
    def tuning(self) -> tuple[EvaluationCase, ...]:
        return tuple(case.model_copy(deep=True) for case in _PARTITIONS[self].tuning)

    @property
    def held_out(self) -> tuple[EvaluationCase, ...]:
        raise HeldOutAccessError("Held-out cases are private to stage 4")

    def _verification_cases(self, capability: object) -> tuple[EvaluationCase, ...]:
        if not _access.is_verifier(capability):
            raise HeldOutAccessError("Invalid verification capability")
        partitions = _PARTITIONS[self]
        cases = tuple(case.model_copy(deep=True) for case in partitions.held_out)
        for case in cases:
            partitions.issued[id(case)] = case
        return cases

    def _allow_retakes(
        self, source: Callable[[], Sequence[EvaluationCase]] | None = None
    ) -> None:
        """Let this floor's exam be retaken; ``source`` reads its extra exam cases.

        Trusted set-up only (``profiles.floor_factory``). ``source`` is not called here:
        a build or a practice run never reads the extra cases.
        """
        partitions = _PARTITIONS[self]
        partitions.retakes = True
        partitions.retake_source = source

    @property
    def retakes_allowed(self) -> bool:
        return _PARTITIONS[self].retakes

    @property
    def exam_size(self) -> int:
        """How many cases the exam holds: a count, never the cases."""
        return len(_PARTITIONS[self].held_out)

    @property
    def exam_digest(self) -> str:
        """One digest of the exam's cases (ids, tasks and answers), in no order: which
        exam this is, never the cases. A report signs it (``TestReport.exam_sha256``), so
        a retake can tell that the exam it is offered is the one the agent passed."""
        digest = hashlib.sha256()
        for item in sorted(_PARTITIONS[self].held_out_digests):
            digest.update(hashlib.sha256(item.encode("utf-8")).digest())
        return digest.hexdigest()

    def _retake_pool(self) -> tuple[EvaluationCase, ...]:
        """The extra exam cases in their fixed order (``retake_order``), read once.

        A case that is also in the practice half or the exam, by id or by content, would
        not be unused: such a file is refused whole."""
        partitions = _PARTITIONS[self]
        if partitions.retake_pool is None:
            if partitions.retake_source is None:
                partitions.retake_pool = ()
                return ()
            try:
                rows = tuple(
                    EvaluationCase.model_validate(row.model_dump(mode="json"))
                    for row in partitions.retake_source()
                )
                digests = frozenset(_fingerprint(row) for row in rows)
            except FloorError:
                raise
            except (OSError, TypeError, ValueError) as exc:
                raise FloorError("Invalid retake cases") from exc
            ids = {row.case_id for row in rows}
            contents = {_content(row) for row in rows}
            own = (*partitions.tuning, *partitions.held_out)
            if (
                len(ids) != len(rows)
                or len(contents) != len(rows)
                or ids & {case.case_id for case in own}
                or contents & {_content(case) for case in own}
            ):
                raise FloorError("Retake cases repeat each other or the floor's own cases")
            partitions.retake_pool = tuple(
                sorted(rows, key=lambda row: retake_order(row.case_id))
            )
            partitions.retake_digests = digests
        return partitions.retake_pool

    def retake_plan(self, used: Collection[str]) -> tuple[Literal["unused", "same_exam"], int]:
        """What a retake would run on, without issuing a case: ``("unused", left)`` when a
        full exam of cases that no id in ``used`` names is left (``left`` counts them all),
        else ``("same_exam", left)``. Counts only."""
        if not self.retakes_allowed:
            raise HeldOutAccessError("This exam cannot be retaken")
        left = sum(1 for case in self._retake_pool() if case.case_id not in used)
        return ("unused" if left >= self.exam_size else "same_exam"), left

    def _retake_cases(
        self, capability: object, used: Collection[str], size: int
    ) -> tuple[EvaluationCase, ...] | None:
        """The first ``size`` extra exam cases, in their fixed order, that no id in
        ``used`` names; None when fewer are left, and the retake is then the same exam.
        Issued like ``_verification_cases``, to the verifier capability only."""
        if not _access.is_verifier(capability):
            raise HeldOutAccessError("Invalid verification capability")
        if not self.retakes_allowed:
            raise HeldOutAccessError("This exam cannot be retaken")
        partitions = _PARTITIONS[self]
        unused = [case for case in self._retake_pool() if case.case_id not in used]
        if size <= 0 or len(unused) < size:
            return None
        cases = tuple(case.model_copy(deep=True) for case in unused[:size])
        for case in cases:
            partitions.issued[id(case)] = case
        return cases

    def _validate_score_inputs(
        self, cases: Sequence[EvaluationCase], outputs: Sequence[JsonValue]
    ) -> None:
        if not cases or len(cases) != len(outputs):
            raise FloorError("Scoring requires nonempty equal case and output lengths")
        if len({case.case_id for case in cases}) != len(cases):
            raise FloorError("Duplicate scoring case IDs")
        partitions = _PARTITIONS[self]
        kinds: set[str] = set()
        for case in cases:
            try:
                digest = _fingerprint(case)
            except (ValueError, TypeError) as exc:
                raise HeldOutAccessError("Invalid case content") from exc
            if digest in partitions.tuning_digests:
                kinds.add("tuning")
            elif (
                digest in partitions.held_out_digests or digest in partitions.retake_digests
            ) and partitions.issued.get(id(case)) is case:
                kinds.add("verification")
            else:
                raise HeldOutAccessError("Unknown, changed, or unauthorized case")
        if len(kinds) != 1:
            raise HeldOutAccessError("Cannot mix tuning and verification cases")
        if (
            partitions.definition.kind == FloorKind.HUMAN_EDIT_RATE
            and len(cases) < partitions.definition.minimum_samples
        ):
            raise FloorError("Insufficient replay samples")
        try:
            json.dumps(list(outputs), allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise FloorError("Candidate outputs must be finite JSON values") from exc

    @abstractmethod
    async def score(self, cases: Sequence[EvaluationCase], outputs: Sequence[JsonValue]) -> float:
        """Score new candidate outputs on one authorized partition."""
        ...

    def passes(self, score: float) -> bool:
        score = unit_score(score)
        definition = self.definition
        if definition.kind == FloorKind.HUMAN_EDIT_RATE:
            return score <= definition.limit
        return score >= definition.limit
