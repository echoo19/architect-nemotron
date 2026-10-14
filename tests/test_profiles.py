"""Trusted profile wiring tests; fixture evidence is synthetic, never live quality."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from architect import profiles
from architect.floors import HeldOutAccessError, HeldOutTasks, HumanEditRate, LLMJudge
from architect.models import ModelRequest, OllamaAdapter
from architect.profiles import (
    AdapterConfig,
    Profile,
    ProfileError,
    _adapter,
    build_services,
    load_profile,
)
from architect.spec import FloorDefinition, FloorKind


def write_profile(tmp_path: Path, **updates: object) -> Path:
    payload: dict[str, object] = {"models": {"fake": {"provider": "fake"}}}
    payload.update(updates)
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def cases(path: Path) -> None:
    path.write_text(json.dumps([
        {"case_id": str(i), "task": {}, "expected": "ok"} for i in range(6)
    ]), encoding="utf-8")


def test_relative_paths_and_typed_settings(tmp_path: Path) -> None:
    path = write_profile(tmp_path, workspace="work", pi={"max_turns": 2},
                         compile={"max_candidates": 1}, orchestrator={"max_tasks": 2})
    profile = load_profile(path)
    assert profile.resolve(profile.workspace) == tmp_path / "work"
    assert profile.compile.max_candidates == 1
    assert profile.pi.max_turns == 2
    assert profile.orchestrator.max_tasks == 2
    with pytest.raises(ValidationError):
        profile.workspace = Path("different")


@pytest.mark.parametrize("patch", [
    {"approved": True}, {"models": {}}, {"pi": {"allow_unrestricted": True}},
    {"compile": {"max_candidates": 0}}, {"orchestrator": {"max_tasks": 0}},
    {"inference": {"provider": "fake"}},
    {"models": {"fake": {"provider": "fake", "api_key_env": "SECRET"}}},
    {"models": {"gpt-4o-mini": {"provider": "openai", "fixture": "fake.json"}}},
    {"models": {"fake": {"provider": "invented"}}},
    {"models": {"fake": {"provider": "openai", "api_key_env": "bad key"}}},
])
def test_invalid_profiles_fail_without_contents(tmp_path: Path, patch: dict[str, object]) -> None:
    with pytest.raises(ProfileError, match="^invalid_profile$"):
        load_profile(write_profile(tmp_path, **patch))


def test_missing_and_malformed_profile(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="invalid_profile"):
        load_profile(tmp_path / "missing")
    path = tmp_path / "bad.json"
    path.write_text('{"secret":"never-print-this",}', encoding="utf-8")
    with pytest.raises(ProfileError, match="^invalid_profile$"):
        load_profile(path)


def test_toml_profile(tmp_path: Path) -> None:
    path = tmp_path / "profile.toml"
    path.write_text('[models.fake]\nprovider = "fake"\n', encoding="utf-8")
    assert load_profile(path).models["fake"].provider == "fake"


async def test_fake_fixture_services_close_and_private_floor(tmp_path: Path) -> None:
    cases(tmp_path / "tasks.json")
    (tmp_path / "responses.json").write_text(json.dumps([
        {"text": "fixture output", "tokens_in": 8, "tokens_out": 12,
         "cost_usd": .00002, "model": "fake"}
    ]), encoding="utf-8")
    profile = load_profile(write_profile(
        tmp_path, models={"fake": {"provider": "fake", "fixture": "responses.json"}},
        floors={"tasks": {"kind": "held_out_tasks", "path": "tasks.json"}},
    ))
    services = build_services(profile, profile.workspace)
    try:
        definition = FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="tasks")
        floor = services.compiler.floor_factory(definition)
        assert isinstance(floor, HeldOutTasks)
        assert len(floor.tuning) == 3
        with pytest.raises(HeldOutAccessError):
            _ = floor.held_out
        response = await services.adapter_factory("fake").complete(ModelRequest(prompt="test"))
        assert response.text == "fixture output"
        # Factories create fresh scripted adapters, not an exhausted shared cursor.
        assert services.adapter_factory("fake").quote(ModelRequest(prompt="test")).tokens_in == 8
        with pytest.raises(ProfileError, match="model_not_configured"):
            services.adapter_factory("untrusted-model")
    finally:
        services.close()
    services.close()
    with pytest.raises(ValueError, match="closed"):
        services.runtime.store.rows()


async def test_human_replay_and_smoke(tmp_path: Path) -> None:
    log = tmp_path / "edits.jsonl"
    log.write_text("".join(json.dumps({
        "case_id": str(i), "task": {}, "proposed": "old", "shipped": "ok", "changed": True,
    }) + "\n" for i in range(60)), encoding="utf-8")
    profile = load_profile(write_profile(tmp_path, floors={
        "edits": {"kind": "human_edit_rate", "path": "edits.jsonl", "smoke": "edits.jsonl"},
    }))
    services = build_services(profile, profile.workspace)
    try:
        definition = FloorDefinition(kind=FloorKind.HUMAN_EDIT_RATE, limit=0, dataset="edits")
        floor = services.compiler.floor_factory(definition)
        assert isinstance(floor, HumanEditRate)
        assert await floor.score(floor.tuning, ["ok"] * 30) == 0
        assert services.compiler.smoke_factory is not None
        assert isinstance(services.compiler.smoke_factory(definition), HumanEditRate)
    finally:
        services.close()


async def test_judge_uses_explicit_calibration_and_model(tmp_path: Path) -> None:
    cases(tmp_path / "tasks.json")
    (tmp_path / "calibration.json").write_text(json.dumps([
        {"case_id": "human-1", "task": {}, "output": "ok", "human_score": 1},
    ]), encoding="utf-8")
    (tmp_path / "judge.json").write_text(json.dumps([
        {"text": "1", "tokens_in": 8, "tokens_out": 1, "cost_usd": .00001, "model": "fake"}
        for _ in range(4)
    ]), encoding="utf-8")
    profile = load_profile(write_profile(tmp_path,
        models={"fake": {"provider": "fake", "fixture": "judge.json"}},
        floors={"tasks": {"kind": "llm_judge", "path": "tasks.json"}},
        calibrations={"human": "calibration.json"},
    ))
    services = build_services(profile, profile.workspace)
    try:
        definition = FloorDefinition(kind=FloorKind.LLM_JUDGE, limit=1, dataset="tasks",
                                     rubric="Exact", judge_model="fake",
                                     calibration_dataset="human")
        floor = services.compiler.floor_factory(definition)
        assert isinstance(floor, LLMJudge)
        assert await floor.score(floor.tuning, ["ok"] * 3) == 1
        rows = services.runtime.store.rows()
        assert len(rows) == 4  # Calibration is an actual accounted model call.
        with pytest.raises(ProfileError, match="calibration_not_configured"):
            services.compiler.floor_factory(definition.model_copy(
                update={"calibration_dataset": "invented"}))
    finally:
        services.close()


def test_game_setup_never_substitutes_equality(tmp_path: Path) -> None:
    profile = load_profile(write_profile(tmp_path, floors={
        "game": {"kind": "held_out_game_score", "path": "scores.json"},
    }))
    with pytest.raises(ProfileError, match="unsupported_game_environment"):
        build_services(profile, profile.workspace)
    assert not (tmp_path / ".architect" / "telemetry").exists()


def test_setup_failure_closes_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = load_profile(write_profile(tmp_path))
    closed: list[bool] = []
    original = profiles.TelemetryStore.close

    def close(store: profiles.TelemetryStore) -> None:
        closed.append(True)
        original(store)

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("private details")

    monkeypatch.setattr(profiles.TelemetryStore, "close", close)
    monkeypatch.setattr(profiles, "Registry", fail)
    with pytest.raises(RuntimeError):
        build_services(profile, profile.workspace)
    assert closed == [True]


def test_missing_explicit_api_key_and_unknown_model_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ARCHITECT_TEST_MISSING_KEY", raising=False)
    profile = Profile.model_validate({"models": {
        "gpt-4o-mini": {"provider": "openai", "api_key_env": "ARCHITECT_TEST_MISSING_KEY"},
    }})
    with pytest.raises(ProfileError, match="missing_api_key"):
        build_services(profile, tmp_path)
    profile = Profile.model_validate({"models": {"invented": {"provider": "fake"}}})
    with pytest.raises(ProfileError, match="adapter_unavailable"):
        build_services(profile, tmp_path)


def test_missing_mismatched_and_invalid_floors(tmp_path: Path) -> None:
    profile = load_profile(write_profile(tmp_path, floors={
        "tasks": {"kind": "held_out_tasks", "path": "missing.json"},
    }))
    services = build_services(profile, profile.workspace)
    try:
        definition = FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="tasks")
        with pytest.raises(ProfileError, match="invalid_floor_fixture"):
            services.compiler.floor_factory(definition)
        with pytest.raises(ProfileError, match="floor_not_configured"):
            services.compiler.floor_factory(definition.model_copy(update={"dataset": "invented"}))
        with pytest.raises(ProfileError, match="floor_not_configured"):
            services.compiler.floor_factory(definition.model_copy(
                update={"kind": FloorKind.HUMAN_EDIT_RATE}))
    finally:
        services.close()


def test_builder_fields_are_optional_typed_and_explicit(tmp_path: Path) -> None:
    default = load_profile(write_profile(tmp_path))
    assert default.builder_floor is None and default.builder_model is None
    profile = load_profile(write_profile(tmp_path,
        builder_floor={"kind": "held_out_tasks", "dataset": "coding", "limit": 1},
        builder_model="fake",
    ))
    assert profile.builder_floor == FloorDefinition(
        kind=FloorKind.HELD_OUT_TASKS, dataset="coding", limit=1,
    )
    assert profile.builder_model == "fake"


@pytest.mark.parametrize("patch", [
    {"builder_model": ""}, {"builder_floor": {"kind": "held_out_tasks"}},
    {"builder_floor": {"kind": "held_out_tasks", "dataset": "coding", "limit": 1,
                       "approved": True}},
])
def test_builder_fields_reject_invalid_setup(tmp_path: Path, patch: dict[str, object]) -> None:
    with pytest.raises(ProfileError, match="invalid_profile"):
        load_profile(write_profile(tmp_path, **patch))


def test_legacy_profile_token_limits_are_ignored_not_serialized(tmp_path: Path) -> None:
    profile = load_profile(write_profile(tmp_path,
        compile={"compile_budget": {"tokens": 1, "cost_usd": .5, "compute_s": 90}},
        orchestrator={"budget": {"tokens": 1, "cost_usd": .5, "compute_s": 90}},
    ))
    assert profile.compile.compile_budget.model_dump() == {"cost_usd": .5, "compute_s": 90}
    assert profile.orchestrator.budget.model_dump() == {"cost_usd": .5, "compute_s": 90}
    schema = Profile.model_json_schema()
    # The only token fields are per-call output caps, which bound a reply and its
    # reservation; neither is a budget. An adapter's (Phase 14), and the Forge agent's.
    for name in ("AdapterConfig", "NamedAdapterConfig"):
        del schema["$defs"][name]["properties"]["max_tokens"]
    del schema["$defs"]["ForgeSettings"]["properties"]["agent_max_tokens"]
    assert "tokens" not in json.dumps(schema)


def test_services_derive_private_control_plane_paths_without_blanket_repo_denial(
    tmp_path: Path,
) -> None:
    cases(tmp_path / "floor.json")
    cases(tmp_path / "smoke.json")
    (tmp_path / "responses.json").write_text(json.dumps([
        {"text": "ok", "tokens_in": 8, "tokens_out": 12, "cost_usd": .00002, "model": "fake"},
    ]), encoding="utf-8")
    profile_path = write_profile(tmp_path,
        workspace=".",
        models={"fake": {"provider": "fake", "fixture": "responses.json"}},
        floors={"tasks": {"kind": "held_out_tasks", "path": "floor.json", "smoke": "smoke.json"}},
        calibrations={"humans": "calibration.json"},
        pi={"denied_paths": ["custom-private"]},
    )
    profile = load_profile(profile_path)
    original = profile.pi
    services = build_services(profile, profile.workspace)
    try:
        backend = services.runtime.pi_backend
        assert isinstance(backend, profiles.PiBackend)
        denied = set(backend.config.denied_paths)
        expected = {tmp_path / name for name in (
            "custom-private", "artifacts", "library", "registry", "telemetry", "proposals",
            "server", "routes", "exports", "passed", "retakes",
            "floor.json", "smoke.json", "calibration.json", "responses.json", "profile.json",
            # W7: the Ed25519 private key and the trust list, present or not.
            "ed25519.key", "trusted-keys.json",
        )}
        assert denied == expected
        assert all(path.is_absolute() for path in denied)
        # Scoped FeatureBuilder backend copies Services.profile.pi, not caller config.
        assert services.profile.pi == backend.config
        assert services.profile.pi.model_copy(update={"allowed_tools": ("compile_agent",)}
                                              ).denied_paths == backend.config.denied_paths
        assert profile.pi == original and profile.pi.denied_paths == (Path("custom-private"),)
        evaluation_repo = tmp_path / "evaluation-checkout"
        assert not any(evaluation_repo == path or evaluation_repo.is_relative_to(path)
                       for path in denied)
        assert services.profile.resolve(Path("relative")) == tmp_path / "relative"
    finally:
        services.close()


def test_named_adapter_fixture_files_are_private(tmp_path: Path) -> None:
    fixture = tmp_path / "private-response.json"
    fixture.write_text(json.dumps([
        {"text": "ok", "tokens_in": 8, "tokens_out": 12, "cost_usd": .00002, "model": "fake"},
    ]), encoding="utf-8")
    config = {"provider": "fake", "model": "fake", "fixture": "private-response.json"}
    profile = load_profile(write_profile(tmp_path, inference=config, decomposition=config,
                                         rewrite=config))
    services = build_services(profile, profile.workspace)
    try:
        assert fixture in services.profile.pi.denied_paths
        assert len(services.profile.pi.denied_paths) == len(set(services.profile.pi.denied_paths))
    finally:
        services.close()


async def test_floor_config_checker_is_wired_from_the_closed_table(tmp_path: Path) -> None:
    from architect.floors.checkers import CHECKERS

    cases(tmp_path / "tasks.json")
    profile = load_profile(write_profile(tmp_path, floors={
        "plain": {"kind": "held_out_tasks", "path": "tasks.json"},
        "exam": {"kind": "held_out_tasks", "path": "tasks.json",
                 "checker": "exam_extraction_v1"},
    }))
    assert profile.floors["plain"].checker == "equality"
    assert profile.floors["exam"].checker == "exam_extraction_v1"
    services = build_services(profile, profile.workspace)
    try:
        plain = services.compiler.floor_factory(
            FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="plain")
        )
        exam = services.compiler.floor_factory(
            FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="exam")
        )
        assert isinstance(plain, HeldOutTasks) and isinstance(exam, HeldOutTasks)
        assert plain._checker is None
        assert exam._checker is CHECKERS["exam_extraction_v1"]
        # Equality still scores exactly; the exam checker treats the trusted "ok" as malformed.
        assert await plain.score(plain.tuning, ["ok"] * 3) == 1.0
        with pytest.raises(ValueError):
            await exam.score(exam.tuning, ["ok"] * 3)
    finally:
        services.close()


@pytest.mark.parametrize("checker", ["python:eval", "Equality", "", 1])
def test_floor_config_rejects_checkers_outside_the_table(tmp_path: Path, checker: object) -> None:
    with pytest.raises(ProfileError, match="^invalid_profile$"):
        load_profile(write_profile(tmp_path, floors={
            "tasks": {"kind": "held_out_tasks", "path": "tasks.json", "checker": checker},
        }))


def test_ollama_profile_options_reach_the_adapter(tmp_path: Path) -> None:
    prices = tmp_path / "local-prices.json"
    prices.write_text(
        '{"models": {"gemma4:26b": {"input_per_million": 0, "output_per_million": 0,'
        ' "input_overhead_tokens": 1024, "output_capacity_tokens": 8192,'
        ' "allowed_response_models": ["gemma4:26b"]}},'
        ' "fake_usage": {"tokens_in": 8, "tokens_out": 2}}',
        encoding="utf-8",
    )
    path = tmp_path / "profile.json"
    path.write_text(
        '{"models": {"gemma4:26b": {"provider": "ollama", "prices": "local-prices.json",'
        ' "num_ctx": 65536, "think": true, "timeout_s": 600}}}',
        encoding="utf-8",
    )
    profile = load_profile(path)
    adapter = _adapter(profile, "gemma4:26b", profile.models["gemma4:26b"])
    assert isinstance(adapter, OllamaAdapter)
    assert (adapter.num_ctx, adapter.think, adapter.raw) == (65536, True, False)
    assert adapter.quote(ModelRequest(prompt="x")).cost_usd == 0


@pytest.mark.parametrize(
    "config",
    [
        {"provider": "anthropic", "num_ctx": 8192},
        {"provider": "openai", "think": True},
        {"provider": "claude-code", "prices": "p.json"},
        {"provider": "fake", "prices": "p.json"},
        {"provider": "ollama", "num_ctx": 100},
        # Nebius options: thinking needs the nebius credential; sampling is OpenAI-only.
        {"provider": "openai", "api_key_ref": "openrouter", "think": False},
        {"provider": "openai", "num_ctx": 8192, "api_key_ref": "nebius"},
        {"provider": "anthropic", "timeout_s": 30},
        {"provider": "claude-code", "timeout_s": 30},
        {"provider": "ollama", "temperature": 0.5},
        {"provider": "anthropic", "top_p": 0.9},
        {"provider": "openai", "temperature": 2.5},
        {"provider": "openai", "top_p": 0},
        {"provider": "openai", "top_p": 1.5},
    ],
)
def test_local_options_are_refused_where_they_do_not_apply(config: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AdapterConfig.model_validate(config)


def test_nebius_and_openai_options_reach_the_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from architect.models import OpenAICompatibleAdapter

    monkeypatch.setenv("NEBIUS_API_KEY", "nebius-key")
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    nebius = {"provider": "openai", "base_url": "https://api.tokenfactory.nebius.com/v1",
              "api_key_ref": "nebius"}
    path = write_profile(
        tmp_path,
        models={
            "nvidia/nemotron-3-super-120b-a12b": nebius,
            "gpt-4o-mini": {"provider": "openai", "api_key_ref": "groq", "timeout_s": 15,
                            "temperature": 0.2},
        },
        # A dedicated endpoint keeps the dialect: the credential decides, not the URL.
        forge_agent={**nebius, "base_url": "https://dedicated.example/v1",
                     "model": "nvidia/Nemotron-3-Ultra-550b-a55b", "think": True,
                     "temperature": 1.0, "top_p": 0.95, "timeout_s": 600},
    )
    profile = load_profile(path)
    chat = _adapter(profile, "nvidia/nemotron-3-super-120b-a12b",
                    profile.models["nvidia/nemotron-3-super-120b-a12b"])
    assert isinstance(chat, OpenAICompatibleAdapter)
    assert (chat.dialect, chat.think, chat.temperature, chat.top_p) == ("nebius", None, None, None)
    assert chat._timeout == 120.0 and chat._api_key == "nebius-key"
    assert profile.forge_agent is not None
    forge = _adapter(profile, profile.forge_agent.model, profile.forge_agent)
    assert isinstance(forge, OpenAICompatibleAdapter)
    assert (forge.dialect, forge.think, forge.temperature, forge.top_p) == (
        "nebius", True, 1.0, 0.95)
    assert forge._timeout == 600.0 and forge._base_url == "https://dedicated.example/v1"
    other = _adapter(profile, "gpt-4o-mini", profile.models["gpt-4o-mini"])
    assert isinstance(other, OpenAICompatibleAdapter)
    assert (other.dialect, other._timeout, other.temperature) == ("openai", 15.0, 0.2)
    assert profiles.is_nebius(profile.models["nvidia/nemotron-3-super-120b-a12b"])
    assert not profiles.is_nebius(profile.models["gpt-4o-mini"])
