# Forge examples

These files support Phase 9 Forge (contract: `docs/interfaces/phase9-forge.md`).
Everything here runs offline with scripted adapters. None of it is a benchmark
result or a compliance claim.

- `arc_explorer.py`: the committed sandbox language v2 reference harness for
  ARC-style games. State lives in code: a `digest` visited set, an
  untried-option-per-state policy, no-op detection and a per-state memory of the
  model's advice. The first steps (one per option, at most a quarter of the
  action budget) probe untried options with no model call. After that the play
  model is asked for one option per new state, at most once per state; the
  answer is reused on revisits and overridden by an untried live option when it
  is a known no-op. Edge rows and columns that change on almost every step (the
  step or energy bars many public games draw) are detected and cropped away
  before states are keyed and no-ops detected, and click targets on them are
  dropped. The `game_explorer` template in
  `architect/compiler/forge_templates/` is pinned byte for byte to this file by
  a test.
- `scripts/<task_id>.json`: Pi control scripts replayed by `ForgeScript` in
  `architect/bench/oracle.py` (bench `--provider fake`). Each file is a JSON array
  of Pi model turns, `{"text"?: str, "tool_calls"?: [{id, name, arguments}]}`. A
  `write` call can use `content_from` to name a committed source instead of
  inlining it. The game scripts write `arc_explorer.py` and then check, measure
  (with `trace`) and submit. The text and vision scripts check, measure and
  submit the workspace incumbent: the default template, already instantiated
  for the exact binding of the spec.

Rules the scripts obey, like any Forge agent:

- They see only the workspace (`SPEC.md`, `TUNING.md`, `BASELINE.json`,
  `LANGUAGE.md`, `templates/`, `NOTES.md`, `harness.py`) and tool results.
- Tool results are tuning evidence, not a license. Only the parent's Stage 4 on
  held-out data licenses the promoted harness.
- `forge_submit` rejects string literals of 24 or more characters that copy
  tuning inputs or expected values.

These scripts drive the $0 Stage A arms (`bench run --provider fake --tier smoke
--variant default --variant template --variant forge`; the CI `bench-smoke` job
runs them). The fixture-only numbers from that run are in `docs/bench.md`
("Forge and template arms"). They are a mechanism check, not a live or benchmark
result.

The ARC experiment profile is `examples/local/profile-forge.json`. The build
agent is the logged-in Claude CLI; its dollars are API-equivalent, not a bill.
The play model is `granite4.2:8b` at `num_ctx` 16384 with `think` false. The
bench runner keeps online play off for these cells. Stages B and C need the
operator's approval. Run one model at a time and do the memory preflight first
(`architect bench probe`).
