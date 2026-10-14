"""Phase 9: per-turn game play (``_Play``) has parity with the whole-run ``GameBackend.run``.

Every check GameBackend.run applies (policy, task key, one play per run, grant,
score backend, offered actions and RESET withholding, max_actions/max_turns and
score source) is shared by ``open_turns``; these tests drive both paths on the
same fixtures. Harnesses here are trusted Python standing in for sandbox code.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import JsonValue

from architect.games.evaluation import ReceiptLedger, task_for
from architect.games.execution import (
    GameBackend,
    GameExecutionPolicy,
    PlayCounters,
    execute_game,
    frame_digest,
)
from architect.games.local import LocalGameProvider
from architect.games.types import (
    ActionName,
    GameAction,
    GameLimits,
    GameProtocolError,
    GameRef,
    GameScore,
    GameSession,
    GameUnavailableError,
    Observation,
)
from architect.models import FakeAdapter, ModelRequest, ModelResponse
from architect.models.types import UsageQuote
from architect.runtime import RunContext, Runtime, _GameScope, _host_game_output
from architect.runtime.evidence import scoped_evaluation
from architect.runtime.sandbox import SandboxError
from architect.spec import Budget, FloorDefinition, FloorKind, Spec
from architect.telemetry import TelemetryStore

DIGEST = "a" * 64


def setup(
    tmp_path: Path,
    limits: GameLimits | None = None,
    *,
    index: int = 0,
    provider: LocalGameProvider | None = None,
) -> tuple[Runtime, GameBackend, Spec, GameRef]:
    local = LocalGameProvider()
    target = local.manifest.refs("delivery")[index]
    policy = GameExecutionPolicy(
        small_model="fake",
        manifest_digest=local.manifest.digest,
        support_dataset="support",
        limits=limits or GameLimits(max_actions=24, max_turns=24),
    )
    backend = GameBackend(
        provider or local, policy, ReceiptLedger(), evaluation_refs=(target,), target=target
    )
    spec = Spec(
        task="Play",
        input_schema={},
        output_schema={},
        domain="game",
        model_target="fake",
        budget=Budget(cost_usd=1, compute_s=30),
        preset_hint="explorer",
        execution_config=policy.model_dump(mode="json"),
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_GAME_SCORE, limit=1, dataset="support"),
    )
    return Runtime(TelemetryStore(tmp_path / "runs")), backend, spec, target


class ScriptedSession:
    """A never-terminal session with a fixed one-cell frame (tests/test_game_execution.py)."""

    def __init__(
        self, ref: GameRef, actions: tuple[ActionName, ...], levels_of: Callable[[int], int]
    ) -> None:
        self.ref = ref
        self.actions = actions
        self.levels_of = levels_of
        self.count = 0
        self.log: list[ActionName] = []

    @property
    def actions_used(self) -> int:
        return self.count

    def _observation(self) -> Observation:
        return Observation(
            frames=(((0,),),),
            state="NOT_FINISHED",
            available_actions=self.actions,
            levels_completed=self.levels_of(self.count),
            win_levels=2,
        )

    async def observe(self) -> Observation:
        return self._observation()

    async def step(self, action: GameAction) -> Observation:
        self.count += 1
        self.log.append(action.name)
        return self._observation()

    async def finalize(self) -> GameScore:
        return GameScore(normalized=0, raw=0, metric="local_win_v1", source="local")

    async def close(self) -> None:
        return None


class ScriptedProvider(LocalGameProvider):
    def __init__(
        self, target: GameRef, actions: tuple[ActionName, ...], levels_of: Callable[[int], int]
    ) -> None:
        super().__init__()
        self.target = target
        self.actions = actions
        self.levels_of = levels_of
        self.last: ScriptedSession | None = None

    async def open(self, ref: GameRef, *, limits: GameLimits) -> GameSession:
        self.last = ScriptedSession(self.target, self.actions, self.levels_of)
        return self.last


def scripted_provider(
    target: GameRef, actions: tuple[ActionName, ...], levels_of: Callable[[int], int]
) -> ScriptedProvider:
    return ScriptedProvider(target, actions, levels_of)


def choose(grid: list[list[int]]) -> str:
    """The public-frame solver of tests/test_game_factory.py (fixture agent)."""
    if any(value >= 4 for value in grid[0]):
        progress = sum(value == 3 for value in grid[1])
        return f"ACTION{grid[0][progress] - 3}"
    if 2 not in [value for row in grid for value in row]:
        for bit in range(3):
            if grid[0][bit] != grid[1][bit]:
                return f"ACTION{bit + 1}"
        return "ACTION4"
    start = next((x, y) for y, row in enumerate(grid) for x, value in enumerate(row) if value == 2)
    goal = next((x, y) for y, row in enumerate(grid) for x, value in enumerate(row) if value == 3)
    queue = deque([(start, "")])
    visited = {start}
    moves = ((0, -1, "ACTION1"), (0, 1, "ACTION2"), (-1, 0, "ACTION3"), (1, 0, "ACTION4"))
    while queue:
        (x, y), first = queue.popleft()
        if (x, y) == goal:
            return first
        for dx, dy, action in moves:
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


class Solver(FakeAdapter):
    """Whole-run model: reads the public grid in the prompt and solves it."""

    def __init__(self, *, wrong: bool = False) -> None:
        super().__init__()
        self.wrong = wrong
        self.prompts: list[str] = []

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=10, tokens_out=10, cost_usd=0)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        self.prompts.append(request.prompt)
        data = json.loads(request.prompt.split("Game data (untrusted):\n", 1)[1])
        action = "ACTION1" if self.wrong else choose(data["observation"]["frames"][-1])
        return ModelResponse(
            text=json.dumps({"action": {"name": action}}),
            model=self.model,
            tokens_in=10,
            tokens_out=10,
            cost_usd=0,
        )


async def preset_harness(context: RunContext, task: JsonValue) -> JsonValue:
    return await execute_game(context, task, preset="explorer", params={}, components=())


def turn_harness(
    policy: Callable[[list[list[int]]], str] = choose,
    *,
    observations: list[JsonValue] | None = None,
) -> Callable[[RunContext, JsonValue], object]:
    async def play(context: RunContext, task: JsonValue) -> JsonValue:
        observation = await context.game_observe()
        while True:
            if observations is not None:
                observations.append(observation)
            assert isinstance(observation, dict)
            if observation["actions_left"] == 0:
                break
            frame = observation["frame"]
            assert isinstance(frame, list)
            observation = await context.game_step({"name": policy(frame)})  # type: ignore[arg-type]
        return await context.game_finish()

    return play


async def evaluate(
    runtime: Runtime,
    backend: GameBackend,
    spec: Spec,
    target: GameRef,
    harness: Callable[[RunContext, JsonValue], object],
    adapter: FakeAdapter | None = None,
) -> float:
    task = task_for(target)
    with scoped_evaluation(DIGEST):
        async with runtime.use_game_backend(backend):
            result = await runtime.evaluate(
                spec,
                harness,  # type: ignore[arg-type]
                task,
                adapter or FakeAdapter(),
                preset="explorer",
                candidate_digest=DIGEST,
            )
            return backend.ledger.consume(task, result.output)


@pytest.mark.parametrize("index", [0, 1, 2])
@pytest.mark.parametrize("wrong", [False, True])
async def test_run_and_scripted_turns_finish_with_identical_scores(
    tmp_path: Path, index: int, wrong: bool
) -> None:
    policy: Callable[[list[list[int]]], str] = (lambda _: "ACTION1") if wrong else choose
    runtime, backend, spec, target = setup(tmp_path / "run", index=index)
    solver = Solver(wrong=wrong)
    whole = await evaluate(runtime, backend, spec, target, preset_harness, solver)
    whole_counters = backend.last_counters
    await backend.close()

    runtime, turns_backend, spec, target = setup(tmp_path / "turns", index=index)
    per_turn = await evaluate(runtime, turns_backend, spec, target, turn_harness(policy))
    turn_counters = turns_backend.last_counters
    await turns_backend.close()

    assert whole == per_turn == (0.0 if wrong else 1.0)
    assert backend.actions_used == turns_backend.actions_used
    assert whole_counters is not None and turn_counters is not None
    assert whole_counters._replace(model_calls=0) == turn_counters
    assert whole_counters.model_calls == solver.calls and turn_counters.model_calls == 0


async def test_offered_actions_and_reset_withholding_match_run(tmp_path: Path) -> None:
    limits = GameLimits(max_actions=4, max_turns=4)
    runtime, backend, spec, target = setup(tmp_path / "run", limits)
    backend.provider = scripted_provider(target, ("RESET", "ACTION1"), lambda n: min(1, n // 2))

    class Recorder(Solver):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            self.prompts.append(request.prompt)
            self.calls += 1
            return ModelResponse(
                text='{"action":{"name":"ACTION1"}}',
                model="fake",
                tokens_in=1,
                tokens_out=1,
                cost_usd=0,
            )

    recorder = Recorder()
    await evaluate(runtime, backend, spec, target, preset_harness, recorder)
    whole = [
        json.loads(p.split("Game data (untrusted):\n", 1)[1])["observation"]["available_actions"]
        for p in recorder.prompts
    ]
    await backend.close()

    runtime, backend, spec, target = setup(tmp_path / "turns", limits)
    backend.provider = scripted_provider(target, ("RESET", "ACTION1"), lambda n: min(1, n // 2))
    seen: list[JsonValue] = []
    await evaluate(
        runtime, backend, spec, target, turn_harness(lambda _: "ACTION1", observations=seen)
    )
    per_turn = [item["available_actions"] for item in seen if isinstance(item, dict)][:4]
    await backend.close()
    assert (
        whole == per_turn == [["ACTION1"], ["RESET", "ACTION1"], ["ACTION1"], ["RESET", "ACTION1"]]
    )


async def test_withheld_reset_is_invalid_call_and_nothing_is_sent(tmp_path: Path) -> None:
    runtime, backend, spec, target = setup(tmp_path)
    scripted = scripted_provider(target, ("RESET", "ACTION1"), lambda _: 0)
    backend.provider = scripted

    async def play(context: RunContext, task: JsonValue) -> JsonValue:
        await context.game_observe()
        return await context.game_step({"name": "RESET"})

    with pytest.raises(SandboxError, match="sandbox_invalid_call"):
        await evaluate(runtime, backend, spec, target, play)
    assert scripted.last is not None and scripted.last.log == []
    assert backend.actions_used == 0 and not backend.completed
    await backend.close()


@pytest.mark.parametrize("action", [{"name": "ACTION9"}, {"name": "ACTION6"}, "ACTION1", None])
async def test_malformed_or_unoffered_action_is_invalid_call(
    tmp_path: Path, action: JsonValue
) -> None:
    runtime, backend, spec, target = setup(tmp_path)

    async def play(context: RunContext, task: JsonValue) -> JsonValue:
        return await context.game_step(action)

    with pytest.raises(SandboxError, match="sandbox_invalid_call"):
        await evaluate(runtime, backend, spec, target, play)
    assert backend.actions_used == 0
    await backend.close()


@pytest.mark.parametrize(
    "limits,sent",
    [(GameLimits(max_actions=2, max_turns=5), 2), (GameLimits(max_actions=5, max_turns=2), 2)],
)
async def test_max_actions_and_turns_refuse_without_sending(
    tmp_path: Path, limits: GameLimits, sent: int
) -> None:
    runtime, backend, spec, target = setup(tmp_path, limits)
    scripted = scripted_provider(target, ("ACTION1", "ACTION2"), lambda _: 0)
    backend.provider = scripted
    results: list[JsonValue] = []

    async def play(context: RunContext, task: JsonValue) -> JsonValue:
        for _ in range(4):
            results.append(await context.game_step({"name": "ACTION1"}))
        return await context.game_finish()

    await evaluate(runtime, backend, spec, target, play)
    refused = [item["refused"] for item in results if isinstance(item, dict)]
    assert refused == [False] * sent + [True] * (4 - sent)
    last = results[-1]
    assert isinstance(last, dict) and last["actions_left"] == 0 and last["actions_taken"] == sent
    assert scripted.last is not None and len(scripted.last.log) == sent
    assert backend.actions_used == sent
    await backend.close()


async def test_step_after_terminal_state_is_refused_and_nothing_is_sent(tmp_path: Path) -> None:
    runtime, backend, spec, target = setup(tmp_path)
    after: list[JsonValue] = []

    async def play(context: RunContext, task: JsonValue) -> JsonValue:
        observation = await context.game_observe()
        while isinstance(observation, dict) and observation["state"] != "WIN":
            frame = observation["frame"]
            assert isinstance(frame, list)
            observation = await context.game_step({"name": choose(frame)})  # type: ignore[arg-type]
        assert isinstance(observation, dict) and observation["actions_left"] == 0
        used = backend.actions_used
        after.append(await context.game_step({"name": "ACTION1"}))
        after.append(used)
        return await context.game_finish()

    assert await evaluate(runtime, backend, spec, target, play) == 1.0
    refused, _ = after
    assert isinstance(refused, dict) and refused["refused"] is True
    assert refused["state"] == "WIN"
    counters = backend.last_counters
    assert counters is not None and counters.actions == backend.actions_used
    await backend.close()


async def test_second_play_in_the_same_run_is_refused(tmp_path: Path) -> None:
    runtime, backend, spec, target = setup(tmp_path)

    async def preset_then_turns(context: RunContext, task: JsonValue) -> JsonValue:
        await preset_harness(context, task)
        return await context.game_observe()

    with pytest.raises(GameProtocolError, match="game_policy_mismatch"):
        await evaluate(runtime, backend, spec, target, preset_then_turns, Solver())

    async def turns_then_preset(context: RunContext, task: JsonValue) -> JsonValue:
        await context.game_observe()
        return await preset_harness(context, task)

    with pytest.raises(GameProtocolError, match="game_policy_mismatch"):
        await evaluate(runtime, backend, spec, target, turns_then_preset, Solver())

    async def two_turn_plays(context: RunContext, task: JsonValue) -> JsonValue:
        backend.open_turns(context, task, preset="explorer", params={}, components=())
        backend.open_turns(context, task, preset="explorer", params={}, components=())
        return None

    with pytest.raises(GameProtocolError, match="game_policy_mismatch"):
        await evaluate(runtime, backend, spec, target, two_turn_plays)
    await backend.close()


async def test_task_key_and_preset_mismatch_are_refused_before_open(tmp_path: Path) -> None:
    runtime, backend, spec, target = setup(tmp_path)
    other = LocalGameProvider().manifest.refs("delivery")[1]

    async def substituted(context: RunContext, task: JsonValue) -> JsonValue:
        backend.open_turns(context, task_for(other), preset="explorer", params={}, components=())
        return None

    async def wrong_preset(context: RunContext, task: JsonValue) -> JsonValue:
        backend.open_turns(context, task, preset="planner", params={}, components=())
        return None

    for harness in (substituted, wrong_preset):
        with pytest.raises(GameProtocolError, match="game_policy_mismatch"):
            await evaluate(runtime, backend, spec, target, harness)
    assert backend.actions_used == 0
    await backend.close()


class WrongSourceProvider(LocalGameProvider):
    """Every session scores as an official card: a local policy must refuse it."""

    def __init__(self) -> None:
        super().__init__()
        self.finalized = 0

    async def open(self, ref: GameRef, *, limits: GameLimits) -> GameSession:
        session = await super().open(ref, limits=limits)
        provider = self

        class Wrapped:
            def __init__(self) -> None:
                self.ref = session.ref

            @property
            def actions_used(self) -> int:
                return session.actions_used

            async def observe(self) -> Observation:
                return await session.observe()

            async def step(self, action: GameAction) -> Observation:
                return await session.step(action)

            async def finalize(self) -> GameScore:
                provider.finalized += 1
                return GameScore(
                    normalized=0.5,
                    raw=50,
                    metric="relative_human_action_efficiency.percent.v1",
                    source="official",
                )

            async def close(self) -> None:
                await session.close()

        return Wrapped()


async def test_score_source_is_checked_on_both_paths(tmp_path: Path) -> None:
    for name, harness, adapter in (
        ("run", preset_harness, Solver()),
        ("turns", turn_harness(), None),
    ):
        provider = WrongSourceProvider()
        runtime, backend, spec, target = setup(tmp_path / name, provider=provider)
        with pytest.raises(GameProtocolError, match="game_score_source_mismatch"):
            await evaluate(runtime, backend, spec, target, harness, adapter)
        assert provider.finalized == 1 and not backend.completed
        await backend.close()


class NeverScoredProvider(LocalGameProvider):
    async def open(self, ref: GameRef, *, limits: GameLimits) -> GameSession:
        session = await super().open(ref, limits=limits)

        async def refuse() -> GameScore:
            raise AssertionError("an unscored play must never reach the score path")

        session.finalize = refuse  # type: ignore[method-assign]
        return session


async def test_unscored_play_never_calls_the_score_path(tmp_path: Path) -> None:
    runtime, backend, spec, target = setup(tmp_path, provider=NeverScoredProvider())
    outputs: list[JsonValue] = []

    async def play(context: RunContext, task: JsonValue) -> JsonValue:
        turns = backend.open_turns(
            context, task, preset="explorer", params={}, components=(), scored=False
        )
        observation = await turns.observe()
        assert isinstance(observation, dict)
        await turns.step({"name": "ACTION1"})
        outputs.append(await turns.finish())
        outputs.append(await turns.finish())  # idempotent after close
        return outputs[0]

    task = task_for(target)
    with scoped_evaluation(DIGEST):
        async with runtime.use_game_backend(backend):
            result = await runtime.evaluate(
                spec, play, task, FakeAdapter(), preset="explorer", candidate_digest=DIGEST
            )
    assert result.output == {"scored": False} and outputs == [{"scored": False}] * 2
    assert not backend.completed and not backend.ledger._receipts
    assert backend.actions_used == 1
    await backend.close()


def test_unscored_play_refuses_a_gateway_session() -> None:
    gateway = GameRef(backend="official_gateway", game_id="ls20", revision="r1")
    policy = GameExecutionPolicy(
        small_model="fake",
        manifest_digest="b" * 64,
        support_dataset="support",
        score_source="official",
        score_policy="official_percent_v1",
        score_metric="relative_human_action_efficiency.percent.v1",
    )
    backend = GameBackend(
        LocalGameProvider(), policy, ReceiptLedger(), evaluation_refs=(), target=gateway
    )
    spec = Spec(
        task="Play",
        input_schema={},
        output_schema={},
        model_target="fake",
        budget=Budget(),
        execution_config=policy.model_dump(mode="json"),
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_GAME_SCORE, limit=1, dataset="support"),
    )
    task = task_for(gateway)

    def context(run_id: str) -> RunContext:
        return RunContext(
            FakeAdapter(),
            Budget(),
            is_evaluation=False,
            spec=spec,
            original_task=task,
            run_id=run_id,
            game_scope=_GameScope(0, backend),
            preset="explorer",
        )

    with pytest.raises(GameUnavailableError, match="forge_dryrun_unavailable"):
        backend.open_turns(
            context("r1"), task, preset="explorer", params={}, components=(), scored=False
        )
    # The scored per-turn delivery of the same target passes every check.
    backend.open_turns(context("r2"), task, preset="explorer", params={}, components=())


async def test_counters_on_fixtures(tmp_path: Path) -> None:
    runtime, backend, spec, target = setup(tmp_path, GameLimits(max_actions=30, max_turns=30))
    # The scripted engine never changes its single-cell frame: every step revisits.
    backend.provider = scripted_provider(target, ("ACTION1", "ACTION2", "ACTION6"), lambda _: 0)
    script: list[JsonValue] = [
        {"name": "ACTION1"},
        {"name": "ACTION2"},
        {"name": "ACTION6", "x": 3, "y": 4},
    ]
    script += [{"name": "ACTION1"}] * 27

    async def play(context: RunContext, task: JsonValue) -> JsonValue:
        for action in script:
            await context.game_step(action)
        return await context.game_finish()

    await evaluate(runtime, backend, spec, target, play)
    counters = backend.last_counters
    assert isinstance(counters, PlayCounters)
    assert counters.actions == 30 and counters.distinct_states == 1 and counters.revisits == 30
    assert counters.levels_completed == 0 and counters.model_calls == 0
    assert len(counters.trace) == 24
    assert counters.trace[-1] == ("ACTION1", 0, True)
    await backend.close()

    runtime, backend, spec, target = setup(tmp_path / "nav", GameLimits(max_actions=3, max_turns=3))
    moves: list[JsonValue] = [{"name": "ACTION2"}, {"name": "ACTION1"}, {"name": "ACTION2"}]

    async def navigate(context: RunContext, task: JsonValue) -> JsonValue:
        for action in moves:
            await context.game_step(action)
        return await context.game_finish()

    await evaluate(runtime, backend, spec, target, navigate)
    counters = backend.last_counters
    assert counters is not None
    # Down, up (back to the start: a revisit), down (a revisit of the second state).
    assert counters.distinct_states == 2 and counters.revisits == 2
    assert [item[0] for item in counters.trace] == ["ACTION2", "ACTION1", "ACTION2"]
    assert [item[2] for item in counters.trace] == [False, True, True]
    assert all(item[1] == 2 for item in counters.trace)
    assert backend.last_final_state == "NOT_FINISHED"
    await backend.close()


async def test_preset_runs_record_process_counters(tmp_path: Path) -> None:
    runtime, backend, spec, target = setup(tmp_path, GameLimits(max_actions=3, max_turns=3))
    solver = Solver(wrong=True)
    await evaluate(runtime, backend, spec, target, preset_harness, solver)
    counters = backend.last_counters
    assert counters is not None and counters.actions == 3 and counters.model_calls == solver.calls
    assert backend.last_run_id is not None and backend.last_final_state is not None
    await backend.close()


def test_frame_digest_is_canonical_json_sha256() -> None:
    import hashlib

    frame = ((0, 1), (2, 3))
    assert frame_digest(frame) == hashlib.sha256(b"[[0,1],[2,3]]").hexdigest()


async def test_host_produces_game_output_and_ignores_harness_return(tmp_path: Path) -> None:
    runtime, backend, spec, target = setup(tmp_path)

    async def returns_junk(context: RunContext, task: JsonValue) -> JsonValue:
        observation = await context.game_observe()
        while isinstance(observation, dict) and observation["actions_left"]:
            frame = observation["frame"]
            assert isinstance(frame, list)
            observation = await context.game_step({"name": choose(frame)})  # type: ignore[arg-type]
        return {"receipt": "forged"}

    assert await evaluate(runtime, backend, spec, target, _host_game_output(returns_junk)) == 1.0
    await backend.close()

    runtime, backend, spec, target = setup(tmp_path / "preset")
    assert (
        await evaluate(runtime, backend, spec, target, _host_game_output(preset_harness), Solver())
        == 1.0
    )
    await backend.close()


async def test_preset_play_output_is_the_host_value_not_the_harness_return(
    tmp_path: Path,
) -> None:
    # Regression: a harness that played through execute_preset could discard the
    # receipt and return a fabricated value, which then became the run output.
    forged: JsonValue = {"score": {"normalized": 1.0, "forged": True}}
    received: list[JsonValue] = []

    async def forges(context: RunContext, task: JsonValue) -> JsonValue:
        value = await preset_harness(context, task)
        received.append(deepcopy(value))
        if isinstance(value, dict):
            value.clear()
            value["tampered"] = True
        return forged

    runtime, backend, spec, target = setup(tmp_path, index=0)
    task = task_for(target)
    with scoped_evaluation(DIGEST):
        async with runtime.use_game_backend(backend):
            result = await runtime.evaluate(
                spec,
                _host_game_output(forges),
                task,
                Solver(),
                preset="explorer",
                candidate_digest=DIGEST,
            )
            assert result.output != forged
            assert result.output == received[0]
            assert backend.ledger.consume(task, result.output) == 1.0
    await backend.close()


async def test_a_swallowed_preset_failure_still_fails_the_run(tmp_path: Path) -> None:
    runtime, backend, spec, target = setup(tmp_path)

    async def swallows(context: RunContext, task: JsonValue) -> JsonValue:
        await preset_harness(context, task)
        try:
            await preset_harness(context, task)
        except GameProtocolError:
            pass
        return {"score": {"normalized": 1.0}}

    with pytest.raises(GameProtocolError, match="game_policy_mismatch"):
        await evaluate(runtime, backend, spec, target, _host_game_output(swallows), Solver())
    await backend.close()


async def test_harness_failure_closes_the_play_unscored(tmp_path: Path) -> None:
    runtime, backend, spec, target = setup(tmp_path)
    closed: list[bool] = []

    class Tracking(LocalGameProvider):
        async def open(self, ref: GameRef, *, limits: GameLimits) -> GameSession:
            session = await super().open(ref, limits=limits)
            original = session.close

            async def close() -> None:
                closed.append(True)
                await original()

            session.close = close  # type: ignore[method-assign]
            return session

    backend.provider = Tracking()

    async def fails(context: RunContext, task: JsonValue) -> JsonValue:
        await context.game_step({"name": "ACTION1"})
        raise RuntimeError("harness failed")

    with pytest.raises(RuntimeError):
        await evaluate(runtime, backend, spec, target, _host_game_output(fails))
    assert closed == [True] and not backend.completed and backend.actions_used == 1
    await backend.close()
