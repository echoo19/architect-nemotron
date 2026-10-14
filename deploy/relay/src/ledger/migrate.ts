/**
 * Moves an Upstash ledger from the first layout (one Redis hash per record, written by the
 * relay until 2026-10) to the current one (lua.ts), under the same key prefix. It copies;
 * it never deletes. The old keys stay as they were, switched off.
 *
 * Steps: switch the old ledger off, so no relay on the old code starts another call; wait
 * until its calls in flight have settled (or the wait runs out); copy the codes, labels,
 * caps and spend, each code's daily figures, the usage by day and model, the newest events
 * and any reservation still open; then write the global record, switched off, last. If the
 * old figures moved while copying (a late settlement), it copies again.
 */

import {
  type RedisLike,
  UpstashLedger,
  codeRecord,
  dayRecord,
  globalRecord,
  usageRecord,
} from "./upstash.js";
import { EVENTS_KEPT, type GlobalState } from "./types.js";

export interface MigrationOptions {
  /** Copy again over a new-layout ledger that has served no call since the copy (see the fingerprint). */
  force?: boolean;
  /** How long to wait for the old ledger's calls in flight to settle (default 300 s). */
  waitMs?: number;
  /** Between checks while waiting (default 2 s). */
  pollMs?: number;
  sleep?: (ms: number) => Promise<void>;
  log?: (line: string) => void;
}

export interface MigrationSummary {
  codes: number;
  codeDays: number;
  usageDays: number;
  events: number;
  /** Reservations still open on the old ledger, carried over; a sweep books them in full. */
  openReservations: number;
  global: GlobalState;
}

function num(value: unknown): number {
  const n = typeof value === "number" ? value : Number(value ?? 0);
  return Number.isFinite(n) ? n : 0;
}

function str(value: unknown): string {
  return typeof value === "string" ? value : value === null || value === undefined ? "" : String(value);
}

/** What the copy left in the new layout (`<prefix>:v2:migrated`), which --force checks. */
interface Fingerprint {
  spent: number;
  reserved: number;
  held: number;
}

function fingerprint(raw: unknown): Fingerprint | null {
  if (typeof raw !== "string") return null;
  try {
    const value = JSON.parse(raw) as Record<string, unknown>;
    const { spent, reserved, held } = value;
    if (typeof spent === "number" && typeof reserved === "number" && typeof held === "number") return { spent, reserved, held };
  } catch {
    // unreadable: treated as missing
  }
  return null;
}

async function scanAll(redis: RedisLike, match: string): Promise<string[]> {
  const keys = new Set<string>();
  let cursor = "0";
  do {
    const [next, found] = await redis.scan(cursor, match, 500);
    for (const key of found) keys.add(key);
    cursor = next;
  } while (cursor !== "0");
  return [...keys].sort();
}

/** Copies the old layout under `prefix` into the new one; see the file comment. */
export async function migrateFromV1(redis: RedisLike, prefix: string, options: MigrationOptions = {}): Promise<MigrationSummary> {
  if (!/^[A-Za-z0-9_.:-]{1,64}$/.test(prefix)) throw new Error("ledger: bad key prefix");
  const log = options.log ?? (() => undefined);
  const sleep = options.sleep ?? ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
  const old = (key: string) => `${prefix}:${key}`;
  const v2 = (key: string) => `${prefix}:v2:${key}`;

  const g1 = await redis.hgetall(old("g"));
  if (!g1) throw new Error(`ledger: no ledger in the old layout under "${prefix}"`);
  if (num(await redis.exists(v2("g"))) > 0) {
    if (!options.force) throw new Error(`ledger: "${prefix}" is already in the new layout (give --force to copy the old one over it again)`);
    // Copying again is safe only while the new ledger has served no call since the copy: its
    // spend, its holds and its open calls must still be what the copy left. A call held and
    // then switched off again changes its holds, never its spend.
    const now = await new UpstashLedger(redis, { prefix }).report({ days: [], events: 0, nowMs: 0 });
    if (!now.global.disabled) {
      throw new Error(`ledger: the new layout under "${prefix}" has been switched on; copying over it could lose what it booked`);
    }
    const left = fingerprint(await redis.get(v2("migrated")));
    if (!left) {
      throw new Error(`ledger: the new layout under "${prefix}" was not written by migrate; copying over it could lose what it holds`);
    }
    if (left.spent !== now.global.spent || left.reserved !== now.global.reserved || left.held !== now.openReservations) {
      throw new Error(
        `ledger: the new layout under "${prefix}" has calls of its own since the copy (its spend, holds or open calls changed); ` +
          "copying over it could lose them",
      );
    }
  }

  // 1. No new calls on the old code from here on.
  await redis.hset(old("g"), { disabled: "1" });
  log("switched the old ledger off");

  // 2. Let the calls already running settle.
  const waitMs = options.waitMs ?? 300_000;
  const pollMs = options.pollMs ?? 2_000;
  for (let waited = 0; ; waited += pollMs) {
    const open = Object.keys((await redis.hgetall(old("res"))) ?? {}).length;
    if (open === 0) break;
    if (waited >= waitMs) {
      log(`${open} reservation(s) still open after ${Math.round(waitMs / 1000)} s; carrying them over (a sweep books them in full)`);
      break;
    }
    if (waited === 0) log(`waiting for ${open} call(s) in flight to settle`);
    await sleep(pollMs);
  }

  for (let attempt = 0; attempt < 5; attempt += 1) {
    const before = (await redis.hgetall(old("g"))) ?? {};
    const summary = await copy(redis, before, old, v2);
    const after = (await redis.hgetall(old("g"))) ?? {};
    if (str(after.spent) === str(before.spent) && str(after.reserved) === str(before.reserved)) return summary;
    log("the old ledger changed while copying (a late settlement); copying again");
  }
  throw new Error("ledger: the old ledger kept changing while it was copied; try again");
}

