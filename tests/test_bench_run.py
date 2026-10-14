"""The bench runner on the fake provider through the real Compiler (Section 8, WP4).

One module-scoped smoke run (``default`` and ``noisy``, seed 7) backs the row
checks; the other tests run small quick-tier cells. Everything is offline: the
fake provider answers through in-process oracles, local games through a public
frame player, and every path stays short for the Windows registry rule.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from architect.bench import BenchError, BenchRow, read_rows
from architect.bench.ci_assert import check_rows
from architect.bench.ci_assert import main as ci_main
from architect.bench.oracle import OracleAdapter, OracleState, oracle_factory
from architect.bench.providers import PublicFramePlayer
from architect.bench.runner import (
    BenchConfig,
    BenchProgress,
    Bound,
    CellBound,
    bound,
    check_max_usd,
    plan,
    run_bench,
)
from architect.bench.suite import SUITES
from architect.compiler import Compiler
from architect.floors import FloorEvaluator, HeldOutAccessError, HeldOutTasks
from architect.games import public
from architect.models import ModelAdapter, ModelRequest, ModelResponse
from architect.models.images import ImageInput
from architect.models.types import UsageQuote
from architect.presets.images import lift_images

TASKS = {task.id: task for task in SUITES["v1"]}
# The scan/dense renderer needs the optional bench extra; without it that one cell is
# recorded as ``extra_missing`` (never priced or guessed) and every other check holds.
HAS_PILLOW = importlib.util.find_spec("PIL") is not None
needs_pillow = pytest.mark.skipif(not HAS_PILLOW, reason="the bench extra (Pillow) is absent")


def ran(row: BenchRow) -> bool:
    return row.failures != ("extra_missing",)


@pytest.fixture(scope="module")
def smoke(tmp_path_factory: pytest.TempPathFactory) -> tuple[list[BenchRow], Path, Path]:
    root = tmp_path_factory.mktemp("smk")
    config = BenchConfig(
        tier="smoke",
        provider="fake",
        seed=7,
        variants=("default", "noisy"),
        out=root / "out",
    )
    workspaces = root / "ws"
    result = asyncio.run(run_bench(config, workspace_root=workspaces))
    return list(result.rows), result.rows_path, workspaces


def by(rows: list[BenchRow], task: str, variant: str = "default") -> BenchRow:
    matches = [row for row in rows if row.task_id == task and row.variant == variant]
    assert len(matches) == 1, (task, variant)
    return matches[0]


def test_smoke_rows_validate_and_round_trip(smoke: tuple[list[BenchRow], Path, Path]) -> None:
    rows, rows_path, _ = smoke
    assert read_rows(rows_path) == rows
    assert {row.task_id for row in rows if row.variant == "default"} == {
        "sentiment-24",
        "tickets-40",
        "invoice-40",
        "exam-clean-40",
        "exam-scan-40",
        "digitizer-24",
        "arc-local-3",
    }
    assert {row.task_id for row in rows if row.variant == "noisy"} == {
        "sentiment-24",
        "exam-clean-40",
    }
    assert len({row.bench_id for row in rows}) == 1
    for row in rows:
        assert row.evidence == "synthetic_fake" and row.provider == "fake"
        assert row.suite_version == "v1" and row.seed == 7
        assert row.cost_usd > 0 or row.kind == "game" or not ran(row)
        assert row.cost_usd <= row.reserved_usd or not ran(row)
    summary = json.loads((rows_path.parent / "summary.json").read_text(encoding="utf-8"))
    assert summary["cancelled"] is False and summary["evidence"] == ["synthetic_fake"]


@needs_pillow
def test_every_section_13_assertion_holds(
    smoke: tuple[list[BenchRow], Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    rows, rows_path, _ = smoke
    assert check_rows(rows) == []
    assert ci_main([str(rows_path)]) == 0
    assert capsys.readouterr().out.startswith("ok 9 rows")


def test_default_licensed_and_noisy_fails_its_floor(
    smoke: tuple[list[BenchRow], Path, Path],
) -> None:
    rows, _, _ = smoke
    for row in rows:
        if not ran(row):
            assert row.task_id == "exam-scan-40" and not HAS_PILLOW
            continue
        if row.variant == "default":
            assert row.licensed, row.task_id
            assert row.failures == ()
        else:
            assert not row.licensed and "primary_floor_failed" in row.failures
            assert row.held_out_score is not None and row.held_out_score < row.floor_limit
    clean = by(rows, "exam-clean-40")
    assert clean.stages == (1, 2, 4)
    assert clean.tuning_pass_bits == "fff0" and clean.tuning_cases == 12
    assert clean.field_accuracy == 1.0 and clean.floor_origin == "synthetic_generated"
    assert by(rows, "sentiment-24").floor_origin == "example_fixture"


def test_digitizer_attaches_one_image_per_run_first_stage_only(
    smoke: tuple[list[BenchRow], Path, Path],
) -> None:
    rows, _, _ = smoke
    row = by(rows, "digitizer-24")
    runs = row.max_candidates * row.tuning_cases + row.held_out_count + row.tuning_cases
    assert row.image_inputs == runs == 24
    assert row.calls == 3 * runs  # three stages; only the first carried the page
    for task in ("exam-clean-40", "exam-scan-40") if HAS_PILLOW else ("exam-clean-40",):
        vision = by(rows, task)
        assert vision.image_inputs == vision.calls > 0
        assert vision.image_tokens_estimate == 100 * vision.image_inputs  # fake: fixed rule
        assert vision.pages_manifest_sha256 is not None
    assert by(rows, "sentiment-24").image_inputs == 0
    assert by(rows, "sentiment-24").pages_manifest_sha256 is None


def test_arc_local_rows(smoke: tuple[list[BenchRow], Path, Path]) -> None:
    rows, _, _ = smoke
    row = by(rows, "arc-local-3")
    assert row.licensed and row.kind == "game" and row.held_out_score == 1.0
    assert row.held_out_count == 3 and row.tuning_pass_bits is None
    assert row.arc is not None
    assert row.arc.score == 1.0 and row.arc.score_source == "local"
    assert row.arc.model_binding == "offline" and row.arc.games == 3
    assert row.arc.compliance == "unverified" and row.arc.actions > 0
    # The fixture player is priced like the text oracles (synthetic, never billed).
    assert row.calls > 0 and 0 < row.cost_usd <= row.reserved_usd


def test_budgets_are_lifted_from_quotes(smoke: tuple[list[BenchRow], Path, Path]) -> None:
    rows, _, _ = smoke
    sentiment = by(rows, "sentiment-24")
    text_quote = OracleAdapter("fake", OracleState()).quote(
        TASKS["sentiment-24"].sizing_request(())
    )
    assert sentiment.per_call_reservation_usd == text_quote.cost_usd
    clean = by(rows, "exam-clean-40")
    bundle = TASKS["exam-clean-40"].bundle(7, 24)
    _, refs = lift_images(bundle.cases[0].task)
    images = [ref for ref in refs if isinstance(ref, ImageInput)]
    image_quote = OracleAdapter("fake", OracleState()).quote(
        TASKS["exam-clean-40"].sizing_request(images)
    )
    assert clean.per_call_reservation_usd == image_quote.cost_usd > text_quote.cost_usd
    # reserved = run cost x (2 candidates x 12 + 12 held-out + 12 replay) x 1.2 safety
    run_cost = image_quote.cost_usd * 4
    assert clean.reserved_usd == pytest.approx(run_cost * 48 * 1.2)
    config = BenchConfig(tier="smoke", provider="fake", variants=("default", "noisy"))
    limits = bound(config)
    assert limits.total_usd == pytest.approx(sum(row.reserved_usd for row in rows))


def test_workspaces_are_removed(smoke: tuple[list[BenchRow], Path, Path]) -> None:
    _, _, workspaces = smoke
    assert workspaces.is_dir() and list(workspaces.iterdir()) == []


class Counting:
    """Counts every adapter the factory hands out and every completed call."""

    def __init__(self) -> None:
        self.inner = oracle_factory(OracleState())
        self.calls = 0

    def __call__(self, model: str) -> ModelAdapter:
        adapter = self.inner(model)
        original = adapter.complete

        async def complete(request: ModelRequest) -> ModelResponse:
            self.calls += 1
            return await original(request)

        adapter.complete = complete  # type: ignore[method-assign]
        return adapter


async def test_max_usd_refuses_the_whole_run_before_spend(tmp_path: Path) -> None:
    factory = Counting()
    config = BenchConfig(
        tier="quick", provider="fake", size=4, max_usd=1e-9, out=tmp_path / "out"
    )
    with pytest.raises(BenchError) as caught:
        await run_bench(config, factory=factory)
    assert caught.value.code == "bench_over_max_usd"
    assert factory.calls == 0
    assert not (tmp_path / "out").exists()


async def test_declined_bound_spends_nothing(tmp_path: Path) -> None:
    factory = Counting()
    seen: list[Bound] = []

    async def decline(limits: Bound) -> bool:
        seen.append(limits)
        return False

    config = BenchConfig(tier="quick", provider="fake", size=4, out=tmp_path / "out")
    with pytest.raises(BenchError) as caught:
        await run_bench(config, factory=factory, on_bound=decline)
    assert caught.value.code == "bench_cancelled" and factory.calls == 0
    assert len(seen) == 1 and seen[0].total_usd > 0 and seen[0].calls > 0


async def test_model_without_image_price_skips_vision_cells(tmp_path: Path) -> None:
    config = BenchConfig(
        tier="quick",
        provider="fake",
        models=("fake", "llama3.2"),
        size=4,
        out=tmp_path / "out",
    )
    result = await run_bench(config, workspace_root=tmp_path / "ws")
    skipped = [row for row in result.rows if row.failures == ("image_not_configured",)]
    assert [(row.task_id, row.model) for row in skipped] == [("exam-clean-40", "llama3.2")]
    assert result.skipped == (("exam-clean-40", "llama3.2", "image_not_configured"),)
    assert skipped[0].calls == 0 and skipped[0].reserved_usd == 0
    local = [row for row in result.rows if row.model == "llama3.2" and row.kind == "text"]
    assert local and all(row.licensed and row.cost_usd == 0 for row in local)
    # A $0 model reserves $0: the internal nominal wallet is never shown as money.
    assert all(row.reserved_usd == 0 and row.per_call_reservation_usd == 0 for row in local)


async def test_a_zero_cost_model_is_never_capped_by_max_usd(tmp_path: Path) -> None:
    config = BenchConfig(
        tier="quick",
        provider="fake",
        models=("llama3.2",),
        presets=("extractor",),
        size=2,
        max_usd=1e-9,
        out=tmp_path / "out",
    )
    limits = bound(config)
    assert limits.total_usd == 0
    codes = {cell.task_id: cell.code for cell in limits.cells}
    assert codes == {
        "sentiment-24": None,
        "invoice-40": None,
        "exam-clean-40": "image_not_configured",
    }
    result = await run_bench(config, workspace_root=tmp_path / "ws")
    ran_rows = [row for row in result.rows if row.task_id != "exam-clean-40"]
    assert ran_rows and all(row.licensed and row.reserved_usd == 0 for row in ran_rows)
    assert all(row.calls > 0 and row.wall_s > 0 for row in ran_rows)


async def test_max_usd_refuses_when_no_cell_is_left_to_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every priced cell over the cap and the rest skipped for another code: refused."""
    from architect.bench import runner

    monkeypatch.setattr(runner, "_has_module", lambda name: name != "PIL")
    factory = Counting()
    config = BenchConfig(
        tier="smoke",
        provider="fake",
        presets=("extractor",),
        size=2,
        max_usd=1e-9,
        out=tmp_path / "out",
    )
    limits = bound(config, factory=factory)
    assert {cell.code for cell in limits.cells} == {"over_max_usd", "extra_missing"}
    with pytest.raises(BenchError) as caught:
        await run_bench(config, factory=factory)
    assert caught.value.code == "bench_over_max_usd"
    assert factory.calls == 0 and not (tmp_path / "out").exists()


