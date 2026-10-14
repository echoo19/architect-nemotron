"""Phase 9: play-time execution of bound, licensed sub-harnesses, and the build context.

Children here are native (trusted Python) artifacts licensed with the local test
authority; the parent's own harness is never executed, because only the host
path in ``architect/runtime/subharness.py`` is under test.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

from architect._authority import _sign_report
from architect.models import FakeAdapter, ModelRequest, ModelResponse
from architect.models.types import UsageQuote
from architect.runtime import BudgetExceeded, RunContext, Runtime
from architect.runtime.sandbox import SandboxError
from architect.runtime.subharness import run_subharness
from architect.runtime.wallet import compilation_wallet
from architect.spec import (
    Budget,
    FloorDefinition,
    FloorKind,
    ForgeBinding,
    HarnessArtifact,
    Lifecycle,
    Spec,
    SubharnessRef,
    TestReport,
    schema_sha256,
)
from architect.telemetry import TelemetryStore, categorical

CHILD_SOURCE = (
    "async def run(context, task):\n"
    "    reply = await context.call('child: ' + task['text'])\n"
    "    return {'answer': reply}\n"
)
INPUT_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "required": ["text"],
    "properties": {"text": {"type": "string"}},
    "additionalProperties": False,
}
OUTPUT_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
    "additionalProperties": False,
}


class Priced(FakeAdapter):
    """Each call costs exactly ``price`` dollars; replies are fixed text."""

    def __init__(self, price: float = 0.01, model: str = "fake") -> None:
        super().__init__(model=model)
        self.price = price
        self.prompts: list[str] = []

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=10, tokens_out=10, cost_usd=self.price)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        self.prompts.append(request.prompt)
        return ModelResponse(
            text="done", model=self.model, tokens_in=10, tokens_out=10, cost_usd=self.price
        )


def sign(artifact: HarnessArtifact) -> HarnessArtifact:
    report = TestReport(
        spec_hash=artifact.spec.spec_hash,
        artifact_digest=artifact.artifact_digest,
        passed=True,
        floor_score=1.0,
        floor_limit=artifact.spec.floor.limit,
        floor_kind=artifact.spec.floor.kind,
        tokens=1,
        cost_usd=0.0,
        compute_s=0.1,
        component_count=artifact.component_count,
        verification_id="child-verification",
    )
    signature = _sign_report(report.model_dump(mode="json", exclude={"signature"}))
    signed = artifact.model_copy(
        update={"report": report.model_copy(update={"signature": signature})}
    )
    signed.save()
    assert signed.licensed
    return signed


def child_artifact(
    directory: Path, *, source: str = CHILD_SOURCE, model: str = "fake", **spec: Any
) -> HarnessArtifact:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "harness.py").write_text(source, encoding="utf-8")
    values: dict[str, Any] = {
        "task": "Answer the sub-question",
        "input_schema": INPUT_SCHEMA,
        "output_schema": OUTPUT_SCHEMA,
        "floor": FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.8, dataset="child.jsonl"),
        "budget": Budget(cost_usd=0.05, compute_s=5),
        "lifecycle": Lifecycle.INSTALL,
        "model_target": model,
    }
    values.update(spec)
    return sign(
        HarnessArtifact(
            spec=Spec(**values),
            preset="extractor",
            model=model,
            components=("model-call",),
            directory=directory,
        )
    )


def parent_artifact(
    root: Path, *, child_model: str = "fake", **child_changes: Any
) -> tuple[HarnessArtifact, HarnessArtifact]:
    directory = root / "parent"
    directory.mkdir(parents=True)
    (directory / "harness.py").write_text(
        "async def run(context, task):\n    return await context.call('x')\n", encoding="utf-8"
    )
    child = child_artifact(
        directory / "subharnesses" / "helper", model=child_model, **child_changes
    )
    parent = HarnessArtifact(
        spec=Spec(
            task="Parent task",
            input_schema={},
            output_schema={},
            floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.8, dataset="p.jsonl"),
            budget=Budget(cost_usd=1, compute_s=30),
        ),
        preset="extractor",
        model="fake",
        components=(),
        directory=directory,
        execution_mode="sandbox",
        forge=ForgeBinding(
            call_limit=128,
            fuel=10_000_000,
            subharnesses={"helper": SubharnessRef.for_child(child, "authored")},
        ),
    )
    parent.save()
    return parent, child


def context(adapter: FakeAdapter | None = None, budget: Budget | None = None) -> RunContext:
    return RunContext(adapter or Priced(), budget or Budget(cost_usd=1, compute_s=30))


async def test_bound_child_runs_and_debits_the_parent_and_wallet(tmp_path: Path) -> None:
    parent, _ = parent_artifact(tmp_path)
    adapter = Priced(0.01)
    run = context(adapter)
    with compilation_wallet(Budget(cost_usd=1, compute_s=30)) as wallet:
        output = await run.subharness(parent, "helper", {"text": "hi"})
    assert output == {"answer": "done"}
    assert adapter.prompts == ["child: hi"]
    assert run.cost_usd == pytest.approx(0.01) and run.model_calls == 1
    assert wallet.cost_usd == pytest.approx(0.01)
    assert not run._children  # fork was absorbed


async def test_child_budget_is_min_of_child_and_parent_remaining(tmp_path: Path) -> None:
    twice = CHILD_SOURCE.replace("    return", "    await context.call('again')\n    return")
    parent, _ = parent_artifact(tmp_path, source=twice)
    # The child's own budget ($0.05) refuses the second $0.03 call.
    with pytest.raises(BudgetExceeded):
        await context(Priced(0.03)).subharness(parent, "helper", {"text": "hi"})
    # A parent with only $0.025 left refuses the first one.
    poor = context(Priced(0.03), Budget(cost_usd=0.025, compute_s=30))
    with pytest.raises(BudgetExceeded):
        await poor.subharness(parent, "helper", {"text": "hi"})
    assert poor.model_calls == 0


async def test_child_calls_do_not_count_against_the_parent_call_cap(tmp_path: Path) -> None:
    parent, _ = parent_artifact(tmp_path)
    run = context(Priced(0.0))
    run.limit_calls(1)
    await run.subharness(parent, "helper", {"text": "a"})
    await run.subharness(parent, "helper", {"text": "b"})
    await run.call("parent call")
    with pytest.raises(BudgetExceeded, match="Model-call cap"):
        await run.call("second parent call")
    assert run.model_calls == 3  # descendants still count in the parent's usage


async def test_input_and_output_schema_checks(tmp_path: Path) -> None:
    parent, _ = parent_artifact(tmp_path)
    for value in ({"text": 5}, {"text": "x", "extra": 1}, "text", None):
        with pytest.raises(SandboxError, match="sandbox_invalid_call"):
            await context().subharness(parent, "helper", value)
    wrong = CHILD_SOURCE.replace("{'answer': reply}", "{'answer': 1}")
    parent, _ = parent_artifact(tmp_path / "wrong", source=wrong)
    with pytest.raises(SandboxError, match="subharness_schema_error"):
        await context().subharness(parent, "helper", {"text": "x"})


async def test_unknown_names_are_refused(tmp_path: Path) -> None:
    parent, _ = parent_artifact(tmp_path)
    with pytest.raises(SandboxError, match="subharness_unknown"):
        await context().subharness(parent, "other", {"text": "x"})
    unbound = parent.model_copy(update={"forge": None})
    with pytest.raises(SandboxError, match="subharness_unknown"):
        await run_subharness(context(), unbound, "helper", {"text": "x"})


async def test_flipping_a_child_byte_is_a_digest_mismatch(tmp_path: Path) -> None:
    parent, child = parent_artifact(tmp_path)
    path = child.directory / "harness.py"
    path.write_bytes(path.read_bytes().replace(b"child: ", b"child! "))
    adapter = Priced()
    with pytest.raises(SandboxError, match="subharness_digest_mismatch"):
        await context(adapter).subharness(parent, "helper", {"text": "x"})
    assert adapter.calls == 0


async def test_missing_or_substituted_child_is_a_digest_mismatch(tmp_path: Path) -> None:
    parent, child = parent_artifact(tmp_path)
    other = child_artifact(tmp_path / "other", task="A different child")
    shutil.rmtree(child.directory)
    with pytest.raises(SandboxError, match="subharness_digest_mismatch"):
        await context().subharness(parent, "helper", {"text": "x"})
    shutil.copytree(other.directory, child.directory)
    with pytest.raises(SandboxError, match="subharness_digest_mismatch"):
        await context().subharness(parent, "helper", {"text": "x"})


async def test_unlicensed_child_is_refused(tmp_path: Path) -> None:
    parent, child = parent_artifact(tmp_path)
    (child.directory / "report.json").unlink()
    with pytest.raises(SandboxError, match="subharness_unlicensed"):
        await context().subharness(parent, "helper", {"text": "x"})


async def test_model_mismatch_is_refused(tmp_path: Path) -> None:
    parent, _ = parent_artifact(tmp_path, child_model="other-model")
    adapter = Priced()
    with pytest.raises(SandboxError, match="subharness_unlicensed"):
        await context(adapter).subharness(parent, "helper", {"text": "x"})
    assert adapter.calls == 0


async def test_lineage_depth_cap(tmp_path: Path) -> None:
    parent, _ = parent_artifact(tmp_path)
    root = context()
    node = root
    for _ in range(3):
        node = node.fork(node.adapter, Budget(cost_usd=1, compute_s=30))
    assert len(node.lineage) == 4
    assert await node.subharness(parent, "helper", {"text": "x"}) == {"answer": "done"}
    deeper = node.fork(node.adapter, Budget(cost_usd=1, compute_s=30))
    with pytest.raises(SandboxError, match="subharness_depth"):
        await deeper.subharness(parent, "helper", {"text": "x"})


def test_subharness_ref_binds_schema_digests(tmp_path: Path) -> None:
    _, child = parent_artifact(tmp_path)
    ref = SubharnessRef.for_child(child, "projection")
    assert ref.artifact_digest == child.artifact_digest
    assert ref.spec_hash == child.spec.spec_hash
    assert ref.input_schema_sha256 == schema_sha256(INPUT_SCHEMA)
    assert ref.output_schema_sha256 == schema_sha256(OUTPUT_SCHEMA)
    unlicensed = child.model_copy(update={"report": None})
    with pytest.raises(ValueError):
        SubharnessRef.for_child(unlicensed, "authored")


# ---------------------------------------------------------------------------
# Build context: only Runtime.build creates is_build=True.
# ---------------------------------------------------------------------------


async def probe(context: RunContext, task: JsonValue) -> JsonValue:
    child = context.fork(context.adapter, Budget(cost_usd=0.1, compute_s=5))
    try:
        return {
            "build": context.is_build,
            "evaluation": context.is_evaluation,
            "child_build": child.is_build,
            "repository": str(context.repository),
        }
    finally:
        await child._close()
        context.absorb(child)


def spec() -> Spec:
    return Spec(
        task="Build",
        input_schema={},
        output_schema={},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.8, dataset="d"),
        budget=Budget(cost_usd=1, compute_s=30),
        lifecycle=Lifecycle.INSTALL,
    )


def test_run_context_defaults_to_not_build() -> None:
    assert RunContext(FakeAdapter(), Budget()).is_build is False
    assert RunContext(FakeAdapter(), Budget(), is_build=True).is_build is True


async def test_build_sets_is_build_and_repository(tmp_path: Path) -> None:
    runtime = Runtime(TelemetryStore(tmp_path / "runs"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = await runtime.build(
        spec(),
        probe,
        {"repo": str(workspace), "goal": "write harness.py"},
        FakeAdapter(),
        preset="forge_agent",
        repository=workspace,
    )
    assert result.output == {
        "build": True,
        "evaluation": False,
        "child_build": True,
        "repository": str(workspace.resolve()),
    }
    row = runtime.store.rows()[-1]
    assert row.preset_origin == categorical("forge_agent") and row.floor_score is None


async def test_evaluate_and_run_are_never_build(tmp_path: Path) -> None:
    runtime = Runtime(TelemetryStore(tmp_path / "runs"))
    evaluated = await runtime.evaluate(spec(), probe, {}, FakeAdapter())
    assert isinstance(evaluated.output, dict)
    assert evaluated.output["build"] is False and evaluated.output["evaluation"] is True
    directory = tmp_path / "delivery"
    directory.mkdir()
    (directory / "harness.py").write_text(
        "async def run(context, task):\n"
        "    return {'build': context.is_build, 'evaluation': context.is_evaluation}\n",
        encoding="utf-8",
    )
    artifact = sign(
        HarnessArtifact(spec=spec(), preset="toy", model="fake", components=(), directory=directory)
    )
    delivered = await runtime.run(artifact, {}, FakeAdapter())
    assert delivered.output == {"build": False, "evaluation": False}


async def test_v2_parent_calls_its_child_through_the_real_worker(tmp_path: Path) -> None:
    """Integration with WP1: ``await context.harness("helper", x)`` reaches this host path."""
    parent, _ = parent_artifact(tmp_path)
    (parent.directory / "harness.py").write_text(
        "from pydantic import JsonValue\n"
        "\n"
        "from architect.runtime import RunContext\n"
        "\n"
        "\n"
        "async def run(context: RunContext, task: JsonValue) -> JsonValue:\n"
        "    first = await context.harness('helper', {'text': task})\n"
        "    return first['answer'] + '!'\n",
        encoding="utf-8",
    )
    persistent = parent.spec.model_copy(update={"lifecycle": Lifecycle.INSTALL})
    parent = sign(
        parent.model_copy(
            update={"spec": persistent, "components": ("extraction",), "report": None}
        )
    )
    runtime = Runtime(TelemetryStore(tmp_path / "runs"))
    adapter = Priced(0.01)
    result = await runtime.run(parent, "hi", adapter)
    assert result.output == "done!"
    assert adapter.prompts == ["child: hi"]
    row = runtime.store.rows()[-1]
    assert row.model_calls == 1 and row.cost_usd == pytest.approx(0.01)
    # A flipped child byte fails the parent run at call time.
    child_code = parent.directory / "subharnesses" / "helper" / "harness.py"
    child_code.write_bytes(child_code.read_bytes() + b"\n")
    with pytest.raises(SandboxError, match="subharness_digest_mismatch"):
        await runtime.run(parent, "hi", adapter)
