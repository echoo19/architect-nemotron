/**
 * W2 security review, re-attack of the fixed relay (wave 3).
 *
 * The fix closed R1 for the shapes it measured (tool schemas that repeat keys, astral
 * characters, number-heavy arguments). This file re-attacks the same guarantees with shapes
 * the fix did not try. The R1 holes it found (U+007F and two-byte non-printable characters
 * in str()-printed schema fields) are closed: the hold adds per_tool_escape_char_tokens for
 * every character from U+007F to U+07FF inside tools, and those tests are plain `it` now.
 * It still pins one hole (R2: a 4xx that follows generation) and locks what held: the R2
 * release boundary, the calls-in-flight count on every settlement path and across relays,
 * a streamed overshoot, and variants of R5 and R6.
 *
 * Offline measurement uses the fixer's own harness: the published chat_template.jinja and
 * tokenizer.json of the served models (HF nvidia Nemotron-3 Nano, Super, Ultra and
 * Nemotron-3.5 Lightning), vLLM's preprocessing, transformers' tojson. Rendered token counts
 * cited below come from render.py over the exact bodies these tests send.
 *
 * The still-open tests are `it.fails`: they fail while the hole is open, which vitest
 * reports green. When a hole is closed its test turns red; switch it to a plain `it` then,
 * as the earlier review's R-number tests were switched.
 */

import RedisMock from "ioredis-mock";
import { afterAll, describe, expect, it } from "vitest";
import { parseConfig } from "../src/config.js";
import type { Ledger, RelayEvent } from "../src/ledger/types.js";
import { UpstashLedger } from "../src/ledger/upstash.js";
import { usdToNano } from "../src/money.js";
import { prepareRequest, promptShape, reservationNano } from "../src/request.js";
import {
  CONFIG_TEXT,
  FakeUpstream,
  SUPER,
  ULTRA,
  chatRequest,
  chunks,
  cleanupDirs,
  completion,
  config,
  fileLedger,
  jsonReply,
  makeRelay,
  messages,
  mint,
  sseReply,
  usage,
  waitFor,
} from "./helpers.js";
import { IoredisMockAdapter } from "./ioredis-adapter.js";

afterAll(() => cleanupDirs());

const ultra = config.models.get(ULTRA)!;

function worstCase(body: Record<string, unknown>): number {
  const prepared = prepareRequest(body, config);
  if (!prepared.ok) throw new Error(`refused: ${prepared.error.message}`);
  return reservationNano(prepared.value.model, prepared.value.inputTokensUpper, prepared.value.maxTokens);
}

/** Ultra with the token-per-minute budget lifted, so only the money caps decide a race. */
const roomyUltra = (() => {
  const raw = JSON.parse(CONFIG_TEXT);
  raw.models[ULTRA].tpm = 100_000_000;
  return parseConfig(raw);
})();

const DEL = "\u007f"; // one UTF-8 byte in the request; Python str() escapes it to "\x7f" (4 chars, 3 tokens)
const range = (n: number) => Array.from({ length: n }, (_, i) => i);

/** A tool whose parameters carry `count` properties, each a list-valued `description`. */
function descListTool(count: number, len: number) {
  return {
    type: "function",
    function: {
      name: "f",
      parameters: {
        type: "object",
        properties: Object.fromEntries(range(count).map((i) => [`p${i}`, { description: [DEL.repeat(len)] }])),
      },
    },
  };
}

