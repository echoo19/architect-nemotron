/**
 * The guided build (clarity spec §0): one build conversation is six steps, Describe, Plan,
 * Build, Results, Try it and Add to project. The step comes from what the app already
 * knows (the build's phase, `cardState`, and the conversation's items); the only new state
 * is which finished step the person is looking at (`wizardView` in the store).
 *
 * Everything here is pure: the screens (components/forge/wizard) draw what these return,
 * and every word with a number in it is made from the bridge's data. Nothing here invents
 * a figure: a value the build did not report leaves its sentence out.
 */
import type { Conv } from '../store/app';
import { cleanQuestion } from './asks';
import { rungTimes, type ForgeModel, type Ladder } from './forge';
import { diffCount } from './linediff';
import type { Ask, Item } from './timeline';
import { cardState, examCount, exportRows, firstSentence, practiceCount, resultLead, type CardState } from './vocab';
import { finite, fmtUsd, type Rung } from './viz';

export { rungTimes };

// ---------------------------------------------------------------- the step

/** A finished build's screens the person can move between. */
export type WizardView = 'results' | 'try' | 'add';

export type StepNumber = 1 | 2 | 3 | 4 | 5 | 6;

/** What the step is showing (clarity spec §0). */
export type SubState =
  // Steps 1 to 3
  | 'describe' | 'reading' | 'question' | 'planning' | 'plan' | 'set_aside' | 'blocked' | 'stopped_plan' | 'building' | 'stopped_build'
  // Step 4
  | 'passed' | 'no_pass' | 'unscored'
  // Step 5
  | 'try'
  // Step 6
  | 'add_ready' | 'add_preparing' | 'add_confirm' | 'add_writing' | 'add_refused' | 'files_written'
  | 'connecting' | 'connect_approve' | 'connect_question' | 'connected' | 'connect_failed';

export interface WizardStep { step: StepNumber; sub: SubState }

type ConvLike = Pick<Conv, 'status' | 'error' | 'timeline'>;

/** The view a finished build shows when the person has not picked one: Add once an export started, else Results. */
export function defaultView(state: CardState): WizardView {
  return state === 'exporting' || state === 'adding' || state === 'exported' ? 'add' : 'results';
}

/** The card state as it will be once the conversation is ready: Starting keeps the step it is on. */
function settledState(model: ForgeModel, conv: ConvLike): CardState {
  const state = cardState(model, conv);
  return state === 'starting' ? cardState(model, { ...conv, status: 'ready' }) : state;
}

/** Whether the build got past the plan: a model started (stage 1 or later). */
function built(model: ForgeModel): boolean {
  return model.phase === 'building' || model.stages.some((s) => s.stage >= 1);
}

/**
 * The step and what it shows. `view` is the store's `wizardView` for this conversation
 * (unset follows the build); `skipped` is the person's Skip on step 6's Connect.
 *
 * Steps 4 to 6 belong to a licensed build: one that did not pass stays on Results. A
 * reading (sent, no plan yet) is step 2: the job has left step 1.
 */
export function wizardStep(model: ForgeModel, conv: ConvLike, view?: WizardView | null, opts: { skipped?: boolean } = {}): WizardStep {
  const state = settledState(model, conv);
  switch (state) {
    case 'empty':
      return conv.timeline.running ? { step: 2, sub: 'reading' } : { step: 1, sub: 'describe' };
    case 'interview':
      return model.question && model.question.kind !== 'approval' ? { step: 2, sub: 'question' } : { step: 2, sub: 'reading' };
    case 'planning':
      return { step: 2, sub: 'planning' };
    case 'plan':
    case 'set_aside':
    case 'blocked':
      return { step: 2, sub: state };
    case 'building':
      return { step: 3, sub: 'building' };
    case 'stopped':
      return built(model) ? { step: 3, sub: 'stopped_build' } : { step: 2, sub: 'stopped_plan' };
    case 'not_licensed':
      return { step: 4, sub: resultLead(model) ? 'no_pass' : 'unscored' };
    default:
      break;
  }
  // Licensed: the person moves between Results, Try it and Add.
  const shown = view ?? defaultView(state);
  if (shown === 'results') return { step: 4, sub: 'passed' };
  if (shown === 'try') return { step: 5, sub: 'try' };
  return { step: 6, sub: addSub(model, conv, state, !!opts.skipped) };
}

function addSub(model: ForgeModel, conv: ConvLike, state: CardState, skipped: boolean): SubState {
  if (model.phase === 'exported') {
    const c = connectState(conv.timeline.items);
    if (c.open) return c.open.kind === 'approval' ? 'connect_approve' : 'connect_question';
    // Connecting only once the coder took the job: a quiet turn that only finishes the
    // export, or a Try it run, is not Architect reading the code.
    if (conv.timeline.running && !c.userAfter && c.started) return 'connecting';
    if (!c.started) return skipped ? 'connected' : 'files_written';
    if (c.failure && c.allowed === 0) return 'connect_failed';
    return 'connected';
  }
  if (state === 'exporting') return 'add_confirm';
  if (state === 'adding') return 'add_writing';
  if (exportRefusal(model, conv.timeline.items)) return 'add_refused';
  if (conv.timeline.running && exportPending(conv.timeline.items)) return 'add_preparing';
  return 'add_ready';
}

