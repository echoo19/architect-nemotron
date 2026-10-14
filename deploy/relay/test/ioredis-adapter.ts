/** ioredis-mock behind the RedisLike interface UpstashLedger expects (tests only). */

import type RedisMock from "ioredis-mock";
import type { RedisLike } from "../src/ledger/upstash.js";

type Mock = InstanceType<typeof RedisMock>;

export class IoredisMockAdapter implements RedisLike {
  constructor(private readonly r: Mock) {}
  evalsha(sha: string, keys: string[], args: string[]) {
    return this.r.evalsha(sha, keys.length, ...keys, ...args) as Promise<unknown>;
  }
  eval(script: string, keys: string[], args: string[]) {
    return this.r.eval(script, keys.length, ...keys, ...args) as Promise<unknown>;
  }
  get(key: string) {
    return this.r.get(key);
  }
  mget(keys: string[]) {
    return this.r.mget(...keys) as Promise<unknown[]>;
  }
  set(key: string, value: string) {
    return this.r.set(key, value);
  }
  smembers(key: string) {
    return this.r.smembers(key);
  }
  sadd(key: string, member: string) {
    return this.r.sadd(key, member);
  }
  async hgetall(key: string) {
    const value = await this.r.hgetall(key);
    return value && Object.keys(value).length > 0 ? value : null;
  }
  hset(key: string, values: Record<string, string>) {
    return this.r.hset(key, values);
  }
  exists(key: string) {
    return this.r.exists(key);
  }
  lrange(key: string, start: number, stop: number) {
    return this.r.lrange(key, start, stop);
  }
  zrangeWithScores(key: string) {
    return this.r.zrange(key, 0, -1, "WITHSCORES");
  }
  async scan(cursor: string, match: string, count: number): Promise<[string, string[]]> {
    const [next, keys] = await this.r.scan(cursor, "MATCH", match, "COUNT", count);
    return [String(next), keys];
  }
}
