"""Content-free bench rows: round trip, strictness, closed labels (Phase 8 WP3)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from bench_public_support import public_row
from pydantic import ValidationError

from architect.bench import CELL_CODES, CODES
from architect.bench.rows import (
    ArcFields,
    BenchError,
    BenchRow,
    ForgeFields,
    append_rows,
    architect_commit,
    decode_pass_bits,
    encode_pass_bits,
    preset_fingerprint,
    prices_sha256,
    read_rows,
    summarize,
    write_rows,
)
from architect.bench.suite import suite_digest
from architect.models.pricing import DEFAULT_PRICES
from architect.telemetry.schema import categorical

SENTINEL = "SENTINEL-PROMPT-Do-not-leak-7f3a"
BENCH_ID = "0b6c9a52-3f1e-4d8a-9c0e-5a8b7c6d5e4f"


def make_row(**overrides: Any) -> BenchRow:
    values: dict[str, Any] = {
        "bench_id": BENCH_ID,
        "timestamp": datetime(2026, 9, 26, 12, 0, tzinfo=UTC),
        "label": "base-2026-09-26",
        "suite_version": "v1",
        "suite_digest": suite_digest("v1"),
        "task_id": "exam-clean-40",
        "tier": "smoke",
        "kind": "vision",
        "provider": "fake",
        "model": "fake",
        "preset": "extractor",
        "variant": "default",
        "params": {"verifier_depth": 1, "retries": 0},
        "components": (categorical("extraction"), categorical("self_check")),
        "max_candidates": 2,
        "seed": 7,
        "size": 24,
        "repeat": 0,
        "spec_hash": "a" * 64,
        "preset_fingerprint": "b" * 64,
        "prices_sha256": "c" * 64,
        "architect_commit": None,
        "pages_manifest_sha256": "d" * 64,
        "licensed": True,
        "held_out_score": 1.0,
        "held_out_count": 12,
        "floor_limit": 0.9,
        "tuning_score": 1.0,
        "tuning_pass_bits": encode_pass_bits([True] * 12),
        "tuning_cases": 12,
        "field_accuracy": 1.0,
        "delivery_score": None,
        "delivery_cases": 0,
        "stages": (1, 2, 4),
        "failures": (),
        "calls": 40,
        "tokens_in": 7200,
        "tokens_out": 1600,
        "cached_tokens_in": 0,
        "cost_usd": 0.0088,
        "reserved_usd": 0.5,
        "per_call_reservation_usd": 0.01,
        "wall_s": 3.5,
        "image_inputs": 40,
        "image_tokens_estimate": 4000,
        "evidence": "synthetic_fake",
        "floor_origin": "synthetic_generated",
        "arc": None,
    }
    values.update(overrides)
    return BenchRow(**values)


def test_round_trip_is_sorted_jsonl(tmp_path: Path) -> None:
    rows = [make_row(), make_row(repeat=1, licensed=False, failures=("primary_floor_failed",))]
    path = tmp_path / "bench" / "rows.jsonl"
    write_rows(path, rows)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        data = json.loads(line)
        assert list(data) == sorted(data)
    assert read_rows(path) == rows
    append_rows(path, [make_row(repeat=2)])
    assert [row.repeat for row in read_rows(path)] == [0, 1, 2]
    write_rows(path, rows[:1])  # write replaces
    assert len(read_rows(path)) == 1


def test_extra_fields_are_forbidden_and_rows_are_frozen() -> None:
    with pytest.raises(ValidationError):
        make_row(prompt=SENTINEL)
    with pytest.raises(ValidationError):
        make_row(case_id="exam-clean-7-000")
    row = make_row()
    with pytest.raises(ValidationError):
        row.licensed = False
    with pytest.raises(ValidationError):
        make_row(cost_usd=float("nan"))
    with pytest.raises(ValidationError):
        make_row(wall_s=-1.0)
    with pytest.raises(ValidationError):
        make_row(held_out_score=1.5)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_id", "exam-clean-41"),
        ("tier", "nightly"),
        ("kind", "audio"),
        ("variant", "loud"),
        ("provider", "gemini"),
        ("evidence", "live"),
        ("floor_origin", "lectern"),
        ("preset", "oracle"),
        ("params", {"image_detail": "high"}),
        ("components", ("self_check",)),  # raw names only as hashed categories
    ],
)
def test_closed_labels(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        make_row(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("label", SENTINEL + " with spaces"),
        ("label", "x" * 65),
        ("model", "a model with spaces"),
        ("model", "-leading"),
        ("failures", ("Not A Code",)),
        ("spec_hash", "A" * 64),
        ("suite_digest", "short"),
        ("architect_commit", "HEAD"),
        ("tuning_pass_bits", "zz"),
        ("bench_id", "not-a-uuid"),
        ("bench_id", "{" + BENCH_ID + "}"),
        ("suite_version", "1"),
        ("suite_version", "V1"),
        ("suite_version", "v1 "),
        ("suite_version", "v1-beta"),
        ("suite_version", "latest"),
        ("suite_version", "v"),
    ],
)
def test_free_text_columns_are_pattern_bound(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        make_row(**{field: value})


def test_uuid_is_canonicalised() -> None:
    assert make_row(bench_id=BENCH_ID.upper()).bench_id == BENCH_ID


def test_sentinel_never_serialised(tmp_path: Path) -> None:
    # No column can carry content: a sentinel planted wherever a string is accepted is
    # rejected, and a valid row's JSON holds only labels, counts and hashes.
    for field in ("label", "model", "suite_version", "bench_id"):
        with pytest.raises(ValidationError):
            make_row(**{field: SENTINEL + " prompt text: what is 2 + 2?"})
    path = tmp_path / "rows.jsonl"
    write_rows(path, [make_row()])
    text = path.read_text(encoding="utf-8")
    assert SENTINEL not in text
    for forbidden in ("prompt", "output", "case_id", "image/png", "base64", "stem"):
        assert forbidden not in text


def test_arc_fields() -> None:
    arc = ArcFields(score=0.25, score_source="fixture_agent", model_binding="online_preview",
                    games=5, actions=24)
    row = make_row(task_id="arc-preview", tier="arc", kind="game", arc=arc,
                   pages_manifest_sha256=None)
    assert row.arc is not None and row.arc.compliance == "unverified"
    with pytest.raises(ValidationError):
        ArcFields(score=0.2, score_source="x", model_binding="y", games=1, actions=1,
                  compliance="verified")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ArcFields(score=2.0, score_source="x", model_binding="y", games=1, actions=1)


def test_unreadable_rows_are_a_closed_code(tmp_path: Path) -> None:
    missing = tmp_path / "missing.jsonl"
    with pytest.raises(BenchError) as caught:
        read_rows(missing)
    assert caught.value.code == "bench_rows_unreadable" == str(caught.value)
    broken = tmp_path / "broken.jsonl"
    broken.write_text('{"bench_id": 1}\n', encoding="utf-8")
    with pytest.raises(BenchError):
        read_rows(broken)
    extra = tmp_path / "extra.jsonl"
    data = json.loads(make_row().model_dump_json())
    data["prompt"] = SENTINEL
    extra.write_text(json.dumps(data) + "\n", encoding="utf-8")
    with pytest.raises(BenchError):
        read_rows(extra)
    blank = tmp_path / "blank.jsonl"
    blank.write_text("\n" + make_row().model_dump_json() + "\n\n", encoding="utf-8")
    assert len(read_rows(blank)) == 1


def test_pass_bits_are_hex_msb_first() -> None:
    assert encode_pass_bits([]) == ""
    assert encode_pass_bits([True]) == "80"
    assert encode_pass_bits([True, False, True, False, False, False, False, True]) == "a1"
    bits = [True, True, False] * 5
    encoded = encode_pass_bits(bits)
    assert encoded == "db6c"
    assert decode_pass_bits(encoded, len(bits)) == bits
    with pytest.raises(ValueError):
        decode_pass_bits("80", 9)


def test_summary_groups_repeats() -> None:
    rows = [
        make_row(repeat=0, held_out_score=1.0, cost_usd=0.01, wall_s=1.0),
        make_row(repeat=1, held_out_score=0.5, licensed=False, cost_usd=0.03, wall_s=3.0),
        make_row(task_id="sentiment-24", kind="text", held_out_score=None, licensed=False),
    ]
    summary = summarize(rows)
    first, second = summary.per_cell
    assert (first.task_id, first.repeats, first.licensed_count) == ("exam-clean-40", 2, 1)
    assert (first.mean_held_out, first.min_held_out, first.max_held_out) == (0.75, 0.5, 1.0)
    assert first.mean_cost_usd == pytest.approx(0.02)
    assert first.mean_wall_s == pytest.approx(2.0)
    assert second.mean_held_out is None and second.repeats == 1


def test_identity_columns() -> None:
    fingerprint = preset_fingerprint()
    assert len(fingerprint) == 64 and fingerprint == preset_fingerprint()
    assert prices_sha256(None) == hashlib.sha256(DEFAULT_PRICES.read_bytes()).hexdigest()
    commit = architect_commit()
    assert commit is None or (len(commit) == 40 and all(c in "0123456789abcdef" for c in commit))


def test_code_tables_are_closed_and_well_formed() -> None:
    assert len(CODES) == 35
    assert {"bench_held_out_access", "bench_no_cells", "bench_model_not_local"} <= CODES
    assert {"bench_resume_mismatch", "bench_resume_unsupported"} <= CODES  # Phase 11
    assert {  # Phase 14 Section 14
        "bench_task_unknown",
        "bench_split_unsupported",
        "bench_size_unsupported",
        "bench_delivery_all_unsupported",
        "bench_delivery_too_large",
        "bench_dataset_missing",
        "bench_dataset_mismatch",
        "bench_plain_variant",
        "bench_audit_unreadable",
        "bench_run_budget_too_small",  # Phase 14 review
    } <= CODES
    # ``bench run --audit`` raises the Forge audit's own code, so the bench table holds it.
    assert all(code.startswith("bench_") for code in CODES - {"audit_unwritable"})
    assert CELL_CODES == {
        "image_not_configured",
        "arc_unavailable",
        "over_max_usd",
        "extra_missing",
        "cancelled",
        "image_reservation_under_configured",
        "image_unresolved",
        "workspace_error",
        "forge_did_not_run",
        "host_memory_low",
        "route_did_not_run",
    }
    for code in CELL_CODES:
        make_row(failures=(code,))  # every cell code is a valid row failure


def test_suite_version_is_a_suite_key() -> None:
    from architect.bench.suite import SUITES

    for version in SUITES:
        assert make_row(suite_version=version).suite_version == version
    assert make_row(suite_version="v2").suite_version == "v2"  # a future bump still parses


FORGE_FIELDS: dict[str, Any] = {
    "mode": "agent",
    "agent_model": "fake",
    "checks": 3,
    "dryruns": 1,
    "measures": 2,
    "submissions": 1,
    "accepted": 1,
    "spawns": 0,
    "agent_cost_usd": 0.0,
    "agent_tokens_in": 1200,
    "agent_tokens_out": 300,
    "agent_wall_s": 4.5,
    "harness_nodes": 310,
    "harness_bytes": 2048,
    "subharnesses": 0,
    "language": 2,
}


def test_forge_and_template_are_variants_and_old_rows_still_validate(tmp_path: Path) -> None:
    rows = [make_row(variant=variant) for variant in ("default", "forge", "template")]
    path = tmp_path / "rows.jsonl"
    write_rows(path, rows)
    assert [row.variant for row in read_rows(path)] == ["default", "forge", "template"]
    # A row serialised before Phase 9 (no forge columns at all) still reads back.
    legacy = make_row().model_dump(mode="json")
    legacy.pop("forge", None)
    path.write_text(json.dumps(legacy, sort_keys=True) + "\n", encoding="utf-8")
    assert read_rows(path) == [make_row()]


def test_forge_fields_round_trip_and_are_content_free() -> None:
    fields = ForgeFields(**FORGE_FIELDS)
    assert ForgeFields.model_validate_json(fields.model_dump_json()) == fields
    assert set(fields.model_dump()) == set(FORGE_FIELDS)
    assert ForgeFields(**{**FORGE_FIELDS, "mode": "template", "agent_model": None}).mode == (
        "template"
    )
    with pytest.raises(ValidationError):
        fields.checks = 4


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mode", "forge"),
        ("agent_model", "a model with spaces"),
        ("checks", -1),
        ("measures", 1.0),
        ("accepted", True),
        ("agent_cost_usd", -0.01),
        ("agent_cost_usd", float("nan")),
        ("agent_cost_usd", "0.1"),
        ("agent_wall_s", float("inf")),
        ("harness_bytes", "2048"),
        ("language", 1),
    ],
)
def test_forge_fields_are_closed(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        ForgeFields(**{**FORGE_FIELDS, field: value})


def test_forge_fields_forbid_extra_and_missing() -> None:
    with pytest.raises(ValidationError):
        ForgeFields(**FORGE_FIELDS, source=SENTINEL)
    missing = dict(FORGE_FIELDS)
    del missing["language"]
    with pytest.raises(ValidationError):
        ForgeFields(**missing)


# --- Phase 9 WP2: BenchRow.forge and the ARC process metrics -------------------------


def test_bench_row_carries_forge_fields_and_defaults_to_none(tmp_path: Path) -> None:
    fields = ForgeFields(**FORGE_FIELDS)
    row = make_row(variant="forge", forge=fields)
    path = tmp_path / "rows.jsonl"
    write_rows(path, [row, make_row()])
    forge_row, default_row = read_rows(path)
    assert forge_row.forge == fields and default_row.forge is None
    # Build-time agent cost is its own column, never folded into the play cost.
    assert "agent_cost_usd" not in BenchRow.model_fields
    with pytest.raises(ValidationError):
        make_row(forge={**FORGE_FIELDS, "source": "text"})


def test_arc_process_metrics_are_optional_and_old_arc_rows_still_read(tmp_path: Path) -> None:
    arc = ArcFields(
        score=0.0, score_source="local", model_binding="offline", games=3, actions=80
    )
    assert arc.distinct_states_mean is None and arc.revisit_rate_mean is None
    assert arc.levels_completed_total is None and arc.calls_per_action is None
    legacy = make_row(task_id="arc-local-3", kind="game", arc=arc).model_dump(mode="json")
    for key in ("distinct_states_mean", "revisit_rate_mean", "levels_completed_total",
                "calls_per_action"):
        legacy["arc"].pop(key)
    path = tmp_path / "rows.jsonl"
    path.write_text(json.dumps(legacy, sort_keys=True) + "\n", encoding="utf-8")
    assert read_rows(path)[0].arc == arc
    full = arc.model_copy(
        update={
            "distinct_states_mean": 12.5,
            "revisit_rate_mean": 0.25,
            "levels_completed_total": 1,
            "calls_per_action": 0.4,
        }
    )
    assert ArcFields.model_validate_json(full.model_dump_json()) == full


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("distinct_states_mean", -1.0),
        ("revisit_rate_mean", 1.5),
        ("revisit_rate_mean", -0.1),
        ("levels_completed_total", -1),
        ("calls_per_action", float("nan")),
    ],
)
def test_arc_process_metrics_are_bounded(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        ArcFields(
            score=0.0,
            score_source="local",
            model_binding="offline",
            games=3,
            actions=80,
            **{field: value},
        )


def test_delivery_outcome_bits_agree_with_their_totals_and_the_passes(tmp_path: Path) -> None:
    """Review fix: which delivery runs were invalid or over budget. Rows written before
    it carry only the totals and still read."""
    passed = [True, False, False, False]
    invalid = [False, True, False, False]
    over_budget = [False, False, True, False]
    row = public_row(passed=passed, invalid=invalid, over_budget=over_budget)
    assert (row.delivery_invalid, row.delivery_over_budget) == (1, 1)
    assert row.delivery_invalid_bits is not None and row.delivery_over_budget_bits is not None
    assert decode_pass_bits(row.delivery_invalid_bits, 4) == invalid
    assert decode_pass_bits(row.delivery_over_budget_bits, 4) == over_budget
    write_rows(tmp_path / "rows.jsonl", [row])
    assert read_rows(tmp_path / "rows.jsonl") == [row]
    older = public_row(passed=passed, delivery_invalid=1, delivery_over_budget=1)
    assert older.delivery_invalid_bits is None and older.delivery_over_budget_bits is None
    for bad in (
        {"delivery_invalid": 2},  # the total disagrees with the bits
        {"delivery_invalid_bits": encode_pass_bits([True, False, False, False])},  # it passed
        {"delivery_over_budget_bits": encode_pass_bits(invalid)},  # invalid and over budget
        {"delivery_invalid_bits": ""},  # shorter than the case count
    ):
        with pytest.raises(ValidationError):
            public_row(passed=passed, invalid=invalid, over_budget=over_budget, **bad)

