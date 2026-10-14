"""CLI-backed adapters against fake executables; the real CLIs are never called."""

import asyncio
import json
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

from architect import profiles
from architect.models import AdapterError, ClaudeCodeAdapter, CodexAdapter, ModelRequest
from architect.models.cli import MINIMAL_SYSTEM_PROMPT
from architect.profiles import ProfileError, load_profile

# Subprocess tests use POSIX fake executables; the pure builder tests run everywhere.
posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake executables")

HAIKU = "claude-haiku-4-5-20251001"
REQUEST = ModelRequest(prompt="Say ok")


def fake_cli(tmp_path: Path, body: str, name: str = "fake-cli") -> Path:
    """Write an executable Python script; ``RECORD`` is a path outside its cwd."""
    path = tmp_path / name
    record = tmp_path / "record.json"
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, os, subprocess, sys, time\n"
        f"RECORD = {str(record)!r}\n"
        "PROMPT = sys.stdin.read()\n"
        "def record(**extra):\n"
        "    data = {'argv': sys.argv[1:], 'cwd': os.getcwd(), 'files': os.listdir('.'),\n"
        "            'env': dict(os.environ), 'prompt': PROMPT, **extra}\n"
        "    open(RECORD, 'w').write(json.dumps(data))\n"
        + textwrap.dedent(body),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def exists(path: str) -> bool:
    return Path(path).exists()


def recorded(tmp_path: Path) -> dict[str, object]:
    data = json.loads((tmp_path / "record.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def claude_result(**overrides: object) -> str:
    payload: dict[str, object] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "hello",
        "total_cost_usd": 0.0135,
        "usage": {
            "input_tokens": 9,
            "cache_creation_input_tokens": 6605,
            "cache_read_input_tokens": 0,
            "output_tokens": 52,
        },
        "modelUsage": {HAIKU: {"inputTokens": 9}},
    }
    payload.update(overrides)
    return json.dumps(payload)


def claude(tmp_path: Path, stdout: str, code: int = 0, **kwargs: object) -> ClaudeCodeAdapter:
    body = f"record()\nsys.stdout.write({stdout!r})\nsys.exit({code})\n"
    return ClaudeCodeAdapter(HAIKU, executable=str(fake_cli(tmp_path, body)), **kwargs)  # type: ignore[arg-type]


@posix
async def test_claude_success_parses_usage_and_isolates(tmp_path: Path) -> None:
    adapter = claude(tmp_path, claude_result())
    response = await adapter.complete(REQUEST)
    assert response.text == "hello"
    assert response.tokens_in == 9 + 6605
    assert response.tokens_out == 52
    assert response.model == HAIKU
    # Reported cost exceeds the list-price estimate here, so it wins.
    assert response.cost_usd == pytest.approx(0.0135)
    seen = recorded(tmp_path)
    assert seen["prompt"] == "Say ok"
    assert seen["files"] == []
    assert not exists(str(seen["cwd"]))
    assert seen["argv"] == [
        "-p", "--output-format", "json", "--tools", "", "--setting-sources", "",
        "--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands",
        "--model", HAIKU, "--system-prompt", MINIMAL_SYSTEM_PROMPT,
    ]
    assert "--bare" not in seen["argv"]  # type: ignore[operator]


@posix
async def test_claude_can_keep_cli_system_prompt(tmp_path: Path) -> None:
    await claude(tmp_path, claude_result(), system_prompt=None).complete(REQUEST)
    argv = recorded(tmp_path)["argv"]
    assert isinstance(argv, list)
    assert "--system-prompt" not in argv


@posix
async def test_claude_cost_uses_price_table_when_larger(tmp_path: Path) -> None:
    adapter = claude(tmp_path, claude_result(total_cost_usd=0))
    response = await adapter.complete(REQUEST)
    # The 6605 cache-creation tokens are priced at Haiku's 1.25x cache-write rate.
    assert response.cost_usd == pytest.approx((9 * 1.0 + 6605 * 1.25 + 52 * 5.0) / 1_000_000)
    assert (response.cache_write_tokens, response.cache_read_tokens) == (6605, 0)


@pytest.mark.parametrize("overrides", [
    {"is_error": True},
    {"subtype": "error_max_turns"},
    {"type": "system"},
    {"usage": None},
    {"usage": {"input_tokens": -1, "output_tokens": 1}},
    {"usage": {"input_tokens": 1}},
    {"usage": {"input_tokens": 1, "output_tokens": True}},
    {"result": None},
    {"total_cost_usd": "free"},
    {"total_cost_usd": -1},
    {"modelUsage": {"claude-sonnet-5": {}}},
    {"modelUsage": None},
])
@posix
async def test_claude_rejects_bad_results(tmp_path: Path, overrides: dict[str, object]) -> None:
    adapter = claude(tmp_path, claude_result(**overrides))
    with pytest.raises(AdapterError) as info:
        await adapter.complete(REQUEST)
    assert "hello" not in str(info.value)


@posix
async def test_claude_rejects_invalid_json_and_failure_exit(tmp_path: Path) -> None:
    with pytest.raises(AdapterError, match="invalid JSON"):
        await claude(tmp_path, "secret prompt echo").complete(REQUEST)
    with pytest.raises(AdapterError, match="^Provider reported an error$"):
        await claude(tmp_path, claude_result(), code=1).complete(REQUEST)


@posix
async def test_claude_allows_configured_response_models(tmp_path: Path) -> None:
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({
        "models": {HAIKU: {
            "input_per_million": 1.0, "output_per_million": 5.0,
            "input_overhead_tokens": 0, "output_capacity_tokens": 10,
            "allowed_response_models": ["claude-haiku-4-5"],
        }},
        "fake_usage": {"tokens_in": 1, "tokens_out": 1},
    }), encoding="utf-8")
    adapter = claude(
        tmp_path, claude_result(modelUsage={"claude-haiku-4-5": {}}), prices_path=prices
    )
    assert (await adapter.complete(REQUEST)).text == "hello"


