# Forge pack v2 on Nemotron: the G1 retry, 2026-10-04

Two more live Forge builds with Ultra as the engineer, R1 and R2. They repeat G1's U1 and
U2 (`docs/feedback/probes/2026-10-04-forge-native-g1.md`) on the Forge pack v2 code, and
this time every call went through the local spend relay. They retry the plan's gates G1a
(mechanics and cost) and G1b (quality) on the CORD receipts rehearsal floor.

The rehearsal floor is for development only. Nothing here is evidence, and no claim may
use these numbers.

## Summary

- **G1b fails again: neither retry session ended with an accepted submission.** With
  G1's U1 and U2, that makes 0 of 4 Ultra sessions. Neither retry session submitted
  anything. Both ran into their $1.75 cap, after 71 and 70 turns.
- **Pack v2 removed the G1 blocker.** No check returned `sandbox_helper` (0 of 24, against
  23 of 31 in G1), and neither session read LANGUAGE.md, which now rides in the system
  prompt (G1: 7 and 14 reads). Run offline through G1's validator at no cost, 16 of the
  25 harness versions the two sessions wrote would have been refused first for an
  annotated helper.
- **A new blocker took its place.** Ultra writes ordinary Python, and a check names only
  the first construct the language refuses. The drafts held several each: code after
  `run`, `run` with parameters of its own, imports, comprehensions (up to 13 in one file)
  and methods the language lacks. R1 passed a check at turn 43, measured 2 of 5 and then
  5 of 20 practice receipts, rewrote the file and spent the rest of its money getting the
  new version through. R2 passed none of its 14 checks.
- **G1a fails on the same criterion as before: there was no submission to be valid v2
  code.** The mechanics pass again. All 141 replies parsed, none of the 159 tool calls was
  malformed, and there was no `REASONING_CUT`. One reply in each session ran to the
  12,288-token output cap after a one-line tool call.
- **The relay paced Ultra to about 3.5 turns a minute.** Each session waited out 55 of
  the relay's 429 answers (840 s and 887 s, about 70% of each Forge session). Forge took
  20 to 21 minutes instead of G1's 4 to 5, and each whole compile took just under 25
  minutes. No call failed, and the money cap, not the clock, ended both sessions.
- Live spend: **$3.3404885** through the relay, against this task's cap of $3.60, from
  the relay ledger.

## Setup

- Code: `hackathon/forge-pack-v2` at `003a021`, six commits on `919738f` (the G1 code).
  Pack v2 changes four things (`docs/interfaces/phase9-forge.md`, section 4.14):
  - the sandbox language accepts type annotations, literal default values, 19 method
    spellings of builtins (`s.split(sep)`), `isinstance` with a plain type, `get` and
    `at` without a default, and binding arguments held in constants;
  - every refusal names its rule and line in a fixed message, for example "Line 46:
    Comprehensions and generator expressions are not supported. Write a for loop that
    appends to a list.";
  - LANGUAGE.md is a 1,227-word card that rides in the system prompt on every turn;
  - DESIGN.md adds two strategies: enforce the task's stated rules in code, and submit
    only when `forge_measure` shows the harness beats the preset.
- This branch does not have `aa90985` from `hackathon/nebius`, so a reply cut at the
  output cap still runs its tool calls.
- Workspaces `C:/t/g2-u1` (R1) and `C:/t/g2-u2` (R2), fresh, each with its own signing
  key. The floor files, the spec and the profile are byte for byte G1's, with one change:
  every model entry's `base_url` is the relay, `http://127.0.0.1:8787/v1`.
  - The profile names the credential by reference (`api_key_ref: "nebius"`). Each
    workspace's credentials file holds the relay's `evidence` access code, and no Token
    Factory key was in Architect's environment.
  - The play model, the Stage 2 setup, the compile budget and the Forge settings are G1's:
    Lightning at temperature 0 with thinking off and `max_tokens` 512; 4 preset
    configurations; $0.25 and 1,200 s plus the Forge cap; 24 measures, 6 submissions, 0
    spawns; agent replies capped at 12,288 tokens and a 60,000-character context cap.
- Same command as G1, with `--audit C:/t/g2-audit-<name>`, under the same driver. The
  driver now also keeps each refusal's rule and message.
- Forge pack hash with these settings (cap $1.75, 25 minutes), as both sessions recorded
  it: `2f83f5f883373b20356933f4a45ec22022a389ca40b0f4d237968069aaad3491`.
  `architect forge pack` with the same flags gives the same hash, and its task-text check
  finds nothing (the spec, `task.json` and `RULES.md` as sources).
