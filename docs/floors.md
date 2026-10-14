# Quality floors and data separation

A primary floor is mandatory. Cost cannot excuse a failed floor. An optional
secondary floor must also pass. Scores and limits are finite values in `[0, 1]`.

| Kind | Passing rule | Evidence |
| --- | --- | --- |
| `held_out_tasks` | Score >= limit | Fraction passing a programmatic checker. |
| `human_edit_rate` | Score <= limit | Fraction of new replay outputs changed from human-shipped output. |
| `llm_judge` | Score >= limit | Fixed model and rubric, with human calibration. |
| `held_out_game_score` | Score >= limit | Mean normalized score from actual authorized game rollouts. |

## Partition boundary

`FloorEvaluator` seals records at construction. It sorts unique case IDs and
splits even positions into tuning and odd positions into held-out data. Each
partition needs at least one case; human edit rate needs `minimum_samples` in
**each** partition (default 30). Duplicate IDs, missing partitions, nonfinite
scores and mismatched scoring inputs fail closed.

The compiler receives only `tuning` copies. Stage 4 holds the private capability
needed to obtain authorized held-out cases. Public `held_out` access raises.
Copied, changed or reconstructed held-out cases do not gain verification
authority. This is a trusted-Python API boundary, not isolation against hostile
reflection or an evaluator that deliberately leaks data. Rewrites run outside
that host. Dataset IDs alone cannot detect semantically duplicated real-world
tasks: dataset quality and content-disjointness are operator responsibilities.

## Task cases and custom floors

Task/judge profile files contain a JSON array of `EvaluationCase` records:

```json
[{"case_id":"example-001","task":{"value":1},"expected":"ok"}]
```

This single record illustrates the shape; a working dataset needs both partitions.
`HeldOutTasks(definition, records, checker=None)` uses exact equality by default.
A trusted Python checker receives `(expected, candidate_output)` and must return
a boolean. Profiles do not import arbitrary checker code.

To implement an evaluator, subclass `FloorEvaluator` and implement async
`score(cases, outputs)`. Call `_validate_score_inputs` before grading. Validate
finite unit scores and honor `passes()` direction. Test success, failure,
empty/malformed data, membership, and held-out access. Do not add a fifth floor
kind without changing product scope.

## Human edit log

JSONL contains one validated `EditLogRow` per record:

```json
{"case_id":"example-001","task":{"value":1},"proposed":"draft","shipped":"final","changed":true}
```

`changed` must equal `proposed != shipped`. The evaluator replays each task and
compares the **new candidate output** with `shipped`. The old changed flag is an
integrity check, not the new candidate's score. Use
`HumanEditRate.from_jsonl(definition, path)`. Both partitions need at least
`minimum_samples` records. The checked-in 200-row fixture is synthetic, not a
production baseline or proof of month-long improvement.

## Judges

`LLMJudge` requires a fixed rubric, fixed judge model, and human-graded calibration
records (`case_id`, `task`, `output`, `human_score`). It refuses missing calibration
or disagreement above the configured tolerance (default mean absolute error
0.15). Judge calls use the normal budgeted runtime. Judge explanations and task
content do not belong in telemetry. A calibrated synthetic judge is still not
evidence of live model quality.

## Games

Game floors score selected cases through an environment scorer, not model-written
score text. Factory integrations use one-use receipts bound to the active
candidate, evaluation and rollout. Receipts cannot be replayed across candidates
or partitions. Compilation support, parent previews and delivery are distinct
manifest roles.

Local scores mean synthetic puzzle wins; the three rule families contain 15
concrete instances, not 15 independently designed games. Official OFFLINE preview
scores use completed scorecard percentages: `raw / 100`, rejecting values outside
`0..100`, booleans and nonfinite values. A WIN is not automatically a score of 1.
Normalization is pinned to SDK 0.9.9 / engine 0.9.3 and a versioned score policy.
A conversion function alone does not authenticate a score. See
[game integration](usage.md#games) and [limitations](limitations.md).
