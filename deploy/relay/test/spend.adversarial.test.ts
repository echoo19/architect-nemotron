/**
 * W2 security review, spend side: a judge-code holder trying to spend past the caps.
 *
 * Regression tests for the attacks the relay holds against, including the review's findings
 * (R-numbers in the review notes), each closed and pinned here.
 */

import path from "node:path";
import { Redis } from "@upstash/redis";
import RedisMock from "ioredis-mock";
import { afterAll, describe, expect, it } from "vitest";
import { parseConfig } from "../src/config.js";
import { FileLedger } from "../src/ledger/file.js";
import type { Ledger, RelayEvent } from "../src/ledger/types.js";
import { type UpstashClient, UpstashLedger, upstashStore } from "../src/ledger/upstash.js";
import { usdToNano } from "../src/money.js";
import { prepareRequest, reservationNano } from "../src/request.js";
import { startFakeUpstash } from "./fake-upstash.js";
import {
  CONFIG_TEXT,
  FakeUpstream,
  LIGHTNING,
  NANO,
  SUPER,
  ULTRA,
  type UpstreamCall,
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
  tempDir,
  usage,
  waitFor,
} from "./helpers.js";
import { IoredisMockAdapter } from "./ioredis-adapter.js";

afterAll(() => cleanupDirs());

async function setup(options: { globalCap?: string; total?: string; daily?: string } = {}) {
  const ledger = fileLedger("adv-spend");
  await ledger.setGlobalCap(usdToNano(options.globalCap ?? "10"));
  const minted = await mint(ledger, { total: options.total ?? "5", ...(options.daily ? { daily: options.daily } : {}) });
  const upstream = new FakeUpstream(() => jsonReply(completion(SUPER, usage(10, 2))));
  return { ledger, upstream, ...minted };
}

async function events(ledger: Ledger, count = 50): Promise<RelayEvent[]> {
  return (await ledger.report({ days: [], events: count, nowMs: Date.now() })).events;
}

async function lastEvent(ledger: Ledger): Promise<RelayEvent> {
  const [event] = await events(ledger, 1);
  if (!event) throw new Error("no settlement event yet");
  return event;
}

async function eventually(check: () => Promise<boolean>, timeoutMs = 10_000): Promise<void> {
  const start = Date.now();
  while (!(await check())) {
    if (Date.now() - start > timeoutMs) throw new Error("eventually: timed out");
    await new Promise((r) => setTimeout(r, 10));
  }
}

/** The real config with the per-code limiter shares lifted, for tests about the money caps only. */
const capsOnly = (() => {
  const raw = JSON.parse(CONFIG_TEXT);
  raw.code_share_pct = 100;
  raw.code_max_in_flight = 1000;
  return parseConfig(raw);
})();

function worstCase(body: Record<string, unknown>): number {
  const prepared = prepareRequest(body, config);
  if (!prepared.ok) throw new Error(`refused: ${prepared.error.message}`);
  return reservationNano(prepared.value.model, prepared.value.inputTokensUpper, prepared.value.maxTokens);
}

/** Answers a call as Token Factory would: SSE when the relay asked for a stream. */
function answer(call: UpstreamCall, used: unknown): Response {
  const model = String(call.body.model);
  return call.body.stream === true ? sseReply(chunks(model, used)) : jsonReply(completion(model, used));
}

