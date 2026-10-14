"""Provider and model commands (Phase 13, Section 3): /login, /logout, /providers,
/models and the /model picker (which replaces the Phase 12 ``/model``).

The flows live in ``architect.providers``; these handlers adapt them to the REPL.
Test seams come from ``conductor.extras["providers_host"]`` (an
``architect.providers.host.Host``: PATH lookup, environment, home, HTTP transport,
the foreground login runner); without one the real host is used.

Switching models rewrites the JSON profile through ``register_model`` and swaps the
running services with the app's ``conductor.extras["reload"]``; a reload that fails
puts the profile file back. The next compile's model is set through build mode's
``set_override`` exactly as Phase 12's ``/model`` did, except for an unpriced model:
build mode would refuse it (``model_unpriced``), so only the conductor switches.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import math
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from architect.models.pricing import DEFAULT_OUTPUT_CAPACITY, UNPRICED_LABEL, Price
from architect.providers import credentials
from architect.providers.catalog import (
    CATALOG,
    CODEX_MODELS,
    ProviderEntry,
    find,
    split_choice,
)
from architect.providers.discovery import Discovery, list_models
from architect.providers.host import Host
from architect.providers.onboarding import (
    ProviderError,
    load_payload,
    provider_of,
    register_model,
    save_payload,
)
from architect.providers.signin import (
    Prompter,
    choose_model,
    price_label,
    sign_in,
)
from architect.providers.status import ProviderStatus, all_statuses

from .commands import command
from .tools import ToolError

if TYPE_CHECKING:
    from .repl import Repl

__all__ = ["PICKER_KEY", "picker_rows"]

PICKER_KEY = "model_picker"  # conductor.extras: the last numbered /model list
_LIST_LIMIT = 40
_GROUP_LIMIT = 15
# Bundled API ids each key-based provider serves (an offline suggestion list).
_FAMILIES: dict[str, tuple[str, ...]] = {
    "anthropic": ("claude-",),
    "openai": ("gpt-", "o3", "o4-"),
    "gemini": ("gemini-",),
    "nebius": ("nvidia/",),
}


# -- seams ------------------------------------------------------------------------------


def _host(repl: Repl) -> Host:
    value = repl.conductor.extras.get("providers_host")
    return value if isinstance(value, Host) else Host()


def _prompter(repl: Repl) -> Prompter:
    secret = getattr(repl.frontend, "ask_secret", None)
    return Prompter(repl.say, repl.frontend.ask, secret if callable(secret) else None)


def _workspace(repl: Repl) -> Path:
    return Path(repl.app.workspace)


async def _reload(repl: Repl) -> None:
    reload = repl.conductor.extras.get("reload")
    if not callable(reload):
        raise ProviderError("reload_unavailable")
    result = reload()
    if inspect.isawaitable(result):
        await result


def _set_override(repl: Repl, key: str, value: object) -> bool:
    """Build mode's ``set_override`` (Phase 12 ``/model`` behaviour), else a plain store."""
    context = repl.conductor.context
    try:
        module = importlib.import_module("architect.agent.build")
        setter: Callable[..., object] | None = getattr(module, "set_override", None)
    except Exception:  # build mode missing or broken: keep the value with the same key
        setter = None
    try:
        if callable(setter):
            text = str(setter(context, key, value))
        else:
            if value not in repl.app.services.profile.models:
                raise ToolError("model_not_configured", str(value))
            overrides = repl.conductor.extras.get("build_overrides")
            if not isinstance(overrides, dict):
                overrides = {}
                repl.conductor.extras["build_overrides"] = overrides
            overrides[key] = value
            text = f"model {value}"
    except ToolError as exc:
        detail = f": {exc.detail}" if exc.detail else ""
        repl.say(f"Not changed ({exc.code}{detail}).", "error")
        return False
    repl.say(f"Next compile: {text}.")
    return True


