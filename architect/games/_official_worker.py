"""Private process entry point; uses only public Arcade/environment game APIs.

Staged Python files are trusted public SDK resources, not an OS sandbox. No
hidden engine attributes, source, solutions, or intermediate scores are read
here. OFFLINE make only compares the public environment listing's baseline
count with the observation's ``win_levels``; OFFLINE finalize only checks the
completed card's public run count and baseline availability markers. Gateway
requests cannot use proxies, redirects or other origins.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import ipaddress
import json
import logging
import socket
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .official import (
    FORWARDED_WORKER_ERRORS,
    OfficialConfig,
    _validate_single_game_score,
    normalize_official_score,
)
from .types import (
    GameAction,
    GameLimits,
    GameProtocolError,
    GameRef,
    GameScore,
    GameUnavailableError,
    decode_observation,
)


class _TransportPolicy:
    def __init__(self, config: OfficialConfig) -> None:
        self.config = config
        self.limits = GameLimits()
        self.discovered_ids: tuple[str, ...] | None = None
        self.expected_game_id: str | None = None
        self.expected_card_id: str | None = None
        self.final_game_ids: set[str] = set()
        self.final_scores: dict[str, Any] | None = None

    def install(self) -> None:
        """Constrain the SDK's requests transport before importing/creating Arcade."""
        requests = importlib.import_module("requests")
        original = requests.sessions.Session.request
        config = self.config
        origin = urlsplit(config.gateway_url or "http://127.0.0.1")
        host, port = origin.hostname, origin.port or 80
        allowed: set[str] = set()
        if config.mode == "gateway":
            for entry in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
                address = str(entry[4][0])
                ip = ipaddress.ip_address(address)
                if not (ip.is_loopback or (host == "gateway" and ip.is_private)):
                    raise GameProtocolError("official_gateway_address")
                allowed.add(address)
        original_connect = socket.socket.connect
        original_connect_ex = socket.socket.connect_ex

        def check(address: Any) -> None:
            if (
                not isinstance(address, tuple)
                or len(address) < 2
                or address[0] not in allowed
                or address[1] != port
            ):
                raise OSError("official_network_denied")

        def connect(sock: socket.socket, address: Any) -> None:
            check(address)
            original_connect(sock, address)

        def connect_ex(sock: socket.socket, address: Any) -> int:
            check(address)
            return original_connect_ex(sock, address)

        socket.socket.connect = connect  # type: ignore[method-assign, assignment]
        socket.socket.connect_ex = connect_ex  # type: ignore[method-assign, assignment]
        policy = self

        def request(session: Any, method: str, url: str, **kwargs: Any) -> Any:
            target = urlsplit(url)
            if (
                config.mode != "gateway"
                or target.scheme != origin.scheme
                or target.hostname != host
                or (target.port or 80) != port
                or target.username
                or target.password
                or target.fragment
            ):
                raise GameProtocolError("official_network_denied")
            session.trust_env = False
            kwargs["proxies"] = {}
            kwargs["allow_redirects"] = False
            kwargs["stream"] = True
            kwargs["timeout"] = config.operation_timeout_s
            response = original(session, method, url, **kwargs)
            try:
                if 300 <= response.status_code < 400:
                    raise GameProtocolError("official_redirect_denied")
                length = response.headers.get("Content-Length")
                if length is not None and int(length) > policy.limits.max_payload_bytes:
                    raise GameProtocolError("official_payload_limit")
                content = bytearray()
                for chunk in response.iter_content(chunk_size=4096):
                    if len(content) + len(chunk) > policy.limits.max_payload_bytes:
                        raise GameProtocolError("official_payload_limit")
                    content.extend(chunk)
                # Cache bounded bytes for the SDK's normal Response.json() call.
                response._content = bytes(content)
                if response.status_code == 200:
                    value = json.loads(content)
                    if target.path == "/api/scorecard/close":
                        if policy.expected_card_id is None:
                            raise GameProtocolError("official_final_score")
                        # Validate original JSON before SDK Pydantic can coerce
                        # bool/string scores to floats or drop summary fields.
                        policy.final_scores = _scorecard_scores(
                            value,
                            card_id=policy.expected_card_id,
                            game_ids=policy.final_game_ids,
                        )
                    elif target.path == "/api/games":
                        policy.discovered_ids = _game_ids(value)
                    elif target.path.startswith(("/api/games/", "/api/cmd/")):
                        if value.get("game_id") != policy.expected_game_id:
                            raise GameProtocolError("official_game_identity")
                        if target.path.startswith("/api/cmd/"):
                            _wire_observation(value, policy.limits)
                return response
            finally:
                response.close()

        requests.sessions.Session.request = request


