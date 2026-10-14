"""Offline helpers for suite v2 (Phase 14): data copies, row fixtures, the receipt checker.

``receipt_v1`` belongs to ``architect.floors.checkers`` (Section 6). Until that checker
is in the build, ``install_receipt_checker`` puts a stand-in with the same semantics
into the bench runner's table, so a fake suite v2 run can be checked end to end; once
the real checker exists the stand-in is never installed.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from pydantic import JsonValue

from architect.bench import public_tasks
from architect.bench import runner as bench_runner
from architect.bench.rows import BenchRow, encode_pass_bits
from architect.floors import EvaluationCase
from architect.floors.checkers import CHECKERS, FieldReport
from architect.floors.checkers import field_report as floor_field_report

REPOSITORY = Path(__file__).resolve().parents[1]
FIELDS = ("total", "subtotal", "tax", "item_count")
BENCH_ID = "3c1e4f52-8a6b-4d1e-9b2a-7c5d4e3f2a10"


def receipt_report(expected: JsonValue, output: JsonValue) -> FieldReport:
    """Section 6: a field matches on equal non-bool ints, or null against null."""
    per_field: dict[str, tuple[int, int]] = {}
    for name in FIELDS:
        assert isinstance(expected, dict)
        want = expected[name]
        got = output.get(name, ...) if isinstance(output, dict) else ...
        matched = (want is None and got is None) or (
            type(want) is int and type(got) is int and got == want
        )
        per_field[name] = (1 if matched else 0, 1)
    return FieldReport(
        fields=4, matched=sum(pair[0] for pair in per_field.values()), per_field=per_field
    )


def receipt_v1(expected: JsonValue, output: JsonValue) -> bool:
    return receipt_report(expected, output).matched == 4


def install_receipt_checker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the runner's checker table hold ``receipt_v1`` (a stand-in until WA's lands)."""
    if "receipt_v1" in CHECKERS:
        return
    monkeypatch.setattr(
        bench_runner, "CHECKERS", MappingProxyType({**CHECKERS, "receipt_v1": receipt_v1})
    )
    original: Callable[..., FieldReport] = floor_field_report

    def field_report(expected: JsonValue, output: JsonValue, *, checker: str) -> FieldReport:
        if checker == "receipt_v1":
            return receipt_report(expected, output)
        return original(expected, output, checker=checker)

    monkeypatch.setattr(bench_runner, "field_report", field_report)


def copy_public(root: Path) -> Path:
    """A checkout-shaped copy of the v2 settings and the receipts data under ``root``."""
    settings = root / "examples" / "bench" / "suite-v2"
    settings.mkdir(parents=True)
    shutil.copy2(REPOSITORY / "examples" / "bench" / "suite-v2" / "receipts.json", settings)
    shutil.copytree(REPOSITORY / "examples" / "receipts", root / "examples" / "receipts")
    return root


def cases(root: Path, split: str) -> list[dict[str, Any]]:
    path = root / "examples" / "receipts" / "cases" / f"{split}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def write_split(root: Path, split: str, rows: list[dict[str, Any]], *, manifest: bool) -> None:
    """Write a split's cases; with ``manifest`` also re-derive its manifest entries,
    count, ``cases_sha256`` and partition, as the data script would."""
    base = root / "examples" / "receipts"
    text = "".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows
    )
    (base / "cases" / f"{split}.jsonl").write_text(text, encoding="utf-8", newline="\n")
    if not manifest:
        return
    path = base / "manifests" / f"{split}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    parsed = [EvaluationCase.model_validate(row) for row in rows]
    sides = {
        case_id: "tuning" if position % 2 == 0 else "held_out"
        for position, case_id in enumerate(sorted(row["case_id"] for row in rows))
    }
    entries = []
    for row, case in zip(rows, parsed, strict=True):
        entry: dict[str, Any] = {
            "case_id": row["case_id"],
            "source": "train/0",
            "case_sha256": public_tasks.case_sha256(case),
        }
        if "partition" in data:
            entry["partition"] = sides[row["case_id"]]
        entries.append(entry)
    data["cases"] = entries
    data["count"] = len(rows)
    data["cases_sha256"] = public_tasks.cases_sha256(parsed)
    if "partition" in data:
        data["partition"]["tuning"] = (len(rows) + 1) // 2
        data["partition"]["held_out"] = len(rows) // 2
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")


