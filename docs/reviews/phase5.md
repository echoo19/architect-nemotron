# Independent Phase 5 review

Status: **no open concrete review blocker**. P5-B1 through P5-B5 and the supplementary concurrent tool-gate finding are closed by current code and independent rechecks. Phase exit still requires the integrator's final complete suite, static checks, coverage and hardened dogfood evidence. Reviewer ownership was this document only; no production or test files were changed.

## Authority and scope

Read the full source `docs/build-prompt.md`, current `docs/interfaces/phase5.md`, `docs/interfaces/token-policy.md`, `.jakeos/index.md` and pinned Pi research. The latest user instruction overrides historical token-budget clauses: **tokens are usage/efficiency measurements, never product quotas**. Dollar/time safeguards remain. Provider technical capacities are not artifact budgets. Earlier phase reviews remain historical; this review does not restore their superseded token requirements.

Inspected actual Pi Python/Node/extension code and package pins; Runtime/RunContext and sandbox integration; pipeline execution; orchestration; feature-builder capability; profiles/CLI; model capacity/pricing changes; and real offline exit/dogfood scripts/tests.

## Reviewer execution evidence

These are reviewer native runs, not substitutes for the integrator's final full run:

- Initial integrated Pi/pipeline/orchestrator/profile/CLI/adversarial subset: **177 passed, 4 failed in 64.19 s**. Four failures were two duplicated pipeline defects, now closed.
- Focused corrected pipeline and licensed monetary batch gates: **5 passed, 81 deselected in 2.90 s**.
- Real Services/private-floor read/write/edit regression recheck: **3 passed, 54 deselected in 3.36 s**.
- Token-policy migration subset: **418 passed, 4 failed in 15.04 s** during migration. Remaining failures were stale sandbox quota/default-128 expectations, not a reason to restore token caps. Current relevant sandbox cases pass below.
- Two-layer feature creation and fixture-checkout exit: **20 passed in 40.66 s** via `uv run python -m pytest`.
- Final hardened Pi/sandbox/adversarial/native-exit selection through the README command `uv run pytest`: **32 passed, 220 deselected in 37.87 s**.
- Independent Node repository-shadow regression: **1 passed in 1.01 s**.
- Reviewer real-Pi monetary probe originally wrote after a handled nested budget denial. Current repeat reports `WALLET_EXHAUSTED True POST_DENIAL_WRITE False`.

The independent verifier separately reproduced the defects, including a licensed-Runtime monetary attack and real-profile private-floor attacks. Its findings were not treated as a substitute for the reviewer's rechecks.

## Closed findings

### P5-B1 — Required pipeline evidence was silently truncated: CLOSED

Generic character thinning truncated a 5000-character extraction and dropped `steps.solve` before the final verifier. The current exact pipeline envelope preserves `original`, `previous` and every declared `steps` output. Both owner and independent regressions pass. Dollar/time and protocol bounds still apply; lost evidence is not silently accepted as a different composite task.

### P5-B2 — Explicit verifier output schema was overridden: CLOSED

Every object-shaped verifier result was incorrectly forced through `{passed, reason}`, rejecting valid custom `{decision}`/`{difference}` outputs. Built-in planner/verifier shape checks now apply only to the generic object schema. Custom schemas, including constraints without a `properties` key, are authoritative. Implicit pipeline verification supplies and checks its explicit standard verdict, requires `passed=True`, then releases the previous payload under the parent schema.

### P5-B3 — Handled nested money denial allowed a later Pi tool: CLOSED

A real Pi assistant batch `[compile_agent, write]` spent $0.04 under a $0.07 wallet. A $0.04 nested quote was denied, but the callback returned a normal compile rejection. The subsequent write ran before the next model call noticed exhaustion. Final rejection was too late to undo its side effect.

Current `RunContext._check` checks all ancestor failures/dollars/time and active-wallet exhaustion/deadlines. Pi checks before every RPC and after model/compiler calls. The same monetary probe now refuses the write; the independent licensed-Runtime regression also passes. No token hard stop was added.

