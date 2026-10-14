/**
 * The spend ledger. All money is integer nano-dollars. One interface, two stores:
 * UpstashLedger (Redis over REST, atomic Lua; shared by every host) and FileLedger (a
 * locked JSON file, for the local relay and tests). Both pass test/ledger-contract.ts.
 */

/** How a call was settled: from reported usage, at its full reservation, or released. */
export type SettleKind = "usage" | "full" | "released";

export interface CodeState {
  hash: string;
  id: string;
  label: string;
  /** Lifetime cap for this code. */
  capTotal: number;
  /** Cap per UTC day. */
  capDaily: number;
  /** The code stops once the whole relay has spent this percent of the global cap. */
  freezePct: number;
  /** Percent of each model's per-minute budget this code may use; null = the relay's default. */
  sharePct: number | null;
  /** Calls this code may have running at once; null = the relay's default. */
  maxInFlight: number | null;
  revoked: boolean;
  /** A call's reported usage passed its hold; refused until the operator unfreezes it. */
  frozen: boolean;
  createdAt: number;
  spent: number;
  reserved: number;
  requests: number;
  /** Calls holding a reservation right now. */
  inFlight: number;
}

export interface DayState {
  spent: number;
  reserved: number;
  requests: number;
}

export interface GlobalState {
  /** The lifetime cap over every code; 0 = not set (everything is refused). */
  cap: number;
  spent: number;
  reserved: number;
  disabled: boolean;
  /** Spend carried over from another ledger (admin opening-balance); already part of `spent`. */
  opening: number;
}

export interface ReserveRequest {
  id: string;
  hash: string;
  model: string;
  /** UTC day (YYYY-MM-DD) the daily cap is charged to. */
  day: string;
  /** Limiter minute (floor(now / 60 s)) and ms into it. */
  window: number;
  elapsedMs: number;
  amount: number;
  rateTokens: number;
  /** The relay's per-minute budgets for this model. */
  rpm: number;
  tpm: number;
  nowMs: number;
  /** After this, a sweep may settle the reservation at its full amount. */
  deadlineMs: number;
  /** For codes that set none: percent of the model's budget, and calls running at once. */
  defaultSharePct: number;
  defaultMaxInFlight: number;
}

export type CapReason = "cap_total" | "cap_daily" | "global_cap" | "global_freeze";

/**
 * `expired` counts the reservations (of any code) past their deadline when the reserve ran.
 * The caller sweeps them when it is above zero, after a refusal as well: holds left by a dead
 * instance can fill a code's calls at once, and its own refused calls must clear them. A
 * refusal leaves it out when there are none.
 */
export type ReserveOutcome =
  | { ok: true; expired: number }
  | { ok: false; reason: "disabled" | "unknown_code" | "revoked" | "frozen" | "global_unset"; expired?: number }
  | { ok: false; reason: "cap_total" | "cap_daily"; remaining: number; cap: number; expired?: number }
  /** The shared budget binds. `remaining` is the code's own (total and daily), never the global figure. */
  | { ok: false; reason: "global_cap" | "global_freeze"; remaining: number; expired?: number }
  | { ok: false; reason: "in_flight"; inFlight: number; limit: number; expired?: number }
  | {
      ok: false;
      reason: "rate_limited";
      /** The model's budget (shared by every code) or this code's share of it. */
      scope: "model" | "code";
      which: "rpm" | "tpm";
      cur: number;
      prev: number;
      elapsedMs: number;
      limit: number;
      need: number;
      inFlight: number;
      expired?: number;
    };

export interface RelayEvent {
  /** ms since epoch, at settlement. */
  t: number;
  /** Reservation id. */
  id: string;
  /** Code id (hash prefix), never the code. */
  code: string;
  model: string;
  status: number;
  kind: SettleKind;
  why?: string;
  reserved: number;
  actual: number;
  in: number;
  out: number;
  rsn: number;
  stream?: boolean;
  ms?: number;
  upstream_ms?: number;
  completion?: string;
  /** Token Factory's x-request-id, for reconciliation. */
  request_id?: string;
  /** Nano-dollars booked above the hold; the ledger froze the code. */
  overshoot?: number;
  /**
   * Nano-dollars the reported usage came to when that was more than Token Factory can bill
   * for one call of the model; `actual` is what was booked instead.
   */
  reported?: number;
  /** Booked in full after a Token Factory error that may have followed generation. */
  reconcile?: boolean;
}

export interface SettleRequest {
  id: string;
  /** The reservation's own keys: code hash, model, day and limiter minute. */
  hash: string;
  model: string;
  day: string;
  window: number;
  actual: number;
  promptTokens: number;
  completionTokens: number;
  reasoningTokens: number;
  kind: SettleKind;
  /** Added to the admission minute's token count (actual minus the limiter guess). */
  tokenCorrection: number;
  /** Count this call among those to check against Token Factory's console. */
  reconcile?: boolean;
  event: RelayEvent;
}

