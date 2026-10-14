"""Authenticated registry acceptance, durability, rollback and observed drift."""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

import pytest

from architect.compiler import CompileConfig, Compiler
from architect.compiler.verification import _signed
from architect.floors import EvaluationCase, HeldOutTasks
from architect.models import FakeAdapter
from architect.registry import DriftEvent, DriftResult, Registry, RegistryEntry, RegistryError
from architect.runtime import Runtime
from architect.spec import (
    Budget,
    FloorDefinition,
    FloorKind,
    HarnessArtifact,
    Lifecycle,
    PipelineStep,
    Spec,
)
from architect.spec import TestReport as Report
from architect.telemetry import TelemetryRow, TelemetryStore, categorical


@pytest.fixture
def make(tmp_path: Path) -> Callable[..., HarnessArtifact]:
    counter = 0

    def create(*, spec: Spec | None = None, score: float = .8, cost: float = .04,
               mean: float | None = .03, components: tuple[str, ...] = ("a", "b"),
               secondary: float | None = None, model: str = "fake") -> HarnessArtifact:
        nonlocal counter
        counter += 1
        if spec is None:
            spec = Spec(task="Map values", input_schema={"type": "string"},
                        output_schema={"type": "string"}, lifecycle=Lifecycle.INSTALL,
                        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS,
                                              dataset="heldout", limit=.5),
                        budget=Budget(cost_usd=.1, compute_s=10))
        directory = tmp_path / "sources" / str(counter)
        directory.mkdir(parents=True)
        (directory / "harness.py").write_text("async def run(context, task):\n    return task\n")
        artifact = HarnessArtifact(spec=spec, version=counter, directory=directory,
                                   preset="synthesizer", model=model, components=components)
        report = _signed(Report(
            spec_hash=spec.spec_hash, artifact_digest=artifact.artifact_digest,
            passed=True, floor_score=score, floor_limit=spec.floor.limit,
            floor_kind=spec.floor.kind, tokens=10, cost_usd=cost, mean_cost_usd=mean,
            compute_s=1, component_count=artifact.component_count, verification_id=uuid4().hex,
            held_out_count=30, secondary_floor_score=secondary,
            secondary_floor_kind=spec.secondary_floor.kind if spec.secondary_floor else None,
            secondary_floor_limit=spec.secondary_floor.limit if spec.secondary_floor else None,
        ))
        artifact = artifact.model_copy(update={"report": report})
        artifact.save()
        return artifact

    return create


def row(artifact: HarnessArtifact, score: float | None, *, model: str = "fake",
        role: Literal["primary", "secondary"] = "primary") -> TelemetryRow:
    floor = artifact.spec.floor if role == "primary" else artifact.spec.secondary_floor
    assert floor is not None
    return TelemetryRow(
        run_id=str(uuid4()), timestamp=datetime.now(UTC), domain="general", task_type="runtime",
        spec_hash=artifact.spec.spec_hash, harness_version=artifact.version,
        lifecycle=artifact.spec.lifecycle, preset_origin=categorical(artifact.preset),
        component_set=tuple(categorical(c) for c in artifact.components),
        model=categorical(model), model_params={}, tokens_in=1, tokens_out=1,
        cost_usd=.01, wall_time_s=.1, floor_type=floor.kind, floor_role=role,
        floor_score=score, floor_limit=floor.limit, accepted=True,
    )


def test_durable_snapshots_revisions_rollback_and_hashed_names(
    tmp_path: Path, make: Callable[..., HarnessArtifact],
) -> None:
    registry = Registry(tmp_path / "registry")
    assert registry.list() == ()
    first, second, third = make(), make(score=.9), make(score=1)
    name = "../../untrusted/name"
    entry = registry.install(name, first)
    assert isinstance(entry, RegistryEntry)
    assert entry.directory.is_relative_to(registry.root)
    assert entry.directory.parts[-3:] == ("artifacts", first.spec.spec_hash, str(first.version))
    assert registry.status(name) == entry
    assert registry.get(name).report == first.report
    assert first.directory.exists()
    shutil.rmtree(first.directory)
    assert registry.get(name).licensed
    assert registry.install(name, second).revision == 2
    restored = Registry(registry.root)
    assert restored.get(name, 1).report == first.report
    assert restored.rollback(name).revision == 1
    assert restored.install(name, third).revision == 3
    assert restored.rollback(name, 2).revision == 2
    assert restored.list() == (restored.status(name),)


