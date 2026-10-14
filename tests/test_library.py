"""Synthetic authenticated fixtures; library operations never execute their source."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from architect._authority import _sign_report
from architect.library import HarnessLibrary, LibraryError
from architect.models import FakeAdapter
from architect.runtime import ArtifactExpiredError, Runtime
from architect.spec import (
    Budget,
    FloorDefinition,
    FloorKind,
    HarnessArtifact,
    Lifecycle,
    PipelineStep,
    Spec,
    TestReport,
)
from architect.telemetry import TelemetryStore


def specification(**changes: object) -> Spec:
    data: dict[str, object] = {
        "task": "Summarize the table",
        "input_schema": {"type": "object"},
        "output_schema": {"type": "string", "minLength": 1},
        "domain": "synthesizer",
        "tools": ("read", "calculate"),
        "floor": FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.8, dataset="fixture"),
        "budget": Budget(),
    }
    data.update(changes)
    return Spec.model_validate(data)


def licensed(root: Path, *, spec: Spec | None = None, version: int = 1) -> HarnessArtifact:
    directory = root / uuid4().hex
    directory.mkdir(parents=True)
    (directory / "harness.py").write_text(
        'async def run(context, task):\n    return "ok"\n', encoding="utf-8"
    )
    artifact = HarnessArtifact(
        spec=spec or specification(), directory=directory, preset="synthesizer", model="fake",
        version=version, components=("model-call",),
    )
    report = TestReport(
        spec_hash=artifact.spec.spec_hash, artifact_digest=artifact.artifact_digest,
        passed=True, floor_score=0.9, floor_limit=artifact.spec.floor.limit,
        floor_kind=artifact.spec.floor.kind, tokens=1, cost_usd=0, compute_s=0,
        component_count=artifact.component_count, verification_id=uuid4().hex,
    )
    signed = report.model_copy(update={
        "signature": _sign_report(report.model_dump(mode="json", exclude={"signature"}))
    })
    artifact = artifact.model_copy(update={"report": signed})
    artifact.save()
    assert artifact.licensed
    return artifact


def test_persistent_isolated_authenticated_snapshot(tmp_path: Path) -> None:
    original = licensed(tmp_path)
    library = HarnessLibrary(tmp_path / "library")
    key = library.store(original)
    assert key == original.artifact_digest
    stored = HarnessLibrary(library.root).lookup(original.spec, "fake")
    assert stored is not None and stored.licensed
    assert stored.directory != original.directory
    assert stored.report == original.report and stored.spec == original.spec
    assert not (original.directory / ".consumed").exists()
    (original.directory / "harness.py").write_text("corrupted", encoding="utf-8")
    again = library.lookup(original.spec, "fake")
    assert again is not None and again.licensed


def test_lookup_is_template_not_transferred_license(tmp_path: Path) -> None:
    original = licensed(tmp_path)
    library = HarnessLibrary(tmp_path / "library")
    library.store(original)
    target = specification(task="Summarize another table", budget=Budget(cost_usd=0.2))
    found = library.lookup(target, "fake")
    assert found is not None and found.spec == original.spec
    assert found.report == original.report
    assert not found.model_copy(update={"spec": target}).licensed


@pytest.mark.parametrize("changes", [
    {"domain": "extractor"},
    {"input_schema": {"type": "array"}},
    {"output_schema": {"type": "string", "minLength": 2}},
    {"tools": ("read",)},
    {"pipeline": (PipelineStep(name="first", preset="extractor", model="fake", budget=Budget()),)},
])
def test_incompatible_specs_do_not_match(tmp_path: Path, changes: dict[str, object]) -> None:
    original = licensed(tmp_path)
    library = HarnessLibrary(tmp_path / "library")
    library.store(original)
    assert library.lookup(specification(**changes), "fake") is None


def test_model_mismatch_does_not_match(tmp_path: Path) -> None:
    original = licensed(tmp_path)
    library = HarnessLibrary(tmp_path / "library")
    library.store(original)
    assert library.lookup(original.spec, "other-model") is None


def test_canonical_schema_and_tool_order(tmp_path: Path) -> None:
    original = licensed(tmp_path)
    library = HarnessLibrary(tmp_path / "library")
    library.store(original)
    target = specification(
        task="A different task", output_schema={"minLength": 1, "type": "string"},
        tools=("calculate", "read"),
    )
    assert library.lookup(target, "fake") is not None


def test_pipeline_model_and_budget_are_exact(tmp_path: Path) -> None:
    step = PipelineStep(name="first", preset="extractor", model="fake", budget=Budget())
    original = licensed(tmp_path, spec=specification(pipeline=(step,)))
    library = HarnessLibrary(tmp_path / "library")
    library.store(original)
    assert library.lookup(specification(task="Another task", pipeline=(step,)), "fake") is not None
    changed = step.model_copy(update={"budget": Budget(cost_usd=0.01)})
    assert library.lookup(specification(pipeline=(changed,)), "fake") is None


def test_deterministic_closest_then_oldest_tie(tmp_path: Path) -> None:
    library = HarnessLibrary(tmp_path / "library")
    oldest = licensed(tmp_path, spec=specification(task="Describe a picture"))
    close = licensed(tmp_path, spec=specification(task="Summarize table"))
    tied = licensed(tmp_path, spec=close.spec, version=2)
    for artifact in (oldest, close, tied):
        library.store(artifact)
    target = specification(task="Summarize table briefly")
    for current in (library, HarnessLibrary(library.root)):
        found = current.lookup(target, "fake")
        assert found is not None and found.artifact_digest == close.artifact_digest
    # Exact spec beats the same task under a different budget, despite age.
    exact = licensed(tmp_path, spec=target)
    library.store(exact)
    found = library.lookup(target, "fake")
    assert found is not None and found.artifact_digest == exact.artifact_digest


def test_promotion_idempotent_store_and_cumulative_eviction(tmp_path: Path) -> None:
    original = licensed(tmp_path)
    library = HarnessLibrary(tmp_path / "library", promotion_reuses=2)
    key = library.store(original)
    template = library.lookup(original.spec, "fake")
    assert template is not None
    assert not library.install_candidates()
    library.record_verification(template, True)
    library.record_verification(template, False)
    assert library.store(original) == key
    restarted = HarnessLibrary(library.root, promotion_reuses=2)
    assert not restarted.install_candidates()
    restarted.record_verification(template, True)
    assert restarted.install_candidates() == (key,)
    restarted.record_verification(template, False)
    assert restarted.lookup(original.spec, "fake") is None
    assert not restarted.install_candidates()
    restarted.record_verification(template, True)
    restarted.record_verification(template, False)
    assert restarted.store(original) == key
    assert HarnessLibrary(library.root).lookup(original.spec, "fake") is None


def test_unknown_or_reportless_record_is_harmless(tmp_path: Path) -> None:
    artifact = licensed(tmp_path)
    library = HarnessLibrary(tmp_path / "library")
    library.record_verification(artifact, True)
    library.record_verification(artifact.model_copy(update={"report": None}), False)
    assert not library.install_candidates()
    with pytest.raises(LibraryError, match="boolean"):
        library.record_verification(artifact, 1)  # type: ignore[arg-type]


@pytest.mark.parametrize("name,contents", [
    ("harness.py", "corrupt"), ("spec.json", "{}"), ("config.json", "[]"),
    ("report.json", "{}"), ("report.json", None),
])
def test_corrupt_snapshot_evicted_and_fallback_returned(
    tmp_path: Path, name: str, contents: str | None,
) -> None:
    library = HarnessLibrary(tmp_path / "library")
    original = licensed(tmp_path)
    fallback = licensed(tmp_path, version=2)
    library.store(original)
    library.store(fallback)
    template = library.lookup(original.spec, "fake")
    assert template is not None
    path = template.directory / name
    before = path.read_bytes()
    if contents is None:
        path.unlink()
    else:
        path.write_text(contents, encoding="utf-8")
    found = library.lookup(original.spec, "fake")
    assert found is not None and found.artifact_digest == fallback.artifact_digest
    path.write_bytes(before)
    found = HarnessLibrary(library.root).lookup(original.spec, "fake")
    assert found is not None and found.artifact_digest == fallback.artifact_digest


def test_promoted_corrupt_snapshot_not_returned(tmp_path: Path) -> None:
    original = licensed(tmp_path)
    library = HarnessLibrary(tmp_path / "library", promotion_reuses=1)
    library.store(original)
    template = library.lookup(original.spec, "fake")
    assert template is not None
    library.record_verification(template, True)
    (template.directory / "harness.py").unlink()
    assert not library.install_candidates()
    library.record_verification(template, True)


def test_store_requires_disk_license_and_matching_metadata(tmp_path: Path) -> None:
    original = licensed(tmp_path)
    library = HarnessLibrary(tmp_path / "library")
    with pytest.raises(LibraryError):
        library.store(original.model_copy(update={"report": None}))
    changed = original.model_copy(update={"spec": specification(task="Unverified new task")})
    with pytest.raises(LibraryError):
        library.store(changed)
    (original.directory / "report.json").unlink()
    with pytest.raises(LibraryError):
        library.store(original)
    (original.directory / ".consumed").write_text("not authority", encoding="utf-8")
    with pytest.raises(LibraryError):
        library.store(original)
    assert library.lookup(original.spec, "fake") is None


@pytest.mark.parametrize("already_consumed", [True, False])
async def test_expire_snapshot_never_grants_another_run(
    tmp_path: Path, already_consumed: bool,
) -> None:
    original = licensed(tmp_path)
    if already_consumed:
        (original.directory / ".consumed").write_text("caller marker", encoding="utf-8")
    library = HarnessLibrary(tmp_path / "library")
    library.store(original)
    template = library.lookup(original.spec, "fake")
    assert template is not None and template.licensed
    assert "caller marker" not in (template.directory / ".consumed").read_text(encoding="utf-8")
    runtime = Runtime(TelemetryStore(tmp_path / "telemetry"))
    with pytest.raises(ArtifactExpiredError):
        await runtime.run(template, {}, FakeAdapter())
    if not already_consumed:
        assert (await runtime.run(original, {}, FakeAdapter())).output == "ok"
    # Missing markers are safely recreated before returning a template.
    (template.directory / ".consumed").unlink()
    assert library.lookup(original.spec, "fake") is not None
    assert (template.directory / ".consumed").exists()


def test_install_snapshot_does_not_inherit_consumption_marker(tmp_path: Path) -> None:
    original = licensed(tmp_path, spec=specification(lifecycle=Lifecycle.INSTALL))
    (original.directory / ".consumed").write_text("caller marker", encoding="utf-8")
    library = HarnessLibrary(tmp_path / "library")
    library.store(original)
    template = library.lookup(original.spec, "fake")
    assert template is not None and not (template.directory / ".consumed").exists()


def test_concurrent_store_and_counts_are_serialized(tmp_path: Path) -> None:
    original = licensed(tmp_path)
    library = HarnessLibrary(tmp_path / "library", promotion_reuses=8)
    with ThreadPoolExecutor(max_workers=4) as pool:
        keys = tuple(pool.map(lambda _: library.store(original), range(8)))
        assert len(set(keys)) == 1
        tuple(pool.map(lambda _: library.record_verification(original, True), range(8)))
    assert library.install_candidates() == (keys[0],)


@pytest.mark.parametrize("count", [0, -1, True, 1.5])
def test_invalid_promotion_threshold(tmp_path: Path, count: int) -> None:
    with pytest.raises(LibraryError):
        HarnessLibrary(tmp_path / "library", promotion_reuses=count)


def test_invalid_storage_root(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.touch()
    with pytest.raises(LibraryError):
        HarnessLibrary(path)


def test_disk_metadata_mismatch_rejected(tmp_path: Path) -> None:
    original = licensed(tmp_path)
    library = HarnessLibrary(tmp_path / "library")
    # Provenance is not part of the signed spec hash, but disk and input must agree.
    changed_spec = original.spec.model_copy(update={"provenance": {"task": "inferred"}})
    changed = original.model_copy(update={"spec": changed_spec})
    assert changed.licensed
    with pytest.raises(LibraryError, match="artifact_disk_mismatch"):
        library.store(changed)


def test_failed_copy_is_not_published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil

    original = licensed(tmp_path)
    library = HarnessLibrary(tmp_path / "library")
    copyfile = shutil.copyfile

    def corrupt_copy(source: Path, destination: Path) -> Path:
        result = copyfile(source, destination)
        if destination.name == "harness.py":
            destination.write_text("corrupt", encoding="utf-8")
        return result

    monkeypatch.setattr("architect.library.shutil.copyfile", corrupt_copy)
    with pytest.raises(LibraryError, match="snapshot_authentication_failed"):
        library.store(original)
    assert not tuple((library.root / "snapshots").iterdir())
    assert library.lookup(original.spec, "fake") is None


def test_missing_source_record_is_harmless(tmp_path: Path) -> None:
    original = licensed(tmp_path)
    library = HarnessLibrary(tmp_path / "library", promotion_reuses=1)
    library.store(original)
    template = library.lookup(original.spec, "fake")
    assert template is not None
    (template.directory / "config.json").unlink()
    library.record_verification(template, False)
    library.record_verification(template, False)
    assert library.lookup(original.spec, "fake") is None


def test_database_corruption_fails_cleanly(tmp_path: Path) -> None:
    library = HarnessLibrary(tmp_path / "library")
    (library.root / "library.sqlite3").write_bytes(b"not sqlite")
    with pytest.raises(LibraryError, match="library_database_failed"):
        library.lookup(specification(), "fake")


def test_duplicate_store_does_not_revive_corrupt_snapshot(tmp_path: Path) -> None:
    original = licensed(tmp_path)
    library = HarnessLibrary(tmp_path / "library")
    library.store(original)
    template = library.lookup(original.spec, "fake")
    assert template is not None
    (template.directory / "harness.py").write_text("corrupt", encoding="utf-8")
    assert library.store(original) == original.artifact_digest
    assert library.lookup(original.spec, "fake") is None


# ---------------------------------------------------------------------------
# Phase 9: Forge artifacts carry subharnesses/; per-spec verification counter.
# ---------------------------------------------------------------------------


def _sign(artifact: HarnessArtifact) -> HarnessArtifact:
    report = TestReport(
        spec_hash=artifact.spec.spec_hash, artifact_digest=artifact.artifact_digest,
        passed=True, floor_score=0.9, floor_limit=artifact.spec.floor.limit,
        floor_kind=artifact.spec.floor.kind, tokens=1, cost_usd=0, compute_s=0,
        component_count=artifact.component_count, verification_id=uuid4().hex,
    )
    signature = _sign_report(report.model_dump(mode="json", exclude={"signature"}))
    signed = artifact.model_copy(
        update={"report": report.model_copy(update={"signature": signature})}
    )
    signed.save()
    assert signed.licensed
    return signed


def _forge_artifact(
    directory: Path, children: dict[str, HarnessArtifact], *, spec: Spec | None = None
) -> HarnessArtifact:
    from architect.spec import ForgeBinding, SubharnessRef

    directory.mkdir(parents=True, exist_ok=True)
    (directory / "harness.py").write_text(
        'async def run(context, task):\n    return await context.harness("a", task)\n',
        encoding="utf-8",
    )
    return _sign(HarnessArtifact(
        spec=spec or specification(), directory=directory, preset="synthesizer", model="fake",
        components=(), execution_mode="sandbox",
        forge=ForgeBinding(call_limit=8, fuel=100_000, subharnesses={
            name: SubharnessRef.for_child(child, "authored") for name, child in children.items()
        }),
    ))


def _nested_forge(root: Path) -> HarnessArtifact:
    """parent -> subharnesses/a (a Forge child) -> subharnesses/b (a native grandchild)."""
    parent = root / uuid4().hex
    child_dir = parent / "subharnesses" / "a"
    grandchild_dir = child_dir / "subharnesses" / "b"
    grandchild_dir.mkdir(parents=True)
    (grandchild_dir / "harness.py").write_text(
        "async def run(context, task):\n    return 'leaf'\n", encoding="utf-8"
    )
    child_spec = specification(task="Child task", tools=())
    grandchild = _sign(HarnessArtifact(
        spec=specification(task="Leaf task", tools=()), directory=grandchild_dir,
        preset="synthesizer", model="fake", components=("model-call",),
    ))
    child = _forge_artifact(child_dir, {"b": grandchild}, spec=child_spec)
    return _forge_artifact(parent, {"a": child})


def test_store_and_snapshot_copy_subharnesses_recursively(tmp_path: Path) -> None:
    original = _nested_forge(tmp_path)
    library = HarnessLibrary(tmp_path / "library")
    key = library.store(original)
    found = HarnessLibrary(library.root).lookup(original.spec, "fake")
    assert found is not None and found.licensed and found.artifact_digest == key
    assert found.directory != original.directory
    child = HarnessArtifact.load(found.directory / "subharnesses" / "a")
    grandchild = HarnessArtifact.load(child.directory / "subharnesses" / "b")
    assert child.licensed and grandchild.licensed
    assert original.forge is not None and child.forge is not None
    assert child.artifact_digest == original.forge.subharnesses["a"].artifact_digest
    assert grandchild.artifact_digest == child.forge.subharnesses["b"].artifact_digest
    # The snapshot is isolated: changing the source tree afterwards changes nothing.
    (original.directory / "subharnesses" / "a" / "harness.py").write_text("x", encoding="utf-8")
    again = library.lookup(original.spec, "fake")
    assert again is not None and again.licensed


def test_store_refuses_a_parent_whose_bound_child_changed(tmp_path: Path) -> None:
    original = _nested_forge(tmp_path)
    leaf = original.directory / "subharnesses" / "a" / "subharnesses" / "b" / "harness.py"
    leaf.write_text("async def run(context, task):\n    return 'changed'\n", encoding="utf-8")
    library = HarnessLibrary(tmp_path / "library")
    with pytest.raises(LibraryError, match="snapshot_authentication_failed"):
        library.store(original)
    assert library.lookup(original.spec, "fake") is None


def test_snapshot_with_a_tampered_child_is_evicted(tmp_path: Path) -> None:
    original = _nested_forge(tmp_path)
    library = HarnessLibrary(tmp_path / "library")
    library.store(original)
    found = library.lookup(original.spec, "fake")
    assert found is not None
    (found.directory / "subharnesses" / "a" / "harness.py").write_text("x", encoding="utf-8")
    assert library.lookup(original.spec, "fake") is None


def test_forge_verification_count_persists(tmp_path: Path) -> None:
    spec_hash = specification().spec_hash
    library = HarnessLibrary(tmp_path / "library")
    assert library.verification_count(spec_hash) == 0
    library.record_forge_verification(spec_hash)
    library.record_forge_verification(spec_hash)
    reopened = HarnessLibrary(tmp_path / "library")
    assert reopened.verification_count(spec_hash) == 2
    assert reopened.verification_count("0" * 64) == 0
    for invalid in ("x", "A" * 64, "0" * 63):
        with pytest.raises(LibraryError, match="invalid_spec_hash"):
            reopened.verification_count(invalid)
        with pytest.raises(LibraryError, match="invalid_spec_hash"):
            reopened.record_forge_verification(invalid)
