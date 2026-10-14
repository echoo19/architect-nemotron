"""Bench providers: evidence labels, model filtering, metering, probes and local models.

No provider is called: the fake and oracle adapters answer in-process, and every
real ``OllamaAdapter`` here talks to an ``httpx.MockTransport`` (no Ollama server,
no network), so the local path is exercised through the adapter's own wire code.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from architect.bench import BenchError, generate_pages, read_rows
from architect.bench.oracle import OracleAdapter, OracleState
from architect.bench.providers import (
    PROBE_CODES,
    CallMeter,
    ProbeRow,
    adapter_price,
    build_factory,
    check_models,
    evidence_label,
    instrument,
    probe_models,
    provider_for,
    resolve_models,
)
from architect.bench.render import bound_line, probe_table
from architect.bench.rows import prices_sha256
from architect.bench.runner import BenchConfig, bound, run_bench
from architect.bench.suite import SUITES
from architect.games import public
from architect.models import FakeAdapter, ModelAdapter, ModelRequest, ModelResponse, OllamaAdapter
from architect.models.images import ImageInput
from architect.models.pricing import load_prices
from architect.profiles import Profile, load_profile

ROOT = Path(__file__).resolve().parents[1]
LOCAL_PRICES = ROOT / "examples" / "local" / "prices.json"
LOCAL_MODELS = ("granite4.2:8b", "gemma4:26b", "muse-glimmer:30b")


def page() -> ImageInput:
    return ImageInput.from_bytes(generate_pages(7, 1, "clean", key_page=False)[0].image)


def local_profile(tmp_path: Path) -> Profile:
    models = {
        name: {
            "provider": "ollama",
            "base_url": "http://127.0.0.1:11434",
            "prices": str(LOCAL_PRICES),
            "num_ctx": 65536,
            "timeout_s": 600,
        }
        for name in LOCAL_MODELS
    }
    path = tmp_path / "local.json"
    path.write_text(json.dumps({"models": models}), encoding="utf-8")
    return load_profile(path)


@pytest.mark.parametrize(
    ("provider", "label"),
    [
        ("fake", "synthetic_fake"),
        ("ollama", "local_zero_cost"),
        ("anthropic", "live_billed"),
        ("openai", "live_billed"),
        ("claude-code", "live_api_equivalent"),
        ("codex", "live_api_equivalent"),
    ],
)
def test_evidence_labels(provider: str, label: str) -> None:
    assert evidence_label(provider) == label  # type: ignore[arg-type]


def test_model_filtering(tmp_path: Path) -> None:
    fake = BenchConfig(tier="quick", provider="fake")
    assert resolve_models(fake, None) == ("fake",)
    assert resolve_models(fake.model_copy(update={"models": ("fake", "fake")}), None) == ("fake",)
    with pytest.raises(BenchError, match="bench_model_unconfigured"):
        resolve_models(fake.model_copy(update={"models": ("invented",)}), None)
    profile = local_profile(tmp_path)
    paid = BenchConfig(tier="quick", provider="profile")
    assert resolve_models(paid, profile) == LOCAL_MODELS
    chosen = paid.model_copy(update={"models": ("gemma4:26b",)})
    assert resolve_models(chosen, profile) == ("gemma4:26b",)
    assert provider_for(chosen, profile, "gemma4:26b") == "ollama"
    with pytest.raises(BenchError, match="bench_model_unconfigured"):
        resolve_models(paid.model_copy(update={"models": ("llama3.2",)}), profile)
    with pytest.raises(BenchError, match="bench_profile_required"):
        resolve_models(paid, None)
    with pytest.raises(BenchError, match="bench_model_unconfigured"):
        provider_for(paid, profile, "claude-sonnet-5")


def test_local_models_are_first_class_offline_providers(tmp_path: Path) -> None:
    profile = local_profile(tmp_path)
    config = BenchConfig(tier="quick", provider="profile", models=("granite4.2:8b",))
    adapter = build_factory(config, profile, None)("granite4.2:8b")
    assert isinstance(adapter, OllamaAdapter)
    assert adapter.num_ctx == 65536 and adapter.model == "granite4.2:8b"
    price = adapter_price(adapter)
    assert price is not None and price.input_per_million == 0 and price.image is None
    assert adapter.quote(ModelRequest(prompt="x" * 1000)).cost_usd == 0
    # quote only: a $0 model reserves $0, and vision cells are not guessed.
    limits = bound(config, profile=profile)
    codes = {cell.task_id: cell.code for cell in limits.cells}
    assert codes == {
        "sentiment-24": None,
        "invoice-40": None,
        "exam-clean-40": "image_not_configured",
    }
    sentiment = next(cell for cell in limits.cells if cell.task_id == "sentiment-24")
    assert sentiment.per_call_usd == 0 and sentiment.reserved_usd == 0
    assert sentiment.calls > 0 and limits.total_usd == 0 and limits.calls > 0
    line = bound_line("quick", len(limits.models), limits.total_usd, limits.calls)
    assert line.startswith("bench quick on 1 models: <=$0.0000 API-equivalent bound, ~")
    capped = config.model_copy(update={"max_usd": 0.1})
    assert [cell.code for cell in bound(capped, profile=profile).cells] == [
        None,
        None,
        "image_not_configured",
    ]  # a dollar cap never skips a free model
    with pytest.raises(BenchError, match="bench_variant_unknown"):
        build_factory(config, profile, None, variant="noisy")


def test_missing_credentials_are_a_provider_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BENCH_TEST_KEY", raising=False)
    path = tmp_path / "p.json"
    path.write_text(
        json.dumps(
            {"models": {"gpt-4o-mini": {"provider": "openai", "api_key_env": "BENCH_TEST_KEY"}}}
        ),
        encoding="utf-8",
    )
    profile = load_profile(path)
    factory = build_factory(BenchConfig(tier="quick", provider="profile"), profile, None)
    with pytest.raises(BenchError, match="bench_provider_failed"):
        factory("gpt-4o-mini")
    with pytest.raises(BenchError, match="bench_model_unconfigured"):
        factory("unlisted")


def test_fake_factory_uses_oracles_and_noisy_rate() -> None:
    config = BenchConfig(tier="smoke", provider="fake", seed=3)
    default = build_factory(config, None, OracleState())("fake")
    noisy = build_factory(config, None, OracleState(), variant="noisy")("fake")
    assert isinstance(default, OracleAdapter) and isinstance(noisy, OracleAdapter)
    assert default._error_rate == 0.0 and noisy._error_rate == 0.15


async def test_instrument_meters_in_place_and_keeps_the_type() -> None:
    meter = CallMeter()
    adapter = FakeAdapter()
    wrapped = instrument(adapter, meter)
    assert wrapped is adapter and isinstance(wrapped, FakeAdapter)
    assert instrument(adapter, meter) is adapter  # idempotent per meter
    image = page()
    await adapter.complete(ModelRequest(prompt="hi", images=(image,)))
    await adapter.complete(ModelRequest(prompt="hi"))
    assert meter.calls == 2 and meter.image_inputs == 1
    assert meter.image_tokens_estimate == 100  # the fake's fixed rule, display only
    assert meter.tokens_in == 8 + 100 + 8 and meter.cost_usd > 0


class OverReporting(FakeAdapter):
    """Reports more image usage than it quoted: the ceiling is under-configured."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await super().complete(request)
        if not request.images:
            return response
        return response.model_copy(
            update={
                "tokens_in": response.tokens_in + 5000,
                "cost_usd": response.cost_usd + 0.005,
            }
        )


