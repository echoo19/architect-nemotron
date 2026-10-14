"""Phase 9 bench arms ``forge`` and ``template`` (contract Sections 13 and 15.2).

One module-scoped fake-provider smoke run (``default``, ``template`` and ``forge``,
seed 7, four cases per text task) goes through the real Compiler, the real Forge
driver and, for the ``forge`` arm, the real pinned Pi SDK driven by the committed
``ForgeScript`` agent. Everything is $0 and offline: no live provider, no CLI model
and no Ollama.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from architect import cli
from architect.bench import BenchRow, read_rows
from architect.bench.oracle import FORGE_EXAMPLES, ForgeScript
from architect.bench.render import bench_table
from architect.bench.runner import (
    BenchConfig,
    bound,
    check_max_usd,
    forge_settings,
    plan,
    process_metrics,
    run_bench,
)
from architect.bench.suite import SUITES
from architect.compiler.types import ForgeSettings
from architect.models import AdapterError, ModelRequest

TASKS = {task.id: task for task in SUITES["v1"]}
SIZE = 4
# Columns that legitimately differ between two identical runs.
_VOLATILE = {"bench_id", "timestamp", "wall_s"}


def _config(out: Path, *variants: str, **changes: Any) -> BenchConfig:
    return BenchConfig(
        tier="smoke",
        provider="fake",
        seed=7,
        size=SIZE,
        variants=variants,  # type: ignore[arg-type]
        out=out,
        **changes,
    )


HAS_PILLOW = importlib.util.find_spec("PIL") is not None
# The smoke tier renders scanned exam pages with the optional bench extra (Pillow).
needs_pillow = pytest.mark.skipif(not HAS_PILLOW, reason="the bench extra (Pillow) is absent")


@pytest.fixture(scope="module")
def arms(tmp_path_factory: pytest.TempPathFactory) -> list[BenchRow]:
    root = tmp_path_factory.mktemp("frg")
    config = _config(root / "out", "default", "template", "forge")
    result = asyncio.run(run_bench(config, workspace_root=root / "ws"))
    assert read_rows(result.rows_path) == list(result.rows)
    return list(result.rows)


def _row(rows: list[BenchRow], task: str, variant: str) -> BenchRow:
    [row] = [item for item in rows if item.task_id == task and item.variant == variant]
    return row


def _stable(row: BenchRow) -> dict[str, Any]:
    data = row.model_dump(mode="json")
    for key in _VOLATILE:
        data.pop(key)
    if data["forge"] is not None:
        data["forge"].pop("agent_wall_s")
    return data


# ------------------------------------------------------------------ rows


@needs_pillow
def test_forge_and_template_rows_carry_forge_fields(arms: list[BenchRow]) -> None:
    for row in arms:
        if row.variant == "default":
            assert row.forge is None
            continue
        forge = row.forge
        assert forge is not None and forge.language == 2
        assert forge.mode == ("agent" if row.variant == "forge" else "template")
        if row.variant == "template":
            assert forge.agent_model is None and forge.agent_tokens_in == 0
            assert forge.checks == 0 and forge.measures == 0 and forge.agent_cost_usd == 0.0
        else:
            assert forge.agent_model == "fake"
        # The fake provider's Forge agent is $0: build-time money is recorded apart.
        assert forge.agent_cost_usd == 0.0


def test_text_arms_actually_forged_through_stage_3(arms: list[BenchRow]) -> None:
    for task in ("sentiment-24", "tickets-40", "invoice-40"):
        forge = _row(arms, task, "forge")
        template = _row(arms, task, "template")
        default = _row(arms, task, "default")
        assert 3 in forge.stages and 3 in template.stages and 3 not in default.stages
        assert forge.forge is not None and forge.forge.measures >= 1
        assert forge.forge.submissions >= 1 and forge.forge.checks >= 1
        # The real Pi session metered the scripted agent's turns (tokens, never capped).
        assert forge.forge.agent_tokens_in > 0 and forge.forge.agent_tokens_out > 0
        assert template.forge is not None and template.forge.submissions >= 1
        assert "forge_did_not_run" not in forge.failures + template.failures
        # Stage 4 stays the only licence authority; the arms are the same measurement.
        assert forge.licensed and template.licensed and default.licensed
        assert forge.spec_hash == template.spec_hash == default.spec_hash


@needs_pillow
def test_the_guard_marks_every_row_where_forge_did_not_run(arms: list[BenchRow]) -> None:
    for row in arms:
        marked = "forge_did_not_run" in row.failures
        if row.variant == "forge":
            assert row.forge is not None and marked == (row.forge.measures == 0)
        elif row.variant == "template":
            assert row.forge is not None and marked == (row.forge.submissions == 0)
        else:
            assert not marked


def test_arc_process_metrics_on_default_rows(arms: list[BenchRow]) -> None:
    row = _row(arms, "arc-local-3", "default")
    arc = row.arc
    assert arc is not None and arc.games == 3
    assert arc.distinct_states_mean is not None and arc.distinct_states_mean > 0
    assert arc.revisit_rate_mean is not None and 0.0 <= arc.revisit_rate_mean <= 1.0
    assert arc.levels_completed_total is not None and arc.levels_completed_total >= 0
    assert arc.calls_per_action is not None and arc.calls_per_action >= 0
    # Every arm gets the metrics, not only default.
    for variant in ("template", "forge"):
        other = _row(arms, "arc-local-3", variant).arc
        assert other is not None and other.distinct_states_mean is not None


def test_bench_table_process_block_fits_and_is_content_free(arms: list[BenchRow]) -> None:
    text = bench_table(arms)
    assert text.isascii() and "\x1b" not in text
    assert all(len(line) <= 100 for line in text.splitlines())
    assert "process (forge counts are tuning evidence, not a license)" in text
    header = next(line for line in text.splitlines() if line.startswith("task") and "meas" in line)
    for column in ("meas", "subs", "agent$", "dist", "revis"):
        assert column in header
    assert " tmpl " in text and " forge " in text
    # A table of pre-Phase-9 rows is unchanged: no process block without the columns.
    plain = [row.model_copy(update={"arc": None}) for row in arms if row.variant == "default"]
    assert "process" not in bench_table(plain)


def test_smoke_forge_and_template_are_deterministic(
    arms: list[BenchRow], tmp_path: Path
) -> None:
    config = _config(tmp_path / "out", "template", "forge")
    again = asyncio.run(run_bench(config, workspace_root=tmp_path / "ws"))
    first = [_stable(row) for row in arms if row.variant != "default"]
    assert first == [_stable(row) for row in again.rows]
    assert all(row.cost_usd >= 0 and row.evidence == "synthetic_fake" for row in again.rows)


# ------------------------------------------------------------------ plan and bound


def test_every_task_supports_the_forge_arms_with_the_default_spec() -> None:
    for task in SUITES["v1"]:
        assert task.supports("forge") and task.supports("template")
        default = task.spec("fake", "default")
        assert task.spec("fake", "forge").spec_hash == default.spec_hash
        assert task.spec("fake", "template").spec_hash == default.spec_hash
    cells = plan(_config(Path("unused"), "default", "forge"), ("fake",))
    assert {cell.variant for cell in cells} == {"default", "forge"}


def test_forge_settings_per_variant() -> None:
    base = ForgeSettings(max_usd=2.0, max_s=300.0, agent_model="fake", trigger="failed")
    config = _config(Path("unused"), "default", "forge", "template", forge=base)
    assert forge_settings(config, "default") is None
    assert forge_settings(config, "noisy") is None
    agent = forge_settings(config, "forge")
    template = forge_settings(config, "template")
    assert agent is not None and template is not None
    assert agent.enabled and agent.trigger == "always" and agent.mode == "agent"
    assert template.enabled and template.trigger == "always" and template.mode == "template"
    assert agent.max_usd == 2.0 and agent.max_s == 300.0 and agent.agent_model == "fake"
    defaults = forge_settings(_config(Path("unused"), "forge"), "forge")
    assert defaults is not None and defaults.max_usd == ForgeSettings().max_usd


@needs_pillow
def test_max_usd_bound_includes_the_forge_caps_up_front() -> None:
    forge = ForgeSettings(max_usd=0.5)
    by_variant = {}
    for variant in ("default", "template", "forge"):
        limits = bound(_config(Path("unused"), variant, forge=forge))
        by_variant[variant] = {cell.task_id: cell.reserved_usd for cell in limits.cells}
    for task_id, reserved in by_variant["default"].items():
        # A template cell adds $0 beyond the normal compile.
        assert by_variant["template"][task_id] == pytest.approx(reserved)
        extra = forge.max_usd * (3 if task_id == "arc-local-3" else 1)
        assert by_variant["forge"][task_id] == pytest.approx(reserved + extra)
    limits = bound(_config(Path("unused"), "forge", forge=forge))
    capped = _config(Path("unused"), "forge", forge=forge, max_usd=limits.total_usd - 0.01)
    from architect.bench import BenchError

    with pytest.raises(BenchError, match="bench_over_max_usd"):
        check_max_usd(capped, bound(capped))


# ------------------------------------------------------------------ the guard and CLI exit


def test_cli_exits_nonzero_when_forge_did_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # No committed script for any task: the Forge agent cannot be built, the Pi session
    # never measures, and every forge row is marked instead of reported as Forge.
    empty = tmp_path / "noscripts"
    (empty / "scripts").mkdir(parents=True)

    def missing(task_id: str, *, model: str = "fake", root: Path | None = None) -> ForgeScript:
        return ForgeScript(task_id, model=model, root=empty)

    monkeypatch.setattr("architect.bench.runner.ForgeScript", missing)
    code = cli.main(
        [
            "bench", "run", "--tier", "quick", "--provider", "fake", "--variant", "forge",
            "--size", "4", "--presets", "extractor", "--out", str(tmp_path / "out"), "--json",
        ]
    )
    captured = capsys.readouterr()
    assert code == 1
    assert json.loads(captured.err.strip().splitlines()[-1]) == {"error": "forge_did_not_run"}
    rows = read_rows(Path(json.loads(captured.out)["rows_path"]))
    assert rows and all("forge_did_not_run" in row.failures for row in rows)
    assert all(row.forge is not None and row.forge.measures == 0 for row in rows)


def test_cli_bench_forge_flags_build_the_config() -> None:
    args = cli._parser().parse_args(
        [
            "bench", "run", "--tier", "smoke", "--provider", "fake", "--variant", "forge",
            "--variant", "template", "--forge-usd", "2.5", "--forge-minutes", "5",
            "--forge-model", "fake",
        ]
    )
    config = cli._bench_config(args)
    assert config.variants == ("forge", "template")
    assert config.forge == ForgeSettings(max_usd=2.5, max_s=300.0, agent_model="fake")
    plain = cli._bench_config(
        cli._parser().parse_args(["bench", "run", "--tier", "smoke", "--provider", "fake"])
    )
    assert plain.forge is None and plain.variants == ("default", "noisy")


@pytest.mark.parametrize(
    ("extra", "code", "status"),
    [
        (["--variant", "forged"], "bench_variant_unknown", 1),
        # The same fixed code as on compile/do/create/recompile (Section 12.4).
        (["--variant", "forge", "--forge-usd", "0"], "forge_budget_invalid", 1),
        (["--variant", "forge", "--forge-usd", "1001"], "forge_budget_invalid", 1),
        (["--variant", "forge", "--forge-usd", "nan"], "forge_budget_invalid", 1),
        (["--variant", "forge", "--forge-minutes", "0"], "forge_budget_invalid", 1),
        (["--variant", "forge", "--forge-minutes", "100000"], "forge_budget_invalid", 1),
    ],
)
def test_cli_bench_forge_flags_are_validated(
    extra: list[str], code: str, status: int, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["bench", "run", "--tier", "smoke", "--provider", "fake", *extra]
    assert cli.main(argv) == status
    assert json.loads(capsys.readouterr().err) == {"error": code}


# ------------------------------------------------------------------ profile Forge settings


def _forge_profile(tmp_path: Path, name: str = "profile.json", **forge: Any) -> Path:
    """A fake-provider profile whose Forge agent differs from the play model."""
    payload: dict[str, Any] = {
        "workspace": "w",
        "models": {"fake": {"provider": "fake"}, "llama3.2": {"provider": "fake"}},
        "forge_agent": {"provider": "fake", "model": "gpt-4o-mini"},
        "compile": {"forge": {"max_usd": 3.0, "max_s": 600.0, "max_measures": 5, **forge}},
    }
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _profile_config(profile: Path, *variants: str, **changes: Any) -> BenchConfig:
    return BenchConfig(
        tier="smoke",
        provider="profile",
        profile=profile,
        models=("fake",),
        seed=7,
        size=SIZE,
        variants=variants,  # type: ignore[arg-type]
        out=profile.parent / "out",
        **changes,
    )


def test_profile_run_takes_forge_settings_from_the_profile(tmp_path: Path) -> None:
    from architect.profiles import load_profile

    path = _forge_profile(tmp_path, agent_model="llama3.2", trigger="failed")
    profile = load_profile(path)
    settings = forge_settings(_profile_config(path, "forge"), "forge", profile)
    assert settings is not None
    # The profile's compile.forge is the base; the cell still forges every compile.
    assert (settings.max_usd, settings.max_s, settings.max_measures) == (3.0, 600.0, 5)
    assert settings.agent_model == "llama3.2"
    assert settings.enabled and settings.trigger == "always" and settings.mode == "agent"
    # Only the --forge-* flags given override it; the rest stays the profile's.
    flags = ForgeSettings.model_validate({"max_usd": 1.5})
    changed = forge_settings(_profile_config(path, "forge", forge=flags), "forge", profile)
    assert changed is not None
    assert (changed.max_usd, changed.max_s, changed.agent_model) == (1.5, 600.0, "llama3.2")
    # The fake provider never reads a profile's Forge settings.
    fake = forge_settings(_config(Path("unused"), "forge"), "forge", profile)
    assert fake == forge_settings(_config(Path("unused"), "forge"), "forge")


def test_profile_run_uses_the_profile_forge_agent_and_its_cap(tmp_path: Path) -> None:
    from architect.bench import runner
    from architect.models.pricing import load_prices
    from architect.profiles import load_profile

    path = _forge_profile(tmp_path)  # no agent_model: the forge_agent is the agent
    profile = load_profile(path)
    task = TASKS["sentiment-24"]
    cell = runner._Cell(0, task, "fake", "forge", SIZE)
    ctx = runner._Context(
        _profile_config(path, "forge"),
        profile=profile,
        factory=None,
        prices=load_prices(),
        clock=lambda: 0.0,
    )
    tally = ctx.forge_tally(cell)
    assert tally is not None and tally.agent_model == "gpt-4o-mini"
    agent = ctx.forge_agent(cell, tally)
    assert agent is not None and agent.model == "gpt-4o-mini"  # never the play model
    assert tally.fields().agent_model == "gpt-4o-mini"
    assert runner._forge_cap(ctx, cell) == 3.0  # the profile's cap, reserved up front
    # agent_model naming a models key selects that model instead.
    other = _forge_profile(tmp_path, "named.json", agent_model="llama3.2")
    ctx = runner._Context(
        _profile_config(other, "forge"),
        profile=load_profile(other),
        factory=None,
        prices=load_prices(),
        clock=lambda: 0.0,
    )
    tally = ctx.forge_tally(cell)
    assert tally is not None and tally.agent_model == "llama3.2"
    agent = ctx.forge_agent(cell, tally)
    assert agent is not None and agent.model == "llama3.2"
    # Template cells have no agent and record none.
    template = ctx.forge_tally(runner._Cell(0, task, "fake", "template", SIZE))
    assert template is not None and template.fields().agent_model is None
    assert ctx.forge_agent(cell, template) is None


def test_stage_b_profile_names_the_claude_cli_as_the_forge_agent() -> None:
    from architect.bench.runner import check_forge_agent, forge_agent_model
    from architect.profiles import load_profile

    path = Path(__file__).resolve().parents[1] / "examples" / "local" / "profile-forge.json"
    profile = load_profile(path)
    flags = ForgeSettings.model_validate({"max_usd": 5.0, "max_s": 3600.0})
    config = BenchConfig(
        tier="full",
        provider="profile",
        profile=path,
        models=("granite4.2:8b",),
        variants=("default", "template", "forge"),
        arc_actions=80,
        repeats=3,
        forge=flags,
    )
    settings = forge_settings(config, "forge", profile)
    assert settings is not None and settings.agent_model == "claude-sonnet-5"
    assert forge_agent_model(config, profile, settings, "granite4.2:8b") == "claude-sonnet-5"
    assert profile.forge_agent is not None and profile.forge_agent.provider == "claude-code"
    check_forge_agent(config, profile)  # nothing is built or called


def test_unknown_forge_model_on_a_profile_bench_is_refused_up_front(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from architect.bench.runner import check_forge_agent
    from architect.profiles import ProfileError, load_profile

    path = _forge_profile(tmp_path)
    profile = load_profile(path)
    unknown = ForgeSettings.model_validate({"agent_model": "nope"})
    for variant in ("forge", "template"):
        with pytest.raises(ProfileError, match="forge_model_unknown"):
            check_forge_agent(_profile_config(path, variant, forge=unknown), profile)
    with pytest.raises(ProfileError, match="forge_model_unknown"):
        bound(_profile_config(path, "forge", forge=unknown), profile=profile)
    # A default-only run and the forge_agent model itself are fine.
    check_forge_agent(_profile_config(path, "default", forge=unknown), profile)
    known = ForgeSettings.model_validate({"agent_model": "gpt-4o-mini"})
    check_forge_agent(_profile_config(path, "forge", forge=known), profile)
    # A profile's own unknown agent_model is refused the same way.
    bad = _forge_profile(tmp_path, "bad.json", agent_model="nope")
    with pytest.raises(ProfileError, match="forge_model_unknown"):
        check_forge_agent(_profile_config(bad, "forge"), load_profile(bad))
    out = tmp_path / "out"
    argv = [
        "bench", "run", "--tier", "smoke", "--provider", "profile", "--profile", str(path),
        "--models", "fake", "--variant", "forge", "--forge-model", "nope", "--out", str(out),
        "--json",
    ]
    assert cli.main(argv) == 1
    assert json.loads(capsys.readouterr().err.strip().splitlines()[-1]) == {
        "error": "forge_model_unknown"
    }
    assert not out.exists()  # refused before any cell or row


def test_cli_compare_pair_variants(
    arms: list[BenchRow], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from architect.bench import write_rows

    path = tmp_path / "rows.jsonl"
    write_rows(path, arms)
    argv = ["bench", "compare", str(path), str(path), "--json"]
    assert cli.main([*argv, "--pair-variants", "default:forge"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pair_variants"] == ["default", "forge"]
    assert {cell["task_id"] for cell in payload["cells"]} == {row.task_id for row in arms}
    assert cli.main([*argv[:-1], "--pair-variants", "template:forge"]) == 0
    assert "paired variants: template (base) -> forge (candidate)" in capsys.readouterr().out
    assert cli.main([*argv, "--pair-variants", "default"]) == 2
    assert json.loads(capsys.readouterr().err) == {"error": "invalid_arguments"}
    assert cli.main([*argv, "--pair-variants", "default:nope"]) == 1
    assert json.loads(capsys.readouterr().err) == {"error": "bench_variant_unknown"}


# ------------------------------------------------------------------ ForgeScript and metrics


def _request() -> ModelRequest:
    return ModelRequest(prompt="{}")


async def test_forge_script_replays_then_ends_and_costs_nothing(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "h.py").write_text("async def run(context, task):\n    return task\n", "utf-8")
    script = [
        {"tool_calls": [{"id": "w", "name": "write",
                         "arguments": {"path": "harness.py", "content_from": "h.py"}}]},
        {"tool_calls": [{"id": "c", "name": "forge_check", "arguments": {}}]},
        {"text": "submitting", "tool_calls": [
            {"id": "s", "name": "forge_submit", "arguments": {}}]},
    ]
    (tmp_path / "scripts" / "sentiment-24.json").write_text(json.dumps(script), "utf-8")
    agent = ForgeScript("sentiment-24", root=tmp_path)
    texts = []
    for _ in range(5):
        quote = agent.quote(_request())
        reply = await agent.complete(_request())
        assert quote.cost_usd == reply.cost_usd == 0.0
        assert (quote.tokens_in, quote.tokens_out) == (reply.tokens_in, reply.tokens_out)
        texts.append(reply.text)
    first = json.loads(texts[0])
    arguments = first["tool_calls"][0]["arguments"]
    assert arguments == {"path": "harness.py", "content": (tmp_path / "h.py").read_text()}
    assert json.loads(texts[2])["text"] == "submitting"
    assert texts[3:] == ["done", "done"]  # final plain text ends the Pi session
    assert agent.calls == 5 and agent.model == "fake"


@pytest.mark.parametrize(
    "script",
    [
        [],
        {"steps": []},
        [{"tool_calls": [{"id": "x", "name": "write", "arguments": {"content_from": "../x"}}]}],
        [{"tool_calls": [{"id": "x", "name": "write",
                          "arguments": {"content_from": "h.py", "content": "both"}}]}],
        [{"text": "ok", "extra": 1}],
        [{"tool_calls": [{"id": "x", "name": "write", "arguments": []}]}],
    ],
)
def test_forge_script_rejects_invalid_scripts(tmp_path: Path, script: object) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "h.py").write_text("x", "utf-8")
    (tmp_path.parent / "x").write_text("outside", "utf-8")
    (tmp_path / "scripts" / "t.json").write_text(json.dumps(script), "utf-8")
    with pytest.raises(AdapterError):
        ForgeScript("t", root=tmp_path)
    with pytest.raises(AdapterError):
        ForgeScript("missing", root=tmp_path)


def test_committed_scripts_load_for_every_task() -> None:
    for task_id in TASKS:
        if (FORGE_EXAMPLES / "scripts" / f"{task_id}.json").is_file():
            assert ForgeScript(task_id).task_id == task_id


def test_process_metrics() -> None:
    def counters(actions: int, distinct: int, revisits: int, calls: int, levels: int) -> object:
        return SimpleNamespace(
            actions=actions,
            distinct_states=distinct,
            revisits=revisits,
            model_calls=calls,
            levels_completed=levels,
        )

    metrics = process_metrics([counters(10, 6, 4, 10, 1), counters(20, 20, 0, 4, 0)])
    assert metrics == {
        "distinct_states_mean": 13.0,
        "revisit_rate_mean": pytest.approx(0.2),
        "levels_completed_total": 1,
        "calls_per_action": pytest.approx(14 / 30),
    }
    empty = dict.fromkeys(metrics)
    assert process_metrics([]) == empty
    assert process_metrics([SimpleNamespace(actions=-1)]) == empty
    idle = process_metrics([counters(0, 1, 0, 0, 0)])
    assert idle["revisit_rate_mean"] == 0.0 and idle["calls_per_action"] is None


# ------------------------------------------------------------------ review round 3 regressions


@pytest.mark.parametrize(
    ("forge_model", "code"),
    [("x y", "bench_model_unconfigured"), ("nope", "forge_model_unknown")],
)
def test_fake_bench_refuses_a_bad_forge_model_before_any_spend(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], forge_model: str, code: str
) -> None:
    """Regression: a name ``ForgeFields.agent_model`` cannot hold (or the fake provider
    does not know) was accepted, every cell compiled and forged, and only then did
    ``tally.fields()`` raise, turning every row into compilation_failed."""
    progress: list[str] = []
    out = tmp_path / "out"
    argv = [
        "bench", "run", "--tier", "quick", "--provider", "fake", "--variant", "forge",
        "--size", "4", "--presets", "extractor", "--forge-model", forge_model,
        "--out", str(out), "--json",
    ]
    assert cli.main(argv) == 1
    captured = capsys.readouterr()
    progress.extend(line for line in captured.err.splitlines() if "stage" in line)
    assert json.loads(captured.err.strip().splitlines()[-1]) == {"error": code}
    assert progress == [] and not out.exists()  # refused before any cell or row


def test_check_forge_agent_on_non_profile_runs() -> None:
    from architect.bench import BenchError
    from architect.bench.runner import check_forge_agent
    from architect.models import FakeAdapter
    from architect.profiles import ProfileError

    def config(name: str, *variants: str) -> BenchConfig:
        forge = ForgeSettings.model_validate({"agent_model": name})
        return _config(Path("unused"), *variants, forge=forge)

    check_forge_agent(config("llama3.2", "forge"), None)  # a priced fake model is fine
    check_forge_agent(config("x y", "default"), None)  # no Forge arm: nothing to check
    with pytest.raises(BenchError, match="bench_model_unconfigured"):
        check_forge_agent(config("x y", "template"), None)
    with pytest.raises(ProfileError, match="forge_model_unknown"):
        check_forge_agent(config("nope", "forge"), None)
    # An injected factory decides its own names; only the row-safe shape is required.
    check_forge_agent(config("nope", "forge"), None, factory=lambda model: FakeAdapter())
    with pytest.raises(BenchError, match="bench_model_unconfigured"):
        check_forge_agent(config("x y", "forge"), None, factory=lambda model: FakeAdapter())


def _preview_config(cache: Path, out: Path, *variants: str, **changes: Any) -> BenchConfig:
    return BenchConfig(
        tier="arc",
        provider="fake",
        arc_cache=cache,
        out=out,
        variants=variants,  # type: ignore[arg-type]
        **changes,
    )


def test_preview_bound_includes_the_forge_cap_per_child_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the arc-preview bound printed $0 for a forge arm. A forge cell now
    reserves ``forge.max_usd`` per child compile the manufacture can open (each child
    forges at most once, the parent never), as the other tiers do; template adds $0."""
    from architect.bench import runner
    from architect.games import public

    monkeypatch.setattr(runner, "_has_module", lambda name: True)
    forge = ForgeSettings(max_usd=0.5)
    config = _preview_config(
        tmp_path / "cache", tmp_path / "out", "default", "template", "forge", forge=forge
    )
    cells = {cell.variant: cell for cell in bound(config).cells}
    assert all(cell.code is None and cell.calls > 0 for cell in cells.values())
    # The fixture player is $0, so the play wallets reserve nothing: only Forge does.
    assert cells["default"].reserved_usd == cells["template"].reserved_usd == 0.0
    sessions = public.forge_sessions(config.arc_games)
    assert sessions == 3
    assert cells["forge"].reserved_usd == pytest.approx(forge.max_usd * sessions)
    larger = _preview_config(
        tmp_path / "cache", tmp_path / "out", "forge", forge=forge, arc_games=10
    )
    [cell] = bound(larger).cells
    assert cell.reserved_usd == pytest.approx(forge.max_usd * public.forge_sessions(10))
    # --max-usd sees the Forge caps: below them the forge cell is skipped before spend,
    # and a forge-only run is refused.
    capped = config.model_copy(update={"max_usd": forge.max_usd * sessions - 0.01})
    skipped = {cell.variant: cell.code for cell in bound(capped).cells}
    assert skipped == {"default": None, "template": None, "forge": "over_max_usd"}
    alone = capped.model_copy(update={"variants": ("forge",)})
    from architect.bench import BenchError

    with pytest.raises(BenchError, match="bench_over_max_usd"):
        check_max_usd(alone, bound(alone))


def test_preview_forge_and_template_arms_forge_the_child_compiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scripted_public_games: Path
) -> None:
    """The real public ARC path (``bench run --tier arc``, task arc-preview) over a
    staged tiny public-game fixture on a scripted official OFFLINE wire, $0.

    Regression: forge and template preview cells ended forge_did_not_run because
    ``public.manufacture`` never enabled Forge. Now every child game compile runs
    Stage 3 (template: the agent-free template harness; forge: the replayed
    ForgeScript through the real Pi SDK), on the public support-tuning games only,
    and the rows carry ForgeFields and process metrics.
    """
    from architect.bench import runner
    from architect.compiler import forge as forge_module
    from architect.games import public

    monkeypatch.setattr(runner, "_has_module", lambda name: True)
    original = forge_module.forge
    seen: list[tuple[str, set[str], set[str], object]] = []

    async def spy(
        candidate: Any, preset: Any, floor: Any, smoke: Any, *args: Any, **kwargs: Any
    ) -> Any:
        tuning = {case.task["game"]["game_id"] for case in floor.tuning}
        smoked = {case.task["game"]["game_id"] for case in smoke.tuning}
        seen.append((args[4].mode, tuning, smoked, args[3]))
        return await original(candidate, preset, floor, smoke, *args, **kwargs)

    monkeypatch.setattr(forge_module, "forge", spy)
    config = _preview_config(
        scripted_public_games, tmp_path / "out", "default", "template", "forge", arc_actions=8
    )
    result = asyncio.run(run_bench(config, workspace_root=tmp_path / "ws"))
    rows = {row.variant: row for row in result.rows}
    assert set(rows) == {"default", "template", "forge"} and result.skipped == ()
    sessions = public.forge_sessions(config.arc_games)
    # The same seeded draw the cells staged: Forge only ever saw support tuning.
    games, _ = public.fetch_public_games(scripted_public_games, offline=True)
    (tmp_path / "draw").mkdir()
    roles = public.stage(games, config.arc_games, config.seed, tmp_path / "draw")
    support = {ref.game_id for ref in roles["support_tuning"]}
    assert [mode for mode, *_ in seen] == ["template"] * sessions + ["agent"] * sessions
    agents = [agent for mode, _, _, agent in seen if mode == "agent"]
    assert len({id(agent) for agent in agents}) == sessions  # a fresh agent per child
    assert all(agent is None for mode, _, _, agent in seen if mode == "template")
    for _, tuning, smoked, _ in seen:
        assert tuning == smoked == support
    default = rows["default"]
    assert default.licensed and default.forge is None and 3 not in default.stages
    assert default.failures == ()
    for variant in ("template", "forge"):
        row = rows[variant]
        assert "forge_did_not_run" not in row.failures, row.failures
        assert row.licensed and 3 in row.stages and {0, 1, 2, 4} <= set(row.stages)
        forge = row.forge
        assert forge is not None and forge.language == 2
        assert forge.submissions == sessions  # every child compile forged once
        # Each child promoted its Forge harness (more action-efficient than the preset
        # on support tuning), so the delivered harness is a Forge sandbox artifact: it
        # played only because the child factory carries allow_forge.
        assert forge.accepted == sessions and forge.harness_nodes > 0
        assert forge.harness_bytes > 0 and forge.subharnesses == 0
        assert forge.agent_cost_usd == 0.0 and row.cost_usd == 0.0
        assert row.spec_hash == default.spec_hash
    template, forged = rows["template"].forge, rows["forge"].forge
    assert template is not None and forged is not None
    assert template.mode == "template" and template.agent_model is None
    assert template.measures == 0 and template.agent_tokens_in == 0
    assert forged.mode == "agent" and forged.agent_model == "fake"
    assert forged.measures == sessions and forged.checks >= sessions
    # The real Pi session metered the scripted agent's turns.
    assert forged.agent_tokens_in > 0 and forged.agent_tokens_out > 0
    # ARC process metrics from the delivery plays' host counters, on every arm.
    for row in rows.values():
        arc = row.arc
        assert arc is not None and arc.games == config.arc_games
        assert arc.distinct_states_mean is not None and arc.distinct_states_mean > 0
        assert arc.revisit_rate_mean is not None and 0.0 <= arc.revisit_rate_mean <= 1.0
        assert arc.levels_completed_total is not None and arc.levels_completed_total >= 0
        assert arc.calls_per_action is not None
    # Exploration state lives in the forged code: fewer model calls per action.
    base = rows["default"].arc
    assert base is not None and base.calls_per_action is not None
    for variant in ("template", "forge"):
        arc = rows[variant].arc
        assert arc is not None and arc.calls_per_action is not None
        assert arc.calls_per_action < base.calls_per_action
    from architect.bench.ci_assert import _forge_checks

    assert _forge_checks(list(result.rows)) == []


def test_free_memory_probe_reports_this_host() -> None:
    from architect.bench.runner import free_memory_bytes

    free = free_memory_bytes()
    assert free is None or (type(free) is int and free > 0)


def test_forge_game_cells_refuse_with_host_memory_low(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (Section 14 host rules): under 6 GB free RAM a forge or template game
    cell of a run that can load a local model refuses before anything runs."""
    from architect.bench import CELL_CODES, runner
    from architect.models.pricing import load_prices
    from architect.profiles import load_profile

    assert "host_memory_low" in CELL_CODES
    started: list[str] = []

    async def never(*args: object, **kwargs: object) -> None:
        started.append("ran")

    monkeypatch.setattr(runner, "_local_games_cell", never)
    free = {"bytes": 1 * 1024**3}
    monkeypatch.setattr(runner, "free_memory_bytes", lambda: free["bytes"])
    path = _forge_profile(tmp_path)
    profile = load_profile(path)
    task = TASKS["arc-local-3"]

    def run(variant: str, config: BenchConfig) -> BenchRow:
        ctx = runner._Context(
            config, profile=profile, factory=None, prices=load_prices(), clock=lambda: 0.0
        )
        cell = runner._Cell(0, task, "fake", variant, 3)  # type: ignore[arg-type]
        sized = runner._size_cell(ctx, cell)
        totals = runner._Totals(total=1)
        return asyncio.run(
            runner._run_cell(ctx, runner._Identity(config, profile), sized, totals, tmp_path)
        )

    for variant in ("forge", "template"):
        row = run(variant, _profile_config(path, variant))
        assert row.failures == ("host_memory_low", "forge_did_not_run")
        assert row.cost_usd == 0.0 and not row.licensed
    assert started == []
    # The default arm, and any cell once memory is back above the floor, is not refused.
    row = run("default", _profile_config(path, "default"))
    assert "host_memory_low" not in row.failures
    free["bytes"] = 7 * 1024**3
    row = run("forge", _profile_config(path, "forge"))
    assert "host_memory_low" not in row.failures
    assert started == ["ran", "ran"]
    # A host that cannot report its memory is not refused; the fake provider never is.
    free["bytes"] = None  # type: ignore[assignment]
    assert not runner._host_memory_low(
        _profile_config(path, "forge"), runner._Cell(0, task, "fake", "forge", 3)
    )
    free["bytes"] = 1
    assert not runner._host_memory_low(
        _config(Path("unused"), "forge"), runner._Cell(0, task, "fake", "forge", 3)
    )
    text = TASKS["sentiment-24"]
    assert not runner._host_memory_low(
        _profile_config(path, "forge"), runner._Cell(0, text, "fake", "forge", SIZE)
    )


# ------------------------------------------------------------------ review round 4 regressions


def _play_counters(distinct: int) -> object:
    return SimpleNamespace(
        actions=10, distinct_states=distinct, revisits=0, model_calls=5, levels_completed=1
    )


def test_delivery_counters_require_the_delivery_play() -> None:
    from architect.bench.runner import _delivery_counters

    verify = _play_counters(3)
    stage4 = SimpleNamespace(last_counters=verify, last_run_id="verify-run")
    no_play = SimpleNamespace(play_result=None)
    # Refused, unlicensed or budget-stopped: the backend's last play was not delivery.
    assert _delivery_counters([stage4], no_play) is None
    # A delivery run whose play did not close on this backend still contributes nothing.
    other = SimpleNamespace(play_result=SimpleNamespace(run_id="delivery-run"))
    assert _delivery_counters([stage4], other) is None
    assert _delivery_counters([], other) is None
    delivered = SimpleNamespace(last_counters=_play_counters(9), last_run_id="delivery-run")
    assert _delivery_counters([stage4, delivered], other) is delivered.last_counters


def test_arc_process_metrics_skip_cells_without_a_delivery_play(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from architect.bench import runner
    from architect.models.pricing import load_prices

    delivered = _play_counters(20)
    # Game 1: delivery refused after Stage 4 verification plays (game_sandbox_forbidden).
    # Game 2: compile unlicensed after tuning plays. Game 3: a real delivery play.
    plans = iter(
        [
            (_play_counters(2), "verify-1", None),
            (_play_counters(4), "tune-2", None),
            (delivered, "deliver-3", "deliver-3"),
        ]
    )

    class Factory:
        def __init__(self, compiler: Any, games: object, config: object) -> None:
            self.compiler = compiler

        async def run(self, target: object, *, support_dataset: str) -> object:
            counters, last_run_id, play_id = next(plans)
            self.compiler.runtime.game_backends.append(
                SimpleNamespace(last_counters=counters, last_run_id=last_run_id)
            )
            play = None if play_id is None else SimpleNamespace(run_id=play_id)
            return SimpleNamespace(
                compile_result=None,
                play_result=play,
                accepted=play is not None,
                failures=() if play is not None else ("game_sandbox_forbidden",),
                actions_used=10,
                score=None,
            )

    monkeypatch.setattr(runner, "GameFactory", Factory)
    config = _config(tmp_path / "out", "default")
    ctx = runner._Context(
        config, profile=None, factory=None, prices=load_prices(), clock=lambda: 0.0
    )
    cell = runner._Cell(0, TASKS["arc-local-3"], "fake", "default", 3)  # type: ignore[arg-type]
    sized = runner._size_cell(ctx, cell)
    row = asyncio.run(
        runner._run_cell(
            ctx, runner._Identity(config, None), sized, runner._Totals(total=1), tmp_path
        )
    )
    arc = row.arc
    assert arc is not None and arc.games == 3
    expected = process_metrics([delivered])
    assert arc.distinct_states_mean == expected["distinct_states_mean"] == 20.0
    assert arc.levels_completed_total == 1
    assert arc.calls_per_action == pytest.approx(0.5)


# ------------------------------------------------------------------ final review regressions


def test_each_game_compile_gets_its_own_forge_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replayed ForgeScript is spent by one Pi session, so one agent per game compile.

    A cell-wide agent would reach game 2 and 3 already at its final text, leaving them
    unforged while the summed counts still pass the guard (a silent partial not-run).
    """
    from architect.bench import runner
    from architect.models.pricing import load_prices

    built: list[object] = []
    original = runner._Context.forge_agent

    def forge_agent(self: Any, cell: Any, tally: Any) -> Any:
        agent = original(self, cell, tally)
        built.append(agent)
        return agent

    seen: list[tuple[object, Any]] = []

    class Factory:
        def __init__(self, compiler: Any, games: object, config: Any) -> None:
            seen.append((compiler.forge_adapter, config))

        async def run(self, target: object, *, support_dataset: str) -> object:
            return SimpleNamespace(
                compile_result=None,
                play_result=None,
                accepted=False,
                failures=("game_compile_failed",),
                actions_used=0,
                score=None,
            )

    monkeypatch.setattr(runner._Context, "forge_agent", forge_agent)
    monkeypatch.setattr(runner, "GameFactory", Factory)
    config = _config(tmp_path / "out", "forge")
    ctx = runner._Context(
        config, profile=None, factory=None, prices=load_prices(), clock=lambda: 0.0
    )
    cell = runner._Cell(0, TASKS["arc-local-3"], "fake", "forge", 3)  # type: ignore[arg-type]
    sized = runner._size_cell(ctx, cell)
    row = asyncio.run(
        runner._run_cell(
            ctx, runner._Identity(config, None), sized, runner._Totals(total=1), tmp_path
        )
    )
    assert row.arc is not None and row.arc.games == 3
    agents = [agent for agent, _ in seen]
    assert len(agents) == 3 and all(agent is not None for agent in agents)
    assert len({id(agent) for agent in agents}) == 3
    assert agents == built
    for _, factory_config in seen:
        assert factory_config.allow_forge is True
        assert factory_config.compile_config.forge.enabled is True
    # The factory never forged here, and the guard says so rather than passing it.
    assert "forge_did_not_run" in row.failures


def test_arc_arms_actually_forged_through_stage_3(arms: list[BenchRow]) -> None:
    """Section 14 Stage A on arc-local-3 (review: the game factory never ran Stage 3)."""
    default = _row(arms, "arc-local-3", "default")
    assert default.licensed and 3 not in default.stages
    for variant in ("forge", "template"):
        row = _row(arms, "arc-local-3", variant)
        assert "forge_did_not_run" not in row.failures
        assert 3 in row.stages and row.licensed
        assert row.forge is not None and row.forge.submissions >= 1
        # The Forge harness itself was promoted (and so delivered), not the preset.
        assert row.forge.accepted >= 1
    forge = _row(arms, "arc-local-3", "forge")
    assert forge.forge is not None and forge.forge.measures >= 1
    assert forge.arc is not None and default.arc is not None
    assert forge.arc.distinct_states_mean is not None
    assert default.arc.distinct_states_mean is not None
    assert forge.arc.distinct_states_mean > default.arc.distinct_states_mean


# ------------------------------------------------------------------ Pi confinement in bench cells


def test_profile_bench_forge_sessions_get_the_effective_denied_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review: a bench profile run handed Pi ``profile.pi`` unchanged, so the Forge
    agent could read or write the cell's signing key, telemetry, artifacts and rows.

    The denied set handed to the session must be the one ``build_services`` builds
    (profile file, floor/smoke/adapter fixtures, calibrations, workspace control
    plane, relative ``denied_paths`` resolved against the profile) plus the cell's
    own secrets.
    """
    import os

    from architect.bench import runner
    from architect.compiler import forge as forge_module
    from architect.models.pricing import load_prices
    from architect.profiles import load_profile

    home = tmp_path / "home"
    home.mkdir()
    (home / "fx.json").write_text("[]", encoding="utf-8")
    (home / "agent.json").write_text("[]", encoding="utf-8")
    payload: dict[str, Any] = {
        "workspace": "w",
        "models": {
            "fake": {"provider": "fake"},
            "spare": {"provider": "fake", "fixture": "fx.json"},
        },
        "forge_agent": {"provider": "fake", "model": "gpt-4o-mini", "fixture": "agent.json"},
        "floors": {"d": {"kind": "held_out_tasks", "path": "floor.json", "smoke": "smoke.json"}},
        "calibrations": {"c": "cal.json"},
        "pi": {"denied_paths": ["custom-private"]},
    }
    path = home / "profile.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    profile = load_profile(path)
    seen: list[tuple[tuple[Path, ...], Path]] = []

    async def capture(*args: Any, pi_config: Any, **kwargs: Any) -> Any:
        key = Path(os.environ["ARCHITECT_SIGNING_KEY_FILE"])
        seen.append((tuple(pi_config.denied_paths), key.parent))
        raise ValueError("forge_failed")  # Stage 3 is recorded as failed; Stage 4 still runs

    monkeypatch.setattr(forge_module, "forge", capture)
    out = tmp_path / "out"
    for variant in ("template", "forge"):
        config = _profile_config(path, variant)
        config = config.model_copy(update={"out": out})
        ctx = runner._Context(
            config, profile=profile, factory=None, prices=load_prices(), clock=lambda: 0.0
        )
        cell = runner._Cell(0, TASKS["sentiment-24"], "fake", variant, SIZE)  # type: ignore[arg-type]
        sized = runner._size_cell(ctx, cell)
        asyncio.run(
            runner._run_cell(
                ctx, runner._Identity(config, profile), sized, runner._Totals(total=1), tmp_path
            )
        )
    assert len(seen) == 2
    workspace = (home / "w").resolve()
    for denied_paths, root in seen:
        denied = set(denied_paths)
        required = {
            # the bench cell's own secrets
            root.resolve(),
            (root / "signing.key").resolve(),
            (root / "telemetry").resolve(),
            (root / "artifacts").resolve(),
            out.resolve(),  # rows.jsonl and summary.json live here
            # build_services' set, resolved against the profile
            path.resolve(),
            (home / "fx.json").resolve(),
            (home / "agent.json").resolve(),
            (home / "floor.json").resolve(),
            (home / "smoke.json").resolve(),
            (home / "cal.json").resolve(),
            (home / "custom-private").resolve(),
            *(workspace / name for name in ("artifacts", "library", "registry", "telemetry")),
        }
        assert required <= denied, required - denied
        # Relative denied_paths never stay relative (they would resolve against Pi's cwd).
        assert all(item.is_absolute() for item in denied)


def test_fake_bench_forge_sessions_deny_the_cell_secrets(tmp_path: Path) -> None:
    from architect.bench import runner
    from architect.models.pricing import load_prices

    config = _config(tmp_path / "out", "forge")
    ctx = runner._Context(
        config, profile=None, factory=None, prices=load_prices(), clock=lambda: 0.0
    )
    root = tmp_path / "cell"
    denied = set(ctx.pi_config(root).denied_paths)  # type: ignore[attr-defined]
    for item in (root, root / "signing.key", root / "telemetry", root / "artifacts"):
        assert item.resolve() in denied
    assert (tmp_path / "out").resolve() in denied


def test_fixture_player_reads_the_forge_harness_frame_and_is_priced() -> None:
    """The same frame choice for a Forge harness's hex-frame prompt, and every call is
    priced from the table (synthetic, API-equivalent) so asking less is cheaper."""
    from architect.bench.providers import PublicFramePlayer
    from architect.games.grids import grid_text

    grid = [[0, 0, 0], [0, 2, 0], [0, 3, 0]]
    prompt = (
        "You play a grid game.\nFrame (one hex digit per cell):\n"
        + grid_text(grid)
        + "\nOptions: ACTION1, ACTION2, ACTION3, ACTION4"
    )
    player = PublicFramePlayer("fake")
    reply = asyncio.run(player.complete(ModelRequest(prompt=prompt)))
    assert json.loads(reply.text) == {"option": PublicFramePlayer.choose(grid)} == {
        "option": "ACTION2"
    }
    unlisted = prompt.replace("ACTION2, ", "")
    assert json.loads(asyncio.run(player.complete(ModelRequest(prompt=unlisted))).text) == {
        "option": None
    }
    assert reply.cost_usd > 0 and player.quote(ModelRequest(prompt=prompt)).cost_usd > 0


@needs_pillow
def test_ci_assert_enforces_stage_a_on_the_forge_arms(arms: list[BenchRow]) -> None:
    """CI must catch Forge silently not running on ARC (the review-found gap)."""
    from architect.bench.ci_assert import _forge_checks

    assert _forge_checks(arms) == []
    forge = _row(arms, "arc-local-3", "forge")
    assert forge.forge is not None and forge.arc is not None
    skipped = forge.model_copy(update={"stages": (0, 1, 2, 4)})
    unmeasured = forge.model_copy(update={"forge": forge.forge.model_copy(update={"measures": 0})})
    default = _row(arms, "arc-local-3", "default")
    assert default.arc is not None
    flat = forge.model_copy(
        update={"arc": forge.arc.model_copy(
            update={"distinct_states_mean": default.arc.distinct_states_mean}
        )}
    )
    for broken, phrase in (
        (skipped, "skipped Stage 3"),
        (unmeasured, "never measured"),
        (flat, "not above default"),
    ):
        rows = [broken if row is forge else row for row in arms]
        assert any(phrase in line for line in _forge_checks(rows)), phrase
