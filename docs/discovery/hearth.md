# Hearth discovery (echoo19/hearth @ 34124e53, v1.9.0)

Clone: `{scratch}\hearth-ref`. All paths below are relative to it. Read-only, nothing modified.

## 1. What it is
Desktop app: sidebar of projects/chats, an agent conversation in the middle, the user's game in an iframe pane on the right, plus a "tester" bot. The coding agent is BYO: Claude via the Anthropic Agent SDK (which runs the Claude Code CLI on the user's own sign-in), ChatGPT via `codex app-server`, or any CLI in an embedded PTY terminal (README.md, PRODUCT.md, docs/architecture.md).

## 2. Tech stack (apps/editor/package.json)
| Concern | Choice |
|---|---|
| Shell | **Electron 33.4.11**. There is a Tauri v2 shell in `apps/editor/src-tauri/`, marked "experimental, not built or shipped" (src-tauri/README.md) |
| UI | React 18.3 + TypeScript 5.6, no UI kit, all components hand-rolled |
| Bundler | Vite 6 (`@vitejs/plugin-react`) for the renderer; esbuild 0.24 bundles Electron main into `dist-electron/main.cjs` (scripts/build-electron.mjs) |
| State | **zustand 5**, one big store: `src/store.ts` (3758 lines; `useApp = create<AppState>` at l.1597, `useEditor` alias) |
| Validation | zod 4 (server request bodies) |
| Styling | Plain CSS with custom properties: `src/styles/tokens.css`, `primitives/*.css`, `app/*.css`, `panels/*.css`. OKLCH palette. **Dark only** (no prefers-color-scheme or light theme) |
| Fonts | `@fontsource-variable/mona-sans` (wght axis) + `@fontsource/commit-mono` 400/500/600 (`src/styles/fonts.css`). DESIGN.md is out of date here: it still says Archivo + IBM Plex Mono |
| Terminal | `@xterm/xterm` 6 + addon-fit, `@lydell/node-pty` |
| Agent | `@anthropic-ai/claude-agent-sdk ^0.3.220`; Codex CLI over stdio JSON-RPC |
| Transport | `ws` 8 (one multiplexed WebSocket) |
| Updates | electron-updater 6, GitHub Releases (`electron/updater.ts`, `updaterPolicy.ts`) |
| Packaging | electron-builder 25: nsis+zip (win), dmg+zip (mac, notarized), AppImage+deb |
| Tests | vitest 3 (root `vitest.config.ts`, node env, jsdom per file via `// @vitest-environment jsdom`), @testing-library/react 16, jsdom 25. No Playwright in the app. `playwright-core` is an optional dependency of `packages/adapter-web` only, used for the game probe |

## 3. Monorepo layout
npm workspaces (`package.json`: `packages/*`, `apps/*`), Node >= 20.
- `apps/editor`: the desktop app.
  - `electron/`: `main.ts` (680 lines), `preload.cjs`, `appMenu.ts`, `updater.ts`
  - `server/`: Node HTTP + WS backend. It runs inside Electron main and also as a Vite plugin in dev (`chat.ts`, `ws.ts`, `projectServer.ts`, `chatStore.ts`, `chatDrivers/codex.ts`, `claudeAuth.ts`, `claudeModels.ts`, `modelPrefs.ts`, `permissionMode.ts`, `ptyManager.ts`, `shellEnv.ts`, `originGuard.ts`, `skills.ts`, `usage.ts`, `devTeam*.ts`, `tester/`)
  - `src/`: React renderer
  - `tests/`: about 120 vitest files
- `packages/`: core, runtime, playtest, probe-core, probe-tools, adapter-web, shipping, templates, cli, mcp-server, examples. These are mostly game/probe specific.