- Relay: the local host from G0 (`start-relay.sh`, a file ledger outside the repository,
  the prices of 2026-10-03), up from 22:54 to 23:44 UTC and stopped afterwards.
  - For this retry the `evidence` code's total cap was set to its earlier spend plus
    $3.60, and its share of each model's per-minute budget to 100%. At the default 25%
    the code could send Ultra about 25,000 tokens a minute, about one Forge turn. Both
    settings went back to what they were ($12 and 25%) after the runs.
  - The relay's own limiter for each model stays at half of the per-minute limits in its
    config (Ultra: 300 requests and 200,000 tokens).
- Before the sessions, one plain and one streamed Lightning call through the relay
  checked the path, for $0.00003114.

| | R1 | R2 |
|---|---|---|
| Workspace | `C:/t/g2-u1` | `C:/t/g2-u2` |
| Engineer | `nvidia/Nemotron-3-Ultra-550b-a55b`: thinking on, temperature 1.0, top_p 0.95 | the same |
| Forge cap | $1.75 and 25 minutes | the same |
| Run (UTC) | 22:54:39 to 23:19:08 | 23:19:24 to 23:44:11 |

## Results

| | R1 | R2 |
|---|---|---|
| Wall time: whole compile / Forge session | 1,469 s / 1,219 s | 1,486 s / 1,251 s |
| Session ended by | its $1.75 cap | its $1.75 cap |
| Agent turns (model calls) | 71 | 70 |
| Replies whose tool calls all parsed | 71 of 71 | 70 of 70 |
| Tool calls / malformed | 81 / 0 | 78 / 0 |
| Re-asks (bad arguments, cut replies, early finals) | 1, an early final | 0 |
| Replies cut at `max_tokens` | 1 | 1 |
| `REASONING_CUT` | 0 | 0 |
| `forge_check` calls that passed | 2 of 10, the first at turn 43 | 0 of 14 |
| Dry runs | 2: one passed, one stopped at run time (`sandbox_index`) | none |
| Measures on practice receipts | 2/5 and 5/20, plus one refused as invalid source | none |
| Submissions / valid v2 code | 0 | 0 |
| Stage 2 best preset on practice | 13/50 (0.26) | 13/50 (0.26) |
| `forge_rank` against the Stage 2 best | nothing was submitted to rank, so Forge's best stays the Stage 2 pick | the same |
| Accepted submissions | 0 | 0 |
| Stage 4, from the signed report | the Stage 2 preset, refused: exam 16/50 (0.32) against 0.40, 0 invalid | the same: refused, 16/50 |
| Agent $ | $1.6325 | $1.6418 |
| Lightning $: Stage 2 / Forge / Stage 4 | $0.0293 / $0.0015 / $0.0030 | $0.0293 / $0 / $0.0030 |
| Build total | $1.6663 | $1.6741 |
| Agent $ per turn | $0.0230 | $0.0235 |
| Prompt tokens per turn, mean / max | 20,919 / 26,654 | 20,944 / 27,150 |
| Cache-hit share of prompt tokens | 75.6% | 76.9% |
| Reasoning share of completion tokens | 22.3% | 16.0% |
| Largest reply, in tokens | 12,288 (the cut one) | 12,288 (the cut one) |
| Seconds per turn, mean, waits included | 16.9 | 17.9 |
| Relay 429s waited out / seconds waited | 55 / 840 | 55 / 887 |

Tool calls by kind, with refusals in brackets:

| | R1 | R2 |
|---|---|---|
| read | 46 (1) | 50 (1) |
| write | 9 | 6 |
| edit | 7 (4) | 8 (1) |
| forge_check | 11 (1) | 14 |
| forge_dryrun | 2 | 0 |
| forge_measure | 6 (3) | 0 |
| forge_submit | 0 | 0 |

- R1 called `forge_measure` three times with a `path` argument the tool does not take,
  and Pi's argument check refused each one. One check and one edit in R1, and one edit in
  R2, failed the same check. Three of R1's edits named text that was not in the file.
- In each session one read named the `templates/` directory.
- The records agree. Architect's per-turn rows in `telemetry/forge_turns/` (71 and 70
  rows) give the same agent dollars as the driver, and the relay ledger's rows for each
  session give the same build totals, to the micro-dollar.
- Stage 2's pick was the extractor preset with one retry and no verifier in both builds,
  as in G1's U2.

## Check results by rule

Each row counts a check's answer: a pass, or the first refusal it named, by code and
rule.

