# Evaluation plan: the receipt reader on Nemotron

Written on 2026-10-05 and committed before the run made its first model call. It replaces the longer pre-registration draft this file held before; git history keeps that draft, and the section numbers that `docs/bench/pilot-2026-10-04.md` cites refer to it.

Public copy: https://gist.github.com/echoo19/217554e19b2d06d9c583f96732469dd6, published on 2026-10-05 at 09:42 UTC, before the run's first call. The Gist holds this file as of commit `dd00f02` (sha256 `cbb03faaece62537f57b6319c6fcd35d569dc7d5da25a154432ef1be720175cb`). Only this line and "Changes after publication" below have changed since.

## What this plan measures

Architect builds an agent from a task description and licenses it only if it passes an exam it never saw. The test task is a receipt reader. It reads the text of a receipt and returns four values: total, subtotal, tax and item count.

The earlier draft planned to compare a harness written by Forge, Architect's harness engineer, against plain prompts. In development no Forge session with a Nemotron engineer produced a harness that cleared the bar. Over 11 sessions (5 with Nemotron 3 Ultra, 6 with Nemotron 3 Super) Forge accepted 0 submissions, and the best submission got 0.30 on the 50 practice receipts of the rehearsal floor, below the bar of 0.40 (`docs/feedback/probes/2026-10-05-forge-last-round.md`). So this plan drops the Forge arms and the hypothesis tests. It does two things:

1. It runs plain Nemotron 3 Ultra, Super and Lightning on the 348 delivery receipts and reports accuracy and cost.
2. It reports one ladder build on the evidence floor, where the exam decides which of the three models ships.

## Data

- Source: CORD v2, the Consolidated Receipt Dataset by NAVER Clova (`naver-clova-ix/cord-v2` at Hugging Face revision `7f0115a4b758a71d6473b8d085751692da2fef98`), shared under CC BY 4.0. Each receipt's text is rebuilt from CORD's word annotations. No image is used and no OCR runs.
- Labels come from CORD's annotation. An amount is the integer left after deleting every character that is not a digit, null means the receipt does not print the value, and `item_count` is the number of entries in CORD's `gt_parse.menu`.
- Rules: rule version `receipts-v2` (rules sha256 `b552ab6033626d0e5f1792a560932b764b749a7dc17f7f2cbe928653a8f52480`), in `examples/receipts/RULES.md`. Every prompt quotes them word for word.
- Scoring: the `receipt_v1` checker counts a receipt as right only when all four values match exactly. An invalid output counts as a miss.
- Exclusions: 202 of the 1,000 source receipts are left out before any set is drawn: 140 duplicate copies, 24 in duplicate groups whose copies disagree on a label, 15 wrong labels, 20 labels that follow a CORD habit the rules settle the other way, and 3 texts the rebuild garbles. `examples/receipts/manifests/excluded.json` lists each one with its reason.

| Set | Receipts | Use in this plan | `cases_sha256` |
|---|---|---|---|
| Pilot | 150 | set the bar on 2026-10-04; calibration only | `5e81a4caade703f852b46fc83b25aed8e535fba63624c96c8b8d7525e3c7b2bf` |
| Rehearsal | 100 (50 practice, 50 exam) | development and the Forge sessions | `7c3eff3f9fa44aa042003b0e8fb20eed03a898e202a508ee13a403080224bea8` |
| Evidence floor | 200 (100 practice, 100 exam) | every build in this plan | `09a2422b6a1fa87587c7919d0b0b779ecfc8230409fe48c89bafa46a50592218` |
| Delivery | 348 (166 train, 86 validation, 96 test) | the accuracy and cost table; no build step sees them | `78f6d3d7bf72ced6373ba1e51731c89c4586566436e3eddf62ac33238cecba2f` |

No two sets share a receipt or a duplicate group. Each set's manifest in `examples/receipts/manifests/` holds a sha256 for every case, and the bench checks every case against it before a run starts.

## Models and prices

| Name here | Token Factory model id | Region | Price per million input / output tokens, 2026-10-03 |
|---|---|---|---|
| Ultra | `nvidia/Nemotron-3-Ultra-550b-a55b` | us-central1 | $1.00 / $3.00 |
| Super | `nvidia/nemotron-3-super-120b-a12b` | us-central1 | $0.30 / $0.90 |
| Lightning | `nvidia/Nemotron-3_5-Lightning` | eu-north1 | $0.06 / $0.24 |

## Settings

Every model call that reads a receipt uses the pilot's settings:

- temperature 0, thinking off and `max_tokens` 512;
- reply parsing `first`, which takes the first JSON object in a reply;
- an input limit of 8,192 characters;
- a per-run budget of $0.02 and 60 seconds, where a run stopped by it counts as a miss;
- invalid output counted as a miss, with at most 2% invalid runs allowed on an exam;
- Token Factory's default service tier.

The bench settings file, `examples/bench/suite-v2/receipts.json`, is frozen in the same commit as this plan, with the bar at 0.40. The plain models run with the pilot's profile, `examples/bench/pilot-2026-10-04/profile.json` (sha256 `0a396a722c2615a71923d84c0f6bcb31f962e027cf6ec1b11eecb1968482d00a`), whose aliases `ultra`, `super` and `lightning` carry these settings. Every call goes through the project's spend relay, which holds this whole run to $3.00 of measured spend.

