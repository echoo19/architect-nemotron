/** The ledger's Lua scripts as they were before the move to one string per record (test fixture). */

/**
 * Redis Lua for the shared ledger. Each script touches only the keys it is given (Upstash
 * and cluster safe) and runs atomically. The rules and their order match state.ts exactly;
 * test/ledger-contract.ts holds both stores to the same cases.
 *
 * Portability rules (Redis runs Lua 5.1, where every number is a double; the offline tests
 * run Lua 5.3 on fengari, whose integers are 32-bit):
 *   - every number read is turned into a float by n() (a no-op on 5.1), so sums of
 *     nano-dollars stay exact up to 2^53 on both;
 *   - nothing computed is passed back into a Redis command: increments, deadlines and
 *     indexes arrive as ARGV strings, and a negative hold is built by string concatenation;
 *   - a missing hash field is tested with tonumber(), which is nil for both Redis' false
 *     and the mock's null.
 *
 * Keys (prefix P, default "relay"):
 *   P:g                     hash   cap, spent, reserved, disabled, opening
 *   P:c:<hash>              hash   label, cap_total, cap_daily, freeze_pct, share_pct, max_inflight,
 *                                  revoked, frozen, created, spent, reserved, requests, inflight
 *   P:cd:<hash>:<day>       hash   spent, reserved, requests             (expires after 120 days)
 *   P:u:<day>               hash   <model>|n|in|out|rsn|cost|usage|full|released|reconcile (120 days)
 *   P:rl:<model>:<min>      hash   req, tok   the model's budget        (expires after 3 minutes)
 *   P:rlc:<hash>:<model>:<min> hash req, tok  one code's share of it    (expires after 3 minutes)
 *   P:res                   hash   reservation id -> hash|day|model|amount|rateTokens|window|created
 *   P:resq                  zset   reservation id by deadline (ms)
 *   P:ev                    list   settlement events, newest first, trimmed
 *   P:codes                 set    code hashes
 *   P:labels                hash   label -> code hash
 */

import { MAX_SETTLE_NANO, MAX_SETTLE_TOKENS } from "../../../src/ledger/types.js";

const NUMBER = `
local function n(v, default)
  local x = tonumber(v)
  if x == nil then x = default or 0 end
  return x * 1.0
end
`;

/**
 * KEYS: g, c, cd, rl (this minute), rl (previous minute), res, resq, rlc (this minute), rlc (previous minute)
 * ARGV: id, record, amount, rateTokens, rpm, tpm, elapsedMs, deadlineMs, nowMs, dayTtlS, limiterTtlMs,
 *       defaultSharePct, defaultMaxInFlight
 */
