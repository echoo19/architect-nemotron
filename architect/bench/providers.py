"""Bench adapter factories, evidence labels, metering and the one-call probe.

The fake provider answers through in-process oracles (``oracle.py``); every
other provider is built from the operator's trusted profile exactly as
``architect.profiles`` builds it, so a bench row measures the adapter the
operator configured. Local Ollama models are first-class: their rows carry the
``local_zero_cost`` evidence label, cost $0 and are measured in wall time.

Metering wraps an adapter's ``complete`` on the instance (the adapter object and
its type are unchanged, so the game factory's offline-binding check still sees
an ``OllamaAdapter`` or ``FakeAdapter``) and records numbers only: calls,
tokens, cached tokens, cost, wall time, images sent and the display-only image
estimate. Nothing here reads or keeps a prompt or a response text.
"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from architect.games.factory import validate_offline_adapter
from architect.games.types import GameProtocolError
from architect.models import AdapterError, FakeAdapter, ModelAdapter, ModelRequest, ModelResponse
from architect.models.images import ImageInput
from architect.models.pricing import (
    Price,
    Prices,
    image_estimate,
    image_reservation,
    load_prices,
    price_for,
)
from architect.models.types import MODEL_ID_PATTERN, UsageQuote
from architect.profiles import AdapterConfig, Profile, ProfileError, _adapter
from architect.routing.labels import PROVIDER_EVIDENCE
from architect.runtime import BudgetExceeded, RunContext, Runtime
from architect.spec import Budget, FloorDefinition, FloorKind, Lifecycle, Spec
from architect.telemetry import TelemetryStore

from .oracle import OracleState, oracle_factory
from .rows import BenchError, Code, Evidence, Provider, Variant

if TYPE_CHECKING:
    from .runner import BenchConfig

__all__ = [
    "CLOUD_PROVIDERS",
    "NOISY_ERROR_RATE",
    "PROBE_CODES",
    "CallMeter",
    "ProbeRow",
    "PublicFramePlayer",
    "adapter_price",
    "build_factory",
    "check_models",
    "evidence_for",
    "evidence_label",
    "failed_call_usage",
    "instrument",
    "probe_models",
    "provider_for",
    "require_local",
    "resolve_models",
]

NOISY_ERROR_RATE = 0.15
# Providers whose model runs off this host: game runs label them online_preview.
CLOUD_PROVIDERS: frozenset[str] = frozenset({"anthropic", "openai", "claude-code", "codex"})
# The provider-to-class map lives in ``architect.routing.labels`` (Phase 10 Section 14);
# ``_EVIDENCE`` is an alias of it, never a copy.
_EVIDENCE: dict[str, Evidence] = PROVIDER_EVIDENCE
_ADAPTER_CODES = {
    "Provider CLI is unavailable": "provider_unavailable",
    "Provider CLI timed out": "provider_timeout",
    "Prompt exceeds the configured context": "provider_context_exceeded",
    "Model is not configured for image input": "image_not_configured",
}
# The closed set of ``ProbeRow.code`` values (Section 10.5: the two image codes and
# ``provider_*``). ``provider_reservation_exceeded`` is the text call's measured
# usage above its reservation (the text ceiling, not the image one, is too low).
PROBE_CODES: frozenset[str] = frozenset(
    {
        "image_reservation_under_configured",
        "image_not_configured",
        "provider_reservation_exceeded",
        "provider_unavailable",
        "provider_timeout",
        "provider_context_exceeded",
        "provider_failed",
    }
)
# ``BenchRow.model``'s pattern: a model a row cannot record is refused before spend.
_MODEL_ID = re.compile(r"(?=.{1,128}\Z)" + MODEL_ID_PATTERN)  # ``ModelId``: 1 to 128 chars
PROBE_TEXT = "Reply with the single word ok."
PROBE_IMAGE = "What is the page number printed on this page? Reply with the number only."


def evidence_label(provider: Provider) -> Evidence:
    """fake -> synthetic_fake; ollama -> local_zero_cost; HTTP APIs -> live_billed;
    CLI subscriptions -> live_api_equivalent."""
    return _EVIDENCE[provider]


def evidence_for(provider: str) -> Evidence | None:
    """``evidence_label`` for a provider name that may be unknown (a probe row's)."""
    return _EVIDENCE.get(provider)


def adapter_price(adapter: ModelAdapter, prices: Prices | None = None) -> Price | None:
    """The price an adapter quotes with (its own table), else ``prices``, else None."""
    price = getattr(adapter, "_price", None)
    if isinstance(price, Price):
        return price
    if prices is not None:
        try:
            return price_for(prices, adapter.model)
        except AdapterError:
            return None
    return None


def provider_for(config: BenchConfig, profile: Profile | None, model: str) -> Provider:
    if config.provider == "fake":
        return "fake"
    if profile is None or model not in profile.models:
        raise BenchError("bench_model_unconfigured")
    return profile.models[model].provider


def resolve_models(config: BenchConfig, profile: Profile | None) -> tuple[str, ...]:
    """The models a run covers; every name must be configured for its provider."""
    if config.provider == "fake":
        known = load_prices().models
        models = config.models or ("fake",)
        if any(model not in known for model in models):
            raise BenchError("bench_model_unconfigured")
        return tuple(dict.fromkeys(models))
    if profile is None:
        raise BenchError("bench_profile_required")
    models = config.models or tuple(profile.models)
    if not models:
        raise BenchError("bench_no_models")
    if any(model not in profile.models for model in models):
        raise BenchError("bench_model_unconfigured")
    resolved = tuple(dict.fromkeys(models))
    check_models(config, profile, resolved)
    return resolved


def check_models(config: BenchConfig, profile: Profile | None, models: Sequence[str]) -> None:
    """Refuse, before any spend, a model whose rows could not be recorded honestly.

    A name ``BenchRow.model`` cannot hold (``/x``, ``a//b``, over 128 characters) is
    ``bench_model_unconfigured``: its cell would spend and then fail to write a
    row. An Ollama model must be local and free (``require_local``), so the
    ``local_zero_cost`` label on its rows stays true.
    """
    routed = tuple(dict.fromkeys(config.route.values()))
    if any(_MODEL_ID.fullmatch(model) is None for model in (*models, *routed)):
        raise BenchError("bench_model_unconfigured")
    if config.provider == "fake" or profile is None:
        return
    if any(model not in profile.models for model in routed):
        raise BenchError("bench_model_unconfigured")
    local_cell = False
    for model in models:
        settings = profile.models.get(model)
        if settings is not None and settings.provider == "ollama":
            require_local(profile, model, settings)
            local_cell = True
    for model in routed:
        settings = profile.models[model]
        if settings.provider == "ollama":
            require_local(profile, model, settings)
        elif local_cell:
            # A row's evidence comes from its cell model; a local cell whose pipeline
            # step ran on a hosted model would be labelled local_zero_cost falsely.
            raise BenchError("bench_model_not_local")


def _free(price: Price) -> bool:
    return (
        price.input_per_million == 0
        and price.output_per_million == 0
        and not price.cache_write_per_million
        and not price.cache_read_per_million
    )


def require_local(profile: Profile, model: str, settings: AdapterConfig) -> None:
    """An Ollama bench model runs on this host at $0, or it is ``bench_model_not_local``.

    ``OllamaAdapter`` accepts any base URL (Ollama's cloud included) and a profile
    may name any price table for it; a row labelled ``local_zero_cost`` must be
    both. The host rule is the game factory's own offline check (a loopback
    ``base_url``, ``OLLAMA_HOST`` included); the price must be $0 in every field.
    Building the adapter makes no network call.
    """
    if model.endswith("-cloud") or ":cloud" in model:
        # Ollama serves its hosted "cloud" models through the local daemon.
        raise BenchError("bench_model_not_local")
    try:
        adapter = _adapter(profile, model, settings)
    except ProfileError:
        raise BenchError("bench_provider_failed") from None
    if getattr(adapter, "_api_key", ""):
        # A keyed endpoint on loopback is a proxy to a hosted service, not this host.
        raise BenchError("bench_model_not_local")
    try:
        validate_offline_adapter(adapter, model)
    except GameProtocolError:
        raise BenchError("bench_model_not_local") from None
    price = adapter_price(adapter)
    if price is None or not _free(price):
        raise BenchError("bench_model_not_local")


def build_factory(
    config: BenchConfig,
    profile: Profile | None,
    state: OracleState | None,
    *,
    variant: Variant = "default",
) -> Callable[[str], ModelAdapter]:
    """Oracle adapters for the fake provider; the profile's adapters otherwise.

    ``noisy`` flips the oracle's fixed per-partition quotas (``oracle.py``); it is
    a fake-only mechanism check. Profile adapters are built lazily per call, as
    ``build_services`` does, and a missing credential is ``bench_provider_failed``.
    """
    if config.provider == "fake":
        rate = NOISY_ERROR_RATE if variant == "noisy" else 0.0
        return oracle_factory(state or OracleState(), error_rate=rate, seed=config.seed)
    if profile is None:
        raise BenchError("bench_profile_required")
    if variant == "noisy":
        raise BenchError("bench_variant_unknown")
    owner = profile

    def factory(model: str) -> ModelAdapter:
        settings = owner.models.get(model)
        if settings is None:
            raise BenchError("bench_model_unconfigured")
        try:
            return _adapter(owner, model, settings)
        except ProfileError:
            raise BenchError("bench_provider_failed") from None

    return factory


# ------------------------------------------------------------------------ metering


@dataclass
class CallMeter:
    """Running totals of completed calls; numbers only."""

    calls: int = 0
    failed: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cached_tokens_in: int = 0
    cost_usd: float = 0.0
    wall_s: float = 0.0
    image_inputs: int = 0
    image_tokens_estimate: int = 0
    listener: Callable[[], None] | None = None

    def notify(self) -> None:
        listener = self.listener
        if listener is not None:
            try:
                listener()
            except Exception:
                pass  # An observer never breaks a bench cell.


_MARK = "_architect_bench_meter"


def failed_call_usage(
    adapter: ModelAdapter, request: ModelRequest, error: BaseException
) -> tuple[int, int, float] | None:
    """``(tokens_in, tokens_out, cost_usd)`` that a call which raised still costs.

    A reply the provider billed and the adapter refused (``AdapterError.usage``, a
    reply cut inside its reasoning above all) counts at the usage the provider
    reported, tokens and dollars. A call cancelled in flight, as at a run's deadline,
    has no usage to read and may have been billed in full, so it counts the dollars of
    its reservation (the adapter's quote), as ``RunContext`` keeps them, and no tokens,
    since none were reported. Any other failure counts nothing.
    """
    if isinstance(error, AdapterError) and error.usage is not None:
        return error.usage.tokens_in, error.usage.tokens_out, error.usage.cost_usd
    if isinstance(error, asyncio.CancelledError):
        try:
            quote = adapter.quote(request)
        except Exception:
            return None
        return 0, 0, quote.cost_usd
    return None


def instrument(
    adapter: ModelAdapter,
    meter: CallMeter,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> ModelAdapter:
    """Meter ``adapter.complete`` in place; idempotent per (adapter, meter).

    A call that raises still counts its dollars when it was billed or may have been
    (``failed_call_usage``), so a run's cost includes replies it never got to use.
    """
    if getattr(adapter, _MARK, None) is meter:
        return adapter
    inner: Callable[[ModelRequest], Awaitable[ModelResponse]] = adapter.complete
    price = adapter_price(adapter)

    async def complete(request: ModelRequest) -> ModelResponse:
        started = clock()
        try:
            response = await inner(request)
        except BaseException as error:
            meter.calls += 1
            meter.failed += 1
            usage = failed_call_usage(adapter, request, error)
            if usage is not None:
                meter.tokens_in += usage[0]
                meter.tokens_out += usage[1]
                meter.cost_usd += usage[2]
            meter.wall_s += max(0.0, clock() - started)
            meter.notify()
            raise
        meter.calls += 1
        meter.tokens_in += response.tokens_in
        meter.tokens_out += response.tokens_out
        meter.cached_tokens_in += response.cache_read_tokens
        meter.cost_usd += response.cost_usd
        meter.wall_s += max(0.0, clock() - started)
        meter.image_inputs += len(request.images)
        if price is not None and price.image is not None:
            meter.image_tokens_estimate += sum(
                image_estimate(price, image, request.image_detail) for image in request.images
            )
        meter.notify()
        return response

    setattr(adapter, "complete", complete)  # noqa: B010 - an instance attribute, by design
    setattr(adapter, _MARK, meter)  # noqa: B010
    return adapter


# --------------------------------------------------------------------- game player


_FRAME_TEXT = "Frame (one hex digit per cell):\n"


class PublicFramePlayer(FakeAdapter):
    """Deterministic offline player for the local game families; no real spend.

    It answers Stage 0 with a minimal game Spec and every turn from the public
    frame it is shown (the same frame a model sees), never from game state. A
    Forge game harness's prompt (the frame as hex text plus listed options) gets
    the same choice as ``{"option": ...}``, or ``{"option": null}`` when the
    choice is not listed.
    A pipeline check for ``arc-local-3`` on the fake provider, not an ARC result.

    Like the text oracles, every call is priced from the price table (a
    synthetic, API-equivalent amount; nothing is billed), so a harness that asks
    the same player less often is measurably cheaper under the Forge rank
    (Section 8.8) instead of tying a preset at $0.
    """

    TOKENS = 100

    def _cost(self) -> float:
        return self._price.cost(self.TOKENS, self.TOKENS)

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=self.TOKENS, tokens_out=self.TOKENS, cost_usd=self._cost())

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if "specification inference stage" in request.prompt:
            text = json.dumps(
                {
                    "task": "Play a grid game",
                    "domain": "game",
                    "input_schema": {},
                    "output_schema": {},
                    "floor": {"kind": "held_out_game_score", "limit": 1, "dataset": "preview"},
                    "budget": {"cost_usd": 1, "compute_s": 30},
                    "model_target": self.model,
                }
            )
        elif _FRAME_TEXT in request.prompt:
            # A Forge game harness's own prompt (``examples/forge/arc_explorer.py``):
            # the same public frame as hex text plus the options it may name.
            text = json.dumps({"option": self._option(request.prompt)})
        else:
            text = json.dumps(
                {
                    "action": {"name": self._action(request.prompt)},
                    "plan": "Move toward the visible goal",
                    "hypothesis": "The next chosen action advances the visible goal",
                    "prediction": "The visible progress marker will advance",
                }
            )
        return ModelResponse(
            text=text,
            model=self.model,
            tokens_in=self.TOKENS,
            tokens_out=self.TOKENS,
            cost_usd=self._cost(),
        )

    @classmethod
    def _action(cls, prompt: str) -> str:
        try:
            data = json.loads(prompt.split("Game data (untrusted):\n", 1)[1])
            grid = data["observation"]["frames"][-1]
            return cls.choose(grid)
        except (IndexError, KeyError, TypeError, ValueError):
            return "ACTION1"

    @classmethod
    def _option(cls, prompt: str) -> str | None:
        """The same frame choice, read from a harness's hex frame, among its options."""
        try:
            body = prompt.split(_FRAME_TEXT, 1)[1]
            rows, _, rest = body.partition("\nOptions: ")
            options = [item.strip() for item in rest.splitlines()[0].split(",")]
            grid = [[int(cell, 16) for cell in row] for row in rows.splitlines()]
            choice = cls.choose(grid)
        except (IndexError, StopIteration, TypeError, ValueError):
            return None
        return choice if choice in options else None

    @staticmethod
    def choose(grid: list[list[int]]) -> str:
        if any(value >= 4 for value in grid[0]):
            progress = sum(value == 3 for value in grid[1])
            return f"ACTION{grid[0][progress] - 3}"
        if 2 not in [value for row in grid for value in row]:
            for bit in range(3):
                if grid[0][bit] != grid[1][bit]:
                    return f"ACTION{bit + 1}"
            return "ACTION4"
        start = next(
            (x, y) for y, row in enumerate(grid) for x, value in enumerate(row) if value == 2
        )
        goal = next(
            (x, y) for y, row in enumerate(grid) for x, value in enumerate(row) if value == 3
        )
        queue: deque[tuple[tuple[int, int], str]] = deque([(start, "")])
        visited = {start}
        moves = ((0, -1, "ACTION1"), (0, 1, "ACTION2"), (-1, 0, "ACTION3"), (1, 0, "ACTION4"))
        while queue:
            (x, y), first = queue.popleft()
            if (x, y) == goal:
                return first or "ACTION1"
            for dx, dy, action in moves:
                xx, yy = x + dx, y + dy
                if (
                    0 <= yy < len(grid)
                    and 0 <= xx < len(grid[0])
                    and grid[yy][xx] != 1
                    and (xx, yy) not in visited
                ):
                    visited.add((xx, yy))
                    queue.append(((xx, yy), first or action))
        return "ACTION1"


# ---------------------------------------------------------------------------- probe


class ProbeRow(BaseModel):
    """One model's text and image calibration call; counts and codes only."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    model: str
    provider: str
    image_configured: bool
    basis: str | None
    text_reserved_tokens: int
    text_measured_tokens: int | None
    image_reserved_tokens: int | None
    image_estimate_tokens: int | None
    image_measured_tokens: int | None
    reserved_usd: Annotated[float, Field(ge=0)]
    measured_usd: Annotated[float, Field(ge=0)] | None
    wall_s: Annotated[float, Field(ge=0)] = 0.0
    code: Code | None


def _probe_spec(model: str, cost_usd: float) -> Spec:
    return Spec(
        task="Probe one model call for bench calibration",
        input_schema={"type": "object"},
        output_schema={"type": "string"},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0, dataset="bench-probe"),
        budget=Budget(cost_usd=max(cost_usd, 1e-6), compute_s=900.0),
        model_target=model,
        lifecycle=Lifecycle.EXPIRE,
    )


def _code(exc: BaseException) -> str:
    """A ``PROBE_CODES`` member; a factory's bench refusal is ``provider_failed``."""
    if isinstance(exc, AdapterError):
        return _ADAPTER_CODES.get(str(exc), "provider_failed")
    return "provider_failed"


async def _probe_call(
    runtime: Runtime,
    adapter: ModelAdapter,
    meter: CallMeter,
    prompt: str,
    images: Sequence[ImageInput],
) -> tuple[int, int | None, float | None, str | None, float]:
    """(reserved tokens, measured tokens, measured usd, code, wall_s) for one call."""
    request = ModelRequest(prompt=prompt, max_output_tokens=16, images=tuple(images))
    quote = adapter.quote(request)
    spec = _probe_spec(adapter.model, quote.cost_usd)
    before = (meter.calls, meter.tokens_in, meter.cost_usd, meter.wall_s)

    async def harness(context: RunContext, task: JsonValue) -> JsonValue:
        context.attach_images(list(images))
        return await context.call(prompt, max_output_tokens=16, images=list(images))

    code: str | None = None
    try:
        await runtime.evaluate(spec, harness, {}, adapter, preset="probe")
    except BudgetExceeded:
        code = "reservation_under_configured"
    except Exception as exc:
        code = _code(exc)
    completed = meter.calls > before[0] and meter.failed == 0
    measured_tokens = meter.tokens_in - before[1] if completed else None
    measured_usd = meter.cost_usd - before[2] if completed else None
    return quote.tokens_in, measured_tokens, measured_usd, code, meter.wall_s - before[3]


async def probe_models(
    factory: Callable[[str], ModelAdapter],
    prices: Prices,
    models: Sequence[str],
    *,
    page: ImageInput,
    providers: dict[str, str] | None = None,
) -> list[ProbeRow]:
    """One text call and, for models with an image price, one image call each.

    Calls run through ``Runtime.evaluate`` with a control Spec sized to the quote,
    so every call is reserved and wallet-checked; measured usage above the
    reservation is the runtime's ``BudgetExceeded`` and is reported as
    ``image_reservation_under_configured`` on the image call. A $0 model can never
    exceed a dollar reservation, so measured image tokens above the ceiling's
    reserved tokens are reported with the same code. ``basis`` shows
    ``assumption`` ceilings that still need a measured row.
    """
    rows: list[ProbeRow] = []
    with tempfile.TemporaryDirectory(prefix="arch") as raw:
        store = TelemetryStore(Path(raw) / "telemetry")
        try:
            runtime = Runtime(store)
            for model in models:
                rows.append(
                    await _probe_model(
                        runtime, factory, prices, model, page, (providers or {}).get(model)
                    )
                )
        finally:
            store.close()
    return rows


async def _probe_model(
    runtime: Runtime,
    factory: Callable[[str], ModelAdapter],
    prices: Prices,
    model: str,
    page: ImageInput,
    provider: str | None,
) -> ProbeRow:
    meter = CallMeter()
    try:
        adapter = instrument(factory(model), meter)
    except Exception as exc:
        return ProbeRow(
            model=model,
            provider=provider or "unknown",
            image_configured=False,
            basis=None,
            text_reserved_tokens=0,
            text_measured_tokens=None,
            image_reserved_tokens=None,
            image_estimate_tokens=None,
            image_measured_tokens=None,
            reserved_usd=0.0,
            measured_usd=None,
            code=_code(exc),
        )
    price = adapter_price(adapter, prices)
    pricing = price.image if price is not None else None
    text = ModelRequest(prompt=PROBE_TEXT, max_output_tokens=16)
    reserved_usd = adapter.quote(text).cost_usd
    text_reserved, text_measured, text_usd, code, wall = await _probe_call(
        runtime, adapter, meter, PROBE_TEXT, ()
    )
    measured_usd = text_usd
    image_reserved = image_estimate_tokens = image_measured = None
    if code == "reservation_under_configured":
        code = "provider_reservation_exceeded"  # the text ceiling; the image call is skipped
    if code is None and text_measured is not None and text_measured > text_reserved:
        # A $0 model never trips the dollar wallet; compare tokens as for images.
        code = "provider_reservation_exceeded"
    if pricing is not None and price is not None:
        image_reserved = image_reservation(price, [page])
        image_estimate_tokens = image_estimate(price, page, pricing.detail)
        if code is None:
            reserved_usd += adapter.quote(
                ModelRequest(prompt=PROBE_IMAGE, max_output_tokens=16, images=(page,))
            ).cost_usd
            _, image_in, image_usd, code, image_wall = await _probe_call(
                runtime, adapter, meter, PROBE_IMAGE, (page,)
            )
            wall += image_wall
            if code == "reservation_under_configured":
                code = "image_reservation_under_configured"
            if image_in is not None and text_measured is not None:
                image_measured = max(0, image_in - text_measured)
            if code is None and image_measured is not None and image_measured > image_reserved:
                # The runtime's wallet check is in dollars, so it cannot see an
                # under-configured ceiling on a $0 (local) model: compare tokens too.
                code = "image_reservation_under_configured"
            if image_usd is not None:
                measured_usd = (measured_usd or 0.0) + image_usd
    elif code is None:
        code = "image_not_configured"
    return ProbeRow(
        model=model,
        provider=provider or ("fake" if isinstance(adapter, FakeAdapter) else "unknown"),
        image_configured=pricing is not None,
        basis=pricing.basis if pricing is not None else None,
        text_reserved_tokens=text_reserved,
        text_measured_tokens=text_measured,
        image_reserved_tokens=image_reserved,
        image_estimate_tokens=image_estimate_tokens,
        image_measured_tokens=image_measured,
        reserved_usd=reserved_usd,
        measured_usd=measured_usd,
        wall_s=max(0.0, wall),
        code=code,
    )
