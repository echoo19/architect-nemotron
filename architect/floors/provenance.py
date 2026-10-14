"""Public dataset provenance for held-out floors (Phase 14, Section 5).

A floor whose cases match a pinned sidecar manifest, case by case, is labelled
``public_dataset`` and shown as "CORD v2 · CC BY 4.0 · 200 receipts". Any other
outcome labels it SYNTHETIC with a fixed reason. Provenance only labels a floor: it
never refuses one, and the floor loads and evaluates exactly as it would without it.

The manifest format is the one ``scripts/fetch_bench_data.py`` writes. Only parsed
content is compared (the case ids and case hashes); the byte hashes in a manifest's
``files`` map are not checked here. Results hold labels and counts, never case content.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from architect.spec import SHA256

from .base import EvaluationCase, FloorError

__all__ = [
    "KNOWN_DATASETS",
    "LICENSE_NAMES",
    "MAX_MANIFEST_BYTES",
    "DatasetManifest",
    "FloorProvenance",
    "ManifestCase",
    "ProvenanceReason",
    "case_sha256",
    "cases_sha256",
    "check_floor",
    "load_manifest",
]

Nonempty = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
NonnegativeInt = Annotated[int, Field(ge=0, strict=True)]
MAX_MANIFEST_BYTES = 16 * 1024 * 1024

# Dataset id -> (title, plural unit) for manifests that name neither.
KNOWN_DATASETS: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        "naver-clova-ix/cord-v2": ("CORD v2", "receipts"),
        "PolyAI/banking77": ("Banking77", "messages"),
    }
)
# SPDX id -> display name; any other id is shown as written.
LICENSE_NAMES: Mapping[str, str] = MappingProxyType(
    {"CC-BY-4.0": "CC BY 4.0", "MIT": "MIT", "Apache-2.0": "Apache 2.0", "CC0-1.0": "CC0 1.0"}
)

ProvenanceReason = Literal[
    "matched", "manifest_unreadable", "stamped_synthetic", "count_mismatch", "case_mismatch"
]
_REASONS: Mapping[str, str] = MappingProxyType(
    {
        "manifest_unreadable": "the manifest could not be read",
        "stamped_synthetic": "cases are stamped synthetic",
        "count_mismatch": "the case count differs from the manifest",
    }
)


class ManifestCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    case_id: Nonempty
    case_sha256: SHA256


class DatasetManifest(BaseModel):
    """The keys of a data-script manifest that provenance reads; the rest are ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    manifest_version: Literal[1]
    dataset: Nonempty
    dataset_revision: Nonempty
    license: Nonempty  # an SPDX id
    rule_version: Nonempty
    count: NonnegativeInt
    cases: tuple[ManifestCase, ...]
    cases_sha256: SHA256 | None = None
    rules_sha256: SHA256 | None = None
    title: Nonempty | None = None  # display name; else KNOWN_DATASETS, else the dataset id
    unit: Nonempty | None = None  # plural noun; else KNOWN_DATASETS, else "cases"
    # Retake: a line to show under every score on these cases ("These are development
    # receipts. ..."); a published dataset's manifest has none.
    note: Nonempty | None = None

    @model_validator(mode="after")
    def complete(self) -> Self:
        if len(self.cases) != self.count:
            raise ValueError("Manifest count differs from its case list")
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("Duplicate manifest case ids")
        return self

    def display(self) -> tuple[str, str]:
        known = KNOWN_DATASETS.get(self.dataset)
        title = self.title or (known[0] if known else self.dataset)
        unit = self.unit or (known[1] if known else "cases")
        return title, unit


class FloorProvenance(BaseModel):
    """Labels and counts only: where a floor's cases come from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    origin: Literal["public_dataset", "synthetic"]
    reason: ProvenanceReason
    dataset: str | None
    dataset_revision: str | None
    license: str | None
    rule_version: str | None
    title: str | None
    unit: str
    cases: int
    matched: int

    def card_text(self, sep: str = " · ") -> str:
        """``CORD v2 · CC BY 4.0 · 200 receipts``, or ``N cases · SYNTHETIC · <reason>``."""
        if self.origin == "public_dataset":
            shown = LICENSE_NAMES.get(self.license or "", self.license or "")
            return f"{self.title}{sep}{shown}{sep}{self.cases} {self.unit}"
        reason = _REASONS.get(
            self.reason, f"{self.cases - self.matched} cases do not match the manifest"
        )
        return f"{self.cases} cases{sep}SYNTHETIC{sep}{reason}"


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hashed(case: EvaluationCase) -> dict[str, object]:
    # The case's own metadata is ignored, as in scripts/fetch_bench_data.py.
    return {"case_id": case.case_id, "task": case.task, "expected": case.expected, "metadata": {}}


def case_sha256(case: EvaluationCase) -> str:
    """sha256 hex of the canonical JSON of ``{case_id, task, expected, metadata: {}}``."""
    return hashlib.sha256(_canonical(_hashed(case)).encode("utf-8")).hexdigest()


def cases_sha256(cases: Sequence[EvaluationCase]) -> str:
    """sha256 hex of the canonical JSON list of those objects, in the given order."""
    payload = [_hashed(case) for case in cases]
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def load_manifest(path: Path) -> DatasetManifest:
    """Read and validate a manifest; ``FloorError`` when it is unreadable or invalid."""
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise FloorError("Manifest too large")
        return DatasetManifest.model_validate_json(raw)
    except FloorError:
        raise
    except (OSError, ValueError) as exc:
        raise FloorError("Unreadable or invalid dataset manifest") from exc


def check_floor(cases: Sequence[EvaluationCase], manifest: Path) -> FloorProvenance:
    """Label ``cases`` against ``manifest``; the first matching rule of Section 5.2 wins."""
    total = len(cases)
    try:
        pinned = load_manifest(manifest)
    except FloorError:
        return FloorProvenance(
            origin="synthetic",
            reason="manifest_unreadable",
            dataset=None,
            dataset_revision=None,
            license=None,
            rule_version=None,
            title=None,
            unit="cases",
            cases=total,
            matched=0,
        )
    entries = {entry.case_id: entry.case_sha256 for entry in pinned.cases}
    matched = sum(1 for case in cases if entries.get(case.case_id) == case_sha256(case))
    reason: ProvenanceReason
    if any(case.metadata.get("synthetic") is True for case in cases):
        reason = "stamped_synthetic"
    elif total != pinned.count:
        reason = "count_mismatch"
    elif matched != total:
        reason = "case_mismatch"
    else:
        reason = "matched"
    title, unit = pinned.display()
    return FloorProvenance(
        origin="public_dataset" if reason == "matched" else "synthetic",
        reason=reason,
        dataset=pinned.dataset,
        dataset_revision=pinned.dataset_revision,
        license=pinned.license,
        rule_version=pinned.rule_version,
        title=title,
        unit=unit,
        cases=total,
        matched=matched,
    )
