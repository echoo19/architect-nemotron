"""The conductor's native loop (Phase 13, Section 4) with a fake streaming adapter (offline)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest
from session_support import fake_profile
from test_agent_conductor import (
    BUILD_ERROR,
    CANCEL_A_QUESTION,
    DECLINE_PLAN,
    DECLINED,
    DESCRIPTION,
    EMAILS,
    STOPPED,
    build_mode,
    failing_make,
    recording_make,
)

from architect.agent import Conductor, ConductorSettings, RecordingUI, ToolError, Transcript
from architect.agent.conductor import BUILD_ENDED, BUILD_MODE, SET_ASIDE, build_ended_text
from architect.agent.events import (
    AssistantText,
    Notice,
    TextDelta,
    Thinking,
    ToolFinished,
    ToolStarted,
)
from architect.agent.native import ObservedChat, chat_messages, streaming_adapter
from architect.models import ModelRequest, ModelResponse, UsageQuote
from architect.models.chat import (
    ChatEvent,
    ChatMessage,
    DoneEvent,
    TextDeltaEvent,
    ToolCallEvent,
    ToolSpec,
    UsageEvent,
)
from architect.profiles import build_services, load_profile
from architect.session.observe import Meter, ObservedAdapter

COST = 0.001


def answer(*pieces: str) -> list[ChatEvent]:
    return [*(TextDeltaEvent(p) for p in pieces), UsageEvent(10, 5, COST), DoneEvent("end_turn")]


def tools(*calls: ToolCallEvent, text: str = "") -> list[ChatEvent]:
    head: list[ChatEvent] = [TextDeltaEvent(text)] if text else []
    return [*head, *calls, UsageEvent(10, 5, COST), DoneEvent("tool_use")]


def valid_conversation(messages: Sequence[ChatMessage]) -> bool:
    """Every tool call is answered right after its message, ids are unique."""
    ids: set[str] = set()
    index = 0
    while index < len(messages):
        message = messages[index]
        index += 1
        if message.role == "tool":
            return False  # a result that does not follow its call
        for call in message.tool_calls:
            if call.id in ids:
                return False
            ids.add(call.id)
        expected = [call.id for call in message.tool_calls]
        answered = [m.tool_call_id for m in messages[index : index + len(expected)]]
        if answered != expected:
            return False
        index += len(expected)
    return True


class Streaming:
    """A ChatStreaming fake: one scripted reply per call; records what it was sent."""

    def __init__(self, *replies: list[ChatEvent], model: str = "fake") -> None:
        self.model = model
        self.replies = list(replies)
        self.sent: list[tuple[str, tuple[ChatMessage, ...], tuple[ToolSpec, ...], int | None]] = []
        self.pause_on: int | None = None
        self.paused = asyncio.Event()

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=1, tokens_out=1, cost_usd=COST)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise AssertionError("the native loop never uses complete")

    def quote_chat(
        self, system: str, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec],
        max_output_tokens: int | None,
    ) -> UsageQuote:
        return UsageQuote(tokens_in=10, tokens_out=5, cost_usd=COST)

    async def stream_chat(
        self, system: str, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec],
        max_output_tokens: int | None,
    ) -> AsyncIterator[ChatEvent]:
        self.sent.append((system, tuple(messages), tuple(tools), max_output_tokens))
        assert valid_conversation(messages)
        if self.pause_on == len(self.sent):
            yield TextDeltaEvent("partial")
            self.paused.set()
            await asyncio.sleep(3600)
        for event in self.replies.pop(0) if self.replies else answer("done"):
            yield event


@pytest.fixture
def services(tmp_path: Path):  # type: ignore[no-untyped-def]
    path = fake_profile(tmp_path)
    profile = load_profile(path)
    return build_services(profile, profile.workspace)


def conductor(  # type: ignore[no-untyped-def]
    services, cwd: Path, *replies: list[ChatEvent], **settings: object
) -> tuple[Conductor, Streaming, RecordingUI]:
    adapter = Streaming(*replies)
    ui = RecordingUI()
    agent = Conductor(services, adapter, cwd, ui, ConductorSettings(**settings))  # type: ignore[arg-type]
    return agent, adapter, ui


async def test_an_answer_streams_in_order_and_ends_with_the_whole_text(  # type: ignore[no-untyped-def]
    services, tmp_path: Path
) -> None:
    agent, adapter, ui = conductor(services, tmp_path, answer("Hel", "lo", "."))
    result = await agent.turn("hi")
    assert result.text == "Hello." and result.tool_calls == 0
    assert result.cost_usd == pytest.approx(COST)
    assert ui.events == [
        Thinking(True), TextDelta("Hel"), TextDelta("lo"), TextDelta("."), Thinking(False),
        AssistantText("Hello."),
    ]
    assert [m.role for m in agent.transcript.messages] == ["user", "assistant"]
    system, messages, specs, cap = adapter.sent[0]
    assert "Reply protocol" not in system and "call `code`" in system
    assert str(tmp_path) in system
    assert messages == (ChatMessage("user", "hi"),)
    assert {spec.name for spec in specs} >= {"read_file", "grep", "code", "spawn", "make"}
    assert cap == agent.settings.max_output_tokens


async def test_tool_calls_run_and_their_results_reach_the_next_call(  # type: ignore[no-untyped-def]
    services, tmp_path: Path
) -> None:
    (tmp_path / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    agent, adapter, ui = conductor(
        services, tmp_path,
        tools(
            ToolCallEvent("toolu_1", "read_file", {"path": "notes.txt"}),
            ToolCallEvent("toolu_2", "grep", {"pattern": "bet"}),
            text="Let me look.",
        ),
        answer("It says alpha and beta."),
    )
    result = await agent.turn("what is in notes.txt?")
    assert result.tool_calls == 2 and result.text == "It says alpha and beta."
    assert result.cost_usd == pytest.approx(2 * COST)
    started = [e for e in ui.events if isinstance(e, ToolStarted)]
    finished = [e for e in ui.events if isinstance(e, ToolFinished)]
    assert [e.summary for e in started] == ["read_file(notes.txt)", "grep(bet)"]
    assert [e.call_id for e in finished] == ["toolu_1", "toolu_2"] and all(e.ok for e in finished)
    # The preamble streamed, then the tools ran; only the final answer is AssistantText.
    kinds = [type(e).__name__ for e in ui.events]
    assert kinds.index("TextDelta") < kinds.index("ToolStarted")
    assert ui.texts() == ["It says alpha and beta."]
    roles = [m.role for m in agent.transcript.messages]
    assert roles == ["user", "assistant", "tool", "tool", "assistant"]
    assert agent.transcript.messages[1].text == "Let me look."
    second = adapter.sent[1][1]
    assert [m.role for m in second] == ["user", "assistant", "tool", "tool"]
    assert second[1].tool_calls[0] == ToolCallEvent("toolu_1", "read_file", {"path": "notes.txt"})
    assert second[2].tool_call_id == "toolu_1" and "alpha" in second[2].text
    assert second[3].tool_call_id == "toolu_2" and "notes.txt:2: beta" in second[3].text


async def test_invalid_and_excess_calls_are_answered_with_errors(  # type: ignore[no-untyped-def]
    services, tmp_path: Path
) -> None:
    agent, adapter, ui = conductor(
        services, tmp_path,
        tools(
            ToolCallEvent("a", "list_dir", {}),
            ToolCallEvent("b", "read_file", {}, error="invalid_arguments"),
            ToolCallEvent("c", "list_dir", {}),
        ),
        answer("ok"),
        max_calls_per_step=2,
    )
    result = await agent.turn("look")
    assert result.tool_calls == 3
    finished = [e for e in ui.events if isinstance(e, ToolFinished)]
    assert [e.ok for e in finished] == [True, False, False]
    results = [m for m in adapter.sent[1][1] if m.role == "tool"]
    assert "invalid_arguments" in results[1].text and results[1].is_error
    assert "too_many_calls" in results[2].text


async def test_session_budget_stops_the_turn_with_a_notice(  # type: ignore[no-untyped-def]
    services, tmp_path: Path
) -> None:
    agent, _, ui = conductor(
        services, tmp_path,
        tools(ToolCallEvent("a", "list_dir", {})), tools(ToolCallEvent("b", "list_dir", {})),
        answer("x"),
        session_usd=0.0015, call_usd=0.001,
    )
    result = await agent.turn("loop")
    assert "session budget" in result.text
    assert any(isinstance(e, Notice) and e.level == "error" for e in ui.events)
    assert agent.spent_usd == pytest.approx(COST)


async def test_cancel_keeps_completed_work_and_the_next_turn_still_converts(  # type: ignore[no-untyped-def]
    services, tmp_path: Path
) -> None:
    agent, adapter, ui = conductor(services, tmp_path, tools(ToolCallEvent("a", "list_dir", {})))
    adapter.pause_on = 2
    task = asyncio.create_task(agent.turn("look around"))
    await asyncio.wait_for(adapter.paused.wait(), 30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [m.role for m in agent.transcript.messages] == ["user", "assistant", "tool", "note"]
    assert TextDelta("partial") in ui.events and ui.texts() == []
    assert Thinking(False) == ui.events[-1]
    adapter.replies = [answer("back")]
    assert (await agent.turn("continue")).text == "back"
    last = adapter.sent[-1][1]
    assert [m.role for m in last] == ["user", "assistant", "tool", "user", "user"]


async def test_build_mode_and_project_instructions_reach_the_system(  # type: ignore[no-untyped-def]
    services, tmp_path: Path
) -> None:
    agent, adapter, _ = conductor(services, tmp_path, answer("ok"))
    agent.mode = "build"
    agent.instructions = "Use tabs."
    await agent.turn("an agent that tags support emails")
    system = adapter.sent[0][0]
    assert BUILD_MODE in system and "Use tabs." in system


async def test_build_mode_sends_the_first_description_to_make_natively(  # type: ignore[no-untyped-def]
    services, tmp_path: Path
) -> None:
    """The routed ``make`` call converts to a provider-valid conversation: the call, then
    its answer, then the model's reply about it."""
    agent, adapter, _ = conductor(services, tmp_path, answer("Built it."))
    seen: list[str] = []
    agent.extras["make"] = recording_make(seen)
    agent.mode = "build"
    result = await agent.turn("an agent that tags support emails by topic")
    assert seen == ["an agent that tags support emails by topic"]
    assert result.tool_calls == 1 and result.text == "Built it."
    messages = adapter.sent[0][1]
    assert [m.role for m in messages] == ["user", "assistant", "tool"]
    assert messages[1].tool_calls[0].name == "make"
    assert messages[2].tool_call_id == messages[1].tool_calls[0].id


