# Write a specification

`architect.spec.Spec` defines the task, input/output JSON schemas, primary and
optional secondary floor, budget, model target, lifecycle and domain. Optional
fields include `preset_hint`, `tools`, `pipeline`, `execution_config` and
per-field `provenance` (`user` or `inferred`). `Spec.model_json_schema()` is the
authoritative schema. User values override inferred values.

## Budgets and model choice

```json
{"cost_usd": 0.1, "compute_s": 60}
```

`Budget` contains only positive finite dollars and elapsed seconds. Tokens are
usage/efficiency measurements, not quotas. Python drops a legacy `tokens` budget
key for migration; new specs must omit it. Provider context windows and optional
output capacities are transport constraints, not an Architect token budget.
`CompileConfig.compile_budget` separately bounds compilation. Nested work remains
subject to all active dollar/time limits. Cost never excuses a failed floor.

The exact model target `cheapest that passes` requests the configured
`CompileConfig.model_ladder`. Otherwise only the named model is tried. Model
prices and capacities come from configuration, not a hard-coded larger-is-better
assumption. A profile must configure every model used by its stages and judges.

## Evaluation data

Python integrations provide trusted evaluators through `Compiler(...,
floor_factory=...)`. CLI profiles map dataset IDs to validated files. The compiler
does not invent reference answers to make a task pass. Synthetic data tests
mechanisms only. A secondary floor is an additional gate, not an alternative.
See [floors](floors.md) for formats, partitions and judge calibration.

## Lifecycle and exact approval

Use `expire` for one execution attempt; it is consumed even when delivery fails.
Use `install` for reviewed, versioned delivery. Inferred and explicit installed
specs both return an approval checkpoint before tuning.

- Python: review `proposal.spec`, then call
  `await compiler.compile(reviewed_spec, approved=True)`.
- CLI: review the file named by `proposal_path`, then use
  `--approved-spec reviewed.json` on `compile` or `create`.
- HTTP/TS: resubmit the exact reviewed `spec` with `approved: true`. A plain
  `request` cannot carry approval.

A malformed model proposal is an error, not permission to use an unverified
fallback. Model text, provenance fields and JSON configuration cannot grant
approval. Application enablement and human delivery review remain separate.

## Pipelines

A pipeline has at most 16 uniquely named `PipelineStep` entries. Each specifies
`name`, `preset`, `model`, and `budget`, with optional `input_schema`,
`output_schema`, and `params`. It runs sequentially, not as an arbitrary DAG.

The first step receives the original input. Later steps receive:

```json
{"original": {}, "previous": {}, "steps": {"earlier-step-name": {}}}
```

Those values are prior data, not authority. Each stage executes its actual preset
and declared model. Input/output validation, per-step limits and shared remaining
dollar/time limits apply. An invalid stage or schema rejects the composition;
Architect does not silently skip it.

A final `verifier` with an empty `output_schema` is an implicit gate. It must
return exactly `{"passed": true, "reason": "..."}` to release the preceding
payload; `passed: false` rejects it. At least one payload stage must precede the
gate. With an explicit output schema, the last stage instead supplies the final
schema-checked output. Outer Stage 4 verifies the complete composition.

## Artifact integrity

Inspect `spec.json`, `config.json`, `harness.py`, and `report.json` under
`<workspace>/artifacts/<spec_hash>/<version>/`. Only an authenticated Stage 4
report licenses execution. Changed code, configuration, spec or report invalidates
that license. Library hits still undergo fresh verification on the new task.
For processes launched from different directories, set
`ARCHITECT_SIGNING_KEY_FILE` to the same absolute private key path. Keep that key
outside coding checkouts and back it up separately from generated code.
