# Integration research

Checked 2026-09-24 UTC. Read `docs/spec.md` first.
This is source research, not a successful competition submission, Pi dogfood run,
or Lectern deployment. Only this file was written. No dependencies were installed.
Public sources were fetched over HTTPS. The requested `websearch` skill was tried,
but Serper is not configured. To enable it: get a key at https://serper.dev, then
Prime Agent `/login` → MCP Connections → Serper (web search). Direct official
sources were available, so research continued without search.

## 1. ARC-AGI-3

### Official SDK and version evidence

The official SDK exists. A local simulator is useful for deterministic tests but
must not be presented as the official environment or as evidence of competition
compliance.

- Benchmark: https://arcprize.org/arc-agi/3
- Documentation index: https://docs.arcprize.org/llms.txt
- Quickstart: https://docs.arcprize.org/index.md
- Official toolkit repository: https://github.com/arcprize/arc-agi
- PyPI metadata: https://pypi.org/pypi/arc-agi/json
- Version-specific metadata: https://pypi.org/pypi/arc-agi/0.9.9/json

PyPI reported **arc-agi 0.9.9**, uploaded 2026-06-10, requiring **Python >=3.12**.
Its dependencies include `arcengine>=0.9.3`, Pydantic, requests, Flask, matplotlib,
Pillow and python-dotenv. Pin the engine too when creating a reproducible optional
integration environment; the SDK's lower bound is not an engine lock.
The 0.9.9 wheel SHA-256 is
`a0536df47b5ab93af16ba708083f74261cd1b7801bb2e0802824623c04d59e50`.

Supported shape from the official quickstart:

```python
import arc_agi
from arcengine import GameAction
arc = arc_agi.Arcade()
env = arc.make("ls20")
if env is None:
    raise RuntimeError("Environment unavailable")
observation = env.step(GameAction.ACTION1)
```

Use `get_environments()` for discovery, `env.action_space` for supported actions,
`env.step(action, data={"x": x, "y": y})` for coordinate actions, and `env.reset()`
only with the selected mode's semantics. Normal mode can download environment
code. Explicit OFFLINE mode and already-staged game files are needed to guarantee
no internet during local tests. Do not import or inspect hidden game internals.

### Observations and scoring are not a generic grid puzzle

Sources:

- https://docs.arcprize.org/game-schema.md
- https://docs.arcprize.org/actions.md
- https://docs.arcprize.org/methodology.md
- https://docs.arcprize.org/toolkit/competition_mode.md

Responses contain one or more frames and metadata, not just a single grid.
Grids are at most 64×64 with values 0–15. States include `NOT_PLAYED`,
`NOT_FINISHED`, `WIN`, and `GAME_OVER`. `ACTION6` requires coordinates 0–63;
`ACTION7` is undo when supported. Only available actions should be exposed.
In game-over state only RESET is valid.

Official scoring is Relative Human Action Efficiency, not binary win rate:
per-level `(human_actions / agent_actions)^2`, capped at 1.15; game aggregation
weights levels by their 1-indexed number and caps by completion; total is the
average across games. Internal reasoning and tool calls do not count as game
actions, **but they still consume Architect's compute budget**. Prefer the official
scorecard to a hand-built approximation. If a simulator uses a simpler score,
label that score and its tests as simulator-only.

Competition mode enforces API-only interaction, one `make` per environment,
one scorecard, level resets rather than whole-game resets, scoring against all
available environments, and no in-flight `get_scorecard`. The docs explicitly say
Kaggle forces this mode. “Offline submission” does not mean loading hidden game
Python into the agent; the official submission uses a local gateway API.

### Submission tooling and confirmed restrictions

- Track page: https://arcprize.org/competitions/2026/arc-agi-3
- Starter docs: https://docs.arcprize.org/arc-prize-2026.md
- Official starter: https://github.com/arcprize/ARC-AGI-3-Kaggle-Starter
- Inspected starter commit: `eeb1535404f321d280a8f9194bbc1d7aca5f05fc`
- Reproducible starter README:
  https://github.com/arcprize/ARC-AGI-3-Kaggle-Starter/blob/eeb1535404f321d280a8f9194bbc1d7aca5f05fc/README.md
