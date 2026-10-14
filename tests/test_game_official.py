"""Deterministic adapter tests; gateway integration uses the optional real SDK.

The fake gateway implements public REST responses, not a substitute game SDK.
No hidden data, live service, measured official game result, or competition claim.
Documented completed-card percentages are verified with frozen public evidence.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import threading
from collections.abc import Iterator
from enum import IntEnum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from architect.games._official_worker import _OfficialDriver, _wire_observation
from architect.games.official import OfficialConfig, OfficialGameProvider
from architect.games.types import (
    GameAction,
    GameActionError,
    GameLimits,
    GameProtocolError,
    GameRef,
    GameSessionError,
    GameUnavailableError,
)


class Action(IntEnum):
    RESET = 0
    ACTION1 = 1
    ACTION6 = 6


def public_frame(**changes: Any) -> dict[str, Any]:
    frame = {
        "game_id": "aa00-v1",
        "frame": [[[1, 2], [3, 4]], [[4, 3], [2, 1]]],
        "state": "NOT_FINISHED",
        "levels_completed": 0,
        "win_levels": 2,
        "available_actions": [1, 6],
        "guid": "public-guid",
    }
    frame.update(changes)
    return frame


class FakeEnvironment:
    def __init__(self) -> None:
        self.action_space = [Action.ACTION1, Action.ACTION6]
        self.observation_space = SimpleNamespace(
            frame=[[[1, 2], [3, 4]], [[4, 3], [2, 1]]],
            state=SimpleNamespace(name="NOT_FINISHED"),
            full_reset=False,
            levels_completed=0,
            win_levels=2,
        )
        self.steps: list[tuple[Action, Any]] = []

    def step(self, action: Action, data: Any = None) -> Any:
        self.steps.append((action, data))
        return self.observation_space

    def reset(self) -> None:
        raise AssertionError("adapter must not call reset")


class FakeArcade:
    def __init__(self, ids: tuple[str, ...] = ("aa00-v1", "bb00-v2")) -> None:
        self.ids = ids
        self.cards = 0
        self.closes = 0
        self.makes: list[tuple[str, str]] = []
        self.env = FakeEnvironment()
        # Public completed-card runs: one play with published human baselines.
        self.runs: list[Any] = [SimpleNamespace(message=None, level_baseline_actions=[1, 1])]
        self.baselines: Any = [1, 1]

    def get_environments(self) -> list[Any]:
        return [
            SimpleNamespace(game_id=game_id, baseline_actions=self.baselines)
            for game_id in self.ids
        ]

    def open_scorecard(self) -> str:
        self.cards += 1
        return "one-card"

    def make(self, game_id: str, *, scorecard_id: str, save_recording: bool) -> Any:
        assert not save_recording
        self.makes.append((game_id, scorecard_id))
        return self.env

    def close_scorecard(self, card: str) -> Any:
        assert card == "one-card"
        self.closes += 1
        game_scores = {
            game_id: 25.0 if game_id.startswith("aa00") else 75.0 for game_id, _ in self.makes
        }
        return SimpleNamespace(
            card_id=card,
            score=sum(game_scores.values()) / len(game_scores),
            environments=[
                SimpleNamespace(id=key, score=value, runs=self.runs)
                for key, value in game_scores.items()
            ],
        )

    def get_scorecard(self, *_: Any, **__: Any) -> None:
        raise AssertionError("no in-flight score read")


def test_driver_public_calls_single_make_and_complete_card() -> None:
    arcade = FakeArcade()
    driver = _OfficialDriver(
        arcade, Action, OfficialConfig(mode="gateway", gateway_url="http://127.0.0.1")
    )
    assert arcade.cards == 1
    make = {"op": "make", "game_id": "aa00-v1", "limits": GameLimits().model_dump()}
    observation = driver.handle(make)
    assert len(observation["frames"]) == 2
    with pytest.raises(GameProtocolError, match="official_single_make"):
        driver.handle(make)
    with pytest.raises(GameProtocolError, match="official_incomplete_coverage"):
        driver.handle({"op": "finalize"})
    driver.handle(
        {
            "op": "step",
            "game_id": "aa00-v1",
            "action": {
                "name": "ACTION6",
                "x": 63,
                "y": 0,
            },
        }
    )
    driver.handle({"op": "step", "game_id": "aa00-v1", "action": {"name": "RESET"}})
    assert arcade.env.steps == [(Action.ACTION6, {"x": 63, "y": 0}), (Action.RESET, None)]
    driver.handle({**make, "game_id": "bb00-v2"})
    result = driver.handle({"op": "finalize"})
    assert result == {"card_score": 50.0, "game_scores": {"aa00-v1": 25.0, "bb00-v2": 75.0}}
    assert arcade.closes == 1
    assert len(arcade.makes) == 2
    with pytest.raises(GameProtocolError, match="official_scorecard_closed"):
        driver.handle({"op": "finalize"})


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://example.com",
        "http://192.168.1.2",
        "http://localhost@evil.com",
        "http://localhost/path",
        "http://localhost?x=1",
        "http://localhost#x",
        "http://localhost:0",
    ],
)
def test_gateway_rejects_nonlocal_or_ambiguous_url(url: str) -> None:
    with pytest.raises(ValidationError):
        OfficialConfig(mode="gateway", gateway_url=url)


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:8001", "http://[::1]:8001", "http://gateway:8001"]
)
def test_explicit_local_urls(url: str) -> None:
    assert OfficialConfig(mode="gateway", gateway_url=url).gateway_url == url


def test_mode_and_timeout_are_explicit() -> None:
    for fields in [
        {},
        {"mode": "normal"},
        {"mode": "offline"},
        {"mode": "gateway"},
        {
            "mode": "offline",
            "staged_game_dir": ".",
            "gateway_url": "http://localhost",
        },
        {"mode": "offline", "staged_game_dir": ".", "operation_timeout_s": float("nan")},
        {
            "mode": "offline",
            "staged_game_dir": ".",
            "operation_timeout_s": 301,
        },
    ]:
        with pytest.raises(ValidationError):
            OfficialConfig.model_validate(fields)


@pytest.mark.parametrize(
    "changes",
    [
        {"frame": [[[256]]]},
        {"frame": [[[True]]]},
        {"frame": [[[1], [1, 2]]]},
        {"available_actions": [True]},
        {"available_actions": [9]},
        {"levels_completed": -1},
    ],
)
def test_raw_gateway_frames_validated_before_sdk_numpy_narrowing(changes: dict[str, Any]) -> None:
    with pytest.raises(GameProtocolError):
        _wire_observation(public_frame(**changes), GameLimits())


def test_frame_bound_and_game_over_actions() -> None:
    with pytest.raises(GameProtocolError):
        _wire_observation(public_frame(), GameLimits(max_frames=1))
    assert _wire_observation(public_frame(state="GAME_OVER"), GameLimits())[
        "available_actions"
    ] == ["RESET"]


async def test_missing_staged_directory_never_falls_back(tmp_path: Path) -> None:
    provider = OfficialGameProvider(
        OfficialConfig(mode="offline", staged_game_dir=tmp_path / "none")
    )
    with pytest.raises(GameUnavailableError, match="official_staged_files_missing"):
        await provider.discover()
    assert provider._worker.process is None
    await provider.close()
    await provider.close()


class MemoryWorker:
    def __init__(self, arcade: FakeArcade, config: OfficialConfig | None = None) -> None:
        self.driver = _OfficialDriver(
            arcade,
            Action,
            config or OfficialConfig(mode="gateway", gateway_url="http://127.0.0.1"),
        )
        self.closed = False
        self.deadline: float | None = None

    async def request(self, request: dict[str, Any], maximum: int = 2_097_152) -> Any:
        if request["op"] == "init":
            return [ref.model_dump() for ref in self.driver.refs]
        return self.driver.handle(request)

    async def close(self) -> None:
        self.closed = True


async def test_provider_sessions_limits_and_pending_score(tmp_path: Path) -> None:
    provider = OfficialGameProvider(OfficialConfig(mode="gateway", gateway_url="http://127.0.0.1"))
    arcade = FakeArcade()
    worker = MemoryWorker(arcade)
    provider._worker = worker  # type: ignore[assignment]
    refs = await provider.discover()
    with pytest.raises(GameUnavailableError, match="official_unknown_game"):
        await provider.open(
            GameRef(backend="local", game_id="aa00-v1", revision="v1"), limits=GameLimits()
        )
    session = await provider.open(refs[0], limits=GameLimits(max_actions=1))
    with pytest.raises(GameSessionError, match="official_single_make"):
        await provider.open(refs[0], limits=GameLimits())
    assert session.actions_used == 0
    before = await session.observe()
    assert await session.observe() == before
    assert not arcade.env.steps
    with pytest.raises(GameActionError):
        await session.step(GameAction(name="ACTION7"))
    await session.step(GameAction(name="ACTION6", x=4, y=5))
    assert session.actions_used == 1
    with pytest.raises(GameSessionError, match="official_action_limit"):
        await session.step(GameAction(name="ACTION1"))
    with pytest.raises(GameSessionError, match="official_incomplete_coverage"):
        await provider.finalize()
    assert (await session.finalize()).metric == "official_scorecard_pending"
    assert arcade.closes == 0
    second = await provider.open(refs[1], limits=GameLimits())
    score = await second.finalize()
    assert score.source == "official" and score.normalized == 0.75 and score.raw == 75
    assert arcade.closes == 1
    assert (await provider.finalize()).normalized == 0.5  # Wholecard, not second game's 0.75.
    assert await second.finalize() == score
    assert (await session.finalize()).normalized == 0.25  # Cached first-game score after close.
    assert arcade.closes == 1
    await session.close()
    await session.close()
    with pytest.raises(GameSessionError):
        await session.observe()
    await provider.close()
    assert worker.closed


@pytest.fixture
def gateway() -> Iterator[tuple[str, dict[str, Any]]]:
    state: dict[str, Any] = {
        "calls": [],
        "release": threading.Event(),
        "entered": threading.Event(),
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: Any) -> None:
            pass

        def do_GET(self) -> None:
            self.reply()

        def do_POST(self) -> None:
            self.reply()

        def reply(self) -> None:
            size = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(size)) if size else {}
            state["calls"].append((self.path, data))
            if state.get("hang") == self.path:
                state["entered"].set()
                state["release"].wait(10)
            if self.path == "/api/games":
                value: Any = [{"game_id": "aa00-v1", "title": "Public fake gateway"}]
            elif self.path == "/api/games/aa00":
                value = {"game_id": "aa00-v1", "title": "Public fake gateway"}
            elif self.path == "/api/scorecard/open":
                value = {"card_id": "one-card"}
            elif self.path == "/api/scorecard/close":
                value = state.get(
                    "scorecard_response",
                    {
                        "card_id": "one-card",
                        "score": 37.5,
                        "environments": [
                            {
                                "id": "aa00-v1",
                                "score": 37.5,
                                "runs": [
                                    {
                                        "id": "aa00-v1",
                                        "score": 37.5,
                                        "levels_completed": 1,
                                        "actions": 3,
                                        "state": "NOT_FINISHED",
                                        "level_scores": [37.5],
                                    }
                                ],
                            }
                        ],
                    },
                )
            elif self.path.startswith("/api/cmd/"):
                value = public_frame(**state.get("frame_changes", {}))
            else:
                raise AssertionError("unexpected SDK API")
            payload = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        state["release"].set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


sdk = pytest.mark.skipif(importlib.util.find_spec("arc_agi") is None, reason="optional ARC SDK")


@sdk
async def test_real_sdk_process_gateway_lifecycle(
    gateway: tuple[str, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url, state = gateway
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("OPERATION_MODE", "normal")
    monkeypatch.setenv("ARC_BASE_URL", "http://example.com")
    provider = OfficialGameProvider(OfficialConfig(mode="gateway", gateway_url=url))
    try:
        refs = await provider.discover()
        assert refs[0].revision == "v1"
        session = await provider.open(refs[0], limits=GameLimits())
        assert len((await session.observe()).frames) == 2
        await session.step(GameAction(name="ACTION6", x=63, y=0))
        await session.step(GameAction(name="RESET"))
        result = await session.finalize()
        assert result.source == "official" and result.raw == 37.5 and result.normalized == 0.375
        paths = [path for path, _ in state["calls"]]
        assert paths == [
            "/api/games",
            "/api/scorecard/open",
            "/api/games/aa00",
            "/api/cmd/RESET",
            "/api/cmd/ACTION6",
            "/api/cmd/RESET",
            "/api/scorecard/close",
        ]
        assert state["calls"][1][1]["competition_mode"] is True
        assert "guid" not in state["calls"][3][1]
        assert state["calls"][5][1]["guid"] == "public-guid"
        assert state["calls"][4][1]["x"] == 63
    finally:
        await provider.close()
    assert provider._worker.process is not None
    assert provider._worker.process.returncode is not None


@sdk
async def test_real_process_cancellation_kills_sdk_request(
    gateway: tuple[str, dict[str, Any]],
) -> None:
    url, state = gateway
    provider = OfficialGameProvider(OfficialConfig(mode="gateway", gateway_url=url))
    try:
        refs = await provider.discover()
        state["hang"] = "/api/cmd/RESET"
        operation = asyncio.create_task(provider.open(refs[0], limits=GameLimits()))
        assert await asyncio.to_thread(state["entered"].wait, 5)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert provider._worker.process is not None
        assert provider._worker.process.returncode is not None
        with pytest.raises(GameSessionError):
            await provider.open(refs[0], limits=GameLimits())
    finally:
        await provider.close()


@sdk
async def test_real_process_deadline_and_byte_bound(gateway: tuple[str, dict[str, Any]]) -> None:
    url, state = gateway
    provider = OfficialGameProvider(OfficialConfig(mode="gateway", gateway_url=url))
    try:
        refs = await provider.discover()
        state["frame_changes"] = {"frame": [[[1] * 64 for _ in range(64)]]}
        with pytest.raises(GameUnavailableError, match="official_operation_failed"):
            await provider.open(refs[0], limits=GameLimits(max_payload_bytes=256))
        assert provider._worker.process is not None
        assert provider._worker.process.returncode is not None
    finally:
        await provider.close()
    expired = OfficialGameProvider(
        OfficialConfig(mode="gateway", gateway_url=url),
        deadline_monotonic=asyncio.get_running_loop().time() - 1,
    )
    with pytest.raises(GameSessionError, match="official_deadline"):
        await expired.discover()
    assert expired._worker.process is None


@sdk
async def test_real_operation_timeout_kills_worker(gateway: tuple[str, dict[str, Any]]) -> None:
    url, state = gateway
    provider = OfficialGameProvider(OfficialConfig(mode="gateway", gateway_url=url))
    try:
        refs = await provider.discover()
        session = await provider.open(refs[0], limits=GameLimits())
        state["hang"] = "/api/cmd/ACTION1"
        # SDK still has a 30-second timeout; this outer deadline must terminate it.
        provider._worker.deadline = asyncio.get_running_loop().time() + 0.15
        with pytest.raises(GameSessionError, match="official_timeout"):
            await session.step(GameAction(name="ACTION1"))
        assert session.actions_used == 1
        assert provider._worker.process is not None
        assert provider._worker.process.returncode is not None
    finally:
        await provider.close()


@sdk
async def test_real_gateway_full_reset_is_rejected(gateway: tuple[str, dict[str, Any]]) -> None:
    url, state = gateway
    provider = OfficialGameProvider(OfficialConfig(mode="gateway", gateway_url=url))
    try:
        refs = await provider.discover()
        session = await provider.open(refs[0], limits=GameLimits())
        state["frame_changes"] = {"full_reset": True}
        with pytest.raises(GameUnavailableError, match="^official_level_reset_required$"):
            await session.step(GameAction(name="RESET"))
    finally:
        await provider.close()


@sdk
async def test_offline_empty_staging_is_explicitly_unavailable(tmp_path: Path) -> None:
    provider = OfficialGameProvider(OfficialConfig(mode="offline", staged_game_dir=tmp_path))
    try:
        with pytest.raises(GameUnavailableError, match="official_operation_failed"):
            await provider.discover()
    finally:
        await provider.close()


def test_provider_revalidates_copied_configuration() -> None:
    config = OfficialConfig(mode="gateway", gateway_url="http://127.0.0.1")
    with pytest.raises(ValidationError):
        OfficialGameProvider(config.model_copy(update={"gateway_url": "http://example.com"}))
    with pytest.raises(ValueError, match="official_deadline"):
        OfficialGameProvider(config, deadline_monotonic=float("inf"))


@pytest.mark.parametrize("ids", [(), ("aa00",), ("aa00-v1", "aa00-v1")])
def test_driver_requires_complete_versioned_unique_discovery(ids: tuple[str, ...]) -> None:
    with pytest.raises(GameProtocolError):
        _OfficialDriver(
            FakeArcade(ids), Action, OfficialConfig(mode="offline", staged_game_dir=Path("."))
        )


async def test_worker_eof_and_environment_are_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    from architect.games.official import _WorkerProcess

    reader = asyncio.StreamReader()
    reader.feed_eof()
    captured: dict[str, Any] = {}

    class Writer:
        def write(self, _: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

    class Process:
        stdin = Writer()
        stdout = reader
        returncode: int | None = 0

    async def spawn(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return Process()

    monkeypatch.setenv("SECRET_TOKEN", "must-not-pass")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-pass")
    monkeypatch.setenv("HTTPS_PROXY", "http://example.com")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    worker = _WorkerProcess(OfficialConfig(mode="gateway", gateway_url="http://127.0.0.1"), None)
    with pytest.raises(GameProtocolError, match="official_transport"):
        await worker.request({"op": "init"})
    environment = captured["env"]
    assert "SECRET_TOKEN" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert "HTTPS_PROXY" not in environment
    assert environment["PYTHON_DOTENV_DISABLED"] == "1"
    assert environment["HOME"] == captured["cwd"]
    assert worker._closed


@pytest.mark.parametrize("payload", [b'{"unexpected":1}', b"[]", b"not-json", b""])
async def test_worker_malformed_response_is_fixed_code(
    payload: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from architect.games.official import _WorkerProcess

    reader = asyncio.StreamReader()
    reader.feed_data(len(payload).to_bytes(4, "big") + payload)
    reader.feed_eof()

    class Writer:
        def write(self, _: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

    class Process:
        stdin = Writer()
        stdout = reader
        returncode: int | None = 0

    async def spawn(*args: Any, **kwargs: Any) -> Any:
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    worker = _WorkerProcess(OfficialConfig(mode="gateway", gateway_url="http://127.0.0.1"), None)
    with pytest.raises(GameProtocolError, match="^official_(response|transport|payload_limit)$"):
        await worker.request({"op": "init"})
    assert worker._closed


async def test_offline_repeated_independent_episodes_and_one_game_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from architect.games import official

    workers: list[MemoryWorker] = []

    def worker_factory(config: OfficialConfig, deadline: float | None) -> MemoryWorker:
        worker = MemoryWorker(FakeArcade(), config)
        worker.deadline = deadline
        workers.append(worker)
        return worker

    monkeypatch.setattr(official, "_WorkerProcess", worker_factory)
    provider = OfficialGameProvider(OfficialConfig(mode="offline", staged_game_dir=tmp_path))
    refs = await provider.discover()
    assert len(refs) == 2
    assert workers[0].closed and workers[0].driver.arcade.cards == 0
    assert await provider.discover() == refs
    assert len(workers) == 1  # Parent catalog cached, no second discovery worker.
    first = await provider.open(refs[0], limits=GameLimits())
    initial = await first.observe()
    await first.step(GameAction(name="ACTION1"))
    score = await first.finalize()
    assert score.source == "official" and score.raw == 25 and score.normalized == 0.25
    assert workers[1].driver.arcade.cards == workers[1].driver.arcade.closes == 1
    # Other discovered game is intentionally not made in this development episode.
    assert workers[1].driver.arcade.makes == [(refs[0].game_id, "one-card")]
    await first.close()
    assert workers[1].closed
    assert not provider._episodes
    # Repeated Stage 2/4 trial has independent environment and scorecard ownership.
    second = await provider.open(refs[0], limits=GameLimits())
    assert second is not first and second._provider is not first._provider
    assert await second.observe() == initial
    assert second.actions_used == 0
    assert workers[2].driver.arcade.env is not workers[1].driver.arcade.env
    assert workers[2].driver.arcade.env.steps == []
    with pytest.raises(GameSessionError, match="official_incomplete_episodes"):
        await provider.finalize()
    assert await second.finalize() == score
    assert workers[2].driver.arcade.cards == workers[2].driver.arcade.closes == 1
    assert (await provider.finalize()).metric == "official_episode_scores_only"
    await provider.close()
    assert workers[2].closed
    assert not provider._episodes


async def test_offline_parent_close_stops_unfinished_episode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from architect.games import official

    workers: list[MemoryWorker] = []

    def worker_factory(config: OfficialConfig, deadline: float | None) -> MemoryWorker:
        worker = MemoryWorker(FakeArcade(), config)
        worker.deadline = deadline
        workers.append(worker)
        return worker

    monkeypatch.setattr(official, "_WorkerProcess", worker_factory)
    provider = OfficialGameProvider(OfficialConfig(mode="offline", staged_game_dir=tmp_path))
    ref = (await provider.discover())[0]
    first = await provider.open(ref, limits=GameLimits())
    second = await provider.open(ref, limits=GameLimits())
    assert first._provider is not second._provider
    await provider.close()
    assert all(worker.closed for worker in workers)
    with pytest.raises(GameSessionError, match="official_provider_closed"):
        await first.observe()
    with pytest.raises(GameSessionError, match="official_provider_closed"):
        await second.observe()


def test_offline_driver_card_is_lazy_and_only_episode_coverage_is_required() -> None:
    arcade = FakeArcade()
    driver = _OfficialDriver(
        arcade,
        Action,
        OfficialConfig(mode="offline", staged_game_dir=Path(".")),
    )
    assert arcade.cards == 0
    with pytest.raises(GameProtocolError, match="official_incomplete_episode"):
        driver.handle({"op": "finalize"})
    driver.handle({"op": "make", "game_id": "aa00-v1", "limits": GameLimits().model_dump()})
    assert arcade.cards == 1
    with pytest.raises(GameProtocolError, match="official_single_episode"):
        driver.handle({"op": "make", "game_id": "bb00-v2", "limits": GameLimits().model_dump()})
    assert driver.handle({"op": "finalize"}) == {
        "card_score": 25.0,
        "game_scores": {"aa00-v1": 25.0},
    }
    assert arcade.closes == 1


@sdk
async def test_pinned_sdk_offline_supports_multiple_public_scorecards(tmp_path: Path) -> None:
    """API compatibility check, not a smoke run of a staged official game."""
    import os
    import sys

    script = """
