"""Sandbox language v2: validator matrix, frozen v1, semantics, builtins and capabilities."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import JsonValue
from sandbox_v2_support import (
    TurnContext,
    binding,
    body,
    game_artifact,
    game_spec,
    interpret,
    make_artifact,
    text_spec,
    wall_limit,
)

from architect.models import FakeAdapter, ModelRequest, ModelResponse
from architect.models.images import ImageInput, encode_png_gray
from architect.presets import all_presets, get_preset
from architect.runtime import RunContext
from architect.runtime import _sandbox_builtins as builtins
from architect.runtime import _sandbox_worker as worker
from architect.runtime import sandbox_language as language
from architect.runtime.sandbox import (
    CALL_LIMIT,
    SandboxError,
    SourceStats,
    _encode,
    _validated_tree,
    call_limit,
    rebind_source,
    sandbox_harness,
    validate_source,
)
from architect.spec import Budget, FloorDefinition, FloorKind, HarnessArtifact, Spec

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def v2(tmp_path: Path) -> HarnessArtifact:
    return make_artifact(tmp_path / "v2", forge=binding(children=("child",)))


@pytest.fixture
def v1(tmp_path: Path) -> HarnessArtifact:
    return make_artifact(tmp_path / "v1", v1=True)


@pytest.fixture
def game(tmp_path: Path) -> HarnessArtifact:
    return game_artifact(tmp_path / "game", body("return None"))


# ---------------------------------------------------------------- validator: accept

ACCEPTED = {
    "module": (
        '"""A reference harness."""\n'
        "from __future__ import annotations\n"
        "from pydantic import JsonValue\n"
        "from architect.presets import execute_preset\n"
        "from architect.runtime import RunContext\n"
        "LIMIT = 3\n"
        "TABLE = {'a': [1, 2.5, None, True], 'b': (3, -4)}\n"
        "async def ask(context, prompt):\n"
        "    return await context.call(prompt, 16)\n"
        "def double(x):\n"
        "    '''Twice x.'''\n"
        "    return x * 2\n"
        "async def run(context: RunContext, task: JsonValue) -> JsonValue:\n"
        "    return double(LIMIT)\n"
    ),
    "for-list": body("total = 0", "for x in [1, 2]:", "    total += x", "return total"),
    "for-pairs": body("for i, v in enumerate(['a']):", "    pass", "return i"),
    "for-dict-str-range": body(
        "for k in {'a': 1}:", "    pass", "for c in 'ab':", "    pass",
        "for n in range(3):", "    pass", "return [k, c, n]",
    ),
    "while-break-continue": body(
        "n = 0", "while True:", "    n += 1", "    if n < 3:", "        continue", "    break",
        "return n",
    ),
    "subscript-assign": body("d = {'a': [0]}", "d['a'][0] = 5", "d['b'] = {}", "return d"),
    "unpack": body("a, b = [1, 2]", "c, d = (3, 4)", "return a + b + c + d"),
    "augmented": body(
        "x = 10", "x += 1", "x -= 1", "x *= 2", "x //= 3", "x %= 5", "l = [1]", "l[0] += 1",
        "return x",
    ),
    "try": body("try:", "    x = task['k']", "except:", "    x = None", "return x"),
    "try-exception": body("try:", "    x = task['k']", "except Exception:", "    pass"),
    "unbound-relaxed": body("if task:", "    x = 1", "return x"),
    "expr-statements": body(
        "out = []", "append(out, 1)", "extend(out, [2])", "insert(out, 0, 0)", "pop(out)",
        "d = {}", "set_key(d, 'k', 1)", "await context.call('x')", "return out",
    ),
    "sync-helper-statement": (
        "def add(l, v):\n    append(l, v)\n" + body("out = []", "add(out, 1)", "return out")
    ),
    "arithmetic": body("return [1 - 2, 3 * 4, 5 / 2, 7 // 2, 7 % 3, -task, not task]"),
    "comparisons": body("return [1 < 2 <= 3, 'a' > 'b', 2 >= 1.5, task is None, task is not None]"),
    "ifexp": body("return 1 if task else 2"),
    "fstring": body("n = 3", "return f'n={n} task={task}'"),
    "slices": body("s = 'hello'", "l = [1, 2, 3]", "return [s[1:3], l[::-1], l[:2:1], s[-2:]]"),
    "computed-keys": body("k = 'x'", "return {k: 1, 'y' + k: 2}"),
    "tuple-literal": body("return (1, 2)"),
    "sorted-reverse": body("return sorted([3, 1], reverse=True)"),
    "call-keywords": body(
        "return await context.call('x', max_output_tokens=8, system='s', images=[])"
    ),
    "call-positional-system": body("return await context.call('x', 8, system='s')"),
    "harness": body("return await context.harness('child', task)"),
    "grids": body("g = [[0, 1], [1, 0]]", "return [grid_text(g), objects(g), grid_diff(g, g)]"),
    # Forge pack v2: annotations are accepted and ignored.
    "annotated-signatures": (
        "def clean(text: str, width: int = 3) -> str:\n    return strip(text)[:width]\n"
        "async def ask(context: RunContext, prompt: str) -> str:\n"
        "    return await context.call(prompt, 16)\n"
        "async def run(context: RunContext, task: dict) -> dict | None:\n"
        "    return {'w': await ask(context, clean(str(task)))}\n"
    ),
    "annotated-assignments": body(
        "n: int = 0", "words: list[str] = split('a b', ' ')", "d = {}", "d['k']: int = 1",
        "return [n, words, d]",
    ),
    "type-shaped-annotations": (
        "def f(a: 'Forward', b: typing.Optional[int], c: tuple[int, ...],\n"
        "      d: Literal['x', -1, True, None], e: Callable[[int], str], g: int | None)"
        " -> list[dict[str, int]]:\n    return [a, b, c, d, e, g]\n"
        + body("return f(1, 2, 3, 4, 5, 6)")
    ),
    "annotated-constant": "C: int = 1\nD: dict[str, int] = {'k': 2}\n" + body("return [C, D]"),
    # Defaults: a literal or a CONSTANT; trailing ones may be left off.
    "defaults": (
        "LIMIT = 3\n"
        "def pad(x, n=LIMIT, fill='0', neg=-1, items=[1, {'k': None}]):\n"
        "    return [x, n, fill, neg, items]\n"
        + body("return [pad(1), pad(1, 2), pad(1, 2, 'x', 0, [])]")
    ),
    "async-defaults": (
        "async def ask(context, prompt, tokens=8):\n    return await context.call(prompt, tokens)\n"
        + body("return [await ask(context, 'x'), await ask(context, 'y', 4)]")
    ),
    # A variable or parameter may reuse a builtin's name; calls still reach the builtin.
    "local-shadows-builtin": body(
        "count = 0", "items = [1, 2]", "for x in items:", "    count += 1", "len = count",
        "return [len, count('aa', 'a'), items]",
    ),
    "param-shadows-builtin": "def f(list, max, str):\n    return [list, max, str]\n" + body(
        "return f(1, 2, 3)"
    ),
    # The builtins' method spellings.
    "methods": body(
        "s = ' A,b '", "parts = s.strip().lower().split(',')", "out = []",
        "out.append(parts[0])", "out.extend(parts)", "out.insert(0, 'x')", "last = out.pop()",
        "d = {'k': 1}",
        "return [', '.join(out), d.get('k'), d.get('z', 0), d.keys(), d.values(), d.items(),"
        " s.startswith(' '), s.endswith(' '), s.replace('A', 'a'), s.find('b'), s.count(','),"
        " 'a b'.splitlines(), s.upper(), last]",
    ),
    "isinstance": body(
        "return [isinstance(task, dict), isinstance(task, (int, float)),"
        " isinstance(task, [str, list, bool])]"
    ),
    "get-at-two-arguments": body("return [get({'a': 1}, 'b'), at([1], 5), at('ab', 0)]"),
    # Comprehensions, desugared at check time into the loops they stand for.
    "comprehensions": body(
        "n = 2",
        "rows = [[x * n for x in range(3) if x] for _ in range(2)]",
        "flat = [y for row in rows for y in row if y > 1 if y < 9]",
        "pairs = {k: v for k, v in items({'a': 1}) if v}",
        "kinds = {kind(x) for x in [1, 'a', 2]}",
        "return [rows, flat, pairs, kinds, [await context.call(p) for p in ['a']]]",
    ),
    "generator-arguments": body(
        "l = [1, 2, 3]",
        "return [any(x > 2 for x in l), all(x > 0 for x in l), sum(x for x in l),"
        " min(x for x in l), max(x for x in l), sorted((x for x in l), reverse=True),"
        " list(x for x in l if x), join(',', (str(x) for x in l)),"
        " ','.join(str(x) for x in l), sorted(x for x in l)]",
    ),
    "any-all-builtins": body("return [any([0, 1]), all([]), any([x for x in [0]])]"),
    # A script's entry block is ignored whole, whatever it holds.
    "main-guard": body("return 1")
    + 'if __name__ == "__main__":\n    import asyncio\n    print(asyncio.run(run(1, 2)))\n',
    "main-guard-reversed": "if '__main__' == __name__:\n    import json\n" + body("return 1"),
    "run-annotated-variants": (
        "async def run(context: 'RunContext', task: dict[str, Any]) -> dict | None:\n"
        "    return {}\n"
    ),
    # Forge pack v4: typed and named excepts, three loop names, and the string builtins.
    "try-typed": body("try:", "    x = task['k']", "except KeyError:", "    x = None", "return x"),
    "try-named": body(
        "try:", "    x = int(task)", "except (ValueError, TypeError) as error:", "    x = error",
        "return x",
    ),
    "for-three-names": body(
        "out = []", "for a, b, c in zip([1], [2], [3]):", "    append(out, a + b + c)",
        "return [out, [a * b * c for a, b, c in zip([1], [2], [3])]]",
    ),
    "pack-v4-builtins": body(
        "s = ' Ab1, c '",
        "return [s.split(), split(s), s.strip(' ,'), s.lstrip(), lstrip(s, ' A'), s.rstrip(),"
        " rstrip(s, ' c'), s.rfind('c'), rfind(s, 'b'), s.isdigit(), isalpha(s), s.isalnum(),"
        " s.isspace(), s.isupper(), islower(s), chr(65), ord('A'), max(1, 2), min('a', 'b'),"
        " [1].copy(), {'k': 1}.copy(), copy([2]), zip([1], [2], [3])]",
    ),
}


@pytest.mark.parametrize("name", sorted(ACCEPTED))
def test_v2_validator_accepts_every_construct(v2: HarnessArtifact, name: str) -> None:
    stats = validate_source(ACCEPTED[name], v2)
    assert isinstance(stats, SourceStats) and stats.language == 2


def test_v2_statistics_count_the_module(v2: HarnessArtifact) -> None:
    source = ACCEPTED["module"]
    stats = validate_source(source, v2)
    assert stats.helpers == 2 and stats.constants == 2
    assert stats.await_sites == 1 and stats.loops == 0
    assert stats.bytes == len(source.encode("utf-8"))
    assert stats.nodes > 20 and 0 < stats.depth <= 40
    loops = validate_source(ACCEPTED["while-break-continue"], v2)
    assert loops.loops == 1


def test_game_capabilities_validate_only_for_game_specs(game: HarnessArtifact) -> None:
    source = body("o = await context.observe()", "return await context.step({'name': 'ACTION1'})")
    assert validate_source(source, game).language == 2


# ---------------------------------------------------------------- validator: reject

REJECTED: list[tuple[str, str, str, int | None]] = [
    ("break-outside-loop", body("break"), "sandbox_statement", 2),
    ("continue-outside-loop", body("if task:", "    continue"), "sandbox_statement", 3),
    ("subscript-assign-task", body("task['x'] = 1"), "sandbox_assignment", 2),
    ("subscript-assign-constant", "C = [1]\n" + body("C[0] = 2"), "sandbox_assignment", 3),
    ("assign-constant", "C = 1\n" + body("C = 2"), "sandbox_assignment", 3),
    ("mutate-constant", "C = [1]\n" + body("append(C, 2)"), "sandbox_assignment", 3),
    ("mutate-task", body("append(task, 2)"), "sandbox_assignment", 2),
    ("mutate-subscript", body("l = [[]]", "append(l[0], 2)"), "sandbox_assignment", 3),
    ("assign-task", body("task = 1"), "sandbox_assignment", 2),
    ("assign-context", "async def f(context, x):\n    context = x\n    return x\n"
     + body("return await f(context, 1)"), "sandbox_assignment", 2),
    ("assign-dunder", body("__class__ = 1"), "sandbox_assignment", 2),
    ("assign-chain", body("a = b = 1"), "sandbox_assignment", 2),
    ("assign-slice", body("l = [1]", "l[0:1] = [2]"), "sandbox_assignment", 3),
    ("augmented-divide", body("x = 1", "x /= 2"), "sandbox_assignment", 3),
    ("recursion-direct", "def f(x):\n    return f(x)\n" + body("return f(1)"),
     "sandbox_recursion", 2),
    ("recursion-mutual", "def f(x):\n    return g(x)\ndef g(x):\n    return f(x)\n"
     + body("return f(1)"), "sandbox_recursion", None),
    ("recursion-async", "async def f(context, x):\n    return await f(context, x)\n"
     + body("return await f(context, 1)"), "sandbox_recursion", 2),
    ("sync-helper-await", "def f(x):\n    return await g(x)\n" + body("return f(1)"),
     "sandbox_helper", 2),
    ("sync-helper-context", "def f(x):\n    return context\n" + body("return f(1)"),
     "sandbox_helper", 2),
    ("async-helper-first-param", "async def f(x):\n    return x\n"
     + body("return await f(1)"), "sandbox_helper", 1),
    ("async-helper-not-awaited", "async def f(context, x):\n    return x\n"
     + body("return f(context, 1)"), "sandbox_helper", 4),
    ("async-helper-without-context", "async def f(context, x):\n    return x\n"
     + body("return await f(task, 1)"), "sandbox_helper", 4),
    ("helper-varargs", "def f(*x):\n    return x\n" + body("return 1"), "sandbox_helper", 1),
    ("helper-decorator", "@f\ndef g(x):\n    return x\n" + body("return 1"), "sandbox_helper",
     2),
    ("helper-shadows-builtin", "def len(x):\n    return x\n" + body("return 1"),
     "sandbox_helper", 1),
    ("helper-shadows-capability", "def call(x):\n    return x\n" + body("return 1"),
     "sandbox_helper", 1),
    ("helper-nine-params", "def f(a, b, c, d, e, g, h, i, j):\n    return a\n" + body("return 1"),
     "sandbox_helper", 1),
    ("nested-def", body("def inner(x):", "    return x", "return 1"), "sandbox_helper", 2),
    ("attribute", body("return task.keys"), "sandbox_expression", 2),
    ("context-attribute-value", body("x = context.call", "return x"), "sandbox_expression", 2),
    ("context-unknown-capability", body("return await context.adapter()"),
     "sandbox_capability", 2),
    ("fstring-format-spec", body("return f'{task:>3}'"), "sandbox_expression", 2),
    ("fstring-conversion", body("return f'{task!r}'"), "sandbox_expression", 2),
    ("is-non-none", body("return task is task"), "sandbox_comparison", 2),
    ("is-not-non-none", body("return task is not 'x'"), "sandbox_comparison", 2),
    ("try-type-unknown", body("try:", "    pass", "except SystemExit as e:", "    pass"),
     "sandbox_statement", 4),
    ("try-else", body("try:", "    pass", "except:", "    pass", "else:", "    pass"),
     "sandbox_statement", 2),
    ("try-finally", body("try:", "    pass", "finally:", "    pass"), "sandbox_statement", 2),
    ("try-two-handlers", body("try:", "    pass", "except:", "    pass", "except:", "    pass"),
     "sandbox_statement", 2),
    ("try-type-dotted", body("try:", "    pass", "except json.JSONDecodeError:", "    pass"),
     "sandbox_statement", 4),
    ("try-type-call", body("try:", "    pass", "except (ValueError, f()):", "    pass"),
     "sandbox_statement", 4),
    ("try-name-reserved", body("try:", "    pass", "except ValueError as task:", "    pass"),
     "sandbox_assignment", 4),
    ("generator-assigned", body("g = (x for x in task)", "return g"), "sandbox_expression", 2),
    ("generator-in-len", body("return len(x for x in task)"), "sandbox_expression", 2),
    ("generator-join-first", body("return join((x for x in task), ',')"),
     "sandbox_expression", 2),
    ("generator-computed-reverse", body("r = True", "return sorted((x for x in task), reverse=r)"),
     "sandbox_expression", 3),
    ("comprehension-async", body("return [x async for x in task]"), "sandbox_statement", 2),
    ("comprehension-nested-target", body("return [a for i, (a, b) in task]"),
     "sandbox_assignment", 2),
    ("comprehension-name-leak", body("ys = [x for x in task]", "return x"),
     "sandbox_expression", 3),
    ("set-literal", body("return {1, 2}"), "sandbox_expression", 2),
    ("lambda", body("return (lambda: 1)"), "sandbox_expression", 2),
    ("walrus", body("return (x := 1)"), "sandbox_expression", 2),
    ("starred", body("return [*task]"), "sandbox_expression", 2),
    ("power", body("return 2 ** 10"), "sandbox_expression", 2),
    ("bit-and", body("return 2 & 3"), "sandbox_expression", 2),
    ("slice-step", body("return task[::2]"), "sandbox_expression", 2),
    ("unknown-name", body("return missing"), "sandbox_expression", 2),
    ("builtin-as-value", body("x = len", "return x"), "sandbox_expression", 2),
    ("unknown-function", body("return getattr(task, 'x')"), "sandbox_call", 2),
    ("builtin-arity", body("return len(task, task)"), "sandbox_call", 2),
    ("builtin-keyword", body("return len(task, key=1)"), "sandbox_call", 2),
    ("sorted-key", body("return sorted(task, key=len)"), "sandbox_call", 2),
    ("helper-arity", "def f(x):\n    return x\n" + body("return f(1, 2)"), "sandbox_call", 4),
    ("dict-unpack", body("return {**task}"), "sandbox_dictionary", 2),
    ("call-too-many-args", body("return await context.call('x', 1, 2)"), "sandbox_call", 2),
    ("call-tokens-twice", body("return await context.call('x', 1, max_output_tokens=2)"),
     "sandbox_call", 2),
    ("call-unknown-keyword", body("return await context.call('x', unknown=1)"),
     "sandbox_call", 2),
    ("call-starred", body("return await context.call(*task)"), "sandbox_call", 2),
    ("loop-nesting-7", body(*[" " * 4 * i + "while task:" for i in range(7)],
                            " " * 28 + "pass"), "sandbox_loop_depth", 8),
    ("for-else", body("for x in task:", "    pass", "else:", "    pass"),
     "sandbox_statement", 2),
    ("for-target-subscript", body("l = [0]", "for l[0] in task:", "    pass"),
     "sandbox_assignment", 3),
    ("for-four-names", body("for a, b, c, d in task:", "    pass"), "sandbox_assignment", 2),
    ("observe-non-game", body("return await context.observe()"), "sandbox_capability", 2),
    ("step-non-game", body("return await context.step({'name': 'ACTION1'})"),
     "sandbox_capability", 2),
    ("harness-unbound", body("return await context.harness('other', task)"),
     "sandbox_binding_mismatch", 2),
    ("harness-computed-name", body("n = 'child'", "return await context.harness(n, task)"),
     "sandbox_binding_mismatch", 3),
    ("await-builtin", body("return await len(task)"), "sandbox_capability", 2),
    ("await-sync-helper", "def f(x):\n    return x\n" + body("return await f(1)"),
     "sandbox_capability", 4),
    ("preset-unbound", body("return await execute_preset('x', {}, (), {}, context, task)"),
     "sandbox_binding_mismatch", 2),
    ("preset-not-awaited", body("return execute_preset(1)"), "sandbox_capability", 2),
    ("raise", body("raise ValueError"), "sandbox_statement", 2),
    ("assert", body("assert task"), "sandbox_statement", 2),
    ("with", body("with task:", "    pass"), "sandbox_statement", 2),
    ("del", body("x = 1", "del x"), "sandbox_statement", 3),
    ("global", body("global x"), "sandbox_statement", 2),
    ("import", body("import os"), "sandbox_statement", 2),
    ("class", body("class C:", "    pass"), "sandbox_statement", 2),
    ("async-for", body("async for x in task:", "    pass"), "sandbox_statement", 2),
    ("bare-expression", body("task"), "sandbox_statement", 2),
    ("constant-call", "C = len('x')\n" + body("return C"), "sandbox_constant", 1),
    ("constant-name", "c = 1\n" + body("return c"), "sandbox_constant", 1),
    ("constant-duplicate", "C = 1\nC = 2\n" + body("return C"), "sandbox_constant", 2),
    ("constant-fstring", "C = f'x'\n" + body("return C"), "sandbox_constant", 1),
    ("run-not-last", body("return 1") + "C = 1\n", "sandbox_entrypoint", 3),
    ("two-runs", body("return 1") + body("return 2"), "sandbox_entrypoint", 1),
    ("module-expression", "print(1)\n" + body("return 1"), "sandbox_entrypoint", 1),
    ("main-guard-else", 'if __name__ == "__main__":\n    pass\nelse:\n    pass\n'
     + body("return 1"), "sandbox_entrypoint", 1),
    ("module-class", "class C:\n    pass\n" + body("return 1"), "sandbox_statement", 1),
    ("import-json", "import json\n" + body("return 1"), "sandbox_import", 1),
    ("from-typing", "from typing import Any\n" + body("return 1"), "sandbox_import", 1),
    ("import-json-inside", body("import json", "return 1"), "sandbox_import", 2),
    ("run-sync", "def run(context, task):\n    return 1\n", "sandbox_signature", 1),
    ("run-other-names", "async def run(item, model):\n    return 1\n",
     "sandbox_signature", 1),
    ("late-import", "C = 1\nimport os\n" + body("return 1"), "sandbox_import", 2),
    ("run-signature", "async def run(context, task, extra):\n    return 1\n",
     "sandbox_signature", 1),
    # Forge pack v2: what is still refused, each with its own rule.
    ("method-unknown", body("s = 'a1'", "return s.isnumeric()"), "sandbox_expression", 3),
    ("method-arity", body("s = 'a b'", "return s.split(' ', 1)"), "sandbox_call", 3),
    ("method-keyword", body("s = 'a'", "return s.split(sep=',')"), "sandbox_call", 3),
    ("method-mutates-subscript", body("d = {'k': []}", "d['k'].append(1)"),
     "sandbox_assignment", 3),
    ("method-mutates-task", body("task.append(1)"), "sandbox_assignment", 2),
    ("method-on-context", body("return context.call('x')"), "sandbox_capability", 2),
    ("method-unknown-on-context", body("return context.adapter()"), "sandbox_capability", 2),
    ("method-dunder", body("return task.__class__()"), "sandbox_expression", 2),
    ("method-statement-no-effect", body("s = 'a'", "s.strip()"), "sandbox_statement", 3),
    ("isinstance-unknown-type", body("return isinstance(task, set)"), "sandbox_call", 2),
    ("isinstance-arity", body("return isinstance(task)"), "sandbox_call", 2),
    ("isinstance-computed-type", body("t = 'x'", "return isinstance(task, t)"),
     "sandbox_call", 3),
    ("helper-named-isinstance", "def isinstance(x):\n    return x\n" + body("return 1"),
     "sandbox_helper", 1),
    ("helper-default-call", "def f(x=len('a')):\n    return x\n" + body("return f()"),
     "sandbox_helper", 1),
    ("helper-default-name", "def f(x=y):\n    return x\n" + body("return f()"),
     "sandbox_helper", 1),
    ("async-helper-context-default", "async def f(context=None):\n    return 1\n"
     + body("return await f(context)"), "sandbox_helper", 1),
    ("async-helper-ctx", "async def f(ctx, x):\n    return x\n"
     + body("return await f(context, 1)"), "sandbox_helper", 1),
    ("helper-keyword-call", "def f(x, n=1):\n    return x\n" + body("return f(1, n=2)"),
     "sandbox_call", 4),
    ("helper-too-few", "def f(x, n=1):\n    return x\n" + body("return f()"),
     "sandbox_call", 4),
    ("helper-too-many", "def f(x, n=1):\n    return x\n" + body("return f(1, 2, 3)"),
     "sandbox_call", 4),
    ("helper-duplicate", "def f(x):\n    return x\ndef f(x):\n    return x\n"
     + body("return 1"), "sandbox_helper", 3),
    ("run-context-renamed", "async def run(ctx, task):\n    return 1\n", "sandbox_signature", 1),
    ("annotation-call", "def f(x: print(1)) -> int:\n    return x\n" + body("return f(1)"),
     "sandbox_expression", 1),
    ("annotation-await", "async def f(context, x: await context.call('leak')):\n"
     "    return x\n" + body("return await f(context, 1)"), "sandbox_expression", 1),
    ("annotation-return-call", "def f(x) -> __import__('os').system('x'):\n    return x\n"
     + body("return f(1)"), "sandbox_expression", 1),
    ("annotation-lambda", body("x: (lambda: 1) = 2", "return x"), "sandbox_expression", 2),
    ("run-annotation-call",
     "async def run(context: RunContext, task: open('x')) -> JsonValue:\n    return 1\n",
     "sandbox_expression", 1),
    ("annotation-no-value", body("x: int", "return 1"), "sandbox_statement", 2),
    ("constant-annotation-no-value", "C: int\n" + body("return 1"), "sandbox_constant", 1),
    ("constant-annotation-call", "C: len('x') = 1\n" + body("return C"),
     "sandbox_expression", 1),
    ("assign-task-in-helper", "def f(x):\n    task = x\n    return task\n" + body("return f(1)"),
     "sandbox_assignment", 2),
    ("assign-reserved", body("step = 1", "return step"), "sandbox_assignment", 2),
    ("get-too-many", body("return get({}, 'k', 1, 2)"), "sandbox_call", 2),
    ("get-too-few", body("return get({})"), "sandbox_call", 2),
    ("missing-print", body("print(task)"), "sandbox_call", 2),
    ("callee-computed", body("return [len][0](task)"), "sandbox_expression", 2),
]

# The rule each refusal above names (``sandbox_language.RULES``).
REJECTED_RULES: dict[str, str] = {
    "break-outside-loop": "break_outside_loop", "continue-outside-loop": "break_outside_loop",
    "subscript-assign-task": "assign_root", "subscript-assign-constant": "assign_root",
    "assign-constant": "assign_constant", "mutate-constant": "mutate_root",
    "mutate-task": "mutate_root", "mutate-subscript": "mutate_root", "assign-task": "assign_task",
    "assign-context": "assign_context", "assign-dunder": "assign_name",
    "assign-chain": "assign_chained", "assign-slice": "assign_slice",
    "augmented-divide": "augmented_op", "recursion-direct": "recursion",
    "recursion-mutual": "recursion", "recursion-async": "recursion",
    "sync-helper-await": "helper_sync_await", "sync-helper-context": "helper_sync_await",
    "async-helper-first-param": "helper_async_context",
    "async-helper-not-awaited": "async_helper_await",
    "async-helper-without-context": "async_helper_context_arg",
    "helper-varargs": "helper_parameter_kind", "helper-decorator": "helper_decorator",
    "helper-shadows-builtin": "helper_builtin_name", "helper-shadows-capability": "helper_reserved",
    "helper-nine-params": "helper_parameter_count", "nested-def": "nested_def",
    "attribute": "attribute", "context-attribute-value": "attribute",
    "context-unknown-capability": "capability_unknown", "fstring-format-spec": "fstring_format",
    "fstring-conversion": "fstring_format", "is-non-none": "is_none_only",
    "is-not-non-none": "is_none_only", "try-type-unknown": "except_shape", "try-else": "try_shape",
    "try-finally": "try_shape", "try-two-handlers": "try_shape", "try-type-dotted": "except_shape",
    "try-type-call": "except_shape", "try-name-reserved": "assign_task",
    "generator-assigned": "generator", "generator-in-len": "generator",
    "generator-join-first": "generator", "generator-computed-reverse": "generator",
    "comprehension-async": "async_block", "comprehension-nested-target": "for_target",
    "comprehension-name-leak": "unknown_name", "main-guard-else": "module_statement",
    "module-class": "class", "import-json": "import_module", "from-typing": "import_module",
    "import-json-inside": "import_module", "run-sync": "run_signature",
    "run-other-names": "run_signature", "set-literal": "set_literal", "lambda": "lambda",
    "walrus": "walrus", "starred": "starred", "power": "operator", "bit-and": "operator",
    "slice-step": "slice_step", "unknown-name": "unknown_name", "builtin-as-value": "unknown_name",
    "unknown-function": "missing_function", "builtin-arity": "builtin_arity",
    "builtin-keyword": "builtin_keyword", "sorted-key": "builtin_keyword",
    "helper-arity": "helper_arity", "dict-unpack": "dict_unpack",
    "call-too-many-args": "call_args", "call-tokens-twice": "call_args",
    "call-unknown-keyword": "call_args", "call-starred": "call_starred",
    "loop-nesting-7": "loop_depth", "for-else": "loop_else", "for-target-subscript": "for_target",
    "for-four-names": "for_target", "observe-non-game": "game_only",
    "step-non-game": "game_only", "harness-unbound": "harness_unbound",
    "harness-computed-name": "harness_unbound", "await-builtin": "await_target",
    "await-sync-helper": "await_target", "preset-unbound": "binding_preset",
    "preset-not-awaited": "await_capability", "raise": "raise", "assert": "assert",
    "with": "with", "del": "del", "global": "global", "import": "import_inside",
    "class": "class", "async-for": "async_block", "bare-expression": "expression_statement",
    "constant-call": "constant_value", "constant-name": "constant_name",
    "constant-duplicate": "constant_duplicate", "constant-fstring": "constant_value",
    "run-not-last": "run_last", "two-runs": "run_twice", "module-expression": "module_statement",
    "late-import": "import", "run-signature": "run_signature",
    "method-unknown": "method_unknown", "method-arity": "method_arity",
    "method-keyword": "builtin_keyword", "method-mutates-subscript": "mutate_root",
    "method-mutates-task": "mutate_root", "method-on-context": "await_capability",
    "method-unknown-on-context": "capability_unknown", "method-dunder": "method_unknown",
    "method-statement-no-effect": "expression_statement",
    "isinstance-unknown-type": "isinstance_form", "isinstance-arity": "isinstance_form",
    "isinstance-computed-type": "isinstance_form", "helper-named-isinstance": "helper_reserved",
    "helper-default-call": "helper_default_value", "helper-default-name": "helper_default_value",
    "async-helper-context-default": "helper_async_context",
    "async-helper-ctx": "helper_async_context", "helper-keyword-call": "helper_keyword",
    "helper-too-few": "helper_arity", "helper-too-many": "helper_arity",
    "helper-duplicate": "helper_duplicate", "run-context-renamed": "run_signature",
    "annotation-call": "annotation_form", "annotation-await": "annotation_form",
    "annotation-return-call": "annotation_form", "annotation-lambda": "annotation_form",
    "run-annotation-call": "annotation_form", "annotation-no-value": "annotation_no_value",
    "constant-annotation-no-value": "constant_no_value",
    "constant-annotation-call": "annotation_form", "assign-task-in-helper": "assign_task",
    "assign-reserved": "assign_reserved", "get-too-many": "builtin_arity",
    "get-too-few": "builtin_arity", "missing-print": "missing_function",
    "callee-computed": "callee",
}


@pytest.mark.parametrize(
    ("source", "code", "line", "rule"),
    [
        pytest.param(source, code, line, REJECTED_RULES[name], id=name)
        for name, source, code, line in REJECTED
    ],
)
def test_v2_validator_rejects_with_fixed_code_and_line(
    v2: HarnessArtifact, source: str, code: str, line: int | None, rule: str
) -> None:
    with pytest.raises(SandboxError, match=f"^{code}$") as raised:
        validate_source(source, v2)
    assert raised.value.code == code
    if line is not None:
        assert raised.value.line == line
    # Each refusal names its rule, whose fixed text belongs to the same code.
    assert raised.value.rule == rule
    assert language.RULES[rule].code == code


def test_an_unlisted_method_names_the_form_that_does_the_job(v2: HarnessArtifact) -> None:
    validate_source(body("return task.split(',')"), v2)  # a listed method spelling
    with pytest.raises(SandboxError) as raised:
        validate_source(body("return task.isnumeric()"), v2)
    error = raised.value
    assert (error.code, error.rule, error.name) == (
        "sandbox_expression", "method_unknown", "isnumeric"
    )
    message = language.refusal_message(error.code, error.rule, error.name, error.line)
    assert message == "Line 2: isnumeric does not exist here: use s.isdigit()."
    # A method the alternatives table does not know is refused without being named.
    with pytest.raises(SandboxError) as raised:
        validate_source(body("return task.secret_method()"), v2)
    assert raised.value.name is None
    message = language.refusal_message("sandbox_expression", raised.value.rule, None, 2)
    assert "secret_method" not in message and "s.split(sep)" in message


def test_counts_limits_for_helpers_constants_and_nodes(v2: HarnessArtifact) -> None:
    constants = "".join(f"C{i} = {i}\n" for i in range(65))
    with pytest.raises(SandboxError, match="^sandbox_constant$"):
        validate_source(constants + body("return 1"), v2)
    assert validate_source(constants[: constants.index("C64")] + body("return 1"), v2)
    helpers = "".join(f"def h{i}(x):\n    return x\n" for i in range(33))
    with pytest.raises(SandboxError, match="^sandbox_helper$"):
        validate_source(helpers + body("return 1"), v2)
    nesting = body(*[" " * 4 * i + "while task:" for i in range(6)], " " * 24 + "pass")
    assert validate_source(nesting, v2).loops == 6
    with pytest.raises(SandboxError, match="^sandbox_ast_limit$"):
        validate_source(body("return [" + ",".join(["0"] * 8001) + "]"), v2)
    assert validate_source(body("return [" + ",".join(["0"] * 3000) + "]"), v2).nodes > 3000


def test_await_sites_are_not_capped_statically_in_v2(v2: HarnessArtifact, v1: HarnessArtifact
                                                     ) -> None:
    source = body(*["await context.call('x')"] * 70, "return 1")
    assert validate_source(source, v2).await_sites == 70
    with pytest.raises(SandboxError, match="^sandbox_call_limit$"):
        validate_source(source, v1)


# ---------------------------------------------------------------- language 1 frozen


@pytest.mark.parametrize(
    ("source", "line"),
    [
        (body("for x in task:", "    pass", "return 1"), 2),
        (body("x = 1", "while x:", "    x = 0", "return x"), 3),
        (body("return f'{task}'"), 2),
        (body("return 1 < 2"), 2),
        ("C = 1\n" + body("return C"), 1),
        ("def f(x):\n    return x\n" + body("return f(1)"), 1),
        (body("try:", "    return task['x']", "except:", "    return None"), 2),
        (body("return [1, 2][0:1]"), 2),
    ],
)
def test_v2_construct_under_v1_binding_is_sandbox_language(
    v1: HarnessArtifact, source: str, line: int
) -> None:
    with pytest.raises(SandboxError, match="^sandbox_language$") as raised:
        validate_source(source, v1)
    assert raised.value.line == line
    with pytest.raises(SandboxError, match="^sandbox_language$"):
        _validated_tree(source, v1)


@pytest.mark.parametrize(
    ("source", "code"),
    [
        (body("return await context.call(task, images=[])"), "sandbox_call"),
        (body("return await context.call(task, system='s')"), "sandbox_call"),
        (body("return [x for x in task]"), "sandbox_expression"),
        (body("return task.keys"), "sandbox_expression"),
        (body(*["await context.call('x')"] * 65), "sandbox_call_limit"),
        (body("x = 1", "return x - 1"), "sandbox_language"),
    ],
)
def test_v1_keeps_its_exact_code_set(v1: HarnessArtifact, source: str, code: str) -> None:
    with pytest.raises(SandboxError, match=f"^{code}$"):
        validate_source(source, v1)


def preset_artifacts(root: Path) -> list[HarnessArtifact]:
    """Every preset's own sandbox emit, each with a spec it builds for."""
    artifacts = []
    for preset in all_presets():
        spec = text_spec()
        if preset.name == "coder":
            spec = spec.model_copy(update={"tools": ("write",)})
        params = preset.validate_params({})
        directory = root / preset.name
        directory.mkdir(parents=True)
        preset.emit(directory, spec, params, preset.components)
        artifacts.append(
            HarnessArtifact(
                spec=spec, directory=directory, preset=preset.name, params=params,
                components=preset.components, model="fake", execution_mode="sandbox",
            )
        )
    return artifacts


