"""Comprehensions in sandbox language v2: desugared at check time into the loops they
stand for, run with their own scope, and metered exactly as those loops are."""

from __future__ import annotations

import ast
import builtins
import copy
import json
import random
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue
from sandbox_v2_support import binding, body, interpret, make_artifact, wall_limit

from architect.models import FakeAdapter
from architect.runtime import RunContext
from architect.runtime import sandbox_language as language
from architect.runtime.sandbox import (
    SandboxError,
    _Comprehension,
    _validated_program,
    sandbox_harness,
    validate_source,
)
from architect.spec import Budget, HarnessArtifact


@pytest.fixture
def v2(tmp_path: Path) -> HarnessArtifact:
    return make_artifact(tmp_path / "v2")


def run(
    monkeypatch: pytest.MonkeyPatch,
    artifact: HarnessArtifact,
    source: str,
    task: JsonValue = None,
    replies: tuple[JsonValue, ...] = (),
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    validate_source(source, artifact)
    return interpret(monkeypatch, artifact, source, task, replies)


def value(frame: dict[str, Any]) -> JsonValue:
    assert frame["op"] == "result", frame
    return frame["value"]


def fuel(monkeypatch: pytest.MonkeyPatch, artifact: HarnessArtifact, source: str,
         task: JsonValue = None) -> int:
    frame, _ = run(monkeypatch, artifact, source, task)
    assert frame["op"] == "result", frame
    return int(frame["fuel"])


# ---------------------------------------------------------------- semantics


# Each expression is valid Python and valid sandbox language: the same text runs in both.
EXPRESSIONS = [
    '[x * 2 for x in task["nums"]]',
    '[x for x in task["nums"] if x % 2 == 0]',
    '[x for x in task["nums"] if x > 0 if x < 50]',
    '[[i, x] for i, x in enumerate(task["nums"])]',
    '[a + b for a, b in zip(task["nums"], task["nums"][1:])]',
    '[[y * x for y in task["nums"][:3]] for x in task["nums"][:4]]',
    '[y for row in task["grid"] for y in row]',
    '[y for row in task["grid"] if row for y in row if y != 3]',
    '[[x for x in row if x % 2] for row in task["grid"]]',
    '[[x, y] for x in range(3) for y in range(x)]',
    '[x for x in range(len(task["nums"])) if task["nums"][x] > 5]',
    '[c for c in "".join(task["words"]) if c in "aeiou"]',
    '[r["k"] for r in task["records"] if r.get("ok")]',
    '[str(x) + s for x in task["nums"][:3] for s in task["words"][:2]]',
    '[x for x in task["nums"] if isinstance(x, int)]',
    '[[x for x in x] for x in task["grid"]]',
    '{k: v * 2 for k, v in task["d"].items()}',
    '{k: v for k, v in task["d"].items() if v > 2}',
    '{w: [i for i, x in enumerate(task["nums"]) if x == len(w)] for w in task["words"]}',
    '{w.upper(): len(w) for w in task["words"]}',
    'sum(x for x in task["nums"] if x > 10)',
    'max(len(s) for s in task["words"])',
    'min(x for x in task["nums"])',
    'sorted(x for x in task["words"])',
    'sorted((x for x in task["nums"]), reverse=True)',
    'list(x for x in task["nums"] if x)',
    '", ".join(s.upper() for s in task["words"])',
    'join("-", (s for s in task["words"] if s))',
    'any(x > 40 for x in task["nums"])',
    'all(x >= 0 for x in task["nums"])',
    'any(s.startswith("a") for s in task["words"])',
    'all(len(row) < 4 for row in task["grid"] for _ in row)',
    'any([x > 5 for x in task["nums"]])',
    'all([x for x in task["nums"]])',
    'len([1 for row in task["grid"] for x in row if x])',
]
# Set comprehensions build a list without repeats, in first-seen order.
SET_EXPRESSIONS = [
    '{x % 3 for x in task["nums"]}',
    '{s.lower() for s in task["words"]}',
    '{x for row in task["grid"] for x in row}',
]


def random_task(rng: random.Random) -> dict[str, JsonValue]:
    letters = "abceiou"
    return {
        "nums": [rng.randint(-5, 60) for _ in range(rng.randint(0, 12))],
        "words": [
            "".join(rng.choice(letters) for _ in range(rng.randint(0, 5)))
            for _ in range(rng.randint(0, 6))
        ],
        "grid": [[rng.randint(0, 5) for _ in range(rng.randint(0, 5))]
                 for _ in range(rng.randint(0, 4))],
        "d": {f"k{index}": rng.randint(0, 6) for index in range(rng.randint(0, 5))},
        "records": [{"k": rng.randint(0, 9), "ok": rng.random() < 0.5}
                    for _ in range(rng.randint(0, 4))],
    }


_PYTHON = {
    name: getattr(builtins, name)
    for name in ("len", "sum", "min", "max", "sorted", "any", "all", "list", "range",
                 "enumerate", "zip", "str", "int", "abs", "isinstance")
}


def python(expression: str, task: JsonValue) -> tuple[bool, JsonValue]:
    """CPython's answer (the test's own fixed text, never model text): ok and value."""
    namespace = {"__builtins__": _PYTHON, "join": lambda sep, items: sep.join(items)}
    try:
        answer = eval(expression, namespace, {"task": copy.deepcopy(task)})
    except (ValueError, TypeError, KeyError, IndexError):
        return False, None
    return True, json.loads(json.dumps(answer))


@pytest.mark.parametrize("expression", EXPRESSIONS + SET_EXPRESSIONS)
def test_comprehensions_answer_as_python_does(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact, expression: str
) -> None:
    rng = random.Random(expression)
    source = body(f"return {expression}")
    for _ in range(12):
        task = random_task(rng)
        if expression in SET_EXPRESSIONS:
            # Python's set has no order; the sandbox keeps the first-seen one.
            listed = "[" + expression[1:-1] + "]"
            ok, expected = python(listed, task)
            if ok:
                assert isinstance(expected, list)
                expected = [item for index, item in enumerate(expected)
                            if item not in expected[:index]]
        else:
            ok, expected = python(expression, task)
        frame, _ = run(monkeypatch, v2, source, task)
        if ok:
            assert value(frame) == expected, (expression, task)
        else:
            assert frame["op"] == "error", (expression, task, frame)
            assert frame["code"] in language.DATA_ERROR_CODES, frame


def test_a_set_comprehension_keeps_first_seen_order_and_python_equality(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact
) -> None:
    source = body("return [{x for x in [3, 1, 3, 2, 1]}, {x for x in [1, True, 1.0, 'a', 'a']},"
                  " {x for x in [[1], [1], {'k': 1}, {'k': 1}]}]")
    assert value(run(monkeypatch, v2, source)[0]) == [[3, 1, 2], [1, "a"], [[1], {"k": 1}]]


def test_a_dict_comprehension_reads_the_key_before_the_value(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact
) -> None:
    source = body("return {await context.call(k): await context.call(v)"
                  " for k, v in [['a', 'b'], ['c', 'd']]}")
    frame, sent = run(monkeypatch, v2, source, replies=("K1", "V1", "K2", "V2"))
    assert value(frame) == {"K1": "V1", "K2": "V2"}
    assert [message["prompt"] for message in sent] == ["a", "b", "c", "d"]


def test_a_non_str_dict_key_is_a_catchable_data_error(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact
) -> None:
    caught = body("try:", "    d = {x: 1 for x in [1]}", "except:", "    d = 'caught'", "return d")
    assert value(run(monkeypatch, v2, caught)[0]) == "caught"
    frame, _ = run(monkeypatch, v2, body("x = 1", "return {x: 1 for x in [1]}"))
    assert (frame["code"], frame["line"]) == ("sandbox_dictionary", 3)


def test_capability_calls_in_a_comprehension_run_in_order(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact
) -> None:
    source = body("return [await context.call(p) for p in ['a', 'b', 'c'] if p != 'b']")
    frame, sent = run(monkeypatch, v2, source, replies=("A", "C"))
    assert value(frame) == ["A", "C"]
    assert [message["prompt"] for message in sent] == ["a", "c"]


def test_any_and_all_stop_at_the_first_item_that_decides(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact
) -> None:
    found = body("return any(await context.call(p) == 'yes' for p in ['a', 'b', 'c'])")
    frame, sent = run(monkeypatch, v2, found, replies=("no", "yes", "never"))
    assert value(frame) is True and [m["prompt"] for m in sent] == ["a", "b"]
    failed = body("return all(await context.call(p) == 'yes' for p in ['a', 'b', 'c'])")
    frame, sent = run(monkeypatch, v2, failed, replies=("yes", "no", "never"))
    assert value(frame) is False and [m["prompt"] for m in sent] == ["a", "b"]
    empty = body("return [any(x for x in []), all(x for x in []), any([]), all([])]")
    assert value(run(monkeypatch, v2, empty)[0]) == [False, True, False, True]


# ---------------------------------------------------------------- scope


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        # The loop names are the comprehension's own: the enclosing ones are restored.
        (("x = 'outer'", "ys = [x for x in [1, 2]]", "return [x, ys]"), ["outer", [1, 2]]),
        (("x = 'outer'", "d = {x: 1 for x in ['a']}", "s = {x for x in [3]}",
          "ok = any(x for x in [1])", "return [x, d, s, ok]"), ["outer", {"a": 1}, [3], True]),
        # The first iterable is read in the enclosing scope, as Python reads it.
        (("x = [[1, 2], [3]]", "return [[x for x in x] for x in x]"), [[1, 2], [3]]),
        (("x = [3, 4]", "ys = [x for x in x]", "return [x, ys]"), [[3, 4], [3, 4]]),
        # A comprehension inside a loop restores the loop's own name.
        (("out = []", "for i in range(3):", "    ys = [i for i in range(10, 12)]",
          "    append(out, [i, ys])", "return out"),
         [[0, [10, 11]], [1, [10, 11]], [2, [10, 11]]]),
        # A failed comprehension restores its names before except runs.
        (("x = 'outer'", "try:", "    ys = [int(x) for x in ['1', 'z']]", "except:",
          "    ys = x", "return ys"), "outer"),
        # A comprehension may mutate an enclosing list it reads, as Python's may.
        (("seen = []", "ys = [append(seen, x) for x in [1, 2]]", "return [seen, ys]"),
         [[1, 2], [None, None]]),
    ],
)
def test_comprehension_names_stay_inside_it(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact, lines: tuple[str, ...],
    expected: JsonValue
) -> None:
    assert value(run(monkeypatch, v2, body(*lines))[0]) == expected


