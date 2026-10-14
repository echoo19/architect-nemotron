# Independent Phase 3 review

Status: **approved for Phase 3 scope; no open review blocker**. The accepted mean-cost ranking issue is fixed and the original independent probe now selects the cheaper-mean model. No implementation or test files were edited by this reviewer.

## Scope and current evidence

Reviewed the full source `docs/build-prompt.md`, current `docs/interfaces/phase3.md`, all compiler stages/facade, runtime evaluation and wallet code, secondary/mean report schema, and current unit/adversarial/exit tests. This final review supersedes earlier in-flight findings.

- Integrator reports final full suite: **793 passed, 3 live skips**, ruff and strict mypy clean.
- Reviewer earlier complete Phase 3 module/adversarial/exit subset: **222 passed in 41.24 s**; separate facade tests: **8 passed in 7.24 s**. These are separate runs, not a combined distinct-test count.
- Original variable-cost ladder probe rerun against the current files: **`WINNER A MAX 0.09`**, with A's held-out mean $0.0455 versus B's $0.05. Both satisfy the per-run budget. The old code selected B; current code selects A.
- Final reviewer focused command `uv run pytest -q tests/test_verification.py tests/test_compiler.py tests/adversarial/test_phase3.py`: **95 passed in 11.22 s**.

## Closed findings

### P3-B1 — Ladder compared per-case maximum instead of mean: CLOSED

Original counterexample: A costs $0.001/$0.09 on two held-out cases (mean $0.0455); B costs $0.05/$0.05 (mean $0.05). The former `_cost_key` selected B from the lower maximum.

Current Stage 4 assigns primary held-out `Measurement.mean_cost_usd` to signed `TestReport.mean_cost_usd`. `cost_usd` remains the maximum per-run resource bound. The facade ranks licensed models by signed mean, with maximum fallback only for reports predating the optional field. The unchanged independent probe now picks A. The new adversarial regression verifies both model rungs ran Stage 4, the correct winner/cost fields persisted, and forged mean cost invalidates the signature.

### P3-O1 — Empty configuration telemetry: CLOSED

Runtime and aggregate evaluation rows project finite whitelisted numeric/boolean artifact parameters. Runtime combines these with actual guarded-call settings and uses the preset as task type. Nested/string metadata is not copied. Execution rows no longer copy a stale licensed-report quality score; measured scores appear in quality-evaluation rows.

### P3-O2 — Full-history scans and duplicate aggregate billing: CLOSED

Current `runtime/evaluation.py` has **no `store.rows()` call**. It consumes Runtime's task-local `last_row`, verifies that the row is fresh, and does not confuse a factory failure with a preceding run. This removes history-size-dependent scans and shared-history concurrency ambiguity. Aggregate quality rows have **zero token/cost totals**; individual runtime rows remain the spending records.

### Floor-factory mismatch exception: CLOSED

The earlier reviewer subset failed because a mismatched factory floor caused `TuningError` to escape. Current facade validates factory definitions before tuning and returns `floor_unavailable`; tuning configuration ValueErrors return a fixed failure code. The later reviewer module/adversarial/exit subset passed. This was fail-closed throughout, not an unauthorized-license defect.

## Budget and security checks

- **Whole-compilation budget:** `RunContext._call` reserves all active ContextVar wallets before invoking any adapter. This covers inference, every tuning/verification call, all model rungs, and separately owned LLMJudge/calibration adapters. Nested refusal refunds earlier wallet reservations; failed/cancelled calls retain conservative reservations. Independent tests prove judge calls are refused before invocation when calibration would exceed token or dollar limits.
- **Compute limit:** compiler has a whole-compilation deadline; per-call deadlines include active wallet time remaining. The documented trusted/cooperative Python limitation remains; later rewritten/Pi execution requires process isolation.
- **Inference authority:** actual guarded LLM inference produces strict Spec JSON only. Report/license/approval fields are refused. Model provenance is discarded; user values override inference. Probe context is bounded and observed inside guarded execution. Installed compilation needs the trusted approval argument.
- **Selection and warm starts:** selection is deterministic and library-first. Hits require an authentic license and matching model. Tuning persists a new spec-bound version without transferring reports. Unsupported rewritten warm source is explicitly refused pending Phase 4.
- **Tuning:** bounded deterministic grid and real successive halving operate on tuning cases only. Halving eliminates candidates and scores finalists on the full tuning partition. Atomic directory allocation never overwrites versions. Effective zero-depth checks are removed from component counts.
- **Evaluation:** candidate code receives only copied task inputs, not expected answers or metadata. Inputs/outputs are schema-validated. Every selected case is attempted; hard runtime/schema/budget failures cannot be dropped to inflate an average. Ordinary checker disagreements still obey the declared aggregate quality floor.
- **Verification authority:** only Stage 4 imports the signer and held-out capability. It authorizes each complete held-out tuple once, checks exact primary/secondary definitions, refuses missing secondary evidence, checks sample counts, and applies maximum per-case resource bounds across both floors.
- **Binding and persistence:** old reports are discarded before fresh verification. Failed tuning cannot become a compiler-issued passing report through lucky held-out results. Artifact digest is checked after evaluation. Signatures bind secondary attestations, count and mean cost. Normal-save failure writes signed negative evidence; failure to persist that evidence raises instead of delivering an artifact.
- **Privacy:** failures use fixed codes, not private exception text. Aggregate rows contain metadata only; task/output/reference content remains local to evaluation. Numeric telemetry projection preserves the closed schema.

## Remaining limitations, not Phase 3 review blockers

1. Stage 2 optimizes the primary floor; Stage 4 checks both floors. If the cheapest primary candidate fails the secondary floor, the current facade does not retry another primary-passing candidate for that model. This can refuse a feasible spec but cannot license a failing secondary. The current tuning interface accepts one floor.
2. The floor API cannot prove that arbitrary caller-supplied outputs came from a candidate. The trusted evaluation runner performs that binding by executing the candidate and scoring its actual outputs. Hostile reflective Python remains outside the stated capability boundary.
3. Multi-stage pipeline shapes can be inferred but compilation refuses them until Phase 5 backend integration. Stage 3 rewriting and real library/registry behavior remain Phase 4 work.
4. Client feature-flag/human-review controls, real Pi execution, ephemeral code discard, coding dogfood, ARC aggregate runtime caps, and HTTP/TS flows remain later-phase requirements, not completed Phase 3 features.
5. Domain <60-second and overall coverage/release evidence remain integrator-owned acceptance checks. The reviewer subset duration is not an individual domain timing claim.

## Conclusion

The current code closes every concrete Phase 3 finding raised in this review. Budgets reserve before spending, held-out evaluation is complete and authority-gated, secondary evidence is signed, cached reports cannot bypass verification, user approval is not model-issued, and model selection now uses signed mean cost. Phase 3 review is closed.
