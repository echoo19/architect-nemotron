"""Phase 13 Package A: providers, credentials, discovery, status, onboarding and pricing.

Offline only: HTTP goes through ``httpx.MockTransport``, PATH lookups and the login
runner are fakes, and no CLI, provider or local model server is ever contacted.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from architect import profiles
from architect.models import OllamaAdapter, OpenAICompatibleAdapter
from architect.models.pricing import (
    DEFAULT_OUTPUT_CAPACITY,
    UNPRICED_LABEL,
    Price,
    is_unpriced,
    load_prices,
    local_price,
    per_token_price,
    unpriced_price,
    write_price_entries,
)
from architect.models.types import AdapterError, ModelRequest
from architect.profiles import (
    ProfileError,
    build_services,
    forge_settings_of,
    load_profile,
    model_unpriced,
    pi_denied_paths,
)
from architect.providers import credentials
from architect.providers.catalog import (
    CATALOG,
    CLAUDE_MODELS,
    CODEX_MODELS,
    default_base_url,
    entry,
    key_required,
    split_choice,
)
from architect.providers.discovery import Discovered, list_models
from architect.providers.host import Host
from architect.providers.onboarding import (
    NEBIUS_COMPILE_BUDGET,
    NEBIUS_FORGE,
    NEBIUS_ROLES,
    NEMOTRON_MODELS,
    PRICES_FILE,
    ProviderError,
    onboarding_choices,
    provider_of,
    recommended,
    register_model,
)
from architect.providers.signin import (
    Prompter,
    choose_model,
    first_run,
    onboard,
    price_label,
    sign_in,
)
from architect.providers.status import all_statuses, provider_status
from architect.session import bootstrap

KEY = "sk-test-SECRET-0123456789"
posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")


# -- helpers ----------------------------------------------------------------------------


Handler = Callable[[httpx.Request], httpx.Response]


def transport(routes: dict[str, Any] | None = None, seen: list[httpx.Request] | None = None,
              ) -> httpx.MockTransport:
    """Answer by URL prefix; anything unrouted is a refused connection."""
    table = routes or {}

    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        url = str(request.url)
        for prefix, answer in table.items():
            if url.startswith(prefix):
                if callable(answer):
                    return answer(request)  # type: ignore[no-any-return]
                if isinstance(answer, int):
                    return httpx.Response(answer, json={"error": "x"})
                return httpx.Response(200, json=answer)
        raise httpx.ConnectError("refused", request=request)

    return httpx.MockTransport(handle)


def host(tmp_path: Path, *, which: dict[str, str] | None = None, env: dict[str, str] | None = None,
         routes: dict[str, Any] | None = None, seen: list[httpx.Request] | None = None,
         runner: Callable[[Sequence[str]], int] | None = None) -> Host:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    found = which or {}
    return Host(
        which=lambda name: found.get(name),
        env=env or {},
        home=home,
        transport=transport(routes, seen),
        runner=runner or (lambda argv: pytest.fail(f"unexpected run {argv}")),
    )


class Recorder:
    def __init__(self, *answers: str | None) -> None:
        self.answers = list(answers)
        self.said: list[tuple[str, str]] = []
        self.asked: list[str] = []

    def say(self, text: str, level: str = "info") -> None:
        self.said.append((text, level))

    async def ask(self, question: str, *, default: str | None = None,
                  choices: Sequence[str] = ()) -> str | None:
        self.asked.append(question)
        if not self.answers:
            return None
        answer = self.answers.pop(0)
        if answer is None:
            return None
        return answer.strip() or (default if default is not None else "")

    def prompter(self) -> Prompter:
        return Prompter(self.say, self.ask)

    def text(self) -> str:
        return "\n".join(text for text, _ in self.said) + "\n" + "\n".join(self.asked)


OPENAI_MODELS = {"object": "list", "data": [{"id": "gpt-4o-mini"}, {"id": "gpt-4.1"},
                                            {"id": "brand-new-model"}]}
SUPER = "nvidia/nemotron-3-super-120b-a12b"
ULTRA = "nvidia/Nemotron-3-Ultra-550b-a55b"
LIGHTNING = "nvidia/Nemotron-3_5-Lightning"
# The shape of Nebius's GET /v1/models?verbose=true (rows trimmed; captured 2026-10-03).
NEBIUS_MODELS = {"object": "list", "data": [
    {"id": SUPER, "context_length": 262144, "object": "model",
     "architecture": {"modality": "text->text", "tokenizer": "Other"},
     "pricing": {"prompt": "0.0000003", "completion": "0.0000009", "image": "0",
                 "request": "0", "input_cache_read": None},
     "supported_features": ["tools", "reasoning"]},
    {"id": ULTRA, "context_length": 1048576,
     "architecture": {"modality": "text->text"},
     "pricing": {"prompt": "0.000001", "completion": "0.000003"}},
    {"id": "moonshotai/Kimi-K3", "context_length": 1024000,
     "architecture": {"modality": "text+image->text"},
     "pricing": {"prompt": "0.000003", "completion": "0.000015"}},
    {"id": "Qwen/Qwen3-Embedding-8B", "context_length": 40960,
     "architecture": {"modality": "text->embedding"},
     "pricing": {"prompt": "0.00000001", "completion": "0.0"}},
    {"id": "house/no-architecture", "pricing": {"prompt": "x", "completion": "0.000001"}},
]}


# -- catalog ----------------------------------------------------------------------------


def test_catalog_has_every_spec_provider_with_valid_adapters() -> None:
    ids = [item.id for item in CATALOG]
    assert ids == ["claude", "chatgpt", "anthropic", "openai", "nebius", "openrouter", "gemini",
                   "groq", "deepseek", "xai", "mistral", "together", "ollama", "lmstudio",
                   "llamacpp", "vllm", "custom"]
    adapters = {"fake", "anthropic", "openai", "ollama", "claude-code", "codex"}
    for item in CATALOG:
        assert item.adapter in adapters
        if item.kind == "api" and item.id != "custom":
            assert item.key_env and item.base_url and item.key_url, item.id
        if item.kind == "local":
            assert item.key_optional and item.base_url and item.base_url.startswith("http://localhost")
    assert entry("gemini").base_url == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert entry("lmstudio").base_url == "http://localhost:1234/v1"
    assert entry("llamacpp").base_url == "http://localhost:8080/v1"
    assert entry("vllm").base_url == "http://localhost:8000/v1"
    assert entry("chatgpt").sign_in == "codex_login" and entry("chatgpt").adapter == "codex"
    # The CLI lists are the bootstrap's, and every one is priced.
    assert CLAUDE_MODELS == bootstrap.CLAUDE_MODELS and CODEX_MODELS == bootstrap.CODEX_MODELS
    priced = load_prices().models
    assert all(model in priced for model in (*CLAUDE_MODELS, *CODEX_MODELS))
    # Recommended defaults for keyed providers are priced where the table knows them.
    for provider in ("anthropic", "openai", "nebius"):
        assert not is_unpriced(entry(provider).default_model or "")
    nebius = entry("nebius")
    assert (nebius.adapter, nebius.listing, nebius.sign_in) == ("openai", "nebius", "api_key")
    assert nebius.base_url == "https://api.tokenfactory.nebius.com/v1"
    assert nebius.key_env == "NEBIUS_API_KEY" and not nebius.key_optional
    assert nebius.default_model == "nvidia/nemotron-3-super-120b-a12b"
    assert nebius.key_url == "https://tokenfactory.nebius.com/project/api-keys"
    with pytest.raises(KeyError):
        entry("nope")


def test_nemotron_prices_are_bundled_exactly_as_cased() -> None:
    """Nebius's public catalog (models_info, 2026-10-03), per million tokens."""
    priced = load_prices().models
    expected = {
        "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B": (0.06, 0.24, 16384),
        "nvidia/Nemotron-3_5-Lightning": (0.06, 0.24, 16384),
        "nvidia/nemotron-3-super-120b-a12b": (0.30, 0.90, 16384),
        "nvidia/Nemotron-3-Ultra-550b-a55b": (1.00, 3.00, 32768),
    }
    assert set(NEMOTRON_MODELS) == set(expected)
    for model, (rate_in, rate_out, capacity) in expected.items():
        price = priced[model]
        assert (price.input_per_million, price.output_per_million) == (rate_in, rate_out)
        assert price.output_capacity_tokens == capacity and price.input_overhead_tokens == 1024
        assert price.allowed_response_models == (model,) and price.image is None
        assert price.accepts_response_model(model, model)
        if model != model.lower():  # ids are case-sensitive: a lowercased name is refused
            assert not price.accepts_response_model(model.lower(), model)
        assert not is_unpriced(model)


