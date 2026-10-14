"""Bench suite v2 runs, flags and per-run delivery metrics (Phase 14 Sections 10.5, 11).

Everything is offline: the fake provider answers through in-process oracles. One
module-scoped run of the Section 15.3 command (plus the noisy control) backs the row
checks; the other tests run small cells or only plan and bound.
"""

from __future__ import annotations

import json
import sys
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from bench_public_support import install_receipt_checker

from architect.bench import BenchError, BenchRow, public_tasks, read_rows
from architect.bench.audit import read_submissions
from architect.bench.compare import pair_arms
from architect.bench.oracle import OracleAdapter, OracleState
from architect.bench.rows import decode_pass_bits
from architect.bench.runner import (
    FORGE_AUDIT_MODULE,
    RUN_FILE,
    BenchConfig,
    bound,
    nearest_rank,
    plan,
    run_bench,
)
from architect.bench.suite import SUITES
from architect.bench.text_tasks import canonical_key
from architect.cli import main as cli_main
from architect.models import ModelAdapter, ModelRequest, ModelResponse
from architect.spec import HarnessArtifact, Spec, TestReport

ARMS_DECLARED = "reply_json" in Spec.model_fields  # WA's Spec fields (Section 3.1)
PLAIN_DECLARED = "plain" in Spec.model_fields
COUNTS_DECLARED = "held_out_counts" in TestReport.model_fields  # WA's counts (Section 3.2)
V1 = {task.id: task for task in SUITES["v1"]}


@pytest.fixture(scope="module")
def v2(tmp_path_factory: pytest.TempPathFactory) -> dict[str, BenchRow]:
    """``bench run --provider fake --suite v2 --tier public --floor-split rehearsal
    --delivery 20 --yes``, with the noisy negative control beside the default arm."""
    root = tmp_path_factory.mktemp("v2run")
    out = root / "out"
    with pytest.MonkeyPatch.context() as patch:
        install_receipt_checker(patch)
        code = cli_main(
            [
                "bench", "run", "--provider", "fake", "--suite", "v2", "--tier", "public",
                "--floor-split", "rehearsal", "--delivery", "20", "--yes",
                "--variant", "default", "--variant", "noisy", "--out", str(out), "--json",
            ]
        )
    assert code == 0
    [rows_path] = list(out.glob("*/rows.jsonl"))
    return {row.variant: row for row in read_rows(rows_path)}


def test_the_v2_rows_carry_the_public_columns(v2: dict[str, BenchRow]) -> None:
    public_tasks.clear_cache()
    data = public_tasks.load_public("receipts")
    size = len(data.splits["rehearsal"])
    assert set(v2) == {"default", "noisy"}
    for row in v2.values():
        assert (row.suite_version, row.task_id, row.tier) == ("v2", "receipts", "public")
        assert row.floor_origin == "public_dataset" and row.evidence == "synthetic_fake"
        assert row.suite_frozen is True and row.tuning_replay is True
        assert (row.floor_split, row.delivery_split) == ("rehearsal", "delivery")
        assert row.size == size and row.held_out_count == size // 2
        assert row.tuning_cases == (size + 1) // 2
        assert row.floor_cases_sha256 == data.manifests["rehearsal"].cases_sha256
        assert row.delivery_cases_sha256 == public_tasks.cases_sha256(
            data.splits["delivery"][:20]
        )
        assert row.api_model is None and row.plain is False
        assert row.reply_json == ("first" if ARMS_DECLARED else "strict")
        assert row.invalid_policy == ("count_as_miss" if ARMS_DECLARED else "fail_partition")
        assert row.compile_cost_usd is not None and 0 < row.compile_cost_usd < row.cost_usd
        assert row.held_out_mean_cost_usd is not None
        if COUNTS_DECLARED:
            assert row.held_out_correct is not None and row.held_out_invalid is not None
        else:  # WA's signed counts are not in this build
            assert row.held_out_correct is None and row.held_out_invalid is None


