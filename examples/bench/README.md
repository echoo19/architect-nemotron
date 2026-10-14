# Bench examples

This directory holds the identity files for the frozen bench suites and the committed
bench rows of the receipt pilot and evaluation. None of it is content: there are no
prompts, outputs, case ids, page text or page images here, only seeds, sizes, counts and
sha256 hashes.

## `suite-v1/`

| File | What it pins |
|---|---|
| `tickets-manifest.json` | seed, `n` and the sha256 of the canonical cases JSON from `generate_tickets` at every tier size of `tickets-40` |
| `invoice-manifest.json` | the same for `generate_invoices` and `invoice-40` |
| `pages-manifest.json` | the `clean` renderer's per-page sha256, question count and key-page flag for `exam-clean-40`, `digitizer-24` and `digitizer-multipage-12` at their full-tier sizes (seed 7, 96 dpi) |
| `smoke-baseline.jsonl` | added with the first green smoke run: the `bench compare --expect identical-scores` baseline for CI |

The cases themselves are regenerated in process from the seed every run.
`architect.bench.suite.example_manifests("v1")` rebuilds all three manifests, and
`tests/test_bench_suite.py` fails if a committed manifest no longer matches its
generator. A mismatch means the generator changed. That needs a new suite version
and a fresh baseline; do not just rewrite the manifest.

Delivery cases (`bench run --delivery N`) are drawn from generator seed `seed + 1000`,
and their case ids start with `delivery-`. They never repeat a tuning or held-out case.
Tickets need extra care here because some ticket templates have no placeholders and
print the same text under every seed. So tickets are split into two fixed pools by the
sha256 of their text: `generate_tickets` draws only from the main pool and
`generate_delivery_tickets` only from the delivery pool. Delivery cases are not
pinned in these manifests.

PNG pages are never committed. To look at a page, render it locally:

```
architect bench pages --seed 7 --count 2 --style clean --out .architect/pages --png
```

Only the stdlib `clean` renderer is byte-deterministic across platforms. Its hashes
are the only page hashes pinned here, and `tests/test_bench_pages.py` pins
`generate_pages(7, 2, "clean")`. The `scan` and `dense` styles use Pillow (the optional
`bench` extra, `uv sync --extra bench`). Their bytes depend on the Pillow build, so
each dataset's `manifest.json` records `pillow_version` and `platform`, and
`bench compare` refuses to pair fake-provider rows whose page manifests differ.

## `suite-v2/`

Suite `v2` has one tier, `public`, with one task, `receipts`. Its cases are real
receipts: CORD v2 receipt text (CC BY 4.0) that `scripts/fetch_bench_data.py` rebuilt
from the dataset's annotations into `examples/receipts`. The answer is four exact
numbers (`total`, `subtotal`, `tax`, `item_count`) under `examples/receipts/RULES.md`,
and the cell's spec quotes those rules word for word.

| File | What it pins |
|---|---|
| `receipts.json` | the bar (`floor_limit`), the per-run budget, how invalid outputs count and their ceiling, the input limit, the reply parsing mode, and `frozen` |

The pilot of 2026-10-04 set the bar at 0.40, and the evaluation plan
(`docs/bench/prereg-nebius.md`) froze the file before the evaluation ran, so it says
`"frozen": true`. Rows measured while it said `false` are rehearsal numbers, and
`bench report` prints a line saying so. Any change to the settings or to one case
changes the v2 suite digest, so rows from before and after such a change never pair.

A cell whose model calls for one run would cost more than the pinned per-run budget is
refused before anything is spent (`bench_run_budget_too_small`), so give each model
alias a `max_tokens` cap.

The data has four splits, each with a manifest of case hashes:

| Split | Use |
|---|---|
| `pilot` | the pilot that sets the bar (`--delivery-split pilot --delivery all`) |
| `rehearsal` | development runs (`--floor-split rehearsal`) |
| `evidence` | the floor of the pre-registered runs, half practice and half exam |
| `delivery` | the replay no build step ever sees (`--delivery N` or `--delivery all`) |

