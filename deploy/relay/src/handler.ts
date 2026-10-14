/**
 * The relay: one Web-standard handler (Request in, Response out) for every host. Each chat
 * call is authenticated by access code, checked against the allowlist, priced at its worst
 * case and reserved atomically in the ledger before Token Factory sees it, then settled
 * from the usage Token Factory reports (or at the full reservation when it reports none).
 *
 * Routes: POST /v1/chat/completions, GET /v1/models, GET /v1/relay/balance, GET /healthz.
 */

import type { ModelConfig, RelayConfig } from "./config.js";
import { CODE_PATTERN, bearerToken, codeId, hashCode, newReservationId } from "./codes.js";
import { errorResponse, jsonResponse, type ErrorBody } from "./errors.js";
import { WINDOW_MS, waitMs, windowOf } from "./limiter.js";
import { formatUsd, nanoToUsdNumber, perMillionToPerTokenString } from "./money.js";
import { prepareRequest, reservationNano, type PreparedRequest } from "./request.js";
import { SseTap, sseErrorEvent } from "./sse.js";
import { costNano, readUsage, type Usage } from "./usage.js";
import type {
  CodeState,
  DayState,
  GlobalState,
  Ledger,
  RelayEvent,
  ReserveOutcome,
  ReserveRequest,
  SettleKind,
  Snapshot,
} from "./ledger/types.js";
import { MAX_SETTLE_NANO, MAX_SETTLE_TOKENS, utcDay } from "./ledger/types.js";

export type Fetch = (url: string, init: RequestInit) => Promise<Response>;

export interface RelayOptions {
  config: RelayConfig;
  ledger: Ledger | null;
  /** Why there is no ledger (a missing env var, for example); shown by /healthz. */
  ledgerError?: string | null;
  upstreamBaseUrl: string;
  /** Token Factory key (NEBIUS_API_KEY). Only ever sent upstream. */
  upstreamKey: string | null;
  fetch: Fetch;
  host: "local" | "vercel" | "test";
  /** The environment kill switch (RELAY_DISABLED). */
  disabled: boolean;
  /** Longest a call may run, start to finish; 0 = no limit (the local relay). */
  maxCallMs: number;
  /** Longest a call waits for the per-model limiter before getting a 429. */
  limiterWaitMs: number;
  /** After this long an unsettled reservation is swept at its full amount. */
  reservationTtlMs: number;
  /** Linked from cap errors (the judge page, for example). */
  helpUrl?: string | null;
  /** /healthz reads the ledger at most once per this many ms on one instance (default 30 s; 0 = every time). */
  healthCacheMs?: number;
  /** Unknown and revoked code hashes are answered from memory for this long (default 10 minutes). */
  badCodeTtlMs?: number;
  now?: () => number;
  sleep?: (ms: number, signal?: AbortSignal) => Promise<void>;
  /** Keeps settlement alive past the response on serverless hosts (Vercel waitUntil). */
  waitUntil?: (promise: Promise<unknown>) => void;
  log?: (entry: Record<string, unknown>) => void;
  newId?: () => string;
}

export interface Relay {
  handle(request: Request): Promise<Response>;
  readonly config: RelayConfig;
  readonly ledgerKind: "file" | "upstash" | null;
  /** Why there is no ledger, when there is none. */
  readonly ledgerError: string | null;
}

/** Who is calling, as far as a chat call needs to know before its reservation. */
interface Caller {
  hash: string;
  id: string;
}

class RelayAbort extends Error {
  constructor(readonly why: "client_closed" | "time_limit") {
    super(why);
  }
}

function abortReason(signal: AbortSignal): string | null {
  if (!signal.aborted) return null;
  return signal.reason instanceof RelayAbort ? signal.reason.why : "aborted";
}

const CONNECT_ERRORS = new Set(["ECONNREFUSED", "ENOTFOUND", "EAI_AGAIN", "UND_ERR_CONNECT_TIMEOUT"]);

/** True when fetch failed before the request could have reached Token Factory. */
function failedBeforeSending(err: unknown): boolean {
  let current: unknown = err;
  for (let depth = 0; depth < 4 && current; depth += 1) {
    const code = (current as { code?: unknown }).code;
    if (typeof code === "string" && CONNECT_ERRORS.has(code)) return true;
    current = (current as { cause?: unknown }).cause;
  }
  return false;
}

function defaultSleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const done = () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", done);
      resolve();
    };
    const timer = setTimeout(done, ms);
    signal?.addEventListener("abort", done, { once: true });
  });
}

function concat(chunks: Uint8Array[], total: number): Uint8Array<ArrayBuffer> {
  const out = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    out.set(chunk, offset);
    offset += chunk.length;
  }
  return out;
}

class TooLarge extends Error {}

async function readLimited(body: ReadableStream<Uint8Array> | null, max: number): Promise<Uint8Array<ArrayBuffer>> {
  if (!body) return new Uint8Array(0);
  const reader = body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.length;
    if (total > max) {
      await reader.cancel().catch(() => undefined);
      throw new TooLarge();
    }
    chunks.push(value);
  }
  return concat(chunks, total);
}

