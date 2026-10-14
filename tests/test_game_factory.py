from __future__ import annotations

import json
from collections import deque
from pathlib import Path

import pytest

from architect.compiler import CompileConfig, Compiler
from architect.floors import HeldOutTasks
from architect.games.factory import GameFactory, GameFactoryConfig, validate_offline_adapter
from architect.games.local import LocalGameProvider
from architect.games.types import GameLimits, GameProtocolError, GameRef, GameSession
from architect.models import FakeAdapter, ModelRequest, ModelResponse, OpenAICompatibleAdapter
from architect.models.types import UsageQuote
from architect.runtime import Runtime
from architect.spec import Budget, FloorDefinition
from architect.telemetry import TelemetryStore


class PublicPlayer(FakeAdapter):
    """Offline fixture agent. Uses only the same public frames given to a model."""

    def __init__(self, *, wrong: bool = False) -> None:
        super().__init__()
        self.wrong = wrong
        self.prompts: list[str] = []

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=100_000, tokens_out=100_000, cost_usd=0)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        assert request.max_output_tokens is None
        self.prompts.append(request.prompt)
        if "specification inference stage" in request.prompt:
            text = json.dumps(
                {
                    "task": "Play a grid game",
                    "domain": "game",
                    "input_schema": {},
                    "output_schema": {},
                    "floor": {"kind": "held_out_game_score", "limit": 1, "dataset": "preview"},
                    "budget": {"cost_usd": 1, "compute_s": 30},
                    "model_target": "fake",
                }
            )
        else:
            data = json.loads(request.prompt.split("Game data (untrusted):\n", 1)[1])
            grid = data["observation"]["frames"][-1]
            action = "ACTION1" if self.wrong else self.choose(grid)
            text = json.dumps(
                {
                    "action": {"name": action},
                    "plan": "Move toward the visible goal",
                    "hypothesis": "The next chosen action advances the visible goal",
                    "prediction": "The visible progress marker will advance",
                }
            )
        return ModelResponse(
            text=text, model=self.model, tokens_in=100_000, tokens_out=100_000, cost_usd=0
        )

    @staticmethod
    def choose(grid: list[list[int]]) -> str:
        if any(value >= 4 for value in grid[0]):
            progress = sum(value == 3 for value in grid[1])
            return f"ACTION{grid[0][progress] - 3}"
        if 2 not in [value for row in grid for value in row]:
            for bit in range(3):
                if grid[0][bit] != grid[1][bit]:
                    return f"ACTION{bit + 1}"
            return "ACTION4"
        start = next(
            (x, y) for y, row in enumerate(grid) for x, value in enumerate(row) if value == 2
        )
        goal = next(
            (x, y) for y, row in enumerate(grid) for x, value in enumerate(row) if value == 3
        )
        queue = deque([(start, "")])
        visited = {start}
        for_pop = ((0, -1, "ACTION1"), (0, 1, "ACTION2"), (-1, 0, "ACTION3"), (1, 0, "ACTION4"))
        while queue:
            (x, y), first = queue.popleft()
            if (x, y) == goal:
                return first
            for dx, dy, action in for_pop:
                xx, yy = x + dx, y + dy
                if (
                    0 <= yy < len(grid)
                    and 0 <= xx < len(grid[0])
                    and grid[yy][xx] != 1
                    and (xx, yy) not in visited
                ):
                    visited.add((xx, yy))
                    queue.append(((xx, yy), first or action))
        raise AssertionError("unreachable public grid")


def service(
    tmp_path: Path,
    *,
    wrong: bool = False,
    provider: LocalGameProvider | None = None,
    **config_values: object,
) -> GameFactory:
    player = PublicPlayer(wrong=wrong)

    def unused_floor(definition: FloorDefinition) -> HeldOutTasks:
        raise AssertionError("Factory must replace the caller floor safely")

    compiler = Compiler(
        tmp_path / "artifacts",
        Runtime(TelemetryStore(tmp_path / "runs")),
        lambda model: player,
        unused_floor,
    )
    values: dict[str, object] = {
        "budget": Budget(cost_usd=1, compute_s=30),
        "compile_config": CompileConfig(enable_rewriting=False, max_rewrites=0, max_candidates=1),
        "small_model": "fake",
        "limits": GameLimits(max_actions=24, max_turns=24),
    }
    values.update(config_values)
    return GameFactory(
        compiler, provider or LocalGameProvider(), GameFactoryConfig.model_validate(values)
    )


