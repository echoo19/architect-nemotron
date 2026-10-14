"""Every screen the session shows; the only module that formats money, time and hashes.

Rendering is plain ASCII with no ANSI or cursor movement, at most 100 columns, and
deterministic for a given input. Screens are returned as text ending in a newline; the
session appends ``footer`` after each one because only the session knows the spend.
Three screens carry content (the Spec, a run's output and the floor draft under
construction) because the operator must read what they approve, what they got and what
they are asked to review; everything else shows counts, codes, hashes and money. A
configured floor's cases never reach a screen: ``floor_screen`` and ``case_screen`` only
ever see the in-memory draft. ``licensed`` is never taken from a cached value:
``result_screen`` asks the outcome, which reloads the artifact from disk.
"""

from __future__ import annotations

import json
import math
import textwrap
from collections.abc import Sequence
from pathlib import Path

from pydantic import JsonValue

from architect.profiles import Profile
from architect.registry import RegistryEntry
from architect.runtime import RunResult
from architect.session.codes import explain, hint_for, is_code
from architect.session.edit import Blocker, cents, suggested_cost
from architect.session.floors import (
    DEFAULT_DRAFT_COUNT,
    RECOMMENDED_CASES,
    synthetic_label,
    unreviewed,
)
from architect.session.state import (
    Draft,
    DraftCase,
    FloorDraft,
    FloorSummary,
    ModelRow,
    Outcome,
    Progress,
    Spend,
    StatusView,
)
from architect.spec import FloorKind, HarnessArtifact, Lifecycle, Spec

__all__ = [
    "INFERENCE_FALLBACK",
    "ascii_text",
    "budget_screen",
    "case_screen",
    "clock",
    "compile_banner",
    "diff_screen",
    "duration",
    "entry_screen",
    "failure",
    "floor_screen",
    "floors_screen",
    "footer",
    "hash8",
    "model_menu",
    "money",
    "profile_summary",
    "progress_line",
    "registry_screen",
    "result_screen",
    "review_screen",
    "reviewed_line",
    "route_banner",
    "run_screen",
    "small_floor_warning",
    "spec_json",
    "spec_screen",
    "status_screen",
    "wrap",
]

WIDTH = 100
# The Stage 0 fallback model named on the menu (bootstrap.INFERENCE_FALLBACK).
INFERENCE_FALLBACK = "claude-haiku-4-5-20251001"
_SCHEMA_INLINE_LIMIT = 90
_FLOOR_CASES_SHOWN = 12
_REVIEW_FORM = "ok|expected <json>|drop"
_ADD_FORM = f'add {{"task":..., "expected":...}} | paste | draft {DEFAULT_DRAFT_COUNT}'


def money(x: float) -> str:
    """Four decimals below one dollar, two above; never scientific notation."""
    magnitude = abs(x)
    return f"${x:.4f}" if magnitude < 1 else f"${x:.2f}"


def _usd(x: float) -> str:
    """Budget-style money: "$0.70", "$0.35", "$0.0031"; trailing zeros trimmed to cents.

    Budgets and reservations are round numbers the operator typed or will type, so
    they read as cents; measured spend keeps the four decimals of ``money``.
    """
    if abs(x) >= 1:
        return f"${x:.2f}"
    text = f"{x:.4f}".rstrip("0")
    whole, _, fraction = text.partition(".")
    return f"${whole}.{fraction.ljust(2, '0')}"


def duration(s: float) -> str:
    """Tenths below a minute ("18.6s"), then "1m02s", then "1h03m"."""
    if s < 60:
        return f"{s:.1f}s"
    whole = int(s)
    if whole < 3600:
        return f"{whole // 60}m{whole % 60:02d}s"
    return f"{whole // 3600}h{(whole % 3600) // 60:02d}m"


def _wall(s: float) -> str:
    """Footer wall time always shows minutes ("0m24s") so columns line up across screens."""
    whole = int(s)
    if whole < 3600:
        return f"{whole // 60}m{whole % 60:02d}s"
    return duration(s)


def _budget_time(s: float) -> str:
    """Budget deadlines are round numbers; "30m" reads better than "30m00s"."""
    whole = int(s)
    if whole >= 60 and whole % 60 == 0 and whole < 3600:
        return f"{whole // 60}m"
    return duration(s)


def hash8(h: str) -> str:
    """First 8 characters of a hash or id; callers escape it because ids come from disk."""
    return h[:8]


def clock(elapsed_s: float) -> str:
    whole = max(0, int(elapsed_s))
    return f"[{whole // 60:02d}:{whole % 60:02d}]"