describe("fields that multiply generation never reach Token Factory", () => {
  it("drops every n/best_of/beam/logprob/vLLM extra and sends n = 1 with the relay's max_tokens", async () => {
    const { ledger, upstream, code } = await setup();
    const smuggled: Record<string, unknown> = {
      n: 8,
      N: 8,
      "n ": 8,
      best_of: 8,
      use_beam_search: true,
      num_beams: 4,
      length_penalty: 2,
      num_return_sequences: 4,
      logprobs: true,
      top_logprobs: 20,
      prompt_logprobs: 5,
      echo: true,
      min_tokens: 4096,
      ignore_eos: true,
      max_completion_tokens: 1_000_000,
      max_new_tokens: 1_000_000,
      max_length: 1_000_000,
      extra_body: { n: 8, best_of: 8, max_tokens: 1_000_000 },
      vllm_xargs: { n: 8 },
      continue_final_message: true,
      add_generation_prompt: false,
      chat_template: "{{ messages }}",
      documents: [{ text: "x" }],
      mm_processor_kwargs: { a: 1 },
      priority: -1000,
      request_id: "r",
      cache_salt: "s",
      truncate_prompt_tokens: 1,
      guided_json: {},
      guided_regex: ".*",
      guided_choice: ["a"],
      structured_outputs: { regex: ".*" },
      kv_transfer_params: {},
      return_tokens_as_token_ids: true,
      skip_special_tokens: false,
      include_stop_str_in_output: true,
      repetition_penalty: 0.1,
      min_p: 0,
      top_k: -1,
      bad_words: ["x"],
      allowed_token_ids: [1],
      stop_token_ids: [1],
      logit_bias: { "1": 100 },
      presence_penalty: -2,
      frequency_penalty: -2,
      reasoning_effort: "max",
      thinking: { type: "enabled", budget_tokens: 1_000_000 },
      parallel_tool_calls: true,
      seed: 1,
      user: "u",
      metadata: { a: "b" },
      store: true,
      prediction: { type: "content", content: "x" },
      modalities: ["text", "audio"],
      audio: { voice: "x" },
      web_search_options: {},
    };
    const res = await makeRelay({ ledger, fetch: upstream.fetch }).handle(
      chatRequest(code, { model: ULTRA, messages, max_tokens: 16, ...smuggled }),
    );
    expect(res.status).toBe(200);
    await res.text();
    const sent = upstream.calls[0]!.body;
    expect(Object.keys(sent).sort()).toEqual(["max_tokens", "messages", "model", "n"]);
    expect(sent).toMatchObject({ model: ULTRA, n: 1, max_tokens: 16 });
    expect(res.headers.get("x-relay-max-tokens")).toBe("16");
    // The same with stream on: the only additions are stream and the forced usage option.
    upstream.handler = (call) => answer(call, usage(5, 1));
    const streamed = await makeRelay({ ledger, fetch: upstream.fetch }).handle(
      chatRequest(code, { model: ULTRA, messages, max_tokens: 16, stream: true, stream_options: { include_usage: false, continuous_usage_stats: true }, ...smuggled }),
    );
    await streamed.text();
    expect(upstream.calls[1]!.body).toEqual({ model: ULTRA, messages, max_tokens: 16, n: 1, stream: true, stream_options: { include_usage: true } });
  });

  it("keeps chat_template_kwargs to a boolean enable_thinking, whatever else is packed in", async () => {
    const { ledger, upstream, code } = await setup();
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    // Ultra's template also reads medium_effort and truncate_history_thinking; both are dropped.
    const raw =
      `{"model":"${ULTRA}","messages":[{"role":"user","content":"hi"}],"max_tokens":8,"chat_template_kwargs":{` +
      `"enable_thinking":true,"enable_thinking ":false,"ENABLE_THINKING":false,"enableThinking":false,` +
      `"medium_effort":true,"truncate_history_thinking":false,"force_nonempty_content":true,` +
      `"reasoning_budget":1000000000,"thinking_budget":1000000000,"tools":[{"type":"function","function":{"name":"x"}}],` +
      `"documents":[{"text":"x"}],"add_generation_prompt":false,"continue_final_message":true,` +
      `"chat_template":"{{ raise_exception('x') }}","messages":[],"__proto__":{"enable_thinking":false},` +
      `"constructor":{"prototype":{"enable_thinking":false}}}}`;
    const res = await relay.handle(chatRequest(code, raw));
    expect(res.status).toBe(200);
    await res.text();
    expect(upstream.calls[0]!.body.chat_template_kwargs).toEqual({ enable_thinking: true });
    // Duplicate keys: the last one wins, and it must still be a boolean.
    const dupTail = await relay.handle(
      chatRequest(code, `{"model":"${ULTRA}","messages":[{"role":"user","content":"hi"}],"max_tokens":8,"chat_template_kwargs":{"enable_thinking":true,"enable_thinking":"yes"}}`),
    );
    expect(dupTail.status).toBe(400);
    await dupTail.text();
    const dupHead = await relay.handle(
      chatRequest(code, `{"model":"${ULTRA}","messages":[{"role":"user","content":"hi"}],"max_tokens":8,"chat_template_kwargs":{"enable_thinking":"yes","enable_thinking":false}}`),
    );
    expect(dupHead.status).toBe(200);
    await dupHead.text();
    expect(upstream.calls[1]!.body.chat_template_kwargs).toEqual({ enable_thinking: false });
    for (const bad of [[], "x", 1, true, [{ enable_thinking: true }]]) {
      const res = await relay.handle(chatRequest(code, { model: ULTRA, messages, max_tokens: 8, chat_template_kwargs: bad }));
      expect(res.status, JSON.stringify(bad)).toBe(400);
      await res.text();
    }
    for (const bad of [null, 1, 0, "true", "false", {}, []]) {
      const res = await relay.handle(chatRequest(code, { model: ULTRA, messages, max_tokens: 8, chat_template_kwargs: { enable_thinking: bad } }));
      expect(res.status, JSON.stringify(bad)).toBe(400);
      await res.text();
    }
    expect(upstream.calls).toHaveLength(2);
  });
});

