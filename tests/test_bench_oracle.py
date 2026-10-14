"""Deterministic oracle adapters for the fake provider (Phase 8 WP3)."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import JsonValue

from architect.bench.compare import mcnemar_exact
from architect.bench.oracle import (
    TEXT_TOKENS_IN,
    TEXT_TOKENS_OUT,
    NoisyOracle,
    OracleAdapter,
    OracleState,
    held_out_flips,
    lookup_key,
    oracle_factory,
    tuning_flips,
)
from architect.bench.pages import (
    EXAM_OUTPUT_SCHEMA,
    STAGE1_OUTPUT_SCHEMA,
    digitizer_expected,
    generate_pages,
    stage1_output,
)
from architect.bench.suite import SUITES, BenchTask
from architect.bench.text_tasks import canonical_key
from architect.floors import EvaluationCase, HeldOutTasks
from architect.floors.checkers import CHECKERS
from architect.models import ModelRequest
from architect.models.images import ImageInput
from architect.presets import get_preset
from architect.runtime import RunContext
from architect.runtime.pipeline import run_pipeline

TASKS = {task.id: task for task in SUITES["v1"]}


def _prompt(task_input: JsonValue, schema: JsonValue, **extra: JsonValue) -> str:
    """The extractor's prompt shape: one JSON object with input and output_schema."""
    body: dict[str, JsonValue] = {
        "instruction": "Extract facts from the input. Do not invent missing values.",
        "task_description": "bench",
        "input": task_input,
        "output_schema": schema,
    }
    body.update(extra)
    return json.dumps(body, sort_keys=True)


def _page_request(image: bytes, schema: JsonValue = EXAM_OUTPUT_SCHEMA) -> ModelRequest:
    stub: JsonValue = {"synthetic": True, "page": 1, "images": [{"$image": {"media_type": "x"}}]}
    return ModelRequest(prompt=_prompt(stub, schema), images=(ImageInput.from_bytes(image),))


async def _ask(adapter: OracleAdapter, request: ModelRequest) -> JsonValue:
    response = await adapter.complete(request)
    parsed: JsonValue = json.loads(response.text)
    return parsed


async def test_vision_answers_by_sha_of_delivered_bytes() -> None:
    rendered = generate_pages(7, 3, "clean", key_page=False)
    state = OracleState(vision={p.sha256: p.truth.model_dump(mode="json") for p in rendered})
    adapter = OracleAdapter("fake", state)
    for page in rendered:
        answer = await _ask(adapter, _page_request(page.image))
        assert answer == page.truth.model_dump(mode="json")
    # The prompt input is irrelevant: the same stub prompt yields each page's own truth.
    assert adapter.calls == 3
    assert [shape.sha256s for shape in adapter.seen] == [(p.sha256,) for p in rendered]
    assert all(shape.image_count == 1 for shape in adapter.seen)


async def test_wrong_bytes_get_a_schema_valid_wrong_answer() -> None:
    rendered = generate_pages(7, 2, "clean", key_page=False)
    state = OracleState(vision={rendered[0].sha256: rendered[0].truth.model_dump(mode="json")})
    adapter = OracleAdapter("fake", state)
    answer = await _ask(adapter, _page_request(rendered[1].image))  # not in the table
    Draft202012Validator(EXAM_OUTPUT_SCHEMA).validate(answer)
    assert answer != rendered[1].truth.model_dump(mode="json")
    assert answer != rendered[0].truth.model_dump(mode="json")
    # A text-only request for a vision task (image dropped on the way) also fails loudly.
    text_only = ModelRequest(prompt=_prompt({"synthetic": True}, EXAM_OUTPUT_SCHEMA))
    assert await _ask(adapter, text_only) == {"page": 0, "questions": [], "answer_key_page": False}


async def test_oracle_never_reads_expected_values() -> None:
    cases, state = TASKS["exam-clean-40"].dataset(7, 2)
    floor_copy = deepcopy(cases)  # what a floor factory receives
    rendered = generate_pages(7, 2, "clean", key_page=False)
    adapter = OracleAdapter("fake", state)
    before = await _ask(adapter, _page_request(rendered[0].image))
    for case in (*cases, *floor_copy):
        assert isinstance(case.expected, dict)
        case.expected["questions"] = []
        case.expected["page"] = 99
    after = await _ask(adapter, _page_request(rendered[0].image))
    assert after == before == rendered[0].truth.model_dump(mode="json")


