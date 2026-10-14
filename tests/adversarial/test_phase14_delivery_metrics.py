"""Phase 14 review: invalid and refused runs must not count as receipts settled in code.

The pre-registration (docs/bench/prereg-nebius.md, 9.1) reports "Receipts settled with
no model call" as ``delivery_zero_call_runs / n``, and the contract (Section 11) defines
the field as "runs that finished with 0 model calls". A run that raised before its first
call (invalid) or was refused at its first reservation (over budget) settled nothing,
so counting it would let a broken arm look like one that answers most receipts in code.
"""

from __future__ import annotations

from architect.bench import runner
from architect.bench.suite import SUITES
from architect.floors import EvaluationCase

RECEIPT = {"total": 1200, "subtotal": 1100, "tax": 100, "item_count": 2}


def test_only_answered_runs_count_as_settled_with_no_model_call() -> None:
    task = next(item for item in SUITES["v2"] if item.id == "receipts")
    cases = [
        EvaluationCase(case_id=f"r{index}", task={"text": f"receipt {index}"}, expected=RECEIPT)
        for index in range(4)
    ]
    runs = [
        # Answered in code: the one receipt settled with no model call.
        runner._Run(output=RECEIPT, outcome="ok", cost_usd=0.0, calls=0, seconds=0.2),
        # Raised before any call: invalid, nothing settled.
        runner._Run(output=None, outcome="invalid", cost_usd=0.0, calls=0, seconds=0.1),
        # Refused at its first reservation: over budget, nothing settled.
        runner._Run(output=None, outcome="over_budget", cost_usd=0.0, calls=0, seconds=0.0),
        # Answered with one model call.
        runner._Run(output=RECEIPT, outcome="ok", cost_usd=0.0001, calls=1, seconds=1.4),
    ]
    metrics = runner._delivery_metrics(task, cases, runs)
    assert metrics.passed == (True, False, False, True)
    assert (metrics.invalid, metrics.over_budget) == (1, 1)
    assert metrics.zero_call_runs == 1, metrics.zero_call_runs
