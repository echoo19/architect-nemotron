"""The five Forge trusted tools and their leakage-safe views (Phase 9, Section 9).

Every callback reads the draft from the workspace through trusted code; source is
never taken from tool arguments. Results carry only fixed codes, line numbers,
tuning-side measurements, the agent's own dry-run shapes and the remaining
budget. Held-out cases, Stage 4 results, dataset paths, exception text and case
metadata never enter a result: the floor is reached only through ``tuning``.

A refusal also carries its ``rule`` and a ``message``: one or two fixed sentences
(``sandbox_language.RULES``, ``HINTS`` or ``REFUSAL_TEXTS``) saying what broke and the
form that is allowed, after the line number. No source text is ever echoed; a message
names an identifier only when it is one of the language's own or a key of its fixed
tables. A source the checker refuses also lists every refused line it found
(``refusals``, at most ``REFUSALS_SHOWN`` of them, by line), so one check is enough to
fix them all.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import hashlib
import importlib
import json
import math
import re
import shutil
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Literal, cast

from jsonschema import Draft202012Validator
from pydantic import JsonValue

from architect.backends.pi import PiConfig, TrustedTool
from architect.compiler.forge_audit import AuditError, AuditSession
from architect.compiler.forge_example import example_reply
from architect.compiler.forge_lint import leaked_literals
from architect.compiler.forge_settings import ForgeSettings
from architect.compiler.types import (
    AdapterFactory,
    CandidateEvaluation,
    FloorFactory,
    LibraryLookup,
    Measurement,
    NullLibrary,
)
from architect.floors import EvaluationCase, FloorEvaluator, HeldOutTasks
from architect.models import ModelAdapter
from architect.runtime import BudgetExceeded, Runtime
from architect.runtime.evaluation import evaluate_cases
from architect.runtime.sandbox import Refusal, SandboxError, validate_source
from architect.runtime.sandbox_language import REFUSALS_SHOWN, refusal_message
from architect.runtime.wallet import CompileWallet, compilation_wallet
from architect.spec import (
    Budget,
    FloorDefinition,
    FloorKind,
    ForgeBinding,
    HarnessArtifact,
    Lifecycle,
    Spec,
    SubharnessRef,
)

__all__ = [
    "ALLOWED_PRESETS",
    "CHECK_PARAMETERS",
    "DRYRUN_PARAMETERS",
    "MEASURE_PARAMETERS",
    "REFUSAL_TEXTS",
    "RESULT_CHARS",
    "SPAWN_PARAMETERS",
    "SUBMIT_PARAMETERS",
    "TOOL_TEXTS",
    "AuditStopped",
    "Child",
    "ForgeRank",
    "ForgeSession",
    "SubmissionsExhausted",
    "add_result_observer",
    "forge_check_tool",
    "forge_dryrun_tool",
    "forge_measure_tool",
    "forge_rank",
    "forge_spawn_tool",
    "forge_submit_tool",
    "forge_tools",
    "notify_result_observers",
    "remove_result_observer",
    "spawn_settings",
    "submit_source",
]

RESULT_CHARS: Final = 16384
NODE_LIMIT_V2: Final = 8000
_SOURCE_LIMIT: Final = 64 * 1024
_SCHEMA_LIMIT: Final = 16 * 1024
_EXCERPT_CHARS: Final = 600
_GAME_KINDS: Final = frozenset({"game", "competition"})
_DRAFT_PATH: Final = re.compile(r"^(harness|drafts/[a-z][a-z0-9_]{0,31})\.py$")
_CHILD_NAME: Final = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SANDBOX_CODE: Final = re.compile(r"^sandbox_[a-z_]{1,48}$")
_PRESETS: Final = frozenset(
    {
        "extractor",
        "synthesizer",
        "recommender",
        "coder",
        "explorer",
        "planner",
        "verifier",
        "hypothesis_tester",
    }
)

# The parent's host preset allowlist, set by the Compiler around ``forge()`` so a
# spawned child inherits it without widening ``forge()``'s fixed signature.
ALLOWED_PRESETS: ContextVar[tuple[str, ...]] = ContextVar("forge_allowed_presets", default=())
# Phase 10 Section 11.6: with ``routing.child_warm_start`` the Compiler sets a
# lookup-only view of the parent's library (``ReadOnlyLibrary``, never a
# ``LibraryLifecycle``) around ``forge()``; None keeps ``NullLibrary()``.
CHILD_LIBRARY: ContextVar[LibraryLookup | None] = ContextVar("forge_child_library", default=None)
# Phase 12: an optional sink for the Forge agent's live Pi tool events (the CLI shows
# them while a compile runs). Informational only; None keeps the backend unchanged.
FORGE_EVENTS: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "forge_events", default=None
)

ForgeRank = tuple[bool, bool, bool, float, float, int, int]

# Result observers (the desktop's live Forge board): process-wide, called at the end of
# every tool callback with ``{"tool", "depth", "result"}`` where ``result`` is a copy of
# the bounded dict the agent receives (``left`` included), and by the Compiler when a
# session starts and ends (``tool == "forge_session"``). When the agent's harness.py
# changed since the last notice, ``source`` holds the version the tool worked on
# (``{"path", "sha256", "text"}``, read through ``_read_draft``), so the desktop can show
# Forge's file versions; the agent never receives it. Tool results and drafts carry
# tuning-side data only (see the module docstring), so nothing here comes from the
# held-out partition. No-ops unless something subscribes; an observer never breaks a
# session.
_RESULT_OBSERVERS: list[Callable[[dict[str, Any]], None]] = []


def add_result_observer(observer: Callable[[dict[str, Any]], None]) -> None:
    if observer not in _RESULT_OBSERVERS:
        _RESULT_OBSERVERS.append(observer)


def remove_result_observer(observer: Callable[[dict[str, Any]], None]) -> None:
    if observer in _RESULT_OBSERVERS:
        _RESULT_OBSERVERS.remove(observer)


def notify_result_observers(data: dict[str, Any]) -> None:
    for observer in tuple(_RESULT_OBSERVERS):
        try:
            observer(copy.deepcopy(data))
        except Exception:
            pass


def _notify(
    session: ForgeSession, tool: str, result: JsonValue, source: str | None = None
) -> None:
    if _RESULT_OBSERVERS:
        data: dict[str, Any] = {
            "tool": tool, "depth": getattr(session, "depth", 0), "result": result
        }
        draft = _observed_source(session, source)
        if draft is not None:
            data["source"] = draft
        notify_result_observers(data)


def _observed_source(session: ForgeSession, source: str | None) -> dict[str, Any] | None:
    """The harness draft a tool just worked on (``source``, else harness.py read through
    trusted code), only when it differs from the last one observers were sent."""
    text = source
    if text is None:
        try:
            text = _read_draft(session)
        except _ToolError:
            return None
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if getattr(session, "observed_source", None) == digest:
        return None
    try:
        session.observed_source = digest
    except AttributeError:
        return None  # a stand-in session that cannot remember it: send nothing
    return {"path": "harness.py", "sha256": digest, "text": text}


_Callback = Callable[[dict[str, JsonValue]], Awaitable[JsonValue]]


def _observing(session: ForgeSession, tool: str, callback: _Callback) -> _Callback:
    async def observed(args: dict[str, JsonValue]) -> JsonValue:
        result = await callback(args)
        _notify(session, tool, result)
        return result

    return observed

CHECK_PARAMETERS: Final[dict[str, JsonValue]] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "path": {"type": "string", "pattern": "^(harness|drafts/[a-z][a-z0-9_]{0,31})\\.py$"}
    },
}
DRYRUN_PARAMETERS: Final[dict[str, JsonValue]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["case"],
    "properties": {
        "case": {"type": "integer", "minimum": 0},
        "replies": {
            "type": "array",
            "maxItems": 8,
            "items": {"type": "string", "maxLength": 2000},
        },
        "game_steps": {"type": "integer", "minimum": 1, "maximum": 20},
    },
}
MEASURE_PARAMETERS: Final[dict[str, JsonValue]] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "cases": {
            "type": "array",
            "maxItems": 32,
            "uniqueItems": True,
            "items": {"type": "integer", "minimum": 0},
        },
        "smoke": {"type": "boolean"},
        "trace": {"type": "boolean"},
    },
}
SUBMIT_PARAMETERS: Final[dict[str, JsonValue]] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"note": {"type": "string", "maxLength": 200}},
}
SPAWN_PARAMETERS: Final[dict[str, JsonValue]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "goal", "input_schema", "output_schema", "cases"],
    "properties": {
        "name": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,31}$"},
        "goal": {"type": "string", "maxLength": 2000},
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "cases": {
            "oneOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["projection"],
                    "properties": {
                        "projection": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["input", "expected"],
                            "properties": {
                                "input": {"type": "string", "maxLength": 256},
                                "expected": {"type": "string", "maxLength": 256},
                            },
                        }
                    },
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["authored"],
                    "properties": {
                        "authored": {
                            "type": "array",
                            "minItems": 8,
                            "maxItems": 64,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["input", "expected"],
                                "properties": {"input": {}, "expected": {}},
                            },
                        }
                    },
                },
            ]
        },
        "floor_limit": {"type": "number", "minimum": 0, "maximum": 1},
        "preset_hint": {"type": "string"},
        "max_usd": {"type": "number", "exclusiveMinimum": 0},
    },
}

_DESCRIPTIONS: Final = {
    "forge_check": (
        "Validate harness.py (or drafts/<name>.py) against the sandbox language. Free. A "
        "refusal lists every refused line it finds (refusals, by line, up to 20), each with "
        "its fixed code, rule and a message with the allowed form; also size statistics, an "
        "advisory literal-leak lint and any referenced sub-harness that is not bound yet."
    ),
    "forge_dryrun": (
        "Run harness.py once on tuning case <case> with scripted model replies, used in turn "
        "(default: a reply shaped like the output schema, then a passing self-check verdict) "
        "and no score. Free. Returns the call shapes, output head, schema check, fuel used "
        "and game steps; a failed run returns its code, line and a message saying why, and a "
        "refused source lists every refused line as forge_check does."
    ),
    "forge_measure": (
        "Measure harness.py with the real play model on tuning cases (default: the first few). "
        "Spends forge budget. Returns the tuning measurement, per-case results when available "
        "(output and expected excerpts for the first failures only) and the Stage 2 "
        "baseline. Tuning evidence, not a license."
    ),
    "forge_submit": (
        "Submit harness.py as a candidate. It is validated, linted for copied tuning literals, "
        "measured on all tuning cases (plus smoke) and accepted only if it ranks above the best "
        "so far. Submit early; the best accepted submission wins."
    ),
    "forge_spawn": (
        "Compile and license a sub-harness with its own input and output schemas, from "
        "projection pointers into tuning cases or at least 8 authored cases. Call it from "
        'harness.py as await context.harness("name", value).'
    ),
}


# What a Forge-level refusal says, by code: the rule that broke and what to do instead.
# Fixed text, covered by the Forge pack hash (``forge_pack``). ``{last}`` is the highest
# tuning index, the one number filled in.
REFUSAL_TEXTS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "forge_invalid_source": (
            "Only harness.py or drafts/<name>.py in this workspace can be used, and the file "
            "must exist as UTF-8 text."
        ),
        "forge_file_too_large": "The file is over 64 KiB. Shorten it, then check it again.",
        "forge_unbound_child": (
            "harness.py calls context.harness with a name that forge_spawn has not made in "
            "this session. Spawn it first, or remove the call."
        ),
        "forge_unused_child": (
            "A sub-harness this harness names is not one forge_spawn made in this session. "
            "Call only the names forge_spawn returned."
        ),
        "invalid_case": (
            "case must be the index of a tuning case, from 0 to {last}. TUNING.md shows them "
            "in order."
        ),
        "forge_dryrun_unavailable": (
            "This case cannot run offline: its game needs an online backend. Use "
            "forge_measure instead."
        ),
        "forge_measure_too_small": "Measure at least one case.",
        "schema_error": (
            "The run's input or output does not match its JSON schema. Compare the output with "
            "the output schema in SPEC.md."
        ),
        "schema_mismatch": (
            "The run finished, but its output does not match the output schema in SPEC.md. "
            "Fix the shape the harness returns."
        ),
        "load_error": "The harness could not be loaded. Call forge_check and fix what it names.",
        "runtime_error": (
            "The run failed without a sandbox code. Check the capability calls and how their "
            "replies are parsed."
        ),
        "budget_exceeded": "The Forge budget is used up; submit the best harness you have.",
        "forge_literal_leak": (
            "This line holds a long literal copied from a tuning case. Write a general rule in "
            "code instead of the case's own text."
        ),
        "tuning_failed": (
            "A submission is accepted only at or above the floor limit in SPEC.md on every "
            "tuning case, and this one scored below it. Improve it with forge_measure first."
        ),
        "smoke_failed": (
            "The harness reached the floor limit on the tuning cases but not on the smoke "
            "cases, where an invalid output counts as a miss. Make every reply parse safely."
        ),
        "not_better": (
            "The harness does not rank above the best so far: BASELINE.json's preset or an "
            "earlier submission. Submit only what forge_measure shows is better."
        ),
        "forge_submissions_exhausted": "No submissions are left.",
        "persistence_failed": "The candidate could not be stored. Submit it again.",
    }
)


def _refusal_list(refusals: Sequence[Refusal]) -> list[JsonValue]:
    """Every refused line, by line, each with its fixed message: at most
    ``REFUSALS_SHOWN``, chosen so each kind of refusal appears once before any repeats.

    A refusal of a kind already listed says only which line it repeats, so a long list
    stays short. Nothing in it comes from the source but line numbers.
    """
    first: dict[tuple[str, str | None, str | None], int] = {}
    for index, refusal in enumerate(refusals):
        first.setdefault((refusal.code, refusal.rule, refusal.name), index)
    chosen = list(first.values())[:REFUSALS_SHOWN]
    taken = set(chosen)
    rest = [index for index in range(len(refusals)) if index not in taken]
    indexes = sorted(chosen + rest[: REFUSALS_SHOWN - len(chosen)])
    shown: list[JsonValue] = []
    seen: dict[tuple[str, str | None, str | None], int | None] = {}
    for index in indexes:
        refusal = refusals[index]
        kind = (refusal.code, refusal.rule, refusal.name)
        where = f"Line {refusal.line}: " if refusal.line is not None else ""
        earlier = seen.get(kind)
        if earlier is not None and refusal.line is not None:
            message = where + _REPEAT.replace("{line}", str(earlier))
        else:
            seen.setdefault(kind, refusal.line)
            message = _message(
                refusal.code, line=refusal.line, rule=refusal.rule, name=refusal.name
            ) or where.rstrip(": ")
        shown.append(
            {"line": refusal.line, "code": refusal.code, "rule": refusal.rule, "message": message}
        )
    return shown


def _message(
    code: str | None,
    *,
    line: int | None = None,
    rule: str | None = None,
    name: str | None = None,
    last: int | None = None,
) -> str | None:
    """A refusal's fixed sentences: a sandbox rule or hint, else the Forge-level text."""
    if code is None:
        return None
    text = refusal_message(code, rule, name, line)
    if text:
        return text
    fixed = REFUSAL_TEXTS.get(code)
    if fixed is None:
        return None
    where = f"Line {line}: " if type(line) is int and line >= 1 else ""
    return where + fixed.replace("{last}", str(last if last is not None else 0))


