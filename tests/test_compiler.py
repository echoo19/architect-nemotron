from pathlib import Path

import pytest
from pydantic import ValidationError

from architect.compiler import CompileConfig, Compiler
from architect.compiler import types as compiler_types
from architect.compiler.forge_settings import ForgeSettings
from architect.floors import EvaluationCase, HeldOutTasks
from architect.models import FakeAdapter, ModelResponse
from architect.runtime import Runtime
from architect.spec import Budget, FloorDefinition, FloorKind, Lifecycle, Spec
from architect.telemetry import TelemetryStore


def specification(**overrides: object) -> Spec:
    values = dict(
        task="Return ok",
        input_schema={"type": "object"},
        output_schema={"type": "string"},
        domain="synthesizer",
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="tasks"),
        budget=Budget(),
    )
    values.update(overrides)
    return Spec.model_validate(values)


def factory(definition: FloorDefinition) -> HeldOutTasks:
    return HeldOutTasks(
        definition, [EvaluationCase(case_id=f"case-{i}", task={}, expected="ok") for i in range(6)]
    )


def compiler(tmp_path: Path, **options: object) -> Compiler:
    return Compiler(
        tmp_path / "artifacts",
        Runtime(TelemetryStore(tmp_path / "runs")),
        lambda model: FakeAdapter(model=model),
        factory,
        **options,
    )


async def test_explicit_install_needs_approval(tmp_path: Path) -> None:
    service = compiler(tmp_path)
    spec = specification(lifecycle=Lifecycle.INSTALL)
    preview = await service.compile(spec)
    assert preview.approval_required and preview.artifact is None
    assert not service.runtime.store.rows()
    result = await service.compile(spec, approved=True)
    assert result.artifact is not None and result.artifact.licensed


async def test_ladder_chooses_measured_cheapest_not_first(tmp_path: Path) -> None:
    service = compiler(tmp_path, config=CompileConfig(model_ladder=("fake", "llama3.2")))
    result = await service.compile(specification(model_target="cheapest that passes"))
    assert result.artifact is not None and result.artifact.licensed
    assert result.artifact.model == "llama3.2"
    assert result.report is not None and result.report.cost_usd == 0


async def test_explicit_target_does_not_use_ladder(tmp_path: Path) -> None:
    service = compiler(tmp_path, config=CompileConfig(model_ladder=("llama3.2",)))
    result = await service.compile(specification())
    assert result.artifact is not None and result.artifact.model == "fake"


@pytest.mark.parametrize("strategy", ["grid", "halving"])
async def test_search_strategies_full_pipeline(tmp_path: Path, strategy: str) -> None:
    service = compiler(tmp_path, config=CompileConfig.model_validate({"strategy": strategy}))
    result = await service.compile(specification())
    assert result.artifact is not None and result.artifact.licensed
    assert result.report is not None and result.report.held_out_count == 3


async def test_empty_ladder_and_selection_fail_cleanly(tmp_path: Path) -> None:
    service = compiler(tmp_path, config=CompileConfig(model_ladder=()))
    result = await service.compile(specification(model_target="cheapest that passes"))
    assert result.failures == ("empty_model_ladder",)
    result = await service.compile(
        specification(domain="unknown", output_schema={"type": "number"})
    )
    assert result.failures == ("selection_failed",)


async def test_global_compile_budget_cannot_be_evaded_by_more_candidates(tmp_path: Path) -> None:
    service = compiler(tmp_path, config=CompileConfig(compile_budget=Budget(cost_usd=0.000035)))
    result = await service.compile(specification())
    assert result.artifact is None or not result.artifact.licensed
    assert result.failures
    spent = sum(row.cost_usd for row in service.runtime.store.rows())
    assert 0 < spent <= 0.000035


