"""Refusals from the judges' relay (``deploy/relay``), each a fixed code with one sentence.

The relay answers a call it will not make with JSON in the OpenAI error shape,
``{"error": {"type", "code", ...}}`` (deploy/relay/README.md, "Errors"). The refusals a
person can act on are mapped here and nowhere else: a code's daily or total limit, the
shared budget, an unknown, revoked or paused code, the kill switch, and the relay's
ledger out of reach. ``refusal`` reads only the status and those two fields, matched
against a closed table, so no text from the relay or the provider reaches a message.

An adapter raises ``RelayRefusal`` (an ``AdapterError`` whose text is the sentence) for
one; ``session.codes.code_for`` gives its code, ``explain`` its sentence, and the CLI and
the desktop show that sentence. A compile counts a refused call as one failed case and
goes on, so build mode and the desktop's Try it also ask ``since(mark())`` whether a
refusal happened while they ran.

The ledger refusal is the one an adapter retries (``OUTAGES``): it says the relay itself
cannot serve anyone for now, which may pass within seconds. It is raised only once the
adapter's retries are spent, and build mode then stops its compile (``on_outage``), since
every later call would wait out the same retries and fail as well.

``Unreachable`` is the same for an address that does not answer at all (no connection,
a wrong address, nothing listening), the relay's or Token Factory's alike: the code
``provider_unreachable`` and one sentence that names the host, never a path or a key.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Iterator, Mapping
from contextvars import ContextVar
from types import MappingProxyType
from urllib.parse import urlsplit

from .types import AdapterError

__all__ = [
    "CODES",
    "OUTAGES",
    "PLAIN",
    "TEXT",
    "UNREACHABLE",
    "RelayRefusal",
    "Unreachable",
    "address",
    "mark",
    "on_outage",
    "refusal",
    "sentence",
    "since",
]

# (HTTP status, error type, error code) -> refusal code; a None code matches any.
_TABLE: Mapping[tuple[int, str, str | None], str] = MappingProxyType(
    {
        (402, "relay_cap_exceeded", "daily_cap"): "relay_daily_limit",
        (402, "relay_cap_exceeded", "total_cap"): "relay_total_limit",
        (402, "relay_cap_exceeded", "global_cap"): "relay_shared_limit",
        (402, "relay_cap_exceeded", "global_freeze"): "relay_shared_limit",
        (503, "upstream_budget", None): "relay_shared_limit",
        (401, "relay_unknown_code", "missing_code"): "relay_unknown_code",
        (401, "relay_unknown_code", "unknown_code"): "relay_unknown_code",
        (401, "relay_unknown_code", "code_revoked"): "relay_code_revoked",
        (403, "relay_code_frozen", "code_frozen"): "relay_code_paused",
        (503, "relay_disabled", "disabled"): "relay_off",
        (503, "relay_ledger_unavailable", None): "relay_ledger_down",
    }
)
TEXT: Mapping[str, str] = MappingProxyType(
    {
        "relay_daily_limit": (
            "This judge code has reached its daily limit. The day resets at 00:00 UTC; "
            "until then, use a spare code."
        ),
        "relay_total_limit": "This judge code has reached its total limit. Use a spare code.",
        "relay_shared_limit": (
            "The judges' relay has no shared budget left for this call, although your code "
            "has money left. Try again later."
        ),
        "relay_unknown_code": (
            "The judges' relay does not know this code. Check that the whole code was "
            "pasted, or use a spare."
        ),
        "relay_code_revoked": "This judge code has been revoked. Use a spare code.",
        "relay_code_paused": (
            "This judge code is paused because one call cost more than the relay set aside "
            "for it. Use a spare code."
        ),
        "relay_off": (
            "The judges' relay is switched off for now and refuses model calls. Try again "
            "later."
        ),
        "relay_ledger_down": (
            "The judges' relay cannot reach its ledger right now, so it made no model call "
            "and charged nothing. Try again later, with this code or a spare."
        ),
    }
)
CODES = frozenset(TEXT)
# Refusals that say the relay cannot serve anyone for now, whatever the code. An adapter
# retries one as it retries a busy server, so a short blip passes, and raises it once
# those retries are spent.
OUTAGES = frozenset({"relay_ledger_down"})
UNREACHABLE = "provider_unreachable"
# Codes that come with one sentence to show as it is (``sentence``).
PLAIN = CODES | {UNREACHABLE}
_UNREACHABLE_TEXT = "Architect could not reach {}; check the connection and try again."
_STATUSES = frozenset(status for status, _, _ in _TABLE)
_MAX_BODY = 65_536


class _Seen:
    """How many refusals and unreachable addresses this process has seen, the last one's
    code, and the last unreachable address's sentence."""

    count = 0
    last: str | None = None
    unreachable: str | None = None


