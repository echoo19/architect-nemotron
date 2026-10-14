import { describe, expect, it } from "vitest";
import { CODE_PATTERN, bearerToken, codeId, hashCode, newCode } from "../src/codes.js";
import { ConfigError, DEFAULT_LIMITER_FRACTION, parseConfig } from "../src/config.js";
import { WINDOW_MS, admits, codeAdmitsTokens, shareOf, waitMs } from "../src/limiter.js";
import { formatUsd, perMillionToNanoPerToken, perMillionToPerTokenString, usdToNano } from "../src/money.js";
import { ledgerFromEnv } from "../src/runtime.js";
import { SseTap } from "../src/sse.js";
import { costNano, readUsage } from "../src/usage.js";
import { CONFIG_TEXT, LIGHTNING, NANO, SUPER, ULTRA, config } from "./helpers.js";

describe("money", () => {
  it("parses dollars and per-1M prices exactly", () => {
    expect(usdToNano("26")).toBe(26_000_000_000);
    expect(usdToNano("0.001")).toBe(1_000_000);
    expect(usdToNano("3.50")).toBe(3_500_000_000);
    expect(perMillionToNanoPerToken("0.06")).toBe(60);
    expect(perMillionToNanoPerToken("0.24")).toBe(240);
    expect(perMillionToNanoPerToken("3.00")).toBe(3000);
    expect(() => perMillionToNanoPerToken("0.0625")).toThrow(/decimals/);
    expect(() => usdToNano("1e3")).toThrow();
    expect(() => usdToNano("-1")).toThrow();
  });

  it("formats without rounding", () => {
    expect(formatUsd(42_840)).toBe("0.00004284");
    expect(formatUsd(3_000_000_000)).toBe("3.00");
    expect(formatUsd(0)).toBe("0.00");
    expect(perMillionToPerTokenString("0.06")).toBe("0.00000006");
    expect(perMillionToPerTokenString("1.00")).toBe("0.000001");
    expect(perMillionToPerTokenString("0.90")).toBe("0.0000009");
  });
});

describe("config", () => {
  it("loads the dated price list for the four Nemotron models", () => {
    expect(config.priceDate).toBe("2026-10-03");
    expect([...config.models.keys()]).toEqual([NANO, LIGHTNING, SUPER, ULTRA]);
    // The limiter allows 90% of each published limit.
    const want: Record<string, [number, number, number, number, number]> = {
      [NANO]: [60, 240, 8192, 90, 720_000],
      [LIGHTNING]: [60, 240, 8192, 540, 360_000],
      [SUPER]: [300, 900, 8192, 270, 180_000],
      [ULTRA]: [1000, 3000, 32768, 270, 180_000],
    };
    for (const [id, [inNano, outNano, clamp, rpm, tpm]] of Object.entries(want)) {
      const m = config.models.get(id)!;
      expect([m.inNano, m.outNano, m.maxTokensClamp, m.limitRpm, m.limitTpm]).toEqual([inNano, outNano, clamp, rpm, tpm]);
    }
  });

  it("limits to 90% of the published limits unless the config says otherwise", () => {
    expect(config.limiterFraction).toBe(0.9);
    expect(config.limiterCountsCachedTokens).toBe(false);
    const raw = JSON.parse(CONFIG_TEXT);
    delete raw.limiter_fraction;
    delete raw.limiter_counts_cached_tokens;
    const fallback = parseConfig(raw);
    expect(fallback.limiterFraction).toBe(DEFAULT_LIMITER_FRACTION);
    expect(fallback.limiterCountsCachedTokens).toBe(false);
    expect(fallback.models.get(ULTRA)!.limitTpm).toBe(180_000);
    const half = parseConfig({ ...raw, limiter_fraction: 0.5, limiter_counts_cached_tokens: true });
    expect([half.models.get(ULTRA)!.limitRpm, half.models.get(ULTRA)!.limitTpm]).toEqual([150, 100_000]);
    expect(half.limiterCountsCachedTokens).toBe(true);
    expect(() => parseConfig({ ...raw, limiter_fraction: 1.5 })).toThrow(/at most 1/);
    expect(() => parseConfig({ ...raw, limiter_counts_cached_tokens: "yes" })).toThrow(/true or false/);
  });

  it("refuses a config that lists a non-nvidia model", () => {
    const raw = JSON.parse(CONFIG_TEXT);
    raw.models["openai/gpt-oss-120b"] = { ...raw.models[SUPER] };
    expect(() => parseConfig(raw)).toThrow(ConfigError);
    expect(() => parseConfig(raw)).toThrow(/nvidia/);
  });
});

