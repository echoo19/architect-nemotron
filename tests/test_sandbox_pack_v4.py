"""Forge pack v4 additions to sandbox language v2, all of them accept-only: typed and named
excepts (still catching data errors only), three loop names for zip of three lists, and
the string builtins (the character tests, chr, ord, split on whitespace, strip with
characters, lstrip, rstrip, rfind, copy, min and max of two values, zip of three lists).

Each answers as CPython does wherever both define the answer, and is metered exactly as
``_sandbox_builtins`` documents it."""

from __future__ import annotations

import copy
import json
import random
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue
from sandbox_v2_support import binding, body, interpret, make_artifact

from architect.runtime import sandbox_language as language
from architect.runtime.sandbox import SandboxError, validate_source
from architect.spec import HarnessArtifact


@pytest.fixture
def v2(tmp_path: Path) -> HarnessArtifact:
    return make_artifact(tmp_path / "v2")


def run(
    monkeypatch: pytest.MonkeyPatch, artifact: HarnessArtifact, source: str,
    task: JsonValue = None,
) -> dict[str, Any]:
    validate_source(source, artifact)
    return interpret(monkeypatch, artifact, source, task)[0]


def value(frame: dict[str, Any]) -> JsonValue:
    assert frame["op"] == "result", frame
    return frame["value"]


def fuel(monkeypatch: pytest.MonkeyPatch, artifact: HarnessArtifact, source: str,
         task: JsonValue = None) -> int:
    frame = run(monkeypatch, artifact, source, task)
    assert frame["op"] == "result", frame
    return int(frame["fuel"])


def work(units: int) -> int:
    """A builtin's floor charge: 1 plus 1 per 256 units of work."""
    return 1 + (units + language.FUEL_UNIT - 1) // language.FUEL_UNIT


def walk(items: int) -> int:
    """A native loop's charge: 1 per 16 things it visits."""
    return items >> 4


# ---------------------------------------------------------------- answers, as CPython's

# Letters of both cases and none, digits that are and are not decimal, whitespace of
# three kinds, punctuation and characters past the BMP.
ALPHABET = "aZ1 \t\n.,_-²٣Σσǅ　\U0001d7d8\U0001f600"

# Each expression is valid Python and valid sandbox language: the same text runs in both,
# over s, c (some characters) and sub, all strs.
STRING_EXPRESSIONS = [
    "[s.isdigit(), s.isalpha(), s.isalnum(), s.isspace(), s.isupper(), s.islower()]",
    "[isdigit(s), isalpha(s), isalnum(s), isspace(s), isupper(s), islower(s)]",
    "[[x.isdigit(), x.isalpha(), x.isspace(), x.isupper(), x.islower()] for x in s]",
    "[s.split(), split(s), s.split(' ')]",
    "[s.strip(), s.lstrip(), s.rstrip(), strip(s), lstrip(s), rstrip(s)]",
    "[s.strip(c), s.lstrip(c), s.rstrip(c), strip(s, c), lstrip(s, c), rstrip(s, c)]",
    "[s.strip(None), s.lstrip(None), s.rstrip(None)]",
    "[s.rfind(sub), rfind(s, sub), s.rfind(''), s.find(sub)]",
    "[ord(x) for x in s]",
    "''.join([chr(ord(x)) for x in s])",
    "[x for x in s.split() if x.isdigit() or x.isalpha()]",
]


def _python_namespace() -> dict[str, Any]:
    """The function spellings, as the str methods they are (the test's own fixed text)."""
    names: dict[str, Any] = {
        name: getattr(str, name)
        for name in ("isdigit", "isalpha", "isalnum", "isspace", "isupper", "islower", "rfind")
    }
    names.update(
        split=lambda s, sep=None: s.split(sep),
        strip=lambda s, chars=None: s.strip(chars),
        lstrip=lambda s, chars=None: s.lstrip(chars),
        rstrip=lambda s, chars=None: s.rstrip(chars),
        ord=ord, chr=chr, max=max, min=min, zip=zip, list=list, len=len, str=str,
        enumerate=enumerate,
    )
    return {"__builtins__": names}


