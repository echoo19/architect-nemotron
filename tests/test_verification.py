"""Stage-4 licensing regressions, secondary gates and complete replay coverage."""

from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import JsonValue

from architect._authority import verify_report
from architect.compiler import verification
from architect.compiler.types import AdapterFactory, Measurement
from architect.compiler.verification import verify
from architect.floors import EditLogRow, EvaluationCase, FloorEvaluator, HeldOutTasks, HumanEditRate
from architect.models import FakeAdapter, ModelResponse
from architect.runtime import Runtime
from architect.spec import Budget, FloorDefinition, FloorKind, HarnessArtifact, Spec, TestReport
from architect.telemetry import TelemetryStore, categorical


class CountingFloor(HeldOutTasks):
    def __init__(self, definition: FloorDefinition, *, expected: str = "ok") -> None:
        super().__init__(
            definition,
            [EvaluationCase(case_id=str(i), task=str(i), expected=expected) for i in range(6)],
        )
        self.issued = 0
        self.scored: list[str] = []

    def _verification_cases(self, capability: object) -> tuple[EvaluationCase, ...]:
        self.issued += 1
        return super()._verification_cases(capability)

    async def score(self, cases: Sequence[EvaluationCase], outputs: Sequence[JsonValue]) -> float:
        self.scored.extend(case.case_id for case in cases)
        return await super().score(cases, outputs)


@pytest.fixture
def artifact(tmp_path: Path) -> HarnessArtifact:
    spec = Spec(
        task="Transform each value",
        input_schema={"type": "string"},
        output_schema={"type": "string"},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="primary"),
        budget=Budget(cost_usd=1, compute_s=5),
    )
    directory = tmp_path / "artifact"
    directory.mkdir()
    (directory / "harness.py").write_text(
        "async def run(context, task):\n    return await context.call(task)\n",
        encoding="utf-8",
    )
    result = HarnessArtifact(
        spec=spec,
        directory=directory,
        preset="synthesizer",
        model="fake",
        components=("synthesis",),
        params={"verifier_depth": 0},
    )
    result.save()
    return result


@pytest.fixture
def runtime(tmp_path: Path) -> Runtime:
    return Runtime(TelemetryStore(tmp_path / "telemetry"))


def authenticated(artifact: HarnessArtifact, passed: bool) -> TestReport:
    report = artifact.report
    assert report is not None
    assert report.passed is passed
    assert artifact.licensed is passed
    assert verify_report(report.model_dump(mode="json", exclude={"signature"}), report.signature)
    return report


def with_secondary(artifact: HarnessArtifact) -> tuple[HarnessArtifact, HumanEditRate]:
    definition = FloorDefinition(
        kind=FloorKind.HUMAN_EDIT_RATE,
        limit=0.25,
        dataset="secondary",
        minimum_samples=2,
    )
    floor = HumanEditRate(
        definition,
        [
            EditLogRow(case_id=str(i), task=str(i), proposed="old", shipped="ok", changed=True)
            for i in range(4)
        ],
    )
    return artifact.model_copy(
        update={
            "spec": artifact.spec.model_copy(update={"secondary_floor": definition}),
        }
    ), floor


async def test_full_partition_is_issued_once_and_persisted(
    artifact: HarnessArtifact,
    runtime: Runtime,
) -> None:
    floor = CountingFloor(artifact.spec.floor)
    adapter = FakeAdapter()
    result = await verify(artifact, floor, lambda _: adapter, runtime)
    report = authenticated(result, True)
    assert floor.issued == 1
    assert floor.scored == ["1", "3", "5"]
    assert adapter.calls == 3
    assert report.held_out_count == 3
    assert report.artifact_digest == artifact.artifact_digest
    assert report.spec_hash == artifact.spec.spec_hash
    assert report.component_count == artifact.component_count
    assert artifact.report is None
    assert HarnessArtifact.load(artifact.directory) == result


async def test_old_authentic_report_cannot_hide_regression(
    artifact: HarnessArtifact,
    runtime: Runtime,
) -> None:
    first = await verify(artifact, CountingFloor(artifact.spec.floor), FakeAdapter, runtime)
    before = authenticated(first, True)
    second = await verify(
        first, CountingFloor(artifact.spec.floor, expected="wrong"), FakeAdapter, runtime
    )
    after = authenticated(second, False)
    assert before.verification_id != after.verification_id
    assert after.floor_score == 0
    assert "primary_floor_failed" in after.failures
    assert HarnessArtifact.load(artifact.directory).report == after


