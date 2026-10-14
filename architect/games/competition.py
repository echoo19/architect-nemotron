"""Installed planners that delegate real, freshly verified offline game agents.

Local scores describe synthetic fixtures, not ARC results or competition compliance.
Official offline previews require an explicit sealed manifest and trusted score scaling.
They do not establish official submission results or competition compliance.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from architect.compiler import CompileConfig, Compiler, CompileResult
from architect.floors import EvaluationCase, HeldOutGameScore
from architect.games.evaluation import (
    GameManifest,
    GameScorer,
    OfficialPreviewManifest,
    ReceiptLedger,
    game_floor,
    task_for,
    task_key,
    validate_discovery,
)
from architect.games.factory import (
    GameFactory,
    GameFactoryConfig,
    GameRunResult,
    ModelBinding,
    provider_manifest,
    resolve_model_binding,
    validate_offline_adapter,
    wallets_exhausted,
)
from architect.games.types import (
    GameLimits,
    GameProtocolError,
    GameProvider,
    GameRef,
    GameUnavailableError,
)
from architect.models import ModelAdapter
from architect.registry import DriftResult, Registry, RegistryEntry, RegistryError
from architect.runtime import BudgetExceeded, RunContext, RunResult
from architect.runtime.wallet import active_wallets, compilation_wallet
from architect.spec import Budget, FloorDefinition, FloorKind, HarnessArtifact, Spec
from architect.telemetry import TelemetryRow


class _Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


Identifier = Annotated[str, Field(min_length=1, max_length=200)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _preflight() -> None:
    if wallets_exhausted():
        raise BudgetExceeded("Competition budget exhausted")


# Child codes are fixed identifiers already; this shape check is defense in depth.
_FIXED_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_CHILD_CODE_LIMIT = 16


def child_failure_codes(children: Sequence[GameRunResult]) -> tuple[str, ...]:
    """``child_<code>`` for every attempted child's fixed failure codes.

    Deduplicated in first-seen order and capped, so a parent report that lost
    the child outcome to ``runtime_error`` still names the fixed child codes.
    """
    codes: list[str] = []
    for child in children:
        compile_failures = child.compile_result.failures if child.compile_result else ()
        for code in (*child.failures, *compile_failures):
            tagged = f"child_{code}"
            if _FIXED_CODE.fullmatch(code) is None or tagged in codes:
                continue
            codes.append(tagged)
            if len(codes) >= _CHILD_CODE_LIMIT:
                return tuple(codes)
    return tuple(codes)


def _with_child_failures(
    result: CompileResult, backend: _DelegationBackend | None
) -> CompileResult:
    """Attach attempted child codes to an unlicensed result; licensed ones are untouched."""
    if backend is None or result.approval_required:
        return result
    if result.artifact is not None and result.artifact.licensed:
        return result
    added = tuple(c for c in child_failure_codes(backend.attempted) if c not in result.failures)
    if not added:
        return result
    return result.model_copy(update={"failures": (*result.failures, *added)})


class CompetitionDescription(_Schema):
    name: Identifier
    rules: str = Field(min_length=1, max_length=16_384)
    action_schema: dict[str, JsonValue]
    observation_schema: dict[str, JsonValue]
    preview_dataset: Identifier
    child_support_dataset: Identifier
    compute_cap: Budget
    floor_limit: float = Field(default=1.0, ge=0, le=1, strict=True)
    small_model: Identifier
    submission_constraints: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=1024)], ...], Field(max_length=32)
    ] = ()
    rules_status: Literal["unverified", "owner_confirmed"] = "unverified"
    max_games: Annotated[int, Field(strict=True, ge=1, le=1024)]

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if self.preview_dataset == self.child_support_dataset:
            raise ValueError("competition_partition_overlap")
        if len(self.model_dump_json().encode()) > 65_536:
            raise ValueError("competition_description_size")
        return self

    @classmethod
    def read(cls, path: Path) -> Self:
        # Bound before parsing. Do not copy free-text rules to telemetry.
        with path.open("rb") as stream:
            payload = stream.read(65_537)
        if len(payload) > 65_536:
            raise ValueError("competition_description_size")

        def unique(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
            result: dict[str, JsonValue] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("competition_duplicate_field")
                result[key] = value
            return result

        return cls.model_validate(json.loads(payload, object_pairs_hook=unique))


class CompetitionPolicy(_Schema):
    kind: Literal["competition"] = "competition"
    version: Literal[1] = 1
    strategy: Literal["planner"] = "planner"
    order: Literal["canonical_complete"] = "canonical_complete"
    max_depth: Literal[1] = 1
    small_model: Identifier
    limits: GameLimits
    compute_cap: Budget
    floor_limit: float = Field(ge=0, le=1, strict=True)
    max_games: Annotated[int, Field(strict=True, ge=1, le=1024)]
    preview_dataset: Identifier
    child_support_dataset: Identifier
    description_digest: Digest
    parent_manifest_digest: Digest
    support_manifest_digest: Digest
    child_config_digest: Digest
    # Derived only from the trusted GameFactoryConfig opt-in, then rechecked.
    model_binding: ModelBinding = "offline"


class CompetitionRunResult(_Schema):
    accepted: bool = Field(strict=True)
    children: tuple[GameRunResult, ...] = Field(default=(), max_length=1024)
    play_result: RunResult | None = None
    tokens_in: int = Field(default=0, strict=True, ge=0)
    tokens_out: int = Field(default=0, strict=True, ge=0)
    cost_usd: float = Field(default=0, ge=0)
    wall_time_s: float = Field(default=0, ge=0)
    score: float | None = Field(default=None, ge=0, le=1)
    score_source: Literal["local_fixture", "official_offline_preview", "unavailable"] = (
        "unavailable"
    )
    metric: Literal["local_win_fraction", "normalized_preview_mean", "unavailable"] = "unavailable"
    compliance: Literal["unverified"] = "unverified"
    # "online_preview": a cloud game model was used; an operator test, never a submission.
    model_binding: ModelBinding = "offline"
    failures: tuple[str, ...] = ()


def _support_digest(manifest: GameManifest) -> str:
    return _digest(
        [
            (ref.model_dump(mode="json"), manifest.identity(ref))
            for role in ("support_tuning", "support_held_out")
            for ref in manifest.refs(role)
        ]
    )


class _DelegationBackend:
    def __init__(
        self, factory: CompetitionFactory, policy: CompetitionPolicy, ledger: ReceiptLedger
    ) -> None:
        self.factory = factory
        self.policy = policy
        self.ledger = ledger
        self.completed: dict[str, tuple[GameRunResult, ...]] = {}
        self.attempted: list[GameRunResult] = []
        self._runs: set[str] = set()

    async def run(
        self,
        context: RunContext,
        task: JsonValue,
        *,
        preset: str,
        params: dict[str, JsonValue],
        components: tuple[str, ...],
    ) -> JsonValue:
        context.check_game_grant()
        if (
            CompetitionPolicy.model_validate(context.execution_config) != self.policy
            or preset != "planner"
            or context.adapter.model != self.policy.small_model
            or task_key(task) != task_key(context.original_task)
            or context.run_id in self._runs
        ):
            raise GameProtocolError("competition_policy_mismatch")
        manifest = self.factory._validate_policy(self.policy)
        if context.is_evaluation:
            allowed = (*manifest.refs("parent_tuning"), *manifest.refs("parent_held_out"))
            targets = tuple(ref for ref in allowed if task_for(ref) == task)
            if len(targets) != 1:
                raise GameProtocolError("competition_preview_not_granted")
        else:
            targets = manifest.refs("delivery")
            if task != self.factory._delivery_task(targets):
                raise GameProtocolError("competition_delivery_incomplete")
        if not targets or len(targets) > self.policy.max_games:
            raise GameProtocolError("competition_coverage_limit")
        self._runs.add(context.run_id)
        children: list[GameRunResult] = []
        for target in targets:
            context.check_game_grant()
            # Recheck mutable host bindings and lease before every descendant side effect.
            self.factory._validate_policy(self.policy)
            child = self.factory._remaining_child()
            result = await child.run_isolated(
                target, support_dataset=self.policy.child_support_dataset
            )
            children.append(result)
            self.attempted.append(result)
            context.check_game_grant()
            if "game_budget_exceeded" in result.failures:
                # Budget refusal, not a candidate defect: parent tuning must see it as such.
                raise BudgetExceeded("Competition child budget exhausted")
            if not result.accepted or result.score is None or result.score.normalized is None:
                raise GameProtocolError("competition_child_failed")
        self.completed[context.run_id] = tuple(children)
        if context.is_evaluation:
            score = children[0].score
            assert score is not None
            return self.ledger.issue(context, task, session_id=uuid4().hex, score=score)
        return {
            "children_completed": len(children),
            "score_source": (
                "official_offline_preview"
                if isinstance(manifest, OfficialPreviewManifest)
                else "local_fixture"
            ),
        }


class CompetitionFactory:
    """Development compiler plus an installed, capability-scoped planner.

    ``registry`` is the actual durable Registry, including its strict replacement,
    drift monitor and rollback APIs. There is no parallel acceptance mechanism.
    """

    def __init__(
        self,
        compiler: Compiler,
        registry: Registry,
        game_factory: GameFactory,
        provider: GameProvider,
    ) -> None:
        if game_factory.provider is not provider:
            raise GameProtocolError("competition_provider_mismatch")
        self.compiler = compiler
        self.registry = registry
        self.game_factory = game_factory
        self.provider = provider

    def _policy(self, description: CompetitionDescription) -> CompetitionPolicy:
        config = self.game_factory.config
        if description.small_model != config.small_model:
            raise GameProtocolError("competition_model_mismatch")
        manifest = provider_manifest(self.provider, self.game_factory.manifest)
        if len(manifest.refs("delivery")) > description.max_games:
            raise GameProtocolError("competition_coverage_limit")
        return CompetitionPolicy(
            small_model=config.small_model,
            limits=config.limits,
            compute_cap=description.compute_cap,
            floor_limit=description.floor_limit,
            max_games=description.max_games,
            preview_dataset=description.preview_dataset,
            child_support_dataset=description.child_support_dataset,
            description_digest=_digest(description.model_dump(mode="json")),
            parent_manifest_digest=manifest.digest,
            support_manifest_digest=_support_digest(manifest),
            child_config_digest=_digest(config.model_dump(mode="json")),
            model_binding=resolve_model_binding(
                self.provider, manifest, allow_online=config.allow_online_model
            ),
        )

    def _validate_policy(self, policy: CompetitionPolicy) -> GameManifest:
        _preflight()
        manifest = provider_manifest(self.provider, self.game_factory.manifest)
        config = GameFactoryConfig.model_validate(self.game_factory.config.model_dump(mode="json"))
        if (
            self.game_factory.provider is not self.provider
            or config.small_model != policy.small_model
            or config.limits != policy.limits
            or _digest(config.model_dump(mode="json")) != policy.child_config_digest
            or manifest.digest != policy.parent_manifest_digest
            or _support_digest(manifest) != policy.support_manifest_digest
            or len(manifest.refs("delivery")) > policy.max_games
        ):
            raise GameProtocolError("competition_policy_mismatch")
        binding = resolve_model_binding(
            self.provider, manifest, allow_online=config.allow_online_model
        )
        if binding != policy.model_binding:
            raise GameProtocolError("competition_policy_mismatch")
        validate_offline_adapter(
            self.game_factory.compiler.adapter_factory(policy.small_model),
            policy.small_model,
            binding=binding,
        )
        return manifest

    def _validated_binding(self, policy: CompetitionPolicy) -> ModelBinding:
        """Recheck the host opt-in against the reviewed policy before adapter use."""
        self._validate_policy(policy)
        return policy.model_binding

    @staticmethod
    def _overrides(policy: CompetitionPolicy) -> dict[str, JsonValue]:
        return {
            "task": "Delegate complete game rollouts to freshly licensed offline agents",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "floor": FloorDefinition(
                kind=FloorKind.HELD_OUT_GAME_SCORE,
                limit=policy.floor_limit,
                dataset=policy.preview_dataset,
            ).model_dump(mode="json"),
            "secondary_floor": None,
            "budget": policy.compute_cap.model_dump(mode="json"),
            "model_target": policy.small_model,
            "lifecycle": "install",
            "domain": "game",
            "preset_hint": "planner",
            "tools": ["game.spawn"],
            "pipeline": [],
            "execution_config": policy.model_dump(mode="json"),
        }

    def _reviewed_policy(self, spec: Spec) -> CompetitionPolicy:
        policy = CompetitionPolicy.model_validate(spec.execution_config)
        self._validate_policy(policy)
        payload = spec.model_dump(mode="json")
        if any(payload[key] != value for key, value in self._overrides(policy).items()):
            raise GameProtocolError("competition_review_mismatch")
        return policy

    def _compiler(self, policy: CompetitionPolicy, ledger: ReceiptLedger) -> Compiler:
        manifest = self._validate_policy(policy)
        definition = FloorDefinition(
            kind=FloorKind.HELD_OUT_GAME_SCORE,
            limit=policy.floor_limit,
            dataset=policy.preview_dataset,
        )
        floor = game_floor(definition, manifest, ledger, parent=True)
        # Fixed regression replay drawn ONLY from parent tuning. No primary
        # held-out record enters the smoke constructor. The normal even/odd gate
        # selects a deterministic replay subset; this is not new generalization
        # evidence. Receipt authority is separate for every fresh measurement.
        smoke_records = tuple(
            EvaluationCase(case_id=f"parent-smoke-{index:04d}", task=case.task, expected=None)
            for index, case in enumerate(floor.tuning)
        )
        smoke: HeldOutGameScore | None = None

        def smoke_floors(requested: FloorDefinition) -> HeldOutGameScore:
            nonlocal smoke
            if requested != definition:
                raise GameProtocolError("competition_floor_not_allowed")
            if smoke is None:
                # One fixed evaluator, created only if offline rewriting needs it.
                # Too few public tuning records fail closed, never borrow held-out.
                smoke = HeldOutGameScore(definition, smoke_records, GameScorer(ledger))
            return smoke

        def floors(requested: FloorDefinition) -> HeldOutGameScore:
            if requested != definition:
                raise GameProtocolError("competition_floor_not_allowed")
            return floor

        def adapters(model: str) -> ModelAdapter:
            if model != policy.small_model:
                raise GameProtocolError("competition_model_not_allowed")
            adapter = self.game_factory.compiler.adapter_factory(model)
            validate_offline_adapter(adapter, model, binding=self._validated_binding(policy))
            return adapter

        config = self.compiler.config.model_dump(mode="json")
        config.update(model_ladder=[policy.small_model], allowed_presets=["planner"])
        return Compiler(
            self.compiler.artifact_root,
            self.compiler.runtime,
            adapters,
            floors,
            config=CompileConfig.model_validate(config),
            library=self.compiler.library,
            inference_adapter=self.compiler.inference_adapter,
            rewrite_adapter=self.compiler.rewrite_adapter,
            smoke_factory=smoke_floors if self.compiler.smoke_factory is not None else None,
        )

    async def compile(
        self,
        description: CompetitionDescription,
        *,
        approved: bool = False,
        approved_spec: Spec | None = None,
    ) -> CompileResult:
        description = CompetitionDescription.model_validate(description.model_dump(mode="json"))
        reviewed: Spec | None = None
        backend: _DelegationBackend | None = None
        ledger = ReceiptLedger()
        # Setup, Stage 0 and all nested rollouts share this operating wallet/deadline.
        with compilation_wallet(description.compute_cap):
            try:
                async with asyncio.timeout(min(w.remaining_s for w in active_wallets())):
                    policy = self._policy(description)
                    compiler = self._compiler(policy, ledger)
                    if approved_spec is None:
                        # A boolean never approves a specification not yet seen by the operator.
                        proposal = await compiler.compile(
                            description.model_dump_json(),
                            overrides=self._overrides(policy),
                            approved=False,
                            exact_overrides=True,
                        )
                        # Defense in depth: freeze the exact host contract before
                        # showing it for review, never after approval.
                        payload = proposal.spec.model_dump(mode="json")
                        payload.update(self._overrides(policy))
                        return proposal.model_copy(update={"spec": Spec.model_validate(payload)})
                    reviewed = Spec.model_validate(approved_spec.model_dump(mode="json"))
                    if self._reviewed_policy(reviewed) != policy:
                        raise GameProtocolError("competition_review_mismatch")
                    if not approved:
                        return CompileResult(spec=reviewed, approval_required=True)
                    backend = _DelegationBackend(self, policy, ledger)
                    async with compiler.runtime.use_game_backend(backend):
                        result = await compiler.compile(reviewed, approved=True)
                    return _with_child_failures(result, backend)
            except (BudgetExceeded, TimeoutError):
                # The shared deadline (setup included) or wallet refused before the
                # inner compiler could fold it: report budget, never a bare timeout.
                if reviewed is None:
                    raise BudgetExceeded("Competition compile budget exhausted") from None
                refused = CompileResult(spec=reviewed, failures=("compile_budget",))
                return _with_child_failures(refused, backend)
            finally:
                ledger.close()

    async def install(self, result: CompileResult, *, name: str) -> RegistryEntry:
        if result.approval_required or result.artifact is None or result.failures:
            raise RegistryError("competition_unlicensed")
        self._reviewed_policy(result.spec)
        if result.spec != result.artifact.spec or result.artifact.preset != "planner":
            raise RegistryError("competition_artifact_mismatch")
        return self.registry.install(name, result.artifact)

    @staticmethod
    def _delivery_task(targets: tuple[GameRef, ...]) -> dict[str, JsonValue]:
        return {"games": [ref.model_dump(mode="json") for ref in targets]}

    def _remaining_child(self) -> GameFactory:
        config = self.game_factory.config
        wallets = active_wallets()
        if not wallets:
            raise GameProtocolError("competition_wallet_missing")
        dollars = min(config.budget.cost_usd, *(w.budget.cost_usd - w.cost_usd for w in wallets))
        seconds = min(config.budget.compute_s, *(w.remaining_s for w in wallets))
        _preflight()
        if dollars < 0 or seconds <= 0:
            raise BudgetExceeded("Competition budget exhausted")
        if dollars == 0:
            # Budget requires a positive local ceiling. Keep the configured child
            # ceiling, but retain the zero-remaining ancestor wallet: it permits
            # only zero-dollar reservations and never grants new spending power.
            dollars = config.budget.cost_usd
        payload = config.model_dump(mode="json")
        payload["budget"] = Budget(cost_usd=dollars, compute_s=seconds).model_dump(mode="json")
        compile_config = config.compile_config.model_dump(mode="json")
        compile_config["compile_budget"] = Budget(
            cost_usd=min(dollars, config.compile_config.compile_budget.cost_usd),
            compute_s=min(seconds, config.compile_config.compile_budget.compute_s),
        ).model_dump(mode="json")
        payload["compile_config"] = compile_config
        return GameFactory(
            self.game_factory.compiler,
            self.provider,
            GameFactoryConfig.model_validate(payload),
            manifest=self.game_factory.manifest,
            forge_agents=self.game_factory.forge_agents,
        )

    async def run(self, name: str) -> CompetitionRunResult:
        started = time.monotonic()
        artifact = self.registry.get(name)
        policy = CompetitionPolicy.model_validate(artifact.spec.execution_config)
        ledger = ReceiptLedger()
        backend = _DelegationBackend(self, policy, ledger)
        play: RunResult | None = None
        children: tuple[GameRunResult, ...] = ()
        failures: tuple[str, ...] = ()
        with compilation_wallet(artifact.spec.budget) as wallet:
            wallet.started = started  # Include registry lookup/setup in the same deadline.
            try:
                async with asyncio.timeout(min(w.remaining_s for w in active_wallets())):
                    manifest = self._validate_policy(policy)
                    self._reviewed_policy(artifact.spec)
                    # Every pinned target must exist. Unrelated public catalog
                    # entries are not additional operator-required delivery games.
                    _preflight()
                    discovered = await self.provider.discover()
                    _preflight()
                    validate_discovery(manifest, discovered)
                    adapter = self.game_factory.compiler.adapter_factory(policy.small_model)
                    validate_offline_adapter(
                        adapter, policy.small_model, binding=self._validated_binding(policy)
                    )
                    async with self.compiler.runtime.use_game_backend(backend):
                        play = await self.compiler.runtime.run(
                            artifact, self._delivery_task(manifest.refs("delivery")), adapter
                        )
                    children = backend.completed.get(play.run_id, ())
                    if len(children) != len(manifest.refs("delivery")):
                        raise GameProtocolError("competition_delivery_incomplete")
            except (BudgetExceeded, TimeoutError):
                failures = ("competition_budget_exceeded",)
            except GameUnavailableError:
                failures = ("competition_unavailable",)
            except Exception:
                failures = (
                    "competition_budget_exceeded"
                    if wallets_exhausted()
                    else "competition_execution_failed",
                )
            finally:
                ledger.close()
            if failures:
                children = tuple(backend.attempted)
            scores = [c.score.normalized for c in children if c.score is not None]
            complete = not failures and play is not None and bool(children)
            score = sum(s for s in scores if s is not None) / len(children) if complete else None
            return CompetitionRunResult(
                accepted=complete,
                children=children,
                play_result=play,
                tokens_in=wallet.tokens_in,
                tokens_out=wallet.tokens_out,
                cost_usd=wallet.cost_usd,
                wall_time_s=time.monotonic() - started,
                score=score,
                score_source=(
                    (
                        "official_offline_preview"
                        if isinstance(manifest, OfficialPreviewManifest)
                        else "local_fixture"
                    )
                    if complete
                    else "unavailable"
                ),
                metric=(
                    (
                        "normalized_preview_mean"
                        if isinstance(manifest, OfficialPreviewManifest)
                        else "local_win_fraction"
                    )
                    if complete
                    else "unavailable"
                ),
                model_binding=(
                    "online_preview"
                    if policy.model_binding == "online_preview"
                    or self.game_factory.config.allow_online_model
                    else "offline"
                ),
                failures=failures,
            )

    async def _recompile_spec(self, spec: Spec) -> HarnessArtifact:
        policy = self._reviewed_policy(spec)
        ledger = ReceiptLedger()
        backend: _DelegationBackend | None = None
        try:
            with compilation_wallet(spec.budget):
                try:
                    async with asyncio.timeout(min(w.remaining_s for w in active_wallets())):
                        compiler = self._compiler(policy, ledger)
                        backend = _DelegationBackend(self, policy, ledger)
                        async with compiler.runtime.use_game_backend(backend):
                            result = await compiler.compile(spec, approved=True)
                except (BudgetExceeded, TimeoutError):
                    # Same RegistryError contract as any other unlicensed recompile.
                    raise RegistryError("competition_recompile_budget") from None
        finally:
            ledger.close()
        result = _with_child_failures(result, backend)
        if result.artifact is None or not result.artifact.licensed:
            raise RegistryError("competition_recompile_failed")
        return result.artifact

    async def recompile(self, name: str) -> RegistryEntry:
        artifact = await self._recompile_spec(self.registry.get(name).spec)
        return self.registry.install(name, artifact)

    async def monitor(self, name: str, rows: Sequence[TelemetryRow]) -> DriftResult:
        """Use Registry's real drift trigger and strictly-better acceptance rule."""
        return await self.registry.monitor(name, rows, self._recompile_spec)

    def rollback(self, name: str, revision: int | None = None) -> RegistryEntry:
        return self.registry.rollback(name, revision)
