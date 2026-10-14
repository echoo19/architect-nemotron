# Last Forge round on pack v4, 2026-10-05

Three more live Forge builds on the CORD receipts rehearsal floor, all with Super as the
engineer, on Forge pack v4: s1, s2 and s3. The setup is the bake-off's super-40
(`docs/feedback/probes/2026-10-05-forge-engineer-bakeoff.md`) with one change: Pi's turn
limit is 400, so it cannot end a session before its 40 minutes. The sessions ran one after
another, and every call went through the local spend relay. This was the last Forge round.

Development only. The rehearsal floor is for development, every number below is a
development number, and nothing here is evidence or may back a claim.

## Decision

The rule was fixed before the first session started: if at least one session ends with an
accepted submission, the evidence engineer is Super (F2); if none does, the O5 framing
applies.

**None of the three sessions ended with an accepted submission, so O5 applies.**

- The framing is O5's: the exam refuses Lightning and licenses the cheapest Nemotron that
  passes it. Whether the exam does refuse Lightning, and which model it licenses, come from
  the evidence run, not from this note.
- Forge on Nemotron is a disclosed limitation, with these session counts: 0 accepted
  submissions in 11 Forge sessions with a Nemotron engineer, 5 with Ultra (G1's U1 and U2,
  the retry's R1 and R2, retry 2's U3) and 6 with Super (G1's S1, retry 2's S3, the
  bake-off's super-40, and s1, s2 and s3 here). Super made 21 submissions across its six
  sessions and Ultra none. With the bake-off's DeepSeek and Kimi sessions, it is 0 in 13.
- In the pre-registration's terms the engineer branch is F3: A7 and A7b drop, H1a, H2 and
  H3 test A5 in A7's place, H1b drops, and the outcome is O5. There is no Forge-built arm
  in the evidence run, and there are no more Forge rounds.

## Summary

- **No accepted submission.** Super submitted 18 times, 6 in each session. 17 of the
  submissions ran on all 50 practice receipts and scored 0.00 to 0.30, best 15 of 50 (in s1
  and s3). Forge accepts a submission only if it clears the 0.40 floor on practice and the
  practice smoke check and then ranks above the best so far. Each one missed the floor, so
  none got as far as the ranking, and the Stage 2 pick stayed Forge's best in every build.
- **Pack v4 did what it was for.**
  - In s3 Super sent the context cap's note as the whole of `harness.py` 5 times. Pi
    refused each write, the file kept its text, and Super read the file and wrote a real
    version. No kept copy of `harness.py` from this round holds a note. In super-40 the
    same mistake replaced the file and cost the last 62 turns.
  - Language refusals were few: a refused check listed 1.5, 1 and 2 lines at the median in
    s1, s2 and s3. The first check on the engineer's own code passed at turn 21, 12 and 16,
    against 36 in super-40 and 28 in S3.
- **The sixth submission ended each session**, at 22, 31 and 16 minutes, with $0.87, $0.39
  and $1.13 of the $1.75 Forge cap left. Pi's turn limit never came close (105, 166 and 84
  turns).
- **Super submitted on very small measures.** Most measures used 3 to 6 practice receipts.
  s3 measured receipts 0 to 2 before each of its six submissions and got 2 or 3 of the 3
  right each time; on all 50, the same harnesses scored 0.26 to 0.30.
- **Stage 4, from the signed reports:** in all three builds the Stage 2 preset went to the
  exam and was refused, 16 of 50 (0.32) against 0.40, with 0 invalid.
- **The mechanics pass.** All 355 replies parsed, none of the 355 tool calls was malformed,
  no reply was cut at the output cap, and there was no `REASONING_CUT`.
- Live spend: **$2.95306092** through the relay, against this round's cap of $5.40, from
  the relay ledger.

## Setup

- Code: `hackathon/forge-pack-v2` at `9cdc3fa`, which is pack v4 (`d6d47f9`, `a41fe88`,
  `7d5cbfb`) and its docs. Nothing in the repository changed for these runs.
