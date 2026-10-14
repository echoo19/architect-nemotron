from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from pydantic import JsonValue

from architect.floors import FloorError
from architect.games.evaluation import ReceiptLedger, task_for
from architect.games.execution import (
    GameBackend,
    GameExecutionPolicy,
    decision_repairs,
    decision_schema,
    execute_game,
    parse_decision,
)
from architect.games.grids import ClockBands
from architect.games.local import LocalGameProvider
from architect.games.types import (
    ActionName,
    GameAction,
    GameActionError,
    GameLimits,
    GameProtocolError,
    GameRef,
    GameScore,
    GameSession,
    Observation,
)
from architect.models import FakeAdapter, ModelRequest, ModelResponse
from architect.runtime import BudgetExceeded, RunContext, Runtime
from architect.runtime.evidence import scoped_evaluation
from architect.spec import Budget, FloorDefinition, FloorKind, Spec
from architect.telemetry import TelemetryStore

HarnessFn = Callable[[RunContext, JsonValue], Awaitable[JsonValue]]


@pytest.mark.parametrize(
    "text",
    [
        '{"action":{"name":"ACTION6","x":true,"y":1}}',
        '{"action":{"name":"ACTION1"},"score":1}',
        '{"action":{"name":"ACTION1"},"action":{"name":"RESET"}}',
        '{"action":{"name":"ACTION6","x":1}}',
        '{"action":{"name":"ACTION1"},"hypothesis":"x"}',
    ],
)
def test_invalid_hypothesis_decisions_rejected(text: str) -> None:
    with pytest.raises(GameProtocolError):
        parse_decision(text, strategy="hypothesis_tester", maximum_bytes=4096)


def test_hypothesis_prediction_required() -> None:
    decision = parse_decision(
        '{"action":{"name":"ACTION1"},"hypothesis":"switch", "prediction":"door opens"}',
        strategy="hypothesis_tester",
        maximum_bytes=4096,
    )
    assert decision.prediction == "door opens"


async def test_forged_evidence_never_scores() -> None:
    ledger = ReceiptLedger()
    with scoped_evaluation("a" * 64):
        with pytest.raises(FloorError):
            ledger.consume({}, {"score": 1})
        with pytest.raises(FloorError):
            ledger.consume({}, {"receipt": "a" * 32})


def setup(
    tmp_path: Path, limits: GameLimits | None = None
) -> tuple[Runtime, GameBackend, Spec, LocalGameProvider]:
    provider = LocalGameProvider()
    target = provider.manifest.refs("delivery")[0]
    policy = GameExecutionPolicy(
        small_model="fake",
        manifest_digest=provider.manifest.digest,
        support_dataset="support",
        limits=limits or GameLimits(max_actions=2, max_turns=2),
    )
    backend = GameBackend(
        provider,
        policy,
        ReceiptLedger(),
        evaluation_refs=(target,),
        target=target,
    )
    spec = Spec(
        task="Play",
        input_schema={},
        output_schema={},
        domain="game",
        model_target="fake",
        budget=Budget(cost_usd=1, compute_s=10),
        preset_hint="explorer",
        execution_config=policy.model_dump(mode="json"),
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_GAME_SCORE, limit=1, dataset="support"),
    )
    return Runtime(TelemetryStore(tmp_path / "runs")), backend, spec, provider


async def harness(context: RunContext, task: JsonValue) -> JsonValue:
    # Kept local rather than granting a game session to model data.
    return await execute_game(context, task, preset="explorer", params={}, components=())


def loser() -> FakeAdapter:
    return FakeAdapter(
        responses=[
            ModelResponse(
                text='{"action":{"name":"ACTION1"}}',
                model="fake",
                tokens_in=100_000,
                tokens_out=100_000,
                cost_usd=0,
            )
        ]
        * 2
    )


async def test_legal_losing_rollout_scores_zero_once(tmp_path: Path) -> None:
    runtime, backend, spec, provider = setup(tmp_path)
    task = task_for(provider.manifest.refs("delivery")[0])
    with scoped_evaluation("a" * 64):
        async with runtime.use_game_backend(backend):
            result = await runtime.evaluate(
                spec,
                harness,
                task,
                loser(),
                preset="explorer",
                candidate_digest="a" * 64,
            )
            assert backend.ledger.consume(task, result.output) == 0
            with pytest.raises(FloorError):
                backend.ledger.consume(task, result.output)
    await backend.close()


async def test_scope_expiry_and_task_rebinding_reject_receipts(tmp_path: Path) -> None:
    runtime, backend, spec, provider = setup(tmp_path)
    task = task_for(provider.manifest.refs("delivery")[0])
    with scoped_evaluation("a" * 64):
        async with runtime.use_game_backend(backend):
            result = await runtime.evaluate(
                spec,
                harness,
                task,
                loser(),
                preset="explorer",
                candidate_digest="a" * 64,
            )
    with scoped_evaluation("a" * 64):
        with pytest.raises(FloorError):
            backend.ledger.consume(task, result.output)
    await backend.close()


