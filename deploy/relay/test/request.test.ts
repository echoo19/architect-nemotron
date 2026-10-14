import { describe, expect, it } from "vitest";
import { MAX_JSON_DEPTH, estimateInputTokens, prepareRequest, promptShape, reservationNano, withinDepth } from "../src/request.js";
import { LIGHTNING, NANO, SUPER, ULTRA, config, messages } from "./helpers.js";

function prepared(body: Record<string, unknown>) {
  const result = prepareRequest(body, config);
  if (!result.ok) throw new Error(`refused: ${result.error.message}`);
  return { ...result.value, body: JSON.parse(new TextDecoder().decode(result.value.payload)) as Record<string, unknown> };
}

describe("field allowlist", () => {
  it("forwards only allowed fields and names every dropped one", () => {
    const out = prepared({
      model: SUPER,
      messages,
      temperature: 0,
      top_p: 0.95,
      max_tokens: 64,
      stop: ["\n\n"],
      tools: [{ type: "function", function: { name: "f", parameters: { type: "object" } } }],
      tool_choice: "auto",
      response_format: { type: "json_object" },
      best_of: 4,
      ignore_eos: true,
      logit_bias: { "50256": -100 },
      guided_json: { type: "object" },
      top_k: 5,
      min_tokens: 4000,
      seed: 7,
      user: "someone",
      n: 3,
      reasoning_effort: "high",
    });
    expect(Object.keys(out.body).sort()).toEqual(
      ["max_tokens", "messages", "model", "n", "response_format", "stop", "temperature", "tool_choice", "tools", "top_p"].sort(),
    );
    expect(out.body.n).toBe(1);
    expect(out.dropped.sort()).toEqual(
      ["best_of", "guided_json", "ignore_eos", "logit_bias", "min_tokens", "n", "reasoning_effort", "seed", "top_k", "user"].sort(),
    );
  });

  it("forces n to 1 even when the client asks for several choices", () => {
    const out = prepared({ model: LIGHTNING, messages, n: 4 });
    expect(out.body.n).toBe(1);
    expect(out.dropped).toContain("n");
    expect(prepared({ model: LIGHTNING, messages, n: 1 }).dropped).not.toContain("n");
  });

  it("keeps only enable_thinking inside chat_template_kwargs", () => {
    const out = prepared({
      model: ULTRA,
      messages,
      chat_template_kwargs: { enable_thinking: false, force_nonempty_content: true, reasoning_budget: 99999 },
    });
    expect(out.body.chat_template_kwargs).toEqual({ enable_thinking: false });
    expect(out.dropped).toEqual(
      expect.arrayContaining(["chat_template_kwargs.force_nonempty_content", "chat_template_kwargs.reasoning_budget"]),
    );
    const none = prepared({ model: ULTRA, messages, chat_template_kwargs: { thinking: true } });
    expect(none.body.chat_template_kwargs).toBeUndefined();
    expect(prepareRequest({ model: ULTRA, messages, chat_template_kwargs: { enable_thinking: "yes" } }, config).ok).toBe(false);
  });

  it("always asks for usage on streams and drops stream_options otherwise", () => {
    const streamed = prepared({ model: SUPER, messages, stream: true, stream_options: { continuous_usage_stats: true } });
    expect(streamed.body.stream).toBe(true);
    expect(streamed.body.stream_options).toEqual({ include_usage: true });
    expect(streamed.clientWantsUsage).toBe(false);
    expect(streamed.dropped).toContain("stream_options.continuous_usage_stats");
    const wanted = prepared({ model: SUPER, messages, stream: true, stream_options: { include_usage: true } });
    expect(wanted.clientWantsUsage).toBe(true);
    const plain = prepared({ model: SUPER, messages, stream_options: { include_usage: true } });
    expect(plain.body.stream).toBeUndefined();
    expect(plain.body.stream_options).toBeUndefined();
    expect(plain.dropped).toContain("stream_options");
  });

  it("passes service_tier only for the configured tiers", () => {
    expect(prepared({ model: SUPER, messages, service_tier: "flex" }).body.service_tier).toBe("flex");
    const priority = prepared({ model: SUPER, messages, service_tier: "priority" });
    expect(priority.body.service_tier).toBeUndefined();
    expect(priority.dropped).toContain("service_tier");
  });

  it("treats explicit nulls as absent", () => {
    const out = prepared({ model: SUPER, messages, tools: null, stop: null, temperature: null, max_tokens: null });
    expect(out.dropped).toEqual([]);
    expect(out.body.max_tokens).toBe(8192);
    expect(out.body.tools).toBeUndefined();
  });
});

