"""The Phase 12 conductor: envelope loop, tools, budget and cancellation (offline)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from session_support import fake_profile, response

from architect.agent import Conductor, ConductorSettings, RecordingUI, ToolError, Transcript
from architect.agent import conductor as conductor_module
from architect.agent.conductor import (
    BUILD_ENDED,
    SET_ASIDE,
    build_ended_text,
    project_instructions,
)
from architect.agent.events import AssistantText, Notice, ToolFinished, ToolStarted
from architect.models import ModelRequest, ModelResponse
from architect.models.types import UsageQuote
from architect.profiles import build_services, load_profile


class Scripted:
    """Replies in order; records every prompt; a fixed small price per call."""

    def __init__(self, *replies: str, model: str = "fake", cost: float = 0.001) -> None:
        self.model = model
        self.replies = list(replies)
        self.cost = cost
        self.prompts: list[str] = []

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=1, tokens_out=1, cost_usd=self.cost)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.prompts.append(request.prompt)
        text = self.replies.pop(0) if self.replies else '{"text": "done"}'
        return ModelResponse(text=text, model=self.model, tokens_in=1, tokens_out=1,
                             cost_usd=self.cost)


def calls(*items: tuple[str, dict[str, object]]) -> str:
    return json.dumps({"tool_calls": [
        {"id": f"call-{i}", "name": name, "arguments": args} for i, (name, args) in enumerate(items)
    ]})


@pytest.fixture
def services(tmp_path: Path):  # type: ignore[no-untyped-def]
    path = fake_profile(tmp_path)
    profile = load_profile(path)
    return build_services(profile, profile.workspace)


def conductor(  # type: ignore[no-untyped-def]
    services, cwd: Path, *replies: str, **settings: object
) -> tuple[Conductor, Scripted, RecordingUI]:
    adapter = Scripted(*replies)
    ui = RecordingUI()
    agent = Conductor(services, adapter, cwd, ui, ConductorSettings(**settings))  # type: ignore[arg-type]
    return agent, adapter, ui


async def test_a_plain_answer_ends_the_turn(services, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    agent, adapter, ui = conductor(services, tmp_path, '{"text": "Hello."}')
    result = await agent.turn("hi")
    assert result.text == "Hello." and result.tool_calls == 0
    assert ui.texts() == ["Hello."]
    assert [m.role for m in agent.transcript.messages] == ["user", "assistant"]
    # The stable prefix carries the protocol and the tool list; the prompt ends with a reminder.
    request = adapter.prompts[0]
    assert '"tool_calls"' in request and "read_file" in request
    assert request.rstrip().endswith("to answer the user.")


async def test_tools_run_and_their_results_reach_the_next_call(services, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    agent, adapter, ui = conductor(
        services, tmp_path,
        calls(("read_file", {"path": "notes.txt"}), ("grep", {"pattern": "bet"})),
        '{"text": "It says alpha and beta."}',
    )
    result = await agent.turn("what is in notes.txt?")
    assert result.tool_calls == 2 and result.text == "It says alpha and beta."
    started = [e for e in ui.events if isinstance(e, ToolStarted)]
    finished = [e for e in ui.events if isinstance(e, ToolFinished)]
    assert [e.summary for e in started] == ["read_file(notes.txt)", "grep(bet)"]
    assert all(e.ok for e in finished)
    assert "alpha" in adapter.prompts[1] and "notes.txt:2: beta" in adapter.prompts[1]


async def test_prose_is_a_final_answer_and_a_missed_tool_call_is_repaired(  # type: ignore[no-untyped-def]
    services, tmp_path: Path
) -> None:
    agent, _, _ = conductor(services, tmp_path, "Just a plain sentence.")
    assert (await agent.turn("hello")).text == "Just a plain sentence."
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    agent, adapter, _ = conductor(
        services, tmp_path, "read_file", calls(("read_file", {"path": "a.md"})), '{"text": "ok"}'
    )
    assert (await agent.turn("read a.md")).text == "ok"
    assert "not a JSON object in the required format" in adapter.prompts[1]


async def test_tool_errors_are_results_not_crashes(services, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    agent, adapter, ui = conductor(
        services, tmp_path,
        calls(
            ("read_file", {"path": "../outside.txt"}),
            ("nope", {}),
            ("read_file", {"path": "missing"}),
        ),
        '{"text": "could not"}',
    )
    await agent.turn("try")
    finished = [e for e in ui.events if isinstance(e, ToolFinished)]
    assert [e.ok for e in finished] == [False, False, False]
    later = adapter.prompts[1]
    assert "path_outside_cwd" in later and "unknown_tool" in later and "not_found" in later


async def test_the_conductor_never_writes_files(services, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    agent, _, _ = conductor(services, tmp_path, '{"text": "x"}')
    names = {tool.name for tool in agent.tools}
    assert {"read_file", "list_dir", "glob", "grep", "code", "spawn", "make"} <= names
    assert not names & {"write", "write_file", "edit", "bash", "shell"}
    # Without a coder bound, a change request fails with a code the model can read.
    change = calls(("code", {"task": "add a file"}))
    agent, adapter, _ = conductor(services, tmp_path, change, '{"text": "no"}')
    await agent.turn("add a file")
    assert "coder_unavailable" in adapter.prompts[1]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["w"]  # noqa: ASYNC240


async def test_session_budget_stops_the_turn_with_a_notice(services, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    agent, _, ui = conductor(
        services, tmp_path, calls(("list_dir", {})), calls(("list_dir", {})), '{"text": "x"}',
        session_usd=0.0015, call_usd=0.001,
    )
    result = await agent.turn("loop")
    assert "session budget" in result.text
    assert any(isinstance(e, Notice) and e.level == "error" for e in ui.events)


async def test_cancel_keeps_completed_work_in_the_transcript(services, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    agent, adapter, _ = conductor(services, tmp_path, calls(("list_dir", {})))
    gate = asyncio.Event()
    original = adapter.complete

    async def slow(request: ModelRequest) -> ModelResponse:
        if adapter.prompts:
            gate.set()
            await asyncio.sleep(3600)
        return await original(request)

    adapter.complete = slow  # type: ignore[method-assign]
    task = asyncio.create_task(agent.turn("look around"))
    await asyncio.wait_for(gate.wait(), 30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    roles = [m.role for m in agent.transcript.messages]
    assert roles == ["user", "assistant", "tool", "note"]


async def test_build_mode_steers_toward_make(services, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    agent, adapter, _ = conductor(services, tmp_path, '{"text": "ok"}')
    agent.mode = "build"
    await agent.turn("an agent that tags support emails")
    assert "BUILD MODE" in adapter.prompts[0]


BUILT: dict[str, object] = {"licensed": True, "installed": "agent", "revision": 1}
# What build mode returns when the user declines the plan (Not now).
DECLINED: dict[str, object] = {"cancelled": True, "stage": "plan", "spec_hash": "abc"}


def recording_make(seen: list[str], result: dict[str, object] | None = None):  # type: ignore[no-untyped-def]
    async def make(description: str, ctx: object, **_: object) -> dict[str, object]:
        seen.append(description)
        return result if result is not None else BUILT
    return make


def build_mode(tmp_path: Path, adapter: object, answers: list[str | None]) -> Conductor:
    """A build-mode conductor with the real ``make`` on the fake profile, whose inference
    fixture answers the interview and Stage 0. ``answers`` reply to build mode's questions
    in order: "" is Enter (the default), None is Esc, "no" declines the plan as Not now
    does. Close ``agent.services`` after."""
    from test_agent_build import INTERVIEW, spec_json

    from architect.agent.build import make, set_override

    path = fake_profile(tmp_path)
    (path.parent / "inference.json").write_text(
        json.dumps([response(INTERVIEW), response(spec_json())]), encoding="utf-8"
    )
    profile = load_profile(path)
    services = build_services(profile, profile.workspace)
    ui = RecordingUI(answers=list(answers))  # it pops what it answers
    agent = Conductor(services, adapter, tmp_path, ui)  # type: ignore[arg-type]
    agent.extras.update(make=make, profile_path=path, build_overrides={})
    set_override(agent.context, "engineer", "preset")
    agent.mode = "build"
    return agent


DESCRIPTION = "make an agent that extracts invoice fields from text"
# The interview asks where the agent lives, then its two uncertain fields; then the plan.
DECLINE_PLAN: list[str | None] = ["", "", "", "no"]
CANCEL_A_QUESTION: list[str | None] = [None]


async def test_build_mode_sends_the_first_description_to_make(  # type: ignore[no-untyped-def]
    services, tmp_path: Path
) -> None:
    """A filmed take: in build mode the model read a receipt in the folder and answered the
    request instead of building. The first description now goes to ``make`` directly."""
    agent, adapter, _ = conductor(services, tmp_path, '{"text": "Built it."}')
    seen: list[str] = []
    agent.extras["make"] = recording_make(seen)
    agent.mode = "build"
    text = "Read a receipt's text and give back the total, subtotal, tax and item count"
    result = await agent.turn(text)
    assert seen == [text]
    assert len(adapter.prompts) == 1 and result.tool_calls == 1
    assert [m.role for m in agent.transcript.messages] == ["user", "assistant", "tool", "assistant"]
    assert agent.transcript.messages[1].calls[0]["name"] == "make"


async def test_build_mode_leaves_questions_short_replies_and_follow_ups_to_the_model(  # type: ignore[no-untyped-def]
    services, tmp_path: Path
) -> None:
    agent, _, _ = conductor(services, tmp_path, *(['{"text": "ok"}'] * 4))
    seen: list[str] = []
    agent.extras["make"] = recording_make(seen)
    agent.mode = "build"
    await agent.turn("what kinds of agents can you build?")
    await agent.turn("ok thanks")
    assert seen == []
    await agent.turn("an agent that tags support emails by topic")
    await agent.turn("raise the bar to 0.5 and build it again")
    assert seen == ["an agent that tags support emails by topic"]


async def test_normal_mode_never_routes_to_make(services, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    agent, _, _ = conductor(services, tmp_path, '{"text": "ok"}')
    seen: list[str] = []
    agent.extras["make"] = recording_make(seen)
    await agent.turn("an agent that tags support emails by topic")
    assert seen == []


async def test_one_build_per_turn(services, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A filmed take: after the user said Not now, the model started a second build on its
    own in the same turn. A declined build now ends the turn (below); after a build that
    ran, a second ``make`` in the same turn is still refused without running."""
    agent, _, ui = conductor(
        services, tmp_path,
        calls(("make", {"description": "x"})),
        calls(("make", {"description": "x, again"})),
        '{"text": "One build per turn."}',
    )
    seen: list[str] = []
    agent.extras["make"] = recording_make(seen)
    await agent.turn("make an agent that does x")
    assert seen == ["x"]
    refused = [m for m in agent.transcript.messages if m.role == "tool"][1]
    assert not refused.ok and "one_build_per_turn" in refused.text
    finished = [e for e in ui.events if isinstance(e, ToolFinished)]
    assert [e.ok for e in finished] == [True, False]
    # The next turn may build again.
    agent2, _, _ = conductor(
        services, tmp_path, calls(("make", {"description": "y"})), '{"text": "ok"}'
    )
    agent2.extras["make"] = recording_make(seen)
    agent2.makes_this_turn = 1  # left over from an earlier turn
    await agent2.turn("make an agent that does y")
    assert seen == ["x", "y"]