@posix
async def test_claude_env_scrubs_credentials_and_parent_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-inherited")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-inherited")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_TOKEN", "parent")
    monkeypatch.setenv("ARCHITECT_KEEP_ME", "yes")
    await claude(tmp_path, claude_result()).complete(REQUEST)
    env = recorded(tmp_path)["env"]
    assert isinstance(env, dict)
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDECODE",
                 "CLAUDE_CODE_MESSAGING_TOKEN"):
        assert name not in env
    assert env["ARCHITECT_KEEP_ME"] == "yes"
    await claude(tmp_path, claude_result(), api_key="sk-explicit").complete(REQUEST)
    env = recorded(tmp_path)["env"]
    assert isinstance(env, dict)
    assert env["ANTHROPIC_API_KEY"] == "sk-explicit"
    assert "ANTHROPIC_AUTH_TOKEN" not in env


@posix
async def test_oversized_output_is_rejected(tmp_path: Path) -> None:
    body = "sys.stdout.write('x' * 200_000)\nsys.stdout.flush()\ntime.sleep(30)\n"
    adapter = ClaudeCodeAdapter(
        HAIKU, executable=str(fake_cli(tmp_path, body)), max_output_bytes=1024, timeout=20
    )
    started = time.monotonic()
    with pytest.raises(AdapterError, match="^Provider output exceeded size limit$"):
        await adapter.complete(REQUEST)
    assert time.monotonic() - started < 10
    body = "sys.stderr.write('e' * 200_000)\nsys.stderr.flush()\ntime.sleep(30)\n"
    adapter = ClaudeCodeAdapter(
        HAIKU, executable=str(fake_cli(tmp_path, body)), max_output_bytes=1024, timeout=20
    )
    with pytest.raises(AdapterError, match="^Provider output exceeded size limit$"):
        await adapter.complete(REQUEST)


SLEEPER = """
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
record(child=child.pid, parent=os.getpid())
time.sleep(60)
"""


def gone(pid: int) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


