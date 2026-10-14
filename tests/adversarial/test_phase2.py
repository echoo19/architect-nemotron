"""Independent fake-only attacks on floor isolation and quality measurements."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from architect.floors import (
    CalibrationError,
    CalibrationRow,
    EditLogRow,
    EvaluationCase,
    FloorError,
    HeldOutAccessError,
    HeldOutGameScore,
    HeldOutTasks,
    HumanEditRate,
    LLMJudge,
)
from architect.floors._access import _VERIFIER_CAPABILITY
from architect.models import FakeAdapter, ModelRequest, ModelResponse
from architect.runtime import Runtime
from architect.spec import Budget, FloorDefinition, FloorKind
from architect.telemetry import TelemetryStore


def definition(kind: FloorKind = FloorKind.HELD_OUT_TASKS, *, minimum: int = 2) -> FloorDefinition:
    return FloorDefinition(
        kind=kind, limit=0.8, dataset="private-fixtures", minimum_samples=minimum
    )


def cases(count: int = 8) -> list[EvaluationCase]:
    return [
        EvaluationCase(
            case_id=f"case-{number:03d}",
            task={"nested": {"secret": f"private-task-{number:03d}"}},
            expected={"answer": number},
            metadata={"nested": {"secret": f"private-metadata-{number:03d}"}},
        )
        for number in range(count)
    ]


def edits(count: int = 60, *, changed: bool = False) -> list[EditLogRow]:
    return [
        EditLogRow(
            case_id=f"edit-{number:03d}",
            task={"index": number},
            proposed="historic-wrong" if changed else "shipped",
            shipped="shipped",
            changed=changed,
        )
        for number in range(count)
    ]


class EqualityImpersonator:
    def __eq__(self, other: object) -> bool:
        return True

    def __bool__(self) -> bool:
        return True


@pytest.mark.parametrize(
    "credential", [None, True, False, "stage4", "verified", object(), EqualityImpersonator()]
)
def test_heldout_requires_exact_capability_identity(credential: object) -> None:
    floor = HeldOutTasks(definition(), cases())
    with pytest.raises(HeldOutAccessError):
        floor._verification_cases(credential)
    with pytest.raises(HeldOutAccessError):
        _ = floor.held_out
    heldout = floor._verification_cases(_VERIFIER_CAPABILITY)
    assert heldout
    assert {case.case_id for case in heldout}.isdisjoint(case.case_id for case in floor.tuning)


async def test_original_records_do_not_confer_heldout_scoring_authority() -> None:
    records = cases()
    floor = HeldOutTasks(definition(), records)
    tuning_ids = {case.case_id for case in floor.tuning}
    unauthorized = [case for case in records if case.case_id not in tuning_ids]
    with pytest.raises(HeldOutAccessError):
        await floor.score(unauthorized, [case.expected for case in unauthorized])
    authorized = floor._verification_cases(_VERIFIER_CAPABILITY)
    assert await floor.score(authorized, [case.expected for case in authorized]) == 1
    # A legitimate verification must not globally unlock raw records afterward.
    with pytest.raises(HeldOutAccessError):
        await floor.score(unauthorized, [case.expected for case in unauthorized])


async def test_copying_heldout_fields_does_not_copy_verifier_authority() -> None:
    floor = HeldOutTasks(definition(), cases())
    heldout = floor._verification_cases(_VERIFIER_CAPABILITY)
    forged = [EvaluationCase.model_validate(case.model_dump()) for case in heldout]
    with pytest.raises(HeldOutAccessError):
        await floor.score(forged, [case.expected for case in forged])


async def test_tuning_case_cannot_be_renamed_to_access_heldout() -> None:
    floor = HeldOutTasks(definition(), cases())
    heldout = floor._verification_cases(_VERIFIER_CAPABILITY)
    tuning = floor.tuning
    forged = tuning[0].model_copy(update=heldout[0].model_dump())
    with pytest.raises(HeldOutAccessError):
        await floor.score([forged], [heldout[0].expected])


async def test_tuning_expected_value_mutation_cannot_game_score() -> None:
    floor = HeldOutTasks(definition(), cases())
    tuning = floor.tuning
    target = tuning[0]
    assert isinstance(target.expected, dict)
    target.expected["answer"] = "attacker-output"
    with pytest.raises((FloorError, HeldOutAccessError)):
        await floor.score([target], [{"answer": "attacker-output"}])


def test_nested_tuning_mutation_does_not_change_any_stored_partition() -> None:
    records = cases()
    floor = HeldOutTasks(definition(), records)
    before_tuning = [case.model_dump() for case in floor.tuning]
    before_heldout = [case.model_dump() for case in floor._verification_cases(_VERIFIER_CAPABILITY)]
    tuning = floor.tuning
    for item in (tuning[0].task, tuning[0].expected, tuning[0].metadata):
        assert isinstance(item, dict)
        item["injected"] = {"deep": "poison"}
    for record in records:
        record.metadata["constructor-poison"] = True
    assert [case.model_dump() for case in floor.tuning] == before_tuning
    assert [
        case.model_dump() for case in floor._verification_cases(_VERIFIER_CAPABILITY)
    ] == before_heldout


def test_heldout_accessor_returns_fresh_deep_copies() -> None:
    floor = HeldOutTasks(definition(), cases())
    first = floor._verification_cases(_VERIFIER_CAPABILITY)
    snapshot = [case.model_dump() for case in first]
    first[0].metadata["poison"] = {"nested": True}
    second = floor._verification_cases(_VERIFIER_CAPABILITY)
    assert [case.model_dump() for case in second] == snapshot
    assert first[0] is not second[0]


def test_split_cannot_be_controlled_by_input_order() -> None:
    records = cases(20)
    first = HeldOutTasks(definition(), records)
    second = HeldOutTasks(definition(), list(reversed(records)))
    assert {case.case_id for case in first.tuning} == {case.case_id for case in second.tuning}
    assert {case.case_id for case in first._verification_cases(_VERIFIER_CAPABILITY)} == {
        case.case_id for case in second._verification_cases(_VERIFIER_CAPABILITY)
    }


@pytest.mark.parametrize("count", [0, 1])
def test_empty_or_unsplittable_task_dataset_refused(count: int) -> None:
    with pytest.raises(FloorError):
        HeldOutTasks(definition(), cases(count))


def test_duplicate_ids_refused_even_when_contents_differ() -> None:
    records = cases()
    records.append(records[0].model_copy(update={"expected": "other"}))
    with pytest.raises(FloorError):
        HeldOutTasks(definition(), records)


@pytest.mark.parametrize(
    "attack", ["empty", "missing-output", "extra-output", "duplicate", "foreign", "mixed-partition"]
)
async def test_score_rejects_unauthorized_or_unexecuted_measurements(attack: str) -> None:
    floor = HeldOutTasks(definition(), cases())
    tuning = list(floor.tuning)
    outputs = [case.expected for case in tuning]
    if attack == "empty":
        tuning, outputs = [], []
    elif attack == "missing-output":
        outputs.pop()
    elif attack == "extra-output":
        outputs.append(None)
    elif attack == "duplicate":
        tuning.append(tuning[0])
        outputs.append(tuning[0].expected)
    elif attack == "foreign":
        tuning[0] = tuning[0].model_copy(update={"case_id": "not-in-this-floor"})
    else:
        heldout = floor._verification_cases(_VERIFIER_CAPABILITY)
        tuning.append(heldout[0])
        outputs.append(heldout[0].expected)
    with pytest.raises((FloorError, HeldOutAccessError)):
        await floor.score(tuning, outputs)


def test_heldout_secrets_absent_from_public_representation() -> None:
    floor = HeldOutTasks(definition(), cases())
    attributes = vars(floor) if hasattr(floor, "__dict__") else {}
    public = {name: value for name, value in attributes.items() if not name.startswith("_")}
    exposed = repr(floor) + repr(public)
    dump = getattr(floor, "model_dump", None)
    if callable(dump):
        exposed += repr(dump())
    for case in floor._verification_cases(_VERIFIER_CAPABILITY):
        assert isinstance(case.task, dict)
        nested = case.task["nested"]
        assert isinstance(nested, dict)
        secret = nested["secret"]
        assert isinstance(secret, str)
        assert secret not in exposed


@pytest.mark.parametrize("changed", [False, True])
async def test_edit_floor_scores_candidate_not_historical_change_flag(changed: bool) -> None:
    floor = HumanEditRate(definition(FloorKind.HUMAN_EDIT_RATE, minimum=30), edits(changed=changed))
    tuning = floor.tuning
    assert await floor.score(tuning, ["shipped"] * len(tuning)) == 0
    assert await floor.score(tuning, ["new-wrong-output"] * len(tuning)) == 1
    assert floor.passes(0)
    assert not floor.passes(1)


@pytest.mark.parametrize("count", [0, 1, 29, 30, 59])
def test_edit_floor_requires_minimum_samples_in_both_partitions(count: int) -> None:
    with pytest.raises(FloorError):
        HumanEditRate(definition(FloorKind.HUMAN_EDIT_RATE, minimum=30), edits(count))


@pytest.mark.parametrize(
    ("proposed", "shipped", "changed"),
    [
        ("same", "same", True),
        ("old", "new", False),
    ],
)
def test_historic_edit_flag_must_match_actual_log_values(
    proposed: str,
    shipped: str,
    changed: bool,
) -> None:
    with pytest.raises(ValidationError):
        EditLogRow(case_id="forged", task=None, proposed=proposed, shipped=shipped, changed=changed)


def test_jsonl_edit_flag_forgery_is_refused(tmp_path: Path) -> None:
    records = [row.model_dump() for row in edits()]
    records[0]["changed"] = True
    path = tmp_path / "forged.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in records), encoding="utf-8")
    with pytest.raises((FloorError, ValidationError)):
        HumanEditRate.from_jsonl(definition(FloorKind.HUMAN_EDIT_RATE, minimum=30), path)


def judge_definition() -> FloorDefinition:
    return FloorDefinition(
        kind=FloorKind.LLM_JUDGE,
        limit=0.8,
        dataset="private-judge-cases",
        minimum_samples=2,
        rubric="Fixed grading rubric",
        judge_model="fake",
        calibration_dataset="private-calibration",
    )


def calibration() -> list[CalibrationRow]:
    return [
        CalibrationRow(
            case_id="calibration-one",
            task="private-calibration-task",
            output="private-calibration-output",
            human_score=0.9,
        )
    ]


def judge_response(text: str) -> ModelResponse:
    return ModelResponse(text=text, tokens_in=1, tokens_out=1, cost_usd=0.001, model="fake")


class RecordingFakeAdapter(FakeAdapter):
    def __init__(self, responses: list[ModelResponse]) -> None:
        super().__init__(responses=responses)
        self.prompts: list[str] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.prompts.append(request.prompt)
        return await super().complete(request)


def make_judge(
    tmp_path: Path,
    responses: list[str],
) -> tuple[LLMJudge, RecordingFakeAdapter, TelemetryStore]:
    adapter = RecordingFakeAdapter(responses=[judge_response(text) for text in responses])
    store = TelemetryStore(tmp_path / "telemetry")
    floor = LLMJudge(
        judge_definition(),
        cases(),
        calibration(),
        adapter,
        Runtime(store),
        judge_budget=Budget(cost_usd=0.1, compute_s=1),
    )
    return floor, adapter, store


@pytest.mark.parametrize("missing", ["rubric", "judge_model", "calibration_dataset"])
def test_judge_missing_fixed_configuration_is_refused(tmp_path: Path, missing: str) -> None:
    invalid = judge_definition().model_copy(update={missing: None})
    with pytest.raises((CalibrationError, FloorError)):
        LLMJudge(
            invalid,
            cases(),
            calibration(),
            FakeAdapter(),
            Runtime(TelemetryStore(tmp_path / "telemetry")),
        )


def test_judge_empty_calibration_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CalibrationError):
        LLMJudge(
            judge_definition(),
            cases(),
            [],
            FakeAdapter(),
            Runtime(TelemetryStore(tmp_path / "telemetry")),
        )


def test_judge_model_must_match_fixed_adapter(tmp_path: Path) -> None:
    invalid = judge_definition().model_copy(update={"judge_model": "different-model"})
    with pytest.raises((CalibrationError, FloorError)):
        LLMJudge(
            invalid,
            cases(),
            calibration(),
            FakeAdapter(),
            Runtime(TelemetryStore(tmp_path / "telemetry")),
        )


async def test_uncalibrated_flattering_judge_cannot_authorize_floor(tmp_path: Path) -> None:
    floor, adapter, store = make_judge(tmp_path, ["0.0", "1.0"])
    with pytest.raises(CalibrationError):
        await floor.score(floor.tuning, [case.expected for case in floor.tuning])
    assert adapter.calls == 1
    assert len(store.rows()) == 1


@pytest.mark.parametrize(
    "score", ["NaN", "Infinity", "-0.1", "1.1", "true", '"0.9"', "{}", '{"score": 0.9}', "not-json"]
)
async def test_calibration_requires_finite_json_number(tmp_path: Path, score: str) -> None:
    floor, adapter, store = make_judge(tmp_path, [score])
    with pytest.raises((CalibrationError, FloorError)):
        await floor.score(floor.tuning, [case.expected for case in floor.tuning])
    assert adapter.calls == 1
    assert len(store.rows()) == 1


async def test_judge_scores_current_outputs_and_emits_no_content(tmp_path: Path) -> None:
    floor, adapter, store = make_judge(tmp_path, ["0.9"] + ["0.0"] * 4 + ["1.0"] * 4)
    tuning = floor.tuning
    first = await floor.score(tuning, ["private-bad-output"] * len(tuning))
    second = await floor.score(tuning, ["private-good-output"] * len(tuning))
    assert first == 0
    assert second == 1
    assert "private-calibration-output" in adapter.prompts[0]
    assert all("private-bad-output" in prompt for prompt in adapter.prompts[1 : 1 + len(tuning)])
    assert all("private-good-output" in prompt for prompt in adapter.prompts[1 + len(tuning) :])
    assert adapter.calls == 1 + 2 * len(tuning)
    assert len(store.rows()) == adapter.calls
    for path in (tmp_path / "telemetry").iterdir():
        if path.is_file():
            raw = path.read_bytes()
            for secret in (
                b"private-bad-output",
                b"private-good-output",
                b"Fixed grading rubric",
                b"private-calibration-output",
                b"private-calibration-task",
            ):
                assert secret not in raw


class InvalidGameScorer:
    def __init__(self, score: float) -> None:
        self.value = score
        self.calls = 0

    async def score(self, task: Any, output: Any) -> float:
        self.calls += 1
        return self.value


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, 1.1])
async def test_game_floor_refuses_invalid_environment_scores(value: float) -> None:
    environment = InvalidGameScorer(value)
    floor = HeldOutGameScore(definition(FloorKind.HELD_OUT_GAME_SCORE), cases(), environment)
    with pytest.raises(FloorError):
        await floor.score(floor.tuning, [case.expected for case in floor.tuning])


def test_only_stage_four_production_code_can_import_verifier_token() -> None:
    package = Path(__file__).resolve().parents[2] / "architect"
    permitted = {Path("floors/_access.py"), Path("compiler/verification.py")}
    violations: list[str] = []
    for path in package.rglob("*.py"):
        relative = path.relative_to(package)
        if relative in permitted:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for imported in node.names:
                    if imported.name == "_VERIFIER_CAPABILITY" or (
                        imported.name == "*" and (node.module or "").endswith("_access")
                    ):
                        violations.append(f"{relative}:{node.lineno}")
            elif isinstance(node, ast.Attribute) and node.attr == "_VERIFIER_CAPABILITY":
                violations.append(f"{relative}:{node.lineno}")
    assert not violations, "Unauthorized verifier capability access: " + ", ".join(violations)


@pytest.mark.parametrize("credential", [1, {}, {"verified": True}, [], object])
def test_capability_type_or_shape_is_not_authority(credential: object) -> None:
    floor = HeldOutTasks(definition(), cases())
    with pytest.raises(HeldOutAccessError):
        floor._verification_cases(credential)


async def test_verifier_provenance_is_bound_to_one_floor_instance() -> None:
    first = HeldOutTasks(definition(), cases())
    second = HeldOutTasks(definition(), cases())
    authorized_for_first = first._verification_cases(_VERIFIER_CAPABILITY)
    with pytest.raises(HeldOutAccessError):
        await second.score(authorized_for_first, [case.expected for case in authorized_for_first])


async def test_mutated_authorized_heldout_case_loses_scoring_authority() -> None:
    floor = HeldOutTasks(definition(), cases())
    authorized = floor._verification_cases(_VERIFIER_CAPABILITY)
    assert isinstance(authorized[0].expected, dict)
    authorized[0].expected["answer"] = "cheat"
    with pytest.raises(HeldOutAccessError):
        await floor.score(authorized, [case.expected for case in authorized])


async def test_edit_floor_cannot_select_one_favorable_example_to_pass() -> None:
    floor = HumanEditRate(definition(FloorKind.HUMAN_EDIT_RATE, minimum=30), edits())
    with pytest.raises(FloorError):
        await floor.score(floor.tuning[:1], ["shipped"])


@pytest.mark.parametrize("kind", ["human", "game", "judge"])
async def test_all_floor_modes_deny_unissued_heldout_records(tmp_path: Path, kind: str) -> None:
    if kind == "human":
        floor: Any = HumanEditRate(definition(FloorKind.HUMAN_EDIT_RATE, minimum=30), edits())
    elif kind == "game":
        floor = HeldOutGameScore(
            definition(FloorKind.HELD_OUT_GAME_SCORE), cases(), InvalidGameScorer(1)
        )
    else:
        floor, _, _ = make_judge(tmp_path, ["0.9"] + ["1.0"] * 4)
    with pytest.raises(HeldOutAccessError):
        _ = floor.held_out
    with pytest.raises(HeldOutAccessError):
        floor._verification_cases(EqualityImpersonator())
    authorized = floor._verification_cases(_VERIFIER_CAPABILITY)
    raw = [EvaluationCase.model_validate(case.model_dump()) for case in authorized]
    with pytest.raises(HeldOutAccessError):
        await floor.score(raw, [case.expected for case in raw])


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -0.1, 1.1, True, "1"])
def test_passes_cannot_accept_non_numeric_or_out_of_range_scores(score: Any) -> None:
    floor = HeldOutTasks(definition(), cases())
    with pytest.raises(FloorError):
        floor.passes(score)


def test_floor_definition_cannot_be_rebound_after_construction() -> None:
    floor = HeldOutTasks(definition(), cases())
    changed = definition().model_copy(update={"limit": 0.0})
    mutable: Any = floor
    with pytest.raises((AttributeError, TypeError, ValidationError)):
        mutable.definition = changed
    assert floor.definition.limit == 0.8


async def test_calibration_constructor_inputs_are_deep_frozen(tmp_path: Path) -> None:
    rows = [
        CalibrationRow(
            case_id="calibration",
            task={"key": "original-task"},
            output={"key": "original-output"},
            human_score=0.9,
        )
    ]
    adapter = RecordingFakeAdapter([judge_response("0.9")] + [judge_response("1.0")] * 4)
    floor = LLMJudge(
        judge_definition(), cases(), rows, adapter, Runtime(TelemetryStore(tmp_path / "telemetry"))
    )
    assert isinstance(rows[0].output, dict)
    rows[0].output["key"] = "injected-calibration-output"
    assert isinstance(rows[0].task, dict)
    rows[0].task["key"] = "injected-calibration-task"
    rows.clear()
    await floor.score(floor.tuning, [case.expected for case in floor.tuning])
    assert "original-task" in adapter.prompts[0]
    assert "original-output" in adapter.prompts[0]
    assert "injected-calibration" not in adapter.prompts[0]


async def test_failed_calibration_cannot_be_skipped_on_retry(tmp_path: Path) -> None:
    floor, adapter, _ = make_judge(tmp_path, ["0.0"] * 8)
    for _ in range(2):
        with pytest.raises(CalibrationError):
            await floor.score(floor.tuning, [case.expected for case in floor.tuning])
    assert adapter.calls <= 2


@pytest.mark.parametrize("collision", ["duplicate-calibration", "evaluation-overlap"])
def test_calibration_ids_cannot_overlap_or_double_count(tmp_path: Path, collision: str) -> None:
    records = cases()
    rows = calibration()
    if collision == "duplicate-calibration":
        rows.append(rows[0])
    else:
        rows[0] = rows[0].model_copy(update={"case_id": records[0].case_id})
    with pytest.raises(CalibrationError):
        LLMJudge(
            judge_definition(),
            records,
            rows,
            FakeAdapter(),
            Runtime(TelemetryStore(tmp_path / "telemetry")),
        )


async def test_judge_model_cannot_change_after_successful_calibration(tmp_path: Path) -> None:
    floor, adapter, _ = make_judge(tmp_path, ["0.9"] + ["1.0"] * 4)
    assert await floor.score(floor.tuning, [case.expected for case in floor.tuning]) == 1
    before = adapter.calls
    adapter.model = "substituted-judge"
    with pytest.raises(CalibrationError):
        await floor.score(floor.tuning, [case.expected for case in floor.tuning])
    assert adapter.calls == before


async def test_disagreement_just_over_calibration_tolerance_is_refused(tmp_path: Path) -> None:
    floor, adapter, _ = make_judge(tmp_path, ["0.749"])
    with pytest.raises(CalibrationError):
        await floor.score(floor.tuning, [case.expected for case in floor.tuning])
    assert adapter.calls == 1


def test_package_does_not_reexport_verifier_capability() -> None:
    import architect.floors as floors

    assert not hasattr(floors, "_VERIFIER_CAPABILITY")
    assert "_VERIFIER_CAPABILITY" not in getattr(floors, "__all__", ())
