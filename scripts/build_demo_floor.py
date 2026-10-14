"""Build the demo exam for the retake feature from development receipts.

    uv run python scripts/build_demo_floor.py            # write examples/receipts/demo
    uv run python scripts/build_demo_floor.py --verify   # re-derive and compare

Two files, both made of receipts that were used while Architect was being built:

- ``demo/floor.json``: the 100 rehearsal receipts and the practice half of the evidence
  set (100), 200 in all. A profile floor splits them by the usual rule (sorted case id,
  even positions practice, odd positions exam) into 100 practice and 100 exam receipts.
- ``demo/retake.json``: the 150 pilot receipts, the extra exam cases a retake draws on.

Neither is evidence. The exam half of the evidence set and the delivery set are left
out: this script reads ``cases/rehearsal.jsonl``, ``cases/evidence.jsonl`` and
``cases/pilot.jsonl`` with their manifests and opens no other case file. The files live in
``examples/receipts/demo`` because ``scripts/fetch_bench_data.py`` owns ``cases``,
``floors`` and ``manifests`` and removes anything there it did not write.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

DATA = Path(__file__).resolve().parents[1] / "examples" / "receipts"
OUT = "demo"
# The sets this script may read; the delivery set is never among them.
SOURCES = ("rehearsal", "evidence", "pilot")
TITLE = "CORD v2 development receipts"
NOTE = (
    "These are development receipts. They were used while Architect was being built, so "
    "scores on them are not its published results."
)
_COPIED = (
    "manifest_version", "dataset", "dataset_url", "dataset_revision", "license",
    "license_url", "rule_version", "rules_sha256",
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def case_sha256(case: Mapping[str, Any]) -> str:
    """As ``architect.floors.provenance.case_sha256`` hashes a case (metadata {})."""
    return _sha256(_canonical({**case, "metadata": {}}).encode("utf-8"))


def cases_sha256(cases: Sequence[Mapping[str, Any]]) -> str:
    return _sha256(_canonical([{**case, "metadata": {}} for case in cases]).encode("utf-8"))


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _cases(data: Path, name: str) -> list[dict[str, Any]]:
    if name not in SOURCES:
        raise ValueError(f"{name} is not a set the demo may read")
    text = (data / "cases" / f"{name}.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _manifest(data: Path, name: str) -> dict[str, Any]:
    if name not in SOURCES:
        raise ValueError(f"{name} is not a set the demo may read")
    loaded: dict[str, Any] = json.loads(
        (data / "manifests" / f"{name}.json").read_text(encoding="utf-8")
    )
    return loaded


def practice_half(cases: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The cases a floor tunes on: even positions of the sorted case ids."""
    ordered = sorted(cases, key=lambda case: str(case["case_id"]))
    return [dict(case) for case in ordered[::2]]


def _render_manifest(
    base: Mapping[str, Any],
    split: str,
    relative: str,
    data: bytes,
    cases: Sequence[Mapping[str, Any]],
    origin: Mapping[str, tuple[str, Mapping[str, Any]]],
    partition: bool,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {key: base[key] for key in _COPIED}
    manifest.update(
        {
            "split": split,
            "title": TITLE,
            "unit": "receipts",
            "note": NOTE,
            "count": len(cases),
            "sources": {
                name: sum(1 for case in cases if origin[case["case_id"]][0] == name)
                for name in sorted({item[0] for item in origin.values()})
            },
            "files": {relative: {"sha256": _sha256(data)}},
            "cases_sha256": cases_sha256(cases),
        }
    )
    ids = sorted(str(case["case_id"]) for case in cases)
    position = {case_id: index for index, case_id in enumerate(ids)}
    if partition:
        manifest["partition"] = {
            "rule": "sorted case_id; even positions tune, odd positions are held out",
            "tuning": len(ids[::2]),
            "held_out": len(ids[1::2]),
        }
    entries = []
    for case in cases:
        case_id = str(case["case_id"])
        name, entry = origin[case_id]
        row = {
            "case_id": case_id,
            "source": entry["source"],
            "from": name,
            "case_sha256": case_sha256(case),
        }
        if partition:
            row["partition"] = "tuning" if position[case_id] % 2 == 0 else "held_out"
        entries.append(row)
    manifest["cases"] = entries
    return manifest


def render(data: Path = DATA) -> dict[str, bytes]:
    """Every output file (path relative to ``data`` -> exact bytes)."""
    manifests = {name: _manifest(data, name) for name in SOURCES}
    listed = {
        name: {entry["case_id"]: entry for entry in manifests[name]["cases"]} for name in SOURCES
    }
    rehearsal = _cases(data, "rehearsal")
    practice = practice_half(_cases(data, "evidence"))
    pilot = _cases(data, "pilot")
    exam_half = {
        entry["case_id"]
        for entry in manifests["evidence"]["cases"]
        if entry["partition"] == "held_out"
    }
    if exam_half & {case["case_id"] for case in practice}:
        raise ValueError("an evidence exam receipt reached the demo floor")
    floor = sorted([*rehearsal, *practice], key=lambda case: str(case["case_id"]))
    retake = sorted(pilot, key=lambda case: str(case["case_id"]))
    if {case["case_id"] for case in floor} & {case["case_id"] for case in retake}:
        raise ValueError("the demo floor and its retake cases overlap")
    origin: dict[str, tuple[str, Mapping[str, Any]]] = {}
    sets = (("rehearsal", rehearsal), ("evidence-practice", practice), ("pilot", pilot))
    for name, cases in sets:
        source = "evidence" if name == "evidence-practice" else name
        for case in cases:
            origin[case["case_id"]] = (name, listed[source][case["case_id"]])
    base = manifests["rehearsal"]
    files: dict[str, bytes] = {}
    for split, cases, partition in (("floor", floor, True), ("retake", retake, False)):
        relative = f"{OUT}/{split}.json"
        files[relative] = _json_bytes(cases)
        files[f"{OUT}/{split}.manifest.json"] = _json_bytes(
            _render_manifest(
                base, f"demo-{split}", relative, files[relative], cases, origin, partition
            )
        )
    return files


def verify(data: Path, files: Mapping[str, bytes]) -> list[str]:
    problems = []
    for relative, expected in sorted(files.items()):
        path = data / relative
        if not path.is_file():
            problems.append(f"missing {relative}")
        elif path.read_bytes() != expected:
            problems.append(f"differs {relative}")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    files = render(args.data)
    if args.verify:
        problems = verify(args.data, files)
        for problem in problems:
            print(problem)
        return 1 if problems else 0
    for relative, content in files.items():
        path = args.data / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        print(f"wrote {relative} ({len(content)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
