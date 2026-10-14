"""Optional ARC SDK adapter with a killable, bounded process transport.

Gateway providers own one complete submission scorecard and cannot be reused
for candidate trials. OFFLINE staged-public providers create independent episode
scorecards for repeated development evaluation. Numeric competition caps remain
unverified; safety limits and normalized scores do not claim compliance.

Score evidence checked 2026-09-24:
https://docs.arcprize.org/methodology.md documents a final total of 0%..100%.
The 1.15 allowance applies per level, not to a 115-point final-score ceiling.
https://docs.arcprize.org/api-reference/scorecards/close-scorecard.md documents
ScorecardSummary and a final ``score: 100`` example. SDK 0.9.9 exposes the public
EnvironmentScorecard.score field as a float without a range constraint.

Policy ``relative_human_action_efficiency.percent.v1`` binds SDK 0.9.9 and engine
0.9.3, converts completed-card percentages with ``raw / 100``, and rejects values
outside 0..100. Frozen documented example/provenance and explicitly synthetic
boundary cases live in tests/fixtures/arc/score-policy-v1.json. Wholecard scores
use ``score``; game scores use the exact ``environments[].id`` entry's ``score``.
Never reconstruct the metric from actions or assume every WIN is 1. Public
development score normalization alone is not evidence of competition compliance.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import os
import signal
import sys
import tempfile
from pathlib import Path
from typing import Any, Literal, Self, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .types import (
    GameAction,
    GameActionError,
    GameLimits,
    GameProtocolError,
    GameRef,
    GameScore,
    GameSessionError,
    GameUnavailableError,
    Observation,
    decode_action,
    decode_observation,
)

_PACKAGE_ROOT = str(Path(__file__).resolve().parents[2])
# Mandatory host cleanup is not new game work. Reaping gets its own short grace
# even when the caller's gameplay deadline has expired.
_CLEANUP_REAP_TIMEOUT_S = 2.0


async def _drain_cleanup(task: asyncio.Task[None]) -> None:
    """Drain one bounded cleanup task before propagating caller cancellation."""
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancellation = error
        except Exception:
            break  # Retrieve the fixed cleanup error below, never abandon the task.
    try:
        task.result()
    finally:
        if cancellation is not None:
            raise cancellation


class OfficialConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    mode: Literal["offline", "gateway"]
    staged_game_dir: Path | None = None
    gateway_url: str | None = Field(default=None, max_length=2048)
    sdk_version: Literal["0.9.9"] = "0.9.9"
    engine_version: Literal["0.9.3"] = "0.9.3"
    operation_timeout_s: float = Field(default=30.0, gt=0, le=300)

    @model_validator(mode="after")
    def explicit_mode(self) -> Self:
        if self.mode == "offline":
            if self.staged_game_dir is None or self.gateway_url is not None:
                raise ValueError("official_offline_config")
        else:
            if self.staged_game_dir is not None or self.gateway_url is None:
                raise ValueError("official_gateway_config")
            try:
                url = urlsplit(self.gateway_url)
                host = url.hostname
                local = host in ("localhost", "gateway")
                if host and not local:
                    local = ipaddress.ip_address(host).is_loopback
                if (
                    url.scheme != "http"
                    or not local
                    or url.username
                    or url.password
                    or url.path not in ("", "/")
                    or url.query
                    or url.fragment
                    or url.port == 0
                ):
                    raise ValueError
            except ValueError:
                raise ValueError("official_gateway_url") from None
        return self


OFFICIAL_SCORE_METRIC = "relative_human_action_efficiency.percent.v1"

# Fixed worker codes the parent may surface. Every other worker failure stays
# ``official_operation_failed``; all of them are unavailable, never a score.
FORWARDED_WORKER_ERRORS = frozenset(
    {
        "official_level_reset_required",
        "official_multiple_runs",
        "official_baseline_unavailable",
    }
)


def normalize_official_score(value: object) -> GameScore:
    """Convert the documented completed-card percentage, never action counts.

    Call only on trusted completed scorecard fields. This pure conversion is not
    a receipt or evidence that an arbitrary caller-provided number is official.
    """
    if type(value) not in (int, float):
        raise GameProtocolError("official_score_value")
    try:
        raw = float(value)  # type: ignore[arg-type]
    except (OverflowError, ValueError):
        raise GameProtocolError("official_score_value") from None
    if not math.isfinite(raw) or not 0 <= raw <= 100:
        raise GameProtocolError("official_score_value")
    return GameScore(
        normalized=raw / 100,
        raw=raw,
        metric=OFFICIAL_SCORE_METRIC,
        source="official",
    )


def _validate_single_game_score(card: GameScore, games: dict[str, GameScore]) -> None:
    """One term's documented average must equal that term's best-run score."""
    if len(games) == 1:
        game = next(iter(games.values()))
        if (
            card.raw is None
            or game.raw is None
            or not math.isclose(
                card.raw,
                game.raw,
                rel_tol=0.0,
                abs_tol=1e-9,
            )
        ):
            raise GameProtocolError("official_scorecard_inconsistent")


def _unavailable(metric: str = "official_scorecard_pending") -> GameScore:
    return GameScore(normalized=None, raw=None, metric=metric, source="unavailable")


class _WorkerProcess:
    """Persistent JSON worker. Cancellation terminates it; calls never retry."""

    def __init__(self, config: OfficialConfig, deadline: float | None) -> None:
        self.config = config
        self.deadline = deadline
        self.process: asyncio.subprocess.Process | None = None
        self._spawn_task: asyncio.Task[asyncio.subprocess.Process] | None = None
        self._directory: tempfile.TemporaryDirectory[str] | None = None
        self._closed = False
        self._cleanup_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def request(self, request: dict[str, Any], maximum: int = 2_097_152) -> Any:
        timeout = self.config.operation_timeout_s
        if self.deadline is not None:
            timeout = min(timeout, self.deadline - asyncio.get_running_loop().time())
        if timeout <= 0:
            await self.close()
            raise GameSessionError("official_deadline")
        try:
            async with asyncio.timeout(timeout), self._lock:
                if self._closed:
                    raise GameSessionError("official_worker_closed")
                if self.process is None:
                    self._directory = tempfile.TemporaryDirectory(prefix="architect-arc-")
                    environment = {
                        key: value
                        for key, value in os.environ.items()
                        if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "LANG"}
                    }
                    for key in ("HOME", "USERPROFILE", "TMP", "TEMP", "TMPDIR"):
                        environment[key] = self._directory.name
                    environment["PYTHON_DOTENV_DISABLED"] = "1"
                    environment["PYTHONPATH"] = _PACKAGE_ROOT
                    self._spawn_task = asyncio.create_task(
                        asyncio.create_subprocess_exec(
                            sys.executable,
                            "-m",
                            "architect.games._official_worker",
                            stdin=asyncio.subprocess.PIPE,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.DEVNULL,
                            cwd=self._directory.name,
                            env=environment,
                            limit=16_777_217,
                            start_new_session=os.name != "nt",
                        )
                    )
                    self.process = await asyncio.shield(self._spawn_task)
                    if self._closed:
                        # A close that overlapped startup owns/drains the same
                        # spawn task. Never send init or any game work afterward.
                        raise GameSessionError("official_worker_closed")
                assert self.process.stdin is not None and self.process.stdout is not None
                payload = json.dumps(request, allow_nan=False, separators=(",", ":")).encode()
                if len(payload) > 2_097_152:
                    raise GameProtocolError("official_request_limit")
                self.process.stdin.write(payload + b"\n")
                await self.process.stdin.drain()
                # Length prefix is checked before allocating/parsing the response body.
                header = await self.process.stdout.readexactly(4)
                length = int.from_bytes(header, "big")
                if not 1 <= length <= maximum:
                    raise GameProtocolError("official_payload_limit")
                result = json.loads(await self.process.stdout.readexactly(length))
                if not isinstance(result, dict) or set(result) not in ({"ok"}, {"error"}):
                    raise GameProtocolError("official_response")
                if "error" in result:
                    # Never expose worker, SDK, or gateway response text.
                    code = result["error"]
                    if not isinstance(code, str) or code not in FORWARDED_WORKER_ERRORS:
                        code = "official_operation_failed"
                    raise GameUnavailableError(code)
                return result["ok"]
        except asyncio.CancelledError:
            await self.close()
            raise
        except TimeoutError:
            await self.close()
            raise GameSessionError("official_timeout") from None
        except (GameUnavailableError, GameProtocolError, GameSessionError):
            await self.close()
            raise
        except (OSError, ValueError, RuntimeError, asyncio.IncompleteReadError):
            await self.close()
            raise GameProtocolError("official_transport") from None

    async def close(self) -> None:
        self._closed = True
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup())
        await _drain_cleanup(self._cleanup_task)

    async def _cleanup(self) -> None:
        try:
            try:
                async with asyncio.timeout(_CLEANUP_REAP_TIMEOUT_S):
                    if self.process is None and self._spawn_task is not None:
                        try:
                            # Leave half the grace for cancellation/reaping if
                            # native startup has not produced a handle yet.
                            async with asyncio.timeout(_CLEANUP_REAP_TIMEOUT_S / 2):
                                self.process = await asyncio.shield(self._spawn_task)
                        except TimeoutError:
                            self._spawn_task.cancel()
                            outcomes = await asyncio.gather(
                                self._spawn_task, return_exceptions=True
                            )
                            if not isinstance(outcomes[0], BaseException):
                                self.process = outcomes[0]
                        except Exception:
                            pass  # Failed native creation has no live handle.
                    process = self.process
                    if process is not None and process.returncode is None:
                        try:
                            if os.name != "nt":
                                cast(Any, os).killpg(process.pid, cast(Any, signal).SIGKILL)
                            else:
                                process.kill()
                        except ProcessLookupError:
                            pass
                        await process.wait()
            except TimeoutError:
                raise GameSessionError("official_cleanup_timeout") from None
        finally:
            # No await between directory cleanup and bookkeeping. Caller
            # cancellation cannot cut this host-only finalization in half.
            if self._directory is not None:
                try:
                    self._directory.cleanup()
                except OSError:
                    raise GameSessionError("official_cleanup_failed") from None
                finally:
                    self._directory = None


class OfficialGameProvider:
    """Own a gateway submission or independent staged-public OFFLINE episodes.

    Gateway: one scorecard, one make per game, all-environment coverage.
    OFFLINE: each open creates a fresh worker and one-game scorecard. Discovery
    is cached; repeated opens never reuse a previous episode's game state.

    ``deadline_monotonic`` is a trusted host deadline, on the event loop clock.
    Outer cancellation also kills the SDK worker. Close the provider explicitly.
    """

    def __init__(
        self,
        config: OfficialConfig,
        *,
        deadline_monotonic: float | None = None,
    ) -> None:
        if deadline_monotonic is not None and not math.isfinite(deadline_monotonic):
            raise ValueError("official_deadline")
        config = OfficialConfig.model_validate(config.model_dump())
        self.config = config
        self._worker = _WorkerProcess(config, deadline_monotonic)
        self._refs: tuple[GameRef, ...] | None = None
        self._sessions: dict[str, OfficialGameSession] = {}
        self._attempted: set[str] = set()
        self._offline_owner: OfficialGameProvider | None = None
        self._episodes: set[OfficialGameProvider] = set()
        self._episodes_started = 0
        self._episodes_finished = 0
        self._closed = False
        self._finalized = False
        self._score: GameScore | None = None
        self._game_scores: dict[str, GameScore] = {}
        self._cleanup_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def discover(self) -> tuple[GameRef, ...]:
        async with self._lock:
            self._check()
            if self._refs is None:
                config = self.config.model_dump(mode="json")
                if self.config.staged_game_dir is not None:
                    directory = self.config.staged_game_dir.resolve()
                    if not directory.is_dir():
                        raise GameUnavailableError("official_staged_files_missing")
                    config["staged_game_dir"] = str(directory)
                result = await self._worker.request({"op": "init", "config": config})
                try:
                    if not isinstance(result, list) or not 1 <= len(result) <= 1024:
                        raise ValueError
                    refs = tuple(GameRef.model_validate(item) for item in result)
                    expected = (
                        "official_offline" if self.config.mode == "offline" else "official_gateway"
                    )
                    if any(r.backend != expected for r in refs):
                        raise ValueError
                    if len({r.game_id for r in refs}) != len(refs):
                        raise ValueError
                    self._refs = refs
                    if self.config.mode == "offline" and self._offline_owner is None:
                        # Catalog-only discovery must not consume an episode/card.
                        await self._worker.close()
                except (ValueError, ValidationError):
                    await self._worker.close()
                    raise GameProtocolError("official_discovery") from None
            return self._refs

    def _check(self) -> None:
        if self._closed or self._finalized:
            raise GameSessionError("official_provider_closed")

    async def open(self, ref: GameRef, *, limits: GameLimits) -> OfficialGameSession:
        refs = await self.discover()
        async with self._lock:
            self._check()
            if ref not in refs:
                raise GameUnavailableError("official_unknown_game")
            if self.config.mode == "offline" and self._offline_owner is None:
                episode = OfficialGameProvider(
                    self.config,
                    deadline_monotonic=self._worker.deadline,
                )
                episode._offline_owner = self
                self._episodes.add(episode)
                self._episodes_started += 1
                try:
                    # A fresh Arcade scans the explicit staged directory. Verify
                    # the frozen public target still exists before making it.
                    if ref not in await episode.discover():
                        raise GameUnavailableError("official_unknown_game")
                    episode._refs = (ref,)
                    return await episode.open(ref, limits=limits)
                except BaseException:
                    await episode.close()
                    raise
            if ref.game_id in self._attempted:
                raise GameSessionError("official_single_make")
            self._attempted.add(ref.game_id)
            result = await self._worker.request(
                {"op": "make", "game_id": ref.game_id, "limits": limits.model_dump()},
                limits.max_payload_bytes + 128,
            )
            session = OfficialGameSession(self, ref, limits, self._observation(result, limits))
            self._sessions[ref.game_id] = session
            return session

    @staticmethod
    def _observation(result: Any, limits: GameLimits) -> Observation:
        try:
            return decode_observation(json.dumps(result, allow_nan=False).encode(), limits)
        except (ValueError, TypeError):
            raise GameProtocolError("official_observation") from None

    async def finalize(self) -> GameScore:
        async with self._lock:
            if self._finalized:
                if self._score is None:
                    raise GameSessionError("official_finalization_failed")
                return self._score
            self._check()
            if self.config.mode == "offline" and self._offline_owner is None:
                if not self._episodes_started or self._episodes_finished != self._episodes_started:
                    raise GameSessionError("official_incomplete_episodes")
                self._finalized = True
                # A catalog of repeated trials is not one official scorecard.
                self._score = _unavailable("official_episode_scores_only")
                return self._score
            if (
                not self._refs
                or len(self._sessions) != len(self._refs)
                or not all(s._finished for s in self._sessions.values())
            ):
                raise GameSessionError("official_incomplete_coverage")
            # Mark before the call: never retry an ambiguous close.
            self._finalized = True
            result = await self._worker.request({"op": "finalize"})
            try:
                if not isinstance(result, dict) or set(result) != {"card_score", "game_scores"}:
                    raise ValueError
                games = result["game_scores"]
                if not isinstance(games, dict) or set(games) != set(self._sessions):
                    raise ValueError
                game_scores = {key: normalize_official_score(raw) for key, raw in games.items()}
                score = normalize_official_score(result["card_score"])
                _validate_single_game_score(score, game_scores)
            except (ValueError, TypeError):
                await self._worker.close()
                raise GameProtocolError("official_final_score") from None
            self._game_scores = game_scores
            self._score = score
            if self._offline_owner is not None:
                self._offline_owner._episodes_finished += 1
            return score

    async def close(self) -> None:
        self._closed = True
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup())
        await _drain_cleanup(self._cleanup_task)

    async def _cleanup(self) -> None:
        try:
            # Start all cleanup together: many episodes must not consume N times
            # the worker's reap grace or be skipped when one close fails.
            results = await asyncio.gather(
                self._worker.close(),
                *(episode.close() for episode in tuple(self._episodes)),
                return_exceptions=True,
            )
            if any(isinstance(result, BaseException) for result in results):
                raise GameSessionError("official_cleanup_failed")
        finally:
            if self._offline_owner is not None:
                self._offline_owner._episodes.discard(self)


class OfficialGameSession:
    def __init__(
        self,
        provider: OfficialGameProvider,
        ref: GameRef,
        limits: GameLimits,
        observation: Observation,
    ) -> None:
        self.ref = ref
        self._provider = provider
        self._limits = limits
        self._observation = observation
        self._actions_used = 0
        self._closed = False
        self._finished = False
        self._lock = asyncio.Lock()

    @property
    def actions_used(self) -> int:
        return self._actions_used

    def _check(self) -> None:
        if self._closed or self._finished:
            raise GameSessionError("official_session_closed")
        self._provider._check()

    async def observe(self) -> Observation:
        self._check()
        return self._observation

    async def step(self, action: GameAction) -> Observation:
        async with self._lock:
            self._check()
            action = decode_action(action, self._observation.available_actions)
            if self._observation.state == "WIN":
                raise GameActionError("official_terminal_state")
            if self._actions_used >= self._limits.max_actions:
                raise GameSessionError("official_action_limit")
            self._actions_used += 1  # Failed and cancelled actions still count.
            result = await self._provider._worker.request(
                {"op": "step", "game_id": self.ref.game_id, "action": action.model_dump()},
                self._limits.max_payload_bytes + 128,
            )
            self._observation = self._provider._observation(result, self._limits)
            return self._observation

    async def finalize(self) -> GameScore:
        async with self._lock:
            if self._closed:
                raise GameSessionError("official_session_closed")
            self._finished = True
            provider = self._provider
            if (
                provider._refs
                and len(provider._sessions) == len(provider._refs)
                and all(s._finished for s in provider._sessions.values())
            ):
                await provider.finalize()
                return provider._game_scores[self.ref.game_id]
            # Not an in-flight scorecard read; this is explicit absence of evidence.
            return _unavailable("official_scorecard_pending")

    async def close(self) -> None:
        self._closed = True
        if self._provider._offline_owner is not None:
            await self._provider.close()
