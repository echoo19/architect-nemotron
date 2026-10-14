# Forge on Nemotron with native tool calls: G1 sessions, 2026-10-04

Three live Forge builds on the native tool-call transport: one with Super as the engineer
(S1) and two with Ultra (U1, U2). They test the plan's gates G1a (mechanics and cost) and
G1b (quality) on the CORD receipts rehearsal floor.

The rehearsal floor is for development only. Nothing here is evidence, and no claim may
use these numbers.

## Summary

- **G1a fails on one criterion: neither Ultra session submitted anything.** The transport
  itself passed. All 281 agent replies arrived with well-formed tool calls (304 calls in
  all), with no repair, no reply cut at the output cap and no `REASONING_CUT`. Every turn
  has its own cost row, and each Ultra session stayed under $2 and 25 minutes.
- **G1b fails: 0 of 2 Ultra sessions ended with an accepted submission.** Super submitted
  twice. Both submissions were valid v2 code, and both scored below the Stage 2 preset on
  the practice receipts.
- Both Ultra sessions ran into their $1.75 cap within 4 to 5 minutes while stuck on one
  check code: 23 of their 31 `forge_check` calls returned `sandbox_helper`. Ultra puts
  type annotations on its helpers (`def only_digits(s: str) -> str:`). The sandbox
  language forbids them, but neither the check's hint nor LANGUAGE.md says so. U2 wrote 18
  versions of harness.py. Forge's own check rejects 16 of them with `sandbox_helper`, and
  in 15 of those the rejected line is an annotated helper.
- An Ultra turn costs about $0.021, and prompt tokens are about 90% of that. 69% of Ultra's
  prompt tokens were cache hits. No cached-input price is published, so this note counts
  them at the full input price, as Architect does. A $1.75 cap buys about 80 Ultra turns.
  Super's 123 turns cost $0.0077 each on average, and its cache never hit.
- Live spend: **$4.3594** at list prices, against this task's cap of $5.00. These calls went
  to Token Factory directly, not through the spend relay.

## Setup

- Code: `hackathon/w3-native-forge` at `e91a428`: native tool calls, the Forge pack and
  cost fixes, merged with `hackathon/nebius` at `fc2e5f7`.
- Each build ran from a fresh workspace (`C:/t/g1-s1`, `C:/t/g1-u1`, `C:/t/g1-u2`), each
  with its own signing key:

  ```
  architect compile spec.json --profile profile.json --approved-spec spec.json \
    --forge --forge-trigger always --forge-usd <cap> --forge-minutes 25 --yes --audit <dir>
  ```

- It ran in process under a driver kept outside the repository.
  - The driver wraps the adapter's `_post` and `stream_chat`, `PiBackend.run` and the
    native re-ask helpers, and it uses the existing `FORGE_EVENTS`, `add_result_observer`
    and `add_stage_observer` hooks.
  - It records usage, stop reasons, tool names and argument sizes. It never records keys,
    headers or the text of written files.
  - It refuses any call once its process has spent a set amount: $1.15 for S1 and $1.90
    for each Ultra session. That stop never fired.
- Floor: `examples/receipts/floors/rehearsal.json`.
  - 100 CORD v2 receipts, split by sorted id into 50 practice and 50 exam receipts.
  - The `receipt_v1` checker and the rehearsal manifest, so the reports label the floor
    CORD v2.
  - Forge's smoke floor is the practice half, as build mode and the bench derive it.
- Spec: the bench's own suite v2 receipts spec, with two changes:
  - the play model is Lightning;
  - the bar is the pilot's 0.40, since the settings file still holds the 0.80 placeholder.

  The task is the receipts-v2 rules from RULES.md, word for word, and the schemas come
  from `task.json`. The rest is the settings file's: invalid output counts as a miss (at
  most 2%), an input limit of 8,192 characters, reply parsing `first`, and $0.02 and 60 s
  per run.
