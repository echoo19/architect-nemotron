"""Per-candidate evidence for routing (Phase 10 Sections 7.2 and 9.2).

Every number comes from a signed ``report.json``, the library counters, the
telemetry journal (through an incremental cache keyed by the journal byte offset),
the route log, or bench rows joined for display. Licenses are re-checked on a
fresh load for every call and are never cached. Aggregates are keyed by evidence
class and never summed across classes (P7).
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

from architect.library import HarnessLibrary, LibraryError, TemplateEvidence
from architect.registry import Registry, RegistryEntry, RegistryError
from architect.routing.labels import (
    PROVIDER_EVIDENCE,
    ROLE_TASK_TYPES,
    EvidenceClass,
    EvidenceLabel,
    Provider,
    RoleName,
    floor_origin_weight,
)
from architect.routing.log import RouteLog, RouteLogError
from architect.routing.signature import TaskSignature, signature
from architect.spec import FloorKind, HarnessArtifact, Lifecycle, Spec
from architect.spec.binding import check_subharness_tree
from architect.telemetry import TelemetryRow, TelemetryStore, categorical

__all__ = [
    "EvidenceIndex",
    "EvidenceUnavailable",
    "InstalledRef",
    "RoleStats",
    "TemplateRef",
    "VersionEvidence",
    "conservative_margin",
    "quality",
    "variant_category",
    "wilson",
]

_RESERVOIR = 256
_MEASURED_MIN = 5
_HUMAN_MIN = 5
# Telemetry task types that are never deliveries of the harness itself.
_NOT_DELIVERY = frozenset(categorical(item) for item in ("quality_evaluation", "rewrite"))


_Match = Literal["slot", "shape", "family"]


class EvidenceUnavailable(RuntimeError):
    """Routing evidence could not be read (``evidence_store_unavailable``, P5)."""


class InstalledRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    revision: int
    artifact_digest: str
    spec_hash: str


class TemplateRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    artifact_digest: str
    model: str


class VersionEvidence(BaseModel):
    """The fields of a ``CandidateView`` before the probe-dependent filters and fit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    origin: Literal["installed", "template"]
    ref: InstalledRef | TemplateRef
    model: str
    spec: Spec | None = None
    signature: TaskSignature | None = None
    # None: never loaded (an already evicted template); the license is unknown.
    licensed: bool | None
    # Installed only: which registry index matched, for an entry that failed to load.
    match: Literal["slot", "shape", "family"] | None = None
    consumed: bool = False
    evicted: bool = False
    drift: Literal["no_window", "stable", "drifting"] | None = None
    evidence_class: EvidenceClass | None = None
    m: float | None = None
    m_lcb: float | None = None
    sec_lcb: float | None = None
    r: float | None = None
    h: float | None = None
    cost_usd: float | None = None
    compute_s: float | None = None
    report_cost_usd: float | None = None
    latency_s: float | None = None
    latency_basis: Literal["measured", "bound"] | None = None
    k: int | None = None
    held_out_count: int = 0
    successes: int = 0
    failures: int = 0
    promoted: bool = False
    sequence: int | None = None
    evidence: tuple[EvidenceLabel, ...] = ()