@pytest.mark.parametrize(
    ("answers", "stage"), [(DECLINE_PLAN, "plan"), (CANCEL_A_QUESTION, "interview")]
)
async def test_a_build_the_user_sets_aside_ends_the_turn_in_one_sentence(  # type: ignore[no-untyped-def]
    tmp_path: Path, answers: list[str | None], stage: str
) -> None:
    """A filmed take: after Not now on the drafted plan, the model went on by itself. It
    tried ``make`` again (refused, shown as a failed build) or asked questions of its own.
    The real build mode here: a declined plan or a cancelled question ends the turn with
    one fixed sentence, and the conversation's model is never called."""
    adapter = Scripted()
    agent = build_mode(tmp_path, adapter, answers)
    ui = agent.ui
    assert isinstance(ui, RecordingUI)
    try:
        result = await agent.turn(DESCRIPTION)
        assert result.text == SET_ASIDE and result.tool_calls == 1
        assert adapter.prompts == []
        assert ui.texts() == [SET_ASIDE]
        if stage == "plan":
            assert ui.questions[-1].startswith("Compile this agent?")
        messages = agent.transcript.messages
        assert [m.role for m in messages] == ["user", "assistant", "tool", "assistant"]
        made = json.loads(messages[2].text)
        assert messages[2].name == "make" and messages[2].ok
        assert made["cancelled"] is True and made["stage"] == stage
        assert messages[3].text == SET_ASIDE
        finished = [e for e in ui.events if isinstance(e, ToolFinished)]
        assert [(e.name, e.ok) for e in finished] == [("make", True)]
        assert not agent.services.registry.list()  # type: ignore[attr-defined]
        # The next message goes to the model, which sees what happened.
        adapter.replies.append('{"text": "Raised."}')
        assert (await agent.turn("raise the bar to 0.5 and build it again")).text == "Raised."
        assert SET_ASIDE in adapter.prompts[0] and "cancelled" in adapter.prompts[0]
    finally:
        agent.services.close()  # type: ignore[attr-defined]