# C0 controls and DEL are ASCII, so ``str.isascii`` cannot catch them; ESC starts ANSI
# sequences, CR/LF forge whole screen lines, BEL rings the terminal.
_CONTROL_ESCAPES = {n: f"\\x{n:02x}" for n in (*range(0x20), 0x7F)}


def _ascii(text: str) -> str:
    """Escape non-ASCII and control characters so the terminal never sees raw model bytes.

    A model-inferred field is rendered inside a fixed line and must never be able to
    move the cursor, colour text or end the line: the operator approves the Spec from
    what this screen shows, so every byte of it must be printable.
    """
    escaped = text.encode("ascii", "backslashreplace").decode("ascii")
    return escaped.translate(_CONTROL_ESCAPES)


ascii_text = _ascii  # the same escape for a model-named token printed by session.py


def wrap(text: str, width: int = WIDTH) -> str:
    """Wrap each over-long line at ``width``, continuing under the line's own indent."""
    out: list[str] = []
    for line in text.split("\n"):
        if len(line) <= width:
            out.append(line)
            continue
        indent = line[: len(line) - len(line.lstrip(" "))]
        out.extend(
            textwrap.wrap(
                line,
                width=width,
                subsequent_indent=indent + "  ",
                break_long_words=True,
                break_on_hyphens=False,
                drop_whitespace=True,
            )
            or [line[:width]]
        )
    return "\n".join(out)


def _compact(value: JsonValue) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _number(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:.4f}".rstrip("0")


def _thousands(n: int) -> str:
    return f"{n:,}"


def spec_json(spec: Spec) -> str:
    return spec.model_dump_json(indent=2)


def _schema_lines(label: str, mark: str, value: dict[str, JsonValue]) -> list[str]:
    compact = _compact(value)
    if len(compact) <= _SCHEMA_INLINE_LIMIT:
        return [f"  {label:<14} {mark} {compact}"]
    pretty = json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True)
    return [f"  {label:<14} {mark}", *(f"                   {row}" for row in pretty.split("\n"))]


def _blocker_description(
    blocker: Blocker, spec: Spec, floor: FloorSummary | None, min_call_cost: float | None
) -> str:
    code = blocker.code
    if code == "budget_below_quote" and min_call_cost is not None:
        return (
            f"{_usd(spec.budget.cost_usd)} is below the {_usd(min_call_cost)} minimum "
            f"reservation for {_ascii(spec.model_target)}; every run would be refused at preflight"
        )
    if code == "floor_not_configured":
        return f"dataset '{_ascii(spec.floor.dataset)}' is not in the profile"
    if code == "floor_kind_mismatch":
        configured = f" as {floor.kind.value}" if floor is not None else " with another kind"
        return (
            f"dataset '{_ascii(spec.floor.dataset)}' is configured{configured}; "
            f"the Spec says {spec.floor.kind.value}"
        )
    if code == "secondary_floor_not_configured" and spec.secondary_floor is not None:
        return f"secondary dataset '{_ascii(spec.secondary_floor.dataset)}' is not in the profile"
    if code == "model_not_configured":
        return f"model '{_ascii(spec.model_target)}' is not in the profile"
    if code in {"input_schema_missing", "output_schema_missing"}:
        field = code.removesuffix("_missing")
        schema: dict[str, JsonValue] = getattr(spec, field)
        shape = "{}" if not schema else "type object with no properties"
        checked = "no input is checked" if field == "input_schema" else "any output passes"
        return f"{field} is {shape}; {checked}"
    if code == "compile_deadline_short":
        return (
            f"the compile deadline is under twice the {_number(spec.budget.compute_s)}s per-run "
            "compute budget; verification may time out (warning)"
        )
    return explain(code)


def _blocker_lines(
    blockers: Sequence[Blocker], spec: Spec, floor: FloorSummary | None, min_call_cost: float | None
) -> list[str]:
    lines: list[str] = []
    for blocker in blockers:
        description = _blocker_description(blocker, spec, floor, min_call_cost)
        # A 21-wide code column as in the contract screen; longer codes get one space.
        column = blocker.code.ljust(21) if len(blocker.code) < 21 else blocker.code + " "
        lines.append(f"! {column}{description}")
        lines.append(f"                       fix: {blocker.fix}")
        if blocker.alternative is not None:
            lines.append(f"                       or: {_ascii(blocker.alternative)}")
    return lines


def _configured_line(floor: FloorSummary | None) -> str:
    if floor is None:
        return "configured: no"
    misses = ""
    if floor.misses_allowed is not None:
        plural = "" if floor.misses_allowed == 1 else "es"
        misses = f" ({floor.misses_allowed} miss{plural} allowed)"
    return (
        f"configured: yes, {floor.count} cases -> {floor.tuning} tuning / "
        f"{floor.held_out} held-out{misses}"
    )


