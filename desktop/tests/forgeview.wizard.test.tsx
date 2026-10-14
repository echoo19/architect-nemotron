// @vitest-environment jsdom
/**
 * Steps 3 and 4 of the guided build (clarity spec §3, S3), drawn from the demo build: the
 * Build list while the models run (no exam score anywhere before the result, C1), the
 * Results table with its pass mark, costs and dots, a build where no model passed, an
 * unscored exam, and a build that stopped. Also kept from the agent card's tests: a Try it
 * run's outcome, the replay's times and the exam grid. Shared set-up in tests/helpers/forge.tsx.
 */
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { tryRun } from '../src/components/forge/try';
import { ExamChart } from '../src/components/forge/ExamChart';
import { BuildScreen } from '../src/components/forge/wizard/BuildScreen';
import { ResultsScreen } from '../src/components/forge/wizard/ResultsScreen';
import { forgeModel, ladderView } from '../src/lib/forge';
import { workLabel, type Timeline } from '../src/lib/timeline';
import { wizardStep } from '../src/lib/wizard';
import { rebuild, useApp, type Conv } from '../src/store/app';
import { DEMO_BUILDING, DEMO_BUILDING_NOW, DEMO_NO_PASS, DEMO_PASSED, demoConv } from './fixtures/demoBuild';
import { ERR_BUILD, ERR_NOTICE, FORGING, HARNESS_1, HARNESS_2, PLAN, SHIPPED, STOPPED_BUILD } from './fixtures/ladderBuild';
import { conv, installForgeHarness, replayed } from './helpers/forge';

installForgeHarness();

afterEach(() => vi.restoreAllMocks());

/** The step's screen for a conversation, as the wizard picks it. */
function page(c: Conv) {
  const model = forgeModel(c.timeline);
  const ladder = ladderView(model, c.forge);
  const ws = wizardStep(model, c);
  const Screen = ws.step === 3 ? BuildScreen : ResultsScreen;
  const view = render(<Screen conv={c} model={model} ladder={ladder} detail={null} sub={ws.sub} />);
  return { ...view, ws };
}

const text = () => document.body.textContent ?? '';
const heading = () => screen.getByRole('heading', { level: 1 }).textContent;
const rowOf = (name: string) => [...document.querySelectorAll<HTMLElement>('.build-row, .exam-row')].find((r) => r.querySelector('b, .exam-model-name')?.textContent?.startsWith(name))!;

/** Words the screens never say (redesign v2 §1), and no em dash. */
function expectPlainWords() {
  const t = text();
  expect(t).not.toMatch(/\bbar\b/i);
  for (const word of ['licensed', 'refused', 'preset', 'Forge', 'harness', 'rung', 'ladder', 'held-out', 'tuning']) {
    expect(t, word).not.toMatch(new RegExp(`\\b${word}\\b`, 'i'));
  }
  expect(t).not.toContain('—');
}

