"""Phase 10 WP2: role routing (contract Sections 11, 15.2).

Fake adapters only; sockets are blocked. The evidence index is a test-local stub
(``StubIndex``) standing in for WP1's ``EvidenceIndex``; the route log is WP1's.
Fixture evidence is synthetic: it exercises plumbing and decision rules, not quality.
"""

from __future__ import annotations

import json
import re
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from architect import profiles
from architect.compiler.inference import _INFERENCE_BUDGET
from architect.models import FakeAdapter, ModelAdapter
from architect.profiles import (
    WORKSPACE_STATE,
    Profile,
    ProfileError,
    Services,
    build_services,
    load_profile,
    pi_denied_paths,
    role_defaults,
)
from architect.routing.labels import EvidenceClass, Provider, RoleName
from architect.routing.log import RoleRecord, RouteLog, RouteLogError
from architect.routing.roles import (
    INFERENCE_BUDGET_USD,
    JUDGE_PRESET,
    ROLE_NAMES,
    ROLE_PRESETS,
    RoleAdapters,
    RoleRouter,
    RoleRouting,
    RoleRoutingError,
    RoleVariant,
    check_roles,
    outcome_success,
    outcome_weight,
    wilson,
)
from architect.routing.router import RoutePolicy, Router
from architect.spec import Budget, FloorDefinition, FloorKind, Spec

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse every non-loopback connection (the event loop's self-pipe stays usable)."""

    def guarded(original: Any) -> Any:
        def connect(self: socket.socket, address: Any, *args: Any) -> Any:
            host = address[0] if isinstance(address, tuple) else address
            if host not in _LOOPBACK:
                raise AssertionError("network access in a WP2 test")
            return original(self, address, *args)

        return connect

    monkeypatch.setattr(socket.socket, "connect", guarded(socket.socket.connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guarded(socket.socket.connect_ex))


# ---------------------------------------------------------------- test-local stubs


@dataclass(frozen=True)
class Stats:
    n: float = 0.0
    successes: float = 0.0
    mean_cost_usd: float | None = None
    median_wall_s: float | None = None
    evidence_class: EvidenceClass = "synthetic_fake"


class StubIndex:
    """A stand-in for WP1's ``EvidenceIndex.role_stats``: a fixed table per bucket."""

    def __init__(self, table: Mapping[tuple[str, str | None], Stats] | None = None) -> None:
        self.table = dict(table or {})
        self.calls: list[tuple[RoleName, str, str | None]] = []

    def role_stats(self, role: RoleName, variant: RoleVariant, domain: str | None) -> Stats:
        self.calls.append((role, variant.key(), domain))
        return self.table.get((variant.key(), domain), Stats())


class BrokenIndex:
    def role_stats(self, role: RoleName, variant: RoleVariant, domain: str | None) -> Stats:
        raise RouteLogError("evidence_store_unavailable")


D = RoleVariant(model="fake")
CHEAP = RoleVariant(model="llama3.2")
OTHER = RoleVariant(model="llama3.2-vision")
PROVIDERS: dict[str, Provider] = {"fake": "fake", "llama3.2": "fake", "llama3.2-vision": "fake"}


def routing(**updates: Any) -> RoleRouting:
    values: dict[str, Any] = {
        "policy": "evidence",
        "candidates": {"inference": (CHEAP, OTHER), "decomposition": (CHEAP,),
                       "rewrite": (CHEAP,), "forge_agent": (CHEAP,), "floor_draft": (CHEAP,)},
    }
    values.update(updates)
    return RoleRouting.model_validate(values)


def router(
    tmp_path: Path,
    table: Mapping[tuple[str, str | None], Stats],
    *,
    settings: RoleRouting | None = None,
    providers: Mapping[str, Provider] = PROVIDERS,
    prices: Mapping[str, float] | None = None,
) -> tuple[RoleRouter, RouteLog, StubIndex]:
    log = RouteLog(tmp_path / "routes")
    index = StubIndex(table)
    price = dict(prices or {})
    role_router = RoleRouter(
        settings or routing(), index, log, providers=providers,
        quote=lambda model: price.get(model, 0.0),
    )
    return role_router, log, index


def stats(n: float, successes: float, cost: float | None = None, wall: float | None = None,
          evidence: EvidenceClass = "synthetic_fake") -> Stats:
    return Stats(n=n, successes=successes, mean_cost_usd=cost, median_wall_s=wall,
                 evidence_class=evidence)


def write_profile(tmp_path: Path, **updates: object) -> Path:
    payload: dict[str, object] = {"models": {"fake": {"provider": "fake"}}}
    payload.update(updates)
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def named_profile(tmp_path: Path, **updates: object) -> Profile:
    """Every role adapter configured, all fake."""
    payload: dict[str, object] = {
        "models": {"fake": {"provider": "fake"}, "llama3.2": {"provider": "fake"}},
        "inference": {"provider": "fake", "model": "fake"},
        "decomposition": {"provider": "fake", "model": "fake"},
        "rewrite": {"provider": "fake", "model": "fake"},
        "forge_agent": {"provider": "fake", "model": "llama3.2"},
    }
    payload.update(updates)
    return load_profile(write_profile(tmp_path, **payload))


# ---------------------------------------------------------------- pinned default (P4)


def test_pinned_adapters_are_todays_objects(tmp_path: Path) -> None:
    services = build_services(named_profile(tmp_path), tmp_path / "work")
    try:
        roles = services.roles
        assert roles.policy == "pinned" and roles.router is None
        assert roles.adapter("inference") is services.compiler.inference_adapter
        assert roles.adapter("decomposition") is services.orchestrator.decomposition_adapter
        assert roles.adapter("rewrite") is services.compiler.rewrite_adapter
        assert roles.adapter("forge_agent") is services.forge_adapter
        assert roles.adapter("forge_agent") is services.compiler.forge_adapter
        assert roles.adapter("floor_draft") is None  # Session._draft keeps its own rule
        for role in ROLE_NAMES:
            assert roles.adapter(role, domain="invoices") is roles.adapter(role)
            choice = roles.choose(role)
            assert choice.codes == () and choice.explored is False
            assert choice.adapter is roles.adapter(role)  # None too (floor_draft)
            roles.record(choice, "success")
        assert services.forge_adapter is not None and services.forge_adapter.model == "llama3.2"
        assert roles.defaults["forge_agent"][1] == RoleVariant(model="llama3.2")
        assert not (tmp_path / "work" / "routes" / "roles.jsonl").exists()
    finally:
        services.close()


def test_pinned_defaults_keep_each_call_sites_fallback(tmp_path: Path) -> None:
    profile = load_profile(write_profile(tmp_path, models={
        "llama3.2": {"provider": "fake"}, "fake": {"provider": "fake"},
    }, compile={"model_ladder": ["fake"]}))
    services = build_services(profile, tmp_path / "work")
    try:
        roles = services.roles
        # Stage 0 falls back to adapter_factory("fake") in the Compiler (line 183).
        assert roles.adapter("inference") is None and services.compiler.inference_adapter is None
        assert roles.defaults["inference"][1] == RoleVariant(model="fake")
        # decomposition = profile.decomposition, else inference, else the first models key.
        decomposer = services.orchestrator.decomposition_adapter
        assert roles.adapter("decomposition") is decomposer and decomposer.model == "llama3.2"
        assert roles.adapter("rewrite") is None and roles.adapter("forge_agent") is None
        assert roles.defaults["floor_draft"][1] == RoleVariant(model="fake")  # ladder[0]
        assert roles.choose("inference").adapter.model == "fake"
    finally:
        services.close()


def test_services_built_by_hand_get_the_pinned_binding(tmp_path: Path) -> None:
    built = build_services(named_profile(tmp_path), tmp_path / "work")
    try:
        manual = Services(
            compiler=built.compiler, orchestrator=built.orchestrator, registry=built.registry,
            runtime=built.runtime, profile=built.profile, workspace=built.workspace,
            adapter_factory=built.adapter_factory, forge_adapter=built.forge_adapter,
        )
        assert manual.router is None and isinstance(manual.roles, RoleAdapters)
        for role in ROLE_NAMES:
            assert manual.roles.adapter(role) is built.roles.adapter(role)
    finally:
        built.close()


async def test_pinned_policy_sends_identical_requests(tmp_path: Path) -> None:
    """A legacy profile and one with explicit pinned roles plus declared candidates
    build the same adapters and send byte-identical Stage 0 and compile requests."""
    proposal = Spec(
        task="Return ok", input_schema={"type": "object"}, output_schema={"type": "string"},
        domain="synthesizer",
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="tasks"),
        budget=Budget(),
    ).model_dump_json()
    outcomes = []
    for label, extra in (
        ("legacy", {}),
        ("pinned", {"roles": {"policy": "pinned", "candidates": {
            "inference": [{"model": "llama3.2"}],
            "decomposition": [{"model": "llama3.2", "params": {"max_tasks": 4}}],
            "forge_agent": [{"model": "llama3.2", "params": {"mode": "template"}}],
        }}, "routing": {"enabled": True}}),
    ):
        root = tmp_path / label
        root.mkdir()
        (root / "tasks.json").write_text(json.dumps([
            {"case_id": str(i), "task": {}, "expected": "ok"} for i in range(6)
        ]), encoding="utf-8")
        (root / "inference.json").write_text(json.dumps([
            {"text": proposal, "tokens_in": 8, "tokens_out": 12, "cost_usd": 0.00002,
             "model": "fake"}
        ]), encoding="utf-8")
        profile = load_profile(write_profile(
            root,
            models={"fake": {"provider": "fake"}, "llama3.2": {"provider": "fake"}},
            inference={"provider": "fake", "model": "fake", "fixture": "inference.json"},
            floors={"tasks": {"kind": "held_out_tasks", "path": "tasks.json"}},
            **extra,
        ))
        services = build_services(profile, root / "work")
        try:
            adapter = services.roles.adapter("inference")
            assert adapter is services.compiler.inference_adapter
            assert isinstance(adapter, FakeAdapter)
            result = await services.compiler.compile("Return ok")
            rows = sorted(
                (row.task_type, row.model, row.tokens_in, row.tokens_out, row.accepted)
                for row in services.runtime.store.rows()
            )
            outcomes.append((
                adapter.calls, list(adapter.seen), result.spec, result.stages, result.failures,
                result.artifact is not None and result.artifact.licensed, rows,
                services.compiler.config, services.orchestrator.config,
                services.orchestrator.decomposition_adapter.model,
                [services.roles.defaults[role][1] for role in ROLE_NAMES],
            ))
        finally:
            services.close()
    assert outcomes[0] == outcomes[1]
    assert outcomes[0][0] == 1 and outcomes[0][5] is True


def test_profile_defaults(tmp_path: Path) -> None:
    profile = load_profile(write_profile(tmp_path))
    assert profile.roles == RoleRouting() and profile.roles.policy == "pinned"
    assert profile.routing == RoutePolicy() and profile.routing.explore_every == 0
    assert RoleRouting().model_dump() == {
        "policy": "pinned", "candidates": {}, "min_samples": 20, "non_inferiority": 0.02,
        "z": 1.64, "explore_every": 0,
    }
    assert RoutePolicy().model_dump() == {
        "enabled": True, "reuse_origins": frozenset(), "min_template_reliability": 0.3,
        "explore_every": 0, "explore_usd_per_day": 1.0, "max_candidates": 8,
        "promote_margin": 0.0, "child_warm_start": False, "subtask_models": False,
    }
    with pytest.raises(ValidationError):
        RoleRouting(min_samples=4)
    with pytest.raises(ValidationError):
        RoleRouting.model_validate({"policy": "bandit"})


# ---------------------------------------------------------------- evidence policy (11.4)


def test_non_inferior_cheaper_variant_is_chosen(tmp_path: Path) -> None:
    table = {
        (D.key(), None): stats(100, 80, cost=0.10),
        (CHEAP.key(), None): stats(40, 36, cost=0.05),
    }
    role_router, log, _ = router(tmp_path, table)
    assert wilson(36, 40, 1.64) >= 0.80 - 0.02
    variant, decision, explored, codes = role_router.choose("inference", domain=None, default=D)
    assert variant == CHEAP and not explored and codes == ()
    [record] = list(log.roles("inference"))
    assert record.decision_id == decision and record.outcome is None
    assert record.chosen_rank == 0 and record.n == 40
    assert record.p_lcb == pytest.approx(wilson(36, 40, 1.64))


def test_inferior_variant_is_not_chosen_even_when_cheaper(tmp_path: Path) -> None:
    table = {
        (D.key(), None): stats(100, 80, cost=0.10),
        (CHEAP.key(), None): stats(40, 28, cost=0.01),
    }
    role_router, log, _ = router(tmp_path, table)
    assert wilson(28, 40, 1.64) < 0.78
    variant, _, _, codes = role_router.choose("inference", domain=None, default=D)
    assert variant == D and codes == ()
    [record] = list(log.roles("inference"))
    assert record.chosen_rank == 0 and record.n == 100


def test_pinned_default_wins_ties_and_unknown_cost_sorts_last(tmp_path: Path) -> None:
    table = {
        (D.key(), None): stats(50, 40, cost=0.05, wall=2.0),
        (CHEAP.key(), None): stats(200, 190, cost=0.05, wall=2.0),
        (OTHER.key(), None): stats(200, 190, cost=None, wall=0.1),
    }
    role_router, _, _ = router(tmp_path, table)
    assert role_router.choose("inference", domain=None, default=D)[0] == D
    faster = {**table, (CHEAP.key(), None): stats(200, 190, cost=0.05, wall=1.0)}
    role_router, _, _ = router(tmp_path / "b", faster)
    assert role_router.choose("inference", domain=None, default=D)[0] == CHEAP


def test_pinned_default_without_outcomes_is_judged_at_one_half(tmp_path: Path) -> None:
    table = {(CHEAP.key(), None): stats(20, 12, cost=0.01)}  # W(12, 20) ~ 0.42 < 0.48
    role_router, _, _ = router(tmp_path, table)
    assert wilson(12, 20, 1.64) < 0.5 - 0.02
    assert role_router.choose("inference", domain=None, default=D)[0] == D
    table = {(CHEAP.key(), None): stats(20, 17, cost=0.01)}
    role_router, _, _ = router(tmp_path / "b", table)
    assert wilson(17, 20, 1.64) >= 0.48
    assert role_router.choose("inference", domain=None, default=D)[0] == CHEAP


@pytest.mark.parametrize(("n", "switched"), [(19, False), (20, True)])
def test_threshold_is_exactly_min_samples(tmp_path: Path, n: int, switched: bool) -> None:
    table = {
        (D.key(), None): stats(100, 50, cost=0.10),
        (CHEAP.key(), None): stats(n, n, cost=0.01),
    }
    role_router, _, _ = router(tmp_path, table)
    variant, _, _, codes = role_router.choose("inference", domain=None, default=D)
    assert (variant == CHEAP) is switched
    assert codes == (() if switched else ("role_evidence_insufficient",))


def test_thin_domain_bucket_falls_back_to_role_wide(tmp_path: Path) -> None:
    table = {
        (D.key(), None): stats(100, 80, cost=0.10),
        (CHEAP.key(), "invoices"): stats(3, 3, cost=0.01),
        (CHEAP.key(), None): stats(40, 38, cost=0.01),
    }
    role_router, log, index = router(tmp_path, table)
    variant, _, _, _ = role_router.choose("inference", domain="invoices", default=D)
    assert variant == CHEAP
    assert ("inference", CHEAP.key(), None) in index.calls
    [record] = list(log.roles("inference"))
    assert re.fullmatch(r"h_[0-9a-f]{32}", record.domain)


def test_inference_quote_cap_is_one_dollar(tmp_path: Path) -> None:
    assert INFERENCE_BUDGET_USD == _INFERENCE_BUDGET.cost_usd == 1.0
    table = {
        (D.key(), None): stats(100, 80, cost=0.10),
        (CHEAP.key(), None): stats(40, 40, cost=0.01),
    }
    role_router, _, _ = router(tmp_path, table, prices={"llama3.2": 1.0})
    assert role_router.choose("inference", domain=None, default=D)[0] == CHEAP
    role_router, _, _ = router(tmp_path / "b", table, prices={"llama3.2": 1.0001})
    variant, _, _, codes = role_router.choose("inference", domain=None, default=D)
    assert variant == D and codes == ("role_evidence_insufficient",)
    # Other roles have no fixed quote budget.
    assert role_router.choose("rewrite", domain=None, default=D)[0] == CHEAP


def test_decomposition_budget_is_one_dollar_and_sixty_seconds(tmp_path: Path) -> None:
    table = {
        (D.key(), None): stats(100, 80, cost=0.10),
        (CHEAP.key(), None): stats(40, 40, cost=0.01, wall=61.0),
    }
    role_router, _, _ = router(tmp_path, table)
    assert role_router.choose("decomposition", domain=None, default=D)[0] == D
    table[(CHEAP.key(), None)] = stats(40, 40, cost=0.01, wall=60.0)
    role_router, _, _ = router(tmp_path / "b", table, prices={"llama3.2": 2.0})
    assert role_router.choose("decomposition", domain=None, default=D)[0] == D
    role_router, _, _ = router(tmp_path / "c", table, prices={"llama3.2": 0.5})
    assert role_router.choose("decomposition", domain=None, default=D)[0] == CHEAP


def test_evidence_classes_are_never_mixed_after_a_provider_change(tmp_path: Path) -> None:
    # "llama3.2" gathered 40 fake outcomes, then its provider became a billed API.
    table = {
        (D.key(), None): stats(100, 80, cost=0.10),
        (CHEAP.key(), None): stats(40, 40, cost=0.01, evidence="synthetic_fake"),
    }
    moved: dict[str, Provider] = {**PROVIDERS, "llama3.2": "anthropic"}
    role_router, _, _ = router(tmp_path, table, providers=moved)
    variant, _, _, codes = role_router.choose("inference", domain=None, default=D)
    assert variant == D and codes == ("role_evidence_insufficient",)
    # The pinned default's evidence of another class is no evidence either: D has
    # p_hat 0.5, so a same-class variant clearing 0.48 switches.
    table = {
        (D.key(), None): stats(100, 99, cost=0.10, evidence="live_billed"),
        (CHEAP.key(), None): stats(40, 30, cost=0.01),
    }
    role_router, _, _ = router(tmp_path / "b", table)
    assert wilson(30, 40, 1.64) >= 0.48
    assert role_router.choose("inference", domain=None, default=D)[0] == CHEAP
    # A model with no provider is never eligible.
    unknown = RoleRouting.model_validate({"policy": "evidence", "candidates": {
        "inference": [{"model": "ghost"}]}})
    table = {(RoleVariant(model="ghost").key(), None): stats(99, 99, cost=0.0)}
    role_router, _, _ = router(tmp_path / "c", table, settings=unknown)
    assert role_router.choose("inference", domain=None, default=D)[0] == D


def test_exploration_is_every_kth_call_to_the_least_sampled(tmp_path: Path) -> None:
    table = {
        (D.key(), None): stats(100, 80, cost=0.10),
        (CHEAP.key(), None): stats(10, 9, cost=0.05),
        (OTHER.key(), None): stats(2, 1, cost=0.20),
    }
    role_router, log, _ = router(tmp_path, table, settings=routing(explore_every=3))
    picks = [role_router.choose("inference", domain=None, default=D) for _ in range(6)]
    assert [pick[0] for pick in picks] == [D, D, OTHER, D, D, OTHER]
    assert [pick[2] for pick in picks] == [False, False, True, False, False, True]
    assert picks[2][3] == ("role_evidence_insufficient", "role_explore")
    records = list(log.roles("inference"))
    assert [record.explored for record in records] == [pick[2] for pick in picks]
    # The counter is persisted: a fresh router over the same log continues it.
    fresh = RoleRouter(routing(explore_every=3), StubIndex(table), RouteLog(tmp_path / "routes"),
                       providers=PROVIDERS, quote=lambda model: 0.0)
    assert [fresh.choose("inference", domain=None, default=D)[2] for _ in range(3)] == [
        False, False, True]
    # Exploration respects the budget filter.
    capped, _, _ = router(tmp_path / "b", table, settings=routing(explore_every=1),
                          prices={"llama3.2-vision": 5.0})
    assert capped.choose("inference", domain=None, default=D)[0] == CHEAP


def test_pinned_policy_router_never_logs(tmp_path: Path) -> None:
    table = {(CHEAP.key(), None): stats(99, 99, cost=0.0)}
    role_router, log, index = router(tmp_path, table, settings=routing(policy="pinned"))
    variant, _, explored, codes = role_router.choose("inference", domain=None, default=D)
    assert (variant, explored, codes) == (D, False, ())
    assert index.calls == [] and list(log.roles("inference")) == []


def test_unreadable_evidence_fails_open_to_the_pinned_default(tmp_path: Path) -> None:
    broken = RoleRouter(routing(), BrokenIndex(), RouteLog(tmp_path / "routes"),
                        providers=PROVIDERS, quote=lambda model: 0.0)
    variant, _, explored, codes = broken.choose("inference", domain=None, default=D)
    assert (variant, explored, codes) == (D, False, ("evidence_store_unavailable",))
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    table = {(D.key(), None): stats(100, 80, 0.1), (CHEAP.key(), None): stats(40, 40, 0.01)}
    unwritable = RoleRouter(routing(explore_every=2), StubIndex(table), RouteLog(blocked),
                            providers=PROVIDERS, quote=lambda model: 0.0)
    assert unwritable.choose("inference", domain=None, default=D)[3] == (
        "evidence_store_unavailable",)
    no_explore = RoleRouter(routing(), StubIndex(table), RouteLog(blocked),
                            providers=PROVIDERS, quote=lambda model: 0.0)
    assert no_explore.choose("inference", domain=None, default=D)[0] == CHEAP  # append swallowed


# ---------------------------------------------------------------- the seam (11.3)


class Factory:
    def __init__(self) -> None:
        self.built: list[str] = []

    def __call__(self, model: str) -> ModelAdapter:
        self.built.append(model)
        return FakeAdapter(model=model)


def seam(
    tmp_path: Path, table: Mapping[tuple[str, str | None], Stats], *,
    pins: Callable[[RoleName], bool] = lambda role: False,
) -> tuple[RoleAdapters, dict[RoleName, FakeAdapter], Factory, RouteLog]:
    defaults_adapters: dict[RoleName, FakeAdapter] = {
        role: FakeAdapter(model="fake") for role in ROLE_NAMES
    }
    defaults = {role: (defaults_adapters[role], D) for role in ROLE_NAMES}
    role_router, log, _ = router(tmp_path, table)
    factory = Factory()
    return RoleAdapters(defaults, role_router, factory, pins), defaults_adapters, factory, log


def test_evidence_seam_keeps_default_identity_and_builds_switched_adapters(
    tmp_path: Path,
) -> None:
    table = {(D.key(), None): stats(100, 80, 0.10), (CHEAP.key(), None): stats(40, 40, 0.01)}
    roles, defaults, factory, log = seam(tmp_path, table)
    assert roles.policy == "evidence"
    choice = roles.choose("inference")
    assert choice.variant == CHEAP and choice.adapter.model == "llama3.2"
    assert factory.built == ["llama3.2"]
    roles.record(choice, "success", floor_origin="user_dataset")
    decision, outcome = [r for r in log.roles("inference")]
    assert outcome.decision_id == decision.decision_id == choice.decision_id
    assert outcome.outcome == "success" and outcome.n == 1.0
    empty, defaults, factory, _ = seam(tmp_path / "b", {})
    assert empty.adapter("inference") is defaults["inference"]
    assert empty.choose("rewrite").adapter is defaults["rewrite"] and factory.built == []
    assert empty.adapter("rewrite", domain="invoices") is defaults["rewrite"]


def test_pins_disable_routing(tmp_path: Path) -> None:
    table = {(D.key(), None): stats(100, 80, 0.10), (CHEAP.key(), None): stats(40, 40, 0.01)}
    roles, defaults, factory, log = seam(tmp_path, table, pins=lambda role: role == "forge_agent")
    choice = roles.choose("forge_agent")
    assert choice.codes == ("role_pinned",) and choice.variant == D
    assert choice.adapter is defaults["forge_agent"] and factory.built == []
    assert roles.adapter("forge_agent") is defaults["forge_agent"]
    roles.record(choice, "success")
    assert list(log.roles("forge_agent")) == []
    assert roles.choose("inference").variant == CHEAP  # other roles still route


def test_forge_agent_model_pins_the_forge_role(tmp_path: Path) -> None:
    profile = named_profile(tmp_path, compile={"forge": {"agent_model": "llama3.2"}})
    services = build_services(profile, tmp_path / "work")
    try:
        choice = services.roles.choose("forge_agent")
        assert choice.codes == ("role_pinned",)
        assert choice.adapter is services.forge_adapter
        assert services.roles.choose("inference").codes == ()
    finally:
        services.close()


def test_floor_origin_weights_on_recorded_outcomes(tmp_path: Path) -> None:
    assert outcome_weight("user_dataset") == outcome_weight("example_fixture") == 1.0
    assert outcome_weight("synthetic_generated") == outcome_weight("model_draft") == 0.5
    assert outcome_weight("authored_cases") == 0.5 and outcome_weight(None) == 1.0
    assert outcome_success("success") == 1.0 and outcome_success("failure") == 0.0
    assert outcome_success("partial", 1.0) == 1.0 and outcome_success("partial", 0.5) == 0.0
    assert outcome_success("excluded") is None
    table = {(D.key(), None): stats(100, 80, 0.10), (CHEAP.key(), None): stats(40, 40, 0.01)}
    roles, _, _, log = seam(tmp_path, table)
    for origin in ("user_dataset", "model_draft", None):
        roles.record(roles.choose("rewrite"), "failure", floor_origin=origin)
    roles.record(roles.choose("decomposition"), "partial", score=0.5)
    weights = [(r.floor_origin, r.n) for r in log.roles("rewrite") if r.outcome is not None]
    assert weights == [("user_dataset", 1.0), ("model_draft", 0.5), (None, 1.0)]
    [partial] = [r for r in log.roles("decomposition") if r.outcome is not None]
    assert (partial.outcome, partial.score) == ("partial", 0.5)
    # An outcome is recorded once per choice; a repeat is a no-op.
    choice = roles.choose("floor_draft")
    roles.record(choice, "excluded")
    roles.record(choice, "excluded")
    assert sum(1 for r in log.roles("floor_draft") if r.outcome is not None) == 1


def test_role_records_are_content_free(tmp_path: Path) -> None:
    secret = "Summarize the ACME Corp merger memo for Jane Doe"
    variant = RoleVariant(model="llama3.2", params={"max_tasks": 4})
    settings = RoleRouting.model_validate({"policy": "evidence", "candidates": {
        "decomposition": [variant.model_dump()]}})
    table = {(D.key(), secret): stats(100, 80, 0.1), (variant.key(), secret): stats(40, 40, 0.01)}
    role_router, log, _ = router(tmp_path, table, settings=settings)
    roles = RoleAdapters({"decomposition": (None, D)}, role_router, Factory(), lambda r: False)
    choice = roles.choose("decomposition", domain=secret)
    assert choice.variant == variant
    roles.record(choice, "partial", score=1.0, floor_origin="model_draft")
    pattern = re.compile(r"^(h_[0-9a-f]{32}|[a-z0-9_.:-]{1,64})$")
    records = list(log.roles("decomposition"))
    assert len(records) == 2
    for record in records:
        for name, value in record.model_dump(mode="json").items():
            values = value if isinstance(value, list) else [value]
            for item in values:
                if isinstance(item, str) and name not in ("decision_id", "timestamp"):
                    assert pattern.fullmatch(item), (name, item)
        UUID(str(record.decision_id))
    journals = (tmp_path / "routes").glob("*.jsonl")
    text = "".join(path.read_text(encoding="utf-8") for path in journals)
    for word in ("ACME", "acme", "merger", "Jane", "llama3.2", "max_tasks"):
        assert word not in text


# ---------------------------------------------------------------- the judge (11.1)


def test_the_judge_is_never_routed(tmp_path: Path) -> None:
    assert "judge" not in ROLE_NAMES and "verifier" not in ROLE_NAMES
    assert JUDGE_PRESET == "verifier" and JUDGE_PRESET not in ROLE_PRESETS.values()
    for name in ("judge", "verifier"):
        with pytest.raises(ValidationError):
            RoleRouting.model_validate({"candidates": {name: [{"model": "fake"}]}})
        with pytest.raises(ProfileError, match="^invalid_profile$"):
            load_profile(write_profile(tmp_path, roles={"candidates": {name: [{"model": "fake"}]}}))
    roles, _, _, _ = seam(tmp_path, {})
    with pytest.raises(ValueError, match="unknown_role"):
        roles.choose("judge")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown_role"):
        roles.adapter("verifier")  # type: ignore[arg-type]
    role_router, _, _ = router(tmp_path / "b", {})
    with pytest.raises(ValueError, match="unknown_role"):
        role_router.choose("judge", domain=None, default=D)  # type: ignore[arg-type]


# ---------------------------------------------------------------- profile codes (11.2)


def test_route_model_unknown(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="^route_model_unknown$"):
        load_profile(write_profile(tmp_path, roles={"candidates": {
            "inference": [{"model": "not-configured"}]}}))
    # A named-only adapter model is not a ``models`` key either.
    with pytest.raises(ProfileError, match="^route_model_unknown$"):
        load_profile(write_profile(
            tmp_path, inference={"provider": "fake", "model": "named"},
            roles={"candidates": {"rewrite": [{"model": "named"}]}}))
    with pytest.raises(RoleRoutingError, match="^route_model_unknown$"):
        check_roles(routing(), {"fake": object()})
    with pytest.raises(ValidationError):
        Profile.model_validate({"models": {"fake": {"provider": "fake"}},
                                "roles": {"candidates": {"inference": [{"model": "x"}]}}})


@pytest.mark.parametrize(("role", "params"), [
    ("inference", {"temperature": 1}),
    ("floor_draft", {"max_tasks": 4}),
    ("decomposition", {"max_tasks": 5}),
    ("decomposition", {"max_tasks": "4"}),
    ("rewrite", {"max_rewrites": 2}),
    ("rewrite", {"max_rewrites": True}),
    ("rewrite", {"max_rewrites": 1.0}),
    ("forge_agent", {"mode": "hybrid"}),
    ("forge_agent", {"pi_max_turns": 150}),
    ("forge_agent", {"pi_max_turns": "100"}),
])
def test_route_param_invalid(tmp_path: Path, role: str, params: dict[str, object]) -> None:
    with pytest.raises(ProfileError, match="^route_param_invalid$"):
        load_profile(write_profile(tmp_path, roles={"candidates": {
            role: [{"model": "fake", "params": params}]}}))


@pytest.mark.parametrize(("role", "params"), [
    ("inference", {}),
    ("decomposition", {"max_tasks": 4}),
    ("decomposition", {"max_tasks": 8}),
    ("rewrite", {"max_rewrites": 1}),
    ("rewrite", {"max_rewrites": 3}),
    ("forge_agent", {"mode": "template", "pi_max_turns": 100}),
    ("forge_agent", {"mode": "agent", "pi_max_turns": 200}),
    ("floor_draft", {}),
])
def test_closed_params_accepted(tmp_path: Path, role: str, params: dict[str, object]) -> None:
    profile = load_profile(write_profile(tmp_path, roles={"policy": "evidence", "candidates": {
        role: [{"model": "fake", "params": params}]}}))
    assert profile.roles.candidates[role][0].params == params  # type: ignore[index]


# ---------------------------------------------------------------- workspace and wiring


def test_workspace_state_denies_routes_and_exports(tmp_path: Path) -> None:
    assert WORKSPACE_STATE[:6] == (
        "artifacts", "library", "registry", "telemetry", "proposals", "server")
    assert "routes" in WORKSPACE_STATE and "exports" in WORKSPACE_STATE
    work = tmp_path / "work"
    for denied in (pi_denied_paths(None, work),
                   pi_denied_paths(load_profile(write_profile(tmp_path)), work)):
        assert work.resolve() / "routes" in denied
        assert work.resolve() / "exports" in denied
    services = build_services(load_profile(write_profile(tmp_path)), work)
    try:
        denied = set(services.profile.pi.denied_paths)
        assert work.resolve() / "routes" in denied and work.resolve() / "exports" in denied
    finally:
        services.close()


def test_evidence_policy_is_wired_through_build_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    table = {
        (RoleVariant(model="fake").key(), None): stats(100, 80, 0.10),
        (RoleVariant(model="llama3.2").key(), None): stats(40, 40, 0.01),
    }
    seen: dict[str, object] = {}

    def stores(profile: Profile, workspace: Path, compiler: object, registry: object,
               store: object, providers: Mapping[str, Provider]) -> tuple[None, Any, Any]:
        seen["providers"] = dict(providers)
        return None, StubIndex(table), RouteLog(workspace / "routes")

    monkeypatch.setattr(profiles, "_routing_stores", stores)
    profile = named_profile(tmp_path, roles={"policy": "evidence", "candidates": {
        "inference": [{"model": "llama3.2"}]}})
    services = build_services(profile, tmp_path / "work")
    try:
        assert seen["providers"] == {"fake": "fake", "llama3.2": "fake"}
        assert services.roles.policy == "evidence"
        choice = services.roles.choose("inference")
        assert choice.variant == RoleVariant(model="llama3.2")
        assert choice.adapter is not services.compiler.inference_adapter
        # Pinned roles and the pinned default keep identity under ``evidence``.
        assert services.roles.adapter("decomposition") is (
            services.orchestrator.decomposition_adapter)
        records = list(RouteLog(tmp_path / "work" / "routes").roles("inference"))
        assert len(records) == 1 and isinstance(records[0], RoleRecord)
    finally:
        services.close()


def test_routing_stores_fail_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = load_profile(write_profile(tmp_path, roles={"policy": "evidence"}))

    def unavailable(*args: object, **kwargs: object) -> object:
        raise OSError("routes unavailable")

    monkeypatch.setattr(profiles, "EvidenceIndex", unavailable)
    services = build_services(profile, tmp_path / "work")
    try:
        assert services.router is None and services.roles.router is None
        assert services.roles.policy == "pinned"
        assert services.roles.adapter("decomposition") is (
            services.orchestrator.decomposition_adapter)
    finally:
        services.close()


def test_default_services_carry_the_task_router(tmp_path: Path) -> None:
    services = build_services(load_profile(write_profile(tmp_path)), tmp_path / "work")
    try:
        assert isinstance(services.router, Router)
        assert services.router.policy == services.profile.routing == RoutePolicy()
        assert services.roles.router is None  # roles stay pinned by default
        assert not (tmp_path / "work" / "routes").exists()  # nothing is read or written yet
    finally:
        services.close()
    disabled = load_profile(write_profile(tmp_path, routing={"enabled": False}))
    services = build_services(disabled, tmp_path / "work2")
    try:
        assert services.router is None
    finally:
        services.close()


def test_evidence_policy_over_the_real_index_starts_pinned(tmp_path: Path) -> None:
    """With WP1's EvidenceIndex and no outcomes yet, ``evidence`` keeps the default."""
    profile = named_profile(tmp_path, roles={"policy": "evidence", "candidates": {
        "inference": [{"model": "llama3.2"}], "decomposition": [{"model": "llama3.2"}]}})
    services = build_services(profile, tmp_path / "work")
    try:
        assert services.roles.policy == "evidence"
        choice = services.roles.choose("inference", domain="invoices")
        assert choice.codes == ("role_evidence_insufficient",)
        assert choice.adapter is services.compiler.inference_adapter
        services.roles.record(choice, "success", floor_origin="example_fixture")
        records = list(RouteLog(tmp_path / "work" / "routes").roles("inference"))
        assert [record.outcome for record in records] == [None, "success"]
        assert services.roles.adapter("decomposition") is (
            services.orchestrator.decomposition_adapter)
    finally:
        services.close()


def test_role_defaults_match_today_without_services(tmp_path: Path) -> None:
    built = build_services(named_profile(tmp_path), tmp_path / "work")
    try:
        defaults = role_defaults(built.profile, built.compiler, built.orchestrator,
                                 built.forge_adapter)
        assert set(defaults) == set(ROLE_NAMES)
        assert all(variant.params == {} for _, variant in defaults.values())
        assert defaults["inference"][0] is built.compiler.inference_adapter
    finally:
        built.close()


def test_wilson_lower_bound() -> None:
    assert wilson(0, 0, 1.0) == 0.5
    assert wilson(5, 10, 1.0) == pytest.approx(0.34924, abs=1e-5)
    assert wilson(10, 10, 1.64) < 1.0 and wilson(0, 10, 1.64) == pytest.approx(0.0, abs=1e-12)


# ---------------------------------------------------------------- review regressions


def test_empty_role_model_is_route_model_unknown(tmp_path: Path) -> None:
    """Section 11.2: ``model`` is a ``profile.models`` key; "" is simply not one."""
    with pytest.raises(ProfileError, match="^route_model_unknown$"):
        load_profile(write_profile(tmp_path, roles={"candidates": {
            "inference": [{"model": ""}]}}))
    with pytest.raises(RoleRoutingError, match="^route_model_unknown$"):
        check_roles(RoleRouting.model_validate({"candidates": {"rewrite": [{"model": ""}]}}),
                    {"fake": object()})


def evidence_services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Services:
    """Evidence policy over a stub index where llama3.2 is non-inferior and cheaper."""
    table = {
        (RoleVariant(model="fake").key(), None): stats(100, 80, 0.10),
        (RoleVariant(model="llama3.2").key(), None): stats(40, 40, 0.01),
    }

    def stores(profile: Profile, workspace: Path, compiler: object, registry: object,
               store: object, providers: Mapping[str, Provider]) -> tuple[None, Any, Any]:
        return None, StubIndex(table), RouteLog(workspace / "routes")

    monkeypatch.setattr(profiles, "_routing_stores", stores)
    profile = named_profile(tmp_path, roles={"policy": "evidence", "candidates": {
        "inference": [{"model": "llama3.2"}]}})
    return build_services(profile, tmp_path / "work")


async def test_roles_follow_the_observed_session_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section 11.3: the Compiler's live attributes are the pinned defaults, so the
    session's metered wrappers reach every role call, routed or not."""
    from architect.models import ModelRequest
    from architect.session.observe import Meter, ObservedAdapter, observe_services

    services = evidence_services(tmp_path, monkeypatch)
    try:
        meter = Meter()
        observed = observe_services(services, meter)
        compiler = observed.compiler
        assert isinstance(compiler.inference_adapter, ObservedAdapter)
        roles = observed.roles
        # Unrouted roles: identity with the live (wrapped) compiler attributes.
        assert roles.adapter("rewrite") is compiler.rewrite_adapter
        assert isinstance(roles.adapter("rewrite"), ObservedAdapter)
        # A switched variant is built by the session's metered factory.
        choice = roles.choose("inference")
        assert choice.variant == RoleVariant(model="llama3.2")
        assert isinstance(choice.adapter, ObservedAdapter)
        await choice.adapter.complete(ModelRequest(prompt="x"))
        assert meter.calls == 1
        # The pinned default D is the live wrapped attribute, not the stale original.
        compiler.inference_adapter = ObservedAdapter(FakeAdapter(model="llama3.2"), meter)
        # D is now llama3.2 itself; it stays the chosen arm and keeps identity.
        assert roles.adapter("inference") is compiler.inference_adapter
    finally:
        services.close()


def test_roles_follow_compiler_adapter_swaps(tmp_path: Path) -> None:
    """``Session._arm_forge`` swaps ``compiler.forge_adapter``; pinned roles follow."""
    services = build_services(named_profile(tmp_path), tmp_path / "work")
    try:
        compiler = services.compiler
        agent = FakeAdapter(model="fake")
        previous = compiler.forge_adapter
        compiler.forge_adapter = agent
        assert services.roles.adapter("forge_agent") is agent
        assert services.roles.choose("forge_agent").adapter is agent
        assert services.roles.defaults["forge_agent"][1] == RoleVariant(model="fake")
        compiler.forge_adapter = previous
        assert services.roles.adapter("forge_agent") is previous
        swapped = FakeAdapter(model="llama3.2")
        compiler.inference_adapter = swapped
        assert services.roles.adapter("inference") is swapped
        assert services.roles.defaults["inference"][1] == RoleVariant(model="llama3.2")
        compiler.inference_adapter = None  # Stage 0's own fallback: the live factory
        built: list[str] = []

        def factory(model: str) -> ModelAdapter:
            built.append(model)
            return FakeAdapter(model=model)

        compiler.adapter_factory = factory
        assert services.roles.adapter("inference") is None
        assert services.roles.choose("inference").adapter.model == "fake"
        assert built == ["fake"]
        manual = Services(
            compiler=compiler, orchestrator=services.orchestrator, registry=services.registry,
            runtime=services.runtime, profile=services.profile, workspace=services.workspace,
            adapter_factory=services.adapter_factory, forge_adapter=services.forge_adapter,
        )
        compiler.forge_adapter = agent
        assert manual.roles.adapter("forge_agent") is agent
        compiler.forge_adapter = previous
    finally:
        services.close()


class CountingFactory:
    """The Compiler's live ``adapter_factory`` replaced by one that records each build."""

    def __init__(self, inner: Callable[[str], ModelAdapter]) -> None:
        self.inner = inner
        self.built: list[str] = []

    def __call__(self, model: str) -> ModelAdapter:
        self.built.append(model)
        return self.inner(model)


@pytest.mark.parametrize("policy", ["pinned", "evidence"])
def test_forge_default_is_the_compilers_forge_or_rewrite_adapter(
    tmp_path: Path, policy: str
) -> None:
    """Section 11.2: D for ``forge_agent`` is exactly what the Compiler hands the Forge
    driver (``forge_adapter or rewrite_adapter``), even when ``rewrite`` names a model
    that is not a ``models`` key and no ``forge_agent`` is configured."""
    profile = load_profile(write_profile(
        tmp_path, rewrite={"provider": "fake", "model": "llama3.2"},
        roles={"policy": policy}))
    services = build_services(profile, tmp_path / "work")
    try:
        compiler = services.compiler
        assert compiler.forge_adapter is None and compiler.rewrite_adapter is not None
        today = compiler.forge_adapter or compiler.rewrite_adapter
        roles = services.roles
        assert roles.policy == policy
        assert roles.defaults["forge_agent"] == (today, RoleVariant(model="llama3.2"))
        assert roles.adapter("forge_agent") is today
        choice = roles.choose("forge_agent")
        assert choice.adapter is today and choice.variant == RoleVariant(model="llama3.2")
        # A Forge agent configured later (``Session._arm_forge``) still wins over rewrite.
        agent = FakeAdapter(model="fake")
        compiler.forge_adapter = agent
        assert roles.adapter("forge_agent") is agent
        assert roles.choose("forge_agent").adapter is agent
    finally:
        services.close()


@pytest.mark.parametrize("policy", ["pinned", "evidence"])
def test_choose_never_invents_a_default_adapter(tmp_path: Path, policy: str) -> None:
    """Where today's binding is None (no rewrite, the Forge driver's
    ``adapter_factory(artifact.model)``, floor drafting's ``spec.model_target``), the
    chosen D carries no adapter: the call site keeps its own fallback, nothing is
    built, and no outcome is credited to a model that did not run."""
    profile = load_profile(write_profile(
        tmp_path, models={"fake": {"provider": "fake"}, "llama3.2": {"provider": "fake"}},
        roles={"policy": policy}))
    services = build_services(profile, tmp_path / "work")
    try:
        compiler = services.compiler
        factory = CountingFactory(compiler.adapter_factory)
        compiler.adapter_factory = factory
        roles = services.roles
        assert roles.policy == policy
        for role in ("rewrite", "forge_agent", "floor_draft"):
            assert roles.adapter(role) is None
            choice = roles.choose(role)
            assert choice.adapter is None, role
            assert choice.variant == roles.defaults[role][1]
            expected = ("role_evidence_insufficient",) if policy == "evidence" else ()
            assert choice.codes == expected
            roles.record(choice, "success")
        assert factory.built == []
        records = list(RouteLog(tmp_path / "work" / "routes").roles("rewrite"))
        assert all(record.outcome is None for record in records)
        # Stage 0's fallback is reproducible: ``adapter_factory("fake")`` (line 183).
        assert roles.adapter("inference") is None
        assert roles.choose("inference").adapter.model == "fake"  # type: ignore[union-attr]
        assert factory.built == ["fake"]
    finally:
        services.close()


def test_evidence_adapter_builds_only_a_switched_variant(tmp_path: Path) -> None:
    """``adapter()`` under ``evidence`` never builds (or fails building) an adapter it
    then discards: D keeps today's None, and only a switched variant is built."""
    insufficient = {(D.key(), None): stats(100, 80, 0.10)}
    role_router, _, _ = router(tmp_path, insufficient)
    factory = Factory()
    defaults: dict[RoleName, tuple[ModelAdapter | None, RoleVariant]] = {
        role: (None, D) for role in ROLE_NAMES
    }
    roles = RoleAdapters(defaults, role_router, factory, lambda role: False)
    for role in ("rewrite", "forge_agent", "floor_draft", "decomposition"):
        assert roles.adapter(role) is None
        assert roles.choose(role).adapter is None
    assert factory.built == []

    def refuse(model: str) -> ModelAdapter:
        raise ProfileError("model_not_configured")

    refusing = RoleAdapters(defaults, role_router, refuse, lambda role: False)
    assert refusing.adapter("forge_agent") is None
    assert refusing.choose("forge_agent").adapter is None
    switched = {**insufficient, (CHEAP.key(), None): stats(40, 40, 0.01)}
    role_router, _, _ = router(tmp_path / "b", switched)
    roles = RoleAdapters(defaults, role_router, factory, lambda role: False)
    adapter = roles.adapter("rewrite")
    assert adapter is not None and adapter.model == "llama3.2"
    assert factory.built == ["llama3.2"]


# ---------------------------------------------------------------- opaque pinned defaults


def opaque_seam(
    tmp_path: Path,
    table: Mapping[tuple[str, str | None], Stats],
    candidates: tuple[RoleVariant, ...],
    **updates: Any,
) -> tuple[RoleAdapters, Factory, RouteLog]:
    """Every role's binding is None with D = ``fake``: for all but ``inference`` the call
    site's own fallback runs (no rewrite, the Forge driver's factory, ``model_target``)."""
    settings = routing(candidates={role: candidates for role in ROLE_NAMES}, **updates)
    role_router, log, _ = router(tmp_path, table, settings=settings)
    factory = Factory()
    defaults: dict[RoleName, tuple[ModelAdapter | None, RoleVariant]] = {
        role: (None, D) for role in ROLE_NAMES
    }
    return RoleAdapters(defaults, role_router, factory, lambda role: False), factory, log


def test_exploration_never_targets_a_default_without_an_adapter(tmp_path: Path) -> None:
    """Section 11.4 step 6: D without a reproducible adapter can never gain samples, so
    it is no exploration target; every k-th call goes to the least-sampled candidate."""
    roles, factory, log = opaque_seam(tmp_path, {}, (CHEAP, OTHER), explore_every=1)
    for role in ("rewrite", "forge_agent", "floor_draft", "decomposition"):
        picks = [roles.choose(role) for _ in range(4)]
        assert [pick.variant for pick in picks] == [CHEAP] * 4, role
        for pick in picks:
            assert pick.explored and pick.adapter is not None, role
            assert pick.adapter.model == "llama3.2"
            assert pick.codes == ("role_evidence_insufficient", "role_explore")
    assert factory.built == ["llama3.2"] * 16
    # With samples on CHEAP, the least-sampled is OTHER, never the opaque D.
    sampled = {(CHEAP.key(), None): stats(3, 3)}
    roles, factory, _ = opaque_seam(tmp_path / "b", sampled, (CHEAP, OTHER), explore_every=1)
    choice = roles.choose("rewrite")
    assert choice.variant == OTHER and choice.explored and factory.built == ["llama3.2-vision"]
    # Between exploring calls the opaque D is still chosen, with today's None adapter.
    roles, factory, log = opaque_seam(tmp_path / "c", {}, (CHEAP,), explore_every=2)
    picks = [roles.choose("rewrite") for _ in range(4)]
    assert [pick.explored for pick in picks] == [False, True, False, True]
    assert [pick.adapter is None for pick in picks] == [True, False, True, False]
    assert [pick.variant for pick in picks] == [D, CHEAP, D, CHEAP]
    assert all("role_explore" not in pick.codes for pick in picks if pick.adapter is None)
    # An explored candidate's outcome is credited; the opaque D's never is.
    for pick in picks:
        roles.record(pick, "success")
    outcomes = [record for record in log.roles("rewrite") if record.outcome is not None]
    assert [record.decision_id for record in outcomes] == [
        picks[1].decision_id,
        picks[3].decision_id,
    ]
    assert all(record.explored for record in outcomes)
    # Without candidates there is nothing to explore: D keeps today's fallback.
    bare, factory, _ = opaque_seam(tmp_path / "d", {}, (), explore_every=1)
    choice = bare.choose("rewrite")
    assert choice.adapter is None and not choice.explored
    assert choice.codes == ("role_evidence_insufficient",) and factory.built == []
    # Stage 0's D is reproducible (``adapter_factory("fake")``), so it is still explorable.
    stage0, _, _ = opaque_seam(
        tmp_path / "e", {(CHEAP.key(), None): stats(3, 3)}, (CHEAP,), explore_every=1
    )
    choice = stage0.choose("inference")
    assert choice.variant == D and choice.explored
    assert choice.adapter is not None and choice.adapter.model == "fake"


def test_declared_candidate_equal_to_an_opaque_default_is_reachable(tmp_path: Path) -> None:
    """A declared ``{model: <first model>}`` is a real variant, not the opaque D that
    shares its placeholder model: it is built, run and credited, and D's own fallback
    never borrows its evidence."""
    same = RoleVariant(model="fake")
    roles, factory, log = opaque_seam(tmp_path, {}, (same,), explore_every=1)
    for _ in range(3):
        choice = roles.choose("rewrite")
        assert choice.variant == same and choice.explored
        assert choice.adapter is not None and choice.adapter.model == "fake"
        assert choice.codes == ("role_evidence_insufficient", "role_explore")
        roles.record(choice, "success")
    assert factory.built == ["fake"] * 3
    outcomes = [record for record in log.roles("rewrite") if record.outcome == "success"]
    assert len(outcomes) == 3
    assert roles.adapter("rewrite") is not None  # adapter() builds the candidate too
    # On evidence (no exploration), the candidate is eligible on its own outcomes and is
    # chosen over the opaque D, which is judged at one half with unknown cost.
    table = {(same.key(), None): stats(40, 40, cost=0.01)}
    roles, factory, _ = opaque_seam(tmp_path / "b", table, (same,))
    choice = roles.choose("rewrite")
    assert choice.variant == same and not choice.explored and choice.codes == ()
    assert choice.adapter is not None and factory.built == ["fake"]
    # An inferior candidate is not chosen: the opaque D keeps today's None.
    table = {(same.key(), None): stats(40, 5, cost=0.01)}
    roles, factory, _ = opaque_seam(tmp_path / "c", table, (same,))
    choice = roles.choose("rewrite")
    assert choice.adapter is None and choice.codes == () and factory.built == []
    # A reproducible D (Stage 0) and an equal candidate are still one arm.
    roles, factory, _ = opaque_seam(tmp_path / "d", {}, (same,), explore_every=1)
    choice = roles.choose("inference")
    assert choice.variant == same and choice.explored and factory.built == ["fake"]


def test_exploration_reaches_candidates_of_adapterless_roles_through_build_services(
    tmp_path: Path,
) -> None:
    """The review probe over WP1's real index and route log: with ``rewrite`` unset and
    no Forge agent, exploration reaches the declared candidates, their outcomes are
    credited, and they reach ``min_samples`` from their own log."""
    models = {"fake": {"provider": "fake"}, "llama3.2": {"provider": "fake"}}
    (tmp_path / "a").mkdir()
    profile = load_profile(
        write_profile(
            tmp_path / "a",
            models=models,
            roles={
                "policy": "evidence",
                "explore_every": 1,
                "min_samples": 5,
                "candidates": {
                    "rewrite": [{"model": "llama3.2"}],
                    "forge_agent": [{"model": "llama3.2"}],
                },
            },
        )
    )
    services = build_services(profile, tmp_path / "work")
    try:
        roles = services.roles
        assert roles.policy == "evidence" and roles.defaults["rewrite"][0] is None
        assert roles.defaults["forge_agent"][0] is None
        assert roles.router is not None
        for role in ("rewrite", "forge_agent"):
            chosen = []
            for _ in range(6):
                choice = roles.choose(role)
                assert choice.explored and choice.adapter is not None, role
                chosen.append(choice.adapter.model)
                roles.record(choice, "success")
            assert chosen == ["llama3.2"] * 6, role
            candidate = RoleVariant(model="llama3.2")
            assert roles.router.index.role_stats(role, candidate, None).n >= 5
    finally:
        services.close()
    # The declared ``{model: fake}`` (the placeholder model of D) is reachable too.
    (tmp_path / "b").mkdir()
    profile = load_profile(
        write_profile(
            tmp_path / "b",
            models=models,
            roles={
                "policy": "evidence",
                "explore_every": 1,
                "candidates": {"rewrite": [{"model": "fake"}]},
            },
        )
    )
    services = build_services(profile, tmp_path / "work2")
    try:
        for _ in range(3):
            choice = services.roles.choose("rewrite")
            assert choice.adapter is not None and choice.adapter.model == "fake"
            assert choice.explored and choice.variant == RoleVariant(model="fake")
    finally:
        services.close()