Before anything is spent, the bench checks the data against the manifests: the hash of
the rule text, every case hash, each split's list hash in file order, both JSON
Schemas, the even/odd partition of the two floor splits, and that no receipt sits in
two splits. A missing file is `bench_dataset_missing` and any difference is
`bench_dataset_mismatch`. The bench refuses a changed dataset; it never relabels one.

The free plumbing check runs the receipts task on the fake provider:

```
architect bench run --provider fake --suite v2 --tier public --floor-split rehearsal --delivery 20 --yes
```

Delivery rows carry per-run metrics: pass bits, invalid and over-budget counts,
dollars, calls, runs answered with no model call, p50 and p95 seconds, and each run's dollars.
Arms on different models pair on those delivery runs, and the report prints both:

```
architect bench compare rows/*.jsonl --pair-arms A1,A2 --pair-arms A2,A3
architect bench report rows/*.jsonl --md --pair-arms A1,A2 --footer footer.md
```

An arm is a run label (`--label A3`) or `MODEL:VARIANT`. The comparison uses exact
McNemar with Holm across every pair, Newcombe's paired interval for the accuracy
difference, and a paired bootstrap interval for the cost ratio.

`--audit DIR` copies every Forge submission out of a run. `bench audit DIR --profile P`
later replays the ones that met the bar in practice on their exam and counts how many
then missed it. The count takes only scored exams on a floor that matches its public
dataset manifest and never a fake-provider replay; the others are listed by reason. A
submission is never replayed on a floor whose practice cases differ from the ones it
was measured on.

## What the scores mean

Every suite v1 floor is synthetic by construction. `sentiment-24` is the committed
`examples/live/sentiment-cases.json` fixture; every other v1 task is generated from a
seed. An exam-page score says "extracts what this generator prints". It is not
live uploaded-image vision and it says nothing about digitizing real uploads.
Each row carries an `evidence` label next to its numbers: `synthetic_fake` for the
deterministic oracle on the fake provider, `live_billed` for API providers,
`live_api_equivalent` for CLI providers, whose money is API-equivalent and not a bill,
and `local_zero_cost` for Ollama. It also carries a `floor_origin`:
`synthetic_generated` or `example_fixture` for v1, and `public_dataset` for the v2
receipts. A fake-provider row on the receipts is still a plumbing check: the oracle
answers from the case labels, so its score says nothing about any model.

The `noisy` variant is a mechanism check, not a model of anything. Its oracle flips a
fixed, seeded quota of answers in each floor partition: enough held-out cases to put
the score strictly below the floor limit, and at least 6 tuning cases, so every noisy
cell is unlicensed and `bench compare` of `default` against `noisy` is a `regression`
with `mcnemar_p < 0.05`. A noisy result tells you the comparison machinery works; it
says nothing about how a weaker model would score.

## Committed rows

Each arm's folder holds its run as the bench wrote it: `rows.jsonl`, one content-free
`BenchRow` per line, with `run.json` and `summary.json`. `preset_fingerprint`,
`prices_sha256`, `suite_digest` and `architect_commit` on each row identify what was
measured. `docs/bench.md` covers the run and compare workflow.

| Folder | What it holds |
|---|---|
| `pilot-2026-10-04/` | the pilot on 150 receipts that set the bar, and its profile (`docs/bench/pilot-2026-10-04.md`) |
| `eval-2026-10-05/` | the evaluation: `A1` Ultra, `A2` Super and `A3` Lightning on the 348 delivery receipts, and `ladder/`, with the ladder build's spec, profile and a summary of its signed reports (`docs/bench/results-receipts.md`) |

This rebuilds the evaluation's table from its rows:

```
architect bench report examples/bench/eval-2026-10-05/A1 examples/bench/eval-2026-10-05/A2 examples/bench/eval-2026-10-05/A3 --md
```
