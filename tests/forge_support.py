"""Offline Forge fixtures: scripted Pi sessions, scripted play models and fingerprinted floors.

Nothing here calls a live provider, a CLI agent or Ollama. ``ScriptedPi`` stands in
for the Pi SDK process: it replays a fixed list of tool calls against the five
capability tools exactly as the Python side of ``PiBackend`` would (schema check,
per-tool cap, soft ``tool_failed``), and records every tool result verbatim.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import JsonValue

from architect.backends.pi import PiConfig, TrustedTool
from architect.compiler import CompileConfig, Compiler
from architect.compiler import forge as forge_module
from architect.compiler.forge_settings import ForgeSettings
from architect.floors import EvaluationCase, HeldOutTasks
from architect.library import HarnessLibrary
from architect.models import FakeAdapter, ModelResponse
from architect.runtime import BudgetExceeded, RunContext, Runtime
from architect.spec import Budget, FloorDefinition, FloorKind, Spec
from architect.telemetry import TelemetryStore

SECRET = "PRIVATE_METADATA_NEVER_FOR_THE_AGENT_0001"
HELD_OUT_MARK = "HELDOUT_FINGERPRINT_"
TUNING_MARK = "TUNING_VISIBLE_"

# Three guarded calls, then the correct constant: a quality repair that costs more.
REPAIR = (
    "from pydantic import JsonValue\n"
    "from architect.runtime import RunContext\n\n"
    "async def run(context: RunContext, task: JsonValue) -> JsonValue:\n"
    '    await context.call("first")\n'
    '    await context.call("second")\n'
    '    await context.call("third")\n'
    '    return "ok"\n'
)
CHEAP_WRONG = (
    "from pydantic import JsonValue\n"
    "from architect.runtime import RunContext\n\n"
    "async def run(context: RunContext, task: JsonValue) -> JsonValue:\n"
    '    return "wrong"\n'
)


def reply(
    text: str, *, model: str = "fake", cost: float = 0.001, tokens: int = 10
) -> ModelResponse:
    return ModelResponse(
        text=text, model=model, tokens_in=tokens, tokens_out=tokens, cost_usd=cost
    )


def play_factory(text: str = "nope") -> Callable[[str], FakeAdapter]:
    def factory(model: str) -> FakeAdapter:
        return FakeAdapter(model, responses=[reply(text, model=model)] * 4096)

    return factory


def spec(**overrides: object) -> Spec:
    values: dict[str, object] = dict(
        task="Return ok for every request",
        input_schema={"type": "object"},
        output_schema={"type": "string"},
        domain="synthesizer",
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="forge-tasks"),
        budget=Budget(cost_usd=0.1, compute_s=60),
    )
    values.update(overrides)
    return Spec.model_validate(values)


def cases(count: int = 6, expected: str = "ok") -> list[EvaluationCase]:
    """Even sorted positions are tuning, odd ones held out; held-out text is fingerprinted."""
    result = []
    for index in range(count):
        mark = TUNING_MARK if index % 2 == 0 else HELD_OUT_MARK
        result.append(
            EvaluationCase(
                case_id=f"case-{index:03d}",
                task={"index": index, "text": f"{mark}{index:03d}_input_padding_text"},
                expected=expected,
                metadata={"private": SECRET},
            )
        )
    return result


def floor_factory(count: int = 6) -> Callable[[FloorDefinition], HeldOutTasks]:
    def factory(definition: FloorDefinition) -> HeldOutTasks:
        return HeldOutTasks(definition, cases(count))

    return factory


def held_out_marks(count: int = 6) -> list[str]:
    return [f"{HELD_OUT_MARK}{index:03d}" for index in range(1, count, 2)]


class ScriptedPi:
    """A deterministic stand-in for the Pi process, driven by a fixed tool-call script.

    Each step is ``{"write": {"path", "content"}}``, ``{"tool": name, "arguments": {...}}``
    or ``{"text": final}``. Every step first spends one agent model call through the
    guarded context, so agent cost and tokens are metered exactly as for Pi.
    """

    def __init__(self, steps: Sequence[Mapping[str, Any]]) -> None:
        self.steps = list(steps)
        self.config: PiConfig | None = None
        self.capabilities: dict[str, TrustedTool] = {}
        self.results: list[tuple[str, JsonValue]] = []
        self.snapshot: dict[str, str] = {}
        self.runs = 0
        self.wallet_budgets: list[Budget] = []
        self.is_build: bool | None = None
        self.pack: dict[str, JsonValue] | None = None

    def bind(self, config: PiConfig, capabilities: Mapping[str, TrustedTool]) -> ScriptedPi:
        self.config = config
        self.capabilities = dict(capabilities)
        return self

    def _snapshot(self, root: Path) -> None:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                self.snapshot[path.relative_to(root).as_posix()] = path.read_text(
                    encoding="utf-8", errors="replace"
                )

    async def run(self, context: RunContext, task: JsonValue, pack: dict[str, JsonValue]) -> str:
        from architect.runtime.wallet import active_wallets

        self.runs += 1
        self.pack = pack
        self.is_build = getattr(context, "is_build", None)
        self.wallet_budgets = [wallet.budget for wallet in active_wallets()]
        assert isinstance(task, dict) and isinstance(task["repo"], str)
        root = Path(task["repo"])
        counts: dict[str, int] = {}
        try:
            for step in self.steps:
                await context.call("turn")
                if "text" in step:
                    return str(step["text"])
                if "write" in step:
                    (root / step["write"]["path"]).write_text(
                        step["write"]["content"], encoding="utf-8", newline="\n"
                    )
                    continue
                name = step["tool"]
                arguments = dict(step.get("arguments", {}))
                tool = self.capabilities[name]
                counts[name] = counts.get(name, 0) + 1
                value: JsonValue
                if not Draft202012Validator(tool.parameters).is_valid(arguments):
                    value = {"ok": False, "code": "invalid_arguments"}
                elif counts[name] > tool.max_calls:
                    value = {"ok": False, "code": "forge_tool_cap"}
                else:
                    try:
                        value = await tool.callback(arguments)
                    except BudgetExceeded:
                        raise
                    except Exception:
                        value = {"ok": False, "code": "tool_failed"}
                text = json.dumps(value, ensure_ascii=True, allow_nan=False)
                assert len(text) <= tool.max_result_chars or '"truncated": true' in text
                self.results.append((name, value))
            return "done"
        finally:
            self._snapshot(root)


def install_pi(monkeypatch: Any, pi: ScriptedPi) -> ScriptedPi:
    monkeypatch.setattr(forge_module, "_pi_backend", lambda config, caps: pi.bind(config, caps))
    return pi


def forge_compiler(
    root: Path,
    *,
    settings: ForgeSettings,
    play: str = "nope",
    count: int = 6,
    agent: FakeAdapter | None = None,
    library: HarnessLibrary | None = None,
    compile_budget: Budget | None = None,
) -> Compiler:
    factory = play_factory(play)
    return Compiler(
        root / "artifacts",
        Runtime(TelemetryStore(root / "telemetry")),
        factory,
        floor_factory(count),
        config=CompileConfig(
            max_candidates=1,
            enable_rewriting=False,
            max_rewrites=0,
            forge=settings,
            compile_budget=compile_budget or Budget(cost_usd=10, compute_s=600),
        ),
        library=library if library is not None else HarnessLibrary(root / "library"),
        smoke_factory=floor_factory(count),
        forge_adapter=agent or agent_adapter(),
        pi_config=PiConfig(),
    )


def agent_adapter(turns: int = 64, *, cost: float = 0.0001, tokens: int = 10) -> FakeAdapter:
    return FakeAdapter("fake", responses=[reply("turn", cost=cost, tokens=tokens)] * turns)


_SANDBOX_WORKS: bool | None = None


def _write_probe(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "harness.py").write_text(REPAIR, encoding="utf-8")


async def _probe_sandbox(root: Path) -> bool:
    from architect.presets import get_preset
    from architect.runtime.sandbox import sandbox_harness
    from architect.spec import HarnessArtifact

    preset = get_preset("synthesizer")
    _write_probe(root)
    artifact = HarnessArtifact(
        spec=spec(),
        preset=preset.name,
        model="fake",
        directory=root,
        components=preset.components,
        params=preset.validate_params({}),
        execution_mode="sandbox",
    )
    runtime = Runtime(TelemetryStore(root / "probe-telemetry"))
    try:
        result = await runtime.evaluate(
            artifact.spec, sandbox_harness(artifact), {}, FakeAdapter("fake")
        )
    except Exception:
        return False
    return result.output == "ok"


class SandboxUnavailable(AssertionError):
    """The real sandbox worker failed its probe: Forge tests must fail, never fall back."""


async def ensure_sandbox(monkeypatch: Any, root: Path) -> bool:
    """Require the real sandbox worker (WP1 has landed): there is no test stand-in.

    A regression in the worker must fail the Forge end-to-end, promotion and leakage
    tests rather than let them pass on a stub. ``monkeypatch`` is unused and kept only
    for the existing call sites.
    """
    del monkeypatch
    global _SANDBOX_WORKS
    if _SANDBOX_WORKS is None:
        _SANDBOX_WORKS = await _probe_sandbox(root)
    if not _SANDBOX_WORKS:
        raise SandboxUnavailable("the real sandbox worker failed its probe")
    return True
