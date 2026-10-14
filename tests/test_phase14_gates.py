"""Phase 14 WA, gate integrity (docs/interfaces/phase14-nebius-evidence.md, Sections 3-4).

Hash and signature invariance against values pinned at commit bc2c315 (before this
phase touched the Spec), the new Spec validators, ``CaseCounts``, the run classes and
``count_as_miss`` scoring (invalid output at 0%, 2% and 3% of a 100-case partition),
Stage 4 counts and refusals, and Stage 0 never proposing the new fields. Offline.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue, ValidationError

from architect._authority import _canonical_payload, _sign_report, verify_report
from architect.bench.suite import SUITES, suite_digest
from architect.compiler.inference import infer_spec
from architect.compiler.verification import verify
from architect.floors import EvaluationCase, HeldOutTasks
from architect.models import FakeAdapter, ModelAdapter, ModelRequest, ModelResponse
from architect.presets import get_preset
from architect.runtime import Runtime
from architect.runtime.evaluation import evaluate_cases
from architect.spec import (
    Budget,
    CaseCounts,
    FloorDefinition,
    FloorKind,
    HarnessArtifact,
    Lifecycle,
    PipelineStep,
    Spec,
    TestReport,
    invalid_allowance,
)
from architect.telemetry import TelemetryStore, categorical

PHASE14_FIELDS = ("invalid_policy", "max_invalid_fraction", "input_limit_chars", "reply_json",
                  "plain")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# -- hash and signature invariance ------------------------------------------------------

TASKS = {task.id: task for task in SUITES["v1"]}


def _inferred() -> Spec:
    return Spec(
        task="Summarise a support ticket in one sentence",
        input_schema={"type": "object", "properties": {"ticket": {"type": "string"}}},
        output_schema={"type": "string"},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.85, dataset="drafted"),
        budget=Budget(cost_usd=0.02, compute_s=60),
        model_target="haiku",
        lifecycle=Lifecycle.INSTALL,
        domain="support",
        preset_hint="synthesizer",
        provenance={"task": "user", "budget": "inferred"},
    )


def _pipeline() -> Spec:
    return Spec(
        task="Two step",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.5, dataset="p"),
        budget=Budget(cost_usd=0.5, compute_s=30),
        pipeline=(
            PipelineStep(name="a", preset="extractor", model="fake", budget=Budget()),
            PipelineStep(name="b", preset="synthesizer", model="fake", budget=Budget()),
        ),
    )


# (spec_hash, sha256 of model_dump_json) recorded at bc2c315, before Phase 14.
PINNED: dict[str, tuple[Callable[[], Spec], str, str]] = {
    "bench": (
        lambda: TASKS["invoice-40"].spec("fake", "default"),
        "03f73ce8d919aafc7dd06c7b12bdfcf5d02f91f59f97c4572af9ebad5b81df4e",
        "7093f27fc8f43f8de83fcb28c60d61de44d703d38323a67e9bdaf158d7e0f1b8",
    ),
    "bench_pipeline": (
        lambda: TASKS["digitizer-24"].spec("fake", "default"),
        "9fd7689daeb127fa3cf54f3702bc9b852bda468b0c3243a69fcb726bc5739665",
        "60aa16cfbd70845306c3238cab2d19a6b51e0382e44897aad074ab1a0e083669",
    ),
    "inferred": (
        _inferred,
        "e80714fdedaeb0f94f0630e5308f9146e2af409a311ffb807e11531a89be5c77",
        "da6c49aa7332d479aa8e4148b7a28c126c65a769217ebc2567dffe67153c5ead",
    ),
    "pipeline": (
        _pipeline,
        "58854c6210f00243b29bb4e68f65b5874d9a54b947c78e08c09cb373998d250e",
        "1d823b3790f4f720de38a9322de474aa809445a44c0be615c00e6df213de3b60",
    ),
}


@pytest.mark.parametrize("name", sorted(PINNED))
def test_default_specs_hash_and_dump_exactly_as_before(name: str) -> None:
    build, spec_hash, dumped = PINNED[name]
    spec = build()
    assert spec.spec_hash == spec_hash
    assert _sha(spec.model_dump_json().encode()) == dumped
    payload = spec.model_dump(mode="json")
    assert not set(PHASE14_FIELDS) & set(payload)
    # Writing a default explicitly hashes like omitting it.
    explicit = Spec.model_validate(
        {
            **payload,
            "invalid_policy": "fail_partition",
            "max_invalid_fraction": 0.02,
            "input_limit_chars": None,
            "reply_json": "strict",
            "plain": False,
        }
    )
    assert explicit.spec_hash == spec_hash
    assert explicit.model_dump_json() == spec.model_dump_json()


def test_emitted_harness_artifact_digest_and_v1_digest_are_unchanged(tmp_path: Path) -> None:
    spec = TASKS["invoice-40"].spec("fake", "default")
    directory = tmp_path / "a"
    directory.mkdir()
    get_preset("extractor").emit(directory, spec)
    assert _sha((directory / "harness.py").read_bytes()) == (
        "59501961cbe9dc3f57ad5ce065b49eea300970d8cc25108deadc8cd2cea6b221"
    )
    artifact = HarnessArtifact(
        spec=spec, preset="extractor", model="fake", components=("x",), directory=directory
    )
    assert artifact.artifact_digest == (
        "51c6f61bfae0370ed17a50b60795306975fbe664b021e40f27126371ad49221e"
    )
    artifact.save()
    assert _sha((directory / "spec.json").read_bytes()) == (
        "5710e0c04555ee8afc0f6d76c6652aa5465667390744e47c90d6ab340967294c"
    )
    assert suite_digest("v1") == "1f23e5e421b2c2c3d79482394a841764b37f4e70d3b06a73993fc7bb4bf335ed"


def test_new_fields_are_hashed_only_when_set() -> None:
    base = TASKS["invoice-40"].spec("fake", "default").model_dump(mode="json")
    miss = Spec.model_validate({**base, "invalid_policy": "count_as_miss"})
    tighter = Spec.model_validate(
        {**base, "invalid_policy": "count_as_miss", "max_invalid_fraction": 0.01}
    )
    hashes = {
        PINNED["bench"][1],
        miss.spec_hash,
        tighter.spec_hash,
        Spec.model_validate({**base, "input_limit_chars": 8192}).spec_hash,
        Spec.model_validate({**base, "reply_json": "first"}).spec_hash,
        Spec.model_validate({**base, "plain": True}).spec_hash,
    }
    assert len(hashes) == 6
    # Under count_as_miss both the policy and the fraction are hashed, the default included.
    dumped = miss.model_dump(mode="json")
    assert dumped["invalid_policy"] == "count_as_miss"
    assert dumped["max_invalid_fraction"] == 0.02
    # Nested dumps omit defaults too, and a dump round-trips.
    assert Spec.model_validate_json(miss.model_dump_json()) == miss


def _report(**updates: Any) -> TestReport:
    values: dict[str, Any] = dict(
        spec_hash="a" * 64,
        artifact_digest="b" * 64,
        passed=True,
        floor_score=0.9,
        floor_limit=0.85,
        floor_kind=FloorKind.HELD_OUT_TASKS,
        tokens=10,
        cost_usd=0.001,
        compute_s=1.5,
        component_count=2,
        verification_id="v" * 32,
        held_out_count=20,
        mean_cost_usd=0.0005,
    )
    values.update(updates)
    return TestReport(**values)


def test_report_without_counts_signs_the_old_payload() -> None:
    report = _report()
    payload = report.model_dump(mode="json", exclude={"signature"})
    assert "held_out_counts" not in payload
    assert _sha(_canonical_payload(payload)) == (
        "c9d319a5e224acd077a0fe110529a5b67552c504ac9fb3d88546f03734ca9654"
    )
    assert _sha(report.model_dump_json(indent=2).encode()) == (
        "103869f5733813be484c7f9a9719243ebce2df741bdb3f3f7b6188aea0d1afb8"
    )
    # A report written before this phase (no held_out_counts key) still parses and verifies.
    signed = report.model_copy(update={"signature": _sign_report(payload)})
    old = TestReport.model_validate_json(signed.model_dump_json())
    assert old.held_out_counts is None
    assert verify_report(old.model_dump(mode="json", exclude={"signature"}), old.signature)


def test_counts_are_signed_and_a_changed_count_breaks_the_signature() -> None:
    counts = CaseCounts(cases=20, correct=18, invalid=1, over_budget=0)
    report = _report(held_out_counts=counts)
    payload = report.model_dump(mode="json", exclude={"signature"})
    assert payload["held_out_counts"] == {
        "cases": 20, "correct": 18, "invalid": 1, "over_budget": 0
    }
    signature = _sign_report(payload)
    assert verify_report(payload, signature)
    changed = report.model_copy(
        update={"held_out_counts": counts.model_copy(update={"invalid": 0})}
    )
    assert not verify_report(changed.model_dump(mode="json", exclude={"signature"}), signature)


# -- validators, counts and the allowance -------------------------------------------------


def _spec(**updates: Any) -> Spec:
    values: dict[str, Any] = dict(
        task="Return the answer",
        input_schema={"type": "object"},
        output_schema={"type": "string"},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.9, dataset="gate"),
        budget=Budget(cost_usd=1, compute_s=30),
    )
    values.update(updates)
    return Spec.model_validate(values)


def test_validators_refuse_inconsistent_phase14_fields() -> None:
    judge = FloorDefinition(
        kind=FloorKind.LLM_JUDGE, limit=0.8, dataset="j", rubric="r", judge_model="fake",
        calibration_dataset="c",
    )
    secondary = FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.5, dataset="second")
    refused: list[dict[str, Any]] = [
        {"max_invalid_fraction": 0.05},  # fail_partition with a non-default fraction
        {"invalid_policy": "count_as_miss", "secondary_floor": secondary},
        {"invalid_policy": "count_as_miss", "floor": judge},
        {"plain": True},  # no preset hint
        {"plain": True, "preset_hint": "coder"},
        {"plain": True, "preset_hint": "extractor",
         "pipeline": [{"name": "a", "preset": "extractor", "model": "fake", "budget": {}}]},
        {"plain": True, "preset_hint": "explorer", "execution_config": {"kind": "game"}},
        {"invalid_policy": "count_as_miss", "max_invalid_fraction": 0.2},
        {"input_limit_chars": 100},
        {"plain": 1, "preset_hint": "extractor"},
    ]
    for updates in refused:
        with pytest.raises(ValidationError):
            _spec(**updates)
    assert _spec(invalid_policy="count_as_miss", max_invalid_fraction=0.1).floor.limit == 0.9
    assert _spec(plain=True, preset_hint="extractor").plain is True


def test_invalid_allowance() -> None:
    assert invalid_allowance(_spec(), 100) == 0
    miss = _spec(invalid_policy="count_as_miss")
    assert [invalid_allowance(miss, n) for n in (0, 49, 50, 100, 149, 200)] == [0, 0, 1, 2, 2, 4]
    # 0.072 * 375 is 26.999999999999996 in floats: the epsilon keeps it at 27.
    assert invalid_allowance(_spec(invalid_policy="count_as_miss", max_invalid_fraction=0.072),
                             375) == 27


def test_case_counts_readout_and_consistency() -> None:
    scored = CaseCounts(cases=100, correct=93, invalid=1, over_budget=0)
    assert scored.readout() == "correct 93/100 · invalid 1"
    assert scored.readout(sep=" | ") == "correct 93/100 | invalid 1"
    unscored = CaseCounts(cases=100, correct=None, invalid=3, over_budget=0)
    assert unscored.readout() == "not scored · invalid 3 · over budget 0"
    with pytest.raises(ValidationError):
        CaseCounts(cases=10, correct=8, invalid=3, over_budget=0)  # more than the partition
    with pytest.raises(ValidationError):
        CaseCounts(cases=10, correct=5, invalid=0, over_budget=1)  # scored despite a refusal


# -- the evaluation matrix ----------------------------------------------------------------

SOURCE = (
    "from architect.runtime import BudgetExceeded\n\n"
    "async def run(context, task):\n"
    "    kind = task['kind']\n"
    "    if kind == 'invalid':\n"
    "        return 7\n"
    "    if kind == 'raise':\n"
    "        raise RuntimeError('boom')\n"
    "    if kind == 'budget':\n"
    "        raise BudgetExceeded('over')\n"
    "    if kind == 'wrong':\n"
    "        return 'wrong'\n"
    "    return task['answer']\n"
)
INPUT = {"type": "object", "required": ["kind"]}


def _cases(kinds: list[str]) -> list[EvaluationCase]:
    """Sorted ids put case 2i in tuning and 2i+1 held out; both get ``kinds[i]``."""
    cases = []
    for index, kind in enumerate(kinds):
        task: JsonValue = {"kind": kind, "answer": f"a{index}"} if kind != "input" else "bad"
        for offset in (0, 1):
            cases.append(
                EvaluationCase(
                    case_id=f"c{2 * index + offset:04d}", task=task, expected=f"a{index}"
                )
            )
    return cases


def _artifact(tmp_path: Path, spec: Spec) -> HarnessArtifact:
    directory = tmp_path / "artifact"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "harness.py").write_text(SOURCE, encoding="utf-8")
    artifact = HarnessArtifact(
        spec=spec, preset="toy", model="fake", components=(), directory=directory
    )
    artifact.save()
    return artifact


def _setup(
    tmp_path: Path, kinds: list[str], *, limit: float = 0.9, **updates: Any
) -> tuple[HarnessArtifact, HeldOutTasks, Runtime]:
    spec = _spec(
        input_schema=INPUT,
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=limit, dataset="gate"),
        **updates,
    )
    floor = HeldOutTasks(spec.floor, _cases(kinds))
    return _artifact(tmp_path, spec), floor, Runtime(TelemetryStore(tmp_path / "telemetry"))


MISS = {"invalid_policy": "count_as_miss"}


@pytest.mark.parametrize(
    ("invalid", "expected"),
    [
        (0, (0.95, True, (), CaseCounts(cases=100, correct=95, invalid=0, over_budget=0))),
        (2, (0.93, True, (), CaseCounts(cases=100, correct=93, invalid=2, over_budget=0))),
        (3, (0.0, False, ("invalid_fraction_exceeded",),
             CaseCounts(cases=100, correct=None, invalid=3, over_budget=0))),
    ],
    ids=["0pct", "2pct", "3pct"],
)
async def test_count_as_miss_at_0_2_and_3_percent_invalid(
    tmp_path: Path, invalid: int, expected: tuple[float, bool, tuple[str, ...], CaseCounts]
) -> None:
    # 100 cases: 5 wrong answers, then ``invalid`` invalid runs (half schema, half raised).
    kinds = ["wrong"] * 5 + ["invalid", "raise", "invalid"][:invalid]
    kinds += ["ok"] * (100 - len(kinds))
    artifact, floor, runtime = _setup(tmp_path, kinds, **MISS)
    measurement = await evaluate_cases(artifact, floor, floor.tuning, FakeAdapter, runtime)
    score, passed, failures, counts = expected
    assert measurement.score == pytest.approx(score)
    assert measurement.floor_passed is passed and measurement.budget_passed
    assert measurement.failures == failures
    assert measurement.counts == counts
    assert measurement.sample_count == 100
    row = runtime.store.rows()[-1]
    assert row.task_type == categorical("quality_evaluation")
    assert row.floor_score == (pytest.approx(score) if failures == () else None)
    # The same invalid runs fail the whole partition under the default policy.
    default, floor2, runtime2 = _setup(tmp_path / "default", kinds)
    baseline = await evaluate_cases(default, floor2, floor2.tuning, FakeAdapter, runtime2)
    if invalid:
        assert baseline.score == 0.0 and not baseline.floor_passed
        assert baseline.failures[0] in ("schema_error", "runtime_error")
        assert baseline.counts == CaseCounts(
            cases=100, correct=None, invalid=invalid, over_budget=0
        )
    else:
        assert baseline.score == pytest.approx(0.95)
        assert baseline.counts == CaseCounts(cases=100, correct=95, invalid=0, over_budget=0)


@pytest.mark.parametrize(
    ("kind", "failures", "counts"),
    [
        ("invalid", ("schema_error",), (None, 1, 0)),
        ("raise", ("runtime_error",), (None, 1, 0)),
        ("budget", ("budget_exceeded",), (None, 0, 1)),
        ("input", ("schema_error",), (None, 0, 0)),
        ("ok", (), (4, 0, 0)),
    ],
)
async def test_fail_partition_keeps_todays_codes_and_fills_counts(
    tmp_path: Path, kind: str, failures: tuple[str, ...], counts: tuple[int | None, int, int]
) -> None:
    artifact, floor, runtime = _setup(tmp_path, ["ok", "ok", "ok", kind])
    measurement = await evaluate_cases(artifact, floor, floor.tuning, FakeAdapter, runtime)
    assert measurement.failures == failures
    assert measurement.budget_passed is (kind != "budget")
    assert measurement.counts == CaseCounts(
        cases=4, correct=counts[0], invalid=counts[1], over_budget=counts[2]
    )
    assert measurement.score == (1.0 if kind == "ok" else 0.0)


@pytest.mark.parametrize(
    ("kind", "failures", "counts"),
    [
        ("budget", ("budget_exceeded",), (None, 0, 1)),
        ("input", ("schema_error",), (None, 0, 0)),
    ],
)
async def test_count_as_miss_keeps_budget_and_input_failures_hard(
    tmp_path: Path, kind: str, failures: tuple[str, ...], counts: tuple[int | None, int, int]
) -> None:
    # 50 cases allow one invalid run, yet one budget or input failure is never a miss.
    artifact, floor, runtime = _setup(
        tmp_path, ["ok"] * 49 + [kind], invalid_policy="count_as_miss"
    )
    measurement = await evaluate_cases(artifact, floor, floor.tuning, FakeAdapter, runtime)
    assert measurement.failures == failures and not measurement.floor_passed
    assert measurement.counts == CaseCounts(
        cases=50, correct=counts[0], invalid=counts[1], over_budget=counts[2]
    )
    assert runtime.store.rows()[-1].floor_score is None


@pytest.mark.parametrize("policy", [{}, MISS], ids=["fail_partition", "count_as_miss"])
async def test_factory_and_accounting_failures_stay_hard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: dict[str, str]
) -> None:
    artifact, floor, runtime = _setup(tmp_path, ["ok"] * 50, **policy)

    def broken(model: str) -> ModelAdapter:
        raise RuntimeError("no adapter")

    measurement = await evaluate_cases(artifact, floor, floor.tuning, broken, runtime)
    assert measurement.failures == ("runtime_error",)
    assert measurement.counts == CaseCounts(cases=50, correct=None, invalid=0, over_budget=0)
    monkeypatch.setattr(Runtime, "last_row", property(lambda self: None))
    accounting = await evaluate_cases(artifact, floor, floor.tuning, FakeAdapter, runtime)
    assert accounting.failures == ("runtime_error",) and not accounting.floor_passed
    assert accounting.counts == CaseCounts(cases=50, correct=None, invalid=0, over_budget=0)


async def test_count_as_miss_scores_the_valid_subset_once(tmp_path: Path) -> None:
    class Recording(HeldOutTasks):
        scored: list[tuple[str, ...]] = []

        async def score(self, cases: Any, outputs: Any) -> float:
            self.scored.append(tuple(case.case_id for case in cases))
            return await super().score(cases, outputs)

    kinds = ["ok", "invalid", "ok", "wrong"] + ["ok"] * 46
    artifact, original, runtime = _setup(tmp_path, kinds, limit=0.5, **MISS)
    floor = Recording(original.definition, _cases(kinds))
    measurement = await evaluate_cases(artifact, floor, floor.tuning, FakeAdapter, runtime)
    assert len(floor.scored) == 1 and len(floor.scored[0]) == 49  # the invalid run left out
    assert "c0002" not in floor.scored[0]
    assert measurement.score == pytest.approx(48 / 50)
    assert measurement.counts == CaseCounts(cases=50, correct=48, invalid=1, over_budget=0)


# -- Stage 4 ------------------------------------------------------------------------------


async def test_stage4_writes_counts_under_both_policies(tmp_path: Path) -> None:
    artifact, floor, runtime = _setup(tmp_path, ["ok"] * 98 + ["invalid", "raise"], **MISS)
    licensed = await verify(artifact, floor, FakeAdapter, runtime)
    report = licensed.report
    assert report is not None and report.passed and licensed.licensed
    assert report.held_out_counts == CaseCounts(cases=100, correct=98, invalid=2, over_budget=0)
    assert report.floor_score == pytest.approx(0.98)
    stored = json.loads((licensed.directory / "report.json").read_text(encoding="utf-8"))
    assert stored["held_out_counts"] == {
        "cases": 100, "correct": 98, "invalid": 2, "over_budget": 0
    }
    assert HarnessArtifact.load(licensed.directory).licensed
    default, floor2, runtime2 = _setup(tmp_path / "default", ["ok"] * 4)
    plain = await verify(default, floor2, FakeAdapter, runtime2)
    assert plain.licensed and plain.report is not None
    assert plain.report.held_out_counts == CaseCounts(
        cases=4, correct=4, invalid=0, over_budget=0
    )


async def test_stage4_refuses_too_many_invalid_runs(tmp_path: Path) -> None:
    artifact, floor, runtime = _setup(tmp_path, ["ok"] * 97 + ["invalid"] * 3, **MISS)
    refused = await verify(artifact, floor, FakeAdapter, runtime)
    report = refused.report
    assert report is not None and not report.passed and not refused.licensed
    assert set(report.failures) == {
        "primary_floor_failed", "primary_evaluation_failed", "primary_invalid_exceeded"
    }
    assert report.held_out_counts == CaseCounts(
        cases=100, correct=None, invalid=3, over_budget=0
    )


async def test_licensed_refuses_inconsistent_signed_counts(tmp_path: Path) -> None:
    artifact, floor, runtime = _setup(tmp_path, ["ok"] * 49 + ["invalid"], **MISS)
    licensed = await verify(artifact, floor, FakeAdapter, runtime)
    report = licensed.report
    assert report is not None and licensed.licensed
    assert report.held_out_counts == CaseCounts(cases=50, correct=49, invalid=1, over_budget=0)

    def resigned(**changes: Any) -> HarnessArtifact:
        assert report is not None and report.held_out_counts is not None
        counts = CaseCounts.model_validate(
            {**report.held_out_counts.model_dump(), **changes.pop("counts", {})}
        )
        updated = report.model_copy(update={"held_out_counts": counts, **changes})
        signature = _sign_report(updated.model_dump(mode="json", exclude={"signature"}))
        return licensed.model_copy(
            update={"report": updated.model_copy(update={"signature": signature})}
        )

    assert resigned().licensed  # the control: re-signing changes nothing
    assert resigned(counts={"correct": 48}, floor_score=0.96).licensed  # consistent counts
    # Each check alone refuses: the case count, the allowance, the score, a budget refusal.
    assert not resigned(counts={"cases": 51}).licensed
    assert not resigned(counts={"invalid": 2, "correct": 48}, floor_score=0.96).licensed
    assert not resigned(counts={"correct": 47}).licensed
    assert not resigned(counts={"correct": None, "invalid": 0, "over_budget": 1}).licensed
    without = report.model_copy(update={"held_out_counts": None})
    signature = _sign_report(without.model_dump(mode="json", exclude={"signature"}))
    old_style = licensed.model_copy(
        update={"report": without.model_copy(update={"signature": signature})}
    )
    assert old_style.licensed  # reports without counts skip the checks


# -- Stage 0 ------------------------------------------------------------------------------


class Proposing(FakeAdapter):
    def __init__(self, text: str) -> None:
        super().__init__(
            model="fake",
            responses=[ModelResponse(text=text, model="fake", tokens_in=5, tokens_out=5,
                                     cost_usd=0.001)],
        )
        self.systems: list[str | None] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.systems.append(request.cache_prefix)  # the fixed system part of the call
        return await super().complete(request)


async def test_stage0_never_shows_or_takes_the_new_fields(tmp_path: Path) -> None:
    proposal = _spec().model_dump(mode="json") | {
        "invalid_policy": "count_as_miss",
        "max_invalid_fraction": 0.1,
        "input_limit_chars": 4096,
        "reply_json": "last",
        "plain": True,
    }
    adapter = Proposing(json.dumps(proposal))
    runtime = Runtime(TelemetryStore(tmp_path / "telemetry"))
    result = await infer_spec("return the answer", adapter, runtime)
    spec = result.spec
    assert (spec.invalid_policy, spec.max_invalid_fraction, spec.input_limit_chars,
            spec.reply_json, spec.plain) == ("fail_partition", 0.02, None, "strict", False)
    assert not set(PHASE14_FIELDS) & set(spec.provenance)
    system = adapter.systems[0]
    assert system is not None
    schema_text = system.split("\nSpec JSON schema:\n", 1)[1]
    schema = json.loads(schema_text)
    assert not set(PHASE14_FIELDS) & set(schema["properties"])
    # Byte-identical to the schema Stage 0 showed before this phase (pinned at bc2c315).
    assert _sha(schema_text.encode()) == (
        "f13eced359f5871dde1da763a1a291aa967bc5c864b67547ada802ca5976827d"
    )


async def test_stage0_keeps_user_supplied_fields(tmp_path: Path) -> None:
    adapter = Proposing(_spec().model_dump_json())
    runtime = Runtime(TelemetryStore(tmp_path / "telemetry"))
    result = await infer_spec(
        "return the answer", adapter, runtime, {"invalid_policy": "count_as_miss"}
    )
    assert result.spec.invalid_policy == "count_as_miss"
    assert "invalid_policy" not in result.spec.provenance