## The bar

The bar is 0.40. The pilot set it on 2026-10-04 (`docs/bench/pilot-2026-10-04.md`): plain Ultra got 74 of 150 pilot receipts right (0.493), and the bar is that accuracy minus 0.05, rounded down to a multiple of 0.05. The pilot's numbers set the bar and are not part of the results.

## What runs

### Plain models on the delivery receipts

```
MAIN="--suite v2 --tier public --tasks receipts --provider profile --profile $PROFILE --floor-split evidence --delivery-split delivery --delivery all --seed 7 --out $OUT --yes"
architect bench run $MAIN --plain --models ultra --label A1 --max-usd <bound>
architect bench run $MAIN --plain --models super --label A2 --max-usd <bound>
architect bench run $MAIN --plain --models lightning --label A3 --max-usd <bound>
```

`$PROFILE` is the pilot's profile and `$OUT` is a scratch directory outside the repository. Each `--max-usd` is the bound the bench prints for that command, rounded up to the cent. Every bench cell first builds on the evidence floor with one plain configuration and sits its 100-receipt exam, then answers all 348 delivery receipts.

### One ladder build

One build on the evidence floor, with the model ladder Architect uses when it builds an agent:

1. Lightning. Stage 2 measures 4 preset configurations on the 100 practice receipts. Then Forge runs one session with Super as the engineer (thinking off, temperature 0, 300 seconds a call), capped at $1.20 and 30 minutes. Its other settings are those of the last development round: 24 measures, 6 submissions, no spawns and a 400-turn limit.
2. Super, with its plain configuration.
3. Ultra, with its plain configuration.

Each rung sits the same 100-receipt exam, and the cheapest licensed rung ships. The stages outside Forge share a compile budget of $0.60 and 3,600 seconds. The spec and the profile are in `examples/bench/eval-2026-10-05/ladder/`. In the build's workspace, `floors/evidence.json` and `floors/evidence.manifest.json` are copies of `examples/receipts/floors/evidence.json` and `examples/receipts/manifests/evidence.json`, and `floors/evidence-practice.json` holds the floor's 100 practice receipts, which Forge uses as its smoke floor.

```
architect compile spec.json --profile profile.json --approved-spec spec.json --forge --forge-trigger always --forge-usd 1.20 --forge-minutes 30 --yes --audit $AUDIT
```

The Forge pack hash for these settings is `9a2d437bfd9376c9940e2fc758cc935cf23fc670b106a6ee16b4a1dfc150fbdf`, and its check finds no task text in the pack. The build answers no delivery receipts, so its result is the exam counts.

## What is reported

For each plain model, on the 348 delivery receipts:

- receipts right, and accuracy with a Wilson 95% interval;
- invalid outputs;
- cost per receipt from the tokens Token Factory reports, at the 2026-10-03 list prices, shown per 1,000 receipts;
- median (p50) latency per receipt as the bench measures it, relay included.

For the ladder build: each rung's exam result from its signed report (right of 100, invalid runs, licensed or refused), the rung that ships, the id of the agent it installs and what the build cost.

There are no significance tests and no pooled numbers.

The Wilson interval for k right out of n, with p = k / n and z = 1.96:

```
center = (p + z^2 / (2n)) / (1 + z^2 / n)
half   = z * sqrt(p * (1 - p) / n + z^2 / (4 * n^2)) / (1 + z^2 / n)
```

## Runs and reruns

- Each command runs once, and no run is repeated to change a number.
- A run that crashes (it writes no row, or the relay refuses one of its calls for a cap) may run once more with the same command. The results say when that happened.
- The rows the bench writes hold counts, hashes, dollars and seconds, with no receipt text. They are committed under `examples/bench/eval-2026-10-05/` with the results.

## Footer

Every results table carries these lines:

```
Prices: Nebius Token Factory list prices of 2026-10-03.
No build step saw the delivery receipts.
Regions: Lightning is served from eu-north1, Super and Ultra from us-central1, and latency depends on the region.
The models may have seen CORD v2 in pretraining.
The input is text rebuilt from CORD's word annotations. No OCR ran.
Every model ran at temperature 0 with thinking off. NVIDIA recommends 1.0 for these models.
```

## Changes after publication

Any change after the Gist is published goes here, with its date.

| Date | Change | Why | Results existed |
|---|---|---|---|
| 2026-10-06 | Adds two rule checks to the receipt reader, declared in its spec and run in code after the model's reply. The spec hash and the license cover them, so the exam tests the model and the checks together. Details below. | The pilot error analysis (`docs/bench/pilot-error-analysis-2026-10-04.md`): Ultra broke the subtotal rule as often as Lightning. On the 51 pilot receipts whose subtotal is null under the rules, Ultra gave a subtotal on 50 and Lightning on all 51. | Yes. All the 2026-10-05 results existed, including the ladder build's exam counts (Lightning 27, Super 51, Ultra 49 of 100). They stand as run. |
| 2026-10-12 | A private experiment used the exam. Plain Lightning, Super and Ultra answered the 100 exam receipts of the evidence floor again, once each (300 model calls), with their replies kept, and checks that Nemotron 3 Ultra and Super wrote were scored on those replies offline. This goes against the 2026-10-06 stop rule below, "no build of a different design sits it before 2026-10-29". The registered rules are unchanged. | I wanted to see whether a Nemotron model could write such checks itself, in place of the two I wrote by hand. | Yes. Every result on the results page existed and stands as run. No number from the experiment is in the results, and it read no delivery receipt. |

