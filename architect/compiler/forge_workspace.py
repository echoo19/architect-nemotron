"""The throwaway Forge workspace the Pi agent edits (Phase 9, Section 8.3).

Everything written here is visible to the agent. It holds only the parent spec
summary, tuning cases (never held-out ones), the Stage 2 baseline measurement
without failure codes, the language reference and the instantiated templates.
No dataset path, case id, case metadata, image bytes or held-out count is
written, and the forge-private candidate store lives in a different directory.
"""

from __future__ import annotations

import importlib
import json
from collections import Counter
from collections.abc import Coroutine, Sequence
from pathlib import Path
from typing import Any, Final, cast

from pydantic import JsonValue

from architect.compiler.forge_settings import ForgeSettings, TemplateName
from architect.compiler.forge_templates import (
    TEMPLATE_NAMES,
    default_template,
    instantiate,
    template_source,
)
from architect.compiler.types import CandidateEvaluation
from architect.floors import EvaluationCase, FloorEvaluator
from architect.games.execution import offered_actions
from architect.games.local import LocalGameProvider
from architect.games.types import GameLimits, GameRef, Observation
from architect.spec import HarnessArtifact, Spec

from .forge_design import DESIGN_TEXT

__all__ = [
    "TUNING_MAX_BYTES",
    "WORKSPACE_FILES",
    "incumbent_source",
    "language_text",
    "prepare_workspace",
    "spec_markdown",
    "tuning_markdown",
]

WORKSPACE_FILES: Final = (
    "harness.py",
    "LANGUAGE.md",
    "DESIGN.md",
    "SPEC.md",
    "TUNING.md",
    "BASELINE.json",
    "NOTES.md",
)
_SOURCE_LIMIT: Final = 64 * 1024
# TUNING.md is read early and re-sent every turn: a size bound keeps that affordable. It
# also fits in one read: a Forge tool result is cut at 16,384 characters, with no marker.
TUNING_MAX_BYTES: Final = 16_000
_TUNING_RESERVE: Final = 160  # room for the longer header line when cases are left out
_LANGUAGE_FALLBACK: Final = (
    "# Sandbox language v2\n\n"
    "The language reference is unavailable in this build. Use forge_check: every\n"
    "rejection returns a fixed code, a line number and a one-line hint.\n"
)
_NO_SUMMARY: Final = "first observation not available offline; use forge_dryrun (unscored)"


def language_text() -> str:
    """``language_markdown()`` from the sandbox language table, when that module exists."""
    try:
        module = importlib.import_module("architect.runtime.sandbox_language")
    except ImportError:
        return _LANGUAGE_FALLBACK
    render = getattr(module, "language_markdown", None)
    if not callable(render):
        return _LANGUAGE_FALLBACK
    text = render()
    return text if isinstance(text, str) else _LANGUAGE_FALLBACK


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n... [truncated]"


def _image_view(value: JsonValue) -> JsonValue:
    """Replace image literals and stubs by content-free metadata; never pixels or bytes."""
    if isinstance(value, list):
        return [_image_view(item) for item in value]
    if not isinstance(value, dict):
        return value
    body = value.get("$image")
    if isinstance(body, dict):
        try:
            from architect.presets.images import lift_images

            _, refs = lift_images({"$image": body})
            ref = refs[0]
            return {
                "$image": {
                    "sha256": ref.sha256[:12],
                    "media_type": ref.media_type,
                    "width": ref.width,
                    "height": ref.height,
                }
            }
        except Exception:
            return {"$image": "unreadable"}
    return {key: _image_view(item) for key, item in value.items()}


def _game_refs(value: JsonValue) -> list[GameRef]:
    """Every well-formed game reference inside a task, deduplicated and sorted by id."""
    found: dict[str, GameRef] = {}
    pending: list[JsonValue] = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if isinstance(item.get("game_id"), str):
                try:
                    ref = GameRef.model_validate(item)
                except ValueError:
                    pass
                else:
                    found[ref.model_dump_json()] = ref
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return sorted(found.values(), key=lambda ref: (ref.game_id, ref.backend, ref.revision))


def _settle[T](step: Coroutine[Any, Any, T]) -> T | None:
    """Run a coroutine that never suspends; ``None`` (after closing it) if it would."""
    try:
        step.send(None)
    except StopIteration as done:
        return cast(T, done.value)
    step.close()
    return None


