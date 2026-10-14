// Pinned SDK owns the coding loop. This file only adapts guarded RPC and resources.
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";
import { readFileSync } from "node:fs";
import { spawn } from "node:child_process";
import { AssistantMessageEventStream } from "@mariozechner/pi-ai";
import { AuthStorage, createAgentSession, createBashTool, DefaultResourceLoader,
  SessionManager, SettingsManager } from "@mariozechner/pi-coding-agent";

// The desktop app runs this file on its Electron binary as Node (ELECTRON_RUN_AS_NODE=1).
// Commands the agent runs from here must not inherit that switch.
delete process.env.ELECTRON_RUN_AS_NODE;
const packageRoot = dirname(dirname(fileURLToPath(import.meta.resolve("@mariozechner/pi-coding-agent"))));
const { getShellConfig } = await import(pathToFileURL(join(packageRoot, "dist/utils/shell.js")).href);
const versions = Object.fromEntries(["pi-ai", "pi-agent-core", "pi-coding-agent"].map(name => {
  const root = dirname(dirname(fileURLToPath(import.meta.resolve(`@mariozechner/${name}`))));
  return [name, JSON.parse(readFileSync(join(root, "package.json"), "utf8")).version];
}));
if (Object.values(versions).some(version => version !== "0.73.1")) throw Error("Pi version mismatch");
let maxBytes = 1048576, buffer = Buffer.alloc(0), sequence = 0;
const pending = new Map();
let initialize;
const initialized = new Promise(resolve => { initialize = resolve; });
function send(message) {
  const line = JSON.stringify(message) + "\n";
  if (Buffer.byteLength(line) > maxBytes) throw Error("Pi protocol limit");
  process.stdout.write(line);
}
function rpc(kind, payload) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    try { send({ kind, id, ...payload }); } catch (error) { pending.delete(id); reject(error); }
  });
}
process.stdin.on("data", chunk => {
  buffer = Buffer.concat([buffer, chunk]);
  while (true) {
    const index = buffer.indexOf(10);
    if (index < 0) break;
    if (index > maxBytes) process.exit(2);
    const line = buffer.subarray(0, index); buffer = buffer.subarray(index + 1);
    let message;
    try { message = JSON.parse(line.toString("utf8")); } catch { process.exit(2); }
    if (message.kind === "init") initialize(message);
    else {
      const waiter = pending.get(message.id);
      if (!waiter) process.exit(2);
      pending.delete(message.id);
      message.error ? waiter.reject(Error("Parent denied request")) : waiter.resolve(message.value);
    }
  }
  if (buffer.length > maxBytes) process.exit(2);
});
process.stdin.on("end", () => process.exit(0));

const config = await initialized;
maxBytes = config.max_protocol_bytes;
// Trusted capability definitions are forwarded to the extension as sent; each one
// must also be a granted tool name so the pinned SDK activates it.
config.capabilities = config.capabilities ?? [];
if (!Array.isArray(config.capabilities) ||
  config.capabilities.some(item => !item || !config.tools.includes(item.name))) process.exit(2);
let fatal = false, extensionLoaded = false, session;
const bridge = {
  config, rpc,
  loaded() { extensionLoaded = true; },
  fail() { fatal = true; session?.agent.abort(); },
};
globalThis[Symbol.for("architect.pi.bridge")] = bridge;
const model = { id: "parent", name: "Architect guarded parent", api: "architect-rpc",
  provider: "architect", baseUrl: "", reasoning: false, input: ["text"],
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  // Virtual transport metadata only. Actual provider capacities and dollar quotes
  // live in Python; this bridge must not clip generation to a manufactured quota.
  contextWindow: Number.MAX_SAFE_INTEGER, maxTokens: Number.MAX_SAFE_INTEGER };

