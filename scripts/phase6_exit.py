"""Native Phase 6 exit on synthetic local fixtures, not official benchmark evidence.

Default: propose only. --approve-fixtures explicitly approves the exact generated
fixture spec for this run. Use a new --workspace; existing evidence is never replaced.
The fixture player reads public observations, not hidden puzzle solutions.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from architect.games.competition import CompetitionDescription
from architect.games.factory import GameRunResult
from architect.games.local import FAMILIES
from architect.models import ModelRequest, ModelResponse
from architect.models.types import UsageQuote
from architect.spec import Spec

ROOT = Path(__file__).resolve().parents[1]
_fixture_spec = importlib.util.spec_from_file_location(
    "phase6_offline_fixture", ROOT / "examples/arc/competition.py"
)
assert _fixture_spec is not None and _fixture_spec.loader is not None
_fixture = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(_fixture)


class PricedFixture(_fixture.OfflineFixtureModel):
    """Synthetic usage prices test registry acceptance, not real provider billing."""

    price = 0.00001

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=100, tokens_out=100, cost_usd=self.price)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await super().complete(request)
        return response.model_copy(update={"cost_usd": self.price})


def observe_licenses(runtime: object) -> set[str]:
    """Observe real dispatch without replacing execution, scoring, or licensing."""
    verified: set[str] = set()
    original = runtime.run

    async def run(artifact, task, adapter):
        assert artifact.licensed and artifact.report is not None
        if artifact.spec.lifecycle.value == "expire":
            assert artifact.report.verification_id not in verified
            verified.add(artifact.report.verification_id)
        return await original(artifact, task, adapter)

    runtime.run = run
    return verified


def check_game(
    result: GameRunResult, seen: set[str], verified: set[str]
) -> dict[str, object]:
    assert result.accepted and not result.failures
    assert result.score is not None and result.score.normalized == 1
    assert result.score.source == "local"
    compiled = result.compile_result
    assert compiled is not None and compiled.stages == (0, 1, 2, 4)
    assert compiled.artifact is not None
    assert compiled.artifact.execution_mode == "native"
    assert compiled.spec.lifecycle.value == "expire"
    report = compiled.report
    assert report is not None and report.passed and report.floor_score == 1
    assert report.held_out_count == 3
    assert report.verification_id in verified and report.verification_id not in seen
    seen.add(report.verification_id)
    assert result.play_result is not None and result.actions_used > 0
    assert result.tokens_in > 0 and result.tokens_out > 0
    assert result.wall_time_s < 60
    return {
        "accepted": True,
        "stages": list(compiled.stages),
        "licensed_before_play": True,
        "fresh_verification": True,
        "full_rollout": True,
        "held_out_count": report.held_out_count,
        "score": result.score.normalized,
        "actions_used": result.actions_used,
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "wall_time_s": result.wall_time_s,
    }


def probe_count(player: object) -> int:
    count = 0
    for prompt in player.prompts:
        if "specification inference stage" not in prompt:
            continue
        data = json.loads(prompt.split("Request data (untrusted):\n", 1)[1])
        source = data["source"]
        if isinstance(source, dict) and "observations" in source:
            assert source["actions"] and len(source["observations"]) > 1
            count += 1
    return count


async def exercise(workspace: Path, *, approve: bool) -> dict[str, object]:
    description = CompetitionDescription.read(ROOT / "examples/arc/competition.local.json")
    started = time.monotonic()
    factory = _fixture.build_factory(workspace / "competition", description)
    store = factory.compiler.runtime.store
    competition_licenses = observe_licenses(factory.compiler.runtime)
    summary: dict[str, object] = {
        "accepted": False,
        "score_source": "local_fixture",
        "compliance": "unverified",
        "model_kind": "deterministic_public_observation_fixture",
        "approval_required": True,
    }
    try:
        proposal = await factory.compile(description)
        assert proposal.approval_required and proposal.artifact is None
        assert proposal.stages == (0,) and not factory.registry.list()
        proposal_path = workspace / "proposed-spec.json"
        proposal_path.write_text(proposal.spec.model_dump_json(indent=2), encoding="utf-8")
        summary["proposal_spec_hash"] = proposal.spec.spec_hash
        if not approve:
            summary.update(telemetry_rows=len(store.rows()), wall_time_s=time.monotonic() - started)
            assert summary["wall_time_s"] < 60
            return summary

        proposal_elapsed = time.monotonic() - started
        local_started = time.monotonic()
        local = _fixture.build_factory(workspace / "games", description)
        seen: set[str] = set()
        local_licenses = observe_licenses(local.compiler.runtime)
        try:
            refs = local.provider.manifest.refs("delivery")
            assert len(refs) == 3
            assert {ref.game_id.rsplit("-", 1)[0] for ref in refs} == set(FAMILIES)
            games = []
            for ref in refs:
                result = await local.game_factory.run(
                    ref, support_dataset=description.child_support_dataset
                )
                games.append(check_game(result, seen, local_licenses))
            player = local.game_factory.compiler.adapter_factory(description.small_model)
            probes = probe_count(player)
            assert probes == 3
            local_elapsed = time.monotonic() - local_started
            assert local_elapsed < 60
            summary["games"] = {
                "families": 3, "family_names": list(FAMILIES),
                "runs": games, "public_probes": probes,
                "wall_time_s": local_elapsed,
                "telemetry_rows": len(local.compiler.runtime.store.rows()),
            }
        finally:
            local.compiler.runtime.store.close()

        competition_started = time.monotonic()
        # Read back exactly what was proposed. The flag approves only this fixed fixture.
        reviewed = Spec.model_validate_json(proposal_path.read_text(encoding="utf-8"))
        assert reviewed == proposal.spec
        player = PricedFixture()
        factory.game_factory.compiler.adapter_factory = lambda model: player
        first = await factory.compile(description, approved=True, approved_spec=reviewed)
        assert first.spec == reviewed and first.stages == (1, 2, 4)
        assert first.artifact is not None and first.artifact.licensed
        assert first.report is not None and first.report.passed
        assert first.report.floor_score == 1 and first.report.held_out_count == 3
        assert first.report.cost_usd > 0
        entry = await factory.install(first, name=description.name)
        assert entry.revision == 1
        assert probe_count(player) == 6  # Real child tuning and Stage 4 rollouts.
        before = len(store.rows())
        run = await factory.run(description.name)
        assert run.accepted and run.score == 1 and len(run.children) == 3
        assert run.score_source == "local_fixture" and run.compliance == "unverified"
        assert run.play_result is not None and run.wall_time_s < 60
        children = [check_game(child, seen, competition_licenses) for child in run.children]
        rows = store.rows()[before:]
        assert run.tokens_in == sum(row.tokens_in for row in rows)
        assert run.tokens_out == sum(row.tokens_out for row in rows)
        assert abs(run.cost_usd - sum(row.cost_usd for row in rows)) < 1e-9
        assert probe_count(player) == 9

        player.price = 0
        replacement = await factory.recompile(description.name)
        assert replacement.revision == 2
        replacement_report = factory.registry.get(description.name).report
        assert replacement_report is not None and replacement_report.passed
        assert replacement_report.verification_id != first.report.verification_id
        assert replacement_report.cost_usd == 0

        # Explicit synthetic drift input; every resulting recompile is a real rollout.
        artifact = factory.registry.get(description.name)
        drift_row = store.rows()[-1].model_copy(update={
            "timestamp": datetime.now(UTC), "run_id": str(uuid4()),
            "spec_hash": artifact.spec.spec_hash, "harness_version": artifact.version,
            "lifecycle": artifact.spec.lifecycle, "floor_type": artifact.spec.floor.kind,
            "floor_limit": artifact.spec.floor.limit, "floor_score": 0.0,
        })
        before_drift = len(store.rows())
        drift = await factory.monitor(description.name, [drift_row])
        assert drift.event is not None and drift.event.reason == "floor_drift"
        assert not drift.accepted and drift.failure == "acceptance_rejected"
        assert len(store.rows()) > before_drift
        assert factory.registry.status(description.name).revision == 2
        restored = factory.rollback(description.name)
        assert restored.revision == 1
        assert factory.registry.get(description.name).report == first.report
        # The restored installed license still executes full fresh children.
        replay = await factory.run(description.name)
        assert replay.accepted and len(replay.children) == 3
        replay_children = [
            check_game(child, seen, competition_licenses) for child in replay.children
        ]
        assert len(competition_licenses) == probe_count(player) == 24
        elapsed = proposal_elapsed + time.monotonic() - competition_started
        assert elapsed < 60
        summary.update(accepted=True, approval_required=False)
        summary["competition"] = {
            "accepted": True, "exact_fixture_approved": True,
            "stages": list(first.stages), "held_out_count": first.report.held_out_count,
            "score": run.score, "metric": run.metric, "children": children,
            "rollback_children": replay_children, "fresh_child_licenses": 6,
            "verified_child_rollouts": len(competition_licenses), "public_probes": 24,
            "installed_revision": entry.revision, "replacement_revision": replacement.revision,
            "rollback_revision": restored.revision, "drift_input": "synthetic_metadata",
            "drift_recompile": True, "equal_candidate_rejected": True,
            "pricing": "synthetic_fixture_usage", "wall_time_s": elapsed,
            "telemetry_rows": len(store.rows()),
        }
        return summary
    finally:
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--approve-fixtures", action="store_true")
    args = parser.parse_args()
    # Refuse reuse before any execution or write, including failure summaries.
    try:
        args.workspace.mkdir(parents=True, exist_ok=False)
    except OSError:
        print(json.dumps({"accepted": False, "failure": "new_workspace_required"}))
        return 1
    try:
        summary = asyncio.run(exercise(args.workspace, approve=args.approve_fixtures))
        code = 0
    except Exception:
        # Never print arbitrary exception messages, reports, prompts or signatures.
        summary = {
            "accepted": False, "failure": "phase6_exit_failed",
            "score_source": "local_fixture", "compliance": "unverified",
        }
        code = 1
    (args.workspace / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
