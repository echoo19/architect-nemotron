/** Shared test kit: the real relay.config.json, a fake Token Factory, ledgers, codes. */

import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { hashCode, newCode } from "../src/codes.js";
import { parseConfig } from "../src/config.js";
import { type Fetch, type Relay, type RelayOptions, createRelay } from "../src/handler.js";
import { FileLedger } from "../src/ledger/file.js";
import type { Ledger } from "../src/ledger/types.js";
import { usdToNano } from "../src/money.js";

export const CONFIG_TEXT = readFileSync(new URL("../relay.config.json", import.meta.url), "utf8");
export const config = parseConfig(CONFIG_TEXT);

export const NANO = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B";
export const LIGHTNING = "nvidia/Nemotron-3_5-Lightning";
export const SUPER = "nvidia/nemotron-3-super-120b-a12b";
export const ULTRA = "nvidia/Nemotron-3-Ultra-550b-a55b";
export const UPSTREAM_KEY = "test-upstream-key-not-real";

const dirs: string[] = [];

export function tempDir(label: string): string {
  const dir = mkdtempSync(path.join(tmpdir(), `relay-${label}-`));
  dirs.push(dir);
  return dir;
}

export function cleanupDirs(): void {
  for (const dir of dirs.splice(0)) rmSync(dir, { recursive: true, force: true });
}

export function fileLedger(label = "ledger"): FileLedger {
  return new FileLedger(path.join(tempDir(label), "ledger.json"));
}

export interface Minted {
  code: string;
  hash: string;
}

export async function mint(
  ledger: Ledger,
  options: { label?: string; total?: string; daily?: string; freezePct?: number } = {},
): Promise<Minted> {
  const code = newCode();
  const hash = await hashCode(code);
  const total = usdToNano(options.total ?? "5");
  await ledger.addCode({
    hash,
    label: options.label ?? `code-${hash.slice(0, 6)}`,
    capTotal: total,
    capDaily: options.daily === undefined ? total : usdToNano(options.daily),
    freezePct: options.freezePct ?? 100,
    createdAt: Date.now(),
  });
  return { code, hash };
}

// -- a fake Token Factory -----------------------------------------------------------------

export interface UpstreamCall {
  url: string;
  headers: Headers;
  body: Record<string, unknown>;
  signal: AbortSignal | null;
}

export type UpstreamHandler = (call: UpstreamCall) => Response | Promise<Response>;

export class FakeUpstream {
  calls: UpstreamCall[] = [];
  constructor(public handler: UpstreamHandler = () => jsonReply(completion(SUPER, usage(10, 5)))) {}

  fetch: Fetch = async (url, init) => {
    const raw = init.body;
    const text = raw instanceof Uint8Array ? new TextDecoder().decode(raw) : String(raw ?? "");
    const call: UpstreamCall = {
      url,
      headers: new Headers(init.headers as HeadersInit),
      body: JSON.parse(text) as Record<string, unknown>,
      signal: init.signal ?? null,
    };
    this.calls.push(call);
    if (call.signal?.aborted) throw new DOMException("aborted", "AbortError");
    return this.handler(call);
  };
}

export function usage(prompt: number, completionTokens: number, reasoning?: number, extra: Record<string, unknown> = {}) {
  return {
    prompt_tokens: prompt,
    completion_tokens: completionTokens,
    total_tokens: prompt + completionTokens,
    ...(reasoning === undefined ? {} : { completion_tokens_details: { reasoning_tokens: reasoning } }),
    ...extra,
  };
}

export function completion(model: string, used: unknown, id = "chatcmpl-test-1") {
  return {
    id,
    object: "chat.completion",
    created: 1791000000,
    model,
    choices: [{ index: 0, message: { role: "assistant", content: "hello" }, finish_reason: "stop" }],
    usage: used,
  };
}

export function jsonReply(body: unknown, init: { status?: number; headers?: Record<string, string> } = {}): Response {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { "content-type": "application/json", ...(init.headers ?? {}) },
  });
}

