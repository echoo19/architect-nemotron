"""Content-free bench rows and the identity columns that make them comparable.

A ``BenchRow`` is one (task, model, variant, repeat) cell: score, licence,
cost, calls, tokens, wall time, reservation bound and evidence label. Nothing
in a row is content: no prompt, output, case id, page text, media type or
hash of content ever enters it. ``model`` is a profile key. ``licensed`` is
``HarnessArtifact.licensed`` re-read from disk by the runner; this module
never constructs or edits a report.

The closed labels (``Tier``, ``Kind``, ``TaskId``, ``Variant``) live here so that
rows, statistics and the suite share one definition without importing the
generators. ``Provider`` and ``Evidence`` (= ``EvidenceClass``) are re-imported from
``architect.routing.labels`` (Phase 10 Section 14): the router owns them and the
bench stays a leaf. The bench ``FloorOrigin`` stays its own, smaller literal.

Phase 10 adds the ``routed`` variant: one row per routing probe (``exact``,
``sibling``, ``foreign``) with ``RouteFields``, plus ``artifact_digest``. Both are
optional, so rows written before Phase 10 stay readable.

Phase 14 adds the ``public`` tier and its ``receipts`` task, and appends defaulted
columns after ``route``: the arm settings the cell's Spec carried, the floor and
delivery case hashes, the signed report's held-out counts, the compile cost and
per-run delivery metrics (pass bits, invalid and over-budget counts, dollars,
calls, latency percentiles and each run's dollars). Rows written before Phase 14
load with every new column at its default. The review fix appends which delivery
runs were invalid and which went over budget, as bits like the pass bits, so a
subset of the delivery cases can be recounted; older rows hold only the totals.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from architect.models.images import SHA256_HEX
from architect.models.pricing import DEFAULT_PRICES
from architect.models.types import MODEL_ID_PATTERN
from architect.routing.labels import EvidenceClass as Evidence
from architect.routing.labels import Provider
from architect.spec import PresetName
from architect.telemetry.schema import (
    Category,
    NonnegativeFloat,
    NonnegativeInt,
    ParameterKey,
    ParameterValue,
    Score,
)

__all__ = [
    "ArcFields",
    "BenchError",
    "BenchRow",
    "CellSummary",
    "Code",
    "DeliverySplit",
    "Evidence",
    "FloorOrigin",
    "FloorSplit",
    "ForgeFields",
    "InvalidPolicy",
    "Kind",
    "ModelId",
    "Probe",
    "Provider",
    "ReplyJson",
    "RouteAction",
    "RouteFields",
    "RouteTier",
    "SuiteVersion",
    "Summary",
    "TaskId",
    "Tier",
    "Variant",
    "append_rows",
    "architect_commit",
    "decode_pass_bits",
    "encode_pass_bits",
    "preset_fingerprint",
    "prices_sha256",
    "read_rows",
    "summarize",
    "write_rows",
]

Tier = Literal["smoke", "quick", "full", "arc", "public"]
Kind = Literal["text", "vision", "pipeline", "game"]
TaskId = Literal[
    "sentiment-24",
    "tickets-40",
    "invoice-40",
    "exam-clean-40",
    "exam-scan-40",
    "digitizer-24",
    "digitizer-multipage-12",
    "arc-local-3",
    "arc-preview",
    "receipts",
]
Variant = Literal["default", "noisy", "images-all-stages", "forge", "template", "routed"]
# ``architect.routing.router.RouteAction`` (Section 8); a test keeps the two equal.
RouteAction = Literal["reuse_installed", "warm_start", "compile", "refuse"]
Probe = Literal["exact", "sibling", "foreign"]
RouteTier = Literal["A", "B", "C", "none"]
ModelId = Annotated[str, Field(min_length=1, max_length=128, pattern=MODEL_ID_PATTERN)]
Code = Annotated[str, Field(pattern=r"^[a-z_]{1,64}$")]
FloorOrigin = Literal["synthetic_generated", "example_fixture", "public_dataset"]
# Mirrors of ``architect.spec.ReplyJson`` and ``architect.spec.InvalidPolicy`` (Phase 14
# Section 3.1); a test keeps each equal to the Spec's own literal.
ReplyJson = Literal["strict", "first", "last", "schema"]
InvalidPolicy = Literal["fail_partition", "count_as_miss"]
# A public task's floor split and delivery (replay) split (Phase 14 Section 10.3).
FloorSplit = Literal["evidence", "rehearsal"]
DeliverySplit = Literal["delivery", "pilot"]
PassBits = Annotated[str, Field(pattern=r"^[0-9a-f]*$")]
# A key of ``suite.SUITES`` ("v1", "v2", ...); rows.py cannot import suite.py (cycle).
SuiteVersion = Annotated[str, Field(pattern=r"^v[0-9]{1,6}$")]


class BenchError(Exception):
    """A bench failure whose ``str()`` is a code from ``architect.bench.CODES``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class ArcFields(_Frozen):
    """Game-cell columns; never a submission or a benchmark result."""

    score: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] | None
    score_source: Code
    model_binding: Code
    games: NonnegativeInt
    actions: NonnegativeInt
    compliance: Literal["unverified"] = "unverified"
    # Phase 9 process metrics (host-side ``PlayCounters``), for every arm; None on
    # rows written before Phase 9 or when the backend reported no counters.
    distinct_states_mean: NonnegativeFloat | None = None
    revisit_rate_mean: Annotated[float, Field(ge=0, le=1)] | None = None
    levels_completed_total: NonnegativeInt | None = None
    calls_per_action: NonnegativeFloat | None = None


