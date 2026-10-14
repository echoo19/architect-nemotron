/**
 * End to end through the real processes: admin.mjs mints a code into a file ledger,
 * local.mjs serves it on loopback, and a fake Token Factory (an HTTP server here) answers.
 * Needs the compiled dist/ (npm test compiles first).
 */

import { type ChildProcess, spawn } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import http from "node:http";
import type { AddressInfo } from "node:net";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { hashCode, newCode } from "../src/codes.js";
import { SUPER, ULTRA, chunks, cleanupDirs, completion, messages, tempDir, usage } from "./helpers.js";

const root = fileURLToPath(new URL("..", import.meta.url));
const ledgerFile = path.join(tempDir("cli"), "ledger.json");
const seen: { auth: string | undefined; body: Record<string, unknown> }[] = [];
let upstreamClosed = 0;
let upstream: http.Server;
let relay: ChildProcess;
let relayPort = 0;
let relayLog = "";
let code = "";

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.once("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const port = (probe.address() as AddressInfo).port;
      probe.close(() => resolve(port));
    });
  });
}

function run(
  args: string[],
  env: Record<string, string | undefined> = { RELAY_LEDGER: "file" },
  input?: string,
): Promise<{ code: number | null; stdout: string; stderr: string }> {
  return new Promise((resolve) => {
    const proc = spawn(process.execPath, [path.join(root, "admin.mjs"), ...args], {
      cwd: root,
      env: { ...process.env, RELAY_LEDGER_FILE: ledgerFile, ...env },
    });
    let stdout = "";
    let stderr = "";
    proc.stdout.on("data", (d) => (stdout += d));
    proc.stderr.on("data", (d) => (stderr += d));
    proc.on("exit", (exit) => resolve({ code: exit, stdout, stderr }));
    if (input !== undefined) proc.stdin.end(input);
  });
}

async function call(body: unknown, init: { code?: string; signal?: AbortSignal } = {}): Promise<Response> {
  return fetch(`http://127.0.0.1:${relayPort}/v1/chat/completions`, {
    method: "POST",
    headers: { authorization: `Bearer ${init.code ?? code}`, "content-type": "application/json" },
    body: JSON.stringify(body),
    ...(init.signal ? { signal: init.signal } : {}),
  });
}

beforeAll(async () => {
  expect(existsSync(path.join(root, "dist", "src", "index.js")), "run `npm run compile` first (npm test does)").toBe(true);
  upstream = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (d) => (raw += d));
    req.on("end", () => {
      const body = JSON.parse(raw) as Record<string, unknown>;
      seen.push({ auth: req.headers.authorization, body });
      const first = (body.messages as { content: string }[])[0]!.content;
      if (first === "overshoot") {
        // Usage far above anything the relay could have held for this request.
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify(completion(String(body.model), usage(10_000, 1), "chatcmpl-over")));
        return;
      }
      if (first === "hang") {
        res.writeHead(200, { "content-type": "text/event-stream" });
        res.write(`data: ${JSON.stringify(chunks(SUPER, null)[0])}\n\n`);
        res.on("close", () => (upstreamClosed += 1));
        return; // never finishes: only the relay hanging up ends it
      }
      if (body.stream) {
        res.writeHead(200, { "content-type": "text/event-stream", "x-ratelimit-remaining-requests": "298" });
        for (const event of chunks(String(body.model), usage(25, 3))) {
          res.write(`data: ${typeof event === "string" ? event : JSON.stringify(event)}\n\n`);
        }
        res.end();
        return;
      }
      res.writeHead(200, { "content-type": "application/json", "x-ratelimit-remaining-requests": "299" });
      res.end(JSON.stringify(completion(String(body.model), usage(30, 6), "chatcmpl-e2e")));
    });
  });
  await new Promise<void>((resolve) => upstream.listen(0, "127.0.0.1", () => resolve()));
  const upstreamPort = (upstream.address() as AddressInfo).port;

  expect((await run(["global", "--cap", "1"])).code).toBe(0);
  const minted = await run(["mint", "--label", "e2e", "--total", "0.01", "--daily", "0.01"]);
  expect(minted.code).toBe(0);
  code = minted.stdout.trim();
  expect(code).toMatch(/^rc_[A-Za-z0-9_-]{32}$/);
  expect(minted.stderr).toContain("shown once");

  relayPort = await freePort();
  relay = spawn(process.execPath, [path.join(root, "local.mjs")], {
    cwd: root,
    env: {
      PATH: process.env.PATH ?? "",
      SYSTEMROOT: process.env.SYSTEMROOT ?? "",
      RELAY_PORT: String(relayPort),
      RELAY_LEDGER_FILE: ledgerFile,
      RELAY_UPSTREAM_BASE_URL: `http://127.0.0.1:${upstreamPort}/v1`,
      NEBIUS_API_KEY: "test-upstream-key",
    },
  });
  relay.stderr!.on("data", (d) => (relayLog += d));
  relay.stdout!.on("data", (d) => (relayLog += d));
  const start = Date.now();
  for (;;) {
    try {
      const health = await fetch(`http://127.0.0.1:${relayPort}/healthz`);
      if (health.status === 200) break;
    } catch {
      // not up yet
    }
    if (Date.now() - start > 15_000) throw new Error(`local.mjs did not start:\n${relayLog}`);
    await new Promise((r) => setTimeout(r, 50));
  }
});

