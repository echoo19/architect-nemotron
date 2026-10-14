"""Trusted first-run setup: profile discovery, CLI detection and the login probe.

Everything here runs in host code from ``shutil.which`` results, fixed model
tables and operator-typed choices; no model output is involved. Detection
executes nothing. Menu rows are computed from adapter quotes built exactly as
``profiles._adapter`` builds them, so the same environment scrubbing applies
when the probe later runs. The probe is one metered call through
``Runtime.evaluate`` with a control Spec, so it is wallet-checked and logged
like every other call; its result is a status word, never provider text.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import JsonValue, ValidationError

from architect.compiler.inference import _INFERENCE_BUDGET
from architect.models import (
    AdapterError,
    ClaudeCodeAdapter,
    CodexAdapter,
    ModelAdapter,
    ModelRequest,
)
from architect.models.pricing import load_prices
from architect.profiles import Profile
from architect.runtime import BudgetExceeded, RunContext, Runtime
from architect.spec import Budget, FloorDefinition, FloorKind, Spec

from .codes import ADAPTER_MESSAGES, SessionError
from .state import ModelRow, Spend

__all__ = [
    "CLAUDE_MODELS",
    "CODEX_MODELS",
    "DEFAULT_MODEL",
    "INFERENCE_FALLBACK",
    "PROFILE_CANDIDATES",
    "SESSION_PROFILE",
    "CliCandidate",
    "ModelRow",
    "ProbeResult",
    "choose_inference",
    "default_workspace",
    "detect_clis",
    "discover_profile",
    "inference_quote",
    "min_call_cost",
    "model_rows",
    "probe_login",
    "profile_from_payload",
    "profile_payload",
    "write_profile",
]

PROFILE_CANDIDATES: tuple[str, ...] = ("architect.json",)
SESSION_PROFILE = "session-profile.json"
CLAUDE_MODELS = (
    "claude-haiku-4-5-20251001",
    "claude-sonnet-5",
    "claude-opus-5-5",
    "claude-fable-5-1",
)
CODEX_MODELS = ("gpt-5.6-terra",)
DEFAULT_MODEL = {"claude-code": "claude-haiku-4-5-20251001", "codex": "gpt-5.6-terra"}
INFERENCE_FALLBACK = "claude-haiku-4-5-20251001"

# The probe asks for one word; 16 output tokens size its quote for a CLI adapter
# (quote-only per models/cli.py). The Stage 0 quote uses a prompt the size of
# the real inference prompt and reserves the model's full output capacity.
_PROBE_PROMPT = "Reply with the single word ok."
_PROBE_REQUEST = ModelRequest(prompt=_PROBE_PROMPT, max_output_tokens=16)
_PROBE_BUDGET_USD = 1.0
_INFERENCE_PROMPT_CHARS = 6500
_CLI_PROVIDERS: tuple[tuple[Literal["claude-code", "codex"], str, tuple[str, ...]], ...] = (
    ("claude-code", "claude", CLAUDE_MODELS),
    ("codex", "codex", CODEX_MODELS),
)


@dataclass(frozen=True)
class CliCandidate:
    provider: Literal["claude-code", "codex"]
    executable: str
    models: tuple[str, ...]


@dataclass(frozen=True)
class ProbeResult:
    status: Literal["ok", "not_logged_in", "unavailable", "over_budget", "error"]
    spend: Spend


def discover_profile(cwd: Path, workspace: Path, explicit: Path | None) -> Path | None:
    """An explicit path wins even when missing, so a typo fails as invalid_profile."""
    if explicit is not None:
        return explicit
    for name in PROFILE_CANDIDATES:
        candidate = cwd / name
        if candidate.is_file():
            return candidate
    candidate = workspace / SESSION_PROFILE
    if candidate.is_file():
        return candidate
    return None


def default_workspace(cwd: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        return cwd / explicit
    configured = os.environ.get("ARCHITECT_WORKSPACE")
    if configured:
        return cwd / Path(configured)
    return cwd / ".architect"


def detect_clis(which: Callable[[str], str | None] = shutil.which) -> tuple[CliCandidate, ...]:
    """Look up the CLIs on PATH; nothing is executed and unpriced ids are never offered."""
    priced = load_prices().models
    found: list[CliCandidate] = []
    for provider, command, models in _CLI_PROVIDERS:
        executable = which(command)
        if not executable:
            continue
        found.append(
            CliCandidate(
                provider=provider,
                executable=os.path.abspath(executable),
                models=tuple(model for model in models if model in priced),
            )
        )
    return tuple(found)


def _adapter(provider: str, model: str, executable: str) -> ModelAdapter:
    """Build exactly what ``profiles._adapter`` builds for a CLI entry without an API key."""
    if provider == "claude-code":
        return ClaudeCodeAdapter(model, executable=executable)
    return CodexAdapter(model, executable=executable)


def min_call_cost(adapter: ModelAdapter) -> float:
    return adapter.quote(ModelRequest(prompt="ok")).cost_usd


def inference_quote(adapter: ModelAdapter) -> float:
    return adapter.quote(ModelRequest(prompt="x" * _INFERENCE_PROMPT_CHARS)).cost_usd


def model_rows(candidates: Sequence[CliCandidate]) -> tuple[ModelRow, ...]:
    rows: list[ModelRow] = []
    for candidate in candidates:
        for model in candidate.models:
            adapter = _adapter(candidate.provider, model, candidate.executable)
            probe_cost = adapter.quote(_PROBE_REQUEST).cost_usd
            rows.append(
                ModelRow(
                    index=len(rows) + 1,
                    model=model,
                    provider=candidate.provider,
                    executable=candidate.executable,
                    min_call_cost=min_call_cost(adapter),
                    can_infer=inference_quote(adapter) <= _INFERENCE_BUDGET.cost_usd,
                    probe_cost=probe_cost,
                    probe_fits=probe_cost <= _PROBE_BUDGET_USD,
                )
            )
    return tuple(rows)


def _claude_candidate(candidates: Sequence[CliCandidate]) -> CliCandidate | None:
    for candidate in candidates:
        if candidate.provider == "claude-code" and INFERENCE_FALLBACK in candidate.models:
            return candidate
    return None


def choose_inference(
    model: str, candidates: Sequence[CliCandidate], rows: Sequence[ModelRow]
) -> str | None:
    """The chosen model when it fits the fixed Stage 0 budget, else Haiku via claude-code."""
    for row in rows:
        if row.model == model and row.can_infer:
            return model
    if _claude_candidate(candidates) is not None:
        return INFERENCE_FALLBACK
    return None


def profile_payload(
    chosen: ModelRow, inference: str | None, candidates: Sequence[CliCandidate]
) -> dict[str, JsonValue]:
    """The exact JSON the session writes; ``workspace "."`` pins the file's own directory."""
    models: dict[str, JsonValue] = {
        chosen.model: {"provider": chosen.provider, "executable": chosen.executable}
    }
    inference_entry: dict[str, JsonValue] | None = None
    if inference is not None:
        if inference == chosen.model:
            inference_entry = {
                "provider": chosen.provider,
                "model": inference,
                "executable": chosen.executable,
            }
        else:
            claude = _claude_candidate(candidates)
            if claude is None:
                raise SessionError("model_not_configured")
            models[inference] = {"provider": claude.provider, "executable": claude.executable}
            inference_entry = {
                "provider": claude.provider,
                "model": inference,
                "executable": claude.executable,
            }
    payload: dict[str, JsonValue] = {"workspace": ".", "models": models}
    if inference_entry is not None:
        payload["inference"] = inference_entry
    payload["compile"] = {
        "model_ladder": [chosen.model],
        "max_candidates": 2,
        "enable_rewriting": False,
        "max_rewrites": 0,
        "compile_budget": {"cost_usd": 5, "compute_s": 1800},
    }
    return payload


