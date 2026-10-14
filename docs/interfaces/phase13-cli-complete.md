# Phase 13: the complete Architect CLI

Status: spec (2026-09-30). The code, `README.md` and `docs/usage.md` win where they differ
after implementation.

## 1. Direction

The user: "build out architect cli completely. ensure the cli experience is up to par in
quality with hermes, claude code, codex, etc. and it's intuitive to use. can connect via
chatgpt oauth, local models, everything every other harness can. and of course there's a
unique forge mode where you build a harness whether it's for an agent you're running locally
or an agent you're implementing into software." GitHub Actions is out of scope.

Fixed from earlier phases: Stage 4 alone licenses; floors and budgets are inferred and shown,
never required; budgets are dollars and seconds (tokens metered, never capped); Architect's
conductor never writes files (the coder does, with approvals); content-free telemetry; tests
never call live providers, CLIs or local model servers; local GPU models run only when the
user asks.

## 2. Work packages and file ownership

Five packages are built in parallel on one tree. Each owns its files; a package that needs a
change in another's file asks the integrator instead of editing it. Slash commands are
registered from per-package modules so `commands.py` is not a shared hot spot:
`commands.py` keeps the registry and imports `commands_providers`, `commands_forge` and
`commands_mcp` (the integrator adds those imports).

| Package | Owns |
|---|---|
| A. Providers and sign-in | `architect/providers/` (new), `architect/profiles.py` (adapter factory), `architect/models/pricing.py`, `architect/models/prices.json`, `architect/agent/commands_providers.py` |
| B. Native tools and streaming | `architect/models/chat.py` (new), `architect/models/http.py` (streaming additions only), `architect/agent/native.py` (new), the minimal hook in `architect/agent/conductor.py` |
| C. CLI experience | `architect/agent/repl.py`, `commands.py`, `render.py`, `app.py`, `sessions.py`, `architect/agent/config.py` (new), `architect/agent/checkpoints.py` (new), new conductor tools in `architect/agent/tools.py` (web_fetch, image attach) |
| D. MCP client | `architect/mcp/` (new), `architect/agent/commands_mcp.py` |
| E. Forge mode | `architect/agent/build.py`, `architect/agent/export.py` (new), `architect/agent/commands_forge.py` |

## 3. Package A: providers and sign-in

- **Catalog** (`architect/providers/catalog.py`): one entry per provider with id, label, kind
  (`cli`, `api`, `local`), adapter (`claude-code`, `codex`, `anthropic`, `openai`, `ollama`),
  default base URL, API key environment variable, how to list models, sign-in method:
  - `claude` (logged-in `claude` CLI; sign-in: run `claude` and log in),
  - `chatgpt` (ChatGPT plan via the official Codex login: `/login chatgpt` runs
    `codex login`, the browser OAuth flow OpenAI ships; models go through `codex exec`),
  - `anthropic`, `openai`, `openrouter`, `gemini` (OpenAI-compatible endpoint), `groq`,
    `deepseek`, `xai`, `mistral`, `together` (API keys),
  - `ollama`, `lmstudio`, `llamacpp`, `vllm` (local servers; $0), and `custom` (any
    OpenAI-compatible base URL).
- **Credentials** (`architect/providers/credentials.py`): API keys come from the environment
  first, else `<workspace>/credentials.json` (0600 where honoured, never logged, never in
  telemetry or transcripts). `AdapterConfig` gains `api_key_ref` (a credential name) next to
  `api_key_env`; the factory resolves it.
- **Model discovery**: OpenAI-compatible `/v1/models`, Ollama `/api/tags`, LM Studio and
  llama.cpp `/v1/models`, OpenRouter `/api/v1/models` (with prices), fixed lists for the
  CLIs. Bounded timeouts; failures are a short message, never a crash.
- **Prices for models not in `prices.json`**: local providers are $0; OpenRouter prices come
  from its model list; otherwise the user declares one (`/model add <id> --price in/out`) or
  the model runs as `unpriced`: it can be used for conversation and subagents with a
  time-only budget and a visible "cost unknown" label, and build mode refuses to compile on it
  (`model_unpriced`) because dollar budgets could not be enforced.
- **Commands** (`commands_providers.py`): `/login [provider]` (guided: pick a provider, then
  the key prompt, the CLI login, or the local URL check), `/logout <provider>`, `/providers`
  (status of every provider: signed in, key present, server reachable), `/models [provider]`
  (live list), and the model picker behind `/model` (numbered list grouped by provider;
  choosing one registers it in the profile).
- **Onboarding**: the first run shows the provider list with what was detected (logged-in
  CLIs, keys in the environment, reachable local servers) and recommends one; picking one
  writes the profile. Exposed as `onboarding_choices()` and `register_model(...)` for app.py.

## 4. Package B: native tool calling and streaming

