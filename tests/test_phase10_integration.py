"""Phase 10 WP5 (Section 15.5): the router, role seats and Forge modes wired into the
compiler, orchestrator, CLI, session and HTTP server.

Offline only: fake adapters, a socket-blocking fixture, temp workspaces and the
throwaway ``ARCHITECT_SIGNING_KEY_FILE`` of ``conftest``. No live provider, no
``claude``/``codex`` CLI and no Ollama is ever called.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from forge_modes_support import block_sockets, forge_repairs, logical_lines, make_profile, spec
from pydantic import JsonValue
from session_support import fake_profile, make_spec, run_script

import architect.cli as cli
import architect.orchestrator as orchestration
from architect.compiler import CompileConfig, Compiler, CompileResult
from architect.floors import EvaluationCase, HeldOutTasks
from architect.library import HarnessLibrary
from architect.models import FakeAdapter, ModelResponse
from architect.orchestrator import Orchestrator
from architect.profiles import Services, build_services, load_profile
from architect.registry import Registry
from architect.routing.evidence import EvidenceIndex
from architect.routing.log import RouteLog
from architect.routing.router import RouteDecision, RoutePolicy, Router
from architect.runtime import Runtime
from architect.runtime.ephemeral import EPHEMERAL_CODE, discard_code
from architect.server import create_app
from architect.spec import Budget, FloorDefinition, HarnessArtifact, Lifecycle, Spec
from architect.telemetry import TelemetryStore

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    block_sockets(monkeypatch)


def routed_profile(tmp_path: Path, **extra: Any) -> Path:
    """The WP3 fake profile with a ``routing`` table: every compile routes."""
    return make_profile(tmp_path, routing={}, **extra)


def services_of(profile: Path) -> Services:
    loaded = load_profile(profile)
    return build_services(loaded, loaded.workspace)


def installed_spec(**changes: Any) -> Spec:
    return spec(lifecycle=Lifecycle.INSTALL, **changes)


async def install(services: Services, name: str, target: Spec) -> HarnessArtifact:
    result = await services.compiler.compile(target, approved=True, no_route=True)
    assert result.artifact is not None and result.artifact.licensed, result.failures
    services.registry.install(name, result.artifact)
    return services.registry.get(name)


def keys_of(result: CompileResult) -> tuple[object, ...]:
    artifact = result.artifact
    return (
        result.stages,
        result.failures,
        result.approval_required,
        result.warm_started,
        artifact is not None and artifact.licensed,
        None if artifact is None else artifact.spec.spec_hash,
    )


# --------------------------------------------------------------- compiler seams


async def test_default_profile_compiles_exactly_without_routing(tmp_path: Path) -> None:
    services = services_of(make_profile(tmp_path))
    try:
        assert services.router is not None and services.compiler.route_all is False
        result = await services.compiler.compile(spec())
        assert result.route is None and result.reused is False
        assert result.artifact is not None and result.artifact.licensed
        # Nothing is routed or logged for a default-profile compile.
        assert not (services.workspace / "routes" / "decisions.jsonl").exists()
    finally:
        services.close()


async def test_no_route_is_identical_to_a_compiler_without_router(tmp_path: Path) -> None:
    outcomes = []
    for label, disable in (("a", "flag"), ("b", "none")):
        root = tmp_path / label
        root.mkdir()
        services = services_of(routed_profile(root))
        try:
            assert services.compiler.route_all is True
            if disable == "none":
                services.compiler.router = None
                result = await services.compiler.compile(spec())
            else:
                result = await services.compiler.compile(spec(), no_route=True)
            assert result.route is None
            outcomes.append(keys_of(result))
        finally:
            services.close()
    assert outcomes[0] == outcomes[1]
    assert outcomes[0][0] == (1, 2, 4) and outcomes[0][4] is True


async def test_granted_reuse_is_the_installed_entry_with_no_stage_and_no_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    services = services_of(routed_profile(tmp_path))
    try:
        target = installed_spec()
        entry = await install(services, "invoices", target)
        installs: list[str] = []
        original = Registry.install

        def spy(self: Registry, name: str, *args: Any, **kwargs: Any) -> Any:
            installs.append(name)
            return original(self, name, *args, **kwargs)

        monkeypatch.setattr(Registry, "install", spy)
        result = await services.compiler.compile(target, approved=True, reuse_installed=True)
        assert result.reused is True and result.stages == ()
        assert result.route is not None and result.route.action == "reuse_installed"
        assert result.artifact is not None and result.artifact.directory == entry.directory
        assert result.artifact.licensed and installs == []
        # The CLI never reinstalls a reused artifact.
        assert cli._compiled(result, services) == 0
        assert installs == []
    finally:
        services.close()


async def test_without_a_grant_an_exact_match_is_advice_and_approval_carries_it(
    tmp_path: Path,
) -> None:
    services = services_of(routed_profile(tmp_path))
    try:
        target = installed_spec()
        await install(services, "invoices", target)
        proposal = await services.compiler.compile(target)
        assert proposal.approval_required and proposal.route is not None
        assert "route_reuse_not_granted" in proposal.route.codes
        compiled = await services.compiler.compile(target, approved=True)
        assert compiled.reused is False and 4 in compiled.stages
        assert compiled.route is not None and compiled.route.action != "reuse_installed"
    finally:
        services.close()


async def test_a_sibling_task_warm_starts_through_the_routed_template(tmp_path: Path) -> None:
    services = services_of(routed_profile(tmp_path))
    try:
        first = await services.compiler.compile(spec())
        assert first.artifact is not None and first.artifact.licensed
        sibling = await services.compiler.compile(spec(task="Return ok for this text"))
        assert sibling.route is not None and sibling.route.action == "warm_start"
        assert "route_warm_start" in sibling.route.codes and sibling.warm_started
        # Stage 4 still ran and decided the license of the warm-started harness.
        assert 4 in sibling.stages and sibling.artifact is not None
        assert sibling.artifact.licensed
    finally:
        services.close()


# ------------------------------------------------------------------ orchestrator

_DELIVERABLE = '''    @staticmethod
    def _deliverable(result: CompileResult, artifact: HarnessArtifact, budget: Budget) -> bool:
        """Independent code checks; a model-supplied field can never pass these."""
        return (
            0 in result.stages
            and 4 in result.stages
            and result.report is not None
            and artifact.report == result.report
            and artifact.spec == result.spec
            and artifact.spec.lifecycle == Lifecycle.EXPIRE
            and _within(artifact.spec.budget, budget)
            and artifact.licensed
        )
'''


def test_deliverable_is_unchanged_and_discard_code_moved_unchanged() -> None:
    source = inspect.getsource(Orchestrator._deliverable)
    assert source.replace("\r\n", "\n") == _DELIVERABLE
    assert orchestration._discard_code is discard_code
    assert orchestration._EPHEMERAL_CODE == EPHEMERAL_CODE == (
        "harness.py", "pi-pack.json", "preset.json", "rewrite.diff",
    )


async def test_deliverable_routed_truth_table(tmp_path: Path) -> None:
    services = services_of(routed_profile(tmp_path))
    try:
        target = installed_spec()
        entry = await install(services, "invoices", target)
        probe = target.model_copy(update={"lifecycle": Lifecycle.EXPIRE})
        base = CompileResult(
            spec=probe, artifact=entry, report=entry.report, stages=(0,), reused=True
        )
        cap = Budget(cost_usd=1.0, compute_s=60)
        ok = Orchestrator._deliverable_routed
        assert ok(base, entry, probe, cap, True)
        assert not ok(base, entry, probe, cap, False)  # no grant
        assert not ok(base.model_copy(update={"reused": False}), entry, probe, cap, True)
        assert not ok(base.model_copy(update={"stages": ()}), entry, probe, cap, True)
        other = probe.model_copy(update={"task": "Return something else"})
        assert not ok(base, entry, other, cap, True)  # another slot
        tight = Budget(cost_usd=entry.spec.budget.cost_usd / 2, compute_s=60)
        assert not ok(base, entry, probe, tight, True)  # over the subtask cap
        expire = entry.model_copy(
            update={"spec": entry.spec.model_copy(update={"lifecycle": Lifecycle.EXPIRE})}
        )
        assert not ok(base, expire, probe, cap, True)  # only INSTALL entries are reused
    finally:
        services.close()


def _floor(definition: FloorDefinition) -> HeldOutTasks:
    return HeldOutTasks(
        definition,
        [EvaluationCase(case_id=f"c{i}", task={"action": i}, expected="ok") for i in range(4)],
    )


def _response(text: str) -> ModelResponse:
    return ModelResponse(text=text, tokens_in=10, tokens_out=10, cost_usd=2e-5, model="fake")


async def test_routed_do_delivers_the_installed_entry_and_keeps_its_harness(
    tmp_path: Path,
) -> None:
    subtask_spec = {
        "task": "Summarize numbers", "input_schema": {}, "output_schema": {"type": "string"},
        "floor": {"kind": "held_out_tasks", "limit": 1.0, "dataset": "ok-cases"},
        "budget": {"cost_usd": 0.1, "compute_s": 60}, "model_target": "fake",
        "lifecycle": "expire", "domain": "synthesizer", "tools": [], "pipeline": [],
    }
    plan = json.dumps({"tasks": [{
        "id": "one", "request": "Summarize the numbers in one sentence",
        "input": {"numbers": [1, 2]}, "domain": "synthesizer", "model": "fake",
        "budget": {"cost_usd": 0.1, "compute_s": 60}, "tools": [], "depends_on": [],
    }]})
    store = TelemetryStore(tmp_path / "telemetry")
    runtime = Runtime(store)
    library = HarnessLibrary(tmp_path / "library")
    registry = Registry(tmp_path / "registry")
    inference = FakeAdapter(responses=[_response(json.dumps(subtask_spec))] * 3)
    compiler = Compiler(
        tmp_path / "artifacts", runtime, lambda model: FakeAdapter(model=model), _floor,
        config=CompileConfig(max_candidates=1, enable_rewriting=False),
        library=library, inference_adapter=inference,
    )
    log = RouteLog(tmp_path / "routes")
    index = EvidenceIndex(registry, library, store, log, tmp_path,
                          providers={"fake": "fake"}, promotion_reuses=3)
    compiler.router = Router(index, RoutePolicy(), log)
    host: dict[str, JsonValue] = {"output_schema": {"type": "string"}}
    try:
        decomposition = FakeAdapter(responses=[_response(plan)] * 2)
        orchestrator = Orchestrator(compiler, decomposition)
        first = await orchestrator.run("summarize", overrides=host)
        assert first.accepted
        directory = first.tasks[0].artifact_directory
        assert directory is not None and not (directory / "harness.py").exists()
        compiled_spec = Spec.model_validate_json((directory / "spec.json").read_text("utf-8"))
        approved = compiled_spec.model_copy(update={"lifecycle": Lifecycle.INSTALL})
        result = await compiler.compile(approved, approved=True, no_route=True)
        assert result.artifact is not None and result.artifact.licensed
        registry.install("summary", result.artifact)
        entry = registry.get("summary")
        routed = await orchestrator.run("summarize", overrides=host, reuse_installed=True)
        assert routed.accepted, routed
        task = routed.tasks[0]
        assert task.output == "ok" and task.artifact_directory == entry.directory
        # The installed entry was delivered, never discarded or consumed.
        assert (entry.directory / "harness.py").is_file()
        assert not (entry.directory / ".consumed").exists()
    finally:
        store.close()


# --------------------------------------------------------------------------- CLI


def _cli(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_route_shows_advice_and_runs_only_a_granted_reuse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import asyncio

    profile = routed_profile(tmp_path)
    services = services_of(profile)
    try:
        asyncio.run(install(services, "invoices", installed_spec()))
    finally:
        services.close()
    path = tmp_path / "spec.json"
    path.write_text(installed_spec().model_dump_json(), encoding="utf-8")
    code, out, _err = _cli(["route", str(path), "--profile", str(profile)], capsys)
    assert code == 0
    assert "invoices@1" in out and "route_reuse_not_granted" in out
    assert "Stage 4 alone decides every license" in out
    assert "plumbing, not quality" in out  # synthetic_fake evidence is labelled
    assert all(len(line) <= 100 and line.isascii() for line in out.splitlines())
    code, out, _err = _cli(["route", str(path), "--profile", str(profile), "--json"], capsys)
    decision = RouteDecision.model_validate_json(out)
    # Advice never reuses: the exact entry is advice, its library snapshot a warm start.
    assert code == 0 and decision.action == "warm_start" and decision.origin == "cli"
    assert "route_reuse_not_granted" in decision.codes
    run = ["route", str(path), "--profile", str(profile), "--run", '{"text": "hi"}']
    code, out, err = _cli(run, capsys)
    assert code == 1 and json.loads(err.splitlines()[-1]) == {"error": "route_no_reuse"}
    assert out == ""
    code, out, err = _cli([*run, "--yes"], capsys)
    assert code == 0, err
    payload = json.loads(out)
    assert payload["reused"] is True and payload["output"] == "ok"
    assert payload["route"]["action"] == "reuse_installed"


def test_cli_compile_no_route_and_roles(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = routed_profile(tmp_path)
    path = tmp_path / "spec.json"
    path.write_text(spec().model_dump_json(), encoding="utf-8")
    code, out, _err = _cli(["compile", str(path), "--profile", str(profile)], capsys)
    routed = json.loads(out)
    code2, out2, _err = _cli(
        ["compile", str(path), "--profile", str(profile), "--no-route"], capsys
    )
    plain = json.loads(out2)
    assert code == code2 == 0
    # A compile's JSON keeps its Phase 9 keys; the route is shown by ``architect route``.
    assert "route" not in routed and "reused" not in routed and set(routed) == set(plain)
    code, out, _err = _cli(["roles", "--profile", str(profile)], capsys)
    assert code == 0 and out.startswith("roles policy pinned")
    assert "proxy_completed" in out and "the judge is part of the Spec" in out
    assert all(len(line) <= 100 for line in out.splitlines())
    # The header keeps the honesty label whole (the columns fit 100 exactly).
    assert out.splitlines()[1].rstrip().endswith("proxy_completed")
    code, out, _err = _cli(["roles", "--profile", str(profile), "--json"], capsys)
    roles = json.loads(out)
    assert code == 0 and roles["policy"] == "pinned"
    assert set(roles["roles"]) == {
        "inference", "decomposition", "rewrite", "forge_agent", "floor_draft"
    }
    # --json carries the table's evidence (Section 13.1), not only the policy knobs.
    entry = roles["roles"]["inference"][0]
    assert {"n", "p_lcb", "mean_cost_usd", "median_wall_s", "evidence_class",
            "proxy_completed"} <= set(entry)
    assert entry["evidence_class"] == "synthetic_fake" and entry["pinned_default"] is True
    assert set(entry["proxy_completed"]) == {"accepted", "runs"}
    assert isinstance(roles["judge"], list)


def test_cli_do_passes_grants_only_when_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from unittest.mock import AsyncMock

    from architect.orchestrator import OrchestrationResult

    run = AsyncMock(return_value=OrchestrationResult(accepted=True, tasks=()))
    monkeypatch.setattr(Orchestrator, "run", run)
    profile = make_profile(tmp_path)
    base = ["do", "goal", "--repo", str(tmp_path), "--profile", str(profile)]
    assert _cli(base, capsys)[0] == 0
    assert run.await_args.kwargs == {"repo": tmp_path.resolve()}
    assert _cli([*base, "--reuse-installed", "--no-route"], capsys)[0] == 0
    assert run.await_args.kwargs == {
        "repo": tmp_path.resolve(), "reuse_installed": True, "no_route": True
    }


def test_cli_forge_run_prints_the_agent_line_before_the_phase9_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    forge_repairs(monkeypatch)
    profile = make_profile(tmp_path, reply="nope")
    path = tmp_path / "spec.json"
    path.write_text(spec().model_dump_json(), encoding="utf-8")
    argv = ["forge", "run", str(path), "--profile", str(profile), "--input", '{"text": "hi"}',
            "--forge-mode", "template"]
    code, out, err = _cli(argv, capsys)
    assert code == 1 and json.loads(err.splitlines()[-1]) == {"error": "forge_cancelled"}
    assert out == ""
    code, out, err = _cli([*argv, "--yes"], capsys)
    assert code == 0, err
    # Every physical stderr line fits 100 ASCII columns (wrapped, never cut).
    lines = logical_lines(err)
    assert lines[0].startswith("forge agent: fake (mode template, turns ")
    assert lines[1].startswith("forge: compile cap $")
    assert lines[-1].startswith("forge run: licensed (held_out ")
    assert "build $" in lines[-1] and "compile $" in lines[-1] and "play $" in lines[-1]
    result = json.loads(out)
    assert result["codes"][0] == "forge_run_delivered" and result["licensed"] is True


def test_cli_forge_integrate_gates_and_export_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    forge_repairs(monkeypatch)
    profile = make_profile(tmp_path, reply="nope")
    repo = tmp_path / "repo"
    repo.mkdir()
    approved = tmp_path / "approved.json"
    approved.write_text(installed_spec().model_dump_json(), encoding="utf-8")
    common = ["--profile", str(profile), "--repo", str(repo), "--into", "pkgs",
              "--forge-mode", "template"]
    code, out, err = _cli(
        ["forge", "integrate", "Return ok", *common, "--approved-spec", str(approved),
         "--stage-only", "--yes"],
        capsys,
    )
    assert code == 0, err
    result = json.loads(out)
    assert result["export"]["placement"] == "staged" and result["licensed"] is True
    assert result["human_review_required"] is True and result["feature_flag_required"] is True
    assert list(repo.iterdir()) == []  # --stage-only writes nothing into the repository
    staged = Path(result["export"]["package_path"])
    assert (staged / "MANIFEST.json").is_file()
    code, out, err = _cli(["export", "verify", str(staged), "--profile", str(profile)], capsys)
    assert code == 0, err
    assert "export evidence (host-attested" in out and "codes -" in out
    code, out, _err = _cli(["export", "verify", str(staged), "--json"], capsys)
    assert code == 0 and json.loads(out)["check"]["integrity"] is True
    (staged / "README.md").write_text("tampered", encoding="utf-8")
    code, out, err = _cli(["export", "verify", str(staged)], capsys)
    assert code == 1
    assert json.loads(err.splitlines()[-1]) == {"error": "export_integrity_mismatch"}


# -------------------------------------------------------------------------- HTTP


def test_http_route_is_spec_only_content_free_advice(tmp_path: Path) -> None:
    import asyncio

    services = services_of(routed_profile(tmp_path))
    try:
        asyncio.run(install(services, "secret-name-xyz", installed_spec()))
        with TestClient(create_app(services, api_token=TOKEN)) as client:
            body = {"spec": installed_spec().model_dump(mode="json")}
            response = client.post("/route", json=body, headers=AUTH)
            assert response.status_code == 200
            payload = response.json()
            assert set(payload) == {
                "action", "artifact_id", "fit_tier", "fit", "quality_lcb",
                "expected_cost_usd", "latency_s", "latency_basis", "evidence", "codes",
            }
            assert payload["action"] != "reuse_installed" and payload["fit_tier"] in ("A", "B")
            assert payload["artifact_id"] is None
            assert "route_not_catalogued" in payload["codes"]
            assert "route_reuse_not_granted" in payload["codes"]
            assert "secret-name-xyz" not in response.text
            assert str(tmp_path) not in response.text and "Return ok" not in response.text
            for bad in ({"request": "prose please"}, {}):
                refused = client.post("/route", json=bad, headers=AUTH)
                assert refused.status_code == 400
                assert refused.json() == {"error": "route_spec_required"}
            unauthorized = client.post("/route", json=body)
            assert unauthorized.status_code == 401
            # Nothing ran and nothing was recorded by advice.
            assert not (services.workspace / "routes" / "decisions.jsonl").exists()
    finally:
        services.close()


# ----------------------------------------------------------------------- session


def _routing_session_profile(tmp_path: Path, **kw: Any) -> Path:
    profile = fake_profile(tmp_path, **kw)
    payload = json.loads(profile.read_text(encoding="utf-8"))
    payload["routing"] = {}
    profile.write_text(json.dumps(payload), encoding="utf-8")
    return profile


def test_session_route_roles_rate_export_and_the_automatic_route_line(
    tmp_path: Path,
) -> None:
    _routing_session_profile(tmp_path, lifecycle="install")
    spec_json = make_spec(lifecycle="install").model_dump_json()
    token = make_spec(lifecycle="install").spec_hash[:8]
    code, transcript = run_script(
        [
            "roles",
            f"spec {spec_json}",
            "route",
            "rate good",
            f"approve {token}",
            "rate good",
            "run {}",
            "rate good",
            f"install invoices {token}",
            "export",
            "again",
            f"reuse invoices {token} {{}}",
            "reuse invoices deadbeef {}",
            "quit",
        ],
        tmp_path,
    )
    assert code == 0
    assert "roles policy pinned" in transcript
    assert "route: compile (fit_none,route_compile)" in transcript  # the automatic line
    assert "Stage 4 alone decides every license" in transcript
    assert "error: rate_no_run" in transcript
    assert "rated good: run " in transcript
    assert "export staged:" in transcript and "feature flag OFF" in transcript
    assert "reuse invoices@1: installed and licensed on a fresh load" in transcript
    assert "error: stale_approval_token" in transcript
    assert "Traceback" not in transcript


def test_session_forge_modes_refuse_before_any_spend(tmp_path: Path) -> None:
    fake_profile(tmp_path, lifecycle="install")
    spec_json = make_spec(lifecycle="install").model_dump_json()
    code, transcript = run_script(
        [
            f"spec {spec_json}",
            'forge run {"text": "hi"}',
            "forge integrate . pkgs",
            "quit",
        ],
        tmp_path,
    )
    assert code == 0
    assert "error: forge_run_needs_expire" in transcript
    assert "next: set lifecycle expire" in transcript
    assert "error: approval_required" in transcript
    assert "forge agent:" not in transcript and "forge: compile cap" not in transcript


def test_session_forge_run_consents_then_delivers_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forge_repairs(monkeypatch)
    profile = fake_profile(tmp_path)
    payload = json.loads(profile.read_text(encoding="utf-8"))
    payload["floors"]["tasks"]["smoke"] = "cases.json"
    payload["compile"]["forge"] = {"mode": "template"}
    profile.write_text(json.dumps(payload), encoding="utf-8")
    spec_json = make_spec().model_dump_json()
    code, transcript = run_script(
        # ``json`` is held while the paid work runs (a scripted console), so the work is
        # never cancelled by the trailing quit.
        [f"spec {spec_json}", 'forge run {"text": "hi"}', "n", 'forge run {"text": "hi"}', "y",
         "json", "quit"],
        tmp_path,
    )
    assert code == 0
    agent = transcript.index("forge agent: fake (mode template")
    cap = transcript.index("forge: compile cap $", agent)
    assert agent < cap
    assert "error: forge_cancelled" in transcript
    assert "forge run codes forge_run_delivered" in transcript


# -------------------------------------------------------------------- invariants


def test_approval_is_written_only_where_phase7_says() -> None:
    """No ``approved=`` keyword outside ``Session._compile`` and no ``_approved_hash``
    assignment outside ``__init__`` and ``approve <hash8>`` in the session package."""
    session_dir = ROOT / "architect" / "session"
    keywords: list[tuple[str, str]] = []
    assigned: list[tuple[str, str]] = []
    for path in sorted(session_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(function):
                called = getattr(node.func, "attr", None) if isinstance(node, ast.Call) else None
                if called == "compile" and any(
                    keyword.arg == "approved" for keyword in node.keywords  # type: ignore[attr-defined]
                ):
                    keywords.append((path.name, function.name))
                if isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target, ast.Attribute) and target.attr == "_approved_hash":
                            assigned.append((path.name, function.name))
    assert sorted(set(keywords)) == [("session.py", "_compile")]
    assert len(keywords) == 1
    assert sorted(assigned) == [("session.py", "__init__"), ("session.py", "_approve")]
    source = (session_dir / "session.py").read_text(encoding="utf-8")
    # P3: the session's one compile routes with origin ``session`` (never the CLI's).
    assert source.count(
        'services.compiler.compile(spec, approved=self.approved(spec), origin="session")'
    ) == 1


def test_every_phase10_code_has_its_fixed_sentence() -> None:
    from architect.session.codes import CODES, explain

    contract = (ROOT / "docs" / "interfaces" / "phase10-router.md").read_text("utf-8")
    table = contract[contract.index("### 13.4"):contract.index("## 14.")]
    rows = re.findall(r"^\| `([a-z_]+)` \| (.+) \|$", table, re.MULTILINE)
    assert len(rows) >= 55
    for code, sentence in rows:
        assert code in CODES, code
        assert explain(code) == sentence, code
        assert sentence.isascii()


def test_route_line_digest_is_stable_for_a_fixed_decision() -> None:
    from architect.session.routes import route_line

    decision = RouteDecision.model_validate({
        "decision_id": "00000000-0000-0000-0000-000000000001", "origin": "session",
        "action": "warm_start",
        "target": {"key": "3fa2b1c9d0e1" + "0" * 52, "artifact_digest": "a" * 64,
                   "model": "fake"},
        "signature": {"slot": "0" * 32, "shape": "1" * 32, "family": "2" * 32,
                      "token_count": 2, "tokens": frozenset()},
        "candidates": [], "chosen_rank": None, "explored": False,
        "codes": ["fit_shape", "route_warm_start"], "proposals": [], "decision_ms": 0.1,
    })
    assert route_line(decision) == "route: warm start from 3fa2b1c9d0e1 (still verified)"
    digest = hashlib.sha256(route_line(decision).encode()).hexdigest()
    assert len(digest) == 64


def test_cli_compile_forge_consent_order_pinned_and_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pinned: the Phase 9 cap line alone, byte-identical. Evidence: the forge agent line
    is chosen and printed immediately before the unchanged Phase 9 line (11.3)."""
    path = tmp_path / "spec.json"
    path.write_text(spec().model_dump_json(), encoding="utf-8")
    lines = {}
    for policy in ("pinned", "evidence"):
        root = tmp_path / policy
        root.mkdir()
        profile = make_profile(root, roles={"policy": policy})
        code, out, err = _cli(
            ["compile", str(path), "--profile", str(profile), "--forge", "--forge-mode",
             "template"],
            capsys,
        )
        assert code == 1 and out == ""
        assert json.loads(err.splitlines()[-1]) == {"error": "forge_cancelled"}
        lines[policy] = err.splitlines()[:-1]
    assert len(lines["pinned"]) == 1 and lines["pinned"][0].startswith("forge: compile cap $")
    assert lines["evidence"][0] == "forge agent: fake (mode template, turns 200)"
    assert lines["evidence"][1] == lines["pinned"][0]


