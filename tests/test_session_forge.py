"""Phase 9 session verb ``forge`` (contract Section 12.3), offline on the fake provider.

The verb edits the session's ``CompileConfig.forge`` only: the Spec hash and the
profile file never change. Forge runs in ``template`` mode here (no Pi, no agent).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from session_support import run_session, spec_text, unwrap

from architect.compiler.types import ForgeSettings
from architect.session import render
from architect.session.codes import CODES, explain, hint_for, is_code
from architect.session.commands import HELP, VOCABULARY, help_for, parse
from architect.session.forge import ENABLE_PROMPT, cap_line, status_lines
from architect.session.state import State

SENTINEL = "SENTINEL_CASE_TEXT_7f3a"


def forge_profile(
    tmp_path: Path,
    *,
    smoke: bool = True,
    forge: dict[str, Any] | None = None,
    extra_models: tuple[str, ...] = (),
    pi: dict[str, Any] | None = None,
) -> Path:
    workspace = tmp_path / "w"
    workspace.mkdir(parents=True, exist_ok=True)
    cases = [
        {"case_id": str(index), "task": {"text": SENTINEL}, "expected": "ok"}
        for index in range(8)
    ]
    (workspace / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
    (workspace / "smoke.json").write_text(json.dumps(cases[:4]), encoding="utf-8")
    inference = [{"text": spec_text(), "tokens_in": 8, "tokens_out": 12, "cost_usd": 0.00002,
                  "model": "fake"}]
    (workspace / "inference.json").write_text(json.dumps(inference), encoding="utf-8")
    floor: dict[str, Any] = {"kind": "held_out_tasks", "path": "cases.json"}
    if smoke:
        floor["smoke"] = "smoke.json"
    compile_config: dict[str, Any] = {"max_candidates": 1, "enable_rewriting": False}
    if forge is not None:
        compile_config["forge"] = forge
    profile = workspace / "session-profile.json"
    profile.write_text(
        json.dumps(
            {
                "workspace": ".",
                "models": {
                    "fake": {"provider": "fake"},
                    **{name: {"provider": "fake"} for name in extra_models},
                },
                "inference": {"provider": "fake", "model": "fake", "fixture": "inference.json"},
                "floors": {"tasks": floor},
                "compile": compile_config,
                **({"pi": pi} if pi is not None else {}),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return profile


def _run(lines: list[str], tmp_path: Path, profile: Path) -> tuple[int, str, Any]:
    return run_session(["spec " + spec_text(), *lines, "quit"], tmp_path, profile_path=profile,
                       probe=False)


def test_forge_is_a_review_verb_with_help() -> None:
    assert "forge" in VOCABULARY[State.REVIEW]
    assert "forge" not in VOCABULARY[State.READY]  # READY help and vocabulary are unchanged
    assert "forge on|off|status" in HELP[State.REVIEW]
    cmd = parse("forge on", State.REVIEW)
    assert cmd is not None and cmd.verb == "forge" and cmd.args == ("on",)
    line = help_for("forge")
    assert line.startswith("  forge on | forge off")
    assert all(len(part) <= 100 for part in line.splitlines())


def test_status_on_budget_mode_model_and_compile(tmp_path: Path) -> None:
    profile = forge_profile(tmp_path, forge={"trigger": "always", "mode": "template"})
    before = profile.read_bytes()
    code, transcript, session = _run(
        [
            "forge status",
            "forge on",
            "y",
            "forge budget 3 2",
            "forge model fake",
            "forge mode template",
            "forge status",
            "compile",
            "report",
        ],
        tmp_path,
        profile,
    )
    assert code == 0
    transcript = unwrap(transcript)
    assert "forge off  mode template  trigger always" in transcript
    assert (
        "forge: compile cap $10.00 + forge cap $5.00 = $15.00 (API-equivalent, not a bill); "
        "forge minutes 60; verifications for this spec so far: 0/10" in transcript
    )
    assert "forge on: mode template, trigger always; the Spec and its hash are unchanged" in (
        transcript
    )
    assert "forge budget $3.00 / 2 min (API-equivalent, not a bill)" in transcript
    assert "forge model fake" in transcript and "forge mode template" in transcript
    # The compile forged: Stage 3 ran as Forge and printed its counts-only line.
    assert "forge  checks 0  dryruns 0  measures 0/24  submits 1/6  spawns 0" in transcript
    assert "$0.0000/$3.00 (API-equivalent)" in transcript
    outcome = session.outcome
    assert outcome is not None and 3 in outcome.result.stages
    assert session.forge_line is not None and session.forge_line.startswith("forge  checks")
    # Never content: no case text and no harness source reaches the terminal.
    assert SENTINEL not in transcript and "async def" not in transcript
    # The verb edited the session's compile config only.
    assert profile.read_bytes() == before
    assert outcome.result.spec.model_dump_json() == spec_text()
    settings = session.forge_override
    assert settings == ForgeSettings(
        enabled=True, trigger="always", mode="template", max_usd=3.0, max_s=120.0,
        agent_model="fake",
    )


def _record_forge_agents(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Spy on ``Compiler._forge``: which agent adapter each Forge session was given."""
    from architect.compiler import Compiler

    seen: list[object] = []
    original = Compiler._forge

    async def spy(self: Compiler, *args: Any, **kwargs: Any) -> Any:
        seen.append(self.forge_adapter)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(Compiler, "_forge", spy)
    return seen


