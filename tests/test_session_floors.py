"""Floor drafts: operator-owned labels, SYNTHETIC stamping, and the same split as Stage 4."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import JsonValue
from session_support import MALFORMED_DRAFT

from architect.floors import EvaluationCase, HeldOutTasks
from architect.models import AdapterError, FakeAdapter, ModelRequest, ModelResponse
from architect.profiles import build_services, load_profile
from architect.runtime import Runtime
from architect.session.codes import SessionError
from architect.session.floors import (
    DEFAULT_DRAFT_COUNT,
    DRAFT_RANGE,
    HONESTY_NOTICE,
    MINIMUM_CASES,
    RECOMMENDED_CASES,
    SYNTHETIC_ORIGINS,
    DraftUnreadable,
    FloorSummary,
    add_case,
    add_file,
    draft_cases,
    draft_fitting_cases,
    find_case,
    load_cases,
    new_draft,
    partition_preview,
    ready,
    register,
    relax_to_fit,
    review,
    save_dataset,
    stamp,
    summaries,
    summarize,
    synthetic_label,
    unreviewed,
)
from architect.session.state import FloorDraft, State
from architect.spec import Budget, FloorDefinition, FloorKind, Spec
from architect.telemetry import TelemetryStore


def spec(dataset: str = "sentiment-v1") -> Spec:
    return Spec(
        task="Label the sentiment of a short product review.",
        input_schema={"properties": {"review": {"type": "string"}}, "type": "object"},
        output_schema={"properties": {"label": {"enum": ["neg", "pos"]}}, "type": "object"},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.9, dataset=dataset),
        budget=Budget(cost_usd=0.7, compute_s=60),
        model_target="fake",
    )


def drafting_adapter(items: object, *, count: int = 1) -> FakeAdapter:
    text = items if isinstance(items, str) else json.dumps(items)
    return FakeAdapter(
        responses=[
            ModelResponse(text=text, tokens_in=8, tokens_out=12, cost_usd=0.0002, model="fake")
            for _ in range(count)
        ]
    )


class SpyAdapter(FakeAdapter):
    def __init__(self, items: object) -> None:
        super().__init__(
            responses=[
                ModelResponse(
                    text=json.dumps(items),
                    tokens_in=8,
                    tokens_out=12,
                    cost_usd=0.0002,
                    model="fake",
                )
            ]
        )
        self.prompts: list[str] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.prompts.append(request.prompt)
        return await super().complete(request)


def profile_with(tmp_path: Path, floors: dict[str, object]) -> Path:
    path = tmp_path / "w" / "session-profile.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "workspace": ".",
                "models": {"fake": {"provider": "fake"}},
                "floors": floors,
                "compile": {"max_candidates": 1, "enable_rewriting": False},
            }
        ),
        encoding="utf-8",
    )
    return path


def draft(*, dataset: str = "sentiment-v1") -> FloorDraft:
    return new_draft(dataset, FloorKind.HELD_OUT_TASKS, State.REVIEW)


def test_constants_match_the_contract() -> None:
    assert MINIMUM_CASES[FloorKind.HELD_OUT_TASKS] == 2 and MINIMUM_CASES[FloorKind.LLM_JUDGE] == 2
    assert MINIMUM_CASES[FloorKind.HUMAN_EDIT_RATE] == 60
    assert RECOMMENDED_CASES == 20 and DRAFT_RANGE == (2, 64)
    assert SYNTHETIC_ORIGINS == ("model_draft", "file")
    assert HONESTY_NOTICE.startswith("Drafted cases were written by the same model")
    assert "SYNTHETIC and is never counted as production evidence." in HONESTY_NOTICE
    assert (
        HONESTY_NOTICE.isascii() and max(len(line) for line in HONESTY_NOTICE.splitlines()) <= 100
    )


@pytest.mark.parametrize(
    "kind", [FloorKind.HUMAN_EDIT_RATE, FloorKind.LLM_JUDGE, FloorKind.HELD_OUT_GAME_SCORE]
)
def test_only_held_out_tasks_is_supported(kind: FloorKind) -> None:
    with pytest.raises(SessionError, match="floor_kind_unsupported_in_session"):
        new_draft("x", kind, State.READY)


def test_dataset_name_cannot_be_a_path() -> None:
    for bad in ("../x", "a/b", ".hidden", "", "x" * 65, "sp ace"):
        with pytest.raises(SessionError, match="invalid_floor_file"):
            new_draft(bad, FloorKind.HELD_OUT_TASKS, State.READY)


def test_add_case_assigns_ids_and_rejects_duplicates() -> None:
    current = draft()
    first = add_case(
        current, {"task": {"review": "good"}, "expected": {"label": "pos"}}, origin="operator"
    )
    assert (
        first.case.case_id == "sentiment-v1-001" and first.origin == "operator" and first.reviewed
    )
    second = add_case(
        current, {"task": "t", "expected": "e", "case_id": "custom"}, origin="operator"
    )
    assert second.case.case_id == "custom"
    third = add_case(current, {"task": "t", "expected": "e"}, origin="operator")
    assert third.case.case_id == "sentiment-v1-002"
    with pytest.raises(SessionError, match="duplicate_case_id"):
        add_case(current, {"task": "t", "expected": "e", "case_id": "custom"}, origin="operator")
    for bad in ([1, 2], "text", {"task": "only"}, {"task": "t", "expected": "e", "extra": 1}):
        with pytest.raises(SessionError, match="invalid_floor_file"):
            add_case(current, bad, origin="operator")
    assert len(current.cases) == 3


def test_add_file_valid_invalid_and_all_or_nothing(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text(
        json.dumps(
            [
                {"case_id": "f1", "task": "a", "expected": "x", "metadata": {"source": "csv"}},
                {"case_id": "f2", "task": "b", "expected": "y"},
            ]
        ),
        encoding="utf-8",
    )
    current = draft()
    assert add_file(current, good) == 2
    assert [c.origin for c in current.cases] == ["file", "file"]
    assert all(c.reviewed for c in current.cases)
    assert current.cases[0].case.metadata == {"source": "csv"}
    with pytest.raises(SessionError, match="duplicate_case_id"):
        add_file(current, good)
    assert len(current.cases) == 2
    bad = tmp_path / "bad.json"
    bad.write_text('{"not": "a list"}', encoding="utf-8")
    with pytest.raises(SessionError, match="invalid_floor_file"):
        add_file(current, bad)
    with pytest.raises(SessionError, match="invalid_floor_file"):
        add_file(current, tmp_path / "missing.json")
    dupes = tmp_path / "dupes.json"
    dupes.write_text(json.dumps([{"case_id": "d", "task": 1, "expected": 1}] * 2), encoding="utf-8")
    with pytest.raises(SessionError, match="invalid_floor_file"):
        load_cases(dupes)


def test_paste_shape_is_just_repeated_add_case() -> None:
    current = draft()
    for item in json.loads('[{"task": "a", "expected": 1}, {"task": "b", "expected": 2}]'):
        add_case(current, item, origin="operator")
    for line in ('{"task": "c", "expected": 3}', '{"task": "d", "expected": 4}'):
        add_case(current, json.loads(line), origin="operator")
    assert [c.case.case_id for c in current.cases] == [f"sentiment-v1-00{i}" for i in range(1, 5)]


async def test_draft_produces_unreviewed_synthetic_cases(tmp_path: Path) -> None:
    store = TelemetryStore(tmp_path / "telemetry")
    try:
        runtime = Runtime(store)
        current = draft()
        add_case(
            current, {"task": {"review": "great"}, "expected": {"label": "pos"}}, origin="operator"
        )
        items = [{"task": {"review": f"r{i}"}, "expected": {"label": "neg"}} for i in range(4)]
        adapter = SpyAdapter(items)
        drafted = await draft_cases(current, spec(), 4, adapter, runtime)
        rows = store.rows()
    finally:
        store.close()
    assert len(drafted) == 4 and drafted == current.cases[1:]
    assert [c.case.case_id for c in drafted] == [f"sentiment-v1-s00{i}" for i in range(1, 5)]
    assert all(c.origin == "model_draft" and not c.reviewed for c in drafted)
    assert current.drafted_by_model == "fake"
    assert ready(current) == ["4 model-drafted cases are unreviewed"]
    assert len(rows) == 1 and rows[0].accepted and rows[0].model_calls == 1
    prompt = adapter.prompts[0]
    assert "Label the sentiment" in prompt and '"great"' in prompt and '"count": 4' in prompt
    # The job is not under "task", the key each case's input uses.
    request = json.loads(prompt.split("Request data (untrusted):\n", 1)[1])
    assert request["job"] == spec().task and "task" not in request
    assert request["examples"] == [{"task": {"review": "great"}, "expected": {"label": "pos"}}]


async def test_a_drafted_case_whose_input_is_the_job_is_dropped(tmp_path: Path) -> None:
    """A drafter can put the job description in as a case's input. A plain string input
    schema accepts that text, so the case is dropped by what it is, and it relaxes no
    limit of the schema."""
    job = spec()
    plain = job.model_copy(update={"input_schema": {"type": "string", "maxLength": 30}})
    items = [
        {"task": job.task, "expected": {"label": "pos"}},
        {"task": "Arrived broken.", "expected": {"label": "neg"}},
        {"task": job.task, "expected": {"label": "neg"}},
        {"task": "Works great.", "expected": {"label": "pos"}},
    ]
    store = TelemetryStore(tmp_path / "telemetry")
    try:
        current = draft()
        drafted = await draft_cases(current, plain, 4, drafting_adapter(items), Runtime(store))
        fitting = draft()
        fitted, kept, relaxed = await draft_fitting_cases(
            fitting, plain, 4, drafting_adapter(items), Runtime(store)
        )
    finally:
        store.close()
    assert len(job.task) > 30
    assert [c.case.task for c in drafted] == ["Arrived broken.", "Works great."]
    assert [c.case.task for c in kept] == ["Arrived broken.", "Works great."]
    assert fitted == plain and relaxed == ()


async def test_draft_prompt_never_contains_configured_floor_cases(tmp_path: Path) -> None:
    configured = tmp_path / "configured.json"
    configured.write_text(
        json.dumps([{"case_id": "c1", "task": "CONFIGURED-SECRET", "expected": "x"}]),
        encoding="utf-8",
    )
    store = TelemetryStore(tmp_path / "telemetry")
    try:
        current = draft()
        add_file(current, configured)
        for i in range(7):
            add_case(current, {"task": f"op-{i}", "expected": i}, origin="operator")
        adapter = SpyAdapter([{"task": "t", "expected": "e"}, {"task": "u", "expected": "f"}])
        await draft_cases(current, spec(), 2, adapter, Runtime(store))
    finally:
        store.close()
    prompt = adapter.prompts[0]
    assert "CONFIGURED-SECRET" not in prompt
    assert prompt.count('"op-') == 5, "at most five operator examples"


@pytest.mark.parametrize("count", [0, 1, 65, -3])
async def test_draft_count_range(tmp_path: Path, count: int) -> None:
    store = TelemetryStore(tmp_path / "telemetry")
    try:
        adapter = drafting_adapter([])
        with pytest.raises(SessionError, match="draft_count_out_of_range"):
            await draft_cases(draft(), spec(), count, adapter, Runtime(store))
        assert adapter.calls == 0
    finally:
        store.close()


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"task": "t", "expected": "e"}',
        '[{"task": "t"}]',
        '[{"task": "t", "expected": "e", "case_id": "sneaky"}]',
        '[{"task": "t", "expected": "e", "metadata": {"synthetic": false}}]',
    ],
)
async def test_draft_bad_output_is_inference_failed_without_text(tmp_path: Path, text: str) -> None:
    """The session's draft is one metered call; build mode asks once more, then stops."""
    store = TelemetryStore(tmp_path / "telemetry")
    try:
        current = draft()
        once = drafting_adapter(text, count=2)
        with pytest.raises(SessionError) as info:
            await draft_cases(current, spec(), 2, once, Runtime(store))
        fitting = draft()
        twice = drafting_adapter(text, count=3)
        with pytest.raises(SessionError) as again:
            await draft_fitting_cases(fitting, spec(), 2, twice, Runtime(store))
    finally:
        store.close()
    assert str(info.value) == "inference_failed" and current.cases == []
    assert str(again.value) == "inference_failed" and fitting.cases == []
    assert once.calls == 1 and twice.calls == 2


