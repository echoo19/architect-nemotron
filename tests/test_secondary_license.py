from pathlib import Path

import pytest

from architect._authority import _sign_report
from architect.spec import Budget, FloorDefinition, FloorKind, HarnessArtifact, Spec, TestReport


@pytest.mark.parametrize(
    "kind,limit,score,declared",
    [
        (FloorKind.HUMAN_EDIT_RATE, 0.2, 0.3, True),
        (FloorKind.HELD_OUT_TASKS, 0.8, 0.7, True),
        (FloorKind.HELD_OUT_TASKS, 0.8, 1, False),
    ],
)
def test_authenticated_but_inconsistent_secondary_evidence_cannot_license(
    tmp_path: Path,
    kind: FloorKind,
    limit: float,
    score: float,
    declared: bool,
) -> None:
    secondary = FloorDefinition(kind=kind, limit=limit, dataset="secondary")
    spec = Spec(
        task="task",
        input_schema={},
        output_schema={},
        budget=Budget(),
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="primary"),
        secondary_floor=secondary if declared else None,
    )
    (tmp_path / "harness.py").write_text("async def run(context, task):\n    return task\n")
    artifact = HarnessArtifact(
        spec=spec, preset="synthesizer", model="fake", components=("synthesis",), directory=tmp_path
    )
    report = TestReport(
        spec_hash=spec.spec_hash,
        artifact_digest=artifact.artifact_digest,
        passed=True,
        floor_score=1,
        floor_limit=1,
        floor_kind=spec.floor.kind,
        tokens=10**12,
        cost_usd=0,
        compute_s=0,
        component_count=1,
        verification_id="test",
        secondary_floor_kind=kind,
        secondary_floor_limit=limit,
        secondary_floor_score=score,
    )
    report = report.model_copy(
        update={"signature": _sign_report(report.model_dump(mode="json", exclude={"signature"}))}
    )
    assert not artifact.model_copy(update={"report": report}).licensed
