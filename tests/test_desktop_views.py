"""What a desktop conversation showed, kept beside its transcript (architect/desktop/views.py).

The transcript holds only what the model reads, so a reopened conversation showed only
the turns the model answered: a slash command's turn, the cards, the build and Try it's
runs were gone after a restart. The bridge now keeps the messages the page folds and
gives them back with ``history``. Offline: the scripted conductor model and the fake
session profile.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from session_support import fake_profile
from test_agent_conductor import Scripted
from test_desktop_bridge import Wire, call, idle, started
from test_desktop_bridge_more import install, ok

from architect.agent.conductor import Conductor
from architect.agent.sessions import SessionStore
from architect.agent.transcript import Transcript
from architect.desktop.bridge import Bridge
from architect.desktop.views import ViewLog, copy_view, remove_view, view_path


def ev(kind: str, **fields: Any) -> dict[str, Any]:
    return {"type": "event", "event": {"kind": kind, **fields}}


def log(tmp_path: Path, session: str = "abc123def456") -> tuple[ViewLog, SessionStore, Any]:
    store = SessionStore(tmp_path / "sessions")
    conductor = SimpleNamespace(session_id=session, transcript=Transcript())
    return ViewLog(store, cast(Conductor, conductor)), store, conductor


def lines(store: SessionStore, session: str) -> list[dict[str, Any]]:
    text = view_path(store, session).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def test_the_record_keeps_what_the_page_folds_and_nothing_else(tmp_path: Path) -> None:
    view, store, conductor = log(tmp_path)
    view.record({"type": "turn_started", "text": "hi", "quiet": False, "at": 1.0})
    view.record(ev("thinking", active=True, label="architect"))
    for piece in ("Hel", "lo", "."):
        view.record(ev("delta", text=piece))
    view.record(ev("link", url="https://example.com/sign-in?state=x"))
    view.record(ev("output", text="a line a command printed"))
    view.record({"type": "state", "state": {"spent_usd": 0.01}})
    view.record({"type": "side", "id": 1, "event": {"kind": "answer", "text": "aside"}})
    view.record({"id": 4, "result": {}})
    view.record(ev("answer", text="Hello."))
    view.record({"type": "turn_finished", "text": "hi", "quiet": False, "at": 2.0})

    # Not saved yet: nothing on disk, but the record is there for this process.
    assert not view_path(store, "abc123def456").exists()
    kept = view.load("abc123def456")
    assert kept is not None
    assert [m["type"] for m in kept] == ["turn_started", "event", "event", "turn_finished"]
    assert kept[-1]["transcript"] == 0  # where the transcript stood when the turn ended
    # The streamed pieces as one; no thinking flag, link, output, status line or reply.
    assert [m["event"] for m in kept[1:3]] == [
        {"kind": "delta", "text": "Hello."}, {"kind": "answer", "text": "Hello."}]

    conductor.transcript.user("hi")
    conductor.transcript.assistant("Hello.")
    view.flush()
    assert not view_path(store, "abc123def456").exists()  # still not saved
    store.save("abc123def456", conductor.transcript, tmp_path)
    view.flush()
    assert lines(store, "abc123def456") == kept
    # Once saved, a message goes straight to the file.
    view.record(ev("notice", text="later", level="info"))
    later = lines(store, "abc123def456")[-1]
    assert later["event"]["text"] == "later"
    assert view.load("abc123def456") == [*kept, later]


def test_a_conversation_saved_before_the_record_began_keeps_its_transcript_first(
    tmp_path: Path,
) -> None:
    view, store, conductor = log(tmp_path)
    for text in ("one", "two", "three"):
        conductor.transcript.user(text)
    store.save("abc123def456", conductor.transcript, tmp_path)
    view.record({"type": "turn_started", "text": "four", "quiet": False})
    assert lines(store, "abc123def456")[0] == {"type": "view", "from": 0, "to": 3}
    # A line cut short when a process stopped is passed over.
    with view_path(store, "abc123def456").open("a", encoding="utf-8") as stream:
        stream.write('{"type": "event", "eve\n')
    view.record({"type": "turn_finished", "text": "four", "quiet": False})
    loaded = view.load("abc123def456")
    assert loaded is not None
    assert [m["type"] for m in loaded] == ["view", "turn_started", "turn_finished"]
    assert view.load("000000000000") is None


def test_a_copy_and_a_removal_take_the_record_along(tmp_path: Path) -> None:
    view, store, conductor = log(tmp_path)
    conductor.transcript.user("hi")
    store.save("abc123def456", conductor.transcript, tmp_path)
    view.record({"type": "turn_started", "text": "hi", "quiet": False})
    copy_view(store, "abc123def456", "fed654cba321")
    assert lines(store, "fed654cba321") == lines(store, "abc123def456")
    remove_view(store, "abc123def456")
    assert not view_path(store, "abc123def456").exists()
    remove_view(store, "abc123def456")  # nothing there: no error
    copy_view(store, "abc123def456", "000000000000")  # nothing to copy: no error
    assert not view_path(store, "000000000000").exists()


async def reopen(tmp_path: Path, session: str) -> tuple[Bridge, Wire]:
    """A new bridge process on the same workspace, resuming ``session`` (an app restart)."""
    wire = Wire()
    bridge = Bridge(wire.write, adapter=Scripted())
    await call(bridge, 1, "boot", cwd=str(tmp_path), workspace=str(tmp_path / "w"),
               resume=session)
    assert "resume_failed" not in ok(wire, 1)
    return bridge, wire


async def test_a_reopened_conversation_gets_back_its_commands_and_runs(tmp_path: Path) -> None:
    """The transcript keeps the message and its answer, and notes for /run and Try it;
    the record keeps both turns as they were shown, and Try it's quiet one."""
    bridge, wire = await started(tmp_path, '{"text": "Hello from Architect."}')
    try:
        await call(bridge, 2, "send", text="hi there")
        await idle(bridge)
        await install(bridge, "alpha")
        await call(bridge, 3, "send", text='/run alpha {"text": "hi"}')
        await idle(bridge)
        await call(bridge, 4, "run_agent", name="alpha", input={"text": "again"})
        ok(wire, 4)
        session = bridge.repl.conductor.session_id  # type: ignore[union-attr]
    finally:
        await bridge.close()

    again, wire = await reopen(tmp_path, session)
    try:
        await call(again, 2, "history")
        history = ok(wire, 2)
        roles = [m["role"] for m in history["messages"]]
        assert roles[:2] == ["user", "assistant"] and roles[2:] == ["note", "note"]
        shown = history["view"]
        assert [m for m in shown if m["type"] == "view"] == []  # it saw every turn
        assert [(m["text"], m["quiet"]) for m in shown if m["type"] == "turn_started"] == [
            ("hi there", False), ('/run alpha {"text": "hi"}', False), ("/run alpha", True)]
        kinds = {m["event"]["kind"] for m in shown if m["type"] == "event"}
        assert "answer" in kinds and "subagent" in kinds
        assert "thinking" not in kinds and all(m["type"] != "state" for m in shown)
        # What the new process said while booting is not part of the conversation.
        assert not any(m["type"] == "event" and m["event"].get("text", "").startswith("Resumed ")
                       for m in shown)
    finally:
        await again.close()


