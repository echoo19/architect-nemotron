"""The bench runner: frozen suite cells through the real compiler, one row per cell.

For every (repeat, variant, task, model) cell the runner builds the frozen suite
Spec, lifts its per-run budget from the adapter's own quote, compiles it with
``Compiler.compile(spec, approved=True)`` (Stages 1, 2 and 4; Stage 3 off, so a
row measures presets, not rewrites) in a fresh short temporary workspace with
its own signing key and telemetry store, re-reads ``licensed`` from disk,
replays the tuning partition one case at a time for paired statistics and
optionally replays a disjoint delivery set, then appends one content-free
``BenchRow`` to ``rows.jsonl``. The ``forge`` and ``template`` variants (Phase 9)
are the one exception to "Stage 3 off": they hand ``CompileConfig.forge`` (enabled,
trigger ``always``) to the same compile, so Forge takes the Stage 3 slot; their
rows carry ``ForgeFields`` from the promoted artifact's content-free ``forge.json``
and a forge row where Forge did not run is marked ``forge_did_not_run``. On
``arc-preview`` Forge runs on the child game compiles inside ``public.manufacture``
(never on the parent planner compile); the cell passes the same plumbing (settings,
a fresh agent per child compile, the effective Pi config, the tally listener) and
reserves ``forge.max_usd`` per child session it can open. A plan
with no cell (``bench_no_cells``) and an arc run whose every cell is unavailable
(``bench_arc_unavailable``) are refused.

Authority: Stage 4 is the only licensing authority. The runner never
constructs, edits, copies or re-signs a ``TestReport``; the tuning replay reads
only ``FloorEvaluator.tuning`` and the delivery replay scores with the task's
closed checker; nothing is installed into any operator registry and the
workspace is deleted after every cell. The one install is ``arc-preview``'s:
``public.manufacture`` installs the harness it manufactured into a throwaway
registry inside the cell workspace (deleted with it) and plays it there; that
row's ``licensed`` is Stage 4's verdict as the manufacture summary reports it,
not a re-read from disk. While a cell runs, the process-wide
``ARCHITECT_SIGNING_KEY_FILE`` names the cell's throwaway key (a session pins
its own key for display first). Rows and progress carry counts, labels and
configuration hashes only.

Budgets are dollars and seconds. Every cell's reservation is computed before any
spend (``bound``); a cell above ``--max-usd`` is skipped with ``over_max_usd``
and a run whose remaining total exceeds it is refused (``bench_over_max_usd``).
A $0 model (a local Ollama model) reserves $0: its bound, ``reserved_usd`` and
the ``--max-usd`` check see only price-table money, and its meaningful spend is
wall time. Internally its Spec and compile budgets still carry a nominal $0.01
wallet per run so the runtime's dollar checks stay well-formed; that wallet is
never shown as money and never judged against ``--max-usd``.

A trip of the held-out guard (``HeldOutAccessError``) outside Stage 4 stops the
run with ``bench_held_out_access``; it is never recorded as an infrastructure
failure. Game licences are witnessed by the runtime at the moment it agrees to
play an artifact (``HarnessArtifact.licensed`` re-read from disk), so the runner
needs no access to the signing module.

Resume (Phase 11 Section 3): every run writes ``run.json`` (its configuration and
the suite, preset and profile fingerprints) next to ``rows.jsonl``. A run started
with ``resume=<bench_id>`` reuses that directory, refuses a changed configuration
or fingerprint with ``bench_resume_mismatch`` before any spend, skips every cell
that already has a row and runs the rest. A ``cancelled`` or ``host_memory_low``
row, or a last line cut short by a stopped process, is not done: it is dropped and
the cell runs again. Routed runs share one route log across cells and are refused
(``bench_resume_unsupported``). A record written before a configuration field
existed compares as if it had recorded that field's default.

Phase 14 (Sections 10 and 11): a public task (suite ``v2``) draws its floor and
replay cases from fixed, checked splits; ``--tasks``, ``--plain``, ``--reply-json``,
the split flags, ``--delivery all`` and ``--no-tuning-replay`` shape the cells, and
every refusal comes before any spend. The delivery replay classifies each run as
``ok``, ``invalid`` or ``over_budget`` and records its dollars, calls and seconds,
so rows carry per-run delivery metrics next to the signed report's held-out
counts. A row's arm settings are read back from the Spec the cell compiled. With
``--audit`` each cell runs inside the Forge audit context (Section 8), so every
compile it makes is audited, game and routed cells included; an audit directory
that cannot be written is ``audit_unwritable`` before any spend.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import json
import math
import os
import random
import shutil
import sys
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    ExitStack,
    contextmanager,
)
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Annotated, Any, Literal, Protocol, cast

from jsonschema import Draft202012Validator
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from architect.compiler import CompileConfig, Compiler, CompileResult
from architect.compiler.types import ForgeSettings, LibraryLookup, NullLibrary
from architect.floors import (
    EvaluationCase,
    FloorError,
    FloorEvaluator,
    HeldOutAccessError,
    HeldOutTasks,
)
from architect.floors.checkers import CHECKERS, Checker, FieldReport, field_report
from architect.floors.checkers import CheckerName as FloorCheckerName
from architect.games import public
from architect.games.execution import GAME_PRESETS, GameStrategy
from architect.games.factory import GameFactory, GameFactoryConfig
from architect.games.local import LocalGameProvider
from architect.games.types import GameLimits
from architect.library import HarnessLibrary
from architect.models import AdapterError, ModelAdapter
from architect.models.images import ImageInput
from architect.models.pricing import Prices, load_prices
from architect.presets.images import lift_images
from architect.profiles import (
    Profile,
    ProfileError,
    effective_pi_config,
    forge_settings_of,
    resolve_forge_agent,
)
from architect.registry import Registry, RegistryError
from architect.runtime import (
    BudgetExceeded,
    GameBackendProtocol,
    RunResult,
    Runtime,
    load_harness,
    numeric_params,
)
from architect.spec import (
    Budget,
    FloorDefinition,
    HarnessArtifact,
    Lifecycle,
    PresetName,
    Spec,
    TestReport,
)
from architect.telemetry import TelemetryStore, categorical

from . import public_tasks
from .oracle import ForgeScript, OracleState
from .providers import (
    CLOUD_PROVIDERS,
    CallMeter,
    PublicFramePlayer,
    adapter_price,
    build_factory,
    check_models,
    evidence_label,
    instrument,
    provider_for,
    resolve_models,
)
from .router_eval import OracleOption, oracle
from .rows import (
    ArcFields,
    BenchError,
    BenchRow,
    Code,
    DeliverySplit,
    Evidence,
    FloorSplit,
    ForgeFields,
    InvalidPolicy,
    ModelId,
    Probe,
    Provider,
    ReplyJson,
    RouteAction,
    RouteFields,
    RouteTier,
    Summary,
    TaskId,
    Tier,
    Variant,
    append_rows,
    architect_commit,
    encode_pass_bits,
    preset_fingerprint,
    prices_sha256,
    summarize,
    write_rows,
)
from .suite import (
    PROBES,
    SUITES,
    BenchTask,
    DatasetBundle,
    foreign_task,
    load_siblings,
    probe_spec,
    spec_setting,
    suite_digest,
    tiers_for,
)

__all__ = [
    "FORGE_VARIANTS",
    "NOISY_SMOKE_TASKS",
    "NOMINAL_RUN_USD",
    "BenchConfig",
    "BenchProgress",
    "BenchResult",
    "Bound",
    "CellBound",
    "audit_scope",
    "bound",
    "check_forge_agent",
    "check_max_usd",
    "forge_agent_model",
    "forge_settings",
    "free_memory_bytes",
    "load_resume",
    "nearest_rank",
    "open_audit",
    "plan",
    "process_metrics",
    "run_bench",
]

SIGNING_KEY_ENV = "ARCHITECT_SIGNING_KEY_FILE"
NOMINAL_RUN_USD = 0.01  # wallet for a $0 model's run; its real spend is wall time
SECONDS_PER_CALL = 120.0
SAFETY = 1.2
# The smoke tier's negative control (Section 13): sentiment and the clean vision floor.
NOISY_SMOKE_TASKS: frozenset[str] = frozenset({"sentiment-24", "exam-clean-40"})
_CODE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz_")
# Phase 9 arms: Forge takes the Stage 3 slot of the same compile (same spec and seed).
FORGE_VARIANTS: frozenset[str] = frozenset({"forge", "template"})
FORGE_DID_NOT_RUN = "forge_did_not_run"
# Section 14 host rules: a forge or template game cell of a run that can load a
# local model refuses to start while free RAM is under 6 GB (``host_memory_low``).
HOST_MEMORY_LOW = "host_memory_low"
HOST_MEMORY_FLOOR_BYTES = 6 * 1024**3
# Phase 10 Section 14: a routed row whose exact probe listed no candidate (or whose
# cell never reached a decision) is marked ``route_did_not_run``; the run fails.
ROUTE_DID_NOT_RUN = "route_did_not_run"
DEFAULT_ROUTE_CANDIDATES = 8  # ``RoutePolicy.max_candidates`` default
# Phase A arms of a routed cell, installed under ``<variant>-<task_id>``.
ROUTED_ARMS: tuple[Variant, ...] = ("default", "template", "forge")
# Phase 11 Section 3: the resume record, and the row failures that leave a cell undone.
RUN_FILE = "run.json"
RUN_FILE_VERSION = 1
NOT_DONE: frozenset[str] = frozenset({"cancelled", HOST_MEMORY_LOW})
# Phase 14 Section 10.5: the variants a plain arm may run, and the Forge audit module
# (Section 8), imported only by a run that asks for an audit.
PLAIN_VARIANTS: frozenset[str] = frozenset({"default", "noisy"})
FORGE_AUDIT_MODULE = "architect.compiler.forge_audit"
_REPLY_JSON: TypeAdapter[ReplyJson] = TypeAdapter(ReplyJson)
_INVALID_POLICY: TypeAdapter[InvalidPolicy] = TypeAdapter(InvalidPolicy)


class BenchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    suite: str = "v1"
    tier: Tier
    provider: Literal["fake", "profile"] = "profile"
    profile: Path | None = None
    models: tuple[str, ...] = ()
    presets: tuple[PresetName, ...] = ()
    variants: tuple[Variant, ...] = ("default",)
    route: dict[str, str] = Field(default_factory=dict)
    max_usd: Annotated[float, Field(gt=0)] | None = None
    seed: int = 7
    repeats: Annotated[int, Field(ge=1, le=100)] = 1
    size: Annotated[int, Field(ge=2, le=10_000)] | None = None
    max_candidates: Annotated[int, Field(gt=0, le=64)] = 2
    arc_games: Annotated[int, Field(ge=5, le=200)] = 5
    arc_actions: Annotated[int, Field(ge=1, le=10_000)] = 24
    arc_cache: Path | None = None
    # ``all`` replays a public task's whole delivery split (Phase 14 Section 10.5).
    delivery_n: Annotated[int, Field(ge=0, le=10_000)] | Literal["all"] = 0
    label: Annotated[str, Field(max_length=64, pattern=r"^[A-Za-z0-9._-]*$")] = ""
    out: Path = Path(".architect") / "bench"
    timeout_s: Annotated[float, Field(gt=0)] = 3600.0
    # The ``--forge-*`` overrides for forge/template cells. Only the fields explicitly
    # set here override the base: the profile's ``compile.forge`` on a profile run,
    # else ``ForgeSettings()``. ``forge_settings`` enables it per cell; default cells
    # ignore it.
    forge: ForgeSettings | None = None
    # ``--rounds`` (Phase 10 Section 14): the routed arm replays the suite this many
    # times, each round in a seed-fixed shuffled order, with one route log per bench.
    rounds: Annotated[int, Field(ge=1, le=100)] = 1
    # Phase 14 (Section 3.6 and 10.5); every default leaves a run as it was.
    tasks: tuple[TaskId, ...] = ()  # --tasks; empty keeps the whole tier
    tuning_replay: bool = True  # --no-tuning-replay sets False
    plain: bool = False  # --plain
    reply_json: ReplyJson | None = None  # --reply-json; None keeps the task's
    floor_split: FloorSplit = "evidence"  # public tasks only
    delivery_split: DeliverySplit = "delivery"  # public tasks only
    audit: Path | None = None  # --audit DIR (Section 8)

    @field_validator("variants")
    @classmethod
    def distinct_variants(cls, value: tuple[Variant, ...]) -> tuple[Variant, ...]:
        if not value:
            raise ValueError("at least one variant")
        return tuple(dict.fromkeys(value))


class CellBound(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    task_id: TaskId
    model: str
    variant: Variant
    repeat: int
    size: int
    per_call_usd: float
    reserved_usd: float
    calls: int
    code: Code | None = None


class Bound(BaseModel):
    """Per-cell reservations, their total and a call estimate; computed before spend."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    cells: tuple[CellBound, ...]
    total_usd: float
    calls: int
    models: tuple[str, ...]


class BenchProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    done: int
    total: int
    task_id: TaskId
    model: str
    stage: int | None
    calls: int
    cost_usd: float
    elapsed_s: float


class BenchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    bench_id: str
    rows: tuple[BenchRow, ...]
    summary: Summary
    bound_usd: float
    cancelled: bool
    skipped: tuple[tuple[TaskId, str, str], ...]
    rows_path: Path


@dataclass(frozen=True)
class _Cell:
    repeat: int
    task: BenchTask
    model: str
    variant: Variant
    size: int
    round: int = 0


@dataclass
class _Sized:
    """A cell's lifted Spec, budgets and reservation, or the code that skips it.

    ``reserved_usd`` is price-table money only ($0 for a $0 model): it is what the
    bound, the row and ``--max-usd`` see. ``run_cost_usd`` is the per-run wallet
    the Spec carries, lifted to a nominal amount for a $0 model; never shown.
    """

    cell: _Cell
    spec: Spec | None
    per_call_usd: float
    reserved_usd: float
    calls: int
    run_cost_usd: float
    compile_budget: Budget | None
    code: str | None
    # Routed cells (Phase 10): the Phase A arms and the foreign probe's own sizing.
    arms: tuple[_Sized, ...] = ()
    foreign: _Sized | None = None


def _task_table(suite: str) -> dict[str, BenchTask]:
    tasks = SUITES.get(suite)
    if tasks is None:
        raise BenchError("bench_suite_unknown")
    return {task.id: task for task in tasks}


def _cell_size(config: BenchConfig, task: BenchTask) -> int:
    if task.id == "arc-preview":
        return config.arc_games
    if task.kind == "game":
        return task.sizes.get(config.tier, task.default_size())
    if task.is_public:
        # A public task's size is its floor split's (``--size`` is refused in ``plan``).
        return len(public_tasks.load_public(task.id).splits[config.floor_split])
    return config.size if config.size is not None else task.sizes[config.tier]


def _check_public(config: BenchConfig, selected: Sequence[BenchTask]) -> None:
    """The split, size and delivery refusals of Section 10.5, before any spend.

    Nothing is checked when no task was selected (``bench_no_cells`` follows). The
    data of every selected public task is loaded and checked here
    (``bench_dataset_missing``, ``bench_dataset_mismatch``).
    """
    if not selected:
        return
    public = [task for task in selected if task.is_public]
    if not public:
        if config.floor_split != "evidence" or config.delivery_split != "delivery":
            raise BenchError("bench_split_unsupported")
        if config.delivery_n == "all":
            raise BenchError("bench_delivery_all_unsupported")
        return
    if config.size is not None:
        raise BenchError("bench_size_unsupported")
    if config.delivery_n == "all" and len(public) != len(selected):
        raise BenchError("bench_delivery_all_unsupported")  # a generated task has no split
    for task in public:
        data = public_tasks.load_public(task.id)
        public_tasks.delivery_cases(data, config.delivery_split, config.delivery_n)