/**
 * Whether an `/export` was sent and nothing has come of it yet (no export plan, no
 * Exported card, no notice since): Preparing the files. Another running turn is not that.
 */
export function exportPending(items: Item[]): boolean {
  const sent = items.findLastIndex((i) => i.kind === 'user' && /^\/export\b/.test(i.text));
  if (sent < 0) return false;
  return !items.slice(sent + 1).some((i) => (i.kind === 'plan' && /^Export(ed| plan)\b/.test(i.title)) || (i.kind === 'notice' && i.level !== 'info'));
}

// ---------------------------------------------------------------- the stepper

export const STEP_LABELS: Record<StepNumber, string> = {
  1: 'Describe', 2: 'Plan', 3: 'Build', 4: 'Results', 5: 'Try it', 6: 'Add to project',
};

export type StepMark = 'done' | 'current' | 'todo' | 'failed' | 'locked';

export interface StepItem {
  step: StepNumber;
  label: string;
  mark: StepMark;
  /** This is the step on screen (`aria-current="step"`), also when its mark is `failed`. */
  current: boolean;
  /** The view a click on this step opens; unset, the step is not a button. */
  view?: WizardView;
}

/**
 * The six steps' marks (clarity spec §2.1): done before the current one, the current one
 * (failed on Results when nothing passed), todo after; Try it and Add are locked when
 * nothing passed. Results is a button once there is a result, Try it and Add once a
 * model passed.
 */
export function stepItems(ws: WizardStep, model: ForgeModel): StepItem[] {
  const licensed = !!model.result?.licensed;
  const result = !!model.result;
  return ([1, 2, 3, 4, 5, 6] as StepNumber[]).map((step) => {
    const current = step === ws.step;
    let mark: StepMark = step < ws.step ? 'done' : current ? 'current' : 'todo';
    if (current && step === 4 && (ws.sub === 'no_pass' || ws.sub === 'unscored')) mark = 'failed';
    if (step >= 5 && result && !licensed) mark = 'locked';
    if (!current && step === 6 && model.phase === 'exported') mark = 'done';
    const view: WizardView | undefined = step === 4 && result ? 'results' : step === 5 && licensed ? 'try' : step === 6 && licensed ? 'add' : undefined;
    return { step, label: STEP_LABELS[step], mark, current, ...(view ? { view } : {}) };
  });
}

// ---------------------------------------------------------------- time

type Work = Extract<Item, { kind: 'work' }>;

/** The build's own fold: the latest one that ran a stage (it counts from the Build press, timeline.ts). */
export function buildWork(items: Item[]): Work | undefined {
  for (let i = items.length - 1; i >= 0; i -= 1) {
    const item = items[i];
    if (item.kind === 'work' && (item.progressLog.some((line) => /^stage \d /i.test(line.trim())) || item.steps.some((s) => s.subagent && s.name === 'compiler'))) return item;
  }
  return undefined;
}

/** The latest fold after the export's approval. */
export function exportWork(items: Item[]): Work | undefined {
  const ask = items.findLastIndex((i) => i.kind === 'ask' && i.ask.kind === 'approval' && i.ask.action === 'export');
  if (ask < 0) return undefined;
  return items.slice(ask).findLast((i): i is Work => i.kind === 'work');
}

/** A wall clock: 4:12, 1:02:03. */
export function fmtClock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = String(total % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
}

/** A duration in seconds as the screens say it: "45s", "12m 28s", "1h 2m". */
export function fmtDuration(seconds: number): string {
  const t = Math.max(0, Math.round(seconds));
  if (t < 60) return `${t}s`;
  if (t < 3600) return `${Math.floor(t / 60)}m ${t % 60}s`;
  return `${Math.floor(t / 3600)}h ${Math.floor((t % 3600) / 60)}m`;
}

/**
 * Whole minutes left before the build's own time cap stops it, never below 0: a cap, not
 * an estimate. Rounded up, so "at most" stays true (44m 58s left reads 45).
 */
export function minutesLeft(buildSeconds: number, elapsedMs: number): number {
  return Math.max(0, Math.ceil((buildSeconds * 1000 - elapsedMs) / 60000 - 1e-9));
}

/** A model's wall time on the build, in seconds, when the bridge stamped its stages (`now` for one still running). */
export function rungSeconds(rung: Pick<Rung, 'startedAt' | 'endedAt'>, now?: number): number | undefined {
  if (rung.startedAt === undefined) return undefined;
  const end = rung.endedAt ?? now;
  return end !== undefined && end >= rung.startedAt ? (end - rung.startedAt) / 1000 : undefined;
}

// ---------------------------------------------------------------- the job's words