| Code / rule | R1 | R2 |
|---|---|---|
| Passed | 2 | 0 |
| `sandbox_entrypoint` / `run_last`: code after `run` | 2 | 2 |
| `sandbox_signature` / `run_signature` | 1 | 1 |
| `sandbox_expression` / `comprehension` | 2 | 2 |
| `sandbox_expression` / `method_unknown` (`isdigit` in both, `isupper` in R2) | 1 | 3 |
| `sandbox_call` / `missing_function` (`any`) | 0 | 2 |
| `sandbox_call` / `method_arity` and `builtin_arity` (`split` with no separator) | 0 | 2 |
| `sandbox_statement` / `import_inside` | 1 | 0 |
| `sandbox_import` / `import` | 0 | 1 |
| `sandbox_assignment` / `for_target` | 1 | 0 |
| `sandbox_invalid_source` / `syntax` | 0 | 1 |
| `sandbox_helper`, G1's blocker | 0 | 0 |
| Checks in all | 10 | 14 |

R1's first measure was refused for the same kind of reason (`forge_invalid_source`, rule
`import`), which this table does not count.

## G1a and G1b against the plan's criteria

| Criterion | R1 | R2 | Verdict |
|---|---|---|---|
| The session finishes | yes, at its cap | yes, at its cap | pass |
| At least 95% of turns parse | 100% | 100% | pass |
| Submissions are valid v2 code | none submitted | none submitted | **fail** |
| No `REASONING_CUT` | 0 | 0 | pass |
| $ per turn is logged | 71 rows | 70 rows | pass |
| Each Ultra session costs at most $2 and takes at most 25 min | $1.67; 24.5 min, Forge 20.3 min | $1.67; 24.8 min, Forge 20.9 min | pass, with under a minute to spare on the whole compile |
| G1b: an Ultra session ends with an accepted submission | no | no | **fail**: 0 of 2, and 0 of 4 with G1 |

## What changed since G1

| | G1 U1 | G1 U2 | R1 | R2 |
|---|---|---|---|---|
| Path to Token Factory | direct | direct | relay | relay |
| `forge_check` calls that passed | 0 of 16 | 1 of 15, the starting file | 2 of 10 | 0 of 14 |
| Checks refused as `sandbox_helper` | 10 | 13 | 0 | 0 |
| LANGUAGE.md read | 7 times | 14 times | never | never |
| Measures | 0 | 0 | 3 | 0 |
| Agent turns | 78 | 80 | 71 | 70 |
| Prompt tokens per turn, mean | 18,772 | 19,262 | 20,919 | 20,944 |
| Agent $ per turn | $0.0211 | $0.0207 | $0.0230 | $0.0235 |
| Cache-hit share of prompt tokens | 69.8% | 68.9% | 75.6% | 76.9% |
| Replies cut at `max_tokens` | 0 | 0 | 1 | 1 |
| Forge session wall time | 274 s | 239 s | 1,219 s | 1,251 s |

## What went wrong

1. **Ultra writes ordinary Python, and each check names one refusal.**
   - Of the 25 harness versions the two sessions wrote, 2 pass the check, both R1's. The
     other 23 each hold at least one construct the language refuses.
   - The first drafts were free-standing Python modules. They imported `re`, `json`,
     `dataclasses` and `typing`, declared `run(receipt, model)` or `run(receipt, ctx)`
     (or no `run` at all) in place of `run(context, task)`, and ended in an
     `if __name__ == "__main__":` block that ran the harness. The prompt already says the
     language is not Python, and the card says `run(context, task)` comes last.
   - Comprehensions are in 21 of the 24 written versions that parse, up to 13 in one
     file. A check reports the first one, so R2 took one file from 13 down to 4 over 8
     versions and ran out of money there. Those 8 versions also kept an `import json`
     inside a function further down, which no check had reached yet.
   - A round from one check to the next took 2 to 25 turns: 3 at the median, 5.7 on
     average, about $0.13. After most refusals the next check named a different rule or
     line; twice it named the same one again.
2. **R1's harness did no better than the preset once it ran.** Its first passing version
   scored 2 of 5 and then 5 of 20 practice receipts (0.25, against the preset's 0.26 on
   all 50). DESIGN.md now says to submit only a harness that beats the preset, and this
   one did not. R1 rewrote the file instead. The rewrite took 6 checks (5 refused), and
   its dry run then stopped at run time on a list index out of range as the money ran
   out.
3. **Through the relay, Ultra turns wait.**
   - The relay's Ultra limiter allows 100,000 tokens a minute, half of the 200,000 in its
     config, and it counts cached prompt tokens. It admits a call only if the last
     minute's tokens plus the new call's prompt and its whole 12,288-token output cap fit.
     At about 21,000 prompt tokens a turn, that is 3 to 4 turns a minute.
   - G1 sent the same kind of turns directly at 17 to 20 a minute, up to about 390,000
     prompt tokens a minute, and Token Factory returned no 429.
   - All 110 waits were the relay's own 429s, and none ran past the adapter's two retries,
     so no call failed. The money cap still ended both sessions, so the pacing cost wall
     time, not turns.
   - A session with a larger cap would reach the 25-minute limit first: at about 17 s a
     turn, 25 minutes is about 88 turns, or about $2.05 at $0.0233 a turn.
