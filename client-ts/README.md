# Architect TypeScript client

A thin native-fetch client for the trusted local control plane. Node 22 or newer.
The only operations are `compile`, `run`, `status`, `recompile` and `route`.
Do not put this client or its bearer token in an untrusted browser bundle.

```sh
cd client-ts
npm ci
npm run build
npm test
```

TypeScript is pinned in `package.json` and `package-lock.json`. Tests use a real,
short-lived loopback HTTP server with mocked responses. They do not load a model.

```ts
import { ArchitectClient, type Spec } from '@architect/client';

const client = new ArchitectClient(baseUrl, operatorToken);
const proposal = await client.compile({ spec }); // spec: Spec; no implicit approval
const status = await client.status(knownArtifactId);
```

`compile` accepts either `{ request }` or `{ spec, approved? }`, never both.
Approval of an inferred request is not allowed. An installed proposal has no
artifact. Approve only the exact reviewed Spec with `{ spec, approved: true }`.
`run` accepts `{ artifact_id, input, enabled?, human_reviewed? }`. Both flags must
be explicit for installed delivery. The client never supplies them or retries.
`route(spec)` (Phase 10) is advice only: it sends `{ spec }` (never prose), spends
nothing, runs nothing and never grants reuse; the response carries closed labels,
numbers and fixed codes only. Routing orders licensed versions; Stage 4 licenses.
`recompile({ artifact_id })` cannot change the approved Spec. A tied or worse
replacement returns `accepted: false` with `failures: ["not_better"]`. Retrying
`compile` with the exact approved Spec that is already installed returns the same
refusal plus the existing `artifact_id`. A 503 (server code `storage_busy`, a held
storage lock) surfaces as `SERVER_ERROR` with `status` 503 and is safe to retry.

Spec approval is **not production deployment readiness**. Feature flags and human
review remain separate. Synthetic fixtures provide no one-month production cost
or edit-rate evidence. `execution_config` is JSON data, not a runtime capability.
Budget contains only `cost_usd` and `compute_s`. Tokens in results are measurements.
The optional `preset_hint` supports all eight registered names.

Requests reject extra DTO fields, non-JSON values, nonfinite numbers, cycles,
excessive nesting, bodies above 1 MiB, and identifiers above 128 characters.
Omit absent optional fields instead of passing `undefined`. IDs are path-encoded;
the server separately enforces its narrower opaque-ID format. Server gates remain
authoritative. Responses are capped at 1 MiB and checked against the DTO shape.
The native fetch deadline defaults to 120 seconds and is set per client with
`new ArchitectClient(baseUrl, token, { timeoutMs })` (a positive integer up to one
day). Set it above the server profile's `compile.compile_budget.compute_s` plus
install time: a compile that outlives the client deadline surfaces as
`NETWORK_ERROR` while the server still finishes and installs it, so the
`compile_budget` failure body is otherwise never observable. Redirects and retries
are disabled.
`ArchitectError` exposes a fixed `code`, fixed `message`, and optional HTTP
`status`. HTTP errors are mapped by status; server bodies and network exception
messages are never exposed. No request credentials are attached to errors.

## Explicit local live-server fixture test

The root integration starts and stops the real server separately. Use a fresh
workspace and the trusted synthetic profile
`examples/lectern/profiles/content-recommender.json`. Do not reuse a registry with
an existing equal entry. From `client-ts`, set these environment variables:

- `ARCHITECT_BASE_URL`: real server origin, e.g. `http://127.0.0.1:8765`.
- `ARCHITECT_API_TOKEN`: the same operator-selected bearer token as the server.
- `ARCHITECT_FIXTURE_SPEC`: absolute path to
  `examples/lectern/specs/content-recommender.json`.
- `ARCHITECT_FIXTURE_INPUT`: absolute path to
  `examples/lectern/fixtures/content-recommender-input.json`.
- `ARCHITECT_FIXTURE_AUTHORIZED=1`: explicit authorization for reviewed synthetic
  fixture approval and fixture-only enabled/human-reviewed delivery.
- Optional `ARCHITECT_FIXTURE_EXPECTED`: absolute JSON file path for exact output
  checking. Expected recommender output is
  `{"content_id":"fractions-practice","review_required":true}`.

```sh
npm run e2e
```

Or after building, `node scripts/e2e.mjs`. Paths resolve from the caller's working
directory. The script refuses non-loopback URLs and missing fixture opt-in. It
checks proposal-without-artifact, explicit Spec approval, licensed status,
explicitly enabled/reviewed run, status after delivery, tied recompile rejection,
and that the original installed artifact still runs. For an ephemeral fixture it
instead checks one-shot expiry and does not recompile. It starts no server.
Output is one bounded JSON summary without credentials, inputs, or model output.
A nonzero exit means the live integration did not pass.