describe("R1 re-attack: a list-valued schema field of U+007F is held at what it renders", () => {
  // Nemotron's template prints a parameter's `description`/`type` with Jinja `| trim` / `|
  // string`, i.e. Python str(). For a LIST value that is str(list), which escapes U+007F as
  // "\x7f" (4 chars -> 3 tokens) although the request sends it as one raw byte. The hold
  // priced `tools` at 2 tokens a byte and surcharged only surrogate-pair (astral)
  // characters, so a BMP control like U+007F was missed and the prompt rendered at ~3
  // tokens a byte. Every character from U+007F to U+07FF inside tools now adds 2 tokens.

  it("prices one small list-valued description of U+007F at or above what it renders", () => {
    // Body: one parameter, description = [U+007F x 2000]. 2,236 request bytes.
    // Measured offline: Nano and Ultra both render 6,256 prompt tokens. Held: 8,977
    // (4,977 before the fix).
    const body = { model: ULTRA, messages: [{ role: "user", content: "hi" }], tools: [descListTool(1, 2000)], max_tokens: 1 };
    const held = worstCase(body);
    const renderedCost = 6_256 * ultra.inNano + 1 * ultra.outNano; // the real cost this call would settle at
    // The worst case covers a prompt rendered with the published template.
    expect(held).toBeGreaterThanOrEqual(renderedCost);
  });

  it("keeps concurrent crafted calls from carrying a code past its total cap", async () => {
    const ledger = fileLedger("adv2-r1");
    await ledger.setGlobalCap(usdToNano("100"));
    const { code, hash } = await mint(ledger, { total: "1.5" });
    // Body: 10 parameters, each description = [U+007F x 20000]. 200,470 request bytes.
    // Held 801,517 prompt tokens ($0.80152 incl. max_tokens 1; 401,517 before the fix).
    // Measured render: 600,463 prompt tokens ($0.600466).
    const body = { model: ULTRA, messages: [{ role: "user", content: "hi" }], tools: [descListTool(10, 20000)], max_tokens: 1 };
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    const upstream = new FakeUpstream(async () => {
      await gate;
      return jsonReply(completion(ULTRA, usage(600_463, 1)));
    });
    const relay = makeRelay({ ledger, fetch: upstream.fetch, config: roomyUltra });
    let refused = 0;
    const calls = Array.from({ length: 6 }, () =>
      relay.handle(chatRequest(code, body)).then(async (res) => {
        if (res.status !== 200) refused += 1;
        await res.text();
        return res.status;
      }),
    );
    await waitFor(() => upstream.calls.length + refused === 6, 30_000);
    release();
    await Promise.all(calls);
    const state = (await ledger.getCode(hash))!;
    // Before the fix the hold let three calls under the $1.50 cap; each settled at $0.60,
    // so the code spent about $1.80 before the overshoot freeze caught the next one. Now
    // one call's hold covers its render, so the cap cannot be passed.
    expect(state.spent).toBeLessThanOrEqual(state.capTotal);
    expect(state.frozen).toBe(false); // no call settled above its hold
  });
});

describe("R1 re-attack: shapes the fix does cover stay covered (regression)", () => {
  it("still surcharges real astral (supplementary-plane) characters in tools", () => {
    const astral = String.fromCodePoint(0xe0001); // a surrogate pair, outside the BMP
    const tools = [
      {
        type: "function",
        function: { name: "f", parameters: { type: "object", properties: { p: { description: [astral.repeat(100)] } } } },
      },
    ];
    const shape = promptShape(tools, [{ role: "user", content: "hi" }]);
    expect(shape.toolAstralChars).toBe(100);
  });

  it("still prices tool-call argument numbers above their bytes", () => {
    // "1e15" is 4 bytes but Python prints the parsed float as 18 characters; the hold adds
    // 16 tokens a number on top of 2 tokens a byte, so the estimate stays above the render.
    const withNumbers = {
      model: ULTRA,
      messages: [
        { role: "user", content: "x" },
        { role: "assistant", content: "", tool_calls: [{ id: "c", type: "function", function: { name: "f", arguments: `{"a":[${range(1000).map(() => "1e15").join(",")}]}` } }] },
        { role: "user", content: "y" },
      ],
      max_tokens: 1,
    };
    const shape = promptShape(undefined, withNumbers.messages);
    expect(shape.argumentNumbers).toBe(1000);
  });
});