def plan(config: BenchConfig, models: Sequence[str]) -> list[_Cell]:
    """Cells in (repeat, variant, task, model) order; noisy is fake-only, never on games.

    ``--tasks`` narrows the tier (an id outside the suite is ``bench_task_unknown``).
    A plain arm runs the default and noisy variants only (``bench_plain_variant``)
    and only on tasks a Spec can pin to one plain configuration, as noisy skips games.
    """
    tasks = _task_table(config.suite)
    tiers = tiers_for(config.suite)
    if config.tier not in tiers:
        raise BenchError("bench_tier_unknown")
    if not models:
        raise BenchError("bench_no_models")
    if "noisy" in config.variants and config.provider != "fake":
        raise BenchError("bench_variant_unknown")
    if config.plain and any(variant not in PLAIN_VARIANTS for variant in config.variants):
        raise BenchError("bench_plain_variant")
    if any(task_id not in tasks for task_id in config.tasks):
        raise BenchError("bench_task_unknown")
    selected = [
        tasks[task_id]
        for task_id in tiers[config.tier]
        if (not config.presets or tasks[task_id].preset in config.presets)
        and (not config.tasks or task_id in config.tasks)
    ]
    _check_public(config, selected)
    cells: list[_Cell] = []
    for repeat in range(config.repeats):
        for variant in config.variants:
            if variant == "routed":
                cells.extend(_routed_cells(config, selected, models, repeat))
                continue
            for task in selected:
                if not task.supports(variant):
                    continue
                if config.plain and not task.supports_plain():
                    continue
                if variant == "noisy" and (
                    task.kind == "game"
                    or (config.tier == "smoke" and task.id not in NOISY_SMOKE_TASKS)
                ):
                    continue
                for model in models:
                    cells.append(_Cell(repeat, task, model, variant, _cell_size(config, task)))
    if not cells:
        # The tier, --presets and --variant filters left nothing to measure: refuse
        # up front rather than write an empty run that looks like a green one.
        raise BenchError("bench_no_cells")
    return cells


def _routed_cells(
    config: BenchConfig, selected: Sequence[BenchTask], models: Sequence[str], repeat: int
) -> list[_Cell]:
    """The routed arm's cells: ``rounds`` replays of the suite (Section 14).

    One round keeps suite order. With ``--rounds R > 1`` every round is shuffled
    by ``random.Random(f"{seed}:{repeat}:{round}")``, so the order is fixed by the
    seed and two runs replay the same sequence.
    """
    base = [
        (task, model) for task in selected if task.supports("routed") for model in models
    ]
    cells: list[_Cell] = []
    for number in range(config.rounds):
        order = list(base)
        if config.rounds > 1:
            random.Random(f"{config.seed}:{repeat}:{number}").shuffle(order)
        for task, model in order:
            cells.append(
                _Cell(repeat, task, model, "routed", _cell_size(config, task), round=number)
            )
    return cells


def forge_settings(
    config: BenchConfig, variant: Variant, profile: Profile | None = None
) -> ForgeSettings | None:
    """The Forge settings of one cell: None for every non-Forge variant.

    The base is the profile's ``compile.forge`` on a profile run (Section 14 Stage B:
    the profile sets the agent, caps and limits), else ``ForgeSettings()``; the
    ``--forge-*`` flags (the fields set in ``config.forge``) override it. A forge or
    template cell forges on every compile (``trigger="always"``), so a passing
    Stage 2 can never make Forge silently not run, in the variant's mode.
    """
    if variant not in FORGE_VARIANTS:
        return None
    base = (
        forge_settings_of(profile.compile)
        if config.provider == "profile" and profile is not None
        else ForgeSettings()
    )
    flags = config.forge
    overrides = flags.model_dump(include=set(flags.model_fields_set)) if flags else {}
    return ForgeSettings.model_validate(
        {
            **base.model_dump(),
            **overrides,
            "enabled": True,
            "trigger": "always",
            "mode": "agent" if variant == "forge" else "template",
        }
    )


def forge_agent_model(
    config: BenchConfig, profile: Profile | None, settings: ForgeSettings, cell_model: str
) -> str:
    """The Forge agent's model key (``ForgeFields.agent_model``), resolved as in 12.1.

    ``agent_model`` when set; else, on a profile run, the profile's ``forge_agent``;
    else the cell's play model.
    """
    if settings.agent_model is not None:
        return settings.agent_model
    if config.provider == "profile" and profile is not None and profile.forge_agent is not None:
        return profile.forge_agent.model
    return cell_model


_MODEL_KEY: TypeAdapter[str] = TypeAdapter(ModelId)


