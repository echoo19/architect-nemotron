"""Phase 9 Sections 8.3, 8.4 and 14: templates, the Forge workspace and committed examples."""

from __future__ import annotations

import ast
import base64
import json
from pathlib import Path

import pytest
from forge_support import (
    HELD_OUT_MARK,
    SECRET,
    cases,
    held_out_marks,
    spec,
)
from jsonschema import Draft202012Validator

from architect.compiler.forge_settings import ForgeSettings, TemplateName
from architect.compiler.forge_templates import (
    PLACEHOLDERS,
    TEMPLATE_NAMES,
    default_template,
    instantiate,
    template_kind,
    template_source,
)
from architect.compiler.forge_tools import (
    CHECK_PARAMETERS,
    DRYRUN_PARAMETERS,
    MEASURE_PARAMETERS,
    SPAWN_PARAMETERS,
    SUBMIT_PARAMETERS,
)
from architect.compiler.forge_workspace import WORKSPACE_FILES, prepare_workspace
from architect.compiler.types import CandidateEvaluation, Measurement
from architect.floors import EvaluationCase, HeldOutTasks
from architect.games.execution import GameExecutionPolicy
from architect.games.local import LocalFixtureManifest
from architect.games.types import GameRef
from architect.models.images import ImageInput
from architect.presets import get_preset
from architect.spec import (
    Budget,
    FloorDefinition,
    FloorKind,
    ForgeBinding,
    HarnessArtifact,
    Spec,
)

ROOT = Path(__file__).parents[1]
EXAMPLES = ROOT / "examples" / "forge"
MEASUREMENT = Measurement(
    score=0.25,
    floor_passed=False,
    budget_passed=True,
    mean_cost_usd=0.001,
    max_tokens=20,
    max_cost_usd=0.001,
    max_compute_s=0.1,
    sample_count=3,
    failures=("floor_not_met",),
)
IMAGE_INPUT = {
    "type": "object",
    "properties": {
        "page": {
            "type": "object",
            "required": ["$image"],
            "properties": {"$image": {"type": "object", "required": ["media_type"]}},
        }
    },
}


def game_spec() -> Spec:
    policy = GameExecutionPolicy(
        small_model="fake", manifest_digest="b" * 64, support_dataset="support"
    )
    return Spec(
        task="Play",
        input_schema={},
        output_schema={},
        domain="game",
        model_target="fake",
        budget=Budget(cost_usd=1, compute_s=10),
        preset_hint="explorer",
        execution_config=policy.model_dump(mode="json"),
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_GAME_SCORE, limit=1, dataset="support"),
    )


def kind_spec(kind: str) -> tuple[str, Spec]:
    if kind == "game":
        return "explorer", game_spec()
    if kind == "image":
        return "extractor", spec(
            domain="extractor", input_schema=IMAGE_INPUT, output_schema={"type": "object"}
        )
    return "synthesizer", spec()


def candidate(tmp_path: Path, kind: str = "text", *, sandbox: bool = False) -> CandidateEvaluation:
    preset_name, value = kind_spec(kind)
    preset = get_preset(preset_name)
    directory = tmp_path / "stage2"
    preset.emit(directory, value, {}, preset.components)
    artifact = HarnessArtifact(
        spec=value,
        preset=preset.name,
        model="fake",
        directory=directory,
        components=preset.components,
        params=preset.validate_params({}),
        execution_mode="sandbox" if sandbox else "native",
    )
    return CandidateEvaluation(artifact=artifact, measurement=MEASUREMENT)


def targets(name: TemplateName) -> list[str]:
    kind = template_kind(name)
    return ["image", "text"] if kind == "image" else [kind]


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_templates_instantiate_without_placeholders(tmp_path: Path, name: TemplateName) -> None:
    for kind in targets(name):
        source = instantiate(name, candidate(tmp_path / kind, kind))
        assert not any(marker in source for marker in PLACEHOLDERS)
        tree = ast.parse(source)
        runs = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)]
        assert runs[-1].name == "run" and tree.body[-1] is runs[-1]
        assert source.isascii()