describe("R2 re-attack: the 5xx release boundary holds (regression)", () => {
  async function probe(status: number, afterMs: number): Promise<{ status: number; kind: string; reconcile: boolean; actual: number; reserved: number }> {
    const ledger = fileLedger("adv2-r2");
    await ledger.setGlobalCap(usdToNano("10"));
    const { code } = await mint(ledger, { total: "5" });
    let clock = Math.floor(Date.parse("2026-10-03T12:00:00Z")); // fixed noon UTC
    const upstream = new FakeUpstream(() => {
      clock += afterMs; // the answer comes back afterMs after the call went out
      return jsonReply({ error: { message: `upstream ${status}` } }, { status });
    });
    const relay = makeRelay({ ledger, fetch: upstream.fetch, now: () => clock });
    const res = await relay.handle(chatRequest(code, { model: ULTRA, messages: [{ role: "user", content: "hi" }], max_tokens: 1000 }));
    await res.text();
    const event = (await ledger.report({ days: [], events: 1, nowMs: clock })).events[0]!;
    return { status: res.status, kind: event.kind, reconcile: Boolean(event.reconcile), actual: event.actual, reserved: event.reserved };
  }

  it("releases a 5xx that returns at exactly the release window, books one 1 ms later", async () => {
    const within = await probe(503, config.release5xxWithinMs); // boundary: <= window releases
    expect(within).toMatchObject({ kind: "released", actual: 0 });
    const after = await probe(503, config.release5xxWithinMs + 1); // just past it: booked in full
    expect(after.kind).toBe("full");
    expect(after.reconcile).toBe(true);
    expect(after.actual).toBe(after.reserved);
  });

  it("books a 408 in full however fast it comes back", async () => {
    const fast = await probe(408, 0);
    expect(fast.kind).toBe("full");
    expect(fast.reconcile).toBe(true);
    expect(fast.actual).toBe(fast.reserved);
  });
});

// -- second re-attack ----------------------------------------------------------------------

const SIGN = "؀"; // ARABIC NUMBER SIGN, a format character: two UTF-8 bytes, which repr() spells ؀

/** A tool whose one parameter has a list-valued `description` holding `text`. */
function listedDescriptionTool(text: string) {
  return { type: "function", function: { name: "f", parameters: { type: "object", properties: { p0: { description: [text] } } } } };
}

/** A tool whose one parameter has a dict-valued `type`: str() escapes its keys and values alike. */
function dictTypeTool(text: string) {
  return { type: "function", function: { name: "f", parameters: { type: "object", properties: { p0: { type: { [text]: text } } } } } };
}

const signBody = { model: ULTRA, messages: [{ role: "user", content: "hi" }], tools: [listedDescriptionTool(SIGN.repeat(2000))], max_tokens: 1 };
const dictTypeBody = { model: ULTRA, messages: [{ role: "user", content: "hi" }], tools: [dictTypeTool(DEL.repeat(1000))], max_tokens: 1 };

function payloadBytes(body: Record<string, unknown>): number {
  const prepared = prepareRequest(body, config);
  if (!prepared.ok) throw new Error(`refused: ${prepared.error.message}`);
  return prepared.value.payload.length;
}

async function eventually(check: () => Promise<boolean>, what = "condition", timeoutMs = 10_000): Promise<void> {
  const start = Date.now();
  while (!(await check())) {
    if (Date.now() - start > timeoutMs) throw new Error(`eventually: timed out waiting for ${what}`);
    await new Promise((r) => setTimeout(r, 5));
  }
}

async function recentEvents(ledger: Ledger, count: number, nowMs = Date.now()): Promise<RelayEvent[]> {
  return (await ledger.report({ days: [], events: count, nowMs })).events;
}

describe("R1 re-attack 2: two-byte non-printable characters and dict-valued types are held", () => {
  // repr() escapes every character Python cannot print, U+007F among them. A two-byte character
  // with no printable form (unassigned, or a format character: U+0378, U+0590, U+0600 to
  // U+0605, U+061C, U+070F and others) becomes ؀: six characters and five tokens for two
  // request bytes, 2.5 tokens a byte where the hold allowed 2. A surcharge on U+007F alone
  // would leave this at 1.25 times the hold (a 400,470-byte request of ten such parameters
  // would hold 801,517 prompt tokens and render to 1,000,463), so the surcharge covers every
  // character from U+007F to U+07FF. A dict-valued `type` is printed with str() too, so it
  // carries U+007F just as a list-valued description does.
  //
  // Nano, Lightning, Super and Ultra render these bodies alike: their templates share the tool
  // section, and Lightning, Super and Ultra ship the same tokenizer.json.

  it("sends exactly the bodies whose rendering was measured", () => {
    expect(payloadBytes(signBody)).toBe(4_236);
    expect(payloadBytes(dictTypeBody)).toBe(2_232);
  });

  it("prices a list-valued description of U+0600 at or above what it renders", () => {
    // Held: 12,977 prompt tokens (8,977 before the fix). Rendered with each served model's
    // template: 10,256.
    expect(worstCase(signBody)).toBeGreaterThanOrEqual(10_256 * ultra.inNano + 1 * ultra.outNano);
  });

  it("prices a dict-valued type of U+007F at or above what it renders", () => {
    // Held: 8,973 prompt tokens (4,973 before the fix). Rendered: 6,257.
    expect(worstCase(dictTypeBody)).toBeGreaterThanOrEqual(6_257 * ultra.inNano + 1 * ultra.outNano);
  });
});

