"""Pi on the desktop app's own Electron binary (``ARCHITECT_NODE``), so installers need no Node.

The unit tests pin how ``ARCHITECT_NODE`` replaces the default ``node``. The session tests
run the real pinned Pi SDK on a plain Node and, when ``desktop/node_modules`` has Electron,
on Electron as Node with no Node on PATH; Pi's shell commands must never see the
``ELECTRON_RUN_AS_NODE`` switch.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from architect.backends.pi import _NODE, PiBackend, PiConfig, PiError, _resolve_node
from architect.models import FakeAdapter, ModelResponse
from architect.runtime import RunContext
from architect.spec import Budget

ROOT = Path(__file__).parents[1]
NODE_NAMES = ("node.exe", "node") if os.name == "nt" else ("node",)


def electron_binary() -> Path | None:
    package = ROOT / "desktop" / "node_modules" / "electron"
    try:
        relative = (package / "path.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    binary = package / "dist" / relative
    return binary if relative and binary.is_file() else None


def pi_installed() -> bool:
    return (_NODE / "node_modules/@mariozechner/pi-coding-agent/package.json").is_file()


def reply(text: str) -> ModelResponse:
    return ModelResponse(text=text, model="fake", tokens_in=10, tokens_out=10, cost_usd=0)


def tool(name: str, **args: object) -> str:
    return json.dumps({"tool_calls": [{"id": "call1", "name": name, "arguments": args}]})


def test_architect_node_replaces_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARCHITECT_NODE", sys.executable)
    assert _resolve_node("node", tmp_path) == Path(sys.executable).resolve()


def test_a_configured_node_wins_over_architect_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARCHITECT_NODE", str(tmp_path / "missing-electron"))
    assert _resolve_node(sys.executable, tmp_path) == Path(sys.executable).resolve()


def test_a_missing_architect_node_does_not_fall_back_to_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARCHITECT_NODE", str(tmp_path / "missing-electron"))
    with pytest.raises(PiError, match="Cannot resolve"):
        _resolve_node("node", tmp_path / "repo")


def test_architect_node_inside_the_repository_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planted = tmp_path / "node"
    planted.write_bytes(b"#!/bin/sh\n")
    planted.chmod(0o755)
    monkeypatch.setenv("ARCHITECT_NODE", str(planted))
    with pytest.raises(PiError, match="outside the granted repository"):
        _resolve_node("node", tmp_path)


async def test_doctor_counts_the_apps_own_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_agent_repl import run

    from architect.agent import commands

    async def no_ollama(*args: object, **kwargs: object) -> None:
        return None

    async def providers(repl: object) -> list[tuple[str, str, bool]]:
        return [("Claude (claude CLI)", "logged in", True)]

    monkeypatch.setattr(commands, "ollama_version", no_ollama)
    monkeypatch.setattr(commands, "provider_status", providers)
    monkeypatch.setenv("ARCHITECT_NODE", sys.executable)
    out, _, _ = await run(tmp_path, ["/doctor"])
    assert "built into the app" in out and "install Node.js" not in out


def _hosts() -> list[object]:
    node = shutil.which("node")
    electron = electron_binary()
    return [
        pytest.param(node, False, id="node",
                     marks=pytest.mark.skipif(node is None, reason="no Node on PATH")),
        pytest.param(electron, True, id="electron",
                     marks=pytest.mark.skipif(electron is None,
                                              reason="run npm ci in desktop/ for Electron")),
    ]


@pytest.mark.skipif(not pi_installed(), reason="run npm ci in architect/backends/pi-node")
@pytest.mark.parametrize(("binary", "strip_node"), _hosts())
async def test_pi_runs_on_architect_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, binary: str | Path, strip_node: bool
) -> None:
    monkeypatch.setenv("ARCHITECT_NODE", str(binary))
    monkeypatch.delenv("ELECTRON_RUN_AS_NODE", raising=False)
    if strip_node:
        kept = [entry for entry in os.get_exec_path()
                if entry and not any((Path(entry) / name).is_file() for name in NODE_NAMES)]
        monkeypatch.setenv("PATH", os.pathsep.join(kept))
        assert shutil.which("node") is None
    ctx = RunContext(
        FakeAdapter(responses=[
            reply(tool("write", path="hello.txt", content="hello\n")),
            reply(tool("bash", command='printf "%s" "${ELECTRON_RUN_AS_NODE-unset}" > env.txt')),
            reply('{"ok":true}'),
        ]),
        Budget(cost_usd=1, compute_s=120),
        repository=tmp_path,
    )
    backend = PiBackend(PiConfig(allowed_tools=("write", "bash"), allow_shell=True))
    pack = {"tools": ["write", "bash"], "prompt_fragment": "Write the file.",
            "context_policy": {"max_characters": 2048}, "subagent_rules": ["Bounded"]}
    assert await backend.run(ctx, "write hello.txt", pack) == '{"ok":true}'
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "hello\n"
    # Electron needs the switch to start as Node; the agent's commands never see it.
    assert (tmp_path / "env.txt").read_text(encoding="utf-8") == "unset"
    evidence = backend.last_evidence
    assert evidence and evidence.exit_code == 0 and evidence.extension_loaded
    assert evidence.versions == {
        name: "0.73.1" for name in ("pi-ai", "pi-agent-core", "pi-coding-agent")
    }