class ForgeFields(_Frozen):
    """Forge/template cell columns: counts, dollars, seconds and sizes only.

    ``agent_cost_usd`` is build-time agent cost and is never folded into ``cost_usd``.
    """

    mode: Literal["agent", "template"]
    agent_model: ModelId | None
    checks: NonnegativeInt
    dryruns: NonnegativeInt
    measures: NonnegativeInt
    submissions: NonnegativeInt
    accepted: NonnegativeInt
    spawns: NonnegativeInt
    agent_cost_usd: NonnegativeFloat
    agent_tokens_in: NonnegativeInt
    agent_tokens_out: NonnegativeInt
    agent_wall_s: NonnegativeFloat
    harness_nodes: NonnegativeInt
    harness_bytes: NonnegativeInt
    subharnesses: NonnegativeInt
    language: Literal[2]


class RouteFields(_Frozen):
    """One routing probe of a ``routed`` cell (Section 14); numbers and closed labels.

    ``chosen_rank`` is the router's rank of its choice (None for ``compile`` and
    ``refuse``); ``oracle_rank`` is the rank of the option the bench executed by
    realized held-out score, ties by cost, among every eligible option (the
    candidates plus the cold compile); ``regret`` is the best realized held-out
    score minus the executed one. ``compile_cost_avoided_usd`` is the cold compile
    cost of the same probe minus the routed arm's compile cost.

    ``codes``, ``installed``, ``templates_ready`` and ``target`` are bench additions
    (reported as a deviation from the contract's field list; all defaulted): the
    router's content-free codes (proposal counts for the learning curve), the Phase A
    entries installed in this cell, the eligible Tier B templates with
    ``r >= min_template_reliability`` the decision listed (pass criterion 2), and the
    decision's target as one ``h_`` category of its full reference (origin, registry
    name and revision or library key, and artifact digest; None without a target),
    so pass criterion 4 compares ``(action, target, chosen_rank)``.
    """

    probe: Probe
    action: RouteAction
    tier: RouteTier
    candidates: NonnegativeInt
    chosen_rank: NonnegativeInt | None
    oracle_rank: NonnegativeInt | None
    regret: float | None
    compile_cost_avoided_usd: float
    fallback: bool
    explored: bool
    decision_ms: NonnegativeFloat
    round: NonnegativeInt = 0
    codes: tuple[Code, ...] = ()
    installed: NonnegativeInt = 0
    templates_ready: NonnegativeInt = 0
    target: Category | None = None


