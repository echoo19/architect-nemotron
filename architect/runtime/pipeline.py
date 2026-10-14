"""Sequential, spec-bound model pipelines under step and shared run budgets.

Stages execute real preset harnesses. Their outputs are evidence for subsequent
stages, not execution authority. Only outer Stage 4 can license this composition.

Image literals are lifted once at entry: the images are attached to the run and
``original``, ``previous`` and ``steps`` carry content-free stubs only. A stage
sends the images with its model calls only when the spec's attach policy
(``presets.images.attach_stages``, default: the first stage) names it, so later
stages are never re-billed for pages they were not configured to see.
"""

from __future__ import annotations

from copy import deepcopy

from pydantic import JsonValue

from architect.models.images import ImageInput
from architect.presets import PresetError, get_preset
from architect.presets._execution import _check_schema, _validate
from architect.presets.images import attach_stages, lift_images, stage_attach
from architect.runtime import BudgetExceeded, Harness, RunContext
from architect.runtime.wallet import compilation_wallet
from architect.spec import Budget, Spec

__all__ = ["PipelineError", "run_pipeline"]


class PipelineError(ValueError):
    """A pipeline configuration, stage, or final verification is invalid."""


_VERDICT: dict[str, JsonValue] = {
    "type": "object",
    "required": ["passed", "reason"],
    "properties": {"passed": {"type": "boolean"}, "reason": {"type": "string"}},
    "additionalProperties": False,
}


def _stage(harness: Harness, *, attach: bool) -> Harness:
    """Run a stage harness with ``attach_images`` bound to this stage's policy."""

    async def run(context: RunContext, task: JsonValue) -> JsonValue:
        with stage_attach(attach):
            return await harness(context, task)

    return run


def _prepare(
    context: RunContext,
    spec: Spec,
    preset_name: str,
    params: dict[str, JsonValue],
    components: tuple[str, ...],
) -> tuple[list[Harness], bool]:
    """Reject invalid declared stages before spending on the first stage."""
    if not spec.pipeline:
        raise PipelineError("Pipeline must declare at least one stage")
    if len(spec.pipeline) > 16 or len({step.name for step in spec.pipeline}) != len(spec.pipeline):
        raise PipelineError("Pipeline stages must have distinct names and number at most 16")
    final = spec.pipeline[-1]
    gate = final.preset == "verifier" and not final.output_schema
    if gate and len(spec.pipeline) < 2:
        raise PipelineError("An implicit verifier requires a preceding payload stage")
    _check_schema(spec.input_schema)
    _check_schema(spec.output_schema)
    attach = attach_stages(spec)
    harnesses: list[Harness] = []
    primary_used = False
    for index, step in enumerate(spec.pipeline):
        if context.adapter_factory is None and step.model != context.adapter.model:
            raise PipelineError("Different pipeline models require an adapter factory")
        preset = get_preset(step.preset)
        output_schema = step.output_schema
        if index == len(spec.pipeline) - 1:
            output_schema = _VERDICT if gate else (output_schema or spec.output_schema)
        stage_spec = spec.model_copy(
            deep=True,
            update={
                "task": f"{spec.task}\nPipeline stage: {step.name} ({step.preset}).",
                "input_schema": deepcopy(step.input_schema),
                "output_schema": deepcopy(output_schema),
                "budget": step.budget,
                "model_target": step.model,
                "pipeline": (),
            },
        )
        primary = not primary_used and step.preset == preset_name
        harnesses.append(
            _stage(
                preset.build(
                    stage_spec,
                    params if primary else step.params,
                    components if primary else None,
                ),
                attach=step.name in attach,
            )
        )
        primary_used = primary_used or primary
    return harnesses, gate


async def run_pipeline(
    context: RunContext,
    spec: Spec,
    task: JsonValue,
    *,
    preset_name: str,
    params: dict[str, JsonValue],
    components: tuple[str, ...],
) -> JsonValue:
    """Run every declared stage and release only a schema-valid final payload.

    A local wallet reserves the parent's *remaining* run budget across children;
    any active compilation wallets stay in force. Child usage is absorbed even
    on schema failure, adapter failure, timeout, or cancellation. No step emits
    its own telemetry row or resets the aggregate budget. Tokens are measured,
    not capped; only dollars and elapsed time constrain this composition.
    """
    try:
        harnesses, gate = _prepare(context, spec, preset_name, params, components)
        _validate(task, spec.input_schema)
        # Lift once: stages, envelopes and prompts see stubs; the run holds the bytes.
        task, refs = lift_images(task)
        context.attach_images([ref for ref in refs if isinstance(ref, ImageInput)])
        context._check()
        cost = context.budget.cost_usd - context.cost_usd
        seconds = context.budget.compute_s - context.elapsed_s
        if cost <= 0 or seconds <= 0:
            raise BudgetExceeded("Pipeline has no remaining run budget")
        remaining = Budget(cost_usd=cost, compute_s=seconds)
        original = deepcopy(task)
        previous: JsonValue = None
        outputs: dict[str, JsonValue] = {}
        with compilation_wallet(remaining):
            for index, (step, harness) in enumerate(zip(spec.pipeline, harnesses, strict=True)):
                context._check()
                adapter = (
                    context.adapter_factory(step.model)
                    if context.adapter_factory is not None
                    else context.adapter
                )
                if adapter.model != step.model:
                    raise PipelineError("Pipeline adapter does not match the declared model")
                child = context.fork(adapter, step.budget)
                stage_input: JsonValue = (
                    deepcopy(original)
                    if index == 0
                    else {
                        "original": deepcopy(original),
                        "previous": deepcopy(previous),
                        "steps": deepcopy(outputs),
                    }
                )
                try:
                    child._check()
                    output = await child._within_deadline(harness(child, stage_input))
                    if child._calls:
                        raise BudgetExceeded("Pipeline stage left model calls unfinished")
                    child._check()
                finally:
                    try:
                        await child._close()
                    finally:
                        context.absorb(child)
                context._check()
                if gate and index == len(spec.pipeline) - 1:
                    _validate(output, _VERDICT)
                    if not isinstance(output, dict) or output.get("passed") is not True:
                        raise PipelineError("Final pipeline verifier rejected the payload")
                    # The verdict gates the preceding payload; it never replaces it.
                    _validate(previous, spec.output_schema)
                    return previous
                outputs[step.name] = deepcopy(output)
                previous = deepcopy(output)
        _validate(previous, spec.output_schema)
        return previous
    except PresetError as exc:
        # Do not put model content or schema-validation details in the public error.
        raise PipelineError("Pipeline stage configuration, input, or output is invalid") from exc
