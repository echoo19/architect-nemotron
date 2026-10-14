"""Rule checks a spec declares, run in code on each output a preset reads from a reply.

A spec may declare ``execution_config["checks"]``: a list of checks that
``_execution.execute`` runs, in the declared order, on the value it parses from a model's
reply, before that value is checked against the output schema. Each check is one JSON
object. ``check`` names one of the generic checks below, ``rule`` is the plain rule
sentence the plan card shows, ``input`` names the input field that holds the printed
text, and the other keys name the output fields and the lines that may print them.

Every word of a task (its field names and the words its lines print) lives in the
declaration; this module holds only generic logic. ``execution_config`` is part of
``spec_hash`` and of the spec literal in an emitted ``harness.py``, so the license covers
the checks, and a build, its exam, Try it, ``/run`` and an export run the same ones.

A printed amount is a run of digits, dots and commas that starts with a digit, and its
value is all of its digits read as one integer. Patterns match one line at a time and
ignore case.

- ``keep_digits``: a value of one of ``fields`` that equals no printed amount, but equals
  a printed amount that ends in two decimals once those are dropped, takes every digit of
  the first such amount.
- ``not_copied``: a value of ``field`` equal to the value of ``copy_of`` becomes null
  unless a line that may print ``field`` holds an amount of that value. A line may print
  it when it matches ``named``; when it matches ``followed.line`` but not ``followed.by``
  and a later line matches ``followed.by``; or, with ``bare_amount``, when it holds an
  amount and no letter from A to Z.

A value that is not an object is left as it is, and ``keep_digits`` leaves a field that
is null, an object or a list; the output schema decides about them.

A declaration must fit its spec (``check_declaration``): one preset's reply, never a
pipeline's or a game's, and only input and output fields the spec's schemas name, so a
misspelled field is refused when the preset is built instead of never applying.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from architect.spec import Spec

from . import PresetError

__all__ = [
    "CHECKS_KEY",
    "Check",
    "Followed",
    "KeepDigits",
    "NotCopied",
    "apply_check",
    "bind",
    "check_declaration",
    "declared",
    "rule_sentences",
]

CHECKS_KEY = "checks"

_AMOUNT = re.compile(r"\d[\d.,]*")
_TWO_DECIMALS = re.compile(r"^(.*\d)[.,](\d\d)$")
_LETTER = re.compile(r"[A-Za-z]")
_DIGIT = re.compile(r"\d")
_NOT_DIGIT = re.compile(r"\D")

Name = Annotated[str, StringConstraints(min_length=1, max_length=256, pattern=r"\S")]
Rule = Annotated[str, StringConstraints(min_length=1, max_length=400, pattern=r"\S")]
Regex = Annotated[str, StringConstraints(min_length=1, max_length=4096)]


def _compile(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


def _valid(pattern: str) -> str:
    try:
        _compile(pattern)
    except re.error as exc:
        raise ValueError("not a valid regular expression") from exc
    return pattern


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Followed(_Model):
    """A line matching ``line`` but not ``by``, that a later line matching ``by`` follows."""

    line: Regex
    by: Regex

    @field_validator("line", "by")
    @classmethod
    def valid_pattern(cls, value: str) -> str:
        return _valid(value)


class KeepDigits(_Model):
    check: Literal["keep_digits"]
    rule: Rule
    input: Name
    fields: Annotated[tuple[Name, ...], Field(min_length=1, max_length=64)]


class NotCopied(_Model):
    check: Literal["not_copied"]
    rule: Rule
    input: Name
    field: Name
    copy_of: Name
    named: Regex | None = None
    followed: Followed | None = None
    bare_amount: Annotated[bool, Field(strict=True)] = False

    @field_validator("named")
    @classmethod
    def valid_pattern(cls, value: str | None) -> str | None:
        return None if value is None else _valid(value)


Check = Annotated[KeepDigits | NotCopied, Field(discriminator="check")]
_DECLARATION: TypeAdapter[tuple[Check, ...]] = TypeAdapter(
    Annotated[tuple[Check, ...], Field(max_length=16)]
)


def declared(config: Mapping[str, JsonValue]) -> tuple[Check, ...]:
    """The checks ``execution_config`` declares, in order; () when it declares none.

    A declaration that is present but not a list of valid checks is ``PresetError``.
    """
    raw = config.get(CHECKS_KEY)
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise PresetError("Invalid checks declaration")
    try:
        return _DECLARATION.validate_python(raw)
    except ValidationError as exc:
        raise PresetError("Invalid checks declaration") from exc


def check_declaration(spec: Spec) -> tuple[Check, ...]:
    """The checks ``spec`` declares, once they fit it; ``PresetError`` when they do not.

    Checks run on one preset's reply, never on a pipeline or a game, and each names only
    an input field and output fields that the spec's schemas have.
    """
    checks = declared(spec.execution_config)
    if checks and (
        spec.pipeline or spec.execution_config.get("kind") in {"game", "competition"}
    ):
        raise PresetError("Declared checks need a single preset, not a pipeline or a game")
    inputs = spec.input_schema.get("properties")
    outputs = spec.output_schema.get("properties")
    for check in checks:
        names = check.fields if isinstance(check, KeepDigits) else (check.field, check.copy_of)
        if not isinstance(inputs, dict) or check.input not in inputs:
            raise PresetError("A declared check reads an input field the spec does not have")
        if not isinstance(outputs, dict) or any(name not in outputs for name in names):
            raise PresetError("A declared check names an output field the spec does not have")
    return checks


def rule_sentences(config: Mapping[str, JsonValue]) -> tuple[str, ...]:
    """Each declared check's rule as the plan card shows it, ending with a stop."""
    return tuple(
        rule if rule.endswith((".", "!", "?")) else rule + "."
        for rule in (check.rule.strip() for check in declared(config))
    )


