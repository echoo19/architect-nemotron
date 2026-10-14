/**
 * Folding bridge messages into the conversation the page shows.
 *
 * The rule from the brief: tool use and progress are visible but never bury the
 * conversation. Every turn's work (tool calls, subagent lines, progress) collects
 * into one `work` item that stays open while it runs and folds to a single line
 * when the turn ends; the answer reads straight after it.
 */

export type Level = 'info' | 'warn' | 'error';

export interface Step {
  id: string;
  callId?: string;
  name: string;
  summary: string;
  state: 'running' | 'ok' | 'error';
  result?: string;
  lines: Array<{ agent: string; text: string }>;
  /** A subagent's own line (no tool was running): `name` is the subagent. */
  subagent?: boolean;
}

export interface Ask {
  id: number;
  kind: 'question' | 'approval' | 'secret';
  question?: string;
  default?: string | null;
  choices?: string[];
  action?: string;
  detail?: string;
}

export type Item =
  | { kind: 'user'; key: string; text: string; at?: number; steered?: boolean }
  | { kind: 'answer'; key: string; text: string; streaming: boolean; meta?: { seconds?: number; cost?: number; at?: number } }
  | { kind: 'work'; key: string; steps: Step[]; progress: string | null; progressLog: string[]; running: boolean; started: number; ended?: number }
  | { kind: 'notice'; key: string; level: Level; text: string }
  /** `data`: the same card as values (hashes, the bar, the ladder; on a result card the signed exam counts). */
  | { kind: 'plan'; key: string; title: string; rows: Array<[string, string]>; footer: string; data?: Record<string, unknown> }
  | { kind: 'rows'; key: string; title: string; rows: Array<[string, string]> }
  | { kind: 'text'; key: string; text: string; style: string }
  | { kind: 'diff'; key: string; text: string }
  | { kind: 'ask'; key: string; ask: Ask; answer?: string | null; closed: boolean };

export interface Timeline {
  items: Item[];
  /** Who is thinking right now ("architect", "coder", ...), or null. */
  thinking: string | null;
  running: boolean;
  seq: number;
}

export const emptyTimeline = (): Timeline => ({ items: [], thinking: null, running: false, seq: 0 });

type Msg = Record<string, unknown>;

function key(t: Timeline, prefix: string): string {
  t.seq += 1;
  return `${prefix}${t.seq}`;
}

/** "stage 2 tune" for a build's stage line ("stage 2 tune: 40 calls, $0.02 (...)"), else null. */
function stageHead(text: string): string | null {
  return /^stage \d [a-z ]+?(?=:)/i.exec(text.trim())?.[0] ?? null;
}

function openWork(t: Timeline, now: number): Extract<Item, { kind: 'work' }> {
  // Work joins the open group of this turn unless something the reader must see
  // (an answer, a notice, a card, a question) came after it.
  const last = t.items.at(-1);
  if (last && last.kind === 'work' && last.running) return last;
  // Earlier groups of this turn stop showing as active; their tools keep their state
  // (a long tool like a build finishes into the group it started in).
  t.items = t.items.map((item) => (item.kind === 'work' && item.running ? { ...item, running: false, ended: now, progress: null } : item));
  const work: Extract<Item, { kind: 'work' }> = {
    kind: 'work', key: key(t, 'w'), steps: [], progress: null, progressLog: [], running: true, started: now,
  };
  t.items.push(work);
  return work;
}

function streamingAnswer(t: Timeline): Extract<Item, { kind: 'answer' }> | null {
  for (let i = t.items.length - 1; i >= 0; i -= 1) {
    const item = t.items[i];
    if (item.kind === 'answer' && item.streaming) return item;
    if (item.kind === 'user') return null;
  }
  return null;
}

/** The answer of the latest turn, if that turn ended in one. */
function lastAnswer(t: Timeline): Extract<Item, { kind: 'answer' }> | null {
  for (let i = t.items.length - 1; i >= 0; i -= 1) {
    const item = t.items[i];
    if (item.kind === 'answer') return item;
    if (item.kind === 'user') return null;
  }
  return null;
}

