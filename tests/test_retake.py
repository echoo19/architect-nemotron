"""Retake: an agent only runs if it passed its exam; change it and it stops until it
passes again (``architect.session.retake``).

Offline on the fake provider, sockets blocked. One agent is built, exported and added to
a project (``retake_support.place``); the tests then change its folder as a person or a
coding agent would.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from forge_modes_support import block_sockets, tree_digest
from retake_support import MISS, PACKAGE, Placed, place, record_rows

import architect.runtime.evaluation as evaluation
from architect.forge_modes import ExportedHarness, ExportError, exam_line
from architect.forge_modes.errors import CANNOT_CHECK, STOPPED
from architect.forge_modes.export import flag_name, read_flag, read_pin, verify_export
from architect.models import FakeAdapter
from architect.runtime import Runtime
from architect.session import retake as retakes
from architect.session.retake import RetakeError, attempts, practice, retake, status, undo
from architect.spec import HarnessArtifact
from architect.telemetry import TelemetryStore

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    block_sockets(monkeypatch)


@pytest.fixture
def placed(tmp_path: Path) -> Placed:
    return asyncio.run(place(tmp_path))


def yes(_text: str) -> bool:
    return True


def record(placed: Placed) -> list[dict[str, Any]]:
    return record_rows(placed.workspace)


def loads(placed: Placed) -> HarnessArtifact:
    return ExportedHarness(placed.folder, read_pin(placed.folder) or {}).load()


async def call(placed: Placed, tmp_path: Path) -> tuple[object, int]:
    """One call of the placed agent as an app makes it: the answer and the model calls."""
    adapter = FakeAdapter(model="fake")
    runtime = Runtime(TelemetryStore(tmp_path / "app-telemetry"))
    try:
        harness = ExportedHarness(placed.folder, read_pin(placed.folder) or {})
        return await harness.run_with(runtime, adapter, {"text": "x"}), adapter.calls
    finally:
        runtime.store.close()


# ------------------------------------------------------------------ the stop


async def test_an_agent_that_passed_runs_and_says_which_attempt(tmp_path: Path) -> None:
    placed = await place(tmp_path)
    answer, calls = await call(placed, tmp_path)
    assert answer == "ok" and calls > 0
    state = status(placed.folder, placed.workspace)
    assert state.state == "passed" and state.attempt == 1 and state.next_attempt == 2
    assert state.lines == (
        "Passed. It is your agent.", "fake. 4 of 4. Pass mark 4. Passed. Attempt 1.")
    assert exam_line(placed.folder) == "passed its exam. 4 of 4, pass mark 4, attempt 1."


async def test_an_edit_stops_the_agent_before_any_model_call(tmp_path: Path) -> None:
    placed = await place(tmp_path)
    placed.use_model(MISS)
    adapter = FakeAdapter(model="fake")
    runtime = Runtime(TelemetryStore(tmp_path / "app-telemetry"))
    try:
        harness = ExportedHarness(placed.folder, read_pin(placed.folder) or {})
        with pytest.raises(ExportError) as stopped:
            await harness.run_with(runtime, adapter, {"text": "x"})
    finally:
        runtime.store.close()
    error = stopped.value
    assert adapter.calls == 0
    assert str(error) == error.code == "export_integrity_mismatch"  # the bare code, as before
    assert error.sentence == STOPPED == "Stopped: changed after its exam. 0 model calls."
    assert error.reason == "changed"
    assert error.changed_files == ("artifact/config.json",)
    assert error.reason_line == "Changed: artifact/config.json."
    with pytest.raises(ExportError) as line:
        exam_line(placed.folder)
    assert line.value.sentence == STOPPED


async def test_a_refusal_that_is_not_a_stop_carries_no_sentence() -> None:
    error = ExportError("export_path_invalid")
    assert str(error) == "export_path_invalid"
    assert (error.sentence, error.reason, error.reason_line, error.changed_files) == (
        None, None, None, ())


async def test_without_the_signing_key_it_does_not_say_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing changed, so "changed after its exam" would be false."""
    placed = await place(tmp_path)
    monkeypatch.delenv("ARCHITECT_SIGNING_KEY_FILE")
    with pytest.raises(ExportError) as stopped:
        loads(placed)
    error = stopped.value
    assert str(error) == "license_unverifiable" and error.reason == "cannot_check"
    assert error.sentence == CANNOT_CHECK and error.changed_files == ()
    assert error.sentence == (
        "Stopped: its exam report cannot be checked on this computer. 0 model calls."
    )
    state = status(placed.folder, placed.workspace)
    assert state.reason == "cannot_check" and not state.can_retake and not state.can_undo