describe("max_tokens can only go down", () => {
  it("clamps or refuses every spelling of a huge max_tokens, and prices exactly what it sends", async () => {
    const { ledger, upstream, code } = await setup();
    upstream.handler = (call) => answer(call, usage(10, 2));
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    const clamp = config.models.get(ULTRA)!.maxTokensClamp;
    const head = `{"model":"${ULTRA}","messages":[{"role":"user","content":"hi"}]`;
    // [extra JSON text, the max_tokens that must be sent, or null for a 400]
    const cases: [string, number | null][] = [
      [`,"max_tokens":1e308`, null],
      [`,"max_tokens":1e400`, null],
      [`,"max_tokens":9007199254740992`, null],
      [`,"max_tokens":9007199254740991`, clamp],
      [`,"max_tokens":1000000`, clamp],
      [`,"max_tokens":${clamp + 1}`, clamp],
      [`,"max_tokens":-1`, null],
      [`,"max_tokens":-0`, null],
      [`,"max_tokens":0`, null],
      [`,"max_tokens":0.5`, null],
      [`,"max_tokens":1.0`, 1],
      [`,"max_tokens":1e3`, 1000],
      [`,"max_tokens":"99999"`, null],
      [`,"max_tokens":[8192]`, null],
      [`,"max_tokens":{"value":1}`, null],
      [`,"max_tokens":true`, null],
      [`,"max_completion_tokens":1000000000`, clamp],
      [`,"max_tokens":null,"max_completion_tokens":1000000000`, clamp],
      [`,"max_completion_tokens":"1"`, null],
      [`,"max_tokens":4,"max_completion_tokens":1000000000`, 4],
      [`,"max_completion_tokens":4,"max_tokens":1000000000`, clamp],
      [`,"max_tokens":8,"max_tokens":1000000000`, clamp],
      [`,"max_tokens":1000000000,"max_tokens":8`, 8],
      [`,"max_tokens":8,"stream":true,"stream":false`, 8],
      [`,"__proto__":{"max_tokens":1000000000},"max_tokens":8`, 8],
      [`,"constructor":{"prototype":{"max_tokens":1000000000}}`, clamp],
    ];
    let sentCount = 0;
    for (const [extra, expected] of cases) {
      const raw = `${head}${extra}}`;
      const res = await relay.handle(chatRequest(code, raw));
      await res.text();
      if (expected === null) {
        expect(res.status, extra).toBe(400);
        expect(upstream.calls, extra).toHaveLength(sentCount);
        continue;
      }
      expect(res.status, extra).toBe(200);
      sentCount += 1;
      const sent = upstream.calls[sentCount - 1]!.body;
      expect(sent.max_tokens, extra).toBe(expected);
      expect(sent.max_completion_tokens, extra).toBeUndefined();
      expect(Object.keys(sent), extra).not.toContain("__proto__");
      expect(res.headers.get("x-relay-max-tokens"), extra).toBe(String(expected));
      // What was held is what this exact payload can cost.
      expect((await lastEvent(ledger)).reserved, extra).toBe(worstCase(JSON.parse(raw) as Record<string, unknown>));
    }
  });
});

describe("reservation races across hosts sharing one ledger", () => {
  let n = 0;
  const shared: [string, () => Promise<{ a: Ledger; b: Ledger; close: () => Promise<void> }>][] = [
    [
      "two FileLedgers on one file",
      async () => {
        const file = path.join(tempDir("adv-race"), "ledger.json");
        return { a: new FileLedger(file), b: new FileLedger(file), close: async () => undefined };
      },
    ],
    [
      "two UpstashLedgers on one Redis (Lua)",
      async () => {
        const redis = new RedisMock();
        const prefix = `advrace${(n += 1)}`;
        return {
          a: new UpstashLedger(new IoredisMockAdapter(redis), { prefix }),
          b: new UpstashLedger(new IoredisMockAdapter(redis), { prefix }),
          close: async () => undefined,
        };
      },
    ],
    [
      "two @upstash/redis clients over REST",
      async () => {
        const rest = await startFakeUpstash();
        const make = () =>
          new UpstashLedger(
            upstashStore(new Redis({ url: rest.url, token: rest.token, automaticDeserialization: false, enableTelemetry: false }) as unknown as UpstashClient),
            { prefix: `advrest${(n += 1)}x` },
          );
        const a = make();
        const b = new UpstashLedger(
          upstashStore(new Redis({ url: rest.url, token: rest.token, automaticDeserialization: false, enableTelemetry: false }) as unknown as UpstashClient),
          { prefix: a.prefix },
        );
        return { a, b, close: () => rest.close() };
      },
    ],
  ];

  it.each(shared)("holds the daily, total and global caps when mixed calls race on %s", async (_name, make) => {
    const { a, b, close } = await make();
    try {
      const bodies: Record<string, unknown>[] = [
        { model: ULTRA, messages, max_tokens: 3000 },
        { model: LIGHTNING, messages, max_tokens: 5000, stream: true },
        { model: SUPER, messages, max_tokens: 4000 },
        { model: NANO, messages: [{ role: "user", content: "x".repeat(20_000) }], max_tokens: 100, stream: true },
      ];
      await a.setGlobalCap(usdToNano("0.05"));
      const judge = await mint(a, { total: "1", daily: "0.03", label: "judge" });
      const other = await mint(b, { total: "1", label: "other" });
      const daily = usdToNano("0.03");
      const global = usdToNano("0.05");
      let release!: () => void;
      const gate = new Promise<void>((resolve) => (release = resolve));
      const violations: string[] = [];
      const check = async () => {
        const [g, day] = await Promise.all([a.getGlobal(), a.getDay(judge.hash, new Date().toISOString().slice(0, 10))]);
        if (g.spent + g.reserved > global) violations.push(`global ${g.spent + g.reserved}`);
        if (day.spent + day.reserved > daily) violations.push(`daily ${day.spent + day.reserved}`);
      };
      const upstream = new FakeUpstream(async (call) => {
        await check();
        await gate;
        return answer(call, usage(100, 10));
      });
      const relays = [makeRelay({ ledger: a, fetch: upstream.fetch, config: capsOnly }), makeRelay({ ledger: b, fetch: upstream.fetch, config: capsOnly })];
      let refused = 0;
      const calls = Array.from({ length: 48 }, (_, i) =>
        relays[i % 2]!.handle(chatRequest(i % 3 === 0 ? other.code : judge.code, bodies[i % bodies.length]!)).then(async (res) => {
          if (res.status === 402) refused += 1;
          await res.text();
          return res.status;
        }),
      );
      await waitFor(() => upstream.calls.length + refused === 48, 30_000);
      await check();
      release();
      const statuses = await Promise.all(calls);
      expect(statuses.every((s) => s === 200 || s === 402)).toBe(true);
      expect(statuses.filter((s) => s === 402).length).toBeGreaterThan(0);
      expect(violations).toEqual([]);
      const g = await b.getGlobal();
      expect(g.reserved).toBe(0);
      expect(g.spent).toBeLessThanOrEqual(global);
      const day = await a.getDay(judge.hash, new Date().toISOString().slice(0, 10));
      expect(day.spent).toBeLessThanOrEqual(daily);
      expect(day.reserved).toBe(0);
    } finally {
      await close();
    }
  });
});

