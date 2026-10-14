"""Suite v2 and the public receipts task (Phase 14 Sections 10.1 to 10.4), offline.

The committed CORD v2 receipt data loads and passes every manifest check; edits to a
copy of it are refused with their codes; the v1 digest and every v1 Spec hash stay as
they were, and the v2 digest moves with the settings and the data.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import get_args

import pytest
from bench_public_support import cases, copy_public, edit_json, write_split
from jsonschema import Draft202012Validator
from pydantic import JsonValue

import architect.spec as spec_module
from architect.bench import public_tasks, read_rows
from architect.bench import suite as suite_module
from architect.bench.oracle import _corrupt
from architect.bench.rows import BenchError, InvalidPolicy, ReplyJson
from architect.bench.runner import BenchConfig, _Context, _size_cell, plan
from architect.bench.suite import (
    PLAIN_PRESETS,
    SUITE_TIERS,
    SUITES,
    TIERS,
    arm_fields,
    suite_digest,
    tiers_for,
)
from architect.bench.text_tasks import canonical_json
from architect.floors import checkers
from architect.models.pricing import load_prices
from architect.spec import FloorKind, Spec

PINNED_V1_DIGEST = "1f23e5e421b2c2c3d79482394a841764b37f4e70d3b06a73993fc7bb4bf335ed"
ROOT = Path(__file__).resolve().parents[1]
RECEIPTS = ROOT / "examples" / "receipts"
# Whether WA's Phase 14 Spec fields (Section 3.1) are in this build.
ARM_FIELDS_DECLARED = "reply_json" in Spec.model_fields


@pytest.fixture
def copied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A copy of the public data that ``load_public`` and suite v2 read instead."""
    root = copy_public(tmp_path / "repo")
    public_tasks.clear_cache()
    monkeypatch.setattr(public_tasks, "REPOSITORY", root)
    yield root
    public_tasks.clear_cache()


def refused(code: str) -> None:
    public_tasks.clear_cache()
    with pytest.raises(BenchError) as caught:
        public_tasks.load_public("receipts")
    assert caught.value.code == code


# ----------------------------------------------------------------- suites and tiers


def test_the_v1_digest_is_unchanged_and_tiers_are_kept_per_suite() -> None:
    assert suite_digest("v1") == PINNED_V1_DIGEST
    assert TIERS is SUITE_TIERS["v1"] and tiers_for("v1") is TIERS
    assert set(TIERS) == {"smoke", "quick", "full", "arc"}  # v1 never gains "public"
    assert dict(tiers_for("v2")) == {"public": ("receipts",)}
    assert list(SUITES) == ["v1", "v2"] and len(SUITES) == 2
    assert "v2" in SUITES and "v3" not in SUITES
    assert [task.id for task in SUITES["v2"]] == ["receipts"]
    with pytest.raises(BenchError) as caught:
        tiers_for("v3")
    assert caught.value.code == "bench_suite_unknown"


def test_the_v2_digest_is_stable_and_moves_with_the_settings_and_the_data(
    copied: Path,
) -> None:
    public_tasks.clear_cache()
    first = suite_digest("v2")
    assert first == suite_digest("v2") != PINNED_V1_DIGEST
    settings = copied / "examples" / "bench" / "suite-v2" / "receipts.json"
    original = settings.read_text(encoding="utf-8")
    edit_json(settings, floor_limit=0.75)
    public_tasks.clear_cache()
    assert suite_digest("v2") != first
    settings.write_text(original, encoding="utf-8")
    public_tasks.clear_cache()
    assert suite_digest("v2") == first
    # One case changed with its manifest re-derived (a new data release): new digest.
    rows = cases(copied, "pilot")
    rows[0]["expected"]["item_count"] += 1
    write_split(copied, "pilot", rows, manifest=True)
    public_tasks.clear_cache()
    assert suite_digest("v2") != first


def test_the_v2_task_carries_its_origin_bar_and_checker() -> None:
    [task] = SUITES["v2"]
    settings = public_tasks.load_settings("receipts")
    assert task.is_public and task.floor_origin == "public_dataset"
    assert (task.kind, task.checker, task.preset) == ("text", "receipt_v1", "extractor")
    assert task.floor_limit == settings.floor_limit and task.tiers == {"public"}
    assert task.supports("forge") and task.supports("template") and task.supports("noisy")
    assert not task.supports("routed") and not task.supports("images-all-stages")
    assert task.supports_plain()
    v1 = {item.id: item for item in SUITES["v1"]}
    assert v1["sentiment-24"].floor_origin == "example_fixture"
    assert {item.floor_origin for item in SUITES["v1"] if item.id != "sentiment-24"} == {
        "synthetic_generated"
    }
    assert not any(item.is_public for item in SUITES["v1"])
    assert not v1["digitizer-24"].supports_plain() and not v1["arc-local-3"].supports_plain()