export const RESERVE_LUA = `${NUMBER}
local id = ARGV[1]
if redis.call('HEXISTS', KEYS[6], id) == 1 then return {'ok', 0} end
local g = redis.call('HMGET', KEYS[1], 'cap', 'spent', 'reserved', 'disabled')
if g[4] == '1' then return {'disabled'} end
local c = redis.call('HMGET', KEYS[2], 'cap_total', 'cap_daily', 'freeze_pct', 'revoked', 'spent', 'reserved',
  'frozen', 'share_pct', 'max_inflight', 'inflight')
if tonumber(c[1]) == nil then return {'unknown_code'} end
if c[4] == '1' then return {'revoked'} end
if c[7] == '1' then return {'frozen'} end
local gcap = n(g[1])
if gcap <= 0 then return {'global_unset'} end
local amount = n(ARGV[3])
local gused = n(g[2]) + n(g[3])
local ccap = n(c[1])
local cused = n(c[5]) + n(c[6])
if cused + amount > ccap then return {'cap_total', ccap - cused, ccap} end
local d = redis.call('HMGET', KEYS[3], 'spent', 'reserved')
local dcap = n(c[2])
local dused = n(d[1]) + n(d[2])
if dused + amount > dcap then return {'cap_daily', dcap - dused, dcap} end
local own = ccap - cused
if dcap - dused < own then own = dcap - dused end
if gused + amount > gcap then return {'global_cap', own} end
local pct = n(c[3], 100)
if pct < 100 and (gused + amount) * 100 > gcap * pct then return {'global_freeze', own} end
local inflight = n(c[10])
local maxin = n(c[9], n(ARGV[13]))
if inflight >= maxin then return {'in_flight', inflight, maxin} end
local W = 60000.0
local e = n(ARGV[7])
local need = n(ARGV[4])
local rpm = n(ARGV[5])
local tpm = n(ARGV[6])
local cur = redis.call('HMGET', KEYS[4], 'req', 'tok')
local prev = redis.call('HMGET', KEYS[5], 'req', 'tok')
local creq = n(cur[1])
local ctok = n(cur[2])
local preq = n(prev[1])
local ptok = n(prev[2])
if creq * W + preq * (W - e) + W > rpm * W then return {'rate_limited', 'model', 'rpm', creq, preq, e, rpm, 1, inflight} end
local busy = ctok * W + ptok * (W - e)
if busy > 0 and busy + need * W > tpm * W then return {'rate_limited', 'model', 'tpm', ctok, ptok, e, tpm, need, inflight} end
local share = n(c[8], n(ARGV[12]))
local crpm = math.floor(rpm * share / 100)
if crpm < 1 then crpm = 1 end
local ctpm = math.floor(tpm * share / 100)
if ctpm < 1 then ctpm = 1 end
local mine = redis.call('HMGET', KEYS[8], 'req', 'tok')
local before = redis.call('HMGET', KEYS[9], 'req', 'tok')
local mreq = n(mine[1])
local mtok = n(mine[2])
local breq = n(before[1])
local btok = n(before[2])
if mreq * W + breq * (W - e) + W > crpm * W then return {'rate_limited', 'code', 'rpm', mreq, breq, e, crpm, 1, inflight} end
local mbusy = mtok * W + btok * (W - e)
if mbusy + need * W > ctpm * W and not (inflight == 0 and mbusy <= ctpm * W) then
  return {'rate_limited', 'code', 'tpm', mtok, btok, e, ctpm, need, inflight}
end
redis.call('HINCRBY', KEYS[1], 'reserved', ARGV[3])
redis.call('HINCRBY', KEYS[2], 'reserved', ARGV[3])
redis.call('HINCRBY', KEYS[2], 'inflight', 1)
redis.call('HINCRBY', KEYS[3], 'reserved', ARGV[3])
redis.call('EXPIRE', KEYS[3], ARGV[10])
redis.call('HINCRBY', KEYS[4], 'req', 1)
redis.call('HINCRBY', KEYS[4], 'tok', ARGV[4])
redis.call('PEXPIRE', KEYS[4], ARGV[11])
redis.call('HINCRBY', KEYS[8], 'req', 1)
redis.call('HINCRBY', KEYS[8], 'tok', ARGV[4])
redis.call('PEXPIRE', KEYS[8], ARGV[11])
redis.call('HSET', KEYS[6], id, ARGV[2])
redis.call('ZADD', KEYS[7], ARGV[8], id)
return {'ok', redis.call('ZCOUNT', KEYS[7], '-inf', ARGV[9])}
`;

/**
 * KEYS: g, c, cd, u, rl (the reservation's minute), res, resq, ev, rlc (the reservation's minute)
 * ARGV: id, prefix, actual, promptTokens, completionTokens, reasoningTokens, kind,
 *       tokenCorrection, eventJson, lastEventIndex, dayTtlS, reconcile ('1' or '0')
 * Returns 1 when settled, 0 when the reservation was already settled. Every figure is
 * checked before anything is written, and the reservation is deleted last, so a failed
 * settlement leaves the hold in place for a sweep to book in full.
 */
