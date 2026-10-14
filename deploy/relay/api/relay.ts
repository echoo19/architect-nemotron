/**
 * Vercel host (Node.js runtime, Fluid compute). vercel.json rewrites /v1/* and /healthz
 * here. Settlement that outlives the response (a client that hung up) runs under
 * waitUntil; each call stops itself at RELAY_MAX_CALL_SECONDS (default 280 s), before the
 * 300 s platform limit, and settles before it goes.
 */

import { readFileSync } from "node:fs";
import path from "node:path";
import { waitUntil } from "@vercel/functions";
import type { Relay } from "../src/handler.js";
import { createRelayFromEnv } from "../src/runtime.js";

let relay: Relay | null = null;
let failure: string | null = null;

/** relay.config.json ships with the function (vercel.json includeFiles). */
function configText(): string {
  try {
    return readFileSync(new URL("../relay.config.json", import.meta.url), "utf8");
  } catch {
    return readFileSync(path.join(process.cwd(), "relay.config.json"), "utf8");
  }
}

function instance(): Relay | null {
  if (relay || failure) return relay;
  try {
    relay = createRelayFromEnv({
      env: process.env,
      host: "vercel",
      configText: configText(),
      waitUntil,
      defaultMaxCallSeconds: 280,
    });
  } catch (err) {
    failure = err instanceof Error ? err.message : String(err);
  }
  return relay;
}

export default {
  async fetch(request: Request): Promise<Response> {
    const ready = instance();
    if (!ready) {
      return new Response(
        JSON.stringify({ error: { message: `The relay is misconfigured: ${failure ?? "unknown"}.`, type: "relay_misconfigured" } }),
        { status: 503, headers: { "content-type": "application/json" } },
      );
    }
    return ready.handle(request);
  },
};