def _scorecard_scores(
    value: Any,
    *,
    card_id: str,
    game_ids: set[str],
) -> dict[str, Any]:
    """Read exact documented fields from a completed official scorecard only."""
    if not isinstance(value, dict) or value.get("card_id") != card_id:
        raise GameProtocolError("official_scorecard_identity")
    try:
        card_score = normalize_official_score(value["score"])
        environments = value["environments"]
        if not isinstance(environments, list) or not 1 <= len(environments) <= 1024:
            raise GameProtocolError("official_scorecard_coverage")
        games: dict[str, GameScore] = {}
        for environment in environments:
            if not isinstance(environment, dict):
                raise GameProtocolError("official_scorecard_coverage")
            game_id = environment["id"]
            if not isinstance(game_id, str) or game_id in games:
                raise GameProtocolError("official_scorecard_coverage")
            games[game_id] = normalize_official_score(environment["score"])
        if set(games) != game_ids:
            raise GameProtocolError("official_scorecard_coverage")
        _validate_single_game_score(card_score, games)
        return {
            "card_score": card_score.raw,
            "game_scores": {game_id: score.raw for game_id, score in games.items()},
        }
    except (KeyError, TypeError):
        raise GameProtocolError("official_final_score") from None


def _check_baselines(baselines: Any, win_levels: Any) -> None:
    """Require one published, non-negative baseline per scored game level.

    The SDK only flags a baseline list shorter than the levels *completed*; it
    otherwise averages over the listed levels alone. A short list would drop
    uncleared levels from the score, and a long one would add phantom levels.
    """
    if (
        type(win_levels) is not int
        or win_levels < 1
        or not isinstance(baselines, list)
        or len(baselines) != win_levels
        or any(type(item) is not int or item < 0 for item in baselines)
    ):
        raise GameUnavailableError("official_baseline_unavailable")


def _check_offline_runs(environments: Any, win_levels: dict[str, int | None]) -> None:
    """Accept exactly one play per game with published human baselines.

    ``EnvironmentScoreList.score`` is the best of its runs, so a second run is a
    replay, not one episode. A run message or a ``-1`` baseline entry means the
    SDK scored 0.0 because baselines were missing, which is not an official 0.0.
    A baseline count other than the game's ``win_levels`` is also unavailable.
    """
    for environment in environments:
        runs = environment.runs
        if len(runs) != 1:
            raise GameProtocolError("official_multiple_runs")
        run = runs[0]
        if run.message is not None:
            raise GameUnavailableError("official_baseline_unavailable")
        _check_baselines(run.level_baseline_actions, win_levels.get(environment.id))


def _game_ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 1024:
        raise GameProtocolError("official_discovery")
    ids = tuple(item["game_id"] for item in value)
    for game_id in ids:
        _ref(game_id, "gateway")
    if len(set(ids)) != len(ids):
        raise GameProtocolError("official_discovery")
    return ids


def _ref(game_id: str, mode: str) -> GameRef:
    # SDK documents everything after '-' as the public game version.
    if not isinstance(game_id, str) or "-" not in game_id:
        raise GameProtocolError("official_unversioned_game")
    _, version = game_id.split("-", 1)
    return GameRef(
        backend="official_offline" if mode == "offline" else "official_gateway",
        game_id=game_id,
        revision=version,
    )


def _wire_observation(value: Any, limits: GameLimits) -> dict[str, Any]:
    """Validate JSON before the SDK narrows integers into int8 numpy frames."""
    actions = value["available_actions"]
    if not isinstance(actions, list) or len(actions) > 8:
        raise GameProtocolError("official_actions")
    names = []
    for action in actions:
        if type(action) is not int or not 0 <= action <= 7:
            raise GameProtocolError("official_actions")
        names.append("RESET" if action == 0 else f"ACTION{action}")
    if value["state"] == "GAME_OVER":
        names = ["RESET"]
    elif "RESET" not in names:
        names.insert(0, "RESET")
    result = {
        "frames": value["frame"],
        "state": value["state"],
        "available_actions": names,
        "levels_completed": value["levels_completed"],
        "win_levels": value.get("win_levels"),
    }
    return decode_observation(json.dumps(result).encode(), limits).model_dump(mode="json")


