# Expense app

A small web API for a bookkeeper. Post a receipt's text to `/receipts` as `{"text": "..."}`, adding `"date"` when it is not from today, and the app reads its total, subtotal, tax and item count and keeps them in SQLite (`EXPENSES_DB`, by default `expenses.db`). `GET /receipts` lists what was read and `GET /totals/daily` adds up each day's totals. Amounts are the printed digits read as one whole number, so "Rp 12.500" is 12500; an amount the receipt does not print is null.

The reading happens in one place, `extract()` in `app/extract.py`. Until Architect's receipt agent is added to this project, it sends each receipt to a large general model, NVIDIA Nemotron 3 Ultra on Nebius Token Factory, with an HTTPS request from Python's standard library. `NEBIUS_API_KEY` holds the key, and `NEBIUS_BASE_URL` and `NEBIUS_MODEL` change the address and the model. A judge code works too: set `NEBIUS_BASE_URL` to the judges' relay, `https://architect-relay.vercel.app/v1`, and the code as `NEBIUS_API_KEY`. Architect reads `NEBIUS_API_KEY` as well, before any key it has stored, so set it only in the shell that runs the app. Once the agent is in `agents/receipt_reader/`, `extract()` uses it instead; the sections below show how.

Run the app with `uv run uvicorn app.main:app` and the tests with `uv run pytest`. The tests use a fake reader and call no model, except `tests/test_rules.py`, which checks the rules in [RULES.md](../receipts/RULES.md) against the real reader and is skipped until the agent is in the project.

## The receipt reader

`receipt-reader.json` is the receipt reader's spec from my [evaluation plan](../../docs/bench/prereg-nebius.md), and `architect.json` is an Architect profile that builds it the way the evaluation did: the 200 evidence receipts in `../receipts/floors/evidence.json`, a pass mark of 0.40, and Nemotron 3.5 Lightning, then Super, then Ultra, where the cheapest model that passes its exam ships. Open this folder in Architect and type `/make @receipt-reader.json`. In a terminal, start Architect from this folder with `uv run --project ../.. architect`. The plan shows the pass mark, the spec's full hash and the two rules the spec checks in code. The build uses a tuned preset. The profile keeps keys, agents and reports in the workspace Architect uses, which is `~/.architect` unless `ARCHITECT_WORKSPACE` or the app's workspace setting moves it. The first run and `architect judge` store your key or judge code there.

## Using the agent in `extract()`

Once a model passes and the reader is your agent, Add to project in the app (`/export receipt-reader` in a terminal) writes it into `agents/receipt_reader/` as a Python package that carries its signed exam report and its own tests. The demo plan's agent, `receipt-reader-demo`, goes to `agents/receipt_reader_demo/`, and `extract()` looks in both. It uses the folder as soon as it is there, and goes back to the general model if you delete the whole folder. To run it:

1. Read the package's `README.md` and `EVIDENCE.md`. The agent stays off until you turn on its feature flag in step 3.
2. Install Architect in the app's environment, since the package runs on Architect's runtime. From this folder in a copy of the Architect repository, run `uv add --editable ../..`. Anywhere else, give the path to your copy of the repository instead of `../..`.
3. Wherever the app or its tests run, set `ARCHITECT_FEATURE_4D80F3D5=1`. That name is the package's `FLAG`, in its `__init__.py`: `ARCHITECT_FEATURE_` and the first eight characters of the hash of the spec the agent was built from, in capitals. A package built from another spec, such as the demo plan below, has another name. The name stays the same when the agent later passes a retake.
4. The package checks its exam report each time it loads, so also set `ARCHITECT_SIGNING_KEY_FILE` to the full path of `signing.key` in Architect's workspace (`~/.architect` unless you moved it), the key Architect signed the report with on your computer.
5. Run `uv run pytest`. `tests/test_rules.py` now runs as well: its six tests send seven receipts to the agent, and each run is paid for with `NEBIUS_API_KEY` when it is set, else with the key or judge code stored in Architect's workspace. If you moved that workspace, set `ARCHITECT_WORKSPACE` to it wherever the app and its tests run. Start the app as before, with the variables from steps 3 and 4 set.

The agent runs with this folder's `architect.json`, which names the models and where their key is kept, and it records each run in Architect's workspace. When the app starts, its terminal prints one line about the agent, such as `Receipt agent: passed its exam. 59 of 100, pass mark 40, attempt 1.` (the demo plan's agent in my trial). Any failure of the agent becomes an `ExtractionError`, so the API still answers 502 when a receipt can't be read. The reply names the kind of failure, and the detail stays in the app's terminal.

## When the agent's files change

The agent only runs if it passed its exam. If any file in `agents/receipt_reader/` changes afterwards, whether a person or a coding agent edited it, the next call is refused before any model is called. The API answers 502 with

    Could not read the receipt. Stopped: changed after its exam. 0 model calls. Changed: artifact/config.json (model: Nemotron 3 Super to Nemotron 3.5 Lightning). Retake its exam or undo the change in Architect.

and the app's terminal prints the same sentences. The last one depends on the change. `extract()` asks Architect where the agent stands (`architect.session.retake.standing`, no model call), so a change a retake cannot examine ends "Undo the change in Architect." and names why, such as "The change touches the budget.", and a file with a mistake names it and ends "Fix it or undo.", such as "retries is 2. It can be 0 or 1." From this folder, with the variable from step 4 set:

    uv run --project ../.. architect verify agents/receipt_reader
    uv run --project ../.. architect retake agents/receipt_reader --profile architect.json --practice
    uv run --project ../.. architect retake agents/receipt_reader --profile architect.json
    uv run --project ../.. architect undo agents/receipt_reader

`verify` says what stands, with no model call. `retake --practice` scores the change on the exam's practice receipts and is not an attempt. `retake` is the exam again, numbered attempt 2: the changed agent passes if it reaches the pass mark, and then the folder is written again with its new signed report and the app's calls work. If it does not pass, the folder stays as it is and stays stopped. `undo` puts back the last version that passed, byte for byte, at no cost.

A package pins the exact version it was imported as, so a running program has to import it again after a retake passes. `extract()` does that itself when the folder has been written again, so this app needs no restart. A program of your own can restart.

A retake can examine a change to the task and rules text or the checks in `artifact/spec.json`, and to the model or its settings in `artifact/config.json`. Architect writes `artifact/harness.py` again from those two files and never runs an edited one. A change to any other file, or to a fixed field such as the pass mark or the budget, can only be undone.

This stops calls made through the agent's own folder. It is a tripwire for a change nobody reviewed, not a lock: a coding agent can still edit `extract()` itself, and someone who controls this computer and the signing key from step 4 can sign anything.

## The demo exam

The evidence receipts above are a published exam, and Architect doesn't retake a published exam, so the exam of `receipt-reader.json` cannot be retaken: `architect retake` refuses it, and a change to that agent can only be undone. To try a retake, build `receipt-reader-demo.json` instead (`/make @receipt-reader-demo.json`), add it to this project, and use `agents/receipt_reader_demo` in the commands above. It is the same task without the two checks, on `bench-receipts-demo`: 200 development receipts (100 practice, 100 exam) with 150 more that only retakes use, so the first retake runs on 100 receipts no earlier attempt used and the next one repeats the first exam and says so. All 350 were used while Architect was being built. Scores on them are not my published results, and no evidence exam receipt and no delivery receipt is among them. `scripts/build_demo_floor.py` in the repository writes the files in `../receipts/demo/`.