def test_split_choice_keeps_ollama_tags_whole() -> None:
    assert split_choice("openai:gpt-5") == (entry("openai"), "gpt-5")
    assert split_choice("ollama:gemma4:26b") == (entry("ollama"), "gemma4:26b")
    assert split_choice("gemma4:26b") == (None, "gemma4:26b")
    routed = split_choice("openrouter:anthropic/claude-sonnet-4.5")
    assert routed[1] == "anthropic/claude-sonnet-4.5"
    assert split_choice("openai:") == (None, "openai:")
    assert default_base_url(entry("ollama"), {"OLLAMA_HOST": "10.0.0.2:11434"}) == (
        "http://10.0.0.2:11434")
    assert default_base_url(entry("ollama"), {}) == "http://localhost:11434"
    assert key_required("openai") and key_required("unknown") and not key_required("ollama")


# -- credentials ------------------------------------------------------------------------


def test_credentials_env_first_then_file_and_never_in_errors(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    assert credentials.get("openai", ws, env={}) is None
    credentials.set_key("openai", f"  {KEY}\n", ws)
    assert credentials.get("openai", ws, env={}) == KEY
    assert credentials.source("openai", ws, env={}) == "file"
    assert credentials.get("openai", ws, env={"OPENAI_API_KEY": "from-env"}) == "from-env"
    assert credentials.source("openai", ws, env={"OPENAI_API_KEY": "x"}) == "OPENAI_API_KEY"
    assert credentials.get("gemini", ws, env={"GOOGLE_API_KEY": "g"}) == "g"  # alternate var
    assert credentials.names(ws) == ("openai",)
    stored = json.loads((ws / "credentials.json").read_text(encoding="utf-8"))
    assert stored["version"] == 1 and stored["keys"] == {"openai": KEY}
    for bad in ("", "two words", "line\nbreak", "x" * 5000):
        with pytest.raises(credentials.CredentialError) as caught:
            credentials.set_key("openai", bad, ws)
        assert caught.value.code == "invalid_key" and str(caught.value) == "invalid_key"
    with pytest.raises(credentials.CredentialError, match="^invalid_name$"):
        credentials.set_key("../evil", KEY, ws)
    # Keyless-capable providers resolve to "" (no OPENAI_API_KEY leaks to them).
    assert credentials.resolve("ollama", ws, env={}) == ""
    assert credentials.resolve("anthropic", ws, env={}) is None
    credentials.set_base_url("custom", "https://llm.example.com/v1/", ws)
    assert credentials.get_base_url("custom", ws) == "https://llm.example.com/v1"
    with pytest.raises(credentials.CredentialError, match="^invalid_url$"):
        credentials.set_base_url("custom", "ftp://x", ws)
    assert credentials.remove("openai", ws) and not credentials.remove("openai", ws)
    assert credentials.get("openai", ws, env={}) is None
    assert not (ws / "credentials.json.tmp").exists()


@posix
def test_credentials_file_is_private(tmp_path: Path) -> None:
    credentials.set_key("openai", KEY, tmp_path)
    mode = stat.S_IMODE(os.stat(tmp_path / "credentials.json").st_mode)
    assert mode == 0o600


def test_unreadable_credentials_file_is_empty(tmp_path: Path) -> None:
    (tmp_path / "credentials.json").write_text("{not json", encoding="utf-8")
    assert credentials.get("openai", tmp_path, env={}) is None
    credentials.set_key("openai", KEY, tmp_path)  # rewritten cleanly
    assert credentials.get("openai", tmp_path, env={}) == KEY


# -- pricing ----------------------------------------------------------------------------


def test_unpriced_policy_and_price_helpers(tmp_path: Path) -> None:
    assert is_unpriced("no-such-model") and not is_unpriced("gpt-4o-mini")
    assert not is_unpriced("claude-haiku-4-5-20251001")
    placeholder = unpriced_price(allowed_response_models=("x:latest",))
    assert placeholder.unpriced and placeholder.output_capacity_tokens == DEFAULT_OUTPUT_CAPACITY
    assert placeholder.cost(1000, 1000) == 0.0
    with pytest.raises(ValueError):
        Price(input_per_million=1, output_per_million=1, output_capacity_tokens=10,
              input_overhead_tokens=0, unpriced=True)
    table = write_price_entries(tmp_path / PRICES_FILE, {"mystery": placeholder,
                                                          "local": local_price()})
    write_price_entries(table, {"router/model": per_token_price(3e-6, 1.5e-5)})  # type: ignore[dict-item]
    loaded = load_prices(table).models
    assert set(loaded) == {"mystery", "local", "router/model"}
    assert is_unpriced("mystery", table) and not is_unpriced("local", table)
    assert loaded["router/model"].input_per_million == pytest.approx(3.0)
    assert loaded["router/model"].output_per_million == pytest.approx(15.0)
    assert per_token_price(-1, 0) is None and per_token_price(float("nan"), 0) is None
    assert is_unpriced("anything", tmp_path / "missing.json")  # unreadable: unpriced
    with pytest.raises(AdapterError):
        write_price_entries(Path(load_prices.__code__.co_filename).with_name("prices.json"),
                            {"x": placeholder})
    gpt = load_prices().models["gpt-4o-mini"]
    assert gpt.accepts_response_model("gpt-4o-mini", "gpt-4o-mini")
    assert gpt.accepts_response_model("gpt-4o-mini-2024-07-18", "gpt-4o-mini")
    assert gpt.accepts_response_model("gpt-4o-mini-2031-01-02", "gpt-4o-mini")  # any snapshot
    assert not gpt.accepts_response_model("gpt-4o", "gpt-4o-mini")
    assert not gpt.accepts_response_model("gpt-4o-mini-2024-7-18", "gpt-4o-mini")


async def test_unpriced_model_runs_metered_at_zero_dollars_under_a_time_budget(
    tmp_path: Path,
) -> None:
    """The existing quote/reserve/reconcile runtime runs an unpriced adapter unchanged."""
    from architect.agent.tools import control_spec

    table = write_price_entries(tmp_path / PRICES_FILE, {"mystery": unpriced_price()})

    def reply(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "model": "mystery", "choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 3}})

    adapter = OpenAICompatibleAdapter("mystery", api_key="k", base_url="http://h/v1",
                                      prices_path=table, transport=httpx.MockTransport(reply))
    assert adapter.quote(ModelRequest(prompt="hi")).cost_usd == 0.0
    profile_path = tmp_path / "p.json"
    profile_path.write_text(json.dumps({"workspace": ".", "models": {
        "fake": {"provider": "fake"}}}), encoding="utf-8")
    services = build_services(load_profile(profile_path), Path("."))
    try:
        spec = control_spec("chat", "mystery", output_schema={"type": "string"},
                            cost_usd=0.01, compute_s=30)

        async def one(context: Any, task: Any) -> Any:
            return await context.call("hi", 16)

        result = await services.runtime.evaluate(spec, one, None, adapter, preset="conductor",
                                                 components=("conductor",))
    finally:
        services.close()
    assert result.output == "hello" and result.cost_usd == 0.0


# -- discovery --------------------------------------------------------------------------


async def test_discovery_parses_every_listing_shape(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    routes = {
        "https://api.openai.com/v1/models": OPENAI_MODELS,
        "https://generativelanguage.googleapis.com/v1beta/openai/models": {
            "data": [{"id": "models/gemini-2.5-flash"}]},
        "https://api.anthropic.com/v1/models": {"data": [{"id": "claude-sonnet-5"}]},
        "http://localhost:11434/api/tags": {"models": [{"name": "gemma4:26b"},
                                                       {"name": "llama3.2:latest"}]},
        "https://openrouter.ai/api/v1/models": {"data": [
            {"id": "anthropic/claude-sonnet-4.5", "context_length": 200000,
             "pricing": {"prompt": "0.000003", "completion": "0.000015"},
             "top_provider": {"max_completion_tokens": 64000}},
            {"id": "openrouter/auto", "pricing": {"prompt": "-1", "completion": "-1"}},
            {"id": 7}, "junk"]},
    }
    h = host(tmp_path, env={"OPENAI_API_KEY": KEY, "GEMINI_API_KEY": "g",
                            "ANTHROPIC_API_KEY": "a"}, routes=routes, seen=seen)
    openai = await list_models(entry("openai"), workspace=tmp_path, host=h)
    assert openai.ok and [m.id for m in openai.models] == ["brand-new-model", "gpt-4.1",
                                                            "gpt-4o-mini"]
    assert seen[-1].headers["Authorization"] == f"Bearer {KEY}"
    gemini = await list_models(entry("gemini"), workspace=tmp_path, host=h)
    assert [m.id for m in gemini.models] == ["gemini-2.5-flash"]
    anthropic = await list_models(entry("anthropic"), workspace=tmp_path, host=h)
    assert [m.id for m in anthropic.models] == ["claude-sonnet-5"]
    assert seen[-1].headers["x-api-key"] == "a" and "anthropic-version" in seen[-1].headers
    ollama = await list_models(entry("ollama"), workspace=tmp_path, host=h)
    assert [m.id for m in ollama.models] == ["gemma4:26b", "llama3.2:latest"]
    assert "Authorization" not in seen[-1].headers
    router = await list_models(entry("openrouter"), workspace=tmp_path, host=h)
    by_id = {m.id: m for m in router.models}
    assert set(by_id) == {"anthropic/claude-sonnet-4.5", "openrouter/auto"}
    price = by_id["anthropic/claude-sonnet-4.5"].price
    assert price is not None and price.input_per_million == pytest.approx(3.0)
    assert price.output_capacity_tokens == 64000 and by_id["openrouter/auto"].price is None
    fixed = await list_models(entry("claude"), host=h)
    assert [m.id for m in fixed.models] == list(CLAUDE_MODELS)


async def test_nebius_lists_priced_text_models_with_the_key(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    h = host(tmp_path, env={"NEBIUS_API_KEY": KEY}, seen=seen, routes={
        "https://api.tokenfactory.nebius.com/v1/models?verbose=true": NEBIUS_MODELS})
    found = await list_models(entry("nebius"), workspace=tmp_path, host=h)
    assert found.ok and found.base_url == "https://api.tokenfactory.nebius.com/v1"
    assert str(seen[-1].url) == "https://api.tokenfactory.nebius.com/v1/models?verbose=true"
    assert seen[-1].headers["Authorization"] == f"Bearer {KEY}"
    by_id = {m.id: m for m in found.models}
    # Embeddings are not chat models; image-reading chat models and unlabelled rows stay.
    assert set(by_id) == {SUPER, ULTRA, "moonshotai/Kimi-K3", "house/no-architecture"}
    price = by_id[SUPER].price
    assert price is not None and (price.input_per_million, price.output_per_million) == (0.3, 0.9)
    assert price.allowed_response_models == (SUPER,) and by_id[SUPER].context == 262144
    assert price.output_capacity_tokens == DEFAULT_OUTPUT_CAPACITY  # Nebius lists no limit
    ultra = by_id[ULTRA].price
    assert ultra is not None and (ultra.input_per_million, ultra.output_per_million) == (1, 3)
    assert by_id["house/no-architecture"].price is None  # an unreadable rate is not guessed
    assert price_label(entry("nebius"), by_id[SUPER]) == "$0.3/$0.9 per M"
    # No key: nothing is sent (the list is not public).
    keyless = await list_models(entry("nebius"), workspace=tmp_path / "empty",
                                host=host(tmp_path, seen=seen))
    assert keyless.code == "no_key" and len(seen) == 1


async def test_discovery_failures_are_codes_never_exceptions(tmp_path: Path) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    routes: dict[str, Any] = {
        "https://api.openai.com": 401,
        "https://api.groq.com": 500,
        "https://api.x.ai": lambda request: httpx.Response(200, content=b"not json"),
        "https://api.mistral.ai": {"unexpected": True},
        "https://api.deepseek.com": slow,
        "https://api.tokenfactory.nebius.com": 403,
    }
    env = {"OPENAI_API_KEY": KEY, "GROQ_API_KEY": "k", "XAI_API_KEY": "k",
           "MISTRAL_API_KEY": "k", "DEEPSEEK_API_KEY": "k", "NEBIUS_API_KEY": "k"}
    h = host(tmp_path, env=env, routes=routes)
    codes = {}
    for provider in ("openai", "groq", "xai", "mistral", "deepseek", "lmstudio", "together",
                     "nebius"):
        found = await list_models(entry(provider), workspace=tmp_path, host=h)
        codes[provider] = found.code
        assert not found.ok and found.models == ()
    assert codes == {"openai": "unauthorized", "groq": "http_error", "xai": "invalid_response",
                     "mistral": "invalid_response", "deepseek": "timeout",
                     "lmstudio": "unreachable", "together": "no_key", "nebius": "unauthorized"}
    for body in ({"data": "not a list"}, ["rows"]):
        broken = host(tmp_path, env=env, routes={"https://api.tokenfactory.nebius.com": body})
        found = await list_models(entry("nebius"), workspace=tmp_path, host=broken)
        assert found.code == "invalid_response"
    custom = await list_models(entry("custom"), workspace=tmp_path, host=h)
    assert custom.code == "no_url"


# -- status -----------------------------------------------------------------------------


async def test_status_uses_cheap_checks_only(tmp_path: Path) -> None:
    calls: list[str] = []

    def which(name: str) -> str | None:
        calls.append(name)
        return {"claude": "/bin/claude", "codex": "/bin/codex"}.get(name)

    h = host(tmp_path, env={"OPENROUTER_API_KEY": "r", "NEBIUS_API_KEY": "n"},
             routes={"http://localhost:11434/api/tags": {"models": [{"name": "qwen3:8b"}]}})
    h = Host(which=which, env=h.env, home=h.home, transport=h.transport, runner=h.runner)
    credentials.set_key("groq", "g", tmp_path)
    statuses = {s.provider: s for s in await all_statuses(tmp_path, host=h)}
    assert statuses["claude"].state == "cli_found" and statuses["claude"].ready
    assert statuses["chatgpt"].state == "cli_found" and not statuses["chatgpt"].ready
    assert statuses["openrouter"].state == "key_env" and "OPENROUTER_API_KEY" in (
        statuses["openrouter"].detail)
    # A key is enough: no model list is fetched for a status (Nebius's would be routed).
    assert statuses["nebius"].state == "key_env" and statuses["nebius"].ready
    assert "NEBIUS_API_KEY" in statuses["nebius"].detail
    assert statuses["groq"].state == "key_stored" and statuses["openai"].state == "no_key"
    assert statuses["ollama"].state == "reachable" and statuses["ollama"].models == ("qwen3:8b",)
    assert statuses["lmstudio"].state == "unreachable" and not statuses["lmstudio"].ready
    assert statuses["custom"].state == "not_configured"
    # Login files on disk mark the CLIs signed in.
    (h.home / ".codex").mkdir()
    (h.home / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
    (h.home / ".claude").mkdir()
    (h.home / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")
    assert (await provider_status(entry("chatgpt"), tmp_path, host=h)).state == "signed_in"
    assert (await provider_status(entry("claude"), tmp_path, host=h)).state == "signed_in"
    skipped = await provider_status(entry("ollama"), tmp_path, host=h, probe=False)
    assert skipped.state == "not_checked"
    missing = await provider_status(entry("claude"), tmp_path,
                                    host=Host(which=lambda _: None, home=h.home))
    assert missing.state == "cli_missing" and not missing.ready


async def test_onboarding_choices_recommend_a_ready_cloud_provider_never_local(
    tmp_path: Path,
) -> None:
    ollama = {"http://localhost:11434/api/tags": {"models": [{"name": "qwen3:8b"}]}}
    nothing = await onboarding_choices(tmp_path, host=host(tmp_path, routes=ollama))
    assert recommended(nothing) is None  # only a local server: the user picks explicitly
    assert nothing[0].provider.id == "ollama" and nothing[0].model == "qwen3:8b"
    assert [c.index for c in nothing] == list(range(1, len(CATALOG) + 1))
    keyed = await onboarding_choices(tmp_path, host=host(
        tmp_path, env={"GEMINI_API_KEY": "g", "OPENAI_API_KEY": "o"}, routes=ollama))
    best = recommended(keyed)
    assert best is not None and best.provider.id == "openai" and best.index == 1
    assert [c.provider.id for c in keyed[:3]] == ["openai", "gemini", "ollama"]
    assert best.label == "OpenAI API" and best.available and "OPENAI_API_KEY" in best.detail
    with_cli = await onboarding_choices(tmp_path, host=host(
        tmp_path, which={"claude": "/bin/claude"}, env={"OPENAI_API_KEY": "o"}), probe_local=False)
    assert recommended(with_cli).provider.id == "claude"  # type: ignore[union-attr]
    # A Nebius key wins the recommendation; its suggested model is Nemotron 3 Super.
    nebius = await onboarding_choices(tmp_path, host=host(
        tmp_path, which={"claude": "/bin/claude"},
        env={"OPENAI_API_KEY": "o", "NEBIUS_API_KEY": "n"}), probe_local=False)
    best = recommended(nebius)
    assert best is not None and best.provider.id == "nebius" and best.index == 1
    assert best.model == SUPER
    assert [c.provider.id for c in nebius[:3]] == ["nebius", "claude", "openai"]


# -- register_model and the factory -----------------------------------------------------


def test_register_bundled_api_model_uses_a_credential_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)  # the stored key is what resolves
    ws = tmp_path / "ws"
    path = ws / "session-profile.json"
    credentials.set_key("openai", KEY, ws)
    h = host(tmp_path)
    assert register_model(path, "openai", "gpt-4o-mini", host=h) == "gpt-4o-mini"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["workspace"] == "."
    assert payload["models"]["gpt-4o-mini"] == {
        "provider": "openai", "base_url": "https://api.openai.com/v1", "api_key_ref": "openai"}
    assert payload["compile"]["model_ladder"] == ["gpt-4o-mini"]
    assert payload["inference"]["model"] == "gpt-4o-mini"  # fits the Stage 0 budget
    assert KEY not in path.read_text(encoding="utf-8")
    profile = load_profile(path)
    adapter = profiles._adapter(profile, "gpt-4o-mini", profile.models["gpt-4o-mini"])
    assert isinstance(adapter, OpenAICompatibleAdapter)
    assert adapter._api_key == KEY and adapter._base_url == "https://api.openai.com/v1"
    assert not model_unpriced(profile, "gpt-4o-mini")
    assert provider_of(profile.models["gpt-4o-mini"]) == "openai"
    # Without a stored key or environment variable the profile fails closed.
    credentials.remove("openai", ws)
    with pytest.raises(ProfileError, match="^missing_api_key$"):
        profiles._adapter(profile, "gpt-4o-mini", profile.models["gpt-4o-mini"])


def test_register_unknown_api_model_is_unpriced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                ) -> None:
    ws = tmp_path / "ws"
    path = ws / "session-profile.json"
    monkeypatch.setenv("GROQ_API_KEY", "gk")
    register_model(path, "groq", "llama-3.3-70b-versatile", host=host(tmp_path))
    payload = json.loads(path.read_text(encoding="utf-8"))
    config = payload["models"]["llama-3.3-70b-versatile"]
    assert config["base_url"] == "https://api.groq.com/openai/v1"
    assert config["prices"] == PRICES_FILE and "inference" not in payload
    profile = load_profile(path)
    assert model_unpriced(profile, "llama-3.3-70b-versatile")
    assert provider_of(profile.models["llama-3.3-70b-versatile"]) == "groq"
    adapter = profiles._adapter(profile, "llama-3.3-70b-versatile",
                                profile.models["llama-3.3-70b-versatile"])
    assert adapter.quote(ModelRequest(prompt="hello")).cost_usd == 0.0
    assert price_label(entry("groq"), "llama-3.3-70b-versatile") == UNPRICED_LABEL


def test_register_local_model_is_zero_dollars_and_keyless(tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("OLLAMA_API_KEY", "must-not-leak")
    ws = tmp_path / "ws"
    path = ws / "session-profile.json"
    register_model(path, "ollama", "qwen3", host=host(tmp_path))
    register_model(path, "lmstudio", "qwen/qwen3-8b", host=host(tmp_path))
    profile = load_profile(path)
    ollama = profiles._adapter(profile, "qwen3", profile.models["qwen3"])
    assert isinstance(ollama, OllamaAdapter) and ollama._api_key == ""
    lm = profiles._adapter(profile, "qwen/qwen3-8b", profile.models["qwen/qwen3-8b"])
    assert isinstance(lm, OpenAICompatibleAdapter) and lm._api_key == ""
    assert lm._base_url == "http://localhost:1234/v1"
    assert not model_unpriced(profile, "qwen3") and not model_unpriced(profile, "qwen/qwen3-8b")
    table = load_prices(ws / PRICES_FILE).models
    assert table["qwen3"].allowed_response_models == ("qwen3:latest",)
    assert table["qwen3"].input_per_million == 0 and not table["qwen3"].unpriced
    assert lm.quote(ModelRequest(prompt="x" * 100)).cost_usd == 0.0
    # The first registration made the profile; the second only added a model.
    assert profile.compile.model_ladder == ("qwen3",)
    # Pi never reaches the generated price table (it could zero a budget).
    assert ws.resolve() / PRICES_FILE in pi_denied_paths(profile, profile.workspace)


def test_register_openrouter_model_writes_its_list_price(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    path = ws / "session-profile.json"
    credentials.set_key("openrouter", KEY, ws)
    price = per_token_price(3e-6, 1.5e-5, allowed_response_models=("anthropic/claude-sonnet-4.5",))
    register_model(path, "openrouter", "anthropic/claude-sonnet-4.5", price=price,
                   host=host(tmp_path))
    profile = load_profile(path)
    assert not model_unpriced(profile, "anthropic/claude-sonnet-4.5")
    entry_ = load_prices(ws / PRICES_FILE).models["anthropic/claude-sonnet-4.5"]
    assert entry_.output_per_million == pytest.approx(15.0)
    assert price_label(entry("openrouter"), Discovered("x", price)) == "$3/$15 per M"


def test_register_nebius_nemotron_writes_the_role_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)  # the stored key is what resolves
    ws = tmp_path / "ws"
    path = ws / "session-profile.json"
    credentials.set_key("nebius", KEY, ws)
    # A listed price for a bundled model (8,192 output tokens) never replaces the bundled one.
    listed = per_token_price(3e-7, 9e-7, allowed_response_models=(SUPER,))
    assert register_model(path, "nebius", SUPER, price=listed, host=host(tmp_path)) == SUPER
    payload = json.loads(path.read_text(encoding="utf-8"))
    endpoint = {"provider": "openai", "base_url": "https://api.tokenfactory.nebius.com/v1",
                "api_key_ref": "nebius"}
    assert payload["models"] == {model: endpoint for model in NEMOTRON_MODELS}
    assert payload["compile"]["model_ladder"] == [LIGHTNING, SUPER]
    # Builds there ship the tuned preset; a Forge the user chooses fits a judge code's day.
    assert payload["compile"]["forge"] == NEBIUS_FORGE == {"max_usd": 1.2, "max_s": 2700}
    # The plan card's "build up to" is $1.50 and 30 minutes, not the $5 of other profiles:
    # with Forge on top it is $2.70, inside the $3 a judge code allows in a day.
    assert payload["compile"]["compile_budget"] == NEBIUS_COMPILE_BUDGET == {
        "cost_usd": 1.5, "compute_s": 1800
    }
    assert NEBIUS_COMPILE_BUDGET["cost_usd"] + NEBIUS_FORGE["max_usd"] <= 3
    assert payload["inference"] == {**endpoint, "model": SUPER}  # thinking off: the default
    # Super engineers the harness too, as in the last Forge round: thinking off, temperature 0.
    assert payload["forge_agent"] == {**endpoint, "model": SUPER, "think": False,
                                      "temperature": 0.0, "timeout_s": 300}
    assert NEBIUS_ROLES["model_ladder"] == [LIGHTNING, SUPER]
    assert not (ws / PRICES_FILE).exists()  # every pack model is bundled
    # The chosen model holds the conversation; the ladder starts with Lightning.
    assert (ws / "config.toml").read_text(encoding="utf-8") == f'model = "{SUPER}"\n'
    assert KEY not in path.read_text(encoding="utf-8")
    profile = load_profile(path)
    for model in NEMOTRON_MODELS:
        assert not model_unpriced(profile, model) and provider_of(profile.models[model]) == "nebius"
        adapter = profiles._adapter(profile, model, profile.models[model])
        assert isinstance(adapter, OpenAICompatibleAdapter)
        assert (adapter.dialect, adapter.think, adapter._timeout) == ("nebius", None, 120.0)
        assert adapter._api_key == KEY
    assert profile.forge_agent is not None and profile.inference is not None
    cap = forge_settings_of(profile.compile)
    assert (cap.enabled, cap.max_usd, cap.max_s) == (False, 1.2, 2700.0)
    budget = profile.compile.compile_budget
    assert (budget.cost_usd, budget.compute_s) == (1.5, 1800.0)
    forge = profiles._adapter(profile, SUPER, profile.forge_agent)
    assert isinstance(forge, OpenAICompatibleAdapter)
    assert (forge.dialect, forge.think, forge.temperature, forge.top_p, forge._timeout) == (
        "nebius", False, 0.0, None, 300.0)
    assert forge.quote(ModelRequest(prompt="x")).tokens_out == 16384
    services = build_services(profile, profile.workspace)
    try:
        assert services.forge_adapter is not None and services.forge_adapter.model == SUPER
        assert services.compiler.inference_adapter is not None
        assert services.compiler.inference_adapter.model == SUPER
    finally:
        services.close()
    # Adding a Nemotron model to an existing profile changes no roles and no config.
    (ws / "config.toml").write_text('model = "x"\n', encoding="utf-8")
    register_model(path, "nebius", ULTRA, host=host(tmp_path))
    assert json.loads(path.read_text(encoding="utf-8")) == payload
    assert (ws / "config.toml").read_text(encoding="utf-8") == 'model = "x"\n'


def test_the_nebius_example_profile_is_the_role_pack(tmp_path: Path) -> None:
    example = Path(__file__).resolve().parents[1] / "examples" / "nebius" / "profile.json"
    load_profile(example)  # a valid profile (adapters are built only when it is used)
    ws = tmp_path / "ws"
    credentials.set_key("nebius", KEY, ws)
    register_model(ws / "p.json", "nebius", SUPER, host=host(tmp_path))
    written = json.loads((ws / "p.json").read_text(encoding="utf-8"))
    shown = json.loads(example.read_text(encoding="utf-8"))
    assert shown.pop("workspace") == "../../.architect/nebius" and written.pop("workspace") == "."
    assert shown == written


EXPENSE_APP = Path(__file__).resolve().parents[1] / "examples" / "expense-app" / "architect.json"
RELAY = "http://127.0.0.1:9/v1"  # a judge code's relay; nothing is called


def test_signing_in_again_keeps_an_entrys_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/login nebius or picking a model in examples/expense-app rewrote the model's entry
    and dropped its temperature 0 and max_tokens 512. Sign-in now changes only what it owns,
    and a profile it would not change is not written at all."""
    from architect.profiles import in_workspace

    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    path = tmp_path / "app" / "architect.json"
    path.parent.mkdir()
    path.write_bytes(EXPENSE_APP.read_bytes())
    original = path.read_bytes()
    ws = tmp_path / "ws"
    code = "rc_" + "a" * 32
    credentials.set_key("nebius", code, ws)  # what a judge code's sign-in stores
    credentials.set_base_url("nebius", RELAY, ws)
    listed = per_token_price(3e-7, 9e-7, allowed_response_models=(SUPER,))
    for model in (SUPER, LIGHTNING, ULTRA):
        assert register_model(path, "nebius", model, workspace=ws, base_url=RELAY, price=listed,
                              host=host(tmp_path)) == model
    assert path.read_bytes() == original  # nothing sign-in owns changed
    profile = in_workspace(load_profile(path), ws)
    for model in (SUPER, LIGHTNING, ULTRA):
        adapter = profiles._adapter(profile, model, profile.models[model])
        assert isinstance(adapter, OpenAICompatibleAdapter)
        assert (adapter._base_url, adapter._api_key) == (RELAY, code)
        assert (adapter.temperature, adapter._max_tokens) == (0.0, 512)
    # A sign-in at another address writes only that address into the entry.
    other = "http://127.0.0.1:8/v1"
    register_model(path, "nebius", SUPER, workspace=ws, base_url=other, host=host(tmp_path))
    written = json.loads(path.read_text(encoding="utf-8"))
    shown = json.loads(original)
    assert written["models"][SUPER] == {**shown["models"][SUPER], "base_url": other}
    assert list(written["models"][SUPER]) == [*shown["models"][SUPER], "base_url"]
    del written["models"][SUPER]["base_url"]
    assert written == shown


def test_another_adapter_or_provider_replaces_what_sign_in_owns(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    path = ws / "p.json"
    credentials.set_key("openrouter", KEY, ws)
    credentials.set_key("nebius", KEY, ws)
    path.write_text(json.dumps({"workspace": ".", "models": {
        SUPER: {"provider": "openai", "base_url": "https://openrouter.ai/api/v1",
                "api_key_ref": "openrouter", "temperature": 0.2, "max_tokens": 900},
        "qwen3": {"provider": "ollama", "num_ctx": 8192, "think": True},
    }}), encoding="utf-8")
    register_model(path, "nebius", SUPER, host=host(tmp_path))
    register_model(path, "lmstudio", "qwen3", host=host(tmp_path))
    models = json.loads(path.read_text(encoding="utf-8"))["models"]
    # The same adapter at another provider: its settings stay, its address follows the key.
    assert models[SUPER] == {"provider": "openai", "base_url": default_base_url(entry("nebius")),
                             "api_key_ref": "nebius", "temperature": 0.2, "max_tokens": 900}
    # Another adapter: Ollama's settings do not apply to it, so the entry is replaced.
    assert models["qwen3"] == {"provider": "openai", "base_url": "http://localhost:1234/v1",
                               "api_key_ref": "lmstudio", "prices": PRICES_FILE}


def test_register_other_nebius_model_uses_its_listed_price(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    path = ws / "session-profile.json"
    credentials.set_key("nebius", KEY, ws)
    listed = per_token_price(1.4e-6, 4.4e-6, allowed_response_models=("zai-org/GLM-5.3",))
    register_model(path, "nebius", "zai-org/GLM-5.3", price=listed, host=host(tmp_path))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload["models"]) == {"zai-org/GLM-5.3"}  # no role pack
    assert payload["compile"]["model_ladder"] == ["zai-org/GLM-5.3"]
    assert "forge_agent" not in payload and not (ws / "config.toml").exists()
    profile = load_profile(path)
    assert not model_unpriced(profile, "zai-org/GLM-5.3")
    table = load_prices(ws / PRICES_FILE).models["zai-org/GLM-5.3"]
    assert (table.input_per_million, table.output_per_million) == (1.4, 4.4)


def test_only_nebius_is_priced_from_the_bundled_nemotron_entries(tmp_path: Path) -> None:
    """They are Nebius's list; OpenRouter lists Super under the same id at its own price
    and output limit (checked 2026-10-03)."""
    ws = tmp_path / "ws"
    path = ws / "session-profile.json"
    credentials.set_key("openrouter", KEY, ws)
    listed = per_token_price(8e-8, 4.5e-7, output_capacity_tokens=235929,
                             allowed_response_models=(SUPER,))
    register_model(path, "openrouter", SUPER, price=listed, host=host(tmp_path))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["models"][SUPER]["prices"] == PRICES_FILE and "forge_agent" not in payload
    table = load_prices(ws / PRICES_FILE).models[SUPER]
    assert (table.input_per_million, table.output_per_million) == (0.08, 0.45)
    profile = load_profile(path)
    adapter = profiles._adapter(profile, SUPER, profile.models[SUPER])
    assert isinstance(adapter, OpenAICompatibleAdapter) and adapter.dialect == "openai"
    assert adapter.quote(ModelRequest(prompt="x")).tokens_out == 235929  # not Nebius's cap
    # Without its list (it failed) the model is unpriced, never billed at Nebius's rates.
    register_model(path, "openrouter", SUPER, host=host(tmp_path))
    assert model_unpriced(load_profile(path), SUPER)
    assert price_label(entry("openrouter"), SUPER) == UNPRICED_LABEL
    assert price_label(entry("nebius"), SUPER) == "$0.3/$0.9 per M"


def test_a_declared_price_wins_over_the_bundled_entry(tmp_path: Path) -> None:
    """``/model add <provider>:<model> --price`` is the user's word, on Nebius too; only
    Nebius's listed price yields to its bundled entry."""
    ws = tmp_path / "ws"
    path = ws / "session-profile.json"
    for provider in ("openai", "nebius"):
        credentials.set_key(provider, KEY, ws)
    declared = Price(input_per_million=0.1, output_per_million=0.5,
                     output_capacity_tokens=DEFAULT_OUTPUT_CAPACITY, input_overhead_tokens=1024)
    for provider, model in (("nebius", SUPER), ("openai", "gpt-4o-mini")):
        register_model(path, provider, model, price=declared, declared=True,
                       host=host(tmp_path))
        config = json.loads(path.read_text(encoding="utf-8"))["models"][model]
        assert config["prices"] == PRICES_FILE
        table = load_prices(ws / PRICES_FILE).models[model]
        assert (table.input_per_million, table.output_per_million) == (0.1, 0.5)
    # The role pack's other entries stay on the bundled table (the price file has only
    # the declared models).
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert all("prices" not in payload[role] for role in ("inference", "forge_agent"))
    profile = load_profile(path)
    for model in NEMOTRON_MODELS:
        assert not model_unpriced(profile, model)
        assert ("prices" in payload["models"][model]) is (model == SUPER)
    listed = per_token_price(3e-7, 9e-7, allowed_response_models=(SUPER,))
    register_model(path, "nebius", SUPER, price=listed, host=host(tmp_path))
    assert "prices" not in json.loads(path.read_text(encoding="utf-8"))["models"][SUPER]
    assert load_profile(path).models[SUPER].prices is None  # the bundled entry again


def test_register_cli_model_and_errors(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    path = ws / "session-profile.json"
    h = host(tmp_path, which={"claude": str(tmp_path / "claude.exe")})
    register_model(path, "claude", "claude-sonnet-5", host=h)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload["models"]) == set(CLAUDE_MODELS)  # every model the CLI offers
    assert payload["models"]["claude-sonnet-5"]["provider"] == "claude-code"
    assert payload["inference"]["model"] == "claude-haiku-4-5-20251001"  # the fallback
    assert payload["compile"]["model_ladder"] == ["claude-sonnet-5"]
    before = path.read_text(encoding="utf-8")
    for args, code in (
        (("nope", "x"), "unknown_provider"),
        (("claude", "gpt-9"), "model_not_offered"),
        (("chatgpt", "gpt-5.6-terra"), "cli_missing"),
        (("openai", "bad model"), "invalid_model"),
        (("custom", "m"), "no_url"),
    ):
        with pytest.raises(ProviderError) as caught:
            register_model(path, *args, host=h)
        assert caught.value.code == code
    assert path.read_text(encoding="utf-8") == before
    with pytest.raises(ProviderError, match="^profile_not_json$"):
        register_model(tmp_path / "p.toml", "openai", "gpt-5", host=h)
    # A stored custom URL is used for custom registrations.
    credentials.set_base_url("custom", "https://llm.example.com/v1", ws)
    register_model(path, "custom", "house-model", host=h)
    profile = load_profile(path)
    assert profile.models["house-model"].base_url == "https://llm.example.com/v1"
    assert provider_of(profile.models["house-model"]) == "custom"
    assert model_unpriced(profile, "house-model")
    assert provider_of(profile.models["claude-sonnet-5"]) == "claude"


@pytest.mark.parametrize("config", [
    {"provider": "fake", "api_key_ref": "openai"},
    {"provider": "codex", "api_key_ref": "openai"},
    {"provider": "openai", "api_key_ref": "openai", "api_key_env": "OPENAI_API_KEY"},
    {"provider": "openai", "api_key_ref": "Bad Name"},
])
def test_api_key_ref_validation(tmp_path: Path, config: dict[str, object]) -> None:
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"models": {"m": config}}), encoding="utf-8")
    with pytest.raises(ProfileError, match="^invalid_profile$"):
        load_profile(path)


def test_existing_profiles_keep_their_behaviour(tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """No api_key_ref: the Phase 12 rules, including the OPENAI_API_KEY default."""
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"models": {"gpt-4o-mini": {"provider": "openai"}}}),
                    encoding="utf-8")
    profile = load_profile(path)
    adapter = profiles._adapter(profile, "gpt-4o-mini", profile.models["gpt-4o-mini"])
    assert isinstance(adapter, OpenAICompatibleAdapter) and adapter._api_key == "env-key"
    assert pi_denied_paths(profile, tmp_path) == pi_denied_paths(profile, tmp_path)
    assert tmp_path / "credentials.json" not in pi_denied_paths(profile, tmp_path)
    credentials.set_key("openai", KEY, tmp_path / ".architect")
    assert (tmp_path / ".architect" / "credentials.json").resolve() in pi_denied_paths(
        profile, profile.workspace)
    assert model_unpriced(profile, "not-configured")
    assert not model_unpriced(profile, "gpt-4o-mini")


