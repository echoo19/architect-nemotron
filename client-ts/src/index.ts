/** JSON data cannot grant runtime capabilities. Tokens are usage, not budget fields. */
export type JsonValue = null | boolean | number | string | JsonValue[] | JsonObject;
export type JsonObject = { [key: string]: JsonValue };
export type Lifecycle = "install" | "expire";
export type Preset = "extractor" | "synthesizer" | "recommender" | "coder" |
  "explorer" | "planner" | "verifier" | "hypothesis_tester";
export interface Budget { cost_usd: number; compute_s: number }
export interface FloorDefinition {
  kind: "held_out_tasks" | "human_edit_rate" | "llm_judge" | "held_out_game_score";
  limit: number;
  dataset: string;
  minimum_samples?: number;
  rubric?: string | null;
  judge_model?: string | null;
  calibration_dataset?: string | null;
}
export interface PipelineStep {
  name: string;
  preset: string;
  model: string;
  budget: Budget;
  input_schema?: JsonObject;
  output_schema?: JsonObject;
  params?: JsonObject;
}
export interface Spec {
  task: string;
  input_schema: JsonObject;
  output_schema: JsonObject;
  floor: FloorDefinition;
  secondary_floor?: FloorDefinition | null;
  budget: Budget;
  model_target?: string;
  lifecycle?: Lifecycle;
  domain?: string;
  tools?: string[];
  pipeline?: PipelineStep[];
  provenance?: Record<string, "inferred" | "user">;
  preset_hint?: Preset | null;
  execution_config?: JsonObject;
}
export type CompileRequest =
  | { spec: Spec; request?: never; approved?: boolean }
  | { request: string; spec?: never; approved?: false };
export interface CompileResponse {
  accepted: boolean;
  approval_required: boolean;
  spec: Spec;
  artifact_id: string | null;
  failures: string[];
  stages: number[];
  warm_started: boolean;
}
export interface RunRequest {
  artifact_id: string;
  input: JsonValue;
  enabled?: boolean;
  human_reviewed?: boolean;
}
export interface RunResponse {
  run_id: string;
  output: JsonValue;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  wall_time_s: number;
}
export interface StatusResponse {
  artifact_id: string;
  licensed: boolean;
  expired: boolean;
  lifecycle: Lifecycle;
  spec_hash: string;
  version: number;
  registry_name?: string | null;
}
export interface RecompileRequest { artifact_id: string }
/** Phase 10 routing: advice over an explicit Spec. Routing never licenses; Stage 4 does. */
export type RouteAction = "reuse_installed" | "warm_start" | "compile" | "refuse";
export type FitTier = "A" | "B" | "C" | "none";
export interface RouteRequest { spec: Spec }
export interface RouteResponse {
  action: RouteAction;
  artifact_id: string | null;
  fit_tier: FitTier;
  fit: number;
  quality_lcb: number | null;
  expected_cost_usd: number | null;
  latency_s: number | null;
  latency_basis: "measured" | "bound" | null;
  evidence: string[];
  codes: string[];
}
export type ErrorCode = "INVALID_CONFIGURATION" | "INVALID_REQUEST" | "NETWORK_ERROR" |
  "INVALID_RESPONSE" | "UNAUTHORIZED" | "FORBIDDEN" | "NOT_FOUND" | "CONFLICT" |
  "REQUEST_REJECTED" | "SERVER_ERROR" | "HTTP_ERROR";
