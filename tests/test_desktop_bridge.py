"""The desktop bridge: JSON lines in, the CLI's own Repl behind them.

Offline: the conductor's model is the scripted adapter and the profile the fake one the
session tests use. Each test drives ``Bridge.handle`` with raw protocol lines and reads the
lines it wrote back.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from session_support import fake_profile, make_spec
from test_agent_conductor import Scripted, calls

from architect.agent.events import Notice, PlanCard, Progress, ToolStarted
from architect.desktop.bridge import Bridge, DesktopFrontend, encode_event
from architect.session.state import MeterEvent


class Wire:
    def __init__(self) -> None:
        self.lines: list[dict[str, Any]] = []

    def write(self, text: str) -> None:
        for line in text.splitlines():
            self.lines.append(json.loads(line))

    def result(self, request_id: int) -> dict[str, Any]:
        for line in self.lines:
            if line.get("id") == request_id:
                return line
        raise AssertionError(f"no reply to {request_id}")

    def events(self, kind: str | None = None) -> list[dict[str, Any]]:
        found = [line["event"] for line in self.lines if line.get("type") == "event"]
        return [e for e in found if kind is None or e["kind"] == kind]

    def asks(self) -> list[dict[str, Any]]:
        return [line["ask"] for line in self.lines if line.get("type") == "ask"]


async def started(tmp_path: Path, *replies: str) -> tuple[Bridge, Wire]:
    fake_profile(tmp_path)
    wire = Wire()
    bridge = Bridge(wire.write, adapter=Scripted(*replies))
    await call(bridge, 1, "boot", cwd=str(tmp_path), workspace=str(tmp_path / "w"))
    assert "result" in wire.result(1), wire.result(1)
    return bridge, wire


async def call(bridge: Bridge, request_id: int, method: str, **params: Any) -> None:
    await bridge.handle(json.dumps({"id": request_id, "method": method, "params": params}))


def resolved(path: Path) -> str:
    return str(path.resolve())


async def asked(frontend: DesktopFrontend) -> None:
    """Wait until ``frontend`` has a pending question."""
    for _ in range(2000):
        if frontend.pending():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("nothing was asked")


async def idle(bridge: Bridge) -> None:
    for _ in range(400):
        await asyncio.sleep(0.01)
        if not bridge.busy and bridge.inbox.empty():
            return
    raise AssertionError("the bridge never went idle")


async def test_boot_reports_the_session_state(tmp_path: Path) -> None:
    bridge, wire = await started(tmp_path)
    try:
        state = wire.result(1)["result"]
        assert state["started"] is True
        assert state["state"] == "normal"
        assert state["permission"] == "ask"
        assert state["cwd"] == resolved(tmp_path)
        assert any(line.get("type") == "state" for line in wire.lines)
    finally:
        await bridge.close()


async def test_a_message_streams_the_answer_and_is_saved(tmp_path: Path) -> None:
    bridge, wire = await started(tmp_path, '{"text": "Hello from Architect."}')
    try:
        await call(bridge, 2, "send", text="hi there")
        await idle(bridge)
        answers = wire.events("answer")
        assert answers and answers[-1]["text"] == "Hello from Architect."
        turns = [line["type"] for line in wire.lines
                 if line.get("type", "").startswith("turn_")]
        assert turns == ["turn_started", "turn_finished"]
        await call(bridge, 3, "sessions")
        sessions = wire.result(3)["result"]["sessions"]
        assert sessions and sessions[0]["title"] == "hi there"
        await call(bridge, 4, "history")
        roles = [m["role"] for m in wire.result(4)["result"]["messages"]]
        assert roles[0] == "user" and "assistant" in roles
    finally:
        await bridge.close()


async def test_slash_commands_run_through_the_cli_dispatcher(tmp_path: Path) -> None:
    bridge, wire = await started(tmp_path)
    try:
        await call(bridge, 2, "send", text="/status")
        await idle(bridge)
        assert wire.events("rows") or wire.events("notice")
        await call(bridge, 3, "commands")
        names = {c["name"] for c in wire.result(3)["result"]["commands"]}
        assert {"/help", "/model", "/forge", "/agents", "/undo", "/diff"} <= names
    finally:
        await bridge.close()


async def test_modes_and_permissions_follow_the_terminal_cycle(tmp_path: Path) -> None:
    bridge, wire = await started(tmp_path)
    try:
        # Build mode's key stays "forge" (the app labels it Build); a saved conversation
        # that opens with it still lands in build mode.
        await call(bridge, 2, "set_state", state="forge")
        assert wire.result(2)["result"]["mode"] == "build"
        assert wire.result(2)["result"]["state"] == "forge"
        await call(bridge, 3, "set_state", state="plan")
        assert wire.result(3)["result"]["permission"] == "plan"
        await call(bridge, 4, "set_state", state="normal")
        assert wire.result(4)["result"]["permission"] == "ask"
        await call(bridge, 5, "set_permission", permission="full-auto")
        assert wire.result(5)["result"]["permission"] == "full-auto"
        await call(bridge, 6, "set_permission", permission="yolo")
        assert wire.result(6)["error"]["code"] == "invalid_permission"
    finally:
        await bridge.close()


async def test_build_mode_answers_to_build_and_forge(tmp_path: Path) -> None:
    bridge, wire = await started(tmp_path)
    try:
        await call(bridge, 2, "set_state", state="build")
        assert wire.result(2)["result"]["mode"] == "build"
        assert wire.result(2)["result"]["state"] == "forge"
        await call(bridge, 3, "set_state", state="forge")  # already in build mode
        assert wire.result(3)["result"]["mode"] == "build"
        assert wire.result(3)["result"]["permission"] == "ask"
        await call(bridge, 4, "set_state", state="chat")
        assert wire.result(4)["error"]["code"] == "invalid_state"
        await call(bridge, 5, "set_state", state=["forge"])
        assert wire.result(5)["error"]["code"] == "invalid_state"
        # The conversation itself names it build mode.
        await call(bridge, 6, "send", text="/mode")
        await idle(bridge)
        assert "Mode: build." in [e["text"] for e in wire.events("notice")]
    finally:
        await bridge.close()


async def test_approvals_are_asked_and_unclear_answers_are_no(tmp_path: Path) -> None:
    wire = Wire()
    frontend = DesktopFrontend(wire.lines.append)

    async def answer_later(value: str | None) -> None:
        await asked(frontend)
        frontend.answer(frontend.pending()[0], value)

    for value, expected in (("yes", "yes"), ("s", "session"), ("maybe", "no"), (None, "no")):
        helper = asyncio.create_task(answer_later(value))
        assert await frontend.approve("edit", "src/app.py") == expected
        await helper
    asks = [line["ask"] for line in wire.lines if line.get("type") == "ask"]
    assert asks[0]["kind"] == "approval" and asks[0]["detail"] == "src/app.py"
    assert any(line.get("type") == "ask_closed" for line in wire.lines)


async def test_an_unanswered_approval_times_out_as_no() -> None:
    sent: list[dict[str, Any]] = []
    frontend = DesktopFrontend(sent.append)
    frontend.approve_timeout_s = 0.05
    assert await frontend.approve("shell", "rm -rf build") == "no"
    assert any(m.get("event", {}).get("kind") == "notice" for m in sent)


async def test_questions_take_the_default_on_empty_and_none_cancels() -> None:
    sent: list[dict[str, Any]] = []
    frontend = DesktopFrontend(sent.append)

    async def reply(value: str | None) -> None:
        await asked(frontend)
        frontend.answer(frontend.pending()[0], value)

    task = asyncio.create_task(reply(""))
    assert await frontend.ask("Model?", default="m1", choices=["m1", "m2"]) == "m1"
    await task
    task = asyncio.create_task(reply(None))
    assert await frontend.ask("Model?") is None
    await task
    assert sent[0]["ask"]["choices"] == ["m1", "m2"]


async def test_a_closed_ask_says_what_was_answered_but_never_a_secret() -> None:
    """A page that re-attaches replays ``ask_closed``: it keeps the answer on the closed
    card, so it must carry it; a secret's value is never sent back."""
    sent: list[dict[str, Any]] = []
    frontend = DesktopFrontend(sent.append)

    async def reply(value: str | None) -> None:
        await asked(frontend)
        frontend.answer(frontend.pending()[0], value)

    task = asyncio.create_task(reply("yes"))
    assert await frontend.ask("Compile this agent?", default="yes") == "yes"
    await task
    task = asyncio.create_task(reply(None))
    assert await frontend.ask("Which fields?") is None
    await task
    task = asyncio.create_task(reply("sk-very-secret"))
    assert await frontend.ask_secret("Paste your key") == "sk-very-secret"
    await task
    closed = [m for m in sent if m.get("type") == "ask_closed"]
    assert [m.get("answer", "absent") for m in closed] == ["yes", None, "absent"]
    assert "sk-very-secret" not in json.dumps(sent) and len(closed) == 3
    frontend.approve_timeout_s = 0.05
    assert await frontend.approve("shell", "ls") == "no"  # no answer came: none is claimed
    assert "answer" not in [m for m in sent if m.get("type") == "ask_closed"][-1]


