"""Phase 10 Section 12.1 (WP3): ``forge run`` builds, licenses, runs once and expires.

Offline only: the fake provider answers ``ok``; Forge runs in ``template`` mode.
The Phase 10 compiler seams (``origin``/``reuse_installed`` keywords,
``CompileResult.route``/``.reused``) belong to WP5; the reuse tests stand in for
them with a test-local compiler stub.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from forge_modes_support import (
    TEMPLATE,
    Consent,
    artifact_dirs,
    block_sockets,
    case_rows,
    forge_repairs,
    logical_lines,
    make_profile,
    services_for,
    spec,
)

from architect.compiler import Compiler
from architect.compiler.types import CompileResult
from architect.forge_modes import ForgeModeError, deliverable_ephemeral, forge_run
from architect.forge_modes._routing import InstalledRef, RouteDecision
from architect.forge_modes.cases import load_cases
from architect.forge_modes.run import with_exact
from architect.library import HarnessLibrary
from architect.spec import FloorDefinition, FloorKind, HarnessArtifact, Lifecycle, TestReport


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    block_sockets(monkeypatch)


def _report(directory: Path) -> TestReport:
    return TestReport.model_validate_json((directory / "report.json").read_text("utf-8"))


def _delivered_dir(root: Path, digest: str) -> Path:
    [directory] = [d for d in artifact_dirs(root) if _report(d).artifact_digest == digest]
    return directory


async def test_template_run_is_licensed_delivered_once_and_expired(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    forge_repairs(monkeypatch)
    services = services_for(make_profile(tmp_path, reply="nope"))
    consent = Consent()
    try:
        # The operator's Spec asks for an installed harness: forge run forces expire.
        source = spec(lifecycle=Lifecycle.INSTALL)
        result = await forge_run(source, {"text": "hi"}, services, TEMPLATE, consent=consent)
    finally:
        services.close()
    assert result.codes == ("forge_run_delivered",)
    assert result.licensed and result.output == "ok" and not result.reused
    assert result.stages == (1, 2, 3, 4)
    assert result.floor_origin == "user_dataset" and result.route is None
    expected = with_exact(source, {"lifecycle": "expire"})
    assert result.spec_hash == expected.spec_hash
    assert result.run_cost_usd == pytest.approx(0.0003)  # three guarded play calls
    assert result.compile_cost_usd > 0
    assert result.build_cost_usd == 0  # template mode: no agent model
    assert set(result.forge_summary) == {
        "checks", "dryruns", "measures", "submissions", "spawns", "agent_cost_usd",
    }
    assert result.forge_summary["submissions"] == 1
    assert result.artifact_digest is not None
    directory = _delivered_dir(tmp_path / "work" / "artifacts", result.artifact_digest)
    assert json.loads((directory / "spec.json").read_text("utf-8"))["lifecycle"] == "expire"
    assert (directory / ".consumed").is_file()
    assert not (directory / "harness.py").exists()
    assert (directory / "forge.json").is_file() and (directory / "report.json").is_file()
    # The licensed EXPIRE snapshot is a pre-consumed library template for Tier B.
    library = HarnessLibrary(tmp_path / "work" / "library")
    template = library.lookup(expected, "fake")
    assert template is not None and template.artifact_digest == result.artifact_digest
    assert (template.directory / ".consumed").is_file()
    # Consent: the role line, then the Phase 9 cap line, unchanged.
    [text] = consent.texts
    assert text.splitlines() == [
        "forge agent: fake (mode template, turns 200)",
        "forge: compile cap $10.00 + forge cap $5.00 = $15.00 (API-equivalent, not a bill); "
        "forge minutes 60; verifications for this spec so far: 0/10",
    ]
    report = _report(directory)
    line = logical_lines(capsys.readouterr().err)[-1]
    assert line == (
        f"forge run: licensed (held_out {report.held_out_count}, held_out_tasks "
        f"{report.floor_score:.2f}>={report.floor_limit:.2f}, verification "
        f"{report.verification_id[:8]}) delivered once, expired; build "
        f"${result.build_cost_usd:.4f} agent (API-equivalent), compile "
        f"${result.compile_cost_usd:.4f}, play ${result.run_cost_usd:.4f}"
    )
    assert line.isascii()


async def test_cancel_is_forge_cancelled_with_nothing_spent(tmp_path: Path) -> None:
    services = services_for(make_profile(tmp_path))
    consent = Consent(False)
    try:
        with pytest.raises(ForgeModeError, match="^forge_cancelled$"):
            await forge_run(spec(), {}, services, TEMPLATE, consent=consent)
        assert services.runtime.store.rows() == []  # not one model call
    finally:
        services.close()
    assert len(consent.texts) == 1
    assert artifact_dirs(tmp_path / "work" / "artifacts") == []


async def test_no_smoke_floor_is_forge_disabled_before_consent(tmp_path: Path) -> None:
    services = services_for(make_profile(tmp_path, smoke=False))
    consent = Consent()
    try:
        with pytest.raises(ForgeModeError, match="^forge_disabled$"):
            await forge_run(spec(), {}, services, TEMPLATE, consent=consent)
    finally:
        services.close()
    assert consent.texts == []


async def test_unlicensed_is_never_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    services = services_for(make_profile(tmp_path, expected="nope"))
    calls: list[Any] = []

    async def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        raise AssertionError("an unlicensed harness must never reach Runtime.run")

    monkeypatch.setattr(services.runtime, "run", spy)
    try:
        result = await forge_run(spec(), {}, services, TEMPLATE, consent=Consent())
    finally:
        services.close()
    assert calls == []
    assert result.codes[0] == "forge_run_unlicensed"
    assert not result.licensed and result.output is None and result.run_cost_usd == 0
    assert all(code.isascii() and " " not in code for code in result.codes)
    # Generated code is discarded; the evidence stays.
    for directory in artifact_dirs(tmp_path / "work" / "artifacts"):
        if (directory / "forge.json").exists():
            assert not (directory / "harness.py").exists()
    assert "forge run: not delivered (forge_run_unlicensed)" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("failure", "code", "licensed"),
    [("budget", "run_budget", True), ("expired", "expired", True),
     ("crash", "run_failed", True), ("unlicensed", "unlicensed", False)],
)
async def test_a_failed_run_is_never_reported_as_delivered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
    code: str,
    licensed: bool,
) -> None:
    from architect.runtime import ArtifactExpiredError, BudgetExceeded, UnlicensedArtifactError

    forge_repairs(monkeypatch)
    services = services_for(make_profile(tmp_path, reply="nope"))
    calls: list[Any] = []

    async def failing(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        raise {
            "budget": BudgetExceeded("over"),
            "expired": ArtifactExpiredError("consumed"),
            "crash": RuntimeError("boom"),
            "unlicensed": UnlicensedArtifactError("no"),
        }[failure]

    monkeypatch.setattr(services.runtime, "run", failing)
    try:
        result = await forge_run(spec(), {"text": "hi"}, services, TEMPLATE, consent=Consent())
    finally:
        services.close()
    assert len(calls) == 1  # the license gate passed and Runtime.run was reached once
    assert "forge_run_delivered" not in result.codes
    assert result.codes[0] == code and result.output is None
    assert result.licensed is licensed
    assert "forge_run_delivered" not in json.dumps(result.model_dump(mode="json"))
    line = logical_lines(capsys.readouterr().err)[-1]
    assert line.startswith(f"forge run: not delivered ({code}); build $")
    # Generated code is still discarded; the evidence stays.
    for directory in artifact_dirs(tmp_path / "work" / "artifacts"):
        if (directory / "forge.json").exists():
            assert not (directory / "harness.py").exists()


def test_deliverable_ephemeral_requires_stage0_only_for_prose(tmp_path: Path) -> None:
    from test_subharness import sign

    directory = tmp_path / "a"
    directory.mkdir()
    (directory / "harness.py").write_text("async def run(c, t):\n    return 1\n", "utf-8")
    artifact = sign(
        HarnessArtifact(
            spec=spec(), preset="extractor", model="fake", components=(), directory=directory
        )
    )
    result = CompileResult(spec=artifact.spec, artifact=artifact, report=artifact.report,
                           stages=(1, 2, 3, 4))
    cap = artifact.spec.budget
    assert deliverable_ephemeral(result, artifact, cap, require_stage0=False)
    assert not deliverable_ephemeral(result, artifact, cap, require_stage0=True)
    staged = result.model_copy(update={"stages": (0, 1, 2, 4)})
    assert deliverable_ephemeral(staged, artifact, cap, require_stage0=True)
    assert not deliverable_ephemeral(
        result.model_copy(update={"stages": (1, 2, 3)}), artifact, cap, require_stage0=False
    )
    small = cap.model_copy(update={"cost_usd": cap.cost_usd / 2})
    assert not deliverable_ephemeral(result, artifact, small, require_stage0=False)
    installed = artifact.spec.model_copy(update={"lifecycle": Lifecycle.INSTALL})
    other = result.model_copy(update={"spec": installed})
    assert not deliverable_ephemeral(other, artifact, cap, require_stage0=False)


# ------------------------------------------------------------------ reuse (WP5 stub)


class _RoutedResult(CompileResult):
    """Test-local stand-in for WP5's ``CompileResult.route``/``.reused``."""

    route: Any = None
    reused: bool = False


