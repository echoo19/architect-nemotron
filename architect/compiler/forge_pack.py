"""The Forge pack: everything task-neutral that Forge's engineer reads or runs with.

A pre-registration pins the pack by one sha256 (``pack_hash``). The pack is:

- the prompt: ``forge_prompt()`` (``FORGE_INSTRUCTIONS`` and the language card after it,
  exactly as the engineer's system prompt gets them), the five Forge tools' descriptions
  and parameter schemas, the fixed texts the Pi parent adds to a session (``PARENT_TEXTS``:
  re-asks, context notes, read refusals and their listings) and those a tool result can
  show (``TOOL_TEXTS``, with the fixed values of a dry run's example reply, and every
  refusal's rule text: the sandbox language's ``RULES``, ``ALTERNATIVES``, ``MODULES``
  and ``HINTS``, how many refusals a check lists, and the tools' ``REFUSAL_TEXTS``);
- the Pi bridge (``bridge.mjs`` and ``extension.ts``, with LF line ends): its system
  prompt line, the rules it appends to every prompt, its refusals and how it thins a
  context;
- the workspace scaffolding: ``SPEC.md`` and ``TUNING.md`` rendered for a fixed,
  task-free probe, so every fixed line around a task's own text is covered;
- ``DESIGN.md`` and ``LANGUAGE.md``, as written into every workspace;
- ``templates/``: each template's source with its placeholders, before a task is put in;
- the ``ForgeSettings``, without ``enabled``, ``trigger`` and ``max_rungs``, which decide
  whether (and on which ladder rungs) Forge runs, not what it does.

Left out: the task, its floor and cases, the agent model's own settings, and the Pi
0.73.1 runtime the lockfile pins. ``task_text_findings`` checks that no part of the pack
holds text from a task: a given term (whole word, any case) or a run of words copied
from a source such as the task's rules.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from pydantic import JsonValue

from architect.backends.pi import PARENT_TEXTS
from architect.compiler.forge_design import DESIGN_TEXT
from architect.compiler.forge_example import example_texts
from architect.compiler.forge_settings import ForgeSettings
from architect.compiler.forge_templates import TEMPLATE_NAMES, template_source
from architect.compiler.forge_tools import _DESCRIPTIONS as TOOL_DESCRIPTIONS
from architect.compiler.forge_tools import (
    CHECK_PARAMETERS,
    DRYRUN_PARAMETERS,
    MEASURE_PARAMETERS,
    REFUSAL_TEXTS,
    SPAWN_PARAMETERS,
    SUBMIT_PARAMETERS,
    TOOL_TEXTS,
)
from architect.compiler.forge_workspace import language_text, spec_markdown, tuning_markdown
from architect.floors import EvaluationCase
from architect.runtime import sandbox_language
from architect.spec import Budget, FloorDefinition, FloorKind, Spec

__all__ = [
    "PACK_FORMAT",
    "RUN_WORDS",
    "Finding",
    "pack_hash",
    "pack_manifest",
    "pack_parts",
    "pack_settings",
    "spec_sources",
    "spec_terms",
    "task_text_findings",
]

PACK_FORMAT: Final = "architect-forge-pack/1"
RUN_WORDS: Final = 8  # a copied run of this many words counts as task text
_RUNTIME_SWITCHES: Final = frozenset({"enabled", "trigger", "max_rungs"})
_TOOL_PARAMETERS: Final[dict[str, dict[str, JsonValue]]] = {
    "forge_check": CHECK_PARAMETERS,
    "forge_dryrun": DRYRUN_PARAMETERS,
    "forge_measure": MEASURE_PARAMETERS,
    "forge_submit": SUBMIT_PARAMETERS,
    "forge_spawn": SPAWN_PARAMETERS,
}
_WORD: Final = re.compile(r"[0-9a-z]+")
_IDENTIFIER: Final = re.compile(r"^(?=.*(?:_|\d|[a-z][A-Z]))[A-Za-z][A-Za-z0-9_]{2,63}$")
_PI_NODE: Final = Path(__file__).resolve().parents[1] / "backends" / "pi-node"
_BRIDGE_FILES: Final = ("bridge.mjs", "extension.ts")
# The task-free probe the workspace scaffolding is rendered for: placeholders only.
_PROBE_SPEC: Final[dict[str, JsonValue]] = {
    "task": "<task>",
    "input_schema": {"type": "object"},
    "output_schema": {"type": "object"},
    "domain": "<domain>",
}
_PROBE_GAME: Final[dict[str, JsonValue]] = {
    "game_id": "probe",
    "backend": "official_gateway",  # never opened here: summarised as unavailable offline
    "revision": "probe",
}


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def pack_settings(settings: ForgeSettings) -> dict[str, JsonValue]:
    """The settings as hashed: everything but the switches that decide whether Forge runs
    (``enabled``, ``trigger``, and ``max_rungs``, which decides it per ladder rung)."""
    return settings.model_dump(mode="json", exclude=set(_RUNTIME_SWITCHES))


def _bridge_text(name: str) -> str:
    """One file of the Pi bridge as hashed: its UTF-8 text with LF line ends."""
    text = (_PI_NODE / name).read_bytes().decode("utf-8")
    return text.replace("\r\n", "\n")


def _scaffold() -> dict[str, str]:
    """SPEC.md and TUNING.md for the task-free probe: every fixed line of both files
    (TUNING.md in both feedback modes, with a case left out and a game summary)."""
    spec = Spec.model_validate(
        {
            **_PROBE_SPEC,
            "floor": FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.5, dataset="<data>"),
            "budget": Budget(cost_usd=0.01, compute_s=1),
        }
    )
    cases = [
        EvaluationCase(case_id="probe-0", task={"input": "<input>"}, expected="<expected>"),
        EvaluationCase(case_id="probe-1", task=dict(_PROBE_GAME), expected="<expected>"),
    ]
    tuning = [
        tuning_markdown(cases, ForgeSettings(feedback=feedback, max_cases_shown=1))
        for feedback in ("per_case", "aggregate")
    ]
    return {
        "workspace/SPEC.md": spec_markdown(spec, "<play model>", "<preset>"),
        "workspace/TUNING.md": "\n".join(tuning),
    }


def _refusals() -> dict[str, JsonValue]:
    """Every fixed sentence a refusal can show the engineer, by table."""
    rules: dict[str, JsonValue] = {
        name: [rule.code, rule.text, rule.named]
        for name, rule in sandbox_language.RULES.items()
    }
    return {
        "rules": rules,
        "alternatives": dict(sandbox_language.ALTERNATIVES),
        "modules": dict(sandbox_language.MODULES),
        "hints": dict(sandbox_language.HINTS),
        "forge": dict(REFUSAL_TEXTS),
        "shown": sandbox_language.REFUSALS_SHOWN,
    }


def pack_parts(settings: ForgeSettings) -> dict[str, str]:
    """Every part of the pack by name, as the exact text that is hashed."""
    from architect.compiler.forge import forge_prompt  # forge imports this module

    tools = [
        {"name": name, "description": TOOL_DESCRIPTIONS[name], "parameters": parameters}
        for name, parameters in sorted(_TOOL_PARAMETERS.items())
    ]
    return {
        "prompt/instructions": forge_prompt(),
        "prompt/tools": _canonical(tools),
        "prompt/parent": _canonical(dict(PARENT_TEXTS)),
        "prompt/tool_texts": _canonical(
            {**TOOL_TEXTS, **{f"dryrun_example/{k}": v for k, v in example_texts().items()}}
        ),
        "prompt/refusals": _canonical(_refusals()),
        **{f"bridge/{name}": _bridge_text(name) for name in _BRIDGE_FILES},
        **_scaffold(),
        "DESIGN.md": DESIGN_TEXT,
        "LANGUAGE.md": language_text(),
        **{f"templates/{name}.py": template_source(name) for name in TEMPLATE_NAMES},
        "settings": _canonical(pack_settings(settings)),
    }


def pack_manifest(settings: ForgeSettings) -> dict[str, str]:
    """The sha256 of each part's UTF-8 text, by part name."""
    return {
        name: hashlib.sha256(text.encode("utf-8")).hexdigest()
        for name, text in pack_parts(settings).items()
    }