describe("streams without usage", () => {
  const cases: [string, (object | string)[]][] = [
    ["no usage chunk", chunks(SUPER, null)],
    ["negative tokens", chunks(SUPER, { prompt_tokens: -5, completion_tokens: 3 })],
    ["tokens as strings", chunks(SUPER, { prompt_tokens: "10", completion_tokens: "3" })],
    ["fractional tokens", chunks(SUPER, { prompt_tokens: 10.5, completion_tokens: 3 })],
    ["prompt tokens only", chunks(SUPER, { prompt_tokens: 10 })],
    ["tokens past 2^53", chunks(SUPER, { prompt_tokens: 2 ** 60, completion_tokens: 3 })],
    ["usage: true", chunks(SUPER, true)],
    ["an error event, then the end", [chunks(SUPER, null)[0]!, { error: { message: "overloaded", type: "server_error" } }, "[DONE]"]],
    ["[DONE] alone", ["[DONE]"]],
    ["nothing at all", []],
  ];

  it.each(cases)("books the full hold when the stream ends with %s", async (_what, events) => {
    const { ledger, upstream, code, hash } = await setup();
    upstream.handler = () => sseReply(events);
    const res = await makeRelay({ ledger, fetch: upstream.fetch }).handle(
      chatRequest(code, { model: SUPER, messages, stream: true, stream_options: { include_usage: true } }),
    );
    expect(res.status).toBe(200);
    await res.text();
    const event = await lastEvent(ledger);
    expect(event.kind).toBe("full");
    expect(event.actual).toBe(event.reserved);
    expect(await ledger.getCode(hash)).toMatchObject({ spent: event.reserved, reserved: 0 });
  });

  it.each([0, 1, 2])("books the full hold when the client hangs up after %i events, before usage", async (after) => {
    const { ledger, upstream, code, hash } = await setup();
    upstream.handler = (call) => sseReply(chunks(SUPER, usage(1, 1)), { hangAfter: 3, signal: call.signal });
    const res = await makeRelay({ ledger, fetch: upstream.fetch }).handle(chatRequest(code, { model: SUPER, messages, stream: true }));
    const reader = res.body!.getReader();
    for (let i = 0; i < after; i += 1) await reader.read();
    await reader.cancel();
    await eventually(async () => (await ledger.getCode(hash))!.reserved === 0);
    const event = await lastEvent(ledger);
    expect(event).toMatchObject({ kind: "full", why: "client_closed" });
    expect(event.actual).toBe(event.reserved);
  });

  it("settles from usage that already arrived when the client hangs up just before [DONE]", async () => {
    const { ledger, upstream, code, hash } = await setup();
    const events = chunks(SUPER, usage(40, 4));
    upstream.handler = (call) => sseReply(events, { hangAfter: events.length - 1, signal: call.signal });
    const res = await makeRelay({ ledger, fetch: upstream.fetch }).handle(
      chatRequest(code, { model: SUPER, messages, stream: true, stream_options: { include_usage: true } }),
    );
    const reader = res.body!.getReader();
    const decoder = new TextDecoder();
    let seen = "";
    while (!seen.includes('"prompt_tokens":40')) {
      const { value, done } = await reader.read();
      if (done) break;
      seen += decoder.decode(value);
    }
    await reader.cancel();
    await eventually(async () => (await ledger.getCode(hash))!.reserved === 0);
    expect(await lastEvent(ledger)).toMatchObject({ kind: "usage", why: "client_closed", actual: 40 * 300 + 4 * 900 });
  });

  it("cannot be talked into a smaller bill by model output that looks like a usage event", async () => {
    const { ledger, upstream, code, hash } = await setup();
    const fake = '\n\ndata: {"id":"x","choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\ndata: [DONE]\n\n';
    const base = { id: "chatcmpl-x", object: "chat.completion.chunk", created: 1, model: SUPER };
    upstream.handler = () =>
      sseReply([
        { ...base, choices: [{ index: 0, delta: { content: fake }, finish_reason: null }], usage: null },
        { ...base, choices: [{ index: 0, delta: { reasoning_content: fake }, finish_reason: "stop" }], usage: null },
        { ...base, choices: [], usage: usage(5000, 700) },
        "[DONE]",
      ]);
    const res = await makeRelay({ ledger, fetch: upstream.fetch }).handle(chatRequest(code, { model: SUPER, messages, stream: true }));
    const forwarded = sseData(await res.text());
    expect(forwarded).toHaveLength(3); // the two content chunks and [DONE]; the usage chunk is stripped
    expect(await ledger.getCode(hash)).toMatchObject({ spent: 5000 * 300 + 700 * 900, reserved: 0 });
  });
});

