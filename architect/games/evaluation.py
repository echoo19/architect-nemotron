"""Trusted one-use rollout evidence and instance-disjoint game floor construction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from architect.floors import EvaluationCase, FloorError, HeldOutGameScore
from architect.games.local import ROLES, LocalFixtureManifest, Role
from architect.games.types import GameRef, GameScore, GameUnavailableError
from architect.spec import FloorDefinition

if TYPE_CHECKING:
    from architect.runtime import RunContext
    from architect.runtime.evidence import EvaluationScope


class OfficialPreviewManifest(BaseModel):
    """Host-pinned public OFFLINE games, never model-selected evaluation data.

    Official public game IDs are the dataset identity boundary. Unlike local toy
    fixtures, their engine geometry is not inspected. Operators must stage the
    exact public revisions and keep every complete game ID in one role only.
    Neither a changed revision label nor a new SDK version suffix can create
    another independent game from the same public base game ID.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    dataset_id: str = Field(min_length=1, max_length=200)
    dataset_revision: str = Field(min_length=1, max_length=200)
    score_policy: Literal["official_percent_v1"] = "official_percent_v1"
    score_metric: Literal["relative_human_action_efficiency.percent.v1"] = (
        "relative_human_action_efficiency.percent.v1"
    )
    sdk_version: Literal["0.9.9"] = "0.9.9"
    engine_version: Literal["0.9.3"] = "0.9.3"
    parent_tuning: tuple[GameRef, ...] = Field(default=(), max_length=1024)
    parent_held_out: tuple[GameRef, ...] = Field(default=(), max_length=1024)
    support_tuning: tuple[GameRef, ...] = Field(min_length=1, max_length=1024)
    support_held_out: tuple[GameRef, ...] = Field(min_length=1, max_length=1024)
    delivery: tuple[GameRef, ...] = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def partitions(self) -> Self:
        refs = tuple(ref for role in ROLES for ref in self.refs(role))
        if (
            len(refs) > 1024
            or len({ref.game_id for ref in refs}) != len(refs)
            or len({ref.game_id.split("-", 1)[0] for ref in refs}) != len(refs)
        ):
            raise ValueError("official_preview_identity_overlap")
        if len(self.support_tuning) != len(self.support_held_out) or len(self.parent_tuning) != len(
            self.parent_held_out
        ):
            raise ValueError("official_preview_partition_size")
        for ref in refs:
            # This is the same versioned public identity returned by SDK discovery.
            if (
                ref.backend != "official_offline"
                or "-" not in ref.game_id
                or ref.revision != ref.game_id.split("-", 1)[1]
                or ref.revision.lower() in {"latest", "main", "unknown", "unversioned"}
            ):
                raise ValueError("official_preview_pinned_offline_required")
        return self

    def refs(self, role: Role) -> tuple[GameRef, ...]:
        if role not in ROLES:
            raise ValueError("official_preview_role")
        values: tuple[GameRef, ...] = getattr(self, role)
        return tuple(sorted(values, key=lambda ref: (ref.game_id, ref.revision)))

    def identity(self, ref: GameRef) -> str:
        if ref not in tuple(item for role in ROLES for item in self.refs(role)):
            raise GameUnavailableError("official_preview_unknown_ref")
        base_id = ref.game_id.split("-", 1)[0]
        return hashlib.sha256(("official_public_base_game_id:" + base_id).encode()).hexdigest()

    @property
    def digest(self) -> str:
        payload = self.model_dump(mode="json")
        for role in ROLES:
            payload[role] = [ref.model_dump(mode="json") for ref in self.refs(role)]
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


GameManifest = LocalFixtureManifest | OfficialPreviewManifest


def manifest_refs(manifest: GameManifest) -> tuple[GameRef, ...]:
    return tuple(ref for role in ROLES for ref in manifest.refs(role))


def validate_discovery(manifest: GameManifest, discovered: tuple[GameRef, ...]) -> None:
    """Compare only public SDK catalog identities, never hidden engine data."""
    if not 1 <= len(discovered) <= 1024:
        raise GameUnavailableError("game_manifest_discovery_size")
    checked = tuple(GameRef.model_validate(ref.model_dump(mode="json")) for ref in discovered)
    expected_backend = (
        "official_offline" if isinstance(manifest, OfficialPreviewManifest) else "local"
    )
    if any(ref.backend != expected_backend for ref in checked) or len(
        {ref.game_id for ref in checked}
    ) != len(checked):
        raise GameUnavailableError("game_manifest_discovery_identity")
    if isinstance(manifest, OfficialPreviewManifest) and len(
        {ref.game_id.split("-", 1)[0] for ref in checked}
    ) != len(checked):
        raise GameUnavailableError("game_manifest_discovery_identity")
    if any(ref not in checked for ref in manifest_refs(manifest)):
        raise GameUnavailableError("game_manifest_discovery_mismatch")


