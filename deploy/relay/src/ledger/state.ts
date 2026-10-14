/**
 * The ledger's rules as plain functions over one in-memory state. FileLedger runs them
 * under its lock; lua.ts is the same rules for Redis, check for check, in the same order.
 */

import { admits, codeAdmitsTokens, shareOf } from "../limiter.js";
import {
  type CodeState,
  type DayState,
  type GlobalState,
  type ReserveOutcome,
  type ReserveRequest,
  type SettleRequest,
  decodeReservation,
  encodeReservation,
  settleFiguresInRange,
} from "./types.js";

export interface StoredCode {
  label: string;
  capTotal: number;
  capDaily: number;
  freezePct: number;
  /** Absent in ledgers written before these existed: the relay's defaults apply. */
  sharePct?: number;
  maxInFlight?: number;
  revoked: boolean;
  frozen?: boolean;
  createdAt: number;
  spent: number;
  reserved: number;
  requests: number;
  inFlight?: number;
}

export interface StoredUsage {
  n: number;
  in: number;
  out: number;
  rsn: number;
  cost: number;
  usage: number;
  full: number;
  released: number;
  reconcile?: number;
}

type Counter = { req: number; tok: number };

export interface LedgerState {
  version: 1;
  global: GlobalState;
  codes: Record<string, StoredCode>;
  days: Record<string, Record<string, DayState>>;
  usage: Record<string, Record<string, StoredUsage>>;
  reservations: Record<string, { record: string; deadline: number }>;
  /** model -> limiter minute -> counts, shared by every code. */
  limiter: Record<string, Record<string, Counter>>;
  /** "<code hash>|<model>" -> limiter minute -> counts, one code's share. */
  codeLimiter: Record<string, Record<string, Counter>>;
}

export function emptyState(): LedgerState {
  return {
    version: 1,
    global: { cap: 0, spent: 0, reserved: 0, disabled: false, opening: 0 },
    codes: {},
    days: {},
    usage: {},
    reservations: {},
    limiter: {},
    codeLimiter: {},
  };
}

/** Fills in what a ledger file written by an older relay lacks. */
export function normalizeState(state: LedgerState): LedgerState {
  state.global.opening ??= 0;
  state.codeLimiter ??= {};
  return state;
}

export function codeView(hash: string, code: StoredCode): CodeState {
  return {
    hash,
    id: hash.slice(0, 12),
    label: code.label,
    capTotal: code.capTotal,
    capDaily: code.capDaily,
    freezePct: code.freezePct,
    sharePct: code.sharePct ?? null,
    maxInFlight: code.maxInFlight ?? null,
    revoked: code.revoked,
    frozen: code.frozen ?? false,
    createdAt: code.createdAt,
    spent: code.spent,
    reserved: code.reserved,
    requests: code.requests,
    inFlight: code.inFlight ?? 0,
  };
}

function dayOf(state: LedgerState, day: string, hash: string): DayState {
  const days = (state.days[day] ??= {});
  return (days[hash] ??= { spent: 0, reserved: 0, requests: 0 });
}

function slot(table: Record<string, Record<string, Counter>>, key: string, window: number): Counter {
  const windows = (table[key] ??= {});
  return (windows[String(window)] ??= { req: 0, tok: 0 });
}

function peek(table: Record<string, Record<string, Counter>>, key: string, window: number): Counter {
  return table[key]?.[String(window)] ?? { req: 0, tok: 0 };
}

export function codeLimiterKey(hash: string, model: string): string {
  return `${hash}|${model}`;
}

export function reserveIn(state: LedgerState, r: ReserveRequest): ReserveOutcome {
  if (state.reservations[r.id]) return { ok: true, expired: 0 }; // a retried reserve: already held
  // Holds past their deadline, counted before anything changes, as the Lua does. A refusal
  // reports them too, so its caller can sweep them (types.ts, ReserveOutcome).
  let expired = 0;
  for (const held of Object.values(state.reservations)) if (held.deadline <= r.nowMs) expired += 1;
  const outcome = admit(state, r);
  if (outcome.ok) return { ok: true, expired };
  return expired > 0 ? { ...outcome, expired } : outcome;
}