describe("max_tokens clamp", () => {
  it("defaults to the clamp when omitted: 32768 for Ultra, 8192 for the others", () => {
    expect(prepared({ model: ULTRA, messages }).maxTokens).toBe(32768);
    for (const model of [NANO, LIGHTNING, SUPER]) expect(prepared({ model, messages }).body.max_tokens).toBe(8192);
  });

  it("clamps larger values and keeps smaller ones", () => {
    expect(prepared({ model: SUPER, messages, max_tokens: 100000 }).maxTokens).toBe(8192);
    expect(prepared({ model: ULTRA, messages, max_tokens: 40000 }).maxTokens).toBe(32768);
    expect(prepared({ model: ULTRA, messages, max_tokens: 512 }).maxTokens).toBe(512);
  });

  it("reads max_completion_tokens as the cap but never forwards it", () => {
    const out = prepared({ model: SUPER, messages, max_completion_tokens: 300 });
    expect(out.body.max_tokens).toBe(300);
    expect(out.body.max_completion_tokens).toBeUndefined();
    expect(out.dropped).toContain("max_completion_tokens");
    expect(prepared({ model: SUPER, messages, max_tokens: 50, max_completion_tokens: 300 }).maxTokens).toBe(50);
  });

  it("rejects a max_tokens that is not a positive integer", () => {
    for (const bad of [0, -1, 1.5, "100", true]) {
      const result = prepareRequest({ model: SUPER, messages, max_tokens: bad }, config);
      expect(result.ok).toBe(false);
      if (!result.ok) expect(result.status).toBe(400);
    }
  });
});

describe("models", () => {
  it("refuses non-nvidia and unlisted models with 403 and lists the allowed ones", () => {
    for (const model of ["gpt-4o", "meta-llama/Llama-3.3-70B-Instruct", "nvidia/nemotron-3-ultra-550b-a55b", "nvidia/unknown"]) {
      const result = prepareRequest({ model, messages }, config);
      expect(result.ok).toBe(false);
      if (!result.ok) {
        expect(result.status).toBe(403);
        expect(result.error.code).toBe("model_not_allowed");
        expect(result.error.allowed_models).toEqual([NANO, LIGHTNING, SUPER, ULTRA]);
      }
    }
  });

  it("needs a model and a non-empty message list", () => {
    expect(prepareRequest({ messages }, config)).toMatchObject({ ok: false, status: 400 });
    expect(prepareRequest({ model: SUPER, messages: [] }, config)).toMatchObject({ ok: false, status: 400 });
    expect(prepareRequest({ model: SUPER, messages: ["hi"] }, config)).toMatchObject({ ok: false, status: 400 });
    expect(prepareRequest([1, 2], config)).toMatchObject({ ok: false, status: 400 });
  });

  it("validates sampling and shape fields", () => {
    const bad: Record<string, unknown>[] = [
      { temperature: 3 },
      { temperature: "0" },
      { top_p: 0 },
      { top_p: 1.5 },
      { stream: "yes" },
      { tool_choice: "sometimes" },
      { response_format: { type: "yaml" } },
      { stop: [1, 2] },
      { tools: "f" },
    ];
    for (const extra of bad) {
      expect(prepareRequest({ model: SUPER, messages, ...extra }, config)).toMatchObject({ ok: false, status: 400 });
    }
  });
});

describe("the reservation estimate", () => {
  it("bounds prompt tokens by request bytes plus template overhead", () => {
    const e = config.estimate;
    expect(estimateInputTokens(1000, 2, 1, e)).toEqual({
      upper: 1000 + e.overheadTokens + 2 * e.perMessageTokens + e.perToolTokens,
      likely: 250 + e.overheadTokens + 2 * e.perMessageTokens + e.perToolTokens,
    });
    const out = prepared({ model: ULTRA, messages: [{ role: "user", content: "x".repeat(10_000) }] });
    expect(out.inputTokensUpper).toBeGreaterThan(10_000);
    expect(out.inputTokensUpper).toBe(out.payload.length + e.overheadTokens + e.perMessageTokens);
  });

  it("prices the worst case: upper prompt tokens plus max_tokens, at list price", () => {
    const ultra = config.models.get(ULTRA)!;
    // $1.00 / $3.00 per 1M: 1,000 n$ and 3,000 n$ a token.
    expect(reservationNano(ultra, 1000, 32768)).toBe(1000 * 1000 + 32768 * 3000);
    const lightning = config.models.get(LIGHTNING)!;
    expect(reservationNano(lightning, 700, 16)).toBe(700 * 60 + 16 * 240);
  });
});

