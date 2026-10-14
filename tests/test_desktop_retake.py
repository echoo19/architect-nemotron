"""The desktop bridge's retake methods: what an agent's page is told about the agent in
the project it was added to, and the three things the page can do about a change.

Offline on the fake provider, sockets blocked, with the agent ``retake_support`` builds
and adds to a project. The bridge is driven without ``boot``: the methods read the
session's services and nothing else.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from forge_modes_support import FINGERPRINT, block_sockets, tree_digest
from retake_support import MISS, PACKAGE, Placed, place, record_rows

from architect.desktop.bridge import Bridge
from architect.desktop.data import BridgeError
from architect.forge_modes.errors import STOPPED
from architect.session.retake import remember_passed

AGENT = "feature-ok"
VIEW_KEYS = {
    "placed", "folder", "state", "package", "reason", "changed_files", "lines", "can_retake",
    "can_undo", "refusal", "attempt", "next_attempt", "model", "right", "total", "pass_mark",
    "unit", "note", "cases", "attempts", "plan", "progress", "last", "practice", "refused",
    "step", "retakes",
}
PLAN_KEYS = {
    "attempt", "cases", "total", "pass_mark", "left", "unit", "note", "estimate_usd",
    "cap_usd", "practice_total", "practice_estimate_usd", "head", "cost", "practice_cost",
    "place_only",
}
ATTEMPT_KEYS = {"number", "cases", "result", "right", "total", "what", "undone"}
PROGRESS_KEYS = {
    "attempt", "cases", "done", "total", "spent_usd", "cap_usd", "left_after", "unit",
    "pass_mark",
}


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    block_sockets(monkeypatch)


class Page:
    """One agent page's side of the bridge."""

    def __init__(self, placed: Placed) -> None:
        self.placed = placed
        self.services = placed.services()
        self.bridge = Bridge(lambda _text: None)
        self.bridge.repl = SimpleNamespace(app=SimpleNamespace(services=self.services))  # type: ignore[assignment]
        self.bridge.cwd = placed.repo
        self.seen: list[dict[str, Any]] = []

    async def ask(self, method: str) -> dict[str, Any]:
        view: dict[str, Any] = await getattr(self.bridge, "m_" + method)(AGENT)
        self.seen.append(view)
        return view

    async def settled(self) -> dict[str, Any]:
        """The view once the running retake or practice run has ended."""
        for run in list(self.bridge._retakes.values()):
            assert run.task is not None
            await run.task
        return await self.ask("retake_status")


@pytest.fixture
async def page(tmp_path: Path) -> AsyncIterator[Page]:
    placed = await place(tmp_path)
    remember_passed(placed.workspace, placed.folder, placed.staged, agent=AGENT)
    made = Page(placed)
    try:
        yield made
    finally:
        await made.bridge.close()
        made.services.close()
        # What the page was told never holds a case, a case id or a per-case result.
        for view in made.seen:
            text = json.dumps(view)
            assert FINGERPRINT not in text
            assert not any(f'"{prefix}0{n}"' in text for prefix in "cr" for n in range(8))
            if view.get("placed"):
                assert set(view) - {"undone"} == VIEW_KEYS
                assert all(set(item) == ATTEMPT_KEYS for item in view["attempts"])
                assert view["plan"] is None or set(view["plan"]) == PLAN_KEYS
                assert view["progress"] is None or set(view["progress"]) == PROGRESS_KEYS


def record(placed: Placed) -> list[dict[str, Any]]:
    return record_rows(placed.workspace)


async def test_an_agent_never_added_to_a_project_has_no_panel(tmp_path: Path) -> None:
    placed = await place(tmp_path)
    (placed.workspace / "placements.json").unlink()
    made = Page(placed)
    try:
        assert await made.ask("retake_status") == {"placed": False}
        with pytest.raises(BridgeError, match="not_placed"):
            await made.ask("retake")
    finally:
        made.services.close()


async def test_a_placement_noted_without_a_name_is_found_by_its_plan(tmp_path: Path) -> None:
    placed = await place(tmp_path)  # retake_support notes no agent name
    made = Page(placed)
    try:
        view = await made.ask("retake_status")
        assert (view["placed"], view["folder"]) == (True, f"agents/{PACKAGE}")
    finally:
        made.services.close()