async def test_build_mode_asks_again_when_a_draft_does_not_parse(tmp_path: Path) -> None:
    """A drafting reply that does not parse does not stop a build: build mode asks once
    more and uses the second reply."""
    items = [{"task": {"review": f"r{i}"}, "expected": {"label": "neg"}} for i in range(2)]
    adapter = FakeAdapter(
        responses=[
            ModelResponse(text=text, tokens_in=8, tokens_out=12, cost_usd=0.0002, model="fake")
            for text in ("```json\n[{\"task\": ", json.dumps(items))
        ]
    )
    store = TelemetryStore(tmp_path / "telemetry")
    try:
        current = draft()
        _, kept, _ = await draft_fitting_cases(current, spec(), 2, adapter, Runtime(store))
        rows = store.rows()
    finally:
        store.close()
    assert [c.case.task for c in kept] == [{"review": "r0"}, {"review": "r1"}]
    assert adapter.calls == 2
    assert [row.accepted for row in rows] == [False, True]  # both calls are metered


async def test_build_mode_does_not_ask_again_after_a_provider_failure(tmp_path: Path) -> None:
    class Down(FakeAdapter):
        def __init__(self) -> None:
            super().__init__(
                responses=[
                    ModelResponse(text="[]", tokens_in=8, tokens_out=12, cost_usd=0.0002,
                                  model="fake")
                ] * 3
            )
            self.attempts = 0

        async def complete(self, request: ModelRequest) -> ModelResponse:
            self.attempts += 1
            raise AdapterError("provider unavailable")

    adapter = Down()
    store = TelemetryStore(tmp_path / "telemetry")
    try:
        with pytest.raises(SessionError, match="inference_failed"):
            await draft_fitting_cases(draft(), spec(), 2, adapter, Runtime(store))
    finally:
        store.close()
    assert adapter.attempts == 1


async def test_draft_truncates_extra_cases_and_keeps_ids_unique(tmp_path: Path) -> None:
    store = TelemetryStore(tmp_path / "telemetry")
    try:
        current = draft()
        add_case(
            current,
            {"task": "t", "expected": "e", "case_id": "sentiment-v1-s001"},
            origin="operator",
        )
        items = [{"task": {"review": f"r{i}"}, "expected": {"label": "neg"}} for i in range(5)]
        drafted = await draft_cases(current, spec(), 3, drafting_adapter(items), Runtime(store))
    finally:
        store.close()
    assert [c.case.case_id for c in drafted] == [
        "sentiment-v1-s002",
        "sentiment-v1-s003",
        "sentiment-v1-s004",
    ]


async def test_draft_drops_cases_the_spec_schemas_refuse(tmp_path: Path) -> None:
    """The exam refuses a task outside the input schema, and no valid output can equal a
    label outside the output schema: such drafted cases are dropped and the rest kept."""
    store = TelemetryStore(tmp_path / "telemetry")
    try:
        current = draft()
        items = [
            {"task": {"review": "fine"}, "expected": {"label": "pos"}},
            {"task": {"review": 3}, "expected": {"label": "neg"}},
            {"task": {"review": "bad"}, "expected": {"label": "awful"}},
            {"task": {"review": "meh"}, "expected": {"label": "neg"}},
        ]
        drafted = await draft_cases(current, spec(), 4, drafting_adapter(items), Runtime(store))
    finally:
        store.close()
    assert [c.case.task for c in drafted] == [{"review": "fine"}, {"review": "meh"}]
    assert [c.case.case_id for c in drafted] == ["sentiment-v1-s001", "sentiment-v1-s002"]
    assert current.cases == drafted


Pairs = list[tuple[JsonValue, JsonValue]]


def invoice_spec() -> Spec:
    """The judges' walkthrough: Stage 0 limited invoice_number to letters and digits."""
    number: dict[str, JsonValue] = {"type": "string", "pattern": "^[A-Za-z0-9]+$"}
    return Spec(
        task="Pull the invoice number, date, vendor and total out of invoice text.",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string", "maxLength": 40}},
            "required": ["text"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "invoice_number": number,
                "date": {"type": "string", "format": "date"},
                "total": {"type": "number"},
            },
            "required": ["invoice_number", "date", "total"],
            "additionalProperties": False,
        },
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.8, dataset="invoices"),
        budget=Budget(cost_usd=0.1, compute_s=60),
        model_target="fake",
    )


