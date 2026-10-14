"""A build with declared checks, on the fake provider, through build mode (``/make @spec``).

The plan card lists the checks under the Engineer row; the exam licenses the model and the
checks together; ``architect verify`` accepts the license; and /run (the call Try it makes)
and an exported package answer with the checks applied, while a package whose declaration
was changed is refused. The same build without the checks is refused. Forge stays off for
a spec with checks, even when chosen, and the plan says so. A declaration that is not
valid, or names a field the spec does not have, says so on the plan and stops at practice
before any model call.

The receipts here were written for these tests; none comes from the CORD data. The fake
model answers them as plain models often do: a subtotal copied from the total when the
receipt prints none, and amounts read without their two decimals. Offline: sockets are
blocked and the signing key is a temporary one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from forge_modes_support import block_sockets
from trust_support import use_host

import architect.profiles as profiles
from architect.agent import RecordingUI, ToolContext
from architect.agent.build import (
    CHECKED_FORGE_NOTE,
    CHECKS_INVALID,
    _check_rules,
    make,
    set_override,
)
from architect.agent.events import PlanCard
from architect.agent.tools import _run_agent
from architect.bench.oracle import OracleAdapter, OracleState
from architect.bench.text_tasks import canonical_key
from architect.cli import main
from architect.forge_modes.export import ExportError, export_package, read_pin
from architect.forge_modes.shim import ExportedHarness
from architect.presets.checks import bind, declared
from architect.profiles import build_services, load_profile
from architect.spec import HarnessArtifact, Spec

ROOT = Path(__file__).resolve().parents[1]
OCT6 = ROOT / "examples" / "bench" / "eval-2026-10-06" / "ladder" / "spec.json"
FLOOR = "receipts-synthetic"
NAME = "receipt-reader"
RULES = (
    'Amounts keep every printed digit, so "226,500.00" is 22650000. A subtotal equal to the '
    "total is null unless the receipt prints that amount as a subtotal."
)

# case id, receipt text, label, the fake model's answer
RECEIPTS: tuple[tuple[str, str, dict[str, Any], dict[str, Any]], ...] = (
    ("r01", "2 ROTI BAKAR KEJU 36.000\n1 KOPI SUSU 15.000\nTOTAL 51.000\nTUNAI 100.000",
     {"total": 51000, "subtotal": None, "tax": None, "item_count": 2},
     {"total": 51000, "subtotal": 51000, "tax": None, "item_count": 2}),
    ("r02", "1 Matcha Cheesecake 45,000.00\n1 Hot Americano 30,000.00\nSubTotal 75,000.00\n"
            "Tax 7,500.00\nTotal 82,500.00\nPayment 100,000.00",
     {"total": 8250000, "subtotal": 7500000, "tax": 750000, "item_count": 2},
     {"total": 82500, "subtotal": 75000, "tax": 7500, "item_count": 2}),
    ("r03", "1 NASI GORENG 25.000\nSUBTOTAL 25.000\nTOTAL 25.000\nCASH 50.000",
     {"total": 25000, "subtotal": 25000, "tax": None, "item_count": 1},
     {"total": 25000, "subtotal": 25000, "tax": None, "item_count": 1}),
    ("r04", "3 ES TEH 15.000\nTOTAL 15.000\nCASH 20.000\nCHANGE 5.000",
     {"total": 15000, "subtotal": None, "tax": None, "item_count": 1},
     {"total": 15000, "subtotal": 15000, "tax": None, "item_count": 1}),
    ("r05", "1 MIE AYAM 20.000\n1 BAKSO 18.000\nTOTAL 38.000\nDISC 0\nGRAND TOTAL 38.000",
     {"total": 38000, "subtotal": 38000, "tax": None, "item_count": 2},
     {"total": 38000, "subtotal": 38000, "tax": None, "item_count": 2}),
    ("r06", "2 Croissant 44,000.00\nTotal 44,000.00\nCard 44,000.00",
     {"total": 4400000, "subtotal": None, "tax": None, "item_count": 1},
     {"total": 44000, "subtotal": 44000, "tax": None, "item_count": 1}),
    ("r07", "1 AIR MINERAL 5.000\n5.000\nTOTAL 5.000",
     {"total": 5000, "subtotal": 5000, "tax": None, "item_count": 1},
     {"total": 5000, "subtotal": 5000, "tax": None, "item_count": 1}),
    ("r08", "1 TEH MANIS 8.000\nTOTAL 8.000\nTUNAI 10.000",
     {"total": 8000, "subtotal": None, "tax": None, "item_count": 1},
     {"total": 8000, "subtotal": 8000, "tax": None, "item_count": 1}),
)
UNSEEN = "1 KOPI TUBRUK 12.000\nTOTAL 12.000\nTUNAI 20.000"
UNSEEN_ANSWER = {"total": 12000, "subtotal": 12000, "tax": None, "item_count": 1}
UNSEEN_CHECKED = {"total": 12000, "subtotal": None, "tax": None, "item_count": 1}


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    block_sockets(monkeypatch)
    use_host(monkeypatch, tmp_path / "host")


def _spec(**changes: Any) -> dict[str, Any]:
    """The receipt reader's checked spec, on the fake model and the receipts above."""
    data: dict[str, Any] = json.loads(OCT6.read_text(encoding="utf-8"))
    data.update(
        model_target="fake",
        floor={**data["floor"], "dataset": FLOOR, "minimum_samples": 4},
        **changes,
    )
    return data


