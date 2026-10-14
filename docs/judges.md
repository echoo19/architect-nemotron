# Testing Architect

Architect is an agent factory. You type a job in one sentence and it builds a small AI agent on NVIDIA Nemotron, through Nebius Token Factory. The agent only runs if it passed its exam, so change it and it stops until it passes again. This page walks you through both halves: build one, then change it and watch it stop. The first path takes about ten minutes. A longer one, on 100 real receipts, is under it.

You need a Windows or macOS computer and your judge code. You do not need a Nebius account or a key of your own.

## Your judge code

Your judge code and three spare codes are in the testing instructions of my Devpost submission, never in this repository.

The code pays for Nemotron calls through a relay I run at https://architect-relay.vercel.app/v1, which keeps my Token Factory key on its server. Architect already knows that address. The code allows $3 a day and $10 in all. Days end at 00:00 UTC. Once a day's $3 is spent, the relay refuses model calls until then, and Architect tells you so. Switch to a spare code if that happens, as shown under "Switch to a spare code" below. Each spare allows $1.

## The ten-minute path

This builds a small invoice reader, adds it to a folder, changes one line of it and shows the stop. About three of the ten minutes are the app's first-open download. The build took 69.8 seconds and cost $0.0095 in my run. Every time and cost in this section is from that one run on 2026-10-13, on my machine, with my Nebius key directly, not a judge code through the relay, so yours will differ.

The exam in this path is six cases a Nemotron model made up. It shows the workflow, not accuracy.

1. From https://github.com/echoo19/architect-nemotron/releases download `Architect-Setup-0.1.0.exe` for Windows, or the `.dmg` for your Mac (`arm64` for Apple silicon, `x64` for Intel), and run it. If Windows SmartScreen stops it, choose More info, then Run anyway. On a Mac, open the `.dmg` and drag Architect to Applications. If macOS says Apple could not verify Architect, click Done, open System Settings, then Privacy & Security, scroll to Security and click Open Anyway next to Architect. On macOS 14 or earlier you can Control-click Architect in Applications and choose Open instead.
2. Open Architect. The first time it opens, it downloads Python 3.12 and Architect's packages (about three minutes), so it needs an internet connection. The window shows each step. Then, under "API key", choose Nebius Token Factory, then Continue.
3. Paste the judge code where the app asks for it, then click Check code. The app checks the code with the relay and lists four Nemotron models.
4. Nemotron 3 Super (`nvidia/nemotron-3-super-120b-a12b`) is already chosen. Click Use this model.
5. On "Where should Architect work?", the app shows a folder (your home folder the first time). Click Change and pick a new, empty folder, so the agent's files in step 10 are easy to find, then Start.
6. Click New agent at the top of the sidebar. The app shows six steps across its top bar: Describe, Plan, Build, Results, Try it and Add to project. Click the example "Pull the invoice number, date, vendor and total out of invoice text", then click Write the plan. You can describe a job of your own in one sentence instead, and then the names and numbers below will differ.
7. Architect asks three questions first: where the agent will live, the name of the input field, and one example input. Each comes with a suggested answer, and the suggested answers work. The Plan step then shows what the agent takes and gives back, the pass mark, the models it will try and, at the bottom, the most the build may cost and how long it may take. Click Build agent. Each model practices with one of Architect's built-in strategies, tuned on the practice cases (the tuned preset).
8. When the build ends, the Results step says which model passed and became your agent, or that none reached the pass mark. Its table shows each model's exam score against the pass mark and what it costs per 1,000 runs. In my run the agent was named `pull-invoice-number`, and Nemotron 3.5 Lightning passed with 6 of 6 against a pass mark of 5.
9. Click Try your agent, paste your own input and click Run agent. The answer shows which model ran, the cost of the run and its time.
10. Click Add to project, check the list of files, then click Write the files. Architect writes the agent into one new folder and changes no existing file. Connect it is optional: it opens a conversation with Architect's coder, with the request typed in, and you allow or refuse each edit to your code. In my run this wrote 13 files into `agents/pull_invoice_number/` in the folder from step 5.
11. Open `agents/pull_invoice_number/artifact/config.json` in any editor. Its `"model"` line read `"nvidia/Nemotron-3_5-Lightning"` in my run. Change the value to `"nvidia/nemotron-3-super-120b-a12b"` and save. If your build picked Super instead, change it to Lightning: the edit is to the other of the two.
12. Open Your agents in the sidebar. The agent's row reads "Stopped: changed". Click it, and its page reads "Stopped: changed after its exam. 0 model calls." and "Changed: artifact/config.json (model: Nemotron 3.5 Lightning to Nemotron 3 Super)." It offers Try on practice, Retake exam and Undo. In my run the next call to the agent was refused in 0.02 seconds with 0 model calls.
13. Click Undo, then Put back the version that passed. The version that passed is back with no model call (0.2 seconds in my run), and the agent answers again. Or, instead of Undo, click Retake exam, then Start attempt 2. The page says "Same exam, attempt 2", because an exam a model drafted has no unused cases. In my run the retake took 6.5 seconds, cost $0.0006 and passed with 6 of 6, and the folder then ran on Super. Do one or the other: after a retake passes, Undo puts back the retake's version, not the first one.

