"""Deterministic preset flows. All external work uses RunContext.call.

Retrieval and memory use only explicit task JSON, never a service or persistent
user store. Probe/search calls propose data, not executable tools. A verdict here
is task output or a candidate check, never an artifact licensing decision.

Image literals in a task are lifted to content-free stubs (``images.lift_images``)
before any prompt is built; the images themselves travel beside the prompt on
every model call of the execution, including the optional self-check.

Phase 14 fair arms: a spec's ``input_limit_chars`` replaces the context allowance,
and its ``reply_json`` mode (``_json.parse_reply``) applies to every parse of one
execution, self-check and repairs included. Under ``count_as_miss`` a candidate that
never passes its self-check is still returned (the last schema-valid one), so the run
is scored instead of counted as invalid output.

Declared checks (``checks.py``): a spec that declares ``execution_config["checks"]`` has
them run in code on each output parsed from the model's reply, before the output schema
is checked, so the self-check and the answer see the checked value and a repaired reply
is checked again. Their printed text is read from the task before any model call. A spec
that declares none runs exactly as before.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from copy import deepcopy
from typing import cast

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError
from pydantic import JsonValue

from architect.runtime import RunContext
from architect.spec import ReplyJson, Spec

from . import PresetError
from ._json import parse_reply
from .checks import bind, check_declaration, declared
from .images import lift_images, stage_attaches

_VERDICT: dict[str, JsonValue] = {
    "type": "object",
    "required": ["passed", "reason"],
    "properties": {"passed": {"type": "boolean"}, "reason": {"type": "string"}},
    "additionalProperties": False,
}
_PLAN: dict[str, JsonValue] = {
    "type": "object",
    "required": ["steps"],
    "properties": {
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": 16,
            "items": {"type": ["string", "object"]},
        }
    },
}
_PROBE: dict[str, JsonValue] = {
    "type": "object",
    "required": ["observations", "actions"],
    "properties": {
        "observations": {"type": "array", "maxItems": 16, "items": {"type": "string"}},
        "actions": {"type": "array", "maxItems": 16, "items": {"type": "string"}},
    },
    "additionalProperties": False,
}
_SEARCH: dict[str, JsonValue] = {
    "type": "object",
    "required": ["candidates"],
    "properties": {"candidates": {"type": "array", "minItems": 1, "maxItems": 4, "items": _PLAN}},
    "additionalProperties": False,
}


def _dump(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _check_schema(schema: dict[str, JsonValue]) -> None:
    # Prevent network/file resolution: schemas may reference only their own root.
    def walk(value: JsonValue) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"$ref", "$dynamicRef", "$recursiveRef"} and (
                    not isinstance(child, str) or not child.startswith("#")
                ):
                    raise PresetError("Only local JSON Schema references are supported")
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise PresetError("Invalid JSON Schema") from exc


def check_spec(spec: Spec) -> None:
    _check_schema(spec.input_schema)
    _check_schema(spec.output_schema)
    # Declared checks run on one preset's reply and name only fields the spec has.
    check_declaration(spec)


def _validate(value: JsonValue, schema: dict[str, JsonValue]) -> None:
    try:
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(value)
    except ValidationError as exc:
        raise PresetError("Value does not satisfy the required output or input schema") from exc
    except Exception as exc:
        # Includes unresolved local references; do not fall back to accepting output.
        raise PresetError("JSON Schema validation could not be completed") from exc


def _parse(
    text: str,
    schema: dict[str, JsonValue],
    mode: ReplyJson = "strict",
    checks: Callable[[JsonValue], JsonValue] | None = None,
) -> JsonValue:
    # A string-typed reply is the text itself in every mode.
    value: JsonValue = text if schema.get("type") == "string" else parse_reply(text, mode)
    if checks is not None:
        value = checks(value)
    _validate(value, schema)
    return value


def _integer(params: dict[str, JsonValue], key: str, default: int) -> int:
    value = params.get(key, default)
    assert isinstance(value, int) and not isinstance(value, bool)
    return value


def _allowance(spec: Spec, params: dict[str, JsonValue]) -> int:
    """Characters of task context a preset keeps: the spec's ``input_limit_chars`` when
    set, else the ``context_window`` parameter, else 2,048."""
    if spec.input_limit_chars is not None:
        return spec.input_limit_chars
    return _integer(params, "context_window", 2048)


def _bounded(value: JsonValue, limit: int) -> JsonValue:
    """Keep context valid JSON, with a deterministic character allowance.

    This is a context payload policy, not a tokenizer. ModelAdapter.quote remains
    the authoritative budget reservation. Metadata and output schemas are kept.
    """
    if len(_dump(value)) <= limit:
        return deepcopy(value)
    if isinstance(value, str):
        return value[: max(0, limit - 2)]
    if isinstance(value, list):
        kept: list[JsonValue] = []
        for item in value:
            remaining = limit - len(_dump(kept)) - 2
            if remaining <= 0:
                break
            kept.append(_bounded(item, remaining))
        return kept
    if isinstance(value, dict):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            remaining = limit - len(_dump(result)) - len(_dump(key)) - 4
            if remaining <= 0:
                break
            result[key] = _bounded(item, remaining)
        return result
    return value


def pack_description(
    spec: Spec,
    params: dict[str, JsonValue],
    components: tuple[str, ...],
) -> dict[str, JsonValue]:
    """Describe the authenticated declarative pack for the upstream Pi session."""
    return {
        "backend": "pi",
        "tools": list(spec.tools),
        "context_policy": (
            {"max_characters": _allowance(spec, params)}
            if "context_policy" in components
            else {}
        ),
        "prompt_fragment": (
            "Make the requested change. Be concise."
            if params.get("terseness") == 1
            else "Make the requested repository change using granted tools and verify the result."
        ),
        "subagent_rules": (
            [
                "Request child agents only through the bounded compile_agent capability, "
                "when granted."
            ]
            if "subagent_rules" in components
            else []
        ),
    }


def _retrieve(task: dict[str, JsonValue]) -> list[JsonValue]:
    candidates = task.get("candidates", task.get("documents", []))
    if not isinstance(candidates, list):
        raise PresetError("Retrieval candidates/documents must be a JSON array")
    query = task.get("query", task.get("request", ""))
    terms = set(re.findall(r"\w+", _dump(query).lower()))
    # Stable ties retain caller order; no hidden external retrieval or state.
    ranked = sorted(
        candidates, key=lambda item: -len(terms & set(re.findall(r"\w+", _dump(item).lower())))
    )
    return ranked[:5]


def _task_context(
    name: str,
    task: JsonValue,
    components: tuple[str, ...],
    limit: int,
) -> JsonValue:
    payload = deepcopy(task)
    if name == "recommender":
        if not isinstance(task, dict):
            raise PresetError("Recommender task must contain explicit JSON inputs")
        # Always remove raw sources first so removed components cannot leak them.
        payload = {
            key: deepcopy(value)
            for key, value in task.items()
            if key
            not in {
                "candidates",
                "documents",
                "preferences",
                "history",
                "preference_memory",
                "retrieved",
            }
        }
        if "preference_memory" in components:
            payload["preference_memory"] = {
                "preferences": deepcopy(task.get("preferences", {})),
                "history": deepcopy(task.get("history", [])),
            }
        if "retrieval" in components:
            payload["retrieved"] = _retrieve(task)
    apply_policy = name not in {"synthesizer", "coder"} or "context_policy" in components
    # Later pipeline stages must see complete evidence from every preceding stage.
    # This envelope is data, not authority, so retaining it grants no capability.
    pipeline_envelope = isinstance(task, dict) and set(task) == {"original", "previous", "steps"}
    if apply_policy and not pipeline_envelope:
        payload = _bounded(payload, limit)
    return payload


_INSTRUCTIONS = {
    "extractor": "Extract facts from the input. Do not invent missing values.",
    "synthesizer": "Synthesize the supplied data into clear prose. Preserve factual values.",
    "recommender": "Recommend from retrieved candidates using supplied preferences and history.",
    "coder": "Make the requested repository change using the granted Pi tools.",
    "explorer": "Analyze the supplied unknown interface using explicit observations only.",
    "planner": "Plan from the supplied rules using at most 16 steps. Follow the output schema.",
    "verifier": "Check the supplied claim/output against evidence. Follow the output schema.",
    "hypothesis_tester": "Test explicit hypotheses against public observations and predictions.",
}
_ATTACHED = " {n} image(s) are attached to this message; extract from them, not from the stub."


async def execute(
    name: str,
    params: dict[str, JsonValue],
    components: tuple[str, ...],
    spec: Spec,
    context: RunContext,
    task: JsonValue,
    *,
    attach_images: bool = True,
) -> JsonValue:
    """Run one preset; ``attach_images=False`` keeps stubs in the prompt but sends no image.

    Inside a pipeline stage the stage's attach policy also applies
    (``images.stage_attach``); either one withholding images wins.
    """
    check_spec(spec)
    _validate(task, spec.input_schema)
    if spec.execution_config.get("kind") in {"game", "competition"}:
        output = await context.play_game(task, preset=name, params=params, components=components)
        _validate(output, spec.output_schema)
        return output
    if spec.pipeline:
        from architect.runtime.pipeline import run_pipeline

        return await run_pipeline(
            context,
            spec,
            task,
            preset_name=name,
            params=params,
            components=components,
        )
    mode = spec.reply_json
    # The declared checks, bound to this task's printed text before any model call.
    checks = declared(spec.execution_config)
    checked = bind(checks, task) if checks else None
    if name == "coder":
        response = await context.code(task, pack_description(spec, params, components))
        return _parse(response, spec.output_schema, mode, checked)
    retries = _integer(params, "retries", 0)
    # Base64 never reaches the prompt, the context policy, the quote or a pipe.
    task, lifted = lift_images(task)
    images = lifted if attach_images and stage_attaches() else ()
    payload = _task_context(name, task, components, _allowance(spec, params))
    instruction = _INSTRUCTIONS[name]
    if _integer(params, "terseness", 0):
        instruction = "Be concise. " + instruction.split(".")[0] + "."
    if images:
        instruction += _ATTACHED.format(n=len(images))
    prompt_data: dict[str, JsonValue] = {
        "instruction": instruction,
        "task_description": spec.task,
        "input": payload,
        "output_schema": spec.output_schema,
    }
    if images:
        prompt_data["attached_images"] = len(images)

    async def model(prompt: str, schema: dict[str, JsonValue]) -> str:
        # Text-only calls keep their exact pre-image shape. Only the ``schema`` reply mode
        # asks the provider for output constrained to the reply's schema (not for text).
        if mode == "schema" and schema.get("type") != "string":
            if images:
                return await context.call(
                    prompt, max_output_tokens=None, images=images, response_schema=schema
                )
            return await context.call(prompt, max_output_tokens=None, response_schema=schema)
        if images:
            return await context.call(prompt, max_output_tokens=None, images=images)
        return await context.call(prompt, max_output_tokens=None)
    # Only a bare object schema requests the archetype's default shape. Custom
    # required/additionalProperties/other constraints are authoritative on their own.
    generic_object = spec.output_schema == {"type": "object"}
    if name == "verifier" and generic_object:
        prompt_data["verdict_schema"] = _VERDICT
    if name == "planner" and generic_object:
        prompt_data["plan_schema"] = _PLAN

    async def ask(prompt: str, schema: dict[str, JsonValue]) -> JsonValue:
        for attempt in range(retries + 1):
            response = await model(prompt, schema)
            try:
                return _parse(response, schema, mode)
            except PresetError:
                if attempt == retries:
                    raise
                prompt += "\nRepair the invalid response. Return only a value matching the schema."
        raise AssertionError("unreachable")

    if name == "hypothesis_tester":
        hypothesis_schema: dict[str, JsonValue] = {
            "type": "object",
            "required": ["hypothesis", "prediction", "test"],
            "properties": {
                key: {"type": "string", "minLength": 1, "maxLength": 2048}
                for key in ("hypothesis", "prediction", "test")
            },
            "additionalProperties": False,
        }
        prompt_data["hypothesis_test"] = await ask(
            _dump(
                {
                    "operation": "form_testable_hypothesis",
                    "input": payload,
                    "output_schema": hypothesis_schema,
                }
            ),
            hypothesis_schema,
        )
        prompt_data["observed_feedback"] = payload
    if name == "explorer" and "probe" in components:
        probe = await ask(
            _dump({"operation": "probe_analysis", "input": payload, "output_schema": _PROBE}),
            _PROBE,
        )
        prompt_data["probe_analysis"] = probe
    if name == "planner" and "search" in components:
        search = await ask(
            _dump({"operation": "bounded_plan_search", "input": payload, "output_schema": _SEARCH}),
            _SEARCH,
        )
        prompt_data["search_candidates"] = search

    review_components = {"self_check", "review", "analysis_check", "evidence_check"}
    review = bool(review_components & set(components)) and _integer(params, "verifier_depth", 0) > 0
    # Under count_as_miss the self-check is advisory: when no attempt passes it, the run
    # answers with its last schema-valid candidate, scored like any answer (a miss when
    # wrong), instead of raising into the invalid-output ceiling. fail_partition keeps
    # the check as a refusal.
    keep_last = spec.invalid_policy == "count_as_miss"
    last: tuple[JsonValue] | None = None
    prompt = _dump(prompt_data)
    for attempt in range(retries + 1):
        # Retry the complete candidate on invalid output or an explicit failed check.
        response = await model(prompt, spec.output_schema)
        try:
            output = _parse(response, spec.output_schema, mode, checked)
            if name == "planner" and generic_object:
                _validate(output, _PLAN)
            if name == "verifier" and generic_object:
                _validate(output, _VERDICT)
            if review:
                last = (output,)
                verdict = await ask(
                    _dump(
                        {
                            "operation": "verify_candidate",
                            "task_description": spec.task,
                            "input": payload,
                            "candidate": output,
                            "output_schema": _VERDICT,
                        }
                    ),
                    _VERDICT,
                )
                if not cast(dict[str, JsonValue], verdict)["passed"]:
                    raise PresetError("Candidate did not pass its optional self-check")
            return output
        except PresetError:
            if attempt == retries:
                if keep_last and last is not None:
                    return last[0]
                raise
            prompt += "\nRepair the candidate: satisfy the output schema and evidence checks."
    raise AssertionError("unreachable")
