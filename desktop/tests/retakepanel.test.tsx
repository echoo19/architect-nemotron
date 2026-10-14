// @vitest-environment jsdom
/**
 * The retake panel on an agent's page: every state the bridge reports, in the words of
 * the retake spec (v2, section 5), and the three things the page can do about a change.
 * Replies are tests/fixtures/retake.ts.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AgentsScreen } from '../src/components/agents/AgentsScreen';
import { RetakePanel } from '../src/components/agents/RetakePanel';
import { rightWords, ruleWords, stoppedRow, stopsAgent, type RetakeReply, type RetakeView } from '../src/lib/retake';
import { useApp } from '../src/store/app';
import {
  KEY_STEP, NO_RETAKES, RETAKE_CANNOT_CHECK, RETAKE_DID_NOT_PASS, RETAKE_NO_COPY, RETAKE_NO_RETAKES, RETAKE_NOT_PLACED, RETAKE_NOTE, RETAKE_PASSED,
  RETAKE_PASSED_AGAIN, RETAKE_PASSED_NO_RETAKES, RETAKE_PRACTISED, RETAKE_RETAKING, RETAKE_SAME_EXAM, RETAKE_STOPPED, RETAKE_UNDO_ONLY,
} from './fixtures/retake';
import { conv, installForgeHarness, replies } from './helpers/forge';

installForgeHarness();

const RECEIPT_OUT = { type: 'object', properties: { total: {}, subtotal: {}, tax: {}, item_count: {} } };
const STOP = 'Stopped: changed after its exam. 0 model calls.';
const UNDO = 'Puts back the version that passed, exactly as it was. Your app works again at once. No cost.';
const CAP = "It stops if it reaches $0.60, the build's cost cap.";

function panel(view: RetakeView, extra: Partial<Parameters<typeof RetakePanel>[0]> = {}) {
  const onView = vi.fn<(reply: RetakeReply) => void>();
  render(<RetakePanel convId="c1" name="receipt-reader" view={view} onView={onView} outputSchema={RECEIPT_OUT} {...extra} />);
  return { onView, card: document.querySelector('[data-retake]') as HTMLElement };
}

/** The panel's sentences, one per element, in order. */
const said = () => [...document.querySelectorAll('.retake h2, .retake p')].map((el) => el.textContent);
/** A sentence on the page, whole: its figures are in their own spans, so text queries would miss it. */
const has = (text: string) => [...document.querySelectorAll('h2, p')].some((el) => el.textContent === text);
const shows = (text: string) => waitFor(() => expect(has(text)).toBe(true));
const BELOW = 'Below is the agent as built, which Architect keeps. Try your agent runs that version.';
const facts = () => Object.fromEntries([...document.querySelectorAll('.retake-facts tr')].map((r) => [r.querySelector('th')!.textContent, r.querySelector('td')!.textContent]));
const buttons = () => [...document.querySelectorAll('.retake button')].map((b) => b.textContent);
const sent = () => (useApp.getState().request as unknown as ReturnType<typeof vi.fn>).mock.calls.map((c) => [c[1], c[2]]);

