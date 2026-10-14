"""Retake: a changed agent takes its exam again, practises for free, or is undone.

An agent Architect builds only runs if it passed its exam. A package whose files changed
refuses every call before a model is called (``ExportedHarness.load``). This module is
what the person does next, from the CLI (``architect retake``, ``architect undo``) and
from the desktop app:

- ``status`` says in plain words why the package is stopped and what may be done.
- ``retake`` lets the changed agent take the exam again. The rule is the first exam's
  rule and nothing else: it passes if it reaches the pass mark, with invalid answers
  within the allowance and every run within its budget. The version that passed is not
  run again and no earlier score decides anything. A pass writes the package again in
  place, with its new signed report and the flag the app already sets; anything else
  leaves the folder as it is, still stopped.
- ``practice`` scores the change on the exam's practice half. It is not an attempt.
- ``undo`` puts back the last version that passed, byte for byte.

What can be retaken: a change confined to ``artifact/spec.json`` (the task and rules
text and the checks) and ``artifact/config.json`` (the model and its settings) of an
agent built from a preset. ``harness.py`` is written again from those two files with
``Preset.emit`` and an edited one is never executed. Any other change can only be undone.

Cases: where the floor names extra exam cases (``FloorConfig.retake_cases``), a retake
runs on ones no earlier attempt of this agent used, in a fixed order. When fewer than a
full exam are left it is the same exam as attempt 1, and every line says so. Attempts
are numbered and kept in ``<workspace>/retakes/<agent>.jsonl``, one line appended before
the first model call, so a cancelled attempt still counts; ``<agent>`` is the end of the
package's flag, which every placement of the same built agent shares. When that record
cannot account for every earlier attempt (a line that does not read, a deleted file), no
retake claims unused cases: it is the same exam and says so. One retake of an agent runs
at a time. An exam is retaken only where its profile entry says ``"retakes": true``, and
only while its cases are the ones the agent passed on (``TestReport.exam_sha256``).

Not signed and not a lock: the record, the copy Undo restores (checked before use) and
``placements.json`` are plain files in the workspace. A new workspace starts a new record.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from pydantic import JsonValue, ValidationError

from architect import trust
from architect.floors import FloorError, FloorEvaluator, load_manifest
from architect.forge_modes._routing import FloorOrigin, evidence_class_of, provider_of
from architect.forge_modes.errors import (
    CANNOT_CHECK,
    STOPPED,
    ExportError,
    StopReason,
    reason_line,
)
from architect.forge_modes.export import _MANIFEST as MANIFEST
from architect.forge_modes.export import (
    EDITABLE_FILES,
    changed_files,
    check_package,
    read_flag,
    read_manifest,
    read_pin,
    replace_package,
    stage_artifact,
)
from architect.forge_modes.words import (
    attempt_of,
    exam_counts,
    pass_mark,
    pretty_model,
    times,
    verdict_line,
)
from architect.presets import PresetError, get_preset
from architect.presets.checks import CHECKS_KEY
from architect.spec import FloorKind, HarnessArtifact, RetakeCases, Spec

if TYPE_CHECKING:
    from architect.profiles import Profile, Services

__all__ = [
    "PLACEMENTS",
    "Attempt",
    "Outcome",
    "RetakeError",
    "Status",
    "attempts",
    "edit_digest",
    "exam_facts",
    "exam_words",
    "last_result",
    "placed_folder",
    "plan",
    "practice",
    "remember_passed",
    "retake",
    "standing",
    "status",
    "undo",
]

PASSED_DIR = "passed"
RECORD_DIR = "retakes"
PLACEMENTS = "placements.json"
_LIMIT = 4 * 1_048_576
# What a retake may change, against the version that passed. Everything else in the two
# files is fixed: the pass mark and the exam, the answer format, the budget and the tools.
_SPEC_EDITABLE = {"task": "task text", "execution_config": "checks"}
_CONFIG_EDITABLE = ("model", "params")
_SPEC_FIXED = {
    "floor": "the pass mark or the exam",
    "secondary_floor": "the pass mark or the exam",
    "input_schema": "the answer format",
    "output_schema": "the answer format",
    "budget": "the budget",
    "tools": "the tools",
    # Inside execution_config only the checks may change: the rest says how the agent
    # runs (its kind, its images, its limits), and that is part of what was examined.
    "execution_config": "how the agent runs",
}
_FIXED = (
    "The pass mark, the exam, the answer format, the budget, the tools and the settings "
    "Architect writes itself are fixed, so this change can only be undone."
)
_SAME_BYTES = "spacing only, same content"
_HARNESS_HINT = (
    "Architect writes harness.py from the agent's task, checks, model and settings. To "
    "change the agent, edit those in artifact/spec.json or artifact/config.json."
)

Refusal = Literal[
    "not_stopped", "cannot_check", "undo_only", "edit_invalid", "not_a_preset_agent",
    "exam_no_retakes", "exam_not_configured", "exam_changed", "model_not_in_profile",
    "plan_names_model", "no_copy", "not_written", "cancelled", "busy",
]


class RetakeError(ValueError):
    """A retake, practice run or undo that was refused. No attempt was used and no model
    was called. ``str()`` is one fixed code and ``lines`` say it in plain words."""

    def __init__(self, code: Refusal, *lines: str) -> None:
        super().__init__(code)
        self.code = code
        self.lines = tuple(lines)
        # The model an undo-only edit also names, so the refusal can say it is unknown.
        self.model: str | None = None


@dataclass(frozen=True)
class Attempt:
    """One line of an agent's attempts list. Counts only."""

    number: int
    cases: Literal["first_exam", "unused", "same_exam"]
    # ``not_placed``: it passed, and its folder was not written (see ``retake``).
    result: Literal["passed", "did_not_pass", "cancelled", "started", "not_placed"]
    right: int | None
    total: int | None
    # What the examined change was ("model: Nemotron 3 Super to Nemotron 3.5 Lightning"),
    # and whether the person undid it afterwards. Neither is part of the result.
    what: str | None = field(default=None, compare=False)
    undone: bool = field(default=False, compare=False)

    def facts(self) -> dict[str, JsonValue]:
        return {
            "number": self.number, "cases": self.cases, "result": self.result,
            "right": self.right, "total": self.total, "what": self.what,
            "undone": self.undone,
        }


@dataclass(frozen=True)
class Status:
    """Where a placed agent stands. ``lines`` is the plain text to show, sentence first."""

    state: Literal["passed", "stopped"]
    package: str
    reason: StopReason | None = None
    changed_files: tuple[str, ...] = ()
    lines: tuple[str, ...] = ()
    can_retake: bool = False
    can_undo: bool = False
    refusal: Refusal | None = None
    attempt: int = 1  # of the version that passed
    next_attempt: int = 2
    model: str | None = None  # of the version that passed
    right: int | None = None
    total: int | None = None
    pass_mark: int | None = None
    # What the person can do about a stop, in one line, for an app to show.
    step: str = ""

    @property
    def sentence(self) -> str | None:
        return self.lines[0] if self.state == "stopped" and self.lines else None

    def facts(self) -> dict[str, JsonValue]:
        return {
            "state": self.state, "package": self.package, "reason": self.reason,
            "changed_files": list(self.changed_files), "lines": list(self.lines),
            "can_retake": self.can_retake, "can_undo": self.can_undo,
            "refusal": self.refusal, "attempt": self.attempt,
            "next_attempt": self.next_attempt, "model": self.model, "right": self.right,
            "total": self.total, "pass_mark": self.pass_mark, "step": self.step,
        }