# ------------------------------------------------------------------- the loader


def test_the_committed_data_loads_with_disjoint_checked_splits() -> None:
    public_tasks.clear_cache()
    data = public_tasks.load_public("receipts")
    # The committed data release (Section 10.3). A new release changes these counts, the
    # v2 digest and this line together; every other test reads the counts from the data.
    assert {split: len(data.splits[split]) for split in public_tasks.PUBLIC_SPLITS} == {
        "pilot": 150,
        "rehearsal": 100,
        "evidence": 200,
        "delivery": 348,
    }
    assert data.task["rule_version"] == "receipts-v2"
    assert data.manifests["delivery"].rule_version == "receipts-v2"
    ids: set[str] = set()
    hashes: set[str] = set()
    for split, members in data.splits.items():
        manifest = data.manifests[split]
        assert manifest.split == split and manifest.count == len(members)
        assert manifest.cases_sha256 == public_tasks.cases_sha256(members)
        assert manifest.dataset == "naver-clova-ix/cord-v2" and manifest.license == "CC-BY-4.0"
        split_ids = {case.case_id for case in members}
        split_hashes = {public_tasks.case_sha256(case) for case in members}
        assert not split_ids & ids and not split_hashes & hashes
        ids |= split_ids
        hashes |= split_hashes
    for split in ("rehearsal", "evidence"):
        partition = data.manifests[split].partition
        assert partition is not None
        assert (partition.tuning, partition.held_out) == (
            (len(data.splits[split]) + 1) // 2,
            len(data.splits[split]) // 2,
        )
    assert data.manifests["evidence"].partition is not None
    assert data.manifests["delivery"].partition is None
    assert public_tasks.load_public("receipts") is data  # cached per process


def test_the_case_hash_ignores_metadata_and_matches_the_manifests() -> None:
    public_tasks.clear_cache()
    data = public_tasks.load_public("receipts")
    case = data.splits["evidence"][0]
    stamped = case.model_copy(update={"metadata": {"synthetic": True}})
    assert public_tasks.case_sha256(stamped) == public_tasks.case_sha256(case)
    entry = data.manifests["evidence"].cases[0]
    assert entry.case_id == case.case_id and entry.case_sha256 == public_tasks.case_sha256(case)


def test_the_hashes_equal_the_floor_provenance_functions() -> None:
    """Section 5.1: the bench's hashes are ``architect.floors.provenance``'s."""
    provenance = pytest.importorskip("architect.floors.provenance")
    public_tasks.clear_cache()
    data = public_tasks.load_public("receipts")
    for split in public_tasks.PUBLIC_SPLITS:
        members = data.splits[split]
        assert provenance.cases_sha256(members) == public_tasks.cases_sha256(members)
        assert all(
            provenance.case_sha256(case) == public_tasks.case_sha256(case) for case in members
        )


def test_a_changed_case_is_a_mismatch(copied: Path) -> None:
    rows = cases(copied, "evidence")
    rows[3]["expected"]["total"] = (rows[3]["expected"]["total"] or 0) + 1
    write_split(copied, "evidence", rows, manifest=False)
    refused("bench_dataset_mismatch")


def test_a_missing_file_is_missing(copied: Path) -> None:
    (copied / "examples" / "receipts" / "cases" / "pilot.jsonl").unlink()
    refused("bench_dataset_missing")


def test_a_missing_manifest_or_task_file_is_missing(copied: Path) -> None:
    (copied / "examples" / "receipts" / "manifests" / "delivery.json").unlink()
    refused("bench_dataset_missing")


def test_an_overlap_between_splits_is_a_mismatch_even_with_consistent_manifests(
    copied: Path,
) -> None:
    evidence = cases(copied, "evidence")
    delivery = cases(copied, "delivery")
    delivery[0] = evidence[0]  # one receipt in two splits
    write_split(copied, "delivery", delivery, manifest=True)
    refused("bench_dataset_mismatch")


def test_a_reordered_split_is_a_mismatch(copied: Path) -> None:
    rows = cases(copied, "delivery")
    rows[0], rows[1] = rows[1], rows[0]  # every case hash holds; the list hash does not
    write_split(copied, "delivery", rows, manifest=False)
    refused("bench_dataset_mismatch")


