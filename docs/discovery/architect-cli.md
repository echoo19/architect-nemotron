# Architect operator CLI: a precise map for a desktop app

Research date: 2026-10-01. Repo: `{repo}` at `b5f8f02` (branch `master`).
Read-only research; nothing in the repo was modified. All citations are `path:line` relative to
the repo root. Where a phase doc and the code disagree, **the code is authoritative** (the docs
say so themselves: `docs/interfaces/phase12-agent-cli.md:3`, `phase13-cli-complete.md:3`).

---

## 0. TL;DR for the bridge

- The core and the terminal are already separated by a small protocol. The `Conductor` never
  prints. It emits frozen dataclass **events** to a `UI` and **asks** the UI for answers. The
  UI has three methods: `emit(event)`, `await ask(question, default=, choices=)`,
  `await approve(action, detail) -> "yes"|"session"|"no"` (`architect/agent/events.py:113-125`).
  There are 9 event types (`events.py:34-110`).
- **The REPL loop (`Repl`) is front-end-agnostic too.** It drives a `Frontend` protocol: the
  `UI` methods plus `renderer` (with `.text/.rows/.diff/.show`), `attach(repl)`,
  `await read_line()` and `await watch(task)` (`architect/agent/repl.py:364-375`). Every slash
  command, `@path` attachment, `!shell`, checkpoint, auto-compact and session save lives in
  `Repl` and `commands*.py`, not in the terminal code. Two frontends exist (prompt_toolkit+rich
  `RichFrontend`, plain `LineFrontend`). A third one, a JSON-lines `BridgeFrontend`, is the
  narrowest bridge: `boot()` + `Repl(app, BridgeFrontend).run()`.
- There is **no** chat/agent HTTP API. `architect/server.py` is a FastAPI control plane with
  exactly 5 routes (`/compile`, `/run`, `/status/{id}`, `/recompile`, `/route`), bearer token,
  loopback (`server.py:1-16`, `1280-1315`). `/serve` runs it as a child process.
  `client-ts/` is a typed fetch client for those 5 routes only, not for the agent.
- Approvals reach the UI from 3 places: the coder (Pi) for `write`/`edit`/`bash`
  (`agent/coder.py:165-179`), MCP tools as `mcp <server>.<tool>` (`mcp/manager.py:339-358`),
  and `export` before writing an agent package into the repo (`agent/export.py:417`). Many other
  confirmations are `ask()` y/n questions: plan card, money consent, /undo, ARC run, etc.
- Gotcha for a stdio bridge: some paths **inherit the process's stdout/stdin**. `codex login`
  runs in the foreground with inherited stdio (`providers/host.py:23-32`, `signin.py:161`),
  `/bench` calls `cli.main` which prints to stdout (`commands.py:862`), and `/memory edit` and
  Ctrl+G run an editor (`commands.py:1238-1240`). A JSON-lines bridge has to move its protocol
  off fd 1 (dup it and point fd 1 at stderr), or use a socket or pipe.

---

## 1. Entry points, commands, flags, modes, config, keys

### 1.1 Process entry

| Invocation | What runs | Cite |
|---|---|---|
| `architect` (console script) | `architect.cli:main` | `pyproject.toml:21-22` |
| `python -m architect` | same `cli.main` | `architect/__main__.py:1-5` |
| `architect` with no args, or first arg starting with `-` (except `-h/--help`) | **agent CLI**: `architect.agent.repl.main(args)` | `architect/cli.py:1265-1270` |
| `architect serve ...` | HTTP service `architect.server.main` | `cli.py:1257-1261` |
| `architect session ...` | old step-by-step session (`architect.session.main`) | `cli.py:1262-1264` |
| `architect <subcommand>` | the classic compiler CLI (below) | `cli.py:1271-1314` |

### 1.2 Agent CLI flags (`architect [flags]`), `repl.py:1397-1417`

| Flag | Meaning |
|---|---|
| `-p, --print TASK` | one non-interactive turn, prints the answer; `-` reads the task from stdin (`repl.py:1533-1541`) |
| `--json` | with `-p`: prints `{"answer","cost_usd","tool_calls","session"}` (`repl.py:1375-1382`) |
| `-c, --continue` | continue the most recent conversation |
| `-r, --resume ID` | resume a saved conversation (id or unique prefix; `sessions.py:281-292`) |
| `--model NAME` | conductor model (must be a profile model, or a CLI model it can add: `app.py:110-136`, `app.py:354-358`) |
| `--workspace PATH` | workspace (default `~/.architect`) |
| `--profile PATH` | profile file (default discovered, §3.4) |
| `--permission {ask,auto-edit,full-auto,plan}` | coder approval mode |
| `--no-probe` | skip the first-run login probe |
| `--plain` | line UI, no prompt_toolkit (also `theme = "plain"` in config: `repl.py:1542`) |

Argument errors raise `BootError("invalid_arguments")` (`repl.py:1392-1394`), printed to stderr as
`{"error": code}` with exit 1 (`repl.py:1550-1552`). Ctrl+C gives exit 130 (`repl.py:1553-1555`).
Any other exception is printed as a fixed code only (`repl.py:1556-1561`).

`-p` mode (`repl.py:1333-1386`) uses `PrintUI(RecordingUI)`. Questions take their defaults.
Approvals are fixed: `"yes"` only when `--permission full-auto`, otherwise `"no"`
(`repl.py:1359`). Non-info notices go to stderr.

### 1.3 Classic subcommands (`cli.py:63-160`, `315-376`)

`compile <request_or_spec>`, `do <goal> --repo`, `create <request> --repo`,
`run <artifact> --input`, `recompile <name>`. All take `--profile` (required). compile/create
take `--approved-spec`, do takes `--reuse-installed`, and all but run take `--no-route` plus
the Forge flags `--forge --forge-usd --forge-minutes --forge-model --forge-trigger {always,failed}
--forge-mode {agent,template} --yes`.

The rest:
- `route <request_or_spec> --profile [--origin cli|session|orchestrator] [--run INPUT] [--yes] [--json]`
- `roles --profile [--json]`
- `export verify <package> [--profile] [--reverify] [--yes] [--json]`
- `status --workspace [name]`, `registry --workspace`
- `bench run|roles|compare|list|pages|probe ...` (`cli.py:315-376`)
- `forge report <artifact> [--json]`, `forge run <request_or_spec> --profile --input [--cases]
  [--reuse-installed] [--json]`, `forge integrate <request> --profile --repo --into
  [--approved-spec] [--package] [--stage-only|--package-only] [--reuse-installed] [--json]` plus
  the Forge mode flags (`cli.py:113-150`)
- `serve --profile [--host 127.0.0.1] [--port 8765] [--token-env ARCHITECT_API_TOKEN]
  [--allow-non-loopback]` (`server.py:1349-1360`)

### 1.4 Modes

There are two orthogonal state variables. The UI's "mode cycle" mixes them.

| State | Values | Where |
|---|---|---|
| `Conductor.mode` | `"normal"`, `"build"` (shown as **forge**) | `conductor.py:40`, `render.py:123-127` |
| `ToolContext.permission` (mirrored to `ConductorSettings.permission`) | `"ask"` (default), `"auto-edit"`, `"full-auto"`, `"plan"` | `tools.py:48`, `conductor.py:109` |

- **Shift+Tab / Alt+M** cycles `normal -> forge -> plan -> normal` (`repl.py:113`, `181-198`).
  Forge sets `mode="build"`. Plan sets `mode="normal"` and permission `plan`, and stores the
  previous permission in `extras["plan_previous"]`. Leaving plan restores it (`repl.py:195-197`).
- `set_permission` adds a transcript **note** when you enter or leave plan (`PLAN_NOTE`,
  `PLAN_OFF_NOTE`, `repl.py:116-120`, `157-172`). It also **clears the session allowances**
  (`context.allowed.clear()`, `repl.py:172`).
- **Build (forge) mode effect:** the envelope prompt appends `BUILD_MODE`, which tells the model
  to call `make` (`conductor.py:68-71`, `182-183`). The native system prompt does the same
  (`native.py:160-166`).
- **Plan mode effect:** the `code` tool raises `ToolError("plan_mode_read_only")`
  (`tools.py:481-488`). MCP tools run only if read-only, and are approved (`mcp/manager.py:343-346`).
