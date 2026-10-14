"""Explicit provider prices and technical capacities; no unknown-model fallback.

``output_capacity_tokens`` must match the deployed model/protocol, not a product
budget. The bundled assumptions are 64,000 output tokens for Claude Sonnet 4,
16,384 for GPT-4o mini, and the full 131,072-token Llama 3.2 context as a
conservative output estimate (input shares that context). The Claude Code
models (Haiku 4.5 at 64,000; Sonnet 5, Opus 5.5 and Fable 5.1 at 128,000) and
Codex's gpt-5.6-terra (128,000) carry API list prices and a 16,384-token input
overhead for the CLI's own system prompt. For CLI subscription use these are
API-equivalent estimates, not what the subscription bills. Deployments with
other capacities, tokenizer framing, or prices must supply ``prices_path``.
These capacities reserve dollars; they do not reject measured token usage.

Optional ``cache_write_per_million`` / ``cache_read_per_million`` price prompt
cache writes and reads (Anthropic: 1.25x and 0.1x input for the 5-minute TTL,
0.025x reads on Fable 5.1; OpenAI gpt-4o-mini: 0.5x reads, no write premium).
Unset rates fall back to the input rate. Measured cache pricing is still an
estimate from the configured table. Quotes never assume a cache hit: every
input token of a request that may write the cache is reserved at the higher of
the input and cache-write rates.
The fake entry describes its two-token default response only: scripts are
quoted exactly and are never clipped by this metadata.

Image inputs
------------
``Price.image`` (an ``ImagePricing``) declares a per-image token CEILING,
``tokens_per_image_max``, and that ceiling is what ``image_reservation``
reserves for every image, unconditionally: ``quote()`` stays a true upper
bound no matter how a provider actually counts pixels. ``rule`` and the
formula knobs feed only ``image_estimate``, a display column that never
affects a reservation. A model without ``image`` refuses images in ``quote()``
before any I/O ("Model is not configured for image input"); there is no
fallback price. ``basis`` says where each ceiling came from; ``assumption``
entries are printed by ``architect bench probe`` until measured rows replace
them, and a ceiling only ever moves DOWN to ``max(measured) * 1.25`` with the
measured row cited here.

Bundled image ceilings and their bases:

- ``fake``: rule ``fixed``, 100 tokens per image, 20 MiB, 100 images; basis
  ``assumption``. At the fake's $1/M input rate an image costs $0.0001, so
  synthetic bench costs stay nonzero and dollar-bound.
- ``claude-sonnet-4-20250514``, ``claude-haiku-4-5-20251001``,
  ``claude-sonnet-5``, ``claude-opus-5-5``, ``claude-fable-5-1``: rule
  ``anthropic_area`` (tokens ~= width * height / 750), ceiling 1600, 5,000,000
  bytes, 100 images; basis ``provider_docs``. The provider downscales images
  over ~1.15 megapixels before counting, and 1.15 MP / 750 ~= 1,533 < 1,600,
  so 1600 bounds every accepted image.
- ``gpt-4o-mini``: rule ``openai_tiles`` with ``patch_multiplier`` 33.33
  (the mini model bills 85 + 170 per 512-px tile, then multiplies by 33.33),
  ceiling 48170, 20,000,000 bytes, 20 images; basis ``provider_docs``. The
  tile formula (fit in 2048 x 2048, short side to 768) allows at most 8 tiles:
  (85 + 170 * 8) * 33.33 = 48,161.85 < 48,170.
- ``gpt-5.6-terra``: rule ``fixed``, ceiling 8192, 20,000,000 bytes, 20
  images; basis ``assumption`` (no published formula for the Codex path;
  calibrated by ``bench probe``, which lowers it to max(measured) * 1.25).
- ``llama3.2``: no ``image`` (a text model); images are refused.
- ``llama3.2-vision``: $0 local model (``allowed_response_models``
  ``llama3.2-vision:latest``, capacity 131,072, overhead 1,024), rule
  ``fixed``, ceiling 1600, 20,000,000 bytes, 20 images; basis ``assumption``
  (the Llama 3.2 vision projector emits a fixed tile budget per image; 1600
  is an upper bound to be confirmed by ``bench probe``; it costs $0 either
  way).

Models the bundled table does not price (Phase 13)
--------------------------------------------------
There is still no silent fallback price: ``price_for`` raises for a model missing
from the table an adapter loads. A model reaches an adapter only through one of
these explicit routes, each an entry in a per-workspace price file
(``<workspace>/model-prices.json``, written by ``architect.providers.onboarding``
and named by the profile entry's ``prices``):

1. **Local servers** (Ollama, LM Studio, llama.cpp, vLLM): ``local_price``, $0
   per token, so budgets stay well-formed and only time binds.
2. **OpenRouter** and **Nebius Token Factory**: the per-token prices their model
   lists publish, converted to per-million rates when the model is registered
   (``per_token_price``). For a model the bundled table prices, Nebius's listed
   price yields to the bundled entry (the same list, with its curated capacity); the
   bundled Nemotron entries never price another provider's model of the same id.
3. **Declared by the user**: an ordinary priced entry passed to registration. It
   wins over a bundled entry too.
4. **Unpriced**: ``unpriced_price`` writes a $0 entry with ``unpriced: true``.
   Its quotes and measured costs are $0, so the existing quote / reserve /
   reconcile runtime runs it unchanged and the call's time budget
   (``compute_s``) is what bounds it. It is for conversation and subagents
   only: callers show "cost unknown" (``UNPRICED_LABEL``) wherever spend is
   shown, and build mode must refuse to compile on it (``model_unpriced``)
   because a dollar floor or compile budget could not be enforced.
   ``is_unpriced(model, prices_path)`` is the one test for both cases (no entry,
   or an entry flagged unpriced); ``profiles.model_unpriced`` applies it to a
   profile model.

Bundled API prices added in Phase 13 are the providers' published list prices
for standard (non-batch, non-priority) text calls; models with tiered or
uncertain pricing are deliberately left out and run unpriced until declared.
Their output capacities are the providers' published maximum output tokens.
Dated snapshot names a provider reports back are accepted by
``Price.accepts_response_model``.

Nebius Token Factory (NVIDIA Nemotron)
--------------------------------------
List prices in USD per million input / output tokens from Nebius's public model
catalog (https://tokenfactory.nebius.com/api/public/models_info, retrieved
2026-10-03; the authenticated ``GET /v1/models?verbose=true`` agreed). Model ids are
case-sensitive and the reply reports them unchanged, so each entry allows exactly its
own id:

- ``nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B``: $0.06 / $0.24
- ``nvidia/Nemotron-3_5-Lightning``: $0.06 / $0.24
- ``nvidia/nemotron-3-super-120b-a12b``: $0.30 / $0.90
- ``nvidia/Nemotron-3-Ultra-550b-a55b``: $1.00 / $3.00

Nebius publishes no output limit, so ``output_capacity_tokens`` (16,384; 32,768 for
Ultra) is Architect's own cap, which the Nebius dialect always sends as ``max_tokens``
(``models/http.py``). Reasoning tokens are billed inside ``completion_tokens`` and
count against that cap. ``input_overhead_tokens`` is 1,024 for the chat template. The
entries are text only (no ``image``), so images are refused before any I/O.
"""

