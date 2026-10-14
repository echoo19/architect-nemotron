"""Validated specifications and disk-backed, report-licensed artifacts."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SerializerFunctionWrapHandler,
    StringConstraints,
    model_serializer,
    model_validator,
)

from architect._authority import verify_report
from architect.trust import Seal

__all__ = [
    "DEFAULT_MAX_INVALID_FRACTION",
    "PLAIN_PRESETS",
    "REPORT_SIGNATURES",
    "SHA256",
    "Budget",
    "CaseCounts",
    "FloorDefinition",
    "FloorKind",
    "ForgeBinding",
    "HarnessArtifact",
    "InvalidPolicy",
    "Lifecycle",
    "PipelineStep",
    "PresetName",
    "ReplyJson",
    "RetakeCases",
    "Seal",
    "Spec",
    "SubharnessName",
    "SubharnessRef",
    "TestReport",
    "invalid_allowance",
    "schema_sha256",
]

PresetName = Literal[
    "extractor",
    "synthesizer",
    "recommender",
    "coder",
    "explorer",
    "planner",
    "verifier",
    "hypothesis_tester",
]
Nonempty = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
UnitFloat = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
NonnegativeFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]
NonnegativeInt = Annotated[int, Field(ge=0, strict=True)]
SHA256 = Digest
SubharnessName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]

# Phase 14: how a partition treats runs without schema-valid output, and how presets
# read a model's JSON reply. Both defaults keep the behaviour and hashes of every
# earlier Spec; only a user or a frozen spec file sets anything else.
InvalidPolicy = Literal["fail_partition", "count_as_miss"]
ReplyJson = Literal["strict", "first", "last", "schema"]
PLAIN_PRESETS: frozenset[str] = frozenset(
    {"extractor", "synthesizer", "recommender", "verifier", "explorer", "planner"}
)
DEFAULT_MAX_INVALID_FRACTION = 0.02
# The two report signatures (W7): the HMAC signs the canonical dump of every other
# field, and the Ed25519 seal signs that dump plus the HMAC.
REPORT_SIGNATURES = frozenset({"signature", "ed25519"})
# Retake: which cases a retake of the exam ran on. ``unused`` cases are ones no earlier
# attempt of this agent saw; ``same_exam`` is the exam of the first attempt again.
RetakeCases = Literal["unused", "same_exam"]


class _Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Lifecycle(StrEnum):
    INSTALL = "install"
    EXPIRE = "expire"


class FloorKind(StrEnum):
    HELD_OUT_TASKS = "held_out_tasks"
    HUMAN_EDIT_RATE = "human_edit_rate"
    LLM_JUDGE = "llm_judge"
    HELD_OUT_GAME_SCORE = "held_out_game_score"


class Budget(_Schema):
    """Dollar and time safety limits; token use is measured, never bounded."""

    cost_usd: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 0.1
    compute_s: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 60

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_tokens(cls, value: object) -> object:
        if isinstance(value, Mapping):
            return {key: item for key, item in value.items() if key != "tokens"}
        return value


class FloorDefinition(_Schema):
    kind: FloorKind
    limit: UnitFloat
    dataset: Nonempty
    minimum_samples: Annotated[int, Field(gt=0, strict=True)] = 30
    rubric: Nonempty | None = None
    judge_model: Nonempty | None = None
    calibration_dataset: Nonempty | None = None


class PipelineStep(_Schema):
    name: Nonempty
    preset: Nonempty
    model: Nonempty
    budget: Budget
    input_schema: dict[str, JsonValue] = Field(default_factory=dict)
    output_schema: dict[str, JsonValue] = Field(default_factory=dict)
    params: dict[str, JsonValue] = Field(default_factory=dict)


class Spec(_Schema):
    task: Nonempty
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue]
    floor: FloorDefinition
    secondary_floor: FloorDefinition | None = None
    budget: Budget
    model_target: Nonempty = "fake"
    lifecycle: Lifecycle = Lifecycle.EXPIRE
    domain: Nonempty = "general"
    preset_hint: PresetName | None = None
    execution_config: dict[str, JsonValue] = Field(default_factory=dict)
    tools: tuple[Nonempty, ...] = ()
    pipeline: tuple[PipelineStep, ...] = Field(default=(), max_length=16)
    provenance: dict[str, Literal["inferred", "user"]] = Field(default_factory=dict)
    # Phase 14 (docs/interfaces/phase14-nebius-evidence.md, Section 3.1). Every dump
    # omits these at their defaults (``_omit_defaults``), so the hash, spec.json and
    # emitted harness bytes of every Spec that leaves them alone are unchanged.
    invalid_policy: InvalidPolicy = "fail_partition"
    max_invalid_fraction: Annotated[float, Field(ge=0, le=0.1, allow_inf_nan=False)] = (
        DEFAULT_MAX_INVALID_FRACTION
    )
    input_limit_chars: Annotated[int, Field(ge=256, le=1_000_000, strict=True)] | None = None
    reply_json: ReplyJson = "strict"
    plain: Annotated[bool, Field(strict=True)] = False

    @model_validator(mode="after")
    def unique_pipeline_names(self) -> Self:
        if len(json.dumps(self.execution_config, allow_nan=False).encode("utf-8")) > 65_536:
            raise ValueError("Execution configuration too large")
        names = [step.name for step in self.pipeline]
        if len(names) != len(set(names)):
            raise ValueError("Pipeline step names must be unique")
        return self

    @model_validator(mode="after")
    def phase14_fields(self) -> Self:
        if (
            self.invalid_policy == "fail_partition"
            and self.max_invalid_fraction != DEFAULT_MAX_INVALID_FRACTION
        ):
            # The fraction is not hashed under fail_partition, so it must stay default.
            raise ValueError("max_invalid_fraction applies only to count_as_miss")
        if self.invalid_policy == "count_as_miss" and (
            self.floor.kind != FloorKind.HELD_OUT_TASKS or self.secondary_floor is not None
        ):
            raise ValueError("count_as_miss needs one held_out_tasks floor")
        if self.plain and (
            self.preset_hint not in PLAIN_PRESETS
            or self.pipeline
            or self.execution_config.get("kind") in ("game", "competition")
        ):
            raise ValueError("plain needs a plain preset hint and no pipeline or game")
        return self

    @model_serializer(mode="wrap")
    def _omit_defaults(self, handler: SerializerFunctionWrapHandler) -> Any:
        data = handler(self)
        if isinstance(data, dict):
            if self.invalid_policy == "fail_partition":
                data.pop("invalid_policy", None)
                data.pop("max_invalid_fraction", None)
            if self.input_limit_chars is None:
                data.pop("input_limit_chars", None)
            if self.reply_json == "strict":
                data.pop("reply_json", None)
            if not self.plain:
                data.pop("plain", None)
        return data

    @property
    def spec_hash(self) -> str:
        return _digest(self.model_dump(mode="json", exclude={"provenance"}))


def invalid_allowance(spec: Spec, cases: int) -> int:
    """Invalid runs a partition of ``cases`` may hold without failing: 0 under
    fail_partition, else floor(max_invalid_fraction * cases + 1e-9)."""
    if spec.invalid_policy == "fail_partition":
        return 0
    return math.floor(spec.max_invalid_fraction * cases + 1e-9)


class CaseCounts(_Schema):
    """How one partition's runs ended; counts only."""

    cases: NonnegativeInt
    correct: NonnegativeInt | None  # None when the partition was not scored
    invalid: NonnegativeInt  # runs with no schema-valid output, budget aside
    over_budget: NonnegativeInt  # runs stopped by the per-run dollars or seconds

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.correct or 0) + self.invalid + self.over_budget > self.cases:
            raise ValueError("Counts exceed the partition")
        if self.correct is not None and self.over_budget:
            raise ValueError("A partition with a budget refusal is never scored")
        return self

    def readout(self, sep: str = " · ") -> str:
        if self.correct is not None:
            return f"correct {self.correct}/{self.cases}{sep}invalid {self.invalid}"
        return f"not scored{sep}invalid {self.invalid}{sep}over budget {self.over_budget}"