- The label "plan" is computed as permission `plan` with mode not `build` (`render.py:123-127`).
- `/mode [normal|forge]` sets `Conductor.mode` directly (`commands.py:319-333`). `/make <text>`
  and `/forge <text>` run one turn with `mode="build"` (`commands.py:336-341`,
  `commands_forge.py:131-138`). `/forge` also sets `extras["force_forge"]`.

### 1.5 Permission semantics (exactly what is gated)

| Permission | Coder write/edit | Coder bash | MCP tool | Cite |
|---|---|---|---|---|
| `ask` | asks | asks | asks (unless allowed for session) | `coder.py:81-87`, `mcp/manager.py:347-358` |
| `auto-edit` | auto | asks | auto | `coder.py:83-84`, `manager.py:347` |
| `full-auto` | auto | auto **only if profile `pi.allow_shell` is true**, else asks | auto | `coder.py:85-86`, `229` |
| `plan` | `code` tool refused (never called) | refused | read-only tools only, and those ask | `tools.py:483-484`, `manager.py:343-346` |

- Control-plane paths are denied in every mode by the Pi guards. The approver can only narrow
  them (`backends/pi.py:781-790`, `1098-1111`).
- **Money consent** (separate from file permissions): in `ask` mode, a slash command whose bound
  is above **$0.50** asks "...can spend up to $X. Continue?" (`commands.py:64`, `247-253`).
  Used by `/recompile`.
- Secret files are never shown by read tools: `credentials.json`, `model-prices.json`,
  `signing.key`, `~/.claude/.credentials.json`, `~/.codex/auth.json` (`tools.py:183-204`).

### 1.6 Slash commands (exhaustive)

Built-ins are registered in `commands.py`. Then `commands_providers`, `commands_forge` and
`commands_mcp` register theirs and **override** `/model`, `/forge`, `/export`
(`commands.py:1453-1464`). Dispatch is `commands.dispatch` (`commands.py:117-130`): an unknown
name falls back to a custom command, then to a "Did you mean" hint.

| Command | Effect | Cite |
|---|---|---|
| `/help [command]` | commands, keys, modes; per-command usage/detail | `commands.py:273-316` |
| `/mode [normal\|forge]` | show/set conductor mode | `commands.py:319-333` |
| `/make <description>` | one turn in build mode | `commands.py:336-341` |
| `/spawn <preset> <task>` | ephemeral preset subagent (presets in §2.3) | `commands.py:347-358` |
| `/forge <description>` or `/forge arc [--run] [--model M]` | Forge mode for one request, or the ARC-AGI harness. `--run` asks first (local GPU) | `commands_forge.py:79-138` (overrides `commands.py:361-398`) |
| `/agents` | installed agents, revision, license, floor | `commands.py:401-414` |
| `/run <agent> [input]` | run an installed agent. Input is JSON when it parses, else a string | `commands.py:417-437` |
| `/report <agent>` | spec summary and test report | `commands.py:440-454` |
| `/recompile <agent>` | forced rebuild (money consent) | `commands.py:457-502` |
| `/library` | harness library cache (sqlite read-only) | `commands.py:505-531` |
| `/floor [limit\|add <file>\|clear]` | next-compile floor override | `commands.py:605-624` |
| `/budget [session <usd> [min] \| run <usd> <s> \| compile <usd> [min]]` | session budget, or next-compile budgets | `commands.py:627-665` |
| `/model [number \| provider:model \| name \| add <p>:<m> [--price in/out]]` | picker, switch the conductor and the next compile | `commands_providers.py:346-402` (overrides `commands.py:668-703`) |
| `/models [provider] [filter]` | live model list | `commands_providers.py:445-481` |
| `/providers` | status of every provider | `commands_providers.py:413-420` |
| `/login [provider]` | guided sign-in | `commands_providers.py:484-524` |
| `/logout <provider>` | forget stored key (chatgpt: `codex logout`) | `commands_providers.py:549-610` |
| `/telemetry` | cost/quality over 24h/7d/30d/all | `commands.py:706-728` |
| `/permissions [ask\|auto-edit\|full-auto\|plan]` | coder approval mode | `commands.py:734-753` |
| `/status` | session id/title, mode, permission, model, cwd, workspace, profile, spend, messages, context, instructions, bound subagents | `commands.py:756-780` |
| `/cost` | spend, calls, tokens, last turn | `commands.py:783-799` |
| `/compact` | shorten old tool results | `commands.py:802-808` |
| `/clear` | new conversation (old one saved) | `commands.py:811-818` |
| `/resume [id]` | load a saved conversation | `commands.py:821-823`, `repl.py:1311-1327` |
| `/sessions` | the 20 latest saved conversations | `commands.py:826-837` |
| `/init` | turn asking the conductor to draft `ARCHITECT.md` via `code` | `commands.py:840-844` |
| `/bench <verb> ...` | `cli.main(["bench", ...])` in a thread (**prints to stdout**) | `commands.py:847-864` |
| `/exit`, `/quit` | quit | `commands.py:867-869` |
| `/context` | context breakdown by role | `commands.py:898-941` |
| `/diff` | `git diff HEAD` plus untracked, or the checkpoint diff outside git | `commands.py:973-1005` |
| `/undo` | restore the last turn's coder changes (asks y/n) | `commands.py:1008-1040` |
| `/config [set <key> <value>]` | show/set config.toml keys | `commands.py:1054-1112` |
| `/doctor` | providers, node, pi bridge, ollama, workspace, profile, disk | `commands.py:1189-1232` |
| `/memory [edit [global]]` | show or edit `ARCHITECT.md` (**runs an editor**) | `commands.py:1243-1287` |
| `/export [file]` | conversation as markdown | `commands.py:1319-1340` |
| `/export <agent> [path]` | export an installed agent into the repo (guarded, approval) | `commands_forge.py:154-205` |
| `/copy` | last answer to the clipboard (`clip`/`pbcopy`/...) | `commands.py:1380-1390` |
| `/review [focus]` | turn: review the working-tree diff | `commands.py:1393-1405` |
| `/title [name]` | show or set the conversation title | `commands.py:1408-1421` |
| `/image <path> [message]` | attach an image to the next (or this) message | `commands.py:1424-1448` |
| `/serve [port\|stop]` | start/stop the HTTP service child process | `commands_forge.py:404-477` |
| `/mcp [tools [server] \| add <name> <cmd...> \| add <name> --url <url> \| remove \| enable \| disable \| restart <name>]` | MCP servers | `commands_mcp.py:37-48`, `267+` |

**Custom commands:** `<workspace>/commands/<name>.md` and `<cwd>/.architect/commands/<name>.md`.
The project file wins, and custom commands never shadow built-ins. Optional front matter
`description:` is read. `$ARGUMENTS` is substituted, else the args are appended. Files are read
up to 32 KiB (`commands.py:133-204`).

**Other input syntax** (in `Repl.handle`, `repl.py:1130-1142`):
- `!<cmd>` runs a shell command in cwd. Output is shown and not sent to the model. 600 s
  timeout, 20k chars (`repl.py:1271-1309`).
- `/...` is a slash command. `//...` is sent as a normal message.
- `@path` attaches a text file inside cwd: max 8 files, 64 KiB each, binary skipped
  (`repl.py:227-260`). `@img.png|jpg|jpeg|webp|gif` attaches an image: max 8
  (`repl.py:275-289`).
