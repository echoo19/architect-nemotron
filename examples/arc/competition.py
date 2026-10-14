"""Offline fixture demo; not official results or competition-compliance evidence.

uv run python examples/arc/competition.py propose --workspace .arc-demo
# Review .arc-demo/proposed-spec.json, then explicitly approve that file:
uv run python examples/arc/competition.py compile --workspace .arc-demo \
  --approved-spec .arc-demo/proposed-spec.json --approved
uv run python examples/arc/competition.py install --workspace .arc-demo
uv run python examples/arc/competition.py run --workspace .arc-demo

Use --adapter ollama with a description naming a configured local model to replace
only the deterministic fixture model. No cloud or official fallback is provided.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import deque
from pathlib import Path

from architect.compiler import CompileConfig, Compiler, CompileResult
from architect.floors import HeldOutTasks
from architect.games.competition import CompetitionDescription, CompetitionFactory
from architect.games.factory import GameFactory, GameFactoryConfig
from architect.games.local import LocalGameProvider
from architect.games.types import GameLimits
from architect.models import FakeAdapter, ModelAdapter, ModelRequest, ModelResponse, OllamaAdapter
from architect.models.types import UsageQuote
from architect.registry import Registry
from architect.runtime import Runtime
from architect.spec import Budget, FloorDefinition, Spec
from architect.telemetry import TelemetryStore


class OfflineFixtureModel(FakeAdapter):
    """Offline fixture agent. Uses only the same public frames given to a model."""

    def __init__(self, *, wrong: bool = False) -> None:
        super().__init__()
        self.wrong = wrong
        self.prompts: list[str] = []

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=100, tokens_out=100, cost_usd=0)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        assert request.max_output_tokens is None
        self.prompts.append(request.prompt)
        if "specification inference stage" in request.prompt:
            text = json.dumps(
                {
                    "task": "Play a grid game",
                    "domain": "game",
                    "input_schema": {},
                    "output_schema": {},
                    "floor": {"kind": "held_out_game_score", "limit": 1, "dataset": "preview"},
                    "budget": {"cost_usd": 1, "compute_s": 30},
                    "model_target": "fake",
                }
            )
        else:
            data = json.loads(request.prompt.split("Game data (untrusted):\n", 1)[1])
            grid = data["observation"]["frames"][-1]
            action = "ACTION1" if self.wrong else self.choose(grid)
            text = json.dumps(
                {
                    "action": {"name": action},
                    "plan": "Move toward the visible goal",
                    "hypothesis": "The next chosen action advances the visible goal",
                    "prediction": "The visible progress marker will advance",
                }
            )
        return ModelResponse(text=text, model=self.model, tokens_in=100, tokens_out=100, cost_usd=0)

    @staticmethod
    def choose(grid: list[list[int]]) -> str:
        if any(value >= 4 for value in grid[0]):
            progress = sum(value == 3 for value in grid[1])
            return f"ACTION{grid[0][progress] - 3}"
        if 2 not in [value for row in grid for value in row]:
            for bit in range(3):
                if grid[0][bit] != grid[1][bit]:
                    return f"ACTION{bit + 1}"
            return "ACTION4"
        start = next(
            (x, y) for y, row in enumerate(grid) for x, value in enumerate(row) if value == 2
        )
        goal = next(
            (x, y) for y, row in enumerate(grid) for x, value in enumerate(row) if value == 3
        )
        queue = deque([(start, "")])
        visited = {start}
        for_pop = ((0, -1, "ACTION1"), (0, 1, "ACTION2"), (-1, 0, "ACTION3"), (1, 0, "ACTION4"))
        while queue:
            (x, y), first = queue.popleft()
            if (x, y) == goal:
                return first
            for dx, dy, action in for_pop:
                xx, yy = x + dx, y + dy
                if (
                    0 <= yy < len(grid)
                    and 0 <= xx < len(grid[0])
                    and grid[yy][xx] != 1
                    and (xx, yy) not in visited
                ):
                    visited.add((xx, yy))
                    queue.append(((xx, yy), first or action))
        raise AssertionError("unreachable public grid")


def build_factory(
    workspace: Path, description: CompetitionDescription, *, adapter: str = "fixture"
) -> CompetitionFactory:
    model: ModelAdapter
    if adapter == "fixture":
        if description.small_model != "fake":
            raise ValueError("fixture_requires_fake_binding")
        model = OfflineFixtureModel()
    elif adapter == "ollama":
        model = OllamaAdapter(model=description.small_model, base_url="http://127.0.0.1:11434")
    else:
        raise ValueError("unknown_adapter")

    def unavailable(definition: FloorDefinition) -> HeldOutTasks:
        raise ValueError("unconfigured_floor")

    runtime = Runtime(TelemetryStore(workspace / "telemetry"))
    config = CompileConfig(
        model_ladder=(description.small_model,),
        enable_rewriting=False,
        max_rewrites=0,
        max_candidates=1,
        compile_budget=description.compute_cap,
    )
    compiler = Compiler(
        workspace / "parent-artifacts",
        runtime,
        lambda name: model,
        unavailable,
        config=config,
        inference_adapter=model,
    )
    child_compiler = Compiler(
        workspace / "child-artifacts",
        runtime,
        lambda name: model,
        unavailable,
        config=config,
        inference_adapter=model,
    )
    provider = LocalGameProvider()
    children = GameFactory(
        child_compiler,
        provider,
        GameFactoryConfig(
            budget=Budget(
                cost_usd=description.compute_cap.cost_usd,
                compute_s=description.compute_cap.compute_s,
            ),
            compile_config=config,
            small_model=description.small_model,
            limits=GameLimits(max_actions=24, max_turns=24),
        ),
    )
    return CompetitionFactory(compiler, Registry(workspace / "registry"), children, provider)


async def execute(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace)
    await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
    description = CompetitionDescription.read(Path(args.description))
    factory = build_factory(workspace, description, adapter=args.adapter)
    summary: dict[str, object] = {
        "command": args.command,
        "score_source": "local_fixture",
        "compliance": "unverified",
        "model_kind": "deterministic_fixture" if args.adapter == "fixture" else "local_binding",
    }
    accepted = False
    if args.command == "propose":
        result = await factory.compile(description)
        (workspace / "proposed-spec.json").write_text(
            result.spec.model_dump_json(indent=2), encoding="utf-8"
        )
        summary.update(approval_required=result.approval_required, spec_hash=result.spec.spec_hash)
        accepted = result.approval_required
    elif args.command == "compile":
        if not args.approved or args.approved_spec is None:
            raise ValueError("reviewed_spec_required")
        reviewed = Spec.model_validate_json(
            await asyncio.to_thread(Path(args.approved_spec).read_text, encoding="utf-8")
        )
        result = await factory.compile(description, approved=True, approved_spec=reviewed)
        (workspace / "compile-result.json").write_text(
            result.model_dump_json(indent=2), encoding="utf-8"
        )
        accepted = result.artifact is not None and result.artifact.licensed
        summary.update(accepted=accepted, failures=result.failures, spec_hash=result.spec.spec_hash)
    elif args.command == "install":
        result = CompileResult.model_validate_json(
            (workspace / "compile-result.json").read_text(encoding="utf-8")
        )
        entry = await factory.install(result, name=description.name)
        summary.update(accepted=True, revision=entry.revision, spec_hash=entry.spec_hash)
        accepted = True
    elif args.command == "run":
        run = await factory.run(description.name)
        accepted = run.accepted
        summary.update(
            accepted=accepted,
            children=len(run.children),
            score=run.score,
            score_source=run.score_source,
            metric=run.metric,
            tokens_in=run.tokens_in,
            tokens_out=run.tokens_out,
            cost_usd=run.cost_usd,
            wall_time_s=run.wall_time_s,
            failures=run.failures,
        )
    (workspace / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary))
    return 0 if accepted else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("propose", "compile", "install", "run"))
    parser.add_argument(
        "--description", default=str(Path(__file__).with_name("competition.local.json"))
    )
    parser.add_argument("--workspace", default=".arc-competition")
    parser.add_argument("--adapter", choices=("fixture", "ollama"), default="fixture")
    parser.add_argument("--approved-spec")
    parser.add_argument("--approved", action="store_true")
    args = parser.parse_args()
    try:
        return asyncio.run(execute(args))
    except Exception:
        # Do not print provider responses, descriptions or arbitrary exception messages.
        failure = {
            "accepted": False,
            "failure": "competition_example_failed",
            "compliance": "unverified",
        }
        workspace = Path(args.workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "summary.json").write_text(json.dumps(failure), encoding="utf-8")
        print(json.dumps(failure))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