/** Fixed messages only. Never retains bearer tokens, URLs, bodies, or exception causes. */
export class ArchitectError extends Error {
  constructor(public readonly code: ErrorCode, public readonly status?: number) {
    super(code);
    this.name = "ArchitectError";
  }
}
const MAX_BYTES = 1_048_576;
const MAX_ID = 128;
const presets = new Set<string>([
  "extractor", "synthesizer", "recommender", "coder", "explorer", "planner", "verifier",
  "hypothesis_tester",
]);
function fail(): never { throw new ArchitectError("INVALID_REQUEST"); }
function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function keys(value: unknown, allowed: readonly string[], required: readonly string[] = []):
  asserts value is Record<string, unknown> {
  if (!object(value) || Object.keys(value).some(k => !allowed.includes(k)) ||
      required.some(k => !Object.hasOwn(value, k))) fail();
}
function nonempty(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}
function identifier(value: unknown): asserts value is string {
  if (!nonempty(value) || value.length > MAX_ID || /[\u0000-\u001f\u007f]/.test(value) ||
      value === "." || value === "..") fail();
  try { encodeURIComponent(value); } catch { fail(); }
}
function jsonData(value: unknown, seen = new Set<object>(), depth = 0): void {
  if (depth > 64) fail();
  if (value === null || typeof value === "string" || typeof value === "boolean") return;
  if (typeof value === "number") { if (!Number.isFinite(value)) fail(); return; }
  if (typeof value !== "object" || seen.has(value)) fail();
  const proto: unknown = Object.getPrototypeOf(value);
  if (!Array.isArray(value) && proto !== Object.prototype && proto !== null) fail();
  if (Object.getOwnPropertySymbols(value).length) fail();
  seen.add(value);
  if (Array.isArray(value)) {
    if (Object.keys(value).length !== value.length) fail();
    for (let i = 0; i < value.length; i++) {
      const descriptor = Object.getOwnPropertyDescriptor(value, String(i));
      if (!descriptor || !("value" in descriptor)) fail();
      jsonData(descriptor.value, seen, depth + 1);
    }
  } else {
    for (const descriptor of Object.values(Object.getOwnPropertyDescriptors(value))) {
      if (!("value" in descriptor) || !descriptor.enumerable) fail();
      jsonData(descriptor.value, seen, depth + 1);
    }
  }
  seen.delete(value);
}
function budget(value: unknown): void {
  keys(value, ["cost_usd", "compute_s"]);
  for (const v of Object.values(value)) if (typeof v !== "number" || v <= 0) fail();
}
function floor(value: unknown): void {
  keys(value, ["kind", "limit", "dataset", "minimum_samples", "rubric", "judge_model",
    "calibration_dataset"], ["kind", "limit", "dataset"]);
  if (!["held_out_tasks", "human_edit_rate", "llm_judge", "held_out_game_score"].includes(String(value.kind)) ||
      typeof value.limit !== "number" || value.limit < 0 || value.limit > 1 || !nonempty(value.dataset)) fail();
  if (value.minimum_samples !== undefined &&
      (!Number.isInteger(value.minimum_samples) || Number(value.minimum_samples) <= 0)) fail();
  for (const key of ["rubric", "judge_model", "calibration_dataset"]) {
    if (value[key] !== undefined && value[key] !== null && !nonempty(value[key])) fail();
  }
}
function spec(value: unknown): void {
  keys(value, ["task", "input_schema", "output_schema", "floor", "secondary_floor", "budget",
    "model_target", "lifecycle", "domain", "tools", "pipeline", "provenance", "preset_hint",
    "execution_config"], ["task", "input_schema", "output_schema", "floor", "budget"]);
  if (!nonempty(value.task) || !object(value.input_schema) || !object(value.output_schema)) fail();
  floor(value.floor); budget(value.budget);
  if (value.secondary_floor !== undefined && value.secondary_floor !== null) floor(value.secondary_floor);
  for (const key of ["model_target", "domain"]) {
    if (value[key] !== undefined && !nonempty(value[key])) fail();
  }
  if (value.lifecycle !== undefined && value.lifecycle !== "install" && value.lifecycle !== "expire") fail();
  if (value.preset_hint !== undefined && value.preset_hint !== null && !presets.has(String(value.preset_hint))) fail();
  if (value.execution_config !== undefined && !object(value.execution_config)) fail();
  if (value.tools !== undefined && (!Array.isArray(value.tools) || !value.tools.every(nonempty))) fail();
  if (value.provenance !== undefined && (!object(value.provenance) ||
      Object.values(value.provenance).some(v => v !== "inferred" && v !== "user"))) fail();
  if (value.pipeline !== undefined) {
    if (!Array.isArray(value.pipeline) || value.pipeline.length > 16) fail();
    const names = new Set<string>();
    for (const step of value.pipeline) {
      keys(step, ["name", "preset", "model", "budget", "input_schema", "output_schema", "params"],
        ["name", "preset", "model", "budget"]);
      if (!nonempty(step.name) || !nonempty(step.preset) || !nonempty(step.model) || names.has(step.name)) fail();
      names.add(step.name); budget(step.budget);
      for (const key of ["input_schema", "output_schema", "params"]) {
        if (step[key] !== undefined && !object(step[key])) fail();
      }
    }
  }
}
function booleanFields(value: Record<string, unknown>, fields: string[], optional = false): void {
  for (const field of fields) if (!(optional && !Object.hasOwn(value, field)) && typeof value[field] !== "boolean") fail();
}
function response(value: unknown, operation: string): void {
  jsonData(value);
  if (operation === "compile" || operation === "recompile") {
    const fields = ["accepted", "approval_required", "spec", "artifact_id", "failures", "stages", "warm_started"];
    keys(value, fields, fields);
    booleanFields(value, ["accepted", "approval_required", "warm_started"]);
    spec(value.spec);
    if (value.artifact_id !== null) identifier(value.artifact_id);
    if (!Array.isArray(value.failures) || !value.failures.every(v => typeof v === "string" && v.length <= 256) ||
        !Array.isArray(value.stages) || !value.stages.every(v => Number.isInteger(v) && v >= 0 && v <= 4)) fail();
    if (value.approval_required && (value.artifact_id !== null || value.accepted)) fail();
  } else if (operation === "route") {
    const fields = ["action", "artifact_id", "fit_tier", "fit", "quality_lcb", "expected_cost_usd",
      "latency_s", "latency_basis", "evidence", "codes"];
    keys(value, fields, fields);
    if (!["reuse_installed", "warm_start", "compile", "refuse"].includes(String(value.action)) ||
        !["A", "B", "C", "none"].includes(String(value.fit_tier)) ||
        typeof value.fit !== "number" ||
        (value.latency_basis !== null && value.latency_basis !== "measured" &&
          value.latency_basis !== "bound")) fail();
    if (value.artifact_id !== null) identifier(value.artifact_id);
    for (const key of ["quality_lcb", "expected_cost_usd", "latency_s"]) {
      if (value[key] !== null && typeof value[key] !== "number") fail();
    }
    for (const key of ["evidence", "codes"]) {
      const list = value[key];
      if (!Array.isArray(list) || !list.every(v => typeof v === "string" && /^[a-z0-9_]{1,64}$/.test(v))) fail();
    }
  } else if (operation === "run") {
    const fields = ["run_id", "output", "tokens_in", "tokens_out", "cost_usd", "wall_time_s"];
    keys(value, fields, fields); identifier(value.run_id);
    for (const key of ["tokens_in", "tokens_out", "cost_usd", "wall_time_s"]) {
      if (typeof value[key] !== "number" || value[key] < 0) fail();
    }
    if (!Number.isInteger(value.tokens_in) || !Number.isInteger(value.tokens_out)) fail();
  } else {
    keys(value, ["artifact_id", "licensed", "expired", "lifecycle", "spec_hash", "version", "registry_name"],
      ["artifact_id", "licensed", "expired", "lifecycle", "spec_hash", "version"]);
    identifier(value.artifact_id); booleanFields(value, ["licensed", "expired"]);
    if ((value.lifecycle !== "install" && value.lifecycle !== "expire") ||
        typeof value.spec_hash !== "string" || !/^[0-9a-f]{64}$/.test(value.spec_hash) ||
        !Number.isInteger(value.version) || Number(value.version) < 1 ||
        (value.registry_name !== undefined && value.registry_name !== null && !nonempty(value.registry_name))) fail();
  }
}
function httpCode(status: number): ErrorCode {
  if (status === 401) return "UNAUTHORIZED";
  if (status === 403) return "FORBIDDEN";
  if (status === 404) return "NOT_FOUND";
  if (status === 409) return "CONFLICT";
  if (status >= 500) return "SERVER_ERROR";
  if (status >= 400) return "REQUEST_REJECTED";
  return "HTTP_ERROR";
}
/** Trusted backend client. No implicit approval, retries, redirects, or deployment workflow. */
export interface ArchitectClientOptions {
  /** Per-request deadline in milliseconds; must exceed the server profile's compile budget. */
  timeoutMs?: number;
}
const DEFAULT_TIMEOUT_MS = 120_000;
const MAX_TIMEOUT_MS = 86_400_000;
export class ArchitectClient {
  #baseUrl: string;
  #token: string;
  #timeoutMs: number;
  constructor(baseUrl: string, bearerToken: string, options: ArchitectClientOptions = {}) {
    try {
      const url = new URL(baseUrl);
      if (!["http:", "https:"].includes(url.protocol) || url.username || url.password ||
          url.search || url.hash || !/^[\x21-\x7e]+$/.test(bearerToken)) throw new Error();
      if (typeof options !== "object" || options === null || Array.isArray(options)) throw new Error();
      keys(options, ["timeoutMs"]);
      const timeoutMs: unknown = Object.hasOwn(options, "timeoutMs") ? options.timeoutMs : DEFAULT_TIMEOUT_MS;
      if (typeof timeoutMs !== "number" || !Number.isSafeInteger(timeoutMs) || timeoutMs < 1 ||
          timeoutMs > MAX_TIMEOUT_MS) throw new Error();
      this.#baseUrl = url.href.replace(/\/+$/, "");
      this.#token = bearerToken;
      this.#timeoutMs = timeoutMs;
    } catch { throw new ArchitectError("INVALID_CONFIGURATION"); }
  }
  async compile(request: CompileRequest): Promise<CompileResponse> {
    return this.#send("compile", request) as Promise<CompileResponse>;
  }
  async run(request: RunRequest): Promise<RunResponse> {
    return this.#send("run", request) as Promise<RunResponse>;
  }
  async status(artifactId: string): Promise<StatusResponse> {
    return this.#send("status", { artifact_id: artifactId }) as Promise<StatusResponse>;
  }
  async recompile(request: RecompileRequest): Promise<CompileResponse> {
    return this.#send("recompile", request) as Promise<CompileResponse>;
  }
  /** Advice only: $0, nothing runs, reuse is never granted over HTTP. */
  async route(spec: Spec): Promise<RouteResponse> {
    return this.#send("route", { spec }) as Promise<RouteResponse>;
  }
  async #send(operation: string, request: unknown): Promise<unknown> {
    let body: string;
    let path: string;
    try {
      jsonData(request);
      if (operation === "compile") {
        keys(request, ["request", "spec", "approved"]);
        if (Object.hasOwn(request, "request") === Object.hasOwn(request, "spec")) fail();
        booleanFields(request, ["approved"], true);
        if (Object.hasOwn(request, "spec")) spec(request.spec);
        else if (!nonempty(request.request) || request.approved === true) fail();
      } else if (operation === "route") {
        keys(request, ["spec"], ["spec"]);
        spec(request.spec);
      } else {
        keys(request, operation === "run" ? ["artifact_id", "input", "enabled", "human_reviewed"] : ["artifact_id"],
          operation === "run" ? ["artifact_id", "input"] : ["artifact_id"]);
        identifier(request.artifact_id);
        if (operation === "run") booleanFields(request, ["enabled", "human_reviewed"], true);
      }
      body = JSON.stringify(request);
      if (new TextEncoder().encode(body).byteLength > MAX_BYTES) fail();
      path = operation === "status" ? `status/${encodeURIComponent(String(request.artifact_id))}` : operation;
    } catch { throw new ArchitectError("INVALID_REQUEST"); }
    let result: Response;
    try {
      result = await fetch(`${this.#baseUrl}/${path}`, {
        method: operation === "status" ? "GET" : "POST",
        headers: { Authorization: `Bearer ${this.#token}`, Accept: "application/json",
          ...(operation === "status" ? {} : { "Content-Type": "application/json" }) },
        ...(operation === "status" ? {} : { body }),
        redirect: "error",
        signal: AbortSignal.timeout(this.#timeoutMs),
      });
    } catch { throw new ArchitectError("NETWORK_ERROR"); }
    if (!result.ok) {
      try { await result.body?.cancel(); } catch { /* Never expose response errors. */ }
      throw new ArchitectError(httpCode(result.status), result.status);
    }
    try {
      const reader = result.body?.getReader();
      if (!reader) throw new Error();
      const chunks: Uint8Array[] = [];
      let total = 0;
      try {
        while (true) {
          const chunk = await reader.read();
          if (chunk.done) break;
          total += chunk.value.byteLength;
          if (total > MAX_BYTES) throw new Error();
          chunks.push(chunk.value);
        }
      } finally { await reader.cancel(); reader.releaseLock(); }
      const data = new Uint8Array(total);
      let offset = 0;
      for (const chunk of chunks) { data.set(chunk, offset); offset += chunk.byteLength; }
      const parsed: unknown = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(data));
      response(parsed, operation);
      return parsed;
    } catch { throw new ArchitectError("INVALID_RESPONSE", result.status); }
  }
}