class TestReport(_Schema):
    # Prevent pytest from trying to collect this public schema as a test class.
    __test__ = False

    spec_hash: Digest
    artifact_digest: Digest
    passed: Annotated[bool, Field(strict=True)]
    floor_score: UnitFloat
    floor_limit: UnitFloat
    floor_kind: FloorKind
    tokens: NonnegativeInt
    cost_usd: NonnegativeFloat
    compute_s: NonnegativeFloat
    component_count: NonnegativeInt
    failures: tuple[str, ...] = ()
    verification_id: Nonempty
    signature: str = ""
    secondary_floor_score: UnitFloat | None = None
    secondary_floor_limit: UnitFloat | None = None
    secondary_floor_kind: FloorKind | None = None
    held_out_count: NonnegativeInt = 0
    mean_cost_usd: NonnegativeFloat | None = None
    # Phase 14: the primary floor's run counts, covered by the signature. Omitted from
    # dumps when None, so every report written earlier signs and verifies unchanged.
    held_out_counts: CaseCounts | None = None
    # Retake: which attempt at the exam this report is (absent on a first exam, which is
    # attempt 1) and which cases it ran on. Both are covered by the signature and omitted
    # from dumps when None, so every report written earlier signs and verifies unchanged.
    attempt: Annotated[int, Field(ge=2, strict=True)] | None = None
    retake_cases: RetakeCases | None = None
    # Retake: a digest of the exam's cases, on a report from an exam that allows retakes
    # (``FloorEvaluator.exam_digest``). A retake is refused when the exam it is offered is
    # not the one the agent passed. Signed, and omitted when None like the two above.
    exam_sha256: Digest | None = None
    # W7: the Ed25519 seal over every other field (``sealed_payload``), on a host holding
    # an Ed25519 key. Omitted when None, so HMAC-only reports dump exactly as before.
    ed25519: Seal | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_counts(self, handler: SerializerFunctionWrapHandler) -> Any:
        data = handler(self)
        if isinstance(data, dict):
            if self.held_out_counts is None:
                data.pop("held_out_counts", None)
            if self.attempt is None:
                data.pop("attempt", None)
            if self.retake_cases is None:
                data.pop("retake_cases", None)
            if self.exam_sha256 is None:
                data.pop("exam_sha256", None)
            if self.ed25519 is None:
                data.pop("ed25519", None)
        return data

    @model_validator(mode="after")
    def retake_fields(self) -> Self:
        if (self.attempt is None) != (self.retake_cases is None):
            raise ValueError("A retake report names both its attempt and its cases")
        return self

    def signed_payload(self) -> dict[str, Any]:
        """What the HMAC covers: every field except the two signatures."""
        payload: dict[str, Any] = self.model_dump(mode="json", exclude=set(REPORT_SIGNATURES))
        return payload

    def sealed_payload(self) -> dict[str, Any]:
        """What the Ed25519 seal covers: every field except the seal, the HMAC included,
        so a changed value anywhere in the report breaks the seal."""
        payload: dict[str, Any] = self.model_dump(mode="json", exclude={"ed25519"})
        return payload


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_json(path: Path, value: object) -> None:
    text = json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def schema_sha256(schema: Mapping[str, JsonValue]) -> str:
    """sha256 of the canonical (sorted-key, compact) JSON of one JSON Schema."""
    return _digest(dict(schema))


