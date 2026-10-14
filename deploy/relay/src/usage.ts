/** The usage Token Factory reports, and what a call costs from it. */

import type { ModelConfig } from "./config.js";

export interface Usage {
  promptTokens: number;
  completionTokens: number;
  reasoningTokens: number;
  /** Output tokens the ledger bills: completion, never less than reasoning implies. */
  billedOutputTokens: number;
  /**
   * Prompt tokens Token Factory served from its prompt cache, at most promptTokens. Only
   * the limiter reads them; the ledger bills every prompt token at the full input price.
   */
  cachedTokens: number;
}

function count(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
}

/**
 * Reads an OpenAI-style usage object. Token Factory counts reasoning inside
 * completion_tokens (verified for Super, Ultra and Lightning), so completion is billed
 * once. If a report could only be consistent with reasoning counted outside completion
 * (total above prompt + completion, or reasoning above completion), the larger reading
 * is billed: the ledger may over-count, never under-count.
 */
export function readUsage(value: unknown): Usage | null {
  if (typeof value !== "object" || value === null) return null;
  const usage = value as Record<string, unknown>;
  const prompt = count(usage.prompt_tokens);
  const completion = count(usage.completion_tokens);
  if (prompt === null || completion === null) return null;
  const details = usage.completion_tokens_details;
  const reasoning =
    typeof details === "object" && details !== null
      ? (count((details as Record<string, unknown>).reasoning_tokens) ?? 0)
      : 0;
  let billed = completion;
  const total = count(usage.total_tokens);
  if (total !== null && total - prompt > billed) billed = total - prompt;
  if (reasoning > billed) billed = completion + reasoning;
  return {
    promptTokens: prompt,
    completionTokens: completion,
    reasoningTokens: reasoning,
    billedOutputTokens: billed,
    cachedTokens: cachedOf(usage, prompt),
  };
}

/**
 * Cache hits: prompt_tokens_details.cached_tokens, or Token Factory's
 * prompt_cache_hit_tokens (the only form some of its models report). A malformed count
 * is 0, and none is more than the prompt itself.
 */
function cachedOf(usage: Record<string, unknown>, prompt: number): number {
  const details = usage.prompt_tokens_details;
  const detailed =
    typeof details === "object" && details !== null
      ? count((details as Record<string, unknown>).cached_tokens)
      : null;
  const hits = detailed || count(usage.prompt_cache_hit_tokens) || 0;
  return Math.min(hits, prompt);
}

/** Cost in nano-dollars at list price. Cached prompt tokens are billed at the full input rate. */
export function costNano(model: ModelConfig, usage: Usage): number {
  return usage.promptTokens * model.inNano + usage.billedOutputTokens * model.outNano;
}
