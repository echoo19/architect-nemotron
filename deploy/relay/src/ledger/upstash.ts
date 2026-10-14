/**
 * UpstashLedger: the shared ledger in Redis (Upstash over REST). Reserve, settle and the
 * expiry claim are single Lua scripts, so two relays (local and Vercel, or several Vercel
 * instances) can never both spend the same dollar.
 *
 * Upstash bills every command, including each one a script runs. Every record is therefore
 * one string (lua.ts has the layout): a reservation reads its three records with one MGET
 * and writes them with one MSET, so a model call costs 6 billed commands in 2 HTTP requests,
 * a refusal 2, /v1/models and /v1/relay/balance 1, and /healthz at most 1. Scripts run by
 * EVALSHA; the script itself is sent only when Upstash answers NOSCRIPT (the first call of
 * an instance after Upstash lost its script cache), and that EVAL loads it again.
 *
 * The store is anything with the RedisLike methods: @upstash/redis' Redis (created with
 * automaticDeserialization: false) in production, an ioredis-mock adapter in tests.
 */

import {
  ADD_CODE_LUA,
  CLAIM_LUA,
  CODE_LUA,
  GLOBAL_LUA,
  OPENING_LUA,
  RESERVE_LUA,
  SETTLE_LUA,
} from "./lua.js";
import {
  type CodePatch,
  type CodeState,
  type CodeView,
  type DayState,
  type GlobalState,
  type Ledger,
  type NewCode,
  type RelayEvent,
  type Report,
  type ReserveOutcome,
  type ReserveRequest,
  type SettleOutcome,
  type SettleRequest,
  type Snapshot,
  type UsageRow,
  EVENTS_KEPT,
  decodeReservation,
  utcDay,
} from "./types.js";

export interface RedisLike {
  evalsha(sha: string, keys: string[], args: string[]): Promise<unknown>;
  eval(script: string, keys: string[], args: string[]): Promise<unknown>;
  get(key: string): Promise<unknown>;
  mget(keys: string[]): Promise<unknown[]>;
  set(key: string, value: string): Promise<unknown>;
  smembers(key: string): Promise<unknown>;
  sadd(key: string, member: string): Promise<unknown>;
  hgetall(key: string): Promise<Record<string, unknown> | null>;
  hset(key: string, values: Record<string, string>): Promise<unknown>;
  exists(key: string): Promise<unknown>;
  lrange(key: string, start: number, stop: number): Promise<unknown>;
  /** Members and scores of a sorted set, as a flat [member, score, ...] list. */
  zrangeWithScores(key: string): Promise<unknown>;
  /** One SCAN step: the next cursor and the keys matching `match`. */
  scan(cursor: string, match: string, count: number): Promise<[string, string[]]>;
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
  evalsha(sha: string, keys: string[], args: string[]): Promise<unknown>;
  eval(script: string, keys: string[], args: string[]): Promise<unknown>;
  get(key: string): Promise<unknown>;
  mget(...keys: string[]): Promise<unknown>;
  set(key: string, value: string): Promise<unknown>;
  smembers(key: string): Promise<unknown>;
  sadd(key: string, member: string): Promise<unknown>;
  hgetall(key: string): Promise<unknown>;
  hset(key: string, values: Record<string, string>): Promise<unknown>;
  exists(...keys: string[]): Promise<unknown>;
  lrange(key: string, start: number, stop: number): Promise<unknown>;
  zrange(key: string, min: number, max: number, options: { withScores: true }): Promise<unknown>;
  scan(cursor: string | number, options: { match: string; count: number }): Promise<unknown>;
}

/**
 * @upstash/redis' client as a RedisLike. Create the client with
 * automaticDeserialization: false so stored strings come back as written; with that
 * setting HGETALL returns Redis' flat field/value list, which this turns into an object.
 */
export function upstashStore(client: UpstashClient): RedisLike {
  return {
    evalsha: (sha, keys, args) => client.evalsha(sha, keys, args),
    eval: (script, keys, args) => client.eval(script, keys, args),
    get: (key) => client.get(key),
    mget: async (keys) => {
      const reply = await client.mget(...keys);
      return Array.isArray(reply) ? reply : [];
    },
    set: (key, value) => client.set(key, value),
    smembers: (key) => client.smembers(key),
    sadd: (key, member) => client.sadd(key, member),
    hgetall: async (key) => asHash(await client.hgetall(key)),
    hset: (key, values) => client.hset(key, values),
    exists: (key) => client.exists(key),
    lrange: (key, start, stop) => client.lrange(key, start, stop),
    zrangeWithScores: (key) => client.zrange(key, 0, -1, { withScores: true }),
    scan: async (cursor, match, count) => {
      const reply = (await client.scan(cursor, { match, count })) as [unknown, unknown];
      return [String(reply[0]), Array.isArray(reply[1]) ? reply[1].map(String) : []];
    },
  };
}

