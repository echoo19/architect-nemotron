"""What the desktop's bar view and exam chart are fed, and the held-out rule around it (W6).

The live feed (the process-wide stage and Forge result observers) carries the ladder,
each rung's practice scores, Forge's tool results and Forge's harness drafts: tuning-side
data only. The exam reaches the page once, after ``compile()`` returns, as the result
card's ``data``: each rung's signed aggregate score and counts, never a case, a per-case
result or a verification id. Offline: fake play models and a scripted Pi session.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from forge_support import (
    HELD_OUT_MARK,
    REPAIR,
    SECRET,
    ScriptedPi,
    agent_adapter,
    ensure_sandbox,
    floor_factory,
    install_pi,
    reply,
    spec,
)

from architect.agent.build import FloorPlan, _exam_data, _plan_data
from architect.agent.events import PlanCard
from architect.backends.pi import PiConfig
from architect.compiler import (
    CompileConfig,
    Compiler,
    CompileResult,
    _rungs,
    add_stage_observer,
    remove_stage_observer,
)
from architect.compiler.forge_settings import ForgeSettings
from architect.compiler.forge_tools import add_result_observer, remove_result_observer
from architect.desktop.bridge import encode_event
from architect.library import HarnessLibrary
from architect.models import FakeAdapter
from architect.runtime import Runtime
from architect.session.state import FloorSummary
from architect.spec import Budget, Spec, TestReport
from architect.telemetry import TelemetryStore

LADDER = ("fake", "llama3.2", "gpt-4o-mini")
HELD_OUT_IDS = ("case-001", "case-003", "case-005")  # odd positions (forge_support.cases)
SESSION = [
    {"write": {"path": "harness.py", "content": REPAIR}},
    {"tool": "forge_check", "arguments": {}},
    {"tool": "forge_measure", "arguments": {"cases": [0]}},
    {"tool": "forge_submit", "arguments": {"note": "three guarded calls"}},
    {"text": "done"},
]
STAGE_KEYS = {"stage", "stages", "depth", "model", "model_index", "model_count", "ladder",
              "tuned", "examined"}
COUNT_KEYS = {"cases", "correct", "invalid", "over_budget"}
RUNG_KEYS = {"model", "preset", "licensed", "ships", "score", "limit", "cases", "counts", "codes",
             "mean_cost_usd"}
REPO = Path(__file__).parent.parent
VIDEO_3 = REPO / "architect" / "published" / "receipts" / "ladder-2026-10-06-video-3"
RECEIPT_READER = REPO / "examples" / "expense-app" / "receipt-reader.json"
NEMOTRON = (
    "nvidia/Nemotron-3_5-Lightning",
    "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/Nemotron-3-Ultra-550b-a55b",
)


@pytest.fixture(autouse=True)
async def working_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    await ensure_sandbox(monkeypatch, tmp_path / "probe")


def plays(replies: dict[str, str]) -> Callable[[str], FakeAdapter]:
    def factory(model: str) -> FakeAdapter:
        return FakeAdapter(model, responses=[reply(replies[model], model=model)] * 4096)

    return factory


def ladder_compiler(root: Path, play: Callable[[str], FakeAdapter]) -> Compiler:
    floors = floor_factory(6)
    return Compiler(
        root / "artifacts",
        Runtime(TelemetryStore(root / "telemetry")),
        play,
        floors,
        config=CompileConfig.model_validate(
            {
                "model_ladder": LADDER,
                "max_candidates": 1,
                "enable_rewriting": False,
                "max_rewrites": 0,
                "compile_budget": Budget(cost_usd=10, compute_s=600),
                "forge": ForgeSettings(enabled=True, max_rungs=1),
            }
        ),
        library=HarnessLibrary(root / "library"),
        smoke_factory=floors,
        forge_adapter=agent_adapter(),
        pi_config=PiConfig(),
    )


async def observed_compile(
    root: Path, monkeypatch: pytest.MonkeyPatch, replies: dict[str, str]
) -> tuple[CompileResult, list[dict[str, Any]], list[dict[str, Any]]]:
    install_pi(monkeypatch, ScriptedPi(SESSION))
    stages: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []
    add_stage_observer(stages.append)
    add_result_observer(tools.append)
    try:
        result = await ladder_compiler(root, plays(replies)).compile(
            spec(model_target="cheapest that passes")
        )
    finally:
        remove_stage_observer(stages.append)
        remove_result_observer(tools.append)
    return result, stages, tools


def verification_ids(root: Path) -> set[str]:
    """Every verification id Stage 4 issued in this compile (each rung's report.json)."""
    found = set()
    for path in (root / "artifacts").rglob("report.json"):
        found.add(json.loads(path.read_text(encoding="utf-8"))["verification_id"])
    return found


def assert_no_held_out(text: str, ids: set[str]) -> None:
    assert HELD_OUT_MARK not in text
    assert SECRET not in text
    assert not any(case in text for case in HELD_OUT_IDS)
    assert "verification_id" not in text
    assert ids and not any(value in text for value in ids)


async def test_the_live_feed_has_the_ladder_and_practice_scores_and_nothing_held_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Lightning fails practice, so Forge works on it; the other rungs pass as they are.
    result, stages, tools = await observed_compile(
        tmp_path, monkeypatch, {"fake": "nope", "llama3.2": "ok", "gpt-4o-mini": "ok"}
    )
    assert result.artifact is not None and result.artifact.licensed

    # The ladder in order on every rung's stages, and its practice scores from Stage 3 on.
    assert all(set(data) <= STAGE_KEYS for data in stages)
    assert {tuple(data["ladder"]) for data in stages} == {LADDER}
    first = [data for data in stages if data["model"] == "fake"]
    assert [data["stage"] for data in first] == [1, 2, 3, 4]
    assert first[2]["tuned"] == {"score": 0.0, "passed": False}  # the best preset, Stage 2
    assert first[3]["tuned"] == {"score": 0.0, "passed": False}
    assert first[3]["examined"] == {"score": 1.0, "passed": True}  # Forge's harness
    later = [data for data in stages if data["model"] == "llama3.2"]
    assert [data["stage"] for data in later] == [1, 2, 4]
    assert later[-1]["examined"] == later[-1]["tuned"] == {"score": 1.0, "passed": True}

    # Forge's harness draft reaches observers once per version, with the tool that saw it.
    drafts = [(data["tool"], data["source"]) for data in tools if "source" in data]
    assert len(drafts) == 1 and drafts[0][0] == "forge_check"
    assert drafts[0][1]["text"] == REPAIR and drafts[0][1]["path"] == "harness.py"
    assert all(set(data) <= {"tool", "depth", "event", "result", "source"} for data in tools)

    # Nothing from the exam: no held-out text, metadata, case id or verification id.
    ids = verification_ids(tmp_path)
    assert len(ids) == len(LADDER)
    assert_no_held_out(json.dumps(stages) + json.dumps(tools), ids)


async def test_the_exam_reaches_the_page_after_the_compile_as_counts_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Forge repairs Lightning; every rung is licensed and the cheapest one ships.
    result, _, _ = await observed_compile(
        tmp_path, monkeypatch, {"fake": "nope", "llama3.2": "ok", "gpt-4o-mini": "ok"}
    )
    rungs = result.rungs
    assert [rung.model for rung in rungs] == list(LADDER)
    assert all(rung.counts is not None and rung.cases == 3 for rung in rungs)
    assert sum(rung.ships for rung in rungs) == 1
    shipped = next(rung for rung in rungs if rung.ships)
    assert result.artifact is not None and shipped.model == result.artifact.model
    assert "rungs" not in result.model_dump(mode="json")  # a compile's JSON keeps its keys

    report = result.artifact.report
    assert report is not None and report.held_out_counts is not None
    data = _exam_data(result, result.spec, report, licensed=True)
    assert data["licensed"] is True and data["score"] == report.floor_score
    exam = data["exam"]
    assert isinstance(exam, dict) and set(exam) == COUNT_KEYS
    assert exam == report.held_out_counts.model_dump(mode="json")
    listed = data["rungs"]
    assert isinstance(listed, list) and len(listed) == len(LADDER)
    for row, rung in zip(listed, rungs, strict=True):
        assert set(row) == RUNG_KEYS
        assert row["counts"] is None or set(row["counts"]) == COUNT_KEYS
        # Each rung's mean cost of one exam case, from its own signed report.
        assert rung.mean_cost_usd is not None and rung.mean_cost_usd >= 0
        assert row["mean_cost_usd"] == rung.mean_cost_usd
    assert listed[-1]["mean_cost_usd"] == report.mean_cost_usd  # gpt-4o-mini's own report

    # What the bridge writes to the page for the result card.
    sent = json.dumps(encode_event(PlanCard("Result: x v1", (("floor", "1.00"),), data=data)))
    assert '"rungs"' in sent
    assert_no_held_out(sent, verification_ids(tmp_path))


async def test_rungs_say_why_one_stopped_before_its_exam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from architect.compiler import selection

    real = selection.select_preset

    def picky(spec_value: Any, model: str, library: Any) -> Any:
        if model == "llama3.2":
            raise selection.SelectionError("no preset for this model")
        return real(spec_value, model, library)

    monkeypatch.setattr(selection, "select_preset", picky)
    result, stages, _ = await observed_compile(
        tmp_path, monkeypatch, {"fake": "ok", "llama3.2": "ok", "gpt-4o-mini": "ok"}
    )
    middle = result.rungs[1]
    assert middle.model == "llama3.2" and middle.codes == ("selection_failed",)
    assert middle.score is None and middle.counts is None and not middle.licensed
    assert [data["stage"] for data in stages if data["model"] == "llama3.2"] == [1]


def test_each_rung_carries_its_signed_mean_cost_as_the_published_exams_say() -> None:
    """B1: the demo build's three exams (video-3), as the result card sends them. Counts and
    costs equal the signed ``exam-*.json``; the RungOutcome is not signed, the reports are
    untouched."""
    reports = [
        TestReport.model_validate_json((VIDEO_3 / f"exam-{tier}.json").read_text("utf-8"))
        for tier in ("lightning", "super", "ultra")
    ]
    ends: list[tuple[str, Any]] = [
        (model, SimpleNamespace(report=report, preset="extractor", licensed=report.passed))
        for model, report in zip(NEMOTRON, reports, strict=True)
    ]
    winner = ends[1][1]
    rungs = _rungs(ends, winner)
    assert [rung.mean_cost_usd for rung in rungs] == [r.mean_cost_usd for r in reports]
    receipt = Spec.model_validate_json(RECEIPT_READER.read_text("utf-8"))
    result = CompileResult.model_construct(spec=receipt, artifact=None, rungs=rungs)
    data = _exam_data(result, receipt, reports[1], licensed=True)
    rows = data["rungs"]
    assert isinstance(rows, list)
    assert [row["counts"] for row in rows] == [
        {"cases": 100, "correct": 39, "invalid": 0, "over_budget": 0},
        {"cases": 100, "correct": 64, "invalid": 0, "over_budget": 0},
        {"cases": 100, "correct": 72, "invalid": 0, "over_budget": 0},
    ]
    assert [row["mean_cost_usd"] for row in rows] == [
        5.83056e-05, 0.0002782949999999999, 0.0009273200000000001
    ]
    assert [row["ships"] for row in rows] == [False, True, False]
    assert [row["licensed"] for row in rows] == [False, True, True]


def test_the_plan_card_carries_the_task_rules_fields_origin_and_list_prices() -> None:
    """B2: what the Plan screen and the rules sheet read, for receipt-reader.json on the
    Nemotron ladder. Plan-time values only: the spec's own text, its check sentences, its
    schemas in order, the floor's origin and the bundled list prices."""
    receipt = Spec.model_validate_json(RECEIPT_READER.read_text("utf-8"))
    profile = SimpleNamespace(
        compile=SimpleNamespace(model_ladder=NEMOTRON),
        models={},
        resolve=lambda path: path,
    )
    summary = FloorSummary(
        dataset="bench-receipts-v2-evidence", kind=receipt.floor.kind, path=Path("cases.json"),
        count=200, tuning=100, held_out=100, synthetic=0, drafted_by_model=None,
        misses_allowed=None,
    )
    floor = FloorPlan(
        dataset="bench-receipts-v2-evidence", summary=summary, origin="public_dataset",
        text="CORD v2 · CC BY 4.0 · 200 receipts (100 tuning / 100 held-out)",
    )
    services: Any = SimpleNamespace(profile=profile)
    data = _plan_data(
        receipt, services, floor, None, Budget(cost_usd=0.6, compute_s=3600),
        frozen="receipt-reader.json",
    )
    assert data["ladder"] == list(NEMOTRON) and data["frozen"] == "receipt-reader.json"
    assert data["task"] == receipt.task
    assert data["checks"] == [
        'Amounts keep every printed digit, so "226,500.00" is 22650000.',
        "A subtotal equal to the total is null unless the receipt prints that amount as a "
        "subtotal.",
    ]
    assert data["inputs"] == [{"name": "receipt", "type": "string", "nullable": False}]
    assert data["outputs"] == [
        {"name": "total", "type": "integer", "nullable": True},
        {"name": "subtotal", "type": "integer", "nullable": True},
        {"name": "tax", "type": "integer", "nullable": True},
        {"name": "item_count", "type": "integer", "nullable": False},
    ]
    assert data["origin"] == "public_dataset"
    assert data["prices"] == {
        "nvidia/Nemotron-3_5-Lightning": [0.06, 0.24],
        "nvidia/nemotron-3-super-120b-a12b": [0.3, 0.9],
        "nvidia/Nemotron-3-Ultra-550b-a55b": [1.0, 3.0],
    }

    # A model the price table does not list is left out, and with none priced, the key.
    profile.compile = SimpleNamespace(model_ladder=("no-such-model",))
    data = _plan_data(receipt, services, floor, None, Budget(cost_usd=0.6, compute_s=3600))
    assert "prices" not in data and data["ladder"] == ["no-such-model"]