async def test_authentic_cache_is_always_reverified(
    artifact: HarnessArtifact,
    runtime: Runtime,
) -> None:
    floor = CountingFloor(artifact.spec.floor)
    adapter = FakeAdapter()
    first = await verify(artifact, floor, lambda _: adapter, runtime)
    second = await verify(first, floor, lambda _: adapter, runtime)
    assert authenticated(first, True).verification_id != authenticated(second, True).verification_id
    assert floor.issued == 2
    assert adapter.calls == 6


async def test_forged_report_is_replaced(artifact: HarnessArtifact, runtime: Runtime) -> None:
    first = await verify(artifact, CountingFloor(artifact.spec.floor), FakeAdapter, runtime)
    report = authenticated(first, True).model_copy(update={"signature": "0" * 64})
    forged = first.model_copy(update={"report": report})
    assert not forged.licensed
    result = await verify(forged, CountingFloor(artifact.spec.floor), FakeAdapter, runtime)
    assert authenticated(result, True).signature != report.signature


@pytest.mark.parametrize(
    "field,value",
    [
        ("dataset", "other"),
        ("limit", 0.1),
        ("minimum_samples", 31),
        ("rubric", "other"),
        ("judge_model", "other"),
        ("calibration_dataset", "other"),
    ],
)
async def test_exact_primary_definition_required(
    artifact: HarnessArtifact,
    runtime: Runtime,
    field: str,
    value: JsonValue,
) -> None:
    floor = CountingFloor(artifact.spec.floor.model_copy(update={field: value}))
    result = await verify(artifact, floor, FakeAdapter, runtime)
    report = authenticated(result, False)
    assert report.failures == ("primary_floor_mismatch",)
    assert floor.issued == 0
    assert report.held_out_count == 0


@pytest.mark.parametrize("mode", ["missing", "mismatch", "unexpected"])
async def test_secondary_configuration_fails_closed(
    artifact: HarnessArtifact,
    runtime: Runtime,
    mode: str,
) -> None:
    candidate, secondary = with_secondary(artifact)
    supplied: FloorEvaluator | None = secondary
    if mode == "missing":
        supplied = None
    elif mode == "mismatch":
        supplied = CountingFloor(artifact.spec.floor)
    else:
        candidate = artifact
    primary = CountingFloor(candidate.spec.floor)
    result = await verify(candidate, primary, FakeAdapter, runtime, secondary_floor=supplied)
    expected = {
        "missing": "secondary_floor_missing",
        "mismatch": "secondary_floor_mismatch",
        "unexpected": "unexpected_secondary_floor",
    }[mode]
    assert authenticated(result, False).failures == (expected,)
    assert primary.issued == 0


@pytest.mark.parametrize("secondary_output,passed", [("ok", True), ("wrong", False)])
async def test_secondary_edit_rate_and_maximum_usage(
    artifact: HarnessArtifact,
    runtime: Runtime,
    secondary_output: str,
    passed: bool,
) -> None:
    candidate, secondary = with_secondary(artifact)
    adapter = FakeAdapter(
        responses=[
            ModelResponse(model="fake", text=text, tokens_in=tokens, tokens_out=1, cost_usd=cost)
            for text, tokens, cost in [("ok", 2, 0.01)] * 3 + [(secondary_output, 8, 0.03)] * 2
        ]
    )
    result = await verify(
        candidate,
        CountingFloor(candidate.spec.floor),
        lambda _: adapter,
        runtime,
        secondary_floor=secondary,
    )
    report = authenticated(result, passed)
    assert report.floor_score == 1
    assert report.secondary_floor_score == (0 if passed else 1)
    assert report.secondary_floor_kind == FloorKind.HUMAN_EDIT_RATE
    assert report.secondary_floor_limit == 0.25
    assert report.held_out_count == 5
    assert report.tokens == 9
    assert report.cost_usd == 0.03
    assert report.mean_cost_usd == pytest.approx(0.01)
    assert adapter.calls == 5
    assert HarnessArtifact.load(candidate.directory).licensed is passed
    if not passed:
        assert "secondary_floor_failed" in report.failures


