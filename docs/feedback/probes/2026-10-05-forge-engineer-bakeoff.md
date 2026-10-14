# Forge engineer bake-off on pack v3, 2026-10-05

Three more live Forge builds on the CORD receipts rehearsal floor, each with a different
engineer: Super again with more time (super-40), DeepSeek V4 Flash (dsflash) and Kimi K2.7
Code (kimi-code). The setup is G1 retry 2's S3
(`docs/feedback/probes/2026-10-05-forge-native-g1-retry2.md`); only the engineer, its
settings, the Forge cap and the Forge minutes change. The sessions ran one after another,
and every call went through the local spend relay.

Development only. The rehearsal floor is for development, every number below is a
development number, and nothing here is evidence or may back a claim.

## Summary

- **No session ended with an accepted submission.** With G1, the retry and retry 2, that
  makes 10 Forge sessions without one: 5 on Ultra, 3 on Super, 1 on DeepSeek, 1 on Kimi.
- **kimi-code wrote valid code almost every time, but no better harness.**
  - 13 of its 14 checks passed, the first at turn 6, 1.7 minutes into the session. 14 of
    the 15 versions it wrote pass the validator as written; the other has one helper that
    calls itself.
  - It measured 14 times. Its best was 4 of 16 practice receipts (0.25), first at turn 29;
    8 of its 14 measures gave that. That never beat the preset (0.26 on all 50) or the 0.40
    floor, so it never submitted.
  - Its $1.75 cap ended it at turn 71, 7.6 minutes in. A Kimi turn cost $0.0229, about as
    much as an Ultra turn, and 47.6% of its output tokens were reasoning. Two early replies
    were 12,288 tokens of reasoning with nothing else; both were cut and re-asked.
- **super-40 did no better with 15 more minutes.**
  - 5 of its 26 checks passed, the first at turn 36. It measured once (0 of 3) and
    submitted once. The submission was refused before it ran: the file held one line of
    text, not code.
  - That line was Architect's own placeholder. Once the 60,000-character context cap
    trims an old `write`, the history shows its content as `[N characters, removed to keep
    the conversation under its size limit. Read the file to see its current text.]`.
    Super copied that text into `harness.py` as the new file. 43 of its 58 writes were
    this placeholder, 40 of them into `harness.py`. From turn 139 on, every check failed
    on that line. Those last 62 turns cost $0.60, and the first of them replaced a
    390-line version that passes the validator (the session never checked that one). S3
    did the same twice (turns 55 and 56).
  - Pi's 200-turn limit ended it at 35.5 minutes, with $0.08 of its cap left.
- **dsflash never wrote a harness.** With thinking off, DeepSeek wrote its plan as reply
  text until it hit the 12,288-token output cap, with no tool call. The re-ask asked for a
  smaller step, and the next reply did the same. Forge re-asks a cut reply once, then
  takes a second cut reply as the final answer. So the session ended at turn 4, after
  98 seconds and $0.014 of agent calls.
- **The mechanics pass.** All 275 replies that arrived parsed, none of the 282 tool calls
  was malformed, and there was no `REASONING_CUT`. 4 replies were cut at the output cap
  (2 in dsflash, 2 in kimi-code).
- **The relay paced only Super.** super-40 waited out 43 of the relay's 429s (504 s), and
  the relay held 129 of its calls for 954 s before admitting them. DeepSeek and Kimi got
  none: the prompt cache served 67% and 79% of their prompt tokens, and the limiter
  counts only uncached ones.
- Live spend: **$3.41763117** through the relay, against this task's cap of $4.30, from
  the relay ledger.

## Setup

- Code: `hackathon/forge-pack-v2` at `2d98a85`, which is pack v3 as G1 retry 2 ran it
  (`d02b75f`) plus that note. Nothing in the repository changed for these runs.