def test_a_helper_comprehension_has_its_own_scope(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact
) -> None:
    source = (
        "def evens(values, x=0):\n"
        "    picked = [x for x in values if x % 2 == 0]\n"
        "    return [x, picked]\n"
        + body("x = 'run'", "return [evens([1, 2, 4]), x]")
    )
    assert value(run(monkeypatch, v2, source)[0]) == [[0, [2, 4]], "run"]


@pytest.mark.parametrize(
    ("source", "rule", "line"),
    [
        # Read after the comprehension: no loop name outlives it.
        (body("ys = [x for x in [1]]", "return x"), "unknown_name", 3),
        (body("ys = [y for x in [[1]] for y in x]", "return y"), "unknown_name", 3),
        # A loop name read before its own for clause binds it, as Python would fail it.
        (body("y = 1", "return [y for x in [[1]] if y for y in x]"), "unknown_name", 3),
        (body("return [x for _ in [1] for x in x]"), "unknown_name", 2),
        # Loop names follow the same rules as any other binding.
        (body("return [1 for task in [1]]"), "assign_task", 2),
        ("C = 1\n" + body("return [C for C in [1]]"), "assign_constant", 3),
        ("def f(x):\n    return x\n" + body("return [f for f in [1]]"), "assign_helper", 4),
        (body("return [x for x, (y, z) in task]"), "for_target", 2),
        (body("return [x async for x in task]"), "async_block", 2),
    ],
)
def test_comprehension_scope_refusals(
    v2: HarnessArtifact, source: str, rule: str, line: int
) -> None:
    with pytest.raises(SandboxError) as raised:
        validate_source(source, v2)
    assert (raised.value.rule, raised.value.line) == (rule, line)