- Profile: the models of `examples/nebius/profile.json`.
  - Lightning plays at temperature 0, with thinking off and `max_tokens` 512 (prereg
    section 4.2).
  - Stage 2 measures 4 preset configurations, as A5 and A7 do. No rewriting.
  - The compile budget is $0.25 and 1,200 s, plus the Forge cap.
  - Forge settings:
    - agent mode, trigger `always`, per-case feedback;
    - 24 measures, 6 submissions, 0 spawns, 25 minutes;
    - the pack defaults: agent replies capped at 12,288 tokens, and a 60,000-character
      context cap applied by the parent;
    - TUNING.md shows 16 practice cases and gives detail for the first 8 failures.

| | S1 | U1 | U2 |
|---|---|---|---|
| Engineer | `nvidia/nemotron-3-super-120b-a12b` | `nvidia/Nemotron-3-Ultra-550b-a55b` | the same as U1 |
| Settings | thinking off; temperature 0 | thinking on; temperature 1.0; top_p 0.95 | the same as U1 |
| Forge cap | $1.00 | $1.75 | $1.75 |
| Forge pack hash (the cap is part of it) | `31a4ec58...` | `2b6a79f0...` | `2b6a79f0...` |
| Run (UTC) | 19:46:51 to 20:05:13 | 20:05:41 to 20:17:09 | 20:18:22 to 20:28:54 |

## Results

| | S1 (Super) | U1 (Ultra) | U2 (Ultra) |
|---|---|---|---|
| Wall time: whole compile / Forge session | 1,102 s / 669 s | 688 s / 274 s | 632 s / 239 s |
| Session ended by | its $1.00 cap | its $1.75 cap | its $1.75 cap |
| Agent turns (model calls) | 123 | 78 | 80 |
| Replies whose tool calls all parsed | 123 of 123 | 78 of 78 | 80 of 80 |
| Tool calls / malformed | 123 / 0 | 85 / 0 | 96 / 0 |
| Re-asks (bad arguments, cut replies, early finals) | 0 | 0 | 0 |
| Replies cut at `max_tokens` | 0 | 0 | 0 |
| `REASONING_CUT` | 0 | 0 | 0 |
| `forge_check` calls that passed | 7 of 30 | 0 of 16 | 1 of 15, on the starting harness before any write |
| Submissions / valid v2 code | 2 / 2 | 0 | 0 |
| Measures on practice receipts | 1/3, 1/3, 3/16, 6/20, 6/25, 1/3, 1/10, plus one refused as invalid source | none | none |
| Submissions on all 50 practice receipts | 11/50 (0.22) and 12/50 (0.24) | none | none |
| Stage 2 best preset on practice | 13/50 (0.26) | 13/50 (0.26) | 13/50 (0.26) |
| Accepted submissions | 0 | 0 | 0 |
| Stage 4, from the signed report | the Stage 2 preset, refused: exam 16/50 (0.32) against 0.40, 0 invalid | the same: refused, 16/50 | the same: refused, 16/50 |
| Agent $ | $0.9440 | $1.6485 | $1.6567 |
| Lightning $: Stage 2 / Forge / Stage 4 | $0.0292 / $0.0135 / $0.0030 | $0.0293 / $0 / $0.0030 | $0.0294 / $0 / $0.0030 |
| Build total | $0.9896 | $1.6808 | $1.6890 |
| Agent $ per turn | $0.0077 | $0.0211 | $0.0207 |
| Prompt tokens per turn, mean / max | 24,213 / 35,812 | 18,772 / 24,245 | 19,262 / 26,267 |
| Cache-hit share of prompt tokens | 0% | 69.8% | 68.9% |
| Reasoning share of completion tokens | 0 (thinking off) | 11.9% | 28.9% |
| Largest reply, in tokens | 2,839 | 5,272 | 2,702 |

Tool calls by kind, with refusals in brackets:

| | S1 | U1 | U2 |
|---|---|---|---|
| read | 41 (8) | 52 (1) | 61 |
| write | 34 | 17 | 13 |
| edit | 4 (4) | 0 | 6 (1) |
| forge_check | 30 | 16 | 15 |
| forge_dryrun | 4 (1) | 0 | 1 |
| forge_measure | 8 | 0 | 0 |
| forge_submit | 2 | 0 | 0 |

