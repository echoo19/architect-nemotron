import RedisMock from "ioredis-mock";
import { afterAll, describe, expect, it } from "vitest";
import { parseConfig } from "../src/config.js";
import { prepareRequest } from "../src/request.js";
import { FileLedger } from "../src/ledger/file.js";
import type { Ledger, RelayEvent } from "../src/ledger/types.js";
import { UpstashLedger } from "../src/ledger/upstash.js";
import { usdToNano } from "../src/money.js";
import { IoredisMockAdapter } from "./ioredis-adapter.js";
import {
  CONFIG_TEXT,
  FakeUpstream,
  LIGHTNING,
  SUPER,
  ULTRA,
  UPSTREAM_KEY,
  chatRequest,
  chunks,
  cleanupDirs,
  completion,
  config,
  fileLedger,
  getRequest,
  jsonReply,
  makeRelay,
  messages,
  mint,
  sseData,
  sseReply,
  usage,
  waitFor,
} from "./helpers.js";

afterAll(() => cleanupDirs());

async function setup(options: { globalCap?: string; total?: string; daily?: string; freezePct?: number } = {}) {
  const ledger = fileLedger("handler");
  await ledger.setGlobalCap(usdToNano(options.globalCap ?? "10"));
  const minted = await mint(ledger, options);
  const upstream = new FakeUpstream();
  return { ledger, upstream, ...minted };
}

async function lastEvent(ledger: FileLedger): Promise<RelayEvent | undefined> {
  const report = await ledger.report({ days: [], events: 1, nowMs: Date.now() });
  return report.events[0];
}

async function eventually(check: () => Promise<boolean>, timeoutMs = 5_000): Promise<void> {
  const start = Date.now();
  while (!(await check())) {
    if (Date.now() - start > timeoutMs) throw new Error("eventually: timed out");
    await new Promise((r) => setTimeout(r, 10));
  }
}

describe("routes and access codes", () => {
  it("reports health, and why it is down", async () => {
    const { ledger, upstream } = await setup();
    const ok = await makeRelay({ ledger, fetch: upstream.fetch }).handle(getRequest("/healthz", null));
    expect(ok.status).toBe(200);
    expect(await ok.json()).toMatchObject({ ok: true, ledger: "file", price_date: "2026-10-03" });
    const unset = fileLedger("unset");
    const noCap = await makeRelay({ ledger: unset, fetch: upstream.fetch }).handle(getRequest("/healthz", null));
    expect(noCap.status).toBe(503);
    expect(await noCap.json()).toMatchObject({ ok: false, reason: "the global cap is not set" });
    const off = await makeRelay({ ledger, fetch: upstream.fetch, disabled: true }).handle(getRequest("/healthz", null));
    expect(off.status).toBe(503);
    const noKey = await makeRelay({ ledger, fetch: upstream.fetch, upstreamKey: null }).handle(getRequest("/healthz", null));
    expect(await noKey.json()).toMatchObject({ ok: false, reason: "NEBIUS_API_KEY is not set" });
    const noLedger = await makeRelay({ ledger: null, ledgerError: "UPSTASH_REDIS_REST_URL missing", fetch: upstream.fetch }).handle(
      getRequest("/healthz", null),
    );
    expect((await noLedger.json()) as object).toMatchObject({ ok: false, reason: "no ledger: UPSTASH_REDIS_REST_URL missing" });
  });

  it("refuses missing, malformed, unknown and revoked codes with 401 before any upstream call", async () => {
    const { ledger, upstream, code, hash } = await setup();
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    const body = { model: SUPER, messages };
    const cases: [string | null, string][] = [
      [null, "missing_code"],
      ["sk-not-a-relay-code", "unknown_code"],
      [`rc_${"A".repeat(32)}`, "unknown_code"],
    ];
    for (const [given, why] of cases) {
      const res = await relay.handle(chatRequest(given, body));
      expect(res.status).toBe(401);
      expect(((await res.json()) as { error: { code: string } }).error.code).toBe(why);
    }
    await ledger.updateCode(hash, { revoked: true });
    const revoked = await relay.handle(chatRequest(code, body));
    expect(revoked.status).toBe(401);
    expect(((await revoked.json()) as { error: { code: string } }).error.code).toBe("code_revoked");
    expect(upstream.calls).toHaveLength(0);
  });

  it("answers unknown routes and wrong methods without touching the ledger", async () => {
    const { ledger, upstream, code } = await setup();
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    expect((await relay.handle(getRequest("/v1/embeddings", code))).status).toBe(404);
    expect((await relay.handle(getRequest("/v1/chat/completions", code))).status).toBe(405);
    // Vercel's rewrite may deliver the original path as ?route= or as its named parameter
    expect((await relay.handle(getRequest("/api/relay?route=v1/models", code))).status).toBe(200);
    expect((await relay.handle(getRequest("/api/relay?path=models", code))).status).toBe(200);
    expect((await relay.handle(getRequest("/api/relay?route=healthz", null))).status).toBe(200);
  });

  it("lists the four Nemotron models with per-token prices and clamps", async () => {
    const { ledger, upstream, code } = await setup();
    const res = await makeRelay({ ledger, fetch: upstream.fetch }).handle(getRequest("/v1/models", code));
    const body = (await res.json()) as { data: { id: string; pricing: Record<string, string>; top_provider: Record<string, number> }[] };
    expect(body.data.map((m) => m.id)).toEqual([...config.models.keys()]);
    const ultra = body.data.find((m) => m.id === ULTRA)!;
    expect(ultra.pricing).toEqual({ prompt: "0.000001", completion: "0.000003" });
    expect(ultra.top_provider.max_completion_tokens).toBe(32768);
    expect((await makeRelay({ ledger, fetch: upstream.fetch }).handle(getRequest("/v1/models", null))).status).toBe(401);
  });

  it("shows a code its balance and the cap that binds", async () => {
    const { ledger, upstream, code } = await setup({ total: "2", daily: "0.5" });
    const res = await makeRelay({ ledger, fetch: upstream.fetch }).handle(getRequest("/v1/relay/balance", code));
    expect(res.status).toBe(200);
    expect(await res.json()).toMatchObject({ remaining_usd: 0.5, limited_by: "daily_cap", cap_total_usd: 2, today: { cap_daily_usd: 0.5 } });
  });
});

