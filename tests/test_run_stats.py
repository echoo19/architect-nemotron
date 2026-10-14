"""A run's own numbers for the desktop's Try it and ``/run``: dollars, seconds, model calls
and the provider's completion id (W6). Offline: MockTransport and fake adapters."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pydantic import JsonValue

from architect.agent.commands import _run_line
from architect.models import AnthropicAdapter, FakeAdapter, ModelRequest, ModelResponse
from architect.models.http import OpenAICompatibleAdapter, completion_identifier
from architect.runtime import COMPLETION_IDS_KEPT, RunContext, Runtime
from architect.spec import Budget, FloorDefinition, FloorKind, Spec
from architect.telemetry import TelemetryStore


def answer(text: str, completion: str | None) -> ModelResponse:
    return ModelResponse(
        text=text, tokens_in=3, tokens_out=2, cost_usd=0.001, model="fake",
        completion_id=completion,
    )


def spec() -> Spec:
    return Spec(
        task="answer twice",
        input_schema={},
        output_schema={},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.8, dataset="x"),
        budget=Budget(cost_usd=1, compute_s=30),
    )


async def twice(context: RunContext, task: JsonValue) -> JsonValue:
    first = await context.call("one")
    return first + await context.call("two")


def test_only_short_plain_completion_ids_are_kept() -> None:
    assert completion_identifier("chatcmpl-9a7Bc_d.e:f") == "chatcmpl-9a7Bc_d.e:f"
    assert completion_identifier("msg_01XFDUDYJgAACzvnptvVoYEL") == "msg_01XFDUDYJgAACzvnptvVoYEL"
    for junk in (None, 7, "", " spaced", "a b", "x" * 200, "id\n2", "<script>"):
        assert completion_identifier(junk) is None


async def test_openai_compatible_replies_keep_their_completion_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://local.invalid/v1/")
    body = {
        "id": "chatcmpl-abc123",
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    adapter = OpenAICompatibleAdapter(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    )
    response = await adapter.complete(ModelRequest(prompt="hi", max_output_tokens=8))
    assert response.completion_id == "chatcmpl-abc123"
    body["id"] = "not a plain id"
    again = await adapter.complete(ModelRequest(prompt="hi", max_output_tokens=8))
    assert again.completion_id is None and again.text == "ok"


async def test_anthropic_replies_keep_their_message_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is False
        return httpx.Response(200, json={
            "id": "msg_01ABC",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 4, "output_tokens": 2},
        })

    adapter = AnthropicAdapter(api_key="test-key", transport=httpx.MockTransport(handler))
    response = await adapter.complete(ModelRequest(prompt="hi", max_output_tokens=20))
    assert response.completion_id == "msg_01ABC"


async def test_a_run_reports_its_model_calls_and_completion_ids(tmp_path: Path) -> None:
    runtime = Runtime(TelemetryStore(tmp_path / "telemetry"))
    adapter = FakeAdapter("fake", responses=[answer("a", "chatcmpl-1"), answer("b", None)])
    result = await runtime.evaluate(spec(), twice, {}, adapter)
    assert result.output == "ab"
    assert result.model_calls == 2
    assert result.completion_ids == ("chatcmpl-1",)  # a reply without one adds nothing
    assert result.wall_time_s >= 0 and result.run_id


async def test_a_long_run_keeps_its_latest_completion_ids(tmp_path: Path) -> None:
    async def many(context: RunContext, task: JsonValue) -> JsonValue:
        for _ in range(COMPLETION_IDS_KEPT + 3):
            await context.call("again")
        return "done"

    total = COMPLETION_IDS_KEPT + 3
    adapter = FakeAdapter(
        "fake", responses=[answer("x", f"chatcmpl-{index}") for index in range(total)]
    )
    runtime = Runtime(TelemetryStore(tmp_path / "telemetry"))
    result = await runtime.evaluate(
        spec().model_copy(update={"budget": Budget(cost_usd=5, compute_s=30)}), many, {}, adapter
    )
    assert result.model_calls == total
    assert len(result.completion_ids) == COMPLETION_IDS_KEPT
    assert result.completion_ids[-1] == f"chatcmpl-{total - 1}"


def test_the_run_line_says_cost_time_calls_and_the_completion_id() -> None:
    line = _run_line("reader", {"cost_usd": 0.0018, "seconds": 3.62, "calls": 2,
                                "completion_id": "chatcmpl-abc"})
    assert line == "reader: $0.0018, 3.6s, 2 model calls, chatcmpl-abc"
    assert _run_line("reader", {"cost_usd": 0, "calls": 1}) == "reader: $0.0000, 1 model call"
    assert _run_line("reader", {"cost_usd": 0.5}) == "reader: $0.5000"
    # A Lightning run costs millionths: two significant digits, as the desktop app shows.
    tiny = _run_line("reader", {"cost_usd": 3.276e-05, "seconds": 0.797, "calls": 1})
    assert tiny == "reader: $0.000033, 0.8s, 1 model call"
    assert _run_line("reader", {"cost_usd": 5e-05}) == "reader: $0.00005"
