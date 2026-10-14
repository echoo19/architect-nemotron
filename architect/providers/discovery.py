"""List a provider's models (Phase 13, Section 3).

- CLIs: their fixed, priced lists (nothing runs).
- OpenAI-compatible APIs, LM Studio, llama.cpp, vLLM, custom: ``GET {base}/models``.
- Anthropic: ``GET {base}/v1/models``.
- Ollama: ``GET {base}/api/tags``.
- OpenRouter: ``GET {base}/models`` with per-token prices, converted to a ``Price``.
- Nebius Token Factory: ``GET {base}/models?verbose=true`` (with the key), priced the
  same way; models whose output is not text (embeddings) are left out.

Every request has a short timeout, follows no redirects and ignores proxy
variables (as the adapters do). Failures return a ``Discovery`` with a fixed
``code`` (``no_key``, ``unreachable``, ``timeout``, ``unauthorized``,
``http_error``, ``invalid_response``, ``no_url``, or a judges' relay refusal from
``architect.models.relay``), never an exception, and never the provider's text or
the key.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import httpx

from architect.models.pricing import Price, per_token_price
from architect.models.relay import refusal

from . import credentials
from .catalog import ProviderEntry, default_base_url
from .host import Host

__all__ = ["DEFAULT_TIMEOUT_S", "MAX_MODELS", "Discovered", "Discovery", "list_models"]

DEFAULT_TIMEOUT_S = 5.0
MAX_MODELS = 1000
_MAX_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class Discovered:
    id: str
    price: Price | None = None  # a priced listing's (OpenRouter, Nebius) list price
    context: int | None = None


@dataclass(frozen=True)
class Discovery:
    provider: str
    models: tuple[Discovered, ...] = ()
    code: str | None = None
    base_url: str | None = None

    @property
    def ok(self) -> bool:
        return self.code is None


def _number(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _priced(item: dict[str, object], model: str) -> Discovered:
    """A row with per-token ``pricing.prompt``/``completion`` strings (OpenRouter, Nebius)."""
    pricing = item.get("pricing")
    top = item.get("top_provider")  # OpenRouter only; Nebius publishes no output limit
    capacity = _positive_int(top.get("max_completion_tokens")) if isinstance(top, dict) else None
    price = None
    if isinstance(pricing, dict):
        prompt, completion = _number(pricing.get("prompt")), _number(pricing.get("completion"))
        if prompt is not None and completion is not None:
            price = per_token_price(
                prompt, completion, output_capacity_tokens=capacity,
                allowed_response_models=(model,),
            )
    return Discovered(model, price, _positive_int(item.get("context_length")))


def _writes_text(item: dict[str, object]) -> bool:
    """False for a row whose declared output is not text (``text->embedding``)."""
    architecture = item.get("architecture")
    modality = architecture.get("modality") if isinstance(architecture, dict) else None
    if not isinstance(modality, str) or "->" not in modality:
        return True
    return "text" in modality.rsplit("->", 1)[1].split("+")


def _parse(item: ProviderEntry, data: object) -> tuple[Discovered, ...] | None:
    if not isinstance(data, dict):
        return None
    rows = data.get("models") if item.listing == "ollama" else data.get("data")
    if not isinstance(rows, list):
        return None
    found: dict[str, Discovered] = {}
    for row in rows[:MAX_MODELS]:
        if not isinstance(row, dict):
            continue
        name = row.get("name") if item.listing == "ollama" else row.get("id")
        if not isinstance(name, str) or not name.strip() or len(name) > 256:
            continue
        model = name.strip()
        if item.id == "gemini" and model.startswith("models/"):
            model = model[len("models/") :]
        if item.listing == "nebius" and not _writes_text(row):
            continue
        if item.listing in ("openrouter", "nebius"):
            found[model] = _priced(row, model)
        else:
            found[model] = Discovered(model)
    return tuple(found[name] for name in sorted(found))


def _request(item: ProviderEntry, base: str, key: str) -> tuple[str, dict[str, str]]:
    base = base.rstrip("/")
    if item.listing == "ollama":
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        return base + "/api/tags", headers
    if item.listing == "anthropic":
        return base + "/v1/models?limit=1000", {
            "x-api-key": key, "anthropic-version": "2023-06-01"}
    if item.listing == "nebius":  # verbose rows carry prices, context and modality
        return base + "/models?verbose=true", {"Authorization": f"Bearer {key}"}
    return base + "/models", {"Authorization": f"Bearer {key}"} if key else {}


async def list_models(
    item: ProviderEntry,
    *,
    workspace: Path | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    host: Host | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Discovery:
    """The provider's models. ``api_key`` and ``base_url`` override what is stored."""
    host = host or Host()
    if item.listing == "fixed":
        return Discovery(item.id, tuple(Discovered(model) for model in item.models))
    base = base_url
    if base is None and workspace is not None:
        base = credentials.get_base_url(item.id, workspace)
    base = base or default_base_url(item, host.env)
    if not base:
        return Discovery(item.id, code="no_url")
    key = api_key
    if key is None and workspace is not None:
        key = credentials.resolve(item.id, workspace, env=host.env)
    if key is None and item.listing != "openrouter":  # OpenRouter's list is public
        return Discovery(item.id, code="no_key", base_url=base)
    url, headers = _request(item, base, key or "")
    try:
        async with httpx.AsyncClient(
            transport=host.transport, timeout=timeout_s, follow_redirects=False, trust_env=False
        ) as client:
            response = await client.get(url, headers=headers)
    except httpx.TimeoutException:
        return Discovery(item.id, code="timeout", base_url=base)
    except (httpx.HTTPError, ValueError):
        return Discovery(item.id, code="unreachable", base_url=base)
    refused = refusal(response.status_code, response.content)
    if refused is not None:  # the judges' relay said why (an unknown code, the kill switch)
        return Discovery(item.id, code=refused, base_url=base)
    if response.status_code in (401, 403):
        return Discovery(item.id, code="unauthorized", base_url=base)
    if response.status_code != 200:
        return Discovery(item.id, code="http_error", base_url=base)
    if len(response.content) > _MAX_BYTES:
        return Discovery(item.id, code="invalid_response", base_url=base)
    try:
        models = _parse(item, response.json())
    except ValueError:
        models = None
    if models is None:
        return Discovery(item.id, code="invalid_response", base_url=base)
    return Discovery(item.id, models, base_url=base)
