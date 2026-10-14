/**
 * One vocabulary on screen: practice (the cases the builder saw), exam (the cases it never
 * saw), bar, licensed and refused. The CLI's words ("floor", "tuning", "held-out") stay in
 * the terminal, code and logs; this turns the build's cards and lines into the app's words.
 */
import type { Conv } from '../store/app';
import { cleanQuestion, numbered } from './asks';
import type { ForgeModel, ForgePhase } from './forge';
import { prettyModel, prettyModels, rungName } from './models';
import type { Ask, Item } from './timeline';
import { fmtUsd } from './viz';

/** Fixed failure codes as a few plain words (anything unknown keeps its code). */
const REASONS: Record<string, string> = {
  primary_floor_failed: 'below the bar on the exam',
  primary_budget_failed: 'over budget on the exam',
  primary_invalid_exceeded: 'too many invalid outputs',
  primary_sample_count: 'too few exam cases',
  primary_evaluation_failed: 'some exam runs failed',
  primary_evaluation_error: 'the exam could not run',
  secondary_floor_failed: 'below the second bar',
  tuning_failed: 'below the bar in practice',
  artifact_changed: 'changed after the exam',
  artifact_unreadable: 'unreadable after the exam',
  compile_budget: 'the build ran out of budget',
  selection_failed: 'no preset fits this model',
  no_candidates: 'no candidate to test',
  tuning_configuration_failed: 'practice could not be set up',
  forge_no_submission: 'Forge submitted nothing',
  forge_failed: 'Forge stopped',
  unverified: 'not tested',
  unlicensed: 'not licensed',
  // A retake, a practice run or an undo that was refused (lib/retake.ts has the sentences).
  undo_only: 'this change can only be undone',
  edit_invalid: 'the edited file has a mistake to fix first',
  not_a_preset_agent: 'Forge wrote this agent, and its exam cannot be retaken',
  exam_no_retakes: 'this exam takes no retakes',
  exam_not_configured: "this agent's exam is not in Architect's settings here",
  exam_changed: "the exam's cases are not the ones this agent passed on",
  model_not_in_profile: "the model is not in Architect's settings here",
  plan_names_model: "the agent's plan allows only the model it names",
  no_copy: 'this computer has no copy of the version that passed',
  not_written: "the agent's folder could not be written",
  not_stopped: 'nothing changed since its exam',
  not_placed: 'not added to a project',
  busy: 'a run is already going',
};

export function reasonWords(code: string): string {
  return REASONS[code] ?? code.replace(/_/g, ' ');
}

/** The first code that explains a refusal, in words ("over budget on the exam"). */
export function refusalReason(codes: string[]): string | undefined {
  const order = ['primary_budget_failed', 'primary_invalid_exceeded', 'tuning_failed', 'primary_sample_count',
    'artifact_changed', 'primary_evaluation_failed', 'primary_evaluation_error', 'compile_budget', 'primary_floor_failed'];
  const known = order.find((code) => codes.includes(code)) ?? codes[0];
  return known ? reasonWords(known) : undefined;
}

/**
 * Practice and exam in place of tuning and held-out, and build in place of compile, in
 * any line from the build.
 */
export function practiceWords(text: string): string {
  return text
    .replace(/\bfloor (\d\.\d\d)\/(\d\.\d\d)/g, 'exam $1, bar $2')
    .replace(/(\d+) tuning \/ (\d+) held-out/g, '$1 practice / $2 exam')
    .replace(/held-out (cases|runs)/g, 'exam $1')
    .replace(/\bheld-out\b/g, 'exam')
    .replace(/on tuning\b/g, 'in practice')
    .replace(/\btuning cases\b/g, 'practice cases')
    .replace(/configured floor '([^']+)': (\d+ cases)/g, "$2 from '$1'")
    .replace(/\(SYNTHETIC floor\)/g, '(synthetic cases)')
    .replace(/\bSYNTHETIC\b/g, 'synthetic')
    .replace(/baseline to beat/g, 'preset to beat')
    .replace(/\bthe tuned (\S+) baseline\b/g, 'the $1 preset')
    .replace(/\/budget compile\b/g, '/budget build')
    .replace(/\bcompile up to\b/g, 'build up to')
    .replace(/\bcompile (budget|wallet)\b/g, 'build $1')
    .replace(/\bthe compile and Forge budgets\b/g, 'the build and Forge budgets')
    .replace(/\bnext compile\b/g, 'next build')
    .replace(/\bEnter compiles\b/g, 'Enter builds');
}