def python(expression: str, task: dict[str, JsonValue]) -> tuple[bool, JsonValue]:
    """CPython's answer: ok and value (as JSON), or not ok when it raises a data error."""
    try:
        answer = eval(expression, _python_namespace(), copy.deepcopy(task))
    except (ValueError, TypeError, KeyError, IndexError):
        return False, None
    return True, json.loads(json.dumps(answer))


def random_text(rng: random.Random, longest: int) -> str:
    return "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, longest)))


def sandbox(expression: str, names: tuple[str, ...]) -> str:
    return body(*[f"{name} = task[{name!r}]" for name in names], f"return {expression}")


def check(monkeypatch: pytest.MonkeyPatch, artifact: HarnessArtifact, expression: str,
          task: dict[str, JsonValue]) -> None:
    ok, expected = python(expression, task)
    frame = run(monkeypatch, artifact, sandbox(expression, tuple(task)), task)
    if ok:
        assert value(frame) == expected, (expression, task)
    else:
        assert frame["op"] == "error", (expression, task, frame)
        assert frame["code"] in language.DATA_ERROR_CODES, frame


@pytest.mark.parametrize("expression", STRING_EXPRESSIONS)
def test_the_string_builtins_answer_as_python_does(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact, expression: str
) -> None:
    rng = random.Random(expression)
    for _ in range(40):
        text = random_text(rng, 12)
        start = rng.randint(0, len(text))
        sub = text[start : start + rng.randint(0, 3)] if rng.random() < 0.6 else random_text(
            rng, 2
        )
        task: dict[str, JsonValue] = {"s": text, "c": random_text(rng, 3), "sub": sub}
        check(monkeypatch, v2, expression, task)


@pytest.mark.parametrize(
    ("number", "expected"),
    [(0, "\x00"), (65, "A"), (0x3A3, "Σ"), (0x1F600, "\U0001f600"), (0x10FFFF, "\U0010ffff"),
     (-1, None), (0x110000, None), (10**40, None), (-(10**40), None)],
)
def test_chr_takes_the_code_points_python_takes(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact, number: int, expected: str | None
) -> None:
    frame = run(monkeypatch, v2, body("return chr(task)"), number)
    if expected is None:
        assert (frame["code"], frame["line"]) == ("sandbox_value", 2)
    else:
        assert value(frame) == expected


@pytest.mark.parametrize("task", ["", "ab", 1, None, ["a"], True])
def test_ord_refuses_anything_but_one_character_as_a_data_error(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact, task: JsonValue
) -> None:
    frame = run(monkeypatch, v2, body("return ord(task)"), task)
    assert (frame["code"], frame["line"]) == ("sandbox_type", 2)
    caught = body("try:", "    x = ord(task)", "except TypeError as e:", "    x = e", "return x")
    assert value(run(monkeypatch, v2, caught, task)) == "sandbox_type"
    if not isinstance(task, int) or task is True:  # chr takes an int (never a bool)
        frame = run(monkeypatch, v2, body("return chr(task)"), task)
        assert (frame["code"], frame["line"]) == ("sandbox_type", 2)


def test_the_string_builtins_refuse_other_types_as_data_errors(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact
) -> None:
    for expression, task in (
        ("s.isdigit()", 5), ("s.strip(1)", "a"), ("lstrip(s, ['a'])", "a"), ("s.rfind(1)", "a"),
        ("rfind(1, s)", "a"), ("split(s)", 1), ("rstrip(None)", "a"),
        # split(s, None) was accepted before pack v4 and still gives its data error.
        ("s.split(None)", "a b"),
    ):
        frame = run(monkeypatch, v2, body("s = task", f"return {expression}"), task)
        assert (frame["op"], frame["code"], frame["line"]) == ("error", "sandbox_type", 3), (
            expression
        )


VALUES: list[JsonValue] = [0, 1, -2, 2.5, -0.0, 7, "", "a", "ab", "b", "Σ", [1], {"k": 1},
                           None, True]