- A refused read named a directory, a missing file or an offset past the end. Each was
  refused with a message that says which.
- All 4 of Super's edits failed Pi's argument check, and so did one dry run that passed an
  extra `path` argument. U2's refused edit changed nothing in the file.
- Architect's own records agree with the driver. The per-turn rows in
  `telemetry/forge_turns/` (123, 78 and 80 rows) give the same agent dollars, and the
  telemetry totals give the same build totals, to the micro-dollar.
- Stage 2's pick scored 13/50 on practice in all three builds. It was the plain extractor
  configuration (no retries, no verifier) in S1 and U1, and the same with one retry in U2.
  Stage 2 took 6.0 to 6.6 minutes for about 500 Lightning calls made one at a time,
  before Forge started.

## G1a and G1b against the plan's criteria

| Criterion | S1 | U1 | U2 | Verdict |
|---|---|---|---|---|
| The session finishes | yes, at its cap | yes, at its cap | yes, at its cap | pass |
| At least 95% of turns parse | 100% | 100% | 100% | pass |
| Submissions are valid v2 code | 2 of 2 | none submitted | none submitted | **fail** |
| No `REASONING_CUT` | 0 | 0 | 0 | pass |
| $ per turn is logged | 123 rows | 78 rows | 80 rows | pass |
| Each Ultra session costs at most $2 and takes at most 25 min | | $1.68; 11.5 min, Forge 4.6 min | $1.69; 10.5 min, Forge 4.0 min | pass; the cap ended both early |
| G1b: an Ultra session ends with an accepted submission | | no | no | **fail** |

## What went wrong

1. **One hint covers several helper rules.**
   - `sandbox_helper` covers type annotations, default values, `*args`, a helper named
     after a builtin, a helper defined twice, an async helper whose first parameter is not
     `context`, and a sync helper that awaits.
   - Its hint is "Helper: def name(a, b) or async def name(context, b); at most 8 plain
     parameters; sync helpers never await or use context." It names none of the first
     five. LANGUAGE.md shows the shape only by example.
   - U1: 10 of its 16 checks returned `sandbox_helper`. Its file versions were not kept.
   - U2's harness.py was copied each time it changed: the starting file and 18 versions
     that U2 wrote. Run offline through Forge's own check at no cost, 16 of the 18 fail
     with `sandbox_helper` and 2 with `sandbox_signature`. In 15 of the 16 the rejected
     line is an annotated helper, and one of those 15 also reuses a builtin's name
     (`lower`). The 16th defines a helper twice.
   - With the annotations removed, the same files fail next on a default value, a helper
     named `lower`, and an async helper whose first parameter is `ctx`. Each of these gets
     the same hint again.
2. **Ultra keeps re-reading the language reference.** It read LANGUAGE.md 7 times in U1
   and 14 times in U2; Super read it once.
   - LANGUAGE.md is 14,118 characters and the first file Ultra reads. The 60,000-character
     cap removes the oldest tool results first, so it goes early.
   - Each re-read costs a turn, about $0.02 on Ultra, and puts the whole file back.
3. **Prompt tokens set the cost of an Ultra turn.** About 19,000 prompt tokens a turn at
   $1 per million made up 89% (U1) and 93% (U2) of the agent's spend. Reasoning was 1% to
   2%.
   - The cache served 69% of those tokens. No cached-input price is published, so they are
     counted here at the full input price. If Token Factory bills them for less, the real
     cost is lower.
   - The prompt also grows with the session. The cap thins old message bodies but keeps
     every message: Super's prompt went from 17,000 tokens at turn 10 to 31,000 at turn
     80, with 239 messages at turn 120.
