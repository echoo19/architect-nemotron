"""Trusted floor measurements, without dataset access or licensing authority.

Callers supply an authorized partition. Candidate code sees only task copies;
outputs remain local and never enter reports, failures, or telemetry.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import BaseModel, ConfigDict, JsonValue

from architect.floors import EvaluationCase, FloorEvaluator, HeldOutAccessError
from architect.floors.base import FloorError, unit_score
from architect.models.fake import ScriptedAdapter
from architect.runtime import (
    BudgetExceeded,
    RunContext,
    RunResult,
    Runtime,
    load_harness,
    numeric_params,
)
from architect.runtime.evidence import current_evaluation, scoped_evaluation
from architect.spec import CaseCounts, FloorKind, HarnessArtifact, invalid_allowance
from architect.telemetry import TelemetryRow, categorical

if TYPE_CHECKING:
    from architect.compiler.types import AdapterFactory, Measurement


class _SchemaFailure(ValueError):
    """A schema or candidate value is invalid; its details stay private."""


class _InputFailure(_SchemaFailure):
    """The case's task failed the input schema: the dataset is wrong, not the candidate."""


class _OutputFailure(_SchemaFailure):
    """The candidate's output failed the output schema: an invalid run."""


# Phase 14 run classes (Section 4.1). "hard" is a failure before the harness ran (the
# adapter factory or its model check); "accounting" is a run with no telemetry row.
_RunClass = Literal["ok", "invalid", "over_budget", "input", "hard", "accounting"]


def _validator(schema: dict[str, JsonValue]) -> Draft202012Validator:
    # Never allow a schema to fetch network or filesystem references.
    def local_references(value: JsonValue) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"$ref", "$dynamicRef", "$recursiveRef"} and (
                    not isinstance(child, str) or not child.startswith("#")
                ):
                    raise _SchemaFailure("Nonlocal schema reference")
                local_references(child)
        elif isinstance(value, list):
            for child in value:
                local_references(child)

    try:
        local_references(schema)
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema, format_checker=FormatChecker())
    except Exception as exc:
        raise _SchemaFailure("Invalid schema") from exc


@dataclass
class _Usage:
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    wall_time_s: float = 0.0
    max_tokens: int = 0
    max_cost_usd: float = 0.0
    max_compute_s: float = 0.0

    def add(self, row: TelemetryRow | RunResult) -> None:
        self.tokens_in += row.tokens_in
        self.tokens_out += row.tokens_out
        self.cost_usd += row.cost_usd
        self.wall_time_s += row.wall_time_s
        self.max_tokens = max(self.max_tokens, row.tokens_in + row.tokens_out)
        self.max_cost_usd = max(self.max_cost_usd, row.cost_usd)
        self.max_compute_s = max(self.max_compute_s, row.wall_time_s)

    def denied(self, error: BudgetExceeded) -> None:
        # Required reservations explain rejection but are NOT consumed usage.
        cost = error.required_cost_usd
        if isinstance(cost, (int, float)) and math.isfinite(cost) and cost >= 0:
            self.max_cost_usd = max(self.max_cost_usd, cost)


async def evaluate_cases(
    artifact: HarnessArtifact,
    floor: FloorEvaluator,
    cases: Sequence[EvaluationCase],
    adapter_factory: AdapterFactory,
    runtime: Runtime,
    *,
    floor_role: Literal["primary", "secondary"] | None = None,
    on_case: Callable[[], None] | None = None,
) -> Measurement:
    """``on_case`` is called once after each case has run, with nothing: a caller can
    count progress, and no output or per-case result leaves the evaluation."""
    try:
        digest = artifact.artifact_digest
    except (OSError, ValueError):
        digest = ""  # The existing loader records the invalid candidate without executing it.
    with scoped_evaluation(digest):
        return await _evaluate_cases(
            artifact,
            floor,
            cases,
            adapter_factory,
            runtime,
            floor_role=floor_role,
            on_case=None if on_case is None else (lambda _outcome: on_case()),
        )


@dataclass
class _CaseOutcome:
    """One executed case, private to this module; codes and numbers only."""

    ran: bool = False
    output: JsonValue = None
    failure: str | None = None
    line: int | None = None
    calls: int = 0
    cost_usd: float = 0.0
    run_id: str | None = None


