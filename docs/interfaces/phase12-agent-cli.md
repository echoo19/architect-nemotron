# Phase 12: Architect as a terminal agent that builds agents

Status: spec (2026-09-30). Where this text and the code differ after implementation, the
code, `docs/usage.md` and `README.md` are authoritative.

## 1. Direction (the user's words, reconciled)

"It should just be a harness specialized towards making harnesses but can also just
function as a normal one. In NORMAL tasks it just produces specialized subagents from
preset harnesses for different tasks. However it is also just naturally better at MAKING
other harnesses/agents/agents in software. I can simply say 'can you make an agent test
extraction pipeline' (like the one in Lectern) and it will work on it using its preset-based
subagents and just be good at making it. Likewise if I want a specific harness for a
specific task like ARC-AGI-3 it will make it the same way and I will be able to use it. The
interface should be a CLI extremely similar to [today's coding agents], with unique slash
commands fitting what Architect is." Later: "maybe we can switch to agent-building mode and
there's an interview process."

Accepted amendment (from a review the user forwarded): **quality floors and budgets are
inferred with sensible defaults and shown, never required as input.** The floor and the
budget are what make Architect a compiler rather than a generator ("agents that are tested
before they are allowed to run"). Architect never edits the user's files itself: in normal
mode it hands the task to the right preset subagent (the Pi coder for code) and gets out of
the way.

## 2. Shape: two layers, one product

- **Core (unchanged):** Stage 0-4 compiler, floors, budgets (dollars and seconds; tokens
  metered, never capped), registry, library, router, Forge, telemetry. Stage 4 alone
  licenses. Nothing in this phase weakens a gate.
- **CLI (new, `architect/agent/`):** `uv run architect` opens a conversational terminal
  agent. The old step-by-step session stays available as `architect session` (its tests and
  scripts keep working), and every existing subcommand is unchanged.

## 3. The conductor (the agent you talk to)

A Python agent loop, `Conductor`, owns the conversation. It is an orchestrator, not a coder:
its own tools read and search the working directory and call Architect's core; every file
change is made by a subagent.

- **Protocol.** The models available on a typical host were text-only (the logged-in `claude`
  and `codex` CLIs, local Ollama), so the conductor uses the same JSON envelope as the Pi
  parent (Phase 11) whenever its adapter cannot stream native tool calls: each turn the
  model replies with exactly one object, `{"tool_calls": [...]}` or `{"text": "..."}`.
  Parsing reuses `_control_reply` (fenced or prose-wrapped envelopes are normalized; a
  missed tool call gets at most two repairs). The prompt is a stable prefix (system
  instructions, tool list, project instructions) passed as `cache_prefix`, then the
  transcript and a closing reminder. Since Phase 13 an adapter that streams native chats
  (the Anthropic, OpenAI-compatible and Ollama HTTP adapters) drives the conductor with
  native tool calls instead (`architect/agent/native.py`), and since W3 (2026-10-04) Pi
  does the same: the coder, Forge's engineer and Pi in Stage 2, Stage 4 and the run tool
  use native calls whenever their model streams, and keep the envelope otherwise
  (Phase 11, Section 4). The session meter wraps every adapter it hands out, and a wrapped
  streaming adapter streams too (`ObservedChat`), so this choice never depends on where
  the adapter came from.
- **Transcript.** Messages (user, assistant, tool call, tool result) are kept in Python and
  rendered into the prompt. Long tool results are clipped; when the rendered transcript would
  exceed the model's context the oldest tool results are summarized to one line
  (`/compact` does it on demand).
- **Budget.** Every conductor and subagent call runs through `RunContext` with a session
  wallet: default $5 and 60 minutes per session, shown in the footer, changed with
  `/budget session <usd> <minutes>`. A tool call that would exceed it is refused with a code.
- **Project instructions.** `ARCHITECT.md`, else `AGENTS.md`, else `CLAUDE.md` in the working
  directory (first found, at most 16 KiB), plus a global `<workspace>/ARCHITECT.md`, are part
  of the stable prefix. `/init` drafts an
  `ARCHITECT.md` through the coder subagent (with approval).

### 3.1 Conductor tools

| Tool | What it does | Writes files? |
|---|---|---|
| `read_file(path, offset?, limit?)` | Read a text file under the working directory | no |
| `list_dir(path?)`, `glob(pattern)`, `grep(pattern, path?)` | Look around the working directory (bounded output) | no |
| `spawn(preset, task, input?)` | Run one preset subagent now, ephemeral: `Preset.build` + `Runtime.evaluate` with a control Spec. Returns its output. No license; labelled `unlicensed` | only `coder` |
| `code(task)` | The Pi coder subagent in the working directory, with the session's permission mode | yes (with approval) |
| `make(description, answers?)` | Build mode (Section 5): infer the Spec, draft the floor, compile, install | installs into the registry |
| `run_agent(name, input)` | Run an installed, licensed agent (`Runtime.run`) | no |
| `forge(target, mode)` | Forge a harness for an environment or competition (`forge run`/`integrate`) | integrate writes into the repo only through the guarded export path |
| `agents()`, `report(name)` | Registry listing with drift; a report's license fields | no |

Tools that spend money state a bound first; in `ask` mode the user confirms any single call
bound above $0.50 (configurable).

### 3.2 Normal mode

The conductor answers questions itself when no work is needed, and otherwise delegates:
code changes to `code`, structured jobs to `spawn` with the matching preset (`extractor`,
`synthesizer`, `planner`, `verifier`, `explorer`, `recommender`, `hypothesis_tester`), and
repeated jobs to an installed agent (`run_agent`) when the router finds a licensed one. It
suggests build mode when the user is asking for an agent, harness or pipeline.

## 4. Subagents

- **Ephemeral preset subagents** (`spawn`) are cheap and unlicensed; their rows say so.
- **The coder** is the Pi backend (read, write, edit, bash) confined to the working directory
  with the control-plane paths denied. Its tool requests are approved per the permission mode
  (Section 7). The bridge streams tool events to the CLI (Section 8).
- **Licensed agents** are whatever build mode installs; they run through the registry with the
  license re-read from disk before every run (unchanged).

## 5. Build mode: interview, then compile

Entered with Shift+Tab (mode cycle), `/mode build`, or for one request with `/make <text>`.
The conductor also proposes it when a request is clearly an agent or harness to build.

1. **Interview.** At most five short questions, only for what cannot be inferred from the
   request, the working directory and `ARCHITECT.md`. Each shows a default; Enter accepts it.
   The questions come from a fixed set: inputs (and an example), outputs (fields), what counts
   as correct, where it should live (installed agent or exported into this repo), and limits
   (cost per run, time per run, models). `/skip` accepts every remaining default.
2. **Plan card.** Stage 0 infers the Spec from the request plus answers. The CLI shows one
   card: task, preset (or pipeline stages), input and output fields, **floor** (kind, limit,
   dataset and its origin), **budget** (per run and for the compile), **model** ("cheapest
   that passes" over the profile ladder), and where it will be installed. Enter compiles;
   `/floor`, `/budget`, `/model` edit it first.
3. **Floor without user data.** If the user has no labelled cases, the inference model drafts
   them (existing `draft_cases`), labelled `SYNTHETIC`, split into tuning and held-out cases
   by the existing partition rule. The card says so. `/floor add <file>` swaps in real cases.
4. **Compile.** Stages 1-4 with a live progress line (stage, calls, spend). Forge takes the
   Stage 3 slot when the preset cannot pass the floor or when the user asked for it.
5. **Result.** Licensed: installed as `<name> v<n>` and runnable (`/run <name>`, `architect
   run`, HTTP). Not licensed: the report's failure codes and the one next step that would most
   likely help (raise the budget, try a larger model, add real cases). Export into a repo goes
   through `forge integrate` (guarded, human-reviewed).

The Lectern-style request ("make an agent test extraction pipeline like the one in Lectern")
is the acceptance example: the interview settles inputs (page images), outputs (question
fields), correctness (field accuracy against an answer key) and location; the plan card shows
a multi-stage pipeline (vision extraction, solver, verifier) with an inferred floor and a hard
budget, which Lectern itself lacks.

## 5a. Built agents are engineered, not re-specced presets

Direction from the user: "built agents/harnesses aren't like differently specced subagents
like what a harness does. They're truly specialized and unique in an innovative way ...
just very good at their job." So build mode uses **Forge by default**: the best preset from
Stages 1-2 is only the baseline, and Forge (the Pi-based harness engineer) writes a harness
for this one task that must beat it on the tuning cases before Stage 4 decides the license.
Forge's workspace carries `DESIGN.md`, a playbook of what "specialized" means in practice:
code before model, decomposition into focused calls, task-specific checks with repair,
agreement between independent passes, cheap-first cascades, licensed sub-harnesses for
sub-skills, rules learned from measured failures (never copied answers), and state-in-code
exploration for games. The harness states its design in its docstring; `/report` shows it.
`/budget` and the plan card show Forge's own budget; `engineer=preset` skips Forge for a
quick, cheaper build.

## 6. Slash commands

| Command | Effect |
|---|---|
| `/help [command]` | Commands, keys and modes |
| `/mode [normal|build]` | Show or switch mode (Shift+Tab cycles) |
| `/make <description>` | Build mode for one request |
| `/spawn <preset> <task>` | Run one ephemeral preset subagent now |
| `/forge <target>` | Forge a harness for an environment or competition (e.g. `arc`) |
| `/agents` | Installed agents, versions, license and drift |
| `/run <agent> [input]` | Run an installed agent |
| `/report <agent>` | Its test report and license |
| `/recompile <agent>` | Force a rebuild |
| `/library` | Cached harnesses and reuse counts |
| `/floor`, `/budget`, `/model` | Show or override the defaults for the next compile (and `/model` for the conductor) |
| `/telemetry` | Cost and quality over time |
| `/permissions [ask|auto-edit|full-auto]` | Approval mode for the coder |
| `/status`, `/cost` | Session state, model, workspace, spend against the session budget |
| `/compact`, `/clear` | Summarize old tool results; start a fresh conversation |
| `/resume [id]`, `/sessions` | Continue a saved conversation |
| `/init` | Draft `ARCHITECT.md` for this directory |
| `/bench ...` | The existing bench verbs |
| `/exit` | Quit (Ctrl-D too) |

Also: `!<command>` runs a shell command yourself (output shown, not sent unless you ask),
`@path` attaches a file's text to the message, Esc interrupts the running turn, Ctrl-C
clears the line or interrupts, Ctrl-D exits.

## 7. Permissions

`ask` (default): every coder write, edit and shell command is shown and approved (`y`, `n`,
`a` = allow this kind for the session). `auto-edit`: file writes inside the working directory
are allowed, shell asks. `full-auto`: everything inside the working directory is allowed.
Control-plane paths (`.architect/`, signing keys, the Architect package) stay denied in every
mode. Money consent (Section 3.1) is separate from file permissions.

## 8. Terminal UI

Researched against today's coding agents (sources in the Phase 12 run note). Decisions:

- **Stack:** `prompt_toolkit` (input) and `rich` (output), printing inline into the normal
  scrollback. No full-screen TUI (Textual inline mode does not work on Windows).
- **Layout:** a short banner (version, model, working directory, workspace, mode, tips); the
  prompt `> `; a bottom toolbar: mode, permission mode, model, session spend against budget,
  running subagents. The toolbar degrades by width.
- **Input:** Enter sends; Ctrl+J (works on every Windows terminal) or a trailing `\` inserts
  a newline; history in `<workspace>/history` with Up and Ctrl+R; Tab completes slash commands,
  agent names and `@paths`; Shift+Tab cycles normal/build (Alt+M fallback); large pastes are
  collapsed to `[pasted N lines]`.
- **During a turn** the prompt is not active (v1). Output is printed block by block with rich:
  assistant text as markdown, tool calls as `* tool(args)` with an indented one-line result,
  coder edits as a short diff, a spinner line with elapsed time and spend. Esc or Ctrl+C
  cancels the turn (a raw key listener runs during the turn); completed work stays in the
  transcript. Ctrl+C at an empty prompt twice exits; Ctrl+D exits.
- **Approvals:** inline `Allow <tool> <summary>? [y]es / [s]ession / [n]o` (Enter = no);
  answers are never guessed; a prompt left 5 minutes is a no.
- **Fallbacks:** when stdin or stdout is not a real console (pipes, CI, Git Bash/mintty without
  winpty), the CLI uses the existing line console and plain text; `NoConsoleScreenBufferError`
  is caught, never shown. stdout is reconfigured to UTF-8 with replacement, and glyphs have
  ASCII fallbacks. Never emit the ESC [K erase-line sequence under prompt_toolkit.
- **Non-interactive:** `architect -p "<task>"` runs one turn and prints the final answer
  (`--json` for a JSON object with answer, spend and tool calls). `-c/--continue` and
  `-r/--resume <id>` reopen a saved conversation.
- **Pi events:** the bridge gains a one-way event message (`{"kind": "event", ...}`: tool
  start, tool end with a short summary) so coder activity renders live. Events are
  informational; approvals keep using the existing request path.

## 9. Sessions and storage

- Workspace default for the CLI: `~/.architect` (agents are reusable across projects);
  `--workspace` and `ARCHITECT_WORKSPACE` override. The first run bootstraps the profile from
  the logged-in CLIs (existing `detect_clis`, `probe_login`, `write_profile`).
- Conversations are saved to `<workspace>/sessions/<id>.jsonl` (messages and tool results,
  clipped), resumable with `/resume` or `architect --continue`. Telemetry stays content-free.

## 10. Work packages

1. **Conductor core** (`architect/agent/conductor.py`, `transcript.py`, `tools.py`): the loop,
   envelope protocol, transcript rendering and compaction, read-only tools, `spawn`, `code`,
   `run_agent`, `agents`, `report`, session wallet. Offline tests with scripted adapters.
2. **Build mode** (`architect/agent/build.py`): interview state machine, plan card, floor
   drafting, compile with progress, install, failure next-step. Offline tests.
3. **Terminal UI** (`architect/agent/repl.py`, `commands.py`, `render.py`): prompt_toolkit and
   rich front end, slash commands, modes, permissions prompts, fallback console, `-p` mode,
   sessions. Tests drive it through the scripted console.
4. **Pi event stream and approvals** (`bridge.mjs`, `pi.py`): events and interactive
   approvals for the coder.
5. **Live acceptance** (after offline green): a real session on the claude CLI building a
   small extraction agent end to end; the Lectern-style request through the interview to a
   plan card and compile; an ARC harness via `/forge arc` (local model only under the GPU
   rule).

## 11. Acceptance

- `uv run architect` on a machine with a logged-in `claude` CLI opens the REPL with no setup
  questions beyond model choice; typing a task works; `/help` lists the commands above.
- Normal mode: "add a --verbose flag to scripts/x.py" goes to the coder, shows each edit for
  approval, and ends with a summary. Architect's own tools never write files.
- Build mode: "make an agent that extracts invoice fields from text" finishes the interview in
  at most five answers (Enter accepts defaults), shows a plan card with an inferred floor and
  budget, compiles, installs a licensed agent, and `/run` uses it.
- Every gate from Phases 1-11 still holds; the existing suite stays green.