4. **Each turn costs 9% to 14% more.**
   - With the card, the system prompt grew from 2,747 to 10,446 characters, about 1,900
     tokens more on every turn. The mean prompt per turn is 1,650 to 2,170 tokens above
     G1's two Ultra sessions.
   - Once in each session, a reply ran to the 12,288-token output cap with 19 and 16
     reasoning tokens, no text shown and a one-line `forge_check` call. Those two replies
     cost $0.050 and $0.056. On this branch the call in a cut reply still runs;
     `hackathon/nebius` runs nothing from a cut reply (`aa90985`).

## Recommendation

- Under the plan, a G1b miss after the pack-work retry moves to the pre-registered
  conditional arms: F1 (Ultra with thinking off), then F2 (Forge on Super), then F3, where
  A7 and A7b drop and H1 to H3 test A5 (outcome O5).
- F1 and F2 would meet the same language and the same one-refusal-per-check loop. In G1,
  Super reached valid code only by submitting the Stage 2 preset call unchanged. Two
  task-neutral pack changes go at what these sessions spent their money on:
  1. Let a check, and the check inside dry run, measure and submit, list every refused
     line in one answer, up to a limit. Messages come from fixed tables and line numbers,
     so the longer answer still echoes no source.
  2. Decide comprehensions again with these counts in hand. They were refused in 4 checks
     and appear in 21 of the 24 written versions that parse.
- Any Forge session that runs through the relay (A7, A7b, a judge's Forge build) will
  run at this pace unless the relay's Ultra limiter changes. Either change the limiter
  (count only uncached prompt tokens, or a larger share of Token Factory's limit for
  Ultra) or set the Forge minutes from about 17 s a turn.
- The pack hash above is for these settings only. Pin the hash again with the final
  settings before the pre-registration.

## Spend

| Item | Calls | Ultra | Lightning | Total |
|---|---|---|---|---|
| Path check before the sessions | 2 | | $0.00003114 | $0.00003114 |
| R1 | 646 | $1.632528 | $0.0337845 | $1.6663125 |
| R2 | 620 | $1.641845 | $0.03229986 | $1.67414486 |
| **Total** | **1,268** | **$3.274373** | **$0.0661155** | **$3.3404885** |

- From the relay ledger. The `evidence` code's spend went from $0.00000672 to
  $3.34049522. All 1,268 calls returned 200 and were settled from the usage Token Factory
  reported. None was booked at its full hold, released, or refused for money. The
  largest hold for one call was $0.18, on an Ultra turn.
- Prices are the Token Factory list prices of 2026-10-03, per million tokens: Ultra $1.00
  in and $3.00 out, Lightning $0.06 and $0.24. Cached tokens count at the full input
  price.
- All of these calls went through the relay, so its ledger already holds them. Its global
  total is now $3.91294634 of its $26 cap.

## Offline re-check on Forge pack v3

Pack v3 (`hackathon/forge-pack-v2`, after the merge of `hackathon/nebius`) answers the two
pack changes above and lists every refused line in one check. Every kept copy of
`harness.py` went through its validator again, offline and at no cost. G1's U2 kept 19
copies, R1 13 and R2 14; G1's U1 and S1 kept none.

| | G1 U2 | R1 | R2 | All |
|---|---|---|---|---|
| Pass as written, pack v2 (`4c70dd7`) | 2 of 19 | 3 of 13 | 1 of 14 | 6 of 46 |
| Pass as written, pack v3 | 2 of 19 | 4 of 13 | 1 of 14 | 7 of 46 |

- The 39 refused copies hold 669 refused lines: a median of 6 per copy (at most 142) and
  a median of 4 different rules. A pack v2 check showed one of them; a pack v3 check lists
  them all, at most 20 at a time.
- No copy is refused for a comprehension or for its `if __name__ == "__main__":` block any
  more. Under pack v2 a comprehension was the first refusal of 5 copies and a main block of
  3 (one of those, R2's third, has no `run` at all and is still refused for that).
- What still fails, by the number of copies that hit it: a helper named after a builtin
  (11), `chr` and `ord` (10), a for target that is not one name or two (9), `re` and its
  patterns (9), an await of something that is not a capability (8), `max(a, b)` with two
  arguments (8), `except` with a type or a name (8), `s.split()` with no separator (8), a
  helper that calls itself (8), and imports of `dataclasses` and `typing` (6 each). Each
  of these now arrives in the first check, with the form that replaces it.
