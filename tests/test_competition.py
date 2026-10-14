from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from pydantic import ValidationError

from architect.compiler import CompileResult
from architect.games.competition import CompetitionDescription, CompetitionFactory
from architect.games.factory import GameFactoryConfig
from architect.games.local import LocalGameProvider
from architect.games.types import GameLimits, GameProtocolError, GameRef, GameUnavailableError
from architect.registry import RegistryError
from architect.spec import Spec

_example_spec = importlib.util.spec_from_file_location(
    "arc_competition_example", Path(__file__).parents[1] / "examples/arc/competition.py"
)
assert _example_spec is not None and _example_spec.loader is not None
_example = importlib.util.module_from_spec(_example_spec)
_example_spec.loader.exec_module(_example)
OfflineFixtureModel = _example.OfflineFixtureModel
build_factory = _example.build_factory


def description() -> CompetitionDescription:
    return CompetitionDescription.read(
        Path(__file__).parents[1] / "examples/arc/competition.local.json"
    )


async def installed(tmp_path: Path) -> tuple[CompetitionFactory, CompileResult]:
    desc = description()
    factory = build_factory(tmp_path, desc)
    proposed = await factory.compile(desc)
    assert proposed.approval_required and proposed.artifact is None
    result = await factory.compile(desc, approved=True, approved_spec=proposed.spec)
    assert result.artifact is not None and result.artifact.licensed, result
    await factory.install(result, name=desc.name)
    return factory, result


async def test_reviewed_spec_required_no_boolean_autoapproval(tmp_path: Path) -> None:
    desc = description()
    factory = build_factory(tmp_path, desc)
    proposed = await factory.compile(desc, approved=True)
    assert proposed.approval_required and proposed.artifact is None
    assert proposed.stages == (0,)
    assert len(factory.compiler.runtime.store.rows()) == 1
    changed = proposed.spec.model_copy(update={"task": "A different task"})
    with pytest.raises(GameProtocolError, match="review"):
        await factory.compile(desc, approved=True, approved_spec=changed)
    assert len(factory.compiler.runtime.store.rows()) == 1
    changed_desc = desc.model_copy(update={"rules": "Different operator rules"})
    with pytest.raises(GameProtocolError, match="review"):
        await factory.compile(changed_desc, approved=True, approved_spec=proposed.spec)


async def test_installed_planner_runs_real_fresh_children(tmp_path: Path) -> None:
    factory, result = await installed(tmp_path)
    assert result.spec.preset_hint == "planner" and result.spec.domain == "game"
    assert result.stages == (1, 2, 4)
    assert result.report is not None and result.report.floor_score == 1
    assert result.report.held_out_count == 3
    rows_before = len(factory.compiler.runtime.store.rows())
    run = await factory.run(description().name)
    assert run.accepted and len(run.children) == 3, run
    assert run.score == 1 and run.score_source == "local_fixture"
    assert run.compliance == "unverified"
    verification_ids = []
    for child in run.children:
        assert child.accepted and child.compile_result is not None
        assert child.compile_result.report is not None and child.play_result is not None
        assert child.compile_result.stages == (0, 1, 2, 4)
        assert child.actions_used > 0
        verification_ids.append(child.compile_result.report.verification_id)
    assert len(set(verification_ids)) == 3
    rows = factory.compiler.runtime.store.rows()[rows_before:]
    assert run.tokens_in == sum(row.tokens_in for row in rows)
    assert run.tokens_out == sum(row.tokens_out for row in rows)
    assert run.cost_usd == sum(row.cost_usd for row in rows)
    assert run.tokens_in == sum(child.tokens_in for child in run.children)
    assert run.wall_time_s < 60
    assert not list((tmp_path / "library").glob("**/child-artifacts/**"))
    text = "\n".join(row.model_dump_json() for row in factory.compiler.runtime.store.rows())
    assert '"frames"' not in text and description().rules not in text
    assert result.spec.model_dump_json() not in text


async def test_parent_floor_is_actual_child_play_and_failure_blocks_install(tmp_path: Path) -> None:
    factory = build_factory(tmp_path, description())
    proposed = await factory.compile(description())
    player = factory.game_factory.compiler.adapter_factory("fake")
    assert isinstance(player, OfflineFixtureModel)
    player.wrong = True
    failed = await factory.compile(description(), approved=True, approved_spec=proposed.spec)
    assert failed.report is not None and not failed.report.passed
    assert failed.report.floor_score == 0
    assert failed.artifact is not None and not failed.artifact.licensed
    with pytest.raises(RegistryError):
        await factory.install(failed, name="bad")
    assert factory.registry.list() == ()


async def test_parent_preview_never_enters_child_inference_or_spec(tmp_path: Path) -> None:
    factory, _ = await installed(tmp_path)
    provider = factory.provider
    assert isinstance(provider, LocalGameProvider)
    player = factory.game_factory.compiler.adapter_factory("fake")
    assert isinstance(player, OfflineFixtureModel)
    inference = [p for p in player.prompts if "specification inference stage" in p]
    assert len(inference) == 7  # one parent proposal, six freshly compiled child rollouts
    for ref in (
        *provider.manifest.refs("parent_tuning"),
        *provider.manifest.refs("parent_held_out"),
    ):
        assert all(ref.game_id not in prompt and ref.revision not in prompt for prompt in inference)
    assert factory.registry.get(description().name).spec.lifecycle.value == "install"


