"""Synthetic local games, identities and bounded public protocols."""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from architect.games.local import (
    FAMILIES,
    ROLES,
    LocalFixtureManifest,
    LocalGameProvider,
    LocalPuzzle,
)
from architect.games.types import (
    GameAction,
    GameActionError,
    GameLimits,
    GameProtocolError,
    GameScore,
    GameSession,
    GameSessionError,
    GameUnavailableError,
    Observation,
    ProbeEnvironment,
    decode_action,
    decode_observation,
)


@pytest.mark.parametrize("role", ROLES)
async def test_three_families_really_win(role: str) -> None:
    provider = LocalGameProvider()
    for ref in provider.manifest.refs(role):  # type: ignore[arg-type]
        session = await provider.open(ref, limits=GameLimits())
        before = await session.observe()
        assert before.state == "NOT_FINISHED"
        assert session.actions_used == 0
        # Test solver consumes only public pixels, not private puzzle data.
        grid = before.frames[0]
        if "navigation" in ref.game_id:
            start = next((x, y) for y, row in enumerate(grid) for x, c in enumerate(row) if c == 2)
            goal = next((x, y) for y, row in enumerate(grid) for x, c in enumerate(row) if c == 3)
            actions = ["ACTION2"] * (goal[1] - start[1])
            actions += ["ACTION4"] * (goal[0] - start[0])
        elif "switches" in ref.game_id:
            actions = [f"ACTION{i + 1}" for i in range(3) if grid[0][i] == 3] + ["ACTION4"]
        else:
            actions = [f"ACTION{pixel - 3}" for pixel in grid[0] if pixel >= 4]
        for action in actions:
            observation = await session.step(GameAction(name=action))  # type: ignore[arg-type]
        assert observation.state == "WIN"
        assert session.actions_used == len(actions)
        score = await session.finalize()
        assert score == GameScore(normalized=1, raw=1, metric="local_win_v1", source="local")
        assert await session.finalize() is score
        with pytest.raises(GameSessionError, match="session_finalized"):
            await session.step(GameAction(name="RESET"))
        await session.close()
        await session.close()
        with pytest.raises(GameSessionError, match="session_closed"):
            await session.observe()


async def test_wrong_play_scores_zero_and_reset_retains_identity() -> None:
    provider = LocalGameProvider()
    for ref in await provider.discover():
        session = await provider.open(ref, limits=GameLimits())
        before = await session.observe()
        await session.step(GameAction(name="ACTION1"))
        after = await session.step(GameAction(name="RESET"))
        assert after == before
        assert session.ref == ref
        assert session.actions_used == 2
        assert (await session.finalize()).normalized == 0


def test_frozen_fifteen_disjoint_fingerprints() -> None:
    manifest = LocalFixtureManifest()
    assert len(manifest.fixtures) == 15
    assert {f.puzzle.family for f in manifest.fixtures} == set(FAMILIES)
    identities = {manifest.identity(ref) for role in ROLES for ref in manifest.refs(role)}
    assert len(identities) == 15
    assert manifest.digest == LocalFixtureManifest().digest
    with pytest.raises(ValidationError):
        manifest.fixtures = ()
    # Validate exactly the sorted even/odd case split used by the floor.
    parent = sorted(
        manifest.refs("parent_tuning") + manifest.refs("parent_held_out"),
        key=lambda ref: ref.game_id,
    )
    assert set(parent[::2]) == set(manifest.refs("parent_tuning"))
    assert set(parent[1::2]) == set(manifest.refs("parent_held_out"))
    support = sorted(
        manifest.refs("support_tuning") + manifest.refs("support_held_out"),
        key=lambda ref: ref.game_id,
    )
    assert set(support[::2]) == set(manifest.refs("support_tuning"))
    assert set(support[1::2]) == set(manifest.refs("support_held_out"))


