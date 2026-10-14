"""W3 review: the native Pi transport and the Forge pack changes, adversarially.

Each test states the behaviour the branch claims or needs. Each was a strict xfail while
its gap was open; the gaps are fixed, so they are regression tests now.
Offline: MockTransport streams shaped like Nebius replies, scripted adapters, an inert
protocol peer and the real pinned Pi SDK. No live provider is called.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from test_pi_native import Streaming, call, context, pack, reply

from architect.backends.pi import PiBackend, PiConfig, TrustedTool, _ContextCap
from architect.compiler.forge import _RESULT_CHARS, _agent_output_cap
from architect.compiler.forge_pack import pack_parts
from architect.compiler.forge_settings import ForgeSettings
from architect.compiler.forge_tools import _case_view, _first_failures
from architect.compiler.forge_workspace import _tuning_markdown
from architect.floors import EvaluationCase
from architect.models import ModelRequest, ModelResponse
from architect.models.cli import ClaudeCodeAdapter
from architect.models.http import OpenAICompatibleAdapter
from architect.runtime import RunContext
from architect.runtime.evaluation import CaseDetail, GameDetail
from architect.spec import Budget

ROOT = Path(__file__).parents[2]
NEBIUS_URL = "https://api.tokenfactory.nebius.com/v1"
SUPER = "nvidia/nemotron-3-super-120b-a12b"
ULTRA = "nvidia/Nemotron-3-Ultra-550b-a55b"
MEASURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"cases": {"type": "array", "items": {"type": "integer"}}},
}


def sse(*chunks: dict[str, Any]) -> bytes:
    body = b"".join(b"data: " + json.dumps(chunk).encode() + b"\n\n" for chunk in chunks)
    return body + b"data: [DONE]\n\n"


def final(model: str, text: str = "Done.") -> bytes:
    return sse(
        {"model": model, "choices": [{"index": 0, "delta": {"content": text}}]},
        {"model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"model": model, "choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 3}},
    )


class Scripted:
    """One canned SSE body per request, in order; records each request's JSON."""

    def __init__(self, *bodies: bytes) -> None:
        self.bodies = list(bodies)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        return httpx.Response(200, content=self.bodies.pop(0))


def measuring_backend(seen: list[Any]) -> PiBackend:
    async def measure(arguments: dict[str, Any]) -> Any:
        seen.append(arguments)  # "{}" means: measure the first few cases, at a cost
        return {"ok": True, "code": None}

    tool = TrustedTool(
        name="forge_measure",
        description="Measure the harness (default: the first few cases).",
        parameters=MEASURE_SCHEMA,
        callback=measure,
        max_calls=5,
    )
    return PiBackend(
        PiConfig(allowed_tools=("read", "forge_measure")), capabilities={"forge_measure": tool}
    )


# -- a reply cut at the output limit -------------------------------------------------


