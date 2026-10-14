"""Forge through the real pinned Pi SDK with an agent that streams native tool calls (P3).

The same session as ``test_forge_pi_protocol`` (write, check, measure, submit), but the
agent model returns provider tool calls instead of JSON envelopes. Forge's agent meter
forwards the streamed chats, so its tokens and dollars still cover every call. Offline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from forge_support import REPAIR, ensure_sandbox, forge_compiler, spec
from test_pi_native import COST, Streaming, call, reply

from architect.backends.pi import _NATIVE_INSTRUCTION
from architect.compiler.forge_settings import ForgeSettings
from architect.library import HarnessLibrary
from architect.telemetry import categorical

ROOT = Path(__file__).parents[1]
PI_READY = (
    ROOT / "architect/backends/pi-node/node_modules/@mariozechner/pi-coding-agent/package.json"
).is_file()


@pytest.fixture(autouse=True)
async def working_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    await ensure_sandbox(monkeypatch, tmp_path / "probe")


@pytest.mark.skipif(not PI_READY, reason="Pi dependencies are not installed")
async def test_a_forge_session_runs_on_native_tool_calls(tmp_path: Path) -> None:
    script = [
        reply(call("write", "chatcmpl-tool-1", path="harness.py", content=REPAIR)),
        reply(call("forge_check", "chatcmpl-tool-2")),
        reply(call("forge_measure", "chatcmpl-tool-3", cases=[0])),
        reply(call("forge_submit", "chatcmpl-tool-4", note="costlier repair"), text="Submitting."),
        reply(text="Submitted."),  # submissions remain, so this is asked once more
        reply(text="Submitted; nothing more to try."),
    ]
    agent = Streaming(*script)
    library = HarnessLibrary(tmp_path / "library")
    compiler = forge_compiler(
        tmp_path,
        settings=ForgeSettings(enabled=True),
        agent=agent,  # type: ignore[arg-type]
        library=library,
    )
    result = await compiler.compile(spec())
    assert len(agent.sent) == len(script)  # one native call per turn, never complete()
    system, _, specs, _ = agent.sent[0]
    assert system.startswith(_NATIVE_INSTRUCTION) and "You are Forge." in system
    assert {"write", "forge_check", "forge_measure", "forge_submit"} <= {s.name for s in specs}
    assert result.failures == ()
    artifact = result.artifact
    assert artifact is not None and artifact.licensed and artifact.forge is not None
    assert (artifact.directory / "harness.py").read_text(encoding="utf-8") == REPAIR
    record = json.loads((artifact.directory / "forge.json").read_text(encoding="utf-8"))
    assert record["checks"] == record["measures"] == record["submissions"] == 1
    assert record["accepted"] == 1
    # Forge's meter saw every streamed call at the usage the provider reported.
    assert record["agent_tokens_in"] == 100 * len(script)
    assert record["agent_tokens_out"] == 20 * len(script)
    assert record["agent_cost_usd"] == pytest.approx(COST * len(script))
    label = categorical("forge_agent")
    rows = [row for row in compiler.runtime.store.rows() if row.preset_origin == label]
    assert len(rows) == 1 and rows[0].model_calls == len(script)
    assert rows[0].cost_usd == pytest.approx(COST * len(script))