describe("model allowlist and kill switch", () => {
  it("rejects non-nvidia models with 403 and never calls upstream", async () => {
    const { ledger, upstream, code, hash } = await setup();
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    for (const model of ["gpt-4o-mini", "deepseek-ai/DeepSeek-V4.1-Flash", "moonshotai/Kimi-K3"]) {
      const res = await relay.handle(chatRequest(code, { model, messages }));
      expect(res.status).toBe(403);
      expect(((await res.json()) as { error: { type: string } }).error.type).toBe("relay_model_not_allowed");
    }
    expect(upstream.calls).toHaveLength(0);
    expect(await ledger.getCode(hash)).toMatchObject({ spent: 0, reserved: 0 });
  });

  it("returns 503 for every call when RELAY_DISABLED is set", async () => {
    const { ledger, upstream, code } = await setup();
    const relay = makeRelay({ ledger, fetch: upstream.fetch, disabled: true });
    const res = await relay.handle(chatRequest(code, { model: SUPER, messages }));
    expect(res.status).toBe(503);
    expect(((await res.json()) as { error: { type: string } }).error.type).toBe("relay_disabled");
    expect((await relay.handle(getRequest("/v1/models", code))).status).toBe(503);
    expect(upstream.calls).toHaveLength(0);
  });

  it("returns 503 when the ledger's kill switch is on, and serves again when it is off", async () => {
    const { ledger, upstream, code, hash } = await setup();
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    await ledger.setDisabled(true);
    expect((await relay.handle(chatRequest(code, { model: SUPER, messages }))).status).toBe(503);
    expect(upstream.calls).toHaveLength(0);
    expect(await ledger.getCode(hash)).toMatchObject({ reserved: 0, spent: 0 });
    await ledger.setDisabled(false);
    expect((await relay.handle(chatRequest(code, { model: SUPER, messages, max_tokens: 8 }))).status).toBe(200);
  });
});

