"""The pre-registered analysis (Phase 14 Section 13.3), as a skeleton.

    uv run python scripts/analyze_prereg.py --config docs/bench/prereg-nebius.json --out DIR
    uv run python scripts/analyze_prereg.py --config ... --out DIR --check

The config (``PreregConfig``) is committed with the pre-registration. It names each
arm's rows (``--label`` runs of ``bench run``), the hypotheses, which of them Holm
corrects together, each arm's independent replicate, and the gate-audit window.

The skeleton computes, from delivery rows only:

- H1a and H1b (superiority): exact McNemar on the delivery pass bits, Holm-corrected
  across the hypotheses the config lists under ``holm``;
- H2 (non-inferiority): the lower bound of Newcombe's paired 95% interval of
  candidate minus base, against ``-margin``;
- H3 (cost ratio): the upper bound of the paired bootstrap 95% interval of the cost
  ratio, against ``max_ratio``;
- the same tests with each arm replaced by its replicate;
- the same tests on the delivery test subset (manifest sources ``test/...``) when the
  rows replayed the whole delivery split in manifest order;
- the gate-audit count from ``bench audit`` rows recorded within the window, with the
  count over every row as a labelled secondary number.

The gate audit is n of m. m counts submissions that met the bar in practice and whose
replay counts: a floor that matched its public dataset manifest (``floor_origin``
``public_dataset``), a provider that is not the fake one, and, in the window, the
pinned exam when the config pins one (the evidence floor's ``floor_cases_sha256`` and
the bar ``floor_limit``). n counts those that scored below the bar on the exam.
Exams refused for budget or invalid output and replays that errored are counted
apart; rows left out of m are counted by reason under ``excluded``. The audit rows
come from one ``bench audit`` replay with no ``--since`` or ``--until``, so the
script applies the window itself and the secondary count covers every build. When
the replay's ``replay.json`` shows a window, the secondary count is null, and a
replay window that misses part of the config's window is refused, as is a row that
repeats a submission.

A missing arm never stops the analysis (pre-registration sections 7.5, 11.3 and
14.3): a rows source that does not exist, no row with the arm's label, only crashed
rows (``cancelled`` or ``host_memory_low``) or a row that replayed no delivery case
leaves that arm out, and every hypothesis that needs it does not hold, with the arm
named under ``missing``. A Holm family keeps its pre-registered size: a hypothesis
that cannot be computed enters it with p = 1. ``missing_arms`` gives each missing
arm's reason, and ``analysis.md`` lists them.

H4 and the chosen outcome stay null here: the pre-registration fixes their rules in
the config before any main-run row exists. Rows whose ``suite_digest`` differs from
the config's are refused, as are unreadable rows and an analysis with no arm left.
``analysis.json`` (sorted keys, compact separators, ASCII, floats as Python
``repr``, one trailing newline) and ``analysis.md`` (the bench report renderer) hold
no timestamp, host name or absolute path, so two runs on the same inputs are
byte-identical. ``--check`` recomputes both and exits 1 when either file
differs from what is on disk.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator

from architect.bench import (
    ArmPair,
    BenchError,
    BenchRow,
    PairedComparison,
    holm,
    pair_arms,
    read_rows,
)
from architect.bench.audit import AuditReplay, AuditRow, practice_drop_exceeds
from architect.bench.render import report
from architect.bench.rows import decode_pass_bits, encode_pass_bits
from architect.bench.runner import NOT_DONE

REPOSITORY = Path(__file__).resolve().parents[1]
DELIVERY_MANIFEST = Path("examples") / "receipts" / "manifests" / "delivery.json"
ANALYSIS_VERSION = 1
# Why an arm is missing, as ``missing_arms`` records it and ``analysis.md`` says it.
MISSING = {
    "no_rows": "no rows file",
    "no_row": "no row with this label",
    "crashed": "only crashed cells",
    "no_delivery": "no delivery results",
}


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    test: Literal["superiority", "noninferiority", "cost_ratio"]
    base: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")]
    cand: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")]
    margin: Annotated[float, Field(ge=0, lt=1)] | None = None
    max_ratio: Annotated[float, Field(gt=0)] | None = None

    @model_validator(mode="after")
    def thresholds_match_the_test(self) -> Self:
        if (self.test == "noninferiority") != (self.margin is not None):
            raise ValueError("A margin belongs to a non-inferiority test, and it needs one")
        if (self.test == "cost_ratio") != (self.max_ratio is not None):
            raise ValueError("max_ratio belongs to a cost-ratio test, and it needs one")
        return self


class AuditWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rows: str  # an audit.jsonl file from a replay with no --since or --until
    since: datetime
    until: datetime
    # The pre-registered exam. When set, a replay in the window counts only on exactly
    # this floor (its cases_sha256, the evidence manifest's) at exactly this bar.
    floor_cases_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    floor_limit: Annotated[float, Field(ge=0, le=1)] | None = None


class PreregConfig(BaseModel):
    """``docs/bench/prereg-nebius.json``; a relative path is relative to the repository
    root, which is how a committed config names its rows."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    prereg_version: Literal[1]
    suite_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    task_id: str
    arms: dict[str, str]  # arm name (its run label) -> rows source
    hypotheses: dict[str, Hypothesis]
    holm: tuple[str, ...]  # the hypotheses corrected together
    replicate: dict[str, str] = Field(default_factory=dict)  # arm -> its replicate arm
    alpha: Annotated[float, Field(gt=0, lt=1)] = 0.05
    bootstrap: Annotated[int, Field(ge=1_000, le=100_000)] = 10_000
    seed: int = 0
    audit: AuditWindow | None = None

    @model_validator(mode="after")
    def names_resolve(self) -> Self:
        arms = set(self.arms)
        for hypothesis in self.hypotheses.values():
            if {hypothesis.base, hypothesis.cand} - arms:
                raise ValueError("A hypothesis names an arm the config does not list")
        if set(self.holm) - set(self.hypotheses):
            raise ValueError("holm names an unknown hypothesis")
        if any(self.hypotheses[name].test != "superiority" for name in self.holm):
            raise ValueError("Holm corrects superiority tests only")
        if set(self.replicate) - arms or set(self.replicate.values()) - arms:
            raise ValueError("A replicate names an arm the config does not list")
        return self


