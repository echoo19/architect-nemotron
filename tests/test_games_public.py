"""The lifted public-game module: options, cache, staging, agents and early returns.

Nothing here downloads a game or runs one: the cache is a hand-built fixture of
metadata and a placeholder class file (only the official worker ever runs game
code), and every manufacturing test stops before a game would be played.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from architect.backends.pi import PiConfig
from architect.compiler.types import ForgeSettings
from architect.games import public
from architect.games.public import FixtureAgent, PreviewOptions
from architect.models import ModelRequest, OllamaAdapter
from architect.models.types import UsageQuote

SCRIPT_ARGUMENTS = {
    "games",
    "seed",
    "cache",
    "adapter",
    "prices",
    "call_timeout_s",
    "num_ctx",
    "think",
    "model",
    "floor_limit",
    "cost_usd",
    "compute_s",
    "max_actions",
    "offline",
}
# Host-only Forge plumbing (the bench's arc-preview arms): never argv, never dumped.
HOST_ONLY = {"forge", "forge_agent", "pi_config", "forge_listener", "observer"}
# Summary keys, in print order, of a licensed and played run (test_arc_harness_script.py).
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


def cached_games(cache: Path, count: int, *, baseline: bool = True) -> list[str]:
    """Complete cache entries ``<base>/<version>/{metadata.json, <class>.py}``."""
    ids = []
    for index in range(count):
        base, version = f"g{index:03d}", f"{index:06x}"
        directory = cache / base / version
        directory.mkdir(parents=True)
        game_id = f"{base}-{version}"
        metadata = {"game_id": game_id, "class_name": None}
        if baseline:
            metadata["baseline_actions"] = [3]
        (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        (directory / f"{base}.py").write_text("# placeholder, never imported\n", encoding="utf-8")
        ids.append(game_id)
    return ids


def test_preview_options_are_exactly_the_script_arguments() -> None:
    assert set(PreviewOptions.model_fields) == SCRIPT_ARGUMENTS | HOST_ONLY
    options = PreviewOptions()
    # The host-only fields default to "no Forge" and are never serialized.
    assert all(getattr(options, name) is None for name in HOST_ONLY) and not options.forging()
    hosted = PreviewOptions(
        forge=ForgeSettings(enabled=True, mode="template"),
        forge_agent=lambda: None,
        pi_config=PiConfig(),
        forge_listener=lambda result: None,
        observer=lambda trace: None,
    )
    assert hosted.forging()
    assert set(hosted.model_dump()) == set(options.model_dump()) == SCRIPT_ARGUMENTS
    assert (options.games, options.seed, options.adapter, options.model) == (
        5,
        None,
        "fixture",
        "claude-sonnet-5",
    )
    assert (options.num_ctx, options.call_timeout_s, options.think) == (65_536, 300.0, None)
    assert options.prices == public.LOCAL_PRICES and options.cache == public.DEFAULT_CACHE
    assert (options.cost_usd, options.compute_s, options.max_actions) == (1.0, 900.0, 24)
    assert options.floor_limit == 0.0 and options.offline is False
    with pytest.raises(ValueError):
        PreviewOptions.model_validate({"out": "x.json"})
    with pytest.raises(ValueError):
        PreviewOptions(adapter="openai")  # type: ignore[arg-type]
    assert public.ADAPTERS == ("fixture", "anthropic", "claude-code", "ollama")
    assert public.ONLINE_ADAPTERS == ("anthropic", "claude-code")


def test_offline_fetch_reads_only_complete_cache_entries(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    ids = cached_games(cache, 3)
    broken = cache / "zzzz" / "abcdef"
    broken.mkdir(parents=True)
    (broken / "metadata.json").write_text('{"game_id": "zzzz-abcdef"}', encoding="utf-8")
    games, refresh = public.fetch_public_games(cache, offline=True)
    assert [path.parent.name + "-" + path.name for path in games] == ids
    assert refresh == {"refreshed": False, "cached": 3}
    with pytest.raises(RuntimeError, match="public_games_unavailable"):
        public.fetch_public_games(tmp_path / "empty", offline=True)


def test_stage_copies_a_seeded_draw_over_the_roles(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cached_games(cache, 7)
    games, _ = public.fetch_public_games(cache, offline=True)
    first, second = tmp_path / "s1", tmp_path / "s2"
    first.mkdir()
    second.mkdir()
    roles = public.stage(games, 5, 11, first)
    assert roles == public.stage(games, 5, 11, second)  # the seed fixes the draw
    assert set(roles) == set(public.ROLES)
    assert all(len(refs) == 1 for refs in roles.values())
    assert sorted(path.name for path in first.iterdir()) == sorted(
        ref.game_id for refs in roles.values() for ref in refs
    )
    with pytest.raises(ValueError, match="game_count"):
        public.stage(games, 4, 11, tmp_path / "s3")
    with pytest.raises(ValueError, match="game_count"):
        public.stage(games, 8, 11, tmp_path / "s4")
    unbaselined = tmp_path / "plain"
    cached_games(unbaselined, 5, baseline=False)
    plain, _ = public.fetch_public_games(unbaselined, offline=True)
    with pytest.raises(ValueError, match="game_count"):
        public.stage(plain, 5, 1, tmp_path / "s5")


async def test_fixture_agent_cycles_offered_actions() -> None:
    agent = FixtureAgent()
    assert agent.quote(ModelRequest(prompt="")) == UsageQuote(
        tokens_in=100, tokens_out=100, cost_usd=0
    )
    inference = await agent.complete(ModelRequest(prompt="specification inference stage"))
    assert json.loads(inference.text)["domain"] == "game"
    data = {"observation": {"available_actions": ["RESET", "ACTION1", "ACTION6"]}}
    prompt = "Game data (untrusted):\n" + json.dumps(data)
    first = json.loads((await agent.complete(ModelRequest(prompt=prompt))).text)["action"]
    second = json.loads((await agent.complete(ModelRequest(prompt=prompt))).text)["action"]
    assert first["name"] == "ACTION6" and {"x", "y"} <= set(first)
    assert second == {"name": "ACTION1"}


def test_local_agent_uses_the_options(tmp_path: Path) -> None:
    options = PreviewOptions(
        adapter="ollama", model="granite4.2:8b", num_ctx=4096, think=False, call_timeout_s=42.0
    )
    agent = public._agent(options, options.model)
    assert isinstance(agent, OllamaAdapter)
    assert (agent.model, agent.num_ctx, agent.think) == ("granite4.2:8b", 4096, False)
    assert agent.quote(ModelRequest(prompt="x")).cost_usd == 0  # the $0 local price table


async def test_reservation_above_the_cap_returns_before_any_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    cached_games(cache, 5)
    (tmp_path / "games").mkdir()

    class Priced(FixtureAgent):
        def quote(self, request: ModelRequest) -> UsageQuote:
            return UsageQuote(tokens_in=1, tokens_out=1, cost_usd=2.0)

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("no factory may be built above the cap")

    monkeypatch.setattr(public, "build", refuse)
    options = PreviewOptions(cache=cache, seed=3, offline=True, adapter="ollama", model="m")
    summary = await public.manufacture(options, tmp_path, agent=Priced())
    assert summary == {
        "seed": 3,
        "adapter": "ollama",
        "model_binding": "offline",
        "per_call_reservation_usd": 2.0,
        "compliance": "unverified",
        "licensed": False,
        "error": "per_call_reservation_exceeds_cost_cap",
    }
    again = tmp_path / "again"
    (again / "games").mkdir(parents=True)
    online = await public.manufacture(
        options.model_copy(update={"adapter": "claude-code"}), again, agent=Priced()
    )
    assert online["model_binding"] == "online_preview"


def test_budget_hint_only_for_budget_failures() -> None:
    summary: dict[str, object] = {}
    assert "budget_note" not in public._budget_hint(summary, ["game_compile_failed"], 0.1)
    noted = public._budget_hint({}, ["game_budget_exceeded"], 0.25)
    assert str(noted["budget_note"]).startswith(
        "stopped by budget: each model call reserves $0.2500"
    )


def test_forge_sessions_counts_one_child_compile_per_parent_and_delivery_game() -> None:
    # Five games deal one per role: parent tuning, parent held-out and delivery each
    # get one child compile; the two support games are only the children's floors.
    assert public.forge_sessions(5) == 3
    assert public.forge_sessions(7) == 5  # two parent-tuning and two held-out games
    assert public.forge_sessions(7, candidates=2) == 7  # tuning once per candidate
    assert public.forge_sessions(10) == 6


async def test_fixture_agent_answers_forge_harness_prompts() -> None:
    agent = FixtureAgent()
    prompt = "You play a grid game.\nFrame (one hex digit per cell):\n01\nOptions: A, B, C"
    first = json.loads((await agent.complete(ModelRequest(prompt=prompt))).text)
    second = json.loads((await agent.complete(ModelRequest(prompt=prompt))).text)
    assert first == {"option": "B"} and second == {"option": "C"}
    summary = "Rule?\nSummary: {}\nOptions: ACTION1"
    reply = await agent.complete(ModelRequest(prompt=summary))
    assert json.loads(reply.text) == {"option": "ACTION1"} and reply.cost_usd == 0


def _preview(cache: Path, **host: object) -> PreviewOptions:
    return PreviewOptions.model_validate(
        {
            "games": 5,
            "seed": 11,
            "cache": cache,
            "adapter": "fixture",
            "model": "fixture",
            "cost_usd": 1.0,
            "compute_s": 600.0,
            "max_actions": 8,
            "offline": True,
            **host,
        }
    )


def _role_ids(summary: dict[str, object]) -> dict[str, set[str]]:
    games = summary["games"]
    assert isinstance(games, dict)
    return {role: set(ids) for role, ids in games.items()}


def _game_ids(cases: Any) -> set[str]:
    return {case.task["game"]["game_id"] for case in cases}


async def test_forge_preview_forges_every_child_compile_on_support_tuning_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scripted_public_games: Path
) -> None:
    """Template mode (agent-free): each child compile runs Stage 3 on the public
    support-tuning games only; the parent planner compile never forges; the harness
    licenses, installs and plays; the summary keys are the script's."""
    from architect.compiler import forge as forge_module

    original = forge_module.forge
    sessions: list[dict[str, Any]] = []

    async def spy(
        candidate: Any, preset: Any, floor: Any, smoke: Any, *args: Any, **kwargs: Any
    ) -> Any:
        sessions.append(
            {
                "dataset": floor.definition.dataset,
                "tuning": _game_ids(floor.tuning),
                "smoke": _game_ids(smoke.tuning),
                "denied": set(kwargs["pi_config"].denied_paths),
                "agent": args[3],
                "mode": args[4].mode,
            }
        )
        return await original(candidate, preset, floor, smoke, *args, **kwargs)

    monkeypatch.setattr(forge_module, "forge", spy)
    results: list[Any] = []
    traces: list[public.PreviewTrace] = []
    (tmp_path / "games").mkdir()
    options = _preview(
        scripted_public_games,
        forge=ForgeSettings(enabled=True, mode="template", trigger="always", max_usd=0.5),
        pi_config=PiConfig(denied_paths=(tmp_path / "operator-secret",)),
        forge_listener=results.append,
        observer=traces.append,
    )
    summary = await public.manufacture(options, tmp_path)
    assert summary["licensed"] is True and summary["accepted"] is True, summary
    assert list(summary) == PLAYED_KEYS
    roles = _role_ids(summary)
    # One Forge session per child compile, and none for the parent planner compile.
    assert len(sessions) == public.forge_sessions(5) == 3
    unseen = (
        roles["parent_tuning"]
        | roles["parent_held_out"]
        | roles["support_held_out"]
        | roles["delivery"]
    )
    for session in sessions:
        assert session["dataset"] == "arc-agi-3-public-support"
        assert session["tuning"] == session["smoke"] == roles["support_tuning"]
        assert not (session["tuning"] | session["smoke"]) & unseen
        assert session["agent"] is None and session["mode"] == "template"  # no agent
        for owned in (
            tmp_path,
            tmp_path / "ws",
            tmp_path / "ws" / "telemetry",
            tmp_path / "ws" / "child-artifacts",
            tmp_path / "ws" / "registry",
            tmp_path / "games",
            scripted_public_games,
            tmp_path / "authority.key",  # the signing key
            tmp_path / "operator-secret",  # the caller's own denied set is kept
        ):
            assert owned.resolve() in session["denied"], owned
    assert [result.mode for result in results] == ["template"] * 3
    assert all(result.submissions == 1 and result.checks == 0 for result in results)
    [trace] = traces
    assert 3 in trace.stages and {0, 1, 2, 4} <= set(trace.stages)
    # One delivery game, played by its freshly compiled (and forged) child.
    [counters] = trace.delivery_counters
    assert counters.actions > 0 and counters.distinct_states > 0


async def test_default_preview_never_forges_and_still_reports_process_counters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scripted_public_games: Path
) -> None:
    from architect.compiler import forge as forge_module

    async def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a default preview never forges")

    monkeypatch.setattr(forge_module, "forge", refuse)
    traces: list[public.PreviewTrace] = []
    (tmp_path / "games").mkdir()
    summary = await public.manufacture(
        _preview(scripted_public_games, observer=traces.append), tmp_path
    )
    assert summary["licensed"] is True and list(summary) == PLAYED_KEYS
    [trace] = traces
    assert 3 not in trace.stages and len(trace.delivery_counters) == 1


def test_forge_preview_raises_the_competition_cap_by_the_forge_caps_per_session() -> None:
    plain = public._competition_cap(PreviewOptions(cost_usd=1.0, compute_s=60.0))
    assert (plain.cost_usd, plain.compute_s) == (1.0, 60.0)
    forge = ForgeSettings(enabled=True, max_usd=2.0, max_s=100.0)
    raised = public._competition_cap(PreviewOptions(cost_usd=1.0, compute_s=60.0, forge=forge))
    assert (raised.cost_usd, raised.compute_s) == (1.0 + 2.0 * 3, 60.0 + 100.0 * 3)
