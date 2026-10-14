"""Trusted evaluation measures disk code without exposing private floor data."""

import asyncio
import time
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import JsonValue

from architect.floors import EvaluationCase, FloorEvaluator, HeldOutTasks, HumanEditRate
from architect.floors.base import EditLogRow
from architect.models import FakeAdapter, ModelResponse
from architect.runtime import Runtime
from architect.runtime.evaluation import evaluate_cases
from architect.spec import Budget, FloorDefinition, FloorKind, HarnessArtifact, Spec
from architect.telemetry import TelemetryStore, categorical


def setup(
    tmp_path: Path,
    *,
    source: str = "async def run(context, task):\n    return task\n",
    budget: Budget | None = None,
    output_schema: dict[str, JsonValue] | None = None,
    limit: float = 1.0,
) -> tuple[HarnessArtifact, HeldOutTasks, Runtime]:
    definition = FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=limit, dataset="private-data")
    spec = Spec(
        task="private-description", input_schema={}, output_schema=output_schema or {},
        floor=definition, budget=budget or Budget(),
    )
    directory = tmp_path / "artifact"
    directory.mkdir()
    (directory / "harness.py").write_text(source, encoding="utf-8")
    artifact = HarnessArtifact(
        spec=spec, preset="toy", model="fake", components=(), directory=directory,
    )
    artifact.save()
    records = [
        EvaluationCase(
            case_id=str(i), task=f"private-task-{i}", expected=f"private-task-{i}",
            metadata={"private-metadata": f"secret-{i}"},
        )
        for i in range(6)
    ]
    floor = HeldOutTasks(definition, records)
    return artifact, floor, Runtime(TelemetryStore(tmp_path / "telemetry"))


async def test_disk_code_runs_every_task_and_emits_real_score(tmp_path: Path) -> None:
    artifact, floor, runtime = setup(tmp_path)
    measurement = await evaluate_cases(artifact, floor, floor.tuning, FakeAdapter, runtime)
    assert measurement.floor_passed and measurement.budget_passed
    assert measurement.score == 1
    assert measurement.sample_count == 3
    assert not measurement.failures
    assert not artifact.licensed
    assert not (artifact.directory / ".consumed").exists()
    rows = runtime.store.rows()
    assert len(rows) == 4
    assert all(row.floor_score is None for row in rows[:-1])
    assert rows[-1].task_type == categorical("quality_evaluation")
    assert rows[-1].floor_score == 1
    assert rows[-1].accepted
    journal = (runtime.store.directory / "runs.jsonl").read_text(encoding="utf-8")
    assert "private-" not in journal
    assert "secret-" not in journal


async def test_no_expected_or_metadata_and_no_mutation_of_case(tmp_path: Path) -> None:
    source = (
        "async def run(context, task):\n"
        "    assert set(task) == {'value'}\n"
        "    task['value'] += 1\n"
        "    return task\n"
    )
    artifact, original, runtime = setup(tmp_path, source=source)
    records = [
        EvaluationCase(case_id=str(i), task={"value": i}, expected={"value": i + 1},
                       metadata={"private-secret": "never-send"})
        for i in range(6)
    ]
    floor = HeldOutTasks(original.definition, records)
    cases = floor.tuning
    result = await evaluate_cases(artifact, floor, cases, FakeAdapter, runtime)
    assert result.floor_passed
    assert [case.task for case in cases] == [{"value": 0}, {"value": 2}, {"value": 4}]


async def test_scoring_uses_new_disk_outputs_not_reference_or_old_code(tmp_path: Path) -> None:
    artifact, floor, runtime = setup(tmp_path)
    (artifact.directory / "harness.py").write_text(
        "async def run(context, task):\n    return 'private-wrong-output'\n", encoding="utf-8",
    )
    result = await evaluate_cases(artifact, floor, floor.tuning, FakeAdapter, runtime)
    assert result.score == 0
    assert not result.floor_passed
    assert result.budget_passed
    assert result.failures == ("floor_not_met",)
    assert runtime.store.rows()[-1].floor_score == 0
    assert not runtime.store.rows()[-1].accepted


class RecordingFloor(HeldOutTasks):
    scored: tuple[str, ...] = ()

    async def score(self, cases: Sequence[EvaluationCase], outputs: Sequence[JsonValue]) -> float:
        self.scored = tuple(case.case_id for case in cases)
        return await super().score(cases, outputs)


