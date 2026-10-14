/**
 * W2 security review, outside surface: an internet scanner and a code holder probing routes,
 * headers, access codes and model ids, and looking for the Token Factory key.
 *
 * Regression tests for the attacks the relay holds against, including the review's findings
 * (R-numbers in the review notes), each closed and pinned here.
 */

import { afterAll, describe, expect, it, vi } from "vitest";
import { parseConfig } from "../src/config.js";
import type { FileLedger } from "../src/ledger/file.js";
import { usdToNano } from "../src/money.js";
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
  fileLedger,
  getRequest,
  jsonReply,
  makeRelay,
  messages,
  mint,
  sseReply,
  usage,
} from "./helpers.js";

afterAll(() => cleanupDirs());

async function setup(label = "adv-surface") {
  const ledger = fileLedger(label);
  await ledger.setGlobalCap(usdToNano("10"));
  const minted = await mint(ledger, { total: "5" });
  const upstream = new FakeUpstream(() => jsonReply(completion(SUPER, usage(10, 2))));
  return { ledger, upstream, ...minted };
}

async function untouched(ledger: FileLedger, hash: string): Promise<void> {
  expect(await ledger.getCode(hash)).toMatchObject({ spent: 0, reserved: 0, requests: 0 });
  expect(await ledger.getGlobal()).toMatchObject({ spent: 0, reserved: 0 });
}

const body = JSON.stringify({ model: SUPER, messages, max_tokens: 8 });

describe("routes: Token Factory sees chat/completions and nothing else", () => {
  it("never reaches another upstream endpoint, however the path or ?route= is spelled", async () => {
    const { ledger, upstream, code, hash } = await setup();
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    const lookups = vi.spyOn(ledger, "getCode");
    const paths = [
      "/v1/embeddings",
      "/v1/completions",
      "/v1/responses",
      "/v1/rerank",
      "/v1/fine_tuning/jobs",
      "/v1/files",
      "/v1/batches",
      "/v1/operations",
      "/v0/dedicated_endpoints",
      "/sandboxes/v1/whoami",
      "/v1/models/../embeddings",
      "/v1/./embeddings",
      "/%2e%2e/v1/embeddings",
      "/v1/chat/completions/../../embeddings",
      "/v1/chat/completions/..%2f..%2fembeddings",
      "/v1/chat/completions%2f..%2f..%2fembeddings",
      "/v1/chat%2fcompletions",
      "/v1//chat/completions",
      "/V1/CHAT/COMPLETIONS",
      "/v1/chat/completions;/../embeddings",
      "/v1/chat/completions%00",
      "/v1/chat/completions%20",
      "/v1/chat/completions.json",
      "/api/relay?route=v1/embeddings",
      "/api/relay?route=../sandboxes/v1/whoami",
      "/api/relay?route=v1/chat/completions/../../embeddings",
      "/api/relay?route=v1%2fchat%2fcompletions%2f..%2fembeddings",
      "/api/relay?route=https://evil.test/v1/chat/completions",
      "/api/relay?path=../embeddings",
      "/api/relay?path=embeddings",
      "/api/relay/v1/embeddings",
      "/api/relay/../sandboxes/v1/whoami",
    ];
    for (const path of paths) {
      for (const method of ["POST", "GET", "PUT", "DELETE", "PATCH", "OPTIONS"]) {
        const res = await relay.handle(
          new Request(`http://relay.test${path}`, {
            method,
            headers: { authorization: `Bearer ${code}`, "content-type": "application/json" },
            ...(method === "GET" ? {} : { body }),
          }),
        );
        expect([404, 405], `${method} ${path}`).toContain(res.status);
        await res.text();
      }
    }
    expect(upstream.calls).toHaveLength(0);
    expect(lookups).not.toHaveBeenCalled(); // unknown routes are answered before any ledger read
    await untouched(ledger, hash);
  });

  it("sends every accepted spelling of the chat route to exactly <base>/chat/completions", async () => {
    const { ledger, upstream, code } = await setup();
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    const spellings = [
      "/v1/chat/completions",
      "/v1/chat/completions/",
      "/v1/chat/completions?route=v1/embeddings&path=../sandboxes",
      "/api/relay?route=v1/chat/completions",
      "/api/relay?route=/v1/chat/completions/",
      "/api/relay?route=v1/chat/completions&route=v1/embeddings",
      "/api/relay?path=chat/completions",
    ];
    for (const path of spellings) {
      const res = await relay.handle(
        new Request(`http://relay.test${path}`, {
          method: "POST",
          headers: { authorization: `Bearer ${code}`, "content-type": "application/json" },
          body,
        }),
      );
      expect(res.status, path).toBe(200);
      await res.text();
    }
    expect(upstream.calls.map((call) => call.url)).toEqual(spellings.map(() => "https://upstream.test/v1/chat/completions"));
  });
});