def test_check_max_usd_rules() -> None:
    def cell(code: str | None, usd: float = 0.0) -> CellBound:
        return CellBound(
            task_id="sentiment-24", model="fake", variant="default", repeat=0, size=2,
            per_call_usd=0.0, reserved_usd=usd, calls=0 if code else 1, code=code,
        )

    def limits(*cells: CellBound) -> Bound:
        return Bound(
            cells=cells, total_usd=sum(c.reserved_usd for c in cells), calls=0, models=("fake",)
        )

    capped = BenchConfig(tier="quick", provider="fake", max_usd=1.0)
    check_max_usd(capped, limits(cell(None, 0.5), cell("over_max_usd")))  # something runs
    check_max_usd(capped, limits(cell("image_not_configured")))  # nothing capped
    check_max_usd(capped.model_copy(update={"max_usd": None}), limits(cell("over_max_usd")))
    for refused in (
        limits(cell(None, 1.5)),
        limits(cell("over_max_usd"), cell("image_not_configured")),
        limits(cell("over_max_usd")),
    ):
        with pytest.raises(BenchError, match="bench_over_max_usd"):
            check_max_usd(capped, refused)


def after_stage_4(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    """Make HeldOutTasks.score raise ``error`` once a compile (Stages 1-4) has returned."""
    compiled: list[int] = []
    original_compile = Compiler.compile
    original_score = HeldOutTasks.score

    async def compile_then_mark(self: Compiler, *args: object, **kwargs: object) -> object:
        compiled.clear()  # each cell's own Stage 4 runs unpatched
        result = await original_compile(self, *args, **kwargs)  # type: ignore[arg-type]
        compiled.append(1)
        return result

    async def score(self: HeldOutTasks, *args: object, **kwargs: object) -> float:
        if compiled:
            raise error
        return await original_score(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Compiler, "compile", compile_then_mark)
    monkeypatch.setattr(HeldOutTasks, "score", score)


async def test_a_held_out_guard_trip_stops_the_run_and_is_never_a_disk_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HeldOutAccessError is a PermissionError (an OSError): it must not be relabelled."""
    assert issubclass(HeldOutAccessError, OSError)
    after_stage_4(monkeypatch, HeldOutAccessError("Held-out cases are private to stage 4"))
    config = BenchConfig(
        tier="quick", provider="fake", size=4, presets=("extractor",), out=tmp_path / "out"
    )
    with pytest.raises(BenchError) as caught:
        await run_bench(config, workspace_root=tmp_path / "ws")
    assert caught.value.code == "bench_held_out_access"
    [directory] = list((tmp_path / "out").iterdir())
    assert read_rows(directory / "rows.jsonl") == []  # no row relabels the breach
    assert list((tmp_path / "ws").iterdir()) == []


async def test_a_real_disk_error_after_stage_4_keeps_the_stage_4_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    after_stage_4(monkeypatch, OSError("disk"))
    config = BenchConfig(
        tier="quick", provider="fake", size=4, presets=("extractor",), out=tmp_path / "out"
    )
    result = await run_bench(config, workspace_root=tmp_path / "ws")
    for row in result.rows:
        if row.task_id == "exam-clean-40" and not ran(row):
            continue
        assert row.failures == ("workspace_error",)
        assert row.licensed and row.stages == (1, 2, 4)  # Stage 4's verdict survives
        assert row.held_out_score is not None and row.held_out_count == 2


def preview_config(tmp_path: Path) -> BenchConfig:
    return BenchConfig(
        tier="arc", provider="fake", arc_cache=tmp_path / "cache", out=tmp_path / "out"
    )


@pytest.mark.parametrize(
    ("error", "failures"),
    [
        (RuntimeError("proposal_skipped_approval"), ("proposal_skipped_approval",)),
        (RuntimeError("harness_reused"), ("harness_reused",)),
        (RuntimeError("public_games_unavailable"), ("arc_unavailable",)),
        (ValueError("game_count"), ("arc_unavailable",)),
        (ImportError("arc_agi"), ("arc_unavailable",)),
        (RuntimeError("Some provider text with spaces"), ("compilation_failed",)),
    ],
)
async def test_arc_preview_failures_keep_their_own_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    failures: tuple[str, ...],
) -> None:
    """Authority refusals from manufacture() are not relabelled as arc_unavailable."""
    from architect.bench import runner

    async def manufacture(*args: object, **kwargs: object) -> dict[str, object]:
        raise error

    monkeypatch.setattr(runner, "_has_module", lambda name: True)
    monkeypatch.setattr(public, "manufacture", manufacture)
    if failures == ("arc_unavailable",):
        # The only cell was unavailable: the row is kept, the run is an exit-1 code.
        with pytest.raises(BenchError) as caught:
            await run_bench(preview_config(tmp_path), workspace_root=tmp_path / "ws")
        assert caught.value.code == "bench_arc_unavailable"
        [directory] = list((tmp_path / "out").iterdir())
        [row] = read_rows(directory / "rows.jsonl")
    else:
        result = await run_bench(preview_config(tmp_path), workspace_root=tmp_path / "ws")
        [row] = result.rows
    assert row.failures == failures and not row.licensed


async def test_arc_preview_held_out_breach_stops_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from architect.bench import runner

    async def manufacture(*args: object, **kwargs: object) -> dict[str, object]:
        raise HeldOutAccessError("Held-out cases are private to stage 4")

    monkeypatch.setattr(runner, "_has_module", lambda name: True)
    monkeypatch.setattr(public, "manufacture", manufacture)
    with pytest.raises(BenchError, match="bench_held_out_access"):
        await run_bench(preview_config(tmp_path), workspace_root=tmp_path / "ws")


async def test_runner_never_reads_held_out_outside_stage_4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held_out_reads: list[int] = []
    issued: list[int] = []
    original_property = FloorEvaluator.held_out
    original_issue = FloorEvaluator._verification_cases

    def guarded(self: FloorEvaluator) -> object:
        held_out_reads.append(1)
        return original_property.fget(self)  # type: ignore[attr-defined]

    def issue(self: FloorEvaluator, capability: object) -> object:
        issued.append(1)
        return original_issue(self, capability)

    monkeypatch.setattr(FloorEvaluator, "held_out", property(guarded))
    monkeypatch.setattr(FloorEvaluator, "_verification_cases", issue)
    config = BenchConfig(
        tier="quick", provider="fake", size=4, presets=("extractor",), out=tmp_path / "out"
    )
    result = await run_bench(config, workspace_root=tmp_path / "ws")
    assert all(row.licensed for row in result.rows)
    assert held_out_reads == []
    assert len(issued) == len(result.rows)  # exactly one Stage 4 verification per cell


async def test_cancel_keeps_partial_rows(tmp_path: Path) -> None:
    config = BenchConfig(tier="quick", provider="fake", size=4, out=tmp_path / "out")
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
    [directory] = list((tmp_path / "out").iterdir())
    rows = read_rows(directory / "rows.jsonl")
    assert [row.task_id for row in rows] == ["sentiment-24", "invoice-40"]
    assert rows[0].licensed and rows[0].failures == ()
    assert rows[1].failures[-1] == "cancelled" and not rows[1].licensed
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    assert summary["cancelled"] is True
    assert list((tmp_path / "ws").iterdir()) == []


async def test_signing_key_environment_is_restored(tmp_path: Path) -> None:
    before = os.environ.get("ARCHITECT_SIGNING_KEY_FILE")
    config = BenchConfig(
        tier="quick", provider="fake", size=2, presets=("extractor",), out=tmp_path / "o"
    )
    result = await run_bench(config, workspace_root=tmp_path / "ws")
    assert result.rows and os.environ.get("ARCHITECT_SIGNING_KEY_FILE") == before


async def test_delivery_replay_scores_a_disjoint_set(tmp_path: Path) -> None:
    config = BenchConfig(
        tier="quick", provider="fake", size=4, delivery_n=3, out=tmp_path / "out"
    )
    result = await run_bench(config, workspace_root=tmp_path / "ws")
    invoice = next(row for row in result.rows if row.task_id == "invoice-40")
    assert invoice.delivery_cases == 3 and invoice.delivery_score == 1.0
    sentiment = next(row for row in result.rows if row.task_id == "sentiment-24")
    assert sentiment.delivery_cases == 0 and sentiment.delivery_score is None


async def test_missing_pillow_is_a_cell_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from architect.bench import runner

    monkeypatch.setattr(runner, "_has_module", lambda name: name != "PIL")
    config = BenchConfig(
        tier="smoke", provider="fake", size=2, presets=("extractor",), out=tmp_path / "out"
    )
    result = await run_bench(config, workspace_root=tmp_path / "ws")
    scan = next(row for row in result.rows if row.task_id == "exam-scan-40")
    assert scan.failures == ("extra_missing",) and scan.calls == 0
    assert scan.pages_manifest_sha256 is None and not scan.licensed
    assert ("exam-scan-40", "fake", "extra_missing") in result.skipped
    assert all(row.licensed for row in result.rows if row is not scan)


def test_plan_orders_cells_and_keeps_noisy_fake_only() -> None:
    config = BenchConfig(tier="smoke", provider="fake", variants=("default", "noisy"))
    cells = plan(config, ("fake",))
    assert [(cell.task.id, cell.variant) for cell in cells][-2:] == [
        ("sentiment-24", "noisy"),
        ("exam-clean-40", "noisy"),
    ]
    assert all(cell.task.kind != "game" for cell in cells if cell.variant == "noisy")
    with pytest.raises(BenchError) as caught:
        plan(config.model_copy(update={"provider": "profile"}), ("fake",))
    assert caught.value.code == "bench_variant_unknown"
    with pytest.raises(BenchError) as caught:
        plan(config.model_copy(update={"suite": "v9"}), ("fake",))
    assert caught.value.code == "bench_suite_unknown"
    with pytest.raises(BenchError) as caught:
        plan(config, ())
    assert caught.value.code == "bench_no_models"
    full = BenchConfig(tier="full", provider="fake", variants=("images-all-stages",))
    assert {cell.task.id for cell in plan(full, ("fake",))} == {
        "digitizer-24",
        "digitizer-multipage-12",
    }


async def test_images_all_stages_resends_the_page(tmp_path: Path) -> None:
    config = BenchConfig(
        tier="smoke",
        provider="fake",
        variants=("images-all-stages",),
        size=4,
        out=tmp_path / "out",
    )
    result = await run_bench(config, workspace_root=tmp_path / "ws")
    [row] = result.rows
    assert row.task_id == "digitizer-24" and row.licensed
    assert row.image_inputs == row.calls  # every stage carried the page


def test_public_frame_player_solves_each_family() -> None:
    assert PublicFramePlayer.choose([[4, 5, 6], [1, 1, 1]]) == "ACTION1"
    assert PublicFramePlayer.choose([[3, 1, 1], [1, 1, 1]]) == "ACTION1"
    grid = [[0, 0, 0], [0, 2, 0], [0, 3, 0]]
    assert PublicFramePlayer.choose(grid) == "ACTION2"
    assert PublicFramePlayer._action("no game data") == "ACTION1"


def test_config_is_closed() -> None:
    with pytest.raises(ValueError):
        BenchConfig.model_validate({"tier": "smoke", "unknown": 1})
    with pytest.raises(ValueError):
        BenchConfig(tier="smoke", size=1)
    with pytest.raises(ValueError):
        BenchConfig(tier="smoke", label="has space")
    with pytest.raises(ValueError):
        BenchConfig(tier="smoke", variants=())


ProgressSink = Callable[[BenchProgress], None]


# ------------------------------------------------------- review regressions (WP4 fixes)


def test_a_licensed_row_with_a_cell_code_still_shows_it(
    smoke: tuple[list[BenchRow], Path, Path],
) -> None:
    """A cell cancelled (or losing its workspace) after Stage 4 keeps licensed=True; the
    table must still say so instead of printing it as a normal licensed row."""
    from architect.bench import CELL_CODES
    from architect.bench.render import TABLE_CELL_CODES, bench_table

    assert TABLE_CELL_CODES == CELL_CODES
    rows, _, _ = smoke
    licensed = by(rows, "sentiment-24")
    assert licensed.licensed
    for codes in (("cancelled",), ("workspace_error",), ("cancelled", "workspace_error")):
        row = licensed.model_copy(
            update={"failures": codes, "tuning_pass_bits": None, "tuning_score": None}
        )
        lines = bench_table([row]).splitlines()
        assert lines[2].startswith("sentiment-24") and " yes " in lines[2]
        assert lines[3] == f"  cell code: {', '.join(codes)}"
    # Compiler codes are not cell codes and add no line.
    plain = licensed.model_copy(update={"failures": ("primary_floor_failed",), "licensed": False})
    assert "cell code" not in bench_table([plain])


LONG_MODELS = (
    "claude-sonnet-4-20250514",
    "claude-sonnet-5",
    "claude-opus-5-5",
    "muse-glimmer:30b",
    "granite4.2:8b",
    "fake",
)


def test_long_model_ids_are_never_clipped_into_one_label(
    smoke: tuple[list[BenchRow], Path, Path],
) -> None:
    from architect.bench import compare
    from architect.bench.providers import ProbeRow
    from architect.bench.render import bench_table, compare_table, probe_table

    rows, _, _ = smoke
    base = by(rows, "sentiment-24")
    relabelled = [base.model_copy(update={"model": model}) for model in LONG_MODELS]

    def model_cells(table: str, start: int, width: int) -> list[str]:
        body = table.splitlines()[2 : 2 + len(LONG_MODELS)]
        return [line[start : start + width].strip() for line in body]

    for table, start, width in (
        (bench_table(relabelled), 23, 13),
        (compare_table(compare(relabelled, relabelled)), 23, 11),
    ):
        labels = model_cells(table, start, width)
        assert len(set(labels)) == len(LONG_MODELS), labels
        assert not any("~" in label for label in labels)
        legend = " ".join(line for line in table.splitlines() if line.startswith("model #"))
        for model in LONG_MODELS:
            if len(model) > width:
                assert model in legend, (model, legend)
                assert model not in labels
            else:
                assert model in labels and model not in legend
        assert all(len(line) <= 100 and line.isascii() for line in table.splitlines())
    probes = [
        ProbeRow(
            model=model,
            provider="ollama",
            image_configured=False,
            basis=None,
            text_reserved_tokens=1,
            text_measured_tokens=1,
            image_reserved_tokens=None,
            image_estimate_tokens=None,
            image_measured_tokens=None,
            reserved_usd=0.0,
            measured_usd=0.0,
            code=None,
        )
        for model in ("claude-sonnet-4-20250514", "claude-sonnet-4-5-20250929")
    ]
    table = probe_table(probes)
    first, second = (line[:18].strip() for line in table.splitlines()[2:4])
    assert first != second and "~" not in first + second
    assert "claude-sonnet-4-20250514" in table and "claude-sonnet-4-5-20250929" in table


async def test_filters_that_select_no_cell_are_refused_before_anything_is_written(
    tmp_path: Path,
) -> None:
    for update in ({"presets": ("coder",)}, {"variants": ("images-all-stages",)}):
        config = BenchConfig(tier="quick", provider="fake", out=tmp_path / "out").model_copy(
            update=update
        )
        with pytest.raises(BenchError) as caught:
            plan(config, ("fake",))
        assert caught.value.code == "bench_no_cells"
        with pytest.raises(BenchError) as caught:
            bound(config)
        assert caught.value.code == "bench_no_cells"
        with pytest.raises(BenchError) as caught:
            await run_bench(config, workspace_root=tmp_path / "ws")
        assert caught.value.code == "bench_no_cells"
    assert not (tmp_path / "out").exists()


def test_ci_baseline_compare_is_never_skipped() -> None:
    """Section 13: the identical-scores gate runs unconditionally in bench-smoke."""
    workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
    text = workflow.read_text(encoding="utf-8")
    job = text.split("  bench-smoke:", 1)[1].split("\n  arc-extra:", 1)[0]
    assert "skipping" not in job and "if [ -f" not in job and "smoke-baseline" in job
    step = job.split("- name: Scores identical to the committed baseline", 1)[1]
    step = step.split("\n      - name:", 1)[0]
    run = step.split("run: |", 1)[1]
    assert "bench compare examples/bench/suite-v1/smoke-baseline.jsonl" in run
    assert "--expect identical-scores" in run
    assert "if:" not in step and "continue-on-error" not in step
    assert "|| true" not in run and "else" not in run
    assert "python -m architect.bench.ci_assert" in job


def _ci_jobs() -> tuple[str, dict[str, str]]:
    """ci.yml's header and each job's text, by job name."""
    workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
    header, body = workflow.read_text(encoding="utf-8").split("\njobs:\n", 1)
    jobs: dict[str, str] = {}
    name = ""
    for line in body.splitlines():
        if line.startswith("  ") and not line.startswith("   ") and line.endswith(":"):
            name = line.strip().removesuffix(":")
        else:
            jobs[name] = jobs.get(name, "") + line + "\n"
    return header, jobs


def _step(job: str, name: str) -> str:
    return job.split(f"- name: {name}\n", 1)[1].split("\n      - ", 1)[0]


def _folded_run(step: str) -> str:
    return " ".join(line.strip() for line in step.split("run: >-\n", 1)[1].splitlines())


def test_a_run_by_hand_makes_the_linux_baseline_with_the_gates_own_command() -> None:
    """Page manifests record the platform, so a new baseline has to come from Linux. A run
    by hand starts only bench-baseline, which runs bench-smoke's exact command, asserts
    the rows and keeps rows.jsonl; bench-smoke's failure hint points there."""
    header, jobs = _ci_jobs()
    assert "workflow_dispatch" in header.split("\non:", 1)[1].split("\n", 1)[0]
    smoke, baseline = jobs["bench-smoke"], jobs["bench-baseline"]
    command = "Smoke tier, default, noisy, template, forge and routed"
    assert _folded_run(_step(baseline, command)) == _folded_run(_step(smoke, command))
    assert "--variant routed --out .architect/bench" in _folded_run(_step(baseline, command))
    assert "    if: github.event_name == 'workflow_dispatch'\n" in baseline
    assert "    runs-on: ubuntu-latest\n" in baseline
    assert "python -m architect.bench.ci_assert" in baseline
    assert "name: smoke-baseline" in baseline and "path: baseline/rows.jsonl" in baseline
    for other in ("test", "test-windows", "bench-smoke", "arc-extra"):
        assert "    if: github.event_name != 'workflow_dispatch'\n" in jobs[other]
    assert "id: baseline" in _step(smoke, "Scores identical to the committed baseline")
    hint = _step(smoke, "How to refresh the baseline (only when the compare above failed)")
    assert "if: failure() && steps.baseline.outcome == 'failure'" in hint
    assert "bench-baseline job" in hint and "smoke-baseline artifact" in hint
    assert "--allow-fingerprint-mismatch" in hint


class PricedFixture(public.FixtureAgent):
    """The fixture player at $0.0001 a call, quoted and measured alike."""

    PRICE = 0.0001

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=1, tokens_out=1, cost_usd=self.PRICE)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await super().complete(request)
        return response.model_copy(update={"cost_usd": self.PRICE})