async def test_effective_policy_drift_rejected_before_any_call(tmp_path: Path) -> None:
    factory, _ = await installed(tmp_path)
    before = len(factory.compiler.runtime.store.rows())
    payload = factory.game_factory.config.model_dump(mode="json")
    payload["limits"] = GameLimits(max_actions=23, max_turns=24).model_dump(mode="json")
    factory.game_factory.config = GameFactoryConfig.model_validate(payload)
    result = await factory.run(description().name)
    assert not result.accepted and result.tokens_in == 0
    assert len(factory.compiler.runtime.store.rows()) == before


async def test_complete_delivery_coverage_no_cherry_picking(tmp_path: Path) -> None:
    desc = description().model_copy(update={"max_games": 2})
    factory = build_factory(tmp_path, desc)
    with pytest.raises(GameProtocolError, match="coverage"):
        await factory.compile(desc)
    factory, _ = await installed(tmp_path / "installed")

    class Incomplete(LocalGameProvider):
        async def discover(self) -> tuple[GameRef, ...]:
            return self.manifest.refs("delivery")[:2]

    factory.provider = Incomplete()
    factory.game_factory.provider = factory.provider
    result = await factory.run(description().name)
    assert not result.accepted and not result.children and result.tokens_in == 0


async def test_unknown_official_provider_does_not_fall_back(tmp_path: Path) -> None:
    factory = build_factory(tmp_path, description())

    class UnavailableProvider:
        async def discover(self) -> tuple[GameRef, ...]:
            raise GameUnavailableError("official_unavailable")

        async def open(self, ref: GameRef, *, limits: GameLimits) -> None:
            raise GameUnavailableError("official_unavailable")

    provider = UnavailableProvider()
    factory.provider = provider  # type: ignore[assignment]
    factory.game_factory.provider = provider  # type: ignore[assignment]
    with pytest.raises(GameUnavailableError):
        await factory.compile(description())
    assert not factory.compiler.runtime.store.rows()


async def test_fresh_verification_recompile_and_real_registry_acceptance(tmp_path: Path) -> None:
    factory, first = await installed(tmp_path)
    fresh = await factory._recompile_spec(factory.registry.get(description().name).spec)
    assert fresh.report is not None and first.report is not None
    assert fresh.report.verification_id != first.report.verification_id
    assert fresh.licensed
    with pytest.raises(RegistryError, match="not_better"):
        factory.registry.install(description().name, fresh)
    assert factory.registry.status(description().name).revision == 1
    with pytest.raises(RegistryError, match="no_previous_revision"):
        factory.rollback(description().name)


async def test_registry_drift_invokes_fresh_compile_and_rejects_worse(tmp_path: Path) -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    factory, first = await installed(tmp_path)
    name = description().name
    artifact = factory.registry.get(name)
    # Registry's public telemetry interface accepts host-supplied observations.
    # These test inputs are synthetic drift data, not fabricated execution scores.
    row = factory.compiler.runtime.store.rows()[-1]
    drift = row.model_copy(
        update={
            "timestamp": datetime.now(UTC),
            "run_id": str(uuid4()),
            "spec_hash": artifact.spec.spec_hash,
            "harness_version": artifact.version,
            "lifecycle": artifact.spec.lifecycle,
            "floor_type": artifact.spec.floor.kind,
            "floor_limit": artifact.spec.floor.limit,
            "floor_score": 0.0,
        }
    )
    before = len(factory.compiler.runtime.store.rows())
    outcome = await factory.monitor(name, [drift])
    assert outcome.event is not None and outcome.event.reason == "floor_drift"
    assert not outcome.accepted and outcome.failure == "acceptance_rejected"
    assert len(factory.compiler.runtime.store.rows()) > before
    assert factory.registry.status(name).revision == 1
    assert first.report is not None


async def test_deadline_includes_discovery_and_failed_descendant_usage(tmp_path: Path) -> None:
    factory, _ = await installed(tmp_path)

    class Slow(LocalGameProvider):
        async def discover(self) -> tuple[GameRef, ...]:
            raise GameUnavailableError("secret response must not escape")

    factory.provider = Slow()
    factory.game_factory.provider = factory.provider
    result = await factory.run(description().name)
    assert result.failures == ("competition_unavailable",)
    assert "secret" not in result.model_dump_json()
    assert result.tokens_in == result.tokens_out == 0


@pytest.mark.parametrize(
    "update",
    [
        {"max_games": True},
        {"extra": "bad"},
        {"rules": "x" * 16_385},
        {"preview_dataset": "local-support-v1"},
    ],
)
def test_description_strict_schema(update: dict[str, object]) -> None:
    payload = description().model_dump(mode="json")
    payload.update(update)
    with pytest.raises(ValidationError):
        CompetitionDescription.model_validate(payload)


def test_description_file_byte_bound(tmp_path: Path) -> None:
    path = tmp_path / "large.json"
    path.write_bytes(b" " * 65_537)
    with pytest.raises(ValueError, match="size"):
        CompetitionDescription.read(path)


