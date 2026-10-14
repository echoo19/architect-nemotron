# Phase 6 games — independent adversarial review

Date: 2026-09-24. Scope: current working tree, not earlier queued reports.

## Result

The independent suite has 46 passing tests. These are **offline scripted-model
local fixture and scripted official-transport tests**, not ARC benchmark results
or official competition evidence.
`FixtureAdapter` reads public observations only. It does not inspect engine state,
solutions, source files, or manifest puzzle fields. The actual compiler, runtime,
game sessions, floor scoring, registry, and receipt checks remain in the path.
No production file was edited by this reviewer. No commits were made.

Commands:

- `uv run pytest tests/adversarial/test_phase6_games.py -q` — 46 passed in 5.92s.
- `uv run ruff check tests/adversarial/test_phase6_games.py` — passed.
- Combined independent + factory + execution + competition + local + official suites:
  `uv run pytest tests/adversarial/test_phase6_games.py tests/test_game_factory.py tests/test_game_execution.py tests/test_competition.py tests/test_game_local.py tests/test_game_official.py -q`
  — 201 passed in 46.49s.

## Defect found and fixed by the game implementation owner

`GameFactory._run` originally checked only its new wallet before opening the
provider. An already exhausted ancestor wallet could still cause a target open.
A blocked provider open could also outlive the ancestor compute deadline because
the timeout used the child wallet alone.

Concrete reproduction:

1. Enter `compilation_wallet(Budget(cost_usd=0.001, compute_s=30))`.
2. Deny a $0.01 reservation, setting the parent wallet to exhausted.
3. Call `GameFactory.run` with an ordinary delivery target.
4. The recording provider observed an open before inference was refused.

Regression:
`test_exhausted_parent_wallet_blocks_factory_before_environment_side_effects`.
This test failed before the owner's fix and passes afterward. The owner added
ancestor preflight and the minimum active-wallet deadline before setup/open.
`test_ancestor_deadline_interrupts_setup_not_only_model_calls` also passes against
a real blocked `open`, with cancellation observed. This review did not weaken
these assertions or replace production wallet enforcement.

## Verified boundaries

- All three local families execute real winning and legal losing rollouts.
  Trusted scores are 1 and 0, respectively. Legal wrong play fails the child
  license without delivery. A model-authored `score` fails instead of earning
  credit.
- Real issued receipts cannot be forged, replayed, moved to another case or
  candidate, or reused after measurement expiry. Candidate mismatch cannot issue
  a receipt. Score claims appended to a receipt are not accepted.
- Runtime grants belong to one Runtime. Scope exit cancels pending provider
  work. Original task substitution and mutable task aliases fail before open.
  The integer/boolean JSON alias attack is rejected even though Python equality
  considers the values equal. A child with pending game work cannot be absorbed.
- Reserved parent targets cannot use the ordinary compile/probe path. Isolated
  child compilation opens only public support instances before target play.
  Actual Stage 0 observations match the support observation, not the target.
  Parent IDs do not enter child inference or Spec. Isolated library reads,
  writes, and verification updates are denied by an assertion-bearing fixture.
- Duplicate puzzle aliases across roles fail despite changed seeds/revisions,
  reordered/repeated walls, or cosmetic canvas changes. Role swaps change the
  manifest digest. Actual even/odd floor splits match the declared roles and
  contain disjoint complete puzzle identities. Changed frozen policy fails
  before environment open.
- Cloud/non-loopback bindings, runtime rewriting, expanded model ladders,
  sandbox warm starts, and rewritten warm starts are rejected.
- Cached children receive a fresh Stage 4 verification ID and perform real play.
  Failed model work retains quoted dollars and input/output usage. High-token,
  zero-dollar responses remain allowed and metered. Raw telemetry is not
  double-counted and does not contain frames.
- An approved installed parent really compiles six preview children and later
  plays three delivery children with distinct fresh Stage 4 reports. A scripted
  player that passes every child support license but loses two parent preview
  games gives the parent a 2/3 score, not a pass. Installation is refused.
  The observation-only child wrapper calls the original factory; it bypasses
  no capability, wallet, compiler stage, or floor.
- Missing staged official files raise explicit unavailability. They do not
  produce local games or scores.
- The generic official OFFLINE factory seam is now implemented and independently
  exercised with a scripted transport through the actual OfficialGameProvider,
  compiler, runtime, and final-score decoder. Completed cards of 100 and 25
  produce floor scores of 1 and 0.25, even when the public observation says WIN.
  Values 115, true, a numeric string, and null fail without binary fallback.
  Score source, metric, and conversion policy are frozen in execution_config.
  Revision aliases cannot move one public base game into two manifest roles.
  A gateway provider is rejected before discovery or model calls.

## Limits and remaining blockers

A real official run and competition compliance remain **blocked/unverified**.
The generic OFFLINE licensing path exists; this is not a claim that its real
staged SDK/files/model smoke test passed. Conversion is exercised with scripted
scorecards, not independently obtained real finalized-card fixtures.
The suite does not validate a real SDK/game-file/model bundle, a competition
submission gateway, real final-score conversion fixtures, Kaggle caps,
permitted runtime tools, or OS-level network isolation. It does not establish
model size from the binding name. Official-mode success is not inferred from
local wins or fake transports. Trusted Python provider/adapter code remains a
host trust boundary. No hidden engine state is offered to an agent.

This is a focused game-path review, not a full repository regression result.