async def test_status_names_what_changed_inside_the_file(tmp_path: Path) -> None:
    placed = await place(tmp_path)
    placed.use_model(MISS)
    placed.reword()
    services = placed.services()
    try:
        state = status(placed.folder, placed.workspace, services)
    finally:
        services.close()
    assert state.state == "stopped" and state.reason == "changed"
    assert state.can_retake and state.can_undo and state.refusal is None
    assert state.lines == (
        STOPPED,
        "Changed: artifact/config.json (model: fake to gpt-4o-mini), artifact/spec.json "
        "(task text).",
    )
    assert (state.attempt, state.next_attempt, state.right, state.total, state.pass_mark) == (
        1, 2, 4, 4, 4)


# ------------------------------------------------------------------ undo


async def test_undo_restores_the_version_that_passed_byte_for_byte(tmp_path: Path) -> None:
    placed = await place(tmp_path)
    before = tree_digest(placed.folder)
    assert before == tree_digest(placed.staged)
    placed.use_model(MISS)
    (placed.folder / "artifact" / "harness.py").write_text("raise SystemExit\n", encoding="utf-8")
    (placed.folder / "notes.txt").write_text("left by a coder", encoding="utf-8")
    with pytest.raises(ExportError):
        loads(placed)
    outcome = undo(placed.folder, placed.workspace)
    assert outcome.kind == "undo" and outcome.lines == (
        "The version that passed is back. It is fake, 4 of 4, passed, attempt 1. 0 model "
            "calls.",
    )
    assert tree_digest(placed.folder) == before
    assert sorted(path.name for path in placed.folder.parent.iterdir()) == [PACKAGE]
    answer, calls = await call(placed, tmp_path)
    assert answer == "ok" and calls > 0
    assert record(placed) == []  # not an attempt
    with pytest.raises(RetakeError, match="^not_stopped$"):
        undo(placed.folder, placed.workspace)


async def test_undo_falls_back_to_the_staged_export_and_else_says_there_is_no_copy(
    tmp_path: Path,
) -> None:
    """A package added before the copy was kept is restored from its staged export."""
    import shutil

    placed = await place(tmp_path)
    before = tree_digest(placed.folder)
    shutil.rmtree(placed.workspace / "passed")
    (placed.workspace / "placements.json").unlink()
    placed.reword()
    assert status(placed.folder, placed.workspace).can_undo
    undo(placed.folder, placed.workspace)
    assert tree_digest(placed.folder) == before
    placed.reword()
    shutil.rmtree(placed.workspace / "exports")
    state = status(placed.folder, placed.workspace)
    assert not state.can_undo and not state.can_retake and state.refusal == "no_copy"
    assert state.lines[-1].startswith(
        "This computer has no copy of the version that passed. Restore "
    ) and state.lines[-1].endswith("with git.")
    changed = tree_digest(placed.folder)
    with pytest.raises(RetakeError, match="^no_copy$"):
        undo(placed.folder, placed.workspace)
    assert tree_digest(placed.folder) == changed


async def test_a_copy_that_was_tampered_with_is_not_restored(tmp_path: Path) -> None:
    placed = await place(tmp_path)
    kept = placed.workspace / "passed" / PACKAGE
    (kept / "artifact" / "harness.py").write_text("raise SystemExit\n", encoding="utf-8")
    placed.reword()
    undo(placed.folder, placed.workspace)  # the staged export still passes its checks
    assert tree_digest(placed.folder) == tree_digest(placed.staged)
    (placed.staged / "README.md").write_text("changed too", encoding="utf-8")
    placed.reword()
    with pytest.raises(RetakeError, match="^no_copy$"):
        undo(placed.folder, placed.workspace)


def _edit_harness(placed: Placed) -> None:
    path = placed.folder / "artifact" / "harness.py"
    path.write_text(path.read_text(encoding="utf-8") + "\n# tuned by hand\n", encoding="utf-8")


def _edit_readme(placed: Placed) -> None:
    (placed.folder / "README.md").write_text("mine now", encoding="utf-8")


