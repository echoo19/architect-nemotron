/** Explicit synthetic fixture smoke test. Never a production approval workflow. */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { ArchitectClient } from '../dist/index.js';

// This driver refuses implicit fixture authorization, non-loopback hosts, and inferred specs.
assert.equal(process.env.ARCHITECT_FIXTURE_AUTHORIZED, '1',
  'Set ARCHITECT_FIXTURE_AUTHORIZED=1 only for reviewed synthetic fixtures.');
const required = name => {
  const value = process.env[name];
  assert.ok(value, `Missing ${name}`);
  return value;
};
const baseUrl = required('ARCHITECT_BASE_URL');
const url = new URL(baseUrl);
assert.equal(url.protocol, 'http:');
assert.ok(['127.0.0.1', '[::1]', 'localhost'].includes(url.hostname), 'Fixture server must be loopback.');
const client = new ArchitectClient(baseUrl, required('ARCHITECT_API_TOKEN'));
const spec = JSON.parse(await readFile(required('ARCHITECT_FIXTURE_SPEC'), 'utf8'));
const input = JSON.parse(await readFile(required('ARCHITECT_FIXTURE_INPUT'), 'utf8'));
const proposal = await client.compile({ spec });
let compiled = proposal;
if (spec.lifecycle === 'install') {
  assert.equal(proposal.approval_required, true);
  assert.equal(proposal.accepted, false);
  assert.equal(proposal.artifact_id, null, 'An unapproved proposal cannot install an artifact.');
  // Explicit exact fixture Spec, not a model-inferred approval.
  compiled = await client.compile({ spec, approved: true });
}
assert.equal(compiled.accepted, true, 'Fixture compile must pass. Use a fresh workspace.');
assert.equal(compiled.approval_required, false);
assert.ok(compiled.artifact_id);
const artifact_id = compiled.artifact_id;
const before = await client.status(artifact_id);
assert.equal(before.licensed, true);
assert.equal(before.expired, false);
// These flags authorize only this operator-reviewed synthetic fixture invocation.
const result = await client.run({ artifact_id, input, enabled: true, human_reviewed: true });
if (process.env.ARCHITECT_FIXTURE_EXPECTED) {
  const expected = JSON.parse(await readFile(process.env.ARCHITECT_FIXTURE_EXPECTED, 'utf8'));
  assert.deepEqual(result.output, expected);
}
const after = await client.status(artifact_id);
let tied_recompile_rejected = false;
if (spec.lifecycle === 'install') {
  assert.equal(after.licensed, true);
  assert.equal(after.expired, false);
  const replacement = await client.recompile({ artifact_id });
  assert.equal(replacement.accepted, false, 'Deterministic fixture replacement must tie, not replace.');
  assert.ok(replacement.failures.includes('not_better'), 'Expected fixed not_better failure.');
  tied_recompile_rejected = true;
  const preserved = await client.status(artifact_id);
  assert.equal(preserved.version, before.version);
  assert.equal(preserved.spec_hash, before.spec_hash);
  assert.equal(preserved.licensed, true);
  const rerun = await client.run({ artifact_id, input, enabled: true, human_reviewed: true });
  assert.deepEqual(rerun.output, result.output);
} else {
  assert.equal(after.expired, true, 'Ephemeral delivery remains one-shot.');
}
// Do not log bearer tokens, host paths, fixture inputs, or model output.
console.log(JSON.stringify({ fixture_only: true, accepted: compiled.accepted,
  output_verified: Boolean(process.env.ARCHITECT_FIXTURE_EXPECTED),
  lifecycle: before.lifecycle, tokens_in: result.tokens_in, tokens_out: result.tokens_out,
  status_checked: true, tied_recompile_rejected }));
