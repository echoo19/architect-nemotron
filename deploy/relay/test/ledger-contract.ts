/**
 * The ledger contract: every store (FileLedger, UpstashLedger over the Lua scripts) must
 * pass these cases identically. Imported by the ledger-*.test.ts files.
 */

import { afterEach, describe, expect, it } from "vitest";
import { hashCode, newCode } from "../src/codes.js";
import type { Ledger, RelayEvent, ReserveRequest, SettleKind, SettleRequest } from "../src/ledger/types.js";

export type LedgerFactory = () => Promise<{ ledger: Ledger; cleanup?: () => Promise<void> }>;

const MODEL = "nvidia/Nemotron-3-Ultra-550b-a55b";
const DAY = "2026-10-03";
const WINDOW = Math.floor(Date.parse(`${DAY}T12:00:00Z`) / 60_000);
let seq = 0;

function reserveRequest(hash: string, overrides: Partial<ReserveRequest> = {}): ReserveRequest {
  seq += 1;
  const window = overrides.window ?? WINDOW;
  const elapsedMs = overrides.elapsedMs ?? 0;
  const nowMs = window * 60_000 + elapsedMs;
  return {
    id: `r_test_${seq}_${Math.random().toString(36).slice(2)}`,
    hash,
    model: MODEL,
    day: DAY,
    window,
    elapsedMs,
    amount: 100,
    rateTokens: 10,
    rpm: 1000,
    tpm: 1_000_000,
    nowMs,
    deadlineMs: nowMs + 600_000,
    // The relay's per-code limits are off unless a case asks for them.
    defaultSharePct: 100,
    defaultMaxInFlight: 1000,
    ...overrides,
  };
}

function settleRequest(r: ReserveRequest, actual: number, kind: SettleKind = "usage", extra: Partial<SettleRequest> = {}): SettleRequest {
  const event: RelayEvent = {
    t: r.nowMs + 1000,
    id: r.id,
    code: r.hash.slice(0, 12),
    model: r.model,
    status: 200,
    kind,
    reserved: r.amount,
    actual,
    in: 7,
    out: 3,
    rsn: 1,
  };
  return {
    id: r.id,
    hash: r.hash,
    model: r.model,
    day: r.day,
    window: r.window,
    actual,
    promptTokens: 7,
    completionTokens: 3,
    reasoningTokens: 1,
    kind,
    tokenCorrection: 0,
    event,
    ...extra,
  };
}

async function addCode(
  ledger: Ledger,
  caps: { total?: number; daily?: number; freezePct?: number; label?: string; sharePct?: number; maxInFlight?: number } = {},
): Promise<string> {
  const hash = await hashCode(newCode());
  await ledger.addCode({
    hash,
    label: caps.label ?? `c-${hash.slice(0, 8)}`,
    capTotal: caps.total ?? 1_000_000,
    capDaily: caps.daily ?? caps.total ?? 1_000_000,
    freezePct: caps.freezePct ?? 100,
    createdAt: Date.now(),
    ...(caps.sharePct === undefined ? {} : { sharePct: caps.sharePct }),
    ...(caps.maxInFlight === undefined ? {} : { maxInFlight: caps.maxInFlight }),
  });
  return hash;
}