@posix
async def test_timeout_kills_process_group(tmp_path: Path) -> None:
    adapter = ClaudeCodeAdapter(HAIKU, executable=str(fake_cli(tmp_path, SLEEPER)), timeout=1.0)
    with pytest.raises(AdapterError, match="^Provider CLI timed out$"):
        await adapter.complete(REQUEST)
    seen = recorded(tmp_path)
    assert gone(int(str(seen["parent"])))
    assert gone(int(str(seen["child"])))


@posix
async def test_cancellation_kills_process_group(tmp_path: Path) -> None:
    adapter = CodexAdapter(executable=str(fake_cli(tmp_path, SLEEPER)), timeout=60)
    task = asyncio.create_task(adapter.complete(REQUEST))
    record = tmp_path / "record.json"
    for _ in range(200):
        if record.exists() and record.stat().st_size:
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    seen = recorded(tmp_path)
    assert gone(int(str(seen["parent"])))
    assert gone(int(str(seen["child"])))


@posix
async def test_missing_executable_and_bad_settings(tmp_path: Path) -> None:
    with pytest.raises(AdapterError, match="^Provider CLI is unavailable$"):
        await ClaudeCodeAdapter(HAIKU, executable=str(tmp_path / "missing")).complete(REQUEST)
    with pytest.raises(AdapterError):
        ClaudeCodeAdapter(HAIKU, timeout=0)
    with pytest.raises(AdapterError):
        CodexAdapter(max_output_bytes=0)
    with pytest.raises(AdapterError, match="No configured price"):
        ClaudeCodeAdapter("claude-unknown")


@posix
def test_quote_reserves_capacity() -> None:
    quote = ClaudeCodeAdapter(HAIKU).quote(REQUEST)
    assert quote.tokens_in == len(b"Say ok") + 16384
    assert quote.tokens_out == 64000
    assert CodexAdapter().quote(ModelRequest(prompt="x", max_output_tokens=5)).tokens_out == 5


def codex_body(events: list[dict[str, object]], text: str | None = "done", code: int = 0) -> str:
    lines = "".join(json.dumps(event) + "\n" for event in events)
    write = (
        f"open(sys.argv[sys.argv.index('-o') + 1], 'w').write({text!r})\n"
        if text is not None else ""
    )
    return f"record()\n{write}sys.stdout.write({lines!r})\nsys.exit({code})\n"


SUCCESS = [
    {"type": "thread.started", "thread_id": "t"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "1", "type": "agent_message", "text": "done"}},
    {"type": "turn.completed",
     "usage": {"input_tokens": 1200, "cached_input_tokens": 1000, "output_tokens": 30}},
]


def codex(tmp_path: Path, body: str) -> CodexAdapter:
    return CodexAdapter(executable=str(fake_cli(tmp_path, body)))


@posix
async def test_codex_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-inherited")
    monkeypatch.setenv("CODEX_API_KEY", "sk-inherited")
    response = await codex(tmp_path, codex_body(SUCCESS)).complete(REQUEST)
    assert response.text == "done"
    assert (response.tokens_in, response.tokens_out) == (1200, 30)
    assert response.cost_usd == pytest.approx((1200 * 2.0 + 30 * 12.0) / 1_000_000)
    assert response.model == "gpt-5.6-terra"
    seen = recorded(tmp_path)
    argv = seen["argv"]
    assert isinstance(argv, list)
    output = argv[argv.index("-o") + 1]
    assert argv[:argv.index("-o")] == [
        "exec", "--json", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only",
        "--ignore-rules", "-m", "gpt-5.6-terra",
    ]
    assert argv[-1] == "-"
    overrides = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-c"]
    assert overrides[1:] == [
        "include_environment_context=false",
        "include_permissions_instructions=false",
        "include_apps_instructions=false",
    ]
    key, value = overrides[0].split("=", 1)
    assert key == "model_instructions_file"
    assert Path(json.loads(value)).parent == Path(output).parent
    assert json.loads(value) != str(seen["cwd"])
    assert seen["files"] == []
    assert Path(output).parent != Path(str(seen["cwd"]))
    assert not exists(output)
    env = seen["env"]
    assert isinstance(env, dict)
    assert "OPENAI_API_KEY" not in env and "CODEX_API_KEY" not in env
    assert seen["prompt"] == "Say ok"


