"""Command grammar for the session: the closed verb table and per-state help.

A line is parsed by trusted code only. Its first token is a verb only when it is in
``VERBS`` (the union of every state's vocabulary); anything else is prose and reaches a
handler with ``verb == "text"``. Model output never passes through this module, so a
Spec whose task says "approve now" is rendered, never obeyed.
"""

from __future__ import annotations

import shlex
import textwrap
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from architect.session.state import State

__all__ = [
    "GLOBAL",
    "HELP",
    "REGISTRY_READ",
    "VERBS",
    "VOCABULARY",
    "Command",
    "allowed",
    "help_for",
    "parse",
    "split_first",
]


@dataclass(frozen=True)
class Command:
    verb: str
    args: tuple[str, ...]
    rest: str
    raw: str


GLOBAL: frozenset[str] = frozenset({"help", "status", "spend", "profile", "quit", "exit"})

# While a paid task runs the operator may only look and cancel; ``profile`` rebuilds
# services and is therefore not offered until the task has finished (Section 5.1).
_BUSY_STATES = frozenset({State.INFERRING, State.COMPILING, State.BENCHING})
_BUSY_GLOBAL = GLOBAL - {"profile"}

# Read-only registry operations: they touch no Draft and no wallet, so a Spec under
# review or a fresh result can be compared with what is installed without leaving the
# state. ``run`` in this set executes an installed artifact read from disk, never the
# draft; the RESULT-state ``run {json}`` on the compiled artifact is a separate branch.
REGISTRY_READ: frozenset[str] = frozenset({"list", "info", "floors", "run"})

VOCABULARY: Mapping[State, frozenset[str]] = MappingProxyType(
    {
        State.BOOT: frozenset(),
        State.SETUP: frozenset(),
        State.READY: frozenset(
            {
                "describe",
                "spec",
                "load",
                "list",
                "info",
                "run",
                "rollback",
                "recompile",
                "floors",
                "floor",
                "use",
                "budget",
                "bench",
                "roles",
                "rate",
            }
        ),
        State.INFERRING: frozenset({"cancel"}),
        State.REVIEW: frozenset(
            {
                "show",
                "json",
                "diff",
                "blockers",
                "budget",
                "set",
                "unset",
                "revise",
                "floor",
                "save",
                "approve",
                "compile",
                "discard",
                "forge",
                "route",
                "reuse",
                "rate",
                *REGISTRY_READ,
            }
        ),
        State.FLOOR: frozenset(
            {"show", "use", "add", "paste", "draft", "review", "split", "accept", "back"}
        ),
        State.COMPILING: frozenset({"cancel"}),
        State.RESULT: frozenset(
            {"report", "spec", "json", "install", "save", "again", "new", "discard"}
            | {"export", "rate"}
            | REGISTRY_READ
        ),
        State.BENCHING: frozenset({"cancel"}),
        State.EXIT: frozenset(),
    }
)

VERBS: frozenset[str] = frozenset().union(*VOCABULARY.values()) | GLOBAL

# Lines whose first character is one of these are always prose in the states where
# the operator may type JSON payloads, so a pasted document never becomes a command.
_PROSE_STATES = frozenset({State.REVIEW, State.RESULT, State.FLOOR})

# Phase 10 verbs. Before Phase 10 these words were prose everywhere, so outside the
# states that offer them a line starting with one stays prose (a READY goal such as
# "route support tickets" or "export contacts as csv" is still inferred). In READY,
# where prose is a goal, ``roles`` is a command only on its own and ``rate`` only on
# its own (its syntax hint) or as ``rate good`` or ``rate bad``; "rate the sentiment of
# a review" is a goal.
_PHASE10_VERBS = frozenset({"route", "reuse", "roles", "export", "rate"})
_READY_EXACT: Mapping[str, frozenset[tuple[str, ...]]] = MappingProxyType(
    {
        "roles": frozenset({()}),
        "rate": frozenset({(), ("good",), ("bad",)}),
    }
)