- Workspaces `C:/t/b4-super`, `C:/t/b4-dsf` and `C:/t/b4-kimi`, fresh, each with its own
  signing key.
  - The spec and the floor files are byte for byte S3's.
  - Each profile differs from S3's only in the engineer entry and the Forge cap and
    minutes. super-40's engineer entry is S3's, byte for byte.
  - The DeepSeek and Kimi entries also name a price file, `model-prices.json` in the
    workspace. It was written with the product's own helpers (`per_token_price`,
    `write_price_entries`): Token Factory's list price, an output capacity of 16,384
    tokens and a 1,024-token template overhead, as in the bundled Nemotron entries.
  - Each workspace's credentials file held the relay's `evidence` access code, every model
    entry's `base_url` was the relay, and no Token Factory key was in Architect's
    environment. The three credentials files were deleted after the analysis.
- Everything else is S3's:
  - the play model: Lightning at temperature 0, thinking off, `max_tokens` 512;
  - Stage 2 with 4 preset configurations;
  - the compile budget of $0.25 and 1,200 s, plus the Forge cap and minutes;
  - Forge in agent mode: 24 measures, 6 submissions, 0 spawns, agent replies capped at
    12,288 tokens, a 60,000-character context cap and Pi's limit of 200 turns;
  - the command, with `--forge-usd` and `--forge-minutes` per session and
    `--audit C:/t/b4-audit-<name>`;
  - the driver, which only observes. Its one functional change is the two new models'
    list prices, for its hard stop ($1.90, $0.75 and $1.90 a session). The hard stop
    never fired.