_SEEN = _Seen()


def _seen(code: str) -> None:
    _SEEN.count += 1
    _SEEN.last = code


def refusal(status: int, body: bytes) -> str | None:
    """The refusal code for a relay answer (its status and JSON body), else None."""
    if status not in _STATUSES or len(body) > _MAX_BODY:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return None
    kind, code = error.get("type"), error.get("code")
    if not isinstance(kind, str):
        return None
    if isinstance(code, str) and (status, kind, code) in _TABLE:
        return _TABLE[(status, kind, code)]
    return _TABLE.get((status, kind, None))


class RelayRefusal(AdapterError):
    """The relay refused a call; ``code`` is a member of ``CODES``, ``str()`` its sentence."""

    def __init__(self, code: str) -> None:
        if code not in TEXT:
            raise ValueError("Unknown relay refusal")
        super().__init__(TEXT[code])
        self.code = code
        _seen(code)
        heard = _ON_OUTAGE.get() if code in OUTAGES else None
        if heard is not None:
            with contextlib.suppress(Exception):  # a listener never changes what is raised
                heard(self)


_ON_OUTAGE: ContextVar[Callable[[RelayRefusal], None] | None] = ContextVar(
    "relay_on_outage", default=None
)


@contextlib.contextmanager
def on_outage(heard: Callable[[RelayRefusal], None]) -> Iterator[None]:
    """Pass ``heard`` each ``OUTAGES`` refusal as it is raised in this block, or in a task
    started in it (a task keeps the context it started with). Build mode stops its
    compile at the first."""
    token = _ON_OUTAGE.set(heard)
    try:
        yield
    finally:
        _ON_OUTAGE.reset(token)


def address(base_url: str) -> str:
    """The host of ``base_url``, with its port when it names one: what a person can check."""
    try:
        parts = urlsplit(base_url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return "the model's address"
    if not host:
        return "the model's address"
    return f"{host}:{port}" if port is not None else host


class Unreachable(AdapterError):
    """No connection to a provider's address; ``str()`` is one sentence naming its host."""

    code = UNREACHABLE

    def __init__(self, base_url: str) -> None:
        self.host = address(base_url)
        text = _UNREACHABLE_TEXT.format(self.host)
        super().__init__(text)
        _SEEN.unreachable = text
        _seen(UNREACHABLE)


def sentence(code: str) -> str:
    """The sentence for a code in ``PLAIN`` (for ``provider_unreachable``, the last one
    said, which names the host); "" for any other code."""
    if code in TEXT:
        return TEXT[code]
    if code == UNREACHABLE:
        return _SEEN.unreachable or _UNREACHABLE_TEXT.format("the model's address")
    return ""


def mark() -> int:
    """How many refusals and unreachable addresses this process has seen so far; pass it
    to ``since`` later."""
    return _SEEN.count


def since(start: int) -> str | None:
    """The code of the last refusal or unreachable address seen after ``start`` (from
    ``mark``), else None."""
    return _SEEN.last if _SEEN.count > start else None