export interface SettleOutcome {
  /** False when the reservation was already settled (or swept). */
  settled: boolean;
}

export interface NewCode {
  hash: string;
  label: string;
  capTotal: number;
  capDaily: number;
  freezePct: number;
  createdAt: number;
  sharePct?: number;
  maxInFlight?: number;
}

export interface CodePatch {
  capTotal?: number;
  capDaily?: number;
  freezePct?: number;
  sharePct?: number;
  maxInFlight?: number;
  revoked?: boolean;
  frozen?: boolean;
}

export interface CodeView extends CodeState {
  today: DayState;
}

export interface UsageRow {
  model: string;
  requests: number;
  promptTokens: number;
  completionTokens: number;
  reasoningTokens: number;
  cost: number;
  /** Requests settled from reported usage / at the full reservation / released. */
  usage: number;
  full: number;
  released: number;
  /** Requests booked in full after a Token Factory error, to check against its console. */
  reconcile: number;
}

export interface Report {
  nowMs: number;
  global: GlobalState;
  codes: CodeView[];
  days: { day: string; models: UsageRow[] }[];
  openReservations: number;
  expiredReservations: number;
  events: RelayEvent[];
}

/** One code as the GET routes see it: the code (null if unknown), the shared switches and its day. */
export interface Snapshot {
  global: GlobalState;
  code: CodeState | null;
  today: DayState;
}

export interface Ledger {
  readonly kind: "file" | "upstash";
  getCode(hash: string): Promise<CodeState | null>;
  getGlobal(): Promise<GlobalState>;
  getDay(hash: string, day: string): Promise<DayState>;
  /** getGlobal, getCode and getDay together (one command on Upstash). */
  snapshot(hash: string, day: string): Promise<Snapshot>;
  /** Atomically checks caps, calls in flight and the limiters, and holds `amount` for one call. */
  reserve(request: ReserveRequest): Promise<ReserveOutcome>;
  /** Releases the hold and books the actual cost; idempotent per reservation id. */
  settle(request: SettleRequest): Promise<SettleOutcome>;
  /**
   * Settles reservations whose deadline is at or before `cutoffMs` (default `nowMs`) at
   * their full amount; returns how many.
   */
  sweep(nowMs: number, limit?: number, cutoffMs?: number): Promise<number>;
  /** Fails if the label is already taken. */
  addCode(code: NewCode): Promise<void>;
  /** False if no such code. */
  updateCode(hash: string, patch: CodePatch): Promise<boolean>;
  listCodes(day: string): Promise<CodeView[]>;
  setGlobalCap(cap: number): Promise<void>;
  setDisabled(disabled: boolean): Promise<void>;
  /** Sets the spend carried over from another ledger; `spent` moves by the difference. */
  setOpeningBalance(amount: number): Promise<{ previous: number }>;
  report(options: { days: string[]; events: number; nowMs: number }): Promise<Report>;
}

/**
 * Settlement events the Upstash ledger keeps, newest first. Each settlement rewrites them in
 * the same command as its figures, so the number is kept small; `admin.mjs report --events`
 * reads at most this many. The file ledger keeps every event.
 */
export const EVENTS_KEPT = 1_000;

/** Settlement figures past these are refused before anything is booked (the hold stays for a sweep). */
export const MAX_SETTLE_NANO = 1e15;
export const MAX_SETTLE_TOKENS = 1e10;

export function settleFiguresInRange(s: SettleRequest): boolean {
  const whole = (value: number, lo: number, hi: number) => Number.isSafeInteger(value) && value >= lo && value <= hi;
  return (
    whole(s.actual, 0, MAX_SETTLE_NANO) &&
    whole(s.promptTokens, 0, MAX_SETTLE_TOKENS) &&
    whole(s.completionTokens, 0, MAX_SETTLE_TOKENS) &&
    whole(s.reasoningTokens, 0, MAX_SETTLE_TOKENS) &&
    whole(s.tokenCorrection, -MAX_SETTLE_TOKENS, MAX_SETTLE_TOKENS) &&
    (s.kind === "usage" || s.kind === "full" || s.kind === "released")
  );
}

export function utcDay(nowMs: number): string {
  return new Date(nowMs).toISOString().slice(0, 10);
}

/** The reservation record both stores keep: hash|day|model|amount|rateTokens|window|created. */
export function encodeReservation(r: ReserveRequest): string {
  return [r.hash, r.day, r.model, r.amount, r.rateTokens, r.window, r.nowMs].join("|");
}

export interface ReservationRecord {
  hash: string;
  day: string;
  model: string;
  amount: number;
  rateTokens: number;
  window: number;
  createdAt: number;
}

export function decodeReservation(text: string): ReservationRecord | null {
  const parts = text.split("|");
  if (parts.length !== 7) return null;
  const [hash, day, model, amount, rate, window, created] = parts as [string, string, string, string, string, string, string];
  return {
    hash,
    day,
    model,
    amount: Number(amount),
    rateTokens: Number(rate),
    window: Number(window),
    createdAt: Number(created),
  };
}
