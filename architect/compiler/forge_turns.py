"""Per-turn telemetry of a Forge session: one content-free row per agent model call.

A row holds numbers only: prompt, cache-hit, reasoning and completion tokens, dollars,
seconds, the provider's stop reason and whether the call completed. Rows reach
``ForgeResult.turns`` and the promoted artifact's ``forge.json``, and each one is also
appended, as its call ends, to a per-session JSON-lines file under the telemetry
directory (``forge_turns/``), so a session that never submits keeps its rows too.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from architect.models.chat import StopReason
from architect.telemetry import categorical

__all__ = ["TURNS_DIRECTORY", "ForgeTurn", "TurnLog"]

TURNS_DIRECTORY: Final = "forge_turns"


class ForgeTurn(BaseModel):
    """One Forge agent model call. ``stop`` is None for a non-streamed call; ``ok`` is
    False for a call that failed or was cancelled (its counts are then its quote, marked
    ``estimated``, unless the provider had already reported its final usage)."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    turn: int = Field(ge=1)
    prompt_tokens: int = Field(ge=0)
    cached_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0)
    seconds: float = Field(ge=0)
    stop: StopReason | None = None
    estimated: bool = False
    ok: bool = True


class TurnLog:
    """Append-only rows of one Forge session in ``<telemetry>/forge_turns/<file>.jsonl``.

    Every line carries the session id, the Forge depth, the spec hash and the agent
    model as a one-way category, then the row. Each line is written, and the file closed,
    as its call ends, so a stopped session keeps every finished turn. Logging never
    breaks a session: after a failed write the log stops, and the rows still reach
    ``ForgeResult.turns``.
    """

    def __init__(self, directory: Path | None, *, spec_hash: str, model: str, depth: int) -> None:
        self.session = uuid4().hex
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.path = (
            None
            if directory is None
            else Path(directory) / TURNS_DIRECTORY / f"{stamp}-{self.session[:12]}.jsonl"
        )
        self._head = {
            "session": self.session,
            "depth": depth,
            "spec_hash": spec_hash,
            "model": categorical(model),
        }

    def append(self, row: ForgeTurn) -> None:
        path = self.path
        if path is None:
            return
        record = {
            **self._head,
            "time": datetime.now(UTC).isoformat(timespec="seconds"),
            **row.model_dump(mode="json"),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(record, ensure_ascii=True, allow_nan=False) + "\n")
        except (OSError, ValueError):
            self.path = None
