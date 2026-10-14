# Phase 13 live: Forge into software (2026-09-30, Windows, claude CLI)

Conductor claude-sonnet-5, play model claude-haiku-4-5, Forge agent claude-sonnet-5 via the claude CLI. API-equivalent costs, not a bill. SYNTHETIC floor (12 drafted cases, 6 held out).

```
> /forge an agent that classifies a customer support message as billing, technical, or account and returns the category
* make(an agent that classifies a customer support message as billing, technical, or account and returns th)
  ... interview: reading the request with claude-haiku-4-5-20251001 ...
Where will this agent live?
  [1] Run it here (installed; /run, `architect run`, HTTP through /serve; local models allowed)
  [2] Put it into software (a tested package exported into this repository)
Choose (1/2) [1] 2
Package directory in this repository? [agents/classifies_customer_support] 
What should the input field be named (message, text, content, etc.)? [{"message": "My billing statement shows a duplicate charge"}] /skip
  ... stage 0 infer spec: one call to claude-haiku-4-5-20251001 ...
  ... floor: drafting 12 cases with claude-haiku-4-5-20251001 ...
  [floor] 'support_message_classification_test_set' configured: 12 cases drafted, SYNTHETIC (6 tuning / 6 held-out)
== Plan: classifies-customer-support ==
  task          Classify customer support messages into billing, technical, or account categories
  engineer      Forge (claude-sonnet-5) writes a custom harness; baseline to beat: extractor
  inputs        message
  outputs       category
  floor         held_out_tasks >= 0.85 on 'support_message_classification_test_set'
  floor data    12 cases drafted, SYNTHETIC (6 tuning / 6 held-out)
  budget        $0.69 and 2 min per run; compile up to $5.00 and 30 min
  forge budget  up to $5.00 and 60 min on top of the compile budget (6 submissions, 24 measures, 3 sub-harnesses)
  model         claude-haiku-4-5-20251001
  destination   into software: agents/classifies_customer_support/ in this repository after the license (shown and asked before anything is written; feature flag off until reviewed)
  install as    classifies-customer-support (installed, then exported)
  note          per-run budget raised to $0.69 so two calls of the model fit and 2 min so two calls of the model can finish
spec 1e4cb42c. Enter compiles; answer no to edit with /floor, /budget or /model first.
Compile this agent? [yes] 
  [compiler] compiling classifies-customer-support (spec 1e4cb42c)
  ... stage 1 select preset: 3 calls, $0.05
  ... stage 2 tune: 3 calls, $0.05
  ... stage 3 forge: 15 calls, $0.07
  [forge] write 3391 bytes
  [forge] forge_check (failed) Validation failed for tool "forge_check":
  [forge] forge_check
  [forge] forge_dryrun
  [forge] forge_measure
  [forge] forge_submit (failed) Validation failed for tool "forge_submit":
  [forge] forge_submit
  [forge] 1 submissions, 1 measures, 0 sub-harnesses, agent $0.73
  ... stage 4 verify: 29 calls, $0.81
installed classifies-customer-support v1: floor 1.00/0.85 (SYNTHETIC floor), $0.81 spent. a custom harness written by Forge (442 nodes, 3391 bytes, 0 licensed sub-harnesses) after 1 submissions and 1 measures; it outranked the tuned extractor baseline on tuning
  ... export: staging classifies_customer_support from classifies-customer-support v1 ...
== Export plan: agents/classifies_customer_support/ ==
  agent         classifies-customer-support v1: licensed, floor 1.00/0.85, extractor on claude-haiku-4-5-20251001
  writes        {scratch}\sw-demo\agents\classifies_customer_support (one new directory; nothing existing is changed)
  files         EVIDENCE.json
                EVIDENCE.md
                MANIFEST.json
                README.md
                __init__.py
                artifact/config.json
                artifact/forge.json
                artifact/harness.py
                artifact/report.json
                artifact/spec.json
                schemas/input.schema.json
                schemas/output.schema.json
                tests/test_classifies_customer_support.py
  size          13 files, 18 KB
  feature flag  ARCHITECT_FEATURE_1E4CB42C: off unless it is exactly 1
  review        a human reviews the package before the flag is set; the license is host-attested (it verifies only with this host's signing key)
Nothing has been written into the repository yet.
Allow export classifies_customer_support (13 files, 18 KB) into agents/classifies_customer_support/ (one new directory; feature flag OFF; human review required)? [y]es / [s]ession / [n]o y
== Exported: agents/classifies_customer_support/ ==
```

The exported harness's own design (artifact/harness.py docstring):

```
"""Classify support messages into billing/technical/account.
Stage 1: deterministic keyword scoring in code (cheap, no model call) using
word sets tied to each category; if one category clearly wins, return it.
Stage 2: only when keywords are ambiguous or absent, ask the model once with
a short, constrained prompt and parse with extract_json.
Stage 3: validate the model's answer is one of the three allowed categories;
if not, fall back to the best keyword guess (or 'account' as last resort).
This keeps cost near zero on easy cases and reserves the model call for the
genuinely ambiguous ones.
"""
```

Called from plain Python in the repository after setting the reviewed feature flag:

```
I can't log in, it says my password expired -> {'category': 'account'}
The app crashes when I upload a photo -> {'category': 'technical'}
Why was I charged twice this month? -> {'category': 'billing'}
```

Also live: /providers and /doctor found the logged-in claude and codex CLIs; /model chatgpt:gpt-5.6-terra switched the conversation to the ChatGPT plan through the codex login and answered with a tool call ($0.053 API-equivalent).