@pytest.mark.parametrize(
    ("answers", "stage"), [(DECLINE_PLAN, "plan"), (CANCEL_A_QUESTION, "interview")]
)
async def test_a_build_the_user_sets_aside_ends_the_native_turn_in_one_sentence(  # type: ignore[no-untyped-def]
    tmp_path: Path, answers: list[str | None], stage: str
) -> None:
    """As in the envelope loop: the real build mode, a declined plan or a cancelled
    question, then one fixed sentence and no streamed model call. The next turn converts
    to a provider-valid conversation that shows what happened."""
    adapter = Streaming()
    agent = build_mode(tmp_path, adapter, answers)
    ui = agent.ui
    assert isinstance(ui, RecordingUI)
    try:
        result = await agent.turn(DESCRIPTION)
        assert result.text == SET_ASIDE and result.tool_calls == 1
        assert adapter.sent == []
        assert ui.texts() == [SET_ASIDE]
        assert not [e for e in ui.events if isinstance(e, TextDelta)]
        made = json.loads(agent.transcript.messages[2].text)
        assert made["cancelled"] is True and made["stage"] == stage
        adapter.replies = [answer("Raised.")]
        assert (await agent.turn("raise the bar to 0.5 and build it again")).text == "Raised."
        messages = adapter.sent[0][1]
        assert [m.role for m in messages] == ["user", "assistant", "tool", "assistant", "user"]
        assert messages[1].tool_calls[0].name == "make" and not messages[2].is_error
        assert messages[3] == ChatMessage("assistant", SET_ASIDE)
    finally:
        agent.services.close()  # type: ignore[attr-defined]