export const SETTLE_LUA = `${NUMBER}
local function whole(v, lo, hi)
  if type(v) ~= 'string' or #v > 17 or not string.find(v, '^%-?%d+$') then return false end
  local x = tonumber(v)
  return x ~= nil and x >= lo and x <= hi
end
local maxn = ${MAX_SETTLE_NANO.toExponential()}
local maxt = ${MAX_SETTLE_TOKENS.toExponential()}
if not (whole(ARGV[3], 0, maxn) and whole(ARGV[4], 0, maxt) and whole(ARGV[5], 0, maxt)
  and whole(ARGV[6], 0, maxt) and whole(ARGV[8], -maxt, maxt)) then
  error('relay: settle figures out of range')
end
if ARGV[7] ~= 'usage' and ARGV[7] ~= 'full' and ARGV[7] ~= 'released' then error('relay: unknown settle kind') end
local rec = redis.call('HGET', KEYS[6], ARGV[1])
if not rec then return 0 end
local hash, day, model, amount, rate, win = string.match(rec, '^([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)|([^|]*)|')
if not hash then error('relay: unreadable reservation record') end
local p = ARGV[2]
if KEYS[2] ~= p .. ':c:' .. hash or KEYS[3] ~= p .. ':cd:' .. hash .. ':' .. day
  or KEYS[4] ~= p .. ':u:' .. day or KEYS[5] ~= p .. ':rl:' .. model .. ':' .. win
  or KEYS[9] ~= p .. ':rlc:' .. hash .. ':' .. model .. ':' .. win then
  error('relay: settle keys do not match the reservation')
end
local release = '-' .. amount
local held = amount ~= '0'
if held then redis.call('HINCRBY', KEYS[1], 'reserved', release) end
redis.call('HINCRBY', KEYS[1], 'spent', ARGV[3])
if redis.call('EXISTS', KEYS[2]) == 1 then
  if held then redis.call('HINCRBY', KEYS[2], 'reserved', release) end
  redis.call('HINCRBY', KEYS[2], 'spent', ARGV[3])
  redis.call('HINCRBY', KEYS[2], 'requests', 1)
  if n(redis.call('HGET', KEYS[2], 'inflight')) > 0 then redis.call('HINCRBY', KEYS[2], 'inflight', -1) end
  if n(ARGV[3]) > n(amount) then redis.call('HSET', KEYS[2], 'frozen', '1') end
end
if held then redis.call('HINCRBY', KEYS[3], 'reserved', release) end
redis.call('HINCRBY', KEYS[3], 'spent', ARGV[3])
redis.call('HINCRBY', KEYS[3], 'requests', 1)
redis.call('EXPIRE', KEYS[3], ARGV[11])
redis.call('HINCRBY', KEYS[4], model .. '|n', 1)
redis.call('HINCRBY', KEYS[4], model .. '|in', ARGV[4])
redis.call('HINCRBY', KEYS[4], model .. '|out', ARGV[5])
redis.call('HINCRBY', KEYS[4], model .. '|rsn', ARGV[6])
redis.call('HINCRBY', KEYS[4], model .. '|cost', ARGV[3])
redis.call('HINCRBY', KEYS[4], model .. '|' .. ARGV[7], 1)
if ARGV[12] == '1' then redis.call('HINCRBY', KEYS[4], model .. '|reconcile', 1) end
redis.call('EXPIRE', KEYS[4], ARGV[11])
if ARGV[8] ~= '0' then
  if redis.call('EXISTS', KEYS[5]) == 1 then redis.call('HINCRBY', KEYS[5], 'tok', ARGV[8]) end
  if redis.call('EXISTS', KEYS[9]) == 1 then redis.call('HINCRBY', KEYS[9], 'tok', ARGV[8]) end
end
redis.call('LPUSH', KEYS[8], ARGV[9])
redis.call('LTRIM', KEYS[8], 0, ARGV[10])
redis.call('HDEL', KEYS[6], ARGV[1])
redis.call('ZREM', KEYS[7], ARGV[1])
return 1
`;

/**
 * Claims reservations whose deadline is at or before the cutoff: each is pushed back to
 * requeueAtMs (so a second sweeper skips it) and returned for the caller to settle, as one
 * string of newline-separated id and record lines (a flat string travels the same through
 * Redis, REST and the mock, where a table built in a loop does not).
 * KEYS: res, resq   ARGV: cutoffMs, limit, requeueAtMs
 */
export const CLAIM_LUA = `
local ids = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', ARGV[1], 'LIMIT', 0, ARGV[2])
local out = {}
for i = 1, #ids do
  local rec = redis.call('HGET', KEYS[1], ids[i])
  if rec then
    redis.call('ZADD', KEYS[2], ARGV[3], ids[i])
    out[#out + 1] = ids[i] .. '\\n' .. rec
  else
    redis.call('ZREM', KEYS[2], ids[i])
  end
end
return table.concat(out, '\\n')
`;

/**
 * Sets the opening balance if it still is what the caller read, and moves spent by the
 * difference (computed by the caller, so no Lua number goes back into Redis).
 * KEYS: g   ARGV: expectedOpening, newOpening, delta
 */
export const OPENING_LUA = `
local was = redis.call('HGET', KEYS[1], 'opening')
if not was then was = '0' end
if was ~= ARGV[1] then return {'changed'} end
redis.call('HSET', KEYS[1], 'opening', ARGV[2])
if ARGV[3] ~= '0' then redis.call('HINCRBY', KEYS[1], 'spent', ARGV[3]) end
return {'ok'}
`;
