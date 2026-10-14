/** relay.config.json: the model allowlist, prices (dated), clamps and rate limits. */

import { perMillionToNanoPerToken } from "./money.js";

export interface ModelConfig {
  id: string;
  name: string;
  inputUsdPerM: string;
  outputUsdPerM: string;
  /** Whole nano-dollars per prompt / completion token. */
  inNano: number;
  outNano: number;
  /** max_tokens when the client sends none, and the most it may ask for. */
  maxTokensClamp: number;
  /** Token Factory's published limits for this model. */
  rpm: number;
  tpm: number;
  /** The relay's own per-minute budgets: limiter_fraction of the above. */
  limitRpm: number;
  limitTpm: number;
  contextLength: number | null;
}

export interface EstimateConfig {
  /** Bytes of request JSON per token for the cost reservation (1 = an upper bound). */
  bytesPerToken: number;
  /** Bytes per token for the rate limiter's guess (typical English is ~4). */
  likelyBytesPerToken: number;
  overheadTokens: number;
  perMessageTokens: number;
  perToolTokens: number;
  /**
   * Tokens per byte for the tools list and for tool-call arguments. The chat template
   * renders a tool schema's extra keys twice and re-serializes arguments, so these parts
   * can render to about twice their bytes.
   */
  toolTokensPerByte: number;
  /** Added for every object key inside the tools list (the tags the template wraps it in). */
  perToolKeyTokens: number;
  /** Added for every character outside the Basic Multilingual Plane inside the tools list. */
  perToolAstralCharTokens: number;
  /**
   * Added for every U+007F and every two-byte UTF-8 character (U+0080 to U+07FF) inside the
   * tools list, which Python's repr() can spell in 4 bytes for 1 or 6 bytes for 2.
   */
  perToolEscapeCharTokens: number;
  /** Added for every top-level tool-call argument (its <parameter=...> block). */
  perArgumentTokens: number;
  /** Added for every number inside string-form tool-call arguments (1e15 renders as 18 digits). */
  perArgumentNumberTokens: number;
}

export interface RelayConfig {
  priceDate: string;
  priceSource: string;
  upstreamBaseUrl: string;
  /** The share of Token Factory's published RPM and TPM the relay's limiter allows. */
  limiterFraction: number;
  /**
   * Whether prompt tokens Token Factory served from its cache count toward a model's
   * per-minute token budget. Off by default: the limiter counts the prompt tokens that
   * missed the cache plus the completion tokens.
   */
  limiterCountsCachedTokens: boolean;
  models: Map<string, ModelConfig>;
  serviceTiers: string[];
  estimate: EstimateConfig;
  /** Each code's share (percent) of a model's per-minute budget, unless the code sets its own. */
  codeSharePct: number;
  /** Calls one code may have running at once, unless the code sets its own. */
  codeMaxInFlight: number;
  /** A Token Factory 5xx that arrives within this long releases the hold; a later one is booked in full. */
  release5xxWithinMs: number;
  maxBodyBytes: number;
  /** The body limit on Vercel, where each call's possible overshoot is kept small. */
  vercelMaxBodyBytes: number;
  maxResponseBytes: number;
  reservationGraceSeconds: number;
}

export class ConfigError extends Error {}

/** limiter_fraction when the config leaves it out: 90% of the published limits. */
export const DEFAULT_LIMITER_FRACTION = 0.9;

function object(value: unknown, what: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new ConfigError(`${what} must be an object`);
  }
  return value as Record<string, unknown>;
}

function positiveInt(value: unknown, what: string): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 1) {
    throw new ConfigError(`${what} must be a positive integer`);
  }
  return value;
}

function wholeNumber(value: unknown, what: string): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) {
    throw new ConfigError(`${what} must be a whole number`);
  }
  return value;
}

function positiveNumber(value: unknown, what: string): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
    throw new ConfigError(`${what} must be a positive number`);
  }
  return value;
}

function text(value: unknown, what: string): string {
  if (typeof value !== "string" || !value.trim()) throw new ConfigError(`${what} must be a string`);
  return value.trim();
}