def _reusing_compiler(base: Compiler, result: _RoutedResult, seen: list[dict[str, Any]]) -> Any:
    class Reusing(Compiler):
        async def compile(  # type: ignore[override]
            self,
            source: Any,
            *,
            overrides: Any = None,
            approved: bool = False,
            exact_overrides: bool = False,
            origin: str = "cli",
            reuse_installed: bool = False,
            budget_cap: Any = None,
            no_route: bool = False,
        ) -> CompileResult:
            seen.append({"origin": origin, "reuse_installed": reuse_installed})
            return result

    stub = Reusing.__new__(Reusing)
    stub.__dict__.update(base.__dict__)
    return stub


async def _installed(services: Any, name: str) -> HarnessArtifact:
    compiled = await services.compiler.compile(
        spec(lifecycle=Lifecycle.INSTALL), approved=True
    )
    assert compiled.artifact is not None and compiled.artifact.licensed
    services.registry.install(name, compiled.artifact)
    return services.registry.get(name)


@pytest.mark.parametrize("granted", [True, False])
async def test_reuse_installed_runs_the_installed_entry_without_building(
    tmp_path: Path, granted: bool
) -> None:
    import dataclasses

    services = services_for(make_profile(tmp_path))
    try:
        installed = await _installed(services, "ok-task")
        decision = RouteDecision.model_construct(
            decision_id=uuid4(),
            origin="forge_run",
            action="reuse_installed",
            target=InstalledRef(
                name="ok-task",
                revision=1,
                artifact_digest=installed.artifact_digest,
                spec_hash=installed.spec.spec_hash,
            ),
            codes=(),
            explored=False,
        )
        routed = _RoutedResult(
            spec=installed.spec, artifact=installed, report=installed.report,
            stages=(), route=decision, reused=True,
        )
        seen: list[dict[str, Any]] = []
        stub = dataclasses.replace(
            services, compiler=_reusing_compiler(services.compiler, routed, seen)
        )
        before = len(services.runtime.store.rows())
        result = await forge_run(
            spec(), {}, stub, TEMPLATE, consent=Consent(), reuse_installed=granted
        )
        rows = services.runtime.store.rows()[before:]
    finally:
        services.close()
    assert seen == [{"origin": "forge_run", "reuse_installed": granted}]
    if granted:
        assert result.codes == ("forge_run_reused_installed",)
        assert result.reused and result.licensed and result.output == "ok"
        assert result.build_cost_usd == 0 and result.forge_summary == {}
        assert len(rows) == 1 and rows[0].lifecycle == Lifecycle.INSTALL
    else:
        # A reused result without this invocation's grant is never run (P3).
        assert result.codes == ("forge_run_unlicensed",) and result.output is None
        assert rows == []
    # The installed entry is never consumed or discarded.
    assert (installed.directory / "harness.py").is_file()
    assert not (installed.directory / ".consumed").exists()


