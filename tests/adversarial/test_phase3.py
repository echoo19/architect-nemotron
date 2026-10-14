"""Independent compiler authority, full-partition, and budget regression attacks."""

from __future__ import annotations

import ast
import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import JsonValue

from architect._authority import verify_report
from architect.compiler import Compiler
from architect.compiler.inference import InferenceError, infer_spec
from architect.compiler.types import CompileConfig, Measurement
from architect.compiler.verification import verify
from architect.floors import CalibrationRow, EvaluationCase, HeldOutTasks, LLMJudge
from architect.models import FakeAdapter, ModelRequest, ModelResponse, UsageQuote
from architect.presets import get_preset
from architect.runtime import Runtime
from architect.spec import Budget, FloorDefinition, FloorKind, HarnessArtifact, Lifecycle, Spec
from architect.telemetry import TelemetryStore


@pytest.fixture(autouse=True)
def private_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCHITECT_SIGNING_KEY_FILE", str(tmp_path / "test-signing.key"))


def make_definition(*, dataset: str = "private-evaluation", limit: float = 0.75) -> FloorDefinition:
    return FloorDefinition(
        kind=FloorKind.HELD_OUT_TASKS, limit=limit, dataset=dataset, minimum_samples=2
    )


def make_spec(*, secondary: FloorDefinition | None = None, budget: Budget | None = None) -> Spec:
    return Spec(
        task="Produce a short generic result",
        input_schema={"type": "object"},
        output_schema={"type": "string"},
        floor=make_definition(),
        secondary_floor=secondary,
        budget=budget or Budget(cost_usd=1, compute_s=2),
        model_target="fake",
        lifecycle=Lifecycle.EXPIRE,
    )


def make_cases(count: int = 8, *, expected: JsonValue = "ok") -> list[EvaluationCase]:
    return [
        EvaluationCase(
            case_id=f"case-{index:03d}",
            task={"id": f"case-{index:03d}", "input": f"private-input-{index}"},
            expected=expected,
            metadata={"secret": "private-metadata"},
        )
        for index in range(count)
    ]


class AuditedFloor(HeldOutTasks):
    def __init__(self, definition: FloorDefinition, records: Sequence[EvaluationCase]) -> None:
        super().__init__(definition, records)
        self.grants = 0
        self.scored: list[tuple[str, ...]] = []

    def _verification_cases(self, capability: object) -> tuple[EvaluationCase, ...]:
        authorized = super()._verification_cases(capability)
        self.grants += 1
        return authorized

    async def score(self, cases: Sequence[EvaluationCase], outputs: Sequence[JsonValue]) -> float:
        self.scored.append(tuple(case.case_id for case in cases))
        return await super().score(cases, outputs)


class AdapterFactory:
    def __init__(self, *, tokens: int = 6, cost: float = 0.02, text: str = "ok") -> None:
        self.tokens = tokens
        self.cost = cost
        self.text = text
        self.adapters: list[FakeAdapter] = []

    def __call__(self, model: str) -> FakeAdapter:
        assert model == "fake"
        response = ModelResponse(
            text=self.text, tokens_in=self.tokens, tokens_out=0, cost_usd=self.cost, model=model
        )
        adapter = FakeAdapter(responses=[response] * 100)
        self.adapters.append(adapter)
        return adapter

    @property
    def calls(self) -> int:
        return sum(adapter.calls for adapter in self.adapters)


def artifact_at(
    tmp_path: Path,
    *,
    spec: Spec | None = None,
    code: str | None = None,
) -> HarnessArtifact:
    spec = spec or make_spec()
    directory = tmp_path / "artifacts" / spec.spec_hash / "1"
    directory.mkdir(parents=True)
    (directory / "harness.py").write_text(
        code or "async def run(context, task):\n    return 'ok'\n", encoding="utf-8"
    )
    artifact = HarnessArtifact(
        spec=spec,
        preset="synthesizer",
        model="fake",
        components=("synthesis",),
        directory=directory,
    )
    artifact.save()
    return artifact


def report_is_authentic(artifact: HarnessArtifact) -> bool:
    assert artifact.report is not None
    return verify_report(
        artifact.report.model_dump(mode="json", exclude={"signature"}), artifact.report.signature
    )


def heldout_ids(floor: AuditedFloor, records: Sequence[EvaluationCase]) -> set[str]:
    tuning = {case.case_id for case in floor.tuning}
    return {case.case_id for case in records} - tuning


