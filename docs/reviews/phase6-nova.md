# Phase 6 independent audit (nova-64)

Read-only audit of the uncommitted Phase 6 tree on 2026-09-24, run as a
multi-agent workflow: seven dimension finders (authority, budget, official
adapter, HTTP, inference, docs, packaging) plus a status check of every item in
`docs/reviews/phase6-final-audit.md`. Each finding was then attacked by two
independent verifiers (a correctness reader and a reproducer) told to refute
it. Only findings that survived both are listed as confirmed. Reproductions
ran in a scratch directory against the pinned `arc-agi==0.9.9` /
`arcengine==0.9.3` packages and the real HTTP server on ephemeral ports; no
repository file was modified by the audit. 38 agents, 675 tool calls.

Baseline on the audited tree: full suite **2,097 passed, 7 skipped**, 94%
total coverage, ruff and strict mypy clean (52 files). Focused Phase 6 files:
253 passed, 4 Windows unprivileged-symlink skips.

## Status of the prior audit's findings

| Item | Status | Where |
| --- | --- | --- |
| M1 parent Stage 3 smoke | fixed | `games/competition.py` builds a distinct parent-tuning smoke floor; `tests/test_competition.py::test_offline_parent_rewrite_reaches_real_fixed_smoke_without_heldout_leak` |
| M2 parent floor limit | fixed | `CompetitionDescription.floor_limit`, frozen into the policy and re-checked on approval |
| L1 deadline cleanup | fixed | shielded reap grace in `games/official.py`, factory teardown outside the expired timeout; real-subprocess tests |
| L2 dollars before side effects | partial | competition path checks `cost_usd > budget` before discovery; `GameFactory.remaining()` still relies on the sticky flag. Exactly-spent zero-cost work is now an explicit tested decision |
| L3 positional per-game scores | partial | one-game card consistency is enforced worker- and parent-side; `GameRunResult` still has no target ref, children remain bound by list order |
| L4 nominal receipt bindings | unchanged | informational; scope + task + run binding is the effective one |
| L5 Stage 0 override keys | fixed | `exact_overrides=True` in both factories; competition re-applies overrides |
| L6 report scan availability | partial | report saves are atomic now; the scan itself is unchanged (see H3) |
| L7 discovery rules | fixed | compile and delivery share `validate_discovery` |

## Confirmed findings

**O1 (medium) OFFLINE preview accepts a best-of-runs score after a full reset.**
`games/_official_worker.py:317` applies the `full_reset` / `levels_completed`
regression check only in gateway mode. Installed `arcengine` 0.9.3 performs a
full reset whenever `_action_count == 0`, which `set_level()` zeroes on every
level transition; `arc_agi` opens a new play on a full reset and
`EnvironmentScoreList.score` is the maximum over runs. Reproduced: a staged
two-level game played sloppily, then RESET immediately after the level
transition, then played perfectly, finalizes as raw 100.0 with
`source="official"`. Fix: apply the same rejection in offline mode and/or set
`ONLY_RESET_LEVELS=true` before importing `arcengine`; reject a finalized card
whose environment has more than one run.

**O2 (medium) Missing or short `baseline_actions` yields an official 0.0
instead of unavailable.** `games/_official_worker.py:343` reads `env.score`
only. With `metadata.json` lacking `baseline_actions`, or `[]`, or fewer
entries than levels completed, the SDK returns `score=0.0`,
`level_baseline_actions=[-1, ...]` and a message. The SDK's own downloader
writes `baseline_actions: []` when the API omits it, so the state is realistic.
Reproduced against the pinned SDK: the same winning play scores 100.0 with
`[1, 1]` and 0.0 with the key absent. Fix: fail with a fixed code when any run
carries a message or a negative baseline, or refuse `make` when the public
`EnvironmentInfo.baseline_actions` is empty or shorter than the level count.

**H1 (medium) One damaged `report.json` denies every compile.** Confirms L6.
`server.py:531-559` walks both roots on every `/compile` and `/recompile`,
requires `st_nlink == 1` and parses every `report.json`; any failure becomes a
500 `compile_failure` with no file identified. Reproduced with a truncated
report in a `registry/.install-*` crash remnant (the in-root `mkdtemp` that
`Registry.install` cleans only in-process) and with one extra hard link to an
old candidate report. Recovers as soon as the file is removed. Fix: skip or
count unparsable reports and dot-prefixed remnants, or scope the snapshot to
the spec being compiled.

