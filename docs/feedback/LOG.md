# Feedback log: Nebius Token Factory, Nebius AI Cloud and NVIDIA Nemotron

The evidence behind my hackathon feedback. [README.md](README.md) answers the organizers'
feedback questions for each tool and ranks the top issues; this file holds every
finding with a repro. Last updated 2026-10-13.

## How entries are written

- An entry goes in only if I reproduced it against the live service, or if an official
  Nebius or NVIDIA source states it: a docs page, the OpenAPI spec, the public catalog, a
  published package or a model card. Each entry says which. Reports from other teams stay
  out until I reproduce them.
- Live repros give the exact request. The API key is always `$NEBIUS_API_KEY` and the
  project ID `<project-id>`; neither appears anywhere in this repository.
- Counts are given as "k of n". A probe is one call unless a count is given. Probes
  describe API behavior on the day; they are not benchmark results.
- `x-request-id` values are included so Nebius can find my calls in its logs.
- Severity:
  - High: a wrong or empty answer that looks like success, a first-run failure, or
    spend that cannot be bounded.
  - Medium: needs a workaround, or the docs lead you into a failure.
  - Low: friction or inconsistency with a cheap workaround, or a feature request.
- Product: Token Factory inference, billing, Sandboxes, docs (docs site, API reference,
  catalog, cookbook), onboarding email, post-training, Nemotron model, AI Cloud, NVIDIA
  tooling.

## Index

