# Phase 6 Claude Code audit

Actual `claude-opus-5-5`, 43 turns, $3.095716 reported development cost. Read-only audit; findings below require integration decisions and validation.

I found no exploitable authority bypass or held-out leak in the current source. There are two medium correctness defects; neither lets anything unverified get delivered. I didn't run any tests or commands; everything here comes from reading the current files, including the `server.py` that is being patched right now.

## Findings

**M1 — The competition parent's Stage 3 rewrite can never run** (medium, fails safely)
- `games/competition.py:343` passes `smoke_factory=floors`, and `floors()` returns the same `floor` object every time.
- `compiler/rewriting.py:242` raises `floor_mismatch` when `floor is smoke_floor`, and the compiler records that as `rewrite_failed` (`compiler/__init__.py:202`).
- Reproduce: use a dev compiler with `enable_rewriting=True`, `max_rewrites>0` and a smoke factory, plus a planner that fails tuning. You always get `rewrite_failed` and then `tuning_failed`.
- The contract allows offline Stage 3 for the parent, so this is a missing capability, not a security hole.

**M2 — Parent floor limit is fixed at 1** (medium, design correctness)
- `competition.py:291-293` and `competition.py:316` set `limit=1`, and `GameFactoryConfig.floor_limit` also defaults to 1 (`factory.py:49`).
- Under `official_percent_v1`, a limit of 1 means a completed-card score of 100 on every preview game, so official licensing is effectively out of reach.
- `CompetitionDescription` has no field to set it.

**L1 — Cleanup is cut short when the deadline has already passed** (low, host hygiene)
- At `factory.py:266-281`, `asyncio.timeout(min remaining_s)` becomes a zero timeout once time runs out.
- `_WorkerProcess.close()` (`official.py:204-215`) kills the worker, then gets cancelled at `await process.wait()`. The process is never reaped, the temp dir isn't cleaned up, and the episode stays in `_episodes`.
- Reproduce: an official OFFLINE delivery where compilation times out before play.

**L2 — Dollars aren't checked before game-provider side effects** (low)
- `remaining()` at `factory.py:114-118` checks only the `exhausted` flag and time.
- A wallet that has spent exactly its budget isn't flagged, because `reserve` or `reconcile` only set it after a refusal or overspend. So `discover`/`open` can still start an official worker or episode card before the first model call fails.
- `competition.run` (`:432-436`) likewise checks only time before `discover()`.
- Children spawned through `_remaining_child` are covered.

**L3 — Per-game scores are bound only by position** (low)
- `GameRunResult` (`factory.py:63-75`) doesn't record the target `GameRef`. `CompetitionRunResult.children` relies on list order, and on failure it holds the partial `attempted` list.
- `_scorecard_scores` (`_official_worker.py:141-155`) and `official.py:359-360` don't check that a one-game card's `score` matches its `environments[]` score.
- The aggregate is a mean of separate one-game episode cards. It is honestly labelled `normalized_preview_mean`, not an official all-environment scorecard.

**L4 — Some receipt bindings are only nominal** (informational)
- The session binding is a fresh `uuid4()`, not tied to the real session (`execution.py:292`, `competition.py:215`).
- The candidate-digest check is circular: `evaluation.py:198` copies the scope's digest into the context, and `ReceiptLedger.issue` then compares the two.
- The effective binding is scope object + task + run, which is still sound: receipts can't be forged, reused across measurements, or used for another case.

**L5 — Stage 0 can add keys to host overrides** (low, fails safely)
- `inference.py:191` deep-merges the model's proposal with the overrides. A model can add keys under `output_schema`, `floor` or `execution_config` in `GameFactory`.
- The result fails safely (`extra=forbid` on the policy, floor mismatch), but it lets a model break its own compile, e.g. an `output_schema.required` that no receipt can satisfy.
- `CompetitionFactory.compile` re-applies the overrides (`:370-372`); `GameFactory` does not.

**L6 — HTTP availability** (low)
- `_verification_ids` (`server.py:538-558`) re-reads every `report.json` under both roots on every compile. Any unparsable report, or one with extra hard links, turns every compile into a 500.
- A non-atomic report save during concurrent compiles may also trip it. I didn't verify whether `save()` is atomic.
- The stale-license, old-license replay, hardlink and fixed-code protections otherwise look correct in the current source.

**L7 — Discovery rules disagree** (low)
- `competition.run` requires discovery to match the manifest exactly (`:438`), while `validate_discovery` accepts extra games (`evaluation.py:112`).
- A staged directory with extra public games breaks delivery but not compile.

## Checked and holding

- **Stage 4 is the only license authority.** Every compile re-verifies with a new `verification_id`; isolated children use `NullLibrary`; runtime children reject rewriting and sandboxed warm starts; the server rejects results whose verification IDs already existed.
- **Parent held-out games stay out of child probes, tuning and library.** `run_isolated` probes only `support_tuning[0]`, evaluates only support games, and binds the target only at play (`key == target`, non-evaluation). `run()` accepts only `delivery` targets, and manifests keep roles disjoint by fingerprint or base game ID.
- **Game policy is frozen and canonical.** It is validated by model equality against the authenticated `spec.execution_config`, the exact model and preset, and the original task.
- **Capabilities are revoked.** Leases are checked before every side effect and after waits; pending tasks are cancelled when the scope exits; closed contexts and calls with differing bindings are rejected.
- **Usage accounting.** `RunResult`/`GameRunResult`/`CompetitionRunResult` report wallet (descendant) totals, while each telemetry row bills only its own context, so nothing is counted twice. Failed reservations stay consumed, and quality rows carry zero usage.
- **No token quotas.** Only call and turn counts, bytes and dollars/time act as limits; tokens are observed only.
- **Official score conversion.** `raw/100` with checks for type, finiteness and 0–100 range, the exact `environments[].id` field, and finalization inside the execution deadline before a receipt is issued.

## Not implemented or needing outside evidence (not blockers)

- **Gateway submission isn't wired into `GameFactory` or `CompetitionFactory`.** `provider_manifest` refuses gateway mode and manifests require `official_offline`. That matches the no-submission scope.
- **Whether SDK 0.9.9 OFFLINE `close_scorecard` fills in `environments[].id` with the full versioned game ID, and actually computes the efficiency score, is unverified.** If the ID lacks the version suffix, every official score fails as unavailable. This needs staged public files and a live smoke test.
- **Competition compliance and numeric Kaggle caps remain unverified**, as the code already labels them.

## Limits of this review

- I didn't read the registry, `HarnessArtifact.licensed`, the base floor split, the sandbox internals in full, `client-ts`, the examples, or the tests.
- I assumed privileged host Python is trusted and gave no OS isolation.
- Hardlink and TOCTOU gaps between checking a file and loading it are host-only hardening, not blockers.