# ------------------------------------------------------------------ cases


def _write_cases(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


async def test_cases_bind_the_file_hash_and_are_denied_to_pi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import architect.compiler.forge as forge_module

    seen: list[Any] = []
    original = forge_module.forge

    async def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs["pi_config"])
        return await original(*args, **kwargs)

    monkeypatch.setattr(forge_module, "forge", spy)
    # No profile smoke floor: the case file supplies both the floor and the smoke floor.
    services = services_for(make_profile(tmp_path, smoke=False))
    cases = _write_cases(tmp_path / "authored.jsonl", case_rows(8, prefix="u"))
    try:
        result = await forge_run(
            spec(), {"text": "x"}, services, TEMPLATE, consent=Consent(), cases=cases
        )
    finally:
        services.close()
    tag = "cases:" + hashlib.sha256(cases.read_bytes()).hexdigest()[:16]
    floor = FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset=tag)
    expected = with_exact(spec(), {"lifecycle": "expire", "floor": floor.model_dump(mode="json")})
    assert result.spec_hash == expected.spec_hash
    assert result.codes == ("forge_run_delivered",) and result.floor_origin == "authored_cases"
    assert result.artifact_digest is not None
    directory = _delivered_dir(tmp_path / "work" / "artifacts", result.artifact_digest)
    delivered = json.loads((directory / "spec.json").read_text("utf-8"))
    assert delivered["floor"]["dataset"] == tag
    assert len(seen) == 1 and cases.resolve() in set(seen[0].denied_paths)
    # The report is content-free: no case text reached it.
    assert "FINGERPRINT" not in (directory / "report.json").read_text("utf-8")