async def test_changed_frozen_policy_fails_before_open(tmp_path: Path) -> None:
    runtime, backend, spec, provider = setup(tmp_path)
    task = task_for(provider.manifest.refs("delivery")[0])
    changed = spec.model_copy(update={"execution_config": {}})
    with scoped_evaluation("a" * 64):
        async with runtime.use_game_backend(backend):
            with pytest.raises(GameProtocolError):
                await runtime.evaluate(
                    changed,
                    harness,
                    task,
                    loser(),
                    preset="explorer",
                    candidate_digest="a" * 64,
                )
    assert backend.actions_used == 0
    await backend.close()


async def test_substituted_task_cannot_select_easier_game(tmp_path: Path) -> None:
    runtime, backend, spec, provider = setup(tmp_path)
    task = task_for(provider.manifest.refs("delivery")[0])

    async def substitute(context: RunContext, original: JsonValue) -> JsonValue:
        return await execute_game(
            context,
            task_for(provider.manifest.refs("delivery")[1]),
            preset="explorer",
            params={},
            components=(),
        )

    with scoped_evaluation("a" * 64):
        async with runtime.use_game_backend(backend):
            with pytest.raises((ValueError, PermissionError)):
                await runtime.evaluate(
                    spec,
                    substitute,
                    task,
                    loser(),
                    preset="explorer",
                    candidate_digest="a" * 64,
                )
    assert backend.actions_used == 0
    await backend.close()


async def test_receipt_bound_to_task_and_candidate(tmp_path: Path) -> None:
    runtime, backend, spec, provider = setup(tmp_path)
    task = task_for(provider.manifest.refs("delivery")[0])
    with scoped_evaluation("a" * 64):
        async with runtime.use_game_backend(backend):
            result = await runtime.evaluate(
                spec,
                harness,
                task,
                loser(),
                preset="explorer",
                candidate_digest="a" * 64,
            )
            with scoped_evaluation("b" * 64):
                with pytest.raises(FloorError):
                    backend.ledger.consume(task, result.output)
    assert not backend.ledger._receipts
    await backend.close()


async def test_score_finalization_is_inside_deadline(tmp_path: Path) -> None:
    import asyncio

    runtime, backend, spec, provider = setup(tmp_path)
    task = task_for(provider.manifest.refs("delivery")[0])
    session = await provider.open(
        provider.manifest.refs("delivery")[0], limits=GameLimits(max_actions=2)
    )
    closed = False

    class DelayedFinalization:
        ref = session.ref

        @property
        def actions_used(self) -> int:
            return session.actions_used

        async def observe(self) -> Observation:
            return await session.observe()

        async def step(self, action: GameAction) -> Observation:
            return await session.step(action)

        async def finalize(self) -> GameScore:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def close(self) -> None:
            nonlocal closed
            closed = True
            await session.close()

    class FinalizingProvider(LocalGameProvider):
        async def open(self, ref: GameRef, *, limits: GameLimits) -> GameSession:
            return DelayedFinalization()

    backend.provider = FinalizingProvider()
    spec = spec.model_copy(update={"budget": Budget(cost_usd=1, compute_s=0.03)})
    with scoped_evaluation("a" * 64):
        async with runtime.use_game_backend(backend):
            with pytest.raises(BudgetExceeded):
                await runtime.evaluate(
                    spec,
                    harness,
                    task,
                    loser(),
                    preset="explorer",
                    candidate_digest="a" * 64,
                )
    assert not backend.completed and not backend.ledger._receipts
    assert closed
    await backend.close()


async def test_tuned_review_adds_real_budgeted_work(tmp_path: Path) -> None:
    runtime, backend, spec, provider = setup(tmp_path)
    task = task_for(provider.manifest.refs("delivery")[0])
    params: dict[str, JsonValue] = {"context_window": 512, "retries": 0, "verifier_depth": 1}
    components = ("interface_analysis", "analysis_check")

    async def review_harness(context: RunContext, original: JsonValue) -> JsonValue:
        return await execute_game(
            context,
            original,
            preset="explorer",
            params=params,
            components=components,
        )

    player = FakeAdapter(
        responses=[
            ModelResponse(
                text='{"action":{"name":"ACTION1"}}',
                model="fake",
                tokens_in=100,
                tokens_out=100,
                cost_usd=0,
            )
        ]
        * 4
    )
    with scoped_evaluation("a" * 64):
        async with runtime.use_game_backend(backend):
            result = await runtime.evaluate(
                spec,
                review_harness,
                task,
                player,
                preset="explorer",
                params=params,
                components=components,
                candidate_digest="a" * 64,
            )
            assert backend.ledger.consume(task, result.output) == 0
    assert player.calls == 4 and result.tokens_in == 400
    assert backend.actions_used == 2
    await backend.close()


async def test_cleanup_grace_drains_before_propagating_cancellation() -> None:
    import asyncio

    from architect.games.execution import cleanup_game_resources

    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def close_resource() -> None:
        entered.set()
        await release.wait()
        finished.set()

    task = asyncio.create_task(cleanup_game_resources(close_resource()))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