async def test_turns_taken_in_the_terminal_are_marked_where_they_came(tmp_path: Path) -> None:
    """The terminal keeps no record. A conversation continued there and reopened in the
    app has transcript messages its record never saw: the next process marks them, so the
    app shows them from the transcript where they came."""
    bridge, wire = await started(tmp_path, '{"text": "Hello."}')
    try:
        await call(bridge, 2, "send", text="hi there")
        await idle(bridge)
        session = bridge.repl.conductor.session_id  # type: ignore[union-attr]
    finally:
        await bridge.close()
    store = SessionStore(tmp_path / "w" / "sessions")
    transcript = store.load(session)
    assert len(transcript.messages) == 2
    transcript.user("asked in the terminal")
    transcript.assistant("Answered in the terminal.")
    store.save(session, transcript, tmp_path)

    wire = Wire()
    again = Bridge(wire.write, adapter=Scripted('{"text": "Back in the app."}'))
    try:
        await call(again, 1, "boot", cwd=str(tmp_path), workspace=str(tmp_path / "w"),
                   resume=session)
        await call(again, 2, "send", text="and again")
        await idle(again)
        await call(again, 3, "history")
        shown = ok(wire, 3)["view"]
        kinds = [(m["type"], m.get("text") or m.get("to")) for m in shown
                 if m["type"] in ("turn_started", "turn_finished", "view")]
        assert kinds == [("turn_started", "hi there"), ("turn_finished", "hi there"),
                         ("view", 4), ("turn_started", "and again"),
                         ("turn_finished", "and again")]
        assert [m for m in shown if m["type"] == "view"] == [{"type": "view", "from": 2, "to": 4}]
        finished = [m["transcript"] for m in shown if m["type"] == "turn_finished"]
        assert finished == [2, 6]
    finally:
        await again.close()