describe("headers: nothing smuggled in, nothing leaked out", () => {
  it("builds Token Factory's request headers from scratch", async () => {
    const { ledger, upstream, code } = await setup();
    const res = await makeRelay({ ledger, fetch: upstream.fetch }).handle(
      chatRequest(code, { model: SUPER, messages, max_tokens: 8 }, {
        headers: {
          "x-forwarded-for": "203.0.113.9",
          "x-forwarded-host": "evil.test",
          "x-real-ip": "203.0.113.9",
          cookie: "session=1",
          "x-api-key": "sk-someone-else",
          "api-key": "sk-someone-else",
          project: "aiproject-someone-else",
          "openai-organization": "org-x",
          "openai-project": "proj-x",
          "x-ratelimit-limit-requests": "999999",
          "x-relay-request": "r_forged",
          "x-relay-max-tokens": "999999",
          "x-http-method-override": "DELETE",
          "accept-encoding": "gzip, br",
          "user-agent": "scanner/1.0",
          prefer: "respond-async",
          "content-type": "text/plain",
        },
      }),
    );
    expect(res.status).toBe(200);
    const sent = upstream.calls[0]!;
    expect([...sent.headers.keys()].sort()).toEqual(["accept", "authorization", "content-type"]);
    expect(sent.headers.get("authorization")).toBe(`Bearer ${UPSTREAM_KEY}`);
    expect(sent.headers.get("content-type")).toBe("application/json");
    expect(res.headers.get("x-relay-request")).not.toBe("r_forged");
    expect(res.headers.get("x-relay-max-tokens")).toBe("8");
  });

  it("passes back only Retry-After, x-ratelimit-* and x-request-id from Token Factory, on every path", async () => {
    const { ledger, upstream, code } = await setup();
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    const hostile = {
      "set-cookie": "tf_session=secret",
      authorization: `Bearer ${UPSTREAM_KEY}`,
      "x-api-key": UPSTREAM_KEY,
      "www-authenticate": 'Bearer realm="tf"',
      location: "https://evil.test/",
      "access-control-allow-origin": "*",
      "access-control-allow-credentials": "true",
      "content-security-policy": "default-src *",
      "x-nebius-project": "aiproject-secret",
      via: "1.1 internal-gateway",
      server: "internal",
      "x-ratelimit-remaining-requests": "42",
      "x-request-id": "req-1",
      "retry-after": "3",
    };
    const passed = ["retry-after", "x-ratelimit-remaining-requests", "x-request-id"];
    const replies: [string, () => Response, number][] = [
      ["a whole reply", () => jsonReply(completion(SUPER, usage(10, 2)), { headers: hostile }), 200],
      ["a stream", () => sseReply(chunks(SUPER, usage(10, 2)), { headers: hostile }), 200],
      ["a passed-through 429", () => jsonReply({ error: { message: "slow down" } }, { status: 429, headers: hostile }), 429],
      ["a mapped 401", () => jsonReply({ error: { message: "bad key" } }, { status: 401, headers: hostile }), 502],
    ];
    for (const [what, reply, status] of replies) {
      upstream.handler = reply;
      const res = await relay.handle(chatRequest(code, { model: SUPER, messages, max_tokens: 8, stream: what === "a stream" }));
      expect(res.status, what).toBe(status);
      await res.text();
      for (const name of Object.keys(hostile)) {
        if (passed.includes(name)) expect(res.headers.get(name), `${what}: ${name}`).toBe(hostile[name as keyof typeof hostile]);
        else expect(res.headers.get(name), `${what}: ${name}`).toBeNull();
      }
    }
  });

  it("never lets a field name write a response header", async () => {
    const { ledger, upstream, code } = await setup();
    const raw = {
      model: SUPER,
      messages,
      max_tokens: 8,
      stream: true,
      stream_options: { "a\r\nset-cookie: x=1": true },
      chat_template_kwargs: { "b\r\nx-injected: 1": true, "c\nlocation: https://evil.test": true },
      "d\r\nx-injected: 1": 1,
      ["e".repeat(5000)]: 1,
      ...Object.fromEntries(Array.from({ length: 100 }, (_, i) => [`extra_${i}`, i])),
    };
    upstream.handler = () => sseReply(chunks(SUPER, usage(10, 2)));
    const res = await makeRelay({ ledger, fetch: upstream.fetch }).handle(chatRequest(code, raw));
    expect(res.status).toBe(200);
    await res.text();
    expect(res.headers.get("x-injected")).toBeNull();
    expect(res.headers.get("set-cookie")).toBeNull();
    expect(res.headers.get("location")).toBeNull();
    const dropped = res.headers.get("x-relay-dropped")!.split(",");
    expect(dropped.length).toBeLessThanOrEqual(24);
    for (const name of dropped) expect(name).toMatch(/^([A-Za-z0-9_.-]{1,64}|\?)$/);
  });
});