async def test_text_answers_by_canonical_input_through_the_real_extractor() -> None:
    task = TASKS["tickets-40"]
    cases, state = task.dataset(7, 8)
    spec = task.spec("fake", "default")
    adapter = OracleAdapter("fake", state)
    for params in ({"verifier_depth": 0, "retries": 0}, {"verifier_depth": 1, "retries": 1}):
        harness = get_preset("extractor").build(spec, params)
        for case in cases:
            output = await harness(RunContext(adapter, spec.budget), case.task)
            assert output == case.expected
    # Self-check calls (verifier_depth 1) passed without a second extraction.
    assert adapter.calls == 8 + 16


async def test_invoice_answers_through_the_real_extractor() -> None:
    task = TASKS["invoice-40"]
    cases, state = task.dataset(5, 6)
    spec = task.spec("fake", "default")
    harness = get_preset("extractor").build(spec, {"verifier_depth": 0, "retries": 0})
    adapter = OracleAdapter("fake", state)
    for case in cases:
        assert await harness(RunContext(adapter, spec.budget), case.task) == case.expected


async def test_unknown_text_input_is_wrong_and_valid() -> None:
    adapter = OracleAdapter("fake", OracleState())
    schema = TASKS["invoice-40"].spec("fake", "default").output_schema
    answer = await _ask(adapter, ModelRequest(prompt=_prompt({"invoice_text": "x"}, schema)))
    Draft202012Validator(schema).validate(answer)
    assert answer == {"vendor": "", "date": "", "total": 0.0, "line_items": []}
    assert await _ask(adapter, ModelRequest(prompt="not json")) == {
        "passed": False,
        "reason": "oracle could not parse the prompt",
    }
    assert await _ask(adapter, ModelRequest(prompt="[1, 2]")) is None


async def test_verify_candidate_passes_and_other_operations_are_minimal() -> None:
    adapter = OracleAdapter("fake", OracleState())
    verdict_schema: JsonValue = {
        "type": "object",
        "required": ["passed", "reason"],
        "properties": {"passed": {"type": "boolean"}, "reason": {"type": "string"}},
    }
    verify = _prompt({"x": 1}, verdict_schema, operation="verify_candidate", candidate={})
    answer = await _ask(adapter, ModelRequest(prompt=verify))
    assert isinstance(answer, dict) and answer["passed"] is True
    probe = _prompt({"x": 1}, verdict_schema, operation="probe_analysis")
    assert await _ask(adapter, ModelRequest(prompt=probe)) == {"passed": False, "reason": ""}


async def test_digitizer_stages_derive_from_what_they_receive() -> None:
    rendered = generate_pages(2, 2, "clean", key_page=True)
    stage1 = stage1_output(rendered)
    state = OracleState(digitizer={rendered[0].sha256: stage1})
    adapter = OracleAdapter("fake", state)
    first = await _ask(adapter, _page_request(rendered[0].image, STAGE1_OUTPUT_SCHEMA))
    assert first == stage1
    solver_schema = TASKS["digitizer-24"].spec("fake", "default").pipeline[1].output_schema
    envelope: JsonValue = {
        "original": {"synthetic": True},
        "previous": first,
        "steps": {"vision-extract": first},
    }
    solver = await _ask(adapter, ModelRequest(prompt=_prompt(envelope, solver_schema)))
    expected = digitizer_expected(rendered)
    assert isinstance(expected, dict)
    assert solver == {"answer_keys": expected["answer_keys"]}
    final_schema = TASKS["digitizer-24"].spec("fake", "default").output_schema
    envelope = {
        "original": {"synthetic": True},
        "previous": solver,
        "steps": {"vision-extract": first, "solver-rederive": solver},
    }
    final = await _ask(adapter, ModelRequest(prompt=_prompt(envelope, final_schema)))
    assert final == expected
    Draft202012Validator(final_schema).validate(final)
    # An image on a later stage (images-all-stages) does not change the derivation.
    again = await adapter.complete(
        ModelRequest(
            prompt=_prompt(envelope, final_schema),
            images=(ImageInput.from_bytes(rendered[0].image),),
        )
    )
    assert json.loads(again.text) == expected