@pytest.mark.parametrize(("tool", "argument"), [("make", "description"), ("forge", "target")])
async def test_a_build_the_model_asked_for_ends_the_turn_when_set_aside(  # type: ignore[no-untyped-def]
    services, tmp_path: Path, tool: str, argument: str
) -> None:
    """The model's own build, declined (``forge`` on a description runs the same build
    mode): the turn ends right there, and a call the model asked for after it in the same
    step does not run."""
    agent, adapter, ui = conductor(
        services, tmp_path,
        calls((tool, {argument: "x"}), ("list_dir", {})),
        '{"text": "never asked for"}',
    )
    seen: list[str] = []
    agent.extras[tool] = recording_make(seen, DECLINED)
    result = await agent.turn("make an agent that does x")
    assert seen == ["x"]
    assert result.text == SET_ASIDE and result.tool_calls == 1
    assert len(adapter.prompts) == 1  # the call that asked for the build, and no other
    assert [e.name for e in ui.events if isinstance(e, ToolStarted)] == [tool]
    assert ui.texts() == [SET_ASIDE]
    assert [m.role for m in agent.transcript.messages] == ["user", "assistant", "tool", "assistant"]


# The words for a build that ended in an error (absent before the guard covered one).
BUILD_ERROR = str(getattr(conductor_module, "BUILD_ERROR", "<no words for a build error>"))
# What build mode returned on the live run of the first Describe example.
STOPPED: dict[str, object] = {
    "licensed": False,
    "compiled": False,
    "failures": ["floor_draft_failed"],
    "next_step": "add your own labeled cases with /floor add <file>",
    "cost_usd": 0.001,
}
EMAILS = "Sort support emails into billing, bug report or question"