| | super-40 | dsflash | kimi-code |
|---|---|---|---|
| Engineer | `nvidia/nemotron-3-super-120b-a12b` | `deepseek-ai/DeepSeek-V4-Flash-0731` | `moonshotai/Kimi-K2.7-Code` |
| Thinking | off | off (`enable_thinking` false) | on (it reasons either way) |
| Temperature | 0 | 0 | 0.6 |
| Timeout a call | 300 s | 300 s | 600 s |
| List price per million tokens, in / out | $0.30 / $0.90 | $0.14 / $0.28 | $0.95 / $4.00 |
| Output cap a reply | 8,192 (the relay's Super clamp, as in S3) | 12,288 | 12,288 |
| Forge cap and minutes | $1.75, 40 | $0.60, 40 | $1.75, 30 |
| Relay | `start-relay-fpk2.sh`, unchanged | the bake-off copy | the bake-off copy |
| Run (UTC) | 03:44:45 to 04:24:34 | 04:24:59 to 04:30:35 | 04:31:36 to 04:43:19 |

- Forge pack hashes, as each session recorded them. `architect forge pack` with the same
  flags gives the same hashes, and its task-text check finds nothing (the spec,
  `spec.json`, `task.json` and `RULES.md` as sources). The engineer model is not part of
  the hash. Only the settings part differs from S3's pack (`1a5c41b7...`), because the cap
  and minutes differ.
  - super-40: `95ee65c74a976b387a86904f18a51b3b329b9b0d1fe9ee3c1f70ef49fb5d0e1d`
  - dsflash: `502fe9ee6eb7b76ab4db5251e575e353cf4ba50ae8271d436a269a9646c3512f`
  - kimi-code: `d4f582c663312b94cf56e7960aff44d99fbadec81dabda0ccbd3b6c4fbc04962`

### The two new engineers on Token Factory

Both models list tool calling: `supported_features` has `tools` in
`GET /v1/models?verbose=true`, and `use_cases` has `function_calling` in the public
`models_info`. Both sources gave the prices in the table above (read 2026-10-05).

Before the sessions, a path check sent each model three plain calls and two streamed tool
calls through the relay, 10 calls for $0.00122086:

| | DeepSeek-V4-Flash-0731 | Kimi-K2.7-Code |
|---|---|---|
| Reasoning tokens: no switch / `enable_thinking` false / true | 14 / 0 / 14 | 25 / 55 / 39 |
| Where the reasoning comes back | `reasoning_content` | `reasoning_content` |
| Streamed tool call through Architect's adapter, thinking off and on | parsed both times | parsed both times |
| The reply names the model it was sent | yes | yes |

- So the switch works for DeepSeek, and dsflash ran with it off. Kimi reasons whether the
  switch is off or on, so thinking is its default, and kimi-code ran with it on.
- The relay passes on only the `enable_thinking` template argument, as before.

### The relay copy

The relay refuses any model outside `nvidia/*` when it reads its config. For the two new
engineers I ran a copy of the same compiled relay (`C:/t/relay-bake`, started by
`start-relay-bake.sh`, outside the repository) with the same ledger file and the same
evidence code. It differs from the fpk2 relay in two places:

- the config check also admits the two model ids by name (3 changed lines in the compiled
  `config.js`);
- its config lists the two models with Token Factory's list prices, a `max_tokens` clamp of
  16,384, and 300 requests and 200,000 tokens a minute. Those are the Super and Ultra
  entries' limits; the verbose model list gives only placeholder per-request limits
  (10^10), so I had nothing better.

super-40 ran on the unchanged fpk2 relay. The relay ran three times, each stopped by its
PID: the copy for the path check (PID 34860), the fpk2 relay for super-40 (PID 7372),
and the copy for dsflash and kimi-code (PID 15496). Port 8787 was free at the end.

For these runs the `evidence` code's total cap was set to its spend plus $4.30
($10.54505778), and its share of each model's per-minute budget to 100%. Both went back to
$12 and 25% after kimi-code, with the relay stopped.

## Results

Development only.

| | super-40 | dsflash | kimi-code |
|---|---|---|---|
| Wall time: whole compile / Forge session | 2,389 s / 2,130 s | 336 s / 98 s | 703 s / 454 s |
| Session ended by | Pi's 200-turn limit, with $0.08 of the cap left | a second cut reply in a row, taken as the final answer | its $1.75 cap: the next turn's worst-case quote no longer fit |
| Agent turns (model calls) | 200 | 4 | 71 |
| Replies whose tool calls all parsed | 200 of 200 | 4 of 4 | 71 of 71 |
| Replies cut at the output cap | 0 | 2, both all text (45,582 and 37,554 characters) | 2, both all reasoning (12,288 tokens each) |
| Re-asks | 0 | 1, after the first cut | 2, one after each cut |
| Tool calls / malformed | 200 / 0 | 7 / 0 | 75 / 0 |
| `REASONING_CUT` | 0 | 0 | 0 |
| `forge_check` calls that passed | 5 of 26 | none called | 13 of 14 |
| First passing check | turn 36, 3.9 minutes in | none | turn 6, 1.7 minutes in |
| Passing checks at turns | 36, 83, 89, 95, 104 | | 6, 10, 20, 25, 28, 31, 35, 39, 44, 49, 54, 58, 69 |
| Dry runs | 5 asked: 2 passed, 1 refused (`binding_spec`), 2 refused by Pi's argument check | none | 5, all passed |
| Measures on practice receipts | 1: 0/3 | none | 14: 0/16, 3/16, 6/32, 2/16, 2/16, eight of 4/16, 2/16 |
| Submissions | 1, at turn 191: refused before it ran (`forge_invalid_source`, syntax, line 1) | 0 | 0 |
| Stage 2 best preset on practice | 12/50 (0.24) | 13/50 (0.26) | 13/50 (0.26) |
| `forge_rank` against the Stage 2 best | no submission reached the ranking, so Forge's best stays the Stage 2 pick | the same | the same |
| Accepted submissions | 0 | 0 | 0 |
| Stage 4, from the signed report | the Stage 2 preset, refused: exam 15/50 (0.30) against 0.40, 0 invalid | the Stage 2 preset, refused: exam 16/50 (0.32) against 0.40, 0 invalid | the Stage 2 preset, refused: exam 16/50 (0.32) against 0.40, 0 invalid |
| Agent $ | $1.669555 | $0.013815 | $1.629392 |
| Agent $ per turn | $0.0083 | $0.0035 | $0.0229 |
| Lightning $: Stage 2 / Forge / Stage 4 | $0.0293 / $0.0002 / $0.0030 | $0.0293 / $0 / $0.0030 | $0.0293 / $0.0066 / $0.0030 |
| Build total, from the relay ledger | $1.70201244 | $0.04612086 | $1.66827701 |
| Prompt tokens per reply, mean / max | 26,547 / 37,500 | 12,164 / 16,057 | 21,077 / 27,966 |
| Cache-hit share of prompt tokens | 0% | 67.4% | 78.5% |
| Reasoning share of completion tokens | 0 (thinking off) | 0 (thinking off) | 47.6% |
| Largest reply, in tokens | 4,055 | 12,288 (cut) | 12,288 (cut) |
| Seconds per turn, mean, waits included | 10.6 | 24.2 | 3.5 |
| Turns a minute | 5.6 | 1.9 | 9.3 |
| Relay 429s waited out / seconds waited | 43 / 504 | 0 / 0 | 0 / 0 |
| Calls the relay held before admitting them / seconds held | 129 / 954 | 0 | 0 |

- Every Stage 4 report's signature checked, and none was licensed. The Stage 2 pick was the
  plain extractor configuration (no retry, no verifier) in all three builds, as in S3.
- The records agree. Architect's per-turn rows in `telemetry/forge_turns/` (200, 4 and 71
  rows, none an estimate) give the same agent dollars as the driver, and the relay
  ledger's rows for each session give the same build totals, to the micro-dollar. All
  2,168 of the sessions' calls that reached Token Factory returned 200 and were settled
  from the usage it reported. None was booked at its full hold.