async def test_probe_rows_for_honest_and_over_reporting_fakes() -> None:
    adapters: dict[str, ModelAdapter] = {"fake": FakeAdapter(), "over": OverReporting()}

    def factory(model: str) -> ModelAdapter:
        return adapters["fake" if model == "fake" else "over"]

    image = page()
    honest, over = await probe_models(factory, load_prices(), ["fake", "over"], page=image)
    assert honest.code is None and honest.image_configured and honest.basis == "assumption"
    assert honest.image_reserved_tokens == 100 and honest.image_estimate_tokens == 100
    assert honest.text_measured_tokens == 8 and honest.image_measured_tokens == 100
    assert honest.measured_usd is not None and honest.measured_usd <= honest.reserved_usd
    assert over.code == "image_reservation_under_configured"
    assert over.image_measured_tokens is not None and over.image_measured_tokens > 100
    text = probe_table([honest, over])
    assert "assumption!" in text and "image_reservation_under_configured" in text
    assert all(len(line) <= 100 and line.isascii() for line in text.splitlines())


async def test_probe_of_a_text_only_model_reports_image_not_configured(tmp_path: Path) -> None:
    prices = tmp_path / "prices.json"
    prices.write_text(
        json.dumps(
            {
                "models": {
                    "fake": {
                        "input_per_million": 1.0,
                        "output_per_million": 1.0,
                        "output_capacity_tokens": 2,
                        "input_overhead_tokens": 0,
                    }
                },
                "fake_usage": {"tokens_in": 8, "tokens_out": 2},
            }
        ),
        encoding="utf-8",
    )
    adapter = FakeAdapter(prices_path=prices)
    [row] = await probe_models(lambda _: adapter, load_prices(prices), ["fake"], page=page())
    assert row.code == "image_not_configured" and not row.image_configured
    assert row.image_reserved_tokens is None and row.image_measured_tokens is None
    assert row.text_measured_tokens == 8 and adapter.calls == 1