async def test_high_primary_and_secondary_tokens_do_not_block_signed_license(
    artifact: HarnessArtifact, runtime: Runtime,
) -> None:
    candidate, secondary = with_secondary(artifact)
    adapter = FakeAdapter(responses=[
        ModelResponse(model="fake", text="ok", tokens_in=2_000_000,
                      tokens_out=1_000_000, cost_usd=0.01)
    ] * 5)
    result = await verify(
        candidate, CountingFloor(candidate.spec.floor), lambda _: adapter, runtime,
        secondary_floor=secondary,
    )
    report = authenticated(result, True)
    assert report.tokens == 3_000_000 and report.held_out_count == 5
    assert report.cost_usd == pytest.approx(0.01)
    assert report.failures == ()
    assert HarnessArtifact.load(candidate.directory).licensed


async def test_primary_edit_rate_uses_ceiling(artifact: HarnessArtifact, runtime: Runtime) -> None:
    _, floor = with_secondary(artifact)
    candidate = artifact.model_copy(
        update={
            "spec": artifact.spec.model_copy(update={"floor": floor.definition}),
        }
    )
    result = await verify(candidate, floor, FakeAdapter, runtime)
    assert authenticated(result, True).floor_score == 0


@pytest.mark.parametrize("kind", ["runtime", "schema", "budget"])
async def test_one_hard_case_cannot_hide_in_average_and_all_cases_run(
    artifact: HarnessArtifact,
    runtime: Runtime,
    kind: str,
) -> None:
    code = {
        "runtime": "raise ValueError('PRIVATE-ERROR-CONTENT')",
        "schema": "return 7",
        "budget": "await context.call(task)\n        await context.call(task)",
    }[kind]
    (artifact.directory / "harness.py").write_text(
        "async def run(context, task):\n    if task == '3':\n        "
        + code
        + "\n    return await context.call(task)\n",
        encoding="utf-8",
    )
    spec = artifact.spec.model_copy(
        update={
            "floor": artifact.spec.floor.model_copy(update={"limit": 0}),
            "budget": Budget(cost_usd=0.000015, compute_s=5),
        }
    )
    candidate = artifact.model_copy(update={"spec": spec})
    result = await verify(candidate, CountingFloor(spec.floor), FakeAdapter, runtime)
    report = authenticated(result, False)
    assert report.held_out_count == 3
    assert "PRIVATE-ERROR-CONTENT" not in report.model_dump_json()
    assert "primary_evaluation_failed" in report.failures
    rows = runtime.store.rows()
    assert len([row for row in rows if row.task_type == categorical(candidate.preset)]) == 3


@pytest.mark.parametrize("change", ["code", "delete"])
async def test_code_change_during_verification_refuses_license(
    artifact: HarnessArtifact,
    runtime: Runtime,
    change: str,
) -> None:
    action = "write_text('# changed')" if change == "code" else "unlink(missing_ok=True)"
    (artifact.directory / "harness.py").write_text(
        "from pathlib import Path\nasync def run(context, task):\n"
        f"    Path(__file__).{action}\n    return 'ok'\n",
        encoding="utf-8",
    )
    result = await verify(artifact, CountingFloor(artifact.spec.floor), FakeAdapter, runtime)
    report = authenticated(result, False)
    assert ("artifact_changed" if change == "code" else "artifact_unreadable") in report.failures