async def test_verifier_executes_entire_heldout_once_and_never_passes_answers(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "executed.jsonl"
    code = (
        "import json\nfrom pathlib import Path\n"
        "async def run(context, task):\n"
        f"    with Path({str(trace)!r}).open('a') as stream:\n"
        "        stream.write(json.dumps(task) + '\\n')\n"
        "    return 'ok'\n"
    )
    artifact = artifact_at(tmp_path, code=code)
    records = make_cases(10)
    floor = AuditedFloor(artifact.spec.floor, records)
    expected = heldout_ids(floor, records)
    store = TelemetryStore(tmp_path / "telemetry")
    verified = await verify(artifact, floor, AdapterFactory(), Runtime(store))
    assert verified.licensed
    assert report_is_authentic(verified)
    assert verified.report is not None
    assert verified.report.held_out_count == len(expected)
    assert floor.grants == 1
    assert len(floor.scored) == 1
    assert set(floor.scored[0]) == expected
    tasks = [json.loads(line) for line in trace.read_text().splitlines()]
    assert len(tasks) == len(expected)
    assert {task["id"] for task in tasks} == expected
    assert all(set(task) == {"id", "input"} for task in tasks)
    assert "private-metadata" not in trace.read_text()
    assert len(store.rows()) >= len(expected) + 1
    assert HarnessArtifact.load(verified.directory).licensed


@pytest.mark.parametrize("failure", ["exception", "invalid-schema", "budget"])
async def test_one_hard_heldout_failure_cannot_be_dropped_to_pass_average(
    tmp_path: Path,
    failure: str,
) -> None:
    spec = make_spec().model_copy(update={"floor": make_definition(limit=0.1)})
    records = make_cases(10)
    floor = AuditedFloor(spec.floor, records)
    target = sorted(heldout_ids(floor, records))[0]
    trace = tmp_path / "executed.jsonl"
    action = {
        "exception": "raise ValueError('private-hard-case-error')",
        "invalid-schema": "return {'invalid': 'private-schema-output'}",
        "budget": "return await context.call('private-over-budget-prompt')",
    }[failure]
    code = (
        "from pathlib import Path\n"
        "async def run(context, task):\n"
        f"    with Path({str(trace)!r}).open('a') as stream:\n"
        "        stream.write(task['id'] + '\\n')\n"
        f"    if task['id'] == {target!r}:\n        {action}\n"
        "    return 'ok'\n"
    )
    artifact = artifact_at(tmp_path, spec=spec, code=code)
    factory = AdapterFactory(cost=spec.budget.cost_usd + 0.01)
    store = TelemetryStore(tmp_path / "telemetry")
    verified = await verify(artifact, floor, factory, Runtime(store))
    assert not verified.licensed
    assert verified.report is not None
    assert not verified.report.passed
    assert verified.report.failures
    assert report_is_authentic(verified)
    assert floor.grants == 1
    assert set(trace.read_text().splitlines()) == heldout_ids(floor, records)
    assert verified.report.held_out_count == len(heldout_ids(floor, records))
    if failure == "budget":
        assert factory.calls == 0
    public_report = verified.report.model_dump_json()
    assert "private-" not in public_report
    for path in (tmp_path / "telemetry").iterdir():
        if path.is_file():
            assert b"private-" not in path.read_bytes()


@pytest.mark.parametrize("changed", ["limit", "dataset", "minimum_samples"])
async def test_floor_definition_must_match_exactly(tmp_path: Path, changed: str) -> None:
    artifact = artifact_at(tmp_path)
    values: dict[str, object] = {"limit": 0.1, "dataset": "different-dataset", "minimum_samples": 3}
    mismatch = artifact.spec.floor.model_copy(update={changed: values[changed]})
    floor = AuditedFloor(mismatch, make_cases())
    verified = await verify(
        artifact, floor, AdapterFactory(), Runtime(TelemetryStore(tmp_path / "telemetry"))
    )
    assert not verified.licensed
    assert verified.report is not None and not verified.report.passed
    assert report_is_authentic(verified)
    assert floor.grants == 0


@pytest.mark.parametrize("secondary_attack", ["omitted", "mismatched", "failed"])
async def test_secondary_floor_cannot_be_omitted_replaced_or_failed(
    tmp_path: Path,
    secondary_attack: str,
) -> None:
    secondary_definition = make_definition(dataset="secondary-private", limit=1)
    artifact = artifact_at(tmp_path, spec=make_spec(secondary=secondary_definition))
    primary = AuditedFloor(artifact.spec.floor, make_cases())
    secondary = None
    if secondary_attack == "mismatched":
        secondary = AuditedFloor(make_definition(dataset="wrong-secondary"), make_cases())
    elif secondary_attack == "failed":
        secondary = AuditedFloor(secondary_definition, make_cases(expected="different-answer"))
    verified = await verify(
        artifact,
        primary,
        AdapterFactory(),
        Runtime(TelemetryStore(tmp_path / "telemetry")),
        secondary_floor=secondary,
    )
    assert not verified.licensed
    assert verified.report is not None and not verified.report.passed
    assert report_is_authentic(verified)


async def test_both_floors_are_measured_and_secondary_values_are_signed(tmp_path: Path) -> None:
    secondary_definition = make_definition(dataset="secondary-private", limit=1)
    artifact = artifact_at(tmp_path, spec=make_spec(secondary=secondary_definition))
    primary = AuditedFloor(artifact.spec.floor, make_cases())
    secondary = AuditedFloor(secondary_definition, make_cases(12))
    verified = await verify(
        artifact,
        primary,
        AdapterFactory(),
        Runtime(TelemetryStore(tmp_path / "telemetry")),
        secondary_floor=secondary,
    )
    assert verified.licensed
    assert primary.grants == secondary.grants == 1
    assert len(primary.scored[0]) == 4
    assert len(secondary.scored[0]) == 6
    report = verified.report
    assert report is not None
    assert report.secondary_floor_score == 1
    assert report.secondary_floor_limit == secondary_definition.limit
    assert report.secondary_floor_kind == secondary_definition.kind
    for field, replacement in (
        ("secondary_floor_score", 0.9),
        ("secondary_floor_limit", 0.9),
        ("secondary_floor_kind", FloorKind.LLM_JUDGE),
        ("held_out_count", report.held_out_count + 1),
    ):
        forged = report.model_copy(update={field: replacement})
        assert not verified.model_copy(update={"report": forged}).licensed


async def test_authentic_existing_report_never_skips_fresh_verification(tmp_path: Path) -> None:
    artifact = artifact_at(tmp_path)
    runtime = Runtime(TelemetryStore(tmp_path / "telemetry"))
    first = await verify(
        artifact, AuditedFloor(artifact.spec.floor, make_cases()), AdapterFactory(), runtime
    )
    assert first.licensed
    assert first.report is not None
    original_id = first.report.verification_id
    changed_records = AuditedFloor(first.spec.floor, make_cases(expected="different-answer"))
    second = await verify(first, changed_records, AdapterFactory(), runtime)
    assert changed_records.grants == 1
    assert not second.licensed
    assert second.report is not None and not second.report.passed
    assert second.report.verification_id != original_id
    assert report_is_authentic(second)
    assert not HarnessArtifact.load(second.directory).licensed


async def test_forged_report_is_replaced_not_used_as_authority(tmp_path: Path) -> None:
    artifact = artifact_at(tmp_path)
    runtime = Runtime(TelemetryStore(tmp_path / "telemetry"))
    first = await verify(
        artifact, AuditedFloor(artifact.spec.floor, make_cases()), AdapterFactory(), runtime
    )
    assert first.report is not None
    forged = first.report.model_copy(update={"signature": "model-approved"})
    artifact = first.model_copy(update={"report": forged})
    assert not artifact.licensed
    floor = AuditedFloor(artifact.spec.floor, make_cases(expected="different-answer"))
    checked = await verify(artifact, floor, AdapterFactory(), runtime)
    assert floor.grants == 1
    assert not checked.licensed
    assert checked.report is not None and checked.report.verification_id != forged.verification_id
    assert report_is_authentic(checked)


async def test_code_mutation_during_measurement_refuses_license(tmp_path: Path) -> None:
    artifact = artifact_at(tmp_path)
    source_path = artifact.directory / "harness.py"
    source_path.write_text(
        "from pathlib import Path\n"
        "async def run(context, task):\n"
        f"    with Path({str(source_path)!r}).open('a') as stream:\n"
        "        stream.write('# mutation\\n')\n"
        "    return 'ok'\n",
        encoding="utf-8",
    )
    before = artifact.artifact_digest
    checked = await verify(
        artifact,
        AuditedFloor(artifact.spec.floor, make_cases()),
        AdapterFactory(),
        Runtime(TelemetryStore(tmp_path / "telemetry")),
    )
    assert artifact.artifact_digest != before
    assert not checked.licensed
    assert checked.report is not None and not checked.report.passed
    assert checked.report.failures
    assert report_is_authentic(checked)


@pytest.mark.parametrize("change", ["budget", "params", "code", "components"])
async def test_fresh_report_never_licenses_later_artifact_changes(
    tmp_path: Path,
    change: str,
) -> None:
    artifact = artifact_at(tmp_path)
    checked = await verify(
        artifact,
        AuditedFloor(artifact.spec.floor, make_cases()),
        AdapterFactory(),
        Runtime(TelemetryStore(tmp_path / "telemetry")),
    )
    assert checked.licensed
    if change == "budget":
        spec = checked.spec.model_copy(update={"budget": Budget(cost_usd=0.001, compute_s=0.001)})
        checked = checked.model_copy(update={"spec": spec})
    elif change == "params":
        checked.params["nested"] = {"changed": True}
    elif change == "components":
        checked = checked.model_copy(update={"components": ("substituted",)})
    else:
        (checked.directory / "harness.py").write_text("# substituted\n", encoding="utf-8")
    assert not checked.licensed


@pytest.mark.parametrize("field", ["tokens", "cost"])
async def test_verification_measures_tokens_but_gates_per_case_dollars(
    tmp_path: Path,
    field: str,
) -> None:
    spec = make_spec(budget=Budget(cost_usd=0.1, compute_s=2))
    floor = AuditedFloor(spec.floor, make_cases(10))
    expensive = sorted(heldout_ids(floor, make_cases(10)))[-1]
    artifact = artifact_at(
        tmp_path,
        spec=spec,
        code=(
            "async def run(context, task):\n"
            f"    if task['id'] == {expensive!r}:\n"
            "        return await context.call('expensive')\n"
            "    return 'ok'\n"
        ),
    )
    factory = AdapterFactory(
        tokens=11 if field == "tokens" else 1, cost=0.11 if field == "cost" else 0.001
    )
    checked = await verify(
        artifact, floor, factory, Runtime(TelemetryStore(tmp_path / "telemetry"))
    )
    assert checked.report is not None
    if field == "tokens":
        assert checked.licensed and checked.report.passed
        assert factory.calls == 1 and checked.report.tokens == 11
    else:
        assert not checked.licensed and not checked.report.passed
        assert factory.calls == 0 and checked.report.cost_usd >= 0.11


class FixedLibrary:
    def __init__(self, artifact: HarnessArtifact) -> None:
        self.artifact = artifact
        self.lookups = 0

    def lookup(self, spec: Spec, model: str) -> HarnessArtifact | None:
        self.lookups += 1
        return self.artifact


async def test_compiler_cache_hit_still_runs_stage_four_on_full_heldout(tmp_path: Path) -> None:
    spec = make_spec()
    preset = get_preset("synthesizer")
    params = preset.validate_params({"verifier_depth": 0})
    components = tuple(item for item in preset.components if item != "review")
    directory = tmp_path / "cached"
    preset.emit(directory, spec, params, components)
    candidate = HarnessArtifact(
        spec=spec,
        preset=preset.name,
        model="fake",
        components=components,
        params=params,
        directory=directory,
    )
    candidate.save()
    runtime = Runtime(TelemetryStore(tmp_path / "telemetry"))
    factory = AdapterFactory()
    cached = await verify(candidate, AuditedFloor(spec.floor, make_cases()), factory, runtime)
    assert cached.licensed and cached.report is not None
    old_id = cached.report.verification_id
    library = FixedLibrary(cached)
    new_floor = AuditedFloor(spec.floor, make_cases())
    compiler = Compiler(
        tmp_path / "compiled",
        runtime,
        factory,
        lambda _: new_floor,
        config=CompileConfig(max_candidates=1),
        library=library,
    )
    result = await compiler.compile(spec)
    assert library.lookups >= 1
    assert result.artifact is not None and result.artifact.licensed
    assert result.report is not None and result.report.verification_id != old_id
    assert 4 in result.stages
    assert new_floor.grants == 1
    assert result.artifact.directory != cached.directory
    assert result.artifact.report is not cached.report


async def test_compiler_floor_factory_mismatch_cannot_license_candidate(tmp_path: Path) -> None:
    spec = make_spec()
    floor = AuditedFloor(make_definition(dataset="wrong-factory-dataset", limit=0), make_cases())
    compiler = Compiler(
        tmp_path / "compiled",
        Runtime(TelemetryStore(tmp_path / "telemetry")),
        AdapterFactory(),
        lambda _: floor,
        config=CompileConfig(max_candidates=1),
    )
    result = await compiler.compile(spec)
    assert result.artifact is None or not result.artifact.licensed
    assert result.report is None or not result.report.passed
    assert result.failures or (result.report is not None and result.report.failures)


@pytest.mark.parametrize("constraint", ["tokens", "cost"])
async def test_compilation_wallet_caps_dollars_but_not_tokens_across_candidates(
    tmp_path: Path,
    constraint: str,
) -> None:
    spec = make_spec()
    factory = AdapterFactory(tokens=6, cost=0.02)
    wallet = Budget(cost_usd=0.03 if constraint == "cost" else 10, compute_s=10)
    compiler = Compiler(
        tmp_path / "compiled",
        Runtime(TelemetryStore(tmp_path / "telemetry")),
        factory,
        lambda floor: AuditedFloor(floor, make_cases()),
        config=CompileConfig(max_candidates=3, compile_budget=wallet),
    )
    result = await compiler.compile(spec)
    if constraint == "tokens":
        assert factory.calls > 1
        assert result.artifact is not None and result.artifact.licensed
        assert result.report is not None and result.report.passed
    else:
        assert factory.calls <= 1
        assert result.artifact is None or not result.artifact.licensed
        assert result.report is None or not result.report.passed


@pytest.mark.parametrize("text", ["not-json", "{}", "[]", "null", '{"licensed":true}'])
async def test_garbage_or_model_claimed_authority_cannot_infer_a_spec(
    tmp_path: Path,
    text: str,
) -> None:
    factory = AdapterFactory(text=text)
    with pytest.raises(InferenceError):
        await infer_spec(
            "private-inference-request",
            factory("fake"),
            Runtime(TelemetryStore(tmp_path / "telemetry")),
        )
    assert factory.calls == 1


@pytest.mark.parametrize("authority", ["licensed", "report", "approval_required"])
async def test_otherwise_valid_inference_cannot_embed_model_authority(
    tmp_path: Path,
    authority: str,
) -> None:
    data = make_spec().model_dump(mode="json")
    data[authority] = True if authority != "report" else {"passed": True}
    factory = AdapterFactory(text=json.dumps(data))
    with pytest.raises(InferenceError):
        await infer_spec(
            "private-inference-request",
            factory("fake"),
            Runtime(TelemetryStore(tmp_path / "telemetry")),
        )


def test_only_verification_production_module_imports_signer_or_heldout_token() -> None:
    package = Path(__file__).resolve().parents[2] / "architect"
    exempt = {Path("compiler/verification.py"), Path("_authority.py"), Path("floors/_access.py")}
    names = {"_sign_report", "_VERIFIER_CAPABILITY"}
    violations: list[str] = []
    for source in package.rglob("*.py"):
        relative = source.relative_to(package)
        if relative in exempt:
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for item in node.names:
                    if item.name in names or (
                        item.name == "*" and (node.module or "").endswith(("_authority", "_access"))
                    ):
                        violations.append(f"{relative}:{node.lineno}")
            elif isinstance(node, ast.Attribute) and node.attr in names:
                violations.append(f"{relative}:{node.lineno}")
    assert not violations, "Private authority outside verification: " + ", ".join(violations)


@pytest.mark.parametrize("failed_gate", ["floor", "budget"])
async def test_verifier_cannot_erase_failed_tuning_with_lucky_heldout_success(
    tmp_path: Path,
    failed_gate: str,
) -> None:
    artifact = artifact_at(tmp_path)
    floor = AuditedFloor(artifact.spec.floor, make_cases())
    evidence = Measurement(
        score=0 if failed_gate == "floor" else 1,
        floor_passed=failed_gate != "floor",
        budget_passed=failed_gate != "budget",
        mean_cost_usd=0,
        max_tokens=0,
        max_cost_usd=0,
        max_compute_s=0,
        sample_count=4,
    )
    checked = await verify(
        artifact,
        floor,
        AdapterFactory(),
        Runtime(TelemetryStore(tmp_path / "telemetry")),
        tuning_evidence=evidence,
    )
    assert not checked.licensed
    assert checked.report is not None
    assert not checked.report.passed
    assert "tuning_failed" in checked.report.failures
    assert report_is_authentic(checked)


async def test_compiler_preserves_failed_tuning_even_when_heldout_is_easy(tmp_path: Path) -> None:
    spec = make_spec()
    records = make_cases()
    original = AuditedFloor(spec.floor, records)
    tuning_ids = {case.case_id for case in original.tuning}
    records = [
        case.model_copy(update={"expected": "impossible"}) if case.case_id in tuning_ids else case
        for case in records
    ]
    floor = AuditedFloor(spec.floor, records)
    compiler = Compiler(
        tmp_path / "compiled",
        Runtime(TelemetryStore(tmp_path / "telemetry")),
        AdapterFactory(),
        lambda _: floor,
        config=CompileConfig(max_candidates=1),
    )
    result = await compiler.compile(spec)
    assert result.artifact is not None
    assert not result.artifact.licensed
    assert result.report is not None and not result.report.passed
    assert "tuning_failed" in result.report.failures
    assert floor.grants == 1
    assert 4 in result.stages


class LadderAdapter:
    def __init__(self, model: str, cost: float, text: str) -> None:
        self.model = model
        self.cost = cost
        self.text = text
        self.calls = 0

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=2, tokens_out=1, cost_usd=self.cost)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            text=self.text, tokens_in=2, tokens_out=1, cost_usd=self.cost, model=self.model
        )