@posix
async def test_codex_can_keep_cli_instructions(tmp_path: Path) -> None:
    exe = fake_cli(tmp_path, codex_body(SUCCESS))
    await CodexAdapter(executable=str(exe), system_prompt=None).complete(REQUEST)
    argv = recorded(tmp_path)["argv"]
    assert isinstance(argv, list)
    assert "-c" not in argv


@posix
async def test_codex_transient_error_before_success(tmp_path: Path) -> None:
    events = [{"type": "error", "message": "Reconnecting... 1/5"}, *SUCCESS]
    assert (await codex(tmp_path, codex_body(events)).complete(REQUEST)).text == "done"


@pytest.mark.parametrize(("events", "code", "message"), [
    ([{"type": "thread.started", "thread_id": "t"},
      {"type": "error", "message": "You've hit your usage limit. Try again later."}],
     1, "^Provider usage limit reached$"),
    ([{"type": "turn.failed", "error": {"message": "Usage limit reached for plan"}}],
     1, "^Provider usage limit reached$"),
    ([{"type": "turn.failed", "error": {"message": "stream disconnected: secret"}}],
     1, "^Provider reported an error$"),
    ([{"type": "error", "message": "boom secret"}], 1, "^Provider reported an error$"),
    ([*SUCCESS, {"type": "turn.failed", "error": {"message": "late"}}],
     0, "^Provider reported an error$"),
    (SUCCESS, 2, "^Provider reported an error$"),
    ([{"type": "turn.completed", "usage": {"input_tokens": 1}}], 0, "token usage"),
    ([{"type": "turn.completed",
       "usage": {"input_tokens": 1, "cached_input_tokens": 5, "output_tokens": 1}}],
     0, "token usage"),
    ([{"type": "turn.started"}], 0, "^Provider reported an error$"),
])
@posix
async def test_codex_error_shapes(
    tmp_path: Path, events: list[dict[str, object]], code: int, message: str
) -> None:
    with pytest.raises(AdapterError, match=message) as info:
        await codex(tmp_path, codex_body(events, code=code)).complete(REQUEST)
    assert "secret" not in str(info.value)


@posix
async def test_codex_invalid_jsonl_and_missing_or_large_text(tmp_path: Path) -> None:
    body = "record()\nsys.stdout.write('not json\\n')\n"
    with pytest.raises(AdapterError, match="invalid JSON"):
        await codex(tmp_path, body).complete(REQUEST)
    with pytest.raises(AdapterError, match="missing text"):
        await codex(tmp_path, codex_body(SUCCESS, text=None)).complete(REQUEST)
    adapter = CodexAdapter(
        executable=str(fake_cli(tmp_path, codex_body(SUCCESS, text="y" * 5000))),
        max_output_bytes=4096,
    )
    with pytest.raises(AdapterError, match="exceeded size limit"):
        await adapter.complete(REQUEST)


def write_profile(tmp_path: Path, models: dict[str, object]) -> Path:
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"models": models}), encoding="utf-8")
    return path