describe('step 3, Build', () => {
  it('lists the three models with their state and time, and no exam score before the result', () => {
    vi.spyOn(Date, 'now').mockReturnValue(DEMO_BUILDING_NOW);
    const { ws } = page(demoConv(DEMO_BUILDING));
    expect(ws).toEqual({ step: 3, sub: 'building' });
    expect(heading()).toBe('Building your agent');
    expect(text()).toContain('Each model practises on 100 receipts, then takes the exam on 100 it has never seen. They run one at a time, cheapest first, and all three take the same exam even after one passes');
    const rows = document.querySelectorAll('.build-row');
    expect(rows).toHaveLength(3);
    expect(screen.getByRole('heading', { name: 'Three models, cheapest first' })).toBeTruthy();
    expect(text()).toContain('Pass mark: 40 of 100. Exam scores arrive together when all three are done.');

    const lightning = rowOf('Lightning');
    expect(lightning.className).toContain('is-done');
    expect(lightning.textContent).toContain('Nemotron 3.5 Lightning · cheapest');
    expect(lightning.querySelector('.build-word')!.textContent).toBe('Done');
    expect(lightning.querySelector('.build-detail')!.textContent).toBe('12m 28s');

    const sup = rowOf('Super');
    expect(sup.className).toContain('is-running');
    expect(sup.textContent).toContain('Taking the exam');
    expect(sup.querySelector('.build-word')!.textContent).toBe('Running');
    expect(sup.querySelector('.build-detail')!.textContent).toBe('2m 34s so far');

    const ultra = rowOf('Ultra');
    expect(ultra.textContent).toContain('most expensive');
    expect(ultra.querySelector('.build-word')!.textContent).toBe('Waiting');
    expect(ultra.querySelector('.build-detail')!.textContent).toBe('starts after Super');

    // The clock counts from the Build press; the time left is the plan's cap, rounded up.
    expect(text()).toContain('15m 2s elapsed');
    expect(text()).toContain('45 min left at most: it stops on its own at 60 min or $0.60');

    // C1: Lightning's exam is over, but its score reaches the app only with the result.
    expect(text()).not.toContain('39');
    expect(text()).not.toContain('Did not pass');
    expect(text()).not.toMatch(/\bPassed\b/);
    expectPlainWords();
  });

  it('offers Stop build, never as the ask box, and keeps See results off until there is a result', async () => {
    vi.spyOn(Date, 'now').mockReturnValue(DEMO_BUILDING_NOW);
    const cancel = vi.fn(async () => undefined);
    useApp.setState({ cancel: cancel as never });
    page(demoConv(DEMO_BUILDING));
    const results = screen.getByRole('button', { name: 'See results' }) as HTMLButtonElement;
    expect(results.disabled).toBe(true);
    expect(text()).toContain('Results open when all three are done.');
    expect((screen.getByRole('textbox', { name: 'Ask Architect about the build' }) as HTMLInputElement).placeholder).toBe('Ask Architect about the build');
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Stop build' }));
    });
    expect(cancel).toHaveBeenCalledWith('c1');
  });

  it('shows a stopped build with its rows frozen, Change the plan and Build again', async () => {
    const send = vi.fn(async () => undefined);
    useApp.setState({ send: send as never });
    const { ws } = page(conv(replayed(STOPPED_BUILD)));
    expect(ws).toEqual({ step: 3, sub: 'stopped_build' });
    expect(heading()).toBe('The build stopped');
    expect(text()).toContain('You stopped the build.');
    // The model it was on reads Stopped, not Done or Running.
    expect(rowOf('Super').querySelector('.build-word')!.textContent).toBe('Stopped');
    expect(rowOf('Lightning').querySelector('.build-word')!.textContent).toBe('Done');
    expect(document.querySelector('.build-row.is-running')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Stop build' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Change the plan' }));
    expect(useApp.getState()).toMatchObject({ forgeSheet: 'plan', forgeSheetSection: 'next' });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Build again' }));
    });
    expect(send).toHaveBeenCalledWith('c1', '/make @receipts-v1.json');
    expectPlainWords();
  });

  it('shows a build the relay turned away as stopped, its sentence as the sub, with Retry', async () => {
    const send = vi.fn(async () => undefined);
    useApp.setState({ send: send as never });
    const { ws } = page(conv(replayed(ERR_BUILD)));
    expect(ws).toEqual({ step: 3, sub: 'stopped_build' });
    expect(heading()).toBe('The build stopped');
    expect(document.querySelector('.wizard-sub')!.textContent).toBe(`${ERR_NOTICE.split('. ')[0]}.`);
    expect(screen.queryByRole('button', { name: 'Build again' })).toBeNull();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    });
    expect(send).toHaveBeenCalledWith('c1', '/make @receipts-v1.json');
  });
});

