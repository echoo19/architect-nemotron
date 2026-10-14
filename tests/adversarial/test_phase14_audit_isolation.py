"""Phase 14 review: nothing from Stage 4 reaches the Forge audit directory.

Section 8.4 of docs/interfaces/phase14-nebius-evidence.md says the audit holds the
submitted code, its spec and tuning-side numbers only: "Nothing in the audit comes from
the held-out partition, from Stage 4 or from a report." A submission that binds a
``forge_spawn`` child carries that child's licensed artifact under ``subharnesses/``,
and the child's directory holds its own signed Stage 4 ``report.json`` (verification
id, held-out score, held-out counts). The audit must not copy it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from forge_support import ensure_sandbox, floor_factory, play_factory, spec

from architect.compiler.forge_audit import ForgeAudit
from architect.compiler.forge_settings import ForgeSettings
from architect.compiler.forge_tools import ForgeSession, forge_tools
from architect.compiler.tuning import tune
from architect.presets import get_preset
from architect.runtime import Runtime
from architect.runtime.wallet import compilation_wallet
from architect.spec import Budget
from architect.telemetry import TelemetryStore

BINDS_CHILD = (
    "from pydantic import JsonValue\n"
    "from architect.runtime import RunContext\n\n"
    "async def run(context: RunContext, task: JsonValue) -> JsonValue:\n"
    '    return await context.harness("helper", "item 0")\n'
)
AUTHORED = {"authored": [{"input": f"item {i}", "expected": "ok"} for i in range(8)]}


async def test_a_spawned_childs_signed_report_never_reaches_the_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await ensure_sandbox(monkeypatch, tmp_path / "probe")
    settings = ForgeSettings(enabled=True)
    value = spec()
    floors = floor_factory(6)
    floor = floors(value.floor)
    factory = play_factory("ok")
    store = TelemetryStore(tmp_path / "telemetry")
    try:
        runtime = Runtime(store)
        runtime.adapter_factory = factory
        tuned = await tune(
            value, get_preset("synthesizer"), "fake", floor, factory, runtime,
            tmp_path / "artifacts", max_candidates=1,
        )
        assert tuned.best is not None
        workspace, private = tmp_path / "workspace", tmp_path / "private"
        workspace.mkdir()
        audit_root = tmp_path / "audit"
        audit = ForgeAudit(audit_root).open_session(
            spec_hash=value.spec_hash, model="fake", agent_model="fake", mode="agent", depth=0
        )
        session = ForgeSession(
            candidate=tuned.best,
            floor=floor,
            smoke_floor=floors(value.floor),
            adapter_factory=factory,
            runtime=runtime,
            settings=settings,
            workspace=workspace,
            private=private,
            floor_factory=floors,
            audit=audit,
        )
        tools = forge_tools(session)
        with compilation_wallet(Budget(cost_usd=settings.max_usd, compute_s=settings.max_s)) as w:
            session.wallet = w
            spawned = await tools["forge_spawn"].callback(
                {
                    "name": "helper",
                    "goal": "Return ok",
                    "input_schema": {"type": "string"},
                    "output_schema": {"type": "string"},
                    "cases": AUTHORED,
                }
            )
            assert isinstance(spawned, dict) and spawned["licensed"] is True
            (workspace / "harness.py").write_text(BINDS_CHILD, encoding="utf-8", newline="\n")
            submitted = await tools["forge_submit"].callback({})
            assert isinstance(submitted, dict)
    finally:
        store.close()

    child = session.children["helper"].artifact
    assert child.report is not None
    verification_id = child.report.verification_id
    recorded = sorted(audit_root.rglob("submission.json"))
    assert len(recorded) == 1  # the submission was audited, with its candidate written

    reports = sorted(path.relative_to(audit_root).as_posix()
                     for path in audit_root.rglob("report.json"))
    assert reports == [], f"Stage 4 reports copied into the audit: {reports}"
    for path in audit_root.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            assert verification_id not in text, path.relative_to(audit_root).as_posix()
            assert "held_out_counts" not in text, path.relative_to(audit_root).as_posix()
