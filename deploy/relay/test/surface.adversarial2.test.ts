/**
 * W2 security review, re-attack of the fixed relay (wave 3), outside surface and availability.
 *
 * R4 asked that unauthenticated traffic stop costing ledger commands. The fix holds for that,
 * and these tests lock the parts that held: the per-instance memory of bad codes never keeps a
 * good code out, and Token Factory's headers and scrubbed error body come through on the path
 * that books a slow 5xx in full. One gap stays open and is pinned with `it.fails`: a call that
 * waits for the limiter polls the ledger once for every slot that opens, so a code holder can
 * make each request cost several ledger commands instead of two.
 *
 * `it.fails` tests fail while the gap is open, which vitest reports green. When it is closed
 * the test turns red; switch it to a plain `it` then.
 */

import { afterAll, describe, expect, it, vi } from "vitest";
import { hashCode, newCode } from "../src/codes.js";
import { emptyState, reserveIn, settleIn } from "../src/ledger/state.js";
import type { Ledger, RelayEvent, ReserveRequest, SettleRequest } from "../src/ledger/types.js";
import { usdToNano } from "../src/money.js";
import {
  FakeUpstream,
  SUPER,
  ULTRA,
  chatRequest,
  cleanupDirs,
  completion,
  config,
  fileLedger,
  jsonReply,
  makeRelay,
  messages,
  mint,
  usage,
  waitFor,
} from "./helpers.js";

afterAll(() => cleanupDirs());

async function recentEvents(ledger: Ledger, count: number, nowMs = Date.now()): Promise<RelayEvent[]> {
  return (await ledger.report({ days: [], events: count, nowMs })).events;
}

async function errorCode(res: Response): Promise<string> {
  return ((await res.json()) as { error: { code?: string; type: string } }).error.code ?? "";
}

describe("R4 re-attack: the memory of bad codes (regression)", () => {
  it("never keeps out a code that was refused for its caps, a freeze, its calls in flight, the kill switch or an unset global cap", async () => {
    const ledger = fileLedger("adv2-badcodes");
    await ledger.setGlobalCap(usdToNano("10"));
    const { code, hash } = await mint(ledger, { total: "0.0001" });
    let gate: Promise<void> | null = null;
    const upstream = new FakeUpstream(async () => {
      if (gate) await gate;
      return jsonReply(completion(SUPER, usage(10, 2)));
    });
    // One relay instance throughout: whatever it remembers about this code, it remembers here.
    const relay = makeRelay({ ledger, fetch: upstream.fetch });
    const small = { model: SUPER, messages, max_tokens: 8 };
    const call = (body: Record<string, unknown> = small) => relay.handle(chatRequest(code, body));
    const refusedThenServed: [string, number, string, () => Promise<unknown>, () => Promise<unknown>][] = [
      ["the total cap", 402, "total_cap", async () => undefined, () => ledger.updateCode(hash, { capTotal: usdToNano("5"), capDaily: usdToNano("5") })],
      ["a freeze", 403, "code_frozen", () => ledger.updateCode(hash, { frozen: true }), () => ledger.updateCode(hash, { frozen: false })],
      ["the kill switch", 503, "disabled", () => ledger.setDisabled(true), () => ledger.setDisabled(false)],
      ["an unset global cap", 503, "global_cap_unset", () => ledger.setGlobalCap(0), () => ledger.setGlobalCap(usdToNano("10"))],
    ];
    for (const [what, status, reason, refuse, restore] of refusedThenServed) {
      await refuse();
      const refused = await call();
      expect(refused.status, what).toBe(status);
      expect(await errorCode(refused), what).toBe(reason);
      await restore();
      const served = await call();
      expect(served.status, `after ${what}`).toBe(200);
      await served.text();
    }
    // Calls in flight: one running call fills a limit of one; the next is refused, then served.
    await ledger.updateCode(hash, { maxInFlight: 1 });
    let open!: () => void;
    gate = new Promise<void>((resolve) => (open = resolve));
    const running = call();
    await waitFor(() => upstream.calls.length === refusedThenServed.length + 1);
    const busy = await call();
    expect(busy.status).toBe(429);
    expect(await errorCode(busy)).toBe("in_flight");
    open();
    gate = null;
    expect((await running).status).toBe(200);
    const served = await call();
    expect(served.status, "after calls in flight").toBe(200);
    await served.text();
    expect(await ledger.getCode(hash)).toMatchObject({ inFlight: 0, reserved: 0 });
  });
});

