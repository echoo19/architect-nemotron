"""Programmatic, human-replay, and game quality floors."""

from collections.abc import Callable, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Protocol

from pydantic import JsonValue, ValidationError

from architect.spec import FloorDefinition, FloorKind

from .base import EditLogRow, EvaluationCase, FloorError, FloorEvaluator, unit_score


def _require_kind(definition: FloorDefinition, kind: FloorKind) -> None:
    if definition.kind != kind:
        raise FloorError("Floor kind does not match evaluator")


class HeldOutTasks(FloorEvaluator):
    """Fraction passing checker(expected, candidate_output); default is equality."""

    def __init__(
        self,
        definition: FloorDefinition,
        records: Sequence[EvaluationCase],
        checker: Callable[[JsonValue, JsonValue], bool] | None = None,
    ) -> None:
        _require_kind(definition, FloorKind.HELD_OUT_TASKS)
        super().__init__(definition, records)
        self._checker = checker

    async def score(self, cases: Sequence[EvaluationCase], outputs: Sequence[JsonValue]) -> float:
        self._validate_score_inputs(cases, outputs)
        results: list[bool] = []
        for case, output in zip(cases, outputs, strict=True):
            result = (
                self._checker(deepcopy(case.expected), deepcopy(output))
                if self._checker is not None
                else output == case.expected
            )
            if not isinstance(result, bool):
                raise FloorError("Task checker must return a boolean")
            results.append(result)
        return sum(results) / len(results)


class HumanEditRate(FloorEvaluator):
    """Replay inputs against shipped outputs, never against historic edit bits."""

    def __init__(self, definition: FloorDefinition, records: Sequence[EditLogRow]) -> None:
        _require_kind(definition, FloorKind.HUMAN_EDIT_RATE)
        try:
            rows = [EditLogRow.model_validate(row.model_dump(mode="json")) for row in records]
        except (TypeError, ValueError) as exc:
            raise FloorError("Invalid human edit log") from exc
        super().__init__(
            definition,
            [
                EvaluationCase(case_id=row.case_id, task=row.task, expected=row.shipped)
                for row in rows
            ],
        )

    @classmethod
    def from_jsonl(cls, definition: FloorDefinition, path: Path) -> "HumanEditRate":
        """Read the EditLogRow format, rejecting malformed or inconsistent lines."""
        rows: list[EditLogRow] = []
        try:
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    if line.strip():
                        rows.append(EditLogRow.model_validate_json(line))
        except (OSError, UnicodeError, ValidationError) as exc:
            raise FloorError("Invalid or unreadable edit log JSONL") from exc
        return cls(definition, rows)

    async def score(self, cases: Sequence[EvaluationCase], outputs: Sequence[JsonValue]) -> float:
        self._validate_score_inputs(cases, outputs)
        return sum(
            output != case.expected for case, output in zip(cases, outputs, strict=True)
        ) / len(cases)


class GameScorer(Protocol):
    async def score(self, task: JsonValue, output: JsonValue) -> float:
        """Return a normalized score for just this task and candidate output."""
        ...


class HeldOutGameScore(FloorEvaluator):
    def __init__(
        self,
        definition: FloorDefinition,
        records: Sequence[EvaluationCase],
        environment: GameScorer,
    ) -> None:
        _require_kind(definition, FloorKind.HELD_OUT_GAME_SCORE)
        super().__init__(definition, records)
        self._environment = environment

    async def score(self, cases: Sequence[EvaluationCase], outputs: Sequence[JsonValue]) -> float:
        self._validate_score_inputs(cases, outputs)
        # Snapshot authorized inputs before yielding control to an external scorer.
        pairs = [
            (deepcopy(case.task), deepcopy(output))
            for case, output in zip(cases, outputs, strict=True)
        ]
        scores = [unit_score(await self._environment.score(task, output)) for task, output in pairs]
        return sum(scores) / len(scores)