async def test_noisy_rate_is_about_fifteen_percent_over_400_draws() -> None:
    # Keys outside every registered floor (an ad hoc table) keep the per-key draw.
    rows = [{"review": f"synthetic review number {index}"} for index in range(400)]
    table: dict[str, JsonValue] = {canonical_key(row): {"label": "positive"} for row in rows}
    schema = TASKS["sentiment-24"].spec("fake", "default").output_schema
    noisy = NoisyOracle("fake", OracleState(text=table), seed=7)
    wrong = 0
    for row in rows:
        answer = await _ask(noisy, ModelRequest(prompt=_prompt(row, schema)))
        Draft202012Validator(schema).validate(answer)
        wrong += answer != {"label": "positive"}
    assert wrong == noisy.flipped
    assert 0.10 <= wrong / 400 <= 0.20
    # Deterministic by seed and key; a different seed flips a different subset.
    again = NoisyOracle("fake", OracleState(text=table), seed=7)
    other = NoisyOracle("fake", OracleState(text=table), seed=8)
    flips_again: list[bool] = []
    flips_other: list[bool] = []
    for row in rows[:100]:
        request = ModelRequest(prompt=_prompt(row, schema))
        flips_again.append(await _ask(again, request) != {"label": "positive"})
        flips_other.append(await _ask(other, request) != {"label": "positive"})
    assert flips_again != flips_other


async def test_noisy_answers_fail_every_checker_shape() -> None:
    task = TASKS["invoice-40"]
    cases, state = task.dataset(9, 40)
    spec = task.spec("fake", "default")
    always = OracleAdapter("fake", state, error_rate=1.0)
    for case in cases:
        answer = await _ask(always, ModelRequest(prompt=_prompt(case.task, spec.output_schema)))
        Draft202012Validator(spec.output_schema).validate(answer)
        assert isinstance(answer, dict) and isinstance(case.expected, dict)
        assert answer["line_items"] != case.expected["line_items"]
    rendered = generate_pages(4, 1, "clean", key_page=True)
    state = OracleState(vision={p.sha256: p.truth.model_dump(mode="json") for p in rendered})
    always = OracleAdapter("fake", state, error_rate=1.0)
    for page in rendered:
        answer = await _ask(always, _page_request(page.image))
        Draft202012Validator(EXAM_OUTPUT_SCHEMA).validate(answer)
        assert answer != page.truth.model_dump(mode="json")
    with pytest.raises(ValueError):
        OracleAdapter("fake", state, error_rate=1.5)


async def test_usage_is_priced_nonzero_and_quote_equals_measured() -> None:
    rendered = generate_pages(7, 1, "clean", key_page=False)
    adapter = OracleAdapter("fake", OracleState())
    text = ModelRequest(prompt=_prompt({"review": "x"}, {"type": "object"}))
    quote = adapter.quote(text)
    response = await adapter.complete(text)
    assert (quote.tokens_in, quote.tokens_out) == (TEXT_TOKENS_IN, TEXT_TOKENS_OUT)
    assert quote.cost_usd == response.cost_usd > 0
    image_request = _page_request(rendered[0].image)
    image_quote = adapter.quote(image_request)
    image_response = await adapter.complete(image_request)
    assert image_quote.tokens_in == TEXT_TOKENS_IN + 100  # the fake per-image ceiling
    assert image_response.tokens_in == image_quote.tokens_in
    assert image_response.cost_usd == image_quote.cost_usd > quote.cost_usd
    assert adapter.seen[-1].image_bytes == len(rendered[0].image)


def test_factory_shares_one_adapter_per_model() -> None:
    state = OracleState(text={"k": {"label": "x"}})
    factory = oracle_factory(state, error_rate=0.15, seed=3)
    first = factory("fake")
    assert factory("fake") is first
    assert isinstance(first, OracleAdapter)
    assert first.model == "fake"
    merged = OracleState()
    merged.merge(state)
    merged.merge(OracleState(vision={"v": {}}, digitizer={"d": {}}))
    assert len(merged) == 3


# --- noisy quotas: a mechanism check with a guaranteed effect size -----------------

PARAMS: dict[str, JsonValue] = {"verifier_depth": 0, "retries": 0}


def test_quota_arithmetic() -> None:
    # Held-out: strictly below the limit (10/12 < 0.9, 5/8 < 0.7, 5/6 < 0.9).
    assert held_out_flips(12, 0.15, 0.90) == 2
    assert held_out_flips(12, 0.15, 0.85) == 2
    assert held_out_flips(8, 0.15, 0.70) == 3
    assert held_out_flips(6, 0.15, 0.90) == 1
    assert held_out_flips(20, 0.15, 0.90) == 3
    assert held_out_flips(20, 0.15, 0.75) == 6  # 15/20 = 0.75 still passes; 14/20 fails
    assert held_out_flips(3, 0.15, 1.0) == 1
    # Tuning: at least 6 so that mcnemar_exact(6, 0) < 0.05, capped at n; ceil(0.15*40) = 6.
    assert mcnemar_exact(6, 0) == 0.03125 and mcnemar_exact(5, 0) > 0.05
    assert [tuning_flips(n, 0.15) for n in (4, 6, 12, 40, 60)] == [4, 6, 6, 6, 9]
    assert tuning_flips(12, 0.0) == held_out_flips(12, 0.0, 0.9) == 0
    assert tuning_flips(12, 1.0) == held_out_flips(12, 1.0, 0.9) == 12
    for n in range(1, 41):
        for limit in (0.7, 0.75, 0.85, 0.9, 1.0):
            assert (n - held_out_flips(n, 0.15, limit)) / n < limit