def test_every_preset_emit_and_rewrite_fixture_validate_as_language_1(tmp_path: Path) -> None:
    artifacts = preset_artifacts(tmp_path)
    assert len(artifacts) == len(all_presets())
    for artifact in artifacts:
        source = (artifact.directory / "harness.py").read_text(encoding="utf-8")
        before = artifact.artifact_digest
        stats = validate_source(source, artifact)
        assert stats.language == 1 and stats.helpers == stats.constants == stats.loops == 0
        assert call_limit(artifact) == CALL_LIMIT == 64
        assert artifact.artifact_digest == before
        assert "forge" not in artifact._config()
    baseline = (FIXTURES / "rewrites" / "baseline.py").read_text(encoding="utf-8")
    stats = validate_source(baseline, artifacts[0])
    assert stats == SourceStats(1, stats.nodes, stats.depth, 0, 0, 0, 3, len(baseline))


def test_v1_artifact_digest_is_pinned(tmp_path: Path) -> None:
    """A committed-fixture artifact keeps its pre-Phase 9 digest (forge omitted)."""
    spec = Spec(
        task="Produce the requested result", input_schema={"type": "object"},
        output_schema={"type": "string"},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, dataset="synthetic", limit=1),
        budget=Budget(cost_usd=0.025),
    )
    directory = tmp_path / "artifact"
    directory.mkdir()
    (directory / "harness.py").write_bytes((FIXTURES / "rewrites" / "baseline.py").read_bytes())
    preset = get_preset("synthesizer")
    params = preset.validate_params({})
    artifact = HarnessArtifact(
        spec=spec, directory=directory, preset=preset.name, params=params,
        components=preset.components, model="fake", execution_mode="sandbox",
    )
    config = {
        "version": 1, "preset": "synthesizer", "model": "fake",
        "components": list(preset.components), "params": params, "execution_mode": "sandbox",
    }
    code = hashlib.sha256((directory / "harness.py").read_bytes()).hexdigest()
    payload = {"spec_hash": spec.spec_hash, "config": config, "code_sha256": code}
    expected = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    assert artifact.artifact_digest == expected
    validate_source((directory / "harness.py").read_text(encoding="utf-8"), artifact)
    assert artifact.artifact_digest == expected