const record = (v: unknown): Record<string, unknown> | undefined => (v && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : undefined);
const num = (v: unknown): number | undefined => (finite(v) ? v : undefined);
const rowOf = (rows: Array<[string, string]> | undefined, key: string): string | undefined => rows?.find(([k]) => k === key)?.[1];
const sentenceCase = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);
const words = (name: string) => name.replace(/_/g, ' ').trim();

/** One field of the plan's input or output schema, in order (plan card data `inputs` / `outputs`). */
export interface SchemaField { name: string; type?: string; nullable: boolean }

function fieldsOf(value: unknown): SchemaField[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const out: SchemaField[] = [];
  for (const raw of value) {
    const f = record(raw);
    if (!f || typeof f.name !== 'string') continue;
    const types = Array.isArray(f.type) ? f.type.map(String) : typeof f.type === 'string' ? [f.type] : [];
    const type = types.find((t) => t !== 'null');
    out.push({ name: f.name, ...(type ? { type } : {}), nullable: f.nullable === true || types.includes('null') });
  }
  return out;
}

/** A JSON schema's properties as fields, in order (an installed agent's `input_schema` or `output_schema`). */
export function schemaFields(schema: unknown): SchemaField[] {
  const props = record(record(schema)?.properties);
  if (!props) return [];
  return Object.entries(props).map(([name, raw]) => {
    const t = record(raw)?.type;
    const types = Array.isArray(t) ? t.map(String) : typeof t === 'string' ? [t] : [];
    const type = types.find((x) => x !== 'null');
    return { name, ...(type ? { type } : {}), nullable: types.includes('null') };
  });
}

/** The plan's input fields: its data (B2), else the inputs row's names (types unknown). */
export function inputFields(model: ForgeModel): SchemaField[] {
  return fieldsOf(model.plan?.data?.inputs) ?? listRow(rowOf(model.plan?.rows, 'inputs'));
}

/** The plan's output fields in schema order: its data (B2), else the outputs row's names (types unknown). */
export function outputFields(model: ForgeModel): SchemaField[] {
  return fieldsOf(model.plan?.data?.outputs) ?? listRow(rowOf(model.plan?.rows, 'outputs'));
}

function listRow(text: string | undefined): SchemaField[] {
  return (text ?? '').split(/,\s*/).map((f) => f.trim()).filter(Boolean).map((name) => ({ name, nullable: false }));
}

/**
 * What one case is called on screen: the single string input's name ("receipt"), else
 * "case". `schema`: an installed agent's input schema, when the plan has no field types.
 */
export function caseNoun(model: ForgeModel, schema?: unknown): string {
  const fields = fieldsOf(model.plan?.data?.inputs) ?? (schema ? schemaFields(schema) : []);
  return fields.length === 1 && fields[0].type === 'string' ? words(fields[0].name) : 'case';
}

/** The noun for a count: "1 receipt", "100 receipts". */
export function casePlural(noun: string, count?: number): string {
  return count === 1 ? noun : `${noun}s`;
}

/** "a", "a and b", "a, b, c and d". */
export function joinAnd(items: string[]): string {
  if (items.length < 2) return items.join('');
  return `${items.slice(0, -1).join(', ')} and ${items.at(-1)}`;
}

/** A field list as one phrase: "Total, subtotal, tax and item count". */
export function fieldsSentence(fields: Array<Pick<SchemaField, 'name'>>): string {
  return sentenceCase(joinAnd(fields.map((f) => words(f.name))));
}

/** What the agent takes: "The text of one receipt" for one string input, else its field names. */
export function takesSentence(model: ForgeModel): string | undefined {
  const fields = inputFields(model);
  if (!fields.length) return undefined;
  const noun = caseNoun(model);
  if (fields.length === 1 && noun !== 'case') return `The text of one ${noun}`;
  return fieldsSentence(fields);
}

/** What the agent gives back: "Total, subtotal, tax and item count". */
export function givesSentence(model: ForgeModel): string | undefined {
  const fields = outputFields(model);
  return fields.length ? fieldsSentence(fields) : undefined;
}

const NUMERIC = new Set(['integer', 'number']);

/**
 * The output schema's two promises, when it makes them: whole numbers (every numeric
 * field is an integer) and empty for what the case does not print (a field is nullable).
 */
export function schemaNote(outputs: SchemaField[], noun: string): string | undefined {
  const numeric = outputs.filter((f) => f.type && NUMERIC.has(f.type));
  const parts: string[] = [];
  if (numeric.length && numeric.every((f) => f.type === 'integer')) parts.push('Amounts as whole numbers.');
  if (outputs.some((f) => f.nullable)) parts.push(`A field the ${noun} does not print comes back empty (null).`);
  return parts.length ? parts.join(' ') : undefined;
}

/** The task's rules: its "- " bullet lines (plan card data `task`, B2), in order. */
export function taskRules(model: ForgeModel): string[] {
  const task = typeof model.plan?.data?.task === 'string' ? model.plan.data.task : '';
  return task.split('\n').map((l) => l.trim()).filter((l) => l.startsWith('- ')).map((l) => l.slice(2).trim()).filter(Boolean);
}