class AnalysisError(Exception):
    """A refusal; ``str()`` is a fixed code."""


# ------------------------------------------------------------------------ inputs


def _source(text: str) -> Path:
    """A rows or audit path: relative to the repository root, as committed configs name
    them. Sources never enter the output, so the outputs hold no path at all."""
    path = Path(text)
    return path if path.is_absolute() else REPOSITORY / path


def _rows(text: str) -> list[BenchRow] | None:
    """The rows of a source, or None when it does not exist (a missing cell)."""
    path = _source(text)
    if path.is_dir():
        path = path / "rows.jsonl"
    if not path.exists():
        return None
    try:
        return read_rows(path)
    except BenchError as exc:
        raise AnalysisError(exc.code) from None


def load_arms(config: PreregConfig) -> tuple[dict[str, list[BenchRow]], dict[str, str]]:
    """Each arm's delivery rows of the config's task, labelled with the arm's name, and
    the arms that are missing, each with its reason (a ``MISSING`` key).

    A crashed row (``cancelled`` or ``host_memory_low``, as the bench's resume drops
    them) never counts. An arm is missing when its source does not exist (``no_rows``),
    when no row of the task carries its label (``no_row``), when only crashed rows do
    (``crashed``), or when a row it keeps replayed no delivery case (``no_delivery``).
    A row whose ``suite_digest`` differs from the config's is refused
    (``bench_compare_mismatch``), as is an unreadable source.
    """
    arms: dict[str, list[BenchRow]] = {}
    missing: dict[str, str] = {}
    for name, source in sorted(config.arms.items()):
        found = _rows(source)
        if found is None:
            missing[name] = "no_rows"
            continue
        rows = [row for row in found if row.task_id == config.task_id]
        if any(row.suite_digest != config.suite_digest for row in rows):
            raise AnalysisError("bench_compare_mismatch")
        labelled = [row for row in rows if row.label == name]
        chosen = [row for row in labelled if not set(row.failures) & NOT_DONE]
        if not chosen:
            missing[name] = "crashed" if labelled else "no_row"
        elif any(row.delivery_pass_bits is None for row in chosen):
            missing[name] = "no_delivery"
        else:
            arms[name] = chosen
    return arms, missing