class SubmissionsExhausted(BudgetExceeded):
    """The last allowed submission was used; the Pi session ends (Section 8.7)."""


class AuditStopped(BudgetExceeded):
    """A submission could not be audited (Phase 14, Section 8.3): the session ends, and
    ``forge()`` promotes nothing from it. Pi lets only budget stops end a session."""


class _ToolError(Exception):
    def __init__(
        self, code: str, line: int | None = None, validated: _Validated | None = None
    ) -> None:
        super().__init__(code)
        self.code = code
        self.line = line
        self.validated = validated

    def fields(self) -> dict[str, JsonValue]:
        """The refusal's sandbox code, rule and message (for a source that failed checks),
        with every refused line the check found."""
        checked = self.validated
        if checked is None:
            return {"sandbox_code": None, "rule": None, "message": _message(self.code)}
        return {
            "sandbox_code": checked.code,
            "rule": checked.rule,
            "message": _message(
                checked.code, line=checked.line, rule=checked.rule, name=checked.name
            ),
            **checked.listed(),
        }


@dataclass(frozen=True)
class Child:
    """A licensed child kept in the session registry (never installed or stored)."""

    artifact: HarnessArtifact
    ref: SubharnessRef


@dataclass(frozen=True)
class _Validated:
    code: str | None
    line: int | None
    stats: dict[str, int]
    rule: str | None = None
    name: str | None = None
    col: int | None = None
    # Every refusal the check found, by line (the first is the one above).
    refusals: tuple[Refusal, ...] = ()

    def listed(self) -> dict[str, JsonValue]:
        """``refusals`` (each refused line with its message) and ``refusals_total``."""
        return {
            "refusals": _refusal_list(self.refusals),
            "refusals_total": len(self.refusals),
        }


