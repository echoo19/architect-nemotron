"""Spec edits: strict re-validation, provenance marks, diffs and blocker arithmetic."""

from __future__ import annotations

import pytest
from pydantic import JsonValue, ValidationError

from architect.profiles import Profile
from architect.session.codes import SessionError
from architect.session.edit import (
    Blocker,
    blockers,
    cents,
    diff_lines,
    floor_alternative,
    normalize_schema,
    parse_value,
    schema_missing,
    set_field,
    suggested_cost,
    unset_field,
    validation_fields,
)
from architect.spec import Budget, FloorDefinition, FloorKind, Lifecycle, Spec


def make_spec(**overrides: object) -> Spec:
    fields: dict[str, object] = {
        "task": "Label the sentiment of a short product review.",
        "input_schema": {"type": "object", "properties": {"review": {"type": "string"}}},
        "output_schema": {"type": "object", "properties": {"label": {"type": "string"}}},
        "floor": FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.9, dataset="tasks"),
        "budget": Budget(cost_usd=0.1, compute_s=60),
        "model_target": "fake",
        "lifecycle": Lifecycle.INSTALL,
        "provenance": {"task": "inferred", "budget": "inferred"},
    }
    fields.update(overrides)
    return Spec.model_validate(fields)


@pytest.fixture
def profile() -> Profile:
    return Profile.model_validate(
        {
            "models": {"fake": {"provider": "fake"}},
            "floors": {"tasks": {"kind": "held_out_tasks", "path": "cases.json"}},
            "compile": {"max_candidates": 2, "compile_budget": {"cost_usd": 5, "compute_s": 1800}},
        }
    )


def test_parse_value_json_or_raw_string() -> None:
    assert parse_value("0.7") == 0.7
    assert parse_value('{"a": [1, 2]}') == {"a": [1, 2]}
    assert parse_value("true") is True
    assert parse_value("null") is None
    assert parse_value("hello world") == "hello world"
    assert parse_value("NaN") == "NaN"
    assert parse_value("Infinity") == "Infinity"
    with pytest.raises(SessionError) as info:
        parse_value("   ")
    assert str(info.value) == "invalid_value_json"


def test_nested_set_creates_intermediate_dicts_and_marks_user() -> None:
    spec = make_spec()
    new, override = set_field(spec, "input_schema.properties.stars.type", "integer")
    assert new.input_schema["properties"]["stars"] == {"type": "integer"}
    assert new.input_schema["properties"]["review"] == {"type": "string"}
    assert new.provenance["input_schema"] == "user"
    assert new.provenance["task"] == "inferred"
    assert override == {"input_schema": new.input_schema}
    assert new.spec_hash != spec.spec_hash
    deeper, _ = set_field(spec, "execution_config.retry.limits.max", 3)
    assert deeper.execution_config == {"retry": {"limits": {"max": 3}}}


def test_whole_schema_replacement_and_submodel_fields() -> None:
    spec = make_spec()
    schema = {"type": "object", "required": ["label"]}
    new, override = set_field(spec, "output_schema", schema)
    assert new.output_schema == schema
    assert override == {"output_schema": schema}
    budgeted, override = set_field(spec, "budget.cost_usd", 0.7)
    assert budgeted.budget.cost_usd == 0.7
    assert budgeted.budget.compute_s == 60
    assert budgeted.provenance["budget"] == "user"
    assert override == {"budget": {"cost_usd": 0.7, "compute_s": 60.0}}
    floored, _ = set_field(spec, "floor.limit", 0.8)
    assert floored.floor.limit == 0.8
    secondary, _ = set_field(
        spec, "secondary_floor", {"kind": "held_out_tasks", "limit": 0.5, "dataset": "b"}
    )
    assert secondary.secondary_floor is not None
    assert secondary.secondary_floor.dataset == "b"
    filled, _ = set_field(secondary, "secondary_floor.limit", 0.6)
    assert filled.secondary_floor is not None
    assert filled.secondary_floor.limit == 0.6


def test_every_edit_changes_the_hash() -> None:
    spec = make_spec()
    seen = {spec.spec_hash}
    for path, value in [
        ("budget.cost_usd", 0.5),
        ("task", "Something else"),
        ("model_target", "other"),
        ("lifecycle", "expire"),
        ("tools", ["search"]),
        ("floor.dataset", "renamed"),
    ]:
        spec, _ = set_field(spec, path, value)
        assert spec.spec_hash not in seen
        seen.add(spec.spec_hash)