async def test_v1_rewrite_fixture_runs_identically_through_the_worker(tmp_path: Path) -> None:
    artifact = make_artifact(
        tmp_path / "v1", (FIXTURES / "rewrites" / "baseline.py").read_text(), v1=True
    )
    texts = ["first", "second", "final"]
    adapter = FakeAdapter(responses=[
        ModelResponse(text=text, tokens_in=1, tokens_out=1, cost_usd=0, model="fake")
        for text in texts
    ])
    context = RunContext(adapter, Budget())
    assert await sandbox_harness(artifact)(context, {"x": 1}) == "final"
    assert adapter.calls == 3 and context.fuel_used == 0


async def test_v1_int_addition_still_raises_sandbox_add(v1: HarnessArtifact) -> None:
    tree = _validated_tree(body("return 1 + 2"), v1)
    with pytest.raises(SandboxError, match="^sandbox_add$"):
        await worker._Interpreter(None).block(tree.body)


def test_v1_worker_rejects_a_forge_binding_and_v2_rejects_a_missing_one(
    monkeypatch: pytest.MonkeyPatch, v1: HarnessArtifact, v2: HarnessArtifact
) -> None:
    fields = {"spec", "preset", "params", "components", "model", "forge", "execution_mode"}
    for artifact, version in ((v2, 1), (v1, 2)):
        initial = {
            "source": body("return 1"),
            "binding": artifact.model_dump(mode="json", include=fields),
            "task": None,
            "language": version,
        }
        monkeypatch.setattr(worker, "_receive", lambda initial=initial: initial)
        with pytest.raises(SandboxError, match="^sandbox_invalid_message$"):
            asyncio.run(worker._main())
    for bad in (3, True, "2"):
        initial = {"source": "", "binding": {}, "task": None, "language": bad}
        monkeypatch.setattr(worker, "_receive", lambda initial=initial: initial)
        with pytest.raises(SandboxError, match="^sandbox_invalid_message$"):
            asyncio.run(worker._main())


def test_call_limit_reads_the_binding(v1: HarnessArtifact, tmp_path: Path) -> None:
    assert call_limit(v1) == 64
    assert call_limit(make_artifact(tmp_path / "a", forge=binding(call_limit=128))) == 128
    game = game_artifact(tmp_path / "g", body("return 1"), max_turns=80, calls_per_turn=2)
    assert call_limit(game) == 160