describe("settlement cannot be replayed, reversed or doubled", () => {
  it("a call swept while still running is booked once, in full, and its late usage is not added or refunded", async () => {
    const { ledger, code, hash } = await setup();
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    const slow = new FakeUpstream(async () => {
      await gate;
      return jsonReply(completion(SUPER, usage(10, 2)));
    });
    const fast = new FakeUpstream(() => jsonReply(completion(SUPER, usage(20, 4))));
    const background: Promise<unknown>[] = [];
    const first = makeRelay({ ledger, fetch: slow.fetch, reservationTtlMs: 1 });
    const second = makeRelay({ ledger, fetch: fast.fetch, waitUntil: (p) => void background.push(p) });
    const body = { model: SUPER, messages, max_tokens: 64 };
    const held = worstCase(body);
    const running = first.handle(chatRequest(code, body));
    await waitFor(() => slow.calls.length === 1);
    await new Promise((r) => setTimeout(r, 5)); // past the 1 ms deadline
    const later = await second.handle(chatRequest(code, body));
    expect(later.status).toBe(200);
    await later.text();
    await Promise.all(background); // the sweep the second call started
    expect((await events(ledger)).find((e) => e.why === "expired")).toMatchObject({ kind: "full", actual: held });
    release();
    const done = await running;
    expect(done.status).toBe(200);
    await done.text();
    expect(await ledger.getCode(hash)).toMatchObject({ reserved: 0, spent: held + 20 * 300 + 4 * 900, requests: 2 });
  });

  it("books usage above the hold in full (the ledger never under-records), then freezes the code", async () => {
    const { ledger, upstream, code, hash } = await setup({ total: "0.05" });
    const body = { model: ULTRA, messages, max_tokens: 1 };
    const held = worstCase(body);
    upstream.handler = () => jsonReply(completion(ULTRA, usage(20_000, 1)));
    const logs: Record<string, unknown>[] = [];
    const relay = makeRelay({ ledger, fetch: upstream.fetch, log: (entry) => logs.push(entry) });
    const res = await relay.handle(chatRequest(code, body));
    expect(res.status).toBe(200);
    await res.text();
    const actual = 20_000 * 1000 + 1 * 3000;
    expect(actual).toBeGreaterThan(held);
    expect(await ledger.getCode(hash)).toMatchObject({ spent: actual, reserved: 0, frozen: true });
    expect(await lastEvent(ledger)).toMatchObject({ kind: "usage", actual, overshoot: actual - held });
    expect(logs.find((entry) => entry.level === "warn")).toMatchObject({ msg: expect.stringMatching(/froze the code/), cost_usd: "0.020003" });
    // The code still has $0.03 of its cap, but stays shut until the operator looks.
    const next = await relay.handle(chatRequest(code, body));
    expect(next.status).toBe(403);
    expect(((await next.json()) as { error: { type: string } }).error.type).toBe("relay_code_frozen");
    expect(upstream.calls).toHaveLength(1);
    await ledger.updateCode(hash, { frozen: false });
    upstream.handler = () => jsonReply(completion(ULTRA, usage(10, 1)));
    expect((await relay.handle(chatRequest(code, body))).status).toBe(200);
  });

  it("refuses a call when the code is revoked, capped, frozen or switched off just before its reservation", async () => {
    const { ledger, upstream, code, hash } = await setup();
    const reserve = ledger.reserve.bind(ledger);
    const changes: [string, () => Promise<unknown>, number][] = [
      ["capped", () => ledger.updateCode(hash, { capTotal: 1 }), 402],
      ["frozen", () => ledger.updateCode(hash, { frozen: true }), 403],
      ["switched off", () => ledger.setDisabled(true), 503],
      ["revoked", () => ledger.updateCode(hash, { revoked: true }), 401],
    ];
    for (const [what, change, status] of changes) {
      await ledger.updateCode(hash, { revoked: false, frozen: false, capTotal: usdToNano("5") });
      await ledger.setDisabled(false);
      ledger.reserve = async (r) => {
        await change();
        return reserve(r);
      };
      // A fresh relay each time: a refused code is remembered by the instance that refused it.
      const res = await makeRelay({ ledger, fetch: upstream.fetch }).handle(chatRequest(code, { model: SUPER, messages, max_tokens: 8 }));
      expect(res.status, what).toBe(status);
      await res.text();
    }
    expect(upstream.calls).toHaveLength(0);
  });
});

