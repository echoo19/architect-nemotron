"""Stage-3 guarded diff proposals, sandbox regression gates, and safe accounting."""

from __future__ import annotations

import asyncio
import difflib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from architect.compiler import rewriting
from architect.compiler.rewriting import RewriteProposal, RewriteResult, rewrite
from architect.compiler.types import CandidateEvaluation
from architect.floors import EvaluationCase, HeldOutTasks
from architect.models import FakeAdapter, ModelRequest, ModelResponse
from architect.presets import get_preset
from architect.runtime import Runtime
from architect.runtime.evaluation import evaluate_cases
from architect.runtime.wallet import compilation_wallet
from architect.spec import Budget, FloorDefinition, FloorKind, HarnessArtifact, Spec
from architect.telemetry import TelemetryStore, categorical

FIXTURES = Path(__file__).parent / "fixtures" / "rewrites"
BASE = (FIXTURES / "baseline.py").read_text(encoding="utf-8")
PROPOSAL = (FIXTURES / "remove-review.json").read_text(encoding="utf-8")
SECRET = "PRIVATE_LABEL_NEVER_FOR_MODEL_OR_TELEMETRY"


def response(text: str, *, cost: float = 0.01) -> ModelResponse:
    return ModelResponse(text=text, model="fake", tokens_in=1, tokens_out=1, cost_usd=cost)


def adapter(model: str) -> FakeAdapter:
    return FakeAdapter(model, responses=[response("ok")] * 8)


def floor(spec: Spec, *, expected: str = "ok", prefix: str = "tuning") -> HeldOutTasks:
    return HeldOutTasks(spec.floor, [
        EvaluationCase(case_id=f"{prefix}-{i}", task={"index": i}, expected=expected,
                       metadata={"private": SECRET})
        for i in range(4)
    ])


async def setup(
    root: Path, *, cost_limit: float = 0.025,
) -> tuple[CandidateEvaluation, HeldOutTasks, HeldOutTasks, Runtime]:
    spec = Spec(
        task="Produce the requested result", input_schema={"type": "object"},
        output_schema={"type": "string"},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, dataset="synthetic", limit=1),
        budget=Budget(cost_usd=cost_limit),
    )
    directory = root / "artifacts" / spec.spec_hash / "1"
    directory.mkdir(parents=True)
    (directory / "harness.py").write_text(BASE, encoding="utf-8")
    preset = get_preset("synthesizer")
    artifact = HarnessArtifact(
        spec=spec, preset=preset.name, model="fake", directory=directory,
        components=preset.components, params=preset.validate_params({}),
    )
    artifact.save()
    primary = floor(spec)
    smoke = floor(spec, prefix="smoke")
    runtime = Runtime(TelemetryStore(root / "telemetry"))
    measured = await evaluate_cases(artifact, primary, primary.tuning, adapter, runtime)
    return CandidateEvaluation(artifact=artifact, measurement=measured), primary, smoke, runtime


def proposal(new: str, components: tuple[str, ...] = ("synthesis",)) -> str:
    patch = "".join(difflib.unified_diff(
        BASE.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile="a/harness.py", tofile="b/harness.py",
    ))
    return json.dumps({"diff": patch, "components": components})


async def attempt(
    root: Path, candidate: CandidateEvaluation, primary: HeldOutTasks,
    smoke: HeldOutTasks, runtime: Runtime, text: str = PROPOSAL, *, cap: int = 1,
) -> RewriteResult:
    return await rewrite(
        candidate, get_preset("synthesizer"), primary, smoke, adapter, runtime,
        root / "artifacts", FakeAdapter(responses=[response(text)] * max(int(cap), 1)),
        max_iterations=cap,
    )