def _edit_report(placed: Placed) -> None:
    placed.edit("report.json", floor_score=1.0, held_out_count=400)


def _add_file(placed: Placed) -> None:
    (placed.folder / "artifact" / "extra.py").write_text("x = 1\n", encoding="utf-8")


def _drop_manifest(placed: Placed) -> None:
    (placed.folder / "MANIFEST.json").unlink()


def _lower_pass_mark(placed: Placed) -> None:
    floor = placed.read("spec.json")["floor"]
    placed.edit("spec.json", floor={**floor, "limit": 0.25})


def _raise_budget(placed: Placed) -> None:
    placed.edit("spec.json", budget={"cost_usd": 5.0, "compute_s": 600.0})


def _change_schema(placed: Placed) -> None:
    placed.edit("spec.json", output_schema={"type": "object"})


def _change_components(placed: Placed) -> None:
    placed.edit("config.json", components=["synthesis"])


@pytest.mark.parametrize(
    ("change", "words"),
    [
        (_edit_harness, "Changed: artifact/harness.py. A change to this file can only be undone."),
        (_edit_readme, "Changed: README.md. A change to this file can only be undone."),
        (_edit_report, "Changed: artifact/report.json. A change to this file can only be undone."),
        (_add_file, "Changed: artifact/extra.py. A change to this file can only be undone."),
        (_drop_manifest, "Changed: MANIFEST.json. A change to this file can only be undone."),
        (_lower_pass_mark, "The change touches the pass mark or the exam."),
        (_raise_budget, "The change touches the budget."),
        (_change_schema, "The change touches the answer format."),
        (_change_components, "The change touches settings Architect writes itself."),
    ],
)
def test_a_change_outside_the_editable_fields_can_only_be_undone(
    placed: Placed, change: Any, words: str
) -> None:
    before = tree_digest(placed.folder)
    change(placed)
    services = placed.services()
    try:
        state = status(placed.folder, placed.workspace, services)
        assert state.lines[0] == STOPPED and words in state.lines
        assert state.reason == "changed_undo_only"
        assert not state.can_retake and state.can_undo
        for run in (retake, practice):
            with pytest.raises(RetakeError) as refused:
                asyncio.run(run(placed.folder, services, yes))
            assert refused.value.lines[0] == STOPPED and words in refused.value.lines
    finally:
        services.close()
    assert record(placed) == []  # refused: no attempt was used
    undo(placed.folder, placed.workspace)
    assert tree_digest(placed.folder) == before


def test_an_edited_harness_gets_the_hint_and_is_never_run(placed: Placed, tmp_path: Path) -> None:
    marker = tmp_path / "ran.txt"
    code = f"open({str(marker)!r}, 'w').write('ran')\n"
    path = placed.folder / "artifact" / "harness.py"
    path.write_text(code + path.read_text(encoding="utf-8"), encoding="utf-8")
    placed.reword()
    services = placed.services()
    try:
        state = status(placed.folder, placed.workspace, services)
        assert state.lines[2] == (
            "Architect writes harness.py from the agent's task, checks, model and settings. "
            "To change the agent, edit those in artifact/spec.json or artifact/config.json."
        )
        with pytest.raises(RetakeError, match="^undo_only$"):
            asyncio.run(retake(placed.folder, services, yes))
        with pytest.raises(ExportError):
            loads(placed)
    finally:
        services.close()
    assert not marker.exists()


def test_an_edit_that_no_longer_reads_is_refused_without_an_attempt(placed: Placed) -> None:
    (placed.folder / "artifact" / "spec.json").write_text("{ not json", encoding="utf-8")
    services = placed.services()
    try:
        state = status(placed.folder, placed.workspace, services)
        assert state.refusal == "edit_invalid" and not state.can_retake and state.can_undo
        assert state.lines[-1] == (
            "artifact/spec.json is not valid JSON: see line 1, column 3. Fix it or undo."
        )
        assert state.step == "Fix the file, or undo the change in Architect."
        with pytest.raises(RetakeError, match="^edit_invalid$"):
            asyncio.run(retake(placed.folder, services, yes))
    finally:
        services.close()
    assert record(placed) == []


# ------------------------------------------------------------------ the retake


