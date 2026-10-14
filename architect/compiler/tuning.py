"""Deterministic tuning-only search. Candidates never carry a verification report.

Phase 14 fair arms: a ``plain`` spec measures exactly one configuration (``_plain``),
and under ``input_limit_chars`` the ``context_window`` parameter stays at its default,
since the spec's limit replaces it (``_pin_context``).
"""

from __future__ import annotations

import ast
import json
from collections.abc import Sequence
from copy import deepcopy
from itertools import product
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

from pydantic import JsonValue

from architect.compiler.types import AdapterFactory, CandidateEvaluation, TuneResult
from architect.floors import FloorEvaluator
from architect.presets import Preset
from architect.runtime import Runtime
from architect.runtime.evaluation import evaluate_cases
from architect.spec import FloorKind, HarnessArtifact, Spec


class TuningError(ValueError):
    """Invalid search input or unsupported warm-start source."""


class SearchStrategy(Protocol):
    def configurations(
        self, preset: Preset, warm_start: HarnessArtifact | None = None
    ) -> Sequence[dict[str, JsonValue]]: ...


def _params_key(params: dict[str, JsonValue]) -> str:
    return json.dumps(params, sort_keys=True, separators=(",", ":"), allow_nan=False)


class GridSearch:
    """Enumerate the Cartesian product in preset parameter/value order."""

    def configurations(
        self, preset: Preset, warm_start: HarnessArtifact | None = None
    ) -> Sequence[dict[str, JsonValue]]:
        configurations: list[dict[str, JsonValue]] = []
        seen: set[str] = set()
        if warm_start is not None:
            params = preset.validate_params(warm_start.params)
            configurations.append(params)
            seen.add(_params_key(params))
        names = tuple(preset.tunable_params)
        for values in product(*(preset.tunable_params[name].values for name in names)):
            params = preset.validate_params(dict(zip(names, values, strict=True)))
            key = _params_key(params)
            if key not in seen:
                configurations.append(params)
                seen.add(key)
        return configurations


