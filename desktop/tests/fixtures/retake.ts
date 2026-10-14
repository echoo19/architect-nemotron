/**
 * What the bridge's `retake_status` says about an agent in its project, one reply per
 * state the retake panel draws. The shapes are the bridge's (`architect/desktop/data.py`,
 * checked key for key by tests/test_desktop_retake.py); the lines are the ones Python
 * writes (`architect/session/retake.py`).
 *
 * The receipt reader's numbers on the demo exam of development receipts are the ones the
 * live trial of 2026-10-12 measured (docs/bench/retake-trial.md): 59 of 100 at the build
 * with pass mark 40, and, for the model swap made by hand after the ten requests, 24 of
 * 100 on practice and 29 of 100 on attempt 2 at $0.006 in 75 seconds. The reply that
 * passed again on the same exam (73 of 100) is an example no live run made. The ticket
 * sorter is a made-up agent on an exam the app drafted, which has no unused cases.
 */
import type { RetakeAttempt, RetakePlan, RetakeView } from '../../src/lib/retake';

export const RETAKE_NOTE = 'These are development receipts. They were used while Architect was being built, so scores on them are not its published results.';
const SUPER = 'nvidia/nemotron-3-super-120b-a12b';
const STOPPED = 'Stopped: changed after its exam. 0 model calls.';
const MODEL_CHANGE = 'model: Nemotron 3 Super to Nemotron 3.5 Lightning';

const BUILT: RetakeAttempt = { number: 1, cases: 'first_exam', result: 'passed', right: 59, total: 100, what: null, undone: false };

const CAP = "It stops if it reaches $0.60, the build's cost cap.";
const PLAN: RetakePlan = {
  attempt: 2, cases: 'unused', total: 100, pass_mark: 40, left: 150, unit: 'receipts', note: RETAKE_NOTE,
  estimate_usd: 0.006, cap_usd: 0.6, practice_total: 100, practice_estimate_usd: 0.006,
  head: 'Attempt 2, on 100 receipts no earlier attempt used.', cost: `About $0.006. ${CAP}`, practice_cost: 'About $0.006.',
  place_only: false,
};

/** The agent as built, added to the project and unchanged. */
export const RETAKE_PASSED: RetakeView = {
  placed: true, folder: 'agents/receipt_reader', state: 'passed', package: 'receipt_reader', reason: null, changed_files: [],
  lines: ['Passed. It is your agent.', 'Nemotron 3 Super. 59 of 100. Pass mark 40. Passed. Attempt 1.'],
  can_retake: false, can_undo: false, refusal: null, attempt: 1, next_attempt: 2, model: SUPER, right: 59, total: 100, pass_mark: 40,
  unit: 'receipts', note: RETAKE_NOTE, cases: 'first_exam', retakes: true, step: '', attempts: [BUILT],
  plan: null, progress: null, last: null, practice: null, refused: null,
};

/** The same agent on an exam that takes no retakes (the published plan's evidence exam). */
export const RETAKE_PASSED_NO_RETAKES: RetakeView = { ...RETAKE_PASSED, retakes: false, note: null };

/** The model was swapped in artifact/config.json: stopped, and a retake has unused receipts. */
export const RETAKE_STOPPED: RetakeView = {
  ...RETAKE_PASSED, state: 'stopped', reason: 'changed', changed_files: ['artifact/config.json'],
  lines: [STOPPED, `Changed: artifact/config.json (${MODEL_CHANGE}).`],
  can_retake: true, can_undo: true, plan: PLAN, step: 'Retake its exam or undo the change in Architect.',
};

/** The same change after Try on practice. */
export const RETAKE_PRACTISED: RetakeView = {
  ...RETAKE_STOPPED, practice: { running: false, lines: ['24 of 100 practice receipts right. Not an attempt.', RETAKE_NOTE], right: 24, total: 100 },
};

/** Attempt 2 under way: 61 of its 100 receipts answered. */
export const RETAKE_RETAKING: RetakeView = {
  ...RETAKE_STOPPED, state: 'retaking', plan: null,
  attempts: [BUILT, { number: 2, cases: 'unused', result: 'started', right: null, total: 100, what: MODEL_CHANGE, undone: false }],
  progress: { attempt: 2, cases: 'unused', done: 61, total: 100, spent_usd: 0.0037, cap_usd: 0.6, left_after: 50, unit: 'receipts', pass_mark: 40 },
};

const FAILED: RetakeAttempt = { number: 2, cases: 'unused', result: 'did_not_pass', right: 29, total: 100, what: MODEL_CHANGE, undone: false };

/** Attempt 2 ended at 29 of 100. Only 50 unused receipts are left, so attempt 3 would be the same exam. */
export const RETAKE_DID_NOT_PASS: RetakeView = {
  ...RETAKE_STOPPED, state: 'did_not_pass', next_attempt: 3, attempts: [BUILT, FAILED],
  plan: {
    ...PLAN, attempt: 3, cases: 'same_exam', left: 50,
    head: 'Same exam, attempt 3. Fewer than 100 unused receipts are left, so the agent retakes the 100 receipts of its first exam.',
  },
  last: { ...FAILED, pass_mark: 40, cost_usd: 0.0061, seconds: 75.2 },
};

