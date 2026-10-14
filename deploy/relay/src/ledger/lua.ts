/**
 * Redis Lua for the shared ledger. Each script touches only the keys it is given (Upstash
 * and cluster safe) and runs atomically. The rules and their order match state.ts exactly;
 * test/ledger-contract.ts holds both stores to the same cases.
 *
 * Upstash bills every command a script runs, plus the EVALSHA itself. So each record is one
 * string, and a script reads all of its records with one MGET and writes them back with one
 * MSET: a reservation costs 3 commands, a settlement 3, a refusal 2. Day records carry no
 * expiry: MSET drops a key's expiry as SET does, so an EXPIRE would last only until the
 * key's next write.
 *
 * Portability rules (Redis runs Lua 5.1, where every number is a double; the offline tests
 * run Lua 5.3 on fengari, whose integers are 32-bit):
 *   - every number read is turned into a float by n(), so sums of nano-dollars stay exact
 *     up to 2^53 on both, and every number written goes through fmt() ('%.0f');
 *   - a missing key is whatever MGET returns that is not a string (false on Redis, a null
 *     proxy on the mock), so str() is the only test for it;
 *   - replies are literal tables or strings: the mock cannot return a table built in a loop.
 *
 * Keys (prefix P, default "relay"); every record is "|"-separated tokens:
 *   P:v2:g                string  2|cap|spent|reserved|disabled|opening
 *                                 |L|(model|minute|req|tok)*L        the models' limiter windows
 *                                 |R|(id|deadline|hash|day|model|amount|rateTokens|window|created)*R
 *                                                                    the calls holding a reservation
 *   P:v2:c:<hash>         string  2|cap_total|cap_daily|freeze_pct|share_pct|max_inflight|revoked
 *                                 |frozen|created|spent|reserved|requests|inflight|label
 *                                 |L|(model|minute|req|tok)*L        this code's share of them
 *                                 (share_pct and max_inflight are "-" when unset; the label is
 *                                 percent-encoded)
 *   P:v2:cd:<hash>:<day>  string  spent|reserved|requests                       (kept)
 *   P:v2:u:<day>          string  M|(model|n|in|out|rsn|cost|usage|full|released|reconcile)*M
 *                                                                               (kept)
 *   P:v2:ev               string  settlement events as JSON lines, newest first, trimmed
 *   P:v2:codes            set     code hashes
 *   P:v2:labels           hash    label -> code hash
 *   P:v2:migrated         string  what the copy from the old layout left (migrate.ts; --force checks it)
 *
 * A limiter list keeps each model's three newest minutes, whatever the writer's clock says.
 * The sliding window reads two; the third lets a host whose clock trails by a minute still
 * read its own two, and a host whose clock runs ahead cannot drop the minutes the others count.
 */

import { MAX_SETTLE_NANO, MAX_SETTLE_TOKENS } from "./types.js";

