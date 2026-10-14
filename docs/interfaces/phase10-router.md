# Phase 10 "Router": verified-version routing for tasks, subagents and Architect's own roles, plus Forge `run` and `integrate` delivery modes

Status: implemented (WP1 to WP5), with one Section 15.5 gate open: the bench-smoke baseline must be regenerated from the Linux CI job's uploaded rows (image-task page manifests differ by platform, so no other host can produce them); until then the identical-scores compare step fails. Line references are to commit `29f9dcd`, where this contract was written. Where this text and the code differ, the code, `docs/usage.md` and `docs/bench.md` are authoritative. Implementation notes: a compile routes by default only when the profile declares a `routing` table (or the operator grants reuse for that call), so the default experience is Phase 9's; under the default `pinned` role policy the `forge agent:` pre-line is printed by `forge run` and `forge integrate` (and by other Forge consents only under `evidence`). Session compiles route with the `session` origin, and every delivery step honours the same grant the router reused under (the per-call grant or `routing.reuse_origins`). A recompile (CLI or session) never reuses an installed entry, even under a standing grant, so its acceptance always comes from a Stage 4 in that call; it may still warm-start. Outside the states that offer them, the new session verbs stay prose, and READY accepts only `roles`, `rate`, `rate good` and `rate bad`. The bench CLI runs `--variant routed [--rounds R]` and `bench roles --policy`. The Tier A key compares latency floored to 0.1 s, so clock-tick noise never decides between tied versions (pass criterion 4). This document extends:

- `docs/interfaces/phase6-http.md` (amended in Section 16)
- `phase6-runtime.md`
- `phase7-session.md` (amended)
- `phase8-bench.md` (amended)
- `phase9-forge.md` (Sections 11 and 12 amended; everything else unchanged)

Where this text and the code differ after implementation, the code, `docs/usage.md` and `docs/bench.md` are authoritative.

This is the contract for four implementers working in parallel on disjoint files (WP1 to WP4) plus one integration package (WP5); see Section 18. Everything below is fixed: every public signature, file path, closed label, formula, error code, JSON shape and operator-facing string. Anything not listed is an implementation detail owned by the file's author.

## How this answers the direction

> "architect should be a harness that switches between multiple optimized versions depending on the task (both for itself and its subagents) with forge as a mode that creates a harness either to run as an agent on the spot or to integrate as software."

- **Switching between versions for tasks.** One deterministic router sees every incoming task: session, CLI, HTTP, orchestrator subtasks, Forge children, the builder and the Forge modes. It picks among the verified versions that already exist: registry revisions, library snapshots, and a fresh compile as the fallback. Each pick is explained, and it rests on task fit plus measured evidence.
- **Switching versions for Architect itself and its subagents.** A role router picks among profile-declared variants (model plus closed parameters) for these roles: Stage 0 inference, goal decomposition, rewrite proposals, the Forge agent and floor drafting. The default policy `pinned` is byte-identical to today's behavior. The policy `evidence` switches only on non-inferiority evidence.
- **Forge as a mode, two deliveries.**
  - `forge run` builds an ephemeral harness, lets Stage 4 license it, runs it once and expires it.
  - `forge integrate` builds, licenses and installs the harness. It then exports it as a self-contained package with pins, content-free evidence, generated tests and a thin shim, and places it in a repository only through the guarded FeatureBuilder/Pi path, behind a disabled flag and with human review.

Routing never grants a license. Stage 4 remains the only licensing authority.

## Provenance

This contract starts from the judges' unanimous winner, "an evidence router over verified harness versions (for tasks and for Architect's own roles), plus Forge run and integrate delivery modes". It grafts in the following:

- Evidence labels move out of the bench into `architect/routing/labels.py`, and the bench re-imports them, so the bench stays a leaf.
- A pure $0 router and role-policy simulation runs as a CI assertion.
- Proposal-only lifecycle outputs: `route_promote_proposed` and `route_rollback_suggested`.
- Export hardening:
  - an export consent line;
  - a post-placement and post-run re-hash against the manifest;
  - host-only `--package-only` placement;
  - a flag that is off by default in the package;
  - a generated wrong-model refusal test.
- The Forge-agent variant is chosen before the Phase 9 consent line prints. Exploration never changes a consented cap.
- The Forge fallback is suggest-only. The router never arms Forge.
- `evidence_store_unavailable` is fail-open: routing problems never block a compile.
- Evidence classes are never mixed in an aggregate. Synthetic floor origins are down-weighted and labelled.
- An optional content-free human rating: session `rate good|bad`.
- Closed per-role parameter variants.
- The session typed-token grant `reuse <name> <hash8> {json}`.
- `BenchRow.artifact_digest`.
- `_discard_code` moves to a shared module.
- A multi-round learning-curve bench and a regret table.

Corrections to the design text:

- The sandbox worker spawn (`sys.executable -I -m architect.runtime._sandbox_worker`) is at `runtime/sandbox.py:1260`.
- `Registry.observe` has one caller, `Registry.monitor` (`registry/__init__.py:391`), whose only caller is `games/competition.py:648`.
- `PiBackend` already accepts `capabilities: Mapping[str, TrustedTool]` (`backends/pi.py:518`), so `backends/pi.py` is not modified.
- `FloorEvaluator` splits cases itself, sorted by id, even/odd (`floors/base.py:110-135`), and callers cannot choose partitions. User-supplied cases therefore use that split and no new one.
- `Services` has no `library` attribute; the library is `services.compiler.library`.

Rejected alternatives:

- **A single Beta-posterior utility with hand-set cost and latency weights.** It is opaque, and on thin evidence it is not better than a lexicographic rule.
- **Evidence-driven override of the Stage 1 preset order.** `docs/spec.md` forbids a fixed task router, and Stage 1 stays rule-based.
- **Automatic exploration spend on internal roles by default.**
- **A key-less "digest-pinned" run mode for exported packages.** It would weaken the single license check.

---

## 1. Fixed properties

- **P1 (Stage 4 only licenses).** No routing component, record or score ever writes `approved=`, `_approved_hash`, a `TestReport`, a signature, or calls `Registry.install`. The only exception is the Forge-mode host code in Section 12, which calls `Registry.install` exactly as `FeatureBuilder` and `cli._compiled` do today.
- **P2 (fresh re-check).** A `RouteDecision` holds references only: `(name, revision, artifact_digest, spec_hash)` or `(library key, artifact_digest)`. It never holds an artifact object. Every execution re-loads from disk and checks, in order:
  1. `HarnessArtifact.load(dir).licensed`;
  2. digest equality with the decision;
  3. `spec_hash` equality with the decision;
  4. slot equality with the probe.
  
  `Runtime.run` then re-checks the license again.
- **P3 (reuse is granted, never assumed).** Installed reuse happens only for an origin granted in this invocation:
  - CLI `--reuse-installed`;
  - session `reuse <name> <hash8> {json}`;
  - `route --run ... --yes`;
  - the profile `routing.reuse_origins`.
  
  HTTP and session `route` are advice only.
- **P4 (pinned default).** With the default profile (`roles.policy = "pinned"`, `routing.explore_every = 0`), every role adapter is identical to today's binding, and no exploration occurs.
- **P5 (fail-open).** If the route log, evidence cache or indexes cannot be read, the result is action `compile` with code `evidence_store_unavailable`, and the Compiler behaves exactly as it does today. Routing never blocks a compile.
- **P6 (content-free).** Route, outcome, role and rating records hold only these things, and never task text, tokens, prompts, outputs, prose reasons, raw registry names or paths:
  - closed literals;
  - numbers;
  - UUIDs;
  - `h_`/sha256 categories.
- **P7 (no mixing).** Each aggregate is keyed by evidence class and never sums across classes. `synthetic_fake` evidence never ranks a non-fake candidate.
- **P8 (Forge is opt-in).** The router may emit `route_forge_suggested`. Forge is armed only through the existing Phase 9 consent path (`--forge`, `forge on`, or the explicit `forge run` and `forge integrate` commands).
- **P9 (no ungated repo writes).** `forge integrate` writes into a repository only after four gates, in this order:
  1. spec approval;
  2. Forge spend consent;
  3. a Stage 4 license;
  4. export consent.
  
  After those gates, it writes either through the single-use trusted Pi tool or through `--package-only` host placement of one new directory. `--stage-only` writes nothing into the repository.
- **P10 (no live or GPU calls in tests).** Every test uses fake adapters and blocks sockets. The only local GPU use in this project remains the ARC experiment (Phase 9 Section 14). Phase 10 adds no Ollama or live runs.

## 2. Vocabulary

- **Version**: a licensed artifact the router can point to. This is an installed registry revision, or a library template, which is a pre-consumed snapshot and usable only as a warm start.
- **Probe** (`TaskProbe`): one routing request with its origin, Spec and budget cap.
- **Slot**: exact task identity, meaning the `Registry._beats` field set (`registry/__init__.py:132-135`).
- **Shape**: the I/O identity. This is the library `_compatibility` key (`library/__init__.py:36`) with the model removed.
- **Family**: a structural I/O skeleton. It is advisory only.
- **Tier A, B, C**: fit tiers for slot, shape and family matches (Section 7).
- **Role**: one of Architect's own model uses: `inference`, `decomposition`, `rewrite`, `forge_agent`, `floor_draft`. The judge is not a role (Section 11.1).
- **Delivery mode**: `ForgeDelivery = Literal["run", "integrate"]`. This is distinct from `ForgeSettings.mode` (`agent|template`, `compiler/forge_settings.py:23`), which is unchanged.
- **Evidence class**: `synthetic_fake | local_zero_cost | live_api_equivalent | live_billed`, derived from the model's provider.

## 3. File list and owners

