"""Fifteen synthetic puzzle instances of exactly three local rule families.

Fingerprints identify concrete transition systems, not seed/revision labels.
This is not a general semantic equivalence detector or an ARC benchmark.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from architect.games.types import (
    ActionName,
    GameAction,
    GameActionError,
    GameLimits,
    GameRef,
    GameScore,
    GameSession,
    GameSessionError,
    GameUnavailableError,
    Observation,
    decode_observation,
)

Role = Literal["parent_tuning", "parent_held_out", "support_tuning", "support_held_out", "delivery"]
Family = Literal["local-navigation-v1", "local-switches-v1", "local-sequence-v1"]
ROLES: tuple[Role, ...] = (
    "parent_tuning",
    "parent_held_out",
    "support_tuning",
    "support_held_out",
    "delivery",
)
FAMILIES: tuple[Family, ...] = ("local-navigation-v1", "local-switches-v1", "local-sequence-v1")
SLOTS = ("P0", "P1", "S0", "S1", "D0")
Point = tuple[
    Annotated[int, Field(strict=True, ge=0, le=63)], Annotated[int, Field(strict=True, ge=0, le=63)]
]
# These describe the implemented rules, independent of user manifest metadata.
_RULES = {
    "local-navigation-v1": "v1:up,down,left,right;wall-stay;reach-goal;reset-initial",
    "local-switches-v1": "v1:toggle-bits-0,1,2;action4-submit;match-mask-win;reset-zero",
    "local-sequence-v1": "v1:actions1-3-match-next;wrong-reset-progress;complete-win;reset-zero",
}


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


class LocalPuzzle(BaseModel):
    """Trusted actual data; never include this model in prompts or telemetry."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    family: Family
    width: Annotated[int, Field(strict=True, ge=3, le=64)] = 7
    height: Annotated[int, Field(strict=True, ge=3, le=64)] = 7
    walls: Annotated[tuple[Point, ...], Field(max_length=4096)] = ()
    start: Point = (1, 1)
    goal: Point = (5, 5)
    target_mask: Annotated[int, Field(strict=True, ge=0, le=7)] = 0
    sequence: Annotated[
        tuple[Annotated[int, Field(strict=True, ge=1, le=3)], ...], Field(max_length=16)
    ] = ()

    @model_validator(mode="after")
    def valid_puzzle(self) -> Self:
        if any(
            x >= self.width or y >= self.height for x, y in (*self.walls, self.start, self.goal)
        ):
            raise ValueError("puzzle_geometry")
        if self.family == "local-navigation-v1":
            if self.start == self.goal or self.start in self.walls or self.goal in self.walls:
                raise ValueError("puzzle_endpoints")
            if self.sequence or self.target_mask:
                raise ValueError("irrelevant_puzzle_fields")
        elif self.family == "local-switches-v1":
            if self.target_mask == 0 or self.sequence:
                raise ValueError("invalid_switch_puzzle")
        elif not self.sequence or len(self.sequence) > self.width:
            raise ValueError("invalid_sequence_puzzle")
        # Inactive geometry/state fields must not manufacture new identities.
        if self.family != "local-navigation-v1":
            if self.walls or self.start != (1, 1) or self.goal != (5, 5):
                raise ValueError("irrelevant_puzzle_fields")
        if self.family == "local-sequence-v1" and self.target_mask:
            raise ValueError("irrelevant_puzzle_fields")
        return self

    def fingerprint(self) -> str:
        # Width/height of non-navigation canvases are cosmetic, not puzzle geometry.
        if self.family == "local-navigation-v1":
            data: object = {
                "geometry": [self.width, self.height],
                "walls": sorted(set(self.walls)),
                "initial": self.start,
                "goal": self.goal,
            }
        elif self.family == "local-switches-v1":
            data = {"initial": 0, "switch_bits": [0, 1, 2], "goal": self.target_mask}
        else:
            data = {"initial": 0, "transition_sequence": self.sequence, "goal": len(self.sequence)}
        return _digest({"rules": _digest(_RULES[self.family]), "puzzle": data})


class LocalFixture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    role: Role
    game_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    puzzle: LocalPuzzle
    # Caller labels are intentionally excluded from the trusted identity.
    seed: Annotated[int, Field(strict=True, ge=0)] = 0
    revision: str = Field(default="local-fixture-v1", min_length=1, max_length=128)

    @property
    def ref(self) -> GameRef:
        return GameRef(backend="local", game_id=self.game_id, revision=self.puzzle.fingerprint())


def _fixtures() -> tuple[LocalFixture, ...]:
    result: list[LocalFixture] = []
    sequences = ((1, 2, 3), (2, 1, 3), (3, 2, 1), (1, 3, 2), (2, 3, 1))
    for family in FAMILIES:
        for index, role in enumerate(ROLES):
            if family == "local-navigation-v1":
                # Same wall/transition rules, different actual goals.
                puzzle = LocalPuzzle(family=family, goal=(index + 1, 5), walls=((3, 2), (3, 3)))
            elif family == "local-switches-v1":
                puzzle = LocalPuzzle(family=family, target_mask=index + 1)
            else:
                puzzle = LocalPuzzle(family=family, sequence=sequences[index])
            result.append(
                LocalFixture(role=role, game_id=f"{family}-{SLOTS[index]}", puzzle=puzzle)
            )
    return tuple(result)


