# Phase 6 games — proposed interfaces and ownership

Status: implemented. Drafted before Phase 6 as the planning contract; the
integrator-approved amendments below are what shipped. Executed evidence lives
in `PROGRESS.md` and `docs/reviews/`, not here.
Sources: full `docs/build-prompt.md` Phase 6 and product spec;
`docs/research.md` (official ARC SDK 0.9.9 and gateway restrictions);
`docs/interfaces/token-policy.md`; current compiler, runtime, floor, orchestrator,
and dogfooded `architect/games/actions.py`.

## Non-negotiable boundaries

- Exactly three **local toy games**, plus a separate optional official adapter.
  The SDK exists. Missing SDK/files/gateway is an explicit error, never a toy fallback.
- Only new preset: `hypothesis_tester`, owned by the integrator. Game agents use
  `explorer`, `planner`, or `hypothesis_tester`. The installed competition harness
  specializes existing `planner`; do not add a competition preset or extra agent roles.
- Budget means `Budget(cost_usd, compute_s)`. No token quotas at any level.
  Tokens remain observed efficiency data. Normal model calls use output cap `None`.
  Bound bytes, frames, actions, turns, candidates and recursion separately.
- Offline development may use large models and Stage 3 to compile the parent.
  Submission runtime uses only a frozen, explicitly allowed small offline model
  for Stage 0 inference, Stage 1 selection, Stage 2 tuning and Stage 4 verification.
  Stage 1/4 gates remain code. Set `enable_rewriting=False`, `max_rewrites=0`,
  `rewrite_adapter=None`; reject rewritten/sandbox warm starts at runtime too.
- All work shares the parent dollar cap and monotonic deadline: setup, probes,
  inference, candidate evaluation, fresh verification, child compilation and play.
  Losing, failed and cancelled work still costs resources. Game-action bounds are
  not compute bounds. Current numeric Kaggle caps and runtime-tool permission are
  **unverified**. Internal licensing never certifies competition compliance.

## Module split and contracts

New wire schemas use frozen, extra-forbidden Pydantic v2 models, finite numbers,
strict integers (reject booleans), bounded collections and fixed error codes.
Protocol objects/capabilities are not JSON and are never model-supplied.

### A. `architect/games/types.py`, `local.py`, `official.py`

```python
class GameAction(BaseModel):
    name: Literal["RESET", "ACTION1", "ACTION2", "ACTION3", "ACTION4",
                  "ACTION5", "ACTION6", "ACTION7"]
    x: int | None = None  # 0..63; both coordinates required only for ACTION6
    y: int | None = None

class Observation(BaseModel):
    frames: tuple[tuple[tuple[int, ...], ...], ...]
    state: Literal["NOT_PLAYED", "NOT_FINISHED", "WIN", "GAME_OVER"]
    available_actions: tuple[str, ...]
    levels_completed: int
    win_levels: int | None = None

class GameLimits(BaseModel):
    max_actions: int = 256
    max_probe_actions: int = 3
    max_turns: int = 256
    max_frames: int = 64
    max_payload_bytes: int = 2_097_152

class GameRef(BaseModel):
    backend: Literal["local", "official_offline", "official_gateway"]
    game_id: str             # bounded public identifier, not a filesystem path
    revision: str           # pinned public dataset/version identity

class GameSession(Protocol):
    ref: GameRef
    async def observe(self) -> Observation: ...
    async def step(self, action: GameAction) -> Observation: ...
    async def close(self) -> None: ...

class GameProvider(Protocol):
    async def discover(self) -> tuple[GameRef, ...]: ...
    async def open(self, ref: GameRef, *, limits: GameLimits) -> GameSession: ...
```

All limits above are local safety defaults, **not Kaggle limits**. Frames retain
animation/multiple-frame observations; each grid is rectangular, <=64x64, with
palette 0..15. Enforce limits before allocating/parsing oversized payloads.
Expose only supported actions; after GAME_OVER only RESET is accepted. Never
implicitly reset on observe/probe/play. Session close is idempotent.