@pytest.mark.parametrize("candidate,accept", [
    ({"score": .9, "cost": .09, "mean": .08, "components": ("a", "b", "c")}, True),
    ({"score": .7, "cost": .01, "mean": .001, "components": ()}, False),
    ({"mean": .02}, True), ({"mean": .04}, False),
    ({"components": ("a",)}, True), ({"components": ("a", "b", "c")}, False),
    ({}, False), ({"mean": None, "cost": .02}, True),
    ({"mean": None, "cost": .04}, False),
])
def test_acceptance_order(tmp_path: Path, make: Callable[..., HarnessArtifact],
                          candidate: dict[str, object], accept: bool) -> None:
    registry = Registry(tmp_path / "registry")
    registry.install("agent", make())
    proposed = make(**candidate)
    if accept:
        assert registry.install("agent", proposed).revision == 2
    else:
        with pytest.raises(RegistryError, match="not_better"):
            registry.install("agent", proposed)
        assert registry.status("agent").revision == 1


@pytest.mark.parametrize("kind", [FloorKind.HELD_OUT_TASKS, FloorKind.HUMAN_EDIT_RATE])
@pytest.mark.parametrize("better", [True, False])
def test_secondary_and_edit_direction(tmp_path: Path, make: Callable[..., HarnessArtifact],
                                      kind: FloorKind, better: bool) -> None:
    base = make()
    primary = base.spec.floor.model_copy(update={"kind": FloorKind.HUMAN_EDIT_RATE})
    secondary = primary.model_copy(update={"kind": kind, "dataset": "secondary"})
    spec = base.spec.model_copy(update={"floor": primary, "secondary_floor": secondary})
    initial = .2 if kind == FloorKind.HUMAN_EDIT_RATE else .8
    change = -.1 if kind == FloorKind.HUMAN_EDIT_RATE else .1
    registry = Registry(tmp_path / "registry")
    registry.install("agent", make(spec=spec, score=.2, secondary=initial))
    candidate = make(spec=spec, score=.2, secondary=initial + change * (1 if better else -1))
    if better:
        registry.install("agent", candidate)
    else:
        with pytest.raises(RegistryError, match="not_better"):
            registry.install("agent", candidate)
    # A strictly better primary ceiling beats worse secondary quality.
    registry.install("agent", make(spec=spec, score=.1, secondary=initial))


@pytest.mark.parametrize("field,value", [
    ("task", "Different task"), ("input_schema", {"type": "number"}),
    ("output_schema", {"type": "number"}), ("domain", "coding"), ("tools", ("lookup",)),
])
def test_incompatible_tasks_rejected(tmp_path: Path, make: Callable[..., HarnessArtifact],
                                     field: str, value: object) -> None:
    registry = Registry(tmp_path / "registry")
    original = make()
    registry.install("agent", original)
    candidate = make(spec=original.spec.model_copy(update={field: value}), score=1)
    with pytest.raises(RegistryError, match="not_better"):
        registry.install("agent", candidate)


def test_floor_and_secondary_changes_cannot_game_acceptance(
    tmp_path: Path, make: Callable[..., HarnessArtifact],
) -> None:
    original = make()
    registry = Registry(tmp_path / "registry")
    registry.install("agent", original)
    for field, floor in [("floor", original.spec.floor.model_copy(update={"dataset": "easy"})),
                         ("secondary_floor", original.spec.floor)]:
        candidate = make(spec=original.spec.model_copy(update={field: floor}), score=1,
                         secondary=1 if field == "secondary_floor" else None)
        with pytest.raises(RegistryError, match="not_better"):
            registry.install("agent", candidate)


@pytest.mark.parametrize("attack", ["unsigned", "code", "disk_report", "missing", "expire",
                                    "memory", "budget", "floor", "components"])
