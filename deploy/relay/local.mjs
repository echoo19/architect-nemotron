#!/usr/bin/env node
// The spend relay on this machine: http://127.0.0.1:8787/v1 (loopback only).
// No per-call time limit (set RELAY_MAX_CALL_SECONDS to add one). The ledger defaults to
// a file next to this script (.data/ledger.json); RELAY_LEDGER=upstash shares the
// Vercel relay's ledger instead. With the Upstash variables set, the file ledger needs
// RELAY_LEDGER=file by name, so one machine never keeps a second global cap by accident.
// Needs `npm run compile` first (`npm start` does both).
//
//   NEBIUS_API_KEY=... node local.mjs
//
// The key is read from the environment only and is never logged.

import http from "node:http";
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const here = new URL(".", import.meta.url);
const built = new URL("./dist/src/index.js", here);
if (!existsSync(built)) {
  console.error("relay: run `npm run compile` first (or `npm start`).");
  process.exit(1);
}
const { createRelayFromEnv, toNodeHandler } = await import(built.href);
const { Agent, fetch: undiciFetch } = await import("undici");

// Token Factory can take minutes on a long Ultra reply: no header or body timeouts here.
const agent = new Agent({ headersTimeout: 0, bodyTimeout: 0, connectTimeout: 30_000 });

const relay = createRelayFromEnv({
  env: process.env,
  host: "local",
  configText: readFileSync(new URL("./relay.config.json", here), "utf8"),
  defaultLedgerFile: fileURLToPath(new URL("./.data/ledger.json", here)),
  fetch: (url, init) => undiciFetch(url, { ...init, dispatcher: agent }),
  defaultMaxCallSeconds: 0,
});
if (!relay.ledgerKind) {
  console.error(`relay: no ledger: ${relay.ledgerError}.`);
  process.exit(1);
}

const port = Number(process.env.RELAY_PORT ?? 8787);
if (!Number.isInteger(port) || port <= 0 || port > 65535) {
  console.error("relay: RELAY_PORT must be a port number.");
  process.exit(1);
}
const base = `http://127.0.0.1:${port}`;
const server = http.createServer(toNodeHandler(relay.handle, base));
server.requestTimeout = 0; // a long stream must not be cut by Node's 5-minute default
server.timeout = 0;
server.keepAliveTimeout = 5_000;
server.on("error", (err) => {
  console.error(`relay: ${err.message}`);
  process.exit(1);
});
server.listen(port, "127.0.0.1", () => {
  const ledger = relay.ledgerKind;
  const limit = Number(process.env.RELAY_MAX_CALL_SECONDS || 0);
  console.error(
    `relay: listening on ${base}/v1 | ledger ${ledger} | ${relay.config.models.size} nvidia models | ` +
      `prices of ${relay.config.priceDate} | ${limit > 0 ? `${limit} s per call` : "no per-call time limit"}`,
  );
});

let closing = false;
for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    if (closing) process.exit(1);
    closing = true;
    console.error("relay: closing (in-flight calls finish and settle first)");
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 30_000).unref();
  });
}