async def test_arc_preview_reserves_every_wallet_manufacture_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """manufacture() opens three wallets, each capped at cost_usd (proposal compile,
    approved compile, play): the bound and --max-usd must cover all three."""
    from architect.bench import runner

    seen: list[public.PreviewOptions] = []

    async def manufacture(
        options: public.PreviewOptions, tmp: Path, *, agent: ModelAdapter
    ) -> dict[str, object]:
        seen.append(options)
        for _ in range(public.COMPUTE_CAP_WALLETS):  # spend every wallet to its cap
            spent = 0.0
            while spent + agent.quote(ModelRequest(prompt="")).cost_usd <= options.cost_usd + 1e-12:
                probe = ModelRequest(prompt="specification inference stage")
                spent += (await agent.complete(probe)).cost_usd
        return {
            "licensed": True,
            "score": 0.5,
            "score_source": "official_offline_preview",
            "model_binding": "offline",
        }

    monkeypatch.setattr(runner, "_has_module", lambda name: True)
    monkeypatch.setattr(public, "manufacture", manufacture)
    assert public.COMPUTE_CAP_WALLETS == 3
    agent = PricedFixture()
    config = preview_config(tmp_path)
    limits = bound(config, factory=lambda model: agent)
    [cell] = limits.cells
    calls = (config.arc_actions + 8) * config.arc_games
    assert cell.reserved_usd == pytest.approx(3 * PricedFixture.PRICE * calls)
    result = await run_bench(config, factory=lambda model: agent, workspace_root=tmp_path / "ws")
    [row] = result.rows
    [options] = seen
    assert options.cost_usd == pytest.approx(PricedFixture.PRICE * calls)  # one wallet's cap
    assert row.cost_usd > options.cost_usd  # more than one wallet really spent
    assert row.cost_usd <= row.reserved_usd + 1e-9  # measured never exceeds the bound
    assert row.reserved_usd == pytest.approx(cell.reserved_usd) == result.bound_usd
    # A cap that holds one wallet but not three is refused before any spend.
    fresh = PricedFixture()
    capped = config.model_copy(
        update={"max_usd": 2 * options.cost_usd, "out": tmp_path / "capped"}
    )
    with pytest.raises(BenchError, match="bench_over_max_usd"):
        await run_bench(capped, factory=lambda model: fresh, workspace_root=tmp_path / "ws")
    assert fresh.calls == 0 and len(seen) == 1


async def test_a_cancelled_cell_records_a_workspace_it_could_not_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cancel path removes the workspace before building its row, and says so on failure."""
    from architect.bench import runner

    original = runner._remove

    def locked(workspace: object) -> bool:
        original(workspace)  # type: ignore[arg-type]
        return False  # as if a file were still held open (Windows)

    monkeypatch.setattr(runner, "_remove", locked)
    config = BenchConfig(tier="quick", provider="fake", size=4, out=tmp_path / "out")
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
    [directory] = list((tmp_path / "out").iterdir())
    rows = read_rows(directory / "rows.jsonl")
    assert rows[0].failures == ("workspace_error",)  # the normal path, as before
    assert rows[1].failures[-2:] == ("cancelled", "workspace_error")