class RoleStats(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    n: float
    successes: float
    mean_cost_usd: float | None
    median_wall_s: float | None
    evidence_class: EvidenceClass


class _Variant(Protocol):
    """Structural view of ``routing.roles.RoleVariant`` (WP2)."""

    @property
    def model(self) -> str: ...

    @property
    def params(self) -> Mapping[str, int | str]: ...


# ------------------------------------------------------------------ formulas


def quality(kind: FloorKind, x: float) -> float:
    """``-x`` for ``human_edit_rate``, otherwise ``x`` (registry ``_quality``)."""
    return -x if kind == FloorKind.HUMAN_EDIT_RATE else x


def conservative_margin(margin: float, n: int) -> float:
    """``m - 0.5 / sqrt(max(n, 1))``: worst-case binomial standard error at z = 1."""
    return margin - 0.5 / math.sqrt(max(n, 1))


def wilson(s: float, n: float, z: float) -> float:
    """Wilson lower bound; 0.5 when ``n = 0``."""
    if n <= 0:
        return 0.5
    p = s / n
    z2 = z * z
    centre = p + z2 / (2 * n)
    spread = z * math.sqrt(max(p * (1 - p) / n + z2 / (4 * n * n), 0.0))
    return (centre - spread) / (1 + z2 / n)


def variant_category(variant: _Variant) -> str:
    """``RoleRecord.variant``: the category of a variant's model and closed params."""
    payload = {"model": variant.model, "params": dict(variant.params)}
    return categorical(json.dumps(payload, sort_keys=True, separators=(",", ":")))


# ------------------------------------------------------------ telemetry cache


def _walls(text: object) -> list[float]:
    """A stored wall-time reservoir: a JSON list of finite numbers, else the cache is corrupt.

    Any other value (bad JSON, a deeply nested array, a non-list, a non-number) raises
    ``EvidenceUnavailable`` so the router fails open (P5) instead of raising.
    """
    try:
        value = json.loads(text) if isinstance(text, str) else None
    except (ValueError, RecursionError) as exc:
        raise EvidenceUnavailable("evidence_store_unavailable") from exc
    if not isinstance(value, list) or not all(
        isinstance(item, int | float) and not isinstance(item, bool) and math.isfinite(item)
        for item in value
    ):
        raise EvidenceUnavailable("evidence_store_unavailable")
    return [float(item) for item in value]


class _TelemetryCache:
    """``routes/evidence.sqlite``: incremental aggregates over ``runs.jsonl``.

    Only the journal tail past the stored byte offset is scanned. A lost cache
    replays the journal once; a journal shorter than the offset resets the cache.
    """

    def __init__(self, path: Path, journal: Path) -> None:
        self.path = path
        self.journal = journal
        self.full_replays = 0

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db: sqlite3.Connection | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, timeout=5.0)
            db.execute("PRAGMA busy_timeout = 5000")
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS versions (spec_hash TEXT, version INTEGER, "
                "class TEXT, deliveries INTEGER, accepted INTEGER, cost REAL, walls TEXT, "
                "PRIMARY KEY(spec_hash, version, class))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS roles (task_type TEXT, model TEXT, domain TEXT, "
                "class TEXT, n INTEGER, cost REAL, walls TEXT, "
                "PRIMARY KEY(task_type, model, domain, class))"
            )
            yield db
            db.commit()
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            # ValueError/TypeError: a corrupt counter (text or NULL) read back from the cache.
            raise EvidenceUnavailable("evidence_store_unavailable") from exc
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def _reservoir(walls: list[float], seen: int, value: float, run_id: str) -> list[float]:
        # Deterministic reservoir: the slot comes from the row's own UUID.
        if len(walls) < _RESERVOIR:
            return [*walls, value]
        slot = UUID(run_id).int % seen
        if slot < _RESERVOIR:
            walls = list(walls)
            walls[slot] = value
        return walls

    def sync(self, classes: Mapping[str, EvidenceClass]) -> None:
        with self._transaction() as db:
            row = db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
            offset = 0 if row is None else int(row[0])
            if not self.journal.exists():
                return
            with self.journal.open("rb") as stream:
                stream.seek(0, 2)
                size = stream.tell()
                if size < offset:
                    db.execute("DELETE FROM versions")
                    db.execute("DELETE FROM roles")
                    offset = 0
                if offset == size:
                    return
                if offset == 0:
                    self.full_replays += 1
                stream.seek(offset)
                while True:
                    line = stream.readline()
                    if not line.endswith(b"\n"):
                        break
                    offset = stream.tell()
                    try:
                        telemetry = TelemetryRow.model_validate_json(line)
                    except ValidationError:
                        continue
                    evidence = classes.get(telemetry.model)
                    if evidence is not None:
                        self._add(db, telemetry, evidence)
            db.execute(
                "INSERT INTO meta VALUES ('offset', ?) ON CONFLICT(key) DO UPDATE "
                "SET value=excluded.value",
                (offset,),
            )

    def _add(self, db: sqlite3.Connection, row: TelemetryRow, evidence: EvidenceClass) -> None:
        if row.task_type not in _NOT_DELIVERY:
            key = (row.spec_hash, row.harness_version, evidence)
            found = db.execute(
                "SELECT deliveries, accepted, cost, walls FROM versions "
                "WHERE spec_hash=? AND version=? AND class=?",
                key,
            ).fetchone()
            count, accepted, cost, walls = (0, 0, 0.0, "[]") if found is None else found
            count = int(count) + 1
            db.execute(
                "INSERT OR REPLACE INTO versions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    *key,
                    count,
                    int(accepted) + int(row.accepted),
                    float(cost) + row.cost_usd,
                    json.dumps(
                        self._reservoir(_walls(walls), count, row.wall_time_s, row.run_id)
                    ),
                ),
            )
        role_key = (row.task_type, row.model, row.domain, evidence)
        found = db.execute(
            "SELECT n, cost, walls FROM roles WHERE task_type=? AND model=? AND domain=? "
            "AND class=?",
            role_key,
        ).fetchone()
        n, cost, walls = (0, 0.0, "[]") if found is None else found
        n = int(n) + 1
        db.execute(
            "INSERT OR REPLACE INTO roles VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                *role_key,
                n,
                float(cost) + row.cost_usd,
                json.dumps(self._reservoir(_walls(walls), n, row.wall_time_s, row.run_id)),
            ),
        )

    def version(
        self, spec_hash: str, version: int, evidence: EvidenceClass
    ) -> tuple[int, list[float]]:
        # Decode inside the transaction so a corrupt row becomes EvidenceUnavailable.
        with self._transaction() as db:
            found = db.execute(
                "SELECT deliveries, walls FROM versions WHERE spec_hash=? AND version=? "
                "AND class=?",
                (spec_hash, version, evidence),
            ).fetchone()
            if found is None:
                return 0, []
            return int(found[0]), _walls(found[1])

    def role(
        self, task_type: str, model: str, domain: str | None, evidence: EvidenceClass
    ) -> tuple[int, float, list[float]]:
        query = "SELECT n, cost, walls FROM roles WHERE task_type=? AND model=? AND class=?"
        values: tuple[str, ...] = (task_type, model, evidence)
        if domain is not None:
            query += " AND domain=?"
            values = (*values, domain)
        # Decode inside the transaction so a corrupt row becomes EvidenceUnavailable.
        with self._transaction() as db:
            rows = db.execute(query, values).fetchall()
            n = sum(int(row[0]) for row in rows)
            cost = sum(float(row[1]) for row in rows)
            walls = [item for row in rows for item in _walls(row[2])]
        return n, cost, walls


