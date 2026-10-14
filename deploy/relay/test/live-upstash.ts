/**
 * A real Upstash database for the live tests, only when UPSTASH_REDIS_REST_URL and
 * UPSTASH_REDIS_REST_TOKEN are set. Each use takes a fresh `relaytest:<random>` key prefix
 * and deletes every key under it afterwards, so a production ledger in the same database
 * (prefix `relay`) is never read or written.
 */

import { Redis } from "@upstash/redis";
import { type UpstashClient, UpstashLedger, upstashStore } from "../src/ledger/upstash.js";

export interface LiveUpstash {
  url: string;
  token: string;
  redis: Redis;
  /** A fresh prefix, remembered for cleanup(). */
  prefix(): string;
  /** An UpstashLedger under a fresh prefix. */
  ledger(): { ledger: UpstashLedger; prefix: string };
  /** Deletes every key under one prefix, or under every prefix handed out so far. */
  cleanup(prefix?: string): Promise<void>;
}

export function liveUpstash(): LiveUpstash | null {
  const url = process.env.UPSTASH_REDIS_REST_URL?.trim();
  const token = process.env.UPSTASH_REDIS_REST_TOKEN?.trim();
  if (!url || !token) return null;
  const redis = new Redis({ url, token, automaticDeserialization: false, enableTelemetry: false });
  const used: string[] = [];
  const drop = async (prefix: string) => {
    let cursor: string | number = "0";
    do {
      const [next, keys] = (await redis.scan(cursor, { match: `${prefix}:*`, count: 500 })) as [string | number, string[]];
      if (keys.length > 0) await redis.del(...keys);
      cursor = next;
    } while (String(cursor) !== "0");
  };
  const prefix = () => {
    const fresh = `relaytest:${Math.random().toString(36).slice(2, 10)}`;
    used.push(fresh);
    return fresh;
  };
  return {
    url,
    token,
    redis,
    prefix,
    ledger: () => {
      const fresh = prefix();
      return { ledger: new UpstashLedger(upstashStore(redis as unknown as UpstashClient), { prefix: fresh }), prefix: fresh };
    },
    cleanup: async (one) => {
      for (const each of one ? [one] : used.splice(0)) await drop(each);
    },
  };
}