- Workspaces `C:/t/b5-s1`, `C:/t/b5-s2` and `C:/t/b5-s3`, fresh, each with its own signing
  key.
  - The spec and the floor files are byte for byte super-40's.
  - Each profile is super-40's with one added setting, `pi_max_turns` 400 (the default is
    200). super-40 hit 200 turns at 35.5 minutes. At the relay's pace for Super, about 5 to
    7 turns a minute, 400 leaves the 40 minutes or the $1.75 cap to end a session.
  - Each workspace's credentials file held the relay's `evidence` access code, every model
    entry's `base_url` was the relay, and no Token Factory key was in Architect's
    environment. The three credentials files were deleted after the analysis.
- Everything else is super-40's:
  - the engineer: `nvidia/nemotron-3-super-120b-a12b`, thinking off, temperature 0, 300 s a
    call;
  - the play model: Lightning at temperature 0, thinking off, `max_tokens` 512;
  - Stage 2 with 4 preset configurations;
  - the compile budget of $0.25 and 1,200 s, plus the Forge cap of $1.75 and 40 minutes;
  - Forge in agent mode: 24 measures, 6 submissions, 0 spawns, agent replies capped at
    12,288 tokens (the relay clamps Super at 8,192), a 60,000-character context cap;
  - the command, with `--forge-usd 1.75 --forge-minutes 40` and
    `--audit C:/t/b5-audit-<name>`;
  - the driver, which only observes, with the same hard stop of $1.90 a session. It never
    fired.
- Forge pack hash, as all three sessions recorded it:
  `a747704c031381e979a9b681c4a3ec52eba1321b60a3a85a9feafc50f2b75563`. `architect forge pack`
  with the same profile and flags gives the same hash, and its task-text check finds
  nothing with the spec, `spec.json`, `task.json` and `RULES.md` as sources. It differs from
  the default pack hash (`05518529...`) only in the settings part: the cap and minutes,
  0 spawns and the turn limit.
- Relay: `start-relay-fpk2.sh`, unchanged, from this branch's `deploy/relay`, with the same
  ledger file as before. It ran once (PID 27832, from 07:08:07 UTC) and was stopped by its
  PID after s3. Port 8787 was free before and after.
  - For these runs the `evidence` code's total cap was set to its spend plus $5.40
    ($15.06268895), and its share of each model's per-minute budget to 100%. Both went back
    to $12 and 25% after s3, with the relay stopped.

| | s1 | s2 | s3 |
|---|---|---|---|
| Run (UTC) | 07:08:23 to 07:34:51 | 07:35:09 to 08:10:29 | 08:10:40 to 08:30:43 |

## Results

Development only.