def test_forge_model_selects_the_agent_that_drives_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: ``forge model <name>`` must change the adapter Forge runs with.

    The session override of ``agent_model`` is resolved with the profile's rule
    (Section 12.1) and installed as ``Compiler.forge_adapter`` for that compile only.
    """
    seen = _record_forge_agents(monkeypatch)
    profile = forge_profile(
        tmp_path, forge={"trigger": "always", "mode": "template"}, extra_models=("llama3.2",)
    )
    code, transcript, session = _run(
        ["forge on", "y", "forge model llama3.2", "compile", "report"], tmp_path, profile
    )
    assert code == 0, transcript
    assert session.outcome is not None and 3 in session.outcome.result.stages
    assert len(seen) == 1
    agent = seen[0]
    assert agent is not None and getattr(agent, "model", None) == "llama3.2"
    # The swap is per compile: the compiler's own agent is restored afterwards.
    assert session._services().compiler.forge_adapter is None


def test_forge_without_a_model_override_keeps_the_driver_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _record_forge_agents(monkeypatch)
    profile = forge_profile(
        tmp_path, forge={"trigger": "always", "mode": "template"}, extra_models=("llama3.2",)
    )
    code, transcript, _session = _run(["forge on", "y", "compile", "report"], tmp_path, profile)
    assert code == 0, transcript
    assert seen == [None]  # no agent_model and no forge_agent: rewrite, then the ladder model


def test_unknown_profile_agent_model_is_refused_before_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session override the profile cannot resolve is ``forge_model_unknown`` at compile."""
    import architect.session.forge as session_forge

    seen = _record_forge_agents(monkeypatch)
    # Bypass the verb's own check to prove the compile-time resolution refuses too.
    monkeypatch.setattr(session_forge, "_known_model", lambda profile, name: True)
    profile = forge_profile(tmp_path, forge={"trigger": "always", "mode": "template"})
    code, transcript, session = _run(
        ["forge on", "y", "forge model nope", "compile"], tmp_path, profile
    )
    assert code == 0
    assert "forge_model_unknown" in transcript
    assert session.outcome is None and seen == []
    assert "compile cap" not in transcript.split("forge model nope")[-1]