class SubharnessRef(_Schema):
    """A licensed child bound into a Forge parent by digest (Phase 9, Section 5)."""

    artifact_digest: SHA256
    spec_hash: SHA256
    # sha256 of the canonical JSON of the child input/output schema
    input_schema_sha256: SHA256
    output_schema_sha256: SHA256
    case_origin: Literal["projection", "authored"]

    @classmethod
    def for_child(
        cls, child: HarnessArtifact, case_origin: Literal["projection", "authored"]
    ) -> SubharnessRef:
        """Bind ``child`` exactly as it is on disk now; it must already be licensed."""
        if not child.licensed:
            raise ValueError("Only a licensed child can be bound")
        return cls(
            artifact_digest=child.artifact_digest,
            spec_hash=child.spec.spec_hash,
            input_schema_sha256=schema_sha256(child.spec.input_schema),
            output_schema_sha256=schema_sha256(child.spec.output_schema),
            case_origin=case_origin,
        )


class ForgeBinding(_Schema):
    """Sandbox language v2 binding of a Forge artifact; part of ``artifact_digest``."""

    language: Literal[2] = 2
    call_limit: Annotated[int, Field(ge=1, le=100_000, strict=True)]
    fuel: Annotated[int, Field(ge=100_000, le=100_000_000, strict=True)]
    subharnesses: Annotated[dict[SubharnessName, SubharnessRef], Field(max_length=8)] = Field(
        default_factory=dict
    )