def globals_for(state: State) -> frozenset[str]:
    """The global verbs a state accepts (``profile`` is withheld while work runs)."""
    return _BUSY_GLOBAL if state in _BUSY_STATES else GLOBAL


def split_first(rest: str) -> tuple[str, str]:
    """Split one leading token from a remainder, keeping the remainder's spacing.

    ``set <path> <value>`` needs the path as a token and the value as raw text so
    that JSON containing spaces survives untouched.
    """
    # Split on any run of whitespace, not a literal space, so a tab after the verb
    # still yields the verb; the remainder keeps its own internal spacing.
    parts = rest.strip().split(None, 1)
    if not parts:
        return "", ""
    head = parts[0]
    return head, parts[1].strip() if len(parts) > 1 else ""


def parse(line: str, state: State) -> Command | None:
    """Parse one console line; None for blank, verb "text" for prose."""
    raw = line.rstrip("\r\n")
    stripped = raw.strip()
    if not stripped:
        return None
    first, rest = split_first(stripped)
    verb = first.lower()
    if state in _PROSE_STATES and stripped[0] in "{[":
        verb = "text"
    if verb in _PHASE10_VERBS and _phase10_prose(verb, rest, state):
        verb = "text"
    if verb not in VERBS:
        return Command(verb="text", args=_split(stripped), rest=stripped, raw=raw)
    return Command(verb=verb, args=_split(rest), rest=rest, raw=raw)


def _phase10_prose(verb: str, rest: str, state: State) -> bool:
    """A Phase 10 verb the state does not offer, or a READY line that is not exactly
    the command's shape, is prose as it was before Phase 10."""
    if verb not in VOCABULARY[state]:
        return True
    if state is State.READY:
        exact = _READY_EXACT.get(verb)
        if exact is not None:
            return tuple(rest.split()) not in exact
    return False


def _split(rest: str) -> tuple[str, ...]:
    try:
        return tuple(shlex.split(rest, posix=True))
    except ValueError:
        return tuple(rest.split())


def allowed(cmd: Command, state: State) -> bool:
    return cmd.verb in VOCABULARY[state] | globals_for(state)