describe("usage and cost (reasoning tokens)", () => {
  const ultra = config.models.get(ULTRA)!;

  it("bills completion once when reasoning is counted inside it (Token Factory)", () => {
    const u = readUsage({ prompt_tokens: 1000, completion_tokens: 9709, total_tokens: 10709, completion_tokens_details: { reasoning_tokens: 9338 } })!;
    expect(u.billedOutputTokens).toBe(9709);
    expect(costNano(ultra, u)).toBe(1000 * 1000 + 9709 * 3000); // $0.030127
  });

  it("bills reasoning on top when the report can only mean it was counted outside", () => {
    const outside = readUsage({ prompt_tokens: 1000, completion_tokens: 371, completion_tokens_details: { reasoning_tokens: 9338 } })!;
    expect(outside.billedOutputTokens).toBe(9709);
    const byTotal = readUsage({ prompt_tokens: 1000, completion_tokens: 371, total_tokens: 10709 })!;
    expect(byTotal.billedOutputTokens).toBe(9709);
  });

  it("reads cache hits from either usage form, never above the prompt", () => {
    expect(readUsage({ prompt_tokens: 500, completion_tokens: 10, prompt_tokens_details: { cached_tokens: 400 } })!.cachedTokens).toBe(400);
    expect(readUsage({ prompt_tokens: 500, completion_tokens: 10, prompt_cache_hit_tokens: 300 })!.cachedTokens).toBe(300);
    expect(readUsage({ prompt_tokens: 500, completion_tokens: 10, prompt_cache_hit_tokens: 900 })!.cachedTokens).toBe(500);
    expect(readUsage({ prompt_tokens: 500, completion_tokens: 10, prompt_tokens_details: { cached_tokens: -1 } })!.cachedTokens).toBe(0);
    expect(readUsage({ prompt_tokens: 500, completion_tokens: 10, prompt_tokens_details: "x", prompt_cache_hit_tokens: 1.5 })!.cachedTokens).toBe(0);
    expect(readUsage({ prompt_tokens: 500, completion_tokens: 10 })!.cachedTokens).toBe(0);
  });

  it("bills cached prompt tokens at the full input price and rejects malformed usage", () => {
    const cached = readUsage({ prompt_tokens: 500, completion_tokens: 10, prompt_cache_hit_tokens: 400, prompt_tokens_details: { cached_tokens: 400 } })!;
    expect(costNano(config.models.get(SUPER)!, cached)).toBe(500 * 300 + 10 * 900);
    expect(readUsage(null)).toBeNull();
    expect(readUsage({ prompt_tokens: -1, completion_tokens: 2 })).toBeNull();
    expect(readUsage({ prompt_tokens: 1.5, completion_tokens: 2 })).toBeNull();
    expect(readUsage({ completion_tokens: 2 })).toBeNull();
  });
});

describe("codes", () => {
  it("mints codes that only the hash identifies", async () => {
    const code = newCode();
    expect(code).toMatch(CODE_PATTERN);
    const hash = await hashCode(code);
    expect(hash).toMatch(/^[0-9a-f]{64}$/);
    expect(codeId(hash)).toBe(hash.slice(0, 12));
    expect(newCode()).not.toBe(code);
    expect(bearerToken(`Bearer ${code}`)).toBe(code);
    expect(bearerToken(`bearer   ${code}  `)).toBe(code);
    expect(bearerToken("Basic abc")).toBeNull();
    expect(bearerToken(null)).toBeNull();
  });
});

describe("sliding-window limiter", () => {
  /** Does a call fit `t` ms from now, if nothing else arrives meanwhile? */
  function fitsAt(cur: number, prev: number, elapsed: number, limit: number, need: number, oversize: boolean, t: number): boolean {
    const at = elapsed + t;
    if (at >= 2 * WINDOW_MS) return admits(0, 0, at - 2 * WINDOW_MS, limit, need, oversize);
    if (at >= WINDOW_MS) return admits(0, cur, at - WINDOW_MS, limit, need, oversize);
    return admits(cur, prev, at, limit, need, oversize);
  }

  it("predicts the first moment a refused call fits", () => {
    const cases: [number, number, number, number, number, boolean][] = [
      [150, 0, 1000, 150, 1, false],
      [100, 100, 30_000, 150, 1, false],
      [10, 200, 5_000, 150, 1, false],
      [0, 150, 59_000, 150, 1, false],
      [90_000, 50_000, 20_000, 100_000, 30_000, true],
      [60_000, 0, 10_000, 100_000, 50_000, true],
      [20_000, 0, 10_000, 100_000, 150_000, true],
      [0, 70_000, 40_000, 100_000, 150_000, true],
      [0, 0, 0, 100_000, 150_000, true],
    ];
    for (const [cur, prev, elapsed, limit, need, oversize] of cases) {
      const wait = waitMs(cur, prev, elapsed, limit, need, oversize);
      expect(Number.isFinite(wait)).toBe(true);
      expect(fitsAt(cur, prev, elapsed, limit, need, oversize, wait)).toBe(true);
      if (wait >= 2) expect(fitsAt(cur, prev, elapsed, limit, need, oversize, wait - 2)).toBe(false);
    }
  });

  it("admits an oversized prompt only into an empty window", () => {
    expect(admits(0, 0, 0, 100, 1000, true)).toBe(true);
    expect(admits(1, 0, 0, 100, 1000, true)).toBe(false);
    expect(admits(0, 0, 0, 100, 1000, false)).toBe(false);
  });
});

