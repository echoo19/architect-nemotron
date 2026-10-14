"""Frozen suite v1: digest, spec builders, sizing and datasets (Phase 8 WP3)."""

from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType

import pytest
from jsonschema import Draft202012Validator

from architect.bench import suite as suite_module
from architect.bench.oracle import FloorPartition, lookup_key
from architect.bench.pages import VISION_INPUT_SCHEMA
from architect.bench.rows import BenchError
from architect.bench.suite import (
    SUITE_TIERS,
    SUITES,
    TIERS,
    BenchTask,
    example_manifests,
    suite_digest,
)
from architect.bench.text_tasks import canonical_key
from architect.models import FakeAdapter
from architect.models.images import ImageInput
from architect.presets import get_preset
from architect.spec import FloorKind, Lifecycle, Spec

# Changing any task, size, floor, checker, tier or generator version changes this and
# needs a new suite version with a fresh baseline (iteration protocol step 2).
PINNED_V1_DIGEST = "1f23e5e421b2c2c3d79482394a841764b37f4e70d3b06a73993fc7bb4bf335ed"
TASKS = {task.id: task for task in SUITES["v1"]}
MANIFESTS = Path(__file__).parents[1] / "examples" / "bench" / "suite-v1"
VARIANTS = ("default", "noisy", "images-all-stages")


def test_suite_digest_is_stable() -> None:
    assert suite_digest("v1") == suite_digest("v1") == PINNED_V1_DIGEST
    with pytest.raises(BenchError) as caught:
        suite_digest("v0")
    assert caught.value.code == "bench_suite_unknown"


