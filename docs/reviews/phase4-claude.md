# Claude Code CLI Phase 4 review

Read-only review by Claude Code2.1.282, actual model claude-opus-5-5. No commands/tests were executed by this reviewer. Findings are reviewed and verified by the integrator, not accepted as test evidence.

I found no sandbox-escape blocker. Within the restricted AST boundary, authored code is never imported, compiled, exec'd or eval'd, and no path I traced lets it reach a Python object beyond JSON values. I found three issues worth fixing (two medium, one low) and some bounded observations. Everything here comes from reading the current files; I didn't run any tests or commands.

## Threat boundary
- **What is supported:** a restricted AST language, not arbitrary hostile Python. `_validated_tree` (`architect/runtime/sandbox.py:200-253`) uses a closed allowlist and rejects everything else before anything runs: decorators, defaults, extra arguments, imports, calls other than `len`/`str`, attribute access other than `context.call`, comprehensions, loops, f-strings, walrus, starred values, slices and `**` in dicts.
- **What executes:** the worker (`_sandbox_worker.py:61-174`) walks the validated tree and interprets it. The only Python object it hands over is `_RPC`, and only to trusted preset code through `execute_preset`. The name, params, components and spec literals must exactly match the artifact (`sandbox.py:186-195`), and the worker checks this again itself (`_sandbox_worker.py:182-183`).
- **What the subprocess adds:** cancellation and reaping, plus some defence in depth: `-I`, an empty temporary working directory, a minimal environment, stderr discarded, and bounded line messages. It is not OS isolation, and the docstrings say so correctly.
- **Budget and authority:**
  - Model calls go through the parent's `RunContext.call`: quote, wallet, per-run limit of 64 calls (`sandbox.py:350-355`), and the whole run sits under `_within_deadline` (`sandbox.py:365`).
  - `_reap` kills and waits even when cancelled twice.
  - The worker binding excludes directory, report and dataset handles (`sandbox.py:306-308`).
  - I found no signer or heldout imports in `rewriting.py`, the registry or the library.

## Findings

**1. Medium — the registry can accept a replacement with a looser budget.**
- **Where:** `_beats` (`architect/registry/__init__.py:94-100`) compares task, schemas, domain, tools and floors. It ignores `budget` (and `model_target`).
- **Effect:** each report is only licensed against its own spec's budget. A candidate with 100× the incumbent's cost ceiling and a slightly better floor score is ranked on quality first and replaces the incumbent. That includes the drift path, `monitor` → `install` (`:289-303`). So any install, including a recompile callback, can silently raise the spending ceiling of an installed name.
- **Contract:** the interface wording ("both reports must independently pass current license/budget gates") is ambiguous, and the code comment at `:95` says budget changes are intended. If that's intended, record it in DECISIONS.md. If not, add this check: the candidate's report tokens, cost and compute must fit within the incumbent's `spec.budget`.
- **Regression test:** install an incumbent with `Budget(cost_usd=0.01)`. Then install a candidate with the same task and floor, `Budget(cost_usd=1)`, `report.cost_usd=0.5` and a higher floor score. The test should expect `not_better` (or a new `budget_regression` code).

**2. Medium — drift detection counts compile-time evidence as field observations.**
- **Where:** `Registry.observe` (`registry/__init__.py:255-268`) filters rows only on spec_hash, version, lifecycle, floor type and floor limit.
- **Which rows get in:**
  - `Runtime._execute` rows always have `floor_score=None`.
  - The rows in the store that do carry scores and match an installed version are compile-time rows:
    - `quality_evaluation` rows from Stage 2 tuning and Stage 4 heldout verification (`runtime/evaluation.py:228-239`). Verification scores the same version (`compiler/verification.py:113`).
    - Stage 3 `rewrite` rows (`compiler/rewriting.py:178-191`), which carry the tuning score and use the accepted proposal's version.
- **Effect:** passing the telemetry store to `observe`/`monitor` feeds the heldout verification score, which is effectively the report score, into the drift window. That contradicts "never historic report scores" and can mask or trigger drift. The current tests only build `task_type="runtime"` rows (`tests/test_registry.py:70`).
- **Fix:** exclude `task_type ∈ {categorical("quality_evaluation"), categorical("rewrite")}`, or ignore rows whose timestamp is earlier than the revision's install time.
- **Regression test:** compile and install, then call `observe(name, store rows)`. Expect `None`, with no observations inserted for those rows.

