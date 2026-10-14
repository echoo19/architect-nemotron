# Build prompt: Architect

You are the lead engineer building **Architect**, an agent factory: a Python system that takes a task, writes a spec for it, compiles a bespoke agent harness from presets, tests that harness against a quality floor at a budget, and either installs it (versioned, monitored, recompiled on drift) or runs it once and discards it. The full product spec is at the bottom of this prompt. Read it completely before writing any code. The spec is the source of truth; where this prompt and the spec disagree, the spec wins, and you flag the conflict in PROGRESS.md.

Your job is to build Architect to near completion: a working core package, all seven presets, all four compiler stages, all four floor types, the registry, the harness library, the orchestrator, telemetry, the Pi-based coder backend, an ARC-AGI-3 adapter, and the TypeScript client, with a test suite that proves each piece. Work until the definition of done below is met or you hit a blocker you cannot resolve without me. Do not stop at a scaffold.

## How to work

**Use subagents aggressively, but keep one integrator.** You are the integrator. You own the architecture, the interfaces, the merge, and the final test run. Subagents own bounded pieces with a written contract. Never let two subagents edit the same module in the same phase.

- Before each phase, write the interface for every module in that phase (function signatures, Pydantic models, exceptions) to `docs/interfaces/<phase>.md`. Subagents build to those interfaces. Change an interface only by editing that file first.
- Dispatch one subagent per module. Give each: the interface file, the relevant spec section, the test file it must make pass, and the rule "do not touch files outside your module." Run them in parallel when their modules don't depend on each other.
- After every subagent returns, you run the full test suite yourself. A subagent saying "tests pass" is not evidence. Only your run counts.
- Keep a **verification subagent** separate from build subagents. It gets the spec and the code and writes adversarial tests: floor gaming, budget overrun, content leaking into telemetry, a cached harness skipping verification. It never fixes code; it only reports.
- Keep a **review subagent** for every phase: reads the diff against the spec section and lists every deviation. You decide what to do with each one.

**Maintain three files at the repo root at all times.**
- `PROGRESS.md`: what's done, what's in flight, which subagent has what, test status. Update after every phase and every blocker.
- `DECISIONS.md`: every choice the spec didn't make for you, with one line of reasoning. Model names, library choices, default budgets, cap values.
- `QUESTIONS.md`: things only I can answer. Keep building on a stated assumption and mark it; never block on a question you can work around.

**Ask me only for:** real Lectern data or logs (use synthetic fixtures until then), API keys, the ARC-AGI-3 competition rules on submission-time tooling, and any decision that would cost more than a day to reverse. Everything else, decide and record.

## Stack and conventions

- Python 3.12, `uv` for env and packaging, `pyproject.toml`, package name `architect`, MIT license.
- Pydantic v2 for every schema. Strict typing, `mypy --strict` clean, `ruff` clean.
- `pytest` with `pytest-asyncio`. Target 85% line coverage on the core; 100% on the gates (verify, budget, registry acceptance, telemetry schema).
- A **deterministic fake model adapter** (`architect.models.fake`) that returns scripted responses from fixture files. Every unit and integration test runs against it. Real adapters (Anthropic, OpenAI-compatible, Ollama) are tested by a separate `tests/live/` suite that is skipped without keys.
- All harness artifacts are code plus config on disk under `artifacts/<spec_hash>/<version>/`. Nothing lives only in memory.
- Telemetry is append-only JSONL plus a SQLite index. The schema is a Pydantic model with no free-text fields. A test asserts that no field can hold content longer than 64 chars except `run_id` and `spec_hash`.
- No Lectern-specific logic anywhere in `architect/`. Lectern examples live in `examples/lectern/` and are excluded from the package. A test greps the package for "lectern", "tutoring", "academy", "report card" and fails if found.
- Commit after every green phase with a message that names the phase. Tag `v0.1.0` at definition of done.

## Build order

Each phase has an exit test. Do not start the next phase until the exit test passes on your own run.

