"""Integrator-owned preset/floor acceptance, including stage4-only view."""
from pathlib import Path

import pytest

from architect.floors import (
    CalibrationRow,
    EvaluationCase,
    HeldOutAccessError,
    HeldOutGameScore,
    HeldOutTasks,
    HumanEditRate,
    LLMJudge,
)
from architect.floors._access import _VERIFIER_CAPABILITY
from architect.models import FakeAdapter, ModelResponse
from architect.presets import all_presets
from architect.runtime import Runtime, load_harness
from architect.spec import Budget, FloorDefinition, FloorKind, Spec
from architect.telemetry import TelemetryStore


def preset_adapter(name: str) -> FakeAdapter:
    if name != "hypothesis_tester":
        return FakeAdapter()
    return FakeAdapter(responses=[
        ModelResponse(
            text=text, tokens_in=1, tokens_out=1, cost_usd=0.000002, model="fake",
        )
        for text in (
            '{"hypothesis":"left opens door","prediction":"door changes","test":"press left"}',
            "ok",
        )
    ])


async def test_all_registered_presets_execute_emitted_code(tmp_path: Path) -> None:
    runtime = Runtime(TelemetryStore(tmp_path / "runs"))
    presets = all_presets()
    assert {preset.name for preset in presets} == {
        "extractor", "synthesizer", "recommender", "coder", "explorer", "planner", "verifier",
        "hypothesis_tester",
    }
    for preset in presets:
        spec = Spec(
            task="Return a short result", input_schema={}, output_schema={"type": "string"},
            budget=Budget(),
            floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="synthetic"),
        )
        directory = tmp_path / preset.name
        # Disable optional secondary calls for this common trivial task.
        params = {key: 0 for key in ("verifier_depth", "retries") if key in preset.tunable_params}
        preset.emit(directory, spec, params=params, components=tuple(
            c for c in preset.components if c not in preset.removable_components
        ))
        assert (directory / "harness.py").exists()
        result = await runtime.evaluate(
            spec, load_harness(directory), {"query": "task"}, preset_adapter(preset.name),
            preset=preset.name, components=preset.components,
        )
        assert result.output is not None
    assert len(runtime.store.rows()) == 8


async def test_four_floors_score_and_isolate_held_out(tmp_path: Path) -> None:
    records = [EvaluationCase(case_id=f"case-{i}", task=i, expected="ok") for i in range(8)]
    runtime = Runtime(TelemetryStore(tmp_path / "runs"))
    tasks = HeldOutTasks(
        FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="tasks"), records,
    )
    edits = HumanEditRate.from_jsonl(
        FloorDefinition(kind=FloorKind.HUMAN_EDIT_RATE, limit=0.1, dataset="edits"),
        Path(__file__).parent / "fixtures" / "edit_log.jsonl",
    )
    judge = LLMJudge(
        FloorDefinition(kind=FloorKind.LLM_JUDGE, limit=0.8, dataset="judge",
                        rubric="Score correctness from zero to one.", judge_model="fake",
                        calibration_dataset="human-grades"),
        records, [CalibrationRow(case_id="cal-1", task="sample", output="ok", human_score=1)],
        FakeAdapter(responses=[ModelResponse(text="1", tokens_in=1, tokens_out=1,
                                             cost_usd=0.000002, model="fake") for _ in range(50)]),
        runtime,
    )

    class Game:
        async def score(self, task: object, output: object) -> float:
            return float(output == "ok")

    game = HeldOutGameScore(
        FloorDefinition(kind=FloorKind.HELD_OUT_GAME_SCORE, limit=0.8, dataset="games"),
        records, Game(),
    )
    for floor in (tasks, edits, judge, game):
        with pytest.raises(HeldOutAccessError):
            _ = floor.held_out
        with pytest.raises(HeldOutAccessError):
            floor._verification_cases(object())
        tuning = floor.tuning
        held_out = floor._verification_cases(_VERIFIER_CAPABILITY)
        assert {row.case_id for row in tuning}.isdisjoint(row.case_id for row in held_out)
        assert floor.passes(await floor.score(tuning, ["ok"] * len(tuning)))
        assert floor.passes(await floor.score(held_out, ["ok"] * len(held_out)))


@pytest.mark.parametrize("preset_name", [
    "extractor", "synthesizer", "recommender", "coder", "explorer", "planner", "verifier",
    "hypothesis_tester",
])
async def test_each_preset_can_fail_quality_floor(tmp_path: Path, preset_name: str) -> None:
    from architect.presets import get_preset

    definition = FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="tasks")
    floor = HeldOutTasks(definition, [
        EvaluationCase(case_id=f"case-{i}", task={"query": "input"}, expected="correct")
        for i in range(4)
    ])
    preset = get_preset(preset_name)
    spec = Spec(task="Produce correct result", input_schema={},
                output_schema={"type": "string"}, floor=definition, budget=Budget())
    params = {"verifier_depth": 0} if "verifier_depth" in preset.tunable_params else {}
    harness = preset.build(spec, params=params, components=tuple(
        c for c in preset.components if c not in preset.removable_components
    ))
    runtime = Runtime(TelemetryStore(tmp_path / "runs"))
    outputs = []
    for case in floor.tuning:
        result = await runtime.evaluate(spec, harness, case.task, preset_adapter(preset_name))
        outputs.append(result.output)
    assert not floor.passes(await floor.score(floor.tuning, outputs))