/** A rule split at its first ":" into the field it is about and what it says ("total" and "the total amount ..."). */
export function ruleParts(rule: string): { field?: string; text: string } {
  const at = rule.indexOf(':');
  if (at <= 0 || at > 40) return { text: rule };
  return { field: rule.slice(0, at).trim(), text: rule.slice(at + 1).trim() };
}

/** The rules code also checks on every answer, exactly as the plan states them (data `checks`, else the checks row). */
/**
 * The sentence build mode sends in place of the checks when the spec's declaration does
 * not fit it (architect/agent/build.py CHECKS_INVALID): a warning, never a rule.
 */
export const CHECKS_INVALID = "The spec's checks are not valid, so the build stops before any model call.";

export function checkRules(model: ForgeModel): string[] {
  const data = model.plan?.data?.checks;
  if (Array.isArray(data)) return data.filter((c): c is string => typeof c === 'string' && !!c.trim() && c.trim() !== CHECKS_INVALID);
  const row = rowOf(model.plan?.rows, 'checks');
  if (!row || row.trim() === CHECKS_INVALID) return [];
  return row.split(/(?<=[.!?])\s+(?=[A-Z])/).map((s) => s.trim()).filter(Boolean);
}

/** Whether the plan's checks did not fit its spec (CHECKS_INVALID): the build will stop. */
export function checksInvalid(model: ForgeModel): boolean {
  const data = model.plan?.data?.checks;
  if (Array.isArray(data)) return data.some((c) => typeof c === 'string' && c.trim() === CHECKS_INVALID);
  return rowOf(model.plan?.rows, 'checks')?.trim() === CHECKS_INVALID;
}

/** The first rule, cut to one sentence, for "For example: ...". */
export function firstRule(model: ForgeModel): string | undefined {
  const rule = taskRules(model)[0];
  return rule ? firstSentence(rule).replace(/[.!?]$/, '') : undefined;
}