### Phase 1: Skeleton, spec schema, runtime, telemetry
- Repo layout: `architect/{spec,presets,compiler,floors,runtime,models,registry,library,orchestrator,telemetry}`, `client-ts/`, `examples/`, `tests/`, `docs/`.
- `Spec` Pydantic model per the spec section "Spec schema," plus `HarnessArtifact`, `TestReport`, `Budget`, `FloorDefinition`, `Lifecycle` enum.
- Model adapter interface with the fake adapter and the three real ones. Adapters count tokens and cost per call; cost tables are config, not code.
- Runtime: execute a harness against an adapter under a budget guard that hard-stops on overrun and emits a telemetry row on every run, including failures.
- **Exit test:** compile nothing yet; run a hand-written toy harness through the runtime against the fake adapter, prove the budget guard stops it mid-run, and prove the telemetry row is schema-valid and content-free.

### Phase 2: Presets and floors
- Seven presets as code: `extractor`, `synthesizer`, `recommender`, `coder`, `explorer`, `planner`, `verifier`. Each declares `tunable_params` with ranges and `removable_components`. `coder` is a Pi extension pack generator (see Phase 5); stub it here with the interface only.
- Four floor evaluators behind one interface: `HeldOutTasks` (fixtures with checkers), `HumanEditRate` (reads a log format you define; synthetic fixture with 200 rows), `LLMJudge` (fixed rubric, fixed judge model, threshold, requires a calibration set or refuses to run), `HeldOutGameScore` (takes an environment interface; stub environment in this phase).
- Every floor evaluator splits data into `tuning` and `held_out` at construction and exposes only `tuning` to the compiler. `held_out` is accessible only by stage 4. Enforce this in code, not convention.
- **Exit test:** each preset instantiates and runs a trivial task on the fake adapter; each floor evaluator scores a fixture and refuses to expose held-out data to anything but the verifier.

### Phase 3: Compiler stages 0, 1, 2, 4
- Stage 0: spec inference from three inputs: a plain-language request, an `Environment` that can be probed, and an orchestrator subtask. Output is a `Spec` with a `provenance` field marking each field as `inferred` or `user`. User fields override. Installed lifecycle returns the spec for approval; ephemeral compiles through.
- Stage 1: deterministic preset selection from domain tag and output shape, with library lookup first (library stubbed until Phase 4, returns no match).
- Stage 2: parameter search over the preset's tunables against the tuning set. Cheapest configuration that clears the floor wins. Search strategy is pluggable; ship grid and a simple successive-halving.
- Stage 4: verification on held-out data, produces `TestReport`. The report is the only thing that can set `HarnessArtifact.licensed = True`.
- "Cheapest that passes": model ladder from config; compile against each rung, return the cheapest licensed artifact.
- **Exit test:** from the request "summarize a table of numbers into two paragraphs for a parent," stage 0 infers a synthesizer spec with an edit-rate floor, stages 1 to 4 produce a licensed artifact on the fake adapter, and a second compile with a lower budget produces an artifact with fewer components or fails cleanly with a report explaining why.

### Phase 4: Stage 3 rewriting, registry, harness library
- Stage 3: an LLM proposes diffs to harness code (add or remove a component, change control flow, compress prompts). Every diff is applied in a sandbox, regression-tested against the floor on the tuning set and a fixed smoke set, and kept only if it passes and cuts cost. Hard cap on iterations from config. Rewrites can never read held-out data.
- Registry: installed agents with version, spec hash, test report, drift monitor (rolling window over telemetry floor scores, plus model-change detection), and a recompile trigger. New versions ship only if their report beats the old one. Old versions kept; rollback is one call.
- Harness library: every licensed artifact is stored with a similarity key (domain, output shape, tool set, model). Lookup returns near matches for warm-starting stage 2. Reused harnesses still go through stage 4 on the new task. Reuse count promotes to install candidate; two verification failures evict.
- **Exit test:** a compile that fails stage 2 on budget proceeds to stage 3, a scripted rewrite in the fixtures removes a component, the artifact passes verification, and telemetry shows the diff was accepted. Separately: install an agent, feed the drift monitor declining scores, prove a recompile fires, and prove a worse recompile is rejected. Separately: compile the same spec twice, prove the second compile warm-started from the library and still ran stage 4.

