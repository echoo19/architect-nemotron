"""Integrator checks for the remaining shared game capability gates."""
from pathlib import Path

import pytest
from pydantic import JsonValue

from architect.floors import EvaluationCase, HeldOutTasks
from architect.models import FakeAdapter
from architect.presets import get_preset
from architect.runtime import BudgetExceeded, RunContext, Runtime, _GameScope
from architect.runtime import _sandbox_worker as worker
from architect.runtime.evaluation import evaluate_cases
from architect.runtime.evidence import current_evaluation, scoped_evaluation
from architect.runtime.sandbox import _validated_tree, sandbox_harness
from architect.spec import Budget, FloorDefinition, FloorKind, HarnessArtifact, Spec
from architect.telemetry import TelemetryStore


class Backend:
    called = False

    async def run(
        self, context: RunContext, task: JsonValue, *, preset: str,
        params: dict[str, JsonValue], components: tuple[str, ...],
    ) -> JsonValue:
        context.check_game_grant()
        self.called = True
        return "ok"


@pytest.mark.parametrize("active", [False, True])
async def test_scope_rejects_direct_expired_or_foreign_context(active: bool) -> None:
    backend = Backend()
    scope = _GameScope(owner=123, backend=backend, active=active)
    context = RunContext(FakeAdapter(), Budget(), game_scope=scope if not active else None)
    with pytest.raises(BudgetExceeded):
        await scope.run(context, None, preset="explorer", params={}, components=())
    assert not backend.called and not scope.pending


def test_multiple_cleanup_errors_drain_all_and_preserve_first_error() -> None:
    events: list[str] = []

    def fail(name: str) -> None:
        events.append(name)
        raise RuntimeError(name)

    with pytest.raises(RuntimeError, match="second"):
        with scoped_evaluation("a" * 64) as scope:
            scope.register_cleanup(lambda: fail("first"))
            scope.register_cleanup(lambda: fail("second"))
    assert events == ["second", "first"]
    assert not scope.active and current_evaluation() is None


def artifact(path: Path, kind: str = "game") -> HarnessArtifact:
    spec = Spec(
        task="Use the bound game capability", domain="explorer", input_schema={},
        output_schema={"type": "string"}, execution_config={"kind": kind},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, dataset="fixture", limit=1),
        budget=Budget(cost_usd=1, compute_s=5),
    )
    preset = get_preset("explorer")
    params = preset.validate_params({"verifier_depth": 0})
    components = tuple(c for c in preset.components if c not in preset.removable_components)
    preset.emit(path, spec, params, components)
    result = HarnessArtifact(
        spec=spec, preset=preset.name, model="fake", directory=path,
        params=params, components=components, execution_mode="sandbox",
    )
    result.save()
    return result


async def test_absent_candidate_digest_records_load_failure_without_execution(
    tmp_path: Path,
) -> None:
    value = artifact(tmp_path / "artifact")
    (value.directory / "harness.py").unlink()
    floor = HeldOutTasks(value.spec.floor, [
        EvaluationCase(case_id=str(i), task=None, expected="ok") for i in range(4)
    ])
    store = TelemetryStore(tmp_path / "telemetry")
    try:
        result = await evaluate_cases(value, floor, floor.tuning, FakeAdapter, Runtime(store))
        assert not result.floor_passed and result.failures == ("load_error",)
        assert current_evaluation() is None
        assert len(store.rows()) == 1 and not store.rows()[0].accepted
    finally:
        store.close()


@pytest.mark.parametrize("kind", ["game", "competition"])
async def test_worker_game_dispatch_only_sends_bound_preset_rpc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    value = artifact(tmp_path / "artifact", kind)
    sent: list[object] = []
    monkeypatch.setattr(worker, "_send", sent.append)
    monkeypatch.setattr(worker, "_receive", lambda: {"value": "ok"})
    tree = _validated_tree((value.directory / "harness.py").read_text(), value)
    with pytest.raises(worker._Return) as returned:
        await worker._Interpreter({"target": 1}).block(tree.body)
    assert returned.value.value == "ok"
    assert sent == [{"op": "preset", "task": {"target": 1}}]


async def test_sandbox_game_dispatch_uses_real_scoped_parent_capability(tmp_path: Path) -> None:
    value = artifact(tmp_path / "artifact")
    backend = Backend()
    store = TelemetryStore(tmp_path / "telemetry")
    runtime = Runtime(store)
    try:
        async with runtime.use_game_backend(backend):
            result = await runtime.evaluate(
                value.spec, sandbox_harness(value), {"target": 1}, FakeAdapter(),
                preset=value.preset, params=value.params, components=value.components,
                candidate_digest=value.artifact_digest,
            )
        assert result.output == "ok" and backend.called
        assert store.rows()[0].accepted
    finally:
        store.close()
