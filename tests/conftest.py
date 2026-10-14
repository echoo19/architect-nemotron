"""Hermetic local authority for every test; never touch production credentials."""
import json
import os
from pathlib import Path
from typing import Any

import pytest

# W7: the bench host may export ARCHITECT_ED25519_KEY_FILE (the release key) for its own
# builds. Tests make their own keys, so neither that variable nor ARCHITECT_TRUST_FILE
# reaches this process, not even the module-scoped fixtures that build before the
# fixture below runs.
for _name in ("ARCHITECT_ED25519_KEY_FILE", "ARCHITECT_TRUST_FILE"):
    os.environ.pop(_name, None)


@pytest.fixture(autouse=True)
def isolated_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCHITECT_SIGNING_KEY_FILE", str(tmp_path / "authority.key"))
    # W7: the Ed25519 key and the trust list then default to the same temp directory,
    # where neither exists unless a test creates it.
    monkeypatch.delenv("ARCHITECT_ED25519_KEY_FILE", raising=False)
    monkeypatch.delenv("ARCHITECT_TRUST_FILE", raising=False)


def _staged_refs(staged: Path) -> list[dict[str, str]]:
    return [
        {
            "backend": "official_offline",
            "game_id": entry.name,
            "revision": entry.name.split("-", 1)[1],
        }
        for entry in sorted(staged.iterdir())
        if entry.is_dir()
    ]


class _ScriptedPublicWorker:
    """``official._WorkerProcess`` stand-in serving the staged public-game directory.

    Scripted wire only: no SDK, no game code, no official score evidence. ``init``
    lists the staged ``<game_id>`` directories; every game is the same tiny level:
    ACTION1 changes the frame and wins on the third press, ACTION2 is a no-op. Like
    the official metric the score rewards action efficiency: ``100 * 3 / actions``
    for a win (a no-op costs score), 0 otherwise.
    """

    def __init__(self, config: object, deadline: float | None) -> None:
        self.deadline = deadline
        self.target: str | None = None
        self.presses = 0
        self.actions = 0
        self.closed = False

    def _frame(self) -> dict[str, Any]:
        won = self.presses >= 3
        return {
            "frames": [[[self.presses % 16, 1], [0, 2]]],
            "state": "WIN" if won else "NOT_FINISHED",
            "available_actions": ["RESET"] if won else ["ACTION1", "ACTION2"],
            "levels_completed": int(won),
            "win_levels": 1,
        }

    async def request(self, request: dict[str, Any], maximum: int = 2_097_152) -> object:
        assert not self.closed
        op = request["op"]
        if op == "init":
            return _staged_refs(Path(request["config"]["staged_game_dir"]))
        if op == "finalize":
            assert self.target is not None
            score = 300 // max(3, self.actions) if self.presses >= 3 else 0
            return {"card_score": score, "game_scores": {self.target: score}}
        if op == "make":
            assert self.target is None
            self.target = str(request["game_id"])
        elif op == "step":
            self.actions += 1
            self.presses += request["action"]["name"] == "ACTION1"
        return self._frame()

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def scripted_public_games(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A cache of five tiny public games played over a scripted official OFFLINE wire.

    Returns the cache directory (``<base>/<version>/{metadata.json, <class>.py}``, the
    class file a placeholder that is never imported). The real ``OfficialGameProvider``
    runs against it; only its worker process is replaced.
    """
    from architect.games import official

    cache = tmp_path / "public-cache"
    for index in range(5):
        base, version = f"g{index:03d}", f"{index:06x}"
        directory = cache / base / version
        directory.mkdir(parents=True)
        metadata = {"game_id": f"{base}-{version}", "class_name": None, "baseline_actions": [3]}
        (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        (directory / f"{base}.py").write_text("# placeholder, never imported\n", encoding="utf-8")
    monkeypatch.setattr(official, "_WorkerProcess", _ScriptedPublicWorker)
    return cache