@dataclass
class ForgeSession:
    """Trusted per-session state behind the five tools. Never shown to the agent."""

    candidate: CandidateEvaluation
    floor: FloorEvaluator
    smoke_floor: FloorEvaluator | None
    adapter_factory: AdapterFactory
    runtime: Runtime
    settings: ForgeSettings
    workspace: Path
    private: Path
    floor_factory: FloorFactory
    depth: int = 0
    allowed_presets: tuple[str, ...] = ()
    agent_adapter: ModelAdapter | None = None
    pi_config: PiConfig | None = None
    wallet: CompileWallet | None = None
    # Phase 14: where ``submit_source`` records each submission (None: not audited).
    audit: AuditSession | None = None
    checks: int = 0
    dryruns: int = 0
    measures: int = 0
    submissions: int = 0
    accepted: int = 0
    spawns: int = 0
    children: dict[str, Child] = field(default_factory=dict)
    spawn_names: set[str] = field(default_factory=set)
    notes: list[tuple[int, str]] = field(default_factory=list)
    best: CandidateEvaluation | None = None
    best_rank: ForgeRank | None = None
    best_number: int | None = None
    best_children: tuple[str, ...] = ()
    best_source_bytes: int = 0
    best_nodes: int = 0
    # sha256 of the last harness draft result observers were sent (``_observed_source``).
    observed_source: str | None = None
    _scratch: int = 0

    def __post_init__(self) -> None:
        self.tuning: tuple[EvaluationCase, ...] = self.floor.tuning
        self.smoke_tuning: tuple[EvaluationCase, ...] = (
            self.smoke_floor.tuning if self.smoke_floor is not None else ()
        )
        (self.private / "candidates").mkdir(parents=True, exist_ok=True)
        (self.private / "spawn_datasets").mkdir(parents=True, exist_ok=True)
        if self.best is None:
            self.best = self.candidate
        if self.best_rank is None:
            artifact = self.candidate.artifact
            try:
                source = (artifact.directory / "harness.py").read_text(encoding="utf-8")
                nodes = _fallback_stats(source)["nodes"]
            except (OSError, UnicodeError):
                nodes = 0
            self.best_rank = forge_rank(
                self.candidate.measurement,
                smoke_passed=None,
                subharnesses=0,
                components=artifact.component_count,
                nodes=nodes,
            )

    @property
    def is_game(self) -> bool:
        return self.candidate.artifact.spec.execution_config.get("kind") in _GAME_KINDS

    def left(self) -> dict[str, JsonValue]:
        settings = self.settings
        wallet = self.wallet
        usd = settings.max_usd - wallet.cost_usd if wallet is not None else settings.max_usd
        seconds = wallet.remaining_s if wallet is not None else settings.max_s
        return {
            "usd": round(max(0.0, usd), 6),
            "seconds": round(max(0.0, seconds), 3),
            "checks": max(0, settings.max_checks - self.checks),
            "dryruns": max(0, settings.max_dryruns - self.dryruns),
            "measures": max(0, settings.max_measures - self.measures),
            "submissions": max(0, settings.max_submissions - self.submissions),
            "spawns": max(0, settings.max_spawns - self.spawns),
        }

    def call_limit(self) -> int:
        """``ForgeBinding.call_limit`` per Section 4.9, computed at submit time."""
        if not self.is_game:
            return self.settings.call_limit
        limits = self.candidate.artifact.spec.execution_config.get("limits")
        turns = limits.get("max_turns") if isinstance(limits, dict) else None
        max_turns = turns if type(turns) is int and turns >= 1 else 256
        return min(100_000, max_turns * self.settings.calls_per_turn)

    def scratch(self) -> Path:
        self._scratch += 1
        directory = self.private / "scratch" / str(self._scratch)
        directory.mkdir(parents=True)
        return directory


def forge_rank(
    m: Measurement, *, smoke_passed: bool | None, subharnesses: int, components: int, nodes: int
) -> ForgeRank:
    """Section 8.8. Lower is better; a quality repair that costs more still ranks higher."""
    return (
        not m.floor_passed,
        not m.budget_passed,
        smoke_passed is False,
        -round(m.score, 6),
        round(m.mean_cost_usd, 9),
        subharnesses + components,
        nodes,
    )


def _dependency(name: str) -> Callable[..., Awaitable[Any]] | None:
    """Contracted runtime helpers (``measure_cases``, ``dry_run_case``), when present."""
    module = importlib.import_module("architect.runtime.evaluation")
    found = getattr(module, name, None)
    return cast(Callable[..., Awaitable[Any]], found) if callable(found) else None


def _fallback_stats(source: str) -> dict[str, int]:
    stats = {
        "nodes": 0,
        "node_limit": NODE_LIMIT_V2,
        "depth": 0,
        "helpers": 0,
        "constants": 0,
        "loops": 0,
        "await_sites": 0,
        "bytes": len(source.encode("utf-8", errors="replace")),
    }
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return stats
    pending: list[tuple[ast.AST, int]] = [(tree, 0)]
    while pending:
        node, depth = pending.pop()
        stats["nodes"] += 1
        stats["depth"] = max(stats["depth"], depth)
        if isinstance(node, (ast.For, ast.While)):
            stats["loops"] += 1
        elif isinstance(node, ast.Await):
            stats["await_sites"] += 1
        pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stats["helpers"] += statement.name != "run"
        elif isinstance(statement, ast.Assign):
            stats["constants"] += 1
    return stats


