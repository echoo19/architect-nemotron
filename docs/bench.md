# Bench

`architect bench` measures presets on a frozen, versioned suite and turns every
proposed change into a paired comparison with a verdict computed by code. It
runs the real compiler path (`Compiler.compile(spec, approved=True)`: Stage 1
selection, Stage 2 tuning, Stage 4 verification; Stage 3 rewriting is off so a
row measures presets, not rewrites) in a fresh throwaway workspace per cell and
writes one content-free row per (task, model, variant, repeat) cell to
`<out>/<bench_id>/rows.jsonl`, plus `summary.json`.

The bench is a measuring instrument, not an authority. Stage 4 stays the only
licensing authority. For text, vision, pipeline and local-game cells `licensed`
in a row is re-read from a signed report on disk. Nothing is ever installed into
an operator registry, the workspace is deleted after every cell, and a `compare`
verdict advises the operator without changing any registry, artifact or preset.
The one exception is `bench arc`: its `arc-preview` cell manufactures a harness
through `architect.games.public` exactly as `scripts/arc_harness.py` does, which
installs that harness into a throwaway registry inside the cell workspace
(deleted with it) and plays it there. That row's `licensed` is Stage 4's verdict
as the manufacture summary reports it, not a separate re-read from disk.

## Tiers and tasks

`architect bench list` prints the suite (tasks, kinds, floors, checkers, sizes
per tier, calls at the full size), a price-table bound per configured model and
the bench ids recorded under `--out` (default `.architect/bench`).

| Tier | Tasks | Use |
|---|---|---|
| `smoke` | sentiment-24, tickets-40, invoice-40, exam-clean-40, exam-scan-40, digitizer-24, arc-local-3 | free mechanism gate on the fake provider (CI) |
| `quick` | sentiment-24, invoice-40, exam-clean-40 | the paid decision tier before a preset change merges |
| `full` | smoke tasks plus digitizer-multipage-12 at full sizes | monthly, or before quoting a number |
| `arc` | arc-preview (cached OFFLINE public ARC-AGI-3 games) | when the ARC harness or game presets change |

Every task is synthetic by construction except `sentiment-24`, which is the
committed example fixture. A score says "extracts what this generator prints",
never anything about real uploaded images or real exam pages. The exam pages
are rendered synthetic pages with known ground truth; they are not live
uploaded-image vision and they are not a digitizer result.

## Run it

Local models first: the ARC-AGI-3 competition runs a LOCAL model on the
organizers' hardware, so a local Ollama model is a first-class bench provider.

```bash
# Free, deterministic, no profile: the mechanism gate (measured: about 10 s on a
# Windows laptop for default + noisy).
uv run architect bench run --provider fake --tier smoke --seed 7

# Local Ollama models ($0; wall time is the meaningful spend).
uv run architect bench probe --profile local.json
uv run architect bench run --tier quick --profile local.json --models granite4.2:8b --seed 7
uv run architect bench run --tier arc --profile local.json --models gemma4:26b \
    --arc-games 5 --arc-actions 24 --timeout-s 7200

# Cloud models (paid or API-equivalent; confirmed before any spend).
uv run architect bench run --tier quick --profile examples/live/claude-code.json --seed 7 \
    --label base-2026-09-26 --max-usd 5

# Compare two runs by bench id (under --out) or by rows.jsonl path.
uv run architect bench compare <base-id> <candidate-id>

# Inspect the synthetic exam pages (PNGs only when asked).
uv run architect bench pages --seed 7 --count 2 --out .architect/pages --png
```

A local profile names each installed model with the `$0` price table in
`examples/local/prices.json`, a fixed context and a per-call timeout:

```json
{
  "models": {
    "granite4.2:8b": {"provider": "ollama", "base_url": "http://127.0.0.1:11434",
                      "prices": "examples/local/prices.json", "num_ctx": 65536,
                      "timeout_s": 600},
    "gemma4:26b": {"provider": "ollama", "base_url": "http://127.0.0.1:11434",
                   "prices": "examples/local/prices.json", "num_ctx": 65536,
                   "timeout_s": 900, "think": false}
  }
}
```

