"""The demo exam for retakes (examples/receipts/demo), built by scripts/build_demo_floor.py.

It is made of development receipts only. No receipt of the evidence exam and none of the
348 delivery receipts may be in it, and the example app's profile loads it as a floor a
retake may take.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from architect.floors import EvaluationCase, HeldOutTasks, check_floor, load_manifest
from architect.floors._access import _VERIFIER_CAPABILITY
from architect.floors.checkers import CHECKERS
from architect.profiles import load_profile
from architect.spec import Spec

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "examples" / "receipts"
APP = ROOT / "examples" / "expense-app"
FLOOR = "bench-receipts-demo"


def _script() -> Any:
    spec = importlib.util.spec_from_file_location(
        "build_demo_floor", ROOT / "scripts" / "build_demo_floor.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ids(path: Path) -> set[str]:
    if path.suffix == ".jsonl":
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    else:
        rows = json.loads(path.read_text(encoding="utf-8"))
    return {row["case_id"] for row in rows}


def _cases(name: str) -> list[EvaluationCase]:
    rows = json.loads((DATA / "demo" / name).read_text(encoding="utf-8"))
    return [EvaluationCase.model_validate(row) for row in rows]


def test_the_committed_files_are_what_the_script_derives() -> None:
    script = _script()
    assert script.verify(DATA, script.render(DATA)) == []
    assert "delivery" not in script.SOURCES


def test_the_script_opens_no_delivery_file(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _script()
    opened: list[str] = []
    real = Path.read_text

    def read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        opened.append(self.name)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    script.render(DATA)
    assert set(opened) == {
        "rehearsal.jsonl", "evidence.jsonl", "pilot.jsonl",
        "rehearsal.json", "evidence.json", "pilot.json",
    }
    with pytest.raises(ValueError):
        script._cases(DATA, "delivery")


def test_no_demo_receipt_is_an_evidence_exam_or_delivery_receipt() -> None:
    floor, retake = _ids(DATA / "demo" / "floor.json"), _ids(DATA / "demo" / "retake.json")
    assert (len(floor), len(retake)) == (200, 150) and not floor & retake
    evidence = json.loads((DATA / "manifests" / "evidence.json").read_text(encoding="utf-8"))
    exam_half = {e["case_id"] for e in evidence["cases"] if e["partition"] == "held_out"}
    practice_half = {e["case_id"] for e in evidence["cases"] if e["partition"] == "tuning"}
    delivery = _ids(DATA / "cases" / "delivery.jsonl")
    assert len(exam_half) == 100 and len(delivery) == 348
    for demo in (floor, retake):
        assert not demo & exam_half
        assert not demo & delivery
    # What it is made of: the rehearsal set, the evidence practice half, and the pilot.
    assert floor == _ids(DATA / "cases" / "rehearsal.jsonl") | practice_half
    assert retake == _ids(DATA / "cases" / "pilot.jsonl")


def test_the_manifests_match_and_label_the_receipts_as_development_receipts() -> None:
    for name, count in (("floor", 200), ("retake", 150)):
        manifest_path = DATA / "demo" / f"{name}.manifest.json"
        manifest = load_manifest(manifest_path)
        provenance = check_floor(_cases(f"{name}.json"), manifest_path)
        assert provenance.reason == "matched" and provenance.cases == count
        assert manifest.display() == ("CORD v2 development receipts", "receipts")
        assert manifest.note == (
            "These are development receipts. They were used while Architect was being "
            "built, so scores on them are not its published results."
        )
    # A published dataset's manifest carries no such line.
    assert load_manifest(DATA / "manifests" / "evidence.json").note is None


def test_the_example_profile_loads_the_demo_floor_with_its_retake_cases() -> None:
    profile = load_profile(APP / "architect.json")
    config = profile.floors[FLOOR]
    assert config.retakes_allowed and config.checker == "receipt_v1"
    assert profile.resolve(config.path) == (DATA / "demo" / "floor.json").resolve()
    assert config.retake_cases is not None
    assert profile.resolve(config.retake_cases) == (DATA / "demo" / "retake.json").resolve()
    spec = Spec.model_validate_json(
        (APP / "receipt-reader-demo.json").read_text(encoding="utf-8")
    )
    assert spec.floor.dataset == FLOOR and spec.floor.limit == 0.4
    assert spec.execution_config == {}  # the published task, without the two checks
    published = Spec.model_validate_json(
        (APP / "receipt-reader.json").read_text(encoding="utf-8")
    )
    assert spec.task == published.task and spec.output_schema == published.output_schema
    assert spec.model_target == "cheapest that passes" and spec.budget == published.budget

    floor = HeldOutTasks(spec.floor, _cases("floor.json"), CHECKERS[config.checker])
    floor._allow_retakes(lambda: _cases("retake.json"))
    assert (len(floor.tuning), floor.exam_size) == (100, 100)
    # Attempt 2 takes 100 receipts no attempt used; 50 are left, so attempt 3 is the same
    # exam.
    assert floor.retake_plan(()) == ("unused", 150)
    second = floor._retake_cases(_VERIFIER_CAPABILITY, (), floor.exam_size)
    assert second is not None and len(second) == 100
    used = {case.case_id for case in second}
    exam = {case.case_id for case in floor._verification_cases(_VERIFIER_CAPABILITY)}
    assert not used & exam and not used & {case.case_id for case in floor.tuning}
    assert floor.retake_plan(used) == ("same_exam", 50)
    assert floor._retake_cases(_VERIFIER_CAPABILITY, used, floor.exam_size) is None
