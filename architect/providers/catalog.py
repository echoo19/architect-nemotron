"""Every provider Architect can connect to, as fixed host data (Phase 13, Section 3).

Three kinds:

- ``cli``: a locally logged-in coding-agent CLI. ``claude`` (the Claude Code CLI;
  sign in by running ``claude`` and ``/login`` there) and ``chatgpt`` (a ChatGPT
  plan through the official Codex login: ``codex login`` runs OpenAI's browser
  OAuth flow; models run through ``codex exec``). Their model lists are fixed and
  priced (API-equivalent estimates).
- ``api``: a hosted API with a key. Anthropic uses its own Messages adapter;
  every other one is OpenAI-compatible (Chat Completions at ``base_url``).
- ``local``: a server on this machine (Ollama, LM Studio, llama.cpp, vLLM). $0 per
  token; time is the budget. ``custom`` is any OpenAI-compatible URL, with an
  optional key, and is priced like a hosted API (unpriced unless declared).

``adapter`` is the profile's ``AdapterConfig.provider``. Nothing here performs I/O.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "CATALOG",
    "CLAUDE_MODELS",
    "CODEX_MODELS",
    "AdapterName",
    "Kind",
    "Listing",
    "ProviderEntry",
    "SignIn",
    "default_base_url",
    "entry",
    "find",
    "key_envs",
    "key_required",
    "split_choice",
]

Kind = Literal["cli", "api", "local"]
AdapterName = Literal["claude-code", "codex", "anthropic", "openai", "ollama"]
# How to list models: a fixed list, OpenAI-compatible ``GET {base}/models``,
# Anthropic ``GET /v1/models``, Ollama ``GET /api/tags``, OpenRouter (with prices),
# Nebius Token Factory (``GET {base}/models?verbose=true``, with prices).
Listing = Literal["fixed", "openai", "anthropic", "ollama", "openrouter", "nebius"]
SignIn = Literal["claude_cli", "codex_login", "api_key", "local_url", "custom_url"]

# The same ids ``architect.session.bootstrap`` offers (kept equal by a test; not
# imported, so ``profiles`` -> ``credentials`` -> ``catalog`` never cycles).
CLAUDE_MODELS = (
    "claude-haiku-4-5-20251001",
    "claude-sonnet-5",
    "claude-opus-5-5",
    "claude-fable-5-1",
)
CODEX_MODELS = ("gpt-5.6-terra",)


@dataclass(frozen=True)
class ProviderEntry:
    id: str
    label: str
    kind: Kind
    adapter: AdapterName
    listing: Listing
    sign_in: SignIn
    base_url: str | None = None
    # The primary API key variable, then accepted alternatives (checked in order).
    key_env: str | None = None
    alt_key_envs: tuple[str, ...] = ()
    # Local servers and custom URLs may run without a key.
    key_optional: bool = False
    # An environment variable that overrides ``base_url`` (``OLLAMA_HOST``).
    base_url_env: str | None = None
    default_model: str | None = None
    models: tuple[str, ...] = ()  # fixed lists (CLIs)
    executable: str | None = None  # CLIs: the command looked up on PATH
    key_url: str | None = None  # where a user gets a key
    note: str = ""


CATALOG: tuple[ProviderEntry, ...] = (
    ProviderEntry(
        "claude", "Claude (claude CLI login)", "cli", "claude-code", "fixed", "claude_cli",
        default_model="claude-haiku-4-5-20251001", models=CLAUDE_MODELS, executable="claude",
        note="Uses your Claude subscription through the logged-in claude CLI.",
    ),
    ProviderEntry(
        "chatgpt", "ChatGPT plan (codex login)", "cli", "codex", "fixed", "codex_login",
        default_model="gpt-5.6-terra", models=CODEX_MODELS, executable="codex",
        note="Signs in with OpenAI's official Codex OAuth flow; models run through codex.",
    ),
    ProviderEntry(
        "anthropic", "Anthropic API", "api", "anthropic", "anthropic", "api_key",
        base_url="https://api.anthropic.com", key_env="ANTHROPIC_API_KEY",
        default_model="claude-sonnet-5",
        key_url="https://console.anthropic.com/settings/keys",
    ),
    ProviderEntry(
        "openai", "OpenAI API", "api", "openai", "openai", "api_key",
        base_url="https://api.openai.com/v1", key_env="OPENAI_API_KEY",
        default_model="gpt-4o-mini", key_url="https://platform.openai.com/api-keys",
    ),
    ProviderEntry(
        "nebius", "Nebius Token Factory", "api", "openai", "nebius", "api_key",
        base_url="https://api.tokenfactory.nebius.com/v1", key_env="NEBIUS_API_KEY",
        default_model="nvidia/nemotron-3-super-120b-a12b",
        key_url="https://tokenfactory.nebius.com/project/api-keys",
        note="NVIDIA Nemotron and other open models; prices come from its model list.",
    ),
    ProviderEntry(
        "openrouter", "OpenRouter", "api", "openai", "openrouter", "api_key",
        base_url="https://openrouter.ai/api/v1", key_env="OPENROUTER_API_KEY",
        key_url="https://openrouter.ai/keys",
        note="Hundreds of models behind one key; prices come from its model list.",
    ),
    ProviderEntry(
        "gemini", "Google Gemini API", "api", "openai", "openai", "api_key",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        key_env="GEMINI_API_KEY", alt_key_envs=("GOOGLE_API_KEY",),
        default_model="gemini-2.5-flash", key_url="https://aistudio.google.com/apikey",
    ),
    ProviderEntry(
        "groq", "Groq", "api", "openai", "openai", "api_key",
        base_url="https://api.groq.com/openai/v1", key_env="GROQ_API_KEY",
        default_model="llama-3.3-70b-versatile", key_url="https://console.groq.com/keys",
    ),
    ProviderEntry(
        "deepseek", "DeepSeek", "api", "openai", "openai", "api_key",
        base_url="https://api.deepseek.com/v1", key_env="DEEPSEEK_API_KEY",
        default_model="deepseek-chat", key_url="https://platform.deepseek.com/api_keys",
    ),
    ProviderEntry(
        "xai", "xAI (Grok)", "api", "openai", "openai", "api_key",
        base_url="https://api.x.ai/v1", key_env="XAI_API_KEY",
        key_url="https://console.x.ai",
    ),
    ProviderEntry(
        "mistral", "Mistral", "api", "openai", "openai", "api_key",
        base_url="https://api.mistral.ai/v1", key_env="MISTRAL_API_KEY",
        default_model="mistral-large-latest", key_url="https://console.mistral.ai/api-keys",
    ),
    ProviderEntry(
        "together", "Together AI", "api", "openai", "openai", "api_key",
        base_url="https://api.together.xyz/v1", key_env="TOGETHER_API_KEY",
        key_url="https://api.together.ai/settings/api-keys",
    ),
    ProviderEntry(
        "ollama", "Ollama (local)", "local", "ollama", "ollama", "local_url",
        base_url="http://localhost:11434", base_url_env="OLLAMA_HOST", key_optional=True,
        note="Local models are $0; they run on this machine only when you choose one.",
    ),
    ProviderEntry(
        "lmstudio", "LM Studio (local)", "local", "openai", "openai", "local_url",
        base_url="http://localhost:1234/v1", key_optional=True,
    ),
    ProviderEntry(
        "llamacpp", "llama.cpp server (local)", "local", "openai", "openai", "local_url",
        base_url="http://localhost:8080/v1", key_optional=True,
    ),
    ProviderEntry(
        "vllm", "vLLM (local)", "local", "openai", "openai", "local_url",
        base_url="http://localhost:8000/v1", key_optional=True,
    ),
    ProviderEntry(
        "custom", "Custom OpenAI-compatible URL", "api", "openai", "openai", "custom_url",
        key_optional=True,
        note="Any server speaking the OpenAI Chat Completions API, with or without a key.",
    ),
)

_BY_ID = {item.id: item for item in CATALOG}


def find(provider_id: str) -> ProviderEntry | None:
    return _BY_ID.get(provider_id.strip().lower())


def entry(provider_id: str) -> ProviderEntry:
    """The catalog entry; ``KeyError`` for an unknown id."""
    found = find(provider_id)
    if found is None:
        raise KeyError(provider_id)
    return found


def key_envs(provider_id: str) -> tuple[str, ...]:
    """Environment variables that hold this provider's key, in lookup order."""
    found = find(provider_id)
    if found is None or found.key_env is None:
        return ()
    return (found.key_env, *found.alt_key_envs)


def key_required(credential: str) -> bool:
    """False only for providers that may run keyless (local servers, custom URLs)."""
    found = find(credential)
    return found is None or not found.key_optional


def default_base_url(item: ProviderEntry, env: Mapping[str, str] | None = None) -> str | None:
    """The entry's base URL, honouring its override variable (``OLLAMA_HOST``)."""
    environ = os.environ if env is None else env
    if item.base_url_env:
        configured = environ.get(item.base_url_env, "").strip()
        if configured:
            return configured if "://" in configured else "http://" + configured
    return item.base_url


def split_choice(text: str) -> tuple[ProviderEntry | None, str]:
    """``provider:model`` -> (entry, model); otherwise (None, text).

    Only a catalog id counts as a prefix, so Ollama tags such as ``gemma4:26b``
    stay whole model names while ``ollama:gemma4:26b`` names the provider.
    """
    head, sep, rest = text.strip().partition(":")
    found = find(head) if sep else None
    if found is not None and rest.strip():
        return found, rest.strip()
    return None, text.strip()
