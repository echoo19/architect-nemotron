/**
 * What the relay costs its Upstash database, measured through the whole handler and the
 * real @upstash/redis client against the counting stand-in (test/fake-upstash.ts), and
 * pinned. `billed` is what Upstash counts against the free tier's 500,000 a month: each
 * command sent plus each command a script runs inside. `requests` is HTTP round trips.
 *
 * Before the records became one string each, a call cost 52 billed commands in 2 requests
 * (the reserve script ran 22 commands, the settle script 28), /v1/models 2, /v1/relay/balance
 * 3 and a refusal 3 to 9 (9 when the code's share of the limiter refused it).
 */

import { Redis } from "@upstash/redis";
import { afterAll, describe, expect, it } from "vitest";
import { hashCode, newCode } from "../src/codes.js";
import { parseConfig } from "../src/config.js";
import type { Relay, RelayOptions } from "../src/handler.js";
import { type UpstashClient, UpstashLedger, upstashStore } from "../src/ledger/upstash.js";
import { usdToNano } from "../src/money.js";
import { type FakeUpstash, startFakeUpstash } from "./fake-upstash.js";
import {
  CONFIG_TEXT,
  FakeUpstream,
  LIGHTNING,
  SUPER,
  ULTRA,
  chatRequest,
  chunks,
  completion,
  getRequest,
  jsonReply,
  makeRelay,
  messages,
  mint,
  sseReply,
  usage,
} from "./helpers.js";

const fake: FakeUpstash = await startFakeUpstash();
afterAll(() => fake.close());

let n = 0;
function ledgerOn(prefix = `cost${(n += 1)}`, eventsKept?: number): UpstashLedger {
  const redis = new Redis({ url: fake.url, token: fake.token, automaticDeserialization: false, enableTelemetry: false });
  return new UpstashLedger(upstashStore(redis as unknown as UpstashClient), { prefix, ...(eventsKept ? { eventsKept } : {}) });
}

async function setup(options: Partial<RelayOptions> = {}) {
  const ledger = ledgerOn();
  await ledger.setGlobalCap(usdToNano("10"));
  const { code, hash } = await mint(ledger, { total: "5", label: "judge" });
  const upstream = new FakeUpstream(() => jsonReply(completion(SUPER, usage(10, 5))));
  const background: Promise<unknown>[] = [];
  // One UTC day throughout, whenever the test runs.
  let clock = Date.parse("2026-12-02T12:00:00.000Z");
  const now = () => (clock += 10);
  const relay = makeRelay({ ledger, fetch: upstream.fetch, waitUntil: (p) => void background.push(p), now, ...options });
  // The scripts are loaded and today's records exist, as on a relay that has served a call today.
  await drain(relay, chatRequest(code, body));
  await Promise.all(background.splice(0));
  return { ledger, upstream, relay, code, hash, background, now };
}

interface Cost {
  requests: number;
  billed: number;
  /** Commands sent, each with the number it ran inside: "evalsha[2] evalsha[2]". */
  sent: string;
}

/** Runs `act` (and any background work it starts) and returns what it cost. */
async function cost(background: Promise<unknown>[], act: () => Promise<unknown>): Promise<Cost> {
  fake.reset();
  await act();
  await Promise.all(background.splice(0));
  return {
    requests: fake.requests,
    billed: fake.billed,
    sent: fake.sent.map((c) => (c.inner.length ? `${c.name}[${c.inner.length}]` : c.name)).join(" "),
  };
}

async function drain(relay: Relay, request: Request): Promise<number> {
  const res = await relay.handle(request);
  await res.text();
  return res.status;
}

const body = { model: SUPER, messages, max_tokens: 64 };

