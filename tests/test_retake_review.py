"""Retake: one regression test for each bug the review of ``hackathon/retake`` and the
ten-request trial found (``architect.session.retake``).

Offline on the fake provider, sockets blocked, on the agent ``retake_support`` builds and
adds to a project. Each test changes the folder, the workspace or the exam as a person, a
second terminal or a coding agent would, and says which mistake it guards against.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from forge_modes_support import block_sockets, case_rows, tree_digest
from retake_support import MISS, PACKAGE, Placed, place, record_rows

from architect.forge_modes import ExportedHarness, ExportError, export_package, place_package
from architect.forge_modes import export as export_module
from architect.forge_modes.errors import RELOADED, STOPPED
from architect.forge_modes.export import read_pin, replace_package
from architect.profiles import FloorConfig, load_profile
from architect.session import retake as retakes
from architect.session.retake import (
    RetakeError,
    attempts,
    plan,
    remember_passed,
    retake,
    standing,
    status,
    undo,
)
from architect.spec import FloorKind, HarnessArtifact
from architect.spec.license import check_license

ROOT = Path(__file__).resolve().parents[1]
UNDO_STEP = "Undo the change in Architect."
FIX_STEP = "Fix the file, or undo the change in Architect."


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    block_sockets(monkeypatch)


def yes(_text: str) -> bool:
    return True


def never(_text: str) -> bool:
    raise AssertionError("nothing was to be spent, so nothing was to be asked")


def record_file(placed: Placed) -> Path:
    (path,) = sorted((placed.workspace / "retakes").glob("*.jsonl"))
    return path


def loads(placed: Placed) -> HarnessArtifact:
    return ExportedHarness(placed.folder, read_pin(placed.folder) or {}).load()


# ------------------------------------------------------------------ which exam


async def test_a_retake_is_refused_when_the_exam_is_not_the_one_the_agent_passed(
    tmp_path: Path,
) -> None:
    """A changed agent that missed the pass mark must not pass because the exam's file was
    rewritten to suit it. The first report signs a digest of the exam's cases."""
    placed = await place(tmp_path, retake_cases=0)
    first = HarnessArtifact.load(placed.folder / "artifact").report
    assert first is not None and first.exam_sha256 is not None
    placed.use_model(MISS)  # answers "nope": 0 of 4
    services = placed.services()
    try:
        outcome = await retake(placed.folder, services, yes)
        assert outcome.passed is False and outcome.cases == "same_exam"
    finally:
        services.close()
    # The exam file is rewritten with cases the changed agent gets right. No key is touched.
    for count in (8, 12):  # the same number of cases, and another number
        rows = case_rows(count, expected="nope")
        (tmp_path / "cases.json").write_text(json.dumps(rows), encoding="utf-8")
        services = placed.services()
        try:
            for run in (retake, retakes.practice):
                with pytest.raises(RetakeError, match="^exam_changed$") as refused:
                    await run(placed.folder, services, never)
                assert refused.value.lines[-2] == (
                    "The exam in Architect's settings is not the one this agent passed: its "
                    "cases have changed. A retake needs that same exam, so no attempt was used."
                )
            with pytest.raises(RetakeError, match="^exam_changed$"):
                plan(placed.folder, services)
        finally:
            services.close()
    assert [row["attempt"] for row in record_rows(placed.workspace)] == [1, 2]
    with pytest.raises(ExportError):
        loads(placed)


