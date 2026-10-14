# Limitations and evidence boundaries

This page lists what Architect's checks can and can't show. The short version: an agent
only runs if it passed its exam, and a changed agent stops until it passes again, but
that's a tripwire, not a lock, and a pass on a small exam says little about inputs unlike
its cases. The stop rule's limits are under [Retake](#retake). The older sections cover
earlier development phases, and their numbers are in [PROGRESS.md](../PROGRESS.md).

## What local checks can establish

- A fresh held-out report is required to license code/configuration/spec bytes.
- Dollar/time limits remain active across model calls and nested work.
- Tokens are recorded for usage/efficiency; there are no token quotas.
- Floor failures, expired artifacts, unapproved installs and registry regressions
  are refused. Cache reuse cannot skip the new task's held-out verification.
- Sequential pipelines execute all declared stages and validate their outputs.
- The real upstream Pi 0.73.1 loop and file tools can run under a guarded model
  transport with scripted Fake responses.
- HTTP and TypeScript can exercise exact-spec approval, opaque IDs, licensing,
  separate installed delivery flags and fixed-code errors on a local server.
- Local game rollouts, receipts, partition rules and aggregate budgets can be
  tested without calling a live model or official game service.

These are mechanism claims. Consult [PROGRESS.md](../PROGRESS.md) and the labeled
[evidence ledger](../examples/telemetry/README.md) for executed checks, rather
than interpreting source code or a passing fake fixture as external evidence.

## Trust boundary, not complete isolation

Profiles, adapters, evaluator code and the host are trusted. Held-out capabilities
protect normal Python API access; they do not withstand malicious reflection by
code already executing inside the trusted process. Dataset IDs and sorted splits
do not establish semantic independence of arbitrary real-world records.

Pi file tools restrict paths and deny private control-plane state. Explicit
`pi.allow_shell: true` plus a granted `bash` tool permits **trusted host code**.
It is not an OS/network sandbox. A repository's scripts can use host permissions.
Do not run untrusted checkouts or expose secrets through shell. Hostile concurrent
filesystem mutation is outside this boundary. Use separately configured OS or
container isolation where required. The restricted rewrite subprocess is also
not a general-purpose promise that arbitrary Python is safe on any host.
Pi cleanup reaps the whole shell process tree. Windows uses a kill-on-close job.
POSIX kills the bridge's session and process group plus every descendant still
reachable by parent links, including ones that called `setsid`. This runs even
when the bridge has already exited: anything still in its session (for example
after `setpgid`) is found by session id. A POSIX process that leaves the session
*and* is orphaned before cleanup (a classic double-fork daemon) has no remaining
link and can survive; use OS/container isolation there.

Signing keys are local trust roots. Losing the key can make old reports unusable;
exposing it undermines report authenticity. Set `ARCHITECT_SIGNING_KEY_FILE` to a
stable private absolute path outside coding checkouts and back it up separately.

## Cost and time are safety limits, not billing guarantees