async def test_review_persists_across_factory_restart(tmp_path: Path) -> None:
    desc = description()
    first = build_factory(tmp_path, desc)
    proposed = await first.compile(desc)
    checkpoint = tmp_path / "reviewed.json"
    checkpoint.write_text(proposed.spec.model_dump_json(), encoding="utf-8")
    second = build_factory(tmp_path, desc)
    reviewed = Spec.model_validate_json(checkpoint.read_text(encoding="utf-8"))
    result = await second.compile(desc, approved=True, approved_spec=reviewed)
    assert result.spec == reviewed and result.artifact is not None and result.artifact.licensed
    await second.install(result, name=desc.name)
    third = build_factory(tmp_path, desc)
    assert (await third.run(desc.name)).accepted


async def test_parent_measures_losses_even_when_every_child_license_passes(tmp_path: Path) -> None:
    factory = build_factory(tmp_path, description())
    proposed = await factory.compile(description())
    player = factory.game_factory.compiler.adapter_factory("fake")
    assert isinstance(player, OfflineFixtureModel)
    original = player.choose

    def lose_parent_navigation(grid: list[list[int]]) -> str:
        # This fake can solve support instances but not either parent instance.
        if any(2 in row for row in grid) and any(grid[5][x] == 3 for x in (1, 2)):
            return "ACTION1"
        return original(grid)

    player.choose = lose_parent_navigation
    result = await factory.compile(description(), approved=True, approved_spec=proposed.spec)
    assert result.report is not None and result.report.floor_score == pytest.approx(2 / 3)
    assert result.artifact is not None and not result.artifact.licensed
    assert result.report.held_out_count == 3


async def test_strictly_better_recompile_installs_then_real_rollback(tmp_path: Path) -> None:
    from architect.models import ModelRequest, ModelResponse
    from architect.models.types import UsageQuote

    factory = build_factory(tmp_path, description())
    proposed = await factory.compile(description())

    class PricedFixture(OfflineFixtureModel):
        price = 0.00001

        def quote(self, request: ModelRequest) -> UsageQuote:
            return UsageQuote(tokens_in=100, tokens_out=100, cost_usd=self.price)

        async def complete(self, request: ModelRequest) -> ModelResponse:
            result = await super().complete(request)
            return result.model_copy(update={"cost_usd": self.price})

    player = PricedFixture()
    factory.game_factory.compiler.adapter_factory = lambda model: player
    first = await factory.compile(description(), approved=True, approved_spec=proposed.spec)
    assert first.report is not None and first.report.cost_usd > 0
    await factory.install(first, name="priced")
    player.price = 0
    second = await factory.recompile("priced")
    assert second.revision == 2
    new_report = factory.registry.get("priced").report
    assert new_report is not None and new_report.verification_id != first.report.verification_id
    assert new_report.floor_score == first.report.floor_score and new_report.cost_usd == 0
    restored = factory.rollback("priced")
    assert restored.revision == 1
    assert factory.registry.get("priced").report == first.report


async def test_inherited_deadline_covers_discovery(tmp_path: Path) -> None:
    import asyncio

    from architect.runtime.wallet import compilation_wallet
    from architect.spec import Budget

    factory, _ = await installed(tmp_path)

    class Slow(LocalGameProvider):
        async def discover(self) -> tuple[GameRef, ...]:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    factory.provider = Slow()
    factory.game_factory.provider = factory.provider
    with compilation_wallet(Budget(cost_usd=1, compute_s=0.02)):
        result = await factory.run(description().name)
    assert result.failures == ("competition_budget_exceeded",)
    assert result.wall_time_s < 1 and result.tokens_in == 0


async def test_failed_descendant_charged_once_to_parent(tmp_path: Path) -> None:
    from architect.models import ModelRequest, ModelResponse
    from architect.models.types import UsageQuote

    factory, _ = await installed(tmp_path)

    class FailedFixture(OfflineFixtureModel):
        def quote(self, request: ModelRequest) -> UsageQuote:
            return UsageQuote(tokens_in=900_000, tokens_out=900_000, cost_usd=0.02)

        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise RuntimeError("secret failed provider response")

    factory.game_factory.compiler.adapter_factory = lambda model: FailedFixture()
    before = len(factory.compiler.runtime.store.rows())
    result = await factory.run(description().name)
    assert not result.accepted and len(result.children) == 1
    assert result.tokens_in == result.tokens_out == 900_000 and result.cost_usd == 0.02
    assert result.cost_usd == sum(
        r.cost_usd for r in factory.compiler.runtime.store.rows()[before:]
    )
    assert "secret" not in result.model_dump_json()


def test_description_rejects_duplicate_keys(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"max_games": 3, "max_games": 2}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        CompetitionDescription.read(path)


