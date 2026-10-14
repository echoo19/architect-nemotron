"""Task-local nested compilation budgets shared by every guarded model call."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from architect.models import ModelResponse, UsageQuote
from architect.spec import Budget


class CompileWallet:
    def __init__(self, budget: Budget) -> None:
        self.budget = budget
        # Observed usage (including in-flight reservations), not an allowance.
        self.tokens = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_usd = 0.0
        self.started = time.monotonic()
        self.exhausted = False

    @property
    def remaining_s(self) -> float:
        return max(0.0, self.budget.compute_s - (time.monotonic() - self.started))

    def reserve(self, quote: UsageQuote) -> None:
        from architect.runtime import BudgetExceeded

        tokens = self.tokens + quote.tokens_in + quote.tokens_out
        cost = self.cost_usd + quote.cost_usd
        if self.exhausted or not self.remaining_s or cost > self.budget.cost_usd:
            self.exhausted = True
            raise BudgetExceeded(
                "Compilation budget exhausted", required_tokens=tokens, required_cost_usd=cost
            )
        self.tokens = tokens
        self.tokens_in += quote.tokens_in
        self.tokens_out += quote.tokens_out
        self.cost_usd = cost

    def rollback(self, quote: UsageQuote) -> None:
        self.tokens -= quote.tokens_in + quote.tokens_out
        self.tokens_in -= quote.tokens_in
        self.tokens_out -= quote.tokens_out
        self.cost_usd = max(0.0, self.cost_usd - quote.cost_usd)

    def reconcile(self, quote: UsageQuote, response: ModelResponse) -> None:
        self.tokens += response.tokens_in + response.tokens_out - quote.tokens_in - quote.tokens_out
        self.tokens_in += response.tokens_in - quote.tokens_in
        self.tokens_out += response.tokens_out - quote.tokens_out
        self.cost_usd = max(0.0, self.cost_usd + response.cost_usd - quote.cost_usd)
        if not self.remaining_s or self.cost_usd > self.budget.cost_usd:
            self.exhausted = True


_ACTIVE: ContextVar[tuple[CompileWallet, ...]] = ContextVar("architect_wallets", default=())


def active_wallets() -> tuple[CompileWallet, ...]:
    return _ACTIVE.get()


@contextmanager
def compilation_wallet(budget: Budget) -> Iterator[CompileWallet]:
    wallet = CompileWallet(budget)
    token = _ACTIVE.set((*_ACTIVE.get(), wallet))
    try:
        yield wallet
    finally:
        _ACTIVE.reset(token)