@pytest.mark.parametrize(
    ("rows", "code"),
    [
        (case_rows(7), "forge_cases_too_few"),
        ([*case_rows(8), case_rows(1)[0]], "forge_cases_invalid"),  # duplicate id
        ([*case_rows(8), {"case_id": "x", "task": {}}], "forge_cases_invalid"),  # no expected
        ([*case_rows(8), {"case_id": "y", "task": {}, "expected": "ok", "x": 1}],
         "forge_cases_invalid"),
        ([*case_rows(8), {"case_id": "z", "task": {"text": 3}, "expected": "ok"}],
         "forge_cases_invalid"),  # task does not match the input schema
        (case_rows(8, expected=3), "forge_cases_invalid"),  # expected is not a string
    ],
)
async def test_invalid_case_files_are_refused_before_consent(
    tmp_path: Path, rows: list[dict[str, Any]], code: str
) -> None:
    services = services_for(make_profile(tmp_path))
    consent = Consent()
    cases = _write_cases(tmp_path / "authored.jsonl", rows)
    try:
        with pytest.raises(ForgeModeError, match=f"^{code}$"):
            await forge_run(spec(), {}, services, TEMPLATE, consent=consent, cases=cases)
    finally:
        services.close()
    assert consent.texts == []
    assert artifact_dirs(tmp_path / "work" / "artifacts") == []


def test_case_file_edge_cases(tmp_path: Path) -> None:
    good = _write_cases(tmp_path / "good.jsonl", case_rows(8))
    cases, tag = load_cases(good, spec())
    assert len(cases) == 8 and tag == "cases:" + hashlib.sha256(good.read_bytes()).hexdigest()[:16]
    judged = spec(floor=FloorDefinition(kind=FloorKind.HUMAN_EDIT_RATE, limit=0.1, dataset="x"))
    with pytest.raises(ForgeModeError, match="^forge_cases_floor_kind$"):
        load_cases(good, judged)
    for content in (b"not json\n", b"[1, 2]\n", b'{"case_id": "a", "task": 1, "expected": NaN}\n',
                    b'{"case_id": "a", "case_id": "b", "task": 1, "expected": 1}\n', b"\xff\n"):
        bad = tmp_path / "bad.jsonl"
        bad.write_bytes(content)
        with pytest.raises(ForgeModeError, match="^forge_cases_invalid$"):
            load_cases(bad, None)
    with pytest.raises(ForgeModeError, match="^forge_cases_invalid$"):
        load_cases(tmp_path / "missing.jsonl", None)
    # Rows only (a prose request): schemas are checked once Stage 0 fixed the Spec.
    loose, _ = load_cases(_write_cases(tmp_path / "loose.jsonl", case_rows(8, expected=3)), None)
    assert len(loose) == 8


