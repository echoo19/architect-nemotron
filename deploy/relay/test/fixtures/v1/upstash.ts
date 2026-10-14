/**
 * The Upstash ledger as it was before the move to one string per record: one Redis hash per
 * record, written by the relay until 2026-10. A test fixture, kept to write ledgers in the old
 * layout for test/ledger-migrate.test.ts.
 */

/**
 * UpstashLedger: the shared ledger in Redis (Upstash over REST). Reserve, settle and the
 * expiry claim are single Lua scripts, so two relays (local and Vercel, or several Vercel
 * instances) can never both spend the same dollar. Admin writes are plain commands.
 *
 * The store is anything with the RedisLike methods: @upstash/redis' Redis (created with
 * automaticDeserialization: false) in production, an ioredis-mock adapter in tests.
 */

import { CLAIM_LUA, OPENING_LUA, RESERVE_LUA, SETTLE_LUA } from "./lua.js";

/** The constants of that version. */
const EVENTS_KEPT = 20_000;
const LIMITER_TTL_MS = 180_000;
const DAY_TTL_SECONDS = 120 * 24 * 3600;
import {
  type CodePatch,
  type CodeState,
  type CodeView,
  type DayState,
  type GlobalState,
  type NewCode,
  type RelayEvent,
  type Report,
  type ReserveOutcome,
  type ReserveRequest,
  type SettleOutcome,
  type SettleRequest,
  type UsageRow,
  decodeReservation,
  encodeReservation,
  utcDay,
} from "../../../src/ledger/types.js";

export interface RedisLikeV1 {
  eval(script: string, keys: string[], args: string[]): Promise<unknown>;
  hgetall(key: string): Promise<Record<string, unknown> | null>;
  hset(key: string, values: Record<string, string>): Promise<unknown>;
  hsetnx(key: string, field: string, value: string): Promise<unknown>;
  hget(key: string, field: string): Promise<unknown>;
  hdel(key: string, field: string): Promise<unknown>;
  hlen(key: string): Promise<unknown>;
  exists(key: string): Promise<unknown>;
  sadd(key: string, member: string): Promise<unknown>;
  smembers(key: string): Promise<unknown>;
  zcount(key: string, min: string, max: string): Promise<unknown>;
  lrange(key: string, start: number, stop: number): Promise<unknown>;
}

/** A hash reply as an object: Redis' flat [field, value, ...] list or an object; null when empty. */
function asHash(reply: unknown): Record<string, unknown> | null {
  if (Array.isArray(reply)) {
    if (reply.length === 0) return null;
    const out: Record<string, unknown> = {};
    for (let i = 0; i + 1 < reply.length; i += 2) out[String(reply[i])] = reply[i + 1];
    return out;
  }
  if (typeof reply === "object" && reply !== null) return Object.keys(reply).length > 0 ? (reply as Record<string, unknown>) : null;
  return null;
}

/** The methods of @upstash/redis' Redis that UpstashLedger uses. */
export interface UpstashClient {
  eval(script: string, keys: string[], args: string[]): Promise<unknown>;
  hgetall(key: string): Promise<unknown>;
  hset(key: string, values: Record<string, string>): Promise<unknown>;
  hsetnx(key: string, field: string, value: string): Promise<unknown>;
  hget(key: string, field: string): Promise<unknown>;
  hdel(key: string, ...fields: string[]): Promise<unknown>;
  hlen(key: string): Promise<unknown>;
  exists(...keys: string[]): Promise<unknown>;
  sadd(key: string, member: string): Promise<unknown>;
  smembers(key: string): Promise<unknown>;
  zcount(key: string, min: string, max: string): Promise<unknown>;
  lrange(key: string, start: number, stop: number): Promise<unknown>;
}

/**
 * @upstash/redis' client as a RedisLike. Create the client with
 * automaticDeserialization: false so stored strings come back as written; with that
 * setting HGETALL returns Redis' flat field/value list, which this turns into an object.
 */
export function upstashStoreV1(client: UpstashClient): RedisLikeV1 {
  return {
    eval: (script, keys, args) => client.eval(script, keys, args),
    hgetall: async (key) => asHash(await client.hgetall(key)),
    hset: (key, values) => client.hset(key, values),
    hsetnx: (key, field, value) => client.hsetnx(key, field, value),
    hget: (key, field) => client.hget(key, field),
    hdel: (key, field) => client.hdel(key, field),
    hlen: (key) => client.hlen(key),
    exists: (key) => client.exists(key),
    sadd: (key, member) => client.sadd(key, member),
    smembers: (key) => client.smembers(key),
    zcount: (key, min, max) => client.zcount(key, min, max),
    lrange: (key, start, stop) => client.lrange(key, start, stop),
  };
}

