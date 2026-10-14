# Receipt reader results

These are the results of the evaluation plan in `docs/bench/prereg-nebius.md`, run on 2026-10-05. The plan was committed and published as a public Gist (https://gist.github.com/echoo19/217554e19b2d06d9c583f96732469dd6) before the first model call, and the run followed it without changes. On 2026-10-06 a second ladder build ran, with two rule checks added to the spec, and the same spec was built three more times in the desktop app, twice that day and once on 2026-10-07. The plan records that change under "Changes after publication", with predictions and stop rules written before the change's first model call. On 2026-10-12 a private experiment had the plain models answer the 100 exam receipts again and scored other checks on those replies offline; none of its numbers is on this page, and the plan lists it under "Changes after publication".

The task is to read the text of a receipt and return total, subtotal, tax and item count. A receipt counts as right only when all four values match the labels exactly.

## Plain models on 348 unseen receipts

This table is the models alone: each one read the receipts with the plain prompt, and no rule check ran on its replies.

| Model | Accuracy on 348 unseen receipts, Wilson 95% interval | Invalid outputs | Cost per 1,000 receipts | p50 latency |
|---|---|---|---|---|
| Nemotron 3 Ultra | 0.529 [0.476, 0.581], 184 right | 0 | $0.92 | 0.75 s |
| Nemotron 3 Super | 0.517 [0.465, 0.569], 180 right | 0 | $0.28 | 0.72 s |
| Nemotron 3.5 Lightning | 0.270 [0.226, 0.319], 94 right | 0 | $0.058 | 0.47 s |

```
Prices: Nebius Token Factory list prices of 2026-10-03.
No build step saw the delivery receipts.
Regions: Lightning is served from eu-north1, Super and Ultra from us-central1, and latency depends on the region.
The models may have seen CORD v2 in pretraining.
The input is text rebuilt from CORD's word annotations. No OCR ran.
Every model ran at temperature 0 with thinking off. NVIDIA recommends 1.0 for these models.
```

Super got 4 fewer receipts right than Ultra, and their intervals overlap for most of their width. The plan runs no significance test, so the table does not say which of the two is more accurate. Super's cost per receipt is 30% of Ultra's. Lightning costs about a sixteenth of what Ultra costs per receipt and gets about half as many receipts right.

Cost is the price of the tokens Token Factory reported for the 348 receipts, divided by 348. Latency is the median time per receipt as the bench measured it, relay included. Before reading the delivery receipts, each model's bench cell built on the evidence floor and sat its 100-receipt exam. The exam licensed Ultra (49 of 100 right) and Super (51 of 100) and refused Lightning (26 of 100).

## The ladder build, 2026-10-05

One build on the evidence floor (100 practice receipts, 100 exam receipts, bar 0.40) ran the ladder Lightning, Super, Ultra, with Forge on for the Lightning rung, and every rung sat the exam. It ran from 10:11 to 10:51 UTC.

| Rung | What sat the exam | Practice score | Exam, right of 100 | Invalid | Exam result |
|---|---|---|---|---|---|
| Lightning | the plain prompt, Stage 2's pick of 4 preset configurations | 0.32 | 27 | 0 | refused |
| Super | the plain prompt | 0.55 | 51 | 0 | licensed, ships |
| Ultra | the plain prompt | 0.58 | 49 | 0 | licensed |

The exam refused Lightning and licensed Super and Ultra. Super ships because it is the cheaper of the two licensed models. The exam counts come from each rung's signed report.

The agent the build installed has the id `ff985dd9d7cdea94938fabb5fc6ec909c28996cd6e7c693af60b2778cf6ebfdc` (its registry name, which is the spec hash), revision 1. It is Super with the extractor preset's plain configuration, and its artifact digest is `f4c57fabdfdaf49c84c83d1d8d6a8b373e0aa6ec1dd88e2acfe8203246c43d6e`.

The build made 1,813 model calls and cost $1.46845512: $1.16049492 for the Forge session and $0.3079602 for everything else. Its reports are signed with the build host's HMAC key. They carry no Ed25519 seal, because the release key did not exist yet when the build ran. Copies of the three exam reports, the plan and the bench rows are in `architect/published/receipts`, signed with the release key, and `architect verify --published receipts` checks them.

## The ladder build with the rule checks, 2026-10-06

This build's spec, `examples/bench/eval-2026-10-06/ladder/spec.json`, is the 2026-10-05 spec plus two rule checks. Each check is code that applies a rule the prompt already states, and both run on every reply before it is scored:

- Amounts keep every printed digit, so "226,500.00" is 22650000.
- A subtotal equal to the total is null unless the receipt prints that amount as a subtotal.

The lines that count as printing a subtotal are the ones the subtotal rule names in `examples/receipts/RULES.md`. The checks are part of the spec, so the spec hash changed, and each rung's exam tested the model and its checks together.

The build ran the ladder again on the same 100 practice and 100 exam receipts and the same bar of 0.40, with Forge off, from 10:14 to 10:32 UTC at commit `1074a80`. Each rung sat the exam once, and nothing was rerun.

| Rung | Practice score | Exam, right of 100 | Exam result | Same exam without the checks, 2026-10-05 | Gain | Exam cost per 1,000 receipts |
|---|---|---|---|---|---|---|
| Lightning | 0.48 | 39 | refused | 27 | +12 | $0.058 |
| Super | 0.68 | 65 | licensed, ships | 51 | +14 | $0.28 |
| Ultra | 0.78 | 71 | licensed | 49 | +22 | $0.93 |

The exam refused Lightning, one receipt short of the bar, and licensed Super and Ultra. Super ships because it is the cheaper of the two. It got 14 more exam receipts right than without the checks, and the plan keeps the checks only if the rung that ships gains at least 8, so they stay. Every count is inside the range the plan predicted: about 41 for Lightning (36 to 47) and about 68 for Super and Ultra (62 to 74). No rung gave an invalid output. Lightning's practice score is Stage 2's pick of 4 preset configurations, and Super and Ultra ran their plain configuration.

The checks run in code and make no model calls, and each rung's exam cost the same per 1,000 receipts as on 2026-10-05, to the cent. The build made 1,500 model calls and cost $0.30784026 on the relay's ledger.

The agent the build installed has the id `4d80f3d580cac16d3b384eb7f8ca7d84990bd48b8fca0482cca4afb00e0e48cb` (its registry name, which is the spec hash), revision 1. It is Super with the extractor preset's plain configuration and the two checks, and its artifact digest is `c6269719e8f5730f5f1717bd26197a3745b71259b61e3e0cac202704842fdc69`.

The exam counts come from each rung's signed report. Stage 4 signed them with the build host's HMAC key and sealed them with the release key, so `architect verify` checks each one against the public key that ships with Architect. The practice scores come from the build's own log, which is not signed. The spec, the profile, the three reports and a summary of the build (`outcome.json`) are in `examples/bench/eval-2026-10-06/ladder/`, and the check functions as they were frozen before the first call are in `docs/bench/eval-2026-10-06/frozen_checks.py`. Copies of all of these are in `architect/published/receipts/ladder-2026-10-06/`, signed with the release key.

The checks did not run on the 348 delivery receipts.

## Four builds of the checked design

Four builds of the checked spec ran to the end, each in a new workspace, and each build's rungs sat the exam once. The 10:14 build is the one above. The other three ran in the desktop app while I was making an earlier cut of the video, which is no longer used: the 14:27 build called the models through the relay, the 20:20 build called Token Factory directly because the relay was down, and the 09:19 build on 2026-10-07 called Token Factory directly too. The picture in the README shows the 09:19 build. The video is a reconstruction from recorded runs, and the receipt build in it is a different one, on the demo exam of development receipts described in `docs/bench/retake-trial.md` (36, 59 and 64 against a pass mark of 40). Those aren't published results. Times are UTC.

| Build | Lightning | Super | Ultra | Shipped |
|---|---|---|---|---|
| 2026-10-06 10:14 | 39, refused | 65 | 71 | Super |
| 2026-10-06 14:27 | 39, refused | 63 | 70 | Super |
| 2026-10-06 20:20 | 42, licensed | 65 | 72 | Lightning |
| 2026-10-07 09:19 | 39, refused | 64 | 72 | Super |

All four builds made the same three agents, with the same artifact digests, and examined them on the same 100 receipts. Lightning was never more than two receipts from the bar (40 of 100), and it was licensed in one build of four. Super and Ultra were well above the bar every time, at 63 to 65 and 70 to 72. So which rung ships depends on how Lightning does on the exam: Super shipped three times and Lightning once.

The three exam reports and a summary (`outcome.json`) of each of those three builds are in `examples/bench/eval-2026-10-06/video-1/`, `video-2/` and `video-3/`, with signed copies in `architect/published/receipts/`. One more build of the spec started at 16:01 on 2026-10-06 and was stopped by hand during Lightning's exam, after the relay began refusing every call when its database hit its request limit. It scored no exam and wrote no report.

## Gate 1 on the rehearsal receipts, 2026-10-06

Before the 10:14 build, the plan's Gate 1 (under "Changes after publication") tried the two checks on kept replies. Plain Lightning and plain Super read the 100 rehearsal receipts once each, from 07:23 to 07:27 UTC through the relay, with the spec of the 2026-10-05 plain arms A3 and A2 (the run compared spec hashes before its first call). Every reply was kept, and the frozen check functions in `docs/bench/eval-2026-10-06/frozen_checks.py` then ran on the replies offline: `guard_digits`, the digits check, and `guard_not_total_copy`, the subtotal check. Run on the 100 rehearsal labels as if they were replies, the checks change nothing. The plan keeps the rehearsal receipts for development, so unlike the exam's receipts they are not held out.

The gate ran both checks in the order the receipt reader's spec declares, the digits check first, and the work went on only if each model gained at least 8. The other rows run on the same replies: each check alone, and both in the reverse order. Right of 100:

| Checks run | Lightning | Super |
|---|---|---|
| None | 29 | 50 |
| Subtotal check alone | 37 (+8) | 60 (+10) |
| Digits check alone | 31 (+2) | 52 (+2) |
| Both, digits check first (the declared order, and the gate) | 41 (+12) | 64 (+14) |
| Both, subtotal check first | 40 (+11) | 63 (+13) |

No check or order turned a right receipt wrong. Both models gained at least 8 in the declared order, within the predicted ranges (10 to 20 for Lightning, 12 to 25 for Super), so the work went on.

Alone, the subtotal check set 22 of Lightning's subtotals and 15 of Super's to null, each one equal to the total in the same reply. The label is null on all of them but one, so 21 and 14 subtotals went from wrong to right. A receipt counts only when all four fields are right, so the gain in right receipts is smaller, 8 and 10.

That one, cord-train-0447, is also the receipt the reverse order loses for both models. It prints "Subtotal: Rp 29000.00" and "Total: Rp 29000.00", which the rules read as 2900000 each, and both models answered 29000 for both fields. Run first, the subtotal check sees a subtotal equal to the total, finds no 29000 on the subtotal line, whose amount reads as 2900000, and sets the subtotal to null. The digits check then repairs only the total. Run second, after the digits check has made both values 2900000, the subtotal check finds that amount on the subtotal line and keeps it.

These counts come from Gate 1's own scoring of the kept replies, which is not signed. Every reply is in `examples/bench/eval-2026-10-06/gate1/`, with a note on how to recount. Gate 1 made 200 model calls and cost $0.03397872 on the relay's ledger.

## Forge

In the 2026-10-05 build, Forge did not find a better harness for Lightning. With Super as the engineer it ran for 27 minutes and 149 turns and spent $1.16 of its $1.20 cap. It submitted two harnesses, which got 31 and 10 of the 100 practice receipts right. Both were under the 0.40 bar, so Forge accepted neither, and the plain prompt Stage 2 had picked sat the exam instead. Development went the same way: 11 Forge sessions with a Nemotron engineer ended with no accepted harness. For now, Forge with a Nemotron engineer is a known limitation, and in that build it cost almost four times as much as the rest of the build.

## How it ran on 2026-10-05

- The plain models ran one after another from 09:43 to 10:11 UTC at commit `91048c0`, with the commands in the plan. The ladder build ran after them.
- Every call went through the project's spend relay under one access code. Its ledger shows 3,757 calls, all answered with status 200, for $2.2878319 at list prices: $0.81937678 for the plain models and $1.46845512 for the ladder build.
- Nothing was rerun.
- The bench rows of the plain models are in `examples/bench/eval-2026-10-05/`: `A1` is Ultra, `A2` Super and `A3` Lightning. The ladder build's spec, profile and a summary of its signed reports (`outcome.json`) are in `examples/bench/eval-2026-10-05/ladder/`. None of these files holds receipt text.
- `architect bench report examples/bench/eval-2026-10-05/A1 examples/bench/eval-2026-10-05/A2 examples/bench/eval-2026-10-05/A3 --md` rebuilds the first table from the rows.
