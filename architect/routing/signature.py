"""Task signatures: exact slot, I/O shape and advisory family (Phase 10 Section 5).

A signature is three one-way 32-hex identities plus an in-memory token set used
only as a Jaccard tiebreak inside an already schema-equal group (Section 7.1).
The tokens are never dumped, logged or hashed.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from architect.spec import Spec, schema_sha256

__all__ = ["TaskSignature", "fit", "jaccard", "norm", "schema_skeleton", "signature"]

Tier = Literal["A", "B", "C", "none"]
_HEX32 = r"^[0-9a-f]{32}$"
_SKELETON_KEYS = ("type", "properties", "items", "required")


class TaskSignature(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    slot: str = Field(pattern=_HEX32)
    shape: str = Field(pattern=_HEX32)
    family: str = Field(pattern=_HEX32)
    token_count: int = Field(ge=0)
    # Memory only: never dumped, logged or hashed.
    tokens: frozenset[str] = Field(default=frozenset(), exclude=True, repr=False)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _h(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()[:32]


def norm(text: str) -> str:
    """The ``telemetry.categorical`` normalization: NFKC, strip, casefold."""
    return unicodedata.normalize("NFKC", text).strip().casefold()


def schema_skeleton(schema: JsonValue) -> JsonValue:
    """Keep only ``type``, ``properties``, ``items`` and ``required``, recursively."""
    if isinstance(schema, list):
        return [schema_skeleton(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    kept: dict[str, JsonValue] = {}
    for key in _SKELETON_KEYS:
        if key not in schema:
            continue
        value = schema[key]
        if key == "properties" and isinstance(value, dict):
            kept[key] = {name: schema_skeleton(item) for name, item in value.items()}
        elif key == "items":
            kept[key] = schema_skeleton(value)
        else:
            kept[key] = value
    return kept


def _floor(spec: Spec) -> tuple[JsonValue, JsonValue]:
    secondary = spec.secondary_floor
    return (
        spec.floor.model_dump(mode="json"),
        None if secondary is None else secondary.model_dump(mode="json"),
    )


def signature(spec: Spec) -> TaskSignature:
    """Slot is the ``Registry._beats`` field set; model, budget, lifecycle are excluded."""
    tools = sorted(set(spec.tools))
    floor, secondary = _floor(spec)
    slot = _h(
        {
            "task": norm(spec.task),
            "input_schema": spec.input_schema,
            "output_schema": spec.output_schema,
            "domain": spec.domain,
            "tools": tools,
            "floor": floor,
            "secondary_floor": secondary,
        }
    )
    shape = _h(
        {
            "domain": spec.domain,
            "in": schema_sha256(spec.input_schema),
            "out": schema_sha256(spec.output_schema),
            "tools": tools,
            "pipeline": [step.model_dump(mode="json") for step in spec.pipeline],
        }
    )
    family = _h(
        {
            "domain": norm(spec.domain),
            "in": schema_skeleton(dict(spec.input_schema)),
            "out": schema_skeleton(dict(spec.output_schema)),
        }
    )
    tokens = frozenset(re.findall(r"\w+", norm(spec.task)))
    return TaskSignature(
        slot=slot, shape=shape, family=family, token_count=len(tokens), tokens=tokens
    )


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """``|T1 & T2| / |T1 | T2|``, and 1 when both sets are empty."""
    union = left | right
    if not union:
        return 1.0
    return len(left & right) / len(union)


def fit(
    probe: TaskSignature, probe_spec: Spec, other: TaskSignature, other_spec: Spec
) -> tuple[Tier, float]:
    """Section 7.1 tiers. The Tier B ladder condition is the router's to apply."""
    if probe.slot == other.slot:
        return "A", 1.0
    j = jaccard(probe.tokens, other.tokens)
    if probe.shape == other.shape:
        same_kind = 1.0 if probe_spec.floor.kind == other_spec.floor.kind else 0.0
        return "B", 0.5 + 0.4 * j + 0.1 * same_kind
    if probe.family == other.family:
        return "C", 0.2 + 0.2 * j
    return "none", 0.0