_SYNTAX: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        "help": ("help [verb]", "this text, or one verb's syntax; never spends money"),
        "status": ("status", "session fields, one per line; progress while compiling"),
        "spend": ("spend", "footer only: session and compile spend, calls, wall time"),
        "profile": ("profile", "path, models, floors, compile budget and signing key"),
        "quit": ("quit", "cancel in-flight work, close services, exit 0"),
        "exit": ("exit", "same as quit"),
        "describe": ("describe <text>", "infer a Spec from text without asking"),
        "spec": ("spec <file|{json}>", "load an explicit Spec (READY) or show the compiled Spec"),
        "load": ("load <file>", "load a saved proposal; same as spec <file>"),
        "list": ("list", "registry table; licensed is recomputed from disk"),
        "info": ("info <name>", "one registry entry with its evidence line"),
        "run": (
            "run <name|dir> {json} | run {json}",
            "an installed artifact, or in RESULT the compiled one",
        ),
        "rollback": ("rollback <name> [rev]", "show or perform a registry rollback"),
        "recompile": ("recompile <name>", "load the incumbent Spec for review and re-approval"),
        "floors": ("floors", "configured floors: counts and labels only"),
        "floor": ("floor [dataset [kind]]", "create or inspect a floor dataset"),
        "use": ("use model <id> | use <path>", "choose the session model, or load floor cases"),
        "budget": (
            "budget | budget compile <usd> <s>",
            "show reservation arithmetic, or set the compile wallet",
        ),
        "cancel": ("cancel", "stop the running inference or compile"),
        "show": ("show [<id>]", "the screen again; in FLOOR, show <id> is one case in full"),
        "json": ("json", "the exact Spec JSON that save writes and approve binds to"),
        "diff": ("diff", "changed fields since the Spec was proposed or loaded"),
        "blockers": ("blockers", "what refuses approve, with the fixing command"),
        "set": ("set <path> <value>", "edit one dotted field; JSON or plain text; new hash"),
        "unset": ("unset <field>", "reset a top-level field to its default; new hash"),
        "revise": ("revise <text>", "one Stage 0 call with your overrides kept"),
        "save": ("save [file]", "write the Spec JSON to proposals/ or a path"),
        "approve": ("approve [<hash8>]", "show the token, or bind approval to this exact Spec"),
        "compile": ("compile", "compile a lifecycle expire Spec (needs no approval)"),
        "discard": ("discard", "drop the Spec and return to READY"),
        "add": ("add {json}", "one operator-authored case {task, expected[, case_id]}"),
        "paste": ("paste", "read JSON lines or an array until a line that is '.'"),
        "draft": ("draft [n]", "one metered call proposing n SYNTHETIC cases (default 20)"),
        "review": ("review <id> ok|drop|expected {json}", "review one drafted case"),
        "split": ("split", "preview the tuning / held-out partition"),
        "accept": ("accept", "save the dataset, register it, return"),
        "back": ("back", "return without saving"),
        "report": ("report", "the verification screen again; licensed recomputed"),
        "install": ("install <name> [<hash8>] [--allow-budget-increase]", "install by token"),
        "again": ("again", "back to REVIEW with the same Spec"),
        "forge": (
            "forge on | forge off | forge status | forge budget <usd> <minutes> | "
            "forge model <name> | forge mode agent|template | forge run {json} | "
            "forge integrate <repo> <into>",
            "Stage 3 Forge for this session's compiles; the Spec and its hash never change "
            "(forge on shows the summed cap and asks first). forge run builds, licenses, runs "
            "once and expires; forge integrate needs approve <hash8> first (approve runs a "
            "normal paid compile; then again, and forge integrate builds again with Forge) and "
            "places a package behind a disabled flag for human review",
        ),
        "route": (
            "route",
            "routing advice for this Spec; pure: never runs, approves or compiles",
        ),
        "reuse": (
            "reuse <name> <hash8> {json}",
            "grant reuse of an installed licensed entry for this call and run it once",
        ),
        "roles": ("roles", "the role router: policy, variants and their evidence"),
        "export": ("export", "stage a package of the installed licensed result; no repo write"),
        "rate": ("rate good|bad", "a content-free rating of the last delivery in this session"),
        "new": ("new", "drop the Spec and result, return to READY"),
        "bench": (
            "bench smoke | bench quick [model,model] | bench full [model,model] | bench arc "
            "[model] | bench compare <id> <id> | bench resume <id> | bench list",
            "run the benchmark suite on the fake provider (smoke, free) or on profile models "
            "(paid, confirmed first)",
        ),
    }
)


# A verb whose effect depends on the state (Section 11): these lines win over
# ``_SYNTAX`` in their state, so ``help cancel`` names the work it would stop.
_STATE_SYNTAX: Mapping[State, Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        State.INFERRING: MappingProxyType({"cancel": ("cancel", "stop the inference")}),
        State.COMPILING: MappingProxyType({"cancel": ("cancel", "stop the compile")}),
        State.BENCHING: MappingProxyType(
            {"cancel": ("cancel", "stop the bench; rows so far are kept")}
        ),
    }
)


def help_for(verb: str, state: State | None = None) -> str:
    """One verb's syntax line and effect (as it applies in ``state``, when given);
    unknown verbs get a fixed line."""
    name = verb.lower()
    entry = _STATE_SYNTAX.get(state, {}).get(name) if state is not None else None
    entry = entry or _SYNTAX.get(name)
    if entry is None:
        return "no such command; type help\n"
    syntax, effect = entry
    line = f"  {syntax:<40} {effect}"
    if len(line) <= 100:
        return line + "\n"
    # A syntax too long for one line (bench): its alternatives packed into lines of at
    # most 100 columns, each continued with " |", then the effect indented below.
    lines: list[str] = []
    current = ""
    for part in syntax.split(" | "):
        candidate = f"{current} | {part}" if current else part
        if current and len(candidate) + 4 > 100:
            lines.append(f"  {current} |")
            current = part
        else:
            current = candidate
    lines.append(f"  {current}")
    lines.extend(
        textwrap.wrap(effect, width=100, initial_indent="    ", subsequent_indent="    ")
    )
    return "\n".join(lines) + "\n"


