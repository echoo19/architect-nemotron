# Architect Forge: end-to-end map for a dedicated desktop UI workspace

Repo: `{repo}` (branch `master`, HEAD `b5f8f02`). This is read-only research. Every citation is `path:line` relative to the repo root. Where a doc and the code disagree, the code wins (most docs say this about themselves, e.g. `docs/interfaces/phase9-forge.md:3`, `phase12-agent-cli.md:3-4`).

---

## 0. What "Forge" means: three layers that share one name

The name "Forge" covers three different things. A UI has to separate them, because the CLI mostly doesn't.

| Layer | What it is | Where |
|---|---|---|
| **Forge (engine)** | An opt-in Pi coding-agent session that takes the **Stage 3 slot** of the compiler. It writes `harness.py` in sandbox language v2 and reaches trusted code only through 5 tools. | `architect/compiler/forge.py`, `forge_tools.py`, `forge_workspace.py`, `forge_templates/`; contract `docs/interfaces/phase9-forge.md` |
| **Forge delivery modes** | `forge run` builds, licenses, runs once and expires. `forge integrate` builds, licenses, installs and exports a package into a repo. | `architect/forge_modes/`; contract `docs/interfaces/phase10-router.md` §12 |
| **Forge mode (CLI)** | The conductor's "build" mode (shown to the user as "forge"). Its steps are: interview, plan card, floor drafting, compile (Forge on by default), install, and then the destination (run it here or into software). | `architect/agent/build.py`, `commands_forge.py`, `export.py`; contracts `phase12-agent-cli.md` §5 and `phase13-cli-complete.md` §7 |

The internal mode value is `"build"` and the label is "forge": `MODE_NAMES = {"normal": "normal", "forge": "build", "build": "build"}` (`architect/agent/commands.py:62`), and `Mode = Literal["normal", "build"]` (`architect/agent/conductor.py:40`). In build mode the conductor gets an extra prompt telling it to call `make` (`conductor.py:68-71`, `:182-183`).

Fixed properties that every layer keeps, and that the UI must never appear to weaken:
- **Stage 4 is the only thing that licenses a harness.** Forge never sees held-out data, and never sees a Stage 4 score, report or `verification_id` (`phase9-forge.md:39-40`, `:908-936`).
- **Budgets are dollars and seconds only.** Tokens are metered but never capped (`phase9-forge.md:41`; `docs/specs.md:116-127`).
- **Telemetry, rows and `forge.json` are content-free:** counts, dollars, seconds, codes, digests (`phase9-forge.md:45`).
- **Money from CLI providers is "API-equivalent, not a bill"** (`phase9-forge.md:47`).

Live status (from `PROGRESS.md`):
- A Forge harness licensed in "about a minute (12 agent calls)" on a synthetic fixture (`PROGRESS.md:141`).
- Live build-mode runs: an invoice extractor licensed at 1.00/0.95 on 6 held-out SYNTHETIC cases for $0.97 (`PROGRESS.md:154`), and a support classifier at 1.00/0.85 for $0.81, exported into a repo (`PROGRESS.md:164`).
- ARC Stage B is incomplete, and the best ARC score is still 0.0 (`PROGRESS.md:134`, `:147`).

---

## 1. What the user provides, and how the Spec is formed and approved

### 1.1 The Spec (the unit of everything)

`Spec` is defined at `architect/spec/__init__.py:104-131`. Its fields:
- `task`
- `input_schema` and `output_schema` (JSON Schema)
- `floor: FloorDefinition` and an optional `secondary_floor`
- `budget: Budget(cost_usd, compute_s)` (`:70-80`; per-run defaults $0.10 and 60 s)
- `model_target` (a model name, or the literal `"cheapest that passes"`, which means "use the profile ladder")
- `lifecycle` (`expire` or `install`)
- `domain`, `preset_hint`, `execution_config`, `tools`
- `pipeline` (at most 16 `PipelineStep`s, `:94-101`)
- `provenance` (per field: `inferred` or `user`)

`FloorDefinition` (`:84-91`) holds `kind`, `limit` (0..1), `dataset` (a dataset id), `minimum_samples` (default 30), and `rubric`, `judge_model` and `calibration_dataset` for judges.

There are 4 floor kinds (`docs/floors.md:196-201`):

| Kind | Passes when |
|---|---|
| `held_out_tasks` | score >= limit |
| `human_edit_rate` | score <= limit |
| `llm_judge` | score >= limit |
| `held_out_game_score` | score >= limit |

**`spec_hash`** is the digest of the Spec JSON without `provenance` (`spec/__init__.py:129-131`). Every edit changes it.

**The partition rule is fixed.** Case ids are sorted; even positions are tuning and odd positions are held-out (`docs/floors.md:205-209`; `architect/floors/base.py:111`). The compiler gets only `floor.tuning` (`base.py:144`). Held-out cases are reachable only through Stage 4's private capability (`base.py:148`; `architect/floors/_access.py`).

Consequence for the UI: 12 drafted cases give 6 tuning and 6 held-out. With a limit of 0.95, a single held-out miss fails the license (the live builds above were all "on 6 held-out SYNTHETIC cases").

### 1.2 What the user provides in each surface

| Input | Agent CLI Forge mode (`agent/build.py`) | Step-by-step session (`architect session`) | Non-interactive CLI |
|---|---|---|---|
| Task description | Free text to `/make`, `/forge <text>`, or any message in forge mode | `describe <text>`, or prose then `infer?` | positional `request_or_spec` |
| Destination | Interview Q1: "run it here" or "into software" (+ package path) (`build.py:126-135`, `:608-644`) | n/a | `forge run` vs `forge integrate --repo --into` |
| Inputs, example, outputs, correctness, limits | Interview fields `FIELDS = (location, inputs, example, outputs, correctness, limits)` (`build.py:122`). At most 5 questions, asked only for fields the model marked uncertain (`:647-680`). | Edit the Spec: `set <path> <value>` | Spec JSON file |
| Quality floor (limit) | Inferred. Override with `/floor <limit>` (0..1 or "90%") (`commands.py:605-624`; `build.py:316-320`) | `set floor.limit` | in the Spec |
| Floor cases | Uses a configured floor if the dataset matches. Otherwise `/floor add <file>`. Otherwise **12 drafted SYNTHETIC cases** (`build.py:124`, `:929-983`). Image-input agents refuse drafting with `image_floor_needs_cases` (`:911-950`). | FLOOR state: `use <path>`, `add`, `paste`, `draft [n]` (default 20), then **per-case review** `review <id> ok/drop/expected` (`docs/usage.md:300-343`) | profile `floors` map; `forge run --cases FILE.jsonl` (at least 8 rows, `{case_id, task, expected}`) |
| Per-run budget | Inferred. `/budget run <usd> <s>`. Raised automatically to fit 2 model calls (`build.py:819-839`). | `set budget.cost_usd` (auto-lift on session-written profiles) | Spec |
| Compile wallet | `/budget compile <usd> [min]` (`commands.py:659-663`). Default is the profile's `compile.compile_budget`. | `budget compile <usd> <s>` | profile |
| Forge budget | `forge_usd` / `forge_s` overrides exist (`build.py:352-361`), but **no slash command sets them** (see §7) | `forge budget <usd> <minutes>` | `--forge-usd`, `--forge-minutes` |
| Engineer (Forge vs preset) | Override `engineer` = `forge` or `preset` (`build.py:362-374`); also **no slash command** | `forge on/off` | `--forge` |
| Model | `/model <name>` sets both the conductor model and the next compile (`commands.py:668-703`); default "cheapest that passes" over the ladder | `use model <id>` | Spec `model_target` / profile |
| Install name | Override `name` (kebab-case); default derived from the request (`build.py:989-1004`, collision handling `:1011-1026`) | `install <name> <hash8>` | `compile` installs under the spec_hash (`cli.py:462`); `integrate` uses `feature-<hash16>` |

