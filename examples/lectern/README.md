# Synthetic Lectern consumer examples

These examples test Architect's mechanics, **not model quality**. They use no real
student records, uploads, edit logs, client feedback, provider calls, or live
Lectern services. No files were copied from the consumer application. Every
consumer schema and response function stays here, outside `architect/`.

## Run

From the repository root, use a **fresh workspace** for each run:

```console
uv run python examples/lectern/run.py --workspace .architect/lectern-proposals
uv run python examples/lectern/run.py --workspace .architect/lectern-passing --approve-fixtures
uv run python examples/lectern/run.py --workspace .architect/lectern-failing --approve-fixtures --variant floor-failure
uv run pytest tests/test_phase6_examples.py -q
uv run ruff check examples/lectern tests/test_phase6_examples.py
```

The default run stops at proposed installed Specs. Inspect each
`<workspace>/<example>/proposed-spec.json`. The explicit `--approve-fixtures`
option simulates review of that exact proposed Spec: it writes
`reviewed-spec.json` and a hash-bound `fixture-approval.json` **before** compiling.
This is fixture-only approval, not a claim that a human reviewed it. Normal
consumer integration must obtain actual authorized review of the proposed Spec.

The approved run executes real Stage 1 selection, Stage 2 tuning (two candidates),
Stage 4 held-out verification, registry installation, license reload, and runtime
delivery. Stage 3 is deliberately disabled: passing fixtures need no rewrite.
The digitizer also executes real Stage 0 using a labeled synthetic model response.
No code here grants licenses, bypasses held-out access, or modifies the compiler.

Use `--example report-cards`, `--example test-digitizer`, or
`--example content-recommender` to run one flow. The whole passing runner is tested
as a native Python subprocess with a hard 60-second timeout. It normally takes
about five seconds locally; this is not a service latency benchmark.

## Evidence and delivery gates

All generated files go under `--workspace`:

- `summary.json`: synthetic label, stages, real reports, call counts, run IDs.
- `<example>/artifacts/`, `library/`, `registry/`: real persisted harnesses.
- `<example>/telemetry/*.jsonl` and SQLite index: content-free runtime/evaluation rows.
- `<example>/synthetic-output.json`: content-bearing synthetic output, kept separate
  from telemetry. No real content is authorized for this runner.
- Proposed/reviewed Specs and fixture-only approval records.

Passing examples produce licensed artifacts and registry revision 1. A passing
report-card replay has edit rate 0/100; digitizer and recommender score 6/6.
These numbers are **deterministic fixture results**, not statistical quality
estimates. `floor-failure` produces schema-valid wrong outputs. It must return
unlicensed artifacts with a real `primary_floor_failed` Stage 4 report for all
three examples, and it must not install or deliver them.

The example application checks both a feature flag and explicit fixture review
before delivery. Each positive run proves that either missing gate blocks model
execution. The fixture runner can enable these **only for synthetic inputs**.
All summaries retain `production_feature_enabled: false`,
`human_review_required: true`, and `production_approved: false`. Real per-item human
review must replace the simulated review gate before any client delivery.
No production flag, API, tenant store, rendering route, or sending route is used.

There is **no measured current-pipeline cost or month-long production baseline**.
Report drafts stay behind human review until an owner verifies the required month
of lower cost and acceptable real edit rate. A license on fixtures does not meet
that deployment gate. Metered tokens are observations, never quotas; only dollar
and elapsed-time budgets bound these examples. Fake usage and costs are synthetic
nonzero accounting values, not provider bills or tokenizer measurements.

## Report cards: 200-row edit log

`specs/report-cards.json` selects `synthesizer`, with `human_edit_rate <= 0.10`.
`fixtures/report-edits.jsonl` contains 200 explicitly synthetic replay records.
The floor sorts unique IDs and splits even/odd positions at construction, giving
**100 tuning and 100 held-out samples**, both above the default minimum of 30.
Only Stage 4 can obtain held-out cases from the evaluator.

Each UTF-8 JSONL line has these required fields, with no header:

```json
{"case_id":"synthetic-example","task":{"synthetic":true,"completed":2,"assigned":20,"topic":"fractions"},"proposed":"earlier draft","shipped":"reviewed fixture draft","changed":true}
```

