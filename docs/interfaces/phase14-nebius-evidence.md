# Phase 14: gate integrity and a public evidence bench

Status: contract (2026-10-03), not yet implemented. It extends `docs/interfaces/phase8-bench.md` and `docs/interfaces/phase9-forge.md` and changes no existing subcommand, code or default. Where this text and the code differ after implementation, the code, `docs/usage.md` and `docs/bench.md` are authoritative. Line references are to commit `3d6f304` on `hackathon/nebius`. The plan items come from `.jakeos/hackathon/PLAN.md` (Sections 0, 2 W4 and W5, and 4); the confounds this phase removes are listed with file and line in `.jakeos/hackathon/recon/report-evidence.md` Section 3.

This document is the contract for three workers building in parallel on one tree (WA, WB and WC, Section 2) and one integrator. Every public name, type, file, closed label, error code, JSON shape and operator-facing string below is fixed. Anything not listed belongs to the author of the file it lives in.

## 1. Goal and fixed properties

Phase 14 delivers seven things:

1. **Invalid outputs (W4a).** An opt-in spec policy that counts an invalid run as a miss, with a hard ceiling of 2% of the partition, and the counts (correct k of n, invalid j) in the signed report.
2. **Public dataset floors (W4b).** A floor whose cases match a pinned sidecar manifest is labelled `public_dataset` and shown as "CORD v2 · CC BY 4.0 · 200 receipts". Any mismatch labels it SYNTHETIC.
3. **The `receipt_v1` checker.** Exact match on `total`, `subtotal`, `tax` and `item_count` under `examples/receipts/RULES.md`, with field accuracy as a secondary measure.
4. **Frozen specs (W4d).** `/make @spec.json` compiles exactly the pre-registered spec and prints its full hash on the plan card.
5. **The Forge audit (W4e).** Every Forge submission is copied, with its tuning measurement, into a directory Forge cannot read.
6. **Fair arms (W5).** A per-spec input limit, tolerant reply parsing equal to Forge's `extract_json`, a plain arm pinned in the spec, and model aliases so ids containing "/" work in the bench.
7. **Bench suite v2 (W5).** A `public` tier with the `receipts` task loaded from `examples/receipts`, per-run delivery metrics on rows, paired statistics across arms, a Markdown report, a replay of audited submissions on the exam, and the skeleton of the pre-registered analysis.

Fixed properties, inherited and never weakened:

- **Stage 4 alone licenses.** Nothing in this phase constructs, edits or re-signs a `TestReport` outside `compiler/verification.py`. The bench audit replay (13.2) calls that same `verify()` in a throwaway workspace with a throwaway key, as the bench runner already does through `Compiler.compile`.
- **Held-out isolation.** No held-out case, score, count or verification id reaches a Forge tool result, a Forge workspace file, the audit directory, a stage or result observer (`compiler/__init__.py:65-80`, `compiler/forge_tools.py:121-145`), or a bench progress event. The new report counts live only in `report.json` and in what is read from it after `compile()` returns.
- **Defaults change nothing.** With every new field at its default, every `spec_hash`, `artifact_digest`, emitted `harness.py` byte, report signature, telemetry row and the v1 suite digest `1f23e5e421b2c2c3d79482394a841764b37f4e70d3b06a73993fc7bb4bf335ed` are identical to today. Rows, reports, specs and run records written before this phase still parse, verify and license.
- **Money and time only.** Budgets are dollars and seconds. Tokens are metered, never budgeted. The profile `max_tokens` field (9.4) is the request parameter W1 already sends on every call; it bounds a reply and its reservation and is not a budget.
- **Content-free records.** Bench rows, audit metadata (`submission.json`, `session.json`, `audit.jsonl`), provenance results and analysis output hold counts, dollars, seconds, closed labels and hashes. No prompt, receipt text, output, expected value or case id enters them (case ids appear only inside the dataset manifests the data script writes). The audit directory also keeps each submitted `harness.py` and its spec, by design: they are what the replay in 13.2 runs.
- **Synthetic stays synthetic.** Fake-provider rows (`synthetic_fake`), generated or drafted floors, SYNTHETIC floors and rows measured under unfrozen v2 settings are never presented as results. Every renderer in this phase says so in plain words (Section 18).
- **Tests are offline.** No live provider, CLI or local model server runs in any test.
- **Rendering.** Terminal tables stay ASCII-safe, at most 100 columns and deterministic. Every outward string fixed here is plain English: no em or en dashes, straight quotes, sentence case.

## 2. Workers and files

| Worker | Scope |
|---|---|
| WA | Gate integrity: Spec and report schema, evaluation, verification, floors (checkers and provenance), frozen specs, the Forge audit hook |
| WB | Fair arms: presets, plain tuning, reply parsing, the profile model alias and output cap, the model id rule |
| WC | Bench: suite v2, the receipts task, runner metrics, rows, paired comparison, reports, the audit replay, the analysis skeleton, bench CLI |

### 2.1 File ownership

| File | Change | Owner |
|---|---|---|
| `architect/spec/__init__.py` | Spec fields, serializer, validators, `invalid_allowance`, `CaseCounts`, `TestReport.held_out_counts`, `licensed` checks (3.1, 3.2) | WA |
| `architect/compiler/types.py` | `Measurement.counts` only; W3 owns the rest of the file | WA |
| `architect/runtime/evaluation.py` | run classes and `count_as_miss` scoring (4) | WA |
| `architect/compiler/verification.py` | counts on the report, `primary_invalid_exceeded` (4.3) | WA |
| `architect/compiler/inference.py` | Stage 0 never proposes the Phase 14 fields (3.1) | WA |
| `architect/floors/checkers.py` | `receipt_v1` and its `field_report` entry (6) | WA |
| `architect/floors/provenance.py` (new), `architect/floors/__init__.py` | manifest, check, card text (5) | WA |
| `architect/compiler/forge_audit.py` (new) | audit API (8) | WA |
| `architect/compiler/forge.py`, `architect/compiler/forge_tools.py` | only the hooks in 8.3; W3 owns everything else in both files | WA |
| `architect/routing/labels.py` | `FloorOrigin` gains `"public_dataset"` | WA |
| `architect/agent/build.py` | provenance row, frozen build, counts readout, audit context, export origin (4.4, 5.4, 7) | WA |
| `architect/agent/commands.py`, `commands_forge.py`, `tools.py`, `repl.py`, `render.py`, `export.py` | `/make @`, `/forge @`, the `spec_file` tool argument, print mode, the ASCII separator, `_floor_origin` (5, 7) | WA |
| `architect/server.py` | two codes in `_FAILURE_CODES` (14) | WA |
| `architect/presets/_execution.py`, `architect/presets/_json.py` (new) | input limit, reply parsing, schema mode (9.1, 9.2) | WB |
| `architect/compiler/tuning.py`, `architect/compiler/selection.py` | plain configuration, `context_window` pin (9.1, 9.3) | WB |
| `architect/models/types.py` | `MODEL_ID_PATTERN` (9.5) | WB |
| `architect/models/http.py` | `api_model` and the output cap in `_HTTPAdapter` and `OpenAICompatibleAdapter` (9.4) | WB |
| `architect/bench/suite.py`, `public_tasks.py` (new), `runner.py`, `compare.py`, `render.py`, `oracle.py`, `audit.py` (new), `__init__.py` | Sections 10 to 13 | WC |
| `examples/bench/suite-v2/receipts.json` (new), `examples/bench/README.md` | settings file and usage (10.2) | WC |
| `scripts/analyze_prereg.py` (new) | analysis skeleton (13.3) | WC |
| Tests | Section 15 | each worker |
| `docs/bench.md`, `docs/usage.md`, `docs/limitations.md`, `DECISIONS.md`, `PROGRESS.md`, `.jakeos/index.md` | written after the merge | integrator |

Shared files, each split by section so two workers never edit the same lines:

| File | WA | WB | WC |
|---|---|---|---|
| `architect/profiles.py` | `FloorConfig.manifest` | `AdapterConfig.api_model`, `AdapterConfig.max_tokens`, `_adapter`, `model_unpriced` | none |
| `architect/cli.py` | `--audit` on the compile-family parsers and handlers | none | `_bench_parser`, the `_bench_*` handlers, `_pair_variants`, the new `_pair_arms` |
| `architect/bench/rows.py` | none | the `ModelId` line | everything else |
| `architect/bench/providers.py` | none | the `_MODEL_ID` line | none |
| `architect/session/codes.py` | its codes in one block | none | the bench mirror in one block |

Package boundary: `architect.bench` may import `architect.floors.provenance` and `architect.compiler.verification`; nothing outside `architect/bench/`, `architect/cli.py`, `architect/session/bench.py` and `scripts/` imports `architect.bench` (`tests/test_package_boundary.py:215` keeps holding). `architect/compiler/forge_audit.py` imports neither `architect.floors._access` nor `architect.compiler.verification`, like every `forge*.py` module.

### 2.2 Day 0

Each worker lands its shared types verbatim from Section 3 as its first change, so the others import real types instead of stubs:

- **WA:** the five Spec fields with serializer and validators, `invalid_allowance`, `CaseCounts`, `TestReport.held_out_counts`, `Measurement.counts`, `CheckerName` with `"receipt_v1"`, `FloorConfig.manifest`, the models and `case_sha256` of `floors/provenance.py`, the `forge_audit.py` API (its hooks may land later), `FloorOrigin` with `"public_dataset"`.
- **WB:** `MODEL_ID_PATTERN`, the `ModelId` and `_MODEL_ID` rule, `AdapterConfig.api_model` and `AdapterConfig.max_tokens` with their validators.
- **WC:** the bench labels (`Tier`, `TaskId`, bench `FloorOrigin`), the new `BenchRow` and `BenchConfig` fields with defaults, the `CheckerName` mirror, `SUITE_TIERS` and `tiers_for` with the v1 digest test.

The integrator merges, runs the full suite and owns every acceptance check in Section 16. A worker's own green tests never advance the phase.

## 3. Shared types

### 3.1 Spec (WA)

```python
# architect/spec/__init__.py
InvalidPolicy = Literal["fail_partition", "count_as_miss"]
ReplyJson = Literal["strict", "first", "last", "schema"]
PLAIN_PRESETS: frozenset[str] = frozenset(
    {"extractor", "synthesizer", "recommender", "verifier", "explorer", "planner"}
)
DEFAULT_MAX_INVALID_FRACTION = 0.02

class Spec(_Schema):
    ...  # every existing field unchanged and in the same order, then:
    invalid_policy: InvalidPolicy = "fail_partition"
    max_invalid_fraction: Annotated[float, Field(ge=0, le=0.1, allow_inf_nan=False)] = 0.02
    input_limit_chars: Annotated[int, Field(ge=256, le=1_000_000, strict=True)] | None = None
    reply_json: ReplyJson = "strict"
    plain: Annotated[bool, Field(strict=True)] = False

def invalid_allowance(spec: Spec, cases: int) -> int:
    """Invalid runs a partition of ``cases`` may hold without failing: 0 under
    fail_partition, else floor(max_invalid_fraction * cases + 1e-9)."""
```