describe("R2 re-attack 2: a 4xx can follow generation too", () => {
  // vLLM parses a non-streamed tool_choice "required" reply with pydantic. When max_tokens cut
  // the reply short, that parse fails with a ValueError, and the server answers 400 after it
  // generated all of max_tokens. Checked in 0.10.1, 0.11.0, 0.12.0, 0.13.0, 0.14.0, 0.15.0 and
  // 0.17.0; from 0.19.0 on the parse failure is suppressed. The relay releases every 4xx but
  // 408 however late it comes, so on such a server, if Token Factory bills those tokens, this
  // shape generates for free, eight calls at a time per code. Holding 4xx answers to the same
  // time window as 5xx closes it.
  it.fails("books a 400 that comes back two minutes after the call went out", async () => {
    const ledger = fileLedger("adv2-r2late");
    await ledger.setGlobalCap(usdToNano("10"));
    const { code } = await mint(ledger, { total: "5" });
    let clock = Date.parse("2026-10-03T12:00:00Z");
    const upstream = new FakeUpstream(() => {
      clock += 120_000; // all 8,192 tokens were generated before the parse failed
      return jsonReply(
        { object: "error", message: "1 validation error for list[FunctionDefinition]\n  Invalid JSON: EOF while parsing a string", type: "BadRequestError", code: 400 },
        { status: 400 },
      );
    });
    const relay = makeRelay({ ledger, fetch: upstream.fetch, now: () => clock });
    const body = {
      model: ULTRA,
      messages: [{ role: "user", content: "Call f with a text of 50,000 words." }],
      tools: [{ type: "function", function: { name: "f", parameters: { type: "object", properties: { text: { type: "string" } }, required: ["text"] } } }],
      tool_choice: "required",
      max_tokens: 8192,
      chat_template_kwargs: { enable_thinking: false },
    };
    const res = await relay.handle(chatRequest(code, body));
    expect(res.status).toBe(400);
    await res.text();
    const [event] = await recentEvents(ledger, 1, clock);
    expect(event!.kind).toBe("full");
    expect(event!.actual).toBe(event!.reserved);
  });
});