def test_every_delivery_field_is_filled(v2: dict[str, BenchRow]) -> None:
    for row in v2.values():
        assert row.delivery_cases == 20 and row.delivery_pass_bits is not None
        passed = decode_pass_bits(row.delivery_pass_bits, 20)
        assert row.delivery_score == sum(passed) / 20
        assert row.delivery_invalid == 0 and row.delivery_over_budget == 0
        costs = row.delivery_run_costs_usd
        assert costs is not None and len(costs) == 20 and all(cost > 0 for cost in costs)
        assert row.delivery_cost_usd == pytest.approx(sum(costs))
        assert row.delivery_calls is not None and row.delivery_calls >= 20
        assert row.delivery_zero_call_runs == 0
        assert row.delivery_p50_s is not None and row.delivery_p95_s is not None
        assert 0 <= row.delivery_p50_s <= row.delivery_p95_s
        assert row.delivery_field_accuracy is not None
        assert row.tuning_pass_bits is not None and row.field_accuracy is not None


def test_the_default_arm_licenses_and_noisy_lowers_delivery_accuracy(
    v2: dict[str, BenchRow],
) -> None:
    default, noisy = v2["default"], v2["noisy"]
    assert default.licensed and default.failures == ()
    assert default.delivery_score == 1.0 and default.delivery_field_accuracy == 1.0
    assert not noisy.licensed and "primary_floor_failed" in noisy.failures
    assert noisy.delivery_score is not None and noisy.delivery_score < 1.0
    assert noisy.delivery_field_accuracy is not None and noisy.delivery_field_accuracy < 1.0
    assert default.spec_hash == noisy.spec_hash  # one spec, two oracles


def test_two_v2_arms_pair_on_their_delivery_cases(v2: dict[str, BenchRow]) -> None:
    rows = list(v2.values())
    [pair] = pair_arms(rows, [("fake:default", "fake:noisy")], bootstrap=1000).pairs
    assert pair.n == 20 and pair.n01 == 0 and pair.n10 > 0
    assert pair.diff < 0 and pair.cost_ratio == pytest.approx(1.0)


# ----------------------------------------------------------------- v1 metrics


async def test_v1_delivery_metrics_and_no_tuning_replay(tmp_path: Path) -> None:
    config = BenchConfig(
        tier="quick", provider="fake", size=4, tasks=("invoice-40",), delivery_n=3,
        tuning_replay=False, out=tmp_path / "out",
    )
    with_replay = bound(config.model_copy(update={"tuning_replay": True}))
    assert bound(config).total_usd < with_replay.total_usd
    result = await run_bench(config, workspace_root=tmp_path / "ws")
    [row] = result.rows
    assert row.task_id == "invoice-40" and row.licensed
    assert row.tuning_replay is False and row.tuning_pass_bits is None
    assert row.tuning_score is None and row.field_accuracy is None and row.tuning_cases == 2
    assert row.delivery_cases == 3 and row.delivery_pass_bits == "e0"
    assert row.delivery_field_accuracy == 1.0 and row.delivery_invalid == 0
    assert row.delivery_split is None and row.floor_split is None and row.suite_frozen is None
    assert row.floor_origin == "synthetic_generated"
    cases = V1["invoice-40"].bundle(7, 4).cases
    assert row.floor_cases_sha256 == public_tasks.cases_sha256(cases)
    delivered = V1["invoice-40"].delivery(7, 3)
    assert row.delivery_cases_sha256 == public_tasks.cases_sha256(delivered)