def test_changed_rules_are_a_mismatch(copied: Path) -> None:
    task = copied / "examples" / "receipts" / "task.json"
    data = json.loads(task.read_text(encoding="utf-8"))
    edit_json(task, rules=data["rules"] + " ")
    refused("bench_dataset_mismatch")


def test_a_broken_partition_is_a_mismatch(copied: Path) -> None:
    manifest = copied / "examples" / "receipts" / "manifests" / "evidence.json"
    edit_json(manifest, partition={"rule": "x", "tuning": 101, "held_out": 99})
    refused("bench_dataset_mismatch")


def test_an_expected_value_outside_the_schema_is_a_mismatch(copied: Path) -> None:
    rows = cases(copied, "pilot")
    rows[0]["expected"]["item_count"] = None  # item_count is never null
    write_split(copied, "pilot", rows, manifest=True)
    refused("bench_dataset_mismatch")


@pytest.mark.parametrize(
    "updates",
    [
        {"invalid_policy": "fail_partition", "max_invalid_fraction": 0.05},
        {"floor_limit": 1.5},
        {"source": "elsewhere"},
        {"input_limit_chars": 10},
        {"surprise": True},
    ],
)
def test_bad_settings_are_an_unknown_suite(copied: Path, updates: dict[str, object]) -> None:
    edit_json(copied / "examples" / "bench" / "suite-v2" / "receipts.json", **updates)
    refused("bench_suite_unknown")
    with pytest.raises(BenchError) as caught:
        suite_digest("v2")
    assert caught.value.code == "bench_suite_unknown"
    assert suite_digest("v1") == PINNED_V1_DIGEST  # a broken v2 never stops v1


def test_missing_settings_are_an_unknown_suite(copied: Path) -> None:
    (copied / "examples" / "bench" / "suite-v2" / "receipts.json").unlink()
    refused("bench_suite_unknown")


def test_the_settings_hash_is_the_canonical_json_of_the_parsed_settings() -> None:
    public_tasks.clear_cache()
    settings = public_tasks.load_settings("receipts")
    assert settings.settings_sha256 == hashlib.sha256(
        canonical_json(settings.model_dump(mode="json")).encode("utf-8")
    ).hexdigest()
    # Frozen for the evaluation run at the pilot's bar (docs/bench/prereg-nebius.md).
    assert settings.frozen is True and settings.task_id == "receipts"
    assert settings.floor_limit == 0.4


# ------------------------------------------------------------- the cell Spec


def test_the_receipts_spec_quotes_the_rules_and_takes_the_settings() -> None:
    [task] = SUITES["v2"]
    public_tasks.clear_cache()
    data = public_tasks.load_public("receipts")
    settings = data.settings
    spec = task.spec("lightning", "default", suite="v2")
    assert spec.task == data.rules == json.loads(
        (RECEIPTS / "task.json").read_text(encoding="utf-8")
    )["rules"]
    # The prompt rules are RULES.md's block word for word, under the hash every manifest pins.
    assert f"```text\n{spec.task}\n```" in (RECEIPTS / "RULES.md").read_text(encoding="utf-8")
    assert hashlib.sha256(spec.task.encode("utf-8")).hexdigest() == data.task["rules_sha256"]
    assert {manifest.rules_sha256 for manifest in data.manifests.values()} == {
        data.task["rules_sha256"]
    }
    assert spec.input_schema == data.task["input_schema"]
    assert spec.output_schema == data.task["output_schema"]
    assert spec.floor.kind == FloorKind.HELD_OUT_TASKS and spec.floor.limit == settings.floor_limit
    assert spec.floor.dataset == "bench-receipts-v2-evidence"
    assert spec.floor.minimum_samples == len(data.splits["evidence"]) // 2
    assert spec.budget == settings.run_budget and spec.model_target == "lightning"
    assert spec.preset_hint == "extractor" and spec.pipeline == ()
    rehearsal = task.spec("lightning", "default", suite="v2", floor_split="rehearsal")
    assert rehearsal.floor.dataset == "bench-receipts-v2-rehearsal"
    assert rehearsal.floor.minimum_samples == len(data.splits["rehearsal"]) // 2
    assert task.spec("lightning", "default", suite="v2", seed=11, size=7) == spec
    if not ARM_FIELDS_DECLARED:
        # WA's Spec fields are not in this build: nothing is claimed that the Spec lacks.
        assert arm_fields({"reply_json": "first", "plain": True}) == {}
        return
    assert getattr(spec, "invalid_policy") == settings.invalid_policy  # noqa: B009
    assert getattr(spec, "max_invalid_fraction") == settings.max_invalid_fraction  # noqa: B009
    assert getattr(spec, "input_limit_chars") == settings.input_limit_chars  # noqa: B009
    assert getattr(spec, "reply_json") == settings.reply_json  # noqa: B009
    assert getattr(spec, "plain") is False  # noqa: B009
    plain = task.spec("lightning", "default", suite="v2", plain=True, reply_json="last")
    assert getattr(plain, "plain") is True and getattr(plain, "reply_json") == "last"  # noqa: B009
    assert plain.spec_hash != spec.spec_hash


