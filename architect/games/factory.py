"""Runtime-only game compilation with sealed support floors and actual rollouts."""

from __future__ import annotations

import asyncio
import ipaddress
import time
from collections.abc import Callable
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from architect.compiler import CompileConfig, Compiler, CompileResult
from architect.compiler.inference import InferenceError, probe_context_chars
from architect.compiler.types import LibraryLifecycle, LibraryLookup, NullLibrary
from architect.floors import HeldOutGameScore
from architect.games.evaluation import (
    GameManifest,
    GameScorer,
    OfficialPreviewManifest,
    ReceiptLedger,
    game_floor,
    task_for,
    validate_discovery,
)
from architect.games.execution import (
    GAME_PRESETS,
    GameBackend,
    GameExecutionPolicy,
    GameStrategy,
    cleanup_game_resources,
)
from architect.games.local import LocalFixtureManifest
from architect.games.official import FORWARDED_WORKER_ERRORS
from architect.games.types import (
    GameActionError,
    GameLimits,
    GameProtocolError,
    GameProvider,
    GameRef,
    GameScore,
    GameSession,
    GameSessionError,
    GameUnavailableError,
    ProbeEnvironment,
)
from architect.models import FakeAdapter, ModelAdapter, OllamaAdapter, OpenAICompatibleAdapter
from architect.runtime import BudgetExceeded, RunResult
from architect.spec import Budget, FloorDefinition, FloorKind, HarnessArtifact, Spec

# "online_preview" marks runs whose game agent used a non-loopback (cloud) model.
# Such runs are operator tests only: never Kaggle-rule evidence or submissions.
ModelBinding = Literal["offline", "online_preview"]

# Fixed GameBackend/official session codes that may accompany the coarse play
# failure. Only a member of this set is ever copied; never exception text.
PLAY_FAILURE_CODES = frozenset(
    {
        "game_decision_invalid",
        "game_decision_size",
        "game_action_unavailable",
        "game_action_limit",
        "game_prompt_size",
        "game_score_unavailable",
        "game_score_source_mismatch",
        "game_score_scale_mismatch",
        "official_operation_failed",
        "official_terminal_state",
        "official_action_limit",
        "official_timeout",
        "official_deadline",
        *FORWARDED_WORKER_ERRORS,
    }
)


def play_failure_code(exc: BaseException) -> str | None:
    """Return the fixed code a game exception carries, or None for anything else."""
    if not isinstance(
        exc, (GameProtocolError, GameActionError, GameSessionError, GameUnavailableError)
    ):
        return None
    code = exc.args[0] if len(exc.args) == 1 else None
    return code if isinstance(code, str) and code in PLAY_FAILURE_CODES else None


class GameFactoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    budget: Budget
    compile_config: CompileConfig
    small_model: str = Field(min_length=1, max_length=200)
    limits: GameLimits = Field(default_factory=GameLimits)
    strategy: GameStrategy = "explorer"
    floor_limit: float = Field(default=1, ge=0, le=1)
    # Trusted host-side opt-in for a cloud model on OFFLINE previews only. It is
    # never read from a Spec or model proposal; policies freeze its effect.
    allow_online_model: bool = Field(default=False, strict=True)
    # Trusted host-side opt-in (bench forge/template cells only): lifts the
    # delivery refusal for a licensed Forge sandbox artifact, and nothing else.
    allow_forge: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def runtime_policy(self) -> Self:
        if self.compile_config.enable_rewriting or self.compile_config.max_rewrites != 0:
            raise ValueError("game_runtime_rewriting_forbidden")
        if self.compile_config.model_ladder != (self.small_model,):
            raise ValueError("game_runtime_model_policy")
        allowed = self.compile_config.allowed_presets
        if allowed and (set(allowed) - set(GAME_PRESETS) or self.strategy not in allowed):
            raise ValueError("game_runtime_preset_policy")
        return self


class GameRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    accepted: bool = Field(strict=True)
    compile_result: CompileResult | None = None
    play_result: RunResult | None = None
    actions_used: int = Field(default=0, ge=0, strict=True)
    tokens_in: int = Field(default=0, ge=0, strict=True)
    tokens_out: int = Field(default=0, ge=0, strict=True)
    cost_usd: float = Field(default=0, ge=0)
    wall_time_s: float = Field(default=0, ge=0)
    failures: tuple[str, ...] = ()
    score: GameScore | None = None
    model_binding: ModelBinding = "offline"
    compliance: Literal["unverified"] = "unverified"


