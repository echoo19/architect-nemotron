"""scripts/arc_harness.py: a thin CLI over architect.games.public with its old surface.

Windows refuses to delete a file that another handle still holds, so a SQLite
telemetry index or an official worker left open turns a finished run into a
``PermissionError`` from ``TemporaryDirectory`` cleanup with no summary printed.
These tests build the real factory (no games are played) and check the ordering,
and an ``--offline`` run over a cached fixture pins the argv and the summary keys
byte-for-byte to what the script printed before the lift.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from architect.games import public
from architect.games.competition import CompetitionDescription, CompetitionFactory
from architect.games.evaluation import OfficialPreviewManifest
from architect.games.types import GameLimits, GameRef
from architect.spec import Budget
from architect.telemetry import TelemetryStore

ROOT = Path(__file__).resolve().parents[1]
# The flags the script accepted before the lift, in order; the lift must keep all of them.
SCRIPT_FLAGS = (
    "--games",
    "--seed",
    "--cache",
    "--adapter",
    "--prices",
    "--call-timeout-s",
    "--num-ctx",
    "--think",
    "--model",
    "--floor-limit",
    "--cost-usd",
    "--compute-s",
    "--max-actions",
    "--out",
    "--offline",
)
# Summary keys, in print order, of a licensed and played run before the lift.
PLAYED_KEYS = [
    "seed",
    "adapter",
    "model_binding",
    "per_call_reservation_usd",
    "compliance",
    "arc_result",
    "score_meaning",
    "public_games",
    "games",
    "fresh_workspace",
    "spec_hash",
    "licensed",
    "compile_failures",
    "score_source",
    "revision",
    "accepted",
    "score",
    "metric",
    "children",
    "cost_usd",
    "tokens_in",
    "tokens_out",
    "wall_time_s",
    "run_failures",
    "child_failures",
]
UNLICENSED_KEYS = [*PLAYED_KEYS[: PLAYED_KEYS.index("score_source") + 1], "budget_note"]


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("arc_harness", ROOT / "scripts/arc_harness.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ref(index: int) -> GameRef:
    return GameRef(backend="official_offline", game_id=f"ga0{index}-00000{index}",
                   revision=f"00000{index}")


def description() -> CompetitionDescription:
    return CompetitionDescription(
        name="arc-agi-3-harness",
        rules="Public ARC-AGI-3 games, OFFLINE preview scoring. Not a submission.",
        action_schema={"type": "object"},
        observation_schema={"type": "object"},
        preview_dataset="arc-agi-3-public-parent",
        child_support_dataset="arc-agi-3-public-support",
        compute_cap=Budget(cost_usd=1.0, compute_s=60),
        floor_limit=0.0,
        small_model="fixture",
        submission_constraints=("Offline preview only; submission compliance unverified.",),
        max_games=5,
    )


def assert_released(workspace: Path, store: TelemetryStore) -> None:
    with pytest.raises(ValueError):
        with store:
            pass  # A closed store refuses reuse, so its SQLite handle is gone.
    shutil.rmtree(workspace)  # The real Windows check: nothing holds a file open.
    assert not workspace.exists()


def cached_games(cache: Path, count: int) -> None:
    for index in range(count):
        base, version = f"g{index:03d}", f"{index:06x}"
        directory = cache / base / version
        directory.mkdir(parents=True)
        metadata = {"game_id": f"{base}-{version}", "class_name": None, "baseline_actions": [4]}
        (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        (directory / f"{base}.py").write_text("# placeholder, never imported\n", encoding="utf-8")


def test_script_is_a_thin_cli_over_the_public_module() -> None:
    script = load_script()
    for name in ("FixtureAgent", "build", "close_workspace", "fetch_public_games", "stage"):
        assert getattr(script, name) is getattr(public, name)
    assert script.ROLES == public.ROLES and script.LOCAL_PRICES == public.LOCAL_PRICES
    assert script.ONLINE_ADAPTERS == public.ONLINE_ADAPTERS


def test_argv_surface_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = load_script()
    monkeypatch.setattr(sys, "argv", ["arc_harness.py", "--help"])
    with pytest.raises(SystemExit) as exit_info:
        script.main()
    assert exit_info.value.code == 0
    text = capsys.readouterr().out
    positions = [text.index(flag + " ") for flag in SCRIPT_FLAGS]
    assert positions == sorted(positions)
    assert "{fixture,anthropic,claude-code,ollama}" in text and "{on,off,default}" in text


def test_options_from_args_maps_every_flag() -> None:
    script = load_script()
    args = argparse.Namespace(
        games=6, seed=2, cache=Path("c"), adapter="ollama", prices=Path("p.json"),
        call_timeout_s=30.0, num_ctx=8192, think=True, model="gemma4:26b", floor_limit=0.5,
        cost_usd=2.0, compute_s=60.0, max_actions=9, offline=True, out=Path("o.json"),
    )
    options = script.options_from_args(args)
    assert options.model_dump() == {
        key: value for key, value in vars(args).items() if key != "out"
    }


async def test_close_workspace_releases_worker_and_telemetry_index(tmp_path: Path) -> None:
    script = load_script()
    workspace, staged = tmp_path / "ws", tmp_path / "games"
    staged.mkdir()
    roles = {role: (ref(index + 1),) for index, role in enumerate(script.ROLES)}
    manifest = OfficialPreviewManifest.model_validate(
        {"dataset_id": "arc-agi-3-public", "dataset_revision": "draw-1", **roles}
    )
    model = script.FixtureAgent()
    model.model = "fixture"
    store = TelemetryStore(workspace / "telemetry")
    factory = script.build(
        workspace, staged, manifest, description(), model,
        GameLimits(max_actions=4, max_turns=4), store=store,
    )
    assert isinstance(factory, CompetitionFactory)
    assert factory.compiler.runtime.store is store
    assert (workspace / "telemetry" / "index.sqlite3").is_file()
    assert (workspace / "registry").is_dir()

    await script.close_workspace(factory, store)
    await script.close_workspace(None, store)  # Idempotent, also before a factory exists.
    assert_released(workspace, store)


async def test_manufacture_closes_the_workspace_when_licensing_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = load_script()
    stores: list[TelemetryStore] = []

    class RecordingStore(TelemetryStore):
        def __init__(self, directory: Path) -> None:
            super().__init__(directory)
            stores.append(self)

    roles = {role: (ref(index + 1),) for index, role in enumerate(script.ROLES)}
    monkeypatch.setattr(public, "TelemetryStore", RecordingStore)
    monkeypatch.setattr(public, "fetch_public_games", lambda cache, *, offline: ([], {}))
    monkeypatch.setattr(public, "stage", lambda games, count, seed, staged: roles)

    async def refuse(self: CompetitionFactory, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("compile_stub")

    monkeypatch.setattr(CompetitionFactory, "compile", refuse)
    (tmp_path / "games").mkdir()
    args = argparse.Namespace(
        games=5, seed=1, cache=tmp_path / "cache", adapter="fixture", model="fixture",
        floor_limit=0.0, cost_usd=1.0, compute_s=60.0, max_actions=4, offline=True,
    )
    with pytest.raises(RuntimeError, match="compile_stub"):
        await script.manufacture(args, tmp_path)
    assert len(stores) == 1
    assert_released(tmp_path / "ws", stores[0])


def stub_factory(monkeypatch: pytest.MonkeyPatch, *, licensed: bool) -> None:
    """Replace licensing and play only; fetch, stage, build and cleanup stay real."""
    proposal = SimpleNamespace(approval_required=True, spec=object())
    compiled = SimpleNamespace(
        approval_required=False,
        spec=SimpleNamespace(spec_hash="a" * 64),
        artifact=SimpleNamespace(licensed=licensed),
        failures=() if licensed else ("game_budget_exceeded",),
    )

    async def compile_stub(
        self: CompetitionFactory, desc: Any, approved: bool = False, approved_spec: Any = None
    ) -> Any:
        return compiled if approved else proposal

    async def install(self: CompetitionFactory, result: Any, *, name: str) -> Any:
        return SimpleNamespace(revision=1)

    async def run(self: CompetitionFactory, name: str) -> Any:
        return SimpleNamespace(
            accepted=True, score=0.5, score_source="official_offline_preview",
            metric="normalized_preview_mean", children=(), cost_usd=0.0, tokens_in=10,
            tokens_out=10, wall_time_s=1.25, failures=(), model_binding="offline",
        )

    monkeypatch.setattr(CompetitionFactory, "compile", compile_stub)
    monkeypatch.setattr(CompetitionFactory, "install", install)
    monkeypatch.setattr(CompetitionFactory, "run", run)


@pytest.mark.parametrize("licensed", [True, False])
def test_offline_run_on_a_cached_fixture_keeps_summary_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    licensed: bool,
) -> None:
    script = load_script()
    cache, out = tmp_path / "cache", tmp_path / "summary.json"
    cached_games(cache, 6)
    stub_factory(monkeypatch, licensed=licensed)
    monkeypatch.setattr(
        sys,
        "argv",
        ["arc_harness.py", "--offline", "--games", "5", "--seed", "4", "--cache", str(cache),
         "--max-actions", "4", "--out", str(out)],
    )
    code = script.main()
    printed = capsys.readouterr().out
    summary = json.loads(printed)
    assert list(summary) == (PLAYED_KEYS if licensed else UNLICENSED_KEYS)
    assert json.loads(out.read_text(encoding="utf-8")) == summary
    assert code == (0 if licensed else 1)
    assert summary["adapter"] == "fixture" and summary["model_binding"] == "offline"
    assert summary["public_games"] == {"refreshed": False, "cached": 6}
    assert summary["score_meaning"] == "pipeline_check_only" and summary["seed"] == 4
    assert sorted(summary["games"]) == sorted(public.ROLES)