# ------------------------------------------------------------------ prose requests


def _inference(tmp_path: Path, inferred: Any) -> dict[str, Any]:
    text = inferred.model_dump_json(exclude={"provenance"})
    response = {"text": text, "model": "fake", "tokens_in": 10, "tokens_out": 10,
                "cost_usd": 0.0}
    (tmp_path / "inference.json").write_text(json.dumps([response] * 4), encoding="utf-8")
    return {"provider": "fake", "model": "fake", "fixture": "inference.json"}


async def test_prose_request_needs_and_gets_stage_0(tmp_path: Path) -> None:
    profile = make_profile(tmp_path, inference=_inference(tmp_path, spec()))
    services = services_for(profile)
    consent = Consent()
    try:
        result = await forge_run("Return ok", {}, services, TEMPLATE, consent=consent)
    finally:
        services.close()
    assert result.codes == ("forge_run_delivered",) and result.output == "ok"
    assert result.stages[0] == 0 and 4 in result.stages
    # The Spec is unknown before Stage 0, so the verification count shows "-".
    assert consent.texts[0].endswith("verifications for this spec so far: -/10")


async def test_prose_cases_floor_kind_is_checked_after_stage_0(tmp_path: Path) -> None:
    judged = spec(floor=FloorDefinition(kind=FloorKind.HUMAN_EDIT_RATE, limit=0.1, dataset="x"))
    profile = make_profile(tmp_path, inference=_inference(tmp_path, judged))
    services = services_for(profile)
    cases = _write_cases(tmp_path / "authored.jsonl", case_rows(8))
    try:
        result = await forge_run(
            "Return ok", {}, services, TEMPLATE, consent=Consent(), cases=cases
        )
    finally:
        services.close()
    # Stage 0 ran; the tagged floor of the wrong kind stops the compile before Stage 1.
    assert result.stages == (0,) and result.output is None and not result.licensed
    assert result.codes[:2] == ("forge_run_unlicensed", "forge_cases_floor_kind")
    assert artifact_dirs(tmp_path / "work" / "artifacts") == []


# ------------------------------------------------------------------ discard_code move


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.mark.parametrize("where", ["inside", "outside", "root"])
def test_discard_code_move_is_byte_identical(tmp_path: Path, where: str) -> None:
    from architect import orchestrator
    from architect.runtime import ephemeral

    assert ephemeral.EPHEMERAL_CODE == orchestrator._EPHEMERAL_CODE
    names = ("harness.py", "pi-pack.json", "preset.json", "rewrite.diff", "report.json",
             "spec.json", "config.json", "forge.json", "notes.txt")
    results = []
    for label, discard in (("old", orchestrator._discard_code), ("new", ephemeral.discard_code)):
        base = tmp_path / label
        root = base / "artifacts"
        directory = {"inside": root / "h" / "1", "outside": base / "registry" / "e",
                     "root": root}[where]
        directory.mkdir(parents=True)
        for name in names:
            (directory / name).write_text(name, encoding="utf-8")
        (directory / "subharnesses" / "c").mkdir(parents=True)
        (directory / "subharnesses" / "c" / "harness.py").write_text("child", encoding="utf-8")
        discard(root, directory)
        results.append(_tree(base))
    assert results[0] == results[1]
    kept = set(results[1])
    if where == "inside":
        assert not any(key.endswith("/1/harness.py") for key in kept)
        assert "artifacts/h/1/report.json" in kept and "artifacts/h/1/forge.json" in kept
        assert "artifacts/h/1/subharnesses/c/harness.py" in kept
    else:
        assert len(kept) == len(names) + 1  # a no-op outside the root and on the root
