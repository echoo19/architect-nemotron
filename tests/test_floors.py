"""Floor replay behavior and sealed verification partition contracts."""

import json
from pathlib import Path

import pytest
from pydantic import JsonValue, ValidationError

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


def definition(kind: FloorKind = FloorKind.HELD_OUT_TASKS, **kwargs: object) -> FloorDefinition:
    return FloorDefinition.model_validate({"kind": kind, "limit": 0.5, "dataset": "test", **kwargs})


def records(count: int = 4) -> list[EvaluationCase]:
    return [
        EvaluationCase(case_id=f"case-{i:03}", task={"input": i}, expected={"answer": i})
        for i in range(count)
    ]


def edits(count: int = 60) -> list[EditLogRow]:
    return [
        EditLogRow(
            case_id=f"case-{i:03}", task={"input": i}, proposed="bad", shipped="good", changed=True
        )
        for i in range(count)
    ]


def judge_definition(**kwargs: object) -> FloorDefinition:
    return definition(
        FloorKind.LLM_JUDGE,
        **{
            "rubric": "private-rubric-canary",
            "judge_model": "fake",
            "calibration_dataset": "human-scores",
            **kwargs,
        },
    )


class RecordingAdapter(FakeAdapter):
    def __init__(self, scores: list[str]) -> None:
        super().__init__(
            responses=[
                ModelResponse(text=score, tokens_in=2, tokens_out=1, cost_usd=0, model="fake")
                for score in scores
            ]
        )
        self.prompts: list[str] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.prompts.append(request.prompt)
        return await super().complete(request)


def calibration(score: float = 0.8) -> list[CalibrationRow]:
    return [
        CalibrationRow(case_id="cal-1", task="private-cal-task", output="human", human_score=score)
    ]


def test_split_stable_and_deeply_private() -> None:
    rows = records()
    floor = HeldOutTasks(definition(), rows)
    reversed_floor = HeldOutTasks(definition(), list(reversed(rows)))
    assert floor.tuning == reversed_floor.tuning
    held = floor._verification_cases(_VERIFIER_CAPABILITY)
    assert {row.case_id for row in held}.isdisjoint(row.case_id for row in floor.tuning)
    assert len(held) == 2
    assert all(row not in repr(floor) + repr(vars(floor)) for row in ("case-001", "case-003"))
    assert "held_out" not in repr(vars(floor.tuning[0]))
    assert "_VERIFIER_CAPABILITY" not in repr(vars(floor.tuning[0]))
    assert isinstance(rows[0].task, dict)
    rows[0].task["input"] = "mutated"
    tuning = floor.tuning
    assert isinstance(tuning[0].expected, dict)
    tuning[0].expected["answer"] = "mutated"
    assert floor.tuning[0].expected == {"answer": 0}
    assert floor.tuning[0].task == {"input": 0}
    assert held == floor._verification_cases(_VERIFIER_CAPABILITY)
    assert held[0] is not floor._verification_cases(_VERIFIER_CAPABILITY)[0]
    with pytest.raises(HeldOutAccessError):
        _ = floor.held_out


@pytest.mark.parametrize("credential", [None, True, False, "verifier", "stage4", object(), 1])
def test_wrong_capability_denied(credential: object) -> None:
    with pytest.raises(HeldOutAccessError):
        HeldOutTasks(definition(), records())._verification_cases(credential)


def test_equality_spoof_cannot_grant_access() -> None:
    class Spoof:
        def __eq__(self, other: object) -> bool:
            return True

    with pytest.raises(HeldOutAccessError):
        HeldOutTasks(definition(), records())._verification_cases(Spoof())


async def test_actual_task_outputs_and_checker_order() -> None:
    floor = HeldOutTasks(definition(), records())
    assert await floor.score(floor.tuning, [row.expected for row in floor.tuning]) == 1
    assert await floor.score(floor.tuning, [None, None]) == 0
    assert await floor.score(floor.tuning, [floor.tuning[0].expected, None]) == 0.5
    held = floor._verification_cases(_VERIFIER_CAPABILITY)
    assert await floor.score(held, [row.expected for row in held]) == 1
    custom = HeldOutTasks(
        definition(), records(), checker=lambda expected, output: output == {"copy": expected}
    )
    assert await custom.score(custom.tuning, [{"copy": row.expected} for row in custom.tuning]) == 1


