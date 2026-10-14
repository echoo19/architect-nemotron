/**
 * What the Forge panel shows, derived from the conversation.
 *
 * Build mode already tells its front end everything in order (docs/discovery/forge.md
 * §6): progress lines ("stage 2 tune: 14 calls, $0.03"), the plan card ("Plan: <name>")
 * and its "Compile this agent?" question (the app asks "Build this agent?"), subagent
 * lines from the floor drafter, the compiler and Forge, and the result card ("Result:
 * <name> vN" or "Result: not licensed"). The cards carry their values as `data` (the
 * spec and Forge pack hashes, the bar and the bar view's starting scale, the ladder; after
 * the compile, each rung's signed exam counts), and the structured feed from the
 * bridge's observers (stage, model k of n, each rung's practice scores, Forge tool
 * results and harness drafts) adds the numbers the text leaves out. Nothing here is
 * invented: a value the build has not reported yet is absent, and nothing from the exam
 * arrives before the compile returns.
 */
import { prettyModel, rungName } from './models';
import type { Ask, Item, Timeline } from './timeline';
import { barDomain, domainOf, finite, type ExamCounts, type Rung } from './viz';
import { practiceWords, refusalReason } from './vocab';

/** Build mode's approval question, as the CLI asks it ("Compile this agent?") or the app. */
export const BUILD_QUESTION = /^(Compile|Build) this agent\b/;

/** `set_aside`: the person answered the plan's question no (Not now), so nothing was built. */
export type ForgePhase =
  | 'empty' | 'interview' | 'planning' | 'plan' | 'set_aside' | 'blocked' | 'building' | 'licensed' | 'not_licensed' | 'exporting' | 'exported';

export interface StageInfo { stage: number; name: string; calls?: number; spend?: string; text: string }

export interface ForgeModel {
  phase: ForgePhase;
  request?: string;
  name?: string;
  plan?: { title: string; rows: Array<[string, string]>; footer: string; hash?: string; data?: Record<string, unknown> };
  approval?: Ask;
  question?: Ask;
  stages: StageInfo[];
  stage?: StageInfo;
  progress?: string;
  lines: Array<{ agent: string; text: string }>;
  result?: { title: string; rows: Array<[string, string]>; licensed: boolean; name?: string; revision?: number; data?: Record<string, unknown> };
  exportPlan?: { title: string; rows: Array<[string, string]> };
  exported?: { title: string; rows: Array<[string, string]> };
  notices: Array<{ level: string; text: string }>;
  running: boolean;
}

/** Each stage's title (the Report) and its plain line (the card's status line, the transcript). */
export const STAGE_WORDS: Record<number, { title: string; plain: string }> = {
  0: { title: 'Understand the job', plain: 'Writing the spec and the cases' },
  1: { title: 'Pick a starting point', plain: 'Picking a starting point' },
  2: { title: 'Practice', plain: 'Practicing on the cases' },
  3: { title: 'Engineer', plain: 'Writing custom code' },
  4: { title: 'Exam', plain: 'Sitting the exam' },
};

/** A stage's plain line, with the plan's practice count when it has one ("Practicing on 100 cases"). */
export function stageLine(stage: number, practice?: number): string {
  if (stage === 2 && practice !== undefined) return `Practicing on ${practice} cases`;
  return STAGE_WORDS[stage]?.plain ?? 'Building';
}

const STAGE_LINE = /^stage (\d) ([a-z ]+?)(?::\s*(?:(\d+) calls?,\s*(\$[\d.]+))?.*)?$/i;

export function parseStage(text: string): StageInfo | null {
  const match = STAGE_LINE.exec(text.trim());
  if (!match) return null;
  return {
    stage: Number(match[1]),
    name: match[2].trim(),
    calls: match[3] ? Number(match[3]) : undefined,
    spend: match[4],
    text,
  };
}

function rowValue(rows: Array<[string, string]>, label: string): string | undefined {
  return rows.find(([key]) => key === label)?.[1];
}

/**
 * Whether an answer to the plan's question declined the build, by build mode's own rule
 * (architect/agent/build.py): no answer (cancelled), or anything but y or yes. An empty
 * answer is Enter, which takes the default, yes. Undefined is not known.
 */
