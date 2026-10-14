"""CLI dispatch and real native-process fake-profile compiler/delivery checks."""

import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from architect import cli
from architect.orchestrator import OrchestrationResult
from architect.profiles import Services
from architect.spec import Budget, FloorDefinition, FloorKind, Lifecycle, Spec

ROOT = Path(__file__).resolve().parents[1]


def make_spec(*, install: bool = False) -> Spec:
    return Spec(task="Return ok", input_schema={"type": "object"},
                output_schema={"type": "string"}, domain="synthesizer",
                floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="tasks"),
                budget=Budget(), lifecycle=Lifecycle.INSTALL if install else Lifecycle.EXPIRE)


def setup_profile(tmp_path: Path, *, install: bool = False) -> tuple[Path, Path]:
    spec = make_spec(install=install)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(spec.model_dump_json(), encoding="utf-8")
    (tmp_path / "cases.json").write_text(json.dumps([
        {"case_id": str(i), "task": {}, "expected": "ok"} for i in range(6)
    ]), encoding="utf-8")
    (tmp_path / "responses.json").write_text(json.dumps([
        {"text": "ok", "tokens_in": 8, "tokens_out": 12, "cost_usd": .00002, "model": "fake"}
        for _ in range(20)
    ]), encoding="utf-8")
    (tmp_path / "inference.json").write_text(json.dumps([
        {"text": spec.model_dump_json(), "tokens_in": 8, "tokens_out": 12,
         "cost_usd": .00002, "model": "fake"}
    ]), encoding="utf-8")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps({
        "workspace": "work",
        "models": {"fake": {"provider": "fake", "fixture": "responses.json"}},
        "inference": {"provider": "fake", "model": "fake", "fixture": "inference.json"},
        "floors": {"tasks": {"kind": "held_out_tasks", "path": "cases.json"}},
        "compile": {"max_candidates": 1, "enable_rewriting": False},
    }), encoding="utf-8")
    return profile_path, spec_path


def native(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "architect", *args], cwd=ROOT,
                          capture_output=True, text=True, timeout=120, check=False)


def test_native_compile_run_and_expiry(tmp_path: Path) -> None:
    profile, spec = setup_profile(tmp_path)
    compiled = native("compile", str(spec), "--profile", str(profile))
    assert compiled.returncode == 0, compiled.stderr
    result = json.loads(compiled.stdout)
    assert result["accepted"] and result["stages"] == [1, 2, 4]
    assert result["report"]["held_out_count"] == 3
    artifact = result["artifact"]["directory"]
    run = native("run", artifact, "--input", "{}", "--profile", str(profile))
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["output"] == "ok"
    again = native("run", artifact, "--input", "{}", "--profile", str(profile))
    assert again.returncode != 0
    assert json.loads(again.stderr) == {"error": "artifact_expired"}


def test_install_inference_review_exact_file_and_registry(tmp_path: Path) -> None:
    profile, _ = setup_profile(tmp_path, install=True)
    proposed = native("compile", "Make a persistent feature", "--profile", str(profile))
    assert proposed.returncode == 0, proposed.stderr
    proposal = json.loads(proposed.stdout)
    assert proposal["approval_required"] and not proposal["accepted"]
    assert proposal["artifact"] is None and proposal["stages"] == [0]
    proposal_path = Path(proposal["proposal_path"])
    spec = Spec.model_validate_json(proposal_path.read_text(encoding="utf-8"))
    assert proposal_path == tmp_path / "work" / "proposals" / f"{spec.spec_hash}.json"
    assert not list((tmp_path / "work").glob("artifacts/**/harness.py"))
    compiled = native("compile", "UNREVIEWED request must not replace reviewed spec",
                      "--profile", str(profile), "--approved-spec", str(proposal_path))
    assert compiled.returncode == 0, compiled.stderr
    result = json.loads(compiled.stdout)
    assert result["spec"]["task"] == spec.task
    assert result["stages"] == [1, 2, 4]
    installed = result["registry_entry"]["directory"]
    for _ in range(2):
        run = native("run", installed, "--input", "{}", "--profile", str(profile))
        assert run.returncode == 0, run.stderr
        assert json.loads(run.stdout)["output"] == "ok"
    assert (Path(installed) / "harness.py").is_file()
    name = result["registry_entry"]["name"]
    listing = native("registry", "--workspace", str(tmp_path / "work"))
    assert listing.returncode == 0, listing.stderr
    assert json.loads(listing.stdout)[0]["name"] == name
    status = native("status", name, "--workspace", str(tmp_path / "work"))
    assert json.loads(status.stdout)["revision"] == 1
    # A recompile is measured and reverified, but equal quality/cost does not ship.
    recompiled = native("recompile", name, "--profile", str(profile))
    assert recompiled.returncode != 0
    assert json.loads(recompiled.stderr) == {"error": "registry_rejected"}
    assert json.loads(native("status", name, "--workspace", str(tmp_path / "work")).stdout)[
        "revision"] == 1


