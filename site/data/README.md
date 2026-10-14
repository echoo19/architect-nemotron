# Site data

Everything the page shows comes from the two files here. `results.json` holds the real
numbers of the evaluation run of 2026-10-05 and of the ladder builds with rule checks of
2026-10-06, and says `"placeholder": false`. Until the
release is out, `site.json` holds placeholder data: it says `"placeholder": true`, the page
shows a banner that says so, and `node site/build.mjs --release` refuses it. A real file
must say `"placeholder": false` in so many words.

Both files are JSON in UTF-8 and name their shape in `"schema"`. Scores are fractions from 0
to 1, and money is US dollars at the list prices of `results.json` `prices_date`.

## site.json

`"schema": "architect-site/2"`

| Field | Meaning |
|---|---|
| `repo` | the public repository's https URL, used for the source links and the downloads |
| `video` | `youtube_id`, an 11-character YouTube id or `null` until the video is out, and `duration` as `m:ss` or `null` |
| `release` | `tag` and the installer file names: `windows`, `mac_arm64` and `mac_x64`. A download link is `<repo>/releases/download/<tag>/<file>` |
| `commands` | the terminal commands, one string each, in order |

The installer names follow `desktop/electron-builder.yml`: `Architect-Setup-<version>.exe`
and `Architect-<version>-<arch>.dmg`.

## results.json

`"schema": "architect-site-report/1"`, holding the evaluation run of 2026-10-05 and the
ladder build with rule checks of 2026-10-06 (`docs/bench/results-receipts.md`).
`site/build.mjs` turns it into the page's two tables. The first has one row per `plain` arm,
in order: the models alone on the delivery receipts. The second has one row per rung of
`checked_ladder`, cheapest first, beside the rung of `ladder` with the same model. A rung
belongs to the plain arm whose `model_name` is the rung's `model`. The video's poster draws
`checked_ladder`. The build checks every count and interval before it draws them, and stops
on a mark that would fall outside `domain`.

| Field | Meaning |
|---|---|
| `source` | `command`, `suite`, `suite_digest`, `frozen`: what produced the numbers |
| `task` | `id`, `title`, `fields` (the four receipt fields), `rules_url` |
| `bar` | the bar, 0.40 for the receipt reader |
| `domain` | the scale of the small charts and the poster, `[0.2, 0.8]`, wide enough for every score they draw |
| `prices_date` | the date of the list prices behind the costs, `2026-10-03` |
| `delivery` | `split`, `n` (the 348 delivery receipts every arm read), `cases_sha256` of the delivery list, `test_n` (the CORD test receipts within it, 96) |
| `outcome` | `id` and `headline`, the run's outcome in one sentence |
| `arms` | one entry per arm, in table order (below) |
| `footer` | the lines every results table carries, verbatim from the evaluation plan's footer; the page lists them under the tables |
| `plan` | links to the evaluation plan, its public Gist and the results page |
| `ladder` | the ladder build of 2026-10-05, without rule checks (below) |
| `checked_ladder` | the ladder build of 2026-10-06, with the rule checks (below) |
| `forge_record` | every Forge session on this task with a Nemotron engineer (below) |

Each arm:

| Field | Meaning |
|---|---|
| `arm`, `name` | the label in the evaluation plan (`A1`) and a short description |
| `kind` | `plain` for a plain prompt; the page shows only these |
| `model`, `model_name` | the Token Factory model id and its readable name |
| `builder`, `builder_name` | the model that wrote the harness, or `null` |
| `replicate_of` | the arm this one replicates, or `null` |
| `practice` | the practice score that sat the exam, or `null` |
| `exam` | `cases`, `correct`, `invalid`, `over_budget`, `score`, or `null` for an arm that ran on delivery receipts only |
| `licensed` | the license decision, or `null` without an exam |
| `delivery` | `n`, `k` (receipts with all four values exact; invalid outputs count as misses), `score` (`k / n`), `ci` (the Wilson 95% interval of `k / n`), `invalid`, `over_budget`, `field_accuracy`, `test` (`n` and `k` on the test receipts) |
| `cost_per_receipt_usd` | model cost per delivery receipt, reasoning included |
| `calls_per_receipt`, `zero_call_share` | model calls per receipt, and the share of receipts settled with none |
| `p50_s`, `p95_s` | latency in seconds, nearest rank |
| `build_cost_usd` | what the build cost, or `null` |
| `break_even_receipts` | receipts until the build pays for itself against A1, or `null` |
| `links` | `harness` and `rows` URLs, or `null` |
| `receipt` | a signed run summary, or `null` |

