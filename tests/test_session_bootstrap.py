"""Bootstrap is trusted host code: fixed tables, which() results and quotes; nothing executes."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from architect.compiler.inference import _INFERENCE_BUDGET
from architect.models import (
    AdapterError,
    ClaudeCodeAdapter,
    ModelRequest,
    ModelResponse,
    UsageQuote,
)
from architect.profiles import build_services, load_profile
from architect.runtime import Runtime
from architect.session.bootstrap import (
    CLAUDE_MODELS,
    CODEX_MODELS,
    DEFAULT_MODEL,
    INFERENCE_FALLBACK,
    SESSION_PROFILE,
    CliCandidate,
    choose_inference,
    default_workspace,
    detect_clis,
    discover_profile,
    inference_quote,
    min_call_cost,
    model_rows,
    probe_login,
    profile_from_payload,
    profile_payload,
    write_profile,
)
from architect.session.codes import ADAPTER_MESSAGES, SessionError
from architect.telemetry import TelemetryStore

HAIKU = "claude-haiku-4-5-20251001"


def which_both(name: str) -> str | None:
    return {"claude": "/opt/bin/claude", "codex": "/opt/bin/codex"}.get(name)


def which_codex(name: str) -> str | None:
    return "/opt/bin/codex" if name == "codex" else None


class ProbeAdapter:
    """A CLI-shaped adapter whose only behaviour is the failure it is told to raise."""

    def __init__(self, error: Exception | None = None, *, cost: float = 0.01) -> None:
        self.model = "fake"
        self.error = error
        self.cost = cost
        self.requests: list[ModelRequest] = []

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=8, tokens_out=16, cost_usd=self.cost)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return ModelResponse(text="ok", tokens_in=8, tokens_out=1, cost_usd=self.cost, model="fake")


def test_discover_profile_prefers_explicit_then_cwd_then_workspace(tmp_path: Path) -> None:
    cwd = tmp_path / "cwd"
    workspace = cwd / ".architect"
    workspace.mkdir(parents=True)
    explicit = tmp_path / "missing.json"
    assert discover_profile(cwd, workspace, explicit) == explicit
    assert discover_profile(cwd, workspace, None) is None
    (workspace / SESSION_PROFILE).write_text("{}", encoding="utf-8")
    assert discover_profile(cwd, workspace, None) == workspace / SESSION_PROFILE
    (cwd / "architect.json").write_text("{}", encoding="utf-8")
    assert discover_profile(cwd, workspace, None) == cwd / "architect.json"


def test_default_workspace_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARCHITECT_WORKSPACE", raising=False)
    assert default_workspace(tmp_path, None) == tmp_path / ".architect"
    monkeypatch.setenv("ARCHITECT_WORKSPACE", "elsewhere")
    assert default_workspace(tmp_path, None) == tmp_path / "elsewhere"
    assert default_workspace(tmp_path, Path("w")) == tmp_path / "w"
    assert default_workspace(tmp_path, tmp_path / "abs") == tmp_path / "abs"


def test_detect_clis_uses_which_only_and_filters_prices() -> None:
    assert detect_clis(lambda name: None) == ()
    found = detect_clis(which_both)
    assert [c.provider for c in found] == ["claude-code", "codex"]
    assert all(os.path.isabs(c.executable) for c in found)
    assert found[0].models == CLAUDE_MODELS and found[1].models == CODEX_MODELS
    only = detect_clis(which_codex)
    assert len(only) == 1 and only[0].provider == "codex"


def test_model_rows_quote_only_and_infer_rule() -> None:
    rows = model_rows(detect_clis(which_both))
    assert [row.index for row in rows] == list(range(1, len(rows) + 1))
    assert [row.model for row in rows] == [*CLAUDE_MODELS, *CODEX_MODELS]
    by_model = {row.model: row for row in rows}
    haiku = by_model[HAIKU]
    assert haiku.can_infer and haiku.provider == "claude-code" and haiku.probe_fits
    assert haiku.executable.endswith("claude")
    assert 0 < haiku.probe_cost < haiku.min_call_cost
    adapter = ClaudeCodeAdapter(HAIKU, executable="/opt/bin/claude")
    assert haiku.min_call_cost == min_call_cost(adapter)
    assert inference_quote(adapter) <= _INFERENCE_BUDGET.cost_usd
    expensive = by_model["claude-fable-5-1"]
    assert not expensive.can_infer and expensive.min_call_cost > haiku.min_call_cost
    codex = by_model[CODEX_MODELS[0]]
    assert codex.provider == "codex" and not codex.can_infer
    assert DEFAULT_MODEL == {"claude-code": HAIKU, "codex": CODEX_MODELS[0]}


def test_choose_inference_falls_back_to_haiku_only_with_claude_code() -> None:
    candidates = detect_clis(which_both)
    rows = model_rows(candidates)
    assert choose_inference(HAIKU, candidates, rows) == HAIKU
    assert choose_inference("claude-opus-5-5", candidates, rows) == INFERENCE_FALLBACK
    assert choose_inference(CODEX_MODELS[0], candidates, rows) == INFERENCE_FALLBACK
    codex_only = detect_clis(which_codex)
    assert choose_inference(CODEX_MODELS[0], codex_only, model_rows(codex_only)) is None


def test_profile_payload_exact_shape() -> None:
    candidates = detect_clis(which_both)
    rows = {row.model: row for row in model_rows(candidates)}
    payload = profile_payload(rows[HAIKU], HAIKU, candidates)
    claude = rows[HAIKU].executable
    assert payload == {
        "workspace": ".",
        "models": {HAIKU: {"provider": "claude-code", "executable": claude}},
        "inference": {"provider": "claude-code", "model": HAIKU, "executable": claude},
        "compile": {
            "model_ladder": [HAIKU],
            "max_candidates": 2,
            "enable_rewriting": False,
            "max_rewrites": 0,
            "compile_budget": {"cost_usd": 5, "compute_s": 1800},
        },
    }
    opus = profile_payload(rows["claude-opus-5-5"], INFERENCE_FALLBACK, candidates)
    assert set(opus["models"]) == {"claude-opus-5-5", HAIKU}  # type: ignore[arg-type]
    assert opus["inference"] == {"provider": "claude-code", "model": HAIKU, "executable": claude}
    assert opus["compile"]["model_ladder"] == ["claude-opus-5-5"]  # type: ignore[index]
    codex_only = detect_clis(which_codex)
    codex = profile_payload(model_rows(codex_only)[0], None, codex_only)
    assert "inference" not in codex and list(codex) == ["workspace", "models", "compile"]
    assert codex["models"] == {
        CODEX_MODELS[0]: {"provider": "codex", "executable": codex_only[0].executable}
    }
    with pytest.raises(SessionError, match="model_not_configured"):
        profile_payload(model_rows(codex_only)[0], INFERENCE_FALLBACK, codex_only)


def test_write_profile_round_trips_and_keeps_secrets_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-key-value")
    monkeypatch.setenv("CLAUDECODE", "1")
    candidates = detect_clis(which_both)
    rows = {row.model: row for row in model_rows(candidates)}
    payload = profile_payload(rows["claude-opus-5-5"], INFERENCE_FALLBACK, candidates)
    workspace = tmp_path / "w"
    path = write_profile(payload, workspace / SESSION_PROFILE)
    assert path == workspace / SESSION_PROFILE and not list(workspace.glob("*.tmp"))
    text = path.read_text(encoding="utf-8")
    assert "sk-secret" not in text and "CLAUDECODE" not in text
    assert json.loads(text) == payload
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    profile = load_profile(path)
    assert profile.resolve(profile.workspace) == workspace.resolve()
    assert profile.compile.model_ladder == ("claude-opus-5-5",)
    assert profile.compile.max_candidates == 2 and not profile.compile.enable_rewriting
    assert profile.compile.compile_budget.cost_usd == 5
    assert profile.inference is not None and profile.inference.model == HAIKU
    assert profile.models[HAIKU].executable == rows[HAIKU].executable
    same = profile_from_payload(payload, workspace)
    assert same.resolve(same.workspace) == profile.resolve(profile.workspace)
    assert same.models == profile.models and same.compile == profile.compile


def test_write_profile_reports_only_a_code(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(SessionError, match="profile_write_failed"):
        write_profile({"workspace": "."}, blocker / "nested" / SESSION_PROFILE)
    with pytest.raises(SessionError, match="invalid_profile"):
        profile_from_payload({"workspace": ".", "models": {}}, tmp_path)


def test_fake_variant_of_the_written_profile_builds_services(tmp_path: Path) -> None:
    workspace = tmp_path / "w"
    payload = profile_payload(
        model_rows(detect_clis(which_both))[0], HAIKU, detect_clis(which_both)
    )
    payload["models"] = {"fake": {"provider": "fake"}}
    payload["inference"] = {"provider": "fake", "model": "fake"}
    payload["compile"]["model_ladder"] = ["fake"]  # type: ignore[index]
    path = write_profile(payload, workspace / SESSION_PROFILE)
    profile = load_profile(path)
    services = build_services(profile, profile.workspace)
    try:
        assert services.workspace == workspace.resolve()
        assert path.resolve() in services.profile.pi.denied_paths
        assert services.compiler.inference_adapter is not None
    finally:
        services.close()


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (None, "ok"),
        (AdapterError("Provider CLI is unavailable"), "unavailable"),
        (AdapterError("Provider CLI timed out"), "error"),
        (AdapterError("Provider request failed or returned invalid JSON"), "not_logged_in"),
        (AdapterError("Provider reported an error"), "not_logged_in"),
        (AdapterError("Provider usage limit reached"), "not_logged_in"),
        (AdapterError("some new wording sk-secret"), "error"),
        (RuntimeError("boom sk-secret"), "error"),
    ],
)
async def test_probe_login_maps_every_failure_to_a_status(
    tmp_path: Path, error: Exception | None, status: str, capsys: pytest.CaptureFixture[str]
) -> None:
    store = TelemetryStore(tmp_path / "telemetry")
    try:
        runtime = Runtime(store)
        adapter = ProbeAdapter(error)
        result = await probe_login(adapter, runtime, clock=iter([10.0, 12.5]).__next__)
        rows = store.rows()
    finally:
        store.close()
    assert result.status == status
    assert result.spend.calls == 1 and result.spend.wall_s == 2.5
    assert len(adapter.requests) == 1
    assert adapter.requests[0].prompt == "Reply with the single word ok."
    assert adapter.requests[0].max_output_tokens == 16
    assert len(rows) == 1 and rows[0].model_calls == 1
    if status == "ok":
        assert result.spend.cost_usd == 0.01 and result.spend.tokens_out == 1
        assert rows[0].accepted
    else:
        assert not rows[0].accepted
    captured = capsys.readouterr()
    assert "sk-secret" not in captured.out + captured.err + repr(result)
    if isinstance(error, AdapterError):
        assert ADAPTER_MESSAGES.get(str(error), "provider_failed") in {
            "provider_unavailable",
            "provider_timeout",
            "provider_not_logged_in",
            "provider_failed",
        }


async def test_probe_login_over_budget_never_calls_the_provider(tmp_path: Path) -> None:
    store = TelemetryStore(tmp_path / "telemetry")
    try:
        adapter = ProbeAdapter(cost=1.5)
        result = await probe_login(adapter, Runtime(store))
    finally:
        store.close()
    assert result.status == "over_budget" and adapter.requests == []
    assert result.spend.cost_usd == 0


def test_max_output_tokens_appears_only_in_bootstrap_with_16() -> None:
    package = Path(__file__).resolve().parents[1] / "architect" / "session"
    for module in package.glob("*.py"):
        text = module.read_text(encoding="utf-8")
        if module.name != "bootstrap.py":
            assert "max_output_tokens" not in text, module.name
        else:
            assert "max_output_tokens=16" in text
            assert text.count("max_output_tokens") == 1


def test_candidate_is_frozen_data() -> None:
    candidate = CliCandidate(provider="codex", executable="/opt/bin/codex", models=CODEX_MODELS)
    with pytest.raises(AttributeError):
        candidate.executable = "/tmp/other"  # type: ignore[misc]
