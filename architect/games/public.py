"""Manufacture a fresh ARC-AGI-3 harness on cached public games (OFFLINE preview).

Lifted from ``scripts/arc_harness.py`` so the bench (``arc-preview``) and the
script share one path; the script stays a thin CLI over this module and its
argv and summary keys are unchanged.

Every call starts from nothing: a fresh temporary workspace, registry and
library, and a seeded random draw of public games split into the five manifest
roles. Architect infers the Spec, the operator approval is applied explicitly,
Stage 4 licenses the harness on official OFFLINE scorecards of held-out public
games, the registry installs it and the installed harness plays the delivery
games. Nothing is reused between runs.

Public game files are downloaded over HTTP with the anonymous public key (never
imported here; only the official worker process runs game code) into the cache
and are never committed. ``adapter="fixture"`` is a deterministic no-cost agent
that only exercises manufacturing and official scoring: its scores are not ARC
results. Cloud adapters (``anthropic``, ``claude-code``) set the host-side
``allow_online_model`` opt-in, so the summary says ``model_binding:
online_preview``; a local model (``ollama``) keeps ``offline``, the
competition's shape. No run is a competition submission or compliance evidence.

Forge (Phase 9, the bench's ``forge`` and ``template`` arms) runs only when the host
sets ``PreviewOptions.forge``; the script never does. It runs on the CHILD game
compiles, where the harness that plays is made, and never on the parent competition
compile: the parent compiles the ``planner`` delegation harness (the only preset the
delegation backend accepts), whose tuning measurement is itself a batch of child
compiles, so a parent session would multiply child compiles per measure and would
not change the playing harness the ARC hypothesis is about (contract Section 14).
Each child Forge sees only the public support-tuning games (its floor's tuning
partition and a support-tuning-only smoke floor); parent, support held-out and
delivery games never reach it.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import secrets
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from architect.backends.pi import PiConfig
from architect.compiler import CompileConfig, Compiler
from architect.compiler.forge import ForgeResult
from architect.compiler.types import ForgeSettings
from architect.floors import HeldOutTasks
from architect.games.competition import CompetitionDescription, CompetitionFactory
from architect.games.evaluation import OfficialPreviewManifest
from architect.games.execution import PlayCounters
from architect.games.factory import GameFactory, GameFactoryConfig
from architect.games.official import OfficialConfig, OfficialGameProvider
from architect.games.types import GameLimits, GameRef
from architect.models import (
    AnthropicAdapter,
    ClaudeCodeAdapter,
    FakeAdapter,
    ModelAdapter,
    ModelRequest,
    ModelResponse,
    OllamaAdapter,
)
from architect.models.types import UsageQuote
from architect.registry import Registry
from architect.runtime import RunResult, Runtime
from architect.spec import Budget, FloorDefinition, HarnessArtifact
from architect.telemetry import TelemetryStore

__all__ = [
    "ADAPTERS",
    "COMPUTE_CAP_WALLETS",
    "DEFAULT_CACHE",
    "LOCAL_PRICES",
    "ONLINE_ADAPTERS",
    "PUBLIC_API",
    "ROLES",
    "FixtureAgent",
    "PreviewForge",
    "PreviewOptions",
    "PreviewTrace",
    "build",
    "close_workspace",
    "fetch_public_games",
    "forge_sessions",
    "manufacture",
    "stage",
]

ROOT = Path(__file__).resolve().parents[2]
ADAPTERS = ("fixture", "anthropic", "claude-code", "ollama")
ONLINE_ADAPTERS = ("anthropic", "claude-code")
# A local model on this host is the competition's shape: no cloud call at play time.
LOCAL_PRICES = ROOT / "examples" / "local" / "prices.json"
DEFAULT_CACHE = ROOT / ".architect" / "arc-public"
ROLES = ("parent_tuning", "parent_held_out", "support_tuning", "support_held_out", "delivery")
PUBLIC_API = "https://three.arcprize.org"
# ``manufacture`` opens this many wallets one after another, each capped at the
# full ``cost_usd``: the proposal compile, the approved compile and the play run
# (whose Spec budget is the same compute cap). The most one manufacture can
# spend is therefore ``COMPUTE_CAP_WALLETS * cost_usd``; a caller that bounds
# spend in advance (the bench) must reserve that much.
COMPUTE_CAP_WALLETS = 3
# The parent competition compile tunes this many planner candidates (``build``).
PARENT_CANDIDATES = 1
_GAME_ID = re.compile(r"^([a-z0-9]{4})-([0-9a-f]{6,64})$")
_CLASS = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


@dataclass(frozen=True)
class PreviewTrace:
    """What the bench records beyond the summary: stage numbers and counters only.

    ``stages`` is the sorted union of the parent compile's stages and every delivered
    child compile's stages (so Stage 3 appears when a delivered child forged).
    ``delivery_counters`` holds the host-side ``PlayCounters`` of each child's
    delivery play in the play run.
    """

    stages: tuple[int, ...] = ()
    delivery_counters: tuple[PlayCounters, ...] = ()


class PreviewOptions(BaseModel):
    """The script's manufacturing arguments (``--out`` is output only), plus host-only
    Forge plumbing.

    The host-only fields (``forge`` to ``observer``) are set only by the bench's
    arc-preview cell. They are never argv and never serialized (``exclude=True``),
    and their defaults mean "no Forge, nobody listening", so the script's argv, its
    options dump and its summary keys are byte-for-byte what they were.
    """

    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, arbitrary_types_allowed=True
    )

    games: int = 5
    seed: int | None = None
    cache: Path = DEFAULT_CACHE
    adapter: Literal["fixture", "anthropic", "claude-code", "ollama"] = "fixture"
    prices: Path = LOCAL_PRICES
    call_timeout_s: float = Field(default=300.0, gt=0)
    num_ctx: int = 65_536
    think: bool | None = None
    model: str = "claude-sonnet-5"
    floor_limit: float = 0.0
    cost_usd: float = 1.0
    compute_s: float = 900.0
    max_actions: int = 24
    offline: bool = False
    # Host-only. Enabled ForgeSettings make every CHILD game compile forge (Stage 3)
    # and let a licensed Forge sandbox artifact deliver (``allow_forge``).
    forge: ForgeSettings | None = Field(default=None, exclude=True)
    # A FRESH Forge agent per child compile (one Pi session each; a replayed script
    # is spent by one session). Unused in template mode.
    forge_agent: Callable[[], ModelAdapter | None] | None = Field(default=None, exclude=True)
    # The caller's effective Pi config; ``manufacture`` adds its own workspace secrets.
    pi_config: PiConfig | None = Field(default=None, exclude=True)
    # Receives each child Stage 3 session's content-free ``ForgeResult``.
    forge_listener: Callable[[ForgeResult], None] | None = Field(default=None, exclude=True)
    # Receives the content-free ``PreviewTrace`` once the manufacture ends.
    observer: Callable[[PreviewTrace], None] | None = Field(default=None, exclude=True)

    def forging(self) -> bool:
        return self.forge is not None and self.forge.enabled


@dataclass(frozen=True)
class PreviewForge:
    """Forge plumbing for the child game compiles of one manufacture (``build``)."""

    settings: ForgeSettings
    agents: Callable[[], ModelAdapter | None] | None
    pi_config: PiConfig
    listener: Callable[[ForgeResult], None] | None
    # The children's own play wallet; the competition cap adds the Forge headroom.
    play_cap: Budget


def forge_sessions(games: int, candidates: int = PARENT_CANDIDATES) -> int:
    """The most child compiles, and so Forge sessions, one manufacture can open.

    ``stage`` deals the drawn games round-robin over ``ROLES``. Every parent-tuning
    game is played once per planner candidate (Stage 2), every parent held-out game
    once (Stage 4) and every delivery game once (the play run), each through one
    fresh child compile that forges at most once. The proposal compile opens none,
    and support games are only the children's floors, never a child compile.
    """
    counts = dict.fromkeys(ROLES, 0)
    for index in range(max(0, games)):
        counts[ROLES[index % len(ROLES)]] += 1
    return counts["parent_tuning"] * candidates + counts["parent_held_out"] + counts["delivery"]


class _PlayRecorder(Runtime):
    """A Runtime that keeps the host-side counters of child delivery plays.

    ``Runtime.run`` of a game artifact is a child's play of its target (tuning and
    verification go through ``evaluate``). While ``delivered`` is a list (the play
    run only), the counters of each play whose backend closed on that run are kept.
    Counters are numbers and action codes only; no frame is kept.
    """

    delivered: list[PlayCounters] | None = None

    async def run(
        self, artifact: HarnessArtifact, task: JsonValue, adapter: ModelAdapter
    ) -> RunResult:
        result = await super().run(artifact, task, adapter)
        backend = self.scoped_game_backend()
        counters = getattr(backend, "last_counters", None)
        if (
            self.delivered is not None
            and getattr(backend, "last_run_id", None) == result.run_id
            and isinstance(counters, PlayCounters)
        ):
            self.delivered.append(counters)
        return result


class FixtureAgent(FakeAdapter):
    """Deterministic no-cost agent: cycles the offered actions, never RESET."""

    def __init__(self) -> None:
        super().__init__()
        self.turn = 0

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=100, tokens_out=100, cost_usd=0)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if "Game data (untrusted):\n" not in request.prompt and "\nOptions: " in request.prompt:
            # A Forge game harness's prompt (explorer and hypothesis templates): name
            # one of the listed options, cycling as on the preset path.
            listed = request.prompt.rsplit("\nOptions: ", 1)[1].split("\n", 1)[0]
            choices = [item.strip() for item in listed.split(",") if item.strip()]
            self.turn += 1
            option = choices[self.turn % len(choices)] if choices else None
            return ModelResponse(
                text=json.dumps({"option": option}),
                model=self.model,
                tokens_in=100,
                tokens_out=100,
                cost_usd=0,
            )
        if "specification inference stage" in request.prompt:
            text = json.dumps(
                {
                    "task": "Play public ARC-AGI-3 games",
                    "domain": "game",
                    "input_schema": {},
                    "output_schema": {},
                    "floor": {"kind": "held_out_game_score", "limit": 0, "dataset": "preview"},
                    "budget": {"cost_usd": 1, "compute_s": 600},
                    "model_target": self.model,
                }
            )
        else:
            data = json.loads(request.prompt.split("Game data (untrusted):\n", 1)[1])
            offered = [a for a in data["observation"]["available_actions"] if a != "RESET"]
            self.turn += 1
            name = offered[self.turn % len(offered)] if offered else "RESET"
            action: dict[str, object] = {"name": name}
            if name == "ACTION6":
                action.update(x=(self.turn * 7) % 64, y=(self.turn * 13) % 64)
            text = json.dumps(
                {
                    "action": action,
                    "plan": "Probe each offered action in turn",
                    "hypothesis": "Some offered action changes the visible state",
                    "prediction": "The frame changes after this action",
                }
            )
        return ModelResponse(text=text, model=self.model, tokens_in=100, tokens_out=100, cost_usd=0)


def _complete(directory: Path) -> str | None:
    """Return the game ID of a complete, self-consistent cache entry, else None."""
    try:
        metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    game_id, class_name = metadata.get("game_id"), metadata.get("class_name")
    match = _GAME_ID.match(game_id) if isinstance(game_id, str) else None
    if match is not None and class_name is None:
        # The SDK stores null and derives the class name from the game ID.
        class_name = match.group(1)[0].upper() + match.group(1)[1:]
    if (
        match is None
        or (match.group(1), match.group(2)) != (directory.parent.name, directory.name)
        or not isinstance(class_name, str)
        or not _CLASS.match(class_name)
        or not (directory / f"{class_name.lower()}.py").is_file()
    ):
        return None
    return str(game_id)


def _download(client: httpx.Client, headers: dict[str, str], game_id: str, cache: Path) -> None:
    """Fetch metadata and source over HTTP, never importing or running the game."""
    base, version = game_id.split("-", 1)
    metadata = client.get(f"/api/games/{base}", headers=headers).raise_for_status().json()
    if not isinstance(metadata, dict) or metadata.get("game_id") != game_id:
        raise RuntimeError("public_game_metadata_mismatch")
    class_name = metadata.get("class_name") or base[0].upper() + base[1:]
    if not isinstance(class_name, str) or not _CLASS.match(class_name):
        raise RuntimeError("public_game_metadata_mismatch")
    metadata["class_name"] = class_name
    source = client.get(f"/api/games/{game_id}/source", headers=headers).raise_for_status().text
    target = cache / base / version
    target.parent.mkdir(parents=True, exist_ok=True)
    # Write into a sibling temp dir and rename, so an interrupted fetch never
    # leaves a metadata.json without its source.
    with tempfile.TemporaryDirectory(dir=target.parent, prefix=".part-") as raw:
        part = Path(raw) / "entry"
        part.mkdir()
        (part / f"{class_name.lower()}.py").write_text(source, encoding="utf-8")
        (part / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        if target.exists():
            shutil.rmtree(target)
        part.rename(target)


def fetch_public_games(cache: Path, *, offline: bool) -> tuple[list[Path], dict[str, object]]:
    """Refresh the public-game cache from the public listing; return complete entries.

    Game code is only downloaded here. It runs later inside the official worker
    process, never in this one, which holds the signing key and model credentials.
    A listing or download failure is an error unless ``offline`` asks for the cache.
    """
    cache.mkdir(parents=True, exist_ok=True)
    refresh: dict[str, object] = {"refreshed": False}
    if not offline:
        with httpx.Client(base_url=PUBLIC_API, timeout=30, trust_env=False) as client:
            key = client.get("/api/games/anonkey").raise_for_status().json()["api_key"]
            headers = {"X-Api-Key": str(key), "Accept": "application/json"}
            listing = client.get("/api/games", headers=headers).raise_for_status().json()
            listed = [item["game_id"] for item in listing]
            if not listed or not all(isinstance(g, str) and _GAME_ID.match(g) for g in listed):
                raise RuntimeError("public_listing_invalid")
            present = {_complete(path.parent) for path in cache.rglob("metadata.json")}
            missing = [game_id for game_id in listed if game_id not in present]
            for game_id in missing:
                _download(client, headers, game_id, cache)
        refresh = {"refreshed": True, "listed": len(listed), "downloaded": len(missing)}
    games = sorted(path.parent for path in cache.rglob("metadata.json") if _complete(path.parent))
    if not games:
        raise RuntimeError("public_games_unavailable")
    refresh["cached"] = len(games)
    return games, refresh


def stage(games: list[Path], count: int, seed: int, staged: Path) -> dict[str, tuple[GameRef, ...]]:
    """Copy a random subset into a fresh staging dir and split it over the roles."""
    usable = []
    for directory in games:
        game_id = _complete(directory)
        metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        if game_id is not None and metadata.get("baseline_actions"):
            ref = GameRef(backend="official_offline", game_id=game_id, revision=directory.name)
            usable.append((directory, ref))
    if count < len(ROLES) or count > len(usable):
        raise ValueError("game_count")
    chosen = random.Random(seed).sample(usable, count)
    for directory, ref in chosen:
        shutil.copytree(directory, staged / ref.game_id, symlinks=False)
    roles: dict[str, list[GameRef]] = {role: [] for role in ROLES}
    for index, (_, ref) in enumerate(chosen):
        roles[ROLES[index % len(ROLES)]].append(ref)
    return {role: tuple(values) for role, values in roles.items()}


def build(
    workspace: Path,
    staged: Path,
    manifest: OfficialPreviewManifest,
    description: CompetitionDescription,
    model: ModelAdapter,
    limits: GameLimits,
    *,
    store: TelemetryStore,
    allow_online_model: bool = False,
    forge: PreviewForge | None = None,
) -> CompetitionFactory:
    """Build the factory over a caller-owned store; ``close_workspace`` releases it.

    With ``forge`` the CHILD game factory forges: its compile config carries the
    enabled settings, ``allow_forge`` is set, and its compiler carries the Pi config
    and the listener, with a fresh agent per child compile. The children's own
    wallet stays ``forge.play_cap``; ``description.compute_cap`` (the parent's) is
    expected to carry the Forge headroom (``manufacture`` sizes it). The parent
    compile never forges: its config keeps Forge disabled and it has no smoke floor.
    """

    def unavailable(definition: FloorDefinition) -> HeldOutTasks:
        raise ValueError("unconfigured_floor")

    runtime = _PlayRecorder(store)
    config = CompileConfig(
        model_ladder=(description.small_model,),
        enable_rewriting=False,
        max_rewrites=0,
        max_candidates=PARENT_CANDIDATES,
        compile_budget=description.compute_cap,
    )
    child_budget = forge.play_cap if forge is not None else description.compute_cap
    child_config = config
    if forge is not None:
        child_config = config.model_copy(
            update={"compile_budget": child_budget, "forge": forge.settings}
        )

    def compiler(name: str, compile_config: CompileConfig) -> Compiler:
        return Compiler(
            workspace / name,
            runtime,
            lambda _: model,
            unavailable,
            config=compile_config,
            inference_adapter=model,
            pi_config=forge.pi_config if forge is not None else None,
        )

    child_compiler = compiler("child-artifacts", child_config)
    if forge is not None:
        child_compiler.forge_listener = forge.listener
    provider = OfficialGameProvider(OfficialConfig(mode="offline", staged_game_dir=staged))
    children = GameFactory(
        child_compiler,
        provider,
        GameFactoryConfig(
            budget=Budget(cost_usd=child_budget.cost_usd, compute_s=child_budget.compute_s),
            compile_config=child_config,
            small_model=description.small_model,
            limits=limits,
            floor_limit=description.floor_limit,
            # Trusted operator opt-in; valid only with this OFFLINE provider.
            allow_online_model=allow_online_model,
            # Trusted host opt-in (the bench's forge and template arms only).
            allow_forge=forge is not None,
        ),
        manifest=manifest,
        forge_agents=forge.agents if forge is not None else None,
    )
    parent = compiler("parent-artifacts", config)
    return CompetitionFactory(parent, Registry(workspace / "registry"), children, provider)


def _preview_forge(
    options: PreviewOptions, tmp: Path, workspace: Path, staged: Path
) -> PreviewForge | None:
    """The child Forge plumbing, or None when this manufacture does not forge.

    The Pi denied set is the caller's (the bench's effective set: profile files and
    the cell's secrets) plus everything this manufacture owns: the temporary root,
    the workspace (telemetry, artifacts, library, registry), the staged games, the
    public-game cache and the signing key.
    """
    settings = options.forge
    if settings is None or not settings.enabled:
        return None
    base = options.pi_config if options.pi_config is not None else PiConfig()
    owned = [
        tmp,
        workspace,
        workspace / "telemetry",
        workspace / "child-artifacts",
        workspace / "parent-artifacts",
        workspace / "library",
        workspace / "registry",
        staged,
        options.cache,
    ]
    key = os.environ.get("ARCHITECT_SIGNING_KEY_FILE")
    if key:
        owned.append(Path(key))
    denied = {Path(item).resolve() for item in (*base.denied_paths, *owned)}
    return PreviewForge(
        settings=settings,
        agents=options.forge_agent if settings.mode == "agent" else None,
        pi_config=base.model_copy(update={"denied_paths": tuple(sorted(denied))}),
        listener=options.forge_listener,
        play_cap=Budget(cost_usd=options.cost_usd, compute_s=options.compute_s),
    )


def _competition_cap(options: PreviewOptions) -> Budget:
    """The parent's compute cap: the play cap, plus Forge's caps per child session.

    Every child Forge dollar and second is also debited to the competition wallets
    above it, so the cap is raised by ``forge_sessions`` times the Forge caps: the
    per-session rule the bench's bound uses. Forge's own nested wallet still caps
    every session, and the children's play wallet stays the play cap.
    """
    cap = Budget(cost_usd=options.cost_usd, compute_s=options.compute_s)
    if options.forge is None or not options.forge.enabled:
        return cap
    sessions = forge_sessions(options.games)
    return Budget(
        cost_usd=cap.cost_usd + options.forge.max_usd * sessions,
        compute_s=cap.compute_s + options.forge.max_s * sessions,
    )


def _agent(options: PreviewOptions, small_model: str) -> ModelAdapter:
    model: ModelAdapter
    if options.adapter == "fixture":
        model = FixtureAgent()
        model.model = small_model
    elif options.adapter == "claude-code":
        # Logged-in Claude Code CLI; costs are API-equivalent, not subscription bills.
        model = ClaudeCodeAdapter(options.model)
    elif options.adapter == "ollama":
        # Local Ollama model on this host; $0, bounded by compute_s only.
        model = OllamaAdapter(
            options.model,
            prices_path=options.prices,
            timeout=options.call_timeout_s,
            num_ctx=options.num_ctx,
            think=options.think,
        )
    else:
        model = AnthropicAdapter(model=options.model)
    return model


async def manufacture(
    options: PreviewOptions, tmp: Path, *, agent: ModelAdapter | None = None
) -> dict[str, object]:
    """Manufacture, license, install and play one fresh harness; return the summary.

    ``agent`` replaces the adapter ``options.adapter`` would build (the bench passes
    its metered profile adapter); ``options.adapter`` still decides the labels.
    """
    workspace, staged = tmp / "ws", tmp / "games"
    seed = options.seed if options.seed is not None else secrets.randbelow(2**32)
    games, refresh = await asyncio.to_thread(
        fetch_public_games, options.cache, offline=options.offline
    )
    roles = await asyncio.to_thread(stage, games, options.games, seed, staged)
    manifest = OfficialPreviewManifest.model_validate(
        {"dataset_id": "arc-agi-3-public", "dataset_revision": f"draw-{seed}", **roles}
    )
    small_model = "fixture" if options.adapter == "fixture" else options.model
    description = CompetitionDescription(
        name="arc-agi-3-harness",
        rules="Public ARC-AGI-3 games, OFFLINE preview scoring. Not a submission.",
        action_schema={"type": "object"},
        observation_schema={"type": "object"},
        preview_dataset="arc-agi-3-public-parent",
        child_support_dataset="arc-agi-3-public-support",
        compute_cap=_competition_cap(options),
        floor_limit=options.floor_limit,
        small_model=small_model,
        submission_constraints=("Offline preview only; submission compliance unverified.",),
        max_games=options.games,
    )
    model = agent if agent is not None else _agent(options, small_model)
    limits = GameLimits(max_actions=options.max_actions, max_turns=options.max_actions)
    online = options.adapter in ONLINE_ADAPTERS
    # Every call reserves its quote (cloud CLIs: the model's full output capacity)
    # against the per-run and competition budgets before it is sent.
    reservation = model.quote(ModelRequest(prompt="")).cost_usd
    base: dict[str, object] = {
        "seed": seed,
        "adapter": options.adapter,
        "model_binding": "online_preview" if online else "offline",
        "per_call_reservation_usd": round(reservation, 6),
        "compliance": "unverified",
    }
    if reservation > options.cost_usd:
        return {**base, "licensed": False, "error": "per_call_reservation_exceeds_cost_cap"}
    fresh = not await asyncio.to_thread(workspace.exists)
    base.update(
        {
            # A fixture agent's score checks the manufacturing and scoring path only.
            "arc_result": options.adapter != "fixture",
            "score_meaning": "pipeline_check_only"
            if options.adapter == "fixture"
            else "offline_public_preview_not_submission",
            "public_games": refresh,
            "games": {role: [ref.game_id for ref in refs] for role, refs in roles.items()},
            "fresh_workspace": fresh,
        }
    )
    # The store and the official worker hold files under ``tmp``. Windows refuses
    # to delete a file another handle still holds, so both are released here, on
    # every exit, before the caller removes the temporary directory.
    store = TelemetryStore(workspace / "telemetry")
    factory: CompetitionFactory | None = None
    trace = _Trace()
    try:
        factory = build(
            workspace,
            staged,
            manifest,
            description,
            model,
            limits,
            store=store,
            allow_online_model=online,
            forge=_preview_forge(options, tmp, workspace, staged),
        )
        return await _license_and_run(factory, description, base, reservation, trace)
    finally:
        try:
            await close_workspace(factory, store)
        finally:
            _report(options, trace)


@dataclass
class _Trace:
    stages: set[int] | None = None
    counters: list[PlayCounters] | None = None


def _report(options: PreviewOptions, trace: _Trace) -> None:
    """Hand the observer its content-free trace; an observer never breaks a run."""
    observer = options.observer
    if observer is None:
        return
    try:
        observer(
            PreviewTrace(
                stages=tuple(sorted(trace.stages or ())),
                delivery_counters=tuple(trace.counters or ()),
            )
        )
    except Exception:
        pass


async def close_workspace(factory: CompetitionFactory | None, store: TelemetryStore) -> None:
    """Release every open workspace handle: the official worker, then the SQLite index."""
    try:
        if factory is not None:
            close = getattr(factory.provider, "close", None)
            if close is not None:
                await close()
    finally:
        store.close()


async def _license_and_run(
    factory: CompetitionFactory,
    description: CompetitionDescription,
    base: dict[str, object],
    reservation: float,
    trace: _Trace | None = None,
) -> dict[str, object]:
    trace = trace if trace is not None else _Trace()
    proposal = await factory.compile(description)
    if not proposal.approval_required:
        raise RuntimeError("proposal_skipped_approval")
    # Explicit operator approval of the exact proposed Spec for this scripted run.
    compiled = await factory.compile(description, approved=True, approved_spec=proposal.spec)
    trace.stages = stages = set(getattr(compiled, "stages", ()))
    summary: dict[str, object] = {
        **base,
        "spec_hash": compiled.spec.spec_hash,
        "licensed": bool(compiled.artifact and compiled.artifact.licensed),
        "compile_failures": list(compiled.failures),
        "score_source": "unavailable",
    }
    if not summary["licensed"]:
        return _budget_hint(summary, list(compiled.failures), reservation)
    entry = await factory.install(compiled, name=description.name)
    if entry.revision != 1:
        raise RuntimeError("harness_reused")
    runtime = factory.compiler.runtime
    recorder = runtime if isinstance(runtime, _PlayRecorder) else None
    if recorder is not None:
        recorder.delivered = trace.counters = []
    try:
        run = await factory.run(description.name)
    finally:
        if recorder is not None:
            recorder.delivered = None
    for child in run.children:
        # Every delivered child's own compile stages (3 when it forged).
        stages.update(getattr(getattr(child, "compile_result", None), "stages", ()))
    summary.update(
        revision=entry.revision,
        accepted=run.accepted,
        score=run.score,
        score_source=run.score_source,
        metric=run.metric,
        children=len(run.children),
        cost_usd=run.cost_usd,
        tokens_in=run.tokens_in,
        tokens_out=run.tokens_out,
        wall_time_s=round(run.wall_time_s, 3),
        run_failures=list(run.failures),
        child_failures=[list(child.failures) for child in run.children if child.failures],
        model_binding=run.model_binding,
    )
    failures = [*run.failures, *(f for child in run.children for f in child.failures)]
    return _budget_hint(summary, failures, reservation)


def _budget_hint(
    summary: dict[str, object], failures: list[str], reservation: float
) -> dict[str, object]:
    """Say plainly when the budget, not play, stopped the run."""
    if any("budget" in item for item in failures):
        summary["budget_note"] = (
            f"stopped by budget: each model call reserves ${reservation:.4f} before it is "
            "sent; raise --cost-usd/--compute-s or lower --games/--max-actions"
        )
    return summary