_Scorer = Callable[[tuple[EvaluationCase, ...], list[JsonValue]], Awaitable[float]]


async def _evaluate_cases(
    artifact: HarnessArtifact,
    floor: FloorEvaluator,
    cases: Sequence[EvaluationCase],
    adapter_factory: AdapterFactory,
    runtime: Runtime,
    *,
    floor_role: Literal["primary", "secondary"] | None = None,
    on_case: Callable[[_CaseOutcome], None] | None = None,
    scorer: _Scorer | None = None,
) -> Measurement:
    """Execute all selected cases and score only a complete successful partition.

    Runtime rows account actual consumed usage. The aggregate quality row carries
    no token or cost usage, so summing telemetry never bills the same work twice.
    Failed cases are never dropped or replaced by fabricated candidate outputs.

    Under the Spec's ``count_as_miss`` policy (Phase 14, Section 4) an invalid run is
    a miss instead of a partition failure, up to ``invalid_allowance`` runs; the valid
    subset is scored once and the score is correct runs over every selected case.
    Budget refusals, input-schema failures and runs without a telemetry row stay hard.
    """
    # Loading this submodule must not import the compiler's heavyweight facade.
    from architect.compiler.types import Measurement

    selected = tuple(cases)
    usage = _Usage()
    failures: list[str] = []
    outputs: list[JsonValue] = []
    actual_score: float | None = None
    definition = floor.definition
    budget_passed = True
    invalid = over_budget = 0
    correct: int | None = None
    valid_cases: list[EvaluationCase] = []
    valid_outputs: list[JsonValue] = []

    def fail(code: str) -> None:
        if code not in failures:
            failures.append(code)

    role = floor_role or ("primary" if definition == artifact.spec.floor else "secondary")
    declared = artifact.spec.floor if role == "primary" else artifact.spec.secondary_floor
    if definition != declared:
        fail("floor_mismatch")
    if not selected:
        fail("no_cases")
    # The Spec validator allows count_as_miss only on one held_out_tasks floor.
    miss = (
        artifact.spec.invalid_policy == "count_as_miss"
        and role == "primary"
        and definition.kind == FloorKind.HELD_OUT_TASKS
    )

    if not failures:
        try:
            harness = load_harness(artifact.directory, artifact=artifact)
            input_validator = _validator(artifact.spec.input_schema)
            output_validator = _validator(artifact.spec.output_schema)
        except _SchemaFailure:
            fail("schema_error")
        except Exception:
            fail("load_error")
        else:

            async def checked(context: RunContext, task: JsonValue) -> JsonValue:
                try:
                    json.dumps(task, allow_nan=False)
                    input_validator.validate(task)
                except Exception as exc:
                    raise _InputFailure("Invalid input") from exc
                output = await harness(context, task)
                try:
                    json.dumps(output, allow_nan=False)
                    output_validator.validate(output)
                except Exception as exc:
                    raise _OutputFailure("Invalid output") from exc
                return output

            for case in selected:
                invoked = False
                prior_row = runtime.last_row
                run_id: str | None = None
                result: RunResult | None = None
                outcome = _CaseOutcome()
                kind: _RunClass = "hard"
                try:
                    adapter = adapter_factory(artifact.model)
                    if adapter.model != artifact.model:
                        raise ValueError("Candidate model mismatch")
                    task = deepcopy(case.task)
                    prior_row = runtime.last_row
                    invoked = True
                    result = await runtime.evaluate(
                        artifact.spec,
                        checked,
                        task,
                        adapter,
                        version=artifact.version,
                        preset=artifact.preset,
                        components=artifact.components,
                        params=artifact.params,
                        candidate_digest=(
                            scope.candidate_digest if (scope := current_evaluation()) else None
                        ),
                    )
                    run_id = result.run_id
                    outputs.append(deepcopy(result.output))
                    outcome.ran = True
                    kind = "ok"
                    if on_case is not None:
                        outcome.output = deepcopy(result.output)
                except BudgetExceeded as exc:
                    budget_passed = False
                    usage.denied(exc)
                    fail("budget_exceeded")
                    outcome.failure = "budget_exceeded"
                    kind = "over_budget"
                except _SchemaFailure as exc:
                    kind = "invalid" if isinstance(exc, _OutputFailure) else "input"
                    if kind == "input" or not miss:
                        fail("schema_error")
                    outcome.failure = "schema_error"
                except Exception as exc:
                    # Anything the harness raised is an invalid run; a failure before
                    # it ran (the adapter factory or its model check) stays hard.
                    kind = "invalid" if invoked else "hard"
                    if kind == "hard" or not miss:
                        fail("runtime_error")
                    outcome.failure = _failure_code(exc) or "runtime_error"
                    outcome.line = _failure_line(exc)
                finally:
                    # The runtime exposes the exact task-local emitted row,
                    # including failed runs, without scanning the journal. A
                    # factory failure must never reuse a preceding run's row.
                    if invoked:
                        row = runtime.last_row
                        if (
                            row is not None
                            and (prior_row is None or row.run_id != prior_row.run_id)
                            and (run_id is None or row.run_id == run_id)
                        ):
                            # The aggregate snapshot carries descendant usage and the
                            # row's wall time, which (unlike RunResult) includes cleanup.
                            aggregate = runtime.last_usage
                            spent: TelemetryRow | RunResult = (
                                aggregate
                                if aggregate is not None and aggregate.run_id == row.run_id
                                else result
                                if result is not None
                                else row
                            )
                            usage.add(spent)
                            outcome.cost_usd = spent.cost_usd
                            outcome.calls = row.model_calls
                            outcome.run_id = row.run_id
                        else:
                            fail("runtime_error")
                            outcome.failure = outcome.failure or "runtime_error"
                            if kind in ("ok", "invalid"):
                                kind = "accounting"
                    if on_case is not None:
                        on_case(outcome)
                if kind == "ok":
                    valid_cases.append(case)
                    valid_outputs.append(outputs[-1])
                elif kind == "invalid":
                    invalid += 1
                elif kind == "over_budget":
                    over_budget += 1

    if not failures and miss and invalid > invalid_allowance(artifact.spec, len(selected)):
        fail("invalid_fraction_exceeded")
    if not failures:
        try:
            if miss:
                # Score the valid subset once; the score is correct runs over every case.
                part = tuple(valid_cases)
                value = (
                    unit_score(
                        await (
                            scorer(part, valid_outputs)
                            if scorer
                            else floor.score(part, valid_outputs)
                        )
                    )
                    if part
                    else 0.0
                )
                correct = round(value * len(part))
                actual_score = correct / len(selected)
            else:
                actual_score = unit_score(
                    await (scorer(selected, outputs) if scorer else floor.score(selected, outputs))
                )
                if definition.kind == FloorKind.HELD_OUT_TASKS:
                    correct = round(actual_score * len(selected))
            if not floor.passes(actual_score):
                fail("floor_not_met")
        except HeldOutAccessError:
            if scorer is not None:
                raise  # measure_cases refuses non-tuning cases outright
            fail("floor_error")
        except BudgetExceeded as exc:
            budget_passed = False
            usage.denied(exc)
            fail("budget_exceeded")
        except Exception:
            fail("floor_error")
    if actual_score is None:
        correct = None

    budget = artifact.spec.budget
    if usage.max_cost_usd > budget.cost_usd or usage.max_compute_s > budget.compute_s:
        budget_passed = False
        fail("budget_exceeded")
    floor_passed = not failures and actual_score is not None
    counts = CaseCounts(
        cases=len(selected), correct=correct, invalid=invalid, over_budget=over_budget
    )
    measurement = Measurement(
        score=(
            actual_score
            if actual_score is not None
            else 1.0
            if definition.kind == FloorKind.HUMAN_EDIT_RATE
            else 0.0
        ),
        floor_passed=floor_passed,
        budget_passed=budget_passed,
        mean_cost_usd=usage.cost_usd / len(selected) if selected else 0.0,
        mean_tokens=(usage.tokens_in + usage.tokens_out) / len(selected) if selected else 0.0,
        max_tokens=usage.max_tokens,
        max_cost_usd=usage.max_cost_usd,
        max_compute_s=usage.max_compute_s,
        sample_count=len(selected),
        failures=tuple(failures),
        counts=counts,
    )
    await runtime.record(
        TelemetryRow(
            run_id=str(uuid4()),
            timestamp=datetime.now(UTC),
            domain=categorical(artifact.spec.domain),
            task_type=categorical("quality_evaluation"),
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
            wall_time_s=usage.wall_time_s,
            floor_type=definition.kind,
            floor_role=role,
            floor_score=actual_score,
            floor_limit=definition.limit,
            accepted=floor_passed and budget_passed,
        )
    )
    return measurement