describe("calls in flight: every way a call ends gives its slot back (regression)", () => {
  it("leaves no call counted as running, and nothing held, after each kind of ending", async () => {
    const ledger = fileLedger("adv2-inflight");
    await ledger.setGlobalCap(usdToNano("10"));
    const { code, hash } = await mint(ledger, { total: "5" });
    let clock = Math.floor(Date.now() / 86_400_000) * 86_400_000 + 12 * 3_600_000; // noon UTC: one day throughout
    const background: Promise<unknown>[] = [];
    const upstream = new FakeUpstream();
    const relay = makeRelay({ ledger, fetch: upstream.fetch, now: () => clock, waitUntil: (p) => void background.push(p) });
    const whole = { model: SUPER, messages, max_tokens: 8 };
    const streamed = { ...whole, stream: true };
    const send = async (body: Record<string, unknown>, signal?: AbortSignal): Promise<number> => {
      const res = await relay.handle(chatRequest(code, body, signal ? { signal } : {}));
      await res.text().catch(() => undefined);
      return res.status;
    };
    const answer = (handler: FakeUpstream["handler"]) => {
      upstream.handler = handler;
    };
    const reserve = ledger.reserve.bind(ledger);
    const endings: [string, number, () => Promise<number>][] = [
      ["settled from usage", 200, () => (answer(() => jsonReply(completion(SUPER, usage(10, 2)))), send(whole))],
      ["a reply without usage", 200, () => (answer(() => jsonReply({ id: "x", choices: [] })), send(whole))],
      ["a stream with usage", 200, () => (answer(() => sseReply(chunks(SUPER, usage(10, 2)))), send(streamed))],
      ["a stream that breaks", 200, () => (answer(() => sseReply(chunks(SUPER, usage(10, 2)), { breakAfter: 1 })), send(streamed))],
      ["a fast 4xx", 400, () => (answer(() => jsonReply({ error: { message: "bad" } }, { status: 400 })), send(whole))],
      ["a fast 5xx", 503, () => (answer(() => jsonReply({ error: { message: "busy" } }, { status: 503 })), send(whole))],
      [
        "a slow 5xx",
        504,
        () => (
          answer(() => {
            clock += 30_000;
            return jsonReply({ error: { message: "gateway timeout" } }, { status: 504 });
          }),
          send(whole)
        ),
      ],
      [
        "Token Factory unreachable",
        502,
        () => (
          answer(() => {
            throw Object.assign(new TypeError("fetch failed"), { cause: { code: "ECONNREFUSED" } });
          }),
          send(whole)
        ),
      ],
      [
        "the line dropped",
        502,
        () => (
          answer(() => {
            throw Object.assign(new TypeError("fetch failed"), { cause: { code: "ECONNRESET" } });
          }),
          send(whole)
        ),
      ],
      [
        "the client left before the send",
        499,
        async () => {
          const client = new AbortController();
          ledger.reserve = async (r) => {
            const outcome = await reserve(r);
            client.abort();
            return outcome;
          };
          try {
            return await send(whole, client.signal);
          } finally {
            ledger.reserve = reserve;
          }
        },
      ],
      [
        "the client left mid-stream",
        200,
        async () => {
          answer((call) => sseReply(chunks(SUPER, usage(1, 1)), { hangAfter: 2, signal: call.signal }));
          const client = new AbortController();
          const res = await relay.handle(chatRequest(code, streamed, { signal: client.signal }));
          const reader = res.body!.getReader();
          await reader.read();
          client.abort();
          await reader.cancel();
          return res.status;
        },
      ],
      [
        "the reserve reply was lost",
        503,
        async () => {
          ledger.reserve = async (r) => {
            await reserve(r);
            throw new Error("fetch failed: ECONNRESET");
          };
          try {
            return await send(whole);
          } finally {
            ledger.reserve = reserve;
          }
        },
      ],
    ];
    for (const [what, status, end] of endings) {
      expect(await end(), what).toBe(status);
      await Promise.all(background);
      await eventually(async () => {
        const state = (await ledger.getCode(hash))!;
        return state.inFlight === 0 && state.reserved === 0;
      }, what);
    }
    // A host that died mid-call leaves its hold behind; the sweep books it and frees the slot.
    const abandoned = {
      id: "r_abandoned",
      hash,
      model: SUPER,
      day: new Date(clock).toISOString().slice(0, 10),
      window: Math.floor(clock / 60_000),
      elapsedMs: clock % 60_000,
      amount: 1000,
      rateTokens: 10,
      rpm: 150,
      tpm: 100_000,
      nowMs: clock,
      deadlineMs: clock - 1,
      defaultSharePct: 25,
      defaultMaxInFlight: 8,
    };
    expect(await ledger.reserve(abandoned)).toMatchObject({ ok: true });
    expect((await ledger.getCode(hash))!.inFlight).toBe(1);
    expect(await ledger.sweep(clock)).toBe(1);
    expect(await ledger.getCode(hash)).toMatchObject({ inFlight: 0, reserved: 0, requests: endings.length + 1 });
    expect((await ledger.getGlobal()).reserved).toBe(0);
    expect(await recentEvents(ledger, 100, clock)).toHaveLength(endings.length + 1);
  });

  it("holds a code to its calls in flight when two relays race on one Redis (Lua)", async () => {
    const redis = new RedisMock();
    const a = new UpstashLedger(new IoredisMockAdapter(redis), { prefix: "adv2inflight" });
    const b = new UpstashLedger(new IoredisMockAdapter(redis), { prefix: "adv2inflight" });
    await a.setGlobalCap(usdToNano("100"));
    const { code, hash } = await mint(a, { total: "50" });
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    const upstream = new FakeUpstream(async () => {
      await gate;
      return jsonReply(completion(SUPER, usage(10, 2)));
    });
    const relays = [makeRelay({ ledger: a, fetch: upstream.fetch }), makeRelay({ ledger: b, fetch: upstream.fetch })];
    const refusals: string[] = [];
    const calls = Array.from({ length: 24 }, (_, i) =>
      relays[i % 2]!.handle(chatRequest(code, { model: SUPER, messages, max_tokens: 8 })).then(async (res) => {
        if (res.status !== 200) refusals.push(`${res.status} ${((await res.json()) as { error: { code: string } }).error.code}`);
        else await res.text();
        return res.status;
      }),
    );
    await waitFor(() => upstream.calls.length + refusals.length === 24, 30_000);
    expect(upstream.calls).toHaveLength(config.codeMaxInFlight);
    expect((await a.getCode(hash))!.inFlight).toBe(config.codeMaxInFlight);
    release();
    const statuses = await Promise.all(calls);
    expect(statuses.filter((s) => s === 200)).toHaveLength(config.codeMaxInFlight);
    expect(refusals).toEqual(Array.from({ length: 24 - config.codeMaxInFlight }, () => "429 in_flight"));
    expect(await b.getCode(hash)).toMatchObject({ inFlight: 0, reserved: 0, requests: config.codeMaxInFlight });
  });
});

