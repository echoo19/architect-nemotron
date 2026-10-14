"""How a preset reads JSON out of a model reply (``Spec.reply_json``).

Every mode first parses the whole reply strictly, exactly as presets always have, and
only a reply that fails that parse falls back:

- ``strict``: no fallback.
- ``first`` and ``schema``: ``extract_json_text``, the function behind Forge's
  ``extract_json`` (the first closed code fence whose body is or holds an object or
  array, else the first balanced ``{...}`` or ``[...]`` that parses). ``schema`` also
  asks the provider for schema-constrained output; ``_execution`` sends the schema.
- ``last``: ``extract_last_json_text``, the last balanced object or array, for replies
  that think aloud before their answer.

A fallback value is checked like a strict one: JSON values and finite numbers only.
"""

from __future__ import annotations

import json

from pydantic import JsonValue, TypeAdapter

from architect.runtime._sandbox_builtins import (
    PARSE_LIMIT,
    _balanced_end,
    extract_json_text,
    parse_json_text,
)
from architect.spec import ReplyJson

from . import PresetError

__all__ = ["LAST_STARTS", "extract_last_json_text", "parse_reply"]

_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
# Brackets ``extract_last_json_text`` tries before it stops (``first`` tries 16).
LAST_STARTS = 256
_INVALID = "Model response is not valid JSON"


def _reject_constant(value: str) -> None:
    raise PresetError("Non-finite JSON numbers are not supported")


def _checked(value: object) -> JsonValue:
    try:
        checked = _JSON.validate_python(value)
        # Also rejects overflowing JSON floats such as 1e999.
        json.dumps(checked, sort_keys=True, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise PresetError(_INVALID) from exc
    return checked


def _strict(text: str) -> JsonValue:
    try:
        value = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, TypeError) as exc:  # PresetError is a ValueError
        raise PresetError(_INVALID) from exc
    return _checked(value)


def extract_last_json_text(text: str) -> object | None:
    """The last balanced ``{...}`` or ``[...]`` of ``text`` that parses, else None.

    Scans left to right. At each ``{`` or ``[`` it finds the string-aware balanced end;
    a slice that parses strictly to an object or array is kept and the scan resumes
    after it, otherwise the scan moves one character on. At most ``LAST_STARTS``
    brackets are tried, and a text over ``PARSE_LIMIT`` characters gives None.
    """
    if len(text) > PARSE_LIMIT:
        return None
    found: object | None = None
    position = 0
    for _ in range(LAST_STARTS):
        starts = [index for index in (text.find("{", position), text.find("[", position))
                  if index >= 0]
        if not starts:
            break
        start = min(starts)
        end = _balanced_end(text, start)
        if end > 0:
            value = parse_json_text(text[start:end])
            if type(value) in (dict, list):
                found = value
                position = end
                continue
        position = start + 1
    return found


def parse_reply(text: str, mode: ReplyJson) -> JsonValue:
    """The JSON value of a model reply under ``mode``; ``PresetError`` when there is none."""
    if mode not in ("strict", "first", "last", "schema"):
        raise PresetError("Unknown reply mode")
    try:
        return _strict(text)
    except (PresetError, RecursionError):
        if mode == "strict":
            raise  # exactly as presets always failed, a RecursionError included
    found = extract_last_json_text(text) if mode == "last" else extract_json_text(text)
    if found is None:
        raise PresetError(_INVALID)
    return _checked(found)
