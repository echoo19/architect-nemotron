"""Phase 8 adversarial checks on the bench: it measures, it never licenses or leaks.

The fake provider must not be able to read the floor's expected values; a row may
say ``licensed`` only when a signed Stage 4 report on disk says so; changed presets
cannot be compared silently; planted sentinel strings (in a task and a case id)
never reach rows, summaries, tables or progress; and nothing is installed.
"""

from __future__ import annotations

import ast
import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from architect.bench import (
    BenchError,
    bench_table,
    compare,
    read_rows,
)
from architect.bench import runner as bench_runner
from architect.bench.oracle import OracleState, lookup_key
from architect.bench.runner import BenchConfig, BenchProgress, run_bench
from architect.bench.suite import BenchTask, DatasetBundle
from architect.bench.text_tasks import canonical_key
from architect.compiler import Compiler, CompileResult
from architect.models import FakeAdapter
from architect.spec import HarnessArtifact

SENTINEL = "SENTINEL-7f3a9c"


def quick(tmp_path: Path, **updates: object) -> BenchConfig:
    values: dict[str, object] = {
        "tier": "quick",
        "provider": "fake",
        "size": 4,
        "out": tmp_path / "out",
    }
    values.update(updates)
    return BenchConfig.model_validate(values)


def patch_bundle(
    monkeypatch: pytest.MonkeyPatch,
    task_id: str,
    rewrite: Callable[[DatasetBundle], DatasetBundle],
) -> None:
    original = BenchTask.bundle

    def bundle(self: BenchTask, seed: int, size: int) -> DatasetBundle:
        made = original(self, seed, size)
        return rewrite(made) if self.id == task_id else made

    monkeypatch.setattr(BenchTask, "bundle", bundle)


async def test_the_fake_cannot_read_expected_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Corrupt every expected label but not the oracle's tables: the floor must fail."""

    def corrupt(made: DatasetBundle) -> DatasetBundle:
        cases = [case.model_copy(update={"expected": {"label": "neutral"}}) for case in made.cases]
        return DatasetBundle(cases=cases, state=made.state, manifests=made.manifests)

    patch_bundle(monkeypatch, "sentiment-24", corrupt)
    result = await run_bench(quick(tmp_path), workspace_root=tmp_path / "ws")
    sentiment = next(row for row in result.rows if row.task_id == "sentiment-24")
    assert not sentiment.licensed
    assert sentiment.held_out_score is not None and sentiment.held_out_score < 0.9
    others = [row for row in result.rows if row.task_id != "sentiment-24"]
    assert others and all(row.licensed for row in others)


@pytest.mark.parametrize("tamper", ["delete", "score", "signature"])
async def test_a_row_is_licensed_only_by_a_signed_report_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    original = Compiler.compile

    async def compile_then_tamper(self: Compiler, *args: object, **kwargs: object) -> object:
        result = await original(self, *args, **kwargs)  # type: ignore[arg-type]
        assert isinstance(result, CompileResult) and result.artifact is not None
        report = result.artifact.directory / "report.json"
        if tamper == "delete":
            report.unlink()
        else:
            payload = json.loads(report.read_text(encoding="utf-8"))
            if tamper == "score":
                payload["floor_score"] = 1.0 if payload["floor_score"] < 1 else 0.99
                payload["passed"] = True
            else:
                payload["signature"] = "0" * 64
            report.write_text(json.dumps(payload), encoding="utf-8")
        assert result.artifact.report is not None  # the in-memory copy still looks licensed
        return result

    monkeypatch.setattr(Compiler, "compile", compile_then_tamper)
    config = quick(tmp_path, presets=("extractor",))
    result = await run_bench(config, workspace_root=tmp_path / "ws")
    assert result.rows and not any(row.licensed for row in result.rows)