/** The checks in the Lua's order; holds the reservation when they all pass. */
function admit(state: LedgerState, r: ReserveRequest): ReserveOutcome {
  const g = state.global;
  if (g.disabled) return { ok: false, reason: "disabled" };
  const code = state.codes[r.hash];
  if (!code) return { ok: false, reason: "unknown_code" };
  if (code.revoked) return { ok: false, reason: "revoked" };
  if (code.frozen) return { ok: false, reason: "frozen" };
  if (g.cap <= 0) return { ok: false, reason: "global_unset" };
  if (code.spent + code.reserved + r.amount > code.capTotal) {
    return { ok: false, reason: "cap_total", remaining: code.capTotal - code.spent - code.reserved, cap: code.capTotal };
  }
  const today = state.days[r.day]?.[r.hash] ?? { spent: 0, reserved: 0, requests: 0 };
  if (today.spent + today.reserved + r.amount > code.capDaily) {
    return { ok: false, reason: "cap_daily", remaining: code.capDaily - today.spent - today.reserved, cap: code.capDaily };
  }
  const own = Math.min(code.capTotal - code.spent - code.reserved, code.capDaily - today.spent - today.reserved);
  if (g.spent + g.reserved + r.amount > g.cap) {
    return { ok: false, reason: "global_cap", remaining: own };
  }
  if (code.freezePct < 100 && (g.spent + g.reserved + r.amount) * 100 > g.cap * code.freezePct) {
    return { ok: false, reason: "global_freeze", remaining: own };
  }
  const inFlight = code.inFlight ?? 0;
  const maxInFlight = code.maxInFlight ?? r.defaultMaxInFlight;
  if (inFlight >= maxInFlight) return { ok: false, reason: "in_flight", inFlight, limit: maxInFlight };
  const e = r.elapsedMs;
  // The model's budget, shared by every code.
  const cur = peek(state.limiter, r.model, r.window);
  const prev = peek(state.limiter, r.model, r.window - 1);
  if (!admits(cur.req, prev.req, e, r.rpm, 1, false)) {
    return { ok: false, reason: "rate_limited", scope: "model", which: "rpm", cur: cur.req, prev: prev.req, elapsedMs: e, limit: r.rpm, need: 1, inFlight };
  }
  if (!admits(cur.tok, prev.tok, e, r.tpm, r.rateTokens, true)) {
    return { ok: false, reason: "rate_limited", scope: "model", which: "tpm", cur: cur.tok, prev: prev.tok, elapsedMs: e, limit: r.tpm, need: r.rateTokens, inFlight };
  }
  // This code's share of it.
  const share = code.sharePct ?? r.defaultSharePct;
  const codeRpm = shareOf(r.rpm, share);
  const codeTpm = shareOf(r.tpm, share);
  const key = codeLimiterKey(r.hash, r.model);
  const mine = peek(state.codeLimiter, key, r.window);
  const mineBefore = peek(state.codeLimiter, key, r.window - 1);
  if (!admits(mine.req, mineBefore.req, e, codeRpm, 1, false)) {
    return { ok: false, reason: "rate_limited", scope: "code", which: "rpm", cur: mine.req, prev: mineBefore.req, elapsedMs: e, limit: codeRpm, need: 1, inFlight };
  }
  if (!codeAdmitsTokens(mine.tok, mineBefore.tok, e, codeTpm, r.rateTokens, inFlight)) {
    return { ok: false, reason: "rate_limited", scope: "code", which: "tpm", cur: mine.tok, prev: mineBefore.tok, elapsedMs: e, limit: codeTpm, need: r.rateTokens, inFlight };
  }
  g.reserved += r.amount;
  code.reserved += r.amount;
  code.inFlight = inFlight + 1;
  dayOf(state, r.day, r.hash).reserved += r.amount;
  const counter = slot(state.limiter, r.model, r.window);
  counter.req += 1;
  counter.tok += r.rateTokens;
  const ownCounter = slot(state.codeLimiter, key, r.window);
  ownCounter.req += 1;
  ownCounter.tok += r.rateTokens;
  state.reservations[r.id] = { record: encodeReservation(r), deadline: r.deadlineMs };
  return { ok: true, expired: 0 };
}

export function settleIn(state: LedgerState, s: SettleRequest): boolean {
  if (!settleFiguresInRange(s)) throw new Error("ledger: settle figures out of range");
  const held = state.reservations[s.id];
  if (!held) return false;
  const rec = decodeReservation(held.record);
  if (!rec) throw new Error("ledger: unreadable reservation record");
  if (rec.hash !== s.hash || rec.day !== s.day || rec.model !== s.model || rec.window !== s.window) {
    throw new Error("ledger: settle keys do not match the reservation");
  }
  const g = state.global;
  g.reserved -= rec.amount;
  g.spent += s.actual;
  const code = state.codes[rec.hash];
  if (code) {
    code.reserved -= rec.amount;
    code.spent += s.actual;
    code.requests += 1;
    code.inFlight = Math.max(0, (code.inFlight ?? 0) - 1);
    // Reported usage above the hold means the estimate missed: stop the code until the
    // operator has looked (admin unfreeze).
    if (s.actual > rec.amount) code.frozen = true;
  }
  const day = dayOf(state, rec.day, rec.hash);
  day.reserved -= rec.amount;
  day.spent += s.actual;
  day.requests += 1;
  const models = (state.usage[rec.day] ??= {});
  const row = (models[rec.model] ??= { n: 0, in: 0, out: 0, rsn: 0, cost: 0, usage: 0, full: 0, released: 0 });
  row.n += 1;
  row.in += s.promptTokens;
  row.out += s.completionTokens;
  row.rsn += s.reasoningTokens;
  row.cost += s.actual;
  row[s.kind] += 1;
  if (s.reconcile) row.reconcile = (row.reconcile ?? 0) + 1;
  if (s.tokenCorrection !== 0) {
    const counter = state.limiter[rec.model]?.[String(rec.window)];
    if (counter) counter.tok += s.tokenCorrection;
    const own = state.codeLimiter[codeLimiterKey(rec.hash, rec.model)]?.[String(rec.window)];
    if (own) own.tok += s.tokenCorrection;
  }
  // Last, as in the Lua: nothing above can leave the call both unbooked and unheld.
  delete state.reservations[s.id];
  return true;
}

/** Reservations whose deadline is at or before `cutoffMs` (oldest first), up to `limit`. */
export function expiredIn(state: LedgerState, cutoffMs: number, limit: number): { id: string; record: string }[] {
  return Object.entries(state.reservations)
    .filter(([, held]) => held.deadline <= cutoffMs)
    .sort((a, b) => a[1].deadline - b[1].deadline)
    .slice(0, limit)
    .map(([id, held]) => ({ id, record: held.record }));
}

/**
 * Keeps each limiter key's three newest minutes, whatever the caller's clock says, as lua.ts
 * does: a host whose clock runs ahead must not drop the minutes the other hosts still count.
 */
export function pruneLimiter(state: LedgerState): void {
  for (const table of [state.limiter, state.codeLimiter]) {
    for (const [key, windows] of Object.entries(table)) {
      const minutes = Object.keys(windows).sort((a, b) => Number(b) - Number(a));
      for (const minute of minutes.slice(3)) delete windows[minute];
      if (Object.keys(windows).length === 0) delete table[key];
    }
  }
}
