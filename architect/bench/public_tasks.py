"""Public bench tasks (Phase 14 Section 10): settings, pinned data and their checks.

The ``receipts`` task of suite ``v2`` reads two committed inputs. The settings file
``examples/bench/suite-v2/receipts.json`` holds the bar, the run budget and the arm
settings; the pre-registration freezes it. The data under ``examples/receipts`` is
CORD v2 receipt text rebuilt from the dataset's annotations (CC BY 4.0, never
images), written with sha256 manifests by ``scripts/fetch_bench_data.py``.

``load_public`` checks everything before any spend. A missing data file is
``bench_dataset_missing``; any difference from the manifests (a changed case, a
changed rule text, an overlap between splits, a partition that breaks the even/odd
rule) is ``bench_dataset_mismatch``. Rows from these tasks claim the pre-registered
dataset, so a mismatch refuses the run instead of relabelling it. A missing or
invalid settings file is ``bench_suite_unknown``, as a missing siblings file is
for suite ``v1``.

Case hashes follow ``architect.floors.provenance`` (Phase 14 Section 5.1): the
sha256 of the canonical JSON of ``{"case_id", "task", "expected", "metadata": {}}``,
and for a list, of the list of those objects in file order. Nothing here is
content: settings, labels, counts and hashes only.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Literal, Self

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    ValidationError,
    model_validator,
)

from architect.floors import EvaluationCase
from architect.models.images import SHA256_HEX
from architect.spec import Budget

from .rows import BenchError, DeliverySplit, FloorSplit, InvalidPolicy, ReplyJson
from .text_tasks import canonical_json

__all__ = [
    "FLOOR_SPLITS",
    "PUBLIC_SPLITS",
    "REPOSITORY",
    "ManifestCase",
    "PublicData",
    "PublicSettings",
    "PublicSplit",
    "SplitManifest",
    "case_sha256",
    "cases_sha256",
    "clear_cache",
    "delivery_cases",
    "load_public",
    "load_settings",
    "manifest_origin",
    "settings_path",
]

PublicSplit = Literal["pilot", "rehearsal", "evidence", "delivery"]
PUBLIC_SPLITS: tuple[PublicSplit, ...] = ("pilot", "rehearsal", "evidence", "delivery")
# The splits a floor is drawn from; their manifests carry the even/odd partition.
FLOOR_SPLITS: tuple[FloorSplit, ...] = ("evidence", "rehearsal")
# The checkout this package lives in; every public source path is relative to it.
REPOSITORY = Path(__file__).resolve().parents[2]
_MANIFEST_LIMIT = 16 * 1024 * 1024
_FILE_LIMIT = 64 * 1024 * 1024

Nonempty = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]


def case_sha256(case: EvaluationCase) -> str:
    """sha256 of one case as its manifest records it (the case's metadata ignored)."""
    return hashlib.sha256(canonical_json(_hashed(case)).encode("utf-8")).hexdigest()


def cases_sha256(cases: Sequence[EvaluationCase]) -> str:
    """sha256 of the list of hashed case objects, in the given order."""
    payload: list[JsonValue] = [_hashed(case) for case in cases]
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _hashed(case: EvaluationCase) -> dict[str, JsonValue]:
    return {
        "case_id": case.case_id,
        "task": case.task,
        "expected": case.expected,
        "metadata": {},
    }


class PublicSettings(BaseModel):
    """``examples/bench/suite-v2/<task>.json``: the bar, the run budget and the arm settings."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    settings_version: Literal[1]
    task_id: Literal["receipts"]
    source: Literal["examples/receipts"]  # relative to the repository root
    floor_limit: Annotated[float, Field(ge=0, le=1)]  # the bar
    run_budget: Budget | None  # None: lifted from quotes, as suite v1 does
    invalid_policy: InvalidPolicy
    max_invalid_fraction: Annotated[float, Field(ge=0, le=0.1)]
    # The Spec's own bounds for ``input_limit_chars`` (Section 3.1).
    input_limit_chars: Annotated[int, Field(ge=256, le=1_000_000, strict=True)] | None
    reply_json: ReplyJson
    frozen: bool

    @model_validator(mode="after")
    def one_fraction_per_default_policy(self) -> Self:
        # The Spec refuses this pair (Section 3.1, rule 1), so it is refused here first.
        if self.invalid_policy == "fail_partition" and self.max_invalid_fraction != 0.02:
            raise ValueError("fail_partition keeps the default invalid fraction")
        return self

    @property
    def settings_sha256(self) -> str:
        """sha256 of the canonical JSON of the parsed settings."""
        payload: JsonValue = self.model_dump(mode="json")
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class ManifestCase(BaseModel):
    """One case entry of a split manifest; the keys the loader checks."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    case_id: Nonempty
    case_sha256: SHA256_HEX
    source: str | None = None  # "train/12", "validation/3" or "test/0"
    partition: Literal["tuning", "held_out"] | None = None


class _Partition(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    tuning: Annotated[int, Field(ge=0)]
    held_out: Annotated[int, Field(ge=0)]


class SplitManifest(BaseModel):
    """``examples/receipts/manifests/<split>.json`` as the data script writes it.

    It reads the keys of ``architect.floors.provenance.DatasetManifest`` plus the
    split name and the floor partition, and ignores the rest (byte hashes of the
    files, sources, flags).
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    manifest_version: Literal[1]
    dataset: Nonempty
    dataset_revision: Nonempty
    license: Nonempty
    rule_version: Nonempty
    rules_sha256: SHA256_HEX | None = None
    split: Nonempty
    count: Annotated[int, Field(ge=0)]
    cases_sha256: SHA256_HEX | None = None
    partition: _Partition | None = None
    cases: tuple[ManifestCase, ...]

    @model_validator(mode="after")
    def cases_match_count(self) -> Self:
        if len(self.cases) != self.count:
            raise ValueError("Manifest case count differs from its cases")
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("Manifest case ids repeat")
        return self


class PublicData(BaseModel):
    """A public task's settings, task file, checked splits and their manifests."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    settings: PublicSettings
    task: dict[str, JsonValue]
    splits: dict[PublicSplit, tuple[EvaluationCase, ...]]
    manifests: dict[PublicSplit, SplitManifest]

    @property
    def rules(self) -> str:
        rules = self.task.get("rules")
        assert isinstance(rules, str)  # checked by load_public
        return rules

    def task_schema(self, name: Literal["input_schema", "output_schema"]) -> dict[str, JsonValue]:
        value = self.task.get(name)
        assert isinstance(value, dict)  # checked by load_public
        return value

    def digest_entry(self) -> dict[str, JsonValue]:
        """The ``public`` entry of the suite digest (Section 10.1)."""
        manifest = self.manifests["evidence"]
        splits: dict[str, JsonValue] = {
            split: cases_sha256(self.splits[split]) for split in PUBLIC_SPLITS
        }
        return {
            "settings_sha256": self.settings.settings_sha256,
            "dataset": manifest.dataset,
            "dataset_revision": manifest.dataset_revision,
            "rule_version": manifest.rule_version,
            "rules_sha256": manifest.rules_sha256,
            "splits": splits,
        }


_CACHE: dict[tuple[str, Path], PublicData] = {}
_SETTINGS: dict[tuple[str, Path], PublicSettings] = {}


def clear_cache() -> None:
    """Forget every loaded setting and public task (tests that edit a copy of the data)."""
    _CACHE.clear()
    _SETTINGS.clear()


def settings_path(task_id: str, root: Path | None = None) -> Path:
    return (root or REPOSITORY) / "examples" / "bench" / "suite-v2" / f"{task_id}.json"


def load_settings(task_id: str = "receipts", *, root: Path | None = None) -> PublicSettings:
    """The task's settings file, cached per process; missing, unreadable or invalid
    is ``bench_suite_unknown``."""
    key = (task_id, (root or REPOSITORY).resolve())
    cached = _SETTINGS.get(key)
    if cached is not None:
        return cached
    try:
        text = settings_path(task_id, key[1]).read_text(encoding="utf-8")
        settings = PublicSettings.model_validate_json(text)
    except (OSError, UnicodeError, ValueError):
        raise BenchError("bench_suite_unknown") from None
    if settings.task_id != task_id:
        raise BenchError("bench_suite_unknown")
    _SETTINGS[key] = settings
    return settings


def load_public(task_id: str = "receipts", *, root: Path | None = None) -> PublicData:
    """Settings, task file and the four checked splits; cached per process.

    ``root`` is the checkout holding ``examples/`` (this one by default). The checks
    of Section 10.3 all run here, before anything can be spent.
    """
    base = (root or REPOSITORY).resolve()
    key = (task_id, base)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    settings = load_settings(task_id, root=base)
    source = base / settings.source
    task = _task_file(source / "task.json")
    manifests = {
        split: _manifest(source / "manifests" / f"{split}.json") for split in PUBLIC_SPLITS
    }
    splits = {split: _cases(source / "cases" / f"{split}.jsonl") for split in PUBLIC_SPLITS}
    _check(task, manifests, splits)
    data = PublicData(settings=settings, task=task, splits=splits, manifests=manifests)
    _CACHE[key] = data
    return data


def delivery_cases(
    data: PublicData, split: DeliverySplit, n: int | Literal["all"]
) -> tuple[EvaluationCase, ...]:
    """The replay set: the whole split for ``all``, else its first ``n`` cases in
    manifest order (``bench_delivery_too_large`` above the split's size)."""
    cases = data.splits[split]
    if isinstance(n, str):  # "all"
        return cases
    if n > len(cases):
        raise BenchError("bench_delivery_too_large")
    return cases[:n]


def manifest_origin(
    cases: Sequence[EvaluationCase], manifest: Path
) -> Literal["public_dataset", "synthetic"]:
    """The Section 5.2 label of ``cases`` against a sidecar manifest, first rule wins:
    an unreadable manifest, a stamped case, another count, or any case missing from
    or differing from its entry is ``synthetic``; otherwise ``public_dataset``."""
    try:
        if manifest.stat().st_size > _MANIFEST_LIMIT:
            return "synthetic"
        parsed = _ProvenanceManifest.model_validate_json(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return "synthetic"
    if any(case.metadata.get("synthetic") is True for case in cases):
        return "synthetic"
    if len(cases) != parsed.count:
        return "synthetic"
    entries = {entry.case_id: entry.case_sha256 for entry in parsed.cases}
    if any(entries.get(case.case_id) != case_sha256(case) for case in cases):
        return "synthetic"
    return "public_dataset"


class _ProvenanceManifest(BaseModel):
    """The keys a provenance check reads (``DatasetManifest`` of Section 5.1)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    manifest_version: Literal[1]
    dataset: Nonempty
    dataset_revision: Nonempty
    license: Nonempty
    rule_version: Nonempty
    count: Annotated[int, Field(ge=0)]
    cases: tuple[ManifestCase, ...]

    @model_validator(mode="after")
    def cases_match_count(self) -> Self:
        if len(self.cases) != self.count:
            raise ValueError("Manifest case count differs from its cases")
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("Manifest case ids repeat")
        return self


# ---------------------------------------------------------------------- reading


def _read(path: Path, limit: int) -> str:
    """A file's text; absent is ``bench_dataset_missing``, unreadable a mismatch."""
    try:
        if path.stat().st_size > limit:
            raise BenchError("bench_dataset_mismatch")
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise BenchError("bench_dataset_missing") from None
    except (OSError, UnicodeError):
        raise BenchError("bench_dataset_mismatch") from None


def _task_file(path: Path) -> dict[str, JsonValue]:
    try:
        task = json.loads(_read(path, _MANIFEST_LIMIT))
    except ValueError:
        raise BenchError("bench_dataset_mismatch") from None
    if not isinstance(task, dict):
        raise BenchError("bench_dataset_mismatch")
    for key in ("rules", "rule_version", "rules_sha256", "dataset", "dataset_revision", "license"):
        if not isinstance(task.get(key), str) or not task[key]:
            raise BenchError("bench_dataset_mismatch")
    for key in ("input_schema", "output_schema"):
        schema = task.get(key)
        if not isinstance(schema, dict):
            raise BenchError("bench_dataset_mismatch")
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError:
            raise BenchError("bench_dataset_mismatch") from None
    return task


def _manifest(path: Path) -> SplitManifest:
    try:
        return SplitManifest.model_validate_json(_read(path, _MANIFEST_LIMIT))
    except ValidationError:
        raise BenchError("bench_dataset_mismatch") from None


def _cases(path: Path) -> tuple[EvaluationCase, ...]:
    cases: list[EvaluationCase] = []
    try:
        for line in _read(path, _FILE_LIMIT).splitlines():
            if line.strip():
                cases.append(EvaluationCase.model_validate_json(line))
    except ValidationError:
        raise BenchError("bench_dataset_mismatch") from None
    return tuple(cases)


# --------------------------------------------------------------------- checking


def _check(
    task: Mapping[str, JsonValue],
    manifests: Mapping[PublicSplit, SplitManifest],
    splits: Mapping[PublicSplit, Sequence[EvaluationCase]],
) -> None:
    """Section 10.3 rules 1 to 5; any failure is ``bench_dataset_mismatch``."""
    rules = task["rules"]
    assert isinstance(rules, str)
    if hashlib.sha256(rules.encode("utf-8")).hexdigest() != task["rules_sha256"]:
        raise BenchError("bench_dataset_mismatch")
    for name, manifest in manifests.items():
        identity = (
            manifest.dataset,
            manifest.dataset_revision,
            manifest.license,
            manifest.rule_version,
            manifest.rules_sha256,
        )
        expected = tuple(
            task[key]
            for key in ("dataset", "dataset_revision", "license", "rule_version", "rules_sha256")
        )
        if identity != expected or manifest.split != name:
            raise BenchError("bench_dataset_mismatch")
        _check_split(manifest, splits[name])
    _check_schemas(task, splits)
    seen_ids: set[str] = set()
    seen_hashes: set[str] = set()
    for name in PUBLIC_SPLITS:
        ids = {case.case_id for case in splits[name]}
        hashes = {case_sha256(case) for case in splits[name]}
        if ids & seen_ids or hashes & seen_hashes:
            raise BenchError("bench_dataset_mismatch")  # one case in two splits
        seen_ids |= ids
        seen_hashes |= hashes
    for name in FLOOR_SPLITS:
        _check_partition(manifests[name], splits[name])


def _check_split(manifest: SplitManifest, cases: Sequence[EvaluationCase]) -> None:
    if len(cases) != manifest.count:
        raise BenchError("bench_dataset_mismatch")
    if len({case.case_id for case in cases}) != len(cases):
        raise BenchError("bench_dataset_mismatch")
    entries = {entry.case_id: entry.case_sha256 for entry in manifest.cases}
    if any(entries.get(case.case_id) != case_sha256(case) for case in cases):
        raise BenchError("bench_dataset_mismatch")
    if manifest.cases_sha256 != cases_sha256(cases):
        raise BenchError("bench_dataset_mismatch")  # also the file order


def _check_schemas(
    task: Mapping[str, JsonValue], splits: Mapping[PublicSplit, Sequence[EvaluationCase]]
) -> None:
    input_schema, output_schema = task["input_schema"], task["output_schema"]
    assert isinstance(input_schema, dict) and isinstance(output_schema, dict)  # _task_file
    inputs = Draft202012Validator(input_schema)
    outputs = Draft202012Validator(output_schema)
    for cases in splits.values():
        for case in cases:
            if not inputs.is_valid(case.task) or not outputs.is_valid(case.expected):
                raise BenchError("bench_dataset_mismatch")


def _check_partition(manifest: SplitManifest, cases: Sequence[EvaluationCase]) -> None:
    """The floor rule (sorted case ids, even positions tune, odd are held out)."""
    count = len(cases)
    partition = manifest.partition
    if partition is None or (partition.tuning, partition.held_out) != (
        (count + 1) // 2,
        count // 2,
    ):
        raise BenchError("bench_dataset_mismatch")
    sides = {
        case_id: "tuning" if position % 2 == 0 else "held_out"
        for position, case_id in enumerate(sorted(case.case_id for case in cases))
    }
    if any(entry.partition != sides[entry.case_id] for entry in manifest.cases):
        raise BenchError("bench_dataset_mismatch")