def test_the_result_name_cannot_be_spelled_in_source(v2: HarnessArtifact) -> None:
    program = _validated_program(body("return [x for x in task]"), v2)
    returned = program.run.body[0]
    assert isinstance(returned, ast.Return) and isinstance(returned.value, _Comprehension)
    comprehension = returned.value
    assert comprehension.result is not None and not comprehension.result.isidentifier()
    assert set(comprehension.names) == {comprehension.result, "x"}


# ---------------------------------------------------------------- fuel and copies


EQUIVALENT = [
    # (comprehension, the for loop it stands for): the same fuel, exactly.
    (("ys = [x * 2 for x in task]", "return ys"),
     ("out = []", "for x in task:", "    append(out, x * 2)", "ys = out", "return ys")),
    (("ys = [x for x in task if x > 1 if x < 9]", "return ys"),
     ("out = []", "for x in task:", "    if x > 1 and x < 9:", "        append(out, x)",
      "ys = out", "return ys")),
    (("ys = [x for x in task if x % 2]", "return ys"),
     ("out = []", "for x in task:", "    if x % 2:", "        append(out, x)", "ys = out",
      "return ys")),
    (("g = [[x, x] for x in task]", "ys = [y for row in g for y in row]", "return ys"),
     ("g = [[x, x] for x in task]", "out = []", "for row in g:", "    for y in row:",
      "        append(out, y)", "ys = out", "return ys")),
    (("ys = {str(i): v for i, v in enumerate(task)}", "return ys"),
     ("out = {}", "for i, v in enumerate(task):", "    set_key(out, str(i), v)", "ys = out",
      "return ys")),
    (("ys = {x % 3 for x in task}", "return ys"),
     ("out = []", "for x in task:", "    v = x % 3", "    if v not in out:",
      "        append(out, v)", "ys = out", "return ys")),
    # Containers are copied into the result one fuel per element, as append copies them.
    (("g = [[x, x, x] for x in task]", "ys = [row for row in g]", "return ys"),
     ("g = [[x, x, x] for x in task]", "out = []", "for row in g:", "    append(out, row)",
      "ys = out", "return ys")),
    (("ys = [[y for y in range(x % 7)] for x in task]", "return ys"),
     ("out = []", "for x in task:", "    inner = []", "    for y in range(x % 7):",
      "        append(inner, y)", "    append(out, inner)", "ys = out", "return ys")),
]


