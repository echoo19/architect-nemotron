"""Golden screens: ASCII only, at most 100 columns, licensed always recomputed from disk."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from architect.compiler import CompileResult
from architect.compiler.verification import _signed
from architect.floors import EvaluationCase
from architect.profiles import Profile
from architect.registry import RegistryEntry
from architect.runtime import RunResult
from architect.session import render
from architect.session.edit import Blocker
from architect.session.state import (
    Draft,
    DraftCase,
    FloorDraft,
    FloorSummary,
    ModelRow,
    Outcome,
    Progress,
    Spend,
    State,
    StatusView,
)
from architect.spec import (
    Budget,
    FloorDefinition,
    FloorKind,
    HarnessArtifact,
    Lifecycle,
    Spec,
    TestReport,
)


def make_spec(**overrides: object) -> Spec:
    fields: dict[str, object] = {
        "task": "Label the sentiment of a short product review.",
        "input_schema": {
            "type": "object",
            "properties": {"review": {"type": "string"}},
            "required": ["review"],
        },
        "output_schema": {
            "type": "object",
            "properties": {"label": {"type": "string"}},
            "required": ["label"],
        },
        "floor": FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.9, dataset="sentiment-v1"),
        "budget": Budget(cost_usd=0.1, compute_s=60),
        "model_target": "fake",
        "lifecycle": Lifecycle.INSTALL,
        "preset_hint": "extractor",
        "provenance": {"task": "inferred", "budget": "user", "model_target": "user"},
    }
    fields.update(overrides)
    return Spec.model_validate(fields)


def make_floor(**overrides: object) -> FloorSummary:
    fields: dict[str, object] = {
        "dataset": "sentiment-v1",
        "kind": FloorKind.HELD_OUT_TASKS,
        "path": Path("floors/sentiment-v1.json"),
        "count": 24,
        "tuning": 12,
        "held_out": 12,
        "synthetic": 0,
        "drafted_by_model": None,
        "misses_allowed": 1,
    }
    fields.update(overrides)
    return FloorSummary(**fields)  # type: ignore[arg-type]


SYNTHETIC = make_floor(synthetic=20, drafted_by_model="fake")


def flat(text: str) -> str:
    """Collapse wrapped continuation lines so a phrase can be checked unwrapped."""
    return " ".join(text.split())


@pytest.fixture
def profile() -> Profile:
    return Profile.model_validate(
        {
            "models": {"fake": {"provider": "fake"}},
            "floors": {"tasks": {"kind": "held_out_tasks", "path": "cases.json"}},
            "compile": {"max_candidates": 2, "compile_budget": {"cost_usd": 5, "compute_s": 1800}},
        }
    )


def licensed_artifact(tmp_path: Path, spec: Spec) -> HarnessArtifact:
    directory = tmp_path / "art"
    directory.mkdir()
    (directory / "harness.py").write_text("async def run(context, task):\n    return task\n")
    artifact = HarnessArtifact(
        spec=spec,
        version=2,
        directory=directory,
        preset="extractor",
        model="fake",
        components=("self_check",),
    )
    report = _signed(
        TestReport(
            spec_hash=spec.spec_hash,
            artifact_digest=artifact.artifact_digest,
            passed=True,
            floor_score=1.0,
            floor_limit=0.9,
            floor_kind=FloorKind.HELD_OUT_TASKS,
            tokens=1102,
            cost_usd=0.0021,
            compute_s=4.1,
            component_count=1,
            verification_id="7b1e3c2a" + "0" * 24,
            held_out_count=12,
            mean_cost_usd=0.0017,
        )
    )
    artifact = artifact.model_copy(update={"report": report})
    artifact.save()
    return artifact


def test_money_and_duration_edge_cases() -> None:
    assert render.money(0.0017) == "$0.0017"
    assert render.money(0.00004) == "$0.0000"
    assert render.money(0.0) == "$0.0000"
    assert render.money(0.99994) == "$0.9999"
    assert render.money(1.34) == "$1.34"
    assert render.money(1.0) == "$1.00"
    assert render.money(12345.678) == "$12345.68"
    assert render.money(1e-9) == "$0.0000"
    assert "e" not in render.money(1e-9) and "e" not in render.money(1e9)
    assert render.duration(18.6) == "18.6s"
    assert render.duration(0) == "0.0s"
    assert render.duration(62) == "1m02s"
    assert render.duration(690) == "11m30s"
    assert render.duration(3780) == "1h03m"
    assert render.hash8("3fa9c2d1" + "f" * 56) == "3fa9c2d1"
    assert render.clock(0) == "[00:00]"
    assert render.clock(690.7) == "[11:30]"
    assert render.clock(6000) == "[100:00]"


def test_wrap_keeps_indent_and_width() -> None:
    long = "  " + "word " * 40
    wrapped = render.wrap(long.rstrip())
    lines = wrapped.split("\n")
    assert len(lines) > 1
    assert all(len(line) <= 100 for line in lines)
    assert lines[1].startswith("    word")
    assert render.wrap("short\n\nlines") == "short\n\nlines"
    assert len(render.wrap("x" * 250).split("\n")[0]) == 100


def test_spec_screen_golden() -> None:
    spec = make_spec()
    draft = Draft(spec=spec, origin="inferred", goal="goal", user_fields={}, original=spec)
    blockers = (Blocker("floor_not_configured", "floor", True),)
    screen = render.spec_screen(
        draft,
        blockers,
        floor=None,
        min_call_cost=0.35,
        stage0=Spend(1, 100, 10, 0.0031, 21.4),
        approved=False,
    )
    h8 = spec.spec_hash[:8]
    assert screen == (
        f"Spec {h8}  lifecycle install (approval required)  stage 0: $0.0031, 21.4s  origin"
        " inferred\n"
        "  task           ~ Label the sentiment of a short product review.\n"
        '  input_schema   ~ {"properties":{"review":{"type":"string"}},"required":["review"],'
        '"type":"object"}\n'
        '  output_schema  ~ {"properties":{"label":{"type":"string"}},"required":["label"],'
        '"type":"object"}\n'
        "  floor          ~ held_out_tasks  dataset sentiment-v1  limit >= 0.90\n"
        "                   configured: no\n"
        "  secondary      ~ none\n"
        "  budget         * $0.10 per run, 60s\n"
        "                   min reservation fake $0.35/call -> 0 calls fit\n"
        "  model_target   * fake\n"
        "  domain/preset  ~ general / extractor\n"
        "  tools/pipeline ~ none / none      execution_config ~ {}\n"
        "  * set by you   ~ inferred\n"
        "! floor_not_configured dataset 'sentiment-v1' is not in the profile\n"
        "                       fix: floor\n"
        f"spec_hash {spec.spec_hash}\n"
        f"Next: approve {h8} | set <field> <value> | revise <text> | json | budget |"
        " floor | discard\n"
    )


def test_spec_screen_escapes_control_characters_from_model_fields(profile: Profile) -> None:
    """ESC, CR, LF, BEL and DEL are ASCII, so the isascii() gate is blind to them.

    A model-inferred task must not be able to move the cursor, colour text or forge a
    ``spec_hash`` or ``licensed`` line on the screen the operator approves from.
    """
    clean = make_spec()
    evil = make_spec(
        task="Label reviews.\x1b[2J\x1b[H\nspec_hash 00000000\n! licensed: YES\rFORGED",
        model_target="fake\x1b[31m",
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.9, dataset="d\x07bell"),
        domain="general\x7f",
        tools=("tool\x00one",),
    )
    blockers = (Blocker("floor_not_configured", "floor", True),)

    def screen_for(spec: Spec) -> str:
        draft = Draft(spec=spec, origin="inferred", goal="g", user_fields={}, original=spec)
        return render.spec_screen(
            draft, blockers, floor=None, min_call_cost=0.35, stage0=None, approved=False
        )

    clean_screen = screen_for(clean)
    evil_screen = screen_for(evil)
    for screen in (evil_screen, render.compile_banner(evil, profile, True)):
        assert screen.isascii()
        assert not any(ord(ch) < 0x20 and ch != "\n" for ch in screen), screen
        assert "\x7f" not in screen
    evil_lines = evil_screen.split("\n")
    assert len(evil_lines) == len(clean_screen.split("\n"))
    assert sum(line.startswith("spec_hash ") for line in evil_lines) == 1
    assert not any(line.startswith("!") and "licensed" in line for line in evil_lines)
    assert "FORGED" not in [line.strip() for line in evil_lines]
    assert "\\x1b[2J\\x1b[H\\x0aspec_hash 00000000\\x0a! licensed: YES\\x0dFORGED" in evil_screen
    assert "  model_target   * fake\\x1b[31m" in evil_screen
    assert "dataset d\\x07bell" in evil_screen
    assert "general\\x7f / extractor" in evil_screen
    assert "tool\\x00one" in evil_screen
    hinted = render.failure("no_goal", "x\x1b[31m\r\n")
    assert "\x1b" not in hinted and "\r" not in hinted
    assert hinted.count("\n") == 3


def test_spec_screen_configured_approved_and_expire() -> None:
    spec = make_spec(budget=Budget(cost_usd=0.7, compute_s=60))
    draft = Draft(spec=spec, origin="file", goal=None, user_fields={}, original=spec)
    screen = render.spec_screen(
        draft, (), floor=SYNTHETIC, min_call_cost=0.35, stage0=None, approved=True
    )
    lines = screen.split("\n")
    assert lines[0] == f"Spec {draft.hash8}  lifecycle install  origin file  approved"
    assert (
        "                   configured: yes, 24 cases -> 12 tuning / 12 held-out (1 miss allowed)"
        in lines
    )
    assert "[SYNTHETIC: 20 of 24 cases model-drafted by fake" in screen
    assert "accepted by operator; not production evidence]" in flat(screen)
    assert "                   min reservation fake $0.35/call -> 2 calls fit" in lines
    assert f"Next: approve {draft.hash8} | set <field> <value>" in screen
    budget_blocker = (Blocker("budget_below_quote", "set budget.cost_usd 0.70", True),)
    low = render.spec_screen(
        Draft(spec=make_spec(), origin="inferred", goal="g", user_fields={}, original=make_spec()),
        budget_blocker,
        floor=None,
        min_call_cost=0.35,
        stage0=None,
        approved=False,
    )
    assert "! budget_below_quote   $0.10 is below the $0.35 minimum reservation for fake;" in low
    assert "fix: set budget.cost_usd 0.70" in low
    # Fixed text (Section 8.1): the Next: line keeps the approve token even with blockers.
    assert "Next: approve " in low and "fix the blockers" not in low
    expire = make_spec(lifecycle=Lifecycle.EXPIRE)
    draft = Draft(spec=expire, origin="registry", goal=None, user_fields={}, original=expire)
    screen = render.spec_screen(
        draft, (), floor=None, min_call_cost=None, stage0=None, approved=False
    )
    assert screen.startswith(
        f"Spec {draft.hash8}  lifecycle expire (single use, no approval needed)  origin registry\n"
    )
    assert "Next: compile | set <field> <value>" in screen
    assert "min reservation" not in screen


def test_long_schema_prints_indented_on_following_lines() -> None:
    spec = make_spec(
        output_schema={
            "type": "object",
            "properties": {
                "label": {"enum": ["negative", "neutral", "positive"], "description": "x" * 40}
            },
        }
    )
    draft = Draft(spec=spec, origin="inferred", goal="g", user_fields={}, original=spec)
    screen = render.spec_screen(
        draft, (), floor=None, min_call_cost=None, stage0=None, approved=False
    )
    assert "  output_schema  ~\n                   {\n" in screen
    assert '                     "properties": {' in screen


def test_spec_json_is_the_saved_bytes() -> None:
    spec = make_spec()
    assert render.spec_json(spec) == spec.model_dump_json(indent=2)
    assert Spec.model_validate_json(render.spec_json(spec)).spec_hash == spec.spec_hash


def test_budget_screen_golden(profile: Profile) -> None:
    screen = render.budget_screen(make_spec(), profile, 0.35, make_floor())
    assert screen == (
        "budget for fake\n"
        "  min reservation per call   $0.35\n"
        "  per-run budget for 2 calls $0.70   set budget.cost_usd 0.70\n"
        "  per-run budget for 3 calls $1.05   set budget.cost_usd 1.05\n"
        "  per run now                $0.10 / 60s\n"
        "  compile budget             $5.00 / 30m   budget compile <usd> <seconds>\n"
        "  compile reservation        up to $12.60 = (2 candidates x 12 tuning + 12 held-out)"
        " x $0.35\n"
        "  note: measured live cost is typically about 1% of the reservation\n"
    )
    unknown = render.budget_screen(make_spec(model_target="gpt-9"), profile, None, None)
    assert "  min reservation per call   unknown (model not in profile)\n" in unknown
    assert "compile reservation" not in unknown


def test_diff_screen_and_progress_and_banner(profile: Profile) -> None:
    assert render.diff_screen(["- budget.cost_usd 0.1", "+ budget.cost_usd 0.7"], "3fa9c2d1") == (
        "- budget.cost_usd 0.1\n+ budget.cost_usd 0.7\nspec is now 3fa9c2d1\n"
    )
    assert render.diff_screen([], "3fa9c2d1") == "no changes\nspec is now 3fa9c2d1\n"
    progress = Progress(
        elapsed_s=19.2,
        stage=2,
        stage_text="stage 2 tuning",
        calls=1,
        in_flight=1,
        case_rows=1,
        quality_rows=0,
        rewrite_rows=0,
        accepted_rows=0,
        spend=Spend(1, 1204, 9, 0.0018, 18.6),
        budget=Budget(cost_usd=5, compute_s=1800),
        left_usd=4.9982,
        left_s=1781,
    )
    assert render.progress_line(progress) == (
        "[00:19] stage 2 tuning        calls 1 (1 running)  spent $0.0018  left $5.00 / 29m41s\n"
    )
    failed = dataclasses.replace(progress, calls=3, in_flight=0, failed=1)
    assert render.progress_line(failed) == (
        "[00:19] stage 2 tuning        calls 3 (0 running, 1 failed)  spent $0.0018  "
        "left $5.00 / 29m41s\n"
    )
    scored = Progress(
        elapsed_s=231,
        stage=2,
        stage_text="stage 2 tuning",
        calls=14,
        in_flight=0,
        case_rows=12,
        quality_rows=1,
        rewrite_rows=0,
        accepted_rows=1,
        spend=Spend(14, 0, 0, 0.021, 200),
        budget=Budget(cost_usd=5, compute_s=1800),
        left_usd=4.979,
        left_s=1569,
    )
    assert flat(render.progress_line(scored)) == (
        "[03:51] stage 2 tuning calls 14 (0 running) spent $0.0210 left $4.98 / 26m09s"
        " rows: quality 1, accepted 1, rewrite 0"
    )
    spec = make_spec()
    assert render.compile_banner(spec, profile, True) == (
        f"compiling {spec.spec_hash[:8]}  model fake  compile budget $5.00 / 30m\n"
        "approved by operator  (cancel or Ctrl-C to stop)\n"
    )
    assert "lifecycle expire, no approval needed" in render.compile_banner(spec, profile, False)


def test_result_screen_licensed_golden_and_recomputed_from_disk(tmp_path: Path) -> None:
    spec = make_spec()
    artifact = licensed_artifact(tmp_path, spec)
    assert artifact.report is not None
    result = CompileResult(spec=spec, artifact=artifact, report=artifact.report, stages=(1, 2, 4))
    outcome = Outcome(result=result, approved_hash=spec.spec_hash)
    assert outcome.licensed
    screen = render.result_screen(outcome, make_floor(), spec)
    lines = screen.split("\n")
    assert lines[:5] == [
        "Verification (stage 4)  id 7b1e3c2a  stages 1,2,4  licensed: YES (recomputed from disk)",
        "  floor      held_out_tasks  score 1.00 >= limit 0.90   held-out cases 12",
        "  per run    max $0.0021  max 4.1s  max tokens 1,102   mean $0.0017",
        "  budget     $0.10 / 60s -> fits",
        "  failures   none",
    ]
    # The artifact line carries a temp path and may wrap; compare it unwrapped.
    artifact_line = " ".join(" ".join(lines[5:-2]).split())
    assert artifact_line == (
        f"artifact {artifact.directory} version 2 preset extractor model fake components "
        "self_check warm start: no"
    )
    assert lines[-2] == "Next: install <name> | run {json} | report | again | new"
    assert lines[-1] == ""
    with_label = render.result_screen(outcome, SYNTHETIC, spec)
    assert "[SYNTHETIC: 20 of 24 cases model-drafted by fake" in with_label
    # Tampering with the report on disk is seen at the next render; nothing is cached.
    report_path = artifact.directory / "report.json"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["floor_score"] = 1.0
    payload["passed"] = True
    payload["signature"] = "0" * 64
    report_path.write_text(json.dumps(payload), encoding="utf-8")
    assert not outcome.licensed
    tampered = render.result_screen(outcome, None, spec)
    assert "licensed: NO (recomputed from disk)" in tampered
    assert "Next: again (edit and recompile) | report | new" in tampered


def test_result_screen_expire_and_unlicensed(tmp_path: Path) -> None:
    spec = make_spec(lifecycle=Lifecycle.EXPIRE)
    artifact = licensed_artifact(tmp_path, spec)
    outcome = Outcome(
        CompileResult(spec=spec, artifact=artifact, report=artifact.report, stages=(1, 2, 4)), None
    )
    assert "Next: run {json} (single use) | again | new" in render.result_screen(
        outcome, None, spec
    )
    assert artifact.report is not None
    failed_report = artifact.report.model_copy(
        update={"passed": False, "floor_score": 0.83, "failures": ("primary_floor_failed",)}
    )
    failed = CompileResult(
        spec=spec,
        report=failed_report,
        stages=(1, 2, 4),
        failures=("primary_floor_failed", "compile_budget", "floor_unavailable"),
    )
    screen = render.result_screen(Outcome(failed, None), None, spec)
    lines = screen.split("\n")
    assert lines[0] == (
        "Verification (stage 4)  id 7b1e3c2a  stages 1,2,4  licensed: NO (recomputed from disk)"
    )
    assert lines[1] == "  floor      held_out_tasks  score 0.83 < limit 0.90   held-out cases 12"
    assert "  failures" in lines
    assert "  primary_floor_failed   The held-out floor score is below the Spec's limit." in screen
    assert (
        "score 0.83 < 0.90 next: fix wrong expected values (floor), lower the limit "
        "(set floor.limit 0.8), or revise the task" in flat(screen)
    )
    assert "  compile_budget         The whole-compile wallet ran out before verification" in screen
    assert "next: budget compile 10 3600" in flat(screen)
    assert (
        "floor_unavailable The floor dataset could not be loaded for this Spec. next: floor"
        in flat(screen)
    )
    assert "Next: again (edit and recompile) | report | new" in screen
    no_report = render.result_screen(
        Outcome(CompileResult(spec=spec, stages=(1,), failures=("selection_failed",)), None),
        None,
        spec,
    )
    assert no_report.startswith("Verification (stage 4)  id -  stages 1  licensed: NO")
    assert "  floor      not verified (no report)" in no_report
    assert "selection_failed" in no_report
    bad_code = CompileResult(spec=spec, stages=(1,), failures=("Model said: sk-secret",))
    leaked = render.result_screen(Outcome(bad_code, None), None, spec)
    assert "sk-secret" not in leaked and "unknown_failure" in leaked


def test_run_screen_golden() -> None:
    run = RunResult(
        run_id="9e0d41f2-1111-2222",
        output={"label": "positive"},
        tokens_in=1190,
        tokens_out=7,
        cost_usd=0.0019,
        wall_time_s=3.8,
    )
    assert render.run_screen(run, "fake", expired_after=True) == (
        "run 9e0d41f2  fake  $0.0019  3.8s  1,190 in / 7 out\n"
        "{\n"
        '  "label": "positive"\n'
        "}\n"
        "artifact consumed (single use)\n"
    )
    plain = render.run_screen(
        run.model_copy(update={"output": "café"}), "fake", expired_after=False
    )
    assert plain == 'run 9e0d41f2  fake  $0.0019  3.8s  1,190 in / 7 out\n"caf\\u00e9"\n'


def test_registry_and_entry_screens(tmp_path: Path) -> None:
    spec = make_spec()
    artifact = licensed_artifact(tmp_path, spec)
    entry = RegistryEntry(
        name="demo", revision=1, spec_hash=spec.spec_hash, artifact_version=2, directory=Path("art")
    )
    h8 = spec.spec_hash[:8]
    assert render.registry_screen(
        [(entry, True), (entry.model_copy(update={"name": "b", "revision": 12}), False)]
    ) == (
        "name            rev  spec      ver  licensed  directory\n"
        f"demo              1  {h8}    2  yes       art\n"
        f"b                12  {h8}    2  no        art\n"
    )
    assert render.registry_screen([]) == "registry: no installed artifacts\n"
    screen = render.entry_screen(entry, artifact, SYNTHETIC)
    lines = screen.split("\n")
    assert lines[0] == f"demo  revision 1  spec {h8}  version 2  licensed yes"
    assert lines[1] == "  directory  art"
    assert lines[2] == "  model      fake  preset extractor  components self_check"
    assert lines[3] == "  lifecycle  install  budget $0.10 / 60s"
    assert lines[4] == (
        "  evidence   held_out_tasks  score 1.00 >= limit 0.90  held-out cases 12  id 7b1e3c2a"
    )
    assert "[SYNTHETIC: 20 of 24 cases model-drafted by fake" in screen
    unlicensed = artifact.model_copy(update={"report": None})
    assert "licensed no" in render.entry_screen(entry, unlicensed, None)
    assert "  evidence   none on disk" in render.entry_screen(entry, unlicensed, None)


def test_entry_and_result_screens_escape_control_bytes_in_ids_from_disk(tmp_path: Path) -> None:
    """A report.json whose verification_id starts with ESC must not reach the terminal raw.

    ``TestReport.verification_id`` is only ``Nonempty`` and ``HarnessArtifact.load``
    returns it as written, so the id column is file content like every other field.
    """
    spec = make_spec()
    artifact = licensed_artifact(tmp_path, spec)
    assert artifact.report is not None
    hostile = _signed(artifact.report.model_copy(update={"verification_id": "\x1b[31mRED\x07"}))
    artifact.model_copy(update={"report": hostile}).save()
    loaded = HarnessArtifact.load(artifact.directory)
    assert loaded.report is not None
    assert loaded.report.verification_id.startswith("\x1b")
    entry = RegistryEntry(
        name="demo", revision=1, spec_hash=spec.spec_hash, artifact_version=2, directory=Path("art")
    )
    screen = render.entry_screen(entry, loaded, None)
    assert not any(ord(ch) < 0x20 and ch != "\n" for ch in screen)
    assert "\x7f" not in screen
    assert "id \\x1b[31mRED" in screen
    outcome = Outcome(
        result=CompileResult(spec=spec, artifact=loaded, report=loaded.report, stages=(1, 2, 4)),
        approved_hash=spec.spec_hash,
    )
    result = render.result_screen(outcome, None, spec)
    assert not any(ord(ch) < 0x20 and ch != "\n" for ch in result)
    assert "id \\x1b[31mRED" in result
    run = RunResult(
        run_id="\x1b[2Jcleared", output={}, tokens_in=1, tokens_out=1, cost_usd=0.0, wall_time_s=0.1
    )
    run_text = render.run_screen(run, "fake", expired_after=False)
    assert not any(ord(ch) < 0x20 and ch != "\n" for ch in run_text)
    assert run_text.startswith("run \\x1b[2Jclea  fake")


def test_floor_screens() -> None:
    def case(case_id: str, **metadata: object) -> EvaluationCase:
        return EvaluationCase(
            case_id=case_id, task={"review": "x"}, expected="positive", metadata=dict(metadata)
        )  # type: ignore[arg-type]

    draft = FloorDraft(
        dataset="sv1",
        kind=FloorKind.HELD_OUT_TASKS,
        cases=[
            DraftCase(case("c1"), "operator", True),
            DraftCase(case("c2"), "file", False),
            DraftCase(case("c3"), "model_draft", False),
        ],
        drafted_by_model="fake",
        return_state=State.REVIEW,
    )
    # Every case is shown with its content (the operator reviews these labels); the
    # unreviewed drafted case comes first and the screen ends with the next step.
    assert render.floor_screen(draft, ["1 model-drafted cases are unreviewed"]) == (
        "floor sv1  held_out_tasks  cases 3 (operator 1, file 1, model_draft 1)  unreviewed 1"
        "  split 2 / 1\n"
        '  c3  [draft ?]  task: {"review":"x"}\n'
        '    expected: "positive"\n'
        '  c1  [op]  task: {"review":"x"}\n'
        '    expected: "positive"\n'
        '  c2  [file]  task: {"review":"x"}\n'
        '    expected: "positive"\n'
        "  1 held-out cases; small floors license lucky harnesses\n"
        "blocked: 1 model-drafted cases are unreviewed\n"
        "Next: review c3 ok|expected <json>|drop | accept (after review) | back\n"
    )
    one_case = FloorDraft(
        dataset="sv1",
        kind=FloorKind.HELD_OUT_TASKS,
        cases=[DraftCase(case("c1"), "operator", True)],
        drafted_by_model=None,
        return_state=State.READY,
    )
    assert render.floor_screen(one_case, ["below minimum: 1 cases, need 2"]).endswith(
        "blocked: below minimum: 1 cases, need 2\n"
        'Next: add {"task":..., "expected":...} | paste | draft 20 | back\n'
    )
    # With the Spec's limit the exact Section 7.6 line is printed: M = floor(H * (1 - L)).
    with_limit = render.floor_screen(draft, [], limit=0.9)
    assert (
        "  1 held-out cases at limit 0.90 allow at most 0 misses; small floors license lucky"
        " harnesses\n" in with_limit
    )
    assert render.small_floor_warning(10, 0.8) == (
        "10 held-out cases at limit 0.80 allow at most 2 misses; small floors license lucky"
        " harnesses"
    )
    assert render.small_floor_warning(12, 0.9).startswith(
        "12 held-out cases at limit 0.90 allow at most 1 misses;"
    )
    draft.cases[2].reviewed = True
    draft.cases.append(DraftCase(case("c4", corrected=True), "model_draft", True))
    draft.cases.extend(DraftCase(case(f"c{n}"), "operator", True) for n in range(5, 25))
    screen = render.floor_screen(draft, [])
    assert (
        '  c3  [draft ok]  task: {"review":"x"}\n    expected: "positive"\n'
        '  c4  [draft fixed]  task: {"review":"x"}\n    expected: "positive"\n'
    ) in screen
    assert "  ... 12 more; show <id> for one case\n" in screen
    assert screen.count("  task: ") == 12 and screen.count("    expected: ") == 12
    assert screen.endswith("ready: type accept\nNext: accept | back\n")
    assert "small floors" not in screen
    assert flat(screen).startswith(
        "floor sv1 held_out_tasks cases 24 (operator 21, file 1, "
        'model_draft 2) unreviewed 0 split 12 / 12 c1 [op] task: {"review":"x"}'
    )
    assert render.floors_screen([]) == "no floors configured\n"
    empty = FloorDraft(
        dataset="sv1", kind=FloorKind.HELD_OUT_TASKS, cases=[], drafted_by_model=None,
        return_state=State.READY,
    )
    assert render.floor_screen(empty, ["below minimum: 0 cases, need 2"]) == (
        "floor sv1  held_out_tasks  cases 0 (operator 0, file 0, model_draft 0)  unreviewed 0"
        "  split 0 / 0\n"
        "blocked: below minimum: 0 cases, need 2\n"
        'Next: use <file> | add {"task":..., "expected":...} | paste | draft 20\n'
    )


def test_floor_case_lines_are_clipped_and_escaped() -> None:
    long_task = {"review": "\x1b[2J" + "a" * 200, "caf\N{LATIN SMALL LETTER E WITH ACUTE}": True}
    item = DraftCase(
        EvaluationCase(case_id="c1", task=long_task, expected=["b" * 120]),  # type: ignore[arg-type]
        "model_draft",
        False,
    )
    draft = FloorDraft(
        dataset="sv1", kind=FloorKind.HELD_OUT_TASKS, cases=[item], drafted_by_model="fake",
        return_state=State.READY,
    )
    screen = render.floor_screen(draft, ["1 model-drafted cases are unreviewed"])
    lines = screen.split("\n")
    task_line, expected_line = lines[1], lines[2]
    assert task_line.startswith('  c1  [draft ?]  task: {"caf\\u00e9":true,"review":"\\u001b[2Jaaa')
    assert task_line.endswith("...") and len(task_line) == 100
    assert expected_line.startswith('    expected: ["bbb') and expected_line.endswith("...")
    assert len(expected_line) == 100
    assert screen.isascii() and not any(ord(ch) < 0x20 and ch != "\n" for ch in screen)
    assert all(len(line) <= 100 for line in lines)
    # A short value is never clipped and carries no "...".
    short = render.floor_screen(
        FloorDraft(
            dataset="sv1",
            kind=FloorKind.HELD_OUT_TASKS,
            cases=[DraftCase(EvaluationCase(case_id="c2", task="t", expected=1), "operator", True)],
            drafted_by_model=None,
            return_state=State.READY,
        ),
        [],
    )
    assert '  c2  [op]  task: "t"\n    expected: 1\n' in short and "..." not in short


def test_case_screen_and_review_lines() -> None:
    task = {"review": "great", "meta": {"lang": "en"}}
    item = DraftCase(
        EvaluationCase(case_id="c1", task=task, expected={"label": "pos"}),  # type: ignore[arg-type]
        "model_draft",
        False,
    )
    assert render.case_screen(item) == (
        "c1  [draft ?]\n"
        "task:\n"
        "{\n"
        '  "meta": {\n'
        '    "lang": "en"\n'
        "  },\n"
        '  "review": "great"\n'
        "}\n"
        "expected:\n"
        "{\n"
        '  "label": "pos"\n'
        "}\n"
    )
    hostile = DraftCase(
        EvaluationCase(
            case_id="\x1b[2Jc2",
            task="caf\N{LATIN SMALL LETTER E WITH ACUTE}\r\n",
            expected="x" * 150,
        ),
        "operator",
        True,
    )
    hostile_screen = render.case_screen(hostile)
    assert hostile_screen.startswith("\\x1b[2Jc2  [op]\ntask:\n\"caf\\u00e9\\r\\n\"\nexpected:\n")
    assert hostile_screen.isascii() and all(len(line) <= 100 for line in hostile_screen.split("\n"))
    assert "\r" not in hostile_screen and "\x1b" not in hostile_screen
    # reviewed_line: ok carries the clipped task, expected the new label, drop only the id.
    assert render.reviewed_line("c1", "ok", item) == (
        'reviewed c1 ok  task: {"meta":{"lang":"en"},"review":"great"}'
    )
    assert render.reviewed_line("c1", "expected", item) == 'reviewed c1 expected -> {"label":"pos"}'
    assert render.reviewed_line("c1", "drop", None) == "dropped c1"
    wide = DraftCase(
        EvaluationCase(case_id="c9", task="w" * 200, expected=0), "model_draft", True
    )
    ok_line = render.reviewed_line("c9", "ok", wide)
    assert ok_line.startswith('reviewed c9 ok  task: "www') and ok_line.endswith("...")
    assert len(ok_line) == 100
    # review_screen: the count, then the next id, the blockers, or the ready line.
    second = DraftCase(EvaluationCase(case_id="c3", task="t", expected=1), "model_draft", False)
    draft = FloorDraft(
        dataset="sv1", kind=FloorKind.HELD_OUT_TASKS, cases=[item, second],
        drafted_by_model="fake", return_state=State.REVIEW,
    )
    item.reviewed = True
    pending = ["1 model-drafted cases are unreviewed"]
    assert render.review_screen(draft, pending, "reviewed c1 ok") == (
        "reviewed c1 ok\nunreviewed: 1\nnext: review c3 ok|expected <json>|drop\n"
    )
    second.reviewed = True
    assert render.review_screen(draft, [], "reviewed c3 ok") == (
        "reviewed c3 ok\nunreviewed: 0\nready: type accept\n"
    )
    draft.cases.remove(second)
    reasons = ["below minimum: 1 cases, need 2", "held-out partition would have 0 cases"]
    assert render.review_screen(draft, reasons, "dropped c3") == (
        "dropped c3\nunreviewed: 0\nblocked: below minimum: 1 cases, need 2\n"
        "blocked: held-out partition would have 0 cases\n"
    )
    listed = render.floors_screen([make_floor(), SYNTHETIC])
    assert listed.startswith(
        "sentiment-v1  held_out_tasks  24 cases -> 12 tuning / 12 held-out  "
        f"{Path('floors/sentiment-v1.json')}\n"
    )
    assert listed.count("[SYNTHETIC:") == 1


def test_status_screen_lists_every_field(tmp_path: Path) -> None:
    spec = make_spec()
    draft = Draft(spec=spec, origin="inferred", goal="g", user_fields={}, original=spec, edits=2)
    entry = RegistryEntry(
        name="demo", revision=3, spec_hash=spec.spec_hash, artifact_version=1, directory=Path("art")
    )
    view = StatusView(
        state=State.REVIEW,
        workspace=Path("w"),
        profile_path=Path("w") / "p.json",
        model="fake",
        inference_model=None,
        floors=(make_floor(),),
        compile_budget=Budget(cost_usd=5, compute_s=1800),
        draft=draft,
        outcome=None,
        last_entry=entry,
        session_spend=Spend(1, 2, 3, 0.0031, 24),
        compile_spend=None,
        signing_key=Path("w") / "signing.key",
    )
    assert render.status_screen(view) == (
        "state            review\n"
        "workspace        w\n"
        f"profile_path     {Path('w') / 'p.json'}\n"
        "model            fake\n"
        "inference_model  none\n"
        "floors           sentiment-v1\n"
        "compile_budget   $5.00 / 30m\n"
        f"draft            {draft.hash8}  origin inferred  edits 2\n"
        "outcome          none\n"
        "last_entry       demo revision 3\n"
        "session_spend    $0.0031  calls 1  2 in / 3 out  wall 0m24s\n"
        "compile_spend    -\n"
        f"signing_key      {Path('w') / 'signing.key'}\n"
    )


def test_model_menu_and_profile_preview() -> None:
    rows = [
        ModelRow(1, "haiku", "claude-code", "c", 0.35, True, 0.01, True),
        ModelRow(2, "sonnet", "claude-code", "c", 1.34, False, 0.02, True),
        ModelRow(3, "terra", "codex", "x", 1.58, False, 0.02, True),
    ]
    screen = render.model_menu(rows)
    lines = screen.split("\n")
    assert lines[0] == "Choose a model for this session:"
    assert lines[1] == (
        "  1) haiku   claude-code  min reservation/call $0.35  can infer Specs: yes   (default)"
    )
    assert (
        "  2) sonnet  claude-code  min reservation/call $1.34  can infer Specs: no (Stage 0"
        in screen
    )
    assert "can infer Specs: no (Stage 0 will use claude-haiku-4-5-20251001)" in flat(screen)
    assert screen.endswith("  q) quit\n")
    codex_only = render.model_menu(rows[2:])
    assert "can infer Specs: no (describe unavailable; use spec <file>)" in flat(codex_only)
    assert "(default)" in codex_only


def test_profile_summary_is_four_lines_not_json() -> None:
    claude = {"provider": "claude-code", "executable": "/opt/bin/claude"}
    profile = Profile.model_validate(
        {
            "workspace": ".",
            "models": {"claude-sonnet-5": claude, "claude-haiku-4-5-20251001": claude},
            "inference": {**claude, "model": "claude-haiku-4-5-20251001"},
            "compile": {
                "model_ladder": ["claude-sonnet-5"],
                "max_candidates": 2,
                "enable_rewriting": False,
                "compile_budget": {"cost_usd": 5, "compute_s": 1800},
            },
        }
    )
    path = Path("w") / "session-profile.json"
    assert render.profile_summary(profile, path) == (
        "model claude-sonnet-5 via claude-code (claude)\n"
        "inference model claude-haiku-4-5-20251001\n"
        "compile budget $5.00 / 30m  candidates 2  rewriting off\n"
        f"full profile will be written to {path}\n"
    )
    codex = Profile.model_validate(
        {
            "models": {"gpt-5.6-terra": {"provider": "codex", "executable": "/opt/bin/codex"}},
            "compile": {"model_ladder": ["gpt-5.6-terra"], "enable_rewriting": True},
        }
    )
    summary = render.profile_summary(codex, path)
    assert summary.startswith("model gpt-5.6-terra via codex (codex)\ninference model none\n")
    assert "  rewriting on\n" in summary and "{" not in summary


def test_footer_golden() -> None:
    assert render.footer(Spend(14, 0, 0, 0.0431, 192), Spend(cost_usd=0.0089)) == (
        "spend  session $0.0431 | this compile $0.0089 | calls 14 | wall 3m12s | "
        "API-equivalent, not a bill\n"
    )
    assert render.footer(Spend(1, 0, 0, 0.0031, 24), None) == (
        "spend  session $0.0031 | this compile - | calls 1 | wall 0m24s | "
        "API-equivalent, not a bill\n"
    )


def test_every_screen_is_ascii_and_at_most_100_columns(tmp_path: Path, profile: Profile) -> None:
    spec = make_spec(
        task="café ✓ " + "long " * 30,
        model_target="fake",
        tools=("über",),
        execution_config={"kéy": "v" * 120},
    )
    artifact = licensed_artifact(tmp_path, spec)
    draft = Draft(spec=spec, origin="inferred", goal="g", user_fields={}, original=spec)
    outcome = Outcome(
        CompileResult(spec=spec, artifact=artifact, report=artifact.report, stages=(1, 2, 4)), None
    )
    entry = RegistryEntry(
        name="démo",
        revision=1,
        spec_hash=spec.spec_hash,
        artifact_version=2,
        directory=artifact.directory,
    )
    run = RunResult(
        run_id="r",
        output={"téxt": "✓" * 150},
        tokens_in=1,
        tokens_out=1,
        cost_usd=0.0,
        wall_time_s=0.0,
    )
    floor = make_floor(dataset="sént", drafted_by_model="mödel", synthetic=3)
    floor_draft = FloorDraft(
        dataset="sént",
        kind=FloorKind.HELD_OUT_TASKS,
        cases=[DraftCase(EvaluationCase(case_id="é-1", task={}, expected=1), "operator", True)],
        drafted_by_model=None,
        return_state=State.READY,
    )
    view = StatusView(
        state=State.RESULT,
        workspace=Path("wé"),
        profile_path=None,
        model="fake",
        inference_model="fake",
        floors=(floor,),
        compile_budget=Budget(),
        draft=draft,
        outcome=outcome,
        last_entry=entry,
        session_spend=Spend(),
        compile_spend=Spend(),
        signing_key=Path("k"),
    )
    blockers = (
        Blocker("budget_below_quote", "set budget.cost_usd 0.70", True),
        Blocker("compile_deadline_short", "budget compile 5 240", False),
    )
    screens = [
        render.spec_screen(
            draft,
            blockers,
            floor=floor,
            min_call_cost=0.35,
            stage0=Spend(1, 1, 1, 0.1, 1),
            approved=True,
        ),
        render.budget_screen(spec, profile, 0.35, floor),
        render.diff_screen(['- task "café"', '+ task "x"'], "abcdef12"),
        render.progress_line(
            Progress(1, 1, "stage 1 selection", 0, 1, 0, 0, 0, 0, Spend(), Budget(), 1.0, 1.0)
        ),
        render.compile_banner(spec, profile, False),
        render.result_screen(outcome, floor, spec),
        render.run_screen(run, "mödel", expired_after=True),
        render.registry_screen([(entry, False)]),
        render.entry_screen(entry, artifact, floor),
        render.floor_screen(floor_draft, ["below minimum: 1 cases, need 2"]),
        render.floors_screen([floor]),
        render.status_screen(view),
        render.model_menu([ModelRow(1, "mödel", "codex", "x", 0.5, False, 0.1, True)]),
        render.profile_summary(
            Profile.model_validate(
                {"models": {"mödel": {"provider": "codex", "executable": "/x/mödel"}}}
            ),
            Path("w") / "prôfile.json",
        ),
        render.failure("stale_approval_token", "approve abcdef12"),
        render.failure("café"),
        render.footer(Spend(), None),
    ]
    for screen in screens:
        assert screen.isascii(), screen
        assert screen.endswith("\n")
        assert all(len(line) <= 100 for line in screen.split("\n")), screen
        # isascii() is blind to C0 controls and DEL; only the line feed may appear.
        assert not any(ord(ch) < 0x20 and ch != "\n" for ch in screen), screen
        assert "\x7f" not in screen


def test_schema_missing_blockers_say_which_shape_was_found() -> None:
    """{} and a property-less object read differently; the fix is the full JSON Schema shape."""
    shape = '{"type":"object","properties":{...},"required":[...],"additionalProperties":false}'
    spec = make_spec(input_schema={}, output_schema={"type": "object"})
    draft = Draft(spec=spec, origin="inferred", goal="goal", user_fields={}, original=spec)
    blockers = (
        Blocker("input_schema_missing", f"set input_schema {shape}", True),
        Blocker("output_schema_missing", f"set output_schema {shape}", True),
    )
    screen = render.spec_screen(
        draft, blockers, floor=None, min_call_cost=None, stage0=None, approved=False
    )
    assert "  input_schema   ~ {}\n" in screen
    assert '  output_schema  ~ {"type":"object"}\n' in screen
    # A 20-character code fills the 21-wide column, so one space follows it (21 gets one too).
    assert "! input_schema_missing input_schema is {}; no input is checked\n" in screen
    assert (
        "! output_schema_missing output_schema is type object with no properties; any output "
        "passes\n"
    ) in screen
    # The fix line is longer than 100 columns; it wraps exactly as any long screen line does.
    assert render.wrap(f"                       fix: set input_schema {shape}") + "\n" in screen
    assert render.wrap(f"                       fix: set output_schema {shape}") + "\n" in screen
    assert all(len(line) <= 100 for line in screen.split("\n"))


def test_ascii_text_is_the_screen_escape() -> None:
    assert render.ascii_text("café\x1b[31m") == "caf\\xe9\\x1b[31m"
    assert render.ascii_text("review") == "review"