### P5-B4 — Repository grant exposed private authority: CLOSED

The initial repository-wide grant included default/custom signing state, configured evaluation fixtures and active executable source when Architect's own checkout was granted.

The independent real-Services regression exposed synthetic held-out expected labels through read, and changed the configured floor to `[]` through write/edit. These were actual Pi tools, not simulated filesystem operations.

Current protection has two layers:

- Pi automatically denies the canonical signer file, repository `.architect` state, **entire active Architect package**, Python environment/base-interpreter roots and resolved Node executable. Python and the actual TS extension both check canonical private paths. Traversal, alternate expansion, Windows ADS/trailing aliases, symlinks/junctions and hard links are refused.
- `build_services` adds exact profile, floor/smoke/calibration and response-fixture files, operator-denied paths, and specific artifacts/library/registry/telemetry/proposals directories. Returned `Services.profile` carries the effective restrictions, so FeatureBuilder's scoped backend retains them. A blanket workspace denial is deliberately avoided because it can contain authorized disposable evaluation repos.

Node is resolved to an absolute host executable outside the repository; relative/repository PATH entries and implicit Windows cwd search cannot select a model-planted executable. Sandbox `-I` workers import the installed package without inserting the writable checkout parent into sys.path. This closes the corresponding dependency-shadow/fresh-Python-worker source path rather than only hiding key bytes.

The whole active package has **no games/dogfood exception**. New dogfood runs use a separate staged checkout. Final hardened/private/source/import tests pass. Explicit shell remains a separate trusted-host grant, not an isolation promise.

### P5-B5 — Documented pytest command could not collect exit test: CLOSED

The initial test imported uninstalled `scripts.phase5_exit`, failing under README's `uv run pytest` even though `uv run python -m pytest` passed. It now invokes the actual script through a native subprocess and validates JSON output. The final reviewer console-entrypoint test run collects and executes this exit successfully.

## Current boundary checks

### Real Pi, not a substitute coding loop

The locked package versions are Pi coding-agent, agent-core and ai **0.73.1**. Code calls real `createAgentSession`, `session.prompt`, the upstream event loop and filesystem/bash tools. The shipped TS extension is explicitly loaded and evidenced only after handler registration. A bounded parent RPC adapts model responses; Python does not write requested files for Pi.

Parent model calls complete dollar reservation/reconciliation and identity validation before assistant/tool events reach Pi. Tools run sequentially; turn/tool/protocol/output bounds remain. Unneeded retries/compaction, ambient extensions, skills, context, settings, auth and persistent sessions are disabled. HOME/agentDir are disposable; credentials are not copied. Windows uses a kill-on-close job; POSIX uses a new process group. Cancellation/deadline/failure reaps the managed tree. This is not an OS/network jail for explicitly trusted shell code or hostile host mutation.

Repository authority comes from original runtime input, not substituted nested/model task data. Informational `pi-pack.json` does not grant authority: execution rebuilds the bound configuration. Evaluation removes nested compiler capability even when a callback exists. Denied tools return fixed denials.

### Token metrics, dollar/time limits

Budget exposes only dollars/time; legacy input `tokens` is discarded and absent from serialized schema. Runtime and compile wallets meter tokens but do not compare them to a quota. Token quote differences reconcile without rejection when dollar reservation remains valid. Licensing, registry acceptance and orchestration/pipeline clamping do not impose token ceilings.

Normal compiler/preset/judge/Pi requests use `max_output_tokens=None`; the judge's remaining 32-token literal was removed. Positive legacy low-level provider options remain explicit options, not tuning quotas. Required provider output capacities are documented/configurable pricing metadata used for conservative dollar quotes; OpenAI/Ollama omit optional caps by default. Efficiency still uses actual costs and observed token efficiency where dollars are zero.

### Pipelines and lifecycle

Every declared stage executes its real preset/model. Different models require a factory and resolved identity must match. Step schemas/params are spec-bound; nested pipeline definitions are cleared. Calls charge all ancestor contexts before provider awaits; child close/absorb settles state once, including failure. The composite emits one billed runtime row, not duplicate step billing. Full outer Stage 4 licenses the behavior.