class GameFactory:
    """License real rollouts using local fixtures or pinned official OFFLINE previews.

    Official runs require an explicit public manifest and staged public files.
    Gateway submission sessions are never candidate-tuning resources. Local
    support scores cannot be presented as official evidence.
    """

    def __init__(
        self,
        compiler: Compiler,
        provider: GameProvider,
        config: GameFactoryConfig,
        *,
        manifest: GameManifest | None = None,
        forge_agents: Callable[[], ModelAdapter | None] | None = None,
    ) -> None:
        # Validate copies, including model_copy(update=...) inputs that bypass validators.
        self.config = GameFactoryConfig.model_validate(config.model_dump(mode="json"))
        self.compiler = compiler
        self.provider = provider
        self.manifest = manifest
        # Trusted host plumbing: a fresh Forge agent per game compile (one Pi session
        # each). None falls back to the parent compiler's ``forge_adapter``.
        self.forge_agents = forge_agents

    def _forging(self) -> bool:
        """Forge runs on a game compile only on the trusted host opt-in with Forge enabled."""
        return self.config.allow_forge is True and self.config.compile_config.forge.enabled

    def _forge_agent(self) -> ModelAdapter | None:
        """The Forge agent of one game compile: a fresh one when the host supplies them."""
        if self.forge_agents is not None:
            return self.forge_agents()
        return self.compiler.forge_adapter

    def _wallet_budget(self, forging: bool) -> Budget:
        """The factory wallet; raised by the Forge caps exactly as ``Compiler`` raises
        its outer wallet (Section 8.2), so the session is not starved by the play
        budget. The child Spec's own budget is unchanged."""
        budget = self.config.budget
        if not forging:
            return budget
        forge = self.config.compile_config.forge
        return Budget(
            cost_usd=budget.cost_usd + forge.max_usd, compute_s=budget.compute_s + forge.max_s
        )

    async def run(self, target: GameRef, *, support_dataset: str) -> GameRunResult:
        return await self._run(target, support_dataset=support_dataset, isolated=False)

    async def run_isolated(self, target: GameRef, *, support_dataset: str) -> GameRunResult:
        """Compile solely on public support, then bind an unseen target for play.

        Parent evaluators use this path for BOTH tuning and held-out rollouts.
        No target ID, frame or outcome enters child inference, tuning, or a library.
        """
        return await self._run(target, support_dataset=support_dataset, isolated=True)

    async def _run(self, target: GameRef, *, support_dataset: str, isolated: bool) -> GameRunResult:
        from architect.runtime.wallet import active_wallets, compilation_wallet

        def remaining() -> float:
            if wallets_exhausted():
                raise BudgetExceeded("game_parent_budget_exhausted")
            return min(item.remaining_s for item in active_wallets())

        started = time.monotonic()
        result: CompileResult | None = None
        play: RunResult | None = None
        score: GameScore | None = None
        failures: tuple[str, ...] = ()
        backend: GameBackend | None = None
        probe_session: GameSession | None = None
        probe_actions = 0
        play_failure: str | None = None
        ledger = ReceiptLedger()
        forging = self._forging()
        with compilation_wallet(self._wallet_budget(forging)) as wallet:
            try:
                async with asyncio.timeout(remaining()):
                    config = GameFactoryConfig.model_validate(self.config.model_dump(mode="json"))
                    target = GameRef.model_validate(target.model_dump(mode="json"))
                    manifest = provider_manifest(self.provider, self.manifest)
                    identity = manifest.identity(target)
                    support = (*manifest.refs("support_tuning"), *manifest.refs("support_held_out"))
                    if identity in {manifest.identity(ref) for ref in support}:
                        raise GameProtocolError("game_target_support_overlap")
                    if not isolated and target not in manifest.refs("delivery"):
                        raise GameProtocolError("game_reserved_probe_target")
                    remaining()
                    discovered = await self.provider.discover()
                    remaining()
                    validate_discovery(manifest, discovered)
                    official = isinstance(manifest, OfficialPreviewManifest)
                    binding = resolve_model_binding(
                        self.provider, manifest, allow_online=config.allow_online_model
                    )
                    policy = GameExecutionPolicy(
                        small_model=config.small_model,
                        strategy=config.strategy,
                        limits=config.limits,
                        manifest_digest=manifest.digest,
                        support_dataset=support_dataset,
                        score_source="official" if official else "local",
                        score_policy="official_percent_v1" if official else "local_binary_v1",
                        score_metric=(
                            "relative_human_action_efficiency.percent.v1"
                            if official
                            else "local_win_v1"
                        ),
                        model_binding=binding,
                    )
                    remaining()  # Recheck ancestors before any adapter setup.
                    adapter = self.compiler.adapter_factory(config.small_model)
                    validate_offline_adapter(adapter, config.small_model, binding=binding)

                    def adapters(model: str) -> ModelAdapter:
                        if model != config.small_model:
                            raise GameProtocolError("game_model_not_allowed")
                        validate_offline_adapter(adapter, config.small_model, binding=binding)
                        return adapter

                    definition = FloorDefinition(
                        kind=FloorKind.HELD_OUT_GAME_SCORE,
                        limit=config.floor_limit,
                        dataset=support_dataset,
                    )
                    floor = game_floor(definition, manifest, ledger)

                    def floors(requested: FloorDefinition) -> HeldOutGameScore:
                        if requested != definition:
                            raise GameProtocolError("game_floor_not_allowed")
                        return floor

                    def smokes(requested: FloorDefinition) -> HeldOutGameScore:
                        if requested != definition:
                            raise GameProtocolError("game_floor_not_allowed")
                        return support_smoke_floor(definition, floor, ledger)

                    compile_payload = config.compile_config.model_dump(mode="json")
                    compile_payload["allowed_presets"] = list(
                        config.compile_config.allowed_presets or GAME_PRESETS
                    )
                    compiler = Compiler(
                        self.compiler.artifact_root,
                        self.compiler.runtime,
                        adapters,
                        floors,
                        config=CompileConfig.model_validate(compile_payload),
                        inference_adapter=adapter,
                        rewrite_adapter=None,
                        library=NullLibrary() if isolated else _SafeLibrary(self.compiler.library),
                        # Forge (Stage 3) only on the host opt-in with Forge enabled: the
                        # parent's agent and effective Pi config, and a smoke floor built
                        # solely from public support-tuning games.
                        smoke_factory=smokes if forging else None,
                        forge_adapter=self._forge_agent() if forging else None,
                        pi_config=self.compiler.pi_config if forging else None,
                    )
                    if forging:
                        compiler.forge_listener = self.compiler.forge_listener
                    overrides: dict[str, JsonValue] = {
                        "task": "Play a grid game from public observations",
                        "input_schema": {
                            "type": "object",
                            "required": ["game"],
                            "properties": {"game": GameRef.model_json_schema()},
                            "additionalProperties": False,
                        },
                        "output_schema": {"type": "object"},
                        "floor": definition.model_dump(mode="json"),
                        "secondary_floor": None,
                        "budget": config.budget.model_dump(mode="json"),
                        "model_target": config.small_model,
                        "lifecycle": "expire",
                        "domain": "game",
                        "tools": ["game.observe", "game.step"],
                        "pipeline": [],
                        "preset_hint": config.strategy,
                        "execution_config": policy.model_dump(mode="json"),
                    }
                    # The only probe source during nested evaluation is disjoint public support.
                    probe_ref = manifest.refs("support_tuning")[0] if isolated else target
                    remaining()  # Setup/probes share the ancestor deadline, not only model calls.
                    probe_session = await self.provider.open(probe_ref, limits=config.limits)
                    remaining()
                    # Sized to the inference request, so an animated or dense public
                    # game cannot fail Stage 0 with an oversized probe.
                    environment = ProbeEnvironment(
                        probe_session, config.limits, max_chars=probe_context_chars(overrides)
                    )
                    backend = GameBackend(
                        self.provider,
                        policy,
                        ledger,
                        evaluation_refs=support,
                        target=target,
                        target_session=None if isolated else probe_session,
                    )
                    async with compiler.runtime.use_game_backend(backend):
                        result = await compiler.compile(
                            environment, exact_overrides=True, overrides=overrides
                        )
                        if isolated:
                            probe_actions = probe_session.actions_used
                            await probe_session.close()
                            probe_session = None
                        if result.artifact is None or not result.artifact.licensed:
                            # compile() folds per-case BudgetExceeded and its own
                            # deadline into failures; an exhausted wallet is budget.
                            failures = (
                                "game_budget_exceeded"
                                if wallets_exhausted() or "compile_budget" in result.failures
                                else "game_compile_failed",
                            )
                        elif (
                            refusal := delivery_refusal(
                                result.artifact, allow_forge=config.allow_forge
                            )
                        ) is not None:
                            failures = (refusal,)
                        else:
                            # Target-only delivery consumes ONE ephemeral artifact for all turns.
                            try:
                                play = await compiler.runtime.run(
                                    result.artifact, task_for(target), adapters(config.small_model)
                                )
                            except Exception as exc:
                                play_failure = play_failure_code(exc)
                                raise
                            score = backend.completed.get(play.run_id)
                            if score is None:
                                failures = ("game_execution_incomplete",)
            except (TimeoutError, BudgetExceeded):
                failures = ("game_budget_exceeded",)
            except GameUnavailableError:
                failures = ("game_unavailable",)
            except InferenceError:
                # Stage 0 could not produce the child Spec: a compile failure with
                # its fixed cause, never the model's or the probe's text.
                failures = (
                    ("game_budget_exceeded",)
                    if wallets_exhausted()
                    else ("game_compile_failed", "game_inference_failed")
                )
            except Exception:
                # Fixed codes only: no frames, task IDs, model responses or credentials.
                failures = (
                    "game_budget_exceeded" if wallets_exhausted() else "game_execution_failed",
                )
            finally:
                if probe_session is not None:
                    # Delivery backend already includes probe actions in its final session total.
                    if isolated or backend is None or backend.target_session is not None:
                        probe_actions = probe_session.actions_used

                async def cleanup() -> None:
                    backend_owns_probe = (
                        backend is not None and backend.target_session is probe_session
                    )
                    try:
                        if backend is not None:
                            await backend.close()
                    finally:
                        if probe_session is not None and not backend_owns_probe:
                            await probe_session.close()

                try:
                    await cleanup_game_resources(cleanup())
                except Exception:
                    failures = failures or ("game_cleanup_failed",)
                finally:
                    ledger.close()
            if play_failure and failures and failures[0] != "game_budget_exceeded":
                # Keep the coarse code first; add the fixed play code the parent can show.
                failures = (*failures, play_failure)
            return GameRunResult(
                accepted=not failures and play is not None and score is not None,
                compile_result=result,
                play_result=play,
                score=score,
                actions_used=probe_actions + (backend.actions_used if backend else 0),
                tokens_in=wallet.tokens_in,
                tokens_out=wallet.tokens_out,
                cost_usd=wallet.cost_usd,
                wall_time_s=time.monotonic() - started,
                failures=failures,
                # Label from the host opt-in itself, so even refused runs say so.
                model_binding="online_preview" if self.config.allow_online_model else "offline",
            )