async def test_a_make_the_model_asked_for_ends_the_native_turn_when_set_aside(  # type: ignore[no-untyped-def]
    services, tmp_path: Path
) -> None:
    """A declined ``make`` from the model's own reply ends the turn there. The reply's
    later call does not run; the next turn answers it as not run."""
    agent, adapter, ui = conductor(
        services, tmp_path,
        tools(
            ToolCallEvent("toolu_1", "make", {"description": "x"}),
            ToolCallEvent("toolu_2", "list_dir", {}),
        ),
        answer("never streamed"),
    )
    seen: list[str] = []
    agent.extras["make"] = recording_make(seen, DECLINED)
    result = await agent.turn("make an agent that does x")
    assert seen == ["x"]
    assert result.text == SET_ASIDE and result.tool_calls == 1
    assert len(adapter.sent) == 1
    assert [e.name for e in ui.events if isinstance(e, ToolStarted)] == ["make"]
    assert ui.texts() == [SET_ASIDE]
    adapter.replies = [answer("ok")]
    await agent.turn("what happened?")
    messages = adapter.sent[1][1]
    assert [m.role for m in messages] == [
        "user", "assistant", "tool", "tool", "assistant", "user"]
    assert messages[3].tool_call_id == "toolu_2" and "not_run" in messages[3].text
    assert messages[4] == ChatMessage("assistant", SET_ASIDE)