def _sandbox_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if not isinstance(code, str) and exc.args and isinstance(exc.args[0], str):
        code = exc.args[0]
    if isinstance(code, str) and _SANDBOX_CODE.fullmatch(code):
        return code
    return "sandbox_invalid_source"


def _validate(source: str, artifact: HarnessArtifact) -> _Validated:
    """Validate without executing; v1 builds return None, v2 builds return SourceStats."""
    validator: Callable[[str, HarnessArtifact], object] = validate_source
    stats = _fallback_stats(source)
    try:
        result = validator(source, artifact)
    except SandboxError as exc:
        line, col = getattr(exc, "line", None), getattr(exc, "col", None)
        rule, name = getattr(exc, "rule", None), getattr(exc, "name", None)
        code = _sandbox_code(exc)
        line = line if type(line) is int else None
        rule = rule if isinstance(rule, str) else None
        name = name if isinstance(name, str) else None
        found = tuple(
            refusal
            for refusal in getattr(exc, "refusals", ())
            if isinstance(refusal, Refusal) and _SANDBOX_CODE.fullmatch(refusal.code)
        )
        return _Validated(
            code,
            line,
            stats,
            rule=rule,
            name=name,
            col=col if type(col) is int else None,
            refusals=found or (Refusal(line, code, rule, name),),
        )
    except (ValueError, TypeError, OSError):
        return _Validated(
            "sandbox_invalid_source",
            None,
            stats,
            refusals=(Refusal(None, "sandbox_invalid_source", None, None),),
        )
    for key in ("nodes", "depth", "helpers", "constants", "loops", "await_sites", "bytes"):
        value = getattr(result, key, None)
        if type(value) is int and value >= 0:
            stats[key] = value
    return _Validated(None, None, stats)


def _read_draft(session: ForgeSession, path: str = "harness.py") -> str:
    """Read one workspace draft through trusted code, bounded to 64 KiB."""
    if not isinstance(path, str) or _DRAFT_PATH.fullmatch(path) is None:
        raise _ToolError("forge_invalid_source")
    target = session.workspace / path
    try:
        resolved = target.resolve(strict=True)
        if (
            not resolved.is_relative_to(session.workspace.resolve())
            or target.is_symlink()
            or not resolved.is_file()
        ):
            raise _ToolError("forge_invalid_source")
        with resolved.open("rb") as stream:
            raw = stream.read(_SOURCE_LIMIT + 1)
    except OSError:
        raise _ToolError("forge_invalid_source") from None
    if len(raw) > _SOURCE_LIMIT:
        raise _ToolError("forge_file_too_large")
    try:
        return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeError:
        raise _ToolError("forge_invalid_source") from None


def _referenced_children(source: str) -> tuple[str, ...]:
    """Literal names in ``context.harness("<name>", value)`` calls, in first-use order."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return ()
    names: dict[str, None] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "harness"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "context"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            names[node.args[0].value] = None
    return tuple(names)


def _artifact(session: ForgeSession, directory: Path, children: Sequence[str]) -> HarnessArtifact:
    base = session.candidate.artifact
    binding = ForgeBinding(
        language=2,
        call_limit=session.call_limit(),
        fuel=session.settings.fuel,
        subharnesses={name: session.children[name].ref for name in children},
    )
    return base.model_copy(
        update={
            "spec": base.spec.model_copy(deep=True),
            "directory": directory,
            "report": None,
            "execution_mode": "sandbox",
            "forge": binding,
        }
    )


def _materialize(
    session: ForgeSession, source: str, directory: Path, children: Sequence[str]
) -> HarnessArtifact:
    """Write the source and copy only the referenced children into ``subharnesses/``."""
    (directory / "harness.py").write_text(source, encoding="utf-8", newline="\n")
    for name in children:
        shutil.copytree(
            session.children[name].artifact.directory,
            directory / "subharnesses" / name,
            ignore=shutil.ignore_patterns(".consumed", ".pending-*"),
        )
    return _artifact(session, directory, children)


def _measurement_view(
    measurement: Measurement, floor: FloorEvaluator, mean_calls: float | None
) -> dict[str, JsonValue]:
    return {
        "score": measurement.score,
        "floor_limit": floor.definition.limit,
        "floor_passed": measurement.floor_passed,
        "budget_passed": measurement.budget_passed,
        "mean_cost_usd": measurement.mean_cost_usd,
        "mean_tokens": measurement.mean_tokens,
        "mean_calls": mean_calls,
        "max_compute_s": measurement.max_compute_s,
        "sample_count": measurement.sample_count,
        "failures": list(measurement.failures),
    }


def _bounded(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Drop per-case entries from the end until the result fits ``RESULT_CHARS``."""

    def size() -> int:
        return len(json.dumps(payload, ensure_ascii=True, allow_nan=False))

    while size() > RESULT_CHARS:
        for key in ("cases", "calls"):
            items = payload.get(key)
            if isinstance(items, list) and items:
                items.pop()
                payload["truncated"] = True
                break
        else:
            break
    return payload


def _result(
    session: ForgeSession, ok: bool, code: str | None, fields: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {"ok": ok, "code": code}
    payload.update(fields)
    payload["left"] = session.left()
    payload["truncated"] = False
    return _bounded(payload)


def _json_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


# forge_check -----------------------------------------------------------------


def _check_fields(
    validated: _Validated | None,
    lint: list[JsonValue],
    unbound: list[JsonValue],
    refused: str | None = None,
) -> dict[str, JsonValue]:
    """forge_check's fields. ``refused`` is a Forge-level code when the file was not read."""
    stats = validated.stats if validated is not None else _fallback_stats("")
    code = validated.code if validated is not None else refused
    line = validated.line if validated is not None else None
    rule = validated.rule if validated is not None else None
    name = validated.name if validated is not None else None
    listed: dict[str, JsonValue] = (
        validated.listed() if validated is not None else {"refusals": [], "refusals_total": 0}
    )
    return {
        "line": line,
        "col": validated.col if validated is not None else None,
        "rule": rule,
        "message": _message(code, line=line, rule=rule, name=name),
        **listed,
        "stats": cast(JsonValue, dict(stats)),
        "lint": lint,
        "unbound_children": unbound,
        "language": 2,
    }


def forge_check_tool(session: ForgeSession) -> TrustedTool:
    async def callback(args: dict[str, JsonValue]) -> JsonValue:
        session.checks += 1
        path = args.get("path", "harness.py")
        try:
            source = _read_draft(session, path if isinstance(path, str) else "")
        except _ToolError as exc:
            return _result(session, False, exc.code, _check_fields(None, [], [], exc.code))
        referenced = _referenced_children(source)
        bound = [name for name in referenced if name in session.children]
        unbound: list[JsonValue] = [name for name in referenced if name not in session.children]
        validated = _validate(source, _artifact(session, session.workspace, bound))
        lint: list[JsonValue] = (
            ["forge_literal_leak"] if leaked_literals(source, session.tuning) else []
        )
        return _result(
            session,
            validated.code is None,
            validated.code,
            _check_fields(validated, lint, unbound),
        )

    return TrustedTool(
        name="forge_check",
        description=_DESCRIPTIONS["forge_check"],
        parameters=CHECK_PARAMETERS,
        callback=_observing(session, "forge_check", callback),
        max_calls=session.settings.max_checks,
        max_result_chars=RESULT_CHARS,
    )


# forge_dryrun ----------------------------------------------------------------

_PASSING_VERDICT: Final = '{"passed": true, "reason": "The candidate matches the schema."}'
_DRYRUN_EMPTY: Final[dict[str, JsonValue]] = {
    "line": None,
    "sandbox_code": None,
    "rule": None,
    "message": None,
    "calls": [],
    "output_head": "",
    "schema_ok": False,
    "fuel_used": 0,
    "steps": 0,
}


_EMPTY_REPLY: Final = "{}"
# A refused line of a kind the list already showed: ``{line}`` is that earlier line.
_REPEAT: Final = "the same problem as line {line}."
# Fixed texts a tool result can show the engineer (the dry run's default replies appear in
# its calls and output). The Forge pack hash covers them (``forge_pack``).
TOOL_TEXTS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "dryrun_verdict": _PASSING_VERDICT,
        "dryrun_empty_reply": _EMPTY_REPLY,
        "refusal_repeat": _REPEAT,
    }
)