def spec_screen(
    draft: Draft,
    blockers: Sequence[Blocker],
    *,
    floor: FloorSummary | None,
    min_call_cost: float | None,
    stage0: Spend | None,
    approved: bool,
) -> str:
    """The Spec exactly as the token binds it, with provenance marks and blockers."""
    spec = draft.spec
    marks = {name: "*" if origin == "user" else "~" for name, origin in spec.provenance.items()}

    def mark(name: str) -> str:
        return marks.get(name, "~")

    if spec.lifecycle is not Lifecycle.INSTALL:
        lifecycle = "lifecycle expire (single use, no approval needed)"
    elif approved:
        lifecycle = "lifecycle install"
    else:
        lifecycle = "lifecycle install (approval required)"
    header = f"Spec {draft.hash8}  {lifecycle}"
    if stage0 is not None:
        header += f"  stage 0: {money(stage0.cost_usd)}, {duration(stage0.wall_s)}"
    header += f"  origin {draft.origin}"
    if approved:
        header += "  approved"
    lines = [header, f"  {'task':<14} {mark('task')} {_ascii(spec.task)}"]
    lines += _schema_lines("input_schema", mark("input_schema"), spec.input_schema)
    lines += _schema_lines("output_schema", mark("output_schema"), spec.output_schema)
    floor_def = spec.floor
    lines.append(
        f"  {'floor':<14} {mark('floor')} {floor_def.kind.value}  dataset "
        f"{_ascii(floor_def.dataset)}  limit >= {floor_def.limit:.2f}"
    )
    lines.append(f"                   {_configured_line(floor)}")
    if floor is not None and synthetic_label(floor):
        lines.append(f"                   {_ascii(synthetic_label(floor))}")
    secondary = spec.secondary_floor
    secondary_text = "none"
    if secondary is not None:
        secondary_text = (
            f"{secondary.kind.value}  dataset {_ascii(secondary.dataset)}  "
            f"limit {secondary.limit:.2f}"
        )
    lines.append(f"  {'secondary':<14} {mark('secondary_floor')} {secondary_text}")
    budget_text = f"{_usd(spec.budget.cost_usd)} per run, {_number(spec.budget.compute_s)}s"
    lines.append(f"  {'budget':<14} {mark('budget')} {budget_text}")
    if min_call_cost is not None:
        fits = int(spec.budget.cost_usd // min_call_cost) if min_call_cost > 0 else 0
        plural = "" if fits == 1 else "s"
        lines.append(
            f"                   min reservation {_ascii(spec.model_target)} "
            f"{_usd(min_call_cost)}/call -> {fits} call{plural} fit"
        )
    lines.append(f"  {'model_target':<14} {mark('model_target')} {_ascii(spec.model_target)}")
    preset = _ascii(spec.preset_hint) if spec.preset_hint else "none"
    lines.append(f"  {'domain/preset':<14} {mark('domain')} {_ascii(spec.domain)} / {preset}")
    tools = ", ".join(_ascii(tool) for tool in spec.tools) or "none"
    pipeline = ", ".join(_ascii(step.name) for step in spec.pipeline) or "none"
    lines.append(
        f"  {'tools/pipeline':<14} {mark('tools')} {tools} / {pipeline}      "
        f"execution_config {mark('execution_config')} {_compact(spec.execution_config)}"
    )
    lines.append("  * set by you   ~ inferred")
    lines += _blocker_lines(blockers, spec, floor, min_call_cost)
    lines.append(f"spec_hash {spec.spec_hash}")
    # The Next: line is fixed text even while blockers are shown (Section 8.1); the
    # blockers above carry their own fixing command and approve is refused until then.
    first = f"approve {draft.hash8}" if spec.lifecycle is Lifecycle.INSTALL else "compile"
    lines.append(
        f"Next: {first} | set <field> <value> | revise <text> | json | budget | floor | discard"
    )
    return wrap("\n".join(lines)) + "\n"


def budget_screen(
    spec: Spec, profile: Profile, min_call_cost: float | None, floor: FloorSummary | None
) -> str:
    """Reservation arithmetic only; the operator applies any change by typing it."""
    compile_budget = profile.compile.compile_budget
    lines = [f"budget for {_ascii(spec.model_target)}"]
    if min_call_cost is None:
        lines.append("  min reservation per call   unknown (model not in profile)")
    else:
        lines.append(f"  min reservation per call   {_usd(min_call_cost)}")
        lines.append(
            f"  per-run budget for 2 calls {_usd(suggested_cost(min_call_cost, 2))}"
            f"   set budget.cost_usd {cents(suggested_cost(min_call_cost, 2))}"
        )
        lines.append(
            f"  per-run budget for 3 calls {_usd(suggested_cost(min_call_cost, 3))}"
            f"   set budget.cost_usd {cents(suggested_cost(min_call_cost, 3))}"
        )
    lines.append(
        f"  per run now                {_usd(spec.budget.cost_usd)} / "
        f"{_number(spec.budget.compute_s)}s"
    )
    lines.append(
        f"  compile budget             {money(compile_budget.cost_usd)} / "
        f"{_budget_time(compile_budget.compute_s)}   budget compile <usd> <seconds>"
    )
    if min_call_cost is not None and floor is not None:
        candidates = profile.compile.max_candidates
        calls = candidates * floor.tuning + floor.held_out
        upper = calls * min_call_cost
        lines.append(
            f"  compile reservation        up to {_usd(upper)} = ({candidates} candidates x "
            f"{floor.tuning} tuning + {floor.held_out} held-out) x {_usd(min_call_cost)}"
        )
    lines.append("  note: measured live cost is typically about 1% of the reservation")
    return wrap("\n".join(lines)) + "\n"


def diff_screen(lines: Sequence[str], new_hash8: str) -> str:
    body = "\n".join(_ascii(line) for line in lines) if lines else "no changes"
    return wrap(f"{body}\nspec is now {new_hash8}") + "\n"


def progress_line(p: Progress) -> str:
    """One content-free line: stage, calls, rows, spend and what is left."""
    running = f"{p.in_flight} running"
    if p.failed:
        running += f", {p.failed} failed"
    text = (
        f"{clock(p.elapsed_s)} {p.stage_text:<20}  calls {p.calls} ({running})"
        f"  spent {money(p.spend.cost_usd)}  left {money(max(0.0, p.left_usd))} / "
        f"{_wall(max(0.0, p.left_s))}"
    )
    if p.quality_rows or p.accepted_rows or p.rewrite_rows:
        # Row counts trail the money so a wrap never splits what the operator watches.
        text += (
            f"  rows: quality {p.quality_rows}, accepted {p.accepted_rows}, "
            f"rewrite {p.rewrite_rows}"
        )
    return wrap(text) + "\n"


def forge_progress_line(
    *,
    checks: int,
    dryruns: int,
    measures: int,
    max_measures: int,
    submissions: int,
    max_submissions: int,
    spawns: int,
    spent_usd: float,
    cap_usd: float,
) -> str:
    """The Forge stage line (Section 12.3): counts and money only, never source or cases."""
    return wrap(
        f"forge  checks {checks}  dryruns {dryruns}  measures {measures}/{max_measures}  "
        f"submits {submissions}/{max_submissions}  spawns {spawns}  "
        f"{money(spent_usd)}/{money(cap_usd)} (API-equivalent)"
    ) + "\n"


def compile_banner(spec: Spec, profile: Profile, approved: bool) -> str:
    compile_budget = profile.compile.compile_budget
    authority = "approved by operator" if approved else "lifecycle expire, no approval needed"
    # Two lines by design: a real model id would push a single line past 100 columns.
    return (
        wrap(
            f"compiling {hash8(spec.spec_hash)}  model {_ascii(spec.model_target)}  compile budget "
            f"{money(compile_budget.cost_usd)} / {_budget_time(compile_budget.compute_s)}\n"
            f"{authority}  (cancel or Ctrl-C to stop)"
        )
        + "\n"
    )


def route_banner(spec: Spec, profile: Profile, target: str, approved: bool) -> str:
    """The banner of an approve a standing-grant reuse delivers (Phase 10 Section 13.2).

    The decision is taken again when the compile starts: while it still reuses the
    installed entry nothing is compiled; only a changed route compiles, within the
    compile budget shown on the second line.
    """
    compile_budget = profile.compile.compile_budget
    authority = "approved by operator" if approved else "lifecycle expire, no approval needed"
    return (
        wrap(
            f"routing {hash8(spec.spec_hash)} to installed {_ascii(target)}  no Stage 1 to 4 "
            "and no compile spend\n"
            f"a compile runs only if the route changes: model {_ascii(spec.model_target)}  "
            f"compile budget {money(compile_budget.cost_usd)} / "
            f"{_budget_time(compile_budget.compute_s)}\n"
            f"{authority}  (cancel or Ctrl-C to stop)"
        )
        + "\n"
    )


def _failure_line(code: str, spec: Spec, score_text: str = "") -> str:
    shown = code if is_code(code) else "unknown_failure"
    parts = [f"  {shown:<22} {explain(shown)}"]
    if score_text:
        parts.append(score_text)
    hint = hint_for(shown, spec)
    if hint:
        parts.append(f"next: {hint}")
    return "   ".join(parts)


def result_screen(outcome: Outcome, floor: FloorSummary | None, spec: Spec) -> str:
    """The Stage 4 verdict; ``licensed`` is recomputed from disk by ``outcome.licensed``."""
    result = outcome.result
    report = result.report
    artifact = result.artifact
    licensed = outcome.licensed
    stages = ",".join(str(stage) for stage in result.stages) or "-"
    # The id comes from report.json on disk, so it is escaped like every other file field.
    ident = _ascii(hash8(report.verification_id)) if report is not None else "-"
    verdict = "YES" if licensed else "NO"
    lines = [
        f"Verification (stage 4)  id {ident}  stages {stages}  licensed: {verdict} "
        "(recomputed from disk)"
    ]
    if report is not None:
        met = report.floor_score >= report.floor_limit
        if report.floor_kind is FloorKind.HUMAN_EDIT_RATE:
            met = report.floor_score <= report.floor_limit
        lines.append(
            f"  {'floor':<10} {report.floor_kind.value}  score {report.floor_score:.2f} "
            f"{'>=' if met else '<'} limit {report.floor_limit:.2f}   "
            f"held-out cases {report.held_out_count}"
        )
        if floor is not None and synthetic_label(floor):
            lines.append(f"             {_ascii(synthetic_label(floor))}")
        if report.secondary_floor_kind is not None and report.secondary_floor_score is not None:
            limit = report.secondary_floor_limit or 0.0
            lines.append(
                f"  {'secondary':<10} {report.secondary_floor_kind.value}  score "
                f"{report.secondary_floor_score:.2f} vs limit {limit:.2f}"
            )
        mean = f"   mean {money(report.mean_cost_usd)}" if report.mean_cost_usd is not None else ""
        lines.append(
            f"  {'per run':<10} max {money(report.cost_usd)}  max {duration(report.compute_s)}  "
            f"max tokens {_thousands(report.tokens)}{mean}"
        )
        fits = report.cost_usd <= spec.budget.cost_usd and report.compute_s <= spec.budget.compute_s
        lines.append(
            f"  {'budget':<10} {_usd(spec.budget.cost_usd)} / {_number(spec.budget.compute_s)}s"
            f" -> {'fits' if fits else 'exceeded'}"
        )
    else:
        lines.append("  floor      not verified (no report)")
    failures = tuple(dict.fromkeys((*result.failures, *(report.failures if report else ()))))
    if failures:
        lines.append("  failures")
        for code in failures:
            score_text = ""
            if report is not None and code in {"primary_floor_failed", "secondary_floor_failed"}:
                score_text = f"score {report.floor_score:.2f} < {report.floor_limit:.2f}"
            lines.append(_failure_line(code, spec, score_text))
    else:
        lines.append("  failures   none")
    if artifact is not None:
        components = ", ".join(_ascii(c) for c in artifact.components) or "none"
        warm = "yes" if result.warm_started else "no"
        lines.append(
            f"  {'artifact':<10} {_ascii(str(artifact.directory))}  version {artifact.version}  "
            f"preset {_ascii(artifact.preset)}  model {_ascii(artifact.model)}  components "
            f"{components}  warm start: {warm}"
        )
    if result.reused:
        # Phase 10 Section 10.2: routing chose an installed entry; no Stage 1 to 4 ran.
        target = getattr(result.route, "target", None)
        label = f"{_ascii(str(getattr(target, 'name', '-')))}@{getattr(target, 'revision', '-')}"
        lines.append(
            f"  {'reused':<10} installed {label}, licensed on a fresh load; no Stage 1 to 4 "
            "ran in this compile. Stage 4 issued that license earlier; routing only chose it"
        )
        nxt = "run {json} | report | again | new (already installed; never installed again)"
    elif licensed and spec.lifecycle is Lifecycle.INSTALL:
        nxt = "install <name> | run {json} | report | again | new"
    elif licensed:
        nxt = "run {json} (single use) | again | new"
    else:
        nxt = "again (edit and recompile) | report | new"
    lines.append(f"Next: {nxt}")
    return wrap("\n".join(lines)) + "\n"


def run_screen(run: RunResult, model: str, *, expired_after: bool) -> str:
    """The run's output, pretty-printed; the one screen besides the Spec that shows content."""
    header = (
        f"run {_ascii(hash8(run.run_id))}  {_ascii(model)}  {money(run.cost_usd)}  "
        f"{duration(run.wall_time_s)}  {_thousands(run.tokens_in)} in / "
        f"{_thousands(run.tokens_out)} out"
    )
    body = json.dumps(run.output, indent=2, ensure_ascii=True, allow_nan=False)
    lines = [header, body]
    if expired_after:
        lines.append("artifact consumed (single use)")
    return wrap("\n".join(lines)) + "\n"


def registry_screen(rows: Sequence[tuple[RegistryEntry, bool]]) -> str:
    if not rows:
        return "registry: no installed artifacts\n"
    lines = [f"{'name':<15} {'rev':>3}  {'spec':<8}  {'ver':>3}  {'licensed':<8}  directory"]
    for entry, licensed in rows:
        lines.append(
            f"{_ascii(entry.name):<15} {entry.revision:>3}  {hash8(entry.spec_hash):<8}  "
            f"{entry.artifact_version:>3}  {'yes' if licensed else 'no':<8}  "
            f"{_ascii(str(entry.directory))}"
        )
    return wrap("\n".join(lines)) + "\n"


def entry_screen(
    entry: RegistryEntry, artifact: HarnessArtifact, floor: FloorSummary | None
) -> str:
    """One registry entry with its evidence line; the license is the artifact's own."""
    report = artifact.report
    licensed = "yes" if artifact.licensed else "no"
    budget = artifact.spec.budget
    lines = [
        f"{_ascii(entry.name)}  revision {entry.revision}  spec {hash8(entry.spec_hash)}  "
        f"version {entry.artifact_version}  licensed {licensed}",
        f"  directory  {_ascii(str(entry.directory))}",
        f"  model      {_ascii(artifact.model)}  preset {_ascii(artifact.preset)}  components "
        f"{', '.join(_ascii(c) for c in artifact.components) or 'none'}",
        f"  lifecycle  {artifact.spec.lifecycle.value}  budget {_usd(budget.cost_usd)}"
        f" / {_number(budget.compute_s)}s",
    ]
    if report is not None:
        lines.append(
            f"  evidence   {report.floor_kind.value}  score {report.floor_score:.2f} >= limit "
            f"{report.floor_limit:.2f}  held-out cases {report.held_out_count}  "
            f"id {_ascii(hash8(report.verification_id))}"
        )
    else:
        lines.append("  evidence   none on disk")
    if floor is not None and synthetic_label(floor):
        lines.append(f"             {_ascii(synthetic_label(floor))}")
    return wrap("\n".join(lines)) + "\n"


def _case_flag(origin: str, reviewed: bool, corrected: bool) -> str:
    if origin == "operator":
        return "[op]"
    if origin == "file":
        return "[file]"
    if corrected:
        return "[draft fixed]"
    return "[draft ok]" if reviewed else "[draft ?]"


def small_floor_warning(held_out: int, limit: float | None) -> str:
    """The Section 7.6 size-honesty line for a floor below ``RECOMMENDED_CASES``.

    ``M = floor(H * (1 - L))`` is the number of held-out misses the limit tolerates;
    the float product is rounded first because ``10 * (1 - 0.8)`` is ``1.9999...``.
    Without a limit (a floor started from READY, where no Spec is on screen) the
    arithmetic clause is omitted because it cannot be computed honestly.
    """
    if limit is None:
        return f"{held_out} held-out cases; small floors license lucky harnesses"
    misses = max(0, math.floor(round(held_out * (1 - limit), 9)))
    return (
        f"{held_out} held-out cases at limit {limit:.2f} allow at most {misses} misses; "
        "small floors license lucky harnesses"
    )


def _clip(text: str, width: int) -> str:
    """Cut ``text`` to ``width`` columns, ending in "..." when anything was cut."""
    if len(text) <= width:
        return text
    return text[: max(0, width - 3)] + "..."


def _pending(item: DraftCase) -> bool:
    return item.origin == "model_draft" and not item.reviewed


def _case_lines(item: DraftCase) -> list[str]:
    """Two lines per case: id, flag and the task, then the expected value, each clipped."""
    corrected = item.case.metadata.get("corrected") is True
    flag = _case_flag(item.origin, item.reviewed, corrected)
    head = f"  {_ascii(item.case.case_id)}  {flag}  task: "
    expected_head = "    expected: "
    return [
        head + _clip(_ascii(_compact(item.case.task)), WIDTH - len(head)),
        expected_head + _clip(_ascii(_compact(item.case.expected)), WIDTH - len(expected_head)),
    ]


def _floor_next(draft: FloorDraft, reasons: Sequence[str], pending: Sequence[DraftCase]) -> str:
    if not draft.cases:
        return f"Next: use <file> | {_ADD_FORM}"
    if pending:
        first = _ascii(pending[0].case.case_id)
        return f"Next: review {first} {_REVIEW_FORM} | accept (after review) | back"
    if reasons:
        return f"Next: {_ADD_FORM} | back"
    return "Next: accept | back"


def floor_screen(draft: FloorDraft, reasons: Sequence[str], *, limit: float | None = None) -> str:
    """The floor under construction: counts, the cases with their content, what blocks accept.

    Every listed case is the in-memory draft's own (never a configured floor's): the
    operator is asked to review these labels, so the screen shows them. Unreviewed
    drafted cases come first; after ``_FLOOR_CASES_SHOWN`` the rest are counted and
    ``show <id>`` prints one in full. ``limit`` is the Spec's floor limit when the floor
    is being built for a Spec on screen; ``FloorDraft`` does not carry it, so the caller
    passes it to get the exact Section 7.6 warning line.
    """
    counts = {"operator": 0, "file": 0, "model_draft": 0}
    for item in draft.cases:
        counts[item.origin] += 1
    pending = unreviewed(draft)
    total = len(draft.cases)
    tuning = (total + 1) // 2
    held_out = total // 2
    lines = [
        f"floor {_ascii(draft.dataset)}  {draft.kind.value}  cases {total} (operator "
        f"{counts['operator']}, file {counts['file']}, model_draft {counts['model_draft']})  "
        f"unreviewed {len(pending)}  split {tuning} / {held_out}"
    ]
    ordered = [*pending, *(item for item in draft.cases if not _pending(item))]
    for item in ordered[:_FLOOR_CASES_SHOWN]:
        lines.extend(_case_lines(item))
    if total > _FLOOR_CASES_SHOWN:
        lines.append(f"  ... {total - _FLOOR_CASES_SHOWN} more; show <id> for one case")
    if 0 < total < RECOMMENDED_CASES:
        lines.append(f"  {small_floor_warning(held_out, limit)}")
    if reasons:
        lines.extend(f"blocked: {_ascii(reason)}" for reason in reasons)
    else:
        lines.append("ready: type accept")
    lines.append(_floor_next(draft, reasons, pending))
    return wrap("\n".join(lines)) + "\n"


def case_screen(item: DraftCase) -> str:
    """One draft case in full: id and flag, then the task and expected value pretty-printed."""
    corrected = item.case.metadata.get("corrected") is True
    lines = [f"{_ascii(item.case.case_id)}  {_case_flag(item.origin, item.reviewed, corrected)}"]
    for label, value in (("task", item.case.task), ("expected", item.case.expected)):
        lines.append(f"{label}:")
        pretty = json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False)
        # Escape row by row: the JSON's own line breaks are structure, not model bytes.
        lines.extend(_ascii(row) for row in pretty.split("\n"))
    return wrap("\n".join(lines)) + "\n"