describe("the Token Factory key and the access code stay secret", () => {
  it("keeps both out of every response and every log line, whatever Token Factory answers", async () => {
    const key = "tfk_SECRET_6f1d3c0a9e8b7d6c5b4a39281706f5e4";
    const { ledger, upstream, code } = await setup();
    const logs: Record<string, unknown>[] = [];
    const relay = makeRelay({ ledger, fetch: upstream.fetch, upstreamKey: key, log: (entry) => logs.push(entry) });
    const echoKey = { error: { message: `Invalid API key: Bearer ${key}`, type: "auth" } };
    const answers: [string, () => Response | Promise<Response>, boolean][] = [
      ["whole reply", () => jsonReply(completion(SUPER, usage(10, 2))), false],
      ["stream", () => sseReply(chunks(SUPER, usage(10, 2))), true],
      ["401 echoing the key", () => jsonReply(echoKey, { status: 401 }), false],
      ["403 echoing the key", () => jsonReply(echoKey, { status: 403 }), false],
      ["402 echoing the key", () => jsonReply(echoKey, { status: 402 }), false],
      [
        "fetch failure naming the key",
        () => {
          throw Object.assign(new TypeError(`fetch failed (authorization: Bearer ${key}; code ${code})`), { cause: { code: "ECONNRESET" } });
        },
        false,
      ],
      [
        "connect failure naming the key",
        () => {
          throw Object.assign(new TypeError(`connect ECONNREFUSED Bearer ${key}`), { cause: { code: "ECONNREFUSED" } });
        },
        false,
      ],
    ];
    for (const [what, answer, stream] of answers) {
      upstream.handler = answer;
      const res = await relay.handle(chatRequest(code, { model: SUPER, messages, max_tokens: 8, stream }));
      const text = await res.text();
      const headerText = JSON.stringify([...res.headers.entries()]);
      expect(text, what).not.toContain(key);
      expect(headerText, what).not.toContain(key);
      expect(text, what).not.toContain(code);
      expect(headerText, what).not.toContain(code);
    }
    // Refusals the relay makes itself.
    const poor = await mint(ledger, { total: "0.001" });
    for (const res of [
      await relay.handle(chatRequest(code, { model: "gpt-4o", messages })),
      await relay.handle(chatRequest(poor.code, { model: ULTRA, messages })),
      await relay.handle(chatRequest(code, "{not json")),
      await relay.handle(getRequest("/v1/relay/balance", code)),
      await relay.handle(getRequest("/v1/models", code)),
      await relay.handle(getRequest("/healthz", null)),
    ]) {
      const text = await res.text();
      expect(text).not.toContain(key);
      expect(text).not.toContain(code);
      expect(text).not.toContain(poor.code);
    }
    const logText = JSON.stringify(logs);
    expect(logs.length).toBeGreaterThan(0);
    expect(logText).not.toContain(key);
    expect(logText).not.toContain(code);
  });
});

