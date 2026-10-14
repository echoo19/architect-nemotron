"""Phase 9 artifact binding beyond the day-0 models (tests/test_spec_forge.py).

Golden ``forge=None`` digests are pinned in tests/test_spec_forge.py; this file
covers the binding rules WP3 adds: a Forge artifact is always sandboxed, schema
digests are canonical, and any change to a bound child or to the binding breaks
the parent's license.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from architect._authority import _sign_report
from architect.spec import (
    Budget,
    FloorDefinition,
    FloorKind,
    ForgeBinding,
    HarnessArtifact,
    Spec,
    SubharnessRef,
    TestReport,
    schema_sha256,
)


def spec(**changes: Any) -> Spec:
    values: dict[str, Any] = {
        "task": "Classify",
        "input_schema": {"type": "string"},
        "output_schema": {"type": "string"},
        "floor": FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.8, dataset="d.jsonl"),
        "budget": Budget(cost_usd=0.5, compute_s=10),
    }
    values.update(changes)
    return Spec(**values)


def sign(artifact: HarnessArtifact) -> HarnessArtifact:
    report = TestReport(
        spec_hash=artifact.spec.spec_hash,
        artifact_digest=artifact.artifact_digest,
        passed=True,
        floor_score=1.0,
        floor_limit=artifact.spec.floor.limit,
        floor_kind=artifact.spec.floor.kind,
        tokens=1,
        cost_usd=0.0,
        compute_s=0.1,
        component_count=artifact.component_count,
        verification_id="v",
    )
    signature = _sign_report(report.model_dump(mode="json", exclude={"signature"}))
    signed = artifact.model_copy(
        update={"report": report.model_copy(update={"signature": signature})}
    )
    signed.save()
    assert signed.licensed
    return signed


def artifact(directory: Path, source: str, **changes: Any) -> HarnessArtifact:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "harness.py").write_text(source, encoding="utf-8")
    values: dict[str, Any] = {
        "spec": spec(),
        "preset": "extractor",
        "model": "fake",
        "components": (),
        "directory": directory,
    }
    values.update(changes)
    return HarnessArtifact(**values)


def test_forge_artifact_must_be_sandboxed(tmp_path: Path) -> None:
    binding = ForgeBinding(call_limit=8, fuel=100_000)
    with pytest.raises(ValidationError, match="sandbox"):
        artifact(tmp_path / "a", "x = 1\n", forge=binding)
    forged = artifact(tmp_path / "b", "x = 1\n", forge=binding, execution_mode="sandbox")
    forged.save()
    path = forged.directory / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["execution_mode"] = "native"
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValidationError):
        HarnessArtifact.load(forged.directory)


def test_schema_digest_is_canonical_json() -> None:
    schema = {"type": "object", "properties": {"b": {"type": "string"}, "a": {}}}
    reordered = {"properties": {"a": {}, "b": {"type": "string"}}, "type": "object"}
    expected = hashlib.sha256(
        json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert schema_sha256(schema) == schema_sha256(reordered) == expected
    assert schema_sha256({"type": "string"}) != schema_sha256({"type": "number"})


def test_a_changed_child_cannot_be_rebound_without_invalidating_the_parent(
    tmp_path: Path,
) -> None:
    parent_dir = tmp_path / "parent"
    child = sign(artifact(parent_dir / "subharnesses" / "helper", "async def run(c, t): ...\n"))
    ref = SubharnessRef.for_child(child, "authored")
    parent = sign(
        artifact(
            parent_dir,
            "async def run(context, task):\n    return await context.call(task)\n",
            execution_mode="sandbox",
            forge=ForgeBinding(call_limit=8, fuel=100_000, subharnesses={"helper": ref}),
        )
    )
    # The promoted layout (subharnesses/ next to the metadata) reloads unchanged.
    loaded = HarnessArtifact.load(parent.directory)
    assert loaded.licensed and loaded.forge == parent.forge
    # A child changed on disk no longer matches the bound digest ...
    (child.directory / "harness.py").write_text("async def run(c, t): return 1\n", "utf-8")
    changed = sign(HarnessArtifact.load(child.directory).model_copy(update={"report": None}))
    assert changed.artifact_digest != ref.artifact_digest
    # ... and binding the new digest changes the parent's own digest, so its license breaks.
    assert parent.forge is not None
    rebound = parent.model_copy(
        update={
            "forge": parent.forge.model_copy(
                update={"subharnesses": {"helper": SubharnessRef.for_child(changed, "authored")}}
            )
        }
    )
    assert rebound.artifact_digest != parent.artifact_digest
    assert not rebound.licensed and parent.licensed


def test_case_origin_is_part_of_the_license(tmp_path: Path) -> None:
    parent_dir = tmp_path / "parent"
    child = sign(artifact(parent_dir / "subharnesses" / "helper", "async def run(c, t): ...\n"))
    projection = SubharnessRef.for_child(child, "projection")
    parent = sign(
        artifact(
            parent_dir,
            "async def run(context, task):\n    return task\n",
            execution_mode="sandbox",
            forge=ForgeBinding(call_limit=8, fuel=100_000, subharnesses={"helper": projection}),
        )
    )
    assert parent.forge is not None
    relabelled = parent.forge.model_copy(
        update={
            "subharnesses": {"helper": projection.model_copy(update={"case_origin": "authored"})}
        }
    )
    assert not parent.model_copy(update={"forge": relabelled}).licensed
