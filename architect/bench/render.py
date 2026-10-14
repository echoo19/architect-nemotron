"""ASCII tables for bench rows, comparisons, probes and the suite listing.

Shared by the CLI and the session. Every line is ASCII, carries no ANSI, is at
most 100 columns and is deterministic for a given input. Numbers are printed
next to their evidence label (abbreviated in the column, spelled out in the
closing line) and scores next to their floor origin. Money is API-equivalent,
never a bill; image token numbers are estimates and say so.

Phase 9 adds a ``process`` block to ``bench_table`` whenever a row carries Forge
or ARC process columns: ``meas``, ``subs``, ``agent$``, ``dist`` and ``revis``
(each at most 7 characters, so the table stays within 100 columns). Forge
counts are tuning-side evidence, never a licence; ``agent$`` is build-time cost,
never part of ``cost $``.

Phase 10 adds a ``route`` block whenever a row is ``routed``: ``probe``, ``tier``,
``action``, ``rank/orc`` (the router's rank of its choice / the realized oracle
rank), ``regret`` and ``dCost$`` (the cold compile's cost minus the routed arm's).
Routing orders licensed versions; Stage 4 decides every licence, and the block
says so.

Phase 14 adds ``pairs_table`` (paired arms on delivery cases) and ``report``, the
``bench report`` renderer: one table per task in ASCII or Markdown, the paired
arms when asked, then either a committed footer or the honesty lines that apply
(rehearsal numbers, fake-provider rows, synthetic floors). Markdown tables are a
file format and keep one line per row; every terminal line stays within 100
columns. Nothing printed depends on a clock.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence

from .compare import ArmPair, Comparison, PairedComparison, arm_label, pct_text, wilson
from .providers import ProbeRow, evidence_for
from .rows import BenchError, BenchRow, Evidence, decode_pass_bits
from .suite import BenchTask, tiers_for

__all__ = [
    "EVIDENCE_SHORT",
    "TABLE_CELL_CODES",
    "WIDTH",
    "bench_table",
    "bound_line",
    "compare_table",
    "footer_lines",
    "list_table",
    "money",
    "pairs_table",
    "probe_table",
    "progress_line",
    "report",
    "wrap",
]

WIDTH = 100
EVIDENCE_SHORT: Mapping[str, str] = {
    "synthetic_fake": "fake",
    "live_api_equivalent": "api-eq",
    "live_billed": "billed",
    "local_zero_cost": "local",
}
_VARIANT_SHORT = {
    "default": "default",
    "noisy": "noisy",
    "images-all-stages": "all-img",
    "forge": "forge",
    "template": "tmpl",
    "routed": "routed",
}
# The bench's own cell codes (``architect.bench.CELL_CODES``; a test keeps the two
# equal). A row carrying one always gets a ``cell code:`` line, licensed or not:
# a licensed cell can still be cancelled or lose its workspace after Stage 4.
TABLE_CELL_CODES: frozenset[str] = frozenset(
    {
        "image_not_configured",
        "arc_unavailable",
        "over_max_usd",
        "extra_missing",
        "cancelled",
        "image_reservation_under_configured",
        "image_unresolved",
        "workspace_error",
        "forge_did_not_run",
        "host_memory_low",
        "route_did_not_run",
    }
)


def money(x: float) -> str:
    """Four decimals below one dollar, two above; never scientific notation."""
    return f"${x:.4f}" if abs(x) < 1 else f"${x:.2f}"


def _wall(seconds: float) -> str:
    """Five characters or fewer below ten hours: 9.8s, 12m02, 1h03m."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    whole = int(seconds)
    if whole < 3600:
        return f"{whole // 60}m{whole % 60:02d}"
    return f"{whole // 3600}h{(whole % 3600) // 60:02d}m"


def _ascii(text: str) -> str:
    return "".join(ch if " " <= ch <= "~" else "?" for ch in text)


def _clip(text: str, width: int) -> str:
    text = _ascii(text)
    return text if len(text) <= width else text[: max(0, width - 1)] + "~"


def _model_labels(
    models: Iterable[str], width: int, noun: str = "model"
) -> tuple[dict[str, str], list[str]]:
    """A column label per model, and legend lines for the ids that do not fit.

    A model id that fits ``width`` is printed as is. A longer one is never clipped
    (two clipped ids can read the same); it gets ``#<n>`` (a model id cannot start
    with ``#``) and a ``model #<n>: <id>`` legend line spells it out in full,
    wrapped at ``WIDTH``. ``noun`` names other columns labelled the same way (arms).
    """
    labels: dict[str, str] = {}
    legend: list[str] = []
    for model in sorted(set(models)):
        text = _ascii(model)
        if len(text) <= width:
            labels[model] = text
            continue
        alias = f"#{len(legend) + 1}"
        labels[model] = alias
        legend.append(f"{noun} {alias}: {text}")
    return labels, [part for line in legend for part in _wrap(line)]