class _OfficialDriver:
    """Test seam accepts trusted SDK-shaped objects, never model capabilities."""

    def __init__(
        self,
        arcade: Any,
        action_type: Any,
        config: OfficialConfig,
        policy: _TransportPolicy | None = None,
    ) -> None:
        self.arcade = arcade
        self.action_type = action_type
        self.config = config
        self.policy = policy
        environments = arcade.get_environments()
        if not 1 <= len(environments) <= 1024:
            raise GameProtocolError("official_discovery")
        self.refs = tuple(_ref(env.game_id, config.mode) for env in environments)
        # Public listing metadata; only its count and validity are checked.
        self.baselines = {
            env.game_id: getattr(env, "baseline_actions", None) for env in environments
        }
        ids = tuple(ref.game_id for ref in self.refs)
        if len(set(ids)) != len(ids):
            raise GameProtocolError("official_discovery")
        if policy is not None and config.mode == "gateway":
            if policy.discovered_ids is None or set(ids) != set(policy.discovered_ids):
                raise GameProtocolError("official_incomplete_discovery")
        self.card: str | None = None
        if config.mode == "gateway":
            self.card = self._open_card()
        self.environments: dict[str, Any] = {}
        self.limits: dict[str, GameLimits] = {}
        self.win_levels: dict[str, int | None] = {}
        self.attempted: set[str] = set()
        self.closed = False

    def _open_card(self) -> str:
        card = self.arcade.open_scorecard()
        if not isinstance(card, str) or not card:
            raise GameProtocolError("official_scorecard_missing")
        return card

    def observation(self, env: Any, response: Any, limits: GameLimits) -> dict[str, Any]:
        if response is None:
            raise GameProtocolError("official_observation_missing")
        frames = response.frame
        if not 1 <= len(frames) <= limits.max_frames:
            raise GameProtocolError("official_frame_limit")
        output = []
        for frame in frames:
            # Check numpy shape before .tolist() allocates Python cell objects.
            if hasattr(frame, "shape"):
                shape = frame.shape
                if len(shape) != 2 or not all(1 <= dim <= 64 for dim in shape):
                    raise GameProtocolError("official_frame_shape")
                frame = frame.tolist()
            if not 1 <= len(frame) <= 64 or any(not 1 <= len(row) <= 64 for row in frame):
                raise GameProtocolError("official_frame_shape")
            output.append(frame)
        actions = env.action_space
        if len(actions) > 8:
            raise GameProtocolError("official_actions")
        return _wire_observation(
            {
                "frame": output,
                "state": response.state.name,
                "available_actions": [action.value for action in actions],
                "levels_completed": response.levels_completed,
                "win_levels": response.win_levels,
            },
            limits,
        )

    def handle(self, request: dict[str, Any]) -> Any:
        if self.closed:
            raise GameProtocolError("official_scorecard_closed")
        op = request["op"]
        if op == "make":
            game_id = request["game_id"]
            if game_id not in {r.game_id for r in self.refs} or game_id in self.attempted:
                raise GameProtocolError("official_single_make")
            if self.config.mode == "offline" and self.attempted:
                raise GameProtocolError("official_single_episode")
            self.attempted.add(game_id)
            if self.card is None:
                self.card = self._open_card()
            if self.policy is not None:
                self.policy.expected_game_id = game_id
            limits = GameLimits.model_validate(request["limits"])
            if self.policy is not None:
                self.policy.limits = limits
            env = self.arcade.make(game_id, scorecard_id=self.card, save_recording=False)
            if env is None:
                raise GameProtocolError("official_make_failed")
            observation = self.observation(env, env.observation_space, limits)
            win_levels = observation["win_levels"]
            if self.config.mode == "offline":
                # Refuse before gameplay spends actions on an unscorable game.
                _check_baselines(self.baselines.get(game_id), win_levels)
            self.win_levels[game_id] = win_levels
            self.environments[game_id] = env
            self.limits[game_id] = limits
            return observation
        if op == "step":
            game_id = request["game_id"]
            env = self.environments[game_id]
            if self.policy is not None:
                self.policy.expected_game_id = game_id
            limits = self.limits[game_id]
            if self.policy is not None:
                self.policy.limits = limits
            action = GameAction.model_validate(request["action"])
            data = {"x": action.x, "y": action.y} if action.name == "ACTION6" else None
            # RESET goes through step, preserving the existing gateway game GUID.
            previous = env.observation_space
            response = env.step(self.action_type[action.name], data=data)
            # A full reset opens a new SDK play, and OFFLINE scores keep the best
            # play. Reject it in both modes so a replay never becomes the score.
            if response is not None and (
                response.full_reset or response.levels_completed < previous.levels_completed
            ):
                raise GameProtocolError("official_level_reset_required")
            return self.observation(env, response, limits)
        if op == "finalize":
            if self.config.mode == "gateway":
                if set(self.environments) != {r.game_id for r in self.refs}:
                    raise GameProtocolError("official_incomplete_coverage")
            elif len(self.environments) != 1:
                raise GameProtocolError("official_incomplete_episode")
            self.closed = True
            assert self.card is not None
            if self.policy is not None:
                self.policy.limits = GameLimits()
                self.policy.expected_card_id = self.card
                self.policy.final_game_ids = set(self.environments)
                self.policy.final_scores = None
            result = self.arcade.close_scorecard(self.card)
            if result is None:
                raise GameProtocolError("official_final_score_missing")
            if self.config.mode == "gateway" and self.policy is not None:
                if self.policy.final_scores is None:
                    raise GameProtocolError("official_final_score_missing")
                return self.policy.final_scores
            # OFFLINE SDK computes this completed-card public object. Never
            # inspect private engine state or reconstruct the metric from moves.
            _check_offline_runs(result.environments, self.win_levels)
            return _scorecard_scores(
                {
                    "card_id": result.card_id,
                    "score": result.score,
                    "environments": [
                        {"id": env.id, "score": env.score} for env in result.environments
                    ],
                },
                card_id=self.card,
                game_ids=set(self.environments),
            )
        raise GameProtocolError("official_operation")