async def test_explicit_official_preview_manifest_uses_real_nested_path(tmp_path: Path) -> None:
    """Fake public transport only: this is not an official game/score result."""
    from architect.games.evaluation import OfficialPreviewManifest, manifest_refs
    from architect.games.factory import GameFactory
    from architect.games.types import GameAction, GameScore, GameSession, Observation

    refs = tuple(
        GameRef(backend="official_offline", game_id=f"aa{n:02}-r1", revision="r1") for n in range(5)
    )
    manifest = OfficialPreviewManifest(
        dataset_id="fake-public-sdk-catalog",
        dataset_revision="test-v1",
        parent_tuning=(refs[0],),
        parent_held_out=(refs[1],),
        support_tuning=(refs[2],),
        support_held_out=(refs[3],),
        delivery=(refs[4],),
    )
    opened: list[GameRef] = []

    class Session:
        def __init__(self, ref: GameRef) -> None:
            self.ref = ref
            self.actions_used = 0

        async def observe(self) -> Observation:
            return Observation(
                frames=(((4, 0, 0), (1, 0, 0), (0, 0, 0)),),
                state="WIN" if self.actions_used else "NOT_FINISHED",
                available_actions=("RESET",) if self.actions_used else ("ACTION1",),
                levels_completed=int(self.actions_used > 0),
                win_levels=1,
            )

        async def step(self, action: GameAction) -> Observation:
            assert action.name == "ACTION1"
            self.actions_used += 1
            return await self.observe()

        async def finalize(self) -> GameScore:
            return GameScore(
                normalized=float(self.actions_used > 0),
                raw=100 * float(self.actions_used > 0),
                metric="relative_human_action_efficiency.percent.v1",
                source="official",
            )

        async def close(self) -> None:
            pass

    class Provider:
        async def discover(self) -> tuple[GameRef, ...]:
            return manifest_refs(manifest)

        async def open(self, ref: GameRef, *, limits: GameLimits) -> GameSession:
            assert ref in refs
            opened.append(ref)
            return Session(ref)

    factory = build_factory(tmp_path, description())
    provider = Provider()
    factory.provider = provider
    factory.game_factory = GameFactory(
        factory.game_factory.compiler, provider, factory.game_factory.config, manifest=manifest
    )
    proposed = await factory.compile(description())
    compiled = await factory.compile(description(), approved=True, approved_spec=proposed.spec)
    assert compiled.artifact is not None and compiled.artifact.licensed, compiled
    await factory.install(compiled, name="sdk-fixture")
    assert refs[4] not in opened  # Delivery is never a probe or floor.
    run = await factory.run("sdk-fixture")
    assert run.accepted and len(run.children) == 1, run
    assert run.score_source == "official_offline_preview" and run.compliance == "unverified"
    assert run.metric == "normalized_preview_mean"
    assert run.children[0].score is not None and run.children[0].score.source == "official"
    assert refs[4] in opened


@pytest.mark.parametrize("limit", [-0.01, 1.01, float("nan"), float("inf"), True, "0.5"])
def test_parent_floor_limit_is_finite_and_bounded(limit: object) -> None:
    payload = description().model_dump(mode="json")
    payload["floor_limit"] = limit
    with pytest.raises(ValidationError):
        CompetitionDescription.model_validate(payload)


async def test_operator_parent_floor_limit_is_frozen_and_controls_actual_score(
    tmp_path: Path,
) -> None:
    desc = description().model_copy(update={"floor_limit": 0.6})
    factory = build_factory(tmp_path, desc)
    proposed = await factory.compile(desc)
    assert proposed.spec.floor.limit == 0.6
    assert proposed.spec.execution_config["floor_limit"] == 0.6
    player = factory.game_factory.compiler.adapter_factory("fake")
    assert isinstance(player, OfflineFixtureModel)
    original = player.choose

    def lose_parent_navigation(grid: list[list[int]]) -> str:
        if any(2 in row for row in grid) and any(grid[5][x] == 3 for x in (1, 2)):
            return "ACTION1"
        return original(grid)

    player.choose = lose_parent_navigation
    result = await factory.compile(desc, approved=True, approved_spec=proposed.spec)
    assert result.report is not None and result.report.floor_score == pytest.approx(2 / 3)
    assert result.report.floor_limit == 0.6
    assert result.artifact is not None and result.artifact.licensed
    lower = desc.model_copy(update={"floor_limit": 0})
    with pytest.raises(GameProtocolError, match="review"):
        await factory.compile(lower, approved=True, approved_spec=proposed.spec)


async def test_unrelated_catalog_games_do_not_change_required_delivery(tmp_path: Path) -> None:
    factory, _ = await installed(tmp_path)

    class ExtraCatalog(LocalGameProvider):
        async def discover(self) -> tuple[GameRef, ...]:
            return (
                *await super().discover(),
                GameRef(backend="local", game_id="unrelated-public-game", revision="public-r1"),
            )

    factory.provider = ExtraCatalog()
    factory.game_factory.provider = factory.provider
    result = await factory.run(description().name)
    assert result.accepted and len(result.children) == 3


async def test_duplicate_catalog_games_still_rejected(tmp_path: Path) -> None:
    factory, _ = await installed(tmp_path)

    class DuplicateCatalog(LocalGameProvider):
        async def discover(self) -> tuple[GameRef, ...]:
            refs = await super().discover()
            return (*refs, refs[0])

    factory.provider = DuplicateCatalog()
    factory.game_factory.provider = factory.provider
    result = await factory.run(description().name)
    assert not result.accepted and not result.children and result.tokens_in == 0


async def test_exactly_spent_ancestor_permits_free_descendant_work(tmp_path: Path) -> None:
    from architect.models.types import UsageQuote
    from architect.runtime.wallet import compilation_wallet
    from architect.spec import Budget

    factory, _ = await installed(tmp_path)
    with compilation_wallet(Budget(cost_usd=0.01, compute_s=5)) as wallet:
        wallet.reserve(UsageQuote(tokens_in=0, tokens_out=0, cost_usd=0.01))
        result = await factory.run(description().name)
        assert wallet.cost_usd == 0.01
    assert result.accepted and result.cost_usd == 0 and result.tokens_in > 0