async def test_score_cannot_reconstruct_mutate_or_mix_heldout() -> None:
    floor = HeldOutTasks(definition(), records())
    held = floor._verification_cases(_VERIFIER_CAPABILITY)
    for cases in [
        [held[0].model_copy(deep=True)],
        [EvaluationCase.model_validate_json(held[0].model_dump_json())],
        [records()[1]],
        [held[0], floor.tuning[0]],
        [EvaluationCase(case_id="unknown", task=None, expected=None)],
    ]:
        with pytest.raises(HeldOutAccessError):
            await floor.score(cases, [row.expected for row in cases])
    assert isinstance(held[0].expected, dict)
    held[0].expected["answer"] = "spoof"
    with pytest.raises(HeldOutAccessError):
        await floor.score([held[0]], [held[0].expected])
    fresh = floor._verification_cases(_VERIFIER_CAPABILITY)
    assert fresh[0].expected == {"answer": 1}


async def test_verification_provenance_is_evaluator_scoped() -> None:
    one, two = HeldOutTasks(definition(), records()), HeldOutTasks(definition(), records())
    cases = one._verification_cases(_VERIFIER_CAPABILITY)
    with pytest.raises(HeldOutAccessError):
        await two.score(cases, [case.expected for case in cases])


@pytest.mark.parametrize("count", [0, 1])
def test_small_datasets_refused(count: int) -> None:
    with pytest.raises(FloorError):
        HeldOutTasks(definition(), records(count))


def test_duplicate_ids_refused_even_if_content_differs() -> None:
    rows = records()
    rows.append(rows[0].model_copy(update={"task": "different"}))
    with pytest.raises(FloorError):
        HeldOutTasks(definition(), rows)


async def test_invalid_scoring_shapes() -> None:
    floor = HeldOutTasks(definition(), records())
    for cases, outputs in [([], []), (floor.tuning, []), ([floor.tuning[0]] * 2, [None] * 2)]:
        with pytest.raises(FloorError):
            await floor.score(cases, outputs)
    with pytest.raises(FloorError):
        await floor.score(floor.tuning, [float("nan")] * 2)


@pytest.mark.parametrize("kind", list(FloorKind))
def test_kind_and_boundary_rules(kind: FloorKind) -> None:
    floor = HumanEditRate(definition(kind), edits()) if kind == FloorKind.HUMAN_EDIT_RATE else None
    if floor is None:
        floor = HeldOutTasks(definition(), records())
    assert floor.passes(0.5)
    assert floor.passes(0.49) == (kind == FloorKind.HUMAN_EDIT_RATE)
    assert floor.passes(0.51) == (kind != FloorKind.HUMAN_EDIT_RATE)
    for score in [float("nan"), float("inf"), -0.01, 1.01, True]:
        with pytest.raises(FloorError):
            floor.passes(score)


@pytest.mark.parametrize("count", [0, 1, 29, 30, 59])
def test_edit_minimum_on_both_partitions(count: int) -> None:
    with pytest.raises(FloorError):
        HumanEditRate(definition(FloorKind.HUMAN_EDIT_RATE), edits(count))


async def test_edit_replay_not_historic_changed_and_subset_minimum() -> None:
    floor = HumanEditRate(definition(FloorKind.HUMAN_EDIT_RATE), edits())
    assert len(floor.tuning) == 30
    assert await floor.score(floor.tuning, ["good"] * 30) == 0
    assert await floor.score(floor.tuning, ["bad"] * 30) == 1
    assert await floor.score(floor.tuning, ["good"] * 15 + ["bad"] * 15) == 0.5
    with pytest.raises(FloorError):
        await floor.score(floor.tuning[:29], ["good"] * 29)
    held = floor._verification_cases(_VERIFIER_CAPABILITY)
    assert await floor.score(held, ["good"] * 30) == 0


async def test_edit_custom_minimum_and_unchanged_history() -> None:
    rows = [row.model_copy(update={"proposed": "good", "changed": False}) for row in edits(4)]
    floor = HumanEditRate(definition(FloorKind.HUMAN_EDIT_RATE, minimum_samples=2), rows)
    assert await floor.score(floor.tuning, ["bad"] * 2) == 1


