"""Phase 14 WA, build mode (docs/interfaces/phase14-nebius-evidence.md, Sections 4.4, 5, 7, 8).

The plan card's floor row from a manifest (and its SYNTHETIC form for a tampered one),
frozen builds (``/make @spec.json``: the full hash on the card, no Stage 0, overrides
not applied, ``floor_not_configured``, print mode), the counts readout, the ASCII
separator, the build-mode audit and the export origin. Offline, on the fake profile.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from forge_support import ScriptedPi, ensure_sandbox, install_pi
from session_support import fake_profile, make_spec, response
from test_agent_conductor import Scripted

from architect.agent import RecordingUI, ToolContext
from architect.agent.app import AppOptions, boot
from architect.agent.build import (
    AUDIT_STEP,
    DESTINATION_QUESTION,
    FROZEN_FLOOR_STEP,
    FROZEN_NOTE,
    FROZEN_STEP,
    _next_step,
    floor_provenance,
    frozen_name,
    make,
    set_override,
)
from architect.agent.commands import frozen_path
from architect.agent.events import PlanCard
from architect.agent.export import _floor_origin
from architect.agent.render import ASCII, UNICODE, PlainRenderer, card_rows
from architect.agent.repl import LineFrontend, Repl, frozen_task, main
from architect.compiler.forge_audit import AUDIT_ENV, AuditSubmission
from architect.floors import EvaluationCase
from architect.floors.provenance import case_sha256
from architect.profiles import build_services, load_profile
from architect.session import ScriptedConsole
from architect.session.floors import add_case, new_draft, save_dataset
from architect.session.state import DraftCase, State
from architect.spec import FloorKind, Spec
from architect.telemetry import categorical

DESCRIPTION = "make an agent that extracts invoice fields from text"
INTERVIEW = json.dumps(
    {
        "inputs": {"value": "invoice text", "certain": True},
        "example": {"value": '{"text": "Invoice 7"}', "certain": True},
        "outputs": {"value": "vendor, total", "certain": True},
        "correctness": {"value": "exact match", "certain": True},
        "location": {"value": "installed agent", "certain": True},
        "limits": {"value": "defaults", "certain": True},
    }
)
CHEAP_OK = (
    "from pydantic import JsonValue\n"
    "from architect.runtime import RunContext\n\n"
    "async def run(context: RunContext, task: JsonValue) -> JsonValue:\n"
    '    return "ok"\n'
)


def write_manifest(workspace: Path, *, tamper: bool = False) -> None:
    """A manifest for the fake profile's six cases, named by its ``tasks`` floor."""
    rows = json.loads((workspace / "cases.json").read_text(encoding="utf-8"))
    cases = [EvaluationCase.model_validate(row) for row in rows]
    entries = [{"case_id": case.case_id, "case_sha256": case_sha256(case)} for case in cases]
    if tamper:
        entries[1]["case_sha256"] = "0" * 64
    manifest = {
        "manifest_version": 1, "dataset": "example/fixture", "dataset_revision": "r1",
        "license": "MIT", "rule_version": "v1", "count": len(entries), "cases": entries,
        "title": "Fixture", "unit": "rows", "files": {"ignored": True},
    }
    (workspace / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    profile = workspace / "session-profile.json"
    data = json.loads(profile.read_text(encoding="utf-8"))
    data["floors"]["tasks"]["manifest"] = "manifest.json"
    profile.write_text(json.dumps(data, indent=2), encoding="utf-8")


class Build:
    """A ToolContext on the fake profile, as build mode's own tests set it up."""

    def __init__(
        self,
        tmp_path: Path,
        replies: list[str],
        answers: list[str | None] | None = None,
        *,
        engineer: str | None = "preset",
        manifest: str | None = None,
    ) -> None:
        self.path = fake_profile(tmp_path)
        (self.path.parent / "inference.json").write_text(
            json.dumps([response(text) for text in replies]), encoding="utf-8"
        )
        if manifest is not None:
            write_manifest(self.path.parent, tamper=manifest == "tampered")
        self.ui = RecordingUI(answers=list(answers or []))
        self.ctx = ToolContext(services=self._services(), cwd=tmp_path, ui=self.ui, model="fake")
        self.ctx.extras.update(profile_path=self.path, reload=self.reload, build_overrides={})
        if engineer is not None:
            set_override(self.ctx, "engineer", engineer)

    def _services(self) -> Any:
        profile = load_profile(self.path)
        return build_services(profile, profile.workspace)

    def reload(self) -> Any:
        old = self.ctx.services
        self.ctx.services = self._services()
        old.close()
        return self.ctx.services

    def cards(self, title: str = "Plan") -> list[PlanCard]:
        return [
            event for event in self.ui.events
            if isinstance(event, PlanCard) and event.title.startswith(title)
        ]

    def rows(self, title: str = "Plan") -> dict[str, str]:
        return dict(self.cards(title)[-1].rows)

    def close(self) -> None:
        self.ctx.services.close()


@pytest.fixture
def build(tmp_path: Path):  # type: ignore[no-untyped-def]
    made: list[Build] = []

    def factory(*args: Any, **kwargs: Any) -> Build:
        item = Build(tmp_path, *args, **kwargs)
        made.append(item)
        return item

    yield factory
    for item in made:
        item.close()


# -- provenance on the plan card -----------------------------------------------------------


async def test_a_matching_manifest_labels_the_floor_public(build) -> None:  # type: ignore[no-untyped-def]
    h = build([INTERVIEW, make_spec().model_dump_json()], ["", ""], manifest="matching")
    result = await make(DESCRIPTION, h.ctx)
    assert h.rows()["floor data"] == "Fixture · MIT · 6 rows (3 tuning / 3 held-out)"
    assert isinstance(result, dict) and result["licensed"] is True, result
    assert result["floor_origin"] == "public_dataset"
    floor = h.rows("Result")["floor"]
    assert floor == "1.00/1.00 on 3 held-out cases (correct 3/3 · invalid 0)"
    assert result["held_out_counts"] == {"cases": 3, "correct": 3, "invalid": 0,
                                         "over_budget": 0}


async def test_a_tampered_manifest_shows_synthetic_on_the_plan_card(build) -> None:  # type: ignore[no-untyped-def]
    h = build([INTERVIEW, make_spec().model_dump_json()], ["", ""], manifest="tampered")
    result = await make(DESCRIPTION, h.ctx)
    assert h.rows()["floor data"] == (
        "6 cases · SYNTHETIC · 1 cases do not match the manifest (3 tuning / 3 held-out)"
    )
    assert isinstance(result, dict) and result["floor_origin"] == "synthetic"
    assert h.rows("Result")["floor"].endswith("(SYNTHETIC floor)")


async def test_a_floor_without_a_manifest_keeps_todays_row(build) -> None:  # type: ignore[no-untyped-def]
    h = build([INTERVIEW, make_spec().model_dump_json()], ["", ""])
    result = await make(DESCRIPTION, h.ctx)
    assert h.rows()["floor data"] == "configured floor 'tasks': 6 cases (3 tuning / 3 held-out)"
    assert isinstance(result, dict) and result["floor_origin"] == "user"
    assert floor_provenance(h.ctx.services.profile, "tasks") is None


def test_export_origin_follows_the_manifest(tmp_path: Path) -> None:
    for manifest, expected in ((None, "user_dataset"), ("matching", "public_dataset"),
                               ("tampered", "model_draft")):
        root = tmp_path / (manifest or "none")
        h = Build(root, [], manifest=manifest)
        try:
            artifact = type("A", (), {"spec": make_spec()})()
            assert _floor_origin(h.ctx, artifact) == expected
        finally:
            h.close()


def test_export_origin_of_a_floor_another_folder_built(tmp_path: Path) -> None:
    """Put into a project from a folder whose profile does not list the agent's floor: the
    cases build mode saved in the workspace say where the floor came from, and a floor that
    cannot be read there is never called the user's own data."""
    h = Build(tmp_path, [])
    try:
        workspace = h.ctx.services.workspace
        dataset = "elsewhere-cases"
        assert dataset not in h.ctx.services.profile.floors
        artifact = type("A", (), {"spec": make_spec(dataset=dataset)})()
        assert _floor_origin(h.ctx, artifact) == "model_draft"  # nothing saved
        drafted = new_draft(dataset, FloorKind.HELD_OUT_TASKS, State.REVIEW)
        for index in range(4):
            case = EvaluationCase(
                case_id=f"{dataset}-s00{index + 1}", task={"text": str(index)}, expected="ok"
            )
            drafted.cases.append(DraftCase(case=case, origin="model_draft", reviewed=False))
        save_dataset(workspace, drafted)
        assert _floor_origin(h.ctx, artifact) == "model_draft"
        typed = new_draft(dataset, FloorKind.HELD_OUT_TASKS, State.REVIEW)
        for index in range(4):
            add_case(typed, {"task": {"text": str(index)}, "expected": "ok"}, origin="operator")
        save_dataset(workspace, typed)
        assert _floor_origin(h.ctx, artifact) == "user_dataset"
        # A dataset that is a path never reads a file, even one that holds the user's cases.
        path = type("A", (), {"spec": make_spec(dataset=f"../floors/{dataset}")})()
        assert _floor_origin(h.ctx, path) == "model_draft"
    finally:
        h.close()


# -- frozen builds -------------------------------------------------------------------------


def write_spec(tmp_path: Path, name: str = "receipts.spec.json", **updates: Any) -> Spec:
    spec = Spec.model_validate(
        {**make_spec(lifecycle="install").model_dump(mode="json"), **updates}
    )
    (tmp_path / name).write_text(spec.model_dump_json(), encoding="utf-8")
    return spec


def test_frozen_names_and_paths() -> None:
    assert frozen_name("receipts.spec.json") == "receipts"
    assert frozen_name("A7_forge.json") == "a7-forge"
    assert frozen_name(".json") == "agent"
    assert frozen_path("@spec.json") == "spec.json"
    assert frozen_path('@"dir with space/spec.json"') == "dir with space/spec.json"
    assert frozen_path("@spec.json and more words") is None
    assert frozen_path("make an agent") is None and frozen_path("@") is None
    assert frozen_task("/make @spec.json") == (False, "spec.json")
    assert frozen_task("  /forge @specs/a.json ") == (True, "specs/a.json")
    assert frozen_task("/make an agent") is None and frozen_task("hello") is None


async def test_frozen_build_compiles_exactly_the_file(build, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    spec = write_spec(tmp_path)
    h = build([])  # no Stage 0 reply exists: any inference call would fail the build
    set_override(h.ctx, "floor_limit", 0.5)
    set_override(h.ctx, "budget_usd", 0.5)
    result = await make("", h.ctx, spec_file="receipts.spec.json")
    assert h.ui.questions == ["Compile this agent?"]  # no interview
    card = h.cards()[-1]
    assert card.rows[0][0] == "task"
    assert card.rows[1] == ("spec", f"{spec.spec_hash} (frozen, receipts.spec.json)")
    assert ("note", FROZEN_NOTE) in card.rows
    # The footer invites no /floor, /budget or /model edit: a frozen spec ignores them.
    assert card.footer == f"spec {spec.spec_hash[:8]}. Enter compiles; the spec file is " \
        "compiled as written."
    assert isinstance(result, dict) and result["licensed"] is True, result
    assert result["spec_hash"] == spec.spec_hash and len(spec.spec_hash) == 64
    assert result["spec_file"] == "receipts.spec.json" and result["installed"] == "receipts"
    installed = h.ctx.services.registry.get("receipts")
    assert installed.spec.spec_hash == spec.spec_hash  # the overrides were not applied
    inference = categorical("inference")
    assert not [row for row in h.ctx.services.runtime.store.rows()
                if row.preset_origin == inference]


async def test_frozen_build_stops_when_its_floor_is_not_configured(build, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    floor = make_spec().floor.model_copy(update={"dataset": "missing"})
    spec = write_spec(tmp_path, "x.json", floor=floor.model_dump(mode="json"))
    h = build([])
    result = await make("", h.ctx, spec_file=str(tmp_path / "x.json"))
    assert isinstance(result, dict) and result["failures"] == ["floor_not_configured"]
    assert result["next_step"] == FROZEN_FLOOR_STEP
    assert result["spec_hash"] == spec.spec_hash and result["spec_file"] == "x.json"
    assert not h.cards() and h.ui.questions == []


async def test_frozen_build_refuses_an_unreadable_spec_file(build, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "bad.json").write_text("{not a spec", encoding="utf-8")
    h = build([])
    for name in ("bad.json", "absent.json"):
        result = await make("", h.ctx, spec_file=name)
        assert isinstance(result, dict) and result["failures"] == ["invalid_spec_file"]
        assert result["spec_file"] == name and "spec_hash" not in result


async def test_a_refused_frozen_build_names_no_change_it_ignores(build, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A frozen build compiles its file as written (FROZEN_IGNORED), so its next step
    never suggests /floor, /budget or /model: following one would rebuild the same plan."""
    write_spec(tmp_path)
    h = build([])
    cases = h.path.parent / "cases.json"
    rows = json.loads(cases.read_text(encoding="utf-8"))
    for index, row in enumerate(rows):
        if index % 2 == 1:  # the held-out half: practice passes, the exam does not
            row["expected"] = "nope"
    cases.write_text(json.dumps(rows), encoding="utf-8")
    result = await make("", h.ctx, spec_file="receipts.spec.json")
    assert isinstance(result, dict) and result["licensed"] is False, result
    failures = result["failures"]
    assert isinstance(failures, list) and "primary_floor_failed" in failures
    assert result["next_step"] == FROZEN_STEP
    assert h.rows("Result")["next"] == FROZEN_STEP


def test_a_frozen_builds_next_step_names_the_profile_the_file_or_a_later_try(build) -> None:  # type: ignore[no-untyped-def]
    h = build([])
    services = h.ctx.services
    spec = make_spec()
    steps = {
        code: _next_step([code], spec, services, None, frozen="receipt-reader.json")
        for code in ("compile_budget", "primary_budget_failed", "primary_floor_failed",
                     "primary_invalid_exceeded", "tuning_failed")
    }
    for code, step in steps.items():
        assert not any(word in step for word in ("/budget", "/floor", "/model")), (code, step)
    assert steps["compile_budget"] == (
        "the build ran out of the profile's compile budget ($10.00 and 2 min): raise "
        "compile.compile_budget in the profile, then /make @receipt-reader.json again"
    )
    assert steps["primary_budget_failed"] == (
        "an exam run went over the $0.10 and 60 s a run that receipt-reader.json allows, "
        "which happens when a model answers slowly: try /make @receipt-reader.json again later"
    )
    assert steps["primary_floor_failed"] == steps["tuning_failed"] == FROZEN_STEP
    spaced = _next_step(["primary_budget_failed"], spec, services, None, frozen="my spec.json")
    assert spaced.endswith('try /make @"my spec.json" again later')
    # Without a spec file the steps are as before.
    assert "/budget run" in _next_step(["primary_budget_failed"], spec, services, None)


async def test_declining_a_frozen_plan_compiles_nothing(build, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    spec = write_spec(tmp_path, invalid_policy="count_as_miss")
    h = build([], ["no"])
    result = await make("", h.ctx, spec_file="receipts.spec.json")
    assert isinstance(result, dict) and result["cancelled"] is True
    assert result["spec_hash"] == spec.spec_hash
    assert h.rows()["invalid output"] == "counts as a miss, at most 0 of 3 held-out runs"
    assert not h.ctx.services.registry.list()


def test_print_mode_prints_the_card_and_compiles_only_on_full_auto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(AUDIT_ENV, raising=False)
    fake_profile(tmp_path)
    spec = write_spec(tmp_path, "spec.json")
    monkeypatch.chdir(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    code = main(["-p", "/make @spec.json", "--workspace", "w"], stdout=out, stderr=err)
    assert code == 0, err.getvalue()
    payload = json.loads(out.getvalue())
    assert payload["cancelled"] is True and payload["spec_hash"] == spec.spec_hash
    assert payload["spec_file"] == "spec.json"
    assert "== Plan: spec ==" in err.getvalue() and spec.spec_hash in err.getvalue()
    assert not (tmp_path / "w" / "registry").exists() or not list(
        (tmp_path / "w" / "registry").glob("*/*")
    )
    out, err = io.StringIO(), io.StringIO()
    code = main(["-p", "/make @spec.json", "--workspace", "w", "--permission", "full-auto"],
                stdout=out, stderr=err)
    assert code == 0, err.getvalue()
    built = json.loads(out.getvalue())
    assert built["licensed"] is True and built["spec_hash"] == spec.spec_hash
    assert "== Result: spec v1 ==" in err.getvalue()


async def test_slash_make_at_path_calls_the_tool_directly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(AUDIT_ENV, raising=False)
    fake_profile(tmp_path)
    spec = write_spec(tmp_path, "spec.json")
    console = ScriptedConsole(["no"])
    frontend = LineFrontend(console)
    adapter = Scripted('{"text": "never asked"}')
    app = await boot(AppOptions(cwd=tmp_path, workspace=tmp_path / "w"), frontend,
                     adapter=adapter)
    try:
        repl = Repl(app, frontend)
        await repl.handle("/make @spec.json")
        await repl.handle("/forge @missing.json")
    finally:
        app.close()
    transcript = console.transcript()
    assert spec.spec_hash in transcript and f"Not compiled. Spec {spec.spec_hash}." in transcript
    assert "Not compiled (invalid_spec_file)" in transcript
    assert adapter.prompts == []  # the conductor's model never saw either request


# -- rendering -----------------------------------------------------------------------------


def test_ascii_terminals_draw_the_separator_as_a_bar() -> None:
    rows = (("floor data", "CORD v2 · CC BY 4.0 · 200 receipts (100 tuning / 100 held-out)"),)
    assert card_rows(rows, ASCII) == [
        ("floor data", "CORD v2 | CC BY 4.0 | 200 receipts (100 tuning / 100 held-out)")
    ]
    assert card_rows(rows, UNICODE) == list(rows)
    lines: list[str] = []
    PlainRenderer(lines.append, ASCII).show(PlanCard("Plan: x", rows))
    assert "CORD v2 | CC BY 4.0 | 200 receipts" in "".join(lines)
    assert "".join(lines).isascii()


def test_a_terminal_card_starts_each_sentence_row_with_a_capital() -> None:
    """As on the desktop's cards: the rows that are sentences start with a capital, while
    the task, field lists, names, paths and field names keep their case."""
    rows = (
        ("task", "pull the total out of receipt text"),
        ("engineer", "a tuned extractor preset (Forge off)"),
        ("inputs", "receipt"),
        ("invalid output", "counts as a miss, at most 2 of 100 held-out runs"),
        ("forge budget", "up to $1.20 and 45 min on top of the compile budget"),
        ("note", "per-run budget raised to $0.04 and 180 s so two model calls fit"),
        ("note", "input_schema normalized into a JSON Schema object"),
        ("destination", "run it here: /run r, `architect run`, HTTP through /serve"),
        ("install as", "receipt-reader (installed agent)"),
        ("next", "/run r <input>"),
    )
    assert [value for _, value in card_rows(rows, UNICODE)] == [
        "pull the total out of receipt text",
        "A tuned extractor preset (Forge off)",
        "receipt",
        "Counts as a miss, at most 2 of 100 held-out runs",
        "Up to $1.20 and 45 min on top of the compile budget",
        "Per-run budget raised to $0.04 and 180 s so two model calls fit",
        "input_schema normalized into a JSON Schema object",
        "Run it here: /run r, `architect run`, HTTP through /serve",
        "receipt-reader (installed agent)",
        "/run r <input>",
    ]
    lines: list[str] = []
    PlainRenderer(lines.append, ASCII).show(PlanCard("Plan: x", rows))
    assert "Counts as a miss, at most 2 of 100 held-out runs" in "".join(lines)


# -- the audit in build mode -------------------------------------------------------------


async def test_build_mode_refuses_an_unwritable_audit_before_any_spend(  # type: ignore[no-untyped-def]
    build, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv(AUDIT_ENV, str(blocker))
    h = build([INTERVIEW, make_spec().model_dump_json()], ["", ""], engineer=None)
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result["failures"] == ["audit_unwritable"]
    assert result["next_step"] == AUDIT_STEP and result["cost_usd"] == 0
    assert h.ui.questions == []  # stopped before the interview
    # Without Forge there is nothing to audit, so the variable does not stop the build.
    set_override(h.ctx, "engineer", "preset")
    plain = await make(DESCRIPTION, h.ctx)
    assert isinstance(plain, dict) and plain["licensed"] is True


async def test_build_mode_audits_every_forge_submission(  # type: ignore[no-untyped-def]
    build, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await ensure_sandbox(monkeypatch, tmp_path / "probe")
    install_pi(
        monkeypatch,
        ScriptedPi(
            [
                {"write": {"path": "harness.py", "content": CHEAP_OK}},
                {"tool": "forge_submit", "arguments": {"note": "custom"}},
                {"text": "done"},
            ]
        ),
    )
    audit = tmp_path / "audit"
    monkeypatch.setenv(AUDIT_ENV, str(audit))
    h = build([INTERVIEW, make_spec().model_dump_json()], ["", ""], engineer=None)
    result = await make(DESCRIPTION, h.ctx)
    assert h.ui.questions == [DESTINATION_QUESTION, "Compile this agent?"]
    assert isinstance(result, dict) and result["licensed"] is True, result
    records = sorted(audit.rglob("submission.json"))
    assert len(records) == 1
    record = AuditSubmission.model_validate_json(records[0].read_text(encoding="utf-8"))
    assert record.verdict == "accepted" and record.tuning is not None
    assert (records[0].parent / "harness.py").read_bytes() == CHEAP_OK.encode()
