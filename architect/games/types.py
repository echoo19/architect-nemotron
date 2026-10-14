"""Bounded public game messages and trusted host-side protocols.

These safety limits are not competition limits. Puzzle data and final score
capabilities stay in the provider, never in model-authored messages.
"""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Annotated, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator

ActionName = Literal[
    "RESET", "ACTION1", "ACTION2", "ACTION3", "ACTION4", "ACTION5", "ACTION6", "ACTION7"
]
Coordinate = Annotated[int, Field(strict=True, ge=0, le=63)]
Count = Annotated[int, Field(strict=True, ge=0, le=1_000_000)]
Pixel = Annotated[int, Field(strict=True, ge=0, le=15)]
Row = Annotated[tuple[Pixel, ...], Field(min_length=1, max_length=64)]
Grid = Annotated[tuple[Row, ...], Field(min_length=1, max_length=64)]


class GameUnavailableError(RuntimeError):
    """A requested real backend or trusted score is unavailable."""


class GameProtocolError(ValueError):
    """Malformed public backend data."""


class GameActionError(ValueError):
    """Invalid or unavailable action."""


class GameSessionError(RuntimeError):
    """Invalid session lifecycle or exhausted safety bound."""


class _Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class GameAction(_Schema):
    name: ActionName
    x: Coordinate | None = None
    y: Coordinate | None = None

    @model_validator(mode="after")
    def coordinates(self) -> Self:
        if self.name == "ACTION6":
            if self.x is None or self.y is None:
                raise ValueError("action_coordinates_required")
        elif self.x is not None or self.y is not None:
            raise ValueError("action_coordinates_forbidden")
        return self


class Observation(_Schema):
    frames: Annotated[tuple[Grid, ...], Field(min_length=1, max_length=64)]
    state: Literal["NOT_PLAYED", "NOT_FINISHED", "WIN", "GAME_OVER"]
    available_actions: Annotated[tuple[ActionName, ...], Field(max_length=8)]
    levels_completed: Count
    win_levels: Annotated[int, Field(strict=True, ge=1, le=1_000_000)] | None = None

    @model_validator(mode="after")
    def rectangular(self) -> Self:
        if any(any(len(row) != len(frame[0]) for row in frame) for frame in self.frames):
            raise ValueError("nonrectangular_frame")
        if len(set(self.available_actions)) != len(self.available_actions):
            raise ValueError("duplicate_actions")
        if self.state == "GAME_OVER" and any(a != "RESET" for a in self.available_actions):
            raise ValueError("game_over_actions")
        if self.win_levels is not None and self.levels_completed > self.win_levels:
            raise ValueError("invalid_level_count")
        return self


class GameLimits(_Schema):
    max_actions: Annotated[int, Field(strict=True, ge=1, le=100_000)] = 256
    max_probe_actions: Annotated[int, Field(strict=True, ge=0, le=256)] = 3
    max_turns: Annotated[int, Field(strict=True, ge=1, le=100_000)] = 256
    max_frames: Annotated[int, Field(strict=True, ge=1, le=64)] = 64
    max_payload_bytes: Annotated[int, Field(strict=True, ge=128, le=16_777_216)] = 2_097_152


class GameRef(_Schema):
    backend: Literal["local", "official_offline", "official_gateway"]
    game_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    revision: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")


class GameScore(_Schema):
    normalized: Annotated[float, Field(ge=0, le=1)] | None
    raw: Annotated[float, Field(ge=0)] | None
    metric: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$")
    source: Literal["local", "official", "unavailable"]

    @model_validator(mode="after")
    def availability(self) -> Self:
        if self.source == "unavailable":
            if self.normalized is not None or self.raw is not None:
                raise ValueError("unavailable_score_has_value")
        elif self.normalized is None or self.raw is None:
            raise ValueError("available_score_missing_value")
        if self.source == "local" and (self.raw not in (0, 1) or self.normalized != self.raw):
            raise ValueError("local_score_not_binary")
        return self


class GameSession(Protocol):
    ref: GameRef

    @property
    def actions_used(self) -> int: ...
    async def observe(self) -> Observation: ...
    async def step(self, action: GameAction) -> Observation: ...
    async def finalize(self) -> GameScore: ...
    async def close(self) -> None: ...