def test_game_licence_needs_a_witnessed_licence(tmp_path: Path) -> None:
    from architect.telemetry import TelemetryStore

    store = TelemetryStore(tmp_path / "telemetry")
    try:
        runtime = bench_runner._LicenceWitness(store, adapter_factory=FakeAdapter)
        assert not bench_runner._witnessed(runtime, None)
        missing = HarnessArtifact.model_construct(directory=tmp_path / "nope")
        assert not bench_runner._witnessed(runtime, missing)  # never played: no licence
        assert not bench_runner._licensed_on_disk(missing)
        runtime.witnessed[missing.directory] = False
        assert not bench_runner._witnessed(runtime, missing)
    finally:
        store.close()


async def test_a_game_row_is_licensed_only_when_the_runtime_saw_it_licensed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runtime plays the artifact, but the witnessed on-disk licence says no: unlicensed."""
    config = BenchConfig(
        tier="smoke", provider="fake", presets=("explorer",), out=tmp_path / "out"
    )
    result = await run_bench(config, workspace_root=tmp_path / "ws")
    [row] = result.rows
    assert row.task_id == "arc-local-3" and row.licensed  # the honest path licenses
    monkeypatch.setattr(bench_runner, "_licensed_on_disk", lambda artifact: False)
    result = await run_bench(config, workspace_root=tmp_path / "ws")
    [row] = result.rows
    assert not row.licensed and row.calls > 0


async def test_compare_refuses_changed_presets_without_the_flag(tmp_path: Path) -> None:
    result = await run_bench(
        quick(tmp_path, size=2, presets=("extractor",)), workspace_root=tmp_path / "ws"
    )
    base = list(result.rows)
    changed = [row.model_copy(update={"preset_fingerprint": "f" * 64}) for row in base]
    with pytest.raises(BenchError) as caught:
        compare(base, changed)
    assert caught.value.code == "bench_compare_fingerprint"
    assert compare(base, changed, allow_fingerprint_mismatch=True).verdict == "identical"
    other_suite = [row.model_copy(update={"suite_digest": "0" * 64}) for row in base]
    with pytest.raises(BenchError) as caught:
        compare(base, other_suite)
    assert caught.value.code == "bench_compare_mismatch"


async def test_sentinels_never_reach_rows_tables_or_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def plant(made: DatasetBundle) -> DatasetBundle:
        cases = []
        state = OracleState()
        for case in made.cases:
            assert isinstance(case.task, dict)
            task = {"review": f"{case.task['review']} {SENTINEL}"}
            planted = case.model_copy(
                update={"case_id": f"{SENTINEL}-{case.case_id}", "task": task}
            )
            cases.append(planted)
            state.text[canonical_key(task)] = case.expected
        state.add_floor([(case.case_id, lookup_key(case.task)) for case in cases], 0.9)
        return DatasetBundle(cases=cases, state=state, manifests=())

    patch_bundle(monkeypatch, "sentiment-24", plant)
    lines: list[str] = []

    def progress(item: BenchProgress) -> None:
        lines.append(item.model_dump_json())

    result = await run_bench(
        quick(tmp_path, label="adversarial"), on_progress=progress, workspace_root=tmp_path / "ws"
    )
    sentiment = next(row for row in result.rows if row.task_id == "sentiment-24")
    assert sentiment.licensed  # the planted cases were really run and verified
    directory = result.rows_path.parent
    for name in ("rows.jsonl", "summary.json"):
        assert SENTINEL not in (directory / name).read_text(encoding="utf-8")
    assert SENTINEL not in bench_table(read_rows(result.rows_path))
    assert lines and not any(SENTINEL in line for line in lines)
    assert list((tmp_path / "ws").iterdir()) == []


async def test_the_bench_installs_nothing(tmp_path: Path) -> None:
    result = await run_bench(
        quick(tmp_path, size=2), workspace_root=tmp_path / "ws"
    )
    assert all(row.licensed for row in result.rows)
    for name in ("registry", "harness.py", "signing.key"):
        assert not found(tmp_path, name), name


def found(root: Path, name: str) -> list[Path]:
    return list(root.rglob(name))


async def test_bench_arc_installs_only_into_the_throwaway_cell_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``bench arc`` is the one path that installs (public.manufacture does, as the script
    does): the registry it gets must live inside the cell workspace, and go with it."""
    from architect.games import public

    roots: list[Path] = []
    keys: list[str | None] = []

    async def manufacture(options: object, root: Path, **kwargs: object) -> dict[str, object]:
        roots.append(root)
        keys.append(os.environ.get("ARCHITECT_SIGNING_KEY_FILE"))
        (root / "registry").mkdir()
        (root / "registry" / "harness.py").write_text("installed", encoding="utf-8")
        return {
            "licensed": True,
            "score": 0.5,
            "score_source": "official_offline_preview",
            "model_binding": "offline",
        }

    monkeypatch.setattr(bench_runner, "_has_module", lambda name: True)
    monkeypatch.setattr(public, "manufacture", manufacture)
    operator_key = tmp_path / "authority.key"  # the conftest key: the operator's
    config = BenchConfig(
        tier="arc", provider="fake", arc_cache=tmp_path / "cache", out=tmp_path / "out"
    )
    result = await run_bench(config, workspace_root=tmp_path / "ws")
    [row] = result.rows
    assert row.licensed and row.arc is not None and row.arc.score == 0.5
    [root] = roots
    assert root.parent == tmp_path / "ws"  # a throwaway cell workspace
    assert keys == [str(root / "signing.key")] and keys[0] != str(operator_key)
    assert list((tmp_path / "ws").iterdir()) == []  # deleted with the cell
    for name in ("registry", "harness.py", "signing.key"):
        assert not found(tmp_path, name), name