// Use the real Pi bash tool with bounded host process operations. Shell is an
// explicit trusted-host grant, not an OS sandbox. Inherit the parent's group/job.
const shellOperations = {
  exec(command, cwd, { onData, signal, timeout, env }) {
    return new Promise((resolve, reject) => {
      const { shell, args } = getShellConfig();
      const child = spawn(shell, [...args, command], { cwd, env, detached: false,
        stdio: ["ignore", "pipe", "pipe"] });
      let size = 0, stopped = false;
      const stop = () => { stopped = true; child.kill(); bridge.fail(); };
      const timer = timeout ? setTimeout(stop, timeout * 1000) : undefined;
      const data = chunk => {
        size += chunk.length;
        if (size > config.max_tool_output_chars * 4) stop();
        else onData(chunk);
      };
      child.stdout.on("data", data); child.stderr.on("data", data);
      signal?.addEventListener("abort", stop, { once: true });
      child.on("error", reject);
      child.on("close", code => {
        clearTimeout(timer); signal?.removeEventListener("abort", stop);
        stopped ? reject(Error("Shell limit")) : resolve({ exitCode: code });
      });
    });
  },
};
const clip = (value, limit) => String(value ?? "")
  .replace(/[\u0000-\u001f\u007f-\u009f]+/g, " ").trim().slice(0, limit);
const textOf = result => Array.isArray(result?.content)
  ? result.content.filter(item => item?.type === "text").map(item => String(item.text)).join("\n")
  : "";
