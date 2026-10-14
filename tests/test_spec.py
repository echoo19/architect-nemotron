"""Schema and artifact integrity tests. No model or network dependencies."""

import json
import math
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from architect._authority import _sign_report, verify_report
from architect.spec import (
    Budget,
    FloorDefinition,
    FloorKind,
    HarnessArtifact,
    PipelineStep,
    Spec,
    TestReport,
)


@pytest.fixture(autouse=True)
def local_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    key = tmp_path / "authority" / "key"
    monkeypatch.setenv("ARCHITECT_SIGNING_KEY_FILE", str(key))
    return key


@pytest.fixture
def spec() -> Spec:
    return Spec(
        task="Transform a value",
        input_schema={"type": "string"},
        output_schema={"type": "string"},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.8, dataset="held-out"),
        budget=Budget(cost_usd=0.2, compute_s=2),
    )


@pytest.fixture
def artifact(tmp_path: Path, spec: Spec) -> HarnessArtifact:
    directory = tmp_path / "artifact"
    directory.mkdir()
    (directory / "harness.py").write_text("# fixed test code\n", encoding="utf-8")
    return HarnessArtifact(
        spec=spec,
        preset="toy",
        model="fake",
        components=("step",),
        directory=directory,
        params={"temperature": 0},
    )


def unsigned_report(artifact: HarnessArtifact) -> TestReport:
    return TestReport(
        spec_hash=artifact.spec.spec_hash,
        artifact_digest=artifact.artifact_digest,
        passed=True,
        floor_score=0.9,
        floor_limit=artifact.spec.floor.limit,
        floor_kind=artifact.spec.floor.kind,
        tokens=20,
        cost_usd=0.2,
        compute_s=2,
        component_count=artifact.component_count,
        verification_id="test-verification",
    )


def signed(artifact: HarnessArtifact, **changes: object) -> HarnessArtifact:
    values = unsigned_report(artifact).model_dump(mode="json", exclude={"signature"})
    values.update(changes)
    report = TestReport.model_validate(values)
    signature = _sign_report(report.model_dump(mode="json", exclude={"signature"}))
    return artifact.model_copy(
        update={"report": report.model_copy(update={"signature": signature})}
    )


def test_spec_hash_is_canonical_and_excludes_provenance(spec: Spec) -> None:
    changed = spec.model_copy(update={"provenance": {"task": "user"}})
    assert changed.spec_hash == spec.spec_hash
    assert spec.model_copy(update={"task": "Other task"}).spec_hash != spec.spec_hash
    first = spec.model_copy(update={"input_schema": {"a": 1, "b": 2}})
    second = spec.model_copy(update={"input_schema": {"b": 2, "a": 1}})
    assert first.spec_hash == second.spec_hash


@pytest.mark.parametrize("field", ["cost_usd", "compute_s"])
@pytest.mark.parametrize("value", [0, -1, math.nan, math.inf, -math.inf])
def test_invalid_budgets(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        Budget.model_validate({field: value})


def test_schema_forbids_extras_and_frozen_mutation(spec: Spec) -> None:
    with pytest.raises(ValidationError):
        Spec.model_validate({**spec.model_dump(), "license": True})
    with pytest.raises(ValidationError):
        spec.task = "modified"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        Spec.model_validate({**spec.model_dump(), "task": "   "})
    with pytest.raises(ValidationError):
        Budget.model_validate({"unexpected": True})


@pytest.mark.parametrize("legacy", [1, 0, -1, True, None, "ignored", {"quota": 10}])
def test_budget_discards_legacy_tokens_without_mutating_input(legacy: object) -> None:
    values = {"tokens": legacy, "cost_usd": 0.2, "compute_s": 2}
    budget = Budget.model_validate(values)
    assert values["tokens"] is legacy
    assert budget.model_dump() == {"cost_usd": 0.2, "compute_s": 2}
    assert "tokens" not in budget.model_dump_json()
    assert not hasattr(budget, "tokens")
    assert set(Budget.model_json_schema()["properties"]) == {"cost_usd", "compute_s"}


def test_legacy_tokens_do_not_affect_spec_hash_or_nested_schema(spec: Spec) -> None:
    values = spec.model_dump()
    values["budget"]["tokens"] = 1
    assert Spec.model_validate(values).spec_hash == spec.spec_hash
    assert set(Spec.model_json_schema()["$defs"]["Budget"]["properties"]) == {
        "cost_usd", "compute_s"
    }


@pytest.mark.parametrize("limit", [-0.01, 1.01, math.inf, math.nan])
def test_invalid_floor_limits(limit: float) -> None:
    with pytest.raises(ValidationError):
        FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=limit, dataset="test")


