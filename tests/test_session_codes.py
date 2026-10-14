"""The closed code table: every exception maps to a code and no text ever leaks."""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from architect.cli import CLIError
from architect.compiler.inference import InferenceError
from architect.models import AdapterError
from architect.profiles import ProfileError
from architect.registry import RegistryError
from architect.runtime import ArtifactExpiredError, BudgetExceeded, UnlicensedArtifactError
from architect.session import render
from architect.session.codes import (
    ADAPTER_MESSAGES,
    CODES,
    SessionError,
    code_for,
    explain,
    hint_for,
    is_code,
)
from architect.spec import Budget, FloorDefinition, FloorKind, Spec


def _validation_error() -> ValidationError:
    try:
        Spec.model_validate({"task": ""})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a ValidationError")


def _decode_error() -> json.JSONDecodeError:
    try:
        json.loads("{not json")
    except json.JSONDecodeError as exc:
        return exc
    raise AssertionError("expected a JSONDecodeError")


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (SessionError("no_draft"), "no_draft"),
        (SessionError("Not A Code"), "unknown_failure"),
        (CLIError("invalid_arguments"), "invalid_arguments"),
        (ProfileError("invalid_profile"), "invalid_profile"),
        (RegistryError("not_better"), "not_better"),
        (RegistryError("Registry exploded: /secret/path"), "unknown_failure"),
        (InferenceError("model said sk-secret"), "inference_failed"),
        (BudgetExceeded("over"), "budget_exceeded"),
        (_validation_error(), "invalid_spec"),
        (UnlicensedArtifactError("nope"), "unlicensed_artifact"),
        (ArtifactExpiredError("consumed"), "artifact_expired"),
        (AdapterError("Provider CLI is unavailable"), "provider_unavailable"),
        (AdapterError("Provider CLI timed out"), "provider_timeout"),
        (AdapterError("sk-secret-1234 leaked"), "provider_failed"),
        (asyncio.CancelledError(), "cancelled"),
        (KeyboardInterrupt(), "interrupted"),
        (_decode_error(), "invalid_input_json"),
        (OSError(13, "denied", "/private/file"), "storage_error"),
        (RuntimeError("anything else"), "command_failed"),
    ],
)
def test_code_for_table(exc: BaseException, code: str) -> None:
    assert code_for(exc) == code
    assert is_code(code_for(exc))


def test_every_adapter_message_maps_and_unknown_text_degrades() -> None:
    expected = {
        "Provider CLI is unavailable": "provider_unavailable",
        "Provider CLI timed out": "provider_timeout",
        "Provider request failed or returned invalid JSON": "provider_not_logged_in",
        "Provider reported an error": "provider_not_logged_in",
        "Provider usage limit reached": "provider_not_logged_in",
        "Provider reply ended inside its reasoning, before any answer": "provider_reasoning_cut",
        "Provider request failed (HTTP 429)": "provider_busy",
        "Provider request failed (HTTP 503)": "provider_busy",
        "Provider request failed (HTTP 529)": "provider_busy",
    }
    assert dict(ADAPTER_MESSAGES) == expected
    from architect.models.http import REASONING_CUT

    assert code_for(AdapterError(REASONING_CUT)) == "provider_reasoning_cut"
    for message, code in expected.items():
        assert code_for(AdapterError(message)) == code
        assert code in CODES
    assert code_for(AdapterError("Provider reported an error!")) == "provider_failed"
    assert code_for(AdapterError("")) == "provider_failed"
    # Only rate limits and overload are "busy"; any other status stays a plain failure.
    assert code_for(AdapterError("Provider request failed (HTTP 500)")) == "provider_failed"


def test_is_code_rejects_anything_that_could_carry_text() -> None:
    assert is_code("provider_failed")
    assert not is_code("Sk-secret")
    assert not is_code("sk-secret")
    assert not is_code("")
    assert not is_code("a" * 65)
    assert not is_code("code with space")
    assert not is_code("code\n")