# ---------------------------------------------------------------------------
# Phase 9 "Forge": tuning measurement with per-case detail, and $0 dry runs.
# Both resolve cases only as ``floor.tuning[i]``; neither touches held_out,
# _verification_cases or the verifier capability.
# ---------------------------------------------------------------------------

_CODE = re.compile(r"^(sandbox|subharness|game|official)_[a-z0-9_]{1,60}$")
_GAME_KINDS = frozenset({"game"})


def _failure_code(error: BaseException) -> str | None:
    """A fixed code carried by a sandbox or game failure; never exception text."""
    from architect.games.types import (
        GameActionError,
        GameProtocolError,
        GameSessionError,
        GameUnavailableError,
    )
    from architect.runtime.sandbox import SandboxError

    if not isinstance(
        error,
        (SandboxError, GameActionError, GameProtocolError, GameSessionError, GameUnavailableError),
    ):
        return None
    code = getattr(error, "code", None)
    if code is None and len(error.args) == 1:
        code = error.args[0]
    return code if isinstance(code, str) and _CODE.fullmatch(code) else None


def _failure_line(error: BaseException) -> int | None:
    line = getattr(error, "line", None)
    return line if type(line) is int and line >= 0 else None


def _excerpt(value: JsonValue, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, allow_nan=False)
    return text[:limit]


