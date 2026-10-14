# Architect desktop: plan

Built on `docs/discovery.md`. The owner asked for the whole brief end to end without a
check-in, so the open questions at the end were not put to them first; each one records
the assumption that was used instead, and the README points at this list.

## 1. Stack

Hearth's stack, unchanged where there was no reason to change it:

| Concern | Choice | Why |
|---|---|---|
| Shell | Electron 33 | Hearth's; Tauri is unavailable here (no Rust toolchain) and Hearth abandoned its Tauri shell |
| UI | React 18 + TypeScript, Vite 6 | Hearth's |
| State | zustand 5, split by concern (not one 3.7k-line store) | Hearth's library; its single store was the one thing discovery said not to copy |
| Styling | Plain CSS with custom properties | Hearth's approach; the values are Lectern's campus tokens, copied verbatim |
| Transport | Loopback HTTP + one WebSocket (`/api/ws`), origin-guarded | Hearth's architecture; the same server runs as a Vite plugin, so `npm run dev` is the whole app in a browser (used for testing) |
| Agent | **Architect's own core, unmodified in behaviour**, one Python child process per conversation | See §2 |
| Tests | vitest (+ jsdom for components); pytest for the Python bridge | Hearth's and Architect's |

## 2. Connecting to Architect's core

Reuse, not reimplementation. The core already separates itself from its terminal: the
conductor emits events to a `UI`, and `Repl` (every slash command, `@path`, `!shell`,
checkpoints, auto-compact, the queue, saving) runs over a `Frontend` protocol.

```
React page --WebSocket--> loopback server (Electron main, or Vite in dev)
                              | one child per open conversation
                              v
                 python -m architect.desktop.bridge     (JSON lines over stdio)
                              |
             DesktopFrontend (a Frontend) + Repl(app, frontend) from architect/agent
```

- `architect/desktop/bridge.py` is the third Frontend. Input goes through `Repl.handle`,
  so plain text is a turn, `/x` a slash command, `!x` a shell command, exactly as in the
  terminal. `ask`/`approve`/`ask_secret` become `ask` messages the page answers; the
  terminal's rules are kept (unclear is no; five minutes without an answer is no).
- The bridge takes file descriptors 0 and 1 for itself and points them at an empty input
  and stderr, so `codex login`, `/bench` and stray prints cannot corrupt the protocol;
  Python-level prints during a command are shown in the conversation as output.
- Structured reads the UI needs that the terminal formats as text get small bridge
  methods that call the same Python functions the commands call (provider status, model
  rows, sessions, registry entries one by one, an agent's spec/report/design/history,
  config values, MCP servers, presets).
- Two additive hooks in the core, both no-ops unless something subscribes:
  `compiler` stage observers (stage number, model k of n) and Forge tool result observers
  (the tuning measurement, per-case results, submissions, the `left` budget). They exist
  because forge.md §6 found those numbers never leave the tool callbacks. Nothing they
  carry comes from the held-out partition.
- One process per conversation gives parallel conversations for free and isolates a crash
  to its tab. Processes survive a page reload; the server replays their recent messages.

## 3. Feature parity

Every CLI capability and where it lives in the desktop app. "Bridge" means it runs through
the same `Repl` code path as the terminal.

### 3.1 Launch flags

| CLI | Desktop |
|---|---|
| `architect` (interactive) | The app; each conversation is its own bridge process |
| `-c, --continue` | Opening the app restores the open conversations; the sidebar lists all saved ones |
| `-r, --resume ID` | Click a conversation in the sidebar (bridge `resume`) |
| `--model NAME` | Model menu in the composer bar; default in Settings > Models |
| `--workspace PATH` | Settings > Advanced > Workspace folder (applies to new conversations) |
| `--profile PATH` | Settings > Advanced > Profile file |
| `--permission MODE` | Permission menu in the composer bar; default in Settings > Permissions |
| `--no-probe` | Settings > Advanced (first-run login check) |
| `--plain` | Not applicable (no terminal); the conversation density setting covers the intent |
| `-p TASK [--json]` | Not a window feature: the CLI keeps it for scripts. The desktop's equivalent is a normal message |

### 3.2 Input syntax and keys

