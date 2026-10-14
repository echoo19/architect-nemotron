"""Closed table of trusted task checkers for ``HeldOutTasks``.

Each checker is host code comparing a trusted ``expected`` value with a
candidate output; no model text is executed or interpreted beyond JSON. The
table is closed: a profile or suite names a checker by ``CheckerName`` and
``CHECKERS`` maps it to the function, so no configuration can supply code.

``field_report`` gives the per-field counts behind a checker's verdict; it
holds numbers only (field names are fixed labels from this module, never
content). A checker passes exactly when every field it reports matched.
Malformed candidate output never raises: it simply matches nothing. Malformed
``expected`` data is a trusted-configuration error and raises ``FloorError``.
"""

from __future__ import annotations

import difflib
import math
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue

from .base import FloorError

__all__ = [
    "CHECKERS",
    "FIELDS_READ",
    "RECEIPT_FIELDS",
    "Checker",
    "CheckerName",
    "FieldReport",
    "exam_extraction_v1",
    "field_report",
    "invoice_extraction_v1",
    "receipt_v1",
    "similar",
]

Checker = Callable[[JsonValue, JsonValue], bool]
CheckerName = Literal["equality", "exam_extraction_v1", "invoice_extraction_v1", "receipt_v1"]
# examples/receipts/RULES.md: four exact fields. Rule versions receipts-v1 and receipts-v2
# word the prompt rules differently; the four fields and exact match are the same in
# both, so one checker serves both.
RECEIPT_FIELDS: tuple[str, ...] = ("total", "subtotal", "tax", "item_count")

SIMILARITY = 0.9
TOTAL_TOLERANCE = 0.005
_EXACT_TOLERANCE = 1e-9


class FieldReport(BaseModel):
    """Counts only: how many checked fields matched, overall and per fixed label."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fields: int
    matched: int
    per_field: dict[str, tuple[int, int]]  # name -> (matched, total)


class _Tally:
    def __init__(self) -> None:
        self.counts: dict[str, list[int]] = {}

    def add(self, name: str, matched: bool, total: int = 1) -> None:
        entry = self.counts.setdefault(name, [0, 0])
        entry[0] += total if matched else 0
        entry[1] += total

    def report(self) -> FieldReport:
        per_field = {name: (pair[0], pair[1]) for name, pair in sorted(self.counts.items())}
        return FieldReport(
            fields=sum(total for _, total in per_field.values()),
            matched=sum(matched for matched, _ in per_field.values()),
            per_field=per_field,
        )


def _normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


def similar(a: str, b: str) -> float:
    """difflib ratio on whitespace-collapsed, casefolded text (1.0 for two empty strings)."""
    return difflib.SequenceMatcher(None, _normalize(a), _normalize(b), autojunk=False).ratio()


def _similar_enough(expected: JsonValue, output: JsonValue) -> bool:
    return (
        isinstance(expected, str)
        and isinstance(output, str)
        and similar(expected, output) >= SIMILARITY
    )


def _number(value: JsonValue) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _close(expected: JsonValue, output: JsonValue, tolerance: float) -> bool:
    left, right = _number(expected), _number(output)
    return left is not None and right is not None and abs(left - right) <= tolerance


def _integer(value: JsonValue) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _label(value: JsonValue) -> str | None:
    return value.strip().upper() if isinstance(value, str) else None


def _malformed() -> FloorError:
    return FloorError("Checker expected value is malformed")


def _object(value: JsonValue) -> dict[str, JsonValue] | None:
    return value if isinstance(value, dict) else None


def _list(value: JsonValue) -> list[JsonValue] | None:
    return value if isinstance(value, list) else None


def _index(
    items: list[JsonValue] | None, key: str, normalize: Callable[[JsonValue], object]
) -> dict[object, dict[str, JsonValue]]:
    """Map key -> item for dict items whose key is valid and unique; duplicates are dropped."""
    found: dict[object, dict[str, JsonValue]] = {}
    duplicates: set[object] = set()
    for item in items or []:
        entry = _object(item)
        if entry is None:
            continue
        value = normalize(entry.get(key))
        if value is None:
            continue
        if value in found:
            duplicates.add(value)
        found[value] = entry
    for value in duplicates:
        found.pop(value)
    return found


def _exam_report(expected: JsonValue, output: JsonValue) -> FieldReport:
    truth = _object(expected)
    questions = _list(truth.get("questions")) if truth is not None else None
    if truth is None or questions is None or not isinstance(truth.get("answer_key_page"), bool):
        raise _malformed()
    candidate = _object(output) or {}
    produced = _list(candidate.get("questions"))
    tally = _Tally()
    tally.add("question_count", produced is not None and len(produced) == len(questions))
    tally.add(
        "answer_key_page",
        isinstance(candidate.get("answer_key_page"), bool)
        and candidate.get("answer_key_page") == truth["answer_key_page"],
    )
    by_number = _index(produced, "number", _integer)
    for raw in questions:
        question = _object(raw)
        number = _integer(question.get("number")) if question is not None else None
        choices = _list(question.get("choices")) if question is not None else None
        if question is None or number is None or choices is None:
            raise _malformed()
        match = by_number.get(number, {})
        key = _label(question.get("answer_key"))
        if key is None:
            raise _malformed()
        tally.add("answer_key", _label(match.get("answer_key")) == key)
        tally.add("stem", _similar_enough(question.get("stem"), match.get("stem")))
        produced_choices = _list(match.get("choices"))
        tally.add(
            "choice_count",
            produced_choices is not None and len(produced_choices) == len(choices),
        )
        by_label = _index(produced_choices, "label", _label)
        for raw_choice in choices:
            choice = _object(raw_choice)
            label = _label(choice.get("label")) if choice is not None else None
            if choice is None or label is None:
                raise _malformed()
            tally.add(
                "choice_text",
                _similar_enough(choice.get("text"), by_label.get(label, {}).get("text")),
            )
    return tally.report()


def _invoice_field(
    tally: _Tally,
    name: str,
    expected: dict[str, JsonValue],
    output: dict[str, JsonValue],
    compare: Callable[[JsonValue, JsonValue], bool],
) -> None:
    if name not in expected:
        raise _malformed()
    value = expected[name]
    if value is None:
        # A value the source does not contain must stay null, never be invented.
        tally.add(name, name in output and output[name] is None)
    else:
        tally.add(name, compare(value, output.get(name)))


def _invoice_report(expected: JsonValue, output: JsonValue) -> FieldReport:
    truth = _object(expected)
    items = _list(truth.get("line_items")) if truth is not None else None
    if truth is None or items is None:
        raise _malformed()
    candidate = _object(output) or {}
    tally = _Tally()
    _invoice_field(tally, "vendor", truth, candidate, _similar_enough)
    _invoice_field(tally, "date", truth, candidate, lambda a, b: a == b and type(a) is type(b))
    _invoice_field(
        tally, "total", truth, candidate, lambda a, b: _close(a, b, TOTAL_TOLERANCE)
    )
    produced = _list(candidate.get("line_items"))
    tally.add("line_item_count", produced is not None and len(produced) == len(items))
    for position, raw in enumerate(items):
        item = _object(raw)
        if item is None:
            raise _malformed()
        match: dict[str, JsonValue] = {}
        if produced is not None and position < len(produced):
            match = _object(produced[position]) or {}
        _invoice_field(tally, "description", item, match, _similar_enough)
        _invoice_field(
            tally, "quantity", item, match, lambda a, b: _close(a, b, _EXACT_TOLERANCE)
        )
        _invoice_field(
            tally, "unit_price", item, match, lambda a, b: _close(a, b, _EXACT_TOLERANCE)
        )
    return tally.report()


def _equality_report(expected: JsonValue, output: JsonValue) -> FieldReport:
    tally = _Tally()
    tally.add("output", output == expected)
    return tally.report()


def _receipt_amount(value: JsonValue, *, nullable: bool) -> bool:
    """A well-formed expected receipt value: an int of at least 0, or null when allowed."""
    if value is None:
        return nullable
    number = _integer(value)
    return number is not None and number >= 0


def _receipt_report(expected: JsonValue, output: JsonValue) -> FieldReport:
    truth = _object(expected)
    if truth is None or any(
        name not in truth or not _receipt_amount(truth[name], nullable=name != "item_count")
        for name in RECEIPT_FIELDS
    ):
        raise _malformed()
    candidate = _object(output) or {}
    tally = _Tally()
    for name in RECEIPT_FIELDS:
        value = truth[name]
        if name not in candidate:
            tally.add(name, False)
        elif value is None:
            tally.add(name, candidate[name] is None)
        else:
            # An int, never a bool or a float: 75000.0 does not match 75000.
            produced = _integer(candidate[name])
            tally.add(name, produced is not None and produced == value)
    return tally.report()


_REPORTS: Mapping[CheckerName, Callable[[JsonValue, JsonValue], FieldReport]] = (
    MappingProxyType(
        {
            "equality": _equality_report,
            "exam_extraction_v1": _exam_report,
            "invoice_extraction_v1": _invoice_report,
            "receipt_v1": _receipt_report,
        }
    )
)


def field_report(expected: JsonValue, output: JsonValue, *, checker: CheckerName) -> FieldReport:
    """Per-field match counts behind ``checker``'s verdict; counts only, no content."""
    try:
        report = _REPORTS[checker]
    except KeyError as exc:
        raise FloorError("Unknown checker") from exc
    return report(expected, output)