def test_edit_jsonl_roundtrip_and_errors(tmp_path: Path) -> None:
    path = tmp_path / "edits.jsonl"
    path.write_text("\n".join(row.model_dump_json() for row in edits()) + "\n\n", encoding="utf-8")
    floor = HumanEditRate.from_jsonl(definition(FloorKind.HUMAN_EDIT_RATE), path)
    assert len(floor.tuning) == 30
    for text in ["bad json", "{}", "[]", '{"changed":true}', "", edits()[0].model_dump_json()]:
        path.write_text(text, encoding="utf-8")
        with pytest.raises(FloorError):
            HumanEditRate.from_jsonl(definition(FloorKind.HUMAN_EDIT_RATE), path)
    path.unlink()
    with pytest.raises(FloorError):
        HumanEditRate.from_jsonl(definition(FloorKind.HUMAN_EDIT_RATE), path)
    with pytest.raises(ValidationError):
        EditLogRow(case_id="a", task=None, proposed=1, shipped=1, changed=True)
    with pytest.raises(ValidationError):
        EditLogRow(case_id="a", task=None, proposed=1, shipped=2, changed=False)


class Game:
    def __init__(self, fixed: float | None = None) -> None:
        self.fixed = fixed
        self.seen: list[JsonValue] = []

    async def score(self, task: JsonValue, output: JsonValue) -> float:
        self.seen.append(task)
        return self.fixed if self.fixed is not None else float(output == "win")


async def test_game_only_selected_tasks_and_candidate_scores() -> None:
    game = Game()
    floor = HeldOutGameScore(definition(FloorKind.HELD_OUT_GAME_SCORE), records(), game)
    assert await floor.score(floor.tuning, ["win", "lose"]) == 0.5
    assert game.seen == [row.task for row in floor.tuning]
    assert await floor.score(floor.tuning, ["lose", "lose"]) == 0
    held = floor._verification_cases(_VERIFIER_CAPABILITY)
    assert await floor.score(held, ["win", "win"]) == 1


@pytest.mark.parametrize("score", [-1.0, 1.1, float("nan"), float("inf"), True])
async def test_game_invalid_scores_refused(score: float) -> None:
    floor = HeldOutGameScore(definition(FloorKind.HELD_OUT_GAME_SCORE), records(), Game(score))
    with pytest.raises(FloorError):
        await floor.score(floor.tuning, ["win", "win"])


@pytest.mark.parametrize("field", ["rubric", "judge_model", "calibration_dataset"])
def test_judge_missing_fixed_configuration(field: str, tmp_path: Path) -> None:
    with TelemetryStore(tmp_path) as store, pytest.raises(CalibrationError):
        LLMJudge(
            judge_definition(**{field: None}),
            records(),
            calibration(),
            FakeAdapter(),
            Runtime(store),
        )


def test_judge_missing_calibration_mismatch_and_overlap(tmp_path: Path) -> None:
    with TelemetryStore(tmp_path) as store:
        runtime = Runtime(store)
        for rows in [
            [],
            calibration() * 2,
            [calibration()[0].model_copy(update={"case_id": "case-000"})],
        ]:
            with pytest.raises(CalibrationError):
                LLMJudge(judge_definition(), records(), rows, FakeAdapter(), runtime)
        with pytest.raises(CalibrationError):
            LLMJudge(
                judge_definition(judge_model="other"),
                records(),
                calibration(),
                FakeAdapter(),
                runtime,
            )
        for tolerance in [-0.1, 1.1, float("nan"), float("inf")]:
            with pytest.raises(CalibrationError):
                LLMJudge(
                    judge_definition(),
                    records(),
                    calibration(),
                    FakeAdapter(),
                    runtime,
                    calibration_tolerance=tolerance,
                )