def reviewed_line(case_id: str, action: str, case_after: DraftCase | None) -> str:
    """The one line ``review`` prints: what was decided about which case.

    ``case_after`` is the case as it is after the review, or None when it was dropped.
    """
    ident = _ascii(case_id)
    if action == "drop" or case_after is None:
        return f"dropped {ident}"
    if action == "expected":
        return f"reviewed {ident} expected -> {_ascii(_compact(case_after.case.expected))}"
    head = f"reviewed {ident} ok  task: "
    return head + _clip(_ascii(_compact(case_after.case.task)), WIDTH - len(head))


def review_screen(draft: FloorDraft, reasons: Sequence[str], line: str) -> str:
    """After ``review``: the decision line, the unreviewed count and the next step.

    The whole floor screen is not reprinted; ``show`` does that. ``ready: type accept``
    appears only when nothing blocks accept, so a floor that a drop made too small says
    what still blocks it instead.
    """
    pending = unreviewed(draft)
    lines = [line, f"unreviewed: {len(pending)}"]
    if pending:
        lines.append(f"next: review {_ascii(pending[0].case.case_id)} {_REVIEW_FORM}")
    elif reasons:
        lines.extend(f"blocked: {_ascii(reason)}" for reason in reasons)
    else:
        lines.append("ready: type accept")
    return wrap("\n".join(lines)) + "\n"


