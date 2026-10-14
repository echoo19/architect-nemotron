# Reference

The long form of the [README](../README.md): how to run Architect from a checkout, its
commands, and what each stage of a build does. [usage.md](usage.md) goes deeper into each
command, and [desktop.md](desktop.md) covers the desktop app from source.

## The terminal agent

Architect's terminal agent is a coding agent you talk to in a terminal, and its specialty
is building agents. Ask it anything and it works through subagents. Describe an agent and it
builds one for that job, tests it on cases the build never saw, and then installs it,
exports it into your codebase, or throws it away.

### Set up from a checkout

You need Python 3.12, uv, and Node 20.6 or later (CI uses Node 22). From the repository
root:

```sh
uv sync --frozen --python 3.12
npm ci --prefix architect/backends/pi-node
```

The TypeScript client for the HTTP service is separate and needs Node 22 or later. Only
scripts that talk to the service need it:

```sh
npm ci --prefix client-ts
npm run build --prefix client-ts
```

### Start

```sh
uv run architect                                   # interactive; the first run walks you through sign-in
uv run architect -p "what does src/app.py do?"     # one turn (--json for a JSON object)
uv run architect -c                                # continue the last conversation (-r <id> for one)
```

Run it from the project you want it to work in (`uv run --project <architect-checkout>
architect` from another directory), or install it as a tool.

### Connect a model

The first run detects what you already have and recommends one. `/login` adds more at any
time, `/model` switches, and `/providers` shows each provider's status.

| Kind | Providers |
|---|---|
| Subscriptions | Claude (the logged-in `claude` CLI), ChatGPT (`/login chatgpt` runs the official `codex login` OAuth) |
| API keys | Nebius Token Factory (NVIDIA Nemotron), Anthropic, OpenAI, OpenRouter, Gemini, Groq, DeepSeek, xAI, Mistral, Together |
| Local, $0 | Ollama, LM Studio, llama.cpp, vLLM, any OpenAI-compatible URL |

API providers stream answers and use native tool calling. Text-only CLIs use a JSON
protocol that works with any model. Keys stay out of logs, transcripts and the agent's own
file tools. A model without a known price works for conversation, with its cost shown as
unknown, but Forge will not build on it until you declare a price.

### Modes

Shift+Tab cycles three modes: normal, build and plan. `/mode` sets normal or build
(`/mode forge`, the old name for build mode, still works), and `/permissions plan` sets
plan. The app's composer has the same switch, labeled Chat, Build and Plan.

Normal mode answers questions and delegates. Code changes go to the coder subagent, which
asks before every write, edit and shell command (`/permissions ask | auto-edit |
full-auto`), and structured jobs go to preset subagents (`/spawn`). Architect never edits
your files itself. `/undo` restores the coder's last changes and `/diff` shows them.

Build mode turns a description such as "make an agent that extracts invoice fields from
text" into a tested, licensed agent. The interview first asks where the agent will live.
"Run it here" installs it, so you can use `/run`, `architect run` or `/serve` over HTTP,
and local models are allowed. "Put it into software" writes a verified package into your
repository after the license, but only once you approve the list of files, and it prints
the Python and HTTP usage. `/make <text>` runs one request this way, `/forge <text>` does
the same with Forge forced on, and `/forge arc` builds an ARC-AGI-3 harness.

Plan mode is read-only. The coder is not called, and changes are proposed instead.

### Commands

`/help` lists them all: `/make` `/forge` `/mode` `/permissions` `/login` `/logout`
`/model` `/providers` `/models` `/agents` `/run` `/recompile` `/report` `/export` `/serve`
`/floor` `/budget` `/library` `/bench` `/spawn` `/mcp` `/config` `/doctor` `/context`
`/compact` `/clear` `/diff` `/undo` `/review` `/memory` `/init` `/image` `/sessions`
`/resume` `/title` `/copy` `/cost` `/status` `/telemetry` `/exit`. There is also `@file`
to attach text or an image, `!cmd` to run a shell command yourself, Esc to interrupt,
Ctrl+J for a new line, Ctrl+G for your editor and Ctrl+R for history. Custom commands come
from `.architect/commands/*.md`, and MCP servers from `.mcp.json` or `config.toml`.

