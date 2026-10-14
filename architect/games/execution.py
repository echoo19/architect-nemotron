"""Adaptive whole-game execution behind a revocable, host-owned runtime grant."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import deque
from collections.abc import Awaitable
from copy import deepcopy
from typing import Literal, NamedTuple, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator

from architect.games.evaluation import ReceiptLedger, task_for, task_key
from architect.games.grids import ClockBands
from architect.games.grids import grid_diff as _grid_diff  # shared with sandbox builtins
from architect.games.types import (
    ActionName,
    GameAction,
    GameActionError,
    GameLimits,
    GameProtocolError,
    GameProvider,
    GameRef,
    GameScore,
    GameSession,
    GameSessionError,
    GameUnavailableError,
    Observation,
    decode_action,
)
from architect.runtime import RunContext

GameStrategy = Literal["explorer", "planner", "hypothesis_tester"]
GAME_PRESETS = ("explorer", "planner", "hypothesis_tester")

# Fixed repair prefixes: they never quote the rejected reply.
_REPAIR_INVALID = "Repair the invalid decision. Return the required JSON only. "
_REPAIR_UNAVAILABLE = (
    "Repair the invalid decision. The action name must be one of "
    "observation.available_actions. Return the required JSON only. "
)
# Every prompt must leave room for its longest repair prefix, so a reply that
# needs repairing is never turned into a prompt-size failure.
_REPAIR_HEADROOM_BYTES = max(
    len(_REPAIR_INVALID.encode("utf-8")), len(_REPAIR_UNAVAILABLE.encode("utf-8"))
)


def decision_repairs(retries: int) -> int:
    """Repair calls one decision may consume: the tuned retries, but never zero."""
    return max(retries, 1)


def offered_actions(observation: Observation, *, fresh: bool) -> tuple[ActionName, ...]:
    """Actions the model may choose from; RESET is withheld while it would be a full reset.

    The engine treats RESET as a full reset when its per-level action count is
    zero: at the start of a game, immediately after a level transition, and
    immediately after a level RESET (which re-enters the level and zeroes the
    count again). The official worker refuses such a reset, so it is not offered
    on a ``fresh`` prompt unless it is the only action the environment allows.
    The first prompt of a run is always fresh: a session handed over already
    stepped (the Stage 0 probe) may have just completed a level, and the backend
    cannot read the engine's per-level count for steps it did not take.
    """
    actions = observation.available_actions
    if not fresh or "RESET" not in actions or len(actions) == 1:
        return actions
    return tuple(action for action in actions if action != "RESET")


async def cleanup_game_resources(operation: Awaitable[None]) -> None:
    """Drain mandatory teardown under a five-second grace, even after work expires.

    This grace permits only close/reap/unlink operations, never model calls,
    environment actions or scoring. Actual latency stays in run/factory wall time.
    Caller cancellation is propagated after teardown; no background task escapes.
    Official workers also enforce their own shorter two-second reap grace.
    """

    async def bounded() -> None:
        async with asyncio.timeout(5.0):
            await operation

    cleanup = asyncio.create_task(bounded())
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    try:
        cleanup.result()
    finally:
        if cancelled:
            raise asyncio.CancelledError


class GameExecutionPolicy(BaseModel):
    """Declarative policy bound by the authenticated Spec digest, not a grant."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    kind: Literal["game"] = "game"
    version: Literal[1] = 1
    small_model: str = Field(min_length=1, max_length=200)
    strategy: GameStrategy = "explorer"
    limits: GameLimits = Field(default_factory=GameLimits)
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    support_dataset: str = Field(min_length=1, max_length=200)
    score_source: Literal["local", "official"] = "local"
    score_policy: Literal["local_binary_v1", "official_percent_v1"] = "local_binary_v1"
    score_metric: Literal["local_win_v1", "relative_human_action_efficiency.percent.v1"] = (
        "local_win_v1"
    )
    # Frozen into the authenticated Spec; set only from the host opt-in.
    model_binding: Literal["offline", "online_preview"] = "offline"

    @model_validator(mode="after")
    def score_binding(self) -> Self:
        expected = "local_binary_v1" if self.score_source == "local" else "official_percent_v1"
        metric = (
            "local_win_v1"
            if self.score_source == "local"
            else "relative_human_action_efficiency.percent.v1"
        )
        if self.score_policy != expected or self.score_metric != metric:
            raise ValueError("game_score_policy_mismatch")
        return self


