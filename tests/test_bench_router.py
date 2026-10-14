"""Phase 10 WP4: the routed bench arm, router metrics, the simulation and CI checks.

Everything is offline and $0: the fake provider answers through in-process
oracles, sockets are refused, and the router is either the real
``architect.routing`` (WP1) or the test-local stub in ``bench_route_stub``.
The routed rows are ``synthetic_fake``: they prove plumbing and decision
determinism, not routing quality.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path
from typing import Any, get_args

import pytest

from architect.bench import CELL_CODES, BenchError, BenchRow, read_rows
from architect.bench import providers as bench_providers
from architect.bench import rows as bench_rows
from architect.bench.ci_assert import (
    check_rows,
    determinism_failures,
    route_failures,
    simulation_failures,
)
from architect.bench.ci_assert import main as ci_main
from architect.bench.compare import compare
from architect.bench.render import TABLE_CELL_CODES, bench_table
from architect.bench.router_eval import (
    PLUMBING_NOTE,
    OracleOption,
    learning_curve,
    oracle,
    regret_table,
)
from architect.bench.router_sim import (
    SimInstalled,
    SimResult,
    SimStep,
    SimTemplate,
    SimVariant,
    roles_arm,
    roles_table,
    simulate,
    simulate_roles,
    simulation_suite,
)
from architect.bench.rows import RouteFields, summarize
from architect.bench.runner import BenchConfig, plan, run_bench
from architect.bench.suite import PROBES, SUITES, foreign_task, load_siblings, probe_spec
from architect.routing import labels
from architect.routing.log import RouteAction as RouterAction
from architect.routing.roles import RoleRouting
from architect.routing.router import RoutePolicy
from architect.routing.signature import signature

sys.path.insert(0, str(Path(__file__).parent))
from bench_route_stub import StubLog, stub_opener  # noqa: E402

TASKS = {task.id: task for task in SUITES["v1"]}
ROUTED = [task for task in SUITES["v1"] if task.supports("routed")]
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse every non-loopback connection (the event loop's self-pipe stays usable)."""

    def guarded(original: Any) -> Any:
        def connect(self: socket.socket, address: Any, *args: Any) -> Any:
            host = address[0] if isinstance(address, tuple) else address
            if host not in _LOOPBACK:
                raise AssertionError("network access in a hermetic test")
            return original(self, address, *args)

        return connect

    monkeypatch.setattr(socket.socket, "connect", guarded(socket.socket.connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guarded(socket.socket.connect_ex))


def _config(root: Path, **overrides: Any) -> BenchConfig:
    values: dict[str, Any] = {
        "tier": "quick",
        "provider": "fake",
        "seed": 7,
        "variants": ("default", "routed"),
        "size": 8,
        "out": root / "out",
    }
    values.update(overrides)
    return BenchConfig.model_validate(values)


def _run(root: Path, routing: Any = None, **overrides: Any) -> list[BenchRow]:
    config = _config(root, **overrides)
    result = asyncio.run(run_bench(config, workspace_root=root / "ws", routing=routing))
    assert read_rows(result.rows_path) == list(result.rows)
    return list(result.rows)


@pytest.fixture(scope="module")
def real(tmp_path_factory: pytest.TempPathFactory) -> list[BenchRow]:
    """Two replay rounds of the quick tier through the real router (WP1)."""
    return _run(tmp_path_factory.mktemp("real"), rounds=2)


@pytest.fixture(scope="module")
def real_again(tmp_path_factory: pytest.TempPathFactory) -> list[BenchRow]:
    return _run(tmp_path_factory.mktemp("again"), rounds=2)


def routed(rows: list[BenchRow]) -> list[BenchRow]:
    return [row for row in rows if row.variant == "routed"]


# ------------------------------------------------------------------- rows


def _row(**overrides: Any) -> BenchRow:
    base = json.loads(
        (Path(__file__).parents[1] / "examples/bench/suite-v1/smoke-baseline.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    base.update(overrides)
    return BenchRow.model_validate(base)


ROUTE = {
    "probe": "exact",
    "action": "reuse_installed",
    "tier": "A",
    "candidates": 2,
    "chosen_rank": 0,
    "oracle_rank": 0,
    "regret": 0.0,
    "compile_cost_avoided_usd": 0.0014,
    "fallback": False,
    "explored": False,
    "decision_ms": 1.5,
}


def test_route_fields_round_trip_and_old_rows_stay_readable(tmp_path: Path) -> None:
    # Frozen rows from the pre-Phase-10 smoke baseline: no route, no digest.
    old = read_rows(Path(__file__).parent / "fixtures/bench/pre-phase10-rows.jsonl")
    assert old and all(row.route is None and row.artifact_digest is None for row in old)
    row = _row(variant="routed", artifact_digest="e" * 64, route=ROUTE)
    assert row.route is not None and row.route.round == 0 and row.route.codes == ()
    path = tmp_path / "rows.jsonl"
    bench_rows.write_rows(path, [row, old[0]])
    assert read_rows(path) == [row, old[0]]
    with pytest.raises(ValueError):
        RouteFields.model_validate({**ROUTE, "probe": "cousin"})
    with pytest.raises(ValueError):
        RouteFields.model_validate({**ROUTE, "task": "leak"})  # extra="forbid"


def test_labels_are_the_routers_own() -> None:
    assert bench_rows.Evidence is labels.EvidenceClass
    assert bench_rows.Provider is labels.Provider
    assert bench_providers._EVIDENCE is labels.PROVIDER_EVIDENCE
    assert get_args(bench_rows.RouteAction) == get_args(RouterAction)
    assert set(get_args(bench_rows.FloorOrigin)) <= set(get_args(labels.FloorOrigin))
    for provider in get_args(labels.Provider):
        assert bench_providers.evidence_label(provider) == labels.evidence_class(provider)


def test_route_did_not_run_is_a_closed_cell_code() -> None:
    assert "route_did_not_run" in CELL_CODES and TABLE_CELL_CODES == CELL_CODES


# ----------------------------------------------------------------- probes


def test_probe_derivation_is_deterministic() -> None:
    siblings = load_siblings("v1")
    assert set(siblings) == {task.id for task in ROUTED}
    for task in ROUTED:
        spec = task.spec("fake", "routed")
        assert spec == task.spec("fake", "default")
        assert probe_spec(spec, "exact", task, siblings=siblings) is spec
        sibling = probe_spec(spec, "sibling", task, siblings=siblings)
        assert sibling == probe_spec(spec, "sibling", task, siblings=siblings)
        assert sibling.task == siblings[task.id] != spec.task
        assert sibling.model_dump(exclude={"task"}) == spec.model_dump(exclude={"task"})
        mine, theirs = signature(spec), signature(sibling)
        assert mine.shape == theirs.shape and mine.slot != theirs.slot
        foreign = foreign_task(task)
        assert foreign == foreign_task(task) and foreign.supports("routed")
        assert signature(foreign.spec("fake", "default")).shape != mine.shape
    assert foreign_task(TASKS["exam-clean-40"]).id == "digitizer-24"  # skips same-shape scan
    assert foreign_task(TASKS["digitizer-24"]).id == "sentiment-24"  # wraps past the games
    assert not TASKS["arc-local-3"].supports("routed")
    with pytest.raises(BenchError):
        probe_spec(TASKS["sentiment-24"].spec("fake", "default"), "foreign", TASKS["sentiment-24"],
                   siblings=siblings)


def test_rounds_replay_in_a_seed_fixed_shuffled_order(tmp_path: Path) -> None:
    config = _config(tmp_path, tier="smoke", rounds=3, variants=("routed",))
    cells = plan(config, ("fake",))
    assert [cell.round for cell in cells] == [r for r in range(3) for _ in range(6)]
    orders = [[c.task.id for c in cells if c.round == r] for r in range(3)]
    assert all(sorted(order) == sorted(orders[0]) for order in orders)
    assert len({tuple(order) for order in orders}) > 1  # shuffled per round
    assert [(c.task.id, c.round) for c in plan(config, ("fake",))] == [
        (c.task.id, c.round) for c in cells
    ]
    single = plan(_config(tmp_path, tier="smoke", variants=("routed",)), ("fake",))
    assert [c.task.id for c in single] == [
        "sentiment-24", "tickets-40", "invoice-40", "exam-clean-40", "exam-scan-40",
        "digitizer-24",
    ]


# ------------------------------------------------------------ oracle, regret


def test_oracle_ranks_by_realized_score_then_cost() -> None:
    options = [
        OracleOption(score=0.9, cost_usd=0.0),
        OracleOption(score=1.0, cost_usd=0.5),
        OracleOption(score=1.0, cost_usd=0.2),
        OracleOption(score=None, cost_usd=0.0),
    ]
    assert oracle(options, 2) == (0, 0.0)
    assert oracle(options, 1) == (1, 0.0)
    rank, regret = oracle(options, 0)
    assert rank == 2 and regret == pytest.approx(0.1)
    assert oracle(options, 3) == (3, 1.0)
    # float noise below 1e-9 dollars is a tie: the given order decides
    tied = [
        OracleOption(score=1.0, cost_usd=0.00264),
        OracleOption(score=1.0, cost_usd=0.00264 - 1e-18),
    ]
    assert oracle(tied, 0) == (0, 0.0)
    with pytest.raises(ValueError):
        oracle(options, 4)


def test_real_router_rows_meet_the_pass_criteria(real: list[BenchRow]) -> None:
    rows = routed(real)
    assert len(rows) == 3 * 3 * 2  # three tasks, three probes, two rounds
    for row in rows:
        route = row.route
        assert route is not None and row.evidence == "synthetic_fake"
        assert "route_did_not_run" not in row.failures and row.licensed
        assert route.installed == 1 and route.oracle_rank is not None
        assert route.regret == 0.0  # the fake oracle scores every option alike
        if route.probe == "exact":
            assert route.action == "reuse_installed" and route.tier == "A"
            assert row.cost_usd == 0.0 and row.stages == () and row.calls == 0
            assert route.compile_cost_avoided_usd > 0 and route.chosen_rank == 0
            assert row.artifact_digest is not None and row.tuning_pass_bits
        elif route.probe == "sibling":
            assert route.action == "warm_start" and route.tier == "B"
            assert route.templates_ready >= 1 and not route.fallback
        else:
            assert route.action == "compile" and route.fallback and route.chosen_rank is None
        # candidates listed on the fake provider are labelled as plumbing evidence
        assert ("evidence_synthetic_only" in route.codes) == (route.candidates > 0)
    assert route_failures(real) == []
    # check_rows runs the routed checks too; the quick tier has no noisy control, so
    # that Section 13 line is its only complaint.
    assert check_rows(real) == ["no noisy rows: the negative control did not run"]


def test_foreign_rows_carry_the_foreign_floor(real: list[BenchRow]) -> None:
    default = {row.task_id: row for row in real if row.variant == "default"}
    for row in routed(real):
        assert row.route is not None
        own = default[row.task_id]
        if row.route.probe == "foreign":
            assert row.floor_limit == foreign_task(TASKS[row.task_id]).floor_limit
            assert row.spec_hash != own.spec_hash
        elif row.route.probe == "exact":
            assert row.spec_hash == own.spec_hash  # the task's own spec
        else:
            assert row.spec_hash != own.spec_hash and row.floor_limit == own.floor_limit


def test_two_identical_runs_route_identically(
    real: list[BenchRow], real_again: list[BenchRow]
) -> None:
    assert determinism_failures(real, real_again) == []
    broken = list(real_again)
    index = next(i for i, row in enumerate(broken) if row.route and row.route.probe == "sibling")
    assert broken[index].route is not None
    broken[index] = broken[index].model_copy(
        update={"route": broken[index].route.model_copy(update={"action": "compile"})}
    )
    failures = determinism_failures(real, broken)
    assert len(failures) == 1 and "differs between identical runs" in failures[0]
    assert determinism_failures(real, broken[:-1])[0].startswith("routed decisions differ")
    assert determinism_failures([], real) == ["no routed rows: determinism was not measured"]


def test_stub_router_cells_and_the_outcome_log(tmp_path: Path) -> None:
    log = StubLog()
    rows = routed(_run(tmp_path, routing=stub_opener(log), tier="quick"))
    assert [row.route.probe for row in rows if row.route] == list(PROBES) * 3
    assert [action for action, _ in log.decisions] == [
        "reuse_installed", "warm_start", "compile"
    ] * 3
    # one outcome per executed decision; the reuse is a delivery with $0 compile cost
    assert [item[0] for item in log.outcomes[:3]] == ["delivered", "licensed", "licensed"]
    assert log.outcomes[0][2] == 0.0
    assert route_failures(rows) != []  # no default rows in this list: nothing to pair


def test_route_did_not_run_guard(tmp_path: Path) -> None:
    rows = _run(tmp_path, routing=stub_opener(StubLog(), empty=True), tier="quick")
    exact = [row for row in routed(rows) if row.route and row.route.probe == "exact"]
    assert exact and all("route_did_not_run" in row.failures for row in exact)
    assert all(row.route and row.route.candidates == 0 for row in exact)
    others = [row for row in routed(rows) if row.route and row.route.probe != "exact"]
    assert all("route_did_not_run" not in row.failures for row in others)
    failures = route_failures(rows)
    assert any(line.startswith("routed row did not route") for line in failures)
    rendered = bench_table(rows)
    assert "cell code: route_did_not_run" in rendered


async def test_a_run_without_the_router_is_refused_before_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from architect.bench import runner

    monkeypatch.setattr(runner, "routing_available", lambda: False)
    config = _config(tmp_path)
    with pytest.raises(BenchError) as caught:
        await run_bench(config, workspace_root=tmp_path / "ws")
    assert caught.value.code == "bench_variant_unknown"
    assert not (tmp_path / "out").exists()


def test_the_guard_marks_rows_without_a_decision() -> None:
    from architect.bench.runner import _route_ran

    route = RouteFields.model_validate(ROUTE)
    assert _route_ran("routed", route) and _route_ran("default", None)
    assert not _route_ran("routed", None)
    assert not _route_ran("routed", route.model_copy(update={"candidates": 0}))
    sibling = route.model_copy(update={"probe": "sibling", "candidates": 0})
    assert _route_ran("routed", sibling)


def test_forced_fallbacks_are_recorded(tmp_path: Path) -> None:
    rows = routed(
        _run(tmp_path, routing=stub_opener(StubLog(), action="compile"), tier="quick")
    )
    for row in rows:
        assert row.route is not None and row.route.action == "compile" and row.route.fallback
    failures = route_failures(rows)
    assert any("exact probe did not reuse" in line for line in failures)
    assert any("sibling probe did not warm-start" in line for line in failures)


# ------------------------------------------------------------ compare, render


def test_compare_pairs_default_with_the_exact_round_zero_probe(real: list[BenchRow]) -> None:
    comparison = compare(real, real, pair_variants=("default", "routed"))
    assert len(comparison.cells) == 3
    assert all(cell.licensed_base and cell.licensed_cand for cell in comparison.cells)
    assert {cell.verdict for cell in comparison.cells} <= {"identical", "improvement"}
    assert comparison.pair_variants == ("default", "routed")
    # the routed run against itself pairs every probe and round separately
    assert len(compare(routed(real), routed(real)).cells) == 18


def test_ci_fails_when_routing_reduces_licensing(real: list[BenchRow]) -> None:
    broken = [
        row.model_copy(update={"licensed": False})
        if row.variant == "routed" and row.route and row.route.probe == "exact"
        else row
        for row in real
    ]
    failures = route_failures(broken)
    assert any(line.startswith("routing reduced licensing") for line in failures)


def test_ci_fails_on_each_broken_routed_row(real: list[BenchRow]) -> None:
    def broken(probe: str, **update: Any) -> list[str]:
        rows = list(real)
        index = next(
            i for i, row in enumerate(rows) if row.route is not None and row.route.probe == probe
        )
        route = rows[index].route
        assert route is not None
        rows[index] = rows[index].model_copy(update={"route": route.model_copy(update=update)})
        return route_failures(rows)

    assert any("exact probe did not reuse" in line for line in broken("exact", action="compile"))
    assert any(
        "foreign probe did not compile" in line for line in broken("foreign", action="warm_start")
    )
    assert any(
        "sibling probe did not warm-start" in line for line in broken("sibling", action="compile")
    )
    # without an installed entry or a ready template the probe is not held to it
    assert broken("exact", action="compile", installed=0) == []
    assert broken("sibling", action="compile", templates_ready=0) == []
    rows = list(real)
    index = next(i for i, row in enumerate(rows) if row.variant == "routed")
    rows[index] = rows[index].model_copy(update={"route": None})
    assert any("without route columns" in line for line in route_failures(rows))
    rows[index] = real[index].model_copy(update={"failures": ("route_did_not_run",)})
    assert any("did not route" in line for line in route_failures(rows))
    assert route_failures(routed(real)) == [
        "no default rows: the routed arm has nothing to pair with"
    ]


def test_render_width_and_route_block(real: list[BenchRow]) -> None:
    text = bench_table(real)
    for line in text.splitlines():
        assert len(line) <= 100 and line.isascii()
    assert "route (routing orders licensed versions; Stage 4 decides the license)" in text
    header = next(line for line in text.splitlines() if line.startswith("task") and "probe" in line)
    for column in ("probe", "tier", "rank/orc", "dCost$"):
        assert column in header
    assert "plumbing, not quality" in text


def test_summarize_groups_by_probe(real: list[BenchRow]) -> None:
    cells = summarize(real).per_cell
    probes = [(cell.task_id, cell.variant, cell.probe) for cell in cells]
    assert ("sentiment-24", "default", None) in probes
    for probe in PROBES:
        cell = next(c for c in cells if c.task_id == "sentiment-24" and c.probe == probe)
        assert cell.repeats == 2 and cell.fallback_rate == (1.0 if probe == "foreign" else 0.0)
        assert cell.mean_regret == 0.0
        assert cell.route_accuracy == (None if probe == "foreign" else 1.0)
    default = next(c for c in cells if c.variant == "default")
    assert default.route_accuracy is None and default.mean_cost_avoided_usd is None


# ------------------------------------------------------ learning curve, regret


def test_learning_curve_reports_every_round(real: list[BenchRow]) -> None:
    curve = learning_curve(real)
    assert [point.round for point in curve.points] == [0, 1]
    for point in curve.points:
        assert point.rows == 9 and point.licensed_rate == 1.0
        assert point.exploration_share == 0.0 and point.mean_regret == 0.0
        assert point.promote_proposals == 0 and point.rollback_proposals == 0
        assert point.spend_usd > 0
    assert curve.evidence == ("synthetic_fake",) and curve.note == PLUMBING_NOTE


def test_learning_curve_counts_exploration_and_proposals(real: list[BenchRow]) -> None:
    rows = []
    for row in routed(real):
        assert row.route is not None
        if row.route.round == 1 and row.route.probe == "sibling":
            update = {"explored": True, "codes": ("route_explore", "route_promote_proposed")}
            row = row.model_copy(update={"route": row.route.model_copy(update=update)})
        rows.append(row)
    second = learning_curve(rows).points[1]
    assert second.exploration_share == pytest.approx(3 / 9)
    assert second.promote_proposals == 3 and second.rollback_proposals == 0


def test_regret_table_against_the_hindsight_oracle(real: list[BenchRow]) -> None:
    table = regret_table(real)
    assert table.oracle_arms == ("default",) and table.oracle_models == ("fake",)
    arms = {(cell.task_id, cell.arm) for cell in table.cells}
    assert ("sentiment-24", "default") in arms and ("sentiment-24", "routed:exact") in arms
    assert not any(arm.endswith("foreign") for _, arm in arms)
    assert all(cell.mean_regret == 0.0 for cell in table.cells)
    worse = [
        row.model_copy(update={"held_out_score": 0.5})
        if row.variant == "routed" and row.route and row.route.probe == "exact"
        else row
        for row in real
    ]
    cell = next(
        c
        for c in regret_table(worse).cells
        if c.arm == "routed:exact" and c.task_id == "invoice-40"
    )
    assert cell.mean_regret == pytest.approx(0.5)
    assert regret_table(worse).note == PLUMBING_NOTE


# -------------------------------------------------------------- simulation


def test_simulation_meets_every_section_14_1_bound() -> None:
    suite = simulation_suite(2000)
    assert simulation_failures(suite) == []
    assert suite.exploration.exploration_share > 0  # exploration really ran
    assert any(not s.explored and s.action == "warm_start" for s in suite.exploration.steps)
    assert suite.roles_pinned.share([suite.roles_pinned.default or ""]) == 1.0
    assert simulation_suite(50) == simulation_suite(50)  # seeded, pure


def _step(step: int, **update: Any) -> SimStep:
    values: dict[str, Any] = {"step": step, "day": 0, "action": "warm_start", "target": "t0",
                              "explored": False}
    values.update(update)
    return SimStep.model_validate(values)


def test_simulation_checks_fail_on_broken_results() -> None:
    good = simulation_suite(40)
    assert simulation_failures(good) == []

    def failures(**update: Any) -> list[str]:
        return simulation_failures(good.model_copy(update=update))

    explore = good.exploration.model_copy(
        update={"steps": tuple(_step(i, explored=True) for i in range(40))}
    )
    assert any("exploration share" in line for line in failures(exploration=explore))
    capped = good.cap.model_copy(
        update={"steps": (_step(0, explored=True, explore_spent_usd=1.0),)
                + tuple(_step(i) for i in range(1, 40))}
    )
    assert any("cap exceeded" in line for line in failures(cap=capped))
    uncapped = good.cap.model_copy(update={"steps": tuple(_step(i) for i in range(40))})
    assert any("never reached its daily cap" in line for line in failures(cap=uncapped))
    forbidden = good.filters.model_copy(
        update={"steps": tuple(_step(i, forbidden=i == 3) for i in range(40))}
    )
    assert any("evicted, unlicensed or consumed" in line for line in failures(filters=forbidden))
    noisy = tuple(
        (p, result.model_copy(update={"steps": tuple(_step(i, promote=True) for i in range(40))}))
        for p, result in good.promote
    )
    assert sum("false promote-proposal" in line for line in failures(promote=noisy)) == 3
    assert any("must cover" in line for line in failures(promote=good.promote[:2]))
    roles = good.roles_evidence.model_copy(
        update={"steps": tuple(_step(i, target="worse") for i in range(40))}
    )
    assert any("0.10 worse was chosen" in line for line in failures(roles_evidence=roles))
    pinned = good.roles_pinned.model_copy(
        update={"steps": tuple(_step(i, target="worse") for i in range(40))}
    )
    assert any("pinned role policy" in line for line in failures(roles_pinned=pinned))
    short = good.model_copy(update={"trials": 41})
    assert any("of 41 trials" in line for line in simulation_failures(short))


def test_simulate_filters_and_exploration_directly() -> None:
    templates = [SimTemplate(id="bad", p=1.0, evicted=True, successes=9),
                 SimTemplate(id="ok", p=1.0)]
    result = simulate(templates, [SimInstalled(id="used", p=1.0, consumed=True)], 200, 3,
                      RoutePolicy.model_validate({"explore_every": 4}))
    assert result.forbidden_chosen == 0
    assert {s.target for s in result.steps} <= {"ok", None}
    assert result.exploration_share <= 1 / 4 + 0.01
    assert isinstance(result, SimResult) and result.evidence == "synthetic_fake"


def test_simulate_roles_pinned_and_evidence() -> None:
    variants = [
        SimVariant(id="d", model="sim-a", p=0.9, cost_usd=0.02, default=True),
        SimVariant(id="cheap", model="sim-b", p=0.97, cost_usd=0.001, samples=200),
    ]
    pinned = simulate_roles(variants, 100, 1, RoleRouting(policy="pinned"))
    assert pinned.share(["d"]) == 1.0 and pinned.worse == ()
    evidence = simulate_roles(variants, 100, 1, RoleRouting(policy="evidence"))
    assert evidence.share(["cheap"]) > 0.5  # a cheaper non-inferior variant is used


# ---------------------------------------------------------------- roles arm


def test_roles_arm_under_both_policies() -> None:
    pinned = roles_arm("pinned")
    assert len(pinned.replays) == 3
    for replay in pinned.replays:
        assert replay.final == 0 and replay.choices[0] == replay.outcomes
        assert sum(replay.choices[1:]) == 0 and replay.codes == ()
    evidence = {replay.file: replay for replay in roles_arm("evidence").replays}
    rewrite = evidence["rewrite-cheaper-noninferior.jsonl"]
    assert rewrite.final == 1 and rewrite.choices[1] > 0  # cheaper and non-inferior
    assert evidence["inference-cheaper-worse.jsonl"].final == 0  # cheaper but worse
    thin = evidence["forge-agent-thin.jsonl"]
    assert thin.final == 0 and thin.codes == ("role_evidence_insufficient",)
    assert roles_arm("evidence") == roles_arm("evidence")
    for line in roles_table(roles_arm("evidence")).splitlines():
        assert len(line) <= 100 and line.isascii()


def test_roles_arm_refuses_a_malformed_log(tmp_path: Path) -> None:
    with pytest.raises(BenchError):
        roles_arm("pinned", tmp_path)  # no logs at all
    (tmp_path / "bad.jsonl").write_text('{"kind": "scenario"}\n', encoding="utf-8")
    with pytest.raises(BenchError):
        roles_arm("pinned", tmp_path)


def test_role_logs_are_content_free() -> None:
    root = Path(__file__).parents[1] / "examples/bench/roles"
    for path in root.glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            payload = json.loads(line)
            assert set(payload) <= {
                "kind", "role", "domain", "variants", "providers", "prices", "variant",
                "outcome", "score", "cost_usd", "wall_s", "floor_origin",
            }


# ------------------------------------------------------------------ CI main


def test_ci_main_with_repeat_and_simulation(
    real: list[BenchRow], real_again: list[BenchRow], tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first, second = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    bench_rows.write_rows(first, real)
    bench_rows.write_rows(second, real_again)
    code = ci_main([str(first), "--repeat", str(second), "--simulate", "200"])
    out = capsys.readouterr().out
    # The quick tier has no noisy control, so the Section 13 part fails on purpose;
    # every Phase 10 line is absent from the failures.
    assert code == 1
    assert "fail no noisy rows" in out
    assert "routed" not in out and "simulation" not in out and "determinism" not in out
    assert ci_main([str(first), "--simulate", "x"]) == 2
    assert ci_main(["--repeat", str(first)]) == 2
    broken = [row.model_copy(update={"route": None}) if row.variant == "routed" else row
              for row in real]
    bench_rows.write_rows(second, broken)
    assert ci_main([str(first), "--repeat", str(second)]) == 1
    assert "differs between identical runs" in capsys.readouterr().out


def test_image_check_exempts_reuse_and_foreign_rows_only(real: list[BenchRow]) -> None:
    rows = [row for row in real if row.variant == "routed"]
    exact = next(r for r in rows if r.route and r.route.probe == "exact")
    foreign = next(r for r in rows if r.route and r.route.probe == "foreign")
    sibling = next(r for r in rows if r.route and r.route.probe == "sibling")
    imageless = {"kind": "vision", "image_inputs": 0}
    lines = check_rows(
        [
            exact.model_copy(update=imageless),
            foreign.model_copy(update=imageless),
            sibling.model_copy(update=imageless),
        ]
    )
    assert [line for line in lines if line.startswith("no images")] == [
        f"no images sent: {sibling.task_id} routed"
    ]


# ------------------------------------------------- review regressions (WP4 fix 1)


def test_routed_rows_carry_a_content_free_target(real: list[BenchRow]) -> None:
    """Criterion 4 compares (action, target, chosen_rank): every executed reuse or
    warm start names its target as an ``h_`` category, a compile names none."""
    for row in routed(real):
        route = row.route
        assert route is not None
        if route.action in ("reuse_installed", "warm_start"):
            assert route.target is not None and route.target.startswith("h_")
            assert len(route.target) == 34
        else:
            assert route.target is None
    exact = [r.route.target for r in routed(real) if r.route and r.route.probe == "exact"]
    sibling = [r.route.target for r in routed(real) if r.route and r.route.probe == "sibling"]
    assert set(exact).isdisjoint(sibling)  # an installed entry is not its template


def test_determinism_sees_a_different_target_at_the_same_rank(
    real: list[BenchRow], real_again: list[BenchRow]
) -> None:
    assert determinism_failures(real, real_again) == []
    broken = list(real_again)
    index = next(i for i, row in enumerate(broken) if row.route and row.route.probe == "exact")
    route = broken[index].route
    assert route is not None
    # the same action, tier, candidates and rank, but another installed entry
    other = RouteFields.model_validate(
        {**route.model_dump(mode="json"), "target": "h_" + "0" * 32}
    )
    broken[index] = broken[index].model_copy(update={"route": other})
    failures = determinism_failures(real, broken)
    assert len(failures) == 1 and "differs between identical runs" in failures[0]


# ------------------------------------------------- review regressions (WP4 fix 2)


def _forced(monkeypatch: pytest.MonkeyPatch, **force: Any) -> None:
    """Replace the router the simulation drives with one that breaks an 8.3 rule."""
    from architect.bench import router_sim

    original = router_sim.route

    def broken(probe: Any, **kwargs: Any) -> Any:
        k = kwargs["policy"].explore_every
        if force.get("every_step") and k > 0:
            kwargs["counter"] = k - 1  # ignores ``counter % k == k - 1``
        if force.get("no_cap"):
            kwargs["explore_spent_usd"] = 0.0  # ignores the daily cap
        return original(probe, **kwargs)

    monkeypatch.setattr(router_sim, "route", broken)


def test_exploration_scenario_saturates_the_every_kth_rule() -> None:
    suite = simulation_suite(2000)
    explore = suite.exploration
    k = explore.explore_every
    # exploration was possible at every k-th decision: the bound is really tested
    assert explore.exploration_share == pytest.approx(1 / k, abs=0.002)
    assert suite.cap.explored_over_cap == 0
    assert any(s.explore_spent_usd >= suite.cap.explore_usd_per_day for s in suite.cap.steps)


def test_exploration_share_check_catches_a_router_exploring_every_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forced(monkeypatch, every_step=True)
    failures = simulation_failures(simulation_suite(2000))
    assert any(line.startswith("exploration share") for line in failures)


def test_exploration_cap_check_catches_a_router_ignoring_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forced(monkeypatch, no_cap=True)
    failures = simulation_failures(simulation_suite(2000))
    assert any("cap exceeded" in line for line in failures)


def test_a_starved_exploration_scenario_is_reported() -> None:
    good = simulation_suite(40)
    starved = good.exploration.model_copy(
        update={"steps": tuple(_step(i) for i in range(40))}
    )
    failures = simulation_failures(good.model_copy(update={"exploration": starved}))
    assert any("exploration scenario starved" in line for line in failures)


# ------------------------------------------------- review regressions (WP4 fix 3)


def _live(row: BenchRow, **update: Any) -> BenchRow:
    values: dict[str, Any] = {
        "provider": "claude-code",
        "model": "live-m",
        "evidence": "live_api_equivalent",
        "cost_usd": 1.0,
    }
    values.update(update)
    return row.model_copy(update=values)


def test_learning_curve_never_mixes_evidence_classes(real: list[BenchRow]) -> None:
    live = [_live(row, licensed=False) for row in routed(real)]
    curve = learning_curve([*real, *live])
    assert [(p.round, p.evidence) for p in curve.points] == [
        (0, "live_api_equivalent"), (0, "synthetic_fake"),
        (1, "live_api_equivalent"), (1, "synthetic_fake"),
    ]
    for point in curve.points:
        assert point.rows == 9
        if point.evidence == "synthetic_fake":
            assert point.licensed_rate == 1.0 and point.spend_usd < 1.0
        else:
            assert point.licensed_rate == 0.0 and point.spend_usd == pytest.approx(9.0)
    assert curve.evidence == ("live_api_equivalent", "synthetic_fake")


def test_regret_table_measures_each_class_against_its_own_oracle(
    real: list[BenchRow],
) -> None:
    fake = [row.model_copy(update={"held_out_score": 0.6}) for row in real]
    live = [_live(row, held_out_score=1.0) for row in real if row.variant == "default"]
    table = regret_table([*fake, *live])
    cells = [cell for cell in table.cells if cell.evidence == "synthetic_fake"]
    assert cells and all(cell.mean_regret == pytest.approx(0.0) for cell in cells)
    assert {cell.evidence for cell in table.cells} == {"synthetic_fake", "live_api_equivalent"}
    assert table.oracle_models == ("fake", "live-m")


# ------------------------------------------------- review regressions (WP4 fix 4)


def test_routed_phase_a_forge_build_cost_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from architect.bench import runner

    absorb = runner._ForgeTally.absorb

    def priced(self: Any, artifact: Any) -> None:
        absorb(self, artifact)
        if self.settings.mode == "agent":
            self.agent_cost_usd = (self.agent_cost_usd or 0.0) + 0.125

    monkeypatch.setattr(runner._ForgeTally, "absorb", priced)
    rows = _run(tmp_path, variants=("default", "forge", "routed"))
    forged = {row.task_id: row for row in rows if row.variant == "forge"}
    for row in routed(rows):
        assert row.route is not None
        if row.route.probe != "exact":
            assert row.forge is None
            continue
        forge = row.forge
        assert forge is not None and forge.mode == "agent" and forge.measures >= 1
        assert forge.agent_cost_usd == pytest.approx(0.125)  # build cost, reported apart
        assert forge.measures == forged[row.task_id].forge.measures  # type: ignore[union-attr]
        assert row.cost_usd == 0.0  # never folded into the reused row's cost
    text = bench_table(rows)
    process = text[text.index("process (forge counts"):]
    assert "routed" in process and "$0.1250" in process
    assert check_rows(rows) == ["no noisy rows: the negative control did not run"]


# ------------------------------------------------- review regressions (WP4 fix 5)


def _delivery(spec_hash: str, n: int) -> Any:
    from datetime import UTC, datetime
    from uuid import uuid4

    from architect.spec import FloorKind, Lifecycle
    from architect.telemetry import TelemetryRow, categorical

    return TelemetryRow(
        run_id=str(uuid4()), timestamp=datetime.now(UTC), domain=categorical("general"),
        task_type=categorical("extract"), spec_hash=spec_hash, harness_version=1,
        lifecycle=Lifecycle.INSTALL, preset_origin=categorical("extract"), component_set=(),
        model=categorical("fake"), model_params={}, tokens_in=1, tokens_out=1, cost_usd=0.001,
        wall_time_s=float(n + 1), floor_type=FloorKind.HELD_OUT_TASKS, floor_score=None,
        floor_limit=0.5, accepted=True,
    )


def _cache_tables(path: Path) -> tuple[list[Any], list[Any], list[Any]]:
    import sqlite3

    with sqlite3.connect(path) as db:
        return (
            db.execute("SELECT * FROM meta ORDER BY key").fetchall(),
            db.execute("SELECT * FROM versions ORDER BY 1, 2, 3").fetchall(),
            db.execute("SELECT * FROM roles ORDER BY 1, 2, 3, 4").fetchall(),
        )


def test_a_longer_journal_after_a_shorter_one_replays_into_a_fresh_cache(
    tmp_path: Path,
) -> None:
    """One bench route log, two cells: the second cell's journal is longer than the
    first cell's final cache offset. Its evidence must be its own journal, whole."""
    from architect.bench.runner import _fresh_evidence_cache
    from architect.routing import PROVIDER_EVIDENCE
    from architect.routing.evidence import _TelemetryCache
    from architect.telemetry import TelemetryStore, categorical

    log_root = tmp_path / "routes"
    classes = {categorical("fake"): PROVIDER_EVIDENCE["fake"]}
    spec_a, spec_b = "a" * 64, "b" * 64
    first = TelemetryStore(tmp_path / "cell-a" / "telemetry")
    for n in range(4):
        first.append(_delivery(spec_a, n))
    second = TelemetryStore(tmp_path / "cell-b" / "telemetry")
    for n in range(12):
        second.append(_delivery(spec_b, n))
    journal_a = Path(first.directory) / "runs.jsonl"
    journal_b = Path(second.directory) / "runs.jsonl"
    assert journal_b.stat().st_size > journal_a.stat().st_size

    _fresh_evidence_cache(log_root)  # nothing to drop yet
    cell_a = _TelemetryCache(log_root / "evidence.sqlite", journal_a)
    cell_a.sync(classes)
    assert cell_a.version(spec_a, 1, PROVIDER_EVIDENCE["fake"])[0] == 4

    _fresh_evidence_cache(log_root)
    cell_b = _TelemetryCache(log_root / "evidence.sqlite", journal_b)
    cell_b.sync(classes)
    assert cell_b.full_replays == 1
    assert cell_b.version(spec_b, 1, PROVIDER_EVIDENCE["fake"])[0] == 12  # all its own rows
    assert cell_b.version(spec_a, 1, PROVIDER_EVIDENCE["fake"])[0] == 0  # none of cell A's
    fresh = _TelemetryCache(tmp_path / "scratch" / "evidence.sqlite", journal_b)
    fresh.sync(classes)
    assert _cache_tables(cell_b.path) == _cache_tables(fresh.path)


class _CheckedSession:
    """The real routing session; after every decision, the shared cache must equal a
    from-scratch replay of this cell's own telemetry journal."""

    def __init__(self, session: Any, context: Any, scratch: Path, seen: list[int]) -> None:
        from architect.routing import PROVIDER_EVIDENCE
        from architect.telemetry import categorical

        self._session = session
        self._cache = context.log_root / "evidence.sqlite"
        self._journal = Path(context.telemetry.directory) / "runs.jsonl"
        self._classes = {
            categorical(model): PROVIDER_EVIDENCE[provider]
            for model, provider in context.providers.items()
        }
        self._scratch = scratch
        self._seen = seen

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    def route(self, spec: Any, **kwargs: Any) -> Any:
        from architect.routing.evidence import _TelemetryCache

        view = self._session.route(spec, **kwargs)
        self._seen.append(self._journal.stat().st_size)
        replay = _TelemetryCache(self._scratch / f"{len(self._seen)}.sqlite", self._journal)
        replay.sync(self._classes)
        assert self._cache.exists()
        assert _cache_tables(self._cache) == _cache_tables(replay.path)
        return view


def test_each_routed_cell_replays_only_its_own_journal(tmp_path: Path) -> None:
    from architect.bench.runner import open_router

    opened: list[bool] = []
    shared: list[bool] = []
    seen: list[int] = []

    def opener(context: Any) -> Any:
        opened.append((context.log_root / "evidence.sqlite").exists())
        shared.append((context.log_root / "decisions.jsonl").exists())
        return _CheckedSession(open_router(context), context, tmp_path / "replays", seen)

    rows = _run(tmp_path, routing=opener, rounds=2)
    cells = [row for row in routed(rows) if row.route and row.route.probe == "exact"]
    assert len(opened) == len(cells) >= 2
    assert not any(opened)  # no cell opens on a cache an earlier cell left behind
    assert shared[0] is False and all(shared[1:])  # the decision log stays bench-wide
    assert len(seen) == len(cells) * len(PROBES)
    assert all("route_did_not_run" not in row.failures for row in routed(rows))