def test_model_cannot_supply_approval(tmp_path: Path) -> None:
    profile, _ = setup_profile(tmp_path, install=True)
    fixture = tmp_path / "inference.json"
    responses = json.loads(fixture.read_text(encoding="utf-8"))
    spec = json.loads(responses[0]["text"])
    spec["approved"] = True
    responses[0]["text"] = json.dumps(spec)
    fixture.write_text(json.dumps(responses), encoding="utf-8")
    result = native("compile", "The model approves this", "--profile", str(profile))
    assert result.returncode != 0
    assert not list((tmp_path / "work").glob("artifacts/**/harness.py"))


@pytest.mark.parametrize("arguments", [["unknown"], ["compile", "request"],
                                         ["do", "goal", "--profile", "private"]])
def test_argument_errors_are_fixed_json(arguments: list[str], capsys: pytest.CaptureFixture[str]):
    assert cli.main(arguments) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert json.loads(captured.err) == {"error": "invalid_arguments"}


def test_no_arguments_enters_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    profile, _ = setup_profile(tmp_path)
    (tmp_path / "architect.json").write_text(profile.read_text(encoding="utf-8"),
                                             encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO("quit\n"))
    # Phase 12: no arguments opens the Architect CLI; the step-by-step session is
    # ``architect session``.
    assert cli.main(["session"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("architect 0.1.0  workspace ")
    assert "architect> quit" in captured.out
    assert captured.out.rstrip().endswith("bye")
    assert not captured.err


def test_invalid_profile_never_prints_secrets(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    path = tmp_path / "bad.json"
    path.write_text('{"api_key":"PRIVATE_SENTINEL"}', encoding="utf-8")
    assert cli.main(["compile", "private task", "--profile", str(path)]) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err == '{"error": "invalid_profile"}\n'


def test_services_close_on_command_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    profile, _ = setup_profile(tmp_path)
    closed: list[bool] = []
    original = Services.close

    def close(services: Services) -> None:
        closed.append(True)
        original(services)

    monkeypatch.setattr(Services, "close", close)
    assert cli.main(["run", "missing", "--input", "not-json", "--profile", str(profile)]) == 1
    assert closed == [True]
    assert json.loads(capsys.readouterr().err) == {"error": "invalid_input_json"}


def test_do_delegates_real_repo_and_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    profile, _ = setup_profile(tmp_path)
    run = AsyncMock(return_value=OrchestrationResult(accepted=True, tasks=()))
    close = Mock()
    services = SimpleNamespace(orchestrator=SimpleNamespace(run=run), close=close)
    monkeypatch.setattr(cli, "build_services", lambda *args: services)
    assert cli.main(["do", "implement", "--repo", str(tmp_path), "--profile", str(profile)]) == 0
    run.assert_awaited_once_with("implement", repo=tmp_path.resolve())
    close.assert_called_once()
    assert json.loads(capsys.readouterr().out)["accepted"] is True


def test_explicit_install_spec_is_still_not_approved(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    profile, spec = setup_profile(tmp_path, install=True)
    assert cli.main(["compile", str(spec), "--profile", str(profile)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["approval_required"] and result["stages"] == []
    assert result["artifact"] is None


def test_help_and_empty_registry(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--help"]) == 0
    assert "create" in capsys.readouterr().out
    assert cli.main(["status", "--workspace", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_rejected_compile_and_failed_orchestration_are_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    profile, spec = setup_profile(tmp_path)
    (tmp_path / "cases.json").write_text("[]", encoding="utf-8")
    assert cli.main(["compile", str(spec), "--profile", str(profile)]) == 1
    capture = capsys.readouterr()
    assert json.loads(capture.out)["accepted"] is False
    assert json.loads(capture.err) == {"error": "compile_rejected"}
    services = SimpleNamespace(orchestrator=SimpleNamespace(run=AsyncMock(
        return_value=OrchestrationResult(accepted=False, tasks=(), failures=("invalid_plan",))
    )), close=Mock())
    monkeypatch.setattr(cli, "build_services", lambda *args: services)
    assert cli.main(["do", "goal", "--repo", str(tmp_path), "--profile", str(profile)]) == 1
    assert json.loads(capsys.readouterr().err) == {"error": "orchestration_rejected"}
    services.close.assert_called_once()


def test_native_do_uses_model_decomposition_and_full_compiler(tmp_path: Path) -> None:
    profile, _ = setup_profile(tmp_path)
    plan = {"tasks": [{"id": "summary", "request": "Return ok", "input": {},
                       "domain": "synthesizer", "model": "fake"}]}
    (tmp_path / "decomposition.json").write_text(json.dumps([
        {"text": json.dumps(plan), "tokens_in": 8, "tokens_out": 12,
         "cost_usd": .00002, "model": "fake"},
    ]), encoding="utf-8")
    payload = json.loads(profile.read_text(encoding="utf-8"))
    payload["decomposition"] = {
        "provider": "fake", "model": "fake", "fixture": "decomposition.json",
    }
    profile.write_text(json.dumps(payload), encoding="utf-8")
    result = native("do", "Summarize", "--repo", str(tmp_path), "--profile", str(profile))
    assert result.returncode == 0, result.stdout + result.stderr
    task = json.loads(result.stdout)["tasks"][0]
    assert task["accepted"] and task["output"] == "ok"
    artifact = Path(task["artifact_directory"])
    assert (artifact / "report.json").is_file()
    assert (artifact / ".consumed").is_file()
    assert not (artifact / "harness.py").exists()


def test_create_requires_explicit_builder_floor(tmp_path: Path) -> None:
    profile, _ = setup_profile(tmp_path)
    result = native("create", "Make a feature", "--repo", str(tmp_path),
                    "--profile", str(profile))
    assert result.returncode != 0
    assert json.loads(result.stderr) == {"error": "builder_floor_required"}
    assert not list((tmp_path / "work").glob("artifacts/**/harness.py"))


def test_create_delegates_exact_reviewed_file_and_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from architect.orchestrator import features

    profile, reviewed = setup_profile(tmp_path, install=True)
    services = SimpleNamespace(close=Mock())
    create = AsyncMock(return_value=OrchestrationResult(accepted=True, tasks=()))
    constructor = Mock(return_value=SimpleNamespace(create=create))
    monkeypatch.setattr(cli, "build_services", lambda *args: services)
    monkeypatch.setattr(features, "FeatureBuilder", constructor)
    assert cli.main(["create", "Build feature", "--repo", str(tmp_path),
                     "--profile", str(profile), "--approved-spec", str(reviewed)]) == 0
    constructor.assert_called_once_with(services)
    create.assert_awaited_once_with("Build feature", tmp_path.resolve(),
                                   approved_spec=make_spec(install=True))
    services.close.assert_called_once()
    assert json.loads(capsys.readouterr().out)["accepted"] is True


def test_create_shows_trusted_builder_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from architect.orchestrator import TaskResult, features

    profile, _ = setup_profile(tmp_path, install=True)
    services = SimpleNamespace(close=Mock())
    proposed = make_spec(install=True)
    proposal_path = cli._proposal(proposed, tmp_path / "work")
    checkpoint = OrchestrationResult(accepted=False, tasks=(TaskResult(
        id="feature_factory", accepted=False, output={
            "approval_required": True, "spec": proposed.model_dump(mode="json"),
            "proposal_path": str(proposal_path),
        }, failures=("approval_required",),
    ),), failures=("approval_required",))
    create = AsyncMock(return_value=checkpoint)
    monkeypatch.setattr(cli, "build_services", lambda *args: services)
    monkeypatch.setattr(features, "FeatureBuilder", Mock(
        return_value=SimpleNamespace(create=create)))
    assert cli.main(["create", "Make feature", "--repo", str(tmp_path),
                     "--profile", str(profile)]) == 1
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output["tasks"][-1]["output"]["approval_required"] is True
    assert output["tasks"][-1]["output"]["proposal_path"] == str(proposal_path)
    create.assert_awaited_once_with("Make feature", tmp_path.resolve(), approved_spec=None)
    services.close.assert_called_once()


# ------------------------------------------------------------------ bench (Phase 8)


def bench(capsys: pytest.CaptureFixture[str], *arguments: str) -> tuple[int, str, str]:
    code = cli.main(["bench", *arguments])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        (["run", "--tier", "bogus"], "bench_tier_unknown"),
        (["run", "--tier", "smoke", "--suite", "v9"], "bench_suite_unknown"),
        (["run", "--tier", "smoke", "--variant", "weird"], "bench_variant_unknown"),
        (["run", "--tier", "quick", "--provider", "profile"], "bench_profile_required"),
        # No silent fall-back to the fake provider: the provider defaults to the profile.
        (["run", "--tier", "quick", "--max-usd", "0.0001"], "bench_profile_required"),
        (["run", "--tier", "smoke"], "bench_profile_required"),
        (["run", "--tier", "quick", "--profile", "missing-profile.json"],
         "bench_invalid_profile"),
        (["run", "--tier", "quick", "--provider", "fake", "--models", "nope"],
         "bench_model_unconfigured"),
        (["run", "--tier", "quick", "--provider", "fake", "--route", "vision"],
         "bench_route_invalid"),
        (["run", "--tier", "quick", "--provider", "fake", "--route", "judge=fake"],
         "bench_route_invalid"),
        (["run", "--tier", "quick", "--provider", "fake", "--max-usd", "0.000001", "--size",
          "2"], "bench_over_max_usd"),
        (["compare", "no-such-id", "other-id"], "bench_rows_unreadable"),
        (["probe", "--profile", "missing-profile.json"], "bench_invalid_profile"),
        # Filters that select no cell are refused up front, never an empty green run.
        (["run", "--tier", "quick", "--provider", "fake", "--presets", "coder"],
         "bench_no_cells"),
        (["run", "--tier", "quick", "--provider", "fake", "--variant", "images-all-stages"],
         "bench_no_cells"),
    ],
)
def test_bench_errors_are_fixed_codes(
    arguments: list[str],
    code: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    exit_code, out, err = bench(capsys, *arguments)
    assert exit_code == 1
    assert json.loads(err.strip().splitlines()[-1]) == {"error": code}
    assert "Traceback" not in err and not (tmp_path / ".architect" / "bench").exists()


@pytest.mark.parametrize(
    "arguments",
    [["run"], ["run", "--tier", "smoke", "--seed", "x"], ["pages", "--seed", "1"], ["nope"],
     ["run", "--tier", "smoke", "--size", "1", "--provider", "fake"]],
)
def test_bench_argument_errors_exit_2(
    arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code, out, err = bench(capsys, *arguments)
    assert exit_code == 2 and not out
    assert json.loads(err) == {"error": "invalid_arguments"}


def test_bench_run_json_compare_and_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "b"
    common = ["--tier", "quick", "--provider", "fake", "--size", "2", "--out", str(out)]
    code, stdout, stderr = bench(capsys, "run", *common, "--json", "--yes")
    assert code == 0
    payload = json.loads(stdout)
    assert set(payload) == {"bench_id", "rows_path", "summary", "bound_usd", "skipped"}
    assert payload["skipped"] == [] and payload["bound_usd"] > 0
    assert Path(payload["rows_path"]) == out / payload["bench_id"] / "rows.jsonl"
    assert stderr.startswith("bench quick on 1 models: <=$")
    assert "API-equivalent bound" in stderr and "not a bill." in stderr
    code, table, _ = bench(capsys, "run", *common, "--label", "second")
    assert code == 0
    assert table.startswith("bench quick on 1 models:")
    assert "task                   model         var     lic held-out (k/n)" in table
    assert "money is API-equivalent, not a bill; scores are synthetic_fake" in table
    assert all(len(line) <= 100 and line.isascii() for line in table.splitlines())
    second = next(p for p in out.iterdir() if p.name != payload["bench_id"])
    first = payload["bench_id"]
    # Two separate runs share scores and cost but not wall time: a >= 15% wall drop is
    # an ``improvement`` by the Section 9 rule, so only the scores are asserted here.
    code, text, _ = bench(
        capsys, "compare", first, second.name, "--out", str(out), "--expect", "identical-scores"
    )
    assert code == 0 and "overall: " in text
    assert "paired on tuning cases (selection-biased)" in text
    code, data, _ = bench(
        capsys, "compare", payload["rows_path"], str(second), "--out", str(out), "--json"
    )
    comparison = json.loads(data)
    assert code == 0 and comparison["verdict"] in {"identical", "improvement"}
    for cell in comparison["cells"]:
        assert cell["base_k"] == cell["cand_k"] and cell["held_out_delta"] == 0
        assert cell["discordant_b"] == cell["discordant_c"] == 0
        assert cell["licensed_base"] and cell["licensed_cand"]
        assert cell["cost_delta_pct"] == 0 and cell["verdict"] in {"identical", "improvement"}
    assert comparison["evidence"] == ["synthetic_fake"]
    code, listing, _ = bench(capsys, "list", "--out", str(out), "--json")
    listed = json.loads(listing)
    assert code == 0 and {row["bench_id"] for row in listed["recorded"]} == {first, second.name}
    assert [task["id"] for task in listed["tasks"]][0] == "sentiment-24"
    assert listed["bounds"][0]["model"] == "fake"
    code, listing_text, _ = bench(capsys, "list", "--out", str(out))
    assert code == 0 and first in listing_text
    assert all(len(line) <= 100 for line in listing_text.splitlines())


def test_bench_compare_refuses_changed_presets_without_the_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from architect.bench import read_rows, write_rows

    out = tmp_path / "b"
    code, stdout, _ = bench(
        capsys, "run", "--tier", "quick", "--provider", "fake", "--size", "2", "--out", str(out),
        "--json",
    )
    rows_path = Path(json.loads(stdout)["rows_path"])
    changed = tmp_path / "changed.jsonl"
    write_rows(
        changed,
        [row.model_copy(update={"preset_fingerprint": "0" * 64}) for row in read_rows(rows_path)],
    )
    code, _, err = bench(capsys, "compare", str(rows_path), str(changed))
    assert code == 1 and json.loads(err) == {"error": "bench_compare_fingerprint"}
    code, text, _ = bench(
        capsys, "compare", str(rows_path), str(changed), "--allow-fingerprint-mismatch"
    )
    assert code == 0 and "overall: identical" in text


def test_bench_pages_writes_a_synthetic_dataset(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, stdout, _ = bench(
        capsys, "pages", "--seed", "7", "--count", "2", "--out", str(tmp_path / "p"), "--png",
        "--kind", "digitizer",
    )
    result = json.loads(stdout)
    assert code == 0 and result["pages"] == 2 and result["pngs"] == 2 and result["synthetic"]
    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert manifest["seed"] == 7 and manifest["style"] == "clean"
    assert (tmp_path / "p" / "page-000.png").read_bytes().startswith(b"\x89PNG")
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    code, _, err = bench(capsys, "pages", "--seed", "7", "--count", "1", "--out", str(blocker))
    assert code == 1 and json.loads(err) == {"error": "bench_out_unwritable"}


def test_bench_probe_on_a_fake_profile(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"models": {"fake": {"provider": "fake"}}}), encoding="utf-8")
    code, stdout, _ = bench(capsys, "probe", "--profile", str(profile), "--json")
    [row] = json.loads(stdout)
    assert code == 0 and row["model"] == "fake" and row["provider"] == "fake"
    assert row["code"] is None and row["basis"] == "assumption"
    code, table, _ = bench(capsys, "probe", "--profile", str(profile))
    assert code == 0 and "assumption!" in table
    code, _, err = bench(capsys, "probe", "--profile", str(profile), "--models", "nope")
    assert code == 1 and json.loads(err) == {"error": "bench_model_unconfigured"}


def test_bench_probe_needs_no_bench_extra_and_codes_a_missing_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The probe page is a stdlib ``clean`` page; a rendering ImportError is a bench code."""
    import architect.bench

    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"models": {"fake": {"provider": "fake"}}}), encoding="utf-8")
    monkeypatch.setitem(sys.modules, "PIL", None)  # as if the bench extra were absent
    code, stdout, err = bench(capsys, "probe", "--profile", str(profile), "--json")
    assert code == 0, err
    assert json.loads(stdout)[0]["code"] is None

    def missing(*args: object, **kwargs: object) -> object:
        raise ImportError("PIL")

    monkeypatch.setattr(architect.bench, "generate_pages", missing)
    code, _, err = bench(capsys, "probe", "--profile", str(profile))
    assert code == 1 and json.loads(err) == {"error": "bench_extra_missing"}


def test_bench_refuses_an_ollama_model_that_is_not_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    profile = tmp_path / "cloud.json"
    profile.write_text(
        json.dumps(
            {
                "models": {
                    "granite4.2:8b": {
                        "provider": "ollama",
                        "base_url": "https://ollama.com",
                        "prices": str(ROOT / "examples" / "local" / "prices.json"),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    for arguments in (
        ("probe", "--profile", str(profile)),
        ("run", "--tier", "quick", "--profile", str(profile), "--out", str(tmp_path / "o")),
    ):
        code, out, err = bench(capsys, *arguments)
        assert code == 1 and json.loads(err) == {"error": "bench_model_not_local"}, arguments
        assert out == ""
    assert not (tmp_path / "o").exists()


def test_bench_out_unwritable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    code, _, err = bench(
        capsys, "run", "--tier", "quick", "--provider", "fake", "--size", "2", "--out",
        str(blocker),
    )
    assert code == 1 and json.loads(err.strip().splitlines()[-1]) == {
        "error": "bench_out_unwritable"
    }


def test_fake_smoke_carries_its_negative_control_unless_variants_are_named() -> None:
    from architect.bench import BenchError

    parser = cli._parser()
    fake = ["bench", "run", "--provider", "fake"]
    smoke = cli._bench_config(parser.parse_args([*fake, "--tier", "smoke"]))
    assert smoke.provider == "fake" and smoke.variants == ("default", "noisy")
    named = cli._bench_config(
        parser.parse_args([*fake, "--tier", "smoke", "--variant", "default"])
    )
    assert named.variants == ("default",)
    quick = cli._bench_config(parser.parse_args([*fake, "--tier", "quick"]))
    assert quick.variants == ("default",)
    with pytest.raises(BenchError) as caught:  # Section 8.1: the default provider is profile
        cli._bench_config(parser.parse_args(["bench", "run", "--tier", "smoke"]))
    assert caught.value.code == "bench_profile_required"


# ---------------------------------------------------------- bench consent (fail closed)


class ConsoleInput(io.StringIO):
    """A stdin that says it is an interactive console."""

    def isatty(self) -> bool:
        return True


class PipeInput(io.StringIO):
    def isatty(self) -> bool:
        return False


class EndOfInput(ConsoleInput):
    def readline(self, size: int | None = -1) -> str:
        raise EOFError


def paid_profile(tmp_path: Path) -> Path:
    """A profile provider (not ``--provider fake``): the run needs explicit consent."""
    profile = tmp_path / "paid.json"
    profile.write_text(json.dumps({"models": {"fake": {"provider": "fake"}}}), encoding="utf-8")
    return profile


def paid_run(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "run", "--tier", "quick", "--profile", str(paid_profile(tmp_path)), "--presets",
        "extractor", "--size", "2", "--out", str(tmp_path / "out"), *extra,
    ]


def spy_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count every model call any FakeAdapter completes."""
    from architect.models import FakeAdapter

    calls: list[int] = []
    original = FakeAdapter.complete

    async def complete(self: FakeAdapter, request: object) -> object:
        calls.append(1)
        return await original(self, request)  # type: ignore[arg-type]

    monkeypatch.setattr(FakeAdapter, "complete", complete)
    return calls


@pytest.mark.parametrize(
    ("stdin", "extra"),
    [
        (PipeInput("y\n"), ()),  # a pipe, script, cron or CI shell never consents
        (PipeInput(""), ("--json",)),
        (ConsoleInput("n\n"), ()),
        (ConsoleInput("\n"), ("--json",)),
        (ConsoleInput(""), ()),  # end of input at the prompt
        (EndOfInput(""), ()),
    ],
)
def test_paid_bench_without_consent_is_cancelled_before_spend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stdin: io.StringIO,
    extra: tuple[str, ...],
) -> None:
    calls = spy_calls(monkeypatch)
    monkeypatch.setattr(sys, "stdin", stdin)
    code, out, err = bench(capsys, *paid_run(tmp_path, *extra))
    assert code == 1
    # A test console does not echo the typed newline, so the code may share the prompt line.
    assert err.rstrip().endswith('{"error": "bench_cancelled"}')
    human = err if "--json" in extra else out
    assert human.startswith("bench quick on 1 models: <=$")
    if "--json" in extra:
        assert out == ""  # stdout carries only a result object, and there is none
    assert calls == [] and not (tmp_path / "out").exists()


def test_paid_bench_consents_with_yes_or_y_and_json_stays_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = spy_calls(monkeypatch)
    monkeypatch.setattr(sys, "stdin", PipeInput(""))
    code, out, err = bench(capsys, *paid_run(tmp_path, "--yes", "--json"))
    assert code == 0 and json.loads(out)["bench_id"]  # --yes is the non-interactive consent
    assert "Continue?" not in out + err and calls
    calls.clear()
    monkeypatch.setattr(sys, "stdin", ConsoleInput("y\n"))
    code, out, err = bench(capsys, *paid_run(tmp_path, "--json"))
    assert code == 0 and calls
    payload = json.loads(out)  # the prompt went to stderr, never into the JSON
    assert set(payload) == {"bench_id", "rows_path", "summary", "bound_usd", "skipped"}
    assert "Continue? [y/N] " in err and err.startswith("bench quick on 1 models:")
    monkeypatch.setattr(sys, "stdin", ConsoleInput("yes\n"))
    code, out, _ = bench(capsys, *paid_run(tmp_path))
    assert code == 0 and "Continue? [y/N] " in out  # human mode: the prompt follows the bound


def test_paid_bench_over_max_usd_is_refused_before_asking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", ConsoleInput("y\n"))
    code, out, err = bench(capsys, *paid_run(tmp_path, "--max-usd", "0.000001"))
    assert code == 1 and json.loads(err) == {"error": "bench_over_max_usd"}
    assert "Continue?" not in out and not (tmp_path / "out").exists()


def test_bench_interrupt_exits_130_and_keeps_rows_labelled_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A real SIGINT mid-run: asyncio cancels the bench, rows so far are kept."""
    import signal

    from architect.bench import read_rows
    from architect.bench import render as tables

    original = tables.progress_line
    fired: list[int] = []

    def progress_line(*args: object) -> str:
        line = original(*args)  # type: ignore[arg-type]
        done = args[1]
        if done == 1 and not fired:
            fired.append(1)
            signal.raise_signal(signal.SIGINT)
        return line

    monkeypatch.setattr(tables, "progress_line", progress_line)
    out = tmp_path / "out"
    code, _, err = bench(
        capsys, "run", "--tier", "quick", "--provider", "fake", "--size", "2", "--out", str(out)
    )
    assert code == 130 and json.loads(err.strip().splitlines()[-1]) == {"error": "interrupted"}
    [directory] = list(out.iterdir())
    rows = read_rows(directory / "rows.jsonl")
    assert rows[0].task_id == "sentiment-24" and rows[0].failures == ()
    assert rows[-1].failures[-1] == "cancelled" and len(rows) == 2
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    assert summary["cancelled"] is True


def test_bench_list_skips_a_model_whose_adapter_cannot_be_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("BENCH_LIST_UNSET_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)  # the local model's default host
    profile = tmp_path / "mixed.json"
    profile.write_text(
        json.dumps(
            {
                "models": {
                    "gemma4:26b": {
                        "provider": "ollama",
                        "prices": str(ROOT / "examples" / "local" / "prices.json"),
                    },
                    "claude-haiku-4-5-20251001": {
                        "provider": "anthropic",
                        "api_key_env": "BENCH_LIST_UNSET_KEY",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    code, out, err = bench(
        capsys, "list", "--profile", str(profile), "--out", str(tmp_path / "o"), "--json"
    )
    assert code == 0, err
    bounds = json.loads(out)["bounds"]
    assert [row["model"] for row in bounds] == ["gemma4:26b"]
    assert bounds[0]["evidence"] == "local_zero_cost" and bounds[0]["bound_usd"] == 0


def test_bench_table_ends_with_the_fixed_money_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, table, _ = bench(
        capsys, "run", "--tier", "quick", "--provider", "fake", "--size", "2", "--out",
        str(tmp_path / "o"),
    )
    lines = table.split("rows written to", 1)[0].splitlines()
    assert code == 0
    assert lines[-1] == "money is API-equivalent, not a bill; scores are synthetic_fake"
    assert lines[-2] == "floors: example_fixture, synthetic_generated (synthetic by construction)"


def test_bench_rows_written_line_is_wrapped_to_the_table_width(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The closing ``rows written to`` line keeps the 100-column ASCII rule on long paths."""
    out = tmp_path.joinpath(*(["a-rather-long-directory-name"] * 3))
    code, table, _ = bench(
        capsys, "run", "--tier", "quick", "--provider", "fake", "--size", "2", "--out", str(out)
    )
    assert code == 0
    assert all(len(line) <= 100 and line.isascii() for line in table.splitlines())
    tail = table.split("rows written to", 1)[1]
    [rows_path] = list(out.glob("*/rows.jsonl"))
    assert len(f"rows written to {rows_path}") > 100
    assert "".join(tail.split()) == str(rows_path)


def test_bench_arc_with_nothing_available_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from architect.bench import runner

    monkeypatch.setattr(runner, "_has_module", lambda name: False)
    code, out, err = bench(
        capsys, "run", "--tier", "arc", "--provider", "fake", "--out", str(tmp_path / "o")
    )
    assert code == 1 and not out
    assert json.loads(err.strip().splitlines()[-1]) == {"error": "bench_arc_unavailable"}
    assert not (tmp_path / "o").exists()


# ------------------------------------------------------------------ retake, undo, verify


@pytest.fixture
def placed(tmp_path: Path):  # type: ignore[no-untyped-def]
    """One built agent added to a project (``retake_support.place``), offline."""
    import asyncio

    from retake_support import place

    return asyncio.run(place(tmp_path))


def _lines(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return capsys.readouterr().out.splitlines()


STOP = "Stopped: changed after its exam. 0 model calls."


def test_retake_passes_in_place_and_verify_names_the_attempt_and_its_cases(
    placed, capsys: pytest.CaptureFixture[str]
) -> None:
    folder, profile = str(placed.folder), str(placed.profile)
    assert cli.main(["verify", folder]) == 0
    assert _lines(capsys)[:2] == ["verified", "fake. 4 of 4. Pass mark 4. Passed. Attempt 1."]
    placed.reword()
    assert cli.main(["verify", folder]) == 1
    assert _lines(capsys)[:3] == [
        "refused: export_integrity_mismatch", STOP, "Changed: artifact/spec.json.",
    ]
    assert cli.main(["retake", folder, "--profile", profile, "--yes"]) == 0
    assert _lines(capsys) == [
        STOP,
        "Changed: artifact/spec.json (task text).",
        "Attempt 2, on 4 cases no earlier attempt used. Under $0.001. It stops if it "
        "reaches $10.00, the build's cost cap.",
        "4 of 4. Pass mark 4. Passed. It is your agent.",
        "Attempt 2, on 4 cases no earlier attempt used. Your app's calls work again.",
    ]
    assert cli.main(["verify", folder]) == 0
    assert _lines(capsys)[:3] == [
        "verified",
        "fake. 4 of 4. Pass mark 4. Passed. Attempt 2.",
        "Attempt 2 was on 4 cases no earlier attempt used.",
    ]
    # Nothing changed since: there is nothing to retake, and no attempt is used.
    assert cli.main(["retake", folder, "--profile", profile, "--yes"]) == 2
    assert _lines(capsys)[0] == "Nothing changed since its exam, so there is nothing to retake."


def test_retake_that_does_not_pass_exits_1_and_undo_puts_the_version_back(
    placed, capsys: pytest.CaptureFixture[str]
) -> None:
    from retake_support import MISS

    folder, profile = str(placed.folder), str(placed.profile)
    placed.use_model(MISS)
    assert cli.main(["retake", folder, "--profile", profile, "--practice", "--yes"]) == 0
    assert _lines(capsys) == [
        "Practice, on the 4 practice cases, which no exam uses. Not an attempt. Under "
        "$0.001. It stops if it reaches $10.00, the build's cost cap.",
        "0 of 4 practice cases right. Not an attempt.",
    ]
    assert cli.main(["retake", folder, "--profile", profile, "--yes"]) == 1
    lines = _lines(capsys)
    assert lines[:2] == [STOP, "Changed: artifact/config.json (model: fake to gpt-4o-mini)."]
    assert lines[3:] == [
        "0 of 4. Pass mark 4. Did not pass.",
        "Attempt 2, on 4 cases no earlier attempt used. Your agent is still stopped.",
        # What to type next, with the folder and the profile as they were given.
        f"Undo: architect undo {folder}",
        "Try a change without using an attempt: architect retake "
        f"{folder} --profile {profile} --practice",
    ]
    assert cli.main(["undo", folder, "--profile", profile]) == 0
    assert _lines(capsys) == [
        "The version that passed is back. It is fake, 4 of 4, passed, attempt 1. 0 model calls."
    ]
    assert cli.main(["verify", folder]) == 0
    assert _lines(capsys)[1] == "fake. 4 of 4. Pass mark 4. Passed. Attempt 1."
    assert cli.main(["undo", folder, "--profile", profile]) == 2
    assert _lines(capsys) == ["Nothing to undo. This is the version that passed."]


def test_retake_refuses_a_change_it_cannot_examine_and_asks_before_spending(
    placed, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, profile = str(placed.folder), str(placed.profile)
    record = placed.workspace / "retakes"
    placed.reword()
    # No --yes and nobody to ask: the line is shown and nothing is spent.
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert cli.main(["retake", folder, "--profile", profile]) == 2
    assert _lines(capsys)[-2:] == [
        "Not retaken. No attempt was used.", "To go ahead, run it again with --yes.",
    ]
    assert not list(record.glob("*.jsonl"))  # no attempt was recorded
    harness = placed.folder / "artifact" / "harness.py"
    harness.write_text(harness.read_text(encoding="utf-8") + "# mine\n", encoding="utf-8")
    assert cli.main(["retake", folder, "--profile", profile, "--yes", "--json"]) == 2
    refused = json.loads(capsys.readouterr().out)
    assert refused["refused"] == "undo_only" and refused["lines"][0] == STOP
    assert refused["lines"][1] == (
        "Changed: artifact/harness.py, artifact/spec.json. A change to these files can only "
        "be undone."
    )
    assert not list(record.glob("*.jsonl"))  # no attempt was recorded
    assert cli.main(["verify", folder]) == 1
    assert _lines(capsys)[:3] == [
        "refused: export_integrity_mismatch", STOP,
        "Changed: artifact/harness.py, artifact/spec.json. A change to these files can only "
        "be undone.",
    ]
    assert cli.main(["undo", folder, "--profile", profile, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "undo"
    assert cli.main(["undo", folder, "--profile", profile, "--workspace", "w"]) == 1