`ladder` is the ladder build of 2026-10-05: `build`, `arm`, `ladder`, `take`, `recorded_at`
(ISO 8601 in UTC), `spec_hash`, `video_at` (unused, `null`), `bar`, `floor`
(the cases it practiced and was examined on: `dataset`, `cases_sha256`, and the `practice`
and `exam` counts), `ships` (the name of the rung that shipped, or `null`), `rungs`,
`delivery` (`null`, because the ladder build answered no delivery receipts), `retake`
(`null`, or why a take was repeated), `agent` (the installed agent's `id`, `revision`,
`artifact_digest` and `model`), `build_cost_usd` and `forge` (`engineer`, `turns`,
`cost_usd`, `submissions`, `accepted`).

`rungs` lists the rungs cheapest first, as `design/components.js` `barView` takes them:
`name` (the short name), `model` (the readable name), `state`, `preset` (the practice
score of the tuned preset, which the poster draws as a tick), `practice` (the practice
scores of Forge's harnesses, when it wrote any), `examined` (the practice score of what sat
the exam; give it when Forge wrote harnesses and none was accepted, so the poster's stem
starts at the preset), `exam` (`correct`, `cases`, `invalid`), `exam_cost_per_receipt_usd`
(the model cost per exam receipt, `mean_cost_usd` of the rung's signed exam report),
`licensed`, and `ships: true` on the rung that shipped. Only a licensed rung ships, and at
most one does.

`checked_ladder` is the ladder build of 2026-10-06, whose spec declares two rule checks:
`build`, `recorded_at`, `commit` (the commit it ran at), `spec_hash`, `bar`, `floor`,
`checks` (each check's `check` name and its `rule`, as the spec declares them), `ships`,
`rungs` (as in `ladder`), `delivery` (`null`: the checks did not run on the delivery
receipts), `agent`, `build_cost_usd`, `forge` (`null`, because Forge was off) and
`later_sits`. Its `floor` must match the `cases_sha256` and `exam` of `ladder`'s, and it
must have one rung for each model `ladder` has, so the page can set each exam beside the
same exam without the checks. The page's words describe `keep_digits` and `not_copied`, so
the build stops on other checks until the words change.

`later_sits` lists the later builds of the same spec that made the same agents (the same
artifact digests) and sat the same exam, each with `recorded_at`, `exam` (the right answers
of each rung, by its `name`) and `ships` (the rung that shipped, which must have cleared the
bar, or `null`). The page says, in one line under the second table, what they gave the
cheapest rung. They are the three builds made in the desktop app for an earlier cut of the
video, two on 2026-10-06 and one on 2026-10-07 (`examples/bench/eval-2026-10-06/video-1/`,
`video-2/` and `video-3/`). The final video doesn't use them.

`forge_record` counts every Forge session on this task that had a Nemotron model as the
engineer: `task`, `sessions`, `accepted` (sessions that ended with an accepted harness),
`by_engineer` (`model_name` and `sessions`, adding up to `sessions`), `in_ladder_build`
(how many of them ran inside the ladder build) and `doc`. On 2026-10-05 that is 12
sessions, 5 with Ultra and 7 with Super, none accepted: 11 in development on the rehearsal
floor (`docs/feedback/probes/2026-10-05-forge-last-round.md` lists them) and the ladder
build's.

## Filling in the real numbers

1. `results.json` is real. A later run replaces it in the same shape, with every number
   taken from that run's files: `examples/bench/eval-2026-10-05/` and
   `examples/bench/eval-2026-10-06/` for this one, and the exam reports in
   `architect/published/receipts/` for the exam costs.
2. Fill `video` and `release` in `site.json` once they exist, and set its
   `"placeholder": false`.
3. Run `node site/build.mjs --release`. It stops on placeholder data, a count, interval or
   score that does not fit, or a private marker in the output.