async def test_full_selected_sequence_scored_once(tmp_path: Path) -> None:
    artifact, original, runtime = setup(tmp_path)
    records = [EvaluationCase(case_id=str(i), task=i, expected=i) for i in range(8)]
    floor = RecordingFloor(original.definition, records)
    result = await evaluate_cases(artifact, floor, floor.tuning, FakeAdapter, runtime)
    assert result.floor_passed
    assert floor.scored == ("0", "2", "4", "6")


@pytest.mark.parametrize("failure", ["runtime", "schema", "nonfinite"])
async def test_one_hard_case_invalidates_whole_candidate_and_later_cases_run(
    tmp_path: Path, failure: str,
) -> None:
    code = {
        "runtime": "raise ValueError('private-exception')",
        "schema": "return 42",
        "nonfinite": "return float('nan')",
    }[failure]
    source = (
        "async def run(context, task):\n"
        "    if task == 'private-task-2':\n"
        f"        {code}\n"
        "    return task\n"
    )
    artifact, floor, runtime = setup(
        tmp_path, source=source, output_schema={"type": "string"}, limit=0,
    )
    result = await evaluate_cases(artifact, floor, floor.tuning, FakeAdapter, runtime)
    assert not result.floor_passed
    assert result.sample_count == 3
    assert result.failures == (("runtime_error",) if failure == "runtime" else ("schema_error",))
    rows = runtime.store.rows()
    assert len(rows) == 4
    assert [row.accepted for row in rows] == [True, False, True, False]
    assert rows[-1].floor_score is None
    assert "private-exception" not in result.model_dump_json()


async def test_mean_and_maxima_use_per_case_actual_usage(tmp_path: Path) -> None:
    artifact, floor, runtime = setup(
        tmp_path, source="async def run(context, task):\n    return await context.call(task)\n",
    )
    adapter = FakeAdapter(responses=[
        ModelResponse(text=f"private-task-{i * 2}", tokens_in=i + 1, tokens_out=2,
                      cost_usd=0.01 * (i + 1), model="fake")
        for i in range(3)
    ])
    result = await evaluate_cases(artifact, floor, floor.tuning, lambda _: adapter, runtime)
    assert result.floor_passed
    assert result.mean_cost_usd == pytest.approx(0.02)
    assert result.max_cost_usd == pytest.approx(0.03)
    assert result.max_tokens == 5
    rows = runtime.store.rows()
    assert result.max_compute_s == max(row.wall_time_s for row in rows[:-1])
    assert sum(row.cost_usd for row in rows) == pytest.approx(0.06)
    assert sum(row.tokens_in for row in rows) == 6
    assert sum(row.tokens_out for row in rows) == 6
    assert rows[-1].tokens_in == rows[-1].tokens_out == rows[-1].cost_usd == 0


async def test_high_actual_tokens_do_not_fail_quality_or_budget(tmp_path: Path) -> None:
    artifact, floor, runtime = setup(
        tmp_path, source="async def run(context, task):\n    return await context.call(task)\n",
        budget=Budget.model_validate({"tokens": 1, "cost_usd": 0.01}),
    )
    adapter = FakeAdapter(responses=[
        ModelResponse(text=f"private-task-{i * 2}", tokens_in=2_000_000,
                      tokens_out=1_000_000, cost_usd=0.01, model="fake")
        for i in range(3)
    ])
    result = await evaluate_cases(artifact, floor, floor.tuning, lambda _: adapter, runtime)
    assert result.floor_passed and result.budget_passed
    assert result.failures == () and result.max_tokens == 3_000_000
    assert result.mean_tokens == 3_000_000
    assert result.max_cost_usd == pytest.approx(0.01)
    assert sum(row.tokens_in + row.tokens_out for row in runtime.store.rows()) == 9_000_000