4. **Super falls back to the preset.**
   - Its first harness that passed `forge_check` came at turn 63, after 12 failed checks.
     Over the session 23 of its 30 checks failed, with 7 different codes. 8 of them were
     `sandbox_binding_mismatch`: the preset call's literals no longer matched the binding.
   - Both of its submissions were the Stage 2 preset call itself, with nothing added.
     They scored 0.22 and 0.24 against the preset's 0.26.
   - Its 4 edits all failed, and it wrote the whole file 34 times (153,252 characters in
     all).
   - Its prompt cache never hit (0 of 2,978,190 prompt tokens), as in the envelope
     probe.
5. **Acceptance needs the bar on practice.** `forge_submit` accepts a submission only if
   it reaches the bar (0.40) on all 50 practice receipts and on the smoke set, and ranks
   above the incumbent. A submission that beats the preset's 0.26 but stays under 0.40 is
   refused as `tuning_failed`. On this floor G1b asks Forge to lift Lightning from 13 to
   at least 20 of the 50 practice receipts.
6. **A 25-case smoke set allows no invalid run.**
   - S1's first submission scored 0 on the smoke set. One of its 25 smoke runs ended
     without a valid output, and under `count_as_miss` at 2%, 25 cases allow none, so the
     set was not scored. The same harness had no invalid run on the 50 practice receipts.
   - It changed no verdict here, since that submission had already missed the bar on
     practice. The evidence floor's smoke set has 50 cases and allows one.

## Against the envelope probes of 2026-10-03

| | Envelope, 10-03: Super / Ultra | Native, 10-04: Super / Ultra (2 sessions) |
|---|---|---|
| Replies that parsed | 22 of 23 / 2 of 5 | 123 of 123 / 158 of 158 |
| Repairs | 1 / 2 | 0 / 0 |
| Replies cut at the output cap | 0 / 2 | 0 / 0 |
| LANGUAGE.md read | never / never | once / 7 and 14 times |
| Valid v2 code | none / none | 2 submissions / none |

The floors differ (12 synthetic invoice cases then, 100 CORD receipts now), so only the
mechanics compare.

## Recommendation

- The native transport works for both engineers. The miss that remains is in the Forge
  pack's feedback, which is the same for every engineer. Ultra with thinking off (F1)
  would get the same hint.
- Take G1b's miss path first: one day of task-neutral pack work, then a retry. All of
  these change the pack hash, so they land before the pre-registration on 10/11:
  1. `sandbox_helper`, and any other code that covers several rules, names the rule that
     broke and the name or parameter involved.
  2. LANGUAGE.md lists the helper rules: no annotations, no default values, no `*args`,
     a name that is neither a builtin nor used twice, and `context` first in an async
     helper.
  3. The context cap drops the language reference last, or a short rules section moves
     into the system prompt.
  4. Before any spend, check that each of the 16 rejected U2 versions now gets a message
     that names its first broken rule.
- Retry with two Ultra sessions at $1.75 each, on the same floor and settings: about
  $3.50.
- Set the per-session cap after the retry. The plan's rule (1.5 times the G1a median, at
  most $2.50) gives $2.47 from these Forge sessions' median of $1.65. At $0.021 a turn,
  $2.00 buys about 95 Ultra turns. Super needed 63 turns to reach its first valid harness.

## Spend

| Build | Agent | Lightning | Total |
|---|---|---|---|
| S1 | $0.9440 | $0.0457 | $0.9896 |
| U1 | $1.6485 | $0.0323 | $1.6808 |
| U2 | $1.6567 | $0.0323 | $1.6890 |
| Driver self-test: one streamed and one plain Lightning call | | $0.00003 | $0.00003 |
| **Total** | **$4.2491** | **$0.1103** | **$4.3594** |

- Prices are the Token Factory list prices of 2026-10-03, per million tokens: Ultra $1.00
  in and $3.00 out, Super $0.30 and $0.90, Lightning $0.06 and $0.24. Cached tokens count
  at the full input price.
- No 429, 503 or 529 responses, no retries and no HTTP errors.
- These calls used the project key directly, so the relay ledger does not include them.
  Add $4.36 when the ledger is compared with the console.
