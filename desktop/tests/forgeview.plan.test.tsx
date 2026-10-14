// @vitest-environment jsdom
/**
 * The Plan and Report sheets, the harness beside a build, and Forge's harness versions in the dock.
 * Shared set-up in tests/helpers/forge.tsx.
 */
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ForgeWorkspace } from '../src/components/forge/ForgeWorkspace';
import { HarnessPane } from '../src/components/panes/HarnessPane';
import { Shortcuts } from '../src/components/shell/Shortcuts';
import { isMac } from '../src/lib/shortcuts';
import { showSidebar, sidebarYields, useApp, type Conv } from '../src/store/app';
import { FORGING, PACK, PLAN, SHIPPED, SPEC } from './fixtures/ladderBuild';
import { DEMO_CWD, DEMO_LADDER, DEMO_NO_PASS, DEMO_PLAN, demoConv, type Msg } from './fixtures/demoBuild';
import { PlanSheet } from '../src/components/forge/PlanSheet';
import { DescribeScreen, PlanScreen } from '../src/components/forge/wizard/screens';
import { forgeModel } from '../src/lib/forge';
import { wizardStep } from '../src/lib/wizard';
import { conv, installForgeHarness, replayed, replies } from './helpers/forge';

installForgeHarness();

describe('the Plan sheet', () => {
  it('sets the spec and Forge pack hashes as hash lines, with no check it cannot back', () => {
    render(<ForgeWorkspace conv={conv(replayed(PLAN, FORGING, SHIPPED))} />);
    act(() => useApp.getState().openForgeSheet('plan'));
    const sheet = document.querySelector('[data-sheet="plan"]') as HTMLElement;
    const lines = sheet.querySelectorAll('.plan-hashes .hashline');
    expect(lines).toHaveLength(2);
    expect(lines[0].querySelector('.hashline-hash')!.textContent).toBe(SPEC);
    expect(lines[1].querySelector('.hashline-hash')!.textContent).toBe(PACK);
    // A check here would read as "matches the pre-registration": none until that is compared.
    expect(sheet.querySelectorAll('.plan-hashes .hashline-state')).toHaveLength(0);
    expect(within(sheet).getByText('frozen, receipts-v1.json')).toBeTruthy();
    expect(within(sheet).getByText('holds no text from this task')).toBeTruthy();
    // The sheet says pass mark, not bar, and the settings for the next build come first.
    expect(within(sheet).getByText('Pass mark')).toBeTruthy();
    expect(within(sheet).queryByText('Bar')).toBeNull();
    expect(sheet.querySelector('.ui-card-section')!.id).toBe('plan-next');
    // The rows say build where the terminal says compile.
    expect(within(sheet).getByText(/build up to \$5\.00 and 30 min/)).toBeTruthy();
    act(() => useApp.getState().openForgeSheet(null));
  });

  it('builds from its footer while the plan waits, and saves the next build\'s settings only once changed', async () => {
    const answer = vi.fn(async () => undefined);
    useApp.setState({ answer: answer as never });
    replies.overrides = { overrides: {} };
    render(<ForgeWorkspace conv={conv(replayed(PLAN))} />);
    act(() => useApp.getState().openForgeSheet('plan', 'next'));
    const sheet = document.querySelector('[data-sheet="plan"]') as HTMLElement;
    const next = sheet.querySelector('#plan-next') as HTMLElement;
    expect(within(next).queryByRole('button', { name: 'Save' })).toBeNull();
    fireEvent.change(within(next).getByLabelText('Pass mark (0 to 1)'), { target: { value: '0.5' } });
    expect(within(next).getByRole('button', { name: 'Save' })).toBeTruthy();
    // Build waits while a setting is unsaved: it would build the plan without it.
    expect((sheet.querySelector('[data-action="build"]') as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(within(next).getByRole('button', { name: 'Cancel' }));
    expect(within(next).queryByRole('button', { name: 'Save' })).toBeNull();
    const build = sheet.querySelector('[data-action="build"]') as HTMLElement;
    expect(build.textContent).toBe('Build it');
    await act(async () => {
      fireEvent.click(build);
    });
    expect(answer).toHaveBeenCalledWith('c1', 2, 'yes');
    expect(useApp.getState().forgeSheet).toBeNull();
  });
});

describe('the Report sheet', () => {
  it('opens and closes with Mod+J in a build', () => {
    useApp.setState({ convs: { c1: conv(replayed(PLAN, FORGING, SHIPPED)) }, screen: 'chat', forgeSheet: null });
    render(<><Shortcuts /><ForgeWorkspace conv={conv(replayed(PLAN, FORGING, SHIPPED))} /></>);
    expect(document.querySelector('[data-sheet="report"]')).toBeNull();
    act(() => {
      fireEvent.keyDown(window, isMac() ? { key: 'j', metaKey: true } : { key: 'j', ctrlKey: true });
    });
    expect(useApp.getState().forgeSheet).toBe('report');
    expect(document.querySelector('[data-sheet="report"]')).toBeTruthy();
    act(() => {
      fireEvent.keyDown(window, isMac() ? { key: 'j', metaKey: true } : { key: 'j', ctrlKey: true });
    });
    expect(useApp.getState().forgeSheet).toBeNull();
  });

  it('keeps the harness record: what Forge did, the versions to switch back to, the files', async () => {
    replies.agent = {
      name: 'receipt-reader', licensed: true, spec_hash: SPEC,
      report: { floor_score: 0.44, floor_limit: 0.4, held_out_count: 100 },
      forge: { agent_model: 'nvidia/Nemotron-3-Ultra-550b-a55b', submissions: 1, accepted: 1, pack_sha256: PACK },
      history: [{ revision: 2, spec_hash: SPEC, current: true }, { revision: 1, spec_hash: SPEC }],
      files: [{ path: 'harness.py', size: 900 }],
    };
    render(<ForgeWorkspace conv={conv(replayed(PLAN, FORGING, SHIPPED))} />);
    act(() => useApp.getState().openForgeSheet('report'));
    const sheet = document.querySelector('[data-sheet="report"]') as HTMLElement;
    await within(sheet).findByText('What Forge did');
    expect(within(sheet).getByText('Signed by this computer')).toBeTruthy();
    expect(within(sheet).getByText('Switch to this version')).toBeTruthy();
    expect(within(sheet).getByText('harness.py')).toBeTruthy();
    expect(within(sheet).getByText('Build log')).toBeTruthy();
    expect(sheet.querySelectorAll('.report-stage')).toHaveLength(5);
    expect(sheet.querySelectorAll('.report-hashes .hashline')).toHaveLength(2);
    act(() => useApp.getState().openForgeSheet(null));
  });
});

describe('the harness beside a build', () => {
  it('lets the sidebar step aside until the person brings it back', () => {
    useApp.setState({ convs: { c1: conv(replayed(PLAN, FORGING)) }, screen: 'chat', dock: { tab: null } });
    const { openDock } = useApp.getState();
    openDock('harness');
    expect(sidebarYields(useApp.getState())).toBe(true);
    showSidebar();
    expect(sidebarYields(useApp.getState())).toBe(false);
    openDock(null);
    openDock('harness');
    expect(sidebarYields(useApp.getState())).toBe(true);
    openDock('changes');
    expect(sidebarYields(useApp.getState())).toBe(false);
  });

  it('keeps the agent card its room: beside the dock on a small window the transcript steps aside', () => {
    // jsdom reports every container 578 wide: too little for the transcript beside the card.
    useApp.setState({ dock: { tab: 'harness' }, forgeTranscript: true });
    render(<ForgeWorkspace conv={conv(replayed(PLAN, FORGING))} />);
    expect(document.querySelector('.forge.has-transcript')).toBeNull();
    cleanup();
    useApp.setState({ dock: { tab: null } });
    render(<ForgeWorkspace conv={conv(replayed(PLAN, FORGING))} />);
    expect(document.querySelector('.forge.has-transcript')).toBeTruthy();
    useApp.setState({ forgeTranscript: false });
  });
});

describe('Forge\'s harness versions in the dock', () => {
  it('shows the newest version as a diff from the one before, and the first one whole', () => {
    const feed = replayed(PLAN, FORGING).forge;
    render(<HarnessPane conv={conv({ forge: feed })} />);
    expect(screen.getByText('submitted')).toBeTruthy();
    expect(screen.getByLabelText('Accepted')).toBeTruthy();
    expect(document.querySelectorAll('.change-line.is-add')).toHaveLength(2);
    expect(document.querySelectorAll('.change-line.is-del')).toHaveLength(1);
    fireEvent.click(screen.getByLabelText('Earlier version'));
    expect(screen.getByText('submitted')).toBeTruthy();
    expect(screen.getByText('0.36')).toBeTruthy();
    expect(screen.queryByLabelText('Accepted')).toBeNull();
    expect(document.querySelectorAll('.change-line.is-add')).toHaveLength(0);
    expect(document.querySelectorAll('.change-line.is-ctx')).toHaveLength(3);
  });

  it('shows a quick measure as the cases it passed, never as a practice score', () => {
    // Up to version 1's first measure, on 8 of the 100 practice cases (3 passed).
    const upTo = FORGING.findIndex((m) => (m as { data?: { tool?: string } }).data?.tool === 'forge_submit');
    render(<HarnessPane conv={conv({ forge: replayed(PLAN, FORGING.slice(0, upTo)).forge })} />);
    expect(screen.getByText('measured')).toBeTruthy();
    expect(document.querySelector('.harness-outcome')!.textContent).toBe('measured3 of 8');
    expect(screen.queryByText('0.38')).toBeNull();
  });

  it('wraps a long line under its own start instead of scrolling it sideways', () => {
    const long = '    found = stated(text, [["TOTAL ", "total"], ["SUBTOTAL ", "subtotal"], ["TAX ", "tax"]])\n';
    const feed = replayed(PLAN, [{ type: 'forge', data: { kind: 'tool', tool: 'forge_check', depth: 0, result: { ok: true }, source: { path: 'harness.py', sha256: 'c3', text: long } } }]).forge;
    render(<HarnessPane conv={conv({ forge: feed })} />);
    const code = document.querySelector('.change-code.is-wrapped')!;
    expect(code.querySelector('.change-lead')!.textContent).toBe('    ');
    expect(code.querySelector('.change-rest')!.textContent).toBe(long.trim());
  });
});

// ---------------------------------------------------------------- the guided build, steps 1 and 2 (clarity spec)

const BANNED = /\b(bar|licensed|refused|preset|Forge|harness|rung|ladder|held-out|tuning)\b/i;

/** The rendered words hold no em dash and no word the screens never say (clarity spec §3). */
function plainWords(root: HTMLElement = document.body): void {
  const text = root.textContent ?? '';
  expect(text).not.toContain('—');
  expect(text.match(BANNED)?.[0]).toBeUndefined();
}

function stepOf(msgs: Msg[], patch: Partial<Conv> = {}) {
  const c = demoConv(msgs, patch);
  const model = forgeModel(c.timeline);
  return { c, model, ws: wizardStep(model, c) };
}

function renderStep(msgs: Msg[], patch: Partial<Conv> = {}, withSheet = false) {
  const { c, model, ws } = stepOf(msgs, patch);
  const Screen = ws.step === 1 ? DescribeScreen : PlanScreen;
  render(
    <>
      <Screen conv={c} model={model} ladder={null} detail={null} sub={ws.sub} />
      {withSheet && <PlanSheet conv={c} model={model} />}
    </>,
  );
  return { c, model, ws };
}

const bar = () => screen.getByRole('group', { name: 'Step actions' });
const PLAN_ONLY = DEMO_PLAN.length;
/** The plan Architect drafted from the sentence alone: pass mark 95, no file. */
const SUGGESTED = DEMO_NO_PASS.slice(0, PLAN_ONLY);
const RECEIPT_JOB = "Read a receipt's text and give back the total, subtotal, tax and item count.";
const ASKING: Msg[] = [
  { type: 'turn_started', text: 'Read a receipt and give back the total' },
  { type: 'ask', ask: { id: 1, kind: 'question', question: 'Which fields should it return?', default: 'total, subtotal' } },
];

describe('step 1, Describe', () => {
  it('asks for the job, puts the receipt example first and fills the field without sending', async () => {
    replies.models = { ladder: DEMO_LADDER };
    const send = vi.fn(async () => undefined);
    useApp.setState({ send: send as never });
    const { ws } = renderStep([]);
    expect(ws).toEqual({ step: 1, sub: 'describe' });
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('What job should your agent do?');
    expect(await screen.findByText(/Architect tests three NVIDIA Nemotron models on example cases/)).toBeTruthy();
    expect(screen.getByText('expense-app')).toBeTruthy();
    const examples = screen.getByRole('list', { name: 'Examples' });
    const rows = within(examples).getAllByRole('listitem');
    expect(rows[0].textContent).toBe(RECEIPT_JOB);
    const write = within(bar()).getByRole('button', { name: 'Write the plan' }) as HTMLButtonElement;
    expect(write.disabled).toBe(true);
    const field = document.querySelector('.describe-step textarea') as HTMLTextAreaElement;
    await act(async () => {
      fireEvent.click(within(rows[0]).getByRole('button'));
    });
    expect(field.value).toBe(RECEIPT_JOB);
    expect(send).not.toHaveBeenCalled();
    expect(write.disabled).toBe(false);
    await act(async () => {
      fireEvent.click(write);
    });
    expect(send.mock.calls[0].slice(0, 2)).toEqual(['c1', RECEIPT_JOB]);
    plainWords();
  });

  it('opens a saved plan by putting /make @ in the field, and waits while Architect starts', () => {
    renderStep([], { status: 'starting' });
    expect(screen.getByText('Starting Architect')).toBeTruthy();
    expect((within(bar()).getByRole('button', { name: 'Write the plan' }) as HTMLButtonElement).disabled).toBe(true);
    cleanup();
    renderStep([]);
    act(() => {
      fireEvent.click(screen.getByRole('button', { name: 'Open a plan file you saved (.json)' }));
    });
    expect((document.querySelector('.describe-step textarea') as HTMLTextAreaElement).value).toBe('/make @');
  });

  it('shows only "Opening this build" while a reopened build loads', () => {
    renderStep([], { resuming: true } as Partial<Conv>);
    expect(screen.getByText('Opening this build')).toBeTruthy();
    expect(screen.queryByRole('heading', { level: 1 })).toBeNull();
  });
});

describe('step 2, Plan', () => {
  it('reviews the plan in the bridge\'s numbers', () => {
    const { ws } = renderStep(DEMO_PLAN);
    expect(ws).toEqual({ step: 2, sub: 'plan' });
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Review the plan');
    expect(screen.getByText('Opened from your plan file, receipt-reader.json')).toBeTruthy();
    expect(screen.getByText('The text of one receipt')).toBeTruthy();
    expect(screen.getByText('Total, subtotal, tax and item count')).toBeTruthy();
    expect(screen.getByText('Amounts as whole numbers. A field the receipt does not print comes back empty (null).')).toBeTruthy();
    expect(screen.getByText('7 rules for reading a receipt')).toBeTruthy();
    expect(screen.getByText('Code also checks every answer on two points, for example: Amounts keep every printed digit, so "226,500.00" is 22650000.')).toBeTruthy();
    expect(screen.getByText('Three Nemotron models, cheapest first, one at a time, on 200 receipts from CORD v2, a public receipt dataset.')).toBeTruthy();
    expect(screen.getByText('Pass mark: 40 of 100')).toBeTruthy();
    expect(screen.getByText('At least 40 of the 100 exam receipts must be fully right, and one wrong field makes a receipt wrong. Set in receipt-reader.json, the plan file you opened; change it there.')).toBeTruthy();
    expect(screen.getByText(/Each model tries a few setups, such as rechecking its answer, and keeps its best\./)).toBeTruthy();
    // A plan file sets its own pass mark, so the bar offers its details, not an edit.
    expect(within(bar()).getByRole('button', { name: 'Plan details' })).toBeTruthy();
    expect(within(bar()).queryByRole('button', { name: 'Edit plan' })).toBeNull();
    expect(screen.getByText('100 other receipts it has never seen. A receipt counts only when the whole answer matches the answer key.')).toBeTruthy();
    const tiles = screen.getByRole('list', { name: 'Models' });
    expect(within(tiles).getAllByRole('listitem').map((t) => t.querySelector('.plan-tile-name')!.textContent)).toEqual(['Lightning', 'Super', 'Ultra']);
    expect(within(tiles).getByText(/^\$0\.06 in/)).toBeTruthy();
    expect(screen.getByText('List price per million tokens')).toBeTruthy();
    expect(within(bar()).getByText('Costs at most $0.60')).toBeTruthy();
    expect(within(bar()).getByText('and takes at most 60 min')).toBeTruthy();
    plainWords();
  });

  it('builds on Build agent and sets the plan aside on Back', async () => {
    const answer = vi.fn(async () => undefined);
    useApp.setState({ answer: answer as never });
    renderStep(DEMO_PLAN);
    await act(async () => {
      fireEvent.click(within(bar()).getByRole('button', { name: 'Build agent' }));
    });
    expect(answer).toHaveBeenCalledWith('c1', 3, 'yes');
    await act(async () => {
      fireEvent.click(within(bar()).getByRole('button', { name: 'Back' }));
    });
    expect(answer).toHaveBeenLastCalledWith('c1', 3, 'no');
  });

  it('lists the 7 rules and, apart, the 2 sentences code checks', () => {
    renderStep(DEMO_PLAN);
    act(() => {
      fireEvent.click(screen.getByRole('button', { name: 'View all 7' }));
    });
    const sheet = document.querySelector('[data-sheet="rules"]') as HTMLElement;
    expect(within(sheet).getByRole('heading', { name: 'The 7 rules' })).toBeTruthy();
    expect(within(sheet).getByText(/^The agent follows these on every receipt\. They are written for the receipts in CORD v2, so they use the labels printed on them, in the receipts' own language\. Null means the field comes back empty\. The rules come from receipt-reader\.json; to change them, edit that file\./)).toBeTruthy();
    expect(within(within(sheet).getByRole('list', { name: 'Rules' })).getAllByRole('listitem')).toHaveLength(7);
    expect(within(sheet).getByText('Item count')).toBeTruthy();
    expect(within(sheet).getByText('Count each listed item once, whatever its quantity; the same item on two separate item lines counts twice.')).toBeTruthy();
    expect(within(sheet).getByText('Also checked by code on every answer')).toBeTruthy();
    const checks = within(within(sheet).getByRole('list', { name: 'Checked by code' })).getAllByRole('listitem');
    expect(checks.map((c) => c.textContent)).toEqual([
      'Amounts keep every printed digit, so "226,500.00" is 22650000.',
      'A subtotal equal to the total is null unless the receipt prints that amount as a subtotal.',
    ]);
    plainWords(sheet);
    // Its rules change in the plan file, so the sheet offers no Edit plan.
    expect(within(sheet).queryByRole('button', { name: 'Edit plan' })).toBeNull();
  });

  it('calls a pass mark it drew from the sentence Architect\'s suggestion', () => {
    renderStep(SUGGESTED);
    expect(screen.getByText('Pass mark: 95 of 100')).toBeTruthy();
    expect(screen.getByText("At least 95 of the 100 exam receipts must be fully right, and one wrong field makes a receipt wrong. Architect's suggestion for this job. Change it with Edit plan.")).toBeTruthy();
    expect(screen.queryByText(/^From /)).toBeNull();
    plainWords();
  });

  it('builds with changes saved while the plan waits: no, then the same /make', async () => {
    const answer = vi.fn(async () => undefined);
    const send = vi.fn(async () => undefined);
    useApp.setState({ answer: answer as never, send: send as never });
    replies.overrides = { overrides: {} };
    renderStep(SUGGESTED, {}, true);
    act(() => useApp.getState().openForgeSheet('plan', 'next'));
    const sheet = document.querySelector('[data-sheet="plan"]') as HTMLElement;
    const next = sheet.querySelector('#plan-next') as HTMLElement;
    expect(within(next).getByText('Saved changes apply when you click Build with changes')).toBeTruthy();
    fireEvent.change(within(next).getByLabelText('Pass mark (0 to 1)'), { target: { value: '0.6' } });
    await act(async () => {
      fireEvent.click(within(next).getByRole('button', { name: 'Save' }));
    });
    expect((sheet.querySelector('[data-action="build"]') as HTMLElement).textContent).toBe('Build with changes');
    act(() => useApp.getState().openForgeSheet(null));
    await act(async () => {
      fireEvent.click(within(bar()).getByRole('button', { name: 'Build with changes' }));
    });
    expect(answer).toHaveBeenCalledWith('c1', 3, 'no');
    expect(send).toHaveBeenCalledWith('c1', "/make Read a receipt's text and give back the total, subtotal, tax and item count");
    expect(answer.mock.invocationCallOrder[0]).toBeLessThan(send.mock.invocationCallOrder[0]);
  });

  it('keeps Build agent on a plan file when only what the file fixes was saved', async () => {
    replies.overrides = { overrides: {} };
    renderStep(DEMO_PLAN, {}, true);
    act(() => useApp.getState().openForgeSheet('plan', 'next'));
    const next = document.querySelector('#plan-next') as HTMLElement;
    expect(within(next).getByText(/receipt-reader\.json is built as written/)).toBeTruthy();
    fireEvent.change(within(next).getByLabelText('Pass mark (0 to 1)'), { target: { value: '0.6' } });
    await act(async () => {
      fireEvent.click(within(next).getByRole('button', { name: 'Save' }));
    });
    act(() => useApp.getState().openForgeSheet(null));
    expect(within(bar()).getByRole('button', { name: 'Build agent' })).toBeTruthy();
  });

  it('asks the interview\'s question and answers it from the bar', async () => {
    const answer = vi.fn(async () => undefined);
    useApp.setState({ answer: answer as never });
    const { ws } = renderStep(ASKING);
    expect(ws).toEqual({ step: 2, sub: 'question' });
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('One question before the plan');
    expect(screen.getByText('Architect needs this to write the plan. Nothing runs yet.')).toBeTruthy();
    expect((within(bar()).getByRole('button', { name: 'Back' }) as HTMLButtonElement).disabled).toBe(true);
    expect(within(bar()).queryByRole('textbox')).toBeNull();
    fireEvent.change(screen.getByLabelText('Which fields should it return?'), { target: { value: 'total' } });
    await act(async () => {
      fireEvent.click(within(bar()).getByRole('button', { name: 'Answer' }));
    });
    expect(answer).toHaveBeenCalledWith('c1', 1, 'total');
    plainWords();
  });

  it('cancels the interview\'s question with Escape or Cancel', async () => {
    const answer = vi.fn(async () => undefined);
    useApp.setState({ answer: answer as never });
    renderStep(ASKING);
    await act(async () => {
      fireEvent.keyDown(screen.getByLabelText('Which fields should it return?'), { key: 'Escape' });
    });
    expect(answer).toHaveBeenLastCalledWith('c1', 1, null);
    answer.mockClear();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    });
    expect(answer).toHaveBeenLastCalledWith('c1', 1, null);
  });

  it('reads the job, then writes the plan, with Stop and Add a detail', async () => {
    const cancel = vi.fn(async () => undefined);
    useApp.setState({ cancel: cancel as never });
    const { ws: reading } = renderStep([{ type: 'turn_started', text: 'Read a receipt' }]);
    expect(reading).toEqual({ step: 2, sub: 'reading' });
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Writing the plan');
    expect(screen.getAllByText('Reading the job').length).toBeGreaterThan(0);
    expect(within(bar()).getByRole('textbox', { name: 'Add a detail' })).toBeTruthy();
    await act(async () => {
      fireEvent.click(within(bar()).getByRole('button', { name: 'Stop' }));
    });
    expect(cancel).toHaveBeenCalledWith('c1');
    cleanup();
    const planning: Msg[] = [
      { type: 'turn_started', text: 'Read a receipt' },
      { type: 'event', event: { kind: 'tool_started', call_id: 'm1', name: 'make', summary: 'receipt reader' } },
      { type: 'event', event: { kind: 'progress', text: 'stage 0 understand the job' } },
      { type: 'event', event: { kind: 'subagent', agent: 'floor', text: "'receipts' configured: 200 cases (100 tuning / 100 held-out)" } },
    ];
    const { ws } = renderStep(planning);
    expect(ws).toEqual({ step: 2, sub: 'planning' });
    expect(screen.getByText('Writing the spec and the cases')).toBeTruthy();
    expect(screen.getByText(/200 cases, 100 for practice and 100 for the exam/)).toBeTruthy();
    plainWords();
  });

  it('sets a declined plan aside, with Build this plan and Describe again', async () => {
    const send = vi.fn(async () => undefined);
    const openConversation = vi.fn(() => 'c2');
    useApp.setState({ send: send as never, openConversation: openConversation as never });
    const declined: Msg[] = [...DEMO_PLAN, { type: 'ask_closed', id: 3, answer: 'no' }, { type: 'turn_finished', text: '/make @receipt-reader.json' }];
    const { ws } = renderStep(declined);
    expect(ws).toEqual({ step: 2, sub: 'set_aside' });
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Plan set aside');
    expect(screen.getByText('Pass mark: 40 of 100')).toBeTruthy();
    await act(async () => {
      fireEvent.click(within(bar()).getByRole('button', { name: 'Build this plan' }));
    });
    expect(send).toHaveBeenCalledWith('c1', '/make @receipt-reader.json');
    fireEvent.click(within(bar()).getByRole('button', { name: 'Describe again' }));
    expect(openConversation).toHaveBeenCalledWith({ cwd: DEMO_CWD, kind: 'forge' });
    plainWords();
  });

  it('says why a blocked plan cannot run, with Change settings', () => {
    const blocked = DEMO_PLAN.map((m) => {
      const ev = m.event as Record<string, unknown> | undefined;
      return ev?.kind === 'plan_card' ? { ...m, event: { ...ev, title: 'Plan (blocked): receipt-reader', footer: 'blocked: model_unpriced' } } : m;
    }).filter((m) => m.type !== 'ask');
    const { ws } = renderStep(blocked);
    expect(ws).toEqual({ step: 2, sub: 'blocked' });
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe("This plan can't be built yet");
    expect(screen.getByText('This model has no known price, so the build limit cannot be kept.')).toBeTruthy();
    act(() => {
      fireEvent.click(within(bar()).getByRole('button', { name: 'Change settings' }));
    });
    expect(useApp.getState().forgeSheet).toBe('plan');
    act(() => useApp.getState().openForgeSheet(null));
    plainWords();
  });

  it('says the turn stopped before the plan, with Try again', async () => {
    const send = vi.fn(async () => undefined);
    useApp.setState({ send: send as never });
    const stopped: Msg[] = [
      { type: 'turn_started', text: 'Read a receipt' },
      { type: 'event', event: { kind: 'tool_started', call_id: 'm1', name: 'make', summary: 'receipt reader' } },
      { type: 'event', event: { kind: 'progress', text: 'stage 0 understand the job' } },
      { type: 'turn_finished', text: 'Read a receipt' },
    ];
    const { ws } = renderStep(stopped);
    expect(ws).toEqual({ step: 2, sub: 'stopped_plan' });
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Stopped before the plan');
    await act(async () => {
      fireEvent.click(within(bar()).getByRole('button', { name: 'Try again' }));
    });
    expect(send).toHaveBeenCalledWith('c1', '/make Read a receipt');
    plainWords();
  });
});
