/**
 * The retake panel's data and its words (design: retake spec v2, section 5).
 *
 * An agent Architect builds only runs if it passed its exam; change it, and it stops
 * until it passes again. The bridge's `retake_status` says where the agent in the
 * project stands; everything on screen is said here, once, from its counts. The stop
 * sentence and the lines under it come from Python (`lines`), and so do a retake's
 * attempt, cases and cost (`plan.head`, `plan.cost`), so the app, the terminal and this
 * page say the same thing.
 */
import { fmtUsd } from './viz';

export type RetakeCases = 'first_exam' | 'unused' | 'same_exam';

export interface RetakeAttempt {
  number: number;
  cases: RetakeCases;
  /** `not_placed`: it passed, and its folder was not written. */
  result: 'passed' | 'did_not_pass' | 'cancelled' | 'started' | 'not_placed';
  right: number | null;
  total: number | null;
  /** What the examined change was: "model: Nemotron 3 Super to Nemotron 3.5 Lightning". */
  what: string | null;
  undone: boolean;
}

/** What a retake and a practice run would be, before anything is spent. */
export interface RetakePlan {
  attempt: number;
  cases: 'unused' | 'same_exam';
  total: number;
  pass_mark: number;
  /** Extra exam cases no attempt has used yet. */
  left: number;
  unit: string;
  note: string | null;
  estimate_usd: number | null;
  cap_usd: number;
  practice_total: number;
  practice_estimate_usd: number | null;
  /** Python's sentences: the attempt and its cases, what it costs, what practice costs. */
  head: string;
  cost: string;
  practice_cost: string;
  /** The last attempt passed on this change and only its folder is left to write. */
  place_only: boolean;
}

export interface RetakeProgress {
  attempt: number;
  cases: 'unused' | 'same_exam';
  done: number;
  total: number | null;
  spent_usd: number | null;
  cap_usd: number | null;
  left_after: number | null;
  unit: string | null;
  pass_mark: number | null;
}

export interface RetakeLast extends RetakeAttempt {
  pass_mark?: number | null;
  cost_usd?: number | null;
  seconds?: number | null;
}

export interface RetakeView {
  placed: true;
  folder: string;
  state: 'passed' | 'stopped' | 'retaking' | 'did_not_pass';
  package: string;
  reason: 'changed' | 'changed_undo_only' | 'cannot_check' | null;
  changed_files: string[];
  /** Plain lines from Python, the sentence first. */
  lines: string[];
  can_retake: boolean;
  can_undo: boolean;
  refusal: string | null;
  /** Of the version that passed. */
  attempt: number;
  next_attempt: number;
  model: string | null;
  right: number | null;
  total: number | null;
  pass_mark: number | null;
  unit: string;
  note: string | null;
  cases: RetakeCases;
  /** Whether a change to this agent could be retaken here; null when that is not known. */
  retakes: boolean | null;
  /** What can be done about a stop, in one line. */
  step: string;
  attempts: RetakeAttempt[];
  plan: RetakePlan | null;
  progress: RetakeProgress | null;
  last: RetakeLast | null;
  practice: { running: boolean; lines?: string[]; right?: number | null; total?: number | null } | null;
  /** Why the last retake or practice run did not start or finish, in plain lines. */
  refused: string[] | null;
  /** Set on the reply to `undo`. */
  undone?: string[];
}

export type RetakeReply = RetakeView | { placed: false };

export const REFUSED_BEFORE = "Your app's calls to this agent are refused before any model is called.";
export const NOT_EXAMINED = 'Stopped means not examined yet. It does not mean worse.';
export const UNDO_WORDS = 'Puts back the version that passed, exactly as it was. Your app works again at once. No cost.';
export const UNDONE = "The version that passed is back. Your app's calls work. 0 model calls.";
export const AS_BUILT = 'As built';
export const WRITE_FOLDER = 'Write the folder';
export const BUILD_VERSION = 'Below is the agent as built, which Architect keeps. Try your agent runs that version.';

/** "receipts" as one: "receipt". */
export function oneOf(unit: string): string {
  return unit.endsWith('s') ? unit.slice(0, -1) : unit;
}

const cap = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);
const times = (n: number) => (n === 2 ? 'twice' : `${n} times`);

function listed(items: string[]): string {
  if (items.length <= 1) return items.join('');
  return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;
}