async def test_judge_candidate_dependence_calibrates_once_and_private_telemetry(
    tmp_path: Path,
) -> None:
    adapter = RecordingAdapter(["0.8", "0.9", "0.3", "0", "0"])
    with TelemetryStore(tmp_path) as store:
        floor = LLMJudge(judge_definition(), records(), calibration(), adapter, Runtime(store))
        assert await floor.score(floor.tuning, ["candidate-secret-a", "candidate-secret-b"]) == 0.6
        assert await floor.score(floor.tuning, [None, None]) == 0
        assert adapter.calls == 5
        assert "private-cal-task" in adapter.prompts[0]
        assert "candidate-secret-a" in adapter.prompts[1]
        assert "candidate-secret-b" in adapter.prompts[2]
        assert '"expected"' not in adapter.prompts[1]
    text = (tmp_path / "runs.jsonl").read_text(encoding="utf-8")
    for canary in [
        "private-rubric-canary",
        "private-cal-task",
        "candidate-secret-a",
        "candidate-secret-b",
    ]:
        assert canary not in text
    assert len(text.splitlines()) == 5
    assert all(json.loads(line)["model"] == "fake" for line in text.splitlines())


@pytest.mark.parametrize("raw", ["0.0", "NaN", "true", '"0.8"', '{"score":0.8}', "1.1", "bad"])
async def test_judge_calibration_refuses_bad_scores_permanently(raw: str, tmp_path: Path) -> None:
    adapter = RecordingAdapter([raw, "0.8", "1", "1"])
    with TelemetryStore(tmp_path) as store:
        floor = LLMJudge(judge_definition(), records(), calibration(), adapter, Runtime(store))
        for _ in range(2):
            with pytest.raises(CalibrationError):
                await floor.score(floor.tuning, [None, None])
        assert adapter.calls == 1


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "true", '"1"', "-0.01", "1.01", "{}"])
async def test_judge_invalid_candidate_score(raw: str, tmp_path: Path) -> None:
    adapter = RecordingAdapter(["0.8", raw])
    with TelemetryStore(tmp_path) as store:
        floor = LLMJudge(judge_definition(), records(), calibration(), adapter, Runtime(store))
        with pytest.raises(FloorError):
            await floor.score(floor.tuning, [None, None])


async def test_calibration_mae_boundary_and_snapshot_immutability(tmp_path: Path) -> None:
    adapter = RecordingAdapter(["0.65", "0.5", "1"])
    rows = calibration()
    rows[0] = rows[0].model_copy(update={"task": {"key": "sealed"}})
    with TelemetryStore(tmp_path) as store:
        floor = LLMJudge(judge_definition(), records(), rows, adapter, Runtime(store))
        assert isinstance(rows[0].task, dict)
        rows[0].task["key"] = "modified"
        assert await floor.score(floor.tuning, [None, None]) == 0.75
        assert "sealed" in adapter.prompts[0] and "modified" not in adapter.prompts[0]
        with pytest.raises(AttributeError):
            floor.definition = judge_definition(rubric="changed")  # type: ignore[misc]
        adapter.model = "other"
        with pytest.raises(CalibrationError):
            await floor.score(floor.tuning, [None, None])


async def test_judge_budget_failure_logged(tmp_path: Path) -> None:
    adapter = FakeAdapter(
        responses=[
            ModelResponse(
                text="0.8",
                tokens_in=2,
                tokens_out=1,
                cost_usd=0.2,
                model="fake",
            )
        ]
    )
    with TelemetryStore(tmp_path) as store:
        floor = LLMJudge(
            judge_definition(),
            records(),
            calibration(),
            adapter,
            Runtime(store),
            judge_budget=Budget(cost_usd=0.1),
        )
        with pytest.raises(CalibrationError):
            await floor.score(floor.tuning, [None, None])
    assert adapter.calls == 0
    assert json.loads((tmp_path / "runs.jsonl").read_text(encoding="utf-8"))["accepted"] is False


async def test_judge_rejects_heldout_before_calibration(tmp_path: Path) -> None:
    adapter = RecordingAdapter(["0.8"])
    with TelemetryStore(tmp_path) as store:
        floor = LLMJudge(judge_definition(), records(), calibration(), adapter, Runtime(store))
        with pytest.raises(HeldOutAccessError):
            await floor.score([records()[1]], [None])
        assert adapter.calls == 0