const COMMON = `
local function n(v, default)
  local x = tonumber(v)
  if x == nil then x = default or 0 end
  return x * 1.0
end
local function str(v)
  if type(v) == 'string' then return v end
  return nil
end
local function fmt(x)
  if x == 0 then return '0' end
  return string.format('%.0f', x)
end
local function split(s)
  local t = {}
  for token in string.gmatch(s .. '|', '([^|]*)|') do t[#t + 1] = token end
  return t
end
local function read_lim(t, i, list)
  local count = n(t[i])
  i = i + 1
  for _ = 1, count do
    list[#list + 1] = {model = t[i], win = n(t[i + 1]), req = n(t[i + 2]), tok = n(t[i + 3])}
    i = i + 4
  end
  return i
end
local function write_lim(out, list)
  out[#out + 1] = fmt(#list)
  for _, e in ipairs(list) do
    out[#out + 1] = e.model
    out[#out + 1] = fmt(e.win)
    out[#out + 1] = fmt(e.req)
    out[#out + 1] = fmt(e.tok)
  end
end
local function lim_get(list, model, win)
  for _, e in ipairs(list) do
    if e.model == model and e.win == win then return e end
  end
  return nil
end
local function lim_add(list, model, win, tok)
  local e = lim_get(list, model, win)
  if not e then
    e = {model = model, win = win, req = 0.0, tok = 0.0}
    list[#list + 1] = e
  end
  e.req = e.req + 1
  e.tok = e.tok + tok
  local mine, kept = {}, {}
  for _, x in ipairs(list) do
    if x.model == model then mine[#mine + 1] = x else kept[#kept + 1] = x end
  end
  table.sort(mine, function(a, b) return a.win > b.win end)
  for i = 1, math.min(#mine, 3) do kept[#kept + 1] = mine[i] end
  return kept
end
local function read_global(raw)
  local g = {cap = 0.0, spent = 0.0, reserved = 0.0, disabled = false, opening = 0.0, lim = {}, res = {}, order = {}}
  if not raw then return g end
  local t = split(raw)
  if t[1] ~= '2' then error('relay: unreadable global record') end
  g.cap, g.spent, g.reserved = n(t[2]), n(t[3]), n(t[4])
  g.disabled = t[5] == '1'
  g.opening = n(t[6])
  local i = read_lim(t, 7, g.lim)
  local count = n(t[i])
  i = i + 1
  for _ = 1, count do
    local id = t[i]
    g.res[id] = {deadline = n(t[i + 1]), hash = t[i + 2], day = t[i + 3], model = t[i + 4], amount = t[i + 5],
      rate = t[i + 6], window = t[i + 7], created = t[i + 8]}
    g.order[#g.order + 1] = id
    i = i + 9
  end
  return g
end
local function write_global(g)
  local out = {'2', fmt(g.cap), fmt(g.spent), fmt(g.reserved), g.disabled and '1' or '0', fmt(g.opening)}
  write_lim(out, g.lim)
  local ids = {}
  for _, id in ipairs(g.order) do
    if g.res[id] then ids[#ids + 1] = id end
  end
  out[#out + 1] = fmt(#ids)
  for _, id in ipairs(ids) do
    local r = g.res[id]
    out[#out + 1] = id
    out[#out + 1] = fmt(r.deadline)
    out[#out + 1] = r.hash
    out[#out + 1] = r.day
    out[#out + 1] = r.model
    out[#out + 1] = r.amount
    out[#out + 1] = r.rate
    out[#out + 1] = r.window
    out[#out + 1] = r.created
  end
  return table.concat(out, '|')
end
local function read_code(raw)
  if not raw then return nil end
  local t = split(raw)
  if t[1] ~= '2' then error('relay: unreadable code record') end
  local c = {cap_total = n(t[2]), cap_daily = n(t[3]), freeze = n(t[4], 100), share = t[5], maxin = t[6],
    revoked = t[7] == '1', frozen = t[8] == '1', created = t[9], spent = n(t[10]), reserved = n(t[11]),
    requests = n(t[12]), inflight = n(t[13]), label = t[14], lim = {}}
  read_lim(t, 15, c.lim)
  return c
end
local function write_code(c)
  local out = {'2', fmt(c.cap_total), fmt(c.cap_daily), fmt(c.freeze), c.share, c.maxin, c.revoked and '1' or '0',
    c.frozen and '1' or '0', c.created, fmt(c.spent), fmt(c.reserved), fmt(c.requests), fmt(c.inflight), c.label}
  write_lim(out, c.lim)
  return table.concat(out, '|')
end
local function read_day(raw)
  local t = split(raw or '0|0|0')
  return {spent = n(t[1]), reserved = n(t[2]), requests = n(t[3])}
end
local function write_day(d)
  return fmt(d.spent) .. '|' .. fmt(d.reserved) .. '|' .. fmt(d.requests)
end
`;

/**
 * KEYS: g, c, cd (the reservation's day)
 * ARGV: id, day, model, amount, rateTokens, rpm, tpm, elapsedMs, window, nowMs, deadlineMs,
 *       defaultSharePct, defaultMaxInFlight, hash
 * Checks in state.ts's order; a refusal writes nothing. Every reply ends with the number of
 * holds past their deadline, a refusal's too, so whoever sees them can sweep them: a code
 * whose calls at once are all held by a dead instance is otherwise locked out.
 */
