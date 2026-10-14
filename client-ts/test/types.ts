import { ArchitectClient, type Budget, type Spec, type JsonValue } from '../src/index.js';
const client = new ArchitectClient('http://127.0.0.1:8000', 'local-token');
const budget: Budget = { cost_usd: 1, compute_s: 1 };
// @ts-expect-error Tokens are measured metadata, never budget policy.
const badBudget: Budget = { ...budget, tokens: 1 };
const spec: Spec = { task: 'fixture', input_schema: {}, output_schema: {}, budget,
  floor: { kind: 'held_out_tasks', dataset: 'fixture', limit: 1 },
  preset_hint: 'hypothesis_tester', execution_config: { nested: [null, false, 4] } };
void client.compile({ spec });
void client.compile({ spec, approved: true });
void client.compile({ request: 'fixture' });
// @ts-expect-error Approval requires an exact explicit Spec.
void client.compile({ request: 'fixture', approved: true });
// @ts-expect-error Exactly one compile source.
void client.compile({ request: 'fixture', spec });
// @ts-expect-error No unknown preset.
const badSpec: Spec = { ...spec, preset_hint: 'cloud_admin' };
// @ts-expect-error No authority override.
void client.recompile({ artifact_id: 'abc123', spec });
// @ts-expect-error JSON excludes undefined.
const badJson: JsonValue = undefined;
void client.run({ artifact_id: 'abc123', input: null, enabled: true, human_reviewed: true });
void client.status('abc123');
void client.route(spec);
// @ts-expect-error Routing over HTTP needs a Spec, not prose.
void client.route({ request: 'fixture' });