function num(value: unknown): number {
  const n = typeof value === "number" ? value : Number(value ?? 0);
  return Number.isFinite(n) ? n : 0;
}

function str(value: unknown): string {
  return typeof value === "string" ? value : value === null || value === undefined ? "" : String(value);
}

/** A number field that may be absent (the relay's default applies then). */
function optional(value: unknown): number | null {
  const text = str(value);
  if (text === "") return null;
  const n = Number(text);
  return Number.isFinite(n) ? n : null;
}

export class UpstashLedgerV1 {
  readonly kind = "upstash" as const;
  readonly prefix: string;

  constructor(
    private readonly redis: RedisLikeV1,
    options: { prefix?: string } = {},
  ) {
    this.prefix = options.prefix ?? "relay";
    if (!/^[A-Za-z0-9_.:-]{1,64}$/.test(this.prefix)) throw new Error("ledger: bad key prefix");
  }

  // -- keys ------------------------------------------------------------------------------

  private get g() {
    return `${this.prefix}:g`;
  }
  private c(hash: string) {
    return `${this.prefix}:c:${hash}`;
  }
  private cd(hash: string, day: string) {
    return `${this.prefix}:cd:${hash}:${day}`;
  }
  private u(day: string) {
    return `${this.prefix}:u:${day}`;
  }
  private rl(model: string, window: number) {
    return `${this.prefix}:rl:${model}:${window}`;
  }
  private rlc(hash: string, model: string, window: number) {
    return `${this.prefix}:rlc:${hash}:${model}:${window}`;
  }
  private get res() {
    return `${this.prefix}:res`;
  }
  private get resq() {
    return `${this.prefix}:resq`;
  }
  private get ev() {
    return `${this.prefix}:ev`;
  }
  private get codes() {
    return `${this.prefix}:codes`;
  }
  private get labels() {
    return `${this.prefix}:labels`;
  }

  // -- reads -----------------------------------------------------------------------------

  private toCode(hash: string, raw: Record<string, unknown> | null): CodeState | null {
    if (!raw || raw.cap_total === undefined) return null;
    return {
      hash,
      id: hash.slice(0, 12),
      label: str(raw.label),
      capTotal: num(raw.cap_total),
      capDaily: num(raw.cap_daily),
      freezePct: raw.freeze_pct === undefined ? 100 : num(raw.freeze_pct),
      sharePct: optional(raw.share_pct),
      maxInFlight: optional(raw.max_inflight),
      revoked: str(raw.revoked) === "1",
      frozen: str(raw.frozen) === "1",
      createdAt: num(raw.created),
      spent: num(raw.spent),
      reserved: num(raw.reserved),
      requests: num(raw.requests),
      inFlight: Math.max(0, num(raw.inflight)),
    };
  }

  async getCode(hash: string): Promise<CodeState | null> {
    return this.toCode(hash, await this.redis.hgetall(this.c(hash)));
  }

  async getGlobal(): Promise<GlobalState> {
    const raw = (await this.redis.hgetall(this.g)) ?? {};
    return {
      cap: num(raw.cap),
      spent: num(raw.spent),
      reserved: num(raw.reserved),
      disabled: str(raw.disabled) === "1",
      opening: num(raw.opening),
    };
  }

  async getDay(hash: string, day: string): Promise<DayState> {
    const raw = (await this.redis.hgetall(this.cd(hash, day))) ?? {};
    return { spent: num(raw.spent), reserved: num(raw.reserved), requests: num(raw.requests) };
  }

  // -- reserve / settle / sweep -----------------------------------------------------------