async def test_corrupt_case_json_denied() -> None:
    floor = HeldOutTasks(definition(), records())
    case = floor.tuning[0]
    assert isinstance(case.expected, dict)
    case.expected["answer"] = float("nan")
    with pytest.raises(HeldOutAccessError):
        await floor.score([case], [None])


async def test_invalid_checker_result_refused() -> None:
    def checker(expected: JsonValue, output: JsonValue) -> bool:
        return 1  # type: ignore[return-value]

    floor = HeldOutTasks(definition(), records(), checker=checker)
    with pytest.raises(FloorError):
        await floor.score(floor.tuning, [None, None])


def test_wrong_kind_and_corrupted_edit_row_refused() -> None:
    with pytest.raises(FloorError):
        HeldOutTasks(definition(FloorKind.HUMAN_EDIT_RATE), records())
    rows = edits()
    rows[0] = rows[0].model_copy(update={"changed": False})
    with pytest.raises(FloorError):
        HumanEditRate(definition(FloorKind.HUMAN_EDIT_RATE), rows)


def test_huge_numeric_score_refused_without_overflow() -> None:
    floor = HeldOutTasks(definition(), records())
    with pytest.raises(FloorError):
        floor.passes(10**1000)


# --- Phase 8: closed trusted checkers -------------------------------------------------

from copy import deepcopy  # noqa: E402
from typing import get_args  # noqa: E402

from architect.floors.checkers import (  # noqa: E402
    CHECKERS,
    CheckerName,
    FieldReport,
    exam_extraction_v1,
    field_report,
    invoice_extraction_v1,
    similar,
)

EXAM: dict[str, JsonValue] = {
    "page": 1,
    "answer_key_page": False,
    "questions": [
        {
            "number": 1,
            "stem": "abcdefghij",
            "choices": [{"label": "A", "text": "alpha"}, {"label": "B", "text": "bravo"}],
            "answer_key": "B",
        },
        {
            "number": 2,
            "stem": "What is 2 + 2?",
            "choices": [{"label": "A", "text": "3"}, {"label": "B", "text": "4"}],
            "answer_key": "A",
        },
    ],
}
INVOICE: dict[str, JsonValue] = {
    "vendor": "Northwind Traders",
    "date": "2026-03-04",
    "total": 45.5,
    "line_items": [
        {"description": "Paper reams", "quantity": 3, "unit_price": 10.5},
        {"description": "Stapler", "quantity": 1, "unit_price": 14.0},
    ],
}


def exam(**question_updates: JsonValue) -> dict[str, JsonValue]:
    value = deepcopy(EXAM)
    questions = value["questions"]
    assert isinstance(questions, list) and isinstance(questions[0], dict)
    questions[0].update(question_updates)
    return value


def invoice(**updates: JsonValue) -> dict[str, JsonValue]:
    return deepcopy(INVOICE) | updates


def test_checker_table_is_closed_and_complete() -> None:
    assert set(CHECKERS) == set(get_args(CheckerName))
    assert CHECKERS["equality"]({"a": 1}, {"a": 1})
    assert not CHECKERS["equality"]({"a": 1}, {"a": 2})
    with pytest.raises(TypeError):
        CHECKERS["equality"] = exam_extraction_v1  # type: ignore[index]


def test_similarity_collapses_whitespace_and_case() -> None:
    assert similar("  Hello   World ", "hello world") == 1.0
    assert similar("", "") == 1.0
    assert similar("abcdefghij", "abcdefghiX") == pytest.approx(0.9)
    assert similar("abcdefghij", "abcdefghXY") == pytest.approx(0.8)


def test_exam_checker_accepts_exact_and_reordered_output() -> None:
    assert exam_extraction_v1(EXAM, deepcopy(EXAM))
    reordered = deepcopy(EXAM)
    questions = reordered["questions"]
    assert isinstance(questions, list)
    questions.reverse()
    assert exam_extraction_v1(EXAM, reordered)
    key_page: dict[str, JsonValue] = {"page": 3, "answer_key_page": True, "questions": []}
    assert exam_extraction_v1(key_page, {"questions": [], "answer_key_page": True})
    assert not exam_extraction_v1(key_page, {"questions": [], "answer_key_page": False})


