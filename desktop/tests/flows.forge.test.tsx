// @vitest-environment jsdom
/**
 * The build page's shell (clarity spec §2.1): the top bar (project, agent, the six steps,
 * the way out), the step screen picked by the build's state, the action bar's ask box,
 * the conversation rail (closed until asked for, or until an ask only it can answer is
 * waiting), the sidebar that steps aside on a build, the Plan and Report sheets and the
 * dock. The step screens' own contents are their slices' tests (forgeview.*).
 * The bridge is replaced by a recorded `request` (tests/helpers/flows.tsx).
 */
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '../src/App';
import { ForgeWorkspace } from '../src/components/forge/ForgeWorkspace';
import { BottomBar, WizardConv } from '../src/components/forge/wizard/BottomBar';
import { useForgeSidebar } from '../src/components/forge/wizard/TopBar';
import { WizardPage } from '../src/components/forge/wizard/WizardPage';
import { emptyTimeline, fold } from '../src/lib/timeline';
import { useApp, type Conv } from '../src/store/app';
import {
  DEMO_BUILDING, DEMO_CONNECT_APPROVE, DEMO_CONNECTED, DEMO_EXPORT_CONFIRM, DEMO_FILES_WRITTEN, DEMO_NO_PASS, DEMO_PASSED, DEMO_PLAN,
  demoConv, type Msg,
} from './fixtures/demoBuild';
import { calls, conv, installFlowsHarness } from './helpers/flows';

installFlowsHarness();
// The conversation header asks which editors can open the folder (panes/OpenIn.tsx, cached once).
beforeEach(() => {
  global.fetch = (async () => new Response('{"editors": []}')) as never;
});

const ev = (event: Record<string, unknown>) => ({ type: 'event', event });

function forgeOpen(patch: Record<string, unknown> = {}) {
  useApp.setState({
    prefs: { ...useApp.getState().prefs, open: [{ id: 'c1', cwd: 'C:\\rec\\expense-app', kind: 'forge', ...patch }], active: 'c1' },
    forgeSheet: null, forgeTranscript: false, wizardView: {}, wizardSkipped: {}, dock: { tab: null },
  });
}

function page(msgs: Msg[], patch: Partial<Conv> = {}) {
  forgeOpen();
  return render(<ForgeWorkspace conv={demoConv(msgs, patch)} />);
}

/** The step the Stepper marks as the one on screen. */
function currentStep(): string | null {
  return document.querySelector('nav[aria-label="Steps"] [aria-current="step"]')?.getAttribute('data-step') ?? null;
}

function stepItem(n: number): HTMLElement {
  return document.querySelector(`nav[aria-label="Steps"] li[data-step="${n}"]`) as HTMLElement;
}

/** A command the coder wants to run: only the transcript answers it. */
const BASH_ASK: Msg[] = [{ type: 'ask', ask: { id: 41, kind: 'approval', action: 'bash', detail: 'python -m pytest -q' } }];

describe('the top bar', () => {
  it('names the project and the agent, and says New agent before there is one', () => {
    const { unmount } = page([]);
    const bar = document.querySelector('.wizard-top') as HTMLElement;
    expect(bar.classList.contains('drag')).toBe(true);
    expect(within(bar).getByText('Architect')).toBeTruthy();
    expect(within(bar).getByText('expense-app')).toBeTruthy();
    expect(within(bar).getByText('New agent')).toBeTruthy();
    unmount();

    page(DEMO_PLAN);
    const top = document.querySelector('.wizard-top') as HTMLElement;
    expect(within(top).getByText('expense-app')).toBeTruthy();
    expect(within(top).getByText('Receipt reader')).toBeTruthy();
    expect(within(top).queryByText('New agent')).toBeNull();
    expect(document.title).toBe('Receipt reader - Architect');
  });

  it('goes to Your agents and Settings, and every control in the drag region is no-drag', () => {
    page(DEMO_PASSED);
    const top = document.querySelector('.wizard-top') as HTMLElement;
    for (const button of within(top).getAllByRole('button')) expect(button.closest('.no-drag') ?? button.classList.contains('no-drag')).toBeTruthy();
    fireEvent.click(within(top).getByRole('button', { name: 'Your agents' }));
    expect(useApp.getState().screen).toBe('agents');
    fireEvent.click(within(top).getByRole('button', { name: 'Settings' }));
    expect(useApp.getState().screen).toBe('settings');
    act(() => useApp.getState().setScreen('chat'));
  });
});