def pack_hash(settings: ForgeSettings) -> str:
    """The pack's sha256: over the format name and every part's own sha256."""
    manifest = {"format": PACK_FORMAT, "parts": pack_manifest(settings)}
    return hashlib.sha256(_canonical(manifest).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Finding:
    """Task text found in the pack: a ``term`` or a copied run of ``words``."""

    part: str
    kind: Literal["term", "words"]
    text: str


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def task_text_findings(
    parts: Mapping[str, str],
    *,
    terms: Iterable[str] = (),
    sources: Iterable[str] = (),
    run_words: int = RUN_WORDS,
) -> list[Finding]:
    """Where the pack holds task text: each term found as a whole word (any case), and
    each run of ``run_words`` words that also appears in a source (punctuation and case
    are ignored). Empty when the pack is clean."""
    if run_words < 1:
        raise ValueError("run_words must be positive")
    findings: list[Finding] = []
    wanted = sorted({term.strip() for term in terms if term.strip()}, key=str.casefold)
    patterns = [
        (term, re.compile(r"(?<![0-9A-Za-z_])" + re.escape(term) + r"(?![0-9A-Za-z_])", re.I))
        for term in wanted
    ]
    runs: set[tuple[str, ...]] = set()
    for source in sources:
        words = _words(source)
        runs.update(tuple(words[i : i + run_words]) for i in range(len(words) - run_words + 1))
    for name, text in parts.items():
        findings += [
            Finding(name, "term", term) for term, pattern in patterns if pattern.search(text)
        ]
        if not runs:
            continue
        words = _words(text)
        seen: set[tuple[str, ...]] = set()
        for start in range(len(words) - run_words + 1):
            window = tuple(words[start : start + run_words])
            if window in runs and window not in seen:
                seen.add(window)
                findings.append(Finding(name, "words", " ".join(window)))
    return findings


def _walk(schema: object) -> Iterable[dict[str, object]]:
    pending = [schema]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            yield item
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def spec_terms(spec: Spec) -> list[str]:
    """Identifier-like words of a spec's schemas (property names and enum strings with an
    underscore, a digit or an inner capital, such as ``item_count``): words that
    task-neutral text has no reason to use."""
    found: set[str] = set()
    for schema in (spec.input_schema, spec.output_schema):
        for node in _walk(schema):
            properties = node.get("properties")
            names = [name for name in properties if isinstance(name, str)] if isinstance(
                properties, dict
            ) else []
            choices = node.get("enum")
            if isinstance(choices, list):
                names += [item for item in choices if isinstance(item, str)]
            found.update(name for name in names if _IDENTIFIER.fullmatch(name))
    return sorted(found)


def spec_sources(spec: Spec) -> list[str]:
    """The spec's own prose: its task text and every schema title and description."""
    texts = [spec.task]
    for schema in (spec.input_schema, spec.output_schema):
        for node in _walk(schema):
            for key in ("title", "description"):
                value = node.get(key)
                if isinstance(value, str):
                    texts.append(value)
    return texts