| Path | Owner | New or modified | Responsibility |
|---|---|---|---|
| `architect/spec/binding.py` | WP1 | new | `copy_subharness_tree`, `check_subharness_tree` (moved from library) |
| `architect/registry/__init__.py` | WP1 | mod | install copies `subharnesses/` and `forge.json`; `slots` index; `find`; `drift` |
| `architect/library/__init__.py` | WP1 | mod | helpers re-imported from `spec.binding`; `shape` column; `candidates`; `ReadOnlyLibrary` |
| `architect/routing/__init__.py` | WP1 | new | re-exports (Section 4) |
| `architect/routing/labels.py` | WP1 | new | `Provider`, `EvidenceClass`, `FloorOrigin`, `EvidenceLabel`, `evidence_class(provider)` |
| `architect/routing/signature.py` | WP1 | new | `TaskSignature`, `signature`, `schema_skeleton`, `fit` |
| `architect/routing/evidence.py` | WP1 | new | `EvidenceIndex`, `VersionEvidence`, `RoleStats`, incremental telemetry cache |
| `architect/routing/router.py` | WP1 | new | `RoutePolicy`, `route`, `Router`, `RoutedLibrary`, `load_reuse_target` |
| `architect/routing/log.py` | WP1 | new | `RouteRecord`, `OutcomeRecord`, `RoleRecord`, `RatingRecord`, `RouteLog` |
| `architect/routing/proposals.py` | WP1 | new | promote and rollback proposals |
| `architect/routing/roles.py` | WP2 | new | `RoleName`, `RoleVariant`, `RoleRouting`, `RoleRouter`, `RoleAdapters`, `RoleChoice` |
| `architect/profiles.py` | WP2 | mod | `Profile.routing`, `Profile.roles`, `Services.router`, `Services.roles`; `WORKSPACE_STATE += ("routes", "exports")` |
| `architect/runtime/ephemeral.py` | WP3 | new | `EPHEMERAL_CODE`, `discard_code` (moved from `orchestrator/__init__.py:69,222`) |
| `architect/forge_modes/__init__.py` | WP3 | new | re-exports |
| `architect/forge_modes/run.py` | WP3 | new | `forge_run`, `deliverable_ephemeral`, `ForgeRunResult` |
| `architect/forge_modes/cases.py` | WP3 | new | `load_cases`, `cases_floor` |
| `architect/forge_modes/integrate.py` | WP3 | new | `forge_integrate`, `IntegrateResult`, `IntegrateOptions` |
| `architect/forge_modes/export.py` | WP3 | new | `export_package`, `place_package`, `verify_export`, `ExportManifest`, `ExportCheck` |
| `architect/forge_modes/shim.py` | WP3 | new | `ExportedHarness`, `FeatureDisabled` (imported by exported packages) |
| `architect/forge_modes/place_tool.py` | WP3 | new | `place_package_tool(...) -> TrustedTool` |
| `architect/forge_modes/templates/{init.py.tmpl,test.py.tmpl,README.md.tmpl,EVIDENCE.md.tmpl}` | WP3 | new | fixed templates; only pin constants are substituted |
| `architect/orchestrator/features.py` | WP3 | mod | `create(..., integrate: IntegrateOptions \| None = None)` |
| `architect/bench/{rows,providers,suite,runner,compare,render}.py` | WP4 | mod | `routed` variant, `RouteFields`, probes, oracle, re-imported labels |
| `architect/bench/router_eval.py`, `architect/bench/router_sim.py`, `architect/bench/ci_assert.py` | WP4 | new | regret and learning curve; pure simulation; CI assertions |
| `examples/bench/suite-v1/siblings.json`, `examples/bench/roles/*.jsonl` | WP4 | new | committed fixed probe paraphrases and fake role logs |
| `architect/compiler/__init__.py`, `architect/compiler/types.py` | WP5 | mod | router pre-step, `CompileResult.route` and `.reused`, `RoutedLibrary` injection, role seats |
| `architect/compiler/forge_tools.py` | WP5 | mod | `_spawn` uses `ReadOnlyLibrary` when `routing.child_warm_start` |
| `architect/compiler/rewriting.py`, `architect/compiler/forge.py` | WP5 | mod | role seats (rewrite, forge_agent) |
| `architect/orchestrator/__init__.py` | WP5 | mod | `_deliverable_routed`, discard guard, reuse grant, subtask models, `discard_code` import |
| `architect/session/{commands,session,codes,floors}.py` | WP5 | mod | verbs `route`, `reuse`, `roles`, `rate`, `export`, `forge run`, `forge integrate`; codes; floor_draft seat |
| `architect/cli.py` | WP5 | mod | `route`, `forge run`, `forge integrate`, `export verify`, `roles`; `--reuse-installed`, `--no-route` |
| `architect/server.py`, `client-ts/` | WP5 | mod | `POST /route`; `_FAILURE_CODES`; TS `route()` |
| `tests/test_package_boundary.py` | WP5 | mod | routing boundary (Section 17.6) |
| `docs/usage.md`, `docs/limitations.md`, `docs/bench.md`, phase amendments | WP5 (bench.md: WP4) | mod | Section 16 |

Package boundary rule: `architect/routing/**` imports only these packages:

- `architect.spec`, `architect.registry`, `architect.library`, `architect.telemetry`, `architect.models`, `architect.presets`;
- the standard library and pydantic.

It never imports `architect.compiler`, `profiles`, `orchestrator`, `session`, `server`, `cli`, `bench`, `backends`, `forge_modes` or `games`. `RoutedLibrary` satisfies the `LibraryLookup` protocol structurally, and the profile is passed in as plain mappings.

---

## 4. Prerequisite fix: installed Forge artifacts keep their children (WP1, first commit)

Defect, found by reading the code; no test demonstrates it yet:

- `Registry.install` copies only `harness.py`, `spec.json`, `config.json` and `report.json` (`registry/__init__.py:274`).
- `run_subharness._load` reloads children from `parent.directory / "subharnesses" / name` (`runtime/subharness.py:36-41`).
- So an installed Forge parent with children passes `_authenticated`, but fails at its first child call with `subharness_digest_mismatch`.

Fix:

1. Move `_copy_subharnesses` and `_check_subharnesses` (`library/__init__.py:63-95`) unchanged into `architect/spec/binding.py`, with these signatures:
   ```python
   def copy_subharness_tree(source: Path, destination: Path) -> None   # links anywhere in the tree: ValueError("invalid_subharness_tree")
   def check_subharness_tree(artifact: HarnessArtifact) -> None       # every bound child present, licensed, digest and spec_hash equal; recursive
   ```
   The library re-imports them under the old private names so its behavior is identical.
2. In `Registry.install`, after the 4-file copy and inside `temporary`:
   - copy `forge.json` if present (fsync);
   - call `copy_subharness_tree(candidate.directory, temporary)`;
   - after `_authenticated(temporary)`, call `check_subharness_tree(copied)`.
   
   Any failure raises `RegistryError("registry_subharness_missing")` before `os.rename`.
3. `_entry_fits` is applied to the deepest copied path.

Regression test `tests/test_registry_forge_children.py`:

- Build a fake Forge parent with one licensed child using the Phase 9 test helpers.
- Call `Registry.install`, then `Registry.get`, then run through `Runtime.run` with a fake adapter. The run must succeed.
- A tampered child gives `registry_subharness_missing`.

This commit lands before any WP3 export work is merged.

---

## 5. Task signature (`architect/routing/signature.py`)

```python
H(x) = sha256(canonical_json(x)).hexdigest()[:32]        # canonical_json = sort_keys, (",", ":"), ensure_ascii=False
norm(t) = casefold(strip(NFKC(t)))                          # the same normalization as telemetry.categorical

class TaskSignature(BaseModel):   # frozen, extra="forbid"
    slot: str       # 32 hex
    shape: str      # 32 hex
    family: str     # 32 hex
    token_count: int
    tokens: frozenset[str] = Field(exclude=True, repr=False)   # memory only; never dumped, logged or hashed

def signature(spec: Spec) -> TaskSignature
def schema_skeleton(schema: JsonValue) -> JsonValue
def fit(probe: TaskSignature, probe_spec: Spec, other: TaskSignature, other_spec: Spec) -> tuple[Literal["A", "B", "C", "none"], float]
```

The signature fields are:

- `slot = H({"task": norm(spec.task), "input_schema", "output_schema", "domain", "tools": sorted(set(tools)), "floor", "secondary_floor"})`. The floors are dumped with `mode="json"`. This is the `_beats` field set; model, budget, lifecycle, preset_hint, pipeline and execution_config are excluded.

  **Contract note:** `_beats` compares `task` without normalization. `slot` equality is necessary for reuse. The executor (Section 10.1) additionally requires exact `_beats`-field equality between the probe Spec and the loaded artifact's Spec, so a normalization-only difference routes as advice but never reuses.
- `shape = H({"domain", "in": schema_sha256(input_schema), "out": schema_sha256(output_schema), "tools": sorted(set(tools)), "pipeline": [step.model_dump(mode="json")]})`.
- `family = H({"domain": norm(domain), "in": schema_skeleton(input_schema), "out": schema_skeleton(output_schema)})`. `schema_skeleton` keeps only `type`, `properties`, `items` and `required`, recursively, and drops everything else.
- `tokens = set(re.findall(r"\w+", norm(task)))`. This is the same token rule as `library._distance`.

A signature requires a Spec. Prose is routed only after Stage 0 has run, and Stage 0 is capped at the existing $1.00 `_INFERENCE_BUDGET`. `architect route <spec.json>` and `POST /route` accept only a Spec, so they spend $0.

---

## 6. Candidates and hard filters

### 6.1 Candidates

Candidates are enumerated per probe from disk, and licenses are never cached across decisions.

1. **installed**: the current revision of each registry name whose indexed `slot` or `shape` equals the probe's, from `Registry.find(slot=..., shape=...)`. Older revisions are not candidates; they appear only in rollback proposals (Section 8.5).
2. **template**: `HarnessLibrary.candidates(shape, model)` rows that are not evicted, for each model in the probe's ladder. The ladder is `(spec.model_target,)` unless it is `"cheapest that passes"`, in which case it is `config.model_ladder`, the same rule as `compiler/__init__.py:222`. An authenticated load happens only for the top `policy.max_candidates` rows per model.
3. **compile**: a virtual last resort, always present. It is not an artifact and is never filtered.

Forge is never a candidate (P8).

### 6.2 Hard filters

A filtered candidate stays in the explanation with `eligible=false` and its code. The filters are applied in this order:

| # | Applies to | Condition to pass | Code on failure |
|---|---|---|---|
| 1 | all | `HarnessArtifact.load(dir).licensed` on a fresh load; for Forge artifacts also `check_subharness_tree` | `route_unlicensed` |
| 2 | installed | `spec.lifecycle == INSTALL` and no `.consumed` | `route_consumed` |
| 3 | template | not evicted | `route_evicted` |
| 4 | all | `model in profile.models`; if `probe.spec.model_target != "cheapest that passes"`, the model must equal it | `route_model_unconfigured` |
| 5 | all | `report.cost_usd <= cap.cost_usd` and `report.compute_s <= cap.compute_s`. These are per-case maxima, the same basis licensing uses, and `cap` is `probe.budget_cap` | `route_budget` |
| 6 | installed | `Registry.drift(name) != "drifting"`; a drifting entry stays eligible as a template if its library snapshot exists | `route_drift` |
| 7 | all | the class is `synthetic_fake` only if every probe ladder model is also `synthetic_fake` | `route_synthetic_excluded` |

`Registry.drift(name) -> Literal["no_window", "stable", "drifting"]` is new, read-only and owned by WP1. It evaluates the stored `observations` window for the current revision with the same floor-drift rule as `observe` (`registry/__init__.py:376-387`) and writes nothing. It returns `no_window` when there are fewer than 2 scored observations. That is the norm outside games, because delivery rows carry `floor_score=None` and `observe` has a single caller chain ending at `games/competition.py:648`. The label `drift_window_none` states this.

---

## 7. Fit and evidence scoring

### 7.1 Fit tiers

`J = |T1 ∩ T2| / |T1 ∪ T2|` over token sets, and `J = 1` when both sets are empty.

| Tier | Condition | F | Allowed use |
|---|---|---|---|
| A | slot equal | `1.0` | installed reuse (granted only); otherwise advice |
| B | shape equal, slot different, and the template model is in the probe ladder | `0.5 + 0.4*J + 0.1*[floor.kind equal]` | warm start only (Stage 2 and Stage 4 still run) |
| C | family equal, shape different | `0.2 + 0.2*J` | advisory text "similar family exists" only; changes no action |
| none | otherwise | `0` | not listed |

A Tier A candidate's library snapshot, when one exists, is also a Tier B candidate. Token Jaccard is only a tiebreak within an already schema-equal group, not a keyword table (`docs/spec.md:69-73`).

### 7.2 Evidence per candidate (`architect/routing/evidence.py`)

All numbers come from the signed `report.json`, library counters, the telemetry cache and the route log:

- `q(kind, x) = -x` for `human_edit_rate`, otherwise `x`, which is the same as registry `_quality`.
- Quality margin: `m = q(kind, report.floor_score) - q(kind, report.floor_limit)`.
- Conservative margin: `m_lcb = m - 0.5 / sqrt(max(report.held_out_count, 1))`. `0.5/sqrt(n)` is the worst-case binomial standard error for scores in [0, 1], at z = 1.
- Secondary: `sec_lcb` is the same formula on the secondary floor, and is `0` when the spec has no secondary floor.
- Wilson lower bound: `W(s, n, z) = (p + z²/(2n) - z*sqrt(p(1-p)/n + z²/(4n²))) / (1 + z²/n)`, with `p = s/n`, and `W = 0.5` when `n = 0`.
- Template reliability: `r = W(successes, successes + failures, 1)` from the library counters. `promoted = successes >= library.promotion_reuses`, the same threshold as `install_candidates`.
- Human rating: `h = W(good, good + bad, 1)` when `good + bad >= 5`, otherwise `0.5`. It uses `RatingRecord`s joined on `sha256(artifact_digest)`.
- Expected cost: `c = report.mean_cost_usd` if present, else `report.cost_usd`, the same rule as `_rank`.
- Latency: `l` is the median `wall_time_s` of delivery telemetry rows for `(spec_hash, harness_version)` of the candidate's own evidence class, labelled `latency_measured`, when there are at least 5 such rows. Otherwise it is `report.compute_s`, labelled `latency_bound`, because that is a per-case maximum.
- Components: `k = component_count`.
- Bench join: rows under `workspace/bench/*/rows.jsonl` join on `artifact_digest` (new optional `BenchRow` field) or else `spec_hash`. They are parsed as plain JSON with only the fields `spec_hash`, `artifact_digest`, `licensed`, `held_out_score`, `evidence` and `floor_origin`, and the router never imports `architect.bench`. Joined rows are display-only (label `bench_joined`, with counts per class). They never enter a ranking key.

