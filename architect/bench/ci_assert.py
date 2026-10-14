"""CI assertions over a smoke run's rows (contract Section 13; Phase 10 Section 14.1).

    uv run python -m architect.bench.ci_assert .architect/bench/<id>/rows.jsonl
        [--repeat .architect/bench/<id2>/rows.jsonl] [--simulate [TRIALS]]

Checks, on content-free rows only:

- every ``default`` cell is licensed;
- every ``noisy`` cell is unlicensed with ``primary_floor_failed``;
- ``image_inputs > 0`` on every vision and pipeline cell (a routed installed reuse
  makes no model call and a routed foreign probe measures another task: exempt);
- on ``digitizer-24`` (``default``) exactly one image per case run, first stage
  only: ``image_inputs`` equals the case runs the row accounts for
  (``max_candidates * tuning + held_out + tuning replay + delivery``) and the
  later stages sent none (``image_inputs < calls``);
- no ``image_reservation_under_configured``, ``sandbox_*``, ``schema_error`` or
  ``image_unresolved`` failure anywhere;
- ``compare`` of the ``default`` rows (base) against the ``noisy`` rows
  (candidate) is a ``regression`` with ``mcnemar_p < 0.05`` on
  ``exam-clean-40``.

Phase 10 (checked whenever ``routed`` rows are present; all fake and $0):

1. the routed licensed rate is at least the default licensed rate on paired cells
   (``compare --pair-variants default:routed``): routing may not reduce licensing;
2. the exact probe reuses (``reuse_installed``) in every cell whose Phase A
   installed an entry, the foreign probe compiles in every cell, and the sibling
   probe warm-starts whenever an eligible template with ``r >= 0.3`` was listed;
3. no routed row has ``route_did_not_run``, and every routed row carries its route
   columns and an evidence label;
4. ``--repeat``: a second identical run makes the identical sequence of
   (action, target, chosen rank) decisions, with the tier and candidate count too;
   the target is ``RouteFields.target``, the content-free category of the chosen
   entry or template;
5. ``--simulate``: the pure router and role-router simulation (2000 seeded trials
   per scenario by default) keeps every Section 14.1 bound.

These rows are ``synthetic_fake``: they prove plumbing and decision determinism,
not routing quality.

Exit 0 and ``ok`` lines when every check holds; exit 1 with one ``fail`` line per
broken check otherwise; exit 2 when the rows cannot be read.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, get_args

from architect.routing.labels import EvidenceClass

from .compare import compare
from .rows import BenchError, BenchRow, read_rows

if TYPE_CHECKING:
    from .router_sim import SimulationSuite

__all__ = [
    "check_rows",
    "determinism_failures",
    "main",
    "route_failures",
    "simulation_failures",
]

FORBIDDEN = frozenset({"image_reservation_under_configured", "schema_error", "image_unresolved"})
IMAGE_KINDS = frozenset({"vision", "pipeline"})
_CLASSES = frozenset(get_args(EvidenceClass))
MIN_TEMPLATE_RELIABILITY = 0.3  # ``RoutePolicy.min_template_reliability`` default


def _runs(row: BenchRow) -> int:
    """Case runs a text/vision/pipeline row accounts for (Stage 2, Stage 4, replays)."""
    return (
        row.max_candidates * row.tuning_cases
        + row.held_out_count
        + row.tuning_cases
        + row.delivery_cases
    )


def _forge_checks(rows: Sequence[BenchRow]) -> list[str]:
    """Phase 9 Stage A: forge and template cells really ran Stage 3 and license.

    Checked only when such rows are present, so a default/noisy-only run is unaffected.
    """
    failures: list[str] = []
    for row in rows:
        if row.variant not in ("forge", "template"):
            continue
        if not row.licensed:
            failures.append(f"{row.variant} cell unlicensed: {row.task_id} {row.model}")
        if 3 not in row.stages:
            failures.append(f"{row.variant} cell skipped Stage 3: {row.task_id} {row.model}")
        if row.forge is None or row.forge.submissions < 1:
            failures.append(f"{row.variant} cell submitted nothing: {row.task_id} {row.model}")
        if row.variant == "forge" and (row.forge is None or row.forge.measures < 1):
            failures.append(f"forge cell never measured: {row.task_id} {row.model}")
    arc = {
        row.variant: row.arc.distinct_states_mean
        for row in rows
        if row.task_id == "arc-local-3" and row.arc is not None
    }
    if "forge" in arc:
        base, forged = arc.get("default"), arc["forge"]
        if base is None or forged is None or forged <= base:
            failures.append(
                f"arc-local-3 forge distinct states {forged} not above default {base}"
            )
    return failures


def _image_exempt(row: BenchRow) -> bool:
    """Routed rows that need not send an image: an installed reuse (no compile, no
    model call) and the foreign probe (it measures another task, maybe a text one)."""
    route = row.route
    if route is None:
        return False
    return route.probe == "foreign" or (route.action == "reuse_installed" and row.calls == 0)


def _where(row: BenchRow) -> str:
    route = row.route
    probe = f"{route.probe} round {route.round}" if route is not None else "no route"
    return f"{row.task_id} {row.model} {probe}"


def route_failures(rows: Sequence[BenchRow]) -> list[str]:
    """Pass criteria 1 to 3 (Section 14.1) over the routed rows; empty without any."""
    routed = [row for row in rows if row.variant == "routed"]
    if not routed:
        return []
    failures: list[str] = []
    for row in routed:
        route = row.route
        if "route_did_not_run" in row.failures:
            failures.append(f"routed row did not route: {_where(row)}")
        if route is None:
            failures.append(f"routed row without route columns: {_where(row)}")
            continue
        if row.evidence not in _CLASSES:
            failures.append(f"routed row without an evidence label: {_where(row)}")
        if route.probe == "exact" and route.installed > 0 and route.action != "reuse_installed":
            failures.append(
                f"exact probe did not reuse its installed entry ({route.action}): {_where(row)}"
            )
        if route.probe == "foreign" and route.action != "compile":
            failures.append(f"foreign probe did not compile ({route.action}): {_where(row)}")
        if route.probe == "sibling" and route.templates_ready > 0 and route.action != "warm_start":
            failures.append(
                f"sibling probe did not warm-start from an eligible template "
                f"(r >= {MIN_TEMPLATE_RELIABILITY}) ({route.action}): {_where(row)}"
            )
    default = [row for row in rows if row.variant == "default"]
    if not default:
        failures.append("no default rows: the routed arm has nothing to pair with")
        return failures
    try:
        comparison = compare(default, routed, pair_variants=("default", "routed"))
    except BenchError as exc:
        failures.append(f"compare default vs routed failed: {exc.code}")
        return failures
    n = len(comparison.cells)
    base = sum(1 for cell in comparison.cells if cell.licensed_base)
    cand = sum(1 for cell in comparison.cells if cell.licensed_cand)
    if cand < base:
        failures.append(
            f"routing reduced licensing: routed {cand}/{n} licensed vs default {base}/{n}"
        )
    return failures


def _decisions(rows: Sequence[BenchRow]) -> list[tuple[object, ...]]:
    """The routed decision sequence: identity, then (action, target, rank, tier,
    candidates); ``target`` is the content-free category of the chosen reference."""
    out: list[tuple[object, ...]] = []
    for row in rows:
        if row.variant != "routed":
            continue
        route = row.route
        identity = (row.task_id, row.model, row.repeat)
        if route is None:
            out.append((*identity, None, None, None, None, None, None, None))
            continue
        out.append(
            (
                *identity,
                route.round,
                route.probe,
                route.action,
                route.target,
                route.chosen_rank,
                route.tier,
                route.candidates,
            )
        )
    return out


def determinism_failures(first: Sequence[BenchRow], second: Sequence[BenchRow]) -> list[str]:
    """Pass criterion 4: two identical runs make identical routed decisions, in order."""
    left, right = _decisions(first), _decisions(second)
    if not left:
        return ["no routed rows: determinism was not measured"]
    if len(left) != len(right):
        return [f"routed decisions differ in number: {len(left)} vs {len(right)}"]
    return [
        f"routed decision {index} differs between identical runs: {a} vs {b}"
        for index, (a, b) in enumerate(zip(left, right, strict=True))
        if a != b
    ]


def simulation_failures(suite: SimulationSuite) -> list[str]:
    """Pass criterion 5 over a ``router_sim.simulation_suite`` (Section 14.1)."""
    failures: list[str] = []
    runs = [
        suite.exploration,
        suite.cap,
        suite.filters,
        *(result for _, result in suite.promote),
    ]
    for result in (*runs, suite.roles_evidence, suite.roles_pinned):
        if len(result.steps) < suite.trials:
            failures.append(f"simulation ran {len(result.steps)} of {suite.trials} trials")
    for name, explore in (("exploration", suite.exploration), ("cap", suite.cap)):
        if explore.explore_every <= 0:
            failures.append(f"{name} scenario has exploration off")
            continue
        bound = 1 / explore.explore_every + 0.01
        if explore.exploration_share > bound:
            failures.append(
                f"exploration share {explore.exploration_share:.4f} above {bound:.4f}"
            )
    explore = suite.exploration
    if explore.explore_every > 0:
        # The share bound only tests the every-k-th rule when exploration was
        # available at every k-th decision; a starved scenario would pass any router.
        floor = 1 / explore.explore_every - 0.01
        if explore.exploration_share < floor:
            failures.append(
                f"exploration scenario starved: share {explore.exploration_share:.4f} "
                f"below {floor:.4f}, so the bound is not tested"
            )
    if not any(step.explore_spent_usd >= suite.cap.explore_usd_per_day for step in suite.cap.steps):
        failures.append("cap scenario never reached its daily cap, so the cap is not tested")
    for result in runs:
        if result.explored_over_cap:
            failures.append(f"daily exploration cap exceeded {result.explored_over_cap} times")
        if result.forbidden_chosen:
            failures.append(
                f"an evicted, unlicensed or consumed candidate was chosen "
                f"{result.forbidden_chosen} times"
            )
    rates = {round(p, 4): result.promote_rate for p, result in suite.promote}
    if set(rates) != {0.5, 0.8, 0.95}:
        failures.append("promote scenarios must cover p in {0.5, 0.8, 0.95}")
    for p, rate in sorted(rates.items()):
        if rate > 0.10:
            failures.append(f"false promote-proposal rate {rate:.4f} above 0.10 at p={p}")
    roles = suite.roles_evidence
    if not roles.worse:
        failures.append("role scenario has no variant 0.10 below the default")
    elif roles.share(roles.worse) > 0.05:
        failures.append(
            f"a role variant 0.10 worse was chosen in {roles.share(roles.worse):.4f} of trials"
        )
    pinned = suite.roles_pinned
    if pinned.default is None or pinned.share([pinned.default]) != 1.0:
        failures.append("pinned role policy did not always choose the default")
    return failures


def check_rows(rows: Sequence[BenchRow]) -> list[str]:
    """Every broken check as one content-free line; empty when all hold."""
    failures: list[str] = []
    if not rows:
        return ["no rows"]
    default = [row for row in rows if row.variant == "default"]
    noisy = [row for row in rows if row.variant == "noisy"]
    for row in default:
        if not row.licensed:
            failures.append(f"default cell unlicensed: {row.task_id} {row.model}")
    for row in noisy:
        if row.licensed or "primary_floor_failed" not in row.failures:
            failures.append(f"noisy cell not failed on its floor: {row.task_id} {row.model}")
    for row in rows:
        if row.kind in IMAGE_KINDS and row.image_inputs <= 0 and not _image_exempt(row):
            failures.append(f"no images sent: {row.task_id} {row.variant}")
        bad = [
            code for code in row.failures if code in FORBIDDEN or code.startswith("sandbox_")
        ]
        if bad:
            failures.append(f"forbidden failure {','.join(bad)}: {row.task_id} {row.variant}")
        if row.task_id == "digitizer-24" and row.variant == "default":
            if row.image_inputs != _runs(row) or row.image_inputs >= row.calls:
                failures.append(
                    f"digitizer-24 attach policy: {row.image_inputs} images for {_runs(row)} "
                    f"runs and {row.calls} calls"
                )
    failures.extend(_forge_checks(rows))
    failures.extend(route_failures(rows))
    if not noisy:
        failures.append("no noisy rows: the negative control did not run")
    else:
        relabelled = [row.model_copy(update={"variant": "default"}) for row in noisy]
        try:
            comparison = compare(default, relabelled)
        except BenchError as exc:
            failures.append(f"compare default vs noisy failed: {exc.code}")
        else:
            cells = [cell for cell in comparison.cells if cell.task_id == "exam-clean-40"]
            if not cells:
                failures.append("compare default vs noisy has no exam-clean-40 cell")
            for cell in cells:
                if (
                    cell.verdict != "regression"
                    or cell.mcnemar_p is None
                    or cell.mcnemar_p >= 0.05
                ):
                    failures.append(
                        f"exam-clean-40 default vs noisy is {cell.verdict} "
                        f"(p={cell.mcnemar_p}), not a significant regression"
                    )
    return failures


USAGE = (
    "usage: python -m architect.bench.ci_assert <rows.jsonl> "
    "[--repeat <rows.jsonl>] [--simulate [TRIALS]]"
)


def _parse(args: list[str]) -> tuple[Path, Path | None, int | None] | None:
    """``ROWS [--repeat ROWS2] [--simulate [TRIALS]]``; None on anything else."""
    if not args or args[0].startswith("--"):
        return None
    rows, repeat, trials = Path(args[0]), None, None
    rest = args[1:]
    while rest:
        flag = rest.pop(0)
        if flag == "--repeat" and rest and not rest[0].startswith("--"):
            repeat = Path(rest.pop(0))
        elif flag == "--simulate":
            trials = 2000
            if rest and rest[0].isdigit():
                trials = int(rest.pop(0))
            if trials < 1:
                return None
        else:
            return None
    return rows, repeat, trials


def main(argv: Sequence[str] | None = None) -> int:
    parsed = _parse(list(sys.argv[1:] if argv is None else argv))
    if parsed is None:
        print(USAGE, file=sys.stderr)
        return 2
    path, repeat, trials = parsed
    try:
        rows = read_rows(path)
        second = read_rows(repeat) if repeat is not None else None
    except BenchError as exc:
        print(f"fail {exc.code}", file=sys.stderr)
        return 2
    failures = check_rows(rows)
    if second is not None:
        failures.extend(determinism_failures(rows, second))
    if trials is not None:
        from .router_sim import simulation_suite

        failures.extend(simulation_failures(simulation_suite(trials)))
    for line in failures:
        print(f"fail {line}")
    if failures:
        return 1
    tasks = sorted({row.task_id for row in rows})
    print(f"ok {len(rows)} rows, {len(tasks)} tasks: every Section 13 assertion holds")
    if any(row.variant == "routed" for row in rows):
        print("ok routed rows: pass criteria 1-3 hold (synthetic_fake: plumbing, not quality)")
    if second is not None:
        print("ok two identical runs made identical routed decisions (criterion 4)")
    if trials is not None:
        print(f"ok simulation over {trials} seeded trials per scenario (criterion 5)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