def free_memory_bytes() -> int | None:
    """Free (available) physical RAM of this host in bytes; None when it cannot say.

    Windows asks ``GlobalMemoryStatusEx`` (``ullAvailPhys``); Linux reads
    ``MemAvailable`` from ``/proc/meminfo``; other POSIX hosts fall back to
    ``sysconf`` available pages. Stdlib only; nothing is loaded or spent.
    """
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class _MemoryStatus(ctypes.Structure):
            _fields_ = [  # noqa: RUF012 - the ctypes layout of MEMORYSTATUSEX
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(_MemoryStatus)
        try:
            ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        except (AttributeError, OSError):
            return None
        return int(status.ullAvailPhys) if ok else None
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        return int(parts[1]) * 1024
    except (OSError, UnicodeDecodeError):
        pass
    try:
        pages = os.sysconf("SC_AVPHYS_PAGES")
        size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return None
    return pages * size if pages >= 0 and size > 0 else None


def _host_memory_low(config: BenchConfig, cell: _Cell) -> bool:
    """Section 14's refusal, checked as each forge or template game cell starts.

    Only a run that can load a local model is checked: the fake provider loads
    none, so the $0 CI arms (Stage A) never depend on the runner's free RAM. A
    host that cannot report its free memory is not refused.
    """
    if config.provider == "fake" or cell.task.kind != "game":
        return False
    if cell.variant not in FORGE_VARIANTS:
        return False
    free = free_memory_bytes()
    return free is not None and free < HOST_MEMORY_FLOOR_BYTES


def check_forge_agent(
    config: BenchConfig,
    profile: Profile | None,
    *,
    factory: Callable[[str], ModelAdapter] | None = None,
) -> None:
    """Refuse, before any spend, a Forge agent a run could not build or record.

    Only a run with a forge or template variant is checked. On a profile run an
    ``agent_model`` that is neither a ``models`` key nor the ``forge_agent`` model
    raises ``ProfileError("forge_model_unknown")``, exactly as ``build_services``
    does (Section 12.1). On every run a name ``ForgeFields.agent_model`` cannot
    hold is ``bench_model_unconfigured`` (as ``check_models`` does for play
    models): its cells would compile and spend, then fail to write their rows. On
    the fake provider (no injected ``factory``) the known names are the price
    table's, the same rule ``resolve_models`` applies to ``--models``.
    """
    if not any(item in FORGE_VARIANTS for item in config.variants):
        return
    if config.provider != "profile" or profile is None:
        base = forge_settings(config, "forge", None)
        assert base is not None  # "forge" is a Forge variant
        if base.agent_model is None:
            return  # the cell's play model, already checked
        try:
            _MODEL_KEY.validate_python(base.agent_model)
        except ValidationError:
            raise BenchError("bench_model_unconfigured") from None
        if (
            config.provider == "fake"
            and factory is None
            and base.agent_model not in load_prices().models
        ):
            raise ProfileError("forge_model_unknown")
        return
    settings = forge_settings(config, "forge", profile)
    assert settings is not None  # "forge" is a Forge variant
    named = profile.forge_agent
    wanted = settings.agent_model
    if (
        wanted is not None
        and wanted not in profile.models
        and (named is None or named.model != wanted)
    ):
        raise ProfileError("forge_model_unknown")
    agent = wanted if wanted is not None else (named.model if named is not None else None)
    if agent is None:
        return  # the cell's play model, already checked
    try:
        _MODEL_KEY.validate_python(agent)
    except ValidationError:
        raise BenchError("bench_model_unconfigured") from None


def _compile_config(forge: ForgeSettings | None, **values: object) -> CompileConfig:
    """``CompileConfig`` with ``forge`` set only for Forge cells.

    Default cells build exactly the pre-Phase-9 config. ``CompileConfig.forge`` is
    WP4's field; until it exists a Forge cell compiles without it and its row is
    marked ``forge_did_not_run`` (never reported as a Forge result).
    """
    if forge is not None and "forge" in CompileConfig.model_fields:
        values["forge"] = forge
    return CompileConfig.model_validate(values)


def _forge_compiler(
    compiler_args: tuple[object, ...],
    *,
    tally: _ForgeTally | None,
    agent: ModelAdapter | None,
    pi_config: object,
    **kwargs: object,
) -> Compiler:
    """A ``Compiler``; Forge cells also get ``forge_adapter`` and ``pi_config`` (8.1).

    For a Forge cell the compiler's ``forge_listener`` hands the content-free
    ``ForgeResult`` of each Stage 3 to the cell's tally, so a session that ended
    without an accepted submission (no ``forge.json``) still reports its counts.
    A game factory copies the listener, the agent and the Pi config onto its inner
    compiler, so game compiles are tallied the same way. Forge itself runs only
    through ``Compiler``.
    """
    if tally is None:
        return Compiler(*compiler_args, **kwargs)  # type: ignore[arg-type]
    kwargs["forge_adapter"] = agent
    kwargs["pi_config"] = pi_config
    compiler = Compiler(*compiler_args, **kwargs)  # type: ignore[arg-type]
    compiler.forge_listener = tally.listen
    return compiler


def process_metrics(counters: Sequence[object]) -> dict[str, float | int | None]:
    """ARC process metrics over per-game ``PlayCounters`` (Section 13); numbers only.

    ``distinct_states_mean`` and ``revisit_rate_mean`` average the games;
    ``levels_completed_total`` sums them; ``calls_per_action`` is total model calls
    over total actions (None when no action was taken). No counters: all None.
    """
    rows: list[tuple[int, int, int, int, int]] = []
    for item in counters:
        values = tuple(getattr(item, name, None) for name in _COUNTER_NAMES)
        if all(type(value) is int and value >= 0 for value in values):
            rows.append(cast(tuple[int, int, int, int, int], values))
    if not rows:
        return {
            "distinct_states_mean": None,
            "revisit_rate_mean": None,
            "levels_completed_total": None,
            "calls_per_action": None,
        }
    actions = sum(row[0] for row in rows)
    return {
        "distinct_states_mean": sum(row[1] for row in rows) / len(rows),
        "revisit_rate_mean": sum(
            min(1.0, row[2] / row[0]) if row[0] else 0.0 for row in rows
        )
        / len(rows),
        "levels_completed_total": sum(row[4] for row in rows),
        "calls_per_action": (sum(row[3] for row in rows) / actions) if actions else None,
    }


_COUNTER_NAMES = ("actions", "distinct_states", "revisits", "model_calls", "levels_completed")
_FORGE_COUNTS = ("checks", "dryruns", "measures", "submissions", "accepted", "spawns")


def _forge_record(artifact: HarnessArtifact | None) -> dict[str, object] | None:
    """The promoted artifact's content-free ``forge.json`` (Section 8.9), if any."""
    if artifact is None:
        return None
    try:
        payload = json.loads((artifact.directory / "forge.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _count(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


def _amount(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number) and number >= 0:
            return number
    return 0.0


@dataclass
class _ForgeTally:
    """Forge counts of one cell (summed over a game cell's compiles); numbers only."""

    settings: ForgeSettings
    agent_model: str | None
    agent: CallMeter = field(default_factory=CallMeter)
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_FORGE_COUNTS, 0))
    agent_cost_usd: float | None = None
    agent_tokens_in: int | None = None
    agent_tokens_out: int | None = None
    agent_wall_s: float | None = None
    harness_nodes: int = 0
    harness_bytes: int = 0
    subharnesses: int = 0
    snapshot: object | None = None

    def listen(self, progress: object) -> None:
        """The latest ``ForgeResult``-shaped counters of this cell's current compile."""
        self.snapshot = progress

    def absorb(self, artifact: HarnessArtifact | None) -> None:
        """Add one compile: its ``forge.json`` if promoted, else the last snapshot."""
        record = _forge_record(artifact)
        source: object = record if record is not None else self.snapshot
        self.snapshot = None
        if source is None:
            return

        def read(name: str) -> object:
            if isinstance(source, dict):
                return source.get(name)
            return getattr(source, name, None)

        for name in _FORGE_COUNTS:
            self.counts[name] += _count(read(name))
        for name in ("agent_cost_usd", "agent_wall_s"):
            key = "wall_s" if name == "agent_wall_s" else name
            value = _amount(read(key))
            setattr(self, name, (getattr(self, name) or 0.0) + value)
        for name in ("agent_tokens_in", "agent_tokens_out"):
            setattr(self, name, (getattr(self, name) or 0) + _count(read(name)))
        if record is not None:
            self.harness_nodes += _count(record.get("harness_nodes"))
            self.harness_bytes += _count(record.get("harness_bytes"))
            children = record.get("children")
            self.subharnesses += len(children) if isinstance(children, list) else 0

    def session(self, result: object) -> None:
        """Add one whole Forge session as it ends (arc-preview's child compiles, which
        the cell never sees): the promoted ``forge.json`` of ``result.best`` when the
        session promoted one, else the session's own ``ForgeResult`` counters."""
        self.snapshot = result
        artifact = getattr(getattr(result, "best", None), "artifact", None)
        self.absorb(artifact if isinstance(artifact, HarnessArtifact) else None)

    def fields(self) -> ForgeFields:
        agent = self.agent
        return ForgeFields(
            mode=self.settings.mode,
            agent_model=self.agent_model if self.settings.mode == "agent" else None,
            checks=self.counts["checks"],
            dryruns=self.counts["dryruns"],
            measures=self.counts["measures"],
            submissions=self.counts["submissions"],
            accepted=self.counts["accepted"],
            spawns=self.counts["spawns"],
            agent_cost_usd=(
                self.agent_cost_usd if self.agent_cost_usd is not None else agent.cost_usd
            ),
            agent_tokens_in=(
                self.agent_tokens_in if self.agent_tokens_in is not None else agent.tokens_in
            ),
            agent_tokens_out=(
                self.agent_tokens_out if self.agent_tokens_out is not None else agent.tokens_out
            ),
            agent_wall_s=self.agent_wall_s if self.agent_wall_s is not None else agent.wall_s,
            harness_nodes=self.harness_nodes,
            harness_bytes=self.harness_bytes,
            subharnesses=self.subharnesses,
            language=2,
        )


def _route_ran(variant: Variant, route: RouteFields | None) -> bool:
    """The routed guard (Section 14): every routed row carries a decision, and the
    exact probe listed at least one candidate (Phase A's own entries)."""
    if variant != "routed":
        return True
    if route is None:
        return False
    return route.probe != "exact" or route.candidates > 0


def _forge_ran(variant: Variant, forge: ForgeFields | None) -> bool:
    """The guard (Section 13): forge needs a measure, template needs a submission."""
    if variant not in FORGE_VARIANTS:
        return True
    if forge is None:
        return False
    return forge.measures > 0 if variant == "forge" else forge.submissions > 0


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _first_images(bundle: DatasetBundle) -> list[ImageInput]:
    if not bundle.cases:
        return []
    _, refs = lift_images(deepcopy(bundle.cases[0].task))
    return [ref for ref in refs if isinstance(ref, ImageInput)]


def _per_call_seconds(adapter: ModelAdapter) -> float:
    timeout = getattr(adapter, "_timeout", None)
    if isinstance(timeout, (int, float)) and math.isfinite(timeout) and timeout > 0:
        return max(SECONDS_PER_CALL, float(timeout))
    return SECONDS_PER_CALL


def _lift(value: float) -> float:
    """The wallet a Spec carries: a $0 amount becomes the nominal run wallet."""
    return value if value > 0 else NOMINAL_RUN_USD


class _Context:
    """Shared, content-free run state: identity columns, caches and progress totals."""

    def __init__(
        self,
        config: BenchConfig,
        *,
        profile: Profile | None,
        factory: Callable[[str], ModelAdapter] | None,
        prices: Prices,
        clock: Callable[[], float],
    ) -> None:
        self.config = config
        self.profile = profile
        self.factory = factory
        self.prices = prices
        self.clock = clock
        # Routed cells (Phase 10): the routing seam and the one route log of the bench.
        self.routing: RouteOpener | None = None
        self.route_log: Path | None = None
        self._bundles: dict[tuple[str, int, int], DatasetBundle] = {}
        self._delivery: dict[tuple[str, int, int], tuple[list[EvaluationCase], OracleState]] = {}
        # Public tasks (Phase 14): one floor split and one replay split per run.
        self._public: dict[str, DatasetBundle] = {}
        self._public_delivery: dict[str, tuple[list[EvaluationCase], OracleState]] = {}

    def bundle(self, task: BenchTask, size: int) -> DatasetBundle:
        if task.is_public:
            if task.id not in self._public:
                self._public[task.id] = task.public_bundle(self.config.floor_split)
            return self._public[task.id]
        key = (task.id, self.config.seed, size)
        if key not in self._bundles:
            self._bundles[key] = task.bundle(self.config.seed, size)
        return self._bundles[key]

    def delivery(self, task: BenchTask) -> tuple[list[EvaluationCase], OracleState]:
        n = self.config.delivery_n
        if task.is_public:
            if task.id not in self._public_delivery:
                self._public_delivery[task.id] = (
                    task.public_delivery(self.config.delivery_split, n)
                    if n
                    else ([], OracleState())
                )
            return self._public_delivery[task.id]
        count = n if isinstance(n, int) else 0  # ``all`` never reaches a generated task
        key = (task.id, self.config.seed, count)
        if key not in self._delivery:
            self._delivery[key] = (
                task.delivery_dataset(self.config.seed, count) if count else ([], OracleState())
            )
        return self._delivery[key]

    def delivery_runs(self, task: BenchTask) -> int:
        """Replay runs a cell reserves: the public replay set's size, else ``--delivery``."""
        if task.is_public:
            return len(self.delivery(task)[0])
        n = self.config.delivery_n
        return n if isinstance(n, int) else 0

    def factory_for(
        self, variant: Variant, state: OracleState | None
    ) -> Callable[[str], ModelAdapter]:
        if self.factory is not None:
            return self.factory
        return build_factory(self.config, self.profile, state, variant=variant)

    def provider(self, model: str) -> Provider:
        return provider_for(self.config, self.profile, model)

    def pi_config(self, root: Path) -> object:
        """The effective ``PiConfig`` for a Forge session of the cell rooted at ``root``.

        The one rule ``build_services`` uses (``profiles.effective_pi_config``): the
        profile's denied set on a profile run (its file, floor/smoke/adapter
        fixtures, calibrations, the workspace control plane, relative
        ``denied_paths`` resolved against the profile), plus this cell's own
        secrets: the whole cell root, its signing key, telemetry and artifacts, and
        the rows directory. Typed ``object`` because the bench imports no backend
        (tests/adversarial/test_phase8_bench.py).
        """
        secrets = (
            root,
            root / "signing.key",
            root / "telemetry",
            root / "artifacts",
            self.config.out,
        )
        if self.profile is not None:
            return effective_pi_config(self.profile, self.profile.workspace, extra=secrets)
        return effective_pi_config(None, root, extra=secrets)

    def forge_tally(self, cell: _Cell) -> _ForgeTally | None:
        settings = forge_settings(self.config, cell.variant, self.profile)
        if settings is None:
            return None
        model = forge_agent_model(self.config, self.profile, settings, cell.model)
        return _ForgeTally(settings, model if settings.mode == "agent" else None)

    def forge_agent(self, cell: _Cell, tally: _ForgeTally | None) -> ModelAdapter | None:
        """The Forge agent, metered apart: build-time cost never enters ``cost_usd``.

        The fake provider replays the committed ``ForgeScript`` for the task. A
        profile run resolves the agent as ``build_services`` does (Section 12.1):
        ``agent_model`` (the ``forge_agent`` model or a ``models`` key), else the
        profile's ``forge_agent``, else the cell's model. Template mode has no agent.
        """
        if tally is None or tally.settings.mode != "agent":
            return None
        model = tally.agent_model or cell.model
        agent: ModelAdapter | None = None
        if self.config.provider == "fake" and self.factory is None:
            try:
                agent = ForgeScript(cell.task.id, model=model)
            except AdapterError:
                return None  # no committed script: the row says forge_did_not_run
        else:
            factory = self.factory_for(cell.variant, None)
            if self.factory is None and self.profile is not None:
                try:
                    agent = resolve_forge_agent(self.profile, tally.settings, factory)
                except ProfileError:
                    # forge_model_unknown was refused up front (check_forge_agent);
                    # what is left is a missing key or an unavailable adapter.
                    raise BenchError("bench_provider_failed") from None
            if agent is None:
                agent = factory(model)
        return instrument(agent, tally.agent, clock=self.clock)

    def game_agent(self, cell: _Cell) -> Callable[[str], ModelAdapter]:
        if self.config.provider == "fake" and self.factory is None:
            players: dict[str, ModelAdapter] = {}

            def player(model: str) -> ModelAdapter:
                if model not in players:
                    if cell.task.id == "arc-preview":
                        agent = public.FixtureAgent()
                        agent.model = model
                        players[model] = agent
                    else:
                        players[model] = PublicFramePlayer(model)
                return players[model]

            return player
        return self.factory_for(cell.variant, None)


# ----------------------------------------------------------------------- sizing


def _image_models(cell: _Cell, spec: Spec) -> set[str]:
    if cell.task.kind == "vision":
        return {spec.model_target}
    if cell.task.kind != "pipeline" or not spec.pipeline:
        return set()
    if cell.variant == "images-all-stages":
        return {step.model for step in spec.pipeline}
    return {spec.pipeline[0].model}


def _size_cell(ctx: _Context, cell: _Cell) -> _Sized:
    config = ctx.config
    task = cell.task

    def skipped(code: str, spec: Spec | None = None) -> _Sized:
        return _Sized(cell, spec, 0.0, 0.0, 0, 0.0, None, code)

    if "pillow" in task.requires and not _has_module("PIL"):
        return skipped("extra_missing")
    if task.kind == "game":
        return _size_game(ctx, cell)
    if cell.variant == "routed":
        return _size_routed(ctx, cell)
    spec = _cell_spec(ctx, cell)
    factory = ctx.factory_for(cell.variant, None)
    bundle = ctx.bundle(task, cell.size)
    images = _first_images(bundle)
    # The bound counts the configurations the compile will really measure: one for a
    # Spec that pins the plain arm (Section 10.5).
    plain = spec_setting(spec, "plain") is True
    try:
        for model in _image_models(cell, spec):
            price = adapter_price(factory(model), ctx.prices)
            if price is None or price.image is None:
                return skipped("image_not_configured", spec)
        if task.is_public:
            # Sized from this cell's own floor and replay cases (Section 10.4).
            shown = [*bundle.cases, *ctx.delivery(task)[0]]
            sizing = text_sizing = task.sizing_request((), spec=spec, cases=shown)
        else:
            sizing = task.sizing_request(images)
            text_sizing = task.sizing_request(())
        adapter = factory(cell.model)
        per_call = adapter.quote(sizing if images else text_sizing).cost_usd
        seconds = _per_call_seconds(adapter)
        max_calls = task.max_calls_per_run(cell.variant, plain=plain)
        payload = spec.model_dump(mode="json")
        if spec.pipeline:
            attach = _image_models(cell, spec)
            steps: list[JsonValue] = []
            run_cost = 0.0
            run_price = 0.0
            run_seconds = 0.0
            for index, step in enumerate(spec.pipeline):
                calls = max_calls - (len(spec.pipeline) - 1) if index == 0 else 1
                step_adapter = factory(step.model)
                with_images = images and (
                    index == 0 or cell.variant == "images-all-stages"
                ) and step.model in attach
                quote = step_adapter.quote(sizing if with_images else text_sizing).cost_usd
                cost = _lift(quote * calls)
                step_seconds = _per_call_seconds(step_adapter) * calls
                run_price += quote * calls
                run_cost += cost
                run_seconds += step_seconds
                dumped = step.model_dump(mode="json")
                dumped["budget"] = {"cost_usd": cost, "compute_s": step_seconds}
                steps.append(dumped)
            payload["pipeline"] = steps
        else:
            run_price = per_call * max_calls
            run_cost = _lift(run_price)
            run_seconds = seconds * max_calls
            pinned = _run_budget(task)
            if pinned is not None:
                # The settings pin the per-run budget (Section 10.2); the bound below
                # stays the price-table projection of the cell's own calls. A pinned
                # budget below those calls' reservations would refuse runs before they
                # call anything and zero the arm, so the run is refused before any spend.
                if run_price > pinned.cost_usd:
                    raise BenchError("bench_run_budget_too_small")
                run_cost, run_seconds = pinned.cost_usd, pinned.compute_s
        payload["budget"] = {"cost_usd": run_cost, "compute_s": run_seconds}
        spec = Spec.model_validate(payload)
    except BenchError:
        raise
    except AdapterError as exc:
        if str(exc) == "Model is not configured for image input":
            return skipped("image_not_configured", spec)
        raise BenchError("bench_provider_failed") from None
    tuning = (cell.size + 1) // 2
    held_out = cell.size // 2
    candidates = 1 if plain else config.max_candidates
    runs = candidates * tuning + held_out + ctx.delivery_runs(task)
    replay = tuning if config.tuning_replay else 0
    compile_budget = Budget(cost_usd=run_cost * runs * SAFETY, compute_s=config.timeout_s)
    # Price-table money only, plus the tuning replay; $0 for a $0 model. A forge cell
    # adds its Forge cap up front; a template cell adds $0 beyond the normal compile.
    reserved = run_price * (runs + replay) * SAFETY + _forge_cap(ctx, cell)
    return _Sized(
        cell,
        spec,
        per_call,
        reserved,
        max_calls * (runs + replay),
        run_cost,
        compile_budget,
        None,
    )


def _cell_spec(ctx: _Context, cell: _Cell) -> Spec:
    """The suite Spec of one cell with the run's arm settings (before budget lifting)."""
    config = ctx.config
    return cell.task.spec(
        cell.model,
        cell.variant,
        route=config.route if cell.task.kind != "game" else {},
        suite=config.suite,
        seed=config.seed,
        size=cell.size,
        plain=config.plain,
        reply_json=config.reply_json,
        floor_split=config.floor_split,
    )


def _run_budget(task: BenchTask) -> Budget | None:
    """A public task's pinned per-run budget, or None (lifted from quotes)."""
    if not task.is_public:
        return None
    return public_tasks.load_public(task.id).settings.run_budget


def _routed_policy(ctx: _Context) -> object | None:
    """The profile's ``routing`` policy on a profile run (WP2's ``Profile.routing``),
    else None: the router's default ``RoutePolicy()``."""
    if ctx.config.provider != "profile" or ctx.profile is None:
        return None
    return getattr(ctx.profile, "routing", None)


def _route_candidates(ctx: _Context) -> int:
    """``policy.max_candidates`` (8 by default, Section 8)."""
    value = getattr(_routed_policy(ctx), "max_candidates", DEFAULT_ROUTE_CANDIDATES)
    return value if type(value) is int and value > 0 else DEFAULT_ROUTE_CANDIDATES


def _foreign_cell(ctx: _Context, cell: _Cell) -> _Cell:
    task = foreign_task(cell.task, ctx.config.suite)
    config = ctx.config
    size = config.size if config.size is not None else task.sizes.get(
        config.tier, task.default_size()
    )
    return _Cell(cell.repeat, task, cell.model, "default", size, round=cell.round)


def _size_routed(ctx: _Context, cell: _Cell) -> _Sized:
    """A routed cell's reservation, before any spend (Section 14 ``_size_cell``).

    Phase A: the default arm plus the ``forge`` and ``template`` arms the run
    selected. Phase B: per probe, one cold compile for each non-exact probe and one
    warm-started compile per template candidate, up to ``max_candidates`` (a cell
    can hold at most one template per Phase A arm and one per executed compile).
    The exact probe's reuse replay is covered by one more default reservation.
    """
    base = _size_cell(ctx, replace(cell, variant="default"))
    if base.code is not None or base.spec is None:
        return replace(base, cell=cell)
    arms = tuple(
        _size_cell(ctx, replace(cell, variant=variant))
        for variant in ("forge", "template")
        if variant in ctx.config.variants
    )
    foreign = _size_cell(ctx, _foreign_cell(ctx, cell))
    templates = min(_route_candidates(ctx), 2 + len(arms))
    reserved = base.reserved_usd + sum(arm.reserved_usd for arm in arms if arm.code is None)
    calls = base.calls + sum(arm.calls for arm in arms if arm.code is None)
    reserved += base.reserved_usd * (templates + 1) * 2  # exact and sibling probes
    calls += base.calls * (templates + 1) * 2
    if foreign.code is None:
        reserved += foreign.reserved_usd * (templates + 1)
        calls += foreign.calls * (templates + 1)
    return replace(base, cell=cell, reserved_usd=reserved, calls=calls, arms=arms, foreign=foreign)


def _size_game(ctx: _Context, cell: _Cell) -> _Sized:
    config = ctx.config
    task = cell.task
    spec = _cell_spec(ctx, cell)
    if task.id == "arc-preview":
        if not _has_module("arc_agi"):
            return _Sized(cell, spec, 0.0, 0.0, 0, 0.0, None, "arc_unavailable")
        if config.provider != "fake" and ctx.provider(cell.model) not in _PREVIEW_ADAPTERS:
            return _Sized(cell, spec, 0.0, 0.0, 0, 0.0, None, "arc_unavailable")
    try:
        agent = ctx.game_agent(cell)(cell.model)
        per_call = agent.quote(task.sizing_request(())).cost_usd
    except BenchError:
        raise
    except AdapterError:
        raise BenchError("bench_provider_failed") from None
    calls_per_game = task.max_calls_per_run(cell.variant)
    if task.id == "arc-preview":
        calls = (config.arc_actions + 8) * config.arc_games
        # ``wallet`` is the compute cap handed to ``public.manufacture``, which opens
        # COMPUTE_CAP_WALLETS wallets in turn (proposal compile, approved compile,
        # play), each allowed the full cap: the bound and --max-usd see all of them.
        # A forge cell adds its Forge cap per child compile the manufacture can open
        # (each child forges at most once; the parent never does), as other tiers do.
        wallet = _lift(per_call * calls)
        reserved = per_call * calls * public.COMPUTE_CAP_WALLETS + _forge_cap(
            ctx, cell
        ) * public.forge_sessions(config.arc_games)
    else:
        # One game factory wallet funds the whole compile of one game (Stage 0, Stage 2
        # plays on every support-tuning game per candidate, Stage 4 on every support
        # held-out game, then the delivery play), as a text cell's compile budget
        # covers ``runs``; a single-run wallet only worked while the player was $0.
        runs = _game_compile_runs(config) * SAFETY
        calls = math.ceil(calls_per_game * runs) * cell.size
        # Every game compiles once, so a forge cell may forge once per game.
        reserved = per_call * calls + _forge_cap(ctx, cell) * cell.size
        wallet = _lift(per_call * calls_per_game) * runs * cell.size
    return _Sized(cell, spec, per_call, reserved, calls, wallet, None, None)


def _game_compile_runs(config: BenchConfig) -> int:
    """Play runs one local game compile may need: Stage 0 (counted as one run), every
    support-tuning game per Stage 2 candidate, every support held-out game (Stage 4)
    and the delivery play."""
    manifest = LocalGameProvider().manifest
    tuning = len(manifest.refs("support_tuning"))
    held_out = len(manifest.refs("support_held_out"))
    return 1 + config.max_candidates * tuning + held_out + 1


def _forge_cap(ctx: _Context, cell: _Cell) -> float:
    """``forge.max_usd`` per forge compile; $0 for template (no agent) and default."""
    settings = forge_settings(ctx.config, cell.variant, ctx.profile)
    return settings.max_usd if settings is not None and settings.mode == "agent" else 0.0


_PREVIEW_ADAPTERS: dict[str, Literal["anthropic", "claude-code", "ollama"]] = {
    "anthropic": "anthropic",
    "claude-code": "claude-code",
    "ollama": "ollama",
}


def _size_all(ctx: _Context, cells: Sequence[_Cell]) -> list[_Sized]:
    sized = [_size_cell(ctx, cell) for cell in cells]
    if sized and all(item.code == "arc_unavailable" for item in sized):
        # Every cell would be skipped as unavailable (no arc extra, or no model the
        # preview can drive): the run would measure nothing, so it is refused.
        raise BenchError("bench_arc_unavailable")
    maximum = ctx.config.max_usd
    if maximum is not None:
        for item in sized:
            if item.code is None and item.reserved_usd > maximum:
                item.code = "over_max_usd"
    return sized


def _bound_of(sized: Sequence[_Sized], models: Sequence[str]) -> Bound:
    cells = tuple(
        CellBound(
            task_id=item.cell.task.id,
            model=item.cell.model,
            variant=item.cell.variant,
            repeat=item.cell.repeat,
            size=item.cell.size,
            per_call_usd=item.per_call_usd,
            reserved_usd=item.reserved_usd if item.code is None else 0.0,
            calls=item.calls if item.code is None else 0,
            code=item.code,
        )
        for item in sized
    )
    return Bound(
        cells=cells,
        total_usd=sum(cell.reserved_usd for cell in cells),
        calls=sum(cell.calls for cell in cells),
        models=tuple(models),
    )


def check_max_usd(config: BenchConfig, limits: Bound) -> None:
    """Refuse (``bench_over_max_usd``) before any spend when the cap cannot hold.

    The run is refused when the runnable total exceeds ``--max-usd``, or when the
    cap left no runnable cell at all (every cell it priced is ``over_max_usd``,
    whatever other codes skipped the rest).
    """
    maximum = config.max_usd
    if maximum is None:
        return
    runnable = any(cell.code is None for cell in limits.cells)
    capped = any(cell.code == "over_max_usd" for cell in limits.cells)
    if limits.total_usd > maximum or (capped and not runnable):
        raise BenchError("bench_over_max_usd")


def _models(
    config: BenchConfig, profile: Profile | None, factory: Callable[[str], ModelAdapter] | None
) -> tuple[str, ...]:
    """The run's models, every one checked before any spend (``check_models``).

    The Forge agent of a profile run is checked here too (``check_forge_agent``).
    """
    if factory is None:
        models = resolve_models(config, profile)  # checks them itself
    else:
        models = tuple(dict.fromkeys(config.models or ("fake",)))
        check_models(config, profile, models)
    check_forge_agent(config, profile, factory=factory)
    return models


def bound(
    config: BenchConfig,
    *,
    factory: Callable[[str], ModelAdapter] | None = None,
    prices: Prices | None = None,
    profile: Profile | None = None,
    resume: str | None = None,
) -> Bound:
    """Every cell's reservation and the total, from quotes only; nothing is spent.

    With ``resume`` the bound covers only the cells the resumed run still has to run.
    """
    models = _models(config, profile, factory)
    ctx = _Context(
        config,
        profile=profile,
        factory=factory,
        prices=prices or load_prices(),
        clock=time.monotonic,
    )
    cells = plan(config, models)
    if resume is not None:
        cells = _pending(cells, _resume_state(config, models, resume).done)
    return _bound_of(_size_all(ctx, cells), models)


# ------------------------------------------------------------------------ resuming


_CellKey = tuple[str, str, str, int]


@dataclass(frozen=True)
class _Resume:
    kept: tuple[BenchRow, ...]
    done: frozenset[_CellKey]


def _cell_key(cell: _Cell) -> _CellKey:
    return (cell.task.id, cell.model, cell.variant, cell.repeat)


def _pending(cells: Sequence[_Cell], done: frozenset[_CellKey]) -> list[_Cell]:
    return [cell for cell in cells if _cell_key(cell) not in done]


def _file_sha256(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _run_record(config: BenchConfig, models: Sequence[str]) -> dict[str, object]:
    """What a resumed run must match: the configuration and its fingerprints."""
    return {
        "version": RUN_FILE_VERSION,
        "config": config.model_dump(mode="json", exclude={"out"}),
        "models": list(models),
        "suite_digest": suite_digest(config.suite),
        "preset_fingerprint": preset_fingerprint(),
        "profile_sha256": _file_sha256(config.profile),
    }


def _canonical_id(bench_id: str) -> str:
    try:
        parsed = uuid.UUID(bench_id)
    except ValueError:
        raise BenchError("bench_rows_unreadable") from None
    if str(parsed) != bench_id.lower():
        raise BenchError("bench_rows_unreadable")
    return str(parsed)


def _stored_record(out: Path, bench_id: str) -> dict[str, object]:
    directory = out / _canonical_id(bench_id)
    if not (directory / "rows.jsonl").is_file():
        raise BenchError("bench_rows_unreadable")  # no such bench under ``out``
    try:
        record = json.loads((directory / RUN_FILE).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        # A run from before Phase 11 (no run.json) or a damaged record: not resumable.
        raise BenchError("bench_resume_mismatch") from None
    if not isinstance(record, dict) or record.get("version") != RUN_FILE_VERSION:
        raise BenchError("bench_resume_mismatch")
    return record


def load_resume(out: Path, bench_id: str) -> BenchConfig:
    """The configuration bench ``bench_id`` under ``out`` started with (``run.json``)."""
    config = _stored_record(out, bench_id).get("config")
    if not isinstance(config, dict):
        raise BenchError("bench_resume_mismatch")
    try:
        return BenchConfig.model_validate({**config, "out": out})
    except ValidationError:
        raise BenchError("bench_resume_mismatch") from None


def _read_kept(path: Path, bench_id: str) -> list[BenchRow]:
    """Rows of a stopped run; only a last line cut short by the stop may be unreadable."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise BenchError("bench_rows_unreadable") from None
    lines = [line for line in text.splitlines() if line.strip()]
    rows: list[BenchRow] = []
    for index, line in enumerate(lines):
        try:
            rows.append(BenchRow.model_validate_json(line))
        except ValidationError:
            if index == len(lines) - 1:
                break  # the process stopped mid-write: that cell is simply not done
            raise BenchError("bench_rows_unreadable") from None
    if any(row.bench_id != bench_id for row in rows):
        raise BenchError("bench_resume_mismatch")
    return rows


def _normalized(record: dict[str, object], out: Path) -> dict[str, object]:
    """``record`` with its configuration re-read through ``BenchConfig``, so a record
    written before a configuration field existed compares as if it held the default."""
    stored = record.get("config")
    if not isinstance(stored, dict):
        return record
    try:
        config = BenchConfig.model_validate({**stored, "out": out})
    except ValidationError:
        return record
    return {**record, "config": config.model_dump(mode="json", exclude={"out"})}


def _resume_state(config: BenchConfig, models: Sequence[str], bench_id: str) -> _Resume:
    """Checked before any spend: the same run, and which of its cells are done."""
    if "routed" in config.variants:
        raise BenchError("bench_resume_unsupported")  # one route log spans the whole run
    bench_id = _canonical_id(bench_id)
    stored = _normalized(_stored_record(config.out, bench_id), config.out)
    if stored != _run_record(config, models):
        raise BenchError("bench_resume_mismatch")
    rows = _read_kept(config.out / bench_id / "rows.jsonl", bench_id)
    kept = tuple(row for row in rows if not NOT_DONE.intersection(row.failures))
    done = frozenset((row.task_id, row.model, row.variant, row.repeat) for row in kept)
    return _Resume(kept=kept, done=done)


# ------------------------------------------------------------------------ running


@dataclass
class _Totals:
    calls: int = 0
    cost_usd: float = 0.0
    done: int = 0
    total: int = 0
    stage: int | None = None
    started: float = 0.0
    listener: Callable[[BenchProgress], None] | None = None
    cell: _Cell | None = None
    meter: CallMeter = field(default_factory=CallMeter)

    def emit(self, clock: Callable[[], float]) -> None:
        listener, cell = self.listener, self.cell
        if listener is None or cell is None:
            return
        try:
            listener(
                BenchProgress(
                    done=self.done,
                    total=self.total,
                    task_id=cell.task.id,
                    model=cell.model,
                    stage=self.stage,
                    calls=self.calls + self.meter.calls,
                    cost_usd=self.cost_usd + self.meter.cost_usd,
                    elapsed_s=max(0.0, clock() - self.started),
                )
            )
        except Exception:
            pass  # A progress observer never breaks the run.


@dataclass
class _Outcome:
    """What a cell measured, before it becomes a row."""

    licensed: bool = False
    held_out_score: float | None = None
    held_out_count: int = 0
    tuning_bits: list[bool] | None = None
    tuning_cases: int = 0
    field_accuracy: float | None = None
    delivery_score: float | None = None
    delivery_cases: int = 0
    stages: tuple[int, ...] = ()
    failures: list[str] = field(default_factory=list)
    params: dict[str, JsonValue] = field(default_factory=dict)
    components: tuple[str, ...] = ()
    arc: ArcFields | None = None
    forge: ForgeFields | None = None
    # Routed rows (Phase 10): the probe's route columns, the delivered or reused
    # artifact's digest and, for the foreign probe, that task's floor limit.
    route: RouteFields | None = None
    artifact_digest: str | None = None
    floor_limit: float | None = None
    # Phase 14 (Section 11): the signed report's counts, the compile's own dollars, the
    # floor's case hash and the delivery replay's per-run metrics.
    held_out_correct: int | None = None
    held_out_invalid: int | None = None
    held_out_over_budget: int | None = None
    held_out_mean_cost_usd: float | None = None
    compile_cost_usd: float | None = None
    floor_cases_sha256: str | None = None
    delivery: _Delivery | None = None


@dataclass(frozen=True)
class _Run:
    """One replayed case: its output (None unless schema-valid), class and usage."""

    output: JsonValue | None
    outcome: Literal["ok", "invalid", "over_budget"]
    cost_usd: float
    calls: int
    seconds: float


@dataclass(frozen=True)
class _Delivery:
    """Per-run delivery metrics over the replayed cases, in their order (Section 11)."""

    passed: tuple[bool, ...]
    invalid: int
    over_budget: int
    invalid_runs: tuple[bool, ...]  # which runs were invalid, in case order
    over_budget_runs: tuple[bool, ...]  # which runs went over budget
    cost_usd: float
    calls: int
    zero_call_runs: int
    p50_s: float
    p95_s: float
    run_costs_usd: tuple[float, ...]
    field_accuracy: float | None
    cases_sha256: str


def nearest_rank(values: Sequence[float], percent: int) -> float:
    """The nearest-rank percentile: the sorted value at index ``ceil(p * n) - 1``.

    ``percent`` is p in whole percent, so the rank is exact integer arithmetic.
    """
    if not values or not 0 < percent <= 100:
        raise ValueError("nearest_rank needs values and 0 < percent <= 100")
    ordered = sorted(values)
    rank = -(-percent * len(ordered) // 100)  # ceil(percent * n / 100)
    return ordered[rank - 1]


class _Workspace:
    """A short temporary root with its own signing key; removed on exit."""

    def __init__(self, root: Path | None) -> None:
        if root is not None:
            root.mkdir(parents=True, exist_ok=True)
        self._raw = tempfile.mkdtemp(prefix="arch", dir=root)
        self.path = Path(self._raw)
        self._previous: str | None = None

    def enter(self) -> Path:
        self._previous = os.environ.get(SIGNING_KEY_ENV)
        os.environ[SIGNING_KEY_ENV] = str(self.path / "signing.key")
        return self.path

    def restore(self) -> None:
        if self._previous is None:
            os.environ.pop(SIGNING_KEY_ENV, None)
        else:
            os.environ[SIGNING_KEY_ENV] = self._previous

    def remove(self) -> bool:
        for _ in range(5):
            try:
                shutil.rmtree(self.path)
                return True
            except FileNotFoundError:
                return True
            except OSError:
                time.sleep(0.2)
        return not self.path.exists()


def _licensed_on_disk(artifact: HarnessArtifact | None) -> bool:
    if artifact is None:
        return False
    try:
        return HarnessArtifact.load(artifact.directory).licensed
    except (OSError, ValueError, TypeError):
        return False


class _LicenceWitness(Runtime):
    """A Runtime that records, per artifact directory, whether the artifact it is
    about to play is licensed, re-read from disk at that moment.

    Delivery consumes a single-use game artifact (its harness file is removed), so
    the licence cannot be re-read afterwards; the runtime's own run is the one
    moment the full check (signature, digest, floors) can still be witnessed.
    Nothing here constructs, edits or signs a report.
    """

    def __init__(
        self, store: TelemetryStore, *, adapter_factory: Callable[[str], ModelAdapter]
    ) -> None:
        super().__init__(store, adapter_factory=adapter_factory)
        self.witnessed: dict[Path, bool] = {}
        self.game_backends: list[object] = []

    def use_game_backend(  # type: ignore[override]
        self, backend: GameBackendProtocol
    ) -> AbstractAsyncContextManager[None]:
        # Kept so the cell can read ``last_counters`` (host-side process counters).
        self.game_backends.append(backend)
        return super().use_game_backend(backend)

    async def run(
        self, artifact: HarnessArtifact, task: JsonValue, adapter: ModelAdapter
    ) -> RunResult:
        self.witnessed[artifact.directory] = _licensed_on_disk(artifact)
        return await super().run(artifact, task, adapter)


def _witnessed(runtime: _LicenceWitness, artifact: HarnessArtifact | None) -> bool:
    """True only when the runtime saw this artifact licensed on disk as it played it."""
    return artifact is not None and runtime.witnessed.get(artifact.directory) is True


def _codes(values: Sequence[object]) -> list[str]:
    """Only fixed lowercase codes reach a row; anything else is dropped."""
    out: list[str] = []
    for value in values:
        if (
            isinstance(value, str)
            and 0 < len(value) <= 64
            and set(value) <= _CODE_CHARS
            and value not in out
        ):
            out.append(value)
    return out


def _validator(schema: dict[str, JsonValue]) -> Draft202012Validator:
    return Draft202012Validator(schema)


async def _replay_runs(
    artifact: HarnessArtifact,
    runtime: Runtime,
    factory: Callable[[str], ModelAdapter],
    cases: Sequence[EvaluationCase],
    *,
    meter: CallMeter | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> list[_Run]:
    """Run each case through ``Runtime.evaluate`` and classify the run (Section 11).

    ``ok`` is an output that passed the output schema; ``over_budget`` a run stopped
    by ``BudgetExceeded``; anything else is ``invalid``, an input-schema failure
    included. Dollars and calls are ``meter``'s difference across the case (every
    call the cell's adapters made, reasoning tokens included); seconds are the
    ``clock`` difference around ``Runtime.evaluate``.
    """
    harness = load_harness(artifact.directory, artifact=artifact)
    inputs = _validator(artifact.spec.input_schema)
    outputs = _validator(artifact.spec.output_schema)
    runs: list[_Run] = []
    for case in cases:
        cost, calls = (meter.cost_usd, meter.calls) if meter is not None else (0.0, 0)
        output: JsonValue | None = None
        outcome: Literal["ok", "invalid", "over_budget"] = "invalid"
        seconds = 0.0
        try:
            task = deepcopy(case.task)
            if inputs.is_valid(task):
                started = clock()
                try:
                    run = await runtime.evaluate(
                        artifact.spec,
                        harness,
                        task,
                        factory(artifact.model),
                        version=artifact.version,
                        preset=artifact.preset,
                        components=artifact.components,
                        params=artifact.params,
                    )
                finally:
                    seconds = max(0.0, clock() - started)
                candidate = run.output
                json.dumps(candidate, allow_nan=False)
                if outputs.is_valid(candidate):
                    output, outcome = candidate, "ok"
        except (asyncio.CancelledError, HeldOutAccessError):
            raise
        except BudgetExceeded:
            outcome = "over_budget"
        except Exception:
            pass  # invalid: no schema-valid output
        runs.append(
            _Run(
                output=output,
                outcome=outcome,
                cost_usd=max(0.0, meter.cost_usd - cost) if meter is not None else 0.0,
                calls=meter.calls - calls if meter is not None else 0,
                seconds=seconds,
            )
        )
    return runs


async def _replay(
    artifact: HarnessArtifact,
    runtime: Runtime,
    factory: Callable[[str], ModelAdapter],
    cases: Sequence[EvaluationCase],
) -> list[JsonValue | None]:
    """Run each case through ``Runtime.evaluate``; None for a failed or invalid output."""
    return [run.output for run in await _replay_runs(artifact, runtime, factory, cases)]


def _floor_checker(name: str) -> FloorCheckerName | None:
    """The checker table's own label equal to ``name`` (the bench mirrors the closed
    literal), or None when this build's table has no such checker."""
    return next((key for key in CHECKERS if key == name), None)


def _checker(task: BenchTask) -> Checker | None:
    """The floor checker of ``task``; None means exact equality."""
    if task.checker == "equality":
        return None
    key = _floor_checker(task.checker)
    if key is None:
        raise FloorError("Checker is not in this build's table")
    return CHECKERS[key]


def _field_report(
    task: BenchTask, expected: JsonValue, output: JsonValue | None
) -> FieldReport | None:
    """Per-field counts behind ``task``'s checker; None for equality tasks. A run
    without output matches none of the checker's fields."""
    if task.checker == "equality":
        return None
    key = _floor_checker(task.checker)
    if key is None:
        return None
    try:
        return field_report(expected, output, checker=key)
    except Exception:
        return None


def _check(task: BenchTask, expected: JsonValue, output: JsonValue | None) -> bool:
    if output is None:
        return False
    if task.checker == "equality":
        return output == expected
    try:
        checker = _checker(task)
        return checker is not None and checker(deepcopy(expected), deepcopy(output))
    except Exception:
        return False


def _delivery_metrics(
    task: BenchTask, cases: Sequence[EvaluationCase], runs: Sequence[_Run]
) -> _Delivery:
    """Section 11's metrics over ``runs``; an invalid or over-budget run never passes
    and never counts as a run settled with no model call."""
    passed = tuple(
        _check(task, case.expected, run.output) for case, run in zip(cases, runs, strict=True)
    )
    reports = [
        report
        for case, run in zip(cases, runs, strict=True)
        if (report := _field_report(task, case.expected, run.output)) is not None
    ]
    fields = sum(report.fields for report in reports)
    seconds = [run.seconds for run in runs]
    invalid = tuple(run.outcome == "invalid" for run in runs)
    over_budget = tuple(run.outcome == "over_budget" for run in runs)
    return _Delivery(
        passed=passed,
        invalid=sum(invalid),
        over_budget=sum(over_budget),
        invalid_runs=invalid,
        over_budget_runs=over_budget,
        cost_usd=sum(run.cost_usd for run in runs),
        calls=sum(run.calls for run in runs),
        # Receipts settled with no model call: an answer, not a run that raised or was
        # refused before its first call (those settled nothing).
        zero_call_runs=sum(1 for run in runs if run.outcome == "ok" and run.calls == 0),
        p50_s=nearest_rank(seconds, 50),
        p95_s=nearest_rank(seconds, 95),
        run_costs_usd=tuple(run.cost_usd for run in runs),
        field_accuracy=sum(report.matched for report in reports) / fields if fields else None,
        cases_sha256=public_tasks.cases_sha256(cases),
    )


def _held_out_counts(report: TestReport) -> tuple[int | None, int | None, int | None]:
    """``(correct, invalid, over_budget)`` of the signed report's ``held_out_counts``
    (Section 3.2); all None for a report without counts."""
    counts = getattr(report, "held_out_counts", None)
    if counts is None:
        return None, None, None

    def count(name: str) -> int | None:
        value = getattr(counts, name, None)
        return value if type(value) is int and value >= 0 else None

    return count("correct"), count("invalid"), count("over_budget")


async def _text_cell(
    ctx: _Context,
    sized: _Sized,
    root: Path,
    meter: CallMeter,
    totals: _Totals,
    outcome: _Outcome,
) -> None:
    """Fill ``outcome`` as the cell proceeds, so a later failure keeps Stage 4's result."""
    cell, spec = sized.cell, sized.spec
    assert spec is not None and sized.compile_budget is not None
    task, config = cell.task, ctx.config
    bundle = ctx.bundle(task, cell.size)
    delivery_cases, delivery_state = ctx.delivery(task)
    state = OracleState()
    state.merge(bundle.state)
    state.merge(delivery_state)
    base = ctx.factory_for(cell.variant, state)

    def metered(model: str) -> ModelAdapter:
        return instrument(base(model), meter, clock=ctx.clock)

    cases = bundle.cases
    checker = _checker(task)
    outcome.floor_cases_sha256 = public_tasks.cases_sha256(cases) if cases else None
    floors: list[FloorEvaluator] = []

    def floor_factory(definition: FloorDefinition) -> FloorEvaluator:
        if definition != spec.floor:
            raise FloorError("Floor is not this bench dataset")
        floor = HeldOutTasks(definition, deepcopy(cases), checker)
        floors.append(floor)
        return floor

    def smoke_factory(definition: FloorDefinition) -> FloorEvaluator:
        # Forge needs a smoke floor (8.2). A bench task has no smoke file, so a Forge
        # cell's smoke floor is built from the parent's tuning partition only: no
        # held-out case can reach it, and it adds no case the agent does not see.
        parent = floors[0] if floors else floor_factory(definition)
        return HeldOutTasks(definition, list(parent.tuning), checker)

    outcome.tuning_cases = (cell.size + 1) // 2
    tally = ctx.forge_tally(cell)
    forge_extra: dict[str, object] = {} if tally is None else {"smoke_factory": smoke_factory}
    store = TelemetryStore(root / "telemetry")
    try:
        runtime = Runtime(store, adapter_factory=metered)
        compiler = _forge_compiler(
            (root / "artifacts", runtime, metered, floor_factory),
            tally=tally,
            agent=ctx.forge_agent(cell, tally),
            pi_config=ctx.pi_config(root),
            **forge_extra,
            config=_compile_config(
                tally.settings if tally is not None else None,
                strategy="grid",
                model_ladder=(cell.model,),
                allowed_presets=(task.preset,),
                max_candidates=config.max_candidates,
                enable_rewriting=False,
                max_rewrites=0,
                compile_budget=sized.compile_budget,
            ),
            library=NullLibrary(),
        )

        def on_stage(stage: int) -> None:
            totals.stage = stage
            totals.emit(ctx.clock)

        compiler.stage_listener = on_stage
        try:
            result: CompileResult = await compiler.compile(spec, approved=True)
        finally:
            if tally is not None:
                outcome.forge = tally.fields()
        # Build cost before any replay; the Forge agent is metered apart (ForgeFields).
        outcome.compile_cost_usd = meter.cost_usd
        artifact = result.artifact
        if tally is not None:
            tally.absorb(artifact)
            outcome.forge = tally.fields()
        outcome.licensed = _licensed_on_disk(artifact)
        outcome.stages = result.stages
        outcome.failures = _codes(result.failures)
        report = result.report
        if report is not None:
            outcome.held_out_score = report.floor_score
            outcome.held_out_count = report.held_out_count
            outcome.held_out_mean_cost_usd = report.mean_cost_usd
            (
                outcome.held_out_correct,
                outcome.held_out_invalid,
                outcome.held_out_over_budget,
            ) = _held_out_counts(report)
        if artifact is not None:
            outcome.params = dict(artifact.params)
            outcome.components = artifact.components
            floor = floors[0] if floors else floor_factory(spec.floor)
            tuning = list(floor.tuning)
            outcome.tuning_cases = len(tuning)
            totals.stage = None
            if config.tuning_replay:
                outputs = await _replay(artifact, runtime, metered, tuning)
                bits: list[bool] = []
                reports: list[FieldReport] = []
                for case, output in zip(tuning, outputs, strict=True):
                    passed = False
                    if output is not None:
                        try:
                            passed = await floor.score([case], [output]) == 1.0
                        except (FloorError, ValueError):
                            passed = False
                    bits.append(passed)
                    report_fields = _field_report(task, case.expected, output)
                    if report_fields is not None:
                        reports.append(report_fields)
                outcome.tuning_bits = bits
                fields = sum(item.fields for item in reports)
                if fields:
                    outcome.field_accuracy = sum(item.matched for item in reports) / fields
            if delivery_cases:
                runs = await _replay_runs(
                    artifact, runtime, metered, delivery_cases, meter=meter, clock=ctx.clock
                )
                delivery = _delivery_metrics(task, delivery_cases, runs)
                outcome.delivery = delivery
                outcome.delivery_cases = len(delivery_cases)
                outcome.delivery_score = sum(delivery.passed) / len(delivery_cases)
    finally:
        store.close()


def _delivery_counters(backends: Sequence[object], run: object) -> object | None:
    """The delivery play's counters, or None when no delivery play happened.

    The factory's one backend also serves the Stage 2 tuning and Stage 4 verification
    plays, so its ``last_counters`` only describe delivery when they carry the delivery
    run's id; a refused, unlicensed or budget-stopped cell contributes no process metrics.
    """
    play = getattr(run, "play_result", None)
    run_id = getattr(play, "run_id", None)
    if run_id is None:
        return None
    for backend in backends[-1:]:
        if getattr(backend, "last_run_id", None) == run_id:
            last: object = getattr(backend, "last_counters", None)
            return last
    return None


async def _local_games_cell(
    ctx: _Context,
    sized: _Sized,
    root: Path,
    meter: CallMeter,
    totals: _Totals,
    outcome: _Outcome,
) -> None:
    cell, config = sized.cell, ctx.config
    agents = ctx.game_agent(cell)

    def metered(model: str) -> ModelAdapter:
        return instrument(agents(model), meter, clock=ctx.clock)

    provider_name = ctx.provider(cell.model)
    online = provider_name in CLOUD_PROVIDERS
    games = LocalGameProvider()
    targets = games.manifest.refs("delivery")[: cell.size]
    per_game = sized.run_cost_usd / max(1, len(targets))
    outcome.held_out_count = len(targets)
    scores: list[float] = []
    licensed = bool(targets)
    stages: list[int] = []
    failures: list[str] = []
    actions = 0
    counters: list[object] = []
    tally = ctx.forge_tally(cell)
    forge = tally.settings if tally is not None else None
    store = TelemetryStore(root / "telemetry")

    def unavailable(definition: FloorDefinition) -> FloorEvaluator:
        raise FloorError("Game floors are built by the game factory")

    try:
        runtime = _LicenceWitness(store, adapter_factory=metered)
        for index, target in enumerate(targets):
            compile_config = _compile_config(
                forge,
                model_ladder=(cell.model,),
                enable_rewriting=False,
                max_rewrites=0,
                max_candidates=config.max_candidates,
                compile_budget=Budget(cost_usd=per_game, compute_s=config.timeout_s),
            )
            # One Forge agent per game compile, as _text_cell has one per compile: a
            # replayed ForgeScript is spent by one Pi session, so a shared agent would
            # leave every later game unforged while the summed counts pass the guard.
            compiler = _forge_compiler(
                (root / f"g{index}", runtime, metered, unavailable),
                tally=tally,
                agent=ctx.forge_agent(cell, tally),
                pi_config=ctx.pi_config(root),
                config=compile_config,
                library=NullLibrary(),
                inference_adapter=metered(cell.model),
            )
            factory_values: dict[str, object] = {
                "budget": Budget(cost_usd=per_game, compute_s=config.timeout_s),
                "compile_config": compile_config,
                "small_model": cell.model,
                "limits": GameLimits(max_actions=config.arc_actions, max_turns=config.arc_actions),
                "strategy": cast(GameStrategy, GAME_PRESETS[index % len(GAME_PRESETS)]),
                "floor_limit": cell.task.floor_limit,
                "allow_online_model": online,
            }
            if forge is not None and "allow_forge" in GameFactoryConfig.model_fields:
                # Only forge and template cells may deliver a licensed Forge sandbox artifact.
                factory_values["allow_forge"] = True
            factory = GameFactory(
                compiler, games, GameFactoryConfig.model_validate(factory_values)
            )
            totals.stage = None
            totals.emit(ctx.clock)
            played = len(runtime.game_backends)
            run = await factory.run(target, support_dataset=f"bench-arc-local-{config.seed}")
            delivered_counters = _delivery_counters(runtime.game_backends[played:], run)
            if delivered_counters is not None:
                counters.append(delivered_counters)
            compiled = run.compile_result
            if tally is not None:
                tally.absorb(compiled.artifact if compiled is not None else None)
            if compiled is not None:
                stages.extend(compiled.stages)
                failures.extend(compiled.failures)
            # Delivery consumes the single-use artifact (its harness file is removed), so
            # the licence is what the runtime witnessed on disk when it agreed to play it.
            licensed = (
                licensed
                and run.accepted
                and _witnessed(runtime, compiled.artifact if compiled is not None else None)
            )
            failures.extend(run.failures)
            actions += run.actions_used
            score = run.score.normalized if run.score is not None else None
            scores.append(float(score) if score is not None else 0.0)
    finally:
        store.close()
        if tally is not None:
            outcome.forge = tally.fields()
    outcome.licensed = licensed
    outcome.stages = tuple(sorted(set(stages)))
    outcome.failures = _codes(failures)
    outcome.held_out_score = sum(scores) / len(scores) if scores else None
    outcome.arc = ArcFields(
        score=outcome.held_out_score,
        score_source="local",
        model_binding="online_preview" if online else "offline",
        games=len(targets),
        actions=actions,
        **process_metrics(counters),  # type: ignore[arg-type]
    )


# manufacture() failures that mean "no usable arc extra or public-game cache".
_ARC_UNAVAILABLE: frozenset[str] = frozenset(
    {
        "public_games_unavailable",
        "public_listing_invalid",
        "public_game_metadata_mismatch",
        "game_count",
    }
)


async def _preview_cell(
    ctx: _Context,
    sized: _Sized,
    root: Path,
    meter: CallMeter,
    totals: _Totals,
    outcome: _Outcome,
) -> None:
    cell, config = sized.cell, ctx.config
    fake = config.provider == "fake"
    agent = instrument(ctx.game_agent(cell)(cell.model), meter, clock=ctx.clock)
    if fake:
        agent.model = "fixture"
        label: Literal["fixture", "anthropic", "claude-code", "ollama"] = "fixture"
    else:
        label = _PREVIEW_ADAPTERS[ctx.provider(cell.model)]
    settings = ctx.profile.models.get(cell.model) if ctx.profile is not None else None
    prices = public.LOCAL_PRICES
    if ctx.profile is not None and settings is not None and settings.prices is not None:
        prices = ctx.profile.resolve(settings.prices)
    tally = ctx.forge_tally(cell)
    traces: list[public.PreviewTrace] = []
    host: dict[str, object] = {"observer": traces.append}
    if tally is not None:
        # The first agent is built up front, so a provider that cannot build it stops
        # the run (BenchError) before any spend; every later child compile gets a
        # fresh one, as each game compile of _local_games_cell does.
        first = [ctx.forge_agent(cell, tally)]

        def forge_agent() -> ModelAdapter | None:
            return first.pop() if first else ctx.forge_agent(cell, tally)

        host.update(
            forge=tally.settings,
            forge_agent=forge_agent,
            pi_config=ctx.pi_config(root),
            forge_listener=tally.session,
        )
    options = public.PreviewOptions.model_validate(
        {
            "games": config.arc_games,
            "seed": config.seed,
            "cache": config.arc_cache or public.DEFAULT_CACHE,
            "adapter": label,
            "prices": prices,
            "call_timeout_s": settings.timeout_s if settings and settings.timeout_s else 300.0,
            "num_ctx": settings.num_ctx if settings and settings.num_ctx else 65_536,
            "think": settings.think if settings else None,
            "model": "fixture" if fake else cell.model,
            "floor_limit": 0.0,
            "cost_usd": sized.run_cost_usd,
            "compute_s": config.timeout_s,
            "max_actions": config.arc_actions,
            "offline": True,
            **host,
        }
    )
    (root / "games").mkdir(exist_ok=True)
    try:
        summary = await public.manufacture(options, root, agent=agent)
    except HeldOutAccessError:
        raise  # an authority breach, never an unavailable extra
    except ImportError:
        outcome.failures = ["arc_unavailable"]
        return
    except (RuntimeError, ValueError) as exc:
        code = str(exc)
        if code in _ARC_UNAVAILABLE:
            outcome.failures = ["arc_unavailable"]
        else:
            # Authority refusals ("proposal_skipped_approval", "harness_reused") and
            # any other failure keep their own fixed code, or a generic one.
            outcome.failures = _codes([code]) or ["compilation_failed"]
        return
    except OSError:
        outcome.failures = ["arc_unavailable"]  # the cache could not be read or staged
        return
    finally:
        if tally is not None:
            outcome.forge = tally.fields()
        if traces:
            # The parent's stages and every delivered child's (Stage 3 when it forged).
            outcome.stages = traces[-1].stages
    score = summary.get("score")
    failures: list[object] = []
    for key in ("compile_failures", "run_failures"):
        value = summary.get(key)
        if isinstance(value, list):
            failures.extend(value)
    children = summary.get("child_failures")
    if isinstance(children, list):
        for child in children:
            if isinstance(child, list):
                failures.extend(child)
    if "error" in summary:
        failures.append(summary["error"])
    outcome.licensed = summary.get("licensed") is True
    outcome.failures = _codes(failures)
    outcome.held_out_score = float(score) if isinstance(score, (int, float)) else None
    outcome.held_out_count = config.arc_games
    source = summary.get("score_source")
    binding = summary.get("model_binding")
    # Host-side counters of the children's delivery plays, for every arm.
    counters = traces[-1].delivery_counters if traces else ()
    outcome.arc = ArcFields(
        score=outcome.held_out_score,
        score_source=source if isinstance(source, str) and _codes([source]) else "unavailable",
        model_binding=binding if isinstance(binding, str) and _codes([binding]) else "offline",
        games=config.arc_games,
        actions=config.arc_actions,
        **process_metrics(counters),  # type: ignore[arg-type]
    )


# ------------------------------------------------------------------ routed cells


@dataclass(frozen=True)
class RouteOption:
    """One candidate of a decision as the bench executes it; host-side only.

    ``name``/``revision`` locate an installed entry and ``key`` a library template;
    none of them ever reaches a row (rows carry ranks, tiers and counts).
    """

    origin: Literal["installed", "template"]
    tier: Literal["A", "B", "C"]
    eligible: bool
    model: str
    name: str | None = None
    revision: int | None = None
    key: str | None = None
    r: float | None = None
    ref: object = None  # the router's own ``InstalledRef`` / ``TemplateRef``


@dataclass(frozen=True)
class RouteView:
    """The parts of a ``RouteDecision`` a routed cell needs (Section 8.4)."""

    action: RouteAction
    tier: RouteTier
    options: tuple[RouteOption, ...]
    target: int | None
    chosen_rank: int | None
    explored: bool
    codes: tuple[str, ...]
    decision_ms: float
    decision: object = None


@dataclass(frozen=True)
class RouteContext:
    """What one routed cell hands the router: its own stores and the bench route log."""

    registry: Registry
    library: HarnessLibrary
    telemetry: TelemetryStore
    log_root: Path
    workspace: Path
    providers: Mapping[str, Provider]
    policy: object | None


RouteOutcome = Literal["licensed", "unlicensed", "delivered", "failed"]


class RouteSession(Protocol):
    """The routing seam of a routed cell; ``open_router`` is the real one.

    ``route`` decides with ``grant_reuse=True`` and origin ``bench``; ``reuse`` is
    ``load_reuse_target`` (fresh license, digest and slot re-check); ``warm`` is the
    warm-start library for one template option (None when it cannot be loaded);
    ``outcome`` appends the content-free outcome record. Nothing here licenses.
    """

    min_template_reliability: float

    def route(self, spec: Spec, *, ladder: tuple[str, ...], budget_cap: Budget) -> RouteView: ...

    def reuse(self, view: RouteView, spec: Spec) -> HarnessArtifact: ...

    def warm(self, view: RouteView, option: RouteOption) -> LibraryLookup | None: ...

    def outcome(
        self,
        view: RouteView,
        *,
        outcome: RouteOutcome,
        stage4_passed: bool | None,
        evidence: Evidence,
        compile_cost_usd: float,
    ) -> None: ...


RouteOpener = Callable[[RouteContext], RouteSession]
_FIT_TIERS: Mapping[str, RouteTier] = {
    "fit_slot": "A",
    "fit_shape": "B",
    "fit_family": "C",
    "fit_none": "none",
}


def route_target(view: RouteView) -> str | None:
    """The decision's target as one content-free ``h_`` category (``RouteFields.target``).

    It hashes the option's full reference: origin, registry name and revision (or the
    library key) and the artifact digest, so two runs that pick different entries at
    the same rank differ here. None when the decision has no target.
    """
    if view.target is None or not 0 <= view.target < len(view.options):
        return None
    option = view.options[view.target]
    digest = getattr(option.ref, "artifact_digest", None)
    where = f"{option.name}@{option.revision}" if option.origin == "installed" else option.key
    identity = f"route-target:{option.origin}:{where}:{digest or '-'}"
    return categorical(identity)


def _best_tier(codes: Sequence[str], options: Sequence[RouteOption]) -> RouteTier:
    for code, tier in _FIT_TIERS.items():
        if code in codes:
            return tier
    tiers = sorted(option.tier for option in options)
    return tiers[0] if tiers else "none"


def route_view(decision: Any) -> RouteView:
    """A ``RouteDecision`` (Section 8.4) as a ``RouteView``; attribute access only,
    so the bench keeps no import-time dependency on the router."""
    options: list[RouteOption] = []
    for candidate in decision.candidates:
        ref = candidate.ref
        installed = candidate.origin == "installed"
        options.append(
            RouteOption(
                origin="installed" if installed else "template",
                tier=candidate.tier,
                eligible=bool(candidate.eligible),
                model=str(candidate.model),
                name=str(ref.name) if installed else None,
                revision=int(ref.revision) if installed else None,
                key=None if installed else str(ref.key),
                r=candidate.r,
                ref=ref,
            )
        )
    target = decision.target
    index: int | None = None
    if target is not None:
        for position, option in enumerate(options):
            if option.origin == "installed":
                same = option.name == getattr(target, "name", None) and (
                    option.revision == getattr(target, "revision", None)
                )
            else:
                same = option.key == getattr(target, "key", None)
            if same:
                index = position
                break
    codes = tuple(str(code) for code in decision.codes)
    return RouteView(
        action=decision.action,
        tier=_best_tier(codes, options),
        options=tuple(options),
        target=index,
        chosen_rank=decision.chosen_rank,
        explored=bool(decision.explored),
        codes=codes,
        decision_ms=max(0.0, float(decision.decision_ms)),
        decision=decision,
    )


class _RouterSession:
    """``architect.routing`` behind the seam (Section 14 Phase B): the real ``Router``
    over the cell's registry, library and telemetry and the bench's route log."""

    def __init__(self, context: RouteContext) -> None:
        self._router_module = importlib.import_module("architect.routing.router")
        self._log_module = importlib.import_module("architect.routing.log")
        evidence = importlib.import_module("architect.routing.evidence")
        log = self._log_module.RouteLog(context.log_root)
        index = evidence.EvidenceIndex(
            context.registry,
            context.library,
            context.telemetry,
            log,
            context.workspace,
            providers=context.providers,
            promotion_reuses=context.library.promotion_reuses,
        )
        policy: Any = context.policy
        if policy is None:
            policy = self._router_module.RoutePolicy()
        self.min_template_reliability = float(policy.min_template_reliability)
        self._context = context
        self._router = self._router_module.Router(index, policy, log)

    def route(self, spec: Spec, *, ladder: tuple[str, ...], budget_cap: Budget) -> RouteView:
        probe = self._router_module.TaskProbe(
            origin="bench", spec=spec, budget_cap=budget_cap, ladder=ladder, grant_reuse=True
        )
        return route_view(self._router.route(probe))

    def reuse(self, view: RouteView, spec: Spec) -> HarnessArtifact:
        artifact: HarnessArtifact = self._router_module.load_reuse_target(
            view.decision, self._context.registry, spec
        )
        return artifact

    def warm(self, view: RouteView, option: RouteOption) -> LibraryLookup | None:
        if option.origin != "template" or option.ref is None:
            return None
        # ``RoutedLibrary`` re-authenticates the template on every lookup and hands
        # store and verification to the cell's library unchanged (Section 10.2).
        routed: LibraryLookup = self._router_module.RoutedLibrary(
            option.ref, fallback=self._context.library
        )
        return routed

    def outcome(
        self,
        view: RouteView,
        *,
        outcome: RouteOutcome,
        stage4_passed: bool | None,
        evidence: Evidence,
        compile_cost_usd: float,
    ) -> None:
        decision: Any = view.decision
        record = self._log_module.OutcomeRecord(
            decision_id=decision.decision_id,
            timestamp=datetime.now(UTC),
            outcome=outcome,
            stage4_passed=stage4_passed,
            evidence_class=evidence,
            compile_cost_usd=compile_cost_usd,
        )
        self._router.outcome(decision, record)


def open_router(context: RouteContext) -> RouteSession:
    """The real routing seam; needs ``architect.routing.router`` (WP1)."""
    return _RouterSession(context)


def routing_available() -> bool:
    """True when the router module (WP1) is importable in this interpreter."""
    return _has_module("architect.routing.router")


class _LookupOnly:
    """A warm-start library for an oracle-only compile: ``lookup`` and nothing else,
    so the Compiler records no verification and stores no template for it."""

    def __init__(self, inner: LibraryLookup) -> None:
        self._inner = inner

    def lookup(self, spec: Spec, model: str) -> HarnessArtifact | None:
        return self._inner.lookup(spec, model)


_METER_FIELDS = (
    "calls",
    "failed",
    "tokens_in",
    "tokens_out",
    "cached_tokens_in",
    "cost_usd",
    "wall_s",
    "image_inputs",
    "image_tokens_estimate",
)


def _usage(meter: CallMeter, before: CallMeter | None = None) -> CallMeter:
    """A copy of ``meter``'s counts, minus ``before`` when given (one activity)."""
    usage = CallMeter()
    for name in _METER_FIELDS:
        base = getattr(before, name) if before is not None else 0
        setattr(usage, name, getattr(meter, name) - base)
    return usage


@dataclass
class _Measure:
    """One compile or reuse inside a routed cell; numbers and the artifact only."""

    artifact: HarnessArtifact | None
    licensed: bool
    score: float | None
    held_out_count: int
    stages: tuple[int, ...]
    failures: list[str]
    usage: CallMeter
    wall_s: float
    warm_started: bool = False
    params: dict[str, JsonValue] = field(default_factory=dict)
    components: tuple[str, ...] = ()
    forge: ForgeFields | None = None  # a Phase A Forge arm's build counts and agent spend

    @property
    def cost_usd(self) -> float:
        return max(0.0, self.usage.cost_usd)


class _Floors:
    """Floor evaluators for a routed cell's own task and its foreign probe's task."""

    def __init__(self) -> None:
        self._cases: dict[str, tuple[list[EvaluationCase], BenchTask]] = {}
        self._made: dict[str, FloorEvaluator] = {}

    @staticmethod
    def _key(definition: FloorDefinition) -> str:
        return definition.model_dump_json()

    def add(
        self, definition: FloorDefinition, cases: list[EvaluationCase], task: BenchTask
    ) -> None:
        self._cases[self._key(definition)] = (cases, task)

    def task(self, definition: FloorDefinition) -> BenchTask:
        entry = self._cases.get(self._key(definition))
        if entry is None:
            raise FloorError("Floor is not this bench dataset")
        return entry[1]

    def factory(self, definition: FloorDefinition) -> FloorEvaluator:
        entry = self._cases.get(self._key(definition))
        if entry is None:
            raise FloorError("Floor is not this bench dataset")
        cases, task = entry
        floor = HeldOutTasks(definition, deepcopy(cases), _checker(task))
        self._made.setdefault(self._key(definition), floor)
        return floor

    def smoke(self, definition: FloorDefinition) -> FloorEvaluator:
        """Forge's smoke floor (8.2): the parent's tuning partition only, as in
        ``_text_cell``; no held-out case can reach it."""
        parent = self._made.get(self._key(definition)) or self.factory(definition)
        task = self.task(definition)
        return HeldOutTasks(definition, list(parent.tuning), _checker(task))


def _unit(score: float | None) -> float | None:
    """A held-out score clipped to [0, 1] for the oracle (bench floors are [0, 1])."""
    if score is None or not math.isfinite(score):
        return None
    return min(1.0, max(0.0, float(score)))


def merge_forge(first: ForgeFields | None, second: ForgeFields) -> ForgeFields:
    """Sum the Phase A Forge arms of one routed cell into one ``ForgeFields``.

    Counts, dollars, tokens, seconds and sizes add up; the mode is ``agent`` when
    any arm ran an agent (the only arm with build spend), with that arm's model.
    """
    if first is None:
        return second
    agent = first if first.mode == "agent" else second
    summed = {
        name: getattr(first, name) + getattr(second, name)
        for name in (
            "checks", "dryruns", "measures", "submissions", "accepted", "spawns",
            "agent_cost_usd", "agent_tokens_in", "agent_tokens_out", "agent_wall_s",
            "harness_nodes", "harness_bytes", "subharnesses",
        )
    }
    return ForgeFields(
        mode=agent.mode, agent_model=agent.agent_model, language=2, **summed
    )


class _RoutedCell:
    """Phase A (install the arms), then Phase B (route and execute every probe) with
    the oracle, for one routed cell (Section 14). One row per probe."""

    def __init__(
        self,
        ctx: _Context,
        identity: _Identity,
        sized: _Sized,
        root: Path,
        meter: CallMeter,
        totals: _Totals,
    ) -> None:
        assert sized.spec is not None and sized.compile_budget is not None
        self.ctx, self.identity, self.sized = ctx, identity, sized
        self.cell, self.spec, self.root = sized.cell, sized.spec, root
        self.budget: Budget = sized.compile_budget
        self.meter, self.totals = meter, totals
        self.floors = _Floors()
        self.probe: Probe = "exact"
        bundle = ctx.bundle(self.cell.task, self.cell.size)
        state = OracleState()
        state.merge(bundle.state)
        self.floors.add(self.spec.floor, bundle.cases, self.cell.task)
        foreign = sized.foreign
        self.foreign: _Sized | None = None
        if (
            foreign is not None
            and foreign.code is None
            and foreign.spec is not None
            and foreign.compile_budget is not None
        ):
            self.foreign = foreign
            other = ctx.bundle(foreign.cell.task, foreign.cell.size)
            state.merge(other.state)
            self.floors.add(foreign.spec.floor, other.cases, foreign.cell.task)
        base = ctx.factory_for("default", state)

        def metered(model: str) -> ModelAdapter:
            return instrument(base(model), meter, clock=ctx.clock)

        self.metered = metered
        self.store = TelemetryStore(root / "telemetry")
        self.runtime = Runtime(self.store, adapter_factory=metered)
        self.library = HarnessLibrary(root / "library")
        self.registry = Registry(root / "registry")
        self.phase_a_results: dict[str, _Measure] = {}
        self.phase_a_forge: ForgeFields | None = None
        self.installed = 0
        self.cell_failures: list[str] = []

    def close(self) -> None:
        self.store.close()

    def _on_stage(self, stage: int) -> None:
        self.totals.stage = stage
        self.totals.emit(self.ctx.clock)

    async def compile(
        self,
        spec: Spec,
        budget: Budget,
        *,
        library: LibraryLookup,
        variant: Variant = "default",
    ) -> _Measure:
        """One compile through the real Compiler; Forge only for a Forge Phase A arm."""
        ctx, cell = self.ctx, self.cell
        task = self.floors.task(spec.floor)
        pseudo = replace(cell, variant=variant)
        tally = ctx.forge_tally(pseudo) if variant in FORGE_VARIANTS else None
        extra: dict[str, object] = {} if tally is None else {"smoke_factory": self.floors.smoke}
        compiler = _forge_compiler(
            (self.root / "artifacts", self.runtime, self.metered, self.floors.factory),
            tally=tally,
            agent=ctx.forge_agent(pseudo, tally),
            pi_config=ctx.pi_config(self.root),
            **extra,
            config=_compile_config(
                tally.settings if tally is not None else None,
                strategy="grid",
                model_ladder=(cell.model,),
                allowed_presets=(task.preset,),
                max_candidates=ctx.config.max_candidates,
                enable_rewriting=False,
                max_rewrites=0,
                compile_budget=budget,
            ),
            library=library,
        )
        compiler.stage_listener = self._on_stage
        before, started = _usage(self.meter), ctx.clock()
        result: CompileResult = await compiler.compile(spec, approved=True)
        artifact = result.artifact
        if tally is not None:
            tally.absorb(artifact)
        report = result.report
        return _Measure(
            forge=tally.fields() if tally is not None else None,
            artifact=artifact,
            licensed=_licensed_on_disk(artifact),
            score=report.floor_score if report is not None else None,
            held_out_count=report.held_out_count if report is not None else 0,
            stages=result.stages,
            failures=_codes(result.failures),
            usage=_usage(self.meter, before),
            wall_s=max(0.0, ctx.clock() - started),
            warm_started=result.warm_started,
            params=dict(artifact.params) if artifact is not None else {},
            components=artifact.components if artifact is not None else (),
        )

    async def phase_a(self) -> None:
        """Compile each selected arm with lifecycle ``install`` (an exact override of
        the suite spec; the slot excludes lifecycle) and install every licensed one
        under ``<variant>-<task_id>`` in this cell's own registry."""
        install = Spec.model_validate(
            {**self.spec.model_dump(mode="json"), "lifecycle": Lifecycle.INSTALL.value}
        )
        arms: dict[str, _Sized] = {"default": self.sized}
        for arm in self.sized.arms:
            arms[arm.cell.variant] = arm
        for variant in ROUTED_ARMS:
            sized = arms.get(variant)
            if sized is None or sized.code is not None:
                continue
            budget = sized.compile_budget or self.budget
            measure = await self.compile(install, budget, library=self.library, variant=variant)
            self.phase_a_results[variant] = measure
            if measure.forge is not None:
                # Build (agent) cost is reported on the exact row, never in ``cost_usd``.
                self.phase_a_forge = merge_forge(self.phase_a_forge, measure.forge)
            if measure.licensed and measure.artifact is not None:
                try:
                    self.registry.install(f"{variant}-{self.cell.task.id}", measure.artifact)
                except (RegistryError, OSError, ValueError) as exc:
                    self.cell_failures.extend(_codes([str(exc)]) or ["registry_install_failed"])
                else:
                    self.installed += 1

    async def replay(
        self, artifact: HarnessArtifact, spec: Spec
    ) -> tuple[list[bool], float | None, int]:
        """Tuning pass bits (and field accuracy) of ``artifact`` on ``spec``'s floor."""
        floor = self.floors.factory(spec.floor)
        task = self.floors.task(spec.floor)
        tuning = list(floor.tuning)
        outputs = await _replay(artifact, self.runtime, self.metered, tuning)
        bits: list[bool] = []
        reports: list[FieldReport] = []
        for case, output in zip(tuning, outputs, strict=True):
            passed = False
            if output is not None:
                try:
                    passed = await floor.score([case], [output]) == 1.0
                except (FloorError, ValueError):
                    passed = False
            bits.append(passed)
            if output is not None:
                report = _field_report(task, case.expected, output)
                if report is not None:
                    reports.append(report)
        fields = sum(item.fields for item in reports)
        accuracy = sum(item.matched for item in reports) / fields if fields else None
        return bits, accuracy, len(tuning)

    def target(self, probe: Probe) -> tuple[Spec, Budget] | None:
        """The probe's Spec and compile budget; None when the foreign task is skipped."""
        if probe == "foreign":
            foreign = self.foreign
            if foreign is None or foreign.spec is None or foreign.compile_budget is None:
                return None
            return foreign.spec, foreign.compile_budget
        siblings = load_siblings(self.ctx.config.suite)
        return probe_spec(self.spec, probe, self.cell.task, siblings=siblings), self.budget

    async def execute(
        self, session: RouteSession, view: RouteView, spec: Spec, budget: Budget
    ) -> tuple[_Measure | None, tuple[str, int] | None, list[str]]:
        """Run the decision: reuse through ``load_reuse_target`` (no compile), a warm
        start from the chosen template, or a normal compile. A reuse or warm start
        that cannot execute is recorded and falls back to a normal compile."""
        failures: list[str] = []
        if view.action == "refuse":
            return None, None, failures
        if view.action == "reuse_installed" and view.target is not None:
            before, started = _usage(self.meter), self.ctx.clock()
            try:
                artifact = session.reuse(view, spec)
            except (BenchError, HeldOutAccessError):
                raise
            except Exception as exc:
                failures.extend(_codes([getattr(exc, "code", str(exc))]) or ["route_failed"])
            else:
                report = artifact.report
                measure = _Measure(
                    artifact=artifact,
                    licensed=_licensed_on_disk(artifact),
                    score=report.floor_score if report is not None else None,
                    held_out_count=report.held_out_count if report is not None else 0,
                    stages=(),
                    failures=[],
                    usage=_usage(self.meter, before),
                    wall_s=max(0.0, self.ctx.clock() - started),
                    params=dict(artifact.params),
                    components=artifact.components,
                )
                return measure, ("option", view.target), failures
        if view.action == "warm_start" and view.target is not None:
            library = session.warm(view, view.options[view.target])
            if library is None:
                failures.append("route_target_changed")
            else:
                measure = await self.compile(spec, budget, library=library)
                return measure, ("option", view.target), failures
        measure = await self.compile(spec, budget, library=self.library)
        return measure, ("compile", 0), failures

    async def score(
        self,
        session: RouteSession,
        view: RouteView,
        option: RouteOption,
        spec: Spec,
        budget: Budget,
    ) -> OracleOption | None:
        """An installed entry scores its Stage 4 held-out score at $0; a template is a
        warm-started compile of the probe from it (lookup only: no verification is
        recorded and nothing is stored for an oracle-only compile)."""
        if option.origin == "installed":
            if option.name is None:
                return None
            try:
                artifact = self.registry.get(option.name, option.revision)
            except (RegistryError, OSError, ValueError):
                return None
            report = artifact.report
            score = report.floor_score if report is not None else None
            return OracleOption(score=_unit(score), cost_usd=0.0)
        library = session.warm(view, option)
        if library is None:
            return None
        measure = await self.compile(spec, budget, library=_LookupOnly(library))
        return OracleOption(score=_unit(measure.score), cost_usd=measure.cost_usd)

    async def probe_row(self, session: RouteSession, probe: Probe) -> BenchRow:
        """Route one probe, execute the decision, score every eligible option."""
        self.probe = probe
        target = self.target(probe)
        if target is None:
            foreign = self.sized.foreign
            code = foreign.code if foreign is not None and foreign.code else "extra_missing"
            return self.row(probe, _Outcome(failures=[code]), CallMeter(), 0.0, self.spec)
        spec, budget = target
        view = session.route(spec, ladder=(self.cell.model,), budget_cap=spec.budget)
        executed, executed_key, failures = await self.execute(session, view, spec, budget)
        # The oracle over every eligible option plus the cold (no warm start) compile.
        options: list[OracleOption] = []
        keys: list[tuple[str, int]] = []
        for position, option in enumerate(view.options):
            if not option.eligible or (option.origin == "installed" and option.tier != "A"):
                # An installed entry of another slot can never be reused for this probe.
                continue
            if executed is not None and executed_key == ("option", position):
                scored: OracleOption | None = OracleOption(
                    score=_unit(executed.score), cost_usd=executed.cost_usd
                )
            else:
                scored = await self.score(session, view, option, spec, budget)
            if scored is not None:
                options.append(scored)
                keys.append(("option", position))
        cold = self.phase_a_results.get("default") if probe == "exact" else None
        if (
            cold is None
            and executed is not None
            and executed_key == ("compile", 0)
            and not executed.warm_started
        ):
            cold = executed
        if cold is None:
            cold = await self.compile(spec, budget, library=NullLibrary())
        options.append(OracleOption(score=_unit(cold.score), cost_usd=cold.cost_usd))
        keys.append(("compile", 0))
        oracle_rank: int | None = None
        regret: float | None = None
        if executed is not None and executed_key in keys:
            assert executed_key is not None
            oracle_rank, regret = oracle(options, keys.index(executed_key))
        compile_cost = executed.cost_usd if executed is not None else 0.0
        reused = executed is not None and executed_key != ("compile", 0) and (
            view.action == "reuse_installed"
        )
        if executed is not None:
            session.outcome(
                view,
                outcome="delivered" if reused else (
                    "licensed" if executed.licensed else "unlicensed"
                ),
                stage4_passed=None if reused else executed.licensed,
                evidence=evidence_label(self.ctx.provider(self.cell.model)),
                compile_cost_usd=compile_cost,
            )
        ready = sum(
            1
            for option in view.options
            if option.origin == "template"
            and option.tier == "B"
            and option.eligible
            and option.r is not None
            and option.r >= session.min_template_reliability
        )
        route = RouteFields(
            probe=probe,
            action=view.action,
            tier=view.tier,
            candidates=len(view.options),
            chosen_rank=view.chosen_rank,
            oracle_rank=oracle_rank,
            regret=regret,
            compile_cost_avoided_usd=cold.cost_usd - compile_cost,
            fallback=view.action in ("compile", "refuse") or executed_key == ("compile", 0),
            explored=view.explored,
            decision_ms=view.decision_ms,
            round=self.cell.round,
            codes=tuple(_codes(view.codes)),
            installed=self.installed,
            templates_ready=ready,
            target=route_target(view),
        )
        outcome = _Outcome(route=route, failures=list(failures))
        if probe == "exact":
            outcome.failures = [*self.cell_failures, *failures]
            outcome.forge = self.phase_a_forge
        if probe == "foreign" and self.foreign is not None:
            outcome.floor_limit = self.foreign.cell.task.floor_limit
        if executed is None:
            outcome.tuning_cases = (self._size(probe) + 1) // 2
            return self.row(probe, outcome, CallMeter(), 0.0, spec)
        outcome.licensed = executed.licensed
        outcome.held_out_score = executed.score
        outcome.held_out_count = executed.held_out_count
        outcome.stages = executed.stages
        outcome.failures = [*outcome.failures, *executed.failures]
        outcome.params = executed.params
        outcome.components = executed.components
        artifact = executed.artifact
        if artifact is None:
            outcome.tuning_cases = (self._size(probe) + 1) // 2
        else:
            # The replay is measurement for paired statistics, never routed-arm cost.
            outcome.artifact_digest = artifact.artifact_digest
            bits, accuracy, count = await self.replay(artifact, spec)
            outcome.tuning_bits, outcome.field_accuracy = bits, accuracy
            outcome.tuning_cases = count
        return self.row(probe, outcome, executed.usage, executed.wall_s, spec)

    def _size(self, probe: Probe) -> int:
        if probe == "foreign" and self.foreign is not None:
            return self.foreign.cell.size
        return self.cell.size

    def row(
        self, probe: Probe, outcome: _Outcome, usage: CallMeter, wall_s: float, spec: Spec
    ) -> BenchRow:
        # The cell's reservation is shown once, on its exact-probe row.
        sized = replace(
            self.sized,
            spec=spec,
            reserved_usd=self.sized.reserved_usd if probe == "exact" else 0.0,
        )
        return _row(self.ctx, self.identity, sized, outcome, usage, wall_s)


def _routed_failure(
    ctx: _Context,
    identity: _Identity,
    sized: _Sized,
    probe: Probe,
    failures: list[str],
    forge: ForgeFields | None = None,
) -> BenchRow:
    """A probe row that never reached a decision: its failures plus the guard. The
    exact row still carries whatever Phase A Forge build spend was made."""
    sized = replace(sized, reserved_usd=sized.reserved_usd if probe == "exact" else 0.0)
    outcome = _Outcome(failures=list(failures), forge=forge if probe == "exact" else None)
    return _row(ctx, identity, sized, outcome, CallMeter(), 0.0)


async def _run_routed_cell(
    ctx: _Context,
    identity: _Identity,
    sized: _Sized,
    totals: _Totals,
    workspace_root: Path | None,
) -> list[BenchRow]:
    """One routed cell: Phase A, then one row per probe (Section 14).

    A skipped cell writes one exact-probe row carrying its code and
    ``route_did_not_run``. A failure part-way keeps the rows already built and
    marks every remaining probe with the failure (and the guard).
    """
    meter = CallMeter()
    totals.meter = meter
    totals.cell = sized.cell
    totals.stage = None
    meter.listener = lambda: totals.emit(ctx.clock)
    if sized.code is not None:
        return [_routed_failure(ctx, identity, sized, "exact", [sized.code])]
    totals.emit(ctx.clock)
    opener = ctx.routing or open_router
    try:
        workspace = _Workspace(workspace_root)
    except OSError:
        raise BenchError("bench_workspace_error") from None
    rows: list[BenchRow] = []
    failed: list[str] = []
    cell: _RoutedCell | None = None
    try:
        with _signing_key(workspace) as root, audit_scope(ctx.config.audit):
            cell = _RoutedCell(ctx, identity, sized, root, meter, totals)
            try:
                await cell.phase_a()
                assert ctx.route_log is not None
                _fresh_evidence_cache(ctx.route_log)
                session = opener(
                    RouteContext(
                        registry=cell.registry,
                        library=cell.library,
                        telemetry=cell.store,
                        log_root=ctx.route_log,
                        workspace=root,
                        providers={sized.cell.model: ctx.provider(sized.cell.model)},
                        policy=_routed_policy(ctx),
                    )
                )
                for probe in PROBES:
                    rows.append(await cell.probe_row(session, probe))
            finally:
                cell.close()
    except asyncio.CancelledError:
        failures = ["cancelled"]
        if not _remove(workspace):
            failures.append("workspace_error")
        current: Probe = cell.probe if cell is not None else "exact"
        build = cell.phase_a_forge if cell is not None and not rows else None
        raise _Cancelled(
            _routed_failure(ctx, identity, sized, current, failures, build), done=rows
        ) from None
    except BenchError:
        _remove(workspace)
        raise
    except HeldOutAccessError:
        # Held-out cases are private to Stage 4 (as in ``_run_cell``).
        _remove(workspace)
        raise BenchError("bench_held_out_access") from None
    except OSError:
        failed = ["workspace_error"]
    except Exception:
        failed = ["compilation_failed"]
    if not _remove(workspace):
        failed = [*failed, "workspace_error"]
    build = cell.phase_a_forge if cell is not None and not rows else None
    for probe in PROBES[len(rows) :]:
        rows.append(_routed_failure(ctx, identity, sized, probe, failed, build))
    return rows


class _Identity:
    """Run-wide identity columns; the price-table hash is per model."""

    def __init__(self, config: BenchConfig, profile: Profile | None) -> None:
        self.bench_id = str(uuid.uuid4())
        self.suite_digest = suite_digest(config.suite)
        self.preset_fingerprint = preset_fingerprint()
        self.commit = architect_commit()
        self._config = config
        self._profile = profile
        self._prices: dict[Path | None, str] = {}

    def prices_path(self, model: str) -> Path | None:
        """The price table ``model``'s adapter quotes from; None is the default table."""
        profile = self._profile
        if self._config.provider == "fake" or profile is None:
            return None
        settings = profile.models.get(model)
        if settings is None or settings.prices is None:
            return None
        return profile.resolve(settings.prices)

    def prices_sha256(self, model: str) -> str:
        path = self.prices_path(model)
        if path not in self._prices:
            self._prices[path] = prices_sha256(path)
        return self._prices[path]


def _row(
    ctx: _Context,
    identity: _Identity,
    sized: _Sized,
    outcome: _Outcome,
    meter: CallMeter,
    wall_s: float,
) -> BenchRow:
    cell, config = sized.cell, ctx.config
    task = cell.task
    spec = sized.spec or _cell_spec(ctx, cell)
    bits = outcome.tuning_bits
    delivery = outcome.delivery
    failures = list(outcome.failures)
    if not _forge_ran(cell.variant, outcome.forge) and FORGE_DID_NOT_RUN not in failures:
        # Forge must never silently not run; such a row is never a Forge result.
        failures.append(FORGE_DID_NOT_RUN)
    if not _route_ran(cell.variant, outcome.route) and ROUTE_DID_NOT_RUN not in failures:
        # Routing must never silently not run (Section 14): the run fails on this row.
        failures.append(ROUTE_DID_NOT_RUN)
    manifest = None
    if task.kind in ("vision", "pipeline") and sized.code not in ("extra_missing",):
        manifest = ctx.bundle(task, cell.size).pages_manifest_sha256
    return BenchRow(
        bench_id=identity.bench_id,
        timestamp=datetime.now(UTC),
        label=config.label,
        suite_version=config.suite,
        suite_digest=identity.suite_digest,
        task_id=task.id,
        tier=config.tier,
        kind=task.kind,
        provider=ctx.provider(cell.model),
        model=cell.model,
        preset=task.preset,
        variant=cell.variant,
        params=numeric_params(outcome.params),
        components=tuple(categorical(item) for item in outcome.components),
        max_candidates=config.max_candidates,
        seed=config.seed,
        size=cell.size,
        repeat=cell.repeat,
        spec_hash=spec.spec_hash,
        preset_fingerprint=identity.preset_fingerprint,
        prices_sha256=identity.prices_sha256(cell.model),
        architect_commit=identity.commit,
        pages_manifest_sha256=manifest,
        licensed=outcome.licensed,
        held_out_score=outcome.held_out_score,
        held_out_count=outcome.held_out_count,
        floor_limit=task.floor_limit if outcome.floor_limit is None else outcome.floor_limit,
        tuning_score=(sum(bits) / len(bits)) if bits else None,
        tuning_pass_bits=encode_pass_bits(bits) if bits is not None else None,
        tuning_cases=outcome.tuning_cases,
        field_accuracy=outcome.field_accuracy,
        delivery_score=outcome.delivery_score,
        delivery_cases=outcome.delivery_cases,
        stages=outcome.stages,
        failures=tuple(failures),
        calls=meter.calls,
        tokens_in=meter.tokens_in,
        tokens_out=meter.tokens_out,
        cached_tokens_in=min(meter.cached_tokens_in, meter.tokens_in),
        cost_usd=meter.cost_usd,
        reserved_usd=sized.reserved_usd if sized.code is None else 0.0,
        per_call_reservation_usd=sized.per_call_usd,
        wall_s=max(0.0, wall_s),
        image_inputs=meter.image_inputs,
        image_tokens_estimate=meter.image_tokens_estimate,
        evidence=evidence_label(ctx.provider(cell.model)),
        floor_origin=task.floor_origin,
        arc=outcome.arc,
        forge=outcome.forge,
        artifact_digest=outcome.artifact_digest,
        route=outcome.route,
        api_model=_api_model(ctx, cell.model),
        plain=spec_setting(spec, "plain") is True,
        reply_json=_REPLY_JSON.validate_python(spec_setting(spec, "reply_json") or "strict"),
        invalid_policy=_INVALID_POLICY.validate_python(
            spec_setting(spec, "invalid_policy") or "fail_partition"
        ),
        floor_split=config.floor_split if task.is_public else None,
        delivery_split=(
            config.delivery_split if task.is_public and delivery is not None else None
        ),
        floor_cases_sha256=outcome.floor_cases_sha256,
        delivery_cases_sha256=delivery.cases_sha256 if delivery is not None else None,
        suite_frozen=(
            public_tasks.load_public(task.id).settings.frozen if task.is_public else None
        ),
        tuning_replay=config.tuning_replay,
        held_out_correct=outcome.held_out_correct,
        held_out_invalid=outcome.held_out_invalid,
        held_out_over_budget=outcome.held_out_over_budget,
        held_out_mean_cost_usd=outcome.held_out_mean_cost_usd,
        compile_cost_usd=outcome.compile_cost_usd,
        delivery_pass_bits=encode_pass_bits(delivery.passed) if delivery is not None else None,
        delivery_invalid=delivery.invalid if delivery is not None else None,
        delivery_over_budget=delivery.over_budget if delivery is not None else None,
        delivery_cost_usd=delivery.cost_usd if delivery is not None else None,
        delivery_calls=delivery.calls if delivery is not None else None,
        delivery_zero_call_runs=delivery.zero_call_runs if delivery is not None else None,
        delivery_p50_s=delivery.p50_s if delivery is not None else None,
        delivery_p95_s=delivery.p95_s if delivery is not None else None,
        delivery_run_costs_usd=delivery.run_costs_usd if delivery is not None else None,
        delivery_field_accuracy=delivery.field_accuracy if delivery is not None else None,
        delivery_invalid_bits=(
            encode_pass_bits(delivery.invalid_runs) if delivery is not None else None
        ),
        delivery_over_budget_bits=(
            encode_pass_bits(delivery.over_budget_runs) if delivery is not None else None
        ),
    )


def _api_model(ctx: _Context, model: str) -> str | None:
    """The provider's model id behind a profile alias (Section 9.4), else None."""
    profile = ctx.profile
    if ctx.config.provider != "profile" or profile is None:
        return None
    value = getattr(profile.models.get(model), "api_model", None)
    return value if isinstance(value, str) and 0 < len(value) <= 128 else None


def _forge_audit() -> ModuleType:
    """The Forge audit module (Section 8.2), imported only by a run that audits."""
    return importlib.import_module(FORGE_AUDIT_MODULE)


def open_audit(directory: Path) -> None:
    """Create and probe the audit directory before any spend (``audit_unwritable``)."""
    try:
        _forge_audit().ForgeAudit(directory)
    except OSError:
        raise BenchError("audit_unwritable") from None


@contextmanager
def audit_scope(directory: Path | None) -> Iterator[None]:
    """``forge_audit(directory)`` around one cell, or nothing without a directory.

    The audit is a context variable of the cell's own task, never a process-wide
    observer, so only this cell's compiles (and the child compiles they spawn) are
    audited. A directory that cannot be opened is ``audit_unwritable``.
    """
    with ExitStack() as stack:
        if directory is not None:
            scope: Callable[[Path], AbstractContextManager[object]] = _forge_audit().forge_audit
            try:
                stack.enter_context(scope(directory))
            except OSError:
                raise BenchError("audit_unwritable") from None
        yield


async def _run_cell(
    ctx: _Context,
    identity: _Identity,
    sized: _Sized,
    totals: _Totals,
    workspace_root: Path | None,
) -> BenchRow:
    meter = CallMeter()
    totals.meter = meter
    totals.cell = sized.cell
    totals.stage = None
    meter.listener = lambda: totals.emit(ctx.clock)
    started = ctx.clock()
    if sized.code is not None:
        return _row(ctx, identity, sized, _Outcome(failures=[sized.code]), meter, 0.0)
    if _host_memory_low(ctx.config, sized.cell):
        # Checked as the cell starts (earlier cells may still hold memory): nothing
        # runs or is spent, and the guard marks the row forge_did_not_run too.
        return _row(ctx, identity, sized, _Outcome(failures=[HOST_MEMORY_LOW]), meter, 0.0)
    totals.emit(ctx.clock)
    outcome = _Outcome()
    try:
        workspace = _Workspace(workspace_root)
    except OSError:
        raise BenchError("bench_workspace_error") from None
    try:
        with _signing_key(workspace) as root, audit_scope(ctx.config.audit):
            task = sized.cell.task
            if task.id == "arc-preview":
                await _preview_cell(ctx, sized, root, meter, totals, outcome)
            elif task.kind == "game":
                await _local_games_cell(ctx, sized, root, meter, totals, outcome)
            else:
                await _text_cell(ctx, sized, root, meter, totals, outcome)
    except asyncio.CancelledError:
        outcome.failures = [*outcome.failures, "cancelled"]
        # Removed before the row is built, so a workspace a cancelled cell leaks (a
        # file still held open while the sandbox or worker is torn down) is on the row.
        if not _remove(workspace):
            outcome.failures = [*outcome.failures, "workspace_error"]
        cancelled = _row(ctx, identity, sized, outcome, meter, ctx.clock() - started)
        raise _Cancelled(cancelled) from None
    except BenchError:
        _remove(workspace)
        raise
    except HeldOutAccessError:
        # Held-out cases are private to Stage 4. A guard trip anywhere else is a
        # breach of the bench's own authority boundary: stop, never relabel it.
        _remove(workspace)
        raise BenchError("bench_held_out_access") from None
    except OSError:
        outcome.failures = [*outcome.failures, "workspace_error"]
    except Exception:
        outcome.failures = [*outcome.failures, "compilation_failed"]
    if not _remove(workspace):
        outcome.failures = [*outcome.failures, "workspace_error"]
    return _row(ctx, identity, sized, outcome, meter, ctx.clock() - started)


class _Cancelled(Exception):
    def __init__(self, row: BenchRow, *, done: Sequence[BenchRow] = ()) -> None:
        super().__init__("cancelled")
        self.row = row
        self.done = tuple(done)  # a routed cell's probe rows finished before the cancel


def _remove(workspace: _Workspace) -> bool:
    return workspace.remove()


_EVIDENCE_CACHE = "evidence.sqlite"


def _fresh_evidence_cache(log_root: Path) -> None:
    """Drop the route log's telemetry cache before a routed cell opens the router.

    The bench keeps one route log (decisions, outcomes) across cells, but every
    cell has its own telemetry journal. The cache under ``<log_root>/evidence.sqlite``
    is keyed only by a byte offset into one journal, so a cache left by an earlier
    cell would skip the head of a longer journal and keep that cell's aggregates.
    Each cell therefore replays its own journal into a fresh cache. An undeletable
    cache raises ``OSError`` (the cell's ``workspace_error``).
    """
    for suffix in ("", "-wal", "-shm", "-journal"):
        (log_root / f"{_EVIDENCE_CACHE}{suffix}").unlink(missing_ok=True)


@contextmanager
def _signing_key(workspace: _Workspace) -> Iterator[Path]:
    """Point the signing key at the workspace for the cell, then restore it."""
    try:
        yield workspace.enter()
    finally:
        workspace.restore()


def _write_summary(
    path: Path, result_rows: Sequence[BenchRow], bound_usd: float, skipped: object, cancelled: bool
) -> None:
    payload = {
        "bench_id": result_rows[0].bench_id if result_rows else None,
        "summary": summarize(result_rows).model_dump(mode="json"),
        "bound_usd": bound_usd,
        "skipped": skipped,
        "cancelled": cancelled,
        "evidence": sorted({row.evidence for row in result_rows}),
    }
    path.write_text(
        json.dumps(payload, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )


async def run_bench(
    config: BenchConfig,
    *,
    factory: Callable[[str], ModelAdapter] | None = None,
    prices: Prices | None = None,
    profile: Profile | None = None,
    on_progress: Callable[[BenchProgress], None] | None = None,
    on_bound: Callable[[Bound], Awaitable[bool]] | None = None,
    clock: Callable[[], float] = time.monotonic,
    workspace_root: Path | None = None,
    bench_id: str | None = None,
    routing: RouteOpener | None = None,
    resume: str | None = None,
) -> BenchResult:
    """Run every cell of ``config`` and write ``<out>/<bench_id>/rows.jsonl``.

    ``factory`` overrides the adapters (tests); by default the fake provider uses
    per-cell oracles and a profile run uses the profile's adapters. The bound is
    computed before any spend and handed to ``on_bound``, which returns False to
    refuse the run (``bench_cancelled``, nothing spent). Cancelling the task
    keeps the rows written so far, appends the in-flight cell as ``cancelled``
    and re-raises ``CancelledError``.

    ``routing`` replaces the router seam of routed cells (tests); by default a
    routed run needs ``architect.routing.router`` and is refused
    (``bench_variant_unknown``) before any spend when it is absent.

    ``resume`` continues bench ``resume`` under ``config.out``: ``config`` must be
    the one it started with (``load_resume``), only its undone cells are bounded and
    run, and the summary covers its kept rows and the new ones.
    """
    if config.provider == "profile" and profile is None and factory is None:
        raise BenchError("bench_profile_required")
    routed = "routed" in config.variants
    if routed and routing is None and not routing_available():
        raise BenchError("bench_variant_unknown")
    models = _models(config, profile, factory)
    ctx = _Context(
        config, profile=profile, factory=factory, prices=prices or load_prices(), clock=clock
    )
    ctx.routing = routing
    cells = plan(config, models)
    previous: _Resume | None = None
    if resume is not None:
        if bench_id is not None and bench_id != resume:
            raise BenchError("bench_resume_mismatch")
        previous = _resume_state(config, models, resume)
        bench_id = _canonical_id(resume)
        cells = _pending(cells, previous.done)
    sized = _size_all(ctx, cells)
    limits = _bound_of(sized, models)
    check_max_usd(config, limits)
    if on_bound is not None and not await on_bound(limits):
        raise BenchError("bench_cancelled")  # the operator declined before any spend
    if config.audit is not None:
        open_audit(config.audit)  # audit_unwritable before any spend or output file
    identity = _Identity(config, profile)
    if bench_id is not None:
        identity.bench_id = str(uuid.UUID(bench_id))
    directory = config.out / identity.bench_id
    rows_path = directory / "rows.jsonl"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if previous is None:
            rows_path.touch()
            record = json.dumps(_run_record(config, models), sort_keys=True, indent=2)
            (directory / RUN_FILE).write_text(record + "\n", encoding="utf-8")
        else:
            # Consent is given: drop undone rows and a cut-short line, atomically.
            staged = directory / "rows.jsonl.resume"
            write_rows(staged, previous.kept)
            os.replace(staged, rows_path)
    except OSError:
        raise BenchError("bench_out_unwritable") from None
    totals = _Totals(total=len(sized), started=clock(), listener=on_progress)
    rows: list[BenchRow] = list(previous.kept) if previous is not None else []
    skipped: list[tuple[TaskId, str, str]] = [
        (item.cell.task.id, item.cell.model, item.code) for item in sized if item.code is not None
    ]
    route_log: _Workspace | None = None
    if routed:
        # One route log per bench (Section 14), in its own short temporary root.
        try:
            route_log = _Workspace(workspace_root)
        except OSError:
            raise BenchError("bench_workspace_error") from None
        ctx.route_log = route_log.path / "routes"
    try:
        for index, item in enumerate(sized):
            totals.done = index
            if item.cell.variant == "routed":
                produced = await _run_routed_cell(ctx, identity, item, totals, workspace_root)
            else:
                produced = [await _run_cell(ctx, identity, item, totals, workspace_root)]
            totals.calls += totals.meter.calls
            totals.cost_usd += totals.meter.cost_usd
            rows.extend(produced)
            try:
                append_rows(rows_path, produced)
            except OSError:
                raise BenchError("bench_out_unwritable") from None
        totals.done = len(sized)
    except _Cancelled as cancelled:
        finished = [*cancelled.done, cancelled.row]
        rows.extend(finished)
        try:
            append_rows(rows_path, finished)
            _write_summary(directory / "summary.json", rows, limits.total_usd, skipped, True)
        except OSError:
            pass
        raise asyncio.CancelledError() from None
    finally:
        if route_log is not None:
            _remove(route_log)
    try:
        _write_summary(directory / "summary.json", rows, limits.total_usd, skipped, False)
    except OSError:
        raise BenchError("bench_out_unwritable") from None
    if rows and all(row.failures == ("arc_unavailable",) for row in rows):
        # Found only while running (no cached public games): the rows are kept on
        # disk, but a run that measured nothing is a failure, not a success.
        raise BenchError("bench_arc_unavailable")
    return BenchResult(
        bench_id=identity.bench_id,
        rows=tuple(rows),
        summary=summarize(rows),
        bound_usd=limits.total_usd,
        cancelled=False,
        skipped=tuple(skipped),
        rows_path=rows_path,
    )