def test_rebind_source_rewrites_every_preset_call_in_a_v2_module(tmp_path: Path) -> None:
    artifact = make_artifact(tmp_path / "a")
    emitted = (tmp_path / "a" / "harness.py").read_text(encoding="utf-8")
    preset_call = emitted[emitted.index("await execute_preset(") :].split("\n")[0]
    source = (
        "async def answer(context, x):\n    return "
        + preset_call.strip().replace(", context, task)", ", context, x)") + "\n"
        + body("return await answer(context, task)")
    )
    validate_source(source, artifact)
    new_spec = artifact.spec.model_copy(update={"task": "summarize again"})
    rebound = rebind_source(source, artifact, new_spec)
    moved = artifact.model_copy(update={"spec": new_spec})
    assert validate_source(rebound, moved).helpers == 1
    with pytest.raises(SandboxError, match="^sandbox_binding_mismatch$"):
        validate_source(source, moved)


# ---------------------------------------------------------------- semantics (in process)


def result(frame: dict[str, Any]) -> JsonValue:
    assert frame["op"] == "result", frame
    return frame["value"]


def failure(frame: dict[str, Any]) -> tuple[str, int | None]:
    assert frame["op"] == "error" and set(frame) == {"op", "code", "line"}, frame
    return frame["code"], frame["line"]


def run_source(monkeypatch: pytest.MonkeyPatch, artifact: HarnessArtifact, source: str,
               task: JsonValue = None, replies: tuple[JsonValue, ...] = ()) -> dict[str, Any]:
    return interpret(monkeypatch, artifact, source, task, replies)[0]


@pytest.mark.parametrize(
    ("lines", "task", "expected"),
    [
        (("l = [1]", "append(l, l)", "return l"), None, [1, [1]]),
        (("a = [1]", "b = [a]", "append(a, 2)", "return [a, b]"), None, [[1, 2], [[1]]]),
        (("a = [1]", "b = {'k': a}", "a[0] = 9", "return b"), None, {"k": [1]}),
        (("a = [1]", "b = a", "append(b, 2)", "return a"), None, [1, 2]),
        (("p = [[0]]", "e = enumerate(p)", "e[0][1][0] = 5", "return p"), None, [[0]]),
        (("l = [[0]]", "m = l + l", "m[0][0] = 1", "return [l, m]"), None,
         [[[0]], [[1], [0]]]),
        (("x = task", "x['k'] = 2", "return task"), {"k": 1}, {"k": 2}),
        (("total = 0", "for k, v in items({'a': 1, 'b': 2}):", "    total += v",
          "return total"), None, 3),
        (("out = []", "for c in 'ab':", "    append(out, c)", "return out"), None, ["a", "b"]),
        (("n = 0", "for i in range(10):", "    if i % 2 == 0:", "        continue",
          "    n += i", "    if i > 6:", "        break", "return n"), None, 16),
        (("return [7 // 2, -7 // 2, 7 % 3, 7 / 2, 2 * 3.5]",), None, [3, -4, 1, 3.5, 7.0]),
        (("return ['a' + 'b', [1] + [2], 1 + 2.5]",), None, ["ab", [1, 2], 3.5]),
        (("return [1 < 2 < 3, 3 > 2 > 2, 'a' <= 'a', task is None]",), None,
         [True, False, True, True]),
        (("return [1 in [1], 'a' in 'cat', 'k' in {'k': 1}, 2 not in [1]]",), None,
         [True, True, True, True]),
        (("return [0 or 'x', 1 and 2, not []]",), None, ["x", 2, True]),
        (("n = 2", "return f'n={n} l={[1, 2]} d={task} s={\"q\"}'",), {"b": 1, "a": None},
         'n=2 l=[1,2] d={"a":null,"b":1} s=q'),
        (("s = 'hello'", "l = [1, 2, 3]", "return [s[1:3], l[::-1], l[:-1], s[::-1]]"), None,
         ["el", [3, 2, 1], [1, 2], "olleh"]),
        (("a, b = [1, 2]", "a, b = [b, a]", "return [a, b]"), None, [2, 1]),
        (("d = {'a': {'b': [0, 0]}}", "d['a']['b'][1] += 5", "return d"), None,
         {"a": {"b": [0, 5]}}),
        (("k = 'x'", "return {k: 1, k + k: (1, 2)}"), None, {"x": 1, "xx": [1, 2]}),
        (("return 'yes' if task else 'no'",), 0, "no"),
        (("if task:", "    x = 1", "return 2"), None, 2),
        (("return",), None, None),
        (("pass",), None, None),
    ],
)
def test_v2_value_semantics(monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact,
                            lines: tuple[str, ...], task: JsonValue, expected: JsonValue) -> None:
    assert result(run_source(monkeypatch, v2, body(*lines), task)) == expected


def test_constants_are_read_only_even_through_helpers(monkeypatch: pytest.MonkeyPatch,
                                                      v2: HarnessArtifact) -> None:
    source = (
        "TABLE = {'seen': [1]}\n"
        "def grow(d):\n    s = d['seen']\n    append(s, 2)\n    return d\n"
        "def grow_list(l):\n    append(l, 3)\n    return l\n"
        + body("t = grow(TABLE)", "u = TABLE['seen']", "grow_list(u)",
               "return [t, TABLE, u]")
    )
    assert result(run_source(monkeypatch, v2, source)) == [
        {"seen": [1, 2]}, {"seen": [1]}, [1, 3]
    ]


def test_helpers_get_fresh_locals_and_alias_arguments(monkeypatch: pytest.MonkeyPatch,
                                                      v2: HarnessArtifact) -> None:
    source = (
        "def fill(l, n):\n    i = 0\n    while i < n:\n        append(l, i)\n        i += 1\n"
        "    return i\n"
        "async def ask(context, prompt):\n    return await context.call(prompt)\n"
        + body("i = 'outer'", "out = []", "n = fill(out, 3)",
               "reply = await ask(context, 'q')", "return [i, out, n, reply]")
    )
    frame, sent = interpret(monkeypatch, v2, source, replies=("answer",))
    assert result(frame) == ["outer", [0, 1, 2], 3, "answer"]
    assert sent == [{"op": "call", "prompt": "q", "tokens": None}]
    assert frame["fuel"] > 0


def test_fuel_exhaustion_is_fatal_and_bounded(monkeypatch: pytest.MonkeyPatch,
                                              tmp_path: Path) -> None:
    artifact = make_artifact(tmp_path / "a", forge=binding(fuel=100_000))
    started = time.perf_counter()
    frame = run_source(monkeypatch, artifact, body("while True:", "    pass"))
    assert failure(frame) == ("sandbox_fuel", 3)
    assert time.perf_counter() - started < wall_limit(5)
    source = body("try:", "    while True:", "        pass", "except:", "    return 'caught'")
    assert failure(run_source(monkeypatch, artifact, source))[0] == "sandbox_fuel"


def test_try_catches_data_errors_only(monkeypatch: pytest.MonkeyPatch,
                                      v2: HarnessArtifact) -> None:
    caught = body("try:", "    x = task['missing']", "except:", "    x = 'caught'", "return x")
    assert result(run_source(monkeypatch, v2, caught, {})) == "caught"
    for expression in ("[1][5]", "1 + 'a'", "1 / 0", "int('x')", "{1: 2}", "len(1)"):
        source = body("try:", f"    x = {expression}", "except:", "    x = 'caught'", "return x")
        assert result(run_source(monkeypatch, v2, source)) == "caught", expression
    invalid = body("try:", "    x = await context.call(1)", "except:", "    x = 'caught'",
                   "return x")
    assert failure(run_source(monkeypatch, v2, invalid)) == ("sandbox_invalid_call", 3)
    unbound = body("try:", "    x = y", "except:", "    x = 1", "if task:", "    y = 1")
    assert failure(run_source(monkeypatch, v2, unbound)) == ("sandbox_unbound", 3)
    nested = (
        "def pick(d):\n    return d['k']\n"
        + body("try:", "    x = pick(task)", "except Exception:", "    x = 'fallback'",
               "return x")
    )
    assert result(run_source(monkeypatch, v2, nested, {})) == "fallback"


@pytest.mark.parametrize(
    ("lines", "code", "line"),
    [
        (("x = task['missing']",), "sandbox_key", 2),
        (("x = [1]", "return x[3]"), "sandbox_index", 3),
        (("return 'x' * 3",), "sandbox_type", 2),
        (("return [1] * 3",), "sandbox_type", 2),
        (("return True - 1",), "sandbox_type", 2),
        (("return 'a' < 1",), "sandbox_type", 2),
        (("return 1 in 'abc'",), "sandbox_type", 2),
        (("return -'a'",), "sandbox_type", 2),
        (("return 1 + 'a'",), "sandbox_add", 2),
        (("return 1 / 0",), "sandbox_value", 2),
        (("return 1 // 0",), "sandbox_value", 2),
        (("return 1 % 0",), "sandbox_value", 2),
        (("x = 9223372036854775807", "return x + 1"), "sandbox_value", 3),
        (("x = 4294967296", "return x * x"), "sandbox_value", 3),
        (("x = 1e308", "return x * 10"), "sandbox_value", 3),
        (("return -9223372036854775807 - 2",), "sandbox_value", 2),
        (("a, b = [1]",), "sandbox_value", 2),
        (("for a, b in [[1, 2, 3]]:", "    pass"), "sandbox_value", 2),
        (("for x in 5:", "    pass"), "sandbox_type", 2),
        (("return {task: 1}",), "sandbox_dictionary", 2),
        (("d = {}", "d[1] = 2"), "sandbox_dictionary", 3),
        (("l = [1]", "l['a'] = 2"), "sandbox_type", 3),
        (("l = [1]", "l[4] = 2"), "sandbox_index", 3),
        (("s = 'ab'", "s[0] = 'c'"), "sandbox_type", 3),
        (("return y", "y = 1"), "sandbox_unbound", 2),
        (("return await context.call('x', 0)",), "sandbox_invalid_call", 2),
        (("return await context.call('x', system=1)",), "sandbox_invalid_call", 2),
        (("return await context.call('x', images=['sha'])",), "sandbox_invalid_call", 2),
        (("return await context.call('x', images='sha')",), "sandbox_invalid_call", 2),
        (("r = range(3)", "return r"), "sandbox_type", 3),
        (("r = range(3)", "return [r]"), "sandbox_type", 3),
        (("if range(3):", "    pass"), "sandbox_type", 2),
        (("return range(3) == range(3)",), "sandbox_type", 2),
        (("return str(range(2))",), "sandbox_type", 2),
        (("return range(0, 5, 0)",), "sandbox_value", 2),
        (("return range(200000)",), "sandbox_value_limit", 2),
        (("return min([])",), "sandbox_value", 2),
        (("return sorted([1, 'a'])",), "sandbox_type", 2),
    ],
)
def test_runtime_errors_have_fixed_codes_and_lines(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact, lines: tuple[str, ...], code: str,
    line: int,
) -> None:
    frame = run_source(monkeypatch, v2, body(*lines), {})
    assert failure(frame) == (code, line)
    assert code in language.RUNTIME_ERROR_CODES


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        ("len([1, 2])", 2), ("len(range(5))", 5), ("str(1.5)", "1.5"), ("str('x')", "x"),
        ("str({'b': [1], 'a': None})", '{"a":null,"b":[1]}'), ("int(' 42 ')", 42),
        ("int(3.9)", 3), ("int(True)", 1), ("float('2.5')", 2.5), ("bool('No')", False),
        ("bool('true')", True), ("bool([])", False), ("abs(-3)", 3), ("round(2.567, 2)", 2.57),
        ("round(2.5)", 2), ("min(['b', 'a'])", "a"), ("max([1, 3.5])", 3.5),
        ("sum([1, 2.5])", 3.5), ("sum([])", 0), ("sorted(['b', 'a'])", ["a", "b"]),
        ("list(range(1, 7, 2))", [1, 3, 5]), ("list('ab')", ["a", "b"]),
        ("list({'a': 1})", ["a"]), ("list([[1]])", [[1]]), ("enumerate(['x'])", [[0, "x"]]),
        ("zip([1, 2], ['a'])", [[1, "a"]]), ("kind(None)", "null"), ("kind(1.0)", "float"),
        ("kind({})", "dict"), ("keys({'a': 1})", ["a"]), ("values({'a': [1]})", [[1]]),
        ("items({'a': 1})", [["a", 1]]), ("get({'a': 1}, 'b', 0)", 0),
        ("get({'a': 1}, 'a', 0)", 1), ("at([1], 5, 'none')", "none"), ("at([1, 2], -1, 0)", 2),
        ("at('ab', 1, '')", "b"), ("at(range(3), 2, None)", 2), ("split('a,b', ',')", ["a", "b"]),
        ("join('-', ['a', 'b'])", "a-b"), ("strip('  x ')", "x"), ("lower('AB')", "ab"),
        ("upper('ab')", "AB"), ("startswith('abc', 'ab')", True), ("endswith('abc', 'x')", False),
        ("replace('aaa', 'a', 'bb')", "bbbbbb"), ("find('abc', 'c')", 2), ("count('aaa', 'a')", 3),
        ("splitlines('a\\nb')", ["a", "b"]), ("parse_json('{\"a\": [1, 2]}')", {"a": [1, 2]}),
        ("parse_json('{\"a\": 1, \"a\": 2}')", None), ("parse_json('NaN')", None),
        ("parse_json('1e999')", None), ("parse_json('[[[[1]]]]')", [[[[1]]]]),
        ("parse_json('nope')", None), ("to_json({'b': 1, 'a': [True]})", '{"a":[true],"b":1}'),
        ("grid_text([[1, 10]])", "1a"), ("count_colors([[1, 1, 2]])", {"1": 2, "2": 1}),
        ("find_cells([[0, 3]], 3)", [[1, 0]]), ("crop([[1, 2], [3, 4]], 1, 0, 1, 1)", [[2], [4]]),
        ("grid_equal([[1]], [[1]])", True), ("diff_cells([[1]], [[2]], 10)",
                                             {"cells": 1, "box": [0, 0, 0, 0]}),
        ("objects([[0, 5]], 0)",
         [{"color": 5, "cells": 1, "x0": 1, "y0": 0, "x1": 1, "y1": 0, "cx": 1.0, "cy": 0.0}]),
    ],
)
def test_builtins(monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact, call: str,
                  expected: JsonValue) -> None:
    assert result(run_source(monkeypatch, v2, body(f"return {call}"))) == expected