- `architect/models/chat.py`: `ChatMessage`, `ToolSpec`, `ChatEvent` (text delta, tool call,
  usage, done) and an optional adapter capability `stream_chat(system, messages, tools,
  max_output_tokens) -> AsyncIterator[ChatEvent]` implemented for `AnthropicAdapter`
  (Messages API, SSE, `tool_use`), `OpenAICompatibleAdapter` (Chat Completions, SSE,
  `tool_calls`; works for every OpenAI-compatible provider in Section 3) and `OllamaAdapter`
  (`/api/chat` with `tools`, NDJSON). CLI adapters do not implement it.
- Metering is unchanged in spirit: the call is quoted and reserved against the run budget
  before it starts and reconciled with the usage the stream reports; a stream that exceeds
  its quote raises `BudgetExceeded` like any call.
- `architect/agent/native.py`: the conductor's native loop. When the conductor's adapter has
  `stream_chat`, turns use native tools and stream text; otherwise the envelope loop is used.
  Same tools, transcript, budgets, cancellation and events; new event `TextDelta(text)` so
  the UI streams the answer.

## 5. Package C: the CLI experience

Parity targets (today's coding agents), in priority order:
1. Streaming answers (renders `TextDelta`), a spinner with elapsed time, tokens and spend.
2. A status line with model, mode, permission mode, context fill (percent with colour
   thresholds) and session spend; auto-compact near 85% context; `/context`.
3. Model and provider switching from the CLI (`/model` picker from Package A), `/login`.
4. Checkpoints: the coder's file changes are snapshotted per turn; `/undo` restores the last
   turn's files; `/diff` shows the session's changes (git diff when in a repo, else the
   checkpoint diff).
5. Permission modes `plan` (read-only: the coder is not called; the conductor proposes),
   `ask`, `auto-edit`, `full-auto`; Shift+Tab cycles normal/forge mode, `/permissions` and a
   footer indicator for the rest.
6. Config: `<workspace>/config.toml` and `<project>/.architect/config.toml` (model, provider,
   permission, session budget, theme, auto-compact threshold); `/config` shows and sets.
7. `/doctor` (providers, Node and Pi, Ollama reachability, workspace, profile, disk), `/memory`
   (open ARCHITECT.md in `$EDITOR` or show it), `/init`, `/export [file]` (the conversation
   as markdown), `/copy` (last answer to the clipboard where available), `/review` (ask the
   conductor to review the working-tree diff), `/title`.
8. Input: Ctrl+R history search, Ctrl+G or Ctrl+X Ctrl+E external editor, messages typed
   while a turn runs are queued and sent next, image attachments (`@image.png` or `/image`
   for vision models), bracketed-paste collapse, fuzzy slash-command menu with descriptions.
9. Tools: `web_fetch(url)` for the conductor (text extraction, bounded), custom slash commands
   from `.architect/commands/*.md` and `<workspace>/commands/*.md` (the file body is the
   prompt; `$ARGUMENTS` substitution).
10. A first-run experience that needs no reading: detected providers, a recommendation,
   one keypress.

## 6. Package D: MCP client

- `architect/mcp/`: a client for the Model Context Protocol over stdio (spawned servers) and
  streamable HTTP; `initialize`, `tools/list`, `tools/call`; bounded output; per-server
  timeouts; servers configured in `config.toml` under `[mcp.servers.<name>]` (`command`,
  `args`, `env`, or `url`, `headers`) or added with `/mcp add`.
- MCP tools become conductor tools named `mcp__<server>__<tool>`. Calls go through the
  approval prompt in `ask` mode (an MCP tool can have side effects); results are clipped.
- `/mcp` lists servers, status and tools; `/mcp add|remove|enable|disable`.

## 7. Package E: Forge mode

- The mode Shift+Tab reaches is **Forge mode** (was "build"); `/forge <description>` runs one
  request in it (the old `/forge arc` target form stays: `/forge arc`).
- The interview's location question becomes the first decision: **Run it here** (an installed
  agent: `/run`, `architect run`, `architect serve` over HTTP, optionally on a local model) or
  **Put it into software** (a licensed harness exported into the current repository).
- **Into software**: after a licensed build, `export.py` stages the package with the existing
  guarded export (`forge_modes.export_package` / `place_package`), shows the files, asks
  before writing anything into the repo (the coder-style approval), and prints how to call it:
  a Python snippet (`ExportedHarness`), an HTTP snippet (`architect serve` + curl) for other
  languages, and the test command. `/export <agent> [path]` does this for any installed agent.
- **Run it here**: `/serve [port]` starts the existing HTTP service for installed agents (bound
  to localhost), `/run` as today; for a local model the plan card shows the model and the
  per-run seconds (local models are $0; time is the budget).
- Forge's own progress keeps streaming (Phase 12); the result card shows the harness design.

## 8. Acceptance

- Every package has offline tests; `ruff`, `mypy --strict` and the full suite stay green.
- Live (claude CLI only; no GPU): onboarding to a first answer in under a minute; a streamed
  answer through an API-style provider is exercised offline with recorded SSE fixtures; a
  Forge-mode build exported into a scratch repository and called from a Python snippet.
