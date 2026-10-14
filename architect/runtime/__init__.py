"""Budget-guarded execution and content-free run accounting.

``evaluate`` is an internal measurement path, not client delivery authority.
Python harness code is trusted; model output never grants an execution license.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import json
import math
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from architect.models import AdapterError, ModelAdapter, ModelRequest, ModelResponse, UsageQuote
from architect.models.chat import (
    ChatEvent,
    ChatMessage,
    ChatResult,
    ChatStreaming,
    DoneEvent,
    StopReason,
    TextDeltaEvent,
    ToolCallEvent,
    ToolSpec,
    UsageEvent,
)
from architect.models.images import ImageInput, ImageRef, ImageStub
from architect.models.pricing import Price
from architect.spec import Budget, HarnessArtifact, Lifecycle, Spec
from architect.telemetry import TelemetryRow, TelemetryStore, categorical
from architect.telemetry.schema import ParameterKey, ParameterValue

from .wallet import CompileWallet, active_wallets, compilation_wallet

if TYPE_CHECKING:
    from architect.games.execution import _Play

__all__ = [
    "ArtifactExpiredError",
    "BudgetExceeded",
    "Harness",
    "GameBackendProtocol",
    "RunContext",
    "RunResult",
    "Runtime",
    "UnlicensedArtifactError",
    "load_harness",
]

_T = TypeVar("_T")
# Provider completion ids a run keeps (``RunContext.completion_ids``, ``RunResult``).
COMPLETION_IDS_KEPT = 64
_LAST_ROW: ContextVar[tuple[int, TelemetryRow] | None] = ContextVar("last_run_row", default=None)
_BACKEND: ContextVar[_BackendScope | None] = ContextVar("pi_backend", default=None)
_GAME: ContextVar[_GameScope | None] = ContextVar("game_backend", default=None)
_LAST_USAGE: ContextVar[tuple[int, TelemetryRow] | None] = ContextVar("run_usage", default=None)
_DRY_TURNS: ContextVar[_DryTurns | None] = ContextVar("dry_turns", default=None)


@dataclass(eq=False)
class _DryTurns:
    """An unscored per-turn play for ``forge_dryrun``: at most ``max_steps`` steps.

    ``preset`` is the artifact's game preset; the run's telemetry label is
    ``forge_dryrun``, which is not a game strategy.
    """

    owner: int
    preset: str
    max_steps: int
    stopped: bool = False


class _TurnsExhausted(Exception):
    """A dry run reached its step budget; the host then finishes the play unscored."""


class BudgetExceeded(Exception):
    """The run cannot continue within its hard resource limits."""

    def __init__(
        self,
        message: str,
        *,
        required_tokens: int | None = None,
        required_cost_usd: float | None = None,
    ) -> None:
        super().__init__(message)
        self.required_tokens = required_tokens
        self.required_cost_usd = required_cost_usd


class UnlicensedArtifactError(Exception):
    """The artifact lacks a valid, bound execution license."""


class ArtifactExpiredError(Exception):
    """An ephemeral artifact has already consumed its execution attempt."""


class PiBackendProtocol(Protocol):
    async def run(
        self,
        context: RunContext,
        task: JsonValue,
        pack: dict[str, JsonValue],
    ) -> str: ...


@dataclass
class _BackendScope:
    owner: int
    backend: PiBackendProtocol
    active: bool = True

    async def run(self, context: RunContext, task: JsonValue, pack: dict[str, JsonValue]) -> str:
        if not self.active:
            raise BudgetExceeded("Backend capability expired")
        return await self.backend.run(context, task, pack)


class GameBackendProtocol(Protocol):
    async def run(
        self,
        context: RunContext,
        task: JsonValue,
        *,
        preset: str,
        params: dict[str, JsonValue],
        components: tuple[str, ...],
    ) -> JsonValue: ...


@dataclass
class _GameScope:
    owner: int
    backend: GameBackendProtocol
    active: bool = True
    pending: set[asyncio.Task[object]] = field(default_factory=set)

    async def run(
        self,
        context: RunContext,
        task: JsonValue,
        *,
        preset: str,
        params: dict[str, JsonValue],
        components: tuple[str, ...],
    ) -> JsonValue:
        if not self.active or context._game_scope is not self:
            raise BudgetExceeded("Game capability unavailable or expired")
        context.check_game_grant()
        current = asyncio.current_task()
        assert current is not None
        self.pending.add(current)
        try:
            result = await self.backend.run(
                context,
                task,
                preset=preset,
                params=params,
                components=components,
            )
            context.check_game_grant()
            return result
        finally:
            self.pending.discard(current)


class RunContext:
    """Own resource accounting for one run and serialize model reservations."""

    def __init__(
        self,
        adapter: ModelAdapter,
        budget: Budget,
        *,
        adapter_factory: Callable[[str], ModelAdapter] | None = None,
        pi_backend: PiBackendProtocol | None = None,
        is_evaluation: bool = True,
        repository: Path | None = None,
        spec: Spec | None = None,
        original_task: JsonValue = None,
        candidate_digest: str | None = None,
        run_id: str = "",
        game_scope: _GameScope | None = None,
        preset: str = "",
        params: dict[str, JsonValue] | None = None,
        components: tuple[str, ...] = (),
        is_build: bool = False,
    ) -> None:
        self.adapter = adapter
        self.budget = budget
        # True only for a Forge build session (Runtime.build); never at play time.
        self.is_build = is_build is True
        self.spec = spec.model_copy(deep=True) if spec is not None else None
        self._original_task = deepcopy(original_task)
        self.candidate_digest = candidate_digest
        self.run_id = run_id
        self._game_scope = game_scope
        self._game_binding = (preset, deepcopy(params or {}), components)
        self._game_calls: set[asyncio.Task[object]] = set()
        self.adapter_factory = adapter_factory
        self.pi_backend = pi_backend
        self.is_evaluation = is_evaluation
        self.repository = repository.resolve() if repository is not None else None
        self.models_used = {adapter.model}
        self._parent: RunContext | None = None
        self._children: set[RunContext] = set()
        self._call_limit: int | None = None
        self._call_count = 0
        # Calls counted against _call_limit. A cap root (a bound sub-harness) keeps
        # its own cap: its calls never count against an ancestor's model-call cap.
        self._cap_count = 0
        self._cap_root = False
        # Lazily opened per-turn game play (sandbox observe/step), host-finished.
        self._play: _Play | None = None
        self._preset_played = False
        # The host value of the first whole-run play (what GameBackend.run returned),
        # or the failure it raised. A Forge game run's output is this value, never
        # the harness's own return value.
        self._preset_output: tuple[JsonValue] | None = None
        self._preset_failure: Exception | None = None
        self._dry: _DryTurns | None = None
        # Interpreter fuel reported by the sandbox host for this run, if any.
        self.fuel_used = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_usd = 0.0
        # Prompt tokens served from a provider cache (a subset of tokens_in).
        self.cached_tokens_in = 0
        # The provider's ids of the completions this context and its descendants received,
        # in order (the last ``COMPLETION_IDS_KEPT``), when the provider gives them.
        self.completion_ids: list[str] = []
        self._started = time.monotonic()
        self._lock = asyncio.Lock()
        self._failure: Exception | None = None
        self._closed = False
        self.model_params: dict[ParameterKey, ParameterValue] = {}
        self._calls: set[asyncio.Task[object]] = set()
        # sha256 -> image attached to this run; descendants resolve through the lineage.
        self._images: dict[str, ImageInput] = {}
        # ImageInput objects passed to adapters by this context and its descendants.
        self.images_sent = 0

    def _lineage(self) -> tuple[RunContext, ...]:
        nodes = [self]
        current = self._parent
        while current is not None:
            nodes.append(current)
            current = current._parent
        return tuple(nodes)

    @property
    def lineage(self) -> tuple[RunContext, ...]:
        """This context and its ancestors, nearest first."""
        return self._lineage()

    def _cap_lineage(self) -> tuple[RunContext, ...]:
        nodes = [self]
        current: RunContext | None = self
        while current is not None and not current._cap_root and current._parent is not None:
            current = current._parent
            nodes.append(current)
        return tuple(nodes)

    def attach_images(self, images: Sequence[ImageInput]) -> None:
        """Register images by sha256 for this run and its descendants; idempotent.

        Only registered images can be named by an ``ImageStub`` in ``call``; the
        stub is how task JSON, prompts and the sandbox pipe refer to a page
        without carrying its bytes.
        """
        for image in images:
            if not isinstance(image, ImageInput):
                raise TypeError("Only ImageInput values can be attached")
            self._images.setdefault(image.sha256, image)

    def _attached_image(self, sha256: str) -> ImageInput | None:
        for item in self._lineage():
            image = item._images.get(sha256)
            if image is not None:
                return image
        return None

    def _resolve_images(self, images: Sequence[ImageRef]) -> tuple[ImageInput, ...]:
        resolved: list[ImageInput] = []
        for ref in images:
            if isinstance(ref, ImageInput):
                resolved.append(ref)
                continue
            image = self._attached_image(ref.sha256) if isinstance(ref, ImageStub) else None
            if (
                image is None
                or not isinstance(ref, ImageStub)
                or (image.media_type, image.width, image.height)
                != (ref.media_type, ref.width, ref.height)
            ):
                raise AdapterError("Image reference is not attached to this run")
            resolved.append(image)
        return tuple(resolved)

    def _image_detail(self) -> Literal["auto", "low", "high"]:
        """The model's configured image detail hint, else "auto".

        Metering wrappers delegate through ``_inner``; follow a short chain so a
        wrapped adapter keeps its configured hint.
        """
        adapter: object = self.adapter
        for _ in range(4):
            price = getattr(adapter, "_price", None)
            if isinstance(price, Price):
                return price.image.detail if price.image is not None else "auto"
            adapter = getattr(adapter, "_inner", None)
            if adapter is None:
                break
        return "auto"

    def limit_calls(self, maximum: int) -> None:
        if type(maximum) is not int or maximum < 1:
            raise ValueError("Invalid model-call limit")
        self._call_limit = maximum if self._call_limit is None else min(maximum, self._call_limit)

    def fork(self, adapter: ModelAdapter, budget: Budget) -> RunContext:
        self._check()
        child = RunContext(
            adapter,
            budget,
            adapter_factory=self.adapter_factory,
            pi_backend=self.pi_backend,
            is_evaluation=self.is_evaluation,
            repository=self.repository,
            spec=self.spec,
            original_task=self._original_task,
            candidate_digest=self.candidate_digest,
            run_id=self.run_id,
            game_scope=self._game_scope,
            preset=self._game_binding[0],
            params=self._game_binding[1],
            components=self._game_binding[2],
            is_build=self.is_build,
        )
        child._parent = self
        self._children.add(child)
        return child

    def absorb(self, child: RunContext) -> None:
        if child not in self._children or child._calls or child._children or child._game_calls:
            raise ValueError("Child context is unknown or unsettled")
        self._children.remove(child)
        self.models_used.update(child.models_used)
        if child._failure is not None:
            self._failure = child._failure

    async def code(self, task: JsonValue, pack: dict[str, JsonValue]) -> str:
        if isinstance(task, dict) and "repo" in task:
            requested = task["repo"]
            if (
                not isinstance(requested, str)
                or self.repository is None
                or await asyncio.to_thread(Path(requested).resolve) != self.repository
            ):
                raise ValueError("Coding repository is not granted")
        backend = self.pi_backend
        if backend is None:
            from architect.backends.pi import PiBackend

            backend = PiBackend()
        return await self._within_deadline(backend.run(self, task, pack))

    @property
    def original_task(self) -> JsonValue:
        return deepcopy(self._original_task)

    @property
    def execution_config(self) -> dict[str, JsonValue]:
        return deepcopy(self.spec.execution_config) if self.spec is not None else {}

    def check_game_grant(self) -> None:
        self._check()
        if self._game_scope is None or not self._game_scope.active:
            raise BudgetExceeded("Game capability unavailable or expired")

    async def play_game(
        self,
        task: JsonValue,
        *,
        preset: str,
        params: dict[str, JsonValue],
        components: tuple[str, ...],
    ) -> JsonValue:
        self.check_game_grant()
        expected = (self._original_task, self._game_binding)
        supplied = (task, (preset, params, components))
        if json.dumps(expected, sort_keys=True, allow_nan=False) != json.dumps(
            supplied, sort_keys=True, allow_nan=False
        ):
            raise ValueError("Game invocation differs from authenticated binding")
        scope = self._game_scope
        assert scope is not None
        current = asyncio.current_task()
        assert current is not None
        self._game_calls.add(current)
        self._preset_played = True
        try:
            output = await self._within_deadline(
                scope.run(
                    self,
                    deepcopy(task),
                    preset=preset,
                    params=deepcopy(params),
                    components=components,
                )
            )
        except Exception as exc:
            if self._preset_failure is None:
                self._preset_failure = exc
            raise
        finally:
            self._game_calls.discard(current)
        if self._preset_output is None:
            # A private copy: the harness may mutate the value it is handed.
            self._preset_output = (deepcopy(output),)
        return output

    def _turn_play(self) -> _Play:
        """The per-turn play of this run, opened lazily against the scoped backend.

        It is the same backend object ``execute_preset`` uses for game kinds, and
        ``open_turns`` applies every check ``GameBackend.run`` applies.
        """
        self.check_game_grant()
        if self._play is None:
            scope = self._game_scope
            assert scope is not None
            open_turns = getattr(scope.backend, "open_turns", None)
            if open_turns is None:
                from architect.runtime.sandbox import SandboxError

                raise SandboxError("sandbox_capability")
            preset, params, components = self._game_binding
            dry = self._dry
            self._play = open_turns(
                self,
                self.original_task,
                preset=dry.preset if dry is not None else preset,
                params=deepcopy(params),
                components=components,
                scored=dry is None,
            )
        return self._play

    async def _game_op(self, work: Callable[[], Awaitable[_T]]) -> _T:
        scope = self._game_scope
        if scope is None or not scope.active:
            raise BudgetExceeded("Game capability unavailable or expired")
        current = asyncio.current_task()
        assert current is not None
        self._game_calls.add(current)
        scope.pending.add(current)
        try:
            result = await self._within_deadline(work())
            self.check_game_grant()
            return result
        finally:
            self._game_calls.discard(current)
            scope.pending.discard(current)

    async def game_observe(self) -> JsonValue:
        """The settled observation of this run's game; never counted as an action."""
        play = self._turn_play()
        return await self._game_op(play.observe)

    async def game_step(self, action: JsonValue) -> JsonValue:
        """Send one offered action; an unoffered or malformed one is ``sandbox_invalid_call``."""
        from architect.games.types import GameActionError
        from architect.runtime.sandbox import SandboxError

        play = self._turn_play()
        dry = self._dry
        if dry is not None and play.counters.actions >= dry.max_steps:
            dry.stopped = True
            raise _TurnsExhausted("dry run step budget reached")
        try:
            return await self._game_op(lambda: play.step(action))
        except GameActionError:
            raise SandboxError("sandbox_invalid_call") from None

    async def game_finish(self) -> JsonValue:
        """Host-produced game output: finalize, score and close this run's play."""
        play = self._turn_play()
        return await self._game_op(play.finish)

    async def _close_play(self) -> None:
        play = self._play
        if play is not None:
            await play.close()

    async def subharness(self, parent: HarnessArtifact, name: str, value: JsonValue) -> JsonValue:
        """Run the bound, licensed child ``name`` of ``parent`` (``runtime/subharness.py``)."""
        from architect.runtime.subharness import run_subharness

        return await run_subharness(self, parent, name, value)

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self._started

    def _check(self) -> None:
        for item in self._lineage():
            if item._game_scope is not None and not item._game_scope.active:
                item._failure = BudgetExceeded("Game capability expired")
            if isinstance(item.pi_backend, _BackendScope) and not item.pi_backend.active:
                item._failure = BudgetExceeded("Backend capability expired")
            if item._failure is not None:
                raise item._failure
            if item._closed:
                raise BudgetExceeded("Run context is closed")
            if item.cost_usd > item.budget.cost_usd or item.elapsed_s >= item.budget.compute_s:
                item._failure = BudgetExceeded("Run budget exceeded")
                raise item._failure
        if any(wallet.exhausted or wallet.remaining_s <= 0 for wallet in active_wallets()):
            self._failure = BudgetExceeded("Compilation budget exhausted")
            raise self._failure

    async def _within_deadline(self, work: Awaitable[_T]) -> _T:
        try:
            limits = [max(0.0, item.budget.compute_s - item.elapsed_s) for item in self._lineage()]
            limits.extend(wallet.remaining_s for wallet in active_wallets())
            return await asyncio.wait_for(work, timeout=min(limits))
        except TimeoutError as exc:
            self._failure = BudgetExceeded("Run compute budget exceeded")
            raise self._failure from exc

    async def call(
        self,
        prompt: str,
        max_output_tokens: int | None = None,
        *,
        system: str | None = None,
        images: Sequence[ImageRef] = (),
        response_schema: dict[str, JsonValue] | None = None,
    ) -> str:
        """Track the owning task until its budgeted model call is settled.

        ``system`` is an optional byte-stable leading part (fixed instructions).
        The model is given exactly ``system + prompt``, the same text a single
        concatenated prompt would give it; cache-aware adapters send the
        ``system`` part as a cacheable prefix.

        ``images`` are shown with the prompt. An ``ImageStub`` must name an image
        attached to this run or an ancestor (``attach_images``); an unknown one
        raises ``AdapterError("Image reference is not attached to this run")``
        before any reservation. The adapter's quote reserves each image at the
        model's configured ceiling.

        ``response_schema`` is an optional JSON Schema the adapter may use for
        constrained decoding (``ModelRequest.response_schema``). It is a hint,
        never trust: the returned text must still be parsed and validated. An
        invalid or oversized schema fails the call before any reservation.
        """
        task = asyncio.current_task()
        assert task is not None
        self._calls.add(task)
        try:
            return await self._call(prompt, max_output_tokens, system, images, response_schema)
        finally:
            self._calls.discard(task)

    @property
    def model_calls(self) -> int:
        """Model calls reserved by this context and its descendants."""
        return self._call_count

    async def _close(self) -> None:
        """Cancel and drain outstanding calls before final usage is logged."""
        self._closed = True
        pending = tuple(self._calls | self._game_calls)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await self._close_play()
        for child in tuple(self._children):
            await child._close()
            self.absorb(child)

    async def _call(
        self,
        prompt: str,
        max_output_tokens: int | None,
        system: str | None = None,
        images: Sequence[ImageRef] = (),
        response_schema: dict[str, JsonValue] | None = None,
    ) -> str:
        """Reserve a conservative quote before making any model request.

        Failed or cancelled calls retain the quote as consumed usage because no
        reliable final usage is available. An understated dollar quote invalidates
        the run. Token estimates are reconciled to usage, never enforced as quotas.
        """
        async with self._lock:
            self._check()
            try:
                if system is not None and not isinstance(system, str):
                    raise TypeError("system must be a string")
                resolved = self._resolve_images(tuple(images))
                # Optional fields are passed only when set, so a request without
                # them is constructed exactly as before they existed.
                extra: dict[str, object] = {}
                if resolved:
                    extra["images"] = resolved
                    extra["image_detail"] = self._image_detail()
                if response_schema is not None:
                    extra["response_schema"] = response_schema
                request = ModelRequest.model_validate(
                    {
                        "prompt": (system or "") + prompt,
                        "max_output_tokens": max_output_tokens,
                        "cache_prefix": system or None,
                        **extra,
                    }
                )
                self.model_params["temperature"] = request.temperature
                if request.max_output_tokens is not None:
                    self.model_params["max_output_tokens"] = request.max_output_tokens
                quote = self.adapter.quote(request)
                self._check()
                lineage = self._lineage()
                capped = {id(item) for item in self._cap_lineage()}
                for item in lineage:
                    item._check()
                    if (
                        id(item) in capped
                        and item._call_limit is not None
                        and item._cap_count >= item._call_limit
                    ):
                        raise BudgetExceeded("Model-call cap reached")
                    if item.cost_usd + quote.cost_usd > item.budget.cost_usd:
                        raise BudgetExceeded(
                            "Model call would exceed run budget",
                            required_cost_usd=item.cost_usd + quote.cost_usd,
                        )
                wallets = active_wallets()
                reserved = []
                try:
                    for wallet in wallets:
                        wallet.reserve(quote)
                        reserved.append(wallet)
                except BudgetExceeded:
                    for wallet in reserved:
                        wallet.rollback(quote)
                    raise
                for item in lineage:
                    item._call_count += 1
                    item._cap_count += id(item) in capped
                    item.images_sent += len(resolved)
                    item.tokens_in += quote.tokens_in
                    item.tokens_out += quote.tokens_out
                    item.cost_usd += quote.cost_usd
                    item.models_used.add(self.adapter.model)
                response = await self._within_deadline(self.adapter.complete(request))
                for wallet in wallets:
                    wallet.reconcile(quote, response)
                for item in lineage:
                    item.tokens_in += response.tokens_in - quote.tokens_in
                    item.tokens_out += response.tokens_out - quote.tokens_out
                    item.cost_usd = max(0.0, item.cost_usd + response.cost_usd - quote.cost_usd)
                    item.cached_tokens_in += response.cache_read_tokens
                    if response.completion_id is not None:
                        item.completion_ids.append(response.completion_id)
                        del item.completion_ids[:-COMPLETION_IDS_KEPT]
                if response.cost_usd > quote.cost_usd:
                    raise BudgetExceeded("Adapter exceeded its reserved usage")
                if response.model != self.adapter.model:
                    raise AdapterError("Response model does not match adapter")
                self._check()
                return response.text
            except Exception as exc:
                self._failure = exc
                raise
            except asyncio.CancelledError:
                self._failure = BudgetExceeded("Model call was cancelled")
                raise

    async def stream_call(
        self,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
        max_output_tokens: int | None = None,
        *,
        on_event: Callable[[ChatEvent], None] | None = None,
    ) -> ChatResult:
        """One budgeted native chat call whose events reach ``on_event`` as they stream.

        Requires an adapter with ``ChatStreaming`` (Phase 13). Metering mirrors
        ``call``: the adapter's ``quote_chat`` is reserved against this context, its
        ancestors and every active wallet before the stream opens, and reconciled
        with the final ``UsageEvent`` afterwards; usage above the quote raises
        ``BudgetExceeded("Adapter exceeded its reserved usage")``.

        Only the usage reported with the stream's ``DoneEvent`` is final. A stream that
        fails or is cancelled before its ``DoneEvent`` keeps the quote as consumed usage,
        like a failed or cancelled ``call``: the provider has read the prompt and may have
        written most of the reply, and an earlier usage leaves that reply out (Anthropic's
        ``message_start`` counts the prompt and one output token). So a stream costs the
        same whether a caller's deadline cancels it or this run's own deadline ends it. A
        stream that fails or is cancelled after its ``DoneEvent`` is settled at the final
        usage.
        """
        task = asyncio.current_task()
        assert task is not None
        self._calls.add(task)
        try:
            return await self._stream_call(
                system, tuple(messages), tuple(tools), max_output_tokens, on_event
            )
        finally:
            self._calls.discard(task)

    async def _stream_call(
        self,
        system: str,
        messages: tuple[ChatMessage, ...],
        tools: tuple[ToolSpec, ...],
        max_output_tokens: int | None,
        on_event: Callable[[ChatEvent], None] | None,
    ) -> ChatResult:
        async with self._lock:
            self._check()
            # Set once the quote is reserved; ``settle`` reconciles it exactly once.
            reservation: list[tuple[UsageQuote, tuple[CompileWallet, ...]]] = []
            # The usage reported with the stream's DoneEvent, the only final one.
            final: list[UsageEvent] = []

            def settle(usage: UsageEvent) -> None:
                quote, wallets = reservation.pop()
                response = ModelResponse(
                    text="",
                    tokens_in=usage.tokens_in,
                    tokens_out=usage.tokens_out,
                    cost_usd=usage.cost_usd,
                    model=self.adapter.model,
                    cache_write_tokens=usage.cache_write_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                )
                for wallet in wallets:
                    wallet.reconcile(quote, response)
                for item in self._lineage():
                    item.tokens_in += response.tokens_in - quote.tokens_in
                    item.tokens_out += response.tokens_out - quote.tokens_out
                    item.cost_usd = max(0.0, item.cost_usd + response.cost_usd - quote.cost_usd)
                    item.cached_tokens_in += response.cache_read_tokens

            try:
                adapter = self.adapter
                if not isinstance(adapter, ChatStreaming):
                    raise AdapterError("Adapter does not support streaming chat")
                if not isinstance(system, str):
                    raise TypeError("system must be a string")
                if not messages or not all(isinstance(m, ChatMessage) for m in messages):
                    raise ValueError("A chat call needs ChatMessage values")
                if not all(isinstance(tool, ToolSpec) for tool in tools):
                    raise ValueError("Chat tools must be ToolSpec values")
                if max_output_tokens is not None and (
                    type(max_output_tokens) is not int or max_output_tokens <= 0
                ):
                    raise ValueError("Invalid max_output_tokens")
                if max_output_tokens is not None:
                    self.model_params["max_output_tokens"] = max_output_tokens
                quote = adapter.quote_chat(system, messages, tools, max_output_tokens)
                self._check()
                lineage = self._lineage()
                capped = {id(item) for item in self._cap_lineage()}
                for item in lineage:
                    item._check()
                    if (
                        id(item) in capped
                        and item._call_limit is not None
                        and item._cap_count >= item._call_limit
                    ):
                        raise BudgetExceeded("Model-call cap reached")
                    if item.cost_usd + quote.cost_usd > item.budget.cost_usd:
                        raise BudgetExceeded(
                            "Model call would exceed run budget",
                            required_cost_usd=item.cost_usd + quote.cost_usd,
                        )
                wallets = active_wallets()
                reserved = []
                try:
                    for wallet in wallets:
                        wallet.reserve(quote)
                        reserved.append(wallet)
                except BudgetExceeded:
                    for wallet in reserved:
                        wallet.rollback(quote)
                    raise
                for item in lineage:
                    item._call_count += 1
                    item._cap_count += id(item) in capped
                    item.tokens_in += quote.tokens_in
                    item.tokens_out += quote.tokens_out
                    item.cost_usd += quote.cost_usd
                    item.models_used.add(adapter.model)
                reservation.append((quote, wallets))

                async def consume() -> ChatResult:
                    texts: list[str] = []
                    calls: list[ToolCallEvent] = []
                    usage: UsageEvent | None = None
                    stop: StopReason | None = None
                    stream = adapter.stream_chat(system, messages, tools, max_output_tokens)
                    try:
                        async for event in stream:
                            if isinstance(event, UsageEvent):
                                usage = event
                            elif isinstance(event, TextDeltaEvent):
                                texts.append(event.text)
                            elif isinstance(event, ToolCallEvent):
                                calls.append(event)
                            elif isinstance(event, DoneEvent):
                                stop = event.stop_reason
                                if usage is not None:
                                    final.append(usage)
                            if on_event is not None:
                                on_event(event)
                    finally:
                        close = getattr(stream, "aclose", None)
                        if close is not None:
                            await close()
                    if usage is None or stop is None:
                        raise AdapterError("Provider response is incomplete")
                    return ChatResult("".join(texts), tuple(calls), usage, stop)

                result = await self._within_deadline(consume())
                settle(result.usage)
                if result.usage.cost_usd > quote.cost_usd:
                    raise BudgetExceeded("Adapter exceeded its reserved usage")
                self._check()
                return result
            except Exception as exc:
                if reservation and final:
                    settle(final[-1])  # else the quote stays as consumed usage
                self._failure = exc
                raise
            except asyncio.CancelledError:
                if reservation and final:
                    settle(final[-1])  # else the quote stays as consumed usage
                self._failure = BudgetExceeded("Model call was cancelled")
                raise