- Submission builder:
  https://github.com/arcprize/ARC-AGI-3-Kaggle-Starter/blob/eeb1535404f321d280a8f9194bbc1d7aca5f05fc/scripts/build_notebook.py
- Binding rules entry point:
  https://www.kaggle.com/competitions/arc-prize-2026-arc-agi-3/rules
- Code requirements entry point:
  https://www.kaggle.com/competitions/arc-prize-2026-arc-agi-3/overview/code-requirements

Confirmed by the public track: submissions go through Kaggle; evaluation has
**no internet**; all code and methods must be open sourced for prize eligibility.
The starter says **five official submissions per day**. Its documented hardware
choices are CPU, dual T4, P100, and an ARC-AGI-3-specific RTX 6000 option. These are
supported configurations, not proof of the binding compute ceiling.

The starter packages `agent/my_agent.py` (`MyAgent.is_done` and `choose_action`)
into a notebook. It installs wheels with `--no-index` from competition data,
waits for `http://gateway:8001/api/games`, copies the provided agents framework,
and runs it in competition reruns. Notebook outputs include `submission.parquet`.
`make submit` uploads/runs the notebook; a later explicit “Submit to Competition”
step spends a daily submission. A successful Save & Run All is not hidden-set
validation: the builder writes a dummy parquet in the non-rerun branch.

**Unresolved, do not invent:** exact wall-time, CPU/RAM/GPU-hours, model-size or
weight-license limits; detailed external-data policy; specific permission for
arbitrary subprocesses, package runtimes, dynamically generated code, or runtime
model tuning in the submitted notebook. The public track still says hardware
and compute limits will be announced. Anonymous HTTP requests to both Kaggle
rules/code-requirements URLs returned only the JavaScript app shell, not the
rule text. The README's five-per-day claim also needs rechecking against the
current binding rules before submission. No authenticated submission was made.

### Practical Architect design

1. Keep an optional official `Environment` adapter separate from the small local
   simulator. Fail clearly when the official extra, Python version, game files,
   or gateway are unavailable. Never silently fall back and report official results.
2. Freeze submitted artifacts, model weights, wheels and configuration offline.
   Do not call external LLM APIs, npm registries, or an online compiler at runtime.
3. One parent budget owns setup, compilation, model selection, probes and play.
   Reserve child budgets atomically and charge failed work. A game action cap is
   separate from a wall-time/token/dollar cap.
4. Compile explorer/planner/hypothesis-tester harnesses before deployment.
   Keep stage 3 disabled in competition runtime as required by this project's spec;
   this is an Architect design restriction, not a verified competition rule.
5. Use versioned public games for tuning and separate unseen preview games for
   verification. Do not infer permission to inspect private engine source from
   availability of public local game files.
6. Treat compliance as **unverified** until an owner reads the current Kaggle
   rules, fills a numeric cap manifest, confirms allowed runtime tools, and runs
   the official notebook/gateway path. A local test cannot license that claim.

## 2. Pi 0.73.1: real coding execution with a fake model

### Version and licensing evidence

Both npm versions are real, public and MIT-licensed:

- https://registry.npmjs.org/@mariozechner/pi-agent-core/0.73.1
- https://registry.npmjs.org/@mariozechner/pi-coding-agent/0.73.1
- Repository tag: https://github.com/badlogic/pi-mono/tree/v0.73.1