def test_exam_checker_similarity_boundary_is_inclusive() -> None:
    assert exam_extraction_v1(EXAM, exam(stem="abcdefghiX"))
    assert not exam_extraction_v1(EXAM, exam(stem="abcdefghXY"))
    near = [{"label": "A", "text": "alpha"}, {"label": "B", "text": "brava"}]
    assert similar("bravo", "brava") == pytest.approx(0.8)
    assert not exam_extraction_v1(EXAM, exam(choices=near))


def test_exam_checker_label_case_and_count_mismatch() -> None:
    lower = [{"label": "a", "text": "alpha"}, {"label": " b ", "text": "bravo"}]
    assert exam_extraction_v1(EXAM, exam(answer_key="b", choices=lower))
    assert not exam_extraction_v1(EXAM, exam(answer_key="C"))
    assert not exam_extraction_v1(EXAM, exam(choices=[{"label": "A", "text": "alpha"}]))
    extra = deepcopy(EXAM)
    questions = extra["questions"]
    assert isinstance(questions, list) and isinstance(questions[1], dict)
    questions.append(deepcopy(questions[1]) | {"number": 3})
    assert not exam_extraction_v1(EXAM, extra)
    duplicate = deepcopy(EXAM)
    questions = duplicate["questions"]
    assert isinstance(questions, list) and isinstance(questions[1], dict)
    questions[1]["number"] = 1
    assert not exam_extraction_v1(EXAM, duplicate)
    assert not exam_extraction_v1(EXAM, deepcopy(EXAM) | {"answer_key_page": True})


@pytest.mark.parametrize(
    "output",
    [None, [], "questions", {"questions": None}, {"questions": [None, 1]}, {"answer_key_page": 0}],
)
def test_malformed_candidate_output_fails_without_raising(output: JsonValue) -> None:
    assert exam_extraction_v1(EXAM, output) is False
    assert invoice_extraction_v1(INVOICE, output) is False


@pytest.mark.parametrize(
    "expected",
    [None, {"questions": []}, {"answer_key_page": True, "questions": [{"number": "1"}]}],
)
def test_malformed_trusted_expected_value_raises(expected: JsonValue) -> None:
    with pytest.raises(FloorError):
        exam_extraction_v1(expected, deepcopy(EXAM))


def test_invoice_checker_rules() -> None:
    assert invoice_extraction_v1(INVOICE, deepcopy(INVOICE))
    assert invoice_extraction_v1(INVOICE, invoice(vendor="northwind  TRADERS"))
    assert invoice_extraction_v1(INVOICE, invoice(total=45.504))
    assert not invoice_extraction_v1(INVOICE, invoice(total=45.506))
    assert not invoice_extraction_v1(INVOICE, invoice(date="2026-03-05"))
    assert not invoice_extraction_v1(INVOICE, invoice(total=True))
    items = deepcopy(INVOICE["line_items"])
    assert isinstance(items, list) and isinstance(items[0], dict)
    items[0]["quantity"] = 4
    assert not invoice_extraction_v1(INVOICE, invoice(line_items=items))
    assert not invoice_extraction_v1(INVOICE, invoice(line_items=deepcopy(items[:1])))


def test_invoice_checker_requires_expected_nulls_to_stay_null() -> None:
    missing = invoice(vendor=None)
    assert invoice_extraction_v1(missing, deepcopy(missing))
    assert not invoice_extraction_v1(missing, deepcopy(INVOICE))  # an invented vendor
    absent = deepcopy(missing)
    del absent["vendor"]
    assert not invoice_extraction_v1(missing, absent)
    assert not invoice_extraction_v1(INVOICE, deepcopy(missing))


def test_field_report_counts_only() -> None:
    report = field_report(EXAM, exam(answer_key="C"), checker="exam_extraction_v1")
    assert isinstance(report, FieldReport)
    # question_count + answer_key_page + 2 x (answer_key, stem, choice_count, 2 choice texts)
    assert (report.fields, report.matched) == (12, 11)
    assert report.per_field["answer_key"] == (1, 2)
    assert report.per_field["choice_text"] == (4, 4)
    serialized = report.model_dump_json()
    assert "abcdefghij" not in serialized and "alpha" not in serialized
    empty = field_report(INVOICE, {}, checker="invoice_extraction_v1")
    assert (empty.fields, empty.matched) == (10, 0)
    assert field_report(1, 1, checker="equality").per_field == {"output": (1, 1)}
    with pytest.raises(ValidationError):
        FieldReport.model_validate({"fields": 1, "matched": 1, "per_field": {}, "extra": 1})