Evidence labels per candidate form a closed set in `routing/labels.py`:

```python
EvidenceClass = Literal["synthetic_fake", "local_zero_cost", "live_api_equivalent", "live_billed"]
FloorOrigin = Literal["user_dataset", "example_fixture", "synthetic_generated", "model_draft", "authored_cases"]
EvidenceLabel = EvidenceClass | FloorOrigin | Literal[
    "stage4_signed", "latency_measured", "latency_bound", "drift_window_none", "drift_stable",
    "bench_joined", "human_rated", "template_promoted"]
```

Weighting rules:

- Floor-origin weights apply only to role outcomes (Section 11.4). `user_dataset` and `example_fixture` get 1.0; `synthetic_generated`, `model_draft` and `authored_cases` get 0.5.
- Weighted counts feed `W`, and each weight is shown in the table.

---

## 8. Decision rule, fallback, exploration and proposals (`architect/routing/router.py`)

```python
Origin = Literal["session", "cli", "http", "orchestrator", "forge_spawn", "builder", "forge_run", "forge_integrate", "bench"]
RouteAction = Literal["reuse_installed", "warm_start", "compile", "refuse"]

class TaskProbe(BaseModel):          # frozen, extra="forbid", arbitrary_types_allowed
    origin: Origin
    spec: Spec
    budget_cap: Budget               # CLI and session: spec.budget; orchestrator: the subtask cap
    ladder: tuple[str, ...]          # models the compile would try (Section 6.1)
    grant_reuse: bool = False        # set only by the surfaces listed in P3

class RoutePolicy(BaseModel):        # frozen, extra="forbid"; Profile.routing
    enabled: bool = True
    reuse_origins: frozenset[Origin] = frozenset()     # profile-level standing grants; default none
    min_template_reliability: float = Field(0.3, ge=0, le=1)
    explore_every: int = Field(0, ge=0, le=1000)       # 0 = off
    explore_usd_per_day: float = Field(1.0, ge=0, le=100)
    max_candidates: int = Field(8, ge=1, le=32)
    promote_margin: float = Field(0.0, ge=0, le=1)
    child_warm_start: bool = False                      # Section 11.6
    subtask_models: bool = False                        # Section 11.5

def route(probe: TaskProbe, *, index: EvidenceIndex, policy: RoutePolicy, counter: int = 0,
          explore_spent_usd: float = 0.0, decision_id: UUID | None = None) -> RouteDecision   # pure, synchronous
class Router:
    def __init__(self, index: EvidenceIndex, policy: RoutePolicy, log: RouteLog) -> None
    def route(self, probe: TaskProbe) -> RouteDecision          # reads counter and today's spend from the log, calls route(), appends RouteRecord
    def outcome(self, decision: RouteDecision, record: OutcomeRecord) -> None
```

### 8.1 Decision rule

The rule is deterministic and lexicographic, with no scalar utility.

1. **Invalid probe or no configured model.** If the probe is invalid, or no ladder model is in `profile.models`, the action is `refuse` with code `route_no_candidate`.
2. **Tier A with reuse granted.** If eligible Tier A candidates exist and `probe.grant_reuse or probe.origin in policy.reuse_origins`, the action is `reuse_installed`, targeting the minimum of this key:
   ```
   (-m_lcb, -sec_lcb, -h, c, l, k, categorical(name), revision)
   ```
   This is registry `_rank` on the conservative margin. All Tier A candidates share one floor, so their margins are comparable.
3. **Tier A without a grant.** If eligible Tier A candidates exist but reuse is not granted, add code `route_reuse_not_granted` and keep the top Tier A candidate as advice (shown with `advice=true`). Continue to step 4.
4. **Tier B.** If eligible Tier B candidates exist and the top one has `r >= policy.min_template_reliability`, the action is `warm_start`, targeting the minimum of this key:
   ```
   (-F, -r, -promoted, -m_lcb, c, k, library sequence)
   ```
   Ties resolve oldest first, like `HarnessLibrary.lookup`.
5. **Otherwise** the action is `compile`, with code `route_compile`.

Codes are also added when these conditions hold:

- `fit_slot`, `fit_shape`, `fit_family` or `fit_none`: the best tier seen.
- `evidence_synthetic_only`: every eligible candidate is `synthetic_fake`.
- `evidence_insufficient`: every eligible candidate has `held_out_count < 5` and, for templates, `successes + failures = 0`.
- `route_forge_suggested` (P8), when either:
  - (a) the route log holds an `OutcomeRecord` with `outcome="unlicensed"` for this slot and no later `licensed` one; or
  - (b) no Tier A or B candidate exists, and the route log holds at least 3 `unlicensed` and 0 `licensed` outcomes for this shape.

`selection_failed` is not a routing outcome. Stage 1 runs inside the compile, and Forge cannot rescue it, because Forge occupies the Stage 3 slot (Phase 9 Section 8.2).

### 8.2 Fallback order

`reuse_installed` (granted) > `warm_start` > `compile` (+ `route_forge_suggested` hint). With the existing `--forge` or `forge on`, the compile already runs Forge with `trigger=failed`. That is the only automatic Forge fallback, and it is consented. `forge run` and `forge integrate` force `trigger=always`.

### 8.3 Exploration (bounded; off by default)

Exploration applies to `warm_start` decisions only, and never to `reuse_installed`, HTTP, `forge_integrate`, or origins with an INSTALL-lifecycle probe. All of these must hold:

- `policy.explore_every = k > 0`;
- `counter % k == k - 1`, where `counter` is the number of prior decisions for this `shape` in the route log, so exploration is deterministic;
- `explore_spent_usd < policy.explore_usd_per_day`, where the spend is the sum of `compile_cost_usd` over today's (UTC) `OutcomeRecord`s whose decision had `explored=true`.

When they hold, the target is the eligible Tier B template with the fewest trials `s + f` among those with `s + f < 3`, tie-broken by the Tier B key. Codes: `route_explore`, or `route_explore_cap` when the cap blocked exploration.

Exploration cannot cause an unlicensed run. Stage 4 still gates, and two failures evict the template (`library/__init__.py:253-278`).

### 8.4 Explanation output

```python
class InstalledRef(BaseModel): name: str; revision: int; artifact_digest: str; spec_hash: str
class TemplateRef(BaseModel): key: str; artifact_digest: str; model: str
class CandidateView(BaseModel):       # content-free apart from the host-side ref
    tier: Literal["A", "B", "C"]; origin: Literal["installed", "template"]; ref: InstalledRef | TemplateRef
    model: str; fit: float; m_lcb: float | None; sec_lcb: float | None; r: float | None; h: float | None
    cost_usd: float | None; latency_s: float | None; latency_basis: Literal["measured", "bound"] | None
    k: int | None; evidence: tuple[EvidenceLabel, ...]; codes: tuple[RouteCode, ...]
    eligible: bool; advice: bool = False
class Proposal(BaseModel): kind: Literal["promote", "rollback"]; name: str; revision: int | None; command: str
class RouteDecision(BaseModel):       # frozen, extra="forbid"
    decision_id: UUID; origin: Origin; action: RouteAction
    target: InstalledRef | TemplateRef | None
    signature: TaskSignature; candidates: tuple[CandidateView, ...]   # ranked, <= max_candidates
    chosen_rank: int | None; explored: bool; codes: tuple[RouteCode, ...]
    proposals: tuple[Proposal, ...]; decision_ms: float
```

The CLI and session table uses fixed columns and is at most 100 ASCII columns wide:

```
tier candidate        model        F    m_lcb  r    $cost    lat_s   k  evidence       codes
A    invoices@3       claude-code  1.00 +0.083 -    0.0123   4.1b    3  stage4,live_ae route_reuse_not_granted
B    3fa2b1c9d0e1     claude-code  0.86 +0.041 0.69 0.0098   2.7m    2  stage4,live_ae -
route: warm_start -> 3fa2b1c9d0e1 (fit_shape)
```

Column widths are 4, 16, 12, 4, 6, 4, 8, 7, 2 and 14, and the codes column is truncated to fit.

- The `lat_s` suffix is `m` (measured) or `b` (bound).
- Evidence labels are abbreviated by a fixed map: `stage4`, `fake`, `local`, `live_ae`, `live_b`, `nodrift`, `bench`, `human`, `promo`.
- Proposals print as `proposal: <command>`.

### 8.5 Proposals (`architect/routing/proposals.py`; no automatic effect)

- **`route_promote_proposed`**: the probe has Tier A installed entry X and another eligible licensed version Y of the same slot. Y is a library snapshot or another registry name, and `m_lcb(Y) - (m(X) + 0.5/sqrt(max(n_X, 1))) > policy.promote_margin`, meaning Y's lower bound beats X's upper bound. The proposal command is `architect recompile <name> --profile <profile>`. A recompile warm-starts through the router, and `Registry.install`/`_beats` still requires a strictly better Stage 4 report.
- **`route_rollback_suggested`**: `Registry.drift(name) == "drifting"` and an earlier revision R has a higher `m_lcb` and is licensed on a fresh load. The command is the session verb `rollback <name> <R>`. Rollback stays operator-only.

---

## 9. Records and aggregation

### 9.1 Route log (`architect/routing/log.py`; `workspace/routes/`, Pi-denied via `WORKSPACE_STATE`)

The log is append-only JSONL (`decisions.jsonl`, `outcomes.jsonl`, `roles.jsonl`, `ratings.jsonl`). Each line is fsynced, and a sqlite index lives at `routes/index.sqlite` (WAL, `busy_timeout=5000`). All models use `extra="forbid"`, frozen, and reuse `telemetry.schema.Category` and `categorical()`.

```python
class RouteRecord(BaseModel):
    decision_id: UUID; timestamp: AwareDatetime; origin: Origin; action: RouteAction
    tier: Literal["A", "B", "C", "none"]; slot: Category; shape: Category; family: Category
    token_count: NonnegativeInt; candidate_count: NonnegativeInt; chosen_rank: NonnegativeInt | None
    target_digest: Category | None           # categorical(artifact_digest)
    model: Category | None; fit: float; m_lcb: float | None; r: float | None
    expected_cost_usd: float | None; latency_s: float | None; latency_basis: Literal["measured", "bound"] | None
    evidence: tuple[EvidenceLabel, ...]; codes: tuple[RouteCode, ...]; explored: bool; decision_ms: float
class OutcomeRecord(BaseModel):
    decision_id: UUID; timestamp: AwareDatetime
    outcome: Literal["licensed", "unlicensed", "delivered", "failed", "discarded", "cancelled"]
    stage4_passed: bool | None; evidence_class: EvidenceClass | None
    compile_cost_usd: float = 0; build_cost_usd: float = 0; delivery_cost_usd: float = 0
class RoleRecord(BaseModel):
    decision_id: UUID; timestamp: AwareDatetime; role: RoleName; model: Category; variant: Category
    domain: Category; evidence_class: EvidenceClass; floor_origin: FloorOrigin | None
    chosen_rank: NonnegativeInt; n: float; p_lcb: float | None; explored: bool
    outcome: Literal["success", "failure", "partial", "excluded"] | None; score: float | None
    codes: tuple[RouteCode, ...]
class RatingRecord(BaseModel):
    run_id: UUID; timestamp: AwareDatetime; target_digest: Category; rating: Literal["good", "bad"]

class RouteLog:
    def __init__(self, root: Path) -> None
    def append(self, record: RouteRecord | OutcomeRecord | RoleRecord | RatingRecord) -> None
    def counter(self, shape: str) -> int
    def explore_spent_today(self) -> float
    def outcomes(self, *, slot: str | None = None, shape: str | None = None) -> Iterator[tuple[RouteRecord, OutcomeRecord]]
    def roles(self, role: RoleName) -> Iterator[RoleRecord]
    def ratings(self, target_digest: str) -> tuple[int, int]
```