class Fixture:
    """A folder with the spec file, a fake profile whose model answers from ``RECEIPTS``,
    and build mode's ToolContext on it."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch, spec: dict[str, Any]):
        root.mkdir(parents=True)
        cases = [{"case_id": cid, "task": {"receipt": text}, "expected": label}
                 for cid, text, label, _ in RECEIPTS]
        (root / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
        (root / "profile.json").write_text(json.dumps({
            "workspace": ".",
            "models": {"fake": {"provider": "fake"}},
            "floors": {FLOOR: {"kind": "held_out_tasks", "path": "cases.json",
                               "checker": "receipt_v1"}},
            "compile": {"max_candidates": 1, "enable_rewriting": False},
        }), encoding="utf-8")
        (root / "receipt-reader.json").write_text(json.dumps(spec), encoding="utf-8")
        answers = {canonical_key({"receipt": text}): answer for _, text, _, answer in RECEIPTS}
        answers[canonical_key({"receipt": UNSEEN})] = UNSEEN_ANSWER
        self.model = OracleAdapter("fake", OracleState(text=answers))
        monkeypatch.setattr(profiles, "_adapter", lambda profile, name, config: self.model)
        loaded = load_profile(root / "profile.json")
        self.services = build_services(loaded, loaded.workspace)
        self.ui = RecordingUI(answers=["yes"])
        self.ctx = ToolContext(services=self.services, cwd=root, ui=self.ui, model="fake")
        self.ctx.extras.update(profile_path=root / "profile.json", build_overrides={})
        set_override(self.ctx, "engineer", "preset")

    async def make(self) -> dict[str, Any]:
        result = await make("", self.ctx, spec_file="receipt-reader.json")
        assert isinstance(result, dict)
        return result

    def plan(self) -> list[tuple[str, str]]:
        [card] = [e for e in self.ui.events if isinstance(e, PlanCard)
                  and e.title.startswith("Plan")]
        return list(card.rows)


def test_the_receipts_here_keep_their_labels_under_the_checks() -> None:
    checks = declared(_spec()["execution_config"])
    for _, text, label, answer in RECEIPTS:
        assert bind(checks, {"receipt": text})(label) == label
        assert bind(checks, {"receipt": text})(answer) == label


async def test_a_checked_build_licenses_verifies_and_runs_its_checks_everywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    made = Fixture(tmp_path / "app", monkeypatch, _spec())
    try:
        result = await made.make()
        # The plan: the checks right after the engineer, in the declared order.
        keys = [key for key, _ in made.plan()]
        assert keys[keys.index("engineer") + 1] == "checks"
        assert dict(made.plan())["checks"] == RULES
        # The exam licensed the model and the checks together.
        assert result["licensed"] is True and result["installed"] == NAME, result
        spec = Spec.model_validate(_spec())
        entry = made.services.registry.status(NAME)
        artifact = HarnessArtifact.load(entry.directory)
        assert artifact.licensed and artifact.spec.spec_hash == spec.spec_hash
        assert artifact.spec.execution_config == spec.execution_config
        assert artifact.report is not None and artifact.report.floor_score == 1.0
        assert "'not_copied'" in (entry.directory / "harness.py").read_text(encoding="utf-8")
        code = main(["verify", str(entry.directory)])
        lines = capsys.readouterr().out.splitlines()
        assert code == 0 and lines[0] == "verified", lines
        assert lines[1].startswith(f"licensed artifact: spec {spec.spec_hash[:8]}, ")
        # /run and Try it: the agent tool the bridge's run_agent calls.
        task = {"receipt": UNSEEN}
        ran = await _run_agent({"name": NAME, "input": task}, made.ctx)
        assert isinstance(ran, dict) and ran["output"] == UNSEEN_CHECKED
        # Put into this project: the exported package runs the same harness bytes.
        manifest, _ = export_package(entry, made.services.workspace / "exports", "reader")
        package = made.services.workspace / "exports" / manifest.artifact_digest[:16] / "reader"
        assert (package / "artifact" / "harness.py").read_bytes() == (
            entry.directory / "harness.py").read_bytes()
        pin = read_pin(package)
        assert pin is not None and pin["spec_hash"] == spec.spec_hash
        exported = await ExportedHarness(package, pin).run_with(
            made.services.runtime, made.model, task)
        assert exported == UNSEEN_CHECKED
        # A package whose declaration was taken out is refused before it runs.
        spec_file = package / "artifact" / "spec.json"
        data = json.loads(spec_file.read_text(encoding="utf-8"))
        data["execution_config"] = {}
        spec_file.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ExportError, match="export_integrity_mismatch"):
            await ExportedHarness(package, pin).run_with(made.services.runtime, made.model, task)
    finally:
        made.services.close()


async def test_the_same_build_without_the_checks_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = Fixture(tmp_path / "app", monkeypatch, _spec(execution_config={}))
    try:
        result = await made.make()
        assert "checks" not in dict(made.plan())
        assert result["licensed"] is False and "primary_floor_failed" in result["failures"]
        assert not made.services.registry.list()
    finally:
        made.services.close()


async def test_forge_stays_off_for_a_spec_with_checks_even_when_chosen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A harness Forge writes need not run the checks, which live in the tuned preset."""
    made = Fixture(tmp_path / "app", monkeypatch, _spec())
    set_override(made.ctx, "engineer", "forge")
    try:
        result = await made.make()
        rows = made.plan()
        assert dict(rows)["engineer"] == "Tuned preset only: extractor (Forge off)"
        assert ("note", CHECKED_FORGE_NOTE) in rows
        assert CHECKED_FORGE_NOTE == (
            "Forge stays off: this spec's checks run inside the tuned preset.")
        assert "forge budget" not in dict(rows) and "forge pack" not in dict(rows)
        [card] = [e for e in made.ui.events if isinstance(e, PlanCard)
                  and e.title.startswith("Plan")]
        assert (card.data or {})["engineer"] == "preset"
        assert result["licensed"] is True and result["installed"] == NAME, result
    finally:
        made.services.close()