class Harness(Protocol):
    async def __call__(self, context: RunContext, task: JsonValue) -> JsonValue:
        """Run trusted harness code using the guarded context."""
        ...


def load_harness(
    directory: Path,
    *,
    artifact: HarnessArtifact | None = None,
) -> Harness:
    """Load trusted persisted source for internal evaluation, without licensing it.

    Compile the source directly rather than importing a potentially stale cached
    bytecode file. Client delivery must use ``Runtime.run`` to enforce licensing.
    """
    if artifact is not None and artifact.execution_mode == "sandbox":
        from architect.runtime.sandbox import sandbox_harness

        sandboxed = sandbox_harness(artifact)
        if artifact.forge is not None and artifact.spec.execution_config.get("kind") == "game":
            return _host_game_output(sandboxed)
        return sandboxed
    path = directory / "harness.py"
    namespace: dict[str, object] = {
        "__name__": "_architect_harness_" + uuid4().hex,
        "__file__": str(path),
    }
    exec(compile(path.read_bytes(), str(path), "exec"), namespace)
    harness = namespace.get("run")
    if not inspect.iscoroutinefunction(harness):
        raise TypeError("Harness source must define async run(context, task)")
    return cast(Harness, harness)


def _host_game_output(harness: Harness) -> Harness:
    """For a Forge game artifact the host, not the harness, produces the output.

    After the harness returns, the host finishes this run's play: exactly what
    ``GameBackend.run`` returns (a receipt or a score), or ``{"scored": false}``
    for a dry run. The harness's own return value is always ignored. A harness
    that played through ``execute_preset`` gets the host value that play produced
    (recorded by ``RunContext.play_game``), never what the harness returned; if
    that play failed, its failure propagates even when the harness swallowed it.
    On a harness failure the play is closed unscored and the failure propagates,
    except that a dry run stopped at its step budget still finishes unscored.
    """

    async def run(context: RunContext, task: JsonValue) -> JsonValue:
        try:
            await harness(context, task)
        except Exception:
            dry = context._dry
            if dry is None or not dry.stopped:
                await context._close_play()
                raise
        except BaseException:
            await context._close_play()
            raise
        else:
            if context._preset_played and context._play is None:
                if context._preset_failure is not None:
                    raise context._preset_failure
                if context._preset_output is None:
                    await context._close_play()
                    raise BudgetExceeded("Game capability unavailable or expired")
                return deepcopy(context._preset_output[0])
        return await context.game_finish()

    return run


class RunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    run_id: str
    output: JsonValue
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    cost_usd: float = Field(ge=0)
    wall_time_s: float = Field(ge=0)
    # Model calls the run made, and the provider's ids of the completions it received
    # (in order, the last ``COMPLETION_IDS_KEPT``) when the provider gives them.
    model_calls: int = Field(default=0, ge=0)
    completion_ids: tuple[str, ...] = ()


def _consume(directory: Path, run_id: str) -> None:
    """Atomically consume a persisted artifact before any harness execution."""
    marker = directory / ".consumed"
    try:
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ArtifactExpiredError("Artifact execution attempt already consumed") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(run_id + "\n")
        stream.flush()
        os.fsync(stream.fileno())


class Runtime:
    def __init__(
        self,
        store: TelemetryStore,
        *,
        adapter_factory: Callable[[str], ModelAdapter] | None = None,
        pi_backend: PiBackendProtocol | None = None,
    ) -> None:
        self.store = store
        self.adapter_factory = adapter_factory
        self.pi_backend = pi_backend

    @contextmanager
    def use_pi_backend(self, backend: PiBackendProtocol) -> Iterator[None]:
        scope = _BackendScope(id(self), backend)
        token = _BACKEND.set(scope)
        try:
            yield
        finally:
            scope.active = False
            _BACKEND.reset(token)

    @asynccontextmanager
    async def use_game_backend(self, backend: GameBackendProtocol) -> AsyncIterator[None]:
        scope = _GameScope(id(self), backend)
        token = _GAME.set(scope)
        try:
            yield
        finally:
            scope.active = False
            _GAME.reset(token)
            pending = tuple(scope.pending)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    def scoped_game_backend(self) -> GameBackendProtocol | None:
        """The game backend granted to this runtime in the current scope, if any."""
        scope = _GAME.get()
        if scope is None or scope.owner != id(self) or not scope.active:
            return None
        return scope.backend

    @contextmanager
    def _dry_turns(self, *, preset: str, max_steps: int) -> Iterator[_DryTurns]:
        """Make per-turn game play in this scope unscored and bounded (``forge_dryrun``)."""
        if type(max_steps) is not int or max_steps < 1:
            raise ValueError("Invalid dry-run step budget")
        dry = _DryTurns(id(self), preset, max_steps)
        token = _DRY_TURNS.set(dry)
        try:
            yield dry
        finally:
            _DRY_TURNS.reset(token)

    async def record(self, row: TelemetryRow) -> None:
        """Append mandatory telemetry without blocking the event loop.

        The store serializes writers with a SQLite lock that another process can
        hold; waiting for it must never stall other work on this loop. Failures
        propagate unchanged: an unlogged run is never a successful delivery.
        Once submitted the append is uncancellable: a cancelled caller waits
        for the queued write to land before the cancellation propagates.
        """
        loop = asyncio.get_running_loop()
        context = contextvars.copy_context()
        future = loop.run_in_executor(None, functools.partial(context.run, self.store.append, row))
        cancelled = False
        while True:
            try:
                await asyncio.shield(future)
                break
            except asyncio.CancelledError:
                if future.cancelled():
                    raise
                cancelled = True
                if future.done():
                    break
        if cancelled:
            future.exception()  # retrieved; the caller's cancellation wins
            raise asyncio.CancelledError

    @property
    def last_usage(self) -> TelemetryRow | None:
        """Aggregate descendant usage; never append this snapshot as another bill."""
        entry = _LAST_USAGE.get()
        return entry[1] if entry is not None and entry[0] == id(self) else None

    @property
    def last_row(self) -> TelemetryRow | None:
        entry = _LAST_ROW.get()
        return entry[1] if entry is not None and entry[0] == id(self) else None

    async def run(
        self,
        artifact: HarnessArtifact,
        task: JsonValue,
        adapter: ModelAdapter,
    ) -> RunResult:
        """Execute only an authenticated artifact; log rejected attempts too."""
        with compilation_wallet(artifact.spec.budget) as wallet:
            return await self._execute(
                artifact.spec,
                None,
                task,
                adapter,
                version=artifact.version,
                preset=artifact.preset,
                components=artifact.components,
                artifact=artifact,
                params=artifact.params,
                wallet=wallet,
                candidate_digest=None,
            )

    async def evaluate(
        self,
        spec: Spec,
        harness: Harness,
        task: JsonValue,
        adapter: ModelAdapter,
        *,
        version: int = 1,
        preset: str = "toy",
        components: tuple[str, ...] = (),
        params: dict[str, JsonValue] | None = None,
        candidate_digest: str | None = None,
    ) -> RunResult:
        """Measure internal candidates with budgets, but never issue a license."""
        with compilation_wallet(spec.budget) as wallet:
            return await self._execute(
                spec,
                harness,
                task,
                adapter,
                version=version,
                preset=preset,
                components=components,
                artifact=None,
                params=params,
                wallet=wallet,
                candidate_digest=candidate_digest,
            )

    async def build(
        self,
        spec: Spec,
        harness: Harness,
        task: JsonValue,
        adapter: ModelAdapter,
        *,
        preset: str,
        repository: Path,
    ) -> RunResult:
        """Run a build-time session (Forge); the only way to get ``is_build=True``.

        It is metered and logged like ``evaluate`` but is neither an evaluation
        nor a delivery, and it never licenses anything.
        """
        with compilation_wallet(spec.budget) as wallet:
            return await self._execute(
                spec,
                harness,
                task,
                adapter,
                version=1,
                preset=preset,
                components=(),
                artifact=None,
                params=None,
                wallet=wallet,
                candidate_digest=None,
                build_repository=Path(repository),
            )

    async def _execute(
        self,
        spec: Spec,
        harness: Harness | None,
        task: JsonValue,
        adapter: ModelAdapter,
        *,
        version: int,
        preset: str,
        components: tuple[str, ...],
        artifact: HarnessArtifact | None,
        params: dict[str, JsonValue] | None,
        wallet: CompileWallet,
        candidate_digest: str | None,
        build_repository: Path | None = None,
    ) -> RunResult:
        is_build = build_repository is not None
        run_id = str(uuid4())
        timestamp = datetime.now(UTC)
        repo = task.get("repo") if isinstance(task, dict) else None
        scoped = _BACKEND.get()
        backend = (
            scoped
            if scoped is not None and scoped.owner == id(self) and scoped.active
            else self.pi_backend
        )
        game_scope = _GAME.get()
        if game_scope is not None and game_scope.owner != id(self):
            game_scope = None
        if artifact is not None:
            # Delivery refuses up front, before consuming an ephemeral artifact or
            # spending model budget, when mandatory logging is already impossible.
            await asyncio.to_thread(self.store.probe)
        context = RunContext(
            adapter,
            spec.budget,
            adapter_factory=self.adapter_factory,
            pi_backend=backend,
            is_evaluation=artifact is None and not is_build,
            repository=(
                build_repository
                if build_repository is not None
                else Path(repo)
                if isinstance(repo, str)
                else None
            ),
            spec=spec,
            original_task=task,
            candidate_digest=candidate_digest,
            run_id=run_id,
            game_scope=game_scope,
            preset=preset,
            params=params,
            components=components,
            is_build=is_build,
        )
        dry = _DRY_TURNS.get()
        if dry is not None and dry.owner == id(self) and artifact is None and not is_build:
            context._dry = dry
        accepted = False
        consumed_here = False
        floor_score: float | None = None
        try:
            if artifact is not None:
                if (
                    spec.lifecycle == Lifecycle.EXPIRE
                    and (artifact.directory / ".consumed").exists()
                ):
                    raise ArtifactExpiredError("Artifact execution attempt already consumed")
                if not artifact.licensed:
                    raise UnlicensedArtifactError("Artifact execution license is invalid")
                if adapter.model != artifact.model:
                    raise UnlicensedArtifactError("Adapter does not match licensed model")
                assert artifact.report is not None  # A valid license always binds a report.
                if spec.lifecycle == Lifecycle.EXPIRE:
                    _consume(artifact.directory, run_id)
                    consumed_here = True
                harness = load_harness(artifact.directory, artifact=artifact)
                if not artifact.licensed:
                    raise UnlicensedArtifactError("Artifact changed while loading")
                context.candidate_digest = artifact.artifact_digest
            elif spec.model_target != "cheapest that passes" and adapter.model != spec.model_target:
                raise AdapterError("Adapter does not match evaluation model")
            assert harness is not None
            context._check()
            output = await context._within_deadline(harness(context, task))
            if context._calls or context._children or context._game_calls:
                raise BudgetExceeded("Harness left model calls unfinished")
            context._check()
            if artifact is not None and not artifact.licensed:
                raise UnlicensedArtifactError("Artifact changed during execution")
            result = RunResult(
                run_id=run_id,
                output=output,
                tokens_in=wallet.tokens_in,
                tokens_out=wallet.tokens_out,
                cost_usd=wallet.cost_usd,
                wall_time_s=context.elapsed_s,
                model_calls=context.model_calls,
                completion_ids=tuple(context.completion_ids),
            )
            accepted = True
            return result
        finally:
            try:
                await context._close()
            finally:
                row = TelemetryRow(
                    run_id=run_id,
                    timestamp=timestamp,
                    domain=categorical(spec.domain),
                    task_type=categorical(preset),
                    spec_hash=spec.spec_hash,
                    harness_version=version,
                    lifecycle=spec.lifecycle,
                    preset_origin=categorical(preset),
                    component_set=tuple(categorical(item) for item in components),
                    model=categorical("|".join(sorted(context.models_used))),
                    model_params={**numeric_params(params or {}), **context.model_params},
                    tokens_in=context.tokens_in,
                    tokens_out=context.tokens_out,
                    cost_usd=context.cost_usd,
                    wall_time_s=context.elapsed_s,
                    model_calls=context.model_calls,
                    cached_tokens_in=min(context.cached_tokens_in, context.tokens_in),
                    image_inputs=context.images_sent,
                    floor_type=spec.floor.kind,
                    floor_score=floor_score,
                    floor_limit=spec.floor.limit,
                    accepted=accepted,
                )
                try:
                    await self.record(row)
                    _LAST_ROW.set((id(self), row))
                    _LAST_USAGE.set(
                        (
                            id(self),
                            row.model_copy(
                                update={
                                    "tokens_in": wallet.tokens_in,
                                    "tokens_out": wallet.tokens_out,
                                    "cost_usd": wallet.cost_usd,
                                }
                            ),
                        )
                    )
                finally:
                    if artifact is not None and consumed_here:
                        for name in ("harness.py", "pi-pack.json", "preset.json", "rewrite.diff"):
                            (artifact.directory / name).unlink(missing_ok=True)


def numeric_params(params: dict[str, JsonValue]) -> dict[ParameterKey, ParameterValue]:
    """Project only safe numeric configuration; never copy arbitrary model metadata."""
    keys: tuple[ParameterKey, ...] = (
        "temperature",
        "max_output_tokens",
        "context_window",
        "retries",
        "verifier_depth",
        "terseness",
    )
    result: dict[ParameterKey, ParameterValue] = {}
    for key in keys:
        value = params.get(key)
        if type(value) in (bool, int, float) and (
            not isinstance(value, float) or math.isfinite(value)
        ):
            result[key] = cast(ParameterValue, value)
    return result
