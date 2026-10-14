"""Fake-only Stage 1 taxonomy and authenticated cache boundary tests."""
from pathlib import Path

import pytest
from pydantic import JsonValue, ValidationError

from architect._authority import _sign_report
from architect.compiler.selection import NullLibrary, Selection, SelectionError, select_preset
from architect.presets import all_presets
from architect.spec import Budget, FloorDefinition, FloorKind, HarnessArtifact, Spec, TestReport


def make_spec(domain: str = "general", shape: JsonValue = "string") -> Spec:
    return Spec(
        task="Summarize a table for a parent",
        input_schema={},
        output_schema={"type": shape},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.8, dataset="fixture"),
        budget=Budget(),
        domain=domain,
    )


class Library:
    def __init__(self, artifact: HarnessArtifact | None) -> None:
        self.artifact = artifact
        self.calls: list[tuple[Spec, str]] = []

    def lookup(self, spec: Spec, model: str) -> HarnessArtifact | None:
        self.calls.append((spec, model))
        return self.artifact

    def __bool__(self) -> bool:
        # An empty library must still receive the lookup.
        return False


@pytest.fixture
def licensed(tmp_path: Path) -> HarnessArtifact:
    (tmp_path / "harness.py").write_text("async def run(context, task): return task\n")
    artifact = HarnessArtifact(
        spec=make_spec(), preset="synthesizer", model="fake",
        components=("synthesis",), directory=tmp_path,
    )
    report = TestReport(
        spec_hash=artifact.spec.spec_hash, artifact_digest=artifact.artifact_digest,
        passed=True, floor_score=1, floor_limit=0.8, floor_kind=FloorKind.HELD_OUT_TASKS,
        tokens=1, cost_usd=0, compute_s=0, component_count=1,
        verification_id="selection-test", held_out_count=1,
    )
    report = report.model_copy(update={
        "signature": _sign_report(report.model_dump(mode="json", exclude={"signature"})),
    })
    artifact = artifact.model_copy(update={"report": report})
    assert artifact.licensed
    return artifact


@pytest.mark.parametrize("domain", [preset.name for preset in all_presets()])
@pytest.mark.parametrize("shape", ["string", "object", "array", "boolean", None])
def test_exact_domain_overrides_shape(domain: str, shape: JsonValue) -> None:
    assert select_preset(make_spec(domain, shape), "fake").preset == domain


@pytest.mark.parametrize("domain,expected", [("coding", "coder"), ("game", "explorer")])
def test_domain_aliases(domain: str, expected: str) -> None:
    assert select_preset(make_spec(domain, "object"), "fake").preset == expected


@pytest.mark.parametrize("domain", ["general", "unknown", "Coding"])
@pytest.mark.parametrize("shape,expected", [
    ("string", "synthesizer"), ("object", "extractor"), ("array", "extractor"),
])
def test_shape_fallback(domain: str, shape: str, expected: str) -> None:
    result = select_preset(make_spec(domain, shape), "fake")
    assert result == Selection(preset=expected)
    assert result.warm_start is None


@pytest.mark.parametrize("shape", [None, "boolean", "integer", "number", [], {}, ["string"]])
def test_unknown_shape_refused(shape: JsonValue) -> None:
    with pytest.raises(SelectionError, match="Unsupported"):
        select_preset(make_spec("unsupported", shape), "fake")


def test_empty_output_refused() -> None:
    spec = make_spec().model_copy(update={"output_schema": {}})
    with pytest.raises(SelectionError):
        select_preset(spec, "fake")


def test_lookup_precedes_even_unsupported_taxonomy(licensed: HarnessArtifact) -> None:
    spec = make_spec("unknown", "boolean")
    library = Library(licensed)
    before = licensed.model_dump()
    selection = select_preset(spec, "fake", library)
    assert library.calls == [(spec, "fake")]
    assert selection.preset == "synthesizer"
    assert selection.warm_start is licensed
    assert licensed.model_dump() == before
    assert licensed.spec.spec_hash != spec.spec_hash
    # Rebinding a source artifact cannot transfer its authenticated report.
    assert not licensed.model_copy(update={"spec": spec}).licensed


def test_library_miss_uses_taxonomy() -> None:
    spec = make_spec("coding")
    library = Library(None)
    assert select_preset(spec, "fake", library) == Selection(preset="coder")
    assert library.calls == [(spec, "fake")]
    assert NullLibrary().lookup(spec, "fake") is None


@pytest.mark.parametrize("damage", ["missing", "forged", "code", "config", "missing_code"])
def test_unlicensed_cache_refused(licensed: HarnessArtifact, damage: str) -> None:
    artifact = licensed
    if damage == "missing":
        artifact = artifact.model_copy(update={"report": None})
    elif damage == "forged":
        assert artifact.report is not None
        artifact = artifact.model_copy(update={
            "report": artifact.report.model_copy(update={"signature": "forged"}),
        })
    elif damage == "code":
        (artifact.directory / "harness.py").write_text("# changed\n")
    elif damage == "missing_code":
        (artifact.directory / "harness.py").unlink()
    else:
        artifact = artifact.model_copy(update={"params": {"retries": 1}})
    assert not artifact.licensed
    with pytest.raises(SelectionError, match="authentically licensed"):
        select_preset(make_spec(), "fake", Library(artifact))


def test_wrong_model_refused(licensed: HarnessArtifact) -> None:
    with pytest.raises(SelectionError, match="model"):
        select_preset(make_spec(), "other", Library(licensed))


def test_repeatable_and_does_not_mutate_input() -> None:
    spec = make_spec()
    before = spec.model_dump()
    results = [select_preset(spec, "fake") for _ in range(4)]
    assert all(result == results[0] for result in results)
    assert spec.model_dump() == before


def test_selection_frozen() -> None:
    selection = Selection(preset="synthesizer")
    with pytest.raises(ValidationError):
        selection.preset = "coder"  # type: ignore[misc]

