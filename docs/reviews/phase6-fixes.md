# Phase 6 audit fixes and re-verification (rhea-39)

Follow-up to [phase6-nova.md](phase6-nova.md), 2026-09-25, on macOS (the suite had
previously only run on Windows). Each round fixed findings in isolated worktrees,
merged them, ran the full suite, then sent every merged fix to an adversarial
workflow: one attacker per fix, and each claimed defect had to survive two
independent refuters (a code reader and a reproducer) to count as confirmed.

## Round 1: nova-64 findings

| Finding | Result | Commit |
| --- | --- | --- |
| CI red on Linux (strict mypy, Windows-only `WinDLL`) | fixed | `a0e996c` |
| O1 offline best-of-runs score after RESET | fixed: reset/regression rejected offline, multi-run cards refused (`official_multiple_runs`) | `927e598` |
| O2 missing baselines score 0.0 | fixed: `official_baseline_unavailable` | `927e598` |
| H1 one damaged `report.json` denies every compile | fixed: damaged/remnant/linked reports skipped and never trusted | `883d71f` |
| H2 blocking storage work in the event loop | fixed: `asyncio.to_thread`, 5 s lock wait, 503 `storage_busy` | `883d71f` |
| H3 retried identical approval gets no artifact id | fixed | `883d71f` |
| B1 budget exhaustion after Stage 0 reported as `game_compile_failed` | fixed: shared `wallets_exhausted()` | `a37b539` |
| L2 dollar check in `GameFactory.remaining()` | already correct (a wallet cannot exceed dollars without being flagged); now uses the shared predicate | `a37b539` |
| P1 live TypeScript e2e runs nowhere | fixed: `scripts/ts_e2e.py` in CI | `0432793` |
| D2-D4 doc drift, Windows path limit undocumented | fixed | `ddd3564` |
| macOS-only failures: detached Pi shell descendant survived, eval timing excluded cleanup, long tmp path | fixed (two were product bugs) | `1385980` |

Round-1 verification: 29 agents, 9 of 12 claims confirmed.

## Round 2: confirmed defects in round-1 fixes

| Defect | Commit |
| --- | --- |
| Short `baseline_actions` still inflated the score when the agent stopped early; too-long ones deflated it | `1076598` (length must equal `win_levels`, refused at `make`) |
| Compile's own dollar wallet refusal reported as `game_compile_failed` | `1076598` (`compile_budget`) |
| Catalog `storage_busy` after registry commit orphaned the install | `2f0bacb` (reservation row + heal) |
| `/run` telemetry writes still blocked the loop | `2f0bacb` |
| Stale-path check compared case-folded strings | `2f0bacb` (st_dev/st_ino) |
| POSIX reap skipped once the bridge exited | `8264727` |
| Write to an exited bridge raised a raw `ConnectionResetError` | `56eace8` |
| Harness script ran downloaded game code in the host process; partial/stale cache trusted; fixture score unlabeled | `3268b17`, `f02727d` |

Round-2 verification: 20 agents, 8 claims confirmed.

## Round 3: confirmed defects in round-2 fixes

| Defect | Commit |
| --- | --- |
| Zombie-only process group made macOS `killpg` raise EPERM, masking the Pi error | `08d082a` |
| Cancelled `Runtime.record` could drop a queued mandatory telemetry row | `08d082a` |
| Overlapping approvals rebound a reservation so it never healed | `08d082a` |
| Public-game fetch still ran as the same user; game ID not checked against its path | `f02727d` (HTTP download only; game code runs only in the official worker) |

Round-3 fixes were verified by their own regression tests and the full suite, not
by another adversarial round.

## Found by live runs, not by fixtures

- A CLI model wrapped a correct Spec in a markdown fence (`a7f25b9`).
- Inferred `input_schema`/`output_schema` were not valid JSON Schema objects; the
  operator corrected them at review. Inference-side schema validation is open.
- The game prompt's shorthand action format produced `{"name": ...}`; the prompt
  now shows the exact shape per strategy, tested to parse (`b12708a`).
- A fixed 30 s inference limit was too short for CLI latency; now 120 s.

Live ARC-AGI-3 preview through Claude Code (Haiku 4.5, `online_preview` binding,
five public games, 8 actions, floor 0): before the prompt fix, rollouts ended on the
first malformed decision (`tuning_failed`); after it, tuning passed but Stage 4
still reported `primary_floor_failed` / `primary_evaluation_failed` after 25
minutes. With floor 0 that points at an evaluation error, not a low score. It was
not diagnosed; live runs were paused. No live harness has been licensed on ARC.
The live sentiment example (`examples/live/`) did license: 12/12 held-out cases.

## Still open

- The compiler's own blocking work inside `compile()` still runs on the event loop.
- `Registry.install` holds its lock while copying, so a server install can make
  other requests wait up to 5 s and then get `storage_busy`.
- A telemetry lock taken after the pre-run probe still spends an ephemeral artifact.
- A POSIX double-fork daemon that orphans itself before cleanup can escape the reap.
- Gateway mode does not count scorecard runs (resets are refused at step instead).
- Why the live ARC Stage 4 evaluation fails (above).
- Codex adapter has not run live (quota exhausted until 2026-10-01).
- Live model quality at scale, Windows CI and competition compliance are unverified.