@dataclass(frozen=True)
class Outcome:
    """What a retake, a practice run or an undo did. ``lines`` is the plain text."""

    kind: Literal["retake", "practice", "undo"]
    passed: bool | None  # None for a practice run and an undo
    lines: tuple[str, ...]
    attempt: int | None = None
    cases: RetakeCases | None = None
    right: int | None = None
    total: int | None = None
    pass_mark: int | None = None
    cost_usd: float = 0.0
    seconds: float = 0.0
    model_calls: int = 0

    def facts(self) -> dict[str, JsonValue]:
        return {
            "kind": self.kind, "passed": self.passed, "lines": list(self.lines),
            "attempt": self.attempt, "cases": self.cases, "right": self.right,
            "total": self.total, "pass_mark": self.pass_mark,
            "cost_usd": round(self.cost_usd, 6), "seconds": round(self.seconds, 1),
        }


# ------------------------------------------------------------------ plain words


def money(value: float) -> str:
    """Dollars as a person reads them: cents, or more digits for less than a cent."""
    if 0 < value < 0.001:
        return f"${value:.4f}"
    return f"${value:.3f}" if 0 < value < 0.01 else f"${value:.2f}"


def _unit(profile: Profile | None, dataset: str) -> str:
    """What the exam's cases are called ("receipts"), from the floor's manifest."""
    if profile is None:
        return "cases"
    config = profile.floors.get(dataset)
    if config is None or config.manifest is None:
        return "cases"
    try:
        return load_manifest(profile.resolve(config.manifest)).display()[1]
    except FloorError:
        return "cases"


def _note(profile: Profile | None, dataset: str) -> tuple[str, ...]:
    """The line a floor's manifest asks to be shown under every score, if it has one."""
    if profile is None:
        return ()
    config = profile.floors.get(dataset)
    if config is None or config.manifest is None:
        return ()
    try:
        note = load_manifest(profile.resolve(config.manifest)).note
    except FloorError:
        return ()
    return (note,) if note else ()


def _shown(folder: Path) -> str:
    """The folder as the person typed or sees it: relative to here when it is below."""
    try:
        return folder.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except (ValueError, OSError):
        return str(folder)


def _source_words(cases: RetakeCases, attempt: int, total: int, unit: str) -> str:
    if cases == "unused":
        return f"Attempt {attempt}, on {total} {unit} no earlier attempt used."
    return f"Same exam, attempt {attempt}."


# ------------------------------------------------------------------ the copy for Undo


def _same_path(left: Path, right: Path) -> bool:
    try:
        return os.path.normcase(left.resolve()) == os.path.normcase(right.resolve())
    except OSError:
        return False


def _placements(workspace: Path) -> dict[str, Any]:
    path = workspace / PLACEMENTS
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _LIMIT:
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def remember_passed(
    workspace: Path, folder: Path, staged: Path, *, agent: str | None = None
) -> None:
    """Keep ``staged``, the package just placed at ``folder``, as the version Undo puts
    back, and note where it was placed. Called at Add to project and after every retake
    that passed. ``staged`` is the host's own staged copy, byte for byte what was placed.
    ``agent`` is the installed agent's name, kept so its page finds the folder; a retake
    passes none and the name noted at Add to project stays.
    """
    manifest = read_manifest(staged)[0]
    copy = workspace / PASSED_DIR / manifest.package
    temporary = copy.with_name(f".{copy.name}.{uuid4().hex[:8]}")
    copy.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(staged, temporary, symlinks=False)
        if copy.exists():
            shutil.rmtree(copy)
        os.replace(temporary, copy)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    placements = _placements(workspace)
    earlier = placements.get(manifest.package)
    if agent is None and isinstance(earlier, dict) and isinstance(earlier.get("agent"), str):
        agent = earlier["agent"]
    placements[manifest.package] = {
        "path": str(folder.resolve()),
        "artifact_digest": manifest.artifact_digest,
        "spec_hash": manifest.spec_hash,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        **({} if agent is None else {"agent": agent}),
    }
    _write_json(workspace / PLACEMENTS, placements)


def placed_folder(
    workspace: Path, agent: str, spec_hashes: Collection[str] = ()
) -> Path | None:
    """Where the installed agent ``agent`` was last added to a project, or None when it
    never was (or the folder is gone). The note of its name decides; a placement noted
    without one is matched by the plan it was built from (``spec_hashes``)."""
    found: tuple[str, Path] | None = None
    for entry in _placements(Path(workspace)).values():
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            continue
        named = entry.get("agent")
        if named != agent and (named is not None or entry.get("spec_hash") not in spec_hashes):
            continue
        at = entry["at"] if isinstance(entry.get("at"), str) else ""
        if found is None or at > found[0]:
            found = (at, Path(entry["path"]))
    if found is None or not found[1].is_dir():
        return None
    return found[1]


def _checks_out(copy: Path, package: str) -> bool:
    try:
        manifest = read_manifest(copy)[0]
    except ExportError:
        return False
    if manifest.package != package:
        return False
    return not check_package(copy, read_pin(copy), registry=None).codes


def _passed_copy(folder: Path, workspace: Path) -> Path | None:
    """The copy of the version that passed, checked before use.

    The folder's own manifest names that version: an edit to the agent's files
    leaves it alone. So the copy is the one kept for this folder or the staged export of
    that version, whichever holds the version the manifest names. That is the last one
    that passed even when the kept copy fell behind (a retake whose copy could not be
    written) or the folder was set back to an older version with git. Only when the
    manifest names neither, or does not read, is it the kept copy as it is. None when no
    copy here passes its own checks."""
    package = folder.name
    entry = _placements(workspace).get(package)
    found: list[Path] = []
    if (
        isinstance(entry, dict)
        and isinstance(entry.get("path"), str)
        and _same_path(Path(entry["path"]), folder)
    ):
        found.append(workspace / PASSED_DIR / package)
    try:
        digest: str | None = read_manifest(folder)[0].artifact_digest
    except ExportError:
        digest = None
    if digest is not None:
        found.append(workspace / "exports" / digest[:16] / package)
    copies = [copy for copy in found if _checks_out(copy, package)]
    for copy in copies:
        if digest is not None and read_manifest(copy)[0].artifact_digest == digest:
            return copy
    return copies[0] if copies else None


# ------------------------------------------------------------------ the record


def _agent_key(folder: Path, copy: Path | None) -> str:
    """Which agent a record is of: the end of the flag its package carries. Every
    placement of the same built agent shares the flag, whatever its folder is called, and
    a retake that passes keeps it, so "no earlier attempt used" is true of the agent and
    not of one folder. Two different agents under one package name have different flags.
    The package's name when no flag reads."""
    flag = read_flag(folder) or (read_flag(copy) if copy is not None else None)
    return flag.rsplit("_", 1)[-1].lower() if flag else folder.name


@dataclass
class _Record:
    """An agent's attempts as this workspace recorded them: one row per attempt, oldest
    first. ``damaged`` when a line of the file does not read as a row, or the file itself
    cannot be read: what it said is then unknown, and nothing is dropped from it."""

    path: Path
    rows: list[dict[str, Any]] = field(default_factory=list)
    damaged: bool = False

    def complete(self, passed_attempt: int) -> bool:
        """Whether the rows account for every retake so far, with the cases each used:
        only then can a retake say which cases no earlier attempt used."""
        if self.damaged:
            return False
        numbers = sorted(int(row["attempt"]) for row in self.rows if row["attempt"] >= 2)
        if numbers != list(range(2, len(numbers) + 2)):
            return False
        if passed_attempt > max([1, *numbers]):
            return False
        return all(row.get("case_ids") for row in self.rows if row.get("cases") == "unused")