| CLI | Desktop |
|---|---|
| Enter sends, Ctrl+J / trailing `\` newline | Enter sends, Shift+Enter newline |
| `/command` with fuzzy Tab menu and descriptions | `/` opens the same menu (from the bridge's command list, custom commands included), arrow keys + Enter |
| `@path` attach with completion | `@` opens file completion (bridge `mentions`); also drag files onto the composer |
| `@image.png`, `/image` | Drag or paste an image, or the attach button (bridge `attach_image`, any location) |
| `!command` | Same, in the composer |
| `//text` sends literal slash | Same |
| Shift+Tab / Alt+M cycles normal, forge, plan | Shift+Tab in the composer; also the mode switch in the composer bar |
| Esc / Ctrl+C during a turn cancels | Esc, or the stop button |
| Typing during a turn queues | Same; queued messages show above the composer and can be pulled back to edit |
| Up / Ctrl+R history | Up in an empty composer recalls the last message |
| Ctrl+G external editor | The composer is a full multi-line editor; not needed |
| 12+ line paste collapses | Not needed: the composer scrolls |
| Ctrl+D / `/exit` | Close the conversation (Ctrl+W); quitting the app saves everything |

### 3.3 Slash commands

All 41 run unchanged through the bridge, so typing any of them works exactly as in the
terminal. The table says where each one also has a direct control.

| Command | Direct control in the desktop |
|---|---|
| `/help [cmd]` | `/` menu with every command and its description; Mod+/ lists shortcuts |
| `/mode [normal\|forge]` | Mode switch in the composer bar; the Forge workspace is forge mode |
| `/make <desc>` | Forge workspace: describe what to build |
| `/forge <desc>`, `/forge arc` | Forge workspace "Engineer" setting (Forge or preset only); ARC from the Forge workspace's new-agent menu |
| `/spawn <preset> <task>` | Settings > Presets lists them; `/spawn` in the composer |
| `/agents` | Agents screen (every installed agent, one row each, failures shown per row) |
| `/run <agent> [input]` | Agent page: "Try it" form built from the input schema |
| `/report <agent>` | Agent page: Overview and Details |
| `/recompile <agent>` | Agent page: Rebuild (asks with the cost bound, as the CLI does) |
| `/library` | Agents screen > Harness library |
| `/floor ...` | Forge workspace > Build settings: quality bar, case file |
| `/budget session\|run\|compile` | Composer bar budget popover (session); Forge Build settings (run, compile, Forge) |
| `/model ...`, `/models`, `/providers` | Model menu (composer bar), Settings > Models and sign-in |
| `/login [provider]`, `/logout` | Settings > Models and sign-in (and first run) |
| `/telemetry` | Agents screen > Usage |
| `/permissions [mode]` | Permission menu in the composer bar |
| `/status` | Conversation menu > Details |
| `/cost` | Spend shown in the composer bar; click for the breakdown |
| `/compact` | Context meter popover > Compact |
| `/clear` | New conversation (Mod+N) |
| `/resume`, `/sessions` | Sidebar |
| `/init` | Shown as a one-click suggestion when a folder has no ARCHITECT.md |
| `/bench <verb>` | `/bench` in the composer; output shows in the conversation |
| `/exit`, `/quit` | Close conversation / quit |
| `/context` | Context meter popover |
| `/diff` | Changes button in the conversation header (appears once files changed) |
| `/undo` | "Undo these changes" on a turn's file-change card (asks, as the CLI does) |
| `/config [set k v]` | Settings (each key is a control) |
| `/doctor` | Settings > Health check |
| `/memory [edit [global]]` | Settings > Instructions (edits ARCHITECT.md in place) |
| `/export [file]` | Conversation menu > Export as Markdown |
| `/export <agent> [path]` | Agent page: Put into software |
| `/copy` | Copy button on every answer |
| `/review [focus]` | Changes view: Review these changes |
| `/title [name]` | Rename in the conversation header |
| `/image <path>` | Attach button / drag and drop |
| `/serve [port\|stop]` | Agents screen > Local service |
| `/mcp ...` | Settings > Tools (MCP servers) |
| custom `/name` | Appear in the `/` menu; Settings > Instructions lists them |

### 3.4 Agent behaviour