async def test_a_build_that_ended_without_an_agent_runs_no_more_tools(  # type: ignore[no-untyped-def]
    services, tmp_path: Path
) -> None:
    """Live: the build of this sentence stopped at case drafting. The conversation's model
    then asked for a second build and started the coder, which asked for 57 shell commands.
    After such a build the model gets one reply; when it asks for a tool, none runs and
    the turn ends with what the build said."""
    agent, adapter, ui = conductor(
        services, tmp_path,
        calls(("code", {"task": "create a training data file"}), ("make", {"description": "x"})),
        calls(("shell", {"command": "ls"})),
        '{"text": "never asked for"}',
    )
    seen: list[str] = []
    agent.extras["make"] = recording_make(seen, STOPPED)
    agent.mode = "build"
    result = await agent.turn(EMAILS)
    assert seen == [EMAILS]
    assert len(adapter.prompts) == 1  # one reply after the build, and no other
    assert [e.name for e in ui.events if isinstance(e, ToolStarted)] == ["make"]
    assert result.tool_calls == 1
    assert result.text == build_ended_text(STOPPED) == (  # type: ignore[arg-type]
        BUILD_ENDED + " Add your own labeled cases with /floor add <file>."
    )
    assert ui.texts() == [result.text]
    assert [m.role for m in agent.transcript.messages] == ["user", "assistant", "tool", "assistant"]
    # The code is not in the sentence a person reads; it stays in the build's own result,
    # which is what the transcript and the logs keep.
    assert "floor_draft_failed" not in result.text
    assert "floor_draft_failed" in str(agent.transcript.messages[2])
    # The next turn is the user's: the model may use tools again.
    adapter.replies = [calls(("list_dir", {})), '{"text": "Listed."}']
    assert (await agent.turn("what is in this folder?")).text == "Listed."
    assert [e.name for e in ui.events if isinstance(e, ToolStarted)] == ["make", "list_dir"]