def _delivery_test_positions() -> tuple[str, int, list[int]]:
    """The delivery manifest's ``cases_sha256``, count and the ``test/`` positions."""
    try:
        manifest = json.loads((REPOSITORY / DELIVERY_MANIFEST).read_text(encoding="utf-8"))
        cases = manifest["cases"]
        positions = [
            index
            for index, case in enumerate(cases)
            if str(case.get("source", "")).startswith("test/")
        ]
        return str(manifest["cases_sha256"]), int(manifest["count"]), positions
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        raise AnalysisError("bench_dataset_missing") from None


# ------------------------------------------------------------------------- tests


def _clean(value: object) -> JsonValue:
    """JSON values with non-finite floats as strings (``Infinity``), keys as given."""
    if isinstance(value, float) and not math.isfinite(value):
        return "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, Mapping):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError("not a JSON value")


def _holds(hypothesis: Hypothesis, pair: ArmPair) -> bool:
    if hypothesis.test == "superiority":
        return pair.better
    if hypothesis.test == "noninferiority":
        assert hypothesis.margin is not None  # the config validator
        return pair.diff_ci[0] > -hypothesis.margin
    interval = pair.cost_ratio_ci
    assert hypothesis.max_ratio is not None  # the config validator
    return interval is not None and interval[1] <= hypothesis.max_ratio


def _margin(config: PreregConfig) -> float:
    """The margin shown next to every pair: the first non-inferiority margin, else 0.05."""
    margins = [h.margin for _, h in sorted(config.hypotheses.items()) if h.margin is not None]
    return margins[0] if margins else 0.05


def _paired(
    config: PreregConfig,
    rows: Sequence[BenchRow],
    names: Mapping[str, str],
    group: Sequence[str],
    size: int,
) -> list[ArmPair]:
    """The pairs of ``group``'s hypotheses, Holm-corrected as a family of ``size``.

    A family member that cannot be computed counts as p = 1, so the others are
    corrected exactly as the pre-registration sized the family (prereg 7.1 and 7.5).
    """
    hypotheses = [config.hypotheses[name] for name in group]
    try:
        paired: PairedComparison = pair_arms(
            rows,
            [(names[h.base], names[h.cand]) for h in hypotheses],
            alpha=config.alpha,
            margin=_margin(config),
            bootstrap=config.bootstrap,
            seed=config.seed,
        )
    except BenchError as exc:
        raise AnalysisError(exc.code) from None
    if len(paired.pairs) != len(group):
        raise AnalysisError("bench_compare_mismatch")  # one cell per hypothesis
    if size == len(group):
        return list(paired.pairs)
    adjusted = holm([pair.mcnemar_p for pair in paired.pairs] + [1.0] * (size - len(group)))
    return [
        pair.model_copy(
            update={
                "holm_p": value,
                "better": value < config.alpha and pair.n01 > pair.n10,
                "worse": value < config.alpha and pair.n10 > pair.n01,
            }
        )
        for pair, value in zip(paired.pairs, adjusted, strict=False)
    ]