import arc_agi
import json
import logging
logging.disable(logging.CRITICAL)
logger = logging.getLogger('compatibility')
logger.addHandler(logging.NullHandler())
logger.propagate = False
arcade = arc_agi.Arcade(
    operation_mode=arc_agi.OperationMode.OFFLINE,
    environments_dir='empty-staged-public', logger=logger,
)
first = arcade.open_scorecard()
second = arcade.open_scorecard()
assert first != second
assert arcade.get_environments() == []
assert arcade.close_scorecard(first).card_id == first
assert arcade.close_scorecard(second).card_id == second
from arc_agi.scorecard import EnvironmentScorecard
from pathlib import Path
import sys
example = EnvironmentScorecard.model_validate_json(Path(sys.argv[1]).read_bytes())
assert example.score == 100
assert example.environments[0].id == 'am92-80effacb'
assert example.environments[0].score == 100
print(json.dumps({'independent_cards': True, 'staged_games_played': 0, 'documented_fixture': True}))
"""
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT"}
    }
    environment["PYTHON_DOTENV_DISABLED"] = "1"
    environment["HOME"] = str(tmp_path)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(Path(__file__).parent / "fixtures" / "arc" / "documented-close-scorecard.json"),
        cwd=tmp_path,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(30):
            stdout, _ = await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 0
    assert json.loads(stdout) == {
        "independent_cards": True,
        "staged_games_played": 0,
        "documented_fixture": True,
    }


@pytest.mark.parametrize(
    "raw",
    [True, False, "50", None, -1, 100.01, 115, float("nan"), float("inf"), float("-inf"), 10**400],
)
def test_official_percentage_rejects_unknown_or_invalid_values(raw: object) -> None:
    from architect.games.official import normalize_official_score

    with pytest.raises(GameProtocolError, match="official_score_value"):
        normalize_official_score(raw)


def test_frozen_documented_score_example_and_synthetic_boundaries() -> None:
    import hashlib

    from architect.games._official_worker import _scorecard_scores
    from architect.games.official import OFFICIAL_SCORE_METRIC, normalize_official_score

    fixtures = Path(__file__).parent / "fixtures" / "arc"
    policy = json.loads((fixtures / "score-policy-v1.json").read_text())
    assert policy["metric"] == OFFICIAL_SCORE_METRIC
    assert policy["sdk_version"] == OfficialConfig.model_fields["sdk_version"].default
    assert policy["engine_version"] == OfficialConfig.model_fields["engine_version"].default
    assert policy["normalization_divisor"] == 100 and policy["raw_range"] == [0, 100]
    assert policy["completed_response_only"] is True
    example_bytes = (fixtures / policy["documented_fixture"]["path"]).read_bytes()
    assert hashlib.sha256(example_bytes).hexdigest() == policy["documented_fixture"]["sha256_utf8"]
    documented = json.loads(example_bytes)
    scores = _scorecard_scores(
        documented, card_id=documented["card_id"], game_ids={"am92-80effacb"}
    )
    assert scores == {"card_score": 100.0, "game_scores": {"am92-80effacb": 100.0}}
    assert normalize_official_score(scores["card_score"]).normalized == 1
    synthetic = json.loads((fixtures / "synthetic-score-boundaries.json").read_text())
    assert synthetic["kind"] == "synthetic_boundary_cases_not_official_runs"
    for case in synthetic["cases"]:
        normalized = normalize_official_score(case["score"])
        assert normalized.raw == case["score"]
        assert normalized.normalized == case["normalized"]
        assert normalized.metric == OFFICIAL_SCORE_METRIC
        assert normalized.source == "official"


@pytest.mark.parametrize(
    "card",
    [
        {"card_id": "wrong", "score": 25, "environments": [{"id": "aa00-v1", "score": 25}]},
        {"card_id": "one-card", "environments": [{"id": "aa00-v1", "score": 25}]},
        {"card_id": "one-card", "score": 25, "environments": []},
        {"card_id": "one-card", "score": 25, "environments": [{"id": "aa00-other", "score": 25}]},
        {
            "card_id": "one-card",
            "score": 25,
            "environments": [{"id": "aa00-v1", "score": 25}, {"id": "aa00-v1", "score": 25}],
        },
        {"card_id": "one-card", "score": 25, "environments": [{"id": "aa00-v1"}]},
        {"card_id": "one-card", "score": 25, "environments": [{"id": "aa00-v1", "score": True}]},
        {"card_id": "one-card", "score": 25, "environments": [{"id": "aa00-v1", "score": 115}]},
    ],
)
def test_completed_card_requires_exact_identity_coverage_and_values(card: dict[str, Any]) -> None:
    from architect.games._official_worker import _scorecard_scores

    with pytest.raises(GameProtocolError):
        _scorecard_scores(card, card_id="one-card", game_ids={"aa00-v1"})


@sdk
@pytest.mark.parametrize("invalid", [True, "37.5", -1, 115, float("nan"), float("inf")])
async def test_real_gateway_raw_score_rejected_before_sdk_coercion(
    gateway: tuple[str, dict[str, Any]],
    invalid: object,
) -> None:
    url, state = gateway
    provider = OfficialGameProvider(OfficialConfig(mode="gateway", gateway_url=url))
    try:
        refs = await provider.discover()
        session = await provider.open(refs[0], limits=GameLimits())
        state["scorecard_response"] = {
            "card_id": "one-card",
            "score": invalid,
            "environments": [{"id": "aa00-v1", "score": 37.5}],
        }
        with pytest.raises(GameUnavailableError, match="official_operation_failed"):
            await session.finalize()
        with pytest.raises(GameSessionError, match="official_finalization_failed"):
            await session.finalize()
        assert sum(path == "/api/scorecard/close" for path, _ in state["calls"]) == 1
    finally:
        await provider.close()


@sdk
async def test_real_gateway_per_game_bool_and_wrong_card_are_not_scores(
    gateway: tuple[str, dict[str, Any]],
) -> None:
    url, state = gateway
    provider = OfficialGameProvider(OfficialConfig(mode="gateway", gateway_url=url))
    try:
        refs = await provider.discover()
        session = await provider.open(refs[0], limits=GameLimits())
        state["scorecard_response"] = {
            "card_id": "one-card",
            "score": 25,
            "environments": [{"id": "aa00-v1", "score": True}],
        }
        with pytest.raises(GameUnavailableError, match="official_operation_failed"):
            await session.finalize()
        assert provider._score is None
    finally:
        await provider.close()


async def _live_cleanup_episode(
    tmp_path: Path,
) -> tuple[OfficialGameProvider, OfficialGameProvider, asyncio.subprocess.Process, Path]:
    """Real killable child, without requiring optional SDK or staged game files."""
    import os
    import sys
    import tempfile

    owner = OfficialGameProvider(OfficialConfig(mode="offline", staged_game_dir=tmp_path))
    episode = OfficialGameProvider(OfficialConfig(mode="offline", staged_game_dir=tmp_path))
    episode._offline_owner = owner
    owner._episodes.add(episode)
    directory = tempfile.TemporaryDirectory(prefix="arc-cleanup-test-", dir=tmp_path)
    episode._worker._directory = directory
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys; sys.stdin.buffer.read(1)",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=os.name != "nt",
    )
    episode._worker.process = process
    return owner, episode, process, Path(directory.name)


async def test_cancel_during_real_process_close_drains_and_unlinks_episode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, episode, process, directory = await _live_cleanup_episode(tmp_path)
    native_wait = process.wait
    entered, release = asyncio.Event(), asyncio.Event()

    async def gated_wait() -> int:
        entered.set()
        await release.wait()
        return await native_wait()

    monkeypatch.setattr(process, "wait", gated_wait)
    closing = asyncio.create_task(episode.close())
    try:
        async with asyncio.timeout(5):
            await entered.wait()
            # Cancellation lands while the *real* killed child is being reaped.
            closing.cancel()
            closing.cancel()  # A repeated request must not cancel the cleanup task.
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await closing
        assert process.returncode is not None
        assert not directory.exists()
        assert episode._worker._directory is None
        assert episode not in owner._episodes
        assert episode._cleanup_task is not None and episode._cleanup_task.done()
        assert episode._worker._cleanup_task is not None and episode._worker._cleanup_task.done()
        await episode.close()  # Concurrent/cancelled close memoizes a successful cleanup.
    finally:
        release.set()
        await owner.close()
        if process.returncode is None:
            process.kill()
            await native_wait()


async def test_expired_work_timeout_still_reaps_and_cleans_real_episode(tmp_path: Path) -> None:
    owner, episode, process, directory = await _live_cleanup_episode(tmp_path)
    try:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0):
                await episode.close()
        assert process.returncode is not None
        assert not directory.exists()
        assert not owner._episodes
        assert episode._closed
    finally:
        await owner.close()


async def test_cleanup_reap_grace_is_bounded_but_bookkeeping_always_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from architect.games import official

    owner, episode, process, directory = await _live_cleanup_episode(tmp_path)
    native_wait = process.wait
    never = asyncio.Event()

    async def stuck_wait() -> int:
        await never.wait()
        return await native_wait()

    monkeypatch.setattr(process, "wait", stuck_wait)
    monkeypatch.setattr(official, "_CLEANUP_REAP_TIMEOUT_S", 0.02)
    try:
        async with asyncio.timeout(2):
            with pytest.raises(GameSessionError, match="official_cleanup_failed"):
                await episode.close()
        assert episode._worker._cleanup_task is not None
        assert episode._worker._cleanup_task.done()
        assert not directory.exists()
        assert episode not in owner._episodes
        # A failed cleanup is never cached as a silently successful close.
        with pytest.raises(GameSessionError, match="official_cleanup_failed"):
            await episode.close()
    finally:
        never.set()
        await native_wait()
        await owner.close()


async def test_cancelled_provider_close_starts_all_episode_cleanup_concurrently(
    tmp_path: Path,
) -> None:
    provider = OfficialGameProvider(OfficialConfig(mode="offline", staged_game_dir=tmp_path))
    release = asyncio.Event()
    entered = [asyncio.Event(), asyncio.Event()]
    workers: list[Any] = []

    class ClosingWorker:
        def __init__(self, ready: asyncio.Event) -> None:
            self.ready = ready
            self.closed = False

        async def close(self) -> None:
            self.ready.set()
            await release.wait()
            self.closed = True

    for ready in entered:
        episode = OfficialGameProvider(OfficialConfig(mode="offline", staged_game_dir=tmp_path))
        episode._offline_owner = provider
        worker = ClosingWorker(ready)
        episode._worker = worker  # type: ignore[assignment]
        workers.append(worker)
        provider._episodes.add(episode)
    closing = asyncio.create_task(provider.close())
    try:
        async with asyncio.timeout(2):
            await asyncio.gather(*(ready.wait() for ready in entered))
            closing.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await closing
        assert all(worker.closed for worker in workers)
        assert not provider._episodes
        await provider.close()
    finally:
        release.set()
        if not closing.done():
            await closing


def test_single_game_scorecard_consistency_uses_documented_average() -> None:
    from architect.games._official_worker import _scorecard_scores

    # docs.arcprize.org/scorecards.md: score is the average of the best score for
    # each environment. One game therefore has identical aggregate/per-game score.
    card = {"card_id": "card", "score": 50, "environments": [{"id": "aa00-v1", "score": 25}]}
    with pytest.raises(GameProtocolError, match="official_scorecard_inconsistent"):
        _scorecard_scores(card, card_id="card", game_ids={"aa00-v1"})
    card["score"] = 25.0000000005  # Only floating arithmetic noise is tolerated.
    result = _scorecard_scores(card, card_id="card", game_ids={"aa00-v1"})
    assert result["card_score"] == 25.0000000005  # No score is reconstructed or replaced.
    assert result["game_scores"] == {"aa00-v1": 25}


async def test_parent_rejects_inconsistent_single_game_worker_result() -> None:
    class InconsistentWorker(MemoryWorker):
        async def request(self, request: dict[str, Any], maximum: int = 2_097_152) -> Any:
            result = await super().request(request, maximum)
            if request["op"] == "finalize":
                result["card_score"] = 99
            return result

    arcade = FakeArcade(("aa00-v1",))
    worker = InconsistentWorker(arcade)
    provider = OfficialGameProvider(OfficialConfig(mode="gateway", gateway_url="http://127.0.0.1"))
    provider._worker = worker  # type: ignore[assignment]
    session = await provider.open((await provider.discover())[0], limits=GameLimits())
    with pytest.raises(GameProtocolError, match="official_final_score"):
        await session.finalize()
    assert provider._score is None
    with pytest.raises(GameSessionError, match="official_finalization_failed"):
        await session.finalize()
    assert arcade.closes == 1
    await provider.close()


@pytest.mark.parametrize("cancel_request", [False, True])
async def test_close_or_cancel_during_real_spawn_never_sends_init_or_leaks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel_request: bool,
) -> None:
    from architect.games.official import _WorkerProcess

    worker = _WorkerProcess(OfficialConfig(mode="gateway", gateway_url="http://127.0.0.1"), None)
    native_spawn = asyncio.create_subprocess_exec
    entered, release, cleanup_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    spawned: list[asyncio.subprocess.Process] = []
    writes: list[bytes] = []
    original_cleanup = worker._cleanup

    async def observed_cleanup() -> None:
        cleanup_entered.set()
        await original_cleanup()

    async def gated_spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await native_spawn(*args, **kwargs)
        spawned.append(process)
        assert process.stdin is not None
        native_write = process.stdin.write

        def record_write(data: bytes) -> None:
            writes.append(data)
            native_write(data)

        monkeypatch.setattr(process.stdin, "write", record_write)
        entered.set()
        try:
            await release.wait()
            return process
        except asyncio.CancelledError:
            # This gate is test-only. Emulate native subprocess creation's own
            # transport cleanup if its startup coroutine is cancelled.
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise

    monkeypatch.setattr(asyncio, "create_subprocess_exec", gated_spawn)
    monkeypatch.setattr(worker, "_cleanup", observed_cleanup)
    request = asyncio.create_task(worker.request({"op": "init"}))
    closing: asyncio.Task[None] | None = None
    try:
        async with asyncio.timeout(5):
            await entered.wait()
            assert worker.process is None  # Real process exists, assignment is still pending.
            assert worker._directory is not None
            directory = Path(worker._directory.name)
            if cancel_request:
                request.cancel()
            else:
                closing = asyncio.create_task(worker.close())
            await cleanup_entered.wait()
            assert worker._closed
            release.set()
            if cancel_request:
                with pytest.raises(asyncio.CancelledError):
                    await request
            else:
                assert closing is not None
                await closing
                with pytest.raises(GameSessionError, match="official_worker_closed"):
                    await request
        assert spawned[0].returncode is not None
        assert not await asyncio.to_thread(directory.exists)
        assert writes == []  # No SDK/game init after the closed flag was set.
        assert worker._spawn_task is not None and worker._spawn_task.done()
        assert worker._cleanup_task is not None and worker._cleanup_task.done()
    finally:
        release.set()
        await worker.close()
        if not request.done():
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)


async def test_stalled_native_creation_is_cancelled_and_drained_within_cleanup_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from architect.games import official

    entered, never = asyncio.Event(), asyncio.Event()
    cancelled = False

    async def stalled_spawn(*args: Any, **kwargs: Any) -> Any:
        nonlocal cancelled
        entered.set()
        try:
            await never.wait()
        finally:
            cancelled = True

    monkeypatch.setattr(asyncio, "create_subprocess_exec", stalled_spawn)
    monkeypatch.setattr(official, "_CLEANUP_REAP_TIMEOUT_S", 0.1)
    worker = official._WorkerProcess(
        OfficialConfig(mode="gateway", gateway_url="http://127.0.0.1"),
        None,
    )
    request = asyncio.create_task(worker.request({"op": "init"}))
    async with asyncio.timeout(2):
        await entered.wait()
        assert worker._directory is not None
        directory = Path(worker._directory.name)
        await worker.close()
        with pytest.raises(asyncio.CancelledError):
            await request
    assert cancelled
    assert worker._spawn_task is not None and worker._spawn_task.done()
    assert worker._cleanup_task is not None and worker._cleanup_task.done()
    assert not await asyncio.to_thread(directory.exists)


@pytest.mark.parametrize("changes", [{"full_reset": True}, {"levels_completed": 0}])
def test_offline_driver_rejects_full_reset_and_level_regression(changes: dict[str, Any]) -> None:
    arcade = FakeArcade()
    arcade.env.observation_space.levels_completed = 1
    driver = _OfficialDriver(
        arcade, Action, OfficialConfig(mode="offline", staged_game_dir=Path("."))
    )
    driver.handle({"op": "make", "game_id": "aa00-v1", "limits": GameLimits().model_dump()})
    response = SimpleNamespace(**{**vars(arcade.env.observation_space), **changes})
    arcade.env.step = lambda action, data=None: response  # type: ignore[method-assign]
    with pytest.raises(GameProtocolError, match="^official_level_reset_required$"):
        driver.handle({"op": "step", "game_id": "aa00-v1", "action": {"name": "RESET"}})


def _offline_finalize(runs: list[Any]) -> Any:
    arcade = FakeArcade()
    arcade.runs = runs
    driver = _OfficialDriver(
        arcade, Action, OfficialConfig(mode="offline", staged_game_dir=Path("."))
    )
    driver.handle({"op": "make", "game_id": "aa00-v1", "limits": GameLimits().model_dump()})
    return driver.handle({"op": "finalize"})


@pytest.mark.parametrize("baselines", [None, [], [1], [1, 1, 1], [1, -1], [1, True]])
def test_offline_make_refuses_baselines_not_matching_win_levels(baselines: Any) -> None:
    arcade = FakeArcade()
    arcade.baselines = baselines
    driver = _OfficialDriver(
        arcade, Action, OfficialConfig(mode="offline", staged_game_dir=Path("."))
    )
    with pytest.raises(GameUnavailableError, match="^official_baseline_unavailable$"):
        driver.handle({"op": "make", "game_id": "aa00-v1", "limits": GameLimits().model_dump()})
    assert arcade.env.steps == []


def test_offline_finalize_rejects_best_of_runs_card() -> None:
    run = SimpleNamespace(message=None, level_baseline_actions=[1, 1])
    with pytest.raises(GameProtocolError, match="^official_multiple_runs$"):
        _offline_finalize([run, run])
    with pytest.raises(GameProtocolError, match="^official_multiple_runs$"):
        _offline_finalize([])
    assert _offline_finalize([run])["card_score"] == 25.0


@pytest.mark.parametrize(
    "run",
    [
        SimpleNamespace(message="Human baseline actions size mismatch", level_baseline_actions=[1]),
        SimpleNamespace(message=None, level_baseline_actions=[-1, -1]),
        SimpleNamespace(message=None, level_baseline_actions=[1, -1]),
        SimpleNamespace(message=None, level_baseline_actions=None),
        # Scored without a mismatch message, but not one baseline per level.
        SimpleNamespace(message=None, level_baseline_actions=[1]),
        SimpleNamespace(message=None, level_baseline_actions=[1, 1, 1]),
    ],
)
def test_offline_finalize_missing_baselines_is_unavailable_not_zero(run: Any) -> None:
    with pytest.raises(GameUnavailableError, match="^official_baseline_unavailable$"):
        _offline_finalize([run])


@pytest.mark.parametrize(
    ("error", "code"),
    [
        ('"official_baseline_unavailable"', "official_baseline_unavailable"),
        ('"official_multiple_runs"', "official_multiple_runs"),
        ('"official_level_reset_required"', "official_level_reset_required"),
        ('"official_operation_failed"', "official_operation_failed"),
        ('"Traceback: secret gateway text"', "official_operation_failed"),
        ("7", "official_operation_failed"),
    ],
)
async def test_parent_maps_worker_errors_to_fixed_unavailable_codes(
    error: str,
    code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from architect.games.official import _WorkerProcess

    payload = b'{"error":' + error.encode() + b"}"
    reader = asyncio.StreamReader()
    reader.feed_data(len(payload).to_bytes(4, "big") + payload)
    reader.feed_eof()

    class Writer:
        def write(self, _: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

    class Process:
        stdin = Writer()
        stdout = reader
        returncode: int | None = 0

    async def spawn(*args: Any, **kwargs: Any) -> Any:
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    worker = _WorkerProcess(OfficialConfig(mode="gateway", gateway_url="http://127.0.0.1"), None)
    with pytest.raises(GameUnavailableError, match=f"^{code}$"):
        await worker.request({"op": "finalize"})
    assert worker._closed


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"full_reset": True}, "official_level_reset_required"),
        ({"frame": [[[1] * 65]]}, "official_operation_failed"),
    ],
)
def test_worker_main_forwards_only_allowlisted_codes(
    changes: dict[str, Any],
    code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io
    import sys

    from architect.games import _official_worker

    arcade = FakeArcade()
    driver = _OfficialDriver(
        arcade, Action, OfficialConfig(mode="offline", staged_game_dir=Path("."))
    )
    monkeypatch.setattr(_official_worker, "_create_driver", lambda config: driver)
    response = SimpleNamespace(**{**vars(arcade.env.observation_space), **changes})
    arcade.env.step = lambda action, data=None: response  # type: ignore[method-assign]
    lines = [
        {"op": "init", "config": {"mode": "offline", "staged_game_dir": "."}},
        {"op": "make", "game_id": "aa00-v1", "limits": GameLimits().model_dump()},
        {"op": "step", "game_id": "aa00-v1", "action": {"name": "ACTION1"}},
    ]
    stdin = io.BytesIO(b"".join(json.dumps(line).encode() + b"\n" for line in lines))
    stdout = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=stdin))
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=stdout))
    _official_worker.main()
    data = stdout.getvalue()
    frames = []
    while data:
        length = int.from_bytes(data[:4], "big")
        frames.append(json.loads(data[4 : 4 + length]))
        data = data[4 + length :]
    assert len(frames) == 3 and "ok" in frames[1]
    assert frames[2] == {"error": code}


_TWO_LEVEL_GAME = """
from arcengine import ARCBaseGame, GameAction, Level, Sprite


