"""Phase 9 Section 9: the five Forge tools, their caps, codes and leakage-safe views."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from forge_support import (
    CHEAP_WRONG,
    HELD_OUT_MARK,
    REPAIR,
    SECRET,
    TUNING_MARK,
    ensure_sandbox,
    floor_factory,
    play_factory,
    spec,
)

from architect.backends.pi import FORGE_TOOL_NAMES
from architect.compiler import forge as forge_module
from architect.compiler.forge_settings import ForgeSettings
from architect.compiler.forge_tools import (
    REFUSAL_TEXTS,
    RESULT_CHARS,
    ForgeSession,
    SubmissionsExhausted,
    _bounded,
    _materialize,
    forge_rank,
    forge_tools,
    spawn_settings,
    submit_source,
)
from architect.compiler.tuning import tune
from architect.floors import EvaluationCase, HeldOutTasks
from architect.presets import get_preset
from architect.runtime import Runtime
from architect.runtime.wallet import CompileWallet, compilation_wallet
from architect.spec import Budget, FloorDefinition, FloorKind, HarnessArtifact
from architect.telemetry import TelemetryStore


@dataclass
class Rig:
    session: ForgeSession
    wallet: CompileWallet
    tools: dict[str, Any]
    root: Path

    def write(self, source: str, path: str = "harness.py") -> None:
        target = self.session.workspace / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")

    async def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        value = await self.tools[tool].callback(arguments)
        assert isinstance(value, dict)
        text = json.dumps(value)
        assert len(text) <= RESULT_CHARS and text.isascii()
        assert SECRET not in text and HELD_OUT_MARK not in text
        assert list(value)[:2] == ["ok", "code"] and list(value)[-2:] == ["left", "truncated"]
        return value


@contextmanager
def wallet(settings: ForgeSettings) -> Iterator[CompileWallet]:
    with compilation_wallet(Budget(cost_usd=settings.max_usd, compute_s=settings.max_s)) as open_:
        yield open_


async def make_rig(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    settings: ForgeSettings | None = None,
    play: str = "nope",
    count: int = 6,
    smoke: bool = True,
    depth: int = 0,
) -> Rig:
    await ensure_sandbox(monkeypatch, root / "probe")
    settings = settings or ForgeSettings(enabled=True)
    value = spec()
    floors = floor_factory(count)
    floor = floors(value.floor)
    factory = play_factory(play)
    runtime = Runtime(TelemetryStore(root / "telemetry"))
    runtime.adapter_factory = factory
    tuned = await tune(
        value, get_preset("synthesizer"), "fake", floor, factory, runtime, root / "artifacts",
        max_candidates=1,
    )
    assert tuned.best is not None
    workspace, private = root / "workspace", root / "private"
    workspace.mkdir()
    session = ForgeSession(
        candidate=tuned.best,
        floor=floor,
        smoke_floor=floors(value.floor) if smoke else None,
        adapter_factory=factory,
        runtime=runtime,
        settings=settings,
        workspace=workspace,
        private=private,
        floor_factory=floors,
        depth=depth,
    )
    return Rig(session, None, forge_tools(session), root)  # type: ignore[arg-type]


@pytest.fixture
async def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Rig]:
    made = await make_rig(tmp_path, monkeypatch)
    with wallet(made.session.settings) as open_:
        made.session.wallet = open_
        made.wallet = open_
        yield made


def test_tool_set_schemas_and_caps() -> None:
    settings = ForgeSettings(max_checks=7, max_dryruns=0, max_measures=5, max_submissions=2)
    session = ForgeSession.__new__(ForgeSession)
    session.settings = settings
    tools = forge_tools(session)
    assert set(tools) == FORGE_TOOL_NAMES
    caps = {name: tool.max_calls for name, tool in tools.items()}
    assert caps == {
        "forge_check": 7,
        "forge_dryrun": 0,
        "forge_measure": 5,
        "forge_submit": 2,
        "forge_spawn": 3,
    }
    for tool in tools.values():
        assert tool.parameters["type"] == "object"
        assert tool.parameters["additionalProperties"] is False
        assert tool.requires_build is True and tool.max_result_chars == RESULT_CHARS
        assert len(tool.description) <= 1024 and tool.description.isascii()
    assert tools["forge_dryrun"].parameters["required"] == ["case"]
    assert tools["forge_spawn"].parameters["required"] == [
        "name", "goal", "input_schema", "output_schema", "cases",
    ]


# forge_check -------------------------------------------------------------------


async def test_check_ok_stats_and_left(rig: Rig) -> None:
    rig.write(REPAIR)
    result = await rig.call("forge_check")
    assert result["ok"] is True and result["code"] is None and result["language"] == 2
    assert set(result["stats"]) == {
        "nodes", "node_limit", "depth", "helpers", "constants", "loops", "await_sites", "bytes",
    }
    assert result["stats"]["node_limit"] == 8000 and result["stats"]["await_sites"] == 3
    assert result["lint"] == [] and result["unbound_children"] == []
    assert set(result["left"]) == {
        "usd", "seconds", "checks", "dryruns", "measures", "submissions", "spawns",
    }
    assert result["left"]["checks"] == 199 and rig.wallet.cost_usd == 0


async def test_check_rejections_are_fixed_codes(rig: Rig) -> None:
    rig.write("import os\nasync def run(context, task):\n    return os.environ\n")
    result = await rig.call("forge_check")
    assert result["ok"] is False and result["code"] == "sandbox_import"
    assert (result["line"], result["rule"]) == (1, "import")
    assert result["message"].startswith("Line 1: Only these imports exist")
    assert "os.environ" not in json.dumps(result)
    rig.write("x" * (64 * 1024 + 1))
    too_large = await rig.call("forge_check")
    assert too_large["code"] == "forge_file_too_large"
    assert too_large["message"] == REFUSAL_TEXTS["forge_file_too_large"]
    escape = await rig.call("forge_check", path="../escape.py")
    assert escape["code"] == "forge_invalid_source" and escape["rule"] is None
    assert escape["message"] == REFUSAL_TEXTS["forge_invalid_source"]
    assert (await rig.call("forge_check", path="drafts/missing.py"))["code"] == (
        "forge_invalid_source"
    )
    rig.write(REPAIR, "drafts/alt.py")
    passed = await rig.call("forge_check", path="drafts/alt.py")
    assert passed["ok"] is True and passed["rule"] is None and passed["message"] is None
    assert rig.session.checks == 5


# The constructs an engineer model wrote in the G1 sessions, in a task-neutral harness:
# annotated helpers, a default, method spellings, isinstance, get with no default.
ANNOTATED_HARNESS = (
    '"""Keep the digits in code, check the count, and ask the model only on a miss."""\n'
    "from __future__ import annotations\n"
    "from pydantic import JsonValue\n"
    "from architect.runtime import RunContext\n\n"
    'DIGITS = "0123456789"\n\n'
    "def only_digits(s: str) -> str:\n"
    '    out: str = ""\n'
    "    for ch in s:\n"
    "        if ch in DIGITS:\n"
    "            out += ch\n"
    "    return out\n\n"
    "def words(text: str, sep: str = ' ') -> list[str]:\n"
    "    kept: list[str] = []\n"
    "    for part in text.strip().split(sep):\n"
    "        if part:\n"
    "            kept.append(part)\n"
    "    return kept\n\n"
    "async def ask(context: RunContext, prompt: str) -> str | None:\n"
    "    reply = await context.call(prompt, 64)\n"
    "    return reply if isinstance(reply, str) else None\n\n"
    "async def run(context: RunContext, task: JsonValue) -> JsonValue:\n"
    "    count: int = len(words(only_digits(str(get(task, 'index'))) + ' x'))\n"
    "    if count > 0:\n"
    '        return "ok"\n'
    '    return await ask(context, "Reply ok")\n'
)


async def test_annotated_helpers_now_pass_check_and_run(rig: Rig) -> None:
    rig.write(ANNOTATED_HARNESS)
    checked = await rig.call("forge_check")
    assert checked["ok"] is True and checked["stats"]["helpers"] == 3
    dry = await rig.call("forge_dryrun", case=0)
    assert dry["ok"] is True and dry["output_head"] == "ok" and dry["message"] is None


@pytest.mark.parametrize(
    ("old", "new", "code", "rule", "start"),
    [
        (
            "def words(text: str, sep: str = ' ') -> list[str]:",
            "def lower(text: str, sep: str = ' ') -> list[str]:",
            "sandbox_helper", "helper_builtin_name",
            "Line 15: lower is a builtin, so no helper can take that name.",
        ),
        (
            "async def ask(context: RunContext, prompt: str) -> str | None:\n"
            "    reply = await context.call(prompt, 64)",
            "async def ask(ctx: RunContext, prompt: str) -> str | None:\n"
            "    reply = await ctx.call(prompt, 64)",
            "sandbox_helper", "helper_async_context",
            "Line 22: An async helper's first parameter must be named context",
        ),
        (
            "        if ch in DIGITS:",
            "        if reversed(ch):",
            "sandbox_call", "missing_function",
            "Line 11: reversed does not exist here: use l[::-1].",
        ),
        (
            "            kept.append(part)",
            "            kept.add(part)",
            "sandbox_expression", "method_unknown",
            "Line 19: add does not exist here: use append(l, x) after if x not in l:",
        ),
        (
            "            kept.append(part)",
            "            kept.discard(part)",
            "sandbox_expression", "method_unknown",
            "Line 19: Values have no method by this name.",
        ),
        (
            "async def run(context: RunContext, task: JsonValue) -> JsonValue:",
            "async def run(ctx: RunContext, task: JsonValue) -> JsonValue:",
            "sandbox_signature", "run_signature",
            "Line 26: Write run's first line as async def run(context: RunContext, task: "
            "JsonValue) -> JsonValue:",
        ),
        (
            "def only_digits(s: str) -> str:",
            "def only_digits(s: print(1)) -> str:",
            "sandbox_expression", "annotation_form",
            "Line 8: An annotation must name a type",
        ),
    ],
)
async def test_check_and_dryrun_name_the_rule_line_and_allowed_form(
    rig: Rig, old: str, new: str, code: str, rule: str, start: str
) -> None:
    source = ANNOTATED_HARNESS.replace(old, new)
    assert source != ANNOTATED_HARNESS
    marked = source.replace('"Reply ok"', '"QQ_PRIVATE_PROMPT_TEXT"')
    rig.write(marked)
    checked = await rig.call("forge_check")
    assert checked["ok"] is False and (checked["code"], checked["rule"]) == (code, rule)
    assert checked["message"].startswith(start), checked["message"]
    dry = await rig.call("forge_dryrun", case=0)
    assert dry["code"] == "forge_invalid_source" and dry["sandbox_code"] == code
    assert dry["rule"] == rule and dry["message"] == checked["message"]
    assert dry["line"] == checked["line"]
    # A message is fixed text: no part of the source is ever echoed back.
    for result in (checked, dry):
        text = json.dumps(result)
        assert "QQ_PRIVATE_PROMPT_TEXT" not in text and "only_digits" not in text
        assert "kept" not in text and "words" not in text


async def test_dryrun_and_measure_refusals_say_why(rig: Rig) -> None:
    rig.write(REPAIR)
    invalid = await rig.call("forge_dryrun", case=9)
    assert invalid["code"] == "invalid_case"
    assert invalid["message"] == (
        "case must be the index of a tuning case, from 0 to 2. TUNING.md shows them in order."
    )
    measured = await rig.call("forge_measure", cases=[5])
    assert measured["code"] == "invalid_case" and measured["message"] == invalid["message"]
    rig.write("async def run(context, task):\n    x = task['missing']\n    return x\n")
    failed = await rig.call("forge_dryrun", case=0)
    assert failed["ok"] is False and (failed["code"], failed["line"]) == ("sandbox_key", 2)
    assert failed["message"] == "Line 2: A dict key is missing; use get(d, k, default)."
    rig.write("async def run(context, task):\n    return {'k': 1}\n")
    shaped = await rig.call("forge_dryrun", case=0)
    assert shaped["ok"] is True and shaped["schema_ok"] is False
    assert shaped["message"] == REFUSAL_TEXTS["schema_mismatch"]
    rig.write("def str(x):\n    return x\nasync def run(context, task):\n    return 1\n")
    refused = await rig.call("forge_measure", cases=[0])
    assert refused["code"] == "forge_invalid_source"
    assert (refused["sandbox_code"], refused["rule"]) == ("sandbox_helper", "helper_builtin_name")
    assert refused["message"].startswith("Line 1: str is a builtin")
    assert rig.wallet.cost_usd == 0


async def test_submit_refusals_carry_a_message(rig: Rig) -> None:
    rig.write("async def run(ctx, task):\n    return 1\n")
    first = await rig.call("forge_submit")
    assert (first["code"], first["sandbox_code"], first["rule"]) == (
        "forge_invalid_source", "sandbox_signature", "run_signature"
    )
    assert first["message"].startswith(
        "Line 1: Write run's first line as async def run(context: RunContext, task: JsonValue)"
    )
    assert first["refusals_total"] == 1 and first["refusals"] == [
        {"line": 1, "code": "sandbox_signature", "rule": "run_signature",
         "message": first["message"]}
    ]
    rig.write(CHEAP_WRONG)
    second = await rig.call("forge_submit")
    assert second["code"] == "tuning_failed" and second["rule"] is None
    assert second["message"] == REFUSAL_TEXTS["tuning_failed"]


# A draft written as ordinary Python: every refused line, at once.
PYTHON_HARNESS = (
    '"""A harness written as ordinary Python."""\n'  # 1
    "import json\n"  # 2
    "import re\n"  # 3
    "from typing import Any\n"  # 4
    "\n"  # 5
    "PATTERN = re.compile('QQ_PRIVATE_PATTERN')\n"  # 6
    "\n"  # 7
    "class Record:\n"  # 8
    "    pass\n"  # 9
    "\n"  # 10
    "def parse(text: str) -> Any:\n"  # 11
    "    return json.loads(text)\n"  # 12
    "\n"  # 13
    "def digits(s: str) -> str:\n"  # 14
    "    return ''.join(c for c in s if c.isnumeric())\n"  # 15
    "\n"  # 16
    "async def run(context, task):\n"  # 17
    "    words = [w.strip() for w in str(task).split(',') if w]\n"  # 18
    "    found = PATTERN.search(words[0])\n"  # 19
    "    keep = Record()\n"  # 20
    "    return {'n': len(words), 'd': digits(words[0]), 'p': parse('{}')}\n"  # 21
    "\n"  # 22
    'if __name__ == "__main__":\n'  # 23
    "    import asyncio\n"  # 24
    "    print(asyncio.run(run(None, 'QQ_PRIVATE_MAIN')))\n"  # 25
)


async def test_check_lists_every_refused_line_at_once(rig: Rig) -> None:
    rig.write(PYTHON_HARNESS)
    checked = await rig.call("forge_check")
    assert checked["ok"] is False and checked["code"] == "sandbox_import"
    assert (checked["line"], checked["rule"]) == (2, "import_module")
    refusals = checked["refusals"]
    assert [(item["line"], item["rule"]) for item in refusals] == [
        (2, "import_module"), (3, "import_module"), (4, "import_module"),
        (6, "module_use"), (8, "class"), (12, "module_use"), (15, "method_unknown"),
        (19, "method_unknown"), (20, "class_use"),
    ]
    assert checked["refusals_total"] == len(refusals)
    assert refusals[0]["message"] == checked["message"] == (
        "Line 2: No module can be imported, so delete this import of json: json.loads(s) is "
        "parse_json(s), which gives None for text that is not JSON, json.dumps(x) is "
        "to_json(x), and extract_json(s) finds JSON inside prose or a code fence."
    )
    messages = {item["line"]: item["message"] for item in refusals}
    assert messages[4].startswith("Line 4: No module can be imported, so delete this import "
                                  "of typing: annotations are never evaluated")
    assert messages[6].startswith("Line 6: re is not available, since no module can be "
                                  "imported: there are no regular expressions")
    assert messages[15] == "Line 15: isnumeric does not exist here: use s.isdigit()."
    assert messages[19].startswith("Line 19: search does not exist here: there are no regular")
    # The comprehension, the generator in join and the main block pass as they are.
    assert 18 not in messages and 23 not in messages and 25 not in messages
    # Fixed text and line numbers only: nothing from the source comes back.
    text = json.dumps(checked)
    assert "QQ_PRIVATE" not in text and "digits" not in text and "Record" not in text
    # The dry run, the measure and the submission refuse the source with the same list.
    dry = await rig.call("forge_dryrun", case=0)
    assert dry["code"] == "forge_invalid_source" and dry["refusals"] == refusals
    measured = await rig.call("forge_measure", cases=[0])
    assert measured["refusals"] == refusals and measured["refusals_total"] == len(refusals)
    submitted = await rig.call("forge_submit")
    assert submitted["refusals"] == refusals and rig.wallet.cost_usd == 0
    fixed = (
        PYTHON_HARNESS.replace("import json\n", "").replace("import re\n", "")
        .replace("from typing import Any\n", "")
        .replace("PATTERN = re.compile('QQ_PRIVATE_PATTERN')\n", "")
        .replace("class Record:\n    pass\n", "")
        .replace("json.loads(text)", "parse_json(text)")
        .replace("c.isnumeric()", "c.isdigit()")
        .replace("    found = PATTERN.search(words[0])\n", "")
        .replace("    keep = Record()\n", "")
    )
    rig.write(fixed)
    passed = await rig.call("forge_check")
    assert passed["ok"] is True and passed["refusals"] == [] and passed["refusals_total"] == 0


async def test_a_repeated_refusal_names_the_line_it_repeats(rig: Rig) -> None:
    lines = ["import re", "async def run(context, task):", "    out = []"]
    lines += [f"    append(out, re.sub('a', 'b', str({index})))" for index in range(30)]
    lines += ["    s = 'x'", "    return s.isnumeric()"]
    rig.write("\n".join(lines) + "\n")
    checked = await rig.call("forge_check")
    refusals = checked["refusals"]
    assert checked["refusals_total"] == 32 and len(refusals) == 20
    # Each kind of refusal is listed before any repeat, even the last line's.
    assert refusals[0]["rule"] == "import_module" and refusals[-1]["line"] == 35
    assert refusals[-1]["rule"] == "method_unknown"
    assert refusals[1]["message"].startswith("Line 4: re is not available")
    assert [item["message"] for item in refusals[2:4]] == [
        "Line 5: the same problem as line 4.", "Line 6: the same problem as line 4."
    ]
    assert [item["line"] for item in refusals] == sorted(item["line"] for item in refusals)


async def test_a_syntax_error_is_the_one_refusal(rig: Rig) -> None:
    rig.write("async def run(context, task):\n    return (\n")
    checked = await rig.call("forge_check")
    assert checked["code"] == "sandbox_invalid_source" and checked["refusals_total"] == 1
    assert checked["refusals"][0]["message"] == checked["message"]


def test_refusal_texts_are_plain_and_task_neutral() -> None:
    import re

    sentence_end = re.compile(r"(?<=[.?!])\s+(?=[A-Z0-9])")
    for code, text in REFUSAL_TEXTS.items():
        assert text.isascii() and len(sentence_end.split(text)) <= 2, code
        assert "\u2014" not in text and "\u2013" not in text, code


async def test_check_lint_is_advisory_and_lists_unbound_children(rig: Rig) -> None:
    leak = rig.session.tuning[0].task["text"]  # type: ignore[index]
    rig.write(REPAIR.replace('"third"', repr(leak)))
    result = await rig.call("forge_check")
    assert result["lint"] == ["forge_literal_leak"] and result["ok"] is True
    assert leak not in json.dumps(result)
    rig.write(REPAIR.replace('return "ok"', 'return await context.harness("helper", task)'))
    result = await rig.call("forge_check")
    assert result["ok"] is False and result["unbound_children"] == ["helper"]


# forge_dryrun ------------------------------------------------------------------


async def test_dryrun_is_free_and_bounded(rig: Rig) -> None:
    rig.write(REPAIR)
    assert (await rig.call("forge_dryrun", case=3))["code"] == "invalid_case"
    result = await rig.call("forge_dryrun", case=0, replies=["a", "b"])
    assert result["ok"] is True and result["output_head"] == "ok"
    assert [call["prompt_head"] for call in result["calls"]] == ["first", "second", "third"]
    assert rig.wallet.cost_usd == 0
    rig.write("async def run(context, task):\n    import os\n")
    result = await rig.call("forge_dryrun", case=0)
    assert result["ok"] is False and result["code"] == "forge_invalid_source"
    assert rig.session.dryruns == 3


# forge_measure -----------------------------------------------------------------


async def test_measure_per_case_view_and_spend(rig: Rig) -> None:
    rig.write(REPAIR)
    result = await rig.call("forge_measure")
    assert result["ok"] is True and result["per_case"] == "available"
    measurement = result["measurement"]
    assert measurement["sample_count"] == 3 and measurement["floor_passed"] is True
    assert measurement["mean_calls"] == 3 and measurement["floor_limit"] == 1
    assert [case["index"] for case in result["cases"]] == [0, 1, 2]
    # Passing cases keep their compact rows; excerpts are for failures only.
    assert all(case["passed"] is True for case in result["cases"])
    assert all(case["expected_excerpt"] is None for case in result["cases"])
    assert result["baseline"] == {
        "score": 0.0,
        "mean_cost_usd": rig.session.candidate.measurement.mean_cost_usd,
        "floor_passed": False,
    }
    assert rig.wallet.cost_usd > 0
    assert (await rig.call("forge_measure", cases=[0, 7]))["code"] == "invalid_case"
    smoke = await rig.call("forge_measure", cases=[1], smoke=True)
    assert smoke["ok"] is True and smoke["measurement"]["sample_count"] == 1


async def test_measure_details_only_the_first_failures(tmp_path: Path, monkeypatch: Any) -> None:
    settings = ForgeSettings(enabled=True, max_failures_shown=2)
    made = await make_rig(tmp_path, monkeypatch, settings=settings, count=8)
    with wallet(made.session.settings) as open_:
        made.session.wallet = open_
        made.write(CHEAP_WRONG)
        result = await made.tools["forge_measure"].callback({"cases": [0, 1, 2, 3]})
    cases = result["cases"]
    assert [case["passed"] for case in cases] == [False] * 4
    assert [case["expected_excerpt"] for case in cases] == ["ok", "ok", None, None]
    assert [case["output_excerpt"] for case in cases] == ["wrong", "wrong", None, None]
    assert all(case["calls"] == 0 and case["cost_usd"] == 0 for case in cases)
    # The default shows the first 8.
    assert ForgeSettings().max_failures_shown == 8


async def test_dryrun_default_reply_is_shaped_like_the_output_schema(
    tmp_path: Path, monkeypatch: Any
) -> None:
    schema = {
        "type": "object",
        "properties": {"total": {"type": ["integer", "null"]}, "item_count": {"type": "integer"}},
        "required": ["total", "item_count"],
        "additionalProperties": False,
    }
    made = await make_rig(tmp_path, monkeypatch)
    artifact = made.session.candidate.artifact
    shaped = artifact.model_copy(
        update={"spec": artifact.spec.model_copy(update={"output_schema": schema})}
    )
    made.session.candidate = made.session.candidate.model_copy(update={"artifact": shaped})
    echo = (
        "from pydantic import JsonValue\n"
        "from architect.runtime import RunContext\n\n"
        "async def run(context: RunContext, task: JsonValue) -> JsonValue:\n"
        '    return parse_json(await context.call("answer"))\n'
    )
    with wallet(made.session.settings) as open_:
        made.session.wallet = open_
        made.write(echo)
        result = await made.tools["forge_dryrun"].callback({"case": 0})
        scripted = await made.tools["forge_dryrun"].callback({"case": 0, "replies": ["{}"]})
    assert result["ok"] is True and result["schema_ok"] is True
    assert json.loads(result["output_head"]) == {"total": 0, "item_count": 0}
    assert scripted["ok"] is True and scripted["schema_ok"] is False  # the old default


async def test_measure_aggregate_mode_hides_labels(tmp_path: Path, monkeypatch: Any) -> None:
    made = await make_rig(tmp_path, monkeypatch, settings=ForgeSettings(feedback="aggregate"))
    with wallet(made.session.settings) as open_:
        made.session.wallet = open_
        made.write(REPAIR)
        result = await made.tools["forge_measure"].callback({"cases": [0, 1]})
    assert result["per_case"] == "aggregate"
    assert all(
        case["passed"] is None and case["expected_excerpt"] is None and case["score"] is None
        for case in result["cases"]
    )


async def test_measure_without_smoke_floor_and_budget_exhaustion(
    tmp_path: Path, monkeypatch: Any
) -> None:
    made = await make_rig(tmp_path, monkeypatch, smoke=False)
    made.write(REPAIR)
    with compilation_wallet(Budget(cost_usd=0.0005, compute_s=60)) as open_:
        made.session.wallet = open_
        assert (await made.tools["forge_measure"].callback({"smoke": True}))["code"] == (
            "invalid_case"
        )
        result = await made.tools["forge_measure"].callback({"cases": [0]})
    assert result["ok"] is False and result["code"] == "budget_exceeded"
    assert "budget_exceeded" in result["measurement"]["failures"]


# forge_submit ------------------------------------------------------------------


async def test_submit_gate_codes_and_acceptance(rig: Rig) -> None:
    rig.write("async def run(context, task):\n    import os\n")
    first = await rig.call("forge_submit")
    assert first["code"] == "forge_invalid_source" and first["sandbox_code"].startswith("sandbox_")
    leak = rig.session.tuning[1].task["text"]  # type: ignore[index]
    rig.write(REPAIR.replace('"second"', repr(leak)))
    second = await rig.call("forge_submit")
    assert second["code"] == "forge_literal_leak" and second["line"] == 6
    rig.write(CHEAP_WRONG)
    third = await rig.call("forge_submit", note="cheap but wrong")
    assert third["code"] == "tuning_failed" and third["accepted"] is False
    assert third["smoke"] == {"floor_passed": False, "score": 0.0}
    rig.write(REPAIR)
    fourth = await rig.call("forge_submit", note="three calls")
    assert fourth["ok"] is True and fourth["accepted"] is True and fourth["best_so_far"] is True
    assert fourth["candidate"] == 4 and fourth["submissions_left"] == 2
    assert fourth["measurement"]["floor_passed"] is True
    assert rig.session.best_number == 4 and rig.session.accepted == 1
    assert (rig.session.private / "candidates" / "4" / "harness.py").read_text() == REPAIR
    assert rig.session.notes == [
        (15, hashlib.sha256(b"cheap but wrong").hexdigest()),
        (11, hashlib.sha256(b"three calls").hexdigest()),
    ]
    fifth = await rig.call("forge_submit")
    assert fifth["code"] == "not_better"
    rig.write(REPAIR)
    with pytest.raises(SubmissionsExhausted):
        await rig.tools["forge_submit"].callback({})
    code, fields = await submit_source(rig.session, REPAIR)
    assert code == "forge_submissions_exhausted" and fields["submissions_left"] == 0


@pytest.mark.parametrize(
    "draft", [b"#" * 65546, bytes([0xFF, 0xFE]) + b" not utf-8"], ids=["too_large", "undecodable"]
)
async def test_unreadable_draft_as_last_submission_ends_the_session(
    tmp_path: Path, monkeypatch: Any, draft: bytes
) -> None:
    settings = ForgeSettings(enabled=True, max_submissions=2)
    made = await make_rig(tmp_path, monkeypatch, settings=settings)
    expected = "forge_file_too_large" if len(draft) > 65536 else "forge_invalid_source"
    with wallet(settings) as open_:
        made.session.wallet = open_
        (made.session.workspace / "harness.py").write_bytes(draft)
        first = await made.call("forge_submit")
        assert first["code"] == expected and first["submissions_left"] == 1
        assert first["left"]["submissions"] == 1
        # Section 8.7: the last attempt ends the session even when the draft is unreadable.
        with pytest.raises(SubmissionsExhausted):
            await made.tools["forge_submit"].callback({})
    assert made.session.submissions == 2 and made.session.best_number is None
    assert not any((made.session.private / "candidates").iterdir())


async def test_candidate_store_is_private(rig: Rig) -> None:
    rig.write(REPAIR)
    await rig.call("forge_submit")
    private = rig.session.private.resolve()
    assert not private.is_relative_to(rig.session.workspace.resolve())
    assert not any("candidates" in path.parts for path in rig.session.workspace.rglob("*"))


async def test_rank_accepts_quality_repairs_that_cost_more(rig: Rig) -> None:
    from architect.compiler.rewriting import _cheaper

    baseline = rig.session.candidate.measurement
    rig.write(REPAIR)
    result = await rig.call("forge_submit")
    best = rig.session.best
    assert result["accepted"] and best is not None
    assert best.measurement.mean_cost_usd > baseline.mean_cost_usd
    assert not _cheaper(best.measurement, baseline)
    kwargs = {"smoke_passed": True, "subharnesses": 0, "components": 1, "nodes": 10}
    assert forge_rank(best.measurement, **kwargs) < forge_rank(baseline, **kwargs)


def test_bounded_results_truncate_cases_from_the_end() -> None:
    payload: dict[str, Any] = {
        "ok": True,
        "code": None,
        "cases": [{"index": i, "output_excerpt": "x" * 600} for i in range(40)],
        "left": {},
        "truncated": False,
    }
    bounded = _bounded(payload)
    assert bounded["truncated"] is True
    assert len(json.dumps(bounded)) <= RESULT_CHARS
    assert [case["index"] for case in bounded["cases"]] == list(range(len(bounded["cases"])))


# forge_spawn -------------------------------------------------------------------

AUTHORED = {"authored": [{"input": f"item {i}", "expected": "ok"} for i in range(8)]}
STRING = {"type": "string"}


async def spawn(rig: Rig, **arguments: Any) -> dict[str, Any]:
    values = {
        "name": "helper",
        "goal": "Return ok",
        "input_schema": STRING,
        "output_schema": STRING,
        "cases": AUTHORED,
    }
    values.update(arguments)
    return await rig.call("forge_spawn", **values)


async def test_spawn_refusals(rig: Rig) -> None:
    assert (await spawn(rig, input_schema={"type": 3}))["code"] == "invalid_schema"
    big = {"type": "string", "description": "x" * 17000}
    assert (await spawn(rig, output_schema=big))["code"] == "invalid_schema"
    remote = {"$ref": "https://example.invalid/schema"}
    assert (await spawn(rig, input_schema=remote))["code"] == "invalid_schema"
    few = {"authored": AUTHORED["authored"][:3]}
    assert (await spawn(rig, cases=few))["code"] == "invalid_cases"
    bad = {"authored": [{"input": 1, "expected": "ok"}] * 8}
    assert (await spawn(rig, cases=bad))["code"] == "invalid_cases"
    # The parent has only 3 tuning cases: a projection cannot reach 8.
    projection = {"projection": {"input": "/text", "expected": ""}}
    assert (await spawn(rig, cases=projection))["code"] == "invalid_cases"
    assert rig.session.spawns == 0 and not rig.session.children


async def test_spawn_depth_and_cap(tmp_path: Path, monkeypatch: Any) -> None:
    deep = await make_rig(tmp_path / "deep", monkeypatch, depth=1)
    result = await deep.tools["forge_spawn"].callback({"name": "x"})
    assert result["code"] == "forge_depth"
    capped = await make_rig(tmp_path / "cap", monkeypatch, settings=ForgeSettings(max_spawns=0))
    result = await capped.tools["forge_spawn"].callback({"name": "x"})
    assert result["code"] == "forge_spawn_cap"


async def test_authored_child_is_licensed_and_registered(
    tmp_path: Path, monkeypatch: Any
) -> None:
    made = await make_rig(tmp_path, monkeypatch, play="ok")
    with wallet(made.session.settings) as open_:
        made.session.wallet = open_
        result = await spawn(made)
        again = await spawn(made)
    assert result["ok"] is True and result["licensed"] is True
    assert result["case_origin"] == "authored" and result["preset"] == "synthesizer"
    assert result["call_as"] == 'await context.harness("helper", value)'
    assert result["verified"]["sample_count"] == 4 and result["spawns_left"] == 2
    assert result["tuning"] == {"score": 1.0, "floor_passed": True}
    assert again["code"] == "forge_spawn_name_taken"
    child = made.session.children["helper"]
    assert child.artifact.licensed and child.ref.case_origin == "authored"
    assert child.ref.artifact_digest == child.artifact.artifact_digest
    assert not child.artifact.directory.resolve().is_relative_to(
        made.session.workspace.resolve()
    )
    directory = made.session.private / "copy"
    directory.mkdir()
    artifact = _materialize(made.session, REPAIR, directory, ["helper"])
    copied = HarnessArtifact.load(directory / "subharnesses" / "helper")
    assert copied.licensed and copied.artifact_digest == child.ref.artifact_digest
    assert artifact.forge is not None and set(artifact.forge.subharnesses) == {"helper"}


async def test_projection_child_is_built_from_tuning_only(
    tmp_path: Path, monkeypatch: Any
) -> None:
    made = await make_rig(tmp_path, monkeypatch, play="ok", count=16)
    with wallet(made.session.settings) as open_:
        made.session.wallet = open_
        result = await spawn(
            made, name="proj", cases={"projection": {"input": "/text", "expected": ""}}
        )
    assert result["licensed"] is True and result["case_origin"] == "projection"
    dataset = (made.session.private / "spawn_datasets" / "proj.jsonl").read_text("utf-8")
    assert dataset.count("\n") == 8 and TUNING_MARK in dataset
    assert HELD_OUT_MARK not in dataset and SECRET not in dataset


async def test_unlicensed_or_unknown_child_is_unbindable(rig: Rig) -> None:
    rig.write(REPAIR.replace('return "ok"', 'return await context.harness("ghost", task)'))
    result = await rig.call("forge_submit")
    assert result["code"] == "forge_unbound_child"
    assert result["sandbox_code"] == "sandbox_binding_mismatch"
    assert not (rig.session.private / "candidates" / "1").exists()


async def test_promotion_copies_referenced_children(tmp_path: Path, monkeypatch: Any) -> None:
    made = await make_rig(tmp_path, monkeypatch, play="ok")
    with wallet(made.session.settings) as open_:
        made.session.wallet = open_
        await spawn(made)
    directory = made.session.private / "candidates" / "9"
    directory.mkdir(parents=True)
    artifact = _materialize(made.session, REPAIR, directory, ["helper"])
    artifact.save()
    from architect.compiler.types import CandidateEvaluation

    made.session.best = CandidateEvaluation(
        artifact=artifact, measurement=made.session.candidate.measurement
    )
    made.session.best_number = 9
    promoted = forge_module._promote(made.session, tmp_path / "artifacts")
    assert promoted.version == 2 and promoted.forge == artifact.forge
    child = HarnessArtifact.load(promoted.directory / "subharnesses" / "helper")
    assert child.licensed


def test_child_settings_disable_forge_at_depth() -> None:
    settings = ForgeSettings(enabled=True, trigger="always", max_depth=1)
    child = settings.child_settings(1)
    assert child.enabled is False and child.trigger == "failed"
    deeper = ForgeSettings(enabled=True, max_depth=2).child_settings(1)
    assert deeper.enabled is True
    with pytest.raises(ValueError):
        settings.child_settings(-1)


def test_spawn_settings_fill_money_and_time_with_the_allotment() -> None:
    parent = ForgeSettings(enabled=True, max_depth=2, max_usd=5, max_s=3600)
    child = spawn_settings(parent, 1, 0.4, 90.0)
    assert child.enabled is True and child.trigger == "failed"
    assert child.max_usd == 0.4 and child.max_s == 90.0 and child.spawn_max_usd == 0.4
    assert child.model_dump(exclude={"max_usd", "max_s", "spawn_max_usd"}) == (
        parent.child_settings(1).model_dump(exclude={"max_usd", "max_s", "spawn_max_usd"})
    )
    # Never above the parent's own caps either.
    small = ForgeSettings(enabled=True, max_usd=0.1, max_s=10)
    assert spawn_settings(small, 1, 1.0, 50.0).max_usd == 0.1
    assert spawn_settings(small, 1, 1.0, 50.0).max_s == 10


async def test_spawn_child_compile_is_sized_and_timed_by_the_allotment(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import asyncio
    import time

    from architect.compiler import Compiler

    settings = ForgeSettings(
        enabled=True, max_depth=2, max_usd=5, max_s=10, spawn_share=0.01, spawn_max_usd=2
    )
    made = await make_rig(tmp_path, monkeypatch, settings=settings, play="ok")
    seen: dict[str, Any] = {}

    async def slow_compile(self: Compiler, source: Any, **kwargs: Any) -> Any:
        seen.update(forge=self.config.forge, outer=self._outer_budget())
        seen["compile"] = self.config.compile_budget
        await asyncio.sleep(30)
        raise AssertionError("the spawn deadline did not fire")

    monkeypatch.setattr(Compiler, "compile", slow_compile)
    with wallet(settings) as open_:
        made.session.wallet = open_
        started = time.monotonic()
        result = await spawn(made)
        elapsed = time.monotonic() - started
    assert result["code"] == "budget_exceeded" and result["licensed"] is False
    assert elapsed < 5  # spawn_share * remaining_s is about 0.1 s: a real deadline
    child = seen["forge"]
    assert isinstance(child, ForgeSettings) and child.enabled is True
    assert child.max_usd <= 0.05 + 1e-9 and child.max_s <= 0.1 + 1e-9
    assert child.max_usd == seen["compile"].cost_usd and child.max_s == seen["compile"].compute_s
    # The child's outer wallet is sized by the spawn allotment, never the parent's caps.
    assert seen["outer"].cost_usd <= 0.1 + 1e-9 and seen["outer"].compute_s <= 0.2 + 1e-9
    assert made.session.spawns == 1 and "helper" not in made.session.children


def test_game_call_limit_uses_turns_times_calls_per_turn(tmp_path: Path) -> None:
    session = ForgeSession.__new__(ForgeSession)
    session.settings = ForgeSettings(calls_per_turn=3, call_limit=77)

    class Holder:
        pass

    for config, expected in (
        ({}, 77),
        ({"kind": "game", "limits": {"max_turns": 50}}, 150),
        ({"kind": "game"}, 256 * 3),
        ({"kind": "competition", "limits": {"max_turns": 90_000}}, 100_000),
    ):
        holder = Holder()
        holder.artifact = Holder()  # type: ignore[attr-defined]
        holder.artifact.spec = spec().model_copy(update={"execution_config": config})  # type: ignore[attr-defined]
        session.candidate = holder  # type: ignore[assignment]
        assert session.call_limit() == expected


def test_spawn_floor_is_standard_and_minimum_half(tmp_path: Path) -> None:
    records = [EvaluationCase(case_id=f"c{i:03d}", task="x", expected="ok") for i in range(8)]
    definition = FloorDefinition(
        kind=FloorKind.HELD_OUT_TASKS, limit=0.8, dataset="d", minimum_samples=4
    )
    floor = HeldOutTasks(definition, records)
    assert len(floor.tuning) == 4


async def test_referenced_child_runs_through_the_host_and_is_bound(
    tmp_path: Path, monkeypatch: Any
) -> None:
    made = await make_rig(tmp_path, monkeypatch, play="ok")
    source = REPAIR.replace('return "ok"', 'return await context.harness("helper", task["text"])')
    with wallet(made.session.settings) as open_:
        made.session.wallet = open_
        assert (await spawn(made))["licensed"] is True
        made.write(source)
        check = await made.call("forge_check")
        assert check["ok"] is True and check["unbound_children"] == []
        result = await made.call("forge_submit")
    assert result["measurement"]["floor_passed"] is True
    assert result["measurement"]["sample_count"] == 3
    # Same score as the passing Stage 2 preset but costlier and bigger: not better.
    assert result["code"] == "not_better"
    candidate = made.session.private / "candidates" / "1"
    bound = HarnessArtifact.load(candidate)
    assert bound.forge is not None and set(bound.forge.subharnesses) == {"helper"}
    assert bound.forge.subharnesses["helper"].case_origin == "authored"
    child = HarnessArtifact.load(candidate / "subharnesses" / "helper")
    assert child.licensed and child.artifact_digest == (
        bound.forge.subharnesses["helper"].artifact_digest
    )


async def test_result_observers_get_each_bounded_result(rig: Rig) -> None:
    from architect.compiler.forge_tools import add_result_observer, remove_result_observer

    seen: list[dict[str, Any]] = []
    add_result_observer(seen.append)
    try:
        rig.write(REPAIR)
        checked = await rig.call("forge_check")
        dry = await rig.call("forge_dryrun", case=0)
        submitted = await rig.call("forge_submit")
    finally:
        remove_result_observer(seen.append)
    assert [s["tool"] for s in seen] == ["forge_check", "forge_dryrun", "forge_submit"]
    assert [s["result"] for s in seen] == [checked, dry, submitted]
    assert seen[2]["result"]["left"] == submitted["left"] and seen[0]["depth"] == 0
    text = json.dumps(seen)
    assert SECRET not in text and HELD_OUT_MARK not in text