describe("what reaches Token Factory", () => {
  it("sends the cleaned body with the relay's key, never the access code", async () => {
    const { ledger, upstream, code } = await setup();
    upstream.handler = () => jsonReply(completion(ULTRA, usage(20, 5)));
    const res = await makeRelay({ ledger, fetch: upstream.fetch }).handle(
      chatRequest(code, {
        model: ULTRA,
        messages,
        best_of: 3,
        ignore_eos: true,
        max_completion_tokens: 99999,
        logit_bias: { 1: 5 },
        n: 2,
        top_k: 1,
        chat_template_kwargs: { enable_thinking: false, force_nonempty_content: true },
      }),
    );
    expect(res.status).toBe(200);
    const call = upstream.calls[0]!;
    expect(call.url).toBe("https://upstream.test/v1/chat/completions");
    expect(call.headers.get("authorization")).toBe(`Bearer ${UPSTREAM_KEY}`);
    expect(JSON.stringify(Object.fromEntries(call.headers))).not.toContain(code);
    expect(call.body).toEqual({ model: ULTRA, messages, max_tokens: 32768, n: 1, chat_template_kwargs: { enable_thinking: false } });
    expect(res.headers.get("x-relay-max-tokens")).toBe("32768");
    expect(res.headers.get("x-relay-dropped")!.split(",").sort()).toEqual(
      ["best_of", "chat_template_kwargs.force_nonempty_content", "ignore_eos", "logit_bias", "max_completion_tokens", "n", "top_k"].sort(),
    );
  });

  it("forces include_usage on streams", async () => {
    const { ledger, upstream, code } = await setup();
    upstream.handler = () => sseReply(chunks(SUPER, usage(10, 3)));
    const res = await makeRelay({ ledger, fetch: upstream.fetch }).handle(chatRequest(code, { model: SUPER, messages, stream: true }));
    await res.text();
    expect(upstream.calls[0]!.body).toMatchObject({ stream: true, stream_options: { include_usage: true }, max_tokens: 8192 });
  });
});

describe("headers and statuses pass through", () => {
  it("passes x-ratelimit-*, x-request-id and the completion id, adds x-upstream-ms, keeps the body byte for byte", async () => {
    const { ledger, upstream, code } = await setup();
    const reply = completion(SUPER, usage(30, 7), "chatcmpl-abc123");
    upstream.handler = () =>
      jsonReply(reply, {
        headers: {
          "x-ratelimit-remaining-requests": "299",
          "x-ratelimit-limit-tokens": "200000",
          "x-request-id": "req-77",
          "set-cookie": "nope=1",
          server: "upstream",
        },
      });
    const res = await makeRelay({ ledger, fetch: upstream.fetch }).handle(chatRequest(code, { model: SUPER, messages, max_tokens: 64 }));
    expect(res.status).toBe(200);
    expect(res.headers.get("x-ratelimit-remaining-requests")).toBe("299");
    expect(res.headers.get("x-ratelimit-limit-tokens")).toBe("200000");
    expect(res.headers.get("x-request-id")).toBe("req-77");
    expect(res.headers.get("set-cookie")).toBeNull();
    expect(res.headers.get("server")).toBeNull();
    expect(res.headers.get("x-completion-id")).toBe("chatcmpl-abc123");
    expect(Number(res.headers.get("x-upstream-ms"))).toBeGreaterThanOrEqual(0);
    expect(res.headers.get("x-relay-cost-usd")).toBe("0.0000153"); // 30 * 300 + 7 * 900 = 15,300 n$
    expect(await res.text()).toBe(JSON.stringify(reply));
  });

  it("passes an upstream 429 with Retry-After and releases the hold", async () => {
    const { ledger, upstream, code, hash } = await setup();
    const body = { error: { message: "Rate limit exceeded", type: "rate_limit" } };
    upstream.handler = () =>
      jsonReply(body, { status: 429, headers: { "retry-after": "7", "x-ratelimit-remaining-tokens": "0" } });
    const res = await makeRelay({ ledger, fetch: upstream.fetch }).handle(chatRequest(code, { model: LIGHTNING, messages }));
    expect(res.status).toBe(429);
    expect(res.headers.get("retry-after")).toBe("7");
    expect(res.headers.get("x-ratelimit-remaining-tokens")).toBe("0");
    expect(res.headers.get("x-upstream-ms")).not.toBeNull();
    expect(await res.json()).toEqual(body);
    expect(await ledger.getCode(hash)).toMatchObject({ spent: 0, reserved: 0, requests: 1 });
    expect(await lastEvent(ledger)).toMatchObject({ kind: "released", status: 429, actual: 0 });
  });

  it("passes 404 (a retired model) and 503 through; maps the relay's own key or funds trouble", async () => {
    const { ledger, upstream, code, hash } = await setup();
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    for (const status of [404, 503, 400]) {
      upstream.handler = () => jsonReply({ error: { message: `upstream ${status}` } }, { status });
      const res = await relay.handle(chatRequest(code, { model: SUPER, messages, max_tokens: 8 }));
      expect(res.status).toBe(status);
      expect(await res.json()).toEqual({ error: { message: `upstream ${status}` } });
    }
    upstream.handler = () => jsonReply({ detail: "Invalid API key" }, { status: 401 });
    const auth = await relay.handle(chatRequest(code, { model: SUPER, messages, max_tokens: 8 }));
    expect(auth.status).toBe(502);
    expect(await auth.json()).toMatchObject({ error: { type: "upstream_auth", upstream_status: 401 } });
    upstream.handler = () => jsonReply({ detail: "budget exhausted" }, { status: 402 });
    const broke = await relay.handle(chatRequest(code, { model: SUPER, messages, max_tokens: 8 }));
    expect(broke.status).toBe(503);
    expect(await broke.json()).toMatchObject({ error: { type: "upstream_budget", upstream_status: 402 } });
    expect(await ledger.getCode(hash)).toMatchObject({ spent: 0, reserved: 0 });
  });
});