On this small job the retake passes, so it doesn't show a retake that fails. The longer path below has one.

Forge, which writes a custom harness around the model instead of the tuned preset, runs only if you choose it: type `/forge` before the description, or on the Plan step click Edit plan, set Harness to "Written for this job", save, and click Build with changes. Forge can take up to $1.20 and 45 minutes more.

## The longer path: the receipt reader

This path is optional. It builds on 100 real receipts and takes about 20 minutes. My measured results are about this example. It needs a copy of the repository: on https://github.com/echoo19/architect-nemotron click Code, then Download ZIP, and unzip it (or `git clone` it).

1. Open the repository's `examples/expense-app` folder. On a first run, that's the folder to pick in step 5 above. If you're already past it, click the folder button next to New agent in the sidebar (its tip says Another folder), choose Choose a folder, and pick `examples/expense-app`. That opens a new agent in that folder. Its `architect.json` sets up the build the way my evaluation ran it: the same 200 receipts, a pass mark of 0.40 (40 of 100), and Nemotron 3.5 Lightning, then Super, then Ultra. The cheapest model that passes its exam ships.
2. On the Describe step, click Open a plan file you saved (.json), pick `receipt-reader.json` and click Write the plan (or type `/make @receipt-reader.json` and press Enter). Architect loads that spec as it is, without questions. The Plan step shows "Pass mark: 40 of 100" and says it was set in receipt-reader.json, and View all, next to the rules, lists the two that code also checks on every answer. Edit plan shows the spec's full hash. The spec is byte for byte `examples/bench/eval-2026-10-06/ladder/spec.json`, and its hash is the id of the agent my second build installed, the one with the checks (`docs/bench/results-receipts.md`).
3. Click Build agent, then go on from step 8 above.

In a terminal, after steps 1 to 3 under "In a terminal" below, run `cd examples/expense-app`, then `uv run --project ../.. architect`, type `/make @receipt-reader.json` and press Enter at "Compile this agent?".

In my 2026-10-06 run of this spec, through the relay judges use, the build took about 18 minutes and cost $0.31, which comes out of the code's $3 for the day.

### Change the receipt reader and watch it stop

Here my change by hand to Lightning got 29 of 100 on its retake and did not pass. Use the demo plan in the same folder. The receipt reader above took a published exam, and Architect doesn't retake a published exam.

1. In the same folder, click New agent, then Open a plan file you saved (.json), pick `receipt-reader-demo.json`, and click Write the plan and Build agent. In my run it took 20 minutes and cost $0.31, and Nemotron 3 Super passed with 59 of 100 against a pass mark of 40. Its receipts are development receipts, used while Architect was being built, so this score is not one of my published results.
2. When the build ends, click Try your agent, then Add to project, then Write the files, as in steps 9 and 10. The agent goes to `agents/receipt_reader_demo`. Open Your agents in the sidebar and click the agent: its page now starts with "Passed. It is your agent."
3. Open `agents/receipt_reader_demo/artifact/config.json` in any editor, change the model to `nvidia/Nemotron-3_5-Lightning` and save. Reopen the agent's page: it reads "Stopped: changed after its exam. 0 model calls." and names the file and the two models.
4. Try on practice scores the change on 100 practice receipts and is not an attempt. Retake exam, then Start attempt 2, is attempt 2, on 100 receipts no earlier attempt used. In my run both cost under one cent and took about 75 seconds, practice gave 24 of 100, and the retake read "29 of 100. Pass mark 40. Did not pass."
5. Undo, then Put back the version that passed, restores the version that passed, and the page reads "Passed. It is your agent." again.