describe("access codes", () => {
  it("treats every near-variant of a code as unknown, and reads the ledger only for well-formed ones", async () => {
    const { ledger, upstream, code, hash } = await setup();
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    const lookups = vi.spyOn(ledger, "getCode");
    const reservations = vi.spyOn(ledger, "reserve");
    const swapped = code.slice(0, 3) + code.slice(3).replace(/[A-Za-z]/g, (c) => (c === c.toUpperCase() ? c.toLowerCase() : c.toUpperCase()));
    const lastChanged = code.slice(0, -1) + (code.endsWith("A") ? "B" : "A");
    // [Authorization header, expected error code, costs a ledger read]
    const variants: [string, string, boolean][] = [
      [`Bearer ${swapped}`, "unknown_code", true],
      [`Bearer ${lastChanged}`, "unknown_code", true],
      [`Bearer rc_${"A".repeat(32)}`, "unknown_code", true],
      [`Bearer ${code.slice(0, -1)}`, "unknown_code", false],
      [`Bearer ${code}x`, "unknown_code", false],
      [`Bearer ${code}é`, "unknown_code", false],
      [`Bearer ${code.replace(/^rc_/, "RC_")}`, "unknown_code", false],
      [`Bearer ${code.replace(/^rc_/, "rc-")}`, "unknown_code", false],
      [`Bearer ${code.slice(0, 10)}%${code.slice(11)}`, "unknown_code", false],
      [`Bearer ${code} ${code}`, "missing_code", false],
      [`Bearer ${code}, Bearer ${code}`, "missing_code", false],
      [`Basic ${btoa(`${code}:`)}`, "missing_code", false],
      [`Token ${code}`, "missing_code", false],
      [code, "missing_code", false],
      ["Bearer", "missing_code", false],
      ["", "missing_code", false],
    ];
    let reads = 0;
    for (const [header, why, read] of variants) {
      const res = await relay.handle(
        new Request("http://relay.test/v1/chat/completions", {
          method: "POST",
          headers: header ? { authorization: header, "content-type": "application/json" } : { "content-type": "application/json" },
          body,
        }),
      );
      expect(res.status, header).toBe(401);
      expect(((await res.json()) as { error: { code: string } }).error.code, header).toBe(why);
      if (read) reads += 1;
      expect(lookups.mock.calls.length + reservations.mock.calls.length, header).toBe(reads);
    }
    expect(upstream.calls).toHaveLength(0);
    await untouched(ledger, hash);
  });

  it("takes the code from the Authorization header only", async () => {
    const { ledger, upstream, code, hash } = await setup();
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    const elsewhere: Request[] = [
      new Request(`http://relay.test/v1/chat/completions?api_key=${code}`, { method: "POST", body }),
      new Request(`http://relay.test/v1/chat/completions?key=${code}&code=${code}`, { method: "POST", body }),
      new Request("http://relay.test/v1/chat/completions", { method: "POST", body, headers: { "x-api-key": code } }),
      new Request("http://relay.test/v1/chat/completions", { method: "POST", body, headers: { "api-key": code } }),
      new Request("http://relay.test/v1/chat/completions", { method: "POST", body, headers: { cookie: `code=${code}` } }),
      new Request("http://relay.test/v1/chat/completions", { method: "POST", body, headers: { "proxy-authorization": `Bearer ${code}` } }),
      new Request(`http://relay.test/v1/chat/completions`, {
        method: "POST",
        body: JSON.stringify({ model: SUPER, messages, max_tokens: 8, api_key: code, user: code }),
      }),
    ];
    for (const req of elsewhere) {
      const res = await relay.handle(req);
      expect(res.status).toBe(401);
      await res.text();
    }
    expect(upstream.calls).toHaveLength(0);
    await untouched(ledger, hash);
  });
});

