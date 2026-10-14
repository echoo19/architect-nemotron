# Usage

Run commands from the repository root after the [setup from a checkout](reference.md#set-up-from-a-checkout).
Use a trusted local profile. Do not load a profile supplied by a model or an
untrusted customer. CLI JSON and successful HTTP outputs can contain task data;
they are not the private, content-free telemetry journal.

## Quick start: the Architect CLI

`uv run architect` opens the conversational CLI. Run it from the project it should work in
(`uv run --project <architect-checkout> architect` from another directory). Phase 13
contract: `docs/interfaces/phase13-cli-complete.md`.

### First run and providers

The first run lists what it found (logged-in `claude`/`codex` CLIs, API keys in the
environment, reachable local servers), recommends one, and writes
`<workspace>/session-profile.json` with every model that provider offers. Enter accepts the
recommendation.

| Command | Effect |
|---|---|
| `/login [provider]` | Guided sign-in: a key prompt (hidden where the terminal allows), the official CLI login (`/login chatgpt` runs `codex login`), or a local server check |
| `/logout <provider>` | Forget a stored key and the models that need it |
| `/providers` | Every provider: signed in, key present, server reachable |
| `/models [provider] [filter]` | Live model list |
| `/model` | Numbered picker; `/model <n>` or `/model provider:model` switches the conversation and the next compile; `/model add p:m --price in/out` declares a price |

Keys come from the environment first, else `<workspace>/credentials.json` (private mode).
They never appear in logs, telemetry, transcripts or the agent's file tools. Local servers
cost $0 (time is their budget). A model with no known price works for conversation and
subagents with the cost shown as unknown; Forge refuses to compile on it
(`model_unpriced`) until a price is declared.

**Judge codes.** The hackathon judges get a code instead of a key, for a relay that holds
the Token Factory key and caps each code's spend (`deploy/relay`). `architect judge <code>`
checks the code at the relay, then stores it as the Nebius key with the relay as Nebius's
URL, so the first run, `/models` and every call go through the relay. Pasting the code
where the Nebius sign-in asks for a key does the same, in the terminal and in the desktop
app. The relay's address is `ARCHITECT_RELAY_URL`, else the one built into
`architect/providers/judge.py` (`architect judge --url` overrides both). A
`NEBIUS_API_KEY` in the environment is used before a stored key, so `architect judge`
refuses while one is set. It also refuses to replace a stored Nebius key that is not a
code (`/logout nebius` removes that key first). The code is never printed.

### Modes, permissions and safety

Shift+Tab (Alt+M) cycles **normal**, **build** and **plan**. `/mode build` switches straight
to build mode, and `/mode forge`, its old name, still works. In normal mode the conductor
answers or delegates: `code` (the Pi coder in the working directory), `spawn` (an
unlicensed preset subagent), `run_agent` (an installed agent), plus read-only tools of its
own (`read_file`, `list_dir`, `glob`, `grep`, `web_fetch`) and any MCP tools. It never
writes files itself.

| Permission | Coder writes and edits | Shell | MCP tools |
|---|---|---|---|
| `plan` | refused (the change is proposed) | refused | read-only tools only, asked |
| `ask` (default) | asked | asked | asked |
| `auto-edit` | allowed | asked | allowed |
| `full-auto` | allowed | allowed if the profile grants shell, else asked | allowed |

Approval answers are `y` (once), `s` (for the session) or `n`; Enter and a five-minute
silence are `n`. Each coder run is checkpointed: `/undo` restores the last turn's files and
`/diff` shows the changes (git diff in a repository).

### Build mode

Describe an agent; Architect asks a few questions (Enter accepts each default), the first
always being where it will live:

- **Run it here**: installed in the registry. Use it with `/run <agent> <input>` (plain text
  becomes the single text field), `uv run architect run <dir> --profile <p> --input <json>`,
  or `/serve` (a localhost HTTP service; an agent the CLI installed needs one `POST
  /compile` on the service before `/run` there, because the service runs only agents it
  licensed itself).
- **Put it into software**: after the license the package is staged, every file is listed,
  and nothing is written into the repository until you approve. The export is verified and
  the result shows a Python snippet, the HTTP calls and the test command. The package's
  feature flag stays off until a human review. `/export <agent> [path]` does this later.

The plan card shows the engineer (Forge, with the preset baseline to beat, or the tuned
preset alone), the floor and its data (drafted cases are SYNTHETIC; agents that read images
need real or generated page cases), the per-run and compile budgets, Forge's budget when
Forge runs, the model and the destination. `/floor`, `/budget` and `/model` change them
first. If the floor's cases break a pattern, a format or a length limit in a schema Stage 0
inferred, build mode drops that limit and a note on the plan card names it. A list of
values you did not give is dropped only for cases drafted in this build, never to make a
case set the profile already has fit.

If the plan names a case set the profile already has, that set is the exam when every one
of its cases fits the agent as it was inferred: each input is one the agent takes and each
answer fits what it gives back. A set that does not fit can still be the exam, with the
agent changed to fit it, only when it is a checked set (its profile entry has a manifest or
a checker of its own, as the expense app's 200 receipts have). The agent then takes the
input field its cases use, and an answer field whose answers do not fit takes the types
those answers have (a field written as "a number, or a string of digits" keeps both and
gains null); no other limit moves, and afterwards every case must fit. The answer
fields must have the same names. Where the set's checker reads a fixed list of fields and
ignores the rest (`receipt_v1`), the agent may give back more fields, and the note says
they are not examined. A name that is only close (`items_count` for `item_count`) is not
matched. In every other case the set is not this job's exam: the cases are drafted under
another name, the set is left as it is, and a note says which exam is used and why. A
shared answer name such as `label` is never the reason a set is kept. What the rule
cannot tell apart is a checked set for another job whose answers have the same field names
and break nothing the agent states; the note then says what marked the set and what the
agent took from it.

A drafted case is kept once, and not at all when the drafting model gave the same input two
different answers ("Yes" and "yes", or 1 and 1.0, are the same answer). Drafted cases are
an exam only from 6 up, 3 to practice on and 3 to be examined on, whether they came from a
valid reply or are the whole cases of one that was not a list; your own cases file needs 2.
When the plan names the synthesizer preset for an agent whose answer has no free text
(every field is a number, a boolean or one of a list of values), build mode uses the
extractor preset and a note says so: the synthesizer's prompt asks the model for prose,
and the cheaper models then answer with a paragraph where one JSON object is wanted.
With drafted cases and no `/floor` limit, a
floor limit that one miss would fail is lowered so that one miss still passes (0.80 with 6
held-out cases), and a note says so. Forge's steps stream while it works; the result shows
the harness design. `/forge <text>` runs one request in build mode with Forge on; `/forge arc [--run]`
builds an ARC-AGI-3 harness (a run asks first).

Forge is the default engineer, except when the model that would drive Forge is on Nebius
Token Factory, whether through a judge code or your own key. There a build ships the tuned
preset, since no Forge session driven by a Nemotron model has had a harness accepted.
`/forge <text>`, or Harness set to "Written for this job" under Edit plan in the app,
turns Forge on. A Nebius profile from the first run builds within $1.50 and 30
minutes and caps Forge at $1.20 and 45 minutes on top, so a build with Forge fits in one day
of a judge code.

### Everything else

| Command | Effect |
|---|---|
| `/agents`, `/report <agent>`, `/recompile <agent>`, `/library` | Installed agents, their test report and design |
| `/spawn <preset> <task>` | One ephemeral preset subagent |
| `/mcp`, `/mcp add <name> <command...>` or `--url <url>`, `/mcp tools`, `/mcp remove|enable|disable|restart` | MCP servers (also read from `.mcp.json` and `[mcp.servers.<name>]` in `config.toml`) |
| `/config [set key value]` | `model`, `provider`, `permission`, `session_budget_usd`, `session_minutes`, `auto_compact_percent`, `theme`, `editor`, `web_fetch_allow_private` |
| `/status`, `/cost`, `/context`, `/compact` | Session state, spend, context fill (auto-compact at 85%) |
| `/doctor` | Providers, Node and the Pi bridge, local servers, workspace, profile, disk |
| `/sessions`, `/resume [id]`, `/title`, `/clear`, `/export [file]`, `/copy` | Conversations |
| `/review [focus]`, `/diff`, `/undo`, `/init`, `/memory [edit]` | Working-tree review, checkpoints, `ARCHITECT.md` |
| `/image <path> [message]`, `@path` | Attach text or an image (images reach models that take them) |
| `/bench ...`, `/telemetry` | The bench verbs; cost and quality over time |

Input: Enter sends, Ctrl+J or a trailing `\` adds a line, Ctrl+R searches history, Ctrl+G
opens your editor, Tab completes commands, agents and paths, text typed during a turn is
queued, Esc or Ctrl+C interrupts the turn (finished work is kept), Ctrl+C twice or Ctrl+D
exits. `!cmd` runs a shell command yourself. Custom commands live in
`.architect/commands/*.md` (the body is the prompt; `$ARGUMENTS` is replaced).

Non-interactive: `architect -p "<task>" [--json]`, `-c` / `-r <id>` to continue, `--model`,
`--permission`, `--workspace`, `--plain`. `architect serve` runs the HTTP service.

## Profiles

`--profile` accepts JSON or TOML. Relative paths resolve from the profile file,
not the current directory, and a path that starts with `~` is in your home folder.
`"workspace": "~/.architect"` shares the workspace of the CLI and the desktop app.
`--workspace`, `ARCHITECT_WORKSPACE` or the app's workspace setting moves it in those
two, and an exported package's `run()` moves it with its `workspace` argument, else
`ARCHITECT_WORKSPACE`. An `architect.json` in the working directory is found without
`--profile`. The profile selects:

- `workspace`: artifacts, registry, library, telemetry and proposal storage.
  On Windows without the `LongPathsEnabled` policy, keep the registry root
  (`<workspace>/registry`) at or under about 123 characters: entries nest about 120
  characters below it and Windows refuses directories above 247. The registry
  refuses a longer root with `workspace_path_too_long` before any compile spends
  money. macOS, Linux and policy-enabled Windows have no such limit.
- `models`: model IDs mapped to `provider` (`fake`, `anthropic`, `openai`,
  `ollama`, `claude-code`, or `codex`), optional `base_url`, and optional
  `api_key_env` or `api_key_ref` (a stored key's provider, such as `nebius`). An entry
  with `api_key_ref` and no `base_url` uses the address stored with that key (a judge
  code's relay), else the provider's own. Only `fake` accepts a response `fixture`;
  only the CLI providers accept `executable`; do not put API keys directly in this file.
- `inference`, `decomposition`, `rewrite`: optional adapter settings with `model`.
- `forge_agent`: optional adapter settings (like `rewrite`) for the model that
  drives a [Forge](#forge) session; `compile.forge` holds the Forge settings.
- `floors`: dataset IDs mapped to `kind`, `path`, and optional `smoke` path.
- `calibrations`: judge calibration IDs mapped to JSON paths.
- `compile`, `orchestrator`, `pi`: typed component settings.
- `builder_floor` and optional `builder_model`: required floor and model selection
  for the compiled `create` builder. Use disposable checkouts for coding
  evaluation tasks, never the delivery repository or production data.

### Local coding-agent CLIs (subscription, no API key)

`claude-code` and `codex` run the locally logged-in `claude` or `codex` CLI once
per model call, in an empty temporary directory, with tools, settings, MCP and
session persistence off and the CLI's long coding-agent system prompt replaced
by a one-line one (about 6,600 input tokens down to about 425 per Claude Code
call). Log in with the CLI first. Exported `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`
(or `OPENAI_API_KEY`/`CODEX_API_KEY`) are removed from the child so the
subscription login is used; set `api_key_env` on a `claude-code` model to pass a
key deliberately. `codex` rejects `api_key_env`. Every model ID needs a price
entry; the bundled table covers `claude-haiku-4-5-20251001`, `claude-sonnet-5`,
`claude-opus-5-5`, `claude-fable-5-1` and `gpt-5.6-terra`.

```json
{
  "models": {
    "claude-sonnet-5": {"provider": "claude-code"},
    "claude-haiku-4-5-20251001": {"provider": "claude-code", "executable": "/usr/local/bin/claude"}
  }
}
```

```json
{"models": {"gpt-5.6-terra": {"provider": "codex"}}}
```

Reported costs are API-equivalent estimates, not what the subscription bills.
Live checks: `ARCHITECT_LIVE_TESTS=1 ARCHITECT_LIVE_CLAUDE_CODE=1` (or
`ARCHITECT_LIVE_CODEX=1`) `uv run pytest tests/live -q`.

### Prompt caching

`context.call(prompt, system=instructions)` gives the model exactly
`instructions + prompt`, the same text as one concatenated prompt; the
`system` part only marks a byte-stable prefix. The Anthropic adapter sends that
prefix as the first text block of the same user message with
`cache_control: {"type": "ephemeral"}` (prefixes under the model's minimum,
512-4096 tokens, are simply not cached). OpenAI-compatible endpoints cache long
shared prefixes on their own, Ollama reuses its KV cache, and the CLI adapters
pass the whole text on stdin unchanged. Cache writes and reads are priced from
the optional `cache_write_per_million` / `cache_read_per_million` price fields;
quotes always assume a cold cache. Existing `context.call(prompt)` code is
unaffected.

`Profile.model_json_schema()` in `architect.profiles` gives exact fields. Missing
models, datasets, calibration or smoke fixtures fail explicitly. A task-score
file cannot replace a game environment: current CLI profiles reject game floors
with `unsupported_game_environment`. Use the Python game integration below.

The checked-in synthetic profile
`examples/lectern/profiles/content-recommender.json` needs no API key. It has
explicit floor data and scripted Fake responses, but no inference adapter for
arbitrary requests. Use its checked-in explicit Spec, not a plain-language request.

## Interactive session

`uv run architect session` (before Phase 12, also bare `uv run architect`) starts a
line-oriented terminal session that takes one agent from a plain-language goal
to a verified artifact: propose a Spec, review and edit it, build a floor
dataset, approve by hash, watch the compile, install, run. The non-interactive
subcommands below are unchanged. Nothing the session prints is a license or an
approval: Stage 4 verification stays the only licensing authority, and
`licensed` is re-read from the artifact on disk every time it is shown.

Flags: `--workspace P` (default `$ARCHITECT_WORKSPACE`, else `.architect`),
`--profile P` (default: `./architect.json`, then `<workspace>/session-profile.json`),
`--script FILE` (one command per line; prompts and output still go to stdout) and
`--no-probe` (skip the first-run login probe). An explicit `--profile` wins even
when the file is missing, and that profile's own `workspace` setting decides where
artifacts, the registry and telemetry go, not `--workspace`. Unless
`ARCHITECT_SIGNING_KEY_FILE` is already set, the session pins it to
`<workspace>/signing.key`. `uv run architect session --help` prints the flags and
the READY help text.

### First run without a profile

When no profile is found the session prints `Nothing printed by this session is
a license or an approval. Costs are API-equivalent estimates, not a bill.`, looks
for `claude` and `codex` on PATH (`shutil.which` only; nothing is executed), and
shows a model menu with each model's minimum reservation per call and whether it
can infer Specs under the fixed $1.00 Stage 0 budget. Only priced ids are offered
(`claude-haiku-4-5-20251001`, `claude-sonnet-5`, `claude-opus-5-5`,
`claude-fable-5-1`, `gpt-5.6-terra`); with exactly one CLI and a `--script` or
piped stdin the default row is taken without a `model>` prompt. Choosing a row
prints a four-line summary of the profile that will be written (`model <id> via
<provider> (<executable>)`, `inference model <id|none>`, `compile budget $5.00 /
30m  candidates 2  rewriting off`, `full profile will be written to
<workspace>/session-profile.json`), one note on what that budget buys, and asks
`confirm> write <path>? [Y/n]`. The JSON itself is not printed: the file has
`"workspace": "."` and one model in the ladder, and `profile` shows it later. Then,
unless `--no-probe`, one tiny metered call (`Reply with the single word ok.`,
reservation shown, typically under $0.01, up to 90 s) checks that the CLI is
logged in. A not-logged-in or unavailable CLI writes nothing and exits 1; with
no CLI at all the session prints `No profile and no logged-in CLI found. Log in
with 'claude' or 'codex' once, or write architect.json (see docs/usage.md), then
run 'architect' again.` and exits 1. Later starts find the written profile and
open at `architect>`. Edit the file by hand to add models, floors or budgets;
`use model <id>` and `budget compile <usd> <s>` rewrite it from inside the
session (session-written profiles only; others get `profile_not_editable`).

### States and prompts

| Prompt | State | What you are doing |
|---|---|---|
| `architect> ` | READY | describing a goal, loading a Spec, managing the registry |
| `review <hash8>> ` | REVIEW | reading and editing the Spec; nothing compiled yet |
| `floor <dataset>> ` | FLOOR | building a held-out floor dataset for the Spec |
| `compiling <hash8>> ` | COMPILING | watching Stages 1-4 as content-free progress lines (one per stage change, failed call, fifth completed call or 20 s); `cancel` works |
| `result <hash8>> ` | RESULT | reading the verification report; installing or running |
| `bench> ` | BENCHING | watching a bench run as content-free progress lines; `cancel` stops it and keeps the rows so far |

`hash8` is the first eight characters of the Spec hash. Setup uses `model>` and
`confirm>`; a failed inference asks one `clarify>` question and retries once.

### Commands per state

Global in every state: `help [verb]`, `status`, `spend`, `profile`, `quit`,
`exit` (`profile` is withheld while inferring, compiling or benching). `help` never
spends money.

| State | Verbs |
|---|---|
| READY | `describe <text>`, `spec <file\|{json}>`, `load <file>`, `list`, `info <name>`, `run <name\|dir> {json}`, `rollback <name> [rev]`, `recompile <name>`, `floors`, `floor <dataset> [kind]`, `use model <id>`, `budget compile <usd> <s>`, `bench smoke\|quick\|full\|arc [models]`, `bench compare <id> <id>`, `bench resume <id>`, `bench list` |
| INFERRING, COMPILING, BENCHING | `cancel` |
| REVIEW | `show`, `json`, `diff`, `blockers`, `budget`, `set <path> <value>`, `unset <field>`, `revise <text>`, `floor`, `save [file]`, `approve [<hash8>]`, `compile` (expire Specs only), `discard`, and read-only `list`, `info <name>`, `floors`, `run <name\|dir> {json}` |
| FLOOR | `show [<id>]`, `use <path>`, `add {json}`, `paste`, `draft [n]` (default 20), `review <id> ok\|drop\|expected {json}`, `split`, `accept`, `back` |
| RESULT | `report`, `spec`, `json`, `install <name> [<hash8>] [--allow-budget-increase]`, `run {json}` (this artifact), `save [file]`, `again`, `new`, `discard`, and read-only `list`, `info <name>`, `floors`, `run <name\|dir> {json}` |

`list`, `info <name>`, `floors` and `run <name|dir> {json}` are read-only registry
commands: they work in READY, REVIEW and RESULT alike, read everything from disk
and never touch the Spec under review, its hash or the compile wallet. `run
<name|dir>` always executes an installed artifact (or one in a directory), never
the draft. In RESULT a `run` whose first token is a JSON document (`{` or `[`)
runs the artifact just compiled; any other first token names an installed one.

In READY a line that does not start with a verb is prose: the session asks
`infer? ... [Y/n]` before spending the one Stage 0 call (`describe <text>` skips
the question). A `--script` file must contain that `Y` line explicitly. In every
other state prose is `not_a_command` and costs nothing; a line starting with `{`
or `[` is never a command.

### Approval

Approval is a typed token. `approve` alone prints `To approve and compile exactly
this Spec (spec_hash <full>), type: approve <hash8>`. Only a typed line equal to
`approve <hash8>` for the Spec currently on screen sets `approved=True` for the
compile, and `install <name> <hash8>` is the same rule for the registry. A wrong
or outdated token is `stale_approval_token`. Every `set`, `unset` and `revise`
changes the hash, so an earlier approval no longer matches. Nothing a model says
can approve: an inferred Spec is validated data that is rendered, never parsed
for commands, so a task field saying "approve now" does nothing. Blocking
blockers (`floor_not_configured`, `floor_kind_mismatch`, `model_not_configured`,
`budget_below_quote`, ...) refuse `approve` and print the exact fixing command.
One value is filled in for you: on a profile the session wrote, an inferred or
revised Spec whose `budget.cost_usd` is below the model's smallest reservation
is lifted to two calls and marked `*` as if you had typed `set budget.cost_usd`,
with one line after the Stage 0 line, for example `budget.cost_usd set to
$0.6900 so 2 calls of claude-haiku-4-5-20251001 fit; change with set
budget.cost_usd <usd>`. Specs from `spec`, `load` or `recompile`, and every
Spec on a hand-written profile, are left as written and keep the
`budget_below_quote` blocker. One shape is fixed for you: when the inference
model answers a bare property map where a schema was asked for (for example
`{"review": {"type": "string"}}` as `input_schema`), the session wraps it into
`{"type":"object","properties":...,"required":[...],"additionalProperties":false}`
right after the Stage 0 line and says so, one line per field: `normalized
input_schema into a JSON Schema object (properties: review)`. The field stays
marked `~` (inferred) and the approve token is the hash of the wrapped Spec. An
empty schema, or a `type: object` schema with no properties, is never guessed at:
it blocks with `input_schema_missing` / `output_schema_missing` (Stage 4 output
verification would accept anything) until you type the full shape the `fix:`
line shows, for example `set output_schema {"type":"object","properties":{"label":
{"type":"string"}},"required":["label"],"additionalProperties":false}`. Specs
from `spec`, `load` or `recompile` are shown exactly as written. `expire` Specs
need no approval and start with `compile`.

### Floor datasets

Stage 4 needs a configured `held_out_tasks` file, and an inferred Spec usually
names a dataset that does not exist yet. The session never invents one silently:
`floor_not_configured` blocks approval, and `floor` opens FLOOR for that dataset.
The session never binds the profile's only floor to an inferred Spec: a goal
about `tickets-v1` keeps `tickets-v1` even when only `sentiment-v1` is
configured, and the blocker's `fix: floor` line is followed by `or: set
floor.dataset sentiment-v1` (several floors: `set floor.dataset <a|b>`) so you
choose the dataset the harness is verified on.
The screen offers, in this order, `use <path>` (an operator-owned file), `add
{json}` / `paste` (cases you write) and last `draft [n]` (one metered call
proposing `n` model-written cases, 20 when you give no number). Every `draft`
prints this notice:

> Drafted cases were written by the same model that will be tuned on them. Their
> 'expected' values measure agreement with that model, not correctness. Read each
> case and fix or drop the expected value yourself; the floor is only as honest as
> the labels you accept. Every drafted case is stamped SYNTHETIC and is never
> counted as production evidence.

The FLOOR screen lists the cases you are building, unreviewed drafted ones
first, each with its id, a flag (`[op]`, `[file]`, `[draft ?]`, `[draft ok]`,
`[draft fixed]`), its task and its expected value as compact JSON clipped to the
line; after 12 cases the rest are counted, and `show <id>` prints one case in
full. The screen ends with a `Next:` line naming the next command. Drafted cases
are reviewed one at a time (`review <id> ok`, `review <id> drop`, `review <id>
expected {json}`); each answer is one line (`reviewed <id> ok  task: ...`,
`reviewed <id> expected -> ...` or `dropped <id>`) followed by `unreviewed: N` and
either `next: review <id> ...` or `ready: type accept`, never the whole screen
again. There is no bulk review, and `accept` is refused while any drafted case
is unreviewed. `split` previews the tuning / held-out
partition (sorted ids, even positions tune, odd are held out). `accept` writes
`<workspace>/floors/<dataset>.json` with `metadata.synthetic`, `origin`,
`drafted_by_model`, `reviewed` and `corrected` on every case, registers it in the
profile and rebuilds services; the Spec hash is unchanged. Afterwards `floors`,
`info`, `status` and the result screen show counts and the `[SYNTHETIC: ...]`
label only; a configured floor's cases are never printed (only the draft you are
building is), and `draft` prompts never include cases from a configured floor. Below 20 cases the screen says how many held-out
misses the limit tolerates and that small floors license lucky harnesses. A
license earned on a SYNTHETIC-labeled floor proves agreement with the labels you
accepted, nothing more. `llm_judge`, `human_edit_rate` and game floors are refused
in the session (`floor_kind_unsupported_in_session`); configure them in the
profile by hand.

### Costs, scripted transcripts, codex-only setups

Every screen ends with `spend  session $... | this compile $... | calls N | wall
... | API-equivalent, not a bill`. Costs are estimates at API list prices, not
what a subscription bills. Tokens appear as measured usage; the session adds no
token cap. Reservations (minimum per call, compile reservation on the `budget`
screen) are upper bounds; measured live cost is typically about 1% of them.

While a compile runs, a progress line (`[mm:ss] stage N ...  calls C (R running)
spent $X  left $Y / T`) is printed when the stage changes, when a call fails
(`calls 14 (0 running, 1 failed)`), after every fifth completed call and when
20 s passed since the last line, so a 36-call compile prints about ten lines
instead of two per call; `status` shows the latest snapshot at any time, and the
`compile finished in ...: N calls, $X, stages 1,2,4` line always closes it.

`--script FILE` replays one command per line and echoes prompts and output, which
gives reproducible transcripts. The excerpt below comes from such a run against
the checked-in synthetic `content-recommender` profile (Fake adapter, no API key,
`--no-probe`); the schema listing and most progress lines are trimmed:

```
architect> spec examples/lectern/specs/content-recommender.json
Spec dcd2d76a  lifecycle install (approval required)  origin file
  task           ~ Recommend synthetic fraction practice content from candidates and preferences;
    require human review.
  ...
  floor          ~ held_out_tasks  dataset synthetic-content-acceptance  limit >= 1.00
                   configured: yes, 12 cases -> 6 tuning / 6 held-out (0 misses allowed)
                   [SYNTHETIC: 12 of 12 cases not operator-authored, accepted by operator; not
                     production evidence]
  budget         ~ $0.10 per run, 5s
  model_target   ~ fake
spec_hash dcd2d76ad9bb92e93f73bab022b7f3af36305250fa5a05353d2b1a0f481e6ae9
Next: approve dcd2d76a | set <field> <value> | revise <text> | json | budget | floor | discard
spend  session $0.0000 | this compile - | calls 0 | wall 0m00s | API-equivalent, not a bill
review dcd2d76a> approve
To approve and compile exactly this Spec (spec_hash
  dcd2d76ad9bb92e93f73bab022b7f3af36305250fa5a05353d2b1a0f481e6ae9), type: approve dcd2d76a
review dcd2d76a> approve dcd2d76a
approved dcd2d76a - compiling. Type cancel or press Ctrl-C to stop.
compiling dcd2d76a  model fake  compile budget $1.00 / 30.0s
approved by operator  (cancel or Ctrl-C to stop)
[00:00] stage 2 tuning        calls 18 (0 running)  spent $0.0022  left $0.9978 / 0m29s  rows:
  quality 1, accepted 13, rewrite 0
[00:00] compile finished in 0.4s: 24 calls, $0.0029, stages 1,2,4
Verification (stage 4)  id bd3649f3  stages 1,2,4  licensed: YES (recomputed from disk)
  floor      held_out_tasks  score 1.00 >= limit 1.00   held-out cases 6
  ...
Next: install <name> | run {json} | report | again | new
result dcd2d76a> install demo
To install exactly this licensed artifact as "demo", type: install demo dcd2d76a
result dcd2d76a> install demo dcd2d76a
installed demo revision 1 -> C:\...\.architect\lectern-content-fixture\registry\versions\...\1
result dcd2d76a> run {"synthetic": true, "query": "fractions", "candidates": [...], "preferences": {"format": "practice"}}
run 9e921b4a  fake  $0.0001  0.0s  80 in / 40 out
{
  "content_id": "fractions-practice",
  "review_required": true
}
spend  session $0.0030 | this compile $0.0029 | calls 25 | wall 0m00s | API-equivalent, not a bill
```

To reproduce it, put those commands in a file and run
`uv run architect session --profile examples/lectern/profiles/content-recommender.json --script commands.txt --no-probe`
(the `run` line takes the JSON from `examples/lectern/fixtures/content-recommender-input.json`).

With only `codex` on PATH, `gpt-5.6-terra` cannot infer Specs under the fixed
$1.00 Stage 0 budget and there is no fallback model, so the menu row says `can
infer Specs: no (describe unavailable; use spec <file>)` and the profile is
written without an `inference` entry. `describe`, `revise` and bare prose then
fail with `no_inference_model`; load an explicit Spec with `spec <file>` instead.
When a `claude` CLI is also present, Stage 0 falls back to
`claude-haiku-4-5-20251001` and the chosen model stays the compile target.

The transcript above is fixture-only evidence. Live runs against logged-in CLIs
are recorded in [PROGRESS.md](../PROGRESS.md), not claimed here.

## Compile, approve, run

```sh
uv run architect compile examples/lectern/specs/content-recommender.json --profile examples/lectern/profiles/content-recommender.json
```

This installed Spec returns `approval_required: true`, `accepted: false`, and a
`proposal_path`; it does not license an artifact. Review the returned file. Copy
it to `reviewed.json` only after review, then run:

```sh
uv run architect compile reviewed.json --profile examples/lectern/profiles/content-recommender.json --approved-spec reviewed.json
uv run architect registry --workspace .architect/lectern-content-fixture
uv run architect status --workspace .architect/lectern-content-fixture
```

`--approved-spec` is the approval flag; there is no bare `--approved` on the main
CLI. It loads that exact file instead of re-inferring or merging the positional
request. Use the artifact directory emitted by a successful compile for `run`.
`--input` takes JSON text, **not** an input filename:

```sh
uv run architect run ARTIFACT_DIRECTORY --profile profile.json --input '{"value":1}'
uv run architect status REGISTRY_NAME --workspace .architect
uv run architect recompile REGISTRY_NAME --profile profile.json
```

The last examples need matching operator-owned files, schemas and registry names.
`recompile` reuses the incumbent's approved Spec. Ties or regressions cannot
replace it. A recompile is always a fresh Stage 4 attempt: with routing on it may
warm-start from a template, but it never reuses the installed entry, not even under
`routing.reuse_origins` (the same holds for the session's `recompile <name>`).
`expire` artifacts allow only one delivery attempt, even on failure.
Keep installed delivery behind an application feature flag and human review;
local CLI `run` is a trusted operator action, not a deployment workflow.

`compile` exits 0 for a review checkpoint, but that is not acceptance. Other
refusals return a nonzero exit and a fixed JSON error on stderr. Read `accepted`,
`approval_required`, `failures`, and the authenticated report rather than trusting
process success alone. CLI help is available with `uv run architect --help`.

## Do versus create

```sh
uv run architect do "add a CLI that prints the registry" --profile profile.json --repo .
uv run architect create "add an AI feature" --profile profile.json --repo .
uv run architect create "add an AI feature" --profile profile.json --repo . --approved-spec reviewed.json
```

- `do` asks the orchestrator to decompose the goal, compile ephemeral subtask
  agents, execute them and collect results. Repository changes still need review.
- `create` compiles a coding builder with a host-scoped `compile_agent` tool. The
  builder can request one bounded child compilation for the exact feature
  request. It cannot grant its own approval, widen budgets or replace the compiler
  with generated untested code.
- The first `create` can return a proposal in its `feature_factory` task output.
  That checkpoint is not an accepted feature; the CLI exits nonzero. Review its
  `proposal_path`, then rerun with `--approved-spec`. Missing `builder_floor`
  returns `builder_floor_required`.
- Licensed installation is not production deployment readiness. Generated
  integration must keep delivery disabled until separate application review.

## Pi and host trust

`npm ci --prefix architect/backends/pi-node` installs genuine upstream
`@mariozechner/pi-ai`, `pi-agent-core`, and `pi-coding-agent`, each pinned to
**0.73.1**. The bridge runs Pi's SDK loop and tools; Architect does not reimplement
that loop. Scripted Fake responses can drive the real loop in offline tests.

`pi.allowed_tools` defaults to `read`, `write`, `edit`. File tools enforce the
repository boundary and deny profile, fixture and private control-plane paths.
To permit shell execution, a trusted operator must both include `bash` in the
allowed tools and set `pi.allow_shell: true`. The pack must also request that tool.
This runs **trusted checkout/host code**. It is not an OS or network sandbox and
can access resources available to that host process. Do not grant it to untrusted
repositories. Hostile concurrent filesystem mutation is outside the boundary.

Set `ARCHITECT_SIGNING_KEY_FILE` to a stable absolute private path for processes
started from different working directories. Keep it outside coding repositories.
Use external OS/container isolation if your threat model requires it.

## Forge

Forge is an opt-in harness-engineering session in the Stage 3 slot. A coding
agent on the pinned Pi SDK writes `harness.py` in **sandbox language v2** (see
the generated `LANGUAGE.md` card, which also ends the agent's prompt: helpers
with defaults, fuel-bounded loops, arithmetic, f-strings, a narrow `try/except`,
pure builtins including ARC grid helpers and their method spellings such as
`s.split(sep)`, list, dict and set comprehensions that run as the loops they stand
for, type annotations that are accepted and ignored, per-turn `observe`/`step` for
games, licensed sub-harness calls, and `images=`/`system=` on `context.call`). The agent works in a throwaway workspace and reaches trusted
code only through five tools:

| Tool | Does | Cap (default) | Money |
|---|---|---|---|
| `forge_check` | validates the draft; a refusal lists every refused line with its rule and the allowed form | 200 | $0 |
| `forge_dryrun` | runs one tuning case on scripted, zero-priced replies; nothing scored | 100 | $0 |
| `forge_measure` | measures the draft with the play model on some tuning cases | 24 | play-model spend |
| `forge_submit` | lint, bind children, measure on all tuning cases (plus smoke) | 6 | play-model spend |
| `forge_spawn` | compiles and licenses a child sub-harness (Stages 1, 2, 4) | 3, depth 1 | child compile |

`mode: "template"` runs no agent: it submits the instantiated default template
(`game_explorer` for games, `vision_two_pass` for image inputs, `extract_verify`
otherwise) once and then promotes and verifies it. It is the agent-free
comparison arm.

**When to use it.** When the tuned preset misses its floor, or when the task
needs state kept in code rather than in the prompt (ARC-style games are the
motivating case). With the default `trigger: "failed"` Forge runs only when the
Stage 2 best misses its floor or budget on tuning; `always` forces it. A
promoted Forge harness is only kept if it ranks better on tuning than the
Stage 2 best (floor, budget, smoke, then score, cost, size), and Stage 4 then
verifies it once on held-out cases exactly as for any other candidate.

**Requirements.** The floor needs a `smoke` path in the profile's `floors`
entry; without a smoke floor Forge would silently not run, so it is refused with
`forge_disabled`. Pi dependencies must be installed
(`npm ci --prefix architect/backends/pi-node`).

**Flags.** `compile`, `do`, `create` and `recompile` accept these; they override
`profile.compile.forge` for this invocation only:

```sh
--forge                      # enable
--forge-usd X                # Forge dollar cap (default 5)
--forge-minutes M            # Forge time cap (default 60)
--forge-model NAME           # agent model: the forge_agent model or a models key
--forge-trigger always|failed
--forge-mode agent|template
--yes                        # consent to the summed cap without a prompt
```

```sh
uv run architect compile reviewed.json --profile profile.json --approved-spec reviewed.json \
    --forge --forge-trigger always --forge-usd 5 --forge-minutes 60
```

**Profile.** `forge_agent` is optional adapter settings, like `rewrite`.
`compile.forge` holds the settings (`enabled`, `mode`, `trigger`,
`agent_model`, `template`, `feedback`, `max_usd`, `max_s` and the per-tool caps).
The agent is `compile.forge.agent_model` when set, which must be the
`forge_agent` model or a `models` key (otherwise `forge_model_unknown`). If no
agent model is set, Forge uses `forge_agent`, then `rewrite`, then the ladder
model. `examples/local/profile-forge.json` is the ARC experiment profile: a
Claude CLI agent and a local `granite4.2:8b` play model.

**Costs, caps and consent.** Budgets are dollars and seconds. Tokens are
metered, never capped. With Forge enabled, the outer compile wallet is the
compile budget plus the Forge budget. Forge's own nested wallet caps the
session. When that wallet runs out, the session ends normally with the best
accepted submission so far. Before any spend, the CLI prints

```
forge: compile cap $A + forge cap $B = $C (API-equivalent, not a bill); forge minutes M; verifications for this spec so far: K/N
```

and asks `Continue? [y/N]`. `--yes` is the only non-interactive consent. A
non-interactive stdin without `--yes` gives `forge_cancelled` with nothing spent.
A session that promotes a candidate records one Stage 4 verification for that
Spec. Once a Spec reaches `max_verifications_per_spec` (default 10), Forge does
not run for it again (`forge_verification_cap`). This bounds adaptive
overfitting to the held-out set across repeated compiles.
Build-time agent cost is reported separately from play-time cost. CLI-provider
dollars are API-equivalent.

**Session verb.** In the interactive session:

```
forge on | forge off | forge status | forge budget <usd> <minutes> | forge model <name> | forge mode agent|template
```

It edits only this session's compile settings. It never touches the Spec (the
hash is unchanged) or the profile file. `forge on` shows the summed-cap line
and asks for confirmation. While compiling, the progress shows
`forge  checks C  dryruns D  measures M/Mx  submits S/Sx  spawns P  $spent/$cap (API-equivalent)`.
It never shows source, prompts or case content.

**Report.** `uv run architect forge report <artifact-dir> [--json]` prints the
promoted version's `forge.json` as an ASCII table, or the raw JSON with `--json`.
A missing file gives `forge_report_missing`. `forge.json` sits next to the promoted
artifact (`<workspace>/artifacts/<spec_hash>/<n>/forge.json`). It is content-free:
language, mode, agent and play model keys, tool counts, accepted count, agent
cost, tokens and wall time, harness node and byte counts, `stage4_calls`,
`verifications_for_spec`, note sizes and digests, and per child its name,
`artifact_digest`, `case_origin` and `licensed`. It never holds source, prompts,
cases, replies or note text.

**Honesty rules.**

- Only the parent's Stage 4 on held-out cases licenses a harness. Tuning
  measurements, dry runs and child verifications are evidence, never a license
  or a held-out score; `forge report` prints `tuning evidence, not a license`.
- The agent sees `SPEC.md`, tuning inputs and, in the default `per_case` feedback
  mode, truncated tuning expected values; `feedback: "aggregate"` hides them. It
  never sees a held-out case, id, count, score, report or dataset path, and
  never sees the parent's Stage 4 outcome.
- `forge_submit` rejects string literals of 24 or more characters that copy a
  tuning input or expected value (`forge_literal_leak`).
- A child license means "meets its own contract on unseen cases drawn from parent
  tuning or agent-authored data". It is weaker than a parent license, and it is
  always shown with its `case_origin`.
- A bench row where Forge did not run is marked `forge_did_not_run`, never
  reported as a Forge result.

## Routing and Forge delivery modes

Phase 10 (`docs/interfaces/phase10-router.md`) adds a deterministic router over the
verified versions that already exist. Routing orders licensed versions; it never licenses,
and every surface that shows a route also says that Stage 4 decides.

**Profile.** Two optional tables. Without a `routing` table a compile never routes unless
you grant reuse for that call, so the default behaves exactly as before:

```json
{
  "routing": {"reuse_origins": [], "min_template_reliability": 0.3, "explore_every": 0,
              "max_candidates": 8, "child_warm_start": false, "subtask_models": false},
  "roles": {"policy": "pinned"}
}
```

`"routing": {"enabled": false}` switches routing off everywhere. `roles.policy` is `pinned`
(today's adapters, byte-identical) or `evidence` (a role switches to a declared variant only
on non-inferiority evidence; `route_model_unknown` and `route_param_invalid` guard the
variants).

**Actions.** `reuse_installed` (only when granted: `route --run --yes`, `do
--reuse-installed`, session `reuse <name> <hash8> {json}`, `forge run|integrate
--reuse-installed`, or `routing.reuse_origins`), `warm_start` (a library template of the
same input/output shape seeds Stage 1; Stage 2 and Stage 4 still run), `compile`, and
`refuse` (no configured model; the normal compile path still runs). A route that suggests
Forge (`route_forge_suggested`) never arms it.

**CLI.**

```bash
architect route <request_or_spec> --profile P [--origin cli|session|orchestrator] [--json]
architect route <spec.json> --profile P --run INPUT_JSON --yes   # runs only a granted reuse
architect compile|recompile|create ... --no-route                 # A/B: no router
architect do <goal> --profile P --repo R [--reuse-installed] [--no-route]
architect roles --profile P [--json]
architect bench run --provider fake --tier smoke --variant routed [--rounds R]  # $0
architect bench roles --policy pinned|evidence [--json]            # replay fake role logs
```

`route` on prose runs Stage 0 first and prints `route: stage 0 cost $X (API-equivalent)`
on stderr; a Spec file costs $0. `route --run` without `--yes` is `route_no_reuse` and runs
nothing, even when the profile's `routing.reuse_origins` lists `cli`. The table has fixed columns (at most 100 ASCII columns): tier, candidate, model,
fit `F`, conservative quality margin `m_lcb`, template reliability `r`, cost, latency (`m`
measured or `b` bound), components and evidence labels, followed by the decision line and
any proposals (`proposal: architect recompile ...`, `proposal: rollback <name> <rev>`),
which never take effect by themselves. Below the table each code prints its fixed sentence,
and an actionable code (`route_reuse_not_granted`, `route_forge_suggested`) adds a `next:`
command; long lines wrap, never truncate.

`roles` prints the policy and, per role variant, `n`, `p_lcb`, mean `$`, median wall, the
evidence class and `proxy_completed` (telemetry `accepted`: the run completed, never that it
was correct), plus judge rows marked `report only`; `roles --json` carries the same values.
When a variant's class is `synthetic_fake`, both add `evidence synthetic_fake: plumbing, not
quality`.

**Forge delivery modes.**

```bash
architect forge run <request_or_spec> --profile P --input JSON|@FILE [--cases FILE] \
  [--reuse-installed] [--forge-usd N --forge-minutes N --forge-model M --forge-mode M] [--yes]
architect forge integrate <request> --profile P --repo R --into DIR [--approved-spec F] \
  [--package NAME] [--stage-only | --package-only] [--reuse-installed] [--yes]
architect export verify <package-dir> [--profile P] [--workspace W] [--reverify] [--json]
```

`forge run` prints `forge agent: <model> (mode M, turns N)` and the Phase 9 cap line, asks
`Continue? [y/N]` (or `--yes`), compiles with Forge in the Stage 3 slot, delivers once only
if Stage 4 licensed the harness, removes the generated code and keeps `report.json`,
`forge.json` and `subharnesses/` as evidence. Its stderr summary keeps build (agent),
compile and play cost separate. `--cases FILE` (JSONL `{case_id, task, expected}`, at least
8 rows) binds a user case set into the spec hash.

`forge integrate` runs four gates in order: approval (without `--approved-spec` it runs Stage
0 and writes the proposal, as `compile` does), Forge spend consent, the Stage 4 license
(installed as `feature-<hash16>`), and the export consent
`export: <package> (<n> files, <kb> KB) -> <into>/<package>; feature flag OFF; human review
required`. Then exactly one placement runs: `--stage-only` (nothing in the repository),
`--package-only` (one new directory, host code) or the default FeatureBuilder/Pi path with
the single-use `place_harness_package` tool. Every placement is re-hashed against the
manifest the operator consented to; a mismatch is reported (`export_integrity_mismatch`),
never deleted. After the build it prints `forge integrate: licensed and installed as
<name>@<rev>; build $A agent (API-equivalent), compile $B, play $0.0000 (nothing is run)`,
and the JSON result carries `build_cost_usd` and `compile_cost_usd`.

`export verify` re-hashes the package, compares its pins, children and license (with the
signing key, or with a trusted Ed25519 key when the package is signed; see
[Signed results](#signed-results)), prints the content-free `EVIDENCE.json` table and exits 0
only when all pass. Only configured keys count: `--workspace W` or the `ARCHITECT_*`
variables, never `./.architect` on its own.
`--reverify` re-runs Stage 4 of the exported Spec on this host's floor (paid, asked first,
bounded by `max_verifications_per_spec`) without writing the package. It never executes code
shipped in the package: the package must pass its integrity, pin and children checks first,
and Stage 4 runs a private copy of this host's own installed entry with the same digests. A
package this host never installed is `export_unverified`; on another host, mint a license
with `architect compile --approved-spec <package>/artifact/spec.json`.

**Session.** READY `roles`; REVIEW `route`, `reuse <name> <hash8> {json}`, `forge run {json}`
(lifecycle expire only) and `forge integrate <repo> <into>`; RESULT `export`; and
`rate good|bad` in READY, REVIEW and RESULT (it rates the last `run`, `reuse` or `forge run`
delivery). With a `routing` table the REVIEW screen ends with one line such as
`route: warm start from 3fa2b1c9d0e1 (still verified)`, plus a `next:` command when an exact
installed match was not granted or Forge is suggested. Session compiles route with the
`session` origin, so only `"reuse_origins": ["session"]` grants installed reuse there; when
it does, the REVIEW line says so before anything runs (`next: approve <hash8> delivers
<name@rev> as installed: no Stage 1 to 4 and no compile spend`, with the Stage 4 and
plumbing lines), and `approve` prints `routing to installed <name@rev>`. A goal that starts
with a Phase 10 verb (`route support tickets ...`, `export contacts as csv`) is still prose
in READY; only `roles`, `rate`, `rate good` and `rate bad` are READY commands. After a `forge run`
or `forge integrate` the footer's `this compile` is that command's spend.
`forge integrate` needs `approve <hash8>` first; `approve` runs a normal paid compile, so the
flow is `approve <hash8>`, `again`, `forge integrate <repo> <into>` (a second, Forge build).

**HTTP.** `POST /route` with `{"spec": Spec}` returns content-free advice; see
[HTTP and TypeScript](#http-and-typescript).

## Signed results

```bash
architect verify <report.json | artifact-dir | package-dir | published-dir> [--workspace W] [--require-key FINGERPRINT] [--json]
architect verify --published receipts
architect keys generate [--name N] [--pub FILE] [--workspace W]
architect keys list [--json] [--workspace W]
architect keys trust <file.pub> --name N [--workspace W]
architect keys sign <published-dir> [--name N] [--workspace W]
```

A host that holds an Ed25519 key (`ed25519.key` next to `signing.key`, made by
`architect keys generate`) signs every Stage 4 report and export manifest with it as well
as with the HMAC key. Any host whose trust list holds the public key can then check them
offline, and an exported package runs there without the signing host's HMAC key. The trust
list defaults to the key shipped in the package; `keys trust` adds keys for one workspace.
`verify` prints `verified` or `refused: <code>` and a few plain lines, and exits 0 only when
verified. It never runs package code. It uses the keys `--workspace` or the `ARCHITECT_*`
variables name, never `./.architect` on its own; with neither, only the shipped key counts.
`--require-key` accepts only that key's Ed25519 signature, so CI can refuse a package
stripped back to the HMAC. See [docs/trust.md](trust.md) for the trust model and for making
the release key on the bench host.

## Retake

```bash
architect retake <agent-folder> --profile P [--practice] [--yes] [--json]
architect undo <agent-folder> [--profile P | --workspace W] [--json]
```

An agent added to a project (`/export`, Add to project) only runs if it passed its exam.
When a file in its folder changes, every call through the package is refused before a
model is called. The `ExportError` it raises still has the check's code as its `str()`.
Its `sentence` is "Stopped: changed after its exam. 0 model calls.", its `reason_line`
names the files that changed (`changed_files`), and `reason` is `changed` when only the
agent's editable files changed, `changed_undo_only` for any other file and `cannot_check`
when nothing changed but no key here can check the report. A fourth reason, `reloaded`,
is for a program that imported the package before the folder was written again (a retake
that passed, or an undo): no file is wrong, the sentence says the app loaded an earlier
version, and the fix is to restart it. `architect verify <agent-folder>` prints the same
two lines under `refused:`. For a package that stands it prints the model, the score, the
pass mark and the attempt, and for a retake which cases it ran on.

`reason` only says which files changed. Whether the change can be retaken depends on what
changed inside them, and on the exam. An app that wants to tell its user what to do asks
`architect.session.retake.standing(folder, profile)`. It reads the folder and the profile
file, calls no model, and returns the same lines the app in Architect shows, with one
`step`: "Retake its exam or undo the change in Architect.", "Fix the file, or undo the
change in Architect." or "Undo the change in Architect." The example app does this.

`retake` lets the changed agent take its exam again. The rule is the first exam's: it
passes if it reaches the pass mark, with invalid answers within the allowance and every
run within its budget. The version that passed is not run again and no earlier score
decides anything. A pass writes the folder again in place with its new signed report, and
the package keeps its `FLAG`, so the app's environment needs no edit. A program that
imported the package before has to import it again or restart. Anything else leaves the
folder as it was, still stopped. `retake` exits 0 when it passed, 1 when it did not and 2
when it was refused with no attempt used. It shows the attempt, its cases, an estimate
and the cost cap (the profile's `compile_budget`) and asks before spending; `--yes`
answers. Stopping it with Ctrl+C still uses the attempt and its cases. The estimate is the
last exam's mean cost per run times the cases, scaled by the two models' input prices when
the change names another model.

A retake is refused, with no attempt used, when:

- the change touches a fixed field or a file other than the two a retake reads (`undo_only`);
- one of the two files has a mistake (`edit_invalid`). The line names it: where the JSON
  stops reading, the field the plan is missing, or the setting and what it may be
  ("retries is 2. It can be 0 or 1.");
- the model is not in the profile, or the plan names one model and the change another;
- the exam takes no retakes, or its cases are not the ones the agent passed on
  (`exam_changed`). Each report from an exam that allows retakes signs a digest of the
  exam's cases, and a retake compares it;
- another retake of the same agent is running (`busy`).

A pass writes the folder only while it still holds the change that was examined. If the
folder was undone or edited again while the exam ran, or a program has it open and it
cannot be replaced, the folder is left alone and the line says so. The attempt is listed
as passed with its folder not written, and running `retake` again on that same change
writes the folder without a new attempt.

`--practice` scores the change on the exam's practice cases. It pays for its model calls,
is not an attempt and uses no exam case.

A retake can examine a change to `task` and `execution_config` in `artifact/spec.json`
and to `model` and `params` in `artifact/config.json`, for an agent built from a preset.
`harness.py` is written again from those two files and an edited one is never executed.
The pass mark, the exam, the schemas, the budget and the tools are fixed. A change to
anything else can only be undone.

`undo` puts back the last version that passed, byte for byte, from the copy Add to project
and every passed retake keep in `<workspace>/passed/<package>` (checked before use). It
exits 0 when the folder was restored and 2 when there was nothing to restore.

Two profile keys on a `held_out_tasks` floor:

```json
"floors": {"my-exam": {"kind": "held_out_tasks", "path": "cases.json",
                       "retake_cases": "more-cases.json", "retakes": true}}
```

`retake_cases` names extra exam cases that only retakes read. A retake takes the first
ones, in a fixed order, that no earlier attempt of this agent used. When fewer than a full
exam are left, or the floor names none, the retake is the exam of attempt 1 again and
every line says "same exam". `retakes` says whether the exam may be retaken at all. Left
out, it may not: no exam is retaken unless its entry says `"retakes": true`, so no changed
design takes an evidence exam by accident, whether or not the entry still names the
dataset's `manifest`. Architect writes `"retakes": true` for the exams it drafts.

Attempts are numbered in `<workspace>/retakes/<agent>.jsonl`, where `<agent>` is the end
of the package's flag in lower case, so the same agent added under two names shares one
record. The file is append-only, one line when an attempt starts and one when it ends.
The number and the kind of cases are signed into each report. If the record cannot account
for every earlier attempt (a line that does not read, or a missing file after a retake
already passed), the next retake does not claim unused cases: it is the same exam and says
why. See [docs/limitations.md](limitations.md) for what this does not do.

After a coder run, the reply to the conversation carries `notes` the host checked itself:
that no file changed, that a changed `.json` file no longer parses, and that a changed
file belongs to an agent that is now stopped.

## HTTP and TypeScript

The HTTP server is a trusted backend control plane, not a public browser API.
Set `ARCHITECT_API_TOKEN` to a strong random ASCII token (32 to 512 non-whitespace
characters), then start:

```sh
uv run python -m architect.server --profile profile.json
```

Defaults: `127.0.0.1:8765`. Options include `--host`, `--port`, `--token-env` and
`--allow-non-loopback`. Non-loopback binding needs explicit permission; that flag
does not add TLS, tenant isolation or public-service hardening. Every request
requires `Authorization: Bearer TOKEN`. Authentication occurs before body reads.
JSON bodies are limited to 1 MiB. Extra fields and malformed authority data fail
closed. There are exactly five routes; automatic docs/OpenAPI routes are disabled.

| Method and path | Body / result |
| --- | --- |
| `POST /compile` | Exactly one of `{request}` or `{spec, approved?}`; returns acceptance, proposal Spec, opaque `artifact_id`, failures and stages. |
| `POST /run` | `{artifact_id, input, enabled?, human_reviewed?}`; returns output and usage. Both flags must be explicitly true for installed delivery. |
| `GET /status/{artifact_id}` | Returns current license, expiry, lifecycle, spec hash and version. |
| `POST /recompile` | `{artifact_id}` for an installed artifact; uses the same approved Spec and strict registry replacement. |
| `POST /route` | `{spec}` only (prose is `route_spec_required`); $0 advice: `{action, artifact_id, fit_tier, fit, quality_lcb, expected_cost_usd, latency_s, latency_basis, evidence, codes}`. No names, paths or text; nothing runs, reuse is never granted. |

Approval requires the exact reviewed `spec` and `approved: true`; a plain request
cannot carry it. IDs are opaque catalog references, not filesystem paths or
licenses. The server re-authenticates disk artifacts. A compile refusal can be
HTTP 200 with `accepted: false`; do not treat HTTP success as a quality license.
Missing enablement returns 403, consumed ephemeral artifacts 410, and dollar/time
overruns 422. Errors use fixed codes without provider exception text. When the
server's catalog, the registry or the telemetry log stays write-locked for more
than a few seconds (for example by a long CLI install), the request answers 503
`storage_busy`; retry it. A `/run` whose mandatory telemetry cannot be written
never returns output. It is refused before an ephemeral artifact is consumed when
the log is already locked; if the lock appears mid-run, the artifact is spent.
Retrying an approval of the exact Spec that is already installed
returns `accepted: false`, `failures: ["not_better"]` and the existing
`artifact_id`, so a client that lost the first response can recover the ID. This
includes an install that committed while its catalog write hit `storage_busy`.
An installation made outside the server (for example by the CLI) is never given
an ID this way, even for the same Spec.

The local private package `@architect/client` supports only the same five methods
(`route(spec)` is advice only).
It is not claimed to be published to npm. Import its built module from this
checkout, or wire the local package in your trusted backend:

```ts
import { ArchitectClient } from './client-ts/dist/index.js';
const client = new ArchitectClient('http://127.0.0.1:8765', operatorToken);
const proposal = await client.compile({ request: 'your configured task' });
// Stop here. Review proposal.spec outside the model before approving it.
const compiled = await client.compile({ spec: reviewedSpec, approved: true });
if (!compiled.accepted || !compiled.artifact_id) throw new Error('compile rejected');
const status = await client.status(compiled.artifact_id);
// Only after separate operator enablement and human delivery review:
const run = await client.run({ artifact_id: compiled.artifact_id, input,
                               enabled: true, human_reviewed: true });
const replacement = await client.recompile({ artifact_id: compiled.artifact_id });
```

`operatorToken`, `reviewedSpec` and `input` are caller-owned values, not automatic
approval helpers. The client validates DTOs, uses native fetch, and does not retry
or follow redirects. `ArchitectError` exposes fixed codes and optional HTTP status,
not bodies or credentials. Never ship its bearer token to an untrusted browser.
See [client-ts/README.md](../client-ts/README.md) for the explicit fixture-only
live local-server `npm run e2e` procedure. Normal `npm test` uses a short-lived
loopback server with mocked responses; that is different evidence.

## Games

The Python API is separate from the five HTTP routes and CLI profile loader.
`LocalGameProvider` in `architect.games.local` supplies three synthetic families:
navigation, switches and sequence. Its default `LocalFixtureManifest` contains
15 actual instances, one per family in each of five roles: `parent_tuning`,
`parent_held_out`, `support_tuning`, `support_held_out`, and `delivery`. Fingerprints
bind implemented puzzle data, not arbitrary seed/revision labels. This is not an
ARC benchmark or proof of general reasoning.

Official optional dependencies install with `uv sync --frozen --extra arc`:
SDK `arc-agi==0.9.9`, engine `arcengine==0.9.3`. The concrete integration is:

- `OfficialConfig(mode="offline", staged_game_dir=Path(...))` and
  `OfficialGameProvider(config)` from `architect.games.official`.
- `OfficialPreviewManifest` from `architect.games.evaluation`: required
  `dataset_id`, `dataset_revision`, `support_tuning`, `support_held_out`, and
  `delivery`. Optional `parent_tuning`/`parent_held_out` must be nonempty for
  competition-parent evaluation. Both tuning/held-out pairs have equal sizes.
- Each partition contains `GameRef` values from `architect.games.types`, with
  `backend="official_offline"`, a full versioned SDK `game_id`, and `revision`
  equal to the suffix after its first hyphen. Placeholder revisions such as
  `latest` are rejected. Base game IDs must be globally unique across all roles;
  revision aliases do not make independent games. At most 1024 total refs.
- `GameFactory(compiler, provider, config, manifest=manifest)` from
  `architect.games.factory`. `GameFactoryConfig` requires `budget`,
  `compile_config`, and `small_model`. Optional `limits`, `strategy` and
  `floor_limit` control rollout behavior. Strategies are `explorer`, `planner`
  and `hypothesis_tester`. Set `enable_rewriting=False`, `max_rewrites=0`, and
  `model_ladder=(small_model,)` in the child `CompileConfig`.
- `await factory.run(target, support_dataset="your-support-id")` compiles and
  plays. Nested parent evaluation uses `run_isolated` so target observations do
  not enter child compilation. Compile, probe, tune and play share dollar/time
  limits. Fake or explicit loopback model bindings are permitted; this binding
  check is not proof that the host has no network access. To test with a cloud
  model on public previews, set `GameFactoryConfig(allow_online_model=True)`
  (host-side only; `scripts/arc_harness.py --adapter claude-code|anthropic` does
  this). It needs an OFFLINE provider and a preview or local fixture manifest,
  is refused in gateway mode (`game_online_model_gateway_forbidden`), and labels
  results `model_binding="online_preview"`.

An official manifest is explicit; there is no automatic local fallback. Discovery
must match pinned manifest references before probe/compile. The staged public
files must exist and match the intended revisions. Close official providers with
`await provider.close()`. No live official game files ship in this repository.

`normalize_official_score` and `OFFICIAL_SCORE_METRIC` live in
`architect.games.official`. The versioned policy is `official_percent_v1`, metric
`relative_human_action_efficiency.percent.v1`, pinned to SDK/engine above. Only
finite non-boolean numbers in `0..100` convert to `raw / 100`. Completed-card game
scores come from the exact environment entry; WIN and action counts do not
replace that score. The pure conversion is not proof a caller's number is official.

Gateway mode owns a complete submission scorecard. It is **refused for factory
candidate evaluation**, even before discovery; it is not reusable preview data.
An OFFLINE provider instead opens independent episodes for public development.
The implementation and scripted transport tests do not establish live official
preview quality, an official benchmark score or competition compliance.

### Installed local competition example

`CompetitionFactory` in `architect.games.competition` compiles an installed
planner from `CompetitionDescription`, then delegates freshly verified game
agents. It exposes `compile`, `install`, `run`, `recompile`, `monitor`, `rollback`.
Review uses `compile(description, approved=True, approved_spec=reviewed_spec)`;
a boolean alone cannot approve an unseen spec. Registry acceptance remains the
same gate as other installed agents. `compliance` stays `unverified`.

The standalone local example uses different approval flags from the main CLI:

```sh
uv run python examples/arc/competition.py propose --workspace .architect/arc-demo
# Review .architect/arc-demo/proposed-spec.json before continuing.
uv run python examples/arc/competition.py compile --workspace .architect/arc-demo --approved --approved-spec .architect/arc-demo/proposed-spec.json
uv run python examples/arc/competition.py install --workspace .architect/arc-demo
uv run python examples/arc/competition.py run --workspace .architect/arc-demo
```

Default `--adapter fixture` is scripted and synthetic. `--adapter ollama` selects
an operator-configured local model binding; it is not a claim of tested model
quality. Numeric Kaggle caps and submission tooling/compliance remain unconfirmed.

### Local Phase 6 exit harness

```sh
uv run python scripts/phase6_exit.py --workspace .architect/p6-proposal
# Fresh workspace, explicit synthetic-fixture authorization:
uv run python scripts/phase6_exit.py --workspace .architect/p6-fixtures --approve-fixtures
```

Without the flag, the script only proposes a spec; exit 0 does not mean accepted
execution. The opt-in exercises local game families and installed competition
lifecycle checks. `summary.json` is metadata-only; proposed specs, artifacts and
telemetry remain separate. Costs/drift are synthetic, and compliance remains
unverified. Script output is local diagnostic evidence, not final Phase 6
acceptance counts or an official benchmark.

## Bench

`architect bench` measures presets on a frozen, versioned suite through the real
compiler (Stages 1, 2 and 4) and writes one content-free row per (task, model,
variant, repeat) cell. The free mechanism check needs no profile:

```bash
uv run architect bench run --provider fake --tier smoke --seed 7
uv run architect bench run --tier quick --profile examples/live/claude-code.json --label base
uv run architect bench compare <base-id> <candidate-id>
uv run architect bench run --resume <bench-id>   # continue a stopped run; done cells are kept
uv run architect bench list
uv run architect bench probe --profile P
uv run architect bench pages --seed 7 --count 2 --out DIR
# Phase 9 arms: preset vs agent-free template vs Forge, then a paired compare
uv run architect bench run --provider fake --tier smoke --seed 7 --variant default --variant template --variant forge
uv run architect bench compare <id> <id> --pair-variants default:forge
```

In the session, `bench smoke` runs the same smoke tier for free; `bench quick
[model,model]`, `bench full [model,model]` and `bench arc [model]` print a bound and
ask `Continue? [y/N]` before any spend. Local Ollama models are first-class (rows
labeled `local_zero_cost`, $0, measured in wall time). Scores are synthetic by
construction and never a license: Stage 4 stays the only licensing authority and
the bench never installs into your registry (`bench arc` installs its manufactured
harness only into a throwaway registry inside the cell workspace, deleted with
it). `scripts/arc_harness.py` is a thin CLI over
`architect.games.public`, which `bench arc` shares. See
[docs/bench.md](bench.md) for tiers, local models, the iteration protocol and the
honesty rules.

## Synthetic consumer examples

```sh
uv run python examples/lectern/run.py --workspace .architect/lectern-proposals
# Explicit fixture-only approval; not production approval:
uv run python examples/lectern/run.py --workspace .architect/lectern-fixture-run --approve-fixtures
```

Use a fresh workspace each time. These exercise report replay, content acceptance
and the extractor/independent-solver/verifier digitizer pipeline. Fixtures use
Fake adapters and synthetic text/keys, not live uploaded-image vision. They do
not prove live model quality, one-month edit-rate improvement or production
cost savings. Production data, consent, operational review and feature enablement
remain external requirements.
