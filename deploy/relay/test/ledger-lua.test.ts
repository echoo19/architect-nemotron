/**
 * The Upstash ledger's Lua scripts, run offline: ioredis-mock executes them in a Lua VM
 * (fengari: Lua 5.3 with 32-bit integers, where Redis runs Lua 5.1 with doubles; lua.ts
 * explains how the scripts read the same on both). This checks the scripts' logic; the
 * live contract against a real Upstash database is ledger-upstash.test.ts.
 */

import RedisMock from "ioredis-mock";
import { UpstashLedger } from "../src/ledger/upstash.js";
import { IoredisMockAdapter } from "./ioredis-adapter.js";
import { ledgerContract } from "./ledger-contract.js";

const redis = new RedisMock();
let n = 0;

ledgerContract("UpstashLedger (Lua on ioredis-mock)", async () => {
  n += 1;
  return { ledger: new UpstashLedger(new IoredisMockAdapter(redis), { prefix: `lua${n}` }) };
});
