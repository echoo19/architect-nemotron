from pathlib import Path

import pytest

from architect.models import FakeAdapter, ModelResponse, UsageQuote
from architect.runtime import BudgetExceeded, RunContext, Runtime
from architect.runtime.wallet import active_wallets, compilation_wallet
from architect.spec import Budget, FloorDefinition, FloorKind, Spec
from architect.telemetry import TelemetryStore


def response(tokens: int = 6, cost_usd: float = 0.001) -> ModelResponse:
    return ModelResponse(text="ok", tokens_in=tokens, tokens_out=0, cost_usd=cost_usd, model="fake")


async def test_aggregate_dollar_budget_stops_separate_runs(tmp_path: Path) -> None:
    spec = Spec(
        task="task",
        input_schema={},
        output_schema={},
        budget=Budget(),
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="data"),
    )
    runtime = Runtime(TelemetryStore(tmp_path))

    async def harness(context: RunContext, task: object) -> str:
        return await context.call("private")

    with compilation_wallet(Budget(cost_usd=0.0015, compute_s=10)) as wallet:
        await runtime.evaluate(spec, harness, None, FakeAdapter(responses=[response()]))
        blocked = FakeAdapter(responses=[response()])
        with pytest.raises(BudgetExceeded):
            await runtime.evaluate(spec, harness, None, blocked)
        assert blocked.calls == 0
        assert wallet.tokens == 6
        assert wallet.exhausted
    assert active_wallets() == ()
    assert [row.accepted for row in runtime.store.rows()] == [True, False]


def test_nested_wallet_reservation_rollback_and_reconcile() -> None:
    quote = UsageQuote(tokens_in=4, tokens_out=2, cost_usd=0.01)
    with compilation_wallet(Budget(cost_usd=1)) as wallet:
        wallet.reserve(quote)
        wallet.rollback(quote)
        assert wallet.tokens == 0
        wallet.reserve(quote)
        wallet.reconcile(quote, response(3))
        assert wallet.tokens == 3
        assert wallet.cost_usd == pytest.approx(0.001)
        large = UsageQuote(tokens_in=10**9, tokens_out=10**9, cost_usd=0.01)
        wallet.reserve(large)
        wallet.reconcile(large, response(10**12))
        assert wallet.tokens == 10**12 + 3
        assert wallet.cost_usd == pytest.approx(0.002)
        assert not wallet.exhausted
        wallet.reserve(quote)
        wallet.reconcile(quote, response(cost_usd=2))
        assert wallet.exhausted
        assert wallet.cost_usd == pytest.approx(2.002)


async def test_nested_failure_refunds_prior_wallet() -> None:
    with compilation_wallet(Budget(cost_usd=1)) as outer:
        with compilation_wallet(Budget(cost_usd=0.0005)) as inner:
            context = RunContext(FakeAdapter(responses=[response()]), Budget())
            with pytest.raises(BudgetExceeded):
                await context.call("x")
            assert inner.exhausted
            assert inner.tokens == 0
            assert not outer.exhausted
        assert outer.tokens == 0
        assert outer.cost_usd == 0


@pytest.mark.parametrize("operation", ["reserve", "reconcile"])
def test_wallet_time_exhaustion(monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    monkeypatch.setattr("architect.runtime.wallet.time.monotonic", lambda: 100.0)
    quote = UsageQuote(tokens_in=4, tokens_out=2, cost_usd=0.001)
    with compilation_wallet(Budget(cost_usd=1, compute_s=2)) as wallet:
        if operation == "reconcile":
            wallet.reserve(quote)
        monkeypatch.setattr("architect.runtime.wallet.time.monotonic", lambda: 102.0)
        assert wallet.remaining_s == 0
        if operation == "reserve":
            with pytest.raises(BudgetExceeded):
                wallet.reserve(quote)
            assert wallet.tokens == 0
            assert wallet.cost_usd == 0
        else:
            wallet.reconcile(quote, response())
            assert wallet.tokens == 6
            assert wallet.cost_usd == pytest.approx(0.001)
        assert wallet.exhausted
        with pytest.raises(BudgetExceeded):
            wallet.reserve(quote)
    assert active_wallets() == ()


async def test_large_token_usage_is_metered_across_runs(tmp_path: Path) -> None:
    spec = Spec(
        task="task",
        input_schema={},
        output_schema={},
        budget=Budget.model_validate({"tokens": 1, "cost_usd": 1}),
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="data"),
    )
    runtime = Runtime(TelemetryStore(tmp_path))

    async def harness(context: RunContext, task: object) -> str:
        return await context.call("private")

    with compilation_wallet(spec.budget) as wallet:
        for _ in range(2):
            result = await runtime.evaluate(
                spec, harness, None, FakeAdapter(responses=[response(10**9)])
            )
            assert result.tokens_in == 10**9
        assert wallet.tokens == 2 * 10**9
        assert wallet.cost_usd == pytest.approx(0.002)
        assert not wallet.exhausted
    assert active_wallets() == ()
    assert [row.accepted for row in runtime.store.rows()] == [True, True]
