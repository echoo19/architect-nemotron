"""The closed error-code table for the interactive session.

Every failure the session shows an operator is a short code from ``CODES`` (or a
code that an existing seam already fixed and that is rendered verbatim). Codes
exist so that provider text, exception messages, file contents and paths can
never reach the terminal: ``code_for`` maps any exception to a code and
``explain`` maps a code to one fixed ASCII sentence. Nothing here is built from
model output.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from types import MappingProxyType

from pydantic import ValidationError

from architect.cli import CLIError
from architect.compiler.inference import InferenceError
from architect.models import AdapterError
from architect.models.relay import CODES as RELAY_CODES
from architect.models.relay import TEXT as RELAY_TEXT
from architect.models.relay import UNREACHABLE, RelayRefusal, Unreachable
from architect.profiles import ProfileError
from architect.registry import RegistryError
from architect.runtime import ArtifactExpiredError, BudgetExceeded, UnlicensedArtifactError
from architect.spec import Spec

__all__ = [
    "ADAPTER_MESSAGES",
    "CODES",
    "SessionError",
    "code_for",
    "explain",
    "hint_for",
    "is_code",
]

_CODE_PATTERN = re.compile(r"[a-z_]{1,64}")


class SessionError(ValueError):
    """A session failure whose ``str()`` is a code in ``CODES``.

    ``fields`` optionally carries dotted Spec field paths (names only, never
    values) so that ``invalid_edit`` can say which field was rejected.
    """

    def __init__(self, code: str, *, fields: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.fields = fields


CODES: frozenset[str] = frozenset(
    {
        "not_a_command",
        "busy_inferring",
        "busy_compiling",
        "no_draft",
        "no_result",
        "no_goal",
        "no_licensed_artifact",
        "not_installable",
        "no_inference_model",
        "no_provider",
        "unknown_field",
        "invalid_edit",
        "invalid_value_json",
        "stale_approval_token",
        "approval_token_required",
        "install_token_required",
        "invalid_input_json",
        "invalid_spec",
        "invalid_spec_file",
        "invalid_profile",
        "profile_write_failed",
        "profile_not_editable",
        "provider_not_logged_in",
        "provider_unavailable",
        "provider_timeout",
        "provider_failed",
        "provider_reasoning_cut",
        "provider_busy",
        "probe_over_budget",
        "floor_not_configured",
        "secondary_floor_not_configured",
        "floor_kind_mismatch",
        "floor_kind_unsupported_in_session",
        "invalid_floor_file",
        "floor_too_small",
        "duplicate_case_id",
        "unknown_case_id",
        "unreviewed_cases",
        "draft_count_out_of_range",
        "model_not_configured",
        "input_schema_missing",
        "output_schema_missing",
        "budget_below_quote",
        "compile_deadline_short",
        "inference_failed",
        "cancelled",
        "interrupted",
        "unlicensed_artifact",
        "artifact_expired",
        "budget_exceeded",
        "storage_error",
        "command_failed",
        "unknown_failure",
        # Phase 8 bench: mirrors architect.bench.CODES, plus the busy state's code.
        "busy_benching",
        "bench_suite_unknown",
        "bench_tier_unknown",
        "bench_variant_unknown",
        "bench_profile_required",
        "bench_invalid_profile",
        "bench_model_unconfigured",
        "bench_no_models",
        "bench_route_invalid",
        "bench_over_max_usd",
        "bench_extra_missing",
        "bench_arc_unavailable",
        "bench_out_unwritable",
        "bench_rows_unreadable",
        "bench_compare_mismatch",
        "bench_compare_fingerprint",
        "bench_expectation_failed",
        "bench_cancelled",
        "bench_provider_failed",
        "bench_workspace_error",
        "bench_held_out_access",
        "bench_no_cells",
        "bench_model_not_local",
        "bench_resume_mismatch",
        "bench_resume_unsupported",
        "bench_task_unknown",
        "bench_split_unsupported",
        "bench_size_unsupported",
        "bench_delivery_all_unsupported",
        "bench_delivery_too_large",
        "bench_dataset_missing",
        "bench_dataset_mismatch",
        "bench_plain_variant",
        "bench_audit_unreadable",
        "audit_unwritable",
        "bench_run_budget_too_small",
        # Phase 9 Forge (Section 12.4): session and CLI codes.
        "forge_disabled",
        "forge_budget_invalid",
        "forge_model_unknown",
        "forge_cancelled",
        "forge_report_missing",
        "forge_verification_cap",
        "host_memory_low",
        # Phase 10 Section 13.4: routing, role, Forge-mode and export codes (the CLI
        # and the server print the same codes).
        "route_reuse_installed",
        "route_warm_start",
        "route_compile",
        "route_forge_suggested",
        "route_no_candidate",
        "route_unlicensed",
        "route_evicted",
        "route_consumed",
        "route_budget",
        "route_drift",
        "route_model_unconfigured",
        "route_synthetic_excluded",
        "route_reuse_not_granted",
        "route_no_reuse",
        "route_target_changed",
        "route_spec_required",
        "route_not_catalogued",
        "route_explore",
        "route_explore_cap",
        "route_promote_proposed",
        "route_rollback_suggested",
        "route_model_unknown",
        "route_param_invalid",
        "fit_slot",
        "fit_shape",
        "fit_family",
        "fit_none",
        "evidence_synthetic_only",
        "evidence_insufficient",
        "evidence_store_unavailable",
        "role_evidence_insufficient",
        "role_explore",
        "role_pinned",
        "forge_run_delivered",
        "forge_run_unlicensed",
        "forge_run_reused_installed",
        "forge_run_needs_expire",
        "forge_cases_too_few",
        "forge_cases_invalid",
        "forge_cases_floor_kind",
        "integrate_reused_installed",
        "integrate_not_ready",
        "export_cancelled",
        "export_target_exists",
        "export_path_invalid",
        "export_package_invalid",
        "export_unverified",
        "export_pin_mismatch",
        "export_integrity_mismatch",
        "export_subharness_missing",
        "export_failed",
        "export_architect_missing",
        "license_unverifiable",
        # W7 (docs/trust.md): Ed25519 signatures and the local trust list.
        "key_untrusted",
        "unsigned",
        "trust_list_invalid",
        "trust_list_missing",
        "trust_in_target",
        "published_unknown",
        "published_invalid",
        "verify_target_unknown",
        "key_exists",
        "key_missing",
        "key_invalid",
        "registry_subharness_missing",
        "rate_no_run",
        "approval_required",
        "feature_compile_failed",
        "approved_feature_must_be_installed",
    }
) | RELAY_CODES | {UNREACHABLE}  # the relay's refusals, an unreachable address (models.relay)

# Exact AdapterError messages from models/cli.py and models/http.py. The three
# "not logged in" messages are what a CLI produces when it runs but cannot answer
# as a signed-in client; any wording change degrades to provider_failed, never
# to text on the terminal.
ADAPTER_MESSAGES: Mapping[str, str] = MappingProxyType(
    {
        "Provider CLI is unavailable": "provider_unavailable",
        "Provider CLI timed out": "provider_timeout",
        "Provider request failed or returned invalid JSON": "provider_not_logged_in",
        "Provider reported an error": "provider_not_logged_in",
        "Provider usage limit reached": "provider_not_logged_in",
        "Provider reply ended inside its reasoning, before any answer": (
            "provider_reasoning_cut"
        ),
        # Rate limited or overloaded, named once retries (Nebius) are used up.
        "Provider request failed (HTTP 429)": "provider_busy",
        "Provider request failed (HTTP 503)": "provider_busy",
        "Provider request failed (HTTP 529)": "provider_busy",
    }
)

_EXPLANATIONS: Mapping[str, str] = MappingProxyType(
    {
        **RELAY_TEXT,
        "not_a_command": "That is not a command in this state. Type help.",
        "busy_inferring": "A Spec is being inferred; wait for it or type cancel.",
        "busy_compiling": "A compile is running; wait for it, type status, or type cancel.",
        "no_draft": "There is no Spec on screen yet. Describe the task or load a Spec first.",
        "no_result": "Nothing has been compiled in this session yet.",
        "no_goal": "This Spec came from a file or the registry, so there is no prose to revise.",
        "no_licensed_artifact": "Only Stage 4 can license an artifact; none exists for this Spec.",
        "not_installable": "Expire artifacts are single-use and cannot be installed.",
        "no_inference_model": (
            "No model in the profile can infer Specs under the fixed $1.00 Stage-0 budget; "
            "load an explicit Spec instead."
        ),
        "no_provider": "No profile and no logged-in CLI were found; nothing can run.",
        "unknown_field": (
            "Spec has no such field. provenance is computed by trusted code and tokens are "
            "usage, never a limit; there is nothing to set."
        ),
        "invalid_edit": "The edited Spec did not validate; the named fields were rejected.",
        "invalid_value_json": "A value is required; write JSON or plain text after the field.",
        "stale_approval_token": (
            "The token does not match the Spec on screen; it changed since you last looked. "
            "Read it again and retype the token shown."
        ),
        "approval_token_required": (
            "This Spec has lifecycle install; type approve <hash8> to bind your approval to it."
        ),
        "install_token_required": "Installation needs the artifact token: install <name> <hash8>.",
        "invalid_input_json": "The input is not valid JSON (NaN and Infinity are refused).",
        "invalid_spec": "The JSON is not a valid Spec.",
        "invalid_spec_file": "The file could not be read as a Spec.",
        "invalid_profile": "The profile file is missing, unreadable or invalid.",
        "profile_write_failed": "The profile file could not be written.",
        "profile_not_editable": (
            "Only a session-written JSON profile can be edited here; change hand-written "
            "profiles in the file."
        ),
        "provider_not_logged_in": (
            "The CLI ran but did not answer as a logged-in client. Run it once by hand and sign in."
        ),
        "provider_unavailable": "The provider CLI could not be started.",
        "provider_unreachable": (
            "Architect could not reach the model's address; check the connection and try again."
        ),
        "provider_timeout": "The provider CLI did not answer in time.",
        "provider_failed": "The provider call failed. Details are withheld by design.",
        "provider_reasoning_cut": (
            "The model spent its whole output allowance reasoning and gave no answer. Turn "
            "thinking off for it (think: false) or raise its output cap."
        ),
        "provider_busy": (
            "The provider refused the call as rate limited or overloaded (HTTP 429, 503 or "
            "529), after any retries. Wait a minute and try again, or check the key's limits."
        ),
        "probe_over_budget": "The login probe would reserve more than its $1.00 control budget.",
        "floor_not_configured": "The Spec names a floor dataset the profile does not configure.",
        "secondary_floor_not_configured": (
            "The Spec names a secondary floor dataset that the profile does not configure."
        ),
        "floor_kind_mismatch": "The configured floor has a different kind than the Spec says.",
        "floor_kind_unsupported_in_session": (
            "The session helps only with held_out_tasks floors. human_edit_rate needs 60 real "
            "edit-log rows and llm_judge needs calibrations; configure them in the profile file."
        ),
        "invalid_floor_file": "The floor file is not a JSON array of evaluation cases.",
        "floor_too_small": "The floor has fewer cases than its kind needs.",
        "duplicate_case_id": "A case with that id already exists in the draft.",
        "unknown_case_id": "No case in the draft has that id.",
        "unreviewed_cases": "Review every model-drafted case before accepting the floor.",
        "draft_count_out_of_range": "Draft between 2 and 64 cases at a time.",
        "model_not_configured": "The model is not in the profile.",
        "input_schema_missing": (
            "The input_schema is empty or an object with no properties, so no input would "
            "ever be checked against a shape."
        ),
        "output_schema_missing": (
            "The output_schema is empty or an object with no properties, so Stage 4 output "
            "verification would accept anything."
        ),
        "budget_below_quote": (
            "The per-run budget is smaller than the smallest reservation the model can quote, "
            "so every run would be refused before it starts."
        ),
        "compile_deadline_short": (
            "The compile deadline is less than twice the per-run compute budget; verification "
            "may run out of time."
        ),
        "inference_failed": (
            "The model did not produce a valid Spec, or no inference model is configured. "
            "Details are withheld by design."
        ),
        "cancelled": "The running work was cancelled.",
        "interrupted": "Interrupted at the prompt.",
        "unlicensed_artifact": "The artifact on disk carries no valid license from Stage 4.",
        "artifact_expired": "This single-use artifact has already consumed its attempt.",
        "budget_exceeded": "The call would exceed its budget and was refused before it started.",
        "storage_error": "A file could not be read or written.",
        "command_failed": "The command failed for a reason the session does not classify.",
        "unknown_failure": "A failure was reported without a recognised code.",
        # Pass-through codes from the compiler, verification and registry that the
        # result and install screens explain in place.
        "primary_floor_failed": "The held-out floor score is below the Spec's limit.",
        "secondary_floor_failed": "The secondary floor score is below its limit.",
        "primary_budget_failed": "A held-out run exceeded the per-run budget.",
        "secondary_budget_failed": "A secondary-floor run exceeded the per-run budget.",
        "primary_sample_count": "The held-out partition has fewer cases than minimum_samples.",
        "secondary_sample_count": "The secondary held-out partition is below minimum_samples.",
        "primary_evaluation_failed": "The held-out evaluation could not score every case.",
        "primary_evaluation_error": "The held-out evaluation raised an error.",
        # Phase 14 (docs/interfaces/phase14-nebius-evidence.md, Section 14): gate integrity.
        "invalid_fraction_exceeded": "More runs gave invalid output than the spec allows.",
        "primary_invalid_exceeded": "Too many held-out runs gave invalid output.",
        "forge_audit_failed": (
            "The Forge audit could not be written, so no Forge harness was kept."
        ),
        "audit_unwritable": "The audit directory cannot be created or written.",
        "compile_budget": "The whole-compile wallet ran out before verification finished.",
        "floor_unavailable": "The floor dataset could not be loaded for this Spec.",
        "empty_model_ladder": "The profile's compile.model_ladder is empty.",
        "selection_failed": "Stage 1 could not select a preset for the task.",
        "tuning_configuration_failed": "Stage 2 could not configure any candidate.",
        "no_candidates": "Stage 2 produced no candidate harness.",
        "tuning_failed": "No candidate passed the tuning partition.",
        "rewrite_failed": "Stage 3 could not produce a valid rewrite.",
        "rewrite_cap": "Stage 3 reached the rewrite cap.",
        "unverified": "The candidate reached Stage 4 without a report.",
        "compilation_failed": "Compilation failed without a more specific code.",
        "artifact_changed": "The artifact or package changed after it was verified or signed.",
        "artifact_unreadable": "The artifact directory could not be read.",
        "artifact_save_failed": "The verified artifact could not be saved.",
        "not_better": "The candidate is not better than the installed revision.",
        "budget_increase": "The candidate needs a higher per-run budget than the installed one.",
        "artifact_mismatch": "The artifact on disk does not match the one being installed.",
        "invalid_license": "The artifact's license did not verify.",
        "unknown_name": "No installed artifact has that name.",
        "unknown_revision": "That revision does not exist.",
        "invalid_name": "Names are short lowercase identifiers.",
        "invalid_revision": "Revisions are positive integers.",
        "workspace_path_too_long": "The workspace path is too long for the registry.",
        "revision_conflict": "Another install changed the registry first; retry.",
        "invalid_budget_permission": "The budget permission flag was not understood.",
        "missing_api_key": "The profile names an API key environment variable that is not set.",
        "adapter_unavailable": "A configured adapter could not be built.",
        "smoke_not_configured": "The floor has no smoke dataset.",
        "calibration_not_configured": "The llm_judge floor needs a calibration dataset.",
        "unsupported_game_environment": "Game floors need an explicit integration.",
        "invalid_floor_fixture": "A configured floor file could not be read.",
        "invalid_arguments": (
            "The command-line flags or Forge tool arguments were not understood."
        ),
        "busy_benching": "A bench is running; wait for it, type status, or type cancel.",
        "bench_suite_unknown": "There is no bench suite with that version.",
        "bench_tier_unknown": (
            "The bench tier is not in this suite: smoke, quick, full or arc in v1, public in v2."
        ),
        "bench_variant_unknown": (
            "The bench variant is unknown, or noisy was asked of a real provider (it is "
            "fake-only)."
        ),
        "bench_profile_required": "Paid bench tiers need a profile with the models to measure.",
        "bench_invalid_profile": "The bench profile is missing, unreadable or invalid.",
        "bench_model_unconfigured": (
            "A bench model is not configured in the profile, or its name is not a plain "
            "model id (letters, digits, '.', '_', ':' and '-')."
        ),
        "bench_no_models": "The bench has no models to run.",
        "bench_route_invalid": "A route must be vision=M, solver=M or verifier=M.",
        "bench_over_max_usd": (
            "The bench would reserve more than --max-usd before spending anything; nothing ran."
        ),
        "bench_extra_missing": "The bench needs the optional Pillow extra (uv sync --extra bench).",
        "bench_arc_unavailable": (
            "No arc cell could run: the ARC extra, the cached public games or a model the "
            "preview can drive is missing."
        ),
        "bench_out_unwritable": "The bench output directory could not be written.",
        "bench_rows_unreadable": "The bench rows could not be read; check the id or path.",
        "bench_compare_mismatch": (
            "The two runs are not comparable (suite, digest, sizes or pages differ)."
        ),
        "bench_compare_fingerprint": (
            "The presets changed between the runs; label the comparison to allow it."
        ),
        "bench_expectation_failed": "The bench scores differ from the expected identical scores.",
        "bench_cancelled": "The bench was cancelled; rows written so far are kept.",
        "bench_provider_failed": "A bench provider could not be built or failed.",
        "bench_workspace_error": "A bench workspace could not be created or removed.",
        "bench_held_out_access": (
            "The bench touched held-out cases outside Stage 4; the run was stopped."
        ),
        "bench_no_cells": (
            "The tier, presets and variants select no bench task; nothing ran."
        ),
        "bench_model_not_local": (
            "An Ollama bench model must run on this host (a loopback base_url) at $0; "
            "nothing ran."
        ),
        "bench_resume_mismatch": (
            "That bench cannot be resumed: its settings, suite, presets or profile changed, "
            "or it was started before resume existed. Nothing ran."
        ),
        "bench_resume_unsupported": (
            "A routed bench cannot be resumed; start it again. Nothing ran."
        ),
        "bench_task_unknown": "That task is not in this suite.",
        "bench_split_unsupported": "Only public tasks have floor and delivery splits.",
        "bench_size_unsupported": "A public task's size is fixed by its split.",
        "bench_delivery_all_unsupported": "--delivery all needs a public task.",
        "bench_delivery_too_large": "The delivery split has fewer cases than asked for.",
        "bench_dataset_missing": "The public dataset files are not in this checkout.",
        "bench_dataset_mismatch": "The public dataset does not match its manifest.",
        "bench_plain_variant": "--plain runs only the default and noisy variants.",
        "bench_audit_unreadable": "The audit directory or one of its records cannot be read.",
        "bench_run_budget_too_small": (
            "The pinned per-run budget cannot pay for one run's model calls; cap the "
            "model's max_tokens or raise the budget. Nothing ran."
        ),
        # Phase 9 Forge: session and CLI codes, then pass-through compile and bench codes.
        "forge_disabled": (
            "Forge cannot run here: it needs a floor with a smoke file, so it stays off."
        ),
        "forge_budget_invalid": (
            "The Forge budget must be above $0 and at most $1000, and above 0 and at most "
            "1440 minutes."
        ),
        "forge_model_unknown": "The Forge agent model is not a model key in the profile.",
        "forge_cancelled": "Forge was not confirmed, so nothing was compiled or spent.",
        "forge_report_missing": "That artifact directory has no forge.json.",
        "forge_verification_cap": (
            "This spec reached its Forge verification cap; Forge did not run again."
        ),
        "host_memory_low": "Free host memory is below 6 GB; the Forge game cell did not run.",
        "forge_no_submission": (
            "Forge ended without an accepted submission; the Stage 2 harness was verified."
        ),
        "forge_failed": "The Forge session failed; the Stage 2 harness was verified.",
        "forge_did_not_run": (
            "A forge or template bench row did not run Forge; it is not a Forge result."
        ),
        # Phase 9 Forge tools (Section 9 and 12.4): soft codes the agent reads and the
        # operator may see in forge.json, telemetry or a failure list.
        "forge_tool_cap": "A Forge tool reached its per-tool call cap for this session.",
        "tool_failed": "A Forge tool failed internally; the agent got a fixed code only.",
        "result_too_large": (
            "A Forge tool result was too large to return, so only this code was returned."
        ),
        "forge_file_too_large": "The Forge draft harness.py is larger than 64 KiB.",
        "invalid_case": "A Forge tool named a case index outside the tuning cases.",
        "forge_measure_too_small": (
            "This floor scores only in aggregate; the Forge measure needs more cases."
        ),
        "forge_dryrun_unavailable": (
            "Forge dry runs need a local or offline game backend; this one is online."
        ),
        "forge_invalid_source": "The Forge draft failed sandbox validation at a reported line.",
        "forge_literal_leak": (
            "The Forge draft copies long tuning text into string literals; submit refused it."
        ),
        "forge_unbound_child": "The Forge draft calls a sub-harness that is not licensed.",
        "forge_unused_child": "A sub-harness bound to the Forge draft is never called.",
        "smoke_failed": "The Forge candidate failed the smoke floor on tuning cases.",
        "persistence_failed": "The Forge candidate could not be written to the private store.",
        "forge_submissions_exhausted": "Forge used every submission attempt for this session.",
        "forge_depth": "Sub-harness spawning reached the Forge depth cap.",
        "forge_spawn_cap": "Forge reached its sub-harness spawn cap for this session.",
        "forge_spawn_name_taken": "A sub-harness with that name was already spawned this session.",
        "invalid_schema": (
            "A spawn schema is not valid JSON Schema (Draft 2020-12) or is over 16 KiB."
        ),
        "invalid_cases": "A spawn needs at least 8 valid cases that match both schemas.",
        "spawn_kind_unsupported": "Sub-harnesses cannot be spawned for game specs.",
        "compile_failed": "The sub-harness compile failed; no child was kept.",
        "not_licensed": "The sub-harness did not earn a licence, so it cannot be bound.",
        # Sandbox language (Section 4.10): validation codes, then runtime codes.
        "sandbox_source_limit": "The sandboxed harness source is larger than 64 KiB.",
        "sandbox_source_unreadable": "The sandboxed harness source is not readable UTF-8.",
        "sandbox_invalid_source": "The sandboxed harness source does not parse.",
        "sandbox_ast_limit": "The sandboxed harness has too many syntax nodes or nests too deep.",
        "sandbox_import": "The sandboxed harness has an import outside the fixed allowed set.",
        "sandbox_entrypoint": "The sandboxed harness must end with async def run(context, task).",
        "sandbox_signature": "The sandboxed harness run() signature is not the exact allowed one.",
        "sandbox_statement": "The sandboxed harness uses a statement its language does not allow.",
        "sandbox_expression": (
            "The sandboxed harness uses an expression its language does not allow."
        ),
        "sandbox_assignment": "The sandboxed harness assigns to a name it may not assign.",
        "sandbox_comparison": "The sandboxed harness uses a comparison its language forbids.",
        "sandbox_call": "The sandboxed harness calls something that is not an allowed builtin.",
        "sandbox_call_limit": "The sandboxed harness has more await sites than language 1 allows.",
        "sandbox_capability": "The sandboxed harness uses a capability it was not granted.",
        "sandbox_preset_call": "The sandboxed harness calls execute_preset with wrong arguments.",
        "sandbox_binding_mismatch": (
            "The sandboxed harness does not match the preset or sub-harness it is bound to."
        ),
        "sandbox_literal_required": "A sandbox argument must be a literal value.",
        "sandbox_invalid_json": "A sandbox value is not JSON.",
        "sandbox_recursion": "Sandbox helpers call themselves directly or through each other.",
        "sandbox_loop_depth": "Sandbox loops nest deeper than 6.",
        "sandbox_helper": "A sandbox helper breaks the helper rules (form, parameters or await).",
        "sandbox_constant": "A sandbox module constant breaks the constant rules.",
        "sandbox_language": (
            "The harness uses a sandbox language 2 construct but is bound to language 1."
        ),
        "sandbox_key": "The sandboxed harness read a missing dictionary key.",
        "sandbox_index": "The sandboxed harness used an index out of range.",
        "sandbox_type": "The sandboxed harness used a value of the wrong type.",
        "sandbox_value": "The sandboxed harness hit an invalid value (parse, division or range).",
        "sandbox_dictionary": "The sandboxed harness built a dictionary with a non-str key.",
        "sandbox_add": "The sandboxed harness added values of mismatched types.",
        "sandbox_length": "The sandboxed harness took len of a value with no length.",
        "sandbox_unary": "The sandboxed harness negated a value that is not a number.",
        "sandbox_unbound": "The sandboxed harness read a local name before assigning it.",
        "sandbox_fuel": "The sandboxed harness ran out of fuel; the run was stopped.",
        "sandbox_memory": "The sandbox worker ran out of memory; the run was stopped.",
        "sandbox_value_limit": "The sandboxed harness built a value over a size limit.",
        "sandbox_invalid_call": "The host refused a sandbox capability call.",
        "sandbox_invalid_message": "The sandbox worker sent a malformed message.",
        "sandbox_invalid_preset": "This preset cannot run through the sandbox preset call.",
        "sandbox_message_limit": "A message across the sandbox boundary exceeded 128 KiB.",
        "sandbox_protocol_failed": "The sandbox worker pipe failed.",
        "sandbox_worker_failed": "The sandbox worker failed without a reported code.",
        # Sub-harness runtime (Section 6.3): fatal for the parent run.
        "subharness_unknown": "The harness called a sub-harness that is not bound to it.",
        "subharness_digest_mismatch": "A bound sub-harness changed after it was bound.",
        "subharness_unlicensed": "A bound sub-harness is not licensed.",
        "subharness_schema_error": "A sub-harness output did not match its output schema.",
        "subharness_depth": "Sub-harness calls nested deeper than 4.",
        # Phase 10 Section 13.4: one fixed ASCII sentence per code.
        "route_reuse_installed": (
            "An installed, licensed harness for exactly this task slot was chosen."
        ),
        "route_warm_start": (
            "A licensed template with the same input and output shape seeds the compile; Stage "
            "4 still decides the license."
        ),
        "route_compile": "No reusable version fits; a normal compile runs.",
        "route_forge_suggested": (
            "Earlier compiles of this task failed Stage 4; rerun with --forge to let Forge try."
        ),
        "route_no_candidate": "No configured model can serve this task.",
        "route_unlicensed": "The candidate failed a fresh license check and was skipped.",
        "route_evicted": "The template was evicted after failed reuse and was skipped.",
        "route_consumed": "The candidate was already consumed and was skipped.",
        "route_budget": "The candidate's measured cost or compute exceeds this task's cap.",
        "route_drift": "The installed entry is drifting; it is offered only as a template.",
        "route_model_unconfigured": "The candidate's model is not configured in this profile.",
        "route_synthetic_excluded": (
            "Evidence from the fake provider cannot rank a real-model task."
        ),
        "route_reuse_not_granted": (
            "An exact installed match exists, but reuse was not granted for this request."
        ),
        "route_no_reuse": "The decision is not an installed reuse, so nothing was run.",
        "route_target_changed": (
            "The chosen version changed on disk after routing; nothing was run."
        ),
        "route_spec_required": "Routing over HTTP needs a Spec, not prose.",
        "route_not_catalogued": "The matching installed entry is not in the server catalog.",
        "route_explore": (
            "A less-tried licensed template was chosen to gather evidence; Stage 4 still gates "
            "it."
        ),
        "route_explore_cap": "Today's exploration budget is spent; the top candidate was used.",
        "route_promote_proposed": (
            "A licensed version may beat the installed one; a recompile would test it."
        ),
        "route_rollback_suggested": (
            "The installed entry is drifting and an earlier revision scored better."
        ),
        "route_model_unknown": "A routing candidate names a model missing from the profile.",
        "route_param_invalid": "A role variant uses a parameter outside its closed set.",
        "fit_slot": "The best match is the exact same task slot.",
        "fit_shape": "The best match shares input, output, tools and domain.",
        "fit_family": "Only a structurally similar task family exists.",
        "fit_none": "No similar verified version exists.",
        "evidence_synthetic_only": (
            "All evidence comes from the fake provider and proves plumbing only."
        ),
        "evidence_insufficient": "Too few held-out cases or reuse trials to rank with confidence.",
        "evidence_store_unavailable": "Routing evidence could not be read; a normal compile runs.",
        "role_evidence_insufficient": (
            "Too few outcomes to switch this role; the pinned default is used."
        ),
        "role_explore": "A less-sampled role variant was used to gather evidence.",
        "role_pinned": "This role is pinned by the operator; routing is off for it.",
        "forge_run_delivered": "The Forge harness was licensed, run once and expired.",
        "forge_run_unlicensed": "The Forge harness was not licensed, so it was not run.",
        "forge_run_reused_installed": (
            "An installed licensed harness for this exact task was run instead of building."
        ),
        "forge_run_needs_expire": (
            "Forge run builds a one-shot harness; set lifecycle expire first."
        ),
        "forge_cases_too_few": "At least 8 cases are required so each half has at least 4.",
        "forge_cases_invalid": "A case row is invalid, duplicated or does not match the schemas.",
        "forge_cases_floor_kind": "User cases need a held_out_tasks floor.",
        "integrate_reused_installed": (
            "The approved feature is already installed and licensed; it was reused."
        ),
        "integrate_not_ready": "The package can be placed only after compile_agent succeeds.",
        "export_cancelled": "Export was not confirmed; nothing was written to the repository.",
        "export_target_exists": "The package destination already exists.",
        "export_path_invalid": "The package destination is outside the repository or not allowed.",
        "export_package_invalid": (
            "The package name must be a lowercase Python identifier of at most 40 characters."
        ),
        "export_unverified": "Only an installed, licensed harness can be exported or run.",
        "export_pin_mismatch": "The exported harness does not match its pinned digests.",
        "export_integrity_mismatch": "An exported file differs from the manifest.",
        "export_subharness_missing": (
            "A bound sub-harness is missing or does not match its digest."
        ),
        "export_failed": "The export could not be written safely.",
        "export_architect_missing": "The architect package is not installed in this interpreter.",
        "license_unverifiable": (
            "Neither this host's signing key nor a trusted public key can check the license."
        ),
        # W7 (docs/trust.md): Ed25519 signatures and the local trust list.
        "key_untrusted": "It is signed by a key this workspace does not trust.",
        "unsigned": "No signature this host trusts covers these files.",
        "trust_list_invalid": "The trust list cannot be read, so no key is trusted.",
        "trust_list_missing": (
            "ARCHITECT_TRUST_FILE names a file that does not exist, so no key is trusted."
        ),
        "trust_in_target": (
            "The signing key or trust list sits inside the folder being checked, so it "
            "cannot vouch for it."
        ),
        "published_unknown": "No published report by that name ships with this install.",
        "published_invalid": "That folder holds a link, a special file or an unusual file name.",
        "verify_target_unknown": (
            "Give a report.json, an artifact folder, an exported package or a published folder."
        ),
        "key_exists": "A key is already there; it was not replaced.",
        "key_missing": "This host holds no Ed25519 private key.",
        "key_invalid": (
            "That key file is not a usable Ed25519 key (PEM, or base64 for a public key)."
        ),
        "registry_subharness_missing": (
            "The artifact's sub-harness tree is missing or invalid, so it was not installed."
        ),
        "rate_no_run": "There is no delivery in this session to rate.",
        "approval_required": (
            "This action needs a prior approve <hash8> of the exact Spec on screen."
        ),
        "feature_compile_failed": (
            "The feature did not compile to an installed, licensed harness; nothing was placed."
        ),
        "approved_feature_must_be_installed": (
            "Forge integrate builds an installed feature; the Spec needs lifecycle install."
        ),
    }
)


def is_code(text: str) -> bool:
    """Only lowercase words and underscores may reach the terminal as a code."""
    return _CODE_PATTERN.fullmatch(text) is not None


def _passthrough(exc: BaseException) -> str:
    text = str(exc)
    return text if is_code(text) else "unknown_failure"


def code_for(exc: BaseException) -> str:
    """Map any exception to a fixed code (Section 9.2); the result always satisfies is_code.

    Order matters: several of these are ValueError subclasses, so the specific
    classes are checked before the general ones and text is never inspected
    except for the exact AdapterError messages.
    """
    if isinstance(exc, SessionError | CLIError | ProfileError | RegistryError):
        return _passthrough(exc)
    fixed = _fixed_code(exc)
    if fixed is not None:
        return fixed
    if isinstance(exc, InferenceError):
        return "inference_failed"
    if isinstance(exc, BudgetExceeded):
        return "budget_exceeded"
    if isinstance(exc, ValidationError):
        return "invalid_spec"
    if isinstance(exc, UnlicensedArtifactError):
        return "unlicensed_artifact"
    if isinstance(exc, ArtifactExpiredError):
        return "artifact_expired"
    if isinstance(exc, RelayRefusal | Unreachable):
        return exc.code
    if isinstance(exc, AdapterError):
        return ADAPTER_MESSAGES.get(str(exc), "provider_failed")
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    if isinstance(exc, KeyboardInterrupt):
        return "interrupted"
    if isinstance(exc, json.JSONDecodeError):
        return "invalid_input_json"
    if isinstance(exc, OSError):
        return "storage_error"
    return "command_failed"


def _fixed_code(exc: BaseException) -> str | None:
    """Phase 10 errors carry one fixed code (``RouteError``, ``ForgeModeError``...)."""
    from architect.forge_modes.errors import ForgeModeError
    from architect.routing.router import RouteError
    from architect.session.exports import ReverifyError

    if isinstance(exc, ForgeModeError | RouteError | ReverifyError):
        return _passthrough(exc)
    return None


def explain(code: str) -> str:
    """One fixed ASCII line per code; unknown codes get a fixed line too."""
    return _EXPLANATIONS.get(code, "No explanation on file for this code.")


def _limit_text(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def hint_for(code: str, spec: Spec | None) -> str:
    """The next command to type after a failure, or "" when there is none.

    Hints are command text only. They may use the Spec on screen (its hash and
    limit) because that Spec is operator-approved data, never model prose.
    """
    token = spec.spec_hash[:8] if spec is not None else "<hash8>"
    if code == "primary_floor_failed":
        lower = _limit_text(max(0.0, spec.floor.limit - 0.1)) if spec is not None else "<limit>"
        return (
            "fix wrong expected values (floor), lower the limit "
            f"(set floor.limit {lower}), or revise the task"
        )
    if code == "secondary_floor_failed":
        return "unset secondary_floor, or fix its expected values in the profile"
    if code in {"primary_budget_failed", "secondary_budget_failed"}:
        return "set budget.cost_usd <usd> or set budget.compute_s <seconds>"
    if code == "compile_budget":
        return "budget compile 10 3600"
    if code == "workspace_path_too_long":
        return "quit, then start again with a shorter --workspace path"
    if code in {"floor_unavailable", "floor_not_configured", "floor_too_small"}:
        return "floor"
    if code in {"primary_sample_count", "secondary_sample_count"}:
        return "floor (add cases) or set floor.minimum_samples <n>"
    if code == "secondary_floor_not_configured":
        return "unset secondary_floor"
    if code == "floor_kind_mismatch":
        return "set floor.kind <configured kind>"
    if code == "budget_below_quote":
        return "budget"
    if code in {"input_schema_missing", "output_schema_missing"}:
        field = code.removesuffix("_missing")
        return (
            f"set {field} "
            '{"type":"object","properties":{...},"required":[...],"additionalProperties":false}'
        )
    if code == "compile_deadline_short":
        return "budget compile <usd> <seconds>"
    if code in {"stale_approval_token", "approval_token_required"}:
        return f"approve {token}"
    if code == "install_token_required":
        return f"install <name> {token}"
    if code == "no_goal":
        return "describe the task again, or use set"
    if code == "no_inference_model":
        return "spec <file|{json}>"
    if code == "not_a_command":
        return "help"
    if code in {"unlicensed_artifact", "no_licensed_artifact"}:
        return "again"
    if code == "not_installable":
        return "run {json}"
    if code == "unreviewed_cases":
        return "review <id> ok | review <id> expected <json> | review <id> drop"
    if code == "no_provider":
        return "log in with 'claude' or 'codex', or write architect.json"
    if code == "model_not_configured":
        return "set model_target <model in profile>"
    if code == "no_draft":
        return "describe <text> or spec <file|{json}>"
    if code == "bench_over_max_usd":
        return "raise --max-usd or drop models with: bench quick <model>"
    if code in {"bench_profile_required", "bench_model_unconfigured", "bench_no_models"}:
        return "bench smoke, or bench quick <model in profile>"
    if code == "bench_extra_missing":
        return "uv sync --extra bench"
    if code == "bench_model_not_local":
        return "point base_url at http://localhost:11434 with a $0 price table, or drop the model"
    if code in {"bench_rows_unreadable", "bench_compare_mismatch", "bench_no_cells"}:
        return "bench list"
    if code == "forge_budget_invalid":
        return "forge budget 5 60"
    if code == "forge_model_unknown":
        return "forge model <model in profile>"
    if code == "forge_disabled":
        return "add a smoke file to the floor in the profile, or forge off"
    if code == "forge_cancelled":
        return "compile again and answer y, or forge off"
    if code == "forge_run_needs_expire":
        return "set lifecycle expire"
    if code == "approval_required":
        return f"approve {token} (runs a normal paid compile), then again and repeat the command"
    if code == "route_reuse_not_granted":
        return f"reuse <name> {token} {{json}}"
    if code == "rate_no_run":
        return "run <name|dir> {json}, then rate good|bad"
    if code == "export_unverified":
        return "install <name> <hash8>, then export"
    if code == "export_cancelled":
        return "the package stays staged; forge integrate <repo> <into> places it after a y"
    return ""