async def test_the_agent_as_built_is_passed_with_its_counts(page: Page) -> None:
    view = await page.ask("retake_status")
    assert (view["state"], view["attempt"], view["cases"]) == ("passed", 1, "first_exam")
    assert (view["right"], view["total"], view["pass_mark"]) == (4, 4, 4)
    assert view["folder"] == f"agents/{PACKAGE}"
    assert view["unit"] == "cases" and view["plan"] is None
    assert [(a["number"], a["result"]) for a in view["attempts"]] == [(1, "passed")]
    assert "stopped" not in (await page.bridge.m_agents())["agents"][0]


async def test_a_change_stops_it_and_the_view_says_what_a_retake_would_be(page: Page) -> None:
    page.placed.use_model(MISS)
    view = await page.ask("retake_status")
    assert view["state"] == "stopped" and view["reason"] == "changed"
    assert view["lines"][0] == STOPPED
    assert view["lines"][1].startswith("Changed: artifact/config.json (model: ")
    assert view["changed_files"] == ["artifact/config.json"]
    assert (view["can_retake"], view["can_undo"]) == (True, True)
    plan = view["plan"]
    assert (plan["attempt"], plan["cases"], plan["total"], plan["pass_mark"]) == (
        2, "unused", 4, 4)
    assert (plan["left"], plan["practice_total"]) == (6, 4)
    assert plan["cap_usd"] > 0
    assert record(page.placed) == []  # looking uses no attempt
    rows = (await page.bridge.m_agents())["agents"]
    assert [(row["name"], row["stopped"]) for row in rows] == [(AGENT, "changed")]


async def test_a_retake_that_does_not_pass_then_undo(page: Page) -> None:
    page.placed.use_model(MISS)
    before = tree_digest(page.placed.staged)
    started = await page.ask("retake")
    assert started["state"] == "retaking"
    assert (started["progress"]["attempt"], started["progress"]["cases"]) == (2, "unused")
    assert started["progress"]["left_after"] == 2
    with pytest.raises(BridgeError, match="busy"):
        await page.ask("retake")
    view = await page.settled()
    assert view["state"] == "did_not_pass"
    last = view["last"]
    assert (last["number"], last["result"], last["right"], last["total"]) == (
        2, "did_not_pass", 0, 4)
    assert last["pass_mark"] == 4 and last["cost_usd"] > 0
    assert "model: " in last["what"]
    assert view["plan"]["attempt"] == 3  # the change may be examined again
    # The files change again: the old result is no longer this change's result.
    page.placed.reword()
    assert (await page.ask("retake_status"))["state"] == "stopped"
    page.placed.edit("spec.json", task="Return ok")
    undone = await page.ask("undo")
    assert undone["state"] == "passed" and undone["undone"][0].startswith(
        "The version that passed is back.")
    assert tree_digest(page.placed.folder) == before
    assert [(a["number"], a["result"], a["undone"]) for a in undone["attempts"]] == [
        (1, "passed", False), (2, "did_not_pass", True)]


async def test_a_retake_that_passes_counts_cases_and_never_a_score(page: Page) -> None:
    page.placed.reword()
    started = await page.ask("retake")
    assert started["state"] == "retaking" and started["progress"]["done"] == 0
    view = await page.settled()
    assert (view["state"], view["attempt"], view["cases"]) == ("passed", 2, "unused")
    assert (view["right"], view["total"]) == (4, 4)
    assert view["attempts"][-1]["what"] == "task text"
    assert view["progress"] is None and view["last"] is None
    # Two unused cases are left, fewer than an exam: the next retake is the same exam.
    page.placed.reword("Return ok, please")
    again = await page.ask("retake_status")
    assert (again["plan"]["attempt"], again["plan"]["cases"], again["plan"]["left"]) == (
        3, "same_exam", 2)