@pytest.mark.parametrize(
    "update",
    [
        {"kind": "text"},
        {"sizes": {"smoke": 12, "quick": 40, "full": 40}},
        {"floor_limit": 0.85},
        {"checker": "equality"},
        {"preset": "verifier"},
        {"discriminates": "something else"},
        {"requires": frozenset({"pillow"})},
        {"tiers": frozenset({"quick", "full"})},
    ],
)
def test_suite_digest_changes_on_any_task_edit(
    update: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    edited = tuple(
        task.model_copy(update=update) if task.id == "exam-clean-40" else task
        for task in SUITES["v1"]
    )
    monkeypatch.setattr(suite_module, "SUITES", MappingProxyType({"v1": edited}))
    assert suite_digest("v1") != PINNED_V1_DIGEST


def test_suite_digest_covers_tiers_order_and_generators(monkeypatch: pytest.MonkeyPatch) -> None:
    # Tiers live per suite (Phase 14): v1's digest reads v1's own tier table.
    edited = MappingProxyType({**TIERS, "quick": ("sentiment-24",)})
    monkeypatch.setattr(
        suite_module, "SUITE_TIERS", MappingProxyType({**SUITE_TIERS, "v1": edited})
    )
    assert suite_digest("v1") != PINNED_V1_DIGEST
    monkeypatch.undo()
    reordered = (SUITES["v1"][1], SUITES["v1"][0], *SUITES["v1"][2:])
    monkeypatch.setattr(suite_module, "SUITES", MappingProxyType({"v1": reordered}))
    assert suite_digest("v1") != PINNED_V1_DIGEST
    monkeypatch.undo()
    monkeypatch.setattr(
        suite_module, "GENERATOR_VERSIONS", MappingProxyType({"pages": "v2", "text": "v1"})
    )
    assert suite_digest("v1") != PINNED_V1_DIGEST


def test_suite_table_matches_the_contract() -> None:
    expected = {
        "sentiment-24": ("text", 0.90, "equality", {"smoke": 24, "quick": 24, "full": 24}),
        "tickets-40": ("text", 0.85, "equality", {"smoke": 24, "full": 40}),
        "invoice-40": ("text", 0.90, "invoice_extraction_v1",
                       {"smoke": 24, "quick": 40, "full": 40}),
        "exam-clean-40": ("vision", 0.90, "exam_extraction_v1",
                          {"smoke": 24, "quick": 40, "full": 40}),
        "exam-scan-40": ("vision", 0.70, "exam_extraction_v1", {"smoke": 16, "full": 40}),
        "digitizer-24": ("pipeline", 0.90, "equality", {"smoke": 12, "full": 24}),
        "digitizer-multipage-12": ("pipeline", 0.75, "equality", {"full": 12}),
        "arc-local-3": ("game", 1.0, "equality", {"smoke": 3, "full": 3}),
        "arc-preview": ("game", 0.0, "equality", {"arc": 5}),
    }
    assert list(TASKS) == list(expected)
    for task_id, (kind, limit, checker, sizes) in expected.items():
        task = TASKS[task_id]
        assert (task.kind, task.floor_limit, task.checker, task.sizes) == (
            kind, limit, checker, sizes
        )
        assert task.tiers == frozenset(sizes)
    assert TASKS["exam-scan-40"].requires == {"pillow"}
    assert TASKS["arc-preview"].requires == {"arc"}
    assert not TASKS["exam-clean-40"].requires  # no Pillow for the clean floor
    assert TIERS["smoke"] == (
        "sentiment-24", "tickets-40", "invoice-40", "exam-clean-40", "exam-scan-40",
        "digitizer-24", "arc-local-3",
    )
    assert TIERS["quick"] == ("sentiment-24", "invoice-40", "exam-clean-40")
    assert set(TIERS["full"]) == set(TASKS) - {"arc-preview"}
    assert TIERS["arc"] == ("arc-preview",)
    for tier, members in TIERS.items():
        for task_id in members:
            assert tier in TASKS[task_id].tiers


@pytest.mark.parametrize("task_id", list(TASKS))
@pytest.mark.parametrize("variant", VARIANTS)
def test_spec_builders_validate(task_id: str, variant: str) -> None:
    task = TASKS[task_id]
    spec = task.spec("fake", variant, seed=11, size=24)  # type: ignore[arg-type]
    assert isinstance(Spec.model_validate(spec.model_dump(mode="json")), Spec)
    assert spec.model_target == "fake"
    assert spec.lifecycle == Lifecycle.EXPIRE
    assert spec.floor.dataset == f"bench-{task_id}-v1-11"
    assert spec.floor.limit == task.floor_limit
    assert spec.floor.minimum_samples == 12
    if task.kind == "game":
        assert spec.floor.kind == FloorKind.HELD_OUT_GAME_SCORE
        assert spec.execution_config == {"kind": "game"}
    else:
        assert spec.floor.kind == FloorKind.HELD_OUT_TASKS
        get_preset(task.preset).build(spec)  # every suite spec builds with its preset
    if task.kind == "pipeline":
        names = [step.name for step in spec.pipeline]
        assert names == ["vision-extract", "solver-rederive", "verifier-diff"]
        stages = spec.execution_config["images"]["stages"]  # type: ignore[index,call-overload]
        assert stages == (names if variant == "images-all-stages" else ["vision-extract"])
        assert set(stages) <= set(names)
    else:
        assert "images" not in spec.execution_config


def test_spec_is_deterministic_per_cell() -> None:
    task = TASKS["exam-clean-40"]
    one = task.spec("fake", "default", size=24)
    assert one == task.spec("fake", "default", size=24)
    assert one.floor.minimum_samples == 12
    assert task.spec("fake", "default", size=40).floor.minimum_samples == 20
    assert task.spec("fake", "default").floor.minimum_samples == 20  # default: largest size
    assert task.spec("fake", "default", size=1).floor.minimum_samples == 1


def test_route_maps_pipeline_roles() -> None:
    task = TASKS["digitizer-24"]
    spec = task.spec("fake", "default", route={"vision": "claude-sonnet-5", "verifier": "gpt"})
    assert [step.model for step in spec.pipeline] == ["claude-sonnet-5", "fake", "gpt"]
    with pytest.raises(BenchError) as caught:
        task.spec("fake", "default", route={"reader": "fake"})
    assert caught.value.code == "bench_route_invalid"
    with pytest.raises(BenchError):
        task.spec("fake", "default", route={"vision": ""})
    with pytest.raises(BenchError) as caught:
        task.spec("fake", "loud")  # type: ignore[arg-type]
    assert caught.value.code == "bench_variant_unknown"


def test_max_calls_per_run_per_preset() -> None:
    for task in SUITES["v1"]:
        calls = task.max_calls_per_run("default")
        if task.kind in {"text", "vision"}:
            assert calls == 4  # (1 + retries) * (1 + verifier_depth) at their maxima
        elif task.kind == "pipeline":
            assert calls == 6  # tuned first stage 4 + two fixed single-call stages
        else:
            assert calls > 0
        assert task.max_calls_per_run("images-all-stages") == calls
    ranges = get_preset("extractor").tunable_params
    retries = max(int(str(value)) for value in ranges["retries"].values)
    depth = max(int(str(value)) for value in ranges["verifier_depth"].values)
    assert (1 + retries) * (1 + depth) == 4


def test_sizing_request_carries_images_and_prices_them() -> None:
    task = TASKS["exam-clean-40"]
    cases, _state = task.dataset(7, 2)
    literal = cases[0].task["images"][0]["$image"]  # type: ignore[index,call-overload]
    import base64

    image = ImageInput.from_bytes(base64.b64decode(literal["base64"]))
    text = TASKS["sentiment-24"].sizing_request(())
    vision = task.sizing_request((image,))
    assert vision.images == (image,)
    fake = FakeAdapter("fake")
    assert fake.quote(vision).cost_usd > fake.quote(text).cost_usd
    assert len(vision.prompt) >= 6144


@pytest.mark.parametrize(
    ("task_id", "size"),
    [("sentiment-24", 24), ("tickets-40", 24), ("invoice-40", 24), ("exam-clean-40", 6),
     ("digitizer-24", 4), ("digitizer-multipage-12", 2)],
)
def test_datasets_partition_and_oracle_tables(task_id: str, size: int) -> None:
    task = TASKS[task_id]
    bundle = task.bundle(7, size)
    cases = bundle.cases
    assert len(cases) == size
    assert len({case.case_id for case in cases}) == size
    ids = sorted(case.case_id for case in cases)
    tuning, held_out = ids[0::2], ids[1::2]
    minimum = task.spec("fake", "default", size=size).floor.minimum_samples
    assert len(held_out) == size // 2 == minimum
    assert len(tuning) >= len(held_out)
    if task.kind == "text":
        assert bundle.pages_manifest_sha256 is None
        for case in cases:
            assert bundle.state.text[canonical_key(case.task)] == case.expected
    else:
        assert bundle.pages_manifest_sha256 is not None
        validator = Draft202012Validator(VISION_INPUT_SCHEMA)
        for case in cases:
            validator.validate(case.task)
        table = bundle.state.vision if task.kind == "vision" else bundle.state.digitizer
        assert len(table) == size
    # The noisy quotas see the floor's own split, by case id and task-derived key only.
    by_id = {case.case_id: lookup_key(case.task) for case in cases}
    assert bundle.state.floors == [
        FloorPartition(
            tuning=tuple(by_id[i] for i in tuning),
            held_out=tuple(by_id[i] for i in held_out),
            limit=task.floor_limit,
        )
    ]
    again = task.bundle(7, size)
    assert [c.model_dump() for c in again.cases] == [c.model_dump() for c in cases]
    assert again.pages_manifest_sha256 == bundle.pages_manifest_sha256


def test_multipage_cases_carry_three_pages() -> None:
    cases, _ = TASKS["digitizer-multipage-12"].dataset(7, 1)
    images = cases[0].task["images"]  # type: ignore[index,call-overload]
    assert isinstance(images, list) and len(images) == 3
    assert TASKS["digitizer-multipage-12"].images_per_case() == 3
    assert TASKS["digitizer-24"].images_per_case() == 1
    assert TASKS["sentiment-24"].images_per_case() == 0


def test_delivery_is_disjoint_and_seeded() -> None:
    task = TASKS["invoice-40"]
    cases, _ = task.dataset(7, 24)
    delivery = task.delivery(7, 8)
    assert len(delivery) == 8
    assert {canonical_key(c.task) for c in delivery}.isdisjoint(
        canonical_key(c.task) for c in cases
    )
    seeded = task.dataset(1007, 8)[0]
    assert [c.task for c in delivery] == [c.task for c in seeded]
    assert [c.expected for c in delivery] == [c.expected for c in seeded]
    assert [c.case_id for c in delivery] == [f"delivery-{c.case_id}" for c in seeded]
    assert TASKS["sentiment-24"].delivery(7, 8) == []  # the fixture has no disjoint pool
    vision = TASKS["exam-clean-40"]
    pages, _ = vision.dataset(7, 2)
    extra, state = vision.delivery_dataset(7, 2)
    assert {json.dumps(c.task) for c in extra}.isdisjoint(json.dumps(c.task) for c in pages)
    assert len(state.vision) == 2


@pytest.mark.parametrize("task_id", ["tickets-40", "invoice-40"])
@pytest.mark.parametrize("seed", [0, 7, 11])
def test_text_delivery_never_repeats_a_dataset_case(task_id: str, seed: int) -> None:
    # Ticket templates without placeholders print the same text under any seed, so only
    # the disjoint delivery pool keeps delivery out of the tuning and held-out cases.
    task = TASKS[task_id]
    for size, n in ((24, 24), (40, 40), (120, 120)):
        cases, _ = task.dataset(seed, size)
        delivery, state = task.delivery_dataset(seed, n)
        assert len(delivery) == n
        keys = {canonical_key(c.task) for c in cases}
        assert keys.isdisjoint(canonical_key(c.task) for c in delivery), (size, n)
        assert {c.case_id for c in cases}.isdisjoint(c.case_id for c in delivery)
        assert set(state.text) == {canonical_key(c.task) for c in delivery}
        assert len(state.floors) == 1  # delivery flips are a fixed quota too
        assert all(c.case_id.startswith("delivery-") for c in delivery)


def test_delivery_case_ids_never_collide_with_the_dataset() -> None:
    for task_id in ("exam-clean-40", "digitizer-24"):
        task = TASKS[task_id]
        cases, _ = task.dataset(7, 4)
        delivery = task.delivery(7, 4)
        assert delivery
        assert {c.case_id for c in cases}.isdisjoint(c.case_id for c in delivery)


def test_game_tasks_have_no_cases() -> None:
    cases, state = TASKS["arc-local-3"].dataset(7, 3)
    assert cases == [] and len(state) == 0


def test_scan_dataset_needs_pillow() -> None:
    pytest.importorskip("PIL")
    bundle = TASKS["exam-scan-40"].bundle(7, 3)
    assert len(bundle.cases) == 3
    styles = sorted(str(case.metadata["style"]) for case in bundle.cases)
    assert styles == ["dense", "scan", "scan"]
    assert len(bundle.manifests) == 2
    assert all(isinstance(part["pillow_version"], str) for part in bundle.manifests)


def test_committed_manifests_match_the_generators() -> None:
    generated = example_manifests("v1", seed=7)
    assert sorted(generated) == [
        "invoice-manifest.json",
        "pages-manifest.json",
        "tickets-manifest.json",
    ]
    for name, payload in generated.items():
        committed = json.loads((MANIFESTS / name).read_text(encoding="utf-8"))
        assert committed == payload, name
    text = (MANIFESTS / "pages-manifest.json").read_text(encoding="utf-8")
    assert "base64" not in text and "stem" not in text  # hashes and counts only
    with pytest.raises(BenchError):
        example_manifests("v0")


def test_bench_task_is_frozen() -> None:
    task = TASKS["sentiment-24"]
    with pytest.raises(ValueError):
        task.floor_limit = 0.5
    assert isinstance(task, BenchTask)