@pytest.mark.parametrize(
    "path", ["provenance", "provenance.task", "foo", "budget.tokens", "task.x", "floor.nope"]
)
def test_unknown_fields_are_refused_and_nothing_changes(path: str) -> None:
    spec = make_spec()
    before = spec.spec_hash
    with pytest.raises(SessionError) as info:
        set_field(spec, path, 1)
    assert str(info.value) == "unknown_field"
    assert spec.spec_hash == before
    assert spec.provenance == {"task": "inferred", "budget": "inferred"}


def test_invalid_edit_carries_field_paths_only() -> None:
    spec = make_spec()
    with pytest.raises(SessionError) as info:
        set_field(spec, "floor.limit", "sk-secret-value")
    assert str(info.value) == "invalid_edit"
    assert info.value.fields == ("floor.limit",)
    assert "sk-secret" not in repr(info.value.fields)
    with pytest.raises(SessionError) as info:
        set_field(spec, "budget", {"cost_usd": -1, "compute_s": "x"})
    assert set(info.value.fields) == {"budget.cost_usd", "budget.compute_s"}
    # Strict validation: a numeric string is not a number.
    with pytest.raises(SessionError):
        set_field(spec, "budget.cost_usd", "0.7")


def test_unset_optional_and_required_fields() -> None:
    spec = make_spec(
        secondary_floor=FloorDefinition(kind=FloorKind.LLM_JUDGE, limit=0.5, dataset="b")
    )
    new, override = unset_field(spec, "secondary_floor")
    assert new.secondary_floor is None
    assert override == {"secondary_floor": None}
    assert new.provenance["secondary_floor"] == "user"
    assert new.spec_hash != spec.spec_hash
    reset, override = unset_field(make_spec(model_target="other", tools=("a",)), "model_target")
    assert reset.model_target == "fake"
    assert override == {"model_target": "fake"}
    tools, override = unset_field(make_spec(tools=("a",)), "tools")
    assert tools.tools == ()
    assert override == {"tools": []}
    for required in ("task", "input_schema", "output_schema", "floor", "budget"):
        with pytest.raises(SessionError) as info:
            unset_field(spec, required)
        assert str(info.value) == "invalid_edit"
        assert info.value.fields == (required,)
    for bad in ("provenance", "foo", "budget.cost_usd"):
        with pytest.raises(SessionError) as info:
            unset_field(spec, bad)
        assert str(info.value) == "unknown_field"


def test_diff_lines_golden() -> None:
    spec = make_spec()
    edited, _ = set_field(spec, "budget.cost_usd", 0.7)
    assert diff_lines(spec, edited) == ["- budget.cost_usd 0.1", "+ budget.cost_usd 0.7"]
    assert diff_lines(spec, spec) == []
    provenance_only = spec.model_copy(update={"provenance": {"task": "user"}})
    assert diff_lines(spec, provenance_only) == []
    nested, _ = set_field(spec, "input_schema.properties.stars", {"type": "integer"})
    assert diff_lines(spec, nested) == ['+ input_schema.properties.stars.type "integer"']
    tools, _ = set_field(spec, "tools", ["search", "calc"])
    assert diff_lines(spec, tools) == ["- tools []", '+ tools ["search","calc"]']
    renamed, _ = set_field(spec, "task", "café ✓")
    assert diff_lines(spec, renamed) == [
        '- task "Label the sentiment of a short product review."',
        '+ task "caf\\u00e9 \\u2713"',
    ]


def test_suggested_cost_rounds_up_to_cents() -> None:
    assert suggested_cost(0.35, 2) == 0.7
    assert suggested_cost(0.333, 3) == 1.0
    assert suggested_cost(0.001, 2) == 0.01
    assert suggested_cost(0.0, 2) == 0.0
    assert suggested_cost(1.005, 1) == 1.01


def test_no_blockers_for_a_ready_spec(profile: Profile) -> None:
    assert blockers(make_spec(budget=Budget(cost_usd=0.7, compute_s=60)), profile, 0.35) == ()
    assert blockers(make_spec(), profile, None) == ()