class GameProvider(Protocol):
    async def discover(self) -> tuple[GameRef, ...]: ...
    async def open(self, ref: GameRef, *, limits: GameLimits) -> GameSession: ...


def decode_action(value: object, allowed: tuple[str, ...]) -> GameAction:
    """Decode one typed action; never translate the legacy string-batch format."""
    try:
        data = value.model_dump() if isinstance(value, GameAction) else value
        action = GameAction.model_validate(data)
    except (ValidationError, TypeError, ValueError):
        raise GameActionError("invalid_action") from None
    if action.name not in allowed:
        raise GameActionError("unavailable_action")
    return action


def decode_observation(payload: bytes, limits: GameLimits) -> Observation:
    """Check the transport byte bound before allocating JSON containers."""
    if len(payload) > limits.max_payload_bytes:
        raise GameProtocolError("observation_payload_limit")
    try:
        observation = Observation.model_validate_json(payload)
    except (ValidationError, ValueError):
        raise GameProtocolError("invalid_observation") from None
    if len(observation.frames) > limits.max_frames:
        raise GameProtocolError("observation_frame_limit")
    return observation


class ProbeEnvironment:
    """One cached, public-only probe on the same action-metered session.

    The probe is Stage 0 inference data, so it is kept small enough to send:
    each observation carries only its last frame (the settled state an action
    produced; earlier frames are that action's animation), and with a
    ``max_chars`` budget the probe stops taking actions, rather than failing,
    once another observation would not fit. ``max_payload_bytes`` stays the
    hard transport bound.
    """

    def __init__(
        self, session: GameSession, limits: GameLimits, *, max_chars: int | None = None
    ) -> None:
        if max_chars is not None and (type(max_chars) is not int or max_chars < 0):
            raise GameActionError("invalid_probe_limit")
        self.session = session
        self.limits = limits
        self.max_chars = max_chars
        self._cache: dict[str, JsonValue] | None = None
        self._started = False
        self._lock = asyncio.Lock()

    @staticmethod
    def _public(observation: Observation) -> dict[str, JsonValue]:
        public = observation.model_dump(mode="json")
        frames = public["frames"]
        assert isinstance(frames, list)
        public["frames"] = frames[-1:]
        return public

    def _fits(self, size: int) -> bool:
        return self.max_chars is None or size <= self.max_chars

    async def probe(self, max_actions: int = 3) -> dict[str, JsonValue]:
        if type(max_actions) is not int or max_actions < 0:
            raise GameActionError("invalid_probe_limit")
        async with self._lock:
            if self._cache is not None:
                return copy.deepcopy(self._cache)
            if self._started:
                raise GameSessionError("probe_failed")
            self._started = True
            observation = await self.session.observe()
            observations: list[JsonValue] = [self._public(observation)]
            actions: list[JsonValue] = []
            # Probe JSON is ASCII, so this byte count is also its character count.
            payload_size = len(
                json.dumps({"observations": observations, "actions": actions}).encode()
            )
            if payload_size > self.limits.max_payload_bytes or not self._fits(payload_size):
                raise GameProtocolError("probe_payload_limit")
            last_size = payload_size
            for _ in range(min(max_actions, self.limits.max_probe_actions)):
                choices = [
                    a for a in observation.available_actions if a not in ("RESET", "ACTION6")
                ]
                if observation.state in ("WIN", "GAME_OVER") or not choices:
                    break
                if not self._fits(payload_size + last_size):
                    # Another observation of the last size would not fit: spend no action.
                    break
                action = GameAction(name=choices[0])
                observation = await self.session.step(action)
                public_action = action.model_dump(mode="json")
                public_observation = self._public(observation)
                # Bound the cache incrementally, not after accumulating many large
                # frames: exactly what json.dumps adds, a ", " before every
                # element but the first action (the first observation always exists).
                added = len(json.dumps(public_action).encode()) + (2 if actions else 0)
                added += len(json.dumps(public_observation).encode()) + 2
                if payload_size + added > self.limits.max_payload_bytes:
                    raise GameProtocolError("probe_payload_limit")
                if not self._fits(payload_size + added):
                    # The action is spent, but the probe stays within its budget.
                    break
                payload_size += added
                last_size = added
                actions.append(public_action)
                observations.append(public_observation)
            result: dict[str, JsonValue] = {"observations": observations, "actions": actions}
            self._cache = result
            return copy.deepcopy(result)