describe("model ids", () => {
  it("refuses ids that only resemble an allowlisted one, and never calls upstream", async () => {
    const { ledger, upstream, code, hash } = await setup();
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    const lookalikes = [
      LIGHTNING.toUpperCase(),
      LIGHTNING.toLowerCase(),
      ULTRA.toLowerCase(),
      "NVIDIA/Nemotron-3_5-Lightning",
      ` ${LIGHTNING}`,
      `${LIGHTNING} `,
      `${LIGHTNING}\n`,
      `${LIGHTNING}\t`,
      `${LIGHTNING}\u0000`,
      `${LIGHTNING}​`,
      `﻿${LIGHTNING}`,
      LIGHTNING.replace("nvidia", "nvidiа"), // Cyrillic a
      LIGHTNING.replace("n", "ｎ"), // fullwidth n
      LIGHTNING.normalize("NFKD") === LIGHTNING ? `${LIGHTNING}́` : LIGHTNING.normalize("NFKD"),
      "nvidia//Nemotron-3_5-Lightning",
      "nvidia/Nemotron-3_5-Lightning/",
      "./nvidia/Nemotron-3_5-Lightning",
      "nvidia/../nvidia/Nemotron-3_5-Lightning",
      `${LIGHTNING}-fast`,
      `${LIGHTNING}:flex`,
      `${LIGHTNING}@latest`,
      `${ULTRA},${LIGHTNING}`,
      "nvidia/llama-3.1-nemotron-70b-instruct",
      "nvidia/",
      "nvidia/*",
      "__proto__",
      "constructor",
      "toString",
      "hasOwnProperty",
    ];
    for (const model of lookalikes) {
      const res = await relay.handle(chatRequest(code, { model, messages, max_tokens: 8 }));
      expect(res.status, JSON.stringify(model)).toBe(403);
      expect(((await res.json()) as { error: { type: string } }).error.type).toBe("relay_model_not_allowed");
    }
    for (const model of [[LIGHTNING], { id: LIGHTNING }, 1, true, "", "   "]) {
      const res = await relay.handle(chatRequest(code, { model, messages, max_tokens: 8 }));
      expect(res.status, JSON.stringify(model)).toBe(400);
      await res.text();
    }
    expect(upstream.calls).toHaveLength(0);
    await untouched(ledger, hash);
  });

  it("sends the allowlisted id itself upstream, never the client's string", async () => {
    const { ledger, upstream, code } = await setup();
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    const res = await relay.handle(chatRequest(code, `{"model":"gpt-4o","model":"${LIGHTNING}","messages":[{"role":"user","content":"hi"}],"max_tokens":8}`));
    expect(res.status).toBe(200);
    await res.text();
    expect(upstream.calls[0]!.body.model).toBe(LIGHTNING);
    const last = await relay.handle(chatRequest(code, `{"model":"${LIGHTNING}","model":"gpt-4o","messages":[{"role":"user","content":"hi"}]}`));
    expect(last.status).toBe(403);
    await last.text();
    expect(upstream.calls).toHaveLength(1);
  });
});