async def test_child_warm_start_and_subtask_models_are_opt_in(tmp_path: Path) -> None:
    from architect.library import ReadOnlyLibrary

    services = services_of(routed_profile(tmp_path))
    try:
        compiler = services.compiler
        assert compiler._child_library() is None  # default: Forge children get NullLibrary
        assert compiler._subtask_model(spec()) == spec()  # default: the planned model stays
        router = compiler.router
        assert router is not None
        router.policy = RoutePolicy(child_warm_start=True, subtask_models=True)
        child = compiler._child_library()
        assert isinstance(child, ReadOnlyLibrary)
        assert not hasattr(child, "store") and not hasattr(child, "record_verification")
        # With no eligible Tier A or B version the planned model is kept.
        assert compiler._subtask_model(spec()) == spec()
    finally:
        services.close()


def test_cli_route_prose_origin_and_more_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    forge_repairs(monkeypatch)
    reply = {"text": spec().model_dump_json(), "model": "fake", "tokens_in": 5,
             "tokens_out": 5, "cost_usd": 0.0001}
    (tmp_path / "infer.json").write_text(json.dumps([reply] * 8), encoding="utf-8")
    profile = routed_profile(
        tmp_path, reply="nope",
        inference={"provider": "fake", "model": "fake", "fixture": "infer.json"},
    )
    # Prose runs Stage 0 first and says what it cost; the table still goes to stdout.
    code, out, err = _cli(
        ["route", "Return ok", "--profile", str(profile), "--origin", "orchestrator",
         "--json"],
        capsys,
    )
    assert code == 0 and err.startswith("route: stage 0 cost $0.0001 (API-equivalent)")
    assert RouteDecision.model_validate_json(out).origin == "orchestrator"
    # forge integrate without an approved Spec: Stage 0 and the proposal only, no Forge line.
    repo = tmp_path / "repo"
    repo.mkdir()
    code, out, err = _cli(
        ["forge", "integrate", "Return ok", "--profile", str(profile), "--repo", str(repo),
         "--into", "pkgs"],
        capsys,
    )
    proposal = json.loads(out)
    assert code == 0 and proposal["approval_required"] is True
    assert Path(proposal["proposal_path"]).is_file()
    assert "forge agent:" not in err and "forge: compile cap" not in err
    assert list(repo.iterdir()) == []
    # --package-only places exactly one new directory, then re-hashes it.
    approved = tmp_path / "approved.json"
    approved.write_text(installed_spec().model_dump_json(), encoding="utf-8")
    code, out, err = _cli(
        ["forge", "integrate", "Return ok", "--profile", str(profile), "--repo", str(repo),
         "--into", "pkgs", "--approved-spec", str(approved), "--package-only", "--yes",
         "--forge-mode", "template"],
        capsys,
    )
    assert code == 0, err
    placed = json.loads(out)
    assert placed["export"]["placement"] == "host"
    assert [path.name for path in repo.iterdir()] == ["pkgs"]
    package = Path(placed["export"]["package_path"])
    assert any(
        line.startswith("export: ") and line.endswith("feature flag OFF; human review required")
        for line in logical_lines(err)
    )
    # export verify --reverify: a fresh host-local Stage 4 on a private copy.
    before = sorted(path.relative_to(package).as_posix() for path in package.rglob("*"))
    code, out, err = _cli(
        ["export", "verify", str(package), "--profile", str(profile), "--reverify", "--yes",
         "--json"],
        capsys,
    )
    assert code == 0, err
    assert err.startswith("reverify: Stage 4 on held_out_tasks; cap $")
    assert json.loads(out)["reverify"]["licensed"] is True
    after = sorted(path.relative_to(package).as_posix() for path in package.rglob("*"))
    assert before == after  # the package itself was never written
    # recompile --no-route parses and behaves as Phase 9: a tie never replaces the entry.
    code, out, err = _cli(
        ["recompile", placed["name"], "--profile", str(profile), "--no-route"], capsys
    )
    assert code == 1 and json.loads(err.splitlines()[-1]) == {"error": "registry_rejected"}
