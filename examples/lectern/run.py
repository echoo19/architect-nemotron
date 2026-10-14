"""Run offline consumer examples through real compiler, registry, and runtime.

Default: proposals only. --approve-fixtures is an explicit test-only approval;
it does not represent a person's review or authorize any production delivery.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path
from time import perf_counter

from synthetic_adapter import PIPELINE_MODELS, adapter_factory

from architect.compiler import CompileConfig, Compiler
from architect.floors import EvaluationCase, HeldOutTasks, HumanEditRate
from architect.models import FakeAdapter
from architect.registry import Registry
from architect.runtime import Runtime
from architect.spec import Budget, Spec
from architect.telemetry import TelemetryStore

BASE = Path(__file__).resolve().parent
NAMES = ("report-cards", "test-digitizer", "content-recommender")
REQUEST = (
    "I want an AI feature that turns uploaded tests into Bluebook-style online tests "
    "with answer keys."
)


def save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def make_floor(definition):
    if definition.dataset == "synthetic-report-edits":
        return HumanEditRate.from_jsonl(definition, BASE / "fixtures/report-edits.jsonl")
    paths = {"synthetic-answer-keys": "answer-keys.json",
             "synthetic-content-acceptance": "content-acceptance.json"}
    records = json.loads((BASE / "fixtures" / paths[definition.dataset]).read_text())
    return HeldOutTasks(definition, [EvaluationCase.model_validate(row) for row in records])


async def fixture_delivery(runtime, artifact, task, adapter, *, feature_enabled: bool,
                           fixture_review_approved: bool):
    # Application gating remains distinct from Stage 4's harness license.
    # A real consumer must substitute authenticated per-item HUMAN review.
    if not feature_enabled or not fixture_review_approved or task.get("synthetic") is not True:
        raise PermissionError("fixture_delivery_not_approved")
    return await runtime.run(artifact, task, adapter)


async def run_example(name: str, workspace: Path, *, approve: bool, fail_floor: bool) -> dict:
    await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
    counts = Counter()
    factory = adapter_factory(name, counts, fail_floor=fail_floor)
    store = TelemetryStore(workspace / "telemetry")
    try:
        runtime = Runtime(store, adapter_factory=factory)
        inference = FakeAdapter(fixture=BASE / "fixtures/digitizer-inference.json")
        compiler = Compiler(
            workspace / "artifacts", runtime, factory, make_floor,
            config=CompileConfig(max_candidates=2, enable_rewriting=False, max_rewrites=0,
                                 compile_budget=Budget(cost_usd=2, compute_s=45)),
            inference_adapter=inference,
        )
        source = (REQUEST if name == "test-digitizer" else
                  Spec.model_validate_json((BASE / "specs" / f"{name}.json").read_text()))
        proposal = await compiler.compile(source)
        if not proposal.approval_required or proposal.artifact is not None:
            raise AssertionError("installed_spec_must_stop_for_review")
        save(workspace / "proposed-spec.json", proposal.spec.model_dump(mode="json"))
        row = {"name": name, "synthetic": True, "approval_required": True,
               "proposal_stages": list(proposal.stages), "licensed": False,
               "inference_adapter": "scripted-synthetic-not-llm-quality"
               if name == "test-digitizer" else "explicit-spec",
               "production_feature_enabled": False, "human_review_required": True,
               "real_monthly_cost_baseline": None, "fixture_approval": approve}
        if not approve:
            return row
        # Checkpoint records identify the EXACT proposed Spec explicitly approved
        # by this fixture runner. No compile occurs before that checkpoint.
        save(workspace / "reviewed-spec.json", proposal.spec.model_dump(mode="json"))
        save(workspace / "fixture-approval.json", {
            "spec_hash": proposal.spec.spec_hash, "scope": "synthetic-fixtures-only",
            "human_review_performed": False, "production_approved": False,
        })
        result = await compiler.compile(proposal.spec, approved=True)
        row.update({"stages": list(result.stages), "failures": list(result.failures),
                    "licensed": bool(result.artifact and result.artifact.licensed),
                    "model_calls": dict(counts)})
        if result.report is not None:
            row["report"] = result.report.model_dump(mode="json")
            row["tuning_count"] = len(make_floor(result.spec.floor).tuning)
        if fail_floor:
            if row["licensed"] or result.report is None or result.report.passed:
                raise AssertionError("negative_fixture_must_fail_real_quality_floor")
            if "primary_floor_failed" not in result.report.failures:
                raise AssertionError("negative_fixture_did_not_reach_floor_gate")
            return row
        if not row["licensed"] or result.artifact is None:
            raise AssertionError(f"synthetic_compile_failed:{name}:{result.failures}")
        registry = Registry(workspace / "registry")
        entry = registry.install(name, result.artifact)
        installed = registry.get(name)
        task = json.loads((BASE / "fixtures" / f"{name}-input.json").read_text())
        before = counts.copy()
        # Prove both independent application gates refuse before model execution.
        for enabled, reviewed in ((False, True), (True, False)):
            try:
                await fixture_delivery(runtime, installed, task, factory(installed.model),
                                       feature_enabled=enabled, fixture_review_approved=reviewed)
            except PermissionError:
                pass
            else:
                raise AssertionError("application_gate_did_not_refuse")
        if counts != before:
            raise AssertionError("disabled_delivery_called_model")
        delivery = await fixture_delivery(
            runtime, installed, task, factory(installed.model),
            feature_enabled=True, fixture_review_approved=True,
        )
        delivered_calls = dict(counts - before)
        if name == "test-digitizer":
            if any(delivered_calls.get(model, 0) != 1 for model in PIPELINE_MODELS):
                raise AssertionError("every_pipeline_model_must_execute")
            if proposal.spec.provenance.get("pipeline") != "inferred" or inference.calls != 1:
                raise AssertionError("pipeline_was_not_inferred_by_stage_zero")
        save(workspace / "synthetic-output.json", delivery.output)
        row.update({"registry_revision": entry.revision, "run_id": delivery.run_id,
                    "delivery_model_calls": delivered_calls, "model_calls": dict(counts),
                    "delivery_tokens": delivery.tokens_in + delivery.tokens_out,
                    "delivery_cost_usd": delivery.cost_usd,
                    "application_gate_refusals": 2})
        return row
    finally:
        store.close()


async def main_async(args) -> dict:
    started = perf_counter()
    workspace = args.workspace.resolve()
    if (workspace / "summary.json").exists():
        raise ValueError("use_a_fresh_workspace_to_preserve_evidence")
    names = NAMES if args.example == "all" else (args.example,)
    rows = []
    for name in names:
        rows.append(await run_example(name, workspace / name, approve=args.approve_fixtures,
                                      fail_floor=args.variant == "floor-failure"))
    summary = {"evidence": "synthetic-offline-only", "variant": args.variant,
               "examples": rows, "elapsed_s": perf_counter() - started,
               "production_approved": False}
    save(workspace / "summary.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--approve-fixtures", action="store_true")
    parser.add_argument("--example", choices=("all", *NAMES), default="all")
    parser.add_argument("--variant", choices=("passing", "floor-failure"), default="passing")
    args = parser.parse_args()
    if args.variant == "floor-failure" and not args.approve_fixtures:
        parser.error("floor-failure needs explicit --approve-fixtures to compile")
    print(json.dumps(asyncio.run(main_async(args))))


if __name__ == "__main__":
    main()