async def test_budget_failed_baseline_repaired_full_sets_and_atomic_version(tmp_path: Path) -> None:
    candidate, primary, smoke, runtime = await setup(tmp_path)
    assert not candidate.measurement.budget_passed
    assert candidate.measurement.mean_cost_usd == pytest.approx(0.02)
    # The denied third reservation is not invented consumed cost.
    assert candidate.measurement.max_cost_usd == pytest.approx(0.03)
    before = (candidate.artifact.directory / "harness.py").read_bytes()
    result = await attempt(tmp_path, candidate, primary, smoke, runtime, cap=8)
    assert result.attempts == result.accepted == 1 and not result.failures
    best = result.best
    assert best.measurement.mean_cost_usd == pytest.approx(0.01)
    assert best.measurement.sample_count == len(primary.tuning) == 2
    assert best.artifact.components == ("synthesis",)
    assert best.artifact.execution_mode == "sandbox"
    assert best.artifact.version == 2
    assert best.artifact.report is None and not best.artifact.licensed
    assert HarnessArtifact.load(best.artifact.directory) == best.artifact
    assert (best.artifact.directory / "rewrite.diff").read_text() == json.loads(PROPOSAL)["diff"]
    assert (candidate.artifact.directory / "harness.py").read_bytes() == before
    rows = runtime.store.rows()
    runs = [row for row in rows if row.harness_version == 2
            and row.task_type == categorical("synthesizer")]
    assert len(runs) == len(primary.tuning) + len(smoke.tuning)
    events = [row for row in rows if row.task_type == categorical("rewrite")]
    assert len(events) == 1 and events[0].accepted and events[0].floor_score == 1
    assert events[0].tokens_in == events[0].tokens_out == events[0].cost_usd == 0
    assert events[0].model_params == {
        "context_window": 2048, "terseness": 0, "verifier_depth": 0,
    }
    assert sum(row.cost_usd for row in rows) == pytest.approx(0.09)
    assert SECRET not in (tmp_path / "telemetry" / "runs.jsonl").read_text()
    again = await attempt(tmp_path, candidate, primary, smoke, runtime)
    assert again.best.artifact.version == 3


@pytest.mark.parametrize("cap", [0, 1, 3])
async def test_attempt_cap_and_no_free_text(tmp_path: Path, cap: int) -> None:
    candidate, primary, smoke, runtime = await setup(tmp_path)
    script = FakeAdapter(responses=[response(SECRET)] * 5)
    result = await rewrite(
        candidate, get_preset("synthesizer"), primary, smoke, adapter, runtime,
        tmp_path / "artifacts", script, max_iterations=cap,
    )
    assert result.attempts == script.calls == cap
    assert result.accepted == 0 and result.best == candidate
    assert result.failures == ("invalid_proposal",) * cap
    assert SECRET not in result.model_dump_json()
    assert SECRET not in (tmp_path / "telemetry" / "runs.jsonl").read_text()
    events = [row for row in runtime.store.rows() if row.task_type == categorical("rewrite")]
    assert len(events) == cap and all(row.floor_score is None for row in events)


@pytest.mark.parametrize("cap", [-1, True, 1.5])
async def test_invalid_cap(tmp_path: Path, cap: int) -> None:
    candidate, primary, smoke, runtime = await setup(tmp_path)
    with pytest.raises(ValueError, match="invalid_iterations"):
        await attempt(tmp_path, candidate, primary, smoke, runtime, cap=cap)


@pytest.mark.parametrize("text,code", [
    ("{}", "invalid_proposal"),
    ('{"diff":"x","components":[],"surprise":true}', "invalid_proposal"),
    ('{"diff":"x","diff":"y","components":[]}', "invalid_proposal"),
    (proposal('async def run(context, task):\n    return "ok"\n', ()), "invalid_components"),
    (proposal('async def run(context, task):\n    return "ok"\n', ("bad",)), "invalid_components"),
    (proposal('async def run(context, task):\n    return "ok"\n',
              ("synthesis", "synthesis")), "invalid_components"),
    (proposal('async def run(context, task):\n    return __import__("os").environ\n'),
     "unsafe_source"),
    (proposal('import os\nasync def run(context, task):\n    return "ok"\n'), "unsafe_source"),
])
async def test_bad_proposals_never_execute_or_persist(
    tmp_path: Path, text: str, code: str,
) -> None:
    candidate, primary, smoke, runtime = await setup(tmp_path)
    result = await attempt(tmp_path, candidate, primary, smoke, runtime, text)
    assert result.accepted == 0 and result.failures == (code,)
    assert sorted(path.name for path in candidate.artifact.directory.parent.iterdir()) == ["1"]


async def test_full_smoke_regression_rejects_cheap_edit(tmp_path: Path) -> None:
    candidate, primary, _, runtime = await setup(tmp_path)
    smoke = floor(candidate.artifact.spec, expected="different", prefix="smoke")
    result = await attempt(tmp_path, candidate, primary, smoke, runtime)
    assert result.best == candidate and result.failures == ("smoke_failed",)
    events = [row for row in runtime.store.rows() if row.task_type == categorical("rewrite")]
    assert events[0].floor_score == 1 and not events[0].accepted