class SuccessiveHalving(GridSearch):
    """Rank on growing deterministic prefixes and keep the better half each round.

    Enumeration is shared with grid, but ``tune`` uses this strategy's sample
    schedule and elimination policy. Every surviving finalist gets a fresh full
    tuning evaluation. Small partitions without room to grow reduce to grid.
    """

    def sample_sizes(self, total: int, minimum: int, candidates: int) -> tuple[int, ...]:
        rounds = max(0, (candidates - 1).bit_length())
        size = min(total, max(minimum, total // (2**rounds)))
        sizes = [size]
        while size < total:
            size = min(total, size * 2)
            sizes.append(size)
        return tuple(sizes)


def _effective_components(
    preset: Preset, params: dict[str, JsonValue], components: tuple[str, ...]
) -> tuple[str, ...]:
    # These are precisely the optional checks gated by verifier_depth in presets.
    checks = {"self_check", "review", "analysis_check", "evidence_check"}
    if params.get("verifier_depth", 0) == 0:
        return tuple(
            component
            for component in components
            if component not in checks or component not in preset.removable_components
        )
    return components


# Optional components that make a model call of their own outside the checks above: the
# explorer's probe and the planner's search.
_OWN_CALLS = frozenset({"probe", "search"})


def _plain(preset: Preset) -> tuple[dict[str, JsonValue], tuple[str, ...]]:
    """The plain arm's one configuration: the preset's defaults with ``verifier_depth`` and
    ``retries`` at 0 wherever they exist, without the optional probe or search. With the
    checks then dropped (``_effective_components``) every run makes one model call."""
    params = preset.validate_params(
        {name: 0 for name in ("verifier_depth", "retries") if name in preset.tunable_params}
    )
    components = tuple(
        component
        for component in preset.components
        if component not in _OWN_CALLS or component not in preset.removable_components
    )
    return params, components


def _pin_context(spec: Spec, preset: Preset, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """``context_window`` at its default when the spec's ``input_limit_chars`` replaces it.

    Game and competition specs keep it: there it sizes the play memory, and the input
    limit does not apply to them.
    """
    bounds = preset.tunable_params.get("context_window")
    if (
        spec.input_limit_chars is None
        or bounds is None
        or spec.execution_config.get("kind") in ("game", "competition")
    ):
        return params
    return {**params, "context_window": deepcopy(bounds.default)}


def _source_tree(source: str) -> str:
    """JSON persistence may reorder map keys without changing a preset wrapper."""

    class LiteralMaps(ast.NodeTransformer):
        def visit_Dict(self, node: ast.Dict) -> ast.AST:
            self.generic_visit(node)
            try:
                ast.literal_eval(node)
            except (ValueError, TypeError, RecursionError):
                return node
            items: list[tuple[str, ast.expr, ast.expr]] = []
            for key, value in zip(node.keys, node.values, strict=True):
                if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                    return node
                items.append((key.value, key, value))
            items.sort(key=lambda item: item[0])
            node.keys = [item[1] for item in items]
            node.values = [item[2] for item in items]
            return node

    return ast.dump(LiteralMaps().visit(ast.parse(source)))


def _validate_warm(spec: Spec, preset: Preset, model: str, warm: HarnessArtifact) -> None:
    if (
        warm.preset != preset.name
        or warm.model != model
        or warm.spec.input_schema != spec.input_schema
        or warm.spec.output_schema != spec.output_schema
        or set(warm.spec.tools) != set(spec.tools)
        or warm.spec.pipeline != spec.pipeline
    ):
        raise TuningError("Incompatible warm-start configuration")
    if warm.execution_mode == "sandbox":
        from architect.runtime.sandbox import validate_source

        validate_source((warm.directory / "harness.py").read_text(encoding="utf-8"), warm)
        preset.build(spec, warm.params, warm.components)
        return
    try:
        # Never import or execute warm code to check compatibility. Comparing ASTs
        # allows formatting changes but rejects any executable source extension.
        with TemporaryDirectory(prefix="architect-warm-") as temporary:
            directory = Path(temporary)
            preset.emit(directory, warm.spec, warm.params, warm.components)
            expected = _source_tree((directory / "harness.py").read_text(encoding="utf-8"))
        actual = _source_tree((warm.directory / "harness.py").read_text(encoding="utf-8"))
    except (OSError, ValueError, SyntaxError) as exc:
        raise TuningError("Invalid warm-start source or configuration") from exc
    if actual != expected:
        raise TuningError("Rewritten warm-start source is unsupported in Phase 3")


def _allocate(root: Path, spec: Spec) -> tuple[Path, int]:
    parent = root / spec.spec_hash
    parent.mkdir(parents=True, exist_ok=True)
    # Include files and reserved/incomplete directories: no name is ever reused.
    version = max((int(path.name) for path in parent.iterdir() if path.name.isdecimal()), default=0)
    while True:
        version += 1
        directory = parent / str(version)
        try:
            directory.mkdir()
        except FileExistsError:
            continue
        return directory, version


def _persist(
    root: Path,
    spec: Spec,
    preset: Preset,
    model: str,
    params: dict[str, JsonValue],
    components: tuple[str, ...],
) -> HarnessArtifact:
    params = preset.validate_params(params)
    components = _effective_components(preset, params, components)
    preset.build(spec, params, components)  # Reject bad inputs before reserving a version.
    directory, version = _allocate(root, spec)
    preset.emit(directory, spec, params, components)
    artifact = HarnessArtifact(
        spec=spec.model_copy(deep=True),
        version=version,
        preset=preset.name,
        model=model,
        components=components,
        params=params,
        directory=directory,
    )
    artifact.save()
    return artifact


def _rank(candidate: CandidateEvaluation) -> tuple[float | str, ...]:
    measurement, artifact = candidate.measurement, candidate.artifact
    stable = _params_key(artifact.params)
    if measurement.floor_passed and measurement.budget_passed:
        return (
            0,
            measurement.mean_cost_usd,
            artifact.component_count,
            measurement.max_tokens,
            stable,
            artifact.version,
        )
    # Failure candidates remain useful to rewriting/reporting. Respect the same
    # gate order: quality, budget, then simplicity. Score breaks remaining ties.
    quality = (
        measurement.score
        if artifact.spec.floor.kind == FloorKind.HUMAN_EDIT_RATE
        else -measurement.score
    )
    return (
        1,
        float(not measurement.floor_passed),
        float(not measurement.budget_passed),
        artifact.component_count,
        quality,
        measurement.mean_cost_usd,
        measurement.max_tokens,
        stable,
        artifact.version,
    )


async def tune(
    spec: Spec,
    preset: Preset,
    model: str,
    floor: FloorEvaluator,
    adapter_factory: AdapterFactory,
    runtime: Runtime,
    artifact_root: Path,
    *,
    strategy: SearchStrategy | None = None,
    warm_start: HarnessArtifact | None = None,
    max_candidates: int = 16,
) -> TuneResult:
    """Search parameters, persisting new unlicensed artifacts and measured evidence.

    ``candidates`` contains the latest evidence for every evaluated configuration,
    including partial-prefix measurements for eliminated halving candidates.
    Only full-partition finalists are eligible for ``best``.

    A ``plain`` spec measures only its one configuration (``_plain``) on every tuning
    case, whatever ``max_candidates`` and the strategy say, and ignores ``warm_start``.
    """
    if (
        isinstance(max_candidates, bool)
        or not isinstance(max_candidates, int)
        or max_candidates < 1
    ):
        raise TuningError("max_candidates must be a positive integer")
    if floor.definition != spec.floor:
        raise TuningError("Floor definition does not match specification")
    if not model.strip():
        raise TuningError("Model must be nonempty")
    if spec.plain:
        params, components = _plain(preset)
        artifact = _persist(artifact_root, spec, preset, model, params, components)
        measurement = await evaluate_cases(artifact, floor, floor.tuning, adapter_factory, runtime)
        candidate = CandidateEvaluation(artifact=artifact, measurement=measurement)
        return TuneResult(best=candidate, candidates=(candidate,), warm_started=False)
    if warm_start is not None:
        _validate_warm(spec, preset, model, warm_start)
    if warm_start is not None and warm_start.execution_mode == "sandbox":
        from architect.runtime.sandbox import rebind_source, validate_source

        directory, version = _allocate(artifact_root, spec)
        artifact = warm_start.model_copy(
            update={
                "spec": spec.model_copy(deep=True),
                "version": version,
                "directory": directory,
                "report": None,
            }
        )
        source = rebind_source(
            (warm_start.directory / "harness.py").read_text(encoding="utf-8"),
            warm_start,
            spec,
        )
        validate_source(source, artifact)
        (directory / "harness.py").write_text(source, encoding="utf-8")
        artifact.save()
        measurement = await evaluate_cases(artifact, floor, floor.tuning, adapter_factory, runtime)
        candidate = CandidateEvaluation(artifact=artifact, measurement=measurement)
        return TuneResult(best=candidate, candidates=(candidate,), warm_started=True)
    search = GridSearch() if strategy is None else strategy
    configurations = search.configurations(preset, warm_start)
    # Even custom strategies cannot displace the first, rebound warm candidate.
    params_list = (
        []
        if warm_start is None
        else [_pin_context(spec, preset, preset.validate_params(warm_start.params))]
    )
    seen = {_params_key(params) for params in params_list}
    for configuration in configurations:
        if len(params_list) >= max_candidates:
            break
        params = _pin_context(spec, preset, preset.validate_params(configuration))
        key = _params_key(params)
        if key not in seen:
            params_list.append(params)
            seen.add(key)
    if not params_list:
        return TuneResult(best=None, candidates=(), warm_started=False)
    artifacts = [
        _persist(
            artifact_root,
            spec,
            preset,
            model,
            params,
            warm_start.components if index == 0 and warm_start is not None else preset.components,
        )
        for index, params in enumerate(params_list)
    ]
    cases = floor.tuning
    minimum = (
        floor.definition.minimum_samples if spec.floor.kind == FloorKind.HUMAN_EDIT_RATE else 1
    )
    sizes = (
        search.sample_sizes(len(cases), minimum, len(artifacts))
        if isinstance(search, SuccessiveHalving)
        else (len(cases),)
    )
    latest: dict[int, CandidateEvaluation] = {}
    active = artifacts
    for size in sizes:
        round_results = []
        for artifact in active:
            measurement = await evaluate_cases(
                artifact,
                floor,
                cases[:size],
                adapter_factory,
                runtime,
            )
            candidate = CandidateEvaluation(artifact=artifact, measurement=measurement)
            latest[artifact.version] = candidate
            round_results.append(candidate)
        if size < len(cases):
            ranked = sorted(round_results, key=_rank)
            active = [item.artifact for item in ranked[: max(1, (len(ranked) + 1) // 2)]]
    finalists = [latest[artifact.version] for artifact in active]
    return TuneResult(
        best=min(finalists, key=_rank),
        candidates=tuple(latest[artifact.version] for artifact in artifacts),
        warm_started=warm_start is not None,
    )
