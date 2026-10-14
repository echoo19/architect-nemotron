/** Trusted shipped extension. It executes inside actual Pi's extension loader. */
import { Type } from "typebox";
import { realpathSync } from "node:fs";
import { dirname, basename, resolve, relative, isAbsolute, sep } from "node:path";
import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";

export default function (pi: ExtensionAPI) {
  const bridge = (globalThis as any)[Symbol.for("architect.pi.bridge")];
  if (!bridge) throw new Error("Missing bound Architect configuration");
  const { config, rpc } = bridge;
  // The operating system's own realpath: on Windows it also expands 8.3 short names
  // (TRUSTE~1.JSO), which the JavaScript realpathSync leaves as they are. On a volume
  // where it fails for an existing path, the JavaScript one is used; the ~digit rule in
  // privatePath still refuses short names there.
  function real(path: string): string {
    try {
      return realpathSync.native(path);
    } catch (error) {
      const code = (error as NodeJS.ErrnoException | undefined)?.code;
      if (code === "ENOENT" || code === "ENOTDIR") throw error;
      return realpathSync(path);
    }
  }
  // Compared without case on every platform: APFS and NTFS, the macOS and Windows
  // defaults, open a case variant of a missing tail (OPERATOR/TRUSTED-KEYS.JSON) as the
  // protected file. This only widens what is denied, as in Python's _granted_path.
  function canonical(path: string): string {
    let current = resolve(path);
    const missing: string[] = [];
    while (true) {
      try {
        return resolve(real(current), ...missing).toLowerCase();
      } catch {
        const parent = dirname(current);
        if (parent === current) throw new Error("Unresolvable tool path");
        missing.unshift(basename(current)); current = parent;
      }
    }
  }
  const privateRoots = config.denied_paths.map((path: string) => canonical(path));
  function privatePath(raw: unknown): boolean {
    if (typeof raw !== "string") return true;
    // A segment with ~ and a digit may be an 8.3 alias of a protected name, on any
    // volume that keeps such aliases.
    if (raw.split(/[\\/]/).some(part => /~\d/.test(part))) return true;
    // A UNC share, a loopback address, an extended-length prefix (\\?\C:\...) or a device
    // path (\\.\...) names the same tree as a drive path but keeps its own leading text,
    // so the realpath comparison below would miss a protected file reached that way. A
    // coding grant's repo is a plain drive path, so refuse any raw that opens with \\.
    if (process.platform === "win32" && /^[\\/]{2}/.test(raw)) return true;
    if (process.platform === "win32" && (raw.split(/[\\/]/).some(part => /[. ]$/.test(part)) ||
        raw.replace(/^[a-z]:/i, "").includes(":"))) return true;
    const candidate = canonical(resolve(config.repo, raw));
    return privateRoots.some((root: string) => {
      const suffix = relative(root, candidate);
      return suffix === "" || (!isAbsolute(suffix) && suffix !== ".." && !suffix.startsWith(".." + sep));
    });
  }
  pi.on("before_agent_start", event => ({
    systemPrompt: event.systemPrompt + "\n" + config.pack.prompt_fragment + "\n" +
      JSON.stringify(config.pack.subagent_rules ?? []) +
      "\nNo autonomous subagent spawning. compile_agent is the only nested compiler capability.",
  }));
  // The parent's context cap (pi.py _ContextCap), applied here first so the context the
  // bridge sends is no bigger than what the model will see; the parent applies the same
  // rule again to what arrives. Past max_characters, the oldest tool-result bodies are
  // dropped and the oldest write and edit texts and Forge call arguments trimmed, down to
  // the floor. The latest assistant turn and its results stay whole, and a removal holds
  // for the session.
  const parentCap = config.context_cap;
  const capLimit = parentCap?.max_characters;
  const capped = Number.isInteger(capLimit) && capLimit >= 1;
  const dropped = new Set<number>();
  const trimmed = new Set<string>();
  const joined = (content: any): string => typeof content === "string" ? content
    : Array.isArray(content) ? content.filter((item: any) => item?.type === "text" &&
      typeof item.text === "string").map((item: any) => item.text).join("\n") : "";
  // pi.py _BULKY_ARGUMENTS: argument values that count whole, a string by its length and
  // anything else by its compact JSON.
  const bulkyArguments: Record<string, string[]> = {
    forge_dryrun: ["replies"],
    forge_spawn: ["goal", "input_schema", "output_schema", "cases"],
  };
  type Bulky = { part: number, path: (string | number)[], size: number, note: string };
  // The same values, in the same order and with the same notes, as pi.py _bulky_strings.
  function bulkyStrings(message: any): Bulky[] {
    const found: Bulky[] = [];
    const content = Array.isArray(message?.content) ? message.content : [];
    content.forEach((part: any, index: number) => {
      const args = part?.arguments;
      if (part?.type !== "toolCall" || !args || typeof args !== "object" || Array.isArray(args))
        return;
      if (part.name === "write") {
        if (typeof args.content === "string")
          found.push({ part: index, path: ["content"], size: args.content.length, note: "trimmed" });
      } else if (part.name === "edit") {
        if (typeof args.edits === "string")
          found.push({ part: index, path: ["edits"], size: args.edits.length, note: "trimmed" });
        else if (Array.isArray(args.edits))
          args.edits.forEach((edit: any, position: number) => {
            for (const key of ["oldText", "newText"]) {
              const value = edit && typeof edit === "object" && !Array.isArray(edit) ? edit[key] : undefined;
              if (typeof value === "string")
                found.push({ part: index, path: ["edits", position, key], size: value.length, note: "trimmed" });
            }
          });
        for (const key of ["oldText", "newText"])
          if (typeof args[key] === "string")
            found.push({ part: index, path: [key], size: args[key].length, note: "trimmed" });
      } else if (typeof part.name === "string" && Object.hasOwn(bulkyArguments, part.name)) {
        for (const key of bulkyArguments[part.name]) {
          if (!Object.hasOwn(args, key)) continue;
          const value = args[key];
          const size = typeof value === "string" ? value.length : (JSON.stringify(value) ?? "").length;
          found.push({ part: index, path: [key], size, note: "trimmed_argument" });
        }
      }
    });
    return found;
  }
  const trimNote = (item: Bulky): string =>
    String(parentCap[item.note]).replace("{chars}", String(item.size));
  function replaced(value: any, path: (string | number)[], note: string): any {
    if (!path.length) return note;
    const [head, ...rest] = path;
    if (typeof head === "string" && value && typeof value === "object" && !Array.isArray(value) &&
        Object.hasOwn(value, head)) return { ...value, [head]: replaced(value[head], rest, note) };
    if (typeof head === "number" && Array.isArray(value) && head >= 0 && head < value.length)
      return value.map((item: any, at: number) => at === head ? replaced(item, rest, note) : item);
    return value;
  }
  function capContext(messages: any[]): any[] {
    const dropNote = String(parentCap.dropped);
    let latest = -1;
    messages.forEach((message, position) => { if (message?.role === "assistant") latest = position; });
    let total = 0;
    const removable: { position: number, key?: string, size: number, note: number }[] = [];
    messages.forEach((message, position) => {
      if (!message || typeof message !== "object") return;
      if (message.role === "toolResult") {
        const size = joined(message.content).length;
        if (dropped.has(position)) { total += dropNote.length; return; }
        total += size;
        if (position < latest && size > dropNote.length)
          removable.push({ position, size, note: dropNote.length });
      } else if (message.role === "assistant") {
        for (const item of bulkyStrings(message)) {
          const key = JSON.stringify([position, item.part, item.path]);
          const note = trimNote(item).length;
          if (trimmed.has(key)) { total += note; continue; }
          total += item.size;
          if (position < latest && item.size > note)
            removable.push({ position, key, size: item.size, note });
        }
      }
    });
    if (total > capLimit) {
      for (const item of removable) {
        if (total <= parentCap.floor) break;
        if (item.key === undefined) dropped.add(item.position); else trimmed.add(item.key);
        total -= item.size - item.note;
      }
    }
    if (!dropped.size && !trimmed.size) return messages;
    return messages.map((message, position) => {
      if (!message || typeof message !== "object") return message;
      if (message.role === "toolResult" && dropped.has(position))
        return { ...message, content: [{ type: "text", text: dropNote }] };
      if (message.role !== "assistant" || !Array.isArray(message.content)) return message;
      const items = bulkyStrings(message)
        .filter(item => trimmed.has(JSON.stringify([position, item.part, item.path])));
      if (!items.length) return message;
      const content = [...message.content];
      for (const item of items) {
        const part = content[item.part];
        content[item.part] = { ...part, arguments: replaced(part.arguments, item.path, trimNote(item)) };
      }
      return { ...message, content };
    });
  }
  pi.on("context", event => {
    if (capped) return { messages: capContext(event.messages as any[]) };
    const maximum = config.pack.context_policy?.max_characters;
    if (!Number.isInteger(maximum) || maximum < 1) return;
    // Preserve assistant/tool-call adjacency. Thin older tool payloads, not identities.
    let remaining = maximum;
    const messages = [...event.messages].reverse().map(message => {
      if (message.role !== "toolResult") return message;
      const text = message.content.filter(item => item.type === "text")
        .map(item => (item as any).text).join("\n");
      const content = [{ type: "text" as const, text: text.slice(0, remaining) }];
      remaining = Math.max(0, remaining - text.length);
      return { ...message, content };
    }).reverse();
    return { messages };
  });
  pi.on("tool_call", async event => {
    try {
      const allowed = await rpc("tool", { name: event.toolName, arguments: event.input });
      const privateFile = ["read", "write", "edit"].includes(event.toolName) &&
        privatePath(event.input.path);
      // Only true runs the tool. A string is the parent's own refusal (a missing file or a
      // directory, with what is there); anything else is the policy denial.
      const refusal = typeof allowed === "string" && !privateFile ? allowed : undefined;
      if (allowed !== true || privateFile)
        return { block: true, reason: refusal || "Tool denied by Architect policy" };
    } catch { bridge.fail(); return { block: true, reason: "Tool denied by Architect policy" }; }
  });
  pi.on("tool_result", event => ({
    content: [{ type: "text" as const, text: !config.tools.includes(event.toolName)
      ? "Tool denied by Architect policy"
      : event.content.filter(item => item.type === "text")
        .map(item => (item as any).text).join("\n").slice(0, config.max_tool_output_chars) }],
    details: undefined,
  }));
  if (config.tools.includes("compile_agent")) {
    pi.registerTool({ name: "compile_agent", label: "Compile approved agent",
      description: "Ask the trusted bounded compiler to propose or compile a feature. Cannot grant approval or supply reports.",
      parameters: Type.Object({ request: Type.String() }, { additionalProperties: false }),
      async execute(_id, params) {
        const result = await rpc("compile", { arguments: params });
        return { content: [{ type: "text", text: JSON.stringify(result) }], details: undefined };
      },
    });
  }
  // Host-trusted capabilities (Phase 9). Python sends only tools granted for this
  // run; TypeBox validates each call against the same JSON Schema before any RPC,
  // and Python re-validates the arguments on the "capability" request.
  const builtin = ["read", "write", "edit", "bash", "compile_agent"];
  for (const capability of config.capabilities ?? []) {
    const name = capability?.name;
    if (typeof name !== "string" || builtin.includes(name) || !config.tools.includes(name))
      throw new Error("Invalid bound capability");
    pi.registerTool({ name, label: name, description: String(capability.description),
      parameters: Type.Unsafe(capability.parameters),
      async execute(_id, params) {
        const result = await rpc("capability", { name, arguments: params });
        return { content: [{ type: "text", text: JSON.stringify(result) }], details: undefined };
      },
    });
  }
  // Evidence is true only after canonical roots and every handler/tool registered.
  bridge.loaded();
}