class BenchRow(_Frozen):
    bench_id: str
    timestamp: datetime
    label: Annotated[str, Field(max_length=64, pattern=r"^[A-Za-z0-9._-]*$")]
    suite_version: SuiteVersion
    suite_digest: SHA256_HEX
    task_id: TaskId
    tier: Tier
    kind: Kind
    provider: Provider
    model: ModelId
    preset: PresetName
    variant: Variant
    params: dict[ParameterKey, ParameterValue]
    components: tuple[Category, ...]
    max_candidates: Annotated[int, Field(gt=0, strict=True)]
    seed: Annotated[int, Field(strict=True)]
    size: NonnegativeInt
    repeat: NonnegativeInt
    spec_hash: SHA256_HEX
    preset_fingerprint: SHA256_HEX
    prices_sha256: SHA256_HEX
    architect_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")] | None
    # vision/pipeline cells: sha256 of manifest.json (seed, style, pillow_version, platform)
    pages_manifest_sha256: SHA256_HEX | None
    licensed: bool
    held_out_score: Score | None
    held_out_count: NonnegativeInt
    floor_limit: Score
    tuning_score: Score | None
    tuning_pass_bits: Annotated[str, Field(pattern=r"^[0-9a-f]*$")] | None
    tuning_cases: NonnegativeInt
    field_accuracy: Score | None
    delivery_score: Score | None
    delivery_cases: NonnegativeInt
    stages: tuple[NonnegativeInt, ...]
    failures: tuple[Code, ...]
    calls: NonnegativeInt
    tokens_in: NonnegativeInt
    tokens_out: NonnegativeInt
    cached_tokens_in: NonnegativeInt
    cost_usd: NonnegativeFloat
    reserved_usd: NonnegativeFloat
    per_call_reservation_usd: NonnegativeFloat
    wall_s: NonnegativeFloat
    image_inputs: NonnegativeInt
    image_tokens_estimate: NonnegativeInt
    evidence: Evidence
    floor_origin: FloorOrigin
    arc: ArcFields | None
    # Forge/template cells only; build-time agent cost lives here, never in cost_usd.
    forge: ForgeFields | None = None
    # Phase 10: the delivered or reused artifact's digest (joins the router's bench
    # evidence), and the routing probe of a ``routed`` row.
    artifact_digest: SHA256_HEX | None = None
    route: RouteFields | None = None
    # Phase 14 (Section 3.6 and 11). The arm settings are read back from the Spec the
    # cell compiled; the counts come from the signed report; delivery metrics cover the
    # replayed cases in order and stay None when nothing was replayed.
    api_model: Annotated[str, Field(max_length=128)] | None = None
    plain: bool = False
    reply_json: ReplyJson = "strict"
    invalid_policy: InvalidPolicy = "fail_partition"
    floor_split: FloorSplit | None = None
    delivery_split: DeliverySplit | None = None
    floor_cases_sha256: SHA256_HEX | None = None
    delivery_cases_sha256: SHA256_HEX | None = None
    suite_frozen: bool | None = None
    tuning_replay: bool = True
    held_out_correct: NonnegativeInt | None = None
    held_out_invalid: NonnegativeInt | None = None
    held_out_over_budget: NonnegativeInt | None = None
    held_out_mean_cost_usd: NonnegativeFloat | None = None
    compile_cost_usd: NonnegativeFloat | None = None
    delivery_pass_bits: PassBits | None = None
    delivery_invalid: NonnegativeInt | None = None
    delivery_over_budget: NonnegativeInt | None = None
    delivery_cost_usd: NonnegativeFloat | None = None
    delivery_calls: NonnegativeInt | None = None
    delivery_zero_call_runs: NonnegativeInt | None = None
    delivery_p50_s: NonnegativeFloat | None = None
    delivery_p95_s: NonnegativeFloat | None = None
    delivery_run_costs_usd: tuple[NonnegativeFloat, ...] | None = None
    delivery_field_accuracy: Score | None = None
    # Review fix: which delivery runs were invalid and which went over budget, encoded
    # like the pass bits, so a subset of the cases can be recounted. None on rows
    # written before the fix, which hold only the two totals.
    delivery_invalid_bits: PassBits | None = None
    delivery_over_budget_bits: PassBits | None = None

    @field_validator("bench_id")
    @classmethod
    def canonical_uuid(cls, value: str) -> str:
        try:
            parsed = UUID(value)
        except ValueError as exc:
            raise ValueError("bench_id must be a UUID") from exc
        if str(parsed) != value.lower():
            raise ValueError("bench_id must have canonical UUID formatting")
        return str(parsed)

    @model_validator(mode="after")
    def outcome_bits_agree(self) -> Self:
        """Outcome bits, when present, match their totals, never overlap each other and
        never mark a passing run: an invalid or over-budget run settled nothing."""
        named = (
            (self.delivery_invalid_bits, self.delivery_invalid),
            (self.delivery_over_budget_bits, self.delivery_over_budget),
        )
        if all(bits is None for bits, _ in named):
            return self
        cases = self.delivery_cases
        try:
            passed = (
                decode_pass_bits(self.delivery_pass_bits, cases)
                if self.delivery_pass_bits is not None
                else [False] * cases
            )
            flags = [
                decode_pass_bits(bits, cases) if bits is not None else [False] * cases
                for bits, _ in named
            ]
        except ValueError:
            raise ValueError("Delivery outcome bits are shorter than the case count") from None
        for (bits, total), marked in zip(named, flags, strict=True):
            if bits is not None and total != sum(marked):
                raise ValueError("Delivery outcome bits disagree with their total")
        if any(a and b for a, b in zip(*flags, strict=True)) or any(
            p and (a or b) for p, a, b in zip(passed, *flags, strict=True)
        ):
            raise ValueError("A delivery run has more than one outcome")
        return self


