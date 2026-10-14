"""A fixed suite v2 rows fixture for the report golden file (Phase 14 Section 13.1)."""

from __future__ import annotations

from bench_public_support import public_row

from architect.bench.compare import PairedComparison, pair_arms
from architect.bench.rows import BenchRow, ForgeFields

# A fixed digest, so the golden file does not move with the committed data or settings.
DIGEST = "5ca1ab1e" * 8
PASSED = {
    "A1": [1, 1, 1, 1, 1, 1, 1, 1, 1, 0],
    "A3": [1, 0, 1, 1, 0, 1, 0, 1, 1, 0],
    "A7": [1, 1, 1, 1, 0, 1, 1, 1, 1, 0],
}
COSTS = {
    "A1": (0.0021, 0.0019, 0.0024, 0.0020, 0.0022, 0.0018, 0.0023, 0.0021, 0.0020, 0.0022),
    "A3": (0.0002, 0.0002, 0.0003, 0.0002, 0.0002, 0.0002, 0.0003, 0.0002, 0.0002, 0.0002),
    "A7": (0.0001, 0.0, 0.0002, 0.0001, 0.0001, 0.0, 0.0002, 0.0001, 0.0001, 0.0001),
}
MODELS = {
    "A1": ("ultra", "nvidia/Nemotron-3-Ultra-550b-a55b"),
    "A3": ("lightning", "nvidia/Nemotron-3_5-Lightning"),
    "A7": ("lightning", "nvidia/Nemotron-3_5-Lightning"),
}
FORGE = ForgeFields(
    mode="agent",
    agent_model="ultra",
    checks=12,
    dryruns=4,
    measures=6,
    submissions=2,
    accepted=1,
    spawns=0,
    agent_cost_usd=1.2345,
    agent_tokens_in=400_000,
    agent_tokens_out=60_000,
    agent_wall_s=900.0,
    harness_nodes=420,
    harness_bytes=3100,
    subharnesses=0,
    language=2,
)


def fixture_rows() -> list[BenchRow]:
    rows = []
    for arm in ("A7", "A1", "A3"):  # out of order on purpose: the report sorts by arm
        model, api_model = MODELS[arm]
        passed = [bool(bit) for bit in PASSED[arm]]
        zero_calls = 2 if arm == "A7" else 0
        rows.append(
            public_row(
                label=arm,
                suite_digest=DIGEST,
                model=model,
                api_model=api_model,
                variant="forge" if arm == "A7" else "default",
                passed=passed,
                costs=COSTS[arm],
                held_out_correct={"A1": 96, "A3": 85, "A7": 93}[arm],
                held_out_score={"A1": 0.96, "A3": 0.85, "A7": 0.93}[arm],
                held_out_invalid={"A1": 0, "A3": 2, "A7": 1}[arm],
                licensed=arm != "A3",
                tuning_score={"A1": 0.97, "A3": 0.88, "A7": 0.99}[arm],
                delivery_invalid={"A1": 0, "A3": 1, "A7": 0}[arm],
                delivery_over_budget={"A1": 1, "A3": 0, "A7": 0}[arm],
                delivery_calls={"A1": 10, "A3": 10, "A7": 8}[arm],
                delivery_zero_call_runs=zero_calls,
                delivery_p50_s={"A1": 2.104, "A3": 0.3, "A7": 0.061}[arm],
                delivery_p95_s={"A1": 4.5, "A3": 0.91, "A7": 0.4}[arm],
                compile_cost_usd={"A1": 0.25, "A3": 0.02, "A7": 0.031}[arm],
                forge=FORGE if arm == "A7" else None,
            )
        )
    return rows


def fixture_pairs(rows: list[BenchRow]) -> PairedComparison:
    return pair_arms(rows, [("A3", "A7"), ("A1", "A7")], bootstrap=1000, seed=0)