async def test_probe_reports_a_factory_failure_as_a_code() -> None:
    def factory(model: str) -> ModelAdapter:
        raise BenchError("bench_provider_failed")

    [row] = await probe_models(
        factory, load_prices(), ["gpt-4o-mini"], page=page(), providers={"gpt-4o-mini": "openai"}
    )
    assert row == ProbeRow(
        model="gpt-4o-mini",
        provider="openai",
        image_configured=False,
        basis=None,
        text_reserved_tokens=0,
        text_measured_tokens=None,
        image_reserved_tokens=None,
        image_estimate_tokens=None,
        image_measured_tokens=None,
        reserved_usd=0.0,
        measured_usd=None,
        code="provider_failed",  # a PROBE_CODES member, never a bench_* run code
    )


async def test_bench_arc_accepts_a_local_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``bench arc`` on a local Ollama model: offline binding, $0, the profile's settings."""
    from architect.bench import runner

    profile = local_profile(tmp_path)
    seen: list[public.PreviewOptions] = []
    agents: list[ModelAdapter] = []

    async def manufacture(
        options: public.PreviewOptions, tmp: Path, *, agent: ModelAdapter | None = None
    ) -> dict[str, object]:
        seen.append(options)
        assert agent is not None
        agents.append(agent)
        return {
            "adapter": options.adapter,
            "model_binding": "offline",
            "licensed": True,
            "score": 0.25,
            "score_source": "official_offline_preview",
            "compile_failures": [],
            "run_failures": [],
            "child_failures": [["game_action_limit"]],
            "compliance": "unverified",
        }

    monkeypatch.setattr(runner, "_has_module", lambda name: True)
    monkeypatch.setattr(public, "manufacture", manufacture)
    config = BenchConfig(
        tier="arc",
        provider="profile",
        models=("gemma4:26b",),
        arc_games=5,
        arc_actions=8,
        arc_cache=tmp_path / "cache",
        out=tmp_path / "out",
    )
    result = await runner.run_bench(config, profile=profile, workspace_root=tmp_path / "ws")
    [row] = result.rows
    [options] = seen
    assert options.adapter == "ollama" and options.model == "gemma4:26b"
    assert options.offline and options.max_actions == 8 and options.games == 5
    assert options.prices == LOCAL_PRICES and options.num_ctx == 65536
    assert options.call_timeout_s == 600 and options.cache == tmp_path / "cache"
    assert isinstance(agents[0], OllamaAdapter)
    assert row.evidence == "local_zero_cost" and row.provider == "ollama"
    assert row.licensed and row.cost_usd == 0 and row.held_out_score == 0.25
    assert row.arc is not None and row.arc.model_binding == "offline"
    assert row.arc.score_source == "official_offline_preview"
    assert row.failures == ("game_action_limit",)


