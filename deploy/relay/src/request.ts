/**
 * The chat request the relay forwards: a field allowlist, n forced to 1, max_tokens
 * defaulted and clamped per model, usage always requested on streams, and the token
 * estimate the reservation is priced from.
 */

import type { ErrorBody } from "./errors.js";
import type { EstimateConfig, ModelConfig, RelayConfig } from "./config.js";

/** Top-level fields the relay forwards (after checking each one). Everything else is dropped. */
export const ALLOWED_FIELDS = [
  "model",
  "messages",
  "tools",
  "tool_choice",
  "temperature",
  "top_p",
  "max_tokens",
  "stream",
  "stream_options",
  "response_format",
  "stop",
  "chat_template_kwargs",
  "service_tier",
] as const;

/** chat_template_kwargs keys that survive (the Nemotron thinking switch only). */
export const ALLOWED_TEMPLATE_KWARGS = ["enable_thinking"] as const;

/** Arrays and objects may nest this deep in a request body; deeper ones get a 400. */
export const MAX_JSON_DEPTH = 64;

const TOOL_CHOICES = new Set(["none", "auto", "required"]);
const RESPONSE_FORMATS = new Set(["text", "json_object", "json_schema"]);
const MAX_MESSAGES = 4096;
const MAX_TOOLS = 512;
const MAX_STOPS = 16;

export interface PreparedRequest {
  model: ModelConfig;
  /** The exact JSON body sent upstream, and its UTF-8 bytes. */
  payload: Uint8Array<ArrayBuffer>;
  stream: boolean;
  /** The client asked for the usage chunk itself; otherwise the relay strips it. */
  clientWantsUsage: boolean;
  maxTokens: number;
  /** Field names the relay dropped or overrode (names only, never values). */
  dropped: string[];
  /** Upper-bound prompt tokens (for the money reservation). */
  inputTokensUpper: number;
  /** Likely prompt tokens (for the rate limiter). */
  inputTokensLikely: number;
}

export type PrepareResult =
  | { ok: true; value: PreparedRequest }
  | { ok: false; status: 400 | 403; error: ErrorBody };

