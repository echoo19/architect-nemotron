# Forge pack v3 on Nemotron: G1 retry 2, 2026-10-05

Two more live Forge builds on the CORD receipts rehearsal floor, on the Forge pack v3
code: U3 with Ultra as the engineer and S3 with Super. U3 repeats the G1 retry's R1 and R2
(`docs/feedback/probes/2026-10-04-forge-native-g1-retry.md`), and S3 repeats G1's Super
session S1 (`docs/feedback/probes/2026-10-04-forge-native-g1.md`). Every call went through
the local spend relay. They retry the plan's gates G1a (mechanics and cost) and G1b
(quality).

The rehearsal floor is for development only. Nothing here is evidence, and no claim may
use these numbers.

## Summary

- **G1b fails again: neither session submitted anything.** With U3, 0 of 5 Ultra sessions
  have ended with an accepted submission (G1's U1 and U2, the retry's R1 and R2, and U3).
  S3, the first Super session on pack v3, did not submit either.
- **Pack v3 made the check loop shorter, but neither engineer wrote a harness that beats
  the preset.**
  - U3's first check found 47 refused lines in 6 rules and showed 20 of them. After seven
    more refused checks, its ninth check, at turn 41, passed. It measured that file twice,
    and both measures scored 0 of 5 practice receipts. U3 then went back to the preset
    call, and its $1.75 cap ran out at turn 68 with that version still refused.
  - S3 passed 6 of its 15 checks, the first at turn 28. Its measures scored 0 of 5, 1 of
    5, 1 of 3 and 1 of 3. It never measured more than 5 receipts and never submitted.
- **G1a fails on the same criterion as before: there was no submission to be valid v2
  code.** The mechanics pass. All 232 replies that arrived parsed, none of the 249 tool
  calls was malformed, no reply was cut at the output cap, and there was no
  `REASONING_CUT`.
- **The relay change ended the waiting for Ultra, not for Super.** U3 got no 429 and ran
  14 turns a minute; R1 and R2 each waited out 55 and ran about 3.5. Super's prompt cache
  never hits, so every prompt token counts against the relay's Super budget. The relay
  held 128 of S3's 164 calls before admitting them, 820 s in all, and answered 16
  requests with a 429 (189 s of client waits). S3 ran 6.6 turns a minute, and the
  25-minute limit ended it with $0.52 of its cap unspent.
- Live spend: **$2.90095038** through the relay, against this task's cap of $3.60, from
  the relay ledger.

## Setup

- Code: `hackathon/forge-pack-v2` at `d02b75f`: pack v3 (`7ac268e`, `f3bec4a`, `d02b75f`)
  on a merge of `hackathon/nebius` at `77431b0` (`4e1f101`). Since the retry, pack v3
  (`docs/interfaces/phase9-forge.md`, section 4.15) changed these things:
  - a check lists every refused line it finds, at most 20, each kind of refusal before
    any repeat, and dry run, measure and submit do the same;
  - list, dict and set comprehensions are accepted as the loops they stand for, and a
    generator expression is accepted as the list argument of `any`, `all`, `sum`, `min`,
    `max`, `sorted`, `list` and `join`;
  - an `if __name__ == "__main__":` block is dropped at check time;
  - the documented `run` line and its annotated forms are accepted, and any other `run`
    line gets the exact line to write;
  - each import is refused with a message naming what replaces it;
  - the relay's limiter allows 90% of the published per-minute limits, and a settled call
    counts only its uncached prompt tokens plus its completion tokens.
- From `hackathon/nebius` the branch now also has `aa90985`, so a reply cut at the output
  cap runs none of its tool calls. No reply was cut in these two sessions.
- Workspaces `C:/t/g3-u3` (U3) and `C:/t/g3-s3` (S3), fresh, each with its own signing
  key. The floor files and the spec are byte for byte the retry's, and U3's profile is
  byte for byte R1's. S3's profile differs only in the engineer entry.
  - Each workspace's credentials file holds the relay's `evidence` access code, every
    model entry's `base_url` is the relay, and no Token Factory key was in Architect's
    environment.
  - The play model, Stage 2, the compile budget and the Forge settings are the retry's:
    Lightning at temperature 0 with thinking off and `max_tokens` 512; 4 preset
    configurations; $0.25 and 1,200 s plus the Forge cap; 24 measures, 6 submissions, 0
    spawns; agent replies capped at 12,288 tokens; a 60,000-character context cap; Pi's
    limit of 200 turns.