**Serialization.** A `model_serializer(mode="wrap")` on `Spec` removes, from every dump (`model_dump`, `model_dump_json`, nested dumps):

- `invalid_policy` and `max_invalid_fraction` when `invalid_policy == "fail_partition"`;
- `input_limit_chars` when it is None;
- `reply_json` when it is `"strict"`;
- `plain` when it is False.

So `spec_hash` (`spec/__init__.py:131`), `spec.json`, the spec literal emitted into preset `harness.py` (`presets/__init__.py:113` and `:132`), router signatures and bench `spec_hash` columns are byte-identical for every spec that leaves the five fields at their defaults, and a spec file that writes a default explicitly (`"invalid_policy": "fail_partition"`) hashes like one that omits it. Under `count_as_miss` both the policy and the fraction are hashed, the default 0.02 included. Validation (`model_validate`, `model_validate_json`, `model_json_schema`) is unchanged by the serializer.

**Validators** (`mode="after"`, `ValueError`; callers report their existing spec codes such as `invalid_spec` and `invalid_spec_file`):

1. `invalid_policy == "fail_partition"` with `max_invalid_fraction != 0.02` is refused, so two different files can never share a hash.
2. `count_as_miss` needs `floor.kind == held_out_tasks` and `secondary_floor is None`.
3. `plain` needs `preset_hint in PLAIN_PRESETS`, `pipeline == ()` and `execution_config.get("kind")` not in `("game", "competition")`.

**Stage 0.** `compiler/inference.py:263` removes the five keys from the schema it shows the inference model (as it already removes `provenance`), drops them from the model's proposal before merging, and keeps the `provenance` keys to the fields that existed before this phase. Stage 0 prompts and inferred specs are therefore byte-identical to today; only a user or a frozen spec file sets these fields.

### 3.2 Counts and the signed report (WA)

```python
# architect/spec/__init__.py
class CaseCounts(_Schema):
    """How one partition's runs ended; counts only."""
    cases: NonnegativeInt
    correct: NonnegativeInt | None   # None when the partition was not scored
    invalid: NonnegativeInt          # runs with no schema-valid output, budget aside
    over_budget: NonnegativeInt      # runs stopped by the per-run dollars or seconds
    # validator: (correct or 0) + invalid + over_budget <= cases, and
    #            correct is not None implies over_budget == 0

    def readout(self, sep: str = " · ") -> str: ...

class TestReport(_Schema):
    ...  # every existing field unchanged
    held_out_counts: CaseCounts | None = None   # the primary floor; omitted from dumps when None
```

`readout` returns exactly `f"correct {correct}/{cases}{sep}invalid {invalid}"` when scored, else `f"not scored{sep}invalid {invalid}{sep}over budget {over_budget}"`. Examples: `correct 93/100 · invalid 1`; ASCII callers pass `sep=" | "` and get `correct 93/100 | invalid 1`. `correct` is filled only for `held_out_tasks` floors, so callers show the readout only for those.

**Signature compatibility.** A wrap serializer on `TestReport` omits `held_out_counts` when it is None. The signed payload (`model_dump(mode="json", exclude={"signature"})`, `compiler/verification.py:34`) of every report written before this phase is therefore byte-identical and its HMAC still verifies. Every report Stage 4 writes from now on carries the counts, under both policies, and the counts are covered by the signature. W7's Ed25519 signature signs this same payload.

**`licensed` checks.** `HarnessArtifact.licensed` (`spec/__init__.py:262`) adds these, applied only when `held_out_counts` is present, so they can only refuse:

- `cases == held_out_count` when the spec has no secondary floor, else `cases <= held_out_count`;
- `over_budget == 0`;
- `invalid <= invalid_allowance(spec, cases)`;
- for a `held_out_tasks` floor with `correct` not None: `abs(correct / cases - floor_score) <= 1e-9`.

### 3.3 Measurement (WA)

`Measurement` (`compiler/types.py:41`) gains `counts: CaseCounts | None = None`. `_evaluate_cases` fills it on every measurement: Stage 2 candidates, Forge measures and submissions, and Stage 4.

### 3.4 Profile fields

```python
# architect/profiles.py
class FloorConfig(_Settings):          # WA
    ...
    # Sidecar dataset manifest (Section 5), relative to the profile file like ``path``.
    # Only held-out-tasks floors may name one (ValueError otherwise).
    manifest: Path | None = None

class AdapterConfig(_Settings):        # WB
    ...
    # OpenAI-compatible adapters only (ValueError otherwise). The provider's model id when
    # the profile key is an alias: sent as "model", priced, and checked against the reply.
    api_model: Annotated[str, StringConstraints(pattern=MODEL_ID_PATTERN, max_length=128)] | None = None
    # OpenAI-compatible adapters only: a ceiling on every call's max_tokens and reservation.
    max_tokens: Annotated[int, Field(ge=16, le=1_048_576, strict=True)] | None = None
```

`NamedAdapterConfig` inherits both, so role adapters (`inference`, `forge_agent`, ...) can be aliases too.

### 3.5 Closed labels

- `architect.floors.checkers.CheckerName` gains `"receipt_v1"` (WA); `architect.bench.suite.CheckerName` mirrors it (WC) and the existing mirror test keeps the two equal.
- `architect.routing.labels.FloorOrigin` gains `"public_dataset"` (WA). It is not down-weighted: `floor_origin_weight("public_dataset") == 1.0`.
- `architect.bench.rows` (WC): `Tier` gains `"public"`, `TaskId` gains `"receipts"`, the bench `FloorOrigin` gains `"public_dataset"`.
- `architect.bench.rows.ModelId` follows `MODEL_ID_PATTERN` (WB, 9.5).

### 3.6 Bench config and rows (WC)

```python
# architect/bench/runner.py, BenchConfig: new fields, all defaulted
    tasks: tuple[TaskId, ...] = ()                       # --tasks; empty keeps the whole tier
    tuning_replay: bool = True                           # --no-tuning-replay sets False
    plain: bool = False                                  # --plain
    reply_json: ReplyJson | None = None                  # --reply-json; None keeps the task's
    floor_split: Literal["evidence", "rehearsal"] = "evidence"     # public tasks only
    delivery_split: Literal["delivery", "pilot"] = "delivery"      # public tasks only
    delivery_n: Annotated[int, Field(ge=0, le=10_000)] | Literal["all"] = 0
    audit: Path | None = None                            # --audit DIR (Section 8)

# architect/bench/rows.py, BenchRow: new fields, all defaulted, appended after ``route``
    api_model: Annotated[str, Field(max_length=128)] | None = None
    plain: bool = False
    reply_json: ReplyJson = "strict"
    invalid_policy: InvalidPolicy = "fail_partition"
    floor_split: Literal["evidence", "rehearsal"] | None = None
    delivery_split: Literal["delivery", "pilot"] | None = None
    floor_cases_sha256: SHA256_HEX | None = None
    delivery_cases_sha256: SHA256_HEX | None = None
    suite_frozen: bool | None = None
    tuning_replay: bool = True
    held_out_correct: NonnegativeInt | None = None
    held_out_invalid: NonnegativeInt | None = None
    held_out_over_budget: NonnegativeInt | None = None
    held_out_mean_cost_usd: NonnegativeFloat | None = None
    compile_cost_usd: NonnegativeFloat | None = None
    delivery_pass_bits: Annotated[str, Field(pattern=r"^[0-9a-f]*$")] | None = None
    delivery_invalid: NonnegativeInt | None = None
    delivery_over_budget: NonnegativeInt | None = None
    delivery_cost_usd: NonnegativeFloat | None = None
    delivery_calls: NonnegativeInt | None = None
    delivery_zero_call_runs: NonnegativeInt | None = None
    delivery_p50_s: NonnegativeFloat | None = None
    delivery_p95_s: NonnegativeFloat | None = None
    delivery_run_costs_usd: tuple[NonnegativeFloat, ...] | None = None
    delivery_field_accuracy: Score | None = None
    # review fix: which runs were invalid and which went over budget (Section 11)
    delivery_invalid_bits: Annotated[str, Field(pattern=r"^[0-9a-f]*$")] | None = None
    delivery_over_budget_bits: Annotated[str, Field(pattern=r"^[0-9a-f]*$")] | None = None
```

Section 11 defines every value. `run.json` (Phase 11 resume) stores the new `BenchConfig` fields; records written earlier load with the defaults and `RUN_FILE_VERSION` stays 1.

## 4. Invalid outputs count as misses (W4a, WA)

Today `_evaluate_cases` scores a partition only when every case produced valid output (`runtime/evaluation.py:144`, `:228-290`), so one invalid run turns a Stage 2 measurement or a Stage 4 exam into 0.0 and the held-out score is a validity gate. `count_as_miss` turns it into an accuracy with a hard ceiling on invalid runs. The default `fail_partition` keeps today's behaviour exactly.

### 4.1 Run classes

Every executed case falls in exactly one class:

| Class | Cause | `fail_partition` (default) | `count_as_miss` |
|---|---|---|---|
| ok | the harness returned output that passed the output schema | scored | scored |
| invalid | the harness raised anything other than `BudgetExceeded` (code `runtime_error` or a sandbox or game code; an adapter failure, such as a reply cut inside its reasoning or a provider still busy after its retries, lands here as `runtime_error`), or its output failed the output schema | partition failure with today's code (`runtime_error`, `schema_error` or the sandbox code); not scored | a miss counted in `invalid`; no partition failure |
| over_budget | `BudgetExceeded` from the run: dollars, the per-run deadline (`runtime/__init__.py:541`), the call cap, the reservation check, the compile wallet | hard failure `budget_exceeded`; not scored | the same hard failure `budget_exceeded`, counted in `over_budget`; not scored |
| input error | the case's task failed the input schema | `schema_error` | `schema_error`, hard: the dataset is wrong, not the candidate |
| accounting error | no telemetry row for the run (`evaluation.py:265-267`) | `runtime_error` | `runtime_error`, hard |

WA splits `_SchemaFailure` into an input subclass and an output subclass so the input case stays a hard `schema_error`. Under `fail_partition` both still report `schema_error`, exactly as today.

### 4.2 Scoring

Under `count_as_miss`, after every case ran and no hard failure was recorded:

1. If `invalid > invalid_allowance(spec, n)`, fail with `invalid_fraction_exceeded`; the partition is not scored.
2. Otherwise score the valid subset with one call of the floor's scorer, `s = floor.score(valid_cases, valid_outputs)` (or the `scorer` hook that `measure_cases` passes), set `k = round(s * len(valid))`, and the score is `k / n`, where `n` counts every selected case.
3. `floor.passes(k / n)` or `floor_not_met`, as today.

The aggregate telemetry row keeps its shape, with `floor_score = k / n`. `measure_cases` (Forge's tuning measure) passes its scorer the valid subset under `count_as_miss`; its per-case details are unchanged (an invalid case keeps `passed=None` and its failure code). Forge tool results and `TUNING.md` are not changed by this phase; W3 owns them.

Under `fail_partition` nothing changes except that `Measurement.counts` is filled: `correct = round(score * n)` when a `held_out_tasks` partition was scored, else None.

Presets follow the same policy (review fix). Under `count_as_miss` a preset's self-check is advisory: when no attempt passes it, the execution returns its last schema-valid candidate, which is then scored like any output, instead of raising `Candidate did not pass its optional self-check` into the invalid count. A Forge harness that checks its own answer can do the same, so a check-and-repair configuration is not capped by the invalid ceiling for catching its own mistakes. Under `fail_partition` the failed check still raises.

### 4.3 Stage 4 and the report

`verify()` keeps its order and adds:

- `held_out_counts` = the primary measurement's `counts`, on every report it writes;
- the report failure `primary_invalid_exceeded` when the primary measurement failed with `invalid_fraction_exceeded`. Today's rules still add `primary_floor_failed` and `primary_evaluation_failed` next to it.

`primary_budget_failed` stays the only budget code, and a budget refusal is never folded into `invalid`. The `tuning_evidence` rule is unchanged: a Stage 2 measurement with any failure code makes `tuning_failed`, and invalid runs within the allowance are not failure codes.

### 4.4 The readout

`CaseCounts.readout` appears in:

- the build result card, after the floor score: `0.93/0.85 on 100 held-out cases (correct 93/100 · invalid 1)`; the SYNTHETIC label rule is unchanged (WA, `agent/build.py`);
- the build result JSON, as `held_out_counts` (`{"cases", "correct", "invalid", "over_budget"}` or null) (WA);
- the plan card, when the spec's policy is `count_as_miss`, as one row: label `invalid output`, value `f"counts as a miss, at most {allowance} of {held_out} held-out runs"` (WA);
- bench rows and reports (`held_out_correct`, `held_out_invalid`, `held_out_over_budget`; WC);
- `/report`, which already dumps the report and so includes `held_out_counts` without a change;
- the desktop exam chart, which W6 draws from `report.json` after `compile()` returns.

## 5. Public dataset floors (W4b, WA)

### 5.1 The sidecar manifest

The manifest format is the one `scripts/fetch_bench_data.py` writes for `examples/receipts/manifests/<split>.json`. `architect/floors/provenance.py` reads these keys and ignores the rest:

```python
# architect/floors/provenance.py (new)
class ManifestCase(BaseModel):          # frozen, extra="ignore"
    case_id: Nonempty
    case_sha256: SHA256

class DatasetManifest(BaseModel):       # frozen, extra="ignore"
    manifest_version: Literal[1]
    dataset: Nonempty                   # "naver-clova-ix/cord-v2"
    dataset_revision: Nonempty          # "7f0115a4b758a71d6473b8d085751692da2fef98"
    license: Nonempty                   # an SPDX id: "CC-BY-4.0"
    rule_version: Nonempty              # "receipts-v2"
    count: NonnegativeInt
    cases: tuple[ManifestCase, ...]     # validator: len == count, case ids unique
    cases_sha256: SHA256 | None = None
    rules_sha256: SHA256 | None = None
    title: Nonempty | None = None       # display name; else KNOWN_DATASETS
    unit: Nonempty | None = None        # plural noun; else KNOWN_DATASETS, else "cases"

KNOWN_DATASETS: Mapping[str, tuple[str, str]]   # dataset id -> (title, unit)
#   {"naver-clova-ix/cord-v2": ("CORD v2", "receipts"), "PolyAI/banking77": ("Banking77", "messages")}
LICENSE_NAMES: Mapping[str, str]                # SPDX id -> display name, else the id itself
#   {"CC-BY-4.0": "CC BY 4.0", "MIT": "MIT", "Apache-2.0": "Apache 2.0", "CC0-1.0": "CC0 1.0"}

def case_sha256(case: EvaluationCase) -> str: ...
def cases_sha256(cases: Sequence[EvaluationCase]) -> str: ...
def load_manifest(path: Path) -> DatasetManifest: ...      # FloorError when unreadable or invalid
```

`case_sha256` is the sha256 hex of the UTF-8 canonical JSON (`sort_keys=True, separators=(",", ":"), ensure_ascii=False`) of `{"case_id", "task", "expected", "metadata": {}}`. The case's own metadata is ignored. This equals the data script's `case_sha256` (`scripts/fetch_bench_data.py:1155`) and, case by case, `architect.bench.text_tasks.cases_sha256`. `cases_sha256` hashes the list of those objects in file order. Both were checked against the committed data: all 200 evidence-floor cases match their manifest, and the evidence and delivery lists match their `cases_sha256`.

### 5.2 The check

```python
ProvenanceReason = Literal[
    "matched", "manifest_unreadable", "stamped_synthetic", "count_mismatch", "case_mismatch"
]

class FloorProvenance(BaseModel):       # frozen, extra="forbid"; labels and counts only
    origin: Literal["public_dataset", "synthetic"]
    reason: ProvenanceReason
    dataset: str | None
    dataset_revision: str | None
    license: str | None
    rule_version: str | None
    title: str | None
    unit: str
    cases: int
    matched: int

    def card_text(self, sep: str = " · ") -> str: ...

def check_floor(cases: Sequence[EvaluationCase], manifest: Path) -> FloorProvenance: ...
```

Rules, first match wins:

1. The manifest is unreadable, invalid or larger than 16 MiB: `synthetic`, `manifest_unreadable`.
2. Any case has `metadata["synthetic"] is True` (the session stamp): `synthetic`, `stamped_synthetic`.
3. `len(cases) != manifest.count`: `synthetic`, `count_mismatch`.
4. Any case whose `case_id` is not in the manifest or whose `case_sha256` differs from its entry: `synthetic`, `case_mismatch`, with `matched` counting the cases that do match.
5. Otherwise `public_dataset`, `matched`, `matched == cases`.

Only parsed content is compared. The byte hashes in a manifest's `files` map are not checked at run time.

### 5.3 Card text

- `public_dataset`: `f"{title}{sep}{license name}{sep}{cases} {unit}"`, which gives `CORD v2 · CC BY 4.0 · 200 receipts`, and with `sep=" | "` the ASCII-safe `CORD v2 | CC BY 4.0 | 200 receipts`.
- `synthetic`: `f"{cases} cases{sep}SYNTHETIC{sep}{reason text}"`, where the reason text is one of `the manifest could not be read`, `cases are stamped synthetic`, `the case count differs from the manifest`, or `f"{cases - matched} cases do not match the manifest"`.

### 5.4 Where provenance is used

- **Profiles.** `FloorConfig.manifest` names the sidecar. The floor itself loads and evaluates exactly as before; provenance labels it and never refuses it.
- **Build mode** (`agent/build.py:_floor`). For a configured floor with a manifest, `FloorPlan.origin` is `"public_dataset"` on a match and `"synthetic"` for every other reason, and the new `FloorPlan.provenance: FloorProvenance | None` holds the result. The plan card's `floor data` row is `f"{card_text} ({tuning} tuning / {held_out} held-out)"`. A floor without a manifest shows today's row.
- **Build results and export.** `floor_origin` in the result JSON is `"public_dataset"`; `_export_into_software` maps it to the routing label `"public_dataset"`, keeps `"synthetic"` as `"model_draft"` and everything else as `"user_dataset"`. `agent/export.py:_floor_origin` (used by `/export`) applies the same check.
- **ASCII terminals.** Plan-card rows carry the Unicode separator. A terminal renderer using the ASCII glyph set (`agent/render.py`, `ASCII`) replaces `" · "` with its `sep` (`" | "`) in plan-card values; Unicode renderers and the desktop print rows unchanged.
- **Bench.** The receipts loader (10.3) refuses any mismatch with `bench_dataset_mismatch` instead of labelling it, because its rows claim the pre-registered dataset.
- The step-by-step `architect session` is unchanged in this phase.

A profile entry for the hero floor:

```json
"floors": {
  "cord-receipts-evidence": {
    "kind": "held_out_tasks",
    "path": "examples/receipts/floors/evidence.json",
    "manifest": "examples/receipts/manifests/evidence.json",
    "checker": "receipt_v1"
  }
}
```

## 6. The receipt_v1 checker (WA, mirrored by WC)

```python
# architect/floors/checkers.py
CheckerName = Literal["equality", "exam_extraction_v1", "invoice_extraction_v1", "receipt_v1"]
RECEIPT_FIELDS: tuple[str, ...] = ("total", "subtotal", "tax", "item_count")

def receipt_v1(expected: JsonValue, output: JsonValue) -> bool: ...
```

Semantics, following `examples/receipts/RULES.md` (rule version `receipts-v2`; the four fields and exact match are the same under `receipts-v1`):

- `expected` must be an object holding the four keys, with `total`, `subtotal` and `tax` each an int of at least 0 (never a bool) or null, and `item_count` an int of at least 0 (never a bool or null). Anything else raises `FloorError("Checker expected value is malformed")`, as the module's other checkers do.
- A field matches when the output is an object holding the key and either the expected value is null and the output value is null, or the expected value is an int and the output value is an int (not a bool and not a float) equal to it. `75000.0` therefore never matches `75000`, even though JSON Schema accepts it as an integer.
- `receipt_v1` passes exactly when all four fields match. Keys beyond the four are ignored here; the output schema in `examples/receipts/task.json` refuses them before any checker runs.
- `field_report(expected, output, checker="receipt_v1")` reports `per_field` for the four names, each `(matched, 1)`, with `fields == 4`. `receipt_v1` is True exactly when `matched == 4`.

Field accuracy, the secondary measure, is matched fields over fields across a set of runs, with an invalid run counting 0 of 4. Floors pick the checker through `FloorConfig.checker`; the bench task names it in its `BenchTask` (10.4) and the runner's `_check` and `field_report` paths need no change.

## 7. Frozen specs (W4d, WA)

### 7.1 Entry points

- **`/make @<path>` and `/forge @<path>`** in the agent CLI, and so in the desktop, whose bridge runs the same `Repl`. The argument is exactly `@` followed by one path, relative to the working directory or absolute. `/make <description>`, `/forge <description>` and `/forge arc` are unchanged.
- **The `make` tool** gains an optional string argument `spec_file`. The `/make @<path>` handler calls the tool directly (`repl.call_tool("make", {"description": "", "spec_file": path})`), never through the conductor and never through `@` mention expansion. `/forge @<path>` does the same with Forge forced on.
- **Print mode.** `architect -p "/make @<path>"` (and `/forge @<path>`) runs the same frozen build. The plan card goes to stderr. Unless `--permission full-auto` is given, the plan question is answered no, so the hash is printed and nothing compiles.

```python
# architect/agent/build.py
async def make(description: str, ctx: ToolContext, *, spec_file: str | None = None) -> JsonValue: ...
```

### 7.2 What a frozen build does

1. It reads the file (UTF-8, at most 1 MiB) with `Spec.model_validate_json`. Any read or validation failure is the existing code `invalid_spec_file`.
2. It skips the interview, Stage 0, `_shape` and `_lift_budget`: the spec is compiled exactly as read. `/floor`, `/budget` and `/model` overrides are not applied; when any is set, the plan card adds the note `frozen spec: /floor, /budget and /model changes are not applied`. The engineer choice (Forge or preset only) still applies, because it is compile configuration and not part of the spec.
3. The floor is the profile floor named `spec.floor.dataset` with kind `spec.floor.kind`. Nothing is drafted or copied. When it is missing, the build stops with the existing code `floor_not_configured` and the next step `configure the floor this spec names in the profile`. Provenance follows Section 5.
4. The plan card shows today's rows plus a `spec` row right after `task`, with the value `f"{spec.spec_hash} (frozen, {file name})"`: the full 64-character hash, so it can be compared with the pre-registration. The footer is today's.
5. The install name is the file name without `.json` and without a trailing `.spec`, made kebab-case by the `agent_name` rules, or `agent` when nothing is left. The destination is "run it here"; `/export` places the agent into software afterwards.
6. Approval and everything after it are unchanged. The result JSON adds `"spec_hash"` (all 64 characters) and `"spec_file"` (the file name only, never a directory).

## 8. The Forge audit (W4e)

### 8.1 Turning it on

- `architect bench run --audit DIR` (WC, Section 10).
- `--audit DIR` on `compile`, `create`, `do`, `recompile`, `forge run` and `forge integrate` (WA, added in `_forge_flags` and `_forge_mode_flags`).
- The environment variable `ARCHITECT_FORGE_AUDIT_DIR`, honoured by build mode (agent CLI and desktop) and by the subcommands above when `--audit` is absent. `architect serve` does not audit in this phase.
- There is no `ForgeSettings` field for it: the Forge pack hash (PLAN Section 4) must not depend on where audits go.

### 8.2 API

```python
# architect/compiler/forge_audit.py (new, WA)
AUDIT_VERSION = 1
AUDIT_ENV = "ARCHITECT_FORGE_AUDIT_DIR"

class AuditError(OSError): ...

class ForgeAudit:
    directory: Path                       # resolved
    def __init__(self, directory: Path) -> None: ...
        # creates the directory with parents and writes then removes a probe file;
        # AuditError when either fails (the caller reports audit_unwritable before any spend)
    def open_session(
        self, *, spec_hash: str, model: str, agent_model: str | None,
        mode: Literal["agent", "template"], depth: int,
    ) -> AuditSession: ...

class AuditSession:
    session_id: str                       # uuid4 hex
    def record(
        self, *, number: int, source: str, candidate: Path | None, verdict: str,
        measurement: Measurement | None, smoke_passed: bool | None, floor_limit: float,
    ) -> None: ...                        # AuditError on any write failure
    def close(self, *, promoted: int | None, failures: Sequence[str]) -> None: ...

@contextmanager
def forge_audit(directory: Path | None) -> Iterator[ForgeAudit | None]: ...
def current_audit() -> ForgeAudit | None: ...
def audit_directory(flag: Path | None) -> Path | None: ...    # the flag, else the env, else None
```

`forge_audit` sets a `ContextVar`. It is never a process-wide observer: compiles in other tasks are unaffected, and a `forge_spawn` child compile inherits the audit of its parent's task.

### 8.3 Hooks (WA's only edits to Forge files)

- **`forge.py:forge()`.** When `current_audit()` is set, it opens an `AuditSession` before any tool can run. It adds the audit directory to the Pi session's `denied_paths` (`forge.py:215`) and to the `_outside` checks (`forge.py:378-380`), so Forge's workspace and private store are never inside it and the agent's file tools cannot reach it. It closes the session on every exit path, with `promoted = session.best_number`.
- **`forge_tools.py:submit_source`.** It records exactly once per call, after its verdict: `"accepted"`, or the code it returns (`forge_invalid_source`, `forge_literal_leak`, `forge_unbound_child`, `forge_unused_child`, `persistence_failed`, `tuning_failed`, `smoke_failed`, `not_better`, `forge_submissions_exhausted`), with the tuning measurement when one was taken. `ForgeSession` gains `audit: AuditSession | None = None` to carry it. Template mode goes through `submit_source` and is audited the same way.
- **Failure.** An `AuditError` during a session ends it. `forge()` then promotes nothing from that session and returns the Stage 2 candidate with the failure `forge_audit_failed`, and Stage 4 runs on that baseline as it does when Forge does not improve on it. A build that cannot write its audit never ships a Forge harness.

### 8.4 Files

```
<DIR>/<spec_hash>/<session_id>/session.json
<DIR>/<spec_hash>/<session_id>/<n>/harness.py          the submitted source, exact bytes
<DIR>/<spec_hash>/<session_id>/<n>/spec.json           \
<DIR>/<spec_hash>/<session_id>/<n>/config.json          > copied when the candidate was written
<DIR>/<spec_hash>/<session_id>/<n>/subharnesses/       /  (subharnesses only when it has children; each child without its report.json)
<DIR>/<spec_hash>/<session_id>/<n>/submission.json
```

JSON files are written with `sort_keys=True, indent=2, ensure_ascii=True` and LF line ends.

`submission.json` (`AuditSubmission`, frozen, `extra="forbid"`):

| Key | Type | Meaning |
|---|---|---|
| `audit_version` | `1` | |
| `spec_hash` | sha256 hex | the build's spec |
| `session_id` | str | uuid4 hex |
| `submission` | int >= 1 | `submit_source`'s number |
| `depth` | int | 0 for the parent, 1 for a spawned child |
| `mode` | `"agent"` or `"template"` | |
| `model` | str | the play model (profile key) |
| `agent_model` | str or null | the Forge agent (profile key) |
| `code_sha256` | sha256 hex | of `harness.py` |
| `artifact_digest` | sha256 hex or null | null when the candidate was never written |
| `verdict` | str | `"accepted"` or one of the codes in 8.3 |
| `floor_limit` | float | the bar at build time |
| `tuning` | object or null | `{"score", "floor_passed", "budget_passed", "mean_cost_usd", "max_cost_usd", "max_compute_s", "sample_count", "counts", "failures", "cases_sha256"}` from the tuning `Measurement`; null when none was taken. `cases_sha256` (review fix) is `floors.provenance.cases_sha256` of the practice cases the measurement ran on, in order: tuning-side only, and null in older records |
| `smoke_passed` | bool or null | |
| `recorded_at` | str | ISO 8601 UTC |

`session.json` (`AuditSessionRecord`): `audit_version`, `spec_hash`, `session_id`, `depth`, `mode`, `model`, `agent_model`, `started_at`, `ended_at` (null until `close`), `submissions`, `promoted` (Forge's chosen submission number by its tuning rank, or null) and `failures`.

Nothing in the audit comes from the held-out partition, from Stage 4 or from a report. A bound child is a licensed artifact with its own signed Stage 4 report; the copy under `subharnesses/` leaves that `report.json` out at every depth (review fix), and the replay in 13.2 never replays a submission that binds children. Forge never reads the directory, and no compile reads it either; only the replay in 13.2 does, after the fact.

## 9. Fair arms (W5, WB)

These remove confounds 1 and 2 of `report-evidence.md` Section 3 and pin the plain arm. Every behaviour below is selected by a spec field from Section 3.1, so specs that leave them at their defaults behave exactly as today.

### 9.1 Input limit

`_task_context` (`presets/_execution.py:236`) uses `spec.input_limit_chars` as the character allowance when it is set, and today's rule otherwise (the `context_window` param, else 2,048). `pack_description` uses the same allowance for its context policy. When the field is set, `tune()` pins `context_window` at its default so Stage 2 never measures two candidates that behave the same.

### 9.2 Reply parsing

```python
# architect/presets/_json.py (new)
def parse_reply(text: str, mode: ReplyJson) -> JsonValue: ...
    # raises PresetError("Model response is not valid JSON")
def extract_last_json_text(text: str) -> object | None: ...
```

For a schema whose type is not `"string"`, `_parse` (`presets/_execution.py:109-118`) calls `parse_reply`. Every mode first tries today's strict parse of the whole reply; only when that raises does the mode apply:

- `strict`: the error stands, as today.
- `first`: `architect.runtime._sandbox_builtins.extract_json_text(text)` with no meter, which is the function behind Forge's `extract_json`. It takes the first closed code fence whose body is (or holds) an object or array, else the first balanced `{...}` or `[...]` in the text that parses, trying at most 16 starts.
- `last`: `extract_last_json_text`. It scans left to right; at each `{` or `[` it finds the balanced end (`_sandbox_builtins._balanced_end`, string-aware). A slice that `parse_json_text` turns into an object or array is remembered and the scan continues after it; otherwise the scan moves one character on. At most 256 starts are tried and a text over 128 KiB gives None. The last remembered value wins. Fences need no special case.
- `schema`: as `first`, and every call whose reply is parsed against a schema passes that schema as `response_schema` to `RunContext.call`, so the provider is asked for `json_schema` output. W1's adapter keeps thinking off on schema calls. Inside the sandbox (review fix) a bound preset's call carries its schema to the host as the call message's `schema` key, and the host forwards it as `response_schema` only for an artifact whose spec has `reply_json` `schema` and only as a JSON object; otherwise the key is refused. Harness code's own `context.call` takes no schema.

A None result raises the `PresetError` above. A parsed value then goes through today's checks (finite numbers, JSON values, the schema). String-typed schemas return the reply text unchanged in every mode. The same mode applies to every parse in one preset execution, including the self-check and the repair retries.

### 9.3 The plain arm

`spec.plain = True` pins Stage 2 to one configuration:

- `tuning.py:tune()` measures exactly one candidate: the preset's defaults with `verifier_depth = 0` and `retries = 0` wherever those tunables exist, whatever `max_candidates` says and with no warm start. `_effective_components` then drops the check components, so the plain presets make one model call per run.
- `selection.py`: with `plain`, Stage 1 ignores library warm starts and takes `preset_hint`.

`plain` does not switch Stage 3 off. A plain bench arm runs as variant `default`, where rewriting and Forge are already off (`runner.py:_compile_config`), and `--plain` refuses other variants (10.5). The arm definitions in the pre-registration state both facts.

### 9.4 Model aliases and the output cap

- `api_model` is sent as `"model"` on every request (`complete` and `stream_chat`), is the price-table key, and is what the reply's model is checked against (together with `allowed_response_models`). `adapter.model` stays the profile key, so the spec's `model_target`, artifacts, the candidate model check (`evaluation.py:205-206`), telemetry, routing and bench rows all carry the alias.
- `max_tokens` is a ceiling: each call's `max_tokens` and its reservation are `min(request cap or the price entry's capacity, max_tokens)`.
- `model_unpriced` looks the price up by `api_model` when it is set.
- W1's Nebius behaviour (dialect, thinking switch, retries, reasoning-cut guard) is unchanged.

Example entries for the evidence profile (base URL and key reference elided):

```json
"models": {
  "ultra":           {"provider": "openai", "api_key_ref": "nebius", "api_model": "nvidia/Nemotron-3-Ultra-550b-a55b", "max_tokens": 512},
  "super":           {"provider": "openai", "api_key_ref": "nebius", "api_model": "nvidia/nemotron-3-super-120b-a12b", "max_tokens": 512},
  "lightning":       {"provider": "openai", "api_key_ref": "nebius", "api_model": "nvidia/Nemotron-3_5-Lightning", "max_tokens": 512},
  "lightning-think": {"provider": "openai", "api_key_ref": "nebius", "api_model": "nvidia/Nemotron-3_5-Lightning", "think": true, "max_tokens": 4096}
}
```

Without the 512 cap, Ultra reserves its full 32,768-token capacity per call (about $0.10), which is above the pinned per-run budget in 10.2, so every Ultra run would be refused before spend as `over_budget`.

### 9.5 The model id rule

```python
# architect/models/types.py
MODEL_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*(/[A-Za-z0-9][A-Za-z0-9._:-]*)*$"

# architect/bench/rows.py
ModelId = Annotated[str, Field(min_length=1, max_length=128, pattern=MODEL_ID_PATTERN)]
```

`bench/providers.py:101` (`_MODEL_ID`) applies the same pattern and length. `nvidia/Nemotron-3-Ultra-550b-a55b` and `granite4.2:8b` are valid; `/x`, `x/`, `a//b` and anything over 128 characters are not. Model ids never become path components in the bench.

## 10. Bench suite v2 and the receipts task (W5, WC)

### 10.1 Suites and tiers

```python
# architect/bench/suite.py
SUITES: Mapping[str, tuple[BenchTask, ...]]          # {"v1": _V1, "v2": _V2}, _V2 = (receipts,)
SUITE_TIERS: Mapping[str, Mapping[Tier, tuple[TaskId, ...]]]
    # {"v1": today's TIERS, unchanged; "v2": {"public": ("receipts",)}}
TIERS = SUITE_TIERS["v1"]                            # kept for existing importers
def tiers_for(version: str) -> Mapping[Tier, tuple[TaskId, ...]]: ...   # bench_suite_unknown
```

`suite_digest("v1")` builds exactly today's payload and stays `1f23e5e421b2c2c3d79482394a841764b37f4e70d3b06a73993fc7bb4bf335ed`. `suite_digest("v2")` hashes `{"version": "v2", "tasks": [v2 payload of receipts], "tiers": {"public": ["receipts"]}, "public": {"receipts": {"settings_sha256", "dataset", "dataset_revision", "rule_version", "rules_sha256", "splits": {split: cases_sha256}}}}`, where the v2 task payload is the v1 payload plus `floor_origin`. Any change to the settings file or the data changes the v2 digest, so rows across such a change never pair. `cli.py:811` and `render.list_table` use `tiers_for(suite)` instead of the global table.

`BenchTask` gains `floor_origin: FloorOrigin = "synthetic_generated"`; `sentiment-24` sets `"example_fixture"`. The runner writes `floor_origin=task.floor_origin`, replacing the hard-coded rule at `runner.py:2887`. The v1 digest payload does not include the new field.

### 10.2 The settings file

`examples/bench/suite-v2/receipts.json`, written by WC now and rewritten by the bar script when the pilot freezes the bar (PLAN, 10/9):

```python
# architect/bench/public_tasks.py
class PublicSettings(BaseModel):      # frozen, extra="forbid"
    settings_version: Literal[1]
    task_id: Literal["receipts"]
    source: Literal["examples/receipts"]           # relative to the repository root
    floor_limit: Annotated[float, Field(ge=0, le=1)]   # the bar
    run_budget: Budget | None                      # None: lifted from quotes, as v1 does
    invalid_policy: InvalidPolicy
    max_invalid_fraction: Annotated[float, Field(ge=0, le=0.1)]
    input_limit_chars: int | None
    reply_json: ReplyJson
    frozen: bool
```

First version: `floor_limit` 0.80 (a placeholder), `run_budget` `{"cost_usd": 0.02, "compute_s": 60.0}`, `invalid_policy` `"count_as_miss"`, `max_invalid_fraction` 0.02, `input_limit_chars` 8192, `reply_json` `"first"`, `frozen` false. `settings_sha256` is the sha256 of the canonical JSON of the parsed settings. The run budget assumes the 512-token cap of 9.4.

### 10.3 Loading and checking the data

```python
# architect/bench/public_tasks.py
PublicSplit = Literal["pilot", "rehearsal", "evidence", "delivery"]

class PublicData(BaseModel):          # frozen
    settings: PublicSettings
    task: dict[str, JsonValue]        # task.json
    splits: dict[PublicSplit, tuple[EvaluationCase, ...]]
    manifests: dict[PublicSplit, DatasetManifest]

def load_public(task_id: str = "receipts") -> PublicData: ...   # cached per process
```

`load_public` reads `cases/<split>.jsonl` for the four splits and refuses, before any spend, with `bench_dataset_missing` when a file is absent and `bench_dataset_mismatch` when any check fails:

1. `task.json`: the sha256 of `rules` equals `rules_sha256`, and `dataset`, `dataset_revision` and `license` (and `rule_version`, `rules_sha256`) equal every manifest's.
2. Each split: the case count, every `case_sha256` by `case_id`, and the list's `cases_sha256` in file order equal the manifest (`floors/provenance.py` functions).
3. Every task validates against `task.json`'s input schema and every expected value against its output schema.
4. No `case_id` and no `case_sha256` appears in two splits.
5. The rehearsal and evidence manifests' `partition` counts equal the sorted even/odd rule (50/50 and 100/100).

The manifests define the counts. Data v2 (rule version `receipts-v2`, merged after this contract) has pilot 150, rehearsal 100, evidence 200 and delivery 348 (166 train, 86 validation, 96 test), with 202 source receipts excluded (`manifests/excluded.json`). The plan's 500 delivery receipts became 354 after the first deduplication and 348 in v2; the pre-registration uses the real counts. The code reads every count from the manifests; one test (`tests/test_bench_public.py`) pins them and moves with each data release.

### 10.4 The receipts task

`BenchTask(id="receipts", kind="text", tiers={"public"}, sizes={"public": 200}, floor_limit=<settings>, checker="receipt_v1", preset="extractor", discriminates="exact four-field extraction from real receipt text", requires=frozenset(), floor_origin="public_dataset")`.

Its cell Spec:

| Field | Value |
|---|---|
| `task` | `task.json["rules"]`, verbatim, so the spec and the plain prompt quote the rules word for word |
| `input_schema`, `output_schema` | from `task.json` |
| `floor` | `held_out_tasks`, `limit=floor_limit`, `dataset=f"bench-receipts-{suite}-{floor_split}"`, `minimum_samples` = the held-out count |
| `budget` | `run_budget` when set, else lifted as v1 lifts it |
| `model_target` | the cell's model (profile key) |
| `preset_hint` | `"extractor"` |
| `invalid_policy`, `max_invalid_fraction`, `input_limit_chars` | from the settings |
| `reply_json` | `BenchConfig.reply_json` when set, else the settings' |
| `plain` | `BenchConfig.plain` |

Data: the floor is `splits[floor_split]` and the cell size is its count (`--size` is refused with `bench_size_unsupported`). The replay set is `splits[delivery_split]`, all of it for `--delivery all` or its first N cases in manifest order, with case ids unchanged. The oracle tables come from every loaded case, so the fake provider answers correctly and `noisy` corrupts receipts by adding 1 to `item_count` (a new rule in `oracle._corrupt` for objects holding an int `item_count`, checked before the `total` rule). The seed does not change the data.

Sizing (confound 5): `sizing_request` for receipts uses `len(spec.task) + len(output schema JSON) + 2 * min(largest input, allowance) + 1024` characters, where the largest input is the longest canonical JSON task among the cell's floor and replay cases and the allowance is `input_limit_chars` or 2,048. Calls per run are 1 for a plain cell and 4 otherwise. v1 sizing is unchanged. When the settings pin `run_budget`, a cell whose sized calls (the per-call quote times the calls per run) cost more than the pinned dollars is refused before any spend with `bench_run_budget_too_small` (review fix): every run would otherwise be refused at its first reservation and the arm zeroed.

### 10.5 Bench run flags

| Flag | Field | Default | Refusal |
|---|---|---|---|
| `--tasks a,b` | `tasks` | the whole tier | an id not in the suite: `bench_task_unknown`; nothing left: `bench_no_cells` |
| `--no-tuning-replay` | `tuning_replay=False` | replay on | |
| `--plain` | `plain` | off | a variant other than `default` or `noisy`: `bench_plain_variant` |
| `--reply-json strict\|first\|last\|schema` | `reply_json` | the task's (v1 tasks: `strict`) | |
| `--floor-split evidence\|rehearsal` | `floor_split` | `evidence` | non-default with no public task: `bench_split_unsupported` |
| `--delivery-split delivery\|pilot` | `delivery_split` | `delivery` | non-default with no public task: `bench_split_unsupported` |
| `--delivery N\|all` | `delivery_n` | 0 | `all` with no public task: `bench_delivery_all_unsupported`; N above the split: `bench_delivery_too_large` |
| `--audit DIR` | `audit` | none | unwritable: `audit_unwritable` before any spend |

With `--plain`, the cost bound counts one candidate per cell. With `--audit`, each cell's compile runs inside `forge_audit(config.audit)`. The pilot of PLAN Section 4 is, for example, `bench run --suite v2 --tier public --floor-split rehearsal --delivery-split pilot --delivery all --plain --models ultra,super --label A1-pilot ...`; the exact commands belong to the pre-registration.

## 11. Per-run delivery metrics (WC)

The runner's `_replay` returns, for each case, the output (or None), an outcome class, and the case's cost, calls and seconds:

- **Outcome.** `"ok"` when the run returned output that passed the output schema; `"over_budget"` when it raised `BudgetExceeded`; `"invalid"` for anything else (an input-schema failure included, which public data rules out at load).
- **Cost and calls.** The `CallMeter` difference across the case (the cell's adapters are already instrumented), so reasoning tokens are included. A call that raises still counts when it was billed (review fix, `providers.failed_call_usage`): a reply the adapter refuses after the provider reported its usage, such as a reply cut inside its reasoning, counts at that usage, which the Nebius adapter attaches to the error as `AdapterError.usage`; a call cancelled in flight, as at the run deadline, returns no usage and counts the dollars of its reservation (the adapter's quote, the figure `RunContext` keeps for it) and no tokens. Any other failure, such as an HTTP error after the retries, counts nothing.
- **Seconds.** The `ctx.clock` difference around `runtime.evaluate`, so a sandboxed harness's process start is included.

The delivery fields (Section 3.6), over the `n` replayed cases in manifest order:

| Field | Value |
|---|---|
| `delivery_pass_bits` | `encode_pass_bits`; an invalid or over-budget run is 0 |
| `delivery_score` (existing) | passing runs / n |
| `delivery_invalid`, `delivery_over_budget` | counts of those outcomes |
| `delivery_invalid_bits`, `delivery_over_budget_bits` | review fix: which runs ended that way, encoded like the pass bits, so a subset of the cases can be recounted. A row validator checks that each matches its count, that the two never mark the same run and that neither marks a passing run. Rows written before the fix hold only the counts and leave these null |
| `delivery_cost_usd` | total measured dollars over the n runs |
| `delivery_calls` | total model calls |
| `delivery_zero_call_runs` | runs that returned a schema-valid output with 0 model calls; an invalid or over-budget run never counts, since it settled nothing (review fix) |
| `delivery_p50_s`, `delivery_p95_s` | nearest rank over every run's seconds: the sorted value at index `ceil(p * n) - 1` |
| `delivery_run_costs_usd` | each run's dollars, in case order (the paired bootstrap needs them) |
| `delivery_field_accuracy` | matched fields / fields over every run (invalid runs count 0 of the checker's fields); null for `equality` tasks |
| `delivery_cases_sha256` | `cases_sha256` of exactly the replayed list |
| `delivery_split` | the replay split name, for public tasks |

Every delivery field stays null when nothing was replayed. The other new row fields:

- `compile_cost_usd`: the meter's dollars when `compiler.compile` returns, before any replay. It excludes `forge.agent_cost_usd`; build cost is the sum of the two.
- `held_out_correct`, `held_out_invalid`, `held_out_over_budget`: from `report.held_out_counts`. `held_out_mean_cost_usd`: `report.mean_cost_usd`.
- `floor_cases_sha256`, `floor_split`, `api_model`, `plain`, `reply_json`, `invalid_policy`: from the cell. `suite_frozen`: the settings' `frozen` for public tasks, null for v1.
- With `--no-tuning-replay`: `tuning_pass_bits`, `tuning_score` and `field_accuracy` are null and `tuning_replay` is false. The legacy compare treats such a row as having no tuning bits (`compare._bits` returns None), never as all failed.

## 12. Paired comparison across arms (WC)

### 12.1 Command

```
architect bench compare SOURCE [SOURCE ...] --pair-arms BASE,CAND [--pair-arms BASE,CAND ...]
    [--margin 0.05] [--alpha 0.05] [--bootstrap 10000] [--seed 0]
    [--allow-fingerprint-mismatch] [--json] [--out DIR]
```

Without `--pair-arms`, `compare` takes exactly two sources and behaves as today. With it, any number of sources (rows files, run directories or bench ids) are pooled. `--pair-arms` cannot be combined with `--pair-variants` or `--expect` (`invalid_arguments`). `--bootstrap` accepts 1,000 to 100,000.

An arm selector is either `MODEL:VARIANT`, split at the last `:` (the variant must be a bench variant, else `bench_variant_unknown`), or a run `LABEL` (labels cannot contain `:`). `MODEL:VARIANT` matches rows with that model and variant (for routed rows only the exact probe of round 0, as `_paired` does today); `LABEL` matches rows with that `label`. Pre-registered arms carry labels (`--label A3`), so `--pair-arms A3,A7` is the usual form.

For every `--pair-arms` value and every `(task_id, repeat)` present on both sides, exactly one base row and one candidate row must match. The pair is refused with `bench_compare_mismatch` when any of these fails: the same `suite_version` and `suite_digest` across all rows used; both rows carry `delivery_pass_bits`; equal non-null `delivery_cases_sha256`; equal `delivery_cases`. `preset_fingerprint` must match unless `--allow-fingerprint-mismatch` is given (`bench_compare_fingerprint`). `spec_hash` may differ, since arms on different models always do.

### 12.2 Statistics

With `n11` (both pass), `n10` (base passes, candidate fails), `n01` (base fails, candidate passes) and `n00` (both fail) over the `n` delivery cases:

- **Accuracies.** `base_k = n11 + n10`, `cand_k = n11 + n01`, with Wilson 95% intervals (`compare.wilson`, z = 1.96).
- **Difference.** `diff = (cand_k - base_k) / n`.
- **Exact McNemar.** `mcnemar_p = mcnemar_exact(n10, n01)`, two-sided.
- **Holm.** Across every `(pair, task, repeat)` cell of one invocation: sort the p-values ascending (ties by input order); the adjusted value of rank i (from 0) is the running maximum of `min(1, (m - i) * p)`. `better` is `holm_p < alpha and n01 > n10`; `worse` is `holm_p < alpha and n10 > n01`.
- **Paired Newcombe interval** (method 10, hybrid score, no continuity correction) for `diff`, with `pc = cand_k / n`, `pb = base_k / n`, Wilson limits `(lc, uc)` and `(lb, ub)` at z = 1.96, and `phi = (n11*n00 - n10*n01) / sqrt((n11+n01)*(n10+n00)*(n11+n10)*(n01+n00))`, or 0 when that product is 0:
  - `lower = diff - sqrt((pc - lc)**2 - 2*phi*(pc - lc)*(ub - pb) + (ub - pb)**2)`
  - `upper = diff + sqrt((pb - lb)**2 - 2*phi*(pb - lb)*(uc - pc) + (uc - pc)**2)`
  - each square root of `max(0, ...)`, and the interval clamped to [-1, 1].
- **Non-inferiority.** `noninferior = lower > -margin` (strictly), the H2 rule.
- **Cost ratio.** `cost_ratio = sum(cand costs) / sum(base costs)` over the paired runs (`delivery_run_costs_usd`). Its 95% interval is a paired percentile bootstrap: one fresh `random.Random(seed)` per cell; each of the B resamples draws `n` indexes with `rng.randrange(n)`, sums both vectors in draw order and takes the ratio (`inf` when the base sum is 0 and the candidate's is not, 1.0 when both are 0); after sorting, `lower = r[floor(0.025 * B)]` and `upper = r[ceil(0.975 * B) - 1]`. H3 holds when `upper <= 0.25`. All cost fields are null when either row lacks the vector.

Reference vectors, which the tests pin:

| n11, n10, n01, n00 | diff | Newcombe 95% | McNemar p |
|---|---|---|---|
| 300, 20, 45, 135 | 0.05 | [0.018680047579, 0.081241471210] | 0.0026263768106840383 |
| 80, 5, 12, 3 | 0.07 | [-0.011602415443, 0.154723316867] | 0.143463134765625 |
| 50, 0, 0, 50 | 0.0 | [0.0, 0.0] | 1.0 |

Holm: `[0.01, 0.04]` gives `[0.02, 0.04]`; `[0.03, 0.02, 0.5]` gives `[0.06, 0.06, 0.5]`.

### 12.3 Output

```python
# architect/bench/compare.py
class ArmPair(BaseModel):       # frozen, extra="forbid", allow_inf_nan=False, ser_json_inf_nan="strings"
    base: str                   # the selector as given
    cand: str
    task_id: TaskId
    repeat: int
    n: int
    base_k: int
    cand_k: int
    base_invalid: int | None        # review fix: None when the row cannot tell (13.3)
    cand_invalid: int | None
    base_over_budget: int | None
    cand_over_budget: int | None
    n11: int
    n10: int
    n01: int
    n00: int
    diff: float
    diff_ci: tuple[float, float]
    wilson_base: tuple[float, float]
    wilson_cand: tuple[float, float]
    mcnemar_p: float
    holm_p: float
    better: bool
    worse: bool
    margin: float
    noninferior: bool
    base_cost_per_run: float | None
    cand_cost_per_run: float | None
    cost_ratio: float | None
    cost_ratio_ci: tuple[float, float] | None

class PairedComparison(BaseModel):   # same config
    suite_version: SuiteVersion
    suite_digest: str
    alpha: float
    margin: float
    bootstrap: int
    seed: int
    pairs: list[ArmPair]
    evidence: tuple[Evidence, ...]
    floor_origins: tuple[str, ...]

def newcombe_paired(n11: int, n10: int, n01: int, n00: int, z: float = 1.96) -> tuple[float, float]: ...
def holm(p_values: Sequence[float]) -> list[float]: ...
def bootstrap_ratio(base: Sequence[float], cand: Sequence[float], *, resamples: int, seed: int) -> tuple[float, float]: ...
def pair_arms(
    rows: Sequence[BenchRow], hypotheses: Sequence[tuple[str, str]], *, alpha: float = 0.05,
    margin: float = 0.05, bootstrap: int = 10_000, seed: int = 0,
    allow_fingerprint_mismatch: bool = False,
) -> PairedComparison: ...
```

`--json` prints `PairedComparison.model_dump_json()`; otherwise `render.pairs_table` prints an ASCII table of at most 100 columns.

## 13. Reports, the audit replay and the analysis skeleton (WC)

### 13.1 `bench report`

```
architect bench report SOURCE [SOURCE ...] [--md] [--pair-arms BASE,CAND ...]
    [--margin 0.05] [--alpha 0.05] [--bootstrap 10000] [--seed 0] [--footer FILE] [--out DIR]
```

Sources must share one `suite_version` and `suite_digest` (`bench_compare_mismatch`). Without `--md` the output is an ASCII table; with it, Markdown. One table per task, one line per row, sorted by arm then repeat. Arm is the label, else `MODEL:VARIANT`.

| Column | Value |
|---|---|
| Arm | label or `model:variant` |
| Model | `api_model`, else `model` |
| Delivery | `f"{k}/{n} ({k/n:.3f})"` |
| 95% CI | Wilson, `f"[{lo:.3f}, {hi:.3f}]"` |
| Invalid | `delivery_invalid` |
| Over budget | `delivery_over_budget` |
| Exam | `f"{held_out_correct}/{held_out_count}"`, else the held-out score `f"{score:.3f}"` |
| Licensed | `yes` or `no` (Stage 4 on the floor) |
| $/run | `f"${delivery_cost_usd / n:.6f}"` |
| p50 s, p95 s | `f"{value:.2f}"` |
| Calls/run | `f"{delivery_calls / n:.2f}"` |
| Build $ | `f"${compile_cost_usd + agent_cost_usd:.4f}"` |
| Tuning, exam, delivery | `f"{tuning_score:.3f} / {held_out_score:.3f} / {delivery_score:.3f}"`, `-` for a missing value |

With `--pair-arms`, a second table follows with one line per pair: Base, Candidate, n, Diff, 95% CI, McNemar p, Holm p, Non-inferior (margin), Cost ratio, 95% CI. Proportions and differences print with 3 decimals and p-values with 4 significant digits.

The footer is the content of `--footer FILE`, printed verbatim (the pre-registration commits it, for example `docs/bench/footer-nebius.md`, written by the release workstream). Without `--footer`, the report prints these lines, each only when it applies:

- `Suite {version} {digest[:8]}; settings frozen: {yes|no}.`
- `Evidence: {labels}. Floor origin: {origins}.`
- `Delivery cases {sha[:8]}, n = {n}.`
- `These rows were measured before the settings were frozen: they are rehearsal numbers.`
- `Fake provider rows: plumbing checks, not model quality.`
- `Synthetic floor: not evidence of quality.`

The report has no timestamps and is byte-identical for identical input.

### 13.2 `bench audit`

```
architect bench audit AUDIT_DIR --profile P [--since ISO] [--until ISO] [--all]
    [--max-usd X] [--yes] [--out DIR] [--json]
```

It replays audited submissions on their exam so the gate audit can be counted (PLAN Section 0: met the bar in practice, failed the exam):

1. Select submissions whose `recorded_at` lies within `--since`/`--until`. By default only those that met the bar in practice: a tuning measurement with no failure code and a score of at least `floor_limit`. `--all` takes every submission with a tuning measurement.
2. For each, copy the submission directory into a fresh throwaway workspace with its own signing key (the bench's `_Workspace`), load it as a `HarnessArtifact`, build the profile floor named by its `spec.floor.dataset` (it must equal `spec.floor`, otherwise the row's `exam` is `"error"`), and call `architect.compiler.verification.verify()` once. Read the counts from the report it writes, then delete the workspace. The audit directory is never written. A profile floor whose practice cases are not the ones the submission was measured on (review fix: the recorded `tuning.cases_sha256`, or for an older record `tuning.sample_count`) is never replayed or bounded, and its row's `exam` is `"error"`.
3. Consent and money work like `bench run`: the bound is the per-run budget times the held-out cases of every selected submission, `Continue? [y/N]` unless `--yes`, and `--max-usd`.
4. Write `<out>/<replay_id>/audit.jsonl` with one `AuditRow` per submission, and beside it `replay.json` (`AuditReplay`: `audit_version`, `replay_id`, `since`, `until`, `include_all`), and print the counts.

```python
# architect/bench/audit.py
class AuditRow(BaseModel):          # frozen, extra="forbid"; content-free
    audit_version: Literal[1]
    replay_id: str                  # uuid4
    spec_hash: SHA256_HEX
    session_id: str
    submission: int
    depth: int
    mode: Literal["agent", "template"]
    model: ModelId
    agent_model: ModelId | None
    recorded_at: datetime
    artifact_digest: SHA256_HEX
    verdict: Code
    floor_limit: Score
    tuning_score: Score | None
    tuning_correct: int | None
    tuning_cases: int
    tuning_invalid: int
    met_bar_in_practice: bool
    exam: Literal["passed", "below_bar", "over_budget", "invalid_exceeded", "error"]
    held_out_score: Score
    held_out_correct: int | None
    held_out_cases: int
    held_out_invalid: int
    held_out_over_budget: int
    practice_drop: float | None     # practice minus exam accuracy when both were scored, from exact fractions
    floor_origin: Literal["public_dataset", "synthetic", "no_manifest"]
    evidence: Evidence
    replay_cost_usd: NonnegativeFloat
    floor_cases_sha256: SHA256_HEX | None = None  # review fix: the replayed floor's cases
```

`floor_origin` is the 5.2 check of the replayed floor against its profile manifest, or `"no_manifest"` when the profile floor names none. `exam` is `"passed"` when the report passed; `"over_budget"` when it carries `primary_budget_failed`; `"invalid_exceeded"` when it carries `primary_invalid_exceeded`; `"below_bar"` when it was scored below `floor_limit`; `"error"` otherwise. `floor_cases_sha256` is `cases_sha256` of the replayed profile floor's cases in file order, which for a public floor equals its manifest's `cases_sha256`. The gate-audit count (review fix) is n of m over the rows with `met_bar_in_practice` that `audit.exclusion` keeps: a `public_dataset` floor, an `evidence` other than `synthetic_fake`, and an exam that was scored (`passed` or `below_bar`). m counts them (`AuditResult.met_bar`) and n those `below_bar` (`AuditResult.gate_audit`). The met-bar rows left out are counted by reason in `excluded_floor`, `excluded_fake` and `excluded_unscored`. The exam classes and the harnesses whose `practice_drop` exceeds 0.05 are counted over every row. That comparison is exact (review fix, `audit.practice_drop_exceeds`): practice correct over practice cases minus exam correct over exam cases, as fractions, against 1/20, so a drop of exactly 5 points never counts; a record with no practice counts takes its practice score as the nearest fraction over its case count. `practice_drop` itself is that exact difference as a float. No verification id enters a row.

### 13.3 `scripts/analyze_prereg.py` (skeleton)

```
uv run python scripts/analyze_prereg.py --config docs/bench/prereg-nebius.json --out DIR [--check]
```

The config (`PreregConfig`, committed with the pre-registration) holds `prereg_version` (1), `suite_digest`, `task_id`, `arms` (name to rows source), `hypotheses` (`{"H1a": {"test": "superiority", "base": "A3", "cand": "A7"}, "H1b": {...}, "H2": {"test": "noninferiority", "base": "A1", "cand": "A7", "margin": 0.05}, "H3": {"test": "cost_ratio", "base": "A1", "cand": "A7", "max_ratio": 0.25}}`), `holm` (the hypotheses corrected together), `replicate` (`{"A7": "A7b"}`), `alpha`, `bootstrap`, `seed`, and `audit` (`{"rows", "since", "until", "floor_cases_sha256", "floor_limit"}`; the last two pin the pre-registered exam and may be omitted only outside the pre-registration).

The skeleton:

- loads the arms through `architect.bench`, refusing rows whose `suite_digest` differs from the config's and unreadable rows. A missing arm (review fix: a source that does not exist, no row with its label, only crashed rows carrying `cancelled` or `host_memory_low`, or a row with no delivery pass bits) is left out and listed in `missing_arms` with its reason and in `analysis.md`. Every hypothesis that needs it does not hold: `holds` is false, `pair` is null and `missing` names the arms it lacked. A Holm family keeps its configured size, a member that cannot be computed counting as p = 1. With no arm left the analysis is refused (`bench_compare_mismatch`);
- computes H1a and H1b (exact McNemar with Holm), H2 (Newcombe lower bound) and H3 (bootstrap upper bound) with `compare.pair_arms`, then the same tests with each arm replaced by its replicate;
- reports the delivery test subset by joining bit positions with `examples/receipts/manifests/delivery.json` (`cases[i].source` starts with `test/`). Pass bits, run dollars and the invalid and over-budget counts are recounted over those positions (review fix), the counts from `delivery_invalid_bits` and `delivery_over_budget_bits`. A row written before those bits existed gives 0 when its total is 0 and null otherwise, which the ASCII table prints as `-`, never the whole set's count;
- counts the gate audit from audit rows (13.2) recorded within `since` and `until` (review fix): of the rows that met the bar in practice, a row is left out of m, first reason wins, when its floor is not `public_dataset`, when its evidence is `synthetic_fake`, or, in the window, when its `floor_cases_sha256` or bar differs from the pinned exam; scored exams make m and those `below_bar` make n; over-budget, invalid and errored exams are counted apart, as are scored exams that fell more than 5 points below practice (the exact rule of 13.2), and the rows left out are listed under `excluded` by floor origin, by evidence and as `other_exam`. The rows come from one replay with no `--since` or `--until`, so the secondary count over every row covers every build. A row that repeats a submission is refused (`bench_audit_unreadable`), and so is a replay whose `replay.json` window misses part of the config's; a windowed replay leaves the secondary count null;
- writes `analysis.json` (`sort_keys=True`, `separators=(",", ":")`, `ensure_ascii=True`, floats as Python `repr`, one trailing LF) and `analysis.md` (the 13.1 renderer).

H4 and the chosen outcome are null in the skeleton; the pre-registration (due 10/11) fixes their rules in the config before any main-run row exists. `--check` recomputes both files and exits 1 when either differs byte for byte. No timestamp, host name or absolute path enters the output.

## 14. Error codes

All are lowercase codes from closed tables, explained by one plain sentence each in `architect/session/codes.py`.

| Code | Where | Owner | Sentence |
|---|---|---|---|
| `invalid_fraction_exceeded` | measurement failure | WA | `More runs gave invalid output than the spec allows.` |
| `primary_invalid_exceeded` | report failure; also added to `server.py` `_FAILURE_CODES` | WA | `Too many held-out runs gave invalid output.` |
| `forge_audit_failed` | compile failure | WA | `The Forge audit could not be written, so no Forge harness was kept.` |
| `audit_unwritable` | CLI, agent, bench | WA | `The audit directory cannot be created or written.` |
| `bench_task_unknown` | bench | WC | `That task is not in this suite.` |
| `bench_split_unsupported` | bench | WC | `Only public tasks have floor and delivery splits.` |
| `bench_size_unsupported` | bench | WC | `A public task's size is fixed by its split.` |
| `bench_delivery_all_unsupported` | bench | WC | `--delivery all needs a public task.` |
| `bench_delivery_too_large` | bench | WC | `The delivery split has fewer cases than asked for.` |
| `bench_dataset_missing` | bench | WC | `The public dataset files are not in this checkout.` |
| `bench_dataset_mismatch` | bench | WC | `The public dataset does not match its manifest.` |
| `bench_plain_variant` | bench | WC | `--plain runs only the default and noisy variants.` |
| `bench_audit_unreadable` | bench | WC | `The audit directory or one of its records cannot be read.` |
| `bench_run_budget_too_small` | bench (review fix) | WC | `The pinned per-run budget cannot pay for one run's model calls; cap the model's max_tokens or raise the budget. Nothing ran.` |

Existing codes reused: `invalid_spec_file`, `floor_not_configured`, `budget_exceeded`, `primary_budget_failed`, `bench_compare_mismatch`, `bench_compare_fingerprint`, `bench_variant_unknown`, `invalid_arguments`. WC adds the bench codes, and `audit_unwritable` because `bench run --audit` raises it, to `architect.bench.CODES` and its session mirror; WA writes the sentence for `audit_unwritable` once in its own block.

## 15. Tests per worker

All tests are offline (`FakeAdapter`, `ScriptedAdapter`, oracles, `ForgeScript`). On this host run them as `uv run pytest <files> --basetemp C:/t/<label> --color=no -q`.

### 15.1 WA

- **Hash and signature invariance.** Before editing, record `spec_hash` and the serialized bytes of a v1 bench spec, an inferred-shape spec and a pipeline spec at `3d6f304`; after the change they are identical. An explicit `"invalid_policy": "fail_partition"` hashes the same. `count_as_miss` hashes differently, and two fractions give two hashes. A report built without counts dumps to the old payload and its old signature verifies; a report with counts signs and verifies, and changing one count breaks it.
- **Validators.** `fail_partition` with fraction 0.05 refused; `count_as_miss` with a secondary floor or a judge floor refused; `plain` without a plain preset hint, with a pipeline, or on a game spec refused.
- **Evaluation matrix.** Under `fail_partition`, today's codes and scores for every class in 4.1. Under `count_as_miss`: invalid output is a miss and the score is `k / n`; invalid above the allowance gives `invalid_fraction_exceeded` and no score; a budget failure is hard and counted in `over_budget`; input-schema and accounting failures are hard. Counts on every `Measurement`.
- **Stage 4.** Report counts, `primary_invalid_exceeded`, and each `licensed` check refusing a hand-built inconsistent report.
- **`receipt_v1`.** Exact ints, null against 0, bool and float rejected (`75000.0` against `75000`), a missing key, an extra key ignored, a malformed expected value raising, and `field_report` counts.
- **Provenance**, on the committed evidence floor: `public_dataset` with `CORD v2 · CC BY 4.0 · 200 receipts` and the ASCII form; one changed character in one expected value gives SYNTHETIC with `case_mismatch` (1 case); a short manifest gives `count_mismatch`; an unreadable one `manifest_unreadable`; a stamped case `stamped_synthetic`.
- **Build mode.** The plan card floor row with provenance and its SYNTHETIC form; the frozen build (full hash row, no Stage 0 call, the override note, `floor_not_configured`, approval of exactly that hash); print mode prints the card and compiles nothing.
- **Audit.** A `ForgeScript` session and a template session write one record per `submit_source` call with tuning data only; the Pi denied paths include the audit directory; a failing write gives `forge_audit_failed` and no promotion; no file under the audit directory contains the report's `verification_id` or any held-out case id.
- **Observers.** A compile with stage and result observers registered never passes them held-out counts, scores or verification ids.
- **Stage 0.** The schema shown has none of the five fields, and a proposal carrying them yields a spec with the defaults.

### 15.2 WB

- **Input limit.** A 5,000-character input is clipped at 2,048 by default and kept whole with `input_limit_chars=8192`; with the field set, a synthesizer grid no longer varies `context_window`.
- **Reply parsing.** Strict is unchanged (a fenced reply still fails). `first` returns the same value as Forge's `extract_json` on a shared corpus (fences with and without a tag, prose before and after, text after a closed think tag, nested objects, arrays, two objects). `last` returns the final object of a reply that holds a draft object then the answer, and skips an unbalanced trailing brace. `schema` passes `response_schema` on the answer, self-check and repair calls (checked on the fake adapter's recorded requests) and parses like `first`.
- **Plain.** Stage 2 measures exactly one configuration (`verifier_depth` 0, `retries` 0) for any `max_candidates`, ignores a library warm start, and each run makes one model call; plain and non-plain specs hash differently.
- **Aliases.** The wire `model`, the price lookup and the reply-model check use `api_model`; `adapter.model` is the alias and `evaluate_cases`' model check passes; `max_tokens` caps the sent `max_tokens` and the quote; both fields are refused on non-OpenAI providers; `model_unpriced` follows `api_model`.
- **Model ids.** The examples of 9.5, accepted and refused, through both `ModelId` and `check_models`.

### 15.3 WC

- **Suites.** `suite_digest("v1") == "1f23e5e421b2c2c3d79482394a841764b37f4e70d3b06a73993fc7bb4bf335ed"`; the v2 digest is stable and changes when the settings or one case change; `tiers_for` per suite; `TIERS` is v1's.
- **Loader.** The committed data loads; a changed case, a missing file and a split overlap each give their code.
- **Fake v2 run.** `bench run --provider fake --suite v2 --tier public --floor-split rehearsal --delivery 20 --yes` writes rows with `floor_origin` `public_dataset`, evidence `synthetic_fake`, every delivery field, `suite_frozen` false and a `receipt_v1` field accuracy; `noisy` lowers delivery accuracy.
- **Flags.** `--tasks`, `--no-tuning-replay`, `--plain` (one candidate, one call per run), `--reply-json`, `--delivery all`, the split flags, `--audit` (records appear for a forge cell), every refusal in 10.5, and a resume across the new fields.
- **Metrics.** Nearest-rank percentiles on fixed timings, and invalid against over-budget classification with a scripted adapter.
- **Pairs.** The reference vectors of 12.2, Holm, a bootstrap that is identical for identical input and seed, a ratio of 1.0 with interval [1.0, 1.0] for equal costs, and each refusal.
- **Report.** A golden Markdown file for a fixed rows fixture, and each footer line on its trigger.
- **Audit replay.** On a fake audit directory: selection, consent, the `exam` classes and the counts.
- **Analysis.** Two runs of the skeleton on fixture rows are byte-identical, and `--check` passes and then fails after a one-byte edit.

## 16. Integration and acceptance

The integrator, after merging WA, WB and WC:

1. Runs the full suite through `scripts/run_tests_resumable.py`, then `uv run ruff check .` and `uv run mypy --strict architect`.
2. Confirms `suite_digest("v1")` is `1f23e5e421b2c2c3d79482394a841764b37f4e70d3b06a73993fc7bb4bf335ed`.
3. Reruns the fake v1 smoke tier and confirms every cell's `spec_hash` equals `examples/bench/suite-v1/smoke-baseline.jsonl`, then `bench compare --expect identical-scores --allow-fingerprint-mismatch` against it (presets changed, so the fingerprint differs by design).
4. Confirms an artifact licensed and a package exported before the merge still load as licensed.
5. Runs the fake v2 command of 15.3 and the analysis `--check` twice.
6. Confirms a tampered manifest shows SYNTHETIC on a build-mode plan card (W4 done-when).
7. Writes the DECISIONS entries (the omit-at-default serializers; `count_as_miss` semantics and its limits; provenance labels instead of refusals; frozen builds; the audit as a context variable outside `ForgeSettings`; reply modes; the plain pin; aliases), updates `docs/bench.md`, `docs/usage.md`, `docs/limitations.md` and `PROGRESS.md`, and refreshes `.jakeos/index.md`.

W4a, W4b and W4e land by 10/7 and W4d by 10/6 (PLAN Section 3). The bench pieces of W5 land before the pilot on 10/9; `bench audit` and the analysis skeleton land before the main run on 10/11.

## 17. Boundaries with other workstreams

- **W1 (provider).** WB edits `models/http.py`, which W1 wrote. Only the model id sent, priced and checked, and the output ceiling, change; the Nebius dialect, thinking switch, retries and reasoning-cut guard stay as committed in `adac7d1`.
- **W2 (relay).** Profiles point `base_url` at the relay; nothing in this phase depends on the relay itself. The flex tier is a W1 or relay setting, not part of this contract.
- **W3 (Forge on Nemotron).** W3 owns `compiler/__init__.py`, `forge*.py` and `compiler/types.py` apart from the hooks and the one field named in Section 2. W3's per-rung `max_candidates = 1` gives the plain configuration only when no library warm start applies, so the product ladder's Super and Ultra rungs should skip warm starts, or the pre-registration must define those rungs as "the first grid configuration". A frozen spec cannot pin plain per rung, because one spec covers every rung.
- **W6 (desktop).** The exam chart reads `held_out_counts` from `report.json` after `compile()` returns; nothing reaches it through observers. The desktop shows the `public_dataset` origin and the plan card's provenance text, and draws the separator its own way.
- **W7 (trust path).** Ed25519 signs the same report payload, counts included when present. Whether `EVIDENCE.json` repeats the counts is W7's call; this phase does not change `architect/forge_modes/`.
- **Data script.** Manifests stay its output. `title` and `unit` are optional keys it may add. A judge floor (20 practice, 20 exam) shows as `public_dataset` only with its own manifest in the same format.
- **Pre-registration (10/11).** It freezes `examples/bench/suite-v2/receipts.json`, commits the analysis config and footer, and states every arm as a bench command with its `--label`.

## 18. Honesty rules

- Fake-provider rows and every v1 task are plumbing checks; reports say so on the page and they are never quoted as model results.
- Rows measured while `frozen` is false are rehearsal numbers, and the report prints that line.
- A SYNTHETIC floor, drafted or failing its manifest, never counts as public data anywhere.
- Delivery numbers come from cases no build step saw; tuning numbers are labelled tuning or practice, never exam.
- The gate-audit count uses audit replays of builds recorded after the pre-registration tag; a count across all builds is labelled secondary.
- The filmed builds (A5c, A7c) never enter the analysis config.

## 19. Non-goals

- W4c (the label-list lint exemption) and any Banking77 data or task.
- Any change to v1 tasks, v1 sizing or the v1 digest.
- `count_as_miss` on judge, game or edit-rate floors, and with a secondary floor.
- A replay-only bench mode: every cell still compiles.
- Showing any held-out number to Forge, to observers or in the audit directory.
- Auditing compiles made through `architect serve`.
- Updating floor labels in the step-by-step `architect session`.
- Tavily, relay, installer and desktop work.