### Where things live

Everything goes in `~/.architect` unless `--workspace` or `ARCHITECT_WORKSPACE` says
otherwise. `config.toml` sits there, and per project in `.architect/config.toml`. Pipes, CI
and terminals without a console get a plain line UI. The older step-by-step session is
`architect session` (see [below](#the-step-by-step-session)), and the other subcommands
are for [scripts](#scripts-and-other-subcommands).

## Nebius Token Factory

Architect runs on [Nebius Token Factory](https://tokenfactory.nebius.com/) with NVIDIA's
open Nemotron models. With `NEBIUS_API_KEY` set, or the key pasted at `/login nebius`, the
first run recommends Nebius and writes a profile that puts each role on a Nemotron model:

| Role | Model | Thinking |
|---|---|---|
| Conversation, interview, Stage 0 (the spec and floor drafts) | Nemotron 3 Super | off |
| Forge, the harness engineer, when you choose Forge | Nemotron 3 Super (temperature 0, 300 s per call) | off |
| The agent it builds | Nemotron 3.5 Lightning, else Super (the cheapest that passes) | off |

Nemotron 3 Ultra and Nano are in the profile too, for `/model`. The four Nemotron models carry
Nebius's list prices per million input and output tokens: $0.06 and $0.24 for Nano and
Lightning, $0.30 and $0.90 for Super, and $1 and $3 for Ultra (the source is cited in
`architect/models/pricing.py`). Any other Token Factory model is priced from Nebius's
model list.

```sh
export NEBIUS_API_KEY=...          # from https://tokenfactory.nebius.com/project/api-keys
uv run architect                   # first run: pick Nebius; Enter keeps Nemotron 3 Super
uv run architect --profile examples/nebius/profile.json --model nvidia/nemotron-3-super-120b-a12b
```

Hackathon judges get a code instead of a key. Run `uv run architect judge <code>`, or paste
the code where the first run asks for the Nebius key, and Nemotron calls go through a relay
that holds the key and caps each code's spend ([judges.md](judges.md),
[deploy/relay](../deploy/relay/README.md)).

Requests to Nebius always carry `max_tokens`, so reasoning cannot run past the cost
Architect reserved. Each profile entry's `think` switches reasoning through
`chat_template_kwargs.enable_thinking`. It is off unless set, and always off for
JSON-schema calls. Reasoning is never shown or used as an answer. A reply that runs out of
tokens while still reasoning stops with `provider_reasoning_cut`. HTTP 429, 503 and 529 are
retried twice, honoring `Retry-After`, and then reported as `provider_busy`.
`scripts/nebius_live_check.py` checks all of this against the live API (four models,
thinking on and off, plain, tool and JSON-schema calls) under a $0.05 cap, and writes its
results to `docs/feedback/probes/`.

## How a build works

1. Describe the task, in plain words or as a JSON spec. A model drafts the spec from your
   words, and you review it and approve its exact hash. The spec holds the input and output
   schemas, the quality floor (the bar, with a dataset of cases) and the dollar and time
   budget.
2. Select and tune a preset. Stage 1 picks the preset strategies that fit the spec. Stage 2
   tunes their parameters and measures each candidate on the tuning half of the floor's
   cases only. The app calls these the practice cases.
3. Forge. A coding agent on the pinned Pi SDK writes the harness as code in a restricted
   sandbox language (language v2: loops under fuel, helpers, grid builtins, no imports).
   It checks and measures its drafts on tuning cases only, and never sees a held-out case
   or score. Builds from the app and from `/make` run Forge unless the engineer is the
   tuned preset only. That is the default on Nebius Token Factory, where Forge runs only
   when you choose it: `/forge`, or Harness under Edit plan in the app. A Nebius profile
   from the first run caps Forge at $1.20 and 45 minutes. A `compile`
   runs it only when `--forge` or the profile's `compile.forge` turns it on, and then only
   when the tuned preset misses its floor or budget on tuning, unless `--forge-trigger
   always` forces it. Without Forge, Stage 3 is the ordinary optional rewrite.
4. Stage 4 verifies. The best candidate runs once on the held-out half, which the app
   calls the exam. This is the only step that can license a harness. Tuning numbers, Forge
   measurements and dry runs are evidence, never a license.
5. Install and run. A licensed harness can be installed into the registry, with a
   separate exact-spec approval, and run with a budget.

```sh
# Normal compile of a reviewed spec
uv run architect compile reviewed.json --profile profile.json --approved-spec reviewed.json
# The same compile with Forge in the Stage 3 slot (shows the summed cap and asks first)
uv run architect compile reviewed.json --profile profile.json --approved-spec reviewed.json \
    --forge --forge-usd 5 --forge-minutes 60
# Bench: the preset against the agent-free template arm and the Forge arm ($0, fake
# provider; the scanned pages need the bench extra)
uv run --extra bench architect bench run --provider fake --tier smoke --seed 7 \
    --variant default --variant template --variant forge
```

On the receipts task, Forge has run live on Nebius Token Factory in 12 sessions with a
Nemotron model as its engineer: 11 development sessions and one inside the evaluation's
ladder build. None produced an accepted harness; see the README's
[Results](../README.md#results), [usage](usage.md#forge) and
[limitations](limitations.md#forge).

## The step-by-step session

```sh
uv run architect session
```

On the first run the session looks for a logged-in `claude` or `codex` CLI on PATH, writes
`.architect/session-profile.json` after you confirm, and checks the login with one tiny
metered call. An abridged run, with illustrative paths and dollar amounts:

```
architect 0.1.0  workspace /home/you/proj/.architect (new)  profile none
Nothing printed by this session is a license or an approval. Costs are API-equivalent estimates, not a bill.
looking for logged-in CLIs on PATH: claude found (/usr/local/bin/claude), codex not found
Choose a model for this session:
  1) claude-haiku-4-5-20251001  claude-code  min reservation/call $0.35  can infer Specs: yes   (default)
model> 1
confirm> write /home/you/proj/.architect/session-profile.json? [Y/n] y
checking claude-code login with one tiny metered call (about $0.35 reserved, typically under $0.01, up to 90 s)...
claude: logged in (3.1s, $0.0006)
architect>
```

At `architect>` you describe what the agent should do. The session proposes a spec, lists
blockers with the command that fixes each, and helps you build a held-out floor dataset.
Cases a model drafts are labeled SYNTHETIC and reviewed one by one. It compiles only after
you type `approve <hash8>` for the exact spec on screen, and `install <name> <hash8>` is the
same rule for the registry. Nothing a model outputs can approve. Costs shown are
API-equivalent estimates, not bills. With no CLI, the same flow runs offline against the
checked-in synthetic profile, with commands one per line in `commands.txt`:

```sh
uv run architect session --profile examples/lectern/profiles/content-recommender.json --script commands.txt --no-probe
```

[Interactive session](usage.md#interactive-session) covers the states, the command table
and a real fixture transcript. That transcript and the quickstart below are synthetic,
offline evidence only. Live CLI runs are recorded in [PROGRESS.md](../PROGRESS.md).

## Offline library quickstart

Save this as `quickstart.py`, then run `uv run python quickstart.py` from the repository
root. It compiles and runs one synthetic, offline agent. It needs no API key, and the
temporary artifact is not installed.

```python
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from architect.compiler import Compiler
from architect.floors import EvaluationCase, HeldOutTasks
from architect.models import FakeAdapter
from architect.runtime import Runtime
from architect.spec import Budget, FloorDefinition, Spec
from architect.telemetry import TelemetryStore

async def main():
    floor = FloorDefinition(kind="held_out_tasks", limit=1, dataset="demo")
    cases = [EvaluationCase(case_id=str(i), task={}, expected="ok") for i in range(6)]
    spec = Spec(task="Return ok", input_schema={"type": "object"},
                output_schema={"type": "string"}, domain="synthesizer",
                floor=floor, budget=Budget(cost_usd=0.1, compute_s=60))
    with TemporaryDirectory() as work, TelemetryStore(Path(work) / "telemetry") as store:
        runtime = Runtime(store)
        compiler = Compiler(Path(work) / "artifacts", runtime, FakeAdapter,
                            lambda definition: HeldOutTasks(definition, cases))
        result = await compiler.compile(spec)
        if result.artifact is None or not result.artifact.licensed:
            raise RuntimeError(result.failures)
        print((await runtime.run(result.artifact, {}, FakeAdapter())).output)
asyncio.run(main())
```

## Routing and Forge delivery modes

Architect can route a task to a verified version that already exists: an installed
registry entry (reused only when you grant it), a library template (a warm start that
Stage 4 still verifies), or a fresh compile. Routing orders licensed versions and never
licenses. Add a `routing` table to a profile to route every compile. Without one, a
compile never routes unless you grant reuse for that call.

```bash
architect route spec.json --profile architect.json          # advice: the routing table
architect route spec.json --profile architect.json --run '{"text": "hi"}' --yes
architect do "goal" --profile architect.json --repo . --reuse-installed
architect forge run spec.json --profile architect.json --input '{"text": "hi"}'
architect forge integrate "feature" --profile architect.json --repo . --into pkgs \
  --approved-spec approved.json --stage-only
architect export verify .architect/exports/<digest16>/<package>
architect roles --profile architect.json
```

`forge run` builds a one-shot harness, licenses it with Stage 4, runs it once and expires
it. `forge integrate` exports a licensed harness as a package behind a disabled feature
flag for human review. `recompile` always runs a fresh Stage 4. It may warm-start from a
template but never reuses the installed entry. See
[usage](usage.md#routing-and-forge-delivery-modes).

## Scripts and other subcommands

These subcommands work from scripts: `compile`, `run`, `recompile`, `status`, `registry`,
`do` and `create`.

```sh
uv run architect do "add a CLI that prints the registry" --profile profile.json --repo .
uv run architect create "add an AI feature" --profile profile.json --repo .
```

They need your own trusted profile, configured models and evaluation data. `do` breaks a
goal into parts and runs ephemeral compiled agents. `create` uses a compiled builder agent
with a bounded child compiler. Installed features need a separate exact-spec review
(`--approved-spec reviewed.json`). Neither command is a hard-coded task router, and
neither is permission to deploy without review. [usage](usage.md) covers profile setup,
approval and HTTP and TypeScript examples.

## Efficiency and refusals

There are no token quotas. Tokens measure usage and efficiency, and the compiler looks for
a cheaper, simpler harness that still meets the quality floor. Dollar and elapsed-time
limits are safety gates. Provider context and output capacities are separate from
Architect's budgets.

Architect refuses unlicensed or changed artifacts, missing evaluation data, uncalibrated
judges, failed floors, unapproved installs, expired one-shot agents, and dollar or time
overruns. Reusing a cache still requires a fresh held-out verification. Task content never
goes into telemetry. A refusal is not a partial license.

The coder runs upstream Pi 0.73.1 itself, not a replacement coding loop. Pi's file tools
are confined to the repository and denied Architect's private control-plane paths. An
explicit `pi.allow_shell` permission runs trusted host code. It is not an OS or network
sandbox, so never grant it to a checkout you do not trust.

## Checks

```sh
uv run ruff check .
uv run mypy --strict architect
uv run pytest --cov=architect --cov-report=term-missing --cov-report=json
npm test --prefix client-ts
uv sync --frozen --extra arc
uv run --extra arc pytest tests/test_game_official.py tests/test_game_factory.py tests/test_competition.py
```

The optional ARC extra pins SDK `arc-agi==0.9.9` and engine `arcengine==0.9.3`. Local games
have three synthetic families and 15 distinct puzzle instances. The official OFFLINE
adapter requires staged public files and a trusted manifest. No live official game files
or official benchmark results are included, and Kaggle numeric caps and submission
compliance are not confirmed.

[.github/workflows/ci.yml](../.github/workflows/ci.yml) defines the Python, Pi and
TypeScript checks and the optional-dependency ARC tests. [PROGRESS.md](../PROGRESS.md)
records what has been run.

## More

- [Usage and integrations](usage.md)
- [Specifications and pipelines](specs.md)
- [Quality floors](floors.md) and [telemetry privacy](telemetry.md)
- [Signed results and trust](trust.md)
- [Architecture reference](spec.md) and [limitations](limitations.md)
- [Examples](../examples/lectern/README.md), [decisions](../DECISIONS.md) and
  [external questions](../QUESTIONS.md)
