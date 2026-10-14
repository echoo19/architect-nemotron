"""Offline scripted-model exit with the real compiler and upstream Pi tools.

--dogfood replays the reviewed Phase6 change in a separate checkout. Active
host enforcement code is never in a Pi write grant.
No live-provider quality, billing or competition claim is made by this fixture.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

from pydantic import JsonValue

from architect.backends.pi import PiBackend, PiEvidence
from architect.compiler import CompileConfig, Compiler
from architect.floors import EvaluationCase, HeldOutTasks
from architect.library import HarnessLibrary
from architect.models import FakeAdapter, ModelResponse
from architect.orchestrator import Orchestrator, OrchestratorConfig
from architect.registry import Registry
from architect.runtime import RunContext, Runtime
from architect.spec import Budget, FloorDefinition, Lifecycle, Spec, TestReport
from architect.telemetry import TelemetryStore

REGISTRY_CODE = """import argparse
import json
from pathlib import Path
from architect.registry import Registry


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("registry", type=Path)
    args = parser.parse_args()
    print(json.dumps([entry.model_dump(mode="json") for entry in Registry(args.registry).list()]))


if __name__ == "__main__":
    main()
"""
ACTION_CODE = '''"""Strict JSON action decoding shared by the local game environments."""

from collections.abc import Collection


class ActionError(ValueError):
    """Invalid model-authored action data; no input content is included."""


def parse_actions(value: object, allowed: Collection[str], maximum: int) -> tuple[str, ...]:
    if type(maximum) is not int or maximum < 1:
        raise ValueError("invalid_action_limit")
    if not isinstance(value, dict) or set(value) != {"actions"}:
        raise ActionError("invalid_actions")
    actions = value["actions"]
    if not isinstance(actions, list) or len(actions) > maximum:
        raise ActionError("invalid_actions")
    choices = frozenset(allowed)
    if any(not isinstance(action, str) or action not in choices for action in actions):
        raise ActionError("invalid_actions")
    return tuple(actions)
'''
ACTION_TEST = """import pytest

from architect.games.actions import ActionError, parse_actions


def test_actions_preserve_order_and_allow_empty():
    assert parse_actions({"actions": ["up", "up"]}, {"up"}, 2) == ("up", "up")
    assert parse_actions({"actions": []}, {"up"}, 1) == ()


@pytest.mark.parametrize("value", [None, [], {"actions": "up"}, {"actions": [True]},
                                  {"actions": ["down"]}, {"actions": ["up"] * 3},
                                  {"actions": [], "license": True}])
def test_invalid_actions(value):
    with pytest.raises(ActionError, match="invalid_actions"):
        parse_actions(value, {"up"}, 2)


@pytest.mark.parametrize("maximum", [0, -1, True, 1.5])
def test_invalid_limit(maximum):
    with pytest.raises(ValueError, match="invalid_action_limit"):
        parse_actions({"actions": []}, {"up"}, maximum)