/** The ledger's own rules (state.ts, as FileLedger runs them under its lock), in memory, counting commands. */
function memoryLedger() {
  const state = emptyState();
  const commands = { reserve: 0, settle: 0 };
  const ledger = {
    kind: "file" as const,
    state,
    commands,
    async reserve(r: ReserveRequest) {
      commands.reserve += 1;
      return reserveIn(state, r);
    },
    async settle(s: SettleRequest) {
      commands.settle += 1;
      return { settled: settleIn(state, s) };
    },
    async sweep() {
      return 0;
    },
  };
  return ledger;
}

/** A clock, and a sleep for the relay, that move only when every unfinished call is asleep. */
function fakeTime(startMs: number) {
  let clock = startMs;
  let order = 0;
  const timers: { at: number; order: number; wake: () => void }[] = [];
  return {
    now: () => clock,
    set(ms: number) {
      clock = ms;
    },
    sleep: (ms: number) =>
      new Promise<void>((resolve) => {
        timers.push({ at: clock + ms, order: (order += 1), wake: resolve });
      }),
    async drive(unfinished: () => number): Promise<void> {
      for (let turns = 0; unfinished() > 0; turns += 1) {
        if (turns > 200_000) throw new Error("fake time: the calls never finished");
        if (timers.length >= unfinished()) {
          timers.sort((x, y) => x.at - y.at || x.order - y.order);
          const next = timers.shift()!;
          clock = Math.max(clock, next.at);
          next.wake();
        }
        await new Promise((r) => setTimeout(r, 0));
      }
    },
  };
}

/**
 * One code fills its share of Ultra's requests in one minute. A minute later that minute
 * fades out of the sliding window and frees a slot about every 1.6 s; `concurrent` calls from
 * the same code arrive and wait for the limiter (up to the default 10 s).
 */
async function waitingCalls(concurrent: number) {
  const random = vi.spyOn(Math, "random").mockReturnValue(0.5); // the relay's jitter, fixed
  try {
    const memory = memoryLedger();
    memory.state.global.cap = usdToNano("100");
    const code = newCode();
    const hash = await hashCode(code);
    memory.state.codes[hash] = {
      label: "judge",
      capTotal: usdToNano("50"),
      capDaily: usdToNano("50"),
      freezePct: 100,
      revoked: false,
      frozen: false,
      createdAt: 0,
      spent: 0,
      reserved: 0,
      requests: 0,
      inFlight: 0,
    };
    const ledger = memory as unknown as Ledger;
    const time = fakeTime(Date.parse("2026-10-04T11:59:00.000Z"));
    const upstream = new FakeUpstream(() => jsonReply(completion(ULTRA, usage(20, 1))));
    const body = { model: ULTRA, messages, max_tokens: 1 };
    const filler = makeRelay({ ledger, fetch: upstream.fetch, now: time.now });
    let filled = 0;
    for (;;) {
      const res = await filler.handle(chatRequest(code, body));
      await res.text();
      if (res.status !== 200) break;
      filled += 1;
    }
    time.set(Date.parse("2026-10-04T12:00:00.000Z"));
    const before = memory.commands.reserve;
    const relay = makeRelay({ ledger, fetch: upstream.fetch, now: time.now, sleep: time.sleep, limiterWaitMs: 10_000 });
    let unfinished = concurrent;
    const running = Promise.all(
      Array.from({ length: concurrent }, () =>
        relay.handle(chatRequest(code, body)).then(async (res) => {
          await res.text();
          unfinished -= 1;
          return res.status;
        }),
      ),
    );
    await time.drive(() => unfinished);
    const statuses = await running;
    return { filled, statuses, reserves: memory.commands.reserve - before };
  } finally {
    random.mockRestore();
  }
}

