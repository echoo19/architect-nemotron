"""Shared fixtures for sandbox language v2 tests: artifacts, a stub turn context, a runner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

from architect.games.execution import GameExecutionPolicy
from architect.games.types import GameLimits
from architect.models import FakeAdapter, ModelAdapter
from architect.presets import get_preset
from architect.runtime import RunContext
from architect.runtime import _sandbox_worker as worker
from architect.runtime.sandbox import _encode
from architect.spec import (
    Budget,
    FloorDefinition,
    FloorKind,
    ForgeBinding,
    HarnessArtifact,
    Spec,
    SubharnessRef,
)

DEFAULT_FUEL = 10_000_000


def text_spec() -> Spec:
    return Spec(
        task="summarize",
        input_schema={},
        output_schema={"type": "string"},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="private-set"),
        budget=Budget(),
    )


def game_spec(max_turns: int = 2) -> Spec:
    policy = GameExecutionPolicy(
        small_model="fake",
        manifest_digest="0" * 64,
        support_dataset="support",
        limits=GameLimits(max_actions=max_turns, max_turns=max_turns),
    )
    return Spec(
        task="Play",
        input_schema={},
        output_schema={},
        domain="game",
        model_target="fake",
        budget=Budget(cost_usd=1, compute_s=10),
        preset_hint="explorer",
        execution_config=policy.model_dump(mode="json"),
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_GAME_SCORE, limit=1, dataset="support"),
    )


def binding(
    *, fuel: int = DEFAULT_FUEL, call_limit: int = 128, children: tuple[str, ...] = ()
) -> ForgeBinding:
    refs = {
        name: SubharnessRef(
            artifact_digest="a" * 64,
            spec_hash="b" * 64,
            input_schema_sha256="c" * 64,
            output_schema_sha256="d" * 64,
            case_origin="authored",
        )
        for name in children
    }
    return ForgeBinding(call_limit=call_limit, fuel=fuel, subharnesses=refs)


def make_artifact(
    directory: Path,
    source: str | None = None,
    *,
    spec: Spec | None = None,
    preset_name: str = "synthesizer",
    forge: ForgeBinding | None = None,
    v1: bool = False,
) -> HarnessArtifact:
    """A sandbox artifact bound to language 2 (or 1 with ``v1=True``)."""
    spec = spec or text_spec()
    preset = get_preset(preset_name)
    params = preset.validate_params({})
    directory.mkdir(parents=True, exist_ok=True)
    preset.emit(directory, spec, params, preset.components)
    artifact = HarnessArtifact(
        spec=spec,
        directory=directory,
        preset=preset.name,
        params=params,
        components=preset.components,
        model="fake",
        execution_mode="sandbox",
        forge=None if v1 else (forge or binding()),
    )
    if source is not None:
        (directory / "harness.py").write_text(source, encoding="utf-8")
    return artifact


def game_artifact(directory: Path, source: str, *, max_turns: int = 2,
                  calls_per_turn: int = 2) -> HarnessArtifact:
    return make_artifact(
        directory,
        source,
        spec=game_spec(max_turns),
        preset_name="explorer",
        forge=binding(call_limit=max_turns * calls_per_turn),
    )


def body(*lines: str) -> str:
    """``async def run`` with the given body lines."""
    return "async def run(context, task):\n" + "".join(f"    {line}\n" for line in lines)


def interpret(
    monkeypatch: pytest.MonkeyPatch,
    artifact: HarnessArtifact,
    source: str,
    task: JsonValue = None,
    replies: tuple[JsonValue, ...] = (),
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run the v2 interpreter in process; return (final frame, capability messages).

    The pipe is replaced at the byte level, so capability RPCs are metered exactly
    as through the real worker (the round trip plus the bytes sent and received).
    """
    sent: list[dict[str, Any]] = []
    queue = list(replies)

    def write(data: bytes) -> None:
        sent.append(json.loads(data))

    def read() -> bytes:
        return _encode({"value": queue.pop(0) if queue else "ok"})

    monkeypatch.setattr(worker, "_write", write)
    monkeypatch.setattr(worker, "_read", read)
    worker._run_v2(source, artifact, task)
    return sent[-1], sent[:-1]


class TurnContext(RunContext):
    """A run context whose game and sub-harness capabilities are local stubs."""

    def __init__(self, adapter: ModelAdapter | None = None, *, actions_left: int = 8) -> None:
        super().__init__(adapter or FakeAdapter(), Budget(cost_usd=10, compute_s=60))
        self.observes = 0
        self.steps: list[JsonValue] = []
        self.children: list[tuple[str, JsonValue]] = []
        self.actions_left = actions_left

    def observation(self, *, refused: bool = False) -> dict[str, JsonValue]:
        return {
            "frame": [[0, 1], [2, 3]],
            "frame_count": 1,
            "state": "NOT_FINISHED",
            "available_actions": ["ACTION1", "ACTION2"],
            "levels_completed": 0,
            "win_levels": 1,
            "actions_taken": len(self.steps),
            "actions_left": self.actions_left,
            "refused": refused,
        }

    async def game_observe(self) -> JsonValue:
        self.observes += 1
        return self.observation()

    async def game_step(self, action: JsonValue) -> JsonValue:
        if self.actions_left == 0:
            return self.observation(refused=True)
        self.steps.append(action)
        self.actions_left -= 1
        return self.observation()

    async def subharness(self, parent: HarnessArtifact, name: str, value: JsonValue) -> JsonValue:
        assert parent.forge is not None and name in parent.forge.subharnesses
        self.children.append((name, value))
        return {"echo": value}


def wall_limit(seconds: float) -> float:
    """A wall-clock bound for fuel tests, scaled when coverage tracing is on.

    Fuel bounds the interpreter's work; the seconds that work takes depend on the
    host. Coverage tracing (the Linux CI job) slows pure-Python interpretation
    several-fold, so there the bound is five times wider. ARCHITECT_TIMING_SLACK
    scales it further on a known-slow machine.
    """
    import os
    import sys

    monitoring = getattr(sys, "monitoring", None)
    traced = (
        sys.gettrace() is not None
        or any(key.startswith("COV_CORE") for key in os.environ)
        or (monitoring is not None and monitoring.get_tool(monitoring.COVERAGE_ID) is not None)
    )
    return seconds * (5.0 if traced else 1.0) * float(os.environ.get("ARCHITECT_TIMING_SLACK", "1"))