async function copy(
  redis: RedisLike,
  g1: Record<string, unknown>,
  old: (key: string) => string,
  v2: (key: string) => string,
): Promise<MigrationSummary> {
  // Codes and labels.
  const members = await redis.smembers(old("codes"));
  const hashes = (Array.isArray(members) ? members.map(str) : []).filter((h) => /^[0-9a-f]{64}$/.test(h));
  let codes = 0;
  for (const hash of hashes) {
    const c = await redis.hgetall(old(`c:${hash}`));
    if (!c || c.cap_total === undefined) continue;
    const optional = (value: unknown) => (str(value) === "" ? undefined : num(value));
    const sharePct = optional(c.share_pct);
    const maxInFlight = optional(c.max_inflight);
    await redis.set(
      v2(`c:${hash}`),
      codeRecord({
        hash,
        label: str(c.label),
        capTotal: num(c.cap_total),
        capDaily: num(c.cap_daily),
        freezePct: c.freeze_pct === undefined ? 100 : num(c.freeze_pct),
        createdAt: num(c.created),
        ...(sharePct === undefined ? {} : { sharePct }),
        ...(maxInFlight === undefined ? {} : { maxInFlight }),
        spent: num(c.spent),
        reserved: num(c.reserved),
        requests: num(c.requests),
        inFlight: Math.max(0, num(c.inflight)),
        revoked: str(c.revoked) === "1",
        frozen: str(c.frozen) === "1",
      }),
    );
    await redis.sadd(v2("codes"), hash);
    if (str(c.label)) await redis.hset(v2("labels"), { [str(c.label)]: hash });
    codes += 1;
  }

  // Daily figures per code, and usage by day and model. The new layout keeps day records
  // (lua.ts), so they are copied without the expiry the old keys had.
  let codeDays = 0;
  for (const key of await scanAll(redis, old("cd:*"))) {
    const d = await redis.hgetall(key);
    if (!d) continue;
    const rest = key.slice(old("cd:").length);
    await redis.set(v2(`cd:${rest}`), dayRecord({ spent: num(d.spent), reserved: num(d.reserved), requests: num(d.requests) }));
    codeDays += 1;
  }
  let usageDays = 0;
  for (const key of await scanAll(redis, old("u:*"))) {
    const u = await redis.hgetall(key);
    if (!u) continue;
    await redis.set(v2(`u:${key.slice(old("u:").length)}`), usageRecord(u));
    usageDays += 1;
  }

  // The newest events.
  const lines = await redis.lrange(old("ev"), 0, EVENTS_KEPT - 1);
  const events = (Array.isArray(lines) ? lines.map(str) : []).filter((line) => line.startsWith("{"));
  if (events.length > 0) await redis.set(v2("ev"), events.join("\n") + "\n");

  // Reservations still open, with their deadlines.
  const res = (await redis.hgetall(old("res"))) ?? {};
  const scores = await redis.zrangeWithScores(old("resq"));
  const deadline = new Map<string, number>();
  if (Array.isArray(scores)) for (let i = 0; i + 1 < scores.length; i += 2) deadline.set(str(scores[i]), num(scores[i + 1]));
  const held = Object.entries(res).map(([id, value]) => {
    const recordText = str(value);
    const created = num(recordText.split("|")[6]);
    // A reservation without a deadline is overdue at once: the next sweep books it in full.
    return { id, record: recordText, deadline: deadline.get(id) ?? created };
  });

  // The global record last, switched off: until it exists the new relay refuses every call.
  // Just before it, the figures --force will check (written first, so a copy cut short
  // between the two leaves no global record and can simply run again).
  const global: GlobalState = {
    cap: num(g1.cap),
    spent: num(g1.spent),
    reserved: num(g1.reserved),
    disabled: true,
    opening: num(g1.opening),
  };
  const left: Fingerprint = { spent: global.spent, reserved: global.reserved, held: held.length };
  await redis.set(v2("migrated"), JSON.stringify(left));
  await redis.set(v2("g"), globalRecord(global, held));
  return { codes, codeDays, usageDays, events: events.length, openReservations: held.length, global };
}