def _default_replies(session: ForgeSession) -> list[str]:
    """A reply shaped like the output schema (``forge_example``), then a passing verdict.

    Replies are used in turn, so a preset's answer and its own self-check (the
    extractor's default) both get the reply they parse, and the starting harness passes
    its first dry run. Game replies are actions, not outputs, so games keep ``{}``.
    """
    if session.is_game:
        return [_EMPTY_REPLY]
    shaped = example_reply(session.candidate.artifact.spec.output_schema)
    return [shaped, _PASSING_VERDICT] if shaped is not None else [_EMPTY_REPLY]


def _prepared(session: ForgeSession) -> tuple[str, HarnessArtifact]:
    """Read and validate the draft into a fresh scratch directory ($0)."""
    source = _read_draft(session)
    referenced = _referenced_children(source)
    bound = [name for name in referenced if name in session.children]
    validated = _validate(source, _artifact(session, session.workspace, bound))
    if validated.code is not None:
        raise _ToolError("forge_invalid_source", validated.line, validated)
    if len(bound) != len(referenced):
        raise _ToolError("forge_unbound_child")
    return source, _materialize(session, source, session.scratch(), bound)


def _invalid_case(session: ForgeSession, fields: dict[str, JsonValue]) -> dict[str, JsonValue]:
    last = max(0, len(session.tuning) - 1)
    return _result(
        session, False, "invalid_case", {**fields, "message": _message("invalid_case", last=last)}
    )


def forge_dryrun_tool(session: ForgeSession) -> TrustedTool:
    async def callback(args: dict[str, JsonValue]) -> JsonValue:
        session.dryruns += 1
        index = args.get("case")
        if type(index) is not int or not 0 <= index < len(session.tuning):
            return _invalid_case(session, dict(_DRYRUN_EMPTY))
        default = _default_replies(session)
        replies_value = args.get("replies", default)
        replies = (
            [str(item) for item in replies_value][:8]
            if isinstance(replies_value, list)
            else default
        ) or default
        steps_value = args.get("game_steps", 5)
        game_steps = steps_value if type(steps_value) is int and 1 <= steps_value <= 20 else 5
        unavailable = {
            **_DRYRUN_EMPTY, "message": _message("forge_dryrun_unavailable")
        }
        try:
            _, artifact = _prepared(session)
        except _ToolError as exc:
            return _result(
                session, False, exc.code, {**_DRYRUN_EMPTY, "line": exc.line, **exc.fields()}
            )
        run = _dependency("dry_run_case")
        if run is None:
            return _result(session, False, "forge_dryrun_unavailable", dict(unavailable))
        try:
            dry = await run(
                artifact, session.floor, index, replies, session.runtime, game_steps=game_steps
            )
        except SandboxError as exc:
            line = getattr(exc, "line", None)
            line = line if type(line) is int else None
            sandbox = _sandbox_code(exc)
            rule = getattr(exc, "rule", None)
            invalid = {
                **_DRYRUN_EMPTY,
                "line": line,
                "sandbox_code": sandbox,
                "rule": rule if isinstance(rule, str) else None,
                "message": _message(
                    sandbox, line=line, rule=rule if isinstance(rule, str) else None
                ),
            }
            return _result(session, False, "forge_invalid_source", invalid)
        except ValueError as exc:
            if str(exc) == "forge_dryrun_unavailable":
                return _result(session, False, "forge_dryrun_unavailable", dict(unavailable))
            raise
        finally:
            shutil.rmtree(artifact.directory, ignore_errors=True)
        data = dry.model_dump(mode="json") if hasattr(dry, "model_dump") else {}
        if not isinstance(data, dict):
            data = {}
        code = data.get("code")
        code = code if isinstance(code, str) and len(code) <= 64 else None
        ok = data.get("ok") is True
        fields: dict[str, JsonValue] = {
            key: cast(JsonValue, data.get(key, default)) for key, default in _DRYRUN_EMPTY.items()
        }
        calls = fields.get("calls")
        fields["calls"] = calls[:8] if isinstance(calls, list) else []
        line = fields.get("line")
        line = line if type(line) is int else None
        # A failed run says why (a runtime code's fixed hint, at its line); a run whose
        # output misses the schema says so too, though it is not an error.
        if not ok:
            fields["message"] = _message(code, line=line)
        elif fields.get("schema_ok") is False:
            fields["message"] = _message("schema_mismatch")
        return _result(session, ok, code, fields)

    return TrustedTool(
        name="forge_dryrun",
        description=_DESCRIPTIONS["forge_dryrun"],
        parameters=DRYRUN_PARAMETERS,
        callback=_observing(session, "forge_dryrun", callback),
        max_calls=session.settings.max_dryruns,
        max_result_chars=RESULT_CHARS,
    )


# forge_measure ---------------------------------------------------------------


def _case_view(detail: object, mode: str) -> dict[str, JsonValue]:
    """A leakage-safe per-case view of a ``CaseDetail`` (tuning cases only)."""
    data = detail.model_dump(mode="json") if hasattr(detail, "model_dump") else {}
    if not isinstance(data, dict):
        data = {}

    def excerpt(value: object) -> JsonValue:
        return value[:_EXCERPT_CHARS] if isinstance(value, str) and mode == "available" else None

    game = data.get("game")
    failure = data.get("failure")
    line = data.get("sandbox_line")
    calls = data.get("calls")
    index = data.get("index")
    view: dict[str, JsonValue] = {
        "index": index if type(index) is int else None,
        "passed": data.get("passed") if mode == "available" else None,
        "score": _json_number(data.get("score")) if mode == "available" else None,
        "failure": failure if isinstance(failure, str) and len(failure) <= 64 else None,
        "sandbox_line": line if type(line) is int else None,
        "calls": calls if type(calls) is int else 0,
        "cost_usd": _json_number(data.get("cost_usd")) or 0.0,
        "output_excerpt": excerpt(data.get("output_excerpt")),
        "expected_excerpt": excerpt(data.get("expected_excerpt")),
        "game": cast(JsonValue, game) if isinstance(game, dict) else None,
    }
    if view["passed"] is not None and not isinstance(view["passed"], bool):
        view["passed"] = None
    return view


_DETAIL_KEYS: Final = ("output_excerpt", "expected_excerpt")


def _first_failures(
    views: list[dict[str, JsonValue]], limit: int, *, per_case: bool = True
) -> list[dict[str, JsonValue]]:
    """Keep the detail (excerpts, game trace) of the first ``limit`` failed cases only.

    Every case keeps its compact row (index, pass, score, failure code, calls, cost) and
    its game metrics; a passing case needs no excerpt or trace, and later failures usually
    repeat the first ones. Without per-case results (``per_case`` False: aggregate or
    unavailable feedback) no case is known to pass, so the views are left as they are.
    """
    if not per_case:
        return views
    failures = 0
    for view in views:
        failed = view.get("passed") is False or view.get("failure") is not None
        if failed and failures < limit:
            failures += 1
            continue
        for key in _DETAIL_KEYS:
            view[key] = None
        game = view.get("game")
        if isinstance(game, dict) and game.get("trace") is not None:
            view["game"] = {**game, "trace": None}
    return views