async def test_held_out_tasks_scores_with_a_table_checker() -> None:
    rows = [
        EvaluationCase(case_id=f"page-{i}", task={"page": i}, expected=deepcopy(EXAM))
        for i in range(4)
    ]
    floor = HeldOutTasks(definition(), rows, CHECKERS["exam_extraction_v1"])
    outputs: list[JsonValue] = [exam(stem="abcdefghiX"), exam(answer_key="D")]
    assert await floor.score(floor.tuning, outputs) == 0.5


# ------------------------------------------------------------------ retake cases


def extra(count: int = 5, start: int = 100) -> list[EvaluationCase]:
    return [
        EvaluationCase(case_id=f"extra-{i:03}", task={"input": i}, expected={"answer": i})
        for i in range(start, start + count)
    ]


def retakeable(count: int = 5) -> tuple[HeldOutTasks, list[int]]:
    """A 4-case floor (2 practice, 2 exam) with ``count`` extra exam cases, and how often
    the extra cases were read."""
    reads: list[int] = []

    def source() -> list[EvaluationCase]:
        reads.append(1)
        return extra(count)

    floor = HeldOutTasks(definition(), records())
    floor._allow_retakes(source)
    return floor, reads


def test_a_floor_refuses_retakes_until_it_is_told_to_allow_them() -> None:
    floor = HeldOutTasks(definition(), records())
    assert not floor.retakes_allowed and floor.exam_size == 2
    with pytest.raises(HeldOutAccessError):
        floor._retake_cases(_VERIFIER_CAPABILITY, (), 2)
    with pytest.raises(HeldOutAccessError):
        floor.retake_plan(())
    floor._allow_retakes()
    assert floor.retakes_allowed
    # Allowed, but no extra cases: every retake is the same exam.
    assert floor._retake_cases(_VERIFIER_CAPABILITY, (), 2) is None
    assert floor.retake_plan(()) == ("same_exam", 0)


@pytest.mark.parametrize("credential", [None, True, "verifier", object()])
def test_retake_cases_need_the_verifier_capability(credential: object) -> None:
    floor, reads = retakeable()
    with pytest.raises(HeldOutAccessError):
        floor._retake_cases(credential, (), 2)
    assert reads == []


def test_retake_cases_are_unused_in_a_fixed_order_and_never_the_floors_own() -> None:
    floor, reads = retakeable(5)
    assert reads == []  # setting a floor up reads no extra case
    own = {row.case_id for row in floor.tuning} | {
        row.case_id for row in floor._verification_cases(_VERIFIER_CAPABILITY)
    }
    assert reads == []  # nor does the exam or the practice half
    assert floor.retake_plan(()) == ("unused", 5)
    first = floor._retake_cases(_VERIFIER_CAPABILITY, (), 2)
    assert first is not None and len(first) == 2 and reads == [1]
    used = {row.case_id for row in first}
    assert used.isdisjoint(own)
    again, _ = retakeable(5)  # the order is fixed: another evaluator issues the same two
    assert [row.case_id for row in again._retake_cases(_VERIFIER_CAPABILITY, (), 2) or ()] == [
        row.case_id for row in first
    ]
    second = floor._retake_cases(_VERIFIER_CAPABILITY, used, 2)
    assert second is not None and {row.case_id for row in second}.isdisjoint(used | own)
    used |= {row.case_id for row in second}
    # One case is left, fewer than an exam: the next retake is the same exam.
    assert floor.retake_plan(used) == ("same_exam", 1)
    assert floor._retake_cases(_VERIFIER_CAPABILITY, used, 2) is None
    assert reads == [1]  # read once