# -- sign-in flows ----------------------------------------------------------------------


async def test_api_key_sign_in_checks_then_stores_and_never_echoes(tmp_path: Path) -> None:
    rejected = Recorder("sk-bad-key")
    h = host(tmp_path, routes={"https://api.openai.com/v1/models": 401})
    result = await sign_in(entry("openai"), tmp_path, rejected.prompter(), host=h)
    assert not result.ok and result.code == "unauthorized"
    assert credentials.get("openai", tmp_path, env={}) is None
    assert "sk-bad-key" not in rejected.text()
    good = Recorder(KEY)
    h = host(tmp_path, routes={"https://api.openai.com/v1/models": OPENAI_MODELS})
    result = await sign_in(entry("openai"), tmp_path, good.prompter(), host=h)
    assert result.ok and len(result.models) == 3
    assert credentials.get("openai", tmp_path, env={}) == KEY
    assert KEY not in good.text()
    assert "what you type is shown here" in good.asked[0] and "never logged" in good.asked[0]
    # Already stored: keep it unless asked to replace.
    keep = Recorder("")
    assert (await sign_in(entry("openai"), tmp_path, keep.prompter(), host=h)).ok
    assert "Replace it?" in keep.asked[0]
    # A key in the environment is used as is, with no prompt.
    env_only = Recorder()
    h = host(tmp_path, env={"GEMINI_API_KEY": "g"},
             routes={"https://generativelanguage.googleapis.com": {"data": []}})
    assert (await sign_in(entry("gemini"), tmp_path, env_only.prompter(), host=h)).ok
    assert env_only.asked == [] and "GEMINI_API_KEY" in env_only.text()
    cancelled = Recorder("")
    result = await sign_in(entry("mistral"), tmp_path, cancelled.prompter(), host=host(tmp_path))
    assert not result.ok and result.code == "cancelled"