def _run_tests(
    config: PreregConfig,
    rows: Sequence[BenchRow],
    names: Mapping[str, str],
    missing: Mapping[str, str],
) -> tuple[dict[str, JsonValue], list[ArmPair]]:
    """Every hypothesis once, with arm ``names[arm]`` standing in for each arm.

    The ``holm`` hypotheses share one Holm correction; every other hypothesis is
    alone in its own. Each needs exactly one paired cell (one task, one repeat). A
    hypothesis that needs a ``missing`` arm does not hold: its pair is null and
    ``missing`` names the arms it lacked (prereg 7.5 and 11.3).
    """
    results: dict[str, JsonValue] = {}
    pairs: list[ArmPair] = []
    groups: list[list[str]] = []
    family = [name for name in sorted(config.hypotheses) if name in config.holm]
    if family:
        groups.append(family)
    groups.extend([name] for name in sorted(config.hypotheses) if name not in config.holm)
    for group in groups:
        lacking = {
            name: sorted(
                {names[config.hypotheses[name].base], names[config.hypotheses[name].cand]}
                & set(missing)
            )
            for name in group
        }
        present = [name for name in group if not lacking[name]]
        computed = (
            dict(zip(present, _paired(config, rows, names, present, len(group)), strict=True))
            if present
            else {}
        )
        for name in group:
            hypothesis = config.hypotheses[name]
            pair = computed.get(name)
            if pair is not None:
                pairs.append(pair)
            results[name] = {
                "test": hypothesis.test,
                "base": hypothesis.base,
                "cand": hypothesis.cand,
                "margin": hypothesis.margin,
                "max_ratio": hypothesis.max_ratio,
                "holds": pair is not None and _holds(hypothesis, pair),
                "missing": list(lacking[name]),
                "pair": _clean(pair.model_dump(mode="python")) if pair is not None else None,
            }
    return results, pairs


def _subset_runs(
    bits: str | None, total: int | None, cases: int, positions: Sequence[int]
) -> tuple[str | None, int | None]:
    """Outcome bits and their count over ``positions``. A row that kept only its total
    gives 0 when the total is 0, and None (unknown) otherwise, never the whole set's."""
    if bits is None:
        return None, (0 if total == 0 else None)
    flags = decode_pass_bits(bits, cases)
    chosen = [flags[i] for i in positions]
    return encode_pass_bits(chosen), sum(chosen)


def _subset_row(row: BenchRow, positions: Sequence[int], marker: str) -> BenchRow:
    """``row`` restricted to the delivery cases at ``positions`` (same order).

    Pass bits, run dollars and the invalid and over-budget counts are recounted over
    those cases; the per-run figures the row cannot split (calls, latency, field
    accuracy) are left out, since only the pairs read the subset rows.
    """
    bits = row.delivery_pass_bits
    if bits is None:
        raise AnalysisError("bench_compare_mismatch")
    cases = row.delivery_cases
    every = decode_pass_bits(bits, cases)
    passed = [every[i] for i in positions]
    costs = row.delivery_run_costs_usd
    chosen = tuple(costs[i] for i in positions) if costs is not None else None
    invalid_bits, invalid = _subset_runs(
        row.delivery_invalid_bits, row.delivery_invalid, cases, positions
    )
    over_bits, over_budget = _subset_runs(
        row.delivery_over_budget_bits, row.delivery_over_budget, cases, positions
    )
    return row.model_copy(
        update={
            "delivery_pass_bits": encode_pass_bits(passed),
            "delivery_cases": len(positions),
            "delivery_cases_sha256": marker,
            "delivery_score": sum(passed) / len(positions),
            "delivery_run_costs_usd": chosen,
            "delivery_cost_usd": sum(chosen) if chosen is not None else None,
            "delivery_invalid": invalid,
            "delivery_over_budget": over_budget,
            "delivery_invalid_bits": invalid_bits,
            "delivery_over_budget_bits": over_bits,
            "delivery_calls": None,
            "delivery_zero_call_runs": None,
            "delivery_p50_s": None,
            "delivery_p95_s": None,
            "delivery_field_accuracy": None,
        }
    )


def _test_subset(
    config: PreregConfig,
    rows: Sequence[BenchRow],
    names: Mapping[str, str],
    missing: Mapping[str, str],
) -> dict[str, JsonValue] | None:
    """The tests on the delivery test subset, or None when the rows did not replay
    the whole delivery split in manifest order (positions cannot be joined)."""
    full, count, positions = _delivery_test_positions()
    if not positions or any(
        row.delivery_cases_sha256 != full or row.delivery_cases != count for row in rows
    ):
        return None
    marker = hashlib.sha256(f"{full}:test".encode()).hexdigest()
    subset = [_subset_row(row, positions, marker) for row in rows]
    return _run_tests(config, subset, names, missing)[0]


def _utc(stamp: datetime) -> datetime:
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


