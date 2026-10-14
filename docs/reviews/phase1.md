# Independent Phase 1 review

Status: **B1–B4 closed on final code recheck; no open Phase 1 review blocker found.** Final phase acceptance still requires the integrator's full-suite, lint, type and coverage results. No implementation or test files were changed by this reviewer.

## Scope and evidence

Reviewed the full archived original user prompt and v0.2 specification in `docs/build-prompt.md`, `docs/spec.md`, `docs/interfaces/phase1.md`, every current Python module under `architect`, and the schema, adapter, telemetry, runtime, exit, package-boundary and adversarial tests. The full source review found no additional Phase 1 blocker. Product-level decision rules and deferred work are tracked below.

## Final recheck evidence

- Reviewer command: `uv run pytest -q tests/test_runtime.py tests/test_models.py tests/test_telemetry.py tests/adversarial/test_phase1.py tests/test_phase1_exit.py` — **258 passed in 5.75 s**.
- The original independent B2 probe was rerun unchanged. Instead of delivering output, it now raises `BudgetExceeded: Harness left model calls unfinished`. The new adversarial tests additionally verify cancellation/drain completion and exactly one rejected telemetry row for both disk and evaluation entrypoints, on return and exception paths.
- The integrator previously reported **294 passed, 3 live skips, ruff and mypy clean**. Its final full-suite/coverage run is separate from this reviewer subset and remains the phase-exit authority.
- Compared fixes with the revised `docs/interfaces/phase1.md` and the full source in `docs/build-prompt.md`.

## Closed Phase 1 blockers

### B1 — Executed callable did not match licensed code: CLOSED

Original evidence: the runtime hashed `directory/harness.py` but executed an unrelated caller-supplied callable; old success fixtures even licensed a comment-only file.

Final evidence: `Runtime.run(artifact, task, adapter)` has no caller-harness argument (`architect/runtime/__init__.py:207-223`). It checks the license and model, then loads only the artifact's source using `load_harness`, which compiles source bytes directly rather than trusting cached bytecode (`161-176,266-277`). It checks integrity after loading and before delivery. New tests cover the persisted entrypoint, no callback override, unsigned source not loading, changed source, and source mutation during loading/execution. The issue is closed within the stated trusted-host boundary. Imported dependencies and malicious filesystem/host races are not a claimed Python sandbox.

### B2 — Detached calls could outlive accepted delivery: CLOSED

Original independent reproduction: a harness spawned `context.call` as a task and returned while an adapter waited. Runtime delivered output and persisted `accepted=True` with one quoted token; the adapter later consumed 100 tokens and raised `BudgetExceeded` without updating the row.

Final evidence: `RunContext.call` tracks owning tasks; `_close` closes the context and cancels/drains outstanding tasks (`runtime:92-109`). `_execute` rejects a harness that returns with unfinished calls and runs cleanup before the sole telemetry append (`282-284,298-324`). The original exploit now refuses delivery. Tests cover disk/evaluation entrypoints and both returned/raised harness endings, verify no active adapter work remains, and verify rejection rows retain reservations. Hard cancellation remains cooperative as documented under O2; this fix does not claim hostile-host isolation.

### B3 — Reported provider-model substitution was hidden: CLOSED

Original evidence: HTTP adapters discarded provider identity and always labeled output with the requested model, so runtime's comparison could not detect a returned different model.

Final evidence: `_HTTPAdapter._post` rejects a supplied response model unless it equals the requested model or an explicitly configured `allowed_response_models` entry (`architect/models/http.py:77-83`). Aliases are declared in the pricing config and documented as identities billed at the configured rate (`models/pricing.py:21-22`). Tests cover all three providers, mismatches, malformed identities, allowed aliases and unconfigured aliases. Canonical response/telemetry model names remain the configured model. Compatible responses without a `model` field still rely on the requested endpoint binding; the code does not prove an absent or dishonest server identity. This is a trust limitation, not the original silent reported-substitution defect.

### B4 — Initial tool failures: CLOSED

The integrator's later green full run superseded the first review's test, lint and typing failures. No old tool findings remain open.

## Other observed deviations and limitations

Integrator dispositions: O1 is assigned to Phase 3 compiler integration; O2 hard process stops belong to the Stage 3/Pi sandbox, with cooperative trusted-preset limits documented; O3 discard belongs to Phase 5; O4 incremental tail reconciliation has now been reviewed as implemented. Deferred items are not claims of completed product behavior.

### O1 — Model configuration telemetry is always empty

`runtime:266` emits `model_params={}` for every run. Artifact parameters are signed, but no whitelisted numeric configuration is reported, and even call-level `max_output_tokens` is absent. This is privacy-safe but makes materially different runs indistinguishable in the required configuration field. `task_type` is likewise always the hash of `"harness"` rather than a meaningful task/preset category (`runtime:259`). Phase 1 does not define the task-type mapping; later compiler integration must supply accurate metadata, not just present keys.

### O2 — Compute deadlines are cooperative, not process-level hard stops

`runtime:80-87` uses `asyncio.wait_for`. A synchronous CPU loop blocks its event loop; a coroutine that suppresses cancellation can delay completion beyond the limit. The current timeout tests use cooperative event waits. Trusted-host scope means this is not a claim that Python reflection should be sandboxed, but generated or accidentally blocking code cannot receive a hard-stop guarantee from this mechanism. This must remain an explicit boundary until the later sandbox exists.