def write_profile(payload: dict[str, JsonValue], path: Path) -> Path:
    """Atomic write, private mode where honoured; the OSError never reaches the screen."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    except OSError:
        raise SessionError("profile_write_failed") from None
    return path


def profile_from_payload(payload: dict[str, JsonValue], workspace: Path) -> Profile:
    """Validate the in-memory payload with the base ``load_profile`` will use for the file."""
    try:
        profile = Profile.model_validate(payload)
    except (ValidationError, ValueError, TypeError):
        raise SessionError("invalid_profile") from None
    profile._base = workspace
    return profile


async def probe_login(
    adapter: ModelAdapter,
    runtime: Runtime,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> ProbeResult:
    """One tiny metered call; the answer is a status word, never provider text."""
    control = Spec(
        task="Probe provider login",
        input_schema={},
        output_schema={},
        floor=FloorDefinition(
            kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="login-probe-control"
        ),
        budget=Budget(cost_usd=_PROBE_BUDGET_USD, compute_s=90),
        model_target=adapter.model,
    )

    async def probe(context: RunContext, task: JsonValue) -> JsonValue:
        return await context.call(_PROBE_PROMPT, 16)

    started = clock()
    status: Literal["ok", "not_logged_in", "unavailable", "over_budget", "error"]
    spend = Spend(calls=1)
    try:
        result = await runtime.evaluate(
            control, probe, None, adapter, preset="login_probe", components=("login_probe",)
        )
    except BudgetExceeded:
        status = "over_budget"
    except AdapterError as exc:
        code = ADAPTER_MESSAGES.get(str(exc), "provider_failed")
        if code == "provider_unavailable":
            status = "unavailable"
        elif code == "provider_not_logged_in":
            status = "not_logged_in"
        else:
            status = "error"
    except Exception:
        status = "error"
    else:
        status = "ok"
        spend.tokens_in = result.tokens_in
        spend.tokens_out = result.tokens_out
        spend.cost_usd = result.cost_usd
    if status != "ok":
        row = runtime.last_row
        if row is not None:
            spend.tokens_in = row.tokens_in
            spend.tokens_out = row.tokens_out
            spend.cost_usd = row.cost_usd
    spend.wall_s = max(0.0, clock() - started)
    return ProbeResult(status=status, spend=spend)