@pytest.mark.parametrize("index", [0, 1, 2])
async def test_three_families_compile_real_rollouts_and_play(tmp_path: Path, index: int) -> None:
    factory = service(tmp_path)
    provider = factory.provider
    assert isinstance(provider, LocalGameProvider)
    original = factory.compiler.config.model_dump()
    target = provider.manifest.refs("delivery")[index]
    result = await factory.run(target, support_dataset="support-v1")
    assert result.accepted, result
    assert result.score is not None and result.score.normalized == 1
    assert result.compile_result is not None
    assert result.compile_result.stages == (0, 1, 2, 4)
    assert result.compile_result.report is not None
    assert result.compile_result.report.floor_score == 1
    assert result.play_result is not None and result.actions_used > 0
    assert result.tokens_in > 100_000 and result.tokens_out > 100_000
    assert result.cost_usd == 0
    assert factory.compiler.config.model_dump() == original
    assert factory.compiler.config.enable_rewriting
    rows = factory.compiler.runtime.store.rows()
    assert result.tokens_in == sum(row.tokens_in for row in rows)
    assert result.tokens_out == sum(row.tokens_out for row in rows)
    assert all("frames" not in row.model_dump_json() for row in rows)
    artifact = result.compile_result.artifact
    assert artifact is not None and not (artifact.directory / "harness.py").exists()


async def test_wrong_actions_fail_actual_floor_without_delivery(tmp_path: Path) -> None:
    factory = service(tmp_path, wrong=True)
    provider = factory.provider
    assert isinstance(provider, LocalGameProvider)
    result = await factory.run(provider.manifest.refs("delivery")[0], support_dataset="support")
    assert not result.accepted and result.play_result is None
    assert result.compile_result is not None and result.compile_result.report is not None
    assert result.compile_result.report.floor_score == 0
    assert result.tokens_in > 0


async def test_isolated_child_never_probes_target_or_writes_library(tmp_path: Path) -> None:
    factory = service(tmp_path)
    provider = factory.provider
    assert isinstance(provider, LocalGameProvider)
    target = provider.manifest.refs("parent_held_out")[2]
    rejected = await factory.run(target, support_dataset="support")
    assert not rejected.accepted and rejected.tokens_in == 0
    result = await factory.run_isolated(target, support_dataset="support")
    assert result.accepted
    player = factory.compiler.adapter_factory("fake")
    assert isinstance(player, PublicPlayer)
    inference = next(
        prompt for prompt in player.prompts if "specification inference stage" in prompt
    )
    assert target.game_id not in inference and target.revision not in inference
    assert result.compile_result is not None
    assert target.game_id not in result.compile_result.spec.model_dump_json()
    library_dir = tmp_path / "library"
    assert not library_dir.exists() or not list(library_dir.rglob("harness.py"))


def test_runtime_policy_refuses_cloud_and_rewrites(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="rewriting"):
        service(tmp_path, compile_config=CompileConfig())
    with pytest.raises(GameProtocolError, match="offline"):
        validate_offline_adapter(OpenAICompatibleAdapter(), "gpt-4o-mini")
    validate_offline_adapter(
        OpenAICompatibleAdapter(base_url="http://127.0.0.1:1234/v1"), "gpt-4o-mini"
    )


async def test_strategy_hypotheses_and_feedback(tmp_path: Path) -> None:
    factory = service(tmp_path, strategy="hypothesis_tester")
    provider = factory.provider
    assert isinstance(provider, LocalGameProvider)
    result = await factory.run(provider.manifest.refs("delivery")[0], support_dataset="support")
    assert result.accepted, result
    player = factory.compiler.adapter_factory("fake")
    assert isinstance(player, PublicPlayer)
    turns = [p for p in player.prompts if "Game data (untrusted):" in p]
    assert any('"previous_test": {' in p and '"prediction":' in p for p in turns)


async def test_cached_games_get_fresh_verification(tmp_path: Path) -> None:
    factory = service(tmp_path)
    provider = factory.provider
    assert isinstance(provider, LocalGameProvider)
    target = provider.manifest.refs("delivery")[0]
    first = await factory.run(target, support_dataset="support")
    second = await factory.run(target, support_dataset="support")
    assert first.accepted and second.accepted
    assert first.compile_result is not None and second.compile_result is not None
    assert second.compile_result.warm_started
    assert first.compile_result.report is not None and second.compile_result.report is not None
    assert (
        first.compile_result.report.verification_id != second.compile_result.report.verification_id
    )
    assert second.actions_used > 0


async def test_budget_starts_before_provider_setup(tmp_path: Path) -> None:
    import asyncio

    factory = service(tmp_path, budget=Budget(cost_usd=1, compute_s=0.03))

    class SlowProvider(LocalGameProvider):
        async def open(self, ref: GameRef, *, limits: GameLimits) -> GameSession:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    factory.provider = SlowProvider()
    target = factory.provider.manifest.refs("delivery")[0]
    result = await factory.run(target, support_dataset="support")
    assert not result.accepted and result.failures == ("game_budget_exceeded",)
    assert result.tokens_in == result.tokens_out == 0
    assert result.wall_time_s < 1


