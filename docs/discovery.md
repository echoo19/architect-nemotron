# Desktop app discovery

Phase 1 of the desktop app brief (2026-10-01). Five parallel read-throughs, each written in
full under `docs/discovery/` with `path:line` citations. This page is the summary the plan
(`docs/plan.md`) is built on. Where a doc and the code disagree, the code was taken as the
truth; the disagreements found are listed at the end.

| Report | Covers |
|---|---|
| [discovery/architect-cli.md](discovery/architect-cli.md) | Every command, flag, mode, config key, key binding; the agent loop, tools, coder, approvals, sessions, checkpoints, images, `/serve`; providers and auth including Codex; the UI seam |
| [discovery/forge.md](discovery/forge.md) | Forge end to end: Spec, interview, stages 0-4, the Forge engine and its tools, delivery modes, artifacts and registry on disk, router, versions, what a UI can show live |
| [discovery/hearth.md](discovery/hearth.md) | Hearth's stack, architecture, UI-to-agent transport, layout, interaction patterns, components, tests; what to reuse |
| [discovery/reference-apps.md](discovery/reference-apps.md) | Codex, Claude Code desktop and Hermes desktop: layout, parallel work, tool calls, diffs, approvals, onboarding, models, keys |

## 1. Architect

**What it is.** A terminal agent that builds agents (`architect/agent/repl.py` banner). A
conductor model talks with the user and delegates: file edits go to a coder subagent (Pi,
a Node process, `architect/agent/coder.py`), structured jobs go to preset subagents
(`spawn`), and "make an agent" requests go to build mode, which turns a request into a
tested, licensed harness through the compiler (`architect/agent/build.py`). Nothing else in
the generic-coding-agent space licenses its output: only Stage 4 verification on held-out
cases licenses a harness, with an HMAC-signed report (`architect/_authority.py`,
`compiler/verification.py`).

**Commands.** 41 slash commands (full table in architect-cli.md §1.6), `!shell`, `@path`
and `@image` attachments, custom commands from `.architect/commands/*.md`, and the flags
`-p --json -c -r --model --workspace --profile --permission --no-probe --plain`
(`repl.py:1397-1417`). The classic subcommands (`compile`, `do`, `create`, `run`,
`recompile`, `route`, `roles`, `status`, `registry`, `export verify`, `bench ...`,
`forge report|run|integrate`, `serve`, `session`) are for scripts (`cli.py:63-376`).

**Modes.** Two independent switches that the terminal cycles together with Shift+Tab:
`Conductor.mode` (`normal` or `build`, shown as "forge") and the permission (`ask`,
`auto-edit`, `full-auto`, `plan`). "Plan" is permission `plan` in normal mode
(`repl.py:175-198`, `render.py:123-127`).

**Approvals.** Exactly three callers of `UI.approve`: the coder's `write`, `edit` and
`bash` (after Pi's own guards, `coder.py:165-179`), MCP tools (`mcp/manager.py:339-358`),
and exporting an agent into a repo (`agent/export.py:417`). Answers are yes, session, or
no; anything unclear is no; the terminal treats five minutes without an answer as no
(`repl.py:397-422`). Other confirmations (plan card, money over $0.50, `/undo`, ARC) are
ordinary questions.

**The seam.** The conductor never prints. It emits nine event types and asks a `UI`
(`architect/agent/events.py`). `Repl` (slash commands, attachments, checkpoints,
auto-compact, the message queue, saving) runs over a `Frontend` protocol
(`repl.py:364-375`). Everything a command shows goes through four renderer calls
(`show`, `text`, `rows`, `diff`). A third Frontend therefore gets every feature with no
reimplementation. There are no events for turn start or end, mode, permission, usage or
context fill; those are read from `Repl._toolbar_values` and the `Meter`.

**State on disk.** Workspace `~/.architect` (or `--workspace`, `ARCHITECT_WORKSPACE`):
`session-profile.json`, `config.toml`, `credentials.json`, `sessions/<id>.jsonl` +
`index.json`, `checkpoints/`, `registry/`, `artifacts/`, `floors/`, `library/`, `routes/`,
`exports/`, `telemetry/`, `serve/` (architect-cli.md §3.4, forge.md §3.3).

**Providers and auth.** 16 providers (`providers/catalog.py:84-165`): the `claude` CLI,
ChatGPT through the Codex CLI, nine hosted APIs, four local servers and a custom URL.
Codex auth is the official `codex login` browser OAuth flow run as a child process;
Architect only checks that `~/.codex/auth.json` exists, never reads it, and strips
`OPENAI_API_KEY` when calling `codex exec` so the saved login is used
(`providers/signin.py:147-170`, `providers/status.py:51-55`, `models/cli.py:439-500`).
API keys come from the environment, else `<workspace>/credentials.json`, verified with one
model-list call before they are stored (`signin.py:173-210`). First run is
`providers.signin.first_run`: detected providers, one recommendation, one keypress
(`app.py:457-468`).

**Streaming.** Only the HTTP adapters (Anthropic, OpenAI-compatible, Ollama) stream
`TextDelta`; the claude and codex CLIs return whole answers (`native.py`). Images reach the
model only on the non-streaming path with a priced image entry (`conductor.py:148-163`).

**Stdout gotcha.** `codex login`, `/bench` and `/memory edit` use the process's own stdio
(`providers/host.py:23-32`, `commands.py:862`, `:1238`). A stdio bridge must move its
protocol off file descriptor 1.

## 2. Forge

"Forge" names three things the CLI blurs (forge.md §0): the Stage 3 engine that writes
`harness.py` in sandbox language v2 through five tools (`compiler/forge.py`,
`forge_tools.py`); the delivery modes `forge run` (ephemeral) and `forge integrate`
(export into a repo); and the CLI's forge mode (internally `build`), which is what an
operator uses.

**One build in forge mode** (`build.py:1534-1648`): an interview (destination first: run
it here or put it into software; then at most five uncertain fields), Stage 0 infers a
Spec, a floor is loaded or 12 SYNTHETIC cases are drafted (6 tuning, 6 held-out), blockers
are checked, a plan card is shown and "Compile this agent?" (yes approves that exact Spec,
`spec <hash8>` in the footer), then compile: Stage 1 select a preset, Stage 2 tune, Stage 3
Forge (forced on in build mode), Stage 4 verify and sign. Stages 1-4 repeat for each model
on the ladder when the model is "cheapest that passes". Then install (with a separate yes
for a higher per-run budget) and the destination (an export needs its own approval).

**Live data.** The `UI` protocol already carries the interview, plan card, stage progress
(`Progress("stage N name: calls, $")`, `build.py:1259-1267`), Forge tool activity
(`SubagentActivity("forge", ...)`), approvals and the result card, but as preformatted
text. Two gaps: no structured stage signal, and the Forge tools' results (tuning score vs
floor, per-case pass or fail, accepted and best submissions, remaining budget) never leave
the tool callbacks (`forge_tools.py:774-1010`); the CLI shows `forge_measure` with an empty
summary (`bridge.mjs:117-132`). Held-out content is unreachable by design and must never be
shown during a build.