def _per_case_mode(session: ForgeSession, floor: FloorEvaluator) -> str:
    if session.settings.feedback == "aggregate":
        return "aggregate"
    definition = floor.definition
    if definition.kind == FloorKind.HUMAN_EDIT_RATE and definition.minimum_samples > 1:
        return "unavailable"
    return "available"


def _baseline_view(session: ForgeSession) -> dict[str, JsonValue]:
    measurement = session.candidate.measurement
    return {
        "score": measurement.score,
        "mean_cost_usd": measurement.mean_cost_usd,
        "floor_passed": measurement.floor_passed,
    }


def forge_measure_tool(session: ForgeSession) -> TrustedTool:
    empty: dict[str, JsonValue] = {
        "line": None,
        "sandbox_code": None,
        "rule": None,
        "message": None,
        "measurement": None,
        "per_case": None,
        "cases": [],
        "baseline": None,
    }

    async def callback(args: dict[str, JsonValue]) -> JsonValue:
        session.measures += 1
        smoke = args.get("smoke", False) is True
        trace = args.get("trace", False) is True and session.is_game
        floor = session.smoke_floor if smoke else session.floor
        pool = session.smoke_tuning if smoke else session.tuning
        requested = args.get("cases")
        indexes = (
            list(range(min(session.settings.measure_cases, len(pool))))
            if requested is None
            else requested
        )
        if (
            floor is None
            or not isinstance(indexes, list)
            or not indexes
            or len(indexes) > 32
            or any(type(item) is not int or not 0 <= item < len(pool) for item in indexes)
            or len(set(cast(list[int], indexes))) != len(indexes)
        ):
            last = max(0, len(pool) - 1)
            message = _message("invalid_case", last=last) if floor is not None else None
            return _result(
                session, False, "invalid_case", {**empty, "baseline": None, "message": message}
            )
        try:
            _, artifact = _prepared(session)
        except _ToolError as exc:
            return _result(session, False, exc.code, {**empty, "line": exc.line, **exc.fields()})
        measure = _dependency("measure_cases")
        if measure is None:
            shutil.rmtree(artifact.directory, ignore_errors=True)
            return _result(session, False, "tool_failed", dict(empty))
        mode = _per_case_mode(session, floor)
        try:
            measurement, details = await measure(
                artifact,
                floor,
                cast(list[int], indexes),
                session.adapter_factory,
                session.runtime,
                excerpt_chars=_EXCERPT_CHARS,
                trace=trace,
                per_case=mode != "aggregate",
            )
        except SandboxError as exc:
            line = getattr(exc, "line", None)
            line = line if type(line) is int else None
            sandbox = _sandbox_code(exc)
            return _result(
                session,
                False,
                "forge_invalid_source",
                {
                    **empty,
                    "line": line,
                    "sandbox_code": sandbox,
                    "message": _message(sandbox, line=line),
                },
            )
        except ValueError as exc:
            if str(exc) == "forge_measure_too_small":
                too_small = {**empty, "message": _message("forge_measure_too_small")}
                return _result(session, False, "forge_measure_too_small", too_small)
            raise
        finally:
            shutil.rmtree(artifact.directory, ignore_errors=True)
        views = _first_failures(
            [_case_view(detail, mode) for detail in details],
            session.settings.max_failures_shown,
            per_case=mode == "available",
        )
        calls = [cast(int, view["calls"]) for view in views]
        mean_calls = sum(calls) / len(calls) if calls else None
        exhausted = (
            "budget_exceeded" in measurement.failures
            and session.wallet is not None
            and session.wallet.exhausted
        )
        return _result(
            session,
            not exhausted,
            "budget_exceeded" if exhausted else None,
            {
                "line": None,
                "message": _message("budget_exceeded") if exhausted else None,
                "measurement": _measurement_view(measurement, floor, mean_calls),
                "per_case": mode,
                "cases": cast(JsonValue, views),
                "baseline": _baseline_view(session),
            },
        )

    return TrustedTool(
        name="forge_measure",
        description=_DESCRIPTIONS["forge_measure"],
        parameters=MEASURE_PARAMETERS,
        callback=_observing(session, "forge_measure", callback),
        max_calls=session.settings.max_measures,
        max_result_chars=RESULT_CHARS,
    )


# forge_submit ----------------------------------------------------------------


def _full(measurement: Measurement, count: int) -> bool:
    return measurement.floor_passed and measurement.sample_count == count


@dataclass
class _Taken:
    """What one submission produced for the audit: the written candidate and its
    tuning-side measurements (never anything held out)."""

    number: int = 0
    candidate: Path | None = None
    measurement: Measurement | None = None
    smoke_passed: bool | None = None


async def submit_source(
    session: ForgeSession, source: str, note: str | None = None
) -> tuple[str | None, dict[str, JsonValue]]:
    """The ``forge_submit`` gate (Section 9.4), also run once in process by template mode.

    With an audit session (Phase 14, Section 8.3) every call is recorded exactly once,
    after its verdict; a record that cannot be written raises ``AuditStopped``.
    """
    taken = _Taken()
    code, fields = await _submit(session, source, note, taken)
    if code is not None and fields.get("message") is None:
        line = fields.get("line")
        fields["message"] = _message(code, line=line if type(line) is int else None)
    audit = getattr(session, "audit", None)
    if audit is not None:
        try:
            audit.record(
                number=taken.number,
                source=source,
                candidate=taken.candidate,
                verdict=code or "accepted",
                measurement=taken.measurement,
                smoke_passed=taken.smoke_passed,
                floor_limit=session.floor.definition.limit,
                tuning_cases=session.tuning,  # what the measurement ran on (hash only)
            )
        except AuditError:
            raise AuditStopped("Forge audit could not be written") from None
    return code, fields