def test_incoming_authentication(tmp_path: Path, make: Callable[..., HarnessArtifact],
                                 attack: str) -> None:
    registry = Registry(tmp_path / "registry")
    artifact = make()
    assert artifact.report is not None
    if attack == "unsigned":
        artifact = artifact.model_copy(update={"report": None})
        artifact.save()
    elif attack == "code":
        (artifact.directory / "harness.py").write_text("tampered")
    elif attack == "disk_report":
        (artifact.directory / "report.json").write_text("{}")
    elif attack == "missing":
        shutil.rmtree(artifact.directory)
    elif attack == "expire":
        artifact = make(spec=artifact.spec.model_copy(update={"lifecycle": Lifecycle.EXPIRE}))
    elif attack == "memory":
        artifact = artifact.model_copy(update={"model": "other"})
    else:
        attacks: dict[str, dict[str, object]] = {
            "budget": {"cost_usd": 1}, "floor": {"floor_score": .1},
            "components": {"component_count": 99},
        }
        changes = attacks[attack]
        assert artifact.report is not None
        artifact = artifact.model_copy(update={"report": _signed(
            artifact.report.model_copy(update=changes))})
        artifact.save()
    with pytest.raises(RegistryError):
        registry.install("agent", artifact)
    assert registry.list() == ()


@pytest.mark.parametrize("operation", ["get", "status", "list", "install", "rollback"])
def test_corrupt_owned_license_never_used(tmp_path: Path, make: Callable[..., HarnessArtifact],
                                         operation: str) -> None:
    registry = Registry(tmp_path / "registry")
    first = registry.install("agent", make())
    if operation == "rollback":
        registry.install("agent", make(score=.9))
    (first.directory / "harness.py").write_text("changed")
    with pytest.raises(RegistryError):
        if operation == "install":
            registry.install("agent", make(score=1))
        elif operation == "list":
            registry.list()
        else:
            getattr(registry, operation)("agent")


@pytest.mark.parametrize("name", ["", "  ", "a\0b", "a" * 1025, None, 2])
def test_invalid_names(tmp_path: Path, make: Callable[..., HarnessArtifact], name: str) -> None:
    registry = Registry(tmp_path / "registry")
    with pytest.raises(RegistryError, match="invalid_name"):
        registry.install(name, make())
    with pytest.raises(RegistryError, match="invalid_name"):
        registry.get(name)


@pytest.mark.parametrize("revision", [0, -1, True, 1.5, "1"])
def test_invalid_revisions(tmp_path: Path, revision: int) -> None:
    registry = Registry(tmp_path / "registry")
    with pytest.raises(RegistryError, match="invalid_revision"):
        registry.get("agent", revision)
    with pytest.raises(RegistryError, match="invalid_revision"):
        registry.rollback("agent", revision)


def test_unknown_and_nonprior_revisions(
    tmp_path: Path, make: Callable[..., HarnessArtifact],
) -> None:
    registry = Registry(tmp_path / "registry")
    with pytest.raises(RegistryError, match="unknown_name"):
        registry.get("missing")
    with pytest.raises(RegistryError, match="unknown_revision"):
        registry.get("missing", 1)
    registry.install("agent", make())
    with pytest.raises(RegistryError, match="no_previous_revision"):
        registry.rollback("agent")
    with pytest.raises(RegistryError, match="not_previous_revision"):
        registry.rollback("agent", 1)
    with pytest.raises(RegistryError, match="unknown_revision"):
        registry.get("agent", 8)


@pytest.mark.parametrize("kwargs", [{"window": 1}, {"window": True}, {"window": 2.5},
                                    {"margin": -.1}, {"margin": 2}, {"margin": float("nan")},
                                    {"margin": True}, {"margin": "bad"}])
def test_invalid_monitor_configuration(tmp_path: Path, kwargs: dict[str, object]) -> None:
    with pytest.raises(RegistryError):
        Registry(tmp_path, window=cast(int, kwargs.get("window", 10)),
                 margin=cast(float, kwargs.get("margin", .05)))


def test_concurrent_acceptance_serialized(
    tmp_path: Path, make: Callable[..., HarnessArtifact],
) -> None:
    registry = Registry(tmp_path / "registry")
    registry.install("agent", make())
    candidate = make(score=.9)

    def attempt() -> bool:
        try:
            Registry(registry.root).install("agent", candidate)
            return True
        except RegistryError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: attempt(), range(2))) == [False, True]
    assert registry.status("agent").revision == 2