describe("SSE tap", () => {
  const enc = new TextEncoder();
  const dec = new TextDecoder();

  it("finds usage across split chunks and strips the usage-only chunk unless asked", () => {
    const text =
      'data: {"id":"c1","choices":[{"delta":{"content":"hi"}}]}\n\n' +
      'data: {"id":"c1","choices":[],"usage":{"prompt_tokens":3,"completion_tokens":1}}\n\n' +
      "data: [DONE]\n\n";
    for (const strip of [true, false]) {
      const tap = new SseTap(strip);
      let out = "";
      for (let i = 0; i < text.length; i += 7) {
        const piece = tap.push(enc.encode(text.slice(i, i + 7)));
        if (piece) out += dec.decode(piece);
      }
      const rest = tap.end();
      if (rest) out += dec.decode(rest);
      expect(tap.usage).toEqual({ prompt_tokens: 3, completion_tokens: 1 });
      expect(tap.completionId).toBe("c1");
      expect(tap.sawDone).toBe(true);
      expect(out.includes('"usage"')).toBe(!strip);
      expect(out.endsWith("data: [DONE]\n\n")).toBe(true);
    }
  });

  it("handles CRLF events and multi-byte text split across chunks", () => {
    const tap = new SseTap(false);
    const bytes = enc.encode('data: {"id":"c2","choices":[{"delta":{"content":"héllo ✓"}}]}\r\n\r\n');
    const split = bytes.indexOf(0xc3) + 1; // inside the two bytes of é
    const a = tap.push(bytes.slice(0, split));
    const b = tap.push(bytes.slice(split));
    expect(a).toBeNull();
    expect(dec.decode(b!)).toContain("héllo ✓");
    expect(tap.completionId).toBe("c2");
  });
});

describe("per-code limits", () => {
  it("gives a code its share of a budget, never less than one", () => {
    expect(shareOf(300, 25)).toBe(75);
    expect(shareOf(2, 25)).toBe(1);
    expect(shareOf(100_000, 100)).toBe(100_000);
  });

  it("lets a code with nothing running start a call larger than its share, once its own window allows", () => {
    expect(codeAdmitsTokens(0, 0, 0, 500, 800, 0)).toBe(true);
    expect(codeAdmitsTokens(0, 0, 0, 500, 800, 1)).toBe(false);
    expect(codeAdmitsTokens(100, 0, 0, 500, 800, 0)).toBe(true);
    expect(codeAdmitsTokens(600, 0, 0, 500, 800, 0)).toBe(false);
    expect(codeAdmitsTokens(100, 0, 0, 500, 300, 1)).toBe(true);
    expect(codeAdmitsTokens(100, 0, 0, 500, 500, 1)).toBe(false);
  });
});

describe("config limits", () => {
  it("needs the per-code defaults, the 5xx window and the Vercel body limit", () => {
    for (const key of ["code_share_pct", "code_max_in_flight", "release_5xx_within_ms", "vercel_max_body_bytes"]) {
      const raw = JSON.parse(CONFIG_TEXT);
      delete raw[key];
      expect(() => parseConfig(raw), key).toThrow(ConfigError);
    }
    const raw = JSON.parse(CONFIG_TEXT);
    raw.code_share_pct = 101;
    expect(() => parseConfig(raw)).toThrow(/percent/);
    expect(config).toMatchObject({ codeSharePct: 25, codeMaxInFlight: 8, release5xxWithinMs: 5000, vercelMaxBodyBytes: 1_048_576 });
    expect(config.estimate).toMatchObject({ toolTokensPerByte: 2, perToolKeyTokens: 4, perArgumentTokens: 4 });
  });
});

describe("ledger choice", () => {
  it("never starts a second, file ledger by accident while Upstash is configured", () => {
    const upstash = { UPSTASH_REDIS_REST_URL: "https://example-upstash.invalid", UPSTASH_REDIS_REST_TOKEN: "t" };
    expect(() => ledgerFromEnv(upstash, { host: "local", defaultFile: "x.json" })).toThrow(/RELAY_LEDGER=file/);
    expect(() => ledgerFromEnv({ UPSTASH_REDIS_REST_TOKEN: "t" }, { host: "local", defaultFile: "x.json" })).toThrow(/RELAY_LEDGER=upstash/);
    expect(ledgerFromEnv({ ...upstash, RELAY_LEDGER: "file" }, { host: "local", defaultFile: "x.json" }).kind).toBe("file");
    expect(ledgerFromEnv({ ...upstash, RELAY_LEDGER: "upstash" }, { host: "local" }).kind).toBe("upstash");
    expect(ledgerFromEnv(upstash, { host: "vercel" }).kind).toBe("upstash");
    expect(ledgerFromEnv({}, { host: "local", defaultFile: "x.json" }).kind).toBe("file");
  });
});