`ProbeEnvironment(session, limits)` implements existing
`Environment.probe(max_actions: int = 3) -> dict[str, JsonValue]`.
It caches public observations and bounded actual probe actions. A second call
returns the cache, not new actions. No state/solution/source inspection. Probe
moves count against the same session's action limit and parent time.

`LocalGameProvider(manifest: LocalFixtureManifest | None = None)` loads the frozen default manifest when omitted and exposes exactly these three rule families:
- `local-navigation-v1`: reach a goal through walls, directional actions.
- `local-switches-v1`: discover a small deterministic switch/door rule.
- `local-sequence-v1`: learn a short visible feedback sequence.

All are small seeded grid games with explicit local labels and local-only win
score 0 or 1. Distinct revision/seed manifests define fixtures. Never call these
ARC benchmark results. Preserve the dogfooded
`parse_actions(value, allowed, maximum) -> tuple[str, ...]` unchanged as the local
legacy string-batch decoder. Official coordinates require the new typed decoder;
do not overload legacy parsing or silently translate malformed actions.

`OfficialConfig(mode, staged_game_dir?, gateway_url?, sdk_version="0.9.9")` has
explicit modes only. Lazy-import `arc_agi`; map real `Arcade`, `get_environments`,
`make`, `env.action_space`, `env.step(GameAction, data=...)`, and public responses.
Pin `arc-agi==0.9.9` in an optional extra; choose and lock a tested compatible
`arcengine` version separately before claiming reproducibility. No default NORMAL
mode with implicit downloads. Offline mode requires staged public game files.
Gateway mode uses the documented official competition transport, not hidden game
Python. Restrict network to the explicit local gateway; models remain offline.

Competition session manager owns one scorecard and at most one `make` per game.
Enforce level-reset semantics, all-environment coverage and no in-flight scorecard
reads. Scorecard finalization belongs to trusted evaluation, not model tools.
Transport must time out under the outer deadline. A synchronous SDK call in a
thread is not a hard-stop boundary: use a killable worker or cancellable transport.
Never inspect private engine internals. Optional real smoke tests are separate
from fake-transport CI. Freeze wheels, public files and model weights offline.

Errors: `GameUnavailableError`, `GameProtocolError`, `GameActionError`,
`GameSessionError`; messages use fixed codes and never response content.

### B. `architect/games/execution.py`, `evaluation.py`, `factory.py`

```python
class GameFactoryConfig(BaseModel):
    budget: Budget
    compile_config: CompileConfig  # runtime policy above is validated, not advisory
    small_model: str               # exact locally configured binding
    limits: GameLimits

class GameRunResult(BaseModel):
    accepted: bool
    compile_result: CompileResult | None
    play_result: RunResult | None
    actions_used: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    wall_time_s: float             # includes compile/setup, not just delivery
    failures: tuple[str, ...]

class GameFactory:
    def __init__(self, compiler: Compiler, provider: GameProvider,
                 config: GameFactoryConfig) -> None: ...
    async def run(self, target: GameRef, *,
                  support_dataset: str) -> GameRunResult: ...

async def execute_game(context: RunContext, task: JsonValue, *,
                       preset: str, params: dict[str, JsonValue],
                       components: tuple[str, ...]) -> JsonValue: ...

class GameScorer:
    async def score(self, task: JsonValue, output: JsonValue) -> float: ...
```

`GameFactory.run` is probe -> `Compiler.compile(Environment, overrides=...)`
(Stage 0/1/2/4) -> authenticated `Runtime.run` -> close/cleanup. Start one wallet
and deadline **before** opening/probing. Host overrides pin ephemeral lifecycle,
game schemas/tools, small model, support floor and dollar/time bounds. Validate
that inference selects only one of the three allowed presets; never allow a
model to grant an environment, pick held-out data or raise a cap. Cache reuse is
only a warm start, with a new report/verification_id. No delivery on failed floor.
One ephemeral artifact covers the entire multi-turn game, not one artifact per
move; preserve run-once consumption and generated-code cleanup.