@pytest.mark.parametrize(("comprehension", "loop"), EQUIVALENT)
def test_a_comprehension_costs_exactly_its_for_loop(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact,
    comprehension: tuple[str, ...], loop: tuple[str, ...]
) -> None:
    for task in ([], [1], [3, 1, 4, 1, 5, 9, 2, 6], list(range(300))):
        short, long = body(*comprehension), body(*loop)
        assert value(run(monkeypatch, v2, short, task)[0]) == value(
            run(monkeypatch, v2, long, task)[0]
        )
        assert fuel(monkeypatch, v2, short, task) == fuel(monkeypatch, v2, long, task), task


def test_a_generator_argument_costs_its_list_comprehension(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact
) -> None:
    task = list(range(-50, 250))
    for call in ("sum({})", "min({})", "max({})", "sorted({})", "list({})",
                 "sorted({}, reverse=True)", "join(',', {})"):
        item = "str(x)" if call.startswith("join") else "x"
        generator = body(f"return {call.format(f'({item} for x in task if x)')}")
        listed = body(f"return {call.format(f'[{item} for x in task if x]')}")
        assert fuel(monkeypatch, v2, generator, task) == fuel(monkeypatch, v2, listed, task)


def test_any_and_all_pay_only_up_to_the_deciding_item(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact
) -> None:
    for source, head in ((body("return any(x > 2 for x in task)"), [5]),
                         (body("return all(x > 2 for x in task)"), [0])):
        short = fuel(monkeypatch, v2, source, head)
        long = fuel(monkeypatch, v2, source, head + [1] * 1000)
        # Only the snapshot of the longer list (1 fuel per 256 items) differs.
        assert long - short == (1001 >> 8) - (1 >> 8)
    # Undecided until the end: every item is paid for.
    undecided = fuel(monkeypatch, v2, body("return any(x > 2 for x in task)"), [0] * 100)
    assert undecided > fuel(monkeypatch, v2, body("return any(x > 2 for x in task)"), [0]) + 400


