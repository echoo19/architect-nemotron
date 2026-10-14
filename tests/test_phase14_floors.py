"""Phase 14 WA, floors (docs/interfaces/phase14-nebius-evidence.md, Sections 5 and 6).

The ``receipt_v1`` checker under examples/receipts/RULES.md, and public dataset
provenance checked against the committed CORD v2 evidence floor and its manifest: a
match is ``public_dataset``; a tampered, short, unreadable or stamped floor is SYNTHETIC.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import TypeAdapter, ValidationError

from architect.floors import EvaluationCase, FloorError, HeldOutTasks
from architect.floors import provenance as provenance_module
from architect.floors.checkers import CHECKERS, RECEIPT_FIELDS, field_report, receipt_v1
from architect.floors.provenance import (
    DatasetManifest,
    FloorProvenance,
    case_sha256,
    cases_sha256,
    check_floor,
    load_manifest,
)
from architect.profiles import FloorConfig, load_profile, pi_denied_paths
from architect.routing.labels import FloorOrigin, floor_origin_weight
from architect.spec import FloorDefinition, FloorKind

ROOT = Path(__file__).resolve().parents[1]
RECEIPTS = ROOT / "examples" / "receipts"
EVIDENCE = RECEIPTS / "floors" / "evidence.json"
MANIFEST = RECEIPTS / "manifests" / "evidence.json"
CASES = TypeAdapter(list[EvaluationCase])


def evidence() -> list[EvaluationCase]:
    return CASES.validate_json(EVIDENCE.read_text(encoding="utf-8"))


# -- receipt_v1 ---------------------------------------------------------------------------

EXPECTED: dict[str, Any] = {"total": 75000, "subtotal": None, "tax": 0, "item_count": 3}


def test_receipt_v1_is_in_the_closed_table() -> None:
    assert CHECKERS["receipt_v1"] is receipt_v1
    assert RECEIPT_FIELDS == ("total", "subtotal", "tax", "item_count")


@pytest.mark.parametrize(
    ("output", "passes"),
    [
        (dict(EXPECTED), True),
        (dict(EXPECTED, note="ignored"), True),  # keys beyond the four are ignored
        (dict(EXPECTED, total=75000.0), False),  # a float never matches an int
        (dict(EXPECTED, total=75001), False),
        (dict(EXPECTED, subtotal=0), False),  # null is not 0
        (dict(EXPECTED, tax=None), False),  # 0 is not null
        (dict(EXPECTED, tax=False), False),  # a bool is not 0
        (dict(EXPECTED, item_count=True), False),
        ({key: value for key, value in EXPECTED.items() if key != "subtotal"}, False),
        ("not an object", False),
        (None, False),
    ],
)
def test_receipt_v1_exact_matching(output: Any, passes: bool) -> None:
    assert receipt_v1(EXPECTED, output) is passes


def test_receipt_v1_field_report_counts() -> None:
    report = field_report(EXPECTED, dict(EXPECTED, total=1, subtotal=5), checker="receipt_v1")
    assert report.fields == 4 and report.matched == 2
    assert report.per_field == {
        "item_count": (1, 1), "subtotal": (0, 1), "tax": (1, 1), "total": (0, 1)
    }
    assert field_report(EXPECTED, {}, checker="receipt_v1").matched == 0
    assert field_report(EXPECTED, dict(EXPECTED), checker="receipt_v1").matched == 4


@pytest.mark.parametrize(
    "expected",
    [
        "75000",
        {"total": 1, "subtotal": 1, "tax": 1},  # a missing key
        dict(EXPECTED, item_count=None),  # item_count is never null
        dict(EXPECTED, total=-1),
        dict(EXPECTED, total=1.5),
        dict(EXPECTED, tax=True),
    ],
)
def test_receipt_v1_refuses_malformed_expected_values(expected: Any) -> None:
    with pytest.raises(FloorError, match="malformed"):
        receipt_v1(expected, dict(EXPECTED))
    with pytest.raises(FloorError):
        field_report(expected, dict(EXPECTED), checker="receipt_v1")


async def test_receipt_v1_scores_the_committed_floor() -> None:
    cases = evidence()
    definition = FloorDefinition(
        kind=FloorKind.HELD_OUT_TASKS, limit=0.8, dataset="cord", minimum_samples=100
    )
    floor = HeldOutTasks(definition, cases, CHECKERS["receipt_v1"])
    tuning = floor.tuning
    outputs: list[Any] = [case.expected for case in tuning]
    outputs[0] = dict(outputs[0], item_count=outputs[0]["item_count"] + 1)  # type: ignore[arg-type]
    assert await floor.score(tuning, outputs) == pytest.approx(99 / 100)


# -- provenance on the committed data ----------------------------------------------------


def test_committed_evidence_floor_is_public_dataset() -> None:
    cases = evidence()
    result = check_floor(cases, MANIFEST)
    assert result == FloorProvenance(
        origin="public_dataset",
        reason="matched",
        dataset="naver-clova-ix/cord-v2",
        dataset_revision="7f0115a4b758a71d6473b8d085751692da2fef98",
        license="CC-BY-4.0",
        rule_version="receipts-v2",
        title="CORD v2",
        unit="receipts",
        cases=200,
        matched=200,
    )
    assert result.card_text() == "CORD v2 · CC BY 4.0 · 200 receipts"
    assert result.card_text(sep=" | ") == "CORD v2 | CC BY 4.0 | 200 receipts"
    manifest = load_manifest(MANIFEST)
    assert cases_sha256(cases) == manifest.cases_sha256
    pinned = {entry.case_id: entry.case_sha256 for entry in manifest.cases}
    assert all(pinned[case.case_id] == case_sha256(case) for case in cases)


@pytest.mark.parametrize("split", ["rehearsal", "evidence"])
def test_each_committed_floor_matches_its_manifest(split: str) -> None:
    """Build mode labels a profile floor from its manifest (Section 5.4): both committed
    floors, the rehearsal one for development builds and the evidence one for the
    pre-registered builds, are the public dataset at the manifest's own count."""
    path, manifest = RECEIPTS / "floors" / f"{split}.json", RECEIPTS / "manifests" / f"{split}.json"
    cases = CASES.validate_json(path.read_text(encoding="utf-8"))
    pinned = load_manifest(manifest)
    result = check_floor(cases, manifest)
    assert (result.origin, result.reason) == ("public_dataset", "matched")
    assert result.rule_version == "receipts-v2" and result.cases == pinned.count == len(cases)
    assert result.card_text(" | ") == f"CORD v2 | CC BY 4.0 | {len(cases)} receipts"
    floor = HeldOutTasks(
        FloorDefinition(
            kind=FloorKind.HELD_OUT_TASKS, limit=0.8, dataset="cord",
            minimum_samples=len(cases) // 2,
        ),
        cases,
        CHECKERS["receipt_v1"],
    )
    assert len(floor.tuning) == (len(cases) + 1) // 2  # the even/odd rule's practice half