- Same command as the retry, with `--audit C:/t/g3-audit-<name>`, under the retry's
  driver with two additions that only observe: it keeps each check's list of refused
  lines (line, code, rule and the fixed message), and it records each HTTP answer's
  status, `retry-after` and the relay's `x-relay-wait-ms`. Its hard stop, $1.90 a session,
  never fired. Each version of `harness.py` was copied as it changed, outside the
  repository, for the offline re-check below.
- Forge pack hash with these settings (cap $1.75, 25 minutes), as both sessions recorded
  it: `1a5c41b72cad8646c5f614f34a715c24527adeff2e3762a8b3b096e3f7d824eb`. The engineer
  model is not part of the hash, so U3 and S3 share it. `architect forge pack` with the
  same flags gives the same hash, and its task-text check finds nothing (the spec,
  `task.json` and `RULES.md` as sources).
- Relay: the local host from G0, this time started from this branch's `deploy/relay`
  (compiled from `d02b75f`), with the G0 launcher's settings: a file ledger outside the
  repository and the prices of 2026-10-03. It was up from 01:57:48 UTC until after S3
  ended at 02:40:42, then stopped.
  - For these runs the `evidence` code's total cap was set to its earlier spend plus
    $3.60 ($6.94049522), and its share of each model's per-minute budget to 100%. Both
    went back to $12 and 25% after the runs.
- Before the sessions, one plain and one streamed Lightning call through the relay
  checked the path, for $0.00000372.

| | U3 | S3 |
|---|---|---|
| Workspace | `C:/t/g3-u3` | `C:/t/g3-s3` |
| Engineer | `nvidia/Nemotron-3-Ultra-550b-a55b`: thinking on, temperature 1.0, top_p 0.95 | `nvidia/nemotron-3-super-120b-a12b`: thinking off, temperature 0 |
| Forge cap | $1.75 and 25 minutes | the same |
| Run (UTC) | 01:58:12 to 02:07:19 | 02:11:36 to 02:40:42 |

G1's S1 also had Super with thinking off, but with no temperature set and a $1.00 cap.

## Results

| | U3 | S3 |
|---|---|---|
| Wall time: whole compile / Forge session | 547 s / 293 s | 1,746 s / 1,500 s |
| Session ended by | its $1.75 cap | its 25-minute limit, with $0.52 of the cap left |
| Agent turns (model calls) | 68 | 165; the last was cancelled at the time limit before any reply |
| Replies whose tool calls all parsed | 68 of 68 | 164 of 164 |
| Tool calls / malformed | 85 / 0 | 164 / 0 |
| Re-asks (bad arguments, cut replies, early finals) | 0 | 0 |
| Replies cut at `max_tokens` | 0 | 0 |
| `REASONING_CUT` | 0 | 0 |
| `forge_check` calls that passed | 2 of 12, the first at turn 41 | 6 of 15, the first at turn 28 |
| Refused lines in the refused checks: all / median / most | 163 / 9.5 / 47, in 10 checks | 44 / 4 / 15, in 9 checks |
| Dry runs | 2, both passed | 8 asked: 6 passed, 1 refused because `run` was not last, 1 refused by Pi's argument check |
| Measures on practice receipts | 0/5 and 0/5 | 0/5, 1/5, 1/3 and 1/3 |
| Submissions / valid v2 code | 0 | 0 |
| Stage 2 best preset on practice | 12/50 (0.24) | 13/50 (0.26) |
| `forge_rank` against the Stage 2 best | nothing was submitted to rank, so Forge's best stays the Stage 2 pick | the same |
| Accepted submissions | 0 | 0 |
| Stage 4, from the signed report | the Stage 2 preset, refused: exam 17/50 (0.34) against 0.40, 0 invalid | the Stage 2 preset, refused: exam 16/50 (0.32) against 0.40, 0 invalid |
| Agent $ | $1.6463 | $1.2339 in Architect's turn rows; $1.1889 at the relay |
| Lightning $: Stage 2 / Forge / Stage 4 | $0.0293 / $0.0009 / $0.0030 | $0.0293 / $0.0003 / $0.0030 |
| Build total | $1.6795 | $1.2664 in Architect's rows; $1.2215 at the relay |
| Agent $ per turn | $0.0242 | $0.0075; $0.0072 per reply settled at the relay |
| Prompt tokens per reply, mean / max | 21,110 / 28,444 | 23,175 / 36,812 |
| Cache-hit share of prompt tokens | 66.7% | 0% |
| Reasoning share of completion tokens | 20.6% | 0 (thinking off) |
| Largest reply, in tokens | 6,821 | 6,809 |
| Seconds per turn, mean, waits included | 3.9 | 9.0 |
| Turns a minute | 14 | 6.6 |
| Relay 429s waited out / seconds waited | 0 / 0 | 16 / 189 |
| Calls the relay held before admitting them / seconds held | 1 / 3.5 | 128 / 820 |