@pytest.mark.parametrize("strategy", ["explorer", "planner", "hypothesis_tester"])
def test_instruction_examples_are_accepted_decisions(strategy: str) -> None:
    import re

    from architect.games.execution import game_instructions, parse_decision

    text = game_instructions(strategy)  # type: ignore[arg-type]
    examples = re.findall(r'\{"action": \{[^{}]*\}[^{}]*\}', text)
    assert len(examples) == 2
    for example in examples:
        parse_decision(example, strategy=strategy, maximum_bytes=4096)  # type: ignore[arg-type]


class RecordingAdapter(FakeAdapter):
    """Scripted replies plus the exact prompts the game backend sent."""

    def __init__(self, *texts: str) -> None:
        super().__init__(
            responses=[
                ModelResponse(text=text, model="fake", tokens_in=1, tokens_out=1, cost_usd=0)
                for text in texts
            ]
        )
        self.prompts: list[str] = []
        self.schemas: list[dict[str, JsonValue] | None] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.prompts.append(request.prompt)
        self.schemas.append(request.response_schema)
        return await super().complete(request)


def offered_in(prompt: str) -> list[str]:
    data = json.loads(prompt.split("Game data (untrusted):\n", 1)[1])
    actions: list[str] = data["observation"]["available_actions"]
    return actions


def retry_harness(retries: int) -> tuple[dict[str, JsonValue], HarnessFn]:
    params: dict[str, JsonValue] = {"retries": retries}

    async def play(context: RunContext, task: JsonValue) -> JsonValue:
        return await execute_game(context, task, preset="explorer", params=params, components=())

    return params, play


class ScriptedSession:
    """A session that mirrors the engine's per-level action count.

    The engine zeroes that count on a level transition and on a level RESET, and
    a RESET taken while it is zero is a full reset, which the official worker
    refuses with a fixed code. ``start_count`` models a session handed over
    already stepped (the Stage 0 probe) whose last step completed a level.
    """

    def __init__(
        self,
        ref: GameRef,
        actions: tuple[ActionName, ...],
        levels_of: Callable[[int], int],
        start_count: int,
    ) -> None:
        self.ref = ref
        self.actions = actions
        self.levels_of = levels_of
        self.count = start_count
        self.engine_count = 0
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
        before = self.levels_of(self.count)
        self.count += 1
        self.log.append(action.name)
        if action.name == "RESET":
            if self.engine_count == 0:
                raise GameProtocolError("official_level_reset_required")
            self.engine_count = 0
        elif self.levels_of(self.count) > before:
            self.engine_count = 0
        else:
            self.engine_count += 1
        return self._observation()

    async def finalize(self) -> GameScore:
        return GameScore(normalized=0, raw=0, metric="local_win_v1", source="local")

    async def close(self) -> None:
        return None


class ScriptedProvider(LocalGameProvider):
    def __init__(
        self,
        target: GameRef,
        actions: tuple[ActionName, ...],
        levels_of: Callable[[int], int],
        start_count: int,
    ) -> None:
        super().__init__()
        self.target = target
        self.actions = actions
        self.levels_of = levels_of
        self.start_count = start_count
        self.last: ScriptedSession | None = None

    async def open(self, ref: GameRef, *, limits: GameLimits) -> GameSession:
        self.last = ScriptedSession(self.target, self.actions, self.levels_of, self.start_count)
        return self.last


def scripted_provider(
    target: GameRef,
    actions: tuple[ActionName, ...],
    levels_of: Callable[[int], int],
    *,
    start_count: int = 0,
) -> ScriptedProvider:
    """A provider whose session always lists ``actions``; levels follow the action count."""
    return ScriptedProvider(target, actions, levels_of, start_count)


async def test_reset_is_withheld_on_first_prompt_and_repaired(tmp_path: Path) -> None:
    runtime, backend, spec, provider = setup(tmp_path, GameLimits(max_actions=1, max_turns=1))
    task = task_for(provider.manifest.refs("delivery")[0])
    params, play = retry_harness(1)
    adapter = RecordingAdapter('{"action":{"name":"RESET"}}', '{"action":{"name":"ACTION1"}}')
    with scoped_evaluation("a" * 64):
        async with runtime.use_game_backend(backend):
            result = await runtime.evaluate(
                spec,
                play,
                task,
                adapter,
                preset="explorer",
                params=params,
                candidate_digest="a" * 64,
            )
            assert backend.ledger.consume(task, result.output) == 0
    assert adapter.calls == 2 and backend.actions_used == 1
    assert "RESET" not in offered_in(adapter.prompts[0])
    assert "ACTION1" in offered_in(adapter.prompts[0])
    assert adapter.prompts[1].startswith("Repair the invalid decision.")
    assert "RESET" not in offered_in(adapter.prompts[1])
    assert "RESET must never be the first action" in adapter.prompts[0]
    await backend.close()