class HarnessArtifact(_Schema):
    spec: Spec
    version: Annotated[int, Field(gt=0, strict=True)] = 1
    preset: Nonempty
    model: Nonempty
    components: tuple[Nonempty, ...]
    params: dict[str, JsonValue] = Field(default_factory=dict)
    directory: Path
    report: TestReport | None = None
    execution_mode: Literal["native", "sandbox"] = "native"
    forge: ForgeBinding | None = None

    @model_validator(mode="after")
    def forge_is_sandboxed(self) -> Self:
        # A Forge artifact always runs as interpreted sandbox language v2.
        if self.forge is not None and self.execution_mode != "sandbox":
            raise ValueError("A Forge artifact must use sandbox execution")
        return self

    @property
    def component_count(self) -> int:
        return len(self.components) + len(self.spec.pipeline)

    def _config(self) -> dict[str, JsonValue]:
        # ``forge`` is omitted when None so every pre-Forge digest stays byte-identical.
        exclude = {"spec", "directory", "report"} | ({"forge"} if self.forge is None else set())
        return self.model_dump(mode="json", exclude=exclude)

    @property
    def artifact_digest(self) -> str:
        """Bind semantic specification, configuration and exact current code bytes."""
        return _digest(
            {
                "spec_hash": self.spec.spec_hash,
                "config": self._config(),
                "code_sha256": hashlib.sha256(
                    (self.directory / "harness.py").read_bytes()
                ).hexdigest(),
            }
        )

    @property
    def licensed(self) -> bool:
        """Apply authenticated floor, budget and component gates, in that order."""
        report = self.report
        if report is None:
            return False
        try:
            if not verify_report(
                report.signed_payload(), report.signature, seal=report.ed25519
            ):
                return False
            if report.spec_hash != self.spec.spec_hash:
                return False
            if report.artifact_digest != self.artifact_digest:
                return False
            floor = self.spec.floor
            if not report.passed or report.failures:
                return False
            if report.floor_kind != floor.kind or report.floor_limit != floor.limit:
                return False
            if floor.kind == FloorKind.HUMAN_EDIT_RATE:
                if report.floor_score > floor.limit:
                    return False
            elif report.floor_score < floor.limit:
                return False
            secondary = self.spec.secondary_floor
            if secondary is not None:
                if (
                    report.secondary_floor_kind != secondary.kind
                    or report.secondary_floor_limit != secondary.limit
                    or report.secondary_floor_score is None
                ):
                    return False
                if secondary.kind == FloorKind.HUMAN_EDIT_RATE:
                    if report.secondary_floor_score > secondary.limit:
                        return False
                elif report.secondary_floor_score < secondary.limit:
                    return False
            elif any(
                value is not None
                for value in (
                    report.secondary_floor_score,
                    report.secondary_floor_limit,
                    report.secondary_floor_kind,
                )
            ):
                return False
            counts = report.held_out_counts
            if counts is not None and not self._counts_agree(report, counts):
                return False
            budget = self.spec.budget
            if report.cost_usd > budget.cost_usd or report.compute_s > budget.compute_s:
                return False
            return report.component_count == self.component_count
        except (OSError, ValueError, TypeError):
            return False

    def _counts_agree(self, report: TestReport, counts: CaseCounts) -> bool:
        """Phase 14 checks on signed counts; reports without counts skip them."""
        spec = self.spec
        if spec.secondary_floor is None:
            if counts.cases != report.held_out_count:
                return False
        elif counts.cases > report.held_out_count:
            return False
        if counts.over_budget or counts.invalid > invalid_allowance(spec, counts.cases):
            return False
        if spec.floor.kind == FloorKind.HELD_OUT_TASKS and counts.correct is not None:
            if counts.cases == 0:
                return False
            return abs(counts.correct / counts.cases - report.floor_score) <= 1e-9
        return True

    def save(self) -> None:
        """Persist metadata. The caller must have already persisted harness.py."""
        # Fail before writing metadata if the code is absent or cannot be read.
        _ = self.artifact_digest
        self.directory.mkdir(parents=True, exist_ok=True)
        _write_json(self.directory / "spec.json", self.spec.model_dump(mode="json"))
        _write_json(self.directory / "config.json", self._config())
        report_path = self.directory / "report.json"
        if self.report is None:
            report_path.unlink(missing_ok=True)
        else:
            _write_json(report_path, self.report.model_dump(mode="json"))

    @classmethod
    def load(cls, directory: Path) -> Self:
        """Load metadata; untrusted or stale reports never restore a license."""
        spec = Spec.model_validate_json((directory / "spec.json").read_text(encoding="utf-8"))
        config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        if not isinstance(config, dict) or set(config) - {
            "version",
            "preset",
            "model",
            "components",
            "params",
            "execution_mode",
            "forge",
        }:
            raise ValueError("Invalid artifact configuration")
        report_path = directory / "report.json"
        report = (
            TestReport.model_validate_json(report_path.read_text(encoding="utf-8"))
            if report_path.exists()
            else None
        )
        artifact = cls(spec=spec, directory=directory, report=report, **config)
        # Recheck disk code and report integrity now, and again at every delivery.
        _ = artifact.licensed
        return artifact