def _on_pinned_exam(row: AuditRow, window: AuditWindow) -> bool:
    """Whether ``row`` sat the exam the config pins (every pin that is set)."""
    if window.floor_cases_sha256 is not None and (
        row.floor_cases_sha256 != window.floor_cases_sha256
    ):
        return False
    return window.floor_limit is None or math.isclose(
        row.floor_limit, window.floor_limit, rel_tol=0.0, abs_tol=1e-9
    )


def _audit_counts(
    members: Sequence[AuditRow], pinned: AuditWindow | None
) -> dict[str, JsonValue]:
    """n of m over ``members`` (Section 10 of the pre-registration).

    Of the submissions that met the bar in practice, a row is left out of m, first
    reason wins, when its floor is not a matched public dataset, when it was replayed
    with the fake provider, or when it sat another exam than the pinned one. Of the
    rest, scored exams (passed or below the bar) make m and those below the bar make n;
    over-budget, invalid and errored exams are counted apart, as are the scored ones
    that fell more than 5 points below their practice score (an exact comparison,
    ``audit.practice_drop_exceeds``).
    """
    floors: dict[str, int] = {}
    evidence: dict[str, int] = {}
    other_exam = 0
    kept: list[AuditRow] = []
    for row in members:
        if not row.met_bar_in_practice:
            continue
        if row.floor_origin != "public_dataset":
            floors[row.floor_origin] = floors.get(row.floor_origin, 0) + 1
        elif row.evidence == "synthetic_fake":
            evidence[row.evidence] = evidence.get(row.evidence, 0) + 1
        elif pinned is not None and not _on_pinned_exam(row, pinned):
            other_exam += 1
        else:
            kept.append(row)
    scored = [row for row in kept if row.exam in ("passed", "below_bar")]
    excluded: dict[str, JsonValue] = {
        "floor_origin": dict(floors),
        "evidence": dict(evidence),
        "other_exam": other_exam,
    }
    return {
        "replayed": len(members),
        "met_bar_in_practice": len(scored),
        "failed_exam": sum(1 for row in scored if row.exam == "below_bar"),
        "over_budget": sum(1 for row in kept if row.exam == "over_budget"),
        "invalid_exceeded": sum(1 for row in kept if row.exam == "invalid_exceeded"),
        "not_countable": sum(1 for row in kept if row.exam == "error"),
        # More than 5 points, decided in exact fractions of the counted cases.
        "practice_drop_over_0_05": sum(1 for row in scored if practice_drop_exceeds(row)),
        "excluded": excluded,
    }


def _replay_window(rows_path: Path) -> AuditReplay | None:
    """The ``replay.json`` that ``bench audit`` writes beside its rows, if present."""
    path = rows_path.parent / "replay.json"
    if not path.exists():
        return None
    try:
        return AuditReplay.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError):
        raise AnalysisError("bench_audit_unreadable") from None


def _gate_audit(config: PreregConfig) -> dict[str, JsonValue] | None:
    """Section 13.2 counts over audit rows recorded within the window, and over all."""
    window = config.audit
    if window is None:
        return None
    path = _source(window.rows)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        rows = [AuditRow.model_validate_json(line) for line in lines if line.strip()]
    except (OSError, UnicodeError, ValidationError):
        raise AnalysisError("bench_audit_unreadable") from None
    # One row per submission: two replays pasted together would count one exam twice.
    identities = {(row.spec_hash, row.session_id, row.submission) for row in rows}
    if len(identities) != len(rows):
        raise AnalysisError("bench_audit_unreadable")
    since, until = _utc(window.since), _utc(window.until)
    every_build = True
    replay = _replay_window(path)
    if replay is not None:
        if (replay.since is not None and _utc(replay.since) > since) or (
            replay.until is not None and _utc(replay.until) < until
        ):
            raise AnalysisError("bench_audit_unreadable")  # builds in the window are missing
        every_build = replay.since is None and replay.until is None
    inside = [row for row in rows if since <= _utc(row.recorded_at) <= until]
    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "floor_cases_sha256": window.floor_cases_sha256,
        "floor_limit": window.floor_limit,
        "window": _audit_counts(inside, window),
        # Every public-dataset build at its own bar, with no window; null when the
        # replay itself was windowed, since it cannot hold every build.
        "all_builds_secondary": _audit_counts(rows, None) if every_build else None,
    }


