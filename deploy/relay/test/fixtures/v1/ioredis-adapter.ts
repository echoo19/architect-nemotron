/** ioredis-mock behind the interface the old Upstash ledger expects (test fixture). */

import type RedisMock from "ioredis-mock";
import type { RedisLikeV1 } from "./upstash.js";

type Mock = InstanceType<typeof RedisMock>;

export class IoredisMockAdapterV1 implements RedisLikeV1 {
  constructor(private readonly r: Mock) {}
  eval(script: string, keys: string[], args: string[]) {
    return this.r.eval(script, keys.length, ...keys, ...args) as Promise<unknown>;
  }
  async hgetall(key: string) {
    const value = await this.r.hgetall(key);
    return value && Object.keys(value).length > 0 ? value : null;
  }
  hset(key: string, values: Record<string, string>) {
    return this.r.hset(key, values);
  }
  hsetnx(key: string, field: string, value: string) {
    return this.r.hsetnx(key, field, value);
  }
  hget(key: string, field: string) {
    return this.r.hget(key, field);
  }
  hdel(key: string, field: string) {
    return this.r.hdel(key, field);
  }
  hlen(key: string) {
    return this.r.hlen(key);
  }
  exists(key: string) {
    return this.r.exists(key);
  }
  sadd(key: string, member: string) {
    return this.r.sadd(key, member);
  }
  smembers(key: string) {
    return this.r.smembers(key);
  }
  zcount(key: string, min: string, max: string) {
    return this.r.zcount(key, min, max);
  }
  lrange(key: string, start: number, stop: number) {
    return this.r.lrange(key, start, stop);
  }
}