describe("prompts the chat template renders larger than their bytes", () => {
  const e = config.estimate;
  const bytes = (value: unknown) => new TextEncoder().encode(JSON.stringify(value)).length;

  it("prices the tools list at two tokens a byte plus four tokens a key", () => {
    const tools = [{ type: "function", function: { name: "f", parameters: { type: "object", properties: { a: { type: "string" } }, extra: 1 } } }];
    // type, function, name, parameters, type, properties, a, type, extra
    expect(promptShape(tools, messages)).toMatchObject({ toolBytes: bytes(tools), toolKeys: 9, toolAstralChars: 0 });
    const out = prepared({ model: ULTRA, messages, tools });
    expect(out.inputTokensUpper).toBe(
      out.payload.length + e.overheadTokens + e.perMessageTokens + e.perToolTokens + bytes(tools) + 9 * e.perToolKeyTokens,
    );
  });

  it("counts characters outside the BMP in the tools list", () => {
    const tools = [{ type: "function", function: { name: "f", parameters: { type: "object", properties: { a: { type: ["\u{E0001}\u{E0001}", "\u{1F600}"] } } } } }];
    expect(promptShape(tools, messages).toolAstralChars).toBe(3);
  });

  it("prices every U+007F to U+07FF character in the tools list two tokens more, and nothing else", () => {
    // repr() spells U+007F as \x7f (4 bytes for 1) and a two-byte character such as U+0600 as
    // \u0600 (6 for 2). Below U+007F and from U+0800 on, it stays within 2 bytes a request byte.
    const inside = "\u007f\u0080\u00e9\u0378\u0434\u0600\u07ff"; // 7, printable ones included
    const outside = "a~\u0000\n\u0800\u2028\uffff\u{E0001}"; // ASCII, controls JSON escapes, 3- and 4-byte
    const tools = [{ type: "function", function: { name: "f", parameters: { type: "object", properties: { a: { description: [inside, outside] } } } } }];
    expect(promptShape(tools, messages)).toMatchObject({ toolEscapeChars: 7, toolAstralChars: 1 });
    const out = prepared({ model: ULTRA, messages, tools });
    expect(out.inputTokensUpper).toBe(
      out.payload.length +
        e.overheadTokens +
        e.perMessageTokens +
        e.perToolTokens +
        bytes(tools) +
        8 * e.perToolKeyTokens + // type, function, name, parameters, type, properties, a, description
        e.perToolAstralCharTokens +
        7 * e.perToolEscapeCharTokens,
    );
    expect(e.perToolEscapeCharTokens).toBeGreaterThanOrEqual(2);
    // The same characters in a message are printed as they are sent, so they cost nothing more.
    const plain = prepared({ model: ULTRA, messages: [{ role: "user", content: inside }] });
    expect(plain.inputTokensUpper).toBe(plain.payload.length + e.overheadTokens + e.perMessageTokens);
  });

  it("prices tool-call arguments again, plus each argument and each number in string-form arguments", () => {
    const args = '{"path":"a.py","n":1e15,"list":[1,2.5,-3e-7],"flag":true}';
    const turn = [{ role: "user", content: "x" }, { role: "assistant", content: "", tool_calls: [{ id: "1", type: "function", function: { name: "f", arguments: args } }] }];
    expect(promptShape(undefined, turn)).toMatchObject({ argumentBytes: bytes(args), argumentNames: 4, argumentNumbers: 4 });
    const out = prepared({ model: ULTRA, messages: turn });
    expect(out.inputTokensUpper).toBe(
      out.payload.length + e.overheadTokens + 2 * e.perMessageTokens + bytes(args) + 4 * e.perArgumentTokens + 4 * e.perArgumentNumberTokens,
    );
    // A mapping travels as the relay printed it, numbers spelled out: they need no extra.
    expect(promptShape(undefined, [{ role: "assistant", tool_calls: [{ function: { name: "f", arguments: { n: 1e15, m: 2 } } }] }])).toMatchObject({
      argumentNames: 2,
      argumentNumbers: 0,
    });
    // Only assistant messages carry tool calls the template prints.
    expect(promptShape(undefined, [{ role: "user", tool_calls: [{ function: { name: "f", arguments: args } }] }]).argumentBytes).toBe(0);
    // Arguments JavaScript cannot parse (Python reads NaN) are counted from their text.
    expect(promptShape(undefined, [{ role: "assistant", tool_calls: [{ function: { name: "f", arguments: '{"a":NaN,"b":[1e15,2]}' } }] }])).toMatchObject({
      argumentNames: 2,
      argumentNumbers: 2,
    });
  });

  it("refuses a body nested deeper than the limit", () => {
    let deep: unknown = "x";
    for (let i = 0; i < MAX_JSON_DEPTH; i += 1) deep = [deep];
    expect(withinDepth(deep, MAX_JSON_DEPTH)).toBe(true);
    expect(withinDepth({ a: deep }, MAX_JSON_DEPTH)).toBe(false);
    expect(prepareRequest({ model: SUPER, messages: [{ role: "user", content: deep }] }, config)).toMatchObject({
      ok: false,
      status: 400,
      error: { code: "too_deep" },
    });
  });

  it("keeps an ordinary tool-free prompt priced as before", () => {
    const out = prepared({ model: LIGHTNING, messages: [{ role: "user", content: "x".repeat(5000) }] });
    expect(out.inputTokensUpper).toBe(estimateInputTokens(out.payload.length, 1, 0, e).upper);
  });
});
