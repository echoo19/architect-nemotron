"""Validated, content-bearing model I/O and the adapter boundary."""

import hashlib
import json
from typing import Annotated, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from .images import MAX_IMAGES_PER_REQUEST, ImageInput

TokenCount = Annotated[int, Field(ge=0, strict=True)]
Cost = Annotated[float, Field(ge=0, allow_inf_nan=False)]
# A provider model id or profile key: segments joined by single slashes, each a letter or
# digit followed by letters, digits and ``._:-`` (``granite4.2:8b``,
# ``nvidia/Nemotron-3_5-Lightning``). Every user also caps the length at 128 characters.
MODEL_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*(/[A-Za-z0-9][A-Za-z0-9._:-]*)*$"
# Bound on the canonical compact JSON bytes of one ``ModelRequest.response_schema``.
MAX_RESPONSE_SCHEMA_BYTES = 16 * 1024


def _canonical_schema(schema: dict[str, JsonValue]) -> bytes:
    return json.dumps(
        schema, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii")


class ModelRequest(BaseModel):
    """One model call.

    ``prompt`` is always the complete text the model is given. ``cache_prefix``
    optionally marks a leading, byte-stable part of it (fixed instructions) that
    a cache-aware adapter may send as a separately cacheable block. It is a
    marker only: it must be a prefix of ``prompt`` and is never extra text, so
    an adapter that ignores it still sends exactly what the model would see.

    ``images`` are shown alongside the prompt (after the cached prefix on
    providers with block-level caching). ``image_detail`` is a request hint for
    OpenAI-shaped providers only; others ignore it. Both default to "no images"
    so existing constructors, spec hashes and ``split()`` are unchanged.

    ``response_schema`` optionally asks the provider to constrain the reply to a
    JSON Schema (constrained decoding). It is a decoding hint, never trust: the
    caller still parses and validates every reply. Adapters with a native
    schema option send it (Ollama ``format``, OpenAI-compatible
    ``response_format``); the others ignore it. It defaults to ``None``, which
    leaves every existing request body byte-identical.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: str
    # Optional provider request option, not an agent or run token allowance.
    max_output_tokens: Annotated[int, Field(gt=0, strict=True)] | None = None
    temperature: Annotated[float, Field(ge=0, le=2, allow_inf_nan=False)] = 0.0
    cache_prefix: Annotated[str, Field(min_length=1)] | None = None
    images: Annotated[tuple[ImageInput, ...], Field(max_length=MAX_IMAGES_PER_REQUEST)] = ()
    image_detail: Literal["auto", "low", "high"] = "auto"  # OpenAI-shaped request hint only
    response_schema: dict[str, JsonValue] | None = None  # constrained-decoding hint only

    @field_validator("response_schema")
    @classmethod
    def _bounded_schema(cls, value: dict[str, JsonValue] | None) -> dict[str, JsonValue] | None:
        if value is None:
            return None
        try:
            size = len(_canonical_schema(value))
        except (TypeError, ValueError, RecursionError):
            raise ValueError("response_schema must be finite JSON") from None
        if size > MAX_RESPONSE_SCHEMA_BYTES:
            raise ValueError("response_schema is too large")
        return value

    @model_validator(mode="after")
    def _prefix_of_prompt(self) -> Self:
        if self.cache_prefix is not None and not self.prompt.startswith(self.cache_prefix):
            raise ValueError("cache_prefix must be a prefix of prompt")
        return self

    def split(self) -> tuple[str, str]:
        """Return (stable prefix, variable rest); their concatenation is ``prompt``."""
        prefix = self.cache_prefix or ""
        return prefix, self.prompt[len(prefix) :]

    def response_schema_sha256(self) -> str | None:
        """Content-free digest of the canonical ``response_schema``, if any."""
        if self.response_schema is None:
            return None
        return hashlib.sha256(_canonical_schema(self.response_schema)).hexdigest()


class ModelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    # Total prompt tokens, including the cache categories below.
    tokens_in: TokenCount
    tokens_out: TokenCount
    cost_usd: Cost
    model: Annotated[str, Field(min_length=1)]
    # Subsets of ``tokens_in`` written to / served from a provider prompt cache.
    cache_write_tokens: TokenCount = 0
    cache_read_tokens: TokenCount = 0
    # The part of ``tokens_out`` the provider reports as reasoning (0 when none is).
    reasoning_tokens: TokenCount = 0
    # The provider's own id for this completion (``chatcmpl-...``, ``msg_...``) when it
    # gives one, so a run can be found in the provider's logs; None otherwise.
    completion_id: Annotated[str, Field(min_length=1, max_length=200)] | None = None

    @model_validator(mode="after")
    def _cache_within_input(self) -> Self:
        if self.cache_write_tokens + self.cache_read_tokens > self.tokens_in:
            raise ValueError("Cached tokens cannot exceed input tokens")
        return self


class UsageQuote(BaseModel):
    """Conservative reservation, never a claim about billed usage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tokens_in: TokenCount
    tokens_out: TokenCount
    cost_usd: Cost


class ModelAdapter(Protocol):
    model: str

    def quote(self, request: ModelRequest) -> UsageQuote: ...

    async def complete(self, request: ModelRequest) -> ModelResponse: ...


class AdapterError(Exception):
    """Invalid adapter configuration, transport, or provider response.

    ``usage`` is set when the provider answered and reported its usage but the adapter
    refused the reply (a Nemotron reply cut inside its reasoning, for example). The
    provider billed that reply, so a meter can count it although no answer came back.
    Its ``text`` is empty. None means no usage is known.
    """

    def __init__(self, *args: object, usage: ModelResponse | None = None) -> None:
        super().__init__(*args)
        self.usage = usage


__all__ = [
    "AdapterError",
    "Cost",
    "ImageInput",
    "MAX_RESPONSE_SCHEMA_BYTES",
    "MODEL_ID_PATTERN",
    "ModelAdapter",
    "ModelRequest",
    "ModelResponse",
    "TokenCount",
    "UsageQuote",
]