| | s1 | s2 | s3 |
|---|---|---|---|
| Wall time: whole compile / Forge session | 1,588 s / 1,331 s | 2,120 s / 1,873 s | 1,203 s / 941 s |
| Session ended by | its sixth submission, with $0.87 and 17.8 minutes left | its sixth submission, with $0.39 and 8.8 minutes left | its sixth submission, with $1.13 and 24.3 minutes left |
| Agent turns (model calls) | 105 | 166 | 84 |
| Replies whose tool calls all parsed | 105 of 105 | 166 of 166 | 84 of 84 |
| Tool calls / malformed | 105 / 0 | 166 / 0 | 84 / 0 |
| Replies cut at the output cap / re-asks / `REASONING_CUT` | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 |
| `forge_check` calls that passed | 20 of 30 | 21 of 40 | 4 of 8 |
| First passing check on its own code | turn 21, 2.0 minutes in | turn 12, 0.8 minutes in | turn 16, 1.2 minutes in |
| Passing checks at turns | 10 (the unchanged starting file), 21, 27, 30, 34, 37, 41, 44, 53, 59, 62, 67, 74, 77, 84, 87, 90, 95, 99, 102 | 6 (the unchanged starting file), 12, 18, 28, 33, 39, 42, 48, 55, 58, 78, 81, 88, 96, 100, 106, 111, 116, 158, 161, 164 | 16, 21, 34, 65 |
| Writes refused for carrying a context note | 0 | 0 | 5 (turns 52, 58, 60, 62, 70) |
| Dry runs | 3 asked, all refused by Pi's argument check | 40, all passed | 10 asked: 7 passed, 2 refused (`sandbox_type`), 1 refused by Pi's argument check |
| Measures on practice receipts | 23: ten on 3 receipts (0 to 2 right), then 13 on 6 to 16 receipts (at best 3/6, four times; the largest 5/16) | 10, all 0/5 | 16 asked, 15 ran: 1/3 (twice), 2/3 (nine times), 3/3 (three times), 3/6; 1 refused (`binding_params`) |
| Submissions | 6 | 6 | 6 |
| Submissions' practice scores (of 50) | 13, 13, 14, 15, 13, 13 | 11, 14, 11, 10, refused before it ran (`binding_spec`), 0 (7 of 50 runs invalid) | 14, 13, 14, 15, 14, 14 |
| Stage 2 best preset on practice | 12/50 (0.24), the extractor with one retry | 14/50 (0.28), the plain extractor | 14/50 (0.28), the extractor with one retry |
| `forge_rank` against the Stage 2 best | none ranked: each submission missed the 0.40 floor on practice, so Forge's best stayed the Stage 2 pick | the same | the same |
| Accepted submissions | 0 | 0 | 0 |
| Stage 4, from the signed report | the Stage 2 preset, refused: exam 16/50 (0.32) against 0.40, 0 invalid | the same: 16/50 (0.32), 0 invalid | the same: 16/50 (0.32), 0 invalid |
| Agent $ | $0.8440302 | $1.3518063 | $0.5931378 |
| Agent $ per turn | $0.0080 | $0.0081 | $0.0071 |
| Lightning $: Stage 2 / Forge / Stage 4 | $0.0293 / $0.0347 / $0.0030 | $0.0293 / $0.0033 / $0.0030 | $0.0293 / $0.0292 / $0.0030 |
| Build total, from the relay ledger | $0.91107606 | $1.38736236 | $0.6546225 |
| Prompt tokens per reply, mean / max | 24,763 / 38,557 | 25,802 / 37,580 | 21,969 / 29,341 |
| Cache-hit share of prompt tokens | 0% | 0% | 0% |
| Largest reply, in tokens | 4,411 | 2,656 | 3,073 |
| Seconds per turn, mean, waits included | 12.2 | 10.9 | 10.6 |
| Turns a minute | 4.9 | 5.5 | 5.7 |
| Relay 429s waited out / seconds waited | 9 / 132 | 34 / 452 | 2 / 24 |
| Calls the relay held before admitting them / seconds held | 49 / 265 | 97 / 609 | 37 / 198 |

- Every Stage 4 report's signature checked, and none was licensed. The Stage 2 pick was
  the extractor preset in all three builds, with one retry in s1 and s3 and none in s2.
  Lightning at temperature 0 still does not repeat exactly: the same four configurations
  gave a best of 12/50 in s1 and 14/50 in s2 and s3.
- The records agree. Architect's per-turn rows in `telemetry/forge_turns/` (105, 166 and
  84 rows, none an estimate) give the same agent dollars as the driver, and the relay
  ledger's rows for each session give the same build totals, to the micro-dollar.
- Each passing check before a session's first write checked the unchanged starting file.

### The submissions against the Stage 2 pick

Submit measures a harness on all 50 practice receipts and on the practice smoke floor. It
refuses one that misses the 0.40 floor on practice (`tuning_failed`) or on the smoke floor
(`smoke_failed`) before it compares ranks, so none of these was compared with the Stage 2
pick. The last column places each one on `forge_rank`'s own order (floor, budget, smoke,
score, cost, size), computed offline with the product's function.

| Session, submission | Practice (of 50) | Smoke check | Verdict | On `forge_rank`, against the Stage 2 pick |
|---|---|---|---|---|
| s1, 1 to 4 | 13, 13, 14, 15 | missed | `tuning_failed` | below: the smoke miss ranks first |
| s1, 5 and 6 | 13, 13 | passed (0.44) | `tuning_failed` | above: 0.26 against 0.24 |
| s2, 1 to 4 | 11, 14, 11, 10 | missed | `tuning_failed` | below |
| s2, 5 | | | `forge_invalid_source` (`binding_spec`) | did not run |
| s2, 6 | 0 (7 of 50 runs invalid) | missed | `tuning_failed` | below |
| s3, 2 and 5 | 13, 14 | missed | `tuning_failed` | below |
| s3, 1, 3 and 6 | 14, 14, 14 | passed (0.40) | `tuning_failed` | below: the same score and cost, and a larger harness |
| s3, 4 | 15 | passed (0.40) | `tuning_failed` | above: 0.30 against 0.28 |

Tool calls by kind, with refusals in brackets:

| | s1 | s2 | s3 |
|---|---|---|---|
| read | 12 (2) | 26 (4) | 14 |
| write | 31 | 33 | 16 (5) |
| edit | 0 | 11 (2) | 14 (1) |
| forge_check | 30 | 40 | 8 |
| forge_dryrun | 3 (3) | 40 | 10 (1) |
| forge_measure | 23 | 10 | 16 |
| forge_submit | 6 | 6 | 6 |

- All 5 refused writes in s3 were 101-character writes of the context cap's note, refused
  by pack v4's rule. Refused reads named the working directory, `templates/`, a missing
  tuning file or an offset past the end of the file. Refused edits changed nothing.
- The context cap trimmed the history 13, 16 and 10 times (the reported prompt shrank
  between turns).

## Refusals by rule

Each check lists every refused line (at most 20 shown). Each rule is counted three ways:
the checks where it came first, the checks that listed it, and the refused lines those
checks showed.

| Code / rule | s1: first / checks / lines | s2: first / checks / lines | s3: first / checks / lines |
|---|---|---|---|
| Passed | 20 | 21 | 4 |
| `sandbox_binding_mismatch` / `binding_spec` | 1 / 1 / 1 | 6 / 8 / 8 | 1 / 1 / 1 |
| `sandbox_call` / `method_arity` | 1 / 1 / 2 | 4 / 4 / 4 | 0 |
| `sandbox_import` / `import_module` | 1 / 1 / 1 | 3 / 3 / 5 | 1 / 1 / 1 |
| `sandbox_expression` / `module_use` | 0 / 1 / 5 | 0 / 1 / 8 | 0 / 1 / 5 |
| `sandbox_assignment` / `for_target` | 0 | 0 | 2 / 3 / 8 |
| `sandbox_assignment` / `mutate_root` | 1 / 2 / 8 | 0 | 0 |
| `sandbox_entrypoint` / `run_last` | 0 | 2 / 3 / 3 | 0 |
| `sandbox_constant` / `constant_value` | 0 | 2 / 2 / 2 | 0 |
| `sandbox_literal_required` / `preset_literal` | 1 / 3 / 3 | 1 / 1 / 1 | 0 |
| `sandbox_call` / `missing_function` | 0 | 1 / 2 / 2 | 0 |
| `sandbox_helper` / `helper_builtin_name` | 0 | 0 / 2 / 3 | 0 |
| `sandbox_expression` / `method_unknown` | 1 / 1 / 3 | 0 | 0 / 1 / 1 |
| `sandbox_expression` / `lambda` | 0 / 1 / 3 | 0 | 0 / 1 / 1 |
| `sandbox_call` / `builtin_arity` | 1 / 1 / 2 | 0 | 0 |
| `sandbox_binding_mismatch` / `binding_preset` | 1 / 1 / 1 | 0 | 0 |
| `sandbox_binding_mismatch` / `binding_params` | 1 / 1 / 1 | 0 | 0 |
| `sandbox_assignment` / `assign_task` | 1 / 1 / 1 | 0 | 0 |
| Checks in all | 30 | 40 | 8 |
| Refused lines in all | 31, at most 11 in one check | 36, at most 14 | 17, at most 12 |

- The binding rules refuse a preset call whose preset name, parameters or spec differ from
  the harness's binding. From turn 127 on, s2 tried 8 times to change the spec argument
  of its preset call, and its fifth submission was refused before it ran for the same
  reason.
- `for_target` in s3 is a nested loop target, which pack v4 still refuses.

Every version that each session wrote, run offline through the same validator at no cost:

| | s1 | s2 | s3 |
|---|---|---|---|
| Versions of `harness.py` kept, the starting file included | 32 | 42 | 25 |
| Pass as written | 20 | 23 | 17 |
| Refused lines in all | 35 | 38 | 27 |
| Copies that hold a context-cap note | 0 | 0 | 0 |

## What happened

1. **The note fix held.** After the cap had trimmed its old writes, s3 sent the trim note
   as the whole file at turns 52, 58, 60, 62 and 70. Each write was refused, and each time
   the next turn read `harness.py`. After the fourth refusal it wrote a full version at turn
   64, which passed its check at turn 65, and after the fifth a full version at turn 72. It
   submitted four more times after the first refusal.