def _row_line(cells: Sequence[tuple[str, int, str]]) -> str:
    parts = []
    for text, width, align in cells:
        clipped = _clip(text, width)
        parts.append(clipped.rjust(width) if align == ">" else clipped.ljust(width))
    return " ".join(parts).rstrip()[:WIDTH]


def _cells(texts: Sequence[str], columns: Sequence[tuple[str, int, str]]) -> str:
    return _row_line(
        [(text, width, align) for text, (_, width, align) in zip(texts, columns, strict=True)]
    )


def _wrap(line: str, width: int = WIDTH) -> list[str]:
    """Break a prose line at spaces so no line exceeds ``width``."""
    words = _ascii(line).split(" ")
    out: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}" if current else word
        if len(candidate) <= width:
            current = candidate
            continue
        if current:
            out.append(current)
        while len(word) > width:
            out.append(word[:width])
            word = word[width:]
        current = word
    if current:
        out.append(current)
    return out


def wrap(line: str) -> str:
    """A prose line as ASCII lines of at most ``WIDTH`` columns, newline-terminated."""
    return "\n".join(_wrap(line)) + "\n"


def _closing(evidence: Sequence[str], origins: Sequence[str] = ()) -> list[str]:
    """Floor origins (when given), then the fixed closing line, which is always last.

    Generated and fixture floors are synthetic by construction; a public dataset
    floor is checked against its pinned manifests, and the line says which is which.
    """
    labels = ", ".join(sorted(set(evidence))) or "none"
    lines: list[str] = []
    unique = sorted(set(origins))
    synthetic = [origin for origin in unique if origin != "public_dataset"]
    if synthetic and len(synthetic) == len(unique):
        lines.append("floors: " + ", ".join(unique) + " (synthetic by construction)")
    elif synthetic:
        lines.append(
            "floors: public_dataset (pinned public data); "
            + ", ".join(synthetic)
            + " (synthetic by construction)"
        )
    elif unique:
        lines.append("floors: public_dataset (pinned public data, checked against its manifests)")
    lines.append(f"money is API-equivalent, not a bill; scores are {labels}")
    return [part for line in lines for part in _wrap(line)]


_BENCH_COLUMNS: tuple[tuple[str, int, str], ...] = (
    ("task", 22, "<"),
    ("model", 13, "<"),
    ("var", 7, "<"),
    ("lic", 3, "<"),
    ("held-out (k/n)", 14, "<"),
    ("tuning", 6, "<"),
    ("cost $", 8, ">"),
    ("calls", 5, ">"),
    ("wall", 5, ">"),
    ("evidence", 8, "<"),
)


def _held_out(row: BenchRow) -> str:
    score, n = row.held_out_score, row.held_out_count
    if row.arc is not None and row.task_id == "arc-preview":
        return f"score {score:.2f}" if score is not None else "-"
    if score is None:
        return "-" if not n else f"-/{n}"
    k = round(score * n) if n else 0
    return f"{k}/{n} {score:.2f}" if n else f"{score:.2f}"


def _tuning(row: BenchRow) -> str:
    if row.tuning_score is None or not row.tuning_cases:
        return "-"
    return f"{round(row.tuning_score * row.tuning_cases)}/{row.tuning_cases}"


def bench_table(rows: Sequence[BenchRow]) -> str:
    """``task | model | var | lic | held-out (k/n) | tuning | cost $ | calls | wall | evidence``."""
    header = _row_line([(name, width, "<") for name, width, _ in _BENCH_COLUMNS])
    lines = [header, "-" * len(header)]
    labels, legend = _model_labels((row.model for row in rows), _BENCH_COLUMNS[1][1])
    for row in rows:
        codes = [code for code in row.failures if code in TABLE_CELL_CODES]
        lic = "yes" if row.licensed else "no"
        cells = (
            row.task_id,
            labels[row.model],
            _VARIANT_SHORT.get(row.variant, row.variant),
            lic,
            _held_out(row),
            _tuning(row),
            money(row.cost_usd),
            str(row.calls),
            _wall(row.wall_s),
            EVIDENCE_SHORT.get(row.evidence, row.evidence),
        )
        lines.append(_cells(cells, _BENCH_COLUMNS))
        if codes:
            text = f"cell code: {', '.join(dict.fromkeys(codes))}"
            lines.extend(f"  {part}" for part in _wrap(text, WIDTH - 2))
    lines.extend(legend)
    lines.extend(_process_block(rows, labels))
    lines.extend(_route_block(rows, labels))
    if any(row.variant == "noisy" for row in rows):
        lines.extend(
            _wrap(
                "noisy rows are the fake-only negative control: they must fail their floor "
                "(a mechanism check, not a weaker model)"
            )
        )
    lines.extend(
        _closing([row.evidence for row in rows], [row.floor_origin for row in rows])
    )
    return "\n".join(lines) + "\n"