@pytest.mark.parametrize("edit", [False, True])
def test_observed_rolling_quality_direction(tmp_path: Path, make: Callable[..., HarnessArtifact],
                                            edit: bool) -> None:
    artifact = make()
    if edit:
        artifact = make(spec=artifact.spec.model_copy(update={"floor":
            artifact.spec.floor.model_copy(update={"kind": FloorKind.HUMAN_EDIT_RATE})}), score=.2)
    registry = Registry(tmp_path / "registry", window=3, margin=.05)
    registry.install("agent", artifact)
    def score(quality: float) -> float:
        return 1 - quality if edit else quality

    assert registry.observe("agent", [row(artifact, score(.9))]) is None
    assert registry.observe("agent", [row(artifact, score(.6))]) is None
    assert registry.observe("agent", [row(artifact, score(.55))]) is None
    event = registry.observe("agent", [row(artifact, score(.5))])
    assert event is not None and event.reason == "floor_drift"
    assert event.score == pytest.approx(score(.55))


def test_no_historic_report_in_drift_dedup_and_restart(
    tmp_path: Path, make: Callable[..., HarnessArtifact],
) -> None:
    artifact = make(score=1)
    registry = Registry(tmp_path / "registry")
    registry.install("agent", artifact)
    assert registry.observe("agent", []) is None
    assert registry.observe("agent", [row(artifact, None)]) is None
    stable = [row(artifact, .5), row(artifact, .5)]
    assert registry.observe("agent", stable) is None
    assert registry.observe("agent", [row(artifact, .51)]) is None
    breached = row(artifact, .1)
    assert registry.observe("agent", [breached]) is not None
    restored = Registry(registry.root)
    assert restored.observe("agent", [breached, *stable]) is None


@pytest.mark.parametrize("changes", [
    {"spec_hash": "a" * 64}, {"harness_version": 999}, {"lifecycle": Lifecycle.EXPIRE},
    {"floor_type": FloorKind.LLM_JUDGE}, {"floor_limit": .1},
])
def test_unrelated_rows_ignored(tmp_path: Path, make: Callable[..., HarnessArtifact],
                                changes: dict[str, object]) -> None:
    artifact = make()
    registry = Registry(tmp_path / "registry")
    registry.install("agent", artifact)
    assert registry.observe("agent", [row(artifact, .1).model_copy(update=changes)]) is None


def test_model_change_even_without_scores_and_single_breach(
    tmp_path: Path, make: Callable[..., HarnessArtifact],
) -> None:
    artifact = make(model="vendor/model")
    registry = Registry(tmp_path / "registry")
    registry.install("agent", artifact)
    assert registry.observe("agent", [row(artifact, None, model="vendor/model")]) is None
    event = registry.observe("agent", [row(artifact, None, model="new/model")])
    assert event == DriftEvent(name="agent", reason="model_change", score=None)
    event = registry.observe("agent", [row(artifact, .4, model="vendor/model")])
    assert event == DriftEvent(name="agent", reason="floor_drift", score=.4)


@pytest.mark.parametrize("outcome", ["better", "worse", "error", "cancel"])
async def test_guarded_monitor(tmp_path: Path, make: Callable[..., HarnessArtifact],
                               outcome: str) -> None:
    original = make()
    registry = Registry(tmp_path / "registry")
    registry.install("agent", original)
    calls = 0

    async def recompile(spec: Spec) -> HarnessArtifact:
        nonlocal calls
        calls += 1
        assert spec == original.spec
        if outcome == "cancel":
            raise asyncio.CancelledError
        if outcome == "error":
            raise RuntimeError("PRIVATE CALLBACK CONTENT")
        return make(spec=spec, score=.9 if outcome == "better" else .7)

    assert await registry.monitor("agent", [], recompile) == DriftResult(event=None)
    batch = [row(original, .3)]
    if outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await registry.monitor("agent", batch, recompile)
    else:
        result = await registry.monitor("agent", batch, recompile)
        assert result.accepted is (outcome == "better")
        assert result.failure == {"better": None, "worse": "acceptance_rejected",
                                  "error": "recompile_failed"}[outcome]
        assert "PRIVATE" not in result.model_dump_json()
    assert calls == 1
    assert await registry.monitor("agent", batch, recompile) == DriftResult(event=None)
    assert calls == 1
    assert registry.status("agent").revision == (2 if outcome == "better" else 1)