describe('the retake panel, state by state', () => {
  it('passed, as built: the score, what counts as right, where it lives and how to change it', () => {
    const { card } = panel(RETAKE_PASSED, { onTry: vi.fn(), onReport: vi.fn() });
    expect(card.dataset.retake).toBe('passed');
    expect(screen.getByRole('img', { name: 'Passed' })).toBeTruthy();
    expect(said()).toEqual([
      'Passed. It is your agent.',
      '59 of 100 receipts read correctly. Pass mark: 40 of 100.',
      'A receipt counts as right only when its total, subtotal, tax and item count are all right.',
      'Your app runs this agent from agents/receipt_reader. If a file in that folder changes, the agent stops until it passes its exam again. To change it: edit, Try on practice, then Retake exam.',
      RETAKE_NOTE,
    ]);
    // The rule is a plain paragraph, not a callout with a stripe down its side.
    expect(document.querySelector('.retake-rule')!.classList.contains('retake-line')).toBe(true);
    expect(facts()).toEqual({
      Model: 'Nemotron 3 Super',
      Exam: '100 receipts with known answers. The build did not see them. This was attempt 1.',
      'In your project': 'agents/receipt_reader',
    });
    expect(buttons()).toEqual(['Try your agent', 'Full report']);
    // One attempt is not a list, and the page below is the same version: no note about it.
    expect(document.querySelector('.retake-attempts')).toBeNull();
    expect(document.querySelector('.retake-below')).toBeNull();
  });

  it('passed, on an exam that takes no retakes: the page does not promise a retake it would refuse', () => {
    panel(RETAKE_PASSED_NO_RETAKES);
    expect(has('Your app runs this agent from agents/receipt_reader. If a file in that folder changes, the agent stops. Its exam takes no retakes, so a change to this agent can only be undone.')).toBe(true);
    expect(document.body.textContent).not.toMatch(/Try on practice|Retake exam/);
  });

  it('stopped: the one sentence, the changed file, and three things to do with their cost before any click', () => {
    const { card } = panel(RETAKE_STOPPED);
    expect(card.dataset.retake).toBe('stopped');
    expect(screen.getByRole('img', { name: 'Stopped' })).toBeTruthy();
    expect(said()).toEqual([
      STOP,
      'Changed: artifact/config.json (model: Nemotron 3 Super to Nemotron 3.5 Lightning).',
      "Your app's calls to this agent are refused before any model is called. Stopped means not examined yet. It does not mean worse.",
      `Attempt 2, on 100 receipts no earlier attempt used. It passes with 40 or more right, as in its first exam. About $0.006. ${CAP}`,
      'Scores the change on the 100 practice receipts, which no exam uses. Not an attempt. About $0.006.',
      UNDO,
    ]);
    expect(buttons()).toEqual(['Retake exam', 'Try on practice', 'Undo']);
    // The card under the panel is the build's version, and the page says so.
    expect(document.querySelector('.retake-below')!.textContent).toBe(BELOW);
    expect(document.querySelectorAll('.retake .ui-button-primary')).toHaveLength(1);
    expect(card.textContent).not.toMatch(/sealed|licensed|floor|held.out|package|export_|—/i);
  });

  it('stopped, after Try on practice: one practice score, and it says it is not an attempt', () => {
    panel(RETAKE_PRACTISED);
    expect(has('24 of 100 practice receipts right. Not an attempt.')).toBe(true);
    expect(buttons()).toEqual(['Retake exam', 'Try on practice', 'Undo']);
  });

  it('stopped on an exam with no unused cases: same exam, attempt 2, said before and after', () => {
    panel(RETAKE_SAME_EXAM, { outputSchema: undefined });
    expect(said()).toEqual([
      STOP,
      'Changed: artifact/spec.json (task text).',
      "Your app's calls to this agent are refused before any model is called. Stopped means not examined yet. It does not mean worse.",
      `Same exam, attempt 2. This exam has no unused cases, so the agent retakes the 20 cases of its first exam. It passes with 16 or more right, as in its first exam. About $0.01. ${CAP}`,
      'Scores the change on the 20 practice cases, which no exam uses. Not an attempt. About $0.01.',
      UNDO,
    ]);
    expect(document.body.textContent).not.toMatch(/fresh/i);
  });

  it('stopped, undo only: an edited harness.py offers Undo and says which files to edit instead', () => {
    panel(RETAKE_UNDO_ONLY);
    expect(said().slice(0, 5)).toEqual([
      STOP,
      'Changed: artifact/harness.py. A change to this file can only be undone.',
      "Architect writes harness.py from the agent's task, checks, model and settings. To change the agent, edit those in artifact/spec.json or artifact/config.json.",
      "Your app's calls to this agent are refused before any model is called. No exam attempt was used.",
      UNDO,
    ]);
    expect(buttons()).toEqual(['Undo']);
    expect(document.querySelector('.retake .ui-button-primary')!.textContent).toBe('Undo');
  });

  it('an exam that takes no retakes offers Undo only; no copy to undo to offers nothing and says to use git', () => {
    panel(RETAKE_NO_RETAKES);
    // Why, and the way forward, not only that it cannot.
    expect(said().slice(2, 4)).toEqual([NO_RETAKES, 'Undo puts back the version that passed.']);
    expect(buttons()).toEqual(['Undo']);
    document.body.innerHTML = '';
    panel(RETAKE_NO_COPY);
    expect(has('This computer has no copy of the version that passed. Restore agents/receipt_reader with git.')).toBe(true);
    expect(buttons()).toEqual([]);
  });

  it('a report that cannot be checked here does not say changed', () => {
    panel(RETAKE_CANNOT_CHECK);
    expect(said().slice(0, 4)).toEqual([
      'Stopped: its exam report cannot be checked on this computer. 0 model calls.',
      'No file changed. The key that signed its exam report is not set up here.',
      KEY_STEP, // the page says what to do, since it has no button to offer
      "Your app's calls to this agent are refused before any model is called.",
    ]);
    expect(buttons()).toEqual([]);
  });

  it('retaking: a count of answered receipts, never a score, what is spent and what cancelling costs', () => {
    const { card } = panel(RETAKE_RETAKING);
    expect(card.dataset.retake).toBe('retaking');
    expect(said()).toEqual([
      'Retaking the exam. Attempt 2.',
      '61 of 100 receipts answered. The score shows at the end.',
      'About $0.004 spent so far, at most $0.60.',
      // Which receipts this attempt is on. What attempt 3 would run on is said when it is offered.
      'These 100 receipts are ones no earlier attempt used.',
      'Cancelling still counts as attempt 2, and these 100 receipts count as used. Your agent stays stopped.',
    ]);
    expect(screen.getByRole('progressbar', { name: 'Answered' }).getAttribute('aria-valuenow')).toBe('61');
    expect(buttons()).toEqual(['Cancel retake']);
    expect(card.textContent).not.toMatch(/right|Pass mark/);
  });

  it('did not pass: the score against the pass mark, a cross and not the pause, Undo first', () => {
    const { card } = panel(RETAKE_DID_NOT_PASS);
    expect(card.dataset.retake).toBe('did_not_pass');
    expect(screen.getByRole('img', { name: 'Did not pass' })).toBeTruthy();
    expect(screen.queryByRole('img', { name: 'Stopped' })).toBeNull();
    expect(said()).toEqual([
      '29 of 100. Pass mark 40. Did not pass.',
      "Attempt 2, on 100 receipts no earlier attempt used. Your app's calls stay stopped.",
      UNDO,
      'Your agent stays stopped while you edit. Trying a change on practice receipts is not an attempt.',
      `This attempt: $0.006, 75 seconds. ${RETAKE_NOTE}`,
    ]);
    // The pass mark is in the heading: no second row repeats it.
    expect(facts()).toEqual({ Changed: 'artifact/config.json (model: Nemotron 3 Super to Nemotron 3.5 Lightning)' });
    expect(buttons()).toEqual(['Undo', 'Keep the change and edit again']);
    expect(document.querySelector('.retake .ui-button-primary')!.textContent).toBe('Undo');
    expect(card.textContent).not.toMatch(/worse/i);
  });

  it('Keep the change and edit again goes back to the stopped page, where attempt 3 is the same exam', () => {
    panel(RETAKE_DID_NOT_PASS);
    fireEvent.click(screen.getByRole('button', { name: 'Keep the change and edit again' }));
    expect(document.querySelector<HTMLElement>('[data-retake]')!.dataset.retake).toBe('stopped');
    expect(has('Attempt 2 did not pass: 29 of 100. Pass mark 40.')).toBe(true);
    expect(said().some((line) => line!.startsWith('Same exam, attempt 3. Fewer than 100 unused receipts are left, so the agent retakes the 100 receipts of its first exam.'))).toBe(true);
    expect(buttons()).toEqual(['Retake exam', 'Try on practice', 'Undo']);
  });

  it('passed again on the same exam: says why it was the same exam, and lists every attempt', () => {
    const { card } = panel(RETAKE_PASSED_AGAIN, { onTry: vi.fn(), onReport: vi.fn() });
    expect(card.dataset.retake).toBe('passed');
    expect(said()).toEqual([
      '73 of 100. Pass mark 40.Passed. It is your agent.',
      "Same exam, attempt 3. Your app's calls work again.",
      'Fewer than 100 unused receipts were left, so this retake used the 100 receipts of the first exam. This agent has now been scored on these receipts twice, so this score is easier to reach than a first score.',
      'Scores on different receipts cannot be compared with each other.',
      RETAKE_NOTE,
    ]);
    expect(facts()).toEqual({ Changed: 'checks', Model: 'Nemotron 3 Super', 'In your project': 'agents/receipt_reader' });
    expect([...card.querySelectorAll('.retake-attempts thead th')].map((th) => th.textContent)).toEqual(['No.', 'What was examined', 'Receipts', 'Score', 'Result']);
    expect([...card.querySelectorAll('.retake-attempts tbody tr')].map((tr) => [...tr.querySelectorAll('td')].map((td) => td.textContent))).toEqual([
      ['1', 'The agent as built', 'The exam, 100', '59 of 100', 'Passed'],
      ['2', 'Changed model: Nemotron 3 Super to Nemotron 3.5 Lightning', '100 no earlier attempt used', '29 of 100', 'Did not pass, undone'],
      ['3', 'Changed checks', 'Same exam as attempt 1', '73 of 100', 'Passed'],
    ]);
    expect(card.querySelectorAll('.retake-attempts tr.is-current')).toHaveLength(1);
    // Try your agent here would run the build's version, so the panel does not offer it.
    expect(buttons()).toEqual([]);
  });
});