async def _submit(
    session: ForgeSession, source: str, note: str | None, taken: _Taken
) -> tuple[str | None, dict[str, JsonValue]]:
    session.submissions += 1
    number = session.submissions
    taken.number = number
    fields: dict[str, JsonValue] = {
        "sandbox_code": None,
        "line": None,
        "rule": None,
        "message": None,
        "candidate": number,
        "accepted": False,
        "best_so_far": False,
        "measurement": None,
        "smoke": None,
        "submissions_left": max(0, session.settings.max_submissions - number),
    }
    if number > session.settings.max_submissions:
        return "forge_submissions_exhausted", fields
    if note is not None:
        session.notes.append((len(note), hashlib.sha256(note.encode("utf-8")).hexdigest()))
    referenced = _referenced_children(source)
    bound = [name for name in referenced if name in session.children]
    unbound = [name for name in referenced if name not in session.children]
    # 1. validate
    validated = _validate(source, _artifact(session, session.workspace, bound))
    if validated.code is not None:
        fields.update({"sandbox_code": validated.code, "line": validated.line})
        if unbound and validated.code == "sandbox_binding_mismatch":
            return "forge_unbound_child", fields
        fields["rule"] = validated.rule
        fields["message"] = _message(
            validated.code, line=validated.line, rule=validated.rule, name=validated.name
        )
        fields.update(validated.listed())
        return "forge_invalid_source", fields
    # 2. lint
    leaks = leaked_literals(source, session.tuning)
    if leaks:
        fields["line"] = leaks[0]
        return "forge_literal_leak", fields
    # 3. child binding
    if unbound:
        return "forge_unbound_child", fields
    if set(session.children) & set(bound) != set(bound):
        return "forge_unused_child", fields
    # 4. write the candidate into the forge-private store
    try:
        directory = session.private / "candidates" / str(number)
        directory.mkdir(parents=True)
        artifact = _materialize(session, source, directory, bound)
        artifact.save()
    except (OSError, ValueError):
        return "persistence_failed", fields
    taken.candidate = directory
    # 5. full tuning measurement; 6. smoke
    measurement = await evaluate_cases(
        artifact, session.floor, session.tuning, session.adapter_factory, session.runtime
    )
    taken.measurement = measurement
    fields["measurement"] = _measurement_view(measurement, session.floor, None)
    smoke_passed: bool | None = None
    if session.smoke_floor is not None:
        smoke = await evaluate_cases(
            artifact,
            session.smoke_floor,
            session.smoke_tuning,
            session.adapter_factory,
            session.runtime,
        )
        smoke_passed = _full(smoke, len(session.smoke_tuning)) and smoke.budget_passed
        taken.smoke_passed = smoke_passed
        fields["smoke"] = {"floor_passed": smoke_passed, "score": smoke.score}
    else:
        fields["smoke"] = "absent"
    # 7. rank
    rank = forge_rank(
        measurement,
        smoke_passed=smoke_passed,
        subharnesses=len(bound),
        components=artifact.component_count,
        nodes=validated.stats["nodes"],
    )
    if not _full(measurement, len(session.tuning)):
        return "tuning_failed", fields
    if smoke_passed is False:
        return "smoke_failed", fields
    if session.best_rank is not None and not rank < session.best_rank:
        return "not_better", fields
    session.best = CandidateEvaluation(artifact=artifact, measurement=measurement)
    session.best_rank = rank
    session.best_number = number
    session.best_children = tuple(bound)
    session.best_source_bytes = len(source.encode("utf-8"))
    session.best_nodes = validated.stats["nodes"]
    session.accepted += 1
    fields.update({"accepted": True, "best_so_far": True})
    return None, fields


def forge_submit_tool(session: ForgeSession) -> TrustedTool:
    async def callback(args: dict[str, JsonValue]) -> JsonValue:
        note_value = args.get("note")
        note = note_value[:200] if isinstance(note_value, str) else None
        source: str | None = None
        try:
            source = _read_draft(session)
        except _ToolError as exc:
            # An unreadable draft still uses a submission attempt (Section 9.4).
            session.submissions += 1
            code: str | None = exc.code
            fields: dict[str, JsonValue] = {
                "sandbox_code": None,
                "line": None,
                "rule": None,
                "message": _message(exc.code),
                "candidate": session.submissions,
                "accepted": False,
                "best_so_far": False,
                "measurement": None,
                "smoke": None,
                "submissions_left": max(
                    0, session.settings.max_submissions - session.submissions
                ),
            }
        else:
            code, fields = await submit_source(session, source, note)
        result = _result(session, code is None, code, fields)
        _notify(session, "forge_submit", result, source)
        if session.submissions >= session.settings.max_submissions:
            # Section 8.7: the session ends once every submission has been used,
            # whichever gate the last attempt stopped at.
            raise SubmissionsExhausted("Forge submissions exhausted")
        return result

    return TrustedTool(
        name="forge_submit",
        description=_DESCRIPTIONS["forge_submit"],
        parameters=SUBMIT_PARAMETERS,
        callback=callback,
        max_calls=session.settings.max_submissions,
        max_result_chars=RESULT_CHARS,
    )


# forge_spawn -----------------------------------------------------------------


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _schema_sha256(schema: dict[str, JsonValue]) -> str:
    return hashlib.sha256(_canonical(schema).encode("utf-8")).hexdigest()


def _schema_validator(value: object) -> Draft202012Validator:
    if not isinstance(value, dict) or len(_canonical(value).encode("utf-8")) > _SCHEMA_LIMIT:
        raise _ToolError("invalid_schema")
    pending: list[object] = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                if key in {"$ref", "$dynamicRef", "$recursiveRef"} and (
                    not isinstance(child, str) or not child.startswith("#")
                ):
                    raise _ToolError("invalid_schema")
                pending.append(child)
        elif isinstance(item, list):
            pending.extend(item)
    try:
        Draft202012Validator.check_schema(value)
        return Draft202012Validator(value)
    except Exception:
        raise _ToolError("invalid_schema") from None


_MISSING: Final = object()


def _pointer(document: JsonValue, pointer: str) -> object:
    """RFC 6901 resolution; ``_MISSING`` for any absent member or index."""
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        return _MISSING
    current: object = document
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                return _MISSING
            current = current[token]
        elif isinstance(current, list):
            if not token.isdecimal() or (token != "0" and token.startswith("0")):
                return _MISSING
            position = int(token)
            if position >= len(current):
                return _MISSING
            current = current[position]
        else:
            return _MISSING
    return current


def _spawn_cases(
    session: ForgeSession,
    cases: object,
    input_check: Draft202012Validator,
    output_check: Draft202012Validator,
) -> tuple[Literal["projection", "authored"], list[EvaluationCase]]:
    if not isinstance(cases, dict) or len(cases) != 1:
        raise _ToolError("invalid_cases")
    pairs: list[tuple[JsonValue, JsonValue]] = []
    origin: Literal["projection", "authored"]
    if "projection" in cases:
        origin = "projection"
        if session.is_game:
            raise _ToolError("spawn_kind_unsupported")
        projection = cases["projection"]
        if not isinstance(projection, dict):
            raise _ToolError("invalid_cases")
        source, target = projection.get("input"), projection.get("expected")
        if not isinstance(source, str) or not isinstance(target, str):
            raise _ToolError("invalid_cases")
        # Built from parent tuning only: the parent's held-out cases never reach a child.
        for case in session.tuning:
            value, expected = _pointer(case.task, source), _pointer(case.expected, target)
            if value is _MISSING or expected is _MISSING:
                continue
            item = (cast(JsonValue, value), cast(JsonValue, expected))
            if input_check.is_valid(item[0]) and output_check.is_valid(item[1]):
                pairs.append(item)
    elif "authored" in cases:
        origin = "authored"
        authored = cases["authored"]
        if not isinstance(authored, list) or not 8 <= len(authored) <= 64:
            raise _ToolError("invalid_cases")
        for entry in authored:
            if not isinstance(entry, dict) or set(entry) != {"input", "expected"}:
                raise _ToolError("invalid_cases")
            if not input_check.is_valid(entry["input"]) or not output_check.is_valid(
                entry["expected"]
            ):
                raise _ToolError("invalid_cases")
            pairs.append((entry["input"], entry["expected"]))
    else:
        raise _ToolError("invalid_cases")
    if len(pairs) < 8:
        raise _ToolError("invalid_cases")
    try:
        return origin, [
            EvaluationCase(case_id=f"c{index:03d}", task=value, expected=expected)
            for index, (value, expected) in enumerate(pairs)
        ]
    except ValueError:
        raise _ToolError("invalid_cases") from None


def _spawn_fields(session: ForgeSession, name: str) -> dict[str, JsonValue]:
    return {
        "name": name,
        "licensed": False,
        "preset": None,
        "execution_mode": None,
        "tuning": None,
        "verified": None,
        "case_origin": None,
        "cost_usd": 0.0,
        "call_as": None,
        "spawns_left": max(0, session.settings.max_spawns - session.spawns),
    }


