# Architect

Architect is an agent factory. You type a job in one sentence and it builds a small AI agent
for your app, on NVIDIA Nemotron through Nebius Token Factory. The agent only runs if it
passed its exam. Change it, and it stops until it passes again.

1. Describe: type the job in one sentence.
2. Exam: models practice on your test cases (or ones a model drafts), then take an exam on
   cases the build never saw. The cheapest one that passes becomes your agent.
3. Add to project: the agent goes into your code as a folder of files.
4. Stop: every call through that folder checks its files against the ones that passed. If
   one changed, the call is refused with 0 model calls until the agent passes again or you
   undo.

The main drawback is that it's a tripwire, not a lock, so someone who controls the computer
can get around it. Architect runs as a desktop app and in a terminal.

[Download the app](https://github.com/echoo19/architect-nemotron/releases/latest), or follow
the [judges' ten-minute walk-through](docs/judges.md).

Of 10 change requests I gave Architect's coder, 8 changed the agent's files, and every
changed version was refused before any model call. This doesn't show the exam catching a
worse change ([the trial](docs/bench/retake-trial.md)).

![The Results step of the Architect desktop app for the receipt reader. The headline reads "Super passed. It is your agent." A table shows each model's exam score out of 100 against a pass mark of 40: Lightning got 39 and did not pass, 1 short; Super got 64 and passed, marked Your agent; Ultra got 72 and passed. Running cost per 1,000 receipts is $0.058, $0.28 and $0.93. The build took 18m 54s and cost $0.31.](docs/media/architect.png)

This is the Results step for the published receipt reader, from its 2026-10-07 build.
Lightning got 39 of 100 and didn't pass, Super got 64 and Ultra 72, and Super is the cheaper
of the two that passed, so it became the agent. These aren't the demo build's numbers on the
Devpost page (36, 59 and 64), which come from a different exam on development receipts. The
exam column of the table under Results comes from the published 2026-10-05 build, before
the rule checks were added.

The video on the Devpost page (1:50) is a reconstruction from recorded runs, not a screen
recording: the app's own interface code is fed the numbers and lines those runs recorded,
and the pointer presses are re-enacted. The film typesets a few cards itself (the terminal,
the two job cards), and the Undo press stands for the undo command I ran. The narration is an AI voice (Bella, ElevenLabs),
and the music is "Closer" by Andrew Ev, from Mixkit.

## Try it

1. Install the desktop app from the
   [releases page](https://github.com/echoo19/architect-nemotron/releases):
   `Architect-Setup-<version>.exe` on Windows, or the `.dmg` on macOS (`arm64` for Apple
   silicon, `x64` for Intel). The app needs no uv, Python or Node. The first time it opens
   it downloads Python 3.12 and Architect's locked packages into its own data folder, so
   that first start needs an internet connection.

   To use the terminal agent instead, you need git, [uv](https://docs.astral.sh/uv/) and
   Node 20.6 or later:

   ```sh
   git clone https://github.com/echoo19/architect-nemotron
   cd architect-nemotron
   uv sync --frozen --python 3.12
   npm ci --no-audit --no-fund --prefix architect/backends/pi-node
   uv run architect
   ```

2. Connect Nebius Token Factory. Create an API key at
   [tokenfactory.nebius.com](https://tokenfactory.nebius.com/project/api-keys). The app's
   first screen asks how to connect Architect to a model: under API key, choose Nebius
   Token Factory, click Continue, then paste the key into the Judge code field (it takes a
   key too) and click Check code.
   In the terminal, set `NEBIUS_API_KEY` before the first run, or paste the key at
   `/login nebius`. Architect then writes a profile that gives each job to a Nemotron
   model, as described [below](#built-on-nebius-token-factory-and-nvidia-nemotron).

   Hackathon judges get a code instead of a key. Run `uv run architect judge <code>`, or
   paste the code where the first run asks for the Nebius key, and Nemotron calls go
   through a relay that holds the key and caps each code's spend
   ([docs/judges.md](docs/judges.md), [deploy/relay](deploy/relay/README.md)).

3. Describe an agent. In the app, click New agent and say in one sentence what it should
   do, or open a plan file you saved. In the terminal, type the description after `/make`.
   Architect may ask a question first, then shows the plan: the pass mark the agent has to
   reach, the most the build may cost and the models it will try. Nothing is built until you
   approve it (Build agent in the app).

Every command and setting is in the [reference](docs/reference.md), and building the app
from source is in [docs/desktop.md](docs/desktop.md).

## How it works

The four steps above are the short version. In the desktop app they're six screens across
the top bar: Describe, Plan, Build, Results, Try it and Add to project. The exam happens
inside Build, and the stop comes after Add to project. The terminal does the same work with
commands. The list below follows the work in order, so it has seven entries: rule checks are
part of the Plan screen, and the exam is the last part of Build.

1. Describe. You say what the agent should do, and a model turns that into a spec: what
   goes in, what comes out, what counts as right, the pass mark and the budget. You can
   also open a spec you saved as a file, and then Architect builds it exactly as written.
2. Plan. The Plan step shows what the agent takes and gives back, its rules, the pass mark,
   the models and the most the build may cost. You can change it with Edit plan, and
   nothing runs until you click Build agent. Architect splits the test cases in two:
   practice cases, which the build may use, and exam cases, which it never sees. If you
   bring no cases, a model drafts them and Architect marks them as synthetic.
3. Rule checks. A spec you write yourself can also declare checks: code that applies one of
   your stated rules to every answer, such as "amounts keep every printed digit". Architect
   never takes a check from a model's draft. The Plan step lists them under the rules, and
   the checks are part of the spec, so the exam tests the model and its checks together.
4. Build. Each model practices on the practice cases: Architect picks a ready-made strategy
   that fits the spec (a preset) and tunes it there. Forge, Architect's harness engineer,
   can then try to beat it. Forge is a coding agent that writes the agent's harness: the
   code around the model calls, which decides how to prompt, how to read the reply and
   which rules to check in code. It writes in a small, restricted language and sees only
   practice cases. Its harness replaces the tuned preset only if it reaches the pass mark on
   practice and ranks above the preset. With a Nemotron model as the engineer, that has not
   happened yet (see [Results](#results)), so on Nebius Token Factory a build skips Forge
   unless you choose it. A build of a spec with rule checks keeps Forge off, since a
   harness Forge writes might skip them.
5. Exam. Each model's best setup runs once on the exam cases. Only the exam decides whether
   a model passes, so a good practice score is never enough on its own.
6. Results. A model passes only if it reached the pass mark on its practice cases and on
   the exam, within its per-run budget. The cheapest model that passes becomes your agent,
   and a model that doesn't pass is never installed. The Results step shows each model's
   exam score against the pass mark and what it costs per 1,000 runs. Behind that,
   Architect signs the exam report either way and gives a passing agent a license: a signed
   record tied to the agent's exact code and spec, so a changed agent needs a new exam.
7. Try it and add it. Try it runs your agent on input you paste and shows the answer, its
   cost and its time. Add to project writes the agent's files into one new folder once you
   approve, and changes no existing file. Connect it then opens a conversation with
   Architect's coder with the request typed in, and you allow or refuse each edit it
   proposes to your code. The agent stays off until you set its feature flag. In the
   terminal, `/run` runs an agent, `/serve` serves it over local HTTP and `/export` writes
   it into a project.

A build tries its models one at a time, cheapest first: by default Nemotron 3.5 Lightning,
then Nemotron 3 Super. Each model gets its own build and its own exam, and the cheapest one
that passes becomes the agent. A model that doesn't pass its exam is never installed,
whatever it scored in practice. [docs/usage.md](docs/usage.md#forge) has the details of
Forge and its rules.

## Change it, and it stops

An agent Architect builds only runs if it passed its exam. Change it, and it stops until it
passes again.

Once the agent is in your project, each call checks its files against the ones that passed.
If a person or a coding agent has edited one, the call is refused before any model is
called, and your app gets one sentence to show: "Stopped: changed after its exam. 0 model
calls.", then the file that changed. The agent's page in the app, and `architect retake`
and `architect undo` in a terminal, offer three things:

- Retake exam: the changed agent takes the exam again and passes if it reaches the pass
  mark, the same rule as the first exam. No earlier score decides anything. Retakes are
  numbered. Where the exam has cases no earlier attempt used, attempt 2 runs on those and
  says so. Where it has none, it is the same exam again and every line says "same exam,
  attempt 2". A pass writes the folder again with its new signed report, and your app's
  calls work.
- Try on practice: scores the change on the practice cases. It is not an attempt and
  uses no exam case, so you can try a change as often as you like before the exam.
- Undo: puts back the last version that passed, byte for byte, and the app works
  again at once.

A retake examines a change to the agent's task and rules text, its checks, its model and
its settings. A change to anything else, such as the pass mark, the budget or the
generated code, can only be undone. An exam on a published set of cases is not retaken
at all, so the published receipt reader can only be undone. The example app has a demo
plan whose exam can be retaken ([examples/expense-app](examples/expense-app/README.md)).

The stop doesn't care who made the edit. It checks the agent's files on every call, so a
change from Claude Code, Codex or you by hand gets the same stop. There's no plugin for
either tool, and I've only recorded it with Claude Code (one run, where it changed the model
to make the agent cheaper) and by hand
([the Claude Code run](docs/bench/retake-trial.md#one-run-with-claude-code)). Codex is
untested.

What this is, and what it is not:

- It stops calls made through the agent's own folder. A coding agent can still edit the
  code that calls it.
- It is a tripwire for a change nobody reviewed, not a lock. Someone who controls the
  computer and the signing key can get past it.
- The exam file is readable on disk. What holds is that no build, practice run or retake
  hands exam cases or per-case exam results to an agent.
- Stopped means not examined. It does not mean worse, and passed does not mean better.
- The demo's receipts are development receipts, used while Architect was being built.
  Scores on them are not the published results.

I gave Architect's own coding agent ten ordinary change requests, written down before any
model call, one run each. Eight changed the agent, and all eight times the app's next call
was refused before a model was called. Three of the changes took the exam again and passed.
Five could not be examined (broken JSON, a model the settings do not have, a setting out of
range, an edit to generated code) and stayed stopped until Undo put back the version that
passed. Two requests changed nothing, and no change failed the pass mark. That is one build,
one agent, one coder model and 100 development receipts per exam. The table is in
[docs/bench/retake-trial.md](docs/bench/retake-trial.md).

## Built on Nebius Token Factory and NVIDIA Nemotron

Architect works with many model providers. With Nebius Token Factory, it gives each job to
one of NVIDIA's Nemotron models:

| Job | Model | Why |
|---|---|---|
| The conversation, the interview and the first draft of the spec | Nemotron 3 Super, thinking off | These are many short turns. Thinking stays off so replies fit their output limits, and Super's list price is under a third of Ultra's. |
| Forge, the harness engineer | Nemotron 3 Super, thinking off, temperature 0, up to 300 s a call | No engineer had a harness accepted on the receipts, Ultra included. Super costs less per turn, and Forge's checks refused fewer of its lines. |
| The agent itself | Nemotron 3.5 Lightning, then Super | The agent runs on every input, so its price is paid on every call. The build tries the cheaper model first. |

Nemotron 3 Ultra and Nano are in the profile too, for `/model`. List prices per million input and
output tokens are $0.06 and $0.24 for Lightning and Nano, $0.30 and $0.90 for Super, and
$1 and $3 for Ultra.

In this setup every model call goes to Token Factory's OpenAI-compatible endpoint with one
API key, so Architect's existing OpenAI-compatible adapter only needed a small dialect. It
always sends `max_tokens`, sets the thinking switch, keeps reasoning out of answers and
retries 429, 503 and 529. Token Factory helped in a few specific places:

- `chat_template_kwargs.enable_thinking` turns reasoning on or off per call. Every role
  in the profile runs with it off, and a profile entry's `think` turns it on.
- Forge's tools run through native tool calls, and strict `json_schema` output works when
  thinking is off.
- `GET /v1/models?verbose=true` returns prices and context lengths, so Architect prices any
  Token Factory model from Nebius's own list, and every cost it shows is at list price.

What I had to work around is logged with repros in [docs/feedback](docs/feedback/README.md).
Two of the biggest items are a thinking reply cut at `max_tokens`, which comes back with
its reasoning as the answer, and the lack of a hard spending cap per API key.

## Results

The worked example is a receipt reader for a small expense app
([examples/expense-app](examples/expense-app/README.md)). It reads a receipt's text and
returns the total, subtotal, tax and item count. The receipts come from CORD v2, and a
receipt counts as right only when all four values are exact. The pass mark, 0.40 (40 of 100
on the exam), was set from a pilot on 150 other receipts before the main run. Each model below read the same 348
receipts, which no build step saw. These are the models alone, with the plain prompt and no
rule checks.

| Model, plain prompt | Exact on 348 unseen receipts, with the 95% interval | Cost per 1,000 receipts | Exam in the build |
|---|---|---|---|
| Nemotron 3 Ultra | 0.529 [0.476, 0.581] | $0.92 | 49 of 100, passed |
| Nemotron 3 Super | 0.517 [0.465, 0.569] | $0.28 | 51 of 100, passed and shipped |
| Nemotron 3.5 Lightning | 0.270 [0.226, 0.319] | $0.058 | 27 of 100, did not pass |

```
Prices: Nebius Token Factory list prices of 2026-10-03.
No build step saw the delivery receipts.
Regions: Lightning is served from eu-north1, Super and Ultra from us-central1, and latency depends on the region.
The models may have seen CORD v2 in pretraining.
The input is text rebuilt from CORD's word annotations. No OCR ran.
Every model ran at temperature 0 with thinking off. NVIDIA recommends 1.0 for these models.
```

Plain Super got 180 receipts right and Ultra 184. The ranges in brackets are 95%
intervals: given 348 receipts, each model's real accuracy on receipts like these most
likely lies inside its range. Super's and Ultra's ranges overlap, and the plan runs no test
of whether the gap between them is real, so this run does not say which of the two is more
accurate. Super's cost per receipt is 30% of Ultra's. Lightning got 94 right.

I read every field that Ultra and Lightning got wrong in a re-run of the pilot. Nearly all
of those misses break a rule the prompt states, most often by giving a subtotal when the
receipt prints none ([analysis](docs/bench/pilot-error-analysis-2026-10-04.md)).

What the build picked: one build tried Lightning, Super and Ultra on 200 other receipts,
100 to practice on and 100 for the exam, and every model sat the exam. Lightning did not
pass, Super and Ultra passed, and the build shipped Super, the cheaper of the two. [docs/bench/results-receipts.md](docs/bench/results-receipts.md) has every number
of the run, and its plan was published before the first model call
([Gist](https://gist.github.com/echoo19/217554e19b2d06d9c583f96732469dd6)).

Forge did not help on this task. In 12 sessions with a Nemotron model as Forge's engineer,
5 with Ultra and 7 with Super, Forge accepted none of the harnesses they wrote. Eleven were
development sessions, and the twelfth ran inside the build above. To be accepted, a harness
has to reach the pass mark on the practice receipts and rank above the tuned preset. None did, so
each of those builds kept Architect's tuned preset, and that is what a build on Nemotron
ships today. Two trial sessions with other open models, DeepSeek V4 Flash and Kimi K2.7
Code, produced no accepted harness either. The development sessions are written up in
[docs/feedback/probes](docs/feedback/probes/).

In the pilot re-run, Ultra broke the subtotal rule about as often as Lightning, so the
receipt reader's spec now declares two rule checks (step 3 above), one for that rule and one
for keeping every digit of an amount. [A second
build](docs/bench/results-receipts.md) with the checks, on the same 200 receipts, got 39, 65
and 71 of 100 on the exam for Lightning, Super and Ultra, against 27, 51 and 49 without them.
Lightning again did not pass, Super and Ultra passed, and Super shipped with 14 more right
at the same cost per exam receipt.

The same spec was built three more times in the desktop app, and the same three agents sat
the same 100 exam receipts each time ([all four
builds](docs/bench/results-receipts.md#four-builds-of-the-checked-design)). Lightning got
39, 39, 42 and 39, never more than two receipts from the pass mark (40 of 100). Only the 42
passed, so that build shipped Lightning instead of Super. The other three shipped Super,
and the picture above is the last of them. Super (63 to 65) and Ultra (70 to 72) were well above
the pass mark every time, so which model ships depends on how Lightning does on the exam.

## Verify a result

The published results ship inside Architect with a `SIGNED.json` that lists the sha256 of
every file and carries an Ed25519 signature. This checks them offline against the public
key that ships with Architect:

```sh
uv run architect verify --published receipts
```

It prints `verified` or `refused: <code>`. A pass means the files are exactly the bytes the
key holder signed. It doesn't prove that the runs behind them were honest. That still rests
on the host that ran them. [docs/trust.md](docs/trust.md) explains the keys and the checks.

## Limits

- Forge has not had a harness accepted with a Nemotron model as the engineer: none in 12
  sessions on the receipts task. A build on Nemotron ships the tuned preset (see
  [Results](#results)).
- Rule checks are written for each task, from rules you state, and only two kinds exist so
  far (`keep_digits` and `not_copied`). Any other rule needs new check code.
- Passing means the agent reached the pass mark on its exam cases. A small exam gives a rough
  score, and inputs unlike those cases can still fail.
- The same agent's score on the same 100-receipt exam moved by up to three receipts from
  one build to the next, even at temperature 0, so a model this close to the pass mark can go
  either way: Lightning got 39, 39, 42 and 39 against a pass mark of 40. Architect does not
  yet rerun an exam or require a margin above the pass mark.
- Cases a model drafts for you are marked synthetic. An exam built from your own real
  cases tells you more.
- Costs are estimates at list prices. Architect stops work at its dollar and time caps,
  but a provider can still bill a call that was running when a cap was hit, and Token
  Factory has no hard spending cap per key.
- If you let the coder (the subagent that makes code changes for you) run shell
  commands, they run on your machine with your permissions. That is not a sandbox.
- Stopping a changed agent covers calls made through the agent's folder, on a computer
  whose signing key nobody else holds. It is not a lock
  (see [Change it, and it stops](#change-it-and-it-stops)).
- The Windows installer is not code-signed, and the Mac build may not be notarized, so
  either system may warn before the app runs.

[docs/limitations.md](docs/limitations.md) covers the rest.

## License

Architect is released under the MIT License ([LICENSE](LICENSE)). It includes or adapts
work under other licenses, listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md):
Lectern's design tokens, motion styles and mark (MIT, contributed by Lectern's owner), code
adapted from Hearth (MIT), Pi (MIT), Tabler Icons (MIT), the Inter and JetBrains Mono
fonts (SIL Open Font License 1.1), and receipt text derived from
CORD v2 (CC BY 4.0).
