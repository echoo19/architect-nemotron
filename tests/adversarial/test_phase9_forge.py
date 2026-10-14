"""Phase 9 adversarial round for Forge: nothing held out ever reaches the agent.

Every held-out case, its metadata and held-out game ids are fingerprinted. A
scripted session calls every tool (including invalid and hostile arguments,
spawns and submissions) and then every tool result, every workspace file and
forge.json are searched for the fingerprints and for Stage 4 report fields.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from forge_support import (
    HELD_OUT_MARK,
    SECRET,
    TUNING_MARK,
    ScriptedPi,
    ensure_sandbox,
    forge_compiler,
    install_pi,
    spec,
)

from architect.backends.pi import _granted_path
from architect.compiler.forge_settings import ForgeSettings
from architect.floors import EvaluationCase, HeldOutTasks
from architect.profiles import build_services, load_profile
from architect.spec import FloorDefinition

CHEAP_OK = (
    "from pydantic import JsonValue\n"
    "from architect.runtime import RunContext\n\n"
    "async def run(context: RunContext, task: JsonValue) -> JsonValue:\n"
    '    return "ok"\n'
)
GAME_MARK = "heldout-game-"


def fingerprinted(count: int = 16) -> Callable[[FloorDefinition], HeldOutTasks]:
    def factory(definition: FloorDefinition) -> HeldOutTasks:
        records = []
        for index in range(count):
            held = index % 2 == 1
            mark = HELD_OUT_MARK if held else TUNING_MARK
            game = f"{GAME_MARK}{index}" if held else f"tuning-game-{index}"
            records.append(
                EvaluationCase(
                    case_id=f"case-{index:03d}",
                    task={
                        "index": index,
                        "text": f"{mark}{index:03d}_input_padding_text",
                        "game": {"backend": "local", "game_id": game, "revision": "r1"},
                    },
                    expected="ok",
                    metadata={"private": SECRET, "note": f"{mark}{index:03d}_metadata"},
                )
            )
        return HeldOutTasks(definition, records)

    return factory


def hostile_session() -> list[dict[str, object]]:
    authored = [{"input": f"authored {i}", "expected": "ok"} for i in range(8)]
    return [
        {"tool": "forge_check", "arguments": {}},
        {"tool": "forge_check", "arguments": {"path": "../private/candidates/1/harness.py"}},
        {"tool": "forge_dryrun", "arguments": {"case": 0, "replies": ["ok"]}},
        {"tool": "forge_dryrun", "arguments": {"case": 99}},
        {"tool": "forge_measure", "arguments": {}},
        {"tool": "forge_measure", "arguments": {"cases": [0, 1, 2, 3, 4, 5, 6, 7], "trace": True}},
        {"tool": "forge_measure", "arguments": {"cases": [8]}},
        {"tool": "forge_measure", "arguments": {"smoke": True}},
        {
            "tool": "forge_spawn",
            "arguments": {
                "name": "proj",
                "goal": "Answer ok",
                "input_schema": {"type": "string"},
                "output_schema": {"type": "string"},
                "cases": {"projection": {"input": "/text", "expected": ""}},
            },
        },
        {
            "tool": "forge_spawn",
            "arguments": {
                "name": "auth",
                "goal": "Answer ok",
                "input_schema": {"type": "string"},
                "output_schema": {"type": "string"},
                "cases": {"authored": authored},
            },
        },
        {"tool": "forge_submit", "arguments": {"note": "incumbent first"}},
        {"write": {"path": "harness.py", "content": CHEAP_OK}},
        {"tool": "forge_submit", "arguments": {"note": "constant answer"}},
        {"tool": "forge_measure", "arguments": {}},
        {"text": "done"},
    ]


def forbidden_marks(count: int = 16) -> list[str]:
    marks = [SECRET, "held_out_count", "verification_id", '"signature"', "_metadata"]
    for index in range(1, count, 2):
        marks += [f"{HELD_OUT_MARK}{index:03d}", f"{GAME_MARK}{index}"]
    return marks


async def test_no_held_out_signal_in_any_tool_result_workspace_file_or_forge_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await ensure_sandbox(monkeypatch, tmp_path / "probe")
    pi = install_pi(monkeypatch, ScriptedPi(hostile_session()))
    compiler = forge_compiler(
        tmp_path, settings=ForgeSettings(enabled=True, trigger="always"), play="ok", count=16
    )
    factory = fingerprinted()
    compiler.floor_factory = factory
    compiler.smoke_factory = factory
    result = await compiler.compile(spec())
    names = [name for name, _ in pi.results]
    assert set(names) == {
        "forge_check", "forge_dryrun", "forge_measure", "forge_spawn", "forge_submit",
    }
    results = {name: [value for tool, value in pi.results if tool == name] for name in names}
    assert results["forge_spawn"][0]["licensed"] is True  # type: ignore[index]
    assert results["forge_spawn"][1]["licensed"] is True  # type: ignore[index]
    assert results["forge_measure"][2]["code"] == "invalid_case"  # type: ignore[index]
    # The schema pattern refuses the traversal before any trusted code reads a file.
    assert results["forge_check"][1]["code"] == "invalid_arguments"  # type: ignore[index]
    assert results["forge_submit"][-1]["accepted"] is True  # type: ignore[index]
    artifact = result.artifact
    assert artifact is not None and artifact.licensed and artifact.report is not None
    corpus = {
        **{f"result {i} {name}": json.dumps(value) for i, (name, value) in enumerate(pi.results)},
        **{f"workspace {path}": text for path, text in pi.snapshot.items()},
        "forge.json": (artifact.directory / "forge.json").read_text(encoding="utf-8"),
    }
    marks = [*forbidden_marks(), artifact.report.verification_id, artifact.report.signature]
    for where, text in corpus.items():
        for mark in marks:
            assert mark not in text, (where, mark)
    # Tuning data is deliberately visible (Section 10.1): the fingerprinting is not vacuous.
    assert any(TUNING_MARK in text for text in corpus.values())
    assert not any(
        part in path for path in pi.snapshot for part in ("candidates", "spawn_datasets")
    )


def probe_confinement(repo: Path, denied_paths: tuple[Path, ...], seen: dict[str, Path]) -> None:
    workspace = repo.resolve()
    private = next(
        path for path in denied_paths if "architect-forge-private-" in str(path)
    ).resolve()
    seen.update(workspace=workspace, private=private)
    denied = tuple(path.resolve() for path in denied_paths)
    (private / "candidates").mkdir(exist_ok=True)
    (private / "candidates" / "secret.txt").write_text("x", encoding="utf-8")
    for raw in (
        str(private / "candidates" / "secret.txt"),
        f"../{private.name}/candidates/secret.txt",
        "..\\" + private.name + "\\spawn_datasets\\a.jsonl",
    ):
        assert not _granted_path(workspace, raw, "read", 1 << 20, denied)
    assert _granted_path(workspace, "harness.py", "read", 1 << 20, denied)


async def test_pi_workspace_cannot_reach_the_private_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await ensure_sandbox(monkeypatch, tmp_path / "probe")
    seen: dict[str, Path] = {}

    class Probe(ScriptedPi):
        async def run(self, context, task, pack):  # type: ignore[no-untyped-def]
            assert self.config is not None
            probe_confinement(Path(task["repo"]), self.config.denied_paths, seen)
            return await super().run(context, task, pack)

    install_pi(monkeypatch, Probe([{"text": "done"}]))
    compiler = forge_compiler(
        tmp_path, settings=ForgeSettings(enabled=True, trigger="always"), play="ok"
    )
    await compiler.compile(spec())
    workspace, private = seen["workspace"], seen["private"]
    assert not private.is_relative_to(workspace) and not workspace.is_relative_to(private)
    for root in (tmp_path / "artifacts", tmp_path / "library", tmp_path / "telemetry"):
        assert not workspace.is_relative_to(root.resolve())
    # The session's workspace and private store are deleted afterwards.
    assert not workspace.exists() and not private.exists()


def floor_profile(root: Path) -> Path:
    """A profile whose floor dataset and smoke file live on disk, as in real operation."""
    root.mkdir(parents=True, exist_ok=True)
    rows = [
        {"case_id": f"case-{index:03d}", "task": {"index": index}, "expected": "ok"}
        for index in range(6)
    ]
    (root / "cases.json").write_text(json.dumps(rows), encoding="utf-8")
    (root / "smoke.json").write_text(json.dumps(rows), encoding="utf-8")
    (root / "responses.json").write_text("[]", encoding="utf-8")
    profile = root / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "workspace": ".",
                "models": {"fake": {"provider": "fake", "fixture": "responses.json"}},
                "floors": {
                    "forge-tasks": {
                        "kind": "held_out_tasks",
                        "path": "cases.json",
                        "smoke": "smoke.json",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return profile


def probe_floor_denial(
    repo: Path, denied_paths: tuple[Path, ...], files: tuple[Path, ...]
) -> tuple[Path, ...]:
    """Pi's path grant refuses every floor file however it is spelled (Section 10.5)."""
    workspace = repo.resolve()
    denied = tuple(path.resolve() for path in denied_paths)
    for path in files:
        assert path.is_file() and path in denied
        # The absolute path and a relative traversal, for every file tool.
        for raw in (str(path), os.path.relpath(path, workspace)):
            for tool in ("read", "write", "edit"):
                assert not _granted_path(workspace, raw, tool, 1 << 20, denied)
        # The denied set itself refuses the dataset, even from a repo that holds it.
        assert not _granted_path(path.parent, path.name, "read", 1 << 20, denied)
    other = files[0].parent / "unrelated.txt"
    other.write_text("x", encoding="utf-8")
    assert _granted_path(other.parent, other.name, "read", 1 << 20, denied)
    assert _granted_path(workspace, "TUNING.md", "read", 1 << 20, denied)
    return denied


