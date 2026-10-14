# Phase 6 HTTP independent adversarial review

## Scope and method

Reviewed `phase6-http.md`, `token-policy.md`, `phase6-runtime.md`, and the current
Server, Compiler, Registry, Runtime, Spec and Services implementations.
Only this review and `tests/adversarial/test_phase6_server.py` are verifier-owned.
No production code was edited.

Tests author their own local fixture profile, floor cases and fake responses.
Successful licenses come from the real Compiler Stage 4. Registry and Runtime are
real. Narrow compiler-result and private-disk faults test integration failure
handling. They are **not** HTTP inputs or claims of resistance to hostile
privileged Python. HTTP cannot supply profiles, reports, artifact paths or licenses.

## First native run

`uv run pytest tests/adversarial/test_phase6_server.py -q --tb=short`

Initial result: **12 failed, 13 passed, 4 skipped** (7.20 seconds).
The four skips are file symlink creation unavailable to this Windows account.
Root's report-equality and `not_better` changes were already present in this run.
Follow-up result with four more cases: **13 failed, 16 passed, 4 skipped** (7.26 seconds).
After root's fixed-code allowlist change: **9 failed, 20 passed, 4 skipped**.
The four compact-code privacy regression cases pass at that checkpoint.

Final independent rerun after the server fix worker's changes:

`uv run pytest tests/adversarial/test_phase6_server.py tests/test_server.py -q --tb=short`

**92 passed, 4 skipped**. All seven findings below are closed by their unchanged
regression tests. `uv run ruff check tests/adversarial/test_phase6_server.py` also
passes. The findings remain below as the audit trail, not as open defects.

The freshness fix snapshots persisted verification IDs before calling the trusted
compiler. This rejects the reproduced stale result, including a candidate created
by a direct compiler call. It is not an invocation-scoped capability and does not
claim resistance to privileged concurrent file swapping. Static review found no
new definite blocker in the fix. Root owns full-suite and TypeScript integration.

## Reproduced findings

1. **Fixed-code privacy is incomplete.** A real compile result with an injected
   failure `PRIVATE_CUSTOMER_A731`, `customer.secret`, `C:private` or `api-key-123`
   returns that exact string to HTTP. A character/length filter is not a fixed-code
   allowlist. Replace unknown strings with `compile_failure`.
2. **Accepted ephemeral artifact need not match its persisted report.** Remove
   `report.json` immediately after a real Stage 4 compile returns. `/compile`
   still returns `accepted=true` and publishes an opaque ID. Runtime subsequently
   rejects delivery, so this is false acceptance, not an unlicensed run. Reload
   and compare persisted metadata before publishing an ID.
3. **Fresh Stage 4 is not established by the stage tuple.** Install a real artifact,
   compile a cheaper real candidate separately, then make the requested recompile
   return that preexisting result without doing work. The HTTP recompile accepts
   and Registry publishes the cheaper old candidate. The signature is authentic,
   but no Stage 4 happened for this request. A trusted invocation-scoped verifier
   receipt would distinguish real fresh measurement from a replayed result.
4. **Catalog database hardlink is followed and modified.** Precreate an external
   SQLite database and hardlink it as `workspace/server/catalog.sqlite3`.
   `create_app` follows it and writes catalog tables to that external database.
   Validate catalog directory and database identity/link state before connecting.
5. **Artifact hardlinks are accepted.** For each of the four artifact files,
   copying authentic bytes outside the artifact tree and hardlinking them back
   allows delivery. This is an alias/containment hardening gap, not signature
   forgery. The explicit contract says reject symlink escapes; rejecting hardlinks
   is a stronger fail-closed interpretation requested for this review.
6. **Missing catalog is recreated by a read.** Deleting the DB after app startup
   makes status return the correct `catalog_unavailable`, but SQLite silently
   creates an empty database in its place. This is state-preservation hardening;
   the request does not gain execution authority.

7. **Corrupt catalog location leaks through status.** Replace the installed row's
   location with `C:/private/PRIVATE_CUSTOMER_A731`. Registry authentication fails,
   but status returns the private value in `registry_name`. Validate the installed
   location against its server-owned spec-hash naming invariant before responding.

## Confirmed passing gates

- Missing or mismatched result report fails after root's equality fix.
- Changed explicit Spec and missing Stage 4 are rejected. An invalid catalog root
  returns a fixed error and does not close caller-owned Services.
- Tied real recompile returns `not_better` and leaves Registry pointer unchanged.
- Two concurrent cheaper real recompiles publish exactly one replacement.
- Duplicate bearer headers fail before the ASGI receive callable is invoked.
- A real loopback socket request sends duplicate bearer headers and declares a
  body but sends no body bytes. Uvicorn returns 401 without waiting for that body.
- Actual loopback HTTP performs proposal, explicit approval, installation and
  delivery; both delivery flags are required.
- Nonfinite, duplicate-key, deep, mistyped-flag and extra authority JSON is rejected
  with fixed `invalid_request`, not echoed validation detail.
- Caller Services remain open after ASGI lifespan shutdown. The private server
  path is in effective Pi denied paths. 20,000 observed tokens are delivered and
  metered without a token quota. Ephemeral restart and one-shot expiry hold.

## Limitations

No remote provider or production deployment readiness is asserted. Fixtures are
synthetic. File symlink tests must also run on a host that permits symlink creation.
Race tests exercise cooperative concurrent requests; they do not establish an OS
sandbox against privileged filesystem mutation or arbitrary Python monkeypatches.