class Scripted(OracleAdapter):
    """The oracle, except that chosen delivery inputs get invalid or over-budget replies."""

    def __init__(self, state: OracleState, invalid: set[str], over: set[str]) -> None:
        super().__init__("fake", state)
        self.invalid, self.over = invalid, over

    async def complete(self, request: ModelRequest) -> ModelResponse:
        parsed, _ = json.JSONDecoder().raw_decode(request.prompt)
        key = canonical_key(parsed.get("input")) if "operation" not in parsed else ""
        if key in self.invalid:
            self.calls += 1
            return ModelResponse(
                text="not json", tokens_in=1, tokens_out=1, cost_usd=0.0, model=self.model
            )
        if key in self.over:
            self.calls += 1
            return ModelResponse(
                text="{}", tokens_in=1, tokens_out=1, cost_usd=1.0, model=self.model
            )
        return await super().complete(request)


async def test_delivery_runs_are_classified_ok_invalid_or_over_budget(tmp_path: Path) -> None:
    task = V1["invoice-40"]
    bundle = task.bundle(7, 4)
    delivery, delivery_state = task.delivery_dataset(7, 6)
    state = OracleState()
    state.merge(bundle.state)
    state.merge(delivery_state)
    keys = [canonical_key(case.task) for case in delivery]
    adapter = Scripted(state, invalid=set(keys[:2]), over={keys[2]})

    def factory(model: str) -> ModelAdapter:
        return adapter

    config = BenchConfig(
        tier="quick", provider="fake", size=4, tasks=("invoice-40",), delivery_n=6,
        out=tmp_path / "out",
    )
    result = await run_bench(config, factory=factory, workspace_root=tmp_path / "ws")
    [row] = result.rows
    assert row.licensed  # the floor's inputs were answered as usual
    assert row.delivery_invalid == 2 and row.delivery_over_budget == 1
    assert row.delivery_pass_bits is not None
    assert decode_pass_bits(row.delivery_pass_bits, 6) == [False] * 3 + [True] * 3
    # Which runs ended which way (review fix), so a subset of the cases can be recounted.
    assert row.delivery_invalid_bits is not None and row.delivery_over_budget_bits is not None
    assert decode_pass_bits(row.delivery_invalid_bits, 6) == [True] * 2 + [False] * 4
    assert decode_pass_bits(row.delivery_over_budget_bits, 6) == [False] * 2 + [True] + [False] * 3
    assert row.delivery_score == 0.5
    costs = row.delivery_run_costs_usd
    assert costs is not None and costs[2] >= 1.0 and max(costs[3:]) < 0.01
    accuracy = row.delivery_field_accuracy
    assert accuracy is not None and 0.4 < accuracy < 0.6  # no fields from the 3 failed runs


def test_nearest_rank_percentiles() -> None:
    assert nearest_rank([5.0, 1.0, 4.0, 2.0, 3.0], 50) == 3.0
    assert nearest_rank([5.0, 1.0, 4.0, 2.0, 3.0], 95) == 5.0
    twenty = [float(value) for value in range(20, 0, -1)]
    assert nearest_rank(twenty, 50) == 10.0 and nearest_rank(twenty, 95) == 19.0
    hundred = [float(value) for value in range(1, 101)]
    assert nearest_rank(hundred, 95) == 95.0 and nearest_rank(hundred, 50) == 50.0
    assert nearest_rank([0.25], 50) == nearest_rank([0.25], 95) == 0.25
    with pytest.raises(ValueError):
        nearest_rank([], 50)


# ---------------------------------------------------------------- flags and refusals