async def test_events_and_settled_questions_say_when() -> None:
    """A page that re-attaches, or a reopened conversation, folds each message at its own
    time (``at``), so a build's fold line counts from Build it, as it did live, and not
    from the /make message before the plan card."""
    sent: list[dict[str, Any]] = []
    frontend = DesktopFrontend(sent.append)

    async def reply(value: str | None) -> None:
        await asked(frontend)
        frontend.answer(frontend.pending()[0], value)

    before = time.time()
    frontend.renderer.text("Plan: receipt-reader")
    task = asyncio.create_task(reply("yes"))
    assert await frontend.ask("Compile this agent?", default="yes") == "yes"
    await task
    timed = [m for m in sent if m.get("type") in ("event", "ask_closed")]
    assert [m["type"] for m in timed] == ["event", "ask_closed"]
    assert all(before - 0.001 <= m["at"] <= time.time() + 0.001 for m in timed)


async def test_cancel_stops_a_running_turn(tmp_path: Path) -> None:
    bridge, wire = await started(tmp_path)
    assert bridge.repl is not None
    adapter = bridge.repl.conductor.adapter

    async def slow(request):  # type: ignore[no-untyped-def]
        await asyncio.sleep(30)

    inner = getattr(adapter, "_inner", adapter)
    inner.complete = slow  # type: ignore[method-assign]
    try:
        await call(bridge, 2, "send", text="take your time")
        for _ in range(200):
            await asyncio.sleep(0.01)
            if bridge.frontend.current is not None:
                break
        await call(bridge, 3, "cancel")
        assert wire.result(3)["result"]["stopped"] is True
        await idle(bridge)
        assert any("Interrupted" in e.get("text", "") for e in wire.events("notice"))
    finally:
        await bridge.close()