def test_each_blocker_code_and_fix(profile: Profile) -> None:
    missing = make_spec(
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.9, dataset="sentiment-v1")
    )
    assert blockers(missing, profile, None) == (
        Blocker("floor_not_configured", "floor", True, "set floor.dataset tasks"),
    )
    kind = make_spec(floor=FloorDefinition(kind=FloorKind.LLM_JUDGE, limit=0.9, dataset="tasks"))
    assert blockers(kind, profile, None) == (
        Blocker("floor_kind_mismatch", "set floor.kind held_out_tasks", True),
    )
    secondary = make_spec(
        secondary_floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.5, dataset="nope")
    )
    assert blockers(secondary, profile, None) == (
        Blocker("secondary_floor_not_configured", "unset secondary_floor", True),
    )
    model = make_spec(model_target="gpt-9")
    assert blockers(model, profile, None) == (
        Blocker("model_not_configured", "set model_target fake", True),
    )
    assert blockers(make_spec(model_target="cheapest that passes"), profile, None) == ()
    assert blockers(make_spec(), profile, 0.35) == (
        Blocker("budget_below_quote", "set budget.cost_usd 0.70", True),
    )
    # The fix is always two decimals (Section 3.5), even when the ceiling is a whole dollar.
    assert blockers(make_spec(), profile, 0.5) == (
        Blocker("budget_below_quote", "set budget.cost_usd 1.00", True),
    )
    assert blockers(make_spec(), profile, 0.1) == ()
    deadline = make_spec(budget=Budget(cost_usd=0.1, compute_s=1000))
    assert blockers(deadline, profile, None) == (
        Blocker("compile_deadline_short", "budget compile 5 4000", False),
    )


def test_floor_alternative_lists_the_configured_floors(profile: Profile) -> None:
    """One floor is the exact command; several are a choice; none means no second line."""
    assert floor_alternative(profile) == "set floor.dataset tasks"
    missing = make_spec(
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.9, dataset="tickets-v1")
    )
    two = Profile.model_validate(
        {
            "models": {"fake": {"provider": "fake"}},
            "floors": {
                "tasks": {"kind": "held_out_tasks", "path": "cases.json"},
                "sentiment-v1": {"kind": "held_out_tasks", "path": "s.json"},
            },
        }
    )
    assert floor_alternative(two) == "set floor.dataset <tasks|sentiment-v1>"
    assert blockers(missing, two, None) == (
        Blocker("floor_not_configured", "floor", True, "set floor.dataset <tasks|sentiment-v1>"),
    )
    none = Profile.model_validate({"models": {"fake": {"provider": "fake"}}})
    assert floor_alternative(none) is None
    assert blockers(missing, none, None) == (Blocker("floor_not_configured", "floor", True),)
    assert blockers(missing, none, None)[0].alternative is None
    # Only floor_not_configured carries an alternative; a kind mismatch has one fix.
    kind = make_spec(floor=FloorDefinition(kind=FloorKind.LLM_JUDGE, limit=0.9, dataset="tasks"))
    assert blockers(kind, profile, None)[0].alternative is None


def test_blockers_come_in_contract_order(profile: Profile) -> None:
    spec = make_spec(
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.9, dataset="missing"),
        secondary_floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.5, dataset="x"),
        model_target="gpt-9",
        budget=Budget(cost_usd=0.1, compute_s=1000),
    )
    codes = [b.code for b in blockers(spec, profile, 0.35)]
    assert codes == [
        "floor_not_configured",
        "secondary_floor_not_configured",
        "model_not_configured",
        "budget_below_quote",
        "compile_deadline_short",
    ]
    assert [b.blocking for b in blockers(spec, profile, 0.35)] == [True, True, True, True, False]


# ------------------------------------------------------------ coverage: parse_value edge cases


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("  0.7  ", 0.7),  # surrounding whitespace is still JSON
        ('"quoted"', "quoted"),  # a JSON string loses its quotes
        ("1e3", 1000.0),
        ("007", "007"),  # leading zeros are not JSON: kept verbatim
        ("[1, 2", "[1, 2"),  # unterminated JSON is plain text
        ('{"a": NaN}', '{"a": NaN}'),  # a constant anywhere inside keeps the whole text raw
        ("-Infinity", "-Infinity"),
        ("0.7 usd", "0.7 usd"),  # trailing junk makes the whole text a string
    ],
)
def test_parse_value_edge_cases(text: str, expected: object) -> None:
    assert parse_value(text) == expected


@pytest.mark.parametrize("text", ["", "\t\n", "   "])
def test_parse_value_blank_text_is_invalid_value_json(text: str) -> None:
    with pytest.raises(SessionError) as info:
        parse_value(text)
    assert info.value.code == "invalid_value_json"
    assert info.value.fields == ()


