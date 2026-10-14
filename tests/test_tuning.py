"""Stage-2 parameter search, warm rebinding, and isolated artifact persistence."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import JsonValue

from architect.compiler import tuning
from architect.compiler.tuning import GridSearch, SuccessiveHalving, TuningError, tune
from architect.compiler.types import AdapterFactory, Measurement
from architect.floors import EditLogRow, EvaluationCase, FloorEvaluator, HeldOutTasks, HumanEditRate
from architect.models import FakeAdapter
from architect.presets import Preset, get_preset
from architect.runtime import RunContext, Runtime, load_harness
from architect.runtime.evaluation import evaluate_cases
from architect.spec import Budget, FloorDefinition, FloorKind, HarnessArtifact, Spec
from architect.telemetry import TelemetryStore


def specification() -> Spec:
    return Spec(
        task="Summarize supplied values", input_schema={"type": "object"},
        output_schema={"type": "string"},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="synthetic"),
        budget=Budget(),
    )


def evaluator(spec: Spec, count: int = 4) -> HeldOutTasks:
    return HeldOutTasks(spec.floor, [
        EvaluationCase(case_id=f"{index:03}", task={"index": index}, expected="ok")
        for index in range(count * 2)
    ])


def runtime(root: Path) -> Runtime:
    return Runtime(TelemetryStore(root / "telemetry"))


def warm_artifact(root: Path, spec: Spec) -> HarnessArtifact:
    preset = get_preset("synthesizer")
    params = preset.validate_params({"context_window": 2048, "terseness": 1, "verifier_depth": 1})
    components = ("synthesis",)
    preset.emit(root, spec, params, components)
    warm = HarnessArtifact(
        spec=spec, preset=preset.name, model="fake", components=components,
        params=params, directory=root,
    )
    warm.save()
    return warm


def measurement(
    count: int, *, score: float = 1, floor: bool = True, budget: bool = True,
    cost: float = 0.01, tokens: int = 10,
) -> Measurement:
    return Measurement(
        score=score, floor_passed=floor, budget_passed=budget, mean_cost_usd=cost,
        max_tokens=tokens, max_cost_usd=cost, max_compute_s=0.001, sample_count=count,
        failures=() if floor and budget else ("floor_not_met",),
    )


def test_cartesian_grid_and_warm_first(tmp_path: Path) -> None:
    preset = get_preset("synthesizer")
    grid = GridSearch().configurations(preset)
    assert len(grid) == 8
    assert grid[0] == {"context_window": 512, "terseness": 0, "verifier_depth": 0}
    assert grid[-1] == {"context_window": 2048, "terseness": 1, "verifier_depth": 1}
    warm = warm_artifact(tmp_path, specification())
    warm_grid = GridSearch().configurations(preset, warm)
    assert len(warm_grid) == 8
    assert warm_grid[0] == warm.params
    assert GridSearch().configurations(preset) == grid


async def test_real_grid_persisted_bounded_and_unlicensed(tmp_path: Path) -> None:
    spec = specification()
    result = await tune(
        spec, get_preset("synthesizer"), "fake", evaluator(spec), FakeAdapter,
        runtime(tmp_path), tmp_path / "artifacts", max_candidates=3,
    )
    assert result.passed and result.best is not None
    assert len(result.candidates) == 3
    assert result.best.artifact.params["verifier_depth"] == 0
    assert result.best.artifact.components == ("synthesis", "context_policy")
    assert not result.warm_started
    assert any(candidate.measurement.failures for candidate in result.candidates)
    for index, candidate in enumerate(result.candidates, 1):
        artifact = candidate.artifact
        assert candidate.measurement.sample_count == 4
        assert artifact.directory == tmp_path / "artifacts" / spec.spec_hash / str(index)
        loaded = HarnessArtifact.load(artifact.directory)
        assert loaded == artifact and not loaded.licensed and loaded.report is None
        assert not (artifact.directory / "report.json").exists()


async def test_warm_rebound_spec_components_and_params(tmp_path: Path) -> None:
    old = specification()
    warm = warm_artifact(tmp_path / "warm", old)
    original = (warm.directory / "harness.py").read_bytes()
    spec = old.model_copy(update={"task": "New request", "budget": Budget(cost_usd=0.05)})
    result = await tune(
        spec, get_preset("synthesizer"), "fake", evaluator(spec), FakeAdapter,
        runtime(tmp_path), tmp_path / "artifacts", warm_start=warm, max_candidates=1,
    )
    assert result.best is not None and result.warm_started
    artifact = result.best.artifact
    assert artifact.spec == spec and artifact.params == warm.params
    assert artifact.components == warm.components
    assert artifact.directory != warm.directory
    assert artifact.report is None and not artifact.licensed
    assert (warm.directory / "harness.py").read_bytes() == original
    source = (artifact.directory / "harness.py").read_text()
    assert "New request" in source and "Summarize supplied values" not in source
    output = await load_harness(artifact.directory)(RunContext(FakeAdapter(), spec.budget), {})
    assert output == "ok"


async def test_rewritten_warm_code_fails_cleanly_before_execution(tmp_path: Path) -> None:
    spec = specification()
    warm = warm_artifact(tmp_path / "warm", spec)
    (warm.directory / "harness.py").write_text("raise RuntimeError('must not execute')")
    with pytest.raises(TuningError, match="Rewritten warm-start source"):
        await tune(
            spec, get_preset("synthesizer"), "fake", evaluator(spec), FakeAdapter,
            runtime(tmp_path), tmp_path / "artifacts", warm_start=warm,
        )
    assert not (tmp_path / "artifacts").exists()


async def test_incompatible_warm_rejected(tmp_path: Path) -> None:
    spec = specification()
    warm = warm_artifact(tmp_path / "warm", spec)
    changed = spec.model_copy(update={"tools": ("read_file",)})
    with pytest.raises(TuningError, match="Incompatible"):
        await tune(
            changed, get_preset("synthesizer"), "fake", evaluator(changed), FakeAdapter,
            runtime(tmp_path), tmp_path / "artifacts", warm_start=warm,
        )


async def test_all_failed_keeps_evidence_and_best(tmp_path: Path) -> None:
    spec = specification().model_copy(update={"budget": Budget(cost_usd=0.000001)})
    result = await tune(
        spec, get_preset("synthesizer"), "fake", evaluator(spec), FakeAdapter,
        runtime(tmp_path), tmp_path / "artifacts", max_candidates=3,
    )
    assert not result.passed and result.best is not None
    assert len(result.best.artifact.components) == 2
    assert all(not item.measurement.budget_passed for item in result.candidates)
    assert all(item.measurement.failures for item in result.candidates)
    assert all(item.measurement.sample_count == 4 for item in result.candidates)


async def test_repeat_tuning_never_overwrites(tmp_path: Path) -> None:
    spec = specification()
    directory = tmp_path / "artifacts" / spec.spec_hash
    directory.mkdir(parents=True)
    (directory / "7").write_text("reserved")
    first = await tune(
        spec, get_preset("synthesizer"), "fake", evaluator(spec), FakeAdapter,
        runtime(tmp_path), tmp_path / "artifacts", max_candidates=1,
    )
    assert first.best is not None
    before = {path.name: path.read_bytes() for path in first.best.artifact.directory.iterdir()}
    second = await tune(
        spec, get_preset("synthesizer"), "fake", evaluator(spec), FakeAdapter,
        runtime(tmp_path), tmp_path / "artifacts", max_candidates=1,
    )
    assert second.best is not None
    assert first.best.artifact.version == 8 and second.best.artifact.version == 9
    after = {path.name: path.read_bytes() for path in first.best.artifact.directory.iterdir()}
    assert before == after
    assert (directory / "7").read_text() == "reserved"


def test_atomic_concurrent_version_allocation(tmp_path: Path) -> None:
    spec = specification()
    with ThreadPoolExecutor(max_workers=8) as pool:
        reservations = list(pool.map(lambda _: tuning._allocate(tmp_path, spec), range(24)))
    assert sorted(version for _, version in reservations) == list(range(1, 25))
    assert len({directory for directory, _ in reservations}) == 24


async def test_halving_really_discards_and_rescores_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = specification()
    calls: list[tuple[int, int]] = []

    async def evaluate(
        artifact: HarnessArtifact, floor: FloorEvaluator, cases: tuple[EvaluationCase, ...],
        adapter_factory: AdapterFactory, runtime: Runtime,
    ) -> Measurement:
        calls.append((artifact.version, len(cases)))
        # The last configuration is cheapest; enumeration order must not win.
        return measurement(len(cases), cost=(9 - artifact.version) / 1000)

    monkeypatch.setattr(tuning, "evaluate_cases", evaluate)
    result = await tune(
        spec, get_preset("synthesizer"), "fake", evaluator(spec, 8), FakeAdapter,
        runtime(tmp_path), tmp_path / "artifacts", strategy=SuccessiveHalving(),
    )
    assert len(result.candidates) == 8
    assert [sum(size == n for _, size in calls) for n in (1, 2, 4, 8)] == [8, 4, 2, 1]
    assert result.best is not None and result.best.artifact.version == 8
    assert result.best.measurement.sample_count == 8
    assert result.candidates[0].measurement.sample_count == 1


async def test_edit_rate_minimum_samples_never_violated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = FloorDefinition(
        kind=FloorKind.HUMAN_EDIT_RATE, dataset="edit-replay", limit=0, minimum_samples=3,
    )
    spec = specification().model_copy(update={"floor": definition})
    floor = HumanEditRate(definition, [
        EditLogRow(case_id=f"{i:03}", task={}, proposed="ok", shipped="ok", changed=False)
        for i in range(20)
    ])
    counts: list[int] = []
    original = evaluate_cases

    async def evaluate(
        artifact: HarnessArtifact, floor: FloorEvaluator, cases: tuple[EvaluationCase, ...],
        adapter_factory: AdapterFactory, runtime: Runtime,
    ) -> Measurement:
        counts.append(len(cases))
        return await original(artifact, floor, cases, adapter_factory, runtime)

    monkeypatch.setattr(tuning, "evaluate_cases", evaluate)
    result = await tune(
        spec, get_preset("synthesizer"), "fake", floor, FakeAdapter,
        runtime(tmp_path), tmp_path / "artifacts", strategy=SuccessiveHalving(),
    )
    assert min(counts) == 3 and max(counts) == 10
    assert result.passed and result.best is not None
    assert result.best.measurement.sample_count == 10


async def test_ranking_measured_cost_before_simplicity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = specification()

    async def evaluate(
        artifact: HarnessArtifact, floor: FloorEvaluator, cases: tuple[EvaluationCase, ...],
        adapter_factory: AdapterFactory, runtime: Runtime,
    ) -> Measurement:
        return measurement(len(cases), cost=0.001 if artifact.version == 2 else 0.01)

    monkeypatch.setattr(tuning, "evaluate_cases", evaluate)
    result = await tune(
        spec, get_preset("synthesizer"), "fake", evaluator(spec), FakeAdapter,
        runtime(tmp_path), tmp_path / "artifacts", max_candidates=3,
    )
    assert result.best is not None and result.best.artifact.version == 2
    assert "review" in result.best.artifact.components


@pytest.mark.parametrize("cap", [0, -1, True])
async def test_invalid_cap_rejected(cap: int, tmp_path: Path) -> None:
    spec = specification()
    with pytest.raises(TuningError, match="positive integer"):
        await tune(
            spec, get_preset("synthesizer"), "fake", evaluator(spec), FakeAdapter,
            runtime(tmp_path), tmp_path / "artifacts", max_candidates=cap,
        )


async def test_mismatched_floor_rejected(tmp_path: Path) -> None:
    spec = specification()
    other = spec.model_copy(update={"floor": spec.floor.model_copy(update={"limit": 0.5})})
    with pytest.raises(TuningError, match="Floor definition"):
        await tune(
            spec, get_preset("synthesizer"), "fake", evaluator(other), FakeAdapter,
            runtime(tmp_path), tmp_path / "artifacts",
        )


async def test_custom_strategy_empty_and_duplicate_configs(tmp_path: Path) -> None:
    class Custom:
        def __init__(self, configs: list[dict[str, JsonValue]]) -> None:
            self.configs = configs

        def configurations(
            self, preset: Preset, warm_start: HarnessArtifact | None = None,
        ) -> list[dict[str, JsonValue]]:
            return self.configs

    spec = specification()
    empty = await tune(
        spec, get_preset("synthesizer"), "fake", evaluator(spec), FakeAdapter,
        runtime(tmp_path), tmp_path / "artifacts", strategy=Custom([]),
    )
    assert empty.best is None and not empty.candidates
    duplicates = await tune(
        spec, get_preset("synthesizer"), "fake", evaluator(spec), FakeAdapter,
        runtime(tmp_path), tmp_path / "artifacts", strategy=Custom([{}, {}, {}]),
    )
    assert len(duplicates.candidates) == 1


@pytest.mark.parametrize("preferred", ["floor", "budget", "simplicity", "tokens", "params"])
async def test_gate_and_tie_order(
    preferred: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = specification()

    async def evaluate(
        artifact: HarnessArtifact, floor: FloorEvaluator, cases: tuple[EvaluationCase, ...],
        adapter_factory: AdapterFactory, runtime: Runtime,
    ) -> Measurement:
        version = artifact.version
        if preferred == "floor":
            return measurement(len(cases), floor=version == 2, budget=version != 2)
        if preferred == "budget":
            return measurement(len(cases), floor=False, budget=version == 2)
        if preferred == "tokens":
            return measurement(len(cases), tokens=1 if version == 3 else 10)
        return measurement(len(cases))

    monkeypatch.setattr(tuning, "evaluate_cases", evaluate)
    result = await tune(
        spec, get_preset("synthesizer"), "fake", evaluator(spec), FakeAdapter,
        runtime(tmp_path), tmp_path / "artifacts", max_candidates=3,
    )
    assert result.best is not None
    expected = 2 if preferred in {"floor", "budget"} else 3 if preferred == "tokens" else 1
    assert result.best.artifact.version == expected


async def test_warm_first_even_for_custom_strategy(tmp_path: Path) -> None:
    class Empty:
        def configurations(
            self, preset: Preset, warm_start: HarnessArtifact | None = None,
        ) -> list[dict[str, JsonValue]]:
            return []

    spec = specification()
    warm = warm_artifact(tmp_path / "warm", spec)
    result = await tune(
        spec, get_preset("synthesizer"), "fake", evaluator(spec), FakeAdapter,
        runtime(tmp_path), tmp_path / "artifacts", strategy=Empty(), warm_start=warm,
    )
    assert result.best is not None and result.best.artifact.params == warm.params
    assert len(result.candidates) == 1


async def test_warm_zero_depth_checks_normalized(tmp_path: Path) -> None:
    spec = specification()
    preset = get_preset("synthesizer")
    params = preset.validate_params({"verifier_depth": 0})
    preset.emit(tmp_path / "warm", spec, params)
    warm = HarnessArtifact(
        spec=spec, preset=preset.name, model="fake", components=preset.components,
        params=params, directory=tmp_path / "warm",
    )
    result = await tune(
        spec, preset, "fake", evaluator(spec), FakeAdapter, runtime(tmp_path),
        tmp_path / "artifacts", warm_start=warm, max_candidates=1,
    )
    assert result.best is not None
    assert result.best.artifact.components == ("synthesis", "context_policy")
    assert "review" in warm.components