class CellSummary(_Frozen):
    task_id: TaskId
    model: ModelId
    variant: Variant
    repeats: NonnegativeInt
    licensed_count: NonnegativeInt
    mean_held_out: Score | None
    min_held_out: Score | None
    max_held_out: Score | None
    mean_cost_usd: NonnegativeFloat
    mean_wall_s: NonnegativeFloat
    # Routed cells only (Section 14): None on every other variant.
    probe: Probe | None = None
    route_accuracy: Annotated[float, Field(ge=0, le=1)] | None = None
    mean_regret: float | None = None
    mean_cost_avoided_usd: float | None = None
    fallback_rate: Annotated[float, Field(ge=0, le=1)] | None = None


class Summary(_Frozen):
    per_cell: list[CellSummary]


def _mean(values: Sequence[float]) -> float | None:
    return float(sum(values) / len(values)) if values else None


def _route_summary(members: Sequence[BenchRow]) -> dict[str, float | None]:
    """Route accuracy (``chosen_rank == oracle_rank`` where both exist), mean regret,
    mean cost avoided and the fallback rate of one routed group."""
    routes = [row.route for row in members if row.route is not None]
    if not routes:
        return {}
    ranked = [r for r in routes if r.chosen_rank is not None and r.oracle_rank is not None]
    return {
        "route_accuracy": (
            sum(1 for r in ranked if r.chosen_rank == r.oracle_rank) / len(ranked)
            if ranked
            else None
        ),
        "mean_regret": _mean([r.regret for r in routes if r.regret is not None]),
        "mean_cost_avoided_usd": _mean([r.compile_cost_avoided_usd for r in routes]),
        "fallback_rate": sum(1 for r in routes if r.fallback) / len(routes),
    }


