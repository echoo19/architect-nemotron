"""Forge template library: sandbox language v2 reference harnesses (Phase 9, Section 8.4).

Templates are data, never imported as harness code. ``instantiate`` substitutes the
exact binding literals of a Stage 2 candidate, so the result validates under v2
for any spec of the template's kind. A template is the Forge agent's first draft
and the whole of the agent-free ``template`` mode.
"""

from __future__ import annotations

import json
import re
from typing import Final

from architect.compiler.forge_settings import TemplateName
from architect.compiler.types import CandidateEvaluation
from architect.spec import Spec

from . import extract_verify, game_explorer, game_hypothesis, prompt_chain, vision_two_pass

__all__ = [
    "PLACEHOLDERS",
    "TEMPLATE_NAMES",
    "default_template",
    "instantiate",
    "template_kind",
    "template_source",
]

TEMPLATE_NAMES: Final[tuple[TemplateName, ...]] = (
    "prompt_chain",
    "extract_verify",
    "vision_two_pass",
    "game_explorer",
    "game_hypothesis",
)
PLACEHOLDERS: Final = ("$PRESET_BINDING", "$TASK_TEXT", "$OUTPUT_SCHEMA")
_SOURCES: Final[dict[TemplateName, str]] = {
    "prompt_chain": prompt_chain.SOURCE,
    "extract_verify": extract_verify.SOURCE,
    "vision_two_pass": vision_two_pass.SOURCE,
    "game_explorer": game_explorer.SOURCE,
    "game_hypothesis": game_hypothesis.SOURCE,
}
_GAME_KINDS: Final = frozenset({"game", "competition"})
_PLACEHOLDER: Final = re.compile(r"\$(?:PRESET_BINDING|TASK_TEXT|OUTPUT_SCHEMA)")


def template_source(name: TemplateName) -> str:
    """The raw template text, placeholders included."""
    try:
        return _SOURCES[name]
    except KeyError:
        raise ValueError("Unknown forge template") from None


def _has_images(spec: Spec) -> bool:
    return '"$image"' in json.dumps(spec.input_schema, sort_keys=True)


def template_kind(name: TemplateName) -> str:
    """``game``, ``image`` or ``text``: the spec kind a template targets."""
    if name in ("game_explorer", "game_hypothesis"):
        return "game"
    if name == "vision_two_pass":
        return "image"
    return "text"


def default_template(spec: Spec) -> TemplateName:
    """Game kinds explore; image inputs take two passes; everything else self-checks."""
    if spec.execution_config.get("kind") in _GAME_KINDS:
        return "game_explorer"
    if _has_images(spec):
        return "vision_two_pass"
    return "extract_verify"


def instantiate(name: TemplateName, candidate: CandidateEvaluation) -> str:
    """Substitute the candidate's exact ``execute_preset`` binding and spec literals.

    The literals are Python ``repr`` of JSON values, exactly as preset emission
    writes them, so the sandbox binding check compares them byte-for-byte.
    """
    artifact = candidate.artifact
    spec_json = artifact.spec.model_dump(mode="json")
    binding = (
        f"{artifact.preset!r}, {artifact.params!r}, {tuple(artifact.components)!r}, "
        f"{spec_json!r}"
    )
    values = {
        "$PRESET_BINDING": binding,
        "$TASK_TEXT": repr(artifact.spec.task),
        "$OUTPUT_SCHEMA": repr(artifact.spec.output_schema),
    }
    # One pass: substituted text is never scanned again for placeholders.
    return _PLACEHOLDER.sub(lambda match: values[match.group(0)], template_source(name))
