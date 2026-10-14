"""Trusted whole-field overrides do not change approval or proposal validation."""
from pathlib import Path

import pytest
from pydantic import JsonValue

from architect.compiler import CompileConfig, Compiler
from architect.compiler.inference import InferenceError
from architect.floors import HeldOutTasks
from architect.models import FakeAdapter, ModelResponse
from architect.runtime import Runtime
from architect.spec import Budget, FloorDefinition, FloorKind, Spec
from architect.telemetry import TelemetryStore


def proposal() -> Spec:
    return Spec(
        task="Perform a valid fixture task", input_schema={},
        output_schema={"type": "object", "required": ["injected"]},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, dataset="fixture", limit=1),
        budget=Budget(cost_usd=1, compute_s=5), lifecycle="install",
        execution_config={"kind": "game", "injected": True},
    )


def unavailable_floor(definition: FloorDefinition) -> HeldOutTasks:
    raise AssertionError("No tuning is allowed before installed approval")


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("exact", [False, True])
async def test_whole_field_overrides_are_explicit_and_never_approve(
    tmp_path: Path, explicit: bool, exact: bool,
) -> None:
    original = proposal()
    adapter = FakeAdapter(responses=[ModelResponse(
        text=original.model_dump_json(exclude={"provenance"}), model="fake",
        tokens_in=1, tokens_out=1, cost_usd=0.000002,
    )])
    store = TelemetryStore(tmp_path / "telemetry")
    compiler = Compiler(
        tmp_path / "artifacts", Runtime(store), lambda _: adapter, unavailable_floor,
        config=CompileConfig(max_candidates=1), inference_adapter=adapter,
    )
    overrides: dict[str, JsonValue] = {
        "output_schema": {"type": "object", "properties": {"safe": {"type": "string"}}},
        "execution_config": {"kind": "game", "host": True},
        "provenance": {"task": "user"},
    }
    try:
        result = await compiler.compile(
            original if explicit else "Perform a valid fixture task",
            overrides=overrides, exact_overrides=exact,
        )
        assert result.approval_required and result.artifact is None
        assert result.stages == (() if explicit else (0,))
        assert adapter.calls == (0 if explicit else 1)
        assert result.spec.provenance["execution_config"] == "user"
        assert result.spec.provenance.get("task") != "user"
        if exact:
            assert result.spec.output_schema == overrides["output_schema"]
            assert result.spec.execution_config == overrides["execution_config"]
        else:
            assert result.spec.output_schema["required"] == ["injected"]
            assert result.spec.execution_config["injected"] is True
        assert original.execution_config == {"kind": "game", "injected": True}
        assert overrides["provenance"] == {"task": "user"}
    finally:
        store.close()


async def test_exact_override_does_not_repair_invalid_model_proposal(tmp_path: Path) -> None:
    adapter = FakeAdapter(responses=[ModelResponse(
        text="not JSON", model="fake", tokens_in=1, tokens_out=1, cost_usd=0.000002,
    )])
    store = TelemetryStore(tmp_path / "telemetry")
    compiler = Compiler(
        tmp_path / "artifacts", Runtime(store), lambda _: adapter, unavailable_floor,
        inference_adapter=adapter,
    )
    try:
        with pytest.raises(InferenceError):
            await compiler.compile(
                "Perform a valid fixture task", exact_overrides=True,
                overrides=proposal().model_dump(mode="json", exclude={"provenance"}),
            )
        assert adapter.calls == 1
        assert not (tmp_path / "artifacts").exists()
    finally:
        store.close()


def _with_repeat(text: str, where: str) -> str:
    """Splice one repeated key into a serialised proposal, as small local models do."""
    if where == "top":
        return text[:-1] + ', "task": "Perform a valid fixture task"}'
    marker = f'"{where}":{{'
    index = text.index(marker) + len(marker)
    return text[:index] + '"type":"object",' + text[index:]


@pytest.mark.parametrize(
    ("where", "exact", "accepted"),
    [
        ("output_schema", True, True),  # pinned by the host, discarded unread
        ("input_schema", True, False),  # kept from the proposal, still checked
        ("top", True, False),  # a repeated top-level key is always ambiguous
        ("output_schema", False, False),  # deep-merge mode reads every field
    ],
)
async def test_repeated_keys_only_matter_in_fields_the_proposal_keeps(
    tmp_path: Path, where: str, exact: bool, accepted: bool
) -> None:
    text = proposal().model_dump_json(exclude={"provenance"})
    text = text.replace('"input_schema":{}', '"input_schema":{"type":"object"}')
    text = _with_repeat(text, where)
    adapter = FakeAdapter(responses=[ModelResponse(
        text=text, model="fake", tokens_in=1, tokens_out=1, cost_usd=0.000002,
    )])
    store = TelemetryStore(tmp_path / "telemetry")
    compiler = Compiler(
        tmp_path / "artifacts", Runtime(store), lambda _: adapter, unavailable_floor,
        config=CompileConfig(max_candidates=1), inference_adapter=adapter,
    )
    overrides: dict[str, JsonValue] = {
        "output_schema": {"type": "object", "properties": {"safe": {"type": "string"}}},
    }
    try:
        if accepted:
            result = await compiler.compile(
                "Perform a valid fixture task", overrides=overrides, exact_overrides=exact
            )
            assert result.spec.output_schema == overrides["output_schema"]
            assert result.approval_required and result.artifact is None
        else:
            with pytest.raises(InferenceError):
                await compiler.compile(
                    "Perform a valid fixture task", overrides=overrides, exact_overrides=exact
                )
    finally:
        store.close()


@pytest.mark.parametrize(
    ("edit", "accepted"),
    [
        # A repeated top-level key the host pins is discarded unread.
        (lambda text: text[:-1] + ', "output_schema": {"type": "object"}}', True),
        # A small model's invented top-level field is not a Spec field: dropped.
        (lambda text: text[:-1] + ', "task_details": {"notes": "x"}}', True),
        # A repeated top-level key the proposal keeps is still ambiguous.
        (lambda text: text[:-1] + ', "task": "Perform a valid fixture task"}', False),
    ],
)
async def test_exact_mode_reads_only_real_unpinned_fields(
    tmp_path: Path, edit: object, accepted: bool
) -> None:
    text = edit(proposal().model_dump_json(exclude={"provenance"}))  # type: ignore[operator]
    adapter = FakeAdapter(responses=[ModelResponse(
        text=text, model="fake", tokens_in=1, tokens_out=1, cost_usd=0.000002,
    )])
    store = TelemetryStore(tmp_path / "telemetry")
    compiler = Compiler(
        tmp_path / "artifacts", Runtime(store), lambda _: adapter, unavailable_floor,
        config=CompileConfig(max_candidates=1), inference_adapter=adapter,
    )
    overrides: dict[str, JsonValue] = {
        "output_schema": {"type": "object", "properties": {"safe": {"type": "string"}}},
    }
    try:
        if accepted:
            result = await compiler.compile(
                "Perform a valid fixture task", overrides=overrides, exact_overrides=True
            )
            assert result.spec.output_schema == overrides["output_schema"]
        else:
            with pytest.raises(InferenceError):
                await compiler.compile(
                    "Perform a valid fixture task", overrides=overrides, exact_overrides=True
                )
    finally:
        store.close()