_PROCESS_COLUMNS: tuple[tuple[str, int, str], ...] = (
    ("task", 22, "<"),
    ("model", 13, "<"),
    ("var", 7, "<"),
    ("meas", 7, ">"),
    ("subs", 7, ">"),
    ("agent$", 7, ">"),
    ("dist", 7, ">"),
    ("revis", 7, ">"),
)


def _has_process(row: BenchRow) -> bool:
    arc = row.arc
    return row.forge is not None or (
        arc is not None
        and (arc.distinct_states_mean is not None or arc.revisit_rate_mean is not None)
    )


def _agent_money(value: float) -> str:
    """At most 7 characters: $0.0000 below a dollar, $123.45 above, then whole dollars."""
    text = money(value)
    return text if len(text) <= 7 else f"${value:.0f}"[:7]


def _process_block(rows: Sequence[BenchRow], labels: Mapping[str, str]) -> list[str]:
    """``process`` columns for Forge/template rows and ARC rows with process metrics."""
    shown = [row for row in rows if _has_process(row)]
    if not shown:
        return []
    header = _row_line([(name, width, "<") for name, width, _ in _PROCESS_COLUMNS])
    lines = ["", "process (forge counts are tuning evidence, not a license)", header]
    lines.append("-" * len(header))
    for row in shown:
        forge, arc = row.forge, row.arc
        dist = arc.distinct_states_mean if arc is not None else None
        revis = arc.revisit_rate_mean if arc is not None else None
        texts = (
            row.task_id,
            labels[row.model],
            _VARIANT_SHORT.get(row.variant, row.variant),
            str(forge.measures) if forge is not None else "-",
            str(forge.submissions) if forge is not None else "-",
            _agent_money(forge.agent_cost_usd) if forge is not None else "-",
            f"{dist:.1f}"[:7] if dist is not None else "-",
            f"{revis:.2f}" if revis is not None else "-",
        )
        lines.append(_cells(texts, _PROCESS_COLUMNS))
    lines.extend(
        _wrap(
            "meas/subs: forge_measure and forge_submit calls; agent$: build-time agent cost "
            "(API-equivalent), not in cost $; dist: mean distinct states; revis: mean revisit rate"
        )
    )
    return lines


_ROUTE_COLUMNS: tuple[tuple[str, int, str], ...] = (
    ("task", 22, "<"),
    ("model", 13, "<"),
    ("probe", 7, "<"),
    ("tier", 4, "<"),
    ("action", 15, "<"),
    ("rank/orc", 8, "<"),
    ("regret", 6, ">"),
    ("dCost$", 9, ">"),
    ("rnd", 3, ">"),
)
_ACTION_SHORT = {
    "reuse_installed": "reuse",
    "warm_start": "warm start",
    "compile": "compile",
    "refuse": "refuse",
}


def _signed_money(value: float) -> str:
    """At most 9 characters: ``+$0.0014``, ``-$1.20``; float noise below 1e-9 is 0."""
    if abs(value) < 1e-9:
        return "$0.0000"
    sign = "+" if value > 0 else "-"
    return (sign + money(abs(value)))[:9]


def _rank(value: int | None) -> str:
    return "-" if value is None else str(value)


def _route_block(rows: Sequence[BenchRow], labels: Mapping[str, str]) -> list[str]:
    """``route`` columns for routed rows (Section 14); ranks and dollars only."""
    shown = [row for row in rows if row.variant == "routed"]
    if not shown:
        return []
    header = _row_line([(name, width, "<") for name, width, _ in _ROUTE_COLUMNS])
    lines = ["", "route (routing orders licensed versions; Stage 4 decides the license)", header]
    lines.append("-" * len(header))
    for row in shown:
        route = row.route
        if route is None:
            texts: tuple[str, ...] = (row.task_id, labels[row.model], "-", "-", "did not run")
            texts += ("-", "-", "-", "-")
        else:
            texts = (
                row.task_id,
                labels[row.model],
                route.probe,
                route.tier,
                _ACTION_SHORT.get(route.action, route.action) + (" (x)" if route.explored else ""),
                f"{_rank(route.chosen_rank)}/{_rank(route.oracle_rank)}",
                f"{route.regret:.2f}" if route.regret is not None else "-",
                _signed_money(route.compile_cost_avoided_usd),
                str(route.round),
            )
        lines.append(_cells(texts, _ROUTE_COLUMNS))
    lines.extend(
        _wrap(
            "rank/orc: the router's rank of its choice / its rank by realized held-out score "
            "(ties by cost); regret: best realized held-out score minus the chosen one; "
            "dCost$: cold compile cost minus the routed arm's compile cost; (x): explored"
        )
    )
    if any(row.evidence == "synthetic_fake" for row in shown):
        lines.extend(_wrap("routed synthetic_fake rows are plumbing, not quality"))
    return lines


