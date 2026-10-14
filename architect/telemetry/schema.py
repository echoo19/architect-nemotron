"""Closed, content-free telemetry records.

Categories are labels from a deliberately small allowlist or one-way identifiers.
Never use a prompt, response, error message, or task body as a category source.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictBool, field_validator

from architect.spec import FloorKind, Lifecycle

_SAFE_CATEGORIES = frozenset(
    {
        "general",
        "unknown",
        "fake",
        "toy",
        "user",
        "inferred",
        "registry",
        "custom",
        "default",
        "evaluation",
        "runtime",
        "none",
    }
)
_CATEGORY_HASH = re.compile(r"h_[0-9a-f]{32}\Z")


def categorical(value: str) -> str:
    """Return a safe label or deterministic normalized SHA256 category identifier.

    Normalization uses Unicode NFKC, surrounding whitespace removal, and casefold.
    Already normalized identifiers are idempotent. Hashing is not anonymization
    against dictionary attacks; callers must not supply content or secret values.
    """
    if not isinstance(value, str):
        raise TypeError("category source must be a string")
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    if normalized in _SAFE_CATEGORIES or _CATEGORY_HASH.fullmatch(normalized):
        return normalized
    return "h_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def _safe_category(value: str) -> str:
    if value not in _SAFE_CATEGORIES and not _CATEGORY_HASH.fullmatch(value):
        raise ValueError("category must be a closed label or hashed identifier")
    return value


Category = Annotated[str, Field(strict=True, max_length=64), AfterValidator(_safe_category)]
ParameterKey = Literal[
    "temperature", "max_output_tokens", "context_window", "retries", "verifier_depth", "terseness"
]
ParameterValue = (
    Annotated[float, Field(strict=True, allow_inf_nan=False)]
    | Annotated[int, Field(strict=True)]
    | StrictBool
)
NonnegativeInt = Annotated[int, Field(strict=True, ge=0)]
NonnegativeFloat = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
Score = Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)]


class TelemetryRow(BaseModel):
    """Only bounded categorical and numerical metadata may cross this boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    run_id: Annotated[str, Field(strict=True)]
    timestamp: datetime
    domain: Category
    task_type: Category
    spec_hash: Annotated[str, Field(strict=True, pattern=r"^[0-9a-fA-F]{64}$")]
    harness_version: Annotated[int, Field(strict=True, ge=1)]
    lifecycle: Lifecycle
    preset_origin: Category
    component_set: tuple[Category, ...]
    model: Category
    model_params: dict[ParameterKey, ParameterValue]
    tokens_in: NonnegativeInt
    tokens_out: NonnegativeInt
    cost_usd: NonnegativeFloat
    wall_time_s: NonnegativeFloat
    # Content-free counters; absent in rows written before they existed.
    model_calls: NonnegativeInt = 0
    cached_tokens_in: NonnegativeInt = 0
    # Images passed to adapters in this run's lineage (a count; never content).
    image_inputs: NonnegativeInt = 0
    floor_type: FloorKind
    floor_role: Literal["primary", "secondary"] = "primary"
    floor_score: Score | None
    floor_limit: Score
    accepted: StrictBool
    drift_flag: StrictBool = False

    @field_validator("run_id")
    @classmethod
    def canonical_uuid(cls, value: str) -> str:
        try:
            parsed = UUID(value)
        except ValueError as exc:
            raise ValueError("run_id must be a UUID") from exc
        if str(parsed) != value.lower():
            raise ValueError("run_id must have canonical UUID formatting")
        return str(parsed)

    @field_validator("spec_hash")
    @classmethod
    def normalized_spec_hash(cls, value: str) -> str:
        return value.lower()

    @field_validator("model_params", mode="before")
    @classmethod
    def strict_parameters(cls, value: object) -> object:
        # Validate before Pydantic's union can coerce a string, object, or nested value.
        if not isinstance(value, dict):
            raise ValueError("model_params must be a numerical parameter mapping")
        for item in value.values():
            if type(item) not in (int, float, bool):
                raise ValueError("model parameter values must be numbers or booleans")
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("model parameter values must be finite")
        return value