async def test_reset_is_withheld_for_one_prompt_after_a_level_transition(tmp_path: Path) -> None:
    runtime, backend, spec, provider = setup(tmp_path, GameLimits(max_actions=4, max_turns=4))
    target = provider.manifest.refs("delivery")[0]
    task = task_for(target)
    # Two levels; the engine always lists RESET and ACTION1. Level 1 completes at action 2.
    backend.provider = scripted_provider(
        target, ("RESET", "ACTION1"), lambda count: min(1, count // 2)
    )
    adapter = RecordingAdapter(*['{"action":{"name":"ACTION1"}}'] * 4)
    with scoped_evaluation("a" * 64):
        async with runtime.use_game_backend(backend):
            await runtime.evaluate(
                spec, harness, task, adapter, preset="explorer", candidate_digest="a" * 64
            )
    offered = [offered_in(prompt) for prompt in adapter.prompts]
    # Prompt 1: start of the game. Prompt 3: first prompt after level 1 completed.
    assert offered == [["ACTION1"], ["RESET", "ACTION1"], ["ACTION1"], ["RESET", "ACTION1"]]
    await backend.close()


async def test_reset_is_withheld_for_one_prompt_after_a_reset(tmp_path: Path) -> None:
    # A level RESET re-enters the level with a zero engine action count, so a
    # RESET directly after it would be a full reset; it is withheld and repaired.
    runtime, backend, spec, provider = setup(tmp_path, GameLimits(max_actions=4, max_turns=4))
    target = provider.manifest.refs("delivery")[0]
    task = task_for(target)
    backend.provider = scripted_provider(target, ("RESET", "ACTION1", "ACTION2"), lambda _: 0)
    params, play = retry_harness(1)
    adapter = RecordingAdapter(
        '{"action":{"name":"ACTION2"}}',
        '{"action":{"name":"RESET"}}',
        '{"action":{"name":"RESET"}}',
        '{"action":{"name":"ACTION1"}}',
        '{"action":{"name":"ACTION1"}}',
    )
    with scoped_evaluation("a" * 64):
        async with runtime.use_game_backend(backend):
            await runtime.evaluate(
                spec,
                play,
                task,
                adapter,
                preset="explorer",
                params=params,
                candidate_digest="a" * 64,
            )
    offered = [offered_in(prompt) for prompt in adapter.prompts]
    # Prompt 3 directly follows the RESET; the second RESET is rejected and prompt 4
    # is its repair. Prompt 5 follows a real action, so RESET is legal again.
    assert offered == [
        ["ACTION1", "ACTION2"],
        ["RESET", "ACTION1", "ACTION2"],
        ["ACTION1", "ACTION2"],
        ["ACTION1", "ACTION2"],
        ["RESET", "ACTION1", "ACTION2"],
    ]
    assert adapter.prompts[3].startswith(
        "Repair the invalid decision. The action name must be one of"
    )
    assert "must never directly follow another RESET" in adapter.prompts[0]
    assert adapter.calls == 5 and backend.actions_used == 4
    await backend.close()


async def test_first_prompt_never_offers_reset_on_a_handed_over_session(tmp_path: Path) -> None:
    # The factory hands the backend the Stage 0 probe session, already stepped.
    # If the probe's last step completed a level the engine's per-level count is
    # zero, which the backend cannot observe, so a model RESET would be a refused
    # full reset. The first prompt is therefore always fresh, whatever
    # actions_used says, and a RESET reply is repaired.
    runtime, backend, spec, provider = setup(tmp_path, GameLimits(max_actions=4, max_turns=3))
    target = provider.manifest.refs("delivery")[0]
    task = task_for(target)
    scripted = scripted_provider(
        target, ("RESET", "ACTION1", "ACTION2"), lambda _: 1, start_count=1
    )
    backend.provider = scripted
    params, play = retry_harness(1)
    adapter = RecordingAdapter(
        '{"action":{"name":"RESET"}}',
        '{"action":{"name":"ACTION1"}}',
        '{"action":{"name":"ACTION1"}}',
        '{"action":{"name":"RESET"}}',
    )
    with scoped_evaluation("a" * 64):
        async with runtime.use_game_backend(backend):
            result = await runtime.evaluate(
                spec,
                play,
                task,
                adapter,
                preset="explorer",
                params=params,
                candidate_digest="a" * 64,
            )
            assert backend.ledger.consume(task, result.output) == 0
    assert scripted.last is not None
    assert scripted.last.actions_used == 1 + 3
    assert scripted.last.log == ["ACTION1", "ACTION1", "RESET"]
    offered = [offered_in(prompt) for prompt in adapter.prompts]
    # Prompt 1 is fresh despite actions_used == 1; prompt 2 is its repair. From
    # prompt 3 the backend has stepped the level itself, so RESET is legal again.
    assert offered == [
        ["ACTION1", "ACTION2"],
        ["ACTION1", "ACTION2"],
        ["RESET", "ACTION1", "ACTION2"],
        ["RESET", "ACTION1", "ACTION2"],
    ]
    assert adapter.prompts[1].startswith(
        "Repair the invalid decision. The action name must be one of"
    )
    assert adapter.calls == 4 and backend.actions_used == 4
    await backend.close()


def test_reset_stays_offered_when_it_is_the_only_action() -> None:
    from architect.games.execution import offered_actions

    observation = Observation(
        frames=(((0,),),), state="NOT_FINISHED", available_actions=("RESET",), levels_completed=0
    )
    assert offered_actions(observation, fresh=True) == ("RESET",)
    observation = Observation(
        frames=(((0,),),),
        state="NOT_FINISHED",
        available_actions=("RESET", "ACTION2"),
        levels_completed=0,
    )
    assert offered_actions(observation, fresh=True) == ("ACTION2",)
    assert offered_actions(observation, fresh=False) == ("RESET", "ACTION2")


async def test_unavailable_action_is_repaired_without_failing_the_run(tmp_path: Path) -> None:
    runtime, backend, spec, provider = setup(tmp_path)
    task = task_for(provider.manifest.refs("delivery")[0])
    params, play = retry_harness(1)
    adapter = RecordingAdapter(
        '{"action":{"name":"ACTION7"}}',
        '{"action":{"name":"ACTION1"}}',
        '{"action":{"name":"ACTION1"}}',
    )
    with scoped_evaluation("a" * 64):
        async with runtime.use_game_backend(backend):
            result = await runtime.evaluate(
                spec,
                play,
                task,
                adapter,
                preset="explorer",
                params=params,
                candidate_digest="a" * 64,
            )
            assert backend.ledger.consume(task, result.output) == 0
    assert adapter.calls == 3 and backend.actions_used == 2
    assert adapter.prompts[1].startswith("Repair the invalid decision.")
    # The repair prefix is fixed text; the rejected reply is never echoed.
    assert "ACTION7" not in adapter.prompts[1].split("Game data (untrusted):", 1)[0]
    assert not adapter.prompts[2].startswith("Repair")
    await backend.close()


async def test_one_malformed_reply_is_repaired_even_without_tuned_retries(tmp_path: Path) -> None:
    runtime, backend, spec, provider = setup(tmp_path, GameLimits(max_actions=1, max_turns=1))
    task = task_for(provider.manifest.refs("delivery")[0])
    params, play = retry_harness(0)
    adapter = RecordingAdapter("not-json", '{"action":{"name":"ACTION1"}}')
    with scoped_evaluation("a" * 64):
        async with runtime.use_game_backend(backend):
            result = await runtime.evaluate(
                spec,
                play,
                task,
                adapter,
                preset="explorer",
                params=params,
                candidate_digest="a" * 64,
            )
            assert backend.ledger.consume(task, result.output) == 0
    assert adapter.calls == 2 and backend.actions_used == 1
    assert adapter.prompts[1].startswith("Repair the invalid decision.")
    await backend.close()


@pytest.mark.parametrize(
    "reply,calls,error,code",
    [
        ("not-json", 2, GameProtocolError, "game_decision_invalid"),
        ('{"action":{"name":"ACTION7"}}', 2, GameActionError, "game_action_unavailable"),
    ],
)
async def test_zero_tuned_retries_bounds_repairs(
    tmp_path: Path, reply: str, calls: int, error: type[Exception], code: str
) -> None:
    # At retries=0 a malformed reply and an unavailable action each get exactly
    # one repair (decision_repairs(0) == 1), then fail with the fixed code on the
    # second reply and no action is spent. The presets' grid is left untouched:
    # a compile's only candidate is tuned at retries=0 and still gets this repair.
    runtime, backend, spec, provider = setup(tmp_path)
    task = task_for(provider.manifest.refs("delivery")[0])
    params, play = retry_harness(0)
    adapter = RecordingAdapter(*[reply] * 3)
    with scoped_evaluation("a" * 64):
        async with runtime.use_game_backend(backend):
            with pytest.raises(error, match=code):
                await runtime.evaluate(
                    spec,
                    play,
                    task,
                    adapter,
                    preset="explorer",
                    params=params,
                    candidate_digest="a" * 64,
                )
    assert adapter.calls == calls and backend.actions_used == 0 and not backend.completed
    assert adapter.prompts[1].startswith("Repair the invalid decision.")
    assert adapter.calls <= backend.policy.limits.max_turns * (decision_repairs(0) + 1)
    await backend.close()


async def test_prompt_size_reserves_room_for_one_repair(tmp_path: Path) -> None:
    from architect.games.execution import _REPAIR_HEADROOM_BYTES

    async def play_with(limit: int | None, *replies: str) -> tuple[RecordingAdapter, GameBackend]:
        limits = GameLimits(max_actions=1, max_turns=1)
        if limit is not None:
            limits = limits.model_copy(update={"max_payload_bytes": limit})
        runtime, backend, spec, provider = setup(tmp_path / str(limit), limits)
        task = task_for(provider.manifest.refs("delivery")[0])
        params, play = retry_harness(0)
        adapter = RecordingAdapter(*replies)
        try:
            with scoped_evaluation("a" * 64):
                async with runtime.use_game_backend(backend):
                    await runtime.evaluate(
                        spec,
                        play,
                        task,
                        adapter,
                        preset="explorer",
                        params=params,
                        candidate_digest="a" * 64,
                    )
        finally:
            await backend.close()
        return adapter, backend

    # Measure the exact first prompt, then run one byte under and at the margin.
    probe, _ = await play_with(None, '{"action":{"name":"ACTION1"}}')
    fitted = len(probe.prompts[0].encode("utf-8"))
    with pytest.raises(GameProtocolError, match="game_prompt_size"):
        await play_with(fitted + _REPAIR_HEADROOM_BYTES - 1, "not-json")
    adapter, backend = await play_with(
        fitted + _REPAIR_HEADROOM_BYTES, "not-json", '{"action":{"name":"ACTION1"}}'
    )
    # The prompt that fits keeps its repair, and no sent prompt exceeds the limit.
    assert adapter.calls == 2 and backend.actions_used == 1
    assert adapter.prompts[1].startswith("Repair the invalid decision.")
    limit = backend.policy.limits.max_payload_bytes
    assert all(len(prompt.encode("utf-8")) <= limit for prompt in adapter.prompts)


async def test_prompt_size_failure_spends_no_model_call(tmp_path: Path) -> None:
    runtime, backend, spec, provider = setup(
        tmp_path, GameLimits(max_actions=1, max_turns=1, max_payload_bytes=512)
    )
    task = task_for(provider.manifest.refs("delivery")[0])
    adapter = RecordingAdapter('{"action":{"name":"ACTION1"}}')
    with scoped_evaluation("a" * 64):
        async with runtime.use_game_backend(backend):
            with pytest.raises(GameProtocolError, match="game_prompt_size"):
                await runtime.evaluate(
                    spec, harness, task, adapter, preset="explorer", candidate_digest="a" * 64
                )
    assert adapter.calls == 0 and backend.actions_used == 0 and not backend.completed
    await backend.close()


async def test_repair_count_is_bounded_by_tuned_retries(tmp_path: Path) -> None:
    runtime, backend, spec, provider = setup(tmp_path)
    task = task_for(provider.manifest.refs("delivery")[0])
    params, play = retry_harness(1)
    adapter = RecordingAdapter(*['{"action":{"name":"ACTION7"}}'] * 4)
    with scoped_evaluation("a" * 64):
        async with runtime.use_game_backend(backend):
            with pytest.raises(GameActionError, match="game_action_unavailable"):
                await runtime.evaluate(
                    spec,
                    play,
                    task,
                    adapter,
                    preset="explorer",
                    params=params,
                    candidate_digest="a" * 64,
                )
    assert adapter.calls == 2 and backend.actions_used == 0 and not backend.completed
    await backend.close()


async def test_history_records_whether_each_action_changed_the_grid() -> None:
    """A model that repeats a dead click must be told, in public host facts, it did nothing."""
    from architect.games.execution import game_instructions

    text = game_instructions("explorer")
    assert "history[].changed is false" in text and "placeholders, never a default" in text


def test_grid_diff_and_action_keys_are_public_host_facts() -> None:
    from architect.games.execution import _action_key, _grid_diff

    before = ((0, 0, 0), (0, 0, 0))
    after = ((0, 5, 0), (0, 0, 7))
    assert _grid_diff(before, after) == {"cells": 2, "box": [1, 0, 2, 1]}
    assert _grid_diff(before, before) == {"cells": 0, "box": None}
    assert _grid_diff(before, ((0, 0),)) == {"cells": None, "box": None, "resized": True}
    assert _action_key({"name": "ACTION6", "x": 10, "y": 18}) == "ACTION6@10,18"
    assert _action_key({"name": "ACTION1"}) == "ACTION1"
    assert _action_key(None) == "?"


async def test_a_loop_is_reported_as_revisited_with_tried_counts(tmp_path: Path) -> None:
    """The baseline local model oscillated between two clicks; the host now says so."""
    runtime, backend, spec, provider = setup(tmp_path, GameLimits(max_actions=3, max_turns=3))
    target = provider.manifest.refs("delivery")[0]
    task = task_for(target)
    scripted = scripted_provider(target, ("ACTION1", "ACTION2"), lambda _: 0, start_count=0)
    backend.provider = scripted
    params, play = retry_harness(0)
    adapter = RecordingAdapter(
        '{"action":{"name":"ACTION1"}}',
        '{"action":{"name":"ACTION1"}}',
        '{"action":{"name":"ACTION2"}}',
    )
    with scoped_evaluation("a" * 64):
        async with runtime.use_game_backend(backend):
            await runtime.evaluate(
                spec, play, task, adapter, preset="explorer", params=params,
                candidate_digest="a" * 64,
            )
    data = [json.loads(p.split("Game data (untrusted):\n", 1)[1]) for p in adapter.prompts]
    assert [d["tried"] for d in data] == [{}, {"ACTION1": 1}, {"ACTION1": 2}]
    last = data[2]["history"][-1]
    # The scripted engine never changes its grid: every step revisits the start.
    assert last["changed"] is False and last["revisited"] is True
    assert last["diff"] == {"cells": 0, "box": None}
    assert "history[].revisited is true" in adapter.prompts[0] or "revisited" in (
        adapter.requests[0].cache_prefix or ""
    )
    await backend.close()


def schema_actions(schema: dict[str, JsonValue] | None) -> list[str]:
    """The action names a decision schema admits, in schema order."""
    assert schema is not None
    properties = schema["properties"]
    assert isinstance(properties, dict)
    action = properties["action"]
    assert isinstance(action, dict)
    variants = action.get("anyOf", [action])
    assert isinstance(variants, list)
    names: list[str] = []
    for variant in variants:
        assert isinstance(variant, dict)
        fields = variant["properties"]
        assert isinstance(fields, dict)
        name = fields["name"]
        assert isinstance(name, dict)
        enum = name["enum"]
        assert isinstance(enum, list)
        names.extend(str(item) for item in enum)
    return names


@pytest.mark.parametrize("strategy", ["explorer", "planner", "hypothesis_tester"])
def test_decision_schema_is_exact_for_strategy_offered_actions_and_grid(strategy: str) -> None:
    import jsonschema

    grid = tuple(tuple(0 for _ in range(5)) for _ in range(3))  # 3 rows, 5 columns
    offered: tuple[ActionName, ...] = ("ACTION1", "ACTION6", "ACTION3")
    schema = decision_schema(strategy, offered, grid)  # type: ignore[arg-type]
    assert schema is not None
    fields: dict[str, list[str]] = {
        "explorer": [],
        "planner": ["plan"],
        "hypothesis_tester": ["hypothesis", "prediction"],
    }
    assert schema["required"] == ["action", *fields[strategy]]
    assert schema["additionalProperties"] is False
    assert sorted(schema_actions(schema)) == sorted(offered)
    jsonschema.Draft202012Validator.check_schema(schema)
    extra = {field: "text" for field in fields[strategy]}
    accepted = [
        {"action": {"name": "ACTION1"}, **extra},
        {"action": {"name": "ACTION6", "x": 4, "y": 2}, **extra},
    ]
    rejected = [
        {"action": {"name": "ACTION2"}, **extra},  # not offered
        {"action": {"name": "RESET"}, **extra},  # not offered
        {"action": {"name": "ACTION6", "x": 5, "y": 0}, **extra},  # column out of grid
        {"action": {"name": "ACTION6", "x": 0, "y": 3}, **extra},  # row out of grid
        {"action": {"name": "ACTION6", "x": 1}, **extra},  # coordinates required
        {"action": {"name": "ACTION1", "x": 1, "y": 1}, **extra},  # coordinates forbidden
        {"action": {"name": "ACTION1"}, "score": 1, **extra},  # no model-authored score
    ]
    if fields[strategy]:
        rejected.append({"action": {"name": "ACTION1"}})  # strategy fields required
    validator = jsonschema.Draft202012Validator(schema)
    for reply in accepted:
        assert validator.is_valid(reply)
        # The schema never admits what the authoritative parser refuses.
        parse_decision(json.dumps(reply), strategy=strategy, maximum_bytes=4096)  # type: ignore[arg-type]
    for reply in rejected:
        assert not validator.is_valid(reply)
    # Without ACTION6 there is a single plain variant; nothing offered, no schema.
    single = decision_schema(strategy, ("ACTION2",), grid)  # type: ignore[arg-type]
    assert single is not None and schema_actions(single) == ["ACTION2"]
    assert decision_schema(strategy, (), grid) is None  # type: ignore[arg-type]
    # Unknown grid: the public coordinate range bounds ACTION6.
    unbounded = decision_schema(strategy, ("ACTION6",))  # type: ignore[arg-type]
    assert jsonschema.Draft202012Validator(unbounded).is_valid(
        {"action": {"name": "ACTION6", "x": 63, "y": 63}, **extra}
    )


async def test_every_decision_call_sends_the_offered_action_schema(tmp_path: Path) -> None:
    # RESET is withheld on the first prompt and directly after a RESET; each call,
    # repairs included, is constrained to exactly the actions that prompt offers.
    runtime, backend, spec, provider = setup(tmp_path, GameLimits(max_actions=3, max_turns=3))
    target = provider.manifest.refs("delivery")[0]
    task = task_for(target)
    backend.provider = scripted_provider(target, ("RESET", "ACTION1", "ACTION6"), lambda _: 0)
    params, play = retry_harness(1)
    adapter = RecordingAdapter(
        '{"action":{"name":"ACTION6","x":0,"y":0}}',
        '{"action":{"name":"RESET"}}',
        '{"action":{"name":"RESET"}}',  # withheld after a RESET: repaired
        '{"action":{"name":"ACTION1"},"score":1}',  # schema-invalid: parse still refuses
    )
    with scoped_evaluation("a" * 64):
        async with runtime.use_game_backend(backend):
            with pytest.raises(GameProtocolError, match="^game_decision_invalid$"):
                await runtime.evaluate(
                    spec,
                    play,
                    task,
                    adapter,
                    preset="explorer",
                    params=params,
                    candidate_digest="a" * 64,
                )
    assert adapter.calls == 4 and backend.actions_used == 2
    assert adapter.prompts[3].startswith("Repair the invalid decision. The action name")
    for prompt, schema in zip(adapter.prompts, adapter.schemas, strict=True):
        assert schema_actions(schema) == offered_in(prompt)
    assert [schema_actions(schema) for schema in adapter.schemas] == [
        ["ACTION1", "ACTION6"],
        ["RESET", "ACTION1", "ACTION6"],
        ["ACTION1", "ACTION6"],
        ["ACTION1", "ACTION6"],
    ]
    # The repair is sent the same exact schema as the call it repairs.
    assert adapter.schemas[3] == adapter.schemas[2]
    await backend.close()


async def test_review_calls_send_the_same_decision_schema(tmp_path: Path) -> None:
    runtime, backend, spec, provider = setup(tmp_path)
    task = task_for(provider.manifest.refs("delivery")[0])
    params: dict[str, JsonValue] = {"context_window": 512, "retries": 0, "verifier_depth": 1}
    components = ("interface_analysis", "analysis_check")

    async def review_harness(context: RunContext, original: JsonValue) -> JsonValue:
        return await execute_game(
            context, original, preset="explorer", params=params, components=components
        )

    adapter = RecordingAdapter(*['{"action":{"name":"ACTION1"}}'] * 4)
    with scoped_evaluation("a" * 64):
        async with runtime.use_game_backend(backend):
            result = await runtime.evaluate(
                spec,
                review_harness,
                task,
                adapter,
                preset="explorer",
                params=params,
                components=components,
                candidate_digest="a" * 64,
            )
            assert backend.ledger.consume(task, result.output) == 0
    # Accounting is unchanged by the schema: exactly the scripted usage.
    assert adapter.calls == 4 and result.tokens_in == 4 and result.tokens_out == 4
    assert adapter.schemas[0] == adapter.schemas[1] and adapter.schemas[2] == adapter.schemas[3]
    for prompt, schema in zip(adapter.prompts, adapter.schemas, strict=True):
        assert schema_actions(schema) == offered_in(prompt)
    assert all(shape.response_schema_sha256 for shape in adapter.seen)
    await backend.close()


class BarSession(ScriptedSession):
    """A game whose only change is a step bar along the bottom row (like ls20)."""

    def _observation(self) -> Observation:
        rows = [[0] * 8 for _ in range(8)]
        rows[3][3] = 5  # the (static) game content
        for x in range(min(self.count, 8)):
            rows[7][x] = 2  # the bar advances one cell per action
        return Observation(
            frames=(tuple(tuple(row) for row in rows),),
            state="NOT_FINISHED",
            available_actions=self.actions,
            levels_completed=0,
            win_levels=2,
        )


async def test_a_step_bar_is_masked_from_changed_diff_and_revisited(tmp_path: Path) -> None:
    """Public games draw step bars on an edge; without masking every no-op 'changes'."""
    runtime, backend, spec, provider = setup(tmp_path, GameLimits(max_actions=7, max_turns=7))
    target = provider.manifest.refs("delivery")[0]
    task = task_for(target)
    backend.provider = BarProvider(target, ("ACTION1", "ACTION2"), lambda _: 0, 0)
    params, play = retry_harness(0)
    adapter = RecordingAdapter(*(['{"action":{"name":"ACTION1"}}'] * 7))
    with scoped_evaluation("a" * 64):
        async with runtime.use_game_backend(backend):
            await runtime.evaluate(
                spec, play, task, adapter, preset="explorer", params=params,
                candidate_digest="a" * 64,
            )
    data = [json.loads(p.split("Game data (untrusted):\n", 1)[1]) for p in adapter.prompts]
    # Before five same-size steps there is no clock: the bar counts as a change.
    assert "clock" not in data[4]
    assert data[4]["history"][-1]["changed"] is True
    # From then on the bottom row is the clock: no-ops are no-ops and revisit the start.
    assert data[6]["clock"]["rows"] == [7] and data[6]["clock"]["columns"] == []
    last = data[6]["history"][-1]
    assert last["changed"] is False and last["revisited"] is True
    assert last["diff"] == {"cells": 0, "box": None}
    await backend.close()


class BarProvider(ScriptedProvider):
    async def open(self, ref: GameRef, *, limits: GameLimits) -> GameSession:
        self.last = BarSession(self.target, self.actions, self.levels_of, self.start_count)
        return self.last


def test_clock_bands_find_edge_bars_only() -> None:
    def frame(bar: int, blob: int = 0) -> tuple[tuple[int, ...], ...]:
        rows = [[0] * 10 for _ in range(10)]
        for x in range(bar):
            rows[0][x] = 3  # a bar on the top edge
        rows[5][blob % 10] = 4  # interior motion, never a clock
        return tuple(tuple(row) for row in rows)

    clock = ClockBands()
    changes = [clock.update(frame(i, i), frame(i + 1, i + 1)) for i in range(6)]
    assert changes == [False, False, False, False, True, False]
    assert clock.rows == {0} and clock.cols == frozenset()
    masked = clock.mask(frame(4, 2))
    assert masked[0] == (0,) * 10 and masked[5][2] == 4
    assert clock.mask(frame(4, 2)) == clock.mask(frame(7, 2))  # the bar no longer matters
    # A resized frame is not a step; a fresh detector masks nothing.
    assert clock.update(frame(1), ((0,),)) is False
    assert ClockBands().mask(frame(3)) == frame(3)