describe("hostile bodies", () => {
  it("answers deeply nested JSON with 400, without reserving or calling upstream", async () => {
    const { ledger, upstream, code, hash } = await setup();
    const logs: Record<string, unknown>[] = [];
    const relay = makeRelay({ ledger, fetch: upstream.fetch, log: (entry) => logs.push(entry) });
    const depth = 200_000;
    for (const raw of [
      `{"model":"${SUPER}","messages":[{"role":"user","content":${"[".repeat(depth)}${"]".repeat(depth)}}]}`,
      `{"model":"${SUPER}","messages":[{"role":"user","content":"hi"}],"tools":[{"type":"function","function":{"name":"f","parameters":${'{"a":'.repeat(depth)}1${"}".repeat(depth)}}}]}`,
      `{"model":"${SUPER}","messages":[{"role":"user","content":"hi"}],"response_format":{"type":"json_object","x":${"[".repeat(depth)}${"]".repeat(depth)}}}`,
    ]) {
      const res = await relay.handle(chatRequest(code, raw));
      expect(res.status).toBe(400);
      expect(await res.json()).toMatchObject({ error: { type: "invalid_request_error", code: "too_deep" } });
    }
    expect(upstream.calls).toHaveLength(0);
    await untouched(ledger, hash);
    // The relay keeps serving.
    const ok = await relay.handle(chatRequest(code, { model: SUPER, messages, max_tokens: 8 }));
    expect(ok.status).toBe(200);
    await ok.text();
    // 64 levels are fine; one more is not.
    const nest = (levels: number) => `${"[".repeat(levels - 3)}0${"]".repeat(levels - 3)}`;
    const at = (levels: number) =>
      relay.handle(chatRequest(code, `{"model":"${SUPER}","messages":[{"role":"user","content":"hi","x":${nest(levels)}}],"max_tokens":8}`));
    expect((await at(64)).status).toBe(200);
    expect((await at(65)).status).toBe(400);
  });

  it("refuses oversized bodies, lists and prompts before Token Factory sees them", async () => {
    const { ledger, upstream, code, hash } = await setup();
    await ledger.updateCode(hash, { capTotal: usdToNano("1.5"), capDaily: usdToNano("1.5") });
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    const huge = "x".repeat(4 * 1024 * 1024 + 1);
    const tooBig = await relay.handle(chatRequest(code, huge));
    expect(tooBig.status).toBe(413);
    // The same without a content-length: a streamed body is cut at the limit while it is read.
    const bytes = new TextEncoder().encode(huge);
    const streamed = new ReadableStream<Uint8Array>({
      start(controller) {
        for (let at = 0; at < bytes.length; at += 65_536) controller.enqueue(bytes.slice(at, at + 65_536));
        controller.close();
      },
    });
    const chunked = await relay.handle(
      new Request("http://relay.test/v1/chat/completions", {
        method: "POST",
        headers: { authorization: `Bearer ${code}` },
        body: streamed,
        duplex: "half",
      } as RequestInit & { duplex: "half" }),
    );
    expect(chunked.status).toBe(413);
    const manyTools = Array.from({ length: 513 }, (_, i) => ({ type: "function", function: { name: `f${i}` } }));
    expect((await relay.handle(chatRequest(code, { model: SUPER, messages, tools: manyTools }))).status).toBe(400);
    const manyMessages = Array.from({ length: 4097 }, () => ({ role: "user", content: "x" }));
    expect((await relay.handle(chatRequest(code, { model: SUPER, messages: manyMessages }))).status).toBe(400);
    // A 3.9 MB prompt is priced by its bytes: far over a $1.50 code, so it is refused up front.
    const long = await relay.handle(chatRequest(code, { model: ULTRA, messages: [{ role: "user", content: "x".repeat(3_900_000) }], max_tokens: 1 }));
    expect(long.status).toBe(402);
    expect(((await long.json()) as { error: { needed_usd: number } }).error.needed_usd).toBeGreaterThan(3.9);
    expect(upstream.calls).toHaveLength(0);
    await untouched(ledger, hash);
  });
});