export function parseConfig(source: string | unknown): RelayConfig {
  let raw: unknown = source;
  if (typeof source === "string") {
    try {
      raw = JSON.parse(source);
    } catch {
      throw new ConfigError("relay config is not valid JSON");
    }
  }
  const root = object(raw, "relay config");
  const priceDate = text(root.price_date, "price_date");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(priceDate)) throw new ConfigError("price_date must be YYYY-MM-DD");
  const upstreamBaseUrl = text(root.upstream_base_url, "upstream_base_url").replace(/\/+$/, "");
  if (!/^https?:\/\/[^\s]+$/.test(upstreamBaseUrl)) throw new ConfigError("upstream_base_url must be an http(s) URL");
  const limiterFraction =
    root.limiter_fraction === undefined
      ? DEFAULT_LIMITER_FRACTION
      : positiveNumber(root.limiter_fraction, "limiter_fraction");
  if (limiterFraction > 1) throw new ConfigError("limiter_fraction must be at most 1");
  const countsCached = root.limiter_counts_cached_tokens ?? false;
  if (typeof countsCached !== "boolean") {
    throw new ConfigError("limiter_counts_cached_tokens must be true or false");
  }

  const models = new Map<string, ModelConfig>();
  for (const [id, entry] of Object.entries(object(root.models, "models"))) {
    if (!id.startsWith("nvidia/")) {
      throw new ConfigError(`models: "${id}" is not an nvidia/* model; the relay serves NVIDIA models only`);
    }
    const row = object(entry, `models["${id}"]`);
    const inputUsdPerM = text(row.input_usd_per_m, `${id}.input_usd_per_m`);
    const outputUsdPerM = text(row.output_usd_per_m, `${id}.output_usd_per_m`);
    let inNano: number;
    let outNano: number;
    try {
      inNano = perMillionToNanoPerToken(inputUsdPerM, `${id}.input_usd_per_m`);
      outNano = perMillionToNanoPerToken(outputUsdPerM, `${id}.output_usd_per_m`);
    } catch (err) {
      throw new ConfigError((err as Error).message);
    }
    if (inNano < 1 || outNano < 1) throw new ConfigError(`${id}: prices must be above zero`);
    const rpm = positiveInt(row.rpm, `${id}.rpm`);
    const tpm = positiveInt(row.tpm, `${id}.tpm`);
    const limitRpm = Math.floor(rpm * limiterFraction);
    const limitTpm = Math.floor(tpm * limiterFraction);
    if (limitRpm < 1 || limitTpm < 1) throw new ConfigError(`${id}: limiter budget rounds to zero`);
    models.set(id, {
      id,
      name: typeof row.name === "string" && row.name.trim() ? row.name.trim() : id,
      inputUsdPerM,
      outputUsdPerM,
      inNano,
      outNano,
      maxTokensClamp: positiveInt(row.max_tokens_clamp, `${id}.max_tokens_clamp`),
      rpm,
      tpm,
      limitRpm,
      limitTpm,
      contextLength: row.context_length === undefined ? null : positiveInt(row.context_length, `${id}.context_length`),
    });
  }
  if (models.size === 0) throw new ConfigError("models must list at least one nvidia/* model");

  const tiers = root.service_tiers === undefined ? [] : root.service_tiers;
  if (!Array.isArray(tiers) || !tiers.every((tier) => typeof tier === "string" && tier.trim())) {
    throw new ConfigError("service_tiers must be a list of strings");
  }
  const estimate = object(root.estimate, "estimate");
  const toolTokensPerByte = positiveNumber(estimate.tool_tokens_per_byte, "estimate.tool_tokens_per_byte");
  if (toolTokensPerByte < 1) throw new ConfigError("estimate.tool_tokens_per_byte must be at least 1");
  const codeSharePct = positiveInt(root.code_share_pct, "code_share_pct");
  if (codeSharePct > 100) throw new ConfigError("code_share_pct must be a percent from 1 to 100");
  return {
    priceDate,
    priceSource: typeof root.price_source === "string" ? root.price_source : "",
    upstreamBaseUrl,
    limiterFraction,
    limiterCountsCachedTokens: countsCached,
    models,
    serviceTiers: tiers.map((tier: string) => tier.trim()),
    estimate: {
      bytesPerToken: positiveNumber(estimate.bytes_per_token, "estimate.bytes_per_token"),
      likelyBytesPerToken: positiveNumber(estimate.likely_bytes_per_token, "estimate.likely_bytes_per_token"),
      overheadTokens: positiveInt(estimate.overhead_tokens, "estimate.overhead_tokens"),
      perMessageTokens: positiveInt(estimate.per_message_tokens, "estimate.per_message_tokens"),
      perToolTokens: positiveInt(estimate.per_tool_tokens, "estimate.per_tool_tokens"),
      toolTokensPerByte,
      perToolKeyTokens: wholeNumber(estimate.per_tool_key_tokens, "estimate.per_tool_key_tokens"),
      perToolAstralCharTokens: wholeNumber(estimate.per_tool_astral_char_tokens, "estimate.per_tool_astral_char_tokens"),
      perToolEscapeCharTokens: wholeNumber(estimate.per_tool_escape_char_tokens, "estimate.per_tool_escape_char_tokens"),
      perArgumentTokens: wholeNumber(estimate.per_tool_call_argument_tokens, "estimate.per_tool_call_argument_tokens"),
      perArgumentNumberTokens: wholeNumber(estimate.per_argument_number_tokens, "estimate.per_argument_number_tokens"),
    },
    codeSharePct,
    codeMaxInFlight: positiveInt(root.code_max_in_flight, "code_max_in_flight"),
    release5xxWithinMs: wholeNumber(root.release_5xx_within_ms, "release_5xx_within_ms"),
    maxBodyBytes: positiveInt(root.max_body_bytes, "max_body_bytes"),
    vercelMaxBodyBytes: positiveInt(root.vercel_max_body_bytes, "vercel_max_body_bytes"),
    maxResponseBytes: positiveInt(root.max_response_bytes, "max_response_bytes"),
    reservationGraceSeconds: positiveInt(root.reservation_grace_seconds, "reservation_grace_seconds"),
  };
}