class Ab12(ARCBaseGame):
    def __init__(self, seed: int = 0) -> None:
        levels = [Level(sprites=[Sprite([[n]], name="p")], grid_size=(4, 4)) for n in (1, 2)]
        super().__init__("ab12", levels, available_actions=[1, 2], seed=seed)

    def step(self) -> None:
        # ACTION1 clears the level; ACTION2 is a wasted move.
        if self.action.id == GameAction.ACTION1:
            self.next_level()
        self.complete_action()
"""


def _stage_two_level_game(root: Path, baselines: Any) -> Path:
    game = root / "ab12"
    game.mkdir(parents=True)
    metadata: dict[str, Any] = {"game_id": "ab12-v1", "class_name": "Ab12"}
    if baselines is not None:
        metadata["baseline_actions"] = baselines
    (game / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (game / "ab12.py").write_text(_TWO_LEVEL_GAME, encoding="utf-8")
    return root


async def _play_staged(root: Path, actions: list[str]) -> Any:
    provider = OfficialGameProvider(OfficialConfig(mode="offline", staged_game_dir=root))
    try:
        refs = await provider.discover()
        assert [ref.game_id for ref in refs] == ["ab12-v1"]
        session = await provider.open(refs[0], limits=GameLimits())
        for name in actions:
            await session.step(GameAction(name=name))  # type: ignore[arg-type]
        return await session.finalize()
    finally:
        await provider.close()


@sdk
async def test_real_offline_clean_perfect_play_scores(tmp_path: Path) -> None:
    root = _stage_two_level_game(tmp_path / "s", [1, 1])
    score = await _play_staged(root, ["ACTION1", "ACTION1"])
    assert score.source == "official" and score.raw == 100.0 and score.normalized == 1.0
    sloppy = await _play_staged(root, ["ACTION2", "ACTION1", "ACTION1"])
    assert sloppy.source == "official" and sloppy.raw is not None and sloppy.raw < 100.0


@sdk
async def test_real_offline_reset_after_level_transition_is_rejected(tmp_path: Path) -> None:
    root = _stage_two_level_game(tmp_path / "s", [1, 1])
    # Sloppy first level, then RESET right after the transition (a full reset
    # that opens a second SDK play), then a perfect replay: never best-of-runs.
    with pytest.raises(GameUnavailableError, match="^official_level_reset_required$"):
        await _play_staged(root, ["ACTION2", "ACTION1", "RESET", "ACTION1", "ACTION1"])


@sdk
@pytest.mark.parametrize(
    ("baselines", "actions"),
    [
        (None, ["ACTION1", "ACTION1"]),
        ([], ["ACTION1", "ACTION1"]),
        ([1], []),
        ([1], ["ACTION1"]),
        ([1], ["ACTION1", "ACTION1"]),
        ([1, 1, 1], ["ACTION1", "ACTION1"]),
    ],
)
async def test_real_offline_missing_baselines_are_unavailable(
    tmp_path: Path, baselines: Any, actions: list[str]
) -> None:
    # The SDK alone scores [1] with one level cleared as 100.0 on this
    # two-level game, and [1, 1, 1] as if a third level were uncleared.
    root = _stage_two_level_game(tmp_path / "s", baselines)
    with pytest.raises(GameUnavailableError, match="^official_baseline_unavailable$"):
        await _play_staged(root, actions)


@sdk
def test_real_offline_short_baseline_backstop_at_finalize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The finalize check alone refuses the SDK's inflated short-baseline score."""
    import logging

    import arc_agi
    import arcengine

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    root = _stage_two_level_game(tmp_path / "s", [1])
    logger = logging.getLogger("architect.test.official.backstop")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    arcade = arc_agi.Arcade(
        operation_mode=arc_agi.OperationMode.OFFLINE,
        environments_dir=str(root),
        recordings_dir=str(tmp_path / "recordings"),
        logger=logger,
    )
    driver = _OfficialDriver(
        arcade, arcengine.GameAction, OfficialConfig(mode="offline", staged_game_dir=root)
    )
    # Bypass only the make-time listing check; the SDK still scores with [1].
    driver.baselines["ab12-v1"] = [1, 1]
    limits = GameLimits().model_dump()
    driver.handle({"op": "make", "game_id": "ab12-v1", "limits": limits})
    step = {"op": "step", "game_id": "ab12-v1", "action": {"name": "ACTION1"}}
    assert driver.handle(step)["levels_completed"] == 1
    with pytest.raises(GameUnavailableError, match="^official_baseline_unavailable$"):
        driver.handle({"op": "finalize"})