async def test_arc_preview_without_the_extra_or_cache_is_bench_arc_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An arc run whose every cell is unavailable measured nothing: exit-1 code, not success."""
    from architect.bench import runner

    monkeypatch.setattr(runner, "_has_module", lambda name: False)
    config = BenchConfig(tier="arc", provider="fake", out=tmp_path / "out")
    # Without the extra it is known from sizing: refused before anything is written.
    with pytest.raises(BenchError) as caught:
        runner.bound(config)
    assert caught.value.code == "bench_arc_unavailable"
    with pytest.raises(BenchError) as caught:
        await runner.run_bench(config, workspace_root=tmp_path / "ws")
    assert caught.value.code == "bench_arc_unavailable"
    assert not (tmp_path / "out").exists()
    # An empty cache is found only while running: the row is kept, the run still fails.
    monkeypatch.setattr(runner, "_has_module", lambda name: True)
    empty = config.model_copy(update={"arc_cache": tmp_path / "empty-cache"})
    with pytest.raises(BenchError) as caught:
        await runner.run_bench(empty, workspace_root=tmp_path / "ws")
    assert caught.value.code == "bench_arc_unavailable"
    [directory] = list((tmp_path / "out").iterdir())
    [row] = read_rows(directory / "rows.jsonl")
    assert row.failures == ("arc_unavailable",) and row.arc is None and not row.licensed
    assert (directory / "summary.json").is_file()


# ------------------------------------------------ local Ollama through a mock transport


class OllamaStub:
    """An ``httpx.MockTransport`` handler speaking Ollama's ``/api/chat``.

    Text requests are answered by an in-process oracle from the delivered prompt
    (system + user message, which the adapter split from one prompt); the stub
    records only shapes (path, num_ctx, image count), never content.
    """

    def __init__(
        self,
        model: str,
        *,
        state: OracleState | None = None,
        text_tokens: int = 20,
        image_tokens: int = 1500,
    ) -> None:
        self.model = model
        self.oracle = OracleAdapter("fake", state or OracleState())
        self.text_tokens = text_tokens
        self.image_tokens = image_tokens
        self.paths: list[str] = []
        self.num_ctx: set[int] = set()
        self.images: list[int] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.paths.append(request.url.path)
        self.num_ctx.add(body["options"]["num_ctx"])
        assert body["model"] == self.model and body["stream"] is False
        messages = body["messages"]
        user = messages[-1]
        images = [base64.b64decode(item) for item in user.get("images", [])]
        self.images.append(len(images))
        if images:
            assert images[0].startswith(b"\x89PNG")
            text, tokens_in = "7", self.text_tokens + self.image_tokens * len(images)
        else:
            prompt = "".join(message["content"] for message in messages)
            answer = await self.oracle.complete(ModelRequest(prompt=prompt))
            text, tokens_in = answer.text, self.text_tokens
        return httpx.Response(
            200,
            json={
                "model": self.model,
                "message": {"role": "assistant", "content": text},
                "done": True,
                "prompt_eval_count": tokens_in,
                "eval_count": 4,
            },
        )


def vision_adapter(stub: OllamaStub) -> OllamaAdapter:
    """``llama3.2-vision``: $0 with a 1600-token ``assumption`` image ceiling."""
    return OllamaAdapter(
        "llama3.2-vision", transport=httpx.MockTransport(stub), num_ctx=32768
    )


@pytest.mark.parametrize(
    ("image_tokens", "code"),
    [(1500, None), (8980, "image_reservation_under_configured")],
)
async def test_ollama_probe_text_and_image_through_the_wire(
    image_tokens: int, code: str | None
) -> None:
    """A $0 local vision model: the probe must still catch an under-configured ceiling."""
    stub = OllamaStub("llama3.2-vision", image_tokens=image_tokens)
    adapter = vision_adapter(stub)
    [row] = await probe_models(
        lambda _: adapter,
        load_prices(),
        ["llama3.2-vision"],
        page=page(),
        providers={"llama3.2-vision": "ollama"},
    )
    assert stub.paths == ["/api/chat", "/api/chat"] and stub.images == [0, 1]
    assert stub.num_ctx == {32768}
    assert row.provider == "ollama" and row.image_configured and row.basis == "assumption"
    assert row.text_measured_tokens == 20
    assert row.image_reserved_tokens == 1600 and row.image_measured_tokens == image_tokens
    assert row.reserved_usd == 0 and row.measured_usd == 0  # $0: tokens decide, not dollars
    assert row.code == code
    table = probe_table([row])
    assert "assumption!" in table and ("ok" in table if code is None else code in table)


async def test_ollama_text_only_model_probe_through_the_wire(tmp_path: Path) -> None:
    profile = local_profile(tmp_path)
    config = BenchConfig(tier="quick", provider="profile", models=("granite4.2:8b",))
    stub = OllamaStub("granite4.2:8b")
    built = build_factory(config, profile, None)("granite4.2:8b")
    assert isinstance(built, OllamaAdapter)
    adapter = OllamaAdapter(
        "granite4.2:8b",
        prices_path=LOCAL_PRICES,
        transport=httpx.MockTransport(stub),
        num_ctx=built.num_ctx,
    )
    [row] = await probe_models(
        lambda _: adapter, load_prices(), ["granite4.2:8b"], page=page(),
        providers={"granite4.2:8b": "ollama"},
    )
    assert stub.paths == ["/api/chat"] and stub.num_ctx == {65536}
    assert row.code == "image_not_configured" and row.text_measured_tokens == 20
    assert row.measured_usd == 0 and row.image_measured_tokens is None


async def test_a_local_text_cell_runs_through_a_real_ollama_adapter(tmp_path: Path) -> None:
    """``bench quick`` on a local model end to end: real adapter, mock wire, oracle answers.

    The profile mixes the local model (examples/local/prices.json) with a fake model
    on the default table, so each row must carry its own model's price-table hash.
    """
    base = local_profile(tmp_path)
    models = {"granite4.2:8b": base.models["granite4.2:8b"].model_dump(mode="json")}
    models["fake"] = {"provider": "fake"}
    path = tmp_path / "mixed.json"
    path.write_text(json.dumps({"models": models}), encoding="utf-8")
    profile = load_profile(path)
    tasks = {task.id: task for task in SUITES["v1"]}
    state = OracleState()
    for task_id in ("sentiment-24", "invoice-40"):
        state.merge(tasks[task_id].bundle(7, 2).state)
    stub = OllamaStub("granite4.2:8b", state=state)
    oracles: dict[str, ModelAdapter] = {}

    def factory(model: str) -> ModelAdapter:
        if model == "granite4.2:8b":
            return OllamaAdapter(
                model,
                prices_path=LOCAL_PRICES,
                transport=httpx.MockTransport(stub),
                num_ctx=65536,
                timeout=600,
            )
        return oracles.setdefault(model, OracleAdapter(model, state))

    config = BenchConfig(
        tier="quick",
        provider="profile",
        models=("granite4.2:8b", "fake"),
        presets=("extractor",),
        size=2,
        max_usd=0.5,
        out=tmp_path / "out",
    )
    result = await run_bench(
        config, factory=factory, profile=profile, workspace_root=tmp_path / "ws"
    )
    local = [row for row in result.rows if row.model == "granite4.2:8b"]
    assert {row.task_id: row.failures for row in local if not row.licensed} == {
        "exam-clean-40": ("image_not_configured",)
    }
    text = [row for row in local if row.task_id != "exam-clean-40"]
    assert [row.task_id for row in text] == ["sentiment-24", "invoice-40"]
    local_sha = hashlib.sha256(LOCAL_PRICES.read_bytes()).hexdigest()
    for row in text:
        assert row.licensed and row.failures == () and row.stages == (1, 2, 4)
        assert row.provider == "ollama" and row.evidence == "local_zero_cost"
        assert row.cost_usd == 0 and row.reserved_usd == 0 and row.per_call_reservation_usd == 0
        assert row.calls > 0 and row.tokens_in == 20 * row.calls and row.wall_s > 0
        assert row.prices_sha256 == local_sha
    assert set(stub.paths) == {"/api/chat"} and stub.num_ctx == {65536}
    assert len(stub.paths) == sum(row.calls for row in text) and set(stub.images) == {0}
    fake_rows = [row for row in result.rows if row.model == "fake"]
    assert fake_rows and all(row.evidence == "synthetic_fake" for row in fake_rows)
    assert all(row.prices_sha256 == prices_sha256(None) != local_sha for row in fake_rows)


# --------------------------------------- local means local and $0; names a row can hold


def ollama_profile(tmp_path: Path, name: str, **settings: object) -> Profile:
    entry: dict[str, object] = {"provider": "ollama", "prices": str(LOCAL_PRICES)}
    entry.update(settings)
    path = tmp_path / f"p{len(list(tmp_path.glob('p*.json')))}.json"
    path.write_text(json.dumps({"models": {name: entry}}), encoding="utf-8")
    return load_profile(path)


class Counting(OracleAdapter):
    made = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        Counting.made += 1
        return await super().complete(request)


def counting(model: str) -> ModelAdapter:
    return Counting(model, OracleState())


@pytest.mark.parametrize(
    "base_url", ["https://ollama.com", "http://10.0.0.5:11434", "http://user:pw@localhost:11434"]
)
async def test_an_ollama_model_off_this_host_is_refused_before_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    """local_zero_cost must be true: Ollama's cloud (or any other host) is not local."""
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    profile = ollama_profile(tmp_path, "granite4.2:8b", base_url=base_url)
    config = BenchConfig(
        tier="quick", provider="profile", models=("granite4.2:8b",), out=tmp_path / "out"
    )
    with pytest.raises(BenchError, match="bench_model_not_local"):
        resolve_models(config, profile)
    with pytest.raises(BenchError, match="bench_model_not_local"):
        bound(config, profile=profile)
    Counting.made = 0
    with pytest.raises(BenchError, match="bench_model_not_local"):
        await run_bench(
            config, factory=counting, profile=profile, workspace_root=tmp_path / "ws"
        )
    assert Counting.made == 0 and not (tmp_path / "out").exists()