def test_storage_failure_is_clean(tmp_path: Path) -> None:
    occupied = tmp_path / "occupied"
    occupied.write_text("file")
    with pytest.raises(RegistryError, match="storage_error"):
        Registry(occupied)
    root = tmp_path / "registry"
    root.mkdir()
    (root / "registry.sqlite3").write_text("not sqlite")
    with pytest.raises(RegistryError, match="storage_error"):
        Registry(root)


def test_copy_failure_does_not_publish(tmp_path: Path, make: Callable[..., HarnessArtifact],
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    registry = Registry(tmp_path / "registry")
    registry.install("agent", make())

    def fail(*args: object) -> None:
        raise OSError("disk failure")

    monkeypatch.setattr(shutil, "copyfile", fail)
    with pytest.raises(RegistryError, match="storage_error"):
        registry.install("agent", make(score=.9))
    assert registry.status("agent").revision == 1
    assert not list(registry.root.rglob(".install-*"))


def test_index_identity_checked(tmp_path: Path, make: Callable[..., HarnessArtifact]) -> None:
    registry = Registry(tmp_path / "registry")
    first = registry.install("agent", make())
    different = make()
    for filename in ("config.json", "spec.json", "report.json", "harness.py"):
        shutil.copyfile(different.directory / filename, first.directory / filename)
    with pytest.raises(RegistryError, match="invalid_license"):
        registry.get("agent")
    # Rollback does not require the broken current license, only its valid destination.
    with sqlite3.connect(registry.root / "registry.sqlite3") as db:
        db.execute("DELETE FROM current")
    assert registry.list() == ()


def test_no_new_score_does_not_repeat_previous_drift(
    tmp_path: Path, make: Callable[..., HarnessArtifact],
) -> None:
    artifact = make()
    registry = Registry(tmp_path / "registry")
    registry.install("agent", artifact)
    assert registry.observe("agent", [row(artifact, .1)]) is not None
    assert registry.observe("agent", [row(artifact, None)]) is None


def test_connection_failure_closes_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("unavailable")

    monkeypatch.setattr(sqlite3, "connect", fail)
    with pytest.raises(RegistryError, match="storage_error"):
        Registry(tmp_path)


def test_snapshot_change_after_acceptance_is_rejected(
    tmp_path: Path, make: Callable[..., HarnessArtifact], monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = Registry(tmp_path / "registry")
    artifact = make()
    alternative = make(score=.9)
    copyfile = shutil.copyfile

    def switch(source: Path, destination: Path) -> str:
        return str(copyfile(alternative.directory / source.name, destination))

    monkeypatch.setattr(shutil, "copyfile", switch)
    with pytest.raises(RegistryError, match="artifact_changed"):
        registry.install("agent", artifact)
    assert registry.list() == ()


def test_revision_collision_never_overwritten_and_orphan_recovery(
    tmp_path: Path, make: Callable[..., HarnessArtifact], monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = Registry(tmp_path / "registry")
    artifact = make()
    copyfile = shutil.copyfile
    destination: Path | None = None

    def collide(source: Path, target: Path) -> str:
        nonlocal destination
        result = copyfile(source, target)
        if source.name == "report.json":
            # Simulate a foreign writer creating the destination during the copy.
            destination = next((registry.root / "versions").glob("*/1/artifacts/*")) / "1"
            destination.mkdir()
            (destination / "sentinel").write_text("preserve")
        return str(result)

    monkeypatch.setattr(shutil, "copyfile", collide)
    with pytest.raises(RegistryError, match="revision_conflict"):
        registry.install("agent", artifact)
    assert destination is not None
    assert (destination / "sentinel").read_text() == "preserve"
    assert registry.list() == ()
    monkeypatch.setattr(shutil, "copyfile", copyfile)
    # Unpublished revision 1 remains intact; the next install skips it.
    assert registry.install("agent", artifact).revision == 2
    assert (destination / "sentinel").read_text() == "preserve"


def test_current_corrupt_rollback_still_possible(
    tmp_path: Path, make: Callable[..., HarnessArtifact],
) -> None:
    registry = Registry(tmp_path / "registry")
    first = registry.install("agent", make())
    second = registry.install("agent", make(score=.9))
    (second.directory / "harness.py").write_text("corrupt")
    assert registry.rollback("agent") == first
    assert registry.get("agent").licensed


def test_each_name_has_isolated_snapshot_and_order(
    tmp_path: Path, make: Callable[..., HarnessArtifact],
) -> None:
    registry = Registry(tmp_path / "registry")
    artifact = make()
    z = registry.install("z", artifact)
    a = registry.install("a", artifact)
    assert z.directory != a.directory
    assert registry.list() == (a, z)


def test_verified_pipeline_and_model_change_can_improve_same_task(
    tmp_path: Path, make: Callable[..., HarnessArtifact],
) -> None:
    registry = Registry(tmp_path / "registry")
    original = make()
    registry.install("agent", original)
    spec = original.spec.model_copy(update={
        "pipeline": (PipelineStep(name="transform", preset="synthesizer", model="new/model",
                                  budget=original.spec.budget),),
        "model_target": "new/model",
    })
    candidate = make(spec=spec, model="new/model", score=.9)
    assert registry.install("agent", candidate).revision == 2
    assert registry.get("agent").spec.pipeline == spec.pipeline


@pytest.mark.parametrize("primary,secondary,expected", [(0.7, 1., .7), (1., .7, .7),
                                                       (.7, .6, .7)])
def test_primary_secondary_same_kind_and_limit_are_independent(
    tmp_path: Path, make: Callable[..., HarnessArtifact],
    primary: float, secondary: float, expected: float,
) -> None:
    base = make()
    definition = base.spec.floor.model_copy(update={"limit": .8})
    spec = base.spec.model_copy(update={"floor": definition, "secondary_floor": definition})
    artifact = make(spec=spec, score=1., secondary=1.)
    registry = Registry(tmp_path / "registry", window=2)
    registry.install("agent", artifact)
    event = registry.observe("agent", [row(artifact, primary),
                                       row(artifact, secondary, role="secondary")])
    assert event == DriftEvent(name="agent", reason="floor_drift", score=expected)


def test_secondary_edit_window_trend_and_role_validation(
    tmp_path: Path, make: Callable[..., HarnessArtifact],
) -> None:
    base = make()
    secondary = base.spec.floor.model_copy(update={"kind": FloorKind.HUMAN_EDIT_RATE})
    artifact = make(spec=base.spec.model_copy(update={"secondary_floor": secondary}), secondary=.2)
    registry = Registry(tmp_path / "registry", window=2)
    registry.install("agent", artifact)
    assert registry.observe("agent", [row(artifact, .9),
                                      row(artifact, .44, role="secondary")]) is None
    event = registry.observe("agent", [row(artifact, .48, role="secondary"), row(artifact, 1.)])
    assert event is not None and event.score == pytest.approx(.46)
    bad = row(artifact, .9).model_copy(update={"floor_role": "secondary"})
    assert registry.observe("agent", [bad]) is None
    assert registry.observe("agent", [row(artifact, .1).model_copy(
        update={"floor_limit": .2, "floor_role": "secondary", "floor_type": secondary.kind},
    )]) is None
    changed = row(artifact, .2, role="secondary", model="new/model")
    event = registry.observe("agent", [changed])
    assert event is not None and event.reason == "model_change"
    assert event.score == pytest.approx(.34)
    assert registry.observe("agent", [changed]) is None


def test_unconfigured_secondary_rows_ignored_and_uuid_cross_role_deduplicated(
    tmp_path: Path, make: Callable[..., HarnessArtifact],
) -> None:
    artifact = make()
    registry = Registry(tmp_path / "registry")
    registry.install("agent", artifact)
    assert registry.observe("agent", [row(artifact, .1).model_copy(
        update={"floor_role": "secondary"},
    )]) is None
    both = make(spec=artifact.spec.model_copy(update={"secondary_floor": artifact.spec.floor}),
                secondary=.9)
    registry.install("both", both)
    primary = row(both, .9)
    duplicate = primary.model_copy(update={"floor_role": "secondary", "floor_score": .1})
    assert registry.observe("both", [primary, duplicate]) is None


def test_existing_observation_database_migrates_primary_role(
    tmp_path: Path, make: Callable[..., HarnessArtifact],
) -> None:
    registry = Registry(tmp_path / "registry", window=2)
    artifact = make()
    registry.install("agent", artifact)
    previous = row(artifact, .6)
    with sqlite3.connect(registry.root / "registry.sqlite3") as db:
        db.execute("DROP TABLE observations")
        db.execute("""CREATE TABLE observations (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, revision INTEGER NOT NULL, run_id TEXT NOT NULL,
            score REAL, UNIQUE(name, revision, run_id))""")
        db.execute("INSERT INTO observations (name, revision, run_id, score) VALUES (?, ?, ?, ?)",
                   ("agent", 1, previous.run_id, .6))
    restored = Registry(registry.root, window=2)
    assert restored.observe("agent", [previous]) is None
    event = restored.observe("agent", [row(artifact, .4)])
    assert event == DriftEvent(name="agent", reason="floor_drift", score=.5)
    with sqlite3.connect(registry.root / "registry.sqlite3") as db:
        assert db.execute("SELECT DISTINCT role FROM observations").fetchall() == [("primary",)]


@pytest.mark.parametrize("ceiling,value", [("cost_usd", .2), ("compute_s", 11)])
def test_budget_increase_requires_explicit_manual_permission(
    tmp_path: Path, make: Callable[..., HarnessArtifact], ceiling: str, value: float,
) -> None:
    registry = Registry(tmp_path / "registry")
    original = make()
    registry.install("agent", original)
    budget = original.spec.budget.model_copy(update={ceiling: value})
    candidate = make(spec=original.spec.model_copy(update={"budget": budget}), score=.9)
    with pytest.raises(RegistryError, match="budget_increase"):
        registry.install("agent", candidate)
    assert registry.status("agent").revision == 1
    assert registry.install("agent", candidate, allow_budget_increase=True).revision == 2


def test_high_measured_tokens_do_not_block_replacement(
    tmp_path: Path, make: Callable[..., HarnessArtifact],
) -> None:
    registry = Registry(tmp_path / "registry")
    original = make()
    registry.install("agent", original)
    budget = Budget.model_validate({**original.spec.budget.model_dump(), "tokens": 1})
    candidate = make(spec=original.spec.model_copy(update={"budget": budget}), score=.9)
    assert candidate.report is not None
    report = _signed(candidate.report.model_copy(update={"tokens": 3_000_000}))
    candidate = candidate.model_copy(update={"report": report})
    candidate.save()
    assert candidate.licensed
    assert registry.install("agent", candidate).revision == 2
    assert HarnessArtifact.load(registry.status("agent").directory).report == report


def test_budget_allowance_never_bypasses_quality_auth_or_strict_permission(
    tmp_path: Path, make: Callable[..., HarnessArtifact],
) -> None:
    registry = Registry(tmp_path / "registry")
    artifact = make()
    registry.install("agent", artifact)
    with pytest.raises(RegistryError, match="invalid_budget_permission"):
        registry.install("agent", artifact, allow_budget_increase=cast(bool, "yes"))
    with pytest.raises(RegistryError, match="not_better"):
        registry.install("agent", make(score=.7), allow_budget_increase=True)
    invalid = make(score=.1)
    with pytest.raises(RegistryError, match="invalid_license"):
        registry.install("agent", invalid, allow_budget_increase=True)


async def test_automatic_monitor_never_relaxes_budget(
    tmp_path: Path, make: Callable[..., HarnessArtifact],
) -> None:
    registry = Registry(tmp_path / "registry")
    original = make()
    registry.install("agent", original)

    async def recompile(spec: Spec) -> HarnessArtifact:
        budget = spec.budget.model_copy(update={"cost_usd": spec.budget.cost_usd + 1})
        return make(spec=spec.model_copy(update={"budget": budget}), score=.9)

    result = await registry.monitor("agent", [row(original, .1)], recompile)
    assert result.failure == "acceptance_rejected" and not result.accepted
    assert registry.status("agent").revision == 1


def test_activation_excludes_history_naive_time_and_survives_restart(
    tmp_path: Path, make: Callable[..., HarnessArtifact],
) -> None:
    artifact = make()
    historic = row(artifact, .1, model="changed")
    registry = Registry(tmp_path / "registry", window=2)
    registry.install("agent", artifact)
    registry = Registry(registry.root, window=2)
    naive = row(artifact, .1).model_copy(update={"timestamp": datetime.now()})
    assert registry.observe("agent", [historic, naive]) is None
    # Offset-aware instants compare by UTC, not their local calendar representation.
    future = row(artifact, .8).model_copy(update={
        "timestamp": datetime.now(UTC).astimezone(timezone(timedelta(hours=-5))),
    })
    assert registry.observe("agent", [future]) is None
    event = registry.observe("agent", [row(artifact, .2)])
    assert event == DriftEvent(name="agent", reason="floor_drift", score=.5)


def test_rollback_resets_window_and_activation_but_preserves_uuid_identity(
    tmp_path: Path, make: Callable[..., HarnessArtifact],
) -> None:
    registry = Registry(tmp_path / "registry", window=2)
    first = make()
    registry.install("agent", first)
    old = row(first, 1.)
    assert registry.observe("agent", [old]) is None
    registry.install("agent", make(score=.9))
    inactive = row(first, 1.)
    registry.rollback("agent")
    registry = Registry(registry.root, window=2)
    replayed = old.model_copy(update={"timestamp": datetime.now(UTC)})
    assert registry.observe("agent", [old, inactive, replayed]) is None
    event = registry.observe("agent", [row(first, .1)])
    assert event == DriftEvent(name="agent", reason="floor_drift", score=.1)


def test_activation_migration_conservatively_ignores_old_rows(
    tmp_path: Path, make: Callable[..., HarnessArtifact],
) -> None:
    registry = Registry(tmp_path / "registry")
    artifact = make()
    registry.install("agent", artifact)
    old = row(artifact, .1, model="changed")
    assert registry.observe("agent", [row(artifact, 1.)]) is None
    with sqlite3.connect(registry.root / "registry.sqlite3") as db:
        db.execute("DROP TABLE current")
        db.execute("CREATE TABLE current (name TEXT PRIMARY KEY, revision INTEGER NOT NULL)")
        db.execute("INSERT INTO current VALUES ('agent', 1)")
    restored = Registry(registry.root)
    assert restored.observe("agent", [old]) is None
    assert restored.observe("agent", [row(artifact, .1)]) == DriftEvent(
        name="agent", reason="floor_drift", score=.1,
    )


async def test_real_compilation_telemetry_never_seeds_installed_drift(tmp_path: Path) -> None:
    store = TelemetryStore(tmp_path / "telemetry")
    spec = Spec(
        task="Return ok", input_schema={"type": "object"}, output_schema={"type": "string"},
        domain="synthesizer", lifecycle=Lifecycle.INSTALL,
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=.8, dataset="tasks"),
        budget=Budget(),
    )

    def floor_factory(definition: FloorDefinition) -> HeldOutTasks:
        return HeldOutTasks(definition, [
            EvaluationCase(case_id=str(i), task={}, expected="ok") for i in range(6)
        ])

    compiler = Compiler(tmp_path / "artifacts", Runtime(store), FakeAdapter, floor_factory,
                        config=CompileConfig(max_candidates=1))
    result = await compiler.compile(spec, approved=True)
    assert result.artifact is not None and result.artifact.licensed
    assert any(item.floor_score is not None for item in store.rows())
    registry = Registry(tmp_path / "registry", window=2)
    registry.install("agent", result.artifact)
    assert registry.observe("agent", store.rows()) is None
    with sqlite3.connect(registry.root / "registry.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    event = registry.observe("agent", [row(result.artifact, .7)])
    assert event == DriftEvent(name="agent", reason="floor_drift", score=.7)


def test_lock_timeout_view_is_separate_and_reports_busy_as_cause(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry")
    view = registry.with_lock_timeout(0)
    assert registry.lock_timeout == 30.0 and view.lock_timeout == 0.0
    assert view.root == registry.root
    for invalid in (-1, float("nan"), float("inf"), True, "1"):
        with pytest.raises(RegistryError, match="invalid_lock_timeout"):
            registry.with_lock_timeout(invalid)  # type: ignore[arg-type]
    holder = sqlite3.connect(registry.root / "registry.sqlite3", isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(RegistryError, match="storage_error") as caught:
            view.list()
        cause = caught.value.__cause__
        assert isinstance(cause, sqlite3.OperationalError)
        assert cause.sqlite_errorcode == sqlite3.SQLITE_BUSY
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert view.list() == ()