describe("settlement and R5/R6 variants (regression)", () => {
  it("books a streamed call's usage above its hold in full, and freezes the code", async () => {
    const ledger = fileLedger("adv2-overshoot");
    await ledger.setGlobalCap(usdToNano("10"));
    const { code, hash } = await mint(ledger, { total: "0.05" });
    const body = { model: ULTRA, messages, max_tokens: 1, stream: true };
    const held = worstCase(body);
    const upstream = new FakeUpstream(() => sseReply(chunks(ULTRA, usage(20_000, 1))));
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    const res = await relay.handle(chatRequest(code, body));
    expect(res.status).toBe(200);
    await res.text();
    const actual = 20_000 * ultra.inNano + 1 * ultra.outNano;
    expect(actual).toBeGreaterThan(held);
    expect(await ledger.getCode(hash)).toMatchObject({ spent: actual, reserved: 0, inFlight: 0, frozen: true });
    expect((await recentEvents(ledger, 1))[0]).toMatchObject({ kind: "usage", stream: true, actual, overshoot: actual - held });
    expect((await relay.handle(chatRequest(code, body))).status).toBe(403);
    expect(upstream.calls).toHaveLength(1);
  });

  it("R5: a client that leaves while its call waits for the limiter gets 499, with nothing held", async () => {
    const raw = JSON.parse(CONFIG_TEXT);
    raw.models[SUPER].rpm = 2; // the relay budgets one call a minute
    const tight = parseConfig(raw);
    const ledger = fileLedger("adv2-r5wait");
    await ledger.setGlobalCap(usdToNano("10"));
    const { code, hash } = await mint(ledger, { total: "5" });
    const upstream = new FakeUpstream(() => jsonReply(completion(SUPER, usage(10, 2))));
    const relay = makeRelay({ ledger, fetch: upstream.fetch, config: tight, limiterWaitMs: 200_000 });
    const first = await relay.handle(chatRequest(code, { model: SUPER, messages, max_tokens: 8 }));
    expect(first.status).toBe(200);
    await first.text();
    const client = new AbortController();
    const started = Date.now();
    const waiting = relay.handle(chatRequest(code, { model: SUPER, messages, max_tokens: 8 }, { signal: client.signal }));
    await new Promise((r) => setTimeout(r, 50));
    client.abort();
    const res = await waiting;
    expect(res.status).toBe(499);
    expect(Date.now() - started).toBeLessThan(10_000);
    expect(upstream.calls).toHaveLength(1);
    expect(await ledger.getCode(hash)).toMatchObject({ reserved: 0, inFlight: 0, requests: 1 });
  });

  it("R6: a reserve that failed before it landed answers 503, and its background release changes nothing", async () => {
    const ledger = fileLedger("adv2-r6");
    await ledger.setGlobalCap(usdToNano("10"));
    const { code, hash } = await mint(ledger, { total: "5" });
    ledger.reserve = async () => {
      throw new Error("fetch failed: ECONNREFUSED");
    };
    const background: Promise<unknown>[] = [];
    const upstream = new FakeUpstream();
    const relay = makeRelay({ ledger, fetch: upstream.fetch, waitUntil: (p) => void background.push(p) });
    const res = await relay.handle(chatRequest(code, { model: ULTRA, messages }));
    expect(res.status).toBe(503);
    await res.text();
    await Promise.all(background);
    expect(upstream.calls).toHaveLength(0);
    expect(await ledger.getCode(hash)).toMatchObject({ spent: 0, reserved: 0, requests: 0, inFlight: 0 });
    expect(await recentEvents(ledger, 10)).toEqual([]);
  });
});
