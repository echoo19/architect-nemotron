"""Ladder controls (W3b): Stage 2 candidates per rung, Forge on the first rungs only, and
the cheapest licensed rung ships.

The hero build reads "Lightning: best of 4 presets then Forge; Super: plain (1 candidate);
Ultra: plain (1 candidate)". Offline: fake play models and a scripted Pi session.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from forge_support import ScriptedPi, agent_adapter, floor_factory, install_pi, reply, spec

from architect.backends.pi import PiConfig
from architect.compiler import (
    CompileConfig,
    Compiler,
    add_stage_observer,
    remove_stage_observer,
    tuning,
)
from architect.compiler.forge_settings import ForgeSettings
from architect.library import HarnessLibrary
from architect.models import FakeAdapter
from architect.profiles import Profile
from architect.runtime import Runtime
from architect.spec import Budget
from architect.telemetry import TelemetryStore

LADDER = ("fake", "llama3.2", "gpt-4o-mini")


def plays(replies: dict[str, tuple[str, float]]) -> Callable[[str], FakeAdapter]:
    def factory(model: str) -> FakeAdapter:
        text, cost = replies[model]
        return FakeAdapter(model, responses=[reply(text, model=model, cost=cost)] * 4096)

    return factory


def ladder_compiler(root: Path, play: Callable[[str], FakeAdapter], **config: Any) -> Compiler:
    floors = floor_factory(6)
    return Compiler(
        root / "artifacts",
        Runtime(TelemetryStore(root / "telemetry")),
        play,
        floors,
        config=CompileConfig.model_validate(
            {
                "model_ladder": LADDER,
                "max_candidates": 2,
                "enable_rewriting": False,
                "max_rewrites": 0,
                "compile_budget": Budget(cost_usd=10, compute_s=600),
                **config,
            }
        ),
        library=HarnessLibrary(root / "library"),
        smoke_factory=floors,
        forge_adapter=agent_adapter(),
        pi_config=PiConfig(),
    )


@pytest.fixture
def tuned(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """(model, max_candidates) of every Stage 2 run, in order."""
    seen: list[tuple[str, int]] = []
    real = tuning.tune

    async def counting(*args: Any, **kwargs: Any) -> Any:
        seen.append((args[2], kwargs["max_candidates"]))
        return await real(*args, **kwargs)

    monkeypatch.setattr(tuning, "tune", counting)
    return seen


async def test_forge_on_the_first_rung_only_and_the_cheapest_licensed_rung_ships(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tuned: list[tuple[str, int]]
) -> None:
    pi = install_pi(monkeypatch, ScriptedPi([{"text": "nothing better to submit"}]))
    # The first rung fails its exam; both later rungs pass, the last one for less.
    play = plays({"fake": ("nope", 0.001), "llama3.2": ("ok", 0.002),
                  "gpt-4o-mini": ("ok", 0.001)})
    compiler = ladder_compiler(
        tmp_path, play, rung_candidates=(4, 1, 1),
        forge=ForgeSettings(enabled=True, trigger="always", max_rungs=1),
    )
    stages: list[dict[str, Any]] = []
    add_stage_observer(stages.append)
    try:
        result = await compiler.compile(spec(model_target="cheapest that passes"))
    finally:
        remove_stage_observer(stages.append)
    assert tuned == [("fake", 4), ("llama3.2", 1), ("gpt-4o-mini", 1)]
    assert pi.runs == 1
    assert [d["model"] for d in stages if d["stage"] == 3] == ["fake"]
    assert [d["model"] for d in stages if d["stage"] == 4] == list(LADDER)
    artifact = result.artifact
    assert artifact is not None and artifact.licensed and artifact.model == "gpt-4o-mini"
    assert result.failures == ()


async def test_without_rung_settings_every_rung_is_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tuned: list[tuple[str, int]]
) -> None:
    pi = install_pi(monkeypatch, ScriptedPi([{"text": "nothing to add"}]))
    play = plays({model: ("ok", 0.001) for model in LADDER})
    compiler = ladder_compiler(
        tmp_path, play, forge=ForgeSettings(enabled=True, trigger="always")
    )
    result = await compiler.compile(spec(model_target="cheapest that passes"))
    assert tuned == [(model, 2) for model in LADDER]
    assert pi.runs == 3  # Forge on every rung
    assert result.artifact is not None and result.artifact.licensed
    assert result.artifact.model == "fake"  # a cost tie keeps the first rung


async def test_a_pinned_model_is_the_first_rung(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tuned: list[tuple[str, int]]
) -> None:
    pi = install_pi(monkeypatch, ScriptedPi([{"text": "nothing to add"}]))
    play = plays({model: ("ok", 0.001) for model in LADDER})
    compiler = ladder_compiler(
        tmp_path, play, rung_candidates=(4, 1, 1),
        forge=ForgeSettings(enabled=True, trigger="always", max_rungs=1),
    )
    result = await compiler.compile(spec(model_target="llama3.2"))
    assert tuned == [("llama3.2", 4)] and pi.runs == 1
    assert result.artifact is not None and result.artifact.model == "llama3.2"


def test_rung_settings_load_from_a_profile_and_are_checked() -> None:
    profile = Profile.model_validate(
        {
            "models": {model: {"provider": "fake"} for model in LADDER},
            "compile": {
                "model_ladder": list(LADDER),
                "max_candidates": 4,
                "rung_candidates": [4, 1, 1],
                "forge": {"enabled": True, "trigger": "always", "max_rungs": 1,
                          "agent_max_tokens": 12288, "max_cases_shown": 16,
                          "max_failures_shown": 8},
            },
        }
    )
    config = profile.compile
    assert [config.candidates_for(rung) for rung in range(4)] == [4, 1, 1, 4]
    assert config.forge.max_rungs == 1
    assert json.loads(config.model_dump_json())["rung_candidates"] == [4, 1, 1]
    for bad in ([0], [-1, 1], [1.5]):
        with pytest.raises(ValueError):
            CompileConfig.model_validate({"rung_candidates": bad})
    assert CompileConfig().rung_candidates == () and CompileConfig().candidates_for(0) == 16
