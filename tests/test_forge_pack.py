"""The Forge pack hash and its task-text check (W3b, for pre-registration).

The pack is the prompt (Forge's instructions, its tools' descriptions and schemas, the Pi
parent's and the tools' fixed texts), the Pi bridge files, the workspace scaffolding,
DESIGN.md, LANGUAGE.md, the raw templates and the ForgeSettings.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from architect.compiler import forge as forge_module
from architect.compiler import forge_pack
from architect.compiler.forge_pack import (
    PACK_FORMAT,
    RUN_WORDS,
    Finding,
    pack_hash,
    pack_manifest,
    pack_parts,
    pack_settings,
    spec_sources,
    spec_terms,
    task_text_findings,
)
from architect.compiler.forge_settings import ForgeSettings
from architect.compiler.forge_templates import TEMPLATE_NAMES
from architect.runtime import sandbox_language
from architect.spec import Budget, FloorDefinition, FloorKind, Spec

ROOT = Path(__file__).parents[1]
# The words the pre-registration draft says the pack must not hold (docs/bench/prereg).
PREREG_TERMS = ("receipt", "CORD", "subtotal", "item_count", "PPN", "Banking77")


def receipt_spec() -> Spec:
    task = json.loads((ROOT / "examples/receipts/task.json").read_text(encoding="utf-8"))
    return Spec(
        task=task["rules"],
        input_schema=task["input_schema"],
        output_schema=task["output_schema"],
        domain="extraction",
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.8, dataset="receipts"),
        budget=Budget(cost_usd=0.01, compute_s=60),
    )


def test_the_pack_hash_is_stable_and_names_every_part() -> None:
    settings = ForgeSettings()
    value = pack_hash(settings)
    assert re.fullmatch(r"[0-9a-f]{64}", value) and pack_hash(settings) == value
    assert set(pack_manifest(settings)) == {
        "prompt/instructions",
        "prompt/tools",
        "prompt/parent",
        "prompt/tool_texts",
        "prompt/refusals",
        "bridge/bridge.mjs",
        "bridge/extension.ts",
        "workspace/SPEC.md",
        "workspace/TUNING.md",
        "DESIGN.md",
        "LANGUAGE.md",
        "settings",
        *(f"templates/{name}.py" for name in TEMPLATE_NAMES),
    }
    assert PACK_FORMAT == "architect-forge-pack/1"
    parts = pack_parts(settings)
    assert all(text.isascii() for text in parts.values())
    # Templates are hashed as written, placeholders and all: no task is ever put in.
    assert "$PRESET_BINDING" in parts["templates/extract_verify.py"]


def test_only_whether_forge_runs_is_left_out_of_the_settings() -> None:
    base = ForgeSettings()
    # max_rungs decides on which ladder rungs Forge runs, as trigger decides whether it does.
    assert not {"enabled", "trigger", "max_rungs"} & set(pack_settings(base))
    switched = ForgeSettings(enabled=True, trigger="always", max_rungs=1)
    assert pack_hash(switched) == pack_hash(base)
    for change in (
        {"max_usd": 2.0},
        {"agent_max_tokens": 8192},
        {"max_cases_shown": 8},
        {"max_failures_shown": 4},
        {"context_chars": 40000},
        {"mode": "template"},
        {"agent_model": "ultra"},
    ):
        assert pack_hash(ForgeSettings.model_validate(change)) != pack_hash(base), change


@pytest.mark.parametrize(
    ("target", "attribute", "part"),
    [
        (forge_module, "FORGE_INSTRUCTIONS", "prompt/instructions"),
        (forge_pack, "DESIGN_TEXT", "DESIGN.md"),
        (forge_pack, "PARENT_TEXTS", "prompt/parent"),
        (forge_pack, "TOOL_DESCRIPTIONS", "prompt/tools"),
        (forge_pack, "TOOL_TEXTS", "prompt/tool_texts"),
        (forge_pack, "REFUSAL_TEXTS", "prompt/refusals"),
        (sandbox_language, "ALTERNATIVES", "prompt/refusals"),
        (sandbox_language, "MODULES", "prompt/refusals"),
        (sandbox_language, "HINTS", "prompt/refusals"),
    ],
)
def test_changing_any_text_changes_its_part_and_the_hash(
    monkeypatch: pytest.MonkeyPatch, target: Any, attribute: str, part: str
) -> None:
    settings = ForgeSettings()
    before, manifest = pack_hash(settings), pack_manifest(settings)
    value = getattr(target, attribute)
    if isinstance(value, str):
        changed: Any = value + " "
    else:
        changed = {**value, next(iter(value)): "changed"}
    monkeypatch.setattr(target, attribute, changed)
    after = pack_manifest(settings)
    assert pack_hash(settings) != before
    assert [name for name in after if after[name] != manifest[name]] == [part]


def test_the_refusal_list_size_and_repeat_line_are_in_the_pack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = ForgeSettings()
    before = pack_manifest(settings)
    assert "the same problem as line {line}." in pack_parts(settings)["prompt/tool_texts"]
    monkeypatch.setattr(sandbox_language, "REFUSALS_SHOWN", sandbox_language.REFUSALS_SHOWN + 1)
    after = pack_manifest(settings)
    assert [name for name in after if after[name] != before[name]] == ["prompt/refusals"]


def test_language_and_templates_are_in_the_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = ForgeSettings()
    before = pack_manifest(settings)
    monkeypatch.setattr(forge_pack, "language_text", lambda: "# Sandbox language 3\n")
    original = forge_pack.template_source
    monkeypatch.setattr(
        forge_pack,
        "template_source",
        lambda name: original(name) + ("\n" if name == "prompt_chain" else ""),
    )
    after = pack_manifest(settings)
    assert [name for name in after if after[name] != before[name]] == [
        "LANGUAGE.md",
        "templates/prompt_chain.py",
    ]


def test_the_bridge_files_are_in_the_pack_whatever_their_line_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = ForgeSettings()
    parts = pack_parts(settings)
    # The fixed texts the bridge adds to every Forge prompt are covered.
    assert "You are a coding agent. Use only granted tools." in parts["bridge/bridge.mjs"]
    assert "No autonomous subagent spawning." in parts["bridge/extension.ts"]
    before = pack_manifest(settings)
    for name in ("bridge.mjs", "extension.ts"):
        text = (ROOT / "architect/backends/pi-node" / name).read_bytes()
        (tmp_path / name).write_bytes(text.replace(b"\n", b"\r\n"))  # a CRLF checkout
    monkeypatch.setattr(forge_pack, "_PI_NODE", tmp_path)
    assert pack_manifest(settings) == before
    edited = (tmp_path / "extension.ts").read_bytes().replace(
        b"No autonomous subagent spawning.", b"No subagent spawning."
    )
    (tmp_path / "extension.ts").write_bytes(edited)
    after = pack_manifest(settings)
    assert [name for name in after if after[name] != before[name]] == ["bridge/extension.ts"]


def test_the_workspace_scaffolding_is_in_the_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = ForgeSettings()
    parts = pack_parts(settings)
    spec_md, tuning_md = parts["workspace/SPEC.md"], parts["workspace/TUNING.md"]
    assert "- play model: <play model> (every measurement uses this model)" in spec_md
    assert "call it with the exact binding" in spec_md and "<task>" in spec_md
    for line in (
        "Tuning evidence, not a license: a hidden verifier you cannot query decides.",
        "Cases 0 to 0 are shown here; forge_measure reaches all 2.",
        "Expected outputs are withheld in aggregate feedback mode.",
        "first observation not available offline; use forge_dryrun (unscored)",
        "Use forge_dryrun on a case to see its full observations (unscored).",
    ):
        assert line in tuning_md, line
    # Rendered from fixed placeholders: the pack's own settings never change them.
    other = pack_parts(ForgeSettings(feedback="aggregate", max_cases_shown=4))
    assert other["workspace/TUNING.md"] == tuning_md
    before = pack_manifest(settings)
    original = forge_pack.spec_markdown
    monkeypatch.setattr(
        forge_pack, "spec_markdown", lambda *args: original(*args) + "Read this first.\n"
    )
    after = pack_manifest(settings)
    assert [name for name in after if after[name] != before[name]] == ["workspace/SPEC.md"]


def test_the_prompt_part_is_the_system_prompt_with_its_card() -> None:
    parts = pack_parts(ForgeSettings())
    assert parts["prompt/instructions"] == forge_module.forge_prompt()
    assert parts["prompt/instructions"].endswith(parts["LANGUAGE.md"])


def test_the_pack_holds_no_receipt_text() -> None:
    spec = receipt_spec()
    parts = pack_parts(ForgeSettings())
    terms = (*PREREG_TERMS, *spec_terms(spec))
    assert task_text_findings(parts, terms=terms, sources=spec_sources(spec)) == []


def test_terms_match_whole_words_in_any_case() -> None:
    parts = {"DESIGN.md": "Check that totals add up. A Receipt reader.", "x": "item_count = 3"}
    found = task_text_findings(parts, terms=["total", "receipt", "item_count", " ", ""])
    assert found == [
        Finding("DESIGN.md", "term", "receipt"),
        Finding("x", "term", "item_count"),
    ]


def test_copied_runs_of_words_are_found_whatever_the_punctuation() -> None:
    rules = "Amounts are integers: take the amount as printed and delete every character."
    parts = {
        "copied": "TAKE the amount, as printed -- and delete every character that is extra.",
        "short": "take the amount as printed and delete",  # 7 words: not a run
        "other": "Nothing like it here at all, not one shared run of words.",
    }
    found = task_text_findings(parts, sources=[rules])
    assert found == [
        Finding("copied", "words", "take the amount as printed and delete every"),
        Finding("copied", "words", "the amount as printed and delete every character"),
    ]
    assert RUN_WORDS == 8
    assert task_text_findings(parts, sources=[rules], run_words=7)[0].part == "copied"
    with pytest.raises(ValueError):
        task_text_findings(parts, sources=[rules], run_words=0)


def test_spec_terms_are_identifiers_and_sources_are_its_prose() -> None:
    spec = receipt_spec().model_copy(
        update={
            "output_schema": {
                "type": "object",
                "title": "Receipt totals",
                "properties": {
                    "total": {"type": "integer", "description": "The amount printed last."},
                    "item_count": {"type": "integer"},
                    "subtotalAmount": {"type": "integer"},
                    "kind": {"enum": ["card_payment", "cash"]},
                    "line2": {"type": "string"},
                },
            }
        }
    )
    assert spec_terms(spec) == ["card_payment", "item_count", "line2", "subtotalAmount"]
    sources = spec_sources(spec)
    assert sources[0] == spec.task
    assert "Receipt totals" in sources and "The amount printed last." in sources