### O3 — Ephemeral code is consumed, not discarded

`runtime:150-160,234-235` creates and fsyncs `.consumed`; no path deletes `harness.py` after execution. Reload tests deliberately retain it. This satisfies the narrower Phase 1 consume-once interface, including failed execution, but not the source lifecycle requirement to discard the executable artifact. Treat cleanup as later lifecycle work, not as an already implemented feature. Consumption is directory-local; copying an artifact without its marker yields another runnable copy under trusted-host control.

### O4 — Persistence is recoverable but has operational limits

- Artifact `save()` atomically replaces each JSON file separately (`spec:197-208`), not the complete file set. Mixed generations after a crash generally lose a license rather than grant one, which is fail-closed, but coherent multi-file availability is not guaranteed.
- **Resolved performance finding:** startup still reconstructs the index, but normal append now reconciles only the unindexed tail using an offset index (`store:92-136,148-158`). New tests verify that history is not reparsed/reindexed, unindexed tails recover, duplicate tails fail, truncation fails closed, and torn tails are repaired. `rows()` necessarily reads all returned records.
- If final telemetry persistence fails, its exception replaces a harness exception (`runtime:252-276`). No result is delivered, but the contract's original-exception propagation/exactly-one-row guarantee is conditional on functioning storage.
- HMAC key creation uses exclusive creation, but a concurrent signer can see the new file before its 32 bytes are fully written (`_authority:28-44`). This fails closed as an invalid key, an availability race rather than forgery.

These limits should not be described as transactional or unconditional exactly-once execution guarantees.

## Gates reviewed without a discovered bypass in their stated scope

- **Authenticity:** the HMAC covers all report values except its signature; verification does not create a key. Unsigned/forged reports and post-signature report changes fail. Current floor, budget, component count and artifact digest are independently checked. The private signer is callable by trusted Python; that is explicitly within the accepted host boundary, not a model-forgery defect.
- **Primary floors:** edit rate uses the lower-is-better comparison. A primary-only report cannot license a secondary floor; current behavior correctly fails closed.
- **Ordinary budget calls:** a lock serializes reservations; token/dollar quotes are checked before invocation. Actual overquote usage is retained, poisons the context, and prevents ordinary caught-error delivery. Failed/cancelled calls retain their conservative reservations.
- **Privacy:** categories accept only the closed set or bounded SHA256 identifiers. Nested parameter values are strictly numeric/boolean with whitelisted keys. The store revalidates rows before persistence, including mutable dictionaries. No task, prompt, result or exception text is intentionally logged. Hashes are deterministic and dictionary-attackable; code documents that hashing is not anonymization. This matches the interface's explicit hashed-category policy.
- **Telemetry durability:** JSONL is fsynced before index commit; reopening reconciles rows. Duplicate UUIDs are rejected. Completed invalid rows fail closed. Torn incomplete final JSON is recoverable. Threaded two-store serialization and incremental-tail tests pass in the final reviewer subset.
- **Lifecycle:** unlicensed/mismatched attempts are rejected before consumption. Accepted expire attempts are marked before execution, including failures. Install artifacts can repeat. Rejection paths log without private error text.
- **Dependencies/domain boundary:** real adapters use injectable HTTP transports and live tests require explicit opt-in. Pricing is configured in JSON. No business-domain logic was found in the core package.

## Later-phase work, not reasons to reject the Phase 1 module scope

The following are required before the full product release, but Phase 1 explicitly does not implement them:

1. Stage 4 as the sole production report issuer; isolated held-out capability; minimum samples, LLM judge calibration/rubric refusal, game/edit-rate evaluators, and secondary-floor report evidence. `FloorDefinition` alone currently permits an uncalibrated LLM judge; only the later evaluator/verifier may make that safe to license.
2. JSON-schema execution checks, inference provenance/approval and user overrides, seven presets, pipeline handling, library-first selection, tuning search, fixed smoke tests, cheapest passing/model ladder behavior, compiler artifact paths, and held-out verification on every cached reuse.
3. Sandbox-bound code rewriting, cheaper-only acceptance, rewrite iteration cap, registry floor/cost/simplicity comparisons, library promotion/eviction, rollback, model-change and rolling-floor drift handling.
4. Ephemeral artifact removal, orchestrator total/per-agent budgets, pinned Pi integration and coding dogfood evidence.
5. Client delivery feature flag and human-review gating (source Decision Rules), including the report-card month of better cost with floor compliance; HTTP and four-method TS client with local-server end-to-end tests; domain examples; ARC adapter/simulator and total compile/runtime compute cap; official-rule confirmation.
6. Definition-of-done evidence: all presets/stages success/failure coverage, domain tests under 60 s, >=85% core line coverage and 100% named gate targets, committed coverage evidence, full docs and 20-line compile sample, five requested telemetry examples, clean-clone/CI/push verification. No coverage report was present at this snapshot.

`pyproject.toml` already says `0.1.0`, but source reserves the v0.1.0 release until definition of done. A development metadata version is not evidence of a release or a completed product.

## Review conclusion

All three substantive Phase 1 blockers are closed by reviewed code and passing targeted regressions. No additional Phase 1 blocker was found during the full-source recheck. The integrator remains responsible for the final phase-exit run. Deferred items above, especially cooperative-only timing, client review gates, held-out verification and ephemeral cleanup, must remain visible and cannot be represented as completed product protections.
