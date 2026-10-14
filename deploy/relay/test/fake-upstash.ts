/**
 * A stand-in for Upstash's REST API over ioredis-mock, so the real @upstash/redis client
 * (its request shape, pipelining, base64 replies and raw HGETALL lists) is exercised
 * offline. It speaks the documented protocol: POST <url> with a JSON command array, or
 * POST <url>/pipeline with a list of them; replies are {"result"} or {"error"}, with
 * strings base64-encoded when the client sends "Upstash-Encoding: base64".
 *
 * It also counts what Upstash bills. Upstash counts every command it is sent, and every
 * redis.call a script makes as one more (its rate-limit docs list one EVAL that runs INCR
 * and PEXPIRE as three commands). So `billed` is the commands sent plus the commands each
 * script ran inside; `requests` is HTTP round trips (a pipeline is one). Scripts live in a
 * server-side cache, as on Upstash: EVAL and SCRIPT LOAD add to it, EVALSHA of a script
 * it does not hold fails with NOSCRIPT, and flushScripts() empties it.
 */

import { createHash } from "node:crypto";
import http from "node:http";
import type { AddressInfo } from "node:net";
import RedisMock from "ioredis-mock";
import type { RedisLike } from "../src/ledger/upstash.js";

type Mock = InstanceType<typeof RedisMock>;

/** One command the client sent, and the commands it ran inside a script. */
export interface SentCommand {
  name: string;
  inner: string[];
}

export interface FakeUpstash {
  url: string;
  token: string;
  /** Names of the commands sent, in order (lower case). */
  commands: string[];
  /** Every command sent, with what it ran inside a script. */
  sent: SentCommand[];
  /** HTTP requests received (a pipeline counts once). */
  readonly requests: number;
  /** Commands Upstash would bill: those sent plus those run inside scripts. */
  readonly billed: number;
  /**
   * The same database and the same count without HTTP, for runs too long to send over the
   * stand-in (a whole judge build). Commands sent this way are billed but are not requests.
   */
  direct: RedisLike;
  /** Forgets the counts (not the data or the scripts). */
  reset(): void;
  /** Empties the script cache, as a SCRIPT FLUSH or a failover would. */
  flushScripts(): void;
  close(): Promise<void>;
}

function encode(value: unknown, base64: boolean): unknown {
  if (!base64) return value;
  if (typeof value === "string") return Buffer.from(value, "utf8").toString("base64");
  if (Array.isArray(value)) return value.map((item) => encode(item, base64));
  return value;
}

const sha1 = (script: string) => createHash("sha1").update(script).digest("hex");

/**
 * The script inside a wrapper that records each redis.call / redis.pcall it makes and
 * hands them back with the result (or the error), so the fake can count them. Only ever
 * run on ioredis-mock, whose `redis` table can be changed.
 */
function counted(script: string): string {
  return `local __call, __pcall, __seen = redis.call, redis.pcall, {}
redis.call = function(name, ...) __seen[#__seen + 1] = string.lower(name) return __call(name, ...) end
redis.pcall = function(name, ...) __seen[#__seen + 1] = string.lower(name) return __pcall(name, ...) end
local __ok, __result = pcall(function()
${script}
end)
if __ok then return {'__counted', table.concat(__seen, ' '), __result} end
return {'__counted', table.concat(__seen, ' '), '__error', tostring(__result)}
`;
}

class ScriptError extends Error {}