def _create_driver(config: OfficialConfig) -> _OfficialDriver:
    if (
        importlib.metadata.version("arc-agi") != config.sdk_version
        or importlib.metadata.version("arcengine") != config.engine_version
    ):
        raise GameProtocolError("official_sdk_version")
    policy = _TransportPolicy(config)
    policy.install()
    sdk = importlib.import_module("arc_agi")
    engine = importlib.import_module("arcengine")
    logger = logging.getLogger("architect.official.silent")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    staged = str(config.staged_game_dir) if config.staged_game_dir else str(Path.cwd() / "empty")
    arcade = sdk.Arcade(
        operation_mode=sdk.OperationMode.OFFLINE
        if config.mode == "offline"
        else sdk.OperationMode.COMPETITION,
        environments_dir=staged,
        arc_base_url=(config.gateway_url or "http://127.0.0.1").rstrip("/"),
        arc_api_key="1234",
        recordings_dir=str(Path.cwd() / "recordings"),
        logger=logger,
    )
    return _OfficialDriver(arcade, engine.GameAction, config, policy)


def main() -> None:
    driver: _OfficialDriver | None = None
    output = sys.stdout.buffer
    # SDK/game prints are not protocol and must not leak to host telemetry.
    sys.stdout = sys.stderr
    for _ in range(1_000_000):
        line = sys.stdin.buffer.readline(2_097_153)
        if not line:
            return
        try:
            if len(line) > 2_097_152 or not line.endswith(b"\n"):
                raise GameProtocolError("official_request_limit")
            request = json.loads(line)
            if driver is None:
                if request["op"] != "init":
                    raise GameProtocolError("official_init_required")
                driver = _create_driver(OfficialConfig.model_validate(request["config"]))
                result = [ref.model_dump() for ref in driver.refs]
            else:
                result = driver.handle(request)
            payload = json.dumps({"ok": result}, allow_nan=False).encode()
            if len(payload) > 16_777_216:
                raise GameProtocolError("official_payload_limit")
        except Exception as error:
            # Only fixed, allowlisted codes raised by this adapter cross the pipe.
            code = "official_operation_failed"
            if (
                type(error) in (GameProtocolError, GameUnavailableError)
                and len(error.args) == 1
                and error.args[0] in FORWARDED_WORKER_ERRORS
            ):
                code = error.args[0]
            payload = json.dumps({"error": code}).encode()
            output.write(len(payload).to_bytes(4, "big") + payload)
            output.flush()
            return
        output.write(len(payload).to_bytes(4, "big") + payload)
        output.flush()


if __name__ == "__main__":
    main()