- Lightning at temperature 0 still does not repeat exactly: the same Stage 2 pick scored
  12/50 and 13/50 on practice and 15/50 and 16/50 on the exam across these builds.

Tool calls by kind, with refusals in brackets:

| | super-40 | dsflash | kimi-code |
|---|---|---|---|
| read | 84 (9) | 7 | 27 (2) |
| write | 58 | 0 | 2 |
| edit | 25 (7) | 0 | 13 (1) |
| forge_check | 26 | 0 | 14 |
| forge_dryrun | 5 (2) | 0 | 5 |
| forge_measure | 1 | 0 | 14 |
| forge_submit | 1 | 0 | 0 |

- Refused reads were offsets past the end of the file, a missing tuning file and the
  `templates/` directory. Refused edits named text that was not in the file, matched it
  twice or changed nothing.
- LANGUAGE.md was read once, by kimi-code. The card rides in the system prompt (11,094
  characters) in all three.

## Refusals by rule

Each check lists every refused line (at most 20 shown). Each rule is counted three ways:
the checks where it came first, the checks that listed it, and the refused lines those
checks showed.

| Code / rule | super-40: first / checks / lines | kimi-code: first / checks / lines |
|---|---|---|
| Passed | 5 | 13 |
| `sandbox_invalid_source` / `syntax` | 7 / 7 / 7 | 0 |
| `sandbox_literal_required` / `preset_literal` | 4 / 7 / 7 | 0 |
| `sandbox_call` / `missing_function` | 3 / 4 / 20 | 0 |
| `sandbox_expression` / `unknown_name` | 2 / 3 / 3 | 0 |
| `sandbox_binding_mismatch` / `binding_spec` | 2 / 2 / 3 | 0 |
| `sandbox_helper` / `helper_duplicate` | 1 / 1 / 2 | 0 |
| `sandbox_import` / `import_module` | 1 / 1 / 1 | 0 |
| `sandbox_entrypoint` / `run_last` | 1 / 1 / 1 | 0 |
| `sandbox_statement` / `except_shape` | 0 / 6 / 11 | 0 |
| `sandbox_expression` / `method_unknown` | 0 / 2 / 12 | 0 |
| `sandbox_expression` / `module_use` | 0 / 1 / 4 | 0 |
| `sandbox_assignment` / `assign_unpack` | 0 / 1 / 1 | 0 |
| `sandbox_expression` / `lambda` | 0 / 1 / 1 | 0 |
| `sandbox_recursion` / `recursion` | 0 | 1 / 1 / 1 |
| Checks in all | 26 | 14 |
| Refused lines in all | 78, at most 25 in one check | 1 |