# ------------------------------------------------------------ coverage: validation_fields shapes


def test_validation_fields_dedupes_joins_indices_and_never_carries_input() -> None:
    exc = ValidationError.from_exception_data(
        "Spec",
        [
            {"type": "missing", "loc": ("budget", "cost_usd"), "input": {"secret": "sk-1"}},
            {"type": "float_type", "loc": ("budget", "cost_usd"), "input": "sk-2"},
            {"type": "string_type", "loc": ("tools", 0), "input": "sk-3"},
            {"type": "missing", "loc": (), "input": "sk-4"},
        ],
    )
    fields = validation_fields(exc)
    assert fields == ("budget.cost_usd", "tools.0", "")
    assert "sk-" not in repr(fields)


def test_invalid_edit_names_list_indices_and_nested_submodel_fields() -> None:
    spec = make_spec()
    with pytest.raises(SessionError) as info:
        set_field(spec, "tools", ["ok", 1, ""])
    assert info.value.code == "invalid_edit"
    assert info.value.fields == ("tools.1", "tools.2")
    with pytest.raises(SessionError) as info:
        set_field(spec, "pipeline", [{"name": "a"}])
    assert info.value.fields == ("pipeline.0.preset", "pipeline.0.model", "pipeline.0.budget")
    with pytest.raises(SessionError) as info:
        set_field(spec, "floor.kind", "nope")
    assert info.value.fields == ("floor.kind",)


# ------------------------------------------------------------ coverage: set_field shapes


def test_setting_a_sub_field_of_an_absent_secondary_floor_is_invalid_edit() -> None:
    """``secondary_floor`` is None by default: ``set secondary_floor.limit`` builds a bare
    sub-model and strict validation names the sibling fields that are now missing."""
    spec = make_spec()
    assert spec.secondary_floor is None
    before = spec.spec_hash
    with pytest.raises(SessionError) as info:
        set_field(spec, "secondary_floor.limit", 0.6)
    assert info.value.code == "invalid_edit"
    assert info.value.fields == ("secondary_floor.kind", "secondary_floor.dataset")
    assert spec.spec_hash == before
    assert spec.provenance == {"task": "inferred", "budget": "inferred"}


def test_a_non_finite_float_value_is_invalid_edit_without_fields() -> None:
    spec = make_spec()
    for value in (float("nan"), float("inf"), -float("inf")):
        with pytest.raises(SessionError) as info:
            set_field(spec, "budget.cost_usd", value)
        assert info.value.code == "invalid_edit"
        assert info.value.fields == ()
    assert spec.budget.cost_usd == 0.1


def test_a_scalar_intermediate_is_replaced_by_a_dict_on_a_deeper_set() -> None:
    spec = make_spec()
    flat, _ = set_field(spec, "execution_config.retry", 2)
    assert flat.execution_config == {"retry": 2}
    deep, override = set_field(flat, "execution_config.retry.max", 3)
    assert deep.execution_config == {"retry": {"max": 3}}
    assert override == {"execution_config": {"retry": {"max": 3}}}
    assert deep.provenance["execution_config"] == "user"


def test_unset_leaves_other_provenance_marks_and_resets_every_optional_default() -> None:
    spec = make_spec(
        domain="reviews",
        lifecycle=Lifecycle.INSTALL,
        execution_config={"retry": 2},
        provenance={"task": "inferred", "domain": "inferred"},
    )
    for path, default in [
        ("domain", "general"),
        ("lifecycle", "expire"),
        ("execution_config", {}),
        ("preset_hint", None),
        ("pipeline", []),
    ]:
        reset, override = unset_field(spec, path)
        assert override == {path: default}
        assert reset.provenance[path] == "user"
        assert reset.provenance["task"] == "inferred"


# ------------------------------------------------------------ coverage: diff_lines leaves


