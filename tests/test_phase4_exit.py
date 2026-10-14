import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from architect.compiler import CompileConfig, Compiler
from architect.compiler.verification import verify
from architect.floors import EvaluationCase, HeldOutTasks
from architect.library import HarnessLibrary
from architect.models import FakeAdapter, ModelRequest, ModelResponse, UsageQuote
from architect.presets import get_preset
from architect.registry import Registry
from architect.runtime import ArtifactExpiredError, Runtime
from architect.spec import Budget, FloorDefinition, HarnessArtifact, Spec
from architect.telemetry import TelemetryStore, categorical

FIXTURES = Path(__file__).parent / "fixtures" / "phase4"


class RoutedFake(FakeAdapter):
    """Fixture-only routing models a costly optional probe, not a provider."""

    def __init__(self, model: str = "fake") -> None:
        super().__init__(model=model)
        self.probe = FakeAdapter(model=model, fixture=FIXTURES / "probe.json")
        self.main = FakeAdapter(model=model, fixture=FIXTURES / "main.json")

    def chosen(self, request: ModelRequest) -> FakeAdapter:
        return self.probe if '"probe_analysis"' in request.prompt else self.main

    def quote(self, request: ModelRequest) -> UsageQuote:
        return self.chosen(request).quote(request)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return await self.chosen(request).complete(request)


def floor_factory(definition: FloorDefinition) -> HeldOutTasks:
    return HeldOutTasks(
        definition,
        [EvaluationCase(case_id=f"case-{i}", task={"action": i}, expected="ok") for i in range(4)],
    )


def smoke_factory(definition: FloorDefinition) -> HeldOutTasks:
    return HeldOutTasks(
        definition,
        [
            EvaluationCase(case_id=f"smoke-{i}", task={"action": i + 100}, expected="ok")
            for i in range(4)
        ],
    )


async def test_budget_failure_rewrites_then_reuses_with_fresh_verification(tmp_path: Path) -> None:
    spec = Spec.model_validate_json((FIXTURES / "spec.json").read_text())
    # Legacy tokens=6 is ignored; probe + main costs $0.011 and exceeds $0.0105.
    assert spec.budget.cost_usd == 0.0105
    assert "tokens" not in spec.budget.model_dump()
    runtime = Runtime(TelemetryStore(tmp_path / "telemetry"))
    library = HarnessLibrary(tmp_path / "library", promotion_reuses=1)
    proposal = FakeAdapter(fixture=FIXTURES / "proposal.json")
    compiler = Compiler(
        tmp_path / "artifacts",
        runtime,
        RoutedFake,
        floor_factory,
        config=CompileConfig(max_candidates=1),
        library=library,
        rewrite_adapter=proposal,
        smoke_factory=smoke_factory,
    )
    result = await compiler.compile(spec)
    artifact = result.artifact
    assert artifact is not None and artifact.licensed
    assert result.stages == (1, 2, 3, 4)
    assert artifact.execution_mode == "sandbox"
    assert artifact.components == ("interface_analysis",)
    assert proposal.calls == 1
    assert result.report is not None and result.report.mean_cost_usd == pytest.approx(0.001)
    events = [row for row in runtime.store.rows() if row.task_type == categorical("rewrite")]
    assert events and events[-1].accepted
    assert events[-1].cost_usd == 0
    assert (artifact.directory / "rewrite.diff").is_file()
    again = await compiler.compile(spec)
    assert again.warm_started and again.artifact is not None and again.artifact.licensed
    assert again.artifact.execution_mode == "sandbox"
    assert again.report is not None and again.report.held_out_count == 2
    assert again.report.verification_id != result.report.verification_id
    assert proposal.calls == 1
    assert library.install_candidates()
    cached = library.lookup(spec, "fake")
    assert cached is not None
    with pytest.raises(ArtifactExpiredError):
        await runtime.run(cached, {}, RoutedFake())
    run = await runtime.run(again.artifact, {}, RoutedFake())
    assert run.output == "ok"


async def test_install_drift_recompile_rejection_and_rollback(tmp_path: Path) -> None:
    payload = json.loads((FIXTURES / "spec.json").read_text())
    payload.update(domain="synthesizer", lifecycle="install", budget=Budget().model_dump())
    payload["floor"]["limit"] = 0.8
    spec = Spec.model_validate(payload)
    runtime = Runtime(TelemetryStore(tmp_path / "telemetry"))

    async def make(version: int, cost: float) -> HarnessArtifact:
        directory = tmp_path / "artifacts" / spec.spec_hash / str(version)
        preset = get_preset("synthesizer")
        preset.emit(directory, spec, components=("synthesis",))
        artifact = HarnessArtifact(
            spec=spec,
            version=version,
            preset="synthesizer",
            model="fake",
            components=("synthesis",),
            params=preset.validate_params({}),
            directory=directory,
        )
        artifact.save()
        return await verify(
            artifact,
            floor_factory(spec.floor),
            lambda model: FakeAdapter(
                model=model,
                responses=[
                    ModelResponse(text="ok", tokens_in=1, tokens_out=1, cost_usd=cost, model=model)
                ],
            ),
            runtime,
        )

    old = await make(1, 0.001)
    worse = await make(2, 0.002)
    assert old.licensed and worse.licensed
    registry = Registry(tmp_path / "registry", window=4)
    registry.install("interface-agent", old)
    base = runtime.store.rows()[-1]
    rows = [
        base.model_copy(
            update={
                "run_id": str(uuid4()),
                "harness_version": old.version,
                "floor_score": score,
                "timestamp": datetime.now(UTC),
            }
        )
        for score in [0.9, 0.85, 0.82, 0.8]
    ]
    calls: list[Spec] = []

    async def recompile(value: Spec) -> HarnessArtifact:
        calls.append(value)
        return worse

    outcome = await registry.monitor("interface-agent", rows, recompile)
    assert outcome.event is not None and not outcome.accepted
    assert len(calls) == 1
    assert registry.get("interface-agent").artifact_digest == old.artifact_digest
    await registry.monitor("interface-agent", rows, recompile)
    assert len(calls) == 1
    better = await make(3, 0.0005)
    registry.install("interface-agent", better)
    assert registry.get("interface-agent").artifact_digest == better.artifact_digest
    registry.rollback("interface-agent")
    assert registry.get("interface-agent").artifact_digest == old.artifact_digest