- A trailing `\` continues the line (`repl.py:540-545`, `797-800`).

### 1.7 Keybindings (RichFrontend; `repl.py:780-843`, help table `commands.py:258-270`)

| Key | Action |
|---|---|
| Enter | send (or accept the highlighted completion first) |
| Ctrl+J, or `\` at line end | newline |
| Shift+Tab, Alt+M (`escape m`) | cycle normal/forge/plan |
| Tab | fuzzy slash menu with descriptions, command args, agent names, `@paths` (`ArchitectCompleter`, `repl.py:559-652`) |
| Up, Ctrl+R | history (`<workspace>/history`, `repl.py:744-748`) |
| Ctrl+G, Ctrl+X Ctrl+E | edit the buffer in an external editor (`editor` config, else `$VISUAL`, `$EDITOR`, notepad/vi; `config.py:319-331`) |
| Ctrl+C | clear the line. At an empty prompt, exit after a second press (`repl.py:814-820`, `1106-1113`) |
| Ctrl+D | exit |
| Esc or Ctrl+C during a turn | cancel the turn (raw key listener, `repl.py:904-925`) |
| typing during a turn | collected, and Enter queues it for after the turn. Ctrl+U clears (`repl.py:677-702`) |
| bracketed paste of 12 lines or more | collapsed to `[pasted N lines #k]`, expanded on submit (`repl.py:655-674`) |

### 1.8 Config keys (`agent/config.py:71-91`)

Files: `<workspace>/config.toml`, then `<cwd>/.architect/config.toml`, which wins key by key
(`config.py:210-220`). CLI flags win over both. `/config set` writes the **workspace** file,
line-based, and keeps tables and comments (`config.py:238-280`). A bad value is reported and
never fatal (`config.py:182-207`).

| Key | Kind | Default | Notes |
|---|---|---|---|
| `model` | str | none | conductor model (a profile model name) |
| `provider` | str | none | preferred provider for the picker |
| `permission` | choice | `ask` | `ask/auto-edit/full-auto/plan` |
| `session_budget_usd` | float | 25.0 | 0.01..10000 |
| `session_minutes` | float | 60.0 | 1..10080 |
| `auto_compact_percent` | float | 85.0 | 0 turns it off; 0..99 |
| `theme` | choice | `rich` | `rich` or `plain` |
| `editor` | str | none | |
| `web_fetch_allow_private` | bool | false | |

Tables the module does not own (e.g. `[mcp.servers.<name>]` with `command, args, env, cwd,
timeout` or `url, headers, enabled`) are kept in `Config.extra` (`config.py:193-200`). MCP also
reads `<cwd>/.mcp.json` (`mcp/config.py:1-5`).

---

## 2. The agent loop

### 2.1 Objects and wiring

`boot(options, ui, adapter=None, ...)` (`app.py:429-454`) does the following:
1. Gets the workspace via `agent_workspace` (`app.py:100-107`): `--workspace`, else
   `$ARCHITECT_WORKSPACE`, else `~/.architect`.
2. Loads the config (`config.load_config`).
3. Discovers the profile (`session/bootstrap.py:102-113`): explicit `--profile`, else
   `<cwd>/architect.json`, else `<workspace>/session-profile.json`.
4. If there is no profile, runs `first_run(ui, workspace)`, the providers onboarding
   (`app.py:457-468` -> `providers/signin.py:412-428`). It falls back to `bootstrap_profile`
   (CLI detection) when the providers package is missing (`app.py:155-236`).
5. Calls `build_app(...)` (`app.py:343-426`):
   - loads the profile, picks the model (`--model`, config `model`, else `compile.model_ladder[0]`,
     else the first profile model: `app.py:139-152`)
   - sets `ARCHITECT_SIGNING_KEY_FILE` to `<workspace>/signing.key` (as a default)
   - `Meter()` plus `observe_services(build_services(profile), meter)`: every model call is
     metered (`session/observe.py:52-91`)
   - builds the conductor adapter: `ObservedAdapter(adapter, meter)` when an adapter is injected,
     else `services.adapter_factory(model)`
   - `ConductorSettings` with permission/budget from config and options
   - `CheckpointStore(<workspace>/checkpoints)`
   - `extras` = `meter`, `profile_path`, `build_overrides`, `checkpoints`,
     `web_fetch_allow_private`, `workspace`, plus `make`/`forge`/`coder` bound lazily by
     `_bind_optional` (`app.py:326-340`). The coder is wrapped in `checkpointed(...)`.
   - creates the `Conductor`, sets `instructions = project_instructions(cwd, workspace)`
   - `extras["reload"]` rebuilds Services from the profile file and swaps them (`app.py:409-422`)
   - `SessionStore(<workspace>/sessions)`, then `App(...)`, then `sync_context_limit()`
6. Calls `attach_mcp(app, cwd)`, which bridges the MCP tools into `conductor.tools`
   (`app.py:471-486`).

`App` (`app.py:243-313`) has fields `conductor, meter, workspace, profile_path, store,
unavailable, config, checkpoints`. Methods: `services`, `save_session()`, `unpriced()`,
`context_tokens()`, `context_used_chars()`, `context_percent()`, `sync_context_limit()`, and
`close()`, which also stops `/serve` children (`app.py:306-313`). Also call
`close_mcp(app, cwd)` at the end (`app.py:489-496`).

`Conductor` (`conductor.py:120-321`) is a dataclass: `services, adapter, cwd, ui, settings,
tools=default_tools(), transcript, mode, session_id (12 hex), own_usd, started, extras,
instructions`. It also holds `self.context = ToolContext(...)` (`conductor.py:136-146`). The
`ToolContext` carries `permission`, `allowed` (session allowances), `model`, and `extras` (the
same dict as `conductor.extras`).

`ConductorSettings` (`conductor.py:99-109`): `max_steps=40`, `max_calls_per_step=8`,
`max_output_tokens=4096`, `call_usd=1.0`, `call_s=300`, `session_usd=25.0`, `session_s=3600`,
`context_chars=400_000`, `permission="ask"`. The Phase 12 doc's "$5 default" is stale. The code
and usage docs say $25.

### 2.2 One turn: `Conductor.turn(text) -> TurnResult(text, tool_calls, cost_usd, cancelled)`

`conductor.py:235-288`. If `streaming_adapter(self.adapter)` is not None, the turn goes to the
**native loop** (`native.py:300-352`). Otherwise it uses the **JSON envelope loop**:

1. `transcript.user(text)`.
2. For up to `max_steps` steps:
   - `_call(extra)` (`conductor.py:203-231`) makes one metered call via
     `services.runtime.evaluate(control_spec(...), one, ...)`. The budget per call is
     `min(call_usd, remaining_usd())`; with no budget left it raises
     `BudgetExceeded("session_budget_exhausted")`. It emits `Thinking(True)` and then
     `Thinking(False)`. The prompt is `system_prompt()` (SYSTEM + tool listing JSON + cwd +
     project instructions, the stable cache prefix) plus `"Conversation (JSON messages...)"` +
     `transcript.render()` + BUILD_MODE (if build) + REMINDER or REPAIR (`conductor.py:172-185`).
   - The reply is parsed by `_control_reply` (`backends/pi.py:661-695`). A fenced or
     prose-wrapped envelope is normalized. If the reply is not an envelope but looks like a
     missed tool call (`_tool_attempt`, `pi.py:698-709`), it is repaired up to `MAX_REPAIRS=2`
     times with the REPAIR prompt. Otherwise the raw reply is treated as `{"text": reply}`.
   - `{"text": ...}`: `transcript.assistant(answer)`, emit `AssistantText`, return.
   - `{"tool_calls": [...]}`: `transcript.assistant(calls=...)`, then `_dispatch` each one.
3. Step limit: a "say 'continue'" note and a `Notice("warn")` (`conductor.py:274-277`).
4. `CancelledError`: adds a note "The user interrupted this turn; finished tool results are
   kept." and re-raises (`conductor.py:278-280`).
5. `BudgetExceeded`: adds a note, emits an error Notice, returns (`conductor.py:281-288`).

**The JSON envelope** (`conductor.py:64-66`): each model reply must be exactly one JSON object.
- `{"tool_calls": [{"id": "call-1", "name": "<tool>", "arguments": {...}}]}`
- `{"text": "<markdown answer>"}`

**`_dispatch`** (`conductor.py:290-317`) emits `ToolStarted(call_id, name, tool_summary)`, then
runs `tool.run(args, self.context)`:
- `ToolError` becomes the result `{"error": code, "detail": ...}` with ok=False.
- An unknown tool becomes `{"error": "unknown_tool", "tools": [...]}`.
- Any other exception becomes `{"error": "tool_failed", "detail": ExcName}`.
- A result with `cost_usd` is charged.
- `_finish` writes `transcript.tool(...)` and emits `ToolFinished(call_id, name, ok,
  result_summary)`.

**Native loop** (`native.py`). It is used when the adapter implements `ChatStreaming`: the
Anthropic, OpenAI-compatible and Ollama HTTP adapters (`models/http.py:656`, `863`, `1099`). CLI
adapters (claude, codex) never stream. `ObservedAdapter` is unwrapped into `ObservedChat` so the
meter still counts (`native.py:73-153`). Per model call it emits `Thinking(True)`, then one
**`TextDelta`** per streamed chunk (`native.py:278-280`), then `Thinking(False)`. After that it
either dispatches tools, where any streamed text was a preamble, or ends with `AssistantText`
holding the whole answer (`native.py:300-352`). Native tool specs come from the same `Tool`
objects (`native.py:156-157`). The transcript is converted per call by `chat_messages`, which
re-ids calls, fills unanswered ones with `not_run`, and turns notes into user text
(`native.py:169-258`). A `max_tokens` stop emits a warn Notice.

### 2.3 Conductor tools (`tools.py:699-741`)

| Tool | Args | Spends | Behaviour |
|---|---|---|---|
| `read_file` | `path, offset?, limit?` | no | numbered lines, ≤2000 lines, 256 KiB, secret files denied (`tools.py:222-245`) |
| `list_dir` | `path?` | no | ≤200 entries, skips VCS/venv dirs (`tools.py:259-274`) |
| `glob` | `pattern` | no | `tools.py:277-285` |
| `grep` | `pattern, path?, glob?` | no | ≤100 matches (`tools.py:288-314`) |
| `code` | `task` | yes | the coder subagent (`extras["coder"]`). Refused in plan (`tools.py:481-488`) |
| `spawn` | `preset ∈ {extractor, synthesizer, planner, verifier, explorer, recommender, hypothesis_tester}, task, input?, output_schema?` | yes | `Preset.build` + `runtime.evaluate`, unlicensed (`tools.py:457-478`) |
| `make` | `description` | yes | build/Forge mode (`extras["make"]` = `build.make`) |
| `forge` | `target` | yes | `extras["forge"]` = `build.forge_target` |
| `web_fetch` | `url, max_chars?` | no | httpx, private addresses blocked unless config allows, 2 MiB, 20 s, 5 redirects (`tools.py:608-679`) |
| `agents` | – | no | registry listing (`tools.py:327-348`) |
| `report` | `name` | no | spec, report, harness design docstring (`tools.py:351-370`) |
| `run_agent` | `name, input` | yes | license re-read, input validated, `runtime.run` (`tools.py:384-411`) |
| `mcp__<server>__<tool>` | per server | – | added by `attach_mcp`, approved per §1.5 |

Each tool also emits `SubagentActivity` lines (spawn: `tools.py:469`, run_agent: `tools.py:402`).
Errors are `ToolError(code, detail)` with fixed codes (`tools.py:90-96`). The REPL maps some to
hints in `TOOL_HINTS` (`repl.py:121-131`).

### 2.4 The coder subagent (Pi)

`agent/coder.py`. `run_coder(request, ctx)` (`coder.py:221-283`):
- Config is the profile's effective Pi config, with tools `read, write, edit[, bash]` and
  `allow_shell` (`coder.py:70-78`). `ctx.extras["coder_shell"] = False` withholds bash.
- `PiBackend(config, approver=run.approve, on_event=run.on_event)`. `PiBackend.run` starts the
  Node bridge `architect/backends/pi-node/bridge.mjs` (`@mariozechner/pi-coding-agent`) and
  speaks JSON lines with it over the child's pipes. Model calls come back to Python as
  `kind: "model"` and run through the **same model as the conductor**
  (`services.adapter_factory(ctx.model)`, `coder.py:245`). Tool requests come back as
  `kind: "tool"`.
- Budget is its own per-run `max($1, $2)` = $2 and `max(600, 900)` = 900 s, capped by the
  session remainder (`coder.py:50-51`, `231-238`). Waiting for an approval counts against the
  seconds.
- Returns `{"ok", "summary", "changed_files", "tool_calls", "denied", "cost_usd"[, "error"]}`
  (`coder.py:273-283`). Error codes: `coder_budget_exceeded`, `coder_failed`,
  `coder_invalid_output`.

**Approval flow, exactly:**
1. Pi asks to run tool `name` with `args`. `PiBackend` first applies every guard: the tool is
   allowed, the shell grant, and `_granted_path` (inside the repo, not a denied path)
   (`pi.py:1090-1106`).
2. Only if the guards allow it and `name ∈ APPROVED_TOOLS = {"write","edit","bash"}`
   (`pi.py:485`) does it call `self._approve(name, args)` (`pi.py:1107-1111`). That passes a
   deep copy to the approver. Any exception or a non-`True` answer is a denial
   (`pi.py:831-843`).
3. `_Run.approve` (`coder.py:165-179`):
   - unknown tool: deny
   - `_auto_allowed(ctx, tool, operator_shell)` (permission rules, §1.5) or
     `tool in ctx.allowed`: allow, without asking
   - otherwise `await ctx.ui.approve(tool, approval_detail(tool, args))`
   - `"session"` adds `tool` to `ctx.allowed` (so all writes, all edits, or all bash for the rest
     of the session, until the permission changes) and allows. `"yes"` allows once. Anything
     else denies.
4. `approval_detail` (`coder.py:103-128`) is plain text, at most 1500 chars:
   - bash: the command.
   - write: `path (N bytes, M lines)` followed by up to 12 `+ ` lines.
   - edit: `path (k edits)` followed by `- old` / `+ new` lines for the first 3 edits.
   - Control characters are stripped.
5. The terminal UIs render it as `Allow <action> <detail>? [y]es / [s]ession / [n]o `. Enter
   means no. **5-minute timeout means no** (`repl.py:102`, `415-422`). Answer parsing:
   `y/yes` gives yes, `s/session/a/always` gives session, anything else gives no
   (`repl.py:201-210`).

**Coder events** (Phase 12 §8): the bridge sends `{"kind":"event", ...}` only when `on_event`
is set (`pi.py:994`, `1026-1034`). They are sanitized to `tool_start{tool,summary}`,
`tool_end{tool,ok,summary}` and `text{text}` (`pi.py:498-530`). `_Run.on_event`
(`coder.py:181-206`) turns them into `SubagentActivity("coder", "write src/x.py: ok")` and
similar lines, and records the edited paths.

Other approve() callers:
- MCP: action `"mcp <server>.<tool>"`, detail = an argument summary. `session` is stored in
  `ctx.allowed` under that action string. A denial is `ToolError("denied_by_user")`
  (`mcp/manager.py:339-358`).
- Export into software: `ctx.ui.approve("export", detail)` with the staged file list
  (`agent/export.py:417`).

### 2.5 Build/Forge mode (the `make` tool), `agent/build.py`

`make(description, ctx)` (`build.py:1515-1531`) calls `_build` (`build.py:1534-1648`):
1. **Interview.** `_propose` makes one model call (emits a `Progress`). Then `_ask_destination`
   always asks first: `ctx.ui.ask(DESTINATION_QUESTION, default="1"|"2", choices=("1","2"))`,
   where 1 = run it here and 2 = into software. For software it then asks `PATH_QUESTION`
   (`build.py:608-644`). After that, up to 5 uncertain fields are asked with defaults. `/skip`
   accepts the rest and `None` cancels (`build.py:647-683`).
   `ctx.extras["skip_interview"]` skips every question.
2. **Stage 0** `infer_spec` (Progress "stage 0 infer spec...").
3. **Floor**: drafts SYNTHETIC cases when there is no real data (Progress plus
   `SubagentActivity("floor", ...)`).
4. **Plan card**: `PlanCard("Plan: <name>", rows, footer)`, then
   `ctx.ui.ask("Compile this agent?", default="yes")`. Empty or "y" compiles, anything else
   cancels. If something blocks, it emits `PlanCard("Plan (blocked)", ...)` and stops
   (`build.py:1620-1637`).
5. **Compile**: `SubagentActivity("compiler", ...)`, stage progress through `_listener`
   (`Progress("stage N name: calls, $")`, `build.py:1259-1267`), and Forge tool events as
   `SubagentActivity("forge", ...)` (`build.py:1359-1369`).
6. **Install**: may ask about a higher per-run budget (`build.py:1430-1459`). The result card is
   a `PlanCard` (`build.py:1814-1842`). For "into software" it calls `export_agent`, which uses
   `approve("export", ...)`.

Next-compile overrides live in `extras["build_overrides"]`, set through `build.set_override`
(`build.py:298-377`). Keys: `floor_limit, floor_file, budget_usd, budget_s, compile_usd,
compile_s, model, name, engineer`. `override_lines` renders them (`build.py:384-421`).

### 2.6 Events (the UI vocabulary), `agent/events.py`

| Event | Fields | Emitted by |
|---|---|---|
| `Thinking` | `active: bool, label="architect"` | every conductor model call start/end |
| `TextDelta` | `text` | native streaming only |
| `AssistantText` | `text` (markdown, final) | end of turn |
| `ToolStarted` | `call_id, name, summary` | `_dispatch` |
| `ToolFinished` | `call_id, name, ok, summary` | `_finish` |
| `SubagentActivity` | `agent, text` | coder, spawn, run_agent, floor, compiler, forge |
| `Progress` | `text` (replaceable status line) | build stages, export |
| `Notice` | `text, level ∈ info/warn/error` | everywhere (`Repl.say`) |
| `PlanCard` | `title, rows: ((label, value), ...), footer` | build mode plan and result |

There are **no** events for turn start/end, usage per call, session-id change, mode or
permission change, or context fill. Those are pulled from state (`Repl._toolbar_values`,
`repl.py:1038-1058`) or from `Meter` (`meter.spend`, `meter.in_flight`, and the per-call
`meter.events()` queue of `MeterEvent(kind, model, tokens_in, tokens_out, cost_usd, wall_s)`,
`session/observe.py:52-91`, `session/state.py:153-159`).

### 2.7 Streaming render, context meter, compaction

- **Rendering streams**: `RichRenderer` prints finished markdown blocks permanently and keeps
  the open block in a rich `Live` region above the spinner. The final `AssistantText` prints
  only the remainder that was not streamed (`render.py:278-295`, `459-469`, `502-506`).
  `split_blocks` cuts at blank lines or closed fences (`render.py:255-275`). `PlainRenderer`
  writes deltas raw (`render.py:311-332`).
- **Context meter**: `App.context_percent()` = (len(system_prompt) + len(transcript.render())) /
  (context_window(model) × 4 chars/token) (`app.py:284-296`). The window is a prefix table, with
  200k for unknown models (`config.py:286-316`). Colours: green below 50, yellow below 80,
  otherwise red (`render.py:130-136`).
- **Context limit**: `sync_context_limit` sets `settings.context_chars = max(20000, 0.95×window
  − prefix − output reserve)` (`app.py:298-304`). `Transcript.fit(limit)` runs before every call
  (`conductor.py:180`, `native.py:273`). It compacts old tool results to 240 chars with
  keep = 8, 4, 2, 0, then drops the oldest messages behind a marker note (`transcript.py:97-122`).
- **Auto-compact**: before each REPL turn, if `context_percent >= auto_compact_percent` (85),
  it compacts and, if needed, fits to half the threshold, then emits a Notice and saves
  (`repl.py:1157-1174`).
- **Toolbar fields** (`repl.py:1038-1058`, `render.py:143-179`): mode label, permission, model,
  `ctx N%`, `$spent/$budget` (or "cost unknown"), elapsed/budget minutes, queued count,
  "N call(s) running".

### 2.8 Checkpoints, `agent/checkpoints.py`

- `checkpointed(coder)` wraps the coder (`checkpoints.py:330-370`). Before a run it calls
  `capture(cwd)`: up to 5000 files, 2 MiB each, 64 MiB total, skipping the same directories
  as the tools. After the run it records `changed_since(snapshot)` ∪ the reported
  `changed_files`. It records even on cancellation.
- Storage: `<workspace>/checkpoints/<session_id>/<turn:05d>/manifest.json`
  (`{session, turn, time, request, undone, files:[{path, state: file|absent|unsaved, blob}]}`)
  plus `files/<nnnnn>.bin` holding the pre-run bytes (`checkpoints.py:140-192`, `238-262`).
  Several coder runs in one turn merge into one checkpoint, and the earliest content wins.
- The key `(session_id, turn)` comes from `extras["checkpoint_key"]`, which **the REPL sets
  before each turn** (`repl.py:1176-1180`). Without it the session is "default".
- `/undo` restores the newest checkpoint not yet undone: it writes the old bytes, deletes files
  that were new, and skips "unsaved" ones. It marks the checkpoint undone
  (`checkpoints.py:231-290`). `/diff` outside git = `store.diff(session, cwd)`
  (`checkpoints.py:292-327`).

### 2.9 Sessions (storage, resume)

- Location: `<workspace>/sessions/` (`app.py:423`). Default workspace is `~/.architect`
  (`app.py:100-107`).
- Format: `<id>.jsonl` holds one message per line, as `Message.to_json()`
  (`transcript.py:39-51`):
  `{"role": "user"|"assistant"|"tool"|"note", "text"?, "tool_calls"?: [{id,name,arguments}],
  "call_id"?, "name"?, "ok"?: false}`. Tool results are clipped at storage time to 12,000 chars
  (`transcript.py:19`, `84-88`).
- `index.json` is a list, newest first, of `{id, title, time, cwd, messages, named}`
  (`sessions.py:163-170`, `245-247`). The title is the first line of the first user message (60
  chars) or the `/title` name. Writes are atomic (`.tmp` + `os.replace`), and a damaged index is
  rebuilt from the files (`sessions.py:189-243`). Ids are 12 hex chars matching
  `^[A-Za-z0-9_-]{1,64}$`.
- Saving: `App.save_session()` after **every** REPL turn (`repl.py:1234`), on `/compact`,
  `/clear`, `/title`, `/undo`, `/resume` and exit. An empty transcript is not saved.
- Resume: `Repl.resume(key)` saves the current conversation, loads the transcript, and sets
  `conductor.transcript` and `conductor.session_id` (`repl.py:1311-1327`). Mode, permission,
  model and checkpoints are **not** stored in the session. `--continue` is the same as
  `store.find(None)`, the newest session.
- Conversation title lives in `conductor.extras["session_title"]` until saved.

### 2.10 Images

- `@image` or `/image` makes `ImageInput.from_bytes` (sniffed header, ≤20 MiB;
  `models/images.py:26-55`, `repl.py:263-272`). The message gets `<attached image="rel"
  media_type=... size=WxH sha256=... />` markers (`repl.py:292-294`). The images go into
  `conductor.extras["pending_images"]` for this turn only (`repl.py:1182-1202`, `1219`).
- They only reach the model when `Conductor.supports_images()`: the **envelope path** (not
  native streaming) **and** the adapter's price entry declares an `image` ceiling
  (`conductor.py:148-163`). Then `context.call(prompt, ..., images=images)` sends them on every
  call of that turn (`conductor.py:215-220`). Claude CLI gets them via stream-json, Codex via
  `-i` files (`models/cli.py:19-27`). Otherwise the user gets a warning that the images are a
  "reference only".

### 2.11 /serve

`commands_forge.py:208-477`. It spawns `sys.executable -m architect.server --profile <p> --host
127.0.0.1 --port <port>` (default **8765**, `export.py:73`) with its own process group and no
window. The token is `$ARCHITECT_API_TOKEN` if valid (32-512 printable chars), else
`secrets.token_urlsafe(32)` (`commands_forge.py:311-320`). It logs to
`<workspace>/serve/server-<port>.log` and waits up to 30 s for the port to accept.
`Served{process, port, token, log, command, started}` is kept in `extras["serve"]` and in the
module-level `_LIVE`. It stops via taskkill /T on Windows and on `/serve stop`, `App.close()`
(`app.py:306-313`) or atexit (`commands_forge.py:286`). It prints the url, token, routes, and
first-call instructions. The service runs only agents it licensed itself, so it needs one
`POST /compile {"spec": ..., "approved": true}` per agent first (`commands_forge.py:375-401`).

### 2.12 HTTP service (server.py) and client-ts

- FastAPI app from `create_app(services, api_token=...)` (`server.py:1260`). Routes:
  `POST /compile`, `POST /run`, `GET /status/{artifact_id}`, `POST /recompile`, `POST /route`
  (`server.py:1280-1315`). Bearer auth is checked before the body is read. Bodies are capped at
  1 MiB. Errors are fixed codes.
- `main` uses uvicorn with flags `--profile --host --port --token-env --allow-non-loopback`
  (`server.py:1349-1395`).
- **It is not an agent or chat API.** No conversation, events or approvals. It serves compiled,
  licensed agents to trusted backends (e.g. Lectern).
- `client-ts/` is `@architect/client` (Node ≥22, private): `ArchitectClient(baseUrl, token,
  {timeoutMs})` with `compile`, `run`, `status`, `recompile`, `route` over those 5 routes
  (`client-ts/README.md`, `client-ts/src/index.ts`). It is not useful for the desktop chat UI.
  It could be reused for a desktop "installed agents / run agent" panel once `/serve` is up.

---

## 3. Providers and auth

### 3.1 Catalog (`providers/catalog.py:84-165`)

| id | kind | adapter (`AdapterConfig.provider`) | sign-in | default model / base URL | key env |
|---|---|---|---|---|---|
| `claude` | cli | `claude-code` | `claude_cli` | models `claude-haiku-4-5-20251001, claude-sonnet-5, claude-opus-5-5, claude-fable-5-1` (`catalog.py:52-57`) | – |
| `chatgpt` | cli | `codex` | `codex_login` | `gpt-5.6-terra` (`catalog.py:58`) | – |
| `anthropic` | api | `anthropic` | api_key | `https://api.anthropic.com`, claude-sonnet-5 | `ANTHROPIC_API_KEY` |
| `openai` | api | `openai` | api_key | `https://api.openai.com/v1`, gpt-4o-mini | `OPENAI_API_KEY` |
| `openrouter` | api | `openai` (listing `openrouter`, with prices) | api_key | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| `gemini` | api | `openai` | api_key | `.../v1beta/openai`, gemini-2.5-flash | `GEMINI_API_KEY`, `GOOGLE_API_KEY` |
| `groq` | api | `openai` | api_key | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` |
| `deepseek` | api | `openai` | api_key | `https://api.deepseek.com/v1` | `DEEPSEEK_API_KEY` |
| `xai` | api | `openai` | api_key | `https://api.x.ai/v1` | `XAI_API_KEY` |
| `mistral` | api | `openai` | api_key | `https://api.mistral.ai/v1` | `MISTRAL_API_KEY` |
| `together` | api | `openai` | api_key | `https://api.together.xyz/v1` | `TOGETHER_API_KEY` |
| `ollama` | local | `ollama` | local_url | `http://localhost:11434` (or `$OLLAMA_HOST`) | optional |
| `lmstudio` | local | `openai` | local_url | `http://localhost:1234/v1` | optional |
| `llamacpp` | local | `openai` | local_url | `http://localhost:8080/v1` | optional |
| `vllm` | local | `openai` | local_url | `http://localhost:8000/v1` | optional |
| `custom` | api | `openai` | custom_url | user URL | optional |

The profile also allows `provider: "fake"` (fixtures) (`profiles.py:126-127`).
`provider:model` syntax picks the provider (`catalog.py:206-216`). For example,
`ollama:gemma4:26b` names Ollama, while a bare `gemma4:26b` stays a model name.

### 3.2 Credentials

- **API keys** (`providers/credentials.py`): the provider's environment variables come first,
  then `<workspace>/credentials.json` = `{"version":1, "keys":{id: key}, "base_urls":{id:
  url}}`. The file is written atomically with mode 0600 where POSIX honours it
  (`credentials.py:80-98`), and keys are never logged. The profile refers to a key by name with
  `api_key_ref` (or `api_key_env`), never by value (`profiles.py:131-140`, `288-299`).
- `/login <api provider>` (`signin.py:173-210`): if an environment key exists it is used and
  checked with one model-list call. Otherwise the user pastes a key through
  `prompter.secret` (`ui.ask_secret`, hidden) or plain `ask`. The key is verified with one list
  request (401/403 means it is not stored) and then stored.
- Local and custom (`signin.py:228-266`): ask for the URL, list the models, store the base_url
  (and an optional key). Architect never starts a server or loads a model.
- **Claude** (`signin.py:137-144`): Architect never holds the login. It checks that `claude` is
  on PATH. The status check looks at `~/.claude/.credentials.json` or `$CLAUDE_CONFIG_DIR`, but
  a keychain login is also accepted as `cli_found` (`status.py:51-73`). Calls run `claude -p`
  in an empty temp dir, with `ANTHROPIC_API_KEY/AUTH_TOKEN` stripped so the subscription is used
  (`models/cli.py:286-330`).
- **ChatGPT/Codex** (`signin.py:147-170`): this is **OAuth through the official Codex CLI**.
  Architect runs `codex login` in the foreground (`asyncio.to_thread(host.runner, [codex,
  "login"])`). The runner is `run_foreground`, i.e. `subprocess.run(argv)` with **inherited
  stdin/stdout/stderr** (`providers/host.py:23-32`). `codex login` opens OpenAI's browser OAuth
  flow. Afterwards Architect only checks that **`$CODEX_HOME/auth.json` or
  `~/.codex/auth.json` exists** (`status.py:51-55`). It **never reads its contents**, and the
  conductor's read tools hide that file (`tools.py:197`). There is **no device-flow code in
  Architect** and no token handling. `/logout chatgpt` runs `codex logout`
  (`commands_providers.py:566-574`). Model calls run `codex exec --json --ephemeral
  --skip-git-repo-check --sandbox read-only --ignore-rules -m <model> -o <root>/last-message.txt
  [-c model_instructions_file=...] -` with `OPENAI_API_KEY/CODEX_API_KEY` stripped, so the saved
  login is used (`models/cli.py:439-500`). Profile validation forbids API keys on codex adapters
  (`profiles.py:160-161`).
- Injection seam: `conductor.extras["providers_host"] = Host(which, env, home, transport,
  runner)` (`commands_providers.py:74-76`, `providers/host.py:35-41`). A desktop bridge should
  set a `Host` whose `runner` launches `codex login` **without** inheriting the bridge's stdio
  (e.g. stdout/stderr to a pipe or a new console). Note that the first-run `onboard` is called
  with `host=None` (`app.py:466` -> `signin.py:412-428`), so the default runner would be used
  there unless the bridge pre-creates the profile or patches it.

### 3.3 Status, model listing, selection

- `all_statuses(workspace, probe=...)` returns one `ProviderStatus(provider, state, ready,
  detail, models)` per catalog entry. These are cheap checks: PATH and login file for CLIs, key
  presence for APIs, and a model-list GET with a 1.5 s timeout for local servers
  (`status.py:36-123`). States: `signed_in, cli_found, cli_missing, key_env, key_stored, no_key,
  reachable, unreachable, not_checked, not_configured`.
- `list_models(item, workspace, base_url?, api_key?, host?)` returns a
  `Discovery(ok, code, models: Discovered(id, price?), base_url)` (`discovery.py:38-165`). It
  uses `/v1/models`, Ollama `/api/tags`, OpenRouter with prices, or the fixed lists for CLIs.
  Failure codes: `no_key, unauthorized, unreachable, timeout, no_url, http_error,
  invalid_response` (`commands_providers.py:434-442`).
- `onboarding_choices(workspace)` ranks the providers and marks one recommended. The preference
  order is claude, chatgpt, anthropic, openai, openrouter, gemini. Local providers are never
  recommended (`onboarding.py:56-145`).
- `register_model(profile_path, provider_id, model, workspace=, base_url=, price=, host=)`
  writes or creates the JSON profile entry with the right `AdapterConfig`, and writes a price
  entry to `<workspace>/model-prices.json` when the model is not bundled ($0 for local, the
  OpenRouter price, a declared price, or "unpriced"). It returns the profile model key
  (`onboarding.py:287+`).
- Switching (`commands_providers.py:152-208`): `register_model`, then `extras["reload"]()` (which
  rebuilds Services and swaps `conductor.services` and `conductor.adapter`, `app.py:409-420`),
  then `conductor.adapter = services.adapter_factory(name)`, `conductor.context.model = name`,
  and the build override `model`. If the reload fails, the old profile file is restored.
- Unpriced models: allowed for conversation on a time budget, shown as "cost unknown". Build
  mode refuses them with `model_unpriced` (`build.py` blockers; `profiles.model_unpriced`
  `profiles.py:302+`).

### 3.4 Files and locations

| What | Where |
|---|---|
| Workspace | `--workspace`, else `$ARCHITECT_WORKSPACE`, else `~/.architect` (`app.py:100-107`). (The classic `architect session` defaults to `<cwd>/.architect`, `bootstrap.py:116-122`.) |
| Profile | `--profile`, else `<cwd>/architect.json`, else `<workspace>/session-profile.json` (`bootstrap.py:64-65`, `102-113`). JSON or TOML. Schema `profiles.Profile` (`profiles.py:185-215`) |
| CLI config | `<workspace>/config.toml`, `<cwd>/.architect/config.toml` |
| Keys / base URLs | `<workspace>/credentials.json` |
| Extra prices | `<workspace>/model-prices.json` |
| Signing key | `$ARCHITECT_SIGNING_KEY_FILE`, default `<workspace>/signing.key` (`app.py:370`) |
| Sessions | `<workspace>/sessions/<id>.jsonl`, `index.json` |
| Checkpoints | `<workspace>/checkpoints/<session>/<turn>/` |
| Input history | `<workspace>/history` |
| Custom commands | `<workspace>/commands/*.md`, `<cwd>/.architect/commands/*.md` |
| Project instructions | first of `ARCHITECT.md`, `AGENTS.md`, `CLAUDE.md` in cwd (16 KiB) plus `<workspace>/ARCHITECT.md` (`conductor.py:41-96`) |
| /serve logs | `<workspace>/serve/server-<port>.log` |
| Registry, library, telemetry | under `<workspace>/` (`profiles.py` `WORKSPACE_STATE`) |

---

## 4. The seam for a UI, and the recommended bridge

### 4.1 Where core ends and rendering begins

```
Conductor (core loop)        --emit(Event)-->      UI
  tools, coder, build,       <--ask()/approve()--  (RecordingUI | PrintUI | LineFrontend | RichFrontend)
  MCP, export
                                                   Frontend = UI + renderer + read_line + watch + attach
Repl (session shell)         --say/text/rows/diff--> frontend.renderer
  slash commands,            <--read_line()------  frontend
  @attach, !shell,           --watch(task)------>  frontend (cancellation, queued typing)
  checkpoints, save,
  auto-compact, queue
```

- **Layer 1, pure core:** `Conductor.turn(text)` and `UI` (`events.py`). The tests drive it
  with `RecordingUI` (`events.py:127-155`): scripted `answers`, `approvals` and
  `default_approval`, plus recorded `events` and `questions`. This is the smallest API, but it
  skips slash commands, attachments, checkpoint keys, auto-compact and saving.
- **Layer 2, Repl:** `Repl(app, frontend)` (`repl.py:986-1327`) with `run()`, `handle(line)`,
  `turn(text, mode=)`, `work(awaitable)`, `call_tool(name, args)`, `shell(cmd)`, `resume(key)`,
  plus helpers `say/text/rows/diff`, `command_names()`, `describe_command()`, `agent_names()`,
  `spend_line()`, `_toolbar_values(width)`, `banner()`, and the `queue` and `pending_images`
  lists. **Every slash command goes through `repl.say` (Notice), `repl.text`, `repl.rows`,
  `repl.diff` (renderer methods) and `repl.frontend.ask`.** So a frontend whose renderer
  serializes those 4 calls plus `show(event)` captures all CLI output.
- The `Frontend` protocol to implement (`repl.py:364-375`, plus the de-facto use in
  `commands*.py`):
  - `emit(event)`, `async ask(question, *, default=None, choices=()) -> str|None`,
    `async approve(action, detail) -> "yes"|"session"|"no"`
  - `renderer` with `show(event)`, `text(text, style="")`, `rows(title, rows)`, `diff(text)`,
    and attribute `glyphs` (used by `/doctor` and the spend line, via `getattr` with fallback)
  - `attach(repl)`, `async read_line() -> str|None` (None ends the run; raise
    `repl.Interrupted` for Ctrl+C semantics), `async watch(task)` (await the task and cancel it
    on user cancel; `_await_cancellable` at `repl.py:384-391` helps)
  - optional `async ask_secret(question)`, used for API keys (`commands_providers.py:79-81`,
    `signin.py:63-71`)
  - optional `close()` (called at the end of `run`, `repl.py:1124-1126`)

### 4.2 Recommendation: `architect desktop-bridge`, JSON lines over stdio, wrapping `Repl`

This is the narrowest route that keeps every feature without reimplementation. Add one module
`architect/agent/bridge.py` and one early branch in `cli.main` (next to `serve`/`session`,
`cli.py:1257-1270`). Nothing in the core changes.

**Process:** `architect desktop-bridge --cwd <dir> [--workspace] [--profile] [--model]
[--permission]`, spawned by the desktop app with pipes.
- At startup: `proto = os.fdopen(os.dup(1), "w")`, then `os.dup2(2, 1)`. After that, children
  that inherit stdout (codex login, the `/bench` print path, the editor) cannot corrupt the
  protocol. Also set `sys.stdout = sys.stderr`. Alternatively, use a named pipe or a localhost
  socket.
- Run `asyncio.run(main())`:
  1. `options = AppOptions(cwd=..., workspace=..., profile=..., model=..., permission=...,
     config=load_config(agent_workspace(cwd, ws), cwd))` (`app.py:88-97`).
  2. `frontend = BridgeFrontend(proto)`.
  3. `app = await boot(options, frontend)`. First-run onboarding questions arrive as `ask`
     requests. Catch `BootError(code)`.
  4. `repl = Repl(app, frontend)`. Optionally `repl.resume(id)`. Then `await repl.run()`.
  5. `finally: await close_mcp(app, cwd); app.close()`. This is the same lifecycle as
     `_interactive` (`repl.py:1446-1474`).

**BridgeFrontend (sketch):**
- A reader task parses stdin lines into dicts and routes them by `type`:
  - `input {text}`: if a turn is running, append to `repl.queue` (the same semantics as the TUI's
    queued typing) and emit `queued`. Otherwise put it on `lines: asyncio.Queue`.
  - `answer {id, value|null}`: resolve the pending `ask` future.
  - `approval {id, answer: "yes"|"session"|"no"}`: resolve the pending `approve` future.
  - `cancel`: `self._task.cancel()` for the watched task.
  - `shutdown` or EOF: `lines.put(None)`.
  - `snapshot`: reply with a state dump (below). This is read-only and safe at any time.
- `read_line()` awaits `lines.get()`.
- `watch(task)`: emit `{"type":"busy"}`, then `await _await_cancellable(task)`, then
  `{"type":"idle"}`.
- `emit(event)`: `{"type":"event","kind":type(event).__name__, **dataclasses.asdict(event)}`
  (all events are frozen dataclasses of str/bool/tuples).
- `ask(question, default, choices)`: `{"type":"ask","id":n,"question":..,"default":..,
  "choices":[..]}`, then await. On cancel or None return None, keeping the `_Questions`
  semantics where an empty answer means the default (`repl.py:405-413`).
- `approve(action, detail)`: `{"type":"approve","id":n,"action":"write|edit|bash|mcp s.t|export",
  "detail":..}`, then await with its own timeout and default "no". Unknown answers become "no".
  Never guess.
- `ask_secret(question)`: `{"type":"ask","secret":true,...}`. The desktop app uses a password
  field. Never log it.
- `renderer`: `show = emit`. `text(t, style)` emits `{"type":"text","text":t,"style":style}`.
  `rows(title, rows)` emits `{"type":"rows","title":..,"rows":[[k,v],..]}`. `diff(t)` emits
  `{"type":"diff","text":t}`. `glyphs = render.ASCII` (or UNICODE).
- After each turn (in `watch`'s finally, or wrapping `Repl.turn`), emit a `state` message from
  `repl._toolbar_values(120)` (mode, permission, model, spent/budget usd, elapsed/budget s,
  context %, queued, unpriced), plus `conductor.session_id`, `cycle_state(conductor)`,
  `app.meter.spend`, and `repl.last_turn` (TurnResult). Optionally forward
  `app.meter.events()` per model call for live token/cost ticks.

**Desktop-native actions** can be sent as **slash-command lines** through `input`, so there is
one code path:
- mode cycle: `cycle_mode(conductor)` (`repl.py:181`). It is safe to call directly when idle
  because it is synchronous and has no I/O. You can also send `/mode forge` or
  `/permissions plan`.
- model picker: `/model` emits `rows` (`commands_providers.py:267-293`). For a structured list,
  call `await picker_rows(repl)` (`commands_providers.py:229-254`) and switch with
  `/model <provider:model>`.
- providers: `await all_statuses(app.workspace)`; login: `/login <id>` (questions arrive as
  `ask`).
- sessions: `app.store.list()` returns `SessionInfo` rows (`sessions.py:278-279`). Resume with
  `/resume <id>`, new with `/clear`, rename with `/title`.
- interrupt: `cancel`. undo/diff: `/undo` (asks), `/diff` (diff message).
- images: `/image <path> [message]`, or put `@path` in the text. Paths must be **inside cwd**
  (`repl.py:222-224`). A drag-dropped image from elsewhere must be copied into cwd first, or
  a small `pending_images` RPC added.

**Wire protocol summary (proposed):**
```
desktop -> bridge: {"type":"input","text":"..."} | {"type":"answer","id":7,"value":"y"|null}
                   {"type":"approval","id":8,"answer":"yes"|"session"|"no"} | {"type":"cancel"}
                   {"type":"snapshot"} | {"type":"shutdown"}
bridge -> desktop: {"type":"ready","session":..,"model":..,"cwd":..,"workspace":..}
                   {"type":"event","kind":"TextDelta"|"AssistantText"|"ToolStarted"|...,...}
                   {"type":"text"|"rows"|"diff",...}   (slash-command output, banner)
                   {"type":"ask",...} {"type":"approve",...}
                   {"type":"busy"} {"type":"idle"} {"type":"queued","count":n}
                   {"type":"state",...} {"type":"fatal","code":"no_provider"|...}
```

**Things a bridge must watch for:**
- **One asyncio loop:** the conductor, Pi and MCP all live on it. The reader must not block the
  loop. Use `asyncio.StreamReader` over stdin via `loop.connect_read_pipe`; on Windows, a
  thread doing `sys.stdin.readline` with `call_soon_threadsafe` is simpler and robust.
- **Questions arrive mid-turn,** from inside the watched task, and can nest (approval while the
  coder runs). Key them by id.
- **Cancellation:** `Repl.turn` already turns a cancelled task into a Notice and keeps finished
  work (`repl.py:1221-1222`). The coder records its checkpoint on cancel. Cancelling while an
  `ask` is pending must cancel the future (the TUI does this through `_answer`).
- **The 5-minute approval timeout** exists only in `_Questions` (`repl.py:397-422`). Implement
  your own, defaulting to "no".
- **Mutating state mid-turn** (permission, model): the TUI only allows it at the prompt.
  Prefer idle-only.
- **SIGINT:** do not install `_sigint_cancels_main`. The desktop app sends `cancel`.
- **Stdout hygiene:** see §0. Also `rich` and `prompt_toolkit` are imported lazily, only by
  `RichFrontend` (`repl.py:715-733`), so the bridge never needs a console.
- **Windows:** `/serve` children are killed with `taskkill /T`. Make sure `App.close()` runs
  (and handle the desktop app killing the bridge: atexit `stop_servers` also runs,
  `commands_forge.py:286`).
- **Secrets:** never echo `ask_secret` answers in logs. Error paths use fixed codes
  (`session.codes.code_for`).

**Alternatives considered:**
- *Wrap only `Conductor.turn` with a custom `UI`.* This is simpler but loses slash commands,
  `@attach`, checkpoints (no `checkpoint_key`), auto-compact and save-after-turn. You would
  re-implement `Repl.turn` (`repl.py:1204-1235`). Not worth it.
- *Drive `architect --plain` through a PTY and parse the text.* This is fragile: the output is
  formatted plain text, not structured, and approvals are free-form prompts.
- *Extend server.py with chat endpoints plus SSE.* That is heavier. The server is a deliberately
  narrow, hardened control plane (5 ops, fixed codes, `server.py:1-16`), so adding a chat surface
  there cuts against its design. A stdio child process also inherits the user's environment and
  logins naturally.
- *`-p --json` per turn.* It works for one-shots (resume with `-r <session>`), but approvals are
  fixed to all-no unless full-auto (`repl.py:1359`), there is no streaming, and every turn is a
  cold boot.

---

## 5. Tests and the free fake provider

### 5.1 Running tests (from `.jakeos/index.md` gotchas)

- Windows/Git Bash: pass `--basetemp C:/t/<label>` (forward slashes; backslashes create stray
  repo dirs), and use `--color=no`. Registry paths are length-limited, so keep workspaces short.
- Agent subset (verified here: **107 passed in 6.4 s**):
  ```
  uv run pytest tests/test_agent_conductor.py tests/test_agent_repl.py \
    tests/test_agent_native.py tests/test_agent_cli13.py -q --color=no -p no:cacheprovider \
    --basetemp C:/t/discagent
  ```
  Other agent tests: `test_agent_build.py`, `test_agent_coder.py`, `test_agent_forge_mode.py`,
  `test_agent_commands_providers.py`, `test_mcp.py`, `test_pi_protocol.py`, `test_server.py`.
- Full suite, resumable: `uv run python scripts/run_tests_resumable.py --run-id <id>`, then
  `--resume <id>` (`scripts/run_tests_resumable.py:1-10`). pytest config: `asyncio_mode =
  "auto"`, `testpaths = ["tests"]` (`pyproject.toml:49-51`). Lint and types: `ruff`,
  `mypy --strict` (`pyproject.toml:53-63`).
- `tests/conftest.py:9-11` autouse-sets `ARCHITECT_SIGNING_KEY_FILE` to a tmp key. Tests never
  call live providers.
- The bench's free gate is `architect bench run --provider fake --tier smoke --seed 7`. That is
  for the bench, not the agent CLI.

### 5.2 How tests drive the agent (patterns to copy for bridge tests)

- **Fake profile:** `tests/session_support.py:50-101` `fake_profile(tmp_path)` writes
  `<tmp>/w/session-profile.json` with `models: {"fake": {"provider":"fake",
  "fixture":"responses.json"}}`, a fake `inference`, a held-out `tasks` floor, and
  `compile.max_candidates=1`.
- **Scripted conductor model:** `tests/test_agent_conductor.py:19-41`. `Scripted(*replies)` has
  `quote()` and `complete()`, returns envelope strings in order (default `{"text":"done"}`),
  records prompts, and costs $0.001 per call. `calls(("read_file", {...}), ...)` builds a
  tool_calls envelope.
- **Injecting it:** `boot(AppOptions(cwd=tmp, workspace=tmp/"w"), frontend, adapter=Scripted(...))`.
  The `adapter=` parameter replaces the conductor's model, and it is still metered
  (`app.py:374-375`). For the REPL: `Repl(app, LineFrontend(ScriptedConsole(lines)))`, then
  `await repl.run()`, then `console.transcript()` (`tests/test_agent_repl.py:46-71`).
  `main(argv, adapter=..., stdin=, stdout=, stderr=)` also accepts an injected adapter
  (`repl.py:1508-1515`). Bridge tests can pass a `Scripted` the same way.
- **Streaming:** `tests/test_agent_native.py:69-110` has a fake `Streaming` adapter with
  `quote_chat` and `stream_chat` yielding `TextDeltaEvent` / `ToolCallEvent` / `UsageEvent` /
  `DoneEvent`. Use it to test `TextDelta` forwarding.
- **Approvals:** `RecordingUI(approvals=["yes","session"], default_approval="no")` for the
  conductor. `tests/test_agent_coder.py:51+` has an `Approvals` recording approver on
  `PiBackend(approver=..., on_event=...)`. Some tests use the real Node bridge.
- **Providers:** `conductor.extras["providers_host"] = Host(transport=httpx.MockTransport(...),
  which=..., env=..., runner=...)` (`commands_providers.py:74-76`).
- **Free fake provider for manual runs:** there is no `--provider fake` flag on the agent CLI.
  Instead, point `--profile` at a profile with a `fake` model whose `fixture` is a JSON array
  of `ModelResponse` objects whose `text` is an envelope (e.g. `{"text": "{\"text\":
  \"hello\"}", "tokens_in":1, "tokens_out":1, "cost_usd":0.0, "model":"fake"}`). With no
  fixture, `FakeAdapter` answers `"ok"` (`models/fake.py:36-60`). The conductor treats that as
  prose and so as a final answer. That gives a $0, offline desktop dev loop:
  `architect desktop-bridge --profile <fake.json> --workspace C:/t/ws`. The coder still needs
  Node plus `npm ci` in `architect/backends/pi-node` (`/doctor` checks this,
  `commands.py:1178-1186`).

---

## 6. Loose ends and doc/code discrepancies

- Phase 12 doc says the session default is $5. The code is **$25** (`conductor.py:106`,
  `config.py:78`).
- Phase 12 says `/model` sets the conductor and the next compile. Phase 13 replaced it with the
  picker in `commands_providers.py`. The `commands.py:668-703` version is dead (overridden).
- Phase 12 §8 says the prompt is inactive during a turn. That holds, but Phase 13 queues the
  typing.
- `supports_images()` returns False for every native-streaming adapter. Images work only via
  CLI adapters or a priced image entry on the envelope path (`conductor.py:148-163`). HEAD
  commit `b5f8f02` "Drop unverified bundled prices" may narrow which models have image
  ceilings. Check `architect/models/prices.json`.
- The coder uses the **conductor's** model (`ctx.model`), and `/model` keeps
  `conductor.context.model` in sync (`commands_providers.py:160-161`).
- No turn-level events exist. The bridge must synthesize busy/idle/state.
- `Repl` reads some frontend attributes duck-typed: `frontend.renderer.glyphs`, `ask_secret`,
  `close`. `RichFrontend`-only hooks (`status_suffix`, `queue_lines`) are not needed.