Profile-level settings (`docs/usage.md:557-564`):
- `forge_agent` (an adapter config)
- `compile.forge` (`ForgeSettings`; all fields at `architect/compiler/forge_settings.py:22-45`, documented at `phase9-forge.md:569-597`)
- the floors map, `smoke` paths, `routing` and `roles`

### 1.3 How the Spec is formed

**Agent CLI build mode** (`build.py:1534-1648`, `_build`):
1. **Interview.** One metered "build_interview" call proposes every field from the request, a listing of the working directory and `ARCHITECT.md` (`_propose`, `:561-596`). Defaults are at `:204-211`. Then the destination question (always asked first), then up to 5 uncertain fields. Enter accepts a default and `/skip` accepts all remaining defaults (`:647-680`).
2. **Stage 0.** `infer_spec(source, adapter, runtime, user_fields)` (`build.py:1554-1557`; `compiler/inference.py`). The pinned user fields are `lifecycle=install`, `model_target`, and any budget or floor overrides (`_user_fields`, `:707-727`).
3. **Shape fixes.** Bare property maps are normalized into JSON Schema. A missing schema is built from the interview's comma list of field names (`_shape`, `:762-779`).
4. **Budget lift** to fit 2 calls of the slowest bound model (`_lift_budget`, `:819-839`; per-provider seconds `:796-798`).
5. **Floor** (`_floor`, `:929-983`): a configured dataset, the user's file, or drafting. A drafted floor is saved to `<workspace>/floors/<dataset>.json`, registered **into the profile file**, and services are reloaded (`:974-977`).
6. **Blocker checks** (`:1601-1615`): `blockers()` from `session/edit.py`; `forge_disabled` if the floor kind is not `held_out_tasks`; `model_unpriced`.
7. **Plan card**, then compile.

**Session**: READY → INFERRING → REVIEW (edit) → FLOOR → approve → COMPILING → RESULT (`docs/usage.md:225-265`).

**HTTP**: `POST /compile {request?|spec?, approved}` (`architect/server.py:170-176`).

### 1.4 Approval: by hash, but different on every surface

- **Core rule.** An INSTALL-lifecycle Spec returns `approval_required=True` and stops before tuning unless `approved=True` (`compiler/__init__.py:457-464`). EXPIRE Specs need no approval (`docs/specs.md:144-146`).
- **Session.** You must type `approve <hash8>` for the Spec currently on screen. A stale token gives `stale_approval_token`. `install <name> <hash8>` follows the same rule (`docs/usage.md:266-277`).
- **Non-interactive CLI.**
  1. `compile` without `--approved-spec` writes `<workspace>/proposals/<spec_hash>.json` and exits 0 with `approval_required: true, accepted: false, proposal_path` (`cli.py:412-425`, `:448-452`).
  2. The operator reviews it and re-runs with `--approved-spec reviewed.json` (`docs/usage.md:428-440`).
- **Agent CLI build mode.** The plan card footer shows `spec <hash8>`. Answering "yes" (default; Enter) to "Compile this agent?" **is** the approval, and `approved=True` is passed for exactly that Spec (`build.py:1619-1636`, `:1339-1342`; docstring `:16-18`). There is no typed hash.
- **HTTP.** Resubmit the exact `spec` with `approved: true`; a plain `request` cannot carry approval (`docs/specs.md:152-153`).
- **Separate consents.**
  - Forge spend: the summed-cap line plus `Continue? [y/N]`, or `--yes` (`cli.py:222-270`; `session/forge.py:94-102`).
  - Export: before anything is written into a repo (`agent/export.py`; `forge_modes/integrate.py:153-159`).
  - Registry `budget_increase`: an extra yes (`build.py:1430-1452`).
  - In the agent CLI, any single tool call bound above $0.50 when in `ask` permission mode (`phase12-agent-cli.md:73-74`).

---

## 2. The stages: what happens, what the user sees, events, durations, costs

### 2.1 Stage map (code: `Compiler._compile_call` and `_compile_spec`, `architect/compiler/__init__.py:397-691`)

The outer wallet is `compile_budget`, plus `forge.max_usd`/`max_s` when Forge is enabled (`_outer_budget`, `:274-287`). Everything runs under `asyncio.timeout(wallet.remaining_s)` (`:416-418`). Exhausting it gives the failure `compile_budget` (`:478-494`).

| Stage | Happens | Code | Shown in the agent CLI |
|---|---|---|---|
| (interview) | 1 metered proposal call | `build.py:561-596` | `Progress("interview: reading the request with <model> ...")` (`:585`) |
| 0 infer | One model call → Spec | `compiler/__init__.py:433-444`; `build.py:1552-1557` | `Progress("stage 0 infer spec: one call to <model> ...")` |
| (floor) | Draft 12 cases (1 metered call, a second when the reply does not parse) or load a file | `build.py:964-982` | `Progress("floor: drafting 12 cases ...")`, then `SubagentActivity("floor", "'<ds>' configured: N cases drafted, SYNTHETIC (t tuning / h held-out)")` |
| (route) | Phase 10 router; may `reuse_installed` (skips 1-4) or `warm_start` | `compiler/__init__.py:446-468` | If reused: `Notice("an installed licensed agent already does this: <name>")` (`build.py:1663-1676`) |
| 1 select | Rule-based preset selection, plus a library warm start lookup | `select_preset`, `compiler/__init__.py:556-567` | `Progress("stage 1 select preset: C calls, $X")` |
| 2 tune | Grid or successive halving over at most `max_candidates` (default 16) parameter sets. Each candidate is persisted as a new unlicensed `artifacts/<spec_hash>/<n>/` and measured on **all tuning cases** | `compiler/tuning.py:217-329` | `Progress("stage 2 tune: ...")`; no per-candidate events |
| 3 forge / rewrite | Forge if `forge.enabled and smoke_factory and (trigger=="always" or not tuned.passed)`, else the classic `rewrite()` when the floor failed | `compiler/__init__.py:592-638` | `Progress("stage 3 forge: ...")` plus a live stream of Forge tool calls (§6.2) and a final `SubagentActivity("forge", "N submissions, M measures, K sub-harnesses, agent $X")` (`build.py:1322-1330`) |
| 4 verify | One run on the held-out partition; writes the HMAC-signed `TestReport` | `compiler/verification.py:55-200`; signing `architect/_authority.py:47-62` | `Progress("stage 4 verify: ...")` |
| (result) | Re-load the license from disk; install; destination | `build.py:1651-1769` | Result `PlanCard` plus a `Notice` |