describe("review findings, closed", () => {
  // R1. The hold priced the prompt at one token per request byte. Ultra's published chat
  // template renders every extra key of a tool's JSON schema twice (<key>value</key>), and
  // private-use characters tokenize at one token per byte, so the prompt could be about twice
  // the bound. Measured offline on 2026-10-03 with chat_template.jinja and tokenizer.json from
  // HF nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B-BF16 (no Token Factory call): this exact
  // request renders to 792,006 prompt tokens. The relay now prices the tools list at two
  // tokens a byte plus four a key, and holds 810,699 for it.
  it("R1: concurrent calls whose tool schemas render at twice their bytes cannot carry a code past its cap", async () => {
    const { ledger, upstream, code, hash } = await setup({ total: "1.5" });
    // Lift Ultra's token budget so only the money caps decide (the limiter is not the subject).
    const raw = JSON.parse(CONFIG_TEXT);
    raw.models[ULTRA].tpm = 100_000_000;
    const roomy = parseConfig(raw);
    const pua = Array.from({ length: 64 }, (_, i) => String.fromCharCode(0xe000 + i)).join("");
    const extra = Object.fromEntries(Array.from({ length: 2000 }, (_, i) => [pua + String(i), 0]));
    const crafted = {
      model: ULTRA,
      messages: [{ role: "user", content: "hi" }],
      tools: [{ type: "function", function: { name: "f", parameters: { type: "object", properties: {}, ...extra } } }],
      max_tokens: 1,
    };
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    upstream.handler = async () => {
      await gate;
      return jsonReply(completion(ULTRA, usage(792_006, 1)));
    };
    const relay = makeRelay({ ledger, fetch: upstream.fetch, config: roomy });
    let refused = 0;
    const calls = Array.from({ length: 6 }, () =>
      relay.handle(chatRequest(code, crafted)).then(async (res) => {
        if (res.status !== 200) refused += 1;
        await res.text();
        return res.status;
      }),
    );
    await waitFor(() => upstream.calls.length + refused === 6, 30_000);
    release();
    const statuses = await Promise.all(calls);
    expect(statuses.every((s) => s === 200 || s === 402)).toBe(true);
    const state = (await ledger.getCode(hash))!;
    expect(state.spent).toBeLessThanOrEqual(state.capTotal);
    expect(worstCase(crafted)).toBeGreaterThan(792_006 * 1000 + 3000);
    expect(state.frozen).toBe(false);
  });

  // R2. Every non-2xx answer released the hold. A 5xx can arrive after Token Factory has
  // generated (and billed) tokens: a gateway timeout on a long non-streamed Ultra call, or a
  // backend that fails mid-generation. Released calls cost the code nothing, so a request
  // shape that reliably ended in one was unlimited spend. A 5xx that comes back after more
  // than a few seconds is now booked at the full hold and marked for reconciliation.
  it("R2: an upstream 5xx that can follow generation is not booked as zero", async () => {
    const { ledger, upstream, code } = await setup();
    let clock = Math.floor(Date.now() / 86_400_000) * 86_400_000 + 12 * 3_600_000; // noon UTC: one day throughout
    const relay = makeRelay({ ledger, fetch: upstream.fetch, now: () => clock });
    for (const status of [500, 502, 504, 524]) {
      upstream.handler = () => {
        clock += 30_000; // the answer comes long after the call went out
        return jsonReply({ error: { message: `upstream ${status}` } }, { status, headers: { "x-request-id": `req-${status}` } });
      };
      const res = await relay.handle(chatRequest(code, { model: ULTRA, messages, max_tokens: 1000 }));
      expect(res.status, String(status)).toBe(status);
      await res.text();
      const event = await lastEvent(ledger);
      expect(event.kind, String(status)).not.toBe("released");
      expect(event.actual, String(status)).toBe(event.reserved);
      expect(event, String(status)).toMatchObject({ reconcile: true, request_id: `req-${status}`, why: `upstream_${status}` });
    }
    const day = (await ledger.report({ days: [new Date(clock).toISOString().slice(0, 10)], events: 0, nowMs: clock })).days[0]!;
    expect(day.models.find((m) => m.model === ULTRA)).toMatchObject({ full: 4, reconcile: 4, released: 0 });
  });

  // R3. The limiter was one budget per model for every code. A single code could fill it
  // with max_tokens 1 calls (about $0.0006 a minute on Lightning) and every other code, the
  // owner's evidence runs included, got 429 for as long as it kept going. Each code now has
  // a share of each model's budget (25% by default).
  it("R3: one code cannot take a model's whole request budget from the others", async () => {
    const raw = JSON.parse(CONFIG_TEXT);
    raw.limiter_fraction = 0.5;
    raw.models[LIGHTNING].rpm = 20; // the relay budgets 10 a minute
    const tight = parseConfig(raw);
    const { ledger, upstream, code } = await setup();
    const judge = await mint(ledger, { total: "1", label: "judge-2" });
    upstream.handler = () => jsonReply(completion(LIGHTNING, usage(5, 1)));
    const relay = makeRelay({ ledger, fetch: upstream.fetch, config: tight });
    const greedy: string[] = [];
    for (let i = 0; i < 10; i += 1) {
      const res = await relay.handle(chatRequest(code, { model: LIGHTNING, messages, max_tokens: 1 }));
      greedy.push(res.status === 429 ? `429 ${((await res.json()) as { error: { code: string } }).error.code}` : String(res.status));
    }
    expect(greedy).toEqual(["200", "200", ...Array.from({ length: 8 }, () => "429 code_rpm")]);
    const res = await relay.handle(chatRequest(judge.code, { model: LIGHTNING, messages, max_tokens: 1 }));
    expect(res.status).toBe(200);
  });

  // R5. If the client was already gone when the hold was placed, fetch() rejected before any
  // byte was sent, yet the call was booked at its full worst case.
  it("R5: a call whose client left before anything was sent is released, not booked in full", async () => {
    const { ledger, upstream, code, hash } = await setup();
    const client = new AbortController();
    const reserve = ledger.reserve.bind(ledger);
    ledger.reserve = async (r) => {
      const outcome = await reserve(r);
      client.abort();
      return outcome;
    };
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    const res = await relay.handle(chatRequest(code, { model: ULTRA, messages }, { signal: client.signal }));
    await res.text();
    await eventually(async () => (await ledger.getCode(hash))!.reserved === 0);
    expect(await ledger.getCode(hash)).toMatchObject({ spent: 0, reserved: 0 });
  });

  // R6. A reserve that succeeded in Redis but whose reply was lost (a dropped REST response
  // after the client's own retries) answered 503 "nothing was charged", yet the hold stayed
  // and a later sweep booked it in full. The relay now releases that hold in the background.
  it("R6: a hold whose reserve reply was lost is released, as the 503 promises", async () => {
    const { ledger, upstream, code, hash } = await setup();
    const reserve = ledger.reserve.bind(ledger);
    ledger.reserve = async (r) => {
      await reserve(r);
      throw new Error("fetch failed: ECONNRESET");
    };
    const background: Promise<unknown>[] = [];
    const relay = makeRelay({ ledger, fetch: upstream.fetch, waitUntil: (p) => void background.push(p) });
    const res = await relay.handle(chatRequest(code, { model: ULTRA, messages }));
    expect(res.status).toBe(503);
    expect(((await res.json()) as { error: { message: string } }).error.message).toMatch(/nothing was charged/);
    await Promise.all(background);
    expect(await ledger.getCode(hash)).toMatchObject({ spent: 0, reserved: 0 });
  });
});

