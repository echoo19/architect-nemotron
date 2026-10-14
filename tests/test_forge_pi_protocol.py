"""Forge through the real pinned Pi SDK when the agent first answers with a bare tool name.

A live Forge session once ended after the model replied just ``read`` (zero checks,
measures or submissions). The parent now re-asks with a fixed correction, so the
forge tools are then called. Offline: fake adapters only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from forge_support import REPAIR, ensure_sandbox, forge_compiler, reply, spec

from architect.backends.pi import _REPAIR_MESSAGE
from architect.compiler.forge_settings import ForgeSettings
from architect.library import HarnessLibrary
from architect.models import FakeAdapter, ModelRequest, ModelResponse
from architect.telemetry import categorical

ROOT = Path(__file__).parents[1]
PI_READY = (
    ROOT / "architect/backends/pi-node/node_modules/@mariozechner/pi-coding-agent/package.json"
).is_file()


@pytest.fixture(autouse=True)
async def working_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    await ensure_sandbox(monkeypatch, tmp_path / "probe")


def calls(name: str, **arguments: Any) -> str:
    return json.dumps(
        {"tool_calls": [{"id": f"call-{name}", "name": name, "arguments": arguments}]}
    )


class Recording(FakeAdapter):
    def __init__(self, texts: list[str]) -> None:
        super().__init__("fake", responses=[reply(text, cost=0.0001) for text in texts])
        self.prompts: list[str] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.prompts.append(request.prompt)
        return await super().complete(request)


@pytest.mark.skipif(not PI_READY, reason="Pi dependencies are not installed")
async def test_read_style_first_answer_is_repaired_and_forge_tools_are_called(
    tmp_path: Path,
) -> None:
    script = [
        "read",  # the live failure: the tool's name instead of a JSON envelope
        calls("write", path="harness.py", content=REPAIR),
        "```json\n" + calls("forge_check") + "\n```",  # fenced envelopes also parse
        calls("forge_measure", cases=[0]),
        "Submitting now.\n" + calls("forge_submit", note="costlier repair"),
        "Submitted.",
    ]
    agent = Recording(script)
    library = HarnessLibrary(tmp_path / "library")
    compiler = forge_compiler(
        tmp_path, settings=ForgeSettings(enabled=True), agent=agent, library=library
    )
    result = await compiler.compile(spec())
    assert agent.calls == len(script)
    first, repaired = (json.loads(prompt) for prompt in agent.prompts[:2])
    assert {item["name"] for item in first["tools"]} >= {
        "forge_check",
        "forge_measure",
        "forge_submit",
        "write",
    }
    assert repaired["previous_reply"] == "read" and repaired["correction"] == _REPAIR_MESSAGE
    assert result.failures == ()
    artifact = result.artifact
    assert artifact is not None and artifact.licensed and artifact.forge is not None
    assert (artifact.directory / "harness.py").read_text(encoding="utf-8") == REPAIR
    record = json.loads((artifact.directory / "forge.json").read_text(encoding="utf-8"))
    assert record["checks"] == record["measures"] == record["submissions"] == 1
    assert record["accepted"] == 1
    label = categorical("forge_agent")
    rows = [row for row in compiler.runtime.store.rows() if row.preset_origin == label]
    # The repair turn is a metered model call like any other.
    assert len(rows) == 1 and rows[0].model_calls == len(script)