Every field above is either a number, a UUID, a closed literal or a `h_` category (P6). A test serializes every record type, checks each string field against `^(h_[0-9a-f]{32}|[a-z0-9_.:-]{1,64})$` or a UUID, and asserts that no task text round-trips.

### 9.2 Indexes (WP1)

These are idempotent, crash-safe migrations, each run inside one transaction:

- Registry: table `slots(name TEXT, revision INT, spec_hash TEXT, slot TEXT, shape TEXT, family TEXT, model TEXT, lifecycle TEXT, PRIMARY KEY(name, revision))`.
  - It is filled in the same transaction as `install`.
  - It is backfilled once on open for current revisions missing from it. This is a one-time load of each entry; a failing load is skipped and re-tried on the next open.
  - New method `Registry.find(*, slot: str | None = None, shape: str | None = None) -> tuple[RegistryEntry, ...]`, current revisions only.
- Library: `ALTER TABLE templates ADD COLUMN shape TEXT`. It is filled on `store` and backfilled lazily in `candidates`.
  - New method `HarnessLibrary.candidates(shape: str, model: str, limit: int = 8) -> tuple[TemplateEvidence, ...]`, with `TemplateEvidence(key, artifact, successes, failures, evicted, sequence, promoted)`. It is ordered by `sequence` and loads the authenticated artifact only for the first `limit` non-evicted rows.
  - `lookup` is unchanged.
  - New class `ReadOnlyLibrary(library)`. It exposes only `lookup` and `verification_count`, so it is not a `LibraryLifecycle`, and a test asserts that `isinstance(ReadOnlyLibrary(x), LibraryLifecycle) is False`.
- Telemetry: `routes/evidence.sqlite` keeps an incremental aggregate keyed by the journal byte offset:
  - per `(spec_hash, harness_version, evidence_class)`: delivery count, accepted count, cost sum, and a sorted reservoir of at most 256 wall times;
  - per `(task_type, model, domain, evidence_class)`: role counts.
  
  Only the tail since the stored offset is scanned. If the cache is lost, the journal is replayed once. A test asserts that routing 100 probes performs at most one full replay.

```python
class VersionEvidence(BaseModel): ...   # the fields of CandidateView before filtering
class RoleStats(BaseModel): n: float; successes: float; mean_cost_usd: float | None; median_wall_s: float | None; evidence_class: EvidenceClass
class EvidenceIndex:
    def __init__(self, registry: Registry, library: HarnessLibrary | None, telemetry: TelemetryStore,
                 log: RouteLog, workspace: Path, *, providers: Mapping[str, Provider],
                 promotion_reuses: int) -> None
    def installed(self, sig: TaskSignature) -> tuple[VersionEvidence, ...]
    def templates(self, sig: TaskSignature, models: tuple[str, ...], limit: int) -> tuple[VersionEvidence, ...]
    def role_stats(self, role: RoleName, variant: RoleVariant, domain: str | None) -> RoleStats
```

---

## 10. Executing a decision and re-checking the license

### 10.1 Reuse executor (`routing/router.py`)

```python
def load_reuse_target(decision: RouteDecision, registry: Registry, probe_spec: Spec) -> HarnessArtifact
```

It raises `RouteError(code)` unless all of the following hold:

- the action is `reuse_installed` (else `route_no_reuse`);
- `artifact = registry.get(name, revision)` is `.licensed` (else `route_unlicensed`);
- the artifact is `check_subharness_tree`-clean (else `route_unlicensed`);
- `artifact.artifact_digest == target.artifact_digest` and `artifact.spec.spec_hash == target.spec_hash` (else `route_target_changed`);
- the `_beats` fields are exactly equal between `artifact.spec` and `probe_spec` (else `route_target_changed`);
- the artifact is not `.consumed` (else `route_consumed`).

The caller then calls `Runtime.run(artifact, input, adapter_factory(artifact.model))`, which re-checks the license and model.

### 10.2 Compiler integration (WP5; `compiler/__init__.py`, `types.py`)

```python
Compiler.__init__(..., router: Router | None = None, roles: RoleAdapters | None = None)   # keyword-only, default None
async def compile(self, source, overrides=None, *, approved=False, exact_overrides=False,
                  origin: Origin = "cli", reuse_installed: bool = False, budget_cap: Budget | None = None,
                  no_route: bool = False) -> CompileResult
CompileResult.route: RouteDecision | None = None
CompileResult.reused: bool = False
```

The router runs after Stage 0 (or on the given Spec) and before the INSTALL approval check, so an `approval_required` result also carries `route` as advice. Then:

- `reuse_installed` with `reuse_installed=True`:
  - the result is `CompileResult(spec, artifact=load_reuse_target(...), report=artifact.report, stages=stages_so_far, reused=True, route=decision)`;
  - there is no Stage 1 to 4 and $0 compile spend;
  - `cli._compiled` must not reinstall a reused artifact.
- `warm_start`: `_compile_spec` uses `RoutedLibrary(chosen_template, fallback=self.library)` for this call only.
  - `RoutedLibrary.lookup(spec, model)` returns the chosen template when `model == template.model` and `_compatibility` matches, and otherwise delegates.
  - `store`, `record_verification` and `verification_count` delegate unchanged, so `LibraryLifecycle` checks and Forge `_verification_count` behave as they do today.
- `compile` or `refuse`: the library is unchanged; `refuse` returns the existing failure path with the route codes appended.
- On `router is None`, `no_route=True` or `P5`: the behavior is identical to today.

After Stage 4, the Compiler calls `router.outcome(decision, OutcomeRecord(...))` with `licensed` or `unlicensed`, the compile cost and the evidence class.

### 10.3 Orchestrator (WP5)

```python
@staticmethod
def _deliverable_routed(result: CompileResult, artifact: HarnessArtifact, spec0: Spec, cap: Budget, grant: bool) -> bool
```

It is true only if all of these hold:

- `grant`;
- `result.reused`;
- `0 in result.stages`;
- `artifact` was freshly reloaded by `load_reuse_target`;
- `artifact.licensed`;
- `artifact.spec.lifecycle == INSTALL`;
- the `_beats` fields of `artifact.spec` equal those of `spec0`;
- `_within(artifact.spec.budget, cap)`;
- `artifact.report.cost_usd <= cap.cost_usd`.

`_deliverable` (`orchestrator/__init__.py:526`) is not changed. `_discard_code` is skipped when `result.reused`. It is already a no-op outside `artifact_root` (line 222), and a test asserts that an installed `harness.py` survives a routed delivery. `_discard_code` and `_EPHEMERAL_CODE` are replaced by imports from `architect/runtime/ephemeral.py`, with identical behavior, and a test pins it. The router is read under the existing `compile_lock`.

---

## 11. Routing Architect's own roles and subagents

### 11.1 Roles

`RoleName = Literal["inference", "decomposition", "rewrite", "forge_agent", "floor_draft"]`.

The judge is not routable, for three reasons:

- `FloorDefinition.judge_model` is part of the Spec and `spec_hash`;
- `LLMJudge` refuses other adapters;
- calibration is a one-time gate.

Judge telemetry (`task_type == categorical("verifier")`) is reported in `architect roles`, but only as a report.

Telemetry task types, which are `categorical(preset)` (`runtime/__init__.py:1055`):

| Role | preset label |
|---|---|
| `inference` | `inference` (`compiler/inference.py:283`) |
| `decomposition` | `orchestrator` (`orchestrator/__init__.py:393`) |
| `rewrite` | `rewrite_proposal` (`compiler/rewriting.py:291`) |
| `forge_agent` | `forge_agent` (`compiler/forge.py:239`) |
| `floor_draft` | `floor_draft` (`session/floors.py:306`) |

### 11.2 Profile (`architect/routing/roles.py`, wired in `profiles.py` by WP2)

```python
class RoleVariant(BaseModel):        # frozen, extra="forbid"
    model: str                        # a profile.models key, else ProfileError("route_model_unknown")
    params: dict[str, int | str] = {} # closed per role, below; other keys give ProfileError("route_param_invalid")
class RoleRouting(BaseModel):        # frozen, extra="forbid"; Profile.roles
    policy: Literal["pinned", "evidence"] = "pinned"
    candidates: dict[RoleName, tuple[RoleVariant, ...]] = {}
    min_samples: int = Field(20, ge=5, le=10000)
    non_inferiority: float = Field(0.02, ge=0, le=0.2)
    z: float = Field(1.64, gt=0, le=4)
    explore_every: int = Field(0, ge=0, le=1000)
```

Closed parameter variants:

| Role | Allowed params |
|---|---|
| `inference` | none |
| `decomposition` | `max_tasks` in {4, 8} (`OrchestratorConfig.max_tasks`) |
| `rewrite` | `max_rewrites` in {1, 3} (`CompileConfig.max_rewrites`) |
| `forge_agent` | `mode` in {agent, template}; `pi_max_turns` in {100, 200} |
| `floor_draft` | none |

Parameters never touch the Spec, the caps or the floor.

**Pinned default.** With a role absent from `candidates`, or with policy `pinned`, the single arm is exactly today's resolution:

- `inference = profile.inference`, falling back to `adapter_factory("fake")` in the Compiler as at line 183;
- `decomposition = profile.decomposition`, else `inference`, else the first models key;
- `forge_agent` via `resolve_forge_agent`;
- `rewrite = profile.rewrite`;
- `floor_draft = spec.model_target` if configured, else `ladder[0]`.

A test asserts adapter identity (`is`) per role under `pinned`.

### 11.3 Resolution seam

```python
class RoleChoice(BaseModel): adapter: ModelAdapter; variant: RoleVariant; decision_id: UUID; explored: bool; codes: tuple[RouteCode, ...]
class RoleRouter:
    def __init__(self, routing: RoleRouting, index: EvidenceIndex, log: RouteLog, *, providers: Mapping[str, Provider],
                 quote: Callable[[str], float]) -> None
    def choose(self, role: RoleName, *, domain: str | None, default: RoleVariant) -> tuple[RoleVariant, UUID, bool, tuple[RouteCode, ...]]
class RoleAdapters:
    def __init__(self, defaults: Mapping[RoleName, tuple[ModelAdapter | None, RoleVariant]], router: RoleRouter | None,
                 adapter_factory: Callable[[str], ModelAdapter], pins: Callable[[RoleName], bool]) -> None
    def choose(self, role: RoleName, *, domain: str | None = None) -> RoleChoice
    def adapter(self, role: RoleName, *, domain: str | None = None) -> ModelAdapter | None
    def record(self, choice: RoleChoice, outcome: Literal["success", "failure", "partial", "excluded"],
               score: float | None = None, floor_origin: FloorOrigin | None = None) -> None
```

`build_services` builds `Services.roles`. It passes `Compiler(roles=...)` and `Orchestrator` access through `services`.

Call sites (WP5):

- Stage 0 at `compiler/__init__.py:183`, with `domain=None`;
- `Session._infer` and `_revise`;
- `Orchestrator._decompose`;
- `Compiler._forge` and `forge()`;
- the rewrite proposal call;
- `Session._draft`.

Explicit pins disable routing for that role. The pins are:

- session `use model` (inference) and `forge model`;
- CLI `--forge-model`;
- `ForgeSettings.agent_model`.

`RoleAdapters` is consulted only when present and `policy == "evidence"`. The Compiler keeps its existing `inference_adapter`, `rewrite_adapter` and `forge_adapter` attributes as the pinned defaults, so `Session._arm_forge` swaps still work.

The `forge_agent` choice is made before the Phase 9 consent line prints. The CLI and session then print, immediately before that unchanged line:

```
forge agent: <model> (mode <agent|template>, turns <N>)
```

Exploration never changes a consented cap, and caps are not role parameters.

### 11.4 Evidence policy (per call)

`D` is the pinned default variant and `C` is the declared candidates plus `D`.

1. **Eligible** means all of the following:
   - weighted outcome count `n >= min_samples` in the `(role, variant, domain)` bucket, or in the role-wide bucket when the domain bucket is thin;
   - the same evidence class as the variant's current provider (P7);
   - the fixed role budget holds by price quote. For inference this is `quote(model) <= _INFERENCE_BUDGET.cost_usd` ($1.00), the same rule as `session/bootstrap.py:171`. For decomposition it is `$1.00 / 60 s`.
2. For each eligible `x`, compute `p_lcb(x) = W(successes_w, n_w, z)`, and let `p_hat(D) = successes_w / n_w`, or `0.5` when D has no outcomes.
3. `x` is non-inferior if `p_lcb(x) >= p_hat(D) - non_inferiority`.
4. The choice is the minimum over non-inferior eligible `x`, with `D` always included, of `(mean_cost_usd, median_wall_s, candidate order)`.
5. If no candidate other than `D` is eligible, choose `D` with code `role_evidence_insufficient`.
6. Exploration is on when `explore_every = k > 0`. Every k-th call per role, as counted in the route log, goes to the least-sampled variant that passes the budget filter, with code `role_explore`.

Outcomes are content-free and joined by `decision_id`; cost and wall come from the role's own telemetry row:

| Role | Outcome |
|---|---|
| `inference` | `success` if the compile consuming this Stage 0 produced a licensed artifact; `failure` if Stage 4 failed; `excluded` on operator discard or cancel |
| `decomposition` | `partial` with `score = delivered / planned`, counted as `success` when `score == 1`; `failure` on plan rejection |
| `forge_agent` | `success` if Forge produced the accepted submission and Stage 4 passed |
| `rewrite` | `success` if an accepted proposal improved the tuning score and Stage 4 passed |
| `floor_draft` | `excluded` (cost only; drafted cases are `model_draft` by label) |

Floor-origin weights come from Section 7.2. Telemetry `accepted` is shown as `proxy_completed`, because it means the run completed (`runtime/__init__.py:1073`), not that it was correct.

### 11.5 Orchestrator subtasks

- Every subtask compile passes `origin="orchestrator"` and `budget_cap=` the subtask cap. Tier B warm starts therefore come from evidence, and exact slots are reused only under `do --reuse-installed`.
- With `routing.subtask_models = True`, `PlannedTask.model` is replaced by the best ladder model for which an eligible Tier A or B candidate exists, ranked by `(-m_lcb, c)`. Otherwise the decomposer's model is kept.

### 11.6 Forge children (Phase 9 Section 11 amendment)

With `routing.child_warm_start = True`, `_spawn` (`compiler/forge_tools.py:1143`) passes `ReadOnlyLibrary(parent_library)` in place of `NullLibrary()` at line 1239. The child's lookup is enabled, and store and verification are never called, so agent-authored-case artifacts never enter the library.

Everything else is unchanged:

- ladder `(parent.model,)`;
- private case floor and `case_origin`;
- the spawn wallet, `max_spawns`, `max_depth <= 2`, `spawn_share` and `spawn_max_usd`.

Children are never Tier A candidates (private floor), so reuse is not attempted. The default is `False`.

---

## 12. Forge delivery modes (WP3)

### 12.1 `forge run`

```python
ForgeDelivery = Literal["run", "integrate"]

class ForgeRunResult(BaseModel):
    output: JsonValue | None; spec_hash: str; artifact_digest: str | None; licensed: bool
    stages: tuple[int, ...]; route: RouteDecision | None; reused: bool
    forge_summary: dict[str, int | float]     # content-free counts from forge.json: checks, dryruns, measures, submissions, spawns, agent_cost_usd
    floor_origin: FloorOrigin
    build_cost_usd: float; compile_cost_usd: float; run_cost_usd: float; codes: tuple[str, ...]

async def forge_run(source: Spec | str, task_input: JsonValue, services: Services, settings: ForgeSettings, *,
                    consent: Callable[[str], bool], cases: Path | None = None, reuse_installed: bool = False,
                    overrides: dict[str, JsonValue] | None = None) -> ForgeRunResult
def deliverable_ephemeral(result: CompileResult, artifact: HarnessArtifact, cap: Budget, *, require_stage0: bool) -> bool
```

`deliverable_ephemeral` is `Orchestrator._deliverable` with `0 in result.stages` required only when `require_stage0` is true, that is, when the source was prose.

Flow:

1. **Role.** Resolve `forge_agent` (Section 11.3).
2. **Consent.** Print the `forge agent:` line and then the Phase 9 line unchanged:
   ```
   forge: compile cap $A + forge cap $B = $C (API-equivalent, not a bill); forge minutes M; verifications for this spec so far: K/N
   ```
   Then `Continue? [y/N]` unless `--yes`. A cancel returns `forge_cancelled` with $0 spent.
3. **Stage 0 and route.** Build a per-call Compiler copied from `services.compiler`, the same way `_spawn` builds one: the same artifact root, runtime, adapter factory, library, router and roles, with `config.forge = settings.model_copy(update={"enabled": True, "trigger": "always"})`. Lifecycle is forced by exact override: `lifecycle: "expire"`.
   - If the route is `reuse_installed` and `reuse_installed=True`, run the installed entry via Section 10.1. The code is `forge_run_reused_installed`, with no build spend.
4. **Cases (optional, `--cases FILE`).** The file is JSONL of `{"case_id", "task", "expected"}` (the `EvaluationCase` shape), with at most 10,000 rows and 10 MB.
   - The Spec floor must be `held_out_tasks`, else `forge_cases_floor_kind`.
   - The floor `dataset` is set by exact override to `"cases:" + sha256(file_bytes)[:16]`, so the `spec_hash` binds the case set.
   - The per-call `floor_factory` and `smoke_factory` return `HeldOutTasks(definition, cases)` for that exact definition and delegate otherwise.
   - The partition is `FloorEvaluator`'s own sorted-id even/odd split (`floors/base.py:110-135`); the caller does not choose it.
   - At least 8 rows are required, so each half has at least 4, else `forge_cases_too_few`.
   - An invalid row, a duplicate id or a schema mismatch gives `forge_cases_invalid`.
   - The resolved file path is added to the effective `pi_config.denied_paths` for this call.
   - `floor_origin = "authored_cases"`.
5. **Compile.** Stages 1, 2, 3 (Forge) and 4 run as today, including `forge_verification_cap` and the nested wallets.
6. **Deliver.** Delivery happens only if `deliverable_ephemeral(result, HarnessArtifact.load(result.artifact.directory), cap, require_stage0=isinstance(source, str))` holds, else `forge_run_unlicensed`. Then `Runtime.run(artifact, task_input, adapter_factory(artifact.model))` runs exactly once, which writes `.consumed` and a telemetry row.
7. In `finally`, call `runtime.ephemeral.discard_code(artifact_root, artifact.directory)`. `forge.json`, `report.json` and `subharnesses/` are kept as evidence. The library already stored the licensed EXPIRE snapshot as a pre-consumed template, so the next similar task can warm-start from it through Tier B.
8. **Records.** Write `OutcomeRecord` (`delivered` or `unlicensed`) and the `forge_agent` `RoleRecord`.
9. **Summary line.** Print it on stderr; the JSON result goes on stdout:
   ```
   forge run: licensed (held_out N, <kind> S<op>L, verification <v8>) delivered once, expired; build $A agent (API-equivalent), compile $B, play $C
   ```
   `<op>` is `>=`, or `<=` for `human_edit_rate`. The build cost stays separate from play cost, as Phase 9 Section 16 requires.

### 12.2 `forge integrate`

```python
class IntegrateOptions(BaseModel):     # frozen, extra="forbid"
    entry: InstalledRef; staged: Path; into: str; package: str; manifest_sha256: str
class IntegrateResult(BaseModel):
    licensed: bool; name: str; revision: int; spec_hash: str
    human_review_required: Literal[True] = True; feature_flag_required: Literal[True] = True
    export: dict[str, JsonValue]       # {package_path, files, bytes, manifest_sha256, evidence_label: "host_attested", placement: "pi"|"host"|"staged"}
    codes: tuple[str, ...]

async def forge_integrate(request: str, repo: Path, services: Services, settings: ForgeSettings, *,
                          into: str, package: str | None, approved_spec: Spec | None,
                          consent: Callable[[str], bool], stage_only: bool = False, package_only: bool = False,
                          reuse_installed: bool = False) -> IntegrateResult | CompileResult
```

The gates run in order (P9):

1. **Approval.** Without `approved_spec`, run Stage 0 and return the `approval_required` `CompileResult`, writing `proposals/<spec_hash>.json` exactly as `cli._compiled` does. This spends $0 beyond Stage 0 and prints no Forge line. `approved_spec.lifecycle` must be `INSTALL`, else `approved_feature_must_be_installed` (existing code).
2. **Spend consent.** Print the `forge agent:` line and the Phase 9 cap line.
3. **Build.**
   - If the route is `reuse_installed` with `reuse_installed=True`, skip the build (code `integrate_reused_installed`).
   - Otherwise run `Compiler.compile(approved_spec, approved=True, origin="forge_integrate")` with Forge armed (`trigger="always"`).
   - A licensed result is installed as `"feature-" + spec_hash[:16]`, the same name and `not_better` handling as `features.py:105-117`.
   - The entry is re-loaded: it must be licensed, child-checked and digest-equal. Otherwise the result is `feature_compile_failed` (existing code).
4. **Export** to staging (Section 12.3).
5. **Export consent.**
   ```
   export: <package> (<files> files, <kb> KB) -> <into>/<package>; feature flag OFF; human review required
   Continue? [y/N]
   ```
   Without `--yes` and without a TTY `y`, the result is `export_cancelled`. The installed entry and the staging directory are kept, and nothing is written into the repo.
6. **Placement.** Exactly one of these three runs:
   - `stage_only`: print `export staged: <staging path>` and write nothing into the repo.
   - `package_only`: call `place_package(staged, repo, into)` (host code, Section 12.5), which writes only the new directory `<into>/<package>` and edits no other file.
   - default: call `FeatureBuilder.create(request, repo, approved_spec=approved_spec, integrate=IntegrateOptions(...))`.
     - With `integrate` set, `compile_agent` does not compile. It re-loads `options.entry` through `load_reuse_target` semantics and checks `spec_hash == approved_spec.spec_hash`. It then returns the existing feature dict with code `integrate_reused_installed`.
     - The capability `place_harness_package` is added (Section 12.5).
     - The goal text gains the fixed sentence: `After compile_agent succeeds, call place_harness_package exactly once with into equal to ` + `json.dumps(into)` + `, then write only call-site glue that checks the package's enabled() flag.`
     - `profile.builder_floor` still gates the builder, as today.
7. **Post-run verification.** After the Pi run or host placement, `verify_export(repo / into / package, registry=services.registry)` re-hashes every file against the manifest. A mismatch sets `export_integrity_mismatch` in `codes` and keeps `human_review_required`. Nothing in the repo is deleted.

### 12.3 Export package (`forge_modes/export.py`; trusted host code, never Pi)