async def test_failed_model_reservation_settles_usage(tmp_path: Path) -> None:
    factory = service(tmp_path)

    class FailedPlayer(PublicPlayer):
        def quote(self, request: ModelRequest) -> UsageQuote:
            return UsageQuote(tokens_in=300, tokens_out=900, cost_usd=0.02)

        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise RuntimeError("SECRET provider failure")

    factory.compiler.adapter_factory = lambda model: FailedPlayer()
    provider = factory.provider
    assert isinstance(provider, LocalGameProvider)
    result = await factory.run(provider.manifest.refs("delivery")[0], support_dataset="support")
    assert not result.accepted
    assert result.tokens_in == 300 and result.tokens_out == 900 and result.cost_usd == 0.02
    assert "SECRET" not in result.model_dump_json()
    assert result.actions_used == 3


async def test_inference_denial_does_not_fabricate_spending(tmp_path: Path) -> None:
    factory = service(tmp_path, budget=Budget(cost_usd=0.001, compute_s=10))

    class ExpensivePlayer(PublicPlayer):
        def quote(self, request: ModelRequest) -> UsageQuote:
            return UsageQuote(tokens_in=300, tokens_out=900, cost_usd=0.02)

    factory.compiler.adapter_factory = lambda model: ExpensivePlayer()
    provider = factory.provider
    assert isinstance(provider, LocalGameProvider)
    result = await factory.run(provider.manifest.refs("delivery")[0], support_dataset="support")
    assert result.failures == ("game_budget_exceeded",)
    assert result.cost_usd == 0 and result.tokens_in == result.tokens_out == 0
    assert result.actions_used == 3


async def test_support_targets_cannot_be_delivery(tmp_path: Path) -> None:
    factory = service(tmp_path)
    provider = factory.provider
    assert isinstance(provider, LocalGameProvider)
    for role in ("support_tuning", "support_held_out"):
        result = await factory.run_isolated(
            provider.manifest.refs(role)[0], support_dataset="support"
        )
        assert not result.accepted and result.tokens_in == 0 and result.actions_used == 0


class ScriptedOfficialPlayer(PublicPlayer):
    """Scripted public-observation model, not a benchmark agent."""

    @staticmethod
    def choose(grid: list[list[int]]) -> str:
        return "ACTION1"


def official_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    raw_score: float = 100,
) -> tuple[GameFactory, list[dict[str, object]]]:
    """Actual OfficialGameProvider with a scripted transport, no live ARC result."""
    from architect.games import official
    from architect.games.evaluation import OfficialPreviewManifest, manifest_refs

    refs = tuple(
        GameRef(backend="official_offline", game_id=f"fixture{i}-v1", revision="v1")
        for i in range(5)
    )
    manifest = OfficialPreviewManifest(
        dataset_id="scripted-official-wire-only",
        dataset_revision="fixture-v1",
        support_tuning=(refs[0],),
        support_held_out=(refs[1],),
        delivery=(refs[2],),
        parent_tuning=(refs[3],),
        parent_held_out=(refs[4],),
    )
    calls: list[dict[str, object]] = []

    class ScriptedWorker:
        def __init__(self, config: object, deadline: float | None) -> None:
            self.deadline = deadline
            self.target: str | None = None
            self.closed = False

        async def request(self, request: dict[str, object], maximum: int = 2_097_152) -> object:
            assert not self.closed
            calls.append(request)
            if request["op"] == "init":
                return [ref.model_dump(mode="json") for ref in manifest_refs(manifest)]
            if request["op"] == "finalize":
                assert self.target is not None
                return {"card_score": raw_score, "game_scores": {self.target: raw_score}}
            if request["op"] == "make":
                assert self.target is None
                self.target = str(request["game_id"])
            won = request["op"] == "step"
            return {
                "frames": [[[2, 3], [0, 0]]],
                "state": "WIN" if won else "NOT_FINISHED",
                "available_actions": ["RESET"] if won else ["ACTION1"],
                "levels_completed": int(won),
                "win_levels": 1,
            }

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(official, "_WorkerProcess", ScriptedWorker)
    base = service(tmp_path, limits=GameLimits(max_actions=8, max_turns=8, max_probe_actions=0))
    base.compiler.adapter_factory = lambda model: ScriptedOfficialPlayer()
    provider = official.OfficialGameProvider(
        official.OfficialConfig(mode="offline", staged_game_dir=tmp_path)
    )
    return GameFactory(base.compiler, provider, base.config, manifest=manifest), calls


async def test_official_offline_factory_real_provider_scripted_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, calls = official_service(tmp_path, monkeypatch)
    assert factory.manifest is not None
    result = await factory.run(
        factory.manifest.refs("delivery")[0], support_dataset="official-public"
    )
    assert result.accepted, result
    assert result.score is not None and result.score.source == "official"
    assert result.score.raw == 100 and result.score.normalized == 1
    assert result.score.metric == "relative_human_action_efficiency.percent.v1"
    assert result.compile_result is not None and result.compile_result.report is not None
    assert result.compile_result.stages == (0, 1, 2, 4)
    assert result.compile_result.report.floor_score == 1
    policy = result.compile_result.spec.execution_config
    assert policy["score_source"] == "official" and policy["score_policy"] == "official_percent_v1"
    assert len([call for call in calls if call["op"] == "step"]) == 3
    assert len([call for call in calls if call["op"] == "finalize"]) == 3


