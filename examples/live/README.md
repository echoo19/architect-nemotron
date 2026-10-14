# Live example: Claude Code CLI as the model

Runs Architect end to end on a real model through a logged-in `claude` CLI, with
no API key. Costs shown are API-equivalent estimates, not subscription bills.

```sh
uv run architect compile "Label the sentiment of a short product review. Input is {\"review\": string}. Output must be exactly {\"label\": one of positive, negative, neutral}. Use held-out floor dataset sentiment-v1 with a minimum pass rate of 0.9, lifecycle install, model claude-haiku-4-5-20251001." --profile examples/live/claude-code.json
# Review the proposal file it prints, fix anything wrong (for example make the
# schemas real JSON Schema objects), save it as reviewed.json, then approve it:
uv run architect compile reviewed.json --profile examples/live/claude-code.json --approved-spec reviewed.json
uv run architect run ARTIFACT_DIRECTORY --profile examples/live/claude-code.json --input '{"review": "Love it"}'
```

`sentiment-cases.json` holds 24 synthetic cases; the floor splits them by sorted
ID into 12 tuning and 12 held-out cases. The CLI cannot cap output length, so each
call reserves the model's full output capacity against the per-run dollar limit;
use a per-run budget of about $0.50 for Haiku even though real calls cost ~$0.002.

## Interactive session transcript (live, 2026-09-25)

A first-user journey through `uv run architect` in an empty workspace, abridged.
The session found the logged-in CLIs, chose Haiku after one probe call, inferred
the Spec with complete JSON Schemas, raised the per-run budget above the smallest
reservation on its own, and helped build the floor from six model-drafted cases
that the operator reviewed one by one:

```
architect> Classify a customer support ticket into exactly one of billing, bug, feature, other. ...
infer? infer a Spec from this text with claude-haiku-4-5-20251001 (reserves $0.3486, ...)? [Y/n] Y
stage 0 done in 12.8s, $0.0091
budget.cost_usd set to $0.6900 so 2 calls of claude-haiku-4-5-20251001 fit; change with set
  budget.cost_usd <usd>
Spec d3223844  lifecycle install (approval required)  stage 0: $0.0091, 12.8s  origin inferred
! floor_not_configured dataset 'tickets-v1' is not in the profile          fix: floor
review d3223844> floor
floor tickets-v1> draft 6
Drafted cases were written by the same model that will be tuned on them. ... Every drafted case
is stamped SYNTHETIC and is never counted as production evidence.
floor tickets-v1> review tickets-v1-s001 ok
...
floor tickets-v1> accept
floor 'tickets-v1' configured: 6 cases -> 3 tuning / 3 held-out [SYNTHETIC: 6 of 6 cases
  model-drafted by claude-haiku-4-5-20251001, accepted by operator; not production evidence]
review d3223844> approve d3223844
approved d3223844 - compiling. Type cancel or press Ctrl-C to stop.
[00:32] compile finished in 32.3s: 9 calls, $0.0149, stages 1,2,4
Verification (stage 4)  id bf33674a  stages 1,2,4  licensed: YES (recomputed from disk)
  floor      held_out_tasks  score 1.00 >= limit 0.90   held-out cases 3
result d3223844> install tickets d3223844
installed tickets revision 1 -> ...\registry\versions\...\1
result d3223844> run {"ticket": "I was charged twice for my subscription this month."}
run 7651386f  claude-haiku-4-5-20251001  $0.0018  3.6s  542 in / 244 out
{ "category": "billing" }
result d3223844> run {"ticket": "The export button crashes the app every time I click it."}
run a0078ff4  claude-haiku-4-5-20251001  $0.0019  4.0s  541 in / 273 out
{ "category": "bug" }
```

Content-free rows for this session are in `examples/telemetry/session-live.jsonl`.
Costs are API-equivalent estimates, not subscription bills. Six cases is the
smallest useful floor; the session warns that three held-out cases allow no
misses, and `draft 20` is the recommended size.
