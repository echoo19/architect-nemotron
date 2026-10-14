"""Offline adapter contract tests. All HTTP uses MockTransport."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from image_support import jpeg_bytes, png_bytes
from pydantic import JsonValue, ValidationError

from architect.models import (
    AdapterError,
    AnthropicAdapter,
    FakeAdapter,
    ModelRequest,
    ModelResponse,
    OllamaAdapter,
    OpenAICompatibleAdapter,
    UsageQuote,
)
from architect.models import http as http_module
from architect.models.fake import RequestShape
from architect.models.http import OLLAMA_IMAGE_TIMEOUT_S, REASONING_CUT
from architect.models.images import ImageInput, b64, data_url
from architect.models.pricing import load_prices, price_for

# Request bodies captured from the adapters before image support existed; a
# text-only request must still produce exactly these bytes.
CAPTURED_ANTHROPIC = (
    b'{"model":"claude-sonnet-4-20250514","messages":[{"role":"user","content":"hi"}],'
    b'"max_tokens":20,"temperature":0.0,"stream":false}'
)
CAPTURED_ANTHROPIC_PREFIX = (
    b'{"model":"claude-sonnet-4-20250514","messages":[{"role":"user","content":'
    b'[{"type":"text","text":"fixed ","cache_control":{"type":"ephemeral"}},'
    b'{"type":"text","text":"rest"}]}],"max_tokens":20,"temperature":0.0,"stream":false}'
)
CAPTURED_OPENAI = (
    b'{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}],'
    b'"temperature":0.0,"stream":false,"n":1,"max_tokens":8}'
)
CAPTURED_OLLAMA = (
    b'{"model":"llama3.2","prompt":"hi","raw":true,"stream":false,'
    b'"options":{"temperature":0.5,"num_predict":12}}'
)
PAGE = ImageInput.from_bytes(png_bytes(816, 1056))
PHOTO = ImageInput.from_bytes(jpeg_bytes(640, 480), cache=True)


def scripted(text: str = "first") -> ModelResponse:
    return ModelResponse(text=text, tokens_in=10, tokens_out=2, cost_usd=0.004, model="fake")


async def test_fake_sequence_and_quote_do_not_consume() -> None:
    adapter = FakeAdapter(responses=[scripted(), scripted("second")])
    request = ModelRequest(prompt="unlogged input")
    first = adapter.quote(request)
    assert first == adapter.quote(request)
    assert first == UsageQuote(tokens_in=10, tokens_out=2, cost_usd=0.004)
    assert adapter.calls == 0
    assert (await adapter.complete(request)).text == "first"
    assert (await adapter.complete(request)).text == "second"
    with pytest.raises(AdapterError, match="exhausted"):
        adapter.quote(request)
    assert adapter.calls == 2
    with pytest.raises(AdapterError, match="exhausted"):
        await adapter.complete(request)
    assert adapter.calls == 3


async def test_fake_default_and_fixture(tmp_path: Path) -> None:
    request = ModelRequest(prompt="hi")
    default = FakeAdapter()
    assert await default.complete(request) == await default.complete(request)
    assert (await default.complete(request)).text == "ok"
    path = tmp_path / "responses.json"
    path.write_text(json.dumps([scripted().model_dump()]), encoding="utf-8")
    adapter = FakeAdapter.from_fixture(path)
    assert await adapter.complete(request) == scripted()
    with pytest.raises(AdapterError):
        FakeAdapter(responses=[], fixture=path)
    with pytest.raises(AdapterError):
        FakeAdapter(fixture=tmp_path / "absent")
    path.write_text('[{"text": "invalid"}]', encoding="utf-8")
    with pytest.raises(AdapterError):
        FakeAdapter(fixture=path)
    with pytest.raises(AdapterError):
        FakeAdapter(responses=[scripted().model_copy(update={"model": "other"})])
    with pytest.raises(AdapterError):
        FakeAdapter(responses=[]).quote(request)


@pytest.mark.parametrize(
    "data",
    [
        {"prompt": "", "max_output_tokens": 0},
        {"prompt": "", "max_output_tokens": True},
        {"prompt": "", "max_output_tokens": 1.5},
        {"prompt": "", "temperature": float("nan")},
        {"prompt": "", "temperature": -1},
        {"prompt": "", "extra": "no"},
    ],
)
def test_request_validation(data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ModelRequest.model_validate(data)


@pytest.mark.parametrize("bad", [-1, float("inf"), float("nan")])
def test_usage_validation(bad: float) -> None:
    with pytest.raises(ValidationError):
        UsageQuote(tokens_in=0, tokens_out=0, cost_usd=bad)
    with pytest.raises(ValidationError):
        ModelResponse(text="", model="fake", tokens_in=0, tokens_out=0, cost_usd=bad)


def price_file(tmp_path: Path) -> Path:
    path = tmp_path / "prices.json"
    path.write_text(
        json.dumps(
            {
                "models": {
                    "custom": {
                        "input_per_million": 2.0,
                        "output_per_million": 4.0,
                        "input_overhead_tokens": 12,
                        "output_capacity_tokens": 2048,
                    }
                },
                "fake_usage": {"tokens_in": 3, "tokens_out": 1},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_unknown_prices_rejected_and_defaults_configurable(tmp_path: Path) -> None:
    for adapter_type in (FakeAdapter, AnthropicAdapter, OpenAICompatibleAdapter, OllamaAdapter):
        with pytest.raises(AdapterError, match="price"):
            adapter_type(model="unknown")
    path = price_file(tmp_path)
    adapter = OpenAICompatibleAdapter(model="custom", prices_path=path)
    request = ModelRequest(prompt="é漢🙂", max_output_tokens=9)
    quote = adapter.quote(request)
    assert quote.tokens_in == len(request.prompt.encode("utf-8")) + 12
    assert quote.tokens_out == 9
    assert quote.cost_usd == pytest.approx((quote.tokens_in * 2 + 9 * 4) / 1_000_000)
    fake = FakeAdapter(model="custom", prices_path=path)
    assert fake.quote(request) == UsageQuote(tokens_in=3, tokens_out=1, cost_usd=0.00001)
    path.write_text('{"models": {}}', encoding="utf-8")
    with pytest.raises(AdapterError):
        FakeAdapter(prices_path=path)


async def test_anthropic_request_and_actual_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.anthropic.com/v1/messages"
        assert request.headers["x-api-key"] == "test-key"
        assert request.headers["anthropic-version"] == "2023-06-01"
        payload = json.loads(request.content)
        assert payload["max_tokens"] == 20
        assert payload["messages"] == [{"role": "user", "content": "hi"}]
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
                "usage": {"input_tokens": 4, "output_tokens": 2},
            },
        )

    adapter = AnthropicAdapter(transport=httpx.MockTransport(handler))
    request = ModelRequest(prompt="hi", max_output_tokens=20)
    quote = adapter.quote(request)
    response = await adapter.complete(request)
    assert response.text == "ab"
    assert (response.tokens_in, response.tokens_out) == (4, 2)
    assert response.cost_usd == pytest.approx(0.000042)
    assert quote.tokens_in >= response.tokens_in
    assert quote.tokens_out >= response.tokens_out
    assert quote.cost_usd >= response.cost_usd


async def test_openai_request_and_actual_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://local.invalid/v1/")

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://local.invalid/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-key"
        payload = json.loads(request.content)
        assert payload["max_tokens"] == 8
        assert payload["stream"] is False
        assert payload["n"] == 1
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    adapter = OpenAICompatibleAdapter(transport=httpx.MockTransport(handler))
    response = await adapter.complete(ModelRequest(prompt="hi", max_output_tokens=8))
    assert response.text == "ok"
    assert response.tokens_in == 5
    assert response.tokens_out == 2
    assert response.cost_usd == pytest.approx(0.00000195)


async def test_ollama_request_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_HOST", "http://local.invalid:11434")

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://local.invalid:11434/api/generate"
        payload = json.loads(request.content)
        assert payload["raw"] is True
        assert payload["options"] == {"num_predict": 12, "temperature": 0.5}
        return httpx.Response(
            200,
            json={
                "response": "ok",
                "done": True,
                "prompt_eval_count": 6,
                "eval_count": 1,
            },
        )

    adapter = OllamaAdapter(transport=httpx.MockTransport(handler), raw=True)
    response = await adapter.complete(
        ModelRequest(prompt="hi", max_output_tokens=12, temperature=0.5)
    )
    assert (response.tokens_in, response.tokens_out, response.cost_usd) == (6, 1, 0.0)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        {"choices": [{"message": {"content": "ok"}}]},
        {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": True, "completion_tokens": 1},
        },
        {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": -1},
        },
        {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1.5, "completion_tokens": 1},
        },
    ],
)
async def test_missing_or_invalid_usage_never_estimated(payload: dict[str, Any]) -> None:
    adapter = OpenAICompatibleAdapter(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    )
    with pytest.raises(AdapterError):
        await adapter.complete(ModelRequest(prompt="hi"))


@pytest.mark.parametrize("status", [301, 401, 429, 500])
async def test_http_failure(status: int) -> None:
    adapter = OpenAICompatibleAdapter(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, text="private server detail")
        )
    )
    with pytest.raises(AdapterError, match="Provider request failed") as caught:
        await adapter.complete(ModelRequest(prompt="private prompt"))
    assert "private" not in str(caught.value)


async def test_invalid_json_and_timeout() -> None:
    adapter = OllamaAdapter(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="not json"))
    )
    with pytest.raises(AdapterError):
        await adapter.complete(ModelRequest(prompt="hi"))

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    adapter = OllamaAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(AdapterError):
        await adapter.complete(ModelRequest(prompt="hi"))


def test_missing_anthropic_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(AdapterError, match="ANTHROPIC_API_KEY"):
        AnthropicAdapter()


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_timeout(timeout: float) -> None:
    with pytest.raises(AdapterError):
        OllamaAdapter(timeout=timeout)


@pytest.mark.parametrize("provider", ["anthropic", "openai", "ollama"])
@pytest.mark.parametrize("reported_model", ["unapproved-model", "", None, 42])
async def test_reported_model_substitution_rejected(provider: str, reported_model: Any) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"model": reported_model})
    )
    adapter: AnthropicAdapter | OpenAICompatibleAdapter | OllamaAdapter
    if provider == "anthropic":
        adapter = AnthropicAdapter(api_key="test-key", transport=transport)
    elif provider == "openai":
        adapter = OpenAICompatibleAdapter(transport=transport)
    else:
        adapter = OllamaAdapter(transport=transport)
    with pytest.raises(AdapterError):
        await adapter.complete(ModelRequest(prompt="hi"))


@pytest.mark.parametrize("reported_model", ["custom", "custom-snapshot"])
async def test_reported_model_exact_or_configured_alias(
    tmp_path: Path, reported_model: str
) -> None:
    path = price_file(tmp_path)
    prices = json.loads(path.read_text(encoding="utf-8"))
    prices["models"]["custom"]["allowed_response_models"] = ["custom-snapshot"]
    path.write_text(json.dumps(prices), encoding="utf-8")
    adapter = OpenAICompatibleAdapter(
        model="custom",
        prices_path=path,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "model": reported_model,
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1},
                },
            )
        ),
    )
    response = await adapter.complete(ModelRequest(prompt="hi"))
    assert response.model == "custom"
    assert response.cost_usd == pytest.approx(0.00001)


async def test_unconfigured_snapshot_alias_rejected(tmp_path: Path) -> None:
    adapter = OpenAICompatibleAdapter(
        model="custom",
        prices_path=price_file(tmp_path),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"model": "custom-snapshot"})
        ),
    )
    with pytest.raises(AdapterError, match="model does not match"):
        await adapter.complete(ModelRequest(prompt="hi"))


def test_request_output_cap_is_optional_provider_option() -> None:
    assert ModelRequest(prompt="hi").max_output_tokens is None
    assert ModelRequest(prompt="hi", max_output_tokens=None).max_output_tokens is None
    assert ModelRequest(prompt="hi", max_output_tokens=7).max_output_tokens == 7


@pytest.mark.parametrize("bad", [None, 0, -1, True, 1.5, "2048"])
def test_invalid_provider_capacity_rejected(tmp_path: Path, bad: Any) -> None:
    path = price_file(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["models"]["custom"]["output_capacity_tokens"] = bad
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(AdapterError, match="price configuration"):
        OpenAICompatibleAdapter(model="custom", prices_path=path)


def test_missing_provider_capacity_has_no_arbitrary_fallback(tmp_path: Path) -> None:
    path = price_file(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["models"]["custom"]["output_capacity_tokens"]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(AdapterError, match="price configuration"):
        OpenAICompatibleAdapter(model="custom", prices_path=path)


@pytest.mark.parametrize("provider", ["anthropic", "openai", "ollama"])
async def test_default_capacity_quote_and_protocol(
    tmp_path: Path, provider: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if provider == "anthropic":
            assert payload["max_tokens"] == 2048
            data = {
                "model": "custom",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 5, "output_tokens": 200},
            }
        elif provider == "openai":
            assert "max_tokens" not in payload
            assert "max_completion_tokens" not in payload
            data = {
                "model": "custom",
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 200},
            }
        else:
            assert "num_predict" not in payload["options"]
            assert payload["options"]["num_ctx"] == 32_768
            data = {
                "model": "custom",
                "message": {"role": "assistant", "content": "ok"},
                "done": True,
                "prompt_eval_count": 5,
                "eval_count": 200,
            }
        return httpx.Response(200, json=data)

    adapter_type = {
        "anthropic": AnthropicAdapter,
        "openai": OpenAICompatibleAdapter,
        "ollama": OllamaAdapter,
    }[provider]
    adapter = adapter_type(
        model="custom", api_key="test", prices_path=price_file(tmp_path),
        transport=httpx.MockTransport(handler),
    )
    request = ModelRequest(prompt="hi")
    quote = adapter.quote(request)
    assert quote.tokens_out == 2048
    assert quote.cost_usd == pytest.approx((14 * 2 + 2048 * 4) / 1_000_000)
    response = await adapter.complete(request)
    assert response.tokens_out == 200  # No former 128-token factory clipping.
    assert response.cost_usd <= quote.cost_usd


async def test_actual_usage_exceeding_token_quote_is_retained(tmp_path: Path) -> None:
    path = price_file(tmp_path)
    # A free provider can exceed token estimates without exceeding dollar safety.
    data = json.loads(path.read_text(encoding="utf-8"))
    data["models"]["custom"].update(input_per_million=0, output_per_million=0)
    path.write_text(json.dumps(data), encoding="utf-8")
    adapter = OpenAICompatibleAdapter(
        model="custom", prices_path=path,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={
                "model": "custom",
                "choices": [{"message": {"content": "complete"}}],
                "usage": {"prompt_tokens": 10000, "completion_tokens": 3000000},
            })
        ),
    )
    request = ModelRequest(prompt="hi")
    quote = adapter.quote(request)
    response = await adapter.complete(request)
    assert response.tokens_in == 10000 > quote.tokens_in
    assert response.tokens_out == 3000000 > quote.tokens_out
    assert response.cost_usd == quote.cost_usd == 0


async def test_fake_large_fixture_usage_is_not_clipped(tmp_path: Path) -> None:
    response = ModelResponse(
        text="complete", model="fake", tokens_in=10000, tokens_out=3000000, cost_usd=0
    )
    path = tmp_path / "responses.json"
    path.write_text(json.dumps([response.model_dump()]), encoding="utf-8")
    adapter = FakeAdapter.from_fixture(path)
    request = ModelRequest(prompt="hi", max_output_tokens=1)
    assert adapter.quote(request).tokens_out == 3000000
    assert await adapter.complete(request) == response


# --- Image inputs -----------------------------------------------------------


def never_called(request: httpx.Request) -> httpx.Response:
    raise AssertionError("transport must not be reached")


async def test_anthropic_text_only_body_is_byte_identical_to_capture() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 4, "output_tokens": 2},
            },
        )

    adapter = AnthropicAdapter(api_key="k", transport=httpx.MockTransport(handler))
    await adapter.complete(ModelRequest(prompt="hi", max_output_tokens=20))
    await adapter.complete(
        ModelRequest(prompt="fixed rest", cache_prefix="fixed ", max_output_tokens=20)
    )
    assert bodies == [CAPTURED_ANTHROPIC, CAPTURED_ANTHROPIC_PREFIX]


async def test_anthropic_image_blocks_follow_the_cached_prefix() -> None:
    payloads: list[dict[str, Any]] = []
    image_tokens = 1149 + 410  # what the provider counts for the page and the photo

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "claude-sonnet-4-20250514",
                "content": [{"type": "text", "text": "seen"}],
                "usage": {
                    "input_tokens": 12 + image_tokens,
                    "cache_creation_input_tokens": 6,
                    "output_tokens": 3,
                },
            },
        )

    adapter = AnthropicAdapter(api_key="k", transport=httpx.MockTransport(handler))
    request = ModelRequest(
        prompt="fixed rest", cache_prefix="fixed ", max_output_tokens=20, images=(PAGE, PHOTO)
    )
    quote = adapter.quote(request)
    price = price_for(load_prices(), "claude-sonnet-4-20250514")
    assert quote.tokens_in == len(b"fixed rest") + 1024 + 2 * 1600
    assert quote.cost_usd == pytest.approx((quote.tokens_in * 3.75 + 20 * 15) / 1e6)
    response = await adapter.complete(request)
    assert payloads[0]["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "fixed ", "cache_control": {"type": "ephemeral"}},
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": b64(PAGE)},
                },
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": b64(PHOTO)},
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": "rest"},
            ],
        }
    ]
    assert response.tokens_in == 18 + image_tokens
    assert response.cost_usd == pytest.approx(
        price.cost(18 + image_tokens, 3, cache_write_tokens=6)
    )
    assert quote.tokens_in >= response.tokens_in
    assert quote.cost_usd >= response.cost_usd
    # Without a prefix the images lead and the whole prompt is the trailing text block.
    await adapter.complete(ModelRequest(prompt="hi", images=(PAGE,)))
    assert [block["type"] for block in payloads[1]["messages"][0]["content"]] == ["image", "text"]
    assert payloads[1]["messages"][0]["content"][1] == {"type": "text", "text": "hi"}
    await adapter.complete(ModelRequest(prompt="", images=(PAGE,)))
    assert [block["type"] for block in payloads[2]["messages"][0]["content"]] == ["image"]


CLAUDE_IMAGE_MODELS = (
    "claude-sonnet-4-20250514",
    "claude-haiku-4-5-20251001",
    "claude-sonnet-5",
    "claude-opus-5-5",
    "claude-fable-5-1",
)


@pytest.mark.parametrize("model", CLAUDE_IMAGE_MODELS)
async def test_anthropic_cached_image_without_prefix_reserves_the_cache_write_rate(
    model: str,
) -> None:
    # A ``cache=True`` image block carries ``cache_control`` on its own, so the
    # provider may bill every input token up to it as a cache write even when
    # no ``cache_prefix`` is set. The quote must stay an upper bound.
    cached_page = ImageInput.from_bytes(png_bytes(816, 1056), cache=True)
    request = ModelRequest(prompt="x" * 100, max_output_tokens=50, images=(cached_page,) * 3)
    adapter = AnthropicAdapter(
        model=model, api_key="k", transport=httpx.MockTransport(never_called)
    )
    quote = adapter.quote(request)
    price = price_for(load_prices(), model)
    assert price.cache_write_per_million is not None
    assert price.cache_write_per_million > price.input_per_million
    assert quote.cost_usd == pytest.approx(
        price.quote_cost(quote.tokens_in, 50, may_write_cache=True)
    )
    usages = (
        {"input_tokens": 30, "cache_creation_input_tokens": 4780, "output_tokens": 50},
        {"input_tokens": 0, "cache_creation_input_tokens": quote.tokens_in, "output_tokens": 50},
    )
    payloads: list[dict[str, Any]] = []
    for usage in usages:

        def handler(http_request: httpx.Request, usage: dict[str, int] = usage) -> httpx.Response:
            payloads.append(json.loads(http_request.content))
            return httpx.Response(
                200, json={"content": [{"type": "text", "text": "ok"}], "usage": usage}
            )

        live = AnthropicAdapter(model=model, api_key="k", transport=httpx.MockTransport(handler))
        response = await live.complete(request)
        blocks = payloads[-1]["messages"][0]["content"]
        assert all(block.get("cache_control") for block in blocks if block["type"] == "image")
        assert quote.tokens_in >= response.tokens_in
        assert quote.cost_usd >= response.cost_usd
    # At the token ceiling the whole reservation is spent as a cache write.
    assert quote.cost_usd == pytest.approx(response.cost_usd)
    # Uncached images and the non-writing providers keep the plain input rate.
    plain = ModelRequest(prompt="x" * 100, max_output_tokens=50, images=(PAGE,) * 3)
    plain_quote = adapter.quote(plain)
    assert plain_quote.cost_usd == pytest.approx(price.cost(plain_quote.tokens_in, 50))
    openai = OpenAICompatibleAdapter(api_key="k", transport=httpx.MockTransport(never_called))
    openai_price = price_for(load_prices(), "gpt-4o-mini")
    openai_quote = openai.quote(request)
    assert openai_quote.cost_usd == pytest.approx(openai_price.cost(openai_quote.tokens_in, 50))


async def test_openai_text_only_body_is_byte_identical_to_capture() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    adapter = OpenAICompatibleAdapter(api_key="k", transport=httpx.MockTransport(handler))
    await adapter.complete(ModelRequest(prompt="hi", max_output_tokens=8))
    assert bodies == [CAPTURED_OPENAI]


async def test_openai_image_parts_carry_data_url_and_detail() -> None:
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "gpt-4o-mini",
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 5 + 25497, "completion_tokens": 2},
            },
        )

    adapter = OpenAICompatibleAdapter(api_key="k", transport=httpx.MockTransport(handler))
    request = ModelRequest(prompt="hi", max_output_tokens=8, images=(PAGE,), image_detail="high")
    quote = adapter.quote(request)
    assert quote.tokens_in == 2 + 1024 + 48170
    response = await adapter.complete(request)
    assert payloads[0]["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "image_url", "image_url": {"url": data_url(PAGE), "detail": "high"}},
            ],
        }
    ]
    assert payloads[0]["messages"][0]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert response.tokens_in == 25502
    assert quote.tokens_in >= response.tokens_in
    assert quote.cost_usd >= response.cost_usd
    await adapter.complete(ModelRequest(prompt="hi", images=(PAGE, PHOTO)))
    parts = payloads[1]["messages"][0]["content"]
    assert [part["type"] for part in parts] == ["text", "image_url", "image_url"]
    assert {part["image_url"]["detail"] for part in parts[1:]} == {"auto"}


async def test_ollama_text_only_stays_raw_generate_and_images_use_chat() -> None:
    seen: list[tuple[str, bytes, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.content, request.extensions["timeout"]["read"]))
        if request.url.path == "/api/generate":
            return httpx.Response(
                200,
                json={"model": "llama3.2-vision", "response": "ok", "done": True,
                      "prompt_eval_count": 6, "eval_count": 1},
            )
        return httpx.Response(
            200,
            json={
                "model": "llama3.2-vision:latest",
                "message": {"role": "assistant", "content": "page 3"},
                "done": True,
                "prompt_eval_count": 6 + 1601,
                "eval_count": 2,
            },
        )

    adapter = OllamaAdapter(
        "llama3.2-vision", base_url="http://local.invalid:11434",
        transport=httpx.MockTransport(handler), raw=True,
    )
    text = ModelRequest(prompt="hi", max_output_tokens=12, temperature=0.5)
    await adapter.complete(text)
    assert seen[0][0] == "http://local.invalid:11434/api/generate"
    assert seen[0][1] == CAPTURED_OLLAMA.replace(b'"llama3.2"', b'"llama3.2-vision"')
    assert seen[0][2] == 60.0
    with_image = ModelRequest(prompt="hi", max_output_tokens=12, temperature=0.5, images=(PAGE,))
    quote = adapter.quote(with_image)
    assert quote.tokens_in == 2 + 1024 + 1600
    response = await adapter.complete(with_image)
    assert seen[1][0] == "http://local.invalid:11434/api/chat"
    assert json.loads(seen[1][1]) == {
        "model": "llama3.2-vision",
        "messages": [{"role": "user", "content": "hi", "images": [b64(PAGE)]}],
        "stream": False,
        "truncate": False,
        "options": {"temperature": 0.5, "num_predict": 12, "num_ctx": 32_768},
    }
    assert "data:" not in seen[1][1].decode()
    assert seen[1][2] == OLLAMA_IMAGE_TIMEOUT_S == 180.0
    assert response.text == "page 3"
    assert (response.tokens_in, response.tokens_out, response.cost_usd) == (1607, 2, 0.0)
    assert quote.cost_usd >= response.cost_usd
    # A longer configured timeout is kept; a shorter one is raised to the image floor.
    longer = OllamaAdapter(
        "llama3.2-vision", base_url="http://local.invalid:11434",
        transport=httpx.MockTransport(handler), timeout=300.0,
    )
    await longer.complete(with_image)
    assert seen[2][2] == 300.0


@pytest.mark.parametrize(
    "body",
    [
        {"message": {"content": "x"}, "done": False, "prompt_eval_count": 1, "eval_count": 1},
        {"done": True, "prompt_eval_count": 1, "eval_count": 1},
        {"message": {"content": 5}, "done": True, "prompt_eval_count": 1, "eval_count": 1},
        {"message": {"content": "x"}, "done": True, "eval_count": 1},
        {"message": "x", "done": True, "prompt_eval_count": 1, "eval_count": 1},
    ],
)
async def test_ollama_chat_fails_closed_on_unexpected_shapes(body: dict[str, Any]) -> None:
    adapter = OllamaAdapter(
        "llama3.2-vision",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
    )
    with pytest.raises(AdapterError):
        await adapter.complete(ModelRequest(prompt="hi", images=(PAGE,)))


@pytest.mark.parametrize("provider", ["anthropic", "openai", "ollama", "fake"])
async def test_model_without_image_price_refuses_before_any_io(
    tmp_path: Path, provider: str
) -> None:
    transport = httpx.MockTransport(never_called)
    adapter: AnthropicAdapter | OpenAICompatibleAdapter | OllamaAdapter | FakeAdapter
    if provider == "anthropic":
        adapter = AnthropicAdapter(
            model="custom", api_key="k", prices_path=price_file(tmp_path), transport=transport
        )
    elif provider == "openai":
        adapter = OpenAICompatibleAdapter(
            model="custom", prices_path=price_file(tmp_path), transport=transport
        )
    elif provider == "ollama":
        adapter = OllamaAdapter(model="llama3.2", transport=transport)  # bundled text model
    else:
        adapter = FakeAdapter(model="custom", prices_path=price_file(tmp_path))
    request = ModelRequest(prompt="hi", images=(PAGE,))
    with pytest.raises(AdapterError, match="Model is not configured for image input"):
        adapter.quote(request)
    with pytest.raises(AdapterError, match="Model is not configured for image input"):
        await adapter.complete(request)
    if isinstance(adapter, FakeAdapter):
        assert adapter.seen == []
    # Text-only requests on the same adapter are unaffected.
    assert adapter.quote(ModelRequest(prompt="hi")).tokens_in > 0


async def test_fake_image_refusal_records_no_call_and_keeps_the_script(tmp_path: Path) -> None:
    # ``complete`` refuses images before counting the call: no phantom call,
    # no consumed scripted response, nothing in ``seen``.
    script = [
        ModelResponse(text=text, tokens_in=3, tokens_out=1, cost_usd=0.0, model="custom")
        for text in ("a", "b")
    ]
    unconfigured = FakeAdapter(model="custom", responses=script, prices_path=price_file(tmp_path))
    with pytest.raises(AdapterError, match="Model is not configured for image input"):
        await unconfigured.complete(ModelRequest(prompt="x", images=(PAGE,)))
    assert (unconfigured.calls, unconfigured.seen) == (0, [])
    assert (await unconfigured.complete(ModelRequest(prompt="x"))).text == "a"
    assert unconfigured.calls == 1
    path = price_file(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["models"]["fake"] = {
        "input_per_million": 1.0,
        "output_per_million": 1.0,
        "input_overhead_tokens": 0,
        "output_capacity_tokens": 16,
        "image": {
            "tokens_per_image_max": 5,
            "max_bytes": 100,
            "max_images": 1,
            "basis": "assumption",
        },
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    tiny = ImageInput.from_bytes(png_bytes(1, 1))
    assert len(tiny.data) <= 100 < len(PAGE.data)
    capped = FakeAdapter(responses=[scripted(), scripted("second")], prices_path=path)
    with pytest.raises(AdapterError, match="Too many images for model"):
        await capped.complete(ModelRequest(prompt="x", images=(tiny, tiny)))
    small = FakeAdapter(responses=[scripted(), scripted("second")], prices_path=path)
    with pytest.raises(AdapterError, match="Image exceeds model byte limit"):
        await small.complete(ModelRequest(prompt="x", images=(PAGE,)))
    for adapter in (capped, small):
        assert (adapter.calls, adapter.seen) == (0, [])
        assert (await adapter.complete(ModelRequest(prompt="x"))).text == "first"
        assert adapter.calls == 1


async def test_per_model_caps_apply_in_quote(tmp_path: Path) -> None:
    adapter = OpenAICompatibleAdapter(api_key="k", transport=httpx.MockTransport(never_called))
    with pytest.raises(AdapterError, match="Too many images for model"):
        adapter.quote(ModelRequest(prompt="hi", images=(PAGE,) * 21))
    path = price_file(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["models"]["custom"]["image"] = {
        "tokens_per_image_max": 5, "max_bytes": 100, "basis": "assumption"
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    small = AnthropicAdapter(
        model="custom", api_key="k", prices_path=path, transport=httpx.MockTransport(never_called)
    )
    assert len(PAGE.data) > 100 >= len(PHOTO.data)
    with pytest.raises(AdapterError, match="Image exceeds model byte limit"):
        small.quote(ModelRequest(prompt="hi", images=(PHOTO, PAGE)))
    assert small.quote(ModelRequest(prompt="hi", images=(PHOTO,))).tokens_in == 2 + 12 + 5
    tiny = ImageInput.from_bytes(png_bytes(1, 1))
    assert small.quote(ModelRequest(prompt="hi", images=(tiny,))).tokens_in == 2 + 12 + 5


async def test_fake_prices_images_at_the_ceiling_and_records_shapes() -> None:
    adapter = FakeAdapter()
    text = ModelRequest(prompt="hi")
    with_images = ModelRequest(prompt="hi", images=(PAGE, PHOTO))
    plain = adapter.quote(text)
    quote = adapter.quote(with_images)
    assert quote.tokens_in == plain.tokens_in + 200
    assert quote.tokens_out == plain.tokens_out
    assert quote.cost_usd == pytest.approx(plain.cost_usd + 0.0002)
    response = await adapter.complete(with_images)
    assert (response.tokens_in, response.tokens_out) == (quote.tokens_in, quote.tokens_out)
    assert response.cost_usd == pytest.approx(quote.cost_usd)
    assert response.text == "ok"
    assert await adapter.complete(text) == FakeAdapter()._default
    assert adapter.seen == [
        RequestShape(
            prompt_bytes=2,
            image_count=2,
            image_bytes=len(PAGE.data) + len(PHOTO.data),
            sha256s=(PAGE.sha256, PHOTO.sha256),
        ),
        RequestShape(prompt_bytes=2, image_count=0, image_bytes=0, sha256s=()),
    ]
    assert "data" not in RequestShape.model_fields
    with pytest.raises(ValidationError):
        RequestShape.model_validate({"prompt_bytes": 1, "image_count": 0, "image_bytes": 0,
                                     "sha256s": [], "prompt": "leak"})
    # Scripted responses are raised by exactly the reservation, never clipped.
    scripted_adapter = FakeAdapter(responses=[scripted()])
    scripted_quote = scripted_adapter.quote(with_images)
    assert (scripted_quote.tokens_in, scripted_quote.tokens_out) == (210, 2)
    assert scripted_quote.cost_usd == pytest.approx(0.0042)
    priced = await scripted_adapter.complete(with_images)
    assert (priced.text, priced.tokens_in, priced.cost_usd) == ("first", 210, pytest.approx(0.0042))


async def test_ollama_chat_is_the_default_with_system_prefix_context_and_think() -> None:
    """Instruction-tuned local models need their chat template and a real context size."""
    seen: list[dict[str, JsonValue]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={
            "model": "llama3.2", "message": {"role": "assistant", "content": "READY"},
            "done": True, "prompt_eval_count": 40, "eval_count": 2,
        })

    adapter = OllamaAdapter(transport=httpx.MockTransport(handler), num_ctx=8192, think=False)
    request = ModelRequest(prompt="Be terse.\nSay READY", cache_prefix="Be terse.\n")
    response = await adapter.complete(request)
    assert response.text == "READY" and response.tokens_in == 40
    assert seen[0] == {
        "model": "llama3.2",
        "messages": [
            {"role": "system", "content": "Be terse.\n"},
            {"role": "user", "content": "Say READY"},
        ],
        "stream": False,
        "truncate": False,
        "options": {"temperature": 0.0, "num_ctx": 8192},
        "think": False,
    }
    await OllamaAdapter(transport=httpx.MockTransport(handler)).complete(
        ModelRequest(prompt="Say READY")
    )
    assert seen[1]["messages"] == [{"role": "user", "content": "Say READY"}]
    assert "think" not in seen[1] and seen[1]["options"]["num_ctx"] == 32_768


async def test_ollama_context_refusal_and_full_context_are_fixed_errors() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        body = {"error": '{"error":{"code":400,"message":"request (35062 tokens) exceeds',
                "type": "exceed_context_size_error"}
        return httpx.Response(400, json=body)

    with pytest.raises(AdapterError, match="^Prompt exceeds the configured context$"):
        await OllamaAdapter(transport=httpx.MockTransport(refuse)).complete(
            ModelRequest(prompt="x")
        )

    def full(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "model": "llama3.2", "message": {"role": "assistant", "content": "partial"},
            "done": True, "prompt_eval_count": 1020, "eval_count": 4,
        })

    with pytest.raises(AdapterError, match="^Prompt exceeds the configured context$"):
        await OllamaAdapter(transport=httpx.MockTransport(full), num_ctx=1024).complete(
            ModelRequest(prompt="x", max_output_tokens=8)
        )

    def other(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "model not found"})

    with pytest.raises(AdapterError, match="^Provider request failed or returned invalid JSON$"):
        await OllamaAdapter(transport=httpx.MockTransport(other)).complete(
            ModelRequest(prompt="x")
        )


@pytest.mark.parametrize("num_ctx", [0, 511, 1_048_577, True, 4096.0])
def test_ollama_context_size_is_validated(num_ctx: object) -> None:
    with pytest.raises(AdapterError, match="^Invalid Ollama context size$"):
        OllamaAdapter(num_ctx=num_ctx)  # type: ignore[arg-type]


# Constrained decoding: ``response_schema`` is opt-in per request.
SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {"answer": {"type": "string", "enum": ["yes", "no"]}},
    "required": ["answer"],
    "additionalProperties": False,
}


def test_response_schema_is_optional_bounded_json_object() -> None:
    plain = ModelRequest(prompt="hi")
    assert plain.response_schema is None and plain.response_schema_sha256() is None
    assert plain == ModelRequest(prompt="hi", response_schema=None)
    assert "response_schema" not in plain.model_dump(exclude_defaults=True)
    shaped = ModelRequest(prompt="hi", response_schema=SCHEMA)
    assert shaped.response_schema == SCHEMA
    reordered = dict(reversed(list(SCHEMA.items())))
    digest = shaped.response_schema_sha256()
    assert digest is not None and len(digest) == 64
    assert ModelRequest(prompt="hi", response_schema=reordered).response_schema_sha256() == digest
    for bad in (["type"], "object", 1, {"type": float("nan")}, {"x": "y" * 16_384}):
        with pytest.raises(ValidationError):
            ModelRequest(prompt="hi", response_schema=bad)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        shaped.response_schema = None  # type: ignore[misc]


def _ollama_reply(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/generate":
        return httpx.Response(200, json={
            "response": '{"answer":"yes"}', "done": True, "prompt_eval_count": 6, "eval_count": 3,
        })
    return httpx.Response(200, json={
        "model": "llama3.2", "message": {"role": "assistant", "content": '{"answer":"yes"}'},
        "done": True, "prompt_eval_count": 6, "eval_count": 3,
    })


@pytest.mark.parametrize("raw", [False, True])
async def test_ollama_sends_format_only_with_a_response_schema(raw: bool) -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return _ollama_reply(request)

    adapter = OllamaAdapter(transport=httpx.MockTransport(handler), raw=raw)
    plain = ModelRequest(prompt="fixed rest", cache_prefix="fixed ", max_output_tokens=8)
    shaped = plain.model_copy(update={"response_schema": SCHEMA})
    first = await adapter.complete(plain)
    second = await adapter.complete(shaped)
    third = await adapter.complete(plain)
    assert bodies[0] == bodies[2] and b'"format"' not in bodies[0]
    without, with_schema = json.loads(bodies[0]), json.loads(bodies[1])
    assert list(with_schema) == [*without, "format"]
    assert with_schema == {**without, "format": SCHEMA}
    # Accounting is unchanged: the same quote and the same measured usage.
    assert adapter.quote(plain) == adapter.quote(shaped)
    assert first == second == third


async def test_openai_sends_strict_json_schema_only_with_a_response_schema() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"answer":"no"}'}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    adapter = OpenAICompatibleAdapter(api_key="k", transport=httpx.MockTransport(handler))
    plain = ModelRequest(prompt="hi", max_output_tokens=8)
    shaped = ModelRequest(prompt="hi", max_output_tokens=8, response_schema=SCHEMA)
    first = await adapter.complete(plain)
    second = await adapter.complete(shaped)
    assert bodies[0] == CAPTURED_OPENAI
    with_schema = json.loads(bodies[1])
    assert list(with_schema) == [*json.loads(CAPTURED_OPENAI), "response_format"]
    assert with_schema["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "response", "strict": True, "schema": SCHEMA},
    }
    assert adapter.quote(plain) == adapter.quote(shaped)
    assert first == second


async def test_anthropic_ignores_a_response_schema() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 4, "output_tokens": 2},
        })

    adapter = AnthropicAdapter(api_key="k", transport=httpx.MockTransport(handler))
    plain = ModelRequest(prompt="hi", max_output_tokens=20)
    shaped = plain.model_copy(update={"response_schema": SCHEMA})
    assert await adapter.complete(plain) == await adapter.complete(shaped)
    assert bodies == [CAPTURED_ANTHROPIC, CAPTURED_ANTHROPIC]
    assert adapter.quote(plain) == adapter.quote(shaped)


async def test_fake_records_only_the_schema_digest() -> None:
    adapter = FakeAdapter()
    shaped = ModelRequest(prompt="hi", response_schema=SCHEMA)
    await adapter.complete(ModelRequest(prompt="hi"))
    await adapter.complete(shaped)
    assert [shape.response_schema_sha256 for shape in adapter.seen] == [
        None,
        shaped.response_schema_sha256(),
    ]
    assert adapter.seen[0] == RequestShape(prompt_bytes=2, image_count=0, image_bytes=0, sha256s=())
    assert "answer" not in adapter.seen[1].model_dump_json()


# -- the Nebius Token Factory dialect -------------------------------------------------

SUPER = "nvidia/nemotron-3-super-120b-a12b"
ULTRA = "nvidia/Nemotron-3-Ultra-550b-a55b"
NEBIUS_URL = "https://api.tokenfactory.nebius.com/v1"


def nebius_reply(
    content: Any, *, model: str = SUPER, finish: str = "stop", reasoning_tokens: int | None = 0,
    **message: Any,
) -> dict[str, Any]:
    """A non-streaming Token Factory reply: reasoning beside the content, as observed live.

    ``reasoning_tokens`` of the 15 completion tokens; None leaves the details out (Nano).
    """
    details = None if reasoning_tokens is None else {"reasoning_tokens": reasoning_tokens}
    return {
        "id": "chatcmpl-1", "object": "chat.completion", "model": model,
        "choices": [{"index": 0, "finish_reason": finish,
                     "message": {"role": "assistant", "content": content, **message}}],
        "usage": {"prompt_tokens": 27, "completion_tokens": 15, "total_tokens": 42,
                  "completion_tokens_details": details,
                  "prompt_tokens_details": None, "prompt_cache_hit_tokens": 0,
                  "prompt_cache_miss_tokens": 27},
    }


def echo_model(content: Any = "ok", **reply: Any) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        return httpx.Response(200, json=nebius_reply(content, model=model, **reply))

    return handler


def nebius(handler: Any, model: str = SUPER, **options: Any) -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter(
        model, api_key="k", base_url=NEBIUS_URL, dialect="nebius",
        transport=httpx.MockTransport(handler), **options,
    )


async def test_nebius_always_caps_output_and_thinks_only_when_asked() -> None:
    bodies: list[dict[str, Any]] = []
    reply = echo_model()

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return reply(request)  # type: ignore[no-any-return]

    chat = nebius(handler)
    await chat.complete(ModelRequest(prompt="hi"))
    await chat.complete(ModelRequest(prompt="hi", max_output_tokens=8, temperature=0.5))
    engineer = nebius(handler, ULTRA, think=True, temperature=1.0, top_p=0.95)
    await engineer.complete(ModelRequest(prompt="hi"))
    await engineer.complete(ModelRequest(prompt="hi", response_schema=SCHEMA))
    # No cap on the request: the price entry's capacity, which is what quote reserves.
    assert bodies[0] == {
        "model": SUPER, "messages": [{"role": "user", "content": "hi"}], "temperature": 0.0,
        "stream": False, "n": 1, "max_tokens": 16384,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    assert chat.quote(ModelRequest(prompt="hi")).tokens_out == 16384
    assert (bodies[1]["max_tokens"], bodies[1]["temperature"]) == (8, 0.5)
    assert bodies[2]["max_tokens"] == 32768 == engineer.quote(ModelRequest(prompt="hi")).tokens_out
    assert bodies[2]["chat_template_kwargs"] == {"enable_thinking": True}
    assert (bodies[2]["temperature"], bodies[2]["top_p"]) == (1.0, 0.95)
    # A schema call never thinks (Lightning ignores json_schema while thinking).
    assert bodies[3]["chat_template_kwargs"] == {"enable_thinking": False}
    assert bodies[3]["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "response", "strict": True, "schema": SCHEMA},
    }
    # Never reasoning_effort ("none" empties Nano's content) and no vLLM-only extras.
    assert all("reasoning_effort" not in body and "guided_json" not in body for body in bodies)


async def test_sampling_overrides_leave_the_default_dialect_body_otherwise_unchanged() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}],
                                         "usage": {"prompt_tokens": 5, "completion_tokens": 2}})

    adapter = OpenAICompatibleAdapter(
        api_key="k", transport=httpx.MockTransport(handler), temperature=0.7, top_p=0.9
    )
    await adapter.complete(ModelRequest(prompt="hi", max_output_tokens=8))
    expected = json.loads(CAPTURED_OPENAI)
    assert json.loads(bodies[0]) == {**expected, "temperature": 0.7, "top_p": 0.9}
    assert adapter.dialect == "openai" and adapter._timeout == 60.0


@pytest.mark.parametrize(
    ("content", "message", "text"),
    [
        ("\n\nThe answer.", {}, "The answer."),
        ('<think>\nweigh it\n</think>\n\n{"answer": "yes"}', {}, '{"answer": "yes"}'),
        ("Plain <think>kept</think>", {}, "Plain <think>kept</think>"),
        ("<th", {}, "<th"),
        ("<think>never closed", {}, ""),
        ("", {"reasoning_content": "private plan", "reasoning": "private plan"}, ""),
        (None, {"tool_calls": [{"id": "chatcmpl-tool-1", "type": "function",
                                "function": {"name": "f", "arguments": "{}"}}]}, ""),
        # Super, thinking off, live (2026-10-03): a note before a call, then blank lines.
        ("We need get_weather for Paris.\n\n\n",
         {"tool_calls": [{"id": "chatcmpl-tool-1", "type": "function",
                          "function": {"name": "get_weather", "arguments": "{}"}}]},
         "We need get_weather for Paris."),
        ("Line one.\n\nLine two.\n", {}, "Line one.\n\nLine two.\n"),  # no call: kept whole
    ],
)
async def test_nebius_replies_show_only_the_answer(
    content: Any, message: dict[str, Any], text: str
) -> None:
    response = await nebius(echo_model(content, **message)).complete(ModelRequest(prompt="hi"))
    assert response.text == text
    assert (response.tokens_in, response.tokens_out) == (27, 15)
    assert response.cost_usd == pytest.approx(price_for(load_prices(), SUPER).cost(27, 15))


async def test_default_dialect_keeps_content_exactly() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "\n\n<think>x"}}],
                                         "usage": {"prompt_tokens": 5, "completion_tokens": 2}})

    adapter = OpenAICompatibleAdapter(api_key="k", transport=httpx.MockTransport(handler))
    assert (await adapter.complete(ModelRequest(prompt="hi"))).text == "\n\n<think>x"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("", {"reasoning_content": "step one, step two"}),
        (None, {"reasoning": "step one"}),
        ("<think>still going", {}),
        ("\n\n", {}),
    ],
)
async def test_nebius_reply_cut_inside_its_reasoning_is_a_distinct_error(
    content: Any, message: dict[str, Any]
) -> None:
    adapter = nebius(echo_model(content, finish="length", **message))
    with pytest.raises(AdapterError) as caught:
        await adapter.complete(ModelRequest(prompt="hi", max_output_tokens=64))
    assert str(caught.value) == REASONING_CUT
    assert "step" not in str(caught.value)


@pytest.mark.parametrize(
    ("think", "content", "reasoning_tokens", "finish"),
    [
        # Ultra and Lightning cut while thinking (live, 2026-10-03): the reasoning comes back
        # as content with no reasoning field, and usage counts every token as reasoning.
        (True, "The user wants to know the count of prime numbers.\nI need to", 15, "length"),
        (True, "Here's a thinking process:\n\n1.  **Understand", None, "length"),  # no details
        (None, "rambling, never answering", 15, "stop"),  # every token was reasoning
    ],
)
async def test_nebius_unparsed_reasoning_is_never_the_answer(
    think: bool | None, content: str, reasoning_tokens: int | None, finish: str
) -> None:
    reply = echo_model(content, finish=finish, reasoning_tokens=reasoning_tokens)
    adapter = nebius(reply, ULTRA, think=think)
    with pytest.raises(AdapterError) as caught:
        await adapter.complete(ModelRequest(prompt="hi", max_output_tokens=64))
    assert str(caught.value) == REASONING_CUT and "thinking" not in str(caught.value)


async def test_nebius_answer_cut_after_its_reasoning_is_still_text() -> None:
    reply = echo_model("There are 21 pri", finish="length", reasoning_tokens=10,
                       reasoning_content="count them one by one")
    adapter = nebius(reply, ULTRA, think=True)
    assert (await adapter.complete(ModelRequest(prompt="hi"))).text == "There are 21 pri"


@pytest.mark.parametrize(
    ("content", "finish", "text"),
    [
        # No reasoning field and no closing tag, not cut: the model did not reason inline.
        ("51", "stop", "51"),
        # Reasoning inline after the template's opening tag, closed, then a cut answer: the
        # answer stays text, as when the reasoning arrives in its own field.
        ("count them\n</think>\n\nThere are 21 pri", "length", "There are 21 pri"),
        ("<think>count them</think>\n21", "stop", "21"),
    ],
)
async def test_nebius_thinking_reply_without_a_reasoning_field(
    content: str, finish: str, text: str
) -> None:
    adapter = nebius(echo_model(content, finish=finish, reasoning_tokens=None), ULTRA,
                     think=True)
    assert (await adapter.complete(ModelRequest(prompt="hi"))).text == text


async def test_nebius_cut_answers_and_missing_text_keep_their_meaning() -> None:
    cut = nebius(echo_model("partial answ", finish="length"))
    assert (await cut.complete(ModelRequest(prompt="hi"))).text == "partial answ"
    for adapter in (
        nebius(echo_model(None, reasoning="all of it")),
        OpenAICompatibleAdapter(SUPER, api_key="k", base_url=NEBIUS_URL,
                                transport=httpx.MockTransport(echo_model(None, finish="length"))),
    ):
        with pytest.raises(AdapterError, match="^Provider response is missing text$"):
            await adapter.complete(ModelRequest(prompt="hi"))


async def test_nebius_retries_busy_answers_honouring_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import email.utils
    from datetime import UTC, datetime, timedelta

    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(http_module, "_sleep", sleep)
    answers = [
        httpx.Response(429, headers={"Retry-After": "3"}),
        httpx.Response(503, headers={"Retry-After": "999"}),
        httpx.Response(200, json=nebius_reply("ok")),
    ]
    adapter = nebius(lambda request: answers.pop(0))
    assert (await adapter.complete(ModelRequest(prompt="hi"))).text == "ok"
    assert waits == [3.0, http_module.RETRY_WAIT_S] and answers == []
    # Still busy after two retries: the usual failure; without Retry-After, 1 s then 2 s.
    waits.clear()
    calls: list[int] = []

    def busy(request: httpx.Request) -> httpx.Response:
        calls.append(529)
        return httpx.Response(529, text="private overload detail")

    with pytest.raises(AdapterError, match="^Provider request failed") as caught:
        await nebius(busy).complete(ModelRequest(prompt="hi"))
    assert len(calls) == 3 and waits == [1.0, 2.0] and "private" not in str(caught.value)
    # An HTTP date is honoured too.
    waits.clear()
    when = email.utils.format_datetime(datetime.now(UTC) + timedelta(seconds=6), usegmt=True)
    dated = [httpx.Response(429, headers={"Retry-After": when}),
             httpx.Response(200, json=nebius_reply("ok"))]
    await nebius(lambda request: dated.pop(0)).complete(ModelRequest(prompt="hi"))
    assert len(waits) == 1 and 3.0 < waits[0] <= 6.0
    # Other statuses, and the default dialect, never retry.
    waits.clear()
    for status, dialect in ((500, "nebius"), (401, "nebius"), (429, "openai")):
        seen: list[int] = []

        def failing(
            request: httpx.Request, status: int = status, seen: list[int] = seen
        ) -> httpx.Response:
            seen.append(status)
            return httpx.Response(status)

        once = OpenAICompatibleAdapter(SUPER, api_key="k", base_url=NEBIUS_URL,
                                       dialect=dialect,  # type: ignore[arg-type]
                                       transport=httpx.MockTransport(failing))
        with pytest.raises(AdapterError):
            await once.complete(ModelRequest(prompt="hi"))
        assert seen == [status]
    assert waits == []


def test_nebius_adapter_options_are_checked() -> None:
    assert nebius(echo_model())._timeout == http_module.NEBIUS_TIMEOUT_S == 120.0
    assert nebius(echo_model(), timeout=600)._timeout == 600.0
    bad: list[dict[str, Any]] = [{"think": True}, {"dialect": "other"}, {"temperature": 2.5},
                                 {"top_p": 0.0}, {"top_p": 1.5}]
    for options in bad:
        with pytest.raises(AdapterError):
            OpenAICompatibleAdapter(api_key="k", **options)