describe('the retake panel, what it does', () => {
  beforeEach(() => {
    useApp.setState({ convs: { c1: conv() } });
  });

  it('Retake exam asks once more with the attempt number, then sends retake and shows the reply', async () => {
    replies.retake = RETAKE_RETAKING;
    const { onView } = panel(RETAKE_STOPPED);
    fireEvent.click(screen.getByRole('button', { name: 'Retake exam' }));
    expect(sent()).toEqual([]);
    expect(buttons()).toEqual(['Start attempt 2', 'Not now', 'Try on practice', 'Undo']);
    fireEvent.click(screen.getByRole('button', { name: 'Not now' }));
    expect(buttons()).toEqual(['Retake exam', 'Try on practice', 'Undo']);
    fireEvent.click(screen.getByRole('button', { name: 'Retake exam' }));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Start attempt 2' }));
    });
    expect(sent()).toEqual([['retake', { name: 'receipt-reader' }]]);
    expect(onView).toHaveBeenCalledWith(RETAKE_RETAKING);
  });

  it('Try on practice sends practice at once; Undo asks first, since it discards the edit', async () => {
    replies.practice = { ...RETAKE_STOPPED, practice: { running: true } };
    replies.undo = { ...RETAKE_PASSED, undone: ['The version that passed is back.'] };
    const { onView } = panel(RETAKE_STOPPED);
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Try on practice' }));
    });
    expect(sent()).toEqual([['practice', { name: 'receipt-reader' }]]);
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }));
    expect(sent()).toHaveLength(1);
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Put back the version that passed' }));
    });
    expect(sent()[1]).toEqual(['undo', { name: 'receipt-reader' }]);
    expect(onView).toHaveBeenLastCalledWith(replies.undo);
  });

  it('Cancel retake sends retake_cancel', async () => {
    replies.retake_cancel = RETAKE_STOPPED;
    panel(RETAKE_RETAKING);
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Cancel retake' }));
    });
    expect(sent()).toEqual([['retake_cancel', { name: 'receipt-reader' }]]);
  });

  it('a refusal is shown in its own plain words, and nothing else changes', async () => {
    useApp.setState({
      request: vi.fn(async () => {
        throw Object.assign(new Error('exam_no_retakes'), { code: 'exam_no_retakes', detail: 'This exam cannot be retaken here.' });
      }) as never,
    });
    const { onView } = panel(RETAKE_STOPPED);
    fireEvent.click(screen.getByRole('button', { name: 'Retake exam' }));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Start attempt 2' }));
    });
    expect(screen.getByRole('alert').textContent).toBe('This exam cannot be retaken here.');
    expect(onView).not.toHaveBeenCalled();
  });

  it('a run that could not finish says so under the choices', () => {
    const line = 'The retake could not finish. The provider CLI could not be started. Your agent stays stopped.';
    panel({ ...RETAKE_STOPPED, refused: [line] });
    expect(screen.getByRole('alert').textContent).toBe(line);
  });

  it('a failure with no lines of its own names the action and says why in words, never a code', async () => {
    useApp.setState({
      request: vi.fn(async () => {
        throw Object.assign(new Error('exam_changed'), { code: 'exam_changed' });
      }) as never,
    });
    panel(RETAKE_STOPPED);
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Try on practice' }));
    });
    expect(screen.getByRole('alert').textContent).toBe("The practice run did not go through: the exam's cases are not the ones this agent passed on.");
  });

  it('a pass whose folder could not be written offers to write it, with no new attempt', async () => {
    replies.retake = RETAKE_PASSED;
    panel(RETAKE_NOT_PLACED);
    expect(has('Attempt 2 passed, but its folder was not written.')).toBe(true);
    expect(has('Attempt 2 passed on this change, but its folder could not be written then. This writes it now. No new attempt and no cost.')).toBe(true);
    // It was examined and it passed, so the page does not call it unexamined or offer an exam.
    expect(document.body.textContent).not.toMatch(/not examined yet|Retake exam/);
    expect(buttons()).toEqual(['Write the folder', 'Try on practice', 'Undo']);
    fireEvent.click(screen.getByRole('button', { name: 'Write the folder' }));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Write the folder of attempt 2' }));
    });
    expect(sent()).toEqual([['retake', { name: 'receipt-reader' }]]);
  });
});

