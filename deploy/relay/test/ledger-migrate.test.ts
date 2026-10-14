/**
 * admin.mjs migrate: a ledger written by the old relay (one Redis hash per record) is
 * copied into the current layout under the same prefix, and reads the same afterwards.
 * Runs on ioredis-mock and through the real @upstash/redis client against the offline
 * stand-in.
 */

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Redis } from "@upstash/redis";
import RedisMock from "ioredis-mock";
import { afterAll, describe, expect, it } from "vitest";
import { hashCode, newCode } from "../src/codes.js";
import type { RelayEvent, ReserveRequest, SettleRequest } from "../src/ledger/types.js";
import { type RedisLike, type UpstashClient, UpstashLedger, upstashStore } from "../src/ledger/upstash.js";
import { startFakeUpstash } from "./fake-upstash.js";
import { IoredisMockAdapterV1 } from "./fixtures/v1/ioredis-adapter.js";
import { type RedisLikeV1, UpstashLedgerV1, upstashStoreV1 } from "./fixtures/v1/upstash.js";
import { IoredisMockAdapter } from "./ioredis-adapter.js";

const MODEL = "nvidia/nemotron-3-super-120b-a12b";
const DAY = "2026-10-05";
const START = Date.parse(`${DAY}T12:00:00Z`);

const rest = await startFakeUpstash();
afterAll(() => rest.close());

interface Stores {
  name: string;
  /** The same database, as the old ledger and as the new one see it. */
  make(): { old: RedisLikeV1; store: RedisLike; mock: InstanceType<typeof RedisMock> | null };
}

let n = 0;
const stores: Stores[] = [
  {
    name: "ioredis-mock",
    make: () => {
      const mock = new RedisMock();
      return { old: new IoredisMockAdapterV1(mock), store: new IoredisMockAdapter(mock), mock };
    },
  },
  {
    name: "@upstash/redis over REST",
    make: () => {
      const client = () => new Redis({ url: rest.url, token: rest.token, automaticDeserialization: false, enableTelemetry: false });
      return {
        old: upstashStoreV1(client() as unknown as Parameters<typeof upstashStoreV1>[0]),
        store: upstashStore(client() as unknown as UpstashClient),
        mock: null,
      };
    },
  },
];

let seq = 0;
function reserveRequest(hash: string, overrides: Partial<ReserveRequest> = {}): ReserveRequest {
  seq += 1;
  const nowMs = overrides.nowMs ?? START + seq * 1000;
  return {
    id: `r_mig_${seq}`,
    hash,
    model: MODEL,
    day: DAY,
    window: Math.floor(nowMs / 60_000),
    elapsedMs: nowMs % 60_000,
    amount: 500_000,
    rateTokens: 1000,
    rpm: 270,
    tpm: 180_000,
    nowMs,
    deadlineMs: nowMs + 400_000,
    defaultSharePct: 25,
    defaultMaxInFlight: 8,
    ...overrides,
  };
}

function settleRequest(r: ReserveRequest, actual: number, extra: Partial<SettleRequest> = {}): SettleRequest {
  const event: RelayEvent = {
    t: r.nowMs + 500,
    id: r.id,
    code: r.hash.slice(0, 12),
    model: r.model,
    status: 200,
    kind: "usage",
    reserved: r.amount,
    actual,
    in: 120,
    out: 30,
    rsn: 5,
    completion: `chatcmpl-${r.id}`,
  };
  return {
    id: r.id,
    hash: r.hash,
    model: r.model,
    day: r.day,
    window: r.window,
    actual,
    promptTokens: 120,
    completionTokens: 30,
    reasoningTokens: 5,
    kind: "usage",
    tokenCorrection: -800,
    event,
    ...extra,
  };
}