const decoder = new TextDecoder();

function routeOf(url: URL): string {
  let path = url.pathname.replace(/\/+$/, "") || "/";
  if (path === "/api/relay" || path.startsWith("/api/relay/")) {
    // A Vercel rewrite may hand over the original path as ?route=... (vercel.json sets it),
    // or only as the rewrite's own named parameter, ?path=...
    const named = url.searchParams.get("path");
    const forwarded = url.searchParams.get("route") ?? (named ? `v1/${named}` : null);
    path = forwarded ? "/" + forwarded.replace(/^\/+/, "").replace(/\/+$/, "") : path.slice("/api/relay".length) || "/";
  }
  return path;
}

function nextUtcMidnight(nowMs: number): string {
  const d = new Date(nowMs);
  return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() + 1)).toISOString();
}

/** Unknown and revoked code hashes, answered without the ledger for a while. Bounded in size. */
class BadCodes {
  private readonly seen = new Map<string, { until: number; why: "unknown_code" | "code_revoked" }>();

  constructor(
    private readonly ttlMs: number,
    private readonly max = 10_000,
  ) {}

  get(hash: string, at: number): "unknown_code" | "code_revoked" | null {
    const hit = this.seen.get(hash);
    if (!hit) return null;
    if (hit.until <= at) {
      this.seen.delete(hash);
      return null;
    }
    return hit.why;
  }

  add(hash: string, why: "unknown_code" | "code_revoked", at: number): void {
    if (this.ttlMs <= 0) return;
    this.seen.delete(hash);
    if (this.seen.size >= this.max) {
      const oldest = this.seen.keys().next();
      if (!oldest.done) this.seen.delete(oldest.value);
    }
    this.seen.set(hash, { until: at + this.ttlMs, why });
  }
}

const RETRY_IN_FLIGHT_S = 5;
const UNKNOWN_CODE: ErrorBody = { message: "That access code is not valid.", type: "relay_unknown_code", code: "unknown_code" };
const REVOKED_CODE: ErrorBody = { message: "That access code was revoked.", type: "relay_unknown_code", code: "code_revoked" };