(Paths resolve against the profile file.) The local price table lists no
`image` entry, so vision cells on those models are skipped with
`image_not_configured` rather than priced by guess. To bench a vision-capable
local model, add an `image` entry (`rule: fixed`, a `tokens_per_image_max`
ceiling, `basis: assumption`) to your own copy of the table and run `bench
probe` first: it makes one text call and one image call and reports whether the
ceiling held. A local model always runs with `model_binding: offline` in ARC
rows; cloud models are labeled `online_preview`.

"Local" is checked, not assumed. Before anything is spent (`run` and `probe`
refuse it; `list` leaves it out), an `ollama` model whose `base_url` (or `OLLAMA_HOST`) is not a
loopback host, such as Ollama's cloud, whose price-table entry is not $0 in
every field, that carries an API key (a keyed loopback endpoint is a proxy to a
hosted service), or whose name ends in `-cloud` (Ollama serves those from its own
servers through the local daemon), is refused with `bench_model_not_local`. A
local cell may not route a pipeline step (`--route`) to a model that fails the
same check. So a `local_zero_cost` row is always a model on this host at $0. A model name a row cannot record (it must
match `^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$`; namespaced names such as
`user/model:tag` or `hf.co/...` do not) is refused with `bench_model_unconfigured`
up front; give such a model a plain alias with `ollama cp`.

Budgets are dollars and seconds; tokens are metered, never capped. Before any
spend the run prints the bound line

```
bench <tier> on <n> models: <=$<bound> API-equivalent bound, ~<calls> calls, not a bill.
```

and, for any provider other than fake, asks `Continue? [y/N]` on an
interactive console. Consent fails closed: `--yes` is the only non-interactive
consent, so a paid run from a pipe, a script, cron or a CI shell without `--yes`
stops with `bench_cancelled` before anything is spent, and so does any answer
other than `y` (end of input included). With `--json` the question goes to
stderr, like every other human line, so stdout carries only the result object.
Per-run budgets are lifted from each adapter's own quote; a cell whose
reservation is above `--max-usd` is skipped with `over_max_usd`, and a run whose
remaining total is above it, or that the cap leaves with no runnable cell, is
refused with `bench_over_max_usd` before anything is spent. A $0 local model
reserves $0: its bound, `reserved_usd` and the `--max-usd` check count only
price-table money, and its spend is wall time (internally its Spec still carries a
nominal $0.01 run wallet so the runtime's dollar checks stay well-formed; that
wallet is never shown or capped). Set `--timeout-s` (default 3600 s per compile)
for slow local hardware. The `arc-preview` cell's bound is three times its
compute cap: `public.manufacture` opens three wallets in turn (the proposal
compile, the approved compile and the play run), each allowed the full cap, and
the bound and `--max-usd` cover all of them. `--provider` defaults to the profile: without
`--provider fake`, `--profile` is required (`bench_profile_required`).

