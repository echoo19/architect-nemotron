"""Submit-time literal-leak lint for Forge harness source (Phase 9, Section 10.3).

A harness may not carry tuning answers or long tuning inputs as string literals:
tuning labels are shown to the agent, so copying them into code would turn the
tuning partition into a lookup table. Only the offending line numbers leave this
module; the literal text and the matched case never do.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator, Sequence

from pydantic import JsonValue

from architect.floors import EvaluationCase

__all__ = ["LEAK_MIN_CHARS", "leaked_literals"]

LEAK_MIN_CHARS = 24


def _leaves(value: JsonValue) -> Iterator[str]:
    pending: list[JsonValue] = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, dict):
            pending.extend(item.values())


def _needles(tuning: Sequence[EvaluationCase]) -> frozenset[str]:
    needles: set[str] = set()
    for case in tuning:
        for value in (case.task, case.expected):
            needles.update(leaf for leaf in _leaves(value) if len(leaf) >= LEAK_MIN_CHARS)
        try:
            canonical = json.dumps(
                case.expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
        except (TypeError, ValueError):
            continue
        if len(canonical) >= LEAK_MIN_CHARS:
            needles.add(canonical)
    return frozenset(needles)


def leaked_literals(source: str, tuning: Sequence[EvaluationCase]) -> tuple[int, ...]:
    """Line numbers of string literals (and f-string literal parts) that leak tuning data.

    A literal of 24 or more characters leaks when it equals or contains a str leaf
    of 24 or more characters from any tuning ``task`` or ``expected``, or the
    canonical JSON of any tuning ``expected`` of 24 or more characters. Source that
    does not parse has no literals to report; validation rejects it separately.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return ()
    needles = _needles(tuning)
    if not needles:
        return ()
    lines: set[int] = set()
    for node in ast.walk(tree):
        # ast.walk reaches the literal Constant parts inside every JoinedStr too.
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and len(node.value) >= LEAK_MIN_CHARS
            and any(needle in node.value for needle in needles)
        ):
            lines.add(node.lineno)
    return tuple(sorted(lines))