def test_ollama_host_from_the_environment_is_checked_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = ollama_profile(tmp_path, "granite4.2:8b")  # no base_url: OLLAMA_HOST decides
    config = BenchConfig(tier="quick", provider="profile", models=("granite4.2:8b",))
    monkeypatch.setenv("OLLAMA_HOST", "https://ollama.com")
    with pytest.raises(BenchError, match="bench_model_not_local"):
        resolve_models(config, profile)
    for local in ("http://localhost:11434", "http://127.0.0.1:11434", "http://[::1]:11434"):
        monkeypatch.setenv("OLLAMA_HOST", local)
        assert resolve_models(config, profile) == ("granite4.2:8b",)
    monkeypatch.delenv("OLLAMA_HOST")
    assert resolve_models(config, profile) == ("granite4.2:8b",)  # the localhost default


def test_a_priced_ollama_model_is_not_local_zero_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    table = json.loads(LOCAL_PRICES.read_text(encoding="utf-8"))
    table["models"]["granite4.2:8b"]["output_per_million"] = 0.5
    priced = tmp_path / "priced.json"
    priced.write_text(json.dumps(table), encoding="utf-8")
    profile = ollama_profile(
        tmp_path, "granite4.2:8b", base_url="http://127.0.0.1:11434", prices=str(priced)
    )
    config = BenchConfig(tier="quick", provider="profile", models=("granite4.2:8b",))
    with pytest.raises(BenchError, match="bench_model_not_local"):
        resolve_models(config, profile)
    with pytest.raises(BenchError, match="bench_model_not_local"):
        check_models(config, profile, ("granite4.2:8b",))
    # The same model on the $0 table and a loopback host is accepted.
    local = ollama_profile(tmp_path, "granite4.2:8b", base_url="http://127.0.0.1:11434")
    check_models(config, local, ("granite4.2:8b",))