async def test_issued_retake_cases_score_as_exam_cases_and_copies_do_not() -> None:
    floor, _ = retakeable()
    cases = floor._retake_cases(_VERIFIER_CAPABILITY, (), 2)
    assert cases is not None
    assert await floor.score(cases, [case.expected for case in cases]) == 1
    with pytest.raises(HeldOutAccessError):
        await floor.score([case.model_copy(deep=True) for case in cases], [None, None])
    with pytest.raises(HeldOutAccessError):
        await floor.score([cases[0], floor.tuning[0]], [None, None])


def test_retake_cases_that_repeat_the_floors_own_are_refused() -> None:
    for rows in (records(1), [*extra(2), *extra(1)]):
        floor = HeldOutTasks(definition(), records())
        floor._allow_retakes(lambda rows=rows: rows)
        with pytest.raises(FloorError):
            floor.retake_plan(())
    # The same content under a new id is not an unused case either.
    renamed = records()[1].model_copy(update={"case_id": "other-id"})
    floor = HeldOutTasks(definition(), records())
    floor._allow_retakes(lambda: [renamed])
    with pytest.raises(FloorError):
        floor._retake_cases(_VERIFIER_CAPABILITY, (), 1)


def test_a_profile_floor_allows_retakes_only_when_its_entry_says_so(
    tmp_path: Path,
) -> None:
    from architect.profiles import FloorConfig, build_services, load_profile

    plain = FloorConfig.model_validate({"kind": "held_out_tasks", "path": "c.json"})
    published = FloorConfig.model_validate(
        {"kind": "held_out_tasks", "path": "c.json", "manifest": "m.json"}
    )
    # Refused by default, with or without the manifest line: a copy of a profile that
    # lost that line must not open an evidence exam to a changed design.
    assert not plain.retakes_allowed and not published.retakes_allowed
    assert published.model_copy(update={"retakes": True}).retakes_allowed
    assert plain.model_copy(update={"retakes": True}).retakes_allowed
    assert not plain.model_copy(update={"retakes": False}).retakes_allowed
    with pytest.raises(ValidationError):
        FloorConfig.model_validate(
            {"kind": "human_edit_rate", "path": "c.jsonl", "retake_cases": "r.json"}
        )

    rows = [row.model_dump(mode="json") for row in records()]
    (tmp_path / "cases.json").write_text(json.dumps(rows), encoding="utf-8")
    (tmp_path / "retake.json").write_text(
        json.dumps([row.model_dump(mode="json") for row in extra(3)]), encoding="utf-8"
    )
    (tmp_path / "profile.json").write_text(
        json.dumps(
            {
                "workspace": "work",
                "models": {"fake": {"provider": "fake"}},
                "floors": {
                    "open": {"kind": "held_out_tasks", "path": "cases.json",
                             "retake_cases": "retake.json", "retakes": True},
                    "unsaid": {"kind": "held_out_tasks", "path": "cases.json",
                               "retake_cases": "retake.json"},
                    "published": {"kind": "held_out_tasks", "path": "cases.json",
                                  "manifest": "missing.json", "retake_cases": "retake.json"},
                    "broken": {"kind": "held_out_tasks", "path": "cases.json",
                               "retake_cases": "nowhere.json", "retakes": True},
                },
            }
        ),
        encoding="utf-8",
    )
    services = build_services(load_profile(tmp_path / "profile.json"), Path("work"))
    try:
        factory = services.compiler.floor_factory
        opened = factory(definition(dataset="open"))
        assert opened.retakes_allowed and opened.retake_plan(()) == ("unused", 3)
        for dataset in ("published", "unsaid"):
            closed = factory(definition(dataset=dataset))
            assert not closed.retakes_allowed
            with pytest.raises(HeldOutAccessError):
                closed._retake_cases(_VERIFIER_CAPABILITY, (), 2)
        # A missing file of extra cases breaks no build: only a retake reads it.
        broken = factory(definition(dataset="broken"))
        assert len(broken.tuning) == 2
        with pytest.raises(FloorError):
            broken.retake_plan(())
        # A coder may read neither the extra cases nor what Undo and the record keep.
        denied = set(services.profile.pi.denied_paths)
        assert (tmp_path / "retake.json").resolve() in denied
        assert {services.workspace / "passed", services.workspace / "retakes"} <= denied
    finally:
        services.close()
