"""Phase 10 WP1 stores: registry slots/find/drift, library shape/candidates, the route
log, the incremental evidence cache, the store-backed router and the reuse executor.

Every adapter is fake and sockets to anything but loopback are refused.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from routing_support import Maker, block_network, make_spec

from architect import registry as registry_module
from architect.compiler.types import LibraryLifecycle
from architect.compiler.verification import _signed
from architect.library import HarnessLibrary, ReadOnlyLibrary
from architect.models import FakeAdapter
from architect.registry import Registry
from architect.routing import (
    EvidenceIndex,
    EvidenceUnavailable,
    OutcomeRecord,
    RatingRecord,
    RoleRecord,
    RouteDecision,
    RoutedLibrary,
    RouteError,
    RouteLog,
    RoutePolicy,
    Router,
    TaskProbe,
    load_reuse_target,
    route,
    signature,
    variant_category,
    wilson,
)
from architect.routing.log import RouteLogError, category
from architect.runtime import Runtime
from architect.spec import HarnessArtifact, Lifecycle, Spec
from architect.telemetry import TelemetryRow, TelemetryStore, categorical


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    block_network(monkeypatch)


class Stores:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.make = Maker(root / "sources")
        self.registry = Registry(root / "workspace" / "registry")
        self.library = HarnessLibrary(root / "workspace" / "library")
        self.telemetry = TelemetryStore(root / "workspace" / "telemetry")
        self.log = RouteLog(root / "workspace" / "routes")
        self.index = self.new_index()

    def new_index(self) -> EvidenceIndex:
        return EvidenceIndex(
            self.registry, self.library, self.telemetry, self.log, self.root / "workspace",
            providers={"fake": "fake", "m1": "anthropic"}, promotion_reuses=3,
        )

    def probe(self, spec: Spec, *, grant: bool = False, origin: str = "cli") -> TaskProbe:
        return TaskProbe.model_validate(
            {"origin": origin, "spec": spec, "budget_cap": spec.budget,
             "ladder": (spec.model_target,), "grant_reuse": grant}
        )


@pytest.fixture
def stores(tmp_path: Path) -> Iterator[Stores]:
    value = Stores(tmp_path)
    yield value
    value.telemetry.close()


def telemetry_row(
    artifact: HarnessArtifact, *, wall: float = 1.0, task_type: str | None = None,
    model: str = "fake", domain: str = "general",
) -> TelemetryRow:
    return TelemetryRow(
        run_id=str(uuid4()), timestamp=datetime.now(UTC), domain=categorical(domain),
        task_type=categorical(task_type or artifact.preset), spec_hash=artifact.spec.spec_hash,
        harness_version=artifact.version, lifecycle=artifact.spec.lifecycle,
        preset_origin=categorical(artifact.preset), component_set=(),
        model=categorical(model), model_params={}, tokens_in=1, tokens_out=1, cost_usd=0.001,
        wall_time_s=wall, floor_type=artifact.spec.floor.kind, floor_score=None,
        floor_limit=artifact.spec.floor.limit, accepted=True,
    )


def _count(path: Path, table: str) -> int:
    with sqlite3.connect(path) as db:
        return int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


# -------------------------------------------------------------------- registry


def test_install_fills_slots_and_find_returns_current_revisions(stores: Stores) -> None:
    spec = make_spec()
    first = stores.make(spec, score=0.7)
    stores.registry.install("tickets", first)
    stores.registry.install("tickets", stores.make(spec, score=0.9))
    sibling = make_spec("Rank the support ticket urgency")
    stores.registry.install("sibling", stores.make(sibling))
    sig = signature(spec)
    assert [(e.name, e.revision) for e in stores.registry.find(slot=sig.slot)] == [("tickets", 2)]
    assert [e.name for e in stores.registry.find(shape=sig.shape)] == ["sibling", "tickets"]
    assert [e.name for e in stores.registry.find(slot=sig.slot, shape=sig.shape)] == [
        "sibling", "tickets"]
    assert stores.registry.find() == ()
    assert [e.name for e in stores.registry.find_family(sig.family)] == ["sibling", "tickets"]
    assert [e.revision for e in stores.registry.history("tickets")] == [1, 2]
    db = stores.registry.root / "registry.sqlite3"
    with sqlite3.connect(db) as connection:
        rows = connection.execute(
            "SELECT name, revision, slot, shape, family, model, lifecycle FROM slots "
            "ORDER BY name, revision").fetchall()
    assert rows[0] == ("sibling", 1, signature(sibling).slot, sig.shape, sig.family, "fake",
                       "install")
    assert [row[:2] for row in rows] == [("sibling", 1), ("tickets", 1), ("tickets", 2)]


def test_slots_migration_is_idempotent_and_backfills(stores: Stores) -> None:
    stores.registry.install("a", stores.make(make_spec("first task")))
    stores.registry.install("b", stores.make(make_spec("second task")))
    db = stores.registry.root / "registry.sqlite3"
    # A pre-Phase 10 registry: no slots table at all.
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE slots")
    Registry(stores.registry.root)
    Registry(stores.registry.root)  # Opening twice changes nothing.
    assert _count(db, "slots") == 2
    assert [e.name for e in Registry(stores.registry.root).find(
        slot=signature(make_spec("first task")).slot)] == ["a"]


def test_slots_backfill_survives_a_crash_and_skips_unloadable_entries(
    stores: Stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("a", "b", "c"):
        stores.registry.install(name, stores.make(make_spec(f"task {name}")))
    db = stores.registry.root / "registry.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.execute("DELETE FROM slots")

    class Crash(BaseException):
        pass

    original = registry_module._index_slot
    calls = {"n": 0}

    def crashing(*args: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise Crash
        original(*args)

    monkeypatch.setattr(registry_module, "_index_slot", crashing)
    with pytest.raises(Crash):
        Registry(stores.registry.root)
    assert _count(db, "slots") == 0  # The whole backfill rolled back.
    monkeypatch.setattr(registry_module, "_index_slot", original)
    # An unloadable entry is skipped now and retried on the next open.
    broken = stores.registry.status("b").directory / "report.json"
    saved = broken.read_bytes()
    broken.write_text("{}", encoding="utf-8")
    Registry(stores.registry.root)
    assert _count(db, "slots") == 2
    broken.write_bytes(saved)
    Registry(stores.registry.root)
    assert _count(db, "slots") == 3


def test_drift_is_read_only(stores: Stores) -> None:
    spec = make_spec()
    artifact = stores.make(spec)
    stores.registry.install("tickets", artifact)
    db = stores.registry.root / "registry.sqlite3"

    def counts() -> tuple[int, ...]:
        return tuple(_count(db, table) for table in ("revisions", "current", "observations",
                                                     "slots"))

    before = counts()
    assert stores.registry.drift("tickets") == "no_window"
    assert counts() == before
    installed = stores.registry.get("tickets")

    def scored(score: float) -> TelemetryRow:
        row = telemetry_row(installed)
        return row.model_copy(update={"floor_score": score})

    stores.registry.observe("tickets", [scored(0.9)])
    assert stores.registry.drift("tickets") == "no_window"  # one score is not a window
    stores.registry.observe("tickets", [scored(0.9)])
    before = counts()
    assert stores.registry.drift("tickets") == "stable"
    stores.registry.observe("tickets", [scored(0.2), scored(0.1)])
    before = counts()
    assert stores.registry.drift("tickets") == "drifting"
    assert counts() == before


# --------------------------------------------------------------------- library


def test_library_shape_candidates_and_lazy_backfill(stores: Stores) -> None:
    spec = make_spec(lifecycle=Lifecycle.EXPIRE)
    keys = [stores.library.store(stores.make(spec, score=0.6 + i / 10)) for i in range(3)]
    other = stores.library.store(stores.make(make_spec("x", output={"type": "integer"},
                                                       lifecycle=Lifecycle.EXPIRE)))
    shape = signature(spec).shape
    db = stores.library.root / "library.sqlite3"
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT shape FROM templates WHERE key=?",
                                  (keys[0],)).fetchone()[0] == shape
        connection.execute("UPDATE templates SET shape = NULL")
    HarnessLibrary(stores.library.root)  # The ALTER TABLE migration is idempotent.
    rows = stores.library.candidates(shape, "fake", limit=2)
    assert [row.key for row in rows] == keys[:2]
    assert [row.sequence for row in rows] == sorted(row.sequence for row in rows)
    assert all(row.artifact is not None and row.artifact.licensed for row in rows)
    assert stores.library.candidates(shape, "other-model") == ()
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM templates WHERE shape IS NULL"
                                  ).fetchone()[0] == 0
        connection.execute("UPDATE templates SET evicted=1, failures=2 WHERE key=?", (keys[0],))
    rows = stores.library.candidates(shape, "fake", limit=8)
    assert [(row.key, row.evicted, row.artifact is None) for row in rows] == [
        (keys[0], True, True), (keys[1], False, False), (keys[2], False, False)]
    assert other not in [row.key for row in rows]
    # ``lookup`` is unchanged: it still returns the closest compatible template.
    assert stores.library.lookup(spec, "fake") is not None


def test_read_only_library_is_not_a_lifecycle(stores: Stores) -> None:
    view = ReadOnlyLibrary(stores.library)
    assert isinstance(view, ReadOnlyLibrary)
    assert isinstance(stores.library, LibraryLifecycle)
    assert isinstance(ReadOnlyLibrary(stores.library), LibraryLifecycle) is False
    assert not hasattr(view, "store") and not hasattr(view, "record_verification")
    spec = make_spec(lifecycle=Lifecycle.EXPIRE)
    stores.library.store(stores.make(spec))
    assert view.lookup(spec, "fake") is not None
    assert view.verification_count("0" * 64) == 0


# ------------------------------------------------------------------- route log


def _route_record(decision: RouteDecision) -> Any:
    from architect.routing import route_record

    return route_record(decision)


def test_route_log_appends_indexes_and_recovers(stores: Stores) -> None:
    spec = make_spec()
    decision = route(stores.probe(spec), index=stores.index, policy=RoutePolicy())
    record = _route_record(decision).model_copy(update={"explored": True})
    stores.log.append(record)
    stores.log.append(OutcomeRecord(decision_id=decision.decision_id, timestamp=datetime.now(UTC),
                                    outcome="unlicensed", stage4_passed=False,
                                    evidence_class="synthetic_fake", compile_cost_usd=0.25))
    stores.log.append(RatingRecord(run_id=uuid4(), timestamp=datetime.now(UTC),
                                   target_digest=categorical("a" * 64), rating="good"))
    assert stores.log.counter(signature(spec).shape) == 1
    assert stores.log.counter(category(signature(spec).shape)) == 1
    assert stores.log.explore_spent_today() == pytest.approx(0.25)
    assert [o.outcome for _, o in stores.log.outcomes(slot=signature(spec).slot)] == ["unlicensed"]
    assert list(stores.log.outcomes(shape="0" * 32)) == []
    assert stores.log.ratings("a" * 64) == (1, 0)
    # Losing the index loses nothing: it is rebuilt from the journals.
    (stores.log.root / "index.sqlite").unlink()
    for suffix in ("-wal", "-shm"):
        (stores.log.root / f"index.sqlite{suffix}").unlink(missing_ok=True)
    assert stores.log.counter(signature(spec).shape) == 1
    assert stores.log.explore_spent_today() == pytest.approx(0.25)
    # A torn final write is ignored, then dropped by the next append.
    with (stores.log.root / "outcomes.jsonl").open("ab") as stream:
        stream.write(b'{"decision_id": "torn')
    assert stores.log.explore_spent_today() == pytest.approx(0.25)
    stores.log.append(OutcomeRecord(decision_id=decision.decision_id, timestamp=datetime.now(UTC),
                                    outcome="licensed", stage4_passed=True,
                                    evidence_class="synthetic_fake"))
    lines = (stores.log.root / "outcomes.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and all(json.loads(line) for line in lines)


def test_evidence_store_unavailable_is_fail_open(stores: Stores) -> None:
    spec = make_spec()
    stores.registry.install("tickets", stores.make(spec))
    blocked = stores.root / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    log = RouteLog(blocked / "routes")
    with pytest.raises(RouteLogError):
        log.counter("0" * 32)
    index = EvidenceIndex(stores.registry, stores.library, stores.telemetry, log, stores.root,
                          providers={"fake": "fake"}, promotion_reuses=3)
    router = Router(index, RoutePolicy(), log)
    decision = router.route(stores.probe(spec, grant=True))
    assert decision.action == "compile"
    assert decision.codes == ("evidence_store_unavailable", "route_compile")
    router.outcome(decision, OutcomeRecord(decision_id=decision.decision_id,
                                           timestamp=datetime.now(UTC), outcome="licensed",
                                           stage4_passed=True, evidence_class="synthetic_fake"))


# -------------------------------------------------------------- evidence cache


def test_evidence_cache_is_incremental_with_at_most_one_full_replay(stores: Stores) -> None:
    spec = make_spec()
    artifact = stores.make(spec)
    stores.registry.install("tickets", artifact)
    installed = stores.registry.get("tickets")
    for wall in (1.0, 2.0, 3.0, 4.0, 5.0, 60.0):
        stores.telemetry.append(telemetry_row(installed, wall=wall))
    router = Router(stores.index, RoutePolicy(), stores.log)
    first = router.route(stores.probe(spec))
    (view,) = [v for v in first.candidates if v.origin == "installed"]
    assert view.latency_basis == "measured" and view.latency_s == pytest.approx(3.5)
    assert "latency_measured" in view.evidence
    assert stores.index.full_replays == 1
    cache = stores.log.root / "evidence.sqlite"
    for suffix in ("", "-wal", "-shm"):
        Path(f"{cache}{suffix}").unlink(missing_ok=True)
    index = stores.new_index()
    router = Router(index, RoutePolicy(), stores.log)
    for _ in range(100):
        router.route(stores.probe(spec))
    assert index.full_replays == 1
    with sqlite3.connect(cache) as db:
        offset = db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()[0]
    journal = Path(stores.telemetry.directory) / "runs.jsonl"
    assert offset == journal.stat().st_size
    stores.telemetry.append(telemetry_row(installed, wall=7.0))
    router.route(stores.probe(spec))
    assert index.full_replays == 1
    with sqlite3.connect(cache) as db:
        assert db.execute("SELECT deliveries FROM versions").fetchone()[0] == 7


def test_latency_is_a_bound_below_five_rows(stores: Stores) -> None:
    spec = make_spec()
    stores.registry.install("tickets", stores.make(spec, compute=2.5))
    installed = stores.registry.get("tickets")
    for _ in range(4):
        stores.telemetry.append(telemetry_row(installed, wall=0.1))
    decision = route(stores.probe(spec), index=stores.index, policy=RoutePolicy())
    (view,) = [v for v in decision.candidates if v.origin == "installed"]
    assert (view.latency_s, view.latency_basis) == (2.5, "bound")
    assert "latency_bound" in view.evidence and "drift_window_none" in view.evidence


def test_undecodable_bench_rows_are_fail_open(stores: Stores) -> None:
    spec = make_spec()
    stores.registry.install("task", stores.make(spec))
    digest = stores.registry.get("task").artifact_digest
    bench = stores.root / "workspace" / "bench" / "run1"
    bench.mkdir(parents=True)
    (bench / "rows.jsonl").write_bytes(bytes([0xFF, 0xFE]) + b" not utf8\n")
    router = Router(stores.index, RoutePolicy(), stores.log)
    decision = router.route(stores.probe(spec, grant=True))
    assert decision.action == "compile"
    assert decision.codes == ("evidence_store_unavailable", "route_compile")
    assert stores.index.bench_counts(digest, spec.spec_hash) == {}


_BAD_WALLS = ("not json", '{"a": 1}', "[{}]", "5", '["x"]', "[" * 100_000)


@pytest.mark.parametrize("walls", _BAD_WALLS, ids=("text", "object", "nested", "scalar",
                                                   "string", "deep"))
def test_corrupt_evidence_cache_versions_is_fail_open(stores: Stores, walls: str) -> None:
    spec = make_spec()
    stores.registry.install("task", stores.make(spec))
    installed = stores.registry.get("task")
    router = Router(stores.index, RoutePolicy(), stores.log)
    assert router.route(stores.probe(spec, grant=True)).action == "reuse_installed"
    with sqlite3.connect(stores.log.root / "evidence.sqlite") as db:
        db.execute("INSERT OR REPLACE INTO versions VALUES (?, ?, ?, 9, 9, 0.0, ?)",
                   (spec.spec_hash, installed.version, "synthetic_fake", walls))
    decision = router.route(stores.probe(spec, grant=True))
    assert decision.action == "compile"
    assert decision.codes == ("evidence_store_unavailable", "route_compile")
    # The next journal row folds into the corrupt aggregate: still fail open.
    stores.telemetry.append(telemetry_row(installed))
    decision = router.route(stores.probe(spec, grant=True))
    assert decision.codes == ("evidence_store_unavailable", "route_compile")


@pytest.mark.parametrize("deliveries", (None, "x"), ids=("null", "text"))
def test_corrupt_evidence_cache_counter_is_fail_open(stores: Stores, deliveries: Any) -> None:
    spec = make_spec()
    stores.registry.install("task", stores.make(spec))
    installed = stores.registry.get("task")
    router = Router(stores.index, RoutePolicy(), stores.log)
    assert router.route(stores.probe(spec, grant=True)).action == "reuse_installed"
    with sqlite3.connect(stores.log.root / "evidence.sqlite") as db:
        db.execute("INSERT OR REPLACE INTO versions VALUES (?, ?, ?, ?, 9, 0.0, '[]')",
                   (spec.spec_hash, installed.version, "synthetic_fake", deliveries))
    decision = router.route(stores.probe(spec, grant=True))
    assert decision.codes == ("evidence_store_unavailable", "route_compile")


@pytest.mark.parametrize("walls", _BAD_WALLS, ids=("text", "object", "nested", "scalar",
                                                   "string", "deep"))
def test_corrupt_evidence_cache_roles_raise_evidence_unavailable(
    stores: Stores, walls: str
) -> None:
    variant = type("Variant", (), {"model": "m1", "params": {}})()
    assert stores.index.role_stats("inference", variant, None).n == 0
    with sqlite3.connect(stores.log.root / "evidence.sqlite") as db:
        db.execute("INSERT OR REPLACE INTO roles VALUES (?, ?, ?, ?, 1, 0.0, ?)",
                   (categorical("inference"), categorical("m1"), categorical("general"),
                    "live_billed", walls))
    with pytest.raises(EvidenceUnavailable, match="^evidence_store_unavailable$"):
        stores.index.role_stats("inference", variant, None)
    artifact = stores.make(make_spec())
    stores.telemetry.append(telemetry_row(artifact, task_type="inference", model="m1"))
    with pytest.raises(EvidenceUnavailable, match="^evidence_store_unavailable$"):
        stores.index.role_stats("inference", variant, None)


def test_deeply_nested_bench_row_is_skipped(stores: Stores) -> None:
    spec = make_spec()
    stores.registry.install("task", stores.make(spec))
    digest = stores.registry.get("task").artifact_digest
    bench = stores.root / "workspace" / "bench" / "run1"
    bench.mkdir(parents=True)
    good = json.dumps({"artifact_digest": digest, "evidence": "synthetic_fake"})
    (bench / "rows.jsonl").write_text("[" * 200_000 + "\n" + good + "\n", encoding="utf-8")
    router = Router(stores.index, RoutePolicy(), stores.log)
    decision = router.route(stores.probe(spec, grant=True))
    assert decision.action == "reuse_installed"
    (view,) = [v for v in decision.candidates if v.origin == "installed"]
    assert "bench_joined" in view.evidence
    assert stores.index.bench_counts(digest, spec.spec_hash) == {"synthetic_fake": 1}


def test_ratings_and_bench_rows_label_but_do_not_rank(stores: Stores) -> None:
    spec = make_spec()
    stores.registry.install("tickets", stores.make(spec))
    digest = stores.registry.get("tickets").artifact_digest
    for rating in ("good",) * 4 + ("bad",):
        stores.log.append(RatingRecord(run_id=uuid4(), timestamp=datetime.now(UTC),
                                       target_digest=categorical(digest), rating=rating))
    bench = stores.root / "workspace" / "bench" / "run1"
    bench.mkdir(parents=True)
    (bench / "rows.jsonl").write_text(
        json.dumps({"artifact_digest": digest, "spec_hash": spec.spec_hash, "licensed": True,
                    "held_out_score": 1.0, "evidence": "synthetic_fake", "task": "ignored"})
        + "\n", encoding="utf-8")
    decision = route(stores.probe(spec), index=stores.index, policy=RoutePolicy())
    (view,) = [v for v in decision.candidates if v.origin == "installed"]
    assert view.h == pytest.approx(wilson(4, 5, 1)) and "human_rated" in view.evidence
    assert "bench_joined" in view.evidence
    assert stores.index.bench_counts(digest, spec.spec_hash) == {"synthetic_fake": 1}


def test_role_stats_weight_floor_origins_and_keep_classes(stores: Stores) -> None:
    class Variant:
        model = "m1"
        params: dict[str, int | str] = {}

    variant = Variant()
    other = type("Other", (), {"model": "fake", "params": {}})()
    now = datetime.now(UTC)

    def record(outcome: str, origin: str | None, *, cls: str = "live_billed",
               score: float | None = None, model: Any = variant) -> RoleRecord:
        return RoleRecord.model_validate({
            "decision_id": uuid4(), "timestamp": now, "role": "inference",
            "model": categorical(model.model), "variant": variant_category(model),
            "domain": "general", "evidence_class": cls, "floor_origin": origin,
            "chosen_rank": 0, "n": 0.0, "p_lcb": None, "explored": False, "outcome": outcome,
            "score": score, "codes": ()})

    for item in (
        record("success", "user_dataset"),
        record("success", "model_draft"),
        record("failure", None),
        record("partial", "example_fixture", score=1.0),
        record("partial", "example_fixture", score=0.5),
        record("excluded", None),
        record("success", None, cls="local_zero_cost"),  # another class: never mixed in
        record("success", None, model=other, cls="synthetic_fake"),
    ):
        stores.log.append(item)
    spec = make_spec()
    artifact = stores.make(spec)
    for wall in (2.0, 4.0):
        stores.telemetry.append(telemetry_row(artifact, wall=wall, task_type="inference",
                                              model="m1"))
    stats = stores.index.role_stats("inference", variant, None)
    assert stats.evidence_class == "live_billed"
    assert stats.n == pytest.approx(1 + 0.5 + 1 + 1 + 1)
    assert stats.successes == pytest.approx(1 + 0.5 + 1)
    assert stats.mean_cost_usd == pytest.approx(0.001) and stats.median_wall_s == 3.0
    assert stores.index.role_stats("inference", variant, "legal").n == 0


# ---------------------------------------------------- store-backed routing flow


async def test_granted_reuse_runs_the_installed_version(stores: Stores) -> None:
    spec = make_spec()
    source = stores.make(spec)
    stores.registry.install("tickets", source)
    stores.library.store(source)
    router = Router(stores.index, RoutePolicy(), stores.log)
    decision = router.route(stores.probe(spec, grant=True))
    assert decision.action == "reuse_installed"
    assert decision.codes[:2] == ("fit_slot", "route_reuse_installed")
    assert decision.target.name == "tickets"  # type: ignore[union-attr]
    artifact = load_reuse_target(decision, stores.registry, spec)
    runtime = Runtime(stores.telemetry)
    result = await runtime.run(artifact, "hello", FakeAdapter("fake"))
    assert result.output == "hello"
    # Not granted: the exact match is advice and the snapshot is a warm start.
    advice = router.route(stores.probe(spec))
    assert advice.action == "warm_start" and "route_reuse_not_granted" in advice.codes
    tiers = {(view.tier, view.origin) for view in advice.candidates}
    assert tiers == {("A", "installed"), ("B", "template")}
    routed = RoutedLibrary(advice.target, fallback=stores.library)  # type: ignore[arg-type]
    assert isinstance(routed, LibraryLifecycle)
    warm = routed.lookup(spec, "fake")
    assert warm is not None and warm.artifact_digest == source.artifact_digest
    assert routed.lookup(spec, "other") is None
    assert stores.log.counter(signature(spec).shape) == 2
    # The route log is content-free: no task text on disk.
    for path in stores.log.root.glob("*.jsonl"):
        assert "ticket" not in path.read_text(encoding="utf-8").casefold()


def test_fresh_load_filters_tampered_and_consumed_entries(stores: Stores) -> None:
    spec = make_spec()
    stores.registry.install("tickets", stores.make(spec))
    entry = stores.registry.status("tickets")
    (entry.directory / ".consumed").write_text("x", encoding="utf-8")
    decision = route(stores.probe(spec, grant=True), index=stores.index, policy=RoutePolicy())
    assert decision.action == "compile" and decision.candidates[0].codes == ("route_consumed",)
    (entry.directory / ".consumed").unlink()
    report = entry.directory / "report.json"
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["floor_score"] = 0.99
    report.write_text(json.dumps(payload), encoding="utf-8")
    decision = route(stores.probe(spec, grant=True), index=stores.index, policy=RoutePolicy())
    assert decision.candidates[0].codes == ("route_unlicensed",)


def _resign(directory: Path) -> None:
    artifact = HarnessArtifact.load(directory)
    assert artifact.report is not None
    report = _signed(artifact.report.model_copy(
        update={"artifact_digest": artifact.artifact_digest}))
    (directory / "report.json").write_text(report.model_dump_json(), encoding="utf-8")


def test_load_reuse_target_rechecks_everything(stores: Stores) -> None:
    spec = make_spec()
    stores.registry.install("tickets", stores.make(spec))
    decision = route(stores.probe(spec, grant=True), index=stores.index, policy=RoutePolicy())
    assert load_reuse_target(decision, stores.registry, spec).licensed
    # A normalization-only difference routes (same slot) but never reuses (_beats).
    variant = spec.model_copy(update={"task": spec.task.upper()})
    assert signature(variant).slot == signature(spec).slot
    with pytest.raises(RouteError) as caught:
        load_reuse_target(decision, stores.registry, variant)
    assert caught.value.code == "route_target_changed"
    # Swapped on disk between route and execute: licensed, but a different digest.
    directory = stores.registry.status("tickets").directory
    saved = {name: (directory / name).read_bytes() for name in ("harness.py", "report.json")}
    (directory / "harness.py").write_text("async def run(context, task):\n    return 1\n",
                                          encoding="utf-8")
    _resign(directory)
    assert stores.registry.get("tickets").licensed
    with pytest.raises(RouteError) as caught:
        load_reuse_target(decision, stores.registry, spec)
    assert caught.value.code == "route_target_changed"
    for name, data in saved.items():
        (directory / name).write_bytes(data)
    # A tampered report fails the fresh license check.
    payload = json.loads(saved["report.json"])
    payload["floor_score"] = 1.0
    (directory / "report.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RouteError) as caught:
        load_reuse_target(decision, stores.registry, spec)
    assert caught.value.code == "route_unlicensed"
    (directory / "report.json").write_bytes(saved["report.json"])
    (directory / ".consumed").write_text("x", encoding="utf-8")
    with pytest.raises(RouteError) as caught:
        load_reuse_target(decision, stores.registry, spec)
    assert caught.value.code == "route_consumed"
    advice = route(stores.probe(spec), index=stores.index, policy=RoutePolicy())
    with pytest.raises(RouteError) as caught:
        load_reuse_target(advice, stores.registry, spec)
    assert caught.value.code == "route_no_reuse"


def test_routing_never_installs_signs_or_approves(
    stores: Stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = make_spec()
    source = stores.make(spec)
    stores.registry.install("tickets", source)
    stores.library.store(source)
    calls: list[str] = []

    def spy(name: str) -> Any:
        def record(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            raise AssertionError(name)

        return record

    import architect._authority as authority
    import architect.compiler.verification as verification

    monkeypatch.setattr(Registry, "install", spy("install"))
    monkeypatch.setattr(Registry, "rollback", spy("rollback"))
    monkeypatch.setattr(Registry, "observe", spy("observe"))
    monkeypatch.setattr(HarnessLibrary, "store", spy("store"))
    monkeypatch.setattr(HarnessLibrary, "record_verification", spy("record_verification"))
    monkeypatch.setattr(HarnessArtifact, "save", spy("save"))
    monkeypatch.setattr(authority, "_sign_report", spy("sign"))
    monkeypatch.setattr(verification, "_sign_report", spy("sign"))
    router = Router(stores.index, RoutePolicy(explore_every=1), stores.log)
    for grant in (True, False):
        decision = router.route(stores.probe(spec, grant=grant))
        if decision.action == "reuse_installed":
            load_reuse_target(decision, stores.registry, spec)
        router.outcome(decision, OutcomeRecord(decision_id=decision.decision_id,
                                               timestamp=datetime.now(UTC), outcome="licensed",
                                               stage4_passed=True,
                                               evidence_class="synthetic_fake"))
    assert calls == []
    for path in stores.log.root.rglob("*.jsonl"):
        assert "approved" not in path.read_text(encoding="utf-8")


def test_router_exploration_counter_persists_across_instances(stores: Stores) -> None:
    spec = make_spec(lifecycle=Lifecycle.EXPIRE)
    for i in range(2):
        stores.library.store(stores.make(make_spec(f"sibling task {i}",
                                                   lifecycle=Lifecycle.EXPIRE)))
    policy = RoutePolicy(explore_every=2)
    seen = []
    for _ in range(4):
        router = Router(stores.new_index(), policy, stores.log)  # a fresh process each time
        decision = router.route(stores.probe(spec))
        seen.append(decision.explored)
    assert seen == [False, True, False, True]


def test_outcome_must_match_the_decision(stores: Stores) -> None:
    router = Router(stores.index, RoutePolicy(), stores.log)
    decision = router.route(stores.probe(make_spec()))
    with pytest.raises(ValueError, match="route_outcome_mismatch"):
        router.outcome(decision, OutcomeRecord(decision_id=uuid4(), timestamp=datetime.now(UTC),
                                               outcome="failed", stage4_passed=None,
                                               evidence_class=None))


def test_route_log_creates_nothing_until_used(tmp_path: Path) -> None:
    RouteLog(tmp_path / "routes")
    assert not (tmp_path / "routes").exists()


_DEEP_JSON = "[" * 200_000 + "]" * 200_000


def test_deeply_nested_installed_config_is_filtered_not_raised(stores: Stores) -> None:
    spec = make_spec()
    stores.registry.install("tickets", stores.make(spec))
    router = Router(stores.index, RoutePolicy(), stores.log)
    decision = router.route(stores.probe(spec, grant=True))
    assert decision.action == "reuse_installed"
    config = stores.registry.status("tickets").directory / "config.json"
    saved = config.read_bytes()
    config.write_text(_DEEP_JSON, encoding="utf-8")
    # Hard filter 1: a pathologically nested file is damage, never a crash of the router.
    fresh = router.route(stores.probe(spec, grant=True))
    assert fresh.action == "compile"
    (view,) = [v for v in fresh.candidates if v.origin == "installed"]
    assert view.codes == ("route_unlicensed",)
    with pytest.raises(RouteError) as caught:
        load_reuse_target(decision, stores.registry, spec)
    assert caught.value.code == "route_unlicensed"
    assert stores.index.earlier_revisions("tickets", 2) == ()
    config.write_bytes(saved)
    assert router.route(stores.probe(spec, grant=True)).action == "reuse_installed"


def test_deeply_nested_template_config_is_evicted_not_raised(stores: Stores) -> None:
    spec = make_spec(lifecycle=Lifecycle.EXPIRE)
    key = stores.library.store(stores.make(spec))
    router = Router(stores.index, RoutePolicy(), stores.log)
    assert router.route(stores.probe(spec)).action == "warm_start"
    (stores.library.root / "snapshots" / key / "config.json").write_text(
        _DEEP_JSON, encoding="utf-8")
    decision = router.route(stores.probe(spec))
    assert decision.action == "compile"
    (view,) = [v for v in decision.candidates if v.origin == "template"]
    assert view.codes == ("route_unlicensed",)
    # The failed fresh read evicted the row, exactly like any other damaged snapshot.
    with sqlite3.connect(stores.library.root / "library.sqlite3") as connection:
        assert connection.execute("SELECT evicted FROM templates WHERE key=?",
                                  (key,)).fetchone()[0] == 1
    # An evicted row whose model cannot be read is not shown, and nothing raises.
    assert stores.library.candidates(signature(spec).shape, "fake") == ()
    assert stores.library.template(key) is None
    assert stores.library.lookup(spec, "fake") is None


def test_slots_backfill_skips_a_deeply_nested_legacy_entry(stores: Stores) -> None:
    for name in ("a", "b"):
        stores.registry.install(name, stores.make(make_spec(f"task {name}")))
    db = stores.registry.root / "registry.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.execute("DELETE FROM slots")
    config = stores.registry.status("b").directory / "config.json"
    saved = config.read_bytes()
    config.write_text(_DEEP_JSON, encoding="utf-8")
    Registry(stores.registry.root)  # A pre-Phase-10 workspace still opens.
    assert _count(db, "slots") == 1
    with pytest.raises(Exception) as caught:
        stores.registry.get("b")
    assert str(caught.value) == "invalid_license"
    config.write_bytes(saved)
    Registry(stores.registry.root)  # Retried, and indexed, on the next open.
    assert _count(db, "slots") == 2