def _unpriced(repl: Repl, model: str) -> bool:
    from architect.profiles import model_unpriced

    try:
        return model_unpriced(repl.app.services.profile, model)
    except Exception:
        return True


def _snapshot(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8") if path.exists() else None
    except OSError:
        return None


def _restore(path: Path, text: str | None) -> None:
    if text is not None:
        try:
            path.write_text(text, encoding="utf-8")
        except OSError:
            pass


# -- switching --------------------------------------------------------------------------


def _use(repl: Repl, name: str, label: str = "") -> bool:
    """Point the conductor at profile model ``name`` and set the next compile's model."""
    try:
        adapter = repl.app.services.adapter_factory(name)
    except Exception:
        repl.say(f"{name} could not be loaded.", "error")
        return False
    conductor = repl.conductor
    conductor.adapter = adapter
    conductor.context.model = name
    repl.say(f"Conductor model: {name}{label}.")
    if _unpriced(repl, name):
        repl.say(f"{name}: {UNPRICED_LABEL}. Conversation and subagents run on the time "
                 "budget only; build mode will not compile on it (model_unpriced), so the "
                 "next compile keeps its own model.", "warn")
        return True
    _set_override(repl, "model", name)
    return True


async def _register_and_use(
    repl: Repl, item: ProviderEntry, model: str, *, price: Any = None,
    base_url: str | None = None, declared: bool = False,
) -> bool:
    """Add ``item``'s ``model`` to the profile, reload, switch. The file is restored on failure.

    ``price`` is the provider's listed price, or the user's when ``declared``."""
    if item.kind == "api" and not item.key_optional and credentials.get(
            item.id, _workspace(repl), env=_host(repl).env) is None:
        repl.say(f"{item.label} has no key yet: /login {item.id} first.", "error")
        return False
    path = Path(repl.app.profile_path)
    try:
        before = _snapshot(path)
        key = await asyncio.to_thread(
            register_model, path, item.id, model, workspace=_workspace(repl),
            base_url=base_url, price=price, declared=declared, host=_host(repl),
        )
    except ProviderError as exc:
        hints = {
            "profile_not_json": "the profile is TOML; add the model to it by hand",
            "model_not_offered": f"{item.label} offers: {', '.join(item.models)}",
            "cli_missing": f"{item.executable} is not on PATH",
            "no_url": f"no URL for {item.label} yet: /login {item.id}",
        }
        repl.say(f"Could not add {model} ({exc.code}"
                 + (f": {hints[exc.code]}" if exc.code in hints else "") + ").", "error")
        return False
    except OSError:
        repl.say("Could not read the profile.", "error")
        return False
    try:
        await _reload(repl)
    except Exception as exc:
        _restore(path, before)
        code = getattr(exc, "code", None) or str(exc) or "invalid_profile"
        repl.say(f"{key} was not added: the profile did not load ({code}).", "error")
        return False
    return _use(repl, key, f" via {item.label}")


# -- listing ----------------------------------------------------------------------------


def _offline(item: ProviderEntry) -> tuple[str, ...]:
    """Models we can suggest without a network call: fixed lists and bundled prices."""
    if item.kind == "cli":
        return item.models
    from architect.models.pricing import load_prices

    prefixes = _FAMILIES.get(item.id, ())
    bundled = [name for name, price in load_prices().models.items()
               if not price.unpriced and name.startswith(prefixes) and prefixes]
    names = [name for name in bundled if name not in CODEX_MODELS]
    if item.default_model and item.default_model not in names:
        names.insert(0, item.default_model)
    return tuple(names)


async def picker_rows(repl: Repl) -> list[tuple[str, str, bool]]:
    """``(provider id, model, configured)`` for the numbered ``/model`` list.

    Configured profile models first (grouped by provider), then models of ready
    providers that need no network to suggest. Local servers are never probed here
    (``/models <provider>`` lists them live).
    """
    profile = repl.app.services.profile
    rows: list[tuple[str, str, bool]] = []
    seen: set[tuple[str, str]] = set()
    configured = sorted(
        ((provider_of(config), name) for name, config in profile.models.items()),
        key=lambda pair: (_order(pair[0]), pair[1]),
    )
    for provider, name in configured:
        rows.append((provider, name, True))
        seen.add((provider, name))
    statuses = await all_statuses(_workspace(repl), host=_host(repl), probe=False)
    for item, status in zip(CATALOG, statuses, strict=True):
        if not status.ready or item.kind == "local":
            continue
        for name in _offline(item):
            if (item.id, name) not in seen and name not in profile.models:
                rows.append((item.id, name, False))
                seen.add((item.id, name))
    return rows


def _order(provider: str) -> int:
    ids = [item.id for item in CATALOG]
    return ids.index(provider) if provider in ids else len(ids)


def _label(provider: str) -> str:
    item = find(provider)
    return item.label if item is not None else provider


async def _show_picker(repl: Repl) -> None:
    conductor = repl.conductor
    overrides = conductor.extras.get("build_overrides")
    if not isinstance(overrides, dict):
        overrides = {}
    current = conductor.adapter.model
    repl.rows("Models", [
        ("conductor", current + (f" ({UNPRICED_LABEL})" if _unpriced(repl, current) else "")),
        ("next compile", str(overrides.get("model", "cheapest that passes"))),
    ])
    rows = await picker_rows(repl)
    conductor.extras[PICKER_KEY] = [(provider, name) for provider, name, _ in rows]
    shown: list[tuple[str, str]] = []
    group = None
    for index, (provider, name, configured) in enumerate(rows, start=1):
        if provider != group:
            group = provider
            shown.append((f"-- {_label(provider)}", ""))
        mark = "*" if name == current else " "
        state = "in profile" if configured else "add"
        item = find(provider)
        cost = price_label(item, name) if item is not None else ""
        shown.append((f"{mark}{index:>3}. {name}", f"{state}  {cost}".rstrip()))
    repl.rows("Pick a model", shown)
    repl.text("Choose with /model <number> or /model <provider>:<model> (e.g. /model "
              "openrouter:anthropic/claude-sonnet-4.5). /models <provider> lists a provider's "
              "models live; /login connects another provider.")


async def _pick_number(repl: Repl, number: int) -> tuple[str, str] | None:
    picker = repl.conductor.extras.get(PICKER_KEY)
    if not isinstance(picker, list) or not picker:
        picker = [(provider, name) for provider, name, _ in await picker_rows(repl)]
        repl.conductor.extras[PICKER_KEY] = picker
    if not 1 <= number <= len(picker):
        repl.say(f"No model {number} in the list; /model shows it.", "warn")
        return None
    provider, name = picker[number - 1]
    return str(provider), str(name)


# -- commands ---------------------------------------------------------------------------


ADD_USAGE = "Usage: /model add <provider>:<model> [--price <in>/<out>]  (USD per million tokens)"


def _declared_price(words: list[str]) -> Price | None | bool:
    """``--price 1.5/6`` -> a ``Price``; None without the flag; False when malformed."""
    if "--price" not in words:
        return None
    index = words.index("--price")
    raw = words[index + 1] if index + 1 < len(words) else ""
    head, sep, tail = raw.lstrip("$").partition("/")
    try:
        rates = (float(head), float(tail.lstrip("$")))
    except ValueError:
        return False
    if not sep or not all(math.isfinite(rate) and 0 <= rate <= 10_000 for rate in rates):
        return False
    return Price(input_per_million=rates[0], output_per_million=rates[1],
                 output_capacity_tokens=DEFAULT_OUTPUT_CAPACITY, input_overhead_tokens=1024)


async def _add(repl: Repl, words: list[str]) -> None:
    """``/model add <provider>:<model> [--price in/out]``: declare a model (and its price)."""
    price = _declared_price(words)
    target = next((word for word in words if word != "--price" and "/" not in word.split(":")[0]
                   and ":" in word), None)
    item, name = split_choice(target or "")
    if price is False or item is None:
        repl.say(ADD_USAGE, "warn")
        return
    if item.kind == "cli":
        repl.say(f"{item.label} models are fixed and priced: {', '.join(item.models)}.", "warn")
        return
    declared = price if isinstance(price, Price) else None
    await _register_and_use(repl, item, name, price=declared, declared=declared is not None)


@command("/model", "/model [number | provider:model | name | add ...]",
         "Pick the model (conductor and next compile); no argument lists them",
         detail="Lists the profile's models and ones your signed-in providers offer, grouped "
         "by provider. A number or provider:model adds the model to the profile when needed "
         "and switches to it. '/model add <provider>:<model> --price <in>/<out>' declares a "
         "price in USD per million tokens. Unpriced models ('cost unknown') work for "
         "conversation and subagents on a time budget; build mode does not compile on them.")
async def _model(repl: Repl, arg: str) -> None:
    if not arg:
        await _show_picker(repl)
        return
    if arg.split()[0] == "add":
        await _add(repl, arg.split()[1:])
        return
    text = arg.split()[0]
    models = list(repl.app.services.profile.models)
    if text.isdigit():
        picked = await _pick_number(repl, int(text))
        if picked is None:
            return
        provider, name = picked
        item = find(provider)
        config = repl.app.services.profile.models.get(name)
        if item is None or (config is not None and provider_of(config) == provider):
            _use(repl, name)
            return
        await _register_and_use(repl, item, name)
        return
    item, name = split_choice(text)
    if item is not None:
        price = None
        if item.listing in ("openrouter", "nebius"):  # listings that publish prices
            found = await list_models(item, workspace=_workspace(repl), host=_host(repl))
            price = next((m.price for m in found.models if m.id == name), None)
        config = repl.app.services.profile.models.get(name)
        if config is not None and provider_of(config) == item.id:
            _use(repl, name, f" via {item.label}")
            return
        await _register_and_use(repl, item, name, price=price)
        return
    if name in models:
        _use(repl, name)
        return
    from .app import add_detected_model

    if add_detected_model(repl.app.profile_path, name):
        try:
            await _reload(repl)
        except Exception:
            repl.say(f"{name} could not be loaded.", "error")
            return
        repl.say(f"Added {name} to the profile.")
        _use(repl, name)
        return
    repl.say(f"{name} is not in the profile. Models: {', '.join(models)}.", "error")
    repl.say(f"To add it, name its provider: /model <provider>:{name} (e.g. /model "
             f"openai:{name}); /model lists what is available.", "warn")


def _status_rows(statuses: Sequence[ProviderStatus]) -> list[tuple[str, str]]:
    rows = []
    for item, status in zip(CATALOG, statuses, strict=True):
        mark = "ready" if status.ready else "-"
        rows.append((f"{item.id:<10} {item.label}", f"{mark:<5}  {status.detail}"))
    return rows


@command("/providers", "/providers", "Every provider: signed in, key present, server reachable",
         detail="Cheap checks only: CLIs on PATH and their login files, API keys in the "
         "environment or stored, local servers answering their model list. No model is "
         "called or loaded.")
async def _providers(repl: Repl, arg: str) -> None:
    statuses = await all_statuses(_workspace(repl), host=_host(repl))
    repl.rows("Providers", _status_rows(statuses))
    repl.text("/login <provider> connects one; /models <provider> lists its models.")


def _discovery_rows(item: ProviderEntry, found: Discovery, word: str,
                    limit: int) -> list[tuple[str, str]]:
    models = [m for m in found.models if word.lower() in m.id.lower()] if word else list(
        found.models)
    rows = [(f"{item.id}:{model.id}", price_label(item, model)) for model in models[:limit]]
    if len(models) > limit:
        rows.append((f"... {len(models) - limit} more",
                     f"/models {item.id} <word> filters"))
    return rows


_CODES = {
    "no_key": "no key (/login {id})",
    "unauthorized": "the key was rejected (/login {id})",
    "unreachable": "not reachable",
    "timeout": "timed out",
    "no_url": "no URL yet (/login {id})",
    "http_error": "the server returned an error",
    "invalid_response": "unexpected answer",
}


@command("/models", "/models [provider] [filter]", "List models (live) for one or every "
         "ready provider", detail="With a provider, its live model list (OpenRouter and "
         "Nebius show prices); a word filters it. Without one, every ready provider, briefly.")
async def _models(repl: Repl, arg: str) -> None:
    words = arg.split()
    host, workspace = _host(repl), _workspace(repl)
    if words:
        item = find(words[0])
        if item is None:
            repl.say(f"Unknown provider {words[0]}. /providers lists them.", "warn")
            return
        found = await list_models(item, workspace=workspace, host=host)
        if not found.ok:
            reason = _CODES.get(found.code or "", found.code or "failed").format(id=item.id)
            repl.say(f"{item.label}: {reason}.", "error")
            return
        word = " ".join(words[1:])
        rows = _discovery_rows(item, found, word, _LIST_LIMIT)
        repl.rows(f"{item.label} models" + (f" matching '{word}'" if word else ""),
                  rows or [("(none)", "")])
        repl.text(f"Use one with /model {item.id}:<model>.")
        return
    statuses = await all_statuses(workspace, host=host)
    ready = [item for item, status in zip(CATALOG, statuses, strict=True) if status.ready]
    if not ready:
        repl.say("No provider is ready. /login connects one.", "warn")
        return
    results = await asyncio.gather(*(
        list_models(item, workspace=workspace, host=host) for item in ready))
    for item, found in zip(ready, results, strict=True):
        if found.ok:
            repl.rows(item.label, _discovery_rows(item, found, "", _GROUP_LIMIT)
                      or [("(none)", "")])
        else:
            reason = _CODES.get(found.code or "", found.code or "failed").format(id=item.id)
            repl.rows(item.label, [("(not listed)", reason)])
    repl.text("Use one with /model <provider>:<model>; /models <provider> shows the full list.")


@command("/login", "/login [provider]", "Connect a provider: sign in, add a key or a local "
         "server", detail="Without a provider, pick one from the list (with what was "
         "detected). claude: uses the claude CLI's login. chatgpt: runs the official "
         "'codex login'. API providers: paste a key (stored in <workspace>/credentials.json, "
         "never logged; environment variables win). Local servers: check the URL and list "
         "models; Architect never starts a server or loads a model.")
async def _login(repl: Repl, arg: str) -> None:
    host, workspace, prompter = _host(repl), _workspace(repl), _prompter(repl)
    if arg:
        item = find(arg.split()[0])
        if item is None:
            repl.say(f"Unknown provider {arg.split()[0]}. Providers: "
                     + ", ".join(entry.id for entry in CATALOG) + ".", "warn")
            return
    else:
        statuses = await all_statuses(workspace, host=host)
        numbered = [(f"{index:>2}. {row[0]}", row[1])
                    for index, row in enumerate(_status_rows(statuses), start=1)]
        repl.rows("Providers", numbered)
        answer = await repl.frontend.ask("Provider (number or name; Enter cancels)")
        text = (answer or "").strip().lower()
        if not text:
            repl.say("Nothing changed.")
            return
        item = (CATALOG[int(text) - 1] if text.isdigit() and 1 <= int(text) <= len(CATALOG)
                else find(text))
        if item is None:
            repl.say(f"'{text}' is not a provider.", "warn")
            return
    result = await sign_in(item, workspace, prompter, host=host)
    if not result.ok:
        return
    models = result.models
    if not models and item.kind != "cli":
        found = await list_models(item, workspace=workspace, host=host)
        models = found.models
    chosen = await choose_model(item, models, prompter, allow_skip=True)
    if chosen is None:
        repl.say(f"{item.label} is connected. Switch any time with /model {item.id}:<model>.")
        return
    await _register_and_use(repl, item, chosen.id, price=chosen.price, base_url=result.base_url)


def _dependents(payload: dict[str, Any], provider: str) -> list[str]:
    models = payload.get("models") or {}
    return [name for name, config in models.items()
            if isinstance(config, dict) and config.get("api_key_ref") == provider]


def _prune(payload: dict[str, Any], provider: str) -> list[str]:
    """Drop profile entries that use ``provider``'s stored key; returns their names."""
    names = _dependents(payload, provider)
    models = payload.get("models") or {}
    for name in names:
        models.pop(name, None)
    ladder = (payload.get("compile") or {}).get("model_ladder")
    if isinstance(ladder, list):
        payload["compile"]["model_ladder"] = [m for m in ladder if m not in names]
    for role in ("inference", "decomposition", "rewrite", "forge_agent"):
        config = payload.get(role)
        if isinstance(config, dict) and config.get("api_key_ref") == provider:
            payload.pop(role)
    return names


@command("/logout", "/logout <provider>", "Forget a provider's stored key (or sign out of "
         "chatgpt)", detail="API providers: the stored key is deleted and the profile's "
         "models that need it are removed (switch the conductor away first). Keys set in "
         "the environment stay in effect. chatgpt runs 'codex logout'; claude is signed out "
         "inside the claude CLI.")
async def _logout(repl: Repl, arg: str) -> None:
    if not arg:
        repl.say("Usage: /logout <provider>  (/providers lists them)", "warn")
        return
    item = find(arg.split()[0])
    if item is None:
        repl.say(f"Unknown provider {arg.split()[0]}.", "warn")
        return
    host, workspace = _host(repl), _workspace(repl)
    if item.id == "claude":
        repl.say("Architect does not hold the claude login: run 'claude' and /logout there.")
        return
    if item.id == "chatgpt":
        executable = host.which(item.executable or "codex")
        if not executable:
            repl.say("codex is not on PATH; nothing to sign out of.", "warn")
            return
        code = await asyncio.to_thread(host.runner, [executable, "logout"])
        repl.say("ChatGPT: signed out of codex." if code == 0
                 else f"codex logout exited with {code}.", "info" if code == 0 else "error")
        return
    path = Path(repl.app.profile_path)
    try:
        payload = load_payload(path)
    except ProviderError:
        payload = None
    env_key = credentials.source(item.id, workspace, env=host.env) not in (None, "file")
    prune = payload is not None and item.kind == "api" and not item.key_optional and not env_key
    if prune and payload is not None:
        current = repl.conductor.adapter.model
        if current in _dependents(payload, item.id):
            repl.say(f"The conductor uses {current} from {item.label}: switch with /model "
                     "first, then /logout again.", "warn")
            return
    removed = credentials.remove(item.id, workspace)
    repl.say(f"{item.label}: stored key removed." if removed
             else f"{item.label}: no stored key.")
    if env_key:
        variable = credentials.source(item.id, workspace, env=host.env)
        repl.say(f"{variable} is still set in your environment, so {item.label} stays usable; "
                 "unset it to sign out fully.", "warn")
        return
    if not prune or payload is None:
        return
    before = _snapshot(path)
    names = _prune(payload, item.id)
    if not names:
        return
    try:
        save_payload(path, payload)
        await _reload(repl)
    except Exception:
        _restore(path, before)
        repl.say(f"The profile still lists {', '.join(names)}; they will fail until you "
                 f"/login {item.id} again.", "warn")
        return
    repl.say(f"Removed from the profile: {', '.join(names)}.")