async def test_denied_reservation_explains_failure_without_claiming_spent_usage(
    tmp_path: Path,
) -> None:
    artifact, floor, runtime = setup(
        tmp_path, source="async def run(context, task):\n    return await context.call(task)\n",
        budget=Budget(cost_usd=0.005),
    )
    adapter = FakeAdapter(responses=[
        ModelResponse(text="private-task-0", tokens_in=3, tokens_out=2,
                      cost_usd=0.01, model="fake"),
    ])
    result = await evaluate_cases(artifact, floor, floor.tuning, lambda _: adapter, runtime)
    assert not result.floor_passed and not result.budget_passed
    assert result.failures == ("budget_exceeded",)
    assert result.max_tokens == result.mean_tokens == 0
    assert result.max_cost_usd == 0.01
    assert result.mean_cost_usd == 0
    assert adapter.calls == 0
    assert len(runtime.store.rows()) == 4
    assert all(row.tokens_in == row.tokens_out == row.cost_usd == 0 for row in runtime.store.rows())


async def test_failure_after_consumption_preserves_usage(tmp_path: Path) -> None:
    source = (
        "async def run(context, task):\n"
        "    await context.call(task)\n"
        "    raise ValueError('private-error')\n"
    )
    artifact, floor, runtime = setup(tmp_path, source=source)
    adapter = FakeAdapter(responses=[
        ModelResponse(text="unused", tokens_in=3, tokens_out=2, cost_usd=0.01, model="fake")
    ] * 3)
    result = await evaluate_cases(artifact, floor, floor.tuning, lambda _: adapter, runtime)
    assert result.mean_cost_usd == pytest.approx(0.01)
    assert result.max_tokens == 5
    assert result.max_cost_usd == 0.01
    assert result.failures == ("runtime_error",)
    assert adapter.calls == 3


async def test_timeout_is_failed_budget_and_attempts_all_cases(tmp_path: Path) -> None:
    source = (
        "import asyncio\nasync def run(context, task):\n"
        "    await asyncio.Event().wait()\n"
    )
    artifact, floor, runtime = setup(tmp_path, source=source, budget=Budget(compute_s=0.05))
    result = await evaluate_cases(artifact, floor, floor.tuning, FakeAdapter, runtime)
    assert not result.floor_passed and not result.budget_passed
    # Run time is read from time.monotonic, the clock asyncio's timers use, and asyncio
    # fires a timer up to one clock tick early. On Windows a tick is 15.6 ms, so a run
    # stopped at its budget can measure one tick short of it.
    tick = time.get_clock_info("monotonic").resolution
    assert result.max_compute_s > 0.05 - tick
    assert len(runtime.store.rows()) == 4


async def test_mismatched_floor_is_refused_without_executing(tmp_path: Path) -> None:
    artifact, floor, runtime = setup(tmp_path, source="raise AssertionError('must-not-load')\n")
    other = HeldOutTasks(
        floor.definition.model_copy(update={"limit": 0.0}),
        [EvaluationCase(case_id=str(i), task=i, expected=i) for i in range(4)],
    )
    result = await evaluate_cases(artifact, other, other.tuning, FakeAdapter, runtime)
    assert result.failures == ("floor_mismatch",)
    assert not result.floor_passed
    assert len(runtime.store.rows()) == 1


async def test_explicit_secondary_floor_is_supported_without_rebinding_artifact(
    tmp_path: Path,
) -> None:
    artifact, floor, runtime = setup(tmp_path)
    secondary = HeldOutTasks(
        floor.definition.model_copy(update={"dataset": "other", "limit": 0.5}),
        [EvaluationCase(case_id=str(i), task=i, expected=i) for i in range(4)],
    )
    artifact = artifact.model_copy(update={
        "spec": artifact.spec.model_copy(update={"secondary_floor": secondary.definition}),
    })
    result = await evaluate_cases(artifact, secondary, secondary.tuning, FakeAdapter, runtime)
    assert result.floor_passed
    assert runtime.store.rows()[-1].floor_limit == 0.5
    assert runtime.store.rows()[-1].spec_hash == artifact.spec.spec_hash


async def test_empty_cases_never_pass_zero_floor(tmp_path: Path) -> None:
    artifact, floor, runtime = setup(tmp_path, limit=0)
    result = await evaluate_cases(artifact, floor, [], FakeAdapter, runtime)
    assert not result.floor_passed
    assert result.sample_count == 0
    assert result.failures == ("no_cases",)


async def test_unauthorized_cases_cannot_earn_score(tmp_path: Path) -> None:
    artifact, floor, runtime = setup(tmp_path, limit=0)
    cases = [EvaluationCase(case_id="made-up", task="x", expected="x")]
    result = await evaluate_cases(artifact, floor, cases, FakeAdapter, runtime)
    assert not result.floor_passed
    assert result.failures == ("floor_error",)