afterAll(async () => {
  relay?.kill();
  await new Promise<void>((resolve) => (upstream ? upstream.close(() => resolve()) : resolve()));
  cleanupDirs();
});

describe("local.mjs and admin.mjs", () => {
  it("serves a whole reply and a stream, and Token Factory sees only the relay's key", async () => {
    const whole = await call({ model: SUPER, messages, max_tokens: 32 });
    expect(whole.status).toBe(200);
    expect(whole.headers.get("x-completion-id")).toBe("chatcmpl-e2e");
    expect(whole.headers.get("x-ratelimit-remaining-requests")).toBe("299");
    expect(Number(whole.headers.get("x-upstream-ms"))).toBeGreaterThanOrEqual(0);
    expect(((await whole.json()) as { id: string }).id).toBe("chatcmpl-e2e");

    const streamed = await call({ model: SUPER, messages, max_tokens: 32, stream: true });
    expect(streamed.status).toBe(200);
    const text = await streamed.text();
    expect(text.trim().endsWith("data: [DONE]")).toBe(true);
    expect(text).not.toContain('"usage":{');

    for (const entry of seen) {
      expect(entry.auth).toBe("Bearer test-upstream-key");
      expect(JSON.stringify(entry)).not.toContain(code);
    }
    expect(seen.at(-1)!.body).toMatchObject({ stream: true, stream_options: { include_usage: true }, n: 1 });
  });

  it("refuses other models and calls over the cap without reaching Token Factory", async () => {
    const before = seen.length;
    expect((await call({ model: "gpt-4o", messages })).status).toBe(403);
    const over = await call({ model: ULTRA, messages }); // 32,768 output tokens of Ultra cannot fit in $0.01
    expect(over.status).toBe(402);
    const err = (await over.json()) as { error: { remaining_usd: number; code: string } };
    expect(err.error.code).toMatch(/cap/);
    expect(err.error.remaining_usd).toBeGreaterThan(0);
    expect(seen.length).toBe(before);
  });

  it("stops the upstream stream when the client hangs up, and books the full hold", async () => {
    const client = new AbortController();
    const res = await call({ model: SUPER, messages: [{ role: "user", content: "hang" }], max_tokens: 16, stream: true }, { signal: client.signal });
    const reader = res.body!.getReader();
    await reader.read();
    client.abort();
    const start = Date.now();
    while (upstreamClosed === 0 && Date.now() - start < 10_000) await new Promise((r) => setTimeout(r, 20));
    expect(upstreamClosed).toBe(1);
    let report: { events: { kind: string; why?: string }[] } = { events: [] };
    while (Date.now() - start < 10_000) {
      report = JSON.parse((await run(["report", "--json", "--events", "1"])).stdout);
      if (report.events[0]?.why === "client_closed") break;
      await new Promise((r) => setTimeout(r, 50));
    }
    expect(report.events[0]).toMatchObject({ kind: "full", why: "client_closed" });
  });

  it("lists and reports without ever printing a code", async () => {
    const list = await run(["list"]);
    expect(list.code).toBe(0);
    expect(list.stdout).toContain("e2e");
    expect(list.stdout).not.toContain(code);
    const report = await run(["report", "--json", "--events", "10"]);
    const parsed = JSON.parse(report.stdout) as {
      global: { spent: number; reserved: number };
      events: { actual: number }[];
      codes: Record<string, unknown>[];
    };
    expect(report.stdout).not.toContain(code);
    expect(parsed.global.reserved).toBe(0);
    expect(parsed.global.spent).toBe(parsed.events.reduce((sum, e) => sum + e.actual, 0));
    expect(parsed.codes[0]).not.toHaveProperty("hash");
    const text = await run(["report"]);
    expect(text.stdout).toContain("usage by UTC day and model");
  });

  it("revokes a code and flips the kill switch", async () => {
    expect((await run(["disable"])).code).toBe(0);
    expect((await fetch(`http://127.0.0.1:${relayPort}/healthz`)).status).toBe(503);
    expect((await call({ model: SUPER, messages, max_tokens: 8 })).status).toBe(503);
    expect((await run(["enable"])).code).toBe(0);
    expect((await call({ model: SUPER, messages, max_tokens: 8 })).status).toBe(200);
    expect((await run(["revoke", "e2e"])).code).toBe(0);
    const revoked = await call({ model: SUPER, messages, max_tokens: 8 });
    expect(revoked.status).toBe(401);
    expect(relayLog).not.toContain(code);
    expect(relayLog).not.toContain("test-upstream-key");
  });

  it("mints a code with its own limits, freezes it after an overshoot and unfreezes it", async () => {
    const minted = await run(["mint", "--label", "e2e-limits", "--total", "1", "--share", "50", "--in-flight", "2"]);
    expect(minted.code).toBe(0);
    expect(minted.stderr).toContain("50% of each model's budget, 2 calls at once");
    const own = minted.stdout.trim();
    const listed = JSON.parse((await run(["list", "--json"])).stdout) as { label: string; sharePct: number; maxInFlight: number }[];
    expect(listed.find((c) => c.label === "e2e-limits")).toMatchObject({ sharePct: 50, maxInFlight: 2 });
    const over = await call({ model: SUPER, messages: [{ role: "user", content: "overshoot" }], max_tokens: 8 }, { code: own });
    expect(over.status).toBe(200);
    await over.text();
    const shut = await call({ model: SUPER, messages, max_tokens: 8 }, { code: own });
    expect(shut.status).toBe(403);
    expect(((await shut.json()) as { error: { type: string } }).error.type).toBe("relay_code_frozen");
    const report = await run(["report", "--events", "3"]);
    expect(report.stdout).toMatch(/frozen +[0-9a-f]{12} "e2e-limits"/);
    expect(report.stdout).toMatch(/usage \(over the hold by \$0\.00/);
    expect(relayLog).toMatch(/froze the code/);
    expect((await run(["unfreeze", "e2e-limits"])).stderr).toContain("works again");
    const again = await call({ model: SUPER, messages, max_tokens: 8 }, { code: own });
    expect(again.status).toBe(200);
    await again.text();
  });

  it("adds a code that already exists elsewhere, read from stdin, and never prints it", async () => {
    const kept = newCode();
    const other = newCode();
    const file = `e2e-kept=${kept}\nspare-x=${other}\n`; // the private codes file keeps <label>=<code> lines
    const added = await run(["mint", "--existing", "--label", "e2e-kept", "--total", "0.5", "--daily", "0.2"], undefined, file);
    expect(added.code, added.stderr).toBe(0);
    expect(added.stdout).toBe("");
    expect(added.stderr).toContain(`added ${(await hashCode(kept)).slice(0, 12)} "e2e-kept": total $0.50, daily $0.20`);
    expect(added.stderr + added.stdout).not.toContain(kept);
    expect(added.stderr).not.toContain(other);
    const res = await call({ model: SUPER, messages, max_tokens: 8 }, { code: kept });
    expect(res.status).toBe(200);
    await res.text();
    const listed = JSON.parse((await run(["list", "--json"])).stdout) as { label: string; capTotal: number; capDaily: number }[];
    expect(listed.find((c) => c.label === "e2e-kept")).toMatchObject({ capTotal: 500_000_000, capDaily: 200_000_000 });
    // The only code on stdin is taken as it is.
    const bare = await run(["mint", "--existing", "--label", "e2e-bare", "--total", "0.1"], undefined, `${newCode()}\n`);
    expect(bare.code, bare.stderr).toBe(0);
    // Several codes and no line for the label, a malformed code, or a code the ledger already has:
    // refused, and the refusal never repeats what stdin held.
    const ambiguous = await run(["mint", "--existing", "--label", "nobody", "--total", "0.1"], undefined, file);
    expect(ambiguous.code).toBe(2);
    expect(ambiguous.stderr).toContain("stdin holds 2 codes");
    expect(ambiguous.stderr).not.toContain(kept);
    const malformed = await run(["mint", "--existing", "--label", "e2e-bad", "--total", "0.1"], undefined, "e2e-bad=rc_tooshort\n");
    expect(malformed.code).toBe(2);
    expect(malformed.stderr).not.toContain("rc_tooshort");
    const twice = await run(["mint", "--existing", "--label", "e2e-kept-again", "--total", "0.1"], undefined, `${kept}\n`);
    expect(twice.code).toBe(1);
    expect(twice.stderr).toContain("already exists");
    expect(twice.stderr).not.toContain(kept);
  });

  it("counts an opening balance against the global cap, from a figure or from another ledger file", async () => {
    const before = JSON.parse((await run(["report", "--json", "--events", "0"])).stdout) as { global: { spent: number } };
    const set = await run(["opening-balance", "--usd", "0.05"]);
    expect(set.code).toBe(0);
    expect(set.stderr).toContain("opening balance $0.05 (was $0.00)");
    const after = JSON.parse((await run(["report", "--json", "--events", "0"])).stdout) as { global: { spent: number; opening: number } };
    expect(after.global).toMatchObject({ spent: before.global.spent + 50_000_000, opening: 50_000_000 });
    expect((await run(["report", "--events", "0"])).stdout).toContain("($0.05 carried over)");
    // A ledger file written before this relay version, with spend still on it.
    const old = path.join(tempDir("cli-old"), "ledger.json");
    writeFileSync(
      old,
      JSON.stringify({
        version: 1,
        global: { cap: 26_000_000_000, spent: 20_000_000, reserved: 3_000_000, disabled: false },
        codes: {},
        days: {},
        usage: {},
        reservations: {},
        limiter: {},
      }),
    );
    const carried = await run(["opening-balance", "--from-file", old]);
    expect(carried.code).toBe(0);
    expect(carried.stderr).toContain("opening balance $0.023 (was $0.05)");
    // The old ledger is switched off, so it cannot keep spending under a cap of its own.
    expect(carried.stderr).toContain("kill switch on");
    expect((JSON.parse(readFileSync(old, "utf8")) as { global: { disabled: boolean } }).global.disabled).toBe(true);
    const same = await run(["opening-balance", "--from-file", ledgerFile]);
    expect(same.code).toBe(2);
    expect(same.stderr).toContain("names the ledger being changed");
    const last = JSON.parse((await run(["report", "--json", "--events", "0"])).stdout) as { global: { spent: number; opening: number } };
    expect(last.global).toMatchObject({ spent: before.global.spent + 23_000_000, opening: 23_000_000 });
    expect((await run(["sweep", "--all"])).stderr).toContain("booked 0 open reservation(s)");
  });

  it("refuses the file ledger while the Upstash variables are set, unless it is asked for by name", async () => {
    const upstash = { RELAY_LEDGER: undefined, UPSTASH_REDIS_REST_URL: "https://example-upstash.invalid", UPSTASH_REDIS_REST_TOKEN: "t" };
    const refused = await run(["list"], upstash);
    expect(refused.code).toBe(1);
    expect(refused.stderr).toContain("RELAY_LEDGER=file");
    expect((await run(["list", "--ledger", "file"], upstash)).code).toBe(0);
    const port = await freePort();
    const base = { PATH: process.env.PATH ?? "", SYSTEMROOT: process.env.SYSTEMROOT ?? "", RELAY_PORT: String(port), RELAY_LEDGER_FILE: ledgerFile, NEBIUS_API_KEY: "k" };
    const local = spawn(process.execPath, [path.join(root, "local.mjs")], { cwd: root, env: { ...base, UPSTASH_REDIS_REST_URL: "https://example-upstash.invalid" } });
    let stderr = "";
    local.stderr!.on("data", (d) => (stderr += d));
    const exit = await new Promise<number | null>((resolve) => local.on("exit", resolve));
    expect(exit).toBe(1);
    expect(stderr).toContain("RELAY_LEDGER=file");
    const named = spawn(process.execPath, [path.join(root, "local.mjs")], {
      cwd: root,
      env: { ...base, RELAY_LEDGER: "file", UPSTASH_REDIS_REST_URL: "https://example-upstash.invalid" },
    });
    try {
      const start = Date.now();
      let status = 0;
      while (status !== 200 && Date.now() - start < 15_000) {
        status = await fetch(`http://127.0.0.1:${port}/healthz`).then((r) => r.status, () => 0);
        if (status !== 200) await new Promise((r) => setTimeout(r, 50));
      }
      expect(status).toBe(200);
    } finally {
      named.kill();
    }
  });
});