**H2 (medium/low) Blocking work inside the event loop.** The scan above and the
30 s `BEGIN IMMEDIATE` SQLite waits in `Catalog._transaction` and
`Registry._transaction` run synchronously in async handlers. Every endpoint,
including bearer-token rejections, stalls behind them; a CLI `install` holding
the registry lock freezes the server for its duration. Reproduced against a
real uvicorn. Fix: `asyncio.to_thread` for the sync work and a short SQLite
timeout that maps to a fixed unavailable code.

**H3 (medium) Client deadline shorter than server compile budget.** The
TypeScript client hard-coded a 120 s abort while the default compile budget is
also 120 s and starts only after the snapshot scan; the server keeps compiling
and installs after the client has thrown `NETWORK_ERROR`, and a retry then
returns `not_better` with no artifact id. Reproduced with the built client
against a 125 s compile. Addressed in this audit: the deadline is now a
constructor option (`{ timeoutMs }`), documented as needing to exceed the
profile compile budget. The server-side idempotency gap (returning the existing
artifact id on an identical approved spec) remains open.

**H4 (medium) Long workspaces fail every install on stock Windows.** Registry
entries nest about 120 characters below the root
(`versions/<32 hex>/<rev>/artifacts/<64 hex>/<version>`), and directories above
247 characters cannot be created unless the machine policy
`LongPathsEnabled` is 1 (it is 0 by default and on this host). Stage 4 succeeds
and spends its dollars, then `Registry.install` fails with `storage_error`,
reported over HTTP as `registry_rejected`, in the example runners as
`competition_example_failed` / `phase6_exit_failed`, and in the Lectern runner as
an uncaught traceback. Addressed in this audit: `Registry.__init__` now
refuses a root whose two-digit-revision entry would not fit (registry root
above 123 characters) with the fixed code `workspace_path_too_long` before
creating anything, and `install` applies the same named check to the exact
directory; both only when the policy is off
(`tests/test_registry_path_length.py`). Documentation of the constraint in
`docs/usage.md` / `docs/limitations.md` and a shorter on-disk layout remain
for the owners of those files.

**B1 (low) Budget exhaustion after Stage 0 reports as `game_compile_failed`.**
`games/factory.py:246` classifies any unlicensed compile as
`game_compile_failed`; `compile()` swallows `BudgetExceeded` per case and the
compile-budget timeout into `failures`, so an exhausted ancestor wallet never
reaches the `game_budget_exceeded` branch and tuning keeps iterating against a
refusing wallet. Reproduced twice. Fix: after `compiler.compile` returns, apply
the same exhausted-wallet predicate the exception path uses.

**P1 (low) The contract-required live-server TypeScript end-to-end test runs
nowhere.** `client-ts/scripts/e2e.mjs` needs an operator-started server and
five environment variables; CI and the Python suite only exercise the mocked
node tests, so a DTO rename in `server.py` would pass CI.

**D1-D4 (low) Documentation drift.** `docs/telemetry.md` omitted the
`floor_role` field (fixed here). `PROGRESS.md` lines 4-13 still say Phase 4 is
committing and Phase 6 pending. `docs/interfaces/phase6-games.md` still opens
with "planning only" above integrator-approved, implemented amendments. No
document mentions the Windows path-length constraint.

## Refuted

- Delegation/game backends accepting parent held-out targets in any scoped
  measurement: reproducible only by privileged host Python stepping around the
  floor gate, which the contract places in the floor owner and treats as
  trusted. Not a defect.
- Explicit `GameFactory` manifest reconciled only by refs: `LocalFixture.ref`
  embeds the puzzle fingerprint as the revision, so a divergent payload fails
  discovery and `open()`. Not a defect.
- Default deep-merge keeping model sub-keys under partial host overrides in
  the orchestrator path: real mechanism, but documented and test-pinned as
  the non-exact default; no authority is gained. Recorded as a design note.

## Verified holding

- Installed SDK facts: OFFLINE `close_scorecard` populates `environments[].id`
  with the exact `game_id` from staged `metadata.json` (full versioned id), the
  per-game score is computed offline as a 0..100 percent with the 1.15 cap
  per level only, and the adapter's range check and `raw / 100` conversion
  match. The "unverified" normalization item in `PROGRESS.md` is now verified
  against the pinned packages, not against live public games.
- Stage 4 remains the only license path; held-out isolation, frozen policy,
  receipt scoping, lease revocation and single-billing all held under
  targeted scripts.
- Clean copy of the tracked tree: `uv lock --check`, `uv sync --frozen
  --all-extras`, wheel build, competition tests, exit script tests, and the
  node tests all pass; the CI job without the ARC extra skips exactly the SDK
  tests. No fixed ports and no dependence on ignored files.