@posix
async def test_profiles_load_cli_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude_exe = fake_cli(tmp_path, f"record()\nsys.stdout.write({claude_result()!r})\n", "c")
    codex_exe = fake_cli(tmp_path, codex_body(SUCCESS), "x")
    profile = load_profile(write_profile(tmp_path, {
        HAIKU: {"provider": "claude-code", "executable": str(claude_exe)},
        "gpt-5.6-terra": {"provider": "codex", "executable": str(codex_exe)},
        "claude-sonnet-5": {"provider": "claude-code", "api_key_env": "MY_KEY"},
    }))
    first = profiles._adapter(profile, HAIKU, profile.models[HAIKU])
    assert isinstance(first, ClaudeCodeAdapter)
    assert (await first.complete(REQUEST)).text == "hello"
    second = profiles._adapter(profile, "gpt-5.6-terra", profile.models["gpt-5.6-terra"])
    assert isinstance(second, CodexAdapter)
    assert (await second.complete(REQUEST)).text == "done"
    with pytest.raises(ProfileError, match="^missing_api_key$"):
        profiles._adapter(profile, "claude-sonnet-5", profile.models["claude-sonnet-5"])
    monkeypatch.setenv("MY_KEY", "sk-configured")
    keyed = profiles._adapter(profile, "claude-sonnet-5", profile.models["claude-sonnet-5"])
    assert isinstance(keyed, ClaudeCodeAdapter)
    default = load_profile(write_profile(tmp_path, {HAIKU: {"provider": "claude-code"}}))
    assert isinstance(profiles._adapter(default, HAIKU, default.models[HAIKU]), ClaudeCodeAdapter)


@pytest.mark.parametrize("config", [
    {"provider": "claude-code", "base_url": "http://localhost"},
    {"provider": "codex", "base_url": "http://localhost"},
    {"provider": "codex", "api_key_env": "OPENAI_API_KEY"},
    {"provider": "anthropic", "executable": "claude"},
    {"provider": "fake", "executable": "claude"},
    {"provider": "codex", "fixture": "responses.json"},
    {"provider": "claude-code", "executable": ""},
])
@posix
def test_profiles_reject_invalid_cli_settings(tmp_path: Path, config: dict[str, object]) -> None:
    with pytest.raises(ProfileError, match="^invalid_profile$"):
        load_profile(write_profile(tmp_path, {HAIKU: config}))


@posix
def test_profile_unpriced_cli_model_is_unavailable(tmp_path: Path) -> None:
    profile = load_profile(write_profile(tmp_path, {"gpt-9": {"provider": "codex"}}))
    with pytest.raises(ProfileError, match="^adapter_unavailable$"):
        profiles._adapter(profile, "gpt-9", profile.models["gpt-9"])


@pytest.mark.parametrize(
    ("raw", "text"),
    [
        ('```json\n{"ok": true}\n```', '{"ok": true}'),
        ('\n```\n{"ok": true}\n```\n', '{"ok": true}'),
        ('{"ok": true}', '{"ok": true}'),
        ('see ```x``` inline', 'see ```x``` inline'),
        ('```a\n1\n```\ntext\n```b\n2\n```', '```a\n1\n```\ntext\n```b\n2\n```'),
    ],
)
@posix
def test_only_a_fence_enclosing_the_whole_reply_is_removed(raw: str, text: str) -> None:
    from architect.models.cli import _unfence

    assert _unfence(raw) == text


# --- Phase 8: image inputs on the CLI adapters -----------------------------------------

import base64  # noqa: E402

from image_support import jpeg_bytes, png_bytes  # noqa: E402

from architect.models.images import ImageInput  # noqa: E402

PNG = ImageInput.from_bytes(png_bytes(20, 10))
JPEG = ImageInput.from_bytes(jpeg_bytes(30, 40))
IMAGE_REQUEST = ModelRequest(prompt="Read the page", images=(PNG, JPEG))
LEGACY_CLAUDE_ARGV = [
    "claude", "-p", "--output-format", "json", "--tools", "", "--setting-sources", "",
    "--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands",
    "--model", HAIKU, "--system-prompt", MINIMAL_SYSTEM_PROMPT,
]
STREAM_CLAUDE_ARGV = [
    "claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
    "--verbose", "--tools", "", "--setting-sources", "", "--strict-mcp-config",
    "--no-session-persistence", "--disable-slash-commands", "--model", HAIKU,
    "--system-prompt", MINIMAL_SYSTEM_PROMPT,
]


def test_claude_builders_keep_text_only_argv_and_stdin() -> None:
    adapter = ClaudeCodeAdapter(HAIKU)
    assert adapter._argv(REQUEST) == LEGACY_CLAUDE_ARGV
    assert adapter._stdin(REQUEST) == b"Say ok"
    cached = ModelRequest(prompt="prefix rest", cache_prefix="prefix ")
    assert adapter._stdin(cached) == b"prefix rest"
    bare = ClaudeCodeAdapter(HAIKU, system_prompt=None)
    assert bare._argv(REQUEST) == LEGACY_CLAUDE_ARGV[:-2]