def test_alias_seed_and_revision_cannot_manufacture_independence() -> None:
    manifest = LocalFixtureManifest()
    fixtures = list(manifest.fixtures)
    fixtures[1] = fixtures[1].model_copy(
        update={
            "puzzle": fixtures[0].puzzle,
            "seed": 999,
            "revision": "totally-new-label",
        }
    )
    with pytest.raises(ValidationError, match="duplicate_puzzle_identity"):
        LocalFixtureManifest(fixtures=tuple(fixtures))
    bypass = manifest.model_copy(update={"fixtures": tuple(fixtures)})
    with pytest.raises(ValidationError, match="duplicate_puzzle_identity"):
        LocalGameProvider(bypass)
    fixture = manifest.fixtures[0]
    alias = fixture.model_copy(update={"game_id": "alias", "seed": 42, "revision": "label"})
    assert alias.ref.revision == fixture.ref.revision
    walls = fixture.puzzle.walls
    permuted = fixture.puzzle.model_copy(update={"walls": tuple(reversed(walls)) + walls})
    assert permuted.fingerprint() == fixture.puzzle.fingerprint()
    switch = next(f.puzzle for f in manifest.fixtures if "switches" in f.game_id)
    assert switch.model_copy(update={"width": 12}).fingerprint() == switch.fingerprint()


async def test_ref_is_recomputed_and_wrong_backend_rejected() -> None:
    provider = LocalGameProvider()
    ref = provider.manifest.refs("delivery")[0]
    for bad in (
        ref.model_copy(update={"revision": "seed-123"}),
        ref.model_copy(update={"backend": "official_offline"}),
    ):
        with pytest.raises(GameUnavailableError, match="unknown_local_instance"):
            await provider.open(bad, limits=GameLimits())


async def test_probe_is_cached_and_meters_same_session() -> None:
    provider = LocalGameProvider()
    session = await provider.open(provider.manifest.refs("delivery")[0], limits=GameLimits())
    probe = ProbeEnvironment(session, GameLimits(max_probe_actions=2))
    first, second = await asyncio.gather(probe.probe(), probe.probe())
    assert first == second
    assert session.actions_used == 2
    first.clear()
    assert await probe.probe(0) == second
    assert session.actions_used == 2
    assert "revision" not in second


async def test_probe_failure_does_not_replay_partial_actions() -> None:
    provider = LocalGameProvider()
    session = await provider.open(
        provider.manifest.refs("delivery")[0], limits=GameLimits(max_actions=1)
    )
    probe = ProbeEnvironment(session, GameLimits())
    with pytest.raises(GameSessionError, match="action_limit"):
        await probe.probe()
    with pytest.raises(GameSessionError, match="probe_failed"):
        await probe.probe()
    assert session.actions_used == 1


async def test_action_bounds_and_no_observe_reset() -> None:
    provider = LocalGameProvider()
    session = await provider.open(
        provider.manifest.refs("delivery")[0], limits=GameLimits(max_actions=1)
    )
    with pytest.raises(GameActionError, match="unavailable_action"):
        await session.step(GameAction(name="ACTION7"))
    assert session.actions_used == 0
    with pytest.raises(GameActionError, match="invalid_action"):
        await session.step(GameAction.model_construct(name="ACTION6"))
    after = await session.step(GameAction(name="ACTION2"))
    assert await session.observe() == after
    with pytest.raises(GameSessionError, match="action_limit"):
        await session.step(GameAction(name="RESET"))


@pytest.mark.parametrize(
    "action",
    [
        {"name": "ACTION6"},
        {"name": "ACTION6", "x": 0},
        {"name": "ACTION6", "x": True, "y": 1},
        {"name": "ACTION6", "x": 64, "y": 0},
        {"name": "ACTION1", "x": 1, "y": 2},
        {"name": "RESET", "extra": "data"},
        {"actions": ["ACTION1"]},
        {"name": "SECRET"},
    ],
)
def test_typed_action_rejects_invalid(action: object) -> None:
    with pytest.raises(GameActionError, match="invalid_action"):
        decode_action(action, ("RESET", "ACTION1", "ACTION6"))


def test_coordinate_action_valid_and_supported_only() -> None:
    assert decode_action({"name": "ACTION6", "x": 0, "y": 63}, ("ACTION6",)).y == 63
    with pytest.raises(GameActionError, match="unavailable_action"):
        decode_action({"name": "ACTION1"}, ("RESET",))