def test_every_code_has_an_explanation_line() -> None:
    default = explain("definitely_not_a_code")
    assert default == "No explanation on file for this code."
    for code in CODES:
        line = explain(code)
        assert line != default, code
        assert "\n" not in line
        assert line.isascii()
        assert len(line) <= 200


@pytest.mark.parametrize(
    ("code", "text"),
    [
        ("not_a_command", "That is not a command in this state. Type help."),
        (
            "unknown_field",
            "Spec has no such field. provenance is computed by trusted code and tokens are "
            "usage, never a limit; there is nothing to set.",
        ),
        (
            "stale_approval_token",
            "The token does not match the Spec on screen; it changed since you last looked. "
            "Read it again and retype the token shown.",
        ),
        (
            "budget_below_quote",
            "The per-run budget is smaller than the smallest reservation the model can quote, "
            "so every run would be refused before it starts.",
        ),
        (
            "inference_failed",
            "The model did not produce a valid Spec, or no inference model is configured. "
            "Details are withheld by design.",
        ),
        (
            "floor_kind_unsupported_in_session",
            "The session helps only with held_out_tasks floors. human_edit_rate needs 60 real "
            "edit-log rows and llm_judge needs calibrations; configure them in the profile file.",
        ),
        (
            "provider_not_logged_in",
            "The CLI ran but did not answer as a logged-in client. "
            "Run it once by hand and sign in.",
        ),
    ],
)
def test_exact_explanations(code: str, text: str) -> None:
    assert explain(code) == text


def test_session_error_carries_code_and_field_names_only() -> None:
    exc = SessionError("invalid_edit", fields=("floor.limit", "budget.cost_usd"))
    assert str(exc) == "invalid_edit"
    assert exc.fields == ("floor.limit", "budget.cost_usd")
    assert SessionError("no_draft").fields == ()
    assert isinstance(exc, ValueError)


def test_render_failure_with_bad_code_prints_unknown_failure() -> None:
    text = render.failure("Sk-secret")
    assert text.startswith("error: unknown_failure\n")
    assert "Sk-secret" not in text
    assert render.failure("no_result") == (
        "error: no_result\n  Nothing has been compiled in this session yet.\n"
    )
    with_hint = render.failure("no_goal", "describe the task again, or use set")
    assert with_hint.endswith("\n  next: describe the task again, or use set\n")


def test_hints_name_the_next_command() -> None:
    spec = Spec(
        task="t",
        input_schema={},
        output_schema={},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.9, dataset="d"),
        budget=Budget(),
    )
    token = spec.spec_hash[:8]
    assert hint_for("stale_approval_token", spec) == f"approve {token}"
    assert hint_for("stale_approval_token", None) == "approve <hash8>"
    assert hint_for("primary_floor_failed", spec) == (
        "fix wrong expected values (floor), lower the limit (set floor.limit 0.8), "
        "or revise the task"
    )
    assert hint_for("compile_budget", None) == "budget compile 10 3600"
    assert hint_for("floor_unavailable", None) == "floor"
    assert hint_for("no_goal", None) == "describe the task again, or use set"
    assert hint_for("invalid_value_json", None) == ""
    for code in CODES:
        assert hint_for(code, spec).isascii()


def test_schema_missing_codes_explain_and_hint_the_full_shape() -> None:
    shape = '{"type":"object","properties":{...},"required":[...],"additionalProperties":false}'
    assert "input_schema_missing" in CODES and "output_schema_missing" in CODES
    assert explain("input_schema_missing") == (
        "The input_schema is empty or an object with no properties, so no input would ever "
        "be checked against a shape."
    )
    assert explain("output_schema_missing") == (
        "The output_schema is empty or an object with no properties, so Stage 4 output "
        "verification would accept anything."
    )
    assert hint_for("input_schema_missing", None) == f"set input_schema {shape}"
    assert hint_for("output_schema_missing", None) == f"set output_schema {shape}"
