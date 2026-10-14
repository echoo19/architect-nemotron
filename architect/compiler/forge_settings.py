"""Forge settings (Phase 9, Section 8.1); re-exported by ``architect.compiler.types``.

``enabled=False`` (the default) means no Forge code runs.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ForgeSettings", "TemplateName"]

TemplateName = Literal[
    "prompt_chain", "extract_verify", "vision_two_pass", "game_explorer", "game_hypothesis"
]


class ForgeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    enabled: bool = False
    mode: Literal["agent", "template"] = "agent"
    trigger: Literal["failed", "always"] = "failed"
    agent_model: str | None = None  # profile.models key; default: the ladder model
    template: TemplateName | None = None  # default: chosen by spec kind (8.4)
    feedback: Literal["per_case", "aggregate"] = "per_case"
    max_usd: Annotated[float, Field(gt=0, le=1000)] = 5.0
    max_s: Annotated[float, Field(gt=0, le=86400)] = 3600.0
    max_checks: Annotated[int, Field(ge=1, le=1000)] = 200
    max_dryruns: Annotated[int, Field(ge=0, le=1000)] = 100
    max_measures: Annotated[int, Field(ge=1, le=256)] = 24
    measure_cases: Annotated[int, Field(ge=1, le=32)] = 8
    max_submissions: Annotated[int, Field(ge=1, le=32)] = 6
    max_spawns: Annotated[int, Field(ge=0, le=8)] = 3
    max_depth: Annotated[int, Field(ge=0, le=2)] = 1
    spawn_share: Annotated[float, Field(gt=0, le=0.5)] = 0.25
    spawn_max_usd: Annotated[float, Field(gt=0, le=100)] = 2.0
    pi_max_turns: Annotated[int, Field(ge=1, le=1024)] = 200
    context_chars: Annotated[int, Field(ge=4096, le=1_000_000)] = 60000
    case_chars: Annotated[int, Field(ge=200, le=20000)] = 2000
    call_limit: Annotated[int, Field(ge=1, le=1024)] = 128
    calls_per_turn: Annotated[int, Field(ge=1, le=4)] = 2
    fuel: Annotated[int, Field(ge=100_000, le=100_000_000)] = 10_000_000
    max_verifications_per_spec: Annotated[int, Field(ge=1, le=100)] = 10
    # Output cap of one agent reply, under a large model's price capacity, so a runaway
    # reply costs at most this. A $0 (local) agent, or one whose capacity is already at or
    # below it, keeps its own capacity.
    agent_max_tokens: Annotated[int, Field(ge=1024, le=131072)] = 12288
    # TUNING.md shows the first cases only; forge_measure and forge_dryrun take any index.
    max_cases_shown: Annotated[int, Field(ge=1, le=256)] = 16
    # forge_measure shows output and expected excerpts for the first failed cases only.
    max_failures_shown: Annotated[int, Field(ge=0, le=32)] = 8
    # Forge runs on at most this many ladder rungs, counted from the first; None: every one.
    # A later rung compiles as it would with Forge off.
    max_rungs: Annotated[int, Field(ge=1, le=32)] | None = None

    def child_settings(self, depth: int) -> ForgeSettings:
        """Settings for a child compile at ``depth`` (Section 11.3).

        The child forges only while ``depth < max_depth`` and only on a failed
        tuning result. Money and time are filled by the caller: ``forge_spawn``
        replaces ``max_usd``/``max_s`` with the spawn allotment
        (``forge_tools.spawn_settings``) before the child compile starts.
        """
        if type(depth) is not int or depth < 0:
            raise ValueError("Invalid forge depth")
        return ForgeSettings.model_validate(
            {
                **self.model_dump(),
                "enabled": self.enabled and depth < self.max_depth,
                "trigger": "failed",
            }
        )
