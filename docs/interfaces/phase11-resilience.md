# Phase 11: resumable long runs, live-model protocol fixes, and the ARC plan

Status: spec and handoff (2026-09-29). Implementation in progress; see the state
section at the end. Where this text and the code differ after implementation, the code,
`docs/usage.md` and `docs/bench.md` are authoritative.

## 1. Why

Long runs on this host are stopped from outside: Claude Code reaps background shells
when free memory is critically low, and the operator may be using the machine (games,
browsers, other Claude sessions). Every stop so far restarted work from zero: a 20-minute
test suite, a 35-minute ARC bench cell. The user asked that "things don't completely have
to restart every time". Separately, two live-model defects block real Forge and local ARC
play; both are fixed in the working tree and must be verified and committed.

Fixed properties (unchanged): Stage 4 alone licenses; tokens are metered, never capped;
telemetry and bench rows are content-free; local GPU use is for ARC only, one light run at
a time, model unloaded after (the user's standing rule).

## 2. Resumable test suite (`scripts/run_tests_resumable.py`)

- Splits the suite into batches of test files (default 12 files per batch, sorted, so the
  split is deterministic), runs each batch as one pytest process, and appends one JSON line
  per finished batch to a state file (default `.architect/test-state/<run-id>.jsonl`):
  `{"batch": i, "files": [...], "exit": code, "passed": n, "failed": n, "skipped": n,
  "seconds": s}`.
- `--resume <run-id>` skips batches already recorded with exit 0 and re-runs failed or
  missing ones; a new `<run-id>` starts fresh. `--basetemp` defaults to `C:/t/rt/<run-id>`
  (forward slashes; short, under the registry path guard) and each batch uses its own
  sub-directory so a stopped batch never corrupts the next.
- It prints a final summary with totals across all recorded batches and exits non-zero if
  any batch failed. It runs from any shell; nothing depends on Git Bash.
- `--memory-floor-gb N` (default 3): before each batch, if free RAM is below N, it waits
  (polling every 30 s) instead of starting and being reaped. After `--wait-limit-min`
  (default 30) it pauses with exit code 3 and the `--resume` hint rather than starting
  anyway: a long wait means the operator is using the machine.

## 3. Resumable bench (`architect bench run --resume <bench_id>`)

- Reuses `<out>/<bench_id>/`, reads the existing `rows.jsonl`, and skips every cell whose
  identity (task, model, variant, repeat, seed) already has a row; runs the rest and appends.
  The run must match the original configuration (suite version, digest, seed, tier, arms);
  a mismatch is refused with `bench_resume_mismatch` before any spend.
- A row written for a cancelled or watchdog-stopped cell (failure `cancelled`) does not count
  as done; it is re-run.
- The summary covers all rows (old and new). CLI and session both accept it; ci_assert is
  unchanged.

## 4. Live-model protocol fixes (in the working tree, to verify and commit)

- **Pi tool protocol** (`architect/backends/pi.py`): the parent prompt now states that every
  reply is exactly one JSON object, `{"tool_calls": [...]}` or `{"text": "<final>"}`, lists
  the granted tools with bounded schemas, and gives an example of each. Envelopes inside code
  fences or next to prose are extracted on the parent side (bridge unchanged and strict).
  A reply that names a tool or mentions `tool_calls` without a valid envelope gets at most two
  fixed repair turns, metered like any call. Evidence: a live claude-sonnet-5 Forge session
  replied `read` and ended with zero tool calls; the new tests replay exactly that.
  Follow-up from the live check (three runs of the same Forge compile): one run
  succeeded end to end (read, write, check, dry run, measure, submit, final envelope),
  one ended when the model answered prose mid-session ("I don't see a specific request
  yet") with no submission. The prompt now sends a trimmed context (system prompt and
  messages without usage, timestamps or provider labels; tools listed once) and ends
  with a fixed reminder of the task and reply format. `PiConfig.repair_prose` re-asks
  any non-envelope reply while tools are listed (at most two repairs); Forge sets it,
  other Pi uses keep "prose is final".
- **Native tool calls (W3, 2026-10-04).** The JSON envelope above is now the fallback,
  not the only transport. When the adapter bound to a Pi run streams native chats
  (`ChatStreaming`: the Anthropic, OpenAI-compatible and Ollama HTTP adapters, and a
  session's `ObservedAdapter` around one, which is an `ObservedChat`), each parent turn is
  one `RunContext.stream_call`. Pi's granted tools go to the provider as its own tool
  definitions and Pi's context as chat messages; the reply reaches the bridge as the same
  control envelope, so `bridge.mjs` is unchanged. Adapters without native tools (the
  `claude` and `codex` CLIs and plain completion adapters) keep the envelope and its
  repairs. On the native path, a reply whose call arguments are not one JSON object is
  re-asked once with fixed errors in their place, and a reply cut at the output limit
  runs none of its calls and is re-asked once with a short instruction (a server can
  close a cut call's arguments, so they may parse and still be incomplete). Malformed
  arguments are never sent back, and a cut reply is not shown again at all. Evidence: a
  live A/B on 2026-10-03 gave 8 of 8 usable code writes as
  native calls and 2 of 8 through the envelope, and the three Forge sessions of
  2026-10-04 on Nemotron sent 304 tool calls with none malformed
  (`docs/feedback/probes/2026-10-04-forge-native-g1.md`; development runs, not evidence).
- **Structured decoding** (`ModelRequest.response_schema`): Ollama sends it as `format`
  (constrained decoding), OpenAI-compatible servers as strict `json_schema`, Anthropic and
  CLI adapters ignore it. Game decision, repair and review calls pass the exact decision
  schema (offered actions as an enum, ACTION6 coordinates bounded by the grid). parse_decision
  stays the authority. Evidence: local granite4.2:8b licensed but failed delivery with
  `game_decision_invalid` on real public ARC games.

## 5. ARC plan after the fixes

1. Verify both fixes with the resumable suite; commit and push; CI green.
2. Live Forge check on a text task, cloud agent only (no GPU): Forge must record checks,
   measures and submissions > 0.
3. One light local ARC run (granite4.2:8b, 5 games, 40 actions, seed 7, arms default and
   forge) under the watchdog, resumable with `--resume`. Report per-arm licensed,
   delivery result, score, distinct states, revisit rate and calls per action.
4. Only if delivery now works: widen to 80 actions, then consider gemma4:26b at a smaller
   context, each as one light run with the user's GPU rule.

## 6. State at the time of writing (handoff)

- Done: the resumable test runner (Section 2; run `p11a` finished 4890 passed, 0 failed,
  72 skipped after resuming only its two failed batches). The live-model protocol fixes
  (Section 4) are committed in `2844bb5`. The resumable bench (Section 3) is implemented:
  `run.json` per run, `bench run --resume <bench_id>`, session `bench resume <id>`, and the
  codes `bench_resume_mismatch` and `bench_resume_unsupported`. Routed runs cannot resume
  because one route log spans the whole run. `host_memory_low` rows count as not done,
  like `cancelled`.
- Live Forge check (Section 5, step 2) done: 3 of 3 runs submit and license at 1.0 after
  the prompt hardening in `86f3be2`.
- CPU-only ARC work while the GPU is held: `scripts/arc_probe.py`, `ClockBands` in the play
  loop, and the clock-aware `game_explorer` template (see PROGRESS.md, Phase 11).
- Next: the one light local ARC run (step 3), when the operator allows GPU use.
- Best ARC-AGI-3 score so far: 0.0 on every real public preview run.
- Diagnostic profile for the live Forge check: `{scratch}/forgediag/profile.json`
  (Lectern synthetic fixture play model, claude-sonnet-5 Forge agent, smoke = the floor file;
  diagnostic only) with the reply tap `{scratch}/forgediag/tap.py`.
- Stage B scripts: `{scratch}/bench10/run_stageB_forge.sh` (watchdog stops on sustained
  low memory or above 84 C; cleanup always unloads the model and stops Ollama).