def test_claude_builders_stream_one_json_user_line_with_images() -> None:
    adapter = ClaudeCodeAdapter(HAIKU)
    assert adapter._argv(IMAGE_REQUEST) == STREAM_CLAUDE_ARGV
    stdin = adapter._stdin(IMAGE_REQUEST)
    assert stdin.endswith(b"\n") and stdin.count(b"\n") == 1
    assert json.loads(stdin) == {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "text", "text": "Read the page"},
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png",
                    "data": base64.b64encode(PNG.data).decode(),
                }},
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/jpeg",
                    "data": base64.b64encode(JPEG.data).decode(),
                }},
            ],
        },
    }


def test_codex_argv_builder_names_image_files_outside_work(tmp_path: Path) -> None:
    adapter = CodexAdapter()
    legacy = adapter._argv(tmp_path, REQUEST)
    assert legacy[:legacy.index("-c")] == [
        "codex", "exec", "--json", "--ephemeral", "--skip-git-repo-check", "--sandbox",
        "read-only", "--ignore-rules", "-m", "gpt-5.6-terra", "-o",
        str(tmp_path / "last-message.txt"),
    ]
    assert "-i" not in legacy and legacy[-1] == "-"
    argv = adapter._argv(tmp_path, IMAGE_REQUEST)
    assert argv[:-5] == legacy[:-1]
    assert argv[-5:] == [
        "-i", str(tmp_path / "image-1.png"), "-i", str(tmp_path / "image-2.jpg"), "-",
    ]
    assert not any(Path(arg).parent == tmp_path / "work" for arg in argv)
    assert "-i" not in CodexAdapter(system_prompt=None)._argv(tmp_path, REQUEST)


def test_cli_quotes_reserve_the_image_ceiling_before_any_process() -> None:
    claude_quote = ClaudeCodeAdapter(HAIKU).quote(IMAGE_REQUEST)
    assert claude_quote.tokens_in == len(b"Read the page") + 16384 + 2 * 1600
    codex_quote = CodexAdapter().quote(IMAGE_REQUEST)
    assert codex_quote.tokens_in == len(b"Read the page") + 16384 + 2 * 8192
    text_only = CodexAdapter().quote(ModelRequest(prompt="Read the page"))
    assert codex_quote.cost_usd > text_only.cost_usd


async def test_cli_model_without_image_price_refuses_before_spawning(tmp_path: Path) -> None:
    missing = str(tmp_path / "never-executed")
    for adapter in (
        ClaudeCodeAdapter("llama3.2", executable=missing),
        CodexAdapter("llama3.2", executable=missing),
    ):
        with pytest.raises(AdapterError, match="^Model is not configured for image input$"):
            adapter.quote(IMAGE_REQUEST)
        with pytest.raises(AdapterError, match="^Model is not configured for image input$"):
            await adapter.complete(IMAGE_REQUEST)


def test_claude_stream_result_selection() -> None:
    result = json.loads(claude_result())
    lines = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}},
        result,
    ]
    stdout = "".join(json.dumps(line) + "\n" for line in lines).encode()
    assert ClaudeCodeAdapter._result(stdout, True) == result
    with pytest.raises(AdapterError, match="^Provider reported an error$"):
        ClaudeCodeAdapter._result(stdout + (json.dumps(result) + "\n").encode(), True)
    with pytest.raises(AdapterError, match="^Provider reported an error$"):
        ClaudeCodeAdapter._result(json.dumps(lines[0]).encode(), True)
    with pytest.raises(AdapterError, match="invalid JSON"):
        ClaudeCodeAdapter._result(stdout + b"not json\n", True)
    with pytest.raises(AdapterError, match="invalid JSON"):
        ClaudeCodeAdapter._result(stdout, False)  # JSONL is not the text-only JSON reply