### Phase 5: Orchestrator and the Pi coder backend
- Orchestrator: decomposes a goal into subtasks (LLM call), compiles one ephemeral agent per subtask through the full compiler, runs them with per-agent budgets and hard stops, collects results. Library reuse across similar subtasks is the point; test it.
- Pi backend: the `coder` preset emits a Pi extension pack (tools, context policy, prompt fragment, sub-agent rules) for `pi-agent-core`. Pin the Pi version in `DECISIONS.md`. Architect calls Pi as a subprocess or via its TS API; it never reimplements the coding loop. If Pi's extension API doesn't support something the preset needs, record it in `QUESTIONS.md` and ship the pack without it.
- Dogfood: use the orchestrator to build at least one module of Phase 6. Log it as a result in `PROGRESS.md` with the telemetry rows.
- **Exit test:** the orchestrator takes "add a CLI that prints the registry" as a goal, decomposes it, compiles coder harnesses, runs them on a real repo checkout, and the CLI exists and passes its tests. Two similar subtasks share one harness from the library.

### Phase 6: ARC-AGI-3 adapter, TS client, examples, docs
- ARC: an `Environment` adapter for the ARC-AGI-3 interface. If the official environment package is available, use it; if not, build a local grid-game simulator with three toy games and a matching interface, and mark it as a stand-in. Game-level flow: probe, infer spec, compile from `explorer` / `planner` / `hypothesis_tester` (add this preset now), play under a compute budget. Competition-level flow: a script that reads a competition description file and compiles an installed harness whose job is to spawn game-level agents under the cap. Runtime on the small model does selection and tuning only; a config flag disables stage 3.
- TS client in `client-ts/`: `compile`, `run`, `status`, `recompile` over HTTP against a FastAPI server in `architect.server`. Nothing else. Typed, tested with a mocked server.
- `examples/lectern/`: three specs (report cards, test digitizer, content recommender) with synthetic fixtures and the edit-rate log format documented. The test digitizer example must demonstrate stage 0 inferring a multi-stage pipeline (vision extract, solver re-derive, verifier diff) from the one-sentence request in the spec.
- `docs/`: architecture overview, how to write a spec, how to add a preset, how to add a floor, telemetry schema, and a "what Architect will refuse to do" page (run unlicensed artifacts, expose held-out data, exceed budget, store content).
- **Exit test:** the three Lectern examples and the ARC game-level flow all compile to licensed artifacts on the fake adapter end to end; the TS client compiles and runs one of them against the live server.

## Testing standard

- Every gate has a test that tries to break it: budget overrun, unlicensed artifact execution, held-out access from the compiler, content in telemetry, cached harness skipping stage 4, worse recompile replacing better, rewrite cap exceeded. These tests are written by the verification subagent, not by whoever built the module.
- Every preset has a golden-path test and a "floor fails" test.
- Every compiler stage has a test for its failure mode: stage 0 with a garbage request, stage 1 with no matching preset, stage 2 with an unreachable floor, stage 3 hitting the cap, stage 4 with a regression.
- One end-to-end test per domain (Lectern synthesizer, coding via orchestrator, ARC game) that runs the full pipeline on the fake adapter in under 60 seconds.
- Coverage report committed to `docs/coverage.md` at definition of done.

## Definition of done

- All six phase exit tests pass on your run, in CI (GitHub Actions on push), on a clean clone.
- `PROGRESS.md` shows every spec component as done, stubbed with a reason, or blocked with a question.
- `DECISIONS.md` and `QUESTIONS.md` are current.
- The three Lectern examples, the coding dogfood run, and the ARC game-level run each have telemetry rows in `examples/telemetry/` that I can load as a dataset.
- `v0.1.0` tagged. A `README.md` that states what Architect is in the spec's one-liner, shows a 20-line compile example, and lists what it refuses to do.

## Things you must not do