2. **Super now gets through the language and submits.** All three passed a check on their
   own code within the first 2 minutes, and all three used every submission. Before this
   round, the three Super sessions made 3 submissions between them.
3. **The measures were too small to tell it the harness was not ready.**
   - DESIGN.md asks for a measure above the preset and at or above the floor before a
     submission. Measured on 3 receipts, 2 right is already 0.67.
   - s3 measured receipts 0 to 2 before each submission, with 2 or 3 right each time. s1's
     largest measure was 16 receipts, 5 right (0.31). s2's ten measures were all 0 of 5,
     and it submitted six times anyway.
   - On all 50 receipts, every submission that ran scored 0.30 or less.
4. **What the harnesses did.**
   - s1's first submission read all four fields in code, then let a yes-or-no Lightning
     check swap them for the preset's answer. Its other five called the preset and changed
     at most the subtotal afterwards. The second was the Stage 2 preset call itself,
     unchanged: it scored 13 of 50, where Stage 2 had measured the same configuration at
     12.
   - s2's first, third and fourth read every field in code and called no model on any
     practice receipt; they scored 10 to 11 of 50. Its second and sixth asked Lightning with
     a short prompt of their own.
   - s3's six called the preset and corrected its answer with arithmetic checks in code.
     Some of those checks compute an amount from the other amounts, which the task's
     rules forbid.
   - So the round tried code reading the fields and the preset with code around it. Neither
     got near 0.40 on practice in the time each session used.
5. **The relay still paces Super.** The prompt cache served none of Super's prompt tokens,
   so every one counts against the relay's Super budget. The relay held 183 calls for 1,071
   s in all and answered 45 requests with a 429 (608 s of client waits). Super ran 4.9 to
   5.7 turns a minute.

## Super sessions so far

Development only. S1 is from G1, S3 from retry 2 and super-40 from the bake-off. For those
three, the first passing check is the one their notes give.

| | S1 | S3 | super-40 | s1 | s2 | s3 |
|---|---|---|---|---|---|---|
| Forge pack | G1's, before v2 | v3 | v3 | v4 | v4 | v4 |
| Forge cap, minutes | $1.00 | $1.75, 25 | $1.75, 40 | $1.75, 40 | $1.75, 40 | $1.75, 40 |
| Session ended by | its cap | its 25 minutes | Pi's 200 turns | its sixth submission | its sixth submission | its sixth submission |
| Agent turns | 123 | 165 | 200 | 105 | 166 | 84 |
| `forge_check` calls that passed | 7 of 30 | 6 of 15 | 5 of 26 | 20 of 30 | 21 of 40 | 4 of 8 |
| Turn of the first passing check on its own code | 63 | 28 | 36 | 21 | 12 | 16 |
| Measures | 8 | 4 | 1 | 23 | 10 | 16 |
| Submissions | 2 | 0 | 1, refused before it ran | 6 | 6 | 6 |
| Best practice score of a submission | 0.24 | | | 0.30 | 0.28 | 0.30 |
| Accepted | 0 | 0 | 0 | 0 | 0 | 0 |
| Agent $ per turn | $0.0077 | $0.0075 | $0.0083 | $0.0080 | $0.0081 | $0.0071 |

## Spend

| Item | Calls | Super | Lightning | Total |
|---|---|---|---|---|
| s1 | 1,317 | $0.8440302 | $0.06704586 | $0.91107606 |
| s2 | 871 | $1.3518063 | $0.03555606 | $1.38736236 |
| s3 | 1,132 | $0.5931378 | $0.0614847 | $0.6546225 |
| **Total** | **3,320** | **$2.7889743** | **$0.16408662** | **$2.95306092** |

- From the relay ledger. The `evidence` code's spend went from $9.66268895 to
  $12.61574987. All 3,320 calls returned 200 and were settled from the usage Token Factory
  reported. None was booked at its full hold, released, or refused for money. The largest
  hold for one call was $0.071241, on a Super turn in s2.
- The `evidence` code's total cap is back at $12, below its spend, so it refuses every call
  until the cap is raised.
- Prices are the Token Factory list prices of 2026-10-03, per million tokens: Super $0.30
  in and $0.90 out, Lightning $0.06 and $0.24. Cached tokens count at the full input price.
- All of these calls went through the relay, so its ledger already holds them. Its global
  total is now $13.18820099 of its $26 cap.