def summarize(rows: Sequence[BenchRow]) -> Summary:
    """Group repeats by (task_id, model, variant, probe) in first-seen order.

    ``probe`` is None for every non-routed row, so their grouping is unchanged.
    """
    groups: dict[tuple[str, str, str, str | None], list[BenchRow]] = {}
    for row in rows:
        probe = row.route.probe if row.route is not None else None
        groups.setdefault((row.task_id, row.model, row.variant, probe), []).append(row)
    cells: list[CellSummary] = []
    for (_task, model, _variant, _probe), members in groups.items():
        scores = [row.held_out_score for row in members if row.held_out_score is not None]
        first = members[0]
        route = _route_summary(members)
        cells.append(
            CellSummary(
                task_id=first.task_id,
                model=model,
                variant=first.variant,
                repeats=len(members),
                licensed_count=sum(1 for row in members if row.licensed),
                mean_held_out=float(sum(scores) / len(scores)) if scores else None,
                min_held_out=float(min(scores)) if scores else None,
                max_held_out=float(max(scores)) if scores else None,
                mean_cost_usd=float(sum(row.cost_usd for row in members) / len(members)),
                mean_wall_s=float(sum(row.wall_s for row in members) / len(members)),
                probe=first.route.probe if first.route is not None else None,
                route_accuracy=route.get("route_accuracy"),
                mean_regret=route.get("mean_regret"),
                mean_cost_avoided_usd=route.get("mean_cost_avoided_usd"),
                fallback_rate=route.get("fallback_rate"),
            )
        )
    return Summary(per_cell=cells)


def encode_pass_bits(bits: Sequence[bool]) -> str:
    """Hex, MSB first, zero-padded to whole bytes; ``""`` for no cases."""
    if not bits:
        return ""
    out = bytearray()
    for start in range(0, len(bits), 8):
        value = 0
        for offset, bit in enumerate(bits[start : start + 8]):
            if bit:
                value |= 0x80 >> offset
        out.append(value)
    return out.hex()


def decode_pass_bits(text: str, count: int) -> list[bool]:
    """Inverse of ``encode_pass_bits`` for the first ``count`` cases."""
    if count < 0 or len(text) * 4 < count:
        raise ValueError("Pass bits are shorter than the case count")
    data = bytes.fromhex(text) if text else b""
    return [bool(data[index // 8] & (0x80 >> (index % 8))) for index in range(count)]


def _line(row: BenchRow) -> str:
    return json.dumps(row.model_dump(mode="json"), sort_keys=True, allow_nan=False)


def write_rows(path: Path, rows: Sequence[BenchRow]) -> None:
    """Write JSONL, one row per line with sorted keys, replacing ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(_line(row) + "\n")


def append_rows(path: Path, rows: Sequence[BenchRow]) -> None:
    """Append rows so partial results survive a cancelled run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(_line(row) + "\n")


def read_rows(path: Path) -> list[BenchRow]:
    """Read JSONL rows; any unreadable or invalid line is ``bench_rows_unreadable``."""
    rows: list[BenchRow] = []
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    rows.append(BenchRow.model_validate_json(line))
    except (OSError, UnicodeError, ValueError, ValidationError) as exc:
        raise BenchError("bench_rows_unreadable") from exc
    return rows


def preset_fingerprint() -> str:
    """sha256 over the sorted ``architect/presets/*.py`` file names and bytes."""
    directory = Path(__file__).resolve().parents[1] / "presets"
    digest = hashlib.sha256()
    for file in sorted(directory.glob("*.py")):
        digest.update(file.name.encode("utf-8") + b"\0")
        digest.update(file.read_bytes() + b"\0")
    return digest.hexdigest()


def prices_sha256(path: Path | None) -> str:
    """sha256 of the price table bytes (the bundled table when ``path`` is None)."""
    return hashlib.sha256((path or DEFAULT_PRICES).read_bytes()).hexdigest()


def architect_commit() -> str | None:
    """Git HEAD of the checkout that holds this package, else None."""
    root = Path(__file__).resolve().parents[2]
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    head = completed.stdout.strip().lower()
    if completed.returncode != 0 or len(head) != 40:
        return None
    if any(char not in "0123456789abcdef" for char in head):
        return None
    return head