```python
class ExportManifest(BaseModel):       # frozen, extra="forbid"
    package_format: Literal[1]; architect_version: str; package: str
    spec_hash: str; artifact_digest: str; verification_id: str; execution_mode: Literal["native", "sandbox"]
    children: dict[str, tuple[str, str, Literal["projection", "authored"]]]   # name -> (artifact_digest, spec_hash, case_origin)
    files: tuple[tuple[str, str], ...]   # (package-relative POSIX path, sha256), sorted; excludes MANIFEST.json
def export_package(entry: RegistryEntry, staging_root: Path, package: str) -> tuple[ExportManifest, str]   # (manifest, manifest_sha256)
def place_package(staged: Path, repo: Path, into: str) -> ExportManifest
def verify_export(package_dir: Path, *, registry: Registry | None) -> ExportCheck
class ExportCheck(BaseModel): integrity: bool; pins: bool; children: bool; licensed: bool | None; registry_match: bool | None; codes: tuple[str, ...]
```

- The staging path is `workspace/exports/<artifact_digest[:16]>/<package>/`, and `exports` is in `WORKSPACE_STATE`, so Pi cannot read it.
- The package name matches `^[a-z][a-z0-9_]{0,39}$` and is not a Python keyword, else `export_package_invalid`. The default is `"architect_" + spec_hash[:8]`.
- The entry must be `INSTALL` and licensed on a fresh load, else `export_unverified`.
- A symlink anywhere gives `export_failed`, and so does any path over the registry's 246-character cap (`_entry_fits`).

Layout:

```
<package>/
  __init__.py            # from templates/init.py.tmpl; pins substituted
  MANIFEST.json          # ExportManifest, canonical JSON
  EVIDENCE.json          # content-free (below)
  EVIDENCE.md            # fixed text from templates/EVIDENCE.md.tmpl
  README.md              # fixed text; install requirement
  schemas/input.schema.json, schemas/output.schema.json
  artifact/              # byte-identical: harness.py, spec.json, config.json, report.json, [forge.json], [subharnesses/**]
  tests/test_<package>.py
```

`EVIDENCE.json` contains:

- identity: `spec_hash`, `artifact_digest`, `verification_id`;
- primary floor: `floor_kind`, `floor_score`, `floor_limit`, `held_out_count`;
- secondary floor: `secondary_floor_kind`, `secondary_floor_score`, `secondary_floor_limit`;
- cost and compute: `mean_cost_usd`, `max_cost_usd`, `max_compute_s`;
- `model_class: EvidenceClass`, `floor_origin: FloorOrigin`, `preset`;
- `forge: {checks, dryruns, measures, submissions, spawns, agent_cost_usd}`;
- `children: [{name, artifact_digest, case_origin}]`;
- `evidence_label: "host_attested"`;
- `report_evidence: "stage4_signed" | "synthetic_fake" | "authored_cases"`;
- `exported_at`, `architect_version`;
- `attestation_note`, which is exactly:

```
licence verifiable only on a host holding the Architect signing key; re-verify with `architect export verify`
```

`EVIDENCE.md` is a fixed text stating three things:

- the license is host-attested;
- child licenses rest on their own `case_origin` floors and are weaker;
- tuning numbers are not a license.

No held-out, tuning, authored or floor-dataset case content is ever exported.

`README.md` states that `architect>=<version>` must be installed in the interpreter that runs the package, because native harnesses import `architect.presets` and sandbox artifacts spawn `sys.executable -I -m architect.runtime._sandbox_worker` (`runtime/sandbox.py:1260`, where `-I` ignores `PYTHONPATH`). It also states that another host needs `architect compile --approved-spec <package>/artifact/spec.json` to mint its own license. A signing key or HMAC is never shipped.

### 12.4 Shim and generated `__init__.py`

```python
# architect/forge_modes/shim.py
class FeatureDisabled(RuntimeError): ...
class ExportedHarness:
    def __init__(self, root: Path, pin: Mapping[str, JsonValue]) -> None
    def verify(self) -> ExportCheck                       # integrity, pins, children, license (host key or trusted Ed25519 key)
    def load(self) -> HarnessArtifact                     # verify(); raise ExportError(code) on any failed check
    async def run(self, value: JsonValue, *, profile: Path, workspace: Path | None = None) -> JsonValue
        # load(); ws = workspace, else ARCHITECT_WORKSPACE, else the profile's; services = build_services(in_workspace(load_profile(profile), ws), ws); services.runtime.run(artifact, value, services.adapter_factory(artifact.model))
    async def run_with(self, runtime: Runtime, adapter: ModelAdapter, value: JsonValue) -> JsonValue
```

Codes from `verify` and `load`:

- W7 (`docs/trust.md`), first when present: `artifact_changed` (bytes a trusted Ed25519 key signed changed: a sealed manifest, a file it lists, or a sealed report), then `unsigned` or `key_untrusted` (the license verifies by its Ed25519 seal, but the manifest is unsealed or sealed by a key this host does not trust);
- `export_integrity_mismatch`: a file hash differs;
- `export_pin_mismatch`: the digest, spec_hash or execution_mode differs from PIN;
- `export_subharness_missing`;
- `license_unverifiable`: no key file at `ARCHITECT_SIGNING_KEY_FILE` (W7: and no Ed25519 seal; a seal from a key this host does not trust is `key_untrusted`). These checks use configured keys only: `.architect/signing.key` under the working directory never counts unless the variable names it (`docs/trust.md`, "Which keys count");
- W7: `trust_in_target` (first) when the HMAC key or the trust list lies inside the package, and `trust_list_invalid` or `trust_list_missing` in place of `key_untrusted` when no key is trusted at all;
- `export_unverified`: a key is present but `.licensed` is false.

Artifacts are INSTALL lifecycle, so a run consumes nothing. Delivery telemetry lands in the run's workspace: `workspace` when given, else `ARCHITECT_WORKSPACE`, else the profile's own. A profile that names `~/.architect` reads its stored key and relay address in that workspace too (`profiles.in_workspace`), as the CLI and the desktop app do.

The generated `__init__.py` is fixed text; only `PIN` and `FLAG` are substituted:

```python
"""Generated by `architect forge integrate`. Do not edit; see EVIDENCE.json."""
from __future__ import annotations
import os
from pathlib import Path
try:
    from architect.forge_modes.shim import ExportedHarness, FeatureDisabled
except ImportError as exc:
    raise ImportError("export_architect_missing: install architect in this interpreter") from exc
PIN = {"package_format": 1, "spec_hash": "...", "artifact_digest": "...", "execution_mode": "...", "children": {...}}
FLAG = "ARCHITECT_FEATURE_<SPEC_HASH_8_UPPER>"
_HARNESS = ExportedHarness(Path(__file__).resolve().parent, PIN)
def enabled() -> bool:
    return os.environ.get(FLAG) == "1"
def verify():
    return _HARNESS.verify()
async def run(value, *, profile, workspace=None):
    if not enabled():
        raise FeatureDisabled(FLAG)
    where = None if workspace is None else Path(workspace)
    return await _HARNESS.run(value, profile=Path(profile), workspace=where)
```

### 12.5 Placement

```python
def place_package_tool(staged: Path, repo: Path, into: str, *, ready: Callable[[], bool],
                       active: Callable[[], bool]) -> TrustedTool
```

The tool definition:

- `name="place_harness_package"`;
- parameters `{"type": "object", "properties": {"into": {"type": "string", "maxLength": 512}}, "required": ["into"], "additionalProperties": false}`;
- `max_calls=1`, `requires_build=False`.

It is passed via `PiBackend(config, compiler_tool=compile_agent, capabilities={"place_harness_package": tool})`. `backends/pi.py` is unchanged, and `"place_harness_package"` is added to `allowed_tools` for this run only.

The callback returns these errors:

- `{"error": "integrate_not_ready"}` until `compile_agent` succeeded in this run;
- `{"error": "factory_capability_expired"}` when not active;
- `{"error": "export_path_invalid"}` unless `into` equals the operator's value exactly.

Otherwise it calls `place_package`.

`place_package(staged, repo, into)` is shared by the tool and by `--package-only`. It validates `repo / into / package` with the same rules as `PiBackend._granted_path` (`backends/pi.py:404`):

- confined to the repo;
- no `..`;
- no `.architect` component;
- no symlink, junction or hardlink on the path;
- not under `pi_denied_paths`.

Violations give `export_path_invalid`, and an existing target gives `export_target_exists`. It then copies the staging tree with fsync per file, re-hashes it against the manifest (`export_integrity_mismatch`), and returns the manifest. Pi receives only `{"path": "<into>/<package>", "files": N, "manifest_sha256": "..."}`. The tool is not offered when `context.is_evaluation`.

### 12.6 Generated tests (`templates/test.py.tmpl`, pytest)

| Test | Needs | Behavior |
|---|---|---|
| `test_manifest_integrity` | nothing | every file sha256 equals MANIFEST |
| `test_pins` | nothing | loaded artifact digest, spec_hash, execution_mode and children equal PIN (`HarnessArtifact.load` needs no key) |
| `test_schema_hashes` | nothing | `schema_sha256(schemas/*)` equals `spec.json` schemas; both are valid Draft 2020-12 |
| `test_flag_default_off` | nothing | with `FLAG` unset, `run()` raises `FeatureDisabled` |
| `test_license_on_signing_host` | key file | `verify().licensed is True`; otherwise `pytest.skip("license_unverifiable_without_host_key")` |
| `test_refuses_wrong_model` | key file | `run_with(Runtime, FakeAdapter(model="not-" + artifact.model), ...)` raises; otherwise the same skip |
| `test_fake_smoke` | key file and artifact model `fake` | one `run_with` using the fake provider; otherwise `pytest.skip("live_model_required")` |

The generated tests never make network calls. W7: "key file" means a license this host can check, either with the key file or with a trusted public key for the report's Ed25519 seal. The license tests skip only when `verify()` reports `license_unverifiable`; `key_untrusted` and every other refusal fail them. The skip text is unchanged. CI should also run `architect verify --require-key <fingerprint> <package>`, which refuses a copy stripped back to the HMAC.

### 12.7 `export verify`

`architect export verify <package-dir> [--profile P] [--json]` runs `verify_export`. With a profile, it also compares the package against the registry entry by digest (`registry_match`). It prints the `EVIDENCE.json` table and the codes, and exits 0 only when integrity, pins, children and the license all pass.

`--reverify` (paid, consent `reverify: Stage 4 on <floor>; cap $X (API-equivalent, not a bill)`) re-runs Stage 4 of the approved Spec against the profile floor. It is bounded by `max_verifications_per_spec`, and a failure deletes nothing.

---

## 13. Surfaces

### 13.1 CLI (`architect/cli.py`)

```
architect route <request_or_spec> --profile P [--origin cli|session|orchestrator] [--json]
architect route <spec.json> --profile P --run INPUT_JSON --yes
architect compile|recompile|create ... [--no-route]            (existing flags unchanged)
architect do <goal> --profile P --repo R [--reuse-installed] [--no-route] [forge flags]
architect forge run <request_or_spec> --profile P --input JSON|@FILE [--cases FILE] [--reuse-installed] [forge flags] [--yes] [--json]
architect forge integrate <request> --profile P --repo R --into DIR [--approved-spec F] [--package NAME]
                          [--stage-only | --package-only] [--reuse-installed] [forge flags] [--yes] [--json]
architect forge report <artifact-dir> [--json]                  (unchanged)
architect export verify <package-dir> [--profile P] [--reverify] [--json]
architect roles --profile P [--json]
```

- **Forge flags** are the Phase 9 set from Section 12.2. `forge run` and `forge integrate` force `enabled=true, trigger=always` and accept `--forge-usd`, `--forge-minutes`, `--forge-model` and `--forge-mode`.
- **`route`**:
  - Prose runs Stage 0 first and prints `route: stage 0 cost $X (API-equivalent)` on stderr. A Spec file costs $0.
  - It never compiles.
  - `--run` executes only `reuse_installed` (Section 10.1); any other action gives `route_no_reuse` with nothing run.
