"""Phase 9 Section 10.3: the submit-time literal-leak lint."""

from __future__ import annotations

from architect.compiler.forge_lint import LEAK_MIN_CHARS, leaked_literals
from architect.floors import EvaluationCase

LONG_INPUT = "the quarterly invoice from ACME Corporation"
LONG_EXPECTED = "Net amount due within thirty days"
TUNING = (
    EvaluationCase(
        case_id="a",
        task={"text": LONG_INPUT, "short": "tiny"},
        expected={"answer": LONG_EXPECTED, "n": 3},
    ),
    EvaluationCase(case_id="b", task={"text": "short"}, expected="positive"),
)

HEADER = (
    "from pydantic import JsonValue\n"
    "from architect.runtime import RunContext\n\n"
    "async def run(context: RunContext, task: JsonValue) -> JsonValue:\n"
)


def test_short_literals_and_unrelated_long_literals_pass() -> None:
    body = '    x = "positive"\n    y = "tiny"\n'
    source = HEADER + body + '    return "an unrelated but long literal text"\n'
    assert leaked_literals(source, TUNING) == ()


def test_expected_leaf_is_rejected_with_its_line() -> None:
    source = HEADER + '    a = "ok"\n    return "' + LONG_EXPECTED + '"\n'
    assert leaked_literals(source, TUNING) == (6,)


def test_input_leaf_contained_in_a_longer_literal_is_rejected() -> None:
    source = HEADER + '    prompt = "Example: ' + LONG_INPUT + ' -> answer"\n    return prompt\n'
    assert leaked_literals(source, TUNING) == (5,)


def test_canonical_json_of_expected_is_rejected() -> None:
    canonical = '{"answer":"Net amount due within thirty days","n":3}'
    source = HEADER + "    return " + repr(canonical) + "\n"
    assert leaked_literals(source, TUNING) == (5,)


def test_fstring_literal_parts_are_checked() -> None:
    source = HEADER + '    return f"{task} ' + LONG_EXPECTED + '"\n'
    assert leaked_literals(source, TUNING) == (5,)


def test_threshold_is_24_characters() -> None:
    assert LEAK_MIN_CHARS == 24
    needle = "x" * 23
    tuning = (EvaluationCase(case_id="c", task={}, expected=needle),)
    assert leaked_literals(HEADER + f'    return "{needle}"\n', tuning) == ()
    longer = "y" * 24
    tuning = (EvaluationCase(case_id="c", task={}, expected=longer),)
    assert leaked_literals(HEADER + f'    return "{longer}"\n', tuning) == (5,)


def test_unparsable_source_reports_nothing() -> None:
    assert leaked_literals("def (:", TUNING) == ()


def test_several_leaks_are_sorted_and_unique() -> None:
    source = (
        HEADER
        + '    a = "' + LONG_INPUT + '"\n'
        + '    b = "' + LONG_EXPECTED + '"\n'
        + '    c = "' + LONG_INPUT + '"\n'
        + "    return a\n"
    )
    assert leaked_literals(source, TUNING) == (5, 6, 7)
