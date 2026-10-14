"""Phase 10 WP5 review fixes: one regression test per reviewed defect.

Offline only: the fake provider, blocked sockets, Forge in template mode and a fake Pi
peer (``_fake_pi``) that calls the real ``compile_agent`` and ``place_harness_package``
capabilities the way a builder would. No live provider, no ``claude``/``codex`` CLI and
no Ollama is ever called.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from forge_modes_support import (
    TEMPLATE,
    Consent,
    block_sockets,
    case_rows,
    forge_repairs,
    logical_lines,
    make_profile,
    spec,
)
from session_support import dense, fake_profile, make_spec, response, run_script, unwrap

import architect.cli as cli
from architect.compiler import CompileConfig, Compiler
from architect.floors import EvaluationCase, HeldOutTasks
from architect.forge_modes import IntegrateResult, forge_integrate, forge_run
from architect.forge_modes.export import _canonical, read_manifest
from architect.library import HarnessLibrary
from architect.models import FakeAdapter, ModelResponse
from architect.orchestrator import OrchestrationResult, Orchestrator
from architect.orchestrator.features import FeatureBuilder
from architect.profiles import Services, build_services, load_profile
from architect.registry import Registry
from architect.routing.evidence import EvidenceIndex
from architect.routing.log import RouteLog
from architect.routing.router import RoutePolicy, Router
from architect.runtime import Runtime
from architect.spec import FloorDefinition, HarnessArtifact, Lifecycle, Spec
from architect.telemetry import TelemetryStore

ROOT = Path(__file__).resolve().parents[1]
BUILDER_FLOOR = {"kind": "held_out_tasks", "limit": 1, "dataset": "tasks"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    block_sockets(monkeypatch)


# ----------------------------------------------------------------------- helpers


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


def records(workspace: Path, name: str) -> list[dict[str, Any]]:
    path = workspace / "routes" / name
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line]


def session_profile(tmp_path: Path, **changes: Any) -> Path:
    """``fake_profile`` plus top-level changes (``routing``, ``roles``, ``builder_floor``)
    and, with ``forge=True``, a smoke floor and template-mode Forge."""
    forge = changes.pop("forge", False)
    kw = {key: changes.pop(key) for key in ("lifecycle", "held_out_expected") if key in changes}
    profile = fake_profile(tmp_path, **kw)
    payload = json.loads(profile.read_text(encoding="utf-8"))
    if forge:
        payload["floors"]["tasks"]["smoke"] = "cases.json"
        payload["compile"]["forge"] = {"mode": "template"}
    payload.update(changes)
    profile.write_text(json.dumps(payload), encoding="utf-8")
    return profile


def _decode_after(goal: str, marker: str) -> Any:
    start = goal.index(marker) + len(marker)
    return json.JSONDecoder().raw_decode(goal, start)[0]


def _fake_pi(
    monkeypatch: pytest.MonkeyPatch,
    *,
    after: Callable[[Path, str], None] | None = None,
) -> list[Any]:
    """A fake builder: the real capabilities of the real ``PiBackend``, called in order.

    It calls ``compile_agent`` with the exact request and, when offered,
    ``place_harness_package`` with the operator's ``into``; ``after`` then runs as the
    builder's own later writes (it keeps repository write access)."""
    replies: list[Any] = []
    captured: dict[str, Any] = {}
    original = Runtime.use_pi_backend

    def capture(self: Runtime, backend: Any) -> Any:
        captured["backend"] = backend
        return original(self, backend)

    async def run(self: Orchestrator, goal: str, *, repo: Path | None = None,
                  overrides: Any = None, **kw: Any) -> OrchestrationResult:
        backend = captured["backend"]
        request = _decode_after(goal, "Feature request: ")
        replies.append(await backend.compiler_tool({"request": request}))
        tool = backend.capabilities.get("place_harness_package")
        if tool is not None:
            into = _decode_after(goal, "into equal to ")
            replies.append(await tool.callback({"into": into}))
            if after is not None and repo is not None:
                after(repo, into)
        return OrchestrationResult(accepted=True, tasks=())

    monkeypatch.setattr(Runtime, "use_pi_backend", capture)
    monkeypatch.setattr(Orchestrator, "run", run)
    return replies


