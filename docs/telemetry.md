# Telemetry

Every runtime attempt routed through `Runtime` records one validated `TelemetryRow`,
including rejected licenses, budget failures and exceptions. Parser or authentication
refusals before runtime are not model execution rows. Content is not a telemetry field. The append-only
`runs.jsonl` journal is the source of truth; `index.sqlite3` is a recoverable index.
The store fsyncs a row before indexing it. It serializes writes and catches duplicate
run IDs. On reopen it replays the journal. Append recovers only the unindexed tail.
A physically torn final record can be repaired; complete corrupt records fail closed.
Storage must remain writable: no system can promise a durable row after total storage
failure, and Architect must not deliver success if its mandatory log fails.

## Fields

`run_id`, `timestamp`, `domain`, `task_type`, `spec_hash`, `harness_version`,
`lifecycle`, `preset_origin`, `component_set`, `model`, `model_params`,
`tokens_in`, `tokens_out`, `cost_usd`, `wall_time_s`, `model_calls`,
`cached_tokens_in`, `image_inputs`, `floor_type`, `floor_role`, `floor_score`, `floor_limit`,
`accepted`, `drift_flag`.

`model_calls` counts the model calls the run reserved (including its nested
calls and calls that failed after reservation). `cached_tokens_in` is the part
of `tokens_in` the provider reported as served from its prompt cache. Both are
nonnegative integers with no content; rows written before they existed read
back with `0`.

`image_inputs` counts the images the run passed to adapters across its whole
lineage (pipeline stages and nested calls included; an image re-sent on a
retry or self-check counts again). It is a nonnegative integer and nothing
else: no media type, dimension, hash, page text or case identifier ever
enters telemetry. Rows written before it existed read back with `0`.

`floor_role` is the closed set `primary` / `secondary` and defaults to `primary`;
secondary-floor rows carry `secondary` so drift windows stay separate per role.

Use `TelemetryRow.model_json_schema()` for the authoritative schema. The model
rejects extra keys, nonfinite numbers, arbitrary strings and unapproved metadata
keys. Strings are capped at 64 characters except the UUID run ID and SHA-256 spec
hash (which have their own stricter formats). Nested component and parameter values
are validated too. `model_params` accepts a small fixed whitelist of numeric/boolean
settings, never free-text prompts, tool arguments or outputs.

`categorical(value)` keeps only a closed set of known labels; other labels become
`h_` plus a truncated SHA-256 identifier. Hashing is not encryption or a guarantee
against guessing low-entropy labels. Never pass content as a category merely to
hide it. Task text, inputs, outputs, source code, errors, customer identifiers,
Pi transcripts and judge explanations do not belong in this journal.

Accepted runtime execution is not by itself a quality observation: evaluation rows
can have `floor_score=null`. Drift logic must use measured floor scores rather than
assuming every completed model call meets the quality floor.

## Usage is not a quota

`tokens_in` and `tokens_out` record usage and support efficiency comparisons.
Neither field is a token allowance. Dollar and elapsed-time safety limits are
separate. Aggregate and nested counters must not be added blindly: a parent can
already include child usage. Use the integration's summary scope when comparing
costs. Prices and synthetic fixture costs are not proof of a provider invoice or
production savings.

## Keep other stores private

The telemetry restriction does not mean all project files are content-free.
Artifacts, reviewed specs, CLI output, HTTP successful outputs, evaluation files
and example summaries can contain task data. Protect them separately. The HTTP
server suppresses access logs and returns fixed error codes; the TS client does
not retain request bodies or credentials in its errors. Neither is a complete
host-wide privacy system.

Checked-in [telemetry examples](../examples/telemetry/README.md) label synthetic
model/fixture evidence. Pi process evidence proves the upstream loop ran with
scripted model responses; it does not prove live model quality. Local-game and
scripted official-transport rows are not official benchmark results. Only real,
consented production edit logs and comparable costs over the requested period
could establish month-long savings.
