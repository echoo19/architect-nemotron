# Architecture reference

This is an engineering guide to the implemented six-phase build, not a claim
that every acceptance condition has passed. [PROGRESS.md](../PROGRESS.md) records
validation. The original [build prompt](build-prompt.md) is historical source.
The later user instruction supersedes its token-cap clauses: **token usage only,
no token quotas**. Dollars and elapsed time remain enforced limits.

## The outer layer

Models propose task inference, decomposition and rewrites. Code owns quality
checks, budgets, signed reports, registry acceptance and drift. A successful model
call is not a quality license. The preference order is floor first, then measured
cost, then simplicity. Token use is measured, not a hard limit.

Artifacts live under `<workspace>/artifacts/<spec_hash>/<version>/` with
`harness.py`, `spec.json`, `config.json`, and `report.json`. A report authenticates
the spec, configuration and exact code. Runtime rechecks the license before
execution. Editing these files does not grant authority.

## Compiler stages, distinct from build phases

| Stage | Mechanism |
| --- | --- |
| 0 | Infer a Spec from a request or probed Environment; user overrides win. |
| 1 | Select a preset and look up a compatible library warm start. |
| 2 | Grid or successive-halving tuning using tuning cases only. |
| 3 | Optional bounded rewrite proposals, checked in the restricted worker against tuning and smoke data. |
| 4 | Fresh held-out verification and a signed TestReport, including on cache reuse. |

An explicit Spec bypasses inference. An installed Spec must be reviewed before
tuning. Rewrites must pass the quality/smoke checks and reduce cost. Failed or
exhausted compilation returns failure rather than an unverified agent. Model
ladders run only for the explicit target `cheapest that passes`.

## Runtime, lifecycle and persistence

- `expire`: one delivery attempt, including a failed attempt; executable artifact
  is consumed and cannot be rerun. It is not installed in the registry.
- `install`: explicit reviewed-spec approval, then a licensed version in the
  registry. Replacements must improve the floor/cost/simplicity ordering; ties
  and regressions do not replace the incumbent. Old versions support rollback.
- Library entries are reusable licensed templates, not permission to skip a new
  task's held-out verification. Repeated reuse can nominate installation, not
  approve it. Failed re-verification drives eviction.
- Registry monitoring uses measured floor observations or changed model identity
  to trigger recompilation. Delivery success alone is not a floor measurement.
- Runtime and nested wallets enforce dollar/time limits. Pipeline stages, model
  calls and delegated work cannot reset an ancestor's remaining budget.

## Eight presets

| Preset | Role |
| --- | --- |
| `extractor` | Structured output with schema checks. |
| `synthesizer` | Data-to-prose generation and optional editing. |
| `recommender` | Retrieval and preference-aware recommendations. |
| `coder` | Pi extension pack: tools, context policy and instructions. |
| `explorer` | Probe an unknown interface. |
| `planner` | Plan against available rules; game delegation uses a scoped backend. |
| `verifier` | Check another result. |
| `hypothesis_tester` | Test candidate explanations in a game environment. |

Each has bounded tunable settings and removable components. A preset name or
`execution_config` JSON does not grant tools, held-out access or a game backend.

## Orchestration and integrations

`do` decomposes a goal through a model and compiles ephemeral subtask agents.
`create` compiles a builder that can call a host-scoped, bounded child compiler;
it does not replace that process with a fixed task router. The Phase 10 router is
evidence-based over schema and slot hashes: it orders already licensed versions
(installed entries, library templates, or a fresh compile) and has no keyword table.
Stage 1 preset selection stays rule-based, and only Stage 4 licenses. The coder uses genuine
upstream Pi 0.73.1 packages via the Node bridge. All Pi model calls use the guarded
Architect runtime. File-tool restrictions are not shell isolation.

Sequential pipelines execute each declared preset/model under step and aggregate
budgets. Only Stage 4 licenses their complete output. See [specs](specs.md).
The HTTP control plane and TS client expose only `compile`, `run`, `status`,
`recompile` and the advice-only `route`; application delivery approval is separate from spec approval.

Games use a provider/session interface and one-use rollout receipts for scoring.
The local simulator has three families and 15 distinct synthetic instances.
Official public OFFLINE evaluation requires explicit staged files and a trusted
`OfficialPreviewManifest`. Gateway submission scorecards cannot be used for
candidate tuning. Runtime game agents use one allowed offline small model and
no Stage 3 rewriting. See [usage](usage.md#games) for the concrete API.

Lectern examples are consumers outside the core package. They exercise synthetic
report replay, content acceptance, and a digitizer pipeline; they do not establish
live vision quality, live model quality, or month-long savings.

## Build phase map and evidence boundaries

1. Specification, model, runtime, telemetry and initial preset foundations.
2. Quality evaluators and partition isolation.
3. Inference, selection, tuning and held-out compilation.
4. Rewrite worker, reusable library and registry/drift lifecycle.
5. Genuine Pi coding, pipelines, goal orchestration and feature builder CLI.
6. Game/competition integration, HTTP/TS client, examples, docs and final gates.

These are implementation areas, not acceptance scores. Required final checks
include pytest, strict mypy, Ruff, independent gate attacks and coverage review.
Final Phase 6 counts are not yet available. No live official game benchmark,
confirmed Kaggle caps/compliance, remote CI run, or production savings is claimed.
See [limitations](limitations.md) before deployment.