describe("R4 re-attack: what a call that waits for the limiter costs the ledger", () => {
  // Measured with this harness: 20 waiting calls make 125 reserve commands for 6 admitted
  // calls, about 6 each (Lightning: about 10 each; 100 calls: 685 commands). Every waiting call
  // wakes for each slot that opens, and all but one are refused again. Calls waiting for the
  // limiter hold no reservation, so the code's limit on calls in flight does not bound them.
  // With the firewall's 300 requests a minute per IP, one valid code and one IP drive about
  // 1,900 Upstash commands a minute, against two for a call that does not wait.

  it("admits some waiting calls and refuses the rest with 429 (the harness works)", async () => {
    const { filled, statuses, reserves } = await waitingCalls(20);
    // 25% of the relay's Ultra requests a minute: 67 of 270 (90% of Token Factory's 300).
    expect(filled).toBe(Math.floor((config.models.get(ULTRA)!.limitRpm * config.codeSharePct) / 100));
    expect(filled).toBe(67);
    expect(statuses.every((s) => s === 200 || s === 429)).toBe(true);
    expect(statuses.filter((s) => s === 200).length).toBeGreaterThan(0);
    expect(statuses.filter((s) => s === 429).length).toBeGreaterThan(0);
    expect(reserves).toBeGreaterThanOrEqual(20);
  });

  it.fails("costs the ledger at most three commands per waiting call", async () => {
    const { reserves } = await waitingCalls(20);
    expect(reserves / 20).toBeLessThanOrEqual(3);
  });
});

describe("headers and key removal on a 5xx booked in full (regression)", () => {
  it("passes Token Factory's headers, and its error body without the relay's key, on a slow 5xx", async () => {
    const key = "tfk_SECRET_slow5xx_0123456789abcdef";
    const ledger = fileLedger("adv2-slow5xx");
    await ledger.setGlobalCap(usdToNano("10"));
    const { code } = await mint(ledger, { total: "5" });
    let clock = Date.parse("2026-10-03T12:00:00Z");
    const upstream = new FakeUpstream(() => {
      clock += 30_000;
      return jsonReply(
        { error: { message: `upstream timed out for Bearer ${key} (key ${key})` } },
        {
          status: 504,
          headers: {
            "x-request-id": "req-504",
            "retry-after": "7",
            "x-ratelimit-remaining-requests": "3",
            "set-cookie": "tf=1",
            location: "https://evil.test/",
          },
        },
      );
    });
    const relay = makeRelay({ ledger, fetch: upstream.fetch, upstreamKey: key, now: () => clock });
    const res = await relay.handle(chatRequest(code, { model: SUPER, messages, max_tokens: 8 }));
    expect(res.status).toBe(504);
    expect(res.headers.get("x-request-id")).toBe("req-504");
    expect(res.headers.get("retry-after")).toBe("7");
    expect(res.headers.get("x-ratelimit-remaining-requests")).toBe("3");
    expect(res.headers.get("x-upstream-ms")).toBe("30000");
    expect(res.headers.get("set-cookie")).toBeNull();
    expect(res.headers.get("location")).toBeNull();
    const text = await res.text();
    expect(text).not.toContain(key);
    expect(text.split("[key removed]")).toHaveLength(3);
    const [event] = await recentEvents(ledger, 1, clock);
    expect(event).toMatchObject({ kind: "full", reconcile: true, request_id: "req-504", why: "upstream_504" });
    expect(event!.actual).toBe(event!.reserved);
  });
});