def _wilson(pair: tuple[float, float]) -> str:
    return f"{round(pair[0] * 100)}-{round(pair[1] * 100)}"


def compare_table(comparison: Comparison) -> str:
    """``task | model | held-out base->cand [wilson] | b/c p | cost % | verdict``."""
    columns = (
        ("task", 22, "<"),
        ("model", 11, "<"),
        ("held-out base->cand [wilson]", 28, "<"),
        ("b/c p", 11, "<"),
        ("cost %", 8, ">"),
        ("verdict", 12, "<"),
    )
    header = _row_line([(name, width, "<") for name, width, _ in columns])
    lines = [header, "-" * len(header)]
    labels, legend = _model_labels((cell.model for cell in comparison.cells), columns[1][1])
    for cell in comparison.cells:
        n = cell.n_held_out
        scores = f"{cell.base_k}/{n}->{cell.cand_k}/{n}"
        bounds = f"[{_wilson(cell.wilson_base)}|{_wilson(cell.wilson_cand)}]"
        p = f"{cell.mcnemar_p:.3f}" if cell.mcnemar_p is not None else "-"
        texts = (
            cell.task_id,
            labels[cell.model],
            f"{scores} {bounds}",
            f"{cell.discordant_b}/{cell.discordant_c} {p}",
            pct_text(cell.cost_delta_pct),
            cell.verdict,
        )
        lines.append(_cells(texts, columns))
    lines.extend(legend)
    if comparison.pair_variants is not None:
        base, cand = comparison.pair_variants
        lines.extend(_wrap(f"paired variants: {base} (base) -> {cand} (candidate), same spec"))
    per_model = ", ".join(f"{model} {v}" for model, v in sorted(comparison.per_model.items()))
    for line in (
        f"overall: {comparison.verdict}; per model: {per_model or 'none'}",
        "b/c: discordant tuning pairs, paired on tuning cases (selection-biased); held-out is "
        "the licensing signal; fewer than 5 discordant pairs never decide",
    ):
        lines.extend(_wrap(line))
    lines.extend(_closing(list(comparison.evidence)))
    return "\n".join(lines) + "\n"


def _tokens(value: int | None) -> str:
    return "-" if value is None else str(value)


def probe_table(rows: Sequence[ProbeRow]) -> str:
    """One line per probed model; ``basis`` assumption ceilings are flagged with ``!``.

    Every row's numbers sit next to its evidence label (``ev``, abbreviated as in
    ``bench_table``), spelled out in full in the closing ``evidence:`` line.
    """
    columns = (
        ("model", 18, "<"),
        ("provider", 11, "<"),
        ("ev", 6, "<"),
        ("basis", 11, "<"),
        ("text r/m", 9, "<"),
        ("image r/e/m", 15, "<"),
        ("$ res/meas", 15, "<"),
        ("code", 8, "<"),
    )
    header = _row_line([(name, width, "<") for name, width, _ in columns])
    lines = [header, "-" * len(header)]
    labels, legend = _model_labels((row.model for row in rows), columns[0][1])
    evidence: list[str] = []
    for row in rows:
        basis = row.basis or "-"
        if basis == "assumption":
            basis = "assumption!"
        label = evidence_for(row.provider)
        if label is not None:
            evidence.append(label)
        image = (
            f"{_tokens(row.image_reserved_tokens)}/{_tokens(row.image_estimate_tokens)}/"
            f"{_tokens(row.image_measured_tokens)}"
        )
        measured = money(row.measured_usd) if row.measured_usd is not None else "-"
        code = row.code or "ok"
        texts = (
            labels[row.model],
            row.provider,
            EVIDENCE_SHORT.get(label, "-") if label is not None else "-",
            basis,
            f"{row.text_reserved_tokens}/{_tokens(row.text_measured_tokens)}",
            image,
            f"{money(row.reserved_usd)}/{measured}",
            code if len(code) <= columns[-1][1] else "below",
        )
        details = [
            (name, text)
            for (name, width, _), text in zip(columns[3:7], texts[3:7], strict=True)
            if len(_ascii(text)) > width
        ]
        lines.append(_cells(texts, columns))
        # A number wider than its column is never shown clipped: it is repeated in full.
        for name, text in details:
            lines.append(_clip(f"  {name}: {text}", WIDTH))
        if len(code) > columns[-1][1]:
            lines.append(_clip(f"  code: {code}", WIDTH))
    lines.extend(legend)
    for line in (
        "evidence: " + (", ".join(sorted(set(evidence))) or "none"),
        "tokens: reserved / estimate (display only) / measured; image measured is the image "
        "call's input minus the text call's input, an approximation",
        "assumption! ceilings stay until a measured row lowers them to max(measured) * 1.25; "
        "money is API-equivalent, not a bill; local models cost $0 and are measured in wall time",
    ):
        lines.extend(_wrap(line))
    if any(row.code == "provider_reservation_exceeded" for row in rows):
        lines.extend(
            _wrap(
                "provider_reservation_exceeded: the text call used more than it reserved (raise "
                "the model's input_overhead_tokens or output_capacity_tokens); no image call ran"
            )
        )
    return "\n".join(lines) + "\n"