export function ledgerContract(name: string, factory: LedgerFactory): void {
  describe(`ledger contract: ${name}`, () => {
    let cleanups: (() => Promise<void>)[] = [];
    afterEach(async () => {
      for (const cleanup of cleanups) await cleanup();
      cleanups = [];
    });
    async function fresh(globalCap = 10_000_000): Promise<Ledger> {
      const made = await factory();
      if (made.cleanup) cleanups.push(made.cleanup);
      if (globalCap > 0) await made.ledger.setGlobalCap(globalCap);
      return made.ledger;
    }

    it("refuses everything until the global cap is set", async () => {
      const ledger = await fresh(0);
      const hash = await addCode(ledger);
      expect(await ledger.reserve(reserveRequest(hash))).toEqual({ ok: false, reason: "global_unset" });
    });

    it("holds the reservation, then books the actual cost once", async () => {
      const ledger = await fresh();
      const hash = await addCode(ledger, { total: 5000 });
      const r = reserveRequest(hash, { amount: 1000 });
      expect(await ledger.reserve(r)).toEqual({ ok: true, expired: 0 });
      expect(await ledger.reserve(r)).toEqual({ ok: true, expired: 0 }); // a retried reserve is not held twice
      expect(await ledger.getCode(hash)).toMatchObject({ reserved: 1000, spent: 0 });
      expect(await ledger.getGlobal()).toMatchObject({ reserved: 1000, spent: 0, cap: 10_000_000 });
      expect(await ledger.getDay(hash, DAY)).toEqual({ spent: 0, reserved: 1000, requests: 0 });
      expect(await ledger.settle(settleRequest(r, 400))).toEqual({ settled: true });
      expect(await ledger.settle(settleRequest(r, 400))).toEqual({ settled: false });
      expect(await ledger.getCode(hash)).toMatchObject({ reserved: 0, spent: 400, requests: 1 });
      expect(await ledger.getGlobal()).toMatchObject({ reserved: 0, spent: 400 });
      expect(await ledger.getDay(hash, DAY)).toEqual({ spent: 400, reserved: 0, requests: 1 });
    });

    it("refuses past each cap and says how much is left", async () => {
      const ledger = await fresh(3000);
      const total = await addCode(ledger, { total: 1000 });
      expect((await ledger.reserve(reserveRequest(total, { amount: 600 }))).ok).toBe(true);
      expect(await ledger.reserve(reserveRequest(total, { amount: 600 }))).toEqual({
        ok: false,
        reason: "cap_total",
        remaining: 400,
        cap: 1000,
      });
      const daily = await addCode(ledger, { total: 10_000, daily: 500 });
      expect((await ledger.reserve(reserveRequest(daily, { amount: 300 }))).ok).toBe(true);
      expect(await ledger.reserve(reserveRequest(daily, { amount: 300 }))).toEqual({
        ok: false,
        reason: "cap_daily",
        remaining: 200,
        cap: 500,
      });
      // Global: 600 + 300 held of 3000. The refusal names the code's own room, never the global figure.
      const big = await addCode(ledger, { total: 100_000, daily: 50_000 });
      expect(await ledger.reserve(reserveRequest(big, { amount: 2200 }))).toEqual({
        ok: false,
        reason: "global_cap",
        remaining: 50_000,
      });
    });

    it("pauses a code at its freeze share of the global cap", async () => {
      const ledger = await fresh(1000);
      const dev = await addCode(ledger, { total: 100_000, freezePct: 50 });
      const judge = await addCode(ledger, { total: 100_000 });
      expect((await ledger.reserve(reserveRequest(dev, { amount: 400 }))).ok).toBe(true);
      expect(await ledger.reserve(reserveRequest(dev, { amount: 200 }))).toEqual({
        ok: false,
        reason: "global_freeze",
        remaining: 100_000 - 400,
      });
      expect((await ledger.reserve(reserveRequest(judge, { amount: 200 }))).ok).toBe(true);
    });

    it("charges the daily cap per UTC day", async () => {
      const ledger = await fresh();
      const hash = await addCode(ledger, { total: 10_000, daily: 1000 });
      const first = reserveRequest(hash, { amount: 1000 });
      expect((await ledger.reserve(first)).ok).toBe(true);
      await ledger.settle(settleRequest(first, 1000));
      expect((await ledger.reserve(reserveRequest(hash, { amount: 1 }))).ok).toBe(false);
      const tomorrow = reserveRequest(hash, { amount: 1000, day: "2026-10-04", window: WINDOW + 1440 });
      expect((await ledger.reserve(tomorrow)).ok).toBe(true);
      expect(await ledger.getCode(hash)).toMatchObject({ spent: 1000, reserved: 1000 });
    });

    it("refuses unknown, revoked and switched-off", async () => {
      const ledger = await fresh();
      const hash = await addCode(ledger);
      expect(await ledger.reserve(reserveRequest("f".repeat(64)))).toEqual({ ok: false, reason: "unknown_code" });
      await ledger.setDisabled(true);
      expect(await ledger.reserve(reserveRequest(hash))).toEqual({ ok: false, reason: "disabled" });
      expect((await ledger.getGlobal()).disabled).toBe(true);
      await ledger.setDisabled(false);
      expect((await ledger.reserve(reserveRequest(hash))).ok).toBe(true);
      expect(await ledger.updateCode(hash, { revoked: true })).toBe(true);
      expect(await ledger.reserve(reserveRequest(hash))).toEqual({ ok: false, reason: "revoked" });
      expect(await ledger.updateCode("0".repeat(64), { revoked: true })).toBe(false);
    });

    it("limits requests per sliding minute", async () => {
      const ledger = await fresh();
      const hash = await addCode(ledger);
      for (let i = 0; i < 2; i += 1) expect((await ledger.reserve(reserveRequest(hash, { rpm: 2, elapsedMs: 1000 }))).ok).toBe(true);
      expect(await ledger.reserve(reserveRequest(hash, { rpm: 2, elapsedMs: 2000 }))).toEqual({
        ok: false,
        reason: "rate_limited",
        scope: "model",
        which: "rpm",
        cur: 2,
        prev: 0,
        elapsedMs: 2000,
        limit: 2,
        need: 1,
        inFlight: 2,
      });
      // Next minute, 10 s in: the last minute still weighs 2 * 50/60 > 1.
      expect((await ledger.reserve(reserveRequest(hash, { rpm: 2, window: WINDOW + 1, elapsedMs: 10_000 }))).ok).toBe(false);
      // 31 s in: 2 * 29/60 < 1, so one more fits.
      expect((await ledger.reserve(reserveRequest(hash, { rpm: 2, window: WINDOW + 1, elapsedMs: 31_000 }))).ok).toBe(true);
    });

    it("keeps the minutes other hosts count when one host's clock runs ahead", async () => {
      const ledger = await fresh();
      const hash = await addCode(ledger);
      const at = { rpm: 2, elapsedMs: 1000 };
      for (let i = 0; i < 2; i += 1) expect((await ledger.reserve(reserveRequest(hash, at))).ok).toBe(true);
      expect(await ledger.reserve(reserveRequest(hash, { ...at, elapsedMs: 2000 }))).toMatchObject({ reason: "rate_limited", scope: "model", which: "rpm" });
      // A host whose clock runs three minutes ahead takes a call in what is, to it, a quiet minute.
      expect((await ledger.reserve(reserveRequest(hash, { rpm: 2, window: WINDOW + 3, elapsedMs: 1000 }))).ok).toBe(true);
      // The hosts on the right clock still count their own two calls in this minute.
      expect(await ledger.reserve(reserveRequest(hash, { ...at, elapsedMs: 3000 }))).toMatchObject({
        reason: "rate_limited",
        scope: "model",
        which: "rpm",
        cur: 2,
      });
    });

    it("limits tokens per minute and corrects the guess at settlement", async () => {
      const ledger = await fresh();
      const hash = await addCode(ledger);
      const first = reserveRequest(hash, { tpm: 1000, rateTokens: 800 });
      expect((await ledger.reserve(first)).ok).toBe(true);
      const refused = await ledger.reserve(reserveRequest(hash, { tpm: 1000, rateTokens: 300 }));
      expect(refused).toMatchObject({ ok: false, reason: "rate_limited", scope: "model", which: "tpm", cur: 800, need: 300 });
      await ledger.settle(settleRequest(first, 50, "usage", { tokenCorrection: -700 }));
      expect((await ledger.reserve(reserveRequest(hash, { tpm: 1000, rateTokens: 300 }))).ok).toBe(true);
    });

    it("takes one oversized prompt into an empty minute", async () => {
      const ledger = await fresh();
      const hash = await addCode(ledger);
      expect((await ledger.reserve(reserveRequest(hash, { tpm: 1000, rateTokens: 5000 }))).ok).toBe(true);
      expect((await ledger.reserve(reserveRequest(hash, { tpm: 1000, rateTokens: 1 }))).ok).toBe(false);
    });

    it("sweeps overdue reservations at their full amount, once", async () => {
      const ledger = await fresh();
      const hash = await addCode(ledger, { total: 10_000 });
      const r = reserveRequest(hash, { amount: 700 });
      r.deadlineMs = r.nowMs + 100;
      expect((await ledger.reserve(r)).ok).toBe(true);
      expect(await ledger.sweep(r.nowMs)).toBe(0);
      const later = reserveRequest(hash, { amount: 10, elapsedMs: 5000 });
      expect(await ledger.reserve(later)).toEqual({ ok: true, expired: 1 });
      expect(await ledger.sweep(r.nowMs + 200)).toBe(1);
      expect(await ledger.sweep(r.nowMs + 200)).toBe(0);
      expect(await ledger.getCode(hash)).toMatchObject({ spent: 700, reserved: 10, inFlight: 1 });
      expect(await ledger.settle(settleRequest(r, 5))).toEqual({ settled: false });
      const report = await ledger.report({ days: [DAY], events: 5, nowMs: r.nowMs + 300 });
      expect(report.events[0]).toMatchObject({ id: r.id, kind: "full", why: "expired", actual: 700 });
      expect(report.openReservations).toBe(1);
    });

    it("reports overdue holds with a refusal too, so the refused caller can sweep them", async () => {
      const ledger = await fresh();
      const hash = await addCode(ledger, { total: 10_000, maxInFlight: 1 });
      const dead = reserveRequest(hash, { amount: 700 });
      dead.deadlineMs = dead.nowMs + 100;
      expect((await ledger.reserve(dead)).ok).toBe(true);
      const next = reserveRequest(hash, { elapsedMs: 5000 });
      expect(await ledger.reserve(next)).toEqual({ ok: false, reason: "in_flight", inFlight: 1, limit: 1, expired: 1 });
      expect(await ledger.reserve(reserveRequest(hash, { amount: 9500, elapsedMs: 5000 }))).toEqual({
        ok: false,
        reason: "cap_total",
        remaining: 10_000 - 700,
        cap: 10_000,
        expired: 1,
      });
      expect(await ledger.sweep(next.nowMs)).toBe(1);
      expect(await ledger.reserve(reserveRequest(hash, { elapsedMs: 6000 }))).toEqual({ ok: true, expired: 0 });
      expect(await ledger.getCode(hash)).toMatchObject({ spent: 700, inFlight: 1 });
    });

    it("reports usage by day and model, newest events first", async () => {
      const ledger = await fresh();
      const hash = await addCode(ledger, { label: "reporter" });
      const a = reserveRequest(hash, { amount: 500 });
      const b = reserveRequest(hash, { amount: 500 });
      const c = reserveRequest(hash, { amount: 500 });
      for (const r of [a, b, c]) expect((await ledger.reserve(r)).ok).toBe(true);
      await ledger.settle(settleRequest(a, 120, "usage"));
      await ledger.settle(settleRequest(b, 500, "full", { promptTokens: 0, completionTokens: 0, reasoningTokens: 0 }));
      await ledger.settle(settleRequest(c, 0, "released", { promptTokens: 0, completionTokens: 0, reasoningTokens: 0 }));
      const report = await ledger.report({ days: [DAY, "2026-10-02"], events: 2, nowMs: a.nowMs + 5000 });
      expect(report.days[0]).toEqual({
        day: DAY,
        models: [
          {
            model: MODEL,
            requests: 3,
            promptTokens: 7,
            completionTokens: 3,
            reasoningTokens: 1,
            cost: 620,
            usage: 1,
            full: 1,
            released: 1,
            reconcile: 0,
          },
        ],
      });
      expect(report.days[1]).toEqual({ day: "2026-10-02", models: [] });
      expect(report.events.map((e) => e.id)).toEqual([c.id, b.id]);
      expect(report.global).toMatchObject({ spent: 620, reserved: 0, opening: 0 });
      expect(report.codes.find((code) => code.hash === hash)).toMatchObject({ label: "reporter", spent: 620, requests: 3 });
      const listed = await ledger.listCodes(DAY);
      expect(listed.find((code) => code.hash === hash)?.today).toEqual({ spent: 620, reserved: 0, requests: 3 });
    });

    it("keeps labels unique and caps editable", async () => {
      const ledger = await fresh();
      const hash = await addCode(ledger, { label: "judge-primary" });
      await expect(addCode(ledger, { label: "judge-primary" })).rejects.toThrow(/taken/);
      expect(await ledger.getCode(hash)).toMatchObject({ sharePct: null, maxInFlight: null, frozen: false, inFlight: 0 });
      expect(await ledger.updateCode(hash, { capTotal: 77, capDaily: 66, freezePct: 85, sharePct: 40, maxInFlight: 3 })).toBe(true);
      expect(await ledger.getCode(hash)).toMatchObject({ capTotal: 77, capDaily: 66, freezePct: 85, sharePct: 40, maxInFlight: 3, revoked: false });
    });

    it("refuses a settlement whose keys do not match its reservation", async () => {
      const ledger = await fresh();
      const hash = await addCode(ledger);
      const r = reserveRequest(hash);
      expect((await ledger.reserve(r)).ok).toBe(true);
      await expect(ledger.settle(settleRequest(r, 1, "usage", { day: "2026-10-04" }))).rejects.toThrow(/match/);
      expect(await ledger.settle(settleRequest(r, 1))).toEqual({ settled: true });
    });

    it("limits the calls one code has running, and frees a slot at settlement", async () => {
      const ledger = await fresh();
      const hash = await addCode(ledger);
      const [a, b] = [reserveRequest(hash, { defaultMaxInFlight: 2 }), reserveRequest(hash, { defaultMaxInFlight: 2 })];
      for (const r of [a, b]) expect((await ledger.reserve(r)).ok).toBe(true);
      expect(await ledger.reserve(reserveRequest(hash, { defaultMaxInFlight: 2 }))).toEqual({ ok: false, reason: "in_flight", inFlight: 2, limit: 2 });
      // A retried reserve of a call already held is not a new call.
      expect(await ledger.reserve(a)).toEqual({ ok: true, expired: 0 });
      await ledger.settle(settleRequest(a, 50));
      expect(await ledger.getCode(hash)).toMatchObject({ inFlight: 1 });
      expect((await ledger.reserve(reserveRequest(hash, { defaultMaxInFlight: 2 }))).ok).toBe(true);
      // A code's own limit wins over the relay's default.
      const wide = await addCode(ledger, { maxInFlight: 3 });
      for (let i = 0; i < 3; i += 1) expect((await ledger.reserve(reserveRequest(wide, { defaultMaxInFlight: 1 }))).ok).toBe(true);
      expect(await ledger.reserve(reserveRequest(wide, { defaultMaxInFlight: 1 }))).toMatchObject({ reason: "in_flight", limit: 3 });
    });

    it("gives each code its share of a model's requests per minute", async () => {
      const ledger = await fresh();
      const greedy = await addCode(ledger);
      const other = await addCode(ledger);
      const at = { rpm: 8, defaultSharePct: 25, elapsedMs: 1000 };
      for (let i = 0; i < 2; i += 1) expect((await ledger.reserve(reserveRequest(greedy, at))).ok).toBe(true);
      expect(await ledger.reserve(reserveRequest(greedy, at))).toEqual({
        ok: false,
        reason: "rate_limited",
        scope: "code",
        which: "rpm",
        cur: 2,
        prev: 0,
        elapsedMs: 1000,
        limit: 2,
        need: 1,
        inFlight: 2,
      });
      // The model still has room for everyone else.
      expect((await ledger.reserve(reserveRequest(other, at))).ok).toBe(true);
      const owner = await addCode(ledger, { sharePct: 100 });
      for (let i = 0; i < 5; i += 1) expect((await ledger.reserve(reserveRequest(owner, at))).ok).toBe(true);
      expect(await ledger.reserve(reserveRequest(owner, at))).toMatchObject({ reason: "rate_limited", scope: "model", which: "rpm" });
    });

    it("lets a code with nothing running start one call larger than its token share", async () => {
      const ledger = await fresh();
      const hash = await addCode(ledger);
      const at = { tpm: 1000, defaultSharePct: 50 };
      const big = reserveRequest(hash, { ...at, rateTokens: 800 });
      expect((await ledger.reserve(big)).ok).toBe(true);
      // While it runs, another call must fit in what is left of the share (500).
      expect(await ledger.reserve(reserveRequest(hash, { ...at, rateTokens: 100 }))).toMatchObject({
        reason: "rate_limited",
        scope: "code",
        which: "tpm",
        cur: 800,
        limit: 500,
        need: 100,
        inFlight: 1,
      });
      // Settled at 100 real tokens, the window is back within the share: the next big call goes.
      await ledger.settle(settleRequest(big, 10, "usage", { tokenCorrection: -700 }));
      expect((await ledger.reserve(reserveRequest(hash, { ...at, rateTokens: 800 }))).ok).toBe(true);
    });

    it("freezes a code whose settled cost passes its hold, until it is unfrozen", async () => {
      const ledger = await fresh();
      const hash = await addCode(ledger, { total: 100_000 });
      const r = reserveRequest(hash, { amount: 100 });
      expect((await ledger.reserve(r)).ok).toBe(true);
      expect(await ledger.settle(settleRequest(r, 150))).toEqual({ settled: true });
      expect(await ledger.getCode(hash)).toMatchObject({ spent: 150, reserved: 0, frozen: true, inFlight: 0 });
      expect(await ledger.reserve(reserveRequest(hash))).toEqual({ ok: false, reason: "frozen" });
      expect(await ledger.updateCode(hash, { frozen: false })).toBe(true);
      expect((await ledger.reserve(reserveRequest(hash))).ok).toBe(true);
    });

    it("refuses settlement figures out of range and keeps the hold for a sweep", async () => {
      const ledger = await fresh();
      const hash = await addCode(ledger);
      const r = reserveRequest(hash, { amount: 100 });
      expect((await ledger.reserve(r)).ok).toBe(true);
      for (const bad of [
        settleRequest(r, 1e16),
        settleRequest(r, -1),
        settleRequest(r, 1.5),
        settleRequest(r, 5, "usage", { promptTokens: 1e11 }),
        settleRequest(r, 5, "usage", { tokenCorrection: -1e11 }),
      ]) {
        await expect(ledger.settle(bad)).rejects.toThrow(/range/);
      }
      expect(await ledger.getCode(hash)).toMatchObject({ reserved: 100, spent: 0, requests: 0 });
      expect(await ledger.getGlobal()).toMatchObject({ reserved: 100, spent: 0 });
      expect(await ledger.settle(settleRequest(r, 40))).toEqual({ settled: true });
      expect(await ledger.getCode(hash)).toMatchObject({ reserved: 0, spent: 40 });
    });

    it("carries an opening balance into the global spend, once, and moves it on a change", async () => {
      const ledger = await fresh(10_000);
      expect(await ledger.setOpeningBalance(3000)).toEqual({ previous: 0 });
      expect(await ledger.getGlobal()).toMatchObject({ spent: 3000, opening: 3000 });
      expect(await ledger.setOpeningBalance(3000)).toEqual({ previous: 3000 });
      expect(await ledger.getGlobal()).toMatchObject({ spent: 3000, opening: 3000 });
      expect(await ledger.setOpeningBalance(1000)).toEqual({ previous: 3000 });
      expect(await ledger.getGlobal()).toMatchObject({ spent: 1000, opening: 1000 });
      const hash = await addCode(ledger, { total: 100_000 });
      expect(await ledger.reserve(reserveRequest(hash, { amount: 9001 }))).toMatchObject({ reason: "global_cap" });
      expect((await ledger.reserve(reserveRequest(hash, { amount: 9000 }))).ok).toBe(true);
    });

    it("counts calls booked for reconciliation by day and model", async () => {
      const ledger = await fresh();
      const hash = await addCode(ledger);
      const r = reserveRequest(hash, { amount: 300 });
      expect((await ledger.reserve(r)).ok).toBe(true);
      await ledger.settle(settleRequest(r, 300, "full", { reconcile: true }));
      const report = await ledger.report({ days: [DAY], events: 1, nowMs: r.nowMs + 1000 });
      expect(report.days[0]!.models[0]).toMatchObject({ requests: 1, full: 1, reconcile: 1, cost: 300 });
    });

    it("books every open reservation in full when swept with a later cutoff", async () => {
      const ledger = await fresh();
      const hash = await addCode(ledger, { total: 100_000 });
      const r = reserveRequest(hash, { amount: 250 });
      expect((await ledger.reserve(r)).ok).toBe(true);
      expect(await ledger.sweep(r.nowMs)).toBe(0);
      expect(await ledger.sweep(r.nowMs, 100, Number.MAX_SAFE_INTEGER)).toBe(1);
      expect(await ledger.getCode(hash)).toMatchObject({ spent: 250, reserved: 0, inFlight: 0 });
    });

    it("never lets concurrent reservations pass a cap", async () => {
      const ledger = await fresh();
      const hash = await addCode(ledger, { total: 1000 });
      const outcomes = await Promise.all(
        Array.from({ length: 40 }, () => ledger.reserve(reserveRequest(hash, { amount: 100 }))),
      );
      expect(outcomes.filter((o) => o.ok)).toHaveLength(10);
      expect(outcomes.filter((o) => !o.ok && o.reason === "cap_total")).toHaveLength(30);
      expect(await ledger.getCode(hash)).toMatchObject({ reserved: 1000, spent: 0 });
    });
  });
}