describe('the retake panel on Your agents', () => {
  const DETAIL = { name: 'receipt-reader', licensed: true, model: 'nvidia/nemotron-3-super-120b-a12b', spec: { task: 'Read a receipt', input_schema: { type: 'object', properties: { text: { type: 'string' } }, required: ['text'] }, output_schema: RECEIPT_OUT }, history: [] };

  beforeEach(() => {
    replies.agents = { agents: [{ name: 'receipt-reader', revision: 1, ok: true, licensed: true, floor_score: 0.59, floor_limit: 0.4 }] };
    replies.agent = DETAIL;
    useApp.setState({ convs: { c1: conv() }, screen: 'agents' });
  });

  it('sits at the top of the agent page, above the build\'s card', async () => {
    replies.retake_status = RETAKE_STOPPED;
    render(<AgentsScreen />);
    await shows(STOP);
    const page = document.querySelector('.agent-page')!;
    expect(page.firstElementChild!.querySelector('[data-retake="stopped"]')).toBeTruthy();
    expect(page.querySelectorAll('[data-card]')).toHaveLength(2);
    expect(sent()).toContainEqual(['retake_status', { name: 'receipt-reader' }]);
  });

  it('an agent never added to a project has no panel, and the page is as it was', async () => {
    replies.retake_status = { placed: false };
    render(<AgentsScreen />);
    await screen.findByText('Add to project');
    await waitFor(() => expect(sent()).toContainEqual(['retake_status', { name: 'receipt-reader' }]));
    expect(document.querySelector('[data-retake]')).toBeNull();
    expect(document.querySelectorAll('.agent-page [data-card]')).toHaveLength(1);
  });

  it('follows a running retake until the bridge says it ended', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      replies.retake_status = RETAKE_RETAKING;
      render(<AgentsScreen />);
      await shows('Retaking the exam. Attempt 2.');
      replies.retake_status = RETAKE_DID_NOT_PASS;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(900);
      });
      await shows('29 of 100. Pass mark 40. Did not pass.');
      const asked = sent().filter(([m]) => m === 'retake_status').length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(3000);
      });
      expect(sent().filter(([m]) => m === 'retake_status')).toHaveLength(asked);
    } finally {
      vi.useRealTimers();
    }
  });

  it('the Installed row says Stopped: changed in place of the score', async () => {
    replies.agents = { agents: [{ name: 'receipt-reader', revision: 1, ok: true, licensed: true, floor_score: 0.59, floor_limit: 0.4, stopped: 'changed' }] };
    replies.retake_status = RETAKE_STOPPED;
    render(<AgentsScreen />);
    await shows(STOP);
    const row = document.querySelector('.agents-list')!;
    expect(row.textContent).toContain('Stopped: changed');
    expect(row.textContent).not.toContain('59 / 100');
  });

  it('with a panel, the card under it is headed As built and gives no second verdict or score', async () => {
    replies.retake_status = RETAKE_STOPPED;
    render(<AgentsScreen />);
    await shows(STOP);
    const [, built] = [...document.querySelectorAll('.agent-page [data-card]')];
    expect(built.querySelector('h2')!.textContent).toBe('As built');
    // One verdict on the page: the red Stopped, and no green Passed with a big 59 / 100 under it.
    expect(built.querySelector('h2')!.parentElement!.textContent).not.toMatch(/Passed|59|v1/);
    expect(document.querySelector('.agent-page')!.querySelectorAll('[role="img"][aria-label="Passed"]')).toHaveLength(0);
    expect(has(BELOW)).toBe(true);
  });

  it('the Installed row shows the score of the version in the project once a retake has passed', async () => {
    replies.agents = { agents: [{ name: 'receipt-reader', revision: 1, ok: true, licensed: true, floor_score: 0.59, floor_limit: 0.4, placed: { right: 73, total: 100, pass_mark: 40, attempt: 3 } }] };
    replies.retake_status = RETAKE_PASSED_AGAIN;
    render(<AgentsScreen />);
    await shows("Same exam, attempt 3. Your app's calls work again.");
    const row = document.querySelector('.agents-list')!;
    expect(row.textContent).toContain('73 / 100, pass mark 40');
    expect(row.textContent).not.toContain('59 / 100');
  });
});