def test_min_and_max_of_two_values_answer_as_the_list_form(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact
) -> None:
    for first in VALUES:
        for second in VALUES:
            task = {"a": first, "b": second}
            pair = sandbox("[max(a, b), min(a, b)]", ("a", "b"))
            listed = sandbox("[max([a, b]), min([a, b])]", ("a", "b"))
            two, one = run(monkeypatch, v2, pair, task), run(monkeypatch, v2, listed, task)
            assert {key: two[key] for key in ("op", "value", "code") if key in two} == {
                key: one[key] for key in ("op", "value", "code") if key in one
            }, task
            ok, expected = python("[max(a, b), min(a, b)]", task)
            if two["op"] == "result":
                assert ok and value(two) == expected, task
            # Where they differ, the sandbox refuses (a bool is not a number here, and lists
            # do not order): it never answers what Python would not.


def test_zip_of_three_lists_and_three_loop_names_answer_as_python_does(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact
) -> None:
    rng = random.Random(4)
    expressions = (
        "[list(t) for t in zip(x, y, z)]",
        "[a + b + c for a, b, c in zip(x, y, z)]",
        "{str(a): [b, c] for a, b, c in zip(x, y, z)}",
        "[list(t) for t in zip(x, y)]",
        "[[i, a] for i, a in enumerate(x)]",
    )
    for _ in range(30):
        task: dict[str, JsonValue] = {
            name: [rng.randint(-5, 9) for _ in range(rng.randint(0, 5))] for name in "xyz"
        }
        for expression in expressions:
            check(monkeypatch, v2, expression, task)
    loop = body(
        "out = []", "for a, b, c in task:", "    append(out, [c, b, a])", "return out"
    )
    assert value(run(monkeypatch, v2, loop, [[1, 2, 3], ["x", [4], None]])) == [
        [3, 2, 1], [None, [4], "x"]
    ]
    # A row of another length is a data error, as Python's ValueError is; a str or dict
    # row is one too, as it is for two names (only a list unpacks).
    for rows in ([[1, 2]], [[1, 2, 3, 4]], ["abc"], [{"a": 1, "b": 2, "c": 3}]):
        frame = run(monkeypatch, v2, loop, rows)
        assert (frame["code"], frame["line"]) == ("sandbox_value", 3), rows
    caught = body("try:", "    for a, b, c in task:", "        pass", "except ValueError:",
                  "    return 'caught'", "return 'ran'")
    assert value(run(monkeypatch, v2, caught, [[1, 2]])) == "caught"


def test_copy_copies_the_whole_value(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact
) -> None:
    source = body(
        "l = [[1, 2], {'k': [3]}]", "d = {'a': [1], 'b': {'c': 2}}",
        "l2 = l.copy()", "d2 = d.copy()", "l3 = copy(l)",
        "inner = l2[0]", "append(inner, 9)", "deep = d2['b']", "deep['c'] = 5",
        "append(l3, 0)", "d2['z'] = 1",
        "return [l, d, l2, d2, l3, l == l.copy(), d == copy(d)]",
    )
    # Value semantics: a copy shares nothing with its source, at any depth.
    assert value(run(monkeypatch, v2, source)) == [
        [[1, 2], {"k": [3]}], {"a": [1], "b": {"c": 2}},
        [[1, 2, 9], {"k": [3]}], {"a": [1], "b": {"c": 5}, "z": 1},
        [[1, 2], {"k": [3]}, 0], True, True,
    ]
    for task in ("ab", 1, None, True):
        frame = run(monkeypatch, v2, body("return task.copy()"), task)
        assert (frame["code"], frame["line"]) == ("sandbox_type", 2), task