A scoped, expiring game capability on Runtime/RunContext drives environment
steps. The generated existing preset harness calls `execute_game` only when a
trusted game grant is present. The executor calls `context.call` for bounded
adaptive turns, validates the action, executes it, and feeds back public frames.
No executable model output. Hypotheses/plans remain data. Runtime must reject
missing/expired/wrong-owner grants; JSON game IDs are not capabilities.

Tuning and verification must execute these real rollouts, not score a proposed
list of IDs or a model-authored score. Trusted execution issues an opaque,
single-use evaluation receipt bound to run, case, candidate digest, session and
observed result. `GameScorer` validates the receipt and reads trusted results;
forged, replayed or mismatched receipts fail. Models never issue receipts.
Keep results in scoped memory; discard frames/receipts after scoring.

Use existing `HeldOutGameScore(definition, records, environment=GameScorer(...))`.
No fifth floor. Local normalized scores are win fractions. Official results use
the final official scorecard and its documented Relative Human Action Efficiency
metric, not an invented binary approximation. Existing floors require [0,1], so
record an explicit versioned conversion of the documented official score range
in the dataset manifest (including any percentage scale and 1.15 allowance).
Raw scores stay labeled separately. Reject unknown scales; test conversion with
real documented scorecard fixtures before enabling official licensing. Do not
request scores mid-flight or replay hidden evaluation through extra `make` calls.

#### Isolation across nested compilers

- Preserve private Stage 4 authority; no game module imports verifier credentials.
- Split complete game identities/revisions into tuning/held-out before compilation,
  not different episodes of the same preview game. Current floor splits sorted
  case IDs even/odd: create one case per game or add a trusted group-aware split
  in the shared floor gate. Assert disjoint game identity sets.
- Child support datasets are separate public preview games, disjoint from parent
  held-out games and the current scored target. Freeze them before parent tuning.
- During parent Stage 4, the held-out target may be exposed only to the child's
  **play** loop. Never pass its ID/frames/results into child Stage 0/2, library
  writes, tuning, or rewrite. Child compilation probes a disjoint support game;
  the trusted evaluator binds the unseen target only after fresh child Stage 4.
  This restriction deliberately prevents "runtime adaptation" from becoming a
  nested held-out-data leak. Normal non-held-out target probing remains allowed.
- A target known to be reserved by an evaluation manifest cannot be used as a
  compile/probe source. No floor assets, solutions, expected outputs or trusted
  receipt state enter model prompts, artifacts, telemetry or cache keys.
- Stage 4 results may update aggregate drift scores, not feed failure-specific
  held-out observations back into tuning. Restrict library reuse during isolated
  verification so earlier held-out rollouts cannot teach later ones.

### C. `architect/games/competition.py`, `examples/arc/competition.py`

```python
class CompetitionDescription(BaseModel):
    name: str
    rules: str                    # bounded operator content; never telemetry
    action_schema: dict[str, JsonValue]
    observation_schema: dict[str, JsonValue]
    preview_dataset: str           # references sealed game-level partitions
    child_support_dataset: str
    compute_cap: Budget            # explicit local operating cap, not inferred Kaggle cap
    small_model: str
    submission_constraints: tuple[str, ...]
    rules_status: Literal["unverified", "owner_confirmed"] = "unverified"
    max_games: int                 # safety bound; cannot silently skip official games

class CompetitionFactory:
    async def compile(self, description: CompetitionDescription, *,
                      approved: bool = False) -> CompileResult: ...
    async def install(self, result: CompileResult, *, name: str) -> RegistryEntry: ...
    async def run(self, name: str) -> CompetitionRunResult: ...
```

`CompetitionRunResult` contains `accepted`, ordered `GameRunResult` children,
aggregate numeric usage, official/local score metadata and fixed failure codes.
Construct with a development `Compiler`, `Registry`, runtime `GameFactory`, and
provider. Parse the file strictly; installed inference returns approval-required
before compile, per existing contract. Parent may use large development models
for inference/rewrite, but emitted installed `planner` artifact is bound to the
small offline runtime model and tested with it. Freeze its declarative delegation
configuration and include it in artifact digest/authentication. No new preset.