const lastLine = text => text.split("\n").map(line => line.trim()).filter(Boolean).at(-1) ?? "";
const startedArgs = new Map();
function emitEvent(payload) {
  try { send({ kind: "event", ...payload }); } catch {}
}
function argumentSummary(name, args) {
  if (!args || typeof args !== "object") return "";
  if (name === "bash") return clip(args.command, 120);
  if (["read", "write", "edit"].includes(name)) return clip(args.path, 200);
  return "";
}
function resultSummary(name, args, result, isError) {
  const text = textOf(result);
  if (isError) return clip(name === "bash" ? lastLine(text) : text.split("\n")[0], 120);
  if (name === "write" && typeof args?.content === "string")
    return `${Buffer.byteLength(args.content)} bytes`;
  if (name === "edit" && typeof result?.details?.diff === "string") {
    let added = 0, removed = 0;
    for (const line of result.details.diff.split("\n")) {
      if (line.startsWith("+")) added++; else if (line.startsWith("-")) removed++;
    }
    return `+${added} -${removed} lines`;
  }
  if (name === "read") return `${text ? text.split("\n").length : 0} lines`;
  if (name === "bash") return clip(lastLine(text), 120);
  return "";
}
function observe(event) {
  if (event?.type === "tool_execution_start") {
    startedArgs.set(event.toolCallId, event.args);
    emitEvent({ event: "tool_start", tool: String(event.toolName).slice(0, 64),
      summary: argumentSummary(event.toolName, event.args) });
  } else if (event?.type === "tool_execution_end") {
    const args = startedArgs.get(event.toolCallId); startedArgs.delete(event.toolCallId);
    emitEvent({ event: "tool_end", tool: String(event.toolName).slice(0, 64), ok: !event.isError,
      summary: resultSummary(event.toolName, args, event.result, event.isError) });
  } else if (event?.type === "message_end" && event.message?.role === "assistant" &&
      Array.isArray(event.message.content) &&
      event.message.content.some(item => item?.type === "toolCall")) {
    // Narration next to tool calls only; the final answer arrives in "done".
    const text = clip(event.message.content.filter(item => item?.type === "text")
      .map(item => String(item.text)).join(" "), 400);
    if (text) emitEvent({ event: "text", text });
  }
}
try {
  const settings = SettingsManager.inMemory({ compaction: { enabled: false },
    retry: { enabled: false, maxRetries: 0 }, blockImages: true, enableSkillCommands: false });
  const loader = new DefaultResourceLoader({ cwd: config.repo, agentDir: config.agent_dir,
    settingsManager: settings, noExtensions: true, noSkills: true, noPromptTemplates: true,
    noThemes: true, noContextFiles: true, systemPrompt: "You are a coding agent. Use only granted tools.",
    additionalExtensionPaths: [fileURLToPath(new URL("extension.ts", import.meta.url))] });
  await loader.reload();
  if (loader.getExtensions().errors.length || !extensionLoaded) throw Error("Extension load failed");
  const result = await createAgentSession({ cwd: config.repo, agentDir: config.agent_dir,
    model, tools: config.tools, authStorage: AuthStorage.inMemory({ architect: { type: "api_key", key: "parent-rpc-only" } }), settingsManager: settings,
    sessionManager: SessionManager.inMemory(config.repo), resourceLoader: loader,
    customTools: config.tools.includes("bash") ? [createBashTool(config.repo, { operations: shellOperations })] : [] });
  session = result.session;
  await session.bindExtensions({ onError: () => bridge.fail() });
  if (fatal || result.extensionsResult.errors.length) throw Error("Extension binding failed");
  session.agent.toolExecution = "sequential";
  // One-way informational events for a live front end (Phase 12, Section 8), only when
  // the parent asks. Never an approval path: approvals stay on the "tool" request.
  // Best-effort and bounded: a failure here never affects the run.
  if (config.events === true) session.subscribe(event => { try { observe(event); } catch {} });
  session.agent.streamFn = (_model, context, options) => {
    const stream = new AssistantMessageEventStream();
    const message = { role: "assistant", content: [], api: model.api, provider: model.provider,
      model: model.id, usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
      stopReason: "stop", timestamp: Date.now() };
    (async () => {
      try {
        if (fatal || options?.signal?.aborted) throw Error("Aborted");
        const response = await rpc("model", { context });
        if (fatal || options?.signal?.aborted) throw Error("Aborted");
        let control;
        try { control = JSON.parse(response); } catch {}
        // Ordinary task JSON is final text. Only the exact control envelope is interpreted.
        const isControl = control && !Array.isArray(control) && typeof control === "object" &&
          Object.keys(control).every(key => ["text", "tool_calls"].includes(key)) &&
          (Object.hasOwn(control, "tool_calls") || typeof control.text === "string");
        if (isControl && control.tool_calls !== undefined) {
          if (!Array.isArray(control.tool_calls) || control.tool_calls.length > config.max_turns)
            throw Error("Invalid tool calls");
          const ids = new Set();
          for (const call of control.tool_calls) {
            if (!call || typeof call.id !== "string" || !call.id || ids.has(call.id) ||
              typeof call.name !== "string" || !call.arguments || Array.isArray(call.arguments) ||
              typeof call.arguments !== "object") throw Error("Invalid tool call");
            ids.add(call.id);
            message.content.push({ type: "toolCall", id: call.id, name: call.name, arguments: call.arguments });
          }
        }
        const text = isControl ? (control.text ?? "") : response;
        if (typeof text !== "string") throw Error("Invalid text");
        if (text) message.content.unshift({ type: "text", text });
        message.stopReason = message.content.some(item => item.type === "toolCall") ? "toolUse" : "stop";
        stream.push({ type: "start", partial: message });
        stream.push({ type: "done", reason: message.stopReason, message }); stream.end();
      } catch {
        fatal = true; message.stopReason = options?.signal?.aborted ? "aborted" : "error";
        message.errorMessage = "Guarded Pi model request failed";
        stream.push({ type: "error", reason: message.stopReason, error: message }); stream.end();
      }
    })();
    return stream;
  };
  send({ kind: "ready", versions, extension_loaded: extensionLoaded });
  // JSON prefix prevents Pi's user !command/extension-command prompt paths.
  await session.prompt(JSON.stringify({ task: config.task }), { expandPromptTemplates: false });
  const last = session.agent.state.messages.filter(message => message.role === "assistant").at(-1);
  if (fatal || !last || ["error", "aborted"].includes(last.stopReason)) throw Error("Pi run failed");
  const text = last.content.filter(item => item.type === "text").map(item => item.text).join("");
  session.dispose();
  send({ kind: "done", text });
  // Keep the tree root alive until the parent has received the result.
} catch {
  session?.dispose(); send({ kind: "failed" });
}
