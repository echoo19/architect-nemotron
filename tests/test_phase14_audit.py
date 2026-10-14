"""Phase 14 WA, the Forge audit (docs/interfaces/phase14-nebius-evidence.md, Section 8).

Every ``submit_source`` call is copied once, after its verdict, into a directory Forge
cannot read; a write that fails ends the session and promotes nothing. Nothing from the
held-out partition, Stage 4 or a report reaches the audit, a Forge tool result or a
stage or result observer. Offline: a scripted Pi and the fake provider.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import tempfile
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest
from forge_support import (
    CHEAP_WRONG,
    HELD_OUT_MARK,
    REPAIR,
    SECRET,
    ScriptedPi,
    agent_adapter,
    cases,
    ensure_sandbox,
    install_pi,
    play_factory,
    reply,
    spec,
)
from session_support import fake_profile, make_spec

from architect import cli
from architect.backends.pi import PiConfig
from architect.compiler import CompileConfig, Compiler, add_stage_observer, remove_stage_observer
from architect.compiler import forge as forge_module
from architect.compiler import forge_audit as audit_module
from architect.compiler.forge import ForgeResult, forge
from architect.compiler.forge_audit import (
    AUDIT_ENV,
    AuditError,
    AuditSession,
    AuditSessionRecord,
    AuditSubmission,
    ForgeAudit,
    audit_directory,
    current_audit,
    forge_audit,
)
from architect.compiler.forge_settings import ForgeSettings
from architect.compiler.forge_tools import add_result_observer, remove_result_observer
from architect.compiler.tuning import tune
from architect.floors import HeldOutTasks
from architect.floors.provenance import cases_sha256
from architect.library import HarnessLibrary
from architect.models import FakeAdapter, ModelRequest, ModelResponse
from architect.presets import get_preset
from architect.runtime import Runtime
from architect.spec import Budget, CaseCounts, FloorDefinition, HarnessArtifact, Spec
from architect.telemetry import TelemetryStore

INVALID_SOURCE = "async def run(:\n    return 1\n"
SESSION = [
    {"write": {"path": "harness.py", "content": CHEAP_WRONG}},
    {"tool": "forge_submit", "arguments": {"note": "cheap"}},
    {"write": {"path": "harness.py", "content": INVALID_SOURCE}},
    {"tool": "forge_submit", "arguments": {}},
    {"write": {"path": "harness.py", "content": REPAIR}},
    {"tool": "forge_check", "arguments": {}},
    {"tool": "forge_measure", "arguments": {"cases": [0]}},
    {"tool": "forge_submit", "arguments": {"note": "repair"}},
    {"text": "done"},
]
HELD_OUT_IDS = ("case-001", "case-003", "case-005")


@pytest.fixture(autouse=True)
async def working_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    await ensure_sandbox(monkeypatch, tmp_path / "probe")


def _floors(held_out_miss: bool = False) -> Callable[[FloorDefinition], HeldOutTasks]:
    """forge_support's six fingerprinted cases; optionally one held-out case expects
    another answer, so the held-out score (2/3) differs from every tuning number."""
    records = cases(6)
    if held_out_miss:
        records[3] = records[3].model_copy(update={"expected": "other"})

    def factory(definition: FloorDefinition) -> HeldOutTasks:
        return HeldOutTasks(definition, records)

    return factory


def _compiler(
    root: Path,
    settings: ForgeSettings,
    *,
    held_out_miss: bool = False,
    listener: Callable[[ForgeResult], None] | None = None,
    agent: FakeAdapter | None = None,
) -> Compiler:
    floors = _floors(held_out_miss)
    compiler = Compiler(
        root / "artifacts",
        Runtime(TelemetryStore(root / "telemetry")),
        play_factory("nope"),
        floors,
        config=CompileConfig(
            max_candidates=1,
            enable_rewriting=False,
            max_rewrites=0,
            forge=settings,
            compile_budget=Budget(cost_usd=10, compute_s=600),
        ),
        library=HarnessLibrary(root / "library"),
        smoke_factory=floors,
        forge_adapter=agent or agent_adapter(),
        pi_config=PiConfig(),
    )
    if listener is not None:
        compiler.forge_listener = listener
    return compiler


def _files(directory: Path) -> dict[str, str]:
    return {
        path.relative_to(directory).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _session_dir(audit: Path, value: Spec) -> Path:
    sessions = [path for path in (audit / value.spec_hash).iterdir() if path.is_dir()]
    assert len(sessions) == 1
    return sessions[0]


def _clean(text: str, *extra: str) -> None:
    for forbidden in (HELD_OUT_MARK, SECRET, *HELD_OUT_IDS, *extra):
        assert forbidden not in text, forbidden


# -- the API ------------------------------------------------------------------------------


def test_forge_audit_creates_probes_and_refuses_unwritable_directories(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "audit"
    audit = ForgeAudit(target)
    assert audit.directory == target.resolve() and target.is_dir()
    assert list(target.iterdir()) == []  # the probe file was removed
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(AuditError):
        ForgeAudit(blocker)
    with pytest.raises(AuditError):
        with forge_audit(blocker / "below"):
            pass
    assert isinstance(AuditError("x"), OSError)


def test_audit_directory_prefers_the_flag_then_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(AUDIT_ENV, raising=False)
    assert audit_directory(None) is None
    monkeypatch.setenv(AUDIT_ENV, str(tmp_path / "env"))
    assert audit_directory(None) == tmp_path / "env"
    assert audit_directory(tmp_path / "flag") == tmp_path / "flag"
    monkeypatch.setenv(AUDIT_ENV, "  ")
    assert audit_directory(None) is None


async def test_the_audit_is_a_context_variable_not_a_process_observer(tmp_path: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    seen: list[object] = []

    async def other_task() -> None:
        started.set()
        await release.wait()
        seen.append(current_audit())

    outside = asyncio.create_task(other_task())  # created before the block: unaffected
    await started.wait()
    with forge_audit(None) as nothing:
        assert nothing is None and current_audit() is None
    with forge_audit(tmp_path / "audit") as audit:
        assert audit is not None and current_audit() is audit

        async def inner() -> object:
            return current_audit()

        assert await asyncio.create_task(inner()) is audit  # tasks started inside inherit
        release.set()
        await outside
    assert seen == [None]
    assert current_audit() is None


# -- sessions -----------------------------------------------------------------------------


async def test_agent_session_records_every_submission_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pi = install_pi(monkeypatch, ScriptedPi(SESSION))
    value = spec(invalid_policy="count_as_miss")
    audit = tmp_path / "audit"
    with forge_audit(audit):
        result = await _compiler(tmp_path, ForgeSettings(enabled=True)).compile(value)
    artifact = result.artifact
    assert artifact is not None and artifact.licensed and artifact.report is not None
    assert pi.config is not None and audit.resolve() in pi.config.denied_paths
    folder = _session_dir(audit, value)
    session = AuditSessionRecord.model_validate_json(
        (folder / "session.json").read_text(encoding="utf-8")
    )
    assert (session.spec_hash, session.mode, session.model, session.agent_model) == (
        value.spec_hash, "agent", "fake", "fake"
    )
    assert session.depth == 0 and session.submissions == 3 and session.promoted == 3
    assert session.ended_at is not None and session.failures == ()
    submissions = {
        number: AuditSubmission.model_validate_json(
            (folder / str(number) / "submission.json").read_text(encoding="utf-8")
        )
        for number in (1, 2, 3)
    }
    assert [submissions[n].verdict for n in (1, 2, 3)] == [
        "tuning_failed", "forge_invalid_source", "accepted"
    ]
    for number, source in ((1, CHEAP_WRONG), (2, INVALID_SOURCE), (3, REPAIR)):
        record = submissions[number]
        assert (folder / str(number) / "harness.py").read_bytes() == source.encode()
        assert record.code_sha256 == hashlib.sha256(source.encode()).hexdigest()
        assert (record.session_id, record.submission) == (session.session_id, number)
        assert record.floor_limit == 1.0 and record.mode == "agent"
    wrong, invalid, accepted = submissions[1], submissions[2], submissions[3]
    assert wrong.tuning is not None and wrong.tuning.score == 0.0
    assert wrong.tuning.counts == CaseCounts(cases=3, correct=0, invalid=0, over_budget=0)
    assert invalid.tuning is None and invalid.artifact_digest is None
    assert not (folder / "2" / "spec.json").exists()
    assert accepted.tuning is not None and accepted.tuning.score == 1.0
    assert accepted.tuning.counts == CaseCounts(cases=3, correct=3, invalid=0, over_budget=0)
    assert accepted.smoke_passed is True
    # Each measurement names the practice cases it ran on (their hash, tuning-side
    # only), so the bench replay can refuse a floor the submission never practised on.
    practice = cases_sha256(_floors()(value.floor).tuning)
    assert wrong.tuning.cases_sha256 == accepted.tuning.cases_sha256 == practice
    # The copy is the measured candidate (promotion only gave it a new version number),
    # ready for the bench replay to load.
    copied = HarnessArtifact.load(folder / "3")
    assert copied.artifact_digest == accepted.artifact_digest and not copied.licensed
    assert copied.spec.spec_hash == artifact.spec.spec_hash
    assert (folder / "3" / "harness.py").read_bytes() == (
        artifact.directory / "harness.py"
    ).read_bytes()
    assert copied.model_copy(update={"version": artifact.version}).artifact_digest == (
        artifact.artifact_digest
    )
    for name, text in _files(audit).items():
        assert text.isascii(), name
        _clean(text, artifact.report.verification_id, "held_out_counts")


async def test_template_session_is_audited_the_same_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pi = install_pi(monkeypatch, ScriptedPi([{"text": "never"}]))
    monkeypatch.setattr(forge_module, "instantiate", lambda name, candidate: REPAIR)
    settings = ForgeSettings(enabled=True, mode="template", template="prompt_chain")
    value = spec()
    with forge_audit(tmp_path / "audit"):
        result = await _compiler(tmp_path, settings).compile(value)
    assert pi.runs == 0 and result.artifact is not None and result.artifact.licensed
    folder = _session_dir(tmp_path / "audit", value)
    session = AuditSessionRecord.model_validate_json(
        (folder / "session.json").read_text(encoding="utf-8")
    )
    assert (session.mode, session.agent_model, session.submissions) == ("template", None, 1)
    record = AuditSubmission.model_validate_json(
        (folder / "1" / "submission.json").read_text(encoding="utf-8")
    )
    assert record.verdict == "accepted" and record.agent_model is None
    assert (folder / "1" / "harness.py").read_bytes() == REPAIR.encode()


async def test_no_audit_writes_nothing_and_keeps_todays_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pi = install_pi(monkeypatch, ScriptedPi(SESSION))
    result = await _compiler(tmp_path, ForgeSettings(enabled=True)).compile(spec())
    assert result.artifact is not None and result.artifact.licensed
    assert pi.config is not None
    # Only the private store is added to the (empty) configured denials, as before.
    (private,) = pi.config.denied_paths
    assert private.name.startswith("architect-forge-private-")


async def test_a_failed_record_ends_the_session_and_promotes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pi = install_pi(
        monkeypatch,
        ScriptedPi(
            [
                {"write": {"path": "harness.py", "content": REPAIR}},
                {"tool": "forge_submit", "arguments": {}},
                {"tool": "forge_check", "arguments": {}},  # never reached
                {"text": "done"},
            ]
        ),
    )
    written = audit_module._write_json

    def failing(path: Path, record: Any) -> None:
        if path.name == "submission.json":
            raise OSError("disk full")
        written(path, record)

    monkeypatch.setattr(audit_module, "_write_json", failing)
    forged: list[ForgeResult] = []
    value = spec()
    with forge_audit(tmp_path / "audit"):
        result = await _compiler(tmp_path, ForgeSettings(enabled=True),
                                 listener=forged.append).compile(value)
    assert [name for name, _ in pi.results] == []  # the session ended at the submit
    assert len(forged) == 1 and "forge_audit_failed" in forged[0].failures
    assert forged[0].submissions == 1 and forged[0].accepted == 1
    # Stage 4 ran on the Stage 2 baseline, which fails here: nothing was promoted.
    assert result.artifact is not None and result.artifact.version == 1
    assert not result.artifact.licensed and "forge_audit_failed" in result.failures
    assert not (tmp_path / "artifacts" / value.spec_hash / "2").exists()
    folder = _session_dir(tmp_path / "audit", value)
    session = json.loads((folder / "session.json").read_text(encoding="utf-8"))
    assert session["failures"] == ["forge_audit_failed"] and session["ended_at"]


async def _direct(tmp_path: Path, settings: ForgeSettings) -> tuple[Any, ForgeResult]:
    value = spec()
    runtime = Runtime(TelemetryStore(tmp_path / "telemetry"))
    factory = play_factory("nope")
    runtime.adapter_factory = factory
    floors = _floors()
    tuned = await tune(
        value, get_preset("synthesizer"), "fake", floors(value.floor), factory, runtime,
        tmp_path / "artifacts", max_candidates=1,
    )
    assert tuned.best is not None
    outcome = await forge(
        tuned.best, get_preset("synthesizer"), floors(value.floor), floors(value.floor),
        factory, runtime, tmp_path / "artifacts", agent_adapter(), settings,
        pi_config=PiConfig(), floor_factory=floors, library=HarnessLibrary(tmp_path / "lib"),
    )
    return tuned.best, outcome


async def test_an_unopenable_session_runs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pi = install_pi(monkeypatch, ScriptedPi(SESSION))

    def refuse(self: ForgeAudit, **kwargs: Any) -> AuditSession:
        raise AuditError("audit_unwritable")

    monkeypatch.setattr(ForgeAudit, "open_session", refuse)
    with forge_audit(tmp_path / "audit"):
        best, outcome = await _direct(tmp_path, ForgeSettings(enabled=True))
    assert pi.runs == 0 and outcome.failures == ("forge_audit_failed",)
    assert outcome.best == best and outcome.submissions == 0


async def test_a_failed_close_discards_the_promoted_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_pi(monkeypatch, ScriptedPi(SESSION))

    def refuse(self: AuditSession, **kwargs: Any) -> None:
        self.failed = True
        raise AuditError("audit_unwritable")

    monkeypatch.setattr(AuditSession, "close", refuse)
    with forge_audit(tmp_path / "audit"):
        best, outcome = await _direct(tmp_path, ForgeSettings(enabled=True))
    assert outcome.accepted == 1 and "forge_audit_failed" in outcome.failures
    assert outcome.best == best  # the Stage 2 candidate, never the unaudited harness
    assert not (tmp_path / "artifacts" / spec().spec_hash / "2").exists()


async def test_forge_never_works_inside_the_audit_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pi = install_pi(monkeypatch, ScriptedPi(SESSION))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))  # Forge's workspace lands here
    with forge_audit(scratch):
        best, outcome = await _direct(tmp_path, ForgeSettings(enabled=True))
    assert pi.runs == 0 and outcome.failures == ("forge_failed",)
    assert outcome.best == best
    assert not [path for path in scratch.rglob("session.json")]


ROOT = Path(__file__).resolve().parents[1]
PI_READY = (
    ROOT / "architect/backends/pi-node/node_modules/@mariozechner/pi-coding-agent/package.json"
).is_file()


class Recording(FakeAdapter):
    """The Forge agent's scripted replies; every prompt it was shown is kept."""

    def __init__(self, texts: list[str]) -> None:
        super().__init__("fake", responses=[reply(text, cost=0.0001) for text in texts])
        self.prompts: list[str] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.prompts.append(request.prompt)
        return await super().complete(request)