_CHECKS = _spec()["execution_config"]["checks"]


@pytest.mark.parametrize(
    "declaration",
    [
        pytest.param([{"check": "spell_check", "rule": "Spell well.", "input": "receipt"}],
                     id="not-a-check"),
        pytest.param([{**_CHECKS[0], "fields": ["total", "subtotl", "tax"]}, _CHECKS[1]],
                     id="output-field"),
        pytest.param([_CHECKS[0], {**_CHECKS[1], "copy_of": "totl"}], id="copy-of"),
        pytest.param([{**_CHECKS[0], "input": "receit"}, _CHECKS[1]], id="input-field"),
    ],
)
async def test_a_declaration_that_is_not_valid_stops_before_any_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, declaration: list[dict[str, Any]]
) -> None:
    bad = {"checks": declaration}
    made = Fixture(tmp_path / "app", monkeypatch, _spec(execution_config=bad))
    try:
        assert _check_rules(Spec.model_validate(_spec(execution_config=bad))) == (
            CHECKS_INVALID,)
        assert CHECKS_INVALID == (
            "The spec's checks are not valid, so the build stops before any model call.")
        result = await made.make()
        keys = [key for key, _ in made.plan()]
        assert keys[keys.index("engineer") + 1] == "checks"
        assert dict(made.plan())["checks"] == CHECKS_INVALID
        assert result["licensed"] is False
        assert result["failures"] == ["tuning_configuration_failed"], result
        assert made.model.calls == 0
    finally:
        made.services.close()
