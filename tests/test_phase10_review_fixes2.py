"""Phase 10 WP5 review fixes, second round: one regression test per reviewed defect.

Offline only: the fake provider, blocked sockets and Forge in template mode. No live
provider, no ``claude``/``codex`` CLI and no Ollama is ever called.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from forge_modes_support import block_sockets, forge_repairs, make_profile, spec
from session_support import fake_profile, make_spec, run_script, unwrap

import architect.cli as cli
from architect.profiles import Services, build_services, load_profile
from architect.session.commands import parse
from architect.session.routes import PLUMBING_LINE, STAGE4_LINE
from architect.session.state import State
from architect.spec import Lifecycle

ROOT = Path(__file__).resolve().parents[1]
BUILDER_FLOOR = {"kind": "held_out_tasks", "limit": 1, "dataset": "tasks"}
FOOTER = re.compile(r"spend  session \$(\d+\.\d{4}) \| this compile (-|\$\d+\.\d{4})")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    block_sockets(monkeypatch)


def services_of(profile: Path) -> Services:
    loaded = load_profile(profile)
    return build_services(loaded, loaded.workspace)


def session_profile(tmp_path: Path, **changes: Any) -> Path:
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


def _cli(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _footers(transcript: str) -> list[tuple[float, float | None]]:
    found: list[tuple[float, float | None]] = []
    for session, compiled in FOOTER.findall(transcript):
        found.append((float(session), None if compiled == "-" else float(compiled[1:])))
    return found


# ------------------------------------------ 1. a recompile never reuses (Section 8.5)


def test_cli_recompile_runs_stage4_even_under_a_standing_cli_grant(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = make_profile(tmp_path, routing={"reuse_origins": ["cli"]})
    services = services_of(profile)
    try:
        target = spec(lifecycle=Lifecycle.INSTALL)
        result = asyncio.run(services.compiler.compile(target, approved=True, no_route=True))
        assert result.artifact is not None and result.artifact.licensed
        services.registry.install("summary", result.artifact)
    finally:
        services.close()
    code, out, err = _cli(["recompile", "summary", "--profile", str(profile)], capsys)
    # A fresh Stage 4 attempt: never the incumbent handed back as an acceptance. The
    # candidate ties the incumbent, so the registry's strict ``_beats`` refuses it.
    assert code == 1 and "registry_rejected" in err and '"reused": true' not in out
    routes = tmp_path / "work" / "routes"
    decisions = [json.loads(line) for line in
                 (routes / "decisions.jsonl").read_text("utf-8").splitlines()]
    recompile = decisions[-1]
    assert recompile["origin"] == "cli" and recompile["action"] == "warm_start"
    assert "route_reuse_installed" not in recompile["codes"]
    outcomes = [json.loads(line) for line in
                (routes / "outcomes.jsonl").read_text("utf-8").splitlines()]
    stage4 = [row for row in outcomes if row["decision_id"] == recompile["decision_id"]]
    assert len(stage4) == 1 and stage4[0]["stage4_passed"] is True
    # The same grant still reuses for a plain compile of that Spec (the rule is narrow).
    path = tmp_path / "spec.json"
    path.write_text(target.model_dump_json(), encoding="utf-8")
    code, out, err = _cli(
        ["compile", "-", "--approved-spec", str(path), "--profile", str(profile)], capsys
    )
    assert code == 0, err
    plain = json.loads(out)
    assert plain["reused"] is True and plain["stages"] == []


def test_session_recompile_runs_stage4_even_under_a_standing_session_grant(
    tmp_path: Path,
) -> None:
    session_profile(tmp_path, lifecycle="install", routing={"reuse_origins": ["session"]})
    target = make_spec(lifecycle="install")
    token = target.spec_hash[:8]
    code, transcript = run_script(
        [f"spec {target.model_dump_json()}", f"approve {token}", f"install invoices {token}",
         "new", "recompile invoices", f"approve {token}", "json", "quit"],
        tmp_path,
    )
    assert code == 0 and "Traceback" not in transcript
    after = unwrap(transcript[transcript.index("> recompile invoices"):])
    assert "route: reuse_installed" not in after
    assert "routing to installed" not in after
    assert "stages 1,2,4" in after and "stages -" not in after


# ----------------------------- 2. Phase 10 verbs stay prose where they are not offered


@pytest.mark.parametrize(
    "goal",
    [
        "route support tickets to the right team",
        "rate the sentiment of a review",
        "export contacts as csv",
        "reuse old answers when possible",
        "roles of each person in a meeting transcript",
    ],
)
def test_default_profile_ready_reads_a_goal_starting_with_a_new_verb_as_prose(
    tmp_path: Path, goal: str
) -> None:
    fake_profile(tmp_path)  # the default profile: no routing table, pinned roles
    code, transcript = run_script([goal, "n", "quit"], tmp_path)
    assert code == 0 and "Traceback" not in transcript
    assert "infer? infer a Spec from this text" in transcript
    for refused in ("error: no_draft", "error: not_a_command", "error: no_result"):
        assert refused not in transcript


def test_phase10_verbs_parse_as_commands_only_where_offered() -> None:
    assert parse("route support tickets", State.READY).verb == "text"  # type: ignore[union-attr]
    assert parse("rate the sentiment", State.READY).verb == "text"  # type: ignore[union-attr]
    assert parse("roles of people", State.READY).verb == "text"  # type: ignore[union-attr]
    assert parse("export contacts", State.REVIEW).verb == "text"  # type: ignore[union-attr]
    assert parse("roles", State.READY).verb == "roles"  # type: ignore[union-attr]
    assert parse("rate good", State.READY).verb == "rate"  # type: ignore[union-attr]
    assert parse("route", State.REVIEW).verb == "route"  # type: ignore[union-attr]
    assert parse("export", State.RESULT).verb == "export"  # type: ignore[union-attr]
    assert parse("rate x", State.RESULT).verb == "rate"  # type: ignore[union-attr]


def test_phase7_amendment_documents_the_prose_rule() -> None:
    text = (ROOT / "docs" / "interfaces" / "phase7-session.md").read_text("utf-8")
    amendment = text[text.index("## 16. Phase 10 amendment"):]
    assert "route support tickets" in amendment
    assert "`rate good` or `rate bad`" in amendment


# ---------------- 3. the CI smoke step can run the routed arm (the baseline's source)


def test_the_ci_smoke_command_line_is_accepted_with_the_routed_arm() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    assert "--variant routed" in workflow
    args = cli._parser().parse_args(
        ["bench", "run", "--provider", "fake", "--tier", "smoke", "--seed", "7",
         "--variant", "default", "--variant", "noisy", "--variant", "template",
         "--variant", "forge", "--variant", "routed", "--yes", "--json"]
    )
    config = cli._bench_config(args)
    assert config.variants == ("default", "noisy", "template", "forge", "routed")
    assert config.rounds == 1


def test_a_routed_row_that_did_not_run_fails_the_bench_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import architect.bench.runner as runner

    async def fake_run(config: Any, **_kw: Any) -> Any:
        rows = [SimpleNamespace(failures=[]), SimpleNamespace(failures=["route_did_not_run"])]
        return SimpleNamespace(
            bench_id="b", rows_path=tmp_path / "rows.jsonl",
            summary=SimpleNamespace(model_dump=lambda mode: {}), bound_usd=0.0,
            skipped=[], rows=rows,
        )

    monkeypatch.setattr(runner, "run_bench", fake_run)
    code, _out, err = _cli(
        ["bench", "run", "--provider", "fake", "--tier", "smoke", "--variant", "routed",
         "--out", str(tmp_path), "--yes", "--json"],
        capsys,
    )
    assert code == 1 and "route_did_not_run" in err


def test_the_bench_cli_accepts_the_routed_arm_rounds_and_the_roles_arm(
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = cli._parser().parse_args(
        ["bench", "run", "--provider", "fake", "--tier", "smoke", "--variant", "routed",
         "--rounds", "2"]
    )
    config = cli._bench_config(args)
    assert config.variants == ("routed",) and config.rounds == 2
    code, out, err = _cli(["bench", "roles", "--policy", "pinned"], capsys)
    assert code == 0, err
    assert out.startswith("roles arm, policy pinned; synthetic_fake:")
    code, out, err = _cli(["bench", "roles", "--policy", "evidence", "--json"], capsys)
    assert code == 0, err
    assert json.loads(out)["evidence"] == "synthetic_fake"


# ------------------------------- 4. a granted reuse is explained before approve runs


def test_session_review_line_explains_a_standing_grant_reuse_before_approve(
    tmp_path: Path,
) -> None:
    session_profile(tmp_path, lifecycle="install", routing={"reuse_origins": ["session"]})
    target = make_spec(lifecycle="install")
    token = target.spec_hash[:8]
    code, transcript = run_script(
        [f"spec {target.model_dump_json()}", f"approve {token}", f"install invoices {token}",
         "again", f"approve {token}", "json", "quit"],
        tmp_path,
    )
    assert code == 0 and "Traceback" not in transcript
    text = unwrap(transcript)
    review = text[text.index("> again"):text.rindex(f"> approve {token}")]
    assert "route: reuse_installed -> invoices@1" in review
    assert "route_reuse_installed: An installed, licensed harness" in review
    assert (f"next: approve {token} delivers invoices@1 as installed: no Stage 1 to 4 and "
            "no compile spend") in review
    assert STAGE4_LINE in review and PLUMBING_LINE in review
    approved = text[text.rindex(f"> approve {token}"):]
    assert f"approved {token} - routing to installed invoices@1" in approved
    assert f"approved {token} - compiling" not in approved
    assert "stages -" in approved


# ---------------------------------- 5. the footer counts a Forge build as this compile


def test_session_footer_after_forge_run_shows_that_run_as_this_compile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forge_repairs(monkeypatch)
    session_profile(tmp_path, forge=True)
    spec_json = make_spec().model_dump_json()
    code, transcript = run_script(
        [f"spec {spec_json}", 'forge run {"text": "hi"}', "y", "json", "quit"], tmp_path
    )
    assert code == 0 and "Traceback" not in transcript
    after = transcript[transcript.index("forge run codes"):]
    footers = _footers(after)
    assert footers, after
    session, compiled = footers[0]
    assert compiled is not None and session > 0  # never "this compile -" after paid work
    assert abs(compiled - session) < 0.00005  # the only paid work of this session


def test_session_footer_after_compile_then_forge_run_is_not_the_stale_compile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forge_repairs(monkeypatch)
    session_profile(tmp_path, forge=True)
    spec_json = make_spec().model_dump_json()
    code, transcript = run_script(
        [f"spec {spec_json}", "compile", "again", 'forge run {"text": "hi"}', "y", "json",
         "quit"],
        tmp_path,
    )
    assert code == 0 and "Traceback" not in transcript
    before = _footers(transcript[:transcript.index("> forge run")])
    after = _footers(transcript[transcript.index("forge run codes"):])
    assert before and after
    session_before, _ = before[-1]
    session_after, compiled = after[0]
    assert compiled is not None
    assert abs(compiled - (session_after - session_before)) < 0.00015


# ------------------------------------ 6. the export_cancelled hint is runnable as shown


def test_session_export_cancel_hint_names_profile_and_the_saved_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forge_repairs(monkeypatch)
    session_profile(tmp_path, lifecycle="install", forge=True, builder_floor=BUILDER_FLOOR)
    (tmp_path / "repo").mkdir()
    target = make_spec(lifecycle="install")
    token = target.spec_hash[:8]
    code, transcript = run_script(
        [f"spec {target.model_dump_json()}", f"approve {token}", "again",
         "forge integrate repo pkgs", "y", "n", "quit"],
        tmp_path,
    )
    assert code == 0 and "Traceback" not in transcript
    text = unwrap(transcript)
    hint = text[text.index("error: export_cancelled"):]
    hint = hint[:hint.index("--reuse-installed") + len("--reuse-installed")]
    profile = tmp_path / "w" / "session-profile.json"
    assert "save <spec.json>" in hint
    assert f"--profile {profile}" in hint
    assert f"--repo {(tmp_path / 'repo').resolve()}" in hint
    assert "--approved-spec <spec.json>" in hint


# --------------------------------- 7. the CLI forge suggestion names --profile


def test_cli_route_forge_suggestion_is_runnable_as_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = session_profile(tmp_path, held_out_expected="bad", routing={})
    target = make_spec()
    code, transcript = run_script(
        [f"spec {target.model_dump_json()}", "compile", "json", "quit"], tmp_path
    )
    assert code == 0 and "licensed: NO" in transcript
    path = tmp_path / "spec.json"
    path.write_text(target.model_dump_json(), encoding="utf-8")
    code, out, _err = _cli(["route", str(path), "--profile", str(profile)], capsys)
    assert code == 0
    text = unwrap(out)
    assert (f"architect forge run <spec.json> --profile {profile} --input JSON") in text


# ----------------------------------------- 8. roles labels synthetic_fake as plumbing


def test_roles_table_labels_synthetic_fake_numbers_as_plumbing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = make_profile(tmp_path)
    code, out, err = _cli(["roles", "--profile", str(profile)], capsys)
    assert code == 0, err
    assert "synthetic_fake" in out
    assert PLUMBING_LINE in out
    assert all(len(line) <= 100 and line.isascii() for line in out.splitlines())
    code, out, err = _cli(["roles", "--profile", str(profile), "--json"], capsys)
    assert code == 0, err
    assert PLUMBING_LINE in json.loads(out)["notes"]


# ---------------------------------------- 9. rate_no_run hints a command of the state


def test_rate_no_run_hint_is_a_command_the_state_accepts(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    spec_json = make_spec().model_dump_json()
    code, transcript = run_script(
        ["rate good", f"spec {spec_json}", "rate good", "quit"], tmp_path
    )
    assert code == 0 and "Traceback" not in transcript
    text = unwrap(transcript)
    assert "next: run {json}\n" not in text
    ready = text[text.index("> rate good"):text.index("> spec")]
    assert "error: rate_no_run" in ready
    assert "next: run <name|dir> {json}, then rate good|bad" in ready
    review = text[text.rindex("> rate good"):]
    assert "error: rate_no_run" in review
    assert ("next: reuse <name> <hash8> {json} | forge run {json} | run <name|dir> {json}, "
            "then rate good|bad") in review


# ------------- 3b. decision determinism: latency below the resolution is not a preference


def _probe(spec: Any, *, grant: bool = True, forbid: bool = False) -> Any:
    from architect.routing.router import TaskProbe

    return TaskProbe(origin="cli", spec=spec, budget_cap=spec.budget, ladder=("m1",),
                     grant_reuse=grant, forbid_reuse=forbid)


def test_tier_a_latency_noise_below_the_resolution_never_decides() -> None:
    from routing_support import FakeIndex, installed_ev
    from routing_support import make_spec as routing_spec

    from architect.routing.router import RoutePolicy, route
    from architect.telemetry import categorical

    target = routing_spec(model="m1")
    first = sorted(("zeta", "alpha"), key=categorical)[0]
    # Clock-tick noise (0 s against one 15.6 ms Windows tick) must not flip the choice:
    # both orders of the noisy measurement choose the same entry, by the name tiebreak.
    for fast, slow in (("zeta", "alpha"), ("alpha", "zeta")):
        rows = (installed_ev(target, name=fast, latency=0.0),
                installed_ev(target, name=slow, latency=0.0156))
        decision = route(_probe(target), index=FakeIndex(rows), policy=RoutePolicy())
        assert decision.target.name == first  # type: ignore[union-attr]
    # A real difference still decides.
    rows = (installed_ev(target, name=first, latency=0.9),
            installed_ev(target, name="other", latency=0.2))
    decision = route(_probe(target), index=FakeIndex(rows), policy=RoutePolicy())
    assert decision.target.name == "other"  # type: ignore[union-attr]


def test_a_forbidden_reuse_probe_never_reuses_even_when_granted() -> None:
    from routing_support import FakeIndex, installed_ev
    from routing_support import make_spec as routing_spec

    from architect.routing.router import RoutePolicy, route

    target = routing_spec(model="m1")
    rows = (installed_ev(target, name="incumbent"),)
    policy = RoutePolicy.model_validate({"reuse_origins": ["cli"]})
    granted = route(_probe(target), index=FakeIndex(rows), policy=policy)
    assert granted.action == "reuse_installed"
    forbidden = route(_probe(target, forbid=True), index=FakeIndex(rows), policy=policy)
    assert forbidden.action != "reuse_installed"
    assert "route_reuse_installed" not in forbidden.codes
