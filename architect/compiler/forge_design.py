"""DESIGN.md: how Forge makes a harness that is specialized, not a re-specced preset.

Phase 12 direction: built agents are "truly specialized and unique ... very good at their
job". The preset baseline (BASELINE.json) is a generic prompt-and-parse loop; Forge's job
is to engineer a harness for this one task. This playbook is written into every Forge
workspace; it names general strategies (code before model, the task's own rules enforced
in code, checks and repair, measuring, submitting only what beats the preset), never
budgets, answers or anything about a particular task.
"""

from typing import Final

__all__ = ["DESIGN_TEXT"]

DESIGN_TEXT: Final = """\
# Designing a specialized harness

BASELINE.json is a generic preset: one prompt, parse, maybe retry. Beat it by engineering
for this task. Decide the design from SPEC.md and from what fails in forge_measure, then
write it down in the docstring of harness.py (3 to 6 lines: the stages and why). The
docstring is shown to the user as the agent's design.

## Strategies (combine the ones the task needs)

1. Code before model. Anything a program can do exactly belongs in code: parsing,
   splitting, arithmetic, date and number normalization, lookups, sorting, dedup, schema
   checks. Ask the model only for judgment. Smaller prompts, fewer errors, lower cost.
   When code can read a field exactly from the input, code's value is final for that
   field; ask the model only for what code cannot decide.
2. Enforce the rules the task states. Read every rule in the task text in SPEC.md and
   apply in code each one that code can apply: formats, allowed values, which of several
   candidates wins, what to leave empty, what never to infer. A model that is told a rule
   still breaks it often; code does not.
3. Decompose. Split the task into focused steps with their own small JSON output shapes
   (locate, then extract, then normalize; or classify, then act). One job per call.
4. Check and repair. Write task-specific checks in code (required fields, types, ranges,
   cross-field consistency, parts that must add up, values that must appear in the
   input). When a check fails, re-ask with the exact failure, once or twice, then fall
   back safely.
5. Agree before trusting. For fields that are often wrong, ask twice with different framings
   (or cheap then careful) and reconcile in code; escalate only on disagreement.
6. Cascade. Try the cheapest path first (code only, or one short call) and spend more only
   when a check fails. Budgets are real; a harness that is right and cheap wins ties.
7. Specialize sub-skills. When a sub-task has clear input and output shapes and a different
   difficulty, forge_spawn it and call it with context.harness("name", value).
8. Measure, then learn from failures, not answers. Run forge_measure after each real
   change, on more cases as the harness improves, and compare with BASELINE.json's score.
   Name the failure pattern (format drift, a missed field, a wrong unit, a broken rule,
   ambiguity) and encode a general rule or check. Never copy expected outputs or long
   input text into code; forge_submit rejects it.
9. Games and interactive tasks: keep state in code (visited states, tried actions, what
   changed), ignore parts of the frame that change every step (timers, energy bars),
   explore systematically, and call the model only when code has no untried idea.

## Before forge_submit

- forge_measure shows the harness above BASELINE.json's score and at or above the floor
  limit in SPEC.md. If it does not, keep improving: a submission that is not better than
  the preset is refused and uses up one of the attempts.
- The harness does something the baseline cannot: enforced rules, code checks,
  decomposition, agreement or a strategy of its own. A reworded prompt, or the preset
  call alone, is not a design.
- Every model reply is parsed defensively with a fallback; no path can crash on bad output.
- The docstring explains the design in plain words.
"""