def invoice(
    number: JsonValue, total: JsonValue = 12.5, date: str = "2024-03-01"
) -> dict[str, JsonValue]:
    return {"invoice_number": number, "date": date, "total": total}


def test_relax_to_fit_drops_string_limits_and_keeps_the_shape() -> None:
    long_text = "Invoice INV-4567 from Acme, dated 1 March 2024, total 12.50"
    pairs: Pairs = [
        ({"text": long_text}, invoice("INV-4567")),
        ({"text": "Invoice INV-999"}, invoice("INV-999", date="March 3, 2024")),
        ({"text": "Invoice A1"}, invoice("A1")),
    ]
    fitted, relaxed = relax_to_fit(invoice_spec(), pairs)
    assert fitted.output_schema["properties"] == {
        "invoice_number": {"type": "string"},
        "date": {"type": "string"},
        "total": {"type": "number"},
    }
    assert fitted.output_schema["required"] == ["invoice_number", "date", "total"]
    assert fitted.output_schema["additionalProperties"] is False
    assert fitted.input_schema["properties"] == {"text": {"type": "string"}}
    assert [(item.schema, item.text()) for item in relaxed] == [
        ("input_schema", "no maxLength on text"),
        ("output_schema", "no pattern on invoice_number"),
        ("output_schema", "no format on date"),
    ]
    assert fitted.spec_hash != invoice_spec().spec_hash
    # Nothing to relax: the same Spec comes back.
    same, none = relax_to_fit(invoice_spec(), pairs[2:])
    assert same == invoice_spec() and none == ()


def test_relax_to_fit_never_relaxes_for_a_case_of_the_wrong_shape() -> None:
    """A wrong type or a missing required field drops the case; its other values do not
    relax anything, and a case of the right shape still does."""
    pairs: Pairs = [
        ({"text": "a"}, invoice("INV-1", total="12")),  # total is a string: wrong type
        ({"text": "b"}, {"invoice_number": "INV-2", "date": "2024-03-01"}),  # no total
        ({"text": "c"}, invoice("A1")),
    ]
    fitted, relaxed = relax_to_fit(invoice_spec(), pairs)
    assert fitted == invoice_spec() and relaxed == ()
    fitted, relaxed = relax_to_fit(invoice_spec(), [*pairs, ({"text": "d"}, invoice("INV-3"))])
    assert [item.text() for item in relaxed] == ["no pattern on invoice_number"]


def test_relax_to_fit_keeps_an_enum_the_user_listed() -> None:
    def category_spec(options: list[JsonValue]) -> Spec:
        return Spec(
            task="Sort support emails.",
            input_schema={"type": "object", "properties": {"email": {"type": "string"}}},
            output_schema={
                "type": "object",
                "properties": {"category": {"type": "string", "enum": options}},
                "required": ["category"],
            },
            floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.8, dataset="mail"),
            budget=Budget(cost_usd=0.1, compute_s=60),
            model_target="fake",
        )

    listed = category_spec(["billing", "bug_report", "question"])
    stated = "Sort support emails into billing, bug report or question"
    pairs: Pairs = [({"email": "it crashes"}, {"category": "Bug report"})]
    assert relax_to_fit(listed, pairs, stated) == (listed, ())
    # A list Stage 0 made up (the user never named the values) is free text.
    made_up = category_spec(["billing", "other"])
    fitted, relaxed = relax_to_fit(made_up, pairs, stated)
    assert fitted.output_schema["properties"] == {"category": {"type": "string"}}
    assert [item.text() for item in relaxed] == ["no enum on category"]