async def test_calls_asked_for_with_a_build_that_ended_do_not_run(  # type: ignore[no-untyped-def]
    services, tmp_path: Path
) -> None:
    """The model's own build, with another call in the same step: the build runs, the
    other call does not, and the model's answer about the build is the turn's answer."""
    agent, adapter, ui = conductor(
        services, tmp_path,
        calls(("make", {"description": "x"}), ("list_dir", {})),
        '{"text": "The build stopped: no cases could be drafted."}',
    )
    seen: list[str] = []
    agent.extras["make"] = recording_make(seen, STOPPED)
    result = await agent.turn("make an agent that does x")
    assert seen == ["x"]
    assert result.text == "The build stopped: no cases could be drafted."
    assert len(adapter.prompts) == 2
    assert [e.name for e in ui.events if isinstance(e, ToolStarted)] == ["make"]
    for prompt in adapter.prompts:
        assert "is a build that ended without an" in prompt and "no shell" in prompt


def test_the_fixed_answer_of_a_build_that_ended_says_what_the_build_said() -> None:
    refused = {"licensed": False, "compiled": True, "outcome": "This build was refused.",
               "failures": ["primary_floor_failed"], "next_step": "lower the bar with /floor 0.7"}
    assert build_ended_text(refused) == (  # type: ignore[arg-type]
        "This build was refused. Lower the bar with /floor 0.7."
    )  # live, the meeting example: the outcome already says nothing was installed
    assert build_ended_text({}) == BUILD_ENDED
    # A next step that is already whole sentences is not given a second full stop, and the
    # failure code is never part of what a person reads.
    whole = {"licensed": False, "failures": ["floor_too_small"],
             "next_step": 'the file holds cases. Each has a "task" and an "expected".'}
    assert build_ended_text(whole) == (  # type: ignore[arg-type]
        BUILD_ENDED + ' The file holds cases. Each has a "task" and an "expected".'
    )


def failing_make(error: Exception):  # type: ignore[no-untyped-def]
    async def make(description: str, ctx: object, **_: object) -> dict[str, object]:
        raise error
    return make