@pytest.mark.parametrize("name", ["/x", "x/", "a//b", "-lead", "team/-x", "a" * 129])
async def test_a_model_name_a_row_cannot_hold_is_refused_before_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Names BenchRow's model pattern refuses (Phase 14 Section 9.5) are refused up front,
    not after spend. Namespaced ids such as ``team/granite:8b`` are now valid."""
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    profile = ollama_profile(tmp_path, name, base_url="http://127.0.0.1:11434")
    config = BenchConfig(tier="quick", provider="profile", models=(name,), out=tmp_path / "out")
    with pytest.raises(BenchError, match="bench_model_unconfigured"):
        resolve_models(config, profile)
    with pytest.raises(BenchError, match="bench_model_unconfigured"):
        bound(config, profile=profile)
    with pytest.raises(BenchError, match="bench_model_unconfigured"):
        bound(config, factory=counting, profile=profile)
    Counting.made = 0
    with pytest.raises(BenchError, match="bench_model_unconfigured"):
        await run_bench(
            config, factory=counting, profile=profile, workspace_root=tmp_path / "ws"
        )
    fake = BenchConfig(tier="quick", provider="fake", models=(name,), out=tmp_path / "out")
    with pytest.raises(BenchError, match="bench_model_unconfigured"):
        await run_bench(fake, factory=counting, workspace_root=tmp_path / "ws")
    assert Counting.made == 0 and not (tmp_path / "out").exists()


class OverReportingText(FakeAdapter):
    """Reports more text usage than it quoted: the text ceiling is under-configured."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await super().complete(request)
        return response.model_copy(
            update={"tokens_in": response.tokens_in + 5000, "cost_usd": response.cost_usd + 1.0}
        )