async def test_tuning_failure_does_not_skip_smoke(tmp_path: Path) -> None:
    candidate, primary, smoke, runtime = await setup(tmp_path)
    result = await attempt(tmp_path, candidate, primary, smoke, runtime,
                           proposal('async def run(context, task):\n    return "bad"\n'))
    assert result.failures == ("tuning_failed",)
    evaluations = [row for row in runtime.store.rows() if row.harness_version == 2
                   and row.task_type == categorical("quality_evaluation")]
    assert len(evaluations) == 2
    events = [row for row in runtime.store.rows() if row.task_type == categorical("rewrite")]
    assert events[0].floor_score == 0


async def test_equal_cost_is_not_repair(tmp_path: Path) -> None:
    candidate, primary, smoke, runtime = await setup(tmp_path)
    text = proposal(BASE.replace('    await context.call("first")\n', ""))
    result = await attempt(tmp_path, candidate, primary, smoke, runtime, text)
    assert result.failures == ("cost_not_lower",)


async def test_zero_observed_cost_never_funds_positive_cost(tmp_path: Path) -> None:
    candidate, primary, smoke, runtime = await setup(tmp_path, cost_limit=0.005)
    assert candidate.measurement.mean_cost_usd == 0

    def cheap(model: str) -> FakeAdapter:
        return FakeAdapter(model, responses=[response("ok", cost=0.001)])

    result = await rewrite(
        candidate, get_preset("synthesizer"), primary, smoke, cheap, runtime,
        tmp_path / "artifacts", FakeAdapter(responses=[response(PROPOSAL)]), max_iterations=1,
    )
    assert result.failures == ("cost_not_lower",)


async def test_actual_zero_dollar_token_decrease(tmp_path: Path) -> None:
    candidate, primary, smoke, runtime = await setup(tmp_path)

    def free(model: str) -> FakeAdapter:
        return FakeAdapter(model, responses=[response("ok", cost=0)] * 3)

    baseline = await evaluate_cases(candidate.artifact, primary, primary.tuning, free, runtime)
    candidate = CandidateEvaluation(artifact=candidate.artifact, measurement=baseline)
    result = await rewrite(
        candidate, get_preset("synthesizer"), primary, smoke, free, runtime,
        tmp_path / "artifacts", FakeAdapter(responses=[response(PROPOSAL)]), max_iterations=1,
    )
    assert result.accepted == 1
    assert result.best.measurement.mean_cost_usd == 0
    assert baseline.mean_tokens is not None
    assert result.best.measurement.mean_tokens == 2 < baseline.mean_tokens


async def test_proposal_budget_independent_of_task_but_wallet_enforced(tmp_path: Path) -> None:
    candidate, primary, smoke, runtime = await setup(tmp_path)
    script = FakeAdapter(responses=[response(PROPOSAL)])
    with compilation_wallet(Budget(cost_usd=0.005)) as wallet:
        result = await rewrite(
            candidate, get_preset("synthesizer"), primary, smoke, adapter, runtime,
            tmp_path / "artifacts", script, max_iterations=2,
        )
    assert script.calls == 0 and wallet.tokens == 0
    assert result.attempts == 2 and result.failures == ("proposal_failed",) * 2


async def test_prompt_omits_labels_metadata_and_failure_text(tmp_path: Path) -> None:
    class Capture(FakeAdapter):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            assert SECRET not in request.prompt
            assert '"source"' in request.prompt and '"measurement"' in request.prompt
            assert request.max_output_tokens is None
            return await super().complete(request)

    candidate, primary, smoke, runtime = await setup(tmp_path)
    candidate = candidate.model_copy(update={
        "measurement": candidate.measurement.model_copy(update={"failures": (SECRET,)}),
    })
    result = await rewrite(
        candidate, get_preset("synthesizer"), primary, smoke, adapter, runtime,
        tmp_path / "artifacts", Capture(responses=[response(PROPOSAL)]), max_iterations=1,
    )
    assert result.accepted == 1


async def test_floor_mismatch_and_same_smoke_rejected(tmp_path: Path) -> None:
    candidate, primary, smoke, runtime = await setup(tmp_path)
    with pytest.raises(ValueError, match="floor_mismatch"):
        await attempt(tmp_path, candidate, primary, primary, runtime)
    other = candidate.artifact.spec.model_copy(update={
        "floor": candidate.artifact.spec.floor.model_copy(update={"limit": 0.5}),
    })
    with pytest.raises(ValueError, match="floor_mismatch"):
        await attempt(tmp_path, candidate, primary, floor(other), runtime)