describe("Retry-After", () => {
  function tight(rpm: number) {
    const raw = JSON.parse(CONFIG_TEXT);
    raw.limiter_fraction = 0.5;
    raw.models[LIGHTNING].rpm = rpm * 2; // the relay budgets half
    raw.code_share_pct = 100; // one code may use the model's whole budget here
    return parseConfig(raw);
  }

  it("never holds a call past the relay's wait budget, and its own Retry-After stays bounded", async () => {
    const { ledger, upstream, code } = await setup();
    upstream.handler = () => jsonReply(completion(LIGHTNING, usage(5, 1)));
    const minute = Math.floor(Date.now() / 60_000) * 60_000;
    let clock = minute + 10_000;
    const slept: number[] = [];
    const attempts = vi.spyOn(ledger, "reserve");
    const relay = makeRelay({
      ledger,
      fetch: upstream.fetch,
      config: tight(2),
      limiterWaitMs: 250,
      now: () => clock,
      sleep: async (ms) => {
        slept.push(ms);
        clock += ms;
      },
    });
    const call = async () => {
      slept.length = 0;
      const before = attempts.mock.calls.length;
      const res = await relay.handle(chatRequest(code, { model: LIGHTNING, messages, max_tokens: 4 }));
      await res.text();
      expect(slept.reduce((a, b) => a + b, 0)).toBeLessThanOrEqual(250);
      expect(attempts.mock.calls.length - before).toBeLessThanOrEqual(1 + 250);
      if (res.status === 429) {
        const retry = Number(res.headers.get("retry-after"));
        expect(Number.isInteger(retry)).toBe(true);
        expect(retry).toBeGreaterThanOrEqual(1);
        expect(retry).toBeLessThanOrEqual(120);
      }
      return { status: res.status, slept: slept.reduce((a, b) => a + b, 0) };
    };
    // Two calls fill this minute's budget of 2; a third waits for no one and is refused.
    expect((await call()).status).toBe(200);
    expect((await call()).status).toBe(200);
    expect(await call()).toMatchObject({ status: 429, slept: 0 });
    // 29.9 s into the next minute the old calls weigh just over the budget: a short wait.
    clock = minute + 60_000 + 29_900;
    const waited = await call();
    expect(waited.status).toBe(200);
    expect(waited.slept).toBeGreaterThan(0);
    // Right after, the window is full for ~30 s: refused at once, with a bounded Retry-After.
    expect(await call()).toMatchObject({ status: 429, slept: 0 });
  });

  it("passes Token Factory's Retry-After on without waiting on it or retrying", async () => {
    const { ledger, upstream, code, hash } = await setup();
    const slept: number[] = [];
    upstream.handler = () => jsonReply({ error: { message: "busy" } }, { status: 429, headers: { "retry-after": "86400" } });
    const relay = makeRelay({ ledger, fetch: upstream.fetch, limiterWaitMs: 10_000, sleep: async (ms) => void slept.push(ms) });
    const res = await relay.handle(chatRequest(code, { model: SUPER, messages, max_tokens: 8 }));
    expect(res.status).toBe(429);
    expect(res.headers.get("retry-after")).toBe("86400");
    expect(upstream.calls).toHaveLength(1);
    expect(slept).toEqual([]);
    expect(await ledger.getCode(hash)).toMatchObject({ spent: 0, reserved: 0 });
  });
});