async def test_a_build_that_ended_without_an_agent_runs_no_more_native_tools(  # type: ignore[no-untyped-def]
    services, tmp_path: Path
) -> None:
    """Live, on Nemotron 3.5 Lightning: after the build stopped at case drafting the model
    said a few words and asked for the coder, which asked for 57 shell commands. No tool
    runs after such a build, and the turn ends with what the build said."""
    agent, adapter, ui = conductor(
        services, tmp_path,
        tools(
            ToolCallEvent("toolu_1", "code", {"task": "create a training data file"}),
            ToolCallEvent("toolu_2", "make", {"description": "again"}),
            text="The first make attempt ran but",
        ),
        answer("never streamed"),
    )
    seen: list[str] = []
    agent.extras["make"] = recording_make(seen, STOPPED)
    agent.mode = "build"
    result = await agent.turn(EMAILS)
    assert seen == [EMAILS]
    assert len(adapter.sent) == 1
    assert [e.name for e in ui.events if isinstance(e, ToolStarted)] == ["make"]
    assert result.text == build_ended_text(STOPPED) and result.tool_calls == 1  # type: ignore[arg-type]
    assert ui.texts() == [result.text]
    adapter.replies = [answer("ok")]
    await agent.turn("what happened?")
    messages = adapter.sent[1][1]
    assert [m.role for m in messages] == ["user", "assistant", "tool", "assistant", "user"]
    assert messages[3] == ChatMessage("assistant", result.text)


async def test_a_build_that_ended_in_an_error_runs_no_more_native_tools(  # type: ignore[no-untyped-def]
    services, tmp_path: Path
) -> None:
    """A build that ends in a tool error is a build without an agent too: the coder the
    model asks for next does not start."""
    agent, adapter, ui = conductor(
        services, tmp_path,
        tools(ToolCallEvent("toolu_1", "code", {"task": "create a training data file"})),
        answer("never streamed"),
    )
    agent.extras["make"] = failing_make(ToolError("profile_not_editable"))
    agent.mode = "build"
    result = await agent.turn(EMAILS)
    assert len(adapter.sent) == 1
    assert [e.name for e in ui.events if isinstance(e, ToolStarted)] == ["make"]
    assert result.text == BUILD_ENDED + " " + BUILD_ERROR and result.tool_calls == 1


async def test_native_calls_asked_for_with_a_build_that_ended_do_not_run(  # type: ignore[no-untyped-def]
    services, tmp_path: Path
) -> None:
    agent, adapter, ui = conductor(
        services, tmp_path,
        tools(
            ToolCallEvent("toolu_1", "make", {"description": "x"}),
            ToolCallEvent("toolu_2", "list_dir", {}),
        ),
        answer("The build stopped."),
    )
    seen: list[str] = []
    agent.extras["make"] = recording_make(seen, STOPPED)
    result = await agent.turn("make an agent that does x")
    assert seen == ["x"] and result.text == "The build stopped."
    assert [e.name for e in ui.events if isinstance(e, ToolStarted)] == ["make"]
    messages = adapter.sent[1][1]
    assert [m.role for m in messages] == ["user", "assistant", "tool", "tool"]
    assert messages[3].tool_call_id == "toolu_2" and "not_run" in messages[3].text