describe("cap math", () => {
  it("returns 402 with the dollars left when the worst case does not fit, before calling upstream", async () => {
    const { ledger, upstream, code } = await setup({ total: "0.001" });
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    // Lightning with no max_tokens holds 8192 * $0.24/1M = $0.00196608 of output alone.
    const res = await relay.handle(chatRequest(code, { model: LIGHTNING, messages }));
    expect(res.status).toBe(402);
    const body = (await res.json()) as { error: Record<string, unknown> };
    expect(body.error).toMatchObject({ type: "relay_cap_exceeded", code: "total_cap", remaining_usd: 0.001, cap_usd: 0.001 });
    expect(body.error.needed_usd as number).toBeGreaterThan(0.00196608);
    expect(String(body.error.message)).toMatch(/\$0\.001 left of its \$0\.001 total/);
    expect(upstream.calls).toHaveLength(0);
    // A small max_tokens fits.
    upstream.handler = () => jsonReply(completion(LIGHTNING, usage(40, 4)));
    expect((await relay.handle(chatRequest(code, { model: LIGHTNING, messages, max_tokens: 16 }))).status).toBe(200);
  });

  it("names the daily cap and when it resets, and the global cap", async () => {
    const { ledger, upstream, code } = await setup({ total: "5", daily: "0.001" });
    const res = await makeRelay({ ledger, fetch: upstream.fetch }).handle(chatRequest(code, { model: ULTRA, messages }));
    expect(res.status).toBe(402);
    const body = (await res.json()) as { error: Record<string, unknown> };
    expect(body.error.code).toBe("daily_cap");
    expect(String(body.error.resets_at)).toMatch(/T00:00:00\.000Z$/);
    const tight = await setup({ globalCap: "0.0001", total: "5" });
    const global = await makeRelay({ ledger: tight.ledger, fetch: tight.upstream.fetch }).handle(
      chatRequest(tight.code, { model: SUPER, messages }),
    );
    expect(global.status).toBe(402);
    expect(((await global.json()) as { error: { code: string } }).error.code).toBe("global_cap");
  });

  it("settles from usage with reasoning counted once (Ultra, thinking on)", async () => {
    const { ledger, upstream, code, hash } = await setup();
    upstream.handler = () =>
      jsonReply(completion(ULTRA, { prompt_tokens: 1000, completion_tokens: 9709, total_tokens: 10709, completion_tokens_details: { reasoning_tokens: 9338 } }));
    const res = await makeRelay({ ledger, fetch: upstream.fetch }).handle(
      chatRequest(code, { model: ULTRA, messages, chat_template_kwargs: { enable_thinking: true } }),
    );
    expect(res.status).toBe(200);
    expect(res.headers.get("x-relay-cost-usd")).toBe("0.030127");
    expect(await ledger.getCode(hash)).toMatchObject({ spent: 1000 * 1000 + 9709 * 3000, reserved: 0 });
    expect(await lastEvent(ledger)).toMatchObject({ kind: "usage", in: 1000, out: 9709, rsn: 9338, actual: 30_127_000 });
  });

  it("settles a stream from its usage chunk, and strips that chunk unless the client asked", async () => {
    const { ledger, upstream, code, hash } = await setup();
    upstream.handler = () => sseReply(chunks(SUPER, usage(412, 58, 21)));
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    const res = await relay.handle(chatRequest(code, { model: SUPER, messages, stream: true }));
    expect(res.headers.get("content-type")).toContain("text/event-stream");
    const events = sseData(await res.text());
    expect(events.at(-1)).toBe("[DONE]");
    expect(events.some((e) => typeof e === "object" && (e as { usage?: unknown }).usage)).toBe(false);
    expect(await ledger.getCode(hash)).toMatchObject({ spent: 412 * 300 + 58 * 900, reserved: 0 });
    const asked = await relay.handle(
      chatRequest(code, { model: SUPER, messages, stream: true, stream_options: { include_usage: true } }),
    );
    const withUsage = sseData(await asked.text());
    expect(withUsage.some((e) => typeof e === "object" && (e as { usage?: unknown }).usage)).toBe(true);
    expect(await lastEvent(ledger)).toMatchObject({ kind: "usage", completion: "chatcmpl-stream-1", stream: true });
  });
});