describe('the six steps', () => {
  const cases: Array<[string, Msg[], string]> = [
    ['an empty build', [], '1'],
    ['DEMO_PLAN', DEMO_PLAN, '2'],
    ['DEMO_BUILDING', DEMO_BUILDING, '3'],
    ['DEMO_PASSED', DEMO_PASSED, '4'],
    ['DEMO_NO_PASS', DEMO_NO_PASS, '4'],
    ['DEMO_EXPORT_CONFIRM', DEMO_EXPORT_CONFIRM, '6'],
    ['DEMO_FILES_WRITTEN', DEMO_FILES_WRITTEN, '6'],
    ['DEMO_CONNECT_APPROVE', DEMO_CONNECT_APPROVE, '6'],
    ['DEMO_CONNECTED', DEMO_CONNECTED, '6'],
  ];
  it.each(cases)('marks the step on screen for %s', (_name, msgs, step) => {
    page(msgs);
    expect(document.querySelectorAll('nav[aria-label="Steps"] ol > li')).toHaveLength(6);
    expect(currentStep()).toBe(step);
    expect(document.querySelectorAll('nav[aria-label="Steps"] [aria-current="step"]')).toHaveLength(1);
    // The page draws that step's screen.
    expect(document.querySelector('.forge')!.getAttribute('data-step')).toBe(step);
  });

  it('labels the steps in order, with the steps before the current one done', () => {
    page(DEMO_BUILDING);
    const labels = [...document.querySelectorAll('.wizard-step-label')].map((n) => n.textContent);
    expect(labels).toEqual(['Describe', 'Plan', 'Build', 'Results', 'Try it', 'Add to project']);
    expect(stepItem(1).classList.contains('is-done')).toBe(true);
    expect(stepItem(2).classList.contains('is-done')).toBe(true);
    expect(stepItem(3).classList.contains('is-current')).toBe(true);
    expect(stepItem(4).classList.contains('is-todo')).toBe(true);
    // Before a result, no step is a button.
    expect(within(document.querySelector('nav[aria-label="Steps"]') as HTMLElement).queryAllByRole('button')).toHaveLength(0);
  });

  it('locks Try it and Add when nothing passed, and marks Results failed', () => {
    page(DEMO_NO_PASS);
    expect(stepItem(4).classList.contains('is-failed')).toBe(true);
    expect(stepItem(5).textContent).toContain(', locked');
    expect(stepItem(6).textContent).toContain(', locked');
    expect(stepItem(4).textContent).toContain(', did not pass');
    expect(stepItem(3).textContent).toContain(', done');
    expect(within(stepItem(5)).queryByRole('button')).toBeNull();
    expect(within(stepItem(6)).queryByRole('button')).toBeNull();
    expect(stepItem(4).textContent).not.toContain('locked');
  });

  it('moves between Results, Try it and Add once a model passed', () => {
    page(DEMO_PASSED);
    expect(stepItem(5).textContent).not.toContain('locked');
    expect(within(stepItem(1)).queryByRole('button')).toBeNull();
    fireEvent.click(within(stepItem(5)).getByRole('button'));
    expect(useApp.getState().wizardView.c1).toBe('try');
    expect(currentStep()).toBe('5');
    fireEvent.click(within(stepItem(6)).getByRole('button'));
    expect(currentStep()).toBe('6');
    fireEvent.click(within(stepItem(4)).getByRole('button'));
    expect(currentStep()).toBe('4');
  });
});