The stop is file-based, so it fires whoever makes the edit. I've recorded it with Claude Code (one run, in `docs/bench/retake-trial.md`) and by hand. There's no plugin for Claude Code or Codex, and Codex is untested.

In a terminal, from `examples/expense-app`: `uv run --project ../.. architect retake agents/receipt_reader_demo --profile architect.json --practice`, the same without `--practice`, and `uv run --project ../.. architect undo agents/receipt_reader_demo`. The example app's README shows how the app itself answers while its agent is stopped.

## In a terminal

You need git, uv (https://docs.astral.sh/uv/) and Node 20.6 or later.

1. Run `git clone https://github.com/echoo19/architect-nemotron`, then `cd architect-nemotron`.
2. Run `uv sync --frozen --python 3.12`, then `npm ci --no-audit --no-fund --prefix architect/backends/pi-node`.
3. Run `uv run architect judge <code>`, with your judge code in place of `<code>`. It checks the code and tells you what it has left.
4. Run `uv run architect` and press Enter at each first-run question. The defaults pick Nebius Token Factory, keep the stored code and use Nemotron 3 Super.
5. Type `/make` and a description of the job, answer its questions, then answer y to "Compile this agent?". The terminal uses the build's own words: the floor is the pass mark, tuning cases are the practice cases, held-out cases are the exam, compiling is building, and a licensed agent is one that passed.
6. When the build ends, the result card shows each exam score against the floor. Run an agent that passed with `/run <name>` and your input, for example `/run receipt-reader` followed by a receipt's text. The line under the answer shows its cost, its time and its number of model calls.

## Switch to a spare code

The desktop app and the terminal keep the code in the same place, so switching in one switches both. A conversation that is already open goes on answering with the old code. Start a new one after you switch.

In the desktop app:

1. Click Settings at the bottom of the sidebar, then Models and sign-in.
2. Under API keys, click Change code next to Nebius Token Factory.
3. To "Nebius Token Factory already has a stored judge code. Replace it?", choose y.
4. Paste the spare code into the field (it reads Paste it here), then click Save. The app checks the code with the relay, and the page says Connected.
5. Start a new conversation.

In a terminal:

1. Leave Architect with `/exit`.
2. Run `uv run architect judge <spare code>`. It replaces the stored code and tells you what the spare has left.
3. Run `uv run architect` again.

From `examples/expense-app`, the two commands are `uv run --project ../.. architect judge <spare code>` and `uv run --project ../.. architect`.

## Check the signed results

Run `uv run architect verify --published receipts`. It checks my published receipt results and their signature on your computer. It needs no code and no network.

## If something goes wrong

- "The judges' relay does not know this code": check that the whole code was pasted, or use a spare.
- "This judge code has reached its daily limit": the code's day resets at 00:00 UTC. Use a spare until then. A build that stops this way says so in its result.
- "The judges' relay is switched off for now": try again later.
- "The judges' relay cannot reach its ledger right now": nothing was charged, and a build stops there and says so. A spare code goes through the same ledger, so try again later.
- A call is refused as rate limited (HTTP 429): the relay takes at most 300 requests a minute from one address, and each code may use only a share of each model's per-minute limit, with at most 8 calls at once. Wait a minute, then try again.
- The receipt reader doesn't pass because it went over budget on the exam: its spec allows 60 seconds a run, and a run is over budget when a model takes longer than that to answer. Build it again later.
- Every model call fails: open https://architect-relay.vercel.app/healthz. It shows `"ok":true` while the relay is up. If it does not, the problem is on my side.
- The app never asks for a key: a `NEBIUS_API_KEY` set on your computer comes first. Architect then runs on your own key, and you do not need the code.