async def test_pi_workspace_cannot_read_the_floor_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await ensure_sandbox(monkeypatch, tmp_path / "probe")
    profile = load_profile(floor_profile(tmp_path / "profile"))
    services = build_services(profile, Path("."))
    try:
        pi_config = services.profile.pi
    finally:
        services.close()
    dataset = (tmp_path / "profile" / "cases.json").resolve()
    smoke = (tmp_path / "profile" / "smoke.json").resolve()
    seen: dict[str, object] = {}

    class Probe(ScriptedPi):
        async def run(self, context, task, pack):  # type: ignore[no-untyped-def]
            assert self.config is not None
            seen["denied"] = probe_floor_denial(
                Path(task["repo"]), self.config.denied_paths, (dataset, smoke)
            )
            return await super().run(context, task, pack)

    pi = install_pi(monkeypatch, Probe([{"tool": "forge_check", "arguments": {}}, {"text": "x"}]))
    compiler = forge_compiler(
        tmp_path / "run", settings=ForgeSettings(enabled=True, trigger="always"), play="ok"
    )
    # The Compiler receives the profile's effective Pi config (floor datasets denied).
    compiler.pi_config = pi_config
    await compiler.compile(spec())
    assert pi.runs == 1 and "denied" in seen
    # The dataset paths never reach any tool result or workspace file.
    corpus = [json.dumps(value) for _, value in pi.results] + list(pi.snapshot.values())
    for text in corpus:
        assert str(dataset) not in text and dataset.name not in text
        assert str(smoke) not in text and smoke.name not in text