def _repack(repo: Path, into: str) -> None:
    """A builder that rewrites a placed file and a self-consistent MANIFEST.json."""
    [package] = [path for path in (repo / into).iterdir() if path.is_dir()]
    readme = package / "README.md"
    readme.write_text("rewritten by the builder\n", encoding="utf-8")
    manifest, _ = read_manifest(package)
    digest = hashlib.sha256(readme.read_bytes()).hexdigest()
    files = tuple(
        (name, digest if name == "README.md" else sha) for name, sha in manifest.files
    )
    repacked = manifest.model_copy(update={"files": files})
    (package / "MANIFEST.json").write_bytes(_canonical(repacked.model_dump(mode="json")))


def _cli(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ------------------------------------------------ P3: the session origin and reuse


def test_session_compiles_route_as_session_so_a_cli_grant_does_not_reach_it(
    tmp_path: Path,
) -> None:
    session_profile(tmp_path, lifecycle="install", routing={"reuse_origins": ["cli"]})
    spec_json = make_spec(lifecycle="install").model_dump_json()
    token = make_spec(lifecycle="install").spec_hash[:8]
    code, transcript = run_script(
        [f"spec {spec_json}", f"approve {token}", f"install invoices {token}", "again",
         f"approve {token}", "quit"],
        tmp_path,
    )
    assert code == 0 and "Traceback" not in transcript
    # Both compiles ran Stage 4: the CLI's standing grant never reuses for the session.
    assert transcript.count("stages 1,2,4") == 2 and "stages -" not in transcript
    origins = [row["origin"] for row in records(tmp_path / "w", "decisions.jsonl")]
    assert origins and set(origins) == {"session"}


def test_a_session_grant_reuses_and_the_reused_entry_is_never_installed_again(
    tmp_path: Path,
) -> None:
    session_profile(tmp_path, lifecycle="install", routing={"reuse_origins": ["session"]})
    spec_json = make_spec(lifecycle="install").model_dump_json()
    token = make_spec(lifecycle="install").spec_hash[:8]
    code, transcript = run_script(
        [f"spec {spec_json}", f"approve {token}", f"install invoices {token}", "again",
         f"approve {token}", f"install second {token}", "export", "list", "quit"],
        tmp_path,
    )
    assert code == 0 and "Traceback" not in transcript
    assert "stages -" in transcript  # the second compile reused: no Stage 1 to 4
    text = unwrap(transcript)
    assert "reused     installed invoices@1, licensed on a fresh load" in text
    assert "routing only chose it" in text
    assert "error: route_reuse_installed" in transcript
    assert "it is already installed as invoices@1" in text
    assert "installed second" not in transcript
    assert "export staged:" in transcript  # the reused entry is the last entry
    registry = Registry(tmp_path / "w" / "registry")
    assert [entry.name for entry in registry.list()] == ["invoices"]


async def test_feature_builder_never_reinstalls_a_routed_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_pi(monkeypatch)
    services = services_of(make_profile(
        tmp_path, builder_floor=BUILDER_FLOOR, routing={"reuse_origins": ["builder"]}
    ))
    repo = tmp_path / "repo"
    repo.mkdir()
    try:
        approved = installed_spec()
        await install(services, "invoices", approved)
        result = await FeatureBuilder(services).create("Return ok", repo, approved_spec=approved)
        feature = result.tasks[-1].output
        assert result.accepted, result
        assert isinstance(feature, dict) and feature["name"] == "invoices"
        assert feature["code"] == "route_reuse_installed" and feature["revision"] == 1
        # No new registry entry: the routed reuse is the installed entry itself.
        assert [entry.name for entry in services.registry.list()] == ["invoices"]
    finally:
        services.close()


# --------------------------------------------- P3: the profile grant is honoured


def _floor(definition: FloorDefinition) -> HeldOutTasks:
    return HeldOutTasks(
        definition,
        [EvaluationCase(case_id=f"c{i}", task={"action": i}, expected="ok") for i in range(4)],
    )


def _response(text: str) -> ModelResponse:
    return ModelResponse(text=text, tokens_in=10, tokens_out=10, cost_usd=2e-5, model="fake")


_SUBTASK = {
    "task": "Summarize numbers", "input_schema": {}, "output_schema": {"type": "string"},
    "floor": {"kind": "held_out_tasks", "limit": 1.0, "dataset": "ok-cases"},
    "budget": {"cost_usd": 0.1, "compute_s": 60}, "model_target": "fake",
    "lifecycle": "expire", "domain": "synthesizer", "tools": [], "pipeline": [],
}
_PLAN = json.dumps({"tasks": [{
    "id": "one", "request": "Summarize the numbers in one sentence",
    "input": {"numbers": [1, 2]}, "domain": "synthesizer", "model": "fake",
    "budget": {"cost_usd": 0.1, "compute_s": 60}, "tools": [], "depends_on": [],
}]})


async def test_orchestrator_delivers_under_the_profile_reuse_grant(tmp_path: Path) -> None:
    store = TelemetryStore(tmp_path / "telemetry")
    runtime = Runtime(store)
    library = HarnessLibrary(tmp_path / "library")
    registry = Registry(tmp_path / "registry")
    inference = FakeAdapter(responses=[_response(json.dumps(_SUBTASK))] * 3)
    compiler = Compiler(
        tmp_path / "artifacts", runtime, lambda model: FakeAdapter(model=model), _floor,
        config=CompileConfig(max_candidates=1, enable_rewriting=False),
        library=library, inference_adapter=inference,
    )
    log = RouteLog(tmp_path / "routes")
    index = EvidenceIndex(registry, library, store, log, tmp_path,
                          providers={"fake": "fake"}, promotion_reuses=3)
    host: Any = {"output_schema": {"type": "string"}}
    try:
        compiler.router = Router(index, RoutePolicy(), log)
        decomposition = FakeAdapter(responses=[_response(_PLAN)] * 2)
        orchestrator = Orchestrator(compiler, decomposition)
        first = await orchestrator.run("summarize", overrides=host)
        directory = first.tasks[0].artifact_directory
        assert first.accepted and directory is not None
        compiled = Spec.model_validate_json((directory / "spec.json").read_text("utf-8"))
        approved = compiled.model_copy(update={"lifecycle": Lifecycle.INSTALL})
        result = await compiler.compile(approved, approved=True, no_route=True)
        assert result.artifact is not None and result.artifact.licensed
        registry.install("summary", result.artifact)
        entry = registry.get("summary")
        # A standing profile grant for the orchestrator origin, and no per-call grant.
        compiler.router = Router(
            index, RoutePolicy(reuse_origins=frozenset({"orchestrator"})), log
        )
        routed = await orchestrator.run("summarize", overrides=host)
        assert routed.accepted, routed
        task = routed.tasks[0]
        assert task.output == "ok" and task.artifact_directory == entry.directory
        assert (entry.directory / "harness.py").is_file()
    finally:
        store.close()


async def test_forge_run_and_integrate_honour_the_profile_reuse_grant(tmp_path: Path) -> None:
    services = services_of(make_profile(
        tmp_path, routing={"reuse_origins": ["forge_run", "forge_integrate"]}
    ))
    repo = tmp_path / "repo"
    repo.mkdir()
    try:
        approved = installed_spec()
        await install(services, "feature-" + approved.spec_hash[:16], approved)
        rows = len(services.runtime.store.rows())
        ran = await forge_run(spec(), {"text": "hi"}, services, TEMPLATE, consent=Consent())
        assert ran.codes[0] == "forge_run_reused_installed" and ran.reused and ran.licensed
        integrated = await forge_integrate(
            "Return ok", repo, services, TEMPLATE, into="src", package="pkg",
            approved_spec=approved, consent=Consent(), package_only=True,
        )
        assert isinstance(integrated, IntegrateResult)
        assert integrated.codes == ("integrate_reused_installed",)
        # One delivery run (the forge run) and no build at all.
        assert len(services.runtime.store.rows()) == rows + 1
        assert len(services.registry.list()) == 1
    finally:
        services.close()


# ------------------------------------------------ the Forge-mode role choice


def test_session_forge_run_chooses_the_forge_agent_role_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forge_repairs(monkeypatch)
    session_profile(
        tmp_path, forge=True,
        forge_agent={"provider": "fake", "model": "fake"},
        roles={"policy": "evidence", "explore_every": 2, "candidates": {
            "forge_agent": [{"model": "fake", "params": {"mode": "template",
                                                         "pi_max_turns": 100}}]}},
    )
    spec_json = make_spec().model_dump_json()
    code, transcript = run_script(
        [f"spec {spec_json}", 'forge run {"text": "hi"}', "y", "json",
         'forge run {"text": "hi"}', "y", "json", "quit"],
        tmp_path,
    )
    assert code == 0 and "Traceback" not in transcript
    assert "forge_cancelled" not in transcript
    assert transcript.count("forge run codes forge_run_delivered") == 2
    roles = [row for row in records(tmp_path / "w", "roles.jsonl")
             if row["role"] == "forge_agent"]
    decisions = [row for row in roles if row["outcome"] is None]
    assert len(decisions) == 2  # one choice per forge run, none for a preview
    assert all(row["outcome"] != "excluded" for row in roles)
    # The second run explored: the consented line named the variant that ran.
    assert "forge agent: fake (mode template, turns 100)" in transcript


def test_cli_compile_forge_agent_choice_gets_its_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    forge_repairs(monkeypatch)
    profile = make_profile(
        tmp_path, reply="nope", roles={"policy": "evidence"},
        forge_agent={"provider": "fake", "model": "fake"},
    )
    path = tmp_path / "spec.json"
    path.write_text(spec().model_dump_json(), encoding="utf-8")
    code, _out, err = _cli(
        ["compile", str(path), "--profile", str(profile), "--forge", "--forge-mode",
         "template", "--yes"],
        capsys,
    )
    assert code == 0, err
    roles = [row for row in records(tmp_path / "work", "roles.jsonl")
             if row["role"] == "forge_agent"]
    assert [row["outcome"] for row in roles] == [None, "success"]
    assert roles[0]["decision_id"] == roles[1]["decision_id"]


def test_session_compile_forge_agent_choice_gets_its_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forge_repairs(monkeypatch)
    profile = session_profile(tmp_path, forge=True, roles={"policy": "evidence"},
                              forge_agent={"provider": "fake", "model": "fake"})
    payload = json.loads(profile.read_text(encoding="utf-8"))
    payload["compile"]["forge"]["trigger"] = "always"
    profile.write_text(json.dumps(payload), encoding="utf-8")
    # Play replies of "nope" fail Stage 2, so the Forge submission is the accepted one.
    (tmp_path / "w" / "responses.json").write_text(
        json.dumps([response("nope") for _ in range(400)]), encoding="utf-8"
    )
    spec_json = make_spec().model_dump_json()
    code, transcript = run_script(
        [f"spec {spec_json}", "forge on", "y", "compile", "json", "quit"], tmp_path
    )
    assert code == 0 and "Traceback" not in transcript
    roles = [row for row in records(tmp_path / "w", "roles.jsonl")
             if row["role"] == "forge_agent"]
    assert [row["outcome"] for row in roles] == [None, "success"]


# ---------------------------------------------------------------- CLI surfaces


def test_cli_forge_run_with_cases_and_reuse_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    forge_repairs(monkeypatch)
    profile = make_profile(tmp_path, reply="nope", routing={})
    path = tmp_path / "spec.json"
    path.write_text(spec().model_dump_json(), encoding="utf-8")
    cases = tmp_path / "authored.jsonl"
    cases.write_text("\n".join(json.dumps(row) for row in case_rows(8, prefix="u")) + "\n",
                     encoding="utf-8")
    base = ["forge", "run", str(path), "--profile", str(profile), "--input", '{"text": "hi"}',
            "--forge-mode", "template", "--yes"]
    code, out, err = _cli([*base, "--cases", str(cases)], capsys)
    assert code == 0, err
    result = json.loads(out)
    assert result["codes"] == ["forge_run_delivered"]
    assert result["floor_origin"] == "authored_cases"
    services = services_of(profile)
    try:
        asyncio.run(install(services, "invoices", installed_spec()))
    finally:
        services.close()
    code, out, err = _cli([*base, "--reuse-installed"], capsys)
    assert code == 0, err
    reused = json.loads(out)
    assert reused["codes"][0] == "forge_run_reused_installed" and reused["reused"] is True
    assert reused["build_cost_usd"] == 0.0 and reused["output"] == "ok"
    assert logical_lines(err)[-1].startswith("forge run: reused installed (held_out ")


def test_cli_forge_integrate_pi_placement_package_name_reuse_and_costs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    forge_repairs(monkeypatch)
    replies = _fake_pi(monkeypatch)
    profile = make_profile(tmp_path, reply="nope", routing={}, builder_floor=BUILDER_FLOOR)
    repo = tmp_path / "repo"
    repo.mkdir()
    approved = tmp_path / "approved.json"
    approved.write_text(installed_spec().model_dump_json(), encoding="utf-8")
    common = ["forge", "integrate", "Return ok", "--profile", str(profile), "--repo", str(repo),
              "--approved-spec", str(approved), "--forge-mode", "template", "--yes"]
    # Default placement: the guarded FeatureBuilder/Pi path with the single-use tool.
    code, out, err = _cli([*common, "--into", "src", "--package", "invoices_pkg"], capsys)
    assert code == 0, err
    result = json.loads(out)
    assert result["export"]["placement"] == "pi" and result["codes"] == []
    assert (repo / "src" / "invoices_pkg" / "MANIFEST.json").is_file()
    assert replies[0]["code"] == "integrate_reused_installed"
    assert replies[1]["path"] == "src/invoices_pkg"
    # Honesty rule 8: build and compile cost, separately, on stderr and in the JSON.
    lines = logical_lines(err)
    assert any(line.startswith("forge integrate: licensed and installed as feature-")
               and "build $" in line and "compile $" in line and "play $0.0000" in line
               for line in lines)
    assert result["build_cost_usd"] >= 0 and result["compile_cost_usd"] > 0
    # --reuse-installed: no build, the installed entry is exported and placed again.
    code, out, err = _cli(
        [*common, "--into", "lib", "--package-only", "--reuse-installed"], capsys
    )
    assert code == 0, err
    again = json.loads(out)
    assert again["codes"] == ["integrate_reused_installed"]
    assert again["name"] == result["name"] and again["compile_cost_usd"] == 0.0
    assert any(line.startswith("forge integrate: reused installed feature-")
               for line in logical_lines(err))


def test_cli_create_no_route_and_do_reuse_installed_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "plan.json").write_text(json.dumps([{
        "text": _PLAN, "model": "fake", "tokens_in": 5, "tokens_out": 5, "cost_usd": 1e-5,
    }] * 16), encoding="utf-8")
    (tmp_path / "infer.json").write_text(json.dumps([{
        "text": json.dumps(_SUBTASK), "model": "fake", "tokens_in": 5, "tokens_out": 5,
        "cost_usd": 1e-5,
    }] * 16), encoding="utf-8")
    (tmp_path / "ok.json").write_text(json.dumps([
        {"case_id": f"c{i}", "task": {"action": i}, "expected": "ok"} for i in range(4)
    ]), encoding="utf-8")
    profile = make_profile(
        tmp_path, routing={}, builder_floor=BUILDER_FLOOR,
        decomposition={"provider": "fake", "model": "fake", "fixture": "plan.json"},
        inference={"provider": "fake", "model": "fake", "fixture": "infer.json"},
    )
    payload = json.loads(profile.read_text(encoding="utf-8"))
    payload["floors"]["ok-cases"] = {"kind": "held_out_tasks", "path": "ok.json"}
    profile.write_text(json.dumps(payload), encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = tmp_path / "work"
    # create --no-route: the builder's compile runs exactly as without a router.
    _fake_pi(monkeypatch)
    approved = tmp_path / "approved.json"
    approved.write_text(installed_spec().model_dump_json(), encoding="utf-8")
    code, out, err = _cli(
        ["create", "Return ok", "--profile", str(profile), "--repo", str(repo),
         "--approved-spec", str(approved), "--no-route"],
        capsys,
    )
    assert code == 0, err
    assert json.loads(out)["accepted"] is True
    assert records(workspace, "decisions.jsonl") == []
    monkeypatch.undo()
    block_sockets(monkeypatch)
    # do: compile and deliver once, then install the compiled subtask's Spec.
    do = ["do", "summarize", "--profile", str(profile), "--repo", str(repo)]
    code, out, err = _cli(do, capsys)
    assert code == 0, err
    directory = Path(json.loads(out)["tasks"][0]["artifact_directory"])
    compiled = Spec.model_validate_json((directory / "spec.json").read_text("utf-8"))
    services = services_of(profile)
    try:
        entry = asyncio.run(install(
            services, "summary", compiled.model_copy(update={"lifecycle": Lifecycle.INSTALL})
        ))
    finally:
        services.close()
    # do --reuse-installed: the installed entry is delivered and survives.
    code, out, err = _cli([*do, "--reuse-installed"], capsys)
    assert code == 0, err
    task = json.loads(out)["tasks"][0]
    assert task["output"] == "ok" and Path(task["artifact_directory"]) == entry.directory
    assert (entry.directory / "harness.py").is_file()
    assert not (entry.directory / ".consumed").exists()
    # The profile's standing grant does the same without the flag.
    payload["routing"] = {"reuse_origins": ["orchestrator"]}
    profile.write_text(json.dumps(payload), encoding="utf-8")
    code, out, err = _cli(do, capsys)
    assert code == 0, err
    assert Path(json.loads(out)["tasks"][0]["artifact_directory"]) == entry.directory


# ------------------------------------------------------- session forge integrate


def _integrate_profile(tmp_path: Path) -> None:
    session_profile(tmp_path, lifecycle="install", forge=True, builder_floor=BUILDER_FLOOR)
    (tmp_path / "repo").mkdir()


def test_session_forge_integrate_end_to_end_places_behind_both_consents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forge_repairs(monkeypatch)
    replies = _fake_pi(monkeypatch)
    _integrate_profile(tmp_path)
    target = make_spec(lifecycle="install")
    token = target.spec_hash[:8]
    code, transcript = run_script(
        [f"spec {target.model_dump_json()}", "forge integrate repo pkgs", f"approve {token}",
         "forge integrate repo pkgs", "again", "forge integrate repo pkgs", "y", "y",
         # Held while the paid work runs (a scripted console), so it cancels nothing.
         "json", "quit"],
        tmp_path,
    )
    assert code == 0 and "Traceback" not in transcript
    text = unwrap(transcript)
    # Approval first, and the hint says approve compiles; RESULT points back to REVIEW.
    assert f"next: approve {token} (runs a normal paid compile), then again" in text
    assert "next: again, then forge run {json} | forge integrate <repo> <into>" in text
    # Spend consent (agent line, Phase 9 line), the staged build and its cost, then the
    # export consent; only then the FeatureBuilder/Pi placement.
    agent = transcript.index("forge agent: fake (mode template")
    cap = transcript.index("forge: compile cap $", agent)
    staged = transcript.index("export staged:", cap)
    cost = text.index("forge integrate: licensed and installed as feature-")
    export = transcript.index("export: architect_", staged)
    assert agent < cap < staged and text.index("export: architect_") > cost
    assert export > staged
    assert "integrate: placed pkgs/architect_" in text and "integrate codes -" in text
    assert replies[0]["code"] == "integrate_reused_installed"
    package = tmp_path / "repo" / "pkgs" / ("architect_" + target.spec_hash[:8])
    assert (package / "MANIFEST.json").is_file()


def test_session_forge_integrate_rejects_a_placed_package_repacked_by_the_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forge_repairs(monkeypatch)
    _fake_pi(monkeypatch, after=_repack)
    _integrate_profile(tmp_path)
    target = make_spec(lifecycle="install")
    token = target.spec_hash[:8]
    code, transcript = run_script(
        [f"spec {target.model_dump_json()}", f"approve {token}", "again",
         "forge integrate repo pkgs", "y", "y", "json", "quit"],
        tmp_path,
    )
    assert code == 0 and "Traceback" not in transcript
    # The repacked manifest is self-consistent, but not the one the operator consented to.
    assert "integrate codes export_integrity_mismatch" in unwrap(transcript)
    package = tmp_path / "repo" / "pkgs" / ("architect_" + target.spec_hash[:8])
    assert (package / "README.md").read_text("utf-8") == "rewritten by the builder\n"


def test_session_forge_integrate_export_cancel_keeps_the_entry_and_says_where(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forge_repairs(monkeypatch)
    replies = _fake_pi(monkeypatch)
    _integrate_profile(tmp_path)
    target = make_spec(lifecycle="install")
    token = target.spec_hash[:8]
    code, transcript = run_script(
        [f"spec {target.model_dump_json()}", f"approve {token}", "again",
         "forge integrate repo pkgs", "n", "forge integrate repo pkgs", "y", "n", "list",
         "quit"],
        tmp_path,
    )
    assert code == 0 and "Traceback" not in transcript
    text = unwrap(transcript)
    assert "next: forge integrate <repo> <into>, and answer y" in text  # spend refused
    assert "error: export_cancelled" in transcript
    assert f"feature-{target.spec_hash[:16]}@1 stays installed and licensed" in text
    assert "the package stays staged at" in text
    assert "install <name> <hash8>, then export" not in text
    assert "--reuse-installed" in text
    assert replies == []  # nothing was placed and the builder never ran
    assert list((tmp_path / "repo").iterdir()) == []
    # The spend footer follows the cancel.
    after = transcript[transcript.index("error: export_cancelled"):]
    assert "spend  session $" in after.split("> list")[0]


# ------------------------------------------------------------ session advice screens


def test_session_route_names_the_installed_match_and_explains_every_code(
    tmp_path: Path,
) -> None:
    session_profile(tmp_path, lifecycle="install", routing={})
    target = make_spec(lifecycle="install")
    token = target.spec_hash[:8]
    code, transcript = run_script(
        [f"spec {target.model_dump_json()}", f"approve {token}", f"install invoices {token}",
         "again", "route", "quit"],
        tmp_path,
    )
    assert code == 0 and "Traceback" not in transcript
    shown = transcript[transcript.index("> route"):].splitlines()[1:]
    assert shown and all(len(line) <= 100 and line.isascii() for line in shown)
    text = unwrap(transcript)
    # The automatic REVIEW line does not hide the exact installed match.
    review = text[text.index("again"):text.index("> route")]
    assert "installed match invoices@1 exists" in review
    assert f"next: reuse invoices {token} {{json}}" in review
    # ``route``: every code with its fixed sentence, and the whole code list (no cut).
    screen = text[text.index("> route"):]
    assert ("route_reuse_not_granted: An exact installed match exists, but reuse was not "
            "granted for this request.") in screen
    services = services_of(tmp_path / "w" / "session-profile.json")
    try:
        from architect.session.routes import advice

        decision = advice(services, target, origin="session")
    finally:
        services.close()
    assert len(decision.codes) >= 3
    assert dense("(" + ",".join(decision.codes) + ")") in dense(screen)


def test_route_forge_suggestion_is_explained_on_both_surfaces(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = session_profile(tmp_path, held_out_expected="bad", routing={})
    target = make_spec()
    code, transcript = run_script(
        [f"spec {target.model_dump_json()}", "compile", "again", "quit"], tmp_path
    )
    assert code == 0 and "licensed: NO" in transcript
    text = unwrap(transcript)
    after = text[text.rindex("> again"):]
    assert "route_forge_suggested: Earlier compiles of this task failed Stage 4" in after
    assert "next: forge on, then compile | forge run {json}" in after  # an expire Spec
    path = tmp_path / "spec.json"
    path.write_text(target.model_dump_json(), encoding="utf-8")
    code, out, _err = _cli(["route", str(path), "--profile", str(profile)], capsys)
    assert code == 0 and all(len(line) <= 100 for line in out.splitlines())
    assert "rerun with --forge to let Forge try." in unwrap(out)
    assert "next: rerun the compile with --forge" in unwrap(out)


def test_session_forge_run_lines_hints_and_rating_are_honest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forge_repairs(monkeypatch)
    session_profile(tmp_path, forge=True)
    spec_json = make_spec().model_dump_json()
    code, transcript = run_script(
        [f"spec {spec_json}", "rate good", 'forge run {"text": "hi"}', "n",
         'forge run {"text": "hi"}', "y", "json", "rate good", "quit"],
        tmp_path,
    )
    assert code == 0 and "Traceback" not in transcript
    text = unwrap(transcript)
    assert "error: rate_no_run" in transcript and "error: no_result" not in transcript
    assert "next: forge run {json}, and answer y" in text
    assert "compile again and answer y, or forge off" not in text
    assert "forge_run_delivered; delivered once; Stage 4 licensed it" in text
    assert "rated good: run " in transcript  # the forge run delivery, rated in REVIEW
    ratings = records(tmp_path / "w", "ratings.jsonl")
    assert len(ratings) == 1 and ratings[0]["rating"] == "good"


def test_session_forge_run_that_stage4_refuses_never_claims_a_license(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forge_repairs(monkeypatch)
    session_profile(tmp_path, forge=True, held_out_expected="bad")
    code, transcript = run_script(
        [f"spec {make_spec().model_dump_json()}", 'forge run {"text": "hi"}', "y", "json",
         "rate good", "quit"],
        tmp_path,
    )
    assert code == 0 and "Traceback" not in transcript
    text = unwrap(transcript)
    assert "forge run codes forge_run_unlicensed" in text
    assert "Stage 4 licensed it" not in text
    assert "not delivered: Stage 4 did not license a harness, so nothing was run" in text
    assert "error: rate_no_run" in transcript


def test_session_reuse_with_the_wrong_name_is_explained_and_not_logged(
    tmp_path: Path,
) -> None:
    session_profile(tmp_path, lifecycle="install", routing={})
    target = make_spec(lifecycle="install")
    token = target.spec_hash[:8]
    code, transcript = run_script(
        [f"spec {target.model_dump_json()}", f"approve {token}", f"install invoices {token}",
         "again", f"reuse other {token} {{}}", f"reuse invoices {token} {{}}", "rate good",
         "quit"],
        tmp_path,
    )
    assert code == 0 and "Traceback" not in transcript
    text = unwrap(transcript)
    assert "error: unknown_name" in transcript
    assert f"next: reuse invoices {token} {{json}}" in text
    assert "route: reuse_installed -> invoices@1" not in text.split("reuse invoices")[0]
    assert "reuse invoices@1: installed and licensed on a fresh load" in text
    assert "rated good: run " in transcript  # the reuse delivery, rated in REVIEW
    # Only the executed grant is a logged decision (plus the approve compile's own).
    granted = [row for row in records(tmp_path / "w", "decisions.jsonl")
               if row["action"] == "reuse_installed"]
    assert len(granted) == 1


# ------------------------------------------------------------------ the doc amendment


def test_phase7_amendment_states_what_changed_for_the_default_profile() -> None:
    text = (ROOT / "docs" / "interfaces" / "phase7-session.md").read_text("utf-8")
    amendment = text[text.index("## 16. Phase 10 amendment"):]
    assert "unchanged except for the help lines" not in amendment
    assert "allowed here:" in amendment and "forge run {json}" in amendment
    assert "READY, REVIEW, RESULT | `rate good" in amendment
    assert "READY, REVIEW and RESULT help texts" in amendment