def test_a_retake_that_passes_writes_the_folder_again_with_the_same_flag(
    placed: Placed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flag = read_flag(placed.folder)
    first = HarnessArtifact.load(placed.folder / "artifact")
    assert flag == flag_name(first.spec.spec_hash)
    placed.reword()
    edited_harness = (placed.folder / "artifact" / "harness.py").read_bytes()
    loaded: list[Path] = []
    real = evaluation.load_harness

    def spy(directory: Path, **kwargs: Any) -> Any:
        loaded.append(Path(directory).resolve())
        return real(directory, **kwargs)

    monkeypatch.setattr(evaluation, "load_harness", spy)
    texts: list[str] = []
    seen: list[tuple[int, int]] = []
    services = placed.services()
    try:
        outcome = asyncio.run(
            retake(placed.folder, services, lambda text: texts.append(text) or True,
                   on_case=lambda done, total: seen.append((done, total)))
        )
    finally:
        services.close()
    assert texts == [
        "Attempt 2, on 4 cases no earlier attempt used. Under $0.001. It stops if it "
        "reaches $10.00, the build's cost cap."
    ]
    assert seen == [(1, 4), (2, 4), (3, 4), (4, 4)]  # a count, never a score
    assert outcome.passed is True and (outcome.attempt, outcome.cases) == (2, "unused")
    assert outcome.lines == (
        "4 of 4. Pass mark 4. Passed. It is your agent.",
        "Attempt 2, on 4 cases no earlier attempt used. Your app's calls work again.",
    )
    # The exam ran the harness Architect wrote, in the workspace, never the folder's.
    assert loaded and not any(path.is_relative_to(placed.folder.resolve()) for path in loaded)
    # In place: the package loads again, under the flag the app already sets.
    artifact = loads(placed)
    assert artifact.spec.task == "Return ok, and nothing else"
    assert artifact.spec.spec_hash != first.spec.spec_hash
    assert read_flag(placed.folder) == flag != flag_name(artifact.spec.spec_hash)
    assert (placed.folder / "artifact" / "harness.py").read_bytes() != edited_harness
    report = artifact.report
    assert report is not None and artifact.licensed
    assert (report.attempt, report.retake_cases) == (2, "unused")
    assert report.verification_id != (first.report.verification_id if first.report else "")
    assert verify_export(placed.folder, registry=None).codes == ()
    assert sorted(path.name for path in placed.folder.parent.iterdir()) == [PACKAGE]
    answer, calls = asyncio.run(call(placed, tmp_path))
    assert answer == "ok" and calls > 0
    assert exam_line(placed.folder) == "passed its exam. 4 of 4, pass mark 4, attempt 2."
    rows = record(placed)
    assert [(row["attempt"], row["cases"], row["result"], row["right"]) for row in rows] == [
        (1, "first_exam", "passed", 4), (2, "unused", "passed", 4)]
    # Undo now means the version that passed last: attempt 2.
    passed = tree_digest(placed.folder)
    placed.reword("Return ok!")
    assert undo(placed.folder, placed.workspace).attempt == 2
    assert tree_digest(placed.folder) == passed


def test_a_retake_that_does_not_pass_leaves_the_folder_stopped(placed: Placed) -> None:
    placed.use_model(MISS)
    before = tree_digest(placed.folder)
    services = placed.services()
    try:
        outcome = asyncio.run(retake(placed.folder, services, yes))
    finally:
        services.close()
    assert outcome.passed is False and (outcome.right, outcome.total, outcome.pass_mark) == (
        0, 4, 4)
    assert outcome.lines[:2] == (
        "0 of 4. Pass mark 4. Did not pass.",
        "Attempt 2, on 4 cases no earlier attempt used. Your agent is still stopped.",
    )
    assert tree_digest(placed.folder) == before
    with pytest.raises(ExportError) as stopped:
        loads(placed)
    assert stopped.value.sentence == STOPPED
    assert [(row["attempt"], row["result"], row["right"]) for row in record(placed)] == [
        (1, "passed", 4), (2, "did_not_pass", 0)]
    assert not list((placed.workspace / "artifacts" / "retake").iterdir())


def test_no_earlier_score_decides_a_retake(placed: Placed) -> None:
    """The rule is the pass mark alone: the same agent with a layout-only change passes on
    its retake cases, and nothing compares it with attempt 1."""
    path = placed.folder / "artifact" / "config.json"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    services = placed.services()
    try:
        state = status(placed.folder, placed.workspace, services)
        assert state.lines[1] == "Changed: artifact/config.json (spacing only, same content)."
        outcome = asyncio.run(retake(placed.folder, services, yes))
    finally:
        services.close()
    assert outcome.passed is True and outcome.attempt == 2


def test_unused_cases_first_then_the_same_exam_and_it_says_so(placed: Placed) -> None:
    """Six extra cases: attempt 2 takes four no attempt used, two are left, fewer than an
    exam, so attempt 3 is the exam of attempt 1 again."""
    texts: list[str] = []

    def ask(text: str) -> bool:
        texts.append(text)
        return True

    services = placed.services()
    try:
        placed.use_model(MISS)
        asyncio.run(retake(placed.folder, services, ask))
        placed.use_model("fake")
        placed.reword()
        outcome = asyncio.run(retake(placed.folder, services, ask))
    finally:
        services.close()
    second, third = record(placed)[1:]
    floor_ids = {f"c{index:02d}" for index in range(8)}
    assert len(second["case_ids"]) == 4 and set(second["case_ids"]) <= {
        f"r{index:02d}" for index in range(6)}
    assert not set(second["case_ids"]) & floor_ids
    assert (third["cases"], third["case_ids"]) == ("same_exam", [])
    assert texts[1] == (
        "Same exam, attempt 3. Fewer than 4 unused cases are left, so the agent retakes the "
        "4 cases of its first exam. Under $0.001. It stops if it reaches $10.00, the "
        "build's cost cap."
    )
    assert outcome.passed and outcome.lines == (
        "4 of 4. Pass mark 4. Passed. It is your agent.",
        "Same exam, attempt 3. Your app's calls work again.",
        "This agent has now been scored on these cases twice, so this score is easier to "
        "reach than a first score.",
    )
    report = loads(placed).report
    assert report is not None and (report.attempt, report.retake_cases) == (3, "same_exam")
    listed = attempts(placed.folder, placed.workspace)
    assert [(item.number, item.cases, item.result, item.right, item.total) for item in listed] == [
        (1, "first_exam", "passed", 4, 4),
        (2, "unused", "did_not_pass", 0, 4),
        (3, "same_exam", "passed", 4, 4),
    ]


def test_an_exam_without_extra_cases_is_retaken_as_the_same_exam(tmp_path: Path) -> None:
    placed = asyncio.run(place(tmp_path, retake_cases=0))
    placed.reword()
    texts: list[str] = []
    services = placed.services()
    try:
        outcome = asyncio.run(
            retake(placed.folder, services, lambda text: texts.append(text) or True)
        )
    finally:
        services.close()
    assert texts[0].startswith(
        "Same exam, attempt 2. This exam has no unused cases, so the agent retakes the 4 "
        "cases of its first exam."
    )
    assert outcome.passed and outcome.cases == "same_exam"
    assert outcome.lines[1] == "Same exam, attempt 2. Your app's calls work again."


def test_saying_no_uses_no_attempt(placed: Placed) -> None:
    placed.reword()
    services = placed.services()
    try:
        with pytest.raises(RetakeError, match="^cancelled$"):
            asyncio.run(retake(placed.folder, services, lambda _text: False))
        assert status(placed.folder, placed.workspace, services).next_attempt == 2
    finally:
        services.close()
    assert record(placed) == []


async def test_cancelling_a_retake_uses_the_attempt_and_its_cases(tmp_path: Path) -> None:
    placed = await place(tmp_path)
    placed.reword()
    before = tree_digest(placed.folder)
    services = placed.services()
    try:
        task: asyncio.Task[Any]

        def cancel_after_first(done: int, _total: int) -> None:
            if done == 1:
                task.cancel()

        task = asyncio.ensure_future(
            retake(placed.folder, services, yes, on_case=cancel_after_first)
        )
        with pytest.raises(asyncio.CancelledError):
            await task
        rows = record(placed)
        assert (rows[-1]["attempt"], rows[-1]["result"], rows[-1]["right"]) == (
            2, "cancelled", None)
        assert len(rows[-1]["case_ids"]) == 4
        assert tree_digest(placed.folder) == before  # still stopped
        state = status(placed.folder, placed.workspace, services)
        assert state.state == "stopped" and state.next_attempt == 3
        # Its four cases are used: two are left, so the next attempt is the same exam.
        outcome = await retake(placed.folder, services, yes)
        assert (outcome.attempt, outcome.cases, outcome.passed) == (3, "same_exam", True)
    finally:
        services.close()
    assert [item.result for item in attempts(placed.folder, placed.workspace)] == [
        "passed", "cancelled", "passed"]


def test_a_retake_stops_at_the_cost_cap(placed: Placed) -> None:
    placed.use_model(MISS)  # each call costs $0.0001
    profile = json.loads(placed.profile.read_text(encoding="utf-8"))
    profile["compile"]["compile_budget"] = {"cost_usd": 0.00025, "compute_s": 600}
    placed.profile.write_text(json.dumps(profile), encoding="utf-8")
    before = tree_digest(placed.folder)
    services = placed.services()
    try:
        outcome = asyncio.run(retake(placed.folder, services, yes))
    finally:
        services.close()
    assert outcome.passed is False and outcome.right is None
    assert outcome.cost_usd <= 0.00025
    assert outcome.lines[0] == (
        "It reached its cost cap of $0.0003 before it finished. Did not pass."
    )
    assert tree_digest(placed.folder) == before
    assert record(placed)[-1]["result"] == "did_not_pass"  # the attempt was used


def test_a_model_the_profile_does_not_hold_is_refused(placed: Placed) -> None:
    placed.use_model("somebody/elses-model")
    services = placed.services()
    try:
        state = status(placed.folder, placed.workspace, services)
        assert state.refusal == "model_not_in_profile" and not state.can_retake
        with pytest.raises(RetakeError, match="^model_not_in_profile$"):
            asyncio.run(retake(placed.folder, services, yes))
    finally:
        services.close()
    assert record(placed) == []


def test_a_plan_that_names_its_model_cannot_be_retaken_on_another(tmp_path: Path) -> None:
    placed = asyncio.run(place(tmp_path, model_target="fake"))
    placed.use_model(MISS)
    services = placed.services()
    try:
        state = status(placed.folder, placed.workspace, services)
        assert state.refusal == "plan_names_model" and state.lines[-1] == (
            "The agent's plan names the model fake, so it cannot be retaken on another."
        )
        placed.use_model("fake")
        placed.reword()
        assert status(placed.folder, placed.workspace, services).can_retake
    finally:
        services.close()


def test_an_unchanged_agent_has_nothing_to_retake(placed: Placed) -> None:
    services = placed.services()
    try:
        with pytest.raises(RetakeError, match="^not_stopped$"):
            asyncio.run(retake(placed.folder, services, yes))
    finally:
        services.close()
    assert record(placed) == []


# ------------------------------------------------------------------ practice


def test_practice_scores_the_change_and_is_not_an_attempt(placed: Placed) -> None:
    placed.use_model(MISS)
    before = tree_digest(placed.folder)
    texts: list[str] = []
    services = placed.services()
    try:
        outcome = asyncio.run(
            practice(placed.folder, services, lambda text: texts.append(text) or True)
        )
        assert status(placed.folder, placed.workspace, services).next_attempt == 2
    finally:
        services.close()
    assert texts == [
        "Practice, on the 4 practice cases, which no exam uses. Not an attempt. Under "
        "$0.001. It stops if it reaches $10.00, the build's cost cap."
    ]
    assert outcome.kind == "practice" and outcome.passed is None and outcome.attempt is None
    assert outcome.lines == ("0 of 4 practice cases right. Not an attempt.",)
    assert tree_digest(placed.folder) == before
    assert record(placed) == [] and not (placed.workspace / "retakes").exists()
    placed.use_model("fake")
    placed.reword()
    services = placed.services()
    try:
        again = asyncio.run(practice(placed.folder, services, yes))
    finally:
        services.close()
    assert again.lines == ("4 of 4 practice cases right. Not an attempt.",)


def test_practice_never_reads_the_exam_or_the_retake_cases(
    placed: Placed, monkeypatch: pytest.MonkeyPatch
) -> None:
    from architect.floors import FloorEvaluator

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("an exam case was asked for")

    monkeypatch.setattr(FloorEvaluator, "_verification_cases", refuse)
    monkeypatch.setattr(FloorEvaluator, "_retake_cases", refuse)
    monkeypatch.setattr(FloorEvaluator, "_retake_pool", refuse)
    placed.reword()
    services = placed.services()
    try:
        outcome = asyncio.run(practice(placed.folder, services, yes))
    finally:
        services.close()
    assert outcome.right == 4


# ------------------------------------------------------------------ which exams


EVIDENCE_FLOOR = (ROOT / "examples" / "receipts" / "floors" / "evidence.json").resolve()
EVIDENCE_MANIFEST = ROOT / "examples" / "receipts" / "manifests" / "evidence.json"


def test_an_exam_on_a_published_dataset_refuses_retakes_by_default(tmp_path: Path) -> None:
    placed = asyncio.run(
        place(tmp_path, floor={"manifest": str(EVIDENCE_MANIFEST), "retakes": None})
    )
    placed.reword()
    services = placed.services()
    try:
        state = status(placed.folder, placed.workspace, services)
        assert not state.can_retake and state.can_undo and state.refusal == "exam_no_retakes"
        assert state.lines == (
            STOPPED, "Changed: artifact/spec.json (task text).",
            "This exam is on a published set of cases, and Architect doesn't retake a "
            "published exam, so a changed agent cannot take it again.",
            "Undo puts back the version that passed.",
        )
        assert state.step == "Undo the change in Architect."
        for run in (retake, practice):
            with pytest.raises(RetakeError, match="^exam_no_retakes$"):
                asyncio.run(run(placed.folder, services, yes))
    finally:
        services.close()
    assert record(placed) == []


def test_a_published_dataset_is_retaken_only_when_the_profile_says_so(tmp_path: Path) -> None:
    placed = asyncio.run(
        place(tmp_path, floor={"manifest": str(EVIDENCE_MANIFEST), "retakes": True})
    )
    placed.reword()
    services = placed.services()
    try:
        assert asyncio.run(retake(placed.folder, services, yes)).passed
    finally:
        services.close()


def _repo_profiles() -> list[Path]:
    """Every JSON file in the repository that names the evidence receipts as a floor."""
    found = []
    for folder in ("examples", "docs", "tests", "desktop", "scripts", "deploy"):
        for path in sorted((ROOT / folder).rglob("*.json")):
            if "node_modules" in path.parts or ".venv" in path.parts:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            floors = payload.get("floors") if isinstance(payload, dict) else None
            if not isinstance(floors, dict):
                continue
            for entry in floors.values():
                target = entry.get("path") if isinstance(entry, dict) else None
                if isinstance(target, str) and (path.parent / target).resolve() == EVIDENCE_FLOOR:
                    found.append(path)
    return found


def test_no_profile_in_the_repository_lets_a_changed_design_take_the_evidence_exam() -> None:
    """docs/bench/prereg-nebius.md: no build of a different design sits the evidence exam.
    Every profile here that names it refuses a retake, the judges' one included."""
    from architect.profiles import FloorConfig

    profiles = _repo_profiles()
    assert ROOT / "examples" / "expense-app" / "architect.json" in profiles
    for path in profiles:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for name, entry in payload["floors"].items():
            if (path.parent / entry["path"]).resolve() != EVIDENCE_FLOOR:
                continue
            config = FloorConfig.model_validate(entry)
            assert not config.retakes_allowed, f"{path}: {name} would allow a retake"
            assert config.retake_cases is None, f"{path}: {name} names retake cases"


def test_the_demo_exam_is_the_one_the_example_app_lets_a_retake_take() -> None:
    from architect.profiles import load_profile

    profile = load_profile(ROOT / "examples" / "expense-app" / "architect.json")
    assert {name for name, config in profile.floors.items() if config.retakes_allowed} == {
        "bench-receipts-demo"}
    assert not profile.floors["bench-receipts-v2-evidence"].retakes_allowed


# ------------------------------------------------------------------ Add to project


def test_remember_passed_notes_where_the_package_was_placed(placed: Placed) -> None:
    placements = json.loads((placed.workspace / "placements.json").read_text(encoding="utf-8"))
    assert Path(placements[PACKAGE]["path"]) == placed.folder.resolve()
    assert tree_digest(placed.workspace / "passed" / PACKAGE) == tree_digest(placed.folder)
    # A copy kept for another folder is not this folder's version.
    other = placed.repo / "elsewhere" / PACKAGE
    other.parent.mkdir()
    retakes.remember_passed(placed.workspace, other, placed.staged)
    placed.reword()
    assert status(placed.folder, placed.workspace).can_undo  # from the staged export