describe("stream framing", () => {
  it("forwards a stream that arrives a few bytes at a time, byte for byte", async () => {
    const { ledger, upstream, code, hash } = await setup();
    const text = chunks(SUPER, usage(20, 4))
      .map((e) => `data: ${typeof e === "string" ? e : JSON.stringify(e)}\n\n`)
      .join("");
    upstream.handler = () => {
      const bytes = new TextEncoder().encode(text);
      let at = 0;
      const body = new ReadableStream<Uint8Array>({
        pull(controller) {
          if (at >= bytes.length) return controller.close();
          controller.enqueue(bytes.slice(at, at + 5));
          at += 5;
        },
      });
      return new Response(body, { headers: { "content-type": "text/event-stream" } });
    };
    const res = await makeRelay({ ledger, fetch: upstream.fetch }).handle(
      chatRequest(code, { model: SUPER, messages, stream: true, stream_options: { include_usage: true } }),
    );
    expect(await res.text()).toBe(text);
    expect(await ledger.getCode(hash)).toMatchObject({ spent: 20 * 300 + 4 * 900, reserved: 0 });
  });
});

describe("streams that break", () => {
  it("settles at the full reservation when the stream breaks before usage", async () => {
    const { ledger, upstream, code, hash } = await setup();
    upstream.handler = (call) => sseReply(chunks(SUPER, usage(10, 10)), { breakAfter: 2, signal: call.signal });
    const res = await makeRelay({ ledger, fetch: upstream.fetch }).handle(chatRequest(code, { model: SUPER, messages, stream: true }));
    const events = sseData(await res.text());
    expect(events).toHaveLength(3);
    expect(events[2]).toMatchObject({ error: { type: "upstream_broken" } });
    const event = (await lastEvent(ledger))!;
    expect(event).toMatchObject({ kind: "full", why: "stream_broken" });
    expect(event.actual).toBe(event.reserved);
    expect(await ledger.getCode(hash)).toMatchObject({ spent: event.reserved, reserved: 0 });
  });

  it("settles at the full reservation when a stream or a reply carries no usage", async () => {
    const { ledger, upstream, code } = await setup();
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    upstream.handler = () => sseReply(chunks(SUPER, null));
    await (await relay.handle(chatRequest(code, { model: SUPER, messages, stream: true }))).text();
    const streamed = (await lastEvent(ledger))!;
    expect(streamed).toMatchObject({ kind: "full", why: "no_usage" });
    expect(streamed.actual).toBe(streamed.reserved);
    upstream.handler = () => jsonReply({ id: "x", choices: [] });
    await (await relay.handle(chatRequest(code, { model: SUPER, messages, max_tokens: 10 }))).text();
    expect(await lastEvent(ledger)).toMatchObject({ kind: "full", why: "no_usage" });
  });

  it("stops the upstream call and settles in full when the client hangs up", async () => {
    const { ledger, upstream, code, hash } = await setup();
    upstream.handler = (call) => sseReply(chunks(SUPER, usage(1, 1)), { hangAfter: 2, signal: call.signal });
    const client = new AbortController();
    const res = await makeRelay({ ledger, fetch: upstream.fetch }).handle(
      chatRequest(code, { model: SUPER, messages, stream: true }, { signal: client.signal }),
    );
    const reader = res.body!.getReader();
    await reader.read();
    client.abort();
    await reader.cancel();
    await waitFor(() => upstream.calls[0]!.signal!.aborted);
    await eventually(async () => (await ledger.getCode(hash))!.reserved === 0);
    const code1 = await ledger.getCode(hash);
    expect(code1!.reserved).toBe(0);
    const event = (await lastEvent(ledger))!;
    expect(event).toMatchObject({ kind: "full", why: "client_closed" });
    expect(code1!.spent).toBe(event.reserved);
  });

  it("ends a call at the per-call time limit and settles it in full", async () => {
    const { ledger, upstream, code } = await setup();
    const relay = makeRelay({ ledger, fetch: upstream.fetch, maxCallMs: 1 }); // floored to 1 s
    upstream.handler = (call) => sseReply(chunks(SUPER, usage(1, 1)), { hangAfter: 1, signal: call.signal });
    const streamed = sseData(await (await relay.handle(chatRequest(code, { model: SUPER, messages, stream: true }))).text());
    expect(streamed.at(-1)).toMatchObject({ error: { type: "relay_timeout" } });
    expect(await lastEvent(ledger)).toMatchObject({ kind: "full", why: "time_limit" });
    upstream.handler = (call) =>
      new Promise<Response>((_, reject) => call.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))));
    const whole = await relay.handle(chatRequest(code, { model: SUPER, messages, max_tokens: 8 }));
    expect(whole.status).toBe(504);
    expect(await lastEvent(ledger)).toMatchObject({ kind: "full", why: "time_limit" });
  });

  it("releases the hold when Token Factory cannot be reached at all, and holds it when the line drops", async () => {
    const { ledger, upstream, code, hash } = await setup();
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    upstream.handler = () => {
      throw Object.assign(new TypeError("fetch failed"), { cause: { code: "ECONNREFUSED" } });
    };
    expect((await relay.handle(chatRequest(code, { model: SUPER, messages, max_tokens: 8 }))).status).toBe(502);
    expect(await ledger.getCode(hash)).toMatchObject({ spent: 0, reserved: 0 });
    upstream.handler = () => {
      throw Object.assign(new TypeError("fetch failed"), { cause: { code: "ECONNRESET" } });
    };
    expect((await relay.handle(chatRequest(code, { model: SUPER, messages, max_tokens: 8 }))).status).toBe(502);
    const event = (await lastEvent(ledger))!;
    expect(event).toMatchObject({ kind: "full", why: "fetch_failed" });
    expect(await ledger.getCode(hash)).toMatchObject({ spent: event.reserved, reserved: 0 });
  });
});