async def test_a_call_cut_after_its_name_never_runs(tmp_path: Path) -> None:
    cut = sse(
        {"model": SUPER, "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
            {"index": 0, "id": "chatcmpl-tool-1", "type": "function",
             "function": {"name": "forge_measure", "arguments": ""}},
        ]}}]},
        {"model": SUPER, "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]},
        {"model": SUPER, "choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 256}},
    )
    script = Scripted(cut, final(SUPER), final(SUPER))
    adapter = OpenAICompatibleAdapter(
        SUPER, api_key="k", base_url=NEBIUS_URL, dialect="nebius",
        transport=httpx.MockTransport(script),
    )
    seen: list[Any] = []
    ctx = RunContext(
        adapter, Budget(cost_usd=1, compute_s=120), repository=tmp_path,
        is_evaluation=False, is_build=True,
    )
    bound = pack("read", "forge_measure", max_output_tokens=256)
    assert await measuring_backend(seen).run(ctx, {}, bound) == "Done."
    assert seen == []  # nothing from a reply cut inside its last call ever runs


async def test_a_reply_cut_inside_its_reasoning_is_re_asked(tmp_path: Path) -> None:
    thinking = sse(
        {"model": ULTRA, "choices": [{"index": 0, "delta": {
            "reasoning_content": "Plan the harness step by step. " * 40}}]},
        {"model": ULTRA, "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]},
        {"model": ULTRA, "choices": [], "usage": {
            "prompt_tokens": 100, "completion_tokens": 256,
            "completion_tokens_details": {"reasoning_tokens": 256}}},
    )
    script = Scripted(thinking, final(ULTRA))
    adapter = OpenAICompatibleAdapter(
        ULTRA, api_key="k", base_url=NEBIUS_URL, dialect="nebius", think=True,
        transport=httpx.MockTransport(script),
    )
    ctx = RunContext(adapter, Budget(cost_usd=1, compute_s=120), repository=tmp_path)
    backend = PiBackend()
    assert await backend.run(ctx, {}, pack("read", max_output_tokens=256)) == "Done."
    assert len(script.requests) == 2


# -- the parent-side context cap and the bridge's wire limit --------------------------


def _workspace(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "big.txt").write_text("B" * 4000, encoding="utf-8")
    return root


async def _reads(root: Path, cap: str) -> str:
    adapter = Streaming(
        *(reply(call("read", f"r{i}", path="big.txt")) for i in range(8)), reply(text="Done.")
    )
    bound = {**pack("read"), "context_policy": {"max_characters": 6000}}
    config = PiConfig(context_cap=cap, max_protocol_bytes=16384)  # type: ignore[arg-type]
    return await PiBackend(config).run(context(root, adapter), {}, bound)


def test_old_edit_arguments_count_toward_the_cap() -> None:
    def edit(ident: str) -> dict[str, Any]:
        edits = [{"oldText": "o" * 600, "newText": "n" * 600}]
        return {"role": "assistant", "content": [{"type": "toolCall", "id": ident,
                "name": "edit", "arguments": {"path": "harness.py", "edits": edits}}]}

    def done(ident: str) -> dict[str, Any]:
        return {"role": "toolResult", "toolCallId": ident, "toolName": "edit",
                "content": [{"type": "text", "text": "Edited."}], "isError": False}

    messages: list[dict[str, Any]] = [{"role": "user", "content": "task"}]
    for index in range(6):
        messages += [edit(f"e{index}"), done(f"e{index}")]
    value = {"systemPrompt": "sys", "messages": messages}
    # 6 x 1,200 characters of edit arguments against a 2,000-character cap.
    assert _ContextCap(2000).apply(value) is not value


async def test_the_bridge_side_cap_keeps_the_wire_small(tmp_path: Path) -> None:
    # Control: the extension thins tool results before the bridge sends the context.
    assert await _reads(_workspace(tmp_path / "extension"), "extension") == "Done."


async def test_the_parent_cap_keeps_a_long_session_alive(tmp_path: Path) -> None:
    assert await _reads(_workspace(tmp_path / "parent"), "parent") == "Done."


# -- the Forge agent's reply cap ------------------------------------------------------


class LongCLI(ClaudeCodeAdapter):
    """A coding-agent CLI adapter: max_output_tokens sizes its quote only (models/cli.py),
    so a long reply comes back whole and is billed whole."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        tokens_in = len(request.prompt.encode("utf-8"))
        tokens_out = 20_000
        return ModelResponse(
            text='{"text":"done"}', tokens_in=tokens_in, tokens_out=tokens_out,
            cost_usd=self._price.cost(tokens_in, tokens_out), model=self.model,
        )


async def test_a_cli_agent_is_not_capped_below_what_it_can_return() -> None:
    agent = LongCLI("claude-opus-5-5")
    cap = _agent_output_cap(agent, ForgeSettings())
    ctx = RunContext(agent, Budget(cost_usd=5, compute_s=60))
    assert await ctx.call("x" * 2000, max_output_tokens=cap) == '{"text":"done"}'


# -- forge_measure's first-failures detail ----------------------------------------------


def test_aggregate_feedback_keeps_game_process_metrics() -> None:
    game = GameDetail(
        levels_completed=1, actions=40, distinct_states=12, revisit_rate=0.25,
        final_state="NOT_FINISHED", calls_per_action=0.5, trace=None,
    )
    detail = CaseDetail(
        index=0, passed=True, score=1.0, failure=None, sandbox_line=None, calls=3,
        cost_usd=0.001, output_excerpt=None, expected_excerpt=None, game=game,
    )
    [view] = _first_failures([_case_view(detail, "aggregate")], 8)
    assert view["game"] is not None and view["game"]["levels_completed"] == 1


def test_every_case_tuning_md_lists_reaches_the_engineer() -> None:
    cases = [
        EvaluationCase(case_id=f"c{i:03d}", task={"text": f"{i:03d}" + "x" * 1300},
                       expected={"n": i})
        for i in range(30)
    ]
    text = _tuning_markdown(cases, ForgeSettings())
    shown = text.count("## Case ")
    assert 1 <= shown < 16 and f"Cases 0 to {shown - 1} are shown here" in text
    seen = text[:_RESULT_CHARS]  # what one read of TUNING.md returns to the engineer
    assert seen.count("## Case ") == shown


# -- the Forge pack hash ---------------------------------------------------------------


def test_the_pack_covers_the_bridge_system_prompt() -> None:
    bridge = (ROOT / "architect/backends/pi-node/bridge.mjs").read_text(encoding="utf-8")
    extension = (ROOT / "architect/backends/pi-node/extension.ts").read_text(encoding="utf-8")
    fixed = [
        bridge.split('systemPrompt: "', 1)[1].split('"', 1)[0],
        "No autonomous subagent spawning.",
    ]
    assert fixed[1] in extension
    text = "\n".join(pack_parts(ForgeSettings()).values())
    assert all(part in text for part in fixed)