## 4. Main process, backend and renderer
Source: docs/architecture.md, `electron/main.ts`.
- **No agent logic in IPC.** Electron main calls `startServer()` (main.ts:89-130), which serves `http.createServer` on `127.0.0.1:0` (an OS-assigned port), `/api/*` JSON routes, the static built UI with SPA fallback, and `attachWebSocket(server, ctx)`. The window then `loadURL`s that origin (main.ts:516). In dev the same handler runs as a Vite plugin (`hearthProjectServer` in `vite.config.ts`), so browser mode at `localhost:5173` is functionally the same app.
- **Security:** `contextIsolation: true`, `nodeIntegration: false` (main.ts:465-469). The Origin/Host loopback guard is in `server/originGuard.ts`. External links go through `shell.openExternal` (main.ts:497-505). Vite `fs.deny` covers `.hearth/**` and `cors: false`.
- **Preload** (`electron/preload.cjs`) exposes only native affordances on `window.hearthNative`: `pickProjectFolder`, `pickDirectory`, `revealInFolder`, `setAudioMuted`, `setWindowMode`, `setAppMenu`, `setUnsavedScripts`, `checkForUpdates`, `onUpdateReady` (with late-subscriber replay), `relaunchToUpdate`, `onMenuInvoke`, `platform`. IPC channels are named `hearth:*` (main.ts:233-416). The renderer feature-detects it (`src/native.ts`).
- **One multiplexed WS** at `/api/ws?project=<abs>`. Frame union `WsFrame` is in `server/ws.ts:150`.
  - Chat up: `chat-send`, `chat-open`, `chat-new`, `chat-cancel`, `chat-interrupt`, `chat-approval`, `chat-input-response`, `chat-commands-list`, `chat-handoff-cli`.
  - Chat down: `chat-ready`, `chat-event`, `chat-opened`, `chat-list`, `chat-providers`, `chat-commands`.
  - PTY: `pty-start/input/resize/stop` up and `pty-data/exit/attach/error` down. A detached PTY lingers for 1h so a reload can reattach.
  - Also journal, evidence and export frames. The client socket is created in `store.ts:2174`.
- **ChatDriver interface** (`server/chat.ts:419`): `kind`, `start(sessionId, root)`, `send(text, agentOpts, attachments)`, `events: AsyncIterable<ChatEvent>`, `stop()`, `closed?`, `dead?`, `interrupt()`. There are three implementations: `agent-sdk`, `codex`, `stub`. Drivers are keyed by `(root, chatId)` and linger for 1h after detach.
- **Agent SDK call** (`server/chat.ts:1655-1712`) uses one long-lived `query()` per conversation with these options:
  - prompt: an async `promptFeed()`
  - `includePartialMessages`, `enableFileCheckpointing`, `forwardSubagentText`
  - `settingSources: ['user','project','local']`
  - `resume` (session id saved in the chat index)
  - `systemPrompt: {type:'preset', preset:'claude_code', append}`
  - `canUseTool` → `askPermission` (the approval seam)
  - `onElicitation`
  - `env`: a login-shell-merged environment (`shellEnv.ts`); `ANTHROPIC_API_KEY` is added only when a key is set
- **Provider-agnostic event vocabulary** (`src/types.ts:237-283`, mirrored on the server): `message-delta`, `message-end`, `reasoning-delta`, `tool-begin{toolId,kind,title,detail}`, `tool-output-delta`, `tool-end{status,exitCode,summary}`, `file-change{files:[{path,kind:edit|create|delete,diff?}]}`, `approval-request{approvalId,kind,title,detail,choices?}`, `approval-resolved{decision:allow|deny|withdrawn}`, `input-request/resolved`, `subagent-start/delta/end`, `plan-update`, `image`, `notice`, `turn-complete`, `error`. Legacy v0 events are upgraded by `normalizeChatEvent` in the store. The transcript is folded into message `parts`: text, reasoning, tool, command, skill, plan, image, notice, file-change, subagent, approval, input (`types.ts:299-442`). Tool rows have four states: `running | ok | error | stopped`.