async def test_the_exam_digest_is_signed_and_only_where_the_exam_allows_retakes(
    tmp_path: Path,
) -> None:
    placed = await place(tmp_path / "open")
    artifact = HarnessArtifact.load(placed.folder / "artifact")
    report = artifact.report
    assert report is not None and report.exam_sha256 is not None
    services = placed.services()
    try:
        floor = services.compiler.floor_factory(artifact.spec.floor)
        assert floor.exam_digest == report.exam_sha256
    finally:
        services.close()
    forged = artifact.model_copy(
        update={"report": report.model_copy(update={"exam_sha256": "0" * 64})}
    )
    assert check_license(artifact).licensed and not check_license(forged).licensed
    # An exam that takes no retakes signs no digest: its reports read as they always did.
    closed = await place(tmp_path / "closed", floor={"retakes": None})
    report = HarnessArtifact.load(closed.folder / "artifact").report
    assert report is not None and report.exam_sha256 is None
    assert "exam_sha256" not in closed.read("report.json")


def test_no_exam_is_retaken_unless_its_entry_says_so(tmp_path: Path) -> None:
    """The evidence exam stays closed to a changed design even in a copy of the profile
    that lost its manifest line: refusing is the default, not a property of that line."""
    evidence = ROOT / "examples" / "receipts" / "floors" / "evidence.json"
    bare = FloorConfig.model_validate(
        {"kind": "held_out_tasks", "path": str(evidence), "checker": "receipt_v1"}
    )
    assert not bare.retakes_allowed
    # An exam the session writes for a person's own agent says so, and may be retaken.
    from architect.session.floors import register

    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"models": {"fake": {"provider": "fake"}}}), encoding="utf-8")
    cases = tmp_path / "floors" / "mine.json"
    cases.parent.mkdir()
    cases.write_text(json.dumps(case_rows(4)), encoding="utf-8")
    register(profile, "mine", FloorKind.HELD_OUT_TASKS, cases)
    assert load_profile(profile).floors["mine"].retakes_allowed


# ------------------------------------------------------------------ the record


async def test_a_record_line_that_does_not_read_ends_the_claim_of_unused_cases(
    tmp_path: Path,
) -> None:
    placed = await place(tmp_path)
    placed.use_model(MISS)
    services = placed.services()
    try:
        first = await retake(placed.folder, services, yes)
        assert (first.attempt, first.cases) == (2, "unused")
        path = record_file(placed)
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join([lines[0], "<<<<<<< HEAD", *lines[1:]]) + "\n", encoding="utf-8")
        planned = plan(placed.folder, services)
        assert (planned["attempt"], planned["cases"]) == (3, "same_exam")
        texts: list[str] = []
        second = await retake(placed.folder, services, lambda text: texts.append(text) or True)
    finally:
        services.close()
    assert texts[0].startswith(
        "Same exam, attempt 3. This computer's record of earlier attempts is incomplete, so "
        "it cannot say which cases are unused, so the agent retakes the 4 cases of its first "
        "exam."
    )
    assert (second.attempt, second.cases) == (3, "same_exam")
    assert second.lines[1] == "Same exam, attempt 3. Your agent is still stopped."
    # Nothing was dropped from the file: the line that does not read is still there, with
    # every row that was before and after it.
    kept = record_file(placed).read_text(encoding="utf-8").splitlines()
    assert kept[: len(lines) + 1] == [lines[0], "<<<<<<< HEAD", *lines[1:]]


async def test_a_deleted_record_does_not_hand_out_the_same_unused_cases_again(
    tmp_path: Path,
) -> None:
    placed = await place(tmp_path)
    placed.reword()
    services = placed.services()
    try:
        first = await retake(placed.folder, services, yes)
        assert first.passed and (first.attempt, first.cases) == (2, "unused")
        record_file(placed).unlink()
        placed.reword("Return ok please")
        second = await retake(placed.folder, services, yes)
    finally:
        services.close()
    # The signed report in the folder says attempt 2 ran on unused cases, and the record
    # no longer says which: attempt 3 cannot call any case unused.
    assert second.passed and (second.attempt, second.cases) == (3, "same_exam")
    report = loads(placed).report
    assert report is not None and (report.attempt, report.retake_cases) == (3, "same_exam")