def test_case_hash_ignores_the_case_metadata() -> None:
    case = evidence()[0]
    stamped = case.model_copy(update={"metadata": {"note": "local"}})
    assert case_sha256(stamped) == case_sha256(case)


def _tampered(cases: list[EvaluationCase], index: int = 7) -> list[EvaluationCase]:
    changed = list(cases)
    expected = dict(changed[index].expected)  # type: ignore[arg-type]
    expected["item_count"] = expected["item_count"] + 1  # one changed character
    changed[index] = changed[index].model_copy(update={"expected": expected})
    return changed


def test_one_changed_value_labels_the_floor_synthetic() -> None:
    result = check_floor(_tampered(evidence()), MANIFEST)
    assert result.origin == "synthetic" and result.reason == "case_mismatch"
    assert (result.cases, result.matched) == (200, 199)
    assert result.card_text() == "200 cases · SYNTHETIC · 1 cases do not match the manifest"
    assert result.card_text(" | ") == "200 cases | SYNTHETIC | 1 cases do not match the manifest"


def test_a_tampered_manifest_labels_the_floor_synthetic(tmp_path: Path) -> None:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    entry = data["cases"][3]
    entry["case_sha256"] = ("0" if entry["case_sha256"][0] != "0" else "1") + entry[
        "case_sha256"
    ][1:]
    tampered = tmp_path / "manifest.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")
    result = check_floor(evidence(), tampered)
    assert result.origin == "synthetic" and result.reason == "case_mismatch"
    assert result.matched == 199