The installed harness's actual execution invokes a host-owned bounded
`spawn_game(target)` capability. Each call runs a real GameFactory child with its
own compilation, fresh Stage 4 report and authenticated Runtime delivery. Child
results are collected from executions, never model claims. Use sequential child
execution initially; this is still genuine delegation, not returning a plan.
One parent wallet/deadline contains all children. Child budgets cannot exceed
remaining dollars/time. Reject recursion beyond competition -> game, cloud model
bindings, online compilation, Stage 3 or child-installed lifecycles.

Parent licensing uses **HeldOutGameScore on complete preview-game rollouts of this
same delegation path**. Stage 2 uses tuning games. Stage 4 uses held-out games
and the isolation rule above. The score measures child play; validating a game ID
or a decomposition is never a quality floor. Each evaluation case includes child
compile and play time/cost. Missing games, unexecuted children, cap exhaustion,
forged scores or failed child licenses fail the parent floor. Official-mode
coverage uses a complete scorecard session; no cherry-picked subset.

Use Registry versioning, acceptance, drift monitor, recompile and rollback. Preview
scores from trusted execution drive drift. Parent offline recompile may use larger
models again; replacement still requires a new, better verified report. The
example script exposes compile/install/run, writes metadata-only example telemetry,
and labels local results, official results and unverified compliance separately.
No submission/upload automation or competition-compliance assertion in Phase 6.
Public development evaluation sessions are distinct from the single submission
gateway session. Do not run repeated Stage 2/4 trials against that gateway or
pretend its all-environment constraint permits per-candidate selective scorecards.
Use staged disjoint public preview evaluation resources. If final official scores
cannot be obtained with these constraints, official licensing fails unavailable;
only the clearly labeled local test path is accepted until this is resolved.

## Integrator changes needed (not owned by module workers)

1. Add only `hypothesis_tester` to preset schemas/registration, inference description
   and deterministic selection. Preserve domain `game`; use explicit host-validated
   game strategy/config, not new pseudo-domains or unrestricted schema fallbacks.
2. Add scoped game/delegation grants to Runtime and authenticated preset dispatch.
   Extend artifact digest for any new frozen config files before using them.
3. Make nested factory usage visible in enclosing RunResult and per-case budget
   measurements. Current wallets enforce shared model costs, but `RunContext`
   counters do not automatically include nested `Compiler`/`Runtime` calls.
   Aggregate actual descendant usage once; do not sum parent+child telemetry twice.
   Charge non-model setup/tool costs explicitly and apply deadline to scoring too.
4. Wire trusted rollout receipts/scorer context into `evaluate_cases`. Scorecard
   finalization currently occurs after runtime evaluation; its latency must be
   in the same evaluation budget. Keep held-out gate and signing in existing owners.
5. Enforce game-group disjointness and nested-evaluation restrictions in the trusted
   evaluation boundary. Current environment protocol alone cannot enforce them.
6. Add runtime compiler policy validation (including cache/model allowlists),
   optional pinned SDK dependencies, registry wiring, CLI/example integration and
   docs. HTTP server and TS client remain a separate workstream; no game transport
   additions to the four-method client API.

## Ownership and acceptance proposal

- Adapter worker: A files plus adapter/local tests. Do not edit dogfooded actions.
- Game worker: B files plus GameFactory/execution tests. Integrator supplies grants.
- Competition worker: C files plus competition tests, after B contract is wired.
- Integrator: shared compiler/runtime/presets/floors/schema/registry/packaging and
  hypothesis_tester. Separate verification/review workers test/report only.

Tests: three local games compile and play with fake models; legal/illegal actions,
coordinates/multi-frame/state bounds; official absence never falls back; SDK/gateway
single-make/reset/scorecard constraints; actual wrong play fails both child and
parent floors; cached children receive fresh Stage 4; held-out probe/receipt leaks
fail; parent cap stops setup/compile/play and settles failed usage; high-token free
calls are observed, not rejected; no Stage 3/cloud calls at runtime; installed
parent really executes multiple children; approval/registry/drift gates persist;
telemetry contains no frames, actions, prompts or free-text rules. Optional official
smoke tests do not replace deterministic CI or establish submission permission.