async def test_exactly_spent_ancestor_cannot_buy_a_positive_cost_call(tmp_path: Path) -> None:
    from architect.models import ModelRequest
    from architect.models.types import UsageQuote
    from architect.runtime.wallet import compilation_wallet
    from architect.spec import Budget

    factory, _ = await installed(tmp_path)

    class Paid(OfflineFixtureModel):
        def quote(self, request: ModelRequest) -> UsageQuote:
            return UsageQuote(tokens_in=100, tokens_out=100, cost_usd=0.001)

    player = Paid()
    factory.game_factory.compiler.adapter_factory = lambda model: player
    with compilation_wallet(Budget(cost_usd=0.01, compute_s=5)) as wallet:
        wallet.reserve(UsageQuote(tokens_in=0, tokens_out=0, cost_usd=0.01))
        result = await factory.run(description().name)
        assert wallet.cost_usd == 0.01
    assert not result.accepted and result.cost_usd == 0 and not player.prompts


async def test_overspent_ancestor_prevents_provider_discovery(tmp_path: Path) -> None:
    from architect.runtime.wallet import compilation_wallet
    from architect.spec import Budget

    factory, _ = await installed(tmp_path)

    class NoDiscovery(LocalGameProvider):
        discovered = False

        async def discover(self) -> tuple[GameRef, ...]:
            self.discovered = True
            return await super().discover()

    provider = NoDiscovery()
    factory.provider = provider
    factory.game_factory.provider = provider
    with compilation_wallet(Budget(cost_usd=0.01, compute_s=5)) as wallet:
        wallet.cost_usd = 0.02  # Explicit corrupt/overspent state, even without the sticky flag.
        result = await factory.run(description().name)
    assert not result.accepted and not provider.discovered
    assert result.failures == ("competition_budget_exceeded",)


async def test_offline_parent_rewrite_reaches_real_fixed_smoke_without_heldout_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import difflib
    import json
    from collections.abc import Sequence

    import architect.games.competition as competition
    from architect.floors import EvaluationCase, GameScorer, HeldOutAccessError, HeldOutGameScore
    from architect.games.evaluation import ReceiptLedger
    from architect.models import ModelRequest, ModelResponse
    from architect.models.types import UsageQuote
    from architect.spec import FloorDefinition
    from architect.telemetry import categorical

    captured_smoke_records: list[tuple[EvaluationCase, ...]] = []
    real_smoke = competition.HeldOutGameScore

    def capture_smoke(
        definition: FloorDefinition,
        records: Sequence[EvaluationCase],
        environment: GameScorer,
    ) -> HeldOutGameScore:
        captured_smoke_records.append(tuple(records))
        return real_smoke(definition, records, environment)

    monkeypatch.setattr(competition, "HeldOutGameScore", capture_smoke)
    desc = description()
    factory = build_factory(tmp_path, desc)
    proposed = await factory.compile(desc)
    player = factory.game_factory.compiler.adapter_factory("fake")
    assert isinstance(player, OfflineFixtureModel)
    player.wrong = True  # Actual child quality fails, forcing offline parent Stage 3.
    rewrite_prompts: list[str] = []

    class Rewriter(OfflineFixtureModel):
        def quote(self, request: ModelRequest) -> UsageQuote:
            return UsageQuote(tokens_in=100, tokens_out=100, cost_usd=0)

        async def complete(self, request: ModelRequest) -> ModelResponse:
            rewrite_prompts.append(request.prompt)
            payload = json.loads(request.prompt)
            source = payload["source"]
            changed = source.replace("Generated candidate harness.", "Rewritten candidate harness.")
            patch = "".join(
                difflib.unified_diff(
                    source.splitlines(keepends=True),
                    changed.splitlines(keepends=True),
                    fromfile="a/harness.py",
                    tofile="b/harness.py",
                )
            )
            return ModelResponse(
                text=json.dumps({"diff": patch, "components": payload["config"]["components"]}),
                model="fake",
                tokens_in=100,
                tokens_out=100,
                cost_usd=0,
            )

    factory.compiler.config = factory.compiler.config.model_copy(
        update={
            "enable_rewriting": True,
            "max_rewrites": 1,
        }
    )
    factory.compiler.rewrite_adapter = Rewriter()
    factory.compiler.smoke_factory = factory.compiler.floor_factory
    ledger = ReceiptLedger()
    compiler = factory._compiler(factory._policy(desc), ledger)
    primary = compiler.floor_factory(proposed.spec.floor)
    assert compiler.smoke_factory is not None
    smoke = compiler.smoke_factory(proposed.spec.floor)
    assert primary is not smoke
    assert all(case.case_id.startswith("parent-smoke-") for case in smoke.tuning)
    assert all(case.task in [row.task for row in primary.tuning] for case in smoke.tuning)
    assert len(smoke.tuning) == 2
    assert len(captured_smoke_records) == 1
    assert [row.task for row in captured_smoke_records[0]] == [row.task for row in primary.tuning]
    assert compiler.smoke_factory(proposed.spec.floor) is smoke  # Fixed, not sampled on demand.
    with pytest.raises(HeldOutAccessError):
        _ = smoke.held_out
    ledger.close()

    result = await factory.compile(desc, approved=True, approved_spec=proposed.spec)
    assert 3 in result.stages and len(rewrite_prompts) == 1
    assert "rewrite_failed" not in result.failures and "unsafe_source" not in result.failures
    assert "tuning_failed" in result.failures
    assert result.artifact is not None and not result.artifact.licensed
    # Real sandbox runs on BOTH complete tuning and fixed tuning-only smoke replay.
    rewrite_rows = [
        row
        for row in factory.compiler.runtime.store.rows()
        if row.task_type == categorical("rewrite")
    ]
    assert len(rewrite_rows) == 1 and not rewrite_rows[0].accepted
    provider = factory.provider
    assert isinstance(provider, LocalGameProvider)
    for ref in provider.manifest.refs("parent_held_out"):
        assert ref.game_id not in rewrite_prompts[0] and ref.revision not in rewrite_prompts[0]
    # Stage 3 does 3 normal tuning + 2 fixed regression smoke executions, not merely IDs.
    sandbox_rows = [
        row
        for row in factory.compiler.runtime.store.rows()
        if row.task_type == categorical("planner") and row.harness_version == 2
    ]
    assert len(sandbox_rows) == 5