def _value(token: str) -> int | None:
    return int(_NOT_DIGIT.sub("", token)) if _DIGIT.search(token) else None


def _amounts(line: str) -> list[str]:
    return _AMOUNT.findall(line)


def _keep_digits(check: KeepDigits, text: str, out: dict[str, JsonValue]) -> dict[str, JsonValue]:
    tokens = [token for line in text.split("\n") for token in _amounts(line)]
    printed = {_value(token) for token in tokens}
    out = dict(out)
    for name in check.fields:
        value = out.get(name)
        if value is None or isinstance(value, (dict, list)) or value in printed:
            continue
        for token in tokens:
            match = _TWO_DECIMALS.match(token)
            if match and _value(match.group(1)) == value:
                out[name] = _value(token)
                break
    return out


def _printing_lines(check: NotCopied, text: str) -> list[str]:
    """The lines of ``text`` that may print ``check.field``."""
    named = _compile(check.named) if check.named is not None else None
    followed = (
        (_compile(check.followed.line), _compile(check.followed.by))
        if check.followed is not None
        else None
    )
    lines = text.split("\n")
    kept: list[str] = []
    for index, line in enumerate(lines):
        if named is not None and named.search(line):
            kept.append(line)
        elif (
            followed is not None
            and followed[0].search(line)
            and not followed[1].search(line)
            and any(followed[1].search(later) for later in lines[index + 1 :])
        ):
            kept.append(line)
        elif check.bare_amount and not _LETTER.search(line) and _amounts(line):
            kept.append(line)
    return kept


def _not_copied(check: NotCopied, text: str, out: dict[str, JsonValue]) -> dict[str, JsonValue]:
    value = out.get(check.field)
    if value is None or value != out.get(check.copy_of):
        return out
    printed = any(
        _value(token) == value
        for line in _printing_lines(check, text)
        for token in _amounts(line)
    )
    return out if printed else {**out, check.field: None}


def apply_check(check: Check, text: str, value: JsonValue) -> JsonValue:
    """One check on one value, given the printed text; a non-object is left as it is."""
    if not isinstance(value, dict):
        return value
    if isinstance(check, KeepDigits):
        return _keep_digits(check, text, value)
    return _not_copied(check, text, value)


def bind(checks: Sequence[Check], task: JsonValue) -> Callable[[JsonValue], JsonValue]:
    """``checks`` bound to the printed text of one task, read before any model call.

    Each check's ``input`` must name a text field of the task, else ``PresetError``.
    """
    texts: list[str] = []
    for check in checks:
        text = task.get(check.input) if isinstance(task, dict) else None
        if not isinstance(text, str):
            raise PresetError("A declared check needs its input field as text")
        texts.append(text)
    bound = tuple(zip(checks, texts, strict=True))

    def run(value: JsonValue) -> JsonValue:
        for check, text in bound:
            value = apply_check(check, text, value)
        return value

    return run