**3. Low (robustness, not an escape) — one parser failure escapes the `SandboxError` contract.**
- **Where:** `_validated_tree` catches `ValueError`, `TypeError`, `SyntaxError`, `RecursionError` and `UnicodeError` (`sandbox.py:252`).
- **Trigger:** on CPython 3.12 (the project's target), source that is too deeply nested for the parser, such as `"x = " + "-" * 7000 + "1"` (about 7 KB, under the 64 KiB limit), should make `ast.parse` raise `MemoryError("Parser stack overflowed…")`. That happens before the node and depth walk.
- **Impact:**
  - Stage 3 swallows it (`except Exception`, recorded as `unsafe_source`), and `evaluate_cases` records `load_error`.
  - A direct `validate_source` call, and the sandbox warm path in `tune`, get a `MemoryError` instead of `SandboxError`. The compiler only catches `ValueError` around `tune` (`compiler/__init__.py:163`), so compile would crash.
  - Reaching that path needs an authenticated warm template, so this is hard to exploit.
- **Fix:** also map `MemoryError` to `SandboxError("sandbox_invalid_source")`. Optionally reject deep nesting with a cheap pre-scan before parsing.
- **Regression test:** `pytest.raises(SandboxError)` on that source.

## Bounded observations (no action required)
- **Components are only metadata for direct guarded-call programs.** A rewrite without `execute_preset` passes `preset.build(... proposal.components)` (`rewriting.py:262`) while claiming any valid component set, including "required" ones it doesn't actually run. The floor, budget and Stage 4 gates still apply. `component_count` in the report and in the registry tie-break is not tied to behaviour. The interface allows this, but it is worth noting in the review.
- **Native warm literal-dict normalization** (`compiler/tuning.py:91-106`):
  - It sorts every dict whose keys are all string constants, not just literal-valued dicts, so the value order of a dict holding `await` or name expressions is also treated as equivalent. That is broader than the interface text ("literal dictionary").
  - This has no execution consequence today: on the native path the warm code is compared only as an AST and never runs. `_persist` re-emits from the preset with the new spec (`tuning.py:283-293`).
  - `ast.dump` still tells `1`, `True` and `1.0` apart, and the sort is stable, so duplicate keys keep their order. Dicts containing `**` are left unsorted.
  - Suggested tightening: sort only when every value is a constant or container literal, and add a test that a warm source whose `{"a": await …, "b": await …}` value order is swapped gets rejected.
- **Cached licenses:** the sandbox warm path clears `report` and signs a new report in Stage 4 (`tuning.py:249-256`).
  - The library re-authenticates on every read, evicts entries that fail, and marks expire snapshots `.consumed`.
  - The registry accepts only `INSTALL` artifacts that are licensed on disk and re-authenticates the copy.
  - I found no path that transfers a cached report.
- **Heldout:** Stage 3 uses only `floor.tuning` and `smoke_floor.tuning` (`rewriting.py:223`). The proposal prompt contains code, config (including dataset names) and numeric measurement. It contains no labels.
- **Minor non-security points:**
  - An uncaught `LibraryError` from `library.store` or `record_verification` (`compiler/__init__.py:211-215`) would turn a licensed compile into an exception.
  - A registry incumbent whose disk license has become invalid blocks every new install for that name until an explicit `rollback`.

**Verdict:** no blocker at the sandbox or authority boundary. I recommend fixing findings 1 and 2, or explicitly deciding they are intended, before the Phase 4 exit. Finding 3 is a small fix to keep the error contract consistent.


## Integrator disposition
All three findings fixed: installed budget ceilings cannot increase without an explicit manual allowance (monitor never grants it); current activation time excludes compilation evidence and resets rollback windows; parser MemoryError becomes SandboxError. Native dictionary comparison was also restricted to literal-valued maps. Full integrator1184passed/3skipped with cleanruff/mypy; see Phase4 review and PROGRESS.md.