/** An old-layout ledger with caps, a carried-over balance, settled calls, a frozen and a revoked code, and one call still open. */
async function oldLedger(old: UpstashLedgerV1) {
  await old.setGlobalCap(26_000_000_000);
  await old.setOpeningBalance(20_300_000_000);
  const hashes: Record<string, string> = {};
  for (const [label, extra] of [
    ["judge", { capTotal: 10_000_000_000, capDaily: 3_000_000_000 }],
    ["film", { capTotal: 5_000_000_000, capDaily: 5_000_000_000, sharePct: 100, maxInFlight: 16 }],
    ["spare-1", { capTotal: 1_000_000_000, capDaily: 1_000_000_000, freezePct: 85 }],
    ["gone", { capTotal: 1_000_000_000, capDaily: 1_000_000_000 }],
  ] as const) {
    const hash = await hashCode(newCode());
    await old.addCode({ hash, label, freezePct: 100, createdAt: START - 86_400_000 + Object.keys(hashes).length, ...extra });
    hashes[label] = hash;
  }
  const settled: ReserveRequest[] = [];
  for (const label of ["judge", "judge", "film", "judge"]) {
    const r = reserveRequest(hashes[label]!);
    expect(await old.reserve(r)).toMatchObject({ ok: true });
    await old.settle(settleRequest(r, 40_000));
    settled.push(r);
  }
  // A call booked in full after a slow 5xx, one released, and one that cost more than its hold.
  const full = reserveRequest(hashes.film!);
  await old.reserve(full);
  await old.settle(settleRequest(full, full.amount, { kind: "full", reconcile: true, promptTokens: 0, completionTokens: 0, reasoningTokens: 0, tokenCorrection: 0 }));
  const released = reserveRequest(hashes.judge!, { day: "2026-10-04", nowMs: START - 86_400_000 });
  await old.reserve(released);
  await old.settle(settleRequest(released, 0, { kind: "released", promptTokens: 0, completionTokens: 0, reasoningTokens: 0, tokenCorrection: -1000 }));
  const over = reserveRequest(hashes["spare-1"]!, { amount: 1000 });
  await old.reserve(over);
  await old.settle(settleRequest(over, 5000));
  await old.updateCode(hashes.gone!, { revoked: true });
  const open = reserveRequest(hashes.judge!);
  expect(await old.reserve(open)).toMatchObject({ ok: true });
  return { hashes, open };
}