## Integrator accounting clarification

Nested factories must report aggregate descendant model usage in `RunResult` and candidate measurements. Raw telemetry continues to bill only each row's own work. Extend the task-local wallet with separate input/output counters if needed; do not sum parent aggregate results into child telemetry again. Keep failed reservations distinct from consumed usage.

Official score finalization must happen inside the trusted game executor and its deadline before it issues a receipt. The floor scorer consumes the already-complete receipt; it does not perform unbudgeted network work. This does not move a separate LLM judge's grading cost into the manufactured agent's per-run budget.

Bind game execution to the original runtime task and candidate identity. A rewritten harness cannot select another game by substituting a task argument. Capabilities must use a revocable lease, not only ContextVar reset.

## Local identity amendment (integrator approved)

Three rule families are not enough independent instances for both a parent floor and a disjoint child support floor. The local provider therefore exposes a frozen manifest with five distinct puzzle instances per family: parent tuning, parent held-out, child support tuning, child support held-out, and delivery. This is 15 instances of the same three games, not 15 new rule families. Parent and support floors each contain six instances; delivery has three separate targets.

The provider recomputes an instance fingerprint from canonical actual puzzle data and a trusted rule-definition digest. Include geometry, initial state, transition parameters and goal. Exclude caller IDs, revision labels, seeds, partition labels, episode counters and cosmetic rendering. Reject duplicate fingerprints across all manifest roles, including aliases. Resets retain the same identity. Canonicalize unordered fields and known cosmetic aliases; do not claim a general semantic-equivalence detector. Private puzzle data stays in the trusted provider, not model input.

Validate the actual even/odd floor split against the manifest, including complete-instance disjointness. A seed or caller revision label is never evidence of independence. This tests unseen **puzzle instances**, not unseen rule families, ARC benchmark performance, or competition compliance.

## Official development manifest and score completion

The official adapter now cites `https://docs.arcprize.org/methodology.md` and the close-scorecard API documentation: final totals are 0..100 percent; the 1.15 allowance is per level, not a 115-point final maximum. Implement a versioned completed-card `raw / 100` conversion against the documented field, reject nonfinite/bool/out-of-range values, and freeze the official documentation example plus clearly synthetic boundary cases. These fixtures are protocol evidence, not live benchmark results. Unknown or nonfinal fields still fail unavailable.

GameFactory must also accept an explicitly supplied trusted OFFLINE preview manifest through an optional `manifest` constructor argument. The common manifest API is refs(role), identity(ref), and digest. LocalFixtureManifest remains the default for the local provider. Official manifests bind pinned public provider references and complete game identities; changing a revision label cannot split the same official game across roles. Validate provider discovery/ref matching before use. Never substitute local puzzle scores for official scores. Gateway submission sessions remain unsuitable for repeated candidate trials. Public OFFLINE development uses separate repeatable episode scorecards.

Absence of staged public files can block a live smoke test; it is not a reason to leave documented normalization or generic OFFLINE factory integration unimplemented.

## Parent floor and offline rewrite amendment

`CompetitionDescription.floor_limit` is an operator-selected finite number in 0..1, default 1. It is bound into `CompetitionPolicy` and the exact approved Spec; inference cannot lower it. Public discovery may contain unrelated games, but every pinned reference must match and duplicate identities still fail.

Offline parent rewriting uses a distinct fixed smoke evaluator, never the primary evaluator object. It may replay a frozen parent-tuning regression set; this is not an independent generalization claim. No parent-held-out case is exposed to rewriting. A separate floor object alone does not permit any model-selected smoke data. Runtime children still prohibit rewriting.

Deadline expiry stops accepted work, not mandatory resource cleanup. Providers must kill/reap owned workers and clear episode/temp state even after the execution deadline. Cleanup has a small bounded grace and actual elapsed time remains in accounting; it never authorizes further game/model work.
