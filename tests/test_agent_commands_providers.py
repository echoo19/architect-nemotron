"""Phase 13 Package A: /login, /logout, /providers, /models and the /model picker.

Driven through the line front end with a scripted console and the fake session
profile; HTTP is an ``httpx.MockTransport``, PATH and the login runner are fakes
(``conductor.extras["providers_host"]``). No provider, CLI or local server is called.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from session_support import fake_profile
from test_agent_conductor import Scripted

from architect.agent.app import AppOptions, boot
from architect.agent.commands import COMMANDS
from architect.agent.commands_providers import PICKER_KEY
from architect.agent.repl import LineFrontend, Repl
from architect.providers import credentials
from architect.providers.host import Host
from architect.session import ScriptedConsole
from architect.session.bootstrap import SESSION_PROFILE

KEY = "sk-cmd-SECRET-9876543210"
OPENAI_MODELS = {"data": [{"id": "gpt-4o-mini"}, {"id": "gpt-4.1"}, {"id": "brand-new-model"}]}
ROUTER_MODELS = {"data": [
    {"id": "anthropic/claude-sonnet-4.5",
     "pricing": {"prompt": "0.000003", "completion": "0.000015"}},
    {"id": "meta-llama/llama-4", "pricing": {"prompt": "0.0000002", "completion": "0.0000006"}},
]}


def make_host(tmp_path: Path, *, which: dict[str, str] | None = None,
              env: dict[str, str] | None = None, routes: dict[str, Any] | None = None,
              runner: Callable[[Sequence[str]], int] | None = None) -> Host:
    table = routes or {}

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for prefix, answer in table.items():
            if url.startswith(prefix):
                if isinstance(answer, int):
                    return httpx.Response(answer, json={})
                return httpx.Response(200, json=answer)
        raise httpx.ConnectError("refused", request=request)

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    found = which or {}
    return Host(which=lambda name: found.get(name), env=env or {}, home=home,
                transport=httpx.MockTransport(handle),
                runner=runner or (lambda argv: 99))


async def run(tmp_path: Path, lines: list[str], host: Host, *replies: str,
              ) -> tuple[str, Repl]:
    if not (tmp_path / "w" / SESSION_PROFILE).exists():
        fake_profile(tmp_path)
    console = ScriptedConsole(lines)
    frontend = LineFrontend(console)
    app = await boot(AppOptions(cwd=tmp_path, workspace=tmp_path / "w"), frontend,
                     adapter=Scripted(*replies))
    repl = Repl(app, frontend)
    repl.conductor.extras["providers_host"] = host
    try:
        assert await repl.run() == 0
    finally:
        repl.app.close()
    return console.transcript(), repl


def profile(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "w" / SESSION_PROFILE).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_commands_are_registered_and_model_is_the_picker() -> None:
    for name in ("/login", "/logout", "/providers", "/models", "/model"):
        assert name in COMMANDS
    assert COMMANDS["/model"].handler.__module__ == "architect.agent.commands_providers"


async def test_providers_lists_every_provider_with_status(tmp_path: Path) -> None:
    host = make_host(tmp_path, which={"claude": "/bin/claude"}, env={"GROQ_API_KEY": "g"},
                     routes={"http://localhost:11434/api/tags": {"models": [{"name": "m"}]}})
    out, _ = await run(tmp_path, ["/providers"], host)
    for provider in ("claude", "chatgpt", "openai", "openrouter", "gemini", "ollama", "vllm",
                     "custom"):
        assert provider in out
    assert "key in GROQ_API_KEY" in out and "reachable at http://localhost:11434" in out
    assert "not reachable at http://localhost:8000/v1" in out
    assert "codex not found" in out


async def test_login_stores_a_checked_key_and_switches(tmp_path: Path) -> None:
    host = make_host(tmp_path, routes={"https://api.openai.com/v1/models": OPENAI_MODELS})
    out, repl = await run(tmp_path, ["/login openai", KEY, "gpt-4o-mini", "/status"], host)
    workspace = tmp_path / "w"
    assert credentials.get("openai", workspace, env={}) == KEY
    assert out.count(KEY) == 1  # only the echo of the typed line in this line UI
    assert "shows what you type" in out
    assert "OpenAI API: key works and is stored (3 models)." in out
    assert "Conductor model: gpt-4o-mini via OpenAI API." in out
    assert "Next compile: model gpt-4o-mini." in out
    assert repl.conductor.adapter.model == "gpt-4o-mini"
    assert repl.conductor.extras["build_overrides"]["model"] == "gpt-4o-mini"
    config = profile(tmp_path)["models"]["gpt-4o-mini"]
    assert config == {"provider": "openai", "base_url": "https://api.openai.com/v1",
                      "api_key_ref": "openai"}
    assert KEY not in (workspace / SESSION_PROFILE).read_text(encoding="utf-8")
    assert "fake" in profile(tmp_path)["models"]  # nothing else changed


async def test_login_picker_and_rejected_key(tmp_path: Path) -> None:
    host = make_host(tmp_path, routes={"https://api.openai.com/v1/models": 401})
    out, repl = await run(tmp_path, ["/login", "4", "sk-wrong"], host)
    assert "Provider (number or name; Enter cancels)" in out
    assert "rejected that key (unauthorized); it was not stored" in out
    assert credentials.get("openai", tmp_path / "w", env={}) is None
    assert repl.conductor.adapter.model == "fake"
    out, _ = await run(tmp_path, ["/login", "", "/login nope"], host)
    assert "Nothing changed." in out and "Unknown provider nope." in out


async def test_login_chatgpt_runs_codex_login_in_the_foreground(tmp_path: Path) -> None:
    ran: list[list[str]] = []

    def runner(argv: Sequence[str]) -> int:
        ran.append(list(argv))
        auth = tmp_path / "home" / ".codex"
        auth.mkdir(parents=True, exist_ok=True)
        (auth / "auth.json").write_text("{}", encoding="utf-8")
        return 0

    host = make_host(tmp_path, which={"codex": "/opt/codex"}, runner=runner)
    out, repl = await run(tmp_path, ["/login chatgpt", ""], host)
    assert ran == [["/opt/codex", "login"]]
    assert "ChatGPT: signed in through codex." in out
    assert "is connected. Switch any time with /model chatgpt:<model>." in out
    assert repl.conductor.adapter.model == "fake"


async def test_login_local_server_lists_models_and_registers_at_zero_cost(tmp_path: Path) -> None:
    host = make_host(tmp_path, routes={"http://localhost:11434/api/tags": {
        "models": [{"name": "gemma4:26b"}, {"name": "qwen3:8b"}]}})
    out, repl = await run(tmp_path, ["/login ollama", "", "2"], host)
    assert "Ollama (local): reachable at http://localhost:11434 ($0, 2 models)." in out
    assert "qwen3:8b  ($0 local)" in out
    assert "Conductor model: qwen3:8b via Ollama (local)." in out
    config = profile(tmp_path)["models"]["qwen3:8b"]
    assert config["provider"] == "ollama" and config["prices"] == "model-prices.json"
    assert config["api_key_ref"] == "ollama"
    assert repl.conductor.extras["build_overrides"]["model"] == "qwen3:8b"
    down = make_host(tmp_path)
    out, _ = await run(tmp_path, ["/login lmstudio", ""], down)
    assert "Nothing answered at http://localhost:1234/v1" in out


async def test_model_picker_lists_and_switches_by_number(tmp_path: Path) -> None:
    host = make_host(tmp_path, which={"claude": str(tmp_path / "claude.exe")},
                     env={"OPENAI_API_KEY": "o"})
    out, repl = await run(tmp_path, ["/model", "/model 1", "/model 99"], host)
    assert "Pick a model" in out and "-- fake" in out
    assert "-- Claude (claude CLI login)" in out and "-- OpenAI API" in out
    assert "claude-haiku-4-5-20251001" in out and "gpt-4o-mini" in out
    picker = repl.conductor.extras[PICKER_KEY]
    assert picker[0] == ("fake", "fake")
    assert "Conductor model: fake." in out and "No model 99 in the list" in out
    # A listed but unconfigured model is added to the profile and used.
    index = next(i for i, (p, m) in enumerate(picker, start=1) if m == "claude-sonnet-5")
    out, repl = await run(tmp_path, [f"/model {index}"], host)
    assert "Conductor model: claude-sonnet-5 via Claude (claude CLI login)." in out
    assert profile(tmp_path)["models"]["claude-sonnet-5"]["provider"] == "claude-code"


async def test_model_provider_prefix_unpriced_and_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "o")  # the profile factory reads the real environment
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    host = make_host(tmp_path, env={"OPENAI_API_KEY": "o"},
                     routes={"https://openrouter.ai/api/v1/models": ROUTER_MODELS})
    out, repl = await run(tmp_path, [
        "/model openai:brand-new-model", "/model anthropic:claude-sonnet-5", "/model nope",
        "/model fake"], host)
    assert "Conductor model: brand-new-model via OpenAI API." in out
    assert "brand-new-model: cost unknown" in out and "will not compile on it" in out
    assert "Anthropic API has no key yet: /login anthropic first." in out
    assert "nope is not in the profile. Models: " in out and "/model <provider>:nope" in out
    assert "Conductor model: fake." in out
    assert repl.conductor.extras["build_overrides"]["model"] == "fake"
    assert "anthropic" not in json.dumps(profile(tmp_path)["models"])
    # OpenRouter models carry the list price into the workspace price table.
    credentials.set_key("openrouter", KEY, tmp_path / "w")
    out, repl = await run(tmp_path, ["/model openrouter:anthropic/claude-sonnet-4.5"], host)
    assert "Conductor model: anthropic/claude-sonnet-4.5 via OpenRouter." in out
    assert "Next compile: model anthropic/claude-sonnet-4.5." in out
    prices = json.loads((tmp_path / "w" / "model-prices.json").read_text(encoding="utf-8"))
    assert prices["models"]["anthropic/claude-sonnet-4.5"]["output_per_million"] == 15.0
    assert prices["models"]["brand-new-model"]["unpriced"] is True


async def test_failed_reload_restores_the_profile(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    path = tmp_path / "w" / SESSION_PROFILE
    payload = json.loads(path.read_text(encoding="utf-8"))
    host = make_host(tmp_path, env={"OPENAI_API_KEY": "o"})
    console = ScriptedConsole(["/model openai:gpt-4o-mini"])
    frontend = LineFrontend(console)
    app = await boot(AppOptions(cwd=tmp_path, workspace=tmp_path / "w"), frontend,
                     adapter=Scripted())
    repl = Repl(app, frontend)
    repl.conductor.extras["providers_host"] = host

    def broken() -> None:
        raise RuntimeError("missing_api_key")

    repl.conductor.extras["reload"] = broken
    try:
        await repl.run()
    finally:
        app.close()
    out = console.transcript()
    assert "gpt-4o-mini was not added: the profile did not load (missing_api_key)." in out
    assert json.loads(path.read_text(encoding="utf-8")) == payload


async def test_models_lists_live_with_prices_and_filters(tmp_path: Path) -> None:
    host = make_host(tmp_path, env={"OPENAI_API_KEY": "o"}, routes={
        "https://openrouter.ai/api/v1/models": ROUTER_MODELS,
        "https://api.openai.com/v1/models": OPENAI_MODELS})
    credentials.set_key("openrouter", KEY, tmp_path / "w")
    out, _ = await run(tmp_path, ["/models openrouter claude", "/models", "/models zzz",
                                  "/models anthropic"], host)
    assert "OpenRouter models matching 'claude'" in out
    assert "openrouter:anthropic/claude-sonnet-4.5" in out and "$3/$15 per M" in out
    assert "openai:gpt-4o-mini" in out and "openai:brand-new-model" in out
    assert "openai:brand-new-model  cost unknown" in out
    assert "Unknown provider zzz." in out
    assert "Anthropic API: no key (/login anthropic)." in out


async def test_logout_prunes_models_and_guards_the_conductor(tmp_path: Path) -> None:
    host = make_host(tmp_path, routes={"https://api.openai.com/v1/models": OPENAI_MODELS})
    out, repl = await run(tmp_path, ["/login openai", KEY, "gpt-4o-mini", "/logout openai"], host)
    assert "The conductor uses gpt-4o-mini from OpenAI API: switch with /model first" in out
    assert credentials.get("openai", tmp_path / "w", env={}) == KEY
    out, repl = await run(tmp_path, ["/model fake", "/logout openai"], host)
    assert "OpenAI API: stored key removed." in out
    assert "Removed from the profile: gpt-4o-mini." in out
    assert "gpt-4o-mini" not in profile(tmp_path)["models"]
    assert credentials.get("openai", tmp_path / "w", env={}) is None
    env_host = make_host(tmp_path, env={"OPENAI_API_KEY": "o"})
    out, _ = await run(tmp_path, ["/logout openai", "/logout claude", "/logout", "/logout x"],
                       env_host)
    assert "OPENAI_API_KEY is still set in your environment" in out
    assert "run 'claude' and /logout there" in out
    assert "Usage: /logout <provider>" in out and "Unknown provider x." in out


async def test_logout_chatgpt_runs_codex_logout(tmp_path: Path) -> None:
    ran: list[list[str]] = []
    host = make_host(tmp_path, which={"codex": "/opt/codex"},
                     runner=lambda argv: ran.append(list(argv)) or 0)  # type: ignore[func-returns-value]
    out, _ = await run(tmp_path, ["/logout chatgpt"], host)
    assert ran == [["/opt/codex", "logout"]] and "signed out of codex" in out


@pytest.mark.parametrize("line", ["/help login", "/help model"])
async def test_help_describes_the_new_commands(tmp_path: Path, line: str) -> None:
    out, _ = await run(tmp_path, [line], make_host(tmp_path))
    assert "usage" in out


async def test_model_add_declares_a_price(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                          ) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "g")
    host = make_host(tmp_path, env={"GROQ_API_KEY": "g"})
    out, repl = await run(tmp_path, [
        "/model add groq:llama-3.3-70b-versatile --price 0.59/0.79", "/model add groq:x --price 1",
        "/model add nothing", "/model add claude:claude-sonnet-5"], host)
    assert "Conductor model: llama-3.3-70b-versatile via Groq." in out
    assert "Next compile: model llama-3.3-70b-versatile." in out  # priced, so compilable
    assert out.count("Usage: /model add") == 2
    assert "models are fixed and priced" in out
    prices = json.loads((tmp_path / "w" / "model-prices.json").read_text(encoding="utf-8"))
    entry = prices["models"]["llama-3.3-70b-versatile"]
    assert entry["input_per_million"] == 0.59 and entry["output_per_million"] == 0.79
    assert "unpriced" not in entry


async def test_model_add_declares_a_price_for_a_bundled_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEBIUS_API_KEY", "n")
    host = make_host(tmp_path, env={"NEBIUS_API_KEY": "n"})
    out, repl = await run(tmp_path, [
        "/model add nebius:nvidia/nemotron-3-super-120b-a12b --price 0.1/0.5"], host)
    assert ("Conductor model: nvidia/nemotron-3-super-120b-a12b via Nebius Token Factory."
            in out)
    config = profile(tmp_path)["models"]["nvidia/nemotron-3-super-120b-a12b"]
    assert config["prices"] == "model-prices.json"
    prices = json.loads((tmp_path / "w" / "model-prices.json").read_text(encoding="utf-8"))
    entry = prices["models"]["nvidia/nemotron-3-super-120b-a12b"]
    assert (entry["input_per_million"], entry["output_per_million"]) == (0.1, 0.5)


NEBIUS_MODELS = {"data": [
    {"id": "nvidia/nemotron-3-super-120b-a12b", "architecture": {"modality": "text->text"},
     "pricing": {"prompt": "0.0000005", "completion": "0.000002"}},  # not what is billed
    {"id": "zai-org/GLM-5.3", "architecture": {"modality": "text->text"},
     "pricing": {"prompt": "0.0000014", "completion": "0.0000044"}},
    {"id": "Qwen/Qwen3-Embedding-8B", "architecture": {"modality": "text->embedding"},
     "pricing": {"prompt": "0.00000001", "completion": "0.0"}},
]}


async def test_nebius_models_are_offered_and_priced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEBIUS_API_KEY", "n")  # the profile factory reads the real environment
    host = make_host(tmp_path, env={"NEBIUS_API_KEY": "n"}, routes={
        "https://api.tokenfactory.nebius.com/v1/models?verbose=true": NEBIUS_MODELS})
    out, repl = await run(tmp_path, ["/model", "/models nebius"], host)
    # Offline suggestions: the four bundled Nemotron models, with their list prices.
    assert "-- Nebius Token Factory" in out
    for model, label in (("nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B", "$0.06/$0.24 per M"),
                         ("nvidia/Nemotron-3_5-Lightning", "$0.06/$0.24 per M"),
                         ("nvidia/nemotron-3-super-120b-a12b", "$0.3/$0.9 per M"),
                         ("nvidia/Nemotron-3-Ultra-550b-a55b", "$1/$3 per M")):
        assert model in out and label in out
    picker = repl.conductor.extras[PICKER_KEY]
    assert ("nebius", "nvidia/Nemotron-3-Ultra-550b-a55b") in picker
    # The live list shows text models only.
    assert "nebius:zai-org/GLM-5.3" in out and "Qwen3-Embedding" not in out
    # A bundled model keeps its curated price; another model takes the listed one.
    out, repl = await run(tmp_path, ["/model nebius:nvidia/nemotron-3-super-120b-a12b",
                                     "/model nebius:zai-org/GLM-5.3"], host)
    assert ("Conductor model: nvidia/nemotron-3-super-120b-a12b via Nebius Token Factory."
            in out)
    assert "Next compile: model nvidia/nemotron-3-super-120b-a12b." in out
    assert "Next compile: model zai-org/GLM-5.3." in out  # priced, so compilable
    config = profile(tmp_path)["models"]["nvidia/nemotron-3-super-120b-a12b"]
    assert config == {"provider": "openai", "base_url": "https://api.tokenfactory.nebius.com/v1",
                      "api_key_ref": "nebius"}
    prices = json.loads((tmp_path / "w" / "model-prices.json").read_text(encoding="utf-8"))
    assert set(prices["models"]) == {"zai-org/GLM-5.3"}
    assert prices["models"]["zai-org/GLM-5.3"]["output_per_million"] == 4.4
    assert repl.conductor.adapter.model == "zai-org/GLM-5.3"


EXPENSE_APP = Path(__file__).resolve().parents[1] / "examples" / "expense-app" / "architect.json"
RELAY = "https://relay.example/v1"
SUPER = "nvidia/nemotron-3-super-120b-a12b"


async def test_login_again_in_the_expense_app_keeps_its_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/login nebius in examples/expense-app rewrote its architect.json and dropped
    temperature 0 and max_tokens 512 from the model picked after the sign-in."""
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    project = tmp_path / "app"
    project.mkdir()
    (project / "architect.json").write_bytes(EXPENSE_APP.read_bytes())
    original = (project / "architect.json").read_bytes()
    workspace = tmp_path / "w"
    credentials.set_key("nebius", "rc_" + "a" * 32, workspace)  # signed in before
    credentials.set_base_url("nebius", RELAY, workspace)
    listed = {"data": [{"id": SUPER, "architecture": {"modality": "text->text"},
                        "pricing": {"prompt": "0.0000003", "completion": "0.0000009"}}]}
    host = make_host(tmp_path, env={"ARCHITECT_RELAY_URL": RELAY},
                     routes={f"{RELAY}/models": listed})
    console = ScriptedConsole(["/login nebius", "y", "rc_" + "b" * 32, SUPER])
    frontend = LineFrontend(console)
    app = await boot(AppOptions(cwd=project, workspace=workspace), frontend,
                     adapter=Scripted())
    repl = Repl(app, frontend)
    repl.conductor.extras["providers_host"] = host
    try:
        assert app.profile_path == project / "architect.json"
        assert await repl.run() == 0
        out = console.transcript()
        assert f"Conductor model: {SUPER} via Nebius Token Factory." in out
        config = repl.app.services.profile.models[SUPER]
        assert (config.temperature, config.max_tokens, config.base_url) == (0.0, 512, None)
    finally:
        repl.app.close()
    assert credentials.get("nebius", workspace, env={}) == "rc_" + "b" * 32
    assert (project / "architect.json").read_bytes() == original