Tool calls by kind, with refusals in brackets:

| | U3 | S3 |
|---|---|---|
| read | 53 (1) | 70 (7) |
| write | 11 | 5 |
| edit | 5 (2) | 62 (23) |
| forge_check | 12 | 15 |
| forge_dryrun | 2 | 8 (1) |
| forge_measure | 2 | 4 |
| forge_submit | 0 | 0 |

- U3 mostly wrote whole files (11 writes; its versions ran from 268 to 467 lines) and
  read `harness.py` 21 times. S3 worked through edits: 23 of its 62 were refused (15
  named text that was not in the file, 6 changed nothing, 2 matched more than once), and
  it read `harness.py` 58 times.
- U3 read LANGUAGE.md once; S3 never did. The card rides in the system prompt, now 11,094
  characters.
- S3's 7 refused reads: 2 denied by Architect's policy, 3 for a tuning file or folder that
  does not exist, and 2 for an offset past the end of the file.
- The records agree. Architect's per-turn rows in `telemetry/forge_turns/` (68 and 165
  rows) give the same agent dollars as the driver, and the relay ledger gives U3's build
  total to the micro-dollar. S3's last call, cancelled by the time limit, never got a
  reservation at the relay, so it never reached Token Factory and the relay charged
  nothing for it. Architect's turn row books it at its quoted worst case, $0.0450, marked
  as an estimate. That is the whole difference between the two S3 totals.
- Stage 2 picked the plain extractor configuration (no retry, no verifier) in both builds.
  It scored 12/50 on practice and 17/50 on the exam in U3, and 13/50 and 16/50 in S3. The
  same configuration's counts differ by one in each half between the two builds, so
  Lightning at temperature 0 does not repeat exactly here.
- Parsing U3's drafts printed 40 Python `SyntaxWarning` lines (an invalid escape
  sequence in a string) to the compile's stderr. The check could parse with warnings
  silenced.

## Refusals by rule

Each pack v3 check lists every refused line (at most 20 shown), so this table counts each
rule three ways: the checks where it came first, the checks that listed it, and the
refused lines those checks showed.

| Code / rule | U3: first / checks / lines | S3: first / checks / lines |
|---|---|---|
| Passed | 2 | 6 |
| `sandbox_import` / `import_module` | 4 / 4 / 9 | 0 |
| `sandbox_expression` / `module_use` | 0 / 3 / 18 | 0 |
| `sandbox_expression` / `method_unknown` | 2 / 9 / 52 | 2 / 5 / 10 |
| `sandbox_call` / `method_arity` | 3 / 5 / 6 | 3 / 5 / 8 |
| `sandbox_call` / `builtin_arity` | 1 / 4 / 5 | 1 / 2 / 2 |
| `sandbox_assignment` / `for_target` | 0 / 3 / 4 | 0 |
| `sandbox_entrypoint` / `run_last` | 0 / 2 / 2 | 0 (one dry run) |
| `sandbox_helper` / `helper_builtin_name` | 0 / 2 / 2 | 0 |
| `sandbox_signature` / `run_signature` | 0 / 1 / 1 | 0 |
| `sandbox_statement` / `raise` | 0 / 1 / 1 | 0 |
| `sandbox_capability` / `await_target` | 0 / 1 / 1 | 0 |
| `sandbox_expression` / `context_value` | 0 / 1 / 1 | 0 |
| `sandbox_statement` / `except_shape` | 0 | 1 / 3 / 17 |
| `sandbox_call` / `missing_function` | 0 | 1 / 2 / 6 |
| `sandbox_invalid_source` / `syntax_indent` | 0 | 1 / 1 / 1 |
| Checks in all | 12 | 15 |
| Refused lines in all (shown) | 163 (102) | 44 (44) |

Every version that each session wrote, run offline through the same validator at no cost:

| | U3 | S3 |
|---|---|---|
| Versions of `harness.py` written | 14 | 43 |
| Pass as written | 2 | 5 |
| Refused lines: all / median per refused version / most | 201 / 9.5 / 47 | 181 / 4 / 15 |

