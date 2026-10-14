"""Integrator-owned Phase 1 acceptance: no compiler involved."""
from pathlib import Path

import pytest

from architect.models import FakeAdapter
from architect.runtime import BudgetExceeded, RunContext, Runtime
from architect.spec import Budget, FloorDefinition, FloorKind, Spec
from architect.telemetry import TelemetryRow, TelemetryStore


@pytest.mark.asyncio
async def test_toy_budget_stops_mid_run_and_logs_no_content(tmp_path: Path) -> None:
    secret = "private-input-do-not-log"
    spec = Spec(
        task="toy", input_schema={}, output_schema={},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="synthetic"),
        budget=Budget(cost_usd=0.015, compute_s=3),
    )
    adapter = FakeAdapter(fixture=Path(__file__).parent / "fixtures" / "toy.json")
    store = TelemetryStore(tmp_path / "telemetry")
    runtime = Runtime(store)

    async def toy(context: RunContext, task: object) -> str:
        await context.call(secret)
        return await context.call(secret)

    with pytest.raises(BudgetExceeded):
        await runtime.evaluate(spec, toy, secret, adapter)
    assert adapter.calls == 1
    rows = store.rows()
    assert len(rows) == 1
    assert not rows[0].accepted
    assert rows[0].tokens_in + rows[0].tokens_out == 10
    assert rows[0].cost_usd == pytest.approx(0.01)
    assert TelemetryRow.model_validate_json(rows[0].model_dump_json()) == rows[0]
    for file in (tmp_path / "telemetry").iterdir():
        assert secret.encode() not in file.read_bytes()