def support_smoke_floor(
    definition: FloorDefinition, floor: HeldOutGameScore, ledger: ReceiptLedger
) -> HeldOutGameScore:
    """Forge's smoke floor for a game compile, built ONLY from support-tuning games.

    ``floor.tuning`` is the public support-tuning partition (the only partition
    Forge may see, Section 10.4); support held-out and delivery games can never
    reach it. The support-tuning cases are re-split even/odd as the bench's text
    cells do. A single support-tuning game is paired with a relabelled copy of
    itself so the split still has both halves, again with no other game added.
    Scoring shares the compile's receipt ledger.
    """
    tuning = floor.tuning
    records = (
        tuning
        if len(tuning) >= 2
        else tuple(
            case.model_copy(update={"case_id": f"smoke-{2 * index + offset:04d}"})
            for index, case in enumerate(tuning)
            for offset in (0, 1)
        )
    )
    return HeldOutGameScore(definition, records, environment=GameScorer(ledger))


def delivery_refusal(artifact: HarnessArtifact, *, allow_forge: bool) -> str | None:
    """The fixed code refusing delivery of a licensed artifact, or None to deliver.

    Native artifacts deliver. A sandbox artifact is refused with
    ``game_sandbox_forbidden`` unless the host opted in with ``allow_forge`` and the
    artifact is a Forge artifact (sandbox language v2 with a ``forge`` binding).
    """
    if artifact.execution_mode == "native":
        return None
    if allow_forge is True and artifact.execution_mode == "sandbox" and artifact.forge is not None:
        return None
    return "game_sandbox_forbidden"