def test_observations_multiframe_and_limits() -> None:
    observation = Observation(
        frames=(((0, 15),), ((3, 4),)),
        state="NOT_FINISHED",
        available_actions=("RESET", "ACTION6"),
        levels_completed=0,
    )
    payload = observation.model_dump_json().encode()
    assert decode_observation(payload, GameLimits()) == observation
    with pytest.raises(GameProtocolError, match="observation_frame_limit"):
        decode_observation(payload, GameLimits(max_frames=1))
    with pytest.raises(GameProtocolError, match="observation_payload_limit"):
        decode_observation(b" " * 129, GameLimits(max_payload_bytes=128))
    with pytest.raises(GameProtocolError, match="invalid_observation"):
        decode_observation(b"sensitive backend response", GameLimits())
    for update in (
        {"frames": (((0,), (1, 2)),)},
        {"frames": (((True,),),)},
        {"frames": (((16,),),)},
        {"frames": ()},
        {"levels_completed": True},
        {"state": "GAME_OVER"},
        {"available_actions": ("RESET", "RESET")},
        {"win_levels": 1, "levels_completed": 2},
    ):
        with pytest.raises(ValidationError):
            Observation.model_validate(observation.model_dump() | update)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_actions": True},
        {"max_frames": 65},
        {"max_turns": 0},
        {"max_probe_actions": -1},
    ],
)
def test_limits_strict(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        GameLimits.model_validate(kwargs)


def test_score_is_finite_and_explicit() -> None:
    for kwargs in (
        {"normalized": float("nan"), "raw": 1, "source": "official"},
        {"normalized": 0.5, "raw": 0.5, "source": "local"},
        {"normalized": 0, "raw": 0, "source": "unavailable"},
        {"normalized": None, "raw": None, "source": "official"},
    ):
        with pytest.raises(ValidationError):
            GameScore.model_validate({"metric": "test"} | kwargs)
    assert GameScore(normalized=None, raw=None, metric="unknown_scale", source="unavailable")


def test_bad_puzzle_geometry_and_manifest_roles() -> None:
    for kwargs in ({"walls": ((1, 1),)}, {"goal": (1, 1)}, {"goal": (63, 63)}, {"sequence": (1,)}):
        with pytest.raises(ValidationError):
            LocalPuzzle.model_validate({"family": "local-navigation-v1"} | kwargs)
    manifest = LocalFixtureManifest()
    fixtures = list(manifest.fixtures)
    fixtures[0] = fixtures[0].model_copy(update={"role": "delivery"})
    with pytest.raises(ValidationError, match="invalid_fixture_roles"):
        LocalFixtureManifest(fixtures=tuple(fixtures))
    with pytest.raises(ValueError, match="invalid_fixture_role"):
        manifest.refs("bad")  # type: ignore[arg-type]


async def test_probe_bounds_initial_and_incremental_payload() -> None:
    provider = LocalGameProvider()
    ref = provider.manifest.refs("delivery")[0]
    session = await provider.open(ref, limits=GameLimits())
    with pytest.raises(GameActionError, match="invalid_probe_limit"):
        await ProbeEnvironment(session, GameLimits()).probe(True)
    with pytest.raises(GameProtocolError, match="probe_payload_limit"):
        await ProbeEnvironment(session, GameLimits(max_payload_bytes=128)).probe(0)
    assert session.actions_used == 0
    with pytest.raises(GameProtocolError, match="probe_payload_limit"):
        await ProbeEnvironment(session, GameLimits(max_payload_bytes=600)).probe()
    assert session.actions_used == 1


async def test_probe_won_game_never_resets_or_moves() -> None:
    provider = LocalGameProvider()
    ref = next(r for r in provider.manifest.refs("parent_tuning") if "switches" in r.game_id)
    session = await provider.open(ref, limits=GameLimits())
    await session.step(GameAction(name="ACTION1"))
    await session.step(GameAction(name="ACTION4"))
    result = await ProbeEnvironment(session, GameLimits()).probe()
    assert result["actions"] == []
    assert session.actions_used == 2


def test_local_invalid_inactive_and_duplicate_id_data() -> None:
    for data in (
        {"family": "local-switches-v1", "target_mask": 0},
        {"family": "local-switches-v1", "target_mask": 1, "walls": ((2, 2),)},
        {"family": "local-sequence-v1", "sequence": ()},
        {"family": "local-sequence-v1", "sequence": (1,), "target_mask": 1},
    ):
        with pytest.raises(ValidationError):
            LocalPuzzle.model_validate(data)
    manifest = LocalFixtureManifest()
    fixtures = list(manifest.fixtures)
    fixtures[1] = fixtures[1].model_copy(update={"game_id": fixtures[0].game_id})
    with pytest.raises(ValidationError, match="duplicate_game_id"):
        LocalFixtureManifest(fixtures=tuple(fixtures))


def test_constructed_action_cannot_bypass_decoder() -> None:
    with pytest.raises(GameActionError, match="invalid_action"):
        decode_action(GameAction.model_construct(name="ACTION6", x=True, y=3), ("ACTION6",))


class AnimatedSession:
    """A session whose every observation is an animation ending in the real frame."""

    def __init__(self, session: GameSession, *, frames: int, size: int | None = None) -> None:
        self._session = session
        self.frames = frames
        self.size = size

    @property
    def actions_used(self) -> int:
        return self._session.actions_used

    def _animate(self, observation: Observation) -> Observation:
        last = observation.frames[-1]
        side = self.size or len(last)
        blank = tuple(tuple(0 for _ in range(side)) for _ in range(side))
        return observation.model_copy(update={"frames": (blank,) * (self.frames - 1) + (last,)})

    async def observe(self) -> Observation:
        return self._animate(await self._session.observe())

    async def step(self, action: GameAction) -> Observation:
        return self._animate(await self._session.step(action))

    async def finalize(self) -> GameScore:
        return await self._session.finalize()

    async def close(self) -> None:
        await self._session.close()


async def test_probe_keeps_settled_frames_and_stops_within_context() -> None:
    """Public ARC games answer one action with up to 64 frames (sb26: 42); the
    Stage 0 probe keeps only each settled frame and, given a context budget,
    stops taking actions rather than failing the whole compile."""
    provider = LocalGameProvider()
    ref = provider.manifest.refs("delivery")[0]
    session = await provider.open(ref, limits=GameLimits())
    plain = await ProbeEnvironment(session, GameLimits()).probe()
    animated = AnimatedSession(await provider.open(ref, limits=GameLimits()), frames=64)
    assert await ProbeEnvironment(animated, GameLimits()).probe() == plain
    assert animated.actions_used == 3
    observations = plain["observations"]
    assert isinstance(observations, list) and len(observations) == 4
    assert all(isinstance(o, dict) and len(o["frames"]) == 1 for o in observations)  # type: ignore[index]

    full = len(json.dumps(plain))
    initial = len(json.dumps({"observations": observations[:1], "actions": []}))
    for budget, expected_actions in ((full, 3), (full - 1, 2), (initial, 0)):
        session = await provider.open(ref, limits=GameLimits())
        result = await ProbeEnvironment(session, GameLimits(), max_chars=budget).probe()
        assert len(json.dumps(result)) <= budget
        assert result["actions"] == plain["actions"][:expected_actions]  # type: ignore[index]
        assert result["observations"] == observations[: expected_actions + 1]
        # At most one action is spent on an observation that did not fit.
        assert expected_actions <= session.actions_used <= expected_actions + 1
    with pytest.raises(GameProtocolError, match="probe_payload_limit"):
        session = await provider.open(ref, limits=GameLimits())
        await ProbeEnvironment(session, GameLimits(), max_chars=initial - 1).probe()
    assert session.actions_used == 0
    for bad in (True, -1):
        with pytest.raises(GameActionError, match="invalid_probe_limit"):
            ProbeEnvironment(session, GameLimits(), max_chars=bad)  # type: ignore[arg-type]