def spawn_settings(
    settings: ForgeSettings, depth: int, usd: float, seconds: float
) -> ForgeSettings:
    """``child_settings(depth)`` with money and time set to the spawn allotment (11.3)."""
    child = settings.child_settings(depth)
    return ForgeSettings.model_validate(
        {
            **child.model_dump(),
            "max_usd": min(child.max_usd, usd),
            "max_s": min(child.max_s, seconds),
            "spawn_max_usd": min(child.spawn_max_usd, usd),
        }
    )


async def _spawn(
    session: ForgeSession, args: dict[str, JsonValue]
) -> tuple[str | None, dict[str, JsonValue]]:
    from architect.compiler import CompileConfig, Compiler

    settings = session.settings
    raw_name = args.get("name")
    name = raw_name if isinstance(raw_name, str) else ""
    fields = _spawn_fields(session, name)
    if session.depth >= settings.max_depth:
        return "forge_depth", fields
    if session.spawns >= settings.max_spawns:
        return "forge_spawn_cap", fields
    if _CHILD_NAME.fullmatch(name) is None:
        return "invalid_arguments", fields
    if name in session.spawn_names or name in session.children:
        return "forge_spawn_name_taken", fields
    goal = args.get("goal")
    if not isinstance(goal, str):
        return "invalid_arguments", fields
    try:
        input_check = _schema_validator(args.get("input_schema"))
        output_check = _schema_validator(args.get("output_schema"))
        origin, cases = _spawn_cases(session, args.get("cases"), input_check, output_check)
    except _ToolError as exc:
        return exc.code, fields
    wallet = session.wallet
    remaining_usd = settings.max_usd - wallet.cost_usd if wallet is not None else settings.max_usd
    remaining_s = wallet.remaining_s if wallet is not None else settings.max_s
    requested = _json_number(args.get("max_usd"))
    usd = min(
        requested if requested is not None and requested > 0 else math.inf,
        settings.spawn_max_usd,
        settings.spawn_share * remaining_usd,
    )
    seconds = settings.spawn_share * remaining_s
    if not usd > 0 or not seconds > 0:
        return "budget_exceeded", fields
    session.spawns += 1
    session.spawn_names.add(name)
    fields["spawns_left"] = max(0, settings.max_spawns - session.spawns)
    parent = session.candidate.artifact
    dataset = session.private / "spawn_datasets" / f"{name}.jsonl"
    try:
        with dataset.open("w", encoding="utf-8", newline="\n") as stream:
            for case in cases:
                stream.write(case.model_dump_json() + "\n")
        limit = _json_number(args.get("floor_limit"))
        definition = FloorDefinition(
            kind=FloorKind.HELD_OUT_TASKS,
            limit=limit if limit is not None else 0.8,
            dataset=str(dataset),
            minimum_samples=max(1, len(cases) // 2),
        )
        hint = args.get("preset_hint")
        budget = Budget(
            cost_usd=min(parent.spec.budget.cost_usd, usd),
            compute_s=min(parent.spec.budget.compute_s, seconds),
        )
        child_spec = Spec(
            task=f"{goal}\n\nCase origin: {origin}.",
            input_schema=cast(dict[str, JsonValue], args["input_schema"]),
            output_schema=cast(dict[str, JsonValue], args["output_schema"]),
            floor=definition,
            budget=budget,
            model_target=parent.model,
            lifecycle=Lifecycle.INSTALL,
            domain=parent.spec.domain,
            preset_hint=cast(Any, hint) if isinstance(hint, str) and hint in _PRESETS else None,
            tools=(),
        )
    except (OSError, ValueError):
        return "invalid_schema", fields

    def child_floor(requested: FloorDefinition) -> FloorEvaluator:
        if requested == definition:
            return HeldOutTasks(definition, cases)
        return session.floor_factory(requested)

    # Section 8.1/11.3: money and time are filled by the caller with the spawn
    # allotment, so a forging child's wallets, deadline and budget line are sized by
    # the spawn wallet and never by the parent's own Forge caps.
    child_settings = spawn_settings(settings, session.depth + 1, usd, seconds)
    compiler = Compiler(
        session.private / "children",
        session.runtime,
        session.adapter_factory,
        child_floor,
        config=CompileConfig(
            model_ladder=(parent.model,),
            allowed_presets=cast(Any, session.allowed_presets),
            enable_rewriting=False,
            max_rewrites=0,
            forge=child_settings,
            compile_budget=Budget(cost_usd=usd, compute_s=seconds),
        ),
        library=CHILD_LIBRARY.get() or NullLibrary(),
        smoke_factory=child_floor if child_settings.enabled else None,
        forge_adapter=session.agent_adapter,
        pi_config=session.pi_config,
    )
    compiler._forge_depth = session.depth + 1
    spent_before = wallet.cost_usd if wallet is not None else 0.0
    try:
        with compilation_wallet(Budget(cost_usd=usd, compute_s=seconds)) as spawn_wallet:
            # The spawn's compute_s is a deadline, not only a reserve-time check.
            async with asyncio.timeout(max(spawn_wallet.remaining_s, 0.001)):
                result = await compiler.compile(child_spec, approved=True)
            artifact = result.artifact
            if artifact is None or not artifact.licensed:
                code = (
                    "budget_exceeded"
                    if "compile_budget" in result.failures
                    else "not_licensed"
                    if artifact is not None
                    else "compile_failed"
                )
                return code, fields
            floor = child_floor(definition)
            tuning = await evaluate_cases(
                artifact, floor, floor.tuning, session.adapter_factory, session.runtime
            )
    except (BudgetExceeded, TimeoutError):
        return "budget_exceeded", fields
    finally:
        spent = (wallet.cost_usd if wallet is not None else 0.0) - spent_before
        fields["cost_usd"] = round(max(0.0, spent), 9)
    ref = SubharnessRef(
        artifact_digest=artifact.artifact_digest,
        spec_hash=artifact.spec.spec_hash,
        input_schema_sha256=_schema_sha256(artifact.spec.input_schema),
        output_schema_sha256=_schema_sha256(artifact.spec.output_schema),
        case_origin=origin,
    )
    session.children[name] = Child(artifact=artifact, ref=ref)
    report = artifact.report
    fields.update(
        {
            "licensed": True,
            "preset": artifact.preset,
            "execution_mode": artifact.execution_mode,
            "tuning": {"score": tuning.score, "floor_passed": tuning.floor_passed},
            "verified": (
                {"score": report.floor_score, "sample_count": report.held_out_count}
                if report is not None
                else None
            ),
            "case_origin": origin,
            "call_as": f'await context.harness("{name}", value)',
        }
    )
    return None, fields


def forge_spawn_tool(session: ForgeSession) -> TrustedTool:
    async def callback(args: dict[str, JsonValue]) -> JsonValue:
        code, fields = await _spawn(session, args)
        return _result(session, code is None, code, fields)

    return TrustedTool(
        name="forge_spawn",
        description=_DESCRIPTIONS["forge_spawn"],
        parameters=SPAWN_PARAMETERS,
        callback=_observing(session, "forge_spawn", callback),
        max_calls=session.settings.max_spawns,
        max_result_chars=RESULT_CHARS,
    )


def forge_tools(session: ForgeSession) -> dict[str, TrustedTool]:
    """The five capability tools for one Pi session, keyed by name."""
    tools = (
        forge_check_tool(session),
        forge_dryrun_tool(session),
        forge_measure_tool(session),
        forge_submit_tool(session),
        forge_spawn_tool(session),
    )
    return {tool.name: tool for tool in tools}