def stream_body(lines: list[str], code: int = 0) -> str:
    output = "".join(line + "\n" for line in lines)
    return f"record()\nsys.stdout.write({output!r})\nsys.exit({code})\n"


@posix
async def test_claude_streams_images_on_stdin_and_selects_the_result_line(tmp_path: Path) -> None:
    lines = [
        json.dumps({"type": "system", "subtype": "init", "model": HAIKU}),
        json.dumps({"type": "assistant", "message": {"content": "hello"}}),
        claude_result(),
    ]
    exe = fake_cli(tmp_path, stream_body(lines))
    adapter = ClaudeCodeAdapter(HAIKU, executable=str(exe))
    response = await adapter.complete(IMAGE_REQUEST)
    assert response.text == "hello"
    assert (response.tokens_in, response.tokens_out) == (9 + 6605, 52)
    assert response.cost_usd <= adapter.quote(IMAGE_REQUEST).cost_usd
    seen = recorded(tmp_path)
    assert seen["argv"] == STREAM_CLAUDE_ARGV[1:]
    prompt = seen["prompt"]
    assert isinstance(prompt, str) and prompt.endswith("\n") and prompt.count("\n") == 1
    content = json.loads(prompt)["message"]["content"]
    assert [block["type"] for block in content] == ["text", "image", "image"]
    assert base64.b64decode(content[1]["source"]["data"]) == PNG.data


@posix
@pytest.mark.parametrize("lines", [
    [json.dumps({"type": "system"})],
    [claude_result(), claude_result()],
    [claude_result(is_error=True)],
    [json.dumps({"type": "system"}), "secret prompt echo"],
])
async def test_claude_stream_fails_closed_on_unexpected_shapes(
    tmp_path: Path, lines: list[str],
) -> None:
    exe = fake_cli(tmp_path, stream_body(lines))
    with pytest.raises(AdapterError) as info:
        await ClaudeCodeAdapter(HAIKU, executable=str(exe)).complete(IMAGE_REQUEST)
    assert "secret" not in str(info.value) and "hello" not in str(info.value)


@posix
async def test_codex_passes_each_image_file_with_i_before_stdin_dash(tmp_path: Path) -> None:
    body = (
        "import hashlib\n"
        "paths = [sys.argv[i + 1] for i, arg in enumerate(sys.argv) if arg == '-i']\n"
        "images = [[p, os.path.exists(p), hashlib.sha256(open(p, 'rb').read()).hexdigest()]\n"
        "          for p in paths]\n"
        + codex_body(SUCCESS).replace("record()", "record(images=images)", 1)
    )
    adapter = CodexAdapter(executable=str(fake_cli(tmp_path, body)))
    response = await adapter.complete(IMAGE_REQUEST)
    assert response.text == "done"
    seen = recorded(tmp_path)
    argv = seen["argv"]
    assert isinstance(argv, list) and argv[-1] == "-"
    assert [arg for arg in argv if arg == "-i"] == ["-i", "-i"]
    images = seen["images"]
    assert isinstance(images, list)
    assert [(Path(p).name, found, digest) for p, found, digest in images] == [
        ("image-1.png", True, PNG.sha256), ("image-2.jpg", True, JPEG.sha256),
    ]
    work = Path(str(seen["cwd"]))
    assert all(Path(p).parent == work.parent for p, _, _ in images)
    assert seen["files"] == [] and seen["prompt"] == "Read the page"
    assert not any(exists(p) for p, _, _ in images)


def test_cli_builders_ignore_a_response_schema(tmp_path: Path) -> None:
    shaped = REQUEST.model_copy(update={"response_schema": {"type": "object"}})
    claude = ClaudeCodeAdapter(HAIKU)
    assert claude._argv(shaped) == claude._argv(REQUEST) == LEGACY_CLAUDE_ARGV
    assert claude._stdin(shaped) == claude._stdin(REQUEST)
    codex = CodexAdapter()
    assert codex._argv(tmp_path, shaped) == codex._argv(tmp_path, REQUEST)