## 5. Feature implementations
- **Streaming:** deltas arrive over WS and are folded into parts in the store. `src/components/chat/useSmoothStream.ts` paces the reveal: about 140ms catch-up, 2 to 90 chars per frame, snap above 6000 chars, and it respects reduced-motion. Tests: `smoothStream*.test.ts`.
- **Tool calls:** `ToolRun.tsx`, `ToolChip.tsx` (path shortening), `CommandRow.tsx`, `SkillRow.tsx`, `SubagentCard.tsx`, `ReasoningRow.tsx`, `PlanCard.tsx`.
- **Approvals:** these ride the WS because the turn is blocked on them. `ApprovalPrompt.tsx` renders inline and grabs the keyboard (Enter allows unless focus is on a button, Escape denies). Once answered it collapses into a past-tense record ("Allowed", "Denied", "Withdrawn"). Permission modes are `ask | auto | skip`, mapped to the SDK modes (`server/permissionMode.ts`, `PermissionSelector.tsx`). `skip` shows a one-time acknowledgement per project.
- **Diffs:** `FileChangeCard.tsx` shows one card per report and one row per file: path clipped from the left, +n/-n counted from the unified diff by `diffCounts()`, and the patch one click away.
- **Working indicator:** `WorkingRow.tsx` derives its label from the tail part ("Thinking", "Running", "Delegating", "Waiting for you", "Working"), shows an elapsed timer after a few seconds, and uses the `FlameMark` breathing animation (`working-breathe` in chat.css:748, `styles/app/flame.css`).
- **Session persistence** (`server/chatStore.ts`):
  - Files are `<project>/.hearth/chats/index.json` plus `<chatId>.jsonl`, one ChatEvent-shaped line per event, appended as it streams.
  - Rows can be "pending" until the first user message.
  - The SDK session id and Codex thread id are stored per chat, so a reopened chat resumes the agent's own session.
  - Attachments are staged, then consumed into `.hearth/chats/attachments/<chatId>/`.
- **Settings:**
  - Per project: `.hearth/app.json` (API keys, auto-gitignored; `server/chat.ts:558-640`).
  - Per machine: `~/.hearth/` holds `recent-projects.json`, `models.json` (the *disabled* models only), `skills/` and `skills.json`.
  - Settings UI: `SettingsDialog.tsx` + `settings/SettingsShell.tsx`, with panes General, Personalization, Agents, Usage, Skills (`settings/panes.ts`, searchable by keywords).
- **Auth and model selection:**
  - `server/claudeAuth.ts` runs `claude auth status` (JSON, 4s timeout) and never reads credentials itself.
  - `server/claudeModels.ts` gets the live catalogue from the SDK's `supportedModels()`, including effort levels, and caches it (about 2s cold).
  - `server/agentClis.ts` detects installed CLIs.
  - UI: `ModelSelector.tsx` (model + effort per turn; switching mid-chat rebinds the driver) and `settings/AgentsPane.tsx`, `providerStatus.ts`, `apiKeyShape.ts`, `providerLogos.tsx`.

## 6. Layout and navigation
`src/App.tsx`:
- Structure: `app-shell` → `Sidebar` (collapsible to a rail, `SIDEBAR_RAIL_PX`/`SIDEBAR_WIDTH_PX`) + `app-main` → `TopBar` + `app-screen`.
- `app-screen` is keyed by project or screen so switching replays the entrance animation, and it is wrapped in `ScreenBoundary` (an error boundary per screen).
- Screens: Home, ProjectHome, chat body (`ChatColumn` + optional `PaneStack` game column), SkillsScreen, TesterHistory.
- Narrow layout uses a matchMedia breakpoint and tabs between chat and pane.
- `CodePeek` is a file-viewer drawer; settings is a modal `<dialog>`.
- On macOS the frame uses `titleBarStyle: hiddenInset` with traffic lights inside the sidebar drag strip. Other platforms keep the native frame (main.ts:461-464).

## 7. Keyboard shortcuts
- `src/shortcuts.ts`: "mod" resolves to Cmd on Mac and Ctrl elsewhere, labels render as "⌘K" or "Ctrl+K", and `isTypingTarget` guards bare keys.
- `src/useShortcut.ts`: a capture-phase window listener that calls `preventDefault` only on a match.
- `components/shell/ShortcutLayer.tsx` binds:
  - Mod+K: search
  - Mod+N: new chat
  - Mod+,: settings
  - Mod+O: open folder
  - Mod+P: files peek