@pytest.mark.parametrize("name", ["prompt_chain", "extract_verify", "vision_two_pass"])
def test_binding_literal_is_exactly_the_preset_emission(tmp_path: Path, name: str) -> None:
    evaluation = candidate(tmp_path)
    emitted = ast.parse((evaluation.artifact.directory / "harness.py").read_text("utf-8"))
    source = ast.parse(instantiate(name, evaluation))  # type: ignore[arg-type]

    def bindings(tree: ast.AST) -> list[str]:
        return [
            ast.dump(ast.Tuple(elts=node.args[:4], ctx=ast.Load()))
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "execute_preset"
        ]

    expected = bindings(emitted)
    assert len(expected) == 1
    assert bindings(source) and set(bindings(source)) == set(expected)


def test_instantiation_is_single_pass(tmp_path: Path) -> None:
    evaluation = candidate(tmp_path)
    tricky = evaluation.artifact.spec.model_copy(update={"task": "Say $OUTPUT_SCHEMA please"})
    artifact = evaluation.artifact.model_copy(update={"spec": tricky})
    changed = CandidateEvaluation(artifact=artifact, measurement=MEASUREMENT)
    source = instantiate("prompt_chain", changed)
    assert "TASK = 'Say $OUTPUT_SCHEMA please'" in source


def test_unknown_template_is_refused() -> None:
    with pytest.raises(ValueError, match="Unknown forge template"):
        template_source("nope")  # type: ignore[arg-type]


def test_game_explorer_is_the_committed_arc_explorer() -> None:
    committed = (EXAMPLES / "arc_explorer.py").read_text(encoding="utf-8")
    assert template_source("game_explorer") == committed
    assert "execute_preset" not in committed


def test_default_template_by_spec_kind(tmp_path: Path) -> None:
    assert default_template(kind_spec("game")[1]) == "game_explorer"
    assert default_template(kind_spec("image")[1]) == "vision_two_pass"
    assert default_template(kind_spec("text")[1]) == "extract_verify"


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_templates_validate_under_v2_for_every_targeted_kind(
    tmp_path: Path, name: TemplateName
) -> None:
    from architect.runtime.sandbox import validate_source

    for kind in targets(name):
        evaluation = candidate(tmp_path / kind, kind)
        artifact = evaluation.artifact.model_copy(
            update={
                "execution_mode": "sandbox",
                "forge": ForgeBinding(call_limit=512, fuel=10_000_000),
            }
        )
        stats = validate_source(instantiate(name, evaluation), artifact)
        assert getattr(stats, "language", 2) == 2


# Workspace -------------------------------------------------------------------


def floor(records: list[EvaluationCase]) -> HeldOutTasks:
    return HeldOutTasks(
        FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="forge-tasks"), records
    )


def test_workspace_files_and_no_held_out_or_metadata(tmp_path: Path) -> None:
    evaluation = candidate(tmp_path)
    root = prepare_workspace(tmp_path / "ws", evaluation, floor(cases(6)), ForgeSettings())
    names = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    assert set(WORKSPACE_FILES) <= names
    assert {f"templates/{name}.py" for name in TEMPLATE_NAMES} <= names
    text = "\n".join(path.read_text("utf-8") for path in root.rglob("*") if path.is_file())
    assert SECRET not in text and HELD_OUT_MARK not in text
    assert all(mark not in text for mark in held_out_marks())
    # No dataset path or name outside the exact execute_preset binding literal, which
    # must repeat the spec byte for byte (the Pi denied paths keep the file unreadable).
    for name in ("SPEC.md", "TUNING.md", "BASELINE.json", "LANGUAGE.md", "NOTES.md"):
        assert "forge-tasks" not in (root / name).read_text("utf-8")
    tuning = (root / "TUNING.md").read_text("utf-8")
    assert "## Case 2" in tuning and "## Case 3" not in tuning
    assert '"ok"' in tuning  # expected values shown in per_case mode
    assert (root / "NOTES.md").read_text("utf-8") == ""
    baseline = json.loads((root / "BASELINE.json").read_text("utf-8"))
    assert set(baseline) == {"preset", "params", "measurement"}
    assert "failures" not in baseline["measurement"]
    assert baseline["measurement"]["score"] == 0.25
    spec_text = (root / "SPEC.md").read_text("utf-8")
    assert "play model: fake" in spec_text and "held_out_tasks" in spec_text
    # A native Stage 2 best has no sandbox source: the default template is the incumbent.
    assert (root / "harness.py").read_text("utf-8") == instantiate("extract_verify", evaluation)