def wallets_exhausted() -> bool:
    """True when any active wallet refuses further work.

    Overspent dollars count; an exactly spent dollar limit does not, so zero-cost
    work may continue while time remains (priced reservations still fail).
    """
    from architect.runtime.wallet import active_wallets

    return any(
        item.exhausted or item.remaining_s <= 0 or item.cost_usd > item.budget.cost_usd
        for item in active_wallets()
    )


def provider_manifest(provider: GameProvider, manifest: GameManifest | None = None) -> GameManifest:
    """Use a host-pinned manifest; no model input can select the floor partition."""
    supplied = manifest if manifest is not None else getattr(provider, "manifest", None)
    if isinstance(supplied, OfficialPreviewManifest):
        # Do not even discover/open a submission scorecard for candidate trials.
        if getattr(getattr(provider, "config", None), "mode", None) == "gateway":
            raise GameUnavailableError("official_gateway_evaluation_forbidden")
        return OfficialPreviewManifest.model_validate(supplied.model_dump(mode="json"))
    if isinstance(supplied, LocalFixtureManifest):
        return LocalFixtureManifest.model_validate(supplied.model_dump(mode="json"))
    raise GameUnavailableError("game_manifest_unavailable")


def resolve_model_binding(
    provider: GameProvider, manifest: GameManifest, *, allow_online: bool
) -> ModelBinding:
    """Resolve the host opt-in; a cloud model never touches a submission session."""
    if not allow_online:
        return "offline"
    mode = getattr(getattr(provider, "config", None), "mode", None)
    if mode == "gateway":
        raise GameProtocolError("game_online_model_gateway_forbidden")
    if isinstance(manifest, OfficialPreviewManifest) and mode == "offline":
        return "online_preview"
    if isinstance(manifest, LocalFixtureManifest) and mode is None:
        return "online_preview"
    raise GameProtocolError("game_online_model_preview_required")