/** The lead under "Passed. It is your agent.": "59 of 100 receipts read correctly. Pass mark: 40 of 100." */
export function passedLead(view: RetakeView): string {
  if (view.right == null || view.total == null) return '';
  const did = view.unit === 'receipts' ? 'read correctly' : 'answered correctly';
  const mark = view.pass_mark != null ? ` Pass mark: ${view.pass_mark} of ${view.total}.` : '';
  return `${view.right} of ${view.total} ${view.unit} ${did}.${mark}`;
}

/** What counts as right: every field of the answer, named when the answer has fields. */
export function rightWords(unit: string, outputSchema: unknown): string {
  const one = oneOf(unit);
  const properties = (outputSchema as { properties?: Record<string, unknown> } | undefined)?.properties;
  const fields = properties && typeof properties === 'object' ? Object.keys(properties).map((f) => f.replace(/_/g, ' ')) : [];
  if (fields.length > 1) return `${cap(/^[aeiou]/i.test(one) ? 'an' : 'a')} ${one} counts as right only when its ${listed(fields)} are all right.`;
  return `A ${one} counts as right only when its whole answer matches the answer key.`;
}

export function examFact(view: RetakeView): string {
  const n = view.total != null ? `${view.total} ${view.unit}` : cap(view.unit);
  return `${n} with known answers. The build did not see them. This was attempt 1.`;
}

/** How to change the agent, said before anything changes. An exam that takes no retakes is not promised one. */
export function ruleWords(folder: string, retakes: boolean | null): string {
  const runs = `Your app runs this agent from ${folder}. If a file in that folder changes, the agent stops`;
  if (retakes === false) return `${runs}. Its exam takes no retakes, so a change to this agent can only be undone.`;
  const until = `${runs} until it passes its exam again.`;
  return retakes ? `${until} To change it: edit, Try on practice, then Retake exam.` : until;
}

/** "73 of 100. Pass mark 40." */
export function scoreWords(right: number | null | undefined, total: number | null | undefined, need: number | null | undefined): string {
  const score = right != null && total != null ? `${right} of ${total}.` : 'No score.';
  return need != null ? `${score} Pass mark ${need}.` : score;
}

/** "Attempt 2, on 100 receipts no earlier attempt used." or "Same exam, attempt 3." */
export function sourceWords(cases: RetakeCases, attempt: number, total: number | null | undefined, unit: string): string {
  if (cases === 'unused') return `Attempt ${attempt}, on ${total ?? ''} ${unit} no earlier attempt used.`.replace('  ', ' ');
  return `Same exam, attempt ${attempt}.`;
}

/** Why a passed retake was the same exam, and what a second score on the same cases is worth. */
export function sameExamPassed(view: RetakeView): string {
  const { unit, total } = view;
  const hadUnused = view.attempts.some((a) => a.cases === 'unused');
  const why = hadUnused ? `Fewer than ${total} unused ${unit} were left` : `This exam has no unused ${unit}`;
  const scored = 1 + view.attempts.filter((a) => a.cases === 'same_exam' && a.right != null).length;
  return `${why}, so this retake used the ${total} ${unit} of the first exam. `
    + `This agent has now been scored on these ${unit} ${times(Math.max(2, scored))}, so this score is easier to reach than a first score.`;
}

/** After "refused before any model is called": what the stop does and does not mean for this change. */
export function stoppedNote(view: RetakeView): string {
  // A change that already passed, with only its folder left to write, was examined.
  if (view.can_retake && view.plan) return view.plan.place_only ? '' : NOT_EXAMINED;
  return view.reason === 'cannot_check' ? '' : 'No exam attempt was used.';
}

/** The paragraph beside Retake exam: the attempt, its cases, the pass mark and the cost, before the click. */
export function retakeWords(plan: RetakePlan): string {
  if (plan.place_only) return `${plan.head} ${plan.cost}`;
  return [plan.head, `It passes with ${plan.pass_mark} or more right, as in its first exam.`, plan.cost].filter(Boolean).join(' ');
}

/** What the second press of Retake exam says: the attempt it starts, or the folder it writes. */
export function confirmWords(plan: RetakePlan): string {
  return plan.place_only ? `Write the folder of attempt ${plan.attempt}` : `Start attempt ${plan.attempt}`;
}