/**
 * A build notice in the app's words. The install notice is one short sentence ("Installed
 * receipt-reader, version 1. Exam 0.44, bar 0.40, $0.15 spent."): what Forge did is in
 * Details. A refusal names its reasons; anything else gets practice and exam words.
 */
export function noticeWords(text: string): string {
  const refused = /^not licensed \(([a-z0-9_, ]+)\); next: ([\s\S]*)$/.exec(text);
  if (refused) return `Refused (${refused[1].split(', ').map(reasonWords).join(', ')}). Next: ${practiceWords(refused[2])}`;
  const installed = /^installed (\S+) v(\d+): floor (\d\.\d\d)\/(\d\.\d\d)( \(SYNTHETIC floor\))?, (\$[\d.,]+) spent\./.exec(text);
  if (installed) {
    const [, name, version, score, bar, synthetic, usd] = installed;
    return `Installed ${name}, version ${version}. Exam ${score}${synthetic ? ' on synthetic cases' : ''}, bar ${bar}, ${usd} spent.`;
  }
  return sentences(practiceWords(text));
}

/** A question from the build in the app's words: the CLI asks "Compile this agent?". */
export function questionWords(text: string): string {
  return text.replace(/^Compile this agent\?/, 'Build this agent?');
}

/**
 * Sentence case for the CLI's lower-case lines: a sentence that opens with a plain word,
 * hyphens allowed, gets a capital ("installed x" reads "Installed x", "per-run budget ..."
 * reads "Per-run budget ..."); a path, a field name or a name before a colon keeps its case.
 */
function sentences(text: string): string {
  return text.replace(/(^|\. )([a-z])(?=[a-z-]*[ ,;])/g, (_all, lead: string, letter: string) => lead + letter.toUpperCase());
}

/**
 * A result card's floor row in the app's words: "1.00/0.90 on 6 held-out cases (correct
 * 6/6 · invalid 0)" reads "1.00 on 6 exam cases, bar 0.90 (6 of 6 correct, 0 invalid)",
 * and a refusal's codes become its reasons.
 */
export function examWords(text: string): string {
  const scored = /^(\d\.\d\d)\/(\d\.\d\d)/.exec(text);
  const cases = /on (\d+) held-out cases/.exec(text);
  const counts = /\(correct (\d+)\/(\d+) · invalid (\d+)\)/.exec(text);
  const unscored = /\(not scored · invalid (\d+) · over budget (\d+)\)/.exec(text);
  const codes = /\(([a-z0-9_]+(?:, [a-z0-9_]+)*)\)\s*$/.exec(text);
  const details: string[] = [];
  if (counts) details.push(`${counts[1]} of ${counts[2]} correct, ${counts[3]} invalid`);
  if (unscored) details.push(`not scored: ${unscored[1]} invalid, ${unscored[2]} over budget`);
  if (/SYNTHETIC/.test(text)) details.push('synthetic cases');
  if (codes) details.push(...codes[1].split(', ').map(reasonWords));
  const head = scored ? `${scored[1]}${cases ? ` on ${cases[1]} exam cases` : ''}, bar ${scored[2]}` : 'No score';
  return details.length ? `${head} (${details.join('; ')})` : head;
}

const PLAN_LABELS: Record<string, string | null> = {
  task: 'Task',
  spec: null, // drawn as a hash line
  'forge pack': null, // drawn as a hash line
  engineer: 'Engineer',
  pipeline: 'Pipeline',
  inputs: 'Takes',
  outputs: 'Gives back',
  floor: 'Bar',
  'floor data': 'Practice and exam',
  'invalid output': 'Invalid output',
  budget: 'Budget',
  'forge budget': 'Forge budget',
  model: 'Models',
  destination: 'Destination',
  'install as': 'Install as',
  note: 'Note',
  design: 'Design',
  cost: 'Cost',
  next: 'Next',
};

/**
 * Rows whose value is a sentence the terminal may write in lower case ("counts as a miss,
 * ...", "run it here: ..."): on a card it starts with a capital. Field lists, names, the task
 * and figures keep their case.
 */
const SENTENCE_ROWS = new Set(['engineer', 'invalid output', 'forge budget', 'destination', 'note', 'next']);