export function createRelay(options: RelayOptions): Relay {
  const config = options.config;
  const limiterPercent = Math.round(config.limiterFraction * 100);
  const ledger = options.ledger;
  const now = options.now ?? (() => Date.now());
  const sleep = options.sleep ?? defaultSleep;
  const log = options.log ?? ((entry) => console.log(JSON.stringify(entry)));
  const newId = options.newId ?? newReservationId;
  const healthCacheMs = options.healthCacheMs ?? 30_000;
  const badCodes = new BadCodes(options.badCodeTtlMs ?? 600_000);
  const background = (promise: Promise<unknown>) => {
    const guarded = promise.catch((err) => log({ level: "error", msg: "background task failed", error: String(err) }));
    options.waitUntil?.(guarded);
    return guarded;
  };
  const help = options.helpUrl ? { help: options.helpUrl } : {};
  /** When the ledger last failed a call's command on this instance; /healthz reports it. */
  let lastLedgerFailureAt: number | null = null;
  const ledgerDown = (err: unknown): Response => {
    lastLedgerFailureAt = now();
    log({ level: "error", msg: "ledger unreachable", error: String(err) });
    return errorResponse(
      503,
      { message: "The relay's ledger cannot be reached right now; nothing was charged. Retry shortly.", type: "relay_ledger_unavailable" },
      { "retry-after": "5" },
    );
  };
  /** Token Factory's error bodies pass through; the relay's own key never does. */
  const scrub = (text: string): string =>
    options.upstreamKey && options.upstreamKey.length >= 8 ? text.split(options.upstreamKey).join("[key removed]") : text;

  // -- routes -----------------------------------------------------------------------------

  let healthRead: { at: number; result: Promise<{ status: number; body: Record<string, unknown> }> } | null = null;

  async function health(): Promise<Response> {
    const base = {
      host: options.host,
      ledger: ledger?.kind ?? null,
      price_date: config.priceDate,
      models: [...config.models.keys()],
    };
    const down = (reason: string, extra: Record<string, unknown> = {}) =>
      jsonResponse(503, { ok: false, ...base, reason, ...extra });
    if (options.disabled) return down("switched off (RELAY_DISABLED)", { disabled: true });
    if (!ledger) return down(`no ledger: ${options.ledgerError ?? "not configured"}`);
    if (!options.upstreamKey) return down("NEBIUS_API_KEY is not set");
    const at = now();
    // A database over its request limit refuses some commands and accepts others, so one
    // read that went through says little. Within healthCacheMs of a call whose ledger
    // command failed on this instance, the answer is 503 whatever the cached read says.
    const failedAt = lastLedgerFailureAt;
    if (failedAt !== null && at >= failedAt && at - failedAt < healthCacheMs) {
      return down("ledger refusing commands");
    }
    // One ledger read per instance per healthCacheMs, however often /healthz is probed.
    if (!healthRead || at - healthRead.at >= healthCacheMs || at < healthRead.at) {
      const result = (async () => {
        try {
          const global = await ledger.getGlobal();
          if (global.disabled) return { status: 503, body: { ok: false, ...base, reason: "switched off in the ledger", disabled: true } };
          if (global.cap <= 0) return { status: 503, body: { ok: false, ...base, reason: "the global cap is not set" } };
          return { status: 200, body: { ok: true, ...base, disabled: false } };
        } catch {
          return { status: 503, body: { ok: false, ...base, reason: "ledger unreachable" } };
        }
      })();
      healthRead = { at, result };
    }
    const { status, body } = await healthRead.result;
    return jsonResponse(status, body);
  }

  function models(): Response {
    const created = Math.floor(Date.parse(`${config.priceDate}T00:00:00Z`) / 1000);
    return jsonResponse(200, {
      object: "list",
      data: [...config.models.values()].map((m) => ({
        id: m.id,
        object: "model",
        created,
        owned_by: "nvidia",
        name: m.name,
        context_length: m.contextLength,
        architecture: { modality: "text->text" },
        pricing: { prompt: perMillionToPerTokenString(m.inputUsdPerM), completion: perMillionToPerTokenString(m.outputUsdPerM) },
        top_provider: { max_completion_tokens: m.maxTokensClamp, context_length: m.contextLength },
        relay: {
          input_usd_per_m: m.inputUsdPerM,
          output_usd_per_m: m.outputUsdPerM,
          price_date: config.priceDate,
          max_tokens_clamp: m.maxTokensClamp,
          rpm_budget: m.limitRpm,
          tpm_budget: m.limitTpm,
        },
      })),
    });
  }

  /** What the code itself has left. The shared budget's figures are the operator's, not the caller's. */
  function balance(code: CodeState, global: GlobalState, today: DayState, at: number): Response {
    const day = utcDay(at);
    const totalLeft = code.capTotal - code.spent - code.reserved;
    const dailyLeft = code.capDaily - today.spent - today.reserved;
    const [limitedBy, left] = dailyLeft < totalLeft ? ["daily_cap", dailyLeft] : ["total_cap", totalLeft];
    return jsonResponse(200, {
      code: code.id,
      label: code.label,
      remaining_usd: nanoToUsdNumber(Math.max(0, left)),
      limited_by: limitedBy,
      spent_usd: nanoToUsdNumber(code.spent),
      in_flight_usd: nanoToUsdNumber(code.reserved),
      cap_total_usd: nanoToUsdNumber(code.capTotal),
      requests: code.requests,
      calls_running: code.inFlight,
      max_calls_running: code.maxInFlight ?? config.codeMaxInFlight,
      share_of_model_budget_pct: code.sharePct ?? config.codeSharePct,
      frozen: code.frozen,
      today: {
        day,
        spent_usd: nanoToUsdNumber(today.spent),
        in_flight_usd: nanoToUsdNumber(today.reserved),
        cap_daily_usd: nanoToUsdNumber(code.capDaily),
        remaining_usd: nanoToUsdNumber(Math.max(0, dailyLeft)),
        resets_at: nextUtcMidnight(at),
      },
      relay_disabled: global.disabled,
      price_date: config.priceDate,
    });
  }

  function refusal(
    outcome: Exclude<ReserveOutcome, { ok: true } | { reason: "rate_limited" }>,
    amount: number,
    request: PreparedRequest,
    caller: Caller,
  ): Response {
    const needed = `$${formatUsd(amount)} (${request.model.name}, max_tokens ${request.maxTokens})`;
    switch (outcome.reason) {
      case "disabled":
        return errorResponse(503, { message: "The relay is switched off by its operator.", type: "relay_disabled", code: "disabled" });
      case "unknown_code":
        badCodes.add(caller.hash, "unknown_code", now());
        return errorResponse(401, UNKNOWN_CODE);
      case "revoked":
        badCodes.add(caller.hash, "code_revoked", now());
        return errorResponse(401, REVOKED_CODE);
      case "frozen":
        return errorResponse(403, {
          message: "This access code is paused: a call cost more than the relay held for it. The operator has to review it before it works again.",
          type: "relay_code_frozen",
          code: "code_frozen",
        });
      case "global_unset":
        return errorResponse(503, {
          message: "The relay's global cap is not set yet (operator: node admin.mjs global --cap <usd>).",
          type: "relay_misconfigured",
          code: "global_cap_unset",
        });
      case "in_flight":
        return errorResponse(
          429,
          {
            message: `This access code already has ${outcome.inFlight} calls running, its limit. Retry when one finishes.`,
            type: "relay_rate_limited",
            code: "in_flight",
          },
          { "retry-after": String(RETRY_IN_FLIGHT_S) },
        );
      default: {
        const left = Math.max(0, outcome.remaining);
        const leftText = `$${formatUsd(left)}`;
        const at = now();
        let message: string;
        const extra: Record<string, unknown> = {};
        if (outcome.reason === "cap_daily") {
          extra.cap_usd = nanoToUsdNumber(outcome.cap);
          extra.resets_at = nextUtcMidnight(at);
          message = `This access code has ${leftText} left today (daily cap $${formatUsd(outcome.cap)}); this call needs up to ${needed}. Lower max_tokens, or wait for the reset at ${extra.resets_at as string}.`;
        } else if (outcome.reason === "cap_total") {
          extra.cap_usd = nanoToUsdNumber(outcome.cap);
          message = `This access code has ${leftText} left of its $${formatUsd(outcome.cap)} total; this call needs up to ${needed}. Lower max_tokens, or ask the operator for more.`;
        } else if (outcome.reason === "global_cap") {
          message = `This access code has ${leftText} left, but the relay's shared budget cannot cover this call, which needs up to ${needed}. Lower max_tokens, or ask the operator.`;
        } else {
          message = `This access code has ${leftText} left, but the relay is keeping the rest of its shared budget for other codes, and this call needs up to ${needed}. Ask the operator.`;
        }
        const codes: Record<string, string> = {
          cap_total: "total_cap",
          cap_daily: "daily_cap",
          global_cap: "global_cap",
          global_freeze: "global_freeze",
        };
        return errorResponse(402, {
          message,
          type: "relay_cap_exceeded",
          code: codes[outcome.reason] ?? outcome.reason,
          remaining_usd: nanoToUsdNumber(left),
          needed_usd: nanoToUsdNumber(amount),
          ...extra,
          ...help,
        });
      }
    }
  }

  /** How long a call refused by a limiter would wait for room, if nothing else arrived. */
  function limiterWait(o: Extract<ReserveOutcome, { reason: "rate_limited" }>): number {
    if (o.scope === "code" && o.which === "tpm") {
      // A code with nothing running gets in once its own recent use is back within its share;
      // with calls running, only room in the window (or a finished call) lets it in.
      return o.inFlight === 0
        ? waitMs(o.cur, o.prev, o.elapsedMs, o.limit, 0, false)
        : waitMs(o.cur, o.prev, o.elapsedMs, o.limit, o.need, false);
    }
    return waitMs(o.cur, o.prev, o.elapsedMs, o.limit, o.need, o.which === "tpm");
  }

  /**
   * What a settled call adds to its model's per-minute token window: the completion and
   * the prompt tokens that missed Token Factory's cache (all prompt tokens when
   * limiter_counts_cached_tokens is set). Admission still holds the prompt estimate plus
   * max_tokens; settlement corrects the window to this.
   */
  function limiterTokens(usage: Usage): number {
    const prompt = config.limiterCountsCachedTokens ? usage.promptTokens : usage.promptTokens - usage.cachedTokens;
    return prompt + usage.billedOutputTokens;
  }

  /**
   * The most Token Factory can bill for one call of a model: its whole context window at the
   * input price plus the clamp at the output price (about $1.15 for Ultra, $0.086 for Super).
   */
  function mostBillable(model: ModelConfig): number {
    return (model.contextLength ?? 1_048_576) * model.inNano + model.maxTokensClamp * model.outNano;
  }

  function rateLimited(o: Extract<ReserveOutcome, { reason: "rate_limited" }>, wait: number, model: ModelConfig): Response {
    const fallback = o.scope === "code" ? RETRY_IN_FLIGHT_S : 60;
    const retry = Number.isFinite(wait) ? Math.min(120, Math.max(1, Math.ceil(wait / 1000))) : fallback;
    const what = o.which === "rpm" ? "request" : "token";
    const message =
      o.scope === "code"
        ? `This access code is at its share of ${model.name}'s per-minute ${what} budget. Retry in ${retry} s.`
        : `${model.name} is at the relay's per-minute ${what} budget (${limiterPercent}% of Token Factory's limit). Retry in ${retry} s.`;
    return errorResponse(
      429,
      { message, type: "relay_rate_limited", code: o.scope === "code" ? `code_${o.which}` : o.which },
      { "retry-after": String(retry) },
    );
  }

  // -- the chat call ------------------------------------------------------------------------

  async function chat(request: Request, caller: Caller): Promise<Response> {
    const started = now();
    const declared = Number(request.headers.get("content-length") ?? Number.NaN);
    if (Number.isFinite(declared) && declared > config.maxBodyBytes) {
      return errorResponse(413, { message: `The request body is over ${config.maxBodyBytes} bytes.`, type: "invalid_request_error", code: "body_too_large" });
    }
    let raw: unknown;
    try {
      raw = JSON.parse(decoder.decode(await readLimited(request.body, config.maxBodyBytes)));
    } catch (err) {
      if (err instanceof TooLarge) {
        return errorResponse(413, { message: `The request body is over ${config.maxBodyBytes} bytes.`, type: "invalid_request_error", code: "body_too_large" });
      }
      return errorResponse(400, { message: "The request body is not valid JSON.", type: "invalid_request_error", code: "invalid_json" });
    }
    const prepared = prepareRequest(raw, config);
    if (!prepared.ok) return errorResponse(prepared.status, prepared.error);
    const call = prepared.value;
    if (!options.upstreamKey) {
      return errorResponse(503, { message: "The relay has no Token Factory key (NEBIUS_API_KEY).", type: "relay_misconfigured", code: "no_upstream_key" });
    }
    const model: ModelConfig = call.model;
    const amount = reservationNano(model, call.inputTokensUpper, call.maxTokens);
    const rateTokens = call.inputTokensLikely + call.maxTokens;
    const id = newId();
    const headers = new Headers({ "x-relay-request": id, "x-relay-max-tokens": String(call.maxTokens) });
    if (call.dropped.length > 0) {
      const names = call.dropped.slice(0, 24).map((name) => (/^[A-Za-z0-9_.-]{1,64}$/.test(name) ? name : "?"));
      headers.set("x-relay-dropped", names.join(","));
    }

    // 1. Reserve the worst case, waiting briefly if a limiter is full. This is also where the
    // code is checked: the ledger refuses unknown, revoked and frozen codes in the same step.
    let slot: ReserveRequest | null = null;
    let waited = 0;
    for (;;) {
      const t = now();
      const attempt: ReserveRequest = {
        id,
        hash: caller.hash,
        model: model.id,
        day: utcDay(t),
        window: windowOf(t),
        elapsedMs: t % WINDOW_MS,
        amount,
        rateTokens,
        rpm: model.limitRpm,
        tpm: model.limitTpm,
        nowMs: t,
        deadlineMs: t + options.reservationTtlMs,
        defaultSharePct: config.codeSharePct,
        defaultMaxInFlight: config.codeMaxInFlight,
      };
      let outcome: ReserveOutcome;
      try {
        outcome = await ledger!.reserve(attempt);
      } catch (err) {
        // The script may have run even though its reply was lost. Release that hold, if
        // there is one, so the 503's "nothing was charged" stays true (settling is idempotent).
        background(releaseLost(attempt, caller));
        return ledgerDown(err);
      }
      // Holds past their deadline (an instance died mid-call, or its settlement was lost) are
      // swept by whichever call sees them, refused or not: when they fill a code's calls at
      // once, the code's own refused calls are the only ones that come by.
      if (outcome.expired) background(ledger!.sweep(t));
      if (outcome.ok) {
        slot = attempt;
        break;
      }
      if (outcome.reason === "rate_limited") {
        const wait = limiterWait(outcome);
        if (Number.isFinite(wait) && waited + wait <= options.limiterWaitMs && !request.signal.aborted) {
          const pause = Math.min(wait + Math.floor(Math.random() * 100), Math.max(1, options.limiterWaitMs - waited));
          await sleep(pause, request.signal);
          waited += pause;
          if (request.signal.aborted) {
            return errorResponse(499, { message: "The client went away.", type: "relay_client_closed" });
          }
          continue;
        }
        return rateLimited(outcome, wait, model);
      }
      return refusal(outcome, amount, call, caller);
    }
    if (waited > 0) headers.set("x-relay-wait-ms", String(waited));
    const held = slot as ReserveRequest;
    // Registered now, inside the request's own context, so a serverless host keeps the
    // function alive until this call is settled, even after the client has gone.
    let markSettled: () => void = () => undefined;
    options.waitUntil?.(new Promise<void>((resolve) => (markSettled = resolve)));

    // 2. Settlement: exactly once per call, from usage when Token Factory reported it.
    let settled: Promise<number> | null = null;
    let upstreamMs: number | undefined;
    let requestId: string | undefined;
    const settle = (
      asked: SettleKind,
      reported: Usage | null,
      status: number,
      note: { why?: string; completion?: string | null; reconcile?: boolean } = {},
    ) => {
      if (settled) return settled;
      let kind = asked;
      let usage = reported;
      let why = note.why;
      let actual = kind === "usage" && usage ? costNano(model, usage) : kind === "full" ? amount : 0;
      let tokenCorrection =
        kind === "usage" && usage ? limiterTokens(usage) - rateTokens : kind === "released" ? -rateTokens : 0;
      if (
        kind === "usage" &&
        usage &&
        !(
          Number.isSafeInteger(actual) &&
          actual <= MAX_SETTLE_NANO &&
          usage.promptTokens <= MAX_SETTLE_TOKENS &&
          usage.billedOutputTokens <= MAX_SETTLE_TOKENS &&
          usage.reasoningTokens <= MAX_SETTLE_TOKENS &&
          Math.abs(tokenCorrection) <= MAX_SETTLE_TOKENS
        )
      ) {
        // Figures no real call produces: book the hold instead of trusting them.
        kind = "full";
        usage = null;
        actual = amount;
        tokenCorrection = 0;
        why = "usage_out_of_range";
      }
      let reconcile = Boolean(note.reconcile);
      let claimed: number | undefined;
      const ceiling = Math.max(amount, mostBillable(model));
      if (kind === "usage" && actual > ceiling) {
        // A report above anything Token Factory can bill for this call (reasoning tokens far
        // above completion, say). Booking it in full would take the global cap with it and no
        // admin command could take it back, so the call is booked at the larger of its hold
        // and that ceiling, still never below what Token Factory can charge. The code is
        // frozen when that is above the hold, and the call is counted under "reconcile".
        claimed = actual;
        actual = ceiling;
        tokenCorrection = 0;
        why = "usage_above_model_max";
        reconcile = true;
      }
      const event: RelayEvent = {
        t: now(),
        id,
        code: caller.id,
        model: model.id,
        status,
        kind,
        reserved: amount,
        actual,
        in: usage?.promptTokens ?? 0,
        out: usage?.billedOutputTokens ?? 0,
        rsn: usage?.reasoningTokens ?? 0,
        stream: call.stream,
        ms: now() - started,
      };
      if (why) event.why = why;
      if (upstreamMs !== undefined) event.upstream_ms = upstreamMs;
      if (note.completion) event.completion = note.completion;
      if (requestId) event.request_id = requestId;
      if (reconcile) event.reconcile = true;
      if (claimed !== undefined) event.reported = claimed;
      if (actual > amount) {
        // The estimate missed. The ledger books the cost (up to mostBillable) and freezes the code.
        event.overshoot = actual - amount;
      }
      if (actual > amount || claimed !== undefined) {
        log({
          level: "warn",
          msg:
            claimed === undefined
              ? "reported usage passed the hold; the ledger froze the code until the operator unfreezes it"
              : "reported usage is more than Token Factory can bill for one call; booked the ceiling instead, to reconcile",
          id,
          code: caller.id,
          model: model.id,
          reserved_usd: formatUsd(amount),
          cost_usd: formatUsd(actual),
          frozen: actual > amount,
          ...(claimed === undefined ? {} : { reported_usd: formatUsd(claimed) }),
        });
      }
      const work = ledger!
        .settle({
          id,
          hash: caller.hash,
          model: model.id,
          day: held.day,
          window: held.window,
          actual,
          promptTokens: event.in,
          completionTokens: event.out,
          reasoningTokens: event.rsn,
          kind,
          tokenCorrection,
          reconcile,
          event,
        })
        .then(
          () => {
            log({ ...event, reserved_usd: formatUsd(amount), cost_usd: formatUsd(actual) });
            return actual;
          },
          (err) => {
            lastLedgerFailureAt = now();
            log({ level: "error", msg: "settle failed; a sweep will book the full reservation", id, error: String(err) });
            return amount;
          },
        )
        .finally(() => markSettled());
      options.waitUntil?.(work);
      settled = work;
      return work;
    };

    // 3. Token Factory.
    const upstreamAbort = new AbortController();
    const onClientGone = () => upstreamAbort.abort(new RelayAbort("client_closed"));
    if (request.signal.aborted) onClientGone();
    else request.signal.addEventListener("abort", onClientGone, { once: true });
    let timer: ReturnType<typeof setTimeout> | undefined;
    if (options.maxCallMs > 0) {
      const left = Math.max(1_000, options.maxCallMs - (now() - started));
      timer = setTimeout(() => upstreamAbort.abort(new RelayAbort("time_limit")), left);
    }
    const cleanup = () => {
      if (timer) clearTimeout(timer);
      timer = undefined;
      request.signal.removeEventListener("abort", onClientGone);
    };
    const timeLimitText = `The relay's per-call time limit (${Math.round(options.maxCallMs / 1000)} s) ended this call.`;

    // A client that left while its hold was being placed has sent nothing upstream yet.
    if (upstreamAbort.signal.aborted) {
      cleanup();
      await settle("released", null, 0, { why: `${abortReason(upstreamAbort.signal) ?? "aborted"}_before_send` });
      return errorResponse(499, { message: "The client went away before the call was sent; nothing was charged.", type: "relay_client_closed" }, Object.fromEntries(headers));
    }

    const sentAt = now();
    let upstream: Response;
    try {
      upstream = await options.fetch(`${options.upstreamBaseUrl}/chat/completions`, {
        method: "POST",
        headers: {
          authorization: `Bearer ${options.upstreamKey}`,
          "content-type": "application/json",
          accept: call.stream ? "text/event-stream" : "application/json",
        },
        body: call.payload,
        redirect: "error",
        signal: upstreamAbort.signal,
      });
    } catch (err) {
      cleanup();
      const reason = abortReason(upstreamAbort.signal);
      const early = reason === null && failedBeforeSending(err);
      await settle(early ? "released" : "full", null, 0, { why: reason ?? (early ? "connect_failed" : "fetch_failed") });
      if (reason === "time_limit") return errorResponse(504, { message: timeLimitText, type: "relay_timeout" }, Object.fromEntries(headers));
      return errorResponse(502, { message: "Token Factory could not be reached.", type: "upstream_unreachable" }, Object.fromEntries(headers));
    }
    upstreamMs = now() - sentAt;
    headers.set("x-upstream-ms", String(upstreamMs));
    upstream.headers.forEach((value, key) => {
      const name = key.toLowerCase();
      if (name === "retry-after" || name.startsWith("x-ratelimit-") || name === "x-request-id") headers.set(name, value);
    });
    const upstreamRequestId = upstream.headers.get("x-request-id");
    if (upstreamRequestId && /^[A-Za-z0-9._:-]{1,128}$/.test(upstreamRequestId)) requestId = upstreamRequestId;

    // 3a. Token Factory said no. A 4xx (other than 408) refuses before generating, and so
    // does a 5xx that comes back within seconds: the hold is released. A 408 or a slower
    // 5xx (a gateway timeout on a long call, a backend that failed mid-generation) may
    // follow tokens Token Factory bills, so the full hold is booked and marked for
    // reconciliation against the console.
    if (!upstream.ok) {
      let text = "";
      try {
        text = decoder.decode(await readLimited(upstream.body, 1024 * 1024));
      } catch {
        // keep the status; the body was unreadable or too large
      }
      cleanup();
      const status = upstream.status;
      const refusedEarly =
        (status >= 400 && status < 500 && status !== 408) || (status >= 500 && upstreamMs <= config.release5xxWithinMs);
      if (refusedEarly) await settle("released", null, status, { why: `upstream_${status}` });
      else await settle("full", null, status, { why: `upstream_${status}`, reconcile: true });
      const extra = Object.fromEntries(headers);
      if (status === 401 || status === 403) {
        return errorResponse(
          502,
          { message: "Token Factory refused the relay's own key; the operator has to fix it. Your code was not charged.", type: "upstream_auth", upstream_status: status },
          extra,
        );
      }
      if (status === 402) {
        return errorResponse(
          503,
          { message: "Token Factory reports the relay's account is out of funds. Your code was not charged.", type: "upstream_budget", upstream_status: 402 },
          extra,
        );
      }
      headers.set("content-type", upstream.headers.get("content-type") ?? "application/json");
      return new Response(scrub(text), { status, headers });
    }

    const contentType = upstream.headers.get("content-type") ?? "";
    // 3b. A whole JSON reply (non-streaming, or a server that would not stream).
    if (!call.stream || !contentType.includes("text/event-stream")) {
      let bytes: Uint8Array<ArrayBuffer>;
      try {
        bytes = await readLimited(upstream.body, config.maxResponseBytes);
      } catch (err) {
        cleanup();
        const reason = abortReason(upstreamAbort.signal);
        await settle("full", null, upstream.status, { why: reason ?? (err instanceof TooLarge ? "response_too_large" : "read_failed") });
        if (reason === "time_limit") return errorResponse(504, { message: timeLimitText, type: "relay_timeout" }, Object.fromEntries(headers));
        return errorResponse(502, { message: "Token Factory's reply broke off.", type: "upstream_broken" }, Object.fromEntries(headers));
      }
      cleanup();
      let usage: Usage | null = null;
      let completion: string | null = null;
      try {
        const parsed = JSON.parse(decoder.decode(bytes)) as Record<string, unknown>;
        usage = readUsage(parsed.usage);
        completion = typeof parsed.id === "string" ? parsed.id : null;
      } catch {
        // not JSON: pass it on, settle at the full reservation
      }
      const actual = await settle(usage ? "usage" : "full", usage, upstream.status, { why: usage ? undefined : "no_usage", completion });
      headers.set("content-type", contentType || "application/json");
      if (completion) headers.set("x-completion-id", completion);
      headers.set("x-relay-cost-usd", formatUsd(actual));
      return new Response(bytes, { status: upstream.status, headers });
    }

    // 3c. A stream: forwarded event by event; usage is read on the way through.
    const tap = new SseTap(!call.clientWantsUsage);
    const reader = upstream.body!.getReader();
    const finish = (why?: string) => {
      cleanup();
      const usage = readUsage(tap.usage);
      return usage
        ? settle("usage", usage, upstream.status, { why, completion: tap.completionId })
        : settle("full", null, upstream.status, { why: why ?? "no_usage", completion: tap.completionId });
    };
    headers.set("content-type", "text/event-stream; charset=utf-8");
    headers.set("cache-control", "no-cache");
    headers.set("x-accel-buffering", "no");
    const body = new ReadableStream<Uint8Array>({
      // Each pull must enqueue, close or error: a pull that returns empty-handed is not
      // called again, so upstream is read until a whole event is ready to forward.
      async pull(controller) {
        for (;;) {
          let chunk: ReadableStreamReadResult<Uint8Array>;
          try {
            chunk = await reader.read();
          } catch {
            const reason = abortReason(upstreamAbort.signal) ?? "stream_broken";
            await finish(reason);
            try {
              const rest = tap.end();
              if (rest) controller.enqueue(rest);
              if (reason !== "client_closed") {
                controller.enqueue(
                  reason === "time_limit"
                    ? sseErrorEvent(timeLimitText, "relay_timeout")
                    : sseErrorEvent("Token Factory's stream broke off before it finished.", "upstream_broken"),
                );
              }
              controller.close();
            } catch {
              // the client is gone
            }
            return;
          }
          if (chunk.done) {
            const rest = tap.end();
            try {
              if (rest) controller.enqueue(rest);
            } catch {
              // the client is gone; settle all the same
            }
            await finish();
            try {
              controller.close();
            } catch {
              // already cancelled
            }
            return;
          }
          const out = tap.push(chunk.value);
          if (out) {
            try {
              controller.enqueue(out);
            } catch {
              await finish("client_closed");
            }
            return;
          }
        }
      },
      async cancel() {
        upstreamAbort.abort(new RelayAbort("client_closed"));
        await reader.cancel().catch(() => undefined);
        await finish("client_closed");
      },
    });
    return new Response(body, { status: upstream.status, headers });
  }

  /** Releases the hold of a reservation whose reply never came back (a no-op if it never landed). */
  async function releaseLost(attempt: ReserveRequest, caller: Caller): Promise<void> {
    const event: RelayEvent = {
      t: now(),
      id: attempt.id,
      code: caller.id,
      model: attempt.model,
      status: 0,
      kind: "released",
      why: "reserve_reply_lost",
      reserved: attempt.amount,
      actual: 0,
      in: 0,
      out: 0,
      rsn: 0,
    };
    const done = await ledger!.settle({
      id: attempt.id,
      hash: attempt.hash,
      model: attempt.model,
      day: attempt.day,
      window: attempt.window,
      actual: 0,
      promptTokens: 0,
      completionTokens: 0,
      reasoningTokens: 0,
      kind: "released",
      tokenCorrection: -attempt.rateTokens,
      event,
    });
    if (done.settled) log({ level: "warn", msg: "released a hold whose reserve reply was lost", id: attempt.id, code: caller.id });
  }

  // -- the entry point ----------------------------------------------------------------------

  async function handle(request: Request): Promise<Response> {
    let url: URL;
    try {
      url = new URL(request.url);
    } catch {
      return errorResponse(400, { message: "Bad request URL.", type: "invalid_request_error" });
    }
    const route = routeOf(url);
    const method = request.method.toUpperCase();
    const known: Record<string, string> = {
      "/healthz": "GET",
      "/v1/models": "GET",
      "/v1/relay/balance": "GET",
      "/v1/chat/completions": "POST",
    };
    const allowed = known[route];
    if (!allowed) {
      return errorResponse(404, {
        message: "Not found. This relay serves POST /v1/chat/completions, GET /v1/models, GET /v1/relay/balance and GET /healthz.",
        type: "relay_not_found",
      });
    }
    if (method !== allowed && !(allowed === "GET" && method === "HEAD")) {
      return errorResponse(405, { message: `Use ${allowed} for ${route}.`, type: "invalid_request_error" }, { allow: allowed });
    }
    try {
      if (route === "/healthz") return await health();
      if (options.disabled) {
        return errorResponse(503, { message: "The relay is switched off by its operator.", type: "relay_disabled", code: "disabled" });
      }
      if (!ledger) {
        return errorResponse(503, { message: `The relay has no ledger: ${options.ledgerError ?? "not configured"}.`, type: "relay_misconfigured" });
      }
      const token = bearerToken(request.headers.get("authorization"));
      if (!token) {
        return errorResponse(401, { message: 'Send your access code as "Authorization: Bearer rc_...".', type: "relay_unknown_code", code: "missing_code" });
      }
      if (!CODE_PATTERN.test(token)) return errorResponse(401, UNKNOWN_CODE);
      const hash = await hashCode(token);
      const remembered = badCodes.get(hash, now());
      if (remembered === "unknown_code") return errorResponse(401, UNKNOWN_CODE);
      if (remembered === "code_revoked") return errorResponse(401, REVOKED_CODE);
      // A chat call goes straight to its reservation, which refuses unknown and revoked codes
      // itself: no separate lookup, so each call costs the ledger two requests in all (one
      // script to reserve, one to settle).
      if (route === "/v1/chat/completions") return await chat(request, { hash, id: codeId(hash) });
      // The GET routes read the code, the switches and the code's day in one request.
      const at = now();
      let view: Snapshot;
      try {
        view = await ledger.snapshot(hash, utcDay(at));
      } catch (err) {
        return ledgerDown(err);
      }
      const code = view.code;
      if (!code) {
        badCodes.add(hash, "unknown_code", now());
        return errorResponse(401, UNKNOWN_CODE);
      }
      if (code.revoked) {
        badCodes.add(hash, "code_revoked", now());
        return errorResponse(401, REVOKED_CODE);
      }
      if (view.global.disabled) {
        return errorResponse(503, { message: "The relay is switched off by its operator.", type: "relay_disabled", code: "disabled" });
      }
      if (route === "/v1/models") return models();
      return balance(code, view.global, view.today, at);
    } catch (err) {
      log({ level: "error", msg: "relay error", route, error: String(err) });
      const body: ErrorBody = { message: "The relay hit an internal error; nothing was charged beyond what the ledger shows.", type: "relay_error" };
      return errorResponse(500, body);
    }
  }

  return {
    handle,
    config,
    ledgerKind: ledger?.kind ?? null,
    ledgerError: ledger ? null : (options.ledgerError ?? "not configured"),
  };
}
