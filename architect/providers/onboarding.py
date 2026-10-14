"""First-run choices and registering a provider's model into the JSON profile.

``onboarding_choices(workspace)`` lists every provider with what was detected
(logged-in CLIs, keys in the environment or stored, reachable local servers) in a
useful order and marks one recommended. Local models are never recommended: they
use this machine's GPU and run only when the user picks one.

``register_model(profile_path, provider_id, model)`` writes (or creates) the
profile entry with the right ``AdapterConfig``: the adapter, ``base_url``,
``api_key_ref`` (the credential name; never the key) and, when the bundled table
does not price the model, a ``prices`` entry in ``<workspace>/model-prices.json``
($0 for local servers, a listed price from OpenRouter or Nebius, a declared price, or
an ``unpriced`` placeholder). It returns the profile model key, which is the model id
the adapter sends. ``signin.onboard`` is the interactive flow on top of both.

A fresh profile for an NVIDIA Nemotron model on Nebius Token Factory gets a role pack
(``NEBIUS_ROLES``): every role on the Nemotron model that suits it, its own build budget
(``NEBIUS_COMPILE_BUDGET``) and Forge cap (``NEBIUS_FORGE``), and the chosen model as the
conversation model in ``<workspace>/config.toml``.
"""

from __future__ import annotations

import copy
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from architect.models.pricing import (
    Price,
    load_prices,
    local_price,
    unpriced_price,
    write_price_entries,
)
from architect.models.types import AdapterError

from . import credentials
from .catalog import CATALOG, ProviderEntry, default_base_url, find
from .host import Host
from .status import ProviderStatus, all_statuses

__all__ = [
    "NEBIUS_COMPILE_BUDGET",
    "NEBIUS_FORGE",
    "NEBIUS_ROLES",
    "NEMOTRON_MODELS",
    "PRICES_FILE",
    "Choice",
    "ProviderError",
    "bundled_price",
    "load_payload",
    "onboarding_choices",
    "profile_workspace",
    "provider_of",
    "recommended",
    "register_model",
    "save_payload",
]

PRICES_FILE = "model-prices.json"
# Recommendation order among ready providers (local ones are never recommended).
_PREFERENCE = ("nebius", "claude", "chatgpt", "anthropic", "openai", "openrouter", "gemini")
# NVIDIA Nemotron on Nebius Token Factory, bundled and priced (models/prices.json).
NEMOTRON_MODELS = (
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
    "nvidia/Nemotron-3_5-Lightning",
    "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/Nemotron-3-Ultra-550b-a55b",
)
# The role pack a fresh Nebius profile gets; the chosen model (Super by default) holds
# the conversation. Thinking is off unless a role turns it on (the Nebius dialect's
# default), so conversation, interview, Stage 0 and preset replies fit their output caps.
# Super infers the Spec and, when the user chooses Forge, engineers the harness, with
# thinking off at temperature 0 and 300 s a call, as in the last Forge round (DECISIONS.md,
# Nebius role pack); the built agent runs on Lightning, else Super. Ultra and Nano stay
# priced for /model.
NEBIUS_ROLES: dict[str, Any] = {
    "inference": {"model": "nvidia/nemotron-3-super-120b-a12b"},
    "forge_agent": {
        "model": "nvidia/nemotron-3-super-120b-a12b",
        "think": False,
        "temperature": 0.0,
        "timeout_s": 300,
    },
    "model_ladder": ["nvidia/Nemotron-3_5-Lightning", "nvidia/nemotron-3-super-120b-a12b"],
}
# Forge's own cap on a fresh Nebius profile: builds there ship the tuned preset unless the
# user chooses Forge (agent.build.default_engineer), and a Forge session then fits in one
# day of a judge code ($3).
NEBIUS_FORGE: dict[str, Any] = {"max_usd": 1.2, "max_s": 2700}
# The build budget of a fresh Nebius profile (the plan card's "build up to"): with Forge's
# cap on top, a build still fits in one day of a judge code ($1.50 + $1.20 of $3).
NEBIUS_COMPILE_BUDGET: dict[str, Any] = {"cost_usd": 1.5, "compute_s": 1800}
_MODEL = re.compile(r"^[^\s\x00-\x1f]{1,256}$")
# What a fresh profile compiles with: the bootstrap's defaults (session/bootstrap.py).
_COMPILE_DEFAULTS: dict[str, Any] = {
    "max_candidates": 2,
    "enable_rewriting": False,
    "max_rewrites": 0,
    "compile_budget": {"cost_usd": 5, "compute_s": 1800},
}
_INFERENCE_PROMPT_BYTES = 6500
_INFERENCE_USD = 1.0  # compiler.inference._INFERENCE_BUDGET