def validate_offline_adapter(
    adapter: ModelAdapter, model: str, *, binding: ModelBinding = "offline"
) -> None:
    """Validate one trusted operator binding; this is not proof of OS isolation.

    ``binding="online_preview"`` comes only from :func:`resolve_model_binding`
    and waives the loopback requirement, never the model-name binding.
    """
    if adapter.model != model:
        raise GameProtocolError("game_model_binding")
    if binding == "online_preview":
        return
    if isinstance(adapter, FakeAdapter):
        return
    if isinstance(adapter, (OllamaAdapter, OpenAICompatibleAdapter)):
        url = urlsplit(adapter._base_url)
        if (
            url.scheme not in {"http", "https"}
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise GameProtocolError("game_model_offline_required")
        host = url.hostname
        if host == "localhost":
            return
        try:
            if host is not None and ipaddress.ip_address(host).is_loopback:
                return
        except ValueError:
            pass
    raise GameProtocolError("game_model_offline_required")


class _SafeLibrary:
    def __init__(self, source: LibraryLookup) -> None:
        self.source = source

    def lookup(self, spec: Spec, model: str) -> HarnessArtifact | None:
        artifact = self.source.lookup(spec, model)
        if artifact is not None and (
            artifact.execution_mode != "native" or (artifact.directory / "rewrite.diff").exists()
        ):
            raise GameProtocolError("game_sandbox_warm_start")
        return artifact

    def store(self, artifact: HarnessArtifact) -> str:
        if isinstance(self.source, LibraryLifecycle):
            return self.source.store(artifact)
        return ""

    def record_verification(self, source: HarnessArtifact, passed: bool) -> None:
        if isinstance(self.source, LibraryLifecycle):
            self.source.record_verification(source, passed)