Limits use configured prices, provider usage and local elapsed time. A provider
may charge for a request before cancellation arrives or return usage after work
has started. Runtime can refuse further work and delivery; it cannot undo an
external bill or side effect. Provider context/output capacities remain separate.
Game cleanup has a bounded grace period after an expired work deadline for
close/reap/unlink only; no new gameplay or model work is authorized by that grace.
Actual wall time includes cleanup. Spending the exact dollar limit does not itself
forbid zero-cost work while time remains; priced reservations still fail before
model I/O when insufficient dollars remain.
Benchmark latency and real invoices require live measurement. Synthetic fixture
prices do not establish production savings.
CLI-backed adapters (`claude-code`, `codex`) report API-equivalent costs from the
price table (and Claude Code's own figure); they are estimates, not subscription
bills, and plan usage limits are enforced by the provider, not by Architect.
Cached-token pricing is still an estimate: cache writes and reads are priced
from the configured multipliers and the provider's reported cache counts, not
from an invoice, and a prefix below the model's minimum cacheable length or
evicted after the cache TTL is billed at the normal rate.

## Telemetry privacy is scoped

Telemetry stores bounded numeric or closed/hashed categorical metadata, not
prompts, outputs, errors or customer content. Hashing is not encryption. Artifacts,
profiles, inputs, successful API responses, CLI stdout, and example summaries can
still contain content. Protect them with your own access and retention controls.
A durable log cannot be guaranteed after total storage failure; mandatory logging
failure must not be treated as successful delivery.

## HTTP is a trusted backend service

Bearer authentication, body bounds, opaque IDs and fixed errors do not provide
public-service hardening, TLS, multi-tenant authorization, rate limits or an
application privacy policy. The server binds to loopback by default. Do not put
the TS client bearer token in an untrusted browser. The package is local/private;
no npm publication is claimed. Spec approval is separate from application
feature enablement and human delivery review.

## Games and competition

- Local fixtures contain exactly three rule families and 15 distinct instances.
  The families are navigation, switches and sequence. They are not official ARC
  games or an ARC benchmark.
- The optional official adapter pins `arc-agi==0.9.9` and `arcengine==0.9.3`.
  Generic OFFLINE support requires real staged public files and an explicit,
  version-pinned `OfficialPreviewManifest`. There is no local fallback.
- Staged game files are Python the SDK executes. Only the official worker process
  runs them, with a scrubbed environment, temporary HOME and its own session, but
  as the same OS user with that user's file access. Stage only games from the
  public ARC API or another source you trust; this is not a sandbox.
- Completed scorecard percentages normalize with a versioned `raw / 100` policy.
  The documented score example and synthetic boundary tests are policy evidence,
  not a live official benchmark. The pure normalization API cannot authenticate
  caller-supplied numbers.
- Official transport tests use scripted responses and synthetic data. No live
  official game files, staged-public-game quality run or benchmark results are
  supplied. Installing the SDK alone does not supply those results.
- Gateway submission scorecards cannot be used for factory candidate trials.
  Public OFFLINE episodes and submission sessions have different lifecycles.
- OFFLINE scoring rejects a full reset or level regression
  (`official_level_reset_required`) and any completed card with more than one
  run per game (`official_multiple_runs`), because the SDK keeps the best run.
  Staged `baseline_actions` must list exactly one non-negative entry per game
  level (the observation's `win_levels`). Missing, empty, short or long lists
  fail as `official_baseline_unavailable` at `make`, before any action, with a
  finalize backstop. The SDK itself only flags a list shorter than the levels
  completed and otherwise scores just the listed levels, which inflates a short
  list and deflates a long one.
- Runtime child agents restrict the model binding and disable Stage 3 rewriting.
  Loopback binding is a local policy check, not proof of disconnected execution.
- The operator opt-in `GameFactoryConfig.allow_online_model` lets a cloud game
  model play public OFFLINE previews or local fixtures. That breaks the ARC
  no-internet rule, so such results carry `model_binding="online_preview"`, stay
  `compliance="unverified"` and are harness tests only, never submission
  evidence. Gateway (submission) mode refuses it.
- Numeric Kaggle compute caps, complete submission rules/tooling and actual
  compliance are not confirmed. `CompetitionRunResult.compliance` remains
  `unverified`, even with local owner-entered rules or passing toy scores.

## Declared rule checks

- A spec can declare rule checks in `execution_config["checks"]` (code in
  `architect/presets/checks.py`). They run in code inside the tuned preset, on each reply,
  before it is checked against the output schema, and the spec hash and the license cover
  them.
- A harness written by Forge or by a Stage 3 rewrite need not call the tuned preset, so
  it could skip the checks. Build mode keeps Forge off for a spec with checks, even when
  Forge is chosen, and the plan says so. Nothing else in the compiler stops Forge or
  Stage 3 rewriting for such a spec, so a profile used with checks should keep both off,
  as both receipt profiles do (`examples/expense-app/architect.json` and
  `examples/bench/eval-2026-10-06/ladder/profile.json`, with `enable_rewriting` false).
- A warm start can skip them too. The library matches a template on its domain, schemas,
  tools, pipeline and model, not on `execution_config`, so a build of a spec with checks
  may start from a licensed Forge harness of a spec without them. That harness keeps its
  own code, and only its calls to the tuned preset get the new spec and its checks. Build
  such a spec in a workspace whose library holds no licensed Forge harness for the same
  schemas and model.

## Retake

- It stops calls made through the agent's own folder. A coding agent can still edit the
  code that calls it, or call a model another way.
- It is a tripwire for a change nobody reviewed, not a lock. Someone who controls the
  computer and the signing key can sign anything, and the app's environment names that
  key (`ARCHITECT_SIGNING_KEY_FILE`).
- It covers the agent's files. It does not cover Architect's own prompt templates and
  runtime (`architect/presets/_execution.py`), the model behind the provider's API or the
  Python environment. With an HMAC-only signature, files outside `artifact/` are not
  protected from someone who also rewrites `MANIFEST.json`.
- The exam file and the extra retake cases are readable on disk. What holds is that no
  build, practice run or retake hands exam cases or per-case exam results to an agent,
  and that Architect's coder is denied those files. Telemetry rows written during an exam
  hold no case id, answer or right-or-wrong mark, but each row says whether that run
  finished, in exam order.
- Stopped means not examined. Did not pass means the changed agent missed the pass mark on
  those cases. Neither proves the change made the agent worse, and passed does not prove
  it lost nothing. A score on 100 cases is uncertain by roughly 10 points either way (the
  95% binomial range).
- Attempts are counted, not limited. Unused cases run out, and then a retake is the same
  exam and says so. A second score on the same cases is easier to reach than a first one.
- The record of attempts, the copy Undo restores and `placements.json` are plain files in
  the workspace and are not signed. The copy is checked before it is used. A new workspace
  starts a new record, so the attempt number in a report is what this workspace counted.
  A record that is damaged or missing ends the claim of unused cases (the next retake is
  the same exam and says so). It does not bring back the cases an earlier attempt used.
- A report signs a digest of the exam's own cases, so a retake refuses an exam file that
  was rewritten. The extra cases only retakes use are not covered by that digest: someone
  who rewrites that file changes what "unused" cases a retake runs on.
- One retake of an agent runs at a time on one computer. Two computers that share a
  workspace folder over a network drive are not covered.
- After a retake passes, the agent in the project is the new version, and the registry
  and Try your agent in the app still hold the version from the build. A program that
  imported the package before must import it again or restart. Until it does, its calls
  are refused with "Stopped: this app loaded an earlier version of its agent. 0 model
  calls."
- The stop sentence an app shows comes from the files alone. Whether a change can be
  retaken or only undone is decided by what changed inside them, which Architect and
  `standing()` report; a host that shows only the sentence and the changed files should
  not promise a retake.
- Retakes are for agents built from a preset, on `held_out_tasks` exams without a second
  floor, where the exam's entry in the profile says `"retakes": true`. A Forge-written
  agent, a change to a file other than `artifact/spec.json` and `artifact/config.json`,
  or a change to a fixed field can only be undone. Inside `execution_config` only the
  checks may change.
- A check's pattern is a regular expression the person wrote, and it runs inside the
  exam. Nothing times out a pattern that takes very long on some answer.
- The one measured trial of this rule is ten change requests on one agent, one build and
  one coder model ([docs/bench/retake-trial.md](bench/retake-trial.md)). No change in it
  missed the pass mark, so it does not show the rule catching a change that made an agent
  worse.
- The demo exam in `examples/receipts/demo` is made of development receipts that were
  used while Architect was being built. Scores on it are not the published results, which
  are the 348-receipt table and the evidence builds.
- It is not an accuracy improvement.

## Forge

- Sandbox language v2 is an interpreted allowlist, not OS isolation. Code is
  never passed to `compile()`/`exec()`. There are no imports beyond four fixed
  from-imports at the top, no attribute access except `context.<capability>` under
  `await` and the listed method spellings of builtins (`s.split(sep)` is
  `split(s, sep)`), and no reachable Python object. The only effects are
  host-checked capability calls. Work is bounded by fuel (default 10,000,000, at
  most 100,000,000; capability calls cost fuel too), by construction limits and by
  the unchanged run deadline. Worker memory is capped at 512 MiB by a Windows Job
  Object and at 1 GiB of address space by POSIX `RLIMIT_AS`. `RLIMIT_AS` counts
  virtual memory, so it is a coarse cap. v2 has no `raise`, `lambda`, recursion or
  closures, and its comprehensions run as the loops they stand for. `try` catches
  only data errors, never fuel, memory or protocol errors. Images reach a model
  only as `$image` stubs from the task, resolved by the host. v1 artifacts keep the
  frozen v1 language.
- Tuning labels are visible to the Forge agent (in the default `per_case`
  feedback mode). Overfitting to tuning is bounded only by the literal-leak lint
  (literals of 24 or more characters), Stage 4 on held-out cases, and the
  per-Spec verification cap. It is not ruled out. `feedback: "aggregate"` hides
  expected values.
- Child licenses are weaker than parent licenses. A child is verified on its
  own held-out half of cases drawn from parent tuning or agent-authored data
  (`case_origin`). It says nothing about the parent task; only the parent's Stage 4
  does.
- With a Nemotron model as its engineer, Forge has not beaten the tuned preset. Pi drives
  the engineer with native tool calls when its adapter streams them (the
  Anthropic, OpenAI-compatible and Ollama HTTP adapters) and with the JSON
  envelope otherwise (the coding-agent CLIs). On the receipts task, Forge ran 12
  sessions with a Nemotron model as the engineer, through Nebius Token Factory:
  5 with Ultra and 7 with Super. Eleven were development sessions on a separate
  rehearsal set of receipts (`docs/feedback/probes/`), and the twelfth ran inside the
  ladder build of the evaluation run of 2026-10-05
  (`docs/bench/results-receipts.md`). None ended with an accepted harness. Ultra
  submitted nothing, and no submission from Super cleared the 0.40 bar on its
  practice receipts; the best scored 0.31. No tool call in the development
  sessions was malformed, so the shortfall is in the harnesses the engineers
  wrote. Two trial sessions with DeepSeek V4 Flash and Kimi K2.7 Code ended the
  same way. Every one of those builds kept the tuned preset, and that is what a
  build on Nemotron ships. A Forge session there spends time and money without
  changing the result: in the ladder build it ran for 27 minutes and cost $1.16,
  almost four times the rest of the build. With a Claude model as the engineer,
  Forge's harnesses were accepted and licensed on small model-drafted floors
  (`examples/live/phase12-build.md`, `examples/live/phase13-export.md`), which
  shows the loop running end to end and says little about real data. Whether a
  local model can drive Forge's tools (check, measure, submit) has not been
  measured live. No claim that Forge "works with any model" is made.
- On fixtures the explorer revisits more than the preset. In the $0 Stage A
  smoke run (fake provider, seed 7, `arc-local-3`), the forge and template arms
  raised `distinct_states_mean` from 5.333 to 7.333 and cut `calls_per_action`
  from 1.0 to 0.607. But `revisit_rate_mean` rose from 0.167 to 0.381, because
  the explorer spends early actions probing untried options. This misses the
  pre-registered target (at most half the preset's revisit rate). It is fixture
  evidence only.
- Live Stages B and C have not finished. Stage B is a cloud Forge agent with
  local `granite4.2:8b` play. Its preset arm licensed but failed delivery on an
  invalid decision, and host memory pressure stopped its template and Forge arms
  twice ([PROGRESS.md](../PROGRESS.md), Phase 10). Stage C is all local and has
  not run. Both need operator approval, one loaded model, and the host memory
  preflight (`bench probe`). Forge and template game cells refuse with
  `host_memory_low` under 6 GB of free RAM. Until they finish and are recorded in
  [PROGRESS.md](../PROGRESS.md) and `examples/telemetry/arc-forge-evidence.json`,
  there is no live Forge result on games and no ARC-level or benchmark result.
- Forge needs a smoke floor, and it refuses (`forge_disabled`) rather than
  skipping silently. The build agent runs on Pi 0.73.1, which is pinned even though
  upstream has marked it deprecated. Migrating to a newer Pi is out of scope.

## Routing

- Routing orders licensed versions; Stage 4 alone licenses. A route is advice unless the
  operator grants reuse for that call, and every reuse re-loads and re-checks the entry.
- Evidence is thin. Delivery telemetry carries no score, drift windows exist only for games
  (outside games the label is `drift_window_none`), and Stage 4 cost and compute are per-case
  maxima (`latency_bound`), not measurements. The router uses a conservative lexicographic
  rule and shows every evidence label instead of a single score.
- Tier A (an exact task slot) is rare for free prose: two prose requests almost never infer
  the identical Spec, so exact reuse is mostly for Specs that were saved and reused. Tier B
  (same input, output, tools and domain) warm starts are the common case.
- `synthetic_fake` numbers (the fake provider) prove plumbing and decision determinism,
  never quality, and never rank a real-model candidate. Evidence classes are never mixed.
- Telemetry `accepted` is shown as `proxy_completed`: the run completed, not that it was
  correct. Role outcome proxies are confounded with task difficulty, so the `evidence` role
  policy is opt-in, needs `min_samples` and switches only on non-inferiority.
- Exported packages from a host without an Ed25519 key are host-attested: their license
  verifies only where `ARCHITECT_SIGNING_KEY_FILE` names the Architect signing key that
  signed it; the generated license test skips rather than faking a pass without a key.
  Giving a CI runner the key widens trust. Another host must compile the exported Spec to
  mint its own license. `export verify --reverify` re-verifies only on the host that
  installed the entry (it runs that host's own copy, never the package's code).
- A signed package whose seals were stripped back to the HMAC also skips its license tests
  on a host without that key, so CI should run `architect verify --require-key
  <fingerprint>` on it. There is no revocation or expiry for trusted keys yet: a leaked key
  stays trusted on a host until its trust list drops it (`"shipped": false` for the key
  shipped with Architect).
- In the session, `forge integrate` needs `approve <hash8>` first, and `approve` always runs
  a normal paid compile (the only place approval is recorded); `forge integrate` then builds
  again with Forge. The session help and the `approval_required` hint say so. The CLI
  (`--approved-spec`) has no such double spend.

## Synthetic examples are not production evidence

Report-card replay uses a synthetic 200-row edit fixture. Content acceptance is
synthetic. The digitizer pipeline uses scripted extraction/solver/verifier
responses, not actual live uploaded-image vision. Genuine Pi process evidence
with Fake responses does not measure live coding model quality. None establishes
one-month edit-rate improvement, model quality, real customer acceptance, or
month-long cost savings. Real consented data, calibrated floors, invoices and
operational observations are still needed.

## Validation and release status

The repository runs native CI on GitHub Actions for
uv/Python 3.12 on Linux, Node 22, pinned Pi `npm ci`, TypeScript build/test, a live
local-server TypeScript e2e against a synthetic fixture, and the optional ARC extra,
which also manufactures a fresh ARC-AGI-3 harness on a random draw of public games
with a no-cost fixture agent. Those CI runs check mechanisms, not model quality or
competition results. The same workflow runs strict types, the Python tests, a
fake-provider bench smoke and the TypeScript tests on Windows, and `desktop.yml` runs the
desktop app's tests on macOS, Windows and Linux. Local full-suite runs have been on macOS
and on Windows, the original development platform.