async def test_high_token_candidate_licenses_and_runs_without_legacy_quota(tmp_path: Path) -> None:
    spec = specification(budget={"tokens": 1, "cost_usd": 0.1})
    assert "tokens" not in spec.budget.model_dump()

    def high_usage(model: str) -> FakeAdapter:
        return FakeAdapter(model=model, responses=[ModelResponse(
            text="ok", model=model, tokens_in=1_000_000, tokens_out=1_000_000,
            cost_usd=0.00001,
        )])

    service = Compiler(
        tmp_path / "artifacts", Runtime(TelemetryStore(tmp_path / "runs")),
        high_usage, factory, config=CompileConfig(max_candidates=1),
    )
    result = await service.compile(spec)
    assert result.artifact is not None and result.artifact.licensed
    assert result.report is not None and result.report.passed
    assert result.report.tokens == 2_000_000
    run = await service.runtime.run(result.artifact, {}, high_usage("fake"))
    assert run.output == "ok"
    measured = [row for row in service.runtime.store.rows() if row.tokens_in]
    assert measured and all(row.accepted for row in measured)
    assert all(row.tokens_in + row.tokens_out == 2_000_000 for row in measured)


async def test_safe_numeric_configuration_telemetry(tmp_path: Path) -> None:
    service = compiler(tmp_path)
    result = await service.compile(specification())
    assert result.artifact is not None
    rows = service.runtime.store.rows()
    measured = [row for row in rows if row.tokens_in]
    assert measured and all("max_output_tokens" not in row.model_params for row in measured)
    assert all(row.floor_score is None for row in measured)


async def test_stage_listener_sees_stages_in_order_for_inferred_compile(tmp_path: Path) -> None:
    proposal = specification().model_dump_json()
    inference = FakeAdapter(responses=[ModelResponse(
        text=proposal, model="fake", tokens_in=8, tokens_out=12, cost_usd=0.00002,
    )])
    service = compiler(tmp_path, inference_adapter=inference)
    seen: list[int] = []
    service.stage_listener = seen.append
    try:
        result = await service.compile("Return ok")
    finally:
        service.stage_listener = None
    assert result.artifact is not None and result.artifact.licensed
    assert seen == [0, 1, 2, 4] and result.stages == (0, 1, 2, 4)
    assert Compiler.stage_listener is None, "the class default is untouched"


async def test_raising_stage_listener_does_not_change_the_result(tmp_path: Path) -> None:
    def explode(stage: int) -> None:
        raise RuntimeError("observer bug")

    quiet = compiler(tmp_path / "quiet")
    expected = await quiet.compile(specification())
    loud = compiler(tmp_path / "loud")
    loud.stage_listener = explode
    result = await loud.compile(specification())
    assert result.stages == expected.stages == (1, 2, 4)
    assert result.failures == expected.failures == ()
    assert result.artifact is not None and result.artifact.licensed
    assert result.report is not None and expected.report is not None
    assert result.report.held_out_count == expected.report.held_out_count == 3


def test_forge_settings_defaults_are_the_contract() -> None:
    settings = ForgeSettings()
    assert compiler_types.ForgeSettings is ForgeSettings
    assert settings.model_dump() == {
        "enabled": False,
        "mode": "agent",
        "trigger": "failed",
        "agent_model": None,
        "template": None,
        "feedback": "per_case",
        "max_usd": 5.0,
        "max_s": 3600.0,
        "max_checks": 200,
        "max_dryruns": 100,
        "max_measures": 24,
        "measure_cases": 8,
        "max_submissions": 6,
        "max_spawns": 3,
        "max_depth": 1,
        "spawn_share": 0.25,
        "spawn_max_usd": 2.0,
        "pi_max_turns": 200,
        "context_chars": 60000,
        "case_chars": 2000,
        "call_limit": 128,
        "calls_per_turn": 2,
        "fuel": 10_000_000,
        "max_verifications_per_spec": 10,
        "agent_max_tokens": 12288,
        "max_cases_shown": 16,
        "max_failures_shown": 8,
        "max_rungs": None,
    }
    with pytest.raises(ValidationError):
        settings.enabled = True  # type: ignore[misc]


