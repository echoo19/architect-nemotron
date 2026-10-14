"""Promote and rollback proposals (Phase 10 Section 8.5): text only, no automatic effect.

A proposal never installs, rolls back, recompiles or arms anything. It prints an
operator command; ``Registry.install``/``_beats`` still demands a strictly better
Stage 4 report, and rollback stays an operator-only verb.
"""

from __future__ import annotations

import math
import shlex
from collections.abc import Sequence
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from architect.routing.evidence import InstalledRef, VersionEvidence

__all__ = ["PROFILE_PLACEHOLDER", "Proposal", "promote_proposals", "rollback_proposals"]

# The router does not know the profile name; surfaces substitute it when printing.
PROFILE_PLACEHOLDER = "<profile>"


class Proposal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["promote", "rollback"]
    name: str
    revision: int | None
    command: str


class _History(Protocol):
    def earlier_revisions(self, name: str, current: int) -> tuple[VersionEvidence, ...]: ...


def _upper(evidence: VersionEvidence) -> float:
    """``m(X) + 0.5 / sqrt(max(n_X, 1))``: the incumbent's upper bound."""
    assert evidence.m is not None
    return evidence.m + 0.5 / math.sqrt(max(evidence.held_out_count, 1))


def promote_proposals(
    incumbent: VersionEvidence | None,
    rivals: Sequence[VersionEvidence],
    *,
    slot: str,
    margin: float,
) -> tuple[Proposal, ...]:
    """``route_promote_proposed``: an eligible rival of the same slot whose lower bound
    beats the incumbent's upper bound by more than ``margin``.

    ``rivals`` must already be the eligible candidates; the incumbent is the top
    eligible Tier A installed entry. A rival is a library snapshot or another name.
    """
    if incumbent is None or incumbent.m is None or not isinstance(incumbent.ref, InstalledRef):
        return ()
    name = incumbent.ref.name
    bound = _upper(incumbent)
    for rival in rivals:
        if (
            rival.m_lcb is None
            or rival.signature is None
            or rival.signature.slot != slot
            or rival.ref.artifact_digest == incumbent.ref.artifact_digest
            or (isinstance(rival.ref, InstalledRef) and rival.ref.name == name)
        ):
            continue
        if rival.m_lcb - bound > margin:
            command = f"architect recompile {shlex.quote(name)} --profile {PROFILE_PLACEHOLDER}"
            return (Proposal(kind="promote", name=name, revision=None, command=command),)
    return ()


def rollback_proposals(
    history: _History, drifting: Sequence[VersionEvidence]
) -> tuple[Proposal, ...]:
    """``route_rollback_suggested``: a drifting entry whose earlier revision has a higher
    ``m_lcb`` and is licensed on a fresh load."""
    found: list[Proposal] = []
    for current in drifting:
        if (
            current.drift != "drifting"
            or current.m_lcb is None
            or not isinstance(current.ref, InstalledRef)
        ):
            continue
        better = [
            earlier
            for earlier in history.earlier_revisions(current.ref.name, current.ref.revision)
            if earlier.m_lcb is not None and earlier.m_lcb > current.m_lcb
            and isinstance(earlier.ref, InstalledRef)
        ]
        if not better:
            continue
        best = min(
            better,
            key=lambda item: (
                -(item.m_lcb or 0.0),
                -(item.ref.revision if isinstance(item.ref, InstalledRef) else 0),
            ),
        )
        assert isinstance(best.ref, InstalledRef)
        revision = best.ref.revision
        found.append(
            Proposal(
                kind="rollback",
                name=current.ref.name,
                revision=revision,
                command=f"rollback {current.ref.name} {revision}",
            )
        )
    return tuple(found)