async def test_human_edit_failure_uses_worst_score_not_false_zero(tmp_path: Path) -> None:
    artifact, _, runtime = setup(
        tmp_path, source="async def run(context, task):\n    raise ValueError\n",
    )
    definition = FloorDefinition(kind=FloorKind.HUMAN_EDIT_RATE, dataset="edits", limit=1,
                                 minimum_samples=1)
    floor = HumanEditRate(definition, [
        EditLogRow(case_id=str(i), task=i, proposed=i, shipped=i, changed=False)
        for i in range(6)
    ])
    artifact = artifact.model_copy(update={
        "spec": artifact.spec.model_copy(update={"floor": definition}),
    })
    result = await evaluate_cases(artifact, floor, floor.tuning, FakeAdapter, runtime)
    assert result.score == 1
    assert not result.floor_passed
    assert runtime.store.rows()[-1].floor_score is None


@pytest.mark.parametrize("source", ["# missing entrypoint\n", "def run(c, t): return t\n"])
async def test_load_failure_is_private_failed_measurement(tmp_path: Path, source: str) -> None:
    artifact, floor, runtime = setup(tmp_path, source=source)
    result = await evaluate_cases(artifact, floor, floor.tuning, FakeAdapter, runtime)
    assert result.failures == ("load_error",)
    assert not result.floor_passed
    assert result.sample_count == 3


class BadScoreFloor(FloorEvaluator):
    async def score(self, cases: Sequence[EvaluationCase], outputs: Sequence[JsonValue]) -> float:
        return float("nan")


async def test_invalid_floor_score_is_refused(tmp_path: Path) -> None:
    artifact, original, runtime = setup(tmp_path)
    floor = BadScoreFloor(original.definition, [
        EvaluationCase(case_id=str(i), task=i, expected=i) for i in range(4)
    ])
    result = await evaluate_cases(artifact, floor, floor.tuning, FakeAdapter, runtime)
    assert result.failures == ("floor_error",)
    assert not result.floor_passed
    assert runtime.store.rows()[-1].floor_score is None


async def test_remote_schema_references_are_rejected_before_resolution(tmp_path: Path) -> None:
    artifact, floor, runtime = setup(tmp_path, output_schema={"$ref": "https://invalid/schema"})
    result = await evaluate_cases(artifact, floor, floor.tuning, FakeAdapter, runtime)
    assert result.failures == ("schema_error",)
    assert len(runtime.store.rows()) == 1


async def test_partial_consumption_and_second_denied_call_are_distinct(tmp_path: Path) -> None:
    source = (
        "async def run(context, task):\n"
        "    await context.call(task)\n"
        "    return await context.call(task)\n"
    )
    artifact, floor, runtime = setup(tmp_path, source=source, budget=Budget(cost_usd=0.015))
    adapters: list[FakeAdapter] = []

    def factory(model: str) -> FakeAdapter:
        adapter = FakeAdapter(model=model, responses=[
            ModelResponse(text="unused", tokens_in=3, tokens_out=2, cost_usd=0.01, model=model)
        ] * 2)
        adapters.append(adapter)
        return adapter

    result = await evaluate_cases(artifact, floor, floor.tuning, factory, runtime)
    assert not result.floor_passed and not result.budget_passed
    assert result.max_tokens == result.mean_tokens == 5
    assert result.max_cost_usd == 0.02
    assert result.mean_cost_usd == pytest.approx(0.01)
    assert [adapter.calls for adapter in adapters] == [1, 1, 1]
    rows = runtime.store.rows()
    assert sum(row.tokens_in + row.tokens_out for row in rows) == 15
    assert sum(row.cost_usd for row in rows) == pytest.approx(0.03)
    assert rows[-1].tokens_in == rows[-1].tokens_out == rows[-1].cost_usd == 0