/** Stream chunks for a short reply, then (unless null) the usage-only chunk, then [DONE]. */
export function chunks(model: string, used: unknown | null, id = "chatcmpl-stream-1"): (object | string)[] {
  const base = { id, object: "chat.completion.chunk", created: 1791000000, model };
  const out: (object | string)[] = [
    { ...base, choices: [{ index: 0, delta: { role: "assistant", content: "" }, finish_reason: null }], usage: null },
    { ...base, choices: [{ index: 0, delta: { content: "Hel" }, finish_reason: null }], usage: null },
    { ...base, choices: [{ index: 0, delta: { content: "lo" }, finish_reason: "stop" }], usage: null },
  ];
  if (used !== null) out.push({ ...base, choices: [], usage: used });
  out.push("[DONE]");
  return out;
}

export interface SseOptions {
  headers?: Record<string, string>;
  /** Error the stream after this many events (a broken connection). */
  breakAfter?: number;
  /** Stop sending after this many events and wait until aborted. */
  hangAfter?: number;
  delayMs?: number;
  signal?: AbortSignal | null;
  status?: number;
}

export function sseReply(events: (object | string)[], options: SseOptions = {}): Response {
  const encoder = new TextEncoder();
  let index = 0;
  const signal = options.signal ?? null;
  const stream = new ReadableStream<Uint8Array>({
    async pull(controller) {
      if (signal?.aborted) {
        controller.error(new DOMException("aborted", "AbortError"));
        return;
      }
      if (options.breakAfter !== undefined && index >= options.breakAfter) {
        controller.error(new TypeError("terminated: other side closed"));
        return;
      }
      if (options.hangAfter !== undefined && index >= options.hangAfter) {
        await new Promise<void>((resolve) => {
          if (!signal) return;
          signal.addEventListener("abort", () => resolve(), { once: true });
        });
        controller.error(new DOMException("aborted", "AbortError"));
        return;
      }
      if (index >= events.length) {
        controller.close();
        return;
      }
      if (options.delayMs) await new Promise((r) => setTimeout(r, options.delayMs));
      const event = events[index++];
      const data = typeof event === "string" ? event : JSON.stringify(event);
      controller.enqueue(encoder.encode(`data: ${data}\n\n`));
    },
  });
  return new Response(stream, {
    status: options.status ?? 200,
    headers: { "content-type": "text/event-stream", ...(options.headers ?? {}) },
  });
}

// -- the relay under test -----------------------------------------------------------------

export function makeRelay(options: Partial<RelayOptions> & { ledger: Ledger | null; fetch: Fetch }): Relay {
  return createRelay({
    config,
    upstreamBaseUrl: "https://upstream.test/v1",
    upstreamKey: UPSTREAM_KEY,
    host: "test",
    disabled: false,
    maxCallMs: 0,
    limiterWaitMs: 0,
    reservationTtlMs: 600_000,
    log: () => undefined,
    ...options,
  });
}

export function chatRequest(
  code: string | null,
  body: unknown,
  init: { signal?: AbortSignal; headers?: Record<string, string> } = {},
): Request {
  const headers: Record<string, string> = { "content-type": "application/json", ...(init.headers ?? {}) };
  if (code !== null) headers.authorization = `Bearer ${code}`;
  return new Request("http://relay.test/v1/chat/completions", {
    method: "POST",
    headers,
    body: typeof body === "string" ? body : JSON.stringify(body),
    ...(init.signal ? { signal: init.signal } : {}),
  });
}

export function getRequest(pathname: string, code: string | null): Request {
  return new Request(`http://relay.test${pathname}`, {
    headers: code === null ? {} : { authorization: `Bearer ${code}` },
  });
}

export async function readAll(response: Response): Promise<string> {
  return await response.text();
}

export function sseData(text: string): unknown[] {
  return text
    .split(/\n\n/)
    .map((event) => event.split("\n").filter((l) => l.startsWith("data: ")).map((l) => l.slice(6)).join("\n"))
    .filter(Boolean)
    .map((data) => (data === "[DONE]" ? "[DONE]" : JSON.parse(data)));
}

export function waitFor(check: () => boolean, timeoutMs = 5_000): Promise<void> {
  const start = Date.now();
  return new Promise((resolve, reject) => {
    const tick = () => {
      if (check()) return resolve();
      if (Date.now() - start > timeoutMs) return reject(new Error("waitFor timed out"));
      setTimeout(tick, 5);
    };
    tick();
  });
}

export const messages = [{ role: "user", content: "Say hello." }];