import json
import math
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .images import ImageInput
from .types import AdapterError, Cost, TokenCount

DEFAULT_PRICES = Path(__file__).with_name("prices.json")
# What the CLI shows instead of a dollar figure for an unpriced model.
UNPRICED_LABEL = "cost unknown"
# Output reservation for models whose capacity is not known (unpriced and local
# entries without a published limit): small enough for every current provider's
# ``max_tokens``, large enough for a conductor turn.
DEFAULT_OUTPUT_CAPACITY = 8192


class ImagePricing(BaseModel):
    """Per-image reservation ceiling plus display-only formula knobs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tokens_per_image_max: Annotated[int, Field(gt=0, strict=True)]  # THE reservation, per image
    rule: Literal["fixed", "anthropic_area", "openai_tiles", "openai_patches"] = "fixed"
    area_divisor: int = 750
    tile_base: int = 85
    tile_tokens: int = 170
    patch_multiplier: float = 1.0
    detail: Literal["auto", "low", "high"] = "auto"  # copied into ModelRequest.image_detail
    max_bytes: Annotated[int, Field(gt=0, strict=True)] = 20 * 1024 * 1024
    max_images: Annotated[int, Field(gt=0, strict=True)] = 20
    basis: Literal["provider_docs", "assumption", "measured"]


class Price(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_per_million: Cost
    output_per_million: Cost
    output_capacity_tokens: Annotated[int, Field(gt=0, strict=True)]
    # Bound for provider-added message framing/template tokens. Configure this
    # for the deployed tokenizer/template; prompt UTF-8 bytes bound text tokens.
    input_overhead_tokens: TokenCount
    # Explicitly approved resolved names billed at this same configured rate.
    allowed_response_models: tuple[Annotated[str, Field(min_length=1)], ...] = ()
    cache_write_per_million: Cost | None = None
    cache_read_per_million: Cost | None = None
    image: ImagePricing | None = None
    # A $0 placeholder for a model nobody has priced: usable for conversation and
    # subagents under a time budget, never for a compile (see the module docstring).
    unpriced: bool = False

    @model_validator(mode="after")
    def _unpriced_is_zero(self) -> Self:
        if self.unpriced and (
            self.input_per_million
            or self.output_per_million
            or self.cache_write_per_million
            or self.cache_read_per_million
        ):
            raise ValueError("An unpriced entry carries no rates")
        return self

    def accepts_response_model(self, reported: str, requested: str) -> bool:
        """Whether a provider's reported model name is billed at this entry's rate.

        The requested name, an explicitly allowed name, or a dated snapshot of the
        requested name (``<requested>-YYYY-MM-DD``), which OpenAI-style providers
        report for an alias. Adapters may use this in place of the exact check.
        """
        if reported == requested or reported in self.allowed_response_models:
            return True
        if self.input_per_million == 0 and self.output_per_million == 0:
            # A $0 entry (local servers, unpriced models): the name cannot change the bill,
            # and local servers report tags or file names (llama3.2:latest, model.gguf).
            return True
        if not reported.startswith(requested + "-"):
            return False
        date = reported[len(requested) + 1 :]
        parts = date.split("-")
        return (
            len(date) == 10
            and [len(part) for part in parts] == [4, 2, 2]
            and all(part.isdigit() for part in parts)
        )

    def cost(
        self,
        tokens_in: int,
        tokens_out: int,
        *,
        cache_write_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> float:
        """Price measured usage; cache counts are subsets of ``tokens_in``."""
        if min(cache_write_tokens, cache_read_tokens) < 0 or (
            cache_write_tokens + cache_read_tokens > tokens_in
        ):
            raise AdapterError("Invalid cached token usage")
        uncached = tokens_in - cache_write_tokens - cache_read_tokens
        write = self.input_per_million if self.cache_write_per_million is None else (
            self.cache_write_per_million
        )
        read = self.input_per_million if self.cache_read_per_million is None else (
            self.cache_read_per_million
        )
        return (
            uncached * self.input_per_million
            + cache_write_tokens * write
            + cache_read_tokens * read
            + tokens_out * self.output_per_million
        ) / 1_000_000

    def quote_cost(self, tokens_in: int, tokens_out: int, *, may_write_cache: bool) -> float:
        """Conservative reservation: no cache hit, and every input token may be written."""
        if not may_write_cache:
            return self.cost(tokens_in, tokens_out)
        rate = max(self.input_per_million, self.cache_write_per_million or 0.0)
        return (tokens_in * rate + tokens_out * self.output_per_million) / 1_000_000


def image_reservation(price: Price, images: Sequence[ImageInput]) -> int:
    """Reserved input tokens for these images: ``len(images) * tokens_per_image_max``.

    Raises ``AdapterError("Model is not configured for image input")`` when
    ``price.image`` is None and ``images`` is non-empty;
    ``AdapterError("Too many images for model")`` over ``max_images``;
    ``AdapterError("Image exceeds model byte limit")`` when any image exceeds
    ``max_bytes``. Never consults a provider formula.
    """
    if not images:
        return 0
    pricing = price.image
    if pricing is None:
        raise AdapterError("Model is not configured for image input")
    if len(images) > pricing.max_images:
        raise AdapterError("Too many images for model")
    if any(len(image.data) > pricing.max_bytes for image in images):
        raise AdapterError("Image exceeds model byte limit")
    return len(images) * pricing.tokens_per_image_max


def _openai_tiles(width: int, height: int) -> int:
    # Fit within 2048 x 2048, then scale the short side to 768, then count 512-px tiles.
    longest = max(width, height)
    if longest > 2048:
        scale = 2048 / longest
        width, height = max(1, int(width * scale)), max(1, int(height * scale))
    shortest = min(width, height)
    if shortest > 768:
        scale = 768 / shortest
        width, height = max(1, int(width * scale)), max(1, int(height * scale))
    return math.ceil(width / 512) * math.ceil(height / 512)


def image_estimate(price: Price, image: ImageInput, detail: str = "auto") -> int:
    """Provider-formula ESTIMATE for display only; never used for reservation.

    ``anthropic_area``: ceil(w*h/area_divisor); ``openai_tiles``: tile_base +
    tile_tokens*tiles(w,h) (fit within 2048, short side to 768, 512 tiles)
    scaled by patch_multiplier, or tile_base alone at ``detail="low"``;
    ``openai_patches``: min(ceil(w/32)*ceil(h/32), 1536)*patch_multiplier;
    ``fixed``: tokens_per_image_max.
    """
    pricing = price.image
    if pricing is None:
        raise AdapterError("Model is not configured for image input")
    if pricing.rule == "anthropic_area":
        return math.ceil(image.pixels / pricing.area_divisor)
    if pricing.rule == "openai_tiles":
        tokens = pricing.tile_base
        if detail != "low":
            tokens += pricing.tile_tokens * _openai_tiles(image.width, image.height)
        return round(tokens * pricing.patch_multiplier)
    if pricing.rule == "openai_patches":
        patches = min(math.ceil(image.width / 32) * math.ceil(image.height / 32), 1536)
        return round(patches * pricing.patch_multiplier)
    return pricing.tokens_per_image_max


class FakeUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tokens_in: TokenCount
    tokens_out: TokenCount


class Prices(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    models: Annotated[dict[str, Price], Field(min_length=1)]
    fake_usage: FakeUsage


def load_prices(path: Path | None = None) -> Prices:
    try:
        return Prices.model_validate_json((path or DEFAULT_PRICES).read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError) as exc:
        raise AdapterError("Invalid or unreadable price configuration") from exc


def price_for(prices: Prices, model: str) -> Price:
    try:
        return prices.models[model]
    except KeyError as exc:
        raise AdapterError("No configured price for model") from exc


def is_unpriced(model: str, prices_path: Path | None = None) -> bool:
    """True when ``model`` has no entry in the table, or an entry flagged ``unpriced``.

    ``prices_path`` is the table the model's adapter loads (None: the bundled one).
    An unreadable table counts as unpriced: no dollar budget could be enforced.
    """
    try:
        return price_for(load_prices(prices_path), model).unpriced
    except AdapterError:
        return True


def unpriced_price(
    *, output_capacity_tokens: int | None = None, allowed_response_models: Sequence[str] = ()
) -> Price:
    """The $0 placeholder entry for a model with no known price (time budget only)."""
    return Price(
        input_per_million=0.0,
        output_per_million=0.0,
        output_capacity_tokens=output_capacity_tokens or DEFAULT_OUTPUT_CAPACITY,
        input_overhead_tokens=1024,
        allowed_response_models=tuple(allowed_response_models),
        unpriced=True,
    )


def local_price(
    *, output_capacity_tokens: int | None = None, allowed_response_models: Sequence[str] = ()
) -> Price:
    """A local server's model: $0 per token, the same shape as ``examples/local``."""
    return Price(
        input_per_million=0.0,
        output_per_million=0.0,
        output_capacity_tokens=output_capacity_tokens or DEFAULT_OUTPUT_CAPACITY,
        input_overhead_tokens=1024,
        allowed_response_models=tuple(allowed_response_models),
    )