export function declined(answer: string | null | undefined): boolean {
  if (answer === null) return true;
  if (answer === undefined) return false;
  const said = answer.trim().toLowerCase();
  return said !== '' && said !== 'y' && said !== 'yes';
}

/** The part of the conversation that belongs to the latest build. */
function latestBuild(items: Item[]): Item[] {
  let start = -1;
  for (let i = items.length - 1; i >= 0; i -= 1) {
    const item = items[i];
    if (item.kind === 'work' && item.steps.some((s) => s.name === 'make' || s.name === 'forge')) {
      start = i;
      break;
    }
    if (item.kind === 'plan' && item.title.startsWith('Plan')) {
      start = i;
      break;
    }
  }
  if (start === -1) return [];
  // Back up to the person's request that started it.
  for (let i = start; i >= 0; i -= 1) if (items[i].kind === 'user') return items.slice(i);
  return items.slice(start);
}

export function forgeModel(timeline: Timeline): ForgeModel {
  const segment = latestBuild(timeline.items);
  const model: ForgeModel = { phase: 'empty', stages: [], lines: [], notices: [], running: false };
  const firstUser = timeline.items.find((i) => i.kind === 'user' && !i.text.startsWith('/'));
  if (firstUser && firstUser.kind === 'user') model.request = firstUser.text;
  if (!segment.length) {
    const openAsk = timeline.items.find((i) => i.kind === 'ask' && !i.closed);
    if (openAsk && openAsk.kind === 'ask') {
      model.phase = 'interview';
      model.question = openAsk.ask;
    }
    model.running = timeline.running;
    return model;
  }
  const user = segment.find((i) => i.kind === 'user');
  if (user && user.kind === 'user') model.request = user.text.replace(/^\/(make|forge)\s+/, '');
  model.phase = 'interview';
  let compiling = false;
  let setAside = false;
  for (const item of segment) {
    if (item.kind === 'work') {
      for (const text of item.progressLog) {
        const stage = parseStage(text);
        if (stage) {
          compiling = compiling || stage.stage >= 1;
          const last = model.stages.at(-1);
          if (last && last.stage === stage.stage) model.stages[model.stages.length - 1] = stage;
          else model.stages.push(stage);
          model.stage = stage;
        }
        model.progress = text;
      }
      for (const step of item.steps) {
        // A subagent line with no tool running is its own step (see timeline.fold).
        if (step.subagent) model.lines.push({ agent: step.name, text: step.summary });
        for (const line of step.lines) model.lines.push(line);
      }
    } else if (item.kind === 'plan') {
      if (item.title.startsWith('Plan (blocked)')) {
        model.plan = { title: item.title, rows: item.rows, footer: item.footer, data: item.data };
        model.phase = 'blocked';
      } else if (item.title.startsWith('Plan: ')) {
        const hash = typeof item.data?.spec_hash === 'string' ? item.data.spec_hash : /spec\s+([0-9a-f]{8,64})/i.exec(item.footer)?.[1];
        model.plan = { title: item.title, rows: item.rows, footer: item.footer, hash, data: item.data };
        model.name = item.title.slice('Plan: '.length).trim();
        model.phase = 'plan';
      } else if (item.title.startsWith('Result')) {
        const licensed = !item.title.includes('not licensed') && !item.title.includes('not installed');
        const named = /^Result: (\S+)(?: v(\d+))?/.exec(item.title);
        model.result = {
          title: item.title, rows: item.rows, licensed,
          name: named && named[1] !== 'not' ? named[1] : model.name,
          revision: named?.[2] ? Number(named[2]) : undefined,
          data: item.data,
        };
        model.phase = licensed ? 'licensed' : 'not_licensed';
      } else if (item.title.startsWith('Export plan')) {
        model.exportPlan = { title: item.title, rows: item.rows };
        model.phase = 'exporting';
      } else if (item.title.startsWith('Exported')) {
        model.exported = { title: item.title, rows: item.rows };
        model.phase = 'exported';
      }
    } else if (item.kind === 'ask' && !item.closed) {
      if (BUILD_QUESTION.test(item.ask.question ?? '')) model.approval = item.ask;
      else model.question = item.ask;
    } else if (item.kind === 'ask' && BUILD_QUESTION.test(item.ask.question ?? '')) {
      setAside = declined(item.answer);
    } else if (item.kind === 'notice' && item.level !== 'info') {
      model.notices.push({ level: item.level, text: item.text });
    }
  }
  if (compiling && (model.phase === 'plan' || model.phase === 'interview')) model.phase = 'building';
  if (model.phase === 'interview' && model.stages.some((s) => s.stage === 0)) model.phase = 'planning';
  // Not now on the plan: the plan stays readable, but nothing waits to be built.
  if (setAside && model.phase === 'plan') model.phase = 'set_aside';
  model.running = timeline.running;
  return model;
}