def floors_screen(summaries: Sequence[FloorSummary]) -> str:
    if not summaries:
        return "no floors configured\n"
    lines: list[str] = []
    for summary in summaries:
        label = _ascii(synthetic_label(summary))
        lines.append(
            f"{_ascii(summary.dataset)}  {summary.kind.value}  {summary.count} cases -> "
            f"{summary.tuning} tuning / {summary.held_out} held-out  {_ascii(str(summary.path))}"
        )
        if label:
            lines.append(f"  {label}")
    return wrap("\n".join(lines)) + "\n"


def status_screen(view: StatusView) -> str:
    """One line per StatusView field."""
    draft = view.draft
    outcome = view.outcome
    entry = view.last_entry
    rows = [
        ("state", view.state.value),
        ("workspace", _ascii(str(view.workspace))),
        ("profile_path", _ascii(str(view.profile_path)) if view.profile_path else "none"),
        ("model", _ascii(view.model) if view.model else "none"),
        ("inference_model", _ascii(view.inference_model) if view.inference_model else "none"),
        ("floors", ", ".join(_ascii(f.dataset) for f in view.floors) or "none"),
        (
            "compile_budget",
            f"{money(view.compile_budget.cost_usd)} / "
            f"{_budget_time(view.compile_budget.compute_s)}",
        ),
        (
            "draft",
            f"{draft.hash8}  origin {draft.origin}  edits {draft.edits}" if draft else "none",
        ),
        (
            "outcome",
            (
                f"{hash8(outcome.result.spec.spec_hash)}  licensed "
                f"{'yes' if outcome.licensed else 'no'}"
                if outcome
                else "none"
            ),
        ),
        ("last_entry", f"{_ascii(entry.name)} revision {entry.revision}" if entry else "none"),
        ("session_spend", _spend_text(view.session_spend)),
        ("compile_spend", _spend_text(view.compile_spend) if view.compile_spend else "-"),
        ("signing_key", _ascii(str(view.signing_key))),
    ]
    if view.bench is not None:
        rows.insert(1, ("bench", _ascii(view.bench)))
    if view.forge is not None:
        rows.append(("forge", _ascii(view.forge)))
    return wrap("\n".join(f"{name:<16} {value}" for name, value in rows)) + "\n"


