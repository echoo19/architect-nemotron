# Forge on Nemotron through the JSON envelope: G1a probes, 2026-10-03

Two live Forge build sessions on the existing Pi JSON-envelope path, before native tool
calls (P3) are built: Super as the engineer, then Ultra. Plus a 16-call check of one
`write` call through the envelope and as a native tool call.

These probes test mechanics and cost on a synthetic rehearsal floor. They are not quality
evidence, and the licences below say nothing about real accuracy.

## Summary

- Neither session produced a submission. Super got past the envelope (22 of 23 replies
  parsed) but wrote ordinary Python that the v2 sandbox rejects. It then wrote one
  identical file five times in a row and ended with a progress note sent as its final
  answer. Ultra broke
  the envelope on its first `write`, and both repairs ran to the 32,768-token cap. The
  session ended after 3 turns, with 90% of its $0.23 spent on those two calls.
- No `REASONING_CUT` in either session. Ultra's reasoning was short (79 to 759 tokens per
  call). Both cuts happened in the answer.
- Same `write` task, same models and settings: native tool calls gave 8 of 8 usable writes;
  the envelope gave 2 of 8.
- G1a is not met on the envelope path. Native tool calls (P3) are needed, together with Forge
  pack and settings changes that apply to either transport (see Recommendation).
- Live spend: $0.3635 at list prices against a $1.50 cap.

## Setup

- Code: `hackathon/nebius` at 3d6f304, unchanged. The headless path is
  `architect compile spec.json --profile profile.json --approved-spec spec.json --forge
  --forge-trigger always --forge-usd <cap> --forge-minutes 25 --yes`.
  - It ran in process under a probe driver kept outside the repository.
  - The driver only wraps `OpenAICompatibleAdapter._post` and `_answer`,
    `pi._control_reply` and `PiBackend.run`. It also uses the existing `FORGE_EVENTS`,
    `add_result_observer` and `add_stage_observer` hooks.
  - It records usage, finish reasons, parse outcomes and tool codes, never keys or
    headers.
- Floor: the 12 model-drafted invoice cases from the 2026-09-30 phase-12 build
  (`examples/live/phase12-build.md`, licensed there on Claude), unchanged.
  - The floor is SYNTHETIC: 6 tuning and 6 held-out cases.
  - Smoke is the tuning partition, which is what build mode derives.
  - The spec is that build's, with `model_target` set to Lightning.
- Profile: the role pack in `examples/nebius/profile.json` (Lightning runs, Super
  infers), with these changes:
  - ladder `[Lightning]`, so there is one rung and Forge runs once;
  - compile budget $0.25 and 1,500 s;
  - Forge in agent mode, trigger `always`, 1,500 s, and `pi_max_turns` 30 as a probe guard
    (default 200; never reached).
- Workspaces: fresh at `C:/t/fp1` and `C:/t/fp2`, each with its own signing key.

| | Session 1 | Session 2 |
|---|---|---|
| Engineer | `nvidia/nemotron-3-super-120b-a12b` | `nvidia/Nemotron-3-Ultra-550b-a55b` |
| Settings | thinking off; temperature unset (request default 0.0) | thinking on; temperature 1.0; top_p 0.95 |
| `max_tokens` sent per agent call | 16,384 (price-table capacity) | 32,768 (price-table capacity) |
| Forge cap | $0.50 | $0.60 |
| Run (UTC) | 16:58:52 to 17:00:38 | 17:02:33 to 17:05:44 |

## Results

| | Session 1 (Super) | Session 2 (Ultra) |
|---|---|---|
| Wall time, whole compile / Forge | 106 s / 91 s | 191 s / 175 s |
| Model calls = Pi turns + repairs | 23 = 22 + 1 | 5 = 3 + 2 |
| Replies that parsed as an envelope | 22 of 23 (95.7%); 2 of them only after stripping trailing text | 2 of 5 (40%) |
| `REASONING_CUT` | 0 | 0 |
| Replies cut at `max_tokens` | 0 | 2 (32,768 tokens each) |
| Tools executed | read 6 (1 denied), write 7, forge_check 6, forge_dryrun 2 | read 6 (2 denied) |
| forge_measure / forge_submit / forge_spawn | 0 / 0 / 0 | 0 / 0 / 0 |
| Valid v2 code | none: 7 writes (3 distinct files), all rejected by 5 checks and 1 dry run | none: no draft reached the workspace |
| Forge outcome | `forge_no_submission` | `forge_no_submission` |
| Stage 4 | The Stage 2 preset (extractor on Lightning) was licensed: 6 of 6 held-out, 1.00 against 0.95. SYNTHETIC floor, mechanics only | Same |
| Agent spend (list price) | $0.0959 | $0.2311 |
| Lightning spend (18 calls, Stages 1, 2 and 4) | $0.0005 | $0.0005 |
| Session total | $0.0963 | $0.2316 |
| Agent $ per model call | $0.0042 | $0.0462 ($0.0075 without the two cut calls) |
| Prompt tokens per call, mean / max | 11,524 / 24,182 | 4,866 / 5,980 |
| Cache-hit tokens (share of prompt) | 0 (0%) | 13,056 (53.7%) |
| Completion tokens per call, mean / max | 791 / 2,209 | 13,785 / 32,768 |
| Reasoning tokens (share of completion) | 0 | 1,589 (2.3%) |

