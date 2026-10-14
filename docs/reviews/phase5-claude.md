# Phase 5 Claude Code development evidence

Claude Code CLI 2.1.282, authenticated model `claude-opus-5-5`, implemented the bounded orchestrator and its offline tests. It edited only `architect/orchestrator/__init__.py`, `tests/test_orchestrator.py` and orchestration fixtures. Local saved result: `.architect/claude-phase5-orchestrator.json` (59 turns, reported development cost $2.891565). This is development-tool usage, not Architect production usage or Pi execution.

The worker reported 46 passing orchestrator tests. The integrator subsequently changed the token policy, inference field handling and integration tests. That earlier test result is historical, not final acceptance. Current full-suite evidence belongs in `PROGRESS.md` and the final coverage report.

The worker identified real inferred-schema suppression by default empty Subtask fields and stale pipeline component-count fixtures. Both were addressed in integration. The product coder continues to use upstream Pi, not Claude Code.

## Final read-only adversarial pass (integration ongoing)

CLI `claude-opus-5-5`, 42 turns, reported development cost $3.267182. Saved result `.architect/claude-phase5-final-audit.json`. No commands/tests/edits were run by the reviewer. Code inspection confirmed (1) active Python worker/environment files needed protection when coding in this checkout, and (2) a concurrent wallet could exhaust while Pi awaited the next RPC, so tool/compile branches need fresh checks after the wait. Both were assigned to the owning worker. The first also led to isolated installed-module sandbox launch and staged, reviewed dogfood replay. These findings are not closed by this note; independent tests and current root acceptance must close them. Token migration, nested reservations, scope revocation, repo grant, Stage4/approval and pipeline semantics had no further finding in that read-only pass.

## Closure after current integration

Both concrete findings above are fixed. Pi file tools deny the entire active package, Python environments and resolved host Node executable. Sandboxed harnesses load the installed worker with `python -I -m`, without adding the writable checkout to `sys.path`. Pi rechecks the parent budget after each awaited RPC read, before tool and compiler work. The independent review in `docs/reviews/phase5.md` confirms both fixes, including a real-Pi concurrent-wallet regression.

Integrator validation: 1,538 tests passed, 3 live-provider tests skipped, 94% total branch/statement coverage. Ruff and strict mypy pass. Hardened dogfood passed with 29 actual Pi tool calls, 33 telemetry rows, and 12 native action-decoder tests in a separately reviewed checkout. Additional negative tests are being added for uncovered gate branches before the Phase 5 commit. These results use scripted model fixtures and do not establish live-provider quality.

Final integrator acceptance supersedes the earlier checkpoint: **1,610 passed, 3 skipped, 96% coverage**; Ruff and strict mypy pass. Added defensive tests were included in the fresh full run. No production change was needed for those tests.