def test_v1_specs_are_unchanged_at_the_default_arm_settings() -> None:
    for task in SUITES["v1"]:
        base = task.spec("fake", "default", seed=7, size=24)
        assert task.spec("fake", "default", seed=7, size=24, plain=False) == base
        assert task.spec("fake", "default", seed=7, size=24, reply_json=None) == base
        assert base.spec_hash == task.spec("fake", "default", seed=7, size=24).spec_hash


def test_every_smoke_cell_hashes_as_the_committed_baseline() -> None:
    """Defaults change nothing (Section 1): the sized v1 smoke specs hash as before."""
    baseline = read_rows(ROOT / "examples" / "bench" / "suite-v1" / "smoke-baseline.jsonl")
    config = BenchConfig(tier="smoke", provider="fake", variants=("default", "noisy"))
    ctx = _Context(config, profile=None, factory=None, prices=load_prices(), clock=lambda: 0.0)
    expected = {(row.task_id, row.variant): row.spec_hash for row in baseline}
    seen = 0
    for cell in plan(config, ("fake",)):
        sized = _size_cell(ctx, cell)
        if sized.spec is None or sized.code is not None:
            continue  # a skipped cell (exam-scan-40 without Pillow)
        assert sized.spec.spec_hash == expected[(cell.task.id, cell.variant)], cell.task.id
        seen += 1
    assert seen >= 7


def test_receipts_sizing_follows_the_cell() -> None:
    [task] = SUITES["v2"]
    public_tasks.clear_cache()
    data = public_tasks.load_public("receipts")
    spec = task.spec("fake", "default", suite="v2")
    shown = [*data.splits["evidence"], *data.splits["delivery"][:20]]
    request = task.sizing_request((), spec=spec, cases=shown)
    largest = max(len(canonical_json(case.task)) for case in shown)
    limit = getattr(spec, "input_limit_chars", None)
    allowance = limit if isinstance(limit, int) else 2048
    schema = json.dumps(spec.output_schema, sort_keys=True)
    assert len(request.prompt) == len(spec.task) + len(schema) + 2 * min(largest, allowance) + 1024
    assert task.max_calls_per_run("default", plain=True) == 1
    assert task.max_calls_per_run("default") == 4
    v1 = {item.id: item for item in SUITES["v1"]}
    assert v1["invoice-40"].max_calls_per_run("default", plain=True) == 4  # v1 sizing kept
    assert len(v1["sentiment-24"].sizing_request(()).prompt) >= 6144


def test_noisy_receipts_add_one_item_and_stay_schema_valid() -> None:
    public_tasks.clear_cache()
    data = public_tasks.load_public("receipts")
    answer: JsonValue = {"total": 1200, "subtotal": None, "tax": 100, "item_count": 2}
    schema = data.task_schema("output_schema")
    wrong = _corrupt(answer, schema)
    assert wrong == {"total": 1200, "subtotal": None, "tax": 100, "item_count": 3}
    Draft202012Validator(schema).validate(wrong)


# ------------------------------------------------------------------ mirrors


def test_the_checker_names_mirror_the_floor_table() -> None:
    bench = set(get_args(suite_module.CheckerName))
    floors = set(get_args(checkers.CheckerName))
    if "receipt_v1" in floors:
        assert bench == floors
    else:  # WA's receipt_v1 (Section 6) is not in this build yet
        assert bench == floors | {"receipt_v1"}


def test_the_arm_labels_mirror_the_spec() -> None:
    if not ARM_FIELDS_DECLARED:
        pytest.skip("WA's Phase 14 Spec literals are not in this build")
    assert get_args(ReplyJson) == get_args(getattr(spec_module, "ReplyJson"))  # noqa: B009
    assert get_args(InvalidPolicy) == get_args(getattr(spec_module, "InvalidPolicy"))  # noqa: B009
    assert getattr(spec_module, "PLAIN_PRESETS") == PLAIN_PRESETS  # noqa: B009