- U3's largest groups: methods the language lacks (81 lines: `isdigit` 23, `lstrip` 17,
  `isalpha` 11, `islower` 10, `rstrip` 6, `rfind` 3, and 11 on regex patterns and
  matches); names bound by refused imports (49: `re` 23, `json` 20, `architect` 6); for
  targets (18); `zip` with more than two lists (15); `split` with no separator (10); and
  the imports themselves (9).
- S3's largest groups: `except` with a type or a name (75 lines in 15 versions), `isdigit`
  (30 lines in 20 versions), `split` with no separator (25 in 17), `chr` and `ord` (12
  each, in 8), `.copy()` (11) and `max(a, b)` (6).
- The card already covers all of these and says what to write instead, for example
  `max([a, b])`, `zip(l1, l2)`, `split(s, sep)`, no `except` with a type or a name, and
  no `chr` or `ord`. The drafts use them anyway.

## G1a and G1b against the plan's criteria

| Criterion | U3 | S3 | Verdict |
|---|---|---|---|
| The session finishes | yes, at its cap | yes, at its 25-minute limit | pass |
| At least 95% of turns parse | 100%: 68 of 68 | 100%: 164 of 164 replies; the 165th call was cancelled before it answered | pass |
| Submissions are valid v2 code | none submitted | none submitted | **fail** |
| No `REASONING_CUT` | 0 | 0 | pass |
| $ per turn is logged | 68 rows | 165 rows, one at its estimate | pass |
| Each Ultra session costs at most $2 and takes at most 25 min | $1.68; 9.1 min, Forge 4.9 min | written for Ultra; S3 cost $1.27 and took 29.1 min, Forge 25.0 min | U3 pass |
| G1b: an Ultra session ends with an accepted submission | no | Super: no | **fail**: 0 of 5 Ultra sessions, and no Super session either |

- **U3:** G1a fails on one criterion, no submission; every other G1a criterion passes.
  G1b fails.
- **S3:** G1a fails on the same criterion. If the cost and time line applied to Super,
  the cost would pass and the whole compile (29.1 minutes) would not. The F2 question, a
  Super session ending with an accepted submission, fails: 0 of 2 Super sessions with S1.

## What changed since the retry

| | R1 | R2 | U3 | G1 S1 | S3 |
|---|---|---|---|---|---|
| Engineer | Ultra | Ultra | Ultra | Super | Super |
| Forge pack | v2 | v2 | v3 | G1's, before v2 | v3 |
| Path to Token Factory | relay | relay | relay, new limiter | direct | relay, new limiter |
| Forge cap | $1.75 | $1.75 | $1.75 | $1.00 | $1.75 |
| `forge_check` calls that passed | 2 of 10 | 0 of 14 | 2 of 12 | 7 of 30 | 6 of 15 |
| Turn of the first passing check | 43 | none | 41 | 63 | 28 |
| Measures | 3 | 0 | 2 | 8 | 4 |
| Submissions | 0 | 0 | 0 | 2 | 0 |
| Agent turns | 71 | 70 | 68 | 123 | 165 |
| Agent $ per turn | $0.0230 | $0.0235 | $0.0242 | $0.0077 | $0.0075 |
| Cache-hit share of prompt tokens | 75.6% | 76.9% | 66.7% | 0% | 0% |
| Turns a minute | 3.5 | 3.4 | 14 | 11 | 6.6 |
| Relay 429s | 55 | 55 | 0 | none, direct | 16 |
| Forge session wall time | 1,219 s | 1,251 s | 293 s | 669 s | 1,500 s |
| Session ended by | its cap | its cap | its cap | its cap | the 25-minute limit |

## What went wrong

1. **Ultra still spends most of its money on the language.**
   - U3's first draft was a 441-line Python module that imported `json`, `re` and
     `typing` and used string methods the language lacks. Pack v3's first check named all
     6 kinds of refusal in it and showed 20 of its 47 refused lines. U3 cleared them in 8
     more checks, by turn 41, which took $0.95 of its $1.75.
   - The version that passed did not call the preset. It parsed the receipt with its own
     line rules and keyword lists and made three Lightning calls a receipt. U3 measured
     it twice, and both measures scored 0 of 5.
   - U3 then rebuilt on the preset call with code around it (268 lines). Three versions of
     that were refused for `split` with no separator, a helper named `extract_json` and an
     unknown method, and the money ran out at turn 68.
   - At $0.024 a turn, $1.75 buys about 70 Ultra turns. U3 needed 41 of them to reach a
     passing check, and R1 needed 43 on pack v2.