- Do not let the model decide whether an artifact is licensed. Only stage 4 code sets that flag.
- Do not let any compiler stage read held-out data. Enforce in code.
- Do not put content in telemetry. Enforce by schema.
- Do not put Lectern logic in the package.
- Do not write a coding agent. Use Pi.
- Do not add a preset, floor, or domain not in the spec without writing it in `QUESTIONS.md` first and continuing without it.
- Do not report "done" for a phase you haven't run the tests on yourself.
- Do not stop to ask about anything you can decide and record.

---

# THE SPEC (source of truth)

# Architect Spec — v0.2
*The agent factory: compiles bespoke agents from specs it writes itself for Lectern, general long-running work, and ARC-AGI-3. Last updated: Sep 24, 2026.*

## The Thesis
Agents today are prompts with tools attached. Architect treats an agent as a compiled artifact: a spec goes in, a tested harness comes out, and it is not allowed to run until it clears a quality floor at a budget. Some agents get installed permanently into software. The rest run their task and expire.

**One-liner:** "Give Architect a task, a model, and a quality bar. It builds the agent, tests it, and either installs it or throws it away when the job is done."

**The claim we make (and no bigger):** first agent compiler with production quality floors from human edit rates, measured across models on real business tasks. Never "first agent factory," never "novel." The moat is Lectern's data, not the architecture.

## Definitions
- **Spec:** what the agent does, its inputs and outputs, its floor, its budget, its target model, its lifecycle.
- **Harness:** everything around the model. Prompts, tools, control flow, context policy, verifier, model binding, budget guard. Stored as code plus config.
- **Agent:** a harness bound to a model, with a passing test report. No report, no agent.
- **Floor:** the minimum quality an agent must hit before it can run. Four kinds, below.
- **Budget:** tokens, dollars, or compute per run. Compilation minimizes cost subject to the floor. Cost never buys its way under the floor.
- **Lifecycle:** `install` (versioned, monitored, recompiled on drift) or `expire` (run once, log, discard).

## Architect Is a Harness
Architect is itself a harness wrapped around an LLM. Its output is other harnesses. Three rules keep that from becoming circular:
- **The model makes the judgment calls only:** turning a task into a spec, choosing a decomposition, proposing a rewrite.
- **The gates are code:** floor verification, budget accounting, the registry, drift detection, the acceptance rule. The model never decides whether an agent it built is good enough. This is the frozen outer layer, and it is why the factory can be trusted.
- **It builds both kinds of agent.** Heavy installed ones with multiple models and steps (the test digitizer: a vision model for layout, a solver model to re-derive keys, a verifier that diffs them, each on its own budget), and disposable ones spun up on the spot to match a Codex-class or Claude-Code-class coder for a single task.

The `coder` preset is a Pi extension pack, not a coding agent written from scratch. Architect's job is to fit and thin it for this repo and this model in seconds, not to build a better coder in general.

## Non-Goals
- Not a chat framework, not a visual builder, not a prompt library.
- Not a general orchestrator first. The orchestrator is a thin layer that calls the compiler.
- Not full program synthesis of harnesses from nothing in v1. Rewriting starts from a preset, always.
- Not a benchmark paper. The paper falls out of the logs; it is not the product.

## Architecture
Python core package `architect`, MIT. Lectern is a consumer through a thin TypeScript client over HTTP. No Lectern logic in the core, ever.