async def test_a_conversation_a_command_began_is_saved_and_named_after_it(
    tmp_path: Path,
) -> None:
    """``/make @spec.json`` as the first input only adds a note: the conversation was
    saved when the app closed, as "(empty)", and the app never learned its id, so a
    restart opened an empty one. Now it is saved when the command ends, named after it."""
    bridge, wire = await started(tmp_path)
    try:
        await install(bridge, "alpha")
        await call(bridge, 2, "send", text='/run alpha {"text": "hi"}')
        await idle(bridge)
        session = bridge.repl.conductor.session_id  # type: ignore[union-attr]
        await call(bridge, 3, "sessions")
        rows = ok(wire, 3)["sessions"]
        assert [(r["id"], r["title"]) for r in rows] == [(session, '/run alpha {"text": "hi"}')]
        states = [line["state"] for line in wire.lines if line.get("type") == "state"]
        assert states[-1]["title"] == '/run alpha {"text": "hi"}'
        store = bridge.repl.app.store  # type: ignore[union-attr]
        written = lines(store, session)
        assert [m for m in written if m["type"] == "view"] == []
        assert [m["text"] for m in written if m["type"] == "turn_started"] == [
            '/run alpha {"text": "hi"}']
        # A command that adds nothing to the conversation saves nothing.
        await call(bridge, 4, "send", text="/status")
        await idle(bridge)
        await call(bridge, 5, "sessions")
        assert len(ok(wire, 5)["sessions"]) == 1
    finally:
        await bridge.close()


async def test_a_later_command_keeps_the_title_the_first_message_gave(tmp_path: Path) -> None:
    bridge, wire = await started(tmp_path, '{"text": "Hello."}')
    try:
        await call(bridge, 2, "send", text="build me a reader")
        await idle(bridge)
        await install(bridge, "alpha")
        await call(bridge, 3, "send", text='/run alpha {"text": "hi"}')
        await idle(bridge)
        await call(bridge, 4, "sessions")
        assert [r["title"] for r in ok(wire, 4)["sessions"]] == ["build me a reader"]
    finally:
        await bridge.close()


async def test_fork_and_delete_take_the_record_along(tmp_path: Path) -> None:
    bridge, wire = await started(tmp_path, '{"text": "First."}', '{"text": "Second."}')
    try:
        await call(bridge, 2, "send", text="first question")
        await idle(bridge)
        await call(bridge, 3, "send", text="second question")
        await idle(bridge)
        original = bridge.repl.conductor.session_id  # type: ignore[union-attr]
        store = bridge.repl.app.store  # type: ignore[union-attr]
        await call(bridge, 4, "fork_session", id=original)
        whole = ok(wire, 4)["id"]
        assert lines(store, whole) == lines(store, original)
        await call(bridge, 5, "fork_session", id=original, upto=1)
        early = ok(wire, 5)["id"]
        assert not view_path(store, early).exists()  # it shows its transcript
        await call(bridge, 6, "delete_session", id=whole)
        ok(wire, 6)
        assert not view_path(store, whole).exists()
    finally:
        await bridge.close()


async def test_boot_alone_records_nothing(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    wire = Wire()
    bridge = Bridge(wire.write, adapter=Scripted())
    try:
        await call(bridge, 1, "boot", cwd=str(tmp_path), workspace=str(tmp_path / "w"))
        await call(bridge, 2, "history")
        assert ok(wire, 2)["view"] is None
        assert not (tmp_path / "w" / "sessions" / "views").exists()
    finally:
        await bridge.close()