describe("Token Factory errors", () => {
  it("releases the hold for refusals that cannot follow generation, and books a 408 in full", async () => {
    const { ledger, upstream, code, hash } = await setup();
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    // Every 4xx but 408, and a 5xx that comes back within seconds, arrive before any token.
    for (const status of [400, 404, 409, 422, 429, 500, 503, 529]) {
      upstream.handler = () => jsonReply({ error: { message: `upstream ${status}` } }, { status });
      const res = await relay.handle(chatRequest(code, { model: SUPER, messages, max_tokens: 8 }));
      expect(res.status, String(status)).toBe(status);
      await res.text();
      expect(await lastEvent(ledger), String(status)).toMatchObject({ kind: "released", actual: 0 });
    }
    expect(await ledger.getCode(hash)).toMatchObject({ spent: 0, reserved: 0 });
    upstream.handler = () => jsonReply({ error: { message: "timeout" } }, { status: 408 });
    await (await relay.handle(chatRequest(code, { model: SUPER, messages, max_tokens: 8 }))).text();
    const event = await lastEvent(ledger);
    expect(event).toMatchObject({ kind: "full", reconcile: true, why: "upstream_408" });
    expect(event.actual).toBe(event.reserved);
  });

  it("removes the relay's key from a Token Factory error body it passes on", async () => {
    const key = "tfk_SECRET_scrub_0123456789abcdef";
    const { ledger, upstream, code } = await setup();
    upstream.handler = () => jsonReply({ error: { message: `bad request for Bearer ${key}` } }, { status: 400 });
    const res = await makeRelay({ ledger, fetch: upstream.fetch, upstreamKey: key }).handle(chatRequest(code, { model: SUPER, messages, max_tokens: 8 }));
    expect(res.status).toBe(400);
    const text = await res.text();
    expect(text).not.toContain(key);
    expect(text).toContain("[key removed]");
  });

  it("never follows a redirect from Token Factory", async () => {
    const { ledger, code } = await setup();
    const seen: RequestInit[] = [];
    const relay = makeRelay({
      ledger,
      fetch: async (_url, init) => {
        seen.push(init);
        return jsonReply(completion(SUPER, usage(1, 1)));
      },
    });
    await (await relay.handle(chatRequest(code, { model: SUPER, messages, max_tokens: 8 }))).text();
    expect(seen).toHaveLength(1);
    expect(seen[0]!.redirect).toBe("error");
  });

  it("books usage figures no real call produces at the full hold, without freezing the code", async () => {
    const { ledger, upstream, code, hash } = await setup();
    upstream.handler = () => jsonReply(completion(SUPER, usage(9_000_000_000_000_000, 1)));
    const res = await makeRelay({ ledger, fetch: upstream.fetch }).handle(chatRequest(code, { model: SUPER, messages, max_tokens: 8 }));
    expect(res.status).toBe(200);
    await res.text();
    const event = await lastEvent(ledger);
    expect(event).toMatchObject({ kind: "full", why: "usage_out_of_range" });
    expect(event.actual).toBe(event.reserved);
    expect(await ledger.getCode(hash)).toMatchObject({ frozen: false, reserved: 0, spent: event.reserved });
  });

  // Reasoning far above completion is read as completion plus reasoning: $8,100 at Super's price.
  const bogus = { prompt_tokens: 10, completion_tokens: 5, completion_tokens_details: { reasoning_tokens: 9e9 } };
  const reportedCost = 10 * 300 + (9e9 + 5) * 900;

  it("books no more than Token Factory can bill for one call when a usage report claims more", async () => {
    const { ledger, upstream, code, hash } = await setup();
    upstream.handler = () => jsonReply(completion(SUPER, bogus));
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    const res = await relay.handle(chatRequest(code, { model: SUPER, messages, max_tokens: 16 }));
    expect(res.status).toBe(200);
    await res.text();
    // Super's whole context window at the input price plus its clamp at the output price.
    const ceiling = 262_144 * 300 + 8192 * 900;
    expect(ceiling).toBe(usdToNano("0.086016"));
    const event = await lastEvent(ledger);
    expect(event).toMatchObject({ kind: "usage", why: "usage_above_model_max", reconcile: true, actual: ceiling, reported: reportedCost });
    expect(event.overshoot).toBe(ceiling - event.reserved);
    expect(await ledger.getCode(hash)).toMatchObject({ frozen: true, reserved: 0, spent: ceiling });
    expect((await ledger.getGlobal()).spent).toBe(ceiling);
    const day = (await ledger.report({ days: [new Date(event.t).toISOString().slice(0, 10)], events: 0, nowMs: event.t })).days[0]!;
    expect(day.models[0]).toMatchObject({ requests: 1, cost: ceiling, reconcile: 1 });
    // The global cap is intact: another code's call goes through.
    upstream.handler = () => jsonReply(completion(SUPER, usage(10, 2)));
    const other = await mint(ledger, { total: "1", label: "after-bogus" });
    expect((await relay.handle(chatRequest(other.code, { model: SUPER, messages, max_tokens: 16 }))).status).toBe(200);
  });

  it("books the hold, never less, when that report meets a hold above the model's ceiling", async () => {
    const { ledger, upstream, code, hash } = await setup();
    upstream.handler = () => jsonReply(completion(SUPER, bogus));
    // A prompt longer than Super's context window holds more than any Super call can cost.
    const body = { model: SUPER, messages: [{ role: "user", content: "x".repeat(300_000) }], max_tokens: 16 };
    const res = await makeRelay({ ledger, fetch: upstream.fetch }).handle(chatRequest(code, body));
    expect(res.status).toBe(200);
    await res.text();
    const event = await lastEvent(ledger);
    expect(event.reserved).toBeGreaterThan(usdToNano("0.086016"));
    expect(event).toMatchObject({ kind: "usage", why: "usage_above_model_max", reconcile: true, actual: event.reserved, reported: reportedCost });
    expect(event.overshoot).toBeUndefined();
    expect(await ledger.getCode(hash)).toMatchObject({ frozen: false, reserved: 0, spent: event.reserved });
  });
});