def test_no_license_constructor_input(artifact: HarnessArtifact) -> None:
    with pytest.raises(ValidationError):
        HarnessArtifact.model_validate({**artifact.model_dump(), "licensed": True})


def test_absent_and_unsigned_reports_do_not_license(artifact: HarnessArtifact) -> None:
    assert not artifact.licensed
    report = unsigned_report(artifact)
    assert not artifact.model_copy(update={"report": report}).licensed
    assert not artifact.model_copy(
        update={"report": report.model_copy(update={"signature": "0" * 64})}
    ).licensed


def test_high_observed_tokens_do_not_revoke_license(artifact: HarnessArtifact) -> None:
    licensed = signed(artifact, tokens=10**12)
    assert licensed.licensed
    licensed.save()
    loaded = HarnessArtifact.load(artifact.directory)
    assert loaded.licensed
    assert loaded.report is not None
    assert loaded.report.tokens == 10**12


def test_pipeline_fields_components_and_execution_mode_roundtrip(
    artifact: HarnessArtifact,
) -> None:
    step = PipelineStep(
        name="first",
        preset="synthesizer",
        model="fake",
        budget=Budget.model_validate({"tokens": 1}),
        input_schema={"type": "string"},
        output_schema={"type": "string"},
        params={"temperature": 0.2},
    )
    updated = artifact.model_copy(update={
        "spec": artifact.spec.model_copy(update={"pipeline": (step,)}),
        "execution_mode": "sandbox",
    })
    assert updated.component_count == 2
    licensed = signed(updated, tokens=10**12)
    assert licensed.licensed
    assert not signed(updated, component_count=1).licensed
    licensed.save()
    loaded = HarnessArtifact.load(artifact.directory)
    assert loaded == licensed
    assert loaded.licensed
    assert loaded.spec.pipeline[0] == step
    assert loaded.execution_mode == "sandbox"
    values = updated.spec.model_dump()
    values["pipeline"] = [step.model_dump(), step.model_dump()]
    with pytest.raises(ValidationError, match="names must be unique"):
        Spec.model_validate(values)


def test_signed_report_and_roundtrip(artifact: HarnessArtifact) -> None:
    licensed = signed(artifact)
    assert licensed.licensed
    licensed.save()
    loaded = HarnessArtifact.load(artifact.directory)
    assert loaded.licensed
    assert loaded == licensed
    assert loaded.artifact_digest == licensed.artifact_digest
    assert loaded.report is not None
    with pytest.raises(ValidationError):
        loaded.report.passed = False  # type: ignore[misc]


@pytest.mark.parametrize(
    "changes",
    [
        {"passed": False},
        {"floor_score": 0.1},
        {"floor_limit": 0.9},
        {"floor_kind": "human_edit_rate"},
        {"cost_usd": 0.21},
        {"compute_s": 2.1},
        {"component_count": 2},
        {"failures": ["failed"]},
        {"spec_hash": "0" * 64},
        {"artifact_digest": "0" * 64},
    ],
)
def test_signed_report_still_must_pass_current_gates(
    artifact: HarnessArtifact, changes: dict[str, object]
) -> None:
    assert not signed(artifact, **changes).licensed


@pytest.mark.parametrize(
    "field,value",
    [
        ("spec_hash", "0" * 64),
        ("artifact_digest", "0" * 64),
        ("passed", False),
        ("floor_score", 1.0),
        ("floor_limit", 0.1),
        ("floor_kind", "human_edit_rate"),
        ("tokens", 0),
        ("cost_usd", 0),
        ("compute_s", 0),
        ("component_count", 0),
        ("failures", ["changed"]),
        ("verification_id", "other"),
    ],
)
def test_signature_authenticates_every_report_field(
    artifact: HarnessArtifact, field: str, value: object
) -> None:
    licensed = signed(artifact)
    assert licensed.report is not None
    values = licensed.report.model_dump(mode="json")
    values[field] = value
    altered = TestReport.model_validate(values)
    assert not verify_report(
        altered.model_dump(mode="json", exclude={"signature"}), altered.signature
    )
    assert not licensed.model_copy(update={"report": altered}).licensed


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", 2),
        ("preset", "other"),
        ("model", "other"),
        ("components", ("other",)),
        ("params", {"temperature": 1}),
        ("execution_mode", "sandbox"),
    ],
)
def test_config_mutation_revokes_license(
    artifact: HarnessArtifact, field: str, value: object
) -> None:
    licensed = signed(artifact)
    assert not licensed.model_copy(update={field: value}).licensed


