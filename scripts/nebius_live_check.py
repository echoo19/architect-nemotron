"""Live check of NVIDIA Nemotron on Nebius Token Factory, and of what Architect makes of it.

One run sends about 40 small requests, one at a time:

- the matrix: Nano, Lightning, Super and Ultra, thinking off and on
  (``chat_template_kwargs.enable_thinking``), each asked a plain question, for a tool call
  (streamed, native ``tools``) and for a strict ``json_schema`` answer;
- a reply cut at ``max_tokens`` 64 while thinking, per model, through Architect's own
  ``nebius`` adapter (``complete`` and ``stream_chat``): it must show nothing, and stop
  with ``provider_reasoning_cut`` (``complete``) or as a cut reply, ``max_tokens``
  (``stream_chat``, so a caller can ask again);
- Super with ``reasoning_effort: "none"`` instead of the thinking switch;
- Ultra's ``force_nonempty_content`` (false, then true) on a reply cut while thinking, to
  see whether Token Factory passes that template argument through;
- ``service_tier: "flex"`` on each model.

Every raw reply is also replayed, offline, through ``OpenAICompatibleAdapter`` in the
``nebius`` dialect, so each row shows the provider's shape beside Architect's reading of
it. Spend is computed from each reply's own usage at Architect's bundled list prices
(``architect/models/prices.json``). Before each request its worst case (every input byte a
token, plus the whole ``max_tokens``) is checked against a hard cap of $0.05; a request
that could cross the cap is skipped, never sent.

    uv run python scripts/nebius_live_check.py            # needs NEBIUS_API_KEY
    uv run python scripts/nebius_live_check.py --dry-run  # the plan and worst cases, no key

The key is read from ``NEBIUS_API_KEY`` and only ever sent in the Authorization header. It
is never printed, logged or written: rows keep reply bodies and rate-limit headers only,
and the output is checked for the key before it is written to
``docs/feedback/probes/<date>-live-check.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import jsonschema

from architect.models import AdapterError, ModelRequest, OpenAICompatibleAdapter
from architect.models.chat import (
    ChatEvent,
    ChatMessage,
    DoneEvent,
    TextDeltaEvent,
    ToolCallEvent,
    ToolSpec,
    UsageEvent,
)
from architect.models.pricing import load_prices, price_for
from architect.session.codes import code_for

ROOT = Path(__file__).resolve().parents[1]
PROBES = ROOT / "docs" / "feedback" / "probes"
BASE_URL = "https://api.tokenfactory.nebius.com/v1"
URL = BASE_URL + "/chat/completions"
NANO = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
LIGHTNING = "nvidia/Nemotron-3_5-Lightning"
SUPER = "nvidia/nemotron-3-super-120b-a12b"
ULTRA = "nvidia/Nemotron-3-Ultra-550b-a55b"
MODELS = (NANO, LIGHTNING, SUPER, ULTRA)
SHORT = {NANO: "nano", LIGHTNING: "lightning", SUPER: "super", ULTRA: "ultra"}
HARD_CAP_USD = 0.05
OFF_TOKENS, ON_TOKENS, CUT_TOKENS = 512, 2048, 64
TIMEOUT_S, FLEX_TIMEOUT_S = 120.0, 180.0
OVERHEAD_TOKENS = 1024  # chat template and framing, as Architect's price entries reserve
HEAD = 160  # characters of model text kept per row

PROMPT = "What is 17 * 23? Reply with just the number."
ANSWER = "391"
CUT_PROMPT = "How many prime numbers are there between 100 and 200? Think it through."
SCHEMA_PROMPT = "Give the facts about the capital city of Japan."
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "city": {"type": "string"},
        "country": {"type": "string"},
        "population_millions": {"type": "number"},
        "continent": {
            "type": "string",
            "enum": ["Africa", "Asia", "Europe", "North America", "Oceania", "South America"],
        },
    },
    "required": ["city", "country", "population_millions", "continent"],
    "additionalProperties": False,
}
SYSTEM = "You are a helpful assistant. Call a tool when it can answer the question."
USER = "What's the weather in Paris right now?"
WEATHER = ToolSpec(
    "get_weather",
    "Get the current weather for a city.",
    {
        "type": "object",
        "properties": {"city": {"type": "string", "description": "The city name"}},
        "required": ["city"],
    },
)
TOOLS: list[Any] = [{"type": "function", "function": {
    "name": WEATHER.name, "description": WEATHER.description, "parameters": WEATHER.parameters,
}}]


# -- budget ---------------------------------------------------------------------------------


@dataclass
class Budget:
    """Spend at list prices, settled from each reply's usage. A reply without usage is
    charged its whole worst case; a refused request (an HTTP error) is free."""

    cap: float
    spent: float = 0.0
    calls: int = 0
    skipped: int = 0

    def allows(self, worst: float) -> bool:
        if self.spent + worst > self.cap:
            self.skipped += 1
            return False
        return True

    def settle(self, model: str, usage: dict[str, Any] | None, worst: float) -> float:
        self.calls += 1
        prompt = usage.get("prompt") if usage else None
        completion = usage.get("completion") if usage else None
        if isinstance(prompt, int) and isinstance(completion, int):
            usd = price_for(load_prices(), model).cost(prompt, completion)
        else:
            usd = worst
        self.spent += usd
        return round(usd, 8)


def worst_case(model: str, body: dict[str, Any]) -> float:
    tokens_in = len(json.dumps(body).encode("utf-8")) + OVERHEAD_TOKENS
    return price_for(load_prices(), model).cost(tokens_in, int(body["max_tokens"]))


# -- exchanges --------------------------------------------------------------------------------


@dataclass
class Exchange:
    status: int | None
    body: bytes = b""
    seconds: float = 0.0
    headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None


def _transport() -> httpx.AsyncBaseTransport:
    """Every connection this script opens (an offline test replaces it)."""
    return httpx.AsyncHTTPTransport(trust_env=False)


def _kept_headers(headers: httpx.Headers) -> dict[str, str]:
    """Rate-limit headers only: nothing else of a response is kept."""
    return {name: value for name, value in headers.items()
            if name.lower().startswith("x-ratelimit") or name.lower() == "retry-after"}


async def send(client: httpx.AsyncClient, body: dict[str, Any], seconds: float) -> Exchange:
    started = time.monotonic()
    try:
        if body["stream"]:
            raw = bytearray()
            async with client.stream("POST", URL, json=body, timeout=seconds) as response:
                async for part in response.aiter_bytes():
                    raw += part
            content = bytes(raw)
        else:
            response = await client.post(URL, json=body, timeout=seconds)
            content = response.content
    except httpx.HTTPError as exc:
        return Exchange(None, seconds=round(time.monotonic() - started, 2),
                        error=type(exc).__name__)
    return Exchange(response.status_code, content, round(time.monotonic() - started, 2),
                    _kept_headers(response.headers))


class Recorder(httpx.AsyncBaseTransport):
    """A real transport for Architect's own adapter that keeps each reply (status, decoded
    body, rate-limit headers); a request header is never read."""

    def __init__(self) -> None:
        self.exchanges: list[Exchange] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        inner = _transport()
        started = time.monotonic()
        response = await inner.handle_async_request(request)
        entry = Exchange(response.status_code, headers=_kept_headers(response.headers))
        self.exchanges.append(entry)
        encoding = response.headers.get("content-encoding")
        if not isinstance(response.stream, httpx.AsyncByteStream):
            raise TypeError("an async transport returns an async stream")
        source: httpx.AsyncByteStream = response.stream
        sink = bytearray()

        class Tee(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                async for part in source:
                    sink.extend(part)
                    yield part

            async def aclose(self) -> None:
                try:
                    await source.aclose()
                finally:
                    raw = bytes(sink)  # as sent: decode what the client decoded
                    entry.body = (httpx.Response(200, headers={"content-encoding": encoding},
                                                 content=raw).content if encoding else raw)
                    entry.seconds = round(time.monotonic() - started, 2)
                    await inner.aclose()

        return httpx.Response(response.status_code, headers=response.headers, stream=Tee(),
                              extensions=response.extensions)


# -- what Token Factory sent ------------------------------------------------------------------


def _usage(usage: Any) -> dict[str, Any] | None:
    if not isinstance(usage, dict):
        return None
    details = usage.get("completion_tokens_details")
    prompt_details = usage.get("prompt_tokens_details")
    return {
        "prompt": usage.get("prompt_tokens"),
        "completion": usage.get("completion_tokens"),
        "reasoning": details.get("reasoning_tokens") if isinstance(details, dict) else None,
        "cached": (prompt_details.get("cached_tokens") if isinstance(prompt_details, dict)
                   else None),
        "cache_hit": usage.get("prompt_cache_hit_tokens"),
    }


def _schema_ok(text: str | None) -> bool:
    try:
        jsonschema.validate(json.loads(text or ""), SCHEMA)
    except (ValueError, jsonschema.ValidationError):
        return False
    return True


def shape(exchange: Exchange, *, stream: bool, schema: bool) -> dict[str, Any]:
    """The reply as sent: where the text and the reasoning went, calls, usage."""
    if exchange.status != 200:
        return {"error_body": exchange.body[:300].decode("utf-8", "replace")}
    content: str | None = None
    reasoning: dict[str, int] = {}
    thought = ""  # the reasoning text, one field's worth
    calls: list[dict[str, Any]] = []
    repeated = False
    raw_usage: Any = None
    finish = model = tier = None
    if stream:
        pieces: list[str] = []
        merged: dict[int, dict[str, Any]] = {}
        for line in exchange.body.decode("utf-8", "replace").splitlines():
            if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                continue
            try:
                chunk = json.loads(line[5:])
            except ValueError:
                continue
            model = model or chunk.get("model")
            tier = tier or chunk.get("service_tier")
            if isinstance(chunk.get("usage"), dict):
                raw_usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                for key in ("reasoning", "reasoning_content"):
                    if isinstance(delta.get(key), str) and delta[key]:
                        reasoning[key] = reasoning.get(key, 0) + len(delta[key])
                piece = delta.get("reasoning_content") or delta.get("reasoning")
                thought += piece if isinstance(piece, str) else ""
                text = delta.get("content")
                if isinstance(text, str) and text:
                    if thought.strip() and text.strip() == thought.strip():
                        repeated = True  # the whole reasoning again, as content
                    pieces.append(text)
                for part in delta.get("tool_calls") or []:
                    call = merged.setdefault(part.get("index", 0),
                                             {"id": None, "name": None, "arguments": ""})
                    call["id"] = call["id"] or part.get("id")
                    function = part.get("function") or {}
                    call["name"] = call["name"] or function.get("name")
                    if isinstance(function.get("arguments"), str):
                        call["arguments"] += function["arguments"]
                finish = choice.get("finish_reason") or finish
        content = "".join(pieces)
        calls = [merged[index] for index in sorted(merged)]
    else:
        try:
            data = json.loads(exchange.body)
        except ValueError:
            return {"error_body": "not JSON"}
        model, tier, raw_usage = data.get("model"), data.get("service_tier"), data.get("usage")
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        finish = choice.get("finish_reason")
        content = message.get("content")
        for key in ("reasoning", "reasoning_content"):
            if isinstance(message.get(key), str) and message[key]:
                reasoning[key] = len(message[key])
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            calls.append({"id": call.get("id"), "name": function.get("name"),
                          "arguments": function.get("arguments")})
    text = content or ""
    reply: dict[str, Any] = {
        "model_echo": model,
        "finish": finish,
        "content": None if content is None else text[:HEAD],
        "content_chars": None if content is None else len(text),
        "leading_whitespace": bool(text) and text[:1].isspace(),
        "think_tag": "<think>" in text or "</think>" in text,
        "reasoning_chars": reasoning,
        "calls": calls,
        "usage": _usage(raw_usage),
        "service_tier": tier,
    }
    if repeated:
        reply["reasoning_repeated_as_content"] = True
    if schema:
        reply["content_matches_schema"] = _schema_ok(content)
    return reply


# -- what Architect makes of it (an offline replay) --------------------------------------------


async def _events(events: AsyncIterator[ChatEvent]) -> dict[str, Any]:
    shown: list[str] = []
    calls: list[dict[str, Any]] = []
    view: dict[str, Any] = {}
    try:
        async for event in events:
            if isinstance(event, TextDeltaEvent):
                shown.append(event.text)
            elif isinstance(event, ToolCallEvent):
                calls.append({"name": event.name, "arguments": event.arguments,
                              "error": event.error})
            elif isinstance(event, UsageEvent):
                view.update(tokens=[event.tokens_in, event.tokens_out], usd=event.cost_usd,
                            estimated=event.estimated)
            elif isinstance(event, DoneEvent):
                view["stop"] = event.stop_reason
    except AdapterError as exc:
        view["error"] = code_for(exc)
    view["text"] = "".join(shown)[:HEAD]
    view["calls"] = calls
    return view


async def architect_view(model: str, think: bool, kind: str, exchange: Exchange,
                         max_tokens: int) -> dict[str, Any]:
    """What ``OpenAICompatibleAdapter`` (``nebius`` dialect) returns for this exact reply.

    A refused request replays in the default dialect: the same failure, without the
    retries (and their waits) the ``nebius`` dialect would spend on 429, 503 and 529."""
    if exchange.status is None:
        return {}
    status = exchange.status
    replay = httpx.MockTransport(lambda request: httpx.Response(status, content=exchange.body))
    adapter = (
        OpenAICompatibleAdapter(model, api_key="replay", base_url=BASE_URL, dialect="nebius",
                                think=think, transport=replay) if status == 200
        else OpenAICompatibleAdapter(model, api_key="replay", base_url=BASE_URL,
                                     transport=replay)
    )
    if kind == "tool":
        return await _events(adapter.stream_chat(SYSTEM, (ChatMessage("user", USER),),
                                                 (WEATHER,), max_tokens))
    schema = SCHEMA if kind == "schema" else None
    prompt = {"schema": SCHEMA_PROMPT, "cut": CUT_PROMPT}.get(kind, PROMPT)
    try:
        response = await adapter.complete(ModelRequest(
            prompt=prompt, max_output_tokens=max_tokens, response_schema=schema))
    except AdapterError as exc:
        return {"error": code_for(exc)}
    return {"text": response.text[:HEAD], "tokens": [response.tokens_in, response.tokens_out],
            "usd": response.cost_usd, "schema_ok": _schema_ok(response.text) if schema else None}


# -- the run ------------------------------------------------------------------------------------


@dataclass
class Row:
    step: str
    model: str
    think: bool | None
    kind: str
    request: dict[str, Any]
    note: str = ""
    http: int | str | None = None
    seconds: float | None = None
    usd: float = 0.0
    ok: str = "-"
    reply: dict[str, Any] = field(default_factory=dict)
    architect: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)


def body_for(model: str, kind: str, think: bool | None, max_tokens: int,
             **extra: Any) -> dict[str, Any]:
    """A request shaped as Architect's ``nebius`` dialect shapes it (``models/http.py``)."""
    stream = kind == "tool"
    messages: list[Any] = (
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": USER}] if stream
        else [{"role": "user",
               "content": {"schema": SCHEMA_PROMPT, "cut": CUT_PROMPT}.get(kind, PROMPT)}]
    )
    body: dict[str, Any] = {"model": model, "messages": messages}
    # Thinking roles run at NVIDIA's recommended sampling; the rest at temperature 0.
    body.update({"temperature": 1.0, "top_p": 0.95} if think else {"temperature": 0.0})
    body["stream"] = stream
    if stream:
        body.update(stream_options={"include_usage": True}, tools=TOOLS)
    else:
        body["n"] = 1
    body["max_tokens"] = max_tokens
    if kind == "schema":
        body["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "response", "strict": True, "schema": SCHEMA}}
    if think is not None:
        body["chat_template_kwargs"] = {"enable_thinking": think}
    body.update(extra)
    return body


def verdict(row: Row) -> str:
    view = row.architect
    if row.kind in ("cut", "cut_stream") and row.step == "cut":
        stopped = view.get("error") == "provider_reasoning_cut" or (
            row.kind == "cut_stream" and "error" not in view and view.get("stop") == "max_tokens"
        )
        return "ok" if stopped and not view.get("text") else "miss"
    if row.step in ("effort_none", "force_nonempty") or (row.think and row.kind == "schema"):
        return "obs"  # what Token Factory does; Architect never sends these
    if not view or "error" in view:
        return "miss"
    if row.kind in ("plain", "flex"):
        return "ok" if ANSWER in str(view.get("text")) else "miss"
    if row.kind == "schema":
        return "ok" if view.get("schema_ok") else "miss"
    calls = view.get("calls") or []
    good = (len(calls) == 1 and calls[0]["name"] == WEATHER.name and calls[0]["error"] is None
            and "paris" in json.dumps(calls[0]["arguments"]).lower())
    return "ok" if good else "miss"


class Run:
    def __init__(self, client: httpx.AsyncClient | None, key: str, budget: Budget) -> None:
        self.client = client  # None: a dry run
        self.key = key
        self.budget = budget
        self.rows: list[Row] = []

    def _start(self, row: Row, worst: float) -> bool:
        self.rows.append(row)
        if self.client is None or not self.budget.allows(worst):
            row.http = "planned" if self.client is None else "skipped_cap"
            row.usd = round(worst, 8)
            return False
        return True

    async def raw(self, step: str, model: str, kind: str, think: bool | None,
                  max_tokens: int, *, note: str = "", seconds: float = TIMEOUT_S,
                  **extra: Any) -> None:
        sent_kind = "plain" if kind in ("flex", "effort") else kind
        body = body_for(model, sent_kind, think, max_tokens, **extra)
        request: dict[str, Any] = {key: body[key] for key in (
            "max_tokens", "temperature", "top_p", "stream", "chat_template_kwargs",
            "reasoning_effort", "service_tier") if key in body}
        if kind == "schema":
            request["response_format"] = "json_schema (strict)"
        if kind == "tool":
            request["tools"] = [WEATHER.name]
        row = Row(step, SHORT[model], think, kind, request, note)
        worst = worst_case(model, body)
        if not self._start(row, worst):
            return
        assert self.client is not None
        exchange = await send(self.client, body, seconds)
        row.http = exchange.status if exchange.status is not None else exchange.error
        row.seconds, row.headers = exchange.seconds, exchange.headers
        row.reply = shape(exchange, stream=body["stream"], schema=kind == "schema")
        row.architect = await architect_view(model, bool(think), sent_kind, exchange,
                                             max_tokens)
        billed = exchange.status == 200 or exchange.status is None
        row.usd = self.budget.settle(model, row.reply.get("usage"), worst if billed else 0.0)
        row.ok = verdict(row)

    async def cut(self, model: str, streamed: bool) -> None:
        """A thinking reply cut at 64 tokens, live through Architect's own adapter."""
        kind = "cut_stream" if streamed else "cut"
        row = Row("cut", SHORT[model], True, kind, {
            "max_tokens": CUT_TOKENS, "stream": streamed,
            "chat_template_kwargs": {"enable_thinking": True}, "via": "OpenAICompatibleAdapter"})
        recorder = Recorder()
        adapter = OpenAICompatibleAdapter(model, api_key=self.key or "unused", base_url=BASE_URL,
                                          dialect="nebius", think=True, transport=recorder)
        messages = (ChatMessage("user", CUT_PROMPT),)
        request = ModelRequest(prompt=CUT_PROMPT, max_output_tokens=CUT_TOKENS)
        quote = (adapter.quote_chat("", messages, (), CUT_TOKENS) if streamed
                 else adapter.quote(request))
        if not self._start(row, quote.cost_usd):
            return
        started = time.monotonic()
        if streamed:
            row.architect = await _events(adapter.stream_chat("", messages, (), CUT_TOKENS))
        else:
            try:
                row.architect = {"text": (await adapter.complete(request)).text[:HEAD]}
            except AdapterError as exc:
                row.architect = {"error": code_for(exc)}
        row.seconds = round(time.monotonic() - started, 2)
        for exchange in recorder.exchanges:  # retried answers (429, 503, 529) are not billed
            row.http, row.headers = exchange.status, exchange.headers
            row.reply = shape(exchange, stream=streamed, schema=False)
        row.usd = self.budget.settle(model, row.reply.get("usage"), quote.cost_usd)
        row.ok = verdict(row)


async def run(budget: Budget, key: str, dry_run: bool) -> Run:
    client = (None if dry_run else httpx.AsyncClient(
        transport=_transport(), headers={"Authorization": f"Bearer {key}"}, trust_env=False,
        follow_redirects=False))
    runner = Run(client, key, budget)
    try:
        for model in MODELS:
            for think in (False, True):
                for kind in ("plain", "tool", "schema"):
                    note = ("Architect sends schema calls with thinking off"
                            if think and kind == "schema" else "")
                    await runner.raw("matrix", model, kind, think,
                                     ON_TOKENS if think else OFF_TOKENS, note=note)
        for model in MODELS:
            for streamed in (False, True):
                await runner.cut(model, streamed)
        await runner.raw("effort_none", SUPER, "effort", None, OFF_TOKENS,
                         note="reasoning_effort none, no thinking switch",
                         reasoning_effort="none")
        for flag in (False, True):
            await runner.raw("force_nonempty", ULTRA, "cut", True, CUT_TOKENS,
                             note=f"force_nonempty_content {str(flag).lower()}",
                             chat_template_kwargs={"enable_thinking": True,
                                                   "force_nonempty_content": flag})
        for model in MODELS:
            await runner.raw("flex", model, "flex", False, OFF_TOKENS, seconds=FLEX_TIMEOUT_S,
                             service_tier="flex")
    finally:
        if client is not None:
            await client.aclose()
    return runner


# -- output -------------------------------------------------------------------------------------


def _ascii(value: object, width: int) -> str:
    text = str(value).encode("ascii", "backslashreplace").decode("ascii").replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "~"


def _architect_cell(row: Row) -> str:
    view = row.architect
    if not view:
        return "-"
    if "error" in view:
        return str(view["error"])
    if row.kind == "tool":
        calls = view.get("calls") or []
        if calls:
            return f"{calls[0]['name']}({json.dumps(calls[0]['arguments'])})"
        return "no call: " + repr(view.get("text", ""))
    if row.kind == "schema":
        return "valid json" if view.get("schema_ok") else "invalid: " + repr(view.get("text"))
    return repr(view.get("text", ""))


def table(rows: Sequence[Row]) -> str:
    head = (f"{'step':<14} {'model':<9} {'think':<5} {'kind':<10} {'http':<11} "
            f"{'finish':<10} {'reasoning':<12} {'out/rsn':<9} {'sec':>5} {'usd':>9} "
            f"{'ok':<4} architect")
    lines = [head, "-" * len(head)]
    for row in rows:
        usage = row.reply.get("usage") or {}
        out = f"{usage.get('completion')}/{usage.get('reasoning')}" if usage else "-"
        fields = row.reply.get("reasoning_chars") or {}
        where = "+".join("rc" if key == "reasoning_content" else "r" for key in sorted(fields))
        if row.reply.get("reasoning_repeated_as_content"):
            where += "+again"
        think = "-" if row.think is None else ("on" if row.think else "off")
        line = (f"{row.step:<14} {row.model:<9} {think:<5} {row.kind:<10} "
                f"{_ascii(row.http, 11):<11} {_ascii(row.reply.get('finish') or '-', 10):<10} "
                f"{where or '-':<12} {_ascii(out, 9):<9} {row.seconds or 0.0:>5.1f} "
                f"{row.usd:>9.6f} {row.ok:<4} {_ascii(_architect_cell(row), 44)}")
        lines.append(line + (f"  ({_ascii(row.note, 48)})" if row.note else ""))
    return "\n".join(lines)


def _describe() -> str:
    try:
        done = subprocess.run(["git", "describe", "--always", "--dirty"], cwd=ROOT,
                              capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return done.stdout.strip() or "unknown"


def _out_path(given: str | None, day: str) -> Path:
    if given:
        return Path(given).resolve()
    path = PROBES / f"{day}-live-check.json"
    number = 2
    while path.exists():  # a second run on the same day never overwrites the first
        path = PROBES / f"{day}-live-check-{number}.json"
        number += 1
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--cap", type=float, default=HARD_CAP_USD,
                        help="spend cap in USD, at most the hard cap of 0.05")
    parser.add_argument("--out", help="the output JSON (default: docs/feedback/probes/)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan and each request's worst case; send nothing")
    args = parser.parse_args(argv)
    if not 0 < args.cap <= HARD_CAP_USD:
        parser.error(f"--cap must be above 0 and at most {HARD_CAP_USD}")
    key = os.environ.get("NEBIUS_API_KEY", "").strip()
    if not key and not args.dry_run:
        print("NEBIUS_API_KEY is not set; nothing was sent.", file=sys.stderr)
        return 2
    started = datetime.now(UTC)
    budget = Budget(args.cap)
    runner = asyncio.run(run(budget, key, args.dry_run))
    print(table(runner.rows))
    if args.dry_run:
        worst = sum(row.usd for row in runner.rows)
        print(f"\n{len(runner.rows)} requests planned; ${worst:.6f} if each hit its worst case")
        return 0
    print(f"\n{budget.calls} calls, ${budget.spent:.6f} spent at list prices (cap "
          f"${budget.cap:.2f}); {budget.skipped} skipped at the cap")
    prices = load_prices()
    result = {
        "check": "scripts/nebius_live_check.py",
        "started_utc": started.isoformat(timespec="seconds"),
        "architect": _describe(),
        "base_url": BASE_URL,
        "prices_usd_per_million": {
            SHORT[model]: [price_for(prices, model).input_per_million,
                           price_for(prices, model).output_per_million] for model in MODELS},
        "spend_note": "each reply's own usage at these list prices; flex is assumed the same",
        "cap_usd": budget.cap,
        "spent_usd": round(budget.spent, 6),
        "calls": budget.calls,
        "skipped_at_cap": budget.skipped,
        "rows": [dataclasses.asdict(row) for row in runner.rows],
    }
    text = json.dumps(result, indent=1, ensure_ascii=False)
    if key in text:
        print("Refusing to write: the output would contain the key.", file=sys.stderr)
        return 3
    path = _out_path(args.out, started.astimezone().date().isoformat())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8", newline="\n")
    shown = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
    print(f"wrote {shown.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