**After a build.** Installed agents live in the registry (`Registry.list/status/history/
rollback/drift`); artifacts are folders with `harness.py`, `spec.json`, `report.json`,
`forge.json`; the harness states its design in its module docstring (`tools.py:373-381`).
`/agents`, `/report`, `/run`, `/recompile`, `/export`, `/serve`, `/library` reach them; there
is no CLI command for history or rollback, and `Registry.list()` fails as a whole when one
entry fails its license check (`registry/__init__.py:406-412`).

**What the CLI makes hard** (forge.md §7, 19 items). The ones the desktop can fix without
touching core behaviour: make the engineer choice (Forge or preset only) and the Forge
budget settable (`set_override` supports them, no command does); show the Spec with its
full hash next to an explicit approve button; separate tuning evidence from the license
everywhere; show the case partition so a reader sees "6 held-out, one miss fails";
explain "licensed but not installed"; one budget view instead of four numbers; a tolerant,
per-row agent list with history; a plain-language reuse card instead of the router table.

## 3. Hearth

Electron 33 + React 18 + TypeScript + Vite 6, zustand 5 for state, plain CSS custom
properties, vitest with jsdom, electron-builder for packaging (hearth.md §2). Electron main
holds no agent logic: it starts a loopback HTTP server on `127.0.0.1` and loads the window
from it; one multiplexed WebSocket at `/api/ws` carries every chat; the same server runs as
a Vite plugin in dev, so the app also works in a plain browser (hearth.md §4). An origin
guard refuses non-loopback and cross-port origins because WebSocket ignores CORS
(`server/originGuard.ts`). Each provider's output is normalized into one event vocabulary,
folded into message parts. Patterns worth keeping: approvals inline in the transcript that
grab Enter and Escape and collapse into a past-tense record; a file-change card with
+n/-n; a working row whose label follows what the agent is doing; Enter queues while a turn
runs; Mod+K/N/, shortcuts with platform-correct labels; settings as a modal with panes.
Not reusable: everything game or brand specific, the dark-only ember palette (structure
yes, values no), and the 3.7k-line store as a whole. On Windows, Hearth's `app:dev` and
`app:dist` scripts use POSIX inline env vars and fail under cmd.exe.

## 4. Lectern

The desktop app uses Lectern's design tokens, motion styles, mark and working
loader, contributed under the MIT License (see THIRD_PARTY_NOTICES.md). The
detailed notes on Lectern's private source are not part of the public repository.

## 5. Reference apps

**Claude Code desktop** (verified from official docs): sessions in a left sidebar; before
the first message, pickers for folder, model and permission mode beside the composer; tool
calls collapsed into summaries by default with more verbose views one key away; a `+12 -1`
diff chip opens a diff viewer; the permission selector sits beside send; Esc stops; typing
while busy queues; side-by-side sessions; a usage ring for context.

**Hermes desktop** (Nous Research; medium confidence): chat-first with a right-hand
preview rail and pane; a Simple vs Advanced interface mode that collapses technical
detail; a subagents frame above the composer; error cards that name the failing layer
(provider, auth, network) with a recovery action; a context meter with a breakdown; drafts
that survive a lost session; Mod+K palette.

**Codex app** (low confidence: official docs moved and returned headings only): project
threads on the left, one worktree per agent, a review pane for diffs with inline comments,
an approval toggle in the composer.

"Hermes desktop app" could mean five products (reference-apps.md §0). Nous Research's
Hermes Desktop was researched as the most likely; this is recorded as an assumption in the
plan.

## 6. Disagreements found between docs and code

- Conversation budget default: $25 / 60 min in code (`conductor.py:106`, `config.py:78`);
  the Phase 12 doc says $5.
- Compile wallet: $10 / 120 s in the package defaults, $5 / 1800 s in a first-run profile.
- `/model`: Phase 12 describes `commands.py:668-703`, which Phase 13's provider picker
  (`commands_providers.py:346`) overrides.
- Stage 3's label is "rewrite", "forge" or "stage 3 rewriting" depending on the surface.
- Hearth's DESIGN.md names fonts the code no longer uses (Archivo, IBM Plex Mono).