/**
 * A plan or result card's rows in the app's words: labels and sentences in sentence case,
 * model ids by name, the bar as a number, practice and exam for tuning and held-out. The
 * spec and Forge pack hashes leave the rows; the card draws them as hash lines.
 */
export function cardRows(rows: Array<[string, string]>, opts: { result?: boolean; models?: string[] } = {}): Array<[string, string]> {
  const models = opts.models ?? [];
  const out: Array<[string, string]> = [];
  for (const [key, raw] of rows) {
    const label = key in PLAN_LABELS ? PLAN_LABELS[key] : key.charAt(0).toUpperCase() + key.slice(1);
    if (label === null) continue;
    // The terminal's `code` marks read as plain words on a card.
    let value = prettyModels(raw, models).replace(/`([^`]+)`/g, '$1');
    if (key === 'floor' && opts.result) {
      out.push(['Exam', examWords(value)]);
      continue;
    }
    if (key === 'floor') {
      // "held_out_tasks >= 0.80 on 'receipts'": the bar is the number.
      const bar = /^held_out_tasks >= ([01](?:\.\d+)?)/.exec(raw)?.[1];
      value = bar !== undefined ? Number(bar).toFixed(2) : practiceWords(value);
    } else if (key === 'model') {
      value = modelsWords(raw, models);
    } else {
      value = practiceWords(value);
      if (SENTENCE_ROWS.has(key)) value = sentences(value);
    }
    out.push([label, value]);
  }
  return out;
}

/**
 * The model ids a card's rows may name: a plan's ladder, or on a result card each rung's
 * model, in full and as the ladder row writes it (after the last "/").
 */
export function cardModels(data?: Record<string, unknown>): string[] {
  if (Array.isArray(data?.ladder)) return data.ladder as string[];
  const rungs = Array.isArray(data?.rungs) ? (data.rungs as Array<{ model?: unknown }>) : [];
  const ids = rungs.map((r) => (typeof r.model === 'string' ? r.model : '')).filter(Boolean);
  return [...ids, ...ids.map((id) => id.slice(id.lastIndexOf('/') + 1))];
}

/** "cheapest that passes over a, b" as "The cheapest one that clears the bar: A or B". */
function modelsWords(raw: string, models: string[]): string {
  const ladder = /^cheapest that passes over (.+)$/.exec(raw);
  if (!ladder) return prettyModels(raw, [...models, raw.replace(/ \(local, \$0\)$/, '')]);
  const names = ladder[1].split(/,\s*/).map((id) => prettyModel(id));
  const list = names.length < 2 ? names.join('') : `${names.slice(0, -1).join(', ')} or ${names.at(-1)}`;
  return `The cheapest one that clears the bar: ${list}`;
}

/**
 * The note in a "<hash> (note)" row: "frozen, receipt-reader.spec.json"; the Forge pack's
 * check reads "holds no text from this task" (or where it found some).
 */
export function hashNote(raw: string | undefined): string | undefined {
  const note = raw ? /\(([^)]+)\)\s*$/.exec(raw)?.[1] : undefined;
  if (!note) return undefined;
  if (note === 'no text from this task') return 'holds no text from this task';
  const found = /^(\d+) match(?:es)? with this task's text$/.exec(note);
  if (found) return `holds text from this task in ${found[1]} ${found[1] === '1' ? 'place' : 'places'}`;
  return practiceWords(note);
}

/** A slash command's line in the menu (usage, then what it does), in the app's words. */
export interface CommandLine { name: string; usage: string; summary: string }

/** What a command does, said the way the app says it; any other command keeps its words. */
const COMMAND_WORDS: Record<string, string> = {
  '/agents': 'Installed agents, their versions, licenses and bars',
  '/budget': 'Show or change the session, per-run or build budget',
  '/floor': "Show or change the next build's bar",
  '/logout': "Forget a provider's stored key, or sign out",
  '/make': 'Build an agent from a description or a spec file',
  '/model': 'Pick the model for this conversation and the next build; no argument lists them',
  '/recompile': 'Build an agent again, with a fresh exam',
  '/report': "An agent's exam report and license",
};

export function commandWords(command: CommandLine): CommandLine {
  return {
    name: command.name,
    usage: command.name === '/budget' ? command.usage.replace(/\bcompile </, 'build <') : command.usage,
    summary: COMMAND_WORDS[command.name] ?? practiceWords(command.summary),
  };
}

// ---------------------------------------------------------------- the agent card (redesign v2 §3.6)

/**
 * Screen words for a judge: the closed list in redesign-v2 §1 (pass mark, passed, did not
 * pass, practice, exam). The Report and the terminal keep the originals. `short` also
 * cuts a Nemotron to its tier ("Nemotron 3.5 Lightning" reads "Lightning").
 */
export function screenWords(text: string, short = false): string {
  const cap = (word: string, like: string) => (/^[A-Z]/.test(like) ? word.charAt(0).toUpperCase() + word.slice(1) : word);
  let out = text
    .replace(/\b(not licensed|refused)\b/gi, (m) => cap('did not pass', m))
    .replace(/\blicensed\b/gi, (m) => cap('passed', m))
    .replace(/\bheld-out\b/gi, (m) => cap('exam', m))
    .replace(/\btuning\b/gi, (m) => cap('practice', m))
    .replace(/\bbars\b/gi, (m) => cap('pass marks', m))
    .replace(/\bbar\b/gi, (m) => cap('pass mark', m));
  if (short) out = out.replace(/\bNemotron \d+(?:\.\d+)? (\w+)/g, '$1');
  return out;
}

type Rows = Array<[string, string]>;
const rowOf = (rows: Rows | undefined, key: string): string | undefined => rows?.find(([k]) => k === key)?.[1];
const num = (v: unknown): number | undefined => (typeof v === 'number' && Number.isFinite(v) ? v : undefined);
const sentenceCase = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);
/** "item_count, subtotal" reads "item count, subtotal". */
const fieldList = (text?: string) => (text ?? '').split(/,\s*/).map((f) => f.trim().replace(/_/g, ' ')).filter(Boolean);

/** The bar as a number: the plan's data, the result's, else its floor row ("held_out_tasks >= 0.40"). */
function barOf(model: ForgeModel): number | undefined {
  const bar = num(model.plan?.data?.bar) ?? num(model.result?.data?.bar);
  if (bar !== undefined) return bar;
  const floor = /(?:>=|<=)\s*([01](?:\.\d+)?)/.exec(rowOf(model.plan?.rows, 'floor') ?? '');
  return floor ? Number(floor[1]) : undefined;
}

/** The plan's exam case count: its data, else the "N held-out" in its floor data row. */
export function examCount(model: ForgeModel): number | undefined {
  const exam = num(model.plan?.data?.exam);
  if (exam !== undefined) return exam;
  const held = /(\d+) held-out/.exec(rowOf(model.plan?.rows, 'floor data') ?? '');
  return held ? Number(held[1]) : undefined;
}

/** How many practice cases the plan has: its data, else the "N tuning" count in its floor data row. */
export function practiceCount(model: ForgeModel): number | undefined {
  const practice = num(model.plan?.data?.practice);
  if (practice !== undefined) return practice;
  const tuning = /(\d+) tuning/.exec(rowOf(model.plan?.rows, 'floor data') ?? rowOf(model.plan?.rows, 'floor') ?? '');
  return tuning ? Number(tuning[1]) : undefined;
}

/** The pass mark as cases ("40 of 100"), or as a share when the plan does not say how many ("90%"). */
export function passMark(model: ForgeModel): string | undefined {
  const bar = barOf(model);
  if (bar === undefined) return undefined;
  const exam = examCount(model);
  return exam ? `${Math.ceil(bar * exam - 1e-9)} of ${exam}` : `${Math.round(bar * 100)}%`;
}

/** The models the plan tries, by tier: "Lightning, Super, Ultra". */
function modelsTried(model: ForgeModel): string | undefined {
  const ladder = model.plan?.data?.ladder;
  if (Array.isArray(ladder) && ladder.length) return (ladder as string[]).map(rungName).join(', ');
  const row = rowOf(model.plan?.rows, 'model');
  if (!row) return undefined;
  const over = /^cheapest that passes over (.+)$/.exec(row);
  if (over) return over[1].split(/,\s*/).map(rungName).join(', ');
  return rungName(row.replace(/ \(local, \$0\)$/, ''));
}

/** The most the build may spend and take: "$0.60, 60 min". */
function buildLimit(model: ForgeModel): string | undefined {
  const data = model.plan?.data ?? {};
  const usd = num(data.build_usd);
  const seconds = num(data.build_s);
  if (usd !== undefined && seconds !== undefined) return `${fmtUsd(usd)}, ${Math.round(seconds / 60)} min`;
  const budget = /compile up to (\$[\d.,]+) and (\d+)\s*(min|m|s)\b/.exec(rowOf(model.plan?.rows, 'budget') ?? '');
  if (!budget) return undefined;
  const minutes = budget[3] === 's' ? Math.round(Number(budget[2]) / 60) : Number(budget[2]);
  return `${budget[1]}, ${minutes} min`;
}

/** The cases: "100 practice, 100 exam". */
function caseCounts(model: ForgeModel): string | undefined {
  const practice = practiceCount(model);
  const exam = examCount(model);
  return practice !== undefined && exam !== undefined ? `${practice} practice, ${exam} exam` : undefined;
}

/** The job as the card says it: the task's first line, else the person's sentence. */
export function jobLine(model: ForgeModel): string | undefined {
  const task = rowOf(model.plan?.rows, 'task') ?? model.request;
  return task?.trim().split('\n')[0].trim() || undefined;
}

/**
 * The plan as Facts (redesign v2 §1 step 2): Job first and wide, then Takes, Gives back,
 * Cases, Pass mark, Models tried, Build limit, and Rules when the spec has checks. A value
 * the plan does not carry leaves its cell out.
 */
export function planFacts(model: ForgeModel): Array<{ value: string; label: string; wide?: boolean }> {
  const rows = model.plan?.rows;
  const takes = fieldList(rowOf(rows, 'inputs'));
  const gives = fieldList(rowOf(rows, 'outputs'));
  const checks = rowOf(rows, 'checks');
  const rules = checks ? checks.split(/(?<=[.!?])\s+(?=[A-Z])/).filter((s) => s.trim()).length : 0;
  const cells: Array<{ value: string | undefined; label: string; wide?: boolean }> = [
    { value: jobLine(model), label: 'Job', wide: true },
    { value: takes.length ? sentenceCase(takes.join(', ')) : undefined, label: 'Takes' },
    { value: gives.length ? sentenceCase(gives.join(', ')) : undefined, label: 'Gives back' },
    { value: caseCounts(model), label: 'Cases' },
    { value: passMark(model), label: 'Pass mark' },
    { value: modelsTried(model), label: 'Models tried' },
    { value: buildLimit(model), label: 'Build limit' },
    { value: rules ? `${rules} ${rules === 1 ? 'check' : 'checks'} after each reply` : undefined, label: 'Rules' },
  ];
  return cells.filter((c): c is { value: string; label: string; wide?: boolean } => !!c.value);
}

/**
 * The agent's name from the plan's rows when its title does not carry one (a blocked plan
 * is titled "Plan (blocked)"): the install row's first word, "receipt-reader".
 */
export function planName(model: ForgeModel): string | undefined {
  const install = rowOf(model.plan?.rows, 'install as')?.trim().split(/\s+/)[0];
  return install && /^[\w.-]+$/.test(install) ? install : undefined;
}

/** What Try it's one field is called: the plan's Takes when it names one thing ("Receipt text"). */
export function takesLabel(model: ForgeModel): string | undefined {
  const takes = fieldList(rowOf(model.plan?.rows, 'inputs'));
  return takes.length === 1 ? sentenceCase(takes[0]) : undefined;
}

/**
 * The plan's spec rows in the screen's words, for the Plan sheet: pass mark for bar, the
 * harness row said plainly, field names with spaces, models by their short names. The
 * Report and the terminal keep the originals.
 */
export function sheetRows(rows: Array<[string, string]>): Array<[string, string]> {
  const LABELS: Record<string, string> = { Bar: 'Pass mark', Engineer: 'Harness', 'Install as': 'Name', Models: 'Models tried' };
  return rows.map(([label, value]) => {
    let v = value;
    if (label === 'Engineer') {
      const forge = /^Forge \(([^)]+)\) writes a custom harness(?:; preset to beat: (.+))?$/i.exec(v);
      if (forge) v = `${screenWords(forge[1], true)} writes one for this job${forge[2] ? `, starting from ${forge[2].replace(/_/g, ' ')}` : ''}`;
    }
    if (label === 'Takes' || label === 'Gives back') v = sentenceCase(fieldList(v).join(', '));
    if (label === 'Models') v = v.replace(/^The cheapest one that clears the bar: /, 'Cheapest past the pass mark: ');
    v = screenWords(v, true);
    return [LABELS[label] ?? label, v];
  });
}

/** The plan's one yes: "Build for up to $0.60"; "Build it" when the plan has no build limit. */
export function buildLabel(model: ForgeModel): string {
  const usd = num(model.plan?.data?.build_usd);
  return usd !== undefined ? `Build for up to ${fmtUsd(usd)}` : 'Build it';
}

/**
 * A numbered question's options: each choice (the value to answer with) with its words
 * from the question text, parentheses dropped ("Run it here (installed)" reads "Run it
 * here"). Null when the question has no choices.
 */
export function questionOptions(ask: Ask): Array<{ value: string; label: string }> | null {
  const choices = ask.choices ?? [];
  if (!choices.length) return null;
  const { labels } = numbered(cleanQuestion(ask.question ?? ''), choices);
  return choices.map((value, i) => ({ value, label: (labels[i] ?? value).replace(/\s*\([^)]*\)/g, '').trim() || value }));
}

/**
 * What an export wrote: the folder (from the Exported card's title, else the export
 * plan's), the files the export plan lists, and the line that imports it (the first
 * `from ... import ...` of the Exported card's Python snippet).
 */
export function exportRows(model: ForgeModel): { folder?: string; files: string[]; line?: string } {
  const title = model.exported?.title ?? model.exportPlan?.title;
  const folder = title ? title.replace(/^[^:]*:\s*/, '').trim() || undefined : undefined;
  const files = (rowOf(model.exportPlan?.rows, 'files') ?? '').split('\n').map((f) => f.trim()).filter(Boolean);
  const python = rowOf(model.exported?.rows, 'python') ?? '';
  const line = python.split('\n').map((l) => l.trim()).find((l) => /^from \S+ import /.test(l));
  return { ...(folder ? { folder } : {}), files, ...(line ? { line } : {}) };
}

/**
 * The exam result as the card's lead: correct of the exam's cases, whether it passed, and
 * the tier that ships ("Super"). From the result card's signed counts, else its floor row
 * ("correct 44/100"). Null before a result, or when the exam was not scored.
 */
export function resultLead(model: ForgeModel): { correct: number; of: number; ships?: string; passed: boolean } | null {
  const result = model.result;
  if (!result) return null;
  const data = result.data ?? {};
  const exam = data.exam as { cases?: unknown; correct?: unknown } | undefined;
  let correct = num(exam?.correct);
  let of = num(exam?.cases);
  if (correct === undefined || of === undefined) {
    const floor = rowOf(result.rows, 'floor') ?? '';
    const counted = /correct (\d+)\/(\d+)/.exec(floor);
    const scored = /^(\d\.\d\d)\/\d\.\d\d on (\d+)/.exec(floor);
    if (counted) [correct, of] = [Number(counted[1]), Number(counted[2])];
    else if (scored) [correct, of] = [Math.round(Number(scored[1]) * Number(scored[2])), Number(scored[2])];
  }
  if (correct === undefined || of === undefined) return null;
  const passed = result.licensed;
  const id = typeof data.model === 'string' ? data.model : undefined;
  return { correct, of, passed, ...(passed && id ? { ships: rungName(id) } : {}) };
}

/**
 * What Build again sends: `/make @<file>` when the build came from a spec file (the
 * person's own `/make @...`, else the plan's frozen file), else `/make <sentence>`.
 * architect/agent/commands.py: `/make @file` compiles that spec as written and `/make
 * <text>` starts a build-mode turn; a bare sentence after the first would go to the model,
 * so the command is spelled out.
 */
export function rerunCommand(model: ForgeModel): string {
  const request = model.request?.trim() ?? '';
  if (request.startsWith('@')) return `/make ${request}`;
  const frozen = model.plan?.data?.frozen;
  if (typeof frozen === 'string' && frozen) return `/make @${/\s/.test(frozen) ? `"${frozen}"` : frozen}`;
  return `/make ${request}`;
}

/**
 * A build line in the card's words: practice and exam, the pass mark for the bar, models
 * by tier. Gentler than `screenWords`: a relay that refused a call still refused it.
 */
export function cardWords(text: string): string {
  return practiceWords(text).replace(/\bbar\b/g, 'pass mark').replace(/\bNemotron \d+(?:\.\d+)? (\w+)/g, '$1');
}

/**
 * A result's next step in the card's words: what to do, without the terminal command that
 * does it ("raise the compile budget: /budget compile 1.20 2 (...)" reads "Raise the
 * compile budget."). The Plan sheet the card's button opens is where it gets done.
 */
export function stepWords(text: string): string {
  const command = String.raw`\/[^\s,;]+(?:\s+[\d.]+)*`;
  const what = (text.includes(':') ? text.slice(0, text.indexOf(':')) : text)
    .replace(/\s*\([^)]*\)/g, '')
    .replace(new RegExp(String.raw`\s*\bwith\s+` + command, 'g'), '')
    .replace(new RegExp(String.raw`\s*` + command, 'g'), '')
    .replace(/\s+,/g, ',').replace(/\s+/g, ' ').trim().replace(/[.,;]+$/, '');
  return what ? `${what.charAt(0).toUpperCase()}${what.slice(1)}.` : '';
}

/** The first sentence of a line. */
export function firstSentence(text: string): string {
  return (/^[\s\S]*?[.!?](?=\s|$)/.exec(text)?.[0] ?? text).trim();
}

/**
 * The card's one line about a problem: the last warn or error notice in plain words, cut
 * to one sentence. `settings` when it names the judge code or a key (a retry cannot fix
 * that), `retry` for any other error, nothing for a warning. A refusal's own notice is
 * left out: the Did not pass card already says it.
 */
export function noticeLine(model: ForgeModel): { level: 'warn' | 'err'; text: string; action: 'retry' | 'settings' | null } | null {
  const last = [...model.notices].reverse()
    .find((n) => (n.level === 'warn' || n.level === 'error' || n.level === 'err') && !/^not licensed \(|^licensed, but not installed/.test(n.text));
  if (!last) return null;
  const level = last.level === 'warn' ? 'warn' : 'err';
  const settings = /judge code|api key|\bkey\b|sign in|signed out/i.test(last.text);
  return { level, text: firstSentence(cardWords(noticeWords(last.text))), action: settings ? 'settings' : level === 'err' ? 'retry' : null };
}

/**
 * The card's state (redesign v2 §3.6): the build's phase, with four states derived on top.
 *   starting  the conversation is not ready yet: primaries wait
 *   stopped   a plan or build that ended without a result, or a conversation that died before one
 *   adding    the export was approved and the coder is wiring the agent in
 *   result    an export plan with no open approval and nothing running: the person said no
 */
export type CardState = ForgePhase | 'starting' | 'stopped' | 'adding' | 'result';

export function cardState(model: ForgeModel, conv: Pick<Conv, 'status' | 'error' | 'timeline'>): CardState {
  const running = conv.timeline.running;
  const dead = conv.status === 'failed' || conv.status === 'stopped';
  const unfinished = model.phase === 'interview' || model.phase === 'planning' || model.phase === 'building' || model.phase === 'plan';
  if (dead && unfinished && !model.result) return 'stopped';
  if (!dead && conv.status !== 'ready') return 'starting';
  if (!running && !model.result && (model.phase === 'planning' || model.phase === 'building' || (model.phase === 'interview' && !model.question))) return 'stopped';
  if (model.phase === 'exporting' || (model.phase === 'exported' && running)) {
    const asks = conv.timeline.items.filter((i): i is Extract<Item, { kind: 'ask' }> => i.kind === 'ask' && i.ask.kind === 'approval' && i.ask.action === 'export');
    const last = asks.at(-1);
    // An answer sent counts at once: the bridge's ask_closed may come a moment later.
    const settled = !!last && (last.closed || last.answer !== undefined);
    if (model.phase === 'exporting' && last && !settled) return 'exporting';
    if (running && (model.phase === 'exported' || (settled && !declined(last!.answer)))) return 'adding';
    return model.phase === 'exported' ? 'exported' : 'result';
  }
  return model.phase;
}

/**
 * The build composer's placeholder, by card state: what typing there does at this step,
 * in four words or fewer. The Describe field has none (its card's title is the prompt).
 */
export function composerHint(state: CardState): string {
  switch (state) {
    case 'starting': return 'Starting';
    case 'interview': return 'Answer in your own words';
    case 'planning': return 'Add a detail';
    case 'plan': case 'blocked': return 'Ask for a change';
    case 'building': return 'Add a note for the build';
    case 'adding': return 'Add a note';
    case 'set_aside': case 'exported': return 'Describe another agent';
    case 'stopped': return 'Ask about this build';
    default: return 'Ask about this agent';
  }
}

/** An approval's answer that said no: anything but yes or session (a closed ask with no answer was cancelled). */
function declined(answer: string | null | undefined): boolean {
  return answer !== 'yes' && answer !== 'session';
}