export async function startFakeUpstash(): Promise<FakeUpstash> {
  const redis = new RedisMock();
  const token = "fake-upstash-token";
  const commands: string[] = [];
  const sent: SentCommand[] = [];
  const scripts = new Map<string, string>();
  let requests = 0;

  async function runScript(script: string, numKeys: string, keysAndArgs: string[], record: SentCommand): Promise<unknown> {
    const reply = (await redis.eval(counted(script), Number(numKeys), ...keysAndArgs)) as unknown[];
    const seen = String(reply[1] ?? "");
    record.inner = seen ? seen.split(" ") : [];
    if (reply[2] === "__error") throw new ScriptError(String(reply[3]));
    return reply[2] ?? null;
  }

  async function run(command: unknown[]): Promise<{ result?: unknown; error?: string }> {
    const [name, ...rest] = command.map((part) => String(part));
    const lower = (name ?? "").toLowerCase();
    const record: SentCommand = { name: lower, inner: [] };
    commands.push(lower);
    sent.push(record);
    try {
      if (lower === "eval" || lower === "eval_ro") {
        const [script, numKeys, ...keysAndArgs] = rest;
        scripts.set(sha1(script!), script!);
        return { result: await runScript(script!, numKeys!, keysAndArgs, record) };
      }
      if (lower === "evalsha" || lower === "evalsha_ro") {
        const [sha, numKeys, ...keysAndArgs] = rest;
        const script = scripts.get(String(sha).toLowerCase());
        if (script === undefined) return { error: "NOSCRIPT No matching script. Please use EVAL." };
        return { result: await runScript(script, numKeys!, keysAndArgs, record) };
      }
      if (lower === "script") {
        const [sub, ...args] = rest;
        const what = (sub ?? "").toLowerCase();
        if (what === "load") {
          const sha = sha1(args[0]!);
          scripts.set(sha, args[0]!);
          return { result: sha };
        }
        if (what === "exists") return { result: args.map((sha) => (scripts.has(sha.toLowerCase()) ? 1 : 0)) };
        if (what === "flush") {
          scripts.clear();
          return { result: "OK" };
        }
        return { error: `ERR unknown subcommand '${sub}'` };
      }
      if (lower === "hgetall") {
        // Redis replies with a flat field/value list; ioredis turns it into an object.
        const object = (await redis.hgetall(rest[0]!)) as Record<string, string>;
        return { result: Object.entries(object).flat() };
      }
      if (lower === "hset") {
        const [key, ...pairs] = rest;
        const values: Record<string, string> = {};
        for (let i = 0; i + 1 < pairs.length; i += 2) values[pairs[i]!] = pairs[i + 1]!;
        return { result: await redis.hset(key!, values) };
      }
      const method = (redis as unknown as Record<string, (...args: string[]) => Promise<unknown>>)[lower];
      if (typeof method !== "function") return { error: `ERR unknown command '${name}'` };
      return { result: await method.apply(redis, rest) };
    } catch (err) {
      return { error: `ERR ${err instanceof Error ? err.message : String(err)}` };
    }
  }

  const server = http.createServer(async (req, res) => {
    let raw = "";
    for await (const chunk of req) raw += chunk;
    const reply = (status: number, body: unknown) => {
      res.writeHead(status, { "content-type": "application/json" });
      res.end(JSON.stringify(body));
    };
    if (req.headers.authorization !== `Bearer ${token}`) return reply(401, { error: "Unauthorized" });
    requests += 1;
    const base64 = String(req.headers["upstash-encoding"] ?? "").toLowerCase() === "base64";
    const body = JSON.parse(raw) as unknown[];
    const path = (req.url ?? "/").split("?")[0]!;
    if (path.endsWith("/pipeline") || path.endsWith("/multi-exec")) {
      const out = [];
      for (const command of body as unknown[][]) {
        const answer = await run(command);
        out.push(answer.error ? { error: answer.error } : { result: encode(answer.result, base64) });
      }
      return reply(200, out);
    }
    const answer = await run(body);
    if (answer.error) return reply(400, { error: answer.error });
    return reply(200, { result: encode(answer.result, base64) });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", () => resolve()));
  const port = (server.address() as AddressInfo).port;
  const call = async (...command: unknown[]): Promise<unknown> => {
    const answer = await run(command);
    if (answer.error) throw new Error(answer.error);
    return answer.result;
  };
  const direct: RedisLike = {
    evalsha: (sha, keys, args) => call("evalsha", sha, keys.length, ...keys, ...args),
    eval: (script, keys, args) => call("eval", script, keys.length, ...keys, ...args),
    get: (key) => call("get", key),
    mget: async (keys) => (await call("mget", ...keys)) as unknown[],
    set: (key, value) => call("set", key, value),
    smembers: (key) => call("smembers", key),
    sadd: (key, member) => call("sadd", key, member),
    hgetall: async (key) => {
      const flat = (await call("hgetall", key)) as unknown[];
      if (flat.length === 0) return null;
      const out: Record<string, unknown> = {};
      for (let i = 0; i + 1 < flat.length; i += 2) out[String(flat[i])] = flat[i + 1];
      return out;
    },
    hset: (key, values) => call("hset", key, ...Object.entries(values).flat()),
    exists: (key) => call("exists", key),
    lrange: (key, start, stop) => call("lrange", key, start, stop),
    zrangeWithScores: (key) => call("zrange", key, 0, -1, "WITHSCORES"),
    scan: async (cursor, match, count) => {
      const [next, keys] = (await call("scan", cursor, "MATCH", match, "COUNT", count)) as [unknown, unknown[]];
      return [String(next), keys.map(String)];
    },
  };
  return {
    url: `http://127.0.0.1:${port}`,
    token,
    commands,
    sent,
    direct,
    get requests() {
      return requests;
    },
    get billed() {
      return sent.reduce((sum, command) => sum + 1 + command.inner.length, 0);
    },
    reset() {
      commands.length = 0;
      sent.length = 0;
      requests = 0;
    },
    flushScripts() {
      scripts.clear();
    },
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  };
}
