"""Operator-authored case files for ``forge run --cases FILE`` (Phase 10 Section 12.1).

The file is JSONL of ``{"case_id", "task", "expected"}`` rows (the ``EvaluationCase``
shape, optional ``metadata``), at most 10,000 rows and 10 MB. The Spec floor must be
``held_out_tasks``; its ``dataset`` becomes ``"cases:" + sha256(file_bytes)[:16]`` so
the ``spec_hash`` binds the exact case set. The partition is ``FloorEvaluator``'s own
sorted-id even/odd split: the caller never chooses it. Case content never leaves
this module except inside the floor evaluator; errors are fixed codes only.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import JsonValue, ValidationError

from architect.floors import EvaluationCase, FloorError, FloorEvaluator, HeldOutTasks
from architect.spec import FloorDefinition, FloorKind, Spec

from .errors import ForgeModeError

__all__ = [
    "MAX_CASE_BYTES",
    "MAX_CASE_ROWS",
    "MIN_CASE_ROWS",
    "cases_floor",
    "cases_tag",
    "check_cases",
    "load_cases",
]

MAX_CASE_ROWS = 10_000
MAX_CASE_BYTES = 10 * 1024 * 1024
# Each sorted-id half must hold at least 4 cases.
MIN_CASE_ROWS = 8
_REQUIRED = frozenset({"case_id", "task", "expected"})
_ALLOWED = _REQUIRED | {"metadata"}

FloorFactory = Callable[[FloorDefinition], FloorEvaluator]


def cases_tag(data: bytes) -> str:
    """The floor ``dataset`` that binds this exact case file into the ``spec_hash``."""
    return "cases:" + hashlib.sha256(data).hexdigest()[:16]


def _unique(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject(value: str) -> None:
    raise ValueError("non-finite number")


def _parse(data: bytes) -> tuple[EvaluationCase, ...]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ForgeModeError("forge_cases_invalid") from None
    rows: list[EvaluationCase] = []
    seen: set[str] = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        if len(rows) >= MAX_CASE_ROWS:
            raise ForgeModeError("forge_cases_invalid")
        try:
            payload = json.loads(line, object_pairs_hook=_unique, parse_constant=_reject)
            if not isinstance(payload, dict) or not _REQUIRED <= set(payload) <= _ALLOWED:
                raise ValueError("row shape")
            case = EvaluationCase.model_validate(payload)
        except (ValueError, TypeError, RecursionError, ValidationError):
            raise ForgeModeError("forge_cases_invalid") from None
        if case.case_id in seen:
            raise ForgeModeError("forge_cases_invalid")
        seen.add(case.case_id)
        rows.append(case)
    return tuple(rows)


def check_cases(cases: Sequence[EvaluationCase], spec: Spec) -> None:
    """Floor kind, then every task and expected value against the Spec's schemas."""
    if spec.floor.kind != FloorKind.HELD_OUT_TASKS:
        raise ForgeModeError("forge_cases_floor_kind")
    try:
        Draft202012Validator.check_schema(spec.input_schema)
        Draft202012Validator.check_schema(spec.output_schema)
    except SchemaError:
        raise ForgeModeError("forge_cases_invalid") from None
    inputs = Draft202012Validator(spec.input_schema)
    outputs = Draft202012Validator(spec.output_schema)
    for case in cases:
        if not inputs.is_valid(case.task) or not outputs.is_valid(case.expected):
            raise ForgeModeError("forge_cases_invalid")


def load_cases(path: Path, spec: Spec | None = None) -> tuple[tuple[EvaluationCase, ...], str]:
    """Read and validate one case file; returns ``(cases, dataset tag)``.

    With a Spec the floor kind is checked first and every row against its schemas.
    Without one (a prose request) only the rows are checked here; ``check_cases`` runs
    once Stage 0 has produced the Spec. Fewer than 8 rows is ``forge_cases_too_few``.
    """
    if spec is not None and spec.floor.kind != FloorKind.HELD_OUT_TASKS:
        raise ForgeModeError("forge_cases_floor_kind")
    try:
        resolved = Path(path).resolve(strict=True)
        if not resolved.is_file() or resolved.stat().st_size > MAX_CASE_BYTES:
            raise ForgeModeError("forge_cases_invalid")
        data = resolved.read_bytes()
    except OSError:
        raise ForgeModeError("forge_cases_invalid") from None
    if len(data) > MAX_CASE_BYTES:
        raise ForgeModeError("forge_cases_invalid")
    cases = _parse(data)
    if spec is not None:
        check_cases(cases, spec)
    if len(cases) < MIN_CASE_ROWS:
        raise ForgeModeError("forge_cases_too_few")
    return cases, cases_tag(data)


def cases_floor(
    match: FloorDefinition | str,
    cases: Sequence[EvaluationCase],
    fallback: FloorFactory | None,
) -> FloorFactory:
    """A per-call floor (and smoke) factory: the case set for exactly one definition.

    ``match`` is the exact effective definition, or, for a prose request whose floor
    is known only after Stage 0, the dataset tag. Any other definition delegates to
    ``fallback`` unchanged. A tagged floor of another kind is refused (the compile then
    stops with ``floor_unavailable`` before Stage 1, which ``forge run`` reports as
    ``forge_cases_floor_kind``).
    """
    sealed = tuple(cases)

    def factory(definition: FloorDefinition) -> FloorEvaluator:
        if isinstance(match, FloorDefinition):
            hit = definition == match
        else:
            hit = definition.dataset == match
            if hit and definition.kind != FloorKind.HELD_OUT_TASKS:
                raise FloorError("forge_cases_floor_kind")
        if hit:
            return HeldOutTasks(definition, sealed)
        if fallback is None:
            raise FloorError("floor_not_configured")
        return fallback(definition)

    return factory