@pytest.mark.parametrize("cheap_passes", [True, False])
async def test_model_ladder_selects_measured_cost_only_after_each_rung_verifies(
    tmp_path: Path,
    cheap_passes: bool,
) -> None:
    spec = make_spec().model_copy(update={"model_target": "cheapest that passes"})
    floor = AuditedFloor(spec.floor, make_cases())
    adapters: list[LadderAdapter] = []

    def factory(model: str) -> LadderAdapter:
        cost = 0.02 if model == "expensive" else 0.001
        text = "wrong" if model == "cheap" and not cheap_passes else "ok"
        adapter = LadderAdapter(model, cost, text)
        adapters.append(adapter)
        return adapter

    compiler = Compiler(
        tmp_path / "compiled",
        Runtime(TelemetryStore(tmp_path / "telemetry")),
        factory,
        lambda _: floor,
        config=CompileConfig(model_ladder=("expensive", "cheap"), max_candidates=1),
    )
    result = await compiler.compile(spec)
    assert result.artifact is not None and result.artifact.licensed
    assert result.artifact.model == ("cheap" if cheap_passes else "expensive")
    assert {adapter.model for adapter in adapters if adapter.calls} == {"expensive", "cheap"}
    assert floor.grants == 2
    assert result.report is not None and result.report.held_out_count == 4