"""


def response(text: str) -> ModelResponse:
    return ModelResponse(text=text, tokens_in=10, tokens_out=10, cost_usd=0, model="fake")


class EvidencePi(PiBackend):
    def __init__(self) -> None:
        super().__init__()
        self.completed: list[PiEvidence] = []

    async def run(self, context: RunContext, task: JsonValue, pack: dict[str, JsonValue]) -> str:
        result = await super().run(context, task, pack)
        evidence = self.last_evidence
        assert evidence is not None
        self.completed.append(evidence)
        return result


def scripted_files(files: dict[str, str]) -> FakeAdapter:
    replies = [
        response(
            json.dumps(
                {
                    "tool_calls": [
                        {
                            "id": f"write-{i}",
                            "name": "write",
                            "arguments": {"path": path, "content": content},
                        }
                    ]
                }
            )
        )
        for i, (path, content) in enumerate(files.items())
    ]
    return FakeAdapter(responses=[*replies, response("done")])


async def native(*args: str, cwd: Path) -> str:
    key_path = await asyncio.to_thread(
        Path(os.environ.get("ARCHITECT_SIGNING_KEY_FILE", ".architect/signing.key")).resolve
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        *args,
        cwd=cwd,
        env={**os.environ, "ARCHITECT_SIGNING_KEY_FILE": str(key_path)},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output, error = await process.communicate()
    if process.returncode:
        raise AssertionError(error.decode(errors="replace") + output.decode(errors="replace"))
    return output.decode()


def manifest(directory: Path) -> dict[str, str]:
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob("*") if path.is_file()
    }


def reviewed_checkout(source: Path, target: Path, files: dict[str, str]) -> dict[str, str]:
    shutil.copytree(source / "architect", target / "architect",
                    ignore=shutil.ignore_patterns("node_modules", "__pycache__"))
    for name in files:
        (target / name).unlink(missing_ok=True)
    return manifest(target)


async def exercise(workspace: Path, *, dogfood: Path | None = None) -> dict[str, JsonValue]:
    await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=False)
    target = workspace / "delivery"
    await asyncio.to_thread(target.mkdir)
    repositories = [workspace / f"case-{i}" for i in range(4)]
    for repo in repositories:
        await asyncio.to_thread(repo.mkdir)
    definition = FloorDefinition(kind="held_out_tasks", dataset="offline-coding", limit=1)
    cases = [
        EvaluationCase(case_id=f"case-{i}", task={"repo": str(repo)}, expected="done")
        for i, repo in enumerate(repositories)
    ]
    spec = Spec(
        task="Add a registry listing CLI",
        domain="coder",
        input_schema={"type": "object"},
        output_schema={"type": "string"},
        floor=definition,
        tools=("write",),
        budget=Budget(cost_usd=1, compute_s=60),
        model_target="fake",
    )
    backend = EvidencePi()
    current_files = {"registry_cli.py": REGISTRY_CODE}

    def factory(model: str) -> FakeAdapter:
        assert model == "fake"
        return scripted_files(current_files)

    with TelemetryStore(workspace / "telemetry") as store:
        runtime = Runtime(store, adapter_factory=factory, pi_backend=backend)
        compiler = Compiler(
            workspace / "artifacts",
            runtime,
            factory,
            lambda floor: HeldOutTasks(floor, cases),
            library=HarnessLibrary(workspace / "library"),
            config=CompileConfig(max_candidates=1, enable_rewriting=False),
            inference_adapter=FakeAdapter(responses=[response(spec.model_dump_json())] * 3),
        )
        plan = json.dumps(
            {
                "tasks": [
                    {
                        "id": "registry",
                        "request": spec.task,
                        "input": {},
                        "domain": "coder",
                        "model": "fake",
                        "tools": ["write"],
                        "budget": spec.budget.model_dump(),
                    }
                ]
            }
        )
        orchestrator = Orchestrator(
            compiler,
            FakeAdapter(responses=[response(plan)] * 3),
            config=OrchestratorConfig(budget=Budget(cost_usd=10, compute_s=300)),
        )
        reports: list[TestReport] = []
        for index in range(2):
            outcome = await orchestrator.run("add a CLI that prints the registry", repo=target)
            assert outcome.accepted, outcome.model_dump()
            task = outcome.tasks[0]
            assert task.warm_started is (index == 1)
            assert task.artifact_directory is not None
            report_text = await asyncio.to_thread(
                (task.artifact_directory / "report.json").read_text,
                encoding="utf-8",
            )
            reports.append(TestReport.model_validate_json(report_text))
            assert not await asyncio.to_thread((task.artifact_directory / "harness.py").exists)
        assert reports[0].verification_id != reports[1].verification_id
        installed = await compiler.compile(
            spec.model_copy(update={"lifecycle": Lifecycle.INSTALL}), approved=True
        )
        assert installed.artifact is not None and installed.artifact.licensed
        registry = Registry(workspace / "registry")
        registry.install("offline-example", installed.artifact)
        listing = json.loads(
            await native(str(target / "registry_cli.py"), str(registry.root), cwd=target)
        )
        assert [entry["name"] for entry in listing] == ["offline-example"]
        dogfood_target = workspace / "dogfood"
        await asyncio.to_thread(dogfood_target.mkdir, parents=True, exist_ok=True)
        current_files = {
            "architect/games/__init__.py": '"""Local games and strict action decoding."""\n',
            "architect/games/actions.py": ACTION_CODE,
            "tests/test_game_actions.py": ACTION_TEST,
        }
        if dogfood is None:
            current_files["architect/__init__.py"] = '"""Isolated fixture checkout."""\n'
            baseline: dict[str, str] = {}
        else:
            baseline = await asyncio.to_thread(reviewed_checkout, dogfood, dogfood_target,
                                               current_files)
        goal = "Implement the shared strict action decoder for three local game environments"
        dog_plan = json.loads(plan)
        dog_plan["tasks"][0]["id"] = "game-actions"
        dog_plan["tasks"][0]["request"] = goal
        orchestrator.decomposition_adapter = FakeAdapter(responses=[response(json.dumps(dog_plan))])
        outcome = await orchestrator.run(goal, repo=dogfood_target)
        assert outcome.accepted, outcome.model_dump()
        # Review the complete staged diff before executing any generated source.
        expected = {**baseline, **{name: hashlib.sha256(text.encode()).hexdigest()
                                  for name, text in current_files.items()}}
        assert await asyncio.to_thread(manifest, dogfood_target) == expected
        if dogfood is not None:
            for name in ("architect/games/actions.py", "tests/test_game_actions.py"):
                assert await asyncio.to_thread((dogfood / name).read_text) == current_files[name]
        validation = await native(
            "-m", "pytest", "tests/test_game_actions.py", "-q", cwd=dogfood_target
        )
        assert "12 passed" in validation
        assert backend.completed and all(item.extension_loaded for item in backend.completed)
        summary: dict[str, JsonValue] = {
            "mode": "offline_scripted_model_real_pi",
            "live_provider": False,
            "registry_cli_passed": True,
            "reuse_fresh_verification": True,
            "dogfood_native_tests": 12,
            "dogfood_staged": True,
            "reviewed_host_match": dogfood is not None,
            "pi_versions": backend.completed[-1].versions,
            "pi_tool_calls": sum(item.tool_calls for item in backend.completed),
            "telemetry_rows": len(store.rows()),
            "verification_ids": [report.verification_id for report in reports],
        }
        await asyncio.to_thread(
            (workspace / "summary.json").write_text, json.dumps(summary, indent=2), encoding="utf-8"
        )
        return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--dogfood", action="store_true")
    args = parser.parse_args()
    checkout = Path(__file__).resolve().parents[1]
    result = asyncio.run(
        exercise(args.workspace.resolve(), dogfood=checkout if args.dogfood else None)
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
