# Spend relay

An OpenAI-compatible relay in front of Nebius Token Factory. It holds the Token Factory key, serves NVIDIA Nemotron models only, and enforces hard dollar caps per access code, per UTC day and overall. One handler runs on two hosts: `local.mjs` on your own machine (no per-call time limit) and a Vercel Function (Fluid compute) for judges and CI.

Clients use an access code as their API key and the relay as their base URL:

```
base URL   http://127.0.0.1:8787/v1          (local)
           https://<project>.vercel.app/v1    (Vercel)
API key    rc_...                             (an access code, never the Token Factory key)
```

## What it enforces

- No call starts without its worst case reserved. Before Token Factory sees a call, the relay prices it at list price and holds that amount against the code's total cap, its daily cap and the global cap in one atomic step: a Lua script on Upstash, or one locked transaction on the file ledger. Concurrent calls cannot overspend. The worst case is the full `max_tokens` (which bounds reasoning too) plus the prompt, counted like this:
  - every byte of the request is a prompt token, plus a fixed allowance for the chat template;
  - the `tools` list and the arguments of earlier tool calls count again, at 2 tokens a byte in all, because Nemotron's chat template prints each extra key of a tool schema twice and re-prints arguments with wider spacing;
  - 4 more tokens for every key inside `tools` and for every tool-call argument, 16 for every number inside an argument string (Python prints `1e15` as `1000000000000000.0`, 18 tokens), 3 for every character outside the Basic Multilingual Plane inside `tools`, and 2 for every character from U+007F to U+07FF inside `tools`. The template prints a list- or dict-valued `type` or `description` with Python's `repr()`, which spells a character it cannot print as `\x7f` or `\u0600`: 4 bytes for 1, or 6 for 2.

  I measured this offline against the published Nano, Lightning, Super and Ultra templates and tokenizers. No request I tried (tool schemas built to repeat keys, private-use and other rare Unicode characters, characters Python cannot print inside list- and dict-valued schema fields, number-heavy arguments, thousands of tools, calls and messages) rendered to more than its hold; the closest came to 0.992 of it.
