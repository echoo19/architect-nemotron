"""``bench report`` (Phase 14 Section 13.1): a golden Markdown file and the footer rules."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bench_public_support import public_row
from bench_report_fixture import fixture_pairs, fixture_rows

from architect.bench.render import footer_lines, report
from architect.bench.rows import BenchError, read_rows, write_rows
from architect.cli import main as cli_main

GOLDEN = Path(__file__).parent / "fixtures" / "bench" / "report-golden.md"
ROOT = Path(__file__).resolve().parents[1]


def test_the_markdown_report_matches_the_golden_file() -> None:
    rows = fixture_rows()
    text = report(rows, markdown=True, pairs=fixture_pairs(rows))
    assert text == GOLDEN.read_text(encoding="utf-8")
    # Byte-identical for identical input, whatever order the rows came in.
    again = list(reversed(fixture_rows()))
    assert report(again, markdown=True, pairs=fixture_pairs(again)) == text
    assert "\r" not in text and text.endswith("\n") and not text.endswith("\n\n")


def test_the_ascii_report_is_narrow_and_names_long_models_in_a_legend() -> None:
    rows = fixture_rows()
    text = report(rows, pairs=fixture_pairs(rows))
    lines = text.splitlines()
    assert all(len(line) <= 100 and line.isascii() for line in lines)
    assert lines[0] == "bench report: suite v2 5ca1ab1e, 3 rows"
    assert "model #1: nvidia/Nemotron-3-Ultra-550b-a55b" in lines
    assert "model #2: nvidia/Nemotron-3_5-Lightning" in lines
    assert any(line.startswith("A7 ") and "8/10 (0.800)" in line for line in lines)
    assert "A3 -> A7: receipts repeat 0, n = 10" in lines
    assert lines[-1] == "Delivery cases fff4adda, n = 10."
    assert report(rows, pairs=fixture_pairs(rows)) == text
    assert "paired arms" not in report(rows)  # no pairs unless asked


def test_cells_without_delivery_print_a_dash() -> None:
    row = public_row(
        delivery_pass_bits=None,
        delivery_cases=0,
        delivery_score=None,
        delivery_cost_usd=None,
        delivery_calls=None,
        delivery_p50_s=None,
        delivery_p95_s=None,
        delivery_run_costs_usd=None,
        delivery_invalid=None,
        delivery_over_budget=None,
        delivery_cases_sha256=None,
        delivery_split=None,
        held_out_correct=None,
        compile_cost_usd=None,
        tuning_score=None,
    )
    [line] = [line for line in report([row], markdown=True).splitlines() if line.startswith("| A3")]
    cells = [cell.strip() for cell in line.strip("|").split("|")]
    assert cells[2:4] == ["-", "-"] and cells[6] == "0.930" and cells[12] == "-"
    assert cells[13] == "- / 0.930 / -"


def test_each_footer_line_appears_on_its_trigger() -> None:
    frozen = public_row()
    assert footer_lines([frozen]) == [
        f"Suite v2 {frozen.suite_digest[:8]}; settings frozen: yes.",
        "Evidence: live_billed. Floor origin: public_dataset.",
        f"Delivery cases {frozen.delivery_cases_sha256[:8]}, n = 4.",  # type: ignore[index]
    ]
    rehearsal = public_row(suite_frozen=False)
    lines = footer_lines([rehearsal])
    assert lines[0].endswith("settings frozen: no.")
    assert (
        "These rows were measured before the settings were frozen: they are rehearsal numbers."
        in lines
    )
    fake = public_row(evidence="synthetic_fake", provider="fake")
    assert "Fake provider rows: plumbing checks, not model quality." in footer_lines([fake])
    synthetic = public_row(floor_origin="synthetic_generated")
    assert "Synthetic floor: not evidence of quality." in footer_lines([synthetic])
    quiet = footer_lines([frozen])
    assert not any("rehearsal" in line or "Fake" in line or "Synthetic" in line for line in quiet)
    no_delivery = public_row(delivery_cases_sha256=None)
    assert not any(line.startswith("Delivery cases") for line in footer_lines([no_delivery]))


def test_v1_rows_say_they_are_plumbing() -> None:
    baseline = read_rows(ROOT / "examples" / "bench" / "suite-v1" / "smoke-baseline.jsonl")
    lines = footer_lines(baseline)
    assert lines[0].endswith("settings frozen: no.")
    assert "Fake provider rows: plumbing checks, not model quality." in lines
    assert "Synthetic floor: not evidence of quality." in lines
    assert all(row.tuning_replay and row.delivery_pass_bits is None for row in baseline)
    text = report(baseline)
    assert all(len(line) <= 100 and line.isascii() for line in text.splitlines())


def test_a_committed_footer_replaces_the_default_lines() -> None:
    rows = fixture_rows()
    footer = "Prices of 2026-10-03. Forge saw the tuning labels; nothing saw the delivery cases.\n"
    text = report(rows, markdown=True, footer=footer)
    assert text.endswith("\n\n" + footer)
    assert "Suite v2" not in text
    assert report(rows, footer=footer).endswith(footer)


def test_rows_of_two_suites_or_no_rows_are_refused() -> None:
    rows = fixture_rows()
    mixed = [*rows, rows[0].model_copy(update={"suite_digest": "0" * 64})]
    for bad in (mixed, []):
        with pytest.raises(BenchError) as caught:
            report(bad)
        assert caught.value.code == "bench_compare_mismatch"


def _bench(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = cli_main(["bench", *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rows = fixture_rows()
    first, second = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    write_rows(first, rows[:2])
    write_rows(second, rows[2:])
    code, out, err = _bench(
        capsys, "report", str(first), str(second), "--md", "--pair-arms", "A3,A7",
        "--pair-arms", "A1,A7", "--bootstrap", "1000",
    )
    assert code == 0, err
    assert out == GOLDEN.read_text(encoding="utf-8")
    code, out, _ = _bench(capsys, "report", str(first), str(second))
    assert code == 0 and out == report(read_rows(first) + read_rows(second))
    footer = tmp_path / "footer.md"
    footer.write_text("Committed footer.\n", encoding="utf-8")
    code, out, _ = _bench(capsys, "report", str(first), "--md", "--footer", str(footer))
    assert code == 0 and out.endswith("\n\nCommitted footer.\n")
    code, _, err = _bench(capsys, "report", str(first), "--footer", str(tmp_path / "nope.md"))
    assert code == 2 and json.loads(err) == {"error": "invalid_arguments"}
    other = tmp_path / "other.jsonl"
    write_rows(other, [rows[0].model_copy(update={"suite_digest": "0" * 64})])
    code, _, err = _bench(capsys, "report", str(first), str(other))
    assert code == 1 and json.loads(err) == {"error": "bench_compare_mismatch"}
    code, _, err = _bench(capsys, "report", str(tmp_path / "missing.jsonl"))
    assert code == 1 and json.loads(err) == {"error": "bench_rows_unreadable"}
