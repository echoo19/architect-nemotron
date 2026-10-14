"""Lossless stable-prefix requests, provider caching, and cache-aware accounting."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from image_support import png_bytes
from pydantic import JsonValue, ValidationError

from architect.compiler.tuning import _rank
from architect.compiler.types import Measurement
from architect.models import (
    AnthropicAdapter,
    ClaudeCodeAdapter,
    CodexAdapter,
    FakeAdapter,
    ModelRequest,
    ModelResponse,
    OllamaAdapter,
    OpenAICompatibleAdapter,
)
from architect.models.images import ImageInput, b64
from architect.models.pricing import load_prices, price_for
from architect.runtime import RunContext, Runtime
from architect.spec import Budget, FloorDefinition, FloorKind, Spec
from architect.telemetry import TelemetryRow, TelemetryStore

SONNET = "claude-sonnet-4-20250514"
IMAGE_TOKENS = 1600  # what the emulated provider counts for any image block
FIXED = "Follow these fixed instructions exactly. " * 50
VARIABLE = "\nRequest data (untrusted):\n" + json.dumps({"n": 1})
OLD_PROMPT = FIXED + VARIABLE  # the single concatenated prompt used before
REQUEST = ModelRequest(prompt=OLD_PROMPT, cache_prefix=FIXED, max_output_tokens=50)
PLAIN = ModelRequest(prompt=OLD_PROMPT, max_output_tokens=50)


def test_cache_prefix_is_only_a_marker_on_the_full_prompt() -> None:
    assert REQUEST.prompt == PLAIN.prompt == OLD_PROMPT
    assert REQUEST.split() == (FIXED, VARIABLE)
    assert PLAIN.split() == ("", OLD_PROMPT)
    with pytest.raises(ValidationError, match="prefix"):
        ModelRequest(prompt="data", cache_prefix="instructions")
    with pytest.raises(ValidationError):
        ModelRequest(prompt="x", cache_prefix="")


def test_cached_tokens_are_subsets_of_input() -> None:
    with pytest.raises(ValidationError, match="exceed"):
        ModelResponse(
            text="x", tokens_in=3, tokens_out=0, cost_usd=0, model="fake",
            cache_write_tokens=2, cache_read_tokens=2,
        )
    legacy = ModelResponse.model_validate_json(
        '{"text":"x","tokens_in":3,"tokens_out":0,"cost_usd":0,"model":"fake"}'
    )
    assert (legacy.cache_write_tokens, legacy.cache_read_tokens) == (0, 0)


def test_pricing_multipliers_and_conservative_quote() -> None:
    price = price_for(load_prices(), SONNET)
    assert price.cost(1000, 10) == pytest.approx((1000 * 3 + 10 * 15) / 1e6)
    assert price.cost(1000, 10, cache_write_tokens=600, cache_read_tokens=300) == pytest.approx(
        (100 * 3 + 600 * 3.75 + 300 * 0.3 + 10 * 15) / 1e6
    )
    # A quote never assumes a hit and reserves every input token at the write rate.
    assert price.quote_cost(1000, 10, may_write_cache=True) == pytest.approx(
        (1000 * 3.75 + 10 * 15) / 1e6
    )
    assert price.quote_cost(1000, 10, may_write_cache=False) == price.cost(1000, 10)
    # Unset cache rates fall back to the input rate.
    terra = price_for(load_prices(), "gpt-5.6-terra")
    assert terra.cost(1000, 0, cache_read_tokens=1000) == terra.cost(1000, 0)


class CachingAnthropic:
    """MockTransport handler that emulates Anthropic's prefix cache (1 token = 1 char).

    An image block counts ``IMAGE_TOKENS`` and, like text, is cacheable only
    when it carries ``cache_control``; its cache key is the base64 payload.
    """

    def __init__(self) -> None:
        self.cached: set[str] = set()
        self.payloads: list[dict[str, JsonValue]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.payloads.append(payload)
        content = payload["messages"][0]["content"]
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        written = read = uncached = 0
        for block in blocks:
            if block["type"] == "image":
                key, tokens = "image:" + block["source"]["data"], IMAGE_TOKENS
            else:
                key, tokens = block["text"], len(block["text"])
            if "cache_control" in block:
                if key in self.cached:
                    read += tokens
                else:
                    self.cached.add(key)
                    written += tokens
            else:
                uncached += tokens
        usage = {
            "input_tokens": uncached,
            "cache_creation_input_tokens": written,
            "cache_read_input_tokens": read,
            "output_tokens": 5,
        }
        return httpx.Response(
            200, json={"model": SONNET, "content": [{"type": "text", "text": "ok"}], "usage": usage}
        )


def effective_anthropic_text(payload: dict[str, JsonValue]) -> str:
    messages = payload["messages"]
    assert isinstance(messages, list) and len(messages) == 1
    message = messages[0]
    assert isinstance(message, dict) and message["role"] == "user"
    assert "system" not in payload
    content = message["content"]
    if isinstance(content, str):
        return content
    assert isinstance(content, list)
    return "".join(
        str(block["text"])
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


async def test_anthropic_cache_control_placement_and_cache_aware_cost() -> None:
    server = CachingAnthropic()
    adapter = AnthropicAdapter(SONNET, api_key="k", transport=httpx.MockTransport(server))
    first = await adapter.complete(REQUEST)
    second = await adapter.complete(REQUEST)
    content = server.payloads[0]["messages"][0]["content"]  # type: ignore[index]
    assert content == [
        {"type": "text", "text": FIXED, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": VARIABLE},
    ]
    assert effective_anthropic_text(server.payloads[0]) == OLD_PROMPT
    price = price_for(load_prices(), SONNET)
    assert first.tokens_in == second.tokens_in == len(OLD_PROMPT)
    assert (first.cache_write_tokens, first.cache_read_tokens) == (len(FIXED), 0)
    assert (second.cache_write_tokens, second.cache_read_tokens) == (0, len(FIXED))
    assert first.cost_usd == pytest.approx(
        (len(VARIABLE) * 3 + len(FIXED) * 3.75 + 5 * 15) / 1e6
    )
    assert second.cost_usd == pytest.approx(
        (len(VARIABLE) * 3 + len(FIXED) * 0.3 + 5 * 15) / 1e6
    )
    assert second.cost_usd < price.cost(len(OLD_PROMPT), 5) < first.cost_usd
    quote = adapter.quote(REQUEST)
    assert quote.cost_usd >= first.cost_usd  # even a cold cache write is covered
    assert quote.cost_usd == pytest.approx(
        ((len(OLD_PROMPT.encode()) + 1024) * 3.75 + 50 * 15) / 1e6
    )
    # Without a marker the request (and its quote) is exactly as before.
    await adapter.complete(PLAIN)
    assert server.payloads[-1]["messages"] == [{"role": "user", "content": OLD_PROMPT}]
    assert adapter.quote(PLAIN).cost_usd == price.cost(len(OLD_PROMPT.encode()) + 1024, 50)


async def test_anthropic_prefix_equal_to_whole_prompt_is_one_block() -> None:
    server = CachingAnthropic()
    adapter = AnthropicAdapter(SONNET, api_key="k", transport=httpx.MockTransport(server))
    await adapter.complete(ModelRequest(prompt=FIXED, cache_prefix=FIXED))
    assert server.payloads[0]["messages"] == [
        {
            "role": "user",
            "content": [{"type": "text", "text": FIXED, "cache_control": {"type": "ephemeral"}}],
        }
    ]


PAGE = ImageInput.from_bytes(png_bytes(816, 1056))
CACHED_PAGE = ImageInput.from_bytes(png_bytes(816, 1056), cache=True)


async def test_prefix_cache_reads_survive_an_image_block() -> None:
    server = CachingAnthropic()
    adapter = AnthropicAdapter(SONNET, api_key="k", transport=httpx.MockTransport(server))
    with_image = ModelRequest(
        prompt=OLD_PROMPT, cache_prefix=FIXED, max_output_tokens=50, images=(PAGE,)
    )
    first = await adapter.complete(REQUEST)  # warms the prefix without any image
    second = await adapter.complete(with_image)
    third = await adapter.complete(with_image)
    content = server.payloads[1]["messages"][0]["content"]
    assert isinstance(content, list)
    assert [block["type"] for block in content if isinstance(block, dict)] == [
        "text", "image", "text"
    ]
    assert content[0] == {"type": "text", "text": FIXED, "cache_control": {"type": "ephemeral"}}
    assert content[1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": b64(PAGE)},
    }
    assert content[2] == {"type": "text", "text": VARIABLE}
    assert effective_anthropic_text(server.payloads[1]) == OLD_PROMPT
    # The prefix stays a byte-stable leading block, so it is read from cache
    # on every call that follows; the uncached image is billed each time.
    assert (first.cache_write_tokens, first.cache_read_tokens) == (len(FIXED), 0)
    assert (second.cache_write_tokens, second.cache_read_tokens) == (0, len(FIXED))
    assert (third.cache_write_tokens, third.cache_read_tokens) == (0, len(FIXED))
    assert second.tokens_in == third.tokens_in == len(OLD_PROMPT) + IMAGE_TOKENS
    assert second.cost_usd == pytest.approx(
        ((len(VARIABLE) + IMAGE_TOKENS) * 3 + len(FIXED) * 0.3 + 5 * 15) / 1e6
    )
    # The quote reserves the image at the cache-write rate and stays a ceiling.
    quote = adapter.quote(with_image)
    assert quote.tokens_in == len(OLD_PROMPT.encode()) + 1024 + 1600
    assert quote.cost_usd == pytest.approx((quote.tokens_in * 3.75 + 50 * 15) / 1e6)
    assert quote.cost_usd >= max(second.cost_usd, third.cost_usd)


async def test_image_is_cacheable_only_with_cache_control() -> None:
    server = CachingAnthropic()
    adapter = AnthropicAdapter(SONNET, api_key="k", transport=httpx.MockTransport(server))
    uncached = ModelRequest(prompt=OLD_PROMPT, cache_prefix=FIXED, images=(PAGE,))
    cached = ModelRequest(prompt=OLD_PROMPT, cache_prefix=FIXED, images=(CACHED_PAGE,))
    assert PAGE.data == CACHED_PAGE.data  # only the marker differs
    await adapter.complete(uncached)
    await adapter.complete(uncached)
    warm = await adapter.complete(cached)
    hit = await adapter.complete(cached)
    blocks = server.payloads[2]["messages"][0]["content"]
    assert isinstance(blocks, list) and isinstance(blocks[1], dict)
    assert blocks[1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in server.payloads[0]["messages"][0]["content"][1]  # type: ignore[index]
    # Two uncached sends never warmed the image; the marked send writes it once.
    assert (warm.cache_write_tokens, warm.cache_read_tokens) == (IMAGE_TOKENS, len(FIXED))
    assert (hit.cache_write_tokens, hit.cache_read_tokens) == (0, len(FIXED) + IMAGE_TOKENS)
    assert hit.cost_usd < warm.cost_usd
    assert adapter.quote(cached).cost_usd >= warm.cost_usd


async def test_fake_records_shapes_and_prices_images_identically_with_or_without_prefix() -> None:
    scripted = [ModelResponse(text="a", tokens_in=1, tokens_out=1, cost_usd=0, model="fake")]
    with_prefix, without = FakeAdapter(responses=scripted), FakeAdapter(responses=scripted)
    marked = ModelRequest(prompt=OLD_PROMPT, cache_prefix=FIXED, images=(PAGE,))
    plain = ModelRequest(prompt=OLD_PROMPT, images=(PAGE,))
    assert with_prefix.quote(marked) == without.quote(plain)
    assert with_prefix.quote(marked).tokens_in == 1 + 100
    assert with_prefix.quote(marked).cost_usd == pytest.approx(0.0001)
    assert await with_prefix.complete(marked) == await without.complete(plain)
    assert with_prefix.seen == without.seen
    shape = with_prefix.seen[0]
    assert (shape.prompt_bytes, shape.image_count, shape.image_bytes) == (
        len(OLD_PROMPT.encode()), 1, len(PAGE.data)
    )
    assert shape.sha256s == (PAGE.sha256,)


async def test_openai_keeps_one_user_message_and_prices_cached_tokens() -> None:
    seen: list[dict[str, JsonValue]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={
            "model": "gpt-4o-mini",
            "choices": [{"message": {"content": "ok"}}],
            "usage": {
                "prompt_tokens": 2000, "completion_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 1536},
            },
        })

    adapter = OpenAICompatibleAdapter(api_key="k", transport=httpx.MockTransport(handler))
    response = await adapter.complete(REQUEST)
    assert seen[0]["messages"] == [{"role": "user", "content": OLD_PROMPT}]
    assert response.cache_read_tokens == 1536
    assert response.cost_usd == pytest.approx((464 * 0.15 + 1536 * 0.075 + 10 * 0.6) / 1e6)
    assert adapter.quote(REQUEST) == adapter.quote(PLAIN)


async def test_ollama_raw_prompt_is_unchanged() -> None:
    seen: list[dict[str, JsonValue]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={
            "model": "llama3.2", "response": "ok", "done": True,
            "prompt_eval_count": 3, "eval_count": 1,
        })

    adapter = OllamaAdapter(transport=httpx.MockTransport(handler), raw=True)
    await adapter.complete(REQUEST)
    await adapter.complete(PLAIN)
    assert seen[0]["prompt"] == seen[1]["prompt"] == OLD_PROMPT
    assert seen[0] == seen[1]


def fake_cli(tmp_path: Path, stdout: str) -> Path:
    path = tmp_path / "cli"
    path.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"open({str(tmp_path / 'stdin.txt')!r}, 'w').write(sys.stdin.read())\n"
        "for i, a in enumerate(sys.argv):\n"
        "    if a == '-o':\n"
        "        open(sys.argv[i + 1], 'w').write('ok')\n"
        f"sys.stdout.write({stdout!r})\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake executables")
async def test_cli_adapters_send_the_whole_prompt_on_stdin(tmp_path: Path) -> None:
    haiku = "claude-haiku-4-5-20251001"
    claude_out = json.dumps({
        "type": "result", "subtype": "success", "is_error": False, "result": "ok",
        "total_cost_usd": 0.0,
        "usage": {"input_tokens": 10, "cache_creation_input_tokens": 100,
                  "cache_read_input_tokens": 900, "output_tokens": 4},
        "modelUsage": {haiku: {}},
    })
    claude = ClaudeCodeAdapter(haiku, executable=str(fake_cli(tmp_path, claude_out)))
    response = await claude.complete(REQUEST)
    assert (tmp_path / "stdin.txt").read_text(encoding="utf-8") == OLD_PROMPT
    assert response.tokens_in == 1010
    assert (response.cache_write_tokens, response.cache_read_tokens) == (100, 900)
    assert response.cost_usd == pytest.approx((10 * 1 + 100 * 1.25 + 900 * 0.1 + 4 * 5) / 1e6)
    assert claude.quote(REQUEST).cost_usd >= response.cost_usd

    codex_out = "\n".join(json.dumps(event) for event in [
        {"type": "turn.completed",
         "usage": {"input_tokens": 1200, "cached_input_tokens": 1000, "output_tokens": 3}},
    ]) + "\n"
    codex = CodexAdapter(executable=str(fake_cli(tmp_path, codex_out)))
    response = await codex.complete(REQUEST)
    assert (tmp_path / "stdin.txt").read_text(encoding="utf-8") == OLD_PROMPT
    assert (response.tokens_in, response.cache_read_tokens) == (1200, 1000)


async def test_fake_fixture_responses_are_unaffected() -> None:
    scripted = [ModelResponse(text="a", tokens_in=1, tokens_out=1, cost_usd=0, model="fake")]
    with_prefix, without = FakeAdapter(responses=scripted), FakeAdapter(responses=scripted)
    assert with_prefix.quote(REQUEST) == without.quote(PLAIN)
    assert await with_prefix.complete(REQUEST) == await without.complete(PLAIN)


class Recording(FakeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return await super().complete(request)


async def test_context_call_system_is_backward_compatible(tmp_path: Path) -> None:
    adapter = Recording()

    async def harness(context: RunContext, task: JsonValue) -> JsonValue:
        await context.call(VARIABLE, system=FIXED)
        await context.call(OLD_PROMPT)
        await context.call(OLD_PROMPT, None, system="")
        return None

    await Runtime(TelemetryStore(tmp_path / "t")).evaluate(Spec(
        task="t", input_schema={}, output_schema={},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="d"),
        budget=Budget(),
    ), harness, None, adapter)
    assert [request.prompt for request in adapter.requests] == [OLD_PROMPT] * 3
    assert [request.cache_prefix for request in adapter.requests] == [FIXED, None, None]


async def test_repeated_calls_reuse_the_cache_and_telemetry_counts_it(tmp_path: Path) -> None:
    """Offline measurement: ten loop round-trips over one fixed instruction prefix."""
    spec = Spec(
        task="t", input_schema={}, output_schema={},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="d"),
        budget=Budget(cost_usd=10), model_target=SONNET,
    )
    costs: dict[bool, float] = {}
    rows: dict[bool, TelemetryRow] = {}
    for cached in (False, True):
        store = TelemetryStore(tmp_path / str(cached))
        adapter = AnthropicAdapter(
            SONNET, api_key="k", transport=httpx.MockTransport(CachingAnthropic())
        )

        async def harness(context: RunContext, task: JsonValue, cached: bool = cached) -> JsonValue:
            for step in range(10):
                data = "\nStep data (untrusted):\n" + json.dumps({"step": step})
                if cached:
                    await context.call(data, 50, system=FIXED)
                else:
                    await context.call(FIXED + data, 50)
            return None

        result = await Runtime(store).evaluate(spec, harness, None, adapter)
        costs[cached] = result.cost_usd
        rows[cached] = store.rows()[0]
    assert rows[False].model_calls == rows[True].model_calls == 10
    assert rows[False].tokens_in == rows[True].tokens_in
    assert rows[False].cached_tokens_in == 0
    assert rows[True].cached_tokens_in == 9 * len(FIXED)
    assert rows[True].cost_usd == pytest.approx(costs[True])
    # One 1.25x write plus nine 0.1x reads versus ten full-price prefixes.
    assert costs[True] < 0.3 * costs[False]


def test_old_telemetry_rows_read_with_zero_counters() -> None:
    old = {
        "run_id": "00000000-0000-4000-8000-000000000000",
        "timestamp": "2026-01-01T00:00:00Z", "domain": "general", "task_type": "toy",
        "spec_hash": "a" * 64, "harness_version": 1, "lifecycle": "expire",
        "preset_origin": "toy", "component_set": [], "model": "fake", "model_params": {},
        "tokens_in": 1, "tokens_out": 1, "cost_usd": 0.0, "wall_time_s": 0.0,
        "floor_type": "held_out_tasks", "floor_score": None, "floor_limit": 1.0,
        "accepted": True,
    }
    row = TelemetryRow.model_validate_json(json.dumps(old))
    assert (row.model_calls, row.cached_tokens_in) == (0, 0)
    for field in ("model_calls", "cached_tokens_in"):
        with pytest.raises(ValidationError):
            TelemetryRow.model_validate({**old, field: -1})
        with pytest.raises(ValidationError):
            TelemetryRow.model_validate({**old, field: "1"})


def candidate(*, passed: bool, cost: float, version: int) -> SimpleNamespace:
    measurement = Measurement(
        score=1.0 if passed else 0.0, floor_passed=passed, budget_passed=True,
        mean_cost_usd=cost, mean_tokens=100, max_tokens=100, max_cost_usd=cost,
        max_compute_s=0.1, sample_count=1,
    )
    artifact = SimpleNamespace(
        params={}, component_count=1, version=version,
        spec=SimpleNamespace(floor=SimpleNamespace(kind=FloorKind.HELD_OUT_TASKS)),
    )
    return SimpleNamespace(measurement=measurement, artifact=artifact)


def test_measured_cache_savings_only_break_ties_among_passing_candidates() -> None:
    uncached = candidate(passed=True, cost=0.010, version=1)
    cached = candidate(passed=True, cost=0.002, version=2)
    failing = candidate(passed=False, cost=0.0, version=3)
    ranked = sorted([failing, uncached, cached], key=_rank)  # type: ignore[arg-type]
    assert ranked == [cached, uncached, failing]