async def test_parent_inference_uses_trusted_exact_overrides(tmp_path: Path) -> None:
    import json

    from architect.models import ModelRequest, ModelResponse

    factory = build_factory(tmp_path, description())

    class Injected(OfflineFixtureModel):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            result = await super().complete(request)
            payload = json.loads(result.text)
            payload["input_schema"] = {"type": "object", "required": ["model_added"]}
            payload["output_schema"] = {"type": "object", "required": ["model_added"]}
            payload["execution_config"] = {"model_added": True}
            payload["floor"]["limit"] = 0
            payload["floor"]["rubric"] = "model_added"
            return result.model_copy(update={"text": json.dumps(payload)})

    factory.compiler.inference_adapter = Injected()
    proposed = await factory.compile(description(), approved=True)
    assert proposed.approval_required and proposed.artifact is None
    assert proposed.spec.floor.limit == 1 and proposed.spec.floor.rubric is None
    assert proposed.spec.input_schema == {"type": "object"}
    assert proposed.spec.output_schema == {"type": "object"}
    assert "model_added" not in proposed.spec.execution_config
    assert proposed.spec.provenance["execution_config"] == "user"


async def test_child_exhausting_ancestor_after_inference_stops_as_budget(tmp_path: Path) -> None:
    from architect.models import ModelRequest
    from architect.models.types import UsageQuote
    from architect.runtime.wallet import compilation_wallet
    from architect.spec import Budget

    factory, _ = await installed(tmp_path)

    class PricedPlay(OfflineFixtureModel):
        def quote(self, request: ModelRequest) -> UsageQuote:
            inference = "specification inference stage" in request.prompt
            return UsageQuote(tokens_in=100, tokens_out=100, cost_usd=0 if inference else 0.001)

    player = PricedPlay()
    factory.game_factory.compiler.adapter_factory = lambda model: player
    with compilation_wallet(Budget(cost_usd=0.01, compute_s=5)) as wallet:
        wallet.reserve(UsageQuote(tokens_in=0, tokens_out=0, cost_usd=0.01))
        result = await factory.run(description().name)
        assert wallet.exhausted and wallet.cost_usd == 0.01
    assert not result.accepted and result.failures == ("competition_budget_exceeded",)
    # One child spent its free Stage 0, then the refusing wallet ended the run.
    assert len(result.children) == 1
    assert result.children[0].failures == ("game_budget_exceeded",)
    assert len(player.prompts) == 1 and "specification inference stage" in player.prompts[0]


async def test_child_own_compile_budget_refusal_stops_as_budget(tmp_path: Path) -> None:
    from architect.compiler import CompileConfig
    from architect.models import ModelRequest
    from architect.models.types import UsageQuote
    from architect.spec import Budget

    desc = description()
    factory = build_factory(tmp_path, desc)
    config = factory.game_factory.config
    # Children keep ample outer dollars; only each child compile() wallet is tight.
    compile_config = CompileConfig.model_validate(
        {
            **config.compile_config.model_dump(mode="json"),
            "compile_budget": Budget(cost_usd=0.0005, compute_s=55).model_dump(mode="json"),
        }
    )
    factory.game_factory.config = config.model_copy(update={"compile_config": compile_config})
    proposed = await factory.compile(desc)
    approved = await factory.compile(desc, approved=True, approved_spec=proposed.spec)
    assert approved.artifact is not None and approved.artifact.licensed
    await factory.install(approved, name=desc.name)

    class PricedPlay(OfflineFixtureModel):
        def quote(self, request: ModelRequest) -> UsageQuote:
            inference = "specification inference stage" in request.prompt
            return UsageQuote(tokens_in=100, tokens_out=100, cost_usd=0 if inference else 0.001)

    player = PricedPlay()
    factory.game_factory.compiler.adapter_factory = lambda model: player
    result = await factory.run(desc.name)
    assert not result.accepted and result.failures == ("competition_budget_exceeded",)
    assert len(result.children) == 1
    child = result.children[0]
    assert child.failures == ("game_budget_exceeded",) and child.cost_usd == 0
    assert child.compile_result is not None and "compile_budget" in child.compile_result.failures


