"""Phase 9 Sections 8 and 15.4: the Forge driver through the Compiler, end to end offline."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from forge_support import (
    HELD_OUT_MARK,
    REPAIR,
    SECRET,
    ScriptedPi,
    agent_adapter,
    ensure_sandbox,
    floor_factory,
    forge_compiler,
    install_pi,
    play_factory,
    reply,
    spec,
)

from architect.backends.pi import FORGE_TOOL_NAMES
from architect.compiler import CompileConfig, Compiler
from architect.compiler import forge as forge_module
from architect.compiler.forge import (
    FORGE_INSTRUCTIONS,
    FORGE_JSON_KEYS,
    forge,
    forge_prompt,
)
from architect.compiler.forge_settings import ForgeSettings
from architect.compiler.rewriting import _cheaper
from architect.compiler.tuning import tune
from architect.library import HarnessLibrary
from architect.models import FakeAdapter
from architect.presets import get_preset
from architect.runtime import Runtime
from architect.spec import Budget, HarnessArtifact
from architect.telemetry import TelemetryStore, categorical

ROOT = Path(__file__).parents[1]
PI_READY = (
    ROOT / "architect/backends/pi-node/node_modules/@mariozechner/pi-coding-agent/package.json"
).is_file()
REPAIR_SESSION = [
    {"write": {"path": "harness.py", "content": REPAIR}},
    {"tool": "forge_check", "arguments": {}},
    {"tool": "forge_dryrun", "arguments": {"case": 0, "replies": ["ok"]}},
    {"tool": "forge_measure", "arguments": {"cases": [0]}},
    {"tool": "forge_submit", "arguments": {"note": "three guarded calls"}},
    {"text": "done"},
]


@pytest.fixture(autouse=True)
async def working_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    await ensure_sandbox(monkeypatch, tmp_path / "probe")


def forge_json(artifact: HarnessArtifact) -> dict[str, Any]:
    value = json.loads((artifact.directory / "forge.json").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def calls(name: str, **arguments: Any) -> str:
    call = {"id": f"call-{name}", "name": name, "arguments": arguments}
    return json.dumps({"tool_calls": [call]})


@pytest.mark.skipif(not PI_READY, reason="Pi dependencies are not installed")
async def test_real_pi_sdk_quality_repair_is_promoted_and_verified(tmp_path: Path) -> None:
    script = [
        calls("write", path="harness.py", content=REPAIR),
        calls("forge_check"),
        calls("forge_measure", cases=[0]),
        calls("forge_submit", note="costlier repair"),
        "Submitted.",
    ]
    agent = FakeAdapter("fake", responses=[reply(text, cost=0.0001) for text in script])
    library = HarnessLibrary(tmp_path / "library")
    compiler = forge_compiler(
        tmp_path, settings=ForgeSettings(enabled=True), agent=agent, library=library
    )
    result = await compiler.compile(spec())
    assert agent.calls == len(script)
    assert result.stages == (1, 2, 3, 4) and result.failures == ()
    artifact = result.artifact
    assert artifact is not None and artifact.licensed
    assert artifact.execution_mode == "sandbox" and artifact.forge is not None
    assert (artifact.directory / "harness.py").read_text(encoding="utf-8") == REPAIR
    record = forge_json(artifact)
    assert tuple(record) == FORGE_JSON_KEYS
    assert record["checks"] == record["measures"] == record["submissions"] == 1
    assert record["accepted"] == 1 and record["verifications_for_spec"] == 1
    assert library.verification_count(artifact.spec.spec_hash) == 1
    label = categorical("forge_agent")
    agent_rows = [row for row in compiler.runtime.store.rows() if row.preset_origin == label]
    assert len(agent_rows) == 1 and agent_rows[0].model_calls == len(script)


async def test_quality_repair_costing_more_is_accepted_where_rewrite_rejects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pi = install_pi(monkeypatch, ScriptedPi(REPAIR_SESSION))
    library = HarnessLibrary(tmp_path / "library")
    compiler = forge_compiler(tmp_path, settings=ForgeSettings(enabled=True), library=library)
    value = spec()
    stage2 = await tune(
        value, get_preset("synthesizer"), "fake", floor_factory()(value.floor),
        play_factory("nope"), Runtime(TelemetryStore(tmp_path / "scratch")),
        tmp_path / "scratch-artifacts", max_candidates=1,
    )
    assert stage2.best is not None and not stage2.passed
    result = await compiler.compile(value)
    assert pi.runs == 1 and result.stages == (1, 2, 3, 4) and result.failures == ()
    artifact = result.artifact
    assert artifact is not None and artifact.licensed and artifact.report is not None
    assert artifact.report.floor_score == 1.0 and artifact.report.held_out_count == 3
    assert artifact.version == 2  # Stage 2 wrote version 1; promotion allocated the next.
    promoted_cost = artifact.report.mean_cost_usd
    assert promoted_cost is not None and promoted_cost > stage2.best.measurement.mean_cost_usd
    # The Stage 3 rewrite rule would refuse exactly this repair: it is not cheaper.
    submit = next(value for name, value in pi.results if name == "forge_submit")
    assert isinstance(submit, dict) and submit["accepted"] is True
    measured = stage2.best.measurement.model_copy(
        update={"mean_cost_usd": submit["measurement"]["mean_cost_usd"]}  # type: ignore[index]
    )
    assert not _cheaper(measured, stage2.best.measurement)
    # Stored in the library; a warm start re-runs Stage 4 on the stored Forge harness.
    assert library.lookup(value, "fake") is not None
    second = await forge_compiler(
        tmp_path, settings=ForgeSettings(enabled=True), library=library
    ).compile(value)
    assert second.warm_started and second.stages == (1, 2, 4)
    assert second.artifact is not None and second.artifact.licensed
    assert second.report is not None and second.report.verification_id != (
        artifact.report.verification_id
    )


async def test_forge_json_is_exact_and_content_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_pi(monkeypatch, ScriptedPi(REPAIR_SESSION))
    result = await forge_compiler(tmp_path, settings=ForgeSettings(enabled=True)).compile(spec())
    assert result.artifact is not None
    record = forge_json(result.artifact)
    assert tuple(record) == FORGE_JSON_KEYS
    assert record["language"] == 2 and record["mode"] == "agent"
    assert record["agent_model"] == "fake" and record["play_model"] == "fake"
    assert (record["checks"], record["dryruns"], record["measures"]) == (1, 1, 1)
    assert (record["submissions"], record["accepted"], record["spawns"]) == (1, 1, 0)
    assert record["stage4_calls"] == 1 and record["children"] == []
    assert record["harness_bytes"] == len(REPAIR.encode()) and record["harness_nodes"] > 0
    assert record["agent_tokens_in"] > 0 and record["agent_cost_usd"] > 0
    assert record["notes"] == [
        {"chars": 19, "sha256": __import__("hashlib").sha256(b"three guarded calls").hexdigest()}
    ]
    text = (result.artifact.directory / "forge.json").read_text(encoding="utf-8")
    for forbidden in ("three guarded calls", "context.call", SECRET, HELD_OUT_MARK):
        assert forbidden not in text
    assert text.isascii()


async def test_trigger_failed_skips_when_tuning_passed_and_always_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pi = install_pi(monkeypatch, ScriptedPi([{"text": "nothing to do"}]))
    skipped = await forge_compiler(
        tmp_path / "failed", settings=ForgeSettings(enabled=True), play="ok"
    ).compile(spec())
    assert pi.runs == 0 and skipped.stages == (1, 2, 4)
    assert skipped.artifact is not None and skipped.artifact.licensed
    ran = await forge_compiler(
        tmp_path / "always", settings=ForgeSettings(enabled=True, trigger="always"), play="ok"
    ).compile(spec())
    assert pi.runs == 1 and ran.stages == (1, 2, 3, 4)
    # No submission: the unchanged Stage 2 best is verified and licensed.
    assert ran.artifact is not None and ran.artifact.licensed and ran.artifact.version == 1


async def test_disabled_forge_runs_no_forge_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pi = install_pi(monkeypatch, ScriptedPi([{"text": "never"}]))
    assert CompileConfig().forge.enabled is False
    result = await forge_compiler(tmp_path, settings=ForgeSettings()).compile(spec())
    assert pi.runs == 0 and 3 not in result.stages
    compiler = forge_compiler(tmp_path / "b", settings=ForgeSettings())
    assert compiler._outer_budget() == compiler.config.compile_budget


async def test_outer_wallet_is_compile_budget_plus_forge_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pi = install_pi(monkeypatch, ScriptedPi([{"text": "done"}]))
    settings = ForgeSettings(enabled=True, trigger="always", max_usd=3, max_s=900)
    compiler = forge_compiler(
        tmp_path, settings=settings, play="ok", compile_budget=Budget(cost_usd=2, compute_s=100)
    )
    assert compiler._outer_budget() == Budget(cost_usd=5, compute_s=1000)
    await compiler.compile(spec())
    outer, forge_wallet = pi.wallet_budgets[0], pi.wallet_budgets[1]
    assert outer == Budget(cost_usd=5, compute_s=1000)
    assert forge_wallet == Budget(cost_usd=3, compute_s=900)
    assert pi.wallet_budgets[-1] == Budget(cost_usd=3, compute_s=900)  # the agent run itself


async def test_budget_exhaustion_ends_session_with_best_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = [
        {"write": {"path": "harness.py", "content": REPAIR}},
        {"tool": "forge_submit", "arguments": {}},
        *({"tool": "forge_check", "arguments": {}} for _ in range(40)),
        {"text": "never reached"},
    ]
    pi = install_pi(monkeypatch, ScriptedPi(steps))
    # Each agent turn costs $0.05 and a million tokens: tokens are metered, never capped.
    agent = agent_adapter(64, cost=0.05, tokens=1_000_000)
    compiler = forge_compiler(
        tmp_path, settings=ForgeSettings(enabled=True, max_usd=0.5), agent=agent
    )
    result = await compiler.compile(spec())
    names = [name for name, _ in pi.results]
    assert names[0] == "forge_submit" and len(names) < 41
    assert result.artifact is not None and result.artifact.licensed
    record = forge_json(result.artifact)
    assert record["submissions"] == record["accepted"] == 1
    assert record["agent_tokens_in"] >= 3_000_000
    assert record["agent_cost_usd"] <= 0.5


async def test_last_submission_ends_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = [
        {"write": {"path": "harness.py", "content": REPAIR}},
        {"tool": "forge_submit", "arguments": {}},
        {"tool": "forge_check", "arguments": {}},
        {"text": "never reached"},
    ]
    pi = install_pi(monkeypatch, ScriptedPi(steps))
    result = await forge_compiler(
        tmp_path, settings=ForgeSettings(enabled=True, max_submissions=1)
    ).compile(spec())
    assert pi.results == []  # the final submission's result is never delivered
    assert result.artifact is not None and result.artifact.licensed
    assert forge_json(result.artifact)["submissions"] == 1


async def test_unreadable_last_submission_ends_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = [
        {"write": {"path": "harness.py", "content": "#" * 65546}},
        {"tool": "forge_submit", "arguments": {}},
        {"tool": "forge_check", "arguments": {}},
        {"tool": "forge_measure", "arguments": {}},
        {"text": "never reached"},
    ]
    pi = install_pi(monkeypatch, ScriptedPi(steps))
    compiled = await forge_compiler(
        tmp_path, settings=ForgeSettings(enabled=True, max_submissions=1)
    ).compile(spec())
    # No budget can be spent after the last (unreadable) submission: nothing ran.
    assert pi.results == []
    assert "forge_no_submission" in compiled.failures
    assert "forge_failed" not in compiled.failures


async def test_forge_tests_use_the_real_sandbox_worker_and_never_a_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import forge_support

    from architect.runtime import sandbox

    real = sandbox.sandbox_harness
    assert real.__module__ == "architect.runtime.sandbox"
    assert not hasattr(forge_support, "_straight_line_harness")

    async def broken(root: Path) -> bool:
        return False

    monkeypatch.setattr(forge_support, "_SANDBOX_WORKS", None)
    monkeypatch.setattr(forge_support, "_probe_sandbox", broken)
    with pytest.raises(forge_support.SandboxUnavailable):
        await ensure_sandbox(monkeypatch, tmp_path / "broken")
    assert sandbox.sandbox_harness is real


async def test_session_configuration_pack_and_build_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pi = install_pi(monkeypatch, ScriptedPi([{"text": "done"}]))
    settings = ForgeSettings(enabled=True, trigger="always", pi_max_turns=33, context_chars=5000)
    compiler = forge_compiler(tmp_path, settings=settings, play="ok")
    denied = tmp_path / "denied-dataset.jsonl"
    compiler.pi_config = compiler.pi_config.model_copy(update={"denied_paths": (denied,)})  # type: ignore[union-attr]
    await compiler.compile(spec())
    config = pi.config
    assert config is not None
    assert config.allowed_tools == ("read", "write", "edit", *sorted(FORGE_TOOL_NAMES))
    assert config.allow_shell is False and config.max_turns == 33
    assert config.max_tool_output_chars == 16384
    assert denied in config.denied_paths
    private = [path for path in config.denied_paths if "architect-forge-private-" in str(path)]
    assert len(private) == 1
    assert pi.is_build is True
    assert pi.pack == {
        "backend": "pi",
        "tools": list(config.allowed_tools),
        "context_policy": {"max_characters": 5000},
        "prompt_fragment": forge_prompt(),
        "subagent_rules": [],
    }
    assert set(pi.snapshot) >= {"harness.py", "LANGUAGE.md", "SPEC.md", "TUNING.md"}
    # The system prompt carries the same card the workspace holds as LANGUAGE.md.
    assert forge_prompt() == FORGE_INSTRUCTIONS + "\n" + pi.snapshot["LANGUAGE.md"]
    assert not any(name.startswith("candidates") for name in pi.snapshot)


def test_forge_instructions_are_fixed_ascii_without_budget_numbers() -> None:
    assert FORGE_INSTRUCTIONS.isascii()
    lines = FORGE_INSTRUCTIONS.splitlines()
    assert 25 <= len(lines) <= 45 and all(len(line) <= 100 for line in lines)
    assert "$" not in FORGE_INSTRUCTIONS and "SPEC.md" in FORGE_INSTRUCTIONS
    assert not re.search(r"\d+\s*(usd|dollars|seconds|minutes)", FORGE_INSTRUCTIONS, re.I)
    for tool in FORGE_TOOL_NAMES:
        assert tool in FORGE_INSTRUCTIONS
    assert 'await context.harness("name", value)' in FORGE_INSTRUCTIONS


async def test_template_mode_runs_without_pi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pi = install_pi(monkeypatch, ScriptedPi([{"text": "never"}]))
    monkeypatch.setattr(forge_module, "instantiate", lambda name, candidate: REPAIR)
    settings = ForgeSettings(enabled=True, mode="template", template="prompt_chain")
    compiler = forge_compiler(tmp_path, settings=settings)
    result = await compiler.compile(spec())
    assert pi.runs == 0 and result.stages == (1, 2, 3, 4)
    assert result.artifact is not None and result.artifact.licensed
    record = forge_json(result.artifact)
    assert record["mode"] == "template" and record["agent_model"] is None
    assert record["submissions"] == 1 and record["agent_cost_usd"] == 0
    agent = categorical("forge_agent")
    assert not [row for row in compiler.runtime.store.rows() if row.preset_origin == agent]


async def direct(
    tmp_path: Path, settings: ForgeSettings, library: HarnessLibrary
) -> tuple[Any, Any]:
    value = spec()
    runtime = Runtime(TelemetryStore(tmp_path / "telemetry"))
    factory = play_factory("nope")
    runtime.adapter_factory = factory
    floors = floor_factory()
    tuned = await tune(
        value, get_preset("synthesizer"), "fake", floors(value.floor), factory, runtime,
        tmp_path / "artifacts", max_candidates=1,
    )
    assert tuned.best is not None
    from architect.backends.pi import PiConfig

    outcome = await forge(
        tuned.best, get_preset("synthesizer"), floors(value.floor), floors(value.floor),
        factory, runtime, tmp_path / "artifacts", agent_adapter(), settings,
        pi_config=PiConfig(), floor_factory=floors, library=library,
    )
    return tuned.best, outcome


async def test_template_mode_without_acceptance_keeps_stage2_best(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pi = install_pi(monkeypatch, ScriptedPi([{"text": "never"}]))
    settings = ForgeSettings(enabled=True, mode="template")
    best, outcome = await direct(tmp_path, settings, HarnessLibrary(tmp_path / "library"))
    assert pi.runs == 0 and outcome.mode == "template"
    assert outcome.submissions == 1 and outcome.agent_cost_usd == 0
    assert outcome.best == best and "forge_no_submission" in outcome.failures


async def test_verification_cap_blocks_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pi = install_pi(monkeypatch, ScriptedPi(REPAIR_SESSION))
    library = HarnessLibrary(tmp_path / "library")
    library.record_forge_verification(spec().spec_hash)
    settings = ForgeSettings(enabled=True, max_verifications_per_spec=1)
    best, outcome = await direct(tmp_path, settings, library)
    assert pi.runs == 0 and outcome.failures == ("forge_verification_cap",)
    assert outcome.best == best and outcome.submissions == 0


async def test_pi_failure_is_a_fixed_code_and_keeps_stage2_best(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Broken(ScriptedPi):
        async def run(self, context: Any, task: Any, pack: Any) -> str:
            raise RuntimeError("provider said something private")

    install_pi(monkeypatch, Broken([]))
    best, outcome = await direct(
        tmp_path, ForgeSettings(enabled=True), HarnessLibrary(tmp_path / "library")
    )
    assert outcome.failures == ("forge_failed", "forge_no_submission")
    assert outcome.best == best
    assert "private" not in outcome.model_dump_json()


def test_boundaries_build_verifier_and_trusted_tools() -> None:
    package = ROOT / "architect"
    build_callers = []
    for path in package.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"runtime\.build\(|getattr\([^)]*runtime[^)]*\"build\"", text):
            build_callers.append(path.relative_to(package).as_posix())
    assert build_callers == ["compiler/forge.py"]
    forge_modules = [
        *package.joinpath("compiler").glob("forge*.py"),
        *package.joinpath("compiler/forge_templates").glob("*.py"),
    ]
    for path in forge_modules:
        text = path.read_text(encoding="utf-8")
        assert "floors._access" not in text and "floors import _access" not in text
        assert "compiler.verification" not in text and "from .verification" not in text
        assert "_verification_cases" not in text and not re.search(r"\.held_out\b", text)
        assert "is_verifier" not in text
        if path.name != "forge_tools.py":
            assert not re.search(r"TrustedTool\(\s*name=\"forge_", text)
    for path in package.rglob("*.py"):
        if path.name == "forge_tools.py":
            continue
        assert not re.search(
            r"TrustedTool\(\s*name=\"forge_", path.read_text(encoding="utf-8")
        ), path


async def test_compiler_default_wiring_is_unchanged(tmp_path: Path) -> None:
    compiler = Compiler(
        tmp_path / "artifacts",
        Runtime(TelemetryStore(tmp_path / "telemetry")),
        play_factory("ok"),
        floor_factory(),
    )
    assert compiler.forge_adapter is None and compiler.pi_config is None
    assert compiler._forge_depth == 0
    result = await compiler.compile(spec())
    assert result.stages == (1, 2, 4) and result.artifact is not None
    assert result.artifact.licensed and result.artifact.forge is None


async def test_observers_see_stages_tool_results_and_session_bounds_without_held_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from architect.compiler import add_stage_observer, remove_stage_observer
    from architect.compiler.forge_tools import add_result_observer, remove_result_observer

    install_pi(monkeypatch, ScriptedPi(REPAIR_SESSION))
    stages: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    add_stage_observer(stages.append)
    add_result_observer(results.append)
    try:
        result = await forge_compiler(
            tmp_path, settings=ForgeSettings(enabled=True)).compile(spec())
    finally:
        remove_stage_observer(stages.append)
        remove_result_observer(results.append)
    assert result.artifact is not None
    assert [s["stage"] for s in stages] == [1, 2, 3, 4]
    assert all(s["model_index"] == 1 and s["model_count"] == 1 for s in stages)
    tools = [r["tool"] for r in results]
    assert tools[0] == tools[-1] == "forge_session"
    assert [r["event"] for r in (results[0], results[-1])] == ["started", "finished"]
    assert {"forge_check", "forge_dryrun", "forge_measure", "forge_submit"} <= set(tools)
    measured = next(r["result"] for r in results if r["tool"] == "forge_measure")
    assert set(measured["left"]) >= {"usd", "submissions"} and measured["cases"]
    finished = results[-1]["result"]
    assert finished["submissions"] == 1 and finished["accepted"] == 1
    assert "best" in finished and "artifact" not in json.dumps(finished["best"])
    text = json.dumps(results)
    assert SECRET not in text and HELD_OUT_MARK not in text