def _call(name: str, **arguments: Any) -> str:
    call = {"id": f"call-{name}", "name": name, "arguments": arguments}
    return json.dumps({"tool_calls": [call]})


@pytest.mark.skipif(not PI_READY, reason="Pi dependencies are not installed")
async def test_real_pi_session_is_audited_and_never_reads_the_audit(tmp_path: Path) -> None:
    audit = tmp_path / "audit"
    planted = "PLANTED_AUDIT_TOKEN_0001"
    agent = Recording(
        [
            _call("read", path=str((audit / "planted.txt").resolve())),
            _call("write", path="harness.py", content=REPAIR),
            _call("forge_submit", note="repair"),
            "Submitted.",
        ]
    )
    value = spec()
    with forge_audit(audit):
        (audit / "planted.txt").write_text(planted, encoding="utf-8")
        result = await _compiler(tmp_path, ForgeSettings(enabled=True), agent=agent).compile(
            value
        )
    assert result.artifact is not None and result.artifact.licensed
    assert agent.prompts and not any(planted in prompt for prompt in agent.prompts)
    record = AuditSubmission.model_validate_json(
        (_session_dir(audit, value) / "1" / "submission.json").read_text(encoding="utf-8")
    )
    assert record.verdict == "accepted"


# -- held-out isolation -------------------------------------------------------------------


