"""Phase 10 WP1: signatures, fit tiers, hard filters, ranking, the decision rule,
exploration, proposals, explanations and content-free records (pure, $0, no I/O).

``route()`` runs over an in-memory evidence fake; the store-backed index is covered
in ``test_routing_stores.py``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from uuid import UUID, uuid4

import pytest
from routing_support import FakeIndex, block_network, installed_ev, key, make_spec, template_ev

from architect.routing import (
    OutcomeRecord,
    RatingRecord,
    RoleRecord,
    RoutePolicy,
    TaskProbe,
    fit,
    render_decision,
    route,
    route_record,
    schema_skeleton,
    signature,
    wilson,
)
from architect.routing.labels import PROVIDER_EVIDENCE, ROUTE_CODES, evidence_class
from architect.spec import Budget, FloorKind, Lifecycle, Spec, schema_sha256


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    block_network(monkeypatch)


def probe(
    spec: Spec | None = None,
    *,
    origin: str = "cli",
    grant: bool = False,
    ladder: tuple[str, ...] = ("m1",),
    cap: Budget | None = None,
) -> TaskProbe:
    spec = spec or make_spec(model="m1")
    return TaskProbe.model_validate(
        {
            "origin": origin,
            "spec": spec,
            "budget_cap": cap or spec.budget,
            "ladder": ladder,
            "grant_reuse": grant,
        }
    )


POLICY = RoutePolicy()


# ------------------------------------------------------------------ signature


def _h(value: object) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def test_signature_golden_hex_and_formula() -> None:
    spec = make_spec()
    sig = signature(spec)
    assert (sig.slot, sig.shape, sig.family, sig.token_count) == (
        "fb03ff02819cff0b6da68c321874b8d5",
        "1454accf0de9e5346a86d40fa40805a0",
        "9cd8d5c7c3ada3cb4de38c89cea28230",
        5,
    )
    # The same values from the Section 5 formulas, written out independently.
    assert sig.slot == _h(
        {
            "task": "classify the support ticket priority",
            "input_schema": spec.input_schema,
            "output_schema": spec.output_schema,
            "domain": "general",
            "tools": [],
            "floor": spec.floor.model_dump(mode="json"),
            "secondary_floor": None,
        }
    )
    assert sig.shape == _h(
        {
            "domain": "general",
            "in": schema_sha256(spec.input_schema),
            "out": schema_sha256(spec.output_schema),
            "tools": [],
            "pipeline": [],
        }
    )
    assert sig.family == _h(
        {"domain": "general", "in": spec.input_schema, "out": spec.output_schema}
    )


def test_signature_normalizes_nfkc_and_casefold() -> None:
    plain = signature(make_spec("Classify the Strasse ticket"))
    # Full-width letters (NFKC), surrounding space (strip) and case (casefold).
    fancy = signature(make_spec("  ＣLASSIFY the STRASSE ticket "))
    assert plain.slot == fancy.slot and plain.tokens == fancy.tokens
    assert signature(make_spec("Classify the ticket!")).slot != plain.slot


def test_excluded_fields_do_not_change_slot() -> None:
    base = make_spec()
    sig = signature(base)
    for update in (
        {"model_target": "other"},
        {"budget": Budget(cost_usd=5, compute_s=500)},
        {"lifecycle": Lifecycle.EXPIRE},
        {"preset_hint": "extractor"},
        {"execution_config": {"retries": 2}},
        {"provenance": {"task": "user"}},
    ):
        assert signature(base.model_copy(update=update)).slot == sig.slot, update
    assert signature(base.model_copy(update={"tools": ("b", "a", "a")})).slot == signature(
        base.model_copy(update={"tools": ("a", "b")})
    ).slot
    # The _beats fields do change it.
    assert signature(base.model_copy(update={"domain": "legal"})).slot != sig.slot
    assert signature(base.model_copy(update={"task": "Other task"})).slot != sig.slot
    floor = base.floor.model_copy(update={"limit": 0.6})
    assert signature(base.model_copy(update={"floor": floor})).slot != sig.slot


def test_tokens_are_never_dumped() -> None:
    sig = signature(make_spec())
    assert sig.tokens == {"classify", "the", "support", "ticket", "priority"}
    assert "tokens" not in sig.model_dump()
    assert "tokens" not in json.loads(sig.model_dump_json())
    assert "ticket" not in sig.model_dump_json() and "ticket" not in repr(sig)


def test_schema_skeleton_keeps_only_structure() -> None:
    schema = {
        "type": "object",
        "title": "Ticket",
        "description": "free text",
        "properties": {"a": {"type": "string", "maxLength": 4}, "b": {"type": "array",
                       "items": {"type": "integer", "minimum": 0}}},
        "required": ["a"],
        "additionalProperties": False,
    }
    assert schema_skeleton(schema) == {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "array",
                       "items": {"type": "integer"}}},
        "required": ["a"],
    }
    # Family ignores constraints and descriptions; shape does not.
    loose = make_spec(output={"type": "string"})
    strict = make_spec(output={"type": "string", "maxLength": 9})
    assert signature(loose).family == signature(strict).family
    assert signature(loose).shape != signature(strict).shape


# ------------------------------------------------------------------ fit tiers


def test_fit_tiers_and_formulas_at_boundaries() -> None:
    base = make_spec("alpha beta")
    sig = signature(base)
    assert fit(sig, base, sig, base) == ("A", 1.0)
    # Tier B: same shape, different slot. J = 1 (same tokens), same floor kind.
    other = base.model_copy(update={"floor": base.floor.model_copy(update={"limit": 0.7})})
    tier, value = fit(sig, base, signature(other), other)
    assert tier == "B" and value == pytest.approx(0.5 + 0.4 * 1 + 0.1)
    # J = 0, floor kind differs: the Tier B minimum 0.5.
    edit = make_spec("gamma delta", kind=FloorKind.HUMAN_EDIT_RATE)
    tier, value = fit(sig, base, signature(edit), edit)
    assert tier == "B" and value == pytest.approx(0.5)
    # J = 1/3 with the same kind.
    half = make_spec("alpha gamma")
    tier, value = fit(sig, base, signature(half), half)
    assert tier == "B" and value == pytest.approx(0.5 + 0.4 / 3 + 0.1)
    # Tier C: same family, different shape. J = 0 -> 0.2, J = 1 -> 0.4.
    family = make_spec("gamma", output={"type": "string", "maxLength": 3})
    assert fit(sig, base, signature(family), family) == ("C", pytest.approx(0.2))
    family_same = make_spec("alpha beta", output={"type": "string", "maxLength": 3})
    assert fit(sig, base, signature(family_same), family_same) == ("C", pytest.approx(0.4))
    foreign = make_spec("alpha beta", output={"type": "integer"})
    assert fit(sig, base, signature(foreign), foreign) == ("none", 0.0)


def test_empty_token_sets_have_jaccard_one() -> None:
    from architect.routing.signature import jaccard

    assert jaccard(frozenset(), frozenset()) == 1.0
    assert jaccard(frozenset({"a"}), frozenset()) == 0.0


def test_wilson_bounds() -> None:
    assert wilson(0, 0, 1) == 0.5
    assert wilson(0, 1, 1) == pytest.approx(0.0)
    assert wilson(10, 10, 1) == pytest.approx(10 / 11)
    p, n, z = 0.7, 20, 1.64
    expected = (p + z * z / (2 * n) - z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / (
        1 + z * z / n
    )
    assert wilson(14, 20, 1.64) == pytest.approx(expected)


def test_labels_map_providers_to_classes() -> None:
    assert evidence_class("fake") == "synthetic_fake"
    assert evidence_class("ollama") == "local_zero_cost"
    assert evidence_class("anthropic") == evidence_class("openai") == "live_billed"
    assert evidence_class("claude-code") == evidence_class("codex") == "live_api_equivalent"
    assert set(PROVIDER_EVIDENCE) == {"fake", "ollama", "anthropic", "openai", "claude-code",
                                      "codex"}


# --------------------------------------------------------------- hard filters


def _only(decision_codes: tuple[str, ...]) -> str:
    assert len(decision_codes) == 1, decision_codes
    return decision_codes[0]


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"licensed": False}, "route_unlicensed"),
        ({"consumed": True}, "route_consumed"),
        ({"model": "m9"}, "route_model_unconfigured"),
        ({"model": "m2"}, "route_model_unconfigured"),  # configured, but not the target
        ({"report_cost": 0.5}, "route_budget"),
        ({"compute": 99.0}, "route_budget"),
        ({"drift": "drifting"}, "route_drift"),
        ({"cls": "synthetic_fake"}, "route_synthetic_excluded"),
        # Filters apply in order: the first failure is the code.
        ({"licensed": False, "consumed": True, "drift": "drifting"}, "route_unlicensed"),
        ({"consumed": True, "report_cost": 0.5}, "route_consumed"),
    ],
)
def test_installed_hard_filters(kwargs: dict[str, object], code: str) -> None:
    spec = make_spec(model="m1")
    index = FakeIndex(
        (installed_ev(spec, **kwargs),),  # type: ignore[arg-type]
        classes={"m1": "live_billed", "m2": "live_billed", "fake": "synthetic_fake"},
    )
    decision = route(probe(spec, grant=True), index=index, policy=POLICY)
    (view,) = decision.candidates
    assert not view.eligible and _only(view.codes) == code
    assert decision.action == "compile" and decision.target is None


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"evicted": True, "licensed": None}, "route_evicted"),
        ({"licensed": False}, "route_unlicensed"),
        ({"cls": "synthetic_fake"}, "route_synthetic_excluded"),
    ],
)
def test_template_hard_filters(kwargs: dict[str, object], code: str) -> None:
    spec = make_spec(model="m1")
    sibling = make_spec("Rank the support ticket urgency", model="m1")
    index = FakeIndex(templates=(template_ev(sibling, key=key("t"), **kwargs),))  # type: ignore[arg-type]
    decision = route(probe(spec), index=index, policy=POLICY)
    (view,) = decision.candidates
    assert view.tier == "B" and not view.eligible and _only(view.codes) == code
    assert decision.action == "compile"


def test_synthetic_evidence_ranks_only_fake_ladders() -> None:
    spec = make_spec(model="fake")
    index = FakeIndex((installed_ev(spec, model="fake", cls="synthetic_fake"),))
    decision = route(probe(spec, grant=True, ladder=("fake",)), index=index, policy=POLICY)
    assert decision.action == "reuse_installed"
    assert "evidence_synthetic_only" in decision.codes


def test_cheapest_ladder_accepts_any_configured_model() -> None:
    spec = make_spec(model="cheapest that passes")
    index = FakeIndex(
        (installed_ev(spec, model="m2"),),
        classes={"m1": "live_billed", "m2": "live_billed"},
    )
    decision = route(probe(spec, grant=True, ladder=("m1",)), index=index, policy=POLICY)
    assert decision.action == "reuse_installed"


# -------------------------------------------------------------------- ranking


def test_tier_a_ranking_key() -> None:
    spec = make_spec(model="m1")
    rows = (
        installed_ev(spec, name="low", m=0.1),
        installed_ev(spec, name="sec", m=0.3, sec_lcb=0.2),
        installed_ev(spec, name="human", m=0.3, sec_lcb=0.2, h=0.9),
        installed_ev(spec, name="cheap", m=0.3, sec_lcb=0.2, h=0.9, cost=0.001),
        installed_ev(spec, name="fast", m=0.3, sec_lcb=0.2, h=0.9, cost=0.001, latency=0.1),
        installed_ev(spec, name="lean", m=0.3, sec_lcb=0.2, h=0.9, cost=0.001, latency=0.1,
                     k=1),
    )
    decision = route(probe(spec, grant=True), index=FakeIndex(rows), policy=POLICY)
    names = [view.ref.name for view in decision.candidates]  # type: ignore[union-attr]
    assert names == ["lean", "fast", "cheap", "human", "sec", "low"]
    assert decision.chosen_rank == 0 and decision.target.name == "lean"  # type: ignore[union-attr]


def test_tier_a_name_tiebreak_is_categorical() -> None:
    from architect.telemetry import categorical

    spec = make_spec(model="m1")
    rows = (installed_ev(spec, name="zeta"), installed_ev(spec, name="alpha"))
    decision = route(probe(spec, grant=True), index=FakeIndex(rows), policy=POLICY)
    expected = sorted(("zeta", "alpha"), key=categorical)
    assert [v.ref.name for v in decision.candidates] == expected  # type: ignore[union-attr]


def test_tier_b_ranking_key_and_sequence_tiebreak() -> None:
    spec = make_spec("alpha beta", model="m1")
    near = make_spec("alpha beta gamma", model="m1")
    far = make_spec("delta", model="m1")
    rows = (
        template_ev(far, key=key("far"), sequence=1),
        template_ev(near, key=key("second"), sequence=3),
        template_ev(near, key=key("first"), sequence=2),
        template_ev(near, key=key("reliable"), s=3, sequence=9),
        template_ev(near, key=key("promoted"), s=3, sequence=10, promoted=True),
        template_ev(near, key=key("margin"), s=3, sequence=11, promoted=True, m=0.5),
        template_ev(near, key=key("cheap"), s=3, sequence=12, promoted=True, m=0.5,
                    cost=0.001),
        template_ev(near, key=key("lean"), s=3, sequence=13, promoted=True, m=0.5,
                    cost=0.001, k=1),
    )
    decision = route(probe(spec), index=FakeIndex(templates=rows), policy=POLICY)
    keys = [view.ref.key for view in decision.candidates]  # type: ignore[union-attr]
    assert keys == [key(n) for n in ("lean", "cheap", "margin", "promoted", "reliable", "first",
                                     "second", "far")]
    assert decision.action == "warm_start" and decision.target.key == key("lean")  # type: ignore[union-attr]


# --------------------------------------------------------------- decision rule


def _matrix_index(spec: Spec, *, tier_a: bool, reliable: bool | None) -> FakeIndex:
    sibling = make_spec("Rank the support ticket urgency", model="m1")
    installed = (installed_ev(spec, name="exact"),) if tier_a else ()
    templates = ()
    if reliable is not None:
        templates = (template_ev(sibling, key=key("t"), s=0 if not reliable else 2,
                                 f=0 if reliable else 1),)
    return FakeIndex(installed, templates)


@pytest.mark.parametrize(
    ("tier_a", "grant", "reliable", "action", "codes"),
    [
        (True, True, True, "reuse_installed", ("fit_slot", "route_reuse_installed")),
        (True, True, None, "reuse_installed", ("fit_slot", "route_reuse_installed")),
        (True, False, True, "warm_start",
         ("fit_slot", "route_reuse_not_granted", "route_warm_start")),
        (True, False, False, "compile",
         ("fit_slot", "route_reuse_not_granted", "route_compile")),
        (True, False, None, "compile",
         ("fit_slot", "route_reuse_not_granted", "route_compile")),
        (False, True, True, "warm_start", ("fit_shape", "route_warm_start")),
        (False, False, False, "compile", ("fit_shape", "route_compile")),
        (False, True, None, "compile", ("fit_none", "route_compile")),
    ],
)
def test_decision_rule_matrix(
    tier_a: bool, grant: bool, reliable: bool | None, action: str, codes: tuple[str, ...]
) -> None:
    spec = make_spec(model="m1")
    index = _matrix_index(spec, tier_a=tier_a, reliable=reliable)
    decision = route(probe(spec, grant=grant), index=index, policy=POLICY)
    assert decision.action == action
    assert decision.codes == codes
    if tier_a and not grant:
        advice = [view for view in decision.candidates if view.advice]
        assert len(advice) == 1 and advice[0].tier == "A"
        assert "route_reuse_not_granted" in advice[0].codes
    if action == "compile":
        assert decision.target is None and decision.chosen_rank is None
    else:
        assert decision.candidates[decision.chosen_rank or 0].ref == decision.target


def test_profile_reuse_origins_grant_but_http_never_reuses() -> None:
    spec = make_spec(model="m1")
    index = FakeIndex((installed_ev(spec),))
    standing = RoutePolicy(reuse_origins=frozenset({"orchestrator", "http"}))
    assert route(probe(spec, origin="orchestrator"), index=index,
                 policy=standing).action == "reuse_installed"
    assert route(probe(spec, origin="cli"), index=index, policy=standing).action == "compile"
    http = route(probe(spec, origin="http", grant=True), index=index, policy=standing)
    assert http.action == "compile" and "route_reuse_not_granted" in http.codes


def test_refuse_when_no_ladder_model_is_configured() -> None:
    spec = make_spec(model="m1")
    decision = route(probe(spec, ladder=("m9",)), index=FakeIndex(), policy=POLICY)
    assert decision.action == "refuse" and decision.codes == ("route_no_candidate",)


def test_disabled_policy_compiles() -> None:
    spec = make_spec(model="m1")
    decision = route(probe(spec, grant=True), index=FakeIndex((installed_ev(spec),)),
                     policy=RoutePolicy(enabled=False))
    assert decision.action == "compile" and decision.candidates == ()


def test_evidence_insufficient_and_forge_suggestions() -> None:
    spec = make_spec(model="m1")
    thin = FakeIndex((installed_ev(spec, n=4),))
    assert "evidence_insufficient" in route(probe(spec), index=thin, policy=POLICY).codes
    enough = FakeIndex((installed_ev(spec, n=5),))
    assert "evidence_insufficient" not in route(probe(spec), index=enough, policy=POLICY).codes
    # (a) an open unlicensed outcome for this slot.
    slot = FakeIndex((installed_ev(spec),), history=(True, 1, 0))
    assert "route_forge_suggested" in route(probe(spec), index=slot, policy=POLICY).codes
    # (b) nothing reusable and >= 3 unlicensed, 0 licensed outcomes for the shape.
    for history, expected in (((False, 3, 0), True), ((False, 2, 0), False),
                              ((False, 5, 1), False)):
        decision = route(probe(spec), index=FakeIndex(history=history), policy=POLICY)
        assert ("route_forge_suggested" in decision.codes) is expected
    # (b) never fires while an eligible Tier A or B candidate exists.
    busy = FakeIndex((installed_ev(spec),), history=(False, 9, 0))
    assert "route_forge_suggested" not in route(probe(spec), index=busy, policy=POLICY).codes


def test_candidates_are_bounded_and_keep_the_chosen_one() -> None:
    spec = make_spec(model="m1")
    sibling = make_spec("Rank the support ticket urgency", model="m1")
    installed = tuple(installed_ev(spec, name=f"n{i}") for i in range(6))
    templates = (template_ev(sibling, key=key("warm")),)
    policy = RoutePolicy(max_candidates=3)
    decision = route(probe(spec), index=FakeIndex(installed, templates), policy=policy)
    assert len(decision.candidates) == 3 and decision.action == "warm_start"
    assert decision.candidates[decision.chosen_rank or 0].ref.key == key("warm")  # type: ignore[union-attr]
    assert any(view.advice for view in decision.candidates)


def test_one_candidate_slot_keeps_the_chosen_row_over_the_advice_row() -> None:
    spec = make_spec(model="m1")
    other = make_spec("Some other ticket words", model="m1")
    index = FakeIndex((installed_ev(spec),), (template_ev(other, key=key("t1"), s=3),))
    decision = route(probe(spec), index=index, policy=RoutePolicy(max_candidates=1))
    assert decision.action == "warm_start" and "route_reuse_not_granted" in decision.codes
    assert decision.chosen_rank == 0 and len(decision.candidates) == 1
    (view,) = decision.candidates
    assert view.ref.key == key("t1") and not view.advice  # type: ignore[union-attr]
    record = route_record(decision)
    assert record.chosen_rank == 0 and record.model is not None and record.fit > 0.0
    assert record.evidence == view.evidence


# ---------------------------------------------------------------- exploration


def _explore_index() -> tuple[Spec, FakeIndex]:
    spec = make_spec(model="m1", lifecycle=Lifecycle.EXPIRE)
    sibling = make_spec("Rank the support ticket urgency", model="m1")
    rows = (
        template_ev(sibling, key=key("top"), s=5, sequence=1),
        template_ev(sibling, key=key("fresh"), s=0, sequence=2),
        template_ev(sibling, key=key("tried"), s=1, sequence=3),
    )
    return spec, FakeIndex(templates=rows)


def test_exploration_is_deterministic_by_counter() -> None:
    spec, index = _explore_index()
    policy = RoutePolicy(explore_every=3)
    chosen = []
    for counter in range(6):
        decision = route(probe(spec), index=index, policy=policy, counter=counter)
        chosen.append((decision.target.key, decision.explored))  # type: ignore[union-attr]
    assert chosen == [
        (key("top"), False), (key("top"), False), (key("fresh"), True),
        (key("top"), False), (key("top"), False), (key("fresh"), True),
    ]
    explored = route(probe(spec), index=index, policy=policy, counter=2)
    assert "route_explore" in explored.codes


def test_exploration_daily_cap() -> None:
    spec, index = _explore_index()
    policy = RoutePolicy(explore_every=1, explore_usd_per_day=0.5)
    capped = route(probe(spec), index=index, policy=policy, explore_spent_usd=0.5)
    assert not capped.explored and capped.target.key == key("top")  # type: ignore[union-attr]
    assert "route_explore_cap" in capped.codes and "route_explore" not in capped.codes
    open_ = route(probe(spec), index=index, policy=policy, explore_spent_usd=0.49)
    assert open_.explored


@pytest.mark.parametrize("origin", ["http", "forge_integrate"])
def test_exploration_never_on_http_or_integrate(origin: str) -> None:
    spec, index = _explore_index()
    decision = route(probe(spec, origin=origin), index=index,
                     policy=RoutePolicy(explore_every=1))
    assert not decision.explored and "route_explore" not in decision.codes


def test_exploration_never_for_install_lifecycle_or_off_by_default() -> None:
    spec, index = _explore_index()
    install = spec.model_copy(update={"lifecycle": Lifecycle.INSTALL})
    assert not route(probe(install), index=index, policy=RoutePolicy(explore_every=1)).explored
    assert all(
        not route(probe(spec), index=index, policy=POLICY, counter=c).explored for c in range(5)
    )


# ------------------------------------------------------------------ proposals


def test_promote_fires_only_when_lower_bound_beats_upper_bound() -> None:
    spec = make_spec(model="m1")
    incumbent = installed_ev(spec, name="inv", m=0.1, n=100)  # upper bound 0.1 + 0.05 = 0.15
    # Rival m_lcb = m - 0.5/sqrt(100) = m - 0.05.
    for rival_m, margin, fires in ((0.21, 0.0, True), (0.20, 0.0, False),
                                   (0.30, 0.05, True), (0.25, 0.05, False)):
        rival = template_ev(spec, key=key(f"r{rival_m}"), m=rival_m, n=100)
        decision = route(probe(spec), index=FakeIndex((incumbent,), (rival,)),
                         policy=RoutePolicy(promote_margin=margin))
        assert ("route_promote_proposed" in decision.codes) is fires, (rival_m, margin)
        if fires:
            (proposal,) = decision.proposals
            assert proposal.kind == "promote" and proposal.name == "inv"
            assert proposal.command == "architect recompile inv --profile <profile>"


def test_promote_ignores_the_incumbents_own_snapshot_and_other_slots() -> None:
    spec = make_spec(model="m1")
    incumbent = installed_ev(spec, name="inv", m=0.0, n=100, digest=key("same"))
    snapshot = template_ev(spec, key=key("same"), m=0.9, n=100)
    sibling = template_ev(make_spec("Rank the ticket", model="m1"), key=key("sib"), m=0.9)
    decision = route(probe(spec), index=FakeIndex((incumbent,), (snapshot, sibling)),
                     policy=POLICY)
    assert decision.proposals == ()


def test_rollback_fires_only_for_drifting_entries_with_better_earlier_revisions() -> None:
    spec = make_spec(model="m1")
    current = installed_ev(spec, name="inv", revision=3, m=0.1, drift="drifting")
    better = installed_ev(spec, name="inv", revision=2, m=0.2)
    worse = installed_ev(spec, name="inv", revision=1, m=0.05)
    index = FakeIndex((current,), earlier={"inv": (worse, better)})
    decision = route(probe(spec, grant=True), index=index, policy=POLICY)
    assert decision.action == "compile"  # drifting: ineligible as an installed reuse
    (proposal,) = decision.proposals
    assert (proposal.kind, proposal.revision, proposal.command) == ("rollback", 2, "rollback inv 2")
    assert "route_rollback_suggested" in decision.codes
    stable = installed_ev(spec, name="inv", revision=3, m=0.1, drift="stable")
    quiet = route(probe(spec), index=FakeIndex((stable,), earlier={"inv": (better,)}),
                  policy=POLICY)
    assert quiet.proposals == ()
    no_better = route(probe(spec), index=FakeIndex((current,), earlier={"inv": (worse,)}),
                      policy=POLICY)
    assert no_better.proposals == ()


# --------------------------------------------------------- explanation, records


def test_render_is_ascii_fixed_width_and_at_most_100_columns() -> None:
    spec = make_spec(model="m1")
    sibling = make_spec("Rank the support ticket urgency", model="m1")
    index = FakeIndex(
        (installed_ev(spec, name="invoices-with-a-very-long-registry-nameé"),),
        (template_ev(sibling, key=key("warm"), s=2),),
    )
    text = render_decision(route(probe(spec), index=index, policy=POLICY))
    lines = text.splitlines()
    assert lines[0].startswith("tier candidate        model        F    m_lcb  r    $cost    "
                               "lat_s   k  evidence       codes")
    assert all(len(line) <= 100 and line.isascii() for line in lines)
    assert lines[1].startswith("A    invoices-with-a-")
    assert " 1.00 " in lines[1] and "1.0b" in lines[1] and "stage4,live_b" in lines[1]
    assert lines[2].startswith("B    " + key("warm")[:12])
    summary = [line for line in lines if line.startswith("route: ")]
    assert summary[0].startswith("route: warm_start -> " + key("warm")[:12] + " (fit_slot,")
    # The summary wraps between whole codes (never cut inside one) and ends the table.
    tail = "".join(line.strip() for line in lines[lines.index(summary[0]):])
    decision = route(probe(spec), index=index, policy=POLICY)
    assert tail.endswith("(" + ",".join(decision.codes) + ")")


_SAFE = re.compile(r"^(h_[0-9a-f]{32}|[a-z0-9_.:-]{1,64})$")


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for part in value.values() for item in _strings(part)]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [item for part in value for item in _strings(part)]
    return []


def test_records_are_content_free() -> None:
    secret = "Summarize Ms Quux invoice for ACME-7781 privately"
    spec = make_spec(secret, model="m1")
    index = FakeIndex((installed_ev(spec, name="acme-private-name"),))
    decision = route(probe(spec, grant=True), index=index, policy=POLICY)
    records = [
        route_record(decision),
        OutcomeRecord(decision_id=decision.decision_id, timestamp=route_record(decision).timestamp,
                      outcome="licensed", stage4_passed=True, evidence_class="live_billed",
                      compile_cost_usd=0.2),
        RoleRecord(decision_id=uuid4(), timestamp=route_record(decision).timestamp,
                   role="inference", model="h_" + "0" * 32, variant="h_" + "1" * 32,
                   domain="general", evidence_class="live_billed", floor_origin="model_draft",
                   chosen_rank=0, n=3.0, p_lcb=0.4, explored=False, outcome="success",
                   score=None, codes=("role_evidence_insufficient",)),
        RatingRecord(run_id=uuid4(), timestamp=route_record(decision).timestamp,
                     target_digest="h_" + "2" * 32, rating="good"),
    ]
    for record in records:
        dumped = record.model_dump()
        # ``tier`` is the contract's closed upper-case literal (Section 9.1).
        assert dumped.pop("tier", "A") in {"A", "B", "C", "none"}
        for value in _strings(dumped):
            assert _SAFE.fullmatch(value), (type(record).__name__, value)
        text = record.model_dump_json()
        for word in ("quux", "acme", "invoice", "privately", "Summarize"):
            assert word not in text.casefold()
        assert isinstance(dumped.get("decision_id", dumped.get("run_id")), UUID)
    assert set(decision.codes) <= ROUTE_CODES
    with pytest.raises(ValueError):
        RatingRecord(run_id=uuid4(), timestamp=records[0].timestamp,  # type: ignore[attr-defined]
                     target_digest="raw text", rating="good")