Both registry records give gitHead
`781152fc24841dc54b22284514604048ebe5e2c9`.
`pi-agent-core` requires Node >=20.0.0; `pi-coding-agent` requires >=20.6.0.
Tarball SHA-1 values: core `332224f2f2370f40b36aa3553e154078c257ff67`;
coding-agent `a14f26f45d3553556149432d743242675771862a`.
The coding-agent package depends on core and pi-ai using `^0.73.1`, so pinning
only the top-level package is not enough. Commit the lockfile and use `npm ci`;
verify the resolved core, coding-agent and pi-ai versions in CI. Keep MIT notices.

The published coding-agent tarball was downloaded and its declarations inspected
in memory, not installed or executed. Versioned source links below were also read.

### Prefer the full coding agent, not a new loop

- Core API: https://github.com/badlogic/pi-mono/blob/v0.73.1/packages/agent/README.md
- Actual core class: https://github.com/badlogic/pi-mono/blob/v0.73.1/packages/agent/src/agent.ts
- SDK guide: https://github.com/badlogic/pi-mono/blob/v0.73.1/packages/coding-agent/docs/sdk.md
- Actual SDK: https://github.com/badlogic/pi-mono/blob/v0.73.1/packages/coding-agent/src/core/sdk.ts
- Extensions: https://github.com/badlogic/pi-mono/blob/v0.73.1/packages/coding-agent/docs/extensions.md
- Provider/tool types: https://github.com/badlogic/pi-mono/blob/v0.73.1/packages/coding-agent/src/core/extensions/types.ts
- RPC: https://github.com/badlogic/pi-mono/blob/v0.73.1/packages/coding-agent/docs/rpc.md

`pi-agent-core.Agent` owns the LLM/tool loop. `pi-coding-agent` supplies the coding
session, built-in read/bash/edit/write tools, extension loader and RPC process.
Use `createAgentSession`, `session.prompt`, `session.subscribe`, `session.abort`
and `session.dispose`, or launch the package's `dist/cli.js --mode rpc` as a real
Node subprocess. A fake response generator alone is not evidence of Pi execution.

**0.73.1 documentation mismatch:** `sdk.md` examples pass tool objects, but both
the tagged implementation and published `dist/core/sdk.d.ts` define
`CreateAgentSessionOptions.tools?: string[]` as an allowlist. Use e.g.
`tools: ["read", "bash", "edit", "write"]`; use `customTools` for definitions.
Prefer published declarations/source when examples disagree. The options do not
have a `streamFn` field; that belongs to the underlying Agent.

RPC uses LF-delimited JSON. `prompt` returning `success: true` means accepted,
not complete or passed. Wait for lifecycle events, then inspect failures and
verify the file/test outcome independently. Keep extension debug logs off stdout.
For Node clients, do not split valid JSON strings at U+2028/U+2029; the docs warn
that generic `readline` behavior is not protocol-compliant.

### Real extension execution

An extension is a TS module exporting a default factory receiving `ExtensionAPI`.
Pi loads TS through its own loader; a custom Python substitute is unnecessary.
Use `pi.registerTool`, `pi.on("context", ...)`, `pi.on("before_agent_start", ...)`
and `pi.on("tool_call", ...)` for generated tools/context/prompt policy. Discover
packs explicitly via `additionalExtensionPaths` or `-e path.ts`.

`pi.exec(command, args, {signal, timeout, cwd})` executes a real child process.
The result has `stdout`, `stderr`, `code`, and `killed`. Source:
https://github.com/badlogic/pi-mono/blob/v0.73.1/packages/coding-agent/src/core/exec.ts
It calls Node `spawn` with `shell: false`. Pass an actual executable and arguments;
do not assume shell syntax, or Windows `.cmd` wrappers, work as executables.
The timeout is milliseconds. This helper kills its direct child and is **not a
sandbox or reliable whole-descendant-tree budget boundary**. It also accumulates
stdout/stderr in memory. An outer runner must bound output and terminate the
entire job/process tree on timeout. Use a clean cwd, filesystem permissions and
network isolation; extensions have full process permissions.

### Fake-model integration, without faking Pi

Upstream fake stream tests:
https://github.com/badlogic/pi-mono/blob/v0.73.1/packages/agent/test/agent.test.ts