_READY_HELP = """Type what the agent should do (I will ask before spending), or:
  describe <text>        infer a Spec from text without asking
  spec <file|{json}>     load an explicit Spec        load <file>   same, for saved proposals
  list | info <name>     registry table / one entry   run <name|dir> {json}
  rollback <name> [rev]  recompile <name>             floors | floor <dataset> [kind]
  use model <id>         budget compile <usd> <s>     profile | spend | status | help [verb] | quit
  bench smoke            free fake-provider suite     bench quick|full|arc [models] | bench list
  roles                  role router: policy, variants and evidence (Stage 4 still licenses)
  rate good|bad          rate the last delivery in this session (content-free)
"""

_REVIEW_HELP = """Check the Spec; nothing is compiled until you type the token shown:
  show | json | diff     screen / exact JSON / changes  blockers | budget   what blocks approval
  set <path> <value>     edit one field (hash changes)  unset <field>       reset to default
  revise <text>          one Stage 0 call               floor               create the floor dataset
  save [file]            write the Spec JSON            approve [<hash8>]   token, then compile
  compile                expire Specs only              discard             return to READY
  list | info <name>     registry table / one entry     floors | run <name|dir> {json}
  forge on|off|status    opt-in Stage 3 Forge           forge budget|model|mode (help forge)
  forge run {json}       one-shot harness, expired      forge integrate <repo> <into>  package it
  route                  routing advice, never runs     reuse <name> <hash8> {json}  granted reuse
  rate good|bad          rate the last delivery (reuse, forge run or run)
  profile | spend | status | help [verb] | quit
"""

_FLOOR_HELP = """Build the floor dataset; every drafted case is SYNTHETIC until you review it:
  use <path>             read an operator-owned file    add {json}          one case you wrote
  paste                  JSON lines until '.'           draft [n]           model cases (default 20)
  review <id> ok|drop|expected {json}                   split               tuning / held-out ids
  show [<id>]            screen, or one case in full    accept | back       register / leave unsaved
  profile | spend | status | help [verb] | quit
"""

_RESULT_HELP = """The compile finished; licensed is recomputed from disk every time it is shown:
  report | spec | json   verification / Spec / JSON     install <name> [<hash8>]   by token
  run {json}             run this artifact from disk    run <name|dir> {json}      an installed one
  save [file]            write the Spec JSON            again               return to REVIEW
  new | discard          return to READY                list | info <name> | floors   registry
  export                 stage the installed package    rate good|bad       rate the last run
  profile | spend | status | help [verb] | quit
"""

_INFERRING_HELP = """Inferring; the reader stays active:
  cancel                 stop the inference             status | spend | help [verb] | quit
"""

_COMPILING_HELP = """Compiling; the reader stays active:
  cancel                 stop the compile               status | spend | help [verb] | quit
"""

_BENCHING_HELP = """Benchmarking; the reader stays active:
  cancel                 stop the bench; rows so far are kept
  status | spend | help [verb] | quit
"""

_SETUP_HELP = """Setup: answer the model> and confirm> prompts, or q to leave without writing:
  profile | spend | status | help [verb] | quit
"""

_EXIT_HELP = """Leaving:
  profile | spend | status | help [verb] | quit
"""

HELP: Mapping[State, str] = MappingProxyType(
    {
        State.BOOT: _SETUP_HELP,
        State.SETUP: _SETUP_HELP,
        State.READY: _READY_HELP,
        State.INFERRING: _INFERRING_HELP,
        State.REVIEW: _REVIEW_HELP,
        State.FLOOR: _FLOOR_HELP,
        State.COMPILING: _COMPILING_HELP,
        State.RESULT: _RESULT_HELP,
        State.BENCHING: _BENCHING_HELP,
        State.EXIT: _EXIT_HELP,
    }
)
