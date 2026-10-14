"""Drive one agent build through the desktop bridge and print what the app would see.

Run as a script (``python tests/desktop_build_driver.py <dir>``) under ``asyncio.run``,
the way the real bridge process runs; ``tests/test_desktop_bridge_forge.py`` checks its
output. Offline and $0: scripted conversation model, fake profile. The floor's exam cases
carry a fingerprint in their input and every case a private metadata value, so the test
can show that neither ever reaches the app.

``python tests/desktop_build_driver.py <dir> notnow`` stops at the plan instead: it
answers "no", as the app's Not now does, and reports that turn.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from forge_support import HELD_OUT_MARK, SECRET
from session_support import fake_profile, response
from test_agent_build import INTERVIEW, spec_json
from test_agent_conductor import Scripted, calls
from test_desktop_bridge import Wire, call

from architect.desktop.bridge import Bridge

DESCRIPTION = "make an agent that extracts invoice fields from text"
NAME = "extracts-invoice-fields"


def fingerprint_exam_cases(profile: Path) -> None:
    """Exam cases (odd positions of the sorted ids) read ``<HELD_OUT_MARK><id>``."""
    path = profile.parent / "cases.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    for position, row in enumerate(sorted(rows, key=lambda item: item["case_id"])):
        lead = HELD_OUT_MARK if position % 2 else "practice "
        row["task"] = {"text": f"{lead}{row['case_id']}"}
    path.write_text(json.dumps(rows), encoding="utf-8")


def verification_ids(root: Path) -> list[str]:
    """Every verification id Stage 4 issued under ``root`` (each copy of report.json)."""
    return sorted({
        json.loads(path.read_text(encoding="utf-8"))["verification_id"]
        for path in root.rglob("report.json")
    })


def cards(wire: Wire, prefix: str) -> list[dict[str, object]]:
    return [event for event in wire.events("plan_card") if str(event["title"]).startswith(prefix)]


def shown(view: list[dict[str, Any]] | None) -> dict[str, object]:
    """The record ``history`` gives back: its marks, turns, cards and answers."""
    messages: list[dict[str, Any]] = list(view or [])
    events = [m["event"] for m in messages if m.get("type") == "event"]
    return {
        "marks": [m for m in messages if m.get("type") == "view"],
        "turns": [[m["text"], m["quiet"]] for m in messages if m.get("type") == "turn_started"],
        "cards": [e["title"] for e in events if e.get("kind") == "plan_card"],
        "answers": [e["text"] for e in events if e.get("kind") == "answer"],
        "closed": [m.get("answer") for m in messages if m.get("type") == "ask_closed"],
        "kinds": sorted({str(m.get("type")) for m in messages}),
        "forge": sum(1 for m in messages if m.get("type") == "forge"),
    }


async def answer_the_turn(bridge: Bridge, wire: Wire, plan: str = "") -> list[dict[str, object]]:
    """Answer each question with its default, as Enter does, and the plan's with ``plan``
    ("no" is the app's Not now), until the turn ends; the questions asked."""
    asked: list[dict[str, object]] = []
    start = time.monotonic()
    while time.monotonic() - start < 90:
        for ask in wire.asks():
            if all(a["id"] != ask["id"] for a in asked):
                asked.append(ask)
                value = plan if ask.get("question") == "Compile this agent?" else ""
                bridge.frontend.answer(int(ask["id"]), value)
        if any(line.get("type") == "turn_finished" for line in wire.lines):
            break
        await asyncio.sleep(0.02)
    return asked


async def not_now(root: Path) -> dict[str, object]:
    """Describe the agent in build mode, then Not now on the drafted plan."""
    profile = fake_profile(root)
    (profile.parent / "inference.json").write_text(
        json.dumps([response(INTERVIEW), response(spec_json())]), encoding="utf-8")
    wire = Wire()
    # Scripted as a filmed take went: told the plan was declined, the model called make
    # again. The conversation's model must not be asked at all.
    adapter = Scripted(calls(("make", {"description": DESCRIPTION})), '{"text": "Built it."}')
    bridge = Bridge(wire.write, adapter=adapter)
    await call(bridge, 1, "boot", cwd=str(root), workspace=str(root / "w"))
    await call(bridge, 2, "set_override", key="engineer", value="preset")
    await call(bridge, 3, "set_state", state="forge")
    await call(bridge, 4, "send", text=DESCRIPTION)
    asked = await answer_the_turn(bridge, wire, plan="no")
    await call(bridge, 5, "history")
    await bridge.close()
    return {
        "questions": [a.get("question") for a in asked],
        "titles": [e["title"] for e in wire.events("plan_card")],
        "started": [e["name"] for e in wire.events("tool_started")],
        "finished_tools": [[e["name"], e["ok"]] for e in wire.events("tool_finished")],
        "answers": [e["text"] for e in wire.events("answer")],
        "model_calls": len(adapter.prompts),
        "finished": any(line.get("type") == "turn_finished" for line in wire.lines),
        "history": [m["role"] for m in wire.result(5)["result"]["messages"]],
        "view": shown(wire.result(5)["result"]["view"]),
    }


async def main(root: Path) -> dict[str, object]:
    profile = fake_profile(root, case_metadata={"private": SECRET})
    fingerprint_exam_cases(profile)
    (profile.parent / "inference.json").write_text(
        json.dumps([response(INTERVIEW), response(spec_json())]), encoding="utf-8")
    wire = Wire()
    bridge = Bridge(wire.write, adapter=Scripted(
        calls(("make", {"description": DESCRIPTION})), '{"text": "Built it."}'))
    await call(bridge, 1, "boot", cwd=str(root), workspace=str(root / "w"))
    await call(bridge, 2, "set_override", key="engineer", value="preset")
    await call(bridge, 3, "set_state", state="forge")
    await call(bridge, 4, "send", text=DESCRIPTION)
    asked = await answer_the_turn(bridge, wire)
    await call(bridge, 5, "agents")
    await call(bridge, 6, "agent", name=NAME)
    await call(bridge, 7, "run_agent", name=NAME, input={"text": "Invoice 7"})
    await call(bridge, 8, "history")
    await bridge.close()
    saved = "".join(path.read_text(encoding="utf-8")
                    for path in (root / "w" / "sessions" / "views").glob("*.jsonl"))
    sent ="\n".join(json.dumps(line) for line in wire.lines)
    kinds = [line.get("type") for line in wire.lines]
    turn = wire.lines[kinds.index("turn_started"):kinds.index("turn_finished")]
    issued = verification_ids(root)
    plans, results = cards(wire, "Plan: "), cards(wire, "Result: ")
    return {
        "questions": [a.get("question") for a in asked],
        "titles": [e["title"] for e in wire.events("plan_card")],
        "stages": [line["data"].get("stage") for line in wire.lines
                   if line.get("type") == "forge" and line["data"].get("kind") == "stage"],
        "progress": [e["text"] for e in wire.events("progress")],
        # What the app's spend meter got during the build: each state sent in the turn.
        "turn_progress": sum(1 for line in turn if line.get("type") == "event"
                             and line["event"]["kind"] == "progress"),
        "turn_spent": [line["state"]["spent_usd"] for line in turn
                       if line.get("type") == "state"],
        "finished": any(line.get("type") == "turn_finished" for line in wire.lines),
        "agents": wire.result(5).get("result"),
        "agent": wire.result(6).get("result"),
        "run": wire.result(7),
        "plan_data": plans[-1].get("data") if plans else None,
        "result_data": results[-1].get("data") if results else None,
        "issued": issued,
        "sent_held_out": HELD_OUT_MARK in sent,
        "sent_secret": SECRET in sent,
        "sent_ids": [value for value in issued if value in sent],
        "sent_verification_key": "verification_id" in sent,
        # What the conversation keeps of what it showed, for after a restart.
        "view": shown(wire.result(8)["result"]["view"]),
        "saved_held_out": HELD_OUT_MARK in saved,
        "saved_secret": SECRET in saved,
        "saved_ids": [value for value in issued if value in saved],
    }


if __name__ == "__main__":
    drive = not_now if sys.argv[2:] == ["notnow"] else main
    print(json.dumps(asyncio.run(drive(Path(sys.argv[1]))), default=str))