  async reserve(r: ReserveRequest): Promise<ReserveOutcome> {
    const reply = await this.redis.eval(
      RESERVE_LUA,
      [
        this.g,
        this.c(r.hash),
        this.cd(r.hash, r.day),
        this.rl(r.model, r.window),
        this.rl(r.model, r.window - 1),
        this.res,
        this.resq,
        this.rlc(r.hash, r.model, r.window),
        this.rlc(r.hash, r.model, r.window - 1),
      ],
      [
        r.id,
        encodeReservation(r),
        String(r.amount),
        String(r.rateTokens),
        String(r.rpm),
        String(r.tpm),
        String(r.elapsedMs),
        String(r.deadlineMs),
        String(r.nowMs),
        String(DAY_TTL_SECONDS),
        String(LIMITER_TTL_MS),
        String(r.defaultSharePct),
        String(r.defaultMaxInFlight),
      ],
    );
    if (!Array.isArray(reply) || reply.length === 0) throw new Error("ledger: unexpected reserve reply");
    const [tag, ...rest] = reply as unknown[];
    switch (str(tag)) {
      case "ok":
        return { ok: true, expired: num(rest[0]) };
      case "disabled":
      case "unknown_code":
      case "revoked":
      case "frozen":
      case "global_unset":
        return { ok: false, reason: str(tag) as "disabled" | "unknown_code" | "revoked" | "frozen" | "global_unset" };
      case "cap_total":
      case "cap_daily":
        return { ok: false, reason: str(tag) as "cap_total" | "cap_daily", remaining: num(rest[0]), cap: num(rest[1]) };
      case "global_cap":
      case "global_freeze":
        return { ok: false, reason: str(tag) as "global_cap" | "global_freeze", remaining: num(rest[0]) };
      case "in_flight":
        return { ok: false, reason: "in_flight", inFlight: num(rest[0]), limit: num(rest[1]) };
      case "rate_limited":
        return {
          ok: false,
          reason: "rate_limited",
          scope: str(rest[0]) === "code" ? "code" : "model",
          which: str(rest[1]) === "tpm" ? "tpm" : "rpm",
          cur: num(rest[2]),
          prev: num(rest[3]),
          elapsedMs: num(rest[4]),
          limit: num(rest[5]),
          need: num(rest[6]),
          inFlight: num(rest[7]),
        };
      default:
        throw new Error(`ledger: unexpected reserve reply ${str(tag)}`);
    }
  }

  async settle(s: SettleRequest): Promise<SettleOutcome> {
    const reply = await this.redis.eval(
      SETTLE_LUA,
      [
        this.g,
        this.c(s.hash),
        this.cd(s.hash, s.day),
        this.u(s.day),
        this.rl(s.model, s.window),
        this.res,
        this.resq,
        this.ev,
        this.rlc(s.hash, s.model, s.window),
      ],
      [
        s.id,
        this.prefix,
        String(s.actual),
        String(s.promptTokens),
        String(s.completionTokens),
        String(s.reasoningTokens),
        s.kind,
        String(s.tokenCorrection),
        JSON.stringify(s.event),
        String(EVENTS_KEPT - 1),
        String(DAY_TTL_SECONDS),
        s.reconcile ? "1" : "0",
      ],
    );
    return { settled: num(reply) === 1 };
  }

  async sweep(nowMs: number, limit = 100, cutoffMs = nowMs): Promise<number> {
    const reply = await this.redis.eval(CLAIM_LUA, [this.res, this.resq], [String(cutoffMs), String(limit), String(nowMs + 60_000)]);
    const items = str(reply).split("\n").filter(Boolean);
    let swept = 0;
    for (let i = 0; i + 1 < items.length; i += 2) {
      const id = str(items[i]);
      const rec = decodeReservation(str(items[i + 1]));
      if (!rec) continue;
      const event: RelayEvent = {
        t: nowMs,
        id,
        code: rec.hash.slice(0, 12),
        model: rec.model,
        status: 0,
        kind: "full",
        why: "expired",
        reserved: rec.amount,
        actual: rec.amount,
        in: 0,
        out: 0,
        rsn: 0,
      };
      const done = await this.settle({
        id,
        hash: rec.hash,
        model: rec.model,
        day: rec.day,
        window: rec.window,
        actual: rec.amount,
        promptTokens: 0,
        completionTokens: 0,
        reasoningTokens: 0,
        kind: "full",
        tokenCorrection: 0,
        event,
      });
      if (done.settled) swept += 1;
    }
    return swept;
  }

  // -- admin -------------------------------------------------------------------------------

  async addCode(code: NewCode): Promise<void> {
    if (num(await this.redis.exists(this.c(code.hash))) > 0) throw new Error("ledger: that code already exists");
    if (num(await this.redis.hsetnx(this.labels, code.label, code.hash)) !== 1) {
      throw new Error(`ledger: the label "${code.label}" is taken`);
    }
    const fields: Record<string, string> = {
      label: code.label,
      cap_total: String(code.capTotal),
      cap_daily: String(code.capDaily),
      freeze_pct: String(code.freezePct),
      revoked: "0",
      frozen: "0",
      created: String(code.createdAt),
      spent: "0",
      reserved: "0",
      requests: "0",
      inflight: "0",
    };
    if (code.sharePct !== undefined) fields.share_pct = String(code.sharePct);
    if (code.maxInFlight !== undefined) fields.max_inflight = String(code.maxInFlight);
    await this.redis.hset(this.c(code.hash), fields);
    await this.redis.sadd(this.codes, code.hash);
  }