export function planField(model: ForgeModel, label: string): string | undefined {
  return model.plan ? rowValue(model.plan.rows, label) : undefined;
}

export function resultField(model: ForgeModel, label: string): string | undefined {
  return model.result ? rowValue(model.result.rows, label) : undefined;
}

/** "held_out_tasks >= 0.90 on 'x'" style floor text to a 0..1 bar, when it says one. */
export function floorLimit(text?: string): number | undefined {
  if (!text) return undefined;
  const match = /(>=|<=)\s*([01](?:\.\d+)?)/.exec(text) ?? /\b(0\.\d+|1\.0+)\b/.exec(text);
  if (!match) return undefined;
  const value = Number(match[2] ?? match[1]);
  return Number.isFinite(value) ? value : undefined;
}

/** A result card's floor row: "0.95/0.90 on 6 held-out cases" style text to numbers. */
export function scoreAndLimit(text?: string): { score?: number; limit?: number } {
  if (!text) return {};
  const pair = /([01](?:\.\d+)?)\s*\/\s*([01](?:\.\d+)?)/.exec(text);
  if (pair) return { score: Number(pair[1]), limit: Number(pair[2]) };
  const single = /([01]\.\d+)/.exec(text);
  return single ? { score: Number(single[1]) } : {};
}

/** A build progress line in plain words for the conversation ("Practicing on the cases"). */
export function friendlyProgress(text: string): string {
  const stage = parseStage(text);
  if (stage) return STAGE_WORDS[stage.stage]?.plain ?? text;
  if (/^interview:/i.test(text)) return 'Reading your request';
  if (/^floor: drafting/i.test(text)) return 'Drafting the practice and exam cases';
  if (/^export: staging/i.test(text)) return 'Preparing the package for your project';
  return text;
}

const FORGE_TOOL_WORDS: Record<string, string> = {
  forge_check: 'Checked the harness for mistakes',
  forge_dryrun: 'Tried it with sample replies',
  forge_measure: 'Measured it on the practice cases',
  forge_submit: 'Submitted a candidate',
  forge_spawn: 'Built a sub-harness',
};

/** The compiler's opening line, "compiling receipt-reader (spec ba94f72c)": the build of that agent. */
const COMPILING = /^compiling (\S+) \(spec [0-9a-f]{6,64}\)$/;

/** Whether a build line only says the compile started (the panel's progress line says more). */
export function startLine(text: string): boolean {
  return COMPILING.test(text.trim());
}

/**
 * A build log line in plain words (Forge reports its tool names; the case drafter its
 * counts). The compiler's opening line, titled "Build" where it shows, keeps only the
 * agent's name: the verb is the title's, and the spec's hash is in Details.
 */
export function friendlyLine(text: string): string {
  const compiling = COMPILING.exec(text.trim());
  if (compiling) return compiling[1];
  const tool = /^(forge_[a-z]+)\b(.*)$/.exec(text.trim());
  if (tool && FORGE_TOOL_WORDS[tool[1]]) return FORGE_TOOL_WORDS[tool[1]] + tool[2].replace(/^[:\s]+/, tool[2].trim() ? ': ' : '');
  // "'receipts' configured: 200 cases (100 tuning / 100 held-out)"
  const cases = /^'[^']+' configured: (.*) \((\d+) tuning \/ (\d+) held-out\)$/.exec(text.trim());
  if (cases) return `${practiceWords(cases[1])}, ${cases[2]} for practice and ${cases[3]} for the exam`;
  // "running v1 on nvidia/Nemotron-3_5-Lightning": models by name.
  return practiceWords(text.replace(/\bnvidia\/[A-Za-z0-9._-]+/g, (id) => prettyModel(id)));
}