@pytest.mark.parametrize(
    "values",
    [
        {"mode": "template", "template": "prompt_chain", "trigger": "always"},
        {"template": "extract_verify", "feedback": "aggregate", "agent_model": "local"},
        {"template": "vision_two_pass"},
        {"template": "game_explorer"},
        {"template": "game_hypothesis"},
        {"max_usd": 1000, "max_s": 86400, "max_checks": 1, "max_dryruns": 0},
        {"max_measures": 256, "measure_cases": 32, "max_submissions": 32, "max_spawns": 8},
        {"max_depth": 2, "spawn_share": 0.5, "spawn_max_usd": 100, "pi_max_turns": 1024},
        {"context_chars": 4096, "case_chars": 200, "call_limit": 1, "calls_per_turn": 4},
        {"fuel": 100_000_000, "max_verifications_per_spec": 100, "max_spawns": 0},
        {"max_depth": 0, "fuel": 100_000, "context_chars": 1_000_000, "case_chars": 20000},
        {"agent_max_tokens": 1024, "max_cases_shown": 1, "max_failures_shown": 0, "max_rungs": 1},
        {"agent_max_tokens": 131072, "max_cases_shown": 256, "max_failures_shown": 32,
         "max_rungs": 32},
    ],
)
def test_forge_settings_accept_contract_boundaries(values: dict[str, object]) -> None:
    settings = ForgeSettings.model_validate(values)
    assert settings.model_dump(include=set(values)) == values


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mode", "forge"),
        ("trigger", "never"),
        ("template", "unknown"),
        ("feedback", "held_out"),
        ("max_usd", 0),
        ("max_usd", 1000.01),
        ("max_usd", float("inf")),
        ("max_usd", float("nan")),
        ("max_s", 0),
        ("max_s", 86401),
        ("max_checks", 0),
        ("max_checks", 1001),
        ("max_dryruns", -1),
        ("max_measures", 0),
        ("max_measures", 257),
        ("measure_cases", 33),
        ("max_submissions", 0),
        ("max_spawns", 9),
        ("max_depth", 3),
        ("spawn_share", 0),
        ("spawn_share", 0.51),
        ("spawn_max_usd", 100.5),
        ("pi_max_turns", 1025),
        ("context_chars", 4095),
        ("case_chars", 20001),
        ("call_limit", 1025),
        ("calls_per_turn", 5),
        ("fuel", 99_999),
        ("max_verifications_per_spec", 101),
        ("token_cap", 1000),
        ("agent_max_tokens", 1023),
        ("agent_max_tokens", 131073),
        ("max_cases_shown", 0),
        ("max_failures_shown", 33),
        ("max_rungs", 0),
    ],
)
def test_forge_settings_reject_out_of_contract_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        ForgeSettings.model_validate({field: value})


async def test_stage_observers_see_model_k_of_n_and_never_break_a_compile(
    tmp_path: Path,
) -> None:
    from architect.compiler import add_stage_observer, remove_stage_observer

    seen: list[dict[str, object]] = []

    def explode(data: dict[str, object]) -> None:
        raise RuntimeError("observer bug")

    add_stage_observer(explode)
    add_stage_observer(seen.append)
    try:
        service = compiler(tmp_path, config=CompileConfig(model_ladder=("fake", "llama3.2")))
        result = await service.compile(specification(model_target="cheapest that passes"))
    finally:
        remove_stage_observer(explode)
        remove_stage_observer(seen.append)
    assert result.artifact is not None and result.artifact.licensed
    assert [(d["stage"], d["model"], d["model_index"], d["model_count"]) for d in seen] == [
        (1, "fake", 1, 2), (2, "fake", 1, 2), (4, "fake", 1, 2),
        (1, "llama3.2", 2, 2), (2, "llama3.2", 2, 2), (4, "llama3.2", 2, 2),
    ]
    assert seen[-1]["stages"] == [1, 2, 4] and seen[-1]["depth"] == 0