async def test_a_text_call_over_its_reservation_is_a_closed_probe_code() -> None:
    """Section 10.5's codes are the two image codes and provider_*; nothing else."""
    [row] = await probe_models(
        lambda _: OverReportingText(),
        load_prices(),
        ["fake"],
        page=page(),
        providers={"fake": "fake"},
    )
    assert row.code == "provider_reservation_exceeded" and row.code in PROBE_CODES
    assert row.image_measured_tokens is None  # the image call is skipped
    assert {code for code in PROBE_CODES if not code.startswith("provider_")} == {
        "image_reservation_under_configured",
        "image_not_configured",
    }
    table = probe_table([row])
    assert "provider_reservation_exceeded: the text call used more than it reserved" in " ".join(
        table.splitlines()
    )
    assert all(len(line) <= 100 and line.isascii() for line in table.splitlines())
    clean = probe_table([row.model_copy(update={"code": None})])
    assert "provider_reservation_exceeded" not in clean


def test_probe_table_prints_the_evidence_label_next_to_every_row() -> None:
    def probe(model: str, provider: str) -> ProbeRow:
        return ProbeRow(
            model=model,
            provider=provider,
            image_configured=True,
            basis="measured",
            text_reserved_tokens=40,
            text_measured_tokens=20,
            image_reserved_tokens=1600,
            image_estimate_tokens=1590,
            image_measured_tokens=1500,
            reserved_usd=0.0123,
            measured_usd=0.0045,
            code=None,
        )

    rows = [
        probe("granite4.2:8b", "ollama"),
        probe("claude-haiku-4-5", "claude-code"),
        probe("gpt-4o-mini", "openai"),
        probe("fake", "fake"),
        probe("odd", "unknown"),
    ]
    lines = probe_table(rows).splitlines()
    assert lines[0].split()[:3] == ["model", "provider", "ev"]
    shown = {line.split()[0]: line.split()[2] for line in lines[2:7]}
    assert shown == {
        "granite4.2:8b": "local",
        "claude-haiku-4-5": "api-eq",
        "gpt-4o-mini": "billed",
        "fake": "fake",
        "odd": "-",
    }
    assert "evidence: live_api_equivalent, live_billed, local_zero_cost, synthetic_fake" in lines
    assert all(len(line) <= 100 and line.isascii() for line in lines)
    # A number too wide for its column is repeated in full, never shown clipped alone.
    wide = rows[0].model_copy(
        update={
            "image_reserved_tokens": 16000,
            "image_estimate_tokens": 15990,
            "image_measured_tokens": 16001,
        }
    )
    assert "  image r/e/m: 16000/15990/16001" in probe_table([wide]).splitlines()


