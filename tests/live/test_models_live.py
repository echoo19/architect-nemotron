"""Paid/provider smoke checks. Disabled unless explicitly opted in."""

import os

import pytest

from architect.models import (
    AnthropicAdapter,
    ClaudeCodeAdapter,
    CodexAdapter,
    ModelRequest,
    OllamaAdapter,
    OpenAICompatibleAdapter,
)

pytestmark = pytest.mark.skipif(
    os.getenv("ARCHITECT_LIVE_TESTS") != "1", reason="Explicit live-test opt-in required"
)


@pytest.mark.skipif(not os.getenv("ANTHROPIC_API_KEY"), reason="Anthropic key required")
async def test_anthropic_live() -> None:
    response = await AnthropicAdapter().complete(
        ModelRequest(prompt="Reply ok.", max_output_tokens=8)
    )
    assert response.tokens_in > 0
    assert response.tokens_out > 0


@pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"), reason="OpenAI key required")
async def test_openai_live() -> None:
    response = await OpenAICompatibleAdapter().complete(
        ModelRequest(prompt="Reply ok.", max_output_tokens=8)
    )
    assert response.tokens_in > 0
    assert response.tokens_out > 0


@pytest.mark.skipif(not os.getenv("OLLAMA_HOST"), reason="Explicit Ollama host required")
async def test_ollama_live() -> None:
    response = await OllamaAdapter().complete(ModelRequest(prompt="Reply ok.", max_output_tokens=8))
    assert response.tokens_in > 0
    assert response.tokens_out > 0


# Local CLI adapters use the machine's logged-in subscription; each needs its
# own opt-in on top of ARCHITECT_LIVE_TESTS because it spends plan quota.
@pytest.mark.skipif(
    os.getenv("ARCHITECT_LIVE_CLAUDE_CODE") != "1", reason="Claude Code CLI opt-in required"
)
async def test_claude_code_live() -> None:
    response = await ClaudeCodeAdapter().complete(ModelRequest(prompt="Reply ok."))
    assert response.text
    assert response.tokens_in > 0
    assert response.tokens_out > 0


@pytest.mark.skipif(os.getenv("ARCHITECT_LIVE_CODEX") != "1", reason="Codex CLI opt-in required")
async def test_codex_live() -> None:
    response = await CodexAdapter().complete(ModelRequest(prompt="Reply ok."))
    assert response.text
    assert response.tokens_in > 0
    assert response.tokens_out > 0