export function practiceWords(plan: RetakePlan): string {
  return [`Scores the change on the ${plan.practice_total} practice ${plan.unit}, which no exam uses. Not an attempt.`, plan.practice_cost].filter(Boolean).join(' ');
}

export function answeredWords(progress: RetakeProgress): string {
  const unit = progress.unit ?? 'cases';
  return `${progress.done} of ${progress.total ?? '?'} ${unit} answered. The score shows at the end.`;
}

export function spentWords(progress: RetakeProgress): string {
  const most = progress.cap_usd != null ? `at most ${fmtUsd(progress.cap_usd)}` : '';
  if (!progress.spent_usd) return most ? `${cap(most)}.` : '';
  return `About ${fmtUsd(Number(progress.spent_usd.toPrecision(1)))} spent so far${most ? `, ${most}` : ''}.`;
}

/** Which cases the running attempt is on. What a later attempt would run on is said when it is offered. */
export function casesWords(progress: RetakeProgress): string {
  const unit = progress.unit ?? 'cases';
  const { total, attempt } = progress;
  if (progress.cases === 'same_exam') return `Same exam, attempt ${attempt}: these are the ${total} ${unit} of the first exam.`;
  return total != null ? `These ${total} ${unit} are ones no earlier attempt used.` : '';
}

export function cancelWords(progress: RetakeProgress): string {
  const unit = progress.unit ?? 'cases';
  const used = progress.cases === 'unused' ? `, and these ${progress.total} ${unit} count as used` : '';
  return `Cancelling still counts as attempt ${progress.attempt}${used}. Your agent stays stopped.`;
}

/** "Changed: artifact/config.json (model: a to b)." as the fact's value. */
export function changedFact(view: RetakeView): string {
  const line = view.lines.find((l) => l.startsWith('Changed: ')) ?? '';
  return line.slice('Changed: '.length).replace(/\.$/, '');
}

/** "This attempt: $0.006, 48 seconds." */
export function attemptCost(last: RetakeLast): string {
  const parts = [
    last.cost_usd ? fmtUsd(Number(last.cost_usd.toPrecision(1))) : '',
    last.seconds ? `${Math.max(1, Math.round(last.seconds))} seconds` : '',
  ].filter(Boolean);
  return parts.length ? `This attempt: ${parts.join(', ')}.` : '';
}

// ---------------------------------------------------------------- the attempts list

export interface AttemptRow { number: number; what: string; cases: string; score: string; result: string; current: boolean }

export function attemptRows(view: RetakeView): AttemptRow[] {
  return view.attempts.map((a) => {
    const what = a.number === 1 || a.cases === 'first_exam' ? 'The agent as built'
      : a.what ? `Changed ${a.what}` : 'A changed agent';
    const cases = a.cases === 'first_exam' ? `The exam, ${a.total ?? ''}`.replace(/, $/, '')
      : a.cases === 'unused' ? `${a.total ?? ''} no earlier attempt used`.trim()
        : 'Same exam as attempt 1';
    const result = a.result === 'passed' ? 'Passed'
      : a.result === 'did_not_pass' ? 'Did not pass'
        : a.result === 'not_placed' ? 'Passed, folder not written'
          : a.result === 'cancelled' ? 'Cancelled' : 'Not finished';
    return {
      number: a.number, what, cases,
      score: a.right != null && a.total != null ? `${a.right} of ${a.total}` : 'No score',
      result: a.undone ? `${result}, undone` : result,
      current: view.state === 'passed' && a.result === 'passed' && a.number === view.attempt,
    };
  });
}

export function compareNote(unit: string): string {
  return `Scores on different ${unit} cannot be compared with each other.`;
}

// ---------------------------------------------------------------- outside the page

/** The Installed list's row for an agent whose folder in the project refuses calls. */
export function stoppedRow(reason: string | undefined): string | undefined {
  if (!reason) return undefined;
  return reason === 'cannot_check' ? 'Stopped: cannot be checked' : 'Stopped: changed';
}

export const SAVING_STOPS = 'Saving this stops your agent until it passes its exam again.';

/** Whether an edit's path is one of an added agent's examined files (its `artifact/` folder). */
export function stopsAgent(path: string): boolean {
  return /(^|\/)[^/]+\/artifact\/(spec\.json|config\.json|harness\.py|report\.json)$/.test(path.split('\\').join('/'));
}