def per_token_price(
    prompt_usd_per_token: float,
    completion_usd_per_token: float,
    *,
    output_capacity_tokens: int | None = None,
    allowed_response_models: Sequence[str] = (),
) -> Price | None:
    """A per-million entry from per-token list prices (OpenRouter's and Nebius's shape).

    None when either rate is negative or not finite (a router with variable
    pricing): such a model stays unpriced rather than guessed.
    """
    rates = (prompt_usd_per_token, completion_usd_per_token)
    if not all(math.isfinite(rate) and rate >= 0 for rate in rates):
        return None
    return Price(
        input_per_million=round(prompt_usd_per_token * 1_000_000, 6),
        output_per_million=round(completion_usd_per_token * 1_000_000, 6),
        output_capacity_tokens=output_capacity_tokens or DEFAULT_OUTPUT_CAPACITY,
        input_overhead_tokens=1024,
        allowed_response_models=tuple(allowed_response_models),
    )


def write_price_entries(path: Path, entries: Mapping[str, Price]) -> Path:
    """Merge ``entries`` into the price file at ``path`` (created when missing), atomically.

    Entries for other models are kept; an entry for the same model is replaced.
    Raises ``AdapterError`` when the existing file is not a valid table or the
    write fails. The bundled table is never a target.
    """
    if path.resolve() == DEFAULT_PRICES.resolve():
        raise AdapterError("The bundled price table is read-only")
    if path.exists():
        existing = load_prices(path)
        current, usage = dict(existing.models), existing.fake_usage
    else:
        current, usage = {}, FakeUsage(tokens_in=8, tokens_out=2)
    current.update(entries)
    table = Prices(models=current, fake_usage=usage)
    payload = {
        "models": {
            name: price.model_dump(mode="json", exclude_defaults=True)
            for name, price in table.models.items()
        },
        "fake_usage": usage.model_dump(mode="json"),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        raise AdapterError("Could not write the price table") from exc
    return path