@pytest.mark.parametrize(
    ("update", "code"),
    [
        ({"suite": "v2", "tier": "public", "tasks": ("sentiment-24",)}, "bench_task_unknown"),
        ({"tier": "quick", "tasks": ("tickets-40",)}, "bench_no_cells"),
        ({"tier": "quick", "plain": True, "variants": ("forge",)}, "bench_plain_variant"),
        ({"tier": "quick", "plain": True, "variants": ("default", "template")},
         "bench_plain_variant"),
        ({"tier": "quick", "floor_split": "rehearsal"}, "bench_split_unsupported"),
        ({"tier": "quick", "delivery_split": "pilot"}, "bench_split_unsupported"),
        ({"tier": "quick", "delivery_n": "all"}, "bench_delivery_all_unsupported"),
        ({"suite": "v2", "tier": "public", "delivery_n": 355}, "bench_delivery_too_large"),
        ({"suite": "v2", "tier": "public", "delivery_split": "pilot", "delivery_n": 151},
         "bench_delivery_too_large"),
        ({"suite": "v2", "tier": "public", "size": 10}, "bench_size_unsupported"),
        ({"suite": "v2", "tier": "smoke"}, "bench_tier_unknown"),
        ({"suite": "v1", "tier": "public"}, "bench_tier_unknown"),
        ({"suite": "v2", "tier": "public", "variants": ("routed",)}, "bench_no_cells"),
        ({"tier": "arc", "plain": True}, "bench_no_cells"),  # a game cannot be plain
    ],
)
def test_plan_refusals(update: dict[str, Any], code: str) -> None:
    config = BenchConfig.model_validate({"provider": "fake", **update})
    with pytest.raises(BenchError) as caught:
        plan(config, ("fake",))
    assert caught.value.code == code


def test_tasks_and_plain_select_cells() -> None:
    quick = BenchConfig(tier="quick", provider="fake", tasks=("invoice-40", "sentiment-24"))
    assert [cell.task.id for cell in plan(quick, ("fake",))] == ["sentiment-24", "invoice-40"]
    smoke = BenchConfig(tier="smoke", provider="fake", plain=True, variants=("default", "noisy"))
    cells = plan(smoke, ("fake",))
    assert {cell.task.id for cell in cells} == {
        "sentiment-24", "tickets-40", "invoice-40", "exam-clean-40", "exam-scan-40"
    }  # no pipeline and no game task can be plain
    public = BenchConfig(suite="v2", tier="public", provider="fake", delivery_n="all")
    public_tasks.clear_cache()
    data = public_tasks.load_public("receipts")
    [cell] = plan(public, ("fake",))
    assert cell.task.id == "receipts" and cell.size == len(data.splits["evidence"])
    rehearsal = public.model_copy(update={"floor_split": "rehearsal"})
    assert plan(rehearsal, ("fake",))[0].size == len(data.splits["rehearsal"])


def test_the_bound_counts_delivery_all_plain_and_the_replay() -> None:
    public = BenchConfig(suite="v2", tier="public", provider="fake", delivery_n="all")
    public_tasks.clear_cache()
    data = public_tasks.load_public("receipts")
    floor, delivered = len(data.splits["evidence"]), len(data.splits["delivery"])
    tuning, held_out = (floor + 1) // 2, floor // 2
    [cell] = bound(public).cells
    assert cell.calls == 4 * (2 * tuning + held_out + delivered + tuning)
    [cell] = bound(public.model_copy(update={"tuning_replay": False})).cells
    assert cell.calls == 4 * (2 * tuning + held_out + delivered)
    [plain] = bound(public.model_copy(update={"plain": True})).cells
    if PLAIN_DECLARED:  # one candidate and one call per run (Section 10.5)
        assert plain.calls == 1 * (1 * tuning + held_out + delivered + tuning)
    else:  # the Spec cannot pin the plain arm yet, so nothing is counted as plain
        assert plain.calls == 4 * (2 * tuning + held_out + delivered + tuning)


@pytest.mark.skipif(not PLAIN_DECLARED, reason="WA's Spec.plain and WB's plain tuning")
async def test_a_plain_cell_measures_one_candidate_with_one_call_per_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_receipt_checker(monkeypatch)
    config = BenchConfig(
        suite="v2", tier="public", provider="fake", plain=True, floor_split="rehearsal",
        delivery_n=10, max_candidates=4, out=tmp_path / "out",
    )
    [row] = (await run_bench(config, workspace_root=tmp_path / "ws")).rows
    assert row.plain is True and row.params.get("verifier_depth") == 0
    assert row.params.get("retries") == 0
    assert row.delivery_calls == row.delivery_cases == 10


