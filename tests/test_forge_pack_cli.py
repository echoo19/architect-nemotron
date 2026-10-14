"""Where the Forge pack hash shows (W3b): ``architect forge pack``, the build plan card and
``architect forge report``. Offline: no model is called outside scripted fakes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from forge_support import ScriptedPi, install_pi
from test_agent_build import (  # noqa: F401 - ``harness`` and ``sandbox`` are fixtures
    CHEAP_OK,
    DESCRIPTION,
    INTERVIEW,
    drafted,
    harness,
    pi_session,
    sandbox,
    spec_json,
)
from test_cli_forge import RECORD
from test_forge_pack import PREREG_TERMS, receipt_spec

from architect import cli
from architect.agent.build import _forge_settings, make
from architect.compiler.forge_pack import pack_hash, pack_manifest, pack_settings
from architect.compiler.forge_settings import ForgeSettings

ROOT = Path(__file__).parents[1]


def run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_forge_pack_prints_the_hash_its_parts_and_a_clean_check(
    capsys: pytest.CaptureFixture[str],
) -> None:
    terms = [part for term in PREREG_TERMS for part in ("--term", term)]
    rules = ROOT / "examples/receipts/RULES.md"
    code, out, _ = run(["forge", "pack", *terms, "--source", str(rules)], capsys)
    assert code == 0
    lines = out.splitlines()
    assert lines[0] == f"forge pack {pack_hash(ForgeSettings())}"
    assert lines[1] == "format architect-forge-pack/1"
    for name, digest in pack_manifest(ForgeSettings()).items():
        assert f"  {name:<30} {digest[:16]}" in lines
    assert lines[-1] == "task text: none found (6 terms, 1 source, runs of 8 words)"
    assert out.isascii() and all(len(line) <= 100 for line in lines)


def test_forge_pack_json_takes_a_spec_and_a_profile(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(receipt_spec().model_dump_json(), encoding="utf-8")
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps({"models": {"fake": {"provider": "fake"}},
                    "compile": {"forge": {"enabled": True, "agent_max_tokens": 8192}}}),
        encoding="utf-8",
    )
    code, out, _ = run(
        ["forge", "pack", "--json", "--profile", str(profile), "--spec", str(spec_path)], capsys
    )
    assert code == 0
    payload = json.loads(out)
    settings = ForgeSettings(agent_max_tokens=8192)
    assert payload["pack_sha256"] == pack_hash(settings) != pack_hash(ForgeSettings())
    assert payload["format"] == "architect-forge-pack/1"
    assert payload["parts"] == pack_manifest(settings)
    assert payload["settings"] == pack_settings(settings) and "enabled" not in payload["settings"]
    assert payload["check"] == {
        "terms": ["item_count"],  # the spec's identifier-like field names
        "sources": 1,  # its task text (the receipt rules)
        "run_words": 8,
        "clean": True,
        "findings": [],
    }


def test_forge_pack_reports_task_text_and_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(["forge", "pack", "--term", "rewording"], capsys)
    assert code == 1
    assert out.splitlines()[-2:] == [
        "task text: 1 found (1 term, 0 sources, runs of 8 words)",
        "  prompt/instructions  term  rewording",
    ]
    copied = tmp_path / "copied.txt"
    copied.write_text("Call forge_check until it returns ok. It is free.", encoding="utf-8")
    code, out, _ = run(["forge", "pack", "--json", "--source", str(copied)], capsys)
    check = json.loads(out)["check"]
    assert code == 1 and check["clean"] is False
    assert check["findings"][0] == {
        "part": "prompt/instructions", "kind": "words",
        "text": "call forge check until it returns ok it",
    }


def test_forge_pack_takes_the_compile_flags_that_change_hashed_settings(
    capsys: pytest.CaptureFixture[str],
) -> None:
    flags = ["--forge-usd", "1.75", "--forge-minutes", "25", "--forge-model", "ultra"]
    code, out, _ = run(["forge", "pack", "--json", *flags], capsys)
    assert code == 0
    settings = ForgeSettings(max_usd=1.75, max_s=1500.0, agent_model="ultra")
    assert json.loads(out)["pack_sha256"] == pack_hash(settings) != pack_hash(ForgeSettings())
    code, out, _ = run(["forge", "pack", "--json", "--forge-mode", "template"], capsys)
    assert json.loads(out)["pack_sha256"] == pack_hash(ForgeSettings(mode="template"))


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (["--source", "missing-file.txt"], "invalid_arguments"),
        (["--spec", "missing-spec.json"], "invalid_spec_file"),
        (["--forge-usd", "0"], "forge_budget_invalid"),
    ],
)
def test_forge_pack_input_errors_are_fixed_codes(
    capsys: pytest.CaptureFixture[str], argv: list[str], error: str
) -> None:
    code, _, err = run(["forge", "pack", *argv], capsys)
    assert code == 1 and json.loads(err) == {"error": error}


def test_forge_report_shows_the_pack_and_the_turns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    turn: dict[str, Any] = {
        "turn": 1, "prompt_tokens": 1000, "cached_tokens": 500, "reasoning_tokens": 50,
        "completion_tokens": 200, "cost_usd": 0.0016, "seconds": 3.2, "stop": "tool_use",
        "estimated": False, "ok": True,
    }
    record = {
        **RECORD,
        "pack_sha256": "ab" * 32,
        "turns": [turn, {**turn, "turn": 2, "stop": "max_tokens", "ok": True},
                  {**turn, "turn": 3, "prompt_tokens": 0, "cached_tokens": 0,
                   "reasoning_tokens": 0, "completion_tokens": 0, "ok": False, "stop": None}],
    }
    (tmp_path / "forge.json").write_text(json.dumps(record), encoding="utf-8")
    code, out, _ = run(["forge", "report", str(tmp_path)], capsys)
    assert code == 0
    assert f"forge pack {'ab' * 32}" in out.splitlines()
    assert (
        "  turns 3  prompt 2000 (cached 50%)  completion 400 (reasoning 25%)  cut 1  failed 1"
        in out.splitlines()
    )
    # A record from before these fields reads as it did.
    (tmp_path / "forge.json").write_text(json.dumps(RECORD), encoding="utf-8")
    code, out, _ = run(["forge", "report", str(tmp_path)], capsys)
    assert code == 0 and "forge pack" not in out and "turns" not in out


async def test_the_plan_card_shows_the_hash_forge_json_records(  # type: ignore[no-untyped-def]
    harness, sandbox: None, monkeypatch: pytest.MonkeyPatch  # noqa: F811 - fixtures
) -> None:
    install_pi(monkeypatch, ScriptedPi(pi_session(CHEAP_OK)))
    h = harness([INTERVIEW, spec_json("invoice-cases"), drafted()], ["", "", "", ""],
                engineer=None)
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result["licensed"] is True, result
    digest, note = h.rows()["forge pack"].split(" ", 1)
    assert note == "(no text from this task)"
    assert digest == pack_hash(_forge_settings(h.ctx.services, {}))
    installed = h.ctx.services.registry.get(str(result["installed"]))
    record = json.loads((installed.directory / "forge.json").read_text(encoding="utf-8"))
    assert record["pack_sha256"] == digest
    assert [row["turn"] for row in record["turns"]] == [1, 2, 3]