async def test_cancelled_proposal_propagates_and_logs_outcome(tmp_path: Path) -> None:
    started = asyncio.Event()

    class Blocking(FakeAdapter):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    candidate, primary, smoke, runtime = await setup(tmp_path)
    task = asyncio.create_task(rewrite(
        candidate, get_preset("synthesizer"), primary, smoke, adapter, runtime,
        tmp_path / "artifacts", Blocking(responses=[response(PROPOSAL)]), max_iterations=3,
    ))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    events = [row for row in runtime.store.rows() if row.task_type == categorical("rewrite")]
    assert len(events) == 1 and not events[0].accepted


@pytest.mark.parametrize("replacement", [
    "../harness.py", "/tmp/harness.py", "b/other.py", "/dev/null", "C:/harness.py",
])
def test_unified_diff_paths_closed(replacement: str) -> None:
    data = json.loads(PROPOSAL)
    data["diff"] = data["diff"].replace("b/harness.py", replacement)
    with pytest.raises(ValidationError):
        RewriteProposal.model_validate(data)


@pytest.mark.parametrize("mutation", [
    lambda text: text.replace("@@ -1,4 +1,2 @@", "@@ -2,4 +1,2 @@"),
    lambda text: text.replace("@@ -1,4 +1,2 @@", "@@ -1,3 +1,2 @@"),
    lambda text: text.replace('"first"', '"wrong"'),
    lambda text: text + "--- a/other.py\n+++ b/other.py\n@@ -0,0 +1 @@\n+bad\n",
])
def test_bad_offsets_counts_context_and_multiple_files(mutation: object) -> None:
    from collections.abc import Callable
    from typing import cast

    patch = cast(Callable[[str], str], mutation)(json.loads(PROPOSAL)["diff"])
    with pytest.raises(ValueError, match="invalid_diff"):
        rewriting._apply_diff(BASE, patch)


def test_diff_omitted_counts_insert_and_no_final_newline() -> None:
    assert rewriting._apply_diff(
        "a\n", "--- harness.py\n+++ harness.py\n@@ -1 +1 @@\n-a\n+b\n",
    ) == "b\n"
    assert rewriting._apply_diff(
        "a\n", "--- harness.py\n+++ harness.py\n@@ -0,0 +1 @@\n+b\n",
    ) == "b\na\n"
    assert rewriting._apply_diff(
        "a", "--- harness.py\n+++ harness.py\n@@ -1 +1 @@\n-a\n"
        "\\ No newline at end of file\n+b\n\\ No newline at end of file\n",
    ) == "b"


def test_proposal_byte_limit_extra_fields_and_frozen() -> None:
    data = json.loads(PROPOSAL)
    with pytest.raises(ValidationError):
        RewriteProposal.model_validate({**data, "extra": True})
    with pytest.raises(ValidationError):
        RewriteProposal.model_validate({**data, "diff": "é" * 32769})
    parsed = RewriteProposal.model_validate(data)
    with pytest.raises(ValidationError):
        parsed.diff = "anything"


async def test_rejected_first_proposal_then_repair_stops(tmp_path: Path) -> None:
    candidate, primary, smoke, runtime = await setup(tmp_path)
    script = FakeAdapter(responses=[response(SECRET), response(PROPOSAL), response(SECRET)])
    result = await rewrite(
        candidate, get_preset("synthesizer"), primary, smoke, adapter, runtime,
        tmp_path / "artifacts", script, max_iterations=3,
    )
    assert result.attempts == script.calls == 2
    assert result.accepted == 1 and result.failures == ("invalid_proposal",)
    events = [row for row in runtime.store.rows() if row.task_type == categorical("rewrite")]
    assert [row.accepted for row in events] == [False, True]


async def test_runtime_error_has_no_fabricated_floor_score(tmp_path: Path) -> None:
    candidate, primary, smoke, runtime = await setup(tmp_path)
    result = await attempt(tmp_path, candidate, primary, smoke, runtime,
                           proposal('async def run(context, task):\n    return {}\n'))
    assert result.failures == ("tuning_failed",)
    events = [row for row in runtime.store.rows() if row.task_type == categorical("rewrite")]
    assert events[0].floor_score is None