| Capability | Desktop |
|---|---|
| Streaming answers | Streamed text with Lectern's solid caret |
| Tool calls | One fold per turn ("Used 4 tools in 12s") that is open while running; each step shows its result |
| Coder subagent activity | Lines under the coder's step |
| Approvals (write, edit, bash, MCP, export) | Inline card: Allow once / Allow for this conversation / Don't allow; Enter allows once, Esc declines; the card stays as a record |
| Questions (interview, plan card yes/no, money consent, undo) | Inline card with choices or a field; Enter takes the default |
| Secrets (API keys) | Password field; never shown back, never in the conversation |
| Plan card | Card with its rows; the Forge workspace shows it in the panel too |
| Notices | Quiet line (info) or a marked line (warning, error) saying what to do next |
| Context meter, auto-compact | Composer bar meter; notice when it compacts |
| Spend vs session budget, time | Composer bar |
| Checkpoints and undo | Per-turn change card with Undo |
| Images | As the CLI: sent only to models that accept them, otherwise a warning |
| Sessions saved after every turn | Same (the bridge saves); drafts are saved by the app |
| Project instructions | Picked up from the folder exactly as the CLI does |

## 4. The Forge workspace

The owner's direction (mid-build): Forge is the chat on the left and a panel on the right.
The panel shows the agent being built as a product, like a browser on it, or switches to a
technical view of its tools and harness. The interface should be as simple or as
technical as the person needs.

- **Left: the conversation.** A conversation in forge mode. The interview, plan card,
  approvals and progress happen here exactly as in the CLI. It stays agent-like: the
  person talks to Architect, Architect builds.
- **Right: the panel, two views.**
  - **Agent** (the default, plain): the agent as a person would meet it. Its name and what
    it does; while building, where it is (asking, planning, building, checking, ready)
    and the quality bar it has to clear; once licensed, a "Try it" form generated from its
    input schema with the output beside it, plus Put into software and Use from code.
  - **Details** (technical): the Spec with its full hash and an explicit Approve; build
    settings (quality bar, case file, run/compile/Forge budgets, engineer Forge or preset,
    model, name); the stage track 0-4 with "model k of n"; a chart of every tuning score
    against the floor line, with tuning evidence drawn differently from the Stage 4
    result; the Forge board (checks, dry runs, measures, submissions against their caps,
    the remaining budget); the per-case grid (tuning only); the harness design, files,
    tools, sub-harnesses; costs split into build, compile and run.
- **Agents screen** for everything already built: one row per agent with its status
  (licensed, failed check), revisions and rollback, report, rebuild, export, local
  service, library.
- **Settings > Presets and roles**: the eight preset harnesses (components, tunable
  parameters, defaults) and Architect's own roles (which model does inference, rewrite,
  Forge agent, floor drafting; Forge mode, budget and turns; routing policy), editable
  where the profile allows it.

## 5. Main flows

1. **First launch.** A window with one question at a time: sign in (detected options
   first; the recommendation is preselected: Claude CLI, ChatGPT through Codex, an API
   key, a local server), then choose a model, then choose a folder. Each step is the CLI's
   own first-run flow rendered as a form. Errors say what failed and what to do.
2. **Everyday.** Open the app: the last conversations are restored, drafts included.
   Mod+N starts a conversation in the current folder; type; watch the fold; answer an
   approval with Enter; see the change card; undo if needed.
3. **Build an agent.** Forge (Mod+B, or switch the mode): describe it, answer the
   interview, read the plan in the panel, approve, watch the build, try it, put it into
   software.
4. **Change model or sign-in later.** Model menu in the composer bar (current
   conversation); Settings > Models and sign-in (default and accounts).
5. **Parallel work.** Several conversations run at once, each its own process; the
   sidebar marks which are working and which are waiting for an answer.

States designed for every surface: empty, loading (Lectern's working mark only while a
request is open), error (what happened, what to do), offline (the page lost its server:
says so and reconnects), signed out (sign-in prompt), long-running (elapsed time, stop).

## 6. Open questions and the assumptions used

1. **Which Hermes?** Five products carry the name (`docs/discovery/reference-apps.md` §0).
   Assumed: Nous Research's Hermes Desktop.
2. **Editable presets.** The eight presets are a frozen set in code
   (`architect/presets/__init__.py`, "the frozen launch set"), and the licensing story
   depends on them being fixed. Assumed: Settings shows them read-only, and "modify" is
   applied to what is genuinely configurable (role models, Forge settings, routing policy
   in the profile). Making presets editable would be a core change for the owner to
   decide.
3. **Core hooks.** Two additive observer hooks were added to the compiler and the Forge
   tools for the live Forge board (§2). Assumed acceptable since they change no behaviour.
4. **Packaging.** Assumed: run from this checkout (`npm run app`), using the checkout's
   `.venv`. An installer that bundles Python was not asked for and is not built.
5. **Light and dark.** Lectern's campus surface defaults to dark. Assumed: follow the
   system, with a switch in Settings.
