"""A model reply shaped like a spec's output schema: ``forge_dryrun``'s default reply.

A dry run scripts every model reply. The old default, ``{}``, fails any harness that
validates the model's answer against an object schema, so a sound starting harness
(the instantiated incumbent) looked broken: ``sandbox_worker_failed`` on its first dry
run, live on 2026-10-03. ``example_reply`` builds a small plain value the schema
accepts instead, and returns None whenever it cannot show that the schema does.
"""

from __future__ import annotations

import json
from typing import Final

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import JsonValue

__all__ = ["REPLY_CHARS", "example_reply", "example_texts"]

REPLY_CHARS: Final = 2000  # the same bound as a scripted reply the agent passes itself
_DEPTH: Final = 12
_NONE: Final = object()  # no value could be built
_FORMATS: Final[dict[str, str]] = {
    "date": "2024-01-31",
    "date-time": "2024-01-31T12:00:00Z",
    "time": "12:00:00Z",
    "email": "name@example.com",
    "hostname": "example.com",
    "ipv4": "192.0.2.1",
    "ipv6": "2001:db8::1",
    "uri": "https://example.com/",
    "uuid": "00000000-0000-4000-8000-000000000000",
}
_TEXT: Final = "example"  # a string with no known format
_PADDING: Final = "x"  # repeated up to a string's minLength
# A value that shows the shape comes first: an object or a number before null.
_TYPE_ORDER: Final = ("object", "array", "string", "integer", "number", "boolean", "null")


def example_texts() -> dict[str, str]:
    """Every fixed text an example reply can hold, by name: Forge's pack covers them
    (``architect.compiler.forge_pack``), since the engineer reads them in a dry run."""
    return {
        "string": _TEXT,
        "padding": _PADDING,
        **{f"format/{name}": text for name, text in _FORMATS.items()},
    }


def _bound(schema: dict[str, object], key: str) -> float | None:
    value = schema.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _resolve(root: dict[str, JsonValue], pointer: str) -> object:
    if pointer == "#":
        return root
    if not pointer.startswith("#/"):
        return _NONE
    current: object = root
    for raw in pointer[2:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or token not in current:
            return _NONE
        current = current[token]
    return current


def _number(schema: dict[str, object], integer: bool) -> object:
    value = 0.0
    low, high = _bound(schema, "minimum"), _bound(schema, "maximum")
    above, below = _bound(schema, "exclusiveMinimum"), _bound(schema, "exclusiveMaximum")
    if low is not None and value < low:
        value = low
    if above is not None and value <= above:
        value = above + 1
    if high is not None and value > high:
        value = high
    if below is not None and value >= below:
        value = below - 1
    step = _bound(schema, "multipleOf")
    if step is not None and step > 0 and value % step:
        value = (value // step + 1) * step
    if value.is_integer():
        return int(value)
    return _NONE if integer else value


def _string(schema: dict[str, object]) -> str:
    form = schema.get("format")
    text = _FORMATS.get(form, _TEXT) if isinstance(form, str) else _TEXT
    low, high = schema.get("minLength"), schema.get("maxLength")
    if isinstance(high, int) and len(text) > high:
        text = text[:high]
    if isinstance(low, int) and len(text) < low:
        text += _PADDING * (low - len(text))
    return text


def _typed(kind: str, schema: dict[str, object], root: dict[str, JsonValue], depth: int) -> object:
    if kind == "object":
        properties = schema.get("properties")
        declared = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        names = list(declared)
        if isinstance(required, list):
            names += [name for name in required if isinstance(name, str) and name not in declared]
        found: dict[str, object] = {}
        for name in names:
            item = _example(declared.get(name, True), root, depth + 1)
            if item is _NONE:
                return _NONE
            found[name] = item
        return found
    if kind == "array":
        items = schema.get("items", True)
        prefix = schema.get("prefixItems")
        values = [_example(child, root, depth + 1) for child in prefix] if isinstance(
            prefix, list
        ) else []
        low, high = schema.get("minItems"), schema.get("maxItems")
        wanted = max(low if isinstance(low, int) else 0, 0 if items is False else 1)
        if isinstance(high, int):
            wanted = min(wanted, high)
        while len(values) < wanted:
            values.append(_example(items, root, depth + 1))
        return _NONE if any(item is _NONE for item in values) else values
    if kind == "string":
        return _string(schema)
    if kind in ("integer", "number"):
        return _number(schema, kind == "integer")
    if kind == "boolean":
        return False
    if kind == "null":
        return None
    return _NONE


def _order(kind: str) -> int:
    return _TYPE_ORDER.index(kind) if kind in _TYPE_ORDER else len(_TYPE_ORDER)


def _example(schema: object, root: dict[str, JsonValue], depth: int) -> object:
    if depth > _DEPTH or schema is False:
        return _NONE
    if schema is True:
        return None
    if not isinstance(schema, dict):
        return _NONE
    reference = schema.get("$ref")
    if isinstance(reference, str):
        return _example(_resolve(root, reference), root, depth + 1)
    if "const" in schema:
        return schema["const"]
    choices = schema.get("enum")
    if isinstance(choices, list) and choices:
        return choices[0]
    for key in ("anyOf", "oneOf", "allOf"):
        branches = schema.get(key)
        if isinstance(branches, list) and branches:
            for branch in branches:
                value = _example(branch, root, depth + 1)
                if value is not _NONE:
                    return value
            return _NONE
    kind = schema.get("type")
    kinds = [kind] if isinstance(kind, str) else [k for k in kind if isinstance(k, str)] if (
        isinstance(kind, list)
    ) else []
    if not kinds and ("properties" in schema or "required" in schema):
        kinds = ["object"]
    if not kinds and ("items" in schema or "prefixItems" in schema):
        kinds = ["array"]
    if not kinds:
        return None  # any value: null is the plainest
    for name in sorted(kinds, key=_order):
        value = _typed(name, schema, root, depth)
        if value is not _NONE:
            return value
    return _NONE


def example_reply(schema: dict[str, JsonValue]) -> str | None:
    """The reply text a model would send for a valid output, or None.

    A string schema's reply is the text itself (presets take it as is); any other schema
    is answered as JSON. The value is checked against the schema, formats included, and
    a reply longer than ``REPLY_CHARS`` is not returned.
    """
    value = _example(schema, schema, 0)
    if value is _NONE:
        return None
    try:
        if not Draft202012Validator(schema, format_checker=FormatChecker()).is_valid(value):
            return None
        if schema.get("type") == "string" and isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except Exception:
        return None
    return text if 0 < len(text) <= REPLY_CHARS else None