describe("what a model call costs the Upstash ledger", () => {
  it("costs two requests and six billed commands, whole or streamed: one MGET and one MSET in each script", async () => {
    const s = await setup();
    expect(await cost(s.background, () => drain(s.relay, chatRequest(s.code, body)))).toEqual({ requests: 2, billed: 6, sent: "evalsha[2] evalsha[2]" });
    expect(fake.sent.map((c) => c.inner)).toEqual([
      ["mget", "mset"],
      ["mget", "mset"],
    ]);
    s.upstream.handler = () => sseReply(chunks(SUPER, usage(10, 5)));
    expect(await cost(s.background, () => drain(s.relay, chatRequest(s.code, { ...body, stream: true })))).toEqual({
      requests: 2,
      billed: 6,
      sent: "evalsha[2] evalsha[2]",
    });
  });

  it("costs the same when Token Factory refuses the call and the hold is released", async () => {
    const s = await setup();
    s.upstream.handler = () => jsonReply({ error: { message: "bad request" } }, { status: 400 });
    expect(await cost(s.background, () => drain(s.relay, chatRequest(s.code, body)))).toMatchObject({ requests: 2, billed: 6 });
  });

  it("costs the same six on the first call of a UTC day: day records are kept, with no EXPIRE", async () => {
    const s = await setup();
    let clock = Date.parse("2026-10-07T23:59:59.000Z");
    const relay = makeRelay({ ledger: s.ledger, fetch: s.upstream.fetch, now: () => clock });
    expect(await cost(s.background, () => drain(relay, chatRequest(s.code, body)))).toEqual({ requests: 2, billed: 6, sent: "evalsha[2] evalsha[2]" });
    clock += 2000; // the next UTC day: new daily records for the code and for usage
    expect(await cost(s.background, () => drain(relay, chatRequest(s.code, body)))).toEqual({ requests: 2, billed: 6, sent: "evalsha[2] evalsha[2]" });
    expect(fake.sent.map((c) => c.inner)).toEqual([
      ["mget", "mset"],
      ["mget", "mset"],
    ]);
  });

  it("sends each script once per instance: EVALSHA, and EVAL only after Upstash answers NOSCRIPT", async () => {
    const s = await setup();
    fake.flushScripts(); // Upstash lost its script cache
    expect(await cost(s.background, () => drain(s.relay, chatRequest(s.code, body)))).toEqual({
      requests: 4,
      billed: 8,
      sent: "evalsha eval[2] evalsha eval[2]",
    });
    expect((await cost(s.background, () => drain(s.relay, chatRequest(s.code, body)))).sent).toBe("evalsha[2] evalsha[2]");
    // A new instance finds the scripts already loaded and never sends them.
    const other = makeRelay({ ledger: ledgerOn(s.ledger.prefix), fetch: s.upstream.fetch, now: s.now });
    expect((await cost(s.background, () => drain(other, chatRequest(s.code, body)))).sent).toBe("evalsha[2] evalsha[2]");
  });
});