- The hold is settled from the usage Token Factory reports. Streams are forced to send usage (`stream_options.include_usage`). Token Factory counts reasoning inside `completion_tokens`, so it is billed once. Cached prompt tokens are billed at the full input price.
- Usage above the hold is booked in full, and the code is frozen. If the estimate ever misses, every reported token is still charged, the relay logs a warning, and the code gets 403 until the operator runs `node admin.mjs unfreeze`. The one bound is what Token Factory can bill for a single call: the model's whole context window at the input price plus its clamp at the output price, about $1.15 for Ultra and $0.09 for Super. A report above that and above the hold, which no real call produces, is booked at the larger of the two, counted under "reconcile", and kept in the call's event as `reported`. One bad usage report cannot use up the global cap.
- If no usage arrives, the full hold is booked. That covers a broken stream, a client that hangs up, a timeout, or a reply without usage. A call that never settles (the host died) is booked in full by a sweep once its deadline passes. The ledger can overstate spend; it never understates it.
- A Token Factory error releases the hold only when nothing can have been generated. That means a 4xx other than 408, or a 5xx that comes back within 5 seconds (`release_5xx_within_ms`). A 408, or a 5xx that takes longer (a gateway timeout on a long call, a backend that failed mid-generation), is booked at the full hold and counted under "reconcile" in `admin.mjs report`, to check against the Token Factory console.
- Only listed fields go through. Allowed: `model`, `messages`, `tools`, `tool_choice`, `temperature`, `top_p`, `max_tokens`, `stream`, `stream_options`, `response_format` and `stop`, plus `chat_template_kwargs` with `enable_thinking` only and `service_tier` with `flex` only. `n` is forced to 1, and `max_completion_tokens` is read as the cap but never sent. Everything else is dropped (`best_of`, `ignore_eos`, `logit_bias`, `guided_json`, `top_k` and the like), and the `x-relay-dropped` header names each dropped field.
- `max_tokens` defaults to the clamp and never exceeds it. The clamp is 32,768 for Ultra and 8,192 for the other models.
- Only `nvidia/*` models are served. They come from the allowlist in `relay.config.json`, and that file itself refuses any other id.
- Each model has a limiter at 90% of Token Factory's rate limits, and each code gets a share of it. Both use a sliding one-minute window. The share of the published limits is `limiter_fraction` (0.9 when left out). A call holds its estimated prompt plus `max_tokens` in the token window while it runs; once settled, it counts its completion tokens and only the prompt tokens Token Factory did not serve from its prompt cache (`limiter_counts_cached_tokens: true` counts the cached ones too). A code may use 25% of a model's requests and tokens per minute (`code_share_pct`, or `--share` per code) and run 8 calls at once (`code_max_in_flight`, or `--in-flight`), so one code cannot lock the others out. The codes that run Architect builds get 50% (see [What it costs the database](#what-it-costs-the-database)). A code with nothing running may always start one call, however large, once its own recent use is back within its share. A call waits up to `RELAY_LIMITER_WAIT_SECONDS` (default 10) for room, then gets a 429 with `Retry-After`.
- There are two kill switches. `RELAY_DISABLED=1` in the environment, or `node admin.mjs disable` in the ledger. The ledger switch takes effect at once on every host sharing that ledger.
- Upstream details pass through. That covers status codes, `Retry-After`, `x-ratelimit-*`, `x-request-id` and the body unchanged (completion ids included). Two exceptions: a stream's final usage-only chunk is dropped when the client did not ask for usage itself, and the relay's own key is cut out of any error body. The relay refuses redirects and does not follow them. The relay adds:
  - `x-upstream-ms`: time to Token Factory's response headers;
  - `x-relay-request`: the ledger's id for the call;
  - `x-relay-max-tokens`;
  - for whole replies, `x-completion-id` and `x-relay-cost-usd`.

## Endpoints

| Route | Auth | What |
|---|---|---|
| `POST /v1/chat/completions` | code | The relayed call |
| `GET /v1/models` | code | The allowlist in Token Factory's verbose shape (per-token prices, context, clamp), so Architect's `nebius` discovery works unchanged |
| `GET /v1/relay/balance` | code | What the code has left under its own total and daily caps, which of the two binds, its calls running and its share; never the shared budget's figures |
| `GET /healthz` | none | 200 when the ledger, key and global cap are in place; 503 with the reason otherwise. On Vercel each instance reads the ledger for it at most once every 30 s, and answers 503 (`ledger refusing commands`) for 30 s after a ledger command for one of its calls failed |

## Errors

All errors are JSON in the OpenAI shape: `{"error": {"message", "type", "code", ...}}`.

| Status | `type` (`code`) | When |
|---|---|---|
| 400 | `invalid_request_error` (`invalid_json`, `too_deep`, ...) | The body is not JSON, nests deeper than 64 levels, or breaks a field rule |
| 401 | `relay_unknown_code` (`missing_code`, `unknown_code`, `code_revoked`) | No code, a malformed code, an unknown code, or a revoked code |
| 402 | `relay_cap_exceeded` (`total_cap`, `daily_cap`, `global_cap`, `global_freeze`) | The worst case does not fit. The body carries `remaining_usd` (what the code itself has left) and `needed_usd`, plus `cap_usd` for the code's own caps and `resets_at` for the daily cap. A refusal by the shared budget gives no figure for it |
| 403 | `relay_model_not_allowed` | Any model outside the allowlist; `allowed_models` lists the ones that are served |
| 403 | `relay_code_frozen` (`code_frozen`) | A call by this code cost more than its hold; the operator has to unfreeze it |
| 413 | `invalid_request_error` (`body_too_large`) | The body is over `max_body_bytes` (4 MiB locally, 1 MiB on Vercel) |
| 429 | `relay_rate_limited` (`rpm`, `tpm`, `code_rpm`, `code_tpm`, `in_flight`) | The model's limiter, the code's share of it, or the code's calls at once, with `Retry-After`. A Token Factory 429 passes through as is |
| 499 | `relay_client_closed` | The client left before the call was sent; nothing was charged |
| 503 | `relay_disabled`, `relay_misconfigured`, `relay_ledger_unavailable`, `upstream_budget` | Kill switch on; no key, ledger or global cap; ledger unreachable (`Retry-After: 5`); Token Factory says the account is out of funds |
| 502 | `upstream_auth`, `upstream_unreachable`, `upstream_broken` | Token Factory refused the relay's key, could not be reached, or cut a reply short |
| 504 | `relay_timeout` | The per-call limit ended the call (a stream ends with an SSE error event instead) |

Every other status from Token Factory (400, 404, 409, 410, 503, ...) passes through unchanged. Whether the hold is released or booked follows the rule under "What it enforces".

## What it costs the database

Upstash bills by the command, and a script counts as one command plus one for every command it runs inside. The free tier allows 500,000 a month. The ledger keeps each record as one string, so a script reads all of its records with one MGET and writes them back with one MSET. The earlier layout, a Redis hash per record changed field by field, cost 52 commands a call (22 in the reserve script, 28 in the settle script). In October 2026 about 9,050 calls used up the free month, at about 55 commands a call. Most of the difference was reserves that a code's share of the limiter refused, at 9 commands each (see the build below).

| What | HTTP requests | Commands counted |
|---|---|---|
| A model call, whole or streamed | 2 | 6 (was 52) |
| A refusal from the ledger: 402, 403 frozen, 429, 503 switched off, a first unknown or revoked code | 1 | 2 (was 3 to 9) |
| A refusal before the ledger: no code, a malformed or remembered code, a model that is not served, a bad body | 0 | 0 |
| `GET /v1/models` or `GET /v1/relay/balance` | 1 | 1 (was 2 and 3) |
| `GET /healthz` | at most 1 every 30 s per instance | at most 1 |
| Each retry of a call that waits for the limiter | 1 | 2 |
| A sweep of one hold left by a dead instance | 2 | 6 |

These counts assume a database with no read regions. Upstash copies each write to every read region and counts it there again.

The ledger keeps every day's records, so the first call of a UTC day costs the same 6. An expiry would not stay on them anyway: the scripts write records back with MSET, which drops a key's expiry as SET does. A code's day takes about 100 bytes, and a day's usage by model a few hundred.

The relay runs its scripts by EVALSHA and sends a script's text only when Upstash answers NOSCRIPT, which happens after Upstash loses its script cache or when a new version of the relay runs. The EVAL that follows loads the script again, and that call costs one command more per script.

A judge build is about 1,500 calls, so it costs about 9,000 commands, as long as its code's share of each model's per-minute budget keeps up. Architect runs a build one call at a time, one rung after another, at about 83 calls a minute. The default share of 25% lets a code start 67 Super or Ultra calls a minute, so on those rungs about 840 reserves a build are refused and tried again after the limiter's wait, at 2 commands each: about 7.1 commands a call, 10,700 a build, and 21 minutes instead of 18. So the codes that run builds (the judge code, the spares, and my film and test codes) get a 50% share (`--share 50`), and the same build then runs without a refusal.

The judge codes put a ceiling on a month of judging: the judge code holds $10, at most $3 a day, and at about $0.31 a build it pays for about 32 builds, while the three $1 spares pay for about 9 more. If judges spent all of it in one month, those 41 builds would come to about 370,000 commands. Each judge's sign-in reads the balance and the model list (2 commands), and an uptime check of `/healthz` every 5 minutes adds about 8,600 a month. A month of 10 to 20 builds costs 90,000 to 180,000, which is 18 to 36 percent of the free tier. Our own builds on the film and test codes count in the month they run, at the same 9,000 each.

`test/ledger-cost.test.ts` pins these numbers through the whole handler, against a stand-in for Upstash that counts commands the same way (`test/fake-upstash.ts`), a whole judge build at each share included.

## Run it locally

Use one ledger for all spend. Two ledgers mean two global caps that each think they hold the whole budget.

```bash
cd deploy/relay
npm install
npm run compile
node admin.mjs global --cap 26                              # the lifetime cap over every code
node admin.mjs mint --label dev-forge --total 2 --freeze-at 85   # prints the code once
NEBIUS_API_KEY=... node local.mjs                           # http://127.0.0.1:8787/v1
```

Give the key to the relay's own command only. Use the access code everywhere else:

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "authorization: Bearer $CODE" -H "content-type: application/json" \
  -d '{"model":"nvidia/Nemotron-3_5-Lightning","messages":[{"role":"user","content":"Say OK."}],
       "max_tokens":8,"chat_template_kwargs":{"enable_thinking":false}}'
```

The local ledger is `.data/ledger.json`. Events go to `.data/ledger.events.jsonl`. Both are git-ignored. `local.mjs` exits with the reason if it cannot open its ledger.

To share one ledger with the Vercel relay, follow [Move the local ledger onto Upstash](#move-the-local-ledger-onto-upstash).

With `UPSTASH_REDIS_REST_URL` or `UPSTASH_REDIS_REST_TOKEN` in the environment, `local.mjs` and `admin.mjs` refuse the file ledger unless `RELAY_LEDGER=file` (or `--ledger file`) asks for it by name.

## Admin

```bash
node admin.mjs global --cap <usd>
node admin.mjs opening-balance --usd <usd> | --from-file <ledger.json>
node admin.mjs mint --label <name> --total <usd> [--daily <usd>] [--freeze-at <pct>] [--share <pct>] [--in-flight <n>]
node admin.mjs mint --existing --label <name> --total <usd> [...] < <file>
node admin.mjs revoke <id|label>
node admin.mjs unfreeze <id|label>
node admin.mjs caps <id|label> [--total <usd>] [--daily <usd>] [--freeze-at <pct>] [--share <pct>] [--in-flight <n>]
node admin.mjs list [--json]
node admin.mjs report [--days 7] [--events 10] [--json]
node admin.mjs disable | enable
node admin.mjs sweep [--all]
node admin.mjs migrate [--wait <s>] [--force]
```

- Codes. `mint` prints the code once, on stdout. The ledger stores only its SHA-256. `list` and `report` show a code's id, which is the first 12 hex characters of that hash. They never show the code itself. `mint --existing` adds a code that already exists, such as a published judge code after a move to a new database. It reads the code from stdin, either the only code there or the one on a `<label>=<code>` line for its `--label`, and prints only the id.
- One code per stream of work. For example: the pilot, A1 to A3, each Forge session, the primary judge code and the spares.
- `--freeze-at 85`. The code stops once the whole relay has spent 85% of the global cap. Use it for development codes and for optional work such as Tavily searches; judge and filming codes keep the default of 100.
- `--share` and `--in-flight`. A code's share of each model's per-minute budget, and the calls it may run at once. Codes that run Architect builds (the judge code, the spares, film and test) get `--share 50` and keep 8 calls at once: a build runs about 83 calls a minute, more than 25% of Super's and Ultra's requests. Evidence and bench codes that run many calls in parallel need more, for example `--share 100 --in-flight 16`.
- `opening-balance`. Counts spend made on another ledger against this one's global cap. `--from-file` reads it (spent plus still held) from a file ledger, then switches that file ledger off so it cannot go on spending under a cap of its own. Setting it again replaces the earlier figure; it is never added twice.
- Frozen codes. `report` lists them. A call's reported usage cost more than the relay held for it, which means the estimate missed: look at the call in `report --events`, then `unfreeze`.
- `report` totals. It prints spend per code and per UTC day and model: calls, input tokens, output tokens (reasoning included), the ledger total in dollars, and how each call was settled. "reconcile" counts calls booked in full after a Token Factory error that may have followed generation, and calls whose reported usage came to more than the model can bill for one call. It also prints which of the 50%, 70%, 85% and 100% global alert levels are crossed. Compare it with the Token Factory console for reconciliation: the ledger should be at or above what was billed.
- `sweep --all`. Books every open reservation at its full hold. Use it after `local.mjs` was stopped mid-call; otherwise those calls stay held, and count against their code's calls at once, until they expire two hours after they started.
- `migrate`. Copies an Upstash ledger kept in the earlier layout into the current one. See [Move an Upstash ledger to the current layout](#move-an-upstash-ledger-to-the-current-layout).
- Choosing the ledger. The default is the file ledger. `--ledger upstash` (with `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN`) works on the shared ledger, and `--file <path>` picks another ledger file.

## Move the local ledger onto Upstash

A global cap only counts what its own ledger records. Until this move, spend on this machine's file ledger is invisible to the Upstash ledger that the Vercel relay uses, so carry it over before the relay serves anyone. Only the total moves. Codes, events and daily figures stay in the file, and codes for later runs are minted on Upstash.

Set `FILE` to the file ledger (the `RELAY_LEDGER_FILE` of the script that starts `local.mjs`, else `.data/ledger.json`). Put `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` in this shell only.

1. Stop `local.mjs`, then check that no call is still held. The `open` line should say 0 reservations. If it does not, `sweep --all` books the open ones in full.

   ```bash
   node admin.mjs --ledger file --file "$FILE" report --events 0
   node admin.mjs --ledger file --file "$FILE" sweep --all    # only if a reservation is open
   ```

2. Set the lifetime cap on Upstash. It is the whole budget, so it includes what was spent before the move.

   ```bash
   RELAY_LEDGER=upstash node admin.mjs global --cap <usd>
   ```

3. Carry the spend over. This reads the file's spent plus held, then switches the file ledger off so that it cannot go on spending under a cap of its own.

   ```bash
   RELAY_LEDGER=upstash node admin.mjs opening-balance --from-file "$FILE"
   ```

4. If the Token Factory console shows a larger total (it also counts calls that did not go through the relay), set that figure instead. It replaces the first one and is never added twice.

   ```bash
   RELAY_LEDGER=upstash node admin.mjs opening-balance --usd <console total>
   ```

5. Check the result. The first line should read `spent $X ($X carried over)` with nothing in flight.

   ```bash
   RELAY_LEDGER=upstash node admin.mjs report --events 0
   ```

6. From then on, start `local.mjs` with `RELAY_LEDGER=upstash` and the two Upstash variables, so local runs and the Vercel relay draw on one cap. A start script that sets `RELAY_LEDGER=file` or unsets the Upstash variables has to change as well.

To undo the move, `node admin.mjs --ledger file --file "$FILE" enable` switches the file ledger back on, and `RELAY_LEDGER=upstash node admin.mjs opening-balance --usd 0` takes the carried figure back out.

These steps were tried on 2026-10-05 on a copy of the ledger file, under a throwaway Upstash prefix that was deleted afterwards. The copy was switched off, the carried figure matched its spend, and a second `--usd` replaced the figure without adding to it.

## Move an Upstash ledger to the current layout

Until 2026-10-06 the Upstash ledger kept each record as a Redis hash. The relay now reads only the current layout, whose keys start with `<prefix>:v2:` (`relay:v2:` by default), so a database the earlier relay wrote has to be copied once. The copy costs a few hundred commands, and the old keys stay as they were.

0. Run the tests against the database first. The current layout's scripts have so far run only offline, on a Lua 5.3 stand-in, and their first run on Upstash's own Lua should not be a judge's call. Wait until the database accepts every command again. One that is over its limit can refuse some commands and accept others, and `/healthz` reads it at most once every 30 s, so check with single reads instead: five reads every 30 s that all go through, three rounds in a row. Then run `npm test` here with `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` set in this shell. Besides the offline tests, it runs the ledger contract, the races and the Vercel entry against the database, under fresh `relaytest:<random>` prefixes that it deletes afterwards; it never reads or writes the `relay` keys. That costs about 900 commands. Go on only if all of it passes and nothing is skipped (a skipped file means the variables were not set).

   ```bash
   npm test
   ```

1. Copy it. This switches the old ledger off, so a relay still running the earlier code refuses new calls. It waits up to 300 s (`--wait`) for the calls already running to settle, then copies the codes and labels, the caps, the spend and the carried-over balance, each code's daily figures, the usage by day and model, the newest 1,000 events, and any reservation still open (a sweep books it in full once its deadline passes). If a late settlement changes the old figures while it copies, it copies again. The copy starts switched off. `--force` copies again later (after a settlement that came in after the copy, say), but only for a new ledger that has never served a call: the copy keeps its spend, holds and open calls under `<prefix>:v2:migrated`, and `--force` refuses once any of them differ or the new ledger is switched on.

   ```bash
   RELAY_LEDGER=upstash node admin.mjs migrate
   ```

2. Give the codes that run builds half of each model's per-minute budget. The copy keeps each code's own share, and these six were minted with the default 25%, which a build outruns ([What it costs the database](#what-it-costs-the-database)).

   ```bash
   for label in judge spare-1 spare-2 spare-3 film test; do
     RELAY_LEDGER=upstash node admin.mjs caps "$label" --share 50
   done
   ```

3. Deploy the relay (step 2 of the next section).
4. Check that the spend, the codes and the recent calls match what the earlier relay showed, and that the six codes show a 50% share.

   ```bash
   RELAY_LEDGER=upstash node admin.mjs report --events 5
   ```

5. Switch it on.

   ```bash
   RELAY_LEDGER=upstash node admin.mjs enable
   ```

6. Make one call with the test code, and check how it was booked and what it cost the database. Before the call, note the database's monthly command count in the Upstash console, with nothing else using the database yet (no uptime check, no judges). Put the test code from the private codes file in `CODE` in this shell.

   ```bash
   curl https://<project>.vercel.app/v1/chat/completions \
     -H "authorization: Bearer $CODE" -H "content-type: application/json" \
     -d '{"model":"nvidia/nemotron-3-super-120b-a12b","messages":[{"role":"user","content":"Say OK."}],"max_tokens":16}'
   RELAY_LEDGER=upstash node admin.mjs report --events 1
   ```

   The newest call should be settled `usage`, from the usage Token Factory reported. Once the console catches up, the count should have grown by 6 for the call, or 8 if Upstash had to load the scripts again, plus 3 for the report. If `migrate` carried open reservations whose deadline has passed, the call also sweeps them: 6 more for one, and 3 more for each one after it. If either check fails, switch the ledger off (`disable`) and find out why before anyone else uses it. When both hold, put the date in the last paragraph of this section.

The copy reads the old keys, so the database has to accept commands. A free database that has used up its month refuses commands until its monthly count resets (in October 2026, about half of them). Before then, either move to pay as you go or start a new database. For a new one, set its URL and token in Vercel, set the cap and carry the spend over (`global --cap`, then `opening-balance --usd` with the Token Factory console's total), and put back the codes already handed out instead of minting new ones, so the judge code in the testing instructions keeps working. Give each the caps it had, with `--total` at what it had left (the `left` column of the last `admin.mjs list` you have):

```bash
RELAY_LEDGER=upstash node admin.mjs mint --existing --label judge --total <usd left> --daily 3 --share 50 < <codes file>
RELAY_LEDGER=upstash node admin.mjs mint --existing --label spare-1 --total <usd left> --share 50 < <codes file>
```

With one `<label>=<code>` line per code in the private codes file, each command takes the line for its own label. A file holding one bare code works too. The other spares, and the film and test codes, go back the same way, which keeps any workspace that stores them working.

`test/ledger-migrate.test.ts` writes ledgers with the earlier code (`test/fixtures/v1`) and checks that every figure reads the same after the copy, through `admin.mjs` as well. As of 2026-10-06 the current layout has run offline only, on ioredis-mock and the REST stand-in, and the deployed relay still runs the earlier layout. Steps 0 and 6 have not been run on Upstash yet.

## Deploy on Vercel (owner)

Since 2026-10-05 the relay runs as the Vercel project `architect-relay` on the Hobby plan, at `https://architect-relay.vercel.app/v1`. The project fits Hobby as it is:

- `maxDuration` is 300 s, which is Hobby's maximum with Fluid compute. The relay ends each call itself at 280 s (`RELAY_MAX_CALL_SECONDS`) and settles it before the platform stops the function.
- Memory stays at Hobby's fixed 2 GB, since `vercel.json` cannot set memory under Fluid compute. The function runs in one region, `iad1` unless the project picks another.
- Request bodies are capped at 1 MiB (`vercel_max_body_bytes`), well under the platform's 4.5 MB, which also bounds how large any one prompt can be.
- Hobby allows one WAF rate-limit rule per project, and step 6 uses it.

`vercel build` (CLI 62.2.0), run offline with a hand-written `.vercel/project.json` and no login, builds this folder into one function on `nodejs24.x` with `maxDuration` 300, request cancellation on and `relay.config.json` included. On 2026-10-05 that built function answered `/healthz`, `/v1/models`, `/v1/relay/balance` and one call against a fake Token Factory, with its ledger on the real Upstash database under a throwaway prefix.

Steps:

1. Upstash. A Redis database on the free tier, in a region near the function (`us-east-1` for `iad1`), with no read regions (each one would count every write again). Its REST URL and REST token go into step 3.
2. Project. Either deploy this folder with the Vercel CLI (`vercel link`, then `vercel deploy --prod`, run in `deploy/relay`), or import the repository in the dashboard with Root Directory `deploy/relay`. `vercel.json` sets everything else: the "Other" framework preset with no build command (the package has no `build` script on purpose), Fluid compute, the function's limits, the rewrites of `/v1/*` and `/healthz` to `api/relay.ts`, and `public/` as the static output. That folder holds only `robots.txt`, so no other file of this folder is served. `engines` in `package.json` keeps Node.js on 24.x.
3. Environment variables, for Production (names only; set the values in Vercel):
   - `NEBIUS_API_KEY`: a Token Factory key. One made for the relay alone can be revoked without touching anything else.
   - `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN`.
   - Optional: `RELAY_HELP_URL` (linked from cap errors; for judges, the page with their testing instructions), `RELAY_MAX_CALL_SECONDS` (default 280; anything outside 1 to 290 stops the relay with a 503 that says so), `RELAY_LIMITER_WAIT_SECONDS` (default 10), `RELAY_KEY_PREFIX` (default `relay`), `RELAY_DISABLED`.

   A changed variable takes effect on the next deployment.
4. Ledger and judge codes. First move the local ledger (the section above), which also sets the global cap. Then mint the judge codes from this machine, with `RELAY_LEDGER=upstash` and the two Upstash variables in the shell. `mint` prints each code once on stdout, so send it straight to a private file outside the repository:

   ```bash
   RELAY_LEDGER=upstash node admin.mjs mint --label judge --total 10 --daily 3 --share 50 >> <private file>
   RELAY_LEDGER=upstash node admin.mjs mint --label spare-1 --total 1 --share 50 >> <private file>
   RELAY_LEDGER=upstash node admin.mjs mint --label spare-2 --total 1 --share 50 >> <private file>
   RELAY_LEDGER=upstash node admin.mjs mint --label spare-3 --total 1 --share 50 >> <private file>
   RELAY_LEDGER=upstash node admin.mjs mint --label film --total 5 --share 50 >> <private file>
   RELAY_LEDGER=upstash node admin.mjs mint --label test --total 3 --share 50 >> <private file>
   ```

   The judge code is the one in the judges' testing instructions: $3 per UTC day and $10 in all. The spares hold $1 each and work when the judge code has used up its day, or after it has been revoked. The film and test codes are my own, for filming the video and for checks on the deployed relay. All six run Architect builds, so they get a 50% share of each model's per-minute budget, and keep the defaults for `--freeze-at` (100) and `--in-flight` (8). The global cap has to leave room for them: the carried-over spend, plus $21 for these six codes, plus anything else that will run through the relay before judging ends.
5. Deployment Protection. It must leave the production domain public. Standard Protection does, and keeps generated deployment URLs behind Vercel's login; "All Deployments" would put the relay itself behind that login. Give judges the production domain: `https://<project>.vercel.app/v1`.
6. Firewall. In the Vercel project, open Firewall, then Configure, then New Rule. Name it "relay per IP", add the condition Request Path starts with `/`, pick the Rate Limit action with a fixed window of 60 s, a limit of 300 requests and the IP key, keep the default 429, save, then Review Changes and Publish. Counters are kept per region.
7. Contract test. With the same two variables, `npm test` also runs the Lua contract, the races and the Vercel entry against the real database. It writes under fresh `relaytest:<random>` prefixes and deletes them afterwards. A run costs about 900 commands.
8. Check. After the deploy, on the production domain:

   ```bash
   curl https://<project>.vercel.app/healthz                    # {"ok": true, ...}
   curl https://<project>.vercel.app/v1/relay/balance -H "authorization: Bearer $CODE"
   ```

   `/healthz` needs no code and reads the ledger at most once every 30 s per instance, so an uptime monitor can poll it. For 30 s after a ledger command for one of an instance's calls fails, that instance's `/healthz` answers 503 as well, whatever its last read said. Look at the database's monthly command count in the Upstash console now and then; a judge build adds about 9,000.
9. Point Architect at it. Set `RELAY_URL` in `architect/providers/judge.py` to `https://<project>.vercel.app/v1` before the release is built. `architect judge <code>` and the Nebius sign-in, in the CLI and in the desktop app, then reach this relay. Until then, `ARCHITECT_RELAY_URL` or `architect judge --url` does the same on one machine.
10. Acceptance. Run a 90 s Ultra call and a 280 s stream, and read GB-hours and CPU in Vercel's usage page.

Kill switch. `RELAY_LEDGER=upstash node admin.mjs disable` refuses every new call at once, on every instance (`enable` undoes it). `RELAY_DISABLED=1` in Vercel does the same only from the next deployment. `RELAY_LEDGER=upstash node admin.mjs revoke <label>` stops one code. Calls already running finish and settle either way.

## Files

| Path | Role |
|---|---|
| `src/handler.ts` | The one Web-standard handler: routes, auth, reserve, upstream, settle |
| `src/request.ts` | Field allowlist, `n`, `max_tokens` clamp, the reservation estimate |
| `src/ledger/` | `types.ts` (the interface); `state.ts` + `file.ts` (FileLedger); `lua.ts` + `upstash.ts` (UpstashLedger); `migrate.ts` (the copy from the earlier Upstash layout) |
| `src/sse.ts`, `src/usage.ts`, `src/limiter.ts`, `src/money.ts` | Stream tap, usage and cost, sliding windows, integer nano-dollars |
| `local.mjs`, `api/relay.ts` | The two hosts |
| `admin.mjs` | Operator CLI |
| `relay.config.json` | Models, prices of 2026-10-03, clamps, Token Factory's RPM and TPM, estimate constants, per-code defaults |

Money is integer nano-dollars everywhere. That is exact for prices with up to 3 decimals per 1M tokens, which covers every Nemotron price on Token Factory.

## Tests

Run `npm test`. It compiles, type-checks the tests, and runs vitest. Coverage:

- the field allowlist, the clamp and the reservation estimate, tools and tool-call arguments included;
- cap math with reasoning tokens, and usage above the hold (booked in full, code frozen);
- 401, 402, 403 and 429 refusals, both kill switches, and the per-code shares and calls at once;
- header and status pass-through, and which Token Factory errors release the hold;
- settlement on broken streams, hang-ups, timeouts and lost reserve replies;
- the limiter, sweeps of overdue reservations, and the opening balance;
- reservation races through the handler on the file ledger and on the Upstash ledger (its Lua on the mock, through the real client over REST, and on a real database when the Upstash variables are set), plus four processes racing on one ledger file;
- an end-to-end run of `admin.mjs` and `local.mjs` against a fake Token Factory;
- what each route costs the Upstash database, counted the way Upstash bills, and a 1,500-call judge build through the limiter: 6 commands a call at a 50% share, about 7.1 at 25% (`test/ledger-cost.test.ts`);
- moving a ledger from the earlier Upstash layout, through `admin.mjs migrate` as well (`test/ledger-migrate.test.ts`);
- the security review's attacks on spend and on the outside surface (`test/*.adversarial.test.ts`).

The ledger contract (`test/ledger-contract.ts`) runs on four backends:

- the FileLedger;
- the Upstash ledger's Lua scripts, offline on `ioredis-mock`. That mock runs Lua 5.3 with 32-bit integers, so `lua.ts` keeps all arithmetic in floats and writes every number with `%.0f`; this checks the scripts' logic, not Upstash itself;
- the same, driven through the real `@upstash/redis` client against an offline stand-in for Upstash's REST API (`test/fake-upstash.ts`: request shape, pipelining, base64 replies, raw HGETALL lists, EVALSHA and its script cache). The Vercel entry is also run end to end over it;
- a real Upstash database, only when the Upstash variables are set. The races and the Vercel entry's end-to-end call then run there too. Each test writes under a fresh `relaytest:<random>` prefix and deletes it afterwards, so a ledger under the default `relay` prefix in the same database is never touched.

## Limits

- Reservations are deliberately pessimistic. Near a cap, a call that would have fit can be refused. Tools and tool-call arguments make the hold larger still. Lowering `max_tokens` is the fix.
- The estimate assumes Token Factory renders prompts with Nemotron's published chat templates (the vLLM default). It was measured offline, without a live call. If a template renders more, the call is still booked in full and its code is frozen.
- The ledger assumes list prices. If Token Factory discounts cached tokens or flex, the ledger overstates spend.
- A dead Vercel instance holds its money for a while. If an instance dies mid-call, the hold stays until the reservation's deadline (call limit plus 120 s). After that, the next call to reach the ledger starts a sweep that books it in full, even a call the ledger refuses, so a code whose calls at once are all held this way frees itself on its next try.
- Revoking a code or flipping the kill switch stops new calls only. Calls already running finish and settle.
- Unknown codes still cost the ledger 2 commands each. A call checks its code in the same step that reserves it; an unknown or revoked code is then remembered by that instance for 10 minutes, so repeats are free. Fresh random codes are not, which is what the firewall rule is for. Malformed codes never reach the ledger.
- On Vercel, `/healthz` can trail the ledger by up to 30 s. Calls themselves see a kill switch at once.
- The per-minute limits assume that the hosts sharing a ledger agree on the time to within a minute. The ledger keeps each model's three newest minutes, so one call from a host whose clock runs ahead cannot wipe what the others count, but a host that is further off and keeps calling can still let calls past the limits. It cannot let them past a dollar cap.
- The Upstash ledger keeps the newest 1,000 settlement events (the file ledger keeps all of them), so `report --events` shows at most 1,000. The ledger keeps spend, calls and usage by day and model in full.
- The file ledger is for one machine. The relay refuses it on Vercel.

Verified 2026-10-05 on the production deployment, after the move to Upstash: `/healthz`, `/v1/models` and `/v1/relay/balance` answered, an unknown code got 401 and a non-NVIDIA model 403, and neither was booked. Two Super calls with `max_tokens` 16 went through, and each was settled from reported usage (19 prompt and 2 completion tokens, $0.0000075). With the ledger kill switch on, a call got 503 (`relay_disabled`) and was not booked. `npm test` with the Upstash variables then passed, and the database kept only the `relay` keys.

Verified 2026-10-05 after the firewall limit went to 300 requests and the relay was redeployed with `public/` as its only static folder: `/admin.mjs`, `/local.mjs`, `/relay.config.json` and `/src/handler.ts` answered 404, `/healthz` reported the Upstash ledger, `/v1/models` listed the four models and `/v1/relay/balance` answered. One Super call with `max_tokens` 16 was settled from reported usage (19 prompt and 2 completion tokens, $0.0000075), and its code's spend grew by exactly that. Of 315 requests to `/healthz` sent from one address in about 5 s, the firewall let 300 through and answered 429 to the other 15, with no `Retry-After` header. The address got through again about a minute after its first request.

Verified 2026-10-05 on the Upstash database the Vercel relay will use, with no model call: the ledger contract (22 cases), both races and the Vercel entry's end-to-end call (against a fake Token Factory on this machine) passed, and the database held no keys afterwards.

Verified 2026-10-03, before the review fixes:

- Two Lightning calls (one whole, one streamed) went through `local.mjs` to Token Factory. Each was settled from reported usage (23 prompt and 2 completion tokens, $0.00000186 each).
- Without reaching Token Factory, the relay answered a non-nvidia model with 403, an over-cap call with 402 and an unknown code with 401.

Until the move to Upstash on 2026-10-05, live calls through the fixed relay ran on `local.mjs` with a file ledger: the relay checks of 2026-10-04 (`docs/feedback/probes/2026-10-04-relay-g0.md`), the pilot and the Forge rounds.
