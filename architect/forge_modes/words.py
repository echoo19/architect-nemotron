"""An exam report in plain words, for a terminal, an app's log and Architect's screens.

The same few lines everywhere: how many cases the agent got right, the pass mark as a
count, which attempt at the exam that was and which cases it ran on. Counts, a model
name and fixed words only: no case, no path and no error code.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

from architect import trust
from architect.spec import FloorKind, HarnessArtifact, TestReport

from .export import check_package, read_pin, stop_error

__all__ = [
    "attempt_of",
    "exam_counts",
    "exam_line",
    "pass_mark",
    "pretty_model",
    "source_line",
    "times",
    "verdict_line",
]

_NEMOTRON = re.compile(r"^(?:nvidia/)?(?:nvidia-)?nemotron-(\d+(?:_\d+)?)-([a-z]+)", re.I)


def pretty_model(model: str) -> str:
    """A model id as people read it: ``nvidia/Nemotron-3_5-Lightning`` is "Nemotron 3.5
    Lightning" (the desktop app's rule, ``desktop/src/lib/models.ts``). Any other id stays
    as it is, cut to printable ASCII."""
    found = _NEMOTRON.match(model)
    if found is not None:
        return f"Nemotron {found[1].replace('_', '.')} {found[2].capitalize()}"
    return "".join(char if " " <= char <= "~" else "?" for char in model[:80])


def pass_mark(limit: float, cases: int) -> int:
    """The fewest right answers out of ``cases`` that reach the pass mark ``limit``."""
    return max(0, math.ceil(limit * cases - 1e-9))


def exam_counts(report: TestReport) -> tuple[int, int, int] | None:
    """``(right, cases, pass mark)`` of a report scored by counting right answers; None
    for a floor scored another way or an exam that was never scored."""
    if report.floor_kind != FloorKind.HELD_OUT_TASKS:
        return None
    counts = report.held_out_counts
    if counts is not None:
        if counts.correct is None or not counts.cases:
            return None
        return counts.correct, counts.cases, pass_mark(report.floor_limit, counts.cases)
    if not report.held_out_count or report.secondary_floor_kind is not None:
        return None
    cases = report.held_out_count
    return round(report.floor_score * cases), cases, pass_mark(report.floor_limit, cases)


def attempt_of(report: TestReport) -> int:
    """Which attempt at the exam a report is; a first exam carries no number and is 1."""
    return report.attempt or 1


def _score(report: TestReport) -> str:
    counts = exam_counts(report)
    if counts is None:
        return f"Score {report.floor_score:.2f}. Pass mark {report.floor_limit:.2f}."
    right, cases, need = counts
    return f"{right} of {cases}. Pass mark {need}."


def verdict_line(model: str, report: TestReport) -> str:
    """``Nemotron 3 Super. 59 of 100. Pass mark 40. Passed. Attempt 1.``"""
    outcome = "Passed" if report.passed else "Did not pass"
    return f"{pretty_model(model)}. {_score(report)} {outcome}. Attempt {attempt_of(report)}."


def times(count: int) -> str:
    return {1: "once", 2: "twice"}.get(count, f"{count} times")


def source_line(report: TestReport, unit: str = "cases") -> str | None:
    """Which cases a retake ran on, or None for a first exam: ``Attempt 2 was on 100
    cases no earlier attempt used.`` or ``Attempt 3 was the same exam as attempt 1.``"""
    if report.attempt is None:
        return None
    if report.retake_cases == "unused":
        counts = exam_counts(report)
        size = f"{counts[1]} " if counts is not None else ""
        return f"Attempt {report.attempt} was on {size}{unit} no earlier attempt used."
    return f"Attempt {report.attempt} was the same exam as attempt 1."


def exam_line(package_dir: Path) -> str:
    """What an app prints once when it loads its agent: ``passed its exam. 59 of 100, pass
    mark 40, attempt 1.`` The package is checked first, as at every load, and a stopped one
    raises ``ExportError`` with its ``sentence`` and ``reason_line``. No model is called."""
    root = Path(package_dir)
    with trust.configured_only():
        check = check_package(root, read_pin(root), registry=None)
        if check.codes:
            raise stop_error(root, check.codes[0])
        try:
            report = HarnessArtifact.load(root / "artifact").report
        except (OSError, ValueError, TypeError):
            raise stop_error(root, "export_integrity_mismatch") from None
    if report is None:
        raise stop_error(root, "export_unverified")
    counts = exam_counts(report)
    attempt = f"attempt {attempt_of(report)}"
    if counts is None:
        return (
            f"passed its exam. Score {report.floor_score:.2f}, pass mark "
            f"{report.floor_limit:.2f}, {attempt}."
        )
    right, cases, need = counts
    return f"passed its exam. {right} of {cases}, pass mark {need}, {attempt}."