async def test_observers_tools_and_audit_never_see_held_out_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pi = install_pi(monkeypatch, ScriptedPi(SESSION))
    stages: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    add_stage_observer(stages.append)
    add_result_observer(results.append)
    value = spec(invalid_policy="count_as_miss")
    try:
        with forge_audit(tmp_path / "audit"):
            result = await _compiler(
                tmp_path, ForgeSettings(enabled=True), held_out_miss=True
            ).compile(value)
    finally:
        remove_stage_observer(stages.append)
        remove_result_observer(results.append)
    report = result.report
    assert report is not None and report.floor_score == pytest.approx(2 / 3)
    assert report.held_out_counts == CaseCounts(cases=3, correct=2, invalid=0, over_budget=0)
    assert [stage["stage"] for stage in stages] == [1, 2, 3, 4]
    assert {"forge_submit", "forge_session"} <= {item["tool"] for item in results}
    held_out = (report.verification_id, str(report.floor_score), "held_out_counts",
                '"correct": 2')
    _clean(json.dumps(stages), *held_out)
    _clean(json.dumps(results), *held_out)
    _clean(json.dumps([value for _, value in pi.results]), *held_out)
    for text in _files(tmp_path / "audit").values():
        _clean(text, *held_out)


# -- the CLI flag -------------------------------------------------------------------------


