"""Seeded ticket and invoice generators with exact oracle tables (Phase 8 WP3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from architect.bench.rows import BenchError
from architect.bench.suite import SUITES
from architect.bench.text_tasks import (
    _AMBIGUOUS,
    INVOICE_ITEMS,
    TICKET_LABELS,
    canonical_json,
    canonical_key,
    cases_sha256,
    generate_delivery_tickets,
    generate_invoices,
    generate_tickets,
    load_sentiment_cases,
    ticket_pool,
)
from architect.floors import EvaluationCase

MANIFESTS = Path(__file__).parents[1] / "examples" / "bench" / "suite-v1"
TASKS = {task.id: task for task in SUITES["v1"]}


def test_tickets_are_deterministic_and_prefix_stable() -> None:
    first, table = generate_tickets(7, 40)
    again, table_again = generate_tickets(7, 40)
    assert [c.model_dump() for c in first] == [c.model_dump() for c in again]
    assert table == table_again
    smaller, _ = generate_tickets(7, 24)
    assert [c.model_dump() for c in smaller] == [c.model_dump() for c in first[:24]]
    other, _ = generate_tickets(8, 40)
    assert [c.task for c in other] != [c.task for c in first]
    assert generate_tickets(7, 0) == ([], {})
    with pytest.raises(ValueError):
        generate_tickets(7, -1)


def test_ticket_labels_and_ambiguity_rate() -> None:
    cases, table = generate_tickets(11, 40)
    ambiguous = [case for case in cases if case.metadata["ambiguous"] is True]
    assert len(ambiguous) == 10  # 25%
    labels = {case.expected["label"] for case in cases}  # type: ignore[index,call-overload]
    assert labels == set(TICKET_LABELS)
    assert len({canonical_json(case.task) for case in cases}) == 40  # no duplicates
    for case in cases:
        assert case.metadata["synthetic"] is True
        assert case.metadata["generator_version"] == "v1"
        assert case.case_id.startswith("tickets-")
    # Ambiguous tickets come from the two-cue templates, whose label is fixed by template.
    for case in ambiguous:
        text = str(case.task["ticket"])  # type: ignore[index,call-overload]
        label = case.expected["label"]  # type: ignore[index,call-overload]
        assert any(
            text.startswith(template.split("{")[0]) and fixed == label
            for fixed, template in _AMBIGUOUS
        )
    plain = [case for case in cases if case.metadata["ambiguous"] is False]
    for case in plain:
        text = str(case.task["ticket"])  # type: ignore[index,call-overload]
        assert not any(text == template for _, template in _AMBIGUOUS)


def test_ticket_oracle_table_is_exact() -> None:
    cases, table = generate_tickets(3, 24)
    assert len(table) == 24
    for case in cases:
        assert table[canonical_key(case.task)] == case.expected
    # The table holds copies: editing a case's expected never changes the oracle.
    target = cases[0].expected
    assert isinstance(target, dict)
    target["label"] = "tampered"
    assert table[canonical_key(cases[0].task)] != target


def test_ticket_pools_are_disjoint_at_any_size_and_seed() -> None:
    # Templates without placeholders ("What are your support hours on weekends?") print the
    # same ticket under every seed; the sha256 pool split keeps delivery out of the dataset.
    constant = "What are your support hours on weekends?"
    assert ticket_pool(constant) in ("main", "delivery")
    main_cases = [c for seed in (7, 8) for c in generate_tickets(seed, 200)[0]]
    delivery_cases, table = generate_delivery_tickets(1007, 200)
    main = {canonical_json(c.task) for c in main_cases}
    delivery = {canonical_json(c.task) for c in delivery_cases}
    assert len(delivery) == 200 and main.isdisjoint(delivery)

    def text(case: EvaluationCase) -> str:
        return str(case.task["ticket"])  # type: ignore[index,call-overload]

    assert {ticket_pool(text(c)) for c in main_cases} == {"main"}
    assert {ticket_pool(text(c)) for c in delivery_cases} == {"delivery"}
    # The delivery generator keeps the same shape: ambiguity rate, labels, exact oracle.
    assert sum(1 for c in delivery_cases if c.metadata["ambiguous"] is True) == 50
    assert all(c.case_id.startswith("tickets-d") for c in delivery_cases)
    assert all(table[canonical_key(c.task)] == c.expected for c in delivery_cases)
    again, _ = generate_delivery_tickets(1007, 40)
    assert [c.model_dump() for c in again] == [c.model_dump() for c in delivery_cases[:40]]


def test_invoices_are_deterministic_and_prefix_stable() -> None:
    first, table = generate_invoices(7, 40)
    again, table_again = generate_invoices(7, 40)
    assert [c.model_dump() for c in first] == [c.model_dump() for c in again]
    assert table == table_again
    smaller, _ = generate_invoices(7, 24)
    assert [c.model_dump() for c in smaller] == [c.model_dump() for c in first[:24]]
    with pytest.raises(ValueError):
        generate_invoices(7, -1)


def test_invoice_null_rate_and_consistency() -> None:
    cases, table = generate_invoices(5, 40)
    missing = [case for case in cases if case.metadata["missing_field"] is not None]
    assert len(missing) == 12  # 30%
    schema = TASKS["invoice-40"].spec("fake", "default").output_schema
    validator = Draft202012Validator(schema)
    fields_missing: set[str] = set()
    for case in cases:
        expected = case.expected
        assert isinstance(expected, dict)
        validator.validate(expected)
        nulls = [name for name in ("vendor", "date", "total") if expected[name] is None]
        field = case.metadata["missing_field"]
        assert nulls == ([] if field is None else [field])
        if field is not None:
            fields_missing.add(str(field))
        text = case.task["invoice_text"]  # type: ignore[index,call-overload]
        assert isinstance(text, str)
        items = expected["line_items"]
        assert isinstance(items, list) and 1 <= len(items) <= 4
        for item in items:
            assert isinstance(item, dict)
            assert item["description"] in INVOICE_ITEMS
            assert item["description"] in text
            assert f"{item['unit_price']:.2f}" in text
        if expected["vendor"] is not None:
            assert expected["vendor"] in text
        if expected["total"] is not None:
            total = sum(i["quantity"] * i["unit_price"] for i in items)  # type: ignore[index]
            assert abs(expected["total"] - total) < 0.006  # type: ignore[operator]
            assert f"{expected['total']:.2f}" in text
        assert table[canonical_key(case.task)] == expected
    assert fields_missing == {"vendor", "date", "total"}


def test_canonical_key_ignores_formatting() -> None:
    value = {"b": [1, 2], "a": "x"}
    assert canonical_key(value) == canonical_key(json.loads(json.dumps(value, indent=4)))
    assert canonical_key(value) != canonical_key({"a": "x", "b": [2, 1]})


def test_sentiment_fixture_table() -> None:
    cases, table = load_sentiment_cases()
    assert len(cases) == 24
    assert len(table) == len({canonical_key(case.task) for case in cases})
    for case in cases:
        assert table[canonical_key(case.task)] == case.expected


def test_sentiment_fixture_unreadable_is_a_code(tmp_path: Path) -> None:
    broken = tmp_path / "cases.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(BenchError) as caught:
        load_sentiment_cases(broken)
    assert caught.value.code == "bench_suite_unknown"


@pytest.mark.parametrize(
    ("name", "generate"),
    [("tickets", generate_tickets), ("invoice", generate_invoices)],
)
def test_committed_manifests_match_the_generators(name: str, generate: object) -> None:
    manifest = json.loads((MANIFESTS / f"{name}-manifest.json").read_text(encoding="utf-8"))
    assert manifest["generator_version"] == "v1"
    assert manifest["suite_version"] == "v1"
    assert manifest["entries"]
    for entry in manifest["entries"]:
        assert set(entry) == {"seed", "n", "cases_sha256"}  # never the cases
        cases, _ = generate(entry["seed"], entry["n"])  # type: ignore[operator]
        assert cases_sha256(cases) == entry["cases_sha256"]