def test_aggregate_mode_withholds_expected_and_truncates_inputs(tmp_path: Path) -> None:
    records = [
        EvaluationCase(case_id=f"c{i}", task={"text": "z" * 5000}, expected="LABEL_VALUE")
        for i in range(4)
    ]
    settings = ForgeSettings(feedback="aggregate", case_chars=200)
    root = prepare_workspace(tmp_path / "ws", candidate(tmp_path), floor(records), settings)
    tuning = (root / "TUNING.md").read_text("utf-8")
    assert "LABEL_VALUE" not in tuning and "expected:" not in tuning
    assert "z" * 201 not in tuning and "[truncated]" in tuning


def test_sandbox_incumbent_and_explicit_template(tmp_path: Path) -> None:
    evaluation = candidate(tmp_path, sandbox=True)
    own = (evaluation.artifact.directory / "harness.py").read_text("utf-8")
    root = prepare_workspace(tmp_path / "a", evaluation, floor(cases()), ForgeSettings())
    assert (root / "harness.py").read_text("utf-8") == own.replace("\r\n", "\n")
    template = ForgeSettings(mode="template", template="prompt_chain")
    root = prepare_workspace(tmp_path / "b", evaluation, floor(cases()), template)
    assert (root / "harness.py").read_text("utf-8") == instantiate("prompt_chain", evaluation)


def test_images_appear_only_as_stub_metadata(tmp_path: Path) -> None:
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    image = ImageInput.from_bytes(png)
    encoded = base64.b64encode(png).decode("ascii")
    records = [
        EvaluationCase(
            case_id=f"c{i}",
            task={"page": {"$image": {"media_type": "image/png", "base64": encoded}}},
            expected={"n": i},
        )
        for i in range(4)
    ]
    root = prepare_workspace(tmp_path / "ws", candidate(tmp_path), floor(records), ForgeSettings())
    tuning = (root / "TUNING.md").read_text("utf-8")
    assert encoded not in tuning and image.sha256 not in tuning
    assert image.sha256[:12] in tuning and '"width": 1' in tuning


def test_game_ids_are_listed_for_tuning_games_only(tmp_path: Path) -> None:
    records = [
        EvaluationCase(
            case_id=f"g{i}",
            task={"game": {"backend": "local", "game_id": f"game-{i}", "revision": "r1"}},
            expected=None,
        )
        for i in range(4)
    ]
    root = prepare_workspace(tmp_path / "ws", candidate(tmp_path), floor(records), ForgeSettings())
    tuning = (root / "TUNING.md").read_text("utf-8")
    assert "- game-0" in tuning and "- game-2" in tuning
    assert "game-1" not in tuning and "game-3" not in tuning


