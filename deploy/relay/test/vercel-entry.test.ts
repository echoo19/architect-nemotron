/** The Vercel entry (api/relay.ts): config loading, ledger choice and its readable 503s. */

import http from "node:http";
import type { AddressInfo } from "node:net";
import { Redis } from "@upstash/redis";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type UpstashClient, UpstashLedger, upstashStore } from "../src/ledger/upstash.js";
import { usdToNano } from "../src/money.js";
import { startFakeUpstash } from "./fake-upstash.js";
import { SUPER, completion, messages, mint, usage } from "./helpers.js";
import { liveUpstash } from "./live-upstash.js";

const saved = { ...process.env };
// Read before any test edits process.env; set only when the Upstash variables are.
const live = liveUpstash();

afterEach(() => {
  process.env = { ...saved };
  vi.resetModules();
});

async function entry(env: Record<string, string | undefined>) {
  vi.resetModules();
  for (const key of ["RELAY_LEDGER", "RELAY_LEDGER_FILE", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN", "NEBIUS_API_KEY", "RELAY_DISABLED", "RELAY_MAX_CALL_SECONDS"]) {
    delete process.env[key];
  }
  Object.assign(process.env, env);
  const module = await import("../api/relay.js");
  return module.default as { fetch(request: Request): Promise<Response> };
}

describe("api/relay.ts", () => {
  it("loads relay.config.json and names the missing Upstash variables", async () => {
    const relay = await entry({ NEBIUS_API_KEY: "k" });
    const health = await relay.fetch(new Request("https://relay.example/healthz"));
    expect(health.status).toBe(503);
    const body = (await health.json()) as { reason: string; host: string; models: string[] };
    expect(body.host).toBe("vercel");
    expect(body.models).toHaveLength(4);
    expect(body.reason).toMatch(/UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN/);
    const call = await relay.fetch(
      new Request("https://relay.example/api/relay?route=v1/chat/completions", { method: "POST", body: "{}" }),
    );
    expect(call.status).toBe(503);
    expect(await call.json()).toMatchObject({ error: { type: "relay_misconfigured" } });
  });

  it("refuses a file ledger on Vercel, where each instance would keep its own caps", async () => {
    const relay = await entry({ NEBIUS_API_KEY: "k", RELAY_LEDGER: "file", RELAY_LEDGER_FILE: "/tmp/ledger.json" });
    const health = await relay.fetch(new Request("https://relay.example/healthz"));
    expect(((await health.json()) as { reason: string }).reason).toMatch(/cannot hold caps across Vercel instances/);
  });

  /**
   * Health, model list, balance and one settled call through the Vercel entry, with the
   * ledger at `url` under `prefix` and a fake Token Factory on this machine.
   */
  async function endToEnd(url: string, token: string, prefix?: string) {
    const upstream = http.createServer((req, res) => {
      let raw = "";
      req.on("data", (d) => (raw += d));
      req.on("end", () => {
        const body = JSON.parse(raw) as { model: string };
        res.writeHead(200, { "content-type": "application/json", "x-ratelimit-remaining-requests": "149" });
        res.end(JSON.stringify(completion(body.model, usage(40, 8), "chatcmpl-vercel")));
      });
    });
    await new Promise<void>((resolve) => upstream.listen(0, "127.0.0.1", () => resolve()));
    try {
      const redis = new Redis({ url, token, automaticDeserialization: false, enableTelemetry: false });
      const ledger = new UpstashLedger(upstashStore(redis as unknown as UpstashClient), prefix ? { prefix } : {});
      await ledger.setGlobalCap(usdToNano("1"));
      const { code, hash } = await mint(ledger, { total: "0.5", daily: "0.25" });
      const relay = await entry({
        NEBIUS_API_KEY: "k",
        UPSTASH_REDIS_REST_URL: url,
        UPSTASH_REDIS_REST_TOKEN: token,
        ...(prefix ? { RELAY_KEY_PREFIX: prefix } : {}),
        RELAY_UPSTREAM_BASE_URL: `http://127.0.0.1:${(upstream.address() as AddressInfo).port}/v1`,
      });
      expect((await relay.fetch(new Request("https://relay.example/healthz"))).status).toBe(200);
      const auth = { authorization: `Bearer ${code}` };
      // The rewrite in vercel.json hands the original path over as ?route=.
      const listed = await relay.fetch(new Request("https://relay.example/api/relay?route=v1/models", { headers: auth }));
      expect(listed.status).toBe(200);
      expect(((await listed.json()) as { data: unknown[] }).data).toHaveLength(4);
      const res = await relay.fetch(
        new Request("https://relay.example/v1/chat/completions", {
          method: "POST",
          headers: { ...auth, "content-type": "application/json" },
          body: JSON.stringify({ model: SUPER, messages, max_tokens: 64 }),
        }),
      );
      expect(res.status).toBe(200);
      expect(res.headers.get("x-completion-id")).toBe("chatcmpl-vercel");
      expect(res.headers.get("x-ratelimit-remaining-requests")).toBe("149");
      const cost = 40 * 300 + 8 * 900;
      expect(await ledger.getCode(hash)).toMatchObject({ spent: cost, reserved: 0, requests: 1 });
      const balance = await relay.fetch(new Request("https://relay.example/api/relay?route=v1/relay/balance", { headers: auth }));
      // Integer nano-dollars over 1e9 parse to the same double as the relay's decimal text.
      expect(await balance.json()).toMatchObject({ limited_by: "daily_cap", remaining_usd: (usdToNano("0.25") - cost) / 1e9, spent_usd: cost / 1e9 });
    } finally {
      await new Promise<void>((resolve) => upstream.close(() => resolve()));
    }
  }

  it("serves a call end to end: Upstash client, Lua ledger, upstream, settlement", async () => {
    const store = await startFakeUpstash();
    try {
      await endToEnd(store.url, store.token);
      expect(store.commands).toContain("eval");
    } finally {
      await store.close();
    }
  });

  it.skipIf(!live)("serves a call end to end on a live Upstash database", async () => {
    const prefix = live!.prefix();
    try {
      await endToEnd(live!.url, live!.token, prefix);
    } finally {
      await live!.cleanup(prefix);
    }
  });

  it("builds an Upstash ledger from the two variables without contacting it", async () => {
    const relay = await entry({
      NEBIUS_API_KEY: "k",
      UPSTASH_REDIS_REST_URL: "https://example-upstash.invalid",
      UPSTASH_REDIS_REST_TOKEN: "t",
      RELAY_DISABLED: "1",
    });
    const health = await relay.fetch(new Request("https://relay.example/healthz"));
    expect(health.status).toBe(503);
    expect(await health.json()).toMatchObject({ ledger: "upstash", disabled: true });
  });
  it("refuses a per-call limit that Vercel would cut short", async () => {
    const upstash = { NEBIUS_API_KEY: "k", UPSTASH_REDIS_REST_URL: "https://example-upstash.invalid", UPSTASH_REDIS_REST_TOKEN: "t" };
    for (const value of ["0", "0.5", "291", "300"]) {
      const relay = await entry({ ...upstash, RELAY_MAX_CALL_SECONDS: value });
      const res = await relay.fetch(new Request("https://relay.example/healthz"));
      expect(res.status, value).toBe(503);
      expect(((await res.json()) as { error: { message: string } }).error.message, value).toMatch(/RELAY_MAX_CALL_SECONDS must be from 1 to 290/);
    }
    const fine = await entry({ ...upstash, RELAY_MAX_CALL_SECONDS: "290", RELAY_DISABLED: "1" });
    expect(await (await fine.fetch(new Request("https://relay.example/healthz"))).json()).toMatchObject({ ledger: "upstash", disabled: true });
  });

  it("takes request bodies of at most 1 MiB on Vercel", async () => {
    const relay = await entry({ NEBIUS_API_KEY: "k", UPSTASH_REDIS_REST_URL: "https://example-upstash.invalid", UPSTASH_REDIS_REST_TOKEN: "t" });
    const body = JSON.stringify({ model: SUPER, messages: [{ role: "user", content: "x".repeat(1_100_000) }] });
    const res = await relay.fetch(
      new Request("https://relay.example/v1/chat/completions", {
        method: "POST",
        headers: { authorization: `Bearer rc_${"A".repeat(32)}`, "content-type": "application/json" },
        body,
      }),
    );
    expect(res.status).toBe(413);
    expect(((await res.json()) as { error: { message: string } }).error.message).toContain("1048576");
  });
});