# ----------------------------------------------------------------------- output


def _missing_lines(missing: Mapping[str, str]) -> str:
    """The Markdown that names each missing arm; empty when none is missing."""
    if not missing:
        return ""
    lines = ["", "## Missing arms", ""]
    lines.extend(f"- {name}: {MISSING[reason]}" for name, reason in sorted(missing.items()))
    lines.extend(
        [
            "",
            "Every hypothesis that needs a missing arm does not hold "
            "(pre-registration sections 7.5 and 11.3).",
        ]
    )
    return "\n".join(lines) + "\n"


def analyze(config: PreregConfig, config_bytes: bytes) -> tuple[bytes, bytes]:
    """``analysis.json`` and ``analysis.md`` as bytes; no clock is read."""
    arms, missing = load_arms(config)
    rows = [row for name in sorted(arms) for row in arms[name]]
    if not rows:
        raise AnalysisError("bench_compare_mismatch")  # no arm is left to analyse
    identity = {name: name for name in config.arms}
    primary, primary_pairs = _run_tests(config, rows, identity, missing)
    replicates: dict[str, JsonValue] = {}
    if config.replicate:
        swapped = {name: config.replicate.get(name, name) for name in config.arms}
        replicates = _run_tests(config, rows, swapped, missing)[0]
    payload: dict[str, JsonValue] = {
        "analysis_version": ANALYSIS_VERSION,
        "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "suite_digest": config.suite_digest,
        "task_id": config.task_id,
        "alpha": config.alpha,
        "bootstrap": config.bootstrap,
        "seed": config.seed,
        "arms": {
            name: {
                "rows": len(members),
                "evidence": [str(item) for item in sorted({row.evidence for row in members})],
                "floor_origin": [
                    str(item) for item in sorted({row.floor_origin for row in members})
                ],
            }
            for name, members in sorted(arms.items())
        },
        "hypotheses": primary,
        "replicates": replicates,
        "missing_arms": {name: reason for name, reason in sorted(missing.items())},
        "test_subset": _test_subset(config, rows, identity, missing),
        "gate_audit": _gate_audit(config),
        "H4": None,
        "outcome": None,
    }
    text = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )
    # The report shows the primary pairs exactly as tested (each with its own Holm group).
    first = rows[0]
    shown = PairedComparison(
        suite_version=first.suite_version,
        suite_digest=first.suite_digest,
        alpha=config.alpha,
        margin=_margin(config),
        bootstrap=config.bootstrap,
        seed=config.seed,
        pairs=primary_pairs,
        evidence=tuple(sorted({row.evidence for row in rows})),
        floor_origins=tuple(sorted({row.floor_origin for row in rows})),
    )
    try:
        markdown = report(rows, markdown=True, pairs=shown) + _missing_lines(missing)
    except BenchError as exc:
        raise AnalysisError(exc.code) from None
    return (text + "\n").encode("ascii"), markdown.encode("utf-8")


def _load_config(path: Path) -> tuple[PreregConfig, bytes]:
    try:
        data = path.read_bytes()
        return PreregConfig.model_validate_json(data), data
    except (OSError, ValidationError, ValueError):
        raise AnalysisError("invalid_arguments") from None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the pre-registered analysis on committed bench rows."
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        config, config_bytes = _load_config(args.config)
        analysis, markdown = analyze(config, config_bytes)
        files = {"analysis.json": analysis, "analysis.md": markdown}
    except AnalysisError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    if args.check:
        changed = []
        for name, data in files.items():
            try:
                same = (args.out / name).read_bytes() == data
            except OSError:
                same = False
            if not same:
                changed.append(name)
        if changed:
            print(json.dumps({"changed": changed}), file=sys.stderr)
            return 1
        print("analysis matches")
        return 0
    args.out.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (args.out / name).write_bytes(data)
    print(f"wrote {len(files)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