def _spend_text(spend: Spend) -> str:
    return (
        f"{money(spend.cost_usd)}  calls {spend.calls}  {_thousands(spend.tokens_in)} in / "
        f"{_thousands(spend.tokens_out)} out  wall {_wall(spend.wall_s)}"
    )


def model_menu(rows: Sequence[ModelRow]) -> str:
    """The SETUP menu; the fallback note depends on whether a claude-code CLI is present."""
    has_claude = any(row.provider == "claude-code" for row in rows)
    model_width = max((len(row.model) for row in rows), default=0)
    provider_width = max((len(row.provider) for row in rows), default=0)
    lines = ["Choose a model for this session:"]
    for position, row in enumerate(rows):
        if row.can_infer:
            infer = "yes"
        elif has_claude:
            infer = f"no (Stage 0 will use {INFERENCE_FALLBACK})"
        else:
            infer = "no (describe unavailable; use spec <file>)"
        suffix = "   (default)" if position == 0 else ""
        lines.append(
            f"  {row.index}) {_ascii(row.model):<{model_width}}  "
            f"{_ascii(row.provider):<{provider_width}}  min reservation/call "
            f"{_usd(row.min_call_cost)}  can infer Specs: {infer}{suffix}"
        )
    lines.append("  q) quit")
    return wrap("\n".join(lines)) + "\n"