def _official_competition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    target: str | None,
    reset_only: bool = False,
) -> tuple[CompetitionFactory, tuple[GameRef, ...], list[dict[str, object]]]:
    """CompetitionFactory over the real OFFLINE official provider with a scripted worker.

    The fixture model answers RESET on the first decision of ``target`` only. The
    worker mirrors the arcengine rule that a RESET before any action is a full
    reset, forwarded as the fixed ``official_level_reset_required`` code; with
    ``reset_only`` the target's opening observation offers nothing but RESET.
    Scripted wire only: no SDK, no official score evidence.
    """
    import json

    from architect.games import official
    from architect.games.evaluation import OfficialPreviewManifest, manifest_refs
    from architect.games.factory import GameFactory
    from architect.models import ModelRequest, ModelResponse

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
    current: dict[str, str] = {}

    class ScriptedWorker:
        def __init__(self, config: object, deadline: float | None) -> None:
            self.deadline = deadline
            self.target: str | None = None
            self.steps = 0
            self.closed = False

        async def request(self, request: dict[str, object], maximum: int = 2_097_152) -> object:
            assert not self.closed
            calls.append(request)
            if request["op"] == "init":
                return [ref.model_dump(mode="json") for ref in manifest_refs(manifest)]
            if request["op"] == "finalize":
                assert self.target is not None
                return {"card_score": 100, "game_scores": {self.target: 100}}
            if request["op"] == "make":
                assert self.target is None
                self.target = current["game"] = str(request["game_id"])
            won = False
            if request["op"] == "step":
                action = request["action"]
                assert isinstance(action, dict)
                if action["name"] == "RESET" and self.steps == 0:
                    raise GameUnavailableError("official_level_reset_required")
                self.steps += 1
                won = action["name"] == "ACTION1"
            fresh_reset_only = reset_only and self.steps == 0 and self.target == target
            return {
                "frames": [[[2, 3], [0, 0]]],
                "state": "WIN" if won else "NOT_FINISHED",
                "available_actions": ["RESET"] if won or fresh_reset_only else ["ACTION1"],
                "levels_completed": int(won),
                "win_levels": 1,
            }

        async def close(self) -> None:
            self.closed = True

    class ResetFirstModel(OfflineFixtureModel):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            if "specification inference stage" in request.prompt:
                return await super().complete(request)
            data = json.loads(request.prompt.split("Game data (untrusted):\n", 1)[1])
            first = data["history"] == [] and data["previous_test"] is None
            action = "RESET" if first and current.get("game") == target else "ACTION1"
            text = json.dumps({"action": {"name": action}, "plan": "Play the visible level"})
            return ModelResponse(
                text=text, model=self.model, tokens_in=100, tokens_out=100, cost_usd=0
            )

    monkeypatch.setattr(official, "_WorkerProcess", ScriptedWorker)
    factory = build_factory(tmp_path, description())
    model = ResetFirstModel()
    factory.compiler.adapter_factory = lambda name: model
    provider = official.OfficialGameProvider(
        official.OfficialConfig(mode="offline", staged_game_dir=tmp_path)
    )
    child = factory.game_factory
    child.compiler.adapter_factory = lambda name: model
    config = child.config.model_copy(
        update={"limits": GameLimits(max_actions=8, max_turns=8, max_probe_actions=0)}
    )
    factory.provider = provider
    factory.game_factory = GameFactory(child.compiler, provider, config, manifest=manifest)
    return factory, refs, calls


def _reset_steps(calls: list[dict[str, object]]) -> list[str]:
    steps = []
    for call in calls:
        action = call.get("action")
        if call["op"] == "step" and isinstance(action, dict) and action["name"] == "RESET":
            steps.append(str(call["game_id"]))
    return steps


def _assert_fixed_child_codes(failures: tuple[str, ...], *, hidden: str) -> None:
    import re

    # The parent's own verdict comes first, then fixed child codes, none repeated.
    own = [code for code in failures if not code.startswith("child_")]
    assert own and failures[: len(own)] == tuple(own)
    assert len(set(failures)) == len(failures)
    assert all(re.fullmatch(r"[a-z][a-z0-9_]{0,69}", code) for code in failures)
    assert hidden not in " ".join(failures)


async def test_official_child_reset_first_surfaces_fixed_child_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = "fixture4-v1"
    factory, refs, calls = _official_competition(tmp_path, monkeypatch, target=target)
    assert refs[4].game_id == target
    proposed = await factory.compile(description())
    compiled = await factory.compile(description(), approved=True, approved_spec=proposed.spec)
    assert compiled.artifact is not None and not compiled.artifact.licensed
    assert "primary_evaluation_failed" in compiled.failures
    # RESET was never offered on the opening prompt, so the backend refused it.
    assert "child_game_execution_failed" in compiled.failures
    assert "child_game_action_unavailable" in compiled.failures
    _assert_fixed_child_codes(compiled.failures, hidden=target)
    assert '"frames"' not in compiled.model_dump_json()
    assert not _reset_steps(calls)
    makes = [call["game_id"] for call in calls if call["op"] == "make"]
    assert makes[-1] == target and refs[3].game_id in makes
    with pytest.raises(RegistryError, match="unlicensed"):
        await factory.install(compiled, name="reset-first")
    assert factory.registry.list() == ()
    await factory.provider.close()

    # Control: the same wire with no RESET licenses, and carries no child codes.
    clean, _, _ = _official_competition(tmp_path / "clean", monkeypatch, target=None)
    proposed = await clean.compile(description())
    licensed = await clean.compile(description(), approved=True, approved_spec=proposed.spec)
    assert licensed.artifact is not None and licensed.artifact.licensed, licensed
    assert not any(code.startswith("child_") for code in licensed.failures)
    await clean.provider.close()


