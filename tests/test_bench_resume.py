"""Resumable bench runs (Phase 11 Section 3), offline on the fake provider.

A stopped run keeps its rows and ``run.json``; ``resume`` re-runs only the cells
without a finished row (missing, ``cancelled``, ``host_memory_low`` or a last line cut
short by the stop), refuses a changed configuration before any spend and never
duplicates a cell.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from session_support import fake_profile, run_session

from architect.bench import BenchError, BenchRow, read_rows
from architect.bench.runner import (
    RUN_FILE,
    BenchConfig,
    BenchProgress,
    bound,
    load_resume,
    plan,
    run_bench,
)
from architect.cli import main as cli_main


def quick(out: Path, **values: object) -> BenchConfig:
    return BenchConfig(tier="quick", provider="fake", size=4, out=out, **values)  # type: ignore[arg-type]


def keys(rows: list[BenchRow]) -> list[tuple[str, str, str, int]]:
    return [(row.task_id, row.model, row.variant, row.repeat) for row in rows]


def planned(config: BenchConfig) -> list[tuple[str, str, str, int]]:
    cells = plan(config, ("fake",))
    return [(cell.task.id, cell.model, cell.variant, cell.repeat) for cell in cells]


async def full_run(tmp_path: Path, config: BenchConfig) -> tuple[str, list[BenchRow]]:
    result = await run_bench(config, workspace_root=tmp_path / "ws")
    return result.bench_id, list(result.rows)


async def test_a_run_records_what_resume_must_match(tmp_path: Path) -> None:
    config = quick(tmp_path / "out")
    bench_id, _ = await full_run(tmp_path, config)
    record = json.loads((config.out / bench_id / RUN_FILE).read_text(encoding="utf-8"))
    assert set(record) == {
        "version", "config", "models", "suite_digest", "preset_fingerprint", "profile_sha256"
    }
    assert "out" not in record["config"] and record["models"] == ["fake"]
    assert load_resume(config.out, bench_id) == config


async def test_resume_after_cancel_reruns_only_the_undone_cells(tmp_path: Path) -> None:
    config = quick(tmp_path / "out")
    started = asyncio.Event()

    def progress(item: BenchProgress) -> None:
        if item.done == 1 and item.calls > item.done:
            started.set()

    task = asyncio.create_task(
        run_bench(config, on_progress=progress, workspace_root=tmp_path / "ws")
    )
    await asyncio.wait_for(started.wait(), timeout=120)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    [directory] = list(config.out.iterdir())
    before = read_rows(directory / "rows.jsonl")
    assert before[-1].failures[-1] == "cancelled"
    assert bound(config, resume=directory.name).cells[0].task_id == "invoice-40"
    assert len(bound(config, resume=directory.name).cells) == 2

    totals: list[int] = []
    result = await run_bench(
        config,
        workspace_root=tmp_path / "ws",
        resume=directory.name,
        on_progress=lambda item: totals.append(item.total),
    )
    assert result.bench_id == directory.name
    assert set(totals) == {2}  # progress counts only the cells still to run
    assert sorted(keys(list(result.rows))) == sorted(planned(config))
    assert result.rows[0] == before[0]  # a finished row is kept as it was
    assert all("cancelled" not in row.failures for row in result.rows)
    assert read_rows(directory / "rows.jsonl") == list(result.rows)
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    assert summary["cancelled"] is False
    assert sum(item["repeats"] for item in summary["summary"]["per_cell"]) == 3


async def test_a_cut_short_last_line_and_host_memory_rows_are_rerun(tmp_path: Path) -> None:
    config = quick(tmp_path / "out")
    bench_id, rows = await full_run(tmp_path, config)
    rows_path = config.out / bench_id / "rows.jsonl"
    low = rows[1].model_copy(update={"failures": ("host_memory_low",), "licensed": False})
    text = "".join(
        row.model_dump_json() + "\n" for row in (rows[0], low)
    ) + rows[2].model_dump_json()[:40]
    rows_path.write_text(text, encoding="utf-8")
    assert [cell.task_id for cell in bound(config, resume=bench_id).cells] == [
        "invoice-40",
        "exam-clean-40",
    ]
    result = await run_bench(config, workspace_root=tmp_path / "ws", resume=bench_id)
    assert keys(list(result.rows)) == planned(config)
    assert result.rows[0] == rows[0]
    assert "host_memory_low" not in result.rows[1].failures


async def test_resuming_a_finished_run_spends_nothing(tmp_path: Path) -> None:
    config = quick(tmp_path / "out")
    bench_id, rows = await full_run(tmp_path, config)
    assert bound(config, resume=bench_id).cells == ()
    result = await run_bench(config, workspace_root=tmp_path / "ws", resume=bench_id)
    assert list(result.rows) == rows


async def test_a_changed_run_is_refused_before_any_spend(tmp_path: Path) -> None:
    config = quick(tmp_path / "out")
    bench_id, rows = await full_run(tmp_path, config)
    rows_path = config.out / bench_id / "rows.jsonl"
    rows_path.write_text(rows[0].model_dump_json() + "\n", encoding="utf-8")
    with pytest.raises(BenchError, match="bench_resume_mismatch"):
        await run_bench(quick(config.out, seed=8), resume=bench_id)
    with pytest.raises(BenchError, match="bench_resume_mismatch"):
        other = "00000000-0000-4000-8000-000000000000"  # never a uuid4 bench id
        await run_bench(config, bench_id=other, resume=bench_id)
    record_path = config.out / bench_id / RUN_FILE
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["preset_fingerprint"] = "0" * 64
    record_path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(BenchError, match="bench_resume_mismatch"):
        bound(config, resume=bench_id)
    record_path.unlink()  # a run from before Phase 11 has no run.json
    with pytest.raises(BenchError, match="bench_resume_mismatch"):
        load_resume(config.out, bench_id)
    assert read_rows(rows_path) == rows[:1]  # nothing was rewritten or appended


async def test_unknown_ids_and_damaged_rows_are_unreadable(tmp_path: Path) -> None:
    config = quick(tmp_path / "out")
    bench_id, rows = await full_run(tmp_path, config)
    for reference in ("not-a-uuid", "00000000-0000-4000-8000-000000000000"):
        with pytest.raises(BenchError, match="bench_rows_unreadable"):
            load_resume(config.out, reference)
    rows_path = config.out / bench_id / "rows.jsonl"
    rows_path.write_text("{broken\n" + rows[0].model_dump_json() + "\n", encoding="utf-8")
    with pytest.raises(BenchError, match="bench_rows_unreadable"):
        bound(config, resume=bench_id)  # only the last line may be cut short


def test_a_routed_run_cannot_resume(tmp_path: Path) -> None:
    config = quick(tmp_path / "out", variants=("routed",))
    with pytest.raises(BenchError, match="bench_resume_unsupported"):
        bound(config, resume="00000000-0000-4000-8000-000000000000")


def test_cli_resume_takes_the_saved_settings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = quick(tmp_path / "out")
    bench_id, rows = asyncio.run(full_run(tmp_path, config))
    rows_path = config.out / bench_id / "rows.jsonl"
    rows_path.write_text(rows[0].model_dump_json() + "\n", encoding="utf-8")
    out = str(config.out)
    # --tier is required without --resume; a run flag next to --resume is refused.
    assert cli_main(["bench", "run", "--out", out]) == 2
    assert cli_main(["bench", "run", "--resume", bench_id, "--out", out, "--seed", "8"]) == 2
    assert cli_main(["bench", "run", "--resume", bench_id, "--out", out, "--tier", "quick"]) == 2
    assert read_rows(rows_path) == rows[:1]
    capsys.readouterr()
    assert cli_main(["bench", "run", "--resume", bench_id, "--out", out, "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["bench_id"] == bench_id
    assert keys(read_rows(rows_path)) == planned(config)
    assert cli_main(["bench", "run", "--resume", "nope", "--out", out]) != 0


def test_session_bench_resume(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    config = quick(tmp_path / "w" / "bench")
    bench_id, rows = asyncio.run(full_run(tmp_path, config))
    rows_path = config.out / bench_id / "rows.jsonl"
    rows_path.write_text(rows[0].model_dump_json() + "\n", encoding="utf-8")
    code, transcript, _ = run_session(
        [f"bench resume {bench_id}", "bench resume", "quit"], tmp_path
    )
    assert code == 0
    assert "bench quick on 1 models" in transcript
    assert "rows written to" in transcript
    assert keys(read_rows(rows_path)) == planned(config)
    assert "bench resume <id>" in transcript  # the bare verb names its syntax
    assert all(line.isascii() and len(line) <= 100 for line in transcript.splitlines())