# --------------------------------------------------------------------- index


class EvidenceIndex:
    """Reads evidence for candidates; never licenses, installs or approves anything."""

    def __init__(
        self,
        registry: Registry,
        library: HarnessLibrary | None,
        telemetry: TelemetryStore,
        log: RouteLog,
        workspace: Path,
        *,
        providers: Mapping[str, Provider],
        promotion_reuses: int,
    ) -> None:
        self.registry = registry
        self.library = library
        self.telemetry = telemetry
        self.log = log
        self.workspace = Path(workspace)
        self.providers: Mapping[str, Provider] = dict(providers)
        self.promotion_reuses = promotion_reuses
        self._classes = {
            categorical(model): PROVIDER_EVIDENCE[provider] for model, provider in providers.items()
        }
        self._cache = _TelemetryCache(
            self.log.root / "evidence.sqlite", Path(telemetry.directory) / "runs.jsonl"
        )
        self._bench: dict[Path, tuple[tuple[int, int], list[dict[str, object]]]] = {}

    @property
    def full_replays(self) -> int:
        """Full telemetry journal replays this index performed (a lost or new cache)."""
        return self._cache.full_replays

    def evidence_class(self, model: str) -> EvidenceClass | None:
        provider = self.providers.get(model)
        return None if provider is None else PROVIDER_EVIDENCE[provider]

    # -------------------------------------------------------------- versions

    def installed(self, sig: TaskSignature) -> tuple[VersionEvidence, ...]:
        """Current registry revisions of the same slot, shape or family, freshly loaded."""
        try:
            self._cache.sync(self._classes)
            entries: dict[str, tuple[RegistryEntry, _Match]] = {}
            queries: tuple[tuple[_Match, tuple[RegistryEntry, ...]], ...] = (
                ("slot", self.registry.find(slot=sig.slot)),
                ("shape", self.registry.find(shape=sig.shape)),
                ("family", self.registry.find_family(sig.family)),
            )
            for match, found in queries:
                for entry in found:
                    entries.setdefault(entry.name, (entry, match))
            return tuple(
                self._installed(entry).model_copy(update={"match": match})
                for entry, match in entries.values()
            )
        except (RegistryError, RouteLogError, sqlite3.Error, OSError, RecursionError) as exc:
            raise EvidenceUnavailable("evidence_store_unavailable") from exc

    def templates(
        self, sig: TaskSignature, models: tuple[str, ...], limit: int
    ) -> tuple[VersionEvidence, ...]:
        """Library templates of the probe shape for each ladder model (fresh reads)."""
        if self.library is None:
            return ()
        try:
            self._cache.sync(self._classes)
            found: list[VersionEvidence] = []
            seen: set[str] = set()
            for model in dict.fromkeys(models):
                for row in self.library.candidates(sig.shape, model, limit):
                    if row.key not in seen:
                        seen.add(row.key)
                        found.append(self._template(row, model))
            return tuple(found)
        except (LibraryError, RouteLogError, sqlite3.Error, OSError, RecursionError) as exc:
            raise EvidenceUnavailable("evidence_store_unavailable") from exc

    def earlier_revisions(self, name: str, current: int) -> tuple[VersionEvidence, ...]:
        """Earlier revisions of ``name`` that are licensed on a fresh load (rollback only)."""
        found = []
        try:
            for entry in self.registry.history(name):
                if entry.revision >= current:
                    continue
                evidence = self._installed(entry, drift=False)
                if evidence.licensed and not evidence.consumed:
                    found.append(evidence)
        except (
            RegistryError, RouteLogError, EvidenceUnavailable, sqlite3.Error, OSError,
            RecursionError,
        ):
            return ()  # A proposal is optional; unreadable history proposes nothing.
        return tuple(found)

    def stage4_history(self, sig: TaskSignature) -> tuple[bool, int, int]:
        """(slot has an unlicensed outcome with no later licensed one, shape unlicensed,
        shape licensed) from the route log's outcomes."""
        try:
            open_failure = False
            for _, outcome in self.log.outcomes(slot=sig.slot):
                if outcome.outcome == "unlicensed":
                    open_failure = True
                elif outcome.outcome == "licensed":
                    open_failure = False
            unlicensed = licensed = 0
            for _, outcome in self.log.outcomes(shape=sig.shape):
                unlicensed += outcome.outcome == "unlicensed"
                licensed += outcome.outcome == "licensed"
            return open_failure, unlicensed, licensed
        except RouteLogError as exc:
            raise EvidenceUnavailable("evidence_store_unavailable") from exc

    def _installed(self, entry: RegistryEntry, *, drift: bool = True) -> VersionEvidence:
        ref = InstalledRef(
            name=entry.name, revision=entry.revision, artifact_digest="", spec_hash=entry.spec_hash
        )
        try:
            artifact = HarnessArtifact.load(entry.directory)
            ref = ref.model_copy(update={"artifact_digest": artifact.artifact_digest})
            licensed = (
                artifact.licensed
                and artifact.spec.spec_hash == entry.spec_hash
                and artifact.version == entry.artifact_version
            )
            if licensed:
                check_subharness_tree(artifact)
        except (OSError, ValueError, TypeError, RecursionError):
            # RecursionError: a pathologically nested JSON file is damage (filter 1).
            return VersionEvidence(origin="installed", ref=ref, model="", licensed=False)
        if not licensed:
            return VersionEvidence(
                origin="installed", ref=ref, model=artifact.model, spec=artifact.spec,
                signature=signature(artifact.spec), licensed=False,
            )
        consumed = (
            artifact.spec.lifecycle != Lifecycle.INSTALL
            or (entry.directory / ".consumed").exists()
        )
        state: Literal["no_window", "stable", "drifting"] | None = None
        if drift:
            try:
                state = self.registry.drift(entry.name)
            except RegistryError as exc:
                if str(exc) == "storage_error":
                    raise
                # The entry changed between the fresh load and the drift read.
                return VersionEvidence(
                    origin="installed", ref=ref, model=artifact.model, spec=artifact.spec,
                    signature=signature(artifact.spec), licensed=False,
                )
        return self._measured(artifact, "installed", ref, consumed=consumed, drift=state)

    def _template(self, row: TemplateEvidence, model: str) -> VersionEvidence:
        artifact = row.artifact
        ref = TemplateRef(key=row.key, artifact_digest=row.key, model=model)
        counters: dict[str, object] = {
            "successes": row.successes,
            "failures": row.failures,
            "promoted": row.promoted,
            "sequence": row.sequence,
            "r": wilson(row.successes, row.successes + row.failures, 1.0),
        }
        if artifact is None:
            # Evicted before this call: not loaded. Otherwise the fresh read failed.
            return VersionEvidence(
                origin="template", ref=ref, model=model,
                licensed=None if row.evicted else False, evicted=row.evicted,
                evidence_class=self.evidence_class(model),
            ).model_copy(update=counters)
        evidence = self._measured(artifact, "template", ref, consumed=False, drift=None)
        labels = evidence.evidence + (("template_promoted",) if row.promoted else ())
        return evidence.model_copy(update={"evicted": row.evicted, "evidence": labels, **counters})

    def _measured(
        self,
        artifact: HarnessArtifact,
        origin: Literal["installed", "template"],
        ref: InstalledRef | TemplateRef,
        *,
        consumed: bool,
        drift: Literal["no_window", "stable", "drifting"] | None,
    ) -> VersionEvidence:
        report = artifact.report
        assert report is not None  # Only licensed artifacts are measured.
        n = report.held_out_count
        m = quality(report.floor_kind, report.floor_score) - quality(
            report.floor_kind, report.floor_limit
        )
        sec_lcb = 0.0
        if (
            artifact.spec.secondary_floor is not None
            and report.secondary_floor_kind is not None
            and report.secondary_floor_score is not None
            and report.secondary_floor_limit is not None
        ):
            kind = report.secondary_floor_kind
            sec_lcb = conservative_margin(
                quality(kind, report.secondary_floor_score)
                - quality(kind, report.secondary_floor_limit),
                n,
            )
        evidence_class = self.evidence_class(artifact.model)
        labels: list[EvidenceLabel] = ["stage4_signed"]
        if evidence_class is not None:
            labels.append(evidence_class)
        latency: float = report.compute_s
        basis: Literal["measured", "bound"] = "bound"
        if evidence_class is not None:
            count, walls = self._cache.version(
                artifact.spec.spec_hash, artifact.version, evidence_class
            )
            if count >= _MEASURED_MIN and walls:
                latency, basis = float(statistics.median(walls)), "measured"
        labels.append("latency_measured" if basis == "measured" else "latency_bound")
        if drift == "no_window":
            labels.append("drift_window_none")
        elif drift == "stable":
            labels.append("drift_stable")
        if self._bench_joined(artifact.artifact_digest, artifact.spec.spec_hash):
            labels.append("bench_joined")
        good, bad = self.log.ratings(categorical(artifact.artifact_digest))
        h = 0.5
        if good + bad >= _HUMAN_MIN:
            h = wilson(good, good + bad, 1.0)
            labels.append("human_rated")
        return VersionEvidence(
            origin=origin,
            ref=ref,
            model=artifact.model,
            spec=artifact.spec,
            signature=signature(artifact.spec),
            licensed=True,
            consumed=consumed,
            drift=drift,
            evidence_class=evidence_class,
            m=m,
            m_lcb=conservative_margin(m, n),
            sec_lcb=sec_lcb,
            h=h,
            cost_usd=report.mean_cost_usd if report.mean_cost_usd is not None else report.cost_usd,
            compute_s=report.compute_s,
            report_cost_usd=report.cost_usd,
            latency_s=latency,
            latency_basis=basis,
            k=report.component_count,
            held_out_count=n,
            evidence=tuple(labels),
        )

    # ----------------------------------------------------------------- bench

    def _bench_rows(self) -> list[dict[str, object]]:
        """Bench rows, parsed as plain JSON with six fields; never imports the bench."""
        rows: list[dict[str, object]] = []
        root = self.workspace / "bench"
        if not root.is_dir():
            return rows
        keep = ("spec_hash", "artifact_digest", "licensed", "held_out_score", "evidence",
                "floor_origin")
        for path in sorted(root.glob("*/rows.jsonl")):
            try:
                stat = path.stat()
                stamp = (stat.st_size, stat.st_mtime_ns)
                cached = self._bench.get(path)
                if cached is None or cached[0] != stamp:
                    parsed: list[dict[str, object]] = []
                    try:
                        text = path.read_text(encoding="utf-8")
                    except UnicodeDecodeError as exc:
                        # An undecodable store is unreadable, not empty (P5: fail open).
                        raise EvidenceUnavailable("evidence_store_unavailable") from exc
                    for line in text.splitlines():
                        try:
                            value = json.loads(line)
                        except (ValueError, RecursionError):
                            continue  # A malformed (or too deeply nested) line is skipped.
                        if isinstance(value, dict):
                            parsed.append({key: value.get(key) for key in keep})
                    cached = (stamp, parsed)
                    self._bench[path] = cached
                rows.extend(cached[1])
            except OSError:
                continue
        return rows

    def _bench_joined(self, artifact_digest: str, spec_hash: str) -> bool:
        for row in self._bench_rows():
            digest = row.get("artifact_digest")
            if digest == artifact_digest or (digest is None and row.get("spec_hash") == spec_hash):
                return True
        return False

    def bench_counts(self, artifact_digest: str, spec_hash: str) -> dict[str, int]:
        """Joined bench rows per evidence class: display only, never a ranking input.

        An unreadable bench store shows no counts rather than raising.
        """
        counts: dict[str, int] = {}
        try:
            rows = self._bench_rows()
        except EvidenceUnavailable:
            return counts
        for row in rows:
            digest = row.get("artifact_digest")
            if digest == artifact_digest or (digest is None and row.get("spec_hash") == spec_hash):
                label = str(row.get("evidence"))
                counts[label] = counts.get(label, 0) + 1
        return counts

    # ----------------------------------------------------------------- roles

    def role_stats(self, role: RoleName, variant: _Variant, domain: str | None) -> RoleStats:
        """Weighted outcomes of one role variant in its current evidence class.

        ``n`` and ``successes`` come from content-free ``RoleRecord`` outcomes joined by
        ``decision_id``, weighted by floor origin; cost and wall come from the role's
        own telemetry rows. ``domain=None`` is the role-wide bucket.
        """
        evidence = self.evidence_class(variant.model)
        if evidence is None:
            raise ValueError("route_model_unknown")
        model = categorical(variant.model)
        key = variant_category(variant)
        wanted = None if domain is None else categorical(domain)
        n = successes = 0.0
        try:
            for record in self.log.roles(role):
                if (
                    record.model != model
                    or record.variant != key
                    or record.evidence_class != evidence
                    or (wanted is not None and record.domain != wanted)
                    or record.outcome in (None, "excluded")
                ):
                    continue
                weight = floor_origin_weight(record.floor_origin)
                n += weight
                if record.outcome == "success" or (
                    record.outcome == "partial" and record.score == 1
                ):
                    successes += weight
            self._cache.sync(self._classes)
            count, cost, walls = self._cache.role(
                categorical(ROLE_TASK_TYPES[role]), model, wanted, evidence
            )
        except RouteLogError as exc:
            raise EvidenceUnavailable("evidence_store_unavailable") from exc
        return RoleStats(
            n=n,
            successes=successes,
            mean_cost_usd=cost / count if count else None,
            median_wall_s=float(statistics.median(walls)) if walls else None,
            evidence_class=evidence,
        )