def _first_observation(ref: GameRef) -> Observation | None:
    """The first observation of a local fixture game, opened without a runtime.

    Local fixtures are plain in-process state machines whose ``open``/``observe``
    never suspend. ``official_offline`` games run only inside the official worker
    process with a staged game directory that this signature does not receive,
    so they (and gateway games) get no summary here; forge_dryrun shows them.
    """
    if ref.backend != "local":
        return None
    try:
        session = _settle(LocalGameProvider().open(ref, limits=GameLimits()))
        if session is None:
            return None
        try:
            return _settle(session.observe())
        finally:
            _settle(session.close())
    except Exception:
        return None


def _observation_summary(ref: GameRef) -> str:
    observation = _first_observation(ref)
    if observation is None:
        return f"- {ref.game_id}: {_NO_SUMMARY}"
    frame = observation.frames[-1]
    height, width = len(frame), len(frame[0]) if frame else 0
    colours = Counter(cell for row in frame for cell in row)
    counts = ", ".join(f"{colour}:{count}" for colour, count in sorted(colours.items()))
    actions = ", ".join(offered_actions(observation, fresh=True)) or "none"
    return (
        f"- {ref.game_id}: {width}x{height} frame (width x height); "
        f"colour counts {counts}; available actions {actions}"
    )


def _bound_templates() -> str:
    """The template files that call the bound preset, by path, for SPEC.md."""
    names = [
        f"templates/{name}.py"
        for name in TEMPLATE_NAMES
        if "$PRESET_BINDING" in template_source(name)
    ]
    return ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else "".join(names)


def _spec_markdown(artifact: HarnessArtifact) -> str:
    return spec_markdown(artifact.spec, artifact.model, artifact.preset)


def spec_markdown(spec: Spec, model: str, preset: str) -> str:
    """SPEC.md for a spec, its play model and its bound preset."""
    floor = spec.floor
    kind = spec.execution_config.get("kind", "standard")
    lines = [
        "# Specification",
        "",
        f"- domain: {spec.domain}",
        f"- execution kind: {kind if isinstance(kind, str) else 'standard'}",
        f"- play model: {model} (every measurement uses this model)",
        f"- floor: {floor.kind.value}, limit {floor.limit}",
        f"- budget per run: ${spec.budget.cost_usd} and {spec.budget.compute_s} s",
        f"- bound preset: {preset} ({_bound_templates()} call it with the exact binding)",
        "",
        "## Task",
        "",
        spec.task,
        "",
        "## Input schema",
        "",
        "```json",
        _json(spec.input_schema),
        "```",
        "",
        "## Output schema",
        "",
        "```json",
        _json(spec.output_schema),
        "```",
        "",
    ]
    return "\n".join(lines)


def _case_block(
    index: int, case: EvaluationCase, settings: ForgeSettings, chars: int | None = None
) -> list[str]:
    """One case as TUNING.md shows it: input and expected output (``per_case``), each
    clipped at ``chars`` characters (``settings.case_chars`` unless given)."""
    limit = settings.case_chars if chars is None else chars
    block = [
        f"## Case {index}",
        "",
        "input:",
        "```json",
        _clip(_json(_image_view(case.task)), limit),
        "```",
    ]
    if settings.feedback == "per_case":
        block += [
            "expected:",
            "```json",
            _clip(_json(_image_view(case.expected)), limit),
            "```",
        ]
    block.append("")
    return block


def _block_bytes(block: Sequence[str]) -> int:
    return sum(len(line.encode("utf-8")) + 1 for line in block)