async def test_official_score_is_actual_completed_card_not_public_win(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, calls = official_service(tmp_path, monkeypatch, raw_score=25)
    assert factory.manifest is not None
    result = await factory.run(
        factory.manifest.refs("delivery")[0], support_dataset="official-public"
    )
    assert not result.accepted and result.play_result is None
    assert result.compile_result is not None and result.compile_result.report is not None
    assert result.compile_result.report.floor_score == 0.25
    assert len([call for call in calls if call["op"] == "finalize"]) == 2


async def test_official_isolated_compile_uses_only_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, calls = official_service(tmp_path, monkeypatch)
    assert factory.manifest is not None
    target = factory.manifest.refs("parent_held_out")[0]
    result = await factory.run_isolated(target, support_dataset="official-public")
    assert result.accepted, result
    makes = [call["game_id"] for call in calls if call["op"] == "make"]
    assert makes[-1] == target.game_id and target.game_id not in makes[:-1]
    assert result.compile_result is not None
    assert target.game_id not in result.compile_result.spec.model_dump_json()


def test_official_manifest_rejects_alias_revisions_gateway_and_overlap() -> None:
    from architect.games.evaluation import OfficialPreviewManifest

    base = {
        "dataset_id": "fixture-only",
        "dataset_revision": "v1",
        "support_tuning": [{"backend": "official_offline", "game_id": "a-v1", "revision": "v1"}],
        "support_held_out": [{"backend": "official_offline", "game_id": "b-v1", "revision": "v1"}],
        "delivery": [{"backend": "official_offline", "game_id": "c-v1", "revision": "v1"}],
    }
    assert OfficialPreviewManifest.model_validate(base).refs("delivery")
    for changed in (
        {"backend": "official_offline", "game_id": "a-v1", "revision": "fake-alias"},
        {"backend": "official_gateway", "game_id": "c-v1", "revision": "v1"},
        {"backend": "official_offline", "game_id": "c-v1", "revision": "latest"},
        {"backend": "official_offline", "game_id": "a-v2", "revision": "v2"},
    ):
        with pytest.raises(ValueError):
            OfficialPreviewManifest.model_validate({**base, "delivery": [changed]})


async def test_official_discovery_mismatch_stops_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, calls = official_service(tmp_path, monkeypatch)
    assert factory.manifest is not None
    original = factory.manifest
    unseen = GameRef(backend="official_offline", game_id="unseen-v1", revision="v1")
    factory.manifest = original.model_copy(update={"delivery": (unseen,)})
    result = await factory.run(unseen, support_dataset="official-public")
    assert not result.accepted and result.failures == ("game_unavailable",)
    assert all(call["op"] == "init" for call in calls)
    assert result.tokens_in == result.actions_used == 0


async def test_official_gateway_is_not_discovered_for_tuning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from architect.games.official import OfficialConfig, OfficialGameProvider

    factory, calls = official_service(tmp_path, monkeypatch)
    assert factory.manifest is not None
    factory.provider = OfficialGameProvider(
        OfficialConfig(mode="gateway", gateway_url="http://127.0.0.1:1234")
    )
    result = await factory.run(
        factory.manifest.refs("delivery")[0], support_dataset="official-public"
    )
    assert not result.accepted and result.failures == ("game_unavailable",)
    assert not calls


async def test_explicit_official_manifest_requires_real_staged_files(tmp_path: Path) -> None:
    from architect.games.evaluation import OfficialPreviewManifest
    from architect.games.official import OfficialConfig, OfficialGameProvider

    refs = tuple(
        GameRef(backend="official_offline", game_id=f"missing{i}-v1", revision="v1")
        for i in range(3)
    )
    manifest = OfficialPreviewManifest(
        dataset_id="operator-staged-public",
        dataset_revision="v1",
        support_tuning=(refs[0],),
        support_held_out=(refs[1],),
        delivery=(refs[2],),
    )
    base = service(tmp_path)
    provider = OfficialGameProvider(
        OfficialConfig(mode="offline", staged_game_dir=tmp_path / "missing-public-files")
    )
    factory = GameFactory(base.compiler, provider, base.config, manifest=manifest)
    result = await factory.run(refs[2], support_dataset="official-public")
    assert not result.accepted and result.failures == ("game_unavailable",)
    assert result.tokens_in == result.actions_used == 0
    assert provider._worker.process is None
    await provider.close()


async def test_expired_factory_reaps_official_probe_episode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scripted OFFLINE transport models the await needed to reap an SDK worker."""
    import asyncio

    factory, _ = official_service(tmp_path, monkeypatch)
    assert factory.manifest is not None
    worker_type = type(factory.provider._worker)
    original_close = worker_type.close

    async def reaping_close(worker: object) -> None:
        resumed = asyncio.Event()
        asyncio.get_running_loop().call_soon(resumed.set)
        await resumed.wait()  # Real process.wait() also requires an event-loop handoff.
        await original_close(worker)

    monkeypatch.setattr(worker_type, "close", reaping_close)

    class BlockedInference(ScriptedOfficialPlayer):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    factory.compiler.adapter_factory = lambda model: BlockedInference()
    factory.config = factory.config.model_copy(
        update={"budget": Budget(cost_usd=1, compute_s=0.03)}
    )
    result = await factory.run(
        factory.manifest.refs("delivery")[0], support_dataset="official-public"
    )
    assert not result.accepted and result.failures == ("game_budget_exceeded",)
    assert not factory.provider._episodes, "Cleanup must reap and detach even after work expires"
    await factory.provider.close()


async def test_host_game_overrides_replace_hostile_nested_inference_keys(tmp_path: Path) -> None:
    class HostileInference(PublicPlayer):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            response = await super().complete(request)
            if "specification inference stage" in request.prompt:
                proposed = json.loads(response.text)
                proposed["execution_config"] = {"model_extra": "not-authority"}
                proposed["output_schema"] = {"type": "object", "required": ["model_extra"]}
                return response.model_copy(update={"text": json.dumps(proposed)})
            return response

    factory = service(tmp_path)
    factory.compiler.adapter_factory = lambda model: HostileInference()
    provider = factory.provider
    assert isinstance(provider, LocalGameProvider)
    result = await factory.run(provider.manifest.refs("delivery")[0], support_dataset="support")
    # Strict inference parsing happens first. Valid model proposals cannot add
    # keys to these host-owned policy/schema fields before tuning or licensing.
    assert result.accepted and result.play_result is not None
    assert result.compile_result is not None
    assert "model_extra" not in result.compile_result.spec.model_dump_json()


async def test_exactly_spent_parent_dollars_still_allow_free_game_work(tmp_path: Path) -> None:
    from architect.runtime.wallet import compilation_wallet

    factory = service(tmp_path)
    provider = factory.provider
    assert isinstance(provider, LocalGameProvider)
    with compilation_wallet(Budget(cost_usd=1, compute_s=30)) as parent:
        parent.reserve(UsageQuote(tokens_in=0, tokens_out=0, cost_usd=1))
        assert not parent.exhausted
        result = await factory.run(provider.manifest.refs("delivery")[0], support_dataset="support")
        assert result.accepted and result.cost_usd == 0
        assert parent.cost_usd == 1


async def test_official_discovery_rejects_extra_revision_of_same_base_game(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, calls = official_service(tmp_path, monkeypatch)
    assert factory.manifest is not None
    discover = factory.provider.discover
    pinned = factory.manifest.refs("support_tuning")[0]
    alias = GameRef(
        backend="official_offline",
        game_id=pinned.game_id.split("-", 1)[0] + "-v2",
        revision="v2",
    )

    async def ambiguous_catalog() -> tuple[GameRef, ...]:
        return (*await discover(), alias)

    monkeypatch.setattr(factory.provider, "discover", ambiguous_catalog)
    result = await factory.run(
        factory.manifest.refs("delivery")[0], support_dataset="official-public"
    )
    assert not result.accepted and result.failures == ("game_unavailable",)
    assert result.tokens_in == result.actions_used == 0
    assert all(call["op"] == "init" for call in calls)


async def test_ancestor_exhausted_after_inference_reports_budget_not_compile(
    tmp_path: Path,
) -> None:
    from architect.runtime.wallet import compilation_wallet

    quotes: list[str] = []

    class PricedPlay(PublicPlayer):
        def quote(self, request: ModelRequest) -> UsageQuote:
            inference = "specification inference stage" in request.prompt
            quotes.append("inference" if inference else "play")
            return UsageQuote(tokens_in=1, tokens_out=1, cost_usd=0 if inference else 0.02)

    factory = service(
        tmp_path,
        compile_config=CompileConfig(enable_rewriting=False, max_rewrites=0, max_candidates=4),
    )
    factory.compiler.adapter_factory = lambda model: PricedPlay()
    provider = factory.provider
    assert isinstance(provider, LocalGameProvider)
    with compilation_wallet(Budget(cost_usd=0.01, compute_s=30)) as ancestor:
        result = await factory.run(provider.manifest.refs("delivery")[0], support_dataset="support")
        assert ancestor.exhausted and ancestor.cost_usd == 0
    # Stage 0 was free; the first priced tuning call is refused and compile()
    # folds that into per-case failures. It is still budget exhaustion.
    assert result.compile_result is not None and 0 in result.compile_result.stages
    assert not result.accepted and result.failures == ("game_budget_exceeded",)
    assert result.play_result is None and result.score is None
    assert quotes == ["inference", "play"], "No model work after the wallet refused"


async def test_own_compile_budget_refusal_reports_budget_not_compile(tmp_path: Path) -> None:
    quotes: list[str] = []

    class PricedPlay(PublicPlayer):
        def quote(self, request: ModelRequest) -> UsageQuote:
            inference = "specification inference stage" in request.prompt
            quotes.append("inference" if inference else "play")
            return UsageQuote(tokens_in=1, tokens_out=1, cost_usd=0 if inference else 0.02)

    # The factory wallet ($1) has room; only compile()'s own $0.01 wallet refuses.
    factory = service(
        tmp_path,
        compile_config=CompileConfig(
            enable_rewriting=False,
            max_rewrites=0,
            max_candidates=4,
            compile_budget=Budget(cost_usd=0.01, compute_s=30),
        ),
    )
    factory.compiler.adapter_factory = lambda model: PricedPlay()
    provider = factory.provider
    assert isinstance(provider, LocalGameProvider)
    result = await factory.run(provider.manifest.refs("delivery")[0], support_dataset="support")
    assert result.compile_result is not None and 0 in result.compile_result.stages
    assert "compile_budget" in result.compile_result.failures
    assert not result.accepted and result.failures == ("game_budget_exceeded",)
    assert result.play_result is None and result.score is None and result.cost_usd == 0
    assert quotes == ["inference", "play"], "No model work after the wallet refused"


async def test_genuine_floor_failure_is_not_reported_as_compile_budget(tmp_path: Path) -> None:
    factory = service(tmp_path, wrong=True)
    provider = factory.provider
    assert isinstance(provider, LocalGameProvider)
    result = await factory.run(provider.manifest.refs("delivery")[0], support_dataset="support")
    assert result.compile_result is not None
    assert "compile_budget" not in result.compile_result.failures
    assert not result.accepted and result.failures == ("game_compile_failed",)


class AnimatedProvider(LocalGameProvider):
    """Local games whose observations are 16-frame 64x64 animations, like public ARC games."""

    async def open(self, ref: GameRef, *, limits: GameLimits) -> GameSession:
        from test_game_local import AnimatedSession

        return AnimatedSession(await super().open(ref, limits=limits), frames=16, size=64)


async def test_animated_public_observations_still_license(tmp_path: Path) -> None:
    """Regression: a support game answering with animation frames (sb26 answers
    ACTION5 with 42 frames) made the Stage 0 probe exceed the inference context,
    so every child compile died as game_execution_failed."""
    from architect.compiler.inference import MAX_CONTEXT_CHARS

    factory = service(tmp_path, provider=AnimatedProvider())
    provider = factory.provider
    assert isinstance(provider, LocalGameProvider)
    result = await factory.run_isolated(
        provider.manifest.refs("parent_tuning")[0], support_dataset="support-v1"
    )
    assert result.accepted, result.failures
    player = factory.compiler.adapter_factory("fake")
    assert isinstance(player, PublicPlayer)
    inference = [p for p in player.prompts if "specification inference stage" in p]
    assert len(inference) == 1
    data = json.loads(inference[0].split("Request data (untrusted):\n", 1)[1])
    assert len(json.dumps(data)) <= MAX_CONTEXT_CHARS
    observations = data["source"]["observations"]
    assert len(observations) == 4 and all(len(o["frames"]) == 1 for o in observations)
    assert data["source"]["actions"] and data["user_fields"]["domain"] == "game"
    # Play sees the settled grid and how many animation frames produced it.
    plays = [p for p in player.prompts if "Game data (untrusted):" in p]
    assert plays
    for prompt in plays:
        seen = json.loads(prompt.split("Game data (untrusted):\n", 1)[1])["observation"]
        assert len(seen["frames"]) == 1 and seen["frame_count"] == 16


async def test_inference_failure_surfaces_a_fixed_compile_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from architect.compiler import inference
    from architect.games.competition import child_failure_codes

    async def broken(*args: object, **kwargs: object) -> object:
        raise inference.InferenceError("Specification inference failed")

    monkeypatch.setattr(inference, "infer_spec", broken)
    factory = service(tmp_path)
    provider = factory.provider
    assert isinstance(provider, LocalGameProvider)
    result = await factory.run(provider.manifest.refs("delivery")[0], support_dataset="support")
    assert not result.accepted and result.compile_result is None
    assert result.failures == ("game_compile_failed", "game_inference_failed")
    assert child_failure_codes([result]) == (
        "child_game_compile_failed",
        "child_game_inference_failed",
    )


# ---------------------------------------------------------------------------
# Phase 9: allow_forge lifts game_sandbox_forbidden for licensed Forge artifacts only.
# ---------------------------------------------------------------------------


def _forge_like(artifact: object, *, forge: bool) -> object:
    from architect._authority import _sign_report
    from architect.spec import ForgeBinding, HarnessArtifact, TestReport

    assert isinstance(artifact, HarnessArtifact)
    converted = artifact.model_copy(
        update={
            "execution_mode": "sandbox",
            "forge": ForgeBinding(call_limit=48, fuel=100_000) if forge else None,
            "report": None,
        }
    )
    report = TestReport(
        spec_hash=converted.spec.spec_hash,
        artifact_digest=converted.artifact_digest,
        passed=True,
        floor_score=1,
        floor_limit=converted.spec.floor.limit,
        floor_kind=converted.spec.floor.kind,
        tokens=1,
        cost_usd=0,
        compute_s=0,
        component_count=converted.component_count,
        verification_id="forge-like",
    )
    signature = _sign_report(report.model_dump(mode="json", exclude={"signature"}))
    signed = converted.model_copy(
        update={"report": report.model_copy(update={"signature": signature})}
    )
    signed.save()
    assert signed.licensed
    return signed


def _sandboxed_compiles(monkeypatch: pytest.MonkeyPatch, *, forge: bool) -> None:
    original = Compiler.compile

    async def compile_then_convert(self: Compiler, *args: object, **kwargs: object) -> object:
        result = await original(self, *args, **kwargs)  # type: ignore[arg-type]
        assert result.artifact is not None and result.artifact.licensed
        return result.model_copy(update={"artifact": _forge_like(result.artifact, forge=forge)})

    monkeypatch.setattr(Compiler, "compile", compile_then_convert)


def test_allow_forge_is_a_strict_default_off_host_opt_in(tmp_path: Path) -> None:
    factory = service(tmp_path)
    assert factory.config.allow_forge is False
    assert service(tmp_path, allow_forge=True).config.allow_forge is True
    with pytest.raises(ValueError):
        service(tmp_path, allow_forge=1)
    with pytest.raises(ValueError, match="rewriting"):
        service(tmp_path, allow_forge=True, compile_config=CompileConfig())


def test_delivery_refusal_rules(tmp_path: Path) -> None:
    from architect.games.factory import delivery_refusal
    from architect.spec import FloorKind, ForgeBinding, HarnessArtifact, Spec

    spec = Spec(
        task="Play",
        input_schema={},
        output_schema={},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_GAME_SCORE, limit=1, dataset="s"),
        budget=Budget(),
    )
    native = HarnessArtifact(
        spec=spec, preset="explorer", model="fake", components=(), directory=tmp_path
    )
    sandbox = native.model_copy(update={"execution_mode": "sandbox"})
    forge = sandbox.model_copy(update={"forge": ForgeBinding(call_limit=8, fuel=100_000)})
    assert delivery_refusal(native, allow_forge=False) is None
    assert delivery_refusal(native, allow_forge=True) is None
    assert delivery_refusal(sandbox, allow_forge=False) == "game_sandbox_forbidden"
    assert delivery_refusal(sandbox, allow_forge=True) == "game_sandbox_forbidden"
    assert delivery_refusal(forge, allow_forge=False) == "game_sandbox_forbidden"
    assert delivery_refusal(forge, allow_forge=True) is None


@pytest.mark.parametrize("allow_forge,forge", [(False, False), (False, True), (True, False)])
async def test_sandbox_delivery_is_refused_without_forge_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_forge: bool, forge: bool
) -> None:
    _sandboxed_compiles(monkeypatch, forge=forge)
    factory = service(tmp_path, allow_forge=allow_forge)
    provider = factory.provider
    assert isinstance(provider, LocalGameProvider)
    result = await factory.run(provider.manifest.refs("delivery")[0], support_dataset="support")
    assert result.failures == ("game_sandbox_forbidden",)
    assert not result.accepted and result.play_result is None


async def test_allow_forge_lifts_the_refusal_for_a_forge_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sandboxed_compiles(monkeypatch, forge=True)
    factory = service(tmp_path, allow_forge=True)
    provider = factory.provider
    assert isinstance(provider, LocalGameProvider)
    result = await factory.run(provider.manifest.refs("delivery")[0], support_dataset="support")
    # Delivery is attempted (the converted preset source is not valid sandbox code,
    # so the play itself fails), rather than refused up front.
    assert "game_sandbox_forbidden" not in result.failures


def test_safe_library_still_rejects_sandbox_and_forge_warm_starts(tmp_path: Path) -> None:
    from architect.games.factory import _SafeLibrary
    from architect.spec import FloorKind, ForgeBinding, HarnessArtifact, Spec

    spec = Spec(
        task="Play",
        input_schema={},
        output_schema={},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_GAME_SCORE, limit=1, dataset="s"),
        budget=Budget(),
    )
    forge = HarnessArtifact(
        spec=spec,
        preset="explorer",
        model="fake",
        components=(),
        directory=tmp_path,
        execution_mode="sandbox",
        forge=ForgeBinding(call_limit=8, fuel=100_000),
    )

    class Source:
        def lookup(self, requested: Spec, model: str) -> HarnessArtifact | None:
            return forge

    with pytest.raises(GameProtocolError, match="game_sandbox_warm_start"):
        _SafeLibrary(Source()).lookup(spec, "fake")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Review: Forge never ran on a game compile (the inner Compiler had no smoke floor,
# agent or Pi config), so bench forge/template game rows were forge_did_not_run.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("allow_forge", [True, False])
async def test_game_compiles_forge_only_on_the_opt_in_with_a_support_tuning_smoke_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_forge: bool
) -> None:
    from architect.backends.pi import PiConfig
    from architect.bench.providers import PublicFramePlayer
    from architect.compiler import forge as forge_module
    from architect.compiler.types import ForgeSettings
    from architect.games import factory as factory_module
    from architect.games.evaluation import task_for

    player = PublicFramePlayer(model="fake")

    def unused_floor(definition: FloorDefinition) -> HeldOutTasks:
        raise AssertionError("Factory must replace the caller floor safely")

    parent_pi = PiConfig(denied_paths=((tmp_path / "secret").resolve(),))
    agent = FakeAdapter()  # template mode: handed through, never called
    compiler = Compiler(
        tmp_path / "artifacts",
        Runtime(TelemetryStore(tmp_path / "runs")),
        lambda model: player,
        unused_floor,
        forge_adapter=agent,
        pi_config=parent_pi,
    )
    listened: list[object] = []
    compiler.forge_listener = listened.append
    settings = ForgeSettings(enabled=True, trigger="always", mode="template")
    config = GameFactoryConfig(
        budget=Budget(cost_usd=1, compute_s=120),
        compile_config=CompileConfig(
            enable_rewriting=False, max_rewrites=0, max_candidates=1, forge=settings
        ),
        small_model="fake",
        limits=GameLimits(max_actions=24, max_turns=24),
        allow_forge=allow_forge,
    )
    provider = LocalGameProvider()
    factory = GameFactory(compiler, provider, config)
    calls: list[dict[str, object]] = []
    original = forge_module.forge

    async def forge(*args: object, **kwargs: object) -> object:
        calls.append({"agent": args[7], "smoke": args[3], **kwargs})
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    smoke_records: list[object] = []

    class Recorded(factory_module.HeldOutGameScore):
        def __init__(self, definition: FloorDefinition, records: object, **kw: object) -> None:
            smoke_records.extend(records)  # type: ignore[call-overload]
            super().__init__(definition, records, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(forge_module, "forge", forge)
    monkeypatch.setattr(factory_module, "HeldOutGameScore", Recorded)
    manifest = provider.manifest
    result = await factory.run(manifest.refs("delivery")[0], support_dataset="support")
    compiled = result.compile_result
    assert compiled is not None
    if not allow_forge:
        # No host opt-in: Stage 3 is never entered and no smoke floor is built.
        assert 3 not in compiled.stages and calls == [] and smoke_records == []
        assert listened == []
        return
    assert 3 in compiled.stages
    [call] = calls
    # The parent's agent and effective Pi config reach the session.
    assert call["agent"] is agent and call["pi_config"] is parent_pi
    # The smoke floor holds ONLY public support-tuning games: never support
    # held-out (the child's Stage 4 set) and never a delivery target.
    tuning = [task_for(ref) for ref in manifest.refs("support_tuning")]
    forbidden = [
        task_for(ref)
        for role in ("support_held_out", "delivery", "parent_tuning", "parent_held_out")
        for ref in manifest.refs(role)  # type: ignore[arg-type]
    ]
    tasks = [record.task for record in smoke_records]  # type: ignore[attr-defined]
    assert tasks and all(task in tuning for task in tasks)
    assert not any(task in forbidden for task in tasks)
    assert call["smoke"] is not None
    # The observer copied from the parent saw the session; template mode submitted.
    [forged] = listened
    assert forged.mode == "template" and forged.submissions >= 1
    # Stage 4 licensed it (delivery then consumed the single-use artifact on disk).
    assert compiled.report is not None and compiled.report.passed
    assert compiled.artifact is not None
    assert compiled.artifact.execution_mode == "sandbox" and compiled.artifact.forge is not None
    assert result.accepted, result.failures


def test_support_smoke_floor_pairs_a_single_support_game_with_itself() -> None:
    from architect.floors import EvaluationCase, HeldOutGameScore
    from architect.games.evaluation import GameScorer, ReceiptLedger, task_for
    from architect.games.factory import support_smoke_floor
    from architect.spec import FloorKind

    definition = FloorDefinition(kind=FloorKind.HELD_OUT_GAME_SCORE, limit=1, dataset="s")
    manifest = LocalGameProvider().manifest
    tuning, held = manifest.refs("support_tuning")[0], manifest.refs("support_held_out")[0]
    ledger = ReceiptLedger()
    parent = HeldOutGameScore(
        definition,
        [
            EvaluationCase(case_id="game-0000", task=task_for(tuning), expected=None),
            EvaluationCase(case_id="game-0001", task=task_for(held), expected=None),
        ],
        environment=GameScorer(ledger),
    )
    smoke = support_smoke_floor(definition, parent, ledger)
    assert [case.task for case in smoke.tuning] == [task_for(tuning)]
    ledger.close()
