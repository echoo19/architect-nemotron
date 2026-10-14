"""Play-time execution of a bound, licensed sub-harness (Phase 9 "Forge").

A Forge parent names each child in ``artifact.forge.subharnesses`` by digest.
This module is the only play-time execution path for a child: it reloads the
child from the parent's own ``subharnesses/<name>/`` directory, re-authenticates
it against the binding and its own signed report, validates the input, runs it
in a forked context (the child's model calls debit the parent's run and every
active wallet) and validates the output. Every refusal is a fixed
``SandboxError`` code and is fatal for the parent run.

It never reads floors, datasets or verification state.
"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy

from pydantic import JsonValue

from architect.runtime import BudgetExceeded, RunContext, load_harness
from architect.spec import Budget, HarnessArtifact, schema_sha256

__all__ = ["MAX_LINEAGE", "run_subharness"]

# A run context deeper than this may not start another child (structural bound).
MAX_LINEAGE = 4


def _error(code: str) -> Exception:
    from architect.runtime.sandbox import SandboxError

    return SandboxError(code)


def _load(parent: HarnessArtifact, name: str) -> HarnessArtifact:
    root = (parent.directory / "subharnesses").resolve()
    path = parent.directory / "subharnesses" / name
    if path.is_symlink() or path.resolve().parent != root:
        raise ValueError("Child path is not inside the parent directory")
    return HarnessArtifact.load(path)


async def run_subharness(
    context: RunContext, parent: HarnessArtifact, name: str, value: JsonValue
) -> JsonValue:
    """Run child ``name`` of ``parent`` with ``value``; fixed codes on every refusal."""
    from architect.runtime.evaluation import _SchemaFailure, _validator

    binding = parent.forge
    # 1. The name must be bound.
    if binding is None or not isinstance(name, str) or name not in binding.subharnesses:
        raise _error("subharness_unknown")
    ref = binding.subharnesses[name]
    # 2. Reload and re-authenticate the child against the binding and its report.
    try:
        child = await asyncio.to_thread(_load, parent, name)
        digest = child.artifact_digest
    except Exception:
        raise _error("subharness_digest_mismatch") from None
    if (
        digest != ref.artifact_digest
        or child.spec.spec_hash != ref.spec_hash
        or schema_sha256(child.spec.input_schema) != ref.input_schema_sha256
        or schema_sha256(child.spec.output_schema) != ref.output_schema_sha256
    ):
        raise _error("subharness_digest_mismatch")
    if not child.licensed or child.model != parent.model or context.adapter.model != child.model:
        raise _error("subharness_unlicensed")
    # 3. The input must satisfy the child's own input schema.
    try:
        input_validator = _validator(child.spec.input_schema)
        output_validator = _validator(child.spec.output_schema)
    except _SchemaFailure:
        raise _error("subharness_schema_error") from None
    try:
        json.dumps(value, allow_nan=False)
        input_validator.validate(value)
    except Exception:
        raise _error("sandbox_invalid_call") from None
    # 4. Nesting is bounded structurally and by this lineage check.
    if len(context.lineage) > MAX_LINEAGE:
        raise _error("subharness_depth")
    # 5. Run under min(child budget, parent remaining) in a forked context.
    context._check()
    cost = context.budget.cost_usd - context.cost_usd
    seconds = context.budget.compute_s - context.elapsed_s
    if cost <= 0 or seconds <= 0:
        raise BudgetExceeded("Run budget exceeded")
    budget = Budget(
        cost_usd=min(child.spec.budget.cost_usd, cost),
        compute_s=min(child.spec.budget.compute_s, seconds),
    )
    try:
        harness = load_harness(child.directory, artifact=child)
    except Exception:
        raise _error("subharness_digest_mismatch") from None
    if not child.licensed or child.artifact_digest != ref.artifact_digest:
        raise _error("subharness_digest_mismatch")
    child_context = context.fork(context.adapter, budget)
    # A child keeps its own model-call cap and never inherits a game grant or spec.
    child_context._cap_root = True
    child_context._game_scope = None
    child_context.spec = child.spec.model_copy(deep=True)
    try:
        child_context._check()
        output = await context._within_deadline(harness(child_context, deepcopy(value)))
        if child_context._calls:
            raise BudgetExceeded("Sub-harness left model calls unfinished")
    finally:
        try:
            await child_context._close()
        finally:
            context.absorb(child_context)
    context._check()
    # 6. The output must satisfy the child's own output schema.
    try:
        json.dumps(output, allow_nan=False)
        output_validator.validate(output)
    except Exception:
        raise _error("subharness_schema_error") from None
    return deepcopy(output)