def test_relax_to_fit_reaches_into_branches_and_items_but_not_a_user_schema() -> None:
    def lines(sku: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return {"type": "array", "items": {"type": "object", "properties": {"sku": sku}}}

    schema: dict[str, JsonValue] = {
        "type": "object",
        "properties": {
            "number": {"anyOf": [{"type": "string", "pattern": "^[0-9]+$"}, {"type": "null"}]},
            "lines": lines({"type": "string", "maxLength": 3}),
        },
    }
    base = invoice_spec().model_copy(update={"output_schema": schema})
    pairs: Pairs = [({"text": "x"}, {"number": "INV-7", "lines": [{"sku": "ABCD"}]})]
    fitted, relaxed = relax_to_fit(base, pairs)
    assert fitted.output_schema["properties"] == {
        "number": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "lines": lines({"type": "string"}),
    }
    assert [item.text() for item in relaxed] == [
        "no pattern on number",
        "no maxLength on lines[].sku",
    ]
    owned = base.model_copy(update={"provenance": {"output_schema": "user"}})
    assert relax_to_fit(owned, pairs) == (owned, ())


async def test_draft_fitting_cases_relaxes_the_spec_and_keeps_the_cases(tmp_path: Path) -> None:
    """The walkthrough's invoice build: the drafted numbers have dashes, so the pattern goes
    and every case of the right shape stays; a case of the wrong shape is still dropped."""
    store = TelemetryStore(tmp_path / "telemetry")
    items = [
        {"task": {"text": "Invoice INV-4567"}, "expected": invoice("INV-4567")},
        {"task": {"text": "Invoice INV-999"}, "expected": invoice("INV-999")},
        {"task": {"text": "Invoice 77"}, "expected": invoice("77", total="seven")},
    ]
    try:
        current = draft(dataset="invoices")
        fitted, kept, relaxed = await draft_fitting_cases(
            current, invoice_spec(), 3, drafting_adapter(items), Runtime(store)
        )
    finally:
        store.close()
    assert [c.case.expected for c in kept] == [invoice("INV-4567"), invoice("INV-999")]
    assert current.cases == kept and current.drafted_by_model == "fake"
    assert "pattern" not in json.dumps(fitted.output_schema)
    assert [item.text() for item in relaxed] == ["no pattern on invoice_number"]
    # The session's draft_cases keeps the operator's Spec and drops what it refuses.
    store = TelemetryStore(tmp_path / "telemetry2")
    try:
        session_draft = draft(dataset="invoices")
        dropped = await draft_cases(
            session_draft, invoice_spec(), 3, drafting_adapter(items), Runtime(store)
        )
    finally:
        store.close()
    assert dropped == []


def test_review_ok_expected_and_drop() -> None:
    current = draft()
    for i in range(3):
        current.cases.append(
            _drafted(current, f"sentiment-v1-s00{i + 1}", {"review": str(i)}, {"label": "neg"})
        )
    review(current, "sentiment-v1-s001", "ok")
    assert current.cases[0].reviewed and "corrected" not in current.cases[0].case.metadata
    review(current, "sentiment-v1-s002", ("expected", {"label": "pos"}))
    fixed = current.cases[1]
    assert fixed.reviewed and fixed.origin == "model_draft"
    assert fixed.case.expected == {"label": "pos"} and fixed.case.metadata["corrected"] is True
    review(current, "sentiment-v1-s003", "drop")
    assert [c.case.case_id for c in current.cases] == ["sentiment-v1-s001", "sentiment-v1-s002"]
    with pytest.raises(SessionError, match="unknown_case_id"):
        review(current, "sentiment-v1-s003", "ok")
    assert ready(current) == []
    stamped = stamp(fixed, "fake")
    assert stamped.metadata == {
        "corrected": True,
        "synthetic": True,
        "origin": "model_draft",
        "drafted_by_model": "fake",
        "reviewed": True,
    }


def _drafted(current: FloorDraft, case_id: str, task: object, expected: object):  # type: ignore[no-untyped-def]
    from architect.session.state import DraftCase

    return DraftCase(
        case=EvaluationCase(case_id=case_id, task=task, expected=expected),  # type: ignore[arg-type]
        origin="model_draft",
        reviewed=False,
    )


def test_ready_reasons_are_exact() -> None:
    current = draft()
    assert ready(current) == [
        "below minimum: 0 cases, need 2",
        "held-out partition would have 0 cases",
    ]
    add_case(current, {"task": "a", "expected": 1}, origin="operator")
    assert ready(current) == [
        "below minimum: 1 cases, need 2",
        "held-out partition would have 0 cases",
    ]
    add_case(current, {"task": "b", "expected": 2}, origin="operator")
    assert ready(current) == []
    current.cases.append(_drafted(current, "z", "t", "e"))
    assert ready(current) == ["1 model-drafted cases are unreviewed"]


def test_unreviewed_and_find_case() -> None:
    current = draft()
    add_case(current, {"task": "a", "expected": 1}, origin="operator")
    first = _drafted(current, "z", "t", "e")
    second = _drafted(current, "y", "u", "f")
    current.cases.extend([first, second])
    assert unreviewed(current) == [first, second]  # draft order, not sorted ids
    assert find_case(current, "y") is second
    review(current, "z", "ok")
    assert unreviewed(current) == [second]
    review(current, "y", "drop")
    assert unreviewed(current) == []
    with pytest.raises(SessionError, match="unknown_case_id"):
        find_case(current, "y")
    assert DEFAULT_DRAFT_COUNT == 20 and DRAFT_RANGE[0] <= DEFAULT_DRAFT_COUNT <= DRAFT_RANGE[1]


def test_split_matches_the_evaluator_partition() -> None:
    current = draft()
    ids = ["b", "a", "d", "c", "e", "sentiment-v1-001"]
    for case_id in ids:
        add_case(current, {"task": case_id, "expected": 1, "case_id": case_id}, origin="operator")
    tuning, held_out = partition_preview(current)
    evaluator = HeldOutTasks(spec().floor, [c.case for c in current.cases])
    assert tuning == [c.case_id for c in evaluator.tuning]
    assert sorted(tuning + held_out) == sorted(ids) and len(held_out) == 3
    assert not set(tuning) & set(held_out)


def test_accept_path_writes_stamped_file_registers_and_rebuilds(tmp_path: Path) -> None:
    profile_path = profile_with(tmp_path, {})
    workspace = profile_path.parent
    current = draft()
    add_case(current, {"task": "a", "expected": 1}, origin="operator")
    file = tmp_path / "in.json"
    file.write_text(
        json.dumps([{"case_id": "f", "task": "b", "expected": 2, "metadata": {"k": "v"}}]),
        encoding="utf-8",
    )
    add_file(current, file)
    current.cases.append(_drafted(current, "sentiment-v1-s001", "c", 3))
    review(current, "sentiment-v1-s001", ("expected", 4))
    current.drafted_by_model = "fake"
    assert ready(current) == []

    path = save_dataset(workspace, current)
    assert path == workspace / "floors" / "sentiment-v1.json" and not list(
        path.parent.glob("*.tmp")
    )
    rows = json.loads(path.read_text(encoding="utf-8"))
    assert [row["metadata"] for row in rows] == [
        {"synthetic": False, "origin": "operator", "drafted_by_model": None, "reviewed": True},
        {"k": "v", "synthetic": True, "origin": "file", "drafted_by_model": None, "reviewed": True},
        {
            "corrected": True,
            "synthetic": True,
            "origin": "model_draft",
            "drafted_by_model": "fake",
            "reviewed": True,
        },
    ]
    assert rows[2]["expected"] == 4

    payload = register(profile_path, "sentiment-v1", FloorKind.HELD_OUT_TASKS, path)
    assert payload["floors"] == {
        "sentiment-v1": {
            "kind": "held_out_tasks", "path": "floors/sentiment-v1.json", "retakes": True,
        }
    }
    assert json.loads(profile_path.read_text(encoding="utf-8")) == payload

    profile = load_profile(profile_path)
    services = build_services(profile, profile.workspace)
    try:
        assert "sentiment-v1" in services.profile.floors
        assert path.resolve() in services.profile.pi.denied_paths
        floor = services.compiler.floor_factory(spec().floor)
        assert len(floor.tuning) == 2
    finally:
        services.close()

    summary = summarize(profile, "sentiment-v1", 0.9)
    assert summary == FloorSummary(
        dataset="sentiment-v1",
        kind=FloorKind.HELD_OUT_TASKS,
        path=path.resolve(),
        count=3,
        tuning=2,
        held_out=1,
        synthetic=2,
        drafted_by_model="fake",
        misses_allowed=0,
    )
    assert synthetic_label(summary) == (
        "[SYNTHETIC: 2 of 3 cases model-drafted by fake, accepted by operator; "
        "not production evidence]"
    )
    assert summaries(profile) == (summarize(profile, "sentiment-v1"),)
    assert summaries(profile)[0].misses_allowed is None


def test_register_refuses_non_json_profiles(tmp_path: Path) -> None:
    toml = tmp_path / "architect.toml"
    toml.write_text("workspace = '.'\n", encoding="utf-8")
    with pytest.raises(SessionError, match="profile_not_editable"):
        register(toml, "d", FloorKind.HELD_OUT_TASKS, tmp_path / "f.json")
    with pytest.raises(SessionError, match="profile_not_editable"):
        register(tmp_path / "missing.json", "d", FloorKind.HELD_OUT_TASKS, tmp_path / "f.json")
    broken = tmp_path / "broken.json"
    broken.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(SessionError, match="invalid_profile"):
        register(broken, "d", FloorKind.HELD_OUT_TASKS, tmp_path / "f.json")


def test_summaries_and_labels(tmp_path: Path) -> None:
    cases = tmp_path / "w" / "cases.json"
    cases.parent.mkdir(parents=True)
    cases.write_text(
        json.dumps(
            [
                {
                    "case_id": str(i),
                    "task": {},
                    "expected": "ok",
                    "metadata": {"synthetic": True, "drafted_by_model": "not a model id!"},
                }
                for i in range(24)
            ]
        ),
        encoding="utf-8",
    )
    edits = tmp_path / "w" / "edits.jsonl"
    edits.write_text("\n".join(['{"case_id": "1"}'] * 3) + "\n\n", encoding="utf-8")
    profile_path = profile_with(
        tmp_path,
        {
            "tasks": {"kind": "held_out_tasks", "path": "cases.json"},
            "edits": {"kind": "human_edit_rate", "path": "edits.jsonl"},
        },
    )
    profile = load_profile(profile_path)
    tasks = summarize(profile, "tasks", 0.9)
    assert (tasks.count, tasks.tuning, tasks.held_out, tasks.synthetic) == (24, 12, 12, 24)
    assert tasks.misses_allowed == 1 and tasks.drafted_by_model is None
    assert synthetic_label(tasks) == (
        "[SYNTHETIC: 24 of 24 cases not operator-authored, accepted by operator; "
        "not production evidence]"
    )
    assert summarize(profile, "tasks", 0.8).misses_allowed == 2
    assert summarize(profile, "tasks", 0.5).misses_allowed == 6
    edits_summary = summarize(profile, "edits")
    assert edits_summary.count == 3 and edits_summary.synthetic == 0
    assert synthetic_label(edits_summary) == ""
    assert [s.dataset for s in summaries(profile)] == ["edits", "tasks"]
    with pytest.raises(SessionError, match="floor_not_configured"):
        summarize(profile, "nope")
    cases.write_text("{}", encoding="utf-8")
    with pytest.raises(SessionError, match="invalid_floor_file"):
        summarize(profile, "tasks")


# -- a drafting reply that is not valid JSON ------------------------------------------------


def email_spec(category: dict[str, JsonValue] | None = None) -> Spec:
    """The support-email job as Stage 0 wrote it on the run where drafting failed."""
    return Spec(
        task="Classify support emails into categories: billing, bug report, or question",
        input_schema={
            "type": "object",
            "properties": {"email_text": {"type": "string"}},
            "required": ["email_text"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"category": category or {"type": "string"}},
            "required": ["category"],
            "additionalProperties": False,
        },
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.85, dataset="emails"),
        budget=Budget(cost_usd=0.7, compute_s=60),
        model_target="fake",
    )


class Replies(FakeAdapter):
    """One reply per call, in order; records every prompt."""

    def __init__(self, *texts: str) -> None:
        super().__init__(
            responses=[
                ModelResponse(text=text, tokens_in=8, tokens_out=12, cost_usd=0.0002, model="fake")
                for text in texts
            ]
        )
        self.prompts: list[str] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.prompts.append(request.prompt)
        return await super().complete(request)


def test_the_live_reply_is_not_json() -> None:
    with pytest.raises(json.JSONDecodeError):
        json.loads(MALFORMED_DRAFT)