describe("limits per code", () => {
  it("refuses a code's call past its calls in flight, with a short Retry-After", async () => {
    const { ledger, upstream, code } = await setup();
    const limit = config.codeMaxInFlight;
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    let held = 0;
    upstream.handler = async () => {
      if (held < limit) {
        held += 1; // the first calls hang until released
        await gate;
      }
      return jsonReply(completion(SUPER, usage(10, 2)));
    };
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    const running = Array.from({ length: limit }, () => relay.handle(chatRequest(code, { model: SUPER, messages, max_tokens: 8 })));
    await waitFor(() => upstream.calls.length === limit);
    const extra = await relay.handle(chatRequest(code, { model: SUPER, messages, max_tokens: 8 }));
    expect(extra.status).toBe(429);
    expect(extra.headers.get("retry-after")).toBe("5");
    expect(await extra.json()).toMatchObject({ error: { type: "relay_rate_limited", code: "in_flight" } });
    // Another code is not held up by this one.
    const other = await mint(ledger, { total: "1", label: "other-code" });
    const free = await relay.handle(chatRequest(other.code, { model: SUPER, messages, max_tokens: 8 }));
    release();
    expect(free.status).toBe(200);
    await free.text();
    for (const res of await Promise.all(running)) {
      expect(res.status).toBe(200);
      await res.text();
    }
    expect((await relay.handle(chatRequest(code, { model: SUPER, messages, max_tokens: 8 }))).status).toBe(200);
  });

  it("lets one code run Ultra calls larger than its token share back to back", async () => {
    const { ledger, upstream, code } = await setup();
    upstream.handler = () => jsonReply(completion(ULTRA, usage(2000, 500)));
    // At half the published limits, a code's 25% share of Ultra is 25,000 tokens a minute.
    const half = parseConfig({ ...JSON.parse(CONFIG_TEXT), limiter_fraction: 0.5 });
    const relay = makeRelay({ ledger, fetch: upstream.fetch, config: half });
    const share = Math.floor((half.models.get(ULTRA)!.limitTpm * half.codeSharePct) / 100);
    expect(32_768).toBeGreaterThan(share); // each call alone is more than the code's share of a minute
    for (let i = 0; i < 3; i += 1) {
      const res = await relay.handle(chatRequest(code, { model: ULTRA, messages, max_tokens: 32_768 }));
      expect(res.status, `call ${i + 1}`).toBe(200);
      await res.text();
    }
  });

  it("tells a code what it has left, never what the relay's shared budget has", async () => {
    const { ledger, upstream, code } = await setup({ globalCap: "0.01", total: "2", daily: "1.5" });
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    const res = await relay.handle(chatRequest(code, { model: ULTRA, messages }));
    expect(res.status).toBe(402);
    const body = (await res.json()) as { error: Record<string, unknown> };
    expect(body.error).toMatchObject({ code: "global_cap", remaining_usd: 1.5 });
    expect(body.error.cap_usd).toBeUndefined();
    expect(String(body.error.message)).not.toMatch(/0\.01\b/);
    const balance = (await (await relay.handle(getRequest("/v1/relay/balance", code))).json()) as Record<string, unknown>;
    expect(balance).toMatchObject({ remaining_usd: 1.5, limited_by: "daily_cap", calls_running: 0, frozen: false });
    expect(JSON.stringify(balance)).not.toMatch(/global|0\.01\b/);
  });
});