| ID | Severity | Product | Finding |
|---|---|---|---|
| [FB-01](#fb-01) | High | Inference, Nemotron model | A thinking reply cut at `max_tokens` returns its reasoning as the answer (Ultra, Lightning) |
| [FB-02](#fb-02) | High | Docs | The Quickstart's first request fails: its model no longer exists |
| [FB-03](#fb-03) | High | Billing | No hard spending cap per project or API key |
| [FB-04](#fb-04) | Medium | Inference, Nemotron model | `reasoning_effort: "none"` leaves Nano's `content` null and puts the answer in `reasoning` |
| [FB-05](#fb-05) | Medium | Docs, inference | The reasoning switch that works is undocumented; `reasoning_effort` is only partly honored |
| [FB-06](#fb-06) | Medium | Inference | Reasoning arrives in different fields per model; the response schema documents neither |
| [FB-07](#fb-07) | Medium | Nemotron model, inference | Lightning with strict `json_schema` and thinking on returned reasoning text instead of JSON (2 of 3 runs) |
| [FB-08](#fb-08) | Medium | Docs | `response_format` says `json_schema` is unsupported; it works |
| [FB-09](#fb-09) | Medium | Inference | Super and Ultra send no rate-limit headers |
| [FB-10](#fb-10) | Medium | Inference, billing | `usage` differs per model; cache fields are undocumented and unpriced |
| [FB-11](#fb-11) | Medium | Inference, docs | Model retirement: 7-day notice, no machine-readable signal, a stale list, mixed error codes |
| [FB-12](#fb-12) | Medium | Billing | Trial end and "Switch to paid usage" are missing from the billing docs; the terms say pre-paid |
| [FB-13](#fb-13) | Medium | Sandboxes | Beta access has no timeline and no request status |
| [FB-14](#fb-14) | Medium | Docs | Cookbook Nemotron pages: wrong-case Nano ID (404), region-pinned host, removed models, broken links |
| [FB-15](#fb-15) | Medium | Docs | Catalog metadata conflicts: region, license, Super's policy and model-card links |
| [FB-16](#fb-16) | Medium | Docs | Dead links on the product page and fine-tuning guide; batch inference advertised but undocumented |
| [FB-17](#fb-17) | Low | Inference | `service_tier` is always null |
| [FB-18](#fb-18) | Low | Nemotron model | Super with thinking off writes a planning sentence into `content` before tool calls |
| [FB-19](#fb-19) | Low | Sandboxes | Docs and SDK disagree on limits and on the project variable |
| [FB-20](#fb-20) | Low | Inference | No `seed` parameter |
| [FB-21](#fb-21) | Low | Post-training | No Nemotron model can be fine-tuned |
| [FB-22](#fb-22) | Low | Docs (catalog) | No NVIDIA safety model is served |
| [FB-23](#fb-23) | Low | AI Cloud | The AI Cloud CLI has no Windows build |
| [FB-24](#fb-24) | Low | NVIDIA tooling | OpenShell on Windows is experimental |
| [FB-25](#fb-25) | Low | Onboarding email | The welcome email came 56 hours after sign-up, and its list of models leaves out Nemotron |
| [FB-26](#fb-26) | Medium | Inference | Temperature 0 is not repeatable: the same agents scored up to 3 apart on the same 100-receipt exam |
| [FB-27](#fb-27) | Medium | Nemotron model | Told that an unprinted amount is null, Ultra fills in a subtotal about as often as Lightning |

## Entries

<a id="fb-01"></a>
### FB-01. A thinking reply cut at `max_tokens` returns its reasoning as the answer

- **Logged:** 2026-10-03. First seen in my live check that morning; reproduced 16:09 UTC.
  Seen again on Lightning on 2026-10-06: 50 of 50 receipt calls with thinking on
  ([FB-05](#fb-05)).
- **Product:** Token Factory inference; Nemotron model (Ultra, Lightning).
- **Severity:** High.
- **Verified by:** me, live.
- **Repro** (Ultra; swap the model ID for the others):

```bash
curl -s https://api.tokenfactory.nebius.com/v1/chat/completions \
  -H "Authorization: Bearer $NEBIUS_API_KEY" -H "Content-Type: application/json" \
  --data-binary @- <<'EOF'
{"model": "nvidia/Nemotron-3-Ultra-550b-a55b", "temperature": 0, "max_tokens": 64,
 "chat_template_kwargs": {"enable_thinking": true},
 "messages": [{"role": "user", "content": "How many prime numbers are there between 100 and 200? Think it through."}]}
EOF
```

- **Observed** (16:09 UTC, `x-request-id` 6ed2ffda9ce7f2e88ef4cbd6e1177fb8, trimmed):

```json
{"finish_reason": "length",
 "message": {"role": "assistant",
   "content": "The user wants to know the count of prime numbers between 100 and 200.\nI need to list the primes in that range and count them.\nRange: 101 to ..."},
 "usage": {"completion_tokens": 64, "completion_tokens_details": {"reasoning_tokens": 64}}}
```

| Model | `content` | Reasoning fields | `reasoning_tokens` | `x-request-id` |
|---|---|---|---|---|
| Ultra | the reasoning text | none | 64 of 64 | 6ed2ffda9ce7f2e88ef4cbd6e1177fb8 |
| Lightning | the reasoning text ("Here's a thinking process: …") | none | 64 of 64 | 4cede01413c310b5e961357a11e2b3ce |
| Super | `null` | `reasoning` and `reasoning_content` | 64 of 64 | ea918c8e51271eb2b85d418c2086b5b2 |
| Nano | `null` | `reasoning` | not reported | c1b2e34da7c5a75d8beb2e7005f2a01b |

  Streaming (add `"stream": true, "stream_options": {"include_usage": true}`) is worse for
  a chat UI. Ultra streams the reasoning as `reasoning_content` deltas, then its final
  chunk (`finish_reason: "length"`) sends the whole reasoning again as one 199-character
  `content` delta (`x-request-id` e5de23c3d139e86f8bff62a6b0dd2bdf). Lightning does the
  same with one 259-character `content` delta (366d35ee05f18dcdcc07365ab5af15ac). A client
  that renders `content` shows the model's private reasoning as its reply.
- **Expected:** `content` empty or null and the reasoning left in the reasoning field, as
  Super and Nano already do, with `finish_reason: "length"` as now. In a stream, no
  `content` delta that repeats the reasoning.
- **Why it matters:** `max_tokens` defaults to 8192 when omitted (OpenAPI version
  20260930-cfb76be12), and reasoning counts against it. The `max_tokens` description does
  not say so; only `max_completion_tokens` mentions reasoning. On Ultra and Lightning, any
  trace longer than the cap ends this way, with HTTP 200, and a client that reads
  `content` without checking `finish_reason` returns reasoning as the answer. Lightning
  spent 600 reasoning tokens on a two-field extraction in [FB-07](#fb-07), and its whole
  512-token cap on every receipt it read with thinking on in [FB-05](#fb-05), so this is
  not limited to hard prompts.
- **Likely cause** (my inference, not confirmed): with thinking on, the Nemotron 3 chat
  templates open the think block inside the prompt, so a reply cut before the closing tag
  has no marker at all, and the reasoning parser used for Ultra and Lightning falls back to
  treating the text as content.
- **Suggested fix:** treat an unterminated think block as reasoning on every model; never
  re-emit reasoning as a `content` delta; say in the `max_tokens` docs that reasoning
  counts against it. NVIDIA's model cards describe a `reasoning_budget` that closes the
  trace before a ceiling; something like it in the API would let callers bound reasoning
  apart from the answer.
- **My workaround:** Architect always sends `max_tokens` and treats `finish_reason:
  "length"` with thinking on as a failure (`REASONING_CUT` in `architect/models/http.py`).
  It never shows text from such a reply.

<a id="fb-02"></a>
### FB-02. The Quickstart's first request fails: its model no longer exists

- **Logged:** 2026-10-03, checked live 16:12 UTC.
- **Product:** docs.
- **Severity:** High. It is the first request a new user sends.
- **Verified by:** me, live, against the official Quickstart.
- **Repro:**

```bash
curl -s https://docs.tokenfactory.nebius.com/quickstart.md | grep -n 'DeepSeek'
# the Python, JavaScript and curl samples all use deepseek-ai/DeepSeek-R1-0528
curl -s https://api.tokenfactory.nebius.com/v1/chat/completions \
  -H "Authorization: Bearer $NEBIUS_API_KEY" -H "Content-Type: application/json" \
  --data-binary @- <<'EOF'
{"model": "deepseek-ai/DeepSeek-R1-0528", "max_tokens": 1,
 "messages": [{"role": "system", "content": "You are a helpful assistant"},
              {"role": "user", "content": [{"type": "text", "text": "hello"}]}]}
EOF
```

  (The Quickstart's curl sample has no `max_tokens`; I added it.)
- **Observed:** HTTP 404, ``{"detail":"The model `deepseek-ai/DeepSeek-R1-0528` does not exist."}``.
  The same holds for other entry pages:

| Page | Model in the sample | Live result |
|---|---|---|
| Quickstart (all three samples) | `deepseek-ai/DeepSeek-R1-0528` | 404, "does not exist" |
| API reference, Introduction | `meta-llama/Meta-Llama-3.1-70B-Instruct` | 404, "does not exist" |
| Switch to Token Factory guide | `moonshotai/Kimi-K2.5` | 404, "does not exist" |
| OpenAPI, 200 example for chat completions | `meta-llama/Llama-3.3-70B-Instruct` | 403, "You do not have access to model" |

  Across the docs' full-text export (`llms-full.txt`), 12 of the 15 distinct model IDs
  used as a `model` value in code samples (requests and example responses) are not in
  today's catalog (25 models in `models_info`).
- **Expected:** the first sample runs as pasted.
- **Suggested fix:** point the samples at a current model, and add a docs check that
  every model ID in a code sample is in `models_info`. The cookbook repo already runs a
  link checker (`.github/scripts/check_internal_links.py`) that could carry the same test.
- **My workaround:** I take model IDs from `GET /v1/models?verbose=true` or the public
  `models_info`, never from samples.

<a id="fb-03"></a>
### FB-03. No hard spending cap per project or API key

- **Logged:** 2026-10-03. Console readings added 2026-10-05.
- **Product:** billing.
- **Severity:** High for anyone who shares a key with testers, judges or an autonomous
  agent.
- **Verified by:** official docs and OpenAPI; my console.
- **Repro:**

```bash
curl -s https://docs.nebius.com/signup-billing/budgets.md | grep -n "does not stop"
curl -s https://docs.tokenfactory.nebius.com/llms-full.txt \
  | grep -n -i -E "spend(ing)? limit|spending cap|hard cap"   # no matches
```

- **Observed:**
  - The Nebius budgets page: "A budget does not stop or cap your usage. Budget usage data
    updates every few hours, so an alert can arrive after your spending has passed an
    alert threshold."
  - My console lagged too. Read at 12:13 UTC on 2026-10-05, the usage page said "Last
    updated: 09:49, UTC". The transactions list showed October's usage so far as one
    "Consumption" line dated 31 Oct, 23:59. By my records, usage passed the trial's $1 at
    about 19:50 UTC on 2026-10-04, but the trial closed only at 22:59, with a credit of
    $5.49 labeled "Trial" ([FB-12](#fb-12)).
  - The Token Factory billing page says the card is charged automatically at the start of
    the month if the balance is negative, or "when the configured billing threshold is
    reached".
  - Nothing in the Token Factory docs sets a hard limit per project or per key.
  - The gateway already has the stop I want: the OpenAPI lists a 402 with "Payment
    Required: You have exhausted your budget. Please add funds to continue using the API."
- **Expected:** a hard cap per project and per API key (total and per day) that returns
  that 402, plus per-key model allowlists and a `max_tokens` ceiling, so a key can be
  handed out safely.
- **Impact on me:** judges must be able to use my app free of charge until 2026-12-15.
  Handing them my key would leave spend unbounded, so I built a relay with its own
  ledger (a reservation per call, per-code and global caps, an `nvidia/*` allowlist, a
  forced `max_tokens`) only to bound spend. Any team with a hosted demo has the same
  problem.
- **Suggested fix:** per-key and per-project spend caps enforced at the gateway through
  the existing 402, plus per-key model and `max_tokens` limits.

<a id="fb-04"></a>
### FB-04. `reasoning_effort: "none"` leaves Nano's `content` null and puts the answer in `reasoning`

- **Logged:** 2026-10-03. Seen in my morning probe; reproduced 16:09 and 16:11 UTC
  (3 of 3).
- **Product:** Token Factory inference; Nemotron model (Nano).
- **Severity:** Medium.
- **Verified by:** me, live.
- **Repro:**

```bash
curl -s https://api.tokenfactory.nebius.com/v1/chat/completions \
  -H "Authorization: Bearer $NEBIUS_API_KEY" -H "Content-Type: application/json" \
  --data-binary @- <<'EOF'
{"model": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B", "max_tokens": 300, "reasoning_effort": "none",
 "messages": [{"role": "user", "content": "Reply with exactly: OK"}]}
EOF
```

- **Observed** (16:09 UTC, `x-request-id` 5e433d1d867a6786c88fbadb39ccdb00):
  `{"finish_reason": "stop", "message": {"content": null, "reasoning": "OK"}, "usage":
  {"completion_tokens": 2}}`. Thinking is off (2 tokens), but the answer is in
  `reasoning`. A sentiment prompt at 16:11 UTC gave `content: null`, `reasoning:
  "negative"`. Lightning, Super and Ultra put the answer in `content` for the same
  parameter.
- **Expected:** `content: "OK"`, which is what `chat_template_kwargs: {"enable_thinking":
  false}` returns on Nano.
- **Suggested fix:** route `reasoning_effort: "none"` through the same template switch as
  `enable_thinking: false`, or fix Nano's reasoning parser for replies with no reasoning.
- **My workaround:** Architect never sends `reasoning_effort`; it sends
  `chat_template_kwargs.enable_thinking`.

<a id="fb-05"></a>
### FB-05. The reasoning switch that works is undocumented; `reasoning_effort` is only partly honored

- **Logged:** 2026-10-03, checked live 16:11 UTC (one call per cell, temperature 0).
  The `/no_think` result is from my morning probe. Lightning re-checked on 2026-10-06,
  10 calls per setting.
- **Product:** docs (API reference); Token Factory inference.
- **Severity:** Medium.
- **Verified by:** me, live; official OpenAPI and NVIDIA model cards.
- **Repro** (one field varied per call; Super and `"low"` shown):

```bash
curl -s https://api.tokenfactory.nebius.com/v1/chat/completions \
  -H "Authorization: Bearer $NEBIUS_API_KEY" -H "Content-Type: application/json" \
  --data-binary @- <<'EOF'
{"model": "nvidia/nemotron-3-super-120b-a12b", "temperature": 0, "max_tokens": 600,
 "reasoning_effort": "low",
 "messages": [{"role": "user", "content": "Classify the sentiment of 'the battery died after a day' as positive or negative. One word."}]}
EOF
```

- **Observed:** completion tokens, with reasoning tokens in brackets. "n/a" means Nano
  reports no reasoning count ([FB-10](#fb-10)).

| Request adds | Nano | Lightning | Super | Ultra |
|---|---|---|---|---|
| nothing (thinking is on by default) | 79 (n/a) | 225 (222) | 41 (37) | 41 (38) |
| `"reasoning_effort": "low"` | 77 (n/a) | 225 (222) | 10 (6) | 41 (38) |
| `"reasoning_effort": "high"` | 77 (n/a) | 223 (220) | 27 (23) | 41 (38) |
| `"reasoning_effort": "none"` | 2, answer in `reasoning` ([FB-04](#fb-04)) | 2 (0) | 2 (0) | 2 (0) |
| `"chat_template_kwargs": {"enable_thinking": false}` | 2 | 2 (0) | 2 (0) | 2 (0) |
| `"chat_template_kwargs": {"enable_thinking": true, "low_effort": true}` | | | 10 (6) | |
| `"chat_template_kwargs": {"enable_thinking": true, "medium_effort": true}` | | | | 54 (51) |

  - `"low"` and `"high"` change nothing on Nano, Lightning or Ultra. On Super, `"high"`
    reasons less than the default.
  - A `/no_think` system prompt did not turn reasoning off on any of the four models.
  - The OpenAPI lists `reasoning_effort` as an enum (`none`, `minimal`, `low`, `medium`,
    `high`, `xhigh`, `max`) with no description, and does not mention
    `chat_template_kwargs`. Yet `enable_thinking` is the switch NVIDIA's model cards give
    for all four models, and Token Factory forwards it, along with `low_effort` (Super)
    and `medium_effort` (Ultra).
  - The cards also tell coding agents to send `force_nonempty_content: true` (Super,
    Ultra, Lightning); the Ultra card says that with `tools` you "must" send it with
    `enable_thinking: true`. Token Factory's docs don't say whether that applies here. In
    my one Ultra tool call with thinking on, the call parsed without it.
  - Lightning again on 2026-10-06, 17:06 to 17:08 UTC, on a real task: Architect's own
    request to read a receipt (the body in [FB-26](#fb-26) with Lightning's ID and without
    `chat_template_kwargs`, 765 to 1,042 prompt tokens), sent for the first 10 rehearsal
    receipts (cord-train-0013 to cord-train-0066 in
    `examples/receipts/cases/rehearsal.jsonl`). Each row changes only the thinking
    control. The reply `id` is the first receipt's.

| Request adds | `finish_reason` | Completion tokens (reasoning) | JSON answer | Reply `id` |
|---|---|---|---|---|
| `"chat_template_kwargs": {"enable_thinking": false}` | `stop`, 10 of 10 | 39 to 48 (0) | 10 of 10 | chatcmpl-bbc90916 |
| `"reasoning_effort": "none"` | `stop`, 10 of 10 | 40 to 48 (0) | 10 of 10 | chatcmpl-664d598a |
| nothing | `length`, 10 of 10 | 512 (512) | 0 of 10 | chatcmpl-37dc3afe |
| `"reasoning_effort": "minimal"` | `length`, 10 of 10 | 512 (512) | 0 of 10 | chatcmpl-5bb669c1 |
| `"reasoning_effort": "low"` | `length`, 10 of 10 | 512 (512) | 0 of 10 | chatcmpl-0592e9f9 |
| `"reasoning_effort": "medium"` | `length`, 10 of 10 | 512 (512) | 0 of 10 | chatcmpl-030f603f |
| `"reasoning_effort": "high"` | `length`, 10 of 10 | 512 (512) | 0 of 10 | chatcmpl-46519cc0 |

  Lightning thinks unless the request turns thinking off, and only two settings do that.
  With no setting, or with `"minimal"`, `"low"`, `"medium"` or `"high"`, it spent the
  whole cap reasoning on every receipt and returned the reasoning as `content` ("Here's a
  thinking process: …") with no reasoning field, which is [FB-01](#fb-01). A developer
  who picks `"minimal"` or `"low"` from the API reference to save tokens gets no answer at
  this cap.
- **Expected:** one documented way to control reasoning for each model, and unsupported
  values rejected with a 400 instead of ignored.
- **Suggested fix:** document `chat_template_kwargs` and which keys each model honors;
  map `reasoning_effort` onto them (`none` to `enable_thinking: false`, `low` to
  `low_effort`, and so on) or return 400 where no mapping exists; state whether
  `force_nonempty_content` is needed on Token Factory.
- **My workaround:** `chat_template_kwargs.enable_thinking` set per role.

<a id="fb-06"></a>
### FB-06. Reasoning arrives in different fields per model; the response schema documents neither

- **Logged:** 2026-10-03 (captures at 07:19 UTC; seen again 16:09 UTC).
- **Product:** Token Factory inference.
- **Severity:** Medium.
- **Verified by:** me, live; official OpenAPI.
- **Repro:** once per model:

```bash
curl -s https://api.tokenfactory.nebius.com/v1/chat/completions \
  -H "Authorization: Bearer $NEBIUS_API_KEY" -H "Content-Type: application/json" \
  --data-binary @- <<'EOF'
{"model": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B", "temperature": 0, "max_tokens": 512,
 "chat_template_kwargs": {"enable_thinking": true},
 "messages": [{"role": "user", "content": "What is 17*3? Reply with just the number."}]}
EOF
```

- **Observed** (thinking on, replies that finished normally):

| Model | `reasoning` | `reasoning_content` | `content` | Also |
|---|---|---|---|---|
| Nano | yes | absent | `"\n51"` | `"tool_calls": []` instead of null |
| Lightning | yes | yes, identical text | `"51"` | |
| Super | yes | yes, identical text | `"\n\n51"` | |
| Ultra | absent | yes | `"51"` | |

  With tools and thinking off, Nano's `content` is `"\n"` beside its tool call. In the
  OpenAPI, `ChatCompletionResponseMessage` lists only `role`, `content` and `tool_calls`.
  `reasoning` and `reasoning_content` appear only on other schemas, each described as
  "The reasoning content of the message."
- **Expected:** one documented reasoning field on every model (or both, always), and
  `content` without leading newlines.
- **Suggested fix:** normalize at the gateway: always fill `reasoning_content` and keep
  `reasoning` as an alias, trim leading whitespace from `content`, return `tool_calls:
  null` when there are none, and add the fields to the response schema.
- **My workaround:** read either field, never show it, strip leading whitespace
  (`architect/models/http.py`).

<a id="fb-07"></a>
### FB-07. Lightning with strict `json_schema` and thinking on returned reasoning text instead of JSON

- **Logged:** 2026-10-03 (morning probe; 16:09 UTC). Failed in 2 of 3 runs with
  thinking on; 0 failures with thinking off.
- **Product:** Nemotron model (Lightning); Token Factory inference.
- **Severity:** Medium.
- **Verified by:** me, live.
- **Repro** (thinking is on by default):

```bash
curl -s https://api.tokenfactory.nebius.com/v1/chat/completions \
  -H "Authorization: Bearer $NEBIUS_API_KEY" -H "Content-Type: application/json" \
  --data-binary @- <<'EOF'
{"model": "nvidia/Nemotron-3_5-Lightning", "max_tokens": 600,
 "response_format": {"type": "json_schema", "json_schema": {"name": "inv", "strict": true, "schema": {
   "type": "object", "properties": {"invoice_number": {"type": "string"}, "total": {"type": "number"}},
   "required": ["invoice_number", "total"], "additionalProperties": false}}},
 "messages": [{"role": "user", "content": "Invoice INV-2291, total due $1,234.50. Extract fields."}]}
EOF
```

- **Observed:**
  - 16:09 UTC (`x-request-id` 595e4c3aa2a35dcd080f570926421644): HTTP 200,
    `finish_reason: "length"`, `reasoning_tokens` 600 of 600, no reasoning fields, and
    `content` = "Here's a thinking process:\n\n1.  **Analyze User Input:** …". Not JSON.
  - Morning run: `content` began "Here's a thinking process…". Not JSON.
  - 16:09 UTC (`x-request-id` fc545a1a4f2fb19a627d30334eca8efd): valid JSON, after 527
    reasoning tokens to extract two fields.
  - Same request plus `"chat_template_kwargs": {"enable_thinking": false}`:
    `{"invoice_number": "INV-2291", "total": 1234.50}` in 26 tokens
    (`x-request-id` d10633df3998e5bb27f1d2aadfe0e7d0). My live check also got
    schema-valid JSON from all four models with thinking off (8 of 8).

  This is [FB-01](#fb-01) in a structured-output call. Lightning reasons at length on
  simple tasks (222 reasoning tokens for a one-word sentiment answer, against 37 for Super
  and 38 for Ultra, [FB-05](#fb-05)), runs into `max_tokens`, and the cut reasoning comes
  back as `content`.
- **Expected:** with strict `json_schema`, schema-valid `content` or an error; never free
  text with HTTP 200.
- **Suggested fix:** the FB-01 fix; document that the schema constrains only the text
  after reasoning and that reasoning shares `max_tokens`; consider turning thinking off
  for `json_schema` requests on Lightning, or tell callers to.
- **My workaround:** schema calls always send `enable_thinking: false`.

<a id="fb-08"></a>
### FB-08. `response_format` says `json_schema` is unsupported; it works

- **Logged:** 2026-10-03.
- **Product:** docs (API reference).
- **Severity:** Medium.
- **Verified by:** official OpenAPI; me, live.
- **Repro:**

```bash
curl -s https://api.tokenfactory.nebius.com/openapi.json | python3 -c "import json,sys; s=json.load(sys.stdin)['components']['schemas']; print(s['ChatCompletionRequest']['properties']['response_format']['description']); print(s['app__models__chat_completion__ResponseFormat']['properties']['type']['enum'])"
```

- **Observed:** "… Only {'type': 'json_object'} or {'type': 'text' } is supported.",
  next to the enum `['text', 'json_object', 'json_schema']`. Strict `json_schema` worked
  on all four Nemotron models in my live check (8 of 8 schema-valid with thinking off;
  Nano, Super and Ultra also with thinking on).
- **Expected:** the description matches the enum and says which models support strict
  mode, with the Lightning caveat in [FB-07](#fb-07).
- **Impact:** a reader who trusts the description falls back to `json_object` plus a
  repair loop, which costs more calls and fails more often.
- **Suggested fix:** update the description, and add a short structured-output page with
  per-model notes.

<a id="fb-09"></a>
### FB-09. Super and Ultra send no rate-limit headers

- **Logged:** 2026-10-03, checked live 16:09 to 16:10 UTC.
- **Product:** Token Factory inference.
- **Severity:** Medium.
- **Verified by:** me, live; official docs.
- **Repro:**

```bash
for m in nvidia/nemotron-3-super-120b-a12b nvidia/Nemotron-3-Ultra-550b-a55b \
         nvidia/Nemotron-3_5-Lightning nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B; do
  echo "== $m"
  curl -s -D - -o /dev/null https://api.tokenfactory.nebius.com/v1/chat/completions \
    -H "Authorization: Bearer $NEBIUS_API_KEY" -H "Content-Type: application/json" \
    -d "{\"model\":\"$m\",\"max_tokens\":4,\"chat_template_kwargs\":{\"enable_thinking\":false},\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: OK\"}]}" \
    | grep -i '^x-ratelimit' || echo "(no x-ratelimit headers)"
done
```

- **Observed:** Nano and Lightning return all 11 `x-ratelimit-*` headers on every reply
  (10 of 10). Lightning shows `x-ratelimit-limit-requests: 600` and
  `x-ratelimit-limit-tokens: 400000`; Nano 100 and 800000. Super and Ultra return none
  (0 of 7 replies, one of them an Ultra stream).
- **Expected:** the same headers on every model, as the rate-limits page promises: "Your
  current rate limits are always visible in the API response headers".
- **Impact:** a client cannot pace its Super and Ultra traffic from headers. I had
  planned to pass these headers through my relay to the desktop app.
- **Suggested fix:** send the same headers from every model. (In `models_info`, Super and
  Ultra are the two models listed in us-central1; that may be related.)

<a id="fb-10"></a>
### FB-10. `usage` differs per model; cache fields are undocumented and unpriced

- **Logged:** 2026-10-03. Checked against the bill on 2026-10-05.
- **Product:** Token Factory inference; billing.
- **Severity:** Medium.
- **Verified by:** me, live and in the console; official OpenAPI and `models_info`.
- **Repro:** any request in [FB-06](#fb-06); compare the `usage` objects.
- **Observed** (`usage` with thinking on):

| Model | `completion_tokens_details.reasoning_tokens` | `prompt_tokens_details` | `prompt_cache_hit_tokens`, `prompt_cache_miss_tokens` |
|---|---|---|---|
| Nano | absent (`completion_tokens_details` is null) | null | absent |
| Lightning | yes | null | yes |
| Super | yes | `cached_tokens`, `created_cache_tokens` | yes |
| Ultra | yes | `cached_tokens` | yes |

  - Nano counts reasoning inside `completion_tokens` (49 completion tokens for the answer
    "51") but never says how many were reasoning, so reasoning cost on Nano cannot be
    measured.
  - The OpenAPI `Usage` schema documents `prompt_tokens_details.cached_tokens` and
    `completion_tokens_details.reasoning_tokens` only. `prompt_cache_hit_tokens`,
    `prompt_cache_miss_tokens` and `created_cache_tokens` are undocumented.
  - `models_info` has input and output prices only. I found no cached-input price in the
    Token Factory docs; the only cache mention is a KV-cache hit-rate metric for dedicated
    endpoints.
- **Expected:** the same `usage` shape on every model, documented cache fields, and a
  published cached-input price, or a statement that cached tokens bill at the full input
  price.
- **Impact:** my code-writing agent resends its whole transcript every turn, so the
  cache price changes my per-build cost estimate a lot. By my reconciliation against
  the console on 2026-10-05, the bill does not discount cached tokens. The usage page
  bills every input token at the list price (Ultra 8.29M input tokens for $8.29, Super
  21.34M for $6.40, Lightning 8.02M for $0.48) and has no cached-input line, while
  Ultra's Forge sessions reported 66.7% to 76.9% of their prompt tokens as cache hits
  ([probes](probes/)). Two of those sessions alone had about 2.1M cached tokens, all
  inside the 8.29M Ultra input tokens billed at $1.00 per million.
- **Suggested fix:** normalize `usage` at the gateway, document the cache fields, and add
  a cached-input price to `models_info`.

<a id="fb-11"></a>
### FB-11. Model retirement: 7-day notice, no machine-readable signal, a stale list, mixed error codes

- **Logged:** 2026-10-03, checked live 16:12 UTC.
- **Product:** Token Factory inference; docs.
- **Severity:** Medium.
- **Verified by:** official docs and `models_info`; me, live.
- **Repro:**

```bash
curl -s https://docs.tokenfactory.nebius.com/deprecation/deprecated-models.md | grep -n -A3 "Transition Period"
curl -s https://tokenfactory.nebius.com/api/public/models_info | python3 -c "import json,sys; ms=json.load(sys.stdin); print(len(ms), {m['status'] for m in ms}, sorted({k for m in ms for k in m}))"
curl -s https://api.tokenfactory.nebius.com/v1/chat/completions \
  -H "Authorization: Bearer $NEBIUS_API_KEY" -H "Content-Type: application/json" \
  --data-binary @- <<'EOF'
{"model": "meta-llama/Llama-3.3-70B-Instruct", "max_tokens": 1,
 "messages": [{"role": "system", "content": "You are a helpful assistant"},
              {"role": "user", "content": [{"type": "text", "text": "hello"}]}]}
EOF
```

- **Observed:**
  - The Deprecated Models page promises a "Transition Period (7 days usually)".
  - Its Deprecation List shows only the 2025-11-03 shutdowns. The June 2026 and August
    2026 removals are on separate notice pages.
  - `models_info`: all 25 models are `"status": "active"`, and no field carries a
    shutdown date or a replacement.
  - IDs that are no longer served fail in different ways.
    `meta-llama/Llama-3.3-70B-Instruct`, removed on 2026-08-31 per the August notice,
    returns 403 "You do not have access to model…". `deepseek-ai/DeepSeek-R1-0528`, the
    Quickstart's model ([FB-02](#fb-02)), returns 404 "does not exist".
- **Expected:** at least 30 days' notice for serverless models; `deprecation_date` and
  `replacement` fields in `models_info` and `/v1/models`; one list; one status code for a
  retired model (410 Gone, naming the replacement).
- **Impact:** judging runs until 2026-12-15. A 7-day notice in that window could break a
  submission while judges are testing it, and a client cannot tell "retired" from "no
  access". My fallback plan switches models on 404, 409 or 410; the 403 above means it
  must treat "no access" as a possible retirement too.
- **Suggested fix:** as expected, plus an email and a console banner for keys that called
  a deprecating model in the last 30 days.

<a id="fb-12"></a>
### FB-12. Trial end and "Switch to paid usage" are missing from the billing docs; the terms say pre-paid

- **Logged:** 2026-10-03. My experience added 2026-10-05.
- **Product:** billing.
- **Severity:** Medium.
- **Verified by:** official docs, terms and console code; me, in the console.
- **Repro:** read https://docs.tokenfactory.nebius.com/other-capabilities/billing-new and
  §6.1 of https://docs.nebius.com/legal/token-factory.

```bash
curl -s https://docs.tokenfactory.nebius.com/other-capabilities/billing-new.md \
  | grep -n -i -E "switch to paid|paid usage|trial|threshold"
curl -s https://docs.nebius.com/legal/token-factory.md | grep -n "6.1"
```

- **Observed:**
  - The billing page covers the $1 trial credit (valid 30 days), automatic card charges
    (at the start of the month if the balance is negative, or "when the configured
    billing threshold is reached") and promo codes. It says nothing about what happens
    when the trial credit runs out, nothing about "Switch to paid usage", and not where
    the billing threshold is set or what it is by default.
  - The console has the answers in its billing app (static.nebius.com/mfe/billing/current/,
    fetched 2026-10-03): "Select what happens when your trial ends", "Switch to paid
    usage", "Paid usage starts at {{nextHour}} (your local time)", and for the other
    choice "Your projects and API keys will stop working and you can't create new ones.
    Your card is never charged."
  - Token Factory Supplemental Terms §6.1: "Token Feature-related fees are on a pre-paid
    basis … Customer is required to deposit funds into the Customer Account". ("Token
    Feature" is presumably "Token Factory".) The billing page describes card charges after
    the balance goes negative.
- **Expected:** the billing page explains the trial-end choice, when paid usage starts,
  the order in which credits and the card are used, and the default threshold, and agrees
  with the terms. It also says what the trial pays for when usage passes $1 before the
  trial closes.
- **My experience switching to paid** (2026-10-03, 15:48 to 15:50 UTC):
  - The control is in the account menu under the trial badge. "Paid usage after trial"
    opens "Select what happens when your trial ends", with two choices: "Stop usage when
    the trial ends" ("Your card is never charged.") and, marked Recommended, "Continue
    with paid usage when the trial ends" ("We'll use your account balance first." and
    "If it goes negative, we'll charge the linked card for usage.").
  - When I saved the second choice, the console showed the error "Trial already
    switched to paid", so it was already on. The switch asked for no card details and no
    payment; the card step had come at sign-up, with a $0 authorization. I was not
    charged.
  - The trial closed on 2026-10-04 with a credit of $5.49 labeled "Trial", dated 22:59
    UTC. All 1,268 calls my relay made between 22:30 and 01:00 UTC returned 200, so the
    move to paid usage interrupted nothing.
  - My records put my usage up to 22:59 at $5.49, so the trial appears to have paid for
    all of it rather than stopping at $1.
- **Suggested fix:** add a "Trial and paid usage" section to the billing page, and align
  §6.1 with the card flow (or the flow with §6.1).

<a id="fb-13"></a>
### FB-13. Sandboxes beta access has no timeline and no request status

- **Logged:** 2026-10-03 (`whoami` at 05:47 and 16:04 UTC). Re-checked through
  2026-10-05.
- **Product:** Sandboxes.
- **Severity:** Medium.
- **Verified by:** me, live; official docs and console.
- **Repro:**

```bash
curl -s https://api.tokenfactory.nebius.com/sandboxes/v1/whoami \
  -H "Authorization: Bearer $NEBIUS_API_KEY" -H "Project: <project-id>"
```

- **Observed:** HTTP 200. All six permissions (`import`, `spawn`, `spawn_disposable`,
  `list`, `cancel`, `set_image_tag`) are `false`. Limits are filled in
  (`instance_max_concurrency` 50, `images_import_max_concurrency` 8,
  `instance_max_timeout` 3600). The console says "Sandboxes are not available yet. This
  feature is currently unavailable for your project. Join the beta to get early access."
  The docs say "Sandboxes are currently in Beta, please request access". Nothing gives a
  timeline, and nothing shows whether a request is pending. Every later `whoami`, the last
  at 10:15 UTC on 2026-10-05, returned the same six `false` values.
- **Expected:** a request status (pending, approved, declined) in the console and in
  `whoami`, and an expected wait.
- **Impact:** Sandboxes is the natural place inside Token Factory to run the code a coding
  agent writes. With access, I would try running each candidate program's exam in a
  disposable VM; without it I keep my in-process sandbox. I asked for access, and it
  hadn't been granted as of 2026-10-05.
- **Suggested fix:** show the request status; grant registered hackathon participants
  automatically, or through a promo code.

<a id="fb-14"></a>
### FB-14. Cookbook Nemotron pages: wrong-case Nano ID (404), region-pinned host, removed models, broken links

- **Logged:** 2026-10-03 (cookbook `main` at c2e6a2a, committed 2026-09-24; checked live
  16:09 UTC).
- **Product:** docs (cookbook).
- **Severity:** Medium.
- **Verified by:** me, live; the official cookbook repo (github.com/nebius/token-factory-cookbook).
- **Repro:**

```bash
curl -s https://raw.githubusercontent.com/nebius/token-factory-cookbook/main/models/nemotron/nemotron3-nano-30b.md \
  | grep -n -E "base_url|model="
curl -s https://api.tokenfactory.nebius.com/v1/chat/completions \
  -H "Authorization: Bearer $NEBIUS_API_KEY" -H "Content-Type: application/json" \
  --data-binary @- <<'EOF'
{"model": "nvidia/nvidia-nemotron-3-nano-30b-a3b", "max_tokens": 4,
 "chat_template_kwargs": {"enable_thinking": false},
 "messages": [{"role": "user", "content": "Reply with exactly: OK"}]}
EOF
```

- **Observed:**
  - `models/nemotron/nemotron3-nano-30b.md` uses `model="nvidia/nvidia-nemotron-3-nano-30b-a3b"`.
    The API answers 404 "The model `nvidia/nvidia-nemotron-3-nano-30b-a3b` does not
    exist." on both the global host and the page's us-central1 host (`x-request-id`
    abefe86aacfb5b455663784c3d3f725b and 452b9dc10f1149f1443c7d8eb63bbc16). The catalog
    ID is `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`. The 404 gives no hint that only the
    case is wrong.
  - The Nano and Ultra pages set `base_url="https://api.tokenfactory.us-central1.nebius.com/v1/"`,
    and the Ultra page calls it the "Recommended endpoint". The docs' Public Serverless page
    warns that a base URL with a region in it "can stop working if the endpoint's
    processing region changes". It works today.
  - `models/nemotron/README.md` offers "Try it @ TF" links for Nemotron-3-Nano-Omni and
    Llama-3.1-Nemotron-Ultra-253B-v1. Both were removed on 2026-08-31 per the August
    notice.
  - The model-card links https://huggingface.co/nvidia/nvidia-nemotron-3-nano-30b-a3b and
    https://huggingface.co/nvidia/nemotron-3-nano-omni return 401 (no such public repo).
- **Expected:** cookbook samples run as written.
- **Suggested fix:** use catalog IDs and the global host; archive pages for removed
  models; add a model-ID check to the cookbook's link-check workflow. In the API, either
  match model IDs case-insensitively or answer such a 404 with "did you mean
  `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`?".

<a id="fb-15"></a>
### FB-15. Catalog metadata conflicts: region, license, Super's policy and model-card links

- **Logged:** 2026-10-03.
- **Product:** docs (catalog).
- **Severity:** Medium. License and policy fields decide whether a team may use a model
  under the hackathon's open-model rule.
- **Verified by:** official `models_info`, docs and NVIDIA model cards.
- **Repro:**

```bash
curl -s https://tokenfactory.nebius.com/api/public/models_info | python3 -c "import json,sys; [print(m['name'], m.get('policy_url'), m['license']['name'], m['huggingface_url'], repr(m['created_at']), m['size_b'], [r['name'] for f in m['flavors'] for r in f['regions']]) for m in json.load(sys.stdin) if m['vendor']=='nvidia']"
```

- **Observed:**
  - Region: the Public Serverless page says public endpoints show their region as
    "Global" and may move "at any time, without notice". `models_info` and
    model-catalog.md list fixed regions: Nano and Lightning `eu-north1`, Super and Ultra
    `us-central1`.
  - License: `models_info` gives Nano and Super `nvidia-open-model-license`
    (…/nvidia-open-model-license/). NVIDIA's Hugging Face cards for the same models give
    `nvidia-nemotron-open-model-license` (…/nvidia-nemotron-open-model-license/). These are
    two different NVIDIA agreements: "NVIDIA Open Model License" (last modified
    2025-10-24) and "NVIDIA Nemotron Open Model License" (last modified 2025-12-15). Ultra
    is `openmdw-1.1` and Lightning `OpenMDW v1.1`: one license, two spellings.
  - Super's `policy_url` is https://llama.meta.com/llama3_1/use-policy/, Meta's Llama 3.1
    acceptable-use policy.
  - Super's `huggingface_url`, https://huggingface.co/nvidia/Nemotron-3-Super-120B,
    returns 401. The model card is nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-BF16.
  - `created_at` is an ISO string for Nano and Lightning and a Unix integer for Super and
    Ultra. `size_b` is 0 for Super and Ultra.
- **Expected:** `models_info` agrees with the docs and the model cards.
- **Impact:** the hackathon requires an NVIDIA open model. A team checking Super's terms
  finds two different NVIDIA agreements and a Llama policy.
- **Suggested fix:** take license and model-card fields from the Hugging Face card, fix
  Super's `policy_url` and link, use one type for `created_at`, and align the region
  wording.

<a id="fb-16"></a>
### FB-16. Dead links on the product page and fine-tuning guide; batch inference advertised but undocumented

- **Logged:** 2026-10-03.
- **Product:** docs.
- **Severity:** Medium.
- **Verified by:** me (HTTP status); official pages and OpenAPI.
- **Repro:**

```bash
for u in https://docs.tokenfactory.nebius.com/ai-models-inference/batch-inference \
         https://docs.tokenfactory.nebius.com/post-training/deploy-custom-model \
         https://docs.tokenfactory.nebius.com/fine-tuning/deploy-custom-model; do
  curl -s -o /dev/null -w "%{http_code} $u\n" "$u"
done
```

- **Observed:** all three return 404.
  - nebius.com/token-factory links "Batch inference" and "Deploy custom models" to the
    first two.
  - The fine-tuning guide (docs.tokenfactory.nebius.com/post-training/how-to-fine-tune)
    links to the second once and to the third twice ("deploy your fine-tuned model",
    "deploy the produced model files", "host the fine-tuned model").
  - nebius.com/solutions/inference says "Batch inference is available at half the
    real-time price for async and data processing workloads." The OpenAPI has no
    `/v1/batches`. Batch exists only as a Data Lab operation type (`batch_inference` on
    `POST /v1/operations`), and the docs index (`llms.txt`) has no batch page.
- **Expected:** working links, and a documented batch path with its price.
- **Impact:** I would have considered batch for my evaluation runs (348 receipts per
  model); they ran on the default tier instead.
- **Suggested fix:** restore or redirect the pages; document batch through Data Lab; and
  consider an OpenAI-compatible `/v1/batches`, so existing SDK code works unchanged.

<a id="fb-17"></a>
### FB-17. `service_tier` is always null

- **Logged:** 2026-10-03.
- **Product:** Token Factory inference.
- **Severity:** Low, to be revisited once I have measured the `flex` tier.
- **Verified by:** me, live; official OpenAPI.
- **Repro:** any request above; read `service_tier` in the reply.
- **Observed:** `"service_tier": null` in 19 of 19 non-streaming replies I checked (all
  four models), and no `service_tier` key in any chunk of 8 of 8 streams. The OpenAPI says
  of the default tier, `auto`: "Analyze response to determine which tier was used", and
  describes the response field as "The service tier used for the request."
- **Expected:** the tier that served the request (`default`, `over-limit` or `flex`).
- **Suggested fix:** fill the field. It is the only documented way to know whether a
  request ran over the limit.

<a id="fb-18"></a>
### FB-18. Super with thinking off writes a planning sentence into `content` before tool calls

- **Logged:** 2026-10-03 (3 of 3: one stream in my live check at about 07:19 UTC, two
  plain calls at 16:09 UTC).
- **Product:** Nemotron model (Super).
- **Severity:** Low.
- **Verified by:** me, live.
- **Repro:**

```bash
curl -s https://api.tokenfactory.nebius.com/v1/chat/completions \
  -H "Authorization: Bearer $NEBIUS_API_KEY" -H "Content-Type: application/json" \
  --data-binary @- <<'EOF'
{"model": "nvidia/nemotron-3-super-120b-a12b", "temperature": 0, "max_tokens": 512,
 "chat_template_kwargs": {"enable_thinking": false},
 "tools": [{"type": "function", "function": {"name": "get_weather",
   "description": "Get the current weather for a city.",
   "parameters": {"type": "object", "properties": {"city": {"type": "string", "description": "The city name"}},
                  "required": ["city"]}}}],
 "messages": [{"role": "system", "content": "You are a helpful assistant. Call a tool when it can answer the question."},
              {"role": "user", "content": "What's the weather in Paris right now?"}]}
EOF
```

- **Observed** (`x-request-id` 62b09ca045dc8f82a7bf2eb2ba0bc2ba): `finish_reason:
  "tool_calls"`, a correct `get_weather` call, and `content: "We need get_weather for
  Paris."`. With thinking on, the same sentence goes to the reasoning fields and `content`
  is empty. Ultra and Lightning return empty `content` with thinking off; Nano returns
  `"\n"`.
- **Expected:** no planning text in `content` when thinking is off, so agent UIs don't show
  it to users.
- **Suggested fix:** for NVIDIA, adjust the chat template or post-training; for Token
  Factory, nothing needed beyond documenting it.
- **My workaround:** none yet; the text is shown as a short assistant line.

<a id="fb-19"></a>
### FB-19. Sandboxes docs and SDK disagree on limits and on the project variable

- **Logged:** 2026-10-03. Found while preparing; not exercised end to end because access
  is not granted ([FB-13](#fb-13)).
- **Product:** Sandboxes.
- **Severity:** Low.
- **Verified by:** official docs; published packages contree-sdk 0.3.6, contree-cli 0.9.4
  and contree-client 0.4.0 (all install and start on Windows 11).
- **Repro:**

```bash
curl -s https://docs.tokenfactory.nebius.com/api-reference/sandboxes/auth/get-current-token-information.md \
  | grep -n "max_concurrency"
curl -s https://docs.tokenfactory.nebius.com/sandboxes/overview.md | grep -n "limited to"
pip download --no-deps contree-sdk==0.3.6 contree-cli==0.9.4 contree-client==0.4.0 -d sbx \
  && cd sbx && for w in *.whl; do unzip -o -q "$w"; done \
  && grep -rn -E "NEBIUS_PROJECT_ID|NEBIUS_AI_PROJECT" contree_sdk contree_cli contree_client
```

- **Observed:**
  - The `whoami` API reference example shows `instance_max_concurrency: 10` and
    `images_import_max_concurrency: 5`. The overview says running operations are "limited
    to 50". My live `whoami` returns 50 and 8.
  - contree-sdk's `IAMAuth` reads the project ID from `NEBIUS_PROJECT_ID`. contree-cli and
    contree-client read `CONTREE_PROJECT` or `NEBIUS_AI_PROJECT`, which is the name the
    docs use.
- **Expected:** one variable name across the SDK, CLI and docs, and an example that
  matches the documented limits.
- **Suggested fix:** accept both names in all three packages; update the example.

<a id="fb-20"></a>
### FB-20. No `seed` parameter

- **Logged:** 2026-10-03.
- **Product:** Token Factory inference.
- **Severity:** Low (feature request).
- **Verified by:** official OpenAPI.
- **Repro:**

```bash
curl -s https://api.tokenfactory.nebius.com/openapi.json | python3 -c "import json,sys; p=json.load(sys.stdin)['components']['schemas']['ChatCompletionRequest']['properties']; print('seed' in p, sorted(p))"
```

- **Observed:** `False`. The chat request has no `seed`, and `temperature` defaults to 1.
  I have not tested whether an unlisted `seed` is passed through.
- **Impact:** for a repeatable evaluation my only option is temperature 0, which departs
  from NVIDIA's recommended temperature 1.0 and top_p 0.95 for these models. Temperature 0
  does not repeat exactly either ([FB-26](#fb-26)).
- **Suggested fix:** document and support `seed`. Replies report a vLLM build in
  `system_fingerprint`, and vLLM accepts a per-request seed.

<a id="fb-21"></a>
### FB-21. No Nemotron model can be fine-tuned

- **Logged:** 2026-10-03.
- **Product:** post-training.
- **Severity:** Low (feature request).
- **Verified by:** official docs.
- **Repro:** read https://docs.tokenfactory.nebius.com/post-training/models.
- **Observed:** the page lists DeepSeek, Google Gemma 4, gpt-oss, Qwen (2.5, 3, 3.5 and
  3.6) and Meta Llama 3.1 to 3.3 base models. No Nemotron model is listed, although NVIDIA
  publishes Nemotron 3 weights. The page adds: "Deployment options currently only include
  via Dedicated endpoints."
- **Impact:** this is why Architect specializes a cheap Nemotron by tuning a harness
  around it and testing that harness on held-out cases, instead of fine-tuning the model.
- **Suggested fix:** LoRA for Nemotron 3 Nano, 3.5 Lightning and 3 Super, and serverless
  serving for those adapters, so a small team can try one without renting a dedicated
  endpoint.

<a id="fb-22"></a>
### FB-22. No NVIDIA safety model is served

- **Logged:** 2026-10-03.
- **Product:** docs (catalog); Token Factory inference.
- **Severity:** Low (feature request).
- **Verified by:** official `models_info`; NVIDIA's Hugging Face organization.
- **Repro:**

```bash
curl -s https://tokenfactory.nebius.com/api/public/models_info | python3 -c "import json,sys; [print(f['model_id']) for m in json.load(sys.stdin) for f in m['flavors']]"
```

- **Observed:** none of the 25 served models is a safety or guard model. NVIDIA publishes
  them, for example `nvidia/Nemotron-3.5-Content-Safety` and
  `nvidia/Llama-3.1-Nemotron-Safety-Guard-8B-v3`.
- **Impact:** an agent builder that wants a safety check on generated agents has to host
  one elsewhere or use a general model as a judge.
- **Suggested fix:** serve one NVIDIA content-safety model serverless.

<a id="fb-23"></a>
### FB-23. The AI Cloud CLI has no Windows build

- **Logged:** 2026-10-03.
- **Product:** AI Cloud.
- **Severity:** Low.
- **Verified by:** official docs.
- **Repro:** `curl -s https://docs.nebius.com/cli/install.md | grep -n "available for"`
- **Observed:** "The Nebius AI Cloud CLI is available for both Ubuntu and macOS."
- **Impact:** I develop on Windows 11 without WSL or Docker, so I could not install the
  CLI on my build machine when I looked at hosting my judge relay on a Serverless
  endpoint.
- **Suggested fix:** a Windows build of the CLI, or a console or Python equivalent next to
  every CLI example.

<a id="fb-24"></a>
### FB-24. OpenShell on Windows is experimental

- **Logged:** 2026-10-03.
- **Product:** NVIDIA tooling (OpenShell).
- **Severity:** Low.
- **Verified by:** official NVIDIA docs.
- **Repro:** read https://docs.nvidia.com/openshell/about/support-matrix.
- **Observed:** "Windows (WSL 2 + Docker Desktop) x86_64: Experimental"; Linux and macOS
  are supported.
- **Impact:** I considered OpenShell to contain my coding agent's shell commands. My
  build host is Windows, so for me it stays a stretch goal.
- **Suggested fix:** a supported Windows path, or a hosted OpenShell sandbox.

<a id="fb-25"></a>
### FB-25. The welcome email came 56 hours after sign-up, and its list of models leaves out Nemotron

- **Logged:** 2026-10-05.
- **Product:** onboarding email.
- **Severity:** Low.
- **Verified by:** the email Nebius sent me; Google's sign-in notice for the sign-up time.
- **Repro:** sign up with Google, then compare the time of Google's notice "You shared
  some Google Account data with Nebius Token Factory" with the time of the email
  "Welcome to Nebius Token Factory".
- **Observed:**
  - I first signed in at 05:21 UTC on 2026-10-03. The welcome email arrived at 13:30 UTC
    on 2026-10-05, about 56 hours later, when I had already made thousands of calls. It
    opens with "You've just gained access", and its footer says it was sent "because
    you've opted-in to Nebius mailing lists".
  - Its list of models reads "30+ open models including Qwen series, GPT OSS, GLM, Kimi,
    DeepSeek and much more". The email doesn't mention Nemotron.
- **Expected:** a welcome sent at sign-up, and a list of models that names the four
  Nemotron models Token Factory serves.
- **Suggested fix:** send the welcome when the account is created, and name Nemotron in
  it.

<a id="fb-26"></a>
### FB-26. Temperature 0 is not repeatable

- **Logged:** 2026-10-07, from four builds on 2026-10-06 and 2026-10-07 and my pilot of
  2026-10-04.
- **Product:** Token Factory inference.
- **Severity:** Medium. An agent that scores near a bar can pass one exam and fail the
  next.
- **Verified by:** me, live.
- **Repro:** send the same request several times and compare `content`. Changes are rare
  (Ultra's reply length changed on 6 of 150 pilot receipts between two passes), so use
  many receipts; pilot receipt train-0039 on Ultra is one that changed. Every exam call had
  this shape, with each model's ID and one receipt in the prompt:

```bash
curl -s https://api.tokenfactory.nebius.com/v1/chat/completions \
  -H "Authorization: Bearer $NEBIUS_API_KEY" -H "Content-Type: application/json" \
  --data-binary @- <<'EOF'
{"model": "nvidia/nemotron-3-super-120b-a12b", "temperature": 0, "max_tokens": 512, "n": 1,
 "stream": false, "chat_template_kwargs": {"enable_thinking": false},
 "messages": [{"role": "user", "content": "<a JSON object: one receipt's text, an instruction, the output schema, then my receipt rules>"}]}
EOF
```

  Architect builds each prompt from the published spec
  (`examples/bench/eval-2026-10-06/ladder/spec.json`). The exam's receipts stay out of this
  log.
- **Observed:**
  - Four builds of that spec, three on 2026-10-06 and one on 2026-10-07, made the same
    three agents (their artifact digests match), and each agent took the same 100-receipt
    exam once per build, with the same prompts and settings: temperature 0, thinking off,
    `max_tokens` 512. Right of 100:

| Build (UTC) | Route | Lightning | Super | Ultra |
|---|---|---|---|---|
| 2026-10-06 10:14 | my relay | 39 | 65 | 71 |
| 2026-10-06 14:27 | my relay | 39 | 63 | 70 |
| 2026-10-06 20:20 | Token Factory, direct | 42 | 65 | 72 |
| 2026-10-07 09:19 | Token Factory, direct | 39 | 64 | 72 |

  - The first two builds went by the same route, my relay, and nothing in Architect's
    model-calling code changed between them, yet Super's and Ultra's scores moved. The
    last two went straight to Token Factory with the same profile, again with no change to
    that code, and Lightning's and Super's scores moved, so my relay is not what moves
    them.
  - The signed exam reports keep counts, not answers, so they can't show which receipts
    changed. The counts mean that from one build to another at least 3 of Lightning's
    answers changed (39 and 42), 2 of Super's (63 and 65) and 2 of Ultra's (70 and 72).
    The reports' exam costs, at list prices, show reply lengths moving as well: Super's
    second exam used 5 fewer output tokens than its other three, and Ultra's second, third
    and fourth used 7, 11 and 11 more than its first. Lightning used the same number in
    all four. The run logs of the last two builds show equal totals for Lightning (4,264
    output tokens) and for Super (4,215) though both scores moved, so an equal token count
    does not mean equal answers.
  - My pilot shows it receipt by receipt. On 2026-10-04 plain Ultra and plain Lightning
    answered the same 150 receipts twice, in the pilot and in a re-run, with equal prompt
    tokens on every receipt. Ultra's reply length changed on 6 of 150 receipts, and
    whether it was right changed on 4. Lightning's reply length changed on 1 of 150, and
    whether it was right on none ([analysis](../bench/pilot-error-analysis-2026-10-04.md)).
    On train-0039 Ultra left the subtotal null in the pilot (35 completion tokens) and gave
    the total, 70000, in the re-run (40 tokens, reply chatcmpl-b80caea926502af9).
  - The bar was 40 of 100, so the build decided which model Architect shipped: Lightning
    in the third, Super in the other three.
- **Expected:** the same request at temperature 0 gets the same reply, or the docs say it
  may not.
- **Likely cause** (my inference, not confirmed): the server's arithmetic can depend on
  which requests share a batch, and a tiny difference changes the top token when two
  tokens are close. If so, a `seed` alone would not fix it.
- **Why it matters:** Token Factory has no `seed` ([FB-20](#fb-20)), so temperature 0 is
  the only way to ask for a repeatable run, and it is not enough. An evaluation that
  compares a score with a bar, or two models with each other, has to run more than once
  or leave a margin.
- **Suggested fix:** say in the API reference that temperature 0 is not deterministic,
  and offer a deterministic option for greedy decoding (batch-invariant kernels), even if
  it runs slower. Support `seed` too, for sampled runs.
- **My workaround:** none in the product. I report every build's exam, and Architect's
  README lists this under Limits. Architect does not yet rerun an exam or require a
  margin above the bar.

<a id="fb-27"></a>
### FB-27. Told that an unprinted amount is null, Ultra fills in a subtotal about as often as Lightning

- **Logged:** 2026-10-07, from the re-run of my 2026-10-04 pilot, which kept every reply,
  and the rule checks of 2026-10-06.
- **Product:** Nemotron model (Ultra, Super, Lightning).
- **Severity:** Medium. It needs a workaround, and a bigger model is not one.
- **Verified by:** me, live; NVIDIA's model cards for the model sizes.
- **Repro:** the pilot commands in [pilot-2026-10-04.md](../bench/pilot-2026-10-04.md)
  (A1-pilot is Ultra, A3-pilot is Lightning) send one request per receipt, shaped like the
  one in [FB-26](#fb-26). The prompt is a JSON object: the receipt's text, an instruction,
  the output schema, then my receipt rules word for word
  ([RULES.md](../../examples/receipts/RULES.md)). Two of the rules: the subtotal is "the
  first amount printed after the items that sums them, apart from the total", and "An
  amount the receipt does not print is null; never compute it from other amounts."
- **Observed:**
  - 51 of the 150 pilot receipts print no subtotal that the rules accept. In the re-run,
    Ultra gave a subtotal on 50 of them and Lightning on all 51, most often by copying the
    total: on train-0008 both answered 36000, its total (replies
    chatcmpl-aa61cc6ba6a7ded6 from Ultra and chatcmpl-31659ae5 from Lightning). My plan
    keeps the pilot for calibration, so these counts are not evaluation results. The
    [error analysis](../bench/pilot-error-analysis-2026-10-04.md) lists every miss.
  - Ultra reads better. It read the subtotal right on 95 of the 99 receipts that print
    one, to Lightning's 91, and it broke the stated rules on 89 of its 600 fields, to
    Lightning's 143. On this one rule the gap was a single receipt.
  - Applying the rule in code helps every model. Two checks run on each reply: this one
    (a subtotal equal to the total becomes null unless a subtotal line prints it) and one
    that keeps every printed digit of an amount. Together they are about 50 lines of
    Python ([frozen_checks.py](../bench/eval-2026-10-06/frozen_checks.py)).
  - On 100 rehearsal receipts, applied to kept replies, the checks took Lightning from 29
    to 41 right and Super from 50 to 64, and turned no right answer wrong. By itself, the
    subtotal check added 8 for Lightning and 10 for Super
    ([results](../bench/results-receipts.md#gate-1-on-the-rehearsal-receipts-2026-10-06)).
  - On my 100-receipt exam they took Lightning from 27 to 39, Super from 51 to 65 and
    Ultra from 49 to 71 ([results](../bench/results-receipts.md)), so Ultra gained the
    most. The exams with and without the checks ran in different builds, so the spread in
    [FB-26](#fb-26) applies, but I saw at most 3 there, and the gains are 12 to 22.
- **Expected:** a model told to leave an unprinted amount null leaves it null, and a
  bigger model does so more often.
- **Why it matters:** Ultra has about 18 times Lightning's parameters (550B against 30B,
  per NVIDIA's model cards) and costs about 16 times as much per receipt. The extra buys
  better reading but not this rule, so moving up to a bigger model costs more and leaves
  these errors in place.
- **Scope:** every call ran at temperature 0 with thinking off. NVIDIA recommends
  temperature 1.0, and I have not tested whether thinking changes this.
- **Suggested fix:** for NVIDIA, post-train on rules of this kind (leave a field null when
  the input doesn't print it; don't fill one field from another), and report in the model
  cards how often each model follows them, so builders know when to check in code.
- **My workaround:** the receipt reader's spec declares both checks, Architect runs them
  on every reply, and the exam licenses the model and its checks together
  (`architect/presets/checks.py`). Each new rule needs new check code.

## Probe ledger

Live calls made to verify this log, at list prices.

| Date (UTC) | What | Requests | Cost |
|---|---|---|---|
| 2026-10-03 16:04 to 16:12 | Re-checks for FB-01, 02, 04, 05, 07, 09, 11, 13, 14, 17, 18 | 46 (6 rejected with 403 or 404 at no charge; 1 free `whoami`) | $0.0024 |
| 2026-10-06 17:06 to 17:08 | Lightning's thinking controls on 10 receipts, for FB-05 | 70 | $0.0098 |
