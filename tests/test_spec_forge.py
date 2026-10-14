"""Phase 9 day-0 binding models: ForgeBinding, SubharnessRef and HarnessArtifact.forge.

Golden digests were computed at commit d8d9fde, before ``forge`` existed, so they prove
that every artifact with ``forge=None`` keeps a byte-identical ``artifact_digest``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from architect._authority import _sign_report
from architect.spec import (
    SHA256,
    Budget,
    FloorDefinition,
    FloorKind,
    ForgeBinding,
    HarnessArtifact,
    Spec,
    SubharnessRef,
    TestReport,
)

FIXTURES = Path(__file__).parent / "fixtures"
SANDBOX_SOURCE = (
    b"async def run(context, task):\n"
    b'    await context.call("first")\n'
    b'    await context.call("second")\n'
    b'    return await context.call("final")\n'
)
GOLDEN_NATIVE = "0c6cd767788527b8d3b555d3e80a8bf3600c4a3252da1bc8c3111ddffb131742"
GOLDEN_SANDBOX = "22f2d9ebfb484fe3fd286005bc9df5535fa344bc18282334c9d30b041e46300d"
# The same sandbox artifact with the committed tests/fixtures/rewrites/baseline.py bytes.
GOLDEN_REWRITE_FIXTURE = "22f2d9ebfb484fe3fd286005bc9df5535fa344bc18282334c9d30b041e46300d"
GOLDEN_SANDBOX_SPEC = "becfd20763c58d1040b595bd693bb2d40bf7806ec57d3e9a18bc0ed80d4b3342"


@pytest.fixture(autouse=True)
def local_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    key = tmp_path / "authority" / "key"
    monkeypatch.setenv("ARCHITECT_SIGNING_KEY_FILE", str(key))
    return key


def native_artifact(root: Path) -> HarnessArtifact:
    directory = root / "native"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "harness.py").write_bytes(b"# fixed test code\n")
    spec = Spec(
        task="Transform a value",
        input_schema={"type": "string"},
        output_schema={"type": "string"},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.8, dataset="held-out"),
        budget=Budget(cost_usd=0.2, compute_s=2),
    )
    return HarnessArtifact(
        spec=spec,
        preset="toy",
        model="fake",
        components=("step",),
        directory=directory,
        params={"temperature": 0},
    )


def sandbox_artifact(root: Path, **changes: Any) -> HarnessArtifact:
    directory = root / "sandbox"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "harness.py").write_bytes(SANDBOX_SOURCE)
    spec = Spec.model_validate_json((FIXTURES / "phase4" / "spec.json").read_text("utf-8"))
    values: dict[str, Any] = {
        "spec": spec,
        "preset": "explorer",
        "model": "fake",
        "components": ("first", "second", "final"),
        "directory": directory,
        "execution_mode": "sandbox",
        "version": 2,
    }
    values.update(changes)
    return HarnessArtifact(**values)


def ref(**changes: Any) -> SubharnessRef:
    values: dict[str, Any] = {
        "artifact_digest": "a" * 64,
        "spec_hash": "b" * 64,
        "input_schema_sha256": "c" * 64,
        "output_schema_sha256": "d" * 64,
        "case_origin": "projection",
    }
    values.update(changes)
    return SubharnessRef(**values)


def binding(**changes: Any) -> ForgeBinding:
    values: dict[str, Any] = {"call_limit": 128, "fuel": 10_000_000}
    values.update(changes)
    return ForgeBinding(**values)


def licensed(artifact: HarnessArtifact) -> HarnessArtifact:
    report = TestReport(
        spec_hash=artifact.spec.spec_hash,
        artifact_digest=artifact.artifact_digest,
        passed=True,
        floor_score=1.0,
        floor_limit=artifact.spec.floor.limit,
        floor_kind=artifact.spec.floor.kind,
        tokens=1,
        cost_usd=0.01,
        compute_s=1,
        component_count=artifact.component_count,
        verification_id="test-verification",
    )
    signature = _sign_report(report.model_dump(mode="json", exclude={"signature"}))
    return artifact.model_copy(
        update={"report": report.model_copy(update={"signature": signature})}
    )


def test_golden_digests_hold_for_forge_none(tmp_path: Path) -> None:
    native = native_artifact(tmp_path)
    sandbox = sandbox_artifact(tmp_path)
    assert native.forge is None and sandbox.forge is None
    assert sandbox.spec.spec_hash == GOLDEN_SANDBOX_SPEC
    assert native.artifact_digest == GOLDEN_NATIVE
    assert sandbox.artifact_digest == GOLDEN_SANDBOX
    (sandbox.directory / "harness.py").write_bytes(
        (FIXTURES / "rewrites" / "baseline.py").read_bytes()
    )
    assert sandbox.artifact_digest == GOLDEN_REWRITE_FIXTURE


def test_config_omits_forge_when_none(tmp_path: Path) -> None:
    artifact = sandbox_artifact(tmp_path)
    assert "forge" not in artifact._config()
    artifact.save()
    config = json.loads((artifact.directory / "config.json").read_text("utf-8"))
    assert set(config) == {
        "version",
        "preset",
        "model",
        "components",
        "params",
        "execution_mode",
    }
    loaded = HarnessArtifact.load(artifact.directory)
    assert loaded.forge is None
    assert loaded.artifact_digest == GOLDEN_SANDBOX


def test_forge_is_in_config_and_digest_when_set(tmp_path: Path) -> None:
    plain = sandbox_artifact(tmp_path)
    forged = sandbox_artifact(tmp_path, forge=binding(subharnesses={"child": ref()}))
    config = forged._config()
    assert config["forge"] == {
        "language": 2,
        "call_limit": 128,
        "fuel": 10_000_000,
        "subharnesses": {
            "child": {
                "artifact_digest": "a" * 64,
                "spec_hash": "b" * 64,
                "input_schema_sha256": "c" * 64,
                "output_schema_sha256": "d" * 64,
                "case_origin": "projection",
            }
        },
    }
    assert forged.artifact_digest != plain.artifact_digest
    assert sandbox_artifact(tmp_path, forge=binding()).artifact_digest not in {
        plain.artifact_digest,
        forged.artifact_digest,
    }


@pytest.mark.parametrize(
    "changed",
    [
        {"call_limit": 129},
        {"fuel": 10_000_001},
        {"subharnesses": {"child": ref(artifact_digest="e" * 64)}},
        {"subharnesses": {"child": ref(case_origin="authored")}},
        {"subharnesses": {"other": ref()}},
    ],
)
def test_changing_the_binding_invalidates_the_license(
    tmp_path: Path, changed: dict[str, Any]
) -> None:
    artifact = licensed(sandbox_artifact(tmp_path, forge=binding(subharnesses={"child": ref()})))
    assert artifact.licensed
    assert artifact.forge is not None
    rebound = artifact.model_copy(update={"forge": artifact.forge.model_copy(update=changed)})
    assert not rebound.licensed
    assert not artifact.model_copy(update={"forge": None}).licensed


def test_forge_binding_round_trips_through_disk(tmp_path: Path) -> None:
    artifact = licensed(sandbox_artifact(tmp_path, forge=binding(subharnesses={"child": ref()})))
    artifact.save()
    loaded = HarnessArtifact.load(artifact.directory)
    assert loaded.forge == artifact.forge
    assert loaded.artifact_digest == artifact.artifact_digest
    assert loaded.licensed


def test_forge_binding_defaults_and_bounds() -> None:
    value = binding()
    assert value.language == 2
    assert value.subharnesses == {}
    assert binding(call_limit=1, fuel=100_000).call_limit == 1
    assert binding(call_limit=100_000, fuel=100_000_000).fuel == 100_000_000
    assert len(binding(subharnesses={f"c{i}": ref() for i in range(8)}).subharnesses) == 8


@pytest.mark.parametrize(
    "changes",
    [
        {"language": 1},
        {"language": 3},
        {"call_limit": 0},
        {"call_limit": 100_001},
        {"call_limit": 128.0},
        {"call_limit": "128"},
        {"call_limit": True},
        {"fuel": 99_999},
        {"fuel": 100_000_001},
        {"fuel": 1e7},
        {"subharnesses": {f"c{i}": ref() for i in range(9)}},
        {"subharnesses": {"Child": ref()}},
        {"subharnesses": {"1child": ref()}},
        {"subharnesses": {"child-x": ref()}},
        {"subharnesses": {"c" * 33: ref()}},
        {"subharnesses": {"": ref()}},
        {"extra": 1},
    ],
)
def test_forge_binding_rejects_out_of_contract_values(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        binding(**changes)


def test_subharness_name_boundaries() -> None:
    assert binding(subharnesses={"c" * 32: ref(), "a": ref(), "a_1": ref()}).subharnesses


@pytest.mark.parametrize(
    "changes",
    [
        {"artifact_digest": "A" * 64},
        {"spec_hash": "b" * 63},
        {"input_schema_sha256": "not-a-digest"},
        {"output_schema_sha256": "g" * 64},
        {"case_origin": "held_out"},
        {"note": "x"},
    ],
)
def test_subharness_ref_rejects_out_of_contract_values(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ref(**changes)


def test_models_are_frozen(tmp_path: Path) -> None:
    value = binding()
    with pytest.raises(ValidationError):
        value.call_limit = 1
    child = ref()
    with pytest.raises(ValidationError):
        child.case_origin = "authored"
    artifact = sandbox_artifact(tmp_path, forge=value)
    with pytest.raises(ValidationError):
        artifact.forge = None


def test_sha256_alias_is_the_digest_pattern() -> None:
    assert SHA256 is not None
    with pytest.raises(ValidationError):
        ref(spec_hash="0" * 65)


def test_load_rejects_an_invalid_forge_config(tmp_path: Path) -> None:
    artifact = sandbox_artifact(tmp_path, forge=binding())
    artifact.save()
    path = artifact.directory / "config.json"
    config = json.loads(path.read_text("utf-8"))
    config["forge"]["language"] = 1
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValidationError):
        HarnessArtifact.load(artifact.directory)