def profile_summary(profile: Profile, path: Path) -> str:
    """Four lines before ``confirm>``: what the profile will do, not its JSON.

    ``profile`` is the validated form of the exact payload that will be written, so
    the summary cannot drift from the file; the file itself is one ``profile`` away.
    """
    ladder = profile.compile.model_ladder
    model = ladder[0] if ladder else next(iter(profile.models), "none")
    config = profile.models.get(model)
    provider = config.provider if config is not None else "?"
    executable = Path(config.executable).name if config and config.executable else "-"
    inference = profile.inference.model if profile.inference is not None else "none"
    compile_cfg = profile.compile
    budget = compile_cfg.compile_budget
    rewriting = "on" if compile_cfg.enable_rewriting else "off"
    lines = [
        f"model {_ascii(model)} via {_ascii(provider)} ({_ascii(executable)})",
        f"inference model {_ascii(inference)}",
        f"compile budget {money(budget.cost_usd)} / {_budget_time(budget.compute_s)}  "
        f"candidates {compile_cfg.max_candidates}  rewriting {rewriting}",
        f"full profile will be written to {_ascii(str(path))}",
    ]
    return wrap("\n".join(lines)) + "\n"


def failure(code: str, hint: str = "") -> str:
    """The error line plus its explanation; a malformed code degrades, never leaks."""
    shown = code if is_code(code) else "unknown_failure"
    text = f"error: {shown}\n  {explain(shown)}"
    if hint:
        text += f"\n  next: {_ascii(hint)}"
    return wrap(text) + "\n"


def footer(session: Spend, compile: Spend | None) -> str:
    compile_text = money(compile.cost_usd) if compile is not None else "-"
    return (
        f"spend  session {money(session.cost_usd)} | this compile {compile_text} | "
        f"calls {session.calls} | wall {_wall(session.wall_s)} | API-equivalent, not a bill\n"
    )