/** Attempt 2 passed, but a program had the agent's folder open: the next press writes it, with no new attempt. */
export const RETAKE_NOT_PLACED: RetakeView = {
  ...RETAKE_STOPPED, next_attempt: 3, changed_files: ['artifact/spec.json'], lines: [STOPPED, 'Changed: artifact/spec.json (task text).'],
  attempts: [BUILT, { ...FAILED, result: 'not_placed', right: 48, what: 'task text' }],
  plan: {
    ...PLAN, head: 'Attempt 2 passed on this change, but its folder could not be written then. This writes it now.',
    cost: 'No new attempt and no cost.', place_only: true,
  },
  last: { ...FAILED, result: 'not_placed', right: 48, what: 'task text', pass_mark: 40 },
};

/** After Undo, a second change (two checks) passed attempt 3 on the first exam's receipts. */
export const RETAKE_PASSED_AGAIN: RetakeView = {
  ...RETAKE_PASSED, attempt: 3, next_attempt: 4, right: 73, cases: 'same_exam',
  lines: ['Passed. It is your agent.', 'Nemotron 3 Super. 73 of 100. Pass mark 40. Passed. Attempt 3.'],
  attempts: [
    BUILT,
    { ...FAILED, undone: true },
    { number: 3, cases: 'same_exam', result: 'passed', right: 73, total: 100, what: 'checks', undone: false },
  ],
};

/** harness.py was edited: a retake never runs edited Python, so only Undo is offered. */
export const RETAKE_UNDO_ONLY: RetakeView = {
  ...RETAKE_PASSED, state: 'stopped', reason: 'changed_undo_only', changed_files: ['artifact/harness.py'],
  lines: [
    STOPPED,
    'Changed: artifact/harness.py. A change to this file can only be undone.',
    "Architect writes harness.py from the agent's task, checks, model and settings. To change the agent, edit those in artifact/spec.json or artifact/config.json.",
  ],
  can_undo: true, refusal: 'undo_only', step: 'Undo the change in Architect.',
};

/** A different agent, on an exam the app drafted: no unused cases, so its retake is the same exam. */
export const RETAKE_SAME_EXAM: RetakeView = {
  ...RETAKE_PASSED, folder: 'agents/ticket_sorter', package: 'ticket_sorter', state: 'stopped', reason: 'changed',
  changed_files: ['artifact/spec.json'], lines: [STOPPED, 'Changed: artifact/spec.json (task text).'],
  can_retake: true, can_undo: true, model: 'nvidia/Nemotron-3_5-Lightning', right: 18, total: 20, pass_mark: 16, unit: 'cases', note: null,
  attempts: [{ number: 1, cases: 'first_exam', result: 'passed', right: 18, total: 20, what: null, undone: false }],
  plan: {
    attempt: 2, cases: 'same_exam', total: 20, pass_mark: 16, left: 0, unit: 'cases', note: null,
    estimate_usd: 0.011, cap_usd: 0.6, practice_total: 20, practice_estimate_usd: 0.011,
    head: 'Same exam, attempt 2. This exam has no unused cases, so the agent retakes the 20 cases of its first exam.',
    cost: `About $0.01. ${CAP}`, practice_cost: 'About $0.01.', place_only: false,
  },
  step: 'Retake its exam or undo the change in Architect.',
};

/** An exam on a published dataset: it takes no retakes, so the change can only be undone. */
export const NO_RETAKES = "This exam is on a published set of cases, and Architect doesn't retake a published exam, so a changed agent cannot take it again.";
export const RETAKE_NO_RETAKES: RetakeView = {
  ...RETAKE_STOPPED, lines: [...RETAKE_STOPPED.lines, NO_RETAKES, 'Undo puts back the version that passed.'],
  can_retake: false, refusal: 'exam_no_retakes', plan: null, retakes: false, note: null, step: 'Undo the change in Architect.',
};

/** Changed, and this computer keeps no copy of the version that passed. */
export const RETAKE_NO_COPY: RetakeView = {
  ...RETAKE_STOPPED,
  lines: [...RETAKE_STOPPED.lines, 'This computer has no copy of the version that passed. Restore agents/receipt_reader with git.'],
  can_retake: false, can_undo: false, refusal: 'no_copy', plan: null, step: '',
};

export const KEY_STEP = 'Set ARCHITECT_SIGNING_KEY_FILE to the key file Architect signed it with, or build the agent again on this computer.';

/** Nothing changed, but no key here can check the exam report. */
export const RETAKE_CANNOT_CHECK: RetakeView = {
  ...RETAKE_PASSED, state: 'stopped', reason: 'cannot_check',
  lines: [
    'Stopped: its exam report cannot be checked on this computer. 0 model calls.',
    'No file changed. The key that signed its exam report is not set up here.',
    KEY_STEP,
  ],
  refusal: 'cannot_check', step: KEY_STEP,
};