- Composer: Enter sends, Shift+Enter adds a newline, and IME composition is guarded. While a turn runs, Enter queues the message (`QueuedMessages.tsx`).
- Tabs: roving tabindex (`ui/tabKeys.ts`). Menus: arrow-key nav (`ui/Menu.tsx` `menuNavIndex`).

## 8. Component library (apps/editor/src/components)
- **Primitives:**
  - `ui/Button.tsx` (Button, IconButton)
  - `ui/Menu.tsx` (MenuItems, ContextMenu, MenuButton, viewport clamping, portal)
  - `ui/Switch.tsx` (segmented)
  - `ui/Tooltip.tsx` (with shortcut hint, warm state)
  - `ui/Toast.tsx` + `src/toast.ts`
  - `ui/ScreenHeader.tsx`
  - `ui/tabKeys.ts`
  - `ui.tsx` (NumberField, TextField, ColorField, Icon, FlameMark, Modal (native `<dialog>` showModal), ConfirmDialog)
- **Chat:** ChatColumn, ConversationHead, MessageList, Markdown (own renderer, `src/chat/markdown.ts`), Composer, AttachmentTray, ModelSelector, PermissionSelector, ApprovalPrompt, InputPrompt, ToolRun, ToolChip, CommandRow, SkillRow, ReasoningRow, PlanCard, SubagentCard, FileChangeCard, WorkingRow, QueuedMessages, TerminalPane, DevTeamPane, useSmoothStream.
- **Shell:** Sidebar, TopBar, SettingsDialog, NewProjectDialog, ShortcutLayer, useOpenFolder.
- **Settings:** SettingsShell, SettingsRow, GeneralPane, PersonalizationPane, AgentsPane, UsagePane, SkillsPane.
- **Other:** agent/Terminal.tsx + useAgentSocket.ts (xterm with an external-store scrollback that survives unmount), code/CodePeek, skills/*, project/*, home/Home, ScreenBoundary.
- **Styles:** `styles/tokens.css` defines the radii (8/10/14/18), `--shadow-1`/`--shadow-2`, control heights 36/30/24, `--row-h` 32, `--t-fast` 100ms / `--t` 150ms / `--t-slow` 220ms, `--ease-out cubic-bezier(.25,1,.4,1)`, and the z-scale.

## 9. Build, packaging and CI
- Root scripts: `build` (packages then editor), `dev` (Vite browser mode), `app`, `app:dist`, `test` (vitest run), `typecheck`.
- Editor scripts:
  - `app` = `vite build` + `scripts/build-electron.mjs` (esbuild) + `electron .`
  - `app:dist` additionally runs `scripts/assemble-app.mjs` → `release-app/`, then electron-builder
- Packaging: asar, with node-pty and the CLI bundles unpacked. Hooks are `afterPack.cjs` and `afterAllArtifactBuild.cjs`.
- CI: `.github/workflows/ci.yml` runs tests on ubuntu only. `release.yml` builds on an OS matrix when a `v*` tag is pushed.

## 10. Running on Windows in dev
- `npm install && npm run build:packages`, then `npm run dev` (browser at http://localhost:5173) or `npm run app` (builds, then launches Electron). Both work, because those scripts set no inline env vars.
- **These break on Windows:**
  - `app:dev` (`ELECTRON_START_URL=http://localhost:5173 electron .`) and `app:dist*` (`CSC_IDENTITY_AUTO_DISCOVERY=false ...`) use POSIX inline env syntax, which cmd.exe (npm's Windows shell) rejects.
  - Workarounds: set the env var first (`$env:ELECTRON_START_URL='http://localhost:5173'; npx electron .` after `npm run app:bundle`), or use cross-env.
- Windows-specific code: the PTY shell is `powershell.exe` on win32 (main.ts:590, 633). The window keeps the native frame. The Windows build is unsigned (SmartScreen warning, docs/desktop-app.md). vitest timeouts were raised to 120s for slow Windows runners (vitest.config.ts).

## 11. Reuse assessment
**Worth copying directly** (MIT licensed; keep attribution):
- `apps/editor/src/shortcuts.ts` + `src/useShortcut.ts`: cross-platform mod-key handling and labels.
- `apps/editor/src/types.ts` ChatEvent union (l.237-283) and the part model (l.290-450): a provider-agnostic transcript vocabulary.
- `apps/editor/server/chat.ts`:
  - the `ChatDriver` interface (l.419)
  - the Agent SDK `query()` options block (l.1655-1712)
  - `askPermission`/`canUseTool` → approval-request mapping, and `sdkPermissionMode`
  - the API-key handling
- `apps/editor/server/chatDrivers/codex.ts` + `codexWire.ts`: if Codex support is wanted.
- `apps/editor/server/chatStore.ts`: index.json + jsonl append-as-you-stream, pending rows, resume handles.
- `apps/editor/server/claudeAuth.ts`, `claudeModels.ts`, `modelPrefs.ts`, `agentClis.ts`, `shellEnv.ts` (login-shell PATH merge; essential for a packaged app), `originGuard.ts`, `ptyManager.ts`.
- `apps/editor/electron/main.ts` pattern (loopback server + `loadURL`, `contextIsolation`, open handler, did-finish-load replay) + `preload.cjs` + `updater.ts`/`updaterPolicy.ts`.
- `apps/editor/scripts/build-electron.mjs` (drop the Lua/wasm bits) + the electron-builder config block in `apps/editor/package.json`.
- Chat UI:
  - `useSmoothStream.ts`, `WorkingRow.tsx` (`workingLabel`, drift-free elapsed timer), `ApprovalPrompt.tsx`, `FileChangeCard.tsx` (`diffCounts`), `ToolRun.tsx`/`ToolChip.tsx`, `Composer.tsx` (IME-safe Enter, queueing), `QueuedMessages.tsx`, `ModelSelector.tsx`, `PermissionSelector.tsx`, `InputPrompt.tsx`, `Markdown.tsx` + `src/chat/markdown.ts`
  - restyle all of these to Lectern tokens
- Primitives: `ui/Menu.tsx`, `ui/Tooltip.tsx`, `ui/Button.tsx`, `ui/Switch.tsx`, `ui/Toast.tsx`, Modal/ConfirmDialog in `ui.tsx`, `ui/tabKeys.ts`.
- `components/agent/Terminal.tsx` + `useAgentSocket.ts` (xterm scrollback store) and `settings/SettingsShell.tsx` + panes structure.
- Test patterns: `tests/inkContrast.test.ts` (computes contrast from tokens), `tests/styleGates.test.ts`, `tests/support/cssRules.ts`, and the jsdom per-file pragma.

**Not worth reusing** (game-dev specific or Hearth-brand specific):
- All of `packages/*`: core, runtime (pixi/Lua), playtest, probe-*, adapter-web, shipping, templates, examples, cli, mcp-server.
- `server/gameServer.ts`, `hearthShim.ts`, `probeStream.ts`, `journalWatcher.ts`, `evidenceWatcher.ts`, `harnessRegistry.ts`, `projectIdentity.ts`, `server/tester/*`, devTeam* (unless multi-agent is wanted).
- `components/game/*`, `components/tester/*`, `src/probeStream.ts`, `src/harness/*`, and the `projects/IdentityPicker`/`ProjectMark` flame identity.
- `styles/app/flame.css`, `FlameMark`, `assets/brand/*` (Hearth brand), and `game.css`/`tester.css`/`devteam.css`.
- The Vite aliases and wasm/Lua inlining in `vite.config.ts` and `build-electron.mjs`, plus `src-tauri/` (abandoned).
- The token *values*. Hearth is dark-only with an OKLCH ember palette. Reuse the token *structure* (control-height tiers, radius scale, motion tokens, z-scale), not the values.
- `store.ts` as a whole: a 3.7k-line monolith. Copy its slices selectively.
