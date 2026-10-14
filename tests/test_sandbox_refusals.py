"""Sandbox language v2 checks: every refused line in one pass, the module table, the
script entry block and the run line (Forge pack v3)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sandbox_v2_support import body, interpret, make_artifact

from architect.runtime import sandbox_language as language
from architect.runtime.sandbox import Refusal, SandboxError, validate_source
from architect.spec import HarnessArtifact


@pytest.fixture
def v2(tmp_path: Path) -> HarnessArtifact:
    return make_artifact(tmp_path / "v2")


def refusals(source: str, artifact: HarnessArtifact) -> list[tuple[int | None, str | None]]:
    with pytest.raises(SandboxError) as raised:
        validate_source(source, artifact)
    found = raised.value.refusals
    assert found[0] == Refusal(
        raised.value.line, raised.value.code, raised.value.rule, raised.value.name
    )
    assert [refusal.line or 0 for refusal in found] == sorted(
        refusal.line or 0 for refusal in found
    )
    for refusal in found:
        assert language.RULES[str(refusal.rule)].code == refusal.code
    return [(refusal.line, refusal.rule) for refusal in found]


def test_one_check_finds_every_refused_line(v2: HarnessArtifact) -> None:
    source = (
        "x = 1\n"  # 1: not a constant
        "def f(a, *rest):\n"  # 2: *args
        "    return [a, rest]\n"
        "async def run(context, task):\n"  # 4
        "    y = task.size\n"  # 5: attribute
        "    if y ** 2:\n"  # 6: operator
        "        raise ValueError\n"  # 7: raise, in a refused if test's body
        "    for a, b, c, d in task:\n"  # 8: four names
        "        z = lambda: a\n"  # 9: lambda, inside that loop
        "    with task:\n"  # 10: with, its body still checked
        "        w = task.other()\n"  # 11: unknown method
        "    return f(1, 2, 3, 4)\n"  # 12: f's own signature is refused: calls are not
    )
    assert refusals(source, v2) == [
        (1, "constant_name"), (2, "helper_parameter_kind"), (5, "attribute"), (6, "operator"),
        (7, "raise"), (8, "for_target"), (9, "lambda"), (10, "with"), (11, "method_unknown"),
    ]


def test_an_outer_refusal_comes_before_its_parts_on_a_line(v2: HarnessArtifact) -> None:
    source = body("return missing(task.size, 2 ** 3)")
    assert refusals(source, v2) == [
        (2, "missing_function"), (2, "attribute"), (2, "operator")
    ]


def test_the_same_refusal_on_a_line_is_kept_once(v2: HarnessArtifact) -> None:
    assert refusals(body("return [task.a, task.b, task.c]"), v2) == [(2, "attribute")]


def test_a_check_goes_on_past_module_level_refusals(v2: HarnessArtifact) -> None:
    source = (
        "import os\n"
        "from pydantic import JsonValue as J\n"
        "C = 1\n"
        "C = 2\n"
        "class Box:\n"
        "    pass\n"
        "print(C)\n"
        "C += 1\n"
        "def f(x):\n    return x\n"
        "def f(x):\n    return x\n"
        + body("return [Box(), os.environ, f(1)]")
    )
    assert refusals(source, v2) == [
        (1, "import"), (2, "import"), (4, "constant_duplicate"), (5, "class"),
        (7, "module_statement"), (8, "constant_change"), (11, "helper_duplicate"),
        (14, "class_use"), (14, "module_use"),
    ]


def test_run_placement_is_refused_and_the_rest_still_checked(v2: HarnessArtifact) -> None:
    source = body("return f(task.x)") + "def f(x):\n    return x.y\n"
    assert refusals(source, v2) == [(2, "attribute"), (3, "run_last"), (4, "attribute")]
    assert refusals("def f(x):\n    return x.y\n", v2) == [(1, "run_last"), (2, "attribute")]


def test_a_recursion_cycle_is_refused_at_each_closing_call(v2: HarnessArtifact) -> None:
    source = (
        "def f(x):\n    return g(x)\n"
        "def g(x):\n    return f(x)\n"
        "def h(x):\n    return h(x)\n"
        + body("return [f(1), h(1)]")
    )
    assert refusals(source, v2) == [(4, "recursion"), (6, "recursion")]


def test_a_long_helper_chain_is_checked_without_host_recursion(v2: HarnessArtifact) -> None:
    # Every helper past the 32nd is refused, and the call graph of all of them is still
    # walked for cycles (with an explicit stack): the chain has none, the loop closing it
    # has one.
    count = 700
    helpers = "".join(f"def h{index}(x):\n    return h{index + 1}(x)\n" for index in range(count))
    tail = f"def h{count}(x):\n    return h0(x)\n"
    found = refusals(helpers + tail + body("return h0(1)"), v2)
    assert [rule for _, rule in found if rule != "helper_limit"] == ["recursion"]
    assert len(found) == count + 1 - language.HELPER_LIMIT + 1


# ---------------------------------------------------------------- the module table


@pytest.mark.parametrize("module", sorted(language.MODULES))
def test_each_known_module_names_what_replaces_it(v2: HarnessArtifact, module: str) -> None:
    for line in (f"import {module}", f"import {module} as m", f"from {module} import x"):
        with pytest.raises(SandboxError) as raised:
            validate_source(line + "\n" + body("return 1"), v2)
        error = raised.value
        assert (error.code, error.rule, error.name, error.line) == (
            "sandbox_import", "import_module", module, 1
        )
        message = language.refusal_message(error.code, error.rule, error.name, error.line)
        assert message.endswith(language.MODULES[module])


def test_a_known_module_is_named_where_it_is_read(v2: HarnessArtifact) -> None:
    source = (
        "import json as j\n"
        "from re import sub\n"
        "PATTERN = j.dumps({})\n"
        + body("x = j.loads(task)", "y = sub('a', 'b', task)", "z = j.JSONDecodeError",
               "return await j.wait()")
    )
    with pytest.raises(SandboxError) as raised:
        validate_source(source, v2)
    found = [(refusal.line, refusal.rule, refusal.name) for refusal in raised.value.refusals]
    assert found == [
        (1, "import_module", "json"), (2, "import_module", "re"), (3, "module_use", "json"),
        (5, "module_use", "json"), (6, "module_use", "re"), (7, "module_use", "json"),
        (8, "module_use", "json"),
    ]


def test_an_unknown_module_is_never_named(v2: HarnessArtifact) -> None:
    source = "import QQ_PRIVATE_MODULE\n" + body("return QQ_PRIVATE_MODULE.read()")
    with pytest.raises(SandboxError) as raised:
        validate_source(source, v2)
    found = [(refusal.line, refusal.rule, refusal.name) for refusal in raised.value.refusals]
    assert found == [(1, "import", None), (3, "module_use", None)]
    for refusal in raised.value.refusals:
        text = language.refusal_message(refusal.code, refusal.rule, refusal.name, refusal.line)
        assert text and "QQ_PRIVATE" not in text


def test_a_local_named_like_a_module_is_a_value(v2: HarnessArtifact) -> None:
    # Only a name no local binds is read as the refused module.
    source = "import re\n" + body("re = 'x'", "return re.upper()")
    with pytest.raises(SandboxError) as raised:
        validate_source(source, v2)
    assert [refusal.line for refusal in raised.value.refusals] == [1]


# ---------------------------------------------------------------- the script entry block


@pytest.mark.parametrize(
    "guard",
    ['if __name__ == "__main__":', "if __name__ == '__main__':",
     'if "__main__" == __name__:'],
)
def test_a_main_block_is_ignored_wherever_it_stands(
    monkeypatch: pytest.MonkeyPatch, v2: HarnessArtifact, guard: str
) -> None:
    block = f"{guard}\n    import asyncio\n    print(asyncio.run(run(None, None)))\n"
    for source in (body("return 'ran'") + block, block + body("return 'ran'")):
        validate_source(source, v2)
        frame, sent = interpret(monkeypatch, v2, source)
        assert frame["value"] == "ran" and sent == []


@pytest.mark.parametrize(
    "block",
    [
        'if __name__ == "__main__":\n    pass\nelse:\n    pass\n',
        'if __name__ != "__main__":\n    pass\n',
        'if __name__ == "__main__" and True:\n    pass\n',
        'if __name__ == "main":\n    pass\n',
        'if name == "__main__":\n    pass\n',
    ],
)
def test_only_the_exact_main_block_is_ignored(v2: HarnessArtifact, block: str) -> None:
    with pytest.raises(SandboxError) as raised:
        validate_source(block + body("return 1"), v2)
    assert (raised.value.rule, raised.value.line) == ("module_statement", 1)


def test_a_main_test_inside_a_function_is_code(v2: HarnessArtifact) -> None:
    with pytest.raises(SandboxError) as raised:
        validate_source(body('if __name__ == "__main__":', "    return 1", "return 2"), v2)
    assert (raised.value.rule, raised.value.line) == ("unknown_name", 2)


# ---------------------------------------------------------------- the run line


@pytest.mark.parametrize(
    "line",
    [
        "async def run(context, task):",
        "async def run(context: RunContext, task: JsonValue) -> JsonValue:",
        "async def run(context: 'RunContext', task: 'JsonValue') -> 'JsonValue':",
        "async def run(context: Any, task: dict[str, Any]) -> dict[str, Any] | None:",
        "async def run(context: typing.Any, task: list[dict]) -> str:",
    ],
)
def test_run_accepts_the_documented_line_and_annotated_variants(
    v2: HarnessArtifact, line: str
) -> None:
    assert validate_source(line + "\n    return 1\n", v2).language == 2


@pytest.mark.parametrize(
    "line",
    [
        "def run(context, task):",
        "async def run(item, model):",
        "async def run(ctx, task):",
        "async def run(context, task, extra=None):",
        "async def run(context, task, /):",
        "async def run(context, *, task):",
        "async def run(context, task, **more):",
        "@staticmethod\nasync def run(context, task):",
    ],
)
def test_any_other_run_line_is_told_the_exact_line_to_write(
    v2: HarnessArtifact, line: str
) -> None:
    source = line + "\n    return 1\n"
    with pytest.raises(SandboxError) as raised:
        validate_source(source, v2)
    error = raised.value
    assert (error.code, error.rule) == ("sandbox_signature", "run_signature")
    message = language.refusal_message(error.code, error.rule, error.name, error.line)
    assert "async def run(context: RunContext, task: JsonValue) -> JsonValue:" in message


def test_a_run_with_other_names_is_still_checked_as_run(v2: HarnessArtifact) -> None:
    source = "def run(item, model):\n    data = item.text\n    return await model.call(data)\n"
    found = refusals(source, v2)
    assert found == [(1, "run_signature"), (2, "attribute"), (3, "await_target")]