describe('the retake words outside the page', () => {
  it('names the Installed row, and the estimate with its cap', () => {
    expect(stoppedRow('changed')).toBe('Stopped: changed');
    expect(stoppedRow('changed_undo_only')).toBe('Stopped: changed');
    expect(stoppedRow('cannot_check')).toBe('Stopped: cannot be checked');
    expect(stoppedRow(undefined)).toBeUndefined();
    // An exam that takes no retakes is not promised one, before anything is changed.
    expect(ruleWords('agents/receipt_reader', false)).toBe('Your app runs this agent from agents/receipt_reader. If a file in that folder changes, the agent stops. Its exam takes no retakes, so a change to this agent can only be undone.');
    expect(ruleWords('agents/receipt_reader', null)).toBe('Your app runs this agent from agents/receipt_reader. If a file in that folder changes, the agent stops until it passes its exam again.');
    expect(rightWords('cases', { type: 'string' })).toBe('A case counts as right only when its whole answer matches the answer key.');
  });

  it("knows an edit to an added agent's examined files from any other edit", () => {
    expect(stopsAgent('agents/receipt_reader/artifact/config.json')).toBe(true);
    expect(stopsAgent('C:\\rec\\expense-app\\agents\\receipt_reader\\artifact\\spec.json')).toBe(true);
    expect(stopsAgent('app/extract.py')).toBe(false);
    expect(stopsAgent('artifact/notes.md')).toBe(false);
  });
});