describe("per-model limiter", () => {
  function tightConfig(rpm: number) {
    const raw = JSON.parse(CONFIG_TEXT);
    raw.limiter_fraction = 0.5;
    raw.models[LIGHTNING].rpm = rpm * 2; // the relay budgets half
    raw.code_share_pct = 100; // one code may use the model's whole budget here
    return parseConfig(raw);
  }

  /**
   * A Lightning budget with room for one call's hold plus `spare` tokens. The prompt is
   * long enough that its hold covers 10,000 reported prompt tokens.
   */
  function tokenConfig(spare: number, countsCached?: boolean) {
    const raw = JSON.parse(CONFIG_TEXT);
    const long = [{ role: "user", content: "word ".repeat(2_500) }];
    const body = { model: LIGHTNING, messages: long, max_tokens: 4 };
    const prepared = prepareRequest(body, parseConfig(raw));
    if (!prepared.ok) throw new Error("the test request does not prepare");
    raw.limiter_fraction = 1;
    raw.models[LIGHTNING].tpm = prepared.value.inputTokensLikely + prepared.value.maxTokens + spare;
    raw.code_share_pct = 100;
    if (countsCached !== undefined) raw.limiter_counts_cached_tokens = countsCached;
    return { config: parseConfig(raw), body };
  }

  it("counts only the prompt tokens that missed the cache, plus the completion", async () => {
    const minute = Math.floor(Date.now() / 60_000) * 60_000 + 30_000; // mid-minute, fixed
    for (const countsCached of [undefined, false, true]) {
      const { ledger, upstream, code } = await setup();
      // 10,000 prompt tokens, 9,800 of them served from the prompt cache, and 50 out.
      upstream.handler = () =>
        jsonReply(completion(LIGHTNING, usage(10_000, 50, undefined, { prompt_tokens_details: { cached_tokens: 9_800 } })));
      const { config: tight, body } = tokenConfig(1_000, countsCached);
      const relay = makeRelay({ ledger, fetch: upstream.fetch, config: tight, now: () => minute });
      expect((await relay.handle(chatRequest(code, body))).status).toBe(200);
      const second = await relay.handle(chatRequest(code, body));
      if (countsCached) {
        // 10,050 tokens now fill the window: no room for a second hold.
        expect(second.status).toBe(429);
        expect(await second.json()).toMatchObject({ error: { type: "relay_rate_limited", code: expect.stringMatching(/tpm$/) } });
      } else {
        // 250 tokens count, inside the 1,000 spare: the second call goes through.
        expect(second.status).toBe(200);
        await second.text();
      }
      expect(upstream.calls).toHaveLength(countsCached ? 1 : 2);
    }
  });

  it("names the limiter's share of Token Factory's limit when it refuses", async () => {
    const { ledger, upstream, code } = await setup();
    upstream.handler = () => jsonReply(completion(LIGHTNING, usage(5, 1)));
    const raw = JSON.parse(CONFIG_TEXT);
    raw.models[LIGHTNING].rpm = 2; // 90% of 2 rounds down to 1 call a minute
    const relay = makeRelay({ ledger, fetch: upstream.fetch, config: parseConfig(raw) });
    expect((await relay.handle(chatRequest(code, { model: LIGHTNING, messages, max_tokens: 4 }))).status).toBe(200);
    const refused = await relay.handle(chatRequest(code, { model: LIGHTNING, messages, max_tokens: 4 }));
    expect(refused.status).toBe(429);
    const body = (await refused.json()) as { error: { message: string } };
    expect(body.error.message).toContain("(90% of Token Factory's limit)");
  });

  it("answers 429 with Retry-After at half the provider's RPM", async () => {
    const { ledger, upstream, code } = await setup();
    upstream.handler = () => jsonReply(completion(LIGHTNING, usage(5, 1)));
    const relay = makeRelay({ ledger, fetch: upstream.fetch, config: tightConfig(2) });
    for (let i = 0; i < 2; i += 1) expect((await relay.handle(chatRequest(code, { model: LIGHTNING, messages, max_tokens: 4 }))).status).toBe(200);
    const refused = await relay.handle(chatRequest(code, { model: LIGHTNING, messages, max_tokens: 4 }));
    expect(refused.status).toBe(429);
    expect(Number(refused.headers.get("retry-after"))).toBeGreaterThanOrEqual(1);
    expect(await refused.json()).toMatchObject({ error: { type: "relay_rate_limited", code: "rpm" } });
    expect(upstream.calls).toHaveLength(2);
  });

  it("waits briefly for the window instead of failing when allowed to", async () => {
    const { ledger, upstream, code } = await setup();
    upstream.handler = () => jsonReply(completion(LIGHTNING, usage(5, 1)));
    let clock = Math.floor(Date.now() / 60_000) * 60_000 + 59_000; // 1 s before the minute turns
    const slept: number[] = [];
    const relay = makeRelay({
      ledger,
      fetch: upstream.fetch,
      config: tightConfig(1),
      limiterWaitMs: 120_000,
      now: () => clock,
      sleep: async (ms) => {
        slept.push(ms);
        clock += ms;
      },
    });
    expect((await relay.handle(chatRequest(code, { model: LIGHTNING, messages, max_tokens: 4 }))).status).toBe(200);
    const waited = await relay.handle(chatRequest(code, { model: LIGHTNING, messages, max_tokens: 4 }));
    expect(waited.status).toBe(200);
    expect(slept.length).toBeGreaterThan(0);
    expect(Number(waited.headers.get("x-relay-wait-ms"))).toBeGreaterThan(0);
  });
});