@pytest.mark.parametrize(
    "text",
    [
        'Sure! Here it is:\n```json\n{"action": {"name": "ACTION1"}}\n```\nGood luck.',
        'Sure!\n```\n{"action": {"name": "ACTION1"}}\n```',
        'The answer is {"action": {"name": "ACTION1"}} as requested.',
        'noise {broken} then {"action": {"name": "ACTION1"}}',
    ],
)
def test_extract_json_on_fenced_and_prose_wrapped_text(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact, text: str
) -> None:
    source = body("return extract_json(await context.call('x'))")
    frame = run_source(monkeypatch, v2, source, replies=(text,))
    assert result(frame) == {"action": {"name": "ACTION1"}}
    assert builtins.extract_json_text("no json here") is None
    assert builtins.parse_json_text("[" * 41 + "]" * 41) is None
    assert builtins.parse_json_text("[" * 40 + "]" * 40) is not None
    assert builtins.parse_json_text("[" * 60000 + "]" * 60000) is None
    assert builtins.extract_json_text("[1, {\"a\": \"]\"}] tail") == [1, {"a": "]"}]


def test_builtin_fuel_is_one_plus_work_over_256() -> None:
    meter = builtins.Meter(1000)
    builtins.BUILTIN_FUNCTIONS["len"](meter, [[1, 2]])
    assert meter.used == 2
    builtins.BUILTIN_FUNCTIONS["lower"](meter, ["x" * 600])
    assert meter.used == 2 + 1 + 3
    meter = builtins.Meter(3)
    with pytest.raises(builtins.Fatal, match="^sandbox_fuel$"):
        builtins.BUILTIN_FUNCTIONS["lower"](meter, ["x" * 600])


def _fuel(name: str, *args: object) -> int:
    meter = builtins.Meter(10**12)
    builtins.BUILTIN_FUNCTIONS[name](meter, list(args))
    return meter.used


def test_walk_charges_pay_for_every_item_a_builtin_visits() -> None:
    # Section 4.9 builtin work charges on top of 1 + ceil(work / 256): a native pass
    # costs 1 per 16 items, a Python-level walk 1 per item (review 2 regressions).
    numbers = list(range(65536))
    assert _fuel("sum", numbers) == 257 + 65536
    assert _fuel("min", numbers) == _fuel("max", numbers) == 257 + 4096 + 4096
    assert _fuel("join", "", [""] * 65536) == 1 + 8192
    assert _fuel("split", "," * 65535, ",") == 257 + 4096
    assert _fuel("keys", {str(i): i for i in range(4096)}) == 17 + 256
    assert _fuel("to_json", [[]] * 1000) == 1001 + 1 + 12
    assert _fuel("lower", "x" * 600) == 1 + 3  # ASCII keeps the plain charge
    assert _fuel("lower", "Σ" * 600) == 1 + 3 + 37
    assert _fuel("parse_json", "[" + "0," * 999 + "0]") == 9 + 1 + 1000
    grid = [[1] * 64 for _ in range(64)]
    assert _fuel("grid_equal", grid, grid) == 17 + 256 + 256
    assert _fuel("count_colors", grid) == 17 + 256 + 4096
    assert _fuel("objects", grid) == 17 + 256 + 2 * 4096
    assert _fuel("grid_diff", grid, grid) == 17 + 2 * 256 + 4096


def test_string_charges_do_not_round_down_per_comparison() -> None:
    # Review 2: 4,095-character strings used to compare for free.
    needle, hay = "y" * 4094 + "z", ["y" * 4095] * 4096
    meter = builtins.Meter(10**12)
    assert builtins.contains(needle, hay, meter) is False
    assert meter.used == 17 + 256 + (4095 * 4096 >> 12)
    meter = builtins.Meter(10**12)
    assert builtins.contains("zz", hay, meter) is False  # no str of length 2: no memcmp
    assert meter.used == 17 + 256
    long = ["x" * 4090 + str(i).zfill(5) for i in range(4096)]
    comparisons = 4096 * 12
    chars = 4095 * 4096 * 12
    assert _fuel("sorted", long) == 1 + comparisons // 256 + comparisons // 16 + 256 + (
        chars >> 10
    )
    assert _fuel("max", long) == 17 + 256 + 256 + (4095 * 4096 >> 10)
    # One long string among short ones is not charged per comparison as the longest.
    mixed = ["a" * 131072] + ["b"] * 65535
    assert _fuel("sorted", mixed) < 200_000


def test_extract_json_charges_every_scan() -> None:
    meter = builtins.Meter(10**12)
    assert builtins.extract_json_text("[" * 4096, meter) is None
    assert meter.used >= 16 * 1000  # 16 bracket scans of about 4,096 characters
    meter = builtins.Meter(1000)
    with pytest.raises(builtins.Fatal, match="^sandbox_fuel$"):
        builtins.BUILTIN_FUNCTIONS["extract_json"](meter, ["[" * 131072])
    assert builtins.extract_json_text("[1, {\"a\": \"]\"}] tail") == [1, {"a": "]"}]


def test_fence_scan_finds_the_bodies_the_old_regex_found() -> None:
    # Review 4: the old fence regex backtracked quadratically on an unclosed fence; the
    # linear scan that replaced it must find exactly the same bodies.
    old = re.compile(r"```[A-Za-z]*[ \t]*\r?\n?(.*?)```", re.DOTALL)
    alphabet = ["`", "``", "```", "a", "js", " ", "\t", "\r", "\n", "{", "1", "]"]
    rng = random.Random(9)
    for _ in range(20_000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(24)))
        expected = [match.group(1) for match in old.finditer(text)]
        assert list(builtins._fence_bodies(text)) == expected, repr(text)


def test_extract_json_on_an_unclosed_fence_is_linear() -> None:
    # Review 4: 64,000 characters after an unclosed fence took 8.5 s for 252 fuel.
    for text in ("```" + "a" * 131_069, "```json" + " " * 131_065, "```\n" + "{" * 131_068):
        meter = builtins.Meter(10**9)
        started = time.perf_counter()
        builtins.BUILTIN_FUNCTIONS["extract_json"](meter, [text])
        assert time.perf_counter() - started < wall_limit(1.0)
    assert builtins.extract_json_text('```json  \r\n{"a": 1}```') == {"a": 1}
    assert builtins.extract_json_text("```\n[1]") == [1]  # unclosed: the bare scan


def test_substring_search_pays_for_its_worst_case_compares() -> None:
    # Review 4: find over 2,499 chars with a middle-mismatch needle of 1,250 took
    # 0.173 ms for 11 fuel; the window-times-needle compares are now charged.
    def fuel(name: str, *args: object) -> int:
        meter = builtins.Meter(10**12)
        builtins.BUILTIN_FUNCTIONS[name](meter, list(args))
        return meter.used

    haystack, needle = "a" * 2499, "a" * 624 + "b" + "a" * 625
    compares = (2499 - 1250 + 1) * 1250
    assert fuel("find", haystack, needle) == 11 + (compares >> 10)
    assert fuel("count", haystack, needle) == 11 + (compares >> 10)
    assert fuel("split", haystack, needle) == 11 + (compares >> 10)
    assert fuel("replace", haystack, needle, "") == 11 + ((3 * compares) >> 10)
    # A one-character needle is a linear memchr, and a needle longer than the text
    # is refused at once: neither pays beyond the input charge.
    assert fuel("find", haystack, "b") == 11
    assert fuel("count", "ab", "abc") == 2
    # Long haystacks cap the windows that can be quadratic.
    assert fuel("find", "a" * 131072, "a" * 99) == 513 + ((30_000 * 99) >> 10)
    assert fuel("find", "a" * 131072, "a" * 1000) == 513 + ((4548 * 1000) >> 10)
    # startswith and endswith compare one prefix: no search charge.
    assert fuel("startswith", haystack, needle) == 11


def test_str_in_pays_for_its_worst_case_compares(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    needle = "a" * 624 + "b" + "a" * 625
    source = body(
        "h = task[0]", "n = task[1]", "for i in range(40):", "    x = n in h or n not in h",
        "return [x, 'b' in h]",
    )
    artifact = make_artifact(tmp_path / "a", source, forge=binding(fuel=100_000))
    # Each str ``in`` pays 11 + 1,525: 80 of them exceed 100,000 fuel.
    frame = interpret(monkeypatch, artifact, source, ["a" * 2499, needle])[0]
    assert frame == {"op": "error", "code": "sandbox_fuel", "line": 5}
    frame = interpret(monkeypatch, artifact, source, ["a" * 2499, "aab"])[0]
    assert frame["op"] == "result" and frame["value"] == [True, False], frame


def test_range_length_matches_python_without_overflow() -> None:
    for start in range(-7, 8):
        for stop in range(-7, 8):
            for step in (-3, -2, -1, 1, 2, 3):
                assert builtins.range_length(start, stop, step) == len(range(start, stop, step))
    assert builtins.range_length(-(2**63) + 1, 2**63 - 1, 1) == 2**64 - 2
    assert builtins.range_length(10**30, -(10**30), -(10**29)) == 20
    meter = builtins.Meter(100)
    with pytest.raises(builtins.Fatal, match="^sandbox_value_limit$"):
        builtins.BUILTIN_FUNCTIONS["range"](meter, [-(10**30), 10**30])
    assert len(builtins.BUILTIN_FUNCTIONS["range"](meter, [0, 10**40, 10**35])) == 100_000


def test_digest_is_canonical_and_independent_of_hash_seed() -> None:
    value = {"z": [1, 2.5, None], "a": {"k": "v", "b": True}, "m": "\u00e9"}
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert builtins.digest_value(value) == expected
    script = (
        "import json,sys\n"
        "from architect.runtime._sandbox_builtins import digest_value\n"
        "value = {'z': [1, 2.5, None], 'a': {'k': 'v', 'b': True}, 'm': '\\u00e9'}\n"
        "value.update({str(i): i for i in range(50)})\n"
        "print(digest_value(value))\n"
    )
    outputs = set()
    for seed in ("0", "1", "12345"):
        environment = {**os.environ, "PYTHONHASHSEED": seed}
        outputs.add(subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, env=environment,
            check=True,
        ).stdout.strip())
    assert len(outputs) == 1 and len(outputs.pop()) == 64


# ---------------------------------------------------------------- capabilities (real worker)


class PageAdapter(FakeAdapter):
    def __init__(self, texts: list[str]) -> None:
        super().__init__(responses=[
            ModelResponse(text=text, tokens_in=10, tokens_out=5, cost_usd=0.0, model="fake")
            for text in texts
        ])
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return await super().complete(request)


PAGE = ImageInput.from_bytes(encode_png_gray([bytes([7] * 16) for _ in range(16)], 16, 16))


def page_task() -> dict[str, JsonValue]:
    import base64

    encoded = base64.b64encode(PAGE.data).decode("ascii")
    return {"images": [{"$image": {"media_type": "image/png", "base64": encoded}}]}


async def test_images_and_system_round_trip_through_the_worker(tmp_path: Path) -> None:
    source = body(
        "page = task['images'][0]",
        "a = await context.call('Read.', 32, system='You read pages.', images=[page])",
        "b = await context.call('Text only.', system='S')",
        "c = await context.call('Plain.')",
        "return [a, b, c]",
    )
    artifact = make_artifact(tmp_path / "a", source)
    adapter = PageAdapter(["one", "two", "three"])
    context = RunContext(adapter, Budget(cost_usd=10))
    assert await sandbox_harness(artifact)(context, page_task()) == ["one", "two", "three"]
    first, second, third = adapter.requests
    assert first.prompt == "You read pages.Read." and first.cache_prefix == "You read pages."
    assert [image.sha256 for image in first.images] == [PAGE.sha256]
    assert first.max_output_tokens == 32
    assert second.prompt == "SText only." and not second.images
    assert third.prompt == "Plain." and third.cache_prefix is None
    assert context.images_sent == 1 and context.fuel_used > 0


async def test_an_unattached_image_sha_is_sandbox_invalid_call(tmp_path: Path) -> None:
    stub = {"media_type": "image/png", "sha256": "0" * 64, "width": 1, "height": 1,
            "attached": 1}
    source = body("return await context.call('x', images=[task['page']])")
    artifact = make_artifact(tmp_path / "a", source)
    adapter = PageAdapter(["never"])
    with pytest.raises(SandboxError, match="^sandbox_invalid_call$"):
        await sandbox_harness(artifact)(
            RunContext(adapter, Budget(cost_usd=10)), {"page": {"$image": stub}}
        )
    assert adapter.requests == []


async def test_dynamic_call_cap_counts_model_calls(tmp_path: Path) -> None:
    loop = body("i = 0", "while i < task:", "    await context.call('x')", "    i += 1",
                "return i")
    artifact = make_artifact(tmp_path / "a", loop, forge=binding(call_limit=128))
    adapter = FakeAdapter()
    assert await sandbox_harness(artifact)(RunContext(adapter, Budget(cost_usd=10)), 128) == 128
    adapter = FakeAdapter()
    with pytest.raises(SandboxError, match="^sandbox_invalid_call$"):
        await sandbox_harness(artifact)(RunContext(adapter, Budget(cost_usd=10)), 129)
    assert adapter.calls == 128