describe('the conversation rail', () => {
  it('is closed by default and opens from the top bar', () => {
    page(DEMO_PLAN);
    expect(document.querySelector('.forge.has-transcript')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Show the conversation' }));
    expect(document.querySelector('.forge.has-transcript')).toBeTruthy();
    expect(screen.getByRole('complementary', { name: 'Conversation' })).toBeTruthy();
    // The plan's approval is the page's to answer: the transcript holds its copy without buttons.
    const held = document.querySelector('.forge-transcript .ask-card.is-held');
    expect(held).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Hide the conversation' }));
    expect(document.querySelector('.forge.has-transcript')).toBeNull();
  });

  it('opens by itself while a command waits outside the Add step, and cannot close then', () => {
    page([...DEMO_PASSED, ...BASH_ASK]);
    expect(currentStep()).toBe('4');
    expect(document.querySelector('.forge.has-transcript')).toBeTruthy();
    const rail = document.querySelector('.forge-transcript') as HTMLElement;
    expect(within(rail).getByText('Run a command')).toBeTruthy();
    expect(within(rail).getByText('Allow for this conversation')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Hide the conversation' }).hasAttribute('disabled')).toBe(true);
  });

  it('leaves the edit approval to the Add step while it shows it, and gives it to the rail elsewhere', () => {
    const { unmount } = page(DEMO_CONNECT_APPROVE);
    expect(document.querySelector('.forge')!.getAttribute('data-sub')).toBe('connect_approve');
    expect(document.querySelector('.forge.has-transcript')).toBeNull();
    unmount();
    // Looking at Results instead, the waiting edit is the rail's.
    forgeOpen();
    useApp.setState({ wizardView: { c1: 'results' } });
    render(<ForgeWorkspace conv={demoConv(DEMO_CONNECT_APPROVE)} />);
    expect(currentStep()).toBe('4');
    expect(document.querySelector('.forge.has-transcript')).toBeTruthy();
  });

  it('leaves the export approval to the Add step', () => {
    page(DEMO_EXPORT_CONFIRM);
    expect(document.querySelector('.forge.has-transcript')).toBeNull();
    // The old export dialog is gone: the Add step asks.
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('gives the export approval to the rail when the person looks at another step, so it never waits unseen', () => {
    forgeOpen();
    useApp.setState({ wizardView: { c1: 'results' } });
    render(<ForgeWorkspace conv={demoConv(DEMO_EXPORT_CONFIRM)} />);
    expect(currentStep()).toBe('4');
    expect(document.querySelector('.forge.has-transcript')).toBeTruthy();
    const rail = document.querySelector('.forge-transcript') as HTMLElement;
    expect(rail.querySelector('.ask-card.is-held')).toBeNull();
    expect(within(rail).getByText('Put the agent into your project')).toBeTruthy();
  });

  it('stays at Results after the export was declined', () => {
    page([...DEMO_EXPORT_CONFIRM, { type: 'ask_closed', id: 4, answer: 'no' }, { type: 'turn_finished', text: '/export receipt-reader', quiet: false, seconds: 2, at: 1791365952.5 }]);
    expect(currentStep()).toBe('4');
    expect(document.querySelector('.forge')!.getAttribute('data-sub')).toBe('passed');
  });
});

describe('the action bar', () => {
  it('asks in one line that sends into the build, and is never the stop control', async () => {
    forgeOpen();
    const c = demoConv(DEMO_BUILDING);
    render(
      <WizardConv.Provider value={c}>
        <WizardPage title="Building your agent" bar={{ secondary: [<button key="s" type="button">Stop build</button>], ask: { placeholder: 'Ask Architect about the build' }, note: 'Results open when all 3 are done.' }} />
      </WizardConv.Provider>,
    );
    const box = screen.getByRole('textbox', { name: 'Ask Architect about the build' }) as HTMLInputElement;
    expect(box.tagName).toBe('INPUT');
    expect(box.type).toBe('text');
    expect(box.placeholder).toBe('Ask Architect about the build');
    const frame = box.closest('.bar-composer') as HTMLElement;
    // A build runs, and still there is no Stop in the ask box: the step has its own.
    expect(within(frame).queryByRole('button', { name: /stop/i })).toBeNull();
    expect(frame.querySelector('.is-stop')).toBeNull();
    fireEvent.change(box, { target: { value: 'Why is Lightning slow?' } });
    await act(async () => {
      fireEvent.keyDown(box, { key: 'Enter' });
    });
    const sent = calls.find((x) => x.method === 'send');
    expect(sent?.params.text).toBe('Why is Lightning slow?');
    expect(box.value).toBe('');
    // Escape in the ask box never cancels the build.
    fireEvent.keyDown(box, { key: 'Escape' });
    expect(calls.some((x) => x.method === 'cancel')).toBe(false);
  });

  it('adds a Stop while a turn the person started runs, on a step with no Stop of its own', async () => {
    forgeOpen();
    const asking = demoConv([...DEMO_PASSED, { type: 'turn_started', text: 'Why did Lightning miss?', quiet: false, at: 1791366000 }]);
    const { unmount } = render(<WizardConv.Provider value={asking}><BottomBar bar={{ ask: { placeholder: 'Ask Architect about the results' } }} /></WizardConv.Provider>);
    const stop = screen.getByRole('button', { name: 'Stop' });
    await act(async () => {
      fireEvent.click(stop);
    });
    expect(calls.some((x) => x.method === 'cancel')).toBe(true);
    unmount();
    // A step with its own Stop shows only that one.
    const own = render(<WizardConv.Provider value={asking}><BottomBar bar={{ ownStop: true }} /></WizardConv.Provider>);
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull();
    own.unmount();
    // A turn waiting on the person (the plan's Build question) is not stopped from the bar.
    render(<WizardConv.Provider value={demoConv(DEMO_PLAN)}><BottomBar bar={{}} /></WizardConv.Provider>);
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull();
  });

  it('lays the bar out as Back, the secondaries, the ask, then the note and the primary', () => {
    forgeOpen();
    const back = { label: 'Back', run: () => undefined };
    render(
      <WizardConv.Provider value={demoConv(DEMO_PLAN)}>
        <BottomBar bar={{ back, secondary: [<button key="e" type="button">Full report</button>], ask: { placeholder: 'Ask Architect about the plan' }, note: 'Costs at most $0.60', primary: <button type="button">Build agent</button> }} />
      </WizardConv.Provider>,
    );
    const bar = screen.getByRole('group', { name: 'Step actions' });
    const order = [...bar.querySelectorAll('button, input, .wizard-bar-note')].map((n) => n.getAttribute('aria-label') ?? n.textContent);
    expect(order).toEqual(['Back', 'Full report', 'Ask Architect about the plan', 'Send', 'Costs at most $0.60', 'Build agent']);
  });

  it('hides the ask box when the step has none, and disables it when asked to', () => {
    forgeOpen();
    const { unmount } = render(<WizardConv.Provider value={demoConv([])}><BottomBar bar={{ primary: <button type="button">Write the plan</button> }} /></WizardConv.Provider>);
    expect(screen.queryByRole('textbox')).toBeNull();
    unmount();
    render(<WizardConv.Provider value={demoConv(DEMO_PLAN)}><BottomBar bar={{ ask: { placeholder: 'Ask Architect about the plan', disabled: true } }} /></WizardConv.Provider>);
    expect((screen.getByRole('textbox', { name: 'Ask Architect about the plan' }) as HTMLInputElement).disabled).toBe(true);
  });

  it('shows an error under the sub with its action, and disables the step while the connection is lost', () => {
    forgeOpen();
    render(
      <WizardPage title="The build stopped" sub="You stopped the build." attention="The relay refused the call." attentionAction={<button type="button">Retry</button>} bar={{ primary: <button type="button">Build again</button> }}>
        <div className="card-cell"><div className="ui-card">x</div></div>
      </WizardPage>,
    );
    expect(screen.getByRole('alert').textContent).toContain('The relay refused the call.');
    expect(document.querySelector('.wizard-page[data-attention]')).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Build again' }) as HTMLButtonElement).closest('fieldset')!.disabled).toBe(false);
    act(() => useApp.setState({ ready: true, online: false }));
    expect((screen.getByRole('button', { name: 'Build again' }).closest('fieldset') as HTMLFieldSetElement).disabled).toBe(true);
    act(() => useApp.setState({ ready: false }));
  });
});

describe('the page around the steps', () => {
  it('shows a reopened build loading, not the Describe step', () => {
    forgeOpen({ session: 's1' });
    render(<ForgeWorkspace conv={{ ...demoConv([]), status: 'starting' }} />);
    expect(screen.getByText('Opening this build')).toBeTruthy();
    expect(document.querySelector('.wizard-page')).toBeNull();
  });

  it('offers Retry on a build conversation that stopped before anything was said', () => {
    page([], { status: 'stopped' });
    expect(currentStep()).toBe('1');
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
  });

  it('keeps a chat switched into build mode a chat until a build starts in it', () => {
    useApp.setState({ prefs: { ...useApp.getState().prefs, open: [{ id: 'c1', cwd: 'C:/p', kind: 'chat' }], active: 'c1' } });
    const timeline = [{ type: 'turn_started', text: 'hello' }, ev({ kind: 'answer', text: 'Hi.' }), { type: 'turn_finished', text: '' }]
      .reduce((t, m) => fold(t, m), emptyTimeline());
    const chat = conv({ timeline, state: { ...conv().state, state: 'forge' } });
    render(<ForgeWorkspace conv={chat} />);
    expect(document.querySelector('.wizard-top')).toBeNull();
    expect(screen.getByRole('radio', { name: 'Chat' })).toBeTruthy();
  });

  it('opens the Plan and Report sheets only over the build they were opened on, with no em dash', () => {
    forgeOpen();
    useApp.setState({ forgeSheet: 'report', forgeSheetConv: 'other' });
    render(<ForgeWorkspace conv={demoConv(DEMO_PASSED)} />);
    expect(document.querySelector('[data-sheet]')).toBeNull();
    act(() => useApp.getState().openForgeSheet('plan', 'next'));
    const sheet = document.querySelector('[data-sheet="plan"]') as HTMLElement;
    expect(sheet.querySelector('#plan-next')).toBeTruthy();
    expect(sheet.textContent).not.toContain('\u2014');
    act(() => useApp.getState().openForgeSheet('report'));
    const report = document.querySelector('[data-sheet="report"]') as HTMLElement;
    expect(report).toBeTruthy();
    expect(report.textContent).not.toContain('\u2014');
    act(() => useApp.getState().openForgeSheet(null));
  });

  it('keeps the dock beside the step', () => {
    forgeOpen();
    useApp.setState({ dock: { tab: 'changes' } });
    render(<ForgeWorkspace conv={demoConv(DEMO_PLAN)} />);
    expect(document.querySelector('.forge-row.has-dock')).toBeTruthy();
    act(() => useApp.getState().openDock(null));
    useApp.setState({ dock: { tab: null } });
  });
});

describe('the sidebar', () => {
  function mountApp(c: Conv, kind: 'forge' | 'chat') {
    useForgeSidebar.setState({ on: false });
    useApp.setState({
      ready: true,
      screen: 'chat',
      init: (async () => undefined) as never,
      refreshSessions: (async () => undefined) as never,
      sessions: [],
      convs: { c1: c },
      prefs: { ...useApp.getState().prefs, sidebar: true, open: [{ id: 'c1', cwd: 'C:\\rec\\expense-app', kind }], active: 'c1' },
    });
    return render(<App />);
  }

  it('steps aside on a build, and the top bar brings it back for the session', () => {
    mountApp(demoConv(DEMO_PLAN), 'forge');
    expect(document.querySelector('.sidebar')).toBeNull();
    expect(document.querySelector('.main.no-sidebar')).toBeTruthy();
    // The bar's own button, not the floating one normal chats use.
    expect(document.querySelector('.main > .sidebar-show')).toBeNull();
    fireEvent.click(within(document.querySelector('.wizard-top') as HTMLElement).getByRole('button', { name: 'Show the sidebar' }));
    expect(document.querySelector('.sidebar')).toBeTruthy();
    expect(within(document.querySelector('.wizard-top') as HTMLElement).queryByRole('button', { name: 'Show the sidebar' })).toBeNull();
  });

  it('comes back on the first Mod+B on a build, though the preference already said shown', () => {
    mountApp(demoConv(DEMO_PLAN), 'forge');
    expect(document.querySelector('.sidebar')).toBeNull();
    act(() => useApp.getState().setPrefs({ sidebar: !useApp.getState().prefs.sidebar }));
    expect(document.querySelector('.sidebar')).toBeTruthy();
    expect(useApp.getState().prefs.sidebar).toBe(true);
    // From then on Mod+B toggles it as usual.
    act(() => useApp.getState().setPrefs({ sidebar: false }));
    expect(document.querySelector('.sidebar')).toBeNull();
  });

  it('stays on a normal chat', () => {
    mountApp(conv(), 'chat');
    expect(document.querySelector('.sidebar')).toBeTruthy();
    expect(document.querySelector('.main.no-sidebar')).toBeNull();
    expect(document.querySelector('.wizard-top')).toBeNull();
  });
});