// ---------------------------------------------------------------- the live feed

/** One version of Forge's harness, as the tool that first worked on it saw it. */
export interface Draft {
  sha256: string;
  text: string;
  /** The Forge tool that first read this version (forge_check, forge_measure...). */
  tool: string;
  /** The ladder rung (1-based) Forge was working on. */
  rung?: number;
  /** Its latest practice score: a measure that covered every practice case. */
  measured?: number;
  /** Its latest quick measure on a few practice cases: not a practice score. */
  sampled?: { score: number; cases?: number };
  /** Its submission's practice score (a submission runs every practice case). */
  submitted?: number;
  accepted?: boolean;
}

export interface ForgeFeed {
  stages: Array<Record<string, unknown>>;
  tools: Array<Record<string, unknown>>;
  latest?: Record<string, unknown>;
  /** Forge's harness versions in order (the top-level session only). */
  drafts?: Draft[];
  /** How many practice cases the build's plan has: a score on all of them is a practice score. */
  practice?: number;
  /** When the build's turn ended (ms, from `turn_finished`'s `at`): the last model's end. */
  endedAt?: number;
}

export const emptyFeed = (): ForgeFeed => ({ stages: [], tools: [], drafts: [] });

const num = (v: unknown): number | undefined => (finite(v) ? v : undefined);
const record = (v: unknown): Record<string, unknown> | undefined => (v && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : undefined);

/**
 * A Forge tool's practice measurement (forge_measure and forge_submit), and whether it is
 * a practice score: one taken on every practice case, as the best preset's tick, the bar
 * and the exam are. forge_submit always runs them all; forge_measure runs a few (8 by
 * default, never more than 32), which is a quick look, not a score to set beside the bar.
 */
export function measurementOf(data: Record<string, unknown>, practice?: number): { score: number; cases?: number; whole: boolean } | undefined {
  const m = record(record(data.result)?.measurement);
  const score = num(m?.score);
  if (score === undefined) return undefined;
  const cases = num(m?.sample_count);
  const whole = cases !== undefined && practice !== undefined ? cases === practice : data.tool === 'forge_submit';
  return { score, cases, whole };
}

/**
 * Fold one bridge message into the feed: `forge` messages add stages, tool results and
 * harness drafts (each tool result tagged with the rung it belongs to); a new plan card
 * starts a new build's feed, holding its practice case count. Live messages and a
 * replayed log go through the same fold, so a reload redraws exactly what was shown.
 */
export function feedMessage(feed: ForgeFeed, msg: Record<string, unknown>): ForgeFeed {
  if (msg.type === 'forge') {
    // The bridge stamps each forge message with when it was sent (`at`, Unix seconds),
    // in its data or beside it; either way the stage keeps it.
    const data = record(msg.data) ?? {};
    return feedForge(feed, finite(msg.at) && !finite(data.at) ? { ...data, at: msg.at } : data);
  }
  // The turn that ran the build ended: the last model to run stops there.
  if (msg.type === 'turn_finished' && finite(msg.at) && feed.endedAt === undefined && feed.stages.some((s) => Number(s.depth ?? 0) === 0)) {
    return { ...feed, endedAt: msg.at * 1000 };
  }
  const event = msg.type === 'event' ? record(msg.event) : undefined;
  if (event?.kind === 'plan_card' && String(event.title ?? '').startsWith('Plan')) {
    const practice = num(record(event.data)?.practice);
    return practice === undefined ? emptyFeed() : { ...emptyFeed(), practice };
  }
  return feed;
}