- `case_id`: unique synthetic replay ID.
- `task`: structured input, not a desired answer.
- `proposed`: historic synthetic draft.
- `shipped`: synthetic stand-in for the human-approved draft.
- `changed`: boolean exactly equal to `proposed != shipped`.

Historic `changed` is an integrity field, **not** the replay score. HumanEditRate
compares each new candidate output to `shipped`. The example callback produces
short paragraphs from input counts only; it never reads `shipped` or held-out
labels. The failure variant returns a wrong draft and gets edit rate 1.0.
The records are generated scenarios, not 200 actual human observations.

## Test digitizer: actual inferred multi-model pipeline

The entry point is the exact one-sentence request in `docs/build-prompt.md`:

> I want an AI feature that turns uploaded tests into Bluebook-style online tests with answer keys.

The runner calls `Compiler.compile(REQUEST)` **without pipeline overrides**.
Stage 0 makes its guarded FakeAdapter call to `fixtures/digitizer-inference.json`,
parses the scripted proposed Spec, and marks the pipeline provenance `inferred`.
It then stops for the installed-Spec checkpoint. The checked-in
`specs/test-digitizer.json` documents the expected proposal, not a bypass of Stage 0.
This proves inference wiring and validation, **not that a real LLM inferred a good
pipeline**. The inference response is explicitly synthetic.

The approved Spec selects the extractor and executes every declared step:

1. `vision-extract` / fake `gpt-4o-mini`: maps a synthetic structured page to
   questions and printed keys. **No image is supplied or visually interpreted.**
2. `solver-rederive` / fake `llama3.2`: independently adds integers from original
   expressions; it does not copy the extracted printed answers.
3. `verifier-diff` / fake `claude-sonnet-4-20250514`: consumes both prior outputs,
   diffs printed and re-derived keys, and returns strict answer-key JSON with
   visible disagreements and `review_required: true`.

These model IDs are labels on **FakeAdapter subclasses**; no vendor is contacted.
Each step has its own dollar/time budget and the runtime enforces the shared
parent budget. Call-count evidence proves all three execute during delivery,
and token/cost accounting includes all three. The ordinary Stage 4 checker uses
exact expected-output equality, including known answer keys and disagreements,
on six unseen fixture cases. It is not the model's own self-verdict.

The fixture output for page 1 re-derives `2 + 1 = 3`, detects a printed key of `4`,
and exposes `disagreements: ["q00"]`; it never hides that behind a majority vote.
The failure variant changes the final key while retaining a valid schema. Every
model still executes; Stage 4's programmatic checker rejects the result.
This is not a working PDF uploader or proof of real vision/answer-key accuracy.

## Content recommender and server/TypeScript integration

`specs/content-recommender.json` selects the recommender. The callback consumes
its explicit retrieval and preference-memory context. Held-out equality against
six synthetic acceptance labels measures fixture acceptance, not actual client
behavior. The intentionally narrow cases all prefer `fractions-practice`, with
irrelevant reading distractors. This is not evidence of broad personalization.

A stock-adapter profile is ready for the HTTP/TS E2E runner:

- Profile: `examples/lectern/profiles/content-recommender.json`
- Installed Spec: `examples/lectern/specs/content-recommender.json`
- Input: `examples/lectern/fixtures/content-recommender-input.json`
- Expected output: `{"content_id":"fractions-practice","review_required":true}`

The profile uses core `FakeAdapter` fixture responses and needs no custom Python
plugin. Resolve fixture paths relative to the profile. Supply a fresh absolute
workspace to the server. Compile without approval first, inspect its proposed
Spec, then submit that exact Spec with explicit approval. Enable only fixture
feature/review gates for run requests. The Python test separately compiles,
installs, and runs this stock profile. HTTP endpoint mechanics and TypeScript
commands are documented by the server/client, not invented by this runner.

Report and digitizer flows use the local `synthetic_adapter.py` callback because
their answers vary by input. The stock profile deliberately covers the small,
fixed-result recommender case; it is not advertised for the other two flows.
Replacing fixture callbacks with network models requires trusted configuration,
real datasets, owner approval, and new independent quality evidence.