function bad(message: string, code = "invalid_request"): PrepareResult {
  return { ok: false, status: 400, error: { message, type: "invalid_request_error", code } };
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

const encoder = new TextEncoder();

/** True when no array or object in `value` sits more than `max` levels deep. Never recurses. */
export function withinDepth(value: unknown, max: number): boolean {
  const stack: [object, number][] = [];
  if (typeof value === "object" && value !== null) stack.push([value, 1]);
  for (let next = stack.pop(); next; next = stack.pop()) {
    const [item, depth] = next;
    if (depth > max) return false;
    for (const child of Array.isArray(item) ? item : Object.values(item)) {
      if (typeof child === "object" && child !== null) stack.push([child, depth + 1]);
    }
  }
  return true;
}

/** Object keys and number leaves anywhere inside `value`. Never recurses. */
function census(value: unknown): { keys: number; numbers: number } {
  let keys = 0;
  let numbers = 0;
  const stack: unknown[] = [value];
  while (stack.length > 0) {
    const item = stack.pop();
    if (typeof item === "number") numbers += 1;
    if (typeof item !== "object" || item === null) continue;
    if (Array.isArray(item)) {
      for (const child of item) stack.push(child);
    } else {
      const entries = Object.values(item);
      keys += entries.length;
      for (const child of entries) stack.push(child);
    }
  }
  return { keys, numbers };
}

/** The parts of a prompt that Nemotron's chat template can render larger than their bytes. */
export interface PromptShape {
  /** UTF-8 bytes of the tools list as sent. */
  toolBytes: number;
  /** Object keys anywhere inside the tools list. */
  toolKeys: number;
  /** Characters outside the Basic Multilingual Plane inside the tools list. */
  toolAstralChars: number;
  /**
   * U+007F and every two-byte UTF-8 character (U+0080 to U+07FF) inside the tools list:
   * Python's repr() can spell one as \x7f or \u0600, 4 bytes for 1 or 6 bytes for 2.
   */
  toolEscapeChars: number;
  /** UTF-8 bytes of every assistant tool call's arguments as sent. */
  argumentBytes: number;
  /** Top-level argument names: each becomes a <parameter=...> block. */
  argumentNames: number;
  /** Numbers inside string-form arguments, which are parsed and re-printed upstream. */
  argumentNumbers: number;
}

const NUMBER_LIKE = /-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g;

export function promptShape(tools: unknown, messages: unknown[]): PromptShape {
  const shape: PromptShape = {
    toolBytes: 0,
    toolKeys: 0,
    toolAstralChars: 0,
    toolEscapeChars: 0,
    argumentBytes: 0,
    argumentNames: 0,
    argumentNumbers: 0,
  };
  if (Array.isArray(tools) && tools.length > 0) {
    const text = JSON.stringify(tools);
    shape.toolBytes = encoder.encode(text).length;
    shape.toolKeys = census(tools).keys;
    // The template prints a parameter's "type" and "description" with Python's str(). For a
    // list or dict value that is repr(), which escapes every character Python cannot print:
    // U+007F as \x7f (4 bytes for 1), a two-byte one such as U+0600 as \u0600 (6 for 2),
    // one outside the BMP as \U000xxxxx (10 for 4). Everything else in a repr(), quotes and
    // separators included, stays within 2 bytes a request byte, and a token is never
    // shorter than a byte (byte-level BPE), so tools priced at 2 tokens a byte need more for
    // these characters only. Which ones Python cannot print depends on its Unicode version,
    // so every character in these ranges is counted.
    for (let i = 0; i < text.length; i += 1) {
      const unit = text.charCodeAt(i);
      if (unit >= 0xd800 && unit <= 0xdbff) shape.toolAstralChars += 1;
      else if (unit >= 0x7f && unit <= 0x7ff) shape.toolEscapeChars += 1;
    }
  }
  for (const message of messages) {
    if (!isObject(message) || message.role !== "assistant" || !Array.isArray(message.tool_calls)) continue;
    for (const call of message.tool_calls) {
      if (!isObject(call)) continue;
      const fn = isObject(call.function) ? call.function : call;
      const args = fn.arguments;
      if (args === undefined || args === null) continue;
      shape.argumentBytes += encoder.encode(JSON.stringify(args)).length;
      if (typeof args !== "string") {
        // A mapping travels as the relay printed it, so its numbers are already spelled out.
        if (isObject(args)) shape.argumentNames += Object.keys(args).length;
        continue;
      }
      let parsed: unknown;
      try {
        parsed = JSON.parse(args);
      } catch {
        // Python may still read it (NaN, Infinity): count generously from the text.
        shape.argumentNames += (args.match(/:/g) ?? []).length;
        shape.argumentNumbers += (args.match(NUMBER_LIKE) ?? []).length;
        continue;
      }
      if (isObject(parsed)) shape.argumentNames += Object.keys(parsed).length;
      shape.argumentNumbers += census(parsed).numbers;
    }
  }
  return shape;
}

export function prepareRequest(raw: unknown, config: RelayConfig): PrepareResult {
  if (!isObject(raw)) return bad("The request body must be a JSON object.");
  if (!withinDepth(raw, MAX_JSON_DEPTH)) return bad(`The request body nests deeper than ${MAX_JSON_DEPTH} levels.`, "too_deep");
  const has = (key: string) => Object.prototype.hasOwnProperty.call(raw, key) && raw[key] !== null && raw[key] !== undefined;

  if (typeof raw.model !== "string" || !raw.model.trim()) return bad("model is required (an nvidia/* model id).", "missing_model");
  const model = config.models.get(raw.model);
  if (!model) {
    return {
      ok: false,
      status: 403,
      error: {
        message:
          `Model "${raw.model.slice(0, 200)}" is not served by this relay. It serves NVIDIA Nemotron models only: ` +
          [...config.models.keys()].join(", ") + ".",
        type: "relay_model_not_allowed",
        code: "model_not_allowed",
        allowed_models: [...config.models.keys()],
      },
    };
  }

  const messages = raw.messages;
  if (!Array.isArray(messages) || messages.length === 0) return bad("messages must be a non-empty list.");
  if (messages.length > MAX_MESSAGES) return bad(`messages may hold at most ${MAX_MESSAGES} entries.`);
  if (!messages.every(isObject)) return bad("every message must be an object.");

  const dropped: string[] = [];
  const allowed = new Set<string>(ALLOWED_FIELDS);
  for (const key of Object.keys(raw)) {
    if (allowed.has(key) || raw[key] === null || raw[key] === undefined) continue;
    if (key === "n" && raw[key] === 1) continue; // n=1 is what is sent anyway
    dropped.push(key);
  }

  const body: Record<string, unknown> = { model: model.id, messages };

  if (has("tools")) {
    const tools = raw.tools;
    if (!Array.isArray(tools) || !tools.every(isObject)) return bad("tools must be a list of objects.");
    if (tools.length > MAX_TOOLS) return bad(`tools may hold at most ${MAX_TOOLS} entries.`);
    if (tools.length > 0) body.tools = tools;
  }
  if (has("tool_choice")) {
    const choice = raw.tool_choice;
    if (!(typeof choice === "string" && TOOL_CHOICES.has(choice)) && !isObject(choice)) {
      return bad('tool_choice must be "none", "auto", "required" or an object.');
    }
    body.tool_choice = choice;
  }
  if (has("temperature")) {
    const t = raw.temperature;
    if (typeof t !== "number" || !Number.isFinite(t) || t < 0 || t > 2) return bad("temperature must be a number from 0 to 2.");
    body.temperature = t;
  }
  if (has("top_p")) {
    const p = raw.top_p;
    if (typeof p !== "number" || !Number.isFinite(p) || p <= 0 || p > 1) return bad("top_p must be a number above 0 and at most 1.");
    body.top_p = p;
  }

  // max_tokens: the client's value (or its max_completion_tokens), defaulted and clamped.
  let asked: unknown = has("max_tokens") ? raw.max_tokens : undefined;
  if (asked === undefined && has("max_completion_tokens")) asked = raw.max_completion_tokens;
  let maxTokens = model.maxTokensClamp;
  if (asked !== undefined) {
    if (typeof asked !== "number" || !Number.isSafeInteger(asked) || asked < 1) {
      return bad("max_tokens must be a positive integer.");
    }
    maxTokens = Math.min(asked, model.maxTokensClamp);
  }
  body.max_tokens = maxTokens;
  body.n = 1;

  let stream = false;
  if (has("stream")) {
    if (typeof raw.stream !== "boolean") return bad("stream must be true or false.");
    stream = raw.stream;
  }
  if (stream) body.stream = true;
  let clientWantsUsage = false;
  if (has("stream_options")) {
    const options = raw.stream_options;
    if (!isObject(options)) return bad("stream_options must be an object.");
    if (!stream) {
      dropped.push("stream_options");
    } else {
      clientWantsUsage = options.include_usage === true;
      for (const key of Object.keys(options)) if (key !== "include_usage") dropped.push(`stream_options.${key}`);
    }
  }
  if (stream) body.stream_options = { include_usage: true };

  if (has("response_format")) {
    const format = raw.response_format;
    if (!isObject(format) || typeof format.type !== "string" || !RESPONSE_FORMATS.has(format.type)) {
      return bad('response_format must be an object whose type is "text", "json_object" or "json_schema".');
    }
    body.response_format = format;
  }
  if (has("stop")) {
    const stop = raw.stop;
    const ok =
      typeof stop === "string" ||
      (Array.isArray(stop) && stop.length <= MAX_STOPS && stop.every((s) => typeof s === "string"));
    if (!ok) return bad(`stop must be a string or a list of at most ${MAX_STOPS} strings.`);
    body.stop = stop;
  }
  if (has("chat_template_kwargs")) {
    const kwargs = raw.chat_template_kwargs;
    if (!isObject(kwargs)) return bad("chat_template_kwargs must be an object.");
    const kept: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(kwargs)) {
      if ((ALLOWED_TEMPLATE_KWARGS as readonly string[]).includes(key)) {
        if (typeof value !== "boolean") return bad(`chat_template_kwargs.${key} must be true or false.`);
        kept[key] = value;
      } else {
        dropped.push(`chat_template_kwargs.${key}`);
      }
    }
    if (Object.keys(kept).length > 0) body.chat_template_kwargs = kept;
  }
  if (has("service_tier")) {
    const tier = raw.service_tier;
    if (typeof tier === "string" && config.serviceTiers.includes(tier)) body.service_tier = tier;
    else dropped.push("service_tier");
  }

  let text: string;
  try {
    text = JSON.stringify(body);
  } catch {
    return bad("The request body could not be re-encoded.", "unencodable");
  }
  const payload = encoder.encode(text);
  const toolCount = Array.isArray(body.tools) ? body.tools.length : 0;
  const shape = promptShape(body.tools, messages);
  const { upper, likely } = estimateInputTokens(payload.length, messages.length, toolCount, config.estimate, shape);
  return {
    ok: true,
    value: {
      model,
      payload,
      stream,
      clientWantsUsage,
      maxTokens,
      dropped,
      inputTokensUpper: upper,
      inputTokensLikely: likely,
    },
  };
}