// -- scripts ------------------------------------------------------------------------------

const shas = new Map<string, Promise<string>>();

/** A script's SHA-1, as EVALSHA names it (Web Crypto, so it runs on any host). */
export function scriptSha(source: string): Promise<string> {
  let sha = shas.get(source);
  if (!sha) {
    sha = crypto.subtle.digest("SHA-1", new TextEncoder().encode(source)).then((digest) => {
      let out = "";
      for (const byte of new Uint8Array(digest)) out += byte.toString(16).padStart(2, "0");
      return out;
    });
    shas.set(source, sha);
  }
  return sha;
}

function noScript(err: unknown): boolean {
  return /NOSCRIPT/i.test(err instanceof Error ? err.message : String(err));
}

// -- records ------------------------------------------------------------------------------

function num(value: unknown): number {
  const n = typeof value === "number" ? value : Number(value ?? 0);
  return Number.isFinite(n) ? n : 0;
}

function str(value: unknown): string {
  return typeof value === "string" ? value : value === null || value === undefined ? "" : String(value);
}

/** A stored record, or null when the key is missing. */
function record(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

/** A number field that may be unset ("-": the relay's default applies then). */
function optional(token: string | undefined): number | null {
  if (token === undefined || token === "" || token === "-") return null;
  const n = Number(token);
  return Number.isFinite(n) ? n : null;
}

/** A whole number as a record token, written exactly as Lua's '%.0f' writes it. */
function whole(value: number, what: string): string {
  if (!Number.isSafeInteger(value)) throw new Error(`ledger: ${what} must be a whole number (${value})`);
  return String(value);
}

/** Text that goes into a record as one token: ids, model ids, hashes and days. */
function token(value: string, what: string): string {
  if (!/^[^|\s]{1,200}$/.test(value)) throw new Error(`ledger: ${what} cannot be stored (${JSON.stringify(value)})`);
  return value;
}

function decodeLabel(text: string): string {
  try {
    return decodeURIComponent(text);
  } catch {
    return text;
  }
}

interface GlobalRecord extends GlobalState {
  /** The deadline of each call holding a reservation (the scripts read the rest). */
  held: number[];
}

/** The fields of a limiter list starting at `i`; returns the index after it. */
function skipLimiter(t: string[], i: number): number {
  return i + 1 + 4 * num(t[i]);
}

function parseGlobal(raw: string | null): GlobalRecord {
  if (raw === null) return { cap: 0, spent: 0, reserved: 0, disabled: false, opening: 0, held: [] };
  const t = raw.split("|");
  if (t[0] !== "2") throw new Error("ledger: unreadable global record");
  let i = skipLimiter(t, 6);
  const count = num(t[i]);
  i += 1;
  const held: number[] = [];
  for (let k = 0; k < count; k += 1, i += 9) held.push(num(t[i + 1]));
  return { cap: num(t[1]), spent: num(t[2]), reserved: num(t[3]), disabled: t[4] === "1", opening: num(t[5]), held };
}

function globalState(g: GlobalRecord): GlobalState {
  return { cap: g.cap, spent: g.spent, reserved: g.reserved, disabled: g.disabled, opening: g.opening };
}

function parseCode(hash: string, raw: string | null): CodeState | null {
  if (raw === null) return null;
  const t = raw.split("|");
  if (t[0] !== "2") throw new Error("ledger: unreadable code record");
  return {
    hash,
    id: hash.slice(0, 12),
    label: decodeLabel(str(t[13])),
    capTotal: num(t[1]),
    capDaily: num(t[2]),
    freezePct: optional(t[3]) ?? 100,
    sharePct: optional(t[4]),
    maxInFlight: optional(t[5]),
    revoked: t[6] === "1",
    frozen: t[7] === "1",
    createdAt: num(t[8]),
    spent: num(t[9]),
    reserved: num(t[10]),
    requests: num(t[11]),
    inFlight: Math.max(0, num(t[12])),
  };
}

/** A new code's record, as CODE_LUA and the reserve and settle scripts read it. */
export function codeRecord(code: NewCode & { spent?: number; reserved?: number; requests?: number; inFlight?: number; revoked?: boolean; frozen?: boolean }): string {
  return [
    "2",
    whole(code.capTotal, "capTotal"),
    whole(code.capDaily, "capDaily"),
    whole(code.freezePct, "freezePct"),
    code.sharePct === undefined ? "-" : whole(code.sharePct, "sharePct"),
    code.maxInFlight === undefined ? "-" : whole(code.maxInFlight, "maxInFlight"),
    code.revoked ? "1" : "0",
    code.frozen ? "1" : "0",
    whole(code.createdAt, "createdAt"),
    whole(code.spent ?? 0, "spent"),
    whole(code.reserved ?? 0, "reserved"),
    whole(code.requests ?? 0, "requests"),
    whole(code.inFlight ?? 0, "inFlight"),
    encodeURIComponent(code.label),
    "0",
  ].join("|");
}

/** A global record with no limiter windows, as the scripts read it. */
export function globalRecord(g: GlobalState, held: { id: string; deadline: number; record: string }[] = []): string {
  const out = ["2", whole(g.cap, "cap"), whole(g.spent, "spent"), whole(g.reserved, "reserved"), g.disabled ? "1" : "0", whole(g.opening, "opening"), "0"];
  out.push(String(held.length));
  for (const h of held) {
    const rec = decodeReservation(h.record);
    if (!rec) throw new Error(`ledger: unreadable reservation ${h.id}`);
    out.push(
      token(h.id, "reservation id"),
      whole(h.deadline, "deadline"),
      token(rec.hash, "code hash"),
      token(rec.day, "day"),
      token(rec.model, "model"),
      whole(rec.amount, "amount"),
      whole(rec.rateTokens, "rateTokens"),
      whole(rec.window, "window"),
      whole(rec.createdAt, "createdAt"),
    );
  }
  return out.join("|");
}

function parseDay(raw: string | null): DayState {
  if (raw === null) return { spent: 0, reserved: 0, requests: 0 };
  const t = raw.split("|");
  return { spent: num(t[0]), reserved: num(t[1]), requests: num(t[2]) };
}

export function dayRecord(d: DayState): string {
  return [whole(d.spent, "spent"), whole(d.reserved, "reserved"), whole(d.requests, "requests")].join("|");
}

const USAGE_FIELDS = ["n", "in", "out", "rsn", "cost", "usage", "full", "released", "reconcile"] as const;

function parseUsage(raw: string | null): UsageRow[] {
  if (raw === null) return [];
  const t = raw.split("|");
  const count = num(t[0]);
  const rows: UsageRow[] = [];
  for (let k = 0, i = 1; k < count; k += 1, i += 1 + USAGE_FIELDS.length) {
    const v = (field: number) => num(t[i + 1 + field]);
    rows.push({
      model: str(t[i]),
      requests: v(0),
      promptTokens: v(1),
      completionTokens: v(2),
      reasoningTokens: v(3),
      cost: v(4),
      usage: v(5),
      full: v(6),
      released: v(7),
      reconcile: v(8),
    });
  }
  return rows.sort((a, b) => a.model.localeCompare(b.model));
}

/** Usage rows (old hash fields "<model>|<metric>" -> value) as a usage record. */
export function usageRecord(fields: Record<string, unknown>): string {
  const byModel = new Map<string, Record<string, number>>();
  for (const [field, value] of Object.entries(fields)) {
    const cut = field.lastIndexOf("|");
    if (cut < 0) continue;
    const model = field.slice(0, cut);
    const row = byModel.get(model) ?? {};
    row[field.slice(cut + 1)] = num(value);
    byModel.set(model, row);
  }
  const out = [String(byModel.size)];
  for (const [model, row] of byModel) {
    out.push(token(model, "model"), ...USAGE_FIELDS.map((name) => whole(row[name] ?? 0, name)));
  }
  return out.join("|");
}

// -- the ledger -----------------------------------------------------------------------------

export class UpstashLedger implements Ledger {
  readonly kind = "upstash" as const;
  readonly prefix: string;
  /** Settlement events kept, newest first (EVENTS_KEPT unless a test asks for fewer). */
  readonly eventsKept: number;

  constructor(
    private readonly redis: RedisLike,
    options: { prefix?: string; eventsKept?: number } = {},
  ) {
    this.prefix = options.prefix ?? "relay";
    if (!/^[A-Za-z0-9_.:-]{1,64}$/.test(this.prefix)) throw new Error("ledger: bad key prefix");
    this.eventsKept = options.eventsKept ?? EVENTS_KEPT;
    if (!Number.isSafeInteger(this.eventsKept) || this.eventsKept < 1) throw new Error("ledger: eventsKept must be at least 1");
  }

  // -- keys ------------------------------------------------------------------------------

  private get g() {
    return `${this.prefix}:v2:g`;
  }
  private c(hash: string) {
    return `${this.prefix}:v2:c:${hash}`;
  }
  private cd(hash: string, day: string) {
    return `${this.prefix}:v2:cd:${hash}:${day}`;
  }
  private u(day: string) {
    return `${this.prefix}:v2:u:${day}`;
  }
  private get ev() {
    return `${this.prefix}:v2:ev`;
  }
  private get codes() {
    return `${this.prefix}:v2:codes`;
  }
  private get labels() {
    return `${this.prefix}:v2:labels`;
  }

  /** EVALSHA, and EVAL (which loads the script again) only when Upstash answers NOSCRIPT. */
  private async run(source: string, keys: string[], args: string[]): Promise<unknown> {
    const sha = await scriptSha(source);
    try {
      return await this.redis.evalsha(sha, keys, args);
    } catch (err) {
      if (!noScript(err)) throw err;
      return await this.redis.eval(source, keys, args);
    }
  }

  // -- reads (one command each) --------------------------------------------------------------

  async getCode(hash: string): Promise<CodeState | null> {
    return parseCode(hash, record(await this.redis.get(this.c(hash))));
  }

  async getGlobal(): Promise<GlobalState> {
    return globalState(parseGlobal(record(await this.redis.get(this.g))));
  }

  async getDay(hash: string, day: string): Promise<DayState> {
    return parseDay(record(await this.redis.get(this.cd(hash, day))));
  }

  async snapshot(hash: string, day: string): Promise<Snapshot> {
    const [g, c, d] = await this.redis.mget([this.g, this.c(hash), this.cd(hash, day)]);
    return { global: globalState(parseGlobal(record(g))), code: parseCode(hash, record(c)), today: parseDay(record(d)) };
  }

  // -- reserve / settle / sweep -----------------------------------------------------------

  async reserve(r: ReserveRequest): Promise<ReserveOutcome> {
    const reply = await this.run(
      RESERVE_LUA,
      [this.g, this.c(token(r.hash, "code hash")), this.cd(r.hash, token(r.day, "day"))],
      [
        token(r.id, "reservation id"),
        r.day,
        token(r.model, "model"),
        whole(r.amount, "amount"),
        whole(r.rateTokens, "rateTokens"),
        whole(r.rpm, "rpm"),
        whole(r.tpm, "tpm"),
        whole(r.elapsedMs, "elapsedMs"),
        whole(r.window, "window"),
        whole(r.nowMs, "nowMs"),
        whole(r.deadlineMs, "deadlineMs"),
        String(r.defaultSharePct),
        String(r.defaultMaxInFlight),
        r.hash,
      ],
    );
    if (!Array.isArray(reply) || reply.length === 0) throw new Error("ledger: unexpected reserve reply");
    const [tag, ...rest] = reply as unknown[];
    // A refusal's last figure: holds past their deadline, for the caller to sweep.
    const overdue = (at: number) => {
      const expired = num(rest[at]);
      return expired > 0 ? { expired } : {};
    };
    switch (str(tag)) {
      case "ok":
        return { ok: true, expired: num(rest[0]) };
      case "disabled":
      case "unknown_code":
      case "revoked":
      case "frozen":
      case "global_unset":
        return { ok: false, reason: str(tag) as "disabled" | "unknown_code" | "revoked" | "frozen" | "global_unset", ...overdue(0) };
      case "cap_total":
      case "cap_daily":
        return { ok: false, reason: str(tag) as "cap_total" | "cap_daily", remaining: num(rest[0]), cap: num(rest[1]), ...overdue(2) };
      case "global_cap":
      case "global_freeze":
        return { ok: false, reason: str(tag) as "global_cap" | "global_freeze", remaining: num(rest[0]), ...overdue(1) };
      case "in_flight":
        return { ok: false, reason: "in_flight", inFlight: num(rest[0]), limit: num(rest[1]), ...overdue(2) };
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
          ...overdue(8),
        };
      default:
        throw new Error(`ledger: unexpected reserve reply ${str(tag)}`);
    }
  }

  async settle(s: SettleRequest): Promise<SettleOutcome> {
    const reply = await this.run(
      SETTLE_LUA,
      [this.g, this.c(token(s.hash, "code hash")), this.cd(s.hash, token(s.day, "day")), this.u(s.day), this.ev],
      [
        token(s.id, "reservation id"),
        this.prefix,
        String(s.actual),
        String(s.promptTokens),
        String(s.completionTokens),
        String(s.reasoningTokens),
        s.kind,
        String(s.tokenCorrection),
        JSON.stringify(s.event),
        String(this.eventsKept),
        s.reconcile ? "1" : "0",
        token(s.model, "model"),
        String(s.window),
      ],
    );
    return { settled: num(reply) === 1 };
  }

  async sweep(nowMs: number, limit = 100, cutoffMs = nowMs): Promise<number> {
    const reply = await this.run(CLAIM_LUA, [this.g], [String(cutoffMs), String(limit), String(nowMs + 60_000)]);
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
    const reply = str(
      await this.run(ADD_CODE_LUA, [this.c(token(code.hash, "code hash")), this.labels, this.codes], [code.hash, code.label, codeRecord(code)]),
    );
    if (reply === "exists") throw new Error("ledger: that code already exists");
    if (reply === "taken") throw new Error(`ledger: the label "${code.label}" is taken`);
    if (reply !== "ok") throw new Error(`ledger: unexpected reply ${reply}`);
  }

  async updateCode(hash: string, patch: CodePatch): Promise<boolean> {
    const field = (value: number | undefined, what: string) => (value === undefined ? "" : whole(value, what));
    const flag = (value: boolean | undefined) => (value === undefined ? "" : value ? "1" : "0");
    const reply = await this.run(
      CODE_LUA,
      [this.c(token(hash, "code hash"))],
      [
        field(patch.capTotal, "capTotal"),
        field(patch.capDaily, "capDaily"),
        field(patch.freezePct, "freezePct"),
        field(patch.sharePct, "sharePct"),
        field(patch.maxInFlight, "maxInFlight"),
        flag(patch.revoked),
        flag(patch.frozen),
      ],
    );
    return num(reply) === 1;
  }

  async listCodes(day: string): Promise<CodeView[]> {
    const members = await this.redis.smembers(this.codes);
    const hashes = Array.isArray(members) ? members.map(str) : [];
    if (hashes.length === 0) return [];
    const values = await this.redis.mget([...hashes.map((h) => this.c(h)), ...hashes.map((h) => this.cd(h, day))]);
    const rows: CodeView[] = [];
    hashes.forEach((hash, i) => {
      const code = parseCode(hash, record(values[i]));
      if (code) rows.push({ ...code, today: parseDay(record(values[hashes.length + i])) });
    });
    return rows.sort((a, b) => a.createdAt - b.createdAt);
  }

  async setGlobalCap(cap: number): Promise<void> {
    await this.run(GLOBAL_LUA, [this.g], [whole(cap, "cap"), ""]);
  }

  async setDisabled(disabled: boolean): Promise<void> {
    await this.run(GLOBAL_LUA, [this.g], ["", disabled ? "1" : "0"]);
  }

  async setOpeningBalance(amount: number): Promise<{ previous: number }> {
    for (let attempt = 0; attempt < 5; attempt += 1) {
      const previous = (await this.getGlobal()).opening;
      const reply = await this.run(OPENING_LUA, [this.g], [whole(previous, "opening"), whole(amount, "amount"), whole(amount - previous, "delta")]);
      if (Array.isArray(reply) && str(reply[0]) === "ok") return { previous };
    }
    throw new Error("ledger: the opening balance kept changing underneath; try again");
  }

  /** Copies a ledger in the first layout under this prefix into this one (src/ledger/migrate.ts). */
  async migrateFromV1(options: import("./migrate.js").MigrationOptions = {}): Promise<import("./migrate.js").MigrationSummary> {
    const { migrateFromV1 } = await import("./migrate.js");
    return migrateFromV1(this.redis, this.prefix, options);
  }

  async report(options: { days: string[]; events: number; nowMs: number }): Promise<Report> {
    const wantEvents = options.events > 0;
    const keys = [this.g, ...(wantEvents ? [this.ev] : []), ...options.days.map((day) => this.u(day))];
    const [values, codes] = await Promise.all([this.redis.mget(keys), this.listCodes(utcDay(options.nowMs))]);
    const g = parseGlobal(record(values[0]));
    const ring = wantEvents ? (record(values[1]) ?? "") : "";
    const firstDay = wantEvents ? 2 : 1;
    const days = options.days.map((day, index) => ({ day, models: parseUsage(record(values[firstDay + index])) }));
    const events = ring
      .split("\n")
      .filter(Boolean)
      .slice(0, Math.max(0, options.events))
      .map((line) => JSON.parse(line) as RelayEvent);
    return {
      nowMs: options.nowMs,
      global: globalState(g),
      codes,
      days,
      openReservations: g.held.length,
      expiredReservations: g.held.filter((deadline) => deadline <= options.nowMs).length,
      events,
    };
  }
}