The model ladder loop: with "cheapest that passes", Stages 1-4 repeat **for each ladder model** (`compiler/__init__.py:556`). The licensed winner is the cheapest by `(expected cost, component_count, tokens)` (`:659-668`, `_cost_key` `:694-698`). The stage listener therefore fires 1, 2, 3, 4, 1, 2, ... A UI needs a "model k of n" dimension that the CLI never shows.

Stage labels are inconsistent: `STAGE_NAMES[3] = "rewrite"` but renamed to "forge" when Forge is on (`build.py:156`, `:1259-1267`). The session's `STAGE_TEXT[3]` is always "stage 3 rewriting" (`session/observe.py:44-49`).

### 2.2 Inside Stage 3 when Forge runs (`compiler/forge.py:314-446`)

1. **Verification cap check.** If `library.verification_count(spec_hash) >= max_verifications_per_spec` (default 10), the failure is `forge_verification_cap` and nothing runs (`forge.py:365-368`).
2. **Two temp dirs.** A workspace the agent sees, and a private dir holding `candidates/` and `spawn_datasets/` that the agent cannot read (`:373-381`).
3. **Workspace files** (`forge_workspace.py:276-291`):
   - `harness.py`: the incumbent, or a template
   - `LANGUAGE.md`
   - `DESIGN.md`, the "specialized harness" playbook (`compiler/forge_design.py`)
   - `SPEC.md`
   - `TUNING.md`: tuning inputs, plus expected values in `per_case` mode
   - `BASELINE.json`: the Stage 2 best measurement
   - `templates/*.py`
   - `NOTES.md`
4. **Nested Forge wallet**, `Budget(max_usd, max_s)` (`forge.py:397-402`).
5. **Mode:**
   - `template`: submit the default template once with no agent (`:403-405`; the default is `game_explorer`, `vision_two_pass` or `extract_verify` per `phase9-forge.md:659`).
   - `agent`: a real Pi 0.73.1 session via `Runtime.build(..., preset="forge_agent")` with tools read, write, edit and the 5 `forge_*` tools; no shell (`forge.py:200-248`). The agent prompt is `FORGE_INSTRUCTIONS` (`forge.py:59-90`).
6. **The five tools** (`compiler/forge_tools.py`; contract `phase9-forge.md:753-906`):

   | Tool | Cost | Default cap | Returns |
   |---|---|---|---|
   | `forge_check` (`:604`) | $0 | 200 | line, hint, stats (nodes/8000...), lint |
   | `forge_dryrun` (`:661`) | $0, scripted replies | 100 | prompt shapes, output head, fuel |
   | `forge_measure` (`:774-870`) | play-model spend | 24 | aggregate `measurement` (score, floor_limit, floor_passed, budget_passed, mean cost/tokens/calls, max_compute_s, sample_count, failures), `per_case` mode, `cases[]` (index, passed, score, failure, sandbox_line, calls, cost, output/expected excerpts, game detail), `baseline` |
   | `forge_submit` (`:876-1010`) | play-model spend | 6 | `candidate`, `accepted`, `best_so_far`, measurement, smoke, `submissions_left`, code (`not_better`, `tuning_failed`, `forge_literal_leak`...) |
   | `forge_spawn` (`:1153-1320`) | nested child compile | 3, depth 1 | child licensed, preset, tuning, verified, `case_origin`, `call_as` |

   Every result ends with `left: {usd, seconds, checks, dryruns, measures, submissions, spawns}` (`ForgeSession.left`, `forge_tools.py:328-341`).
7. **Acceptance rank.** `forge_rank = (not floor_passed, not budget_passed, smoke failed, -score, mean_cost, subharnesses+components, nodes)`; lower is better (`forge_tools.py:359-371`). The starting incumbent is the Stage 2 best, so Forge can never be worse on tuning.
8. **Session end.** The agent's final text, `pi_max_turns` (200), the wallet running out, submissions exhausted, or cancel (`phase9-forge.md:690-699`).
9. **Promotion.** The best accepted candidate is copied to a new `artifacts/<spec_hash>/<n>/` with `subharnesses/`, `forge.json` is written, and the verification counter is incremented (`forge.py:251-266`, `:414-445`). With no acceptance, the result is `forge_no_submission` and the Stage 2 best goes on to Stage 4.
10. **Stage 4** runs once on the promoted candidate (`compiler/__init__.py:639-647`). Its result never goes back to the agent.

### 2.3 Durations and costs (defaults)