6. **Space Grotesk** is Lectern's caption face, used only for small labels above headings,
   which the brief bans. Assumed: loaded with the others, used only for table column
   headings.
7. **Codex sign-in inside the app.** The CLI runs `codex login` with the terminal; in the
   app it runs as a child process and the browser opens. Assumed acceptable; the app
   shows the sign-in link too in case the browser does not open.
8. **Visual language.** The brief names Lectern for type, colour and the loader and asks
   for the result to be "its own thing". Assumed: Lectern's tokens exactly, Architect's own
   layout; the loader keeps Lectern's mark geometry since the brief says to use Lectern's
   animation.

## 7. Status (2026-10-01)

Built as planned, without a check-in (the owner asked for the brief end to end). Code:
`desktop/` (Electron + React), `architect/desktop/` (the bridge: `bridge.py`, `data.py`),
two observer hooks in `architect/compiler/__init__.py` and `compiler/forge_tools.py`.

### How each part was verified

| Area | Evidence |
|---|---|
| Bridge protocol, approvals, questions, cancel, queue, modes, sessions | `tests/test_desktop_bridge.py`, `tests/test_desktop_bridge_more.py` (32 tests) |
| A whole agent build through the bridge: interview, plan, approval, stages 1-4, licensed install, then list, read and run the agent | `tests/test_desktop_bridge_forge.py` (runs `tests/desktop_build_driver.py` in its own process) |
| Page to server to real Python bridge: boot, a turn, a slash command, reload replay, close, origin guard, prefs | `desktop/tests/hub.test.ts` |
| Conversation and Forge models, plain words | `desktop/tests/timeline.test.ts`, `desktop/tests/forge.test.ts` |
| Main flows in the page: approvals by keyboard, send and queue, stop, Shift+Tab, `/` menu, first-run sign-in, a refused key, the Forge panel's approve | `desktop/tests/flows.test.tsx` |
| Electron starts, serves and mounts the page | `ARCHITECT_DESKTOP_SMOKE=1 npx electron .` printed `{"smoke":"ok",...,"mounted":true}` |
| Live providers | Through the bridge on this machine: Claude (claude CLI) sign-in check, model choice, boot and an answer; then a switch to ChatGPT through Codex (`gpt-5.6-terra`) and an answer. $0.03 API-equivalent. |
| Every screen against the real core | Browser session on `npm run dev` with a fake profile: conversation, tool fold, Settings (all panes; Health check runs `/doctor`), Agents, Forge with a recorded build replayed into the panel; dark and light |

Not verified live: a real `codex login` browser sign-in (this machine was already signed
in, and signing out to test it would have disturbed the owner's login); API-key sign-in
against a real provider (no keys here; the key path is covered by the bridge tests with
a mocked HTTP transport); a Forge build on a paid model (the offline build test covers
the whole path with the fake provider).

### Design rules: the final pass

Checked across `desktop/src`: no status dots or decorative bullets (answer lists use a
dash; the CLI's `·` glyph is replaced in the bridge's glyph set), no eyebrow labels, no
pulsing or animated dots, Lectern's tokens copied verbatim (`styles/tokens.css`), its
fonts, and its working mark as the only loader (`components/ui/Working.tsx`,
`styles/motion.css`). Exceptions, and why:

- **Lectern's shimmer** on the current step's label runs while a request is open. It is
  part of Lectern's own AI working state (`.chat-shimmer`), not a pulse.
- **Three native tooltips** carry facts the screen does not otherwise show: the full
  folder path, what the spend figure is, and each Forge case's result.
- **CLI wording inside command output** (`/status`, `/doctor`, plan-card values such as
  `held_out_tasks >= 0.90`) is shown as the core writes it, so the app and the terminal
  never disagree; the Agent view restates the important parts in plain words.
- **Space Grotesk** is loaded with Lectern's other faces but unused, since its only role
  in Lectern is the eyebrow.

### Assumptions added while building

- Approving a build stays the CLI's question ("Compile this agent?", bound to that spec);
  the app shows the spec's 8-character hash the core reports. The full hash is shown for
  installed agents.
- `set_role` copies a profile model's own entry (key references included) into the
  role, rather than the app writing provider settings itself.
- A `capture` bridge method runs a slash command and returns its output as data, for the
  Agents screen (library, usage, local service) and the Health check.
- Desktop prefs and drafts live in `~/.architect/desktop/prefs.json`; the window uses a
  steady loopback port (47817, any free port if taken) so its storage survives restarts.