def test_reply_json_reaches_the_cell_spec() -> None:
    task = SUITES["v2"][0]
    spec = task.spec("fake", "default", suite="v2", reply_json="last")
    v1_spec = V1["invoice-40"].spec("fake", "default", reply_json="schema")
    if ARMS_DECLARED:
        assert getattr(spec, "reply_json") == "last"  # noqa: B009
        assert getattr(v1_spec, "reply_json") == "schema"  # noqa: B009
    else:
        assert v1_spec == V1["invoice-40"].spec("fake", "default")


def _bench(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = cli_main(["bench", *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_run_flags_and_refusals(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    fake = ["run", "--provider", "fake"]
    for args, code in (
        ([*fake, "--tier", "quick", "--tasks", "nope"], "bench_task_unknown"),
        ([*fake, "--suite", "v2", "--tier", "public", "--tasks", "invoice-40"],
         "bench_task_unknown"),
        ([*fake, "--tier", "quick", "--plain", "--variant", "forge"], "bench_plain_variant"),
        ([*fake, "--tier", "quick", "--floor-split", "rehearsal"], "bench_split_unsupported"),
        ([*fake, "--tier", "quick", "--delivery", "all"], "bench_delivery_all_unsupported"),
        ([*fake, "--suite", "v2", "--tier", "public", "--delivery", "400"],
         "bench_delivery_too_large"),
        ([*fake, "--suite", "v2", "--tier", "public", "--size", "8"], "bench_size_unsupported"),
        ([*fake, "--suite", "v2", "--tier", "quick"], "bench_tier_unknown"),
    ):
        exit_code, _, err = _bench(capsys, *args)
        assert exit_code == 1 and json.loads(err.strip().splitlines()[-1]) == {"error": code}
    for args in (
        [*fake, "--tier", "quick", "--delivery", "some"],
        [*fake, "--tier", "quick", "--reply-json", "loose"],
        [*fake, "--tier", "quick", "--floor-split", "pilot"],
    ):
        exit_code, _, err = _bench(capsys, *args)
        assert exit_code == 2 and json.loads(err) == {"error": "invalid_arguments"}
    assert not (tmp_path / ".architect").exists()  # nothing was written or spent


def test_cli_parses_the_new_flags() -> None:
    from architect import cli

    args = cli._parser().parse_args(
        [
            "bench", "run", "--provider", "fake", "--suite", "v2", "--tier", "public",
            "--tasks", "receipts,receipts", "--no-tuning-replay", "--plain",
            "--reply-json", "last", "--floor-split", "rehearsal", "--delivery-split", "pilot",
            "--delivery", "all", "--audit", "audits",
        ]
    )
    config = cli._bench_config(args)
    assert config.tasks == ("receipts",) and config.tuning_replay is False and config.plain
    assert config.reply_json == "last" and config.floor_split == "rehearsal"
    assert config.delivery_split == "pilot" and config.delivery_n == "all"
    assert config.audit == Path("audits")
    plain = cli._bench_config(cli._parser().parse_args(["bench", "run", "--provider", "fake",
                                                       "--tier", "quick"]))
    assert plain.tasks == () and plain.tuning_replay and not plain.plain
    assert plain.reply_json is None and plain.delivery_n == 0 and plain.audit is None


# ----------------------------------------------------------------------- resume


async def test_resume_carries_the_new_fields(tmp_path: Path) -> None:
    config = BenchConfig(
        tier="quick", provider="fake", size=4, tasks=("sentiment-24", "invoice-40"),
        tuning_replay=False, delivery_n=2, out=tmp_path / "out",
    )
    first = await run_bench(config, workspace_root=tmp_path / "ws")
    directory = config.out / first.bench_id
    record = json.loads((directory / RUN_FILE).read_text(encoding="utf-8"))
    assert record["config"]["tasks"] == ["sentiment-24", "invoice-40"]
    assert record["config"]["tuning_replay"] is False and record["version"] == 1
    rows_path = directory / "rows.jsonl"
    rows_path.write_text(first.rows[0].model_dump_json() + "\n", encoding="utf-8")
    resumed = await run_bench(config, workspace_root=tmp_path / "ws", resume=first.bench_id)
    assert [row.task_id for row in resumed.rows] == ["sentiment-24", "invoice-40"]
    assert all(row.tuning_replay is False for row in resumed.rows)
    changed = config.model_copy(update={"tuning_replay": True})
    with pytest.raises(BenchError, match="bench_resume_mismatch"):
        await run_bench(changed, resume=first.bench_id)


async def test_a_run_recorded_before_phase_14_still_resumes(tmp_path: Path) -> None:
    config = BenchConfig(tier="quick", provider="fake", size=4, out=tmp_path / "out")
    first = await run_bench(config, workspace_root=tmp_path / "ws")
    directory = config.out / first.bench_id
    record = json.loads((directory / RUN_FILE).read_text(encoding="utf-8"))
    for key in ("tasks", "tuning_replay", "plain", "reply_json", "floor_split",
                "delivery_split", "audit"):
        del record["config"][key]  # what a Phase 11 run.json held
    (directory / RUN_FILE).write_text(json.dumps(record), encoding="utf-8")
    (directory / "rows.jsonl").write_text(
        first.rows[0].model_dump_json() + "\n", encoding="utf-8"
    )
    resumed = await run_bench(config, workspace_root=tmp_path / "ws", resume=first.bench_id)
    assert [row.task_id for row in resumed.rows] == [row.task_id for row in first.rows]


# ------------------------------------------------------------------------ audit


@contextmanager
def fake_audit_module(entered: list[Path]) -> Iterator[types.ModuleType]:
    """A stand-in for ``architect.compiler.forge_audit`` (Section 8.2's API)."""
    module = types.ModuleType(FORGE_AUDIT_MODULE)

    class ForgeAudit:
        def __init__(self, directory: Path) -> None:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / ".probe"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
            self.directory = directory.resolve()

    @contextmanager
    def forge_audit(directory: Path | None) -> Iterator[ForgeAudit | None]:
        assert directory is not None
        entered.append(directory)
        yield ForgeAudit(directory)

    module.ForgeAudit = ForgeAudit  # type: ignore[attr-defined]
    module.forge_audit = forge_audit  # type: ignore[attr-defined]
    yield module


async def test_audit_scopes_each_cell_and_an_unwritable_directory_spends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered: list[Path] = []
    calls: list[int] = []
    original = OracleAdapter.complete

    async def counting(self: OracleAdapter, request: ModelRequest) -> ModelResponse:
        calls.append(1)
        return await original(self, request)

    monkeypatch.setattr(OracleAdapter, "complete", counting)
    with fake_audit_module(entered) as module:
        monkeypatch.setitem(sys.modules, FORGE_AUDIT_MODULE, module)
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        config = BenchConfig(
            tier="quick", provider="fake", size=2, tasks=("sentiment-24", "invoice-40"),
            audit=blocker / "audit", out=tmp_path / "out",
        )
        with pytest.raises(BenchError) as caught:
            await run_bench(config, workspace_root=tmp_path / "ws")
        assert caught.value.code == "audit_unwritable"
        assert calls == [] and not (tmp_path / "out").exists()
        writable = config.model_copy(update={"audit": tmp_path / "audit"})
        result = await run_bench(writable, workspace_root=tmp_path / "ws")
    assert len(result.rows) == 2 and calls
    assert entered == [tmp_path / "audit"] * 2  # one compile scope per cell


async def test_audit_records_appear_for_a_forge_cell(tmp_path: Path) -> None:
    """With the real Forge audit (Section 8), a forge cell's submissions are copied out."""
    pytest.importorskip(FORGE_AUDIT_MODULE)
    config = BenchConfig(
        tier="quick", provider="fake", size=4, tasks=("sentiment-24",),
        variants=("forge",), audit=tmp_path / "audit", out=tmp_path / "out",
    )
    result = await run_bench(config, workspace_root=tmp_path / "ws")
    [row] = result.rows
    if "forge_did_not_run" in row.failures:
        pytest.skip("Pi is not installed in this checkout (architect/backends/pi-node)")
    records = list((tmp_path / "audit").glob("*/*/*/submission.json"))
    assert records and all(path.parts[-4] == row.spec_hash for path in records)
    # The replay (Section 13.2) reads what the Forge audit wrote: every record parses, and
    # each written candidate loads as the exact artifact the audit recorded.
    found = read_submissions(tmp_path / "audit")
    assert len(found) == len(records)
    for item in found:
        if item.record.artifact_digest is not None:
            artifact = HarnessArtifact.load(item.directory)
            assert artifact.artifact_digest == item.record.artifact_digest
            assert artifact.spec.spec_hash == item.record.spec_hash


def test_the_progress_and_rows_hold_no_case_content(v2: dict[str, BenchRow]) -> None:
    public_tasks.clear_cache()
    data = public_tasks.load_public("receipts")
    text = "".join(row.model_dump_json() for row in v2.values())
    for case in (*data.splits["rehearsal"][:5], *data.splits["delivery"][:5]):
        assert case.case_id not in text
        receipt = case.task["receipt"]  # type: ignore[index,call-overload]
        assert isinstance(receipt, str) and receipt[:24] not in text


async def test_a_pinned_run_budget_below_one_runs_sized_calls_is_refused_before_spend(
    tmp_path: Path,
) -> None:
    """Phase 14 review: the settings pin each run's dollars. When one run's sized calls
    cost more than that, every run would be refused at its first reservation and the arm
    zeroed, so the bench refuses the run before any spend; the alias's output cap fits."""
    import httpx

    from architect.models import OpenAICompatibleAdapter

    ultra = "nvidia/Nemotron-3-Ultra-550b-a55b"
    sent: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(1)
        return httpx.Response(500)

    def adapters(cap: int | None) -> Any:
        def make(model: str) -> ModelAdapter:
            return OpenAICompatibleAdapter(
                model, api_key="offline", base_url="https://relay.invalid/v1",
                transport=httpx.MockTransport(handler), dialect="nebius", max_tokens=cap,
            )

        return make

    public_tasks.clear_cache()
    pinned = public_tasks.load_public("receipts").settings.run_budget
    assert pinned is not None
    config = BenchConfig(
        suite="v2", tier="public", provider="fake", models=(ultra,), plain=True,
        floor_split="rehearsal", delivery_n=4, out=tmp_path / "out",
    )
    with pytest.raises(BenchError, match="^bench_run_budget_too_small$"):
        bound(config, factory=adapters(None))  # the CLI's bound, before consent
    with pytest.raises(BenchError, match="^bench_run_budget_too_small$"):
        await run_bench(config, factory=adapters(None), workspace_root=tmp_path / "ws")
    assert sent == [] and not (tmp_path / "out").exists()
    # The evidence profile's 512-token cap: one plain call fits the pinned budget.
    [cell] = bound(config, factory=adapters(512)).cells
    assert cell.code is None and 0 < cell.per_call_usd <= pinned.cost_usd
    # Four sized calls (verifier_depth and retries) do not, so that arm is refused too.
    if 4 * cell.per_call_usd > pinned.cost_usd:
        with pytest.raises(BenchError, match="^bench_run_budget_too_small$"):
            bound(config.model_copy(update={"plain": False}), factory=adapters(512))