async def test_observe_is_free_step_counts_and_refused_steps_do_not(tmp_path: Path) -> None:
    source = body(
        "i = 0",
        "while i < 10:",
        "    o = await context.observe()",
        "    i += 1",
        "n = 0",
        "while n < task:",
        "    o = await context.step({'name': 'ACTION1'})",
        "    n += 1",
        "return o",
    )
    artifact = game_artifact(tmp_path / "g", source, max_turns=2, calls_per_turn=2)
    assert call_limit(artifact) == 4
    context = TurnContext(actions_left=8)
    observation = await sandbox_harness(artifact)(context, 4)
    assert context.observes == 10 and len(context.steps) == 4
    assert isinstance(observation, dict) and observation["actions_taken"] == 4
    with pytest.raises(SandboxError, match="^sandbox_invalid_call$"):
        await sandbox_harness(artifact)(TurnContext(actions_left=8), 5)
    refused = TurnContext(actions_left=1)
    last = await sandbox_harness(artifact)(refused, 12)
    assert len(refused.steps) == 1 and isinstance(last, dict) and last["refused"] is True


async def test_step_arguments_and_harness_calls(tmp_path: Path) -> None:
    source = body("return await context.step('ACTION1')")
    artifact = game_artifact(tmp_path / "g", source)
    with pytest.raises(SandboxError, match="^sandbox_invalid_call$") as raised:
        await sandbox_harness(artifact)(TurnContext(), None)
    assert raised.value.line == 2
    child = make_artifact(
        tmp_path / "c", body("return await context.harness('child', {'q': task})"),
        forge=binding(children=("child",), call_limit=1),
    )
    context = TurnContext()
    assert await sandbox_harness(child)(context, 5) == {"echo": {"q": 5}}
    assert context.children == [("child", {"q": 5})]


async def test_runtime_error_frames_carry_code_and_line_only(tmp_path: Path) -> None:
    source = body("x = 1", "y = task['secret-key-name']", "return y")
    artifact = make_artifact(tmp_path / "a", source)
    with pytest.raises(SandboxError, match="^sandbox_key$") as raised:
        await sandbox_harness(artifact)(RunContext(FakeAdapter(), Budget()), {})
    assert raised.value.line == 3
    assert "secret" not in str(raised.value)


async def test_fuel_exhaustion_in_the_worker_is_bounded(tmp_path: Path) -> None:
    artifact = make_artifact(
        tmp_path / "a", body("while True:", "    pass"), forge=binding(fuel=1_000_000)
    )
    context = RunContext(FakeAdapter(), Budget(compute_s=60))
    started = time.perf_counter()
    with pytest.raises(SandboxError, match="^sandbox_fuel$"):
        await sandbox_harness(artifact)(context, None)
    assert time.perf_counter() - started < wall_limit(20)
    assert context.fuel_used == 1_000_000


async def test_building_a_list_of_large_strings_is_sandbox_memory(tmp_path: Path) -> None:
    source = body(
        "big = 'x'",
        "while len(big) < 60000:",
        "    big = big + big",
        "out = []",
        "i = 0",
        "while True:",
        "    append(out, big + str(i))",
        "    i += 1",
    )
    artifact = make_artifact(tmp_path / "a", source, forge=binding(fuel=100_000_000))
    with pytest.raises(SandboxError, match="^sandbox_memory$"):
        await sandbox_harness(artifact)(RunContext(FakeAdapter(), Budget(compute_s=120)), None)


# ---------------------------------------------------------------- LANGUAGE.md and tables


def test_language_markdown_matches_the_golden_file() -> None:
    golden = FIXTURES / "phase9" / "LANGUAGE.md"
    text = language.language_markdown()
    assert text == golden.read_text(encoding="utf-8")
    assert text == language.language_markdown()
    assert text.isascii() and max(len(line) for line in text.splitlines()) <= 200


def test_language_tables_are_complete_and_consistent() -> None:
    assert set(builtins.BUILTIN_FUNCTIONS) == set(language.BUILTINS) == set(language.ARITY)
    assert language.RUNTIME_ERROR_CODES == language.DATA_ERROR_CODES | {
        "sandbox_unbound", "sandbox_fuel", "sandbox_memory", "sandbox_value_limit",
        "sandbox_invalid_call",
    }
    static = {
        "sandbox_recursion", "sandbox_loop_depth", "sandbox_helper", "sandbox_constant",
        "sandbox_language", "sandbox_statement", "sandbox_expression", "sandbox_assignment",
        "sandbox_capability", "sandbox_binding_mismatch", "sandbox_ast_limit", "sandbox_call",
        "sandbox_comparison", "sandbox_dictionary", "sandbox_entrypoint", "sandbox_signature",
        "sandbox_import",
    }
    assert static | language.RUNTIME_ERROR_CODES <= set(language.HINTS)
    for code, hint in language.HINTS.items():
        assert hint.isascii() and 0 < len(hint) <= 160, code
    for name, _, code, _ in REJECTED:
        assert code in language.HINTS, name
    assert {spec.name for spec in language.BUILTINS.values() if spec.mutating} == {
        "append", "extend", "insert", "pop", "set_key"
    }
    assert {name for name, spec in language.CAPABILITIES.items() if spec.game_only} == {
        "observe", "step"
    }
    assert language.NODE_LIMIT_V2 == 8000 and language.LANGUAGE_VERSION == 2
    assert language.DEFAULT_FUEL == 10_000_000 and language.MAX_FUEL == 100_000_000


# ---------------------------------------------------------------- edge coverage


MORE_REJECTED = [
    ("await-non-call", body("return await task"), "sandbox_capability"),
    ("builtin-starred", body("return len(*task)"), "sandbox_call"),
    ("async-helper-arity", "async def f(context, x):\n    return x\n"
     + body("return await f(context)"), "sandbox_call"),
    ("async-helper-keyword", "async def f(context, x):\n    return x\n"
     + body("return await f(context, x=1)"), "sandbox_call"),
    ("observe-args", body("return await context.observe(1)"), "sandbox_capability"),
    ("constant-infinite", "C = 1e999\n" + body("return C"), "sandbox_constant"),
    ("constant-int-key", "C = {1: 2}\n" + body("return C"), "sandbox_constant"),
    ("constant-negated-string", "C = -'x'\n" + body("return C"), "sandbox_constant"),
    ("literal-too-big", body("return 9223372036854775808"), "sandbox_expression"),
    ("literal-bytes", body("return b'x'"), "sandbox_expression"),
    ("syntax", body("return ("), "sandbox_invalid_source"),
    ("source-limit", "#" * 65537 + "\n" + body("return 1"), "sandbox_source_limit"),
    ("preset-literal", body("n = 'x'",
                            "return await execute_preset(n, {}, (), {}, context, task)"),
     "sandbox_literal_required"),
    ("preset-shape", body("return await execute_preset('x', context, task)"),
     "sandbox_preset_call"),
    ("run-helper", "async def run(context, task):\n    return 1\n" + body("return 2"),
     "sandbox_entrypoint"),
    ("import-alias", "from pydantic import JsonValue as J\n" + body("return 1"),
     "sandbox_import"),
    ("empty", "", "sandbox_entrypoint"),
]


@pytest.mark.parametrize(
    ("source", "code"),
    [pytest.param(source, code, id=name) for name, source, code in MORE_REJECTED],
)
def test_more_v2_rejections(tmp_path: Path, source: str, code: str) -> None:
    artifact = make_artifact(tmp_path / "a")
    with pytest.raises(SandboxError, match=f"^{code}$"):
        validate_source(source, artifact)
    assert code in language.HINTS


def test_syntax_errors_carry_their_line(tmp_path: Path) -> None:
    with pytest.raises(SandboxError, match="^sandbox_invalid_source$") as raised:
        validate_source(body("x = 1", "return (", ""), make_artifact(tmp_path / "a"))
    assert raised.value.line is not None and raised.value.line >= 3


@pytest.mark.parametrize(
    "source",
    [
        body("k = 'a'", "return {k: 1}"),
        body("x = {}", "x['a'] = 1", "return x"),
        body("return split(task, ',')"),
        body("return (1, 2)"),
        body("return task if task else 1"),
    ],
)
def test_more_v2_constructs_under_v1_are_sandbox_language(v1: HarnessArtifact,
                                                          source: str) -> None:
    with pytest.raises(SandboxError, match="^sandbox_language$"):
        validate_source(source, v1)


def test_program_and_tree_dispatch_by_binding(v1: HarnessArtifact, v2: HarnessArtifact) -> None:
    from architect.runtime.sandbox import _validated_program

    assert _validated_tree(body("return 1"), v2).name == "run"
    with pytest.raises(SandboxError, match="^sandbox_language$"):
        _validated_program(body("return 1"), v1)


def test_in_process_capability_messages(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stub = {"$image": {"media_type": "image/png", "sha256": "e" * 64, "width": 2, "height": 3,
                       "attached": 1}}
    source = body(
        "o = await context.observe()",
        "s = await context.step({'name': 'ACTION6', 'x': 1, 'y': 2})",
        "r = await context.call('p', 5, system='sys', images=[task['page']])",
        "q = await context.call('p', max_output_tokens=7, images=[])",
        "return [o, s, r, q]",
    )
    artifact = game_artifact(tmp_path / "g", source)
    frame, sent = interpret(monkeypatch, artifact, source, {"page": stub},
                            replies=({"frame": 1}, {"frame": 2}, "r", "q"))
    assert result(frame) == [{"frame": 1}, {"frame": 2}, "r", "q"]
    assert sent == [
        {"op": "observe"},
        {"op": "step", "action": {"name": "ACTION6", "x": 1, "y": 2}},
        {"op": "call", "prompt": "p", "tokens": 5, "system": "sys", "images": ["e" * 64]},
        {"op": "call", "prompt": "p", "tokens": 7},
    ]
    child = make_artifact(tmp_path / "c", forge=binding(children=("child",)))
    frame, sent = interpret(monkeypatch, child, body("return await context.harness('child', 3)"),
                            replies=({"out": 1},))
    assert result(frame) == {"out": 1}
    assert sent == [{"op": "harness", "name": "child", "input": 3}]
    source = body("r = range(2)", "return await context.harness('child', r)")
    assert failure(run_source(monkeypatch, child, source)) == ("sandbox_invalid_call", 3)


@pytest.mark.parametrize(
    ("lines", "code"),
    [
        (("return await context.step({'r': range(2)})",), "sandbox_type"),
        (("r = range(2)", "return await context.step(r)"), "sandbox_invalid_call"),
        (("return await context.call('x', images=[{'$image': {'sha256': 1}}])",),
         "sandbox_invalid_call"),
        (("return await context.call('x', images=[{'$image': {}, 'x': 1}])",),
         "sandbox_invalid_call"),
        (("return await context.call('x', system='s' * 2)",), "sandbox_type"),
    ],
)
def test_invalid_capability_arguments_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lines: tuple[str, ...], code: str
) -> None:
    artifact = game_artifact(tmp_path / "g", body(*lines))
    assert failure(run_source(monkeypatch, artifact, body(*lines)))[0] == code


def test_a_host_reply_of_the_wrong_shape_fails_the_worker(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact
) -> None:
    # v2 capability RPCs use the byte-level pipe, so their bytes can be metered.
    monkeypatch.setattr(worker, "_write", lambda data: None)
    monkeypatch.setattr(worker, "_read", lambda: _encode({"value": 1}))
    with pytest.raises(SandboxError, match="^sandbox_invalid_message$"):
        worker._run_v2(body("return await context.call('x')"), v2, None)
    monkeypatch.setattr(worker, "_read", lambda: _encode({"other": 1}))
    game = game_artifact(v2.directory.parent / "g", body("return 1"))
    with pytest.raises(SandboxError, match="^sandbox_invalid_message$"):
        worker._run_v2(body("return await context.observe()"), game, None)


def test_v2_presets_run_in_worker_or_through_the_host(monkeypatch: pytest.MonkeyPatch,
                                                      tmp_path: Path) -> None:
    text = make_artifact(tmp_path / "t")
    emitted = (tmp_path / "t" / "harness.py").read_text(encoding="utf-8")
    call = emitted[emitted.index("await execute_preset(") :].split("\n")[0].strip()
    source = body(f"out = {call}", "return [out]")
    frame, sent = interpret(monkeypatch, text, source, {"input": "x"}, replies=("summary",))
    assert result(frame) == ["summary"] and [message["op"] for message in sent] == ["call"]
    game = make_artifact(tmp_path / "g", spec=game_spec(), preset_name="explorer")
    emitted = (tmp_path / "g" / "harness.py").read_text(encoding="utf-8")
    call = emitted[emitted.index("await execute_preset(") :].split("\n")[0].strip()
    frame, sent = interpret(monkeypatch, game, body(f"return {call}"), {"game": 1},
                            replies=({"score": 1},))
    assert result(frame) == {"score": 1}
    assert sent == [{"op": "preset", "task": {"game": 1}}]


@pytest.mark.parametrize(
    ("error", "code"), [(MemoryError, "sandbox_memory"), (RecursionError, "sandbox_value_limit")]
)
def test_worker_maps_interpreter_exhaustion_to_fixed_codes(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact, error: type[BaseException], code: str
) -> None:
    def explode(self: object, task: object) -> object:
        raise error

    monkeypatch.setattr(worker._InterpreterV2, "execute", explode)
    assert failure(run_source(monkeypatch, v2, body("return 1")))[0] == code


@pytest.mark.parametrize(
    ("error", "code"), [(MemoryError, "sandbox_memory"), (RecursionError, "sandbox_value_limit")]
)
def test_exhaustion_inside_statements_keeps_its_line(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact, error: type[BaseException], code: str
) -> None:
    original = builtins.BUILTIN_FUNCTIONS["len"]

    def exhausted(meter: builtins.Meter, args: list[object]) -> object:
        raise error

    functions = dict(builtins.BUILTIN_FUNCTIONS)
    functions["len"] = exhausted
    monkeypatch.setattr(worker, "BUILTIN_FUNCTIONS", functions)
    frame = run_source(monkeypatch, v2, body("l = []", "n = len(l)"))
    assert failure(frame) == (code, 3)
    assert builtins.BUILTIN_FUNCTIONS["len"] is original