@pytest.mark.parametrize(
    "error", [ToolError("profile_not_editable"), RuntimeError("boom")], ids=["tool", "other"]
)
async def test_a_build_that_ended_in_an_error_runs_no_more_tools(  # type: ignore[no-untyped-def]
    services, tmp_path: Path, error: Exception
) -> None:
    """The review's finding: the guard fired only on a result that said ``licensed:
    false``. A build that ends in an error is as much a build without an agent, and it
    left every tool open, the opening the 57 shell commands came through."""
    agent, adapter, ui = conductor(
        services, tmp_path,
        calls(("shell", {"command": "ls"})),
        '{"text": "never asked for"}',
    )
    agent.extras["make"] = failing_make(error)
    agent.mode = "build"
    result = await agent.turn(EMAILS)
    assert [e.name for e in ui.events if isinstance(e, ToolStarted)] == ["make"]
    assert len(adapter.prompts) == 1 and result.tool_calls == 1
    assert result.text == BUILD_ENDED + " " + BUILD_ERROR
    assert "profile_not_editable" not in result.text and "RuntimeError" not in result.text
    assert [m.role for m in agent.transcript.messages] == ["user", "assistant", "tool", "assistant"]


async def test_the_model_may_still_answer_about_a_build_that_ended_in_an_error(  # type: ignore[no-untyped-def]
    services, tmp_path: Path
) -> None:
    agent, adapter, ui = conductor(
        services, tmp_path,
        calls(("make", {"description": "x"}), ("list_dir", {})),
        '{"text": "The build stopped: this profile cannot be edited."}',
    )
    agent.extras["make"] = failing_make(ToolError("profile_not_editable"))
    result = await agent.turn("make an agent that does x")
    assert result.text == "The build stopped: this profile cannot be edited."
    assert [e.name for e in ui.events if isinstance(e, ToolStarted)] == ["make"]


async def test_a_build_with_a_cancelled_export_still_goes_back_to_the_model(  # type: ignore[no-untyped-def]
    services, tmp_path: Path
) -> None:
    """Only a build the user set aside ends the turn. An installed build whose export into
    the project was declined carries ``cancelled`` inside ``export``; the model reports it."""
    agent, adapter, _ = conductor(
        services, tmp_path,
        calls(("make", {"description": "x"})), '{"text": "Built, not exported."}',
    )
    seen: list[str] = []
    agent.extras["make"] = recording_make(
        seen, {**BUILT, "export": {"exported": False, "cancelled": True}}
    )
    assert (await agent.turn("make an agent that does x")).text == "Built, not exported."
    assert len(adapter.prompts) == 2


async def test_a_build_that_installed_nothing_reaches_the_model_as_such(  # type: ignore[no-untyped-def]
    services, tmp_path: Path
) -> None:
    """A live run called an earlier installed build "Licensed" after a rebuild was refused.
    The model is told how to read a ``make`` result without ``installed`` and gets its
    ``outcome``."""
    from architect.agent.native import NATIVE_SYSTEM

    outcome = ("This build was refused, so nothing was installed or exported. The installed "
               "x v1 is an earlier build and stays as it was.")

    async def make(description: str, ctx: object, **_: object) -> dict[str, object]:
        return {"licensed": False, "compiled": True, "outcome": outcome, "kept": "x v1"}

    agent, adapter, _ = conductor(
        services, tmp_path, calls(("make", {"description": "x"})), '{"text": "Refused."}'
    )
    agent.extras["make"] = make
    await agent.turn("make x again")
    rule = ("When it names none, the new build was not installed: say so plainly, with its "
            "`outcome` when it has one. An agent of the same name that `agents` lists is then "
            "an earlier build, not this one.")
    for prompt in (adapter.prompts[1], NATIVE_SYSTEM):
        assert rule in " ".join(prompt.split())
    assert outcome in adapter.prompts[1]