/**
 * The newest question with this id. A reopened conversation keeps the questions an earlier
 * bridge process asked, and each process numbers its own from 1.
 */
function askIndex(t: Timeline, id: unknown): number {
  return t.items.findLastIndex((i) => i.kind === 'ask' && i.ask.id === id);
}

function closeWork(t: Timeline, now: number): void {
  for (const item of t.items) {
    if (item.kind === 'work' && item.running) {
      item.running = false;
      item.ended = now;
      item.progress = null;
      for (const step of item.steps) if (step.state === 'running') step.state = 'error';
    }
  }
}

/** Apply one bridge message. Mutates and returns a fresh shell so React sees a change. */
export function fold(previous: Timeline, msg: Msg, now = Date.now()): Timeline {
  const t: Timeline = { ...previous, items: [...previous.items] };
  // Items about to change are copied so earlier renders keep their own objects.
  const touch = <I extends Item>(item: I): I => {
    const index = t.items.indexOf(item);
    const copy = { ...item, ...(item.kind === 'work' ? { steps: item.steps.map((s) => ({ ...s, lines: [...s.lines] })) } : {}) } as I;
    if (index !== -1) t.items[index] = copy;
    return copy;
  };
  const type = msg.type;
  if (type === 'turn_started') {
    t.running = true;
    t.items.push({ kind: 'user', key: key(t, 'u'), text: String(msg.text ?? ''), ...(typeof msg.at === 'number' ? { at: msg.at * 1000 } : {}) });
    return t;
  }
  if (type === 'steered') {
    // Sent while the turn ran; read now, after the step that was underway.
    t.items = t.items.map((item) => (item.kind === 'work' && item.running ? touch(item) : item));
    closeWork(t, now);
    t.items.push({ kind: 'user', key: key(t, 'u'), text: String(msg.text ?? ''), steered: true, ...(typeof msg.at === 'number' ? { at: msg.at * 1000 } : {}) });
    return t;
  }
  if (type === 'turn_finished') {
    t.running = false;
    t.thinking = null;
    t.items = t.items.map((item) => (item.kind === 'work' && item.running ? touch(item) : item));
    closeWork(t, now);
    const answer = streamingAnswer(t);
    if (answer) touch(answer).streaming = false;
    const last = lastAnswer(t);
    if (last && !msg.quiet && typeof msg.seconds === 'number') {
      const copy = touch(last);
      copy.meta = { ...copy.meta, seconds: msg.seconds, ...(typeof msg.at === 'number' ? { at: msg.at * 1000 } : {}) };
    }
    return t;
  }
  if (type === 'turn_meta') {
    const last = lastAnswer(t);
    if (!last) return previous;
    const copy = touch(last);
    copy.meta = { ...copy.meta, cost: Number(msg.cost ?? 0) };
    return t;
  }
  if (type === 'ask') {
    const ask = msg.ask as Ask;
    t.items.push({ kind: 'ask', key: `a${ask.id}`, ask, closed: false });
    return t;
  }
  if (type === 'ask_closed') {
    // The bridge says what was answered (never a secret), so a replayed card keeps it.
    const index = askIndex(t, msg.id);
    if (index !== -1) {
      const item = t.items[index] as Extract<Item, { kind: 'ask' }>;
      const said = 'answer' in msg && item.answer === undefined && item.ask.kind !== 'secret' && (typeof msg.answer === 'string' || msg.answer === null);
      t.items[index] = { ...item, closed: true, ...(said ? { answer: msg.answer as string | null } : {}) };
    }
    return t;
  }
  if (type === 'answered') {
    // Local echo of what the user chose, so the closed card says it in the past tense.
    const index = askIndex(t, msg.id);
    if (index !== -1) t.items[index] = { ...(t.items[index] as Extract<Item, { kind: 'ask' }>), answer: msg.value as string | null };
    return t;
  }
  if (type !== 'event') return previous;
  const event = msg.event as Msg;
  switch (event.kind) {
    case 'thinking':
      t.thinking = event.active ? String(event.label ?? 'architect') : null;
      return t;
    case 'delta': {
      const text = String(event.text ?? '');
      if (!text) return previous;
      const answer = streamingAnswer(t);
      if (answer) touch(answer).text += text;
      else t.items.push({ kind: 'answer', key: key(t, 'r'), text, streaming: true });
      return t;
    }
    case 'answer': {
      const text = String(event.text ?? '');
      const answer = streamingAnswer(t);
      if (answer) {
        const copy = touch(answer);
        copy.text = text || copy.text;
        copy.streaming = false;
      } else if (text.trim()) {
        t.items.push({ kind: 'answer', key: key(t, 'r'), text, streaming: false });
      }
      return t;
    }
    case 'tool_started': {
      // Words streamed before a tool call end there: what the model says after the tools
      // starts a new block below them instead of running on ("...agent.Now I'll").
      const open = streamingAnswer(t);
      if (open) touch(open).streaming = false;
      const work = touch(openWork(t, now));
      work.steps.push({
        id: key(t, 's'), callId: String(event.call_id ?? ''), name: String(event.name ?? ''),
        summary: String(event.summary ?? ''), state: 'running', lines: [],
      });
      return t;
    }
    case 'tool_finished': {
      for (let i = t.items.length - 1; i >= 0; i -= 1) {
        const item = t.items[i];
        if (item.kind !== 'work') continue;
        const index = item.steps.findIndex((s) => s.callId === event.call_id && s.state === 'running');
        if (index === -1) continue;
        const work = touch(item);
        work.steps[index].state = event.ok ? 'ok' : 'error';
        work.steps[index].result = String(event.summary ?? '');
        return t;
      }
      const work = touch(openWork(t, now));
      work.steps.push({
        id: key(t, 's'), callId: String(event.call_id ?? ''), name: String(event.name ?? ''),
        summary: String(event.summary ?? ''), state: event.ok ? 'ok' : 'error',
        result: String(event.summary ?? ''), lines: [],
      });
      return t;
    }
    case 'subagent': {
      const work = touch(openWork(t, now));
      const line = { agent: String(event.agent ?? ''), text: String(event.text ?? '') };
      const running = [...work.steps].reverse().find((s) => s.state === 'running');
      if (running) running.lines.push(line);
      else work.steps.push({ id: key(t, 's'), name: line.agent, summary: line.text, state: 'ok', lines: [], subagent: true });
      return t;
    }
    case 'progress': {
      const work = touch(openWork(t, now));
      const text = String(event.text ?? '');
      const log = work.progressLog;
      // A running build stage repeats its line with fresh counts: keep only the latest.
      const head = stageHead(text);
      const again = head !== null && log.length > 0 && stageHead(log[log.length - 1]) === head;
      work.progress = text;
      work.progressLog = again ? [...log.slice(0, -1), text] : [...log, text].slice(-200);
      return t;
    }
    case 'notice':
      t.items.push({ kind: 'notice', key: key(t, 'n'), level: (event.level as Level) ?? 'info', text: String(event.text ?? '') });
      return t;
    case 'plan_card':
      t.items.push({
        kind: 'plan', key: key(t, 'p'), title: String(event.title ?? ''),
        rows: (event.rows as Array<[string, string]>) ?? [], footer: String(event.footer ?? ''),
        ...(event.data && typeof event.data === 'object' ? { data: event.data as Record<string, unknown> } : {}),
      });
      return t;
    case 'rows':
      t.items.push({ kind: 'rows', key: key(t, 'g'), title: String(event.title ?? ''), rows: (event.rows as Array<[string, string]>) ?? [] });
      return t;
    case 'text':
      t.items.push({ kind: 'text', key: key(t, 't'), text: String(event.text ?? ''), style: String(event.style ?? '') });
      return t;
    case 'diff':
      t.items.push({ kind: 'diff', key: key(t, 'd'), text: String(event.text ?? '') });
      return t;
    default:
      return previous;
  }
}

