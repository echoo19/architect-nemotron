"""Substantive negative cases for remaining measurement and sandbox gates."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

from architect.floors import CalibrationRow, EvaluationCase, HeldOutTasks, LLMJudge
from architect.models import FakeAdapter, ModelResponse
from architect.presets import get_preset
from architect.runtime import Runtime
from architect.runtime.evaluation import evaluate_cases
from architect.runtime.sandbox import SandboxError, sandbox_harness
from architect.runtime.wallet import compilation_wallet
from architect.spec import Budget, FloorDefinition, FloorKind, HarnessArtifact, PipelineStep, Spec
from architect.telemetry import TelemetryRow, TelemetryStore


class CountingFloor(HeldOutTasks):
    def __init__(self, definition: FloorDefinition, records: Sequence[EvaluationCase]) -> None:
        super().__init__(definition, records)
        self.score_calls = 0

    async def score(self, cases: Sequence[EvaluationCase], outputs: Sequence[JsonValue]) -> float:
        self.score_calls += 1
        return await super().score(cases, outputs)


def records() -> list[EvaluationCase]:
    return [EvaluationCase(case_id=f"case-{i}", task={"index": i}, expected="ok") for i in range(4)]


def response(text: str = "ok", *, cost: float = 0.01) -> ModelResponse:
    return ModelResponse(text=text, model="fake", tokens_in=2, tokens_out=1, cost_usd=cost)


def candidate(
    tmp_path: Path,
    *,
    input_schema: dict[str, JsonValue] | None = None,
    definition: FloorDefinition | None = None,
    pipeline: bool = False,
) -> HarnessArtifact:
    definition = definition or FloorDefinition(
        kind=FloorKind.HELD_OUT_TASKS,
        dataset="trusted-cases",
        limit=1,
        minimum_samples=1,
    )
    spec = Spec(
        task="Return a generic response",
        input_schema=input_schema or {"type": "object"},
        output_schema={"type": "string"},
        floor=definition,
        budget=Budget(cost_usd=1, compute_s=30),
        pipeline=(
            PipelineStep(
                name="one",
                preset="synthesizer",
                model="fake",
                budget=Budget(cost_usd=1, compute_s=10),
                output_schema={"type": "string"},
            ),
        )
        if pipeline
        else (),
    )
    preset = get_preset("synthesizer")
    directory = tmp_path / "artifact"
    if pipeline:
        preset.emit(directory, spec)
    else:
        directory.mkdir(parents=True)
        (directory / "harness.py").write_text(
            "async def run(context, task):\n    return await context.call('PRIVATE-PROMPT')\n"
        )
    artifact = HarnessArtifact(
        spec=spec,
        preset=preset.name,
        model="fake",
        components=preset.components,
        params=preset.validate_params({}),
        directory=directory,
        execution_mode="sandbox" if pipeline else "native",
    )
    artifact.save()
    return artifact


async def test_invalid_case_input_is_rejected_before_harness_or_floor_scoring(
    tmp_path: Path,
) -> None:
    artifact = candidate(tmp_path, input_schema={"type": "object", "required": ["required-input"]})
    floor = CountingFloor(artifact.spec.floor, records())
    adapter = FakeAdapter(responses=[response()])
    with TelemetryStore(tmp_path / "telemetry") as store:
        measurement = await evaluate_cases(
            artifact, floor, [floor.tuning[0]], lambda _: adapter, Runtime(store)
        )
        assert not measurement.floor_passed and measurement.failures == ("schema_error",)
        assert measurement.budget_passed and measurement.mean_cost_usd == 0
        assert len(store.rows()) == 2 and all(not row.accepted for row in store.rows())
    assert adapter.calls == 0 and floor.score_calls == 0


async def test_nonlocal_reference_nested_in_schema_array_is_rejected_before_execution(
    tmp_path: Path,
) -> None:
    artifact = candidate(
        tmp_path,
        input_schema={
            "allOf": [
                {"type": "object"},
                {"properties": {"data": {"$ref": "https://invalid.example/private-schema"}}},
            ]
        },
    )
    floor = CountingFloor(artifact.spec.floor, records())
    adapter = FakeAdapter(responses=[response()])
    factories = 0

    def factory(model: str) -> FakeAdapter:
        nonlocal factories
        factories += 1
        return adapter

    with TelemetryStore(tmp_path / "telemetry") as store:
        measurement = await evaluate_cases(
            artifact, floor, [floor.tuning[0]], factory, Runtime(store)
        )
        assert measurement.failures == ("schema_error",) and not measurement.floor_passed
        assert len(store.rows()) == 1 and not store.rows()[0].accepted
        assert "private-schema" not in store.rows()[0].model_dump_json()
    assert factories == adapter.calls == floor.score_calls == 0


async def test_factory_model_mismatch_cannot_be_scored_or_invoke_provider(tmp_path: Path) -> None:
    artifact = candidate(tmp_path)
    floor = CountingFloor(artifact.spec.floor, records())
    adapter = FakeAdapter(responses=[response()])
    adapter.model = "unapproved-model"
    with TelemetryStore(tmp_path / "telemetry") as store:
        measurement = await evaluate_cases(
            artifact, floor, [floor.tuning[0]], lambda _: adapter, Runtime(store)
        )
        assert measurement.failures == ("runtime_error",) and not measurement.floor_passed
        assert measurement.mean_cost_usd == measurement.max_tokens == 0
        assert len(store.rows()) == 1 and not store.rows()[0].accepted
    assert adapter.calls == floor.score_calls == 0


class MissingReceiptRuntime(Runtime):
    """Fault injection: execute and bill real work, but lose its attribution receipt."""

    @property
    def last_row(self) -> TelemetryRow | None:
        return None


async def test_actual_execution_with_missing_accounting_receipt_cannot_pass_floor(
    tmp_path: Path,
) -> None:
    artifact = candidate(tmp_path)
    floor = CountingFloor(artifact.spec.floor, records())
    adapter = FakeAdapter(responses=[response(cost=0.03)])
    with TelemetryStore(tmp_path / "telemetry") as store:
        measurement = await evaluate_cases(
            artifact,
            floor,
            [floor.tuning[0]],
            lambda _: adapter,
            MissingReceiptRuntime(store),
        )
        assert not measurement.floor_passed and measurement.failures == ("runtime_error",)
        rows = store.rows()
        assert len(rows) == 2 and rows[0].accepted and not rows[1].accepted
        assert rows[0].cost_usd == pytest.approx(0.03) and rows[0].tokens_in == 2
        assert rows[1].cost_usd == rows[1].tokens_in == 0
    assert adapter.calls == 1 and floor.score_calls == 0


async def test_real_calibrated_judge_money_denial_cannot_pass_candidate_floor(
    tmp_path: Path,
) -> None:
    definition = FloorDefinition(
        kind=FloorKind.LLM_JUDGE,
        dataset="trusted-cases",
        limit=0.8,
        minimum_samples=1,
        rubric="Fixed synthetic rubric",
        judge_model="fake",
        calibration_dataset="trusted-human",
    )
    artifact = candidate(tmp_path, definition=definition)
    judge = FakeAdapter(responses=[response("0.9")] * 4)
    adapter = FakeAdapter(responses=[response()])
    with TelemetryStore(tmp_path / "telemetry") as store:
        runtime = Runtime(store)
        floor = LLMJudge(
            definition,
            records(),
            [
                CalibrationRow(
                    case_id="calibration",
                    task={},
                    output="ok",
                    human_score=0.9,
                )
            ],
            judge,
            runtime,
        )
        selected = [floor.tuning[0]]
        # Calibrate through actual guarded calls, not private state mutation.
        assert await floor.score(selected, ["ok"]) == pytest.approx(0.9)
        assert judge.calls == 2
        before = len(store.rows())
        with compilation_wallet(Budget(cost_usd=0.015, compute_s=30)) as wallet:
            measurement = await evaluate_cases(
                artifact, floor, selected, lambda _: adapter, runtime
            )
        assert not measurement.floor_passed and not measurement.budget_passed
        assert measurement.failures == ("budget_exceeded",)
        assert measurement.mean_cost_usd == pytest.approx(0.01)
        assert measurement.max_cost_usd >= 0.02
        assert wallet.exhausted and wallet.cost_usd == pytest.approx(0.01)
        new_rows = store.rows()[before:]
        assert len(new_rows) == 3 and sum(row.cost_usd for row in new_rows) == pytest.approx(0.01)
        assert not new_rows[-1].accepted and new_rows[-1].floor_score is None
    assert adapter.calls == 1 and judge.calls == 2


async def test_sandbox_rejects_65th_preset_rpc_after_64_real_pipeline_executions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = candidate(tmp_path, pipeline=True)
    adapter = FakeAdapter(responses=[response(cost=0.001)] * 65)
    program = (
        "import json,sys\n"
        "sys.stdin.readline()\n"
        "for _ in range(65):\n"
        "    print(json.dumps({'op':'preset','task':{}}),flush=True)\n"
        "    sys.stdin.readline()\n"
    )
    spawn = asyncio.create_subprocess_exec
    processes: list[asyncio.subprocess.Process] = []

    async def peer(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        assert "architect.runtime._sandbox_worker" in args
        process = await spawn(sys.executable, "-I", "-u", "-c", program, **kwargs)
        processes.append(process)
        return process

    with TelemetryStore(tmp_path / "telemetry") as store:
        with monkeypatch.context() as scoped:
            # Real hostile wire peer; the parent still executes every actual pipeline.
            scoped.setattr(asyncio, "create_subprocess_exec", peer)
            with pytest.raises(SandboxError, match="sandbox_invalid_call"):
                await Runtime(store, adapter_factory=lambda _: adapter).evaluate(
                    artifact.spec,
                    sandbox_harness(artifact),
                    {},
                    adapter,
                )
        assert adapter.calls == 64
        assert len(store.rows()) == 1 and not store.rows()[0].accepted
        assert store.rows()[0].tokens_in == 128 and store.rows()[0].tokens_out == 64
        assert store.rows()[0].cost_usd == pytest.approx(0.064)
    assert len(processes) == 1 and processes[0].returncode is not None
