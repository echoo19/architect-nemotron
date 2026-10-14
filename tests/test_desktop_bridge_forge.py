"""A whole agent build through the desktop bridge (offline, $0).

The build runs in its own process under ``asyncio.run`` (``desktop_build_driver.py``),
as the real bridge does: the first description in build mode goes to ``make``, the
interview and Stage 0 answer from the fake profile, and every question gets its default,
as the app's Enter does. It must reach the plan card, the stage observers, a licensed
install, and the agent screens' reads and runs, and send the app nothing from the exam's
cases. What the conversation showed is kept for a restart, with nothing from the exam
either. Not now on the plan instead must end the turn in one sentence.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from desktop_build_driver import DESCRIPTION, NAME
from session_support import fake_profile, response
from test_agent_build import INTERVIEW, spec_json
from test_agent_conductor import Scripted, calls
from test_desktop_bridge import Wire, call

from architect.agent.build import FROZEN_NOTE
from architect.agent.conductor import SET_ASIDE
from architect.desktop.bridge import Bridge

TESTS = Path(__file__).parent


def drive(root: Path, *mode: str) -> dict[str, Any]:
    """What ``desktop_build_driver.py`` saw, run in its own process."""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(TESTS), str(TESTS.parent)])}
    done = subprocess.run(
        [sys.executable, str(TESTS / "desktop_build_driver.py"), str(root), *mode],
        capture_output=True, text=True, timeout=240, env=env, cwd=TESTS.parent,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    found: dict[str, Any] = json.loads(done.stdout.strip().splitlines()[-1])
    return found


@pytest.fixture(scope="module")
def seen(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """One build, read by every test here (a build takes a while)."""
    return drive(tmp_path_factory.mktemp("build"))


def test_not_now_on_the_drafted_plan_ends_the_turn_in_one_sentence(tmp_path: Path) -> None:
    """A filmed take: Not now answered the plan "no", and the conversation then asked its
    model again, which called make once more (refused, a failed build in the chat) or
    asked questions of its own. The turn now ends there, with one sentence."""
    saw = drive(tmp_path, "notnow")
    assert saw["finished"] is True
    assert saw["questions"][0].startswith("Where will this agent live?"), saw["questions"]
    assert saw["questions"][-1] == "Compile this agent?"
    assert len(saw["titles"]) == 1 and saw["titles"][0].startswith("Plan: ")  # no result
    assert saw["started"] == ["make"] and saw["finished_tools"] == [["make", True]]
    assert saw["answers"] == [SET_ASIDE]
    assert saw["model_calls"] == 0
    assert saw["history"] == ["user", "assistant", "tool", "assistant"]
    # Kept beside the transcript, so the plan and its "no" are there after a restart.
    view = saw["view"]
    assert view["marks"] == [] and view["turns"] == [[DESCRIPTION, False]]
    assert len(view["cards"]) == 1 and view["cards"][0].startswith("Plan: ")
    assert view["closed"][-1] == "no" and view["answers"] == [SET_ASIDE]


def test_a_build_reaches_a_licensed_agent_the_screens_can_read_and_run(
    seen: dict[str, Any],
) -> None:
    assert seen["finished"] is True
    questions = seen["questions"]
    assert questions[0].startswith("Where will this agent live?"), questions
    assert "Compile this agent?" in questions
    assert any(t.startswith("Plan: ") for t in seen["titles"]), seen["titles"]
    assert any(t.startswith("Result: ") and "not" not in t for t in seen["titles"]), seen["titles"]
    assert 4 in seen["stages"], seen["stages"]
    assert any(p.startswith("stage 2 ") for p in seen["progress"]), seen["progress"]

    agents = seen["agents"]["agents"]
    assert [a["name"] for a in agents] == ["extracts-invoice-fields"]
    assert agents[0]["ok"] is True and agents[0]["licensed"] is True
    detail = seen["agent"]
    assert detail["licensed"] is True and detail["spec"]["input_schema"]
    assert detail["history"][0]["current"] is True
    assert "verification_id" not in detail["report"] and "signature" not in detail["report"]
    run = seen["run"].get("result", {})
    assert run.get("agent") == "extracts-invoice-fields", seen["run"]
    assert run["calls"] >= 1 and run["seconds"] >= 0 and run["run_id"]
    assert run["completion_id"] is None  # the fake provider gives none


def test_the_spend_goes_out_with_each_progress_line_of_the_build(seen: dict[str, Any]) -> None:
    """The state follows every progress line of the build (each stage, and every 30 s), so
    the app's spend meter moves with the build instead of jumping when the turn ends."""
    assert seen["turn_progress"] >= 4, seen["progress"]
    spent = seen["turn_spent"]
    assert len(spent) >= seen["turn_progress"], spent
    assert spent == sorted(spent)  # a meter: it never goes back