class ProviderError(Exception):
    """A fixed code: ``unknown_provider``, ``invalid_model``, ``model_not_offered``,
    ``cli_missing``, ``no_url``, ``profile_not_json``, ``invalid_profile``,
    ``write_failed``, ``setup_cancelled``, ``no_provider``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Choice:
    index: int
    provider: ProviderEntry
    status: ProviderStatus
    model: str | None  # the suggested model for this provider, if any
    recommended: bool = False

    # Plain views for front ends that render choices generically.
    @property
    def id(self) -> str:
        return self.provider.id

    @property
    def label(self) -> str:
        return self.provider.label

    @property
    def detail(self) -> str:
        return self.status.detail

    @property
    def available(self) -> bool:
        return self.status.ready


# -- choices --------------------------------------------------------------------------


def _rank(choice: tuple[ProviderEntry, ProviderStatus]) -> tuple[int, int]:
    item, status = choice
    order = _PREFERENCE.index(item.id) if item.id in _PREFERENCE else len(_PREFERENCE)
    group = 0 if status.ready and item.kind != "local" else 1 if status.ready else 2
    return group, order if group == 0 else CATALOG.index(item)


async def onboarding_choices(
    workspace: Path, *, host: Host | None = None, probe_local: bool = True
) -> tuple[Choice, ...]:
    """Every provider, ready ones first, with its status and a suggested model.

    Ready cloud providers come first (Nebius Token Factory, Claude CLI, ChatGPT,
    Anthropic, OpenAI, OpenRouter, Gemini, then the rest), then reachable local servers,
    then the providers that need a sign-in. The first ready cloud provider is
    recommended; with none, nothing is (a local model is the user's explicit choice).
    """
    statuses = await all_statuses(workspace, host=host, probe=probe_local)
    pairs = sorted(zip(CATALOG, statuses, strict=True), key=_rank)
    choices: list[Choice] = []
    marked = False
    for index, (item, status) in enumerate(pairs, start=1):
        is_recommended = not marked and status.ready and item.kind != "local"
        marked = marked or is_recommended
        model = item.default_model
        if model is None and item.kind == "local" and status.models:
            model = status.models[0]
        choices.append(Choice(index, item, status, model, is_recommended))
    return tuple(choices)


def recommended(choices: Sequence[Choice]) -> Choice | None:
    return next((choice for choice in choices if choice.recommended), None)


# -- the profile file -----------------------------------------------------------------


def load_payload(profile_path: Path) -> dict[str, Any] | None:
    """The JSON profile as a dict, None when the file does not exist yet."""
    if profile_path.suffix.lower() != ".json":
        raise ProviderError("profile_not_json")
    if not profile_path.exists():
        return None
    try:
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ProviderError("invalid_profile") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("models", {}), dict):
        raise ProviderError("invalid_profile")
    return payload


def save_payload(profile_path: Path, payload: Mapping[str, Any]) -> None:
    """Validate as a ``Profile``, then write atomically (0600 where honoured)."""
    from architect.profiles import Profile

    try:
        Profile.model_validate(payload)
    except (ValidationError, ValueError, TypeError):
        raise ProviderError("invalid_profile") from None
    temporary = profile_path.with_name(profile_path.name + ".tmp")
    try:
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, profile_path)
    except OSError:
        raise ProviderError("write_failed") from None


def profile_workspace(profile_path: Path, payload: Mapping[str, Any] | None) -> Path:
    """The workspace a profile names (``Profile.workspace``, default ``.architect``), as
    ``Profile.resolve`` reads it."""
    named = (payload or {}).get("workspace", ".architect")
    return (profile_path.parent / Path(str(named)).expanduser()).resolve()


def provider_of(config: Any) -> str:
    """The catalog id for a profile ``AdapterConfig`` (``fake`` and the like stay as is)."""
    ref = getattr(config, "api_key_ref", None)
    if isinstance(ref, str) and find(ref) is not None:
        return ref
    provider = str(getattr(config, "provider", ""))
    if provider == "claude-code":
        return "claude"
    if provider == "codex":
        return "chatgpt"
    if provider == "openai":
        url = str(getattr(config, "base_url", None) or "").rstrip("/")
        if not url:
            return "openai"
        for item in CATALOG:
            if item.adapter == "openai" and item.base_url and url == item.base_url.rstrip("/"):
                return item.id
        return "custom"
    return provider


# -- registration ---------------------------------------------------------------------


def _aliases(item: ProviderEntry, model: str) -> tuple[str, ...]:
    """Names a provider may report back for ``model`` (billed at the same entry)."""
    if item.id == "ollama" and ":" not in model:
        return (f"{model}:latest",)
    if item.id == "gemini":
        return (f"models/{model}",)
    return ()


def bundled_price(model: str, provider: str | None = None) -> Price | None:
    """The bundled table's priced entry for ``model`` from ``provider`` (None: any).

    A custom endpoint is never priced from it, and the Nemotron entries are Nebius's list
    prices: another provider serving the same id (OpenRouter's Super) bills its own.
    """
    if provider == "custom" or (provider not in (None, "nebius") and model in NEMOTRON_MODELS):
        return None
    price = load_prices().models.get(model)
    return price if price is not None and not price.unpriced else None


def _fits_inference(price: Price | None) -> bool:
    if price is None or price.unpriced:
        return False
    tokens_in = _INFERENCE_PROMPT_BYTES + price.input_overhead_tokens
    cost = price.quote_cost(tokens_in, price.output_capacity_tokens, may_write_cache=False)
    return cost <= _INFERENCE_USD


def _relative(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _config(
    item: ProviderEntry,
    model: str,
    workspace: Path,
    profile_dir: Path,
    *,
    base_url: str | None,
    price: Price | None,
    declared: bool,
    host: Host,
) -> tuple[dict[str, Any], Price | None]:
    """The profile entry and the price its adapter will use (None: unpriced).

    ``price`` wins over the bundled entry, except a price Nebius lists for a model the
    bundled table prices: that entry is the same list with the curated output capacity
    and response names (a listed price has 8,192 output tokens and the id only). A
    declared price always wins.
    """
    if item.kind == "cli":
        if model not in item.models:
            raise ProviderError("model_not_offered")
        found = host.which(item.executable or item.id)
        if not found:
            raise ProviderError("cli_missing")
        executable = os.path.abspath(found)
        return {"provider": item.adapter, "executable": executable}, bundled_price(model, item.id)
    url = base_url or credentials.get_base_url(item.id, workspace) or default_base_url(
        item, host.env)
    if not url:
        raise ProviderError("no_url")
    config: dict[str, Any] = {
        "provider": item.adapter,
        "base_url": url.rstrip("/"),
        "api_key_ref": item.id,
    }
    entry: Price | None
    bundled = bundled_price(model, item.id)
    if bundled is not None and item.id == "nebius" and not declared:
        return config, bundled
    if price is not None:
        entry = price
    elif item.kind == "local":
        entry = local_price(allowed_response_models=_aliases(item, model))
    elif bundled is not None:
        return config, bundled
    else:
        entry = unpriced_price(allowed_response_models=_aliases(item, model))
    try:
        path = write_price_entries(workspace / PRICES_FILE, {model: entry})
    except AdapterError:
        raise ProviderError("write_failed") from None
    config["prices"] = _relative(path, profile_dir)
    return config, None if entry.unpriced else entry


def register_model(
    profile_path: Path,
    provider_id: str,
    model: str,
    *,
    workspace: Path | None = None,
    base_url: str | None = None,
    price: Price | None = None,
    declared: bool = False,
    host: Host | None = None,
) -> str:
    """Add ``model`` from ``provider_id`` to the JSON profile (created when missing).

    ``price`` is what the provider lists for the model, or, when ``declared``, the
    user's own price (``/model add ... --price``), which always wins over the bundled
    entry. An existing entry with the same key keeps its own settings (``temperature``,
    ``max_tokens`` and the like) when it is for the same adapter: only the credential, the
    price table, the executable and, where the entry would reach another address, its
    ``base_url`` change. One for another adapter is replaced (the user picked this provider
    for it). A new profile also gets the bootstrap's compile defaults with this
    model as the ladder, and an ``inference`` entry when the model fits the Stage 0
    budget (for the Claude CLI, Haiku otherwise). A new profile for a Nemotron model
    on Nebius gets the ``NEBIUS_ROLES`` pack instead, and ``model`` becomes the
    conversation model in ``<workspace>/config.toml`` (the ladder starts with
    Lightning). ``workspace`` defaults to the one the profile names; credentials, the
    price file and ``config.toml`` live there. Returns the profile model key (the
    model id). Raises ``ProviderError`` with a fixed code; the profile is unchanged on
    any failure except the shared price file, which only ever gains entries. A profile
    that would read the same afterwards is not written at all.
    """
    item = find(provider_id)
    if item is None:
        raise ProviderError("unknown_provider")
    model = model.strip() if isinstance(model, str) else ""
    if not _MODEL.fullmatch(model):
        raise ProviderError("invalid_model")
    host = host or Host()
    payload = load_payload(profile_path)
    fresh = payload is None
    if payload is None:
        payload = {"workspace": "."}
    before = copy.deepcopy(payload)
    ws = workspace.resolve() if workspace is not None else profile_workspace(profile_path, payload)
    config, effective = _config(
        item, model, ws, profile_path.parent, base_url=base_url, price=price,
        declared=declared, host=host,
    )
    models = payload.setdefault("models", {})
    implicit = None
    if item.kind != "cli":
        implicit = credentials.get_base_url(item.id, ws) or default_base_url(item, host.env)
    models[model] = _updated_entry(models.get(model), config, implicit)
    pack = fresh and item.id == "nebius" and model in NEMOTRON_MODELS
    if pack:
        _nebius_pack(payload, config)
    elif fresh:
        if item.kind == "cli":  # every model the CLI offers, so /model can switch
            for other in item.models:
                models.setdefault(other, dict(config))
        payload["compile"] = {"model_ladder": [model], **_COMPILE_DEFAULTS}
        inference = model if _fits_inference(effective) else None
        if inference is None and item.id == "claude":
            inference = next(
                (m for m in item.models if _fits_inference(bundled_price(m, item.id))), None
            )
        if inference is not None:
            payload["inference"] = {**models[inference], "model": inference}
    if fresh or payload != before:
        save_payload(profile_path, payload)
    if pack:
        _set_conversation_model(ws, model)
    return model


# What signing in and picking a model write into a model's entry. Every other key of an
# existing entry for the same adapter (temperature, max_tokens, ...) is the user's setting.
_SIGN_IN_KEYS = frozenset(
    ("provider", "base_url", "api_key_ref", "api_key_env", "prices", "executable")
)


def _same_url(left: object, right: object) -> bool:
    return isinstance(left, str) and isinstance(right, str) and (
        left.rstrip("/") == right.rstrip("/"))


def _updated_entry(
    existing: object, config: dict[str, Any], implicit: str | None
) -> dict[str, Any]:
    """``config`` written over ``existing``, the profile's entry for the same model.

    An entry for the same adapter keeps its own settings; only what signing in owns changes:
    the credential, the price table, the executable, and ``base_url`` when the entry would
    otherwise reach another address than ``config``'s. ``implicit`` is where an entry
    without ``base_url`` goes (its credential's stored URL, else the provider's). A new
    entry, one for another adapter, or settings that no longer apply give ``config`` as is.
    """
    from architect.profiles import AdapterConfig

    if not isinstance(existing, dict) or existing.get("provider") != config.get("provider"):
        return config
    entry = {key: value for key, value in existing.items() if key not in _SIGN_IN_KEYS}
    entry.update((key, value) for key, value in config.items() if key != "base_url")
    url = config.get("base_url")
    if url is not None:
        reached = existing.get("base_url") or implicit
        entry["base_url"] = existing.get("base_url") if _same_url(reached, url) else url
        if entry["base_url"] is None:
            del entry["base_url"]  # it reaches that address without one
    try:
        AdapterConfig.model_validate(entry)
    except (ValidationError, ValueError, TypeError):
        return config
    order = [key for key in existing if key in entry]
    return {key: entry[key] for key in order + [key for key in entry if key not in order]}


def _nebius_pack(payload: dict[str, Any], config: Mapping[str, Any]) -> None:
    """Every Nemotron model, the ladder, Stage 0 and the Forge engineer (``NEBIUS_ROLES``),
    the build budget (``NEBIUS_COMPILE_BUDGET``) and Forge's cap (``NEBIUS_FORGE``).

    All four models are bundled and share the chosen model's endpoint and credential,
    so each entry is that same config; a price declared for the chosen model stays in its
    own entry.
    """
    shared = {key: value for key, value in config.items() if key != "prices"}
    models = payload["models"]
    for name in NEMOTRON_MODELS:
        models.setdefault(name, dict(shared))
    payload["compile"] = {
        "model_ladder": list(NEBIUS_ROLES["model_ladder"]),
        **_COMPILE_DEFAULTS,
        "compile_budget": dict(NEBIUS_COMPILE_BUDGET),
        "forge": dict(NEBIUS_FORGE),
    }
    for role in ("inference", "forge_agent"):
        payload[role] = {**shared, **NEBIUS_ROLES[role]}


def _set_conversation_model(workspace: Path, model: str) -> None:
    """``model = <model>`` in the workspace ``config.toml``; a file that cannot be written
    leaves the default (the first ladder model) in place."""
    from architect.agent.config import ConfigError, set_value, workspace_config_path

    try:
        set_value(workspace_config_path(workspace), "model", model)
    except ConfigError:
        pass