async def test_the_same_agent_under_two_names_shares_one_record(tmp_path: Path) -> None:
    placed = await place(tmp_path)
    services = placed.services()
    try:
        entry = services.registry.status("feature-ok")
        manifest, _ = export_package(
            entry, placed.workspace / "exports", "second_name", model_class="synthetic_fake"
        )
        staged = placed.workspace / "exports" / manifest.artifact_digest[:16] / "second_name"
        place_package(staged, placed.repo, "agents")
        other = placed.repo / "agents" / "second_name"
        remember_passed(placed.workspace, other, staged)
        placed.use_model(MISS)
        config = other / "artifact" / "config.json"
        value = json.loads(config.read_text(encoding="utf-8"))
        config.write_text(json.dumps({**value, "model": MISS}), encoding="utf-8")
        first = await retake(placed.folder, services, yes)
        second = await retake(other, services, yes)
    finally:
        services.close()
    # Six extra cases: the first folder's attempt used four, so the second folder's is not
    # "on 4 cases no earlier attempt used" a second time.
    assert (first.attempt, first.cases) == (2, "unused")
    assert (second.attempt, second.cases) == (3, "same_exam")
    assert [row["attempt"] for row in record_rows(placed.workspace)] == [1, 2, 3]


async def test_two_retakes_of_one_agent_at_once_use_one_attempt(tmp_path: Path) -> None:
    placed = await place(tmp_path)
    placed.use_model(MISS)
    one, two = placed.services(), placed.services()
    try:
        results = await asyncio.gather(
            retake(placed.folder, one, yes), retake(placed.folder, two, yes),
            return_exceptions=True,
        )
    finally:
        one.close()
        two.close()
    refused = [item for item in results if isinstance(item, RetakeError)]
    ran = [item for item in results if not isinstance(item, BaseException)]
    assert len(refused) == 1 and refused[0].code == "busy" and len(ran) == 1
    assert refused[0].lines == (
        "Another retake of this agent is running. Wait for it to finish, or cancel it where "
        "it was started. No attempt was used.",
    )
    assert [(row["attempt"], row["result"]) for row in record_rows(placed.workspace)] == [
        (1, "passed"), (2, "did_not_pass")]
    # The lock goes with the retake: the next one runs.
    services = placed.services()
    try:
        assert (await retake(placed.folder, services, yes)).attempt == 3
    finally:
        services.close()


# ------------------------------------------------------------------ the folder, when it passed


async def test_an_undo_made_while_the_exam_runs_is_not_written_over(tmp_path: Path) -> None:
    placed = await place(tmp_path)
    passed = tree_digest(placed.folder)
    placed.reword()

    def undo_on_first_case(done: int, _total: int) -> None:
        if done == 1:
            undo(placed.folder, placed.workspace)  # another terminal: architect undo

    services = placed.services()
    try:
        outcome = await retake(placed.folder, services, yes, on_case=undo_on_first_case)
    finally:
        services.close()
    assert outcome.passed is False and outcome.lines == (
        "4 of 4. Pass mark 4. Passed, but its folder was not written.",
        "The agent's files changed while the exam ran, so its folder was left as it is now. "
        "What is in it has not been examined.",
        "Attempt 2, on 4 cases no earlier attempt used. The attempt was used.",
    )
    assert tree_digest(placed.folder) == passed and loads(placed).spec.task == "Return ok"
    assert record_rows(placed.workspace)[-1]["result"] == "not_placed"
    assert attempts(placed.folder, placed.workspace)[-1].result == "not_placed"


async def test_a_second_edit_made_while_the_exam_runs_stays_and_stays_stopped(
    tmp_path: Path,
) -> None:
    placed = await place(tmp_path)
    placed.reword()

    def edit_on_first_case(done: int, _total: int) -> None:
        if done == 1:
            placed.use_model(MISS)  # a second, unexamined edit

    services = placed.services()
    try:
        outcome = await retake(placed.folder, services, yes, on_case=edit_on_first_case)
        state = status(placed.folder, placed.workspace, services)
    finally:
        services.close()
    assert outcome.passed is False and placed.read("config.json")["model"] == MISS
    assert state.state == "stopped" and state.can_retake and state.next_attempt == 3