def test_the_app_gets_the_plan_and_the_exam_as_values_and_nothing_held_out(
    seen: dict[str, Any],
) -> None:
    """The bar view and exam chart's data: the plan card's hashes, bar and ladder, then,
    after the compile, each rung's signed counts. No exam case input, case metadata or
    verification id is ever sent to the app (observers, events or replies)."""
    plan = seen["plan_data"]
    assert len(plan["spec_hash"]) == 64 and plan["bar"] == 1.0
    assert plan["ladder"] == ["fake"] and (plan["practice"], plan["exam"]) == (3, 3)
    assert "pack_hash" not in plan  # Forge was off for this build
    result = seen["result_data"]
    assert result["licensed"] is True and result["spec_hash"] == plan["spec_hash"]
    assert result["exam"] == {"cases": 3, "correct": 3, "invalid": 0, "over_budget": 0}
    [rung] = result["rungs"]
    assert rung["model"] == "fake" and rung["ships"] is True and rung["score"] == 1.0
    assert rung["counts"] == result["exam"]

    assert seen["issued"], "Stage 4 issued no report"
    assert seen["sent_held_out"] is False
    assert seen["sent_secret"] is False
    assert seen["sent_ids"] == [] and seen["sent_verification_key"] is False


def test_the_conversation_keeps_what_it_showed_and_nothing_held_out(
    seen: dict[str, Any],
) -> None:
    """What a restarted app shows of the build: the request's turn with its plan and
    result cards, the live feed for the bar view, and Try it's quiet run. On disk, as on
    the wire, nothing from the exam's cases."""
    view = seen["view"]
    assert view["marks"] == []
    assert view["turns"] == [[DESCRIPTION, False], [f"/run {NAME}", True]]
    assert any(c.startswith("Plan: ") for c in view["cards"])
    assert any(c.startswith("Result: ") and "not" not in c for c in view["cards"])
    assert view["forge"] >= 1 and "state" not in view["kinds"]
    assert seen["saved_held_out"] is False
    assert seen["saved_secret"] is False
    assert seen["saved_ids"] == []


def test_every_forge_message_says_when_the_bridge_saw_it() -> None:
    """B3: each live compile and Forge line carries ``at`` (Unix seconds) beside its data,
    as events do, so a model's time on the ladder survives a replay. The data is as sent."""
    wire = Wire()
    bridge = Bridge(wire.write)
    before = time.time()
    bridge._on_stage({"stage": 2, "model": "fake", "model_index": 1, "model_count": 1})
    bridge._on_forge_result({"tool": "forge_measure", "depth": 0, "result": {"ok": True}})
    after = time.time()
    forge = [line for line in wire.lines if line.get("type") == "forge"]
    assert [line["data"]["kind"] for line in forge] == ["stage", "tool"]
    assert forge[0]["data"] == {"kind": "stage", "stage": 2, "model": "fake",
                                "model_index": 1, "model_count": 1}
    assert all(before - 0.001 <= line["at"] <= after + 0.001 for line in forge)
    assert all("at" not in line["data"] for line in forge)


def test_the_build_sends_the_plan_facts_and_a_practice_sample_never_an_exam_case(
    seen: dict[str, Any],
) -> None:
    """B2 on a real build: the task, its fields in schema order, the floor's origin and the
    ladder's list prices on the plan card. B4: the agent page's ``sample`` is the first
    practice case's input (sorted ids, even positions), never an exam case."""
    plan = seen["plan_data"]
    assert plan["task"] and isinstance(plan["checks"], list)
    assert plan["inputs"] == [{"name": "text", "type": "string", "nullable": False}]
    assert plan["outputs"] == []  # a bare string answer has no fields
    assert plan["origin"] == "user"
    assert plan["prices"] == {"fake": [1.0, 1.0]}
    assert seen["agent"]["sample"] == {"text": "practice 0"}
    assert seen["sent_held_out"] is False