Architect's own metering agreed with the list-price sums ($0.0958797 and $0.231097).

Its telemetry keeps one `forge_agent` row per session: tokens in and out, cached tokens,
model calls and $. It has no per-turn row and no reasoning count, so the per-turn
figures here come from the probe driver.

### Session 1 turns (Super)

| Turn | Context chars | Prompt tok | Out tok | s | $ | Reply → result |
|---|---|---|---|---|---|---|
| 1 to 6 | 2,293 to 9,946 | 2,346 to 5,008 | 48 to 50 | 1.1 to 1.5 | 0.0071 total | read SPEC.md, DESIGN.md, TUNING.md, BASELINE.json, `templates/extractor.py` (denied: no such file), harness.py |
| 7 | 14,166 | 6,204 | 49 | 1.2 | 0.0019 | forge_check on the incumbent → ok |
| 8 | 14,836 | 6,464 | 65 | 1.2 | 0.0020 | forge_dryrun case 0 → `sandbox_worker_failed` |
| 9 | 15,884 | 6,873 | 2,073 | 8.8 | 0.0039 | write harness.py, 6,663 bytes |
| 10 | 23,197 | 9,033 | 50 | 1.2 | 0.0028 | forge_check → `sandbox_import`, line 3 |
| 11 | 23,939 | 9,311 | 2,135 | 8.2 | 0.0047 | write, 6,744 bytes (envelope recovered by extraction) |
| 12 | 31,329 | 11,497 | 50 | 1.3 | 0.0035 | forge_check → `sandbox_import`, line 3 |
| 13 | 32,070 | 11,774 | 2,180 | 8.3 | 0.0055 | not JSON (7,383 chars) → repair |
| 13, repair | 32,070 | 12,632 | 2,209 | 7.8 | 0.0058 | write, 6,722 bytes (extraction) |
| 14 to 21 | 39,509 to 64,826 | 14,034 to 21,922 | 50 to 66 or 2,207 | 1.5 to 9.3 | 0.0514 total | the same 6,722-byte file written 4 more times; forge_check → `sandbox_helper`, line 9, three times; forge_dryrun → `forge_invalid_source`, line 9 |
| 22 | 72,265 | 24,182 | 37 | 2.0 | 0.0073 | final text → session ends |

### Session 2 turns (Ultra)

| Turn | Context chars | Prompt tok (cached) | Out tok (reasoning) | s | $ | Reply → result |
|---|---|---|---|---|---|---|
| 1 | 2,293 | 2,348 (0) | 187 (79) | 1.2 | 0.0029 | read ×5: SPEC.md, DESIGN.md, TUNING.md, BASELINE.json, `templates/` (denied: a directory) |
| 2 | 9,790 | 4,963 (0) | 129 (103) | 3.5 | 0.0053 | read `templates/` (denied) |
| 3 | 10,076 | 5,057 (4,352) | 3,071 (759) | 9.5 | 0.0143 | not an envelope (5,912 chars) → repair |
| 3, repair 1 | 10,076 | 5,980 (4,352) | 32,768 (335) | 71.2 | 0.1043 | `length`, not JSON (82,946 chars) → repair |
| 3, repair 2 | 10,076 | 5,980 (4,352) | 32,768 (313) | 89.1 | 0.1043 | `length`, not JSON (83,800 chars) → taken as final text; session ends |

## Failure modes, with the exact text