- **`do --reuse-installed`** grants reuse to the orchestrator origin for this invocation only.
- **`--no-route`** disables the router for that compile, for A/B comparison.
- **`roles`** prints the policy and each role's variants with `n`, `p_lcb`, `$`, wall, class and `proxy_completed`, plus judge telemetry marked `report only`.
- All commands emit JSON on stdout as today. Tables go to stdout only when `--json` is absent, for `route`, `roles` and `export verify`.

### 13.2 Session (`session/commands.py` `VOCABULARY`, `help_for`, `_disallowed`; `session.py`)

| State | Verb | Behavior |
|---|---|---|
| READY | `roles` | the `architect roles` table |
| REVIEW | `route` | the Section 8.4 table for the Spec under review. It is pure: it never runs, approves or compiles |
| REVIEW | (automatic) | `_enter_review` prints one line: `route: <action>[ -> <target12>] (<codes>)`. For a warm start: `route: warm start from <key12> (still verified)` |
| REVIEW | `reuse <name> <hash8> {json}` | `hash8` is the Spec's `spec_hash[:8]`, the same typed-token pattern as `install <name> <hash8>`. It grants reuse for this call, runs Section 10.1, and then `_run_artifact` |
| REVIEW | `forge run {json}` | requires `lifecycle expire`, else `forge_run_needs_expire` with the hint `set lifecycle expire`; the consent flow is Section 12.1 |
| REVIEW | `forge integrate <repo> <into>` | requires a prior `approve <hash8>` for this Spec, else `approval_required`; the flow is Section 12.2, with session confirmation for both consents |
| RESULT | `export` | stages only, for an installed licensed result; prints `export staged: <path>` |
| RESULT | `rate good\|bad` | writes a `RatingRecord` for the last delivery `run_id` in this session, else `rate_no_run` |

The session invariants are unchanged: `approved=` is written only in `Session._compile`, and `_approved_hash` only in `approve <hash8>`. A test greps both.

### 13.3 HTTP (`server.py`; `phase6-http.md` amended from four operations to five)

`POST /route`, body `{"spec": Spec}`:

- A missing Spec or a prose `request` gives `route_spec_required`.
- It spends $0 and runs no Stage 0, no execution and no exploration. The origin is `http`, and reuse is never granted.

Response:

```json
{"action": "...", "artifact_id": "catalog id or null", "fit_tier": "A|B|C|none", "fit": 0.0,
 "quality_lcb": null, "expected_cost_usd": null, "latency_s": null, "latency_basis": "measured|bound|null",
 "evidence": ["..."], "codes": ["..."]}
```

- `artifact_id` is set only when `catalog.find_installed(name)` already knows the Tier A target; otherwise it is null with `route_not_catalogued`.
- The response carries no names, paths or text.
- `/run` is unchanged: installed runs still need `enabled` and `human_reviewed`.
- There are no HTTP Forge endpoints.
- `_FAILURE_CODES` gains: `route_spec_required`, `route_no_candidate`, `route_unlicensed`, `route_budget`, `route_drift`, `route_model_unconfigured`, `route_not_catalogued`, `evidence_store_unavailable`.
- `client-ts` gains `route(spec)` with typed DTOs.

### 13.4 Fixed codes (one ASCII sentence each in `session/codes.py`; the CLI and server share them)

| Code | Sentence |
|---|---|
| `route_reuse_installed` | An installed, licensed harness for exactly this task slot was chosen. |
| `route_warm_start` | A licensed template with the same input and output shape seeds the compile; Stage 4 still decides the license. |
| `route_compile` | No reusable version fits; a normal compile runs. |
| `route_forge_suggested` | Earlier compiles of this task failed Stage 4; rerun with --forge to let Forge try. |
| `route_no_candidate` | No configured model can serve this task. |
| `route_unlicensed` | The candidate failed a fresh license check and was skipped. |
| `route_evicted` | The template was evicted after failed reuse and was skipped. |
| `route_consumed` | The candidate was already consumed and was skipped. |
| `route_budget` | The candidate's measured cost or compute exceeds this task's cap. |
| `route_drift` | The installed entry is drifting; it is offered only as a template. |
| `route_model_unconfigured` | The candidate's model is not configured in this profile. |
| `route_synthetic_excluded` | Evidence from the fake provider cannot rank a real-model task. |
| `route_reuse_not_granted` | An exact installed match exists, but reuse was not granted for this request. |
| `route_no_reuse` | The decision is not an installed reuse, so nothing was run. |
| `route_target_changed` | The chosen version changed on disk after routing; nothing was run. |
| `route_spec_required` | Routing over HTTP needs a Spec, not prose. |
| `route_not_catalogued` | The matching installed entry is not in the server catalog. |
| `route_explore` | A less-tried licensed template was chosen to gather evidence; Stage 4 still gates it. |
| `route_explore_cap` | Today's exploration budget is spent; the top candidate was used. |
| `route_promote_proposed` | A licensed version may beat the installed one; a recompile would test it. |
| `route_rollback_suggested` | The installed entry is drifting and an earlier revision scored better. |
| `route_model_unknown` | A routing candidate names a model missing from the profile. |
| `route_param_invalid` | A role variant uses a parameter outside its closed set. |
| `fit_slot` | The best match is the exact same task slot. |
| `fit_shape` | The best match shares input, output, tools and domain. |
| `fit_family` | Only a structurally similar task family exists. |
| `fit_none` | No similar verified version exists. |
| `evidence_synthetic_only` | All evidence comes from the fake provider and proves plumbing only. |
| `evidence_insufficient` | Too few held-out cases or reuse trials to rank with confidence. |
| `evidence_store_unavailable` | Routing evidence could not be read; a normal compile runs. |
| `role_evidence_insufficient` | Too few outcomes to switch this role; the pinned default is used. |
| `role_explore` | A less-sampled role variant was used to gather evidence. |
| `role_pinned` | This role is pinned by the operator; routing is off for it. |
| `forge_run_delivered` | The Forge harness was licensed, run once and expired. |
| `forge_run_unlicensed` | The Forge harness was not licensed, so it was not run. |
| `forge_run_reused_installed` | An installed licensed harness for this exact task was run instead of building. |
| `forge_run_needs_expire` | Forge run builds a one-shot harness; set lifecycle expire first. |
| `forge_cases_too_few` | At least 8 cases are required so each half has at least 4. |
| `forge_cases_invalid` | A case row is invalid, duplicated or does not match the schemas. |
| `forge_cases_floor_kind` | User cases need a held_out_tasks floor. |
| `integrate_reused_installed` | The approved feature is already installed and licensed; it was reused. |
| `integrate_not_ready` | The package can be placed only after compile_agent succeeds. |
| `export_cancelled` | Export was not confirmed; nothing was written to the repository. |
| `export_target_exists` | The package destination already exists. |
| `export_path_invalid` | The package destination is outside the repository or not allowed. |
| `export_package_invalid` | The package name must be a lowercase Python identifier of at most 40 characters. |
| `export_unverified` | Only an installed, licensed harness can be exported or run. |
| `export_pin_mismatch` | The exported harness does not match its pinned digests. |
| `export_integrity_mismatch` | An exported file differs from the manifest. |
| `export_subharness_missing` | A bound sub-harness is missing or does not match its digest. |
| `export_failed` | The export could not be written safely. |
| `export_architect_missing` | The architect package is not installed in this interpreter. |
| `license_unverifiable` | Neither this host's signing key nor a trusted public key can check the license. |
| `key_untrusted` | It is signed by a key this workspace does not trust. |
| `unsigned` | No signature this host trusts covers these files. |
| `trust_in_target` | The signing key or trust list sits inside the folder being checked, so it cannot vouch for it. |
| `trust_list_missing` | ARCHITECT_TRUST_FILE names a file that does not exist, so no key is trusted. |
| `registry_subharness_missing` | The artifact's sub-harness tree is missing or invalid, so it was not installed. |
| `rate_no_run` | There is no delivery in this session to rate. |

The Phase 9 codes (Section 12.4) are unchanged and reused (`forge_cancelled`, `forge_verification_cap`, `forge_no_submission`, `forge_disabled`).

---

## 14. Bench integration (WP4)

- **Labels.** `bench/rows.py` and `bench/providers.py` import `Provider`, `Evidence` (= `routing.labels.EvidenceClass`) and the provider-to-class map from `routing.labels`. `_EVIDENCE` becomes an alias. The bench `FloorOrigin` literal stays as it is (a subset). A test asserts identity.
- **Rows** (backward compatible; old rows stay readable):
  - `Variant` gains `"routed"`.
  - `BenchRow.artifact_digest: SHA256_HEX | None = None`.
  - `BenchRow.route: RouteFields | None = None`, with `RouteFields(_Frozen)`:
    - `probe: Literal["exact", "sibling", "foreign"]`;
    - `action: RouteAction`;
    - `tier: Literal["A", "B", "C", "none"]`;
    - `candidates: NonnegativeInt`;
    - `chosen_rank: NonnegativeInt | None`;
    - `oracle_rank: NonnegativeInt | None`;
    - `regret: float | None`;
    - `compile_cost_avoided_usd: float`;
    - `fallback: bool`;
    - `explored: bool`;
    - `decision_ms: NonnegativeFloat`;
    - `round: NonnegativeInt = 0`.
- **Probes** (`suite.py`, derived deterministically from existing specs; no new datasets):
  - `exact`: the task's own spec, expected to route to Tier A.
  - `sibling`: the same schemas, tools and floor, with the task text replaced by the committed paraphrase in `examples/bench/suite-v1/siblings.json` (`{task_id: text}`). Expected Tier B.
  - `foreign`: the spec of the next task in suite order whose `shape` differs. Expected `compile`.
- **Routed cell** (`runner.py`):
  - **Phase A.** Compile the default arm, plus the `forge` and `template` arms when selected, into a fresh per-cell workspace. Each licensed result is installed with lifecycle `install` by exact override under a bench-local name (`<variant>-<task_id>`). The `slot` excludes lifecycle, so the exact probe still matches.
  - **Phase B.** For each probe, run `route()` with `grant_reuse=True` (bench origin), then execute the action through the Compiler (plus `Runtime` for reuse).
  - **Oracle.** Every eligible candidate is scored on the probe's held-out split. For an installed entry, the score is its Stage 4 `held_out_score`. For a template, it is a warm-started compile of the probe from that template. The comparison compile runs with no warm start (`--no-route`).
    - `oracle_rank` is the chosen candidate's rank by realized held-out score, ties by cost.
    - `regret = best held_out_score - chosen held_out_score`.
    - `compile_cost_avoided_usd` = the default-arm compile cost of the same probe minus the routed arm's compile cost.
  - `_size_cell` adds the Phase A cells, one compile per non-exact probe, and one per template candidate, up to `max_candidates`. This is $0 under fake and bounded by `--max-usd` when live.
  - **Guard.** A routed row whose exact probe had `candidates == 0` is written with failure `route_did_not_run`, and the bench exits non-zero, mirroring `forge_did_not_run`.
- **Learning curve.** `bench run --variant routed --rounds R --seed S` replays the suite R times in a seed-fixed shuffled order with one route log per bench. Rows carry `round`. `router_eval.learning_curve(rows)` reports, per round:
  - licensed rate, spend and mean regret;
  - exploration share;
  - promote and rollback proposal counts.
- **Summaries.**
  - `summarize()` groups by `(task_id, model, variant, probe)` and adds route accuracy (`chosen_rank == oracle_rank`), mean regret, mean cost avoided and fallback rate.
  - `router_eval.regret_table(rows)` also compares against the hindsight oracle over the default, template and forge variants and the ladder models present.
  - `render.py` adds the columns `probe`, `tier`, `rank/orc` and `dCost$`, with width at most 100.
  - `compare --pair-variants default:routed` is supported.
