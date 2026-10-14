"""Stage 3: bounded, sandboxed edits tested only on tuning partitions.

Model output is data. A proposal is never imported or executed in the host.
Only measured full-partition improvements survive; verification remains Stage 4.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from architect.compiler.tuning import _allocate
from architect.compiler.types import AdapterFactory, CandidateEvaluation, Measurement
from architect.floors import EvaluationCase, FloorEvaluator
from architect.models import ModelAdapter
from architect.presets import Preset
from architect.runtime import RunContext, Runtime, numeric_params
from architect.runtime.evaluation import evaluate_cases
from architect.runtime.sandbox import validate_source
from architect.spec import Budget, FloorKind, HarnessArtifact
from architect.telemetry import TelemetryRow, categorical

_LIMIT = 64 * 1024
_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: [^\r\n]*)?\n?\Z")


class RewriteProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    diff: str = Field(min_length=1, max_length=_LIMIT, strict=True)
    components: tuple[str, ...]

    @field_validator("diff")
    @classmethod
    def bounded_diff(cls, value: str) -> str:
        if len(value.encode("utf-8")) > _LIMIT:
            raise ValueError("invalid_diff")
        _diff_lines(value)
        return value


class RewriteResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    best: CandidateEvaluation
    attempts: int = Field(ge=0)
    accepted: int = Field(ge=0)
    failures: tuple[str, ...]


def _diff_lines(diff: str) -> list[str]:
    """Require a single existing harness.py, without file operations or paths."""
    lines = diff.splitlines(keepends=True)
    if lines and lines[0] == "diff --git a/harness.py b/harness.py\n":
        lines = lines[1:]
        if lines and re.fullmatch(r"index [0-9a-f]+\.\.[0-9a-f]+(?: 100644)?\n", lines[0]):
            lines = lines[1:]
    if len(lines) < 3 or (lines[0], lines[1]) not in {
        ("--- harness.py\n", "+++ harness.py\n"),
        ("--- a/harness.py\n", "+++ b/harness.py\n"),
    }:
        raise ValueError("invalid_diff")
    if not _HUNK.fullmatch(lines[2]):
        raise ValueError("invalid_diff")
    return lines[2:]


def _apply_diff(source: str, diff: str) -> str:
    """Apply exact context, offsets and counts. Never invoke patch or a shell."""
    old = source.splitlines(keepends=True)
    patch = _diff_lines(diff)
    output: list[str] = []
    cursor = 0
    index = 0
    changed = False
    while index < len(patch):
        match = _HUNK.fullmatch(patch[index])
        if match is None:
            raise ValueError("invalid_diff")
        old_start = int(match[1])
        old_count = int(match[2]) if match[2] is not None else 1
        new_start = int(match[3])
        new_count = int(match[4]) if match[4] is not None else 1
        start = old_start - 1 if old_count else old_start
        target = new_start - 1 if new_count else new_start
        if start < cursor or start > len(old) or target != len(output) + start - cursor:
            raise ValueError("invalid_diff")
        output.extend(old[cursor:start])
        cursor = start
        index += 1
        removed = added = 0
        while index < len(patch) and not patch[index].startswith("@@"):
            line = patch[index]
            if not line or line[0] not in " +-":
                raise ValueError("invalid_diff")
            kind, text = line[0], line[1:]
            index += 1
            if index < len(patch) and patch[index] == "\\ No newline at end of file\n":
                if not text.endswith("\n"):
                    raise ValueError("invalid_diff")
                text = text[:-1]
                index += 1
            if kind in " -":
                if cursor >= len(old) or old[cursor] != text:
                    raise ValueError("invalid_diff")
                cursor += 1
                removed += 1
            if kind in " +":
                output.append(text)
                added += 1
            changed = changed or kind != " "
        if (removed, added) != (old_count, new_count):
            raise ValueError("invalid_diff")
    output.extend(old[cursor:])
    result = "".join(output)
    if not changed or result == source or len(result.encode("utf-8")) > _LIMIT:
        raise ValueError("invalid_diff")
    return result


def _read_source(artifact: HarnessArtifact) -> str:
    with (artifact.directory / "harness.py").open("rb") as stream:
        raw = stream.read(_LIMIT + 1)
    if len(raw) > _LIMIT:
        raise ValueError("invalid_source")
    # Match Python text-file semantics used by emitted source and model diffs.
    # Windows preset emission may use CRLF; persisted rewrites use canonical LF.
    return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("invalid_proposal")
        result[key] = value
    return result


def _proposal(text: JsonValue) -> RewriteProposal:
    if not isinstance(text, str) or len(text.encode("utf-8")) > 2 * _LIMIT:
        raise ValueError("invalid_proposal")
    json.loads(text, object_pairs_hook=_unique_object)
    return RewriteProposal.model_validate_json(text, strict=True)


def _cases(floor: FloorEvaluator) -> tuple[EvaluationCase, ...]:
    cases = floor.tuning
    minimum = (
        floor.definition.minimum_samples
        if floor.definition.kind == FloorKind.HUMAN_EDIT_RATE
        else 1
    )
    if len(cases) < minimum or len({case.case_id for case in cases}) != len(cases):
        raise ValueError("invalid_cases")
    # Round-trip validation rejects mutated nested task/metadata values.
    return tuple(EvaluationCase.model_validate_json(case.model_dump_json()) for case in cases)


def _passed(measurement: Measurement, count: int) -> bool:
    return (
        measurement.floor_passed
        and measurement.budget_passed
        and not measurement.failures
        and measurement.sample_count == count
    )


def _cheaper(current: Measurement, previous: Measurement) -> bool:
    if current.mean_cost_usd < previous.mean_cost_usd:
        return True
    return bool(
        current.mean_cost_usd == previous.mean_cost_usd == 0
        and current.mean_tokens is not None
        and previous.mean_tokens is not None
        and current.mean_tokens < previous.mean_tokens
    )


async def _event(
    runtime: Runtime,
    artifact: HarnessArtifact,
    accepted: bool,
    score: float | None,
) -> None:
    await runtime.record(
        TelemetryRow(
            run_id=str(uuid4()),
            timestamp=datetime.now(UTC),
            domain=categorical(artifact.spec.domain),
            task_type=categorical("rewrite"),
            spec_hash=artifact.spec.spec_hash,
            harness_version=artifact.version,
            lifecycle=artifact.spec.lifecycle,
            preset_origin=categorical(artifact.preset),
            component_set=tuple(categorical(item) for item in artifact.components),
            model=categorical(artifact.model),
            model_params=numeric_params(artifact.params),
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            wall_time_s=0.0,
            floor_type=artifact.spec.floor.kind,
            floor_score=score,
            floor_limit=artifact.spec.floor.limit,
            accepted=accepted,
        )
    )


async def rewrite(
    candidate: CandidateEvaluation,
    preset: Preset,
    floor: FloorEvaluator,
    smoke_floor: FloorEvaluator,
    adapter_factory: AdapterFactory,
    runtime: Runtime,
    artifact_root: Path,
    proposal_adapter: ModelAdapter,
    *,
    max_iterations: int = 3,
) -> RewriteResult:
    """Propose guarded diffs, retaining at most one complete successful repair.

    Baseline consumed cost may represent a failed partial execution; it remains
    a conservative lower bound. Denied reservations never become cost savings.
    Neither prompts nor persisted failure codes contain evaluation labels.
    """
    if type(max_iterations) is not int or max_iterations < 0:
        raise ValueError("invalid_iterations")
    best = candidate
    if max_iterations == 0:
        return RewriteResult(best=best, attempts=0, accepted=0, failures=())
    artifact = best.artifact
    if (
        floor is smoke_floor
        or floor.definition != artifact.spec.floor
        or smoke_floor.definition != artifact.spec.floor
    ):
        raise ValueError("floor_mismatch")
    if artifact.preset != preset.name:
        raise ValueError("preset_mismatch")
    cases, smoke_cases = _cases(floor), _cases(smoke_floor)
    source = _read_source(artifact)
    failures: list[str] = []
    for attempt in range(1, max_iterations + 1):
        accepted = False
        score: float | None = None
        outcome = artifact
        failure = "proposal_failed"
        try:
            prompt = json.dumps(
                {
                    "instruction": "Return ONLY JSON with diff and components. Use a unified diff "
                    "for existing harness.py only. Preserve required components. "
                    "Code must use the restricted sandbox language and guarded context.call. "
                    "Reduce measured cost while passing quality and budget gates.",
                    "source": source,
                    "config": artifact.model_dump(mode="json", exclude={"report", "directory"}),
                    "required_components": sorted(
                        set(preset.components) - set(preset.removable_components)
                    ),
                    "allowed_components": preset.components,
                    "measurement": best.measurement.model_dump(exclude={"failures"}),
                },
                allow_nan=False,
            )

            async def propose(context: RunContext, task: JsonValue) -> JsonValue:
                assert isinstance(task, str)
                return await context.call(task)

            proposal_spec = artifact.spec.model_copy(
                update={
                    "model_target": proposal_adapter.model,
                    "budget": Budget(cost_usd=1, compute_s=30),
                },
            )
            response = await runtime.evaluate(
                proposal_spec,
                propose,
                prompt,
                proposal_adapter,
                version=artifact.version,
                preset="rewrite_proposal",
            )
            failure = "invalid_proposal"
            proposal = _proposal(response.output)
            failure = "invalid_components"
            preset.build(artifact.spec, artifact.params, proposal.components)
            failure = "invalid_diff"
            edited = _apply_diff(source, proposal.diff)
            proposed = artifact.model_copy(
                update={
                    "components": proposal.components,
                    "report": None,
                    "execution_mode": "sandbox",
                },
                deep=True,
            )
            failure = "unsafe_source"
            validate_source(edited, proposed)
            failure = "persistence_failed"
            directory, version = _allocate(artifact_root, artifact.spec)
            proposed = proposed.model_copy(update={"directory": directory, "version": version})
            (directory / "harness.py").write_text(edited, encoding="utf-8", newline="\n")
            (directory / "rewrite.diff").write_text(proposal.diff, encoding="utf-8", newline="\n")
            proposed.save()
            outcome = proposed
            failure = "evaluation_failed"
            measured = await evaluate_cases(proposed, floor, cases, adapter_factory, runtime)
            # Failed quality is still an actual score; runtime/schema errors are not.
            if not set(measured.failures) - {"floor_not_met"}:
                score = measured.score
            smoke = await evaluate_cases(
                proposed,
                smoke_floor,
                smoke_cases,
                adapter_factory,
                runtime,
            )
            if not _passed(measured, len(cases)):
                failure = "tuning_failed"
            elif not _passed(smoke, len(smoke_cases)):
                failure = "smoke_failed"
            elif not _cheaper(measured, best.measurement):
                failure = "cost_not_lower"
            else:
                best = CandidateEvaluation(artifact=proposed, measurement=measured)
                accepted = True
        except asyncio.CancelledError:
            raise
        except Exception:
            # No provider, diff, source, or floor exception text crosses this boundary.
            pass
        finally:
            await _event(runtime, outcome, accepted, score)
        if accepted:
            return RewriteResult(best=best, attempts=attempt, accepted=1, failures=tuple(failures))
        failures.append(failure)
    return RewriteResult(best=best, attempts=max_iterations, accepted=0, failures=tuple(failures))