/**
 * A saved transcript (the bridge's ``history``) as items, for a resumed conversation; after
 * `start`'s items when given (a timeline whose turns have ended), numbered on from them.
 */
export function fromHistory(messages: Msg[], start: Timeline = emptyTimeline()): Timeline {
  const t: Timeline = { ...start, items: [...start.items] };
  const calls = new Map<string, Step>();
  for (const message of messages) {
    const role = message.role;
    const text = String(message.text ?? '');
    if (role === 'user') {
      closeWork(t, 0);
      t.items.push({ kind: 'user', key: key(t, 'u'), text: displayUser(text) });
    } else if (role === 'assistant') {
      const toolCalls = (message.tool_calls as Msg[] | undefined) ?? [];
      if (toolCalls.length) {
        const work = openWork(t, 0);
        for (const call of toolCalls) {
          const step: Step = {
            id: key(t, 's'), callId: String(call.id ?? ''), name: String(call.name ?? call.tool ?? 'tool'),
            summary: summarizeArgs(call.args ?? call.arguments), state: 'ok', lines: [],
          };
          work.steps.push(step);
          if (step.callId) calls.set(step.callId, step);
        }
      }
      if (text.trim()) {
        closeWork(t, 0);
        t.items.push({ kind: 'answer', key: key(t, 'r'), text, streaming: false });
      }
    } else if (role === 'tool') {
      const step = calls.get(String(message.call_id ?? ''));
      if (step) {
        step.state = message.ok === false ? 'error' : 'ok';
        step.result = text.length > 400 ? `${text.slice(0, 400)}…` : text;
      }
    }
  }
  closeWork(t, 0);
  return t;
}