/**
 * Prompt tokens from the size of the JSON body. A byte-level tokenizer never emits more
 * tokens than the rendered prompt has bytes, and plain message text renders to no more
 * bytes than its JSON, so bytes_per_token = 1 plus the chat-template overhead bounds it.
 * The tools list and tool-call arguments are priced again on top (see PromptShape), since
 * the template prints schema keys twice, re-prints arguments with wider spacing and
 * numbers, and escapes characters Python cannot print inside list- or dict-valued schema
 * fields. The likely figure (about 4 bytes a token) feeds the rate limiter.
 */
export function estimateInputTokens(
  bytes: number,
  messages: number,
  tools: number,
  estimate: EstimateConfig,
  shape: Partial<PromptShape> = {},
): { upper: number; likely: number } {
  const overhead = estimate.overheadTokens + messages * estimate.perMessageTokens + tools * estimate.perToolTokens;
  const widened = (shape.toolBytes ?? 0) + (shape.argumentBytes ?? 0);
  const again = Math.max(0, estimate.toolTokensPerByte - 1 / estimate.bytesPerToken) * widened;
  const structure =
    (shape.toolKeys ?? 0) * estimate.perToolKeyTokens +
    (shape.toolAstralChars ?? 0) * estimate.perToolAstralCharTokens +
    (shape.toolEscapeChars ?? 0) * estimate.perToolEscapeCharTokens +
    (shape.argumentNames ?? 0) * estimate.perArgumentTokens +
    (shape.argumentNumbers ?? 0) * estimate.perArgumentNumberTokens;
  return {
    upper: Math.ceil(bytes / estimate.bytesPerToken) + overhead + Math.ceil(again) + structure,
    likely: Math.ceil(bytes / estimate.likelyBytesPerToken) + overhead,
  };
}

/** The money a call may cost at most: upper prompt tokens and max_tokens at list price. */
export function reservationNano(model: ModelConfig, inputTokens: number, maxTokens: number): number {
  return inputTokens * model.inNano + maxTokens * model.outNano;
}