describe("ledger outages", () => {
  it("answers 503 with Retry-After and never calls upstream when the ledger is unreachable", async () => {
    const { ledger, upstream, code } = await setup();
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    const original = ledger.reserve.bind(ledger);
    ledger.reserve = async () => {
      throw new Error("ECONNRESET talking to the ledger");
    };
    const res = await relay.handle(chatRequest(code, { model: SUPER, messages, max_tokens: 8 }));
    expect(res.status).toBe(503);
    expect(res.headers.get("retry-after")).toBe("5");
    expect(await res.json()).toMatchObject({ error: { type: "relay_ledger_unavailable" } });
    expect(upstream.calls).toHaveLength(0);
    ledger.reserve = original;
    ledger.getCode = async () => {
      throw new Error("down");
    };
    expect((await relay.handle(getRequest("/v1/models", code))).status).toBe(503);
  });
});

describe("overdue reservations", () => {
  it("books a reservation left behind by a crashed call at its full amount on the next call", async () => {
    const { ledger, upstream, code, hash } = await setup();
    const t = Date.now();
    await ledger.reserve({
      id: "r_crashed",
      hash,
      model: SUPER,
      day: new Date(t).toISOString().slice(0, 10),
      window: Math.floor(t / 60_000),
      elapsedMs: t % 60_000,
      amount: 12_345,
      rateTokens: 10,
      rpm: 1000,
      tpm: 1_000_000,
      nowMs: t - 1_000_000,
      deadlineMs: t - 1000,
      defaultSharePct: 100,
      defaultMaxInFlight: 8,
    });
    const background: Promise<unknown>[] = [];
    const relay = makeRelay({ ledger, fetch: upstream.fetch, waitUntil: (p) => background.push(p) });
    upstream.handler = () => jsonReply(completion(SUPER, usage(10, 2)));
    expect((await relay.handle(chatRequest(code, { model: SUPER, messages, max_tokens: 8 }))).status).toBe(200);
    await Promise.all(background);
    const report = await ledger.report({ days: [], events: 5, nowMs: Date.now() });
    expect(report.events.find((e) => e.id === "r_crashed")).toMatchObject({ kind: "full", why: "expired", actual: 12_345 });
    expect(await ledger.getCode(hash)).toMatchObject({ reserved: 0, spent: 12_345 + 10 * 300 + 2 * 900 });
  });

  const stores: [string, () => Ledger][] = [
    ["FileLedger", () => fileLedger("overdue")],
    ["UpstashLedger (Lua)", () => new UpstashLedger(new IoredisMockAdapter(new RedisMock()), { prefix: "overdue" })],
  ];
  it.each(stores)("lets a code's own refused call sweep overdue holds that fill its calls at once (%s)", async (_name, makeLedger) => {
    const ledger = makeLedger();
    await ledger.setGlobalCap(usdToNano("10"));
    const { code, hash } = await mint(ledger, { total: "5" });
    const t = Date.now();
    // Eight calls of an instance that died mid-call, long past their deadlines.
    for (let i = 0; i < config.codeMaxInFlight; i += 1) {
      const held = await ledger.reserve({
        id: `r_dead_${i}`,
        hash,
        model: SUPER,
        day: new Date(t).toISOString().slice(0, 10),
        window: Math.floor(t / 60_000),
        elapsedMs: t % 60_000,
        amount: 10_000,
        rateTokens: 10,
        rpm: 1000,
        tpm: 1_000_000,
        nowMs: t - 1_000_000,
        deadlineMs: t - 1000,
        defaultSharePct: 100,
        defaultMaxInFlight: config.codeMaxInFlight,
      });
      expect(held.ok).toBe(true);
    }
    const background: Promise<unknown>[] = [];
    const upstream = new FakeUpstream(() => jsonReply(completion(SUPER, usage(10, 2))));
    const relay = makeRelay({ ledger, fetch: upstream.fetch, waitUntil: (p) => void background.push(p) });
    const refused = await relay.handle(chatRequest(code, { model: SUPER, messages, max_tokens: 8 }));
    expect(refused.status).toBe(429);
    expect(await refused.json()).toMatchObject({ error: { code: "in_flight" } });
    await Promise.all(background);
    expect(await ledger.getCode(hash)).toMatchObject({ inFlight: 0, reserved: 0, spent: config.codeMaxInFlight * 10_000 });
    expect((await relay.handle(chatRequest(code, { model: SUPER, messages, max_tokens: 8 }))).status).toBe(200);
    expect(upstream.calls).toHaveLength(1);
  });
});