def _passes(report: FieldReport) -> bool:
    return report.fields > 0 and report.matched == report.fields


def exam_extraction_v1(expected: JsonValue, output: JsonValue) -> bool:
    """Same question count; per question (by ``number``): answer key equal after
    upper-casing, stem similarity >= 0.9, same choice count and each same-label
    choice text similarity >= 0.9; ``answer_key_page`` equal."""
    return _passes(_exam_report(expected, output))


def invoice_extraction_v1(expected: JsonValue, output: JsonValue) -> bool:
    """Vendor similarity >= 0.9, date equal, total within 0.005, same line-item
    count with each description similarity >= 0.9 and quantity and unit price
    equal; every expected null must be null in the output."""
    return _passes(_invoice_report(expected, output))


def receipt_v1(expected: JsonValue, output: JsonValue) -> bool:
    """``total``, ``subtotal``, ``tax`` and ``item_count`` all exactly equal, under
    any rule version of examples/receipts/RULES.md: an expected null needs an output
    null, an expected int an output int (never a bool or a float); keys beyond the
    four are ignored."""
    return _passes(_receipt_report(expected, output))


def _equality(expected: JsonValue, output: JsonValue) -> bool:
    return output == expected


# The checkers that read a fixed list of answer fields and ignore every other key of the
# output. For such a set an agent may give back more fields than the list: they are not
# examined (``session.floors.fit_to_set``). A checker is listed here only when its code
# does exactly that; ``receipt_v1`` reads ``RECEIPT_FIELDS`` by name and nothing else.
FIELDS_READ: Mapping[CheckerName, tuple[str, ...]] = MappingProxyType(
    {"receipt_v1": RECEIPT_FIELDS}
)

CHECKERS: Mapping[CheckerName, Checker] = MappingProxyType(
    {
        "equality": _equality,
        "exam_extraction_v1": exam_extraction_v1,
        "invoice_extraction_v1": invoice_extraction_v1,
        "receipt_v1": receipt_v1,
    }
)
