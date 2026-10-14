# Independent Phase 2 review

Status: **no open Phase 2 review blocker found under the clarified interface**. Final approval requires the integrator's full phase-exit and tool results. No implementation or test files were edited by this reviewer.

## Scope and evidence

Reviewed the full source `docs/build-prompt.md`, `docs/interfaces/phase2.md`, `architect/presets/{__init__,_execution}.py`, all `architect/floors` modules, and arriving unit, exit and independent adversarial tests. Phase 1 review B1–B3 is closed; the integrator reports Phase 1 commit `f4de300` with 333 passed, 3 live skips and clean tools.

- Initial reviewer adversarial/exit subset: **89 passed in 1.41 s**. Final owner-unit/adversarial/exit subset command: `uv run pytest -q tests/test_presets.py tests/test_floors.py tests/adversarial/test_phase2.py tests/test_phase2_exit.py`; see final result below.
- Reviewer `uv run mypy --strict architect`: **clean, 19 source files**.
- Independent `uv run python -c ...` probe used local temporary telemetry and scripted fake responses. Coder, planner and verifier each accepted the raw response `not a pack, plan or verdict` under a string output schema with optional components removed.
- This subset is not the complete Phase 2 exit run. Owner tests and final lint/type results are still being integrated. No conclusion relies on initial transient tool findings.

## Interface clarifications: no unresolved defect

### Coder stub output (former P2-B1): WITHDRAWN

The current contract explicitly puts the marked `backend='pi_pending'` description in emitted `pi-pack.json`. Guarded build/execute smoke returns the declared Spec output schema, not a mandatory pack-shaped runtime result. Reviewed `pack_description`, emission and coder tests meet that contract. No shell execution was found. This remains a clearly labeled interface stub, not a working Pi coding backend.

The earlier review used interface wording that described smoke as producing the marked pack. The integrator confirmed that the sidecar-only interpretation was an intentional dispatch clarification. This is not an outstanding implementation fix.

### String planner/verifier results (former P2-B2): WITHDRAWN

The revised interface explicitly allows prose plans/verdicts for string output schemas; object schemas use `steps` / `passed` and `reason`. The Spec schema is authoritative. Current code enforces the relevant object structures and preserves raw string outputs as specified. Tests cover all preset string outputs, invalid structured outputs, bounded plan lengths and failed optional checks.

The independent probe's `accepted=True` is **internal guarded-execution telemetry**, not a passing floor, artifact license or permission for client delivery. Schema-valid but semantically wrong text is expected to fail the quality floor. The Phase 2 exit separately tests a quality-floor failure for every preset. The probe therefore is not a licensing or quality-gate bypass.

## Checks with no discovered Phase 2 bypass

### Held-out isolation and floor integrity

- Construction deep-validates and snapshots records, rejects duplicate IDs, sorts IDs deterministically, and splits internally. Human edit partitions enforce their configured minimum; every other partition must be nonempty.
- Sealed partitions and issued verification provenance are outside public instance dictionaries. Public `held_out` always refuses. Capability checks use identity, not truthiness/equality.
- Tuning and verification retrieval returns deep copies. Verification scoring requires the exact issued object, matching sealed contents, scoped to that evaluator. Reconstruction, mutation, mixed partitions, foreign cases and duplicate score cases fail.
- The independent static test limits production imports/access of `_VERIFIER_CAPABILITY` to the capability module and future Stage 4 verification module. This is a trusted-Python capability boundary, not a hostile-reflection sandbox. Future Stage 3 sandbox isolation remains essential.
- HeldOutTasks scores actual supplied candidate output through equality or a boolean checker. HumanEditRate scores current disagreement with `shipped`, not historic `changed`; malformed historic bits are refused. Game scoring receives only selected task/output snapshots and rejects nonfinite/out-of-range scores.
- LLMJudge requires rubric, model, matching adapter and a distinct nonempty calibration set. Rubric/calibration/spec are snapshotted; model identity changes are refused. Calibration checks human disagreement, poisons failed calibration, and is locked across concurrent first use. Judging uses current candidate output, not expected labels, through budgeted and metadata-only Runtime evaluations.
- Scoring validates nonempty equal lengths, authorized case membership, unique IDs and finite JSON outputs. Edit-rate scoring enforces minimum replay count. Other floors permit authorized subsets, consistent with successive-halving/tuning; Stage 4 must still use its required held-out evaluation set rather than cherry-picked subsets.

### Preset execution and persistence

- Exactly seven named presets exist. Parameter grids are bounded and each preset exposes at most three tunables. Unknown parameters, out-of-range values, invalid component subsets and removal of required components are refused.
- Registry lookup returns deep copies. Build captures a spec/config snapshot. Emission embeds config literals in signed `harness.py`; execution does not read mutable `preset.json`. The emitted code delegates to trusted package implementation, within the existing host boundary.
- All observed model calls use `RunContext.call`. There is no naked adapter invocation or shell/eval tool execution in the preset module.
- Output JSON is parsed and schema-validated; nonfinite constants are refused. Self-check results are parsed as structured verdicts and failed checks reject/repair candidates. `verifier_depth=0` actually skips optional checks.
- Retrieval and preference memory use explicit task inputs. Removed recommender components strip their corresponding fields. Explorer probe and planner search add guarded model calls and validate intermediate structures.
- Multi-stage pipelines fail explicitly rather than silently pretending to execute. External JSON Schema references are rejected; failed resolution is not accepted as a valid output.

## Nonblocking limits / later-phase requirements

1. The coder executes no Pi backend yet. This is an explicit Phase 2 deferral; the emitted marked pack description is present. Real Pi execution, version pin and coding dogfood remain Phase 5 work.
2. Full pipeline orchestration, aggregate compile/verification budgets, report licensing, complete held-out Stage 4 evaluation, and correct configuration telemetry belong to Phase 3. The floor API does not itself prove that supplied outputs came from the candidate; the trusted compiler must execute the candidate, then score those results.
3. Frozen Pydantic models can contain mutable nested dictionaries. Snapshots/fingerprints prevent the reviewed stored-floor and emitted-config mutation attacks; this is not a promise to prevent malicious reflective host code.
4. Judge calls each have a guarded per-evaluation budget; a whole calibration-plus-scoring workload can consume several such budgets. The later compiler must account for the whole workload and ARC compile-time cap.
5. Context-window tuning currently bounds a character payload, not tokenizer context length. The implementation labels this accurately and model quotes remain the token/dollar guard.
6. Floor subset support is not authority for Stage 4 to cherry-pick favorable held-out cases. That acceptance policy must be enforced in the verifier.
7. Official game environments and the `hypothesis_tester` preset remain Phase 6; only the game scorer interface/stub is required here.

## Conclusion

No concrete Phase 2 blocker remains under the clarified interface. The two preliminary preset findings were contract interpretation issues, not observed quality-floor or license bypasses. Held-out isolation, current-output floor scoring, calibration refusal, guarded calls, schema validation, component behavior and persisted config binding passed the reviewed attacks. Final phase acceptance remains the integrator's responsibility.

### Final reviewer test output

```text
........................................................................ [ 32%]
........................................................................ [ 64%]
........................................................................ [ 96%]
........                                                                 [100%]
224 passed in 2.47s
```