### 2026-10-06: two rule checks

Both checks apply sentences the rules already state (`examples/receipts/RULES.md`):

- `guard_not_total_copy`: a subtotal equal to the total becomes null unless a line the subtotal rule names prints it (SUBTOTAL, ITEMS TOTAL, HARGA JUAL, DPP, NET TOTAL, an ITEMS line, a TOTAL line that a later GRAND TOTAL follows, or a line with an amount and no letters).
- `guard_digits`: a total, subtotal or tax that matches no printed amount, but matches one with its two decimal digits dropped, takes all its digits ("226,500.00" is 22650000).

On the labels of the pilot, the rehearsal floor and the practice half of the evidence floor, 350 receipts in all, the checks change nothing. No replay scored or printed an exam receipt.

Predictions, made before this change's first model call:

- Gate 1: plain Lightning and plain Super answer the 100 rehearsal receipts once, with their replies kept, and the checks run on those replies offline. The checks add 10 to 20 right receipts for Lightning and 12 to 25 for Super.
- Exam: one ladder build runs on the evidence floor with the checks declared, and each rung sits the 100-receipt exam once. Lightning gets about 41 right (36 to 47), and Super and Ultra about 68 each (62 to 74).

Stop rules:

- After Gate 1, the work goes on only if each model gains at least 8. Otherwise the reader ships without the checks.
- The exam gets one build and no rerun, and no build of a different design sits it before 2026-10-29. The demo video's build is the same design sitting the exam again, and both results are reported.
- The checks stay only if the rung that ships gets at least 8 more right than that model's 2026-10-05 exam count (Lightning 27, Super 51, Ultra 49). Otherwise they come out, and the result is still reported.

The checks were frozen before any model call. The sha256 of `frozen_checks.py`, the file that holds them, is `5e9dbaf7816973e882001fd5e39a9e4ae620d867ec494f4210406ca56cfcf40a`. The sha256 of each function's source, as Python's `inspect.getsource` returns it, is `026ee79d7a94d7b635dd38f2bdc0b2f39c6d73a1eaebc4197090498044bc8e33` for `guard_not_total_copy` and `b2bf2c7f2995072023141c7afc885b5ac3299c4c4ae3346aab9ebc6612f047bf` for `guard_digits`.

Results:

- Gate 1 ran first. On the 100 rehearsal receipts the checks took plain Lightning from 29 to 41 right and plain Super from 50 to 64, gains of 12 and 14, and turned no right receipt wrong. Both gains were at least 8, so the work went on.
- The exam: one ladder build ran on the evidence floor with the checks declared (`examples/bench/eval-2026-10-06/ladder/`), and each rung sat the exam once. Lightning got 39 of 100 and was refused. Super got 65 and Ultra 71, and both were licensed. Super shipped with 14 more right than its 51 of 2026-10-05, so the checks stay. Each count is inside its predicted range, and nothing was rerun. `docs/bench/results-receipts.md` has the details.
- The demo video's builds, which the stop rules ask to report: the same spec was built three more times in the desktop app, twice on 2026-10-06 and once on 2026-10-07, each time in a new workspace, and each rung sat the exam once. The stop rule names one video build. There were three because the video was filmed twice more after fixes to the app, and the exam's result above still stands. At 14:27 UTC on 2026-10-06, through the relay, Lightning got 39 and was refused, Super got 63 and Ultra 70, and Super shipped. At 20:20 UTC, with Token Factory called directly because the relay was down, Lightning got 42 and was licensed, Super got 65 and Ultra 72, and Lightning shipped as the cheapest licensed rung. At 09:19 UTC on 2026-10-07, again with Token Factory called directly, Lightning got 39 and was refused, Super got 64 and Ultra 72, and Super shipped. All four builds made the same three agents (their artifact digests match) and examined them on the same 100 receipts. The reports are in `examples/bench/eval-2026-10-06/video-1/`, `video-2/` and `video-3/`. One more build of the spec, at 16:01 UTC on 2026-10-06, was stopped by hand during Lightning's exam after the relay began refusing every call, so it scored no exam and wrote no report.

The sha256 of `frozen_checks.py` above is that of a private working file. Its public copy is `docs/bench/eval-2026-10-06/frozen_checks.py`. The two differ only in their header, the lines above `import re`, and from `import re` to the end they are byte for byte the same. So the public copy has its own file sha256, `a04381fb34a14bc061addb512b2f0e112933e862b92e17f74fb6e0ff6f226bb3`, and the sha256 of each function's source is the one above.