| Item | Default | Source |
|---|---|---|
| Per-run budget | $0.10 / 60 s (Spec default); lifted to 2 calls in build mode | `spec/__init__.py:73-74`; `build.py:819-839` |
| Compile wallet | $10 / 120 s (package `config.json`); **$5 / 1800 s** in a session-bootstrapped profile | `compiler/config.json`; `session/bootstrap.py:231` |
| Forge cap | $5 / 3600 s, on top of the compile wallet | `forge_settings.py:28-29`; `compiler/__init__.py:274-287` |
| Spawn | min(spawn_max_usd $2, 25% of what's left) | `forge_settings.py:38`; `phase9-forge.md:957` |
| Conductor session wallet | **$25 / 60 min** (code). The Phase 12 doc says $5. | `agent/conductor.py:106-107`; `agent/config.py:78`; vs `phase12-agent-cli.md:52-54` |
| Seconds per call, used in budget lifting | claude-code 60, codex 90, ollama 120, anthropic/openai 30 | `build.py:796-798` |
| Observed live | Forge licensed in about 1 min with 12 agent calls; whole builds $0.81-$0.97 | `PROGRESS.md:141`, `:154`, `:164` |

Cost categories (keep them separate in the UI):
- **build or agent cost:** the Forge agent's own model, recorded in `forge.json` `agent_cost_usd`
- **compile cost:** the play model during tuning, measure, submit and Stage 4
- **play or run cost:** per delivery

`forge run` reports all three (`forge_modes/run.py:606-607`; summary line `:191-209`). Bench keeps agent cost out of `cost_usd` (`phase9-forge.md:1018`).

---

## 3. Forge modes, destinations, outputs and artifacts on disk

### 3.1 The CLI destinations (Phase 13): "Run it here" vs "Put it into software"

`DESTINATIONS = ("run it here", "into software")` (`build.py:126`). The destination is always the first interview question (`:608-644`).

- **Run it here.** The agent is installed in the registry and becomes usable through:
  - `/run <name> <input>` (plain text becomes the single text field; `commands.py:417-437`)
  - `uv run architect run <dir> --profile <p> --input '<json>'`
  - `/serve`: a managed `python -m architect.server` on 127.0.0.1:8765 with a fresh bearer token (`commands_forge.py:404-477`)

  Caveat: the server runs only agents it licensed itself, so each agent needs one `POST /compile {spec, approved:true}` first (`commands_forge.py:390-396`; `docs/usage.md:59-63`).

  The next-step text comes from `_run_here_step` (`build.py:1772-1784`).
- **Into software.** After a licensed install, `export_agent` (`agent/export.py:343-491`) does the following:
  1. Stages the package with `forge_modes.export_package`.
  2. Emits a `PlanCard("Export plan: <rel>/")`.
  3. Asks `ui.approve("export", ...)`.
  4. Places it with `place_package` and re-verifies.
  5. Emits a `PlanCard("Exported: ...")` with Python and HTTP snippets and the test command.

  The default target is `agents/<package>/` (`DEFAULT_INTO`, `export.py:72-77`). `/export <agent> [path]` does this later for any installed agent (`commands_forge.py:154-200`).

### 3.2 Non-interactive Forge delivery modes (`architect/forge_modes/`)

**`forge run`** (`forge_modes/run.py:385-645`). Builds an ephemeral harness with Forge forced on (`trigger=always`) and lifecycle `expire`, then delivers once if Stage 4 licensed it, then discards the code.
- What stays on disk: `report.json`, `spec.json`, `config.json`, `forge.json`, `subharnesses/`. Removed: `harness.py`, `pi-pack.json`, `preset.json`, `rewrite.diff` (`runtime/ephemeral.py:17-32`).
- JSON result `ForgeRunResult` (`run.py:119-138`):
  - `output`, `spec_hash`, `artifact_digest`, `licensed`, `stages`, `route`, `reused`
  - `forge_summary`: checks, dryruns, measures, submissions, spawns, agent_cost_usd
  - `floor_origin`, `build_cost_usd`, `compile_cost_usd`, `run_cost_usd`, `codes`, `run_id`
- `--cases FILE.jsonl` binds the cases into the spec hash: the dataset becomes `cases:<sha16>` (`forge_modes/cases.py:47-49`).

**`forge integrate`** (`forge_modes/integrate.py:244-466`). Four gates in order:
1. **Approval.** Without `--approved-spec`, only Stage 0 runs and a proposal is written.
2. **Forge spend consent.**
3. **Stage 4 license**, then install as `feature-<spec_hash[:16]>` (`:217-236`).
4. **Export consent.** The line is `export: <pkg> (<n> files, <kb> KB) -> <into>/<pkg>; feature flag OFF; human review required` (`:153-159`).

Placement is one of `--stage-only`, `--package-only` (host copy), or the default FeatureBuilder/Pi path with a single-use `place_harness_package` tool (`place_tool.py:41-86`). The result is `IntegrateResult` (`:102-118`): `licensed`, `name`, `revision`, `spec_hash`, `human_review_required`, `feature_flag_required`, `export{package_path, files, bytes, manifest_sha256, evidence_label, placement}`, `codes`, `build_cost_usd`, `compile_cost_usd`.

**`/forge arc [--run] [--model M]`** (`build.py:1897-2013`). Explains, or (only with `--run` plus an explicit yes plus a play model plus the `arc` extra installed) runs `architect.games.public.manufacture` in a temp workspace with a throwaway signing key. It defaults to Forge `template` mode. The local-GPU rule applies (`commands_forge.py:108-120`).

### 3.3 On-disk layout

Workspace: the CLI uses `~/.architect`; override with `--workspace` or `ARCHITECT_WORKSPACE` (`phase12-agent-cli.md:209-211`). Control-plane subdirectories are listed at `profiles.py` (`WORKSPACE_STATE`): `artifacts`, `library`, `registry`, `telemetry`, `proposals`, `server`, `routes`, `exports`.

```
<workspace>/
  session-profile.json            # profile written by first run (models, floors map, compile, forge...)
  config.toml, credentials.json, model-prices.json, signing.key (32 random bytes; HMAC key)
  floors/<dataset>.json           # drafted/added cases (metadata.synthetic, origin, drafted_by_model...)
  proposals/<spec_hash>.json      # Stage-0 proposals awaiting approval (cli.py:412-425)
  artifacts/<spec_hash>/<n>/      # every Stage-2 candidate + promoted Forge version; n never reused (tuning.py:144-156)
      harness.py  spec.json  config.json  report.json  [forge.json]  [subharnesses/<name>/...]
  registry/registry.sqlite3       # revisions, current, observations, slots
  registry/versions/<sha256(name)[:32]>/<revision>/artifacts/<spec_hash>/<artifact_version>/...  (copied, re-authenticated)
  library/library.sqlite3         # templates(key, compatibility, successes, failures, evicted, shape), forge_verifications(spec_hash, count)
  library/snapshots/<artifact_digest>/
  routes/decisions.jsonl outcomes.jsonl roles.jsonl ratings.jsonl index.sqlite evidence.sqlite
  exports/<artifact_digest[:16]>/<package>/   # staged export packages
  telemetry/ (runs.jsonl, content-free TelemetryRow)
  sessions/<id>.jsonl, history, serve/server-<port>.log
```

Sources: registry `registry/__init__.py:221-253`, `:294`, `:320-321`; library `library/__init__.py:118-149`, `:225`; routes `routing/log.py:133-193`; exports `forge_modes/export.py:295-347`.

**Artifact identity and license**
- `artifact_digest` = sha256 over `{spec_hash, config, sha256(harness.py)}` (`spec/__init__.py:249-259`). When a Forge binding is present, it is part of `config` (`phase9-forge.md:375-381`).
- `licensed` (`spec/__init__.py:261-313`) re-checks the HMAC, spec_hash, digest, `passed` with no failures, the floor and secondary floor, budget fit and component count.
- Signing is HMAC-SHA256 with the key at `$ARCHITECT_SIGNING_KEY_FILE` (default `.architect/signing.key`) (`_authority.py:24-62`). Only `compiler/verification.py:33-35` signs.
- Editing any file revokes the license. The license is host-attested: a package verifies only on the host that holds the key (`agent/export.py:214`, `:486`).

**`TestReport` fields** (`spec/__init__.py:134-155`):
- `spec_hash`, `artifact_digest`, `passed`
- `floor_score`, `floor_limit`, `floor_kind`
- `tokens`, `cost_usd` (max per run), `compute_s`, `component_count`
- `failures`, `verification_id`, `signature`
- `secondary_*`, `held_out_count`, `mean_cost_usd`

**`forge.json`** has a fixed, content-free key set (`forge.py:96-117`, `:269-311`):
- `language`, `mode`, `agent_model`, `play_model`
- `checks`, `dryruns`, `measures`, `submissions`, `accepted`, `spawns`
- `agent_cost_usd`, `agent_tokens_in`, `agent_tokens_out`, `wall_s`
- `harness_nodes`, `harness_bytes`, `stage4_calls`, `verifications_for_spec`
- `notes[{chars, sha256}]`, `children[{name, artifact_digest, case_origin, licensed}]`

**Harness design text.** A Forge harness states its design in its module docstring. `harness_design(dir)` reads it with `ast.get_docstring`, capped at 1200 characters (`agent/tools.py:373-381`). `/report` and the result card show it.

**Export package** (`forge_modes/export.py:350-414`):
- `artifact/` (the full artifact plus `forge.json` and `subharnesses/`)
- `schemas/input|output.schema.json`
- `EVIDENCE.json` and `EVIDENCE.md` (scores, floor origin, Forge counts, children, `report_evidence`)
- `README.md`
- `tests/test_<pkg>.py` (7 tests)
- `__init__.py`: `PIN`, `FLAG="ARCHITECT_FEATURE_<HASH8>"`, `enabled()`, `verify()`, `run()`
- `MANIFEST.json` (sorted path → sha256)

The shim class `ExportedHarness` is at `forge_modes/shim.py:33-84`.

### 3.4 Listing, inspecting, running and exporting later

| Want | CLI / slash | Python |
|---|---|---|
| List installed | `/agents` (`commands.py:401-414`, via `tools._agents`, `tools.py:327-348`); `architect registry --workspace W` | `Registry.list()` → `RegistryEntry{name, revision, spec_hash, artifact_version, directory}` (`registry/__init__.py:35-42`, `:406-412`) |
| One agent | `/report <agent>` (spec summary, report, design; `tools.py:351-370`); `architect status --workspace W <name>` | `Registry.status(name)`, `get(name, rev)` → `HarnessArtifact` |
| History / rollback | session `rollback <name> [rev]` only; **no CLI command** | `Registry.history(name)`, `rollback(name, revision)` (`:435-484`) |
| Drift | (session `info`) | `Registry.drift(name)` → `no_window`/`stable`/`drifting` (`:443-463`) |
| Forge report | `architect forge report <dir> [--json]` | `session.forge.read_forge_record(dir)`, `forge_report_table(record)` (`session/forge.py:203-280`) |
| Run | `/run`, `architect run <dir> --input JSON`, HTTP `POST /run` | `Runtime.run(artifact, input, adapter)`; tool `_run_agent` (`tools.py:384+`) |
| Recompile | `/recompile <agent>`; `architect recompile <name>` | `compiler.compile(incumbent.spec, approved=True, allow_reuse=False)` (`commands.py:457-502`) |
| Export | `/export <agent> [path]`; `forge integrate` | `agent.export.export_agent(...)`; `forge_modes.export_package` / `place_package` |
| Verify export | `architect export verify <pkg> [--profile] [--reverify] [--yes] [--json]` | `verify_export(dir, registry=)` → `ExportCheck{integrity, pins, children, licensed, registry_match, codes}`; `session.exports.reverify(...)` |
| Library | `/library` (reads `library.sqlite3` directly; `commands.py:505-531`) | `HarnessLibrary.candidates/lookup/verification_count` |

Install and replace rule (`registry/__init__.py:331-394`):
- The candidate must be licensed with an INSTALL lifecycle.
- It must **strictly beat** the incumbent on `(floor, secondary, -cost, -components)` with identical task fields; otherwise `not_better`.
- Its budget may not rise without `allow_budget_increase`; otherwise `budget_increase`.

---

## 4. Router and versions

Phase 10 contract: `docs/interfaces/phase10-router.md`. Code: `architect/routing/`.

**Task signature** (`routing/signature.py`). Hashes are sha256[:32] of canonical JSON:
- `slot` (`:83-93`): normalized task, both schemas, domain, tools, floors. Same task means same slot.
- `shape` (`:94-102`): domain, schema sha, tools, pipeline.
- `family` (`:103-109`): domain plus the schema skeleton.
- `tokens` for Jaccard similarity.

**Candidates** (`routing/evidence.py:395-432`):
- installed registry revisions, matched by slot, shape or family
- library templates by shape
- a virtual "compile" fallback

**Hard filters**, in order (`router.py:245-271`): `route_unlicensed`, `route_consumed`, `route_evicted`, `route_model_unconfigured`, `route_budget`, `route_drift`, `route_synthetic_excluded`.

**Fit tiers** (`signature.py:124-136`):
- A: slot equal, F = 1.0.
- B: shape equal, F = 0.5 + 0.4·J + 0.1·[same floor kind].
- C: family equal, F = 0.2 + 0.2·J.

**Evidence per candidate** (`evidence.py:85-119`, `:530-604`):
- `m = score - limit`, sign-adjusted for `human_edit_rate`
- `m_lcb = m - 0.5/sqrt(n)`
- `r` = template reliability (Wilson lower bound of successes)
- `h` = human-rating Wilson bound
- cost, measured or bound latency, `k` (components)
- labels such as `stage4_signed`, `synthetic_fake`, `live_api_equivalent`

**Decision** (`router.py:305-483`):
- A Tier A row becomes `reuse_installed` **only if granted** (`--reuse-installed`, `route --run --yes`, or `routing.reuse_origins`). Otherwise it is advice with the code `route_reuse_not_granted`.
- Else Tier B with r >= 0.3 → `warm_start`. Stages 2 and 4 still run.
- Else `compile`.
- `refuse` when no model is configured.
- Extra codes include `route_forge_suggested`, which is advice only and never arms Forge (`:438-451`).
- Exploration is off by default (`:411-434`).

**Explanation table** (`render_decision`, `router.py:789-831`):
- Columns: tier, candidate, model, F, m_lcb, r, $cost, lat_s (`m`/`b`), k, evidence, codes.
- Then a `route:` line and `proposal:` lines (promote → `architect recompile ...`; rollback → session `rollback <name> <R>`; `proposals.py:44-114`). Proposals never execute.

**Logs.** `workspace/routes/*.jsonl` holds `RouteRecord` (`log.py:66-89`) and `OutcomeRecord` (licensed, unlicensed, delivered, failed, discarded, cancelled, plus costs; `:92-100`).

**Roles router** (Architect's own models: inference, decomposition, rewrite, forge_agent, floor_draft). The policy is `pinned` (default, byte-identical) or `evidence`, with non-inferiority on Wilson bounds (`routing/roles.py:184-199`, `:381-450`). Under `evidence`, Forge consent first prints `forge agent: <model> (mode M, turns N)` (`cli.py:240-250`; `forge_modes/run.py:212-214`). See it with `architect roles --profile P [--json]`.

**Versions vocabulary.** There are three counters, and the UI should label them clearly:
- `artifact_version`: the `n` in `artifacts/<spec_hash>/<n>`. It is incremented for every Stage 2 candidate and every Forge promotion.
- registry `revision`: per install name, starting at 1. `/agents` shows it as `name vN`.
- the Spec hash itself: a different task is a different hash.

**Re-check on execution.** `load_reuse_target` re-loads the entry and re-checks license, digest, children and consumed state; otherwise it raises `RouteError` (`router.py:623-649`).

**HTTP** (`server.py`): `POST /compile`, `POST /run` (needs `enabled` and `human_reviewed`), `GET /status/{id}`, `POST /recompile`, `POST /route` (advice only). There is **no** HTTP endpoint for registry list or history, Forge, export or bench.

---

## 5. Every CLI command touching Forge, build, registry or bench

### 5.1 `architect` subcommands (`architect/cli.py:63-160`, `:309-376`, `main` `:1254-1324`)

| Command | Flags |
|---|---|
| `architect` (no args) | Opens the conversational CLI. Flags: `-p "<task>" [--json]`, `-c`, `-r <id>`, `--model`, `--permission`, `--workspace`, `--plain`, `--no-probe` (`cli.py:1266-1270`; `docs/usage.md:97-98`) |
| `architect session` | `--workspace`, `--profile`, `--script FILE`, `--no-probe` (`phase7-session.md:443-446`) |
| `architect serve` | → `architect.server` (`--profile`, `--host`, `--port`; token from `ARCHITECT_API_TOKEN`) (`cli.py:1257-1261`) |
| `compile <request_or_spec>` | `--profile` (req), `--approved-spec F`, `--no-route`, `--forge`, `--forge-usd X`, `--forge-minutes M`, `--forge-model NAME`, `--forge-trigger always\|failed`, `--forge-mode agent\|template`, `--yes` |
| `do <goal>` | `--profile`, `--repo` (req), `--reuse-installed`, `--no-route`, forge flags |
| `create <request>` | `--profile`, `--repo`, `--approved-spec`, `--no-route`, forge flags |
| `recompile <name>` | `--profile`, `--no-route`, forge flags |
| `run <artifact_dir>` | `--profile`, `--input JSON` (JSON text, not a file) |
| `status [name]` / `registry` | `--workspace` (req) |
| `route <request_or_spec>` | `--profile`, `--origin cli\|session\|orchestrator`, `--run INPUT_JSON`, `--yes`, `--json` |
| `roles` | `--profile`, `--json` |
| `forge report <artifact>` | `--json` |
| `forge run <request_or_spec>` | `--profile`, `--input JSON\|@FILE` (req), `--cases FILE`, `--reuse-installed`, `--json`, `--forge-usd`, `--forge-minutes`, `--forge-model`, `--forge-mode`, `--yes` |
| `forge integrate <request>` | `--profile`, `--repo`, `--into` (req), `--approved-spec`, `--package NAME`, `--stage-only` \| `--package-only`, `--reuse-installed`, `--json`, the mode flags, `--yes` |
| `export verify <package>` | `--profile`, `--reverify`, `--yes`, `--json` |
| `bench run` | `--tier smoke\|quick\|full\|arc`, `--suite v1`, `--provider fake\|profile`, `--profile`, `--models`, `--presets`, `--variant default\|noisy\|images-all-stages\|forge\|template\|routed` (repeatable), `--route`, `--max-usd`, `--seed 7`, `--repeats`, `--size`, `--max-candidates 2`, `--arc-games 5`, `--arc-actions 24`, `--arc-cache`, `--delivery`, `--timeout-s`, `--label`, `--out`, `--json`, `--yes`, `--forge-usd`, `--forge-minutes`, `--forge-model`, `--rounds`, `--resume BENCH_ID` |
| `bench compare A B` | `--out`, `--json`, `--expect identical-scores`, `--allow-fingerprint-mismatch`, `--pair-variants BASE:CAND` |
| `bench list` / `bench pages` / `bench probe` / `bench roles` | `--out --profile --suite --json` / `--seed --count --style clean\|scan\|dense --dpi --kind vision\|digitizer --out --png` / `--profile --models --json` / `--policy pinned\|evidence --json` |

Forge consent on the compile path:
- It prints `forge: compile cap $A + forge cap $B = $C (API-equivalent, not a bill); forge minutes M; verifications for this spec so far: K/N` to stderr, then asks `Continue? [y/N]`.
- A non-TTY without `--yes` gives `forge_cancelled`.
- No smoke floor gives `forge_disabled` (`cli.py:222-270`).

### 5.2 Agent CLI slash commands (Forge, build and registry related)

| Command | Effect | Code |
|---|---|---|
| Shift+Tab / Alt+M, `/mode [normal\|forge]` | Cycles normal, forge, plan. Plan is really a permission mode. | `commands.py:319-333` |
| `/make <description>` | One request in build mode | `commands.py:336-341` |
| `/forge <description>` | Build mode with Forge forced on (`force_forge`) | `commands_forge.py:79-138` |
| `/forge arc [--run] [--model M]` | ARC harness: explains, or runs after a yes | same |
| `/agents`, `/report <agent>`, `/run <agent> [input]`, `/recompile <agent>`, `/library` | Registry, report, run, rebuild, library | `commands.py:401-531` |
| `/floor [limit\|add <file>\|clear]` | Next compile's floor limit or case file | `commands.py:605-624` |
| `/budget [session <usd> [min] \| run <usd> <s> \| compile <usd> [min]]` | Session, per-run or compile wallet | `commands.py:627-665` |
| `/model [name]` | Conductor **and** next compile model | `commands.py:668-703`; provider picker `commands_providers.py:346` |
| `/export <agent> [path]` | Into software (falls back to the conversation export otherwise) | `commands_forge.py:154-200` |
| `/serve [port\|stop]` | Local HTTP service | `commands_forge.py:404-477` |
| `/bench <verb> ...`, `/telemetry` | Bench verbs; cost and quality over time | `commands.py:706`, `:847` |
| `/skip` (during interview) | Accept remaining defaults | `build.py:623-676` |

Session verbs: `forge on|off|status|budget <usd> <min>|model <name>|mode agent|template` (`session/forge.py:136-200`). Phase 10 adds `route`, `reuse <name> <hash8> {json}`, `forge run {json}`, `forge integrate <repo> <into>`, `export`, `rate good|bad`, and `rollback <name> [rev]` (`docs/usage.md:719-733`).

Conductor tools the model calls (`agent/tools.py:699-740`): `make(description)`, `forge(target)`, `agents()`, `report(name)`, `run_agent(name, input)`, `spawn(preset, task, ...)`, `code(task)`, plus read-only tools.

---

## 6. Live data a UI could show, where it is produced, and the bridge API

### 6.1 Event channel that already exists: the `UI` protocol (`architect/agent/events.py`)

The conductor and build mode never print. They call `ui.emit(Event)`, `await ui.ask(question, default, choices)` and `await ui.approve(action, detail)` (`events.py:113-124`).

Event types (`:33-110`):
- `Thinking(active, label)`
- `TextDelta(text)`, `AssistantText(text)`
- `ToolStarted(call_id, name, summary)`, `ToolFinished(call_id, name, ok, summary)`
- `SubagentActivity(agent, text)`
- `Progress(text)`
- `Notice(text, level)`
- `PlanCard(title, rows: tuple[(label, value)], footer)`

`RecordingUI` (`:127-155`) is a scripted implementation. **A desktop bridge can implement `UI` directly** and get every interview question, plan card, progress line, approval and result card. The catch is that the payloads are pre-formatted strings. For example, the plan card rows are text like `"floor": "held_out_tasks >= 0.80 on 'x'"` (`build.py:1129-1209`), and `Progress` is `"stage 2 tune: 14 calls, $0.03"` (`:1259-1267`). A purpose-built UI will want structured fields (see 6.4).

### 6.2 Forge live stream (Stage 3)

- **Producer chain.** The Pi bridge `observe()` emits `tool_start` and `tool_end` events (`architect/backends/pi-node/bridge.mjs:133-141`). They are sanitized in `backends/pi.py:498-530` and forwarded to `on_event` when the `FORGE_EVENTS` ContextVar sink is set (`compiler/forge_tools.py:109-113`; `compiler/forge.py:161-165`). `build._forge_events` turns them into `SubagentActivity("forge", ...)` (`build.py:1354-1369`).
- **Limitation 1: the summaries for `forge_*` tools are empty.** `resultSummary` only summarizes write (bytes), edit (+/- lines), read (lines) and bash (`bridge.mjs:117-132`). The CLI therefore shows `forge_measure` with no score.
- **Limitation 2: `ok` is the Pi-level `!isError`.** Soft `{ok:false, code:...}` results look like successes.
- **The rich data exists, but only in trusted Python inside the tool callbacks.** These are allowed for the user to see: tuning only, no held-out data.
  - `forge_measure` callback (`forge_tools.py:774-870`): aggregate `Measurement` (score vs `floor_limit`, floor and budget passed, mean cost, calls) and per-case `CaseDetail` (passed, score, failure code, sandbox_line, cost, output and expected excerpts, `GameDetail`; `runtime/evaluation.py:380-411`, `:447-590`).
  - `forge_submit` / `submit_source` (`:876-1010`): candidate number, accepted, best_so_far, full-tuning measurement, smoke, submissions_left.
  - `forge_check`: code, line, hint, node stats, lint.
  - `forge_dryrun`: prompt shapes, output head, fuel.
  - `forge_spawn`: child license and `case_origin`.
  - `ForgeSession.left()` (`:328-341`): the remaining $, seconds and tool caps, which is the live budget meter.
  - `ForgeSession.best_rank` / `best_number` (`:292-297`): the current champion.
- **What a bridge needs.** A new optional sink, for example a second ContextVar `FORGE_RESULTS`, called at the end of each `forge_*_tool` callback with the (already bounded) result dict plus `left`. That one hook would drive the compile board, the score-vs-floor meter, the per-case grid, the submission ladder and the budget gauge. Nothing like it exists today.
- **The final summary** is `ForgeResult` via `Compiler.forge_listener` (`compiler/__init__.py:73-75`, `:334-339`; fields `forge.py:122-137`) and `forge.json` on disk.

### 6.3 Compile-level live data

| Data | Producer | Notes |
|---|---|---|
| Current stage (0-4) | `Compiler.stage_listener(stage: int)` via `_enter` (`compiler/__init__.py:72`, `:289-298`) | A class attribute that build mode swaps per compile (`build.py:1294-1351`). Repeats per ladder model. |
| Calls, spend, in-flight, failed | `session/observe.py` `Meter` + `ObservedAdapter` (`:52-160`), `CompileWatcher` (`:230-320`) emitting `session.state.Progress` (elapsed, stage, calls, in_flight, case_rows, quality_rows, rewrite_rows, accepted_rows, spend, budget, left_usd, left_s, failed) | Content-free. Build mode uses `_Tally` (`build.py:1215-1256`) from the `meter` extra or the telemetry tail. |
| Telemetry rows | `TelemetryRow` (`telemetry/schema.py:75-105`): run_id, task_type, spec_hash, harness_version, model, tokens, cost, wall time, model_calls, floor_type, floor_score, floor_limit, accepted | Appended per run; `JournalCursor` tails them. Usable for a "case results trickling in" indicator, but rows carry no case id. |
| Stage 2 candidates | `tune()` builds a `CandidateEvaluation(artifact, measurement)` per param set (`compiler/tuning.py:306-327`) | **No observer.** A bridge would need a hook. Also visible after the fact as `artifacts/<spec_hash>/<n>/config.json` dirs without a `report.json`. |
| Stage 4 verdict | `CompileResult.report` / `HarnessArtifact.load(dir).report` | `floor_score` vs `floor_limit`, `held_out_count`, `mean_cost_usd`, `cost_usd`, `compute_s`, `failures`, `verification_id` |
| Route decision | `CompileResult.route` (`compiler/types.py:79-83`); `RouteDecision` + `render_decision` | Before Stage 1 |
| Forge consent numbers | `session.forge.cap_line(compile_usd, settings, verifications)` (`session/forge.py:94-102`); `forge_modes.run.consent_text` / `prepare_agent` (`run.py:276-314`) | For a confirm dialog |

**The "meter vs floor line" view.** Stage 2, Forge measures, Forge submits and Stage 4 all produce a `Measurement` (`compiler/types.py:41-51`) with `score`, `floor_passed` and `budget_passed`, and they all share `spec.floor.limit`. A UI can plot each against one horizontal floor line. It has to label which partition each point came from: tuning (Stage 2 and Forge) versus held-out (Stage 4 only). Doing this honestly is the core of the design. Tuning numbers must be labelled "evidence, not a license" (`phase9-forge.md:1206-1214`).

**Held-out cases.** Never display held-out case content during a build. It is architecturally unreachable from Forge. After Stage 4, the report exposes only `held_out_count` and the score. The case files themselves are in `floors/<dataset>.json`, and a UI can show the partition preview (`session/floors.py:361`, `partition_preview`) and per-case review state.

### 6.4 Python functions a bridge would call

**Bootstrap**
- `architect.profiles.load_profile(path)`
- `build_services(profile, workspace) -> Services{compiler, orchestrator, registry, runtime, profile, workspace, adapter_factory, forge_adapter, router, roles}` (`profiles.py:492-520`, `:640`)
- Set `ARCHITECT_SIGNING_KEY_FILE` first (`agent/app.py:370`)
- Also: `agent.app` onboarding helpers `onboarding_choices()` and `register_model(...)` (`phase13-cli-complete.md:306-308`)

**High-level build (reuse everything)**
- `architect.agent.build.make(description, ctx: ToolContext)` or `forge_target(target, ctx)`. The `ToolContext` carries services, cwd, ui and extras: `profile_path`, `reload`, `meter`, `force_forge`, `skip_interview`, `build_overrides`.
- `build.set_override(ctx, key, value)` with `OVERRIDE_KEYS` (`build.py:142-154`, `:298-376`), plus `override_lines` and `clear_overrides`.
- `build.interview(description, ctx) -> Interview`.
- The return JSON shapes are documented at `build.py:1515-1528`, `:1702-1769`.

**Lower-level**
- `architect.compiler.inference.infer_spec(source, adapter, runtime, user_fields)`
- `Compiler.compile(spec, approved=True, origin=..., no_route=..., reuse_installed=..., budget_cap=...) -> CompileResult` (`compiler/__init__.py:353-395`). Set `compiler.config = config.model_copy(update={"forge": ForgeSettings(...)})`, `compiler.stage_listener`, `compiler.forge_listener`, `compiler.smoke_factory`, and the `FORGE_EVENTS` ContextVar exactly as `build._compile` does (`build.py:1297-1351`).
- `architect.compiler.selection.select_preset(spec, model)` for the "baseline to beat" (`build.py:1119-1126`).
- Floors: `session.floors.new_draft`, `draft_cases`, `add_file`, `partition_preview`, `review`, `save_dataset`, `register`, `summarize`, `synthetic_label` (`session/floors.py:66-441`). Also `session.edit.blockers(spec, profile, cost)`, `normalize_schema`, `suggested_cost`.
- Registry: `Registry.list/status/get/history/drift/install/rollback`.
- Library: `HarnessLibrary.candidates/verification_count`.
- Artifacts: `HarnessArtifact.load(dir).licensed/.report/.forge`.
- Reports: `session.forge.read_forge_record`, `agent.tools.harness_design`.
- Routing: `services.router.route(...)` / `session.routes.advice(...)` (`session/routes.py:269-282`); `routing.router.render_decision`.
- Forge modes: `forge_modes.forge_run(...)`, `forge_integrate(...)`, `export_package`, `place_package`, `verify_export`, `run.prepare_agent`. Their `consent` callbacks are **synchronous**, so an async UI must collect the answer first, as the session does (`session/delivery.py:257-265`). They print to stderr directly (`run.py:486`, `:644`; `integrate.py:330-414`).
- Export: `agent.export.export_agent(name, ctx, path, floor_origin=, example=)`, `python_snippet`, `http_snippet`, `tests_command`.
- Bench: `architect.bench` is importable only from `bench/`, `cli.py` and `session/bench.py` (a boundary test, `phase9-forge.md:107`). A UI should shell out to `architect bench ... --json` or go through `session/bench.py`.

Errors are fixed codes, never text: `ToolError(code, detail)`, `ForgeModeError.code`, `RegistryError(code)`, `RouteError`. There is one ASCII sentence per code in `session/codes.py` (`code_for(exc)`, `explain(code)`, `hint_for`).

---

## 7. What is confusing in the CLI that a purpose-built UI could make easier

1. **"Forge" means three things:** the Stage 3 engine, the delivery modes, and the CLI mode (§0). The mode's internal value is `"build"`, `/make` and `/forge <text>` differ only by `force_forge`, and `/forge arc` is a third behaviour.
   - UI fix: one "New agent" flow with an explicit "Engineer: Forge / Preset only" toggle and a separate "ARC/game harness" entry.
2. **The engineer toggle and the Forge budget are invisible.** `set_override` accepts `engineer`, `forge_usd` and `forge_s` (`build.py:142-154`), but no slash command sets them. `/budget` only covers session, run and compile (`commands.py:627-665`). The plan card shows the Forge budget, but the user cannot change it from the CLI.
   - UI fix: editable fields on the plan card.
3. **Four different budgets** (session wallet $25, per-run, compile wallet, Forge cap on top), plus conflicting defaults. The session wallet is $25 in code but $5 in the Phase 12 doc (`conductor.py:106`; `phase12-agent-cli.md:52-54`). The compile wallet is $10/120 s in the package but $5/1800 s in a bootstrapped profile.
   - UI fix: one stacked budget bar: compile + Forge = outer cap, next to the per-run cost and the session.
4. **Approval is implicit in build mode.** Enter on "Compile this agent?" approves an INSTALL Spec whose hash appears only as 8 characters in a footer (`build.py:1619-1627`). The session demands a typed `approve <hash8>`.
   - UI fix: show the full Spec (schemas, floor, budget) with a visible hash, a diff after edits, and an explicit Approve button bound to that hash.
5. **Drafted SYNTHETIC floors are accepted wholesale in build mode** (12 cases, no per-case review; `build.py:964-976`). The session forces one-by-one review and warns that drafted expected values "measure agreement with that model, not correctness" (`docs/usage.md:300-343`). With 12 cases there are only 6 held-out, so one miss can decide the license.
   - UI fix: a case table with tuning/held-out partition colouring, inline review and fixing of expected values, a "misses tolerated" indicator, and an upload/paste of real cases. Image agents need real page cases (`build.py:915-920`; `architect bench pages`).
6. **The Forge stream is opaque.** The CLI shows `forge: forge_measure` lines with empty summaries (`bridge.mjs:117-132`), and soft failures look ok.
   - UI fix (needs a small backend hook, §6.2): a "compile board" with check, dryrun, measure and submit counters against their caps, a tuning score sparkline against the floor line, the per-case pass/fail grid, a submission ladder with accepted/best, and the `left` gauge.
7. **Stage numbering repeats per ladder model, with no "model k of n".** Stage 3's label is "rewrite", "forge" or "stage 3 rewriting" depending on the surface (`build.py:156`; `session/observe.py:44-49`).
8. **Tuning vs held-out vs license is subtle.**
   - Forge "accepted" and "best" are tuning-only.
   - The license is Stage 4 only.
   - Child sub-harness licenses are weaker (`case_origin`).
   - The CLI states this only in footnotes.
   - UI fix: a persistent visual distinction (e.g. hollow vs solid points; a "LICENSE" badge only from `report.json` re-read from disk).
9. **"Licensed but not installed."** The `not_better` and `budget_increase` registry rules (`build.py:1430-1452`, `:1735-1748`) leave a licensed artifact that is not installed, and only a text note says so.
    - UI fix: a side-by-side "incumbent vs new" comparison using the registry `_rank` keys.
10. **Three version counters** (artifact_version, registry revision, spec_hash) plus three naming schemes (`<kebab-name>` from build mode, `<spec_hash>` from `architect compile`, `feature-<hash16>` from integrate and create).
    - UI fix: an agent-centric tree: Agent → revisions → artifact dir → children.
11. **Registry operations are missing from the main CLI.** Rollback and history exist in Python and the session only; HTTP has no list endpoint (§4). `/library` reads SQLite directly. `Registry.list()` raises if any current entry fails to load as licensed (`registry/__init__.py:406-412`), so one tampered entry can blank `/agents`.
    - UI fix: a per-entry tolerant list with a status per row.
12. **The `/serve` "first call" trap.** The service runs only agents it licensed itself, so an installed agent needs a fresh `POST /compile` (a fresh Stage 4) on the service before `/run` (`commands_forge.py:390-396`).
    - UI fix: a one-click "publish to local service" that does it, with a cost estimate.
13. **The export flow is long and text-heavy:** staged path, manifest sha, feature-flag env var name, host-attested license, Python, HTTP and test snippets (`agent/export.py:397-491`).
    - UI fix: a file-tree preview with approve, copyable snippets, a flag toggle explainer, and a "Verify package" button (`verify_export`).
14. **Router output is a dense 100-column table** with abbreviations (F, m_lcb, r, lat m/b, evidence codes; `router.py:729-831`), and grants such as `--reuse-installed` and `reuse_origins` are easy to miss.
    - UI fix: a "Reuse an existing agent?" card listing candidates with plain-language fit and quality margin, and a Reuse button that is the grant.
15. **Consent prompts on stderr** (`forge run` / `integrate`) and synchronous consent callbacks: a GUI must pre-collect consent (§6.4).
16. **Forge silently not running.**
    - Default `trigger: "failed"` skips Forge when the preset already passed. Build mode forces `always`.
    - No smoke floor gives `forge_disabled`.
    - Non-`held_out_tasks` floors are blocked.
    - The `forge_verification_cap` (10 per spec) ends future Forge runs for that Spec.
    - UI fix: surface "verifications used K/10" and why Forge did or did not run (`build.py:1496-1509`).
17. **Unpriced models.** Build mode refuses `model_unpriced`, and the fix is a `/model add p:m --price in/out` incantation (`build.py:136-139`).
    - UI fix: an inline price form.
18. **Local models.** $0 per call, so time is the only binding budget. The Forge rank ties on dollars and never prefers fewer calls (`PROGRESS.md:145`).
    - UI fix: show seconds prominently for local models; flag the known rank limitation.
19. **Cost labels.** CLI providers report "API-equivalent, not a bill". Build (agent) cost, compile cost and play cost are reported separately in `forge run` but merged into one "spent" figure in build mode's result card (`build.py:1839`).
    - UI fix: three labelled cost chips.