def test_partition_matches_the_floor_evaluator() -> None:
    task = TASKS["exam-clean-40"]
    bundle = task.bundle(7, 24)
    floor = HeldOutTasks(task.spec("fake", "noisy", size=24).floor, deepcopy(bundle.cases))
    (partition,) = bundle.state.floors
    assert partition.tuning == tuple(lookup_key(case.task) for case in floor.tuning)
    assert len(partition.held_out) == 12 and partition.limit == task.floor_limit
    assert set(partition.held_out).isdisjoint(partition.tuning)
    # Image tasks key by the sha256 of the delivered bytes; text tasks by canonical input.
    assert {lookup_key(case.task) for case in bundle.cases} == set(bundle.state.vision)
    assert lookup_key({"review": "x"}) == canonical_key({"review": "x"})


def test_mutated_expected_cannot_change_the_quota() -> None:
    task = TASKS["sentiment-24"]
    bundle = task.bundle(7, 24)
    garbage = [
        EvaluationCase(case_id=c.case_id, task=c.task, expected={"label": "zzz"})
        for c in bundle.cases
    ]
    state = OracleState(text=bundle.state.text)
    state.add_floor([(c.case_id, lookup_key(c.task)) for c in garbage], task.floor_limit)
    assert state.floors == bundle.state.floors


async def _run(task: BenchTask, adapter: OracleAdapter, cases: list[EvaluationCase]) -> list[Any]:
    spec = task.spec("fake", "noisy", size=len(cases) * 2)
    out: list[Any] = []
    for case in cases:
        context = RunContext(adapter, spec.budget)
        if task.kind == "pipeline":
            components = get_preset("extractor").components
            out.append(await run_pipeline(context, spec, case.task, preset_name="extractor",
                                          params=PARAMS, components=components))
        else:
            out.append(await get_preset("extractor").build(spec, PARAMS)(context, case.task))
    return out


@pytest.mark.parametrize("task_id", ["sentiment-24", "exam-clean-40", "digitizer-24"])
async def test_noisy_fails_the_floor_and_regresses_tuning_at_ci_seed(task_id: str) -> None:
    task = TASKS[task_id]
    size = task.sizes["smoke"]
    bundle = task.bundle(7, size)
    ordered = sorted(bundle.cases, key=lambda case: case.case_id)
    checker = CHECKERS[task.checker]
    clean = OracleAdapter("fake", bundle.state)
    noisy = NoisyOracle("fake", bundle.state, seed=7)
    results: dict[str, tuple[list[bool], list[bool]]] = {}
    for name, part in (("tuning", ordered[::2]), ("held_out", ordered[1::2])):
        base = await _run(task, clean, part)
        cand = await _run(task, noisy, part)
        results[name] = (
            [checker(c.expected, o) for c, o in zip(part, base, strict=True)],
            [checker(c.expected, o) for c, o in zip(part, cand, strict=True)],
        )
    base_t, cand_t = results["tuning"]
    base_h, cand_h = results["held_out"]
    assert all(base_t) and all(base_h)  # the default oracle licenses
    assert sum(cand_h) / len(cand_h) < task.floor_limit  # noisy: primary floor fails
    b = sum(x and not y for x, y in zip(base_t, cand_t, strict=True))
    c = sum(y and not x for x, y in zip(base_t, cand_t, strict=True))
    assert c == 0 and b == tuning_flips(len(base_t), 0.15) >= 6
    assert mcnemar_exact(b, c) < 0.05
    # Same key, same answer: a second noisy adapter reproduces every bit.
    again = NoisyOracle("fake", bundle.state, seed=7)
    assert await _run(task, again, ordered[::2]) == await _run(task, noisy, ordered[::2])


async def test_stage1_flip_always_changes_the_digitizer_output() -> None:
    # A printed key that is already wrong must not move to another wrong label (which
    # would leave the disagreement list, and so the final output, unchanged).
    task = TASKS["digitizer-24"]
    bundle = task.bundle(3, 24)
    always = OracleAdapter("fake", bundle.state, error_rate=1.0)
    outputs = await _run(task, always, bundle.cases)
    assert all(o != c.expected for o, c in zip(outputs, bundle.cases, strict=True))
    assert always.flipped == len(bundle.cases)  # later stages never flip on their own