function displayUser(text: string): string {
  // Attached files are inlined into the saved message; show what the person typed.
  const cut = text.indexOf('\n\n<file ');
  return cut === -1 ? text : text.slice(0, cut);
}

function summarizeArgs(args: unknown): string {
  if (!args || typeof args !== 'object') return '';
  const values = Object.values(args as Record<string, unknown>).filter((v) => typeof v === 'string') as string[];
  const first = values[0] ?? '';
  return first.length > 80 ? `${first.slice(0, 80)}…` : first;
}

/** The fold line for finished work: "Used 4 tools in 12s". */
export function workLabel(item: Extract<Item, { kind: 'work' }>): string {
  const still = item.steps.find((s) => s.state === 'running');
  if (still) return still.name === 'make' || still.name === 'forge' ? 'Building the agent' : still.name === 'code' ? 'The coder is working' : `Still running: ${toolName(still.name)}`;
  const count = item.steps.length;
  const failed = item.steps.filter(failedStep).length;
  const seconds = item.ended && item.started ? Math.max(1, Math.round((item.ended - item.started) / 1000)) : 0;
  const only = item.steps[0];
  // An installed agent's run (Try it, /run) says so: "Ran receipt-reader".
  const ran = count === 1 && only.subagent && /^running v\d+ on /.test(only.summary);
  // Build mode's compile, from its own lines (a /make command has no tool step).
  const built = item.steps.some((s) => s.subagent && s.name === 'compiler');
  const what = ran ? `Ran ${only.name}` : built ? 'Built the agent' : count === 1 ? capitalize(isBuild(only) ? buildPhrase(only) : pastTense(only.name)) : `Used ${count} tools`;
  // A build refused before it ran took no time of its own.
  const took = seconds && !(count === 1 && skippedBuild(only)) ? ` in ${duration(seconds)}` : '';
  const problems = failed ? `, ${failed} failed` : '';
  return count === 0 ? `Worked${took}` : `${what}${took}${problems}`;
}