async def test_installed_spec_never_compiles_without_explicit_approval(tmp_path: Path) -> None:
    spec = make_spec().model_copy(update={"lifecycle": Lifecycle.INSTALL})
    factory = AdapterFactory()
    floor_calls = 0

    def floor_factory(definition: FloorDefinition) -> AuditedFloor:
        nonlocal floor_calls
        floor_calls += 1
        return AuditedFloor(definition, make_cases())

    compiler = Compiler(
        tmp_path / "compiled",
        Runtime(TelemetryStore(tmp_path / "telemetry")),
        factory,
        floor_factory,
        config=CompileConfig(max_candidates=1),
    )
    result = await compiler.compile(spec)
    assert result.approval_required
    assert result.artifact is None
    assert result.report is None
    assert factory.calls == floor_calls == 0
    assert 4 not in result.stages


async def test_user_provenance_claim_from_model_cannot_bypass_install_approval(
    tmp_path: Path,
) -> None:
    inferred = make_spec().model_copy(update={"lifecycle": Lifecycle.INSTALL})
    data = inferred.model_dump(mode="json")
    data["provenance"] = {"lifecycle": "user", "floor": "user"}
    inference_factory = AdapterFactory(text=json.dumps(data))
    inference_adapter = inference_factory("fake")
    execution_factory = AdapterFactory()
    compiler = Compiler(
        tmp_path / "compiled",
        Runtime(TelemetryStore(tmp_path / "telemetry")),
        execution_factory,
        lambda floor: AuditedFloor(floor, make_cases()),
        config=CompileConfig(max_candidates=1),
        inference_adapter=inference_adapter,
    )
    result = await compiler.compile("private-install-request")
    assert result.approval_required
    assert result.artifact is None
    assert result.report is None
    assert inference_factory.calls == 1
    assert execution_factory.calls == 0
    assert result.spec.provenance["lifecycle"] == "inferred"


