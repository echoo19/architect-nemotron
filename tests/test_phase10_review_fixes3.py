"""Phase 10 WP5 review fixes, third round: one regression test per reviewed defect.

Offline only: the fake provider, blocked sockets, Forge in template mode and the fake
Pi peer of ``test_phase10_review_fixes``. No live provider, no ``claude``/``codex`` CLI
and no Ollama is ever called.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
from forge_modes_support import block_sockets, forge_repairs, make_profile, spec
from session_support import fake_profile, make_spec, run_gated, run_script, unwrap
from test_phase10_review_fixes import _fake_pi

import architect.cli as cli
import architect.forge_modes as forge_modes
from architect.forge_modes import export_package, place_package, verify_export
from architect.forge_modes.export import _canonical, read_manifest
from architect.profiles import Services, build_services, load_profile
from architect.session.state import State
from architect.spec import HarnessArtifact, Lifecycle

BUILDER_FLOOR = {"kind": "held_out_tasks", "limit": 1, "dataset": "tasks"}
FOOTER = re.compile(r"^spend  session \$", re.MULTILINE)


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


def _last_error(err: str) -> str:
    return str(json.loads(err.strip().splitlines()[-1])["error"])


def _install_and_export(profile: Path) -> tuple[Services, HarnessArtifact, Path]:
    """Install a native INSTALL entry and stage its export package (no repository)."""
    services = services_of(profile)
    target = spec(lifecycle=Lifecycle.INSTALL)
    result = asyncio.run(services.compiler.compile(target, approved=True, no_route=True))
    assert result.artifact is not None and result.artifact.licensed, result.failures
    assert result.artifact.execution_mode == "native"
    services.registry.install("invoices", result.artifact)
    entry = services.registry.status("invoices")
    manifest, _ = export_package(entry, services.workspace / "exports", "ok_feature")
    staged = services.workspace / "exports" / manifest.artifact_digest[:16] / "ok_feature"
    return services, services.registry.get("invoices"), staged


# ------------------ 1. --reverify never executes the package's shipped harness code


def test_reverify_refuses_a_tampered_placed_package_and_never_runs_its_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = make_profile(tmp_path)
    services, _artifact, staged = _install_and_export(profile)
    reverify_root = services.compiler.artifact_root / "reverify"
    services.close()
    repo = tmp_path / "repo"
    repo.mkdir()
    placed = place_package(staged, repo, "pkgs")
    package = repo / "pkgs" / placed.package
    marker = tmp_path / "executed.txt"
    harness = package / "artifact" / "harness.py"
    with harness.open("a", encoding="utf-8") as handle:
        handle.write(f"\nimport pathlib\npathlib.Path({str(marker)!r}).write_text('x')\n")
    code, out, err = _cli(
        ["export", "verify", str(package), "--profile", str(profile), "--reverify", "--yes",
         "--json"],
        capsys,
    )
    assert code == 1
    assert _last_error(err) == "export_integrity_mismatch"
    assert not marker.exists()  # the tampered harness never ran in this process
    assert "reverify: Stage 4" not in err  # refused before the paid consent
    assert not reverify_root.exists()  # no copy and no new license was minted
    assert out == ""


def test_reverify_on_a_host_without_the_installed_entry_runs_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The package verifies (integrity, pins, children) but this host never installed its
    # artifact: Stage 4 would have to execute shipped code, so it is refused; another
    # host mints its own license only through ``compile --approved-spec``.
    home = tmp_path / "home"
    home.mkdir()
    services, _artifact, staged = _install_and_export(make_profile(home))
    services.close()
    other = tmp_path / "other"
    other.mkdir()
    profile = make_profile(other)
    foreign = services_of(profile)
    reverify_root = foreign.compiler.artifact_root / "reverify"
    foreign.close()
    check = verify_export(staged, registry=None)
    assert check.integrity and check.pins and check.children
    code, _out, err = _cli(
        ["export", "verify", str(staged), "--profile", str(profile), "--reverify", "--yes",
         "--json"],
        capsys,
    )
    assert code == 1 and _last_error(err) == "export_unverified"
    assert "reverify: Stage 4" not in err
    assert not reverify_root.exists()


def test_reverify_stage4_runs_the_host_owned_installed_copy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import architect.session.exports as exports

    profile = make_profile(tmp_path)
    services, installed, staged = _install_and_export(profile)
    services.close()
    copied: list[Path] = []
    original = exports.shutil.copytree

    def spy(source: Path, target: Path, **kw: Any) -> Any:
        copied.append(Path(source).resolve())
        return original(source, target, **kw)

    monkeypatch.setattr(exports.shutil, "copytree", spy)
    code, out, err = _cli(
        ["export", "verify", str(staged), "--profile", str(profile), "--reverify", "--yes",
         "--json"],
        capsys,
    )
    assert code == 0, err
    assert json.loads(out)["reverify"]["licensed"] is True
    # Stage 4 ran a private copy of the host's installed entry, never of the package.
    assert copied == [installed.directory.resolve()]


# ------------- 2. session forge integrate binds the hash forge_integrate returned


def _repack_staging(staged: Path) -> None:
    readme = staged / "README.md"
    readme.write_text("repacked in staging\n", encoding="utf-8")
    manifest, _ = read_manifest(staged)
    digest = hashlib.sha256(readme.read_bytes()).hexdigest()
    files = tuple(
        (name, digest if name == "README.md" else sha) for name, sha in manifest.files
    )
    repacked = manifest.model_copy(update={"files": files})
    (staged / "MANIFEST.json").write_bytes(_canonical(repacked.model_dump(mode="json")))


def test_session_forge_integrate_refuses_a_staging_package_repacked_before_consent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forge_repairs(monkeypatch)
    replies = _fake_pi(monkeypatch)
    original = forge_modes.forge_integrate

    async def tampered(*args: Any, **kw: Any) -> Any:
        result = await original(*args, **kw)
        _repack_staging(Path(str(result.export["package_path"])))
        return result

    monkeypatch.setattr(forge_modes, "forge_integrate", tampered)
    session_profile(tmp_path, lifecycle="install", forge=True, builder_floor=BUILDER_FLOOR)
    (tmp_path / "repo").mkdir()
    target = make_spec(lifecycle="install")
    token = target.spec_hash[:8]
    code, transcript = run_script(
        [f"spec {target.model_dump_json()}", f"approve {token}", "again",
         "forge integrate repo pkgs", "y", "y", "quit"],
        tmp_path,
    )
    assert code == 0 and "Traceback" not in transcript
    text = unwrap(transcript)
    assert "error: export_integrity_mismatch" in text
    assert "integrate: placed" not in text and "export: architect_" not in text
    assert replies == []  # the builder never ran
    assert not (tmp_path / "repo" / "pkgs").exists()


# ------------------------- 3. route --run needs --yes even with a standing cli grant


def test_route_run_without_yes_runs_nothing_even_with_a_standing_cli_grant(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = make_profile(tmp_path, routing={"reuse_origins": ["cli"]})
    services = services_of(profile)
    try:
        result = asyncio.run(
            services.compiler.compile(
                spec(lifecycle=Lifecycle.INSTALL), approved=True, no_route=True
            )
        )
        assert result.artifact is not None and result.artifact.licensed
        services.registry.install("invoices", result.artifact)
    finally:
        services.close()
    path = tmp_path / "spec.json"
    path.write_text(spec(lifecycle=Lifecycle.INSTALL).model_dump_json(), encoding="utf-8")
    run = ["route", str(path), "--profile", str(profile), "--run", '{"text": "hi"}']
    code, out, err = _cli(run, capsys)
    assert code == 1 and _last_error(err) == "route_no_reuse"
    assert out == ""
    code, out, err = _cli([*run, "--yes"], capsys)
    assert code == 0, err
    assert json.loads(out)["reused"] is True


# ------------- 4. forge integrate on an expire Spec says set lifecycle install first


def test_session_forge_integrate_on_an_expire_spec_hints_set_lifecycle_install(
    tmp_path: Path,
) -> None:
    session_profile(tmp_path, forge=True, routing={}, builder_floor=BUILDER_FLOOR)
    (tmp_path / "repo").mkdir()
    target = make_spec()  # lifecycle expire
    token = target.spec_hash[:8]
    code, transcript = run_script(
        [f"spec {target.model_dump_json()}", "forge integrate repo pkgs", "quit"], tmp_path
    )
    assert code == 0 and "Traceback" not in transcript
    text = unwrap(transcript)
    assert "error: approved_feature_must_be_installed" in text
    assert "error: approval_required" not in text
    assert f"next: set lifecycle install, then approve {token}" in text
    assert "No explanation on file" not in text


# ------------------- 5. route_forge_suggested advice follows the Spec's lifecycle


def _forge_suggested(tmp_path: Path, lifecycle: str) -> str:
    session_profile(tmp_path, lifecycle=lifecycle, held_out_expected="bad", routing={})
    target = make_spec(lifecycle=lifecycle)
    token = target.spec_hash[:8]
    first = f"approve {token}" if lifecycle == "install" else "compile"
    code, transcript = run_script(
        [f"spec {target.model_dump_json()}", first, "again", "route", "quit"], tmp_path
    )
    assert code == 0 and "Traceback" not in transcript
    text = unwrap(transcript)
    assert "route_forge_suggested" in text[text.index("> again"):]
    return text


def test_forge_suggested_advice_on_an_expire_spec_never_says_approve(tmp_path: Path) -> None:
    text = _forge_suggested(tmp_path, "expire")
    token = make_spec().spec_hash[:8]
    after = text[text.index("> again"):]
    nexts = [line for line in after.splitlines() if line.startswith("next: forge on")]
    assert len(nexts) == 2, after  # the REVIEW line and the route screen
    for line in nexts:
        assert line.startswith("next: forge on, then compile | forge run {json}"), line
        assert f"approve {token}" not in line.split("forge integrate")[0]
        assert (
            f"forge integrate <repo> <into> (set lifecycle install, then approve {token})"
            in line
        )


def test_forge_suggested_advice_on_an_install_spec_is_unchanged(tmp_path: Path) -> None:
    text = _forge_suggested(tmp_path, "install")
    token = make_spec(lifecycle="install").spec_hash[:8]
    assert (
        f"next: forge on, then approve {token} | forge run {{json}} (lifecycle expire) | "
        "forge integrate <repo> <into> (after approve)"
    ) in text


# ------------------- 6. a standing-grant reuse approve prints no compile banner


def test_standing_grant_reuse_approve_prints_a_routing_banner_not_a_compile_banner(
    tmp_path: Path,
) -> None:
    session_profile(tmp_path, lifecycle="install", routing={"reuse_origins": ["session"]})
    target = make_spec(lifecycle="install")
    token = target.spec_hash[:8]
    code, transcript = run_script(
        [f"spec {target.model_dump_json()}", f"approve {token}", f"install invoices {token}",
         "again", f"approve {token}", "quit"],
        tmp_path,
    )
    assert code == 0 and "Traceback" not in transcript
    text = unwrap(transcript)
    approved = text[text.rindex(f"> approve {token}"):]
    assert f"compiling {token}  model" not in approved
    assert (
        f"routing {token} to installed invoices@1  no Stage 1 to 4 and no compile spend"
        in approved
    )
    assert "a compile runs only if the route changes" in approved
    # The first approve (no installed entry yet) keeps the Phase 9 compile banner.
    first = text[:text.rindex(f"> approve {token}")]
    assert f"compiling {token}  model fake  compile budget" in first


# -------------------- 7. cancelling forge run: one footer and the cancel summary


def _footers_between(text: str, start: str, end: str) -> int:
    begin = text.index(start)
    return len(FOOTER.findall(text[begin:text.index(end, begin)]))


def test_cancelled_forge_run_prints_one_footer_and_the_cancel_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forge_repairs(monkeypatch)
    session_profile(tmp_path, forge=True)
    target = make_spec()
    # An operator at a terminal: cancel is typed while the build runs, the next line
    # only once the session is back in REVIEW.
    code, transcript, session = run_gated(
        [(f"spec {target.model_dump_json()}", State.READY),
         ('forge run {"text": "hi"}', State.REVIEW), ("y", State.REVIEW),
         ("cancel", State.COMPILING), ("route", State.REVIEW), ("quit", State.REVIEW)],
        tmp_path,
    )
    assert code == 0 and "Traceback" not in transcript
    assert "error: cancelled" in transcript
    # The interactive console echoes no input: the next REVIEW prompt ends the cancel.
    assert _footers_between(transcript, "error: cancelled", f"review {target.spec_hash[:8]}> ") == 1
    summary = unwrap(transcript)
    assert re.search(
        r"cancelled after \$\d+\.\d{4}, \S+; unlicensed candidate directories may remain "
        rf"under artifacts/{target.spec_hash}/ and are never licensed",
        summary,
    ), summary
    assert session.state is State.EXIT


def test_quit_during_forge_run_prints_one_footer_before_bye(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forge_repairs(monkeypatch)
    session_profile(tmp_path, forge=True)
    code, transcript = run_script(
        [f"spec {make_spec().model_dump_json()}", 'forge run {"text": "hi"}', "y", "quit"],
        tmp_path,
    )
    assert code == 0 and "Traceback" not in transcript
    assert "error: cancelled" in transcript
    assert _footers_between(transcript, "error: cancelled", "bye") == 1
    assert "cancelled after $" in transcript
