# Phase 6 runtime integration contract (planning; Phase 5 commit first)

Root owns these shared changes. Game workers must not edit runtime, compiler, presets, profiles or schemas.

## Game capability

- Add `GameBackendProtocol.run(context, task, *, preset, params, components) -> JsonValue` and an async scoped `Runtime.use_game_backend(backend)` capability.
- `RunContext.play_game(task, *, preset, params, components)` validates the original bound task and current scope before dispatch. JSON IDs are not capabilities.
- Scope ownership is the specific Runtime. Use a shared revocable lease. Track outstanding calls and cancel/drain them on async scope exit; recheck before each environment side effect and after waits.
- Bind original task and candidate digest in trusted runtime metadata. Evaluation helpers pass the actual artifact digest. Models cannot supply or modify that metadata.
- Trusted game preset dispatch calls this capability. Generic explorer/planner uses outside the game flow remain supported. A game artifact requiring the capability fails closed without it.
- Keep rewritten game warm starts disabled for runtime game factories. Sandbox privileged dispatch remains bound to the authenticated artifact, never arbitrary worker-supplied configuration.

## Evaluation evidence

Add a task-local, scoped evaluation identity around each complete `evaluate_cases` measurement. It binds candidate digest and a fresh measurement identity. The game executor and scorer can read it; models cannot. A receipt binds this identity, original task, actual run, session, and trusted observed score. Scoring consumes the receipt once and checks all bindings. Clear outstanding receipts on scope exit. Failed measurement outputs cannot be replayed in a later measurement, even for the same candidate.

Official score finalization belongs inside the execution deadline, before issuing a receipt. Scoring then consumes trusted data without network work. Delivery results may expose labeled numeric scores; model-authored scores never count. A valid completed game may lose: keep execution validity distinct from winning, and let the floor aggregate actual scores.

## Usage

Track input and output usage separately in CompileWallet. Return aggregate descendant model usage in RunResult. Keep raw TelemetryRow model usage local to that row. Evaluation measurements use aggregate successful results, while failed runs retain conservative denial evidence and actual billed rows. Never double-count raw telemetry. Do not include a separate judge's grading bill in the agent's per-run budget.

## Presets and model policy

Only add hypothesis_tester. GameFactory permits explorer, planner and hypothesis_tester; installed competition delegation specializes planner. Hypothesis testing must include explicit testable hypotheses/predictions plus public observation feedback, not just a renamed prompt.

Pin runtime inference, tuning, verification and delivery to one trusted, operator-selected offline binding. Disable rewriting and reject sandbox warm starts. Model text cannot select another adapter or a cloud route. Local fixtures are explicitly synthetic. Validate standard local HTTP bindings as loopback and prevent environment proxies from silently routing them outward. This is not proof of model size, OS network isolation, or competition compliance; privileged host adapter code remains trusted.

## Explicit preset selection

Add an optional `Spec.preset_hint` restricted to the eight registered preset names. This separates strategy from the `game` domain instead of using pseudo-domains or hidden JSON-schema extensions. It grants no tools or execution authority. Stage 1 remains deterministic and library-first, but a warm start must match an explicit hint. Add a trusted `CompileConfig.allowed_presets` allowlist (empty means the normal registered set); runtime game compilation restricts it to explorer, planner and hypothesis_tester. Reject an excluded selection before tuning. Stage 0 may propose a hint; host overrides and allowlists still win.

The new field is bound by the Spec digest. Existing persisted licenses whose canonical Spec changes require fresh Stage 4; never migrate signatures or silently grant compatibility.

## Frozen backend configuration

Add `Spec.execution_config: dict[str, JsonValue]` with an empty default. This is declarative data, not a capability or executable code. The Spec digest binds it. Game and competition factories write a strictly validated policy there: strategy, limits, exact small-model binding, support-manifest digest, and delegation settings. Do not store private puzzle payloads or held-out observations in it. The scoped backend checks that its effective policy matches the authenticated artifact before work. A changed host policy cannot silently run under an old license. Unrelated presets do not interpret this field as tool permissions.

Bound serialized configuration size and reject malformed game/competition policies. Do not add new game role types, floors, or domains. The server's Spec DTO includes this data but cannot manufacture the required host capability from a request.

## Trusted exact inference overrides

`Compiler.compile(..., exact_overrides: bool = False)` and `infer_spec(..., *, exact_overrides: bool = False)` preserve default deep merging. Trusted factories may opt in to replace each explicitly supplied top-level field completely after strict proposal validation and before tuning, approval or signing. This is not a Spec field or model-controlled capability. It cannot repair invalid proposal JSON, add authority fields, or approve an inferred installed Spec. Provenance remains code-derived. Game factories use it for complete host-owned schemas and frozen policy.