1. **Code inside the envelope breaks the JSON.** Both models put a multi-KB source file in a
   JSON string inside a reply that must itself be one JSON object.
   - Super: 3 of its 8 write replies were malformed.
     - One was not JSON. Its head and tail were a well-formed write envelope; the decoder
       error was not captured in this session.
     - Two carried text after the closing brace, which `_control_reply` recovered.
     - Every short reply (reads, checks, dry runs) parsed whole.
   - Ultra: all 3 write replies failed:
     - `JSONDecodeError: Invalid \escape: line 1 column 2259 (char 2258)`: a regex written
       as `[:\$]`.
     - `Invalid \escape: line 1 column 2191 (char 2190)`.
     - `Expecting ',' delimiter: line 1 column 4685 (char 4684)`.
2. **Ultra drifted to the transcript's keys.** Its write replies used the keys of the
   tool-result messages shown in `pi_context`: `{"tool_calls": [{"toolCallId": "call-7",
   "toolName": "write", "arguments": {...}}], "toolCallId": "call-7", "toolName": "write",
   "isError": false}`. The envelope needs `id` and `name`. Turns 1 and 2 had used the right
   keys.
3. **Repairs after a cut ran away.** Both re-asks ran to `max_tokens` 32,768 (71 s and 89 s,
   $0.104 each).
   - The first filled the file with generated test cases (about 80,000 characters) until the
     cap.
   - The second copied the repair request's own `previous_reply` field into its output and
     nested it (`...]}]}]}", "isError": false}]}]}]}`).
   - Two failed repairs make the 83,800-character reply Pi's final answer.
4. **The language reference and templates were never read.** The loop in
   `FORGE_INSTRUCTIONS` lists SPEC.md, DESIGN.md, TUNING.md, BASELINE.json and "the files in
   templates/", but not LANGUAGE.md, and nothing names the template files.
   - Neither agent opened LANGUAGE.md.
   - Super guessed `templates/extractor.py`, after "bound preset: extractor" in SPEC.md.
     Ultra read `templates/`.
   - Forge has no listing tool, and `read` of a missing file or a directory returns `Tool
     denied by Architect policy`, which reads like a rule.
   - Both then wrote ordinary Python: `import re`, `import json`, `from typing import Dict,
     Any`. forge_check returned `sandbox_import` ("Only the 4 fixed from-imports are
     allowed; no other import.") and `sandbox_helper` ("Helper: def name(a, b) or async def
     name(context, b); at most 8 plain parameters; sync helpers never await or use
     context.").
   - Ultra also wrote to `extractor.py`, not harness.py.
5. **At temperature 0, Super repeated itself.** It wrote the identical 6,722-byte file 5
   times: same nodes, same `sandbox_helper` error at line 9.
6. **A progress note became the final answer.** Super ended with `{"text": "I'm still
   encountering issues with the sandbox helper constraint. Let me simplify the harness
   further to comply with the restrictions while maintaining the core functionality."}`.
   - Five submissions were left.
   - `repair_prose` re-asks only non-envelope prose, so this ended the session.
7. **The default dry-run reply makes the incumbent look broken.** `forge_dryrun` on the
   instantiated `extract_verify` incumbent returned `sandbox_worker_failed`.
   - Reproduced offline at $0: the default scripted reply `["{}"]` gives
     `sandbox_worker_failed`, and a schema-shaped reply gives ok with `schema_ok` true.
   - This affects any engineer model.
8. **Whole-file rewrites inflate every later prompt.** Each `write` keeps the full file in
   the transcript. Super's context grew from 2.3k to 72k characters (2.3k to 24k prompt
   tokens) in 22 turns.
   - All 43 messages were still in the context at turn 22, above
     `context_policy.max_characters` (60,000). Nothing thins it yet, as W3 expected.
   - Super's prompt cache never hit (0 on all 23 calls, from both `prompt_cache_hit_tokens`
     and `prompt_tokens_details.cached_tokens`), although the prefix is byte-stable.
     Ultra's hit 4,352 tokens from its third call on.
   - Architect prices cached tokens at the full input rate, since the price table has no
     cache rate, so the $ figures here include them in full.

## Envelope against a native tool call (16 calls, $0.0356)

One composition task, sent in both forms: write a regex-heavy `harness.py` (with `\d`,
`\s`, `\$`, both quote styles and a docstring) in one `write` call.

- Envelope arm: the exact Pi request shape, built with `_PROTOCOL_INSTRUCTION` and
  `_REMINDER`, and parsed with the real `_control_reply`.
- Native arm: the same text as a chat with an OpenAI `tools` entry.
- Both arms used the session settings above, `max_tokens` 8,192, and 4 repeats per cell.
- Usable means the call parsed, named `write`, and its `content` compiled as Python.