def test_the_new_builtins_stay_within_the_value_limits(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact
) -> None:
    # zip of three lists copies what it takes one level down, as zip of two does, so a
    # value already 39 deep cannot land in a row: the copy fails as it is made.
    nested = ("x = []", "for i in range(38):", "    x = [x]")
    for call in ("zip([x], [1], [2])", "zip([x], [1])"):
        frame = run(monkeypatch, v2, body(*nested, f"return {call}"))
        assert (frame["code"], frame["line"]) == ("sandbox_value_limit", 5), call
    # A copy lands where its value was: a value 40 deep, the most there is, copies whole.
    deepest = body("x = []", "for i in range(39):", "    x = [x]", "y = x.copy()",
                   "return [len(y), y == x]")
    assert value(run(monkeypatch, v2, deepest)) == [1, True]
    # split on whitespace makes at most 65,536 parts from the longest str there is.
    longest = body("s = 'a '", "while len(s) < 131072:", "    s = s + s",
                   "return [len(s), len(s.split()), len(s.rstrip()), len(lstrip(s, 'a'))]")
    assert value(run(monkeypatch, v2, longest)) == [131072, 65536, 131071, 131071]


# ---------------------------------------------------------------- typed and named excepts


@pytest.mark.parametrize(
    ("raises", "code"),
    [
        ("x = {}['k']", "sandbox_key"),
        ("x = [][1]", "sandbox_index"),
        ("x = int('z')", "sandbox_value"),
        ("x = 1 // 0", "sandbox_value"),
        ("x = len(1)", "sandbox_type"),
        ("x = 'a' + 1", "sandbox_add"),
        ("x = {k: 1 for k in [1]}", "sandbox_dictionary"),
    ],
)
def test_any_typed_or_named_except_catches_every_data_error(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact, raises: str, code: str
) -> None:
    for clause in ("except:", "except Exception:", "except KeyError:", "except ZeroDivisionError:",
                   "except (ValueError, TypeError):", "except (LookupError,):"):
        source = body("try:", f"    {raises}", clause, "    x = 'caught'", "return x")
        assert value(run(monkeypatch, v2, source)) == "caught", clause
    named = body("try:", f"    {raises}", "except (ValueError, KeyError) as error:",
                 "    x = [error, kind(error)]", "return x")
    assert value(run(monkeypatch, v2, named)) == [code, "str"]


def test_the_name_lives_only_while_the_handler_runs(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact
) -> None:
    # As in Python: unbound after the handler, even if it held a value before the try ...
    after = body("e = 'before'", "try:", "    x = int('z')", "except ValueError as e:",
                 "    pass", "return e")
    frame = run(monkeypatch, v2, after)
    assert (frame["code"], frame["line"]) == ("sandbox_unbound", 7)
    # ... but untouched when the handler never ran.
    kept = body("e = 'before'", "try:", "    x = 1", "except ValueError as e:", "    pass",
                "return e")
    assert value(run(monkeypatch, v2, kept)) == "before"
    # However the handler ends: return, break, or a data error caught further out.
    returned = body("try:", "    x = int('z')", "except Exception as e:", "    return [e]")
    assert value(run(monkeypatch, v2, returned)) == ["sandbox_value"]
    looped = body(
        "out = []", "for s in ['1', 'z', '3']:", "    try:", "        append(out, int(s))",
        "    except ValueError as e:", "        append(out, e)", "        break",
        "return [out, get({}, 'k', 'no e here')]",
    )
    assert value(run(monkeypatch, v2, looped)) == [[1, "sandbox_value"], "no e here"]
    nested = body(
        "try:", "    try:", "        x = int('z')", "    except ValueError as e:",
        "        y = e + []", "except TypeError as f:", "    return [f]",
    )
    assert value(run(monkeypatch, v2, nested)) == ["sandbox_add"]