- **Pure simulation** (`bench/router_sim.py`, $0, no I/O):
  ```python
  def simulate(templates: Sequence[SimTemplate], installed: Sequence[SimInstalled], steps: int, seed: int,
               policy: RoutePolicy) -> SimResult
  def simulate_roles(variants: Sequence[SimVariant], steps: int, seed: int, routing: RoleRouting) -> SimResult
  ```
  It drives the real `route()` and `RoleRouter.choose` over an in-memory `EvidenceIndex` fake, with Bernoulli outcomes.
- **Roles arm.** `bench roles --policy pinned|evidence` replays `examples/bench/roles/*.jsonl` (committed fake outcome logs) through `RoleRouter`.
- **CI** (`bench-smoke`):
  - add `--variant routed` on the smoke tier at $0;
  - regenerate the smoke baseline on the Linux runner;
  - `bench/ci_assert.py` runs the pass criteria below.
  
  All smoke rows are `synthetic_fake`, and the docs say they prove plumbing and decision determinism, not routing quality.

### 14.1 Pass criteria

In CI (fake, deterministic):

1. The routed licensed rate is at least the default licensed rate on paired cells (`compare --pair-variants default:routed`); routing may not reduce licensing.
2. The exact probe gives `reuse_installed` in 100% of cells whose Phase A installed at least one entry. The foreign probe gives `compile` in 100%. The sibling probe gives `warm_start` whenever an eligible template with `r >= 0.3` exists.
3. No routed row has `route_did_not_run`, and every routed row has an evidence label.
4. Two identical runs produce identical `(action, target, chosen_rank)` sequences.
5. Simulation, over 2000 seeded trials:
   - exploration share is at most `1/explore_every + 0.01`;
   - the daily exploration cap is never exceeded;
   - evicted, unlicensed and consumed candidates are never chosen;
   - the false promote-proposal rate is at most 0.10 for equal true pass rates p in {0.5, 0.8, 0.95} at n = 30;
   - under `evidence`, a role variant with a true pass rate lower by at least 0.10 is chosen in at most 5% of trials at `min_samples = 20`;
   - under `pinned`, the default is chosen 100% of the time.

Live, which only the operator triggers and which is not gated in CI: report paired licensed rate, mean regret and mean cost avoided, with the evidence class shown. There is no pass or fail on quality claims.

---

## 15. Tests per package

All tests use fake adapters, a socket-blocking fixture, and temp workspaces with a throwaway `ARCHITECT_SIGNING_KEY_FILE`.

### 15.1 WP1

- Registry install and run of a Forge parent with one child (Section 4).
- A tampered child gives `registry_subharness_missing`.
- Library behavior is unchanged after the helper move (the existing library tests pass).
- Signature stability:
  - golden hex for fixed specs;
  - NFKC and casefold equivalence;
  - excluded fields do not change `slot`;
  - `tokens` is absent from `model_dump` and `json`.
- The fit tier table, and the F formulas at boundary values.
- Each hard filter code, with a fixture per code.
- The ranking tables for Tier A and Tier B keys; the tie-break is library sequence.
- The decision rule matrix over (Tier A present, grant, Tier B r) giving the expected action and codes.
- `load_reuse_target` gives `route_target_changed` when the artifact is swapped on disk between route and execute, and `route_unlicensed` when the report is tampered.
- The router never calls `install` or writes `approved`; checked by monkeypatch spies.
- Exploration: determinism with the persisted counter, the daily cap, and never on HTTP or INSTALL.
- Proposals fire only under the stated inequalities, and `Registry.drift` is read-only (no row count change).
- Migrations are idempotent (open twice) and survive a crash mid-backfill.
- The evidence cache offset is incremental; at most one full replay after the cache is deleted.
- Content-freeness of all record types; `evidence_store_unavailable` on an unreadable `routes/`.

### 15.2 WP2

- Pinned-policy adapter identity per role.
- The evidence policy's non-inferiority decisions on fixed fake logs, and the threshold at exactly `min_samples`.
- The inference quote cap of $1.00.
- The judge is never routed.
- `route_model_unknown` and `route_param_invalid`.
- Pins disable routing (`role_pinned`).
- Evidence classes are never mixed after a provider change.
- Floor-origin weights.
- `WORKSPACE_STATE` includes `routes` and `exports`, and `pi_denied_paths` covers them.

### 15.3 WP3

`forge run` in template mode under fake:

- the lifecycle is expire;
- it is delivered once, and `.consumed` exists;
- `harness.py` is removed while `forge.json` and `report.json` are kept;
- a library template is stored;
- the summary line format is exact.

Also for `forge run`:

- cancel gives `forge_cancelled` with $0;
- unlicensed gives `forge_run_unlicensed` with no `Runtime.run` call;
- `--reuse-installed` runs the installed entry with no build.
- Cases: the dataset tag binds the file hash, fewer than 8 rows gives `forge_cases_too_few`, and invalid, duplicate and floor-kind errors are reported; the cases path appears in the effective denied paths.

`forge integrate`:

- Without approval: a proposal is written, no Forge line prints, and $0 is spent.
- Export:
  - the layout is exact;
  - the manifest hashes are correct;
  - EVIDENCE is content-free, and a scan finds no case strings from the floor fixture;
  - symlinks give `export_failed`;
  - EXPIRE entries give `export_unverified`.
- The generated tests run in a temp repo via a `pytest` subprocess: they pass with the key and skip the license test without it; `test_flag_default_off` passes.
- The place tool:
  - `integrate_not_ready` before `compile_agent` succeeds;
  - an exact `into` binding, with `export_path_invalid` otherwise;
  - `..`, `.architect` and a link each give `export_path_invalid`;
  - an existing target gives `export_target_exists`;
  - a second call is refused by `max_calls`;
  - the tool is absent when `is_evaluation`.
- `--stage-only` leaves the repo tree hash unchanged. `--package-only` changes only the new directory.
- Post-run tampering gives `export_integrity_mismatch`.
- The `discard_code` move is byte-identical in behavior.

### 15.4 WP4

- `RouteFields` round-trips and old rows remain readable.
- Label identity with `routing.labels`.
- Probe derivation is deterministic.
- The oracle and regret on fake cells.
- The `route_did_not_run` guard.
- `compare --pair-variants default:routed`.
- Render width at most 100.
- The learning-curve rounds.
- The simulation criteria of Section 14.1 (5).
- The roles arm under both policies.

### 15.5 WP5

- End-to-end fake sessions and CLI runs for every new verb and flag.
- An HTTP `/route` contract test: Spec only, `route_spec_required` for prose, no names in the body, and `/run` still requires its flags.
- No `approved=` write outside `Session._compile` (a grep test).
- `_deliverable` is unchanged; `_deliverable_routed` has a truth table; an installed `harness.py` survives a routed `do`.
- `--no-route` gives identical results to a Compiler without a router.
- The Forge-agent line prints before the Phase 9 line.
- The package boundary (Section 17.6).
- A full `uv run pytest`, and the bench-smoke baseline regenerated on Linux.

---

## 16. Contract amendments (WP5 edits these docs in the same change)

- `phase6-http.md`: five operations; `POST /route`; new `_FAILURE_CODES`; the TS client exposes `route`.
- `phase7-session.md`: the verbs in Section 13.2 and the automatic REVIEW route line.
- `phase8-bench.md`: the `routed` variant, `RouteFields`, `BenchRow.artifact_digest`, `--rounds`, the roles arm, labels moved to `routing.labels`, and `route_did_not_run`.
- `phase9-forge.md`:
  - Section 11: `child_warm_start`.
  - Section 12.2: `forge run`, `forge integrate` and the `forge agent:` pre-line.
  - Section 12.4: new codes added and no existing code changed.
  - The registry install now copies `subharnesses/` and `forge.json`.
- `docs/spec.md:69-73` note: the router is evidence-based over schema and slot hashes. It has no keyword table, and Stage 1 stays rule-based.

## 17. Honesty rules

1. Routing orders licensed versions; it never licenses. Every surface that shows a route also shows that Stage 4 decides.
2. Every quality number shows its evidence class, and `synthetic_fake` numbers are labelled "plumbing, not quality".
3. `latency_bound` is shown whenever latency is a per-case maximum and not a measurement.
4. `drift_window_none` is shown instead of inventing a drift signal.
5. `proxy_completed` is shown for telemetry `accepted`, which is never presented as correctness.
6. **Package boundary.** `architect.routing` imports no bench, compiler, profiles, orchestrator, session, server, CLI, backends, forge_modes or games modules. Nothing imports `architect.bench`. Only `forge_modes/export.py` and `place_tool.py` write export trees.
7. Exported packages say "host-attested", and a license test skips rather than faking a pass without a key.
8. Build (agent) cost, compile cost and play cost are always reported separately.
9. Tier A is rare for free prose, and this is stated in `docs/limitations.md`.

## 18. Non-goals

- No live-provider, `claude`/`codex` CLI or Ollama calls in any test or CI job. The only local GPU use remains the ARC experiment, per the operator's rule.
- No embedding or learned router, no keyword table, and no change to Stage 1 preset rules.
- No automatic registry swap, rollback or Forge arming.
- No key-less or digest-pinned run mode for exported packages, and no shipping of signing keys.
- No cross-model transfer of a harness without a full compile. Tier B is same-model only.
- No HTTP Forge endpoints and no Forge rescue of `selection_failed`.
- No routing of the judge.

## 19. Work packages

- **WP0 (lead, first).** This document is committed as `docs/interfaces/phase10-router.md`.
- **WP1: routing core and stores** (Section 3 rows marked WP1).
  - Commit 1 is the registry subharness fix and its regression test (Section 4), and it lands before anything else merges.
  - Then `routing/labels`, `signature`, `log`, `evidence`, `router` and `proposals`; the registry `slots`, `find` and `drift`; and the library `shape`, `candidates` and `ReadOnlyLibrary`.
- **WP2: role routing.** `routing/roles.py` and `profiles.py`: `Profile.routing: RoutePolicy`, `Profile.roles: RoleRouting`, `Services.router: Router | None`, `Services.roles: RoleAdapters`, and `WORKSPACE_STATE += ("routes", "exports")`. It codes against the WP1 interfaces fixed here, using a stub `EvidenceIndex` until WP1 lands.
- **WP3: Forge modes and export.** `forge_modes/**`, `runtime/ephemeral.py` and the `features.py` integrate hook. It depends only on WP1 commit 1 and on the `Router` and `RouteDecision` signatures fixed here.
- **WP4: bench.** Bench modules, examples and CI job flags. It uses a stub `route()` until WP1 lands, and runs the real routed cells after WP5.
- **WP5: integration** (after WP1 to WP4):
  - compiler seams and `CompileResult.route` and `.reused`;
  - `forge_tools._spawn`;
  - the orchestrator predicate and discard guard;
  - session, CLI, server and client-ts;
  - `tests/test_package_boundary.py`;
  - the doc amendments;
  - the gates in Section 15.5.
  
  WP5 is merged last, and the bench-smoke baseline is regenerated on Linux in that same change.

## 20. Risks bounded by this contract

- **Evidence is thin.** Delivery rows carry no score, drift windows exist only for games, and Stage 4 cost and compute are maxima. The lexicographic conservative-margin rule, and showing the labels, keep the router from overclaiming.
- **Role outcome proxies are confounded with task difficulty.** Evidence routing is therefore opt-in, needs `min_samples`, and is non-inferiority based, and the simulation bounds false switches.
- **Keys and portability.** Exported packages run only where the signing key and `architect` are present. Giving CI the key widens trust, and the docs say so.
- **Migrations.** The `slots` backfill loads each entry once, and the 246-character path cap applies to the registry and exports. Both are tested for idempotency and crash-safety.
- **Nondeterminism.** Decisions change as evidence accumulates, but they are deterministic for a given log snapshot. Bench runs use a fresh per-bench workspace.
- **The existing bench `--route` flag** (`vision/solver/verifier`, `cli.py`) is unrelated. No new flag reuses the name `--route` on `bench`, and the router is selected with `--variant routed`.