def test_a_large_comprehension_ends_in_fuel_at_its_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = make_artifact(tmp_path / "a", forge=binding(fuel=200_000))
    for source in (
        body("x = 1", "return [y for y in range(100000)]"),
        body("x = 1", "return [[y for y in range(1000)] for z in range(1000)]"),
        body("x = 1", "return {str(y): y for y in range(100000)}"),
        body("x = 1", "return {y for y in range(100000)}"),
        body("x = 1", "return sum(y for y in range(100000))"),
        body("x = 1", "return any(y < 0 for y in range(100000))"),
        body("x = 1", "return [[a, b] for a in range(1000) for b in range(1000)]"),
    ):
        started = time.perf_counter()
        frame, _ = run(monkeypatch, artifact, source)
        assert (frame["code"], frame["line"]) == ("sandbox_fuel", 3), source
        assert time.perf_counter() - started < wall_limit(5)


def test_a_comprehension_cannot_build_past_the_value_limits(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact
) -> None:
    # 65,536 entries is the most any list or dict holds.
    for source in (
        body("return [y for y in range(70000)]"),
        body("return {str(y): y for y in range(70000)}"),
        body("big = list(range(40000))", "return [y for y in big for _ in [1, 2]]"),
    ):
        frame, _ = run(monkeypatch, v2, source)
        assert frame["code"] == "sandbox_value_limit", source
    # Nesting deeper than 40 fails when the copy is made, not later.
    nested = body("x = []", "for i in range(45):", "    x = [x for _ in range(1)]", "return x")
    frame, _ = run(monkeypatch, v2, nested)
    assert (frame["code"], frame["line"]) == ("sandbox_value_limit", 4)


async def test_a_large_comprehension_is_bounded_through_the_worker(tmp_path: Path) -> None:
    source = body("x = 1", "return [[b for b in range(60000)] for a in range(60000)]")
    artifact = make_artifact(tmp_path / "a", source, forge=binding(fuel=1_000_000))
    context = RunContext(FakeAdapter(), Budget(cost_usd=1, compute_s=60))
    started = time.perf_counter()
    with pytest.raises(SandboxError, match="^sandbox_fuel$") as caught:
        await sandbox_harness(artifact)(context, None)
    assert caught.value.line == 3
    assert time.perf_counter() - started < wall_limit(10)


def test_comprehension_loops_count_toward_the_loop_depth(v2: HarnessArtifact) -> None:
    six = body("return [1 for a in [1] for b in [1] for c in [1] for d in [1]"
               " for e in [1] for f in [1]]")
    validate_source(six, v2)
    with pytest.raises(SandboxError) as raised:
        validate_source(body("for a in [1]:", "    x = [1 for b in [1] for c in [1]"
                             " for d in [1] for e in [1] for f in [1] for g in [1]]"), v2)
    assert (raised.value.rule, raised.value.line) == ("loop_depth", 3)