dsflash called no check.

Every version that each session wrote, run offline through the same validator at no cost:

| | super-40 | dsflash | kimi-code |
|---|---|---|---|
| Versions of `harness.py` kept | 36 | 1 (the starting file) | 15 |
| Pass as written | 8 | 1 | 14 |
| Refused lines in all / most in one version | 92 / 25 | 0 | 1 / 1 |

- All 7 of super-40's `syntax` refusals, and the syntax error that refused its
  submission, were the placeholder line. Of the other refusals, its first two checks
  listed 25 lines (in 7 rules) and 19; later checks listed 1 to 5 each.

## What happened

1. **Kimi got past the language, then plateaued on harness quality.**
   - From its fourth version on, its harness parses a draft (total, subtotal, tax, item
     count) in code, sends it with the receipt to Lightning, and returns Lightning's
     corrected JSON. Lightning has the last word. From the seventh version, a second
     call follows when the first reply cannot be read, and the code's own parse is used
     only when neither can. Its measures show 1.0 Lightning calls a receipt.
   - The measures rose from 0 of 16 to 4 of 16 by turn 29 and stayed there for 8 measures
     and about 30 turns. Its last measure, at turn 70, gave 2 of 16.
   - At $0.0229 a turn, $1.75 bought 71 turns. The two early replies that were all
     reasoning cost $0.125 together.
2. **Super lost the last third of its session to the trim placeholder.**
   - Super's reported prompt shrank between turns 20 times, from turn 12 on: each is the
     context cap trimming the history. Twice, right after a trim, Super wrote a trimmed
     write's placeholder as the new file.
   - At turn 68 it wrote `[8737 characters, removed ...]`, the placeholder of its own
     8,737-character write at turn 35, and wrote it 6 times in all. It recovered with a
     fresh small file at turn 82 and built back to a 285-line version, which passed its
     check at turn 104 and scored 0 of 3 at turn 106, its only measure. It then grew that
     version to 390 lines.
   - From turn 139 it wrote the placeholder 37 more times, now quoting its own earlier
     placeholder write (`[112 characters, removed ...]`); once it came back with a word
     changed. It checked 6 times, each refused for syntax, and submitted the placeholder
     at turn 191.
   - 65 of its 200 turns read `harness.py`.
