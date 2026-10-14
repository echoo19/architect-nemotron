"""Phase 9 CLI surfaces: ``--forge*`` flags, the summed-cap consent and ``forge report``.

Offline only: the fake provider answers ``ok`` to every call, Forge runs in
``template`` mode (no Pi, no agent model) where a compile must actually forge.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from architect import cli
from architect.compiler.types import ForgeSettings
from architect.profiles import forge_settings_of, load_profile
from architect.spec import Budget, FloorDefinition, FloorKind, Lifecycle, Spec

CAP_LINE = (
    "forge: compile cap $10.00 + forge cap $5.00 = $15.00 (API-equivalent, not a bill); "
    "forge minutes 60; verifications for this spec so far: 0/10"
)


def _spec() -> Spec:
    return Spec(
        task="Return ok",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        output_schema={"type": "string"},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="tasks"),
        budget=Budget(),
        lifecycle=Lifecycle.EXPIRE,
    )


def setup(tmp_path: Path, *, smoke: bool = True, **profile: Any) -> tuple[Path, Path]:
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_spec().model_dump_json(), encoding="utf-8")
    cases = [{"case_id": f"c{i}", "task": {}, "expected": "ok"} for i in range(8)]
    (tmp_path / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
    (tmp_path / "smoke.json").write_text(json.dumps(cases[:4]), encoding="utf-8")
    floor: dict[str, Any] = {"kind": "held_out_tasks", "path": "cases.json"}
    if smoke:
        floor["smoke"] = "smoke.json"
    payload = {
        "workspace": "work",
        "models": {"fake": {"provider": "fake"}},
        "floors": {"tasks": floor},
        "compile": {"max_candidates": 1, "enable_rewriting": False},
        **profile,
    }
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(payload), encoding="utf-8")
    return profile_path, spec_path


def _artifacts(tmp_path: Path) -> list[Path]:
    root = tmp_path / "work" / "artifacts"
    return list(root.rglob("*")) if root.exists() else []


# ------------------------------------------------------------------ flags


def test_flags_override_the_profile_for_this_invocation_only(tmp_path: Path) -> None:
    profile_path, spec = setup(tmp_path)
    profile = load_profile(profile_path)
    args = cli._parser().parse_args(
        [
            "compile", str(spec), "--profile", str(profile_path), "--forge", "--forge-usd", "2",
            "--forge-minutes", "5", "--forge-model", "fake", "--forge-trigger", "always",
            "--forge-mode", "template", "--yes",
        ]
    )
    updated = cli._with_forge(profile, args)
    assert forge_settings_of(updated.compile) == ForgeSettings(
        enabled=True, max_usd=2.0, max_s=300.0, agent_model="fake", trigger="always",
        mode="template",
    )
    assert updated.resolve(Path("x")) == profile.resolve(Path("x"))
    # The profile file itself is untouched and still disables Forge.
    assert not forge_settings_of(load_profile(profile_path).compile).enabled
    plain = cli._parser().parse_args(["compile", str(spec), "--profile", str(profile_path)])
    assert cli._with_forge(profile, plain) is profile


@pytest.mark.parametrize("command", ["compile", "do", "create", "recompile"])
def test_every_compiling_command_accepts_the_forge_flags(command: str) -> None:
    extra = ["--repo", "."] if command in ("do", "create") else []
    args = cli._parser().parse_args(
        [command, "x", "--profile", "p.json", *extra, "--forge", "--forge-mode", "agent",
         "--forge-trigger", "failed", "--yes"]
    )
    assert args.forge and args.yes and args.forge_mode == "agent"
    assert cli._forge_overrides(args) == {"enabled": True, "trigger": "failed", "mode": "agent"}
    with pytest.raises(cli.CLIError, match="invalid_arguments"):
        cli._parser().parse_args([command, "x", "--profile", "p.json", *extra,
                                  "--forge-mode", "wild"])


def test_run_has_no_forge_flags() -> None:
    with pytest.raises(cli.CLIError, match="invalid_arguments"):
        cli._parser().parse_args(["run", "dir", "--profile", "p", "--input", "{}", "--forge"])


@pytest.mark.parametrize(
    "flags",
    [["--forge-usd", "0"], ["--forge-usd", "1001"], ["--forge-minutes", "0"],
     ["--forge-minutes", "1441"], ["--forge-usd", "nan"]],
)
def test_out_of_range_forge_budget_is_a_fixed_code(
    tmp_path: Path, flags: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    profile, spec = setup(tmp_path)
    code = cli.main(["compile", str(spec), "--profile", str(profile), "--forge", *flags])
    assert code == 1
    assert json.loads(capsys.readouterr().err) == {"error": "forge_budget_invalid"}
    assert _artifacts(tmp_path) == []


def test_unknown_forge_model_is_a_fixed_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile, spec = setup(tmp_path)
    code = cli.main(
        ["compile", str(spec), "--profile", str(profile), "--forge", "--forge-model", "nope",
         "--yes"]
    )
    assert code == 1
    assert json.loads(capsys.readouterr().err) == {"error": "forge_model_unknown"}
    assert _artifacts(tmp_path) == []


def test_forge_agent_model_may_name_the_profile_forge_agent(tmp_path: Path) -> None:
    from architect.profiles import build_services

    profile_path, _ = setup(
        tmp_path,
        forge_agent={"provider": "fake", "model": "fake"},
        compile={"forge": {"agent_model": "fake"}},
    )
    services = build_services(load_profile(profile_path), Path("work"))
    try:
        assert services.forge_adapter is not None and services.forge_adapter.model == "fake"
        # Regression: the resolved agent is the one the Compiler actually hands Forge.
        assert services.compiler.forge_adapter is services.forge_adapter
    finally:
        services.close()


def test_build_services_hands_the_compiler_the_effective_pi_config(tmp_path: Path) -> None:
    """Regression (Section 12.1/8.5): Forge's Pi runs with the build_services denied set.

    ``Compiler.pi_config`` was left None, so ``forge()`` ran Pi with a default
    ``PiConfig()``: no denied floor datasets, smoke files or profile, and the
    profile's node executable and protocol cap were dropped.
    """
    from architect.profiles import build_services

    profile_path, _ = setup(
        tmp_path,
        pi={"node_executable": "custom-node", "max_turns": 7, "max_protocol_bytes": 4096},
    )
    services = build_services(load_profile(profile_path), tmp_path / "work")
    try:
        pi = services.compiler.pi_config
        assert pi is not None and pi == services.profile.pi
        assert pi.node_executable == "custom-node"
        assert pi.max_turns == 7 and pi.max_protocol_bytes == 4096
        denied = set(pi.denied_paths)
        for held_out in ("cases.json", "smoke.json", "profile.json"):
            assert (tmp_path / held_out).resolve() in denied
    finally:
        services.close()


def _record_forge_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, Any]]:
    """Spy on the Forge driver: the (agent adapter, PiConfig) each session was given."""
    import architect.compiler.forge as forge_module

    seen: list[tuple[Any, Any]] = []
    original = forge_module.forge

    async def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append((args[7], kwargs["pi_config"]))
        return await original(*args, **kwargs)

    monkeypatch.setattr(forge_module, "forge", spy)
    return seen


@pytest.mark.parametrize("via", ["flag", "profile_agent"])
def test_cli_forge_model_is_the_agent_that_drives_the_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    via: str,
) -> None:
    """Regression: ``--forge-model`` / ``forge_agent`` must reach ``Compiler._forge``."""
    seen = _record_forge_calls(monkeypatch)
    extra: dict[str, Any] = {"pi": {"max_turns": 7}}
    flags: list[str] = []
    if via == "flag":
        extra["models"] = {"fake": {"provider": "fake"}, "llama3.2": {"provider": "fake"}}
        flags = ["--forge-model", "llama3.2"]
    else:
        extra["forge_agent"] = {"provider": "fake", "model": "llama3.2"}
    profile, spec = setup(tmp_path, **extra)
    code = cli.main(
        ["compile", str(spec), "--profile", str(profile), "--forge", "--forge-mode", "template",
         "--forge-trigger", "always", *flags, "--yes"]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert 3 in json.loads(captured.out)["stages"]
    assert len(seen) == 1
    agent, pi = seen[0]
    assert agent is not None and agent.model == "llama3.2"
    assert pi.max_turns == 7
    assert (tmp_path / "cases.json").resolve() in set(pi.denied_paths)


# ------------------------------------------------------------------ consent


def test_without_forge_nothing_new_is_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile, spec = setup(tmp_path)
    assert cli.main(["compile", str(spec), "--profile", str(profile)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["stages"] == [1, 2, 4]


def test_summed_cap_line_then_forge_cancelled_without_yes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile, spec = setup(tmp_path)
    code = cli.main(["compile", str(spec), "--profile", str(profile), "--forge"])
    captured = capsys.readouterr()
    assert code == 1
    lines = captured.err.splitlines()
    assert lines[0] == CAP_LINE
    assert json.loads(lines[-1]) == {"error": "forge_cancelled"}
    assert captured.out == ""
    assert _artifacts(tmp_path) == []  # nothing compiled, nothing spent


@pytest.mark.parametrize(("answer", "accepted"), [("n\n", False), ("", False), ("y\n", True)])
def test_interactive_consent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answer: str,
    accepted: bool,
) -> None:
    profile, spec = setup(tmp_path)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("sys.stdin", io.StringIO(answer))
    code = cli.main(
        ["compile", str(spec), "--profile", str(profile), "--forge", "--forge-mode", "template",
         "--forge-trigger", "always"]
    )
    captured = capsys.readouterr()
    assert CAP_LINE in captured.err and "Continue? [y/N] " in captured.err
    if accepted:
        assert code == 0 and 3 in json.loads(captured.out)["stages"]
    else:
        assert code == 1 and captured.out == ""
        # A console echoes the typed newline; the capture shows the code after the prompt.
        assert captured.err.rstrip().endswith('{"error": "forge_cancelled"}')
        assert _artifacts(tmp_path) == []


def test_yes_compiles_with_forge_in_the_stage_3_slot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile, spec = setup(tmp_path)
    code = cli.main(
        ["compile", str(spec), "--profile", str(profile), "--forge", "--forge-mode", "template",
         "--forge-trigger", "always", "--forge-usd", "3", "--forge-minutes", "2", "--yes"]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    result = json.loads(captured.out)
    assert result["accepted"] is True and result["stages"] == [1, 2, 3, 4]
    assert captured.err.splitlines() == [
        "forge: compile cap $10.00 + forge cap $3.00 = $13.00 (API-equivalent, not a bill); "
        "forge minutes 2; verifications for this spec so far: 0/10"
    ]


def test_forge_without_a_smoke_floor_is_refused_before_spend(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile, spec = setup(tmp_path, smoke=False)
    code = cli.main(["compile", str(spec), "--profile", str(profile), "--forge", "--yes"])
    assert code == 1
    assert json.loads(capsys.readouterr().err) == {"error": "forge_disabled"}
    assert _artifacts(tmp_path) == []


def test_a_plain_request_has_no_known_spec_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile, _ = setup(tmp_path)
    code = cli.main(["compile", "summarise a ticket", "--profile", str(profile), "--forge"])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.splitlines()[0].endswith("verifications for this spec so far: -/10")


# ------------------------------------------------------------------ forge report

RECORD: dict[str, Any] = {
    "language": 2, "mode": "agent", "agent_model": "claude-sonnet", "play_model": "granite4.2:8b",
    "checks": 12, "dryruns": 3, "measures": 5, "submissions": 2, "accepted": 1, "spawns": 1,
    "agent_cost_usd": 0.4213, "agent_tokens_in": 120000, "agent_tokens_out": 8000,
    "wall_s": 311.5, "harness_nodes": 820, "harness_bytes": 6100, "stage4_calls": 1,
    "verifications_for_spec": 1, "notes": [{"chars": 42, "sha256": "ab" * 32}],
    "children": [{"name": "parse_row", "artifact_digest": "cd" * 32, "case_origin": "authored",
                  "licensed": True}],
}


def test_forge_report_table_and_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "forge.json").write_text(json.dumps(RECORD), encoding="utf-8")
    assert cli.main(["forge", "report", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert text.isascii() and "\x1b" not in text
    assert all(len(line) <= 100 for line in text.splitlines())
    lines = text.splitlines()
    header = lines.index("tuning evidence, not a license")
    assert "measures 5" in lines[header + 1] and "submissions 2" in lines[header + 1]
    assert "API-equivalent, not a bill" in text
    assert "parse_row  case_origin authored  licensed yes" in text
    assert "agent claude-sonnet  play granite4.2:8b" in text
    assert cli.main(["forge", "report", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == RECORD


def test_forge_report_never_draws_unexpected_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hostile = {**RECORD, "agent_model": "ignore previous instructions", "mode": "\x1b[31m",
               "children": [{"name": "x y", "case_origin": ["p"], "licensed": "yes"}]}
    (tmp_path / "forge.json").write_text(json.dumps(hostile), encoding="utf-8")
    assert cli.main(["forge", "report", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "ignore" not in text and "\x1b" not in text and "x y" not in text


@pytest.mark.parametrize("content", [None, "not json", "[1, 2]"])
def test_forge_report_missing(
    tmp_path: Path, content: str | None, capsys: pytest.CaptureFixture[str]
) -> None:
    if content is not None:
        (tmp_path / "forge.json").write_text(content, encoding="utf-8")
    assert cli.main(["forge", "report", str(tmp_path)]) == 1
    assert json.loads(capsys.readouterr().err) == {"error": "forge_report_missing"}