def _record(workspace: Path, key: str) -> _Record:
    """Read the record. The file is append-only: a later line for the same attempt adds
    to the earlier one (its result, or that the change was undone)."""
    record = _Record(workspace / RECORD_DIR / f"{key}.jsonl")
    path = record.path
    try:
        if not path.exists() and not path.is_symlink():
            return record
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _LIMIT:
            record.damaged = True
            return record
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        record.damaged = True
        return record
    merged: dict[int, dict[str, Any]] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            row = None
        if not isinstance(row, dict) or type(row.get("attempt")) is not int:
            record.damaged = True
            continue
        merged.setdefault(row["attempt"], {}).update(row)
    record.rows = [merged[number] for number in sorted(merged)]
    return record


def _append(record: _Record, row: dict[str, Any]) -> None:
    """Add one line to the record and to ``record.rows``. Nothing already there is
    rewritten, so a line this code cannot read is never lost."""
    path = record.path
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        whole = path.stat().st_size == 0 or path.read_bytes().endswith(b"\n")
    except OSError:
        whole = True
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(("" if whole else "\n") + json.dumps(row, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    for kept in record.rows:
        if kept["attempt"] == row["attempt"]:
            kept.update(row)
            break
    else:
        record.rows.append(dict(row))
        record.rows.sort(key=lambda item: int(item["attempt"]))


@contextlib.contextmanager
def _one_at_a_time(workspace: Path, key: str) -> Iterator[None]:
    """Hold the agent's retake lock: a second retake of the same agent, from this process
    or another, is refused (``busy``) and uses no attempt. The lock is on an open file, so
    it goes when the process does."""
    path = workspace / RECORD_DIR / f"{key}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RetakeError(
                "busy",
                "Another retake of this agent is running. Wait for it to finish, or "
                "cancel it where it was started. No attempt was used.",
            ) from None
        yield


def _used(rows: list[dict[str, Any]]) -> frozenset[str]:
    return frozenset(
        case_id
        for row in rows
        for case_id in (row.get("case_ids") or ())
        if isinstance(case_id, str)
    )


def _next_attempt(passed_attempt: int, rows: list[dict[str, Any]]) -> int:
    return 1 + max([passed_attempt, *(int(row["attempt"]) for row in rows)])


def attempts(folder: Path, workspace: Path) -> tuple[Attempt, ...]:
    """The agent's attempts, oldest first: the exam it was built on, then every retake
    this workspace recorded. Scores on different cases cannot be compared with each other.
    """
    folder, workspace = Path(folder), Path(workspace)
    copy = _passed_copy(folder, workspace)
    rows = _record(workspace, _agent_key(folder, copy)).rows
    first: Attempt | None = None
    if copy is not None and not rows:
        report = HarnessArtifact.load(copy / "artifact").report
        counts = exam_counts(report) if report is not None else None
        if report is not None and report.attempt is None:
            first = Attempt(
                1, "first_exam", "passed", counts[0] if counts else None,
                counts[1] if counts else None,
            )
    listed = [first] if first is not None else []
    for row in rows:
        if row["attempt"] == 1:
            listed.append(Attempt(1, "first_exam", "passed", row.get("right"), row.get("total")))
            continue
        cases = row.get("cases")
        result = row.get("result")
        known = ("passed", "did_not_pass", "cancelled", "not_placed")
        listed.append(
            Attempt(
                row["attempt"],
                cases if cases in ("unused", "same_exam") else "same_exam",
                result if result in known else "started",
                row.get("right") if type(row.get("right")) is int else None,
                row.get("total") if type(row.get("total")) is int else None,
                what=row.get("what") if isinstance(row.get("what"), str) else None,
                undone=row.get("undone") is True,
            )
        )
    return tuple(listed)


def edit_digest(folder: Path) -> str:
    """The bytes of the two files a retake reads, as one digest: which edit a recorded
    attempt examined."""
    digest = hashlib.sha256()
    for name in EDITABLE_FILES:
        try:
            digest.update((folder / name).read_bytes())
        except OSError:
            pass
        digest.update(b"\0")
    return digest.hexdigest()


def last_result(folder: Path, workspace: Path) -> Attempt | None:
    """The newest retake of the edit that is in ``folder`` now, when it did not pass or
    was cancelled; None once the files changed again, were undone, or it passed."""
    folder, workspace = Path(folder), Path(workspace)
    rows = _record(workspace, _agent_key(folder, _passed_copy(folder, workspace))).rows
    if not rows or rows[-1].get("result") not in ("did_not_pass", "cancelled", "not_placed"):
        return None
    if rows[-1].get("undone") is True or rows[-1].get("edit") != edit_digest(folder):
        return None
    return attempts(folder, workspace)[-1]


def exam_words(spec: Spec, services: Services | None) -> tuple[str, str | None]:
    """What the exam's cases are called ("receipts") and the line its manifest asks to be
    shown under every score, if it has one."""
    profile = services.profile if services is not None else None
    note = _note(profile, spec.floor.dataset)
    return _unit(profile, spec.floor.dataset), (note[0] if note else None)


# ------------------------------------------------------------------ what changed


@dataclass(frozen=True)
class _Edit:
    """The edited agent, read from the two JSON files and checked against the version
    that passed. Nothing in the changed folder is executed to build it."""

    base: HarnessArtifact
    spec: Spec
    model: str
    params: dict[str, JsonValue]
    details: dict[str, str] = field(default_factory=dict)


def _load_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _LIMIT:
        raise ValueError("unreadable")
    return json.loads(path.read_text(encoding="utf-8"))


def _from_preset(base: HarnessArtifact) -> bool:
    """Whether ``harness.py`` of the version that passed is what its preset writes from
    its spec and settings. Forge-written and rewritten agents are not. The check never
    runs the file: it compares syntax trees, as a warm start does, since saving a plan as
    JSON may reorder its keys."""
    from architect.compiler.tuning import _source_tree

    if base.forge is not None or base.execution_mode != "native":
        return False
    try:
        preset = get_preset(base.preset)
        with tempfile.TemporaryDirectory(prefix="architect-retake-") as temporary:
            preset.emit(Path(temporary), base.spec, base.params, base.components)
            written = (Path(temporary) / "harness.py").read_text(encoding="utf-8")
        kept = (base.directory / "harness.py").read_text(encoding="utf-8")
        return _source_tree(written) == _source_tree(kept)
    except (PresetError, OSError, ValueError, TypeError, SyntaxError):
        return False


def _printable(text: str, limit: int = 160) -> str:
    cut = "".join(char if " " <= char <= "~" else "?" for char in text)
    return cut if len(cut) <= limit else cut[: limit - 3] + "..."


def _listed(items: list[str], last: str = "and") -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f" {last} " + items[-1]


def _json_file(folder: Path, name: str) -> Any:
    """``name`` in ``folder`` as JSON, or ``edit_invalid`` saying where it stops reading."""
    try:
        return _load_json(folder / name)
    except json.JSONDecodeError as exc:
        raise RetakeError(
            "edit_invalid",
            f"{name} is not valid JSON: see line {exc.lineno}, column {exc.colno}. Fix it "
            "or undo.",
        ) from None
    except (OSError, ValueError, RecursionError):
        raise RetakeError(
            "edit_invalid", f"{name} cannot be read as JSON. Fix it or undo."
        ) from None


def _first_error(exc: ValidationError) -> str:
    """The first thing a plan fails on: where, and what the rule is. Never the value."""
    try:
        first = exc.errors(include_url=False, include_input=False, include_context=False)[0]
    except (IndexError, TypeError, ValueError):
        return ""
    where = ".".join(str(part) for part in first.get("loc", ()))
    said = str(first.get("msg", "")).rstrip(".")
    return _printable(f"{where}: {said}." if where else f"{said}.")


def _settings(preset_name: str, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """The agent's settings, checked one by one so the refusal names the setting and what
    it may be: ``retries is 2. It can be 0 or 1.``"""
    name = "artifact/config.json"
    try:
        preset = get_preset(preset_name)
        ranges = preset.tunable_params
        unknown = sorted(set(params) - ranges.keys())
        if unknown:
            known = (
                f"Its settings are {_listed(sorted(ranges))}."
                if ranges
                else "This agent has no settings."
            )
            raise RetakeError(
                "edit_invalid",
                f'{name}: "{_printable(unknown[0], 40)}" is not a setting of this agent. '
                f"{known} Fix it or undo.",
            )
        for key, bounds in ranges.items():
            if key not in params:
                continue
            try:
                preset.validate_params({key: params[key]})
            except PresetError:
                value = _printable(json.dumps(params[key]), 40)
                allowed = _listed([json.dumps(item) for item in bounds.values], "or")
                raise RetakeError(
                    "edit_invalid",
                    f"{name}: {key} is {value}. It can be {allowed}. Fix it or undo.",
                ) from None
        return preset.validate_params(params)
    except (PresetError, TypeError, ValueError) as exc:
        if isinstance(exc, RetakeError):
            raise
        raise RetakeError(
            "edit_invalid", f"{name} no longer holds settings this agent can use. Fix it or undo."
        ) from None


def _read_edit(folder: Path, base: HarnessArtifact) -> _Edit:
    """The edit, or ``RetakeError`` saying why not: ``undo_only`` when it touches what is
    fixed (fixing the file would not help), else ``edit_invalid`` naming what to fix."""
    spec_raw = _json_file(folder, "artifact/spec.json")
    try:
        spec = Spec.model_validate(spec_raw)
    except ValidationError as exc:
        found = _first_error(exc)
        raise RetakeError(
            "edit_invalid",
            "artifact/spec.json is no longer a plan Architect can read. "
            + (f"{found} " if found else "")
            + "Fix it or undo.",
        ) from None
    except (ValueError, TypeError, RecursionError):
        raise RetakeError(
            "edit_invalid",
            "artifact/spec.json is no longer a plan Architect can read. Fix it or undo.",
        ) from None
    config = _json_file(folder, "artifact/config.json")
    if not isinstance(config, dict):
        raise RetakeError(
            "edit_invalid",
            "artifact/config.json must be one JSON object with the agent's model and "
            "settings. Fix it or undo.",
        )
    model = config.get("model")
    before, after = base.spec.model_dump(mode="json"), spec.model_dump(mode="json")
    fixed = {
        _SPEC_FIXED.get(key, "other fixed parts of the plan")
        for key in set(before) | set(after)
        if key not in _SPEC_EDITABLE and before.get(key) != after.get(key)
    }
    ran_before = {k: v for k, v in before["execution_config"].items() if k != CHECKS_KEY}
    ran_after = {k: v for k, v in after["execution_config"].items() if k != CHECKS_KEY}
    if ran_before != ran_after:
        fixed.add(_SPEC_FIXED["execution_config"])
    listed = sorted(fixed)
    old_config = base._config()
    if any(
        config.get(key) != old_config.get(key)
        for key in set(config) | set(old_config)
        if key not in _CONFIG_EDITABLE
    ):
        listed.append("settings Architect writes itself")
    if listed:
        refusal = RetakeError("undo_only", f"The change touches {_listed(listed)}.", _FIXED)
        if isinstance(model, str) and model != base.model:
            refusal.model = model
        raise refusal
    params = config.get("params", {})
    if not isinstance(model, str) or not model.strip():
        raise RetakeError(
            "edit_invalid",
            'artifact/config.json needs a model name in "model". Fix it or undo.',
        )
    if not isinstance(params, dict):
        raise RetakeError(
            "edit_invalid",
            'artifact/config.json: "params" must be a JSON object of settings. Fix it or undo.',
        )
    validated = _settings(base.preset, params)
    details: dict[str, str] = {}
    spec_parts = []
    if before.get("task") != after.get("task"):
        spec_parts.append(_SPEC_EDITABLE["task"])
    if before["execution_config"].get(CHECKS_KEY) != after["execution_config"].get(CHECKS_KEY):
        spec_parts.append(_SPEC_EDITABLE["execution_config"])
    config_parts = []
    if model != base.model:
        config_parts.append(f"model: {pretty_model(base.model)} to {pretty_model(model)}")
    if validated != _settings(base.preset, dict(base.params)):
        config_parts.append("settings")
    # A file whose bytes changed and whose content did not: say so, it still stops.
    details["artifact/spec.json"] = ", ".join(spec_parts) or _SAME_BYTES
    details["artifact/config.json"] = ", ".join(config_parts) or _SAME_BYTES
    return _Edit(base, spec, model, validated, details)


def _what(edit: _Edit) -> str:
    """The edit in a few words: "model: Nemotron 3 Super to Nemotron 3.5 Lightning"."""
    parts = [words for words in edit.details.values() if words != _SAME_BYTES]
    return "; ".join(parts) or _SAME_BYTES


def _model_missing(model: str, profile: Profile | None) -> str | None:
    if profile is None or model in profile.models:
        return None
    return (
        f"The model {pretty_model(model)} is not in Architect's settings here. Add it "
        "there, or name one that is."
    )


def _no_retakes(published: bool) -> tuple[str, str]:
    why = (
        "This exam is on a published set of cases, and Architect doesn't retake a "
        "published exam, so a changed agent cannot take it again."
        if published
        else 'This exam is not set to allow retakes ("retakes": true in its entry in '
        "Architect's settings), so a changed agent cannot take it again."
    )
    return why, "Undo puts back the version that passed."


def _exam_refusal(edit: _Edit, profile: Profile) -> RetakeError | None:
    """Why Architect's settings here cannot examine the edit, or None."""
    spec = edit.spec
    config = profile.floors.get(spec.floor.dataset)
    if config is None or config.kind != spec.floor.kind:
        return RetakeError(
            "exam_not_configured",
            "Architect's settings here do not hold this agent's exam, so it cannot be "
            "retaken here.",
            "Undo puts back the version that passed.",
        )
    if (
        not config.retakes_allowed
        or spec.floor.kind != FloorKind.HELD_OUT_TASKS
        or spec.secondary_floor is not None
    ):
        return RetakeError("exam_no_retakes", *_no_retakes(config.manifest is not None))
    missing = _model_missing(edit.model, profile)
    if missing is not None:
        return RetakeError("model_not_in_profile", missing)
    target = spec.model_target
    if target != "cheapest that passes" and target != edit.model:
        return RetakeError(
            "plan_names_model",
            f"The agent's plan names the model {pretty_model(target)}, so it cannot be "
            "retaken on another.",
        )
    return None


# ------------------------------------------------------------------ status

_STEP_RETAKE = "Retake its exam or undo the change in Architect."
_STEP_FIX = "Fix the file, or undo the change in Architect."
_STEP_UNDO = "Undo the change in Architect."
_STEP_KEY = (
    "Set ARCHITECT_SIGNING_KEY_FILE to the key file Architect signed it with, or build "
    "the agent again on this computer."
)


def status(
    folder: Path,
    workspace: Path,
    services: Services | None = None,
    *,
    profile: Profile | None = None,
) -> Status:
    """Where the agent placed at ``folder`` stands, with 0 model calls.

    A profile (``services``, or ``profile`` alone, which builds nothing) adds the checks
    only Architect's settings can make: that they hold the agent's exam, that the exam
    allows retakes and that they know the edited model. Without one, ``can_retake`` says
    only that the change itself is one a retake may examine. ``step`` is what the person
    can do next, in one line.
    """
    folder, workspace = Path(folder), Path(workspace)
    if services is not None:
        profile = services.profile
    package = folder.name
    with trust.configured_only():
        check = check_package(folder, read_pin(folder), registry=None)
        copy = _passed_copy(folder, workspace)
    rows = _record(workspace, _agent_key(folder, copy)).rows
    if not check.codes:
        artifact = HarnessArtifact.load(folder / "artifact")
        report = artifact.report
        assert report is not None  # a package without codes is licensed
        counts = exam_counts(report)
        return Status(
            "passed", package,
            lines=("Passed. It is your agent.", verdict_line(artifact.model, report)),
            attempt=attempt_of(report),
            next_attempt=_next_attempt(attempt_of(report), rows),
            model=artifact.model,
            right=counts[0] if counts else None,
            total=counts[1] if counts else None,
            pass_mark=counts[2] if counts else None,
        )
    base = HarnessArtifact.load(copy / "artifact") if copy is not None else None
    report = base.report if base is not None else None
    counts = exam_counts(report) if report is not None else None
    known: dict[str, Any] = {
        "attempt": attempt_of(report) if report is not None else 1,
        "next_attempt": _next_attempt(attempt_of(report) if report is not None else 1, rows),
        "model": base.model if base is not None else None,
        "right": counts[0] if counts else None,
        "total": counts[1] if counts else None,
        "pass_mark": counts[2] if counts else None,
        "can_undo": copy is not None,
    }
    try:
        changed = changed_files(folder, read_manifest(folder)[0])
    except ExportError:
        changed = (MANIFEST,)
    no_copy = (
        "This computer has no copy of the version that passed. Restore "
        f"{_shown(folder)} with git."
    )
    if not changed:
        if set(check.codes) - {"license_unverifiable", "key_untrusted", "export_unverified"}:
            changed = (MANIFEST,)
        else:
            return Status(
                "stopped", package, reason="cannot_check",
                lines=(CANNOT_CHECK, reason_line("cannot_check", ()), _STEP_KEY),
                refusal="cannot_check", step=_STEP_KEY, **{**known, "can_undo": False},
            )
    undo_step = _STEP_UNDO if copy is not None else ""
    if not set(changed) <= set(EDITABLE_FILES):
        lines = [STOPPED, reason_line("changed_undo_only", changed)]
        if "artifact/harness.py" in changed:
            lines.append(_HARNESS_HINT)
        if copy is None:
            lines.append(no_copy)
        return Status(
            "stopped", package, reason="changed_undo_only", changed_files=changed,
            lines=tuple(lines), refusal="undo_only" if copy is not None else "no_copy",
            step=undo_step, **known,
        )
    if base is None:
        return Status(
            "stopped", package, reason="changed", changed_files=changed,
            lines=(STOPPED, reason_line("changed", changed), no_copy),
            refusal="no_copy", **known,
        )
    if not _from_preset(base):
        return Status(
            "stopped", package, reason="changed_undo_only", changed_files=changed,
            lines=(
                STOPPED, reason_line("changed_undo_only", changed),
                "This agent was written by Forge. Its exam cannot be retaken, so a change "
                "to it can only be undone.",
            ),
            refusal="not_a_preset_agent", step=undo_step, **known,
        )
    try:
        edit = _read_edit(folder, base)
    except RetakeError as exc:
        undo_only = exc.code == "undo_only"
        head = (
            f"Changed: {', '.join(changed)}."
            if undo_only
            else reason_line("changed", changed)
        )
        missing = _model_missing(exc.model, profile) if exc.model is not None else None
        return Status(
            "stopped", package, reason="changed_undo_only" if undo_only else "changed",
            changed_files=changed,
            lines=(STOPPED, head, *exc.lines, *((missing,) if missing else ())),
            refusal=exc.code, step=undo_step if undo_only else _STEP_FIX, **known,
        )
    lines = [STOPPED, reason_line("changed", changed, edit.details)]
    refusal = _exam_refusal(edit, profile) if profile is not None else None
    if refusal is not None:
        lines.extend(refusal.lines)
    step = _STEP_RETAKE if refusal is None else undo_step
    if refusal is not None and refusal.code == "model_not_in_profile":
        step = _STEP_FIX
    return Status(
        "stopped", package, reason="changed", changed_files=changed, lines=tuple(lines),
        can_retake=refusal is None, refusal=refusal.code if refusal else None, step=step,
        **known,
    )


def standing(
    folder: Path, profile: Path | None = None, workspace: Path | None = None
) -> Status:
    """``status`` for an app or a terminal that holds a profile file and nothing else:
    where its agent stands and what to do about a stop. The workspace is the one the
    agent's own ``run(profile=...)`` uses (``workspace``, else ``ARCHITECT_WORKSPACE``,
    else the profile's). Nothing is built and no model is called."""
    from architect.agent.app import agent_workspace
    from architect.forge_modes.shim import _workspace
    from architect.profiles import in_workspace, load_profile

    if profile is None:
        return status(Path(folder), agent_workspace(Path.cwd(), workspace))
    loaded = load_profile(Path(profile))
    chosen = _workspace(loaded, workspace)
    return status(Path(folder), chosen, profile=in_workspace(loaded, chosen))


@dataclass(frozen=True)
class _Ready:
    """A change a retake or a practice run may examine, with what it needs."""

    state: Status
    edit: _Edit
    floor: FloorEvaluator
    record: _Record
    copy: Path
    # Whether the record accounts for every earlier attempt and its cases.
    known: bool


def _retakeable(folder: Path, services: Services) -> _Ready:
    """The checks before any spend; ``RetakeError`` when a retake or practice run is off."""
    workspace = services.workspace
    state = status(folder, workspace, services)
    if state.state == "passed":
        raise RetakeError(
            "not_stopped", "Nothing changed since its exam, so there is nothing to retake.",
            state.lines[1],
        )
    if not state.can_retake:
        raise RetakeError(state.refusal or "undo_only", *state.lines)
    copy = _passed_copy(folder, workspace)
    assert copy is not None  # can_retake
    edit = _read_edit(folder, HarnessArtifact.load(copy / "artifact"))
    try:
        floor = services.compiler.floor_factory(edit.spec.floor)
    except (FloorError, OSError, ValueError):
        raise RetakeError(
            "exam_not_configured", *state.lines,
            "The agent's exam could not be read from Architect's settings here.",
        ) from None
    if not floor.retakes_allowed:
        raise RetakeError("exam_no_retakes", *state.lines, *_no_retakes(False))
    report = edit.base.report
    counts = exam_counts(report) if report is not None else None
    signed = report.exam_sha256 if report is not None else None
    if (signed is not None and floor.exam_digest != signed) or (
        signed is None and counts is not None and floor.exam_size != counts[1]
    ):
        raise RetakeError(
            "exam_changed", *state.lines,
            "The exam in Architect's settings is not the one this agent passed: its cases "
            "have changed. A retake needs that same exam, so no attempt was used.",
            "Put the exam's cases back as they were, or undo the change to the agent.",
        )
    record = _record(workspace, _agent_key(folder, copy))
    return _Ready(state, edit, floor, record, copy, record.complete(state.attempt))


def exam_facts(
    folder: Path, workspace: Path, services: Services | None
) -> dict[str, JsonValue]:
    """How to speak of the exam of the version that passed: what its cases are called,
    the line shown under every score, which cases its last exam ran on (``first_exam``,
    ``unused`` or ``same_exam``), and whether a change to the agent could be retaken with
    these settings (None when that cannot be said). Read from the folder when it is that
    version, else from the copy Undo restores."""
    folder, workspace = Path(folder), Path(workspace)
    facts: dict[str, JsonValue] = {
        "unit": "cases", "note": None, "cases": "first_exam", "retakes": None,
    }
    with trust.configured_only():
        unchanged = not check_package(folder, read_pin(folder), registry=None).codes
        source = folder if unchanged else _passed_copy(folder, workspace)
    if source is None:
        return facts
    artifact = HarnessArtifact.load(source / "artifact")
    unit, note = exam_words(artifact.spec, services)
    report = artifact.report
    cases = report.retake_cases if report is not None else None
    retakes: bool | None = None
    if services is not None:
        config = services.profile.floors.get(artifact.spec.floor.dataset)
        retakes = (
            config is not None
            and config.kind == artifact.spec.floor.kind == FloorKind.HELD_OUT_TASKS
            and config.retakes_allowed
            and artifact.spec.secondary_floor is None
            and _from_preset(artifact)
        )
    return {"unit": unit, "note": note, "cases": cases or "first_exam", "retakes": retakes}


def _waiting(ready: _Ready, folder: Path, workspace: Path) -> Path | None:
    """The staged package of an attempt that passed on exactly this edit and whose folder
    could not be written then: it can be placed now without another attempt. None when
    the last attempt is not that, the files changed since, or the staged copy does not
    pass its own checks."""
    rows = ready.record.rows
    if not rows or rows[-1].get("result") != "not_placed":
        return None
    last = rows[-1]
    if last.get("edit") != edit_digest(folder) or not isinstance(last.get("staged"), str):
        return None
    staged = workspace / "exports" / last["staged"] / folder.name
    if not _checks_out(staged, folder.name):
        return None
    try:
        artifact = HarnessArtifact.load(staged / "artifact")
    except (OSError, ValueError, TypeError):
        return None
    edit = ready.edit
    same = (
        artifact.spec.spec_hash == edit.spec.spec_hash
        and artifact.model == edit.model
        and dict(artifact.params) == dict(edit.params)
    )
    return staged if same else None


def _plan(ready: _Ready, services: Services) -> tuple[RetakeCases, int, str]:
    """Which cases the next retake would run on, how many unused ones are left, and the
    sentence that says so. No case is issued."""
    state, floor = ready.state, ready.floor
    unit = _unit(services.profile, ready.edit.spec.floor.dataset)
    try:
        planned, left = floor.retake_plan(_used(ready.record.rows))
    except FloorError:
        raise RetakeError(
            "exam_not_configured", *state.lines,
            "The exam's extra cases could not be read, so no attempt was used.",
        ) from None
    total, number = floor.exam_size, state.next_attempt
    if planned == "unused" and ready.known:
        return planned, left, f"Attempt {number}, on {total} {unit} no earlier attempt used."
    if not ready.known:
        why = (
            "This computer's record of earlier attempts is incomplete, so it cannot say "
            f"which {unit} are unused"
        )
    elif left:
        why = f"Fewer than {total} unused {unit} are left"
    else:
        why = f"This exam has no unused {unit}"
    return "same_exam", left, (
        f"Same exam, attempt {number}. {why}, so the agent retakes the {total} {unit} of "
        "its first exam."
    )


def plan(folder: Path, services: Services) -> dict[str, JsonValue]:
    """What a retake and a practice run of the changed agent at ``folder`` would be, with
    0 model calls and no attempt used: the attempt's number, whether it would run on
    unused cases or the same exam, the pass mark as a count, the estimates and the cost
    cap, and the sentences that say so (``head``, ``cost``, ``practice_cost``), so every
    screen uses the same words. ``place_only`` when the last attempt already passed on
    this edit and only its folder is left to write. Counts, dollars and fixed words only.
    ``RetakeError`` when neither may run."""
    folder = Path(folder)
    with trust.configured_only():
        ready = _retakeable(folder, services)
        waiting = _waiting(ready, folder, services.workspace)
    edit, floor = ready.edit, ready.floor
    planned, left, head = _plan(ready, services)
    total = floor.exam_size
    practice_total = len(floor.tuning)
    unit, note = exam_words(edit.spec, services)
    cap = services.compiler.config.compile_budget.cost_usd
    estimate = _estimate(edit, total, services)
    practice_estimate = _estimate(edit, practice_total, services)
    number = ready.state.next_attempt
    if waiting is not None:
        number -= 1
        head = (
            f"Attempt {number} passed on this change, but its folder could not be "
            "written then. This writes it now."
        )
    return {
        "attempt": number, "cases": planned, "total": total,
        "pass_mark": pass_mark(edit.spec.floor.limit, total), "left": left, "unit": unit,
        "note": note, "estimate_usd": estimate, "cap_usd": cap,
        "practice_total": practice_total, "practice_estimate_usd": practice_estimate,
        "head": head,
        "cost": "No new attempt and no cost." if waiting is not None
        else _cost_words(estimate, cap),
        "practice_cost": _cost_words(practice_estimate, None),
        "place_only": waiting is not None,
    }


def _candidate(services: Services, edit: _Edit) -> HarnessArtifact:
    """The edited agent as a new artifact in the workspace: ``harness.py`` is written by
    the preset from the edited plan and settings. The changed folder's is never read."""
    base = edit.base
    preset = get_preset(base.preset)
    directory = services.compiler.artifact_root / "retake" / uuid4().hex[:12]
    directory.mkdir(parents=True)
    try:
        preset.emit(directory, edit.spec, edit.params, base.components)
        artifact = HarnessArtifact(
            spec=edit.spec.model_copy(deep=True),
            version=base.version,
            preset=base.preset,
            model=edit.model,
            components=base.components,
            params=edit.params,
            directory=directory,
            execution_mode=base.execution_mode,
        )
        artifact.save()
        return artifact
    except (PresetError, OSError, ValueError, TypeError):
        shutil.rmtree(directory, ignore_errors=True)
        raise RetakeError(
            "edit_invalid",
            "The edited plan and settings do not make an agent Architect can run. Fix "
            "them or undo.",
        ) from None


def _input_rate(services: Services, model: str) -> float | None:
    """The model's list price per million input tokens, when these settings price it."""
    try:
        price = getattr(services.adapter_factory(model), "_price", None)
        rate = getattr(price, "input_per_million", None)
    except Exception:
        return None
    return float(rate) if isinstance(rate, int | float) and rate > 0 else None


def _estimate(edit: _Edit, cases: int, services: Services) -> float | None:
    """What ``cases`` runs are likely to cost: the mean cost per run of the last exam,
    scaled by the two models' input prices when the edit changed the model. None when
    there is nothing to go on; the cost cap is then the only number shown."""
    report = edit.base.report
    if report is None or report.mean_cost_usd is None:
        return None
    estimate = report.mean_cost_usd * cases
    if edit.model == edit.base.model:
        return estimate
    old, new = _input_rate(services, edit.base.model), _input_rate(services, edit.model)
    if old is None or new is None:
        return None
    return estimate * new / old


def _cost_words(estimate: float | None, cap: float | None) -> str:
    """``About $0.03. It stops if it reaches $0.60, the build's cost cap.``"""
    stops = "" if cap is None else f"It stops if it reaches {money(cap)}, the build's cost cap."
    if estimate is None or estimate <= 0:
        return stops
    about = "Under $0.001." if estimate < 0.001 else f"About {money(estimate)}."
    return f"{about} {stops}".strip()


def _floor_origin(copy: Path) -> FloorOrigin:
    """The floor's origin as the version that passed recorded it (``EVIDENCE.json``)."""
    try:
        value = _load_json(copy / "EVIDENCE.json").get("floor_origin")
    except (OSError, ValueError, AttributeError):
        return "model_draft"
    if value in ("user_dataset", "public_dataset", "authored_cases", "model_draft"):
        return value  # type: ignore[no-any-return]
    return "model_draft"


def _unfinished(artifact: HarnessArtifact, cap: float, capped: bool) -> str:
    """Why an exam ended without a score, in plain words."""
    report = artifact.report
    counts = report.held_out_counts if report is not None else None
    if capped:
        return f"It reached its cost cap of {money(cap)} before it finished."
    if counts is not None and counts.over_budget:
        return "A run went over the agent's own budget, so the exam was not scored."
    if counts is not None and counts.invalid:
        return (
            f"{counts.invalid} of {counts.cases} answers were not in the agent's answer "
            "format, more than the exam allows."
        )
    return "The exam could not be scored."


# ------------------------------------------------------------------ retake


class _Moved(Exception):
    """The two files a retake read no longer hold the edit it examined."""


def _place(workspace: Path, folder: Path, staged: Path, examined: str) -> None:
    """Write the folder again from ``staged``, the package of the attempt that passed.

    Only while the folder still holds the edit that was examined (``examined`` is its
    digest): an undo or a second edit made while the exam ran is not written over. The
    copy Undo restores is kept afterwards; when that fails the pass stands, and Undo finds
    the staged copy by the version the folder's manifest names."""
    with trust.configured_only():
        if edit_digest(folder) != examined:
            raise _Moved()
        replace_package(staged, folder)
        try:
            remember_passed(workspace, folder, staged)
        except (ExportError, OSError, ValueError):
            pass


async def _finish_placing(work: asyncio.Future[None]) -> None:
    """Wait for the folder to be written, also through a cancel: the exam is over and the
    verdict is in, and a write stopped half way would leave the app with neither version.
    """
    try:
        await asyncio.shield(work)
    except asyncio.CancelledError:
        await asyncio.wait([work])
        current = asyncio.current_task()
        if current is not None:
            current.uncancel()
        work.result()


def _not_written(folder: Path, exc: ExportError | OSError) -> str:
    aside = getattr(exc, "aside", None)
    if isinstance(aside, Path):
        return (
            f"The agent's folder was moved to {_shown(aside)} and could not be moved back. "
            f"Rename it to {folder.name}."
        )
    return (
        "The agent's folder could not be written, so it is unchanged and still stopped. "
        "Close any program that has the folder open, then retake again: that writes the "
        "folder without a new attempt."
    )


async def retake(
    folder: Path,
    services: Services,
    consent: Callable[[str], bool],
    *,
    on_case: Callable[[int, int], None] | None = None,
) -> Outcome:
    """The changed agent at ``folder`` takes its exam again; see the module docstring.

    ``consent`` gets one line (the attempt, its cases, the estimate and the cost cap)
    before anything is spent; a no is ``RetakeError("cancelled")`` and uses no attempt.
    Cancelling the task once it runs keeps the attempt, recorded as cancelled, and leaves
    the folder stopped. ``on_case(done, total)`` counts answered cases, never right ones.

    A pass writes the folder only while it still holds the edit that was examined. When
    it does not (an undo or another edit made meanwhile), or the folder cannot be written,
    the folder is left alone and the outcome says so; the attempt is recorded as passed
    and not placed, and a retake of that same edit later writes the folder without a new
    attempt. A second retake of the same agent at the same time is refused (``busy``).
    """
    folder = Path(folder)
    workspace = services.workspace
    with trust.configured_only():
        key = _agent_key(folder, _passed_copy(folder, workspace))
    with _one_at_a_time(workspace, key):
        return await _retake(folder, services, consent, on_case)


async def _retake(
    folder: Path,
    services: Services,
    consent: Callable[[str], bool],
    on_case: Callable[[int, int], None] | None,
) -> Outcome:
    from architect.compiler.verification import RetakeExam, verify
    from architect.runtime.wallet import compilation_wallet

    workspace = services.workspace
    package = folder.name
    with trust.configured_only():
        ready = _retakeable(folder, services)
        waiting = _waiting(ready, folder, workspace)
    state, edit, floor, record, copy = (
        ready.state, ready.edit, ready.floor, ready.record, ready.copy
    )
    spec = edit.spec
    unit = _unit(services.profile, spec.floor.dataset)
    note = _note(services.profile, spec.floor.dataset)
    total = floor.exam_size
    need = pass_mark(spec.floor.limit, total)
    examined = edit_digest(folder)

    async def place(staged: Path) -> ExportError | OSError | _Moved | None:
        work = asyncio.ensure_future(
            asyncio.to_thread(_place, workspace, folder, staged, examined)
        )
        try:
            await _finish_placing(work)
        except (ExportError, OSError, _Moved) as exc:
            return exc
        return None

    if waiting is not None:
        # The last attempt passed on exactly this edit; only its folder is left to write.
        last = record.rows[-1]
        number, right = int(last["attempt"]), last.get("right")
        cases: RetakeCases = "unused" if last.get("cases") == "unused" else "same_exam"
        facts: dict[str, Any] = {
            "attempt": number, "cases": cases, "right": right, "total": total,
            "pass_mark": need,
        }
        stuck = await place(waiting)
        if stuck is not None:
            why = (
                "The agent's files changed just now, so its folder was left as it is."
                if isinstance(stuck, _Moved)
                else _not_written(folder, stuck)
            )
            return Outcome(
                "retake", False,
                (f"Attempt {number} passed, and no new attempt was used.", why), **facts,
            )
        _append(record, {"attempt": number, "result": "passed"})
        return Outcome(
            "retake", True,
            (
                f"{right} of {total}. Pass mark {need}. Passed. It is your agent.",
                f"{_source_words(cases, number, total, unit)} Its folder is written now, "
                "with no new attempt. Your app's calls work again.",
                *note,
            ),
            **facts,
        )

    number = state.next_attempt
    planned, _left, head = _plan(ready, services)
    cap = services.compiler.config.compile_budget
    line = f"{head} {_cost_words(_estimate(edit, total, services), cap.cost_usd)}"
    if not consent(line):
        raise RetakeError("cancelled", "Not retaken. No attempt was used.")
    candidate = _candidate(services, edit)
    recorded = False
    used = _used(record.rows)

    def note_cases(cases: RetakeCases, case_ids: tuple[str, ...]) -> None:
        nonlocal recorded
        if not record.rows and state.attempt == 1 and state.total is not None:
            # The exam the agent was built on, kept so the list starts at attempt 1.
            _append(
                record,
                {"attempt": 1, "cases": "first_exam", "case_ids": [], "result": "passed",
                 "right": state.right, "total": state.total},
            )
        _append(
            record,
            {
                "attempt": number, "cases": cases, "case_ids": list(case_ids),
                "result": "started", "right": None, "total": total,
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
                # Which edit this attempt examined, in words and as the two files'
                # digest, and the folder it was in.
                "what": _what(edit), "edit": examined, "folder": str(folder.resolve()),
            },
        )
        recorded = True

    def finish(result: str, right: int | None = None, **more: Any) -> None:
        _append(record, {"attempt": number, "result": result, "right": right, **more})

    started = time.monotonic()
    try:
        with compilation_wallet(cap) as wallet:
            verified = await verify(
                candidate, floor, services.compiler.adapter_factory, services.compiler.runtime,
                retake=RetakeExam(number, used, note_cases, same_exam=not ready.known),
                on_case=on_case,
            )
    except asyncio.CancelledError:
        if recorded:
            finish("cancelled")
        shutil.rmtree(candidate.directory, ignore_errors=True)
        raise
    except BaseException:
        shutil.rmtree(candidate.directory, ignore_errors=True)
        raise
    seconds = time.monotonic() - started
    report = verified.report
    assert report is not None
    if not recorded:  # the exam never reached its cases; the attempt is used all the same
        note_cases(report.retake_cases or "same_exam", ())
    cases = "unused" if record.rows[-1].get("cases") == "unused" else "same_exam"
    counts = exam_counts(report)
    right = counts[0] if counts else None
    facts = {
        "attempt": number, "cases": cases, "right": right, "total": total,
        "pass_mark": need, "cost_usd": wallet.cost_usd, "seconds": seconds,
    }
    source = _source_words(cases, number, total, unit)
    score = f"{right} of {total}. Pass mark {need}." if right is not None else None
    try:
        if not verified.licensed:
            finish("did_not_pass", right)
            lines = [
                f"{score} Did not pass."
                if score is not None
                else f"{_unfinished(verified, cap.cost_usd, wallet.exhausted)} Did not pass.",
                f"{source} Your agent is still stopped.",
                *note,
            ]
            return Outcome("retake", False, tuple(lines), **facts)
        staged: Path | None = None
        failed: ExportError | OSError | _Moved | None
        try:
            with trust.configured_only():
                manifest, _sha = await asyncio.to_thread(
                    stage_artifact,
                    verified.directory,
                    workspace / "exports",
                    package,
                    flag=read_flag(copy),
                    model_class=evidence_class_of(provider_of(services, verified.model)),
                    floor_origin=_floor_origin(copy),
                )
            staged = workspace / "exports" / manifest.artifact_digest[:16] / package
            failed = await place(staged)
        except (ExportError, OSError) as exc:
            failed = exc
        if failed is not None:
            # It passed, and the folder was not written: it is as the person left it.
            finish(
                "not_placed", right,
                **({} if staged is None else {"staged": staged.parent.name}),
            )
            said = (
                "The agent's files changed while the exam ran, so its folder was left as "
                "it is now. What is in it has not been examined."
                if isinstance(failed, _Moved)
                else _not_written(folder, failed)
            )
            return Outcome(
                "retake", False,
                (f"{score} Passed, but its folder was not written.", said,
                 f"{source} The attempt was used."),
                **facts,
            )
        finish("passed", right)
        lines = [f"{score} Passed. It is your agent.", f"{source} Your app's calls work again."]
        if cases == "same_exam":
            seen = 1 + sum(1 for item in record.rows if item.get("cases") == "same_exam")
            lines.append(
                f"This agent has now been scored on these {unit} {times(seen)}, so this "
                "score is easier to reach than a first score."
            )
        return Outcome("retake", True, (*lines, *note), **facts)
    finally:
        shutil.rmtree(candidate.directory, ignore_errors=True)


# ------------------------------------------------------------------ practice


async def practice(
    folder: Path, services: Services, consent: Callable[[str], bool]
) -> Outcome:
    """Score the changed agent on the exam's practice half. Not an attempt: nothing is
    recorded, no exam case is used and the folder is not touched. It pays for its model
    calls, under the same cost cap as a retake. The same changes are refused as there."""
    from architect.runtime.evaluation import evaluate_cases
    from architect.runtime.wallet import compilation_wallet

    folder = Path(folder)
    with trust.configured_only():
        ready = _retakeable(folder, services)
    edit, floor = ready.edit, ready.floor
    cases = floor.tuning
    unit = _unit(services.profile, edit.spec.floor.dataset)
    cap = services.compiler.config.compile_budget
    line = (
        f"Practice, on the {len(cases)} practice {unit}, which no exam uses. Not an "
        f"attempt. {_cost_words(_estimate(edit, len(cases), services), cap.cost_usd)}"
    )
    if not consent(line):
        raise RetakeError("cancelled", "No practice run. Nothing was spent.")
    candidate = _candidate(services, edit)
    started = time.monotonic()
    try:
        with compilation_wallet(cap) as wallet:
            measured = await evaluate_cases(
                candidate, floor, cases, services.compiler.adapter_factory,
                services.compiler.runtime,
            )
    finally:
        shutil.rmtree(candidate.directory, ignore_errors=True)
    right = measured.counts.correct if measured.counts is not None else None
    if right is None:
        first = (
            f"The practice run reached its cost cap of {money(cap.cost_usd)} before it "
            "finished, so there is no score."
            if wallet.exhausted
            else "The practice run could not be scored."
        )
        lines: tuple[str, ...] = (f"{first} Not an attempt.",)
    else:
        lines = (f"{right} of {len(cases)} practice {unit} right. Not an attempt.",)
    return Outcome(
        "practice", None, (*lines, *_note(services.profile, edit.spec.floor.dataset)),
        right=right, total=len(cases),
        pass_mark=pass_mark(edit.spec.floor.limit, len(cases)),
        cost_usd=wallet.cost_usd, seconds=time.monotonic() - started,
    )


# ------------------------------------------------------------------ undo


def _set_aside(folder: Path) -> Path | None:
    """The agent's folder as a replace that died half way left it: moved beside itself
    and never moved back. Only when the folder is gone and exactly one such copy is here.
    """
    if folder.exists() or not folder.parent.is_dir():
        return None
    found = [path for path in folder.parent.glob(f".{folder.name}.old-*") if path.is_dir()]
    return found[0] if len(found) == 1 else None


def undo(folder: Path, workspace: Path) -> Outcome:
    """Put back the version that passed, byte for byte. 0 model calls, not an attempt.

    The copy is checked first (its files, pins and signed report), then placed where the
    folder is; a failure on the way leaves the folder as it was. A folder that an
    interrupted replace left moved aside is moved back first. ``RetakeError`` with
    ``no_copy`` when this workspace holds no copy that passes its checks, ``not_written``
    when the copy could not be placed, and ``not_stopped`` when the folder is already the
    version that passed."""
    folder, workspace = Path(folder), Path(workspace)
    with trust.configured_only():
        aside = _set_aside(folder)
        if aside is not None:
            try:
                os.replace(aside, folder)
            except OSError:
                pass
        if not check_package(folder, read_pin(folder), registry=None).codes:
            raise RetakeError(
                "not_stopped", "Nothing to undo. This is the version that passed."
            )
        copy = _passed_copy(folder, workspace)
        if copy is None:
            raise RetakeError(
                "no_copy",
                "This computer has no copy of the version that passed. Restore "
                f"{_shown(folder)} with git.",
            )
        made = False
        if not folder.exists() and folder.parent.is_dir():
            folder.mkdir()  # the folder was deleted: the copy goes where it was
            made = True
        try:
            replace_package(copy, folder)
        except ExportError as exc:
            if made:
                shutil.rmtree(folder, ignore_errors=True)
            if exc.aside is not None:
                why = (
                    f"The agent's folder was moved to {_shown(exc.aside)} and could not be "
                    f"moved back. Rename it to {folder.name}."
                )
            else:
                why = (
                    "The folder is as it was. Close any program that has it open, then "
                    "undo again."
                )
            raise RetakeError(
                "not_written", f"The version that passed could not be put back. {why}"
            ) from None
        artifact = HarnessArtifact.load(folder / "artifact")
    record = _record(workspace, _agent_key(folder, copy))
    last = record.rows[-1] if record.rows else None
    if (
        last is not None
        and last.get("result") in ("did_not_pass", "cancelled", "not_placed")
        and (not isinstance(last.get("folder"), str) or _same_path(Path(last["folder"]), folder))
    ):
        # The attempts list says the change that did not pass was undone.
        try:
            _append(record, {"attempt": last["attempt"], "undone": True})
        except OSError:
            pass
    report = artifact.report
    assert report is not None
    counts = exam_counts(report)
    score = f"{counts[0]} of {counts[1]}" if counts else f"score {report.floor_score:.2f}"
    return Outcome(
        "undo", None,
        (
            f"The version that passed is back. It is {pretty_model(artifact.model)}, "
            f"{score}, passed, attempt {attempt_of(report)}. 0 model calls.",
        ),
        attempt=attempt_of(report),
        right=counts[0] if counts else None,
        total=counts[1] if counts else None,
        pass_mark=counts[2] if counts else None,
    )