/**
 * Build steps (make, forge) are read from their result: the tool's JSON, from the event's
 * summary or the saved transcript. Build mode answers `{"cancelled": true, "stage": ...}`
 * when the person sets the plan aside (Not now, or a question cancelled), and the
 * conversation refuses a second build in one turn with `{"error": "one_build_per_turn"}`
 * before it runs. Neither built anything, and neither is a failure.
 */
const SET_ASIDE_RESULT = /^\{\s*"cancelled":\s*true\b/;
const SKIPPED_RESULT = /^\{\s*"error":\s*"one_build_per_turn"/;
const STOPPED_RESULT = /^\{\s*"licensed":\s*false,\s*"compiled":\s*false\b/;

function isBuild(step: Step): boolean {
  return !step.subagent && (step.name === 'make' || step.name === 'forge');
}

function skippedBuild(step: Step): boolean {
  return isBuild(step) && step.state === 'error' && SKIPPED_RESULT.test(step.result ?? '');
}

/** A step that failed. A second build refused before it ran did not fail. */
export function failedStep(step: Step): boolean {
  return step.state === 'error' && !skippedBuild(step);
}

/** What a finished build step did: only a build that ran "built the agent". */
function buildPhrase(step: Step): string {
  if (skippedBuild(step)) return 'skipped a second build';
  if (step.state === 'error') return 'tried to build the agent';
  const result = step.result ?? '';
  if (SET_ASIDE_RESULT.test(result)) return /"stage":\s*"interview"/.test(result) ? 'asked about the agent' : 'planned the agent';
  // A build that stopped before anything was compiled (no cases could be drafted, a
  // blocked plan) built nothing. One that compiled and was refused did build an agent.
  if (STOPPED_RESULT.test(result)) return 'tried to build the agent';
  return 'built the agent';
}

/** A build step's own title: "Building the agent" while it runs, then what it did. */
export function buildTitle(step: Step): string {
  return step.state === 'running' ? 'Building the agent' : capitalize(buildPhrase(step));
}

export function duration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function capitalize(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

const PAST: Record<string, string> = {
  read_file: 'read a file',
  list_dir: 'looked in a folder',
  glob: 'found files',
  grep: 'searched the files',
  code: 'the coder made changes',
  web_fetch: 'fetched a page',
  make: 'built the agent',
  forge: 'built the agent',
  spawn: 'ran a subagent',
  agents: 'listed your agents',
  report: 'read an agent report',
  run_agent: 'ran an agent',
  // Build mode's own lines, when they are a turn's only step.
  floor: 'set up the practice and exam cases',
  compiler: 'built the agent',
};

/**
 * A subagent's line, titled by what it does (build mode names them floor, compiler,
 * forge). The compiler's line is the build itself: the app says build, never compile.
 */
const SUBAGENT_TITLES: Record<string, string> = {
  floor: 'Cases',
  compiler: 'Build',
  forge: 'Forge',
  coder: 'The coder',
};

export function subagentTitle(name: string): string {
  return SUBAGENT_TITLES[name] ?? name.charAt(0).toUpperCase() + name.slice(1);
}

/** One finished tool as a past-tense phrase: "looked in a folder". */
export function pastTense(name: string): string {
  return PAST[name] ?? `used ${toolName(name)}`;
}

const TOOL_NAMES: Record<string, string> = {
  read_file: 'read file',
  list_dir: 'list folder',
  grep: 'search files',
  glob: 'find files',
  code: 'coder',
  web_fetch: 'fetch page',
  make: 'build agent',
  forge: 'build agent',
  spawn: 'subagent',
  agents: 'list agents',
  report: 'agent report',
  run_agent: 'run agent',
};

export function toolName(name: string): string {
  if (name.startsWith('mcp__')) return name.split('__').slice(1).join(' ');
  return TOOL_NAMES[name] ?? name.replace(/_/g, ' ');
}