def _fitted_block(
    index: int, case: EvaluationCase, settings: ForgeSettings, room: int
) -> list[str]:
    """The case's block within ``room`` bytes, clipped harder than ``case_chars`` when it
    must be: the first case is always shown, and ``case_chars`` allows 20,000 characters
    of input and as many of expected output, more than all of TUNING.md may hold."""
    chars = settings.case_chars
    block = _case_block(index, case, settings, chars)
    while chars > 0 and _block_bytes(block) > room:
        size = _block_bytes(block)
        chars = max(0, min(chars - 1, chars * room // size))
        block = _case_block(index, case, settings, chars)
    return block


def _tuning_markdown(cases: Sequence[EvaluationCase], settings: ForgeSettings) -> str:
    """The first ``max_cases_shown`` tuning cases, and never more than ``TUNING_MAX_BYTES``.

    Every case stays reachable by index: forge_measure and forge_dryrun take any of them,
    and the header says how many are shown. With every case shown, the text is exactly
    what it was before the cap existed. At least the first case is always shown, clipped
    harder when even it alone would not fit (``_fitted_block``).
    """
    shown = settings.feedback == "per_case"
    lines = [
        f"# Tuning cases ({len(cases)})",
        "",
        "Tuning evidence, not a license: a hidden verifier you cannot query decides.",
        "Cases are indexed 0..n-1 for forge_dryrun and forge_measure.",
        "" if shown else "Expected outputs are withheld in aggregate feedback mode.",
        "",
    ]
    games: dict[str, GameRef] = {}
    for case in cases:
        games.update((ref.model_dump_json(), ref) for ref in _game_refs(case.task))
    tail: list[str] = []
    if games:
        tail = [
            "## Tuning games",
            "",
            "First-observation summary per tuning game (colour counts are value:cells).",
            "",
            *(
                _observation_summary(ref)
                for ref in sorted(games.values(), key=lambda r: (r.game_id, r.revision))
            ),
            "",
            "Use forge_dryrun on a case to see its full observations (unscored).",
            "",
        ]
    used = _block_bytes([*lines, *tail]) + _TUNING_RESERVE
    blocks: list[list[str]] = []
    for index, case in enumerate(cases[: settings.max_cases_shown]):
        block = _case_block(index, case, settings)
        size = _block_bytes(block)
        if used + size > TUNING_MAX_BYTES:
            if blocks:
                break  # the rest stay in order behind it, reachable by index
            block = _fitted_block(index, case, settings, TUNING_MAX_BYTES - used)
            size = _block_bytes(block)
        blocks.append(block)
        used += size
    if len(blocks) < len(cases):
        lines[3] = (
            f"Cases are indexed 0..n-1 for forge_dryrun and forge_measure. Cases 0 to "
            f"{len(blocks) - 1} are shown here; forge_measure reaches all {len(cases)}."
        )
    for block in blocks:
        lines += block
    return "\n".join([*lines, *tail])


tuning_markdown = _tuning_markdown


def _baseline(candidate: CandidateEvaluation) -> dict[str, JsonValue]:
    artifact = candidate.artifact
    return {
        "preset": artifact.preset,
        "params": artifact.params,
        "measurement": cast(
            JsonValue, candidate.measurement.model_dump(mode="json", exclude={"failures"})
        ),
    }


def incumbent_source(candidate: CandidateEvaluation, settings: ForgeSettings) -> str:
    """The Stage 2 best's sandbox source, or the instantiated template (8.3)."""
    artifact = candidate.artifact
    if settings.mode == "agent" and artifact.execution_mode == "sandbox":
        try:
            with (artifact.directory / "harness.py").open("rb") as stream:
                raw = stream.read(_SOURCE_LIMIT + 1)
            if len(raw) <= _SOURCE_LIMIT:
                return raw.decode("utf-8").replace("\r\n", "\n")
        except (OSError, UnicodeError):
            pass
    name: TemplateName = settings.template or default_template(artifact.spec)
    return instantiate(name, candidate)


def prepare_workspace(
    root: Path,
    candidate: CandidateEvaluation,
    floor: FloorEvaluator,
    settings: ForgeSettings,
) -> Path:
    """Write the agent-visible files into ``root`` and return it."""
    root.mkdir(parents=True, exist_ok=True)
    files = {
        "harness.py": incumbent_source(candidate, settings),
        "LANGUAGE.md": language_text(),
        "DESIGN.md": DESIGN_TEXT,
        "SPEC.md": _spec_markdown(candidate.artifact),
        "TUNING.md": _tuning_markdown(floor.tuning, settings),
        "BASELINE.json": _json(_baseline(candidate)) + "\n",
        "NOTES.md": "",
    }
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8", newline="\n")
    templates = root / "templates"
    templates.mkdir(exist_ok=True)
    for template in TEMPLATE_NAMES:
        (templates / f"{template}.py").write_text(
            instantiate(template, candidate), encoding="utf-8", newline="\n"
        )
    (root / "drafts").mkdir(exist_ok=True)
    return root