async def test_missing_code_returns_failed_report(
    artifact: HarnessArtifact,
    runtime: Runtime,
) -> None:
    (artifact.directory / "harness.py").unlink()
    result = await verify(artifact, CountingFloor(artifact.spec.floor), FakeAdapter, runtime)
    report = authenticated(result, False)
    assert report.failures == ("artifact_unreadable", "artifact_save_failed")
    assert report.artifact_digest == "0" * 64
    assert report.mean_cost_usd is None
    assert (
        TestReport.model_validate_json(
            (artifact.directory / "report.json").read_text(encoding="utf-8")
        )
        == report
    )
    assert not (artifact.directory / "harness.py").exists()


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"sample_count": 2}, "primary_sample_count"),
        ({"floor_passed": False}, "primary_floor_failed"),
        ({"score": 0.5}, "primary_floor_failed"),
        ({"budget_passed": False}, "primary_budget_failed"),
        ({"max_cost_usd": 2}, "primary_budget_failed"),
        ({"max_compute_s": 6}, "primary_budget_failed"),
        ({"failures": ("PRIVATE-ERROR-CONTENT",)}, "primary_evaluation_failed"),
    ],
)
async def test_measurement_gates_are_independent(
    artifact: HarnessArtifact,
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, object],
    code: str,
) -> None:
    async def evaluate(
        artifact: HarnessArtifact,
        floor: FloorEvaluator,
        cases: Sequence[EvaluationCase],
        adapter_factory: AdapterFactory,
        runtime: Runtime,
        *,
        floor_role: str = "primary",
    ) -> Measurement:
        values: dict[str, object] = dict(
            score=1,
            floor_passed=True,
            budget_passed=True,
            mean_cost_usd=0,
            max_tokens=0,
            max_cost_usd=0,
            max_compute_s=0,
            sample_count=3,
        )
        values.update(changes)
        return Measurement.model_validate(values)

    monkeypatch.setattr(verification, "evaluate_cases", evaluate)
    result = await verify(artifact, CountingFloor(artifact.spec.floor), FakeAdapter, runtime)
    report = authenticated(result, False)
    assert report.failures == (code,)
    assert "PRIVATE-ERROR-CONTENT" not in report.model_dump_json()