async def test_worker_main_runs_language_2_on_its_own_thread(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact
) -> None:
    fields = {"spec", "preset", "params", "components", "model", "forge", "execution_mode"}
    initial = {
        "source": body("n = 0", "for i in range(5):", "    n += i", "return n"),
        "binding": v2.model_dump(mode="json", include=fields),
        "task": None,
        "language": 2,
    }
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(worker, "_receive", lambda: initial)
    monkeypatch.setattr(worker, "_send", sent.append)
    await worker._main()
    assert sent == [{"op": "result", "value": 10, "fuel": sent[0]["fuel"]}]
    initial["source"] = body("return missing")
    with pytest.raises(SandboxError, match="^sandbox_expression$"):
        await worker._main()


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (body("for k in {'a': 1, 'b': 2}:", "    if k == 'b':", "        return k"), "b"),
        (body("for k in {'a': 1}:", "    break", "return k"), "a"),
        (body("while True:", "    return 'w'"), "w"),
        (body("n = 0", "while n < 5:", "    n += 1", "    if n == 3:", "        break",
              "return n"), 3),
        (body("return [1 != 2, 2 >= 2, 1 >= 2, 'b' > 'a']"), [True, True, False, True]),
        (body("return sorted([1, 3, 2], reverse=True)"), [3, 2, 1]),
        ("def nothing(x):\n    pass\n" + body("return nothing(1)"), None),
    ],
)
def test_more_control_flow(monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact, source: str,
                           expected: JsonValue) -> None:
    assert result(run_source(monkeypatch, v2, source)) == expected


def test_range_truthiness_is_a_type_error(monkeypatch: pytest.MonkeyPatch,
                                          v2: HarnessArtifact) -> None:
    assert failure(run_source(monkeypatch, v2, body("return [not range(1)]")))[0] == "sandbox_type"


@pytest.mark.parametrize(
    ("name", "args", "code"),
    [
        ("len", [5], "sandbox_type"),
        ("int", ["x"], "sandbox_value"),
        ("int", ["9" * 500], "sandbox_value"),
        ("int", [float("inf")], "sandbox_value"),
        ("int", [[1]], "sandbox_type"),
        ("float", ["nan"], "sandbox_value"),
        ("float", ["1e999"], "sandbox_value"),
        ("bool", ["maybe"], "sandbox_value"),
        ("bool", [builtins.RangeValue(0, 1, 1)], "sandbox_type"),
        ("abs", ["x"], "sandbox_type"),
        ("round", ["x"], "sandbox_type"),
        ("round", [1.5, 16], "sandbox_value"),
        ("round", [1.5, "x"], "sandbox_type"),
        ("min", [[1, "a"]], "sandbox_type"),
        ("max", [[]], "sandbox_value"),
        ("max", ["x"], "sandbox_type"),
        ("sum", [["a"]], "sandbox_type"),
        ("sorted", [[1], "yes"], "sandbox_type"),
        ("range", ["a"], "sandbox_type"),
        ("enumerate", ["x"], "sandbox_type"),
        ("zip", [[1], "x"], "sandbox_type"),
        ("list", [5], "sandbox_type"),
        ("list", ["x" * 70000], "sandbox_value_limit"),
        ("list", [builtins.RangeValue(0, 70000, 1)], "sandbox_value_limit"),
        ("kind", [builtins.RangeValue(0, 1, 1)], "sandbox_type"),
        ("keys", [[1]], "sandbox_type"),
        ("get", [{}, 1, 0], "sandbox_type"),
        ("at", [5, 0, 0], "sandbox_type"),
        ("at", [[1], "0", 0], "sandbox_type"),
        ("append", ["x", 1], "sandbox_type"),
        ("append", [[0] * 65536, 1], "sandbox_value_limit"),
        ("extend", [[], "x"], "sandbox_type"),
        ("extend", [[0] * 65536, [1]], "sandbox_value_limit"),
        ("insert", [[1], "0", 2], "sandbox_type"),
        ("insert", [[0] * 65536, 0, 1], "sandbox_value_limit"),
        ("set_key", [{}, 1, 1], "sandbox_dictionary"),
        ("set_key", [{str(i): i for i in range(65536)}, "new", 1], "sandbox_value_limit"),
        ("split", ["a", ""], "sandbox_value"),
        ("split", [1, ","], "sandbox_type"),
        ("split", ["," * 70000, ","], "sandbox_value_limit"),
        ("join", [",", [1]], "sandbox_type"),
        ("join", ["x" * 1000, ["y"] * 200], "sandbox_value_limit"),
        ("count", ["a", ""], "sandbox_value"),
        ("find", ["a", 1], "sandbox_type"),
        ("replace", ["a", "", "b"], "sandbox_value"),
        ("replace", ["a" * 1000, "a", "b" * 1000], "sandbox_value_limit"),
        ("upper", ["ß" * 70000], "sandbox_value_limit"),
        ("splitlines", ["\n" * 70000], "sandbox_value_limit"),
        ("parse_json", [1], "sandbox_type"),
        ("to_json", [["x" * 1000] * 200], "sandbox_value_limit"),
        ("to_json", [builtins.RangeValue(0, 1, 1)], "sandbox_type"),
        ("str", [[{"a": object()}]], "sandbox_type"),
        ("grid_text", [[[1, 2], [3]]], "sandbox_type"),
        ("grid_diff", [[[1]], [[1]], 5000], "sandbox_value"),
        ("objects", [[[1]], 16], "sandbox_value"),
        ("find_cells", [[[1]], 16], "sandbox_value"),
        ("crop", [[[1]], "a", 0, 0, 0], "sandbox_type"),
    ],
)
def test_builtin_errors(name: str, args: list[object], code: str) -> None:
    with pytest.raises(builtins.SandboxFault, match=f"^{code}$") as raised:
        builtins.BUILTIN_FUNCTIONS[name](builtins.Meter(10_000_000), args)
    expected = builtins.DataError if code in language.DATA_ERROR_CODES else builtins.Fatal
    assert isinstance(raised.value, expected)
    assert type(builtins.fault(code)) is expected


def test_builtin_value_edges() -> None:
    meter = builtins.Meter(10_000_000)
    call = builtins.BUILTIN_FUNCTIONS
    assert call["pop"](meter, [[]]) is None
    assert call["parse_json"](meter, ["[1.5, -2]"]) == [1.5, -2]
    assert call["parse_json"](meter, ["x" * (128 * 1024 + 1)]) is None
    assert call["extract_json"](meter, ["x" * (128 * 1024 + 1)]) is None
    assert call["extract_json"](meter, ["```json\n5\n```"]) is None
    assert call["extract_json"](meter, ['```\nhere {"a": 1}\n```']) == {"a": 1}
    assert call["extract_json"](meter, ['{"a": "\\"}"} tail']) == {"a": '"}'}
    assert call["extract_json"](meter, ["{] then [1]"]) == [1]
    assert call["extract_json"](meter, ["{ never closed"]) is None
    assert call["extract_json"](meter, ["{x} " * 20 + "[1]"]) is None  # bounded attempts
    assert call["digest"](meter, [{"a": 1}]) == builtins.digest_value({"a": 1})
    assert call["str"](meter, ["x"]) == "x"
    assert call["float"](meter, [True]) == 1.0 and call["int"](meter, [2.0]) == 2
    deep: object = 1
    for _ in range(45):
        deep = [deep]
    with pytest.raises(builtins.Fatal, match="^sandbox_value_limit$"):
        builtins.store(deep, meter)
    with pytest.raises(builtins.Fatal, match="^sandbox_value_limit$"):
        builtins.check_json(deep)
    with pytest.raises(builtins.Fatal, match="^sandbox_value_limit$"):
        builtins.store({"k": deep}, meter)
    with pytest.raises(builtins.DataError, match="^sandbox_type$"):
        builtins.store(builtins.RangeValue(0, 1, 1), meter)


@pytest.mark.parametrize("mode", ["schema", "first"])
async def test_a_bound_preset_asks_for_its_reply_schema_through_the_worker(
    tmp_path: Path, mode: str
) -> None:
    """Phase 14 review: under ``reply_json`` "schema" a preset bound in a sandboxed
    harness reaches the provider with the answer's schema and the self-check's, as it
    does in process; under "first" no call is constrained."""
    from architect.presets._execution import _VERDICT

    output: dict[str, JsonValue] = {
        "type": "object",
        "required": ["total"],
        "properties": {"total": {"type": "integer"}},
        "additionalProperties": False,
    }
    spec = Spec(
        task="Read the total",
        input_schema={"type": "object"},
        output_schema=output,
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="private-set"),
        budget=Budget(),
        reply_json=mode,  # type: ignore[arg-type]
    )
    artifact = make_artifact(tmp_path / mode, spec=spec, preset_name="extractor")
    replies = ('```json\n{"total": 7}\n```', '{"passed": true, "reason": "printed"}')
    adapter = FakeAdapter(responses=[
        ModelResponse(text=text, tokens_in=1, tokens_out=1, cost_usd=0, model="fake")
        for text in replies
    ])
    context = RunContext(adapter, Budget())
    assert await sandbox_harness(artifact)(context, {"receipt": "TOTAL 7"}) == {"total": 7}
    sent = [shape.response_schema_sha256 for shape in adapter.seen]
    if mode == "schema":
        assert sent == [
            ModelRequest(prompt="x", response_schema=schema).response_schema_sha256()
            for schema in (output, _VERDICT)
        ]
    else:
        assert sent == [None, None]


# ---------------------------------------------------------------- Forge pack v2 additions


ANNOTATED = (
    "LIMIT: int = 2\n"
    "def scale(x: int, by: int = LIMIT) -> int:\n"
    "    total: int = x * by\n"
    "    return total\n"
    "async def ask(context: RunContext, prompt: str) -> str:\n"
    "    return await context.call(prompt, 8)\n"
    "async def run(context: RunContext, task: dict[str, int]) -> list[int] | None:\n"
    "    n: int = scale(get(task, 'n', 0))\n"
    "    reply: str = await ask(context, str(n))\n"
    "    return [n, len(reply)]\n"
)
PLAIN = (
    "LIMIT = 2\n"
    "def scale(x, by=LIMIT):\n"
    "    total = x * by\n"
    "    return total\n"
    "async def ask(context, prompt):\n"
    "    return await context.call(prompt, 8)\n"
    "async def run(context, task):\n"
    "    n = scale(get(task, 'n', 0))\n"
    "    reply = await ask(context, str(n))\n"
    "    return [n, len(reply)]\n"
)


def test_annotations_are_ignored_and_cost_no_fuel(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact
) -> None:
    annotated, sent_annotated = interpret(monkeypatch, v2, ANNOTATED, {"n": 3}, ("abc",))
    plain, sent_plain = interpret(monkeypatch, v2, PLAIN, {"n": 3}, ("abc",))
    assert result(annotated) == result(plain) == [6, 3]
    # The same work: an annotation is never visited, so it costs nothing.
    assert annotated["fuel"] == plain["fuel"]
    assert sent_annotated == sent_plain == [{"op": "call", "prompt": "6", "tokens": 8}]
    stats = validate_source(ANNOTATED, v2)
    assert (stats.helpers, stats.constants, stats.await_sites) == (2, 1, 2)


@pytest.mark.parametrize(
    "annotation",
    [
        "Missing",  # never looked up: no unbound-name error
        "os.environ",  # a dotted name is only a name here
        "'Forward[Ref]'",
        "NotDefined[int, 'x']",
        "typing.Callable[[int], None] | None",
        "Literal[-1, True, None, ...]",
        "task['k']",  # shaped like a type, so accepted, and still never read
    ],
)
def test_annotation_expressions_are_never_evaluated(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact, annotation: str
) -> None:
    source = (
        f"def echo(x: {annotation}) -> {annotation}:\n"
        f"    y: {annotation} = x\n"
        "    return y\n"
        f"async def run(context: {annotation}, task: {annotation}) -> {annotation}:\n"
        f"    value: {annotation} = echo(task)\n"
        "    return value\n"
    )
    plain = "def echo(x):\n    y = x\n    return y\n" + body("value = echo(task)", "return value")
    frame, sent = interpret(monkeypatch, v2, source, {"k": 1})
    reference, _ = interpret(monkeypatch, v2, plain, {"k": 1})
    assert result(frame) == {"k": 1} and sent == []
    assert frame["fuel"] == reference["fuel"]


@pytest.mark.parametrize(
    "source",
    [
        # Evaluation hidden in an annotation is refused before anything runs.
        "def f(x: print(task)) -> int:\n    return x\n" + body("return f(1)"),
        "def f(x) -> __import__('os').system('id'):\n    return x\n" + body("return f(1)"),
        "async def f(context, x: await context.call('leak')):\n    return x\n"
        + body("return await f(context, 1)"),
        "async def f(context, x) -> (await context.harness('child', 1)):\n    return x\n"
        + body("return await f(context, 1)"),
        body("x: [y for y in task] = 1", "return x"),
        body("x: (lambda: task)() = 1", "return x"),
        body("x: f'{task}' = 1", "return x"),
        "C: len('x') = 1\n" + body("return C"),
        "async def run(context, task: getattr(task, 'x')):\n    return 1\n",
    ],
)
def test_annotations_cannot_smuggle_evaluation(v2: HarnessArtifact, source: str) -> None:
    with pytest.raises(SandboxError, match="^sandbox_expression$") as raised:
        validate_source(source, v2)
    assert raised.value.rule == "annotation_form"