class LocalFixtureManifest(BaseModel):
    """Frozen complete instance partition, including trusted fingerprint recomputation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    fixtures: Annotated[tuple[LocalFixture, ...], Field(min_length=15, max_length=15)] = Field(
        default_factory=_fixtures
    )

    @model_validator(mode="after")
    def distinct_instances(self) -> Self:
        identities = [fixture.puzzle.fingerprint() for fixture in self.fixtures]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate_puzzle_identity")
        if len({fixture.game_id for fixture in self.fixtures}) != 15:
            raise ValueError("duplicate_game_id")
        if {(fixture.role, fixture.puzzle.family) for fixture in self.fixtures} != {
            (role, family) for role in ROLES for family in FAMILIES
        }:
            raise ValueError("invalid_fixture_roles")
        return self

    def refs(self, role: Role) -> tuple[GameRef, ...]:
        if role not in ROLES:
            raise ValueError("invalid_fixture_role")
        return tuple(
            sorted((f.ref for f in self.fixtures if f.role == role), key=lambda r: r.game_id)
        )

    def fixture(self, ref: GameRef) -> LocalFixture:
        for fixture in self.fixtures:
            if ref == fixture.ref:
                return fixture
        raise GameUnavailableError("unknown_local_instance")

    def identity(self, ref: GameRef) -> str:
        return self.fixture(ref).puzzle.fingerprint()

    @property
    def digest(self) -> str:
        return _digest(sorted((f.role, f.game_id, f.puzzle.fingerprint()) for f in self.fixtures))


class LocalGameProvider:
    """Provider of local synthetic games, never an official fallback."""

    def __init__(self, manifest: LocalFixtureManifest | None = None) -> None:
        supplied = manifest if manifest is not None else LocalFixtureManifest()
        # Revalidate even objects made with model_copy/model_construct, including puzzles.
        self._manifest = LocalFixtureManifest.model_validate(supplied.model_dump())

    @property
    def manifest(self) -> LocalFixtureManifest:
        return self._manifest

    async def discover(self) -> tuple[GameRef, ...]:
        return tuple(f.ref for f in self._manifest.fixtures)

    async def open(self, ref: GameRef, *, limits: GameLimits) -> GameSession:
        fixture = self._manifest.fixture(ref)
        return _LocalSession(fixture, limits)


class _LocalSession:
    def __init__(self, fixture: LocalFixture, limits: GameLimits) -> None:
        self.ref = fixture.ref
        self._puzzle = fixture.puzzle
        self._limits = limits
        self._actions_used = 0
        self._closed = False
        self._score: GameScore | None = None
        self._reset()

    @property
    def actions_used(self) -> int:
        return self._actions_used

    def _reset(self) -> None:
        self._position = self._puzzle.start
        self._mask = 0
        self._progress = 0
        self._won = False

    def _check(self) -> None:
        if self._closed:
            raise GameSessionError("session_closed")

    def _observation(self) -> Observation:
        p = self._puzzle
        grid = [[0] * p.width for _ in range(p.height)]
        if p.family == "local-navigation-v1":
            for x, y in p.walls:
                grid[y][x] = 1
            grid[p.goal[1]][p.goal[0]] = 3
            grid[self._position[1]][self._position[0]] = 2
            actions: tuple[ActionName, ...] = ("RESET", "ACTION1", "ACTION2", "ACTION3", "ACTION4")
        elif p.family == "local-switches-v1":
            for bit in range(3):
                grid[0][bit] = 3 if p.target_mask & (1 << bit) else 1
                grid[1][bit] = 3 if self._mask & (1 << bit) else 1
            actions = ("RESET", "ACTION1", "ACTION2", "ACTION3", "ACTION4")
        else:
            for index, symbol in enumerate(p.sequence):
                grid[0][index] = symbol + 3
                grid[1][index] = 3 if index < self._progress else 1
            actions = ("RESET", "ACTION1", "ACTION2", "ACTION3")
        observation = Observation(
            frames=(tuple(tuple(row) for row in grid),),
            state="WIN" if self._won else "NOT_FINISHED",
            available_actions=("RESET",) if self._won else actions,
            levels_completed=int(self._won),
            win_levels=1,
        )
        return decode_observation(observation.model_dump_json().encode(), self._limits)

    async def observe(self) -> Observation:
        self._check()
        return self._observation()

    async def step(self, action: GameAction) -> Observation:
        self._check()
        if self._score is not None:
            raise GameSessionError("session_finalized")
        if self._actions_used >= self._limits.max_actions:
            raise GameSessionError("action_limit")
        # Revalidate trusted boundary rather than accepting a model_construct bypass.
        try:
            action = GameAction.model_validate(action.model_dump())
        except (ValueError, AttributeError):
            raise GameActionError("invalid_action") from None
        if action.name not in self._observation().available_actions:
            raise GameActionError("unavailable_action")
        self._actions_used += 1
        p = self._puzzle
        if action.name == "RESET":
            self._reset()
        elif p.family == "local-navigation-v1":
            dx, dy = {"ACTION1": (0, -1), "ACTION2": (0, 1), "ACTION3": (-1, 0), "ACTION4": (1, 0)}[
                action.name
            ]
            x, y = self._position[0] + dx, self._position[1] + dy
            if 0 <= x < p.width and 0 <= y < p.height and (x, y) not in p.walls:
                self._position = (x, y)
            self._won = self._position == p.goal
        elif p.family == "local-switches-v1":
            if action.name == "ACTION4":
                self._won = self._mask == p.target_mask
            else:
                self._mask ^= 1 << (int(action.name[-1]) - 1)
        else:
            symbol = int(action.name[-1])
            self._progress = self._progress + 1 if symbol == p.sequence[self._progress] else 0
            self._won = self._progress == len(p.sequence)
        return self._observation()

    async def finalize(self) -> GameScore:
        self._check()
        if self._score is None:
            value = float(self._won)
            self._score = GameScore(
                normalized=value, raw=value, metric="local_win_v1", source="local"
            )
        return self._score

    async def close(self) -> None:
        self._closed = True