async def test_smoke_budget_failure_rejects(tmp_path: Path) -> None:
    candidate, primary, smoke, runtime = await setup(tmp_path)
    count = 0

    def changing(model: str) -> FakeAdapter:
        nonlocal count
        count += 1
        return FakeAdapter(model, responses=[response("ok", cost=0.01 if count <= 2 else 0.03)])

    result = await rewrite(
        candidate, get_preset("synthesizer"), primary, smoke, changing, runtime,
        tmp_path / "artifacts", FakeAdapter(responses=[response(PROPOSAL)]), max_iterations=1,
    )
    assert count == 4 and result.failures == ("smoke_failed",)


async def test_proposal_codegen_budget_not_target_budget(tmp_path: Path) -> None:
    candidate, primary, smoke, runtime = await setup(tmp_path)
    # A real code-generation response may cost more than the tiny candidate budget.
    script = FakeAdapter(responses=[response(PROPOSAL, cost=0.04)])
    result = await rewrite(
        candidate, get_preset("synthesizer"), primary, smoke, adapter, runtime,
        tmp_path / "artifacts", script, max_iterations=1,
    )
    assert script.calls == result.accepted == 1


async def test_unknown_zero_cost_token_baseline_fails_closed(tmp_path: Path) -> None:
    candidate, primary, smoke, runtime = await setup(tmp_path)
    candidate = candidate.model_copy(update={
        "measurement": candidate.measurement.model_copy(update={
            "mean_cost_usd": 0.0, "mean_tokens": None, "max_tokens": 100000,
        }),
    })
    result = await attempt(tmp_path, candidate, primary, smoke, runtime,
                           proposal('async def run(context, task):\n    return "ok"\n'))
    assert result.failures == ("cost_not_lower",)


async def test_denied_cost_reservation_tokens_are_not_consumed_savings(tmp_path: Path) -> None:
    candidate, primary, smoke, runtime = await setup(tmp_path)
    candidate = candidate.model_copy(update={
        "measurement": candidate.measurement.model_copy(update={
            "mean_cost_usd": 0.0, "mean_tokens": 0.0, "max_tokens": 100000,
        }),
    })
    result = await attempt(tmp_path, candidate, primary, smoke, runtime,
                           proposal('async def run(context, task):\n    return "ok"\n'))
    assert result.failures == ("cost_not_lower",)


async def test_empty_tuning_and_smoke_sets_rejected_before_proposal(tmp_path: Path) -> None:
    class Empty(HeldOutTasks):
        @property
        def tuning(self) -> tuple[EvaluationCase, ...]:
            return ()

    candidate, primary, smoke, runtime = await setup(tmp_path)
    empty = Empty(primary.definition, [
        EvaluationCase(case_id=str(i), task={}, expected="ok") for i in range(4)
    ])
    with pytest.raises(ValueError, match="invalid_cases"):
        await attempt(tmp_path, candidate, empty, smoke, runtime)
    with pytest.raises(ValueError, match="invalid_cases"):
        await attempt(tmp_path, candidate, primary, empty, runtime)


def test_noop_diff_and_oversized_result_rejected() -> None:
    with pytest.raises(ValueError, match="invalid_diff"):
        rewriting._apply_diff("a\n", "--- harness.py\n+++ harness.py\n@@ -1 +1 @@\n a\n")
    with pytest.raises(ValueError, match="invalid_diff"):
        rewriting._apply_diff("a\n", "--- harness.py\n+++ harness.py\n@@ -1 +1 @@\n-a\n+a\n")
    with pytest.raises(ValueError, match="invalid_diff"):
        rewriting._apply_diff("a\n", "--- harness.py\n+++ harness.py\n@@ -0,0 +1 @@\n+"
                              + "x" * 65536 + "\n")


def test_multiple_hunks_and_git_header() -> None:
    source = "".join(f"line{i}\n" for i in range(20))
    edited = source.replace("line1\n", "replacement1\n").replace("line18\n", "replacement18\n")
    diff = "".join(difflib.unified_diff(source.splitlines(keepends=True),
                                      edited.splitlines(keepends=True),
                                      fromfile="a/harness.py", tofile="b/harness.py"))
    diff = "diff --git a/harness.py b/harness.py\nindex aabbcc..ddeeff 100644\n" + diff
    assert rewriting._apply_diff(source, diff) == edited