def edit_json(path: Path, **updates: Any) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(updates)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")


def bits(*values: int) -> list[bool]:
    return [bool(value) for value in values]


def public_row(**overrides: Any) -> BenchRow:
    """A suite v2 delivery row; delivery pass bits from ``passed`` when given.

    ``invalid`` and ``over_budget`` mark which runs ended that way: the row then
    carries their outcome bits and totals, as the runner writes them. Without them the
    row has no outcome bits, the shape of rows written before the review fix.
    """
    from datetime import UTC, datetime

    from architect.bench.suite import suite_digest

    passed: list[bool] = overrides.pop("passed", [True, False, True, True])
    costs = overrides.pop("costs", tuple(0.001 for _ in passed))
    outcomes: dict[str, Any] = {}
    for name in ("invalid", "over_budget"):
        marked: list[bool] | None = overrides.pop(name, None)
        if marked is not None:
            outcomes[f"delivery_{name}"] = sum(marked)
            outcomes[f"delivery_{name}_bits"] = encode_pass_bits(marked)
    sha = hashlib.sha256(b"delivery-fixture").hexdigest()
    values: dict[str, Any] = {
        "bench_id": BENCH_ID,
        "timestamp": datetime(2026, 10, 12, 9, 0, tzinfo=UTC),
        "label": "A3",
        "suite_version": "v2",
        "suite_digest": suite_digest("v2"),
        "task_id": "receipts",
        "tier": "public",
        "kind": "text",
        "provider": "openai",
        "model": "lightning",
        "preset": "extractor",
        "variant": "default",
        "params": {"verifier_depth": 0, "retries": 0},
        "components": (),
        "max_candidates": 1,
        "seed": 7,
        "size": 200,
        "repeat": 0,
        "spec_hash": "a" * 64,
        "preset_fingerprint": "b" * 64,
        "prices_sha256": "c" * 64,
        "architect_commit": None,
        "pages_manifest_sha256": None,
        "licensed": True,
        "held_out_score": 0.93,
        "held_out_count": 100,
        "floor_limit": 0.8,
        "tuning_score": 0.95,
        "tuning_pass_bits": None,
        "tuning_cases": 100,
        "field_accuracy": None,
        "delivery_score": sum(passed) / len(passed),
        "delivery_cases": len(passed),
        "stages": (1, 2, 4),
        "failures": (),
        "calls": 600,
        "tokens_in": 120_000,
        "tokens_out": 30_000,
        "cached_tokens_in": 0,
        "cost_usd": 0.05,
        "reserved_usd": 0.5,
        "per_call_reservation_usd": 0.0002,
        "wall_s": 120.0,
        "image_inputs": 0,
        "image_tokens_estimate": 0,
        "evidence": "live_billed",
        "floor_origin": "public_dataset",
        "arc": None,
        "api_model": "nvidia/Nemotron-3_5-Lightning",
        "plain": True,
        "reply_json": "first",
        "invalid_policy": "count_as_miss",
        "floor_split": "evidence",
        "delivery_split": "delivery",
        "floor_cases_sha256": "e" * 64,
        "delivery_cases_sha256": sha,
        "suite_frozen": True,
        "tuning_replay": False,
        "held_out_correct": 93,
        "held_out_invalid": 1,
        "held_out_over_budget": 0,
        "held_out_mean_cost_usd": 0.0002,
        "compile_cost_usd": 0.04,
        "delivery_pass_bits": encode_pass_bits(passed),
        "delivery_invalid": 0,
        "delivery_over_budget": 0,
        "delivery_cost_usd": float(sum(costs)),
        "delivery_calls": len(passed),
        "delivery_zero_call_runs": 0,
        "delivery_p50_s": 1.25,
        "delivery_p95_s": 2.5,
        "delivery_run_costs_usd": tuple(costs),
        "delivery_field_accuracy": 0.9,
    }
    values.update(outcomes)
    values.update(overrides)
    return BenchRow.model_validate(values)


def table_rows(n11: int, n10: int, n01: int, n00: int) -> tuple[list[bool], list[bool]]:
    """Base and candidate pass bits for a paired 2x2 table, in a fixed order."""
    base = [True] * n11 + [True] * n10 + [False] * n01 + [False] * n00
    cand = [True] * n11 + [False] * n10 + [True] * n01 + [False] * n00
    return base, cand