export function feedForge(feed: ForgeFeed, data: Record<string, unknown>): ForgeFeed {
  const next: ForgeFeed = { ...feed, latest: data };
  if ('stage' in data) {
    next.stages = [...feed.stages, data];
    return next;
  }
  const depth = Number(data.depth ?? 0);
  const current = [...feed.stages].reverse().find((s) => Number(s.depth ?? 0) === 0);
  const rung = depth === 0 ? num(current?.model_index) : undefined;
  const source = record(data.source);
  const entry: Record<string, unknown> = { ...data, ...(rung !== undefined ? { rung } : {}) };
  delete entry.source; // the text lives in drafts only
  next.tools = [...feed.tools, entry].slice(-400);
  if (depth !== 0) return next;
  const tool = String(data.tool ?? '');
  const m = measurementOf(data, feed.practice);
  const accepted = record(data.result)?.accepted === true;
  const drafts = [...(feed.drafts ?? [])];
  if (source && typeof source.text === 'string' && typeof source.sha256 === 'string') {
    drafts.push({ sha256: source.sha256, text: source.text, tool, rung });
  }
  const last = drafts.at(-1);
  if (last && m && (tool === 'forge_measure' || tool === 'forge_submit')) {
    drafts[drafts.length - 1] = tool === 'forge_submit'
      ? { ...last, submitted: m.score, accepted }
      : m.whole
        ? { ...last, measured: m.score }
        : { ...last, sampled: m.cases !== undefined ? { score: m.score, cases: m.cases } : { score: m.score } };
  }
  next.drafts = drafts;
  return next;
}

// ---------------------------------------------------------------- the ladder

/**
 * Each model's wall time from the bridge's stage times (`at`): from its first stage to the
 * next model's first stage, and for the last model that ran, to the end of the build's
 * turn. A model with no stamped stage (a replay from before `at`) has no times. `count` is
 * the ladder's length; times are ms.
 */
export function rungTimes(feed: ForgeFeed, count: number): Array<{ startedAt?: number; endedAt?: number }> {
  const starts: Array<number | undefined> = [];
  for (const s of feed.stages) {
    if (Number(s.depth ?? 0) !== 0 || !finite(s.at) || !finite(s.model_index)) continue;
    const i = s.model_index - 1;
    if (i >= 0 && i < count && starts[i] === undefined) starts[i] = s.at * 1000;
  }
  return Array.from({ length: count }, (_, i) => {
    const startedAt = starts[i];
    if (startedAt === undefined) return {};
    const next = starts.slice(i + 1).find((t): t is number => t !== undefined);
    const endedAt = next ?? (feed.endedAt !== undefined && feed.endedAt >= startedAt ? feed.endedAt : undefined);
    return endedAt !== undefined ? { startedAt, endedAt } : { startedAt };
  });
}

/** A signed report's counts as the bridge sends them. */
interface CountsWire { cases?: unknown; correct?: unknown; invalid?: unknown; over_budget?: unknown }

function counts(value: unknown): ExamCounts | undefined {
  const c = record(value) as CountsWire | undefined;
  if (!c || !finite(c.cases)) return undefined;
  return {
    cases: c.cases,
    correct: finite(c.correct) ? c.correct : null,
    invalid: finite(c.invalid) ? c.invalid : 0,
    overBudget: finite(c.over_budget) ? c.over_budget : 0,
  };
}

export interface LadderExam {
  counts: ExamCounts;
  bar: number;
  licensed: boolean;
  /** The rung whose signed report this is, by name. */
  name: string;
  model: string;
  score?: number;
}

export interface Ladder {
  bar: number;
  /** The scale the plan set (the plan card's, else made from the bar); the bar view grows it to fit a mark outside it. */
  domain: [number, number];
  rungs: Rung[];
  /** The returned artifact's signed counts, once the compile has returned. */
  exam?: LadderExam;
  /** The compile returned: every ring is drawn. */
  finished: boolean;
}

/**
 * The bar view's rungs: names and order from the plan card, the running rung and each
 * rung's practice scores from the live feed, and, once the compile has returned, each
 * rung's signed exam score, counts and decision from the result card. The scale starts as
 * the plan's (its `domain`, else `barDomain(bar)`), and the bar view widens it in 0.1 steps
 * only for a mark beyond it (lib/viz.ts `widenDomain`), so every mark is drawn where its
 * number says. A dot is a practice score on every practice case, so it compares with the
 * tick and the bar; a quick measure on a few cases is not drawn. An exam that was not
 * scored has no score (its signed 0.0 is a placeholder): the row says "unscored" and keeps
 * its counts.
 */