describe("review findings, closed", () => {
  // R4: every unauthenticated request with a well-formed code, and every /healthz probe,
  // cost an Upstash command. The free tier is 500K commands a month, so a scanner that
  // learns the documented rc_ format could switch the ledger off for everyone. A chat call
  // now goes straight to its reservation, unknown and revoked codes are remembered per
  // instance, and /healthz reads the ledger at most once every 30 s.
  it("R4: repeated probes with one unknown well-formed code cost one ledger read, not one each", async () => {
    const { ledger, upstream } = await setup();
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    const lookups = vi.spyOn(ledger, "getCode");
    const reservations = vi.spyOn(ledger, "reserve");
    const stranger = `rc_${"Z".repeat(32)}`;
    for (let i = 0; i < 50; i += 1) {
      const res = await relay.handle(chatRequest(stranger, body));
      expect(res.status).toBe(401);
      await res.text();
    }
    for (let i = 0; i < 10; i += 1) {
      for (const route of ["/v1/models", "/v1/relay/balance"]) expect((await relay.handle(getRequest(route, stranger))).status).toBe(401);
    }
    expect(lookups.mock.calls.length).toBeLessThanOrEqual(1);
    expect(lookups.mock.calls.length + reservations.mock.calls.length).toBe(1);
    expect(upstream.calls).toHaveLength(0);
  });

  it("R4: /healthz does not read the ledger on every probe", async () => {
    const { ledger, upstream } = await setup();
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    const reads = vi.spyOn(ledger, "getGlobal");
    for (let i = 0; i < 50; i += 1) await (await relay.handle(getRequest("/healthz", null))).text();
    expect(reads.mock.calls.length).toBeLessThanOrEqual(1);
  });

  it("R4: remembers a revoked code too, and forgets both kinds after a while", async () => {
    const { ledger, upstream, code, hash } = await setup();
    let clock = Date.now();
    const relay = makeRelay({ ledger, fetch: upstream.fetch, now: () => clock, badCodeTtlMs: 60_000 });
    const reservations = vi.spyOn(ledger, "reserve");
    await ledger.updateCode(hash, { revoked: true });
    for (let i = 0; i < 20; i += 1) {
      const res = await relay.handle(chatRequest(code, body));
      expect(res.status).toBe(401);
      expect(((await res.json()) as { error: { code: string } }).error.code).toBe("code_revoked");
    }
    expect(reservations).toHaveBeenCalledTimes(1);
    clock += 61_000;
    expect((await relay.handle(chatRequest(code, body))).status).toBe(401);
    expect(reservations).toHaveBeenCalledTimes(2);
  });

  it("R4: /healthz reads the ledger again once its 30 s are up", async () => {
    const { ledger, upstream } = await setup();
    let clock = Date.now();
    const relay = makeRelay({ ledger, fetch: upstream.fetch, now: () => clock });
    expect((await relay.handle(getRequest("/healthz", null))).status).toBe(200);
    await ledger.setDisabled(true);
    expect((await relay.handle(getRequest("/healthz", null))).status).toBe(200);
    clock += 30_000;
    const later = await relay.handle(getRequest("/healthz", null));
    expect(later.status).toBe(503);
    expect(await later.json()).toMatchObject({ ok: false, disabled: true });
  });

  // Upstash over its monthly limit refused about half of all commands. /healthz read the
  // ledger once, got through, and said ok for 30 s while calls got relay_ledger_unavailable.
  const refusal = new Error("ERR max requests limit exceeded. Limit: 500000, Usage: 500016");

  it("/healthz answers 503 for 30 s after the ledger failed a call, though its own read went through", async () => {
    const { ledger, upstream, code } = await setup();
    let clock = Date.now();
    const relay = makeRelay({ ledger, fetch: upstream.fetch, now: () => clock });
    const health = async () => {
      const res = await relay.handle(getRequest("/healthz", null));
      return { status: res.status, body: (await res.json()) as Record<string, unknown> };
    };
    expect((await health()).status).toBe(200);
    const reads = vi.spyOn(ledger, "getGlobal");
    vi.spyOn(ledger, "snapshot").mockRejectedValueOnce(refusal);
    const models = await relay.handle(getRequest("/v1/models", code));
    expect(models.status).toBe(503);
    expect(await models.json()).toMatchObject({ error: { type: "relay_ledger_unavailable" } });
    clock += 1_000;
    expect(await health()).toMatchObject({ status: 503, body: { ok: false, reason: "ledger refusing commands" } });
    // After 30 s with no failure it reads the ledger again, which answers.
    clock += 29_000;
    expect((await health()).status).toBe(200);
    expect(reads).toHaveBeenCalledTimes(1);
    // A chat call whose reservation fails counts the same.
    vi.spyOn(ledger, "reserve").mockRejectedValueOnce(refusal);
    const chat = await relay.handle(chatRequest(code, body));
    expect(chat.status).toBe(503);
    expect(await chat.json()).toMatchObject({ error: { type: "relay_ledger_unavailable" } });
    clock += 29_999;
    expect((await health()).body).toMatchObject({ ok: false, reason: "ledger refusing commands" });
    expect(upstream.calls).toHaveLength(0);
  });

  it("/healthz counts a settlement the ledger failed after Token Factory answered", async () => {
    const { ledger, upstream, code } = await setup();
    let clock = Date.now();
    const relay = makeRelay({ ledger, fetch: upstream.fetch, now: () => clock });
    expect((await relay.handle(getRequest("/healthz", null))).status).toBe(200);
    vi.spyOn(ledger, "settle").mockRejectedValueOnce(refusal);
    const answered = await relay.handle(chatRequest(code, body));
    expect(answered.status).toBe(200);
    await answered.text();
    expect(upstream.calls).toHaveLength(1);
    clock += 1_000;
    const res = await relay.handle(getRequest("/healthz", null));
    expect(res.status).toBe(503);
    expect(await res.json()).toMatchObject({ ok: false, reason: "ledger refusing commands" });
  });
});