async def test_a_change_outside_the_editable_fields_offers_undo_only(page: Page) -> None:
    harness = page.placed.folder / "artifact" / "harness.py"
    harness.write_text(harness.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    view = await page.ask("retake_status")
    assert (view["state"], view["reason"]) == ("stopped", "changed_undo_only")
    assert (view["can_retake"], view["can_undo"], view["plan"]) == (False, True, None)
    assert view["lines"][1] == (
        "Changed: artifact/harness.py. A change to this file can only be undone.")
    for method in ("retake", "practice"):
        with pytest.raises(BridgeError, match="undo_only"):
            await page.ask(method)
    assert record(page.placed) == []
    assert (await page.ask("undo"))["state"] == "passed"


async def test_practice_gives_one_score_and_is_not_an_attempt(page: Page) -> None:
    page.placed.use_model(MISS)
    before = tree_digest(page.placed.folder)
    started = await page.ask("practice")
    assert started["state"] == "stopped" and started["practice"] == {"running": True}
    view = await page.settled()
    practice = view["practice"]
    assert practice["running"] is False
    assert practice["lines"][0] == "0 of 4 practice cases right. Not an attempt."
    assert (practice["right"], practice["total"]) == (0, 4)
    assert view["state"] == "stopped" and view["plan"]["attempt"] == 2
    assert record(page.placed) == [] and tree_digest(page.placed.folder) == before


async def test_cancelling_a_retake_keeps_the_attempt(page: Page) -> None:
    page.placed.reword()
    await page.ask("retake")
    run = next(iter(page.bridge._retakes.values()))
    while run.done < 1 and run.task is not None and not run.task.done():
        await _tick()
    view = await page.ask("retake_cancel")
    if view["state"] == "passed":  # four fake answers can beat the cancel
        pytest.skip("the retake finished before it could be cancelled")
    assert view["state"] == "stopped" and view["progress"] is None
    assert (view["last"]["number"], view["last"]["result"]) == (2, "cancelled")
    assert view["plan"]["attempt"] == 3


async def _tick() -> None:
    import asyncio

    await asyncio.sleep(0)


async def test_the_list_row_shows_the_score_of_the_version_in_the_project(page: Page) -> None:
    """After a retake passed, the row and the page name the same score: the row used to
    keep the build's while the page showed the retake's."""
    assert "placed" not in (await page.bridge.m_agents())["agents"][0]
    page.placed.reword()
    await page.ask("retake")
    view = await page.settled()
    assert (view["state"], view["attempt"]) == ("passed", 2)
    (row,) = (await page.bridge.m_agents())["agents"]
    assert row["placed"] == {"right": 4, "total": 4, "pass_mark": 4, "attempt": 2}
    assert "stopped" not in row


async def test_the_passed_page_knows_whether_a_change_could_be_retaken(tmp_path: Path) -> None:
    """The page promised "edit, Try on practice, then Retake exam" for an agent whose exam
    takes no retakes. ``retakes`` says which is true before anything is changed."""
    for name, floor, expected in (("open", {}, True), ("closed", {"retakes": None}, False)):
        placed = await place(tmp_path / name, floor=floor)
        remember_passed(placed.workspace, placed.folder, placed.staged, agent=AGENT)
        made = Page(placed)
        try:
            view = await made.ask("retake_status")
            assert (view["state"], view["retakes"]) == ("passed", expected)
        finally:
            await made.bridge.close()
            made.services.close()


async def test_an_exam_whose_cases_changed_offers_undo_and_says_why(page: Page) -> None:
    page.placed.use_model(MISS)
    cases = page.placed.profile.parent / "cases.json"
    rows = json.loads(cases.read_text(encoding="utf-8"))
    cases.write_text(json.dumps([{**row, "expected": "nope"} for row in rows]), encoding="utf-8")
    page.services.close()
    page.services = page.placed.services()  # the app is started again on the changed exam
    page.bridge.repl = SimpleNamespace(app=SimpleNamespace(services=page.services))  # type: ignore[assignment]
    view = await page.ask("retake_status")
    assert (view["can_retake"], view["can_undo"], view["refusal"], view["plan"]) == (
        False, True, "exam_changed", None)
    assert view["lines"][-2].startswith(
        "The exam in Architect's settings is not the one this agent passed")
    with pytest.raises(BridgeError, match="exam_changed"):
        await page.ask("retake")
    assert record(page.placed) == []


async def test_a_run_that_fails_says_so_in_words_without_a_code(
    page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    from architect.session import retake as retakes

    async def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("provider said: its-own-words")

    monkeypatch.setattr(retakes, "practice", broken)
    page.placed.reword()
    await page.ask("practice")
    view = await page.settled()
    (line,) = view["refused"]
    assert line.startswith("The practice run could not finish.")
    assert line.endswith("Your agent stays stopped.")
    assert "(" not in line and "its-own-words" not in line and "_" not in line
