"""Judge codes: Architect on the hackathon judges' capped relay.

The relay (``deploy/relay``) keeps the Token Factory key on its own server and gives each
judge an access code with dollar caps of its own: ``rc_`` and 32 letters, digits, ``-`` or
``_``. To Architect a code is a Nebius key that works at the relay's URL. ``use_code``
checks it against the relay's model list, then stores it as the ``nebius`` credential
with the relay as Nebius's base URL in ``<workspace>/credentials.json``, and makes
Nemotron 3 Super the workspace's conversation model unless one is set. Model lists, the
profile entries a first run writes, and every call then go to the relay, which serves
NVIDIA Nemotron models only.

The relay's URL is ``ARCHITECT_RELAY_URL`` when set, else ``RELAY_URL`` below, the
judges' relay on Vercel. A Nebius key in the environment is used before a stored one, so
a code is refused while such a key is set. A stored Nebius key that is not a code is
replaced only when the caller says so (the sign-in flow asks the user first).

The code never appears in a message. Every failure is a fixed code that ``explain`` words.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx

from architect.models.relay import TEXT as RELAY_TEXT

from . import credentials
from .catalog import find
from .discovery import Discovered, list_models
from .host import Host

__all__ = [
    "CODE",
    "RELAY_URL",
    "RELAY_URL_ENV",
    "JudgeResult",
    "explain",
    "is_code",
    "relay_url",
    "summary",
    "use_code",
]

RELAY_URL_ENV = "ARCHITECT_RELAY_URL"
# The judges' relay (deploy/relay on Vercel). None means a build that knows no relay.
RELAY_URL: str | None = "https://architect-relay.vercel.app/v1"
CODE = re.compile(r"^rc_[A-Za-z0-9_-]{32}$")
_URL = re.compile(r"^https?://[^\s/]+(/[^\s]*)?$")
_TIMEOUT_S = 20.0  # a relay instance that has to start first answers in a few seconds
_MAX_BALANCE_BYTES = 65_536


def is_code(value: object) -> bool:
    """True for a judge code (``rc_`` and 32 base64url characters)."""
    return isinstance(value, str) and CODE.fullmatch(value.strip()) is not None


def relay_url(env: Mapping[str, str] | None = None) -> str | None:
    """``ARCHITECT_RELAY_URL``, else ``RELAY_URL``, without a trailing slash; None if neither."""
    environ = os.environ if env is None else env
    configured = environ.get(RELAY_URL_ENV, "").strip() or (RELAY_URL or "")
    return configured.rstrip("/") or None


@dataclass(frozen=True)
class JudgeResult:
    ok: bool
    code: str | None = None  # a fixed failure code
    models: tuple[Discovered, ...] = ()
    base_url: str | None = None
    left_today: float | None = None  # dollars, from the relay's balance
    left_total: float | None = None


def _usd(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


async def _balance(base: str, code: str, host: Host) -> tuple[float | None, float | None]:
    """What the code has left today and in all, or (None, None) when the relay will not say."""
    try:
        async with httpx.AsyncClient(
            transport=host.transport, timeout=_TIMEOUT_S, follow_redirects=False, trust_env=False
        ) as client:
            response = await client.get(
                base + "/relay/balance", headers={"Authorization": f"Bearer {code}"}
            )
    except httpx.HTTPError:
        return None, None
    if response.status_code != 200 or len(response.content) > _MAX_BALANCE_BYTES:
        return None, None
    try:
        data = response.json()
    except ValueError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    today = data.get("today")
    left_today = _usd(today.get("remaining_usd")) if isinstance(today, dict) else None
    cap, spent, held = (_usd(data.get(key)) for key in ("cap_total_usd", "spent_usd",
                                                          "in_flight_usd"))
    if cap is None or spent is None or held is None:
        return left_today, None
    return left_today, max(0.0, cap - spent - held)


def _conversation_model(workspace: Path) -> None:
    """Nemotron 3 Super holds the conversation in ``workspace`` (``model`` in its
    ``config.toml``) unless a model is set there already, or the workspace's own profile
    does not list Super. A folder with a profile of its own, such as examples/expense-app,
    asks no first-run question that would set it, and the conversation would run on the
    ladder's first model. A first run on Nemotron that follows sets the model it chose.
    Nothing here stops the code from being used: a file that cannot be read or written is
    left as is."""
    from architect.agent.config import ConfigError, load_config, set_value, workspace_config_path
    from architect.session.bootstrap import SESSION_PROFILE

    from .onboarding import NEBIUS_ROLES

    model = NEBIUS_ROLES["inference"]["model"]
    try:
        if load_config(workspace).text("model") is not None:
            return
        own = workspace / SESSION_PROFILE
        if own.is_file():
            payload = json.loads(own.read_text(encoding="utf-8"))
            models = payload.get("models") if isinstance(payload, dict) else None
            if not isinstance(models, dict) or model not in models:
                return  # its conversation stays on a model it lists
        set_value(workspace_config_path(workspace), "model", model)
    except (ConfigError, OSError, ValueError):
        return


async def use_code(
    code: str,
    workspace: Path,
    *,
    url: str | None = None,
    host: Host | None = None,
    replace: bool = False,
) -> JudgeResult:
    """Check ``code`` at the relay and store it with the relay's URL; never raises.

    ``url`` overrides ``relay_url()``. ``replace`` lets the code take the place of a
    stored Nebius key that is not a code. Nothing is stored unless the relay lists its
    models for this code.
    """
    host = host or Host()
    code = code.strip() if isinstance(code, str) else ""
    if not is_code(code):
        return JudgeResult(False, "invalid_code")
    base = (url or "").strip().rstrip("/") or relay_url(host.env)
    if base is None:
        return JudgeResult(False, "no_relay_url")
    if _URL.fullmatch(base) is None:
        return JudgeResult(False, "invalid_url")
    try:
        httpx.URL(base)  # a host the pattern lets through can still be malformed
    except httpx.InvalidURL:
        return JudgeResult(False, "invalid_url")
    where = credentials.source("nebius", workspace, env=host.env)
    if where is not None and where != "file":
        return JudgeResult(False, "key_in_env")
    stored = credentials.get("nebius", workspace, env={}) if where == "file" else None
    if stored is not None and not replace and not is_code(stored):
        return JudgeResult(False, "key_stored")
    item = find("nebius")
    if item is None:  # the catalog always lists Nebius
        return JudgeResult(False, "unknown_provider")
    found = await list_models(item, base_url=base, api_key=code, host=host, timeout_s=_TIMEOUT_S)
    if not found.ok:
        return JudgeResult(False, found.code, base_url=base)
    try:
        # The key first: a URL stored next to an older key would send that key to the relay.
        credentials.set_key("nebius", code, workspace)
        credentials.set_base_url("nebius", base, workspace)
    except credentials.CredentialError as exc:
        return JudgeResult(False, exc.code, base_url=base)
    _conversation_model(workspace)
    left_today, left_total = await _balance(base, code, host)
    return JudgeResult(True, models=found.models, base_url=base, left_today=left_today,
                       left_total=left_total)


def summary(result: JudgeResult, workspace: Path) -> str:
    """What a stored code means for the user, without the code."""
    count = len(result.models)
    lines = [
        f"The judge code works. It is stored in {credentials.path_for(workspace)} and never "
        "written to a log.",
        f"Nemotron calls now go through the judges' relay at {result.base_url} "
        f"({count} model{'' if count == 1 else 's'}).",
    ]
    if result.left_today is not None and result.left_total is not None:
        lines.append(f"This code has ${result.left_today:.2f} left today (days end at 00:00 UTC) "
                     f"and ${result.left_total:.2f} in all.")
    return "\n".join(lines)


_EXPLAIN = {
    "invalid_code": "That is not a judge code. A judge code is rc_ followed by 32 letters, "
                    "digits, hyphens or underscores.",
    "no_relay_url": f"This copy of Architect does not know the judges' relay address. Set "
                    f"{RELAY_URL_ENV} to it, or pass --url to 'architect judge'.",
    "invalid_url": "The relay address must be an http(s) URL ending in /v1.",
    "key_in_env": "NEBIUS_API_KEY is set in this environment, and Architect uses it before a "
                  "stored key. Unset it to use the judge code, or keep using your own key "
                  "(no code is needed then).",
    "key_stored": "A Nebius key is already stored here. To use the judge code instead, run "
                  "/logout nebius in Architect first.",
    "unauthorized": "The relay refused that code: it is unknown or has been revoked.",
    "unreachable": "The judges' relay did not answer. Check the connection and try again.",
    "timeout": "The judges' relay took too long to answer. Try again in a minute.",
    "write_failed": "The code could not be saved in the workspace; nothing changed.",
}


def explain(code: str | None) -> str:
    """One sentence for a ``use_code`` failure code, a relay refusal's included."""
    return _EXPLAIN.get(code or "") or RELAY_TEXT.get(code or "") or (
        "The judges' relay gave an unexpected answer and nothing was stored. Try again later.")