/** The practice and exam cases' source: "CORD v2" (before the first " · " of the floor data row), and whether it is a public dataset. */
export function datasetOf(model: ForgeModel): { name: string; public: boolean } | undefined {
  const row = rowOf(model.plan?.rows, 'floor data');
  const name = row?.split(' · ')[0]?.replace(/\s*\(.*$/, '').trim();
  if (!name) return undefined;
  return { name, public: model.plan?.data?.origin === 'public_dataset' };
}

/** The pass mark as cases: 40 when the bar is 0.40 of 100. */
export function passNeed(model: ForgeModel): number | undefined {
  const bar = num(model.plan?.data?.bar) ?? num(model.result?.data?.bar);
  const exam = examCount(model) ?? num(record(model.result?.data?.exam)?.cases);
  return bar !== undefined && exam !== undefined ? Math.ceil(bar * exam - 1e-9) : undefined;
}

/** The case counts: practice and exam. */
export function caseCounts(model: ForgeModel): { practice?: number; exam?: number } {
  return { practice: practiceCount(model), exam: examCount(model) };
}

/** The most the build may spend and take (plan card data `build_usd`, `build_s`). */
export function buildLimits(model: ForgeModel): { usd?: number; seconds?: number; minutes?: number } {
  const usd = num(model.plan?.data?.build_usd);
  const seconds = num(model.plan?.data?.build_s);
  return { ...(usd !== undefined ? { usd } : {}), ...(seconds !== undefined ? { seconds, minutes: Math.round(seconds / 60) } : {}) };
}

/** The models the plan tries, in order (plan card data `ladder`). */
export function ladderIds(model: ForgeModel): string[] {
  const ladder = model.plan?.data?.ladder;
  return Array.isArray(ladder) ? ladder.filter((id): id is string => typeof id === 'string') : [];
}

/** "cheapest" on the first and "most expensive" on the last model, when there are two or more (C12). */
export function ladderTag(index: number, count: number): 'cheapest' | 'most expensive' | undefined {
  if (count < 2) return undefined;
  if (index === 0) return 'cheapest';
  if (index === count - 1) return 'most expensive';
  return undefined;
}

/** A model's list price before anything ran (plan card data `prices`, per million tokens): "$0.06 in · $0.24 out". */
export function listPrice(model: ForgeModel, id: string): string | undefined {
  const pair = record(model.plan?.data?.prices)?.[id];
  if (!Array.isArray(pair) || pair.length !== 2 || !finite(pair[0]) || !finite(pair[1])) return undefined;
  return `${fmtUsd(pair[0])} in · ${fmtUsd(pair[1])} out`;
}

/** Why a plan cannot be built, as one sentence. */
export function blockedLine(footer: string): string {
  const code = footer.replace(/^blocked:\s*/, '').split(/,\s*/)[0] ?? '';
  const known: Record<string, string> = {
    model_unpriced: 'This model has no known price, so the build limit cannot be kept.',
    forge_model_unpriced: 'The model that writes the agent has no known price, so the build limit cannot be kept.',
    forge_disabled: 'This build needs practice and exam cases first.',
  };
  return known[code] ?? `It cannot start yet (${code.replace(/_/g, ' ')}).`;
}

/** The project's name: the last part of the conversation's folder ("expense-app"). */
export function projectName(cwd: string | undefined): string {
  return (cwd ?? '').replace(/[\\/]+$/, '').split(/[\\/]/).pop() ?? '';
}

/** What the build cost, from the result's cost row: "$0.31". */
export function buildCost(model: ForgeModel): string | undefined {
  return /^(\$[\d.,]+) to build/.exec(rowOf(model.result?.rows, 'cost') ?? '')?.[1];
}

// ---------------------------------------------------------------- results

/** The exam's right answers for a model, when it was scored. */
export function rightOf(rung: Rung): number | undefined {
  const c = rung.exam?.counts?.correct;
  return finite(c) ? c : undefined;
}

/** How many a model was short of the pass mark ("1 short"), or nothing when it reached it. */
export function shortBy(rung: Rung, need: number): number | undefined {
  const right = rightOf(rung);
  return right !== undefined && right < need ? need - right : undefined;
}

/** "more than 3 times" or "3 times" the price, from two costs per case. */
export function priceRatio(top: number, ship: number): string | undefined {
  if (!(ship > 0) || !(top > ship)) return undefined;
  const ratio = top / ship;
  const whole = Math.floor(ratio + 1e-9);
  if (whole < 2) return 'a higher price';
  return Math.abs(ratio - whole) < 1e-9 ? `${whole} times` : `more than ${whole} times`;
}

/**
 * Why the ship is the ship when a dearer model scored higher (clarity §3, step 4): the
 * pass marks that would move the choice to the highest scorer. The lower end is one past
 * the best score of every model cheaper than it, so the sentence is true of the whole
 * ladder. Nothing when no model that passed scored higher.
 */
/**
 * The why-not line. `frozen` names the plan file a build came from: build mode keeps that
 * file's pass mark over a saved change (FROZEN_IGNORED), so the line says to change it there.
 */
export function whyNotLine(ladder: Ladder, frozen?: string): string | undefined {
  const ship = ladder.rungs.find((r) => r.ships);
  const c = ship ? rightOf(ship) : undefined;
  if (!ship || c === undefined) return undefined;
  let top: Rung | undefined;
  for (const r of ladder.rungs) {
    const s = rightOf(r);
    if (r === ship || !r.licensed || s === undefined || s <= c) continue;
    if (!top || s > (rightOf(top) ?? -1)) top = r;
  }
  if (!top) return undefined;
  const h = rightOf(top)!;
  const at = ladder.rungs.indexOf(top);
  const below = Math.max(-1, ...ladder.rungs.slice(0, at).map((r) => rightOf(r) ?? -1));
  const low = below + 1;
  if (low > h) return undefined;
  return `Why not ${top.name}? It passed too, but ${ship.name} passed for less. If you need more than ${c} right, raise the pass mark ${frozen ? `in ${frozen} ` : ''}to between ${low} and ${h}${frozen ? '' : ' with Edit plan'} and build again: then ${ship.name} no longer passes and ${top.name} becomes the cheapest that does.`;
}

/** Only the pass mark held this model back: its one code is the floor's (nothing else would still refuse it). */
function onlyShort(rung: Rung, codes: string[] | undefined): boolean {
  return !!codes && codes.length > 0 && codes.every((c) => c === 'primary_floor_failed') && rightOf(rung) !== undefined;
}

/**
 * What to change when nothing passed (clarity §3, 4b): the pass mark that would let the
 * best model pass, and, when a cheaper model scored less, the mark that would let the
 * highest of those pass. Only a model the pass mark alone held back is named. `codes`:
 * each rung's refusal codes from the result card, by model id.
 */
export function noPassLines(model: ForgeModel, ladder: Ladder): string[] {
  const codes = resultCodes(model);
  const n = num(record(model.result?.data?.exam)?.cases) ?? ladder.rungs.map((r) => r.exam?.counts?.cases).find(finite);
  const scored = ladder.rungs.filter((r) => onlyShort(r, codes[r.key]));
  if (!scored.length) return [];
  const best = scored.reduce((a, b) => ((rightOf(b) ?? -1) > (rightOf(a) ?? -1) ? b : a));
  const b = rightOf(best)!;
  const lines = [`If ${b}${n !== undefined ? ` of ${n}` : ''} is good enough for this job, lower the pass mark to ${b} or below and ${best.name} would pass.`];
  const at = ladder.rungs.indexOf(best);
  const cheaper = scored.filter((r) => ladder.rungs.indexOf(r) < at && (rightOf(r) ?? -1) < b);
  if (cheaper.length) {
    const next = cheaper.reduce((x, y) => ((rightOf(y) ?? -1) > (rightOf(x) ?? -1) ? y : x));
    lines.push(`If ${rightOf(next)} is enough, ${next.name} would pass and cost less to run.`);
  }
  return lines;
}

/** Each rung's refusal codes from the result card, by model id. */
export function resultCodes(model: ForgeModel): Record<string, string[]> {
  const rungs = model.result?.data?.rungs;
  const out: Record<string, string[]> = {};
  if (!Array.isArray(rungs)) return out;
  for (const raw of rungs) {
    const r = record(raw);
    if (r && typeof r.model === 'string') out[r.model] = Array.isArray(r.codes) ? r.codes.map(String) : [];
  }
  return out;
}

// ---------------------------------------------------------------- add to project

/**
 * What each file of an export is for, from what the export writes (architect/agent/export.py).
 * A file not here shows no purpose.
 */
export const FILE_PURPOSE: Record<string, string> = {
  'README.md': 'How to call it',
  'EVIDENCE.md': 'The exam results',
  'EVIDENCE.json': 'The exam results',
  'MANIFEST.json': 'A fingerprint of every file',
  'artifact/': 'The agent itself',
  'schemas/': 'Input and output formats',
  'tests/': 'Its own tests',
  '__init__.py': 'The code your app imports',
  '.gitattributes': 'Keeps the files byte for byte',
};

/** A file's purpose by its name or its folder ("artifact/harness.py" is the agent itself). */
export function filePurpose(file: string): string | undefined {
  if (FILE_PURPOSE[file]) return FILE_PURPOSE[file];
  const folder = /^([^/]+\/)/.exec(file)?.[1];
  return folder ? FILE_PURPOSE[folder] : undefined;
}

/** The export's size row: "13 files, 22 KB". */
export function exportSize(model: ForgeModel): string | undefined {
  return rowOf(model.exportPlan?.rows, 'size');
}

/** The feature flag line to set, from the Exported card's enable row: "ARCHITECT_FEATURE_4D80F3D5=1". */
export function flagLine(model: ForgeModel): string | undefined {
  const row = rowOf(model.exported?.rows, 'enable');
  return row ? /([A-Z][A-Z0-9_]*=1)\s*$/.exec(row.trim())?.[1] : undefined;
}

/** The export's own test command, the tests row up to its note: "uv run pytest agents/receipt_reader/tests -p no:cacheprovider". */
export function testsCommand(model: ForgeModel): string | undefined {
  const row = rowOf(model.exported?.rows, 'tests');
  const command = row?.split('  ')[0]?.trim();
  return command || undefined;
}

/**
 * What Connect it sends (quiet): the coder reads the export's README and the code that
 * does the job, then changes that call. The flag stays off and the tests are the person's.
 */
export function connectPrompt(model: ForgeModel): string {
  const folder = exportRows(model).folder ?? 'the exported folder/';
  return `Connect the agent in ${folder} to this project's code. Read ${folder}README.md and the code that does this job today, then change that call so it uses the agent's run(). Keep the feature flag off and do not run the tests; I will do both.`;
}

/** Why an export was not written: the latest "not exported (code...)" notice after the latest /export. */
export function exportRefusal(model: ForgeModel, items: Item[]): { code: string; text: string } | null {
  if (model.exported) return null;
  const since = items.findLastIndex((i) => i.kind === 'user' && /^\/export\b/.test(i.text));
  for (let i = items.length - 1; i > since; i -= 1) {
    const item = items[i];
    if (item.kind !== 'notice' || item.level === 'info') continue;
    const m = /^not exported \(([a-z_]+)/i.exec(item.text);
    if (m) return { code: m[1], text: item.text };
  }
  return null;
}

/** A path as the project names it: "C:/rec/expense-app/app/extract.py" in C:\rec\expense-app is "app/extract.py". */
export function projectPath(path: string, cwd: string | undefined): string {
  const p = path.replace(/\\/g, '/');
  const root = (cwd ?? '').replace(/\\/g, '/').replace(/\/+$/, '');
  if (root && p.toLowerCase().startsWith(`${root.toLowerCase()}/`)) return p.slice(root.length + 1);
  return p;
}

/**
 * An edit or write approval's preview (architect's bounded preview: "path (2 edits)",
 * then "- " and "+ " lines): its path, the lines it adds and removes (a line diff of the
 * removed against the added lines), and the function it changes when an edit keeps a
 * `def name(` line. Null when the detail is not a preview.
 */
export function approvalDiff(detail: string | undefined): { path: string; meta: string; added: number; removed: number; fn?: string } | null {
  if (!detail) return null;
  const [head, ...rest] = detail.split('\n');
  const match = /^(.*?) \(([^)]*)\)$/.exec(head ?? '');
  if (!match) return null;
  const before: string[] = [];
  const after: string[] = [];
  for (const line of rest) {
    if (line.startsWith('- ') || line === '-') before.push(line.slice(2));
    else if (line.startsWith('+ ') || line === '+') after.push(line.slice(2));
  }
  const { added, removed } = diffCount(before.join('\n'), after.join('\n'));
  const kept = new Set(before);
  const def = after.map((l) => /^\s*(?:async\s+)?def (\w+)\(/.exec(l)).find((m, i) => m && kept.has(after[i]));
  return { path: match[1], meta: match[2], added, removed, ...(def ? { fn: def[1] } : {}) };
}

/**
 * The most lines of each side of one change the core's approval preview carries
 * (architect/agent/coder.py PREVIEW_LINES), and the most characters of the whole preview
 * (DETAIL_CHARS, past which it ends in "..."). A side this long may have been cut.
 */
export const PREVIEW_LINES = 12;
const DETAIL_CHARS = 1500;

/** One line of a change as the Add step shows it: kept, added, removed, or past where the preview was cut. */
export interface PreviewLine { kind: 'ctx' | 'add' | 'del' | 'unseen'; text: string }

export interface PreviewChange {
  lines: PreviewLine[];
  added: number;
  removed: number;
  /** The preview stops partway through this change: the lines past that point are not compared. */
  cut: boolean;
  /** A side reached the preview's line limit with nothing left over, so the change may go on past it. */
  mayGoOn: boolean;
}

const MORE = /^\.\.\. \d+ more lines?$/;

/**
 * An edit or write approval's preview as changes, each a line diff of its old text
 * against its new text: the preview lists each change's removed lines, then its added
 * lines, so a run of "- " then "+ " lines is one change. Lines both sides keep show as
 * context instead of a removal and an addition. When a side was cut at the preview's
 * limit, what the other side has past that point is marked `unseen`, never removed or
 * added, and the change says it was cut. Null when the detail is not a preview.
 */
export function previewChanges(detail: string | undefined): { path: string; changes: PreviewChange[]; cut: boolean } | null {
  if (!detail) return null;
  const [head, ...rest] = detail.split('\n');
  const match = /^(.*?) \(([^)]*)\)$/.exec(head ?? '');
  if (!match) return null;
  const clipped = detail.length >= DETAIL_CHARS - 3 && detail.endsWith('...');
  type Side = { lines: string[]; more: boolean };
  const raw: Array<{ before: Side; after: Side }> = [];
  let phase: '-' | '+' | null = null;
  for (const line of rest) {
    const sign = line.startsWith('- ') || line === '-' ? '-' : line.startsWith('+ ') || line === '+' ? '+' : null;
    if (!sign) continue;
    const text = line.slice(2);
    if (sign === '-' && phase !== '-') raw.push({ before: { lines: [], more: false }, after: { lines: [], more: false } });
    if (sign === '+' && phase === null) raw.push({ before: { lines: [], more: false }, after: { lines: [], more: false } });
    phase = sign;
    const side = sign === '-' ? raw.at(-1)!.before : raw.at(-1)!.after;
    if (MORE.test(text.trim())) side.more = true;
    else side.lines.push(text);
  }
  const changes = raw.map(({ before, after }, i): PreviewChange => {
    const last = clipped && i === raw.length - 1;
    if (last) {
      // The whole preview stopped mid-line: drop its "..." and treat both sides as cut.
      const side = after.lines.length ? after : before;
      if (side.lines.length) side.lines[side.lines.length - 1] = side.lines.at(-1)!.replace(/\.\.\.$/, '');
    }
    // A side the preview marked as cut, or one that reached its limit and so may have been.
    const beforeCut = last || before.more || before.lines.length >= PREVIEW_LINES;
    const afterCut = last || after.more || after.lines.length >= PREVIEW_LINES;
    const definite = last || before.more || after.more;
    const lines = alignLines(before.lines, after.lines);
    if (afterCut) {
      const seen = lines.findLastIndex((l) => l.kind === 'ctx' || l.kind === 'add');
      lines.forEach((l, j) => { if (j > seen && l.kind === 'del') l.kind = 'unseen'; });
    }
    if (beforeCut) {
      const seen = lines.findLastIndex((l) => l.kind === 'ctx' || l.kind === 'del');
      lines.forEach((l, j) => { if (j > seen && l.kind === 'add') l.kind = 'unseen'; });
    }
    const unseen = lines.some((l) => l.kind === 'unseen');
    return {
      lines,
      added: lines.filter((l) => l.kind === 'add').length,
      removed: lines.filter((l) => l.kind === 'del').length,
      cut: definite || unseen,
      mayGoOn: !definite && !unseen && (beforeCut || afterCut),
    };
  });
  return { path: match[1], changes, cut: changes.some((c) => c.cut || c.mayGoOn) };
}

/**
 * A line diff of two short line lists (a preview's sides, at most a few lines each): the
 * longest run of kept lines, matched as early as they can be, so lines a cut side never
 * reached fall at the end. Between kept lines, removed lines come before added ones.
 */
function alignLines(a: string[], b: string[]): PreviewLine[] {
  const n = a.length;
  const m = b.length;
  // lcs[i][j]: the longest common run of a[i..] and b[j..].
  const lcs = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const out: PreviewLine[] = [];
  let i = 0;
  let j = 0;
  while (i < n || j < m) {
    if (i < n && j < m && a[i] === b[j] && lcs[i][j] === lcs[i + 1][j + 1] + 1) {
      out.push({ kind: 'ctx', text: a[i] });
      i += 1;
      j += 1;
      continue;
    }
    const canDel = i < n && (j >= m || lcs[i + 1][j] === lcs[i][j]);
    const canAdd = j < m && (i >= n || lcs[i][j + 1] === lcs[i][j]);
    if (canAdd && !canDel) {
      out.push({ kind: 'add', text: b[j] });
      j += 1;
    } else {
      out.push({ kind: 'del', text: a[i] });
      i += 1;
    }
  }
  return out;
}

/**
 * The coder's words before an edit, split when it numbers what it will do ("Now I'll
 * make the requested modifications: 1. Add the imports 2. Change EXTRACTOR ..."): the
 * numbered steps as a list, else its first two sentences. Its own words, never a summary.
 */
export function saidSteps(text: string | undefined): { lead?: string; steps: string[] } {
  const said = (text ?? '').replace(/\s+/g, ' ').trim();
  if (!said) return { steps: [] };
  const first = /(^|\s)1\.\s/.exec(said);
  if (first) {
    const lead = said.slice(0, first.index).trim().replace(/:$/, '');
    const parts = said.slice(first.index).trim().split(/\s(?=\d+\.\s)/);
    const steps = parts.map((p) => p.replace(/^\d+\.\s*/, '').trim()).filter(Boolean);
    const numbered = parts.every((p, i) => p.startsWith(`${i + 1}.`));
    if (steps.length >= 2 && numbered) return { ...(lead ? { lead } : {}), steps };
  }
  const sentences = said.match(/[^.!?]+[.!?]+(\s|$)/g) ?? [said];
  const lead = sentences.slice(0, 2).join('').trim();
  return { lead: lead.length > 280 ? `${lead.slice(0, 277).trimEnd()}...` : lead, steps: [] };
}

/** An approval the connect step answers: an edit, a new file or a command. */
export function connectAction(ask: Ask): 'edit' | 'write' | 'bash' | undefined {
  return ask.kind === 'approval' && (ask.action === 'edit' || ask.action === 'write' || ask.action === 'bash') ? ask.action : undefined;
}

/** A coder line that only echoes a tool ("read C:/...", "edit C:/...: +15 -3 lines"), not something it said. */
const TOOL_ECHO = /^(read|edit|write|bash|list|ls|glob|grep|search|run|find)\b\s+\S/i;

export interface ConnectState {
  /** The coder took the job: a `code` step, or an edit, write or command approval, after the Exported card. */
  started: boolean;
  /** The approval or question waiting on the person now. */
  open?: Ask;
  /** Every edit, write and command approval after the Exported card, with the person's answer. */
  edits: Array<{ ask: Ask; answer?: string | null }>;
  /** How many of them the person allowed. */
  allowed: number;
  /** What the coder reports it changed after an allowed edit ("edit <path>: +15 -3 lines"). */
  applied: Array<{ action: 'edit' | 'write'; path: string; added: number; removed: number }>;
  /** The coder's last words before the open approval (or the latest, when none is open). */
  said?: string;
  /** An error notice after the Exported card: the connect step failed. */
  failure?: string;
  /** The person typed a message after the Exported card (a running turn is theirs, not the connect). */
  userAfter: boolean;
}

/** The connect step from the items after the Exported card. */
export function connectState(items: Item[]): ConnectState {
  const from = items.findLastIndex((i) => i.kind === 'plan' && i.title.startsWith('Exported'));
  const state: ConnectState = { started: false, edits: [], allowed: 0, applied: [], userAfter: false };
  if (from < 0) return state;
  let said: string | undefined;
  for (const item of items.slice(from + 1)) {
    if (item.kind === 'user') state.userAfter = true;
    else if (item.kind === 'work') {
      for (const step of item.steps) {
        if (step.name === 'code' && !step.subagent) state.started = true;
        const lines = step.subagent ? [{ agent: step.name, text: step.summary }, ...step.lines] : step.lines;
        for (const line of lines) {
          if (line.agent !== 'coder') continue;
          const done = /^(edit|write) (.+?): \+(\d+) -(\d+) lines?$/.exec(line.text.trim());
          if (done) state.applied.push({ action: done[1] as 'edit' | 'write', path: done[2], added: Number(done[3]), removed: Number(done[4]) });
          else if (!TOOL_ECHO.test(line.text.trim()) && line.text.trim()) said = line.text.trim();
        }
      }
    } else if (item.kind === 'ask') {
      const action = connectAction(item.ask);
      if (action) {
        state.started = true;
        state.edits.push({ ask: item.ask, ...(item.answer !== undefined ? { answer: item.answer } : {}) });
        if (item.answer === 'yes' || item.answer === 'session') state.allowed += 1;
      }
      if (!item.closed && item.answer === undefined) {
        state.open = item.ask;
        state.said = said;
      }
    } else if (item.kind === 'notice' && item.level === 'error') {
      state.failure = item.text;
    }
  }
  if (!state.open && said) state.said = said;
  return state;
}

/** A question's words without its numbered choices, for the connect step's own question. */
export function questionText(ask: Ask): string {
  return cleanQuestion(ask.question ?? '');
}