  async updateCode(hash: string, patch: CodePatch): Promise<boolean> {
    if (num(await this.redis.exists(this.c(hash))) === 0) return false;
    const fields: Record<string, string> = {};
    if (patch.capTotal !== undefined) fields.cap_total = String(patch.capTotal);
    if (patch.capDaily !== undefined) fields.cap_daily = String(patch.capDaily);
    if (patch.freezePct !== undefined) fields.freeze_pct = String(patch.freezePct);
    if (patch.sharePct !== undefined) fields.share_pct = String(patch.sharePct);
    if (patch.maxInFlight !== undefined) fields.max_inflight = String(patch.maxInFlight);
    if (patch.revoked !== undefined) fields.revoked = patch.revoked ? "1" : "0";
    if (patch.frozen !== undefined) fields.frozen = patch.frozen ? "1" : "0";
    if (Object.keys(fields).length > 0) await this.redis.hset(this.c(hash), fields);
    return true;
  }

  async listCodes(day: string): Promise<CodeView[]> {
    const members = await this.redis.smembers(this.codes);
    const hashes = Array.isArray(members) ? members.map(str) : [];
    const rows = await Promise.all(
      hashes.map(async (hash) => {
        const [code, today] = await Promise.all([this.getCode(hash), this.getDay(hash, day)]);
        return code ? { ...code, today } : null;
      }),
    );
    return rows.filter((row): row is CodeView => row !== null).sort((a, b) => a.createdAt - b.createdAt);
  }

  async setGlobalCap(cap: number): Promise<void> {
    await this.redis.hset(this.g, { cap: String(cap) });
  }

  async setDisabled(disabled: boolean): Promise<void> {
    await this.redis.hset(this.g, { disabled: disabled ? "1" : "0" });
  }

  async setOpeningBalance(amount: number): Promise<{ previous: number }> {
    for (let attempt = 0; attempt < 5; attempt += 1) {
      const previous = (await this.getGlobal()).opening;
      const reply = await this.redis.eval(OPENING_LUA, [this.g], [String(previous), String(amount), String(amount - previous)]);
      if (Array.isArray(reply) && str(reply[0]) === "ok") return { previous };
    }
    throw new Error("ledger: the opening balance kept changing underneath; try again");
  }

  async report(options: { days: string[]; events: number; nowMs: number }): Promise<Report> {
    const [global, codes, open, expired, rawEvents, usage] = await Promise.all([
      this.getGlobal(),
      this.listCodes(utcDay(options.nowMs)),
      this.redis.hlen(this.res),
      this.redis.zcount(this.resq, "-inf", String(options.nowMs)),
      options.events > 0 ? this.redis.lrange(this.ev, 0, options.events - 1) : Promise.resolve([]),
      Promise.all(options.days.map((day) => this.redis.hgetall(this.u(day)))),
    ]);
    const days = options.days.map((day, index) => {
      const raw = usage[index] ?? {};
      const byModel = new Map<string, UsageRow>();
      for (const [field, value] of Object.entries(raw ?? {})) {
        const cut = field.lastIndexOf("|");
        if (cut < 0) continue;
        const model = field.slice(0, cut);
        const metric = field.slice(cut + 1);
        const row =
          byModel.get(model) ??
          { model, requests: 0, promptTokens: 0, completionTokens: 0, reasoningTokens: 0, cost: 0, usage: 0, full: 0, released: 0, reconcile: 0 };
        const n = num(value);
        if (metric === "n") row.requests = n;
        else if (metric === "in") row.promptTokens = n;
        else if (metric === "out") row.completionTokens = n;
        else if (metric === "rsn") row.reasoningTokens = n;
        else if (metric === "cost") row.cost = n;
        else if (metric === "usage") row.usage = n;
        else if (metric === "full") row.full = n;
        else if (metric === "released") row.released = n;
        else if (metric === "reconcile") row.reconcile = n;
        byModel.set(model, row);
      }
      return { day, models: [...byModel.values()].sort((a, b) => a.model.localeCompare(b.model)) };
    });
    const events = (Array.isArray(rawEvents) ? rawEvents : []).map((line) => JSON.parse(str(line)) as RelayEvent);
    return {
      nowMs: options.nowMs,
      global,
      codes,
      days,
      openReservations: num(open),
      expiredReservations: num(expired),
      events,
    };
  }
}