1. **Spec schema.** Pydantic model. Task description, input schema, output schema, floor definition, budget, model target (or "cheapest that passes"), lifecycle, domain tag.
2. **Preset library.** Named harness archetypes as code. Launch set: `extractor` (strict schema, self-check), `synthesizer` (data to prose, edit-rate floor), `recommender` (retrieval plus preference memory), `coder` (repo-aware, test-run loop), `explorer` (probe an unknown interface), `planner` (search once rules are known), `verifier` (checks another agent's output). Each preset declares tunable parameters and which components are removable.
3. **Compiler.** Four stages, each gated. Select the nearest preset. Tune its parameters against the floor. Rewrite harness code where tuning is not enough. Verify against the floor and budget. Output is the artifact plus the test report.
4. **Floor evaluators.** One interface, four implementations: held-out tasks with checkers, human edit rate from logs, LLM judge with threshold and a fixed rubric, held-out game score. A spec names one primary floor and may name a secondary.
5. **Runtime.** Executes a harness against a model adapter. Adapters: Anthropic, OpenAI-compatible, Ollama (local, offline). Every run emits a telemetry row.
6. **Registry.** Installed agents with version, spec hash, test report, drift monitor, and recompile trigger. Ephemeral agents are never registered; only their logs are.
7. **Harness library.** Cache of every compiled harness with its spec, test report, and a similarity key (domain, output shape, tool set, model). A new spec searches the library first; a near match warm-starts stage 2 from that harness instead of a preset. Reuse never skips stage 4: the cached harness must pass verification on the new task's held-out set. A harness reused N times is flagged as an install candidate; a harness that fails verification twice is evicted.
8. **Orchestrator.** Decomposes a task, compiles one ephemeral agent per subtask (each with its own harness and possibly its own model), runs them, collects results. Spawning is cheap; the compiler is the work.
9. **Telemetry.** Anonymized by construction. Task type, model, harness version, tokens, cost, wall time, floor score, accepted/rejected, component set. Never content.
10. **TS client.** Compile, run, status, recompile. Nothing else.

**Coding substrate: Pi.** The `coder` preset and all ephemeral coding agents run on Pi (pi-agent-core, MIT). Architect does not write a coding agent; it emits a Pi extension pack (tools, context policy, prompt fragment, sub-agent rules) fitted to the repo and model, and Pi executes it. Pi is one backend behind the runtime, never Architect's own runtime.

## Compilation
- **Stage 0, infer the spec.** Architect writes the spec itself from whatever it is given: a plain-language request ("I want an AI feature that does X"), an environment it can probe (an ARC game's interface and a few moves), or a subtask handed down by the orchestrator. It proposes task description, input and output schemas, pipeline shape (single agent or multi-stage, which models), floor type, and budget. A user-supplied spec is always allowed and any field the user sets overrides the inferred one. Installed agents show the inferred spec for approval before compiling; ephemeral agents compile straight through.
- **Stage 1, select.** Match spec domain and output shape to a preset, or a near match in the harness library. Deterministic.
- **Stage 2, tune.** Search preset parameters (context window policy, verifier depth, retries, prompt terseness level, tool subset). Accept the cheapest configuration that clears the floor on the evaluation set.
- **Stage 3, rewrite.** Only if tuning fails the floor or the budget. An LLM edits harness code: add a component, remove one, change control flow, compress prompts. Every edit is a diff, regression-tested against the floor and a fixed smoke set. Edits that pass and cut cost are kept; anything else is rejected. Hard cap on rewrite iterations per compile.
- **Stage 4, verify.** Final run on held-out evaluation data never seen during tuning or rewriting. Produces the test report. The report is the license to run.
- **Model-aware by default.** A spec with model target "cheapest that passes" compiles against a ladder of models from small local to frontier and returns the cheapest passing agent. This is where the harness thins out for stronger models and thickens for weaker ones.
- **Acceptance rule:** floor first, budget second, simplicity third. Fewer components wins ties.

## Quality Floors
- **Held-out tasks.** Task set with programmatic checkers. Floor is pass rate. General coding and extraction.
- **Human edit rate.** Fraction of outputs a human changed before use, from Lectern logs. Floor is a ceiling on edit rate for the task type. The evaluation set is a replay of recent real inputs, outputs judged against what the human shipped.
- **LLM judge.** Fixed rubric, fixed judge model, threshold. Used only where no checker or human signal exists, and always paired with a small human-graded calibration set.
- **Held-out game score.** Score on preview games not used during compilation. ARC only.

## Lifecycle
- **Install.** Agent gets a version, the spec hash, the test report, and a drift monitor. Drift is a floor score trending toward the limit over a rolling window, or a model change. Drift triggers a recompile; the new version ships only if its report beats the old one. Old versions are kept for rollback.
- **Expire.** Compiled at runtime, run, telemetry logged, harness discarded. If the same spec hash shows up repeatedly, the registry flags it as an install candidate.

## Target Domains

### Lectern (installed)
- **Entry point:** a feature request in plain language. "I want an AI feature that turns uploaded tests into Bluebook-style online tests with answer keys." Architect infers the spec, proposes the pipeline, compiles, and installs behind a flag. The user reviews the spec, not the harness.
- **Report cards first.** Preset `synthesizer`, floor = edit rate, budget = current pipeline cost. Human review stays in the loop behind a feature flag until the compiled agent beats the current pipeline on cost for one month with edit rate under the floor.
- **Test digitizer second.** The proof that stage 0 works on a heavy case: from the request above, Architect should infer a multi-stage harness on its own (vision model for layout and question extraction, solver model that re-derives each answer, verifier that diffs the two, strict output schema) and pick preset `extractor` with correctness over cost. Floor = answer-key accuracy on a held-out set of tests with known keys.
- **Content recommender third.** Preset `recommender`, floor = client acceptance rate. Proves the factory is not a tutoring tool.
- **Month-end swarm.** Orchestrator compiles per-account ephemeral agents for reports, reconciliation, and follow-ups. First demo of many harnesses running at once.

### General long-running work (ephemeral, with reuse)
- **Entry point:** a large goal. The orchestrator decomposes it, and for each subtask compiles a harness (a Pi extension pack for coding) fitted to that subtask, the repo, and the model, then dispatches a subagent on it. Subtasks that look alike get the same harness from the library, now and in future runs; the harness is only recompiled when the library match fails verification.
- Long runs use fixed budgets per agent and a hard stop. No agent runs past its budget; the orchestrator recompiles or reassigns instead.
- This is also Architect's own development loop: Architect compiles the agents that build Architect. Log it; it is a result.

### ARC-AGI-3 (two levels)
- **Game level (ephemeral):** the agent sees a game, probes its interface with a few moves, infers a spec, and compiles a subagent for that game from `explorer`, `planner`, or `hypothesis_tester`. Games that resemble earlier ones reuse from the library.
- **Competition level (installed):** Architect reads the competition itself (rules, action space, observation format, preview games, compute cap, submission constraints) and forges one permanent harness that is the submission: the thing that spawns and manages the per-game subagents within the cap. This harness is compiled offline during development with big models allowed, and is installed with a drift monitor against the preview set.
- At runtime the small offline model does selection and parameter tuning only. No rewriting under the cap. Confirm competition rules on what may run at submission time.
- Floor = held-out preview game score. Budget = the compute cap, with compile time counted.
- Value to the project: the thick end of the harness curve, and one row in the same results table as Lectern.

## Telemetry Schema
One row per run. `run_id, timestamp, domain, task_type, spec_hash, harness_version, lifecycle, preset_origin, component_set, model, model_params, tokens_in, tokens_out, cost_usd, wall_time_s, floor_type, floor_score, floor_limit, accepted, drift_flag`. Numerics and categoricals both present; no content fields exist in the schema, so anonymization cannot be forgotten.

## Decision Rules
- Floor beats budget beats simplicity, always in that order.
- Client delivery never runs on a newly compiled agent without the flag and human review.
- Core stays Lectern-free. The moment a Lectern-specific branch appears in `architect`, it moves to the client.
- Rewrite stage is capped. If compiles routinely hit the cap, the preset is wrong, not the cap.

## Watch Items
- **Scope.** Seven presets, four floors, three domains, an orchestrator. Anything new replaces something.
- **Floor gaming.** Rewrites will find ways to pass the evaluation set without getting better. Held-out data is sacred and never touched by the compiler.
- **Judge floors.** LLM judges drift and flatter. Keep them paired with human calibration or drop them.
- **Privacy.** The telemetry schema has no content fields. Keep it that way.

## Open Questions (record your assumption in DECISIONS.md and proceed)
- Which small offline model for ARC, and does it write usable harness parameters at all?
- Does the edit-rate floor need a minimum sample size per task type before it is trusted? Default to 30.
- Is "cheapest that passes" worth running on every compile, or only when a spec asks for it? Default: only when asked.