async def test_noisy_regression_holds_for_another_seed(tmp_path: Path) -> None:
    """The negative control is a fixed quota per partition, not luck of seed 7."""
    config = BenchConfig(
        tier="smoke",
        provider="fake",
        seed=11,
        variants=("default", "noisy"),
        presets=("extractor",),
        out=tmp_path / "out",
    )
    result = await run_bench(config, workspace_root=tmp_path / "ws")
    rows = [row for row in result.rows if row.task_id == "exam-clean-40"]
    default = [row for row in rows if row.variant == "default"]
    noisy = [row.model_copy(update={"variant": "default"}) for row in rows if row not in default]
    [cell] = compare(default, noisy).cells
    assert cell.verdict == "regression" and cell.mcnemar_p is not None
    assert cell.mcnemar_p < 0.05 and cell.discordant_b >= 6


def test_only_the_bench_surfaces_import_the_bench_package() -> None:
    """``architect.bench`` is imported only by itself, the CLI and the session verb."""
    package = Path(__file__).resolve().parents[2] / "architect"
    allowed = {package / "cli.py", package / "session" / "bench.py"}
    offenders = []
    for path in package.rglob("*.py"):
        if package / "bench" in path.parents or path in allowed:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            if any(name == "architect.bench" or name.startswith("architect.bench.")
                   for name in names):
                offenders.append(path.relative_to(package).as_posix())
    assert offenders == []
    # The bench itself never imports the session or the CLI (no cycles, no UI coupling).
    for path in (package / "bench").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "architect.session" not in text and "architect.cli" not in text, path.name


def test_the_bench_imports_only_its_contract_packages() -> None:
    """Section 2: models, runtime, compiler, floors, presets, spec, telemetry, games,
    profiles. In particular never the signing module ``architect._authority``."""
    allowed = {
        "models", "runtime", "compiler", "floors", "presets", "spec", "telemetry", "games",
        "profiles", "bench",
        # Phase 10 Section 14: the labels and the router are re-imported from
        # ``architect.routing``, and a routed cell's Phase A installs into its own
        # throwaway registry and library (deleted with the cell workspace).
        "routing", "registry", "library",
    }
    package = Path(__file__).resolve().parents[2] / "architect" / "bench"
    offenders = []
    for path in package.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                parts = name.split(".")
                if parts[0] == "architect" and (len(parts) < 2 or parts[1] not in allowed):
                    offenders.append(f"{path.name}: {name}")
    assert offenders == []