@pytest.mark.parametrize("constraint", ["tokens", "cost"])
async def test_compilation_wallet_counts_judge_tokens_but_caps_only_dollars(
    tmp_path: Path,
    constraint: str,
) -> None:
    definition = FloorDefinition(
        kind=FloorKind.LLM_JUDGE,
        limit=0.8,
        dataset="judge-data",
        minimum_samples=2,
        rubric="Fixed rubric",
        judge_model="fake",
        calibration_dataset="human-calibration",
    )
    spec = make_spec().model_copy(update={"floor": definition})
    runtime = Runtime(TelemetryStore(tmp_path / "telemetry"))
    judge_response = ModelResponse(
        text="0.9", tokens_in=2, tokens_out=0, cost_usd=0.002, model="fake"
    )
    judge_adapter = FakeAdapter(responses=[judge_response] * 100)
    floor = LLMJudge(
        definition,
        make_cases(),
        [
            CalibrationRow(
                case_id="calibration",
                task=None,
                output="ok",
                human_score=0.9,
            )
        ],
        judge_adapter,
        runtime,
    )
    factory = AdapterFactory(tokens=6, cost=0.02)
    # Four tuning outputs fit; the first calibration quote must not fit.
    wallet = Budget(cost_usd=0.081 if constraint == "cost" else 10, compute_s=10)
    compiler = Compiler(
        tmp_path / "compiled",
        runtime,
        factory,
        lambda _: floor,
        config=CompileConfig(max_candidates=1, compile_budget=wallet),
    )
    result = await compiler.compile(spec)
    if constraint == "tokens":
        assert factory.calls >= 8 and judge_adapter.calls > 0
        assert result.artifact is not None and result.artifact.licensed
        assert result.report is not None and result.report.passed
    else:
        assert factory.calls == 4 and judge_adapter.calls == 0
        assert result.artifact is None or not result.artifact.licensed
        assert result.report is None or not result.report.passed