def test_default_values_are_still_evaluated_normally(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact
) -> None:
    source = (
        "TABLE = {'seen': [1]}\n"
        "LIMIT = 4\n"
        "def pick(x: str, n: int = LIMIT, table: dict = TABLE, acc: list = []) -> list:\n"
        "    append(acc, x)\n"
        "    seen = table['seen']\n"
        "    append(seen, n)\n"
        "    return [n, table, acc]\n"
        + body("first = pick('a')", "second = pick('b', 7)", "return [first, second, TABLE]")
    )
    frame, _ = interpret(monkeypatch, v2, source)
    # Each call gets its own copy of a default, as a constant read does; the constant
    # itself never changes and a list default never carries over between calls.
    assert result(frame) == [
        [4, {"seen": [1, 4]}, ["a"]],
        [7, {"seen": [1, 7]}, ["b"]],
        {"seen": [1]},
    ]
    # A default is validated like any value: only a literal or a constant.
    for default in ("len('x')", "task", "missing", "[x for x in 'ab']", "LIMIT + 1"):
        bad = f"LIMIT = 4\ndef f(x={default}):\n    return x\n" + body("return f()")
        with pytest.raises(SandboxError, match="^sandbox_helper$") as raised:
            validate_source(bad, v2)
        assert raised.value.rule == "helper_default_value", default
    # The defaults copied per call are paid for like any copy.
    cheap = "def f(x=1):\n    return x\n" + body("return f()")
    costly = "def f(x=[1, 2, 3, 4, 5, 6, 7, 8]):\n    return 1\n" + body("return f()")
    assert run_source(monkeypatch, v2, costly)["fuel"] > run_source(monkeypatch, v2, cheap)["fuel"]


def test_async_helpers_take_defaults_after_context(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact
) -> None:
    source = (
        "async def ask(context, prompt, tokens=8, system='s'):\n"
        "    return await context.call(prompt, tokens, system=system)\n"
        + body("a = await ask(context, 'x')", "b = await ask(context, 'y', 4)", "return [a, b]")
    )
    frame, sent = interpret(monkeypatch, v2, source, replies=("A", "B"))
    assert result(frame) == ["A", "B"]
    assert sent == [
        {"op": "call", "prompt": "x", "tokens": 8, "system": "s"},
        {"op": "call", "prompt": "y", "tokens": 4, "system": "s"},
    ]


@pytest.mark.parametrize(
    ("method", "builtin", "setup"),
    [
        ("s.split(',')", "split(s, ',')", "s = 'a,b'"),
        ("sep.join(parts)", "join(sep, parts)", "sep = '-'; parts = ['a', 'b']"),
        ("s.strip()", "strip(s)", "s = ' a '"),
        ("s.lower()", "lower(s)", "s = 'Ab'"),
        ("s.upper()", "upper(s)", "s = 'Ab'"),
        ("s.startswith('a')", "startswith(s, 'a')", "s = 'ab'"),
        ("s.endswith('b')", "endswith(s, 'b')", "s = 'ab'"),
        ("s.replace('a', 'c')", "replace(s, 'a', 'c')", "s = 'aba'"),
        ("s.find('b')", "find(s, 'b')", "s = 'ab'"),
        ("s.count('a')", "count(s, 'a')", "s = 'aba'"),
        ("s.splitlines()", "splitlines(s)", "s = 'a\\nb'"),
        ("d.get('k')", "get(d, 'k')", "d = {'k': [1]}"),
        ("d.get('z', 0)", "get(d, 'z', 0)", "d = {'k': [1]}"),
        ("d.keys()", "keys(d)", "d = {'k': [1]}"),
        ("d.values()", "values(d)", "d = {'k': [1]}"),
        ("d.items()", "items(d)", "d = {'k': [1]}"),
        ("[l.append(3), l][1]", "[append(l, 3), l][1]", "l = [1, 2]"),
        ("[l.extend([3]), l][1]", "[extend(l, [3]), l][1]", "l = [1, 2]"),
        ("[l.insert(0, 3), l][1]", "[insert(l, 0, 3), l][1]", "l = [1, 2]"),
        ("[l.pop(), l]", "[pop(l), l]", "l = [1, 2]"),
    ],
)
def test_method_spellings_are_exactly_the_builtins(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact, method: str, builtin: str, setup: str
) -> None:
    lines = [part.strip() for part in setup.split(";")]
    spelled = run_source(monkeypatch, v2, body(*lines, f"return {method}"))
    plain = run_source(monkeypatch, v2, body(*lines, f"return {builtin}"))
    assert result(spelled) == result(plain)
    assert spelled["fuel"] == plain["fuel"]  # nothing is looked up on a value


def test_every_listed_method_is_a_builtin_with_a_receiver() -> None:
    assert language.METHODS <= set(language.BUILTINS)
    assert all(language.ARITY[name][1] >= 1 for name in language.METHODS)
    # The mutating spellings keep their rule: the receiver is a local variable.
    assert language.MUTATING & language.METHODS == {"append", "extend", "insert", "pop"}


def test_method_errors_are_the_builtins_errors(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact
) -> None:
    for expression, code in (
        ("[1].lower()", "sandbox_type"),
        ("'a'.append(1)", "sandbox_assignment"),
        ("(1).split(',')", "sandbox_type"),
        ("{'k': 1}.get(1)", "sandbox_type"),
        ("[1, 2].count(1)", "sandbox_type"),
    ):
        source = body(f"return {expression}")
        try:
            validate_source(source, v2)
        except SandboxError as exc:
            assert exc.code == code, expression
            continue
        assert failure(run_source(monkeypatch, v2, source))[0] == code, expression


@pytest.mark.parametrize(
    ("value", "kinds", "expected"),
    [
        ("{}", "dict", True),
        ("[]", "list", True),
        ("'x'", "str", True),
        ("1", "int", True),
        ("True", "int", True),  # as in Python, a bool is an int
        ("True", "bool", True),
        ("1", "bool", False),
        ("1.0", "int", False),
        ("1.0", "(int, float)", True),
        ("None", "(str, int, float, bool, list, dict)", False),
        ("range(2)", "[list]", False),
        ("'x'", "[list, dict]", False),
    ],
)
def test_isinstance_follows_python(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact, value: str, kinds: str, expected: bool
) -> None:
    frame = run_source(monkeypatch, v2, body(f"return isinstance({value}, {kinds})"))
    assert result(frame) is expected


def test_a_method_spelling_works_on_an_awaited_reply(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact
) -> None:
    source = body(
        "reply = (await context.call('x')).strip().lower()",
        "return [reply.split(','), isinstance(reply, str)]",
    )
    frame, sent = interpret(monkeypatch, v2, source, replies=("  A,B  ",))
    assert result(frame) == [["a", "b"], True]
    assert sent == [{"op": "call", "prompt": "x", "tokens": None}]


def test_get_and_at_default_to_none(monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact) -> None:
    source = body(
        "d = {'k': [1]}",
        "found = get(d, 'k')",
        "append(found, 2)",  # a found container is still the dict's own, as before
        "return [get(d, 'z'), at([1], 3), at('ab', -1), d.get('missing'), d]",
    )
    assert result(run_source(monkeypatch, v2, source)) == [None, None, "b", None, {"k": [1, 2]}]


def test_variables_may_reuse_builtin_names(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact
) -> None:
    source = (
        "def tally(items, count):\n"
        "    for item in items:\n"
        "        count += 1\n"
        "    return [count, count('aa', 'a')]\n"
        + body("values = ['x', 'y']", "max = tally(values, 0)", "return [max, values]")
    )
    assert result(run_source(monkeypatch, v2, source)) == [[2, 2], ["x", "y"]]


def test_preset_binding_may_be_held_in_constants(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = make_artifact(tmp_path / "t")
    emitted = (tmp_path / "t" / "harness.py").read_text(encoding="utf-8")
    call = ast.parse(emitted[emitted.index("await execute_preset(") :].split("\n")[0].strip())
    arguments = cast(ast.Call, cast(ast.Await, cast(ast.Expr, call.body[0]).value).value).args
    name, params, components, spec = (ast.unparse(item) for item in arguments[:4])
    constants = (
        f"NAME = {name}\nPARAMS = {params}\nCOMPONENTS = {components}\nSPEC: dict = {spec}\n"
    )
    source = constants + body(
        "out = await execute_preset(NAME, PARAMS, COMPONENTS, SPEC, context, task)",
        "return [out]",
    )
    frame, sent = interpret(monkeypatch, artifact, source, {"input": "x"}, replies=("summary",))
    assert result(frame) == ["summary"] and [message["op"] for message in sent] == ["call"]
    # The binding check still compares every value exactly, and names the key that differs.
    moved = constants.replace("'summarize'", "'summarize again'", 1)
    assert moved != constants
    with pytest.raises(SandboxError, match="^sandbox_binding_mismatch$") as raised:
        validate_source(moved + body(
            "return await execute_preset(NAME, PARAMS, COMPONENTS, SPEC, context, task)"
        ), artifact)
    assert (raised.value.rule, raised.value.name) == ("binding_spec", "task")
    # A variable is not a literal.
    local = constants + body(
        "spec = SPEC", "return await execute_preset(NAME, PARAMS, COMPONENTS, spec, context, task)"
    )
    with pytest.raises(SandboxError, match="^sandbox_literal_required$") as raised:
        validate_source(local, artifact)
    assert raised.value.rule == "preset_literal"


def test_a_binding_key_the_source_added_is_never_named(tmp_path: Path) -> None:
    artifact = make_artifact(tmp_path / "t")
    emitted = (tmp_path / "t" / "harness.py").read_text(encoding="utf-8")
    line = emitted[emitted.index("await execute_preset(") :].split("\n")[0].strip()
    extra = line.replace("{'task':", "{'agent_secret_key': 1, 'task':", 1)
    assert extra != line
    with pytest.raises(SandboxError, match="^sandbox_binding_mismatch$") as raised:
        validate_source(body(f"return {extra}"), artifact)
    assert raised.value.rule == "binding_spec" and raised.value.name is None


@pytest.mark.parametrize(
    ("source", "rule"),
    [
        ("async def run(context, task):\\n    return 1\\n", "syntax_newlines"),
        ("async def run(context, task):\n    return 'x\n", "syntax_string"),
        ("async def run(context, task):\n    return (1\n", "syntax_bracket"),
        ("async def run(context, task)\n    return 1\n", "syntax_colon"),
        ("async def run(context, task):\n  x = 1\n    return x\n", "syntax_indent"),
        ("async def run(context, task):\n    return “x”\n", "syntax_character"),
        ("async def run(context, task):\n    return 1 \\ 2\n", "syntax_backslash"),
        ("async def run(context, task):\n    return f'{'\n", "syntax_fstring"),
        ("async def run(context, task):\n    return 1 +\n", "syntax"),
    ],
)
def test_syntax_errors_name_a_fixed_rule(v2: HarnessArtifact, source: str, rule: str) -> None:
    with pytest.raises(SandboxError, match="^sandbox_invalid_source$") as raised:
        validate_source(source, v2)
    assert raised.value.rule == rule and raised.value.line is not None
    message = language.refusal_message("sandbox_invalid_source", rule, None, raised.value.line)
    assert message.startswith(f"Line {raised.value.line}: ") and "return" not in message


def test_every_refusal_message_is_fixed_text_of_at_most_two_sentences() -> None:
    sentence_end = re.compile(r"(?<=[.?!])\s+(?=[A-Z0-9])")
    for name, rule in language.RULES.items():
        assert rule.code in language.HINTS, name
        for text in filter(None, (rule.text, rule.named)):
            assert text.isascii() and "—" not in text and "–" not in text, name
            assert len(sentence_end.split(text)) <= 2, name
            assert len(text) <= 320, name
    for name, advice in language.ALTERNATIVES.items():
        assert advice.isascii() and advice.endswith("."), name
        message = language.refusal_message("sandbox_call", "missing_function", name, 7)
        assert message == f"Line 7: {name} does not exist here: {advice}"
    for name, advice in language.MODULES.items():
        assert advice.isascii() and advice.endswith(".") and len(advice) <= 200, name
        assert len(sentence_end.split(advice)) == 1, name
        message = language.refusal_message("sandbox_import", "import_module", name, 7)
        assert message == (
            f"Line 7: No module can be imported, so delete this import of {name}: {advice}"
        )
        message = language.refusal_message("sandbox_expression", "module_use", name, 7)
        assert message == (
            f"Line 7: {name} is not available, since no module can be imported: {advice}"
        )
    # A name that is not a plain identifier is never shown.
    message = language.refusal_message("sandbox_helper", "helper_builtin_name", "os.system()", 1)
    assert "os.system" not in message
    assert language.refusal_message("sandbox_key", None, None, 4) == (
        "Line 4: " + language.HINTS["sandbox_key"]
    )
    assert language.refusal_message("unknown_code", None, None, 4) == ""


def test_every_rule_the_validator_raises_is_in_the_table() -> None:
    import architect.runtime.sandbox as sandbox_module

    tree = ast.parse(Path(sandbox_module.__file__).read_text(encoding="utf-8"))
    strings = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    named = {
        cast(str, keyword.value.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "rule" and isinstance(keyword.value, ast.Constant)
    }
    assert named <= set(language.RULES)
    # Every rule is raised somewhere (two are built from the name assigned).
    assert set(language.RULES) <= strings | {"assign_task", "assign_context"}


def test_the_card_is_compact_and_lists_every_builtin_and_rule_area() -> None:
    card = language.language_markdown()
    words = re.findall(r"\S+", card)
    assert len(words) <= 1500
    for spec in language.BUILTINS.values():
        assert spec.signature in card, spec.name
    for line in (
        "from __future__ import annotations",
        "from pydantic import JsonValue",
        "from architect.presets import execute_preset",
        "from architect.runtime import RunContext",
        "## Helper rules",
        "## Refused, and why",
        "async def name(context, a)",
        "Type annotations are allowed",
    ):
        assert line in card, line
