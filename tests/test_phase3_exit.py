from pathlib import Path

from architect.compiler import CompileConfig, Compiler
from architect.floors import HumanEditRate
from architect.models import FakeAdapter
from architect.runtime import Runtime
from architect.spec import FloorDefinition, HarnessArtifact
from architect.telemetry import TelemetryStore

FIXTURES = Path(__file__).parent / "fixtures"


def make_compiler(tmp_path: Path) -> Compiler:
    def floor_factory(definition: FloorDefinition) -> HumanEditRate:
        return HumanEditRate.from_jsonl(definition, FIXTURES / "summary_edits.jsonl")

    return Compiler(
        tmp_path / "artifacts",
        Runtime(TelemetryStore(tmp_path / "telemetry")),
        lambda model: FakeAdapter(fixture=FIXTURES / "summary_response.json"),
        floor_factory,
        inference_adapter=FakeAdapter(fixture=FIXTURES / "summary_inference.json"),
        config=CompileConfig(max_candidates=8),
    )


async def test_infer_tune_verify_run_then_lower_budget(tmp_path: Path) -> None:
    compiler = make_compiler(tmp_path)
    result = await compiler.compile("summarize a table of numbers into two paragraphs for a parent")
    assert result.spec.domain == "synthesizer"
    assert result.spec.floor.kind.value == "human_edit_rate"
    assert result.stages == (0, 1, 2, 4)
    artifact = result.artifact
    assert artifact is not None and artifact.licensed
    assert result.report is not None and result.report.held_out_count == 100
    assert artifact.directory.parent.name == result.spec.spec_hash
    assert HarnessArtifact.load(artifact.directory).licensed
    run = await compiler.runtime.run(
        artifact, {"numbers": [1, 2]}, FakeAdapter(fixture=FIXTURES / "summary_response.json")
    )
    assert isinstance(run.output, str) and len(run.output.split("\n\n")) == 2
    lower = await compiler.compile(result.spec, overrides={"budget": {"cost_usd": 0.00001}})
    assert lower.artifact is not None and not lower.artifact.licensed
    assert lower.report is not None and not lower.report.passed
    assert lower.report.failures
    assert 4 in lower.stages
