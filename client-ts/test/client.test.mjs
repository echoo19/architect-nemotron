import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { after, before, test } from 'node:test';
import { ArchitectClient, ArchitectError } from '../dist/index.js';

const spec = {
  task: 'Synthetic Lectern content recommendation', input_schema: {}, output_schema: {},
  floor: { kind: 'held_out_tasks', limit: 1, dataset: 'synthetic', minimum_samples: 1 },
  budget: { cost_usd: 0.1, compute_s: 60 }, lifecycle: 'install',
  preset_hint: 'hypothesis_tester', execution_config: { fixture: true, nested: [null, 4] },
};
const compileResult = {
  accepted: true, approval_required: false, spec, artifact_id: 'abc123',
  failures: [], stages: [1, 2, 3, 4], warm_started: false,
};
const statusResult = {
  artifact_id: 'abc123', licensed: true, expired: false, lifecycle: 'install',
  spec_hash: 'a'.repeat(64), version: 1, registry_name: 'fixture',
};
const runResult = {
  run_id: 'run1', output: { review_required: true, content_id: 'fractions-practice' },
  tokens_in: 900_000, tokens_out: 800_000, cost_usd: 0, wall_time_s: 0.01,
};
let server, baseUrl, client, handler;
const seen = [];
before(async () => {
  server = createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const raw = Buffer.concat(chunks).toString();
    const call = { method: req.method, url: req.url, headers: req.headers,
      body: raw ? JSON.parse(raw) : undefined };
    seen.push(call);
    if (req.headers.authorization !== 'Bearer fixture-only-secret') {
      res.writeHead(401); res.end('PRIVATE server detail'); return;
    }
    handler(call, res);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  baseUrl = `http://127.0.0.1:${server.address().port}`;
  client = new ArchitectClient(baseUrl, 'fixture-only-secret');
});
after(async () => {
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
});
const json = (res, value) => {
  res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(value));
};
function fixed(code, status) {
  return error => {
    assert.ok(error instanceof ArchitectError);
    assert.equal(error.code, code); assert.equal(error.message, code);
    assert.equal(error.status, status); assert.equal(error.cause, undefined);
    assert.ok(!JSON.stringify(error).includes('PRIVATE'));
    assert.ok(error.message.length < 40);
    return true;
  };
}
test('public client has exactly five operations and no enumerable credentials', () => {
  assert.deepEqual(Object.getOwnPropertyNames(ArchitectClient.prototype).sort(),
    ['compile', 'constructor', 'recompile', 'route', 'run', 'status']);
  assert.equal(JSON.stringify(client), '{}');
});
test('constructor rejects credentials in URLs and malformed tokens without leaking', () => {
  for (const [url, token] of [ ['no-url', 'x'], ['file:///PRIVATE', 'x'],
    ['http://user:PRIVATE@localhost', 'x'], [baseUrl + '?PRIVATE=x', 'x'],
    [baseUrl + '#PRIVATE', 'x'], [baseUrl, ''], [baseUrl, 'PRIVATE\r\nx'] ]) {
    assert.throws(() => new ArchitectClient(url, token), fixed('INVALID_CONFIGURATION'));
  }
});
test('constructor rejects invalid deadlines and accepts an explicit one', async () => {
  for (const options of [ null, [], 'x', { timeoutMs: 0 }, { timeoutMs: -1 }, { timeoutMs: 1.5 },
    { timeoutMs: NaN }, { timeoutMs: Infinity }, { timeoutMs: '1000' }, { timeoutMs: undefined },
    { timeoutMs: 86_400_001 }, { retries: 1 }, { timeoutMs: 1000, retries: 1 } ]) {
    assert.throws(() => new ArchitectClient(baseUrl, 'fixture-only-secret', options),
      fixed('INVALID_CONFIGURATION'));
  }
  const slow = new ArchitectClient(baseUrl, 'fixture-only-secret', { timeoutMs: 50 });
  assert.equal(JSON.stringify(slow), '{}');
  handler = (_, res) => setTimeout(() => json(res, statusResult), 400);
  // Whether the request reached the server before the abort depends on runner speed;
  // the contract is only that the deadline surfaces as NETWORK_ERROR.
  await assert.rejects(slow.status('abc123'), fixed('NETWORK_ERROR'));
  const patient = new ArchitectClient(baseUrl, 'fixture-only-secret', { timeoutMs: 5_000 });
  assert.deepEqual(await patient.status('abc123'), statusResult);
});
test('native HTTP auth and fixed auth errors', async () => {
  await assert.rejects(new ArchitectClient(baseUrl, 'wrong').status('abc123'), fixed('UNAUTHORIZED', 401));
});
test('compile proposal has no artifact and never adds approval', async () => {
  handler = (call, res) => {
    assert.equal(call.method, 'POST'); assert.equal(call.url, '/compile');
    assert.equal(call.headers.authorization, 'Bearer fixture-only-secret');
    assert.match(call.headers['content-type'], /application\/json/);
    assert.deepEqual(call.body, { spec });
    json(res, { ...compileResult, accepted: false, approval_required: true, artifact_id: null, stages: [] });
  };
  const result = await client.compile({ spec });
  assert.equal(result.approval_required, true); assert.equal(result.artifact_id, null);
});
test('compile explicit approval preserves exact Spec including JSON config and preset', async () => {
  handler = (call, res) => { assert.deepEqual(call.body, { spec, approved: true }); json(res, compileResult); };
  assert.deepEqual(await client.compile({ spec, approved: true }), compileResult);
});
test('compile inferred request keeps approval absent', async () => {
  handler = (call, res) => {
    assert.deepEqual(call.body, { request: 'synthetic request' });
    json(res, { ...compileResult, accepted: false, artifact_id: null, approval_required: true });
  };
  await client.compile({ request: 'synthetic request' });
});
test('run passes explicit flags only; input is generic JSON and high token use is measured', async () => {
  const input = [null, 3, 'test', { nested: true }];
  handler = (call, res) => {
    assert.equal(call.url, '/run'); assert.equal(call.method, 'POST');
    assert.deepEqual(call.body, { artifact_id: 'abc123', input }); json(res, runResult);
  };
  assert.deepEqual(await client.run({ artifact_id: 'abc123', input }), runResult);
  handler = (call, res) => {
    assert.deepEqual(call.body, { artifact_id: 'abc123', input, enabled: true, human_reviewed: true });
    json(res, runResult);
  };
  await client.run({ artifact_id: 'abc123', input, enabled: true, human_reviewed: true });
  handler = (call, res) => {
    assert.deepEqual(call.body, { artifact_id: 'abc123', input, enabled: false, human_reviewed: false });
    res.writeHead(403); res.end('PRIVATE');
  };
  await assert.rejects(client.run({ artifact_id: 'abc123', input, enabled: false, human_reviewed: false }), fixed('FORBIDDEN', 403));
});
test('status encodes opaque identifiers as one path segment and has no body', async () => {
  const id = 'slash/ question?# % ü';
  handler = (call, res) => {
    assert.equal(call.url, '/status/' + encodeURIComponent(id));
    assert.equal(call.method, 'GET'); assert.equal(call.body, undefined);
    json(res, { ...statusResult, artifact_id: id });
  };
  assert.equal((await client.status(id)).artifact_id, id);
});
test('status explains expiration without execution', async () => {
  handler = (_, res) => json(res, { ...statusResult, lifecycle: 'expire', expired: true, licensed: false, registry_name: null });
  assert.equal((await client.status('abc123')).expired, true);
});
test('recompile sends ID only and exposes tied rejection without retry/approval', async () => {
  const start = seen.length;
  const rejected = { ...compileResult, accepted: false, artifact_id: null, failures: ['not_better'] };
  handler = (call, res) => {
    assert.equal(call.url, '/recompile'); assert.equal(call.method, 'POST');
    assert.deepEqual(call.body, { artifact_id: 'abc123' }); json(res, rejected);
  };
  assert.deepEqual(await client.recompile({ artifact_id: 'abc123' }), rejected);
  assert.equal(seen.length, start + 1);
});
const routeResult = {
  action: 'warm_start', artifact_id: null, fit_tier: 'B', fit: 0.86, quality_lcb: 0.041,
  expected_cost_usd: 0.0098, latency_s: 2.7, latency_basis: 'measured',
  evidence: ['stage4_signed', 'live_api_equivalent'], codes: ['fit_shape', 'route_warm_start'],
};
test('route sends the Spec only and returns content-free advice', async () => {
  const start = seen.length;
  handler = (call, res) => {
    assert.equal(call.url, '/route'); assert.equal(call.method, 'POST');
    assert.deepEqual(call.body, { spec }); json(res, routeResult);
  };
  assert.deepEqual(await client.route(spec), routeResult);
  assert.equal(seen.length, start + 1);
});
test('route refuses prose, extra fields and text-bearing responses', async () => {
  const start = seen.length;
  for (const bad of [undefined, null, 'prose request', { ...spec, approved: true }]) {
    await assert.rejects(client.route(bad), fixed('INVALID_REQUEST'));
  }
  assert.equal(seen.length, start);
  for (const body of [{ ...routeResult, name: 'invoices' }, { ...routeResult, codes: ['Free text'] },
    { ...routeResult, action: 'install' }, { ...routeResult, fit_tier: 'D' },
    { ...routeResult, latency_basis: 'guess' }]) {
    handler = (_, res) => json(res, body);
    await assert.rejects(client.route(spec), fixed('INVALID_RESPONSE', 200));
  }
  handler = (_, res) => { res.writeHead(400); res.end('{"error":"route_spec_required"}'); };
  await assert.rejects(client.route(spec), fixed('REQUEST_REJECTED', 400));
});
test('all errors use bounded fixed messages independent of response text', async () => {
  for (const [status, code] of [[400, 'REQUEST_REJECTED'], [403, 'FORBIDDEN'], [404, 'NOT_FOUND'],
    [409, 'CONFLICT'], [413, 'REQUEST_REJECTED'], [422, 'REQUEST_REJECTED'],
    [429, 'REQUEST_REJECTED'], [500, 'SERVER_ERROR'], [503, 'SERVER_ERROR']]) {
    handler = (_, res) => { res.writeHead(status); res.end('PRIVATE'.repeat(200_000)); };
    await assert.rejects(client.status('abc123'), fixed(code, status));
  }
});
test('malformed, nonfinite, cyclic, oversized and extra authority requests fail before HTTP', async () => {
  const cycle = {}; cycle.self = cycle;
  const bad = [
    () => client.compile({}), () => client.compile({ request: 'x', spec }),
    () => client.compile({ request: 'x', approved: true }),
    () => client.compile({ spec, approved: 'true' }),
    () => client.compile({ spec, profile: 'PRIVATE' }),
    () => client.compile({ spec: { ...spec, license: {} } }),
    () => client.compile({ spec: { ...spec, budget: { ...spec.budget, tokens: 42 } } }),
    () => client.compile({ spec: { ...spec, budget: { cost_usd: Infinity } } }),
    () => client.compile({ spec: { ...spec, preset_hint: 'unregistered' } }),
    () => client.compile({ spec: { ...spec, execution_config: [] } }),
    () => client.run({ artifact_id: 'abc123', input: NaN }),
    () => client.run({ artifact_id: 'abc123', input: undefined }),
    () => client.run({ artifact_id: 'abc123', input: cycle }),
    () => client.run({ artifact_id: 'abc123', input: new Date() }),
    () => client.run({ artifact_id: 'abc123', input: [1,,3] }),
    () => client.run({ artifact_id: 'abc123', input: { get secret() { throw Error('PRIVATE'); } } }),
    () => client.run({ artifact_id: 'abc123', input: null, human_reviewed: 1 }),
    () => client.run({ artifact_id: 'abc123', input: 'x'.repeat(1_048_576) }),
    () => client.recompile({ artifact_id: 'abc123', spec }),
    ...['', '.', '..', 'a'.repeat(129), 'a\nx', '\ud800'].map(id => () => client.status(id)),
  ];
  const start = seen.length;
  for (const action of bad) await assert.rejects(action(), fixed('INVALID_REQUEST'));
  assert.equal(seen.length, start);
});
test('invalid response bodies and authority-bearing DTOs are fixed failures', async () => {
  for (const body of ['PRIVATE', 'null', '{"x":1e999}', JSON.stringify({ ...statusResult, directory: 'PRIVATE' }),
    JSON.stringify({ ...statusResult, version: -1 }), ' '.repeat(1_048_577)]) {
    handler = (_, res) => { res.writeHead(200); res.end(body); };
    await assert.rejects(client.status('abc123'), fixed('INVALID_RESPONSE', 200));
  }
  handler = (_, res) => json(res, { ...compileResult, approval_required: true });
  await assert.rejects(client.compile({ spec }), fixed('INVALID_RESPONSE', 200));
});
test('network disconnection and redirects are fixed errors with no retry or token forwarding', async () => {
  handler = (_, res) => res.destroy(new Error('PRIVATE'));
  const start = seen.length;
  await assert.rejects(client.status('abc123'), fixed('NETWORK_ERROR'));
  assert.equal(seen.length, start + 1);
  handler = (_, res) => { res.writeHead(302, { Location: baseUrl + '/PRIVATE' }); res.end(); };
  await assert.rejects(client.status('abc123'), fixed('NETWORK_ERROR'));
  assert.equal(seen.length, start + 2);
});