def test_diff_lines_for_removed_leaves_and_dict_list_leaves() -> None:
    spec = make_spec(
        secondary_floor=FloorDefinition(kind=FloorKind.LLM_JUDGE, limit=0.5, dataset="b")
    )
    unset, _ = unset_field(spec, "secondary_floor")
    assert diff_lines(spec, unset) == [
        '- secondary_floor.kind "llm_judge"',
        "- secondary_floor.limit 0.5",
        '- secondary_floor.dataset "b"',
        "- secondary_floor.minimum_samples 30",
        "- secondary_floor.rubric null",
        "- secondary_floor.judge_model null",
        "- secondary_floor.calibration_dataset null",
        "+ secondary_floor null",
    ]
    # Reversed, the same leaves come back as additions after the removed null leaf.
    assert diff_lines(unset, spec) == [
        "- secondary_floor null",
        '+ secondary_floor.kind "llm_judge"',
        "+ secondary_floor.limit 0.5",
        '+ secondary_floor.dataset "b"',
        "+ secondary_floor.minimum_samples 30",
        "+ secondary_floor.rubric null",
        "+ secondary_floor.judge_model null",
        "+ secondary_floor.calibration_dataset null",
    ]
    base = make_spec()
    configured, _ = set_field(base, "execution_config.retry", 2)
    assert diff_lines(base, configured) == ["- execution_config {}", "+ execution_config.retry 2"]
    step = {"name": "a", "preset": "chain", "model": "fake", "budget": {"cost_usd": 0.1}}
    piped, _ = set_field(base, "pipeline", [step])
    assert diff_lines(base, piped) == [
        "- pipeline []",
        '+ pipeline [{"budget":{"compute_s":60.0,"cost_usd":0.1},"input_schema":{},'
        '"model":"fake","name":"a","output_schema":{},"params":{},"preset":"chain"}]',
    ]


# ------------------------------------------------------------ coverage: blockers and rounding


def test_secondary_floor_blocker_only_when_unconfigured_or_kind_differs(profile: Profile) -> None:
    matching = make_spec(
        secondary_floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.5, dataset="tasks")
    )
    assert blockers(matching, profile, None) == ()
    kind = make_spec(
        secondary_floor=FloorDefinition(kind=FloorKind.LLM_JUDGE, limit=0.5, dataset="tasks")
    )
    assert blockers(kind, profile, None) == (
        Blocker("secondary_floor_not_configured", "unset secondary_floor", True),
    )


def test_model_not_configured_for_cheapest_that_passes_with_an_empty_ladder() -> None:
    profile = Profile.model_validate(
        {
            "models": {"fake": {"provider": "fake"}, "other": {"provider": "fake"}},
            "floors": {"tasks": {"kind": "held_out_tasks", "path": "cases.json"}},
            "compile": {"model_ladder": []},
        }
    )
    spec = make_spec(model_target="cheapest that passes")
    assert blockers(spec, profile, None) == (
        Blocker("model_not_configured", "set model_target fake", True),
    )


def test_compile_deadline_fix_uses_plain_fractional_numbers() -> None:
    profile = Profile.model_validate(
        {
            "models": {"fake": {"provider": "fake"}},
            "floors": {"tasks": {"kind": "held_out_tasks", "path": "cases.json"}},
            "compile": {"compile_budget": {"cost_usd": 2.5, "compute_s": 0.5}},
        }
    )
    short = make_spec(budget=Budget(cost_usd=0.1, compute_s=0.3))
    assert blockers(short, profile, None) == (
        Blocker("compile_deadline_short", "budget compile 2.5 1.2", False),
    )
    longer = make_spec(budget=Budget(cost_usd=0.1, compute_s=0.33333))
    assert blockers(longer, profile, None) == (
        Blocker("compile_deadline_short", "budget compile 2.5 1.3333", False),
    )
    # Exactly twice the per-run compute is not short.
    assert blockers(make_spec(budget=Budget(cost_usd=0.1, compute_s=0.25)), profile, None) == ()


def test_suggested_cost_and_cents_rounding() -> None:
    assert suggested_cost(0.333, 2) == 0.67
    assert suggested_cost(0.57, 2) == 1.14
    assert suggested_cost(0.5, 3) == 1.5
    assert suggested_cost(0.12, 3) == 0.36
    assert cents(1.5) == "1.50"
    assert cents(0.7) == "0.70"
    assert cents(suggested_cost(0.333, 3)) == "1.00"


# ------------------------------------------------------------ schema normalization and blockers

SHAPE = '{"type":"object","properties":{...},"required":[...],"additionalProperties":false}'


