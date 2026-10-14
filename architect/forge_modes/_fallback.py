"""Field-for-field mirrors of ``routing.router.InstalledRef`` / ``RouteDecision``.

Used only while the WP1 router module is absent (``_routing.py`` prefers the real
classes). They carry references, never artifacts, exactly like the originals.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class InstalledRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    name: str
    revision: int
    artifact_digest: str
    spec_hash: str


class TemplateRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    key: str
    artifact_digest: str
    model: str


class RouteDecision(BaseModel):
    """The fields Forge modes read; every other ``RouteDecision`` field is carried as is."""

    model_config = ConfigDict(frozen=True, extra="allow", from_attributes=True)

    decision_id: UUID
    origin: str
    action: Literal["reuse_installed", "warm_start", "compile", "refuse"]
    target: InstalledRef | TemplateRef | None = None
    codes: tuple[str, ...] = ()
    explored: bool = False