# -- the Plan screen's "Build with changes" (SPEC step 2, the G-plan gate) ------------------


async def _turn(bridge: Bridge, wire: Wire, text: str, plan: str) -> list[dict[str, Any]]:
    """Send ``text`` and answer its asks (defaults; the plan's with ``plan``) until the turn
    ends; the plan cards it showed."""
    done = sum(1 for line in wire.lines if line.get("type") == "turn_finished")
    shown = len(wire.events("plan_card"))
    answered: set[object] = {ask["id"] for ask in wire.asks()}
    await call(bridge, 300 + done, "send", text=text)
    start = time.monotonic()
    while time.monotonic() - start < 60:
        for ask in wire.asks():
            if ask["id"] not in answered:
                answered.add(ask["id"])
                value = plan if ask.get("question") == "Compile this agent?" else ""
                bridge.frontend.answer(int(ask["id"]), value)
        if sum(1 for line in wire.lines if line.get("type") == "turn_finished") > done:
            break
        await asyncio.sleep(0.02)
    else:
        raise AssertionError("the turn never ended")
    return wire.events("plan_card")[shown:]


async def test_a_declined_plan_then_make_applies_the_saved_overrides(tmp_path: Path) -> None:
    """Edit plan saves overrides while the plan waits; "Build with changes" answers it no and
    sends ``/make <request>``. The new plan must carry the saved pass mark and budget."""
    profile = fake_profile(tmp_path)
    (profile.parent / "inference.json").write_text(
        json.dumps([response(INTERVIEW), response(spec_json())] * 2), encoding="utf-8")
    wire = Wire()
    bridge = Bridge(wire.write, adapter=Scripted(
        calls(("make", {"description": DESCRIPTION})), '{"text": "Built it."}'))
    try:
        await call(bridge, 1, "boot", cwd=str(tmp_path), workspace=str(tmp_path / "w"))
        await call(bridge, 2, "set_override", key="engineer", value="preset")
        await call(bridge, 3, "set_state", state="forge")
        [first] = await _turn(bridge, wire, DESCRIPTION, plan="no")
        assert first["data"]["bar"] == 1.0
        # Request ids from 200: an ask's ``ask_closed`` line carries an ``id`` too.
        await call(bridge, 204, "set_override", key="floor_limit", value=0.5)
        await call(bridge, 205, "set_override", key="compile_usd", value=3)
        assert "result" in wire.result(204) and "result" in wire.result(205)
        [second] = await _turn(bridge, wire, f"/make {DESCRIPTION}", plan="no")
        assert second["title"].startswith("Plan: ")
        assert second["data"]["bar"] == 0.5
        assert second["data"]["build_usd"] == 3
    finally:
        await bridge.close()


async def test_a_frozen_plan_ignores_pass_mark_budget_and_model_changes(tmp_path: Path) -> None:
    """A plan from a spec file (``/make @file``, ``data.frozen``) compiles the file as
    written: /floor, /budget and /model changes are not applied, and the plan says so."""
    profile = fake_profile(tmp_path)
    (tmp_path / "spec.json").write_text(spec_json(), encoding="utf-8")
    wire = Wire()
    bridge = Bridge(wire.write, adapter=Scripted('{"text": "ok"}'))
    try:
        await call(bridge, 1, "boot", cwd=str(tmp_path), workspace=str(profile.parent))
        await call(bridge, 2, "set_override", key="engineer", value="preset")
        [first] = await _turn(bridge, wire, "/make @spec.json", plan="no")
        assert first["data"]["frozen"] == "spec.json"
        await call(bridge, 203, "set_override", key="floor_limit", value=0.5)
        assert "result" in wire.result(203)
        [second] = await _turn(bridge, wire, "/make @spec.json", plan="no")
        assert second["data"]["bar"] == first["data"]["bar"] != 0.5
        assert FROZEN_NOTE in json.dumps(second["rows"])
    finally:
        await bridge.close()