def _two_model_profile(tmp_path: Path, routed: dict[str, object]) -> Profile:
    local = {"provider": "ollama", "prices": str(LOCAL_PRICES), "base_url": "http://127.0.0.1:9"}
    path = tmp_path / "route.json"
    path.write_text(
        json.dumps({"models": {"gemma4:26b": local, "granite4.2:8b": routed}}), encoding="utf-8"
    )
    return load_profile(path)


@pytest.mark.parametrize(
    "routed",
    [
        {"provider": "anthropic"},
        {"provider": "ollama", "prices": str(LOCAL_PRICES), "base_url": "https://ollama.invalid"},
    ],
)
def test_a_local_cell_cannot_route_a_step_off_this_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, routed: dict[str, object]
) -> None:
    """A pipeline step routed to a hosted model would make a local_zero_cost row false."""
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    profile = _two_model_profile(tmp_path, routed)
    config = BenchConfig(
        tier="quick", provider="profile", models=("gemma4:26b",), route={"vision": "granite4.2:8b"}
    )
    with pytest.raises(BenchError, match="bench_model_not_local"):
        resolve_models(config, profile)
    with pytest.raises(BenchError, match="bench_model_not_local"):
        bound(config, profile=profile)


def test_a_route_to_an_unconfigured_model_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    profile = ollama_profile(tmp_path, "gemma4:26b", base_url="http://127.0.0.1:9")
    config = BenchConfig(
        tier="quick", provider="profile", models=("gemma4:26b",), route={"vision": "absent:1"}
    )
    with pytest.raises(BenchError, match="bench_model_unconfigured"):
        resolve_models(config, profile)


def test_a_keyed_loopback_endpoint_is_not_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key on a loopback URL means a proxy to a hosted service."""
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-proxy")
    profile = ollama_profile(tmp_path, "gemma4:26b", base_url="http://127.0.0.1:9")
    config = BenchConfig(tier="quick", provider="profile", models=("gemma4:26b",))
    with pytest.raises(BenchError, match="bench_model_not_local"):
        resolve_models(config, profile)
    monkeypatch.delenv("OLLAMA_API_KEY")
    assert resolve_models(config, profile) == ("gemma4:26b",)


@pytest.mark.parametrize("name", ["gpt-oss:120b-cloud", "qwen3-coder:480b-cloud", "kimi:cloud"])
def test_ollama_cloud_models_are_not_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Ollama runs its "-cloud" models on its own servers even through the local daemon."""
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    prices = json.loads(LOCAL_PRICES.read_text(encoding="utf-8"))
    entry = dict(prices["models"]["gemma4:26b"], allowed_response_models=[name])
    table = tmp_path / "cloud-prices.json"
    table.write_text(json.dumps({**prices, "models": {name: entry}}), encoding="utf-8")
    profile = ollama_profile(tmp_path, name, base_url="http://127.0.0.1:9", prices=str(table))
    config = BenchConfig(tier="quick", provider="profile", models=(name,))
    with pytest.raises(BenchError, match="bench_model_not_local"):
        resolve_models(config, profile)
