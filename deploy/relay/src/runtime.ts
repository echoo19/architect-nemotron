/**
 * Builds the relay from environment variables (names only; values never logged):
 *
 *   NEBIUS_API_KEY              Token Factory key, sent upstream only
 *   RELAY_LEDGER                "file" (local default) or "upstash" (required on Vercel)
 *   RELAY_LEDGER_FILE           FileLedger path (local default: deploy/relay/.data/ledger.json)
 *   UPSTASH_REDIS_REST_URL      UpstashLedger endpoint
 *   UPSTASH_REDIS_REST_TOKEN    UpstashLedger token
 *   RELAY_KEY_PREFIX            Redis key prefix (default "relay")
 *   RELAY_DISABLED              1/true: kill switch, every /v1 call gets 503
 *   RELAY_MAX_CALL_SECONDS      per-call limit (Vercel: 1 to 290, default 280; local default none)
 *   RELAY_LIMITER_WAIT_SECONDS  longest wait for the per-model limiter (default 10)
 *   RELAY_UPSTREAM_BASE_URL     override Token Factory's base URL (tests, a regional host)
 *   RELAY_HELP_URL              linked from cap errors
 *   RELAY_CONFIG_JSON           a whole relay.config.json, inline (overrides the file)
 */

import { Redis } from "@upstash/redis";
import { parseConfig } from "./config.js";
import { type Fetch, type Relay, createRelay } from "./handler.js";
import { FileLedger } from "./ledger/file.js";
import type { Ledger } from "./ledger/types.js";
import { UpstashLedger, upstashStore } from "./ledger/upstash.js";

export type Env = Record<string, string | undefined>;

/** Vercel ends a function at 300 s; the relay must end and settle each call before that. */
export const VERCEL_MAX_CALL_SECONDS = 290;

export function truthy(value: string | undefined): boolean {
  return /^(1|true|yes|on)$/i.test((value ?? "").trim());
}

function seconds(value: string | undefined, fallback: number, what: string): number {
  if (value === undefined || value.trim() === "") return fallback;
  const n = Number(value);
  if (!Number.isFinite(n) || n < 0) throw new Error(`${what} must be a number of seconds`);
  return n;
}

export interface LedgerChoice {
  host: "local" | "vercel";
  /** FileLedger path when RELAY_LEDGER_FILE is not set. */
  defaultFile?: string;
}

/** The ledger the environment asks for. Throws with a readable reason when it cannot. */
export function ledgerFromEnv(env: Env, choice: LedgerChoice): Ledger {
  const asked = (env.RELAY_LEDGER ?? "").trim().toLowerCase();
  const kind = asked || (choice.host === "vercel" ? "upstash" : "file");
  if (kind === "upstash") {
    const url = env.UPSTASH_REDIS_REST_URL?.trim();
    const token = env.UPSTASH_REDIS_REST_TOKEN?.trim();
    if (!url || !token) throw new Error("UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN must both be set");
    const redis = new Redis({ url, token, automaticDeserialization: false, enableTelemetry: false });
    return new UpstashLedger(upstashStore(redis), { prefix: env.RELAY_KEY_PREFIX?.trim() || "relay" });
  }
  if (kind === "file") {
    if (choice.host === "vercel") {
      throw new Error("RELAY_LEDGER=file cannot hold caps across Vercel instances; use upstash");
    }
    // Two ledgers mean two global caps. With Upstash configured, the file ledger is used
    // only when asked for by name.
    if (!asked && (env.UPSTASH_REDIS_REST_URL?.trim() || env.UPSTASH_REDIS_REST_TOKEN?.trim())) {
      throw new Error(
        "UPSTASH_REDIS_REST_URL or UPSTASH_REDIS_REST_TOKEN is set, so the shared Upstash ledger may be meant. " +
          "Set RELAY_LEDGER=upstash to use it, or RELAY_LEDGER=file to keep this machine's own ledger, whose spend the Upstash caps never see",
      );
    }
    const file = env.RELAY_LEDGER_FILE?.trim() || choice.defaultFile;
    if (!file) throw new Error("RELAY_LEDGER_FILE is not set");
    return new FileLedger(file);
  }
  throw new Error(`RELAY_LEDGER must be "file" or "upstash", not "${kind}"`);
}