# ---------------------------------------------------------------- random programs


def _random_comprehension(rng: random.Random, enclosing: list[str], depth: int, kind: str,
                          scalar_only: bool = False) -> str:
    """A random nested comprehension that CPython runs: names a later clause binds are
    never read early, and every loop name holds an int (no list arithmetic)."""

    def scalar(visible: list[str]) -> str:
        pool = visible + ["1", "2", "len(task)"]
        op = rng.choice(["+", "-", "*", "%"])
        right = str(rng.randint(1, 5)) if op == "%" else rng.choice(pool)
        return f"({rng.choice(pool)} {op} {right})"

    def ints(visible: list[str]) -> str:
        if depth < 2 and rng.random() < 0.35:
            return _random_comprehension(rng, visible, depth + 1, "list", scalar_only=True)
        return rng.choice(["task", "[1, 2, 3]", "range(4)", "range(len(task))"])

    targets = [rng.sample(["a", "b", "x", "y"], 2) if rng.random() < 0.3
               else [rng.choice(["a", "b", "x", "y"])] for _ in range(rng.randint(1, 2))]
    own = {name for names in targets for name in names}
    base = [name for name in enclosing if name not in own]
    bound: list[str] = []
    clauses = []
    for index, names in enumerate(targets):
        source = ints(enclosing if index == 0 else base + bound)
        clause = (f"for {names[0]}, {names[1]} in enumerate(list({source}))" if len(names) == 2
                  else f"for {names[0]} in {source}")
        bound += names
        tests = [f"{scalar(base + bound)} {rng.choice(['>', '<', '==', '!='])} "
                 f"{rng.randint(-2, 6)}" for _ in range(rng.randint(0, 2))]
        clauses.append(" ".join([clause, *(f"if {test}" for test in tests)]))
    visible = base + bound
    element = scalar(visible)
    if not scalar_only and kind in ("list", "dict", "any", "all") and depth < 2 \
            and rng.random() < 0.25:
        element = _random_comprehension(rng, visible, depth + 1, "list", scalar_only=True)
    joined = " ".join(clauses)
    if kind == "list":
        return f"[{element} {joined}]"
    if kind == "set":
        return "{" + f"{scalar(visible)} {joined}" + "}"
    if kind == "dict":
        return "{" + f"str({scalar(visible)}): {element} {joined}" + "}"
    if kind in ("sum", "sorted", "min", "max"):
        element = scalar(visible)
    return f"{'list' if kind == 'list()' else kind}({element} {joined})"


def test_random_nested_comprehensions_answer_as_python_does(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact
) -> None:
    rng = random.Random(20261004)
    for _ in range(300):
        kind = rng.choice(["list", "dict", "set", "any", "all", "sum", "sorted", "min", "max",
                           "list()"])
        outer = rng.random() < 0.5
        expression = _random_comprehension(rng, ["x"] if outer else [], 0, kind)
        source = body(*(["x = 7"] if outer else []), f"r = {expression}",
                      "return [r, x]" if outer else "return [r]")
        task: JsonValue = [rng.randint(-3, 6) for _ in range(rng.randint(0, 5))]
        scope = {"__builtins__": _PYTHON, "task": copy.deepcopy(task), **({"x": 7} if outer
                                                                          else {})}
        try:
            answer = eval(expression, scope)
            if kind == "set":
                items = eval("[" + expression[1:-1] + "]", scope)
                answer = [item for index, item in enumerate(items) if item not in items[:index]]
            expected: JsonValue = json.loads(json.dumps([answer, 7] if outer else [answer]))
            ok = True
        except (ValueError, TypeError, ZeroDivisionError):
            ok = False
        frame, _ = run(monkeypatch, v2, source, task)
        if ok:
            assert value(frame) == expected, (expression, task)
        else:
            assert frame["op"] == "error", (expression, task, frame)
            assert frame["code"] in language.DATA_ERROR_CODES, frame