def test_tuning_games_carry_a_first_observation_summary(tmp_path: Path) -> None:
    """Section 8.3: dimensions, colour counts and available actions per tuning game."""
    manifest = LocalFixtureManifest()
    refs = {
        ref.game_id: ref
        for role in ("parent_tuning", "parent_held_out")
        for ref in manifest.refs(role)
    }
    offline = GameRef(backend="official_offline", game_id="ft09-offline", revision="r1")
    tuning = [
        refs["local-navigation-v1-P0"],
        refs["local-switches-v1-P0"],
        refs["local-sequence-v1-P0"],
        offline,
    ]
    held_out = [
        refs["local-navigation-v1-P1"],
        refs["local-switches-v1-P1"],
        refs["local-sequence-v1-P1"],
        GameRef(backend="official_offline", game_id="ls20-hidden", revision="r1"),
    ]
    records = [
        EvaluationCase(
            case_id=f"g{2 * index + offset:02d}",
            task={"game": ref.model_dump(mode="json")},
            expected=None,
        )
        for index, pair in enumerate(zip(tuning, held_out, strict=True))
        for offset, ref in enumerate(pair)
    ]
    root = prepare_workspace(tmp_path / "ws", candidate(tmp_path), floor(records), ForgeSettings())
    tuning_md = (root / "TUNING.md").read_text("utf-8")
    assert (
        "- local-navigation-v1-P0: 7x7 frame (width x height); "
        "colour counts 0:45, 1:2, 2:1, 3:1; available actions ACTION1, ACTION2, ACTION3, ACTION4"
    ) in tuning_md
    assert (
        "- local-switches-v1-P0: 7x7 frame (width x height); "
        "colour counts 0:43, 1:5, 3:1; available actions ACTION1, ACTION2, ACTION3, ACTION4"
    ) in tuning_md
    assert (
        "- local-sequence-v1-P0: 7x7 frame (width x height); "
        "colour counts 0:43, 1:3, 4:1, 5:1, 6:1; available actions ACTION1, ACTION2, ACTION3"
    ) in tuning_md
    # RESET is withheld on a fresh first prompt, as the harness sees it.
    assert "RESET" not in tuning_md
    # official_offline games need the staged worker: no summary, a dryrun pointer instead.
    assert "- ft09-offline: first observation not available offline; use forge_dryrun" in tuning_md
    # Held-out games never appear, neither by id nor by summary.
    for ref in held_out:
        assert ref.game_id not in tuning_md
    tuning_md.encode("ascii")


# Committed examples ------------------------------------------------------------

_SCHEMAS = {
    "forge_check": CHECK_PARAMETERS,
    "forge_dryrun": DRYRUN_PARAMETERS,
    "forge_measure": MEASURE_PARAMETERS,
    "forge_submit": SUBMIT_PARAMETERS,
    "forge_spawn": SPAWN_PARAMETERS,
}


def test_forge_scripts_are_valid_pi_envelopes() -> None:
    scripts = sorted((EXAMPLES / "scripts").glob("*.json"))
    assert {path.stem for path in scripts} >= {"arc-local-3", "sentiment-24", "invoice-40"}
    for path in scripts:
        steps = json.loads(path.read_text("utf-8"))
        assert isinstance(steps, list) and steps
        names = []
        for step in steps:
            assert set(step) <= {"text", "tool_calls"}
            for call in step.get("tool_calls", []):
                assert set(call) == {"id", "name", "arguments"}
                name, arguments = call["name"], dict(call["arguments"])
                names.append(name)
                if name == "write":
                    assert arguments["path"] == "harness.py"
                    assert (EXAMPLES / arguments["content_from"]).is_file()
                else:
                    Draft202012Validator(_SCHEMAS[name]).validate(arguments)
        # Every script measures and submits; Forge must never silently not run.
        assert "forge_measure" in names and "forge_submit" in names
        if path.stem.startswith("arc-"):
            assert names[0] == "write"


def test_arc_explorer_is_ascii_and_lint_clean_reference() -> None:
    source = (EXAMPLES / "arc_explorer.py").read_text(encoding="utf-8")
    assert source.isascii()
    tree = ast.parse(source)
    assert [node.name for node in tree.body if isinstance(node, ast.AsyncFunctionDef)][-1] == "run"


def test_forge_profile_matches_the_arc_experiment() -> None:
    profile = json.loads((ROOT / "examples/local/profile-forge.json").read_text("utf-8"))
    play = profile["models"]["granite4.2:8b"]
    assert play["num_ctx"] == 16384 and play["think"] is False
    forge = ForgeSettings.model_validate(profile["compile"]["forge"])
    assert forge.enabled and forge.trigger == "always" and forge.max_usd == 5
    assert profile["forge_agent"]["provider"] == "claude-code"
    assert profile["compile"]["model_ladder"] == ["granite4.2:8b"]
    from architect import profiles

    if "forge_agent" in profiles.Profile.model_fields:
        loaded = profiles.load_profile(ROOT / "examples/local/profile-forge.json")
        assert loaded.compile.forge == forge
