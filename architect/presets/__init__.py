"""Eight budgeted harness archetypes and deterministic on-disk emission.

These are candidate harnesses, not licensed agents. Only runtime verification can
license an artifact. Declared pipelines dispatch through the shared runtime.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, JsonValue, model_validator

from architect.runtime import Harness, RunContext
from architect.spec import Spec

__all__ = [
    "PRESET_DOCSTRING",
    "ParameterRange",
    "Preset",
    "PresetError",
    "UnknownPresetError",
    "all_presets",
    "execute_preset",
    "get_preset",
]


# The docstring of every emitted preset harness. It is part of harness.py's bytes and so of
# the artifact digest: changing it changes every preset artifact's identity.
PRESET_DOCSTRING = "Generated candidate harness. Execution does not confer a license."


class PresetError(ValueError):
    """Invalid configuration, task, model output, or unsupported pipeline."""


class UnknownPresetError(PresetError):
    """The named preset is not in the frozen launch set."""


def _key(value: JsonValue) -> str:
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":"))


class ParameterRange(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    values: tuple[JsonValue, ...]
    default: JsonValue

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        keys = [_key(value) for value in self.values]
        if not keys or len(set(keys)) != len(keys):
            raise PresetError("Parameter values must be nonempty and distinct")
        if _key(self.default) not in keys:
            raise PresetError("Parameter default must be in its range")
        return self


class Preset(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    name: str
    components: tuple[str, ...]
    tunable_params: dict[str, ParameterRange]
    removable_components: tuple[str, ...]

    @model_validator(mode="after")
    def validate_definition(self) -> Self:
        if (
            not self.name
            or not self.components
            or len(set(self.components)) != len(self.components)
        ):
            raise PresetError("Preset needs a name and distinct components")
        if not set(self.removable_components) <= set(self.components):
            raise PresetError("Removable components must exist")
        return self

    def validate_params(self, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if set(params) - self.tunable_params.keys():
            raise PresetError("Unknown tunable parameter")
        result: dict[str, JsonValue] = {}
        for name, bounds in self.tunable_params.items():
            value = params.get(name, bounds.default)
            try:
                valid = _key(value) in {_key(item) for item in bounds.values}
            except (TypeError, ValueError) as exc:
                raise PresetError("Invalid tunable value") from exc
            if not valid:
                raise PresetError("Tunable parameter is outside its range")
            result[name] = deepcopy(value)
        return result

    def _components(self, components: tuple[str, ...] | None) -> tuple[str, ...]:
        chosen = self.components if components is None else components
        if len(set(chosen)) != len(chosen) or not set(chosen) <= set(self.components):
            raise PresetError("Components must be a distinct subset of the preset")
        if not (set(self.components) - set(self.removable_components)) <= set(chosen):
            raise PresetError("Required components cannot be removed")
        return tuple(item for item in self.components if item in chosen)

    def build(
        self,
        spec: Spec,
        params: dict[str, JsonValue] | None = None,
        components: tuple[str, ...] | None = None,
    ) -> Harness:
        from ._execution import check_spec

        check_spec(spec)
        # Capture deep snapshots, not caller-owned mutable dictionaries.
        config = self.validate_params({} if params is None else params)
        selected = self._components(components)
        spec_json = spec.model_dump(mode="json")
        name = self.name

        async def run(context: RunContext, task: JsonValue) -> JsonValue:
            return await execute_preset(name, config, selected, spec_json, context, task)

        return run

    def emit(
        self,
        directory: Path,
        spec: Spec,
        params: dict[str, JsonValue] | None = None,
        components: tuple[str, ...] | None = None,
    ) -> None:
        # Validate through the same entry point as an in-memory harness.
        self.build(spec, params, components)
        config = self.validate_params({} if params is None else params)
        selected = self._components(components)
        spec_json = spec.model_dump(mode="json")
        payload = {"name": self.name, "params": config, "components": selected, "spec": spec_json}
        # repr produces Python literals safely, including quoted malicious input.
        # Never read preset.json during execution: it is an informational sidecar.
        source = (
            f'"""{PRESET_DOCSTRING}"""\n'
            "from pydantic import JsonValue\n"
            "from architect.presets import execute_preset\n"
            "from architect.runtime import RunContext\n\n"
            "async def run(context: RunContext, task: JsonValue) -> JsonValue:\n"
            f"    return await execute_preset({self.name!r}, {config!r}, {selected!r}, "
            f"{spec_json!r}, context, task)\n"
        )
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "harness.py").write_text(source, encoding="utf-8")
        if self.name == "coder":
            from ._execution import pack_description

            (directory / "pi-pack.json").write_text(
                json.dumps(pack_description(spec, config, selected), indent=2) + "\n",
                encoding="utf-8",
            )
        (directory / "preset.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
        )


def _range(default: int, *values: int) -> ParameterRange:
    return ParameterRange(values=tuple(values), default=default)


_RANGES = {
    "context_window": _range(2048, 512, 2048),
    "verifier_depth": _range(0, 0, 1),
    "retries": _range(0, 0, 1),
    "terseness": _range(0, 0, 1),
}


def _preset(
    name: str,
    required: str,
    optional: tuple[str, ...],
    tunables: tuple[str, ...],
) -> Preset:
    params = {key: _RANGES[key].model_copy(deep=True) for key in tunables}
    if name == "extractor":
        params["verifier_depth"] = _range(1, 0, 1)
    return Preset(
        name=name,
        components=(required, *optional),
        removable_components=optional,
        tunable_params=params,
    )


_PRESETS = (
    _preset(
        "extractor",
        "extraction",
        ("self_check",),
        ("verifier_depth", "retries"),
    ),
    _preset(
        "synthesizer",
        "synthesis",
        ("context_policy", "review"),
        ("context_window", "terseness", "verifier_depth"),
    ),
    _preset(
        "recommender",
        "recommendation",
        ("retrieval", "preference_memory", "review"),
        ("context_window", "verifier_depth"),
    ),
    _preset(
        "coder",
        "extension_pack",
        ("context_policy", "subagent_rules"),
        ("context_window", "terseness"),
    ),
    _preset(
        "explorer",
        "interface_analysis",
        ("probe", "analysis_check"),
        ("context_window", "verifier_depth", "retries"),
    ),
    _preset(
        "planner", "plan", ("search", "review"), ("context_window", "verifier_depth", "retries")
    ),
    _preset(
        "hypothesis_tester",
        "hypothesis",
        ("prediction", "feedback"),
        ("context_window", "verifier_depth", "retries"),
    ),
    _preset(
        "verifier",
        "verdict",
        ("evidence_check",),
        ("verifier_depth", "retries"),
    ),
)


def all_presets() -> tuple[Preset, ...]:
    """Return independent snapshots so compiler edits cannot mutate the registry."""
    return tuple(preset.model_copy(deep=True) for preset in _PRESETS)


def get_preset(name: str) -> Preset:
    for preset in _PRESETS:
        if preset.name == name:
            return preset.model_copy(deep=True)
    raise UnknownPresetError("Unknown preset: " + name)


async def execute_preset(
    name: str,
    params: dict[str, JsonValue],
    components: tuple[str, ...],
    spec_json: dict[str, JsonValue],
    context: RunContext,
    task: JsonValue,
) -> JsonValue:
    """Execute only through the run's budget guard; never create a license."""
    from ._execution import execute

    preset = get_preset(name)
    validated = preset.validate_params(params)
    selected = preset._components(components)
    return await execute(name, validated, selected, Spec.model_validate(spec_json), context, task)