async def test_messages_sent_while_busy_are_queued_in_order(tmp_path: Path) -> None:
    bridge, wire = await started(tmp_path, '{"text": "one"}', '{"text": "two"}')
    try:
        await call(bridge, 2, "send", text="first")
        await call(bridge, 3, "send", text="second")
        await idle(bridge)
        assert [e["text"] for e in wire.events("answer")] == ["one", "two"]
    finally:
        await bridge.close()


async def test_tool_events_reach_the_app(tmp_path: Path) -> None:
    bridge, wire = await started(
        tmp_path,
        calls(("list_dir", {"path": "."})),
        '{"text": "done"}',
    )
    try:
        await call(bridge, 2, "send", text="look around")
        await idle(bridge)
        started_tools = wire.events("tool_started")
        finished = wire.events("tool_finished")
        assert started_tools and started_tools[0]["name"] == "list_dir"
        assert finished and finished[0]["call_id"] == started_tools[0]["call_id"]
    finally:
        await bridge.close()


async def test_a_build_progress_line_sends_the_spend_so_far(tmp_path: Path) -> None:
    """A build's stage lines come while the session meter counts its calls: the state goes
    out with each, so the app's spend follows the build instead of jumping at its end."""
    bridge, wire = await started(tmp_path)
    try:
        assert bridge.app is not None
        bridge.app.meter.record(MeterEvent("complete", "fake", 900, 40, 0.12, 0.8))
        before = len(wire.lines)
        bridge.frontend.emit(Progress("stage 2 tune: 1 calls, $0.12 so far"))
        sent = wire.lines[before:]
        assert [line.get("type") for line in sent] == ["event", "state"]
        assert sent[0]["event"]["kind"] == "progress"
        assert sent[1]["state"]["spent_usd"] == pytest.approx(0.12)
        # Other events leave the state to the turn's end.
        before = len(wire.lines)
        bridge.frontend.emit(Notice("a note"))
        assert [line.get("type") for line in wire.lines[before:]] == ["event"]
    finally:
        await bridge.close()


