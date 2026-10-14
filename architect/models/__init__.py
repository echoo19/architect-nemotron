"""Offline fake, explicitly priced HTTP and local coding-agent CLI model adapters."""

from .cli import ClaudeCodeAdapter, CodexAdapter
from .fake import FakeAdapter
from .http import AnthropicAdapter, OllamaAdapter, OpenAICompatibleAdapter
from .types import AdapterError, ModelAdapter, ModelRequest, ModelResponse, UsageQuote

__all__ = [
    "AdapterError",
    "AnthropicAdapter",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "FakeAdapter",
    "ModelAdapter",
    "ModelRequest",
    "ModelResponse",
    "OllamaAdapter",
    "OpenAICompatibleAdapter",
    "UsageQuote",
]
