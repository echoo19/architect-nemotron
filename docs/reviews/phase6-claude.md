# Phase 6 Claude Code development evidence

Actual Claude Code CLI `claude-opus-5-5` authored `architect/server.py` and `tests/test_server.py`: 46 turns, reported development cost $2.537812. The saved CLI result is `.architect/claude-phase6-server.json`. The CLI ended with a waiting message rather than a final validation report. The integrator therefore ran the tests independently: 63 server tests passed. Root integration then added exact CompileResult/report matching and preserved the fixed `not_better` registry code; combined preset/Phase4/server regressions passed (164 tests).

The actual TypeScript client also passed against the live localhost server: proposal, exact approved compilation, status, guarded delivery, rejected tied recompile, unchanged status and a second delivery. The owned server process was then stopped.

This is development-agent use, not Architect/Pi dogfood or evidence of production model quality. Independent adversarial HTTP verification remains in progress. No deployment or remote CI is claimed.