def test_session_forge_runs_pi_with_the_effective_profile_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (Section 8.5): the session's Forge gets the build_services PiConfig.

    ``Compiler.pi_config`` was None, so the Forge driver fell back to ``PiConfig()``
    and lost the held-out denied set and the profile's Pi limits.
    """
    import architect.compiler.forge as forge_module

    seen: list[Any] = []
    original = forge_module.forge

    async def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs["pi_config"])
        return await original(*args, **kwargs)

    monkeypatch.setattr(forge_module, "forge", spy)
    profile = forge_profile(
        tmp_path,
        forge={"trigger": "always", "mode": "template"},
        pi={"node_executable": "custom-node", "max_turns": 7},
    )
    code, transcript, session = _run(["forge on", "y", "compile", "report"], tmp_path, profile)
    assert code == 0, transcript
    assert session.outcome is not None and 3 in session.outcome.result.stages
    assert len(seen) == 1
    pi = seen[0]
    assert pi.node_executable == "custom-node" and pi.max_turns == 7
    denied = set(pi.denied_paths)
    workspace = tmp_path / "w"
    for held_out in ("cases.json", "smoke.json", "session-profile.json"):
        assert (workspace / held_out).resolve() in denied


def test_spec_hash_and_profile_never_change(tmp_path: Path) -> None:
    profile = forge_profile(tmp_path)
    before = profile.read_bytes()
    code, _transcript, session = _run(
        ["forge on", "y", "forge budget 2 3", "forge mode agent", "forge off"], tmp_path, profile
    )
    assert code == 0
    assert profile.read_bytes() == before
    assert session.draft is not None
    assert session.draft.spec.model_dump_json() == spec_text()
    assert session.forge_override is not None and not session.forge_override.enabled


def test_forge_off_compiles_without_forge(tmp_path: Path) -> None:
    profile = forge_profile(tmp_path, forge={"enabled": True, "trigger": "always",
                                             "mode": "template"})
    code, transcript, session = _run(["forge off", "compile", "report"], tmp_path, profile)
    assert code == 0
    assert "forge off: Stage 3 is the ordinary rewrite path" in transcript
    assert "forge  checks" not in transcript
    assert session.outcome is not None and 3 not in session.outcome.result.stages


def test_declined_forge_on_changes_nothing(tmp_path: Path) -> None:
    profile = forge_profile(tmp_path)
    code, transcript, session = _run(["forge on", "n"], tmp_path, profile)
    assert code == 0
    assert "forge not enabled" in transcript
    assert session.forge_override is None


@pytest.mark.parametrize(
    ("lines", "code"),
    [
        (["forge model nope"], "forge_model_unknown"),
        (["forge budget 0 5"], "forge_budget_invalid"),
        (["forge budget 5 0"], "forge_budget_invalid"),
        (["forge budget 1001 5"], "forge_budget_invalid"),
        (["forge budget 5 99999"], "forge_budget_invalid"),
        (["forge budget five 5"], "forge_budget_invalid"),
        (["forge budget inf 5"], "forge_budget_invalid"),
        (["forge mode wild"], "not_a_command"),
        (["forge sideways"], "not_a_command"),
    ],
)
def test_forge_verb_codes(tmp_path: Path, lines: list[str], code: str) -> None:
    profile = forge_profile(tmp_path)
    exit_code, transcript, session = _run(lines, tmp_path, profile)
    assert exit_code == 0
    assert code in transcript
    assert session.forge_override is None


def test_forge_on_without_a_smoke_floor_is_forge_disabled(tmp_path: Path) -> None:
    profile = forge_profile(tmp_path, smoke=False)
    code, transcript, session = _run(["forge on"], tmp_path, profile)
    assert code == 0
    assert "forge_disabled" in transcript
    assert session.forge_override is None


CAP_LINE = (
    "forge: compile cap $10.00 + forge cap $5.00 = $15.00 (API-equivalent, not a bill); "
    "forge minutes 60; verifications for this spec so far: 0/10"
)
PROFILE_FORGE = {"enabled": True, "trigger": "always", "mode": "template"}


def test_profile_enabled_forge_without_a_smoke_floor_is_refused_at_compile(
    tmp_path: Path,
) -> None:
    # Forge must never silently not run: the same forge_disabled as ``forge on``.
    profile = forge_profile(tmp_path, smoke=False, forge=PROFILE_FORGE)
    code, transcript, session = _run(["compile"], tmp_path, profile)
    assert code == 0
    assert "forge_disabled" in transcript
    assert "compile finished" not in transcript and session.outcome is None
    assert session.session_spend.cost_usd == 0.0


def test_profile_enabled_forge_shows_the_cap_and_confirms_before_spend(
    tmp_path: Path,
) -> None:
    profile = forge_profile(tmp_path, forge=PROFILE_FORGE)
    code, transcript, session = _run(["compile", "y", "report"], tmp_path, profile)
    assert code == 0
    transcript = unwrap(transcript)
    # The fixed summed-cap line and the confirmation come before the compile starts.
    assert CAP_LINE in transcript and ENABLE_PROMPT.strip() in transcript
    assert transcript.index(CAP_LINE) < transcript.index(ENABLE_PROMPT.strip())
    assert transcript.index(ENABLE_PROMPT.strip()) < transcript.index("compile finished")
    assert "compile wallet raised by" not in transcript  # no ad-hoc line
    outcome = session.outcome
    assert outcome is not None and 3 in outcome.result.stages
    assert "forge  checks" in transcript and session.forge_confirmed


def test_declined_profile_forge_is_forge_cancelled_with_nothing_spent(
    tmp_path: Path,
) -> None:
    profile = forge_profile(tmp_path, forge=PROFILE_FORGE)
    code, transcript, session = _run(["compile", "n"], tmp_path, profile)
    assert code == 0
    assert CAP_LINE in unwrap(transcript) and "forge_cancelled" in transcript
    assert "compile finished" not in transcript and session.outcome is None
    assert session.session_spend.cost_usd == 0.0
    assert not session.forge_confirmed


def test_forge_on_confirms_once_and_every_forge_compile_shows_the_cap(
    tmp_path: Path,
) -> None:
    profile = forge_profile(tmp_path, forge={"trigger": "always", "mode": "template"})
    code, transcript, session = _run(["forge on", "y", "compile", "report"], tmp_path, profile)
    assert code == 0
    transcript = unwrap(transcript)
    assert transcript.count(ENABLE_PROMPT.strip()) == 1  # ``forge on`` already asked
    assert transcript.count(CAP_LINE) == 2  # at ``forge on`` and before the compile
    assert session.outcome is not None and 3 in session.outcome.result.stages


def test_new_codes_have_fixed_ascii_sentences() -> None:
    session_codes = {
        "forge_disabled", "forge_budget_invalid", "forge_model_unknown", "forge_cancelled",
        "forge_report_missing", "forge_verification_cap", "host_memory_low",
    }
    assert session_codes <= CODES
    default = explain("definitely_not_a_code")
    for code in session_codes | {"forge_no_submission", "forge_failed", "forge_did_not_run"}:
        assert is_code(code)
        line = explain(code)
        assert line != default and line.isascii() and "\n" not in line and len(line) <= 200
    assert hint_for("forge_budget_invalid", None) == "forge budget 5 60"
    assert hint_for("forge_model_unknown", None) == "forge model <model in profile>"


# Contract 12.4: each code gets one fixed ASCII sentence (Tools, Sandbox, Sub-harness rows).
_TOOL_CODES = (
    # Section 9 result codes.
    "forge_invalid_source", "forge_literal_leak", "forge_unbound_child", "forge_unused_child",
    "tuning_failed", "smoke_failed", "not_better", "persistence_failed",
    "forge_submissions_exhausted", "forge_depth", "forge_spawn_cap", "forge_spawn_name_taken",
    "invalid_schema", "invalid_cases", "spawn_kind_unsupported", "compile_failed",
    "not_licensed", "budget_exceeded",
    # The 12.4 Tools row extras.
    "invalid_arguments", "forge_tool_cap", "tool_failed", "result_too_large",
    "forge_file_too_large", "invalid_case", "forge_measure_too_small",
    "forge_dryrun_unavailable",
)
_SANDBOX_CODES = (
    # 4.10 new validation codes, then the reused examples it names.
    "sandbox_recursion", "sandbox_loop_depth", "sandbox_helper", "sandbox_constant",
    "sandbox_language", "sandbox_statement", "sandbox_expression", "sandbox_assignment",
    "sandbox_capability", "sandbox_binding_mismatch", "sandbox_ast_limit",
    # 4.10 runtime codes (closed set) and the worker failure they fail like.
    "sandbox_key", "sandbox_index", "sandbox_type", "sandbox_value", "sandbox_dictionary",
    "sandbox_add", "sandbox_unbound", "sandbox_fuel", "sandbox_memory", "sandbox_value_limit",
    "sandbox_invalid_call", "sandbox_worker_failed",
)
_SUBHARNESS_CODES = (
    "subharness_unknown", "subharness_digest_mismatch", "subharness_unlicensed",
    "subharness_schema_error", "subharness_depth",
)


@pytest.mark.parametrize("code", _TOOL_CODES + _SANDBOX_CODES + _SUBHARNESS_CODES)
def test_tool_sandbox_and_subharness_codes_have_fixed_sentences(code: str) -> None:
    line = explain(code)
    assert is_code(code)
    assert line != explain("definitely_not_a_code"), code
    assert line.isascii() and "\n" not in line and len(line) <= 200


_CONTRACT = Path(__file__).resolve().parents[1] / "docs" / "interfaces" / "phase9-forge.md"
_TICKED = re.compile(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`")


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    level = heading.split(" ", 1)[0]
    rest = text[start + len(heading) :]
    ends = [m.start() for m in re.finditer(rf"^#{{2,{len(level)}}} ", rest, re.MULTILINE)]
    return rest[: ends[0]] if ends else rest


def _contract_codes() -> set[str]:
    """Every code the 12.4 table names, read from the contract itself (not a copy)."""
    text = _CONTRACT.read_text(encoding="utf-8")
    table = _section(text, "### 12.4 Fixed error codes")
    codes = {code for line in table.splitlines() if line.startswith("|")
             for code in _TICKED.findall(line)}
    # "Section 9": the listed result codes and the parenthesised gate codes.
    tools = _section(text, "## 9. Forge tools: exact contracts")
    for segment in re.findall(r"(?:Codes:|is one of:)(.*?)(?:\.\s|\.$)", tools, re.DOTALL):
        codes |= set(_TICKED.findall(segment))
    codes |= set(re.findall(r"\(`([a-z_]+)`(?: otherwise)?\)", tools))
    # "Section 4.10": the sandbox validation and runtime codes.
    codes |= {c for c in _TICKED.findall(_section(text, "### 4.10 Error codes"))
              if c.startswith("sandbox_")}
    return codes


def test_every_contract_12_4_code_has_a_fixed_sentence() -> None:
    codes = _contract_codes()
    # Spot-check the parse so an empty or truncated read cannot pass vacuously.
    assert {"forge_tool_cap", "tool_failed", "result_too_large", "invalid_arguments",
            "forge_dryrun_unavailable", "tuning_failed", "not_better", "budget_exceeded",
            "forge_spawn_name_taken", "invalid_schema", "forge_file_too_large", "invalid_case",
            "sandbox_value_limit", "subharness_depth", "host_memory_low", "forge_failed"} <= codes
    default = explain("definitely_not_a_code")
    missing = sorted(code for code in codes if explain(code) == default)
    assert missing == []
    for code in codes:
        line = explain(code)
        assert is_code(code) and line.isascii() and "\n" not in line and len(line) <= 200, code
    # The session/CLI row codes are session codes an operator can be shown.
    assert {"forge_disabled", "forge_budget_invalid", "forge_model_unknown", "forge_cancelled",
            "forge_report_missing", "forge_verification_cap", "host_memory_low"} <= CODES


def test_every_runtime_sandbox_code_has_a_sentence() -> None:
    from architect.runtime.sandbox_language import RUNTIME_ERROR_CODES

    default = explain("definitely_not_a_code")
    assert RUNTIME_ERROR_CODES <= set(_SANDBOX_CODES)
    assert all(explain(code) != default for code in RUNTIME_ERROR_CODES)


def test_progress_and_status_lines_are_counts_only() -> None:
    line = render.forge_progress_line(
        checks=3, dryruns=1, measures=2, max_measures=24, submissions=1, max_submissions=6,
        spawns=0, spent_usd=0.25, cap_usd=5.0,
    )
    assert line == (
        "forge  checks 3  dryruns 1  measures 2/24  submits 1/6  spawns 0  "
        "$0.2500/$5.00 (API-equivalent)\n"
    )
    settings = ForgeSettings(enabled=True, max_usd=5.0)
    text = status_lines(settings, 10.0, None)
    assert text.isascii() and all(len(item) <= 100 for item in text.splitlines())
    assert "verifications for this spec so far: -/10" in unwrap(text)
    assert cap_line(10.0, settings, 2).endswith("2/10")