export interface RelayFromEnv {
  env: Env;
  host: "local" | "vercel";
  /** relay.config.json's text (RELAY_CONFIG_JSON wins when set). */
  configText: string;
  defaultLedgerFile?: string;
  fetch?: Fetch;
  waitUntil?: (promise: Promise<unknown>) => void;
  /** Default per-call limit when RELAY_MAX_CALL_SECONDS is unset (0 = none). */
  defaultMaxCallSeconds: number;
  log?: (entry: Record<string, unknown>) => void;
}

/**
 * Throws for a broken relay.config.json, and on Vercel for a per-call limit the platform
 * would cut short; a missing ledger or key becomes a 503.
 */
export function createRelayFromEnv(options: RelayFromEnv): Relay {
  const env = options.env;
  const config = parseConfig(env.RELAY_CONFIG_JSON?.trim() || options.configText);
  if (options.host === "vercel") config.maxBodyBytes = Math.min(config.maxBodyBytes, config.vercelMaxBodyBytes);
  const maxCallSeconds = seconds(env.RELAY_MAX_CALL_SECONDS, options.defaultMaxCallSeconds, "RELAY_MAX_CALL_SECONDS");
  if (options.host === "vercel" && !(maxCallSeconds >= 1 && maxCallSeconds <= VERCEL_MAX_CALL_SECONDS)) {
    throw new Error(
      `RELAY_MAX_CALL_SECONDS must be from 1 to ${VERCEL_MAX_CALL_SECONDS} on Vercel, so that each call ends and ` +
        `settles before the platform's 300 s limit (it is ${maxCallSeconds})`,
    );
  }
  let ledger: Ledger | null = null;
  let ledgerError: string | null = null;
  try {
    const choice: LedgerChoice = { host: options.host };
    if (options.defaultLedgerFile) choice.defaultFile = options.defaultLedgerFile;
    ledger = ledgerFromEnv(env, choice);
  } catch (err) {
    ledgerError = err instanceof Error ? err.message : String(err);
  }
  const maxCallMs = Math.round(maxCallSeconds * 1000);
  // An unsettled reservation is swept after the call could have ended, plus grace; with
  // no per-call limit (local) that is two hours.
  const reservationTtlMs = (maxCallMs > 0 ? maxCallMs : 2 * 3600_000) + config.reservationGraceSeconds * 1000;
  const fetchImpl: Fetch = options.fetch ?? ((url, init) => fetch(url, init));
  return createRelay({
    config,
    ledger,
    ledgerError,
    upstreamBaseUrl: (env.RELAY_UPSTREAM_BASE_URL?.trim() || config.upstreamBaseUrl).replace(/\/+$/, ""),
    upstreamKey: env.NEBIUS_API_KEY?.trim() || null,
    fetch: fetchImpl,
    host: options.host,
    disabled: truthy(env.RELAY_DISABLED),
    maxCallMs,
    limiterWaitMs: Math.round(seconds(env.RELAY_LIMITER_WAIT_SECONDS, 10, "RELAY_LIMITER_WAIT_SECONDS") * 1000),
    reservationTtlMs,
    helpUrl: env.RELAY_HELP_URL?.trim() || null,
    // On Vercel, /healthz is open to the internet and each ledger read is a billed Upstash
    // command; on this machine the operator wants to see a switch flip at once.
    healthCacheMs: options.host === "vercel" ? 30_000 : 0,
    ...(options.waitUntil ? { waitUntil: options.waitUntil } : {}),
    ...(options.log ? { log: options.log } : {}),
  });
}