| | Envelope: usable writes | Native: usable writes |
|---|---|---|
| Ultra (thinking on, 1.0 / 0.95) | 2 of 4. One was `Expecting ',' delimiter`; one was a `{"text": ...}` with no write, which would end a Pi session | 4 of 4 |
| Super (thinking off, 0.0) | 0 of 4: `Expecting value: line 8 column 20 (char 141)`. The content value opened with `\"\"\"` instead of a quoted string | 4 of 4 |

- Cost and latency were similar in both arms: 2.1 to 4.1 s and $0.0006 to $0.0051 per call.
  No reply was cut.
- Limits:
  - This is a single turn.
  - At temperature 0 Super's four repeats are nearly one sample.
  - A native write still needs JSON-escaped arguments. The difference is that the model
    writes them in the tool-call format it was trained on.

## G1a against the plan's criteria

G1a in the plan is defined for native sessions; it is applied to these envelope probes
here.

| Criterion | Session 1 (Super) | Session 2 (Ultra) |
|---|---|---|
| Session finishes | Yes, by its own premature final text | Yes, after two failed repairs |
| At least 95% of turns parse | 22 of 23 replies (95.7%): a borderline pass. Write replies alone: 5 of 8 clean | 2 of 5 (40%): **fail** |
| Submissions are valid v2 code | None submitted; no draft passed forge_check: **fail** | None written: **fail** |
| No `REASONING_CUT` | Pass (thinking off) | Pass, but 2 answer cuts at 32,768 tokens |
| $ per turn logged | Only by the probe driver; Architect logs per session: **open** (W3) | Same |
| Ultra: at most $2 and 25 min | Not applicable | $0.23 and 3.2 min, because it died early |

## Recommendation

**Native tool calls are needed (P3). Do not proceed on the envelope for Ultra.**

- Ultra's failures are in the transport itself:
  - escaping code inside a JSON string inside a JSON request;
  - drift to the transcript's key names;
  - a repair loop that echoes the bad reply.
- Super passes the parse bar only narrowly, and fails the micro-probe 0 of 4.
- The micro-probe gave native 8 of 8 against envelope 2 of 8.

P3 alone will not give a valid submission. These Forge pack and settings changes are
needed with either transport. Some change the Forge pack hash, so they must land before
pre-registration on 10/11:

1. **Bound the agent's output.**
   - Send a Forge-agent `max_tokens` well under the price capacity (for example 8,192 to
     12,288; the longest reply here that did not run away was 3,071 tokens, reasoning
     included).
   - Do not re-ask a `length`-cut reply with the reply echoed back. Stop, or ask once for a
     smaller step.
   - The two cut repairs were 90% of session 2's cost.
2. **Make the language and templates findable.**
   - Put LANGUAGE.md first in the `FORGE_INSTRUCTIONS` loop.
   - Name the template files (`templates/extract_verify.py`, `prompt_chain.py`, and so on)
     in SPEC.md or the instructions.
   - Make `read` report "not found" or "is a directory" rather than `Tool denied by
     Architect policy`, or add a listing tool.
3. **Dry runs:** default the scripted replies to the output schema's shape, or report the
   preset's own failure. Today the incumbent dry run returns `sandbox_worker_failed`.
4. **A final text before any submission:** re-ask once while submissions remain. This would
   have caught session 1's progress note.
5. **Context:** prefer `edit` to whole-file `write`, and have the planned context cap thin
   old `write` arguments as well as tool results.
6. **Per-turn telemetry:** prompt, cache-hit and reasoning tokens for each call. Nebius
   reports `prompt_cache_hit_tokens` and `prompt_tokens_details.cached_tokens` on Ultra and
   Super, but only `prompt_cache_hit_tokens` on Lightning.

Then run G1a as planned on native sessions with these settings. Treat a mechanics miss
there as F1 or F2.

## Spend and call ids

- Session 1: $0.0963. Session 2: $0.2316. Micro-probe: $0.0356. **Total: $0.3635** at
  10/3 list prices: Ultra $1.00/$3.00, Super $0.30/$0.90 and Lightning $0.06/$0.24 per
  million tokens.
- No 429, 503 or 529 responses, no retries and no HTTP errors.
- Ultra's two cut replies (for Nebius): `chatcmpl-9577e25aec78dae8` and
  `chatcmpl-852abf5361a30fee`.
- Session 2's first unparsed write: `chatcmpl-ac5e0d4460187fc7`.
- Session 1's unparsed write: `chatcmpl-9f1b25bf2e94b87a`.