async def test_human_edit_rate_scores_candidate_replays_with_inverse_threshold(
    tmp_path: Path,
) -> None:
    artifact, _, runtime = setup(tmp_path)
    definition = FloorDefinition(kind=FloorKind.HUMAN_EDIT_RATE, dataset="edits", limit=0.4,
                                 minimum_samples=1)
    floor = HumanEditRate(definition, [
        EditLogRow(case_id=str(i), task=i, proposed=i, shipped=i if i else 99, changed=i == 0)
        for i in range(6)
    ])
    artifact = artifact.model_copy(update={
        "spec": artifact.spec.model_copy(update={"floor": definition}),
    })
    result = await evaluate_cases(artifact, floor, floor.tuning, FakeAdapter, runtime)
    assert result.score == pytest.approx(1 / 3)
    assert result.floor_passed
    assert runtime.store.rows()[-1].floor_score == pytest.approx(1 / 3)
    assert runtime.store.rows()[-1].accepted


async def test_evaluation_does_not_scan_telemetry_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact, floor, runtime = setup(tmp_path)

    def no_scan() -> None:
        raise AssertionError("Evaluation must use task-local last_row")

    monkeypatch.setattr(runtime.store, "rows", no_scan)
    result = await evaluate_cases(artifact, floor, floor.tuning, FakeAdapter, runtime)
    assert result.floor_passed
    assert result.sample_count == 3


async def test_factory_failure_does_not_reuse_previous_runtime_usage(tmp_path: Path) -> None:
    artifact, floor, runtime = setup(
        tmp_path, source="async def run(context, task):\n    return await context.call(task)\n",
    )
    attempts = 0

    def factory(model: str) -> FakeAdapter:
        nonlocal attempts
        attempts += 1
        if attempts != 1:
            raise ValueError("private-factory-error")
        return FakeAdapter(model=model, responses=[
            ModelResponse(text="private-task-0", tokens_in=3, tokens_out=2,
                          cost_usd=0.01, model=model),
        ])

    result = await evaluate_cases(artifact, floor, floor.tuning, factory, runtime)
    assert result.failures == ("runtime_error",)
    assert result.mean_cost_usd == pytest.approx(0.01 / 3)
    assert result.max_tokens == 5
    assert len(runtime.store.rows()) == 2
    second = await evaluate_cases(artifact, floor, floor.tuning, factory, runtime)
    assert second.mean_cost_usd == 0
    assert second.max_tokens == second.max_cost_usd == second.max_compute_s == 0


async def test_concurrent_evaluations_use_their_own_failed_run_rows(tmp_path: Path) -> None:
    artifact, floor, runtime = setup(tmp_path, source=(
        "async def run(context, task):\n"
        "    await context.call(task)\n"
        "    raise ValueError('private-failure')\n"
    ))
    cheap = FakeAdapter(responses=[
        ModelResponse(text="unused", tokens_in=3, tokens_out=2, cost_usd=0.01, model="fake")
    ] * 3)
    costly = FakeAdapter(responses=[
        ModelResponse(text="unused", tokens_in=5, tokens_out=2, cost_usd=0.03, model="fake")
    ] * 3)
    first, second = await asyncio.gather(
        evaluate_cases(artifact, floor, floor.tuning, lambda _: cheap, runtime),
        evaluate_cases(artifact, floor, floor.tuning, lambda _: costly, runtime),
    )
    assert first.failures == second.failures == ("runtime_error",)
    assert first.mean_cost_usd == pytest.approx(0.01)
    assert second.mean_cost_usd == pytest.approx(0.03)
    assert first.max_tokens == 5
    assert second.max_tokens == 7
    assert sum(row.cost_usd for row in runtime.store.rows()) == pytest.approx(0.12)


async def test_only_safe_numeric_settings_reach_run_and_quality_telemetry(tmp_path: Path) -> None:
    artifact, floor, runtime = setup(tmp_path)
    artifact = artifact.model_copy(update={"params": {
        "temperature": 0.2, "context_window": 1024, "retries": 1,
        "verifier_depth": "private-setting", "private-key": 7,
        "secret": {"value": "private-content"},
    }})
    result = await evaluate_cases(artifact, floor, floor.tuning, FakeAdapter, runtime)
    assert result.floor_passed
    rows = runtime.store.rows()
    assert len(rows) == 4
    expected = {"temperature": 0.2, "context_window": 1024, "retries": 1}
    assert all(row.model_params == expected for row in rows)
    assert all(row.task_type == categorical(artifact.preset) for row in rows[:-1])
    assert rows[-1].task_type == categorical("quality_evaluation")
    journal = (runtime.store.directory / "runs.jsonl").read_text(encoding="utf-8")
    assert "private-" not in journal
