/**
 * The ledger contract through the real @upstash/redis client, against an offline stand-in
 * for Upstash's REST API (test/fake-upstash.ts). This covers what the ioredis-mock run
 * cannot: the client's request shape, auto-pipelining, base64 replies and raw HGETALL.
 */

import { Redis } from "@upstash/redis";
import { afterAll } from "vitest";
import { type UpstashClient, UpstashLedger, upstashStore } from "../src/ledger/upstash.js";
import { type FakeUpstash, startFakeUpstash } from "./fake-upstash.js";
import { ledgerContract } from "./ledger-contract.js";

let fake: Promise<FakeUpstash> | null = null;
const server = () => (fake ??= startFakeUpstash());
afterAll(async () => {
  if (fake) await (await fake).close();
});

let n = 0;
ledgerContract("UpstashLedger via @upstash/redis REST (offline stand-in)", async () => {
  const f = await server();
  const redis = new Redis({ url: f.url, token: f.token, automaticDeserialization: false, enableTelemetry: false });
  n += 1;
  return { ledger: new UpstashLedger(upstashStore(redis as unknown as UpstashClient), { prefix: `rest${n}` }) };
});
