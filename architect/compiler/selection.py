"""Stage 1: deterministic, library-first preset selection without evaluation."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from architect.compiler.types import LibraryLookup, NullLibrary
from architect.presets import UnknownPresetError, get_preset
from architect.spec import HarnessArtifact, Spec

__all__ = ["NullLibrary", "Selection", "SelectionError", "select_preset"]


class SelectionError(ValueError):
    """No supported preset or trustworthy warm-start template was found."""


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    preset: str
    warm_start: HarnessArtifact | None = None


def select_preset(
    spec: Spec,
    model: str,
    library: LibraryLookup | None = None,
) -> Selection:
    """Select a template; its old license never authorizes the new specification.

    A library hit must be authentic and belong to the requested model. Invalid
    hits fail closed instead of silently using unverified cache contents. Every
    accepted template still needs tuning and Stage 4 verification for this spec.
    A ``plain`` spec never consults the library: it runs its ``preset_hint``.
    """
    cache = NullLibrary() if library is None else library
    candidate = None if spec.plain else cache.lookup(spec, model)
    if candidate is not None:
        if not candidate.licensed:
            raise SelectionError("Library artifact is not authentically licensed")
        if candidate.model != model:
            raise SelectionError("Library artifact model does not match")
        try:
            get_preset(candidate.preset)
        except UnknownPresetError as exc:
            raise SelectionError("Library artifact preset is unsupported") from exc
        if spec.preset_hint is None or candidate.preset == spec.preset_hint:
            return Selection(preset=candidate.preset, warm_start=candidate)

    if spec.preset_hint is not None:
        return Selection(preset=get_preset(spec.preset_hint).name)

    # Exact domain tags outrank shape, including verifier/planner/recommender.
    domain = {"coding": "coder", "game": "explorer"}.get(spec.domain, spec.domain)
    try:
        preset = get_preset(domain)
    except UnknownPresetError:
        shape = spec.output_schema.get("type")
        if shape == "string":
            return Selection(preset="synthesizer")
        if shape in ("object", "array"):
            return Selection(preset="extractor")
        raise SelectionError("Unsupported domain and output shape") from None
    return Selection(preset=preset.name)
