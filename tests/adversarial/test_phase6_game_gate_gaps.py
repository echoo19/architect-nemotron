"""Independent game gate checks using real runtime grants and offline grid sessions.

Remaining defensive branches are not forced by corrupting trusted invariants:

* Competition coverage: evaluated targets have exactly one member; delivery is
  nonempty in both sealed manifests and _validate_policy checks max_games first.
* Competition completion: the awaited backend records every successful child
  under the same run_id before returning; a failed child raises instead.
* Manifest overlap: both manifest validators reject duplicate complete identities.
  Partition ordering: at most 1024 records receive fixed-width four-digit IDs;
  strict zipped interleaving therefore survives lexicographic sorting unchanged.
* Execution tunable types: preset canonical-JSON ranges admit only integer retry
  and context-window values. Task shape/parsing: granted task keys are constructed
  from validated GameRefs, so malformed tasks fail membership before parsing.
* Action-limit recheck: provided sessions only increment actions_used in awaited
  step calls. No session step occurs between the earlier limit check and this one.
* Decision fallthrough: retries is 0 or 1. Each final attempt returns a decision
  or raises; reaching the terminal AssertionError requires an invalid retry count.
* Factory sandbox/completion: runtime rewriting is forbidden; sandbox warm starts
  are refused. A successful awaited game backend stores its score before returning.

These statements describe supported flows, not safety against arbitrary trusted
Python mutation. No test fabricates a successful license or excludes these lines.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from pydantic import ValidationError

from architect.compiler import Compiler
from architect.floors import FloorError
from architect.games.competition import (
    CompetitionDescription,
    CompetitionFactory,
    _DelegationBackend,
)
from architect.games.evaluation import (
    OfficialPreviewManifest,
    ReceiptLedger,
    manifest_records,
    manifest_refs,
    task_for,
    validate_discovery,
)
from architect.games.execution import (
    GameBackend,
    GameExecutionPolicy,
    decision_repairs,
    parse_decision,
)
from architect.games.factory import GameFactoryConfig, _SafeLibrary, validate_offline_adapter
from architect.games.local import LocalGameProvider
from architect.games.types import (
    GameActionError,
    GameLimits,
    GameProtocolError,
    GameRef,
    GameScore,
    GameSessionError,
    GameUnavailableError,
)
from architect.models import FakeAdapter, ModelResponse, OpenAICompatibleAdapter
from architect.models.types import UsageQuote
from architect.registry import RegistryError
from architect.runtime import BudgetExceeded
from architect.runtime.evidence import scoped_evaluation
from architect.runtime.wallet import compilation_wallet
from architect.spec import Budget, FloorDefinition, FloorKind

# Fixed-file loading avoids depending on an installed or shadowed tests namespace.
_fixture_spec = importlib.util.spec_from_file_location(
    "phase6_game_gate_fixtures", Path(__file__).with_name("test_phase6_games.py")
)
assert _fixture_spec is not None and _fixture_spec.loader is not None
fixtures = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(fixtures)


def official_manifest():
    refs = tuple(
        GameRef(backend="official_offline", game_id=f"gate{i}-v1", revision="v1") for i in range(4)
    )
    return OfficialPreviewManifest(
        dataset_id="offline-gate-test",
        dataset_revision="v1",
        support_tuning=(refs[0],),
        support_held_out=(refs[1],),
        delivery=(refs[2],),
    ), refs


def test_official_manifest_sizes_roles_and_unknown_identity():
    manifest, refs = official_manifest()
    payload = manifest.model_dump(mode="json")
    payload["support_tuning"].append(refs[3].model_dump(mode="json"))
    with pytest.raises(ValidationError, match="official_preview_partition_size"):
        OfficialPreviewManifest.model_validate(payload)
    with pytest.raises(ValueError, match="official_preview_role"):
        manifest.refs("not-a-role")
    with pytest.raises(GameUnavailableError, match="official_preview_unknown_ref"):
        manifest.identity(refs[3])
    with pytest.raises(FloorError, match="game_partition_invalid"):
        manifest_records(manifest, parent=True)
    assert len(manifest_records(manifest)) == 2


@pytest.mark.parametrize("attack", ["empty", "oversized", "duplicate", "wrong_backend"])
def test_discovery_rejects_size_and_identity_before_floor_use(attack):
    manifest, refs = official_manifest()
    discovered = manifest_refs(manifest)
    if attack == "empty":
        discovered = ()
    elif attack == "oversized":
        discovered = (refs[0],) * 1025
    elif attack == "duplicate":
        discovered += (refs[0],)
    else:
        discovered += (GameRef(backend="local", game_id="foreign", revision="v1"),)
    code = "size" if attack in {"empty", "oversized"} else "identity"
    with pytest.raises(GameUnavailableError, match=f"game_manifest_discovery_{code}"):
        validate_discovery(manifest, discovered)


def test_non_string_receipt_is_not_an_evidence_capability():
    ledger = ReceiptLedger()
    with scoped_evaluation("a" * 64):
        for token in (None, 1, True, [], {}):
            with pytest.raises(FloorError, match="game_receipt_invalid"):
                ledger.consume({}, {"receipt": token})
    ledger.close()
    assert not ledger.active


async def test_duplicate_receipt_rejected_without_invalidating_first(tmp_path):
    runtime, backend, spec, provider = fixtures.execution_fixture(tmp_path)
    task = task_for(provider.manifest.refs("delivery")[0])

    async def issue_twice(context, original):
        await fixtures.game_harness(context, original)
        score = backend.completed[context.run_id]
        first = backend.ledger.issue(context, original, session_id="same-session", score=score)
        with pytest.raises(FloorError, match="game_receipt_duplicate"):
            backend.ledger.issue(context, original, session_id="same-session", score=score)
        assert backend.ledger.consume(original, first) == 1
        return {"checked": True}

    try:
        with scoped_evaluation("a" * 64):
            async with runtime.use_game_backend(backend):
                result = await runtime.evaluate(
                    spec,
                    issue_twice,
                    task,
                    fixtures.FixtureAdapter(),
                    preset="explorer",
                    candidate_digest="a" * 64,
                )
            assert result.output == {"checked": True}
        assert not backend.ledger._receipts
        assert not backend.ledger._runs
    finally:
        await backend.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("score_policy", "official_percent_v1"),
        ("score_metric", "relative_human_action_efficiency.percent.v1"),
    ],
)
def test_execution_score_policy_cannot_be_relabelled(field, value):
    with pytest.raises(ValidationError, match="game_score_policy_mismatch"):
        GameExecutionPolicy(
            small_model="fake",
            manifest_digest="a" * 64,
            support_dataset="support",
            **{field: value},
        )


@pytest.mark.parametrize(
    "text,maximum,code",
    [
        ("x" * 129, 128, "game_decision_size"),
        ("é" * 65, 128, "game_decision_size"),
        ('{"action":{"name":"ACTION1"}}', 128, "game_decision_invalid"),
    ],
)
def test_planner_decision_bytes_and_required_plan(text, maximum, code):
    with pytest.raises(GameProtocolError, match=code):
        parse_decision(text, strategy="planner", maximum_bytes=maximum)


async def evaluate_backend(runtime, backend, spec, task, adapter, harness=None, **kwargs):
    with scoped_evaluation("a" * 64):
        async with runtime.use_game_backend(backend):
            result = await runtime.evaluate(
                spec,
                harness or fixtures.game_harness,
                task,
                adapter,
                preset=spec.preset_hint,
                candidate_digest="a" * 64,
                **kwargs,
            )
            if isinstance(result.output, dict) and "receipt" in result.output:
                assert backend.ledger.consume(task, result.output) == 1
            return result


@pytest.mark.parametrize("attack", ["closed", "ungranted", "backend"])
async def test_backend_scope_target_and_score_backend_guards(tmp_path, attack):
    runtime, backend, spec, provider = fixtures.execution_fixture(tmp_path)
    target = provider.manifest.refs("delivery")[0]
    task = task_for(target)
    error, code = GameSessionError, "game_scope_expired"
    if attack == "closed":
        await backend.close()
    elif attack == "ungranted":
        task = task_for(provider.manifest.refs("support_tuning")[0])
        code = "game_target_not_granted"
    else:
        wrong = GameRef(backend="official_offline", game_id="gate-v1", revision="v1")
        backend = GameBackend(
            provider, backend.policy, backend.ledger, evaluation_refs=(wrong,), target=wrong
        )
        task = task_for(wrong)
        error, code = GameProtocolError, "game_score_backend_mismatch"
    try:
        with pytest.raises(error, match=code):
            await evaluate_backend(runtime, backend, spec, task, fixtures.FixtureAdapter())
        assert not provider.opens
        assert not backend.completed
    finally:
        await backend.close()


@pytest.mark.parametrize("attack", ["prompt_size", "unavailable_action", "repair", "planner"])
async def test_model_decisions_are_bounded_and_real_rollouts_remain_required(tmp_path, attack):
    runtime, backend, spec, provider = fixtures.execution_fixture(tmp_path)
    task = task_for(provider.manifest.refs("delivery")[0])
    adapter = fixtures.FixtureAdapter()
    params = {}
    if attack == "prompt_size":
        policy = backend.policy.model_copy(update={"limits": GameLimits(max_payload_bytes=512)})
    elif attack == "planner":
        policy = backend.policy.model_copy(update={"strategy": "planner"})
    else:
        policy = backend.policy
    backend.policy = policy
    spec = spec.model_copy(
        update={"execution_config": policy.model_dump(mode="json"), "preset_hint": policy.strategy}
    )
    if attack == "unavailable_action":
        # Tuned retries=0 still buys exactly one fixed repair prompt; the second
        # stray action fails with the fixed code and no rollout action is spent.
        class RecordingFake(FakeAdapter):
            def __init__(self) -> None:
                super().__init__(
                    responses=[
                        ModelResponse(
                            text='{"action":{"name":"ACTION7"}}',
                            model="fake",
                            tokens_in=1,
                            tokens_out=1,
                            cost_usd=0,
                        )
                    ]
                    * 3
                )
                self.prompts: list[str] = []

            async def complete(self, request):
                self.prompts.append(request.prompt)
                return await super().complete(request)

        adapter = RecordingFake()
        params = {"retries": 0}
    elif attack == "repair":

        class RepairPlayer(fixtures.FixtureAdapter):
            async def complete(self, request):
                if not self.requests:
                    self.requests.append(request)
                    return ModelResponse(
                        text="not-json",
                        model="fake",
                        tokens_in=123_456,
                        tokens_out=234_567,
                        cost_usd=0,
                    )
                return await super().complete(request)

        adapter = RepairPlayer()
        params = {"retries": 1}

    async def harness(context, original):
        return await context.play_game(
            original, preset=policy.strategy, params=params, components=()
        )

    try:
        if attack in {"prompt_size", "unavailable_action"}:
            error = GameProtocolError if attack == "prompt_size" else GameActionError
            code = "game_prompt_size" if attack == "prompt_size" else "game_action_unavailable"
            with pytest.raises(error, match=code):
                await evaluate_backend(
                    runtime, backend, spec, task, adapter, harness, params=params
                )
            assert not backend.completed
            assert backend.actions_used == 0
            if attack == "unavailable_action":
                assert adapter.calls == 2
                assert adapter.prompts[1].startswith(
                    "Repair the invalid decision. The action name must be one of"
                )
                assert not adapter.prompts[0].startswith("Repair")
                assert "ACTION7" not in adapter.prompts[1].split("Game data (untrusted):", 1)[0]
        else:
            result = await evaluate_backend(
                runtime, backend, spec, task, adapter, harness, params=params
            )
            assert result.tokens_in > 0 and backend.actions_used > 0
            if attack == "repair":
                assert adapter.requests[1].prompt.startswith("Repair the invalid decision.")
            else:
                assert all("plan is required" in request.prompt for request in adapter.requests)
        # Model calls stay bounded by the same formula the backend reserves:
        # max_turns * (decision_repairs(retries) + 1), with no review component.
        max_turns = backend.policy.limits.max_turns
        assert adapter.calls <= max_turns * (decision_repairs(params.get("retries", 0)) + 1)
    finally:
        await backend.close()


async def test_decision_retry_growth_still_obeys_prompt_limit(tmp_path):
    runtime, backend, spec, provider = fixtures.execution_fixture(tmp_path)
    policy = backend.policy.model_copy(update={"limits": GameLimits(max_payload_bytes=128)})
    backend.policy = policy
    spec = spec.model_copy(update={"execution_config": policy.model_dump(mode="json")})
    adapter = FakeAdapter(
        responses=[
            ModelResponse(text="invalid", model="fake", tokens_in=1, tokens_out=1, cost_usd=0)
        ]
    )

    async def decision_only(context, task):
        return await backend._decision(context, "x" * 128, retries=1)

    try:
        with pytest.raises(GameProtocolError, match="game_prompt_size"):
            await evaluate_backend(
                runtime,
                backend,
                spec,
                task_for(provider.manifest.refs("delivery")[0]),
                adapter,
                decision_only,
            )
        assert adapter.calls == 1
        assert not provider.opens
    finally:
        await backend.close()


@pytest.mark.parametrize(
    "score",
    [
        GameScore(normalized=None, raw=None, source="unavailable", metric="unavailable"),
        GameScore(normalized=1, raw=100, source="official", metric="foreign"),
    ],
)
async def test_host_session_scores_must_be_available_and_policy_bound(tmp_path, score):
    runtime, backend, spec, provider = fixtures.execution_fixture(tmp_path)
    opened = []

    class ScoreProvider(fixtures.RecordingProvider):
        async def open(self, ref, *, limits):
            session = await super().open(ref, limits=limits)
            original_finalize = session.finalize

            async def finalize():
                actual = await original_finalize()
                assert actual.normalized == 1
                return score

            session.finalize = finalize
            opened.append(session)
            return session

    backend.provider = ScoreProvider()
    try:
        error = GameUnavailableError if score.source == "unavailable" else GameProtocolError
        code = (
            "game_score_unavailable"
            if score.source == "unavailable"
            else "game_score_source_mismatch"
        )
        with pytest.raises(error, match=code):
            await evaluate_backend(
                runtime,
                backend,
                spec,
                task_for(provider.manifest.refs("delivery")[0]),
                fixtures.FixtureAdapter(),
            )
        assert backend.actions_used > 0
        assert not backend.completed and not backend.ledger._receipts
        with pytest.raises(GameSessionError):
            await opened[0].observe()
    finally:
        await backend.close()


def test_game_preset_policy_must_allow_strategy_and_only_game_presets(tmp_path):
    factory, _ = fixtures.factory_fixture(tmp_path)
    for allowed in (["planner"], ["explorer", "extractor"]):
        payload = factory.config.model_dump(mode="json")
        payload["compile_config"]["allowed_presets"] = allowed
        with pytest.raises(ValidationError, match="game_runtime_preset_policy"):
            GameFactoryConfig.model_validate(payload)


def test_offline_binding_rejects_misnamed_and_unrecognized_adapters():
    with pytest.raises(GameProtocolError, match="game_model_binding"):
        validate_offline_adapter(FakeAdapter(), "not-fake")

    class ForeignAdapter:
        model = "fake"

    with pytest.raises(GameProtocolError, match="game_model_offline_required"):
        validate_offline_adapter(ForeignAdapter(), "fake")


def test_localhost_is_valid_offline_adapter_binding():
    adapter = OpenAICompatibleAdapter(model="fake", base_url="http://localhost:8000/v1")
    validate_offline_adapter(adapter, "fake")


def test_read_only_library_remains_read_only():
    class ReadOnlyLibrary:
        def lookup(self, spec, model):
            return None

    wrapper = _SafeLibrary(ReadOnlyLibrary())
    assert wrapper.lookup(None, "fake") is None
    assert wrapper.store(None) == ""
    assert wrapper.record_verification(None, False) is None


def test_competition_description_bounds_total_schema_bytes(tmp_path, monkeypatch):
    _, desc, _ = fixtures.competition_fixture(tmp_path, monkeypatch)
    payload = desc.model_dump(mode="json")
    payload["observation_schema"] = {"description": "x" * 65_536}
    with pytest.raises(ValidationError, match="competition_description_size"):
        CompetitionDescription.model_validate(payload)


def test_competition_provider_and_model_binding(tmp_path, monkeypatch):
    factory, desc, _ = fixtures.competition_fixture(tmp_path, monkeypatch)
    with pytest.raises(GameProtocolError, match="competition_provider_mismatch"):
        CompetitionFactory(
            factory.compiler, factory.registry, factory.game_factory, LocalGameProvider()
        )
    with pytest.raises(GameProtocolError, match="competition_model_mismatch"):
        factory._policy(desc.model_copy(update={"small_model": "other"}))
    assert not factory.compiler.runtime.store.rows()


def test_competition_scoped_compiler_denies_other_floor_and_model(tmp_path, monkeypatch):
    factory, desc, _ = fixtures.competition_fixture(tmp_path, monkeypatch)
    ledger = ReceiptLedger()
    try:
        compiler = factory._compiler(factory._policy(desc), ledger)
        with pytest.raises(GameProtocolError, match="competition_model_not_allowed"):
            compiler.adapter_factory("other")
        with pytest.raises(GameProtocolError, match="competition_floor_not_allowed"):
            compiler.floor_factory(
                FloorDefinition(kind=FloorKind.HELD_OUT_GAME_SCORE, dataset="foreign", limit=1)
            )
        assert not factory.compiler.runtime.store.rows()
    finally:
        ledger.close()


def test_remaining_child_requires_live_ancestor_wallet(tmp_path, monkeypatch):
    factory, _, _ = fixtures.competition_fixture(tmp_path, monkeypatch)
    with pytest.raises(GameProtocolError, match="competition_wallet_missing"):
        factory._remaining_child()
    with compilation_wallet(Budget(cost_usd=0.01, compute_s=30)) as wallet:
        wallet.reserve(UsageQuote(tokens_in=1, tokens_out=1, cost_usd=0.01))
        child = factory._remaining_child()
        assert child.provider is factory.provider
        assert child.config.budget.cost_usd == factory.game_factory.config.budget.cost_usd
        assert wallet.cost_usd == wallet.budget.cost_usd and not wallet.exhausted
        with pytest.raises(BudgetExceeded):
            wallet.reserve(UsageQuote(tokens_in=1, tokens_out=1, cost_usd=0.001))
        with pytest.raises(BudgetExceeded, match="Competition budget exhausted"):
            factory._remaining_child()
    assert not factory.game_factory.provider.opens


async def test_reviewed_but_unapproved_competition_does_not_compile_children(tmp_path, monkeypatch):
    factory, desc, player = fixtures.competition_fixture(tmp_path, monkeypatch)
    proposal = await factory.compile(desc)
    before = len(player.requests)
    result = await factory.compile(desc, approved=False, approved_spec=proposal.spec)
    assert result.approval_required and result.artifact is None
    assert result.spec == proposal.spec and len(player.requests) == before
    assert not factory.observed_children


async def licensed_competition(tmp_path, monkeypatch):
    factory, desc, player = fixtures.competition_fixture(tmp_path, monkeypatch)
    proposal = await factory.compile(desc)
    result = await factory.compile(desc, approved=True, approved_spec=proposal.spec)
    assert result.artifact is not None and result.artifact.licensed
    return factory, desc, player, result


async def test_install_requires_exact_artifact_spec_and_planner(tmp_path, monkeypatch):
    factory, desc, _, result = await licensed_competition(tmp_path, monkeypatch)
    for artifact in (
        result.artifact.model_copy(update={"preset": "explorer"}),
        result.artifact.model_copy(
            update={"spec": result.spec.model_copy(update={"task": "other"})}
        ),
    ):
        with pytest.raises(RegistryError, match="competition_artifact_mismatch"):
            await factory.install(result.model_copy(update={"artifact": artifact}), name=desc.name)
    assert factory.registry.list() == ()


async def test_failed_fresh_recompile_does_not_replace_installed_license(tmp_path, monkeypatch):
    factory, desc, player, result = await licensed_competition(tmp_path, monkeypatch)
    await factory.install(result, name=desc.name)
    installed = factory.registry.get(desc.name)
    player.losing = True
    with pytest.raises(RegistryError, match="competition_recompile_failed"):
        await factory.recompile(desc.name)
    assert factory.registry.status(desc.name).revision == 1
    assert factory.registry.get(desc.name) == installed


@pytest.mark.parametrize("attack", ["policy", "preview_target", "delivery_task"])
async def test_delegation_checks_policy_preview_and_complete_delivery(
    tmp_path, monkeypatch, attack
):
    factory, desc, player, result = await licensed_competition(tmp_path, monkeypatch)
    policy = factory._policy(desc)
    ledger = ReceiptLedger()
    backend = _DelegationBackend(factory, policy, ledger)
    before = len(player.requests)
    child_count = len(factory.observed_children)
    runtime = factory.compiler.runtime
    try:
        async with runtime.use_game_backend(backend):
            if attack == "delivery_task":
                # Valid licensed planner, incomplete caller delivery list.
                with pytest.raises(GameProtocolError, match="competition_delivery_incomplete"):
                    await runtime.run(result.artifact, {"games": []}, player)
            else:
                preset = "explorer" if attack == "policy" else "planner"
                task = task_for(factory.provider.manifest.refs("delivery")[0])

                async def delegate(context, original):
                    return await context.play_game(
                        original, preset=preset, params={}, components=()
                    )

                code = (
                    "competition_policy_mismatch"
                    if attack == "policy"
                    else "competition_preview_not_granted"
                )
                with scoped_evaluation("a" * 64):
                    with pytest.raises(GameProtocolError, match=code):
                        await runtime.evaluate(
                            result.spec,
                            delegate,
                            task,
                            player,
                            preset=preset,
                            candidate_digest="a" * 64,
                        )
        assert len(player.requests) == before
        assert len(factory.observed_children) == child_count
        assert not backend.attempted and not backend.completed
    finally:
        ledger.close()


async def test_game_scoped_compiler_rejects_foreign_model_and_floor(tmp_path, monkeypatch):
    import architect.games.factory as factory_module

    checked = []

    class InspectCompiler(Compiler):
        async def compile(self, source, **kwargs):
            with pytest.raises(GameProtocolError, match="game_model_not_allowed"):
                self.adapter_factory("foreign")
            with pytest.raises(GameProtocolError, match="game_floor_not_allowed"):
                self.floor_factory(
                    FloorDefinition(kind=FloorKind.HELD_OUT_GAME_SCORE, dataset="foreign", limit=1)
                )
            checked.append(True)
            # Negative checks must not poison or replace actual compile/verification.
            return await super().compile(source, **kwargs)

    factory, _ = fixtures.factory_fixture(tmp_path)
    monkeypatch.setattr(factory_module, "Compiler", InspectCompiler)
    result = await factory.run(
        factory.provider.manifest.refs("delivery")[0], support_dataset="support"
    )
    assert checked == [True]
    assert result.accepted and result.score.normalized == 1
    # The verified expire-lifecycle artifact must be consumed by delivery.
    assert not result.compile_result.artifact.licensed
    assert result.compile_result.report.passed and result.actions_used > 0


@pytest.mark.parametrize("raw,normalized", [(101, 1), (50, 1)])
async def test_official_score_requires_pinned_percent_scale(tmp_path, monkeypatch, raw, normalized):
    from architect.games.official import OfficialGameSession

    factory, operations, _ = fixtures.scripted_official_factory(tmp_path, monkeypatch, 100)
    original = OfficialGameSession.finalize
    returned = []

    async def inconsistent_score(self):
        score = await original(self)
        assert score.raw == 100 and score.normalized == 1
        returned.append(self.ref)
        # A structurally valid host score with a wrong scale must fail closed.
        return GameScore(normalized=normalized, raw=raw, source="official", metric=score.metric)

    monkeypatch.setattr(OfficialGameSession, "finalize", inconsistent_score)
    result = await factory.run(factory.manifest.refs("delivery")[0], support_dataset="support")
    assert returned and any(operation["op"] == "finalize" for operation in operations)
    assert not result.accepted and result.play_result is None and result.score is None
    assert result.failures == ("game_compile_failed",)
    assert result.compile_result.report.floor_score == 0
    assert not result.compile_result.artifact.licensed


@pytest.mark.parametrize("failed_compile", [False, True])
async def test_cleanup_errors_block_acceptance_and_preserve_primary_failure(
    tmp_path, monkeypatch, failed_compile
):
    factory, _ = fixtures.factory_fixture(tmp_path, fixtures.FixtureAdapter(losing=failed_compile))
    original_open = factory.provider.open
    probe_sessions = []
    close_calls = []

    async def open_with_cleanup_fault(ref, *, limits):
        session = await original_open(ref, limits=limits)
        if not probe_sessions:
            probe_sessions.append(session)
            original_close = session.close

            async def close():
                close_calls.append(True)
                await original_close()
                # Successful delivery closes once in run(); its second close is
                # factory cleanup. Failed compile leaves the probe to backend.close().
                if failed_compile or len(close_calls) >= 2:
                    raise RuntimeError("private-cleanup-fault")

            session.close = close
        return session

    monkeypatch.setattr(factory.provider, "open", open_with_cleanup_fault)
    result = await factory.run(
        factory.provider.manifest.refs("delivery")[0], support_dataset="support"
    )
    # Backend-owned failed probe closes once; delivered probe gets final cleanup.
    assert len(close_calls) == (1 if failed_compile else 2)
    assert not result.accepted
    assert result.failures == ("game_compile_failed" if failed_compile else "game_cleanup_failed",)
    assert "private-cleanup-fault" not in result.model_dump_json()
    if failed_compile:
        assert result.play_result is None and result.score is None
    else:
        assert result.play_result is not None and result.score.normalized == 1
    with pytest.raises(GameSessionError):
        await probe_sessions[0].observe()


@pytest.mark.parametrize("quote_cost", [0, 0.001])
async def test_exact_ancestor_dollar_ceiling_allows_only_zero_cost_model_work(tmp_path, quote_cost):
    player = fixtures.FixtureAdapter(cost=quote_cost)
    factory, _ = fixtures.factory_fixture(tmp_path, player)
    with compilation_wallet(Budget(cost_usd=0.01, compute_s=30)) as ancestor:
        ancestor.reserve(UsageQuote(tokens_in=0, tokens_out=0, cost_usd=0.01))
        assert not ancestor.exhausted
        result = await factory.run(
            factory.provider.manifest.refs("delivery")[0], support_dataset="support"
        )
        assert ancestor.cost_usd == 0.01
        assert result.cost_usd == 0
        if quote_cost == 0:
            assert result.accepted and result.score.normalized == 1
            assert player.requests and result.tokens_in > 0
            assert not ancestor.exhausted
        else:
            assert not result.accepted and result.failures == ("game_budget_exceeded",)
            assert not player.requests, "Positive quote must be denied before model I/O"
            assert result.play_result is None and result.score is None
            assert ancestor.exhausted
    # Discovery/probing is unpriced, remains deadline/action bounded, and need
    # not be prohibited merely because a later positive model quote is refused.
    assert factory.provider.opens


async def test_game_prompts_mark_fixed_instructions_without_changing_text(tmp_path):
    runtime, backend, spec, provider = fixtures.execution_fixture(tmp_path)
    policy = backend.policy.model_copy(update={"strategy": "planner"})
    backend.policy = policy
    spec = spec.model_copy(
        update={"execution_config": policy.model_dump(mode="json"), "preset_hint": "planner"}
    )
    adapter = fixtures.FixtureAdapter()

    async def harness(context, original):
        return await context.play_game(original, preset="planner", params={}, components=())

    try:
        await evaluate_backend(
            runtime,
            backend,
            spec,
            task_for(provider.manifest.refs("delivery")[0]),
            adapter,
            harness,
        )
    finally:
        await backend.close()
    assert adapter.requests
    from architect.games.execution import game_instructions

    instructions = game_instructions("planner")
    for request in adapter.requests:
        # The byte-stable instructions are marked; the text is the old concatenation.
        assert request.cache_prefix == instructions
        prefix, rest = request.split()
        assert prefix + rest == request.prompt
        assert rest.startswith("\nGame data (untrusted):\n")