async def test_ladder_optimizes_signed_mean_cost_not_lower_maximum(tmp_path: Path) -> None:
    spec = make_spec().model_copy(update={"model_target": "cheapest that passes"})
    floor = AuditedFloor(spec.floor, make_cases())
    counts: dict[str, int] = {}

    def factory(model: str) -> LadderAdapter:
        index = counts.get(model, 0)
        counts[model] = index + 1
        cost = 0.05 if model == "steady" else (0.001 if index % 2 == 0 else 0.09)
        return LadderAdapter(model, cost, "ok")

    artifact_root = tmp_path / "compiled"
    compiler = Compiler(
        artifact_root,
        Runtime(TelemetryStore(tmp_path / "telemetry")),
        factory,
        lambda _: floor,
        config=CompileConfig(model_ladder=("steady", "variable"), max_candidates=1),
    )
    result = await compiler.compile(spec)
    assert result.artifact is not None and result.artifact.licensed
    assert result.artifact.model == "variable"
    assert result.report is not None
    assert result.report.mean_cost_usd == pytest.approx(0.0455)
    assert result.report.cost_usd == pytest.approx(0.09)
    assert floor.grants == 2
    artifacts = [HarnessArtifact.load(path.parent) for path in artifact_root.rglob("report.json")]
    steady = next(artifact for artifact in artifacts if artifact.model == "steady")
    assert steady.licensed and steady.report is not None
    assert steady.report.mean_cost_usd == pytest.approx(0.05)
    assert steady.report.cost_usd == pytest.approx(0.05)
    # A forged mean must not change a signed cost objective without revoking authority.
    forged = result.report.model_copy(update={"mean_cost_usd": 0.0})
    assert not result.artifact.model_copy(update={"report": forged}).licensed