async def test_build_mode_asks_again_with_a_request_that_says_what_was_wrong(
    tmp_path: Path,
) -> None:
    """Live: the second request was the first one again, the call is deterministic, and the
    same bad reply came back. The second request now says the last reply was not valid JSON
    and where it broke, so it is not the same request."""
    good = [
        {"task": {"email_text": f"mail {i}"}, "expected": {"category": "question"}}
        for i in range(4)
    ]
    adapter = Replies(MALFORMED_DRAFT, json.dumps(good))
    store = TelemetryStore(tmp_path / "telemetry")
    try:
        current = draft(dataset="emails")
        notes: list[str] = []
        _, kept, _ = await draft_fitting_cases(
            current, email_spec(), 4, adapter, Runtime(store), notes=notes
        )
    finally:
        store.close()
    first, second = adapter.prompts
    assert first != second
    assert "not valid JSON" in second and "line 22" in second
    assert second.endswith(first[first.index("\nRequest data (untrusted):") :])
    assert "charged twice" not in second  # the bad reply itself is not sent back
    assert [c.case.task for c in kept] == [item["task"] for item in good]
    assert notes == []  # the second reply was whole: nothing was salvaged


async def test_build_mode_keeps_the_whole_cases_of_a_reply_that_is_not_json(
    tmp_path: Path,
) -> None:
    """The live reply, twice: 10 of its 12 cases are whole objects that fit the agent's
    fields. Those are kept as they were written. The two around the break are not cases
    (one never closes, one never opens) and nothing is made of them."""
    adapter = Replies(MALFORMED_DRAFT, MALFORMED_DRAFT)
    store = TelemetryStore(tmp_path / "telemetry")
    try:
        current = draft(dataset="emails")
        notes: list[str] = []
        fitted, kept, relaxed = await draft_fitting_cases(
            current, email_spec(), 12, adapter, Runtime(store), notes=notes
        )
    finally:
        store.close()
    assert adapter.calls == 2
    texts = [str(dict(c.case.task)["email_text"]) for c in kept]  # type: ignore[arg-type]
    assert len(kept) == 10 and len(set(texts)) == 10
    assert all(MALFORMED_DRAFT.count(json.dumps(text)) == 1 for text in texts)
    assert not any("didn't authorize" in text or "login button" in text for text in texts)
    assert [c.case.expected for c in kept][:4] == [
        {"category": "billing"}, {"category": "question"}, {"category": "bug report"},
        {"category": "question"},
    ]
    assert all(c.origin == "model_draft" and not c.reviewed for c in kept)
    assert fitted == email_spec() and relaxed == ()
    assert notes == [
        "the reply of the model that writes the exam cases (fake) was not one list of "
        "cases: the 10 complete cases in it are used as they were written, and the rest is "
        "left out"
    ]


async def test_a_salvaged_case_must_fit_the_agents_fields_as_they_are(tmp_path: Path) -> None:
    """A piece of a broken reply never loosens the schema and is never repaired: a case
    outside the listed answers is dropped, and so is one with another input field, one
    with a key of its own, and one that never closes."""
    listed = email_spec({"type": "string", "enum": ["billing", "bug report", "question"]})
    broken = (
        '[{"task": {"email_text": "a"}, "expected": {"category": "billing"}},\n'
        ' {"task": {"email_text": "b"}, "expected": {"category": "refund"}},\n'
        ' {"task": {"body": "c"}, "expected": {"category": "question"}},\n'
        ' {"task": {"email_text": "d"}, "expected": {"category": "question"}, "note": 1},\n'
        ' {"task": {"email_text": "e"}, "expected": {"category": "question"}\n'
        ' {"task": {"email_text": "f"}, "expected": {"category": "question"}}'
    )
    adapter = Replies(broken, broken)
    store = TelemetryStore(tmp_path / "telemetry")
    try:
        current = draft(dataset="emails")
        fitted, kept, _ = await draft_fitting_cases(
            current, listed, 12, adapter, Runtime(store), stated="billing, bug report or question"
        )
    finally:
        store.close()
    assert [c.case.task for c in kept] == [{"email_text": "a"}, {"email_text": "f"}]
    assert fitted == listed


async def test_a_string_limit_is_not_dropped_for_a_salvaged_case(tmp_path: Path) -> None:
    """A whole reply may drop a string limit Stage 0 made up; a piece of a broken one may
    not, so the case that breaks the limit is the one that goes."""
    limited = email_spec({"type": "string", "maxLength": 8})
    broken = (
        '[{"task": {"email_text": "a"}, "expected": {"category": "billing"}},\n'
        ' {"task": {"email_text": "b"}, "expected": {"category": "bug report"}},\n'
        ' {"task": {"email_text": "c"}, "expected": {"category": "question"}},\n'
        ' {"task": {"email_text": "d"}, "expected": {"category": "question"}'
    )
    adapter = Replies(broken, broken)
    store = TelemetryStore(tmp_path / "telemetry")
    try:
        current = draft(dataset="emails")
        fitted, kept, relaxed = await draft_fitting_cases(
            current, limited, 12, adapter, Runtime(store)
        )
    finally:
        store.close()
    assert [c.case.task for c in kept] == [{"email_text": "a"}, {"email_text": "c"}]
    assert fitted == limited and relaxed == ()


async def test_text_inside_a_string_is_never_salvaged_as_a_case(tmp_path: Path) -> None:
    """An email that quotes a case-shaped object is one string, not a case."""
    quoted = json.dumps('{"task": {"email_text": "x"}, "expected": {"category": "billing"}}')
    broken = '[{"task": {"email_text": ' + quoted + '}, "expected": {"category": "question"}'
    adapter = Replies(broken, broken)
    store = TelemetryStore(tmp_path / "telemetry")
    try:
        current = draft(dataset="emails")
        with pytest.raises(DraftUnreadable) as info:
            await draft_fitting_cases(current, email_spec(), 12, adapter, Runtime(store))
    finally:
        store.close()
    assert str(info.value) == "inference_failed" and current.cases == []
    assert info.value.calls == 2 and info.value.model == "fake"


async def test_the_sessions_own_draft_does_not_salvage(tmp_path: Path) -> None:
    """/floor draft stays one call and a strict parse: the operator asks again."""
    adapter = Replies(MALFORMED_DRAFT)
    store = TelemetryStore(tmp_path / "telemetry")
    try:
        current = draft(dataset="emails")
        with pytest.raises(SessionError, match="inference_failed"):
            await draft_cases(current, email_spec(), 12, adapter, Runtime(store))
    finally:
        store.close()
    assert adapter.calls == 1 and current.cases == []


# -- the same input twice -------------------------------------------------------------------


def mail(text: str, category: str) -> dict[str, JsonValue]:
    return {"task": {"email_text": text}, "expected": {"category": category}}


def as_json(*cases: dict[str, JsonValue]) -> str:
    return json.dumps(list(cases))


async def build_draft(  # type: ignore[no-untyped-def]
    tmp_path: Path, *replies: str, count: int = 12, notes: list[str] | None = None
):
    """Build mode's drafting on the email job: the draft, the kept cases, the adapter."""
    adapter = Replies(*replies)
    store = TelemetryStore(tmp_path / "telemetry")
    try:
        current = draft(dataset="emails")
        _, kept, _ = await draft_fitting_cases(
            current, email_spec(), count, adapter, Runtime(store), notes=notes
        )
    finally:
        store.close()
    return current, kept, adapter


def halves(current: FloorDraft) -> tuple[list[JsonValue], list[JsonValue]]:
    """The inputs of practice and of the exam, as the split would make them."""
    tuning, held_out = partition_preview(current)
    by_id = {item.case.case_id: item.case.task for item in current.cases}
    return [by_id[i] for i in tuning], [by_id[i] for i in held_out]


A, B, C, D = (mail(f"mail {name}", "question") for name in "abcd")


async def test_an_array_written_twice_is_salvaged_once(tmp_path: Path) -> None:
    """The review's probe, and the shape of the live reply (an array that closes and opens
    again): every case came back twice, so each exam case was a practice case too. A
    repeated input is one case."""
    restarted = as_json(A, B, C, D) + ",\n" + as_json(A, B, C, D)
    with pytest.raises(json.JSONDecodeError):
        json.loads(restarted)
    current, kept, _ = await build_draft(tmp_path, restarted, restarted)
    assert [c.case.task for c in kept] == [A["task"], B["task"], C["task"], D["task"]]
    practice, exam = halves(current)
    assert practice and exam and not [task for task in exam if task in practice]