describe.each(stores)("migrate a ledger in the old layout ($name)", ({ make }) => {
  it("copies codes, caps, spend, days, usage, events and the open call, and reads the same", async () => {
    const { old: oldStore, store, mock } = make();
    const prefix = `mig${(n += 1)}`;
    const old = new UpstashLedgerV1(oldStore, { prefix });
    const { hashes, open } = await oldLedger(old);
    const nowMs = open.nowMs + 1000;
    const before = await old.report({ days: [DAY, "2026-10-04", "2026-10-03"], events: 20, nowMs });
    const beforeDays = await Promise.all(Object.values(hashes).map((hash) => old.getDay(hash, DAY)));

    const ledger = new UpstashLedger(store, { prefix });
    expect((await ledger.getGlobal()).cap).toBe(0);
    const lines: string[] = [];
    const summary = await ledger.migrateFromV1({ waitMs: 0, log: (line) => lines.push(line) });
    expect(summary).toMatchObject({ codes: 4, codeDays: 4, usageDays: 2, events: 7, openReservations: 1 });
    expect(lines.join("\n")).toMatch(/switched the old ledger off/);
    expect(lines.join("\n")).toMatch(/1 reservation\(s\) still open/);

    const after = await ledger.report({ days: [DAY, "2026-10-04", "2026-10-03"], events: 20, nowMs });
    expect(after.global).toEqual({ ...before.global, disabled: true });
    expect(after.codes).toEqual(before.codes);
    expect(after.days).toEqual(before.days);
    expect(after.events).toEqual(before.events);
    expect(after.openReservations).toBe(1);
    expect(after.expiredReservations).toBe(before.expiredReservations);
    expect(await Promise.all(Object.values(hashes).map((hash) => ledger.getDay(hash, DAY)))).toEqual(beforeDays);
    expect(after.codes.find((c) => c.label === "gone")?.revoked).toBe(true);
    expect(after.codes.find((c) => c.label === "spare-1")).toMatchObject({ frozen: true, freezePct: 85 });
    expect(after.codes.find((c) => c.label === "film")).toMatchObject({ sharePct: 100, maxInFlight: 16 });

    // The old ledger is off: a relay still on the old code refuses every call.
    expect(await old.reserve(reserveRequest(hashes.judge!))).toEqual({ ok: false, reason: "disabled" });
    await expect(old.getGlobal()).resolves.toMatchObject({ disabled: true });
    if (mock) {
      // Day records are kept in the new layout (its scripts' MSET would drop an expiry anyway).
      expect(await mock.pttl(`${prefix}:v2:cd:${hashes.judge}:${DAY}`)).toBe(-1);
      expect(await mock.pttl(`${prefix}:v2:u:${DAY}`)).toBe(-1);
    }

    // Switched on, the new ledger settles the carried call and holds the caps.
    expect(await ledger.reserve(reserveRequest(hashes.judge!))).toEqual({ ok: false, reason: "disabled" });
    await ledger.setDisabled(false);
    expect(await ledger.settle(settleRequest(open, 30_000))).toEqual({ settled: true });
    const judge = (await ledger.getCode(hashes.judge!))!;
    const judgeBefore = before.codes.find((c) => c.label === "judge")!;
    expect(judge).toMatchObject({ reserved: judgeBefore.reserved - open.amount, spent: judgeBefore.spent + 30_000, inFlight: 0 });
    expect(await ledger.getGlobal()).toMatchObject({ reserved: 0, spent: before.global.spent + 30_000, opening: 20_300_000_000 });
    const big = reserveRequest(hashes.judge!, { amount: 3_000_000_000 });
    expect(await ledger.reserve(big)).toMatchObject({ ok: false, reason: "cap_daily" });
    expect(await ledger.reserve(reserveRequest(hashes.gone!))).toEqual({ ok: false, reason: "revoked" });
    expect(await ledger.reserve(reserveRequest(hashes["spare-1"]!))).toEqual({ ok: false, reason: "frozen" });
    expect(await ledger.reserve(reserveRequest(hashes.film!))).toMatchObject({ ok: true });
    await expect(ledger.addCode({ hash: await hashCode(newCode()), label: "judge", capTotal: 1, capDaily: 1, freezePct: 100, createdAt: 1 })).rejects.toThrow(/taken/);

    // Once is enough, and once the new ledger has run, never again.
    await expect(ledger.migrateFromV1({ waitMs: 0 })).rejects.toThrow(/already in the new layout/);
    await expect(ledger.migrateFromV1({ waitMs: 0, force: true })).rejects.toThrow(/switched on; copying over it could lose/);
    await ledger.setDisabled(true);
    await expect(ledger.migrateFromV1({ waitMs: 0, force: true })).rejects.toThrow(/calls of its own since the copy/);
  });

  it("refuses --force once the new ledger holds a call of its own, even switched off again", async () => {
    const { old: oldStore, store } = make();
    const prefix = `mig${(n += 1)}`;
    const old = new UpstashLedgerV1(oldStore, { prefix });
    const { hashes, open } = await oldLedger(old);
    await old.settle(settleRequest(open, 25_000));
    const ledger = new UpstashLedger(store, { prefix });
    await ledger.migrateFromV1({ waitMs: 0 });
    await ledger.setDisabled(false);
    // A call starts on the new relay; then the operator switches it off and tries to copy again.
    const call = reserveRequest(hashes.judge!, { nowMs: Date.parse("2026-10-07T09:00:00Z"), day: "2026-10-07" });
    expect(await ledger.reserve(call)).toMatchObject({ ok: true });
    await ledger.setDisabled(true);
    await expect(ledger.migrateFromV1({ waitMs: 0, force: true })).rejects.toThrow(/calls of its own since the copy/);
    // The call's hold is still there, and its settlement books it.
    expect(await ledger.settle(settleRequest(call, 300_000))).toEqual({ settled: true });
    expect((await ledger.getCode(hashes.judge!))!.spent).toBe((await old.getCode(hashes.judge!))!.spent + 300_000);
  });

  it("refuses --force over a new-layout ledger that migrate did not write", async () => {
    const { old: oldStore, store } = make();
    const prefix = `mig${(n += 1)}`;
    await oldLedger(new UpstashLedgerV1(oldStore, { prefix }));
    const ledger = new UpstashLedger(store, { prefix });
    await ledger.setGlobalCap(1_000_000_000);
    await ledger.setDisabled(true);
    await expect(ledger.migrateFromV1({ waitMs: 0, force: true })).rejects.toThrow(/not written by migrate/);
  });

  it("copies again with --force while the new ledger is still off, taking in a settlement that came late", async () => {
    const { old: oldStore, store } = make();
    const prefix = `mig${(n += 1)}`;
    const old = new UpstashLedgerV1(oldStore, { prefix });
    const { hashes, open } = await oldLedger(old);
    const ledger = new UpstashLedger(store, { prefix });
    expect((await ledger.migrateFromV1({ waitMs: 0 })).openReservations).toBe(1);
    await old.settle(settleRequest(open, 25_000)); // the earlier relay settles it after the copy
    const again = await ledger.migrateFromV1({ waitMs: 0, force: true });
    expect(again.openReservations).toBe(0);
    expect(await ledger.getCode(hashes.judge!)).toEqual(await old.getCode(hashes.judge!));
    expect(await ledger.getGlobal()).toEqual({ ...(await old.getGlobal()), disabled: true });
  });

  it("waits for the old ledger's calls in flight, and the sweep books a carried call that never settles", async () => {
    const { old: oldStore, store } = make();
    const prefix = `mig${(n += 1)}`;
    const old = new UpstashLedgerV1(oldStore, { prefix });
    const { hashes, open } = await oldLedger(old);
    const ledger = new UpstashLedger(store, { prefix });
    let polls = 0;
    const summary = await ledger.migrateFromV1({
      waitMs: 10_000,
      pollMs: 1000,
      sleep: async () => {
        polls += 1;
        // The call finishes on the old relay while the migration waits.
        await old.settle(settleRequest(open, 25_000));
      },
    });
    expect(polls).toBe(1);
    expect(summary.openReservations).toBe(0);
    expect((await ledger.getCode(hashes.judge!))!.reserved).toBe(0);

    // A second ledger whose call never settles: carried over, then swept in full after its deadline.
    const prefix2 = `mig${(n += 1)}`;
    const old2 = new UpstashLedgerV1(make().old, { prefix: prefix2 });
    const second = await oldLedger(old2);
    const ledger2 = new UpstashLedger(make().store, { prefix: prefix2 });
    await ledger2.migrateFromV1({ waitMs: 0 });
    await ledger2.setDisabled(false);
    const held = (await ledger2.getCode(second.hashes.judge!))!;
    expect(await ledger2.sweep(second.open.deadlineMs - 1)).toBe(0);
    expect(await ledger2.sweep(second.open.deadlineMs)).toBe(1);
    expect(await ledger2.getCode(second.hashes.judge!)).toMatchObject({ reserved: held.reserved - second.open.amount, spent: held.spent + second.open.amount });
    const report = await ledger2.report({ days: [], events: 1, nowMs: second.open.deadlineMs });
    expect(report.events[0]).toMatchObject({ id: second.open.id, kind: "full", why: "expired" });
  });

  it("copies again when a late settlement moves the old figures during the copy", async () => {
    const { old: oldStore, store } = make();
    const prefix = `mig${(n += 1)}`;
    const old = new UpstashLedgerV1(oldStore, { prefix });
    const { hashes, open } = await oldLedger(old);
    let late = true;
    const racing: RedisLike = {
      ...bind(store),
      scan: async (cursor, match, count) => {
        if (late) {
          late = false;
          await old.settle(settleRequest(open, 25_000));
        }
        return store.scan(cursor, match, count);
      },
    };
    const lines: string[] = [];
    const summary = await new UpstashLedger(racing, { prefix }).migrateFromV1({ waitMs: 0, log: (line) => lines.push(line) });
    expect(lines.join("\n")).toMatch(/copying again/);
    expect(summary.openReservations).toBe(0);
    const ledger = new UpstashLedger(store, { prefix });
    expect(await ledger.getGlobal()).toEqual({ ...(await old.getGlobal()), disabled: true });
    expect(await ledger.getCode(hashes.judge!)).toEqual(await old.getCode(hashes.judge!));
  });

  it("refuses a prefix with no ledger in the old layout", async () => {
    const { store } = make();
    await expect(new UpstashLedger(store, { prefix: `mig${(n += 1)}` }).migrateFromV1({ waitMs: 0 })).rejects.toThrow(/no ledger in the old layout/);
  });
});

