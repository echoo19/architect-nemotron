# Retake trial: ten change requests

Run on 2026-10-12 against Architect at commit `c4e0783` of my private working repository
(the public repository starts as one commit, so that hash isn't in it), with real calls to
NVIDIA Nemotron on Nebius Token Factory. The plan was written and hashed before the
first model call, and every hashed file still matched when the run was audited. The bugs the
trial found were fixed afterwards; the section "Since the trial" lists them. The ten rows
were not run again.

## What was asked

Architect's rule is that an agent it builds only runs if it passed its exam, and that a
changed agent stops until it passes again. Ten ordinary change requests, fixed in advance,
were each given once to Architect's own coder (Nemotron 3 Super). For each one the trial
recorded whether the coder changed the agent, whether the app's next call was refused with no
model call, and whether the changed agent reached the pass mark on a retake.

The trial did not ask whether a change made the agent better or worse. The agent as built is
not run again by a retake, and no earlier score decides anything.

## The agent

Built once from `examples/expense-app/receipt-reader-demo.json` with
`architect -p "/make @receipt-reader-demo.json"`. The build tried three models against a pass
mark of 40. Nemotron 3.5 Lightning got 36 of 100 on the exam and didn't pass, Nemotron 3 Super
got 59 and Nemotron 3 Ultra 64. Super is the cheaper of the two that passed, so it became the
agent. The build made 1,500 model calls, cost $0.31 and took 20 minutes. It was
added to a copy of the example app in `agents/receipt_reader`, and the app read a made-up
receipt through it (one model call, answer correct).

The exam is 200 development receipts (100 practice, 100 exam), with 150 more that only
retakes use. All 350 were used while Architect was being built. These scores are not
Architect's published results. No evidence exam receipt and no delivery receipt was used:
those files were not copied into the trial's folder.

## Headline

Of the 10 requests:

- 8 changed the agent. All 8 were stopped: the app's next call was refused with 0 model calls.
- 3 of those were retaken and passed (48, 46 and 48 of 100, pass mark 40).
- 5 could not be examined, because the coder's edit was broken or went outside what a retake
  can examine: a model the settings do not have together with a changed budget, settings out
  of range, JSON that no longer parses, and an edit to generated code. They stayed stopped
  until Undo put the passed version back.
- 2 changed nothing: the coder said what it would do and ended without editing a file in the
  app's folder.
- 0 took a retake and missed the pass mark.

So this trial does not show the rule catching a change that made the agent worse. It shows
eight unreviewed changes stopped before a model call, three of them examined and let through,
and five that had to be undone because nothing could examine them. Whether those five would
have broken the app was not measured: no edited agent was run without the stop.

## The table

Every retake here is attempt 2, on the same 100 receipts no earlier attempt used, because
each row starts from the same saved state. "Stopped" is the app's own reply to
`POST /receipts`, with the model calls counted in that process.

| Row | Request | What the coder changed | Stopped (model calls) | Retake | Verdict | Cost | Time |
|---|---|---|---|---|---|---|---|
| control | One space added to the end of the task text, by the trial itself | task text, 1 character | yes (0) | 50 of 100 | passed | retake $0.028 | 113 s |
| 1 | Make the receipt agent cheaper to run. | Model changed to `nvidia/nemotron-3-8b-instruct`, which the settings do not have. Per-run budget halved to $0.01 and 30 s. | yes (0) | refused, no attempt used: the change touches the budget | stopped, undo only | coder $0.005 | coder 10 s |
| 2 | Shorten the agent's prompt. It is too long. | Nothing. It read the files, said "I'll shorten the overly long task prompt" and ended. | not stopped, nothing changed | none | no change | coder $0.005 | coder 11 s |
| 3 | Handle discounts better. Discount lines keep getting mixed up with items. | One sentence added to the item count rule (discount keywords). Settings: retries 0 to 1, verifier_depth 0 to 1. | yes (0) | 48 of 100 | passed | coder $0.008, retake $0.029 | coder 14 s, retake 109 s |
| 4 | Make it faster. | Same missing model as row 1. Added `temperature` and `max_tokens` to the settings. Budget cut to $0.005 and 10 s. Input limit cut from 8,192 to 2,048 characters. | yes (0) | refused, no attempt used: the settings in config.json | stopped, not examined | coder $0.012 | coder 20 s |
| 5 | Be stricter about tax. Only return a tax amount when the receipt really prints one. | One sentence added to the tax rule. Then the coder kept rereading the files until its cost cap ended the run. | yes (0) | 46 of 100 | passed | coder $0.269, retake $0.028 | coder 105 s, retake 120 s |
| 6 | Make it more accurate on the total. | Two sentences added to the total rule. Settings: retries 0 to 2 (the most allowed is 1), verifier_depth 0 to 1. | yes (0) | refused, no attempt used: the settings in config.json | stopped, not examined | coder $0.010 | coder 20 s |
| 7 | Rewrite the rules in simpler English so I can maintain them. | Nothing. It said "I'll simplify the task description" and ended. | not stopped, nothing changed | none | no change | coder $0.005 | coder 11 s |
| 8 | It sometimes returns null for the subtotal when the receipt has one. Fix that. | Added text to the subtotal rule with a raw line break inside the JSON string, so `spec.json` is no longer valid JSON. Settings: retries 0 to 2, verifier_depth 0 to 1. | yes (0) | refused, no attempt used: spec.json is not valid JSON | stopped, not examined | coder $0.052 | coder 51 s |
| 9 | Tidy up the config and remove any settings it does not need. | Removed the whole `params` block, calling both values defaults. One was not: verifier_depth goes from 0 to 1 without it. Also wrote `TASK_COMPLETION_SUMMARY.md` at the top of the app. | yes (0) | 48 of 100 | passed | coder $0.008, retake $0.028 | coder 16 s, retake 117 s |
| 10 (asks for something the receipt rules disagree with) | Count items more carefully. A line like "2 x" should count as two items. | Rewrote the item count rule to count quantities, with unescaped quotes, so `spec.json` is no longer valid JSON. Made the same edit inside `artifact/harness.py`, the generated Python. | yes (0) | refused, no attempt used: harness.py can only be undone | stopped, undo only | coder $0.019 | coder 25 s |

Notes on the rows:

- Rows 4, 6 and 8: the trial's driver recorded all five refused rows as "stopped, undo
  only", which is what the plan's exit-code rule says. The table separates the two that touch
  something fixed (rows 1 and 10) from the three whose file had a mistake that could be fixed
  (rows 4, 6 and 8). That split was made after the plan was hashed.
- Rows 2 and 7: "no change" means no file in the app's folder changed. Files elsewhere
  were not compared.
- Row 3: the settings change did nothing in this run. The retake made exactly 100 model
  calls with no invalid answer, so no retry fired. Only the added sentence was examined.
- Row 9: this is in effect the agent as built. The coder deleted `params`, and the retake
  wrote the defaults back (`retries` 0, `verifier_depth` 1). `verifier_depth` does nothing for
  this agent, which has no review step, and its plan is unchanged. Its 48 against the
  control's 50 is the noise of running the same agent twice.

For every stopped row the three counters of the app's process were all zero: HTTP requests
sent by the model adapter, replies with token usage, and tries to reach another computer.
The same counters read 1, 1 and 2 on each call after a pass. The Nebius key was loaded and
the network was open, so a call could have gone out.

Undo: in the five rows that stayed stopped, `architect undo` put the agent's files back byte
for byte and `architect verify` said `Nemotron 3 Super. 59 of 100. Pass mark 40. Passed.
Attempt 1.` In the three rows that passed, the app worked again on its next call with no
restart, its terminal said `passed its exam. 48 of 100, pass mark 40, attempt 2.`, and Undo
answered "Nothing to undo. This is the version that passed."

## How to read the scores

The agent as built scored 50 of 100 on the retake receipts (the control row). The three
changes that passed scored 48, 46 and 48 on the same receipts. A score on 100 receipts is
uncertain by roughly 10 points either way, so none of the three is shown to be better or
worse than the agent as built. They passed, and that is all the rule says about them. The
right words for them are "three changes passed", not "three improvements".

Stopped means not examined. It does not mean worse, and it does not mean did not pass.

## One run outside the trial

No request reached "did not pass", so three parts of the product had not run on real models:
did not pass, Try on practice, and a same-exam retake. After all eleven rows were finished,
one more run was made by hand. It is not a trial row, it is not counted above, and its
scores did not come from the ten requests. The model in `config.json` was changed from
Nemotron 3 Super to Nemotron 3.5 Lightning, the cheaper model the settings really have (rows
1 and 4 reached for a cheaper model and named one that does not exist).

| Step | Result | Cost | Time |
|---|---|---|---|
| App call after the change | 502, the stop sentence, 0 model calls | $0 | |
| Try on practice | 24 of 100 practice receipts right. Not an attempt. No attempt recorded, no file changed. | $0.006 | 70 s |
| Retake, attempt 2 | 29 of 100 on 100 receipts no earlier attempt used. Did not pass. Files untouched, app still stopped. | $0.006 | 75 s |
| Retake, attempt 3 | Same exam as attempt 1 (50 unused receipts left, fewer than an exam). 32 of 100. Did not pass. | $0.006 | 75 s |
| Undo | Files back byte for byte, app works on its next call | $0 | |

## Where the method differs from a person using the app

- The coder was called directly, the same function the app's `code` tool calls, with the
  app started headless the way `architect` starts it. In the app a request first goes to the
  conductor model, which writes its own description for the coder. That step was skipped so
  the frozen words reached the coder unchanged.
- Edits were allowed without a prompt (`auto-edit`) and the shell tool was withheld.
- Each coder run was capped at $0.30. Architect's own cap is $2.00.
- The agent was exported to `agents/receipt_reader` by naming that path. Left to its default
  it would have gone to `agents/receipt_reader_demo`.
- The app ran in Architect's own Python environment, not one of its own, and was called
  through FastAPI's in-process test client, not over a socket.
- Between rows the saved start state was copied back (app folder and workspace), which also
  resets the record of used receipts. That is why every row is attempt 2 on the same 100.

## Cost

$0.8354 in total against a budget of $3.00, from 2,367 model replies. The trial's own ledger
(tokens at Nebius list prices) and Architect's own arithmetic agree to the cent.

| Part | Cost |
|---|---|
| Build | $0.3089 |
| 10 coder runs | $0.3933 (row 5 alone is $0.2689) |
| 4 retakes on Super (control and rows 3, 5, 9) | $0.1132, about $0.028 each |
| App calls and the path check | $0.0021 |
| The run outside the trial | $0.0179 |

A retake of 100 receipts on Super took 109 to 120 seconds.

## What this does not show

- That the rule caught a change that made the agent worse. No row missed the pass mark.
- That any change improved the agent.
- That the five refused edits would have broken the app.
- Anything about accuracy, the 348 delivery receipts or the evidence results.
- That 0 model calls is a security guarantee. It was counted for calls through the agent's
  own folder, in one process, by the trial's own counter.
- That the coder is reliable. Two requests were reported as done with nothing changed, two
  edits left invalid JSON, and one run looped until its cost cap.

## What was not done, and what rests on the trial's own word

- No rerun of any row. No provider outage happened.
- No blind judgement of whether an edit was a good idea.
- The conductor's step and the desktop app were not exercised.
- The plan's hash and the time of the first call are local file times and the trial's own
  ledger, four seconds apart. Nothing was committed or timestamped by anyone else first.
- No per-receipt result was kept. The verdicts recompute from the signed counts only.
- The provider's own usage records were not checked against the ledger.
- Before the plan was written, two requests reached the provider by accident with a
  placeholder key and failed. They carried no receipt and no request of this trial.

The per-row records (each request, the coder's steps, the diff, the app's reply, the
retake's output and the ledger of model replies) are kept outside this repository.

## Since the trial

The trial ran on commit `c4e0783`. These product bugs it found were fixed afterwards, each
with a test, and the ten rows were not run again on the fixed code:

- The app said "Retake its exam or undo" for changes a retake then refused (rows 1, 4, 6
  and 8). The example app now asks Architect where the agent stands, and says "Undo the
  change" or "Fix the file" with the reason.
- A refused file did not say what was wrong. It now names the setting and what it may be,
  or the line where the JSON stops reading.
- A model the settings do not have was not named when the budget had changed too (row 1).
- The cost line used the old model's price after a model change (the run by hand).
- The example app looked only in `agents/receipt_reader`, and the demo plan's agent goes to
  `agents/receipt_reader_demo` by default. It now looks in both.
- The coder's reply now carries lines the host checked itself: that no file changed (rows 2
  and 7), that a changed JSON file no longer parses (rows 8 and 10), and that an agent is
  now stopped. The coder's looping on Nemotron 3 Super (row 5) is not fixed; its cost cap
  is what ends such a run.

One live pass was made on the fixed code afterwards. Its numbers are in the next section.

## One live pass on the fixed code

Run on 2026-10-12 on commits `11e37ea` (the build) and `839d6e5` (every later step), with
real Nemotron calls on the same demo exam. It is not a trial row and adds nothing to the
counts above. Each step was one process whose model calls were counted, as in the trial,
and the edits were made by a script the way a person with an editor would make them.

| Step | Result | Model calls | Cost | Time |
|---|---|---|---|---|
| Build `receipt-reader-demo.json` | Nemotron 3 Super shipped at 60 of 100, pass mark 40 | 1,501 | $0.3089 | 22.6 min |
| Add to project, default folder | `agents/receipt_reader_demo`, found by the example app with no path given | 0 | $0 | 1 s |
| App call, as built | 201, the receipt read correctly | 1 | $0.0003 | 2 s |
| Model changed, Super to Lightning; app call | 502, "Stopped: changed after its exam. 0 model calls.", the file, both models, "Retake its exam or undo the change in Architect." | 0 | $0 | 1 s |
| Try on practice | 24 of 100 practice receipts right. Not an attempt. | 100 | $0.0059 | 73 s |
| Retake, attempt 2, on 100 receipts no earlier attempt used | 31 of 100. Pass mark 40. Did not pass. Folder untouched, app still stopped. | 100 | $0.0058 | 87 s |
| Undo; app call | The version that passed is back; 201 | 1 | $0.0003 | 2 s |
| Per-run budget halved (a fixed field); app call | 502, "The change touches the budget. Undo the change in Architect." No retake offered, and `architect retake` refused with no attempt used. | 0 | $0 | 1 s |
| `retries` set to 2; app call | 502, "retries is 2. It can be 0 or 1. Fix it or undo." | 0 | $0 | 1 s |
| One sentence added to the task text; app call | 502, "Changed: artifact/spec.json (task text). Retake its exam or undo the change in Architect." | 0 | $0 | 1 s |
| Retake, same exam, attempt 3 (50 unused receipts were left, fewer than an exam) | 55 of 100. Pass mark 40. Passed. | 100 | $0.0286 | 109 s |
| App call after the pass, no restart | 201; the app's terminal read "passed its exam. 55 of 100, pass mark 40, attempt 3." | 1 | $0.0003 | 2 s |

Total $0.3503 from 1,805 model replies, by this pass's own ledger at list prices and by
Architect's arithmetic alike.

The 55 against the build's 60 on the same 100 receipts is inside what an exam of this size
moves by from one run to the next. It does not show the added sentence made the agent worse
or better. The cost line before the Lightning runs read "About $0.006", where the trial's
read "About $0.03" for the same change. After the pass, one sentence of the example app's
reply to a file with a mistake was shortened, and that step was run once more (0 model
calls).

## One run with Claude Code

On 2026-10-13 I gave Claude Code one sentence, in a copy of the example app whose receipt
agent had passed its exam (Nemotron 3 Super, 59 of 100, pass mark 40):

> Make the receipt agent in agents/receipt_reader cheaper to run.

It was told nothing about Architect, the exam or the stop. It changed one line, the model in
`agents/receipt_reader/artifact/config.json`, from Nemotron 3 Super to Nemotron 3.5
Lightning. The next receipt posted to the app was refused with 0 model calls:

```
POST /receipts -> 502
Could not read the receipt. Stopped: changed after its exam. 0 model calls. Changed: artifact/config.json (model: Nemotron 3 Super to Nemotron 3.5 Lightning). Retake its exam or undo the change in Architect.
```

`architect undo` then put back the version that passed, with 0 model calls, and the same
receipt was read on the next call. No retake was run, so this doesn't say whether Lightning
would have passed.

This is one run of one tool with one request, on my Claude subscription, so it gives no rate.
The stop came from the app's check on the agent's files, and there's no plugin for Claude
Code. Codex is untested.

## The same stop on a one-minute job

Also on 2026-10-13, I ran the path a judge can take, headless, through the same backend the
desktop app uses, on commit `62eb757` of my private working repository, with real Nemotron
calls. The job is one of the Describe step's examples: "Pull the invoice number, date, vendor
and total out of invoice text".

| Step | Result | Cost | Time |
|---|---|---|---|
| Build | Nemotron 3.5 Lightning passed with 6 of 6, pass mark 5, and became the agent `pull-invoice-number`. 72 model calls. | $0.0095 | 69.8 s |
| Add to project | 13 files written to `agents/pull_invoice_number/`. 0 network calls. | $0 | |
| Model in `artifact/config.json` changed from Lightning to Super; next call | "Stopped: changed after its exam. 0 model calls." and "Changed: artifact/config.json." | $0 | 0.02 s |
| Undo | The version that passed is back, with 0 model calls, and the next call answered. | $0 | 0.2 s |
| Retake exam, attempt 2 | "Same exam, attempt 2". 6 of 6, passed. The folder then runs on Super. | $0.0006 | 6.5 s |

The whole run cost $0.0118.

The exam here is six cases a Nemotron model made up, so 6 of 6 says nothing about accuracy on
real invoices. A drafted exam has no unused cases, which is why the retake is the same exam
again. The retake passed, so this run shows the stop, the undo and a retake that lets a
change through. It doesn't show a retake that fails. The times are from my machine, with my
Nebius key directly, not a judge code through the relay.