async def test_chatgpt_sign_in_runs_the_official_codex_login(tmp_path: Path) -> None:
    ran: list[list[str]] = []
    home = tmp_path / "home"

    def runner(argv: Sequence[str]) -> int:
        ran.append(list(argv))
        (home / ".codex").mkdir(parents=True, exist_ok=True)
        (home / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
        return 0

    said = Recorder()
    h = host(tmp_path, which={"codex": "/opt/codex"}, runner=runner)
    result = await sign_in(entry("chatgpt"), tmp_path, said.prompter(), host=h)
    assert result.ok and ran == [["/opt/codex", "login"]]
    assert [m.id for m in result.models] == list(CODEX_MODELS)
    assert "codex login" in said.text()
    again = Recorder("n")
    assert (await sign_in(entry("chatgpt"), tmp_path, again.prompter(), host=h)).ok
    assert len(ran) == 1  # already signed in; not asked to run again
    failing = host(tmp_path / "other", which={"codex": "/opt/codex"}, runner=lambda argv: 1)
    result = await sign_in(entry("chatgpt"), tmp_path, Recorder().prompter(), host=failing)
    assert not result.ok and result.code == "login_failed"
    missing = await sign_in(entry("chatgpt"), tmp_path, Recorder().prompter(),
                            host=host(tmp_path))
    assert missing.code == "cli_missing"
    claude = Recorder()
    ok = await sign_in(entry("claude"), tmp_path, claude.prompter(),
                       host=host(tmp_path, which={"claude": "/bin/claude"}))
    assert ok.ok and "/login there" in claude.text()


async def test_local_and_custom_sign_in(tmp_path: Path) -> None:
    down = Recorder("")
    result = await sign_in(entry("ollama"), tmp_path, down.prompter(), host=host(tmp_path))
    assert not result.ok and result.code == "unreachable"
    assert "never starts a local server" in down.text()
    up = Recorder("http://127.0.0.1:9999/")
    h = host(tmp_path, routes={"http://127.0.0.1:9999/api/tags": {"models": [{"name": "m1"}]}})
    result = await sign_in(entry("ollama"), tmp_path, up.prompter(), host=h)
    assert result.ok and result.base_url == "http://127.0.0.1:9999"
    assert credentials.get_base_url("ollama", tmp_path) == "http://127.0.0.1:9999"
    seen: list[httpx.Request] = []
    custom = Recorder("not a url", "https://llm.example.com/v1", KEY)
    h = host(tmp_path, routes={"https://llm.example.com/v1/models": {"data": [{"id": "house"}]}},
             seen=seen)
    result = await sign_in(entry("custom"), tmp_path, custom.prompter(), host=h)
    assert result.ok and [m.id for m in result.models] == ["house"]
    assert seen[-1].headers["Authorization"] == f"Bearer {KEY}"
    assert credentials.get("custom", tmp_path, env={}) == KEY
    assert credentials.get_base_url("custom", tmp_path) == "https://llm.example.com/v1"
    assert KEY not in custom.text()


async def test_choose_model_numbers_names_and_filters() -> None:
    models = tuple(Discovered(name) for name in ("a/alpha", "a/beta", "b/beta-2", "c/gamma"))
    assert (await choose_model(entry("openrouter"), models, Recorder("2").prompter())
            ) == models[1]
    assert (await choose_model(entry("openrouter"), models, Recorder("gamma").prompter())
            ) == models[3]
    narrowed = Recorder("beta", "2")
    assert (await choose_model(entry("openrouter"), models, narrowed.prompter())) == models[2]
    assert (await choose_model(entry("openrouter"), models, Recorder("").prompter())) == models[0]
    assert (await choose_model(entry("openrouter"), models, Recorder("").prompter(),
                               allow_skip=True)) is None
    unknown = Recorder("zzz", None)
    assert (await choose_model(entry("openrouter"), models, unknown.prompter())) is None
    assert "No OpenRouter model matches 'zzz'." in unknown.text()
    # Nothing listed: the default, or a typed name, is accepted.
    assert (await choose_model(entry("openai"), (), Recorder("").prompter())
            ) == Discovered("gpt-4o-mini")
    assert (await choose_model(entry("openai"), (), Recorder("gpt-x").prompter())
            ) == Discovered("gpt-x")


async def test_onboard_writes_a_working_profile(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    routes = {"https://api.openai.com/v1/models": OPENAI_MODELS}
    flow = Recorder("openai", KEY, "brand-new-model")
    key = await onboard(flow.prompter(), ws, ws / "session-profile.json",
                        host=host(tmp_path, routes=routes))
    assert key == "brand-new-model"
    profile = load_profile(ws / "session-profile.json")
    assert model_unpriced(profile, "brand-new-model")
    assert "build mode will not compile on it" in flow.text() and KEY not in flow.text()
    with pytest.raises(ProviderError, match="^setup_cancelled$"):
        await onboard(Recorder(None).prompter(), ws, ws / "other.json", host=host(tmp_path))


async def test_onboard_nebius_suggests_super_and_explains_the_roles(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    routes = {"https://api.tokenfactory.nebius.com/v1/models?verbose=true": NEBIUS_MODELS}
    flow = Recorder("nebius", KEY, "")  # Enter takes the suggested model
    key = await onboard(flow.prompter(), ws, ws / "session-profile.json",
                        host=host(tmp_path, routes=routes), probe_local=False)
    assert key == SUPER
    text = flow.text()
    assert "create a key at https://tokenfactory.nebius.com/project/api-keys" in text
    assert "Nebius Token Factory: key works and is stored (4 models)." in text
    assert f"{SUPER}  ($0.3/$0.9 per M)" in text
    assert f"Nemotron roles: you talk to {SUPER}" in text and KEY not in text
    assert credentials.get("nebius", ws, env={}) == KEY
    profile = load_profile(ws / "session-profile.json")
    assert profile.compile.model_ladder == (LIGHTNING, SUPER)
    assert profile.forge_agent is not None and profile.forge_agent.model == SUPER
    assert profile.forge_agent.think is False and profile.forge_agent.temperature == 0.0
    assert ("Builds tune a built-in strategy (a preset) on the practice cases; if you choose "
            "Forge, Super writes a custom harness instead.") in text


async def test_first_run_entry_point_for_app(tmp_path: Path) -> None:
    from architect.agent.events import RecordingUI

    ws = tmp_path / "ws"
    ui = RecordingUI(answers=["claude", ""])
    path = await first_run(ui, ws, host=host(tmp_path, which={"claude": "/bin/claude"}),
                           probe_local=False)
    assert path == ws / "session-profile.json"
    profile = load_profile(path)
    assert profile.compile.model_ladder == ("claude-haiku-4-5-20251001",)
    assert any("Profile written" in getattr(event, "text", "") for event in ui.events)


def test_zero_rate_entries_accept_any_reported_model_name() -> None:
    """Local servers report tags or file names; a $0 entry's name cannot change the bill."""
    from architect.models.pricing import Price

    free = Price(input_per_million=0, output_per_million=0, output_capacity_tokens=4096,
                 input_overhead_tokens=0)
    assert free.accepts_response_model("llama3.2:latest", "llama3.2")
    paid = Price(input_per_million=1, output_per_million=2, output_capacity_tokens=4096,
                 input_overhead_tokens=0)
    assert not paid.accepts_response_model("llama3.2:latest", "llama3.2")
    assert paid.accepts_response_model("gpt-4.1-2025-04-14", "gpt-4.1")