describe('step 4, Results', () => {
  it('names the model that passed and says why it, with the build\'s time and cost', () => {
    const { ws } = page(demoConv(DEMO_PASSED));
    expect(ws).toEqual({ step: 4, sub: 'passed' });
    expect(heading()).toBe('Super passed. It is your agent.');
    const status = document.querySelector('.wizard-status')!;
    expect(status.querySelector('[data-status-mark="ok"]')!.textContent).toBe('Passed');
    expect(status.textContent).toContain('Built in 18m 54s');
    expect(status.textContent).toContain('One-time build cost $0.31');
    expect(document.querySelector('.wizard-sub')!.textContent).toBe(
      'Super got 64 of 100 exam receipts exactly right; each of the other 36 had at least one field off. The largest model, Ultra, got 72: 8 more, at more than 3 times the price. Super is the cheapest model that reached the pass mark of 40, set in receipt-reader.json, so it is your agent.',
    );
    expectPlainWords();
  });

  it('draws how the three models did: scores, the pass mark, results and costs', () => {
    page(demoConv(DEMO_PASSED));
    expect(screen.getByRole('heading', { name: 'How the three models did' })).toBeTruthy();
    expect(text()).toContain('The same 100-receipt exam for each. A receipt counts only when the whole answer matches the answer key.');
    expect([...document.querySelectorAll('.exam-right')].map((c) => c.textContent)).toEqual(['39', '64', '72']);
    expect(rowOf('Lightning').textContent).toContain('Did not pass1 short');
    expect(rowOf('Super').querySelector('.exam-result')!.textContent).toBe('Passed');
    expect(rowOf('Ultra').querySelector('.exam-result')!.textContent).toBe('Passed');
    // The ship is marked, and only it.
    expect(rowOf('Super').className).toContain('is-ships');
    expect(within(rowOf('Super')).getByText('Your agent')).toBeTruthy();
    expect(document.querySelectorAll('.exam-tag')).toHaveLength(1);
    // Cost per 1,000 receipts, from each model's mean cost per case.
    expect(screen.getByRole('columnheader', { name: 'Running cost per 1,000 receipts' })).toBeTruthy();
    const costs = [...document.querySelectorAll('.exam-cost')].map((c) => c.textContent);
    expect(costs[1]).toBe('$0.28');
    expect(costs[2]).toBe('$0.93');
    // The fill is pine when it passed, grey when not; the rule sits at 40 of 100.
    expect(rowOf('Lightning').querySelector('.exam-bar-fill')!.className).toContain('is-fail');
    expect((rowOf('Ultra').querySelector('.exam-bar-fill') as HTMLElement).style.width).toBe('72%');
    expect((rowOf('Ultra').querySelector('.exam-bar-mark') as HTMLElement).style.left).toBe('40%');
    const axis = document.querySelector('.exam-axis')!;
    expect([...axis.querySelectorAll('span:not(.exam-axis-mark)')].map((s) => s.textContent)).toEqual(['0', '20', '40', '60', '80', '100']);
    expect(axis.querySelector('.exam-axis-mark')!.textContent).toBe('Pass mark 40');
    expect(document.querySelector('.exam-legend')!.textContent).toBe('PassedDid not passPass mark');
    expect(document.querySelector('.exam-table-footer')!.textContent).toBe(
      'Why not Ultra? It passed too, but Super passed for less. If you need more than 64 right, raise the pass mark in receipt-reader.json to between 65 and 72 and build again: then Super no longer passes and Ultra becomes the cheapest that does.',
    );
  });

  it('folds Super\'s exam away until Show, then draws one dot per receipt with the pass line', () => {
    page(demoConv(DEMO_PASSED));
    const card = document.querySelector('.exam-dots-card') as HTMLElement;
    expect(within(card).getByRole('heading').textContent).toBe("Super's exam score as 100 dots");
    expect(card.querySelector('.exam-dots-key')!.textContent).toBe('64 right36 wrong');
    expect(card.textContent).not.toContain('wrong format');
    const toggle = within(card).getByRole('button', { name: 'Show' });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(card.querySelector('.viz-exam')).toBeNull();
    fireEvent.click(toggle);
    expect(within(card).getByRole('button', { name: 'Hide' }).getAttribute('aria-expanded')).toBe('true');
    expect(card.querySelectorAll('.viz-units.is-correct .viz-unit')).toHaveLength(64);
    expect(card.querySelectorAll('.viz-units.is-missed .viz-unit')).toHaveLength(36);
    expect(card.querySelector('.viz-pass-rule')).toBeTruthy();
    expect(card.querySelector('.viz-pass-label')!.textContent).toBe('Pass mark 40');
    // The header holds the key; the chart draws none of its own.
    expect(card.querySelector('.viz-exam-key')).toBeNull();
    expect(card.textContent).toContain('64 right');
    expect(card.textContent).toContain('36 wrong');
    expect(card.textContent).toContain('36 had at least one field off. Every answer came back in the right format. The dashed line is the pass mark: Super needed 40 and got 64.');
    // Why the dots are a count: only the count is kept, said first, so no dot promises a receipt.
    expect(card.querySelector('.exam-dots-lead')!.textContent).toBe("Architect keeps only the count, so these dots can't show which receipts Super got wrong.");
    expect(card.textContent).not.toContain('wrong format');
    // The bar's Full report is the one way to the report.
    expect(within(card).queryByRole('button', { name: /report/i })).toBeNull();
    expectPlainWords();
  });

  it('moves on to Try it, opens the report and the plan, and has no Back', () => {
    page(demoConv(DEMO_PASSED));
    expect(screen.queryByRole('button', { name: 'Back' })).toBeNull();
    expect(text()).toContain('Built from this plan');
    expect(document.querySelector('.plan-summary')!.textContent).toContain('3 models100 practice + 100 exam receiptspass mark 40up to $0.60 and 60 min');
    fireEvent.click(screen.getByRole('button', { name: 'View plan' }));
    expect(useApp.getState().forgeSheet).toBe('plan');
    fireEvent.click(screen.getByRole('button', { name: 'Full report' }));
    expect(useApp.getState().forgeSheet).toBe('report');
    fireEvent.click(screen.getByRole('button', { name: 'Try your agent' }));
    expect(useApp.getState().wizardView.c1).toBe('try');
    expect(screen.getByRole('textbox', { name: 'Ask Architect about the results' })).toBeTruthy();
  });

  it('says no model reached the pass mark, how short each fell and what to change', () => {
    const { ws } = page(demoConv(DEMO_NO_PASS));
    expect(ws).toEqual({ step: 4, sub: 'no_pass' });
    expect(heading()).toBe('No model reached the pass mark of 95.');
    expect(document.querySelector('.wizard-status [data-status-mark="err"]')!.textContent).toBe('Did not pass');
    expect(document.querySelector('.wizard-sub')!.textContent).toBe(
      'The best was Ultra, with 72 of 100 right. No agent was made and nothing was added to your project. Your description and test receipts are kept.',
    );
    expect([...document.querySelectorAll('.exam-short')].map((s) => s.textContent)).toEqual(['56 short', '31 short', '23 short']);
    expect(document.querySelectorAll('.exam-bar-fill.is-pass')).toHaveLength(0);
    expect(document.querySelector('.exam-tag')).toBeNull();
    // The plan sits under the table, as on a build that passed, its pass mark marked.
    const summary = document.querySelector('.plan-summary')!;
    expect(summary.compareDocumentPosition(document.querySelector('.exam-table-card')!) & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy();
    expect(summary.querySelector('.plan-summary-mark.is-flagged')!.textContent).toBe('pass mark 95');
    expect(document.querySelector('.exam-table-footer')!.textContent).toBe(
      'What you can change. If 72 of 100 is good enough for this job, lower the pass mark to 72 or below and Ultra would pass. If 64 is enough, Super would pass and cost less to run. A new pass mark means building again: every model takes the exam again, up to $0.60 and 60 min.',
    );
    // No dots, no Try it.
    expect(document.querySelector('.exam-dots-card')).toBeNull();
    expect(screen.queryByRole('button', { name: /Try your agent/ })).toBeNull();
    expect(text()).toContain('Try it and Add to project open once a model passes.');
    const primary = screen.getByRole('button', { name: 'Change the plan and retry' });
    fireEvent.click(primary);
    expect(useApp.getState()).toMatchObject({ forgeSheet: 'plan', forgeSheetSection: 'next' });
    expectPlainWords();
  });

  it('says when the exam could not be scored, and why', () => {
    const unscored = DEMO_NO_PASS.map((msg) => {
      const event = msg.event as Record<string, unknown> | undefined;
      if (event?.kind !== 'plan_card' || !String(event.title).startsWith('Result')) return msg;
      const data = event.data as Record<string, unknown>;
      const counts = { cases: 100, correct: null, invalid: 40, over_budget: 0 };
      const rungs = (data.rungs as Array<Record<string, unknown>>).map((r) => ({ ...r, score: 0, counts, codes: ['primary_invalid_exceeded'] }));
      const rows = (event.rows as Array<[string, string]>).map(([k, v]): [string, string] => [k, k === 'floor' ? 'not scored (primary_invalid_exceeded)' : v]);
      return { ...msg, event: { ...event, rows, data: { ...data, score: 0, exam: counts, rungs } } };
    });
    const { ws } = page(demoConv(unscored));
    expect(ws).toEqual({ step: 4, sub: 'unscored' });
    expect(heading()).toBe('The exam could not be scored.');
    expect(document.querySelector('.wizard-sub')!.textContent).toBe('Not scored: too many invalid outputs.');
    expect([...document.querySelectorAll('.exam-right')].map((c) => c.textContent)).toEqual(['Not scored', 'Not scored', 'Not scored']);
    expect(screen.getByRole('button', { name: 'Change the plan and retry' })).toBeTruthy();
  });
});

describe('a Try it run', () => {
  const request = (outcome: unknown) => vi.fn(async () => {
    if (outcome instanceof Error) throw outcome;
    return outcome;
  }) as never;

  it('says why it failed: the bridge\'s sentence first, else the app\'s words for the code', async () => {
    const unreachable = 'Architect could not reach architect-relay.vercel.app; check the connection and try again.';
    expect(await tryRun(request(Object.assign(new Error('x'), { code: 'provider_unreachable', detail: unreachable })), 'c1', 'a', {}))
      .toEqual({ ok: false, value: unreachable });
    expect(await tryRun(request(Object.assign(new Error('offline'), { code: 'offline' })), 'c1', 'a', {}))
      .toEqual({ ok: false, value: 'Lost the connection to Architect. Reconnecting.' });
    expect(await tryRun(request(new Error('no code')), 'c1', 'a', {})).toEqual({ ok: false, value: 'That did not work.' });
  });

  it('keeps the output and what the run took', async () => {
    const run = await tryRun(request({ output: 'ok', cost_usd: 0.001, seconds: 1.5, calls: 1, completion_id: 'id-1' }), 'c1', 'a', {});
    expect(run).toEqual({ ok: true, value: 'ok', cost: 0.001, seconds: 1.5, calls: 1, completion: 'id-1' });
  });
});

describe('a page that re-attaches to a build', () => {
  it('folds the replay at the times its turns carry, the Forge board included', () => {
    const at = (msg: Record<string, unknown>, seconds: number) => ({ ...msg, at: seconds });
    const build = [...PLAN, ...FORGING, ...SHIPPED];
    const replay = [at(build[0], 1000), ...build.slice(1, -1), at(build.at(-1)!, 1151), { type: 'turn_started', text: '/run receipt-reader', quiet: true, at: 1160 }];
    const { timeline, forge } = rebuild(replay);
    // The build's work after the plan's question: from the turn's start to its end.
    const work = [...timeline.items].reverse().find((i) => i.kind === 'work') as Extract<Timeline['items'][number], { kind: 'work' }>;
    expect(workLabel(work)).toBe('Built the agent in 2m 31s');
    // Try it's quiet turn: running, with no message of its own.
    expect(timeline.running).toBe(true);
    expect(timeline.items.filter((i) => i.kind === 'user')).toHaveLength(1);
    expect(forge.drafts?.map((d) => d.text)).toEqual([HARNESS_1, HARNESS_2]);
  });
});

describe('the exam grid', () => {
  it('draws from the counts alone, with only its key beside it', () => {
    // Anything else on the object (a case, an id) is never read, let alone drawn.
    const report = { counts: { cases: 20, correct: 17, invalid: 0, overBudget: 0 }, bar: 0.8, licensed: true, cases: [{ input: 'HELDOUT_FINGERPRINT_1' }], verification_id: 'v-123' };
    const { container } = render(<ExamChart report={report as never} />);
    expect(container.textContent).not.toContain('HELDOUT');
    expect(container.innerHTML).not.toContain('v-123');
    expect(container.querySelectorAll('svg circle.viz-unit').length).toBe(20 + 2); // the units and the key's two glyphs
    expect([...container.querySelectorAll('.viz-exam-key li')].map((li) => li.textContent)).toEqual(['17correct', '3missed']);
    expect(container.querySelector('.viz-pass-rule')).toBeNull();
  });

  it('draws the pass mark as a stepped line when asked, after the cells that must be solid', () => {
    const report = { counts: { cases: 20, correct: 17, invalid: 0, overBudget: 0 }, bar: 0.75, licensed: true };
    const { container } = render(<ExamChart report={report} passLine />);
    // 15 of 20: one full row, then five cells of the second: the line steps up after the fifth.
    expect(container.querySelector('.viz-pass-rule')!.getAttribute('d')).toMatch(/^M-7 \d+(\.\d+)?H\d+(\.\d+)?V\d+(\.\d+)?H\d+/);
    expect(container.querySelector('.viz-pass-label')!.textContent).toBe('Pass mark 15');
    expect(container.querySelector('.viz-exam-key')).toBeNull();
  });
});
