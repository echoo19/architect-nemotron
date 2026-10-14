# Feedback: Nebius Token Factory, Nebius AI Cloud and NVIDIA Nemotron

Architect is an agent factory. You type a job in one sentence and it builds a small AI
agent, and the agent only runs if it passed its exam. In this build NVIDIA Nemotron models
on Nebius Token Factory do every job: Nemotron 3 Super holds the conversation and drafts
the plan, and the agent runs on the cheapest Nemotron that reaches the pass mark.

This folder is my feedback for the Nebius x NVIDIA Global AI Hackathon. The organizers
asked, for Token Factory, AI Cloud and each NVIDIA model used: what I used it for, what
worked well, what needs work, how onboarding felt (zero to hello world), and whether I'd
build with it again, and why
([update](https://nebiusglobalaihackathon.devpost.com/updates/46205-how-to-build-a-winning-project)).
This README answers the first four questions for each tool, and the fifth once, under
[Would I build with it again](#would-i-build-with-it-again). [LOG.md](LOG.md) holds every
finding with a repro, what I observed, what I expected and a suggested fix.

How to read it:

- Every finding is one I reproduced against the live service, or one an official Nebius
  or NVIDIA source states. Reports from other teams stay out until I reproduce them.
- Probe numbers come from single calls on 2026-10-03 unless a count is given. They
  describe API behavior, not model quality. Accuracy and cost results come only from my
  pre-registered evaluation of 2026-10-05 and the change to it I registered on
  2026-10-06 ([results](../bench/results-receipts.md)). [FB-27](LOG.md#fb-27) also cites
  counts from my 2026-10-04 pilot, a calibration run that is not part of those results.
- Older entries use the build's own words: the bar is the pass mark, a rung is one model in
  the build, and licensed means passed.
- Last updated 2026-10-13.

## Top issues

Ranked by what they would cost a developer building on Token Factory.

1. [FB-01](LOG.md#fb-01): a thinking reply cut at `max_tokens` comes back from Ultra and
   Lightning with the reasoning as `content` and HTTP 200; in a stream, the whole
   reasoning arrives as the final `content` delta. `max_tokens` defaults to 8192 and
   reasoning counts against it.
2. [FB-02](LOG.md#fb-02): all three Quickstart samples use `deepseek-ai/DeepSeek-R1-0528`,
   which returns 404. 12 of the 15 model IDs in the docs' code samples are not in the
   catalog.
3. [FB-03](LOG.md#fb-03): there is no hard spending cap per project or key. Budgets only
   send alerts, and those can arrive hours late, so sharing a key with judges or an agent
   means building your own relay.
4. [FB-05](LOG.md#fb-05): the reasoning switch that works,
   `chat_template_kwargs.enable_thinking`, is not in the API reference, and
   `reasoning_effort` `"low"` and `"high"` do nothing on three of the four Nemotron models.
   On Lightning, `"minimal"` and `"medium"` don't turn thinking off either: with any of
   the four, it reasoned until `max_tokens` (512) on 10 of 10 receipts and gave no answer.
5. [FB-27](LOG.md#fb-27): Ultra, with about 18 times Lightning's parameters, breaks a rule
   my prompt states about as often as Lightning. It gave a subtotal on 50 of the 51
   receipts in my pilot that print none, and Lightning on all 51. Two rule checks in code
   raised every model's exam score, Ultra's the most (49 to 71 of 100).
6. [FB-06](LOG.md#fb-06): reasoning comes back in `reasoning`, `reasoning_content` or both,
   depending on the model, and the response schema documents neither.
7. [FB-04](LOG.md#fb-04): `reasoning_effort: "none"` on Nano returns `content: null` with
   the answer in `reasoning`.
8. [FB-07](LOG.md#fb-07): Lightning with strict `json_schema` and thinking on returned
   reasoning text instead of JSON in 2 of 3 runs.
9. [FB-26](LOG.md#fb-26): temperature 0 is not repeatable. The same Lightning agent got 39,
   39, 42 and 39 of 100 on the same exam in four builds, against a bar of 40, so the build
   decided which model shipped. There is no `seed` either ([FB-20](LOG.md#fb-20)).
10. [FB-09](LOG.md#fb-09): Super and Ultra send no `x-ratelimit-*` headers, although the
    docs say limits are always in the headers.
11. [FB-11](LOG.md#fb-11): retirement comes with "7 days usually" of notice, nothing
    machine-readable, and different errors (403 or 404) for IDs that are no longer served.
12. [FB-13](LOG.md#fb-13): Sandboxes beta access shows no status and no timeline; my
    `whoami` still reports every permission as false.

Also open: [FB-08](LOG.md#fb-08) `response_format` docs, [FB-10](LOG.md#fb-10) usage
fields and cache pricing, [FB-12](LOG.md#fb-12) trial and paid-switch docs,
[FB-14](LOG.md#fb-14) cookbook, [FB-15](LOG.md#fb-15) catalog metadata,
[FB-16](LOG.md#fb-16) dead links and batch inference, and nine low-severity items in the
[log index](LOG.md#index).

## Tools at a glance

| Tool | What I use it for | Status on 2026-10-05 |
|---|---|---|
| Token Factory inference API | Every model call in this build | Wired in and live-checked on all four Nemotron models |
| Token Factory billing and credits | The trial and two $25 promo credits, then paid usage from that balance | Paid usage since the trial ended on 2026-10-04; credits cover it, and the card has not been charged |
| Token Factory Sandboxes | Nothing; I wanted a disposable VM per candidate program | Not granted |
| Nebius AI Cloud | Considered for hosting the judge relay | Not used |
| NVIDIA Nemotron 3 Ultra | Top rung of the receipts evaluation; Forge's engineer in 5 development sessions; available through `/model` | Wired in; first code-writing sessions from 2026-10-04 |
| NVIDIA Nemotron 3 Super | Interview, conversation, code edits and Forge's engineer; second rung, the one the receipt reader shipped on | Wired in |
| NVIDIA Nemotron 3.5 Lightning | First rung; runs the agent when it passes its exam | Wired in |
| NVIDIA Nemotron 3 Nano | Probed; Lightning took its role | Not used |
| NVIDIA OpenShell | Considered for containing the coding agent's shell | Not used |

## Would I build with it again

The submission form asked the nearest questions as scores out of 10: Nemotron's output
quality, whether I'd recommend Nemotron on Nebius, and how running inference there went.
These are my three answers. I never ran anything on Sandboxes or AI Cloud, so I have no
answer for those two.

- Nemotron's output quality, 7 of 10: with thinking off, all 3 models gave a valid answer on
  every one of 348 receipts, and strict JSON worked in every probe. Tool calls parsed in all
  11 I saved. The main drawback is following stated rules. Told to leave an amount null when
  the receipt doesn't print it, Ultra still gave a subtotal on 50 of 51 pilot receipts that
  print none. As Architect's coder, Super twice said it was done without editing a file.
- Recommending Nemotron on Nebius, 8 of 10: in my evaluation run all 3,757 calls came back
  with status 200. It isn't higher because a key has no hard spending cap, so I had to build
  a relay with its own caps before I could hand access to judges.
- Running Nemotron inference on Nebius, 8 of 10: I haven't run Nemotron in another
  environment, so I can't compare the two directly. On Nebius it took about 6 minutes from
  sign-up to my first successful call, and every reply carries token counts, so I could
  price each call as it happened. The thinking switch cost me time: the one that works on
  every size, `chat_template_kwargs.enable_thinking`, is in NVIDIA's model cards but not in
  Token Factory's API reference, so I had to confirm it by probing.

## Nebius Token Factory: inference API

**What I used it for**

- Every model call Architect makes in this build goes to
  `https://api.tokenfactory.nebius.com/v1`:
  - Nemotron 3 Super interviews the user, converses and edits code (thinking off), and
    it drives Forge, Architect's harness engineer (thinking off, temperature 0). Ultra
    drove Forge in five development sessions (thinking on; temperature 1.0 and top_p
    0.95, as NVIDIA recommends).
  - The licensed agent runs on Nemotron 3.5 Lightning when Lightning passes its exam, and
    on Super otherwise (thinking off, temperature 0). On the receipts the exam refused
    Lightning in my 2026-10-05 build, so that receipt reader runs on Super. With the rule
    checks Lightning got 39, 39, 42 and 39 of 100 on the same exam in four builds. Only the
    build at 42 shipped it. The other three shipped Super.
  - A ladder tries Lightning, then Super; the cheapest model that passes the held-out
    exam ships. My evaluation added Ultra as a third rung.
- Endpoints: `POST /v1/chat/completions` (plain text, strict `json_schema`, native tool
  calls, streaming), `GET /v1/models?verbose=true` for prices and context lengths, and the
  public `models_info` catalog.

**What worked well**

- It is OpenAI-compatible where it matters. My existing OpenAI-compatible adapter needed
  a dialect (always send `max_tokens`, set the thinking switch, hide reasoning fields,
  retry 429, 503 and 529), not a new client.
- Native tool calls: all 11 tool-call requests in my saved captures (four models,
  thinking on and off) came back with parseable arguments and `finish_reason:
  "tool_calls"`.
- Strict `json_schema` was schema-valid on all four models with thinking off (9 of 9).
- `chat_template_kwargs` reaches the chat template. `enable_thinking: false` turned
  reasoning off on all four models (2 completion tokens instead of 41 to 225 on the same
  prompt), and Super's `low_effort` and Ultra's `medium_effort` took effect.
- In replies that finish normally, reasoning stays out of `content`, with no `<think>`
  tags.
- Prices and context lengths are machine-readable from `GET /v1/models?verbose=true` and
  the public `models_info`, and the two agreed for all four Nemotron models.
- Short calls with thinking off took 0.2 to 1.9 s from my host.
- Rate limits are generous for my scale: 600 requests and 400,000 tokens per minute for
  Lightning, 100 and 800,000 for Nano (from response headers).

**What needs work**

- Reasoning handling: [FB-01](LOG.md#fb-01), [FB-04](LOG.md#fb-04),
  [FB-05](LOG.md#fb-05), [FB-06](LOG.md#fb-06), [FB-07](LOG.md#fb-07).
- Docs that fail on first contact: [FB-02](LOG.md#fb-02), [FB-08](LOG.md#fb-08),
  [FB-14](LOG.md#fb-14), [FB-16](LOG.md#fb-16).
- Operational signals: [FB-09](LOG.md#fb-09) (rate-limit headers),
  [FB-10](LOG.md#fb-10) (usage shape and cache price), [FB-11](LOG.md#fb-11)
  (retirement), [FB-17](LOG.md#fb-17) (`service_tier`).
- Repeatable runs: [FB-26](LOG.md#fb-26) (temperature 0 is not repeatable) and
  [FB-20](LOG.md#fb-20) (no `seed`).

**Onboarding, zero to hello world**

- The Quickstart's three samples fail today because their model is gone
  ([FB-02](LOG.md#fb-02)). I took model IDs from `models_info` instead.
- Model IDs are case-sensitive and each Nemotron ID has its own casing:
  `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`, `nvidia/Nemotron-3_5-Lightning`,
  `nvidia/nemotron-3-super-120b-a12b`, `nvidia/Nemotron-3-Ultra-550b-a55b`. Copy them;
  don't type them.
- Time from sign-up to the first successful call: about 6 minutes. On 2026-10-03 I
  signed in with Google at 05:21 UTC, filled in the billing details the console asks for
  before it creates a key, made a key, and got HTTP 200 from `GET /v1/models` at 05:27.
  The first chat replies, one from each Nemotron model, came back by 05:30.
- What I had to learn from probes rather than docs: the thinking switch, the per-model
  reasoning fields, and what a cut reply looks like. The first two took under a minute of
  probing (05:29 to 05:30 UTC). I first saw a cut reply at 06:37, while building my
  adapter, and the adapter worked live on all four models at 07:28, about two hours after
  the first probe.
- The welcome email came 56 hours after I signed up, and its list of models doesn't name
  Nemotron ([FB-25](LOG.md#fb-25)).

## Token Factory billing and credits

**What I used it for**

- By 2026-10-05, credits had paid for every call. The console lists three credits: the
  hackathon's $25 promo credit (applied 2026-10-03), $25 from the AI Builder Program
  (applied 2026-10-04), and $5.49 labeled "Trial", dated 22:59 UTC on 2026-10-04, when
  the $1 trial closed ([FB-12](LOG.md#fb-12)). Paid usage has drawn on that balance
  since. On 2026-10-05 the console showed $18.51 of consumption and a balance of $36.98,
  and the card had not been charged. I bound my own spend with the caps in my relay
  ([FB-03](LOG.md#fb-03)).

**What worked well**

- I applied the hackathon's promo code through Top up, then With promo code, as the docs
  describe. The code arrived by email at 05:45 UTC on 2026-10-03, less than 18 minutes
  after I opened the form, and it was on my balance two minutes later. The AI Builder
  Program's own $25 code came 15 hours after I applied.
- Every reply carries token counts, so I cost each call at list prices as it happens.
  My ledger agreed with the console within $0.14: on 2026-10-05 the usage page showed
  $18.51 for usage up to 09:49 UTC, and my ledger had $18.37 at 09:49 and $18.76 at
  10:00.

**What needs work**

- [FB-03](LOG.md#fb-03): no hard spending cap per project or key, and the usage page ran
  about 2.5 hours behind when I checked.
- [FB-12](LOG.md#fb-12): trial end and "Switch to paid usage" are only explained in the
  console; the terms say fees are pre-paid while the docs describe card charges.
- [FB-10](LOG.md#fb-10): no published price for cached prompt tokens, and by my
  reconciliation the bill charges them at the full input price.

**Onboarding, zero to hello world**

- A bank card is required to finish sign-up, per the docs. For me the card step came
  straight after signing in with Google: the API keys page showed a billing-details form
  (name, address, card, billing email) in place of the key list, with a note that the
  card gets a temporary $0 authorization. The console won't create a key until the form
  is saved; it took me under three minutes.
- The switch to paid usage asked for nothing more. When I went to turn it on, on
  2026-10-03, it was already on ([FB-12](LOG.md#fb-12)).

## Token Factory Sandboxes (beta)

**What I used it for**

- Nothing. I wanted to run each candidate program's exam in a disposable Sandboxes VM;
  without access, Architect uses its own in-process interpreter.

**What worked well**

- `whoami` answers clearly, with permissions and limits.
- contree-cli 0.9.4, contree-sdk 0.3.6 and contree-client 0.4.0 install and start on
  Windows 11.
- The console says Sandboxes are free while in beta.

**What needs work**

- [FB-13](LOG.md#fb-13): no request status and no timeline.
- [FB-19](LOG.md#fb-19): docs and SDK disagree on limits and on the project variable.

**Onboarding, zero to hello world**

- Access is by request form. I asked for it and it hadn't been granted as of 2026-10-05,
  when `whoami` still reported every permission as false ([FB-13](LOG.md#fb-13)).

## Nebius AI Cloud

**What I used it for**

- Nothing in the submission. I considered a CPU Serverless endpoint for the capped relay
  that gives judges free access, and chose a free-tier host instead.

**What worked well**

- Not used, so nothing to report beyond onboarding.

**What needs work**

- [FB-23](LOG.md#fb-23): the CLI is built for Ubuntu and macOS only.

**Onboarding, zero to hello world**

- My build machine runs Windows 11 without WSL or Docker, so I could not install the
  CLI there.

## NVIDIA Nemotron 3 Ultra

`nvidia/Nemotron-3-Ultra-550b-a55b`

**What I used it for**

- Forge's engineer in five development sessions on the receipts (thinking on,
  temperature 1.0, top_p 0.95). A fresh Nebius profile now gives that role to Super.
- A plain arm and the top rung of the receipts evaluation (plain prompt, thinking off),
  and the model whose pilot score set the receipts bar of 0.40.

**What worked well**

- Tool calls and strict `json_schema` worked with thinking on and off in every probe.
- `medium_effort` in `chat_template_kwargs` changed its reasoning length, so NVIDIA's
  documented controls reach the model.
- In its five Forge sessions, none of its tool calls was malformed.
- It got 184 of the 348 evaluation receipts right (0.529), and the build's exam licensed
  it at 49 of 100 ([results](../bench/results-receipts.md)).

**What needs work**

- As Forge's engineer it submitted no harness in five sessions on the receipts
  ([probes](probes/)).
- [FB-01](LOG.md#fb-01): a cut reply returns reasoning as `content`, in streams too.
- [FB-09](LOG.md#fb-09): no rate-limit headers.
- [FB-05](LOG.md#fb-05): `reasoning_effort` `"low"` and `"high"` have no effect.
- [FB-27](LOG.md#fb-27): it gave a subtotal on 50 of the 51 pilot receipts that print
  none, about as often as Lightning. Two rule checks in code took its exam from 49 to 71
  of 100.

**Onboarding, zero to hello world**

- The model card's settings (temperature 1.0, top_p 0.95, `enable_thinking`) carried over
  unchanged through `chat_template_kwargs`. The card's advice on
  `force_nonempty_content` for tool calls is not addressed in Token Factory's docs
  ([FB-05](LOG.md#fb-05)).

## NVIDIA Nemotron 3 Super

`nvidia/nemotron-3-super-120b-a12b`

**What I used it for**

- Interviews the user, holds the conversation, and edits code in the user's repository
  (thinking off).
- Drives Forge, Architect's harness engineer, in a fresh Nebius profile (thinking off,
  temperature 0, up to 300 s a call). It was the engineer in seven Forge sessions on the
  receipts.
- The second rung of the ladder, and the model the receipt reader shipped on.

**What worked well**

- The only model on which `reasoning_effort: "low"` (or `low_effort`) worked: reasoning
  fell from 37 to 6 tokens on a simple prompt, with the same answer.
- Tool calls and strict `json_schema` worked in every probe.
- It got 180 of the 348 evaluation receipts right (0.517), four fewer than Ultra, at $0.28
  per 1,000 receipts against Ultra's $0.92. The build's exam licensed it at 51 of 100,
  and it shipped as the cheapest licensed rung ([results](../bench/results-receipts.md)).

**What needs work**

- As Forge's engineer it had no harness accepted in seven sessions on the receipts. Its
  best submission scored 0.31 on the practice receipts, under the bar of 0.40.
- [FB-18](LOG.md#fb-18): with thinking off it narrates its plan in `content` before tool
  calls.
- [FB-09](LOG.md#fb-09): no rate-limit headers. [FB-06](LOG.md#fb-06): it fills both
  reasoning fields and starts `content` with blank lines.
- [FB-15](LOG.md#fb-15): its catalog entry points to Meta's Llama 3.1 use policy and to a
  model-card link that returns 401.
- [FB-27](LOG.md#fb-27): it copies the total into an unprinted subtotal too. A check in
  code for that rule alone made 10 more of 100 rehearsal receipts right
  ([results](../bench/results-receipts.md#gate-1-on-the-rehearsal-receipts-2026-10-06)).

**Onboarding, zero to hello world**

- Its ID is all lowercase, unlike the other three.

## NVIDIA Nemotron 3.5 Lightning

`nvidia/Nemotron-3_5-Lightning`

**What I used it for**

- Runs the licensed agent when it passes its exam (thinking off, temperature 0).
- The first rung of the ladder: the best of four preset configurations, then Forge's
  attempt to beat it. On the receipts it got 94 of the 348 evaluation receipts right
  (0.270) as a plain arm, and the 2026-10-05 build's exam refused it at 27 of 100 against
  a bar of 0.40. With the rule checks it got 39, 39, 42 and 39 on that exam in four builds,
  and at 42 it was licensed and shipped ([results](../bench/results-receipts.md)).

**What worked well**

- The fastest short calls in my probes (0.23 to 1.05 s with thinking off), $0.06 and
  $0.24 per million input and output tokens, and 600 requests per minute.
- With thinking off, strict `json_schema` and tool calls worked every time.

**What needs work**

- It reasons at length on simple prompts: 222 reasoning tokens for a one-word sentiment
  answer, against 37 for Super and 38 for Ultra (temperature 0, one call each).
- That length runs into [FB-01](LOG.md#fb-01) and [FB-07](LOG.md#fb-07): with thinking
  on, 2 of 3 strict-schema calls returned reasoning text instead of JSON.
- [FB-05](LOG.md#fb-05): it thinks unless `enable_thinking` is false or `reasoning_effort`
  is `"none"`. With `"minimal"`, `"low"`, `"medium"` or `"high"` it reasoned until
  `max_tokens` (512) on 10 of 10 receipts and gave no answer.
- [FB-27](LOG.md#fb-27): it gave a subtotal on all 51 pilot receipts that print none.

**Onboarding, zero to hello world**

- The ID writes "3.5" as `3_5`.

## NVIDIA Nemotron 3 Nano

`nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`

**What I used it for**

- Probes only. Lightning costs the same, has 1M tokens of context against 262k, and is
  listed at 314 tokens per second against 60, so Lightning took the runner's role.

**What worked well**

- `enable_thinking: false`, strict `json_schema` and tool calls all worked.

**What needs work**

- [FB-04](LOG.md#fb-04): `reasoning_effort: "none"` empties `content`.
- [FB-10](LOG.md#fb-10): no reasoning token count, so its reasoning cost can't be measured.
- [FB-06](LOG.md#fb-06): only the `reasoning` field, and `tool_calls: []` instead of null.

**Onboarding, zero to hello world**

- The cookbook's Nano page doesn't run as written: its model ID returns 404
  ([FB-14](LOG.md#fb-14)).

## NVIDIA OpenShell

Considered for containing my coding agent's shell commands; not used, because its
Windows support is experimental and my build host is Windows ([FB-24](LOG.md#fb-24)).

## Data only I have

These numbers come from my own runs, the evaluation of 2026-10-05 among them
([results](../bench/results-receipts.md)).

| Measure | Source | Value |
|---|---|---|
| Invalid-output rate per model | Evaluation, 348 receipts per model | 0 of 348 for Ultra, Super and Lightning, reading the first JSON object in each reply. The evaluation did not use `json_schema`. |
| Latency p50 and p95 per model, with the region it ran in | Evaluation, per receipt, relay included | Ultra 0.750 and 0.875 s, Super 0.719 and 0.906 s (us-central1); Lightning 0.468 and 0.672 s (eu-north1) |
| Reasoning share of output tokens in code-writing sessions | Per-turn token log ([probes](probes/)) | Ultra, thinking on: 11.9% to 28.9% across its five Forge sessions. Super ran with thinking off. |
| Share of prompt tokens served from cache in those sessions, and whether the bill discounts them | Per-turn token log; console reconciliation | Ultra 66.7% to 76.9% across its five Forge sessions; Super 0% in its six development sessions. By my reconciliation the bill does not discount them ([FB-10](LOG.md#fb-10)). |
| 429, 503 and 529 counts, with their `Retry-After` values | Evaluation, relay ledger | None: all 3,757 calls returned 200 ([results](../bench/results-receipts.md)). |
| Replies where Lightning's reasoning arrived as `content` | Evaluation; probes | None in the evaluation, which ran with thinking off. With thinking on, 2 of 3 strict-schema probe calls ([FB-07](LOG.md#fb-07)) and 50 of 50 receipt calls ([FB-05](LOG.md#fb-05)). |
| `flex` tier: latency, 429s, and what the reply reports | Not measured | The evaluation ran on the default tier. |

Single-call reasoning lengths per model are in [FB-05](LOG.md#fb-05).

## Next probes

Open questions. Each becomes a log entry only once I reproduce an answer.

- Does `service_tier: "flex"` change latency or 429 behavior, and does the reply say
  which tier ran?
- Is `force_nonempty_content` needed, or honored, for Ultra tool calls with thinking on?
- Is an unlisted `seed` passed through?
- What do 429, 503 and 529 replies carry under real load (`Retry-After`, body)?
- Does `max_completion_tokens` cut reasoning the same way `max_tokens` does?