3. **DeepSeek with thinking off plans in the open.** Both cut replies were plain-text
   analysis of the task and the cases, with no tool call. The re-ask ("Take a smaller
   step: edit part of a file, or write a shorter one.") did not change that. Nothing it
   wrote ever reached a file.
4. **The relay's Super budget paced super-40 as it paced S3.** No prompt token came from
   the cache, so each turn counted in full against 180,000 tokens a minute. Kimi and
   DeepSeek used the same limits without one wait.

## Across the engineers so far

Development only. U3 and S3 are from G1 retry 2.

| | U3 | S3 | super-40 | dsflash | kimi-code |
|---|---|---|---|---|---|
| Engineer | Ultra, thinking on | Super, thinking off | Super, thinking off | DeepSeek V4 Flash, thinking off | Kimi K2.7 Code, thinking on |
| Forge cap, minutes | $1.75, 25 | $1.75, 25 | $1.75, 40 | $0.60, 40 | $1.75, 30 |
| Session ended by | its cap | its 25 minutes | Pi's 200 turns | a second cut reply | its cap |
| Agent turns | 68 | 165 | 200 | 4 | 71 |
| `forge_check` calls that passed | 2 of 12 | 6 of 15 | 5 of 26 | none called | 13 of 14 |
| Turn of the first passing check | 41 | 28 | 36 | none | 6 |
| Versions that pass as written | 2 of 14 | 5 of 43 | 8 of 36 | 1 of 1, the starting file | 14 of 15 |
| Measures, best | 2, 0/5 | 4, 1/3 | 1, 0/3 | 0 | 14, 4/16 |
| Submissions | 0 | 0 | 1, refused | 0 | 0 |
| Agent $ per turn | $0.0242 | $0.0075 | $0.0083 | $0.0035 | $0.0229 |
| Cache-hit share | 66.7% | 0% | 0% | 67.4% | 78.5% |
| Turns a minute | 14 | 6.6 | 5.6 | 1.9 | 9.3 |
| Relay 429s | 0 | 16 | 43 | 0 | 0 |
| Stage 4 exam, signed | 17/50 | 16/50 | 15/50 | 16/50 | 16/50 |

## What this says for the decision

- **No engineer here should be named the evidence engineer on this data.** None
  submitted a harness that ranked, and none measured above the preset on practice.
  Kimi's best, 4 of 16 (0.25), is close to the preset's 0.26 on all 50.
- **With Kimi, the language stopped being the blocker.** Kimi passed 13 of 14 checks
  with the same pack v3 language card the others had. What no engineer has
  found yet is a harness that beats the preset. The ceiling diagnostic run before this
  bake-off (development only, in-sample, not in the repository) points the same way:
  there, code that reads the amounts itself took the preset from 0.22 to 0.48 on
  practice. Kimi's harnesses let Lightning have the last word instead.
- **Two product behaviors showed up that are not about the engineer model:**
  1. The context cap's `write` placeholder reads like file content, and Super copied it
     into the file (43 times here, twice in S3). That is a bug. A check could refuse a
     write whose content is the placeholder, or the trimmed history could stop showing
     it as `write` content.
  2. A re-asked reply that is cut again ends the session as a final answer, even when
     nothing was submitted. That is the documented rule in `architect/backends/pi.py`,
     and it ended dsflash after 98 seconds. Whether it should end a session that has not
     submitted yet is a design question.
- **If the owner wants one more engineer try before the freeze:**
  - Kimi K2.7 Code is the only engineer that writes valid code from the start. It is not
    a Nemotron model, though. Using it as the builder changes the "core loop on Nemotron"
    story, and judges' Forge builds would need the relay to serve it, which the owner's
    `nvidia/*`-only relay rule does not allow. Both are the owner's call.
  - Super would need the placeholder fix and more than Pi's 200 turns, since super-40 hit
    that limit at 35.5 minutes. Its refused checks list fewer lines than Ultra's: a median
    of 1 here and 4 in S3, against 9.5 in U3.
  - DeepSeek with thinking on is untested. One session at the same $0.60 cap would show
    whether it can work. It was not run here, because this bake-off fixed thinking off.
    $0.88 of the $4.30 cap was left unspent.
- Until one of these lands an accepted submission, the pre-registration should keep F3 as
  the stated fallback (A7 and A7b drop, H1 to H3 test A5, outcome O5) and report these
  session counts.

## Spend

| Item | Calls | Super | DeepSeek | Kimi | Lightning | Total |
|---|---|---|---|---|---|---|
| Path check before the sessions | 10 | | $0.00013496 | $0.0010859 | | $0.00122086 |
| super-40 | 753 | $1.6695552 | | | $0.03245724 | $1.70201244 |
| dsflash | 554 | | $0.01381548 | | $0.03230538 | $0.04612086 |
| kimi-code | 861 | | | $1.62939185 | $0.03888516 | $1.66827701 |
| **Total** | **2,178** | **$1.6695552** | **$0.01395044** | **$1.63047775** | **$0.10364778** | **$3.41763117** |

- From the relay ledger. The `evidence` code's spend went from $6.24505778 to
  $9.66268895. All 2,178 calls returned 200 and were settled from the usage Token Factory
  reported. None was booked at its full hold, released, or refused for money. The largest
  hold for one call was $0.186, on a Kimi turn.
- Prices per million tokens: the Token Factory list prices of 2026-10-03 for Nemotron
  (Super $0.30 in and $0.90 out, Lightning $0.06 and $0.24), and of 2026-10-05 for DeepSeek
  V4 Flash ($0.14 and $0.28) and Kimi K2.7 Code ($0.95 and $4.00). Cached tokens count at
  the full input price.
- All of these calls went through the relay, so its ledger already holds them. Its global
  total is now $10.23514007 of its $26 cap.

## Offline re-check on Forge pack v4

Forge pack v4 (`d6d47f9`, `a41fe88` and `7d5cbfb` on this branch) answers two things this
bake-off found. A trimmed `write` or `edit` text now reads as a note, not as a stand-in for
the text, and with the parent context cap on, a write or edit that carries any of the cap's
notes is refused with a message to read the file and write it again. The language also
accepts typed and named excepts, three loop names for `zip` of three lists, the character
tests, `chr`, `ord`, `split()` on whitespace, `strip`, `lstrip` and `rstrip` with
characters, `rfind`, `copy`, and `min` and `max` of two values
(`docs/interfaces/phase9-forge.md`, Section 4.16).

Every kept copy of `harness.py` from G1, the retry, retry 2 and this bake-off went through
the pack v3 and pack v4 validators, offline and at no cost. The counts include each
session's starting file where it was kept, so U3 shows 15 copies and S3 44.

| | G1 U2 | R1 | R2 | U3 | S3 | super-40 | dsflash | kimi-code | All |
|---|---|---|---|---|---|---|---|---|---|
| Kept copies | 19 | 13 | 14 | 15 | 44 | 36 | 1 | 15 | 157 |
| Pass as written, pack v3 (`a8cb92f`) | 2 | 4 | 1 | 3 | 6 | 8 | 1 | 14 | 39 |
| Pass as written, pack v4 | 4 | 4 | 1 | 6 | 26 | 12 | 1 | 14 | 68 |
| Refused lines, pack v3 / v4 | 64 / 44 | 111 / 109 | 494 / 465 | 201 / 91 | 181 / 22 | 92 / 52 | 0 / 0 | 1 / 1 | 1,144 / 784 |

- No copy is refused for anything new. Each copy's refused lines under pack v4 are among
  its pack v3 ones, and every copy that passed still passes.
- Refused lines by rule, pack v3 / v4: `except_shape` 96 / 4, `method_unknown` 237 / 114,
  `missing_function` 89 / 18, `method_arity` 40 / 14, `builtin_arity` 34 / 1 and
  `for_target` 27 / 12. No other rule changed.
- S3, retry 2's Super session, gains the most: 26 of its 44 copies pass now. All 14 of its
  remaining `method_arity` lines are `split` with a maxsplit argument, which pack v4 still
  refuses.
- What still fails is outside this round: imports and the names they bind (`json`, `re`),
  regular expressions (`search`, `group`, `match`, `findall`, `sub`), classes, helpers named
  after builtins, `setdefault`, `add` and `set()`, `next`, `reversed`, and nested loop
  targets such as `for i, (a, b) in ...`.
- 5 kept copies hold a context-cap note, 4 of super-40's and 1 of S3's. Pack v4 refuses a
  write or edit that carries one, so none of them would have replaced `harness.py`.
- `architect forge pack` with the default settings now gives
  `055185299a44934558f8e4df471812e00629a08ba40699683ab0e92c0855660b`, and its task-text
  check finds nothing with this bake-off's spec, `spec.json`, `task.json` and `RULES.md`
  as sources. A build's own hash still depends on its Forge cap and minutes.