def test_project_instructions_prefer_architect_md(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("claude rules", encoding="utf-8")
    assert "claude rules" in project_instructions(tmp_path)
    (tmp_path / "ARCHITECT.md").write_text("architect rules", encoding="utf-8")
    text = project_instructions(tmp_path)
    assert "architect rules" in text and "claude rules" not in text


def test_transcript_round_trips_and_fits() -> None:
    transcript = Transcript()
    transcript.user("hi")
    transcript.assistant(calls=[{"id": "c", "name": "grep", "arguments": {}}])
    transcript.tool("c", "grep", {"matches": ["x" * 5000]})
    transcript.assistant("done")
    again = Transcript.from_jsonl(transcript.to_jsonl())
    assert again.render() == transcript.render()
    assert transcript.fit(600) > 0 and len(transcript.render()) <= 600
    tiny = Transcript()
    for index in range(50):
        tiny.user("message " * 20 + str(index))
    tiny.fit(400)
    assert len(tiny.render()) <= 400 or len(tiny.messages) <= 2
    assert tiny.messages[0].role == "note"
    assert isinstance(AssistantText("x"), AssistantText)


async def test_spawn_runs_a_preset_subagent_unlicensed(services, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    agent, adapter, ui = conductor(
        services, tmp_path,
        calls(("spawn", {"preset": "synthesizer", "task": "Summarize", "input": {"t": 1}})),
        calls(("spawn", {"preset": "extractor", "task": "Extract the label", "input": {"t": 1}})),
        '{"text": "extracted"}',
    )
    result = await agent.turn("extract it")
    assert result.text == "extracted"
    finished = [e for e in ui.events if isinstance(e, ToolFinished)]
    assert finished[0].ok, adapter.prompts[1][-400:]
    assert '\\"licensed\\": false' in adapter.prompts[1]
    # The fixture answers "ok", which is not an object: the extractor fails with a fixed reason.
    assert not finished[1].ok and "subagent_failed" in adapter.prompts[2]


def test_plain_text_becomes_the_single_string_field() -> None:
    from architect.agent.tools import shape_input

    schema = {"type": "object", "required": ["invoice_text"],
              "properties": {"invoice_text": {"type": "string"}}}
    assert shape_input("INVOICE #1", schema) == {"invoice_text": "INVOICE #1"}  # type: ignore[arg-type]
    assert shape_input({"invoice_text": "x"}, schema) == {"invoice_text": "x"}  # type: ignore[arg-type]
    two = {"type": "object", "required": ["a", "b"],
           "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    assert shape_input("text", two) == "text"  # type: ignore[arg-type]


async def test_attached_images_reach_a_model_that_takes_them(services, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

    from architect.models.images import ImageInput, encode_png_gray

    image = ImageInput.from_bytes(encode_png_gray([b"\x00" * 4] * 3, 4, 3))
    seen: list[int] = []

    class Vision(Scripted):
        _price = SimpleNamespace(image=object())

        async def complete(self, request: ModelRequest) -> ModelResponse:
            seen.append(len(request.images))
            return await super().complete(request)

    adapter = Vision('{"text": "a small grey image"}')
    agent = Conductor(services, adapter, tmp_path, RecordingUI())
    assert agent.supports_images()
    agent.extras["pending_images"] = [image]
    assert (await agent.turn("what is this?")).text == "a small grey image"
    assert seen == [1]
    plain, _, _ = conductor(services, tmp_path, '{"text": "x"}')
    assert not plain.supports_images()  # no image price: the UI warns instead


async def test_a_message_sent_mid_turn_is_read_after_the_step(services, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "notes.txt").write_text("alpha\n", encoding="utf-8")
    agent, adapter, _ui = conductor(
        services, tmp_path,
        calls(("read_file", {"path": "notes.txt"})), '{"text": "Done, with beta too."}',
    )
    taken: list[str] = []
    agent.on_steered = taken.append
    original = adapter.complete

    async def complete_then_user_types(request):  # type: ignore[no-untyped-def]
        reply = await original(request)
        if len(adapter.prompts) == 1:
            agent.steer("also mention beta")  # typed while the first step runs
        return reply

    adapter.complete = complete_then_user_types  # type: ignore[method-assign]
    result = await agent.turn("what is in notes.txt?")
    assert result.text == "Done, with beta too."
    assert taken == ["also mention beta"] and agent.steering == []
    roles = [m.role for m in agent.transcript.messages]
    assert roles == ["user", "assistant", "tool", "user", "assistant"]
    assert "also mention beta" in adapter.prompts[1]


# -- the second review: an error before any build started, and forge arc ---------------------


@pytest.mark.parametrize(
    ("error", "said"),
    [
        (ToolError("invalid_override", "budget_usd"),
         "No build was started: the build setting 'budget_usd' has a value a build cannot "
         "use. Set it again, then ask for the build."),
        (ToolError("not_a_file", "cases/mine.json"),
         "No build was started: cases/mine.json is not a file."),
        (ToolError("model_not_configured", "nvidia/none"),
         "No build was started: the model nvidia/none is not in this profile. Choose one "
         "that is with /model, then ask for the build."),
        (ToolError("invalid_arguments", "description"),
         "No build was started: the request did not say what the agent should do."),
        (ToolError("build_mode_unavailable"),
         "No build was started: build mode is not available in this session."),
    ],
    ids=["override", "file", "model", "arguments", "unavailable"],
)
async def test_an_error_before_any_build_says_what_it_was_and_not_that_a_build_ran(  # type: ignore[no-untyped-def]
    services, tmp_path: Path, error: ToolError, said: str
) -> None:
    """The second review: these errors are raised before anything is built, and the user
    read "The build ended without an agent ... You can ask for the build again", which
    repeats the error. The answer carries the error's own detail (which setting, which
    file, which model) in plain words, and does not say a build ran."""
    agent, adapter, ui = conductor(
        services, tmp_path, calls(("shell", {"command": "ls"})), '{"text": "never asked for"}',
    )
    agent.extras["make"] = failing_make(error)
    agent.mode = "build"
    result = await agent.turn(EMAILS)
    assert result.text == said
    assert error.code not in result.text and "build ended" not in result.text
    assert [e.name for e in ui.events if isinstance(e, ToolStarted)] == ["make"]
    assert error.code in str(agent.transcript.messages[2])  # the code stays in the result


async def test_an_error_during_a_build_carries_what_the_build_reported(  # type: ignore[no-untyped-def]
    services, tmp_path: Path
) -> None:
    agent, adapter, ui = conductor(
        services, tmp_path, calls(("shell", {"command": "ls"})), '{"text": "never asked for"}',
    )
    agent.extras["make"] = failing_make(ToolError("floor_too_small", "only 1 case fits"))
    agent.mode = "build"
    result = await agent.turn(EMAILS)
    assert result.text == (
        BUILD_ENDED + " It stopped on an error before it finished: only 1 case fits. "
        "You can ask for the build again."
    )


def failing_forge(error: Exception):  # type: ignore[no-untyped-def]
    async def forge(target: str, ctx: object) -> dict[str, object]:
        raise error
    return forge


async def test_an_error_from_forge_arc_is_not_described_as_a_build(  # type: ignore[no-untyped-def]
    services, tmp_path: Path
) -> None:
    """``forge`` on ARC builds no agent: its error is a tool failure the model can see and
    work around, as any other tool's is, and the turn does not end as a build that ended."""
    agent, adapter, ui = conductor(
        services, tmp_path,
        calls(("forge", {"target": "arc"})),
        calls(("list_dir", {})),
        '{"text": "The ARC harness could not run here."}',
    )
    agent.extras["forge"] = failing_forge(ToolError("arc_extra_missing"))
    result = await agent.turn("forge arc")
    assert [e.name for e in ui.events if isinstance(e, ToolStarted)] == ["forge", "list_dir"]
    assert result.text == "The ARC harness could not run here."
    assert "build" not in result.text.lower()
    # forge on a description is a build, and its error ends the turn as one.
    agent, adapter, ui = conductor(
        services, tmp_path,
        calls(("forge", {"target": "an agent that sorts emails"})),
        calls(("list_dir", {})),
    )
    agent.extras["forge"] = failing_forge(ToolError("profile_not_editable"))
    result = await agent.turn("forge an email sorter")
    assert [e.name for e in ui.events if isinstance(e, ToolStarted)] == ["forge"]
    assert result.text == BUILD_ENDED + " " + BUILD_ERROR