export function ladderView(model: ForgeModel, feed: ForgeFeed): Ladder | null {
  const plan = model.plan?.data ?? {};
  const result = model.result?.data;
  const stages = feed.stages.filter((s) => Number(s.depth ?? 0) === 0);
  const fromStages = stages.map((s) => s.ladder).find((l): l is string[] => Array.isArray(l));
  const ids = (Array.isArray(plan.ladder) ? plan.ladder as string[] : undefined)
    ?? fromStages
    ?? (Array.isArray(result?.rungs) ? (result.rungs as Array<Record<string, unknown>>).map((r) => String(r.model)) : undefined);
  const bar = num(plan.bar) ?? num(result?.bar) ?? floorLimit(planField(model, 'floor'));
  if (!ids?.length || bar === undefined) return null;
  if (typeof plan.kind === 'string' && plan.kind !== 'held_out_tasks') return null;
  const practiceCases = num(plan.practice) ?? feed.practice;

  const done = Array.isArray(result?.rungs) ? result.rungs as Array<Record<string, unknown>> : undefined;
  const current = num(stages.at(-1)?.model_index);
  const times = rungTimes(feed, ids.length);
  const rungs = ids.map((id, index): Rung => {
    const position = index + 1;
    const own = stages.filter((s) => num(s.model_index) === position || (s.model_index === undefined && s.model === id));
    const tuned = num(record(own.map((s) => s.tuned).filter(Boolean).at(-1))?.score);
    const baseline = feed.tools.filter((t) => t.tool === 'forge_session' && t.event === 'started' && t.rung === position)
      .map((t) => num(record(record(t.result)?.baseline)?.score)).at(-1);
    const examined = num(record(own.map((s) => s.examined).filter(Boolean).at(-1))?.score);
    const practice = feed.tools
      .filter((t) => t.rung === position && (t.tool === 'forge_measure' || t.tool === 'forge_submit'))
      .map((t) => measurementOf(t, practiceCases))
      .filter((m): m is NonNullable<typeof m> => !!m && m.whole)
      .map((m) => m.score);
    const rung: Rung = {
      key: id, name: rungName(id), model: prettyModel(id), state: 'pending',
      preset: tuned ?? baseline, practice, examined, ...times[index],
    };
    const row = done?.find((r) => r.model === id) ?? done?.[index];
    if (row) {
      const cost = num(row.mean_cost_usd);
      if (cost !== undefined) rung.cost = cost;
      const signed = counts(row.counts);
      // Not scored (too many invalid outputs, a budget refusal, a floor error): the
      // signed score is a 0.0 placeholder, never a result to draw.
      const score = signed && signed.correct === null ? undefined : num(row.score);
      const codes = Array.isArray(row.codes) ? row.codes.map(String) : [];
      if (score === undefined && !signed) return { ...rung, state: 'skipped', reason: refusalReason(codes) };
      const licensed = row.licensed === true;
      return {
        ...rung,
        state: 'done',
        exam: { ...(score !== undefined ? { score } : {}), counts: signed ?? null },
        licensed,
        ships: row.ships === true,
        reason: !licensed && (score === undefined || score >= bar) ? refusalReason(codes) : undefined,
      };
    }
    if (done) return { ...rung, state: 'skipped' }; // the compile ended before this rung
    if (current === undefined) return rung;
    if (position < current) return { ...rung, state: 'done' };
    if (position === current) return { ...rung, state: model.running ? 'running' : 'done' };
    return rung;
  });

  let exam: LadderExam | undefined;
  const examCounts = counts(result?.exam);
  if (result && examCounts) {
    const id = typeof result.model === 'string' ? result.model : '';
    const score = examCounts.correct === null ? undefined : num(result.score);
    exam = {
      counts: examCounts, bar, licensed: result.licensed === true,
      name: id ? rungName(id) : '', model: id ? prettyModel(id) : '', ...(score !== undefined ? { score } : {}),
    };
  }
  return { bar, domain: domainOf(plan.domain) ?? barDomain(bar), rungs, exam, finished: !!done };
}