class GameDetail(BaseModel):
    """Host-computed process metrics of one game case (Section 6.4)."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    levels_completed: int
    actions: int
    distinct_states: int
    revisit_rate: float
    final_state: str
    calls_per_action: float
    # Last 24 (action, changed_cells, revisited), only if requested.
    trace: list[tuple[str, int, bool]] | None


class CaseDetail(BaseModel):
    """One measured tuning case: tuning evidence, never a license."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    index: int
    passed: bool | None
    score: float | None
    failure: str | None
    sandbox_line: int | None
    calls: int
    cost_usd: float
    output_excerpt: str | None
    expected_excerpt: str | None
    game: GameDetail | None


class DryRun(BaseModel):
    """A $0 scripted run of one tuning case; nothing is scored or spent."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    ok: bool
    code: str | None
    line: int | None
    # <= 8: {"prompt_head", "prompt_chars", "system_chars", "images", "tokens"}
    calls: list[dict[str, JsonValue]]
    output_head: str
    schema_ok: bool
    fuel_used: int
    steps: int


def _game_detail(runtime: Runtime, run_id: str | None, *, trace: bool) -> GameDetail | None:
    backend = runtime.scoped_game_backend()
    if backend is None or run_id is None or getattr(backend, "last_run_id", None) != run_id:
        return None
    counters = getattr(backend, "last_counters", None)
    if counters is None:
        return None
    actions = counters.actions
    return GameDetail(
        levels_completed=counters.levels_completed,
        actions=actions,
        distinct_states=counters.distinct_states,
        revisit_rate=counters.revisits / actions if actions else 0.0,
        final_state=str(getattr(backend, "last_final_state", None) or "NOT_PLAYED"),
        calls_per_action=counters.model_calls / max(actions, 1),
        trace=[(str(a), int(b), bool(c)) for a, b, c in counters.trace] if trace else None,
    )


async def measure_cases(
    artifact: HarnessArtifact,
    floor: FloorEvaluator,
    indexes: Sequence[int],
    adapter_factory: AdapterFactory,
    runtime: Runtime,
    *,
    excerpt_chars: int = 600,
    trace: bool = False,
    per_case: bool = True,
) -> tuple[Measurement, tuple[CaseDetail, ...]]:
    """Measure ``floor.tuning[i]`` for each index, with per-case detail.

    The aggregate ``Measurement`` is computed as ``evaluate_cases`` computes it
    and emits the same aggregate telemetry row. Per-case scores come from
    ``floor.score([case], [output])`` on tuning cases only; they are disabled
    (None) for floors whose per-score minimum exceeds 1 (``HUMAN_EDIT_RATE``)
    and with ``per_case=False`` (the ``aggregate`` feedback mode). Any case the
    floor does not recognise as tuning raises ``HeldOutAccessError``.
    """
    tuning = floor.tuning
    chosen = tuple(indexes)
    if (
        any(type(index) is not int or not 0 <= index < len(tuning) for index in chosen)
        or len(set(chosen)) != len(chosen)
        or type(excerpt_chars) is not int
        or excerpt_chars < 0
    ):
        raise ValueError("invalid_case")
    definition = floor.definition
    minimum = definition.minimum_samples if definition.kind == FloorKind.HUMAN_EDIT_RATE else 1
    available = minimum <= 1
    if not available and len(chosen) < minimum:
        raise ValueError("forge_measure_too_small")
    scored = available and per_case
    # Fresh copies: an issued verification object would lose its authority here.
    selected = tuple(tuning[index].model_copy(deep=True) for index in chosen)
    outcomes: list[_CaseOutcome] = []
    games: list[GameDetail | None] = []
    scores: dict[int, float | None] = {}
    failures: dict[int, str] = {}
    game = artifact.spec.execution_config.get("kind") in _GAME_KINDS

    def on_case(outcome: _CaseOutcome) -> None:
        # Counters belong to the play that just closed, so read them per case.
        outcomes.append(outcome)
        games.append(_game_detail(runtime, outcome.run_id, trace=trace) if game else None)

    # The first scoring denial. Once the scoring wallet refuses, nothing more is
    # scored; ``_evaluate_cases`` records ``budget_exceeded`` (and the denied
    # reservation) for an aggregate denial exactly as evaluate_cases does.
    denied: list[BudgetExceeded] = []
    # Receipts and judge calls are single-use, so these floors' aggregate is the
    # mean of the per-case scores: each case is scored once, in order, stopping
    # at the first failure exactly as the floors' own per-case loops stop.
    # Every other floor is aggregate-scored first, with the one call
    # evaluate_cases makes, so a stateful scorer (a wallet, a counter) is in the
    # same state and the aggregate cannot drift; per-case detail comes after.
    reused = definition.kind in (FloorKind.HELD_OUT_GAME_SCORE, FloorKind.LLM_JUDGE)
    # Set when a reused floor's aggregate pass ran: it already scored (or stopped
    # at) its cases, so the detail pass below must not score any of them again.
    per_case_aggregate: list[bool] = []

    async def score_one(position: int, output: JsonValue) -> float | None:
        value: float | None
        try:
            value = unit_score(await floor.score([selected[position]], [deepcopy(output)]))
        except HeldOutAccessError:
            raise
        except BudgetExceeded as exc:
            failures[position] = "budget_exceeded"
            denied.append(exc)
            value = None
        except Exception:
            failures[position] = "floor_error"
            value = None
        scores[position] = value
        return value

    async def aggregate(cases: tuple[EvaluationCase, ...], outputs: list[JsonValue]) -> float:
        if not (scored and reused):
            try:
                return await floor.score(cases, outputs)
            except BudgetExceeded as exc:
                denied.append(exc)
                raise
        per_case_aggregate.append(True)
        values: list[float] = []
        for position, output in enumerate(outputs):
            value = await score_one(position, output)
            if value is None:
                # The first failing case decides the aggregate, and nothing
                # after it is scored, exactly as the floor's own loop aborts.
                if denied:
                    raise denied[0]
                raise FloorError("Per-case scoring failed")
            values.append(value)
        return sum(values) / len(values)

    try:
        digest = artifact.artifact_digest
    except (OSError, ValueError):
        digest = ""
    with scoped_evaluation(digest):
        measurement = await _evaluate_cases(
            artifact,
            floor,
            selected,
            adapter_factory,
            runtime,
            floor_role=None,
            on_case=on_case,
            scorer=aggregate,
        )
        if scored:
            # Per-case detail only; the aggregate above is already final (a
            # partial run is never aggregate-scored). Past a denial nothing more
            # is scored; any other per-case failure is recorded and skipped.
            for position, outcome in enumerate(outcomes):
                if denied or per_case_aggregate:
                    break
                if outcome.ran and position not in scores:
                    await score_one(position, outcome.output)
    details: list[CaseDetail] = []
    for position, outcome in enumerate(outcomes):
        score = scores.get(position) if scored else None
        details.append(
            CaseDetail(
                index=chosen[position],
                passed=floor.passes(score) if score is not None else None,
                score=score,
                failure=outcome.failure or failures.get(position),
                sandbox_line=outcome.line,
                calls=outcome.calls,
                cost_usd=outcome.cost_usd,
                output_excerpt=(
                    _excerpt(outcome.output, excerpt_chars) if per_case and outcome.ran else None
                ),
                expected_excerpt=(
                    _excerpt(selected[position].expected, excerpt_chars) if per_case else None
                ),
                game=games[position],
            )
        )
    return measurement, tuple(details)


async def dry_run_case(
    artifact: HarnessArtifact,
    floor: FloorEvaluator,
    index: int,
    replies: Sequence[str],
    runtime: Runtime,
    *,
    game_steps: int = 5,
    prompt_head_chars: int = 300,
    output_head_chars: int = 500,
) -> DryRun:
    """Run ``floor.tuning[index]`` once through the real worker with scripted replies.

    Every model reply comes from ``ScriptedAdapter`` at $0; nothing is scored.
    Game specs play unscored (``open_turns(scored=False)``) and stop after
    ``game_steps`` steps. A gateway (online) game backend is refused.
    """
    tuning = floor.tuning
    if type(index) is not int or not 0 <= index < len(tuning):
        raise ValueError("invalid_case")
    if type(game_steps) is not int or game_steps < 1:
        raise ValueError("invalid_case")
    case_task = deepcopy(tuning[index].task)
    if artifact.spec.execution_config.get("kind") in _GAME_KINDS:
        backend = runtime.scoped_game_backend()
        game = case_task.get("game") if isinstance(case_task, dict) else None
        if (
            backend is None
            or not hasattr(backend, "open_turns")
            or not isinstance(game, dict)
            or game.get("backend") not in ("local", "official_offline")
        ):
            raise ValueError("forge_dryrun_unavailable")
    adapter = ScriptedAdapter(replies, model=artifact.model)
    contexts: list[RunContext] = []

    def result(ok: bool, code: str | None, line: int | None, output: JsonValue = None) -> DryRun:
        context = contexts[0] if contexts else None
        schema_ok = False
        if ok:
            try:
                json.dumps(output, allow_nan=False)
                _validator(artifact.spec.output_schema).validate(output)
                schema_ok = True
            except Exception:
                schema_ok = False
        play = context._play if context is not None else None
        return DryRun(
            ok=ok,
            code=code,
            line=line,
            calls=[
                {
                    "prompt_head": call.prompt[:prompt_head_chars],
                    "prompt_chars": len(call.prompt),
                    "system_chars": call.system_chars,
                    "images": call.images,
                    "tokens": call.tokens,
                }
                for call in adapter.requests[:8]
            ],
            output_head=_excerpt(output, output_head_chars) if ok else "",
            schema_ok=schema_ok,
            fuel_used=max(0, int(context.fuel_used)) if context is not None else 0,
            steps=play.counters.actions if play is not None else 0,
        )

    try:
        harness = load_harness(artifact.directory, artifact=artifact)
        input_validator = _validator(artifact.spec.input_schema)
    except _SchemaFailure:
        return result(False, "schema_error", None)
    except Exception as exc:
        return result(False, _failure_code(exc) or "load_error", _failure_line(exc))

    async def run(context: RunContext, task: JsonValue) -> JsonValue:
        contexts.append(context)
        try:
            json.dumps(task, allow_nan=False)
            input_validator.validate(task)
        except Exception as exc:
            raise _SchemaFailure("Invalid input") from exc
        try:
            return await harness(context, task)
        except Exception:
            dry = context._dry
            if dry is None or not dry.stopped or context._play is None:
                raise
        # The step budget stopped the harness: finish the play unscored.
        return await context.game_finish()

    with runtime._dry_turns(preset=artifact.preset, max_steps=game_steps):
        try:
            run_result = await runtime.evaluate(
                artifact.spec,
                run,
                case_task,
                adapter,
                version=artifact.version,
                preset="forge_dryrun",
                components=artifact.components,
                params=artifact.params,
            )
        except BudgetExceeded:
            return result(False, "budget_exceeded", None)
        except _SchemaFailure:
            return result(False, "schema_error", None)
        except Exception as exc:
            return result(False, _failure_code(exc) or "runtime_error", _failure_line(exc))
    return result(True, None, None, run_result.output)