async def test_an_input_with_two_different_answers_is_dropped_entirely(tmp_path: Path) -> None:
    """"First try ... Corrected ...": one input, labeled question and then bug report.
    Neither label can be trusted, so the input is not a case at all."""
    revised = (
        "First try: " + as_json(A, B, mail("mail c", "question"))
        + "\nCorrected: " + as_json(A, B, mail("mail c", "bug report"), D)
    )
    current, kept, _ = await build_draft(tmp_path, revised, revised)
    assert [c.case.task for c in kept] == [A["task"], B["task"], D["task"]]
    assert {"email_text": "mail c"} not in [item.case.task for item in current.cases]


async def test_two_answers_are_a_conflict_even_when_one_does_not_fit_the_fields(
    tmp_path: Path,
) -> None:
    """The first label is outside the listed answers and would be dropped on its own. The
    input still came back with two answers, so its second label is not kept either."""
    listed = email_spec({"type": "string", "enum": ["billing", "bug report", "question"]})
    broken = "[" + ", ".join(
        json.dumps(case)
        for case in (A, mail("mail c", "refund"), B, mail("mail c", "billing"), D)
    )
    adapter = Replies(broken, broken)
    store = TelemetryStore(tmp_path / "telemetry")
    try:
        current = draft(dataset="emails")
        _, kept, _ = await draft_fitting_cases(
            current, listed, 12, adapter, Runtime(store), stated="billing, bug report or question"
        )
    finally:
        store.close()
    assert [c.case.task for c in kept] == [A["task"], B["task"], D["task"]]


async def test_a_case_a_think_block_rejected_does_not_reach_practice_or_exam(
    tmp_path: Path,
) -> None:
    """What the model wrote while thinking is not its answer: the label it rejected there
    is not a case, and the one it settled on is."""
    reply = (
        "<think>" + json.dumps(mail("mail c", "billing")) + " no, that one is a question. "
        + json.dumps(mail("mail z", "billing")) + "</think>\n"
        + as_json(A, B, mail("mail c", "question"), D)
    )
    current, kept, _ = await build_draft(tmp_path, reply, reply)
    assert [(c.case.task, c.case.expected) for c in kept] == [
        (A["task"], A["expected"]), (B["task"], B["expected"]),
        ({"email_text": "mail c"}, {"category": "question"}), (D["task"], D["expected"]),
    ]


async def test_a_valid_reply_with_repeats_is_de_duplicated_too(tmp_path: Path) -> None:
    """The same gap on the path every build takes: a whole, valid array that repeats an
    input, once with the same answer and once with another."""
    reply = as_json(
        A, B, mail("mail c", "question"), A, mail("  Mail   C ", "bug report"), D,
        mail("mail e", "billing"), mail("mail e", "billing"),
    )
    notes: list[str] = []
    current, kept, adapter = await build_draft(tmp_path, reply, notes=notes)
    assert adapter.calls == 1 and notes == []
    assert [c.case.task for c in kept] == [
        A["task"], B["task"], D["task"], {"email_text": "mail e"},
    ]
    practice, exam = halves(current)
    assert not [task for task in exam if task in practice]


async def test_the_sessions_draft_never_adds_an_input_the_set_already_has(
    tmp_path: Path,
) -> None:
    """/floor draft into a set that holds cases: an input the operator already labeled is
    not added again, whatever answer the model gave it."""
    adapter = Replies(as_json(A, B, A, mail("mail own", "billing"), C))
    store = TelemetryStore(tmp_path / "telemetry")
    try:
        current = draft(dataset="emails")
        add_case(current, {"case_id": "own", **mail("mail own", "question")}, origin="operator")
        kept = await draft_cases(current, email_spec(), 5, adapter, Runtime(store))
    finally:
        store.close()
    assert [c.case.task for c in kept] == [A["task"], B["task"], C["task"]]
    assert [item.case.expected for item in current.cases][0] == {"category": "question"}
    assert len(current.cases) == 4


# -- a second call that fails ---------------------------------------------------------------


class ThenFails(Replies):
    """Replies in order, then a provider error."""

    def __init__(self, *texts: str) -> None:
        super().__init__(*texts)
        self.given = len(texts)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if len(self.prompts) >= self.given:
            self.prompts.append(request.prompt)
            raise AdapterError("provider_unreachable")
        return await super().complete(request)


async def test_a_first_reply_is_kept_when_the_second_call_fails(tmp_path: Path) -> None:
    """The first reply was not valid JSON and the second call got no answer from the
    provider. The first reply's whole cases are still there to keep."""
    adapter = ThenFails(MALFORMED_DRAFT)
    store = TelemetryStore(tmp_path / "telemetry")
    try:
        current = draft(dataset="emails")
        notes: list[str] = []
        _, kept, _ = await draft_fitting_cases(
            current, email_spec(), 12, adapter, Runtime(store), notes=notes
        )
    finally:
        store.close()
    assert len(kept) == 10 and len(set(map(str, (c.case.task for c in kept)))) == 10
    assert len(notes) == 1 and "10 complete cases" in notes[0]


async def test_no_usable_first_reply_and_a_failed_second_call_is_an_unreadable_draft(
    tmp_path: Path,
) -> None:
    """Nothing to keep and no second reply: the stop is the one that says what happened
    (one answer, not readable), not the generic one."""
    adapter = ThenFails("Here are your cases!")
    store = TelemetryStore(tmp_path / "telemetry")
    try:
        current = draft(dataset="emails")
        with pytest.raises(DraftUnreadable) as info:
            await draft_fitting_cases(current, email_spec(), 12, adapter, Runtime(store))
    finally:
        store.close()
    assert info.value.calls == 1 and info.value.model == "fake" and current.cases == []


async def test_a_reply_nested_too_deep_to_parse_is_asked_for_again(tmp_path: Path) -> None:
    """json raises RecursionError, which is not a ValueError: such a reply is one more
    reply that could not be read, not a failed call."""
    deep = "[" * 30_000
    with pytest.raises(RecursionError):
        json.loads(deep)
    current, kept, adapter = await build_draft(tmp_path, deep, as_json(A, B, C, D), count=4)
    assert adapter.calls == 2 and len(kept) == 4
    assert "could not be used" in adapter.prompts[1]


# -- a schema the user set ------------------------------------------------------------------


def test_fields_the_user_set_are_never_changed_to_fit_a_case_set() -> None:
    """Build mode's Stage 0 never marks a schema as the user's, so this is the rule of
    ``fit_to_set`` itself (new with it): where the user set the input, a set under another
    field name is not kept and the reason says whose field it is; where the user set the
    answers, their types are not taken from the set."""
    from architect.session import floors

    receipts: list[tuple[JsonValue, JsonValue]] = [
        ({"receipt": "TOTAL 5"}, {"total": 5, "tax": None}),
        ({"receipt": "TOTAL 7"}, {"total": 7, "tax": 1}),
    ]
    base = email_spec().model_dump(mode="json")
    base["input_schema"]["properties"] = {"receipt_text": {"type": "string"}}
    base["input_schema"]["required"] = ["receipt_text"]
    base["output_schema"]["properties"] = {"total": {"type": "string"}, "tax": {"type": "string"}}
    base["output_schema"]["required"] = ["total", "tax"]
    inferred = Spec.model_validate(base)
    fit = floors.fit_to_set(inferred, receipts, evidence="a manifest")
    assert fit.why == "" and fit.takes == "receipt" and fit.typed == ("total", "tax")
    assert fit.spec.input_schema["properties"] == {"receipt": {"type": "string"}}
    assert fit.spec.output_schema["properties"] == {
        "total": {"type": "integer"}, "tax": {"type": ["integer", "null"]},
    }
    theirs = Spec.model_validate({**base, "provenance": {"input_schema": "user"}})
    kept = floors.fit_to_set(theirs, receipts, evidence="a manifest")
    assert kept.spec == theirs
    assert kept.why == (
        "its cases take receipt, and this agent takes receipt_text, a field you set"
    )
    typed = Spec.model_validate({**base, "provenance": {"output_schema": "user"}})
    refused = floors.fit_to_set(typed, receipts, evidence="a manifest")
    assert refused.spec == typed
    assert refused.why == "its answers do not fit what this agent gives back"