def _cli(argv: list[str]) -> tuple[int, str]:
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        code = cli.main(argv)
    return code, stderr.getvalue()


def test_cli_refuses_an_unwritable_audit_directory_before_any_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = fake_profile(tmp_path)
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(make_spec().model_dump_json(), encoding="utf-8")
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.delenv(AUDIT_ENV, raising=False)
    code, err = _cli(["compile", str(spec_file), "--profile", str(profile), "--audit",
                      str(blocker)])
    assert code == 1 and json.loads(err) == {"error": "audit_unwritable"}
    assert not (profile.parent / "telemetry").exists()  # nothing ran
    monkeypatch.setenv(AUDIT_ENV, str(blocker))
    code, err = _cli(["compile", str(spec_file), "--profile", str(profile)])
    assert code == 1 and json.loads(err) == {"error": "audit_unwritable"}
    for argv in (["forge", "run", "x", "--profile", "p", "--input", "{}"],
                 ["forge", "integrate", "x", "--profile", "p", "--repo", ".", "--into", "y"]):
        args = cli._parser().parse_args(argv)
        with pytest.raises(cli.CLIError, match="audit_unwritable"):
            cli._forge_audit(args)
    run_args = cli._parser().parse_args(["run", "dir", "--profile", "p", "--input", "{}"])
    assert cli._forge_audit(run_args) is None  # only the compile family is audited