/** Every method of a store, bound, so a test can override one. */
function bind(store: RedisLike): RedisLike {
  const out: Record<string, unknown> = {};
  for (const name of [
    "evalsha",
    "eval",
    "get",
    "mget",
    "set",
    "smembers",
    "sadd",
    "hgetall",
    "hset",
    "exists",
    "lrange",
    "zrangeWithScores",
    "scan",
  ] as const) {
    out[name] = (store[name] as (...args: unknown[]) => unknown).bind(store);
  }
  return out as unknown as RedisLike;
}

describe("node admin.mjs migrate", () => {
  const root = fileURLToPath(new URL("..", import.meta.url));
  function admin(args: string[], env: Record<string, string>): Promise<{ code: number | null; stdout: string; stderr: string }> {
    return new Promise((resolve) => {
      const proc = spawn(process.execPath, [path.join(root, "admin.mjs"), ...args], {
        cwd: root,
        env: { ...process.env, UPSTASH_REDIS_REST_URL: rest.url, UPSTASH_REDIS_REST_TOKEN: rest.token, ...env },
      });
      let stdout = "";
      let stderr = "";
      proc.stdout.on("data", (d) => (stdout += d));
      proc.stderr.on("data", (d) => (stderr += d));
      proc.on("exit", (exit) => resolve({ code: exit, stdout, stderr }));
    });
  }

  it("moves a live-shaped ledger, reports it, and switches it on", async () => {
    expect(existsSync(path.join(root, "dist", "src", "index.js")), "run `npm run compile` first (npm test does)").toBe(true);
    const prefix = `migcli${(n += 1)}`;
    const old = new UpstashLedgerV1(stores[1]!.make().old, { prefix });
    const { open } = await oldLedger(old);
    await old.settle(settleRequest(open, 25_000));
    const env = { RELAY_LEDGER: "upstash", RELAY_KEY_PREFIX: prefix };
    const moved = await admin(["migrate", "--wait", "0"], env);
    expect(moved.code, moved.stderr).toBe(0);
    expect(moved.stderr).toMatch(/switched the old ledger off/);
    expect(moved.stderr).toMatch(/copied 4 code\(s\), 4 daily figure\(s\), 2 day\(s\) of usage, 8 event\(s\) and 0 open reservation\(s\)/);
    expect(moved.stderr).toMatch(/global cap \$26\.00, spent \$20\.3/);
    const report = await admin(["report", "--events", "3", "--json"], env);
    expect(report.code, report.stderr).toBe(0);
    const parsed = JSON.parse(report.stdout) as { global: { disabled: boolean; spent: number }; codes: { label: string }[]; events: { id: string }[] };
    expect(parsed.global).toMatchObject({ disabled: true, spent: (await old.getGlobal()).spent });
    expect(parsed.codes.map((c) => c.label)).toEqual(["judge", "film", "spare-1", "gone"]);
    const newest = (await old.report({ days: [], events: 3, nowMs: 0 })).events.map((e) => e.id);
    expect(newest[0]).toBe(open.id);
    expect(parsed.events.map((e) => e.id)).toEqual(newest);
    expect((await admin(["enable"], env)).code).toBe(0);
    expect((await new UpstashLedger(stores[1]!.make().store, { prefix }).getGlobal()).disabled).toBe(false);
    const again = await admin(["migrate", "--wait", "0"], env);
    expect(again.code).toBe(1);
    expect(again.stderr).toMatch(/already in the new layout/);
    const file = await admin(["migrate"], { RELAY_LEDGER: "file", RELAY_LEDGER_FILE: path.join(root, ".data", "never-written.json") });
    expect(file.code).toBe(2);
    expect(file.stderr).toMatch(/migrate works on the Upstash ledger/);
  });
});