async def test_official_worker_reset_refusal_surfaces_fixed_child_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from architect.games.factory import PLAY_FAILURE_CODES

    target = "fixture4-v1"
    factory, refs, calls = _official_competition(
        tmp_path, monkeypatch, target=target, reset_only=True
    )
    proposed = await factory.compile(description())
    compiled = await factory.compile(description(), approved=True, approved_spec=proposed.spec)
    assert compiled.artifact is not None and not compiled.artifact.licensed
    assert "primary_evaluation_failed" in compiled.failures
    # The offline worker refused the opening RESET; its fixed code reaches the parent.
    assert "official_level_reset_required" in PLAY_FAILURE_CODES
    assert "child_game_unavailable" in compiled.failures
    assert "child_official_level_reset_required" in compiled.failures
    _assert_fixed_child_codes(compiled.failures, hidden=target)
    assert _reset_steps(calls) == [target]
    await factory.provider.close()


def test_child_failure_codes_are_fixed_deduplicated_ordered_and_capped() -> None:
    from architect.games.competition import child_failure_codes
    from architect.games.factory import GameRunResult

    spec = Spec.model_validate(
        {
            "task": "x",
            "domain": "game",
            "input_schema": {},
            "output_schema": {},
            "floor": {"kind": "held_out_game_score", "limit": 1, "dataset": "d"},
            "budget": {"cost_usd": 1, "compute_s": 1},
            "model_target": "fake",
        }
    )
    children = [
        GameRunResult(
            accepted=False, failures=("game_unavailable", "official_level_reset_required")
        ),
        GameRunResult(
            accepted=False,
            failures=("game_compile_failed", "Model Said This", "game_unavailable"),
            compile_result=CompileResult(spec=spec, failures=("primary_floor_failed",)),
        ),
        GameRunResult(accepted=True),
    ]
    assert child_failure_codes(children) == (
        "child_game_unavailable",
        "child_official_level_reset_required",
        "child_game_compile_failed",
        "child_primary_floor_failed",
    )
    many = [GameRunResult(accepted=False, failures=(f"code_{i}",)) for i in range(40)]
    assert len(child_failure_codes(many)) == 16
    assert child_failure_codes([]) == ()


@pytest.mark.parametrize("cap", ["ancestor", "description"])
async def test_slow_children_exhaust_shared_deadline_as_compile_budget(
    tmp_path: Path, cap: str
) -> None:
    import asyncio

    from architect.models import ModelRequest, ModelResponse
    from architect.runtime.wallet import compilation_wallet
    from architect.spec import Budget

    class Slow(OfflineFixtureModel):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            if "specification inference stage" not in request.prompt:
                await asyncio.sleep(0.05)
            return await super().complete(request)

    desc = description()
    if cap == "description":
        desc = desc.model_copy(update={"compute_cap": Budget(cost_usd=1, compute_s=1.5)})
    factory = build_factory(tmp_path, desc)
    proposed = await factory.compile(desc)
    player = Slow()
    factory.game_factory.compiler.adapter_factory = lambda name: player
    if cap == "ancestor":
        with compilation_wallet(Budget(cost_usd=1, compute_s=1.5)):
            result = await factory.compile(desc, approved=True, approved_spec=proposed.spec)
    else:
        result = await factory.compile(desc, approved=True, approved_spec=proposed.spec)
    # Below two child rollouts: no bare TimeoutError, a budget verdict with the same spec.
    assert result.spec == proposed.spec and result.artifact is None
    assert result.failures[0] == "compile_budget"
    assert all(code.startswith("child_") for code in result.failures[1:])
    assert not result.approval_required
    with pytest.raises(RegistryError, match="unlicensed"):
        await factory.install(result, name="slow")


async def test_recompile_deadline_expiry_keeps_registry_error_contract(tmp_path: Path) -> None:
    import asyncio

    from architect.models import ModelRequest, ModelResponse
    from architect.runtime.wallet import compilation_wallet
    from architect.spec import Budget

    factory, _ = await installed(tmp_path)
    name = description().name

    class Slow(OfflineFixtureModel):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            if "specification inference stage" not in request.prompt:
                await asyncio.sleep(0.05)
            return await super().complete(request)

    factory.game_factory.compiler.adapter_factory = lambda model: Slow()
    with compilation_wallet(Budget(cost_usd=1, compute_s=0.5)):
        with pytest.raises(RegistryError, match="competition_recompile_budget"):
            await factory.recompile(name)
    assert factory.registry.status(name).revision == 1