def test_short_unreadable_and_stamped_floors_are_synthetic(tmp_path: Path) -> None:
    cases = evidence()
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    data["cases"] = data["cases"][:199]
    data["count"] = 199
    short = tmp_path / "short.json"
    short.write_text(json.dumps(data), encoding="utf-8")
    counted = check_floor(cases, short)
    assert (counted.origin, counted.reason) == ("synthetic", "count_mismatch")
    assert counted.card_text() == (
        "200 cases · SYNTHETIC · the case count differs from the manifest"
    )
    for unreadable in (tmp_path / "missing.json", tmp_path / "garbage.json"):
        if unreadable.name == "garbage.json":
            unreadable.write_text("{not json", encoding="utf-8")
        result = check_floor(cases, unreadable)
        assert (result.origin, result.reason) == ("synthetic", "manifest_unreadable")
        assert result.dataset is None and result.matched == 0
        assert result.card_text() == "200 cases · SYNTHETIC · the manifest could not be read"
    stamped = [cases[0].model_copy(update={"metadata": {"synthetic": True}}), *cases[1:]]
    result = check_floor(stamped, MANIFEST)
    assert (result.origin, result.reason) == ("synthetic", "stamped_synthetic")
    assert result.card_text() == "200 cases · SYNTHETIC · cases are stamped synthetic"


def test_an_oversized_manifest_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(provenance_module, "MAX_MANIFEST_BYTES", 1024)
    with pytest.raises(FloorError):
        load_manifest(MANIFEST)
    assert check_floor(evidence(), MANIFEST).reason == "manifest_unreadable"


def test_manifest_validation() -> None:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    DatasetManifest.model_validate(data)  # extra keys are ignored
    for broken in (
        {**data, "count": 199},  # count differs from the case list
        {**data, "cases": [*data["cases"][:-1], data["cases"][0]]},  # a repeated case id
        {**data, "manifest_version": 2},
    ):
        with pytest.raises(ValidationError):
            DatasetManifest.model_validate(broken)
    titled = DatasetManifest.model_validate(
        {**data, "dataset": "someone/else", "title": "Other", "unit": "rows"}
    )
    assert titled.display() == ("Other", "rows")
    unknown = DatasetManifest.model_validate({**data, "dataset": "someone/else"})
    assert unknown.display() == ("someone/else", "cases")


# -- profile and routing labels -----------------------------------------------------------


def test_floor_manifest_is_for_held_out_tasks_only(tmp_path: Path) -> None:
    config = FloorConfig(kind=FloorKind.HELD_OUT_TASKS, path=Path("cases.json"),
                         manifest=Path("manifest.json"))
    assert config.manifest == Path("manifest.json")
    assert FloorConfig(kind=FloorKind.HELD_OUT_TASKS, path=Path("x.json")).manifest is None
    with pytest.raises(ValidationError):
        FloorConfig(kind=FloorKind.LLM_JUDGE, path=Path("x.json"), manifest=Path("m.json"))


def test_profile_floor_with_manifest_loads_and_hides_the_manifest(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "models": {"fake": {"provider": "fake"}},
                "floors": {
                    "cord-receipts-evidence": {
                        "kind": "held_out_tasks",
                        "path": str(EVIDENCE),
                        "manifest": str(MANIFEST),
                        "checker": "receipt_v1",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    profile = load_profile(profile_path)
    config = profile.floors["cord-receipts-evidence"]
    assert config.manifest is not None and profile.resolve(config.manifest) == MANIFEST
    assert MANIFEST.resolve() in pi_denied_paths(profile, tmp_path / "w")


def test_public_dataset_routing_label_is_not_down_weighted() -> None:
    assert "public_dataset" in get_args(FloorOrigin)
    assert floor_origin_weight("public_dataset") == 1.0
    assert floor_origin_weight("model_draft") == 0.5
