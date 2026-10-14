"""Per-call usage from OpenAI-compatible providers: reasoning and cache-hit tokens.

Nebius reports cache hits as ``prompt_tokens_details.cached_tokens`` on Ultra and Super but
only as ``prompt_cache_hit_tokens`` on Lightning (live, 2026-10-03), and reasoning as
``completion_tokens_details.reasoning_tokens``. Offline: MockTransport only.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from architect.models import ModelRequest
from architect.models.chat import ChatMessage, UsageEvent
from architect.models.http import OpenAICompatibleAdapter

NEBIUS_URL = "https://api.tokenfactory.nebius.com/v1"
LIGHTNING = "nvidia/Nemotron-3_5-Lightning"


def usage_body() -> dict[str, Any]:
    return {
        "prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130,
        "prompt_cache_hit_tokens": 64, "prompt_cache_miss_tokens": 36,
        "completion_tokens_details": {"reasoning_tokens": 12},
    }


@pytest.mark.parametrize("dialect", ["nebius", "openai"])
async def test_usage_reports_reasoning_and_nebius_cache_hits(dialect: str) -> None:
    def complete(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "model": LIGHTNING,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                         "finish_reason": "stop"}],
            "usage": usage_body(),
        })

    adapter = OpenAICompatibleAdapter(
        LIGHTNING, api_key="k", base_url=NEBIUS_URL, dialect=dialect,  # type: ignore[arg-type]
        transport=httpx.MockTransport(complete),
    )
    response = await adapter.complete(ModelRequest(prompt="x", max_output_tokens=64))
    cached = 64 if dialect == "nebius" else 0  # Lightning reports only this field
    assert (response.cache_read_tokens, response.reasoning_tokens) == (cached, 12)

    def stream(request: httpx.Request) -> httpx.Response:
        chunks = [
            {"model": LIGHTNING, "choices": [{"index": 0, "delta": {"content": "hi"}}]},
            {"model": LIGHTNING, "choices": [{"index": 0, "delta": {},
                                              "finish_reason": "stop"}]},
            {"model": LIGHTNING, "choices": [], "usage": usage_body()},
        ]
        body = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks)
        return httpx.Response(200, content=body + b"data: [DONE]\n\n")

    streaming = OpenAICompatibleAdapter(
        LIGHTNING, api_key="k", base_url=NEBIUS_URL, dialect=dialect,  # type: ignore[arg-type]
        transport=httpx.MockTransport(stream),
    )
    events = [e async for e in streaming.stream_chat("", (ChatMessage("user", "x"),), (), 64)]
    usage = next(e for e in events if isinstance(e, UsageEvent))
    assert (usage.cache_read_tokens, usage.reasoning_tokens) == (cached, 12)


@pytest.mark.parametrize(
    ("dialect", "usage", "expected"),
    [
        # The detailed field wins when a provider reports both.
        ("nebius", {"prompt_tokens_details": {"cached_tokens": 40}, "prompt_cache_hit_tokens": 64},
         (40, 0)),
        # A hit count above the prompt is held at the prompt.
        ("nebius", {"prompt_cache_hit_tokens": 500}, (100, 0)),
        # Malformed or absent counts are 0, never an error.
        ("nebius",
         {"prompt_cache_hit_tokens": "64", "completion_tokens_details": {"reasoning_tokens": -1}},
         (0, 0)),
        ("nebius", {"completion_tokens_details": None}, (0, 0)),
        # Reasoning never exceeds the completion it is part of. (On Nebius a reply that is
        # all reasoning is REASONING_CUT instead.)
        ("openai", {"completion_tokens_details": {"reasoning_tokens": 99}}, (0, 30)),
    ],
)
async def test_usage_details_are_read_defensively(
    dialect: str, usage: dict[str, Any], expected: tuple[int, int]
) -> None:
    def complete(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "model": LIGHTNING,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 30, **usage},
        })

    adapter = OpenAICompatibleAdapter(
        LIGHTNING, api_key="k", base_url=NEBIUS_URL, dialect=dialect,  # type: ignore[arg-type]
        transport=httpx.MockTransport(complete),
    )
    response = await adapter.complete(ModelRequest(prompt="x", max_output_tokens=64))
    assert (response.cache_read_tokens, response.reasoning_tokens) == expected
