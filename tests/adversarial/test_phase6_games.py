"""Independent Phase 6 attacks with OFFLINE SCRIPTED models, not ARC results.

FixtureAdapter reads only public observations. Real providers, compiler stages,
runtime grants, rollouts and receipt consumption remain in the execution path.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from pathlib import Path

import pytest
from pydantic import JsonValue, ValidationError

from architect.compiler import CompileConfig, Compiler
from architect.floors import FloorError
from architect.games.competition import CompetitionDescription, CompetitionFactory
from architect.games.evaluation import (
    GameScorer,
    OfficialPreviewManifest,
    ReceiptLedger,
    manifest_records,
    manifest_refs,
    task_for,
)
from architect.games.execution import GameBackend, GameExecutionPolicy, execute_game
from architect.games.factory import GameFactory, GameFactoryConfig, validate_offline_adapter
from architect.games.local import LocalFixtureManifest, LocalGameProvider
from architect.games.official import OfficialConfig, OfficialGameProvider
from architect.games.types import (
    GameLimits,
    GameProtocolError,
    GameRef,
    GameSession,
    GameUnavailableError,
)
from architect.models import FakeAdapter, ModelRequest, ModelResponse, OpenAICompatibleAdapter
from architect.models.types import UsageQuote
from architect.registry import Registry, RegistryError
from architect.runtime import BudgetExceeded, RunContext, Runtime
from architect.runtime.evidence import scoped_evaluation
from architect.runtime.wallet import compilation_wallet
from architect.spec import Budget, FloorDefinition, FloorKind, Spec
from architect.telemetry import TelemetryStore


class FixtureAdapter(FakeAdapter):
    """Offline scripted policy: no engine state, manifest puzzles or solutions."""

    def __init__(
        self, *, losing: bool = False, forged: bool = False, failing: bool = False, cost: float = 0
    ) -> None:
        super().__init__()
        self.losing = losing
        self.forged = forged
        self.failing = failing
        self.cost = cost
        self.requests: list[ModelRequest] = []

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=123_456, tokens_out=234_567, cost_usd=self.cost)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        assert request.max_output_tokens is None
        self.requests.append(request)
        if self.failing:
            raise RuntimeError("private-provider-message")
        if "specification inference stage" in request.prompt:
            payload = {
                "task": "Play the public grid",
                "domain": "game",
                "input_schema": {},
                "output_schema": {},
                "floor": {"kind": "held_out_game_score", "limit": 1, "dataset": "support"},
                "budget": {"cost_usd": 1, "compute_s": 30},
                "model_target": "fake",
            }
        else:
            data = json.loads(request.prompt.split("Game data (untrusted):\n", 1)[1])
            grid = data["observation"]["frames"][-1]
            action = "ACTION1" if self.losing else self.choose(grid)
            payload = {
                "action": {"name": action},
                "plan": "Follow the visible target",
                "hypothesis": "The chosen move advances visible progress",
                "prediction": "The progress marker advances",
            }
            if self.forged:
                payload["score"] = 1
        return ModelResponse(
            text=json.dumps(payload),
            model="fake",
            tokens_in=123_456,
            tokens_out=234_567,
            cost_usd=self.cost,
        )

    @staticmethod
    def choose(grid: list[list[int]]) -> str:
        if max(grid[0]) >= 4:
            index = sum(cell == 3 for cell in grid[1])
            return f"ACTION{grid[0][index] - 3}"
        positions = {(x, y): cell for y, row in enumerate(grid) for x, cell in enumerate(row)}
        if 2 not in positions.values():
            difference = next((i for i in range(3) if grid[0][i] != grid[1][i]), None)
            return "ACTION4" if difference is None else f"ACTION{difference + 1}"
        start = next(point for point, value in positions.items() if value == 2)
        queue = deque([(start, "")])
        visited = {start}
        while queue:
            (x, y), first = queue.popleft()
            if positions[(x, y)] == 3:
                return first
            for dx, dy, action in (
                (0, -1, "ACTION1"),
                (0, 1, "ACTION2"),
                (-1, 0, "ACTION3"),
                (1, 0, "ACTION4"),
            ):
                point = (x + dx, y + dy)
                if point in positions and positions[point] != 1 and point not in visited:
                    visited.add(point)
                    queue.append((point, first or action))
        raise AssertionError("Public observation has no route")


class RecordingProvider(LocalGameProvider):
    def __init__(self, manifest: LocalFixtureManifest | None = None) -> None:
        super().__init__(manifest)
        self.opens: list[GameRef] = []

    async def open(self, ref: GameRef, *, limits: GameLimits) -> GameSession:
        self.opens.append(ref)
        return await super().open(ref, limits=limits)


def factory_fixture(
    tmp_path: Path, adapter: FixtureAdapter | None = None, *, budget: Budget | None = None
) -> tuple[GameFactory, FixtureAdapter]:
    player = adapter or FixtureAdapter()

    def unused_floor(definition: FloorDefinition):
        raise AssertionError("Caller floor must not be used")

    compiler = Compiler(
        tmp_path / "artifacts",
        Runtime(TelemetryStore(tmp_path / "runs")),
        lambda model: player,
        unused_floor,
    )
    config = GameFactoryConfig(
        budget=budget or Budget(cost_usd=1, compute_s=30),
        small_model="fake",
        compile_config=CompileConfig(enable_rewriting=False, max_rewrites=0, max_candidates=1),
        limits=GameLimits(max_actions=16, max_turns=16),
    )
    return GameFactory(compiler, RecordingProvider(), config), player


def execution_fixture(tmp_path: Path):
    provider = RecordingProvider()
    refs = provider.manifest.refs("delivery")
    policy = GameExecutionPolicy(
        small_model="fake",
        manifest_digest=provider.manifest.digest,
        support_dataset="support",
        limits=GameLimits(max_actions=16, max_turns=16),
    )
    backend = GameBackend(provider, policy, ReceiptLedger(), evaluation_refs=refs, target=refs[0])
    spec = Spec(
        task="Public grid play",
        domain="game",
        model_target="fake",
        input_schema={},
        output_schema={},
        budget=Budget(cost_usd=1, compute_s=10),
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_GAME_SCORE, limit=1, dataset="support"),
        execution_config=policy.model_dump(mode="json"),
        preset_hint="explorer",
    )
    return Runtime(TelemetryStore(tmp_path / "runs")), backend, spec, provider


async def game_harness(context: RunContext, task: JsonValue) -> JsonValue:
    return await execute_game(context, task, preset="explorer", params={}, components=())


@pytest.mark.parametrize("index", [0, 1, 2])
async def test_offline_scripted_real_rollout_scores_win_and_legal_loss(tmp_path, index):
    runtime, backend, spec, provider = execution_fixture(tmp_path)
    task = task_for(provider.manifest.refs("delivery")[index])
    try:
        async with runtime.use_game_backend(backend):
            for losing, expected in ((False, 1), (True, 0)):
                with scoped_evaluation("a" * 64):
                    result = await runtime.evaluate(
                        spec,
                        game_harness,
                        task,
                        FixtureAdapter(losing=losing),
                        preset="explorer",
                        candidate_digest="a" * 64,
                    )
                    assert await GameScorer(backend.ledger).score(task, result.output) == expected
                    assert result.tokens_in >= 123_456
                    with pytest.raises(FloorError):
                        await GameScorer(backend.ledger).score(task, result.output)
        assert len(provider.opens) == 2
        assert backend.actions_used > 0
    finally:
        await backend.close()


async def test_exhausted_parent_wallet_blocks_factory_before_environment_side_effects(tmp_path):
    factory, player = factory_fixture(tmp_path)
    provider = factory.provider
    target = provider.manifest.refs("delivery")[0]
    with compilation_wallet(Budget(cost_usd=0.001, compute_s=30)) as parent:
        with pytest.raises(BudgetExceeded):
            parent.reserve(UsageQuote(tokens_in=1, tokens_out=1, cost_usd=0.01))
        assert parent.exhausted
        result = await factory.run(target, support_dataset="support")
    assert not result.accepted
    assert not player.requests
    assert not provider.opens, "Exhausted ancestor must stop setup/probe, not merely inference"
    assert result.actions_used == 0


@pytest.mark.parametrize("attack", ["cross_case", "cross_candidate", "expired", "forged"])
async def test_receipts_bound_to_case_candidate_and_measurement(tmp_path, attack):
    runtime, backend, spec, provider = execution_fixture(tmp_path)
    task = task_for(provider.manifest.refs("delivery")[0])
    scorer = GameScorer(backend.ledger)
    try:
        async with runtime.use_game_backend(backend):
            with scoped_evaluation("a" * 64):
                result = await runtime.evaluate(
                    spec,
                    game_harness,
                    task,
                    FixtureAdapter(),
                    preset="explorer",
                    candidate_digest="a" * 64,
                )
                if attack == "cross_case":
                    other = task_for(provider.manifest.refs("delivery")[1])
                    with pytest.raises(FloorError):
                        await scorer.score(other, result.output)
                elif attack == "cross_candidate":
                    with scoped_evaluation("b" * 64):
                        with pytest.raises(FloorError):
                            await scorer.score(task, result.output)
                elif attack == "forged":
                    for output in (
                        {"score": 1},
                        {"receipt": "f" * 32},
                        {**result.output, "score": 1},
                    ):
                        with pytest.raises(FloorError):
                            await scorer.score(task, output)
                    assert await scorer.score(task, result.output) == 1
            with scoped_evaluation("a" * 64):
                with pytest.raises(FloorError):
                    await scorer.score(task, result.output)
    finally:
        await backend.close()


async def test_candidate_mismatch_cannot_issue_receipt(tmp_path):
    runtime, backend, spec, provider = execution_fixture(tmp_path)
    task = task_for(provider.manifest.refs("delivery")[0])
    try:
        with scoped_evaluation("b" * 64):
            async with runtime.use_game_backend(backend):
                with pytest.raises(FloorError, match="game_receipt_scope"):
                    await runtime.evaluate(
                        spec,
                        game_harness,
                        task,
                        FixtureAdapter(),
                        preset="explorer",
                        candidate_digest="a" * 64,
                    )
        assert backend.actions_used > 0
    finally:
        await backend.close()


async def test_model_authored_score_cannot_replace_real_rollout(tmp_path):
    factory, player = factory_fixture(tmp_path, FixtureAdapter(losing=True, forged=True))
    result = await factory.run(
        factory.provider.manifest.refs("delivery")[0], support_dataset="support"
    )
    assert not result.accepted and result.play_result is None
    assert result.compile_result is not None
    assert result.compile_result.report is not None
    assert result.compile_result.report.floor_score == 0
    assert any("Game data (untrusted):" in request.prompt for request in player.requests)
    assert result.tokens_in > 0


@pytest.mark.parametrize("attack", ["other_target", "mutate_task", "boolean_alias"])
async def test_original_task_binding_survives_substitution_and_python_equality(tmp_path, attack):
    runtime, backend, spec, provider = execution_fixture(tmp_path)
    task = task_for(provider.manifest.refs("delivery")[0])
    if attack == "boolean_alias":
        task["nonce"] = 1

    async def substitute(context, original):
        if attack == "other_target":
            supplied = task_for(provider.manifest.refs("delivery")[1])
        elif attack == "mutate_task":
            original["game"] = task_for(provider.manifest.refs("delivery")[1])["game"]
            supplied = original
        else:
            supplied = dict(original, nonce=True)
            assert supplied == original  # Python equality is NOT canonical JSON binding.
        return await game_harness(context, supplied)

    try:
        async with runtime.use_game_backend(backend):
            with pytest.raises(ValueError, match="authenticated binding"):
                await runtime.evaluate(
                    spec,
                    substitute,
                    task,
                    FixtureAdapter(),
                    preset="explorer",
                    candidate_digest="a" * 64,
                )
        assert not provider.opens
    finally:
        await backend.close()


async def test_runtime_scope_is_owner_bound_and_revoked(tmp_path):
    runtime, backend, spec, provider = execution_fixture(tmp_path)
    foreign = Runtime(TelemetryStore(tmp_path / "foreign"))
    task = task_for(provider.manifest.refs("delivery")[0])
    try:
        async with runtime.use_game_backend(backend):
            with pytest.raises(BudgetExceeded):
                await foreign.evaluate(
                    spec,
                    game_harness,
                    task,
                    FixtureAdapter(),
                    preset="explorer",
                    candidate_digest="a" * 64,
                )
        with pytest.raises(BudgetExceeded):
            await runtime.evaluate(
                spec,
                game_harness,
                task,
                FixtureAdapter(),
                preset="explorer",
                candidate_digest="a" * 64,
            )
        assert not provider.opens
    finally:
        await backend.close()


async def test_scope_exit_cancels_active_open_before_observation(tmp_path):
    runtime, backend, spec, provider = execution_fixture(tmp_path)
    started, cancelled = asyncio.Event(), asyncio.Event()

    class BlockingProvider(RecordingProvider):
        async def open(self, ref, *, limits):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            raise AssertionError("Unreachable open completion")

    blocker = BlockingProvider()
    backend.provider = blocker
    task = task_for(provider.manifest.refs("delivery")[0])
    try:
        async with runtime.use_game_backend(backend):
            pending = asyncio.create_task(
                runtime.evaluate(
                    spec,
                    game_harness,
                    task,
                    FixtureAdapter(),
                    preset="explorer",
                    candidate_digest="a" * 64,
                )
            )
            await asyncio.wait_for(started.wait(), timeout=2)
        outcome = await asyncio.gather(pending, return_exceptions=True)
        assert isinstance(outcome[0], (asyncio.CancelledError, BudgetExceeded))
        assert cancelled.is_set() and backend.actions_used == 0
    finally:
        await backend.close()


async def test_absorb_refuses_child_with_pending_game_work(tmp_path):
    runtime, backend, spec, provider = execution_fixture(tmp_path)
    entered, cancelled = asyncio.Event(), asyncio.Event()

    class BlockingBackend:
        async def run(self, context, task, *, preset, params, components):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    async def orphan_attempt(context, task):
        child = context.fork(FixtureAdapter(), spec.budget)
        work = asyncio.create_task(game_harness(child, task))
        await asyncio.wait_for(entered.wait(), timeout=2)
        try:
            with pytest.raises(ValueError, match="unsettled"):
                context.absorb(child)
        finally:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            context.absorb(child)
        return {"rejected": True}

    async with runtime.use_game_backend(BlockingBackend()):
        result = await runtime.evaluate(
            spec,
            orphan_attempt,
            task_for(provider.manifest.refs("delivery")[0]),
            FixtureAdapter(),
            preset="explorer",
        )
    assert result.output == {"rejected": True} and cancelled.is_set()
    assert not provider.opens
    await backend.close()


@pytest.mark.parametrize("index", [0, 1, 2])
async def test_parent_heldout_target_only_opens_after_support_compile_and_fresh_stage4(
    tmp_path, index
):
    factory, player = factory_fixture(tmp_path)
    provider = factory.provider
    target = provider.manifest.refs("parent_held_out")[index]

    class ForbiddenLibrary:
        def lookup(self, spec, model):
            raise AssertionError("Isolated child must not read library")

        def store(self, artifact):
            raise AssertionError("Isolated child must not write library")

        def record_verification(self, source, passed):
            raise AssertionError("Isolated child must not update library")

    factory.compiler.library = ForbiddenLibrary()
    rejected = await factory.run(target, support_dataset="support")
    assert not rejected.accepted and not provider.opens and not player.requests
    result = await factory.run_isolated(target, support_dataset="support")
    assert result.accepted, result
    assert result.compile_result.stages == (0, 1, 2, 4)
    assert result.compile_result.report.floor_score == 1
    assert provider.opens[-1] == target
    support = set(
        (*provider.manifest.refs("support_tuning"), *provider.manifest.refs("support_held_out"))
    )
    assert set(provider.opens[:-1]) == support
    assert len([ref for ref in provider.opens if ref == target]) == 1
    assert provider.opens[0] == provider.manifest.refs("support_tuning")[0]
    inference = [
        request.prompt
        for request in player.requests
        if "specification inference stage" in request.prompt
    ]
    assert len(inference) == 1
    observed = json.loads(inference[0].split("Request data (untrusted):\n", 1)[1])["source"]
    # Host-side comparison uses public observe only; these references never enter the adapter.
    audit_provider = LocalGameProvider(provider.manifest)
    support_session = await audit_provider.open(provider.opens[0], limits=factory.config.limits)
    target_session = await audit_provider.open(target, limits=factory.config.limits)
    try:
        support_observation = (await support_session.observe()).model_dump(mode="json")
        target_observation = (await target_session.observe()).model_dump(mode="json")
        assert observed["observations"][0] == support_observation
        assert observed["observations"][0] != target_observation
    finally:
        await support_session.close()
        await target_session.close()
    assert target.game_id not in inference[0] and target.revision not in inference[0]
    assert target.game_id not in result.compile_result.spec.model_dump_json()
    assert result.score.source == "local" and result.score.normalized == 1


@pytest.mark.parametrize(
    "family", ["local-navigation-v1", "local-switches-v1", "local-sequence-v1"]
)
def test_duplicate_puzzle_aliases_cannot_cross_manifest_roles(family):
    manifest = LocalFixtureManifest()
    payload = manifest.model_dump(mode="json")
    members = [entry for entry in payload["fixtures"] if entry["puzzle"]["family"] == family]
    source, alias = members[:2]
    alias["seed"] = source["seed"] + 900
    alias["revision"] = "different-caller-label"
    alias["puzzle"] = json.loads(json.dumps(source["puzzle"]))
    if family == "local-navigation-v1":
        alias["puzzle"]["walls"] = list(reversed(alias["puzzle"]["walls"])) * 2
    else:
        alias["puzzle"]["width"] += 1  # Cosmetic canvas alias is not a new instance.
    with pytest.raises(ValidationError, match="duplicate_puzzle_identity"):
        LocalFixtureManifest.model_validate(payload)


def test_role_swap_changes_frozen_policy_digest_and_actual_floor_split():
    manifest = LocalFixtureManifest()
    payload = manifest.model_dump(mode="json")
    first, second = payload["fixtures"][:2]
    first["role"], second["role"] = second["role"], first["role"]
    changed = LocalFixtureManifest.model_validate(payload)
    assert changed.digest != manifest.digest
    for parent in (False, True):
        records = sorted(manifest_records(changed, parent=parent), key=lambda row: row.case_id)
        tuning = {GameRef.model_validate(record.task["game"]) for record in records[::2]}
        heldout = {GameRef.model_validate(record.task["game"]) for record in records[1::2]}
        assert tuning == set(changed.refs("parent_tuning" if parent else "support_tuning"))
        assert heldout == set(changed.refs("parent_held_out" if parent else "support_held_out"))
        assert {changed.identity(ref) for ref in tuning}.isdisjoint(
            changed.identity(ref) for ref in heldout
        )


async def test_changed_manifest_policy_refuses_side_effects(tmp_path):
    runtime, backend, spec, provider = execution_fixture(tmp_path)
    payload = provider.manifest.model_dump(mode="json")
    a, b = payload["fixtures"][:2]
    a["role"], b["role"] = b["role"], a["role"]
    changed = LocalFixtureManifest.model_validate(payload)
    backend.policy = backend.policy.model_copy(update={"manifest_digest": changed.digest})
    try:
        async with runtime.use_game_backend(backend):
            with pytest.raises(GameProtocolError, match="game_policy_mismatch"):
                await runtime.evaluate(
                    spec,
                    game_harness,
                    task_for(provider.manifest.refs("delivery")[0]),
                    FixtureAdapter(),
                    preset="explorer",
                )
        assert not provider.opens
    finally:
        await backend.close()


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com/v1",
        "http://192.168.1.5:8000/v1",
        "http://127.0.0.1.evil.test/v1",
        "http://user:secret@localhost:8000/v1",
    ],
)
def test_cloud_and_nonlocal_model_bindings_rejected(url):
    adapter = OpenAICompatibleAdapter(base_url=url)
    with pytest.raises(GameProtocolError):
        validate_offline_adapter(adapter, adapter.model)


def test_runtime_rewrite_and_model_ladder_policy_revalidated(tmp_path):
    factory, _ = factory_fixture(tmp_path)
    for compile_config in (
        CompileConfig(),
        CompileConfig(enable_rewriting=False, max_rewrites=0, model_ladder=("fake", "cloud")),
    ):
        config = factory.config.model_copy(update={"compile_config": compile_config})
        with pytest.raises(ValidationError):
            GameFactory(factory.compiler, factory.provider, config)


async def test_offline_cached_children_get_fresh_stage4_and_real_play(tmp_path):
    factory, player = factory_fixture(tmp_path)
    target = factory.provider.manifest.refs("delivery")[0]
    first = await factory.run(target, support_dataset="support")
    assert first.accepted
    before = len(player.requests)
    second = await factory.run(target, support_dataset="support")
    assert second.accepted and second.compile_result.warm_started
    assert (
        first.compile_result.report.verification_id != second.compile_result.report.verification_id
    )
    assert second.compile_result.stages[-1] == 4
    assert len(player.requests) > before and second.actions_used > 0
    assert second.play_result.run_id != first.play_result.run_id
    rows = factory.compiler.runtime.store.rows()
    assert first.tokens_in + second.tokens_in == sum(row.tokens_in for row in rows)
    assert first.tokens_out + second.tokens_out == sum(row.tokens_out for row in rows)
    assert all("frames" not in row.model_dump_json() for row in rows)


@pytest.mark.parametrize("attack", ["sandbox", "rewrite"])
async def test_rewritten_or_sandbox_library_warm_start_never_delivers(tmp_path, attack):
    factory, player = factory_fixture(tmp_path)
    target = factory.provider.manifest.refs("delivery")[0]
    first = await factory.run(target, support_dataset="support")
    assert first.accepted
    artifact = first.compile_result.artifact
    if attack == "sandbox":
        artifact = artifact.model_copy(update={"execution_mode": "sandbox"})
    else:
        (artifact.directory / "rewrite.diff").write_text("untrusted rewrite marker")

    class PoisonedLibrary:
        def lookup(self, spec, model):
            return artifact

    factory.compiler.library = PoisonedLibrary()
    before = len(player.requests)
    second = await factory.run(target, support_dataset="support")
    assert not second.accepted and second.play_result is None
    assert len(player.requests) > before  # Inference occurred; no silent test-only short circuit.
    assert not any(
        "Game data (untrusted):" in request.prompt for request in player.requests[before:]
    )


async def test_failed_model_work_keeps_usage_and_fixed_error(tmp_path):
    factory, player = factory_fixture(tmp_path, FixtureAdapter(failing=True, cost=0.02))
    result = await factory.run(
        factory.provider.manifest.refs("delivery")[0], support_dataset="support"
    )
    assert not result.accepted and len(player.requests) == 1
    assert result.cost_usd == pytest.approx(0.02)
    assert result.tokens_in == 123_456 and result.tokens_out == 234_567
    assert "private-provider-message" not in result.model_dump_json()
    assert result.actions_used > 0


async def test_ancestor_deadline_interrupts_setup_not_only_model_calls(tmp_path):
    factory, player = factory_fixture(tmp_path)
    entered, cancelled = asyncio.Event(), asyncio.Event()

    class BlockingProvider(RecordingProvider):
        async def open(self, ref, *, limits):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    provider = BlockingProvider()
    factory.provider = provider
    # A watchdog makes a broken implementation fail quickly without leaving a task alive.
    with compilation_wallet(Budget(cost_usd=1, compute_s=0.03)):
        try:
            result = await asyncio.wait_for(
                factory.run(provider.manifest.refs("delivery")[0], support_dataset="support"),
                timeout=0.5,
            )
        except TimeoutError:
            pytest.fail("Provider setup outlived ancestor compute deadline")
    assert entered.is_set() and cancelled.is_set()
    assert not result.accepted and result.failures == ("game_budget_exceeded",)
    assert not player.requests and result.wall_time_s < 0.4


async def test_official_missing_files_never_fall_back_to_local(tmp_path):
    provider = OfficialGameProvider(
        OfficialConfig(mode="offline", staged_game_dir=tmp_path / "absent-public-games")
    )
    try:
        with pytest.raises(GameUnavailableError, match="official_staged_files_missing"):
            await provider.discover()
    finally:
        await provider.close()


async def test_legal_wrong_play_fails_child_license_without_delivery(tmp_path):
    factory, player = factory_fixture(tmp_path, FixtureAdapter(losing=True))
    result = await factory.run(
        factory.provider.manifest.refs("delivery")[0], support_dataset="support"
    )
    assert not result.accepted and result.play_result is None
    assert result.compile_result.report.floor_score == 0
    assert result.actions_used > 3 and len(player.requests) > 1


def competition_fixture(tmp_path, monkeypatch, player=None):
    game, player = factory_fixture(tmp_path, player)
    game.compiler.config = game.config.compile_config
    factory = CompetitionFactory(
        game.compiler, Registry(tmp_path / "registry"), game, game.provider
    )
    factory.observed_children = []
    original = GameFactory.run_isolated

    async def record(self, target, *, support_dataset):
        # Observation-only wrapper. Preserve real wallets, scopes, compile and play.
        result = await original(self, target, support_dataset=support_dataset)
        factory.observed_children.append((target, result))
        return result

    monkeypatch.setattr(GameFactory, "run_isolated", record)
    description = CompetitionDescription(
        name="offline-scripted-independent-review",
        rules="Synthetic public-grid rules only",
        action_schema={},
        observation_schema={},
        preview_dataset="parent-preview",
        child_support_dataset="child-support",
        compute_cap=Budget(cost_usd=1, compute_s=60),
        small_model="fake",
        max_games=3,
    )
    return factory, description, player


async def test_offline_installed_parent_executes_fresh_children_with_isolated_sources(
    tmp_path, monkeypatch
):
    factory, description, player = competition_fixture(tmp_path, monkeypatch)
    proposal = await factory.compile(description, approved=True)
    assert proposal.approval_required and proposal.artifact is None
    assert not factory.provider.opens
    compiled = await factory.compile(description, approved=True, approved_spec=proposal.spec)
    assert compiled.artifact.licensed and compiled.report.floor_score == 1
    assert len(factory.observed_children) == 6
    assert {target for target, _ in factory.observed_children} == set(
        (
            *factory.provider.manifest.refs("parent_tuning"),
            *factory.provider.manifest.refs("parent_held_out"),
        )
    )
    for target, result in factory.observed_children:
        assert result.accepted and result.compile_result.report.floor_score == 1
        assert result.compile_result.stages == (0, 1, 2, 4)
        assert target.game_id not in result.compile_result.spec.model_dump_json()
    inference = [
        request.prompt
        for request in player.requests
        if "specification inference stage" in request.prompt
    ]
    assert len(inference) == 7
    for ref, _ in factory.observed_children:
        assert all(ref.game_id not in prompt and ref.revision not in prompt for prompt in inference)
    await factory.install(compiled, name=description.name)
    before = len(factory.compiler.runtime.store.rows())
    result = await factory.run(description.name)
    assert result.accepted and result.score == 1 and result.compliance == "unverified"
    assert result.score_source == "local_fixture" and len(result.children) == 3
    assert len({child.compile_result.report.verification_id for child in result.children}) == 3
    assert all(
        child.compile_result.stages == (0, 1, 2, 4) and child.play_result is not None
        for child in result.children
    )
    rows = factory.compiler.runtime.store.rows()[before:]
    assert result.tokens_in == sum(row.tokens_in for row in rows)
    assert result.tokens_out == sum(row.tokens_out for row in rows)
    assert result.cost_usd == sum(row.cost_usd for row in rows)
    assert result.tokens_in == sum(child.tokens_in for child in result.children)


async def test_parent_floor_reads_child_losses_not_successful_child_licenses(tmp_path, monkeypatch):
    class SupportOnlyPlayer(FixtureAdapter):
        @staticmethod
        def choose(grid):
            # Deliberately weak offline policy: refuses left-side visible navigation goals.
            # This is a rule over public pixels, not a manifest/engine lookup.
            if any(2 in row for row in grid) and any(grid[5][x] == 3 for x in (1, 2)):
                return "ACTION1"
            return FixtureAdapter.choose(grid)

    factory, description, _ = competition_fixture(tmp_path, monkeypatch, SupportOnlyPlayer())
    proposal = await factory.compile(description)
    result = await factory.compile(description, approved=True, approved_spec=proposal.spec)
    assert result.report.floor_score == pytest.approx(2 / 3)
    assert result.artifact is not None and not result.artifact.licensed
    children = [child for _, child in factory.observed_children]
    assert len(children) == 6 and all(child.accepted for child in children)
    assert all(child.compile_result.report.floor_score == 1 for child in children)
    assert sum(child.score.normalized == 0 for child in children) == 2
    with pytest.raises(RegistryError):
        await factory.install(result, name="losing-parent")
    assert factory.registry.list() == ()


def scripted_official_factory(tmp_path, monkeypatch, raw_score):
    """Official provider/score decoder with OFFLINE SCRIPTED transport, not SDK evidence."""
    import architect.games.official as official

    refs = tuple(
        GameRef(backend="official_offline", game_id=f"audit{i}-v1", revision="v1") for i in range(3)
    )
    manifest = OfficialPreviewManifest(
        dataset_id="scripted-independent-wire-fixture",
        dataset_revision="v1",
        support_tuning=(refs[0],),
        support_held_out=(refs[1],),
        delivery=(refs[2],),
    )
    operations = []

    class ScriptedTransport:
        def __init__(self, config, deadline):
            self.deadline = deadline
            self.target = None
            self.closed = False

        async def request(self, request, maximum=2_097_152):
            assert not self.closed
            operations.append(request)
            if request["op"] == "init":
                return [ref.model_dump(mode="json") for ref in manifest_refs(manifest)]
            if request["op"] == "make":
                assert self.target is None
                self.target = request["game_id"]
            elif request["op"] == "finalize":
                assert self.target is not None
                return {"card_score": raw_score, "game_scores": {self.target: raw_score}}
            elif request["op"] != "step":
                raise AssertionError("Unexpected transport operation")
            won = request["op"] == "step"
            return {
                "frames": [[[2, 3], [0, 0]]],
                "state": "WIN" if won else "NOT_FINISHED",
                "available_actions": ["RESET"] if won else ["ACTION1"],
                "levels_completed": int(won),
                "win_levels": 1,
            }

        async def close(self):
            self.closed = True

    class SingleActionPlayer(FixtureAdapter):
        @staticmethod
        def choose(grid):
            return "ACTION1"

    monkeypatch.setattr(official, "_WorkerProcess", ScriptedTransport)
    base, player = factory_fixture(tmp_path, SingleActionPlayer())
    config = base.config.model_copy(
        update={"limits": GameLimits(max_actions=8, max_turns=8, max_probe_actions=0)}
    )
    provider = OfficialGameProvider(OfficialConfig(mode="offline", staged_game_dir=tmp_path))
    return GameFactory(base.compiler, provider, config, manifest=manifest), operations, player


@pytest.mark.parametrize("raw, expected", [(100, 1), (25, 0.25)])
async def test_scripted_official_wire_licenses_only_completed_card_score(
    tmp_path, monkeypatch, raw, expected
):
    factory, operations, player = scripted_official_factory(tmp_path, monkeypatch, raw)
    try:
        result = await factory.run(
            factory.manifest.refs("delivery")[0], support_dataset="scripted-official-support"
        )
        assert result.compile_result.report.floor_score == expected
        assert result.accepted is (raw == 100)
        assert any(op["op"] == "step" for op in operations)
        finals = [op for op in operations if op["op"] == "finalize"]
        assert len(finals) == (3 if raw == 100 else 2)
        policy = result.compile_result.spec.execution_config
        assert policy["score_source"] == "official"
        assert policy["score_policy"] == "official_percent_v1"
        if raw == 100:
            assert result.score.raw == 100 and result.score.normalized == 1
            assert result.score.source == "official"
            assert result.compile_result.stages == (0, 1, 2, 4)
        else:
            assert result.play_result is None  # Public WIN cannot substitute for card score.
        assert player.requests and all(r.max_output_tokens is None for r in player.requests)
    finally:
        await factory.provider.close()


@pytest.mark.parametrize("raw", [115, True, "100", None])
async def test_scripted_official_unknown_score_scale_never_binary_fallback(
    tmp_path, monkeypatch, raw
):
    factory, operations, _ = scripted_official_factory(tmp_path, monkeypatch, raw)
    try:
        result = await factory.run(
            factory.manifest.refs("delivery")[0], support_dataset="scripted-official-support"
        )
        assert not result.accepted and result.play_result is None
        assert result.compile_result.report.floor_score == 0
        assert any(op["op"] == "finalize" for op in operations)
    finally:
        await factory.provider.close()


async def test_gateway_not_discovered_or_tuned_under_official_manifest(tmp_path, monkeypatch):
    factory, operations, player = scripted_official_factory(tmp_path, monkeypatch, 100)
    await factory.provider.close()
    factory.provider = OfficialGameProvider(
        OfficialConfig(mode="gateway", gateway_url="http://127.0.0.1:1234")
    )
    try:
        result = await factory.run(
            factory.manifest.refs("delivery")[0], support_dataset="scripted-official-support"
        )
        assert not result.accepted and result.failures == ("game_unavailable",)
        assert not operations and not player.requests
    finally:
        await factory.provider.close()


def test_official_revision_alias_cannot_cross_manifest_partition():
    first = GameRef(backend="official_offline", game_id="same-v1", revision="v1")
    alias = GameRef(backend="official_offline", game_id="same-v2", revision="v2")
    delivery = GameRef(backend="official_offline", game_id="other-v1", revision="v1")
    with pytest.raises(ValidationError, match="official_preview_identity_overlap"):
        OfficialPreviewManifest(
            dataset_id="scripted-only",
            dataset_revision="v1",
            support_tuning=(first,),
            support_held_out=(alias,),
            delivery=(delivery,),
        )