def task_for(ref: GameRef) -> dict[str, JsonValue]:
    return {"game": ref.model_dump(mode="json")}


def task_key(task: JsonValue) -> str:
    return json.dumps(task, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class _Receipt:
    measurement: EvaluationScope
    task: str
    candidate_digest: str
    run_id: str
    session_id: str
    score: float


class ReceiptLedger:
    """A host capability. Receipt strings alone carry no reusable authority."""

    def __init__(self) -> None:
        self._receipts: dict[str, _Receipt] = {}
        self._runs: set[tuple[str, str]] = set()
        self.active = True

    def close(self) -> None:
        self.active = False
        self._receipts.clear()
        self._runs.clear()

    def issue(
        self, context: RunContext, task: JsonValue, *, session_id: str, score: GameScore
    ) -> dict[str, JsonValue]:
        from architect.runtime.evidence import current_evaluation

        context.check_game_grant()
        identity = current_evaluation()
        if (
            not self.active
            or identity is None
            or not identity.active
            or not context.is_evaluation
            or not context.candidate_digest
            or identity.candidate_digest != context.candidate_digest
            or task_key(task) != task_key(context.original_task)
            or not context.run_id
            or not session_id
            or score.normalized is None
            or score.source == "unavailable"
        ):
            raise FloorError("game_receipt_scope")
        run = (context.run_id, session_id)
        if run in self._runs:
            raise FloorError("game_receipt_duplicate")
        self._runs.add(run)
        token = uuid4().hex
        self._receipts[token] = _Receipt(
            identity,
            task_key(task),
            context.candidate_digest,
            context.run_id,
            session_id,
            score.normalized,
        )

        def discard() -> None:
            self._receipts.pop(token, None)
            self._runs.discard(run)

        identity.register_cleanup(discard)
        return {"receipt": token}

    def consume(self, task: JsonValue, output: JsonValue) -> float:
        from architect.runtime.evidence import current_evaluation

        identity = current_evaluation()
        if not isinstance(output, dict) or set(output) != {"receipt"}:
            raise FloorError("game_receipt_invalid")
        token = output["receipt"]
        if not isinstance(token, str):
            raise FloorError("game_receipt_invalid")
        receipt = self._receipts.pop(token, None)
        if (
            not self.active
            or receipt is None
            or identity is None
            or not identity.active
            or receipt.measurement is not identity
            or receipt.candidate_digest != identity.candidate_digest
            or receipt.task != task_key(task)
        ):
            raise FloorError("game_receipt_invalid")
        return receipt.score


class GameScorer:
    def __init__(self, ledger: ReceiptLedger) -> None:
        self.ledger = ledger

    async def score(self, task: JsonValue, output: JsonValue) -> float:
        return self.ledger.consume(task, output)


def manifest_records(manifest: GameManifest, *, parent: bool = False) -> tuple[EvaluationCase, ...]:
    """Interleave complete identities to match the shared even/odd split exactly."""
    tuning = manifest.refs("parent_tuning" if parent else "support_tuning")
    held_out = manifest.refs("parent_held_out" if parent else "support_held_out")
    if not tuning or len(tuning) != len(held_out):
        raise FloorError("game_partition_invalid")
    identities = [manifest.identity(ref) for ref in (*tuning, *held_out)]
    if len(set(identities)) != len(identities):
        raise FloorError("game_partition_overlap")
    records = tuple(
        EvaluationCase(case_id=f"game-{2 * index + offset:04d}", task=task_for(ref), expected=None)
        for index, pair in enumerate(zip(tuning, held_out, strict=True))
        for offset, ref in enumerate(pair)
    )
    ordered = sorted(records, key=lambda row: row.case_id)
    if [row.task for row in ordered[::2]] != [task_for(ref) for ref in tuning] or [
        row.task for row in ordered[1::2]
    ] != [task_for(ref) for ref in held_out]:
        raise FloorError("game_partition_invalid")
    return records


def game_floor(
    definition: FloorDefinition,
    manifest: GameManifest,
    ledger: ReceiptLedger,
    *,
    parent: bool = False,
) -> HeldOutGameScore:
    return HeldOutGameScore(
        definition, manifest_records(manifest, parent=parent), environment=GameScorer(ledger)
    )