2. **Super gets through the checks but has not found a better harness.**
   - S3's drafts carried fewer refusals (a median of 4 lines a version against U3's 9.5),
     and it passed a check at turn 28, having spent $0.13.
   - Its first measure ran a harness that made no model call and scored 0 of 5. Later
     versions scored 1 of 5 and 1 of 3. DESIGN.md asks for a measure above the preset and
     at or above the 0.40 floor before a submission, and no measure got there, so S3
     never submitted. It also never measured more than 5 receipts.
   - `except` with a type or a name was its most common refusal (75 lines in 15
     versions), although the card names it.
3. **The relay's Super budget paced S3.**
   - The limiter now counts a settled call's uncached prompt tokens and its completion
     tokens, at 90% of the published limits. The cache served 67% of U3's prompt tokens,
     so U3 never got a 429 and ran 14 turns a minute.
   - The cache served none of S3's 3.8 million prompt tokens, so each Super turn counts
     in full, about 23,500 tokens against a budget of 180,000 a minute. A running call
     also holds its 12,288-token output cap. The relay held 128 of the 164 calls for 820 s
     before admitting them and answered 16 requests with a 429 (189 s of client waits):
     about 1,009 s of S3's 1,500 s.
   - Without the waits, S3's turns took about 8 minutes of model time. G1's S1 called
     Token Factory directly at 11 turns a minute and got no 429.

## Recommendation

For the evidence engineer: **Super (F2)**, with two changes before the freeze, and with
F3 kept as the stated fallback.

- **Why not Ultra.** Five Ultra sessions have ended without a submission, and the money
  ends them. $1.75 buys about 70 turns, and on pack v2 and v3 alike Ultra needed about 40
  to reach a passing check (R1 at turn 43, U3 at turn 41). The plan's ceiling of $2.50 a
  session adds about 30 turns.
- **Why not F1 (Ultra with thinking off).** It would not buy turns. Prompt tokens are 87%
  of an Ultra turn's cost and reasoning tokens 2.6%, so thinking off saves about 3% a
  turn. Nothing in U3 points at thinking: no reply was cut, and the money went to
  language refusals and a harness that scored 0. The plan sends mechanics misses to F1,
  and the mechanics passed.
- **Why Super.** It costs a third as much per turn ($0.0075 against $0.0242), its drafts
  carry fewer refusals, and it reached a passing check at turn 28 for $0.13 against
  Ultra's $0.95 at turn 41. In the pilot's plain-prompt arms it matched Ultra on the
  receipts themselves (0.500 against 0.493 on 150), and G1's S1 is still the only session
  that has submitted anything.
- **Before the freeze:**
  1. Give a Super build room to run. Either raise the relay's per-minute token budget for
     Super, since it counts every prompt token, or set the Forge minutes for a Super build
     from about 9 s a turn. At S3's pace, 25 minutes is about 165 turns, while its $1.75
     cap pays for about 230 and Pi stops at 200.
  2. One more task-neutral language pass on what the drafts still hit most: `except` with
     a type or a name, `isdigit` and the other string tests, `split` with no separator,
     `chr` and `ord`, `.copy()`, `max(a, b)` and `zip` with more than two lists. The card
     already covers these, so accepting them, or mapping them to the builtins, would do
     more than more text. Pin the pack hash again afterwards.
- No session has yet written a harness that beats the preset on practice, so F2 can fail
  as well. The pre-registration should name F2, keep F3 (A7 and A7b drop, H1 to H3 test
  A5, outcome O5) as the fallback, and report these session counts.

## Spend

| Item | Calls | Ultra | Super | Lightning | Total |
|---|---|---|---|---|---|
| Path check before the sessions | 2 | | | $0.00000372 | $0.00000372 |
| U3 | 648 | $1.646256 | | $0.0332277 | $1.6794837 |
| S3 | 725 | | $1.1888823 | $0.03258066 | $1.22146296 |
| **Total** | **1,375** | **$1.646256** | **$1.1888823** | **$0.06581208** | **$2.90095038** |

- From the relay ledger. The `evidence` code's spend went from $3.34049522 to $6.2414456.
  All 1,375 calls returned 200 and were settled from the usage Token Factory reported.
  None was booked at its full hold, released, or refused for money. The largest hold for
  one call was $0.189 on an Ultra turn and $0.065 on a Super turn.
- Prices are the Token Factory list prices of 2026-10-03, per million tokens: Ultra $1.00
  in and $3.00 out, Super $0.30 and $0.90, Lightning $0.06 and $0.24. Cached tokens count
  at the full input price.
- All of these calls went through the relay, so its ledger already holds them. Its global
  total is now $6.81389672 of its $26 cap.