Two supported seams:

1. Core/API test: inject `new Agent({streamFn})`, or set the public
   `session.agent.streamFn` after creating a coding session. Return Pi's
   `AssistantMessageEventStream` (upstream tests use `EventStream` with terminal
   `done`/`error` messages), not plain text or a generic async generator.
2. Extension/RPC test: `pi.registerProvider` accepts a `streamSimple` callback
   with `(model, context, options) => AssistantMessageEventStream`. Register a
   deterministic local fake provider with a synthetic API key and explicit model;
   do not load real credentials or call a provider endpoint. This tests the real
   CLI, extension loader and coding loop together.

Emit a first assistant response containing a deterministic tool call, then a
second final assistant response after Pi provides the tool result. Populate
`usage.input/output/cacheRead/cacheWrite/totalTokens` and `usage.cost.*` explicitly.
Include nonzero synthetic usage in budget tests even though no vendor is billed.
The stream must honor its AbortSignal and terminate on `done` or `error`.

Acceptance evidence should show: locked Pi package versions; child PID/exit;
extension loaded; a real Pi tool execution; expected temporary file change;
a separately executed project-native test; numeric usage/wall time; and a budget
refusal case that prevents further model/tool execution. Use a fixture worktree
and a fake model for normal CI, then the actual Phase6 module for the required
coding dogfood. Source research here does not satisfy that execution requirement.

### Budgets must sit outside model judgment

- Reserve worst-case tokens/cost **before** calling the stream/provider and cap
  per-call output. Reconcile actual usage once per assistant `message_end`.
  A post-response counter alone cannot prevent overspend on the first call.
- Core `beforeToolCall` and extension `tool_call` can return
  `{block: true, reason: "..."}`. Core Agent subscribers are awaited; the assistant
  `message_end` barrier precedes tool preflight. Low-level `agentLoop` event
  consumers do not provide that same awaited barrier.
- Pi's default is parallel tool execution. Reserve the entire batch or enforce
  sequential execution for easier accounting. Blocking one sibling does not
  automatically cancel all the others.
- `before_provider_request` edits the provider payload; it is not documented as
  a general budget-refusal protocol. Do not rely on returning an invented
  `{block: true}` there. Wrap the stream/request seam and abort at the outer gate.
- Include retries, compaction, extension-initiated model calls and child agents.
  Disable unneeded retries/compaction in fake tests. Context window estimates
  from `getContextUsage()` are not cumulative billing records.
- `agent.abort()` / `session.abort()` handle cooperative stop; an outer monotonic
  deadline and process-tree termination enforce hard wall time. Tool timeout
  and model thinking budget do not enforce a total-run budget.
- Isolate HOME/agentDir, auth, settings, resources and session storage. Use
  `SessionManager.inMemory()` and explicit resources; loader flags include
  `noExtensions`, `noSkills`, `noPromptTemplates`, `noThemes`, `noContextFiles`.
  Ensure the explicitly allowed pack still loads. Verify lifecycle binding
  against the pinned runtime if using a custom SDK embedding.
- Architect telemetry must keep only closed/hashed categories and numeric totals.
  Pi events/transcripts can contain prompts, tool arguments and code; never dump
  them wholesale into metadata-only JSONL/SQLite. Disable persistent Pi sessions
  for ephemeral runs and remove scratch artifacts after verification.

## 3. Lectern integration research

Left out of the public repository: this section summarized the internals of
Lectern's private codebase.

## Completion and remaining gates

Research establishes official APIs, package versions and local integration seams.
It does not establish Pi execution, model quality, runtime isolation, live cost
savings, deployment safety or competition eligibility. Required follow-up evidence:
real pinned Pi subprocess + fake provider dogfood; optional official ARC adapter
smoke test in its own Python >=3.12 environment; owner-confirmed current Kaggle
caps/tools; and synthetic-only Lectern end-to-end tests before any live integration.