def test_nested_mutation_is_detected(artifact: HarnessArtifact) -> None:
    licensed = signed(artifact)
    licensed.params["temperature"] = 1
    assert not licensed.licensed
    second = signed(artifact)
    second.spec.input_schema["type"] = "number"
    assert not second.licensed


def test_code_modification_and_missing_code_revoke_license(artifact: HarnessArtifact) -> None:
    licensed = signed(artifact)
    code = artifact.directory / "harness.py"
    code.write_bytes(code.read_bytes() + b"# changed\n")
    assert not licensed.licensed
    code.unlink()
    assert not licensed.licensed


def test_disk_config_modification_revokes_license(artifact: HarnessArtifact) -> None:
    licensed = signed(artifact)
    licensed.save()
    path = artifact.directory / "config.json"
    values = json.loads(path.read_text(encoding="utf-8"))
    values["model"] = "other"
    path.write_text(json.dumps(values), encoding="utf-8")
    assert not HarnessArtifact.load(artifact.directory).licensed


def test_disk_report_modification_revokes_license(artifact: HarnessArtifact) -> None:
    licensed = signed(artifact)
    licensed.save()
    path = artifact.directory / "report.json"
    values = json.loads(path.read_text(encoding="utf-8"))
    values["tokens"] = 0
    path.write_text(json.dumps(values), encoding="utf-8")
    assert not HarnessArtifact.load(artifact.directory).licensed


def test_no_report_save_removes_stale_report(artifact: HarnessArtifact) -> None:
    signed(artifact).save()
    artifact.save()
    assert not (artifact.directory / "report.json").exists()
    assert not HarnessArtifact.load(artifact.directory).licensed


def test_edit_rate_is_lower_is_better(artifact: HarnessArtifact) -> None:
    floor = artifact.spec.floor.model_copy(update={"kind": FloorKind.HUMAN_EDIT_RATE, "limit": 0.2})
    changed = artifact.model_copy(
        update={"spec": artifact.spec.model_copy(update={"floor": floor})}
    )
    assert signed(changed, floor_score=0.2).licensed
    assert signed(changed, floor_score=0).licensed
    assert not signed(changed, floor_score=0.21).licensed


def test_secondary_floor_fails_closed(artifact: HarnessArtifact) -> None:
    changed = artifact.model_copy(
        update={"spec": artifact.spec.model_copy(update={"secondary_floor": artifact.spec.floor})}
    )
    assert not signed(changed).licensed


def test_key_persistence_and_no_key_creation_on_verification(local_key: Path) -> None:
    payload: dict[str, object] = {"passed": True, "cost": 0.1}
    assert not verify_report(payload, "0" * 64)
    assert not local_key.exists()
    signature = _sign_report(payload)
    saved = local_key.read_bytes()
    assert len(saved) == 32
    assert _sign_report(payload) == signature
    assert verify_report(payload, signature)
    assert local_key.read_bytes() == saved
    if os.name == "posix":
        assert local_key.stat().st_mode & 0o777 == 0o600
    local_key.write_bytes(b"changed key bytes".ljust(32, b"!"))
    assert not verify_report(payload, signature)


@pytest.mark.parametrize(
    "field", ["floor_score", "floor_limit", "tokens", "cost_usd", "compute_s"]
)
@pytest.mark.parametrize("value", [math.nan, math.inf, -1])
def test_report_rejects_nonfinite_and_negative_usage(
    artifact: HarnessArtifact, field: str, value: float
) -> None:
    values = unsigned_report(artifact).model_dump(mode="json")
    values[field] = value
    with pytest.raises(ValidationError):
        TestReport.model_validate(values)


@pytest.mark.parametrize("config", [[], {"licensed": True}, {"directory": "elsewhere"}])
def test_load_rejects_invalid_config_shape(
    artifact: HarnessArtifact, config: object
) -> None:
    artifact.save()
    (artifact.directory / "config.json").write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid artifact configuration"):
        HarnessArtifact.load(artifact.directory)


def test_invalid_key_fails_closed_without_replacing_it(local_key: Path) -> None:
    local_key.parent.mkdir(parents=True)
    local_key.write_bytes(b"truncated")
    payload: dict[str, object] = {"passed": True}
    assert not verify_report(payload, "0" * 64)
    with pytest.raises(ValueError, match="Invalid local signing key"):
        _sign_report(payload)
    assert local_key.read_bytes() == b"truncated"