Expire consumes one attempt before execution and removes only fixed generated harness/pack/config/diff files after success/failure. Spec/report/consumption receipt and caller repository/output files remain. Repeat delivery fails; installed artifacts remain. Library templates are non-runnable and reuse still obtains a fresh verification report.

### Orchestration, profiles, CLI and feature builder

Decomposition is an actual guarded adapter call with strict bounded JSON/DAG validation. Duplicate IDs/keys, missing dependencies, cycles, forged authority and out-of-grant tools fail. Operator fields clamp model requests. Each task goes through the complete compiler and authenticated runtime delivery; failed dependencies cannot feed success. Shared wallets include decomposition, compilation/judges/rewrites and delivery. Concurrency and nested depth are bounded.

Trusted profiles construct configured evaluators/adapters and reject unsupported game setup, rather than substitute fake evidence. CLI install inference stops for an explicit reviewed file; approval does not come from model JSON and unreviewed request text is not merged into the approved spec. Stores close on exits.

FeatureBuilder uses a task-local backend and a revoked, bounded callback. It checks exact request equality, one invocation and nested depth. It either proposes an installed spec or compiles the exact explicit approved spec. The compiler tool is unavailable during evaluation. Tests exercise both compiled layers, actual Pi write/edit/read, separate Stage 4 reports, approval/depth/money failures and a disabled feature flag with human-review requirement. Capability/output claims never substitute for registry/report authentication.

## Exit evidence and remaining limits

The reviewer ran the real offline exit: model decomposition, ephemeral coder compilation, upstream Pi writes, independent native registry-CLI check, warm template reuse with fresh reports, and generated action-decoder tests in a staged checkout. Feature integration tests independently import generated code and check the disabled flag.

The integrator additionally records an earlier actual-checkout dogfood run in `.architect/phase5-exit-dogfood-2/summary.json`: 29 real Pi file-tool calls, 33 telemetry rows, and 12 native action-decoder passes; public rows are in `examples/telemetry/coding.jsonl`. That historical run predates whole-active-package protection. Current hardened dogfood uses a staged checkout; it must not weaken active-code denial.

All these runs use explicitly scripted fake model responses. They prove integration and safety mechanisms, **not live-provider coding quality, production feature quality or competition compliance**. HTTP/TypeScript delivery and ARC integration are Phase 6 work. Human review and disabled client feature flags remain required for production rollout. Full final acceptance belongs to the integrator.

## Supplementary Claude read-only audit

Read `.architect/claude-phase5-final-audit.json`. Its active-source finding matches P5-B4 above and predates the whole-package/environment fix. Its separate concurrent-wallet finding is not this document's B5 (pytest import): the Pi loop checked before awaiting readline, so a sibling could exhaust the shared wallet during that wait and a later tool/compile branch could proceed on a stale check.

Current code adds `_check_parent(context)` at entry to both tool and compile branches, after the awaited read and before authorization/invocation. Reviewer native real-Pi concurrent regression: **1 passed, 48 deselected in 2.61 s**. It exhausts the money wallet while a real upstream bash tool is running and Python awaits the next RPC; the later write is refused, with only one model and one tool call. This additional finding is closed. The audit's optional-tools note matches the declared operator-grant policy. Its custom-workspace hypothesis does not reproduce in the current exit layout: generated checkout roots are `workspace/delivery` and `workspace/dogfood`, while authority directories are siblings outside those grants. Hardened dogfood now validates the entire staged file manifest against the baseline plus exact expected writes before executing generated tests, and compares intended module bytes to reviewed host output.

Final independent verifier report: **358 passed in 113.35 s** across its owned migrated adversarial suites; Ruff and strict mypy are clean for those files. This includes automatic package/interpreter/Node denial, derived profile wiring and event-controlled sibling-wallet races. Its tests used staged synthetic roots and did not modify live package bytes. This is verifier-reported evidence, distinct from the reviewer runs above and the integrator's required full suite.
