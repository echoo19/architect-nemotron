/**
 * Reservation races through the whole handler: many calls at once against a slow fake
 * Token Factory. The cap must admit exactly as many calls as their worst cases fit, and
 * spent + held must never pass it, whichever store holds the ledger.
 */

import { Redis } from "@upstash/redis";
import RedisMock from "ioredis-mock";
import { afterAll, describe, expect, it } from "vitest";
import type { Ledger } from "../src/ledger/types.js";
import { type UpstashClient, UpstashLedger, upstashStore } from "../src/ledger/upstash.js";
import { startFakeUpstash } from "./fake-upstash.js";
import { formatUsd, usdToNano } from "../src/money.js";
import { prepareRequest, reservationNano } from "../src/request.js";
import { IoredisMockAdapter } from "./ioredis-adapter.js";
import { liveUpstash } from "./live-upstash.js";
import { FakeUpstream, LIGHTNING, ULTRA, chatRequest, cleanupDirs, completion, config, fileLedger, jsonReply, makeRelay, messages, mint, usage, waitFor } from "./helpers.js";

const rest = await startFakeUpstash();
// With the Upstash variables set, the same races also run against the real database.
const live = liveUpstash();
afterAll(async () => {
  cleanupDirs();
  await rest.close();
  await live?.cleanup();
});

function worstCase(body: Record<string, unknown>): number {
  const prepared = prepareRequest(body, config);
  if (!prepared.ok) throw new Error("bad body");
  const p = prepared.value;
  return reservationNano(p.model, p.inputTokensUpper, p.maxTokens);
}

let mockN = 0;
const stores: [string, () => Ledger][] = [
  ["FileLedger", () => fileLedger("race")],
  ["UpstashLedger (Lua)", () => new UpstashLedger(new IoredisMockAdapter(new RedisMock()), { prefix: `race${(mockN += 1)}` })],
  [
    "UpstashLedger (@upstash/redis over REST)",
    () =>
      new UpstashLedger(
        upstashStore(new Redis({ url: rest.url, token: rest.token, automaticDeserialization: false, enableTelemetry: false }) as unknown as UpstashClient),
        { prefix: `restrace${(mockN += 1)}` },
      ),
  ],
  ...(live ? [["UpstashLedger (live Upstash)", () => live.ledger().ledger] as [string, () => Ledger]] : []),
];

describe.each(stores)("concurrent calls on %s", (_name, makeLedger) => {
  it("admits exactly the calls a code's cap can hold, and never holds more", async () => {
    const ledger = makeLedger();
    await ledger.setGlobalCap(usdToNano("10"));
    const body = { model: LIGHTNING, messages, max_tokens: 1000 };
    const amount = worstCase(body);
    const fits = 7;
    const cap = fits * amount + Math.floor(amount / 2);
    const { code, hash } = await mint(ledger, { total: formatUsd(cap, { minDecimals: 0 }) });

    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    const upstream = new FakeUpstream(async () => {
      const state = await ledger.getCode(hash);
      expect(state!.spent + state!.reserved).toBeLessThanOrEqual(cap);
      await gate;
      return jsonReply(completion(LIGHTNING, usage(100, 50)));
    });
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    let refused = 0;
    const calls = Array.from({ length: 30 }, () =>
      relay.handle(chatRequest(code, body)).then(async (res) => {
        if (res.status === 402) refused += 1;
        await res.text();
        return res.status;
      }),
    );
    await waitFor(() => upstream.calls.length + refused === 30, 20_000);
    expect(upstream.calls).toHaveLength(fits);
    expect(await ledger.getCode(hash)).toMatchObject({ reserved: fits * amount, spent: 0 });
    release();
    const statuses = await Promise.all(calls);
    expect(statuses.filter((s) => s === 200)).toHaveLength(fits);
    expect(statuses.filter((s) => s === 402)).toHaveLength(30 - fits);
    const actual = 100 * 60 + 50 * 240;
    expect(await ledger.getCode(hash)).toMatchObject({ reserved: 0, spent: fits * actual, requests: fits });
  });

  it("holds the global cap across codes", async () => {
    const ledger = makeLedger();
    const body = { model: ULTRA, messages, max_tokens: 2000 };
    const amount = worstCase(body);
    await ledger.setGlobalCap(5 * amount);
    const a = await mint(ledger, { total: "5" });
    const b = await mint(ledger, { total: "5" });
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    const upstream = new FakeUpstream(async () => {
      await gate;
      return jsonReply(completion(ULTRA, usage(10, 10)));
    });
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    let refused = 0;
    const calls = Array.from({ length: 20 }, (_, i) =>
      relay.handle(chatRequest(i % 2 ? a.code : b.code, body)).then(async (res) => {
        if (res.status === 402) refused += 1;
        await res.text();
        return res.status;
      }),
    );
    await waitFor(() => upstream.calls.length + refused === 20, 20_000);
    expect(upstream.calls).toHaveLength(5);
    const g = await ledger.getGlobal();
    expect(g.reserved).toBe(5 * amount);
    release();
    const statuses = await Promise.all(calls);
    expect(statuses.filter((s) => s === 200)).toHaveLength(5);
    expect((await ledger.getGlobal()).spent).toBe(5 * (10 * 1000 + 10 * 3000));
  });
});