export const RESERVE_LUA = `${COMMON}
local id = ARGV[1]
local v = redis.call('MGET', KEYS[1], KEYS[2], KEYS[3])
local graw, craw, draw = str(v[1]), str(v[2]), str(v[3])
local g = read_global(graw)
if g.res[id] then return {'ok', 0} end
local now = n(ARGV[10])
local expired = 0
for _, rid in ipairs(g.order) do
  local r = g.res[rid]
  if r and r.deadline <= now then expired = expired + 1 end
end
if g.disabled then return {'disabled', expired} end
local c = read_code(craw)
if not c then return {'unknown_code', expired} end
if c.revoked then return {'revoked', expired} end
if c.frozen then return {'frozen', expired} end
local gcap = g.cap
if gcap <= 0 then return {'global_unset', expired} end
local amount = n(ARGV[4])
local gused = g.spent + g.reserved
local ccap = c.cap_total
local cused = c.spent + c.reserved
if cused + amount > ccap then return {'cap_total', ccap - cused, ccap, expired} end
local d = read_day(draw)
local dcap = c.cap_daily
local dused = d.spent + d.reserved
if dused + amount > dcap then return {'cap_daily', dcap - dused, dcap, expired} end
local own = ccap - cused
if dcap - dused < own then own = dcap - dused end
if gused + amount > gcap then return {'global_cap', own, expired} end
local pct = c.freeze
if pct < 100 and (gused + amount) * 100 > gcap * pct then return {'global_freeze', own, expired} end
local inflight = c.inflight
local maxin = n(c.maxin, n(ARGV[13]))
if inflight >= maxin then return {'in_flight', inflight, maxin, expired} end
local W = 60000.0
local model = ARGV[3]
local win = n(ARGV[9])
local e = n(ARGV[8])
local need = n(ARGV[5])
local rpm = n(ARGV[6])
local tpm = n(ARGV[7])
local zero = {req = 0.0, tok = 0.0}
local cur = lim_get(g.lim, model, win) or zero
local prev = lim_get(g.lim, model, win - 1) or zero
local creq, ctok, preq, ptok = cur.req, cur.tok, prev.req, prev.tok
if creq * W + preq * (W - e) + W > rpm * W then return {'rate_limited', 'model', 'rpm', creq, preq, e, rpm, 1, inflight, expired} end
local busy = ctok * W + ptok * (W - e)
if busy > 0 and busy + need * W > tpm * W then return {'rate_limited', 'model', 'tpm', ctok, ptok, e, tpm, need, inflight, expired} end
local share = n(c.share, n(ARGV[12]))
local crpm = math.floor(rpm * share / 100)
if crpm < 1 then crpm = 1 end
local ctpm = math.floor(tpm * share / 100)
if ctpm < 1 then ctpm = 1 end
local mine = lim_get(c.lim, model, win) or zero
local before = lim_get(c.lim, model, win - 1) or zero
local mreq, mtok, breq, btok = mine.req, mine.tok, before.req, before.tok
if mreq * W + breq * (W - e) + W > crpm * W then return {'rate_limited', 'code', 'rpm', mreq, breq, e, crpm, 1, inflight, expired} end
local mbusy = mtok * W + btok * (W - e)
if mbusy + need * W > ctpm * W and not (inflight == 0 and mbusy <= ctpm * W) then
  return {'rate_limited', 'code', 'tpm', mtok, btok, e, ctpm, need, inflight, expired}
end
g.reserved = g.reserved + amount
g.lim = lim_add(g.lim, model, win, need)
g.res[id] = {deadline = n(ARGV[11]), hash = ARGV[14], day = ARGV[2], model = model, amount = ARGV[4], rate = ARGV[5],
  window = ARGV[9], created = ARGV[10]}
g.order[#g.order + 1] = id
c.reserved = c.reserved + amount
c.inflight = inflight + 1
c.lim = lim_add(c.lim, model, win, need)
d.reserved = d.reserved + amount
redis.call('MSET', KEYS[1], write_global(g), KEYS[2], write_code(c), KEYS[3], write_day(d))
return {'ok', expired}
`;