async def test_a_pass_whose_folder_could_not_be_written_is_placed_later_without_an_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows an open file in the agent's folder stops the folder being replaced. The
    attempt passed, so it is not listed as a plain pass and is not thrown away either."""
    placed = await place(tmp_path)
    placed.reword()
    edited = tree_digest(placed.folder)
    real = retakes.replace_package

    def locked(_staged: Path, _target: Path) -> Any:
        raise ExportError("export_failed")

    monkeypatch.setattr(retakes, "replace_package", locked)
    services = placed.services()
    try:
        outcome = await retake(placed.folder, services, yes)
        assert outcome.passed is False and outcome.lines == (
            "4 of 4. Pass mark 4. Passed, but its folder was not written.",
            "The agent's folder could not be written, so it is unchanged and still stopped. "
            "Close any program that has the folder open, then retake again: that writes the "
            "folder without a new attempt.",
            "Attempt 2, on 4 cases no earlier attempt used. The attempt was used.",
        )
        assert "export_failed" not in " ".join(outcome.lines)  # no error code on screen
        assert tree_digest(placed.folder) == edited
        assert record_rows(placed.workspace)[-1]["result"] == "not_placed"
        assert attempts(placed.folder, placed.workspace)[-1].result == "not_placed"
        planned = plan(placed.folder, services)
        assert (planned["attempt"], planned["place_only"]) == (2, True)
        assert planned["head"] == (
            "Attempt 2 passed on this change, but its folder could not be written then. "
            "This writes it now."
        ) and planned["cost"] == "No new attempt and no cost."
        monkeypatch.setattr(retakes, "replace_package", real)
        again = await retake(placed.folder, services, never)
    finally:
        services.close()
    assert again.passed and (again.attempt, again.cases) == (2, "unused")
    assert again.lines[1] == (
        "Attempt 2, on 4 cases no earlier attempt used. Its folder is written now, with no "
        "new attempt. Your app's calls work again."
    )
    assert [(row["attempt"], row["result"]) for row in record_rows(placed.workspace)] == [
        (1, "passed"), (2, "passed")]
    report = loads(placed).report
    assert report is not None and report.attempt == 2


async def test_a_pass_stands_when_the_copy_for_undo_cannot_be_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    placed = await place(tmp_path)
    placed.reword()
    real = retakes.remember_passed

    def locked(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(retakes, "remember_passed", locked)
    services = placed.services()
    try:
        outcome = await retake(placed.folder, services, yes)
    finally:
        services.close()
    monkeypatch.setattr(retakes, "remember_passed", real)
    assert outcome.passed and record_rows(placed.workspace)[-1]["result"] == "passed"
    # Undo after a later edit is the last version that passed, attempt 2, although the
    # kept copy is still the version from the build.
    second = tree_digest(placed.folder)
    placed.use_model(MISS)
    state = status(placed.folder, placed.workspace)
    assert (state.attempt, state.next_attempt) == (2, 3)
    assert undo(placed.folder, placed.workspace).attempt == 2
    assert tree_digest(placed.folder) == second


async def test_a_cancel_that_lands_while_the_folder_is_written_does_not_cut_it_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    placed = await place(tmp_path)
    placed.reword()
    real = retakes.replace_package
    inside = threading.Event()

    def slow(staged: Path, target: Path) -> Any:
        inside.set()
        time.sleep(0.3)
        return real(staged, target)

    monkeypatch.setattr(retakes, "replace_package", slow)
    services = placed.services()
    try:
        task = asyncio.ensure_future(retake(placed.folder, services, yes))
        await asyncio.to_thread(inside.wait, 30)
        task.cancel()  # the app's Cancel button, or Ctrl+C
        outcome = await task  # the verdict was in: it is reported, not a cancel
    finally:
        services.close()
    assert outcome.passed and outcome.attempt == 2
    assert record_rows(placed.workspace)[-1]["result"] == "passed"
    assert tree_digest(placed.workspace / "passed" / PACKAGE) == tree_digest(placed.folder)
    assert status(placed.folder, placed.workspace).state == "passed"


async def test_a_folder_set_back_to_an_older_version_is_measured_against_that_version(
    tmp_path: Path,
) -> None:
    placed = await place(tmp_path)
    first = tree_digest(placed.folder)
    kept = tmp_path / "v1"
    shutil.copytree(placed.folder, kept)
    placed.reword()
    services = placed.services()
    try:
        assert (await retake(placed.folder, services, yes)).passed
    finally:
        services.close()
    shutil.rmtree(placed.folder)
    shutil.copytree(kept, placed.folder)  # git checkout HEAD~1 -- agents/ok_feature
    assert status(placed.folder, placed.workspace).attempt == 1
    placed.use_model(MISS)
    state = status(placed.folder, placed.workspace)
    assert state.attempt == 1 and state.lines[1] == (
        "Changed: artifact/config.json (model: fake to gpt-4o-mini)."
    )
    assert undo(placed.folder, placed.workspace).attempt == 1
    assert tree_digest(placed.folder) == first


def test_a_replace_that_cannot_move_the_old_folder_back_says_where_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    placed = asyncio.run(place(tmp_path))
    passed = tree_digest(placed.folder)
    placed.reword()
    edited = tree_digest(placed.folder)
    real, calls = os.replace, {"n": 0}

    def fail_after_the_first(source: Any, target: Any) -> None:
        calls["n"] += 1
        if calls["n"] > 1:
            raise PermissionError("in use")
        real(source, target)

    monkeypatch.setattr(export_module.os, "replace", fail_after_the_first)
    with pytest.raises(ExportError, match="^export_failed$") as failed:
        replace_package(placed.staged, placed.folder)
    monkeypatch.setattr(export_module.os, "replace", real)
    aside = failed.value.aside
    assert aside is not None and aside.parent == placed.folder.parent
    assert not placed.folder.exists() and tree_digest(aside) == edited
    # Undo finds the folder where the replace left it, moves it back and restores.
    outcome = undo(placed.folder, placed.workspace)
    assert outcome.attempt == 1 and tree_digest(placed.folder) == passed
    assert sorted(path.name for path in placed.folder.parent.iterdir()) == [PACKAGE]


def test_undo_that_cannot_write_the_folder_says_so_without_a_code(
    placed_agent: Placed, monkeypatch: pytest.MonkeyPatch
) -> None:
    placed = placed_agent
    placed.reword()
    before = tree_digest(placed.folder)

    def locked(_staged: Path, _target: Path) -> Any:
        raise ExportError("export_failed")

    monkeypatch.setattr(retakes, "replace_package", locked)
    with pytest.raises(RetakeError, match="^not_written$") as refused:
        undo(placed.folder, placed.workspace)
    assert refused.value.lines == (
        "The version that passed could not be put back. The folder is as it was. Close any "
        "program that has it open, then undo again.",
    )
    assert tree_digest(placed.folder) == before


@pytest.fixture
def placed_agent(tmp_path: Path) -> Placed:
    return asyncio.run(place(tmp_path))


# ------------------------------------------------------------------ a running app


async def test_an_app_that_holds_the_old_version_is_told_to_restart_not_to_undo(
    tmp_path: Path,
) -> None:
    placed = await place(tmp_path)
    old_pin = read_pin(placed.folder) or {}
    placed.reword()
    services = placed.services()
    try:
        assert (await retake(placed.folder, services, yes)).passed
    finally:
        services.close()
    with pytest.raises(ExportError, match="^export_pin_mismatch$") as stopped:
        ExportedHarness(placed.folder, old_pin).load()
    error = stopped.value
    assert (error.reason, error.sentence, error.changed_files) == ("reloaded", RELOADED, ())
    assert error.reason_line == (
        "The version in its folder passed its exam. Restart the app to load it."
    )
    assert loads(placed).spec.task == "Return ok, and nothing else"  # a new import runs
    # A manifest that was really rewritten is still a change that can only be undone.
    path = placed.folder / "MANIFEST.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["artifact_digest"] = "0" * 64
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ExportError) as changed:
        ExportedHarness(placed.folder, old_pin).load()
    assert (changed.value.reason, changed.value.sentence) == ("changed_undo_only", STOPPED)


# ------------------------------------------------------------------ what the stop says


def test_a_plan_nested_too_deep_is_an_edit_to_fix_not_a_crash(placed_agent: Placed) -> None:
    placed = placed_agent
    (placed.folder / "artifact" / "spec.json").write_text("[" * 200_000, encoding="utf-8")
    state = status(placed.folder, placed.workspace)
    assert (state.state, state.refusal, state.reason) == ("stopped", "edit_invalid", "changed")
    assert state.lines[-1] == "artifact/spec.json cannot be read as JSON. Fix it or undo."
    assert state.can_undo and not state.can_retake and state.step == FIX_STEP


def test_only_the_checks_may_change_in_how_the_agent_runs(placed_agent: Placed) -> None:
    placed = placed_agent
    placed.edit("spec.json", execution_config={"limits": {"steps": 99}})
    state = status(placed.folder, placed.workspace)
    assert (state.refusal, state.reason) == ("undo_only", "changed_undo_only")
    assert "The change touches how the agent runs." in state.lines and state.step == UNDO_STEP


@pytest.mark.parametrize(
    ("params", "said"),
    [
        (
            {"context_window": 512, "terseness": 0, "verifier_depth": 2},
            "artifact/config.json: verifier_depth is 2. It can be 0 or 1. Fix it or undo.",
        ),
        (
            {"context_window": 512, "temperature": 0.2},
            'artifact/config.json: "temperature" is not a setting of this agent. Its '
            "settings are context_window, terseness and verifier_depth. Fix it or undo.",
        ),
    ],
)
def test_a_setting_that_is_refused_is_named_with_what_it_may_be(
    placed_agent: Placed, params: dict[str, Any], said: str
) -> None:
    placed = placed_agent
    placed.edit("config.json", params=params)
    services = placed.services()
    try:
        state = status(placed.folder, placed.workspace, services)
        assert state.refusal == "edit_invalid" and state.lines[-1] == said
        assert not state.can_retake and state.step == FIX_STEP
        with pytest.raises(RetakeError, match="^edit_invalid$"):
            asyncio.run(retake(placed.folder, services, never))
    finally:
        services.close()


def test_a_missing_field_of_the_plan_is_named(placed_agent: Placed) -> None:
    placed = placed_agent
    value = placed.read("spec.json")
    del value["task"]
    path = placed.folder / "artifact" / "spec.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    state = status(placed.folder, placed.workspace)
    assert state.refusal == "edit_invalid" and state.lines[-1] == (
        "artifact/spec.json is no longer a plan Architect can read. task: Field required. "
        "Fix it or undo."
    )


def test_a_change_to_the_budget_is_undo_only_and_still_names_a_model_it_does_not_know(
    placed_agent: Placed,
) -> None:
    """The trial's first request: a model the settings do not have, and a halved budget.
    The refusal used to name the budget alone, and the app offered a retake."""
    placed = placed_agent
    placed.edit("spec.json", budget={"cost_usd": 0.05, "compute_s": 30.0})
    placed.use_model("nvidia/nemotron-3-8b-instruct")
    state = standing(placed.folder, placed.profile)
    assert (state.refusal, state.can_retake, state.step) == ("undo_only", False, UNDO_STEP)
    assert state.lines[2:] == (
        "The change touches the budget.",
        "The pass mark, the exam, the answer format, the budget, the tools and the settings "
        "Architect writes itself are fixed, so this change can only be undone.",
        "The model nvidia/nemotron-3-8b-instruct is not in Architect's settings here. Add it "
        "there, or name one that is.",
    )


def test_standing_reads_the_profile_file_and_says_what_can_be_done(
    placed_agent: Placed,
) -> None:
    placed = placed_agent
    assert standing(placed.folder, placed.profile).state == "passed"
    placed.reword()
    state = standing(placed.folder, placed.profile)
    assert state.can_retake and state.step == "Retake its exam or undo the change in Architect."
    placed.use_model("somebody/elses-model")
    state = standing(placed.folder, placed.profile)
    assert (state.refusal, state.can_retake, state.step) == (
        "model_not_in_profile", False, FIX_STEP)


def test_the_estimate_follows_the_price_of_the_model_the_edit_names(
    placed_agent: Placed,
) -> None:
    """The trial's by-hand run changed the model, and the line still quoted the old
    model's cost. The estimate is scaled by the two models' input prices."""
    placed = placed_agent
    services = placed.services()
    try:
        placed.reword()
        same = plan(placed.folder, services)["estimate_usd"]
        placed.use_model(MISS)
        other = plan(placed.folder, services)["estimate_usd"]
        old = retakes._input_rate(services, "fake")
        new = retakes._input_rate(services, MISS)
    finally:
        services.close()
    assert isinstance(same, float) and isinstance(other, float) and old and new and old != new
    assert other == pytest.approx(same * new / old)


def test_the_cost_line_explains_its_two_numbers() -> None:
    assert retakes._cost_words(0.03, 0.6) == (
        "About $0.03. It stops if it reaches $0.60, the build's cost cap."
    )
    assert retakes._cost_words(None, 0.6) == (
        "It stops if it reaches $0.60, the build's cost cap."
    )
    assert retakes._cost_words(0.03, None) == "About $0.03."


# ------------------------------------------------------------------ the coder's reply


def test_the_host_adds_what_it_saw_itself_to_the_coders_reply(placed_agent: Placed) -> None:
    """The trial's coder said "Changes verified" over a plan that no longer parsed, and
    reported success after changing nothing. These lines do not come from the coder."""
    from architect.agent.coder import _notes

    placed, root = placed_agent, placed_agent.repo
    assert _notes(root, [], None) == ["No file changed."]
    (root / "data.json").write_text('{"a": "one\ntwo"}', encoding="utf-8")
    (root / "notes.md").write_text("fine", encoding="utf-8")
    assert _notes(root, ["data.json", "notes.md"], None) == ["data.json is not valid JSON now."]
    spec = placed.folder / "artifact" / "spec.json"
    spec.write_text(spec.read_text(encoding="utf-8").replace('"Return ok"', '"Return "ok""'),
                    encoding="utf-8")
    harness = placed.folder / "artifact" / "harness.py"
    harness.write_text(harness.read_text(encoding="utf-8") + "# tuned\n", encoding="utf-8")
    changed = [f"agents/{PACKAGE}/artifact/spec.json", f"agents/{PACKAGE}/artifact/harness.py"]
    assert _notes(root, changed, "coder_budget_exceeded") == [
        "The run reached its budget before it finished. The files listed changed before that.",
        f"agents/{PACKAGE}/artifact/spec.json is not valid JSON now.",
        f"agents/{PACKAGE}: {STOPPED} Changed: artifact/harness.py, artifact/spec.json. A "
        "change to these files can only be undone.",
    ]
    undo(placed.folder, placed.workspace)
    placed.reword()
    assert _notes(root, [f"agents/{PACKAGE}/artifact/spec.json"], None) == [
        f"agents/{PACKAGE}: {STOPPED} Changed: artifact/spec.json. It stays stopped until it "
        "passes its exam again or the change is undone.",
    ]