def test_fatal_errors_are_never_caught_by_a_typed_or_named_except(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = make_artifact(tmp_path / "a", forge=binding(fuel=200_000))
    for inner in (("while True:", "    pass"), ("l = []", "while True:", "    append(l, 0)"),
                  ("x = y",)):
        for clause in ("except Exception as e:", "except (ValueError, TypeError):"):
            source = body("try:", *["    " + line for line in inner], clause,
                          "    return 'caught'", "if task:", "    y = 1")
            frame = run(monkeypatch, artifact, source)
            assert frame["op"] == "error" and frame["code"] in {
                "sandbox_fuel", "sandbox_value_limit", "sandbox_unbound"
            }, (inner, clause)


def test_excepts_answer_as_python_does(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact
) -> None:
    lines = (
        "out = []",
        "for x in task:",
        "    try:",
        "        n = int(x)",
        "    except (ValueError, TypeError) as error:",
        "        n = -1",
        "    try:",
        "        r = 10 // n",
        "    except ZeroDivisionError:",
        "        r = None",
        "    append(out, [n, r])",
        "return out",
    )
    task: JsonValue = ["1", "a", "0", "-5", "x1"]
    namespace: dict[str, Any] = {"int": int, "ZeroDivisionError": ZeroDivisionError,
                                 "ValueError": ValueError, "TypeError": TypeError}
    scope: dict[str, Any] = {}
    exec(  # the test's own fixed text, run as Python for the expected answer
        "def f(task, append):\n" + "".join(f"    {line}\n" for line in lines),
        {"__builtins__": namespace}, scope,
    )
    expected = scope["f"](copy.deepcopy(task), list.append)
    assert value(run(monkeypatch, v2, body(*lines), task)) == expected


# ---------------------------------------------------------------- exact fuel


def charged(monkeypatch: pytest.MonkeyPatch, artifact: HarnessArtifact, expression: str,
            task: dict[str, JsonValue], nodes: int) -> int:
    """The fuel ``y = <expression>`` costs beyond ``y = s``, less its ``nodes`` evaluated
    syntax nodes beyond that one name: what the builtin itself charged."""
    names = tuple(task)
    setup = [f"{name} = task[{name!r}]" for name in names]
    with_call = body(*setup, f"y = {expression}", "return 1")
    without = body(*setup, "y = s", "return 1")
    return fuel(monkeypatch, artifact, with_call, task) - fuel(
        monkeypatch, artifact, without, task
    ) - nodes


TEXTS = ["", "a", "1234", " ab  cd\t", "　Σx ", "a b " * 700, "σ " * 900,
         "x" * 5000, "\U0001f600" * 600]


@pytest.mark.parametrize("text", TEXTS, ids=range(len(TEXTS)))
def test_the_character_tests_charge_work_and_a_native_walk(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact, text: str
) -> None:
    n = len(text)
    for name in ("isdigit", "isalpha", "isalnum", "isspace", "isupper", "islower"):
        assert charged(monkeypatch, v2, f"s.{name}()", {"s": text}, 1) == work(n) + walk(n)
        assert charged(monkeypatch, v2, f"{name}(s)", {"s": text}, 1) == work(n) + walk(n)


@pytest.mark.parametrize("text", TEXTS, ids=range(len(TEXTS)))
def test_split_and_strip_charge_as_documented(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact, text: str
) -> None:
    n = len(text)
    parts = len(text.split())
    assert charged(monkeypatch, v2, "s.split()", {"s": text}, 1) == (
        work(n) + walk(n) + walk(parts)
    )
    # The whitespace forms cost what strip(s) always cost, with or without None.
    plain = work(n) + (0 if text.isascii() else walk(n))
    for expression, nodes in (("s.strip()", 1), ("s.lstrip()", 1), ("rstrip(s)", 1),
                              ("strip(s, None)", 2), ("s.rstrip(None)", 2)):
        assert charged(monkeypatch, v2, expression, {"s": text}, nodes) == plain, expression
    for chars in ("", "a", " \t", "xyzσ", "\U0001f600" * 40):
        m = len(chars)
        expected = work(n) + walk(n + m) + ((n * m) >> 10)
        for expression in ("s.strip(c)", "lstrip(s, c)", "s.rstrip(c)"):
            got = charged(monkeypatch, v2, expression, {"s": text, "c": chars}, 2)
            assert got == expected, (expression, chars)


def test_rfind_is_charged_its_whole_reverse_search(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact
) -> None:
    for text, sub in (("", ""), ("abc", ""), ("abc", "c"), ("abcabc", "bc"), ("a" * 3000, "ab"),
                      ("a" * 3000, "a" + "b" + "a" * 998), ("ab", "abc"), ("x" * 40, "x" * 40)):
        n, m = len(text), len(sub)
        search = ((n - m + 1) * m) >> 10 if 2 <= m <= n else 0
        assert charged(monkeypatch, v2, "s.rfind(sub)", {"s": text, "sub": sub}, 2) == (
            work(n) + search
        ), (n, m)


def test_chr_ord_and_copy_charge_as_documented(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact
) -> None:
    assert charged(monkeypatch, v2, "chr(n)", {"s": "", "n": 65}, 1) == work(1)
    assert charged(monkeypatch, v2, "ord(s)", {"s": "A"}, 1) == work(1)
    nested: JsonValue = [[1, [2, 3]], {"k": [4, 5, 6]}, "x"]
    elements = 3 + 2 + 2 + 1 + 3  # every container's entries, the root's included
    assert charged(monkeypatch, v2, "s.copy()", {"s": nested}, 1) == work(3) + elements
    mapping: JsonValue = {str(i): [i] for i in range(300)}
    assert charged(monkeypatch, v2, "copy(s)", {"s": mapping}, 1) == work(300) + 300 + 300


def test_copy_of_a_list_costs_exactly_list_of_it(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact
) -> None:
    for task in ([], [1, 2], json.loads(json.dumps([[1, [2]], {"k": [3, 4]}] * 50)),
                 list(range(1000))):
        copied = body("l = task", "y = l.copy()", "return y")
        listed = body("l = task", "y = list(l)", "return y")
        assert fuel(monkeypatch, v2, copied, task) == fuel(monkeypatch, v2, listed, task)


@pytest.mark.parametrize(
    "pair",
    [[1, 2], [2.5, -1], [-3, -3.0], ["a", "b"], ["b", "a"], ["x" * 3000, "x" * 2999 + "y"],
     ["\U0001f600" * 900, "\U0001f600" * 900]],
)
def test_min_and_max_of_two_values_cost_exactly_the_list_form(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact, pair: list[JsonValue]
) -> None:
    for name in ("max", "min"):
        two = body("a = task[0]", "b = task[1]", f"return {name}(a, b)")
        listed = body("a = task[0]", "b = task[1]", f"return {name}([a, b])")
        assert fuel(monkeypatch, v2, two, pair) == fuel(monkeypatch, v2, listed, pair)


def test_zip_charges_each_item_it_takes_from_each_list(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact
) -> None:
    for sizes in ((0, 3, 3), (5, 5, 5), (300, 400, 500), (7, 2, 9)):
        lists = {name: list(range(size)) for name, size in zip("sxy", sizes, strict=True)}
        shortest = min(sizes)
        assert charged(monkeypatch, v2, "zip(s, x, y)", lists, 3) == work(shortest) + 3 * shortest
        assert charged(monkeypatch, v2, "zip(s, x)", lists, 2) == work(min(sizes[:2])) + 2 * min(
            sizes[:2]
        )
    # What it takes is copied, one fuel per copied element, as with two lists.
    rows: dict[str, JsonValue] = {
        "s": [[1, 2] for _ in range(4)], "x": [{"k": 1} for _ in range(4)], "y": [3] * 4
    }
    assert charged(monkeypatch, v2, "zip(s, x, y)", rows, 3) == work(4) + 12 + 4 * 2 + 4 * 1


def test_three_loop_names_cost_what_two_do(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact
) -> None:
    for count in (0, 1, 7, 300):
        three = body("t = 0", "for a, b, c in task[0]:", "    t += a", "return t")
        two = body("t = 0", "for a, b in task[1]:", "    t += a", "return t")
        task: JsonValue = [[[i, i, i] for i in range(count)], [[i, i] for i in range(count)]]
        assert fuel(monkeypatch, v2, three, task) == fuel(monkeypatch, v2, two, task)
        comprehension = body("return [a + b + c for a, b, c in task[0]]")
        loop = body("out = []", "for a, b, c in task[0]:", "    append(out, a + b + c)",
                    "return out")
        assert fuel(monkeypatch, v2, comprehension, task) == fuel(monkeypatch, v2, loop, task)


def test_a_typed_or_named_except_costs_what_a_bare_one_costs(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact
) -> None:
    for raises in ("x = int('z')", "x = 1"):
        costs = {
            clause: fuel(monkeypatch, v2, body(
                "try:", f"    {raises}", clause, "    x = 0", "return x"
            ))
            for clause in ("except:", "except Exception:", "except ValueError as e:",
                           "except (KeyError, ValueError, TypeError) as error:")
        }
        assert len(set(costs.values())) == 1, costs


# ---------------------------------------------------------------- still refused


@pytest.mark.parametrize(
    ("source", "rule", "line"),
    [
        (body("try:", "    pass", "except BaseException:", "    pass"), "except_shape", 4),
        (body("try:", "    pass", "except ValueError:", "    pass", "except KeyError:",
              "    pass"), "try_shape", 2),
        (body("try:", "    pass", "except ():", "    pass"), "except_shape", 4),
        (body("try:", "    pass", "except task:", "    pass"), "except_shape", 4),
        (body("try:", "    pass", "except ValueError as context:", "    pass"),
         "assign_context", 4),
        (body("try:", "    pass", "except ValueError as __class__:", "    pass"),
         "assign_name", 4),
        ("C = 1\n" + body("try:", "    pass", "except ValueError as C:", "    pass"),
         "assign_constant", 5),
        (body("for a, b, c, d in task:", "    pass"), "for_target", 2),
        (body("return [a for a, (b, c) in task]"), "for_target", 2),
        (body("return zip(task, task, task, task)"), "builtin_arity", 2),
        (body("return max(1, 2, 3)"), "builtin_arity", 2),
        (body("return task.split(',', 1)"), "method_arity", 2),
        (body("return task.strip('a', 'b')"), "method_arity", 2),
        (body("return task.rfind('a', 1)"), "method_arity", 2),
        (body("return chr(1, 2)"), "builtin_arity", 2),
        (body("return task.isnumeric()"), "method_unknown", 2),
        (body("return task.isdigit"), "attribute", 2),
        (body("return max(task, key=len)"), "builtin_keyword", 2),
        (body("x = 's'", "x.isdigit()"), "expression_statement", 3),
    ],
)
def test_pack_v4_refusals(v2: HarnessArtifact, source: str, rule: str, line: int) -> None:
    with pytest.raises(SandboxError) as raised:
        validate_source(source, v2)
    assert (raised.value.rule, raised.value.line) == (rule, line)


def test_the_new_builtins_cannot_be_helper_names(v2: HarnessArtifact) -> None:
    for name in ("chr", "ord", "copy", "rfind", "lstrip", "rstrip", "isdigit", "islower"):
        with pytest.raises(SandboxError) as raised:
            validate_source(f"def {name}(x):\n    return x\n" + body("return 1"), v2)
        assert (raised.value.rule, raised.value.name) == ("helper_builtin_name", name)
        # A variable may still take the name, and a call by it still reaches the builtin.
        source = body(f"{name} = 'A'", f"return [{name}, chr(66), ord({name})]")
        validate_source(source, v2)


def test_the_card_and_the_tables_agree() -> None:
    card = language.language_markdown()
    for form in ("min(list) or min(a, b)", "zip(l1, l2[, l3])", "split(s[, sep])",
                 "strip(s[, chars])", "rfind(s, sub)", "isdigit(s)", "chr(i)", "ord(c)",
                 "copy(x)", "for a, b, c in zip(l1, l2, l3):",
                 "except (KeyError, TypeError) as e:"):
        assert form in card, form
    assert "chr and ord (compare characters)" not in card and "s.isdigit()," in card
    assert not set(language.ALTERNATIVES) & (set(language.BUILTINS) | set(language.METHODS))
    assert {"Exception", "ValueError", "TypeError", "KeyError"} <= language.EXCEPTION_NAMES
    message = language.refusal_message("sandbox_statement", "except_shape", None, 4)
    assert message.startswith("Line 4: Write except:") and "ZeroDivisionError" in message
