import asyncio
from pathlib import Path

import pytest

from architect.models import FakeAdapter, ModelRequest, ModelResponse
from architect.runtime import BudgetExceeded, RunContext, Runtime
from architect.spec import Budget, FloorDefinition, FloorKind, Spec
from architect.telemetry import TelemetryStore


def response() -> ModelResponse:
    return ModelResponse(text="ok", tokens_in=6, tokens_out=0, cost_usd=.001, model="fake")


async def test_concurrent_children_reserve_parent_before_spending() -> None:
    release = asyncio.Event()
    entered = asyncio.Event()

    class DelayedFake(FakeAdapter):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            entered.set()
            await release.wait()
            return await super().complete(request)

    parent = RunContext(FakeAdapter(), Budget(cost_usd=0.0015))
    slow = DelayedFake(responses=[response()])
    fast = FakeAdapter(responses=[response()])
    first, second = parent.fork(slow, Budget()), parent.fork(fast, Budget())
    task = asyncio.create_task(first.call("first"))
    await entered.wait()
    with pytest.raises(BudgetExceeded):
        await second.call("second")
    assert fast.calls == 0
    release.set()
    assert await task == "ok"
    assert parent.tokens_in == 6
    await first._close()
    await second._close()
    parent.absorb(first)
    parent.absorb(second)
    assert parent.tokens_in == 6
    with pytest.raises(ValueError):
        parent.absorb(first)
    with pytest.raises(BudgetExceeded):
        parent._check()


async def test_nested_runtime_work_cannot_reset_outer_run_wallet(tmp_path: Path) -> None:
    runtime = Runtime(TelemetryStore(tmp_path))
    spec = Spec(task="task", input_schema={}, output_schema={}, budget=Budget(cost_usd=0.0015),
                floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="data"))
    child_adapter = FakeAdapter(responses=[response()])
    outer_adapter = FakeAdapter(responses=[response()])

    async def child(context: RunContext, task: object) -> str:
        return await context.call("child")

    async def outer(context: RunContext, task: object) -> str:
        await runtime.evaluate(spec, child, None, child_adapter)
        return await context.call("outer")

    with pytest.raises(BudgetExceeded):
        await runtime.evaluate(spec, outer, None, outer_adapter)
    assert child_adapter.calls == 1 and outer_adapter.calls == 0
    assert sum(row.tokens_in for row in runtime.store.rows()) == 6


async def test_high_child_usage_is_reconciled_without_a_token_ceiling() -> None:
    budget = Budget.model_validate({"tokens": 1, "cost_usd": 0.002})
    parent = RunContext(FakeAdapter(), budget)
    adapter = FakeAdapter(responses=[
        response().model_copy(update={"tokens_in": 2_000_000}),
    ])
    child = parent.fork(adapter, budget)
    assert await child.call("large") == "ok"
    await child._close()
    parent.absorb(child)
    parent._check()
    assert parent.tokens_in == child.tokens_in == 2_000_000
    assert parent.cost_usd == pytest.approx(0.001)
    await parent._close()


async def test_closing_parent_cancels_abandoned_child_call() -> None:
    entered = asyncio.Event()

    class DelayedFake(FakeAdapter):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    parent = RunContext(FakeAdapter(), Budget())
    child = parent.fork(DelayedFake(responses=[response()]), Budget())
    task = asyncio.create_task(child.call("never"))
    await entered.wait()
    await parent._close()
    assert task.cancelled() and not parent._children
    assert parent.tokens_in == 6


async def test_model_call_limit_covers_descendants() -> None:
    parent = RunContext(FakeAdapter(), Budget())
    parent.limit_calls(1)
    parent.limit_calls(10)
    child = parent.fork(FakeAdapter(), Budget())
    await child.call("once")
    with pytest.raises(BudgetExceeded):
        await child.call("twice")
    await child._close()
    parent.absorb(child)
    with pytest.raises(ValueError):
        parent.limit_calls(0)
