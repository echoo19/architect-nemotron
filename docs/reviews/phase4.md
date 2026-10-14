# Independent Phase 4 review

Status: **no open Phase 4 review blocker**. Warm tool-set reuse, floor-role drift separation and the additional accepted safeguards are closed on current code and local rechecks. Final phase exit remains the integrator's responsibility. This reviewer changed no implementation or test files.

## Scope and evidence

Reviewed `docs/build-prompt.md`, current `docs/interfaces/phase4.md`, `.jakeos/index.md`, sandbox validator/interpreter/protocol, rewrite acceptance, registry/library, artifact mode binding, runtime/evaluation dispatch, warm rebinding and compiler integration. Also checked the accepted findings in `docs/reviews/phase4-claude.md` against live code, not just the external review's claims.

- Reviewer Phase 4 sandbox/rewrite/registry/library/adversarial/exit subset: **275 passed in 119.54 s**. This is a whole subset duration, not an individual domain timing claim.
- Later reviewer registry/telemetry/verification/evaluation subset: **267 passed in 16.39 s**.
- Final focused registry/sandbox safeguard regressions: **14 passed, 217 deselected in 2.17 s**.
- Integrator reports current full suite **1167 passed, 3 live skips**; its final coverage/tool checks are running.
- Independent warm tool-order probe now returns a new-spec passing signed artifact with no failures.
- Independent primary/secondary role probe now returns `floor_drift` with primary score **0.7**, rather than mixing it with secondary score 1.0.
- Independent legacy activation-migration probe now returns `floor_drift` with fresh score **0.7**, excluding previously persisted score 1.0. Its clean repeat exited successfully.

## Closed findings

### P4-B1 — Tool-set cache hit failed warm compatibility: CLOSED

Library compatibility canonicalized tools as a set, but tuning compared tuple order. The original probe obtained a library hit for reversed identical tools and then failed with `tuning_configuration_failed`. Current tuning uses matching set semantics. The same probe now compiles and freshly verifies the new spec.

### P4-B2 — Secondary scores could mask primary drift: CLOSED

Primary and secondary floors with equal kind/limit produced indistinguishable quality rows. A primary 0.7 and secondary 1.0 at limit 0.8/window 2 previously yielded no drift because they averaged together.

Current TelemetryRow adds only closed `floor_role` values `primary`/`secondary`. Evaluation validates/labels the role; Stage 4 explicitly labels both measurements. Registry persists roles and keeps independent windows against each configured definition; either floor can trigger drift. The updated probe returns primary drift at 0.7. Role migration and UUID dedup remain backward-compatible.

### Native wrapper dictionary ordering: CLOSED

Reviewed `_source_tree` normalization. It sorts string-key dictionary entries only for AST comparison against a newly emitted trusted wrapper. It does not remove nodes, values, imports, calls, duplicate keys or executable structure. Expected wrapper maps contain JSON literals. An authored capability call or other executable change cannot become the expected wrapper through sorting. Comparison never executes the source; native warm candidates are re-emitted. No host-code acceptance bypass was found.

### Additional accepted safeguards: CLOSED

- **Budget ceilings:** replacement Spec token/dollar/compute ceilings cannot rise unless the trusted caller explicitly supplies `allow_budget_increase=True`. Automatic monitor never supplies it. Permission does not bypass quality/authenticity or strictly-better acceptance.
- **Activation boundary:** install/rollback persists an aware activation timestamp. Observe rejects earlier or naive timestamps, excluding tuning/verification evidence generated before activation. Rollback resets derived scores while preserving UUID identities.
- **Legacy activation migration:** an in-flight version set activation=now but retained stored scores. This reviewer flagged that concern. Current migration clears old derived scores for active revisions, retaining UUIDs. A clean probe confirms a fresh 0.7 breach is not masked by legacy score 1.0. The implementation changed during the initial probe; no unresolved current defect remains.
- **Parser robustness:** parser `MemoryError` maps to `SandboxError`, preserving fail-closed API behavior without broadening the language.

## Security boundary reviewed

No arbitrary-Python or held-out escape was found within the documented boundary:

- Authored rewrite source is parsed, validated and **interpreted**, never imported or executed/compiled as Python. Host native execution is reserved for trusted preset source. Root runtime and evaluation pass the complete artifact to the mode-aware loader, and execution mode is signed in its digest.
- Closed grammar rejects unsupported statements/expressions, dynamic calls, arbitrary imports/attributes, defaults, decorators, classes/functions, comprehensions and loops. The interpreter exposes plain JSON values only. Task/model text stays data.
- Source size, AST node/depth, JSON nesting, message size, intermediate values and RPC count are bounded. Only bounded straight-line/conditional execution and the controlled model/preset capabilities are available.
- Preset wrapper literal name/params/components/spec must match the artifact. Direct guarded-call code has no access to Python objects beyond its JSON values and RPC operation. Trusted preset package code is not an authored import.
- Worker starts in isolated Python mode, empty temporary cwd and a minimal environment. No report, artifact path, adapter, wallet, expected labels, dataset handle, signing key or parent credentials is sent. The child is a restricted-language process, **not an OS jail for arbitrary Python**.
- Parent handles model RPC through RunContext and active compile wallets. Quotes reserve before provider invocation. Child stdout is bounded protocol, not passthrough. Cancellation during spawn, runtime errors and completion terminate/reap the child.
- Rebinding validates old restricted source, updates bound spec literals, preserves custom control flow, strips the report and retains sandbox mode. Native source must match the canonical trusted wrapper.

## Rewrite and lifecycle checks

- Proposal calls are guarded and compile-budgeted. Prompt contains source/config/numeric measurement, not labels, metadata or held-out data. Full primary tuning and fixed smoke tuning are used; only Stage 4 accesses held-out capability/signing authority.
- Exact single-file unified diffs cannot traverse paths, create/delete/rename other files or invoke patch/shell tools. Context, offsets and counts are checked. Malformed/unsafe/non-improving attempts count toward the hard cap.
- Root calls Stage 3 only after tuning misses, when enabled and supplied with both proposal adapter and smoke factory. Zero cap means no proposals.
- New versions persist sandbox source plus diff and remain unlicensed until Stage 4. Both tuning and smoke must fully pass with no hard errors. Acceptance requires lower mean dollar cost; zero-cost fallback requires lower actual mean consumed tokens. Denied reservations are not invented savings.
- Registry authenticates incoming and copied INSTALL artifacts, owns revision storage, serializes acceptance/pointer publication, rejects ties/regressions and rechecks rollback. Rank is primary quality, secondary quality, signed mean cost, then components.
- Library snapshots isolate caller paths. Every lookup reauthenticates; corruption evicts. Successful fresh verification counts toward promotion, two cumulative failures evict, and repeated store cannot reset counters/revive an evicted key. Expire snapshots are consumed templates, not additional delivered executions.
- Compiler creates a fresh spec-bound candidate and report on reuse. It updates reuse counts from actual Stage 4 results, stores licensed outputs, and never substitutes cached authority for fresh verification. A successful repair replaces failed tuning evidence before final verification.

## Remaining limitations, not Phase 4 blockers

1. Restricted-language process isolation does not isolate malicious trusted host/package code. Future Pi execution needs its own execution/tool boundary.
2. Direct guarded-call rewrites retain declared component metadata, but the interpreter does not prove that each component name corresponds to a distinct behavioral implementation. Floor/budget/held-out gates still apply; component-count simplicity is a metadata measure for such code.
3. Library copies authenticate before publication but do not fsync each copied file as registry installation does. Power-loss loss of a template fails closed on reauthentication. An unindexed snapshot left between rename and SQLite commit blocks storing that exact digest until repaired; no authority is fabricated.
4. Smoke factories are trusted configuration. Code rejects reuse of the same floor object but does not prove separate instances have disjoint records; callers must provide the separate fixed regression set promised by the contract.
5. Coder/Pi execution, multi-stage pipelines, client approval controls, ephemeral delivered-code deletion, coding dogfood and ARC submission behavior remain later-phase work.

## Conclusion

The current Phase 4 code closes this review's concrete defects and the accepted external-review safeguards. No new sandbox, held-out, licensing, budget, registry-acceptance or cache-authority bypass was found. Review is closed subject to the integrator's final complete exit/tool/coverage run.
