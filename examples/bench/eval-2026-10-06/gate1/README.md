# Gate 1 replies, 2026-10-06

Every reply from Gate 1: plain Nemotron 3.5 Lightning (`lightning.jsonl`) and plain
Nemotron 3 Super (`super.jsonl`) on the 100 rehearsal receipts, one line per receipt,
read through the relay from 07:23 to 07:27 UTC with the spec of the 2026-10-05 plain arms.

Each line holds the case id and its labels (`expected`), the exact request, the raw reply,
the parsed answer (`output`), whether it matched (`passed`), tokens, cost and latency.

To recompute the Gate 1 table in `docs/bench/results-receipts.md`, apply
`guard_digits` and then `guard_not_total_copy` from
`docs/bench/eval-2026-10-06/frozen_checks.py` to each `output` (each check takes the
receipt text from the request's `input` and the answer), and count the receipts whose
four fields then equal `expected`. The rehearsal receipts are for development, so they
are not held out.