@pytest.mark.parametrize(
    ("value", "expected", "changed"),
    [
        (
            {"review": {"type": "string"}},
            {
                "type": "object",
                "properties": {"review": {"type": "string"}},
                "required": ["review"],
                "additionalProperties": False,
            },
            True,
        ),
        (
            {"score": {"type": "number"}, "label": {"enum": ["neg", "pos"]}},
            {
                "type": "object",
                "properties": {"score": {"type": "number"}, "label": {"enum": ["neg", "pos"]}},
                "required": ["label", "score"],  # sorted, not in proposal order
                "additionalProperties": False,
            },
            True,
        ),
        ({}, {}, False),
        ({"type": "object"}, {"type": "object"}, False),
        ({"type": "object", "properties": {}}, {"type": "object", "properties": {}}, False),
        ({"properties": {"a": {}}}, {"properties": {"a": {}}}, False),  # already a schema
        ({"$schema": {}, "a": {}}, {"$schema": {}, "a": {}}, False),
        ({"oneOf": {}}, {"oneOf": {}}, False),
        ({"anyOf": {}}, {"anyOf": {}}, False),
        ({"allOf": {}}, {"allOf": {}}, False),
        ({"enum": {}}, {"enum": {}}, False),
        ({"items": {}}, {"items": {}}, False),
        ({"review": "string"}, {"review": "string"}, False),  # a value that is not a dict
        ({"a": {"type": "string"}, "b": 1}, {"a": {"type": "string"}, "b": 1}, False),
        ("string", "string", False),
        (["a"], ["a"], False),
        (None, None, False),
        (3, 3, False),
    ],
)
def test_normalize_schema_table(value: JsonValue, expected: JsonValue, changed: bool) -> None:
    assert normalize_schema(value) == (expected, changed)


def test_normalize_schema_never_mutates_its_input() -> None:
    bare: dict[str, JsonValue] = {"review": {"type": "string"}}
    wrapped, changed = normalize_schema(bare)
    assert changed and bare == {"review": {"type": "string"}}
    assert isinstance(wrapped, dict) and wrapped["properties"] is not bare


@pytest.mark.parametrize(
    ("schema", "missing"),
    [
        ({}, True),
        ({"type": "object"}, True),
        ({"type": "object", "properties": {}}, True),
        ({"type": "object", "additionalProperties": False}, True),
        ({"type": "string"}, False),
        ({"type": "object", "properties": {"a": {"type": "string"}}}, False),
        ({"properties": {"a": {"type": "string"}}}, False),
        ({"review": {"type": "string"}}, False),  # a bare map is not "missing"; it is wrapped
        ({"oneOf": [{"type": "string"}]}, False),
    ],
)
def test_schema_missing_table(schema: dict[str, JsonValue], missing: bool) -> None:
    assert schema_missing(schema) is missing


def test_schema_blockers_for_empty_and_property_less_schemas(profile: Profile) -> None:
    """{} and a property-less object block with the full JSON Schema shape as the fix."""
    empty_in = make_spec(input_schema={})
    assert blockers(empty_in, profile, None) == (
        Blocker("input_schema_missing", f"set input_schema {SHAPE}", True),
    )
    bare_out = make_spec(output_schema={"type": "object"})
    assert blockers(bare_out, profile, None) == (
        Blocker("output_schema_missing", f"set output_schema {SHAPE}", True),
    )
    both = make_spec(
        input_schema={"type": "object", "properties": {}},
        output_schema={},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.9, dataset="missing"),
    )
    assert [b.code for b in blockers(both, profile, 0.35)] == [
        "input_schema_missing",
        "output_schema_missing",
        "floor_not_configured",
        "budget_below_quote",
    ]
    assert all(b.blocking for b in blockers(both, profile, 0.35))
    assert all(b.alternative is None for b in blockers(both, profile, 0.35)[:2])
    # A schema that names its properties is enough; the shape of the rest is not judged.
    assert blockers(make_spec(output_schema={"type": "string"}), profile, None) == ()
    assert blockers(make_spec(output_schema={"properties": {"a": {}}}), profile, None) == ()


def test_setting_a_full_schema_clears_the_blocker_and_changes_the_hash(profile: Profile) -> None:
    spec = make_spec(output_schema={})
    assert [b.code for b in blockers(spec, profile, None)] == ["output_schema_missing"]
    fixed, top = set_field(
        spec,
        "output_schema",
        {
            "type": "object",
            "properties": {"label": {"type": "string"}},
            "required": ["label"],
            "additionalProperties": False,
        },
    )
    assert blockers(fixed, profile, None) == ()
    assert fixed.spec_hash != spec.spec_hash
    assert fixed.provenance["output_schema"] == "user" and set(top) == {"output_schema"}