describe("what the other routes cost", () => {
  it("answers /v1/models and /v1/relay/balance with one MGET, and /healthz with at most one GET every 30 s", async () => {
    let clock = Date.now();
    const s = await setup({ now: () => clock });
    expect(await cost(s.background, () => drain(s.relay, getRequest("/v1/models", s.code)))).toEqual({ requests: 1, billed: 1, sent: "mget" });
    expect(await cost(s.background, () => drain(s.relay, getRequest("/v1/relay/balance", s.code)))).toEqual({ requests: 1, billed: 1, sent: "mget" });
    expect(await cost(s.background, () => drain(s.relay, getRequest("/healthz", null)))).toEqual({ requests: 1, billed: 1, sent: "get" });
    const probes = await cost(s.background, async () => {
      for (let i = 0; i < 20; i += 1) await drain(s.relay, getRequest("/healthz", null));
    });
    expect(probes.billed).toBe(0);
    clock += 30_000;
    expect((await cost(s.background, () => drain(s.relay, getRequest("/healthz", null)))).billed).toBe(1);
  });

  it("refuses with two billed commands (one script that reads and writes nothing), or none before the ledger", async () => {
    const s = await setup();
    const refused = async (request: Request, status: number) => {
      let answered = 0;
      const spent = await cost(s.background, async () => (answered = await drain(s.relay, request)));
      expect(answered, request.url).toBe(status);
      return spent;
    };
    const stranger = `rc_${"Z".repeat(32)}`;
    const two = { requests: 1, billed: 2, sent: "evalsha[1]" };
    const none = { requests: 0, billed: 0, sent: "" };
    expect(await refused(chatRequest(stranger, body), 401)).toEqual(two);
    expect(await refused(chatRequest(stranger, body), 401)).toEqual(none); // remembered by this instance
    expect(await refused(chatRequest(null, body), 401)).toEqual(none);
    expect(await refused(chatRequest(s.code, { ...body, model: "meta/llama" }), 403)).toEqual(none);
    const poor = await mint(s.ledger, { total: "0.000001", label: "poor" });
    expect(await refused(chatRequest(poor.code, body), 402)).toEqual(two);
    await s.ledger.updateCode(s.hash, { frozen: true });
    expect(await refused(chatRequest(s.code, body), 403)).toEqual(two);
    await s.ledger.updateCode(s.hash, { frozen: false });
    await s.ledger.setDisabled(true);
    expect(await refused(chatRequest(s.code, body), 503)).toEqual(two);
    await s.ledger.setDisabled(false);
    await s.ledger.updateCode(s.hash, { revoked: true });
    expect(await refused(chatRequest(s.code, body), 401)).toEqual(two);
    expect(await refused(getRequest("/v1/models", s.code), 401)).toEqual(none);
  });

  it("refuses at the code's calls in flight and at the limiter with two billed commands", async () => {
    const raw = JSON.parse(CONFIG_TEXT);
    raw.models[LIGHTNING].rpm = 2; // the relay budgets one Lightning call a minute
    const s = await setup({ config: parseConfig(raw) });
    await drain(s.relay, chatRequest(s.code, { ...body, model: LIGHTNING }));
    expect(await cost(s.background, () => drain(s.relay, chatRequest(s.code, { ...body, model: LIGHTNING })))).toEqual({
      requests: 1,
      billed: 2,
      sent: "evalsha[1]",
    });
    await s.ledger.updateCode(s.hash, { maxInFlight: 1 });
    let open!: () => void;
    const gate = new Promise<void>((resolve) => (open = resolve));
    s.upstream.handler = async () => {
      await gate;
      return jsonReply(completion(SUPER, usage(10, 5)));
    };
    const running = drain(s.relay, chatRequest(s.code, body));
    while (s.upstream.calls.length < 3) await new Promise((r) => setTimeout(r, 5));
    const held = s.background.splice(0); // settles once the gate opens
    expect(await cost(s.background, () => drain(s.relay, chatRequest(s.code, body)))).toEqual({ requests: 1, billed: 2, sent: "evalsha[1]" });
    open();
    await running;
    await Promise.all(held);
  });

  it("sweeps a hold left by a dead instance for six more: one script claims it, one books it", async () => {
    const s = await setup();
    await s.ledger.sweep(0); // the claim script is loaded
    const at = s.now() - 1000;
    await s.ledger.reserve({
      id: "r_abandoned",
      hash: s.hash,
      model: SUPER,
      day: new Date(at).toISOString().slice(0, 10),
      window: Math.floor(at / 60_000),
      elapsedMs: at % 60_000,
      amount: 1000,
      rateTokens: 10,
      rpm: 270,
      tpm: 180_000,
      nowMs: at,
      deadlineMs: at,
      defaultSharePct: 25,
      defaultMaxInFlight: 8,
    });
    expect(await cost(s.background, () => drain(s.relay, chatRequest(s.code, body)))).toEqual({
      requests: 4,
      billed: 12,
      sent: "evalsha[2] evalsha[2] evalsha[2] evalsha[2]",
    });
    expect(await s.ledger.getCode(s.hash)).toMatchObject({ reserved: 0, inFlight: 0 });
  });
});

describe("what a judge build costs", () => {
  /**
   * A build as Architect runs one: three rungs of 500 calls, one call at a time, each reply
   * after 700 ms and the next call 20 ms later. That is about 83 calls a minute, the pace of
   * the exam build of 2026-10-06 (1,500 calls in 18 minutes), with the judge code's caps and
   * the given share, through the relay's own 10 s limiter wait and Architect's rule for a 429
   * (at most two retries, each after Retry-After up to 20 s). Counted the same way as over
   * HTTP, but sent straight to the stand-in, since 1,500 calls over HTTP take too long.
   */
  async function build(sharePct: number | undefined) {
    const perRung = 500;
    const ledger = new UpstashLedger(fake.direct, { prefix: `build${(n += 1)}` });
    await ledger.setGlobalCap(usdToNano("50"));
    const code = newCode();
    const hash = await hashCode(code);
    await ledger.addCode({
      hash,
      label: "judge",
      capTotal: usdToNano("10"),
      capDaily: usdToNano("3"),
      freezePct: 100,
      createdAt: 0,
      ...(sharePct === undefined ? {} : { sharePct }),
    });
    let clock = Date.parse("2026-12-01T10:00:00.000Z");
    const upstream = new FakeUpstream((call) => {
      clock += 700;
      return jsonReply(completion(String(call.body.model), usage(300, 60)));
    });
    const sleep = async (ms: number) => {
      clock += ms;
    };
    const relay = makeRelay({ ledger, fetch: upstream.fetch, now: () => clock, sleep, limiterWaitMs: 10_000 });
    const prompt = [{ role: "user", content: "x".repeat(1200) }];
    const send = async (model: string) => {
      const res = await relay.handle(chatRequest(code, { model, messages: prompt, max_tokens: 512 }));
      await res.text();
      return res;
    };
    await send(LIGHTNING); // the scripts are loaded
    fake.reset();
    const started = clock;
    let retried = 0;
    let failed = 0;
    for (const model of [LIGHTNING, SUPER, ULTRA]) {
      for (let i = 0; i < perRung; i += 1) {
        clock += 20;
        let res = await send(model);
        for (let retry = 0; retry < 2 && res.status === 429; retry += 1) {
          retried += 1;
          clock += Math.min(Number(res.headers.get("retry-after") ?? "1"), 20) * 1000;
          res = await send(model);
        }
        if (res.status !== 200) failed += 1;
      }
    }
    const calls = 3 * perRung;
    const billed = fake.billed;
    expect(await ledger.getCode(hash)).toMatchObject({ requests: calls + 1, reserved: 0, inFlight: 0 });
    return { calls, billed, minutes: (clock - started) / 60_000, retried, failed };
  }

  it("bills six commands a call at the judge codes' 50% share: 9,000 for a build, in 18 minutes", async () => {
    const b = await build(50);
    expect(b).toMatchObject({ calls: 1500, billed: 9000, retried: 0, failed: 0 });
    expect(b.minutes).toBeCloseTo(18, 0);
  });

  it("bills about 7.1 a call at the default 25% share, whose 67 Super or Ultra calls a minute the build outruns", async () => {
    const b = await build(undefined);
    expect(b.failed).toBe(0);
    // About 840 reserves are refused and tried again after the limiter's wait, 2 commands each:
    // 10,686 for the build, and 3 minutes more.
    expect(b.billed / b.calls).toBeCloseTo(7.12, 1);
    expect(b.minutes).toBeCloseTo(21, 0);
  });

  it("bills six a call with eight calls at a time over HTTP (requests may share a round trip)", async () => {
    // Interleaved models and a share of 100% keep the limiter out of this one: it is about
    // the client's pipelining, which must not change what is billed.
    const ledger = ledgerOn();
    await ledger.setGlobalCap(usdToNano("50"));
    const code = newCode();
    await ledger.addCode({ hash: await hashCode(code), label: "judge", capTotal: usdToNano("10"), capDaily: usdToNano("3"), freezePct: 100, createdAt: 0, sharePct: 100 });
    let clock = Date.parse("2026-12-01T10:00:00.000Z");
    const upstream = new FakeUpstream((call) => jsonReply(completion(String(call.body.model), usage(300, 40))));
    const relay = makeRelay({ ledger, fetch: upstream.fetch, now: () => clock });
    const models = [LIGHTNING, SUPER, ULTRA];
    await drain(relay, chatRequest(code, body)); // the scripts are loaded
    fake.reset();
    let sent = 0;
    const worker = async () => {
      while (sent < 400) {
        const i = (sent += 1);
        clock += 700;
        expect(await drain(relay, chatRequest(code, { ...body, model: models[i % models.length] }))).toBe(200);
      }
    };
    await Promise.all(Array.from({ length: 8 }, worker));
    expect(fake.billed).toBe(6 * 400);
    expect(fake.requests).toBeLessThanOrEqual(2 * 400);
  });
});

describe("the events the Upstash ledger keeps", () => {
  it("keeps the newest settlements, at most eventsKept of them, in the settle script's own write", async () => {
    const ledger = ledgerOn(undefined, 3);
    await ledger.setGlobalCap(usdToNano("10"));
    const { code } = await mint(ledger, { total: "5" });
    const upstream = new FakeUpstream(() => jsonReply(completion(SUPER, usage(10, 5))));
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    const ids: string[] = [];
    for (let i = 0; i < 5; i += 1) {
      const res = await relay.handle(chatRequest(code, body));
      await res.text();
      ids.push(res.headers.get("x-relay-request")!);
    }
    const report = await ledger.report({ days: [], events: 10, nowMs: Date.now() });
    expect(report.events.map((e) => e.id)).toEqual(ids.slice(2).reverse());
    expect((await ledger.report({ days: [], events: 2, nowMs: Date.now() })).events.map((e) => e.id)).toEqual(ids.slice(3).reverse());
  });
});