async def test_requests_before_boot_and_unknown_methods_fail_with_codes() -> None:
    wire = Wire()
    bridge = Bridge(wire.write)
    await call(bridge, 1, "send", text="hi")
    assert wire.result(1)["error"]["code"] == "not_started"
    await call(bridge, 2, "fly")
    assert wire.result(2)["error"]["code"] == "unknown_method"
    await bridge.handle("not json")
    assert wire.lines[-1]["error"]["code"] == "invalid_json"


def test_events_encode_with_a_kind() -> None:
    assert encode_event(Notice("x", "warn")) == {"text": "x", "level": "warn", "kind": "notice"}
    card = encode_event(PlanCard("Plan", (("floor", "0.9"),), "ok"))
    assert card["rows"] == [["floor", "0.9"]] and card["kind"] == "plan_card"
    assert encode_event(ToolStarted("c1", "read_file", "a.py"))["kind"] == "tool_started"


def test_the_process_keeps_stdout_for_the_protocol(tmp_path: Path) -> None:
    lines = '{"id":1,"method":"ping"}\n{"id":2,"method":"shutdown"}\n'
    done = subprocess.run(
        [sys.executable, "-c", "import os; from architect.desktop import bridge; "
         "real = bridge._take_stdio; "
         "bridge._take_stdio = lambda: (r := real(), print('stray'), "
         "os.system('echo child'), r)[-1]; raise SystemExit(bridge.main())"],
        input=lines, capture_output=True, text=True, timeout=60, cwd=tmp_path,
    )
    out = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
    assert out[0]["type"] == "ready"
    assert "stray" in done.stderr and "child" in done.stderr
    parsed = [m for m in out if m.get("id") == 1]
    assert parsed and parsed[0]["result"]["version"]


async def test_models_name_the_ladder_a_build_tries(tmp_path: Path) -> None:
    """B5: the profile's ``compile.model_ladder``, in order, beside the picker's rows."""
    bridge, wire = await started(tmp_path)
    try:
        assert bridge.app is not None
        await call(bridge, 2, "models")
        models = wire.result(2)["result"]
        assert models["ladder"] == list(bridge.app.services.profile.compile.model_ladder)
        assert models["ladder"] == ["fake"]
    finally:
        await bridge.close()


async def test_an_agent_page_has_a_practice_sample_only_when_its_floor_reads(
    tmp_path: Path,
) -> None:
    """B4: ``sample`` is the input of the first practice case of the agent's floor (sorted
    ids, even positions practice), never an exam case; absent when the floor file does not
    read."""
    bridge, wire = await started(tmp_path)
    cases = tmp_path / "w" / "cases.json"
    rows = json.loads(cases.read_text(encoding="utf-8"))
    for row in rows:
        row["task"] = {"text": f"case {row['case_id']}"}
    cases.write_text(json.dumps(rows), encoding="utf-8")
    try:
        assert bridge.app is not None
        services = bridge.app.services
        compiled = await services.compiler.compile(make_spec(lifecycle="install"), approved=True)
        assert compiled.artifact is not None and compiled.artifact.licensed
        services.registry.install("alpha", compiled.artifact)

        await call(bridge, 2, "agent", name="alpha")
        page = wire.result(2)["result"]
        assert page["sample"] == {"text": "case 0"}
        sent = json.dumps(page)
        assert not any(f"case {held}" in sent for held in ("1", "3", "5"))  # the exam's

        cases.write_text("not json", encoding="utf-8")
        await call(bridge, 3, "agent", name="alpha")
        assert "sample" not in wire.result(3)["result"]
    finally:
        await bridge.close()