Useful flags: `--models a,b`, `--presets extractor`,
`--variant default|noisy|images-all-stages|template|forge` (repeatable; `noisy` is
fake-only; see [Forge and template arms](#forge-and-template-arms)),
`--forge-usd X --forge-minutes M --forge-model NAME` for the forge arm,
`--route vision=M,solver=M,verifier=M` for the
digitizer pipeline, `--repeats R`, `--size N` (every non-game task), `--max-candidates K`
(default 2), `--delivery N` (a disjoint out-of-sample replay), `--arc-games`,
`--arc-actions`, `--arc-cache DIR`, `--label TEXT`, `--out DIR`, `--json`.

Exit codes: 0 success (a `regression` verdict is still 0; the verdict is data), 1
with `{"error": "<bench_* code>"}` on stderr, 2 for argument errors, 130 when
interrupted (rows written so far are kept and the in-flight cell is recorded as
`cancelled`). A tier, `--presets` or `--variant` filter that selects no cell is
refused with `bench_no_cells` before anything is written. An arc run in which
every cell is unavailable (no `arc` extra, no model the preview can drive, or no
cached public games) exits 1 with `bench_arc_unavailable`; when that is found
only while running (an empty cache), the rows are still written.

**Resuming a stopped run** (Phase 11). Every run writes `run.json` next to
`rows.jsonl`: its settings plus the suite, preset and profile fingerprints.
`architect bench run --resume <bench_id>` (with `--out` if it was not the default)
continues that run with those saved settings; only `--out`, `--json` and `--yes`
may accompany it, and any other run flag exits 2. It skips every cell that already
has a row and runs the rest, appending to the same `rows.jsonl`; the bound and the
consent prompt cover only the remaining cells. A `cancelled` or `host_memory_low`
row, and a last line cut short when the process was stopped, do not count as done:
they are dropped and the cell runs again. A changed profile, suite or preset set, or
a run from before Phase 11 (no `run.json`), is refused with `bench_resume_mismatch`
before any spend. A routed run cannot resume (`bench_resume_unsupported`) because one
route log spans all its cells. The summary covers the kept rows and the new ones.

In the interactive session: `bench smoke` (free, never prompts), `bench quick
[model,model]`, `bench full [model,model]`, `bench arc [model]` (bound line, then
`Continue? [y/N]`), `bench compare <id> <id>`, `bench resume <id>` (continue a stopped
run, confirmed like a new one), `bench list`. While a bench runs
the session is in BENCHING: `status` shows the current cell (and still names the
session's own signing key, pinned before the bench starts, although each cell
points `ARCHITECT_SIGNING_KEY_FILE` at its throwaway key while it runs), `cancel`
stops the run and keeps the rows. Rows go to `<workspace>/bench/<bench_id>/rows.jsonl`.

## Reading rows and tables

`bench_table` columns: `task | model | var | lic | held-out (k/n) | tuning | cost $ |
calls | wall | evidence`. A model id longer than its column is never clipped: it is
shown as `#1`, `#2`, ... and a `model #1: <id>` line spells each one out. A row that
carries a bench cell code (`cancelled`, `workspace_error`, `arc_unavailable`, ...)
is followed by a `cell code:` line whether or not it is licensed: a licensed cell
can still be cancelled or lose its workspace after Stage 4. The floor origins come next, and every table ends with
the line `money is API-equivalent, not a bill; scores are <evidence labels>`.
Evidence labels:

| Label | Provider | Meaning of the money column |
|---|---|---|
| `synthetic_fake` | fake (oracles) | synthetic cost from the fake price, a mechanism check |
| `local_zero_cost` | ollama | $0; wall time is the spend |
| `live_billed` | anthropic, openai | measured by the provider API |
| `live_api_equivalent` | claude-code, codex | API-equivalent estimate from `prices.json` (Codex reports no cost) |

A row carries `prices_sha256`, `reserved_usd` and `per_call_reservation_usd`
next to `cost_usd`, the `preset_fingerprint`, `suite_digest` and
`architect_commit` that identify what was measured, and
`image_tokens_estimate`, a provider-formula ESTIMATE for display only
(reservations always use the configured per-image ceiling). Rows never carry
prompts, outputs, case ids, page text, media types or hashes of content.

`bench probe` prints each model's evidence label (`ev`, spelled out in a closing
`evidence:` line), `text r/m` (reserved/measured input tokens), `image r/e/m`
(reserved, estimate, measured) and flags `assumption` ceilings with `!`. Its
codes are a closed set: `image_reservation_under_configured`,
`image_not_configured` and `provider_*` (`provider_unavailable`,
`provider_timeout`, `provider_context_exceeded`, `provider_failed`, and
`provider_reservation_exceeded` when the text call itself used more than it
reserved: raise the model's `input_overhead_tokens` or `output_capacity_tokens`;
the image call is then skipped). A
measured image usage above its reservation is a `BudgetExceeded` from the
runtime, reported as `image_reservation_under_configured`. A $0 local model can
never exceed a dollar reservation, so the probe also compares the measured image
tokens with the ceiling's reserved tokens and reports the same code when they are
higher. Ceilings are only ever lowered to `max(measured) * 1.25` with the
measured row cited.

## Forge and template arms

Phase 9 adds two variants that let Stage 3 run, so the bench can compare the
tuned preset with harness code:

| Variant | Stage 3 | Agent |
|---|---|---|
| `default` | off (the preset as tuned) | none |
| `template` | Forge in `template` mode: the default v2 template is submitted once | none |
| `forge` | Forge in `agent` mode: a Pi session writes, measures and submits code | the Forge agent |

Both arms force `trigger: "always"`, so a passing Stage 2 cannot make Forge
silently skip. `enable_rewriting` stays off. Only these cells set
`GameFactoryConfig.allow_forge`. A forge or template cell uses the same Spec and
seed as its default cell, so `spec_hash` is identical and the arms pair. Stage 4
licenses every arm the same way. On a profile run the Forge settings come from
the profile's `compile.forge`, overridden by `--forge-usd`, `--forge-minutes` and
`--forge-model`. The agent is `--forge-model` / `agent_model` when set, otherwise
the profile's `forge_agent`, otherwise the cell's model. On `--provider fake` the
agent is `ForgeScript`, which replays the committed Pi tool-call script
`examples/forge/scripts/<task_id>.json` through the real pinned Pi SDK. That
run is $0 and deterministic, and it needs
`npm ci --prefix architect/backends/pi-node`.

Rows of these arms carry `forge` (`ForgeFields`): `mode`, `agent_model`,
`checks`, `dryruns`, `measures`, `submissions`, `accepted`, `spawns`,
`agent_cost_usd`, `agent_tokens_in`, `agent_tokens_out`, `agent_wall_s`,
`harness_nodes`, `harness_bytes`, `subharnesses`, `language`. `cost_usd` stays
play-time and delivery cost. Build-time agent cost appears only in
`forge.agent_cost_usd` and is never folded into `cost $`. The up-front bound and
`--max-usd` add `forge.max_usd` for each forge cell and $0 for each template
cell, on top of the normal compile.

Guards:

- A forge row with `forge.measures == 0`, or a template row with
  `forge.submissions == 0`, is written with `forge_did_not_run`. `bench run`
  then exits non-zero, and such a row is never a Forge result.
- On any provider other than fake, a forge or template game cell refuses with
  `host_memory_low` when free RAM is under 6 GB. Run one bench cell and one
  loaded model at a time, and run `bench probe` first.

`bench_table` adds a `process` block whenever a row has Forge or ARC process
data: `meas` (forge_measure calls), `subs` (forge_submit calls), `agent$`
(build-time, API-equivalent), `dist` and `revis` (below). The block is headed
`forge counts are tuning evidence, not a license`.

**Paired variants.** `bench compare A B --pair-variants BASE:CAND` compares
variant BASE in A with CAND in B, cell by cell, ignoring the variant in the
key. A and B may be the same file:

```bash
uv run architect bench compare <run> <run> --pair-variants default:forge
uv run architect bench compare <run> <run> --pair-variants template:forge
```

The `default:template` gap comes from committed code, not from an agent. The
`template:forge` gap is what the agent adds on top of the committed code.

**ARC process metrics.** Every arm's ARC rows (`arc-local-3`, `arc-preview`)
carry these fields in `arc`, computed from the game backend's per-game
counters:

| Field | Meaning |
|---|---|
| `distinct_states_mean` | distinct observed states per game, averaged over games |
| `revisit_rate_mean` | revisits / actions per game (at most 1), averaged over games |
| `calls_per_action` | total play-model calls / total actions |
| `levels_completed_total` | levels completed, summed over games |

Rows written before Phase 9 leave them `null` and stay readable.

**Stage A result (fixture-only).** The following comes from
`bench run --provider fake --tier smoke --seed 7 --variant default --variant noisy
--variant template --variant forge`, on the fake provider with the fixture play
agent. It is a mechanism check, **not a live or benchmark result**, and says
nothing about any real model. Both the forge and template rows of every smoke
task were licensed by Stage 4, with stage 3 in their stages. On `arc-local-3`,
forge made 3 submissions and 3 measures; template made 3 submissions and 0
measures. `levels_completed_total` was 3 for every arm.

| `arc-local-3` | default | template | forge |
|---|---|---|---|
| `distinct_states_mean` | 5.333 | 7.333 | 7.333 |
| `calls_per_action` | 1.0 | 0.607 | 0.607 |
| `revisit_rate_mean` | 0.167 | 0.381 | 0.381 |

The explorer code raises distinct states and cuts model calls per action. Its
revisit rate is *worse* than the preset's, because it spends early actions
probing untried options. The template and forge arms are identical here: the
fixture script writes the committed `examples/forge/arc_explorer.py`, which is
byte-for-byte the `game_explorer` template. The four-variant smoke run took
about 2 minutes on a Windows laptop. Live Stages B and C (a cloud agent with a
local play model, then all local) have not been run. They need operator approval
and the host memory preflight.

## Routed arm (Phase 10)

`--variant routed` measures the router (`architect.routing`) on the frozen suite.
It runs on text, vision and pipeline tasks; game tasks are not routed. Routing
orders licensed versions. It never licenses anything: Stage 4 decides every
license, and every routed number is shown with its evidence class.

Each routed cell runs in a fresh workspace with its own registry, library and
telemetry. One route log serves the whole bench, in a short temporary root that is
removed at the end.

- **Phase A.** The cell compiles the `default` arm, plus the `template` and
  `forge` arms when the run selects them. Each compile uses lifecycle `install`
  (an exact override of the suite spec). Every licensed result is installed in
  the cell's own throwaway registry as `<variant>-<task_id>`. The slot excludes
  lifecycle, so the task's own spec still matches it exactly.
- **Phase B.** Three probes are routed with origin `bench` and reuse granted:
  - `exact`: the task's own spec, expected to reuse the installed entry (Tier A);
  - `sibling`: the same schemas, tools and floor, with the task text replaced by
    the committed paraphrase in `examples/bench/suite-v1/siblings.json`, expected
    to warm-start from a template (Tier B);
  - `foreign`: the spec of the next routed task in suite order whose shape differs
    (with its own dataset and floor), expected to compile.

  The decision is then executed. A reuse goes through `load_reuse_target` (a
  fresh license, digest and slot re-check) and the Runtime, with no compile and
  $0 compile cost. A warm start is a compile through `RoutedLibrary`. A compile is
  a normal compile. A reuse or warm start that cannot execute is recorded (for
  example `route_target_changed`) and falls back to a normal compile.
- **Oracle.** Every eligible option is scored on the probe's held-out split. An
  installed entry of the same slot scores its Stage 4 held-out score at $0. A
  template scores a warm-started compile of the probe from it; these oracle-only
  compiles record no verification and store nothing. The cold compile scores a
  compile with no warm start. `oracle_rank` is the executed option's rank by
  realized held-out score (ties by cost, equal to 1e-9 dollars), and `regret` is
  the best realized score minus the executed one. `compile_cost_avoided_usd` is
  the cold compile's cost minus the routed arm's compile cost. For the exact
  probe, the cold compile is Phase A's `default` compile.

Each routed row is one probe, with `route` columns: `probe`, `action`, `tier`,
`candidates`, `chosen_rank`, `oracle_rank`, `regret`, `compile_cost_avoided_usd`,
`fallback`, `explored`, `decision_ms` and `round`. Four bench additions are also
defaulted there:

- `codes`, the router's content-free codes;
- `installed`, the number of Phase A entries;
- `templates_ready`, the number of eligible Tier B templates with
  `r >= min_template_reliability`;
- `target`, the decision's target as one `h_` category of its full reference
  (origin, registry name and revision or library key, and artifact digest), or
  none without a target. Pass criterion 4 compares it.

`BenchRow.artifact_digest` holds the delivered or reused artifact's digest; the
router joins bench evidence on it (display only). A routed row's `cost_usd` and
`calls` are the executed action's only. Phase A compile, oracle and tuning-replay
spend counts toward the bound and the progress totals, never toward the row. The
Phase A `forge` and `template` arms' build spend is reported as it is for a forge
cell: the `exact` row carries their summed `forge` columns (`ForgeFields`, with the
Forge agent's `agent_cost_usd`, tokens and wall time), shown as `agent$` in the
`process` block and never folded into `cost $`. The bound adds `forge.max_usd` for
the Phase A `forge` arm. The row's
`reserved_usd` holds the whole cell's reservation on its `exact` row. The foreign
row's `spec_hash` and `floor_limit` are the foreign task's.

The bound (`_size_cell`) adds the Phase A arms, one cold compile for each
non-exact probe, and one warm-started compile per template candidate, up to
`max_candidates`. Under fake it is $0. A live run is bounded by `--max-usd` and
confirmed before any spend.

Guard: a routed row with no decision, or whose exact probe listed no candidate, is
written with `route_did_not_run`, mirroring `forge_did_not_run`. A routed run is
refused with `bench_variant_unknown`, before any spend, when the router module is
absent.

`--rounds R` replays the routed arm R times, each round in the order fixed by
`random.Random(f"{seed}:{repeat}:{round}")` (one round keeps suite order), with
one route log per bench. Rows carry `round`. `router_eval.learning_curve(rows)`
reports, per round and evidence class, the licensed rate, spend, mean regret,
exploration share and promote and rollback proposal counts. `router_eval.regret_table(rows)` sets every
arm (`routed:exact` and `routed:sibling`; the foreign probe is scored on another
task's floor) against the hindsight oracle over the `default`, `template` and
`forge` variants and the models present. Both are keyed by evidence class: a point
or cell holds one class, and a regret is measured against the best of its own
class only, never pooled across classes.

Summaries group by `(task_id, model, variant, probe)` and add route accuracy
(`chosen_rank == oracle_rank` where both exist), mean regret, mean cost avoided
and fallback rate. `bench_table` adds a `route` block with the columns `probe`,
`tier`, `action`, `rank/orc`, `regret`, `dCost$` and `rnd`. `compare
--pair-variants default:routed` pairs each `default` row with the routed arm's
`exact` probe of round 0, the one that measures the task's own spec.

Simulation (`architect.bench.router_sim`, $0, no I/O):

- `simulate(templates, installed, steps, seed, policy)` drives the real `route()`
  over an in-memory evidence index, with Bernoulli Stage 4 outcomes and a
  persisted exploration counter;
- `simulate_roles(variants, steps, seed, routing)` drives the real
  `RoleRouter.choose`.

The roles arm, `router_sim.roles_arm("pinned" | "evidence")`, replays the
committed fake role-outcome logs in `examples/bench/roles/*.jsonl` through the
real `RoleRouter`:

- a cheaper, non-inferior rewrite variant is switched to under `evidence`;
- a cheaper but worse inference variant is not;
- a thin Forge-agent log stays on the default with `role_evidence_insufficient`.

Under `pinned`, every choice is the default. The CLI runs it as `bench roles --policy
pinned|evidence [--json]` ($0, no provider). `bench run --variant routed --rounds R`
runs the routed arm, and a routed row with `route_did_not_run` makes `bench run` exit 1
with that code (the rows are kept), as `forge_did_not_run` does.

All of this is `synthetic_fake` on the fake provider. It proves plumbing and
decision determinism, not routing quality. The fake oracle scores every option
alike, so regret is 0 there by construction. A live routed run, which only an
operator triggers, reports the paired licensed rate, mean regret and mean cost
avoided with the evidence class shown. It has no pass or fail on quality.

## Statistics and verdicts

`compare` pairs rows by (task, model, variant, seed, repeat, size) and refuses
runs from different suite versions or digests, different tuning sizes, fake
rows over different page manifests (`bench_compare_mismatch`), and changed
presets unless `--allow-fingerprint-mismatch` (`bench_compare_fingerprint`).

Held-out scores are the licensing signal; the paired signal is the per-case
tuning bits, which are cases Stage 2 selected on (selection-biased, stated in
every compare table). The verdict is code: `regression` on a license flip, a
significant held-out loss, or a cost rise above 20% without a gain; `improvement`
on a significant paired win (McNemar exact, p < 0.05, at least 5 discordant
pairs), a held-out gain of at least 0.10 at n >= 20, or equal scores with at least
15% lower cost or wall time; `identical`; otherwise `inconclusive`. A run is a
`regression` if any model regresses: a change that helps one model and hurts
another is rejected.

Statistical power: 20 held-out cases give a 95% Wilson interval of about +-0.15
around 0.85. For small effects raise `--size 80` or `--repeats 3`; never decide
an `inconclusive` by judgment.

## Iteration protocol

1. Baseline: `bench run --provider fake --tier smoke --seed 7` (free), then
   `bench run --tier quick --profile P --seed 7 --label base-<date>`. Copy the
   rows to `examples/bench/results/<bench_id>.jsonl` and commit them.
2. Propose one preset change on a branch (instruction text, a parameter value, a
   component, an attach policy, a prompt-caching choice). Anything under
   `architect/presets/` changes `preset_fingerprint`; a new task, size, floor or
   generator changes `suite_digest` and needs a fresh baseline under a new suite
   version.
3. Mechanism gate (free): the smoke tier on the fake provider must pass every CI
   assertion (`python -m architect.bench.ci_assert <rows.jsonl>`). A change that
   fails here is not measured live.
4. Measure with the same seed, models, `--max-candidates` and size (`--repeats 3`
   when a repeat costs under $3): `bench run --tier quick ... --label cand-<name>`.
5. Decide with `bench compare <base> <cand>`. Before accepting a live
   `improvement`, one confirmation run with `--seed 11` must not be a
   `regression`.
6. Record the change, both bench ids, the suite version and digest, per-cell
   deltas with Wilson intervals, discordant counts, p-values, per-model and
   overall verdicts and the evidence labels in `docs/bench/decisions.md`.
7. Cadence: smoke on every push (CI); quick before a preset change merges and after
   any `prices.json` or adapter change (a pricing-only change must show identical
   scores with cost deltas only); full monthly; arc when the ARC harness or game
   presets change; probe before the first live run on any newly configured model.

## CI

The `bench-smoke` job runs the smoke tier on the fake provider with
`--variant default --variant noisy --variant template --variant forge` (the
`noisy` negative control plus the $0 Phase 9 arms; it installs the pinned Pi
packages for the forge arm, and a `forge_did_not_run` row fails the run step),
runs `python -m architect.bench.ci_assert` and then compares
the run with `examples/bench/suite-v1/smoke-baseline.jsonl` (`--expect
identical-scores`). The compare step is unconditional: a missing baseline fails
the job (it never skips), and the job uploads its rows so the first green Linux
run can be committed as that baseline (page manifests record the platform, so a
Windows run cannot serve as the Linux baseline). `ci_assert` checks: every `default` cell
licensed; every `noisy` cell unlicensed with `primary_floor_failed`; images sent
on every vision and pipeline cell and exactly one image per case run on
`digitizer-24` (first stage only); no `image_reservation_under_configured`,
`sandbox_*`, `schema_error` or `image_unresolved` failure; and `default` vs
`noisy` is a significant `regression` on `exam-clean-40`. The noisy oracle flips
a fixed quota per floor partition, so both negative-control assertions hold for
every seed, not by luck. The Windows job runs the same assertions on a
`default` and `noisy` smoke run. `ci_assert` has no forge- or template-specific
check. Those rows are held to the run step's `forge_did_not_run` guard, to the
image and forbidden-failure checks, and to the identical-scores compare. The
compare needs every cell on both sides to pair and the same preset fingerprint, so
a change to the variants or to `architect/presets/` needs a new baseline. Run the
workflow by hand for that: a run by hand starts only the `bench-baseline` job,
which runs the same smoke command on the Linux runner, checks the rows with
`ci_assert` and uploads `rows.jsonl` as the `smoke-baseline` artifact, the file to
commit as the baseline. When the compare fails, bench-smoke's log prints these
steps.

Phase 10 adds `--variant routed` to that run, and a second run with identical
flags into `.architect/bench-repeat` (a routed cell's Phase A installs every
selected arm, so only identical flags make the same measurement). `ci_assert` then runs with
`--repeat <second rows> --simulate`. When routed rows are present, it checks
these pass criteria:

1. The routed licensed rate is at least the default licensed rate on the pairs of
   `compare --pair-variants default:routed`.
2. The exact probe reuses in every cell whose Phase A installed an entry. The
   foreign probe compiles in every cell. The sibling probe warm-starts whenever an
   eligible template with `r >= 0.3` was listed.
3. No routed row has `route_did_not_run`, and every routed row carries route
   columns and an evidence label.
4. The two identical runs make the same sequence of (action, target, chosen rank)
   decisions, with the tier and candidate count too. The target is the row's
   content-free `route.target` category.
5. The simulation, over 2000 seeded trials per scenario, holds these bounds:
   - exploration share at most `1/explore_every + 0.01`, in a scenario where an
     untried template is always listed and the cap never binds (the check also
     fails when that scenario's share falls below `1/explore_every - 0.01`, since
     a starved scenario would pass any router);
   - the daily exploration cap is never exceeded, in a scenario where the cap
     binds every day (the check fails when it never reaches the cap);
   - no evicted, unlicensed or consumed candidate is chosen;
   - a false promote-proposal rate of at most 0.10 at equal pass rates
     p in {0.5, 0.8, 0.95} (n = 30);
   - a role variant 0.10 worse is chosen in at most 5% of trials under
     `evidence` (`min_samples = 20`);
   - the default is always chosen under `pinned`.

A routed installed reuse makes no model call, and a foreign probe measures
another task, so both are exempt from the images-sent check. The smoke baseline
holds the routed rows too, and it has to come from the Linux runner: page manifests
differ by platform (the three image tasks), so rows from another platform never pair
with the Linux rows (`bench_compare_mismatch`). The `bench-baseline` job in CI makes
a new one.

## Honesty rules

- Every number is printed next to its evidence label and every score next to its
  floor origin; suite floors are synthetic by construction.
- CLI-provider money is an API-equivalent estimate, not a bill; local models cost
  $0 and are measured in wall time.
- Image reservations are ceilings with a `basis`; `assumption` ceilings stay
  flagged until measured rows replace them; `image_tokens_estimate` is labeled an
  estimate wherever it is shown.
- Stage 4 is the only licensing authority; the bench never constructs or edits a
  test report, re-reads `licensed` from disk (the `arc-preview` row takes it from
  the manufacture summary instead), touches only the tuning partition outside
  Stage 4, and never installs into an operator registry: `bench arc` installs its
  manufactured harness only into a throwaway registry inside the cell workspace,
  deleted with it, and a routed cell installs its Phase A arms only into its own
  throwaway registry, deleted with it. A trip of the held-out guard outside
  Stage 4 stops the run with `bench_held_out_access`; it is never recorded as a
  disk or availability failure.
- Paid runs need explicit consent: `--yes`, or `y` at `Continue? [y/N]` on an
  interactive console. Anything else is `bench_cancelled` before any spend.
- ARC rows keep `score_source`, `model_binding` (`offline` for local models,
  `online_preview` for cloud models) and `compliance: unverified`; fixture-agent
  scores are pipeline checks; nothing is a submission or a benchmark result.
- Forge counts (`meas`, `subs`) are tuning evidence, never a license. Build-time
  agent cost is shown apart from `cost $`. A row where Forge did not run is
  `forge_did_not_run`, never a Forge result. 0.0-vs-0.0 ARC results are reported
  with their process metrics, never omitted. ARC Forge evidence is labeled
  "local-offline-preview, not a benchmark or compliance claim".
- Cost figures in the contract and here are price-table projections until
  measured rows replace them.
- Routing orders licensed versions; it never licenses. Routed and simulated
  numbers on the fake provider are "plumbing, not quality", and every routed
  number carries its evidence class.