class GameDecision(BaseModel):
    """A decision is data only. No model-authored score is accepted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: GameAction
    plan: str | None = Field(default=None, min_length=1, max_length=4096)
    hypothesis: str | None = Field(default=None, min_length=1, max_length=4096)
    prediction: str | None = Field(default=None, min_length=1, max_length=4096)


def parse_decision(text: str, *, strategy: GameStrategy, maximum_bytes: int) -> GameDecision:
    if len(text) > maximum_bytes or len(text.encode("utf-8")) > maximum_bytes:
        raise GameProtocolError("game_decision_size")
    try:
        # Duplicate fields must not silently change the model's requested action.
        def unique(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
            result: dict[str, JsonValue] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate")
                result[key] = value
            return result

        payload = json.loads(text, object_pairs_hook=unique)
        decision = GameDecision.model_validate(payload)
        if strategy == "hypothesis_tester" and (not decision.hypothesis or not decision.prediction):
            raise ValueError("missing hypothesis")
        if strategy == "planner" and not decision.plan:
            raise ValueError("missing plan")
        return decision
    except (ValueError, ValidationError, RecursionError):
        raise GameProtocolError("game_decision_invalid") from None


def _action_key(action: JsonValue) -> str:
    """One label per distinct action, click coordinates included (public, host-made)."""
    if not isinstance(action, dict):
        return "?"
    name = str(action.get("name", "?"))
    if "x" in action or "y" in action:
        return f"{name}@{action.get('x')},{action.get('y')}"
    return name


_TRIED_LIMIT = 48
# The required text fields of each strategy's decision (``GameDecision``).
_TEXT_FIELDS: dict[str, tuple[str, ...]] = {
    "explorer": (),
    "planner": ("plan",),
    "hypothesis_tester": ("hypothesis", "prediction"),
}


def decision_schema(
    strategy: GameStrategy,
    offered: tuple[ActionName, ...],
    grid: tuple[tuple[int, ...], ...] | None = None,
) -> dict[str, JsonValue] | None:
    """The exact decision JSON Schema for one prompt, for constrained decoding.

    The action name is an enum of exactly the ``offered`` actions (RESET
    withholding included). ACTION6 is a separate variant that requires integer
    ``x`` (column) and ``y`` (row), bounded by ``grid`` when known and always by
    the public coordinate range; every other action forbids coordinates. The
    strategy's text fields are required strings. It is a decoding hint only:
    ``parse_decision`` and the offered-action check stay the authority, so an
    invalid reply is repaired or refused exactly as without it. ``None`` when
    nothing is offered (no valid decision exists to constrain to).

    Only keywords that both Ollama's grammar converter and OpenAI's strict
    ``json_schema`` mode accept are used (``minimum``/``maximum`` but no string
    length bounds; ``parse_decision`` still enforces those).
    """
    if not offered:
        return None
    variants: list[JsonValue] = []
    plain = [name for name in offered if name != "ACTION6"]
    if plain:
        variants.append(
            {
                "type": "object",
                "properties": {"name": {"type": "string", "enum": list(plain)}},
                "required": ["name"],
                "additionalProperties": False,
            }
        )
    if "ACTION6" in offered:
        rows = len(grid) if grid else 64
        columns = len(grid[0]) if grid and grid[0] else 64
        variants.append(
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "enum": ["ACTION6"]},
                    "x": {"type": "integer", "minimum": 0, "maximum": min(columns, 64) - 1},
                    "y": {"type": "integer", "minimum": 0, "maximum": min(rows, 64) - 1},
                },
                "required": ["name", "x", "y"],
                "additionalProperties": False,
            }
        )
    action: JsonValue = variants[0] if len(variants) == 1 else {"anyOf": variants}
    properties: dict[str, JsonValue] = {"action": action}
    for field in _TEXT_FIELDS[strategy]:
        properties[field] = {"type": "string"}
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def game_instructions(strategy: GameStrategy) -> str:
    """Fixed per-strategy instructions, including the exact decision shape parsed."""
    fields = {
        "hypothesis_tester": ', "hypothesis": "...", "prediction": "..."',
        "planner": ', "plan": "..."',
    }.get(strategy, "")
    text = (
        "Play this game using only its public observation. Reply with exactly one JSON "
        'object and nothing else, shaped like {"action": {"name": "ACTION1"}' + fields + "}. "
        "The action name must be one of observation.available_actions. ACTION6 also "
        'needs integer grid coordinates: {"action": {"name": "ACTION6", "x": 12, "y": 30}'
        + fields
        + "}. For ACTION6, x is the column and y the row of the grid cell you choose; "
        "the numbers in this example are placeholders, never a default. "
        "observation.frames holds the settled grid after the last action and "
        "observation.frame_count how many animation frames it produced. "
        "history[].changed is false when an action left the grid unchanged: do not "
        "repeat an action with the same parameters after it changed nothing. "
        "history[].diff gives how many cells changed and their bounding box. "
        "history[].revisited is true when the action returned the grid to a state "
        "already seen in this game: that is a loop, so choose an action you have not "
        "tried from that state instead. tried counts each distinct action so far; "
        "prefer actions and click targets you have not tried yet. "
        "Do not return a score or executable code. "
        "RESET is explicit, never automatic. RESET must never be the first action of a "
        "game or of a new level and must never directly follow another RESET; it is "
        "offered only when it is legal. "
    )
    if strategy == "hypothesis_tester":
        return text + (
            "hypothesis and prediction are required testable statements. Compare "
            "previous_test.prediction with observation; revise the hypothesis "
            "using this public feedback before choosing the next test action. "
        )
    if strategy == "planner":
        return text + "plan is required: a concrete plan revised after public feedback. "
    return text + "Explore adaptively; use public feedback to learn the rules. "


async def execute_game(
    context: RunContext,
    task: JsonValue,
    *,
    preset: str,
    params: dict[str, JsonValue],
    components: tuple[str, ...],
) -> JsonValue:
    """The preset entry point cannot obtain a session from a JSON identifier."""
    return await context.play_game(task, preset=preset, params=params, components=components)


class PlayCounters(NamedTuple):
    """Host-computed process metrics of one play; counts and codes only, no frames."""

    actions: int
    distinct_states: int
    revisits: int
    model_calls: int
    levels_completed: int
    # (action token, changed_cells, revisited) for the last _TRACE_LIMIT steps.
    trace: tuple[tuple[str, int, bool], ...]


_TRACE_LIMIT = 24
_CLOCK_NOTE = (
    "These border rows/columns change on almost every action whatever you do, like a "
    "step or energy bar. They are left out of history[].changed, diff and revisited; "
    "if the bar runs out the level may end."
)
_TERMINAL = frozenset({"WIN", "GAME_OVER"})
_UNSCORED_BACKENDS = frozenset({"local", "official_offline"})


def frame_digest(frame: tuple[tuple[int, ...], ...]) -> str:
    """64-hex sha256 of the canonical compact JSON of one settled frame."""
    return hashlib.sha256(json.dumps(frame, separators=(",", ":")).encode("ascii")).hexdigest()


def _changed_cells(
    before: tuple[tuple[int, ...], ...], after: tuple[tuple[int, ...], ...]
) -> int:
    """Cells that differ; a resized frame counts every cell of the new frame."""
    if len(before) != len(after) or any(
        len(a) != len(b) for a, b in zip(before, after, strict=True)
    ):
        return sum(len(row) for row in after)
    return sum(
        old != new
        for old_row, new_row in zip(before, after, strict=True)
        for old, new in zip(old_row, new_row, strict=True)
    )


class _Play:
    """One play of one game in one run: every host check, whole-run or per turn.

    ``GameBackend.run`` drives a play with its preset model loop; sandbox code
    drives one through ``GameBackend.open_turns`` with ``observe``/``step``.
    Both share the policy, task, grant, backend, action and score checks here,
    so neither path can skip one. The constructor applies every check that
    ``run`` applied before opening a session, in the same order.
    """

    def __init__(
        self,
        backend: GameBackend,
        context: RunContext,
        task: JsonValue,
        *,
        preset: str,
        params: dict[str, JsonValue],
        components: tuple[str, ...],
        scored: bool,
        turns: bool,
    ) -> None:
        backend._check(context)
        policy = backend.policy
        try:
            effective = GameExecutionPolicy.model_validate(context.execution_config)
        except ValueError:
            raise GameProtocolError("game_policy_invalid") from None
        if (
            effective != policy
            or preset != policy.strategy
            or context.adapter.model != policy.small_model
            or task_key(task) != task_key(context.original_task)
            or context.run_id in backend._runs
        ):
            raise GameProtocolError("game_policy_mismatch")
        from architect.presets import get_preset

        tunables = get_preset(preset).validate_params(params)
        retries = tunables.get("retries", 0)
        memory_bytes = tunables.get("context_window", 2048)
        if type(retries) is not int or type(memory_bytes) is not int:
            raise GameProtocolError("game_tuning_invalid")
        review = tunables.get("verifier_depth", 0) == 1 and bool(
            {"analysis_check", "review", "prediction"} & set(components)
        )
        if not turns:
            # Per-turn play is bounded by the sandbox's own dynamic op cap instead.
            context.limit_calls(
                policy.limits.max_turns * (decision_repairs(retries) + 1) * (2 if review else 1)
            )
        key = task_key(task)
        if key not in backend.evaluation_refs if context.is_evaluation else key != backend.target:
            raise GameSessionError("game_target_not_granted")
        if not isinstance(task, dict) or set(task) != {"game"}:
            raise GameProtocolError("game_task_invalid")
        try:
            ref = GameRef.model_validate(task["game"])
        except ValueError:
            raise GameProtocolError("game_task_invalid") from None
        expected_backends = (
            {"local"}
            if policy.score_source == "local"
            else {"official_offline"}
            if context.is_evaluation
            else {"official_offline", "official_gateway"}
        )
        if ref.backend not in expected_backends:
            raise GameProtocolError("game_score_backend_mismatch")
        if not scored and ref.backend not in _UNSCORED_BACKENDS:
            raise GameUnavailableError("forge_dryrun_unavailable")
        backend._runs.add(context.run_id)
        self.backend = backend
        self.context = context
        self.ref = ref
        self.scored = scored
        self.retries = retries
        self.memory_bytes = memory_bytes
        self.review = review
        self.session: GameSession | None = None
        self.observation: Observation | None = None
        self._task = task
        self._opened = False
        self._closed = False
        self._result: JsonValue = None
        self._finished = False
        # RESET withholding: the first prompt of a play is always fresh.
        self._first = True
        self._level_advanced = False
        self._reset_applied = False
        self._levels = 0
        # Process counters (host-side, content-free).
        self._steps = 0
        self._revisits = 0
        self._seen: set[str] = set()
        self._trace: deque[tuple[str, int, bool]] = deque(maxlen=_TRACE_LIMIT)
        self._calls_at_start = context.model_calls

    @property
    def counters(self) -> PlayCounters:
        return PlayCounters(
            actions=self._steps,
            distinct_states=len(self._seen),
            revisits=self._revisits,
            model_calls=max(0, self.context.model_calls - self._calls_at_start),
            levels_completed=self.observation.levels_completed if self.observation else 0,
            trace=tuple(self._trace),
        )

    @property
    def actions_used(self) -> int:
        return self.session.actions_used if self.session is not None else 0

    async def begin(self) -> Observation:
        """Open the granted session (or take the handed-over one) and observe it."""
        if self.observation is not None:
            return self.observation
        if self._opened or self._closed:
            raise GameSessionError("game_scope_expired")
        self._opened = True
        backend, context = self.backend, self.context
        backend._check(context)
        if not context.is_evaluation and backend.target_session is not None:
            self.session, backend.target_session = backend.target_session, None
        else:
            self.session = await backend.provider.open(self.ref, limits=backend.policy.limits)
        backend._check(context)
        observation = await self.session.observe()
        backend._check(context)
        self.observation = observation
        self._levels = observation.levels_completed
        self._seen.add(frame_digest(observation.frames[-1]))
        self._calls_at_start = context.model_calls
        return observation

    def offered(self) -> tuple[ActionName, ...]:
        assert self.observation is not None
        return offered_actions(
            self.observation, fresh=self._first or self._level_advanced or self._reset_applied
        )

    def actions_left(self) -> int:
        """Steps this play may still send; 0 once the game is in a terminal state."""
        if self.observation is None or self.observation.state in _TERMINAL:
            return 0
        limits = self.backend.policy.limits
        return max(0, min(limits.max_actions - self.actions_used, limits.max_turns - self._steps))

    async def apply(self, action: GameAction) -> Observation:
        """Send one already-validated action; the only path that steps the session."""
        assert self.session is not None and self.observation is not None
        if self.session.actions_used >= self.backend.policy.limits.max_actions:
            raise GameSessionError("game_action_limit")
        self.backend._check(self.context)
        before = self.observation.frames[-1]
        observation = await self.session.step(action)
        self.backend._check(self.context)
        # The engine zeroes its action count on a level transition and on a
        # level RESET, so the next RESET would be a full reset; hide it for
        # exactly one prompt in either case.
        self._first = False
        self._level_advanced = observation.levels_completed > self._levels
        self._levels = observation.levels_completed
        self._reset_applied = action.name == "RESET"
        self.observation = observation
        after = observation.frames[-1]
        digest = frame_digest(after)
        revisited = digest in self._seen
        self._seen.add(digest)
        self._steps += 1
        self._revisits += revisited
        self._trace.append(
            (
                _action_key(action.model_dump(mode="json", exclude_none=True)),
                _changed_cells(before, after),
                revisited,
            )
        )
        return observation

    def _public(self, *, refused: bool) -> JsonValue:
        observation = self.observation
        assert observation is not None
        return {
            "frame": [list(row) for row in observation.frames[-1]],
            "frame_count": len(observation.frames),
            "state": observation.state,
            "available_actions": list(self.offered()),
            "levels_completed": observation.levels_completed,
            "win_levels": observation.win_levels,
            "actions_taken": self._steps,
            "actions_left": self.actions_left(),
            "refused": refused,
        }

    async def observe(self) -> JsonValue:
        """The settled observation JSON; free, never counted as an action."""
        await self.begin()
        self.backend._check(self.context)
        return self._public(refused=False)

    async def step(self, action: JsonValue) -> JsonValue:
        """Decode against the offered actions, then send; refuse without sending when done."""
        await self.begin()
        self.backend._check(self.context)
        if self.actions_left() == 0:
            return self._public(refused=True)
        await self.apply(decode_action(action, self.offered()))
        return self._public(refused=False)

    async def finish(self) -> JsonValue:
        """Exactly what ``GameBackend.run`` returns; closes the session.

        An unscored play (a dry run) never calls the score path and returns
        ``{"scored": false}``. A second call returns the first result.
        """
        if self._finished:
            return deepcopy(self._result)
        if self._closed:
            raise GameSessionError("game_scope_expired")
        backend, context = self.backend, self.context
        try:
            await self.begin()
            assert self.session is not None
            backend._check(context)
            result: JsonValue
            if not self.scored:
                result = {"scored": False}
            else:
                score = await self.session.finalize()
                backend._check(context)
                if score.normalized is None or score.source == "unavailable":
                    raise GameUnavailableError("game_score_unavailable")
                # Revalidate host results; never read a model-authored score.
                score = GameScore.model_validate(score.model_dump(mode="json"))
                policy = backend.policy
                if score.source != policy.score_source or score.metric != policy.score_metric:
                    raise GameProtocolError("game_score_source_mismatch")
                if policy.score_policy == "official_percent_v1" and (
                    score.raw is None or score.raw > 100 or score.normalized != score.raw / 100
                ):
                    raise GameProtocolError("game_score_scale_mismatch")
                backend.completed[context.run_id] = score
                if context.is_evaluation:
                    result = backend.ledger.issue(
                        context, self._task, session_id=uuid4().hex, score=score
                    )
                else:
                    result = {"score": score.model_dump(mode="json")}
            self._result, self._finished = result, True
            return deepcopy(result)
        finally:
            await self.close()

    async def close(self) -> None:
        """Record counters and release the session exactly once (bounded teardown)."""
        if self._closed:
            return
        self._closed = True
        backend = self.backend
        backend.last_counters = self.counters
        backend.last_final_state = self.observation.state if self.observation else None
        backend.last_run_id = self.context.run_id
        session, self.session = self.session, None
        if session is not None:
            backend.actions_used += session.actions_used
            await cleanup_game_resources(session.close())


class GameBackend:
    """Session owner used only inside Runtime.use_game_backend.

    The public task selects among host-granted references, never arbitrary files.
    Sessions and evidence remain in scoped memory and are cleared on close.
    """

    def __init__(
        self,
        provider: GameProvider,
        policy: GameExecutionPolicy,
        ledger: ReceiptLedger,
        *,
        evaluation_refs: tuple[GameRef, ...],
        target: GameRef,
        target_session: GameSession | None = None,
    ) -> None:
        self.provider = provider
        self.policy = GameExecutionPolicy.model_validate(policy.model_dump(mode="json"))
        self.ledger = ledger
        self.evaluation_refs = frozenset(task_key(task_for(ref)) for ref in evaluation_refs)
        self.target = task_key(task_for(target))
        self.target_session = target_session
        self.active = True
        self.actions_used = 0
        self.completed: dict[str, GameScore] = {}
        self._runs: set[str] = set()
        # Of the most recent play in this backend instance (preset or sandbox).
        self.last_counters: PlayCounters | None = None
        self.last_final_state: str | None = None
        self.last_run_id: str | None = None

    def _check(self, context: RunContext) -> None:
        context.check_game_grant()
        if not self.active:
            raise GameSessionError("game_scope_expired")

    async def close(self) -> None:
        self.active = False
        self.ledger.close()
        self.completed.clear()
        self._runs.clear()
        session, self.target_session = self.target_session, None
        if session is not None:
            await session.close()

    def open_turns(
        self,
        context: RunContext,
        task: JsonValue,
        *,
        preset: str,
        params: dict[str, JsonValue],
        components: tuple[str, ...],
        scored: bool = True,
    ) -> _Play:
        """A per-turn play under exactly the checks ``run`` applies; opened lazily."""
        return _Play(
            self,
            context,
            task,
            preset=preset,
            params=params,
            components=components,
            scored=scored,
            turns=True,
        )

    async def run(
        self,
        context: RunContext,
        task: JsonValue,
        *,
        preset: str,
        params: dict[str, JsonValue],
        components: tuple[str, ...],
    ) -> JsonValue:
        play = _Play(
            self,
            context,
            task,
            preset=preset,
            params=params,
            components=components,
            scored=True,
            turns=False,
        )
        retries, memory_bytes, review = play.retries, play.memory_bytes, play.review
        try:
            observation = await play.begin()
            feedback: JsonValue = None
            history: list[JsonValue] = []
            # Public, host-computed exploration state (never model-authored).
            # Frames are compared with any detected clock band blanked (a step or
            # energy bar on an edge): otherwise every frame is new and no-ops change.
            clock = ClockBands()
            frames_seen: list[tuple[tuple[int, ...], ...]] = [observation.frames[-1]]
            seen: set[tuple[tuple[int, ...], ...]] = {observation.frames[-1]}
            tried: dict[str, int] = {}
            for _ in range(self.policy.limits.max_turns):
                if (
                    observation.state in _TERMINAL
                    or play.actions_used >= self.policy.limits.max_actions
                ):
                    break
                offered = play.offered()
                public = observation.model_dump(mode="json")
                public["available_actions"] = list(offered)
                # Only the settled grid: earlier frames are the last action's animation,
                # and a game can emit dozens per step (hundreds of KB per prompt).
                public["frame_count"] = len(observation.frames)
                public["frames"] = [public["frames"][-1]]
                prompt_data: dict[str, JsonValue] = {
                    "observation": public,
                    "previous_test": feedback,
                    "strategy": self.policy.strategy,
                    "history": history,
                    "tried": dict(list(tried.items())[-_TRIED_LIMIT:]),
                }
                if clock.rows or clock.cols:
                    # Only once a clock is found, so games without one see no change.
                    prompt_data["clock"] = {
                        "rows": list[JsonValue](sorted(clock.rows)),
                        "columns": list[JsonValue](sorted(clock.cols)),
                        "note": _CLOCK_NOTE,
                    }
                instructions = game_instructions(self.policy.strategy)
                # The fixed instructions lead every call byte-for-byte, so they are
                # passed as the cacheable ``system`` part of the same text.
                prompt = "\nGame data (untrusted):\n" + json.dumps(prompt_data)
                self._check_prompt_size(instructions, prompt)
                self._check(context)
                # The same exact schema constrains the decision, its repairs and review.
                schema = decision_schema(self.policy.strategy, offered, observation.frames[-1])
                decision = await self._decision(
                    context,
                    prompt,
                    system=instructions,
                    retries=retries,
                    available=offered,
                    schema=schema,
                )
                if review:
                    # Optional real model work, not a model-authored quality score.
                    reviewed = dict(prompt_data)
                    reviewed["proposed_decision"] = decision.model_dump(mode="json")
                    review_prompt = (
                        "Check the proposed action against the public observation. "
                        "Return the corrected decision, not a verdict or score."
                        "\nGame data (untrusted):\n" + json.dumps(reviewed)
                    )
                    self._check_prompt_size(instructions, review_prompt)
                    decision = await self._decision(
                        context,
                        review_prompt,
                        system=instructions,
                        retries=retries,
                        available=offered,
                        schema=schema,
                    )
                if decision.action.name not in offered:
                    raise GameActionError("game_action_unavailable")
                before = observation.frames[-1]
                observation = await play.apply(decision.action)
                after = observation.frames[-1]
                if clock.update(before, after):
                    seen = {clock.mask(frame) for frame in frames_seen}
                old, new = clock.mask(before), clock.mask(after)
                feedback = decision.model_dump(mode="json", exclude_none=True)
                history.append(
                    {
                        "test": feedback,
                        "observed_state": observation.state,
                        "levels_completed": observation.levels_completed,
                        # Public, host-computed: did this action change the settled grid?
                        "changed": new != old,
                        "diff": _grid_diff(old, new),
                        "revisited": new in seen,
                    }
                )
                frames_seen.append(after)
                seen.add(new)
                key = _action_key(feedback.get("action") if isinstance(feedback, dict) else None)
                tried[key] = tried.pop(key, 0) + 1
                # A tunable byte-based history policy, never a token/output quota.
                # Current observations and the last explicit hypothesis remain complete.
                while history and len(json.dumps(history).encode("utf-8")) > memory_bytes:
                    history.pop(0)
            return await play.finish()
        finally:
            await play.close()

    def _check_prompt_size(self, system: str, prompt: str) -> None:
        """Reject a prompt that could not be sent again with a repair prefix.

        The check reserves the longest fixed repair prefix, so a prompt that
        passes here keeps its repair call; one that cannot fit fails with the
        fixed code before any model call is spent.
        """
        size = _REPAIR_HEADROOM_BYTES + len((system + prompt).encode("utf-8"))
        if size > self.policy.limits.max_payload_bytes:
            raise GameProtocolError("game_prompt_size")

    async def _decision(
        self,
        context: RunContext,
        prompt: str,
        *,
        system: str = "",
        retries: int,
        available: tuple[ActionName, ...] | None = None,
        schema: dict[str, JsonValue] | None = None,
    ) -> GameDecision:
        """The model is given ``system + prompt``, exactly one concatenated text.

        A malformed reply and an action outside ``available`` each get at least
        one fixed repair prefix, even when the tuned ``retries`` is 0, because a
        single formatting slip or one stray action name must not end a whole-game
        rollout (the presets' shared retries grid tunes the first candidate at
        0). Exactly ``decision_repairs(retries)`` repair calls are made at most,
        the same count ``run`` reserves in its model-call bound, and the fixed
        failure code is raised after the last one. No model text is ever echoed
        into a prompt or a failure code.

        ``schema`` (``decision_schema``) is sent with every attempt, repairs
        included, as a constrained-decoding hint; it never replaces the parse.
        """
        base = system
        repairs = decision_repairs(retries)
        for attempt in range(repairs + 1):
            self._check(context)
            # The run loop reserves repair headroom up front; this bound keeps the
            # per-call limit exact for any other caller.
            if len((system + prompt).encode("utf-8")) > self.policy.limits.max_payload_bytes:
                raise GameProtocolError("game_prompt_size")
            text = await context.call(prompt, system=system, response_schema=schema)
            self._check(context)
            try:
                decision = parse_decision(
                    text,
                    strategy=self.policy.strategy,
                    maximum_bytes=self.policy.limits.max_payload_bytes,
                )
                if available is not None and decision.action.name not in available:
                    raise GameActionError("game_action_unavailable")
                return decision
            except (GameProtocolError, GameActionError) as exc:
                if attempt == repairs:
                    raise
                unavailable = isinstance(exc, GameActionError)
                system = (_REPAIR_UNAVAILABLE if unavailable else _REPAIR_INVALID) + base
        raise AssertionError("unreachable")