/**
 * KEYS: g, c, cd, u (the reservation's day), ev
 * ARGV: id, prefix, actual, promptTokens, completionTokens, reasoningTokens, kind,
 *       tokenCorrection, eventJson, eventsKept, reconcile ('1' or '0'), model, window
 * Returns 1 when settled, 0 when the reservation was already settled. Every figure and the
 * reservation's keys are checked before anything is written, and everything is written in
 * one MSET, so a failed settlement leaves the hold in place for a sweep to book in full.
 */
export const SETTLE_LUA = `${COMMON}
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
local kind = ARGV[7]
if kind ~= 'usage' and kind ~= 'full' and kind ~= 'released' then error('relay: unknown settle kind') end
local id = ARGV[1]
local v = redis.call('MGET', KEYS[1], KEYS[2], KEYS[3], KEYS[4], KEYS[5])
local graw, craw, draw, uraw, ring = str(v[1]), str(v[2]), str(v[3]), str(v[4]), str(v[5])
local g = read_global(graw)
local r = g.res[id]
if not r then return 0 end
local p = ARGV[2]
if KEYS[2] ~= p .. ':v2:c:' .. r.hash or KEYS[3] ~= p .. ':v2:cd:' .. r.hash .. ':' .. r.day
  or KEYS[4] ~= p .. ':v2:u:' .. r.day or ARGV[12] ~= r.model or ARGV[13] ~= r.window then
  error('relay: settle keys do not match the reservation')
end
local amount = n(r.amount)
local actual = n(ARGV[3])
local correction = n(ARGV[8])
local win = n(r.window)
g.reserved = g.reserved - amount
g.spent = g.spent + actual
g.res[id] = nil
if correction ~= 0 then
  local e = lim_get(g.lim, r.model, win)
  if e then e.tok = e.tok + correction end
end
local c = read_code(craw)
if c then
  c.reserved = c.reserved - amount
  c.spent = c.spent + actual
  c.requests = c.requests + 1
  if c.inflight > 0 then c.inflight = c.inflight - 1 end
  if actual > amount then c.frozen = true end
  if correction ~= 0 then
    local e = lim_get(c.lim, r.model, win)
    if e then e.tok = e.tok + correction end
  end
end
local d = read_day(draw)
d.reserved = d.reserved - amount
d.spent = d.spent + actual
d.requests = d.requests + 1
local fields = {'n', 'in', 'out', 'rsn', 'cost', 'usage', 'full', 'released', 'reconcile'}
local rows, order = {}, {}
if uraw then
  local t = split(uraw)
  local count = n(t[1])
  local i = 2
  for _ = 1, count do
    local row = {}
    for k, name in ipairs(fields) do row[name] = n(t[i + k]) end
    rows[t[i]] = row
    order[#order + 1] = t[i]
    i = i + 1 + #fields
  end
end
local row = rows[r.model]
if not row then
  row = {}
  for _, name in ipairs(fields) do row[name] = 0.0 end
  rows[r.model] = row
  order[#order + 1] = r.model
end
row['n'] = row['n'] + 1
row['in'] = row['in'] + n(ARGV[4])
row['out'] = row['out'] + n(ARGV[5])
row['rsn'] = row['rsn'] + n(ARGV[6])
row['cost'] = row['cost'] + actual
row[kind] = row[kind] + 1
if ARGV[11] == '1' then row['reconcile'] = row['reconcile'] + 1 end
local usage = {fmt(#order)}
for _, model in ipairs(order) do
  usage[#usage + 1] = model
  for _, name in ipairs(fields) do usage[#usage + 1] = fmt(rows[model][name]) end
end
local events = ARGV[9] .. '\\n' .. (ring or '')
local keep = n(ARGV[10])
local pos, lines = 0, 0
while lines < keep do
  local nl = string.find(events, '\\n', pos + 1, true)
  if not nl then break end
  lines = lines + 1
  pos = nl
end
if lines >= keep then events = string.sub(events, 1, pos) end
if c then
  redis.call('MSET', KEYS[1], write_global(g), KEYS[2], write_code(c), KEYS[3], write_day(d),
    KEYS[4], table.concat(usage, '|'), KEYS[5], events)
else
  redis.call('MSET', KEYS[1], write_global(g), KEYS[3], write_day(d), KEYS[4], table.concat(usage, '|'), KEYS[5], events)
end
return 1
`;

