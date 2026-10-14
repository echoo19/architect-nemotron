"""Spec editing and blocker arithmetic for the review screen.

Edits work on ``spec.model_dump(mode="json")`` and re-validate the whole Spec strictly
through JSON, so an edit can only ever produce a Spec that the compiler itself would
accept. ``provenance`` cannot be set: it is the one field excluded from ``spec_hash``,
so allowing it would let an edit change the Spec without changing the approval token.
Blockers are computed from the profile the session loaded and from adapter quotes; they
show arithmetic and the exact fixing command, and never apply a suggestion themselves.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import JsonValue, ValidationError

from architect.profiles import Profile
from architect.session.codes import SessionError
from architect.spec import Budget, FloorDefinition, Spec

__all__ = [
    "Blocker",
    "blockers",
    "cents",
    "diff_lines",
    "floor_alternative",
    "normalize_schema",
    "parse_value",
    "schema_missing",
    "set_field",
    "suggested_cost",
    "unset_field",
    "validation_fields",
]

# Fields whose values are open JSON objects: intermediate keys are created on demand.
_OPEN_FIELDS = frozenset({"input_schema", "output_schema", "execution_config"})
# Fields whose values are sub-models with a fixed key set.
_SUBMODELS: Mapping[str, frozenset[str]] = {
    "budget": frozenset(Budget.model_fields),
    "floor": frozenset(FloorDefinition.model_fields),
    "secondary_floor": frozenset(FloorDefinition.model_fields),
}
_SETTABLE = frozenset(Spec.model_fields) - {"provenance"}
# A dict carrying any of these keys is already a JSON Schema, not a bare property map.
_SCHEMA_KEYWORDS = frozenset(
    {"type", "properties", "$schema", "oneOf", "anyOf", "allOf", "enum", "items"}
)
# What "set <field> ..." must look like for a schema the blocker calls missing.
_SCHEMA_SHAPE = '{"type":"object","properties":{...},"required":[...],"additionalProperties":false}'


def normalize_schema(value: JsonValue) -> tuple[JsonValue, bool]:
    """Wrap a bare property map into a JSON Schema object; anything else is unchanged.

    Models sometimes answer ``{"review": {"type": "string"}}`` where a schema is asked
    for. A non-empty dict whose values are all dicts and which carries no JSON Schema
    keyword becomes ``{"type": "object", "properties": <value>, "required": <sorted
    keys>, "additionalProperties": false}``; the flag says whether that happened.
    """
    if (
        not isinstance(value, dict)
        or not value
        or not all(isinstance(child, dict) for child in value.values())
        or any(key in _SCHEMA_KEYWORDS for key in value)
    ):
        return value, False
    required: list[JsonValue] = list(sorted(value))
    wrapped: dict[str, JsonValue] = {
        "type": "object",
        "properties": dict(value),
        "required": required,
        "additionalProperties": False,
    }
    return wrapped, True


def schema_missing(schema: Mapping[str, JsonValue]) -> bool:
    """True for ``{}`` and for a ``type: object`` schema that declares no properties."""
    if not schema:
        return True
    return schema.get("type") == "object" and not schema.get("properties")


def _reject_constant(name: str) -> JsonValue:
    raise ValueError(name)


def parse_value(text: str) -> JsonValue:
    """JSON when the text parses as JSON, else the raw string; empty is an error.

    NaN and Infinity are not JSON and are kept as plain strings so that strict
    validation rejects them for numeric fields instead of smuggling them in.
    """
    if not text.strip():
        raise SessionError("invalid_value_json")
    try:
        parsed: JsonValue = json.loads(text, parse_constant=_reject_constant)
    except ValueError:
        return text
    return parsed


def validation_fields(exc: ValidationError) -> tuple[str, ...]:
    """Dotted field paths from a ValidationError; never the offending values."""
    seen: dict[str, None] = {}
    for error in exc.errors(include_url=False, include_input=False):
        seen.setdefault(".".join(str(part) for part in error["loc"]), None)
    return tuple(seen)


def _validate(dump: dict[str, JsonValue]) -> Spec:
    try:
        return Spec.model_validate_json(json.dumps(dump, allow_nan=False), strict=True)
    except ValidationError as exc:
        raise SessionError("invalid_edit", fields=validation_fields(exc)) from None
    except ValueError:
        raise SessionError("invalid_edit") from None


def _dump(spec: Spec) -> dict[str, JsonValue]:
    dumped: dict[str, JsonValue] = spec.model_dump(mode="json")
    return dumped


def _mark_user(dump: dict[str, JsonValue], top: str) -> None:
    provenance = dump.get("provenance")
    if not isinstance(provenance, dict):
        provenance = {}
    provenance[top] = "user"
    dump["provenance"] = provenance


def set_field(spec: Spec, path: str, value: JsonValue) -> tuple[Spec, dict[str, JsonValue]]:
    """Set one dotted field and return the new Spec plus the top-level override.

    The override dict is what ``Draft.user_fields`` carries into re-inference, so a
    revise keeps the operator's choices even when the model proposes otherwise.
    """
    parts = path.split(".")
    top = parts[0]
    if top not in _SETTABLE or any(not part for part in parts):
        raise SessionError("unknown_field")
    dump = _dump(spec)
    if len(parts) == 1:
        dump[top] = value
    elif top in _OPEN_FIELDS:
        node = dump[top]
        if not isinstance(node, dict):
            node = {}
            dump[top] = node
        for part in parts[1:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[parts[-1]] = value
    elif top in _SUBMODELS:
        # Sub-models have a fixed key set; anything else (budget.tokens, floor.x.y)
        # is not a field rather than an invalid value.
        if len(parts) != 2 or parts[1] not in _SUBMODELS[top]:
            raise SessionError("unknown_field")
        node = dump[top]
        if not isinstance(node, dict):
            node = {}
            dump[top] = node
        node[parts[1]] = value
    else:
        raise SessionError("unknown_field")
    _mark_user(dump, top)
    new_spec = _validate(dump)
    return new_spec, {top: _dump(new_spec)[top]}


def unset_field(spec: Spec, path: str) -> tuple[Spec, dict[str, JsonValue]]:
    """Reset one top-level field to its Spec default; required fields cannot be unset."""
    if path not in _SETTABLE:
        raise SessionError("unknown_field")
    info = Spec.model_fields[path]
    if info.is_required():
        raise SessionError("invalid_edit", fields=(path,))
    default = info.get_default(call_default_factory=True)
    dump = _dump(spec)
    dump[path] = json.loads(json.dumps(default, default=_jsonable))
    _mark_user(dump, path)
    new_spec = _validate(dump)
    return new_spec, {path: _dump(new_spec)[path]}


def _jsonable(value: object) -> JsonValue:
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        return str(value)
    raise TypeError("Not JSON")


def _leaves(node: JsonValue, prefix: str, into: dict[str, str]) -> None:
    if isinstance(node, dict) and node:
        for key, child in node.items():
            _leaves(child, f"{prefix}.{key}" if prefix else key, into)
        return
    into[prefix] = json.dumps(node, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def diff_lines(before: Spec, after: Spec) -> list[str]:
    """One "- path value" / "+ path value" pair per changed dotted leaf (provenance excluded)."""
    old: dict[str, str] = {}
    new: dict[str, str] = {}
    _leaves(before.model_dump(mode="json", exclude={"provenance"}), "", old)
    _leaves(after.model_dump(mode="json", exclude={"provenance"}), "", new)
    lines: list[str] = []
    for key in dict.fromkeys((*old, *new)):
        if key in old and key in new and old[key] == new[key]:
            continue
        if key in old:
            lines.append(f"- {key} {old[key]}")
        if key in new:
            lines.append(f"+ {key} {new[key]}")
    return lines


@dataclass(frozen=True)
class Blocker:
    code: str
    fix: str
    blocking: bool
    alternative: str | None = None  # a second exact command, printed as "or: ..."


def floor_alternative(profile: Profile) -> str | None:
    """``set floor.dataset <name>`` for the configured floors, or None when there are none.

    One floor is an exact command; several are listed as ``<a|b>`` for the operator to
    pick from. Names come from the trusted profile file, never from a model.
    """
    names = list(profile.floors)
    if not names:
        return None
    choice = names[0] if len(names) == 1 else "<" + "|".join(names) + ">"
    return f"set floor.dataset {choice}"


def _number(value: float) -> str:
    """Command-line friendly number: no trailing zeros, never scientific."""
    if value == int(value):
        return str(int(value))
    return f"{value:.4f}".rstrip("0")


def cents(value: float) -> str:
    """A suggested budget as the operator will type it: always two decimals ("0.70")."""
    return f"{value:.2f}"


def suggested_cost(min_call_cost: float, calls: int) -> float:
    """Ceiling to cents so the suggestion is never below the reservation it covers."""
    return math.ceil(min_call_cost * calls * 100) / 100


def blockers(spec: Spec, profile: Profile, min_call_cost: float | None) -> tuple[Blocker, ...]:
    """Everything that would make approve or compile fail, with its exact fix, in order."""
    found: list[Blocker] = []
    for field in ("input_schema", "output_schema"):
        if schema_missing(getattr(spec, field)):
            found.append(Blocker(f"{field}_missing", f"set {field} {_SCHEMA_SHAPE}", True))
    floor = profile.floors.get(spec.floor.dataset)
    if floor is None:
        found.append(Blocker("floor_not_configured", "floor", True, floor_alternative(profile)))
    elif floor.kind != spec.floor.kind:
        found.append(Blocker("floor_kind_mismatch", f"set floor.kind {floor.kind.value}", True))
    secondary = spec.secondary_floor
    if secondary is not None:
        configured = profile.floors.get(secondary.dataset)
        if configured is None or configured.kind != secondary.kind:
            found.append(Blocker("secondary_floor_not_configured", "unset secondary_floor", True))
    ladder_ok = spec.model_target == "cheapest that passes" and bool(profile.compile.model_ladder)
    if spec.model_target not in profile.models and not ladder_ok:
        first = next(iter(profile.models))
        found.append(Blocker("model_not_configured", f"set model_target {first}", True))
    if min_call_cost is not None and spec.budget.cost_usd < min_call_cost:
        cost = cents(suggested_cost(min_call_cost, 2))
        found.append(Blocker("budget_below_quote", f"set budget.cost_usd {cost}", True))
    compile_budget = profile.compile.compile_budget
    if compile_budget.compute_s < 2 * spec.budget.compute_s:
        seconds = _number(4 * spec.budget.compute_s)
        fix = f"budget compile {_number(compile_budget.cost_usd)} {seconds}"
        found.append(Blocker("compile_deadline_short", fix, False))
    return tuple(found)