def test_cli_audit_flag_exists_on_the_compile_family_only() -> None:
    parser = cli._parser()
    for argv in (
        ["compile", "x", "--profile", "p"],
        ["create", "x", "--profile", "p", "--repo", "."],
        ["do", "x", "--profile", "p", "--repo", "."],
        ["recompile", "x", "--profile", "p"],
        ["forge", "run", "x", "--profile", "p", "--input", "{}"],
        ["forge", "integrate", "x", "--profile", "p", "--repo", ".", "--into", "y"],
    ):
        assert parser.parse_args([*argv, "--audit", "dir"]).audit == Path("dir")
    with pytest.raises(cli.CLIError):
        parser.parse_args(["run", "dir", "--profile", "p", "--input", "{}", "--audit", "d"])


def test_cli_audit_flag_on_a_compile_without_forge_writes_no_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(AUDIT_ENV, raising=False)
    profile = fake_profile(tmp_path)
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(make_spec().model_dump_json(), encoding="utf-8")
    audit = tmp_path / "audit"
    stdout = io.StringIO()
    with redirect_stderr(io.StringIO()), redirect_stdout(stdout):
        code = cli.main(["compile", str(spec_file), "--profile", str(profile),
                         "--approved-spec", str(spec_file), "--audit", str(audit)])
    assert code == 0, stdout.getvalue()
    assert audit.is_dir() and list(audit.iterdir()) == []
    payload = json.loads(stdout.getvalue())
    assert payload["accepted"] is True
    assert payload["report"]["held_out_counts"] == {
        "cases": 3, "correct": 3, "invalid": 0, "over_budget": 0
    }


def test_audit_records_validate_their_identifiers() -> None:
    valid: dict[str, Any] = {
        "audit_version": 1, "spec_hash": "a" * 64, "session_id": "b" * 32, "submission": 1,
        "depth": 0, "mode": "agent", "model": "fake", "agent_model": None,
        "code_sha256": "0" * 64, "artifact_digest": None, "verdict": "accepted",
        "floor_limit": 1.0, "tuning": None, "smoke_passed": None,
        "recorded_at": "2026-10-03T12:00:00+00:00",
    }
    AuditSubmission.model_validate(valid)
    for key, value in (("spec_hash", "../x"), ("session_id", "y"), ("submission", 0),
                       ("verdict", "Accepted!"), ("mode", "pi"), ("extra", 1)):
        with pytest.raises(ValueError):
            AuditSubmission.model_validate({**valid, key: value})