# -- which configured set is this job's exam ------------------------------------------------

CHECKED = "a manifest and its own checker"
READS = ("total", "subtotal", "tax", "item_count")


def agent(takes: dict[str, JsonValue], gives: dict[str, JsonValue]) -> Spec:
    """A Spec as Stage 0 writes one: strict objects, every field required."""
    def strict(properties: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return {"type": "object", "properties": properties, "required": list(properties),
                "additionalProperties": False}

    base = email_spec().model_dump(mode="json")
    base.update(input_schema=strict(takes), output_schema=strict(gives))
    return Spec.model_validate(base)


SENTIMENT_LABEL: dict[str, JsonValue] = {
    "type": "string", "enum": ["positive", "negative", "neutral"],
}
TOPICS: Pairs = [
    ({"headline": f"headline {i}"}, {"label": label})
    for i, label in enumerate(["sports", "politics", "tech"] * 2)
]
RECEIPT_PAIRS: Pairs = [
    ({"receipt": "TOTAL 5"}, {"total": 5, "subtotal": 4, "tax": None, "item_count": 1}),
    ({"receipt": "TOTAL 7"}, {"total": 7, "subtotal": None, "tax": 1, "item_count": 2}),
]
NULLABLE_INT: dict[str, JsonValue] = {"type": ["integer", "null"]}


@pytest.mark.parametrize("label", [SENTIMENT_LABEL, {"type": "string"}], ids=["enum", "free"])
@pytest.mark.parametrize("evidence", ["", CHECKED], ids=["plain", "checked"])
def test_a_shared_answer_name_does_not_make_a_set_this_jobs_exam(
    label: dict[str, JsonValue], evidence: str
) -> None:
    """The second review's must-fix: a sentiment agent (review -> label) was rebuilt to
    take ``headline`` and licensed on a topic set, because both answers are called
    ``label``. A set with no manifest and no checker of its own is never the reason an
    agent changes its input, and a list of values the agent states is never set aside:
    the topic labels are outside it, so even a checked set is not kept."""
    from architect.session import floors

    spec_ = agent({"review": {"type": "string"}}, {"label": label})
    fit = floors.fit_to_set(spec_, TOPICS, evidence=evidence)
    if evidence and label is not SENTIMENT_LABEL:
        # The one case left: a checked set, the same answer field, and nothing the agent
        # states that its answers break. Documented as the rule's limit.
        assert fit.why == "" and fit.takes == "headline"
        return
    assert fit.spec == spec_ and fit.takes == "" and fit.typed == ()
    if not evidence:
        assert fit.why == "its cases take headline, and this agent takes review"
    else:
        assert fit.why == "its answers do not fit what this agent gives back"


def test_a_set_that_fits_as_it_is_needs_no_evidence_and_changes_nothing() -> None:
    from architect.session import floors

    spec_ = agent({"headline": {"type": "string"}}, {"label": {"type": "string"}})
    fit = floors.fit_to_set(spec_, TOPICS)
    assert fit == floors.SetFit(spec_) and fit.note("topics", 6) == ""


def test_every_case_must_fit_or_the_set_is_not_kept() -> None:
    """One fitting label and five integers under a stated list of values: the set was kept
    with ``type: ["string", "integer"]`` beside the list, so five cases could never be
    matched. All of the cases fit, or the set is not the exam."""
    from architect.session import floors

    mixed: Pairs = [({"review": "fine"}, {"label": "positive"})]
    mixed += [({"review": f"r{i}"}, {"label": i}) for i in range(5)]
    spec_ = agent({"review": {"type": "string"}}, {"label": SENTIMENT_LABEL})
    for evidence in ("", CHECKED):
        fit = floors.fit_to_set(spec_, mixed, evidence=evidence)
        assert fit.spec == spec_ and fit.typed == ()
        assert fit.why == "5 of its 6 answers do not fit what this agent gives back"
    partly: Pairs = [*TOPICS[:4], ({"review": "x"}, {"label": "tech"})]
    spec_ = agent({"headline": {"type": "string"}}, {"label": {"type": "string"}})
    assert floors.fit_to_set(spec_, partly).why == (
        "1 of its 5 cases does not fit what this agent takes"
    )


def test_only_the_fields_whose_answers_do_not_fit_take_the_sets_types() -> None:
    """With ``tax`` alone mistyped, all four fields were listed and a ``number`` that fit
    became ``["integer", "null"]``."""
    from architect.session import floors

    gives: dict[str, JsonValue] = {
        "total": {"type": ["number", "null"], "minimum": 0}, "subtotal": NULLABLE_INT,
        "tax": {"type": "string"}, "item_count": {"type": "number"},
    }
    spec_ = agent({"receipt": {"type": "string"}}, gives)
    fit = floors.fit_to_set(spec_, RECEIPT_PAIRS, evidence=CHECKED, reads=READS)
    assert fit.why == "" and fit.takes == "" and fit.typed == ("tax",)
    assert fit.spec.output_schema["properties"] == {**gives, "tax": NULLABLE_INT}
    assert fit.note("bench-receipts-demo", 200) == (
        "The exam is the case set 'bench-receipts-demo' (200 cases), which has a manifest "
        "and its own checker. To sit it, this agent gives back tax in the types its "
        "answers have, and every case fits after that."
    )
    # Without a manifest or a checker the same set is not this job's exam.
    plain = floors.fit_to_set(spec_, RECEIPT_PAIRS)
    assert plain.spec == spec_
    assert plain.why == "its answers do not fit what this agent gives back"


def test_a_nullable_field_written_as_any_of_keeps_the_set() -> None:
    """``{"anyOf": [{"type": "string"}, {"type": "null"}]}`` on the amounts: a ``type``
    was added beside the ``anyOf``, nothing fit, and the receipts were dropped."""
    from architect.session import floors

    either: dict[str, JsonValue] = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    fits: dict[str, JsonValue] = {"anyOf": [{"type": "integer"}, {"type": "null"}]}
    gives: dict[str, JsonValue] = {
        "total": either, "subtotal": fits, "tax": either, "item_count": {"type": "integer"},
    }
    spec_ = agent({"receipt_text": {"type": "string"}}, gives)
    fit = floors.fit_to_set(spec_, RECEIPT_PAIRS, evidence=CHECKED, reads=READS)
    assert fit.why == "" and fit.takes == "receipt" and fit.typed == ("total", "tax")
    assert fit.spec.output_schema["properties"] == {
        "total": {"type": "integer"}, "subtotal": fits, "tax": NULLABLE_INT,
        "item_count": {"type": "integer"},
    }
    # A branch that says more than a type is not rewritten: the field keeps it and gains
    # a branch for the integers its answers are.
    limited: dict[str, JsonValue] = {
        "anyOf": [{"type": "string", "minLength": 2}, {"type": "null"}],
    }
    spec_ = agent({"receipt": {"type": "string"}}, {**gives, "total": limited})
    more = floors.fit_to_set(spec_, RECEIPT_PAIRS, evidence=CHECKED, reads=READS)
    assert more.why == "" and more.typed == ("tax",) and more.widened == ("total",)
    assert more.spec.output_schema["properties"]["total"] == {  # type: ignore[index,call-overload]
        "anyOf": [{"type": "string", "minLength": 2}, {"type": "null"}, {"type": "integer"}],
    }


def amount(kind: str = "number", **limit: JsonValue) -> dict[str, JsonValue]:
    """An amount as Stage 0 wrote it live (more-examples-5, step R): a number, or a string
    of digits."""
    return {"oneOf": [{"type": kind, **limit},
                      {"type": "string", "pattern": "^\d+(\.\d+)?$"}]}


def test_a_field_that_says_more_than_its_types_gains_a_type_and_keeps_its_limits() -> None:
    """Live, on the commit that first had this rule: Stage 0 wrote each amount as "a number
    or a string of digits" (a ``oneOf`` whose string branch has a pattern). The receipts'
    answers are integers and null; null fits neither branch, the field was left alone
    because a branch says more than a type, and the 200 receipts were dropped for 94
    answers. A type no branch allows at all is added as one more branch: nothing the field
    stated is lost, and every answer fits."""
    from architect.session import floors

    gives: dict[str, JsonValue] = {
        "subtotal": amount(), "tax": amount(), "total": amount(),
        "item_count": amount("integer", minimum=0),
    }
    spec_ = agent({"receipt_text": {"type": "string"}}, gives)
    fit = floors.fit_to_set(spec_, RECEIPT_PAIRS, evidence=CHECKED, reads=READS)
    assert fit.why == "" and fit.takes == "receipt"
    assert fit.typed == () and fit.widened == ("subtotal", "tax")
    null: dict[str, JsonValue] = {"type": "null"}
    assert fit.spec.output_schema["properties"] == {
        "subtotal": {"oneOf": [*amount()["oneOf"], null]},  # type: ignore[misc]
        "tax": {"oneOf": [*amount()["oneOf"], null]},  # type: ignore[misc]
        "total": amount(), "item_count": amount("integer", minimum=0),
    }
    assert fit.note("bench-receipts-demo", 200) == (
        "The exam is the case set 'bench-receipts-demo' (200 cases), which has a manifest "
        "and its own checker. To sit it, this agent takes receipt, as its cases do, and "
        "may also give back subtotal and tax in the types its answers have, and every case "
        "fits after that."
    )
    # A value of a type the field already allows, refused by its limit, adds nothing: the
    # limit stays and the set is not kept.
    low: Pairs = [(task, {**answer, "item_count": -1}) for task, answer in RECEIPT_PAIRS]  # type: ignore[dict-item]
    refused = floors.fit_to_set(spec_, low, evidence=CHECKED, reads=READS)
    assert refused.spec == spec_
    assert refused.why == "its answers do not fit what this agent gives back"
    # In a oneOf, a number beside an integer branch would make every integer fit twice.
    whole: dict[str, JsonValue] = {**gives, "total": amount("integer")}
    halves_: Pairs = [(task, {**answer, "total": 1.5}) for task, answer in RECEIPT_PAIRS]  # type: ignore[dict-item]
    spec_ = agent({"receipt": {"type": "string"}}, whole)
    assert floors.fit_to_set(spec_, halves_, evidence=CHECKED, reads=READS).why == (
        "its answers do not fit what this agent gives back"
    )


def test_a_field_the_sets_checker_does_not_read_keeps_the_set_and_the_note_says_so() -> None:
    """One extra answer field (``currency``) dropped the 200 receipts, although the
    receipt checker reads four fields and ignores the rest."""
    from architect.session import floors

    gives: dict[str, JsonValue] = {name: NULLABLE_INT for name in READS}
    gives["currency"] = {"type": "string"}
    spec_ = agent({"receipt_text": {"type": "string"}}, gives)
    fit = floors.fit_to_set(spec_, RECEIPT_PAIRS, evidence=CHECKED, reads=READS)
    assert fit.why == "" and fit.takes == "receipt" and fit.unread == ("currency",)
    assert fit.spec.output_schema == spec_.output_schema  # currency is still given back
    assert fit.note("bench-receipts-demo", 200) == (
        "The exam is the case set 'bench-receipts-demo' (200 cases), which has a manifest "
        "and its own checker. To sit it, this agent takes receipt, as its cases do, and "
        "every case fits after that. Its checker reads total, subtotal, tax and item_count "
        "and nothing else, so currency is not examined."
    )
    # Under exact equality an answer with one field more can never match: not kept.
    exact = floors.fit_to_set(spec_, RECEIPT_PAIRS, evidence="a manifest")
    assert exact.why == (
        "its answers are total, subtotal, tax and item_count, and this agent gives back "
        "total, subtotal, tax, item_count and currency"
    )


def test_a_near_miss_answer_name_is_not_guessed_at() -> None:
    """``items_count`` for ``item_count``: the checker would look for a field the agent
    never gives back, and renaming on a resemblance is a guess. The set is not kept and
    the reason names both lists."""
    from architect.session import floors

    gives: dict[str, JsonValue] = {
        name: NULLABLE_INT for name in ("total", "subtotal", "tax", "items_count")
    }
    spec_ = agent({"receipt": {"type": "string"}}, gives)
    fit = floors.fit_to_set(spec_, RECEIPT_PAIRS, evidence=CHECKED, reads=READS)
    assert fit.spec == spec_ and fit.why == (
        "its answers are total, subtotal, tax and item_count, and this agent gives back "
        "total, subtotal, tax and items_count"
    )


def test_an_input_the_user_set_as_plain_text_is_not_called_a_field() -> None:
    from architect.session import floors

    base = agent({"x": {"type": "string"}}, {name: NULLABLE_INT for name in READS})
    dump = base.model_dump(mode="json")
    dump["input_schema"] = {"type": "integer"}
    dump["provenance"] = {"input_schema": "user"}
    theirs = Spec.model_validate(dump)
    fit = floors.fit_to_set(theirs, RECEIPT_PAIRS, evidence=CHECKED, reads=READS)
    assert fit.spec == theirs
    assert fit.why == "its cases do not fit the input you set"


def test_a_configured_sets_answers_never_drop_a_list_of_values() -> None:
    """``relax_to_fit`` drops a list of values the user did not give when drafted labels
    break it. For a set the profile already has, that would make any set fit."""
    made_up = agent({"headline": {"type": "string"}}, {"label": SENTIMENT_LABEL})
    assert relax_to_fit(made_up, TOPICS)[0].output_schema["properties"] == {
        "label": {"type": "string"}
    }
    assert relax_to_fit(made_up, TOPICS, enums=False) == (made_up, ())


# -- nits of the second review ---------------------------------------------------------------


async def test_the_same_answer_in_another_case_or_number_form_is_not_a_conflict(
    tmp_path: Path,
) -> None:
    """Inputs were compared without case and white space and answers were not, so "Question"
    and "question" for one input counted as two answers and the input was dropped."""
    reply = as_json(A, B, mail("mail c", "Question "), mail("Mail  C", "question"), D)
    current, kept, _ = await build_draft(tmp_path, reply)
    assert [c.case.task for c in kept] == [
        A["task"], B["task"], {"email_text": "mail c"}, D["task"],
    ]
    from architect.session.floors import _distinct, _DraftItem

    whole = [_DraftItem(task={"n": 1}, expected={"count": 1}),
             _DraftItem(task={"n": 1.0}, expected={"count": 1.0})]
    assert _distinct(whole) == whole[:1]
    apart = [_DraftItem(task={"n": 1}, expected={"count": 1}),
             _DraftItem(task={"n": 1}, expected={"count": 1.5})]
    assert _distinct(apart) == []


@pytest.mark.parametrize(
    ("opens", "closes"),
    [("<thinking>", "</thinking>"), ("<Think>", "</THINK>"), ("<reasoning >", "</reasoning>"),
     ('<think type="scratch">', "</think>")],
)
async def test_other_spellings_of_the_think_tag_are_not_read_as_the_answer(
    tmp_path: Path, opens: str, closes: str
) -> None:
    reply = (
        opens + json.dumps(mail("mail z", "billing")) + closes + "\n"
        + as_json(A, B, C, D)
    )
    current, kept, _ = await build_draft(tmp_path, reply, reply)
    assert [c.case.task for c in kept] == [A["task"], B["task"], C["task"], D["task"]]


async def test_the_note_says_broke_off_only_for_a_reply_that_did(tmp_path: Path) -> None:
    """The live reply closed its array and opened it again, and a reply wrapped in prose
    is whole too: nothing broke off in either. Only a reply that ends inside a case or
    inside the list was cut short."""
    lead = "the reply of the model that writes the exam cases (fake) "
    tail = ": the 4 complete cases in it are used as they were written, and the rest is left out"
    wrapped = "Here are the cases:\n" + as_json(A, B, C, D) + "\nLet me know."
    reopened = as_json(A, B) + ",\n" + as_json(C, D)
    cut = as_json(A, B, C, D)[:-1] + ', {"task": {"email_text": "mail e"}, "expec'
    for index, (reply, said) in enumerate((
        (wrapped, "was not one list of cases"),
        (reopened, "was not one list of cases"),
        (cut, "broke off partway"),
    )):
        notes: list[str] = []
        folder = tmp_path / str(index)
        folder.mkdir()
        _, kept, _ = await build_draft(folder, reply, reply, notes=notes)
        assert len(kept) == 4 and notes == [lead + said + tail]