async def test_the_step_limit_ends_the_turn(services, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    agent, _, ui = conductor(
        services, tmp_path,
        tools(ToolCallEvent("a", "list_dir", {})), tools(ToolCallEvent("b", "list_dir", {})),
        max_steps=2,
    )
    result = await agent.turn("loop")
    assert "step limit" in result.text and result.tool_calls == 2
    assert any(isinstance(e, Notice) and e.level == "warn" for e in ui.events)


async def test_an_empty_answer_cut_at_the_limit_is_reported(  # type: ignore[no-untyped-def]
    services, tmp_path: Path
) -> None:
    agent, _, ui = conductor(
        services, tmp_path, [UsageEvent(1, 1, COST), DoneEvent("max_tokens")]
    )
    result = await agent.turn("hi")
    assert result.text.startswith("(The model returned")
    assert any(isinstance(e, Notice) and "output limit" in e.text for e in ui.events)


async def test_the_session_meter_sees_streamed_calls_through_observed_adapters(  # type: ignore[no-untyped-def]
    services, tmp_path: Path
) -> None:
    meter = Meter()
    inner = Streaming(answer("Hi."))
    ui = RecordingUI()
    agent = Conductor(
        services, ObservedAdapter(inner, meter), tmp_path, ui, extras={"meter": meter}
    )
    assert isinstance(streaming_adapter(agent.adapter), ObservedChat)
    result = await agent.turn("hi")
    assert result.text == "Hi." and result.cost_usd == pytest.approx(COST)
    assert meter.calls == 1 and meter.in_flight == 0 and agent.own_usd == 0.0
    assert meter.spend.cost_usd == pytest.approx(COST) and meter.spend.tokens_in == 10


async def test_cancel_through_observed_adapter_settles_the_meter(  # type: ignore[no-untyped-def]
    services, tmp_path: Path
) -> None:
    meter = Meter()
    inner = Streaming()
    inner.pause_on = 1
    agent = Conductor(
        services, ObservedAdapter(inner, meter), tmp_path, RecordingUI(), extras={"meter": meter}
    )
    task = asyncio.create_task(agent.turn("hi"))
    await asyncio.wait_for(inner.paused.wait(), 30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert meter.in_flight == 0 and meter.failed == 1 and meter.spend.cost_usd == 0.0


def test_envelope_adapters_are_not_native() -> None:
    from architect.models import FakeAdapter

    assert streaming_adapter(FakeAdapter()) is None
    assert streaming_adapter(ObservedAdapter(FakeAdapter(), Meter())) is None
    native = Streaming()
    assert streaming_adapter(native) is native


def test_transcripts_convert_to_provider_valid_conversations() -> None:
    transcript = Transcript()
    transcript.user("first")
    # An envelope-era turn: model-chosen ids that repeat across turns.
    transcript.assistant(calls=[{"id": "call-1", "name": "list_dir", "arguments": {}}])
    transcript.tool("call-1", "list_dir", {"entries": []})
    transcript.assistant("done")
    transcript.user("second")
    transcript.assistant(calls=[
        {"id": "call-1", "name": "grep", "arguments": {"pattern": "x"}},
        {"id": "bad id!", "name": "read_file", "arguments": "not-an-object"},
    ])
    transcript.tool("call-1", "grep", {"matches": []}, ok=False)
    # The second call was interrupted: no result.
    transcript.note("The user interrupted this turn; finished tool results are kept.")
    transcript.tool("gone", "glob", {"files": []})  # its call was dropped by fit()
    transcript.user("third")
    messages = chat_messages(transcript)
    assert valid_conversation(messages)
    assert [m.role for m in messages] == [
        "user", "assistant", "tool", "assistant", "user", "assistant", "tool", "tool",
        "user", "user", "user",
    ]
    first_call = messages[1].tool_calls[0]
    second_calls = messages[5].tool_calls
    assert first_call.id == "call-1"
    assert second_calls[0].id not in {"call-1", "bad id!"} and second_calls[0].name == "grep"
    assert second_calls[1].arguments == {} and second_calls[1].id != second_calls[0].id
    assert messages[6].tool_call_id == second_calls[0].id and messages[6].is_error
    assert messages[7].tool_call_id == second_calls[1].id and "not_run" in messages[7].text
    assert messages[8].text.startswith("[Note from Architect")
    assert messages[9].text.startswith("[Earlier result of glob]")
    assert chat_messages(transcript) == messages  # deterministic: prompt caches keep hitting
