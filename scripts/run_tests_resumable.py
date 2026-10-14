"""Run the test suite in resumable batches (docs/interfaces/phase11-resilience.md, Section 2).

Long runs on a shared machine get stopped from outside (memory pressure, the operator's
own work). This runner splits the suite into deterministic batches of test files, records
each finished batch in a JSON-lines state file, and with ``--resume <run-id>`` re-runs only
batches that are missing or failed, so a stop never restarts the whole suite.

    uv run python scripts/run_tests_resumable.py --run-id p11
    uv run python scripts/run_tests_resumable.py --resume p11
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_ROOT = ROOT / ".architect" / "test-state"
SUMMARY = re.compile(r"(\d+) (passed|failed|skipped|errors?|xfailed|xpassed)")


def _test_files() -> list[str]:
    files = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "tests").rglob("test_*.py")
        if "fixtures" not in path.relative_to(ROOT / "tests").parts[:-1]
    )
    return files


def _batches(files: list[str], size: int) -> list[list[str]]:
    return [files[index : index + size] for index in range(0, len(files), size)]


def _free_gb() -> float | None:
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory"],
                capture_output=True, text=True, timeout=30, check=True,
            ).stdout.strip()
            return int(out) / (1024 * 1024)
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / (1024 * 1024)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return None


def _wait_for_memory(floor_gb: float, limit_s: float) -> bool:
    """True once free RAM reaches the floor (or cannot be read); False after ``limit_s``."""
    waited = 0.0
    while True:
        free = _free_gb()
        if free is None or free >= floor_gb:
            return True
        if waited >= limit_s:
            return False
        if waited % 300 == 0:
            print(f"waiting: {free:.1f} GB free is under {floor_gb:.1f} GB", flush=True)
        time.sleep(30)
        waited += 30


def _counts(output: str) -> dict[str, int]:
    """Counts from pytest's final summary line, e.g. ``12 passed, 1 skipped in 3.20s``."""
    counts = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0}
    tail = ""
    for line in output.splitlines():
        if " in " in line and SUMMARY.search(line):
            tail = line
    for number, label in SUMMARY.findall(tail):
        key = "errors" if label.startswith("error") else label
        if key in counts:
            counts[key] += int(number)
    return counts


def _done(state: Path) -> dict[int, dict[str, object]]:
    records: dict[int, dict[str, object]] = {}
    if state.exists():
        for line in state.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                records[int(record["batch"])] = record
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run-id", help="start a new run with this id")
    group.add_argument("--resume", help="continue the run with this id")
    parser.add_argument("--batch-size", type=int, default=12)
    parser.add_argument("--memory-floor-gb", type=float, default=3.0)
    # On a shared host a long wait means the operator is using it: pause, never push in.
    parser.add_argument("--wait-limit-min", type=float, default=30.0)
    parser.add_argument("--basetemp-root", default="C:/t/rt")
    args = parser.parse_args(argv)
    run_id = args.resume or args.run_id
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", run_id):
        print("invalid run id", file=sys.stderr)
        return 2
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    state = STATE_ROOT / f"{run_id}.jsonl"
    if args.run_id and state.exists():
        print(f"run {run_id} exists; use --resume {run_id}", file=sys.stderr)
        return 2
    batches = _batches(_test_files(), args.batch_size)
    done = _done(state)
    for index, files in enumerate(batches):
        previous = done.get(index)
        if previous is not None and previous.get("exit") == 0 and previous.get("files") == files:
            continue
        if not _wait_for_memory(args.memory_floor_gb, args.wait_limit_min * 60):
            print(f"paused: free RAM stayed low; continue with --resume {run_id}", flush=True)
            return 3
        started = time.monotonic()
        basetemp = f"{args.basetemp_root}/{run_id}/b{index}"
        # pytest creates --basetemp itself but not its parents.
        Path(basetemp).parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-rfE", "--color=no", "-p", "no:cacheprovider",
             "--basetemp", basetemp, *files],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        record: dict[str, object] = {
            "batch": index, "files": files, "exit": result.returncode,
            **_counts(result.stdout), "seconds": round(time.monotonic() - started, 1),
        }
        if result.returncode != 0:
            record["failures"] = [
                line for line in result.stdout.splitlines() if line.startswith(("FAILED", "ERROR"))
            ][:40]
        with state.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
        done[index] = record
        print(
            f"batch {index + 1}/{len(batches)} exit {result.returncode} "
            f"{record['passed']} passed {record['failed']} failed {record['seconds']}s",
            flush=True,
        )
    finals = [done[index] for index in range(len(batches)) if index in done]
    totals = {key: sum(int(record.get(key, 0)) for record in finals)
              for key in ("passed", "failed", "skipped", "errors")}
    bad = [record for record in finals if record.get("exit") != 0]
    missing = len(batches) - len(finals)
    print(json.dumps({"run_id": run_id, "batches": len(batches), "missing": missing,
                      "failed_batches": [record["batch"] for record in bad], **totals}))
    for record in bad:
        for line in record.get("failures", []):  # type: ignore[union-attr]
            print(line)
    return 0 if not bad and not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
