/**
 * The Lua contract against a real Upstash database. Runs only when
 * UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN are set; every case writes under a
 * fresh `relaytest:<random>` prefix and deletes it afterwards, so a production ledger in
 * the same database is never touched.
 */

import { describe, it } from "vitest";
import { ledgerContract } from "./ledger-contract.js";
import { liveUpstash } from "./live-upstash.js";

const live = liveUpstash();

if (live) {
  ledgerContract("UpstashLedger (live Upstash)", async () => {
    const { ledger, prefix } = live.ledger();
    return { ledger, cleanup: () => live.cleanup(prefix) };
  });
} else {
  describe.skip("ledger contract: UpstashLedger (live Upstash; set UPSTASH_REDIS_REST_URL and _TOKEN to run)", () => {
    it("needs Upstash credentials", () => undefined);
  });
}