async def test_unexpected_evaluation_error_is_private(
    artifact: HarnessArtifact,
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def evaluate(
        artifact: HarnessArtifact,
        floor: FloorEvaluator,
        cases: Sequence[EvaluationCase],
        adapter_factory: AdapterFactory,
        runtime: Runtime,
        *,
        floor_role: str = "primary",
    ) -> Measurement:
        raise RuntimeError("PRIVATE-ERROR-CONTENT")

    monkeypatch.setattr(verification, "evaluate_cases", evaluate)
    candidate, secondary = with_secondary(artifact)
    result = await verify(
        candidate,
        CountingFloor(candidate.spec.floor),
        FakeAdapter,
        runtime,
        secondary_floor=secondary,
    )
    report = authenticated(result, False)
    assert report.failures == ("primary_evaluation_error", "secondary_evaluation_error")
    assert report.held_out_count == 5


async def test_save_failure_is_a_signed_failure(
    artifact: HarnessArtifact,
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_save(self: HarnessArtifact) -> None:
        raise OSError("PRIVATE-ERROR-CONTENT")

    monkeypatch.setattr(HarnessArtifact, "save", fail_save)
    result = await verify(artifact, CountingFloor(artifact.spec.floor), FakeAdapter, runtime)
    report = authenticated(result, False)
    assert report.failures == ("artifact_save_failed",)
    assert (
        TestReport.model_validate_json(
            (artifact.directory / "report.json").read_text(encoding="utf-8")
        )
        == report
    )


@pytest.mark.parametrize("mutation", ["params", "spec", "components", "code"])
async def test_license_binds_every_artifact_input(
    artifact: HarnessArtifact,
    runtime: Runtime,
    mutation: str,
) -> None:
    result = await verify(artifact, CountingFloor(artifact.spec.floor), FakeAdapter, runtime)
    authenticated(result, True)
    if mutation == "params":
        result.params["verifier_depth"] = 1
    elif mutation == "spec":
        result.spec.input_schema["type"] = "integer"
    elif mutation == "components":
        result = result.model_copy(update={"components": ("other",)})
    else:
        path = result.directory / "harness.py"
        path.write_text(path.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
    assert not result.licensed


@pytest.mark.parametrize(
    "floor_passed,budget_passed,failures,passed",
    [
        (True, True, (), True),
        (False, True, (), False),
        (True, False, (), False),
        (True, True, ("runtime_error",), False),
    ],
)
async def test_failed_tuning_cannot_be_rescued_by_heldout_luck(
    artifact: HarnessArtifact,
    runtime: Runtime,
    floor_passed: bool,
    budget_passed: bool,
    failures: tuple[str, ...],
    passed: bool,
) -> None:
    evidence = Measurement(
        score=1,
        floor_passed=floor_passed,
        budget_passed=budget_passed,
        mean_cost_usd=0,
        max_tokens=0,
        max_cost_usd=0,
        max_compute_s=0,
        sample_count=3,
        failures=failures,
    )
    floor = CountingFloor(artifact.spec.floor)
    result = await verify(artifact, floor, FakeAdapter, runtime, tuning_evidence=evidence)
    report = authenticated(result, passed)
    assert report.floor_score == 1
    assert floor.scored == ["1", "3", "5"]
    assert report.failures == (() if passed else ("tuning_failed",))


async def test_invalid_floor_definition_returns_failed_report(
    artifact: HarnessArtifact,
    runtime: Runtime,
) -> None:
    class BrokenFloor(CountingFloor):
        @property
        def definition(self) -> FloorDefinition:
            raise ValueError("PRIVATE-ERROR-CONTENT")

    floor = BrokenFloor(artifact.spec.floor)
    result = await verify(artifact, floor, FakeAdapter, runtime)
    assert authenticated(result, False).failures == ("floor_definition_error",)
    assert floor.issued == 0


async def test_unwritable_failure_evidence_never_delivers_artifact(
    artifact: HarnessArtifact,
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_save(self: HarnessArtifact) -> None:
        raise OSError("unwritable")

    def fail_replace(source: str, destination: Path) -> None:
        raise OSError("unwritable")

    monkeypatch.setattr(HarnessArtifact, "save", fail_save)
    monkeypatch.setattr("architect.compiler.verification.os.replace", fail_replace)
    with pytest.raises(OSError, match="unwritable"):
        await verify(artifact, CountingFloor(artifact.spec.floor), FakeAdapter, runtime)
    assert not list(artifact.directory.glob(".report.*"))


async def test_ordinary_checker_miss_obeys_declared_aggregate_threshold(
    artifact: HarnessArtifact,
    runtime: Runtime,
) -> None:
    spec = artifact.spec.model_copy(
        update={
            "floor": artifact.spec.floor.model_copy(update={"limit": 0.6}),
        }
    )
    candidate = artifact.model_copy(update={"spec": spec})
    adapter = FakeAdapter(
        responses=[
            ModelResponse(model="fake", text=text, tokens_in=1, tokens_out=1, cost_usd=0)
            for text in ["ok", "ordinary miss", "ok"]
        ]
    )
    result = await verify(candidate, CountingFloor(spec.floor), lambda _: adapter, runtime)
    assert authenticated(result, True).floor_score == pytest.approx(2 / 3)
    assert adapter.calls == 3


async def test_primary_mean_cost_is_distinct_from_budget_maximum_and_signed(
    artifact: HarnessArtifact,
    runtime: Runtime,
) -> None:
    adapter = FakeAdapter(
        responses=[
            ModelResponse(model="fake", text="ok", tokens_in=1, tokens_out=1, cost_usd=cost)
            for cost in [0.01, 0.02, 0.09]
        ]
    )
    result = await verify(artifact, CountingFloor(artifact.spec.floor), lambda _: adapter, runtime)
    report = authenticated(result, True)
    assert report.mean_cost_usd == pytest.approx(0.04)
    assert report.cost_usd == 0.09
    loaded = HarnessArtifact.load(artifact.directory)
    assert loaded.report == report
    assert not result.model_copy(
        update={
            "report": report.model_copy(update={"mean_cost_usd": 0.01}),
        }
    ).licensed


# ------------------------------------------------------------------ retakes


class RetakeFloor(CountingFloor):
    """A CountingFloor (3 exam cases) that allows retakes, with ``extra`` more cases."""

    def __init__(self, definition: FloorDefinition, *, extra: int = 4, expected: str = "ok"):
        super().__init__(definition, expected=expected)
        self._allow_retakes(
            lambda: [
                EvaluationCase(case_id=f"r{i}", task=f"r{i}", expected=expected)
                for i in range(extra)
            ]
        )


async def test_a_first_exam_report_carries_no_retake_fields_and_old_reports_verify(
    artifact: HarnessArtifact, runtime: Runtime
) -> None:
    result = await verify(artifact, CountingFloor(artifact.spec.floor), FakeAdapter, runtime)
    report = authenticated(result, True)
    assert report.attempt is None and report.retake_cases is None
    dumped = report.model_dump(mode="json")
    assert "attempt" not in dumped and "retake_cases" not in dumped
    assert "attempt" not in report.signed_payload()
    # A report written before the fields existed is the same bytes: it loads and licenses.
    assert TestReport.model_validate(dumped) == report
    assert HarnessArtifact.load(artifact.directory).licensed


async def test_a_retake_report_signs_its_attempt_and_its_cases(
    artifact: HarnessArtifact, runtime: Runtime
) -> None:
    floor = RetakeFloor(artifact.spec.floor)
    recorded: list[tuple[str, tuple[str, ...]]] = []
    seen: list[tuple[int, int]] = []
    adapter = FakeAdapter()

    def record(cases: str, ids: tuple[str, ...]) -> None:
        assert adapter.calls == 0  # written before the first model call
        recorded.append((cases, ids))

    result = await verify(
        artifact, floor, lambda _: adapter, runtime,
        retake=verification.RetakeExam(2, (), record),
        on_case=lambda done, total: seen.append((done, total)),
    )
    report = authenticated(result, True)
    assert (report.attempt, report.retake_cases) == (2, "unused")
    assert report.held_out_count == 3 and seen == [(1, 3), (2, 3), (3, 3)]
    [(cases, ids)] = recorded
    assert cases == "unused" and len(ids) == 3 and set(ids) <= {"r0", "r1", "r2", "r3"}
    assert floor.scored == list(ids)  # the unused cases, not the exam's own
    assert report.signed_payload()["attempt"] == 2
    assert report.signed_payload()["retake_cases"] == "unused"
    loaded = HarnessArtifact.load(artifact.directory)
    assert loaded.licensed and loaded.report == report
    for forged in ({"attempt": 3}, {"retake_cases": "same_exam"}):
        assert not result.model_copy(
            update={"report": report.model_copy(update=forged)}
        ).licensed

    # One extra case is left, fewer than an exam: the next retake is the same exam.
    again = await verify(
        artifact, floor, FakeAdapter, runtime,
        retake=verification.RetakeExam(3, ids, lambda *row: recorded.append(row)),
    )
    report = authenticated(again, True)
    assert (report.attempt, report.retake_cases) == (3, "same_exam")
    assert recorded[-1] == ("same_exam", ())
    assert floor.scored[-3:] == ["1", "3", "5"]


async def test_a_retake_is_decided_by_the_pass_mark_alone(
    artifact: HarnessArtifact, runtime: Runtime
) -> None:
    result = await verify(
        artifact, RetakeFloor(artifact.spec.floor, expected="wrong"), FakeAdapter, runtime,
        retake=verification.RetakeExam(2),
    )
    report = authenticated(result, False)
    assert "primary_floor_failed" in report.failures
    assert (report.attempt, report.retake_cases) == (2, "unused")


async def test_a_floor_that_does_not_allow_retakes_refuses_before_any_model_call(
    artifact: HarnessArtifact, runtime: Runtime
) -> None:
    from architect.floors import HeldOutAccessError

    floor = CountingFloor(artifact.spec.floor)
    adapter = FakeAdapter()
    with pytest.raises(HeldOutAccessError):
        await verify(
            artifact, floor, lambda _: adapter, runtime, retake=verification.RetakeExam(2)
        )
    assert adapter.calls == 0 and floor.issued == 0
    assert not (artifact.directory / "report.json").exists()
    with pytest.raises(HeldOutAccessError):  # attempt 1 is the first exam, never a retake
        await verify(
            artifact, RetakeFloor(artifact.spec.floor), lambda _: adapter, runtime,
            retake=verification.RetakeExam(1),
        )


def test_a_report_names_both_retake_fields_or_neither() -> None:
    from pydantic import ValidationError

    base = dict(
        spec_hash="0" * 64, artifact_digest="1" * 64, passed=True, floor_score=1.0,
        floor_limit=1.0, floor_kind=FloorKind.HELD_OUT_TASKS, tokens=0, cost_usd=0.0,
        compute_s=0.0, component_count=1, verification_id="v",
    )
    assert TestReport(**base, attempt=2, retake_cases="same_exam").attempt == 2
    for bad in (
        {"attempt": 2}, {"retake_cases": "unused"}, {"attempt": 1, "retake_cases": "unused"}
    ):
        with pytest.raises(ValidationError):
            TestReport(**base, **bad)