/**
 * Claims reservations whose deadline is at or before the cutoff, oldest first: each is
 * pushed back to requeueAtMs (so a second sweeper skips it) and returned for the caller to
 * settle, as one string of newline-separated id and record lines.
 * KEYS: g   ARGV: cutoffMs, limit, requeueAtMs
 */
export const CLAIM_LUA = `${COMMON}
local raw = str(redis.call('GET', KEYS[1]))
if not raw then return '' end
local g = read_global(raw)
local cutoff = n(ARGV[1])
local due = {}
for _, id in ipairs(g.order) do
  if g.res[id].deadline <= cutoff then due[#due + 1] = id end
end
if #due == 0 then return '' end
table.sort(due, function(a, b)
  local x, y = g.res[a].deadline, g.res[b].deadline
  if x ~= y then return x < y end
  return a < b
end)
local out = {}
local limit = n(ARGV[2])
for i = 1, #due do
  if i > limit then break end
  local id = due[i]
  local r = g.res[id]
  r.deadline = n(ARGV[3])
  out[#out + 1] = id .. '\\n' .. r.hash .. '|' .. r.day .. '|' .. r.model .. '|' .. r.amount .. '|' .. r.rate .. '|' .. r.window .. '|' .. r.created
end
redis.call('SET', KEYS[1], write_global(g))
return table.concat(out, '\\n')
`;

/**
 * Sets the opening balance if it still is what the caller read, and moves spent by the
 * difference. KEYS: g   ARGV: expectedOpening, newOpening, delta
 */
export const OPENING_LUA = `${COMMON}
local g = read_global(str(redis.call('GET', KEYS[1])))
if fmt(g.opening) ~= ARGV[1] then return {'changed'} end
g.opening = n(ARGV[2])
g.spent = g.spent + n(ARGV[3])
redis.call('SET', KEYS[1], write_global(g))
return {'ok'}
`;

/** Sets the global cap and the kill switch ('' keeps one). KEYS: g   ARGV: cap, disabled */
export const GLOBAL_LUA = `${COMMON}
local g = read_global(str(redis.call('GET', KEYS[1])))
if ARGV[1] ~= '' then g.cap = n(ARGV[1]) end
if ARGV[2] ~= '' then g.disabled = ARGV[2] == '1' end
redis.call('SET', KEYS[1], write_global(g))
return 1
`;

/**
 * Changes a code's caps and switches ('' keeps a field). Returns 0 if there is no such code.
 * KEYS: c   ARGV: capTotal, capDaily, freezePct, sharePct, maxInFlight, revoked, frozen
 */
export const CODE_LUA = `${COMMON}
local c = read_code(str(redis.call('GET', KEYS[1])))
if not c then return 0 end
if ARGV[1] ~= '' then c.cap_total = n(ARGV[1]) end
if ARGV[2] ~= '' then c.cap_daily = n(ARGV[2]) end
if ARGV[3] ~= '' then c.freeze = n(ARGV[3]) end
if ARGV[4] ~= '' then c.share = ARGV[4] end
if ARGV[5] ~= '' then c.maxin = ARGV[5] end
if ARGV[6] ~= '' then c.revoked = ARGV[6] == '1' end
if ARGV[7] ~= '' then c.frozen = ARGV[7] == '1' end
redis.call('SET', KEYS[1], write_code(c))
return 1
`;

/**
 * Adds a code unless it or its label exists. KEYS: c, labels, codes   ARGV: hash, label, record
 * Returns 'ok', 'exists' or 'taken'.
 */
export const ADD_CODE_LUA = `
if redis.call('EXISTS', KEYS[1]) == 1 then return 'exists' end
if redis.call('HSETNX', KEYS[2], ARGV[2], ARGV[1]) == 0 then return 'taken' end
redis.call('SET', KEYS[1], ARGV[3])
redis.call('SADD', KEYS[3], ARGV[1])
return 'ok'
`;