_LIST_TIER_WIDTHS: Mapping[str, int] = {"smoke": 5, "quick": 5, "full": 5, "arc": 3}


def list_table(
    tasks: Sequence[BenchTask],
    *,
    version: str = "v1",
    digest: str = "",
    max_candidates: int = 2,
    bounds: Sequence[tuple[str, str, float, int, str]] = (),
    recorded: Sequence[tuple[str, int, str, str]] = (),
) -> str:
    """Suite tasks with sizes, floors and expected calls; bounds; recorded bench ids.

    ``bounds`` rows are ``(tier, model, bound_usd, calls, evidence)``; ``recorded``
    rows are ``(bench_id, rows, tiers, evidence)``.
    """
    lines = _wrap(f"suite {version}  digest {digest[:16] or '-'}  max_candidates {max_candidates}")
    suite_tiers = tiers_for(version)
    columns = (
        ("task", 22, "<"),
        ("kind", 8, "<"),
        ("floor", 5, ">"),
        ("checker", 21, "<"),
        *((tier, _LIST_TIER_WIDTHS.get(tier, max(5, len(tier))), ">") for tier in suite_tiers),
        ("calls@full" if "full" in suite_tiers else "calls", 10, ">"),
    )
    header = _row_line([(name, width, "<") for name, width, _ in columns])
    lines.extend([header, "-" * len(header)])
    for task in tasks:
        sizes = [str(task.sizes[tier]) if tier in task.sizes else "-" for tier in suite_tiers]
        largest = task.default_size()
        runs = max_candidates * ((largest + 1) // 2) + largest // 2 + (largest + 1) // 2
        calls = task.max_calls_per_run("default") * (runs if task.kind != "game" else largest)
        texts = (task.id, task.kind, f"{task.floor_limit:.2f}", task.checker, *sizes, str(calls))
        lines.append(_cells(texts, columns))
    if bounds:
        lines.append("")
        for tier, model, usd, calls, evidence in bounds:
            lines.extend(
                _wrap(
                    f"bound {tier} on {model}: <={money(usd)} API-equivalent, ~{calls} calls "
                    f"({EVIDENCE_SHORT.get(evidence, evidence)}), a price-table projection"
                )
            )
    lines.append("")
    if recorded:
        lines.append(_row_line([("bench id", 36, "<"), ("rows", 5, ">"), ("tiers", 20, "<"),
                                ("evidence", 30, "<")]))
        for bench_id, count, tiers, evidence in recorded:
            lines.append(
                _row_line([(bench_id, 36, "<"), (str(count), 5, ">"), (tiers, 20, "<"),
                           (evidence, 30, "<")])
            )
    else:
        lines.append("no recorded bench runs under the output directory")
    lines.append("money is API-equivalent, not a bill; bounds are price-table projections")
    return "\n".join(line[:WIDTH] for line in lines) + "\n"


def bound_line(tier: str, models: int, bound_usd: float, calls: int) -> str:
    """The fixed line printed before any spend (Section 10)."""
    return (
        f"bench {tier} on {models} models: <={money(bound_usd)} API-equivalent bound, "
        f"~{calls} calls, not a bill."
    )


def clock(elapsed_s: float) -> str:
    whole = max(0, int(elapsed_s))
    return f"[{whole // 60:02d}:{whole % 60:02d}]"


def progress_line(
    elapsed_s: float,
    done: int,
    total: int,
    task_id: str,
    model: str,
    stage: int | None,
    calls: int,
    cost_usd: float,
) -> str:
    """``[mm:ss] bench <done>/<total> <task> <model>: stage <n>, <calls> calls, $<cost>``."""
    shown = "-" if stage is None else str(stage)
    return _clip(
        f"{clock(elapsed_s)} bench {done}/{total} {task_id} {_clip(model, 40)}: stage {shown}, "
        f"{calls} calls, {money(cost_usd)}",
        WIDTH,
    )


def evidence_labels(rows: Sequence[BenchRow]) -> tuple[Evidence, ...]:
    return tuple(sorted({row.evidence for row in rows}))


# ------------------------------------------------------ paired arms and reports (Phase 14)


def _p(value: float) -> str:
    """A p-value to 4 significant digits."""
    return f"{value:#.4g}"


def _ratio_text(value: float | None) -> str:
    if value is None:
        return "-"
    return "inf" if math.isinf(value) else f"{value:.3f}"


def _interval(pair: tuple[float, float], *, signed: bool = False) -> str:
    form = "{:+.3f}" if signed else "{:.3f}"
    return f"[{form.format(pair[0])}, {form.format(pair[1])}]"


def _result(pair: ArmPair) -> str:
    if pair.better:
        return "candidate better"
    if pair.worse:
        return "candidate worse"
    return "no significant difference"


def _honesty(evidence: Iterable[str], origins: Iterable[str]) -> list[str]:
    """Section 18: fake-provider rows and synthetic floors say what they are."""
    lines: list[str] = []
    if "synthetic_fake" in set(evidence):
        lines.append("Fake provider rows: plumbing checks, not model quality.")
    if any(origin != "public_dataset" for origin in origins):
        lines.append("Synthetic floor: not evidence of quality.")
    return lines


def _count(value: int | None) -> str:
    """A run count, or ``-`` when the row cannot tell (a subset of an older row)."""
    return "-" if value is None else str(value)


def pairs_table(comparison: PairedComparison) -> str:
    """``compare --pair-arms`` as ASCII: one block per (pair, task, repeat) cell."""
    lines = _wrap(
        f"paired arms on delivery cases: suite {comparison.suite_version} "
        f"{comparison.suite_digest[:8]}, alpha {comparison.alpha}, margin {comparison.margin}"
    )
    for pair in comparison.pairs:
        n = pair.n
        cost = (
            f"cost ratio {_ratio_text(pair.cost_ratio)} "
            + (_interval(pair.cost_ratio_ci) if pair.cost_ratio_ci is not None else "")
            + (
                f"; $/run {pair.base_cost_per_run:.6f} -> {pair.cand_cost_per_run:.6f}"
                if pair.base_cost_per_run is not None and pair.cand_cost_per_run is not None
                else ""
            )
            if pair.cost_ratio is not None
            else "cost ratio -: a row has no per-run costs"
        )
        lines.append("")
        lines.extend(
            _wrap(f"{pair.base} -> {pair.cand}: {pair.task_id} repeat {pair.repeat}, n = {n}")
        )
        for detail in (
            f"base {pair.base_k}/{n} {_interval(pair.wilson_base)}, candidate "
            f"{pair.cand_k}/{n} {_interval(pair.wilson_cand)}; invalid "
            f"{_count(pair.base_invalid)}/{_count(pair.cand_invalid)}, over budget "
            f"{_count(pair.base_over_budget)}/{_count(pair.cand_over_budget)}",
            f"diff {pair.diff:+.3f} {_interval(pair.diff_ci, signed=True)}; McNemar p "
            f"{_p(pair.mcnemar_p)}, Holm p {_p(pair.holm_p)}",
            f"{_result(pair)}; non-inferior at {pair.margin}: "
            f"{'yes' if pair.noninferior else 'no'}",
            cost,
        ):
            lines.extend(f"  {part}" for part in _wrap(detail, WIDTH - 2))
    lines.append("")
    for line in (
        "McNemar is exact on the delivery pass bits, Holm-adjusted across every pair, task "
        "and repeat; the diff interval is Newcombe's paired score interval; the cost ratio "
        f"interval is a paired bootstrap ({comparison.bootstrap} resamples, seed "
        f"{comparison.seed}).",
        *_honesty(comparison.evidence, comparison.floor_origins),
    ):
        lines.extend(_wrap(line))
    lines.extend(_closing(list(comparison.evidence)))
    return "\n".join(lines) + "\n"


def footer_lines(rows: Sequence[BenchRow]) -> list[str]:
    """The default report footer (Section 13.1), each line only when it applies."""
    if not rows:
        return []
    frozen = all(row.suite_frozen is True for row in rows)
    first = rows[0]
    lines = [
        f"Suite {first.suite_version} {first.suite_digest[:8]}; "
        f"settings frozen: {'yes' if frozen else 'no'}.",
        f"Evidence: {', '.join(sorted({row.evidence for row in rows}))}. "
        f"Floor origin: {', '.join(sorted({row.floor_origin for row in rows}))}.",
    ]
    deliveries = sorted(
        {
            (row.delivery_cases_sha256, row.delivery_cases)
            for row in rows
            if row.delivery_cases_sha256 is not None
        }
    )
    lines.extend(f"Delivery cases {sha[:8]}, n = {n}." for sha, n in deliveries)
    if any(row.suite_frozen is False for row in rows):
        lines.append(
            "These rows were measured before the settings were frozen: "
            "they are rehearsal numbers."
        )
    lines.extend(_honesty((row.evidence for row in rows), (row.floor_origin for row in rows)))
    return lines


_REPORT_HEADER: tuple[str, ...] = (
    "Arm",
    "Model",
    "Delivery",
    "95% CI",
    "Invalid",
    "Over budget",
    "Exam",
    "Licensed",
    "$/run",
    "p50 s",
    "p95 s",
    "Calls/run",
    "Build $",
    "Tuning, exam, delivery",
)


def _delivery_k(row: BenchRow) -> int | None:
    n = row.delivery_cases
    if n < 1:
        return None
    if row.delivery_pass_bits is not None:
        try:
            return sum(decode_pass_bits(row.delivery_pass_bits, n))
        except ValueError:
            return None
    return round(row.delivery_score * n) if row.delivery_score is not None else None


def _score(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def _report_cells(row: BenchRow) -> tuple[str, ...]:
    """The 14 values of one report line (Section 13.1), ``-`` where nothing applies."""
    n = row.delivery_cases
    k = _delivery_k(row)
    per_run = n if n > 0 else None
    exam = (
        f"{row.held_out_correct}/{row.held_out_count}"
        if row.held_out_correct is not None
        else _score(row.held_out_score)
    )
    agent = row.forge.agent_cost_usd if row.forge is not None else 0.0
    return (
        arm_label(row),
        row.api_model or row.model,
        f"{k}/{n} ({k / n:.3f})" if k is not None else "-",
        _interval(wilson(k, n)) if k is not None else "-",
        str(row.delivery_invalid) if row.delivery_invalid is not None else "-",
        str(row.delivery_over_budget) if row.delivery_over_budget is not None else "-",
        exam,
        "yes" if row.licensed else "no",
        f"${row.delivery_cost_usd / per_run:.6f}"
        if row.delivery_cost_usd is not None and per_run
        else "-",
        f"{row.delivery_p50_s:.2f}" if row.delivery_p50_s is not None else "-",
        f"{row.delivery_p95_s:.2f}" if row.delivery_p95_s is not None else "-",
        (
            f"{row.delivery_calls / per_run:.2f}"
            if row.delivery_calls is not None and per_run
            else "-"
        ),
        f"${row.compile_cost_usd + agent:.4f}" if row.compile_cost_usd is not None else "-",
        f"{_score(row.tuning_score)} / {_score(row.held_out_score)} / "
        f"{_score(row.delivery_score)}",
    )


def _by_task(rows: Sequence[BenchRow]) -> list[tuple[str, list[BenchRow]]]:
    """Tasks in id order; each task's rows by arm, then repeat (then run, for ties)."""
    groups: dict[str, list[BenchRow]] = {}
    for row in rows:
        groups.setdefault(row.task_id, []).append(row)
    return [
        (
            task_id,
            sorted(
                groups[task_id],
                key=lambda row: (arm_label(row), row.repeat, row.bench_id, row.seed),
            ),
        )
        for task_id in sorted(groups)
    ]


def _md_cell(text: str) -> str:
    return text.replace("|", "\\|")


def _md_table(header: Sequence[str], body: Iterable[Sequence[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines.extend("| " + " | ".join(_md_cell(cell) for cell in cells) + " |" for cells in body)
    return lines


_ASCII_DELIVERY: tuple[tuple[str, int, str], ...] = (
    ("arm", 12, "<"),
    ("model", 20, "<"),
    ("delivery", 17, "<"),
    ("95% CI", 16, "<"),
    ("invalid", 7, ">"),
    ("over", 4, ">"),
    ("exam", 9, ">"),
    ("lic", 3, "<"),
)
_ASCII_COST: tuple[tuple[str, int, str], ...] = (
    ("arm", 12, "<"),
    ("$/run", 11, ">"),
    ("p50 s", 7, ">"),
    ("p95 s", 7, ">"),
    ("calls/run", 9, ">"),
    ("build $", 10, ">"),
    ("tuning / exam / delivery", 24, "<"),
)


def _ascii_task(task_id: str, rows: Sequence[BenchRow]) -> list[str]:
    arms, arm_legend = _model_labels((arm_label(row) for row in rows), 12, "arm")
    models, model_legend = _model_labels((row.api_model or row.model for row in rows), 20)
    lines = ["", f"task {task_id}"]
    blocks = ((_ASCII_DELIVERY, tuple(range(8))), (_ASCII_COST, (0, 8, 9, 10, 11, 12, 13)))
    for columns, picks in blocks:
        header = _row_line([(name, width, "<") for name, width, _ in columns])
        lines.extend([header, "-" * len(header)])
        for row in rows:
            cells = list(_report_cells(row))
            cells[0] = arms[cells[0]]
            cells[1] = models[cells[1]]
            lines.append(_cells([cells[index] for index in picks], columns))
    return [*lines, *arm_legend, *model_legend]


def _md_pairs(comparison: PairedComparison) -> list[str]:
    header = (
        "Base",
        "Candidate",
        "n",
        "Diff",
        "95% CI",
        "McNemar p",
        "Holm p",
        f"Non-inferior ({comparison.margin})",
        "Cost ratio",
        "95% CI",
    )
    groups: dict[tuple[str, int], list[ArmPair]] = {}
    for pair in comparison.pairs:
        groups.setdefault((pair.task_id, pair.repeat), []).append(pair)
    repeats = len({repeat for _, repeat in groups}) > 1
    lines: list[str] = []
    for (task_id, repeat), pairs in sorted(groups.items()):
        title = f"Paired arms: {task_id}" + (f", repeat {repeat}" if repeats else "")
        lines.extend(["", f"## {title}", ""])
        lines.extend(
            _md_table(
                header,
                (
                    (
                        pair.base,
                        pair.cand,
                        str(pair.n),
                        f"{pair.diff:+.3f}",
                        _interval(pair.diff_ci, signed=True),
                        _p(pair.mcnemar_p),
                        _p(pair.holm_p),
                        "yes" if pair.noninferior else "no",
                        _ratio_text(pair.cost_ratio),
                        "-"
                        if pair.cost_ratio_ci is None
                        else f"[{_ratio_text(pair.cost_ratio_ci[0])}, "
                        f"{_ratio_text(pair.cost_ratio_ci[1])}]",
                    )
                    for pair in pairs
                ),
            )
        )
    return lines


def report(
    rows: Sequence[BenchRow],
    *,
    markdown: bool = False,
    pairs: PairedComparison | None = None,
    footer: str | None = None,
) -> str:
    """``bench report`` (Section 13.1): byte-identical for identical input.

    Every row must come from one suite version and digest (``bench_compare_mismatch``).
    ``footer`` is printed verbatim in place of the default footer lines.
    """
    if not rows or len({(row.suite_version, row.suite_digest) for row in rows}) != 1:
        raise BenchError("bench_compare_mismatch")
    lines: list[str] = []
    if markdown:
        for task_id, members in _by_task(rows):
            lines.extend([f"## {task_id}", ""])
            lines.extend(_md_table(_REPORT_HEADER, (_report_cells(row) for row in members)))
            lines.append("")
        if pairs is not None:
            lines.extend(_md_pairs(pairs)[1:])
            lines.append("")
        if footer is not None:
            text = footer if footer.endswith("\n") else footer + "\n"
            return "\n".join(lines) + "\n" + text
        for line in footer_lines(rows):
            lines.extend([line, ""])
        return "\n".join(lines[:-1]) + "\n"
    first = rows[0]
    lines.extend(
        _wrap(f"bench report: suite {first.suite_version} {first.suite_digest[:8]}, "
              f"{len(rows)} rows")
    )
    for task_id, members in _by_task(rows):
        lines.extend(_ascii_task(task_id, members))
    if pairs is not None:
        lines.append("")
        lines.extend(pairs_table(pairs).rstrip("\n").splitlines())
    lines.append("")
    if footer is not None:
        text = footer if footer.endswith("\n") else footer + "\n"
        return "\n".join(lines) + "\n" + text
    for line in footer_lines(rows):
        lines.extend(_wrap(line))
    return "\n".join(lines) + "\n"
