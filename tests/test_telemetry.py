"""Fake-only branch tests for telemetry privacy and persistence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from architect.spec import FloorKind, Lifecycle
from architect.telemetry import TelemetryRow, TelemetryStore, categorical


def metadata(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "run_id": str(uuid4()),
        "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
        "domain": "general",
        "task_type": categorical("synthetic task class"),
        "spec_hash": "a" * 64,
        "harness_version": 1,
        "lifecycle": Lifecycle.EXPIRE,
        "preset_origin": "toy",
        "component_set": (categorical("simple component"),),
        "model": "fake",
        "model_params": {"temperature": 0.0, "max_output_tokens": 64},
        "tokens_in": 10,
        "tokens_out": 4,
        "cost_usd": 0.0001,
        "wall_time_s": 0.1,
        "floor_type": FloorKind.HELD_OUT_TASKS,
        "floor_score": 0.9,
        "floor_limit": 0.8,
        "accepted": True,
    }
    values.update(overrides)
    return values


def row(**overrides: Any) -> TelemetryRow:
    return TelemetryRow.model_validate(metadata(**overrides))


def test_categories_are_normalized_deterministic_and_idempotent() -> None:
    assert categorical(" GENERAL ") == "general"
    normalized = "unlisted label"
    expected = "h_" + hashlib.sha256(normalized.encode()).hexdigest()[:32]
    assert categorical(" Unlisted Label ") == expected
    assert categorical(expected) == expected
    assert categorical("ＦＡＫＥ") == "fake"
    assert categorical("é") == categorical("e\u0301")
    assert categorical("another label") != expected
    assert len(categorical("x" * 10000)) == 34
    assert categorical("").startswith("h_")
    with pytest.raises(TypeError):
        categorical(42)  # type: ignore[arg-type]


def test_schema_round_trip_and_defaults() -> None:
    original = row(floor_score=None)
    restored = TelemetryRow.model_validate_json(original.model_dump_json())
    assert original == restored
    assert original.drift_flag is False
    with pytest.raises(ValidationError):
        original.accepted = False


@pytest.mark.parametrize("field", ["domain", "task_type", "preset_origin", "model"])
@pytest.mark.parametrize("secret", ["key", "short secret", "x" * 65, "h_" + "z" * 32])
def test_all_categories_reject_even_short_free_text(field: str, secret: str) -> None:
    with pytest.raises(ValidationError):
        row(**{field: secret})


@pytest.mark.parametrize("secret", ["key", "x" * 100, "h_" + "a" * 31, {"secret": 1}])
def test_component_categories_are_nested_privacy_boundary(secret: object) -> None:
    with pytest.raises(ValidationError):
        row(component_set=(secret,))


@pytest.mark.parametrize(
    "params",
    [
        {"prompt": 1},
        {"temperature": "0.1"},
        {"temperature": "key"},
        {"temperature": {"secret": 1}},
        {"retries": [1]},
        {"retries": None},
        {"temperature": float("nan")},
        {"temperature": float("inf")},
        {"temperature": float("-inf")},
        {"x" * 65: 1},
        [],
        None,
    ],
)
def test_parameter_keys_and_values_are_closed(params: object) -> None:
    with pytest.raises(ValidationError):
        row(model_params=params)


def test_all_numeric_parameter_types_and_keys() -> None:
    params = {
        "temperature": 0.2,
        "max_output_tokens": 1,
        "context_window": 8192,
        "retries": 0,
        "verifier_depth": 2,
        "terseness": True,
    }
    assert row(model_params=params).model_params == params
    assert type(row(model_params={"retries": 1}).model_params["retries"]) is int
    assert type(row(model_params={"terseness": True}).model_params["terseness"]) is bool


@pytest.mark.parametrize("field", ["tokens_in", "tokens_out", "cost_usd", "wall_time_s"])
@pytest.mark.parametrize("value", [-1, "1", True, float("nan"), float("inf")])
def test_usage_is_strict_nonnegative_finite(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        row(**{field: value})


@pytest.mark.parametrize("field", ["floor_score", "floor_limit"])
@pytest.mark.parametrize("value", [-0.1, 1.1, float("nan"), float("inf"), "0.9", True])
def test_scores_are_strict_finite_fractions(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        row(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "short secret"),
        ("run_id", uuid4().hex),
        ("run_id", "x" * 100),
        ("spec_hash", "short secret"),
        ("spec_hash", "g" * 64),
        ("spec_hash", "a" * 65),
        ("harness_version", 0),
        ("harness_version", True),
        ("harness_version", "1"),
        ("lifecycle", "secret"),
        ("floor_type", "secret"),
        ("timestamp", "secret"),
        ("accepted", "true"),
        ("drift_flag", 1),
        ("output", "key"),
        ("prompt", "key"),
    ],
)
def test_other_fields_cannot_hide_text(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        row(**{field: value})


def test_identifiers_normalize_case() -> None:
    run_id = str(uuid4())
    result = row(run_id=run_id.upper(), spec_hash="A" * 64)
    assert result.run_id == run_id
    assert result.spec_hash == "a" * 64


def test_append_round_trip_index_and_reopen(tmp_path: Path) -> None:
    first, second = row(), row(accepted=False, floor_score=None)
    with TelemetryStore(tmp_path) as store:
        assert store.rows() == []
        store.append(first)
        initial = (tmp_path / "runs.jsonl").read_bytes()
        store.append(second)
        assert (tmp_path / "runs.jsonl").read_bytes().startswith(initial)
        assert store.rows() == [first, second]
    with TelemetryStore(tmp_path) as reopened:
        assert reopened.rows() == [first, second]
    with sqlite3.connect(tmp_path / "index.sqlite3") as connection:
        records = connection.execute(
            "SELECT run_id, accepted, byte_offset, byte_length FROM runs ORDER BY byte_offset"
        ).fetchall()
        assert records[0] == (first.run_id, 1, 0, len(initial))
        assert records[1][0:3] == (second.run_id, 0, len(initial))
        columns = {info[1] for info in connection.execute("PRAGMA table_info(runs)")}
        assert "prompt" not in columns and "output" not in columns


def test_duplicate_uuid_refused_without_journal_changes(tmp_path: Path) -> None:
    first = row()
    with TelemetryStore(tmp_path) as store:
        store.append(first)
        before = (tmp_path / "runs.jsonl").read_bytes()
        with pytest.raises(ValueError, match="duplicate"):
            store.append(first)
        assert (tmp_path / "runs.jsonl").read_bytes() == before
        assert store.rows() == [first]


def test_revalidation_blocks_mutation_and_unsafe_construction(tmp_path: Path) -> None:
    poisoned = row()
    poisoned.model_params["temperature"] = "secret"  # type: ignore[assignment]
    forged = TelemetryRow.model_construct(**metadata(domain="secret"))
    with TelemetryStore(tmp_path) as store:
        for invalid in (poisoned, forged):
            with pytest.raises(ValidationError):
                store.append(invalid)
        assert store.rows() == []
        assert (tmp_path / "runs.jsonl").read_bytes() == b""


def test_jsonl_commit_without_sqlite_index_is_recovered(tmp_path: Path) -> None:
    saved = row()
    with TelemetryStore(tmp_path):
        pass
    with (tmp_path / "runs.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(saved.model_dump_json() + "\n")
    with TelemetryStore(tmp_path) as recovered:
        assert recovered.rows() == [saved]
        with pytest.raises(ValueError, match="duplicate"):
            recovered.append(saved)
    with sqlite3.connect(tmp_path / "index.sqlite3") as connection:
        assert connection.execute("SELECT run_id FROM runs").fetchall() == [(saved.run_id,)]


@pytest.mark.parametrize("tail", [b'{"run_id":', b'{"run_id":"\xff'])
def test_torn_final_record_removed_without_losing_complete_rows(
    tmp_path: Path, tail: bytes
) -> None:
    saved, next_row = row(), row()
    original = (saved.model_dump_json() + "\n").encode()
    (tmp_path / "runs.jsonl").write_bytes(original + tail)
    with TelemetryStore(tmp_path) as recovered:
        assert recovered.rows() == [saved]
        assert (tmp_path / "runs.jsonl").read_bytes() == original
        recovered.append(next_row)
        assert recovered.rows() == [saved, next_row]


def test_complete_final_record_without_newline_is_preserved(tmp_path: Path) -> None:
    saved = row()
    (tmp_path / "runs.jsonl").write_text(saved.model_dump_json(), encoding="utf-8")
    with TelemetryStore(tmp_path) as recovered:
        assert recovered.rows() == [saved]
    assert (tmp_path / "runs.jsonl").read_bytes().endswith(b"\n")


@pytest.mark.parametrize("tail", [b"invalid\n", b"{}\n", b"{}"])
def test_corrupt_or_privacy_invalid_records_fail_closed(tmp_path: Path, tail: bytes) -> None:
    path = tmp_path / "runs.jsonl"
    path.write_bytes(tail)
    with pytest.raises(ValueError, match="invalid"):
        TelemetryStore(tmp_path)
    assert path.read_bytes() == tail


def test_duplicate_journal_records_fail_closed(tmp_path: Path) -> None:
    saved = row()
    (tmp_path / "runs.jsonl").write_text((saved.model_dump_json() + "\n") * 2)
    with pytest.raises(ValueError, match="duplicate"):
        TelemetryStore(tmp_path)


def test_index_failure_after_durable_write_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = row()
    with TelemetryStore(tmp_path) as store:

        def fail_index(*args: object) -> None:
            raise sqlite3.OperationalError("simulated index failure")

        with monkeypatch.context() as patch:
            patch.setattr(store, "_index", fail_index)
            with pytest.raises(sqlite3.OperationalError):
                store.append(saved)
        assert store.rows() == [saved]
        with pytest.raises(ValueError, match="duplicate"):
            store.append(saved)
    with TelemetryStore(tmp_path) as reopened:
        assert reopened.rows() == [saved]


def test_same_instance_and_separate_stores_serialize_threads(tmp_path: Path) -> None:
    records = [row() for _ in range(24)]
    with TelemetryStore(tmp_path) as first, TelemetryStore(tmp_path) as second:

        def append(item: tuple[int, TelemetryRow]) -> None:
            index, record = item
            (first if index % 2 else second).append(record)

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(append, enumerate(records)))
        restored = first.rows()
        assert len(restored) == len(records)
        assert {record.run_id for record in restored} == {record.run_id for record in records}
        assert first.rows() == second.rows()
    assert len((tmp_path / "runs.jsonl").read_text().splitlines()) == len(records)


def test_close_is_idempotent_and_closed_operations_fail(tmp_path: Path) -> None:
    store = TelemetryStore(tmp_path)
    store.close()
    store.close()
    with pytest.raises(ValueError, match="closed"):
        store.append(row())
    with pytest.raises(ValueError, match="closed"):
        store.rows()
    with pytest.raises(ValueError, match="closed"):
        store.__enter__()


def test_persisted_json_has_only_contract_fields(tmp_path: Path) -> None:
    record = row()
    with TelemetryStore(tmp_path) as store:
        store.append(record)
    payload = json.loads((tmp_path / "runs.jsonl").read_text())
    assert set(payload) == set(TelemetryRow.model_fields)
    assert all(type(v) in (int, float, bool) for v in payload["model_params"].values())


def test_append_does_not_reparse_or_reindex_history(tmp_path: Path) -> None:
    records = [row() for _ in range(200)]
    with TelemetryStore(tmp_path) as store:
        for record in records[:100]:
            store.append(record)
        with (
            patch.object(
                TelemetryRow, "model_validate_json", wraps=TelemetryRow.model_validate_json
            ) as parse,
            patch.object(store, "_index", wraps=store._index) as index,
        ):
            for record in records[100:]:
                store.append(record)
            assert parse.call_count == 0
            assert index.call_count == 100
        assert store.rows() == records


def test_append_reconciles_only_unindexed_tail(tmp_path: Path) -> None:
    records = [row() for _ in range(5)]
    with TelemetryStore(tmp_path) as store:
        for record in records[:3]:
            store.append(record)
        with (tmp_path / "runs.jsonl").open("ab") as stream:
            stream.write((records[3].model_dump_json() + "\n").encode())
        with patch.object(
            TelemetryRow, "model_validate_json", wraps=TelemetryRow.model_validate_json
        ) as parse:
            store.append(records[4])
            assert parse.call_count == 1
        assert store.rows() == records


def test_indexed_journal_truncation_fails_closed(tmp_path: Path) -> None:
    saved = row()
    with TelemetryStore(tmp_path) as store:
        store.append(saved)
        (tmp_path / "runs.jsonl").write_bytes(b"")
        with pytest.raises(ValueError, match="shorter than its index"):
            store.append(row())
        with pytest.raises(ValueError, match="shorter than its index"):
            store.rows()


def test_duplicate_unindexed_tail_fails_closed(tmp_path: Path) -> None:
    saved = row()
    with TelemetryStore(tmp_path) as store:
        store.append(saved)
        with (tmp_path / "runs.jsonl").open("ab") as stream:
            stream.write((saved.model_dump_json() + "\n").encode())
        with pytest.raises(ValueError, match="duplicate"):
            store.append(row())


def test_existing_store_repairs_torn_tail_before_next_append(tmp_path: Path) -> None:
    saved, next_row = row(), row()
    with TelemetryStore(tmp_path) as store:
        store.append(saved)
        with (tmp_path / "runs.jsonl").open("ab") as stream:
            stream.write(b'{"run_id":')
        store.append(next_row)
        assert store.rows() == [saved, next_row]


def test_lock_wait_bounds_only_its_context_and_probe_reports_busy(tmp_path: Path) -> None:
    import time

    from architect.telemetry.store import lock_wait

    with pytest.raises(ValueError):
        with lock_wait(float("nan")):
            pass
    with TelemetryStore(tmp_path) as store:
        holder = sqlite3.connect(tmp_path / "index.sqlite3", isolation_level=None)
        try:
            holder.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            with lock_wait(0.1), pytest.raises(sqlite3.OperationalError) as busy:
                store.append(row())
            assert time.monotonic() - started < 5
            assert busy.value.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY
            with lock_wait(0.1), pytest.raises(sqlite3.OperationalError):
                store.probe()
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        store.probe()
        store.append(row())  # The default wait is back outside the bounded context.
        assert len(store.rows()) == 1


def test_image_inputs_column_defaults_to_zero_and_is_strict(tmp_path: Path) -> None:
    assert row().image_inputs == 0
    assert row(image_inputs=3).image_inputs == 3
    for invalid in (-1, "2", 1.0, True, None):
        with pytest.raises(ValidationError):
            row(image_inputs=invalid)
    # Rows written before the column existed read back with 0.
    legacy = json.loads(row().model_dump_json())
    del legacy["image_inputs"]
    assert TelemetryRow.model_validate_json(json.dumps(legacy)).image_inputs == 0
    store = TelemetryStore(tmp_path / "telemetry")
    store.append(row(image_inputs=2))
    assert store.rows()[0].image_inputs == 2
    schema = TelemetryRow.model_json_schema()["properties"]["image_inputs"]
    assert schema["type"] == "integer" and schema["minimum"] == 0
