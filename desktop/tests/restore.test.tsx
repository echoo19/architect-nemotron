// @vitest-environment jsdom
/**
 * A saved build conversation reopened after the app restarts: the transcript alone holds
 * only the turns the model answered, so the bridge keeps what the conversation showed
 * (architect/desktop/views.py) and the page folds it again. The whole conversation comes
 * back (the plan set aside, the /make turn and its build, Try it, the export), and the
 * agent panel shows the built agent, not "New agent".
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { ForgeWorkspace } from '../src/components/forge/ForgeWorkspace';
import { emptyFeed, forgeModel, ladderView } from '../src/lib/forge';
import { fold, workLabel, type Item, type Timeline } from '../src/lib/timeline';
import { DEFAULT_PREFS, restored, useApp, type Conv, type SavedHistory } from '../src/store/app';
import { FORGING, LADDER, PLAN, SHIPPED } from './fixtures/ladderBuild';

const ev = (event: Record<string, unknown>) => ({ type: 'event', event });
type Msg = Record<string, unknown>;
const at = (msg: Msg, seconds: number): Msg => ({ ...msg, at: seconds });
const REQUEST = 'Read a receipt and give back the total, subtotal, tax and item count';

/** Turn 1: the description goes to make, the drafted plan is set aside (Not now). */
const NOT_NOW: Msg[] = [
  at({ type: 'turn_started', text: REQUEST, quiet: false }, 1000),
  ev({ kind: 'tool_started', call_id: 'make-1', name: 'make', summary: REQUEST }),
  ev({ kind: 'plan_card', title: 'Plan: receipt-fields', rows: [['task', 'Read one receipt'], ['floor', "held_out_tasks >= 0.80 on 'receipt-fields'"]], footer: 'spec 0123abcd.', data: { bar: 0.8, domain: [0.5, 1], kind: 'held_out_tasks', ladder: LADDER } }),
  { type: 'ask', ask: { id: 1, kind: 'question', question: 'Compile this agent?', default: 'yes', choices: [] } },
  { type: 'ask_closed', id: 1, answer: 'no' },
  ev({ kind: 'tool_finished', call_id: 'make-1', name: 'make', ok: true, summary: '{"cancelled": true, "stage": "plan", "spec_hash": "0123abcd"}' }),
  ev({ kind: 'answer', text: 'Set aside. Nothing was built.' }),
  at({ type: 'turn_finished', text: REQUEST, quiet: false, seconds: 20 }, 1020),
];

/** Turn 2: `/make @receipt-reader.json` runs make directly (no tool step), the build ships. */
const BUILD: Msg[] = [
  at({ type: 'turn_started', text: '/make @receipt-reader.json', quiet: false }, 1100),
  ...PLAN.filter((msg) => (msg.event as { kind?: string } | undefined)?.kind === 'plan_card' || msg.type === 'ask'),
  { type: 'ask_closed', id: 2, answer: '' },
  ...FORGING.filter((msg) => msg.type !== 'answered' && msg.type !== 'ask_closed'),
  ...SHIPPED.filter((msg) => (msg.event as { kind?: string } | undefined)?.kind !== 'tool_finished' && msg.type !== 'turn_finished'),
  at({ type: 'turn_finished', text: '/make @receipt-reader.json', quiet: false, seconds: 1100 }, 2200),
];

/** Try it: a quiet turn. Then Put into this project. */
const TRY_AND_EXPORT: Msg[] = [
  at({ type: 'turn_started', text: '/run receipt-reader', quiet: true }, 2300),
  ev({ kind: 'subagent', agent: 'receipt-reader', text: `running v1 on ${LADDER[0]}` }),
  at({ type: 'turn_finished', text: '/run receipt-reader', quiet: true, seconds: 3 }, 2303),
  at({ type: 'turn_started', text: '/export receipt-reader', quiet: false }, 2400),
  ev({ kind: 'plan_card', title: 'Export plan: agents/receipt-reader/', rows: [['size', '6 files, 12 KB']], footer: 'Nothing has been written into the repository yet.' }),
  { type: 'ask', ask: { id: 3, kind: 'approval', action: 'export', detail: 'receipt_reader (6 files, 12 KB) into agents/receipt-reader/' } },
  { type: 'ask_closed', id: 3, answer: 'yes' },
  ev({ kind: 'plan_card', title: 'Exported: agents/receipt-reader/', rows: [['verified', 'ok']], footer: 'Human review required; the feature flag is OFF until set.' }),
  at({ type: 'turn_finished', text: '/export receipt-reader', quiet: false, seconds: 9 }, 2409),
];

/** What the transcript holds of the same conversation: turn 1, then notes the page never shows. */
const TRANSCRIPT = [
  { role: 'user', text: REQUEST },
  { role: 'assistant', tool_calls: [{ id: 'make-1', name: 'make', arguments: { description: REQUEST } }] },
  { role: 'tool', call_id: 'make-1', name: 'make', text: '{"cancelled": true, "stage": "plan", "spec_hash": "0123abcd"}' },
  { role: 'assistant', text: 'Set aside. Nothing was built.' },
  { role: 'note', text: 'The user ran the make tool directly: {"licensed": true, "installed": "receipt-reader"}' },
  { role: 'note', text: 'The user ran the run_agent tool directly: {"agent": "receipt-reader"}' },
  { role: 'note', text: 'The user ran /export: {"exported": true, "agent": "receipt-reader"}' },
];

const SAVED: SavedHistory = { messages: TRANSCRIPT, view: [...NOT_NOW, ...BUILD, ...TRY_AND_EXPORT] };

const users = (t: Timeline) => t.items.filter((i): i is Extract<Item, { kind: 'user' }> => i.kind === 'user').map((i) => i.text);
const titles = (t: Timeline) => t.items.filter((i): i is Extract<Item, { kind: 'plan' }> => i.kind === 'plan').map((i) => i.title);

function conv(patch: Partial<Conv> = {}): Conv {
  return {
    id: 'c1', status: 'ready', timeline: restored({ messages: [] }).timeline, forge: emptyFeed(), links: [],
    state: { started: true, state: 'forge', permission: 'ask', model: 'fake', models: ['fake'], cwd: 'C:/p' },
    ...patch,
  };
}

let replies: Record<string, unknown> = {};
let calls: Array<{ method: string; params: Record<string, unknown> }> = [];
const width = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientWidth');

beforeAll(() => {
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, get: () => 578 });
});
afterAll(() => {
  if (width) Object.defineProperty(HTMLElement.prototype, 'clientWidth', width);
});

beforeEach(() => {
  replies = {};
  calls = [];
  window.matchMedia = window.matchMedia ?? ((() => ({ matches: false, addEventListener() {}, removeEventListener() {} })) as never);
  Element.prototype.scrollIntoView = () => undefined;
  global.fetch = vi.fn(async () => new Response('{}')) as never;
  const request = vi.fn(async (_id: string, method: string, params: Record<string, unknown> = {}) => {
    calls.push({ method, params });
    return replies[method] ?? {};
  });
  useApp.setState({
    prefs: { ...DEFAULT_PREFS, open: [], active: null, trusted: ['C:/p'] },
    request: request as never,
    convs: {},
    dock: { tab: null },
  });
});

afterEach(() => cleanup());

/** The step the top bar marks as the one on screen. */
function currentStep(): string | null {
  return document.querySelector('nav[aria-label="Steps"] [aria-current="step"]')?.getAttribute('data-step') ?? null;
}

/** Try it from the top bar's steps: the reopened agent is a step away. */
async function openTryIt() {
  const tryIt = document.querySelector('nav[aria-label="Steps"] li[data-step="5"]') as HTMLElement;
  fireEvent.click(within(tryIt).getByRole('button'));
  await waitFor(() => expect(currentStep()).toBe('5'));
  // The reopened agent's own form, with its field and Run agent, not only the step.
  await waitFor(() => expect(document.querySelector('.try-card .try-form')).toBeTruthy());
  expect(within(document.querySelector('.try-card') as HTMLElement).getByRole('button', { name: /Run agent/ })).toBeTruthy();
}

describe('a saved build conversation, reopened', () => {
  it('shows every turn it showed, not only the ones the model answered', () => {
    const { timeline } = restored(SAVED);
    expect(users(timeline)).toEqual([REQUEST, '/make @receipt-reader.json', '/export receipt-reader']);
    expect(titles(timeline)).toEqual([
      'Plan: receipt-fields', 'Plan: receipt-reader', 'Result: receipt-reader v1',
      'Export plan: agents/receipt-reader/', 'Exported: agents/receipt-reader/',
    ]);
    // Try it's run folds as it did live: "Ran receipt-reader".
    const works = timeline.items.filter((i): i is Extract<Item, { kind: 'work' }> => i.kind === 'work');
    expect(works.some((w) => w.steps.length === 1 && w.steps[0].name === 'receipt-reader')).toBe(true);
    expect(timeline.items.find((i) => i.kind === 'answer')).toMatchObject({ text: 'Set aside. Nothing was built.' });
    // Nothing left running or waiting.
    expect(timeline.running).toBe(false);
    expect(timeline.items.filter((i) => i.kind === 'ask').every((i) => i.kind === 'ask' && i.closed)).toBe(true);
  });

  it('gives the agent panel the built agent and its bar view', () => {
    const { timeline, forge } = restored(SAVED);
    const model = forgeModel(timeline);
    expect(model.phase).toBe('exported');
    expect(model.result).toMatchObject({ name: 'receipt-reader', licensed: true });
    // The Forge feed comes back too: the rungs' practice scores and the shipped exam.
    const ladder = ladderView(model, forge)!;
    expect(ladder.rungs[0]).toMatchObject({ state: 'done', preset: 0.31, practice: [0.36, 0.48], ships: true });
    expect(ladder.exam?.counts).toMatchObject({ cases: 100, correct: 44 });
  });

  it('draws the reopened agent in the panel, with Try it', async () => {
    replies.agent = { name: 'receipt-reader', licensed: true, spec: { input_schema: { type: 'object', properties: { text: { type: 'string' } } } } };
    const { timeline, forge } = restored(SAVED);
    render(<ForgeWorkspace conv={conv({ timeline, forge })} />);
    expect(within(document.querySelector('.wizard-top') as HTMLElement).getByText('Receipt reader')).toBeTruthy();
    expect(screen.queryByText('New agent')).toBeNull();
    // An exported build reopens on Add to project; Try it is one step away.
    expect(currentStep()).toBe('6');
    await openTryIt();
  });

  it('times a reopened build from Build it, as it showed live', () => {
    // The 2026-10-07 film build: /make, the plan card, Build it ten seconds later, then the
    // build. Live its fold read 18m 54s; before events and settled questions carried their
    // time, a reopened one read 19m 4s, counted from /make.
    const view: Msg[] = [
      at({ type: 'turn_started', text: '/make @receipt-reader.json', quiet: false }, 1000),
      at(ev({ kind: 'plan_card', title: 'Plan: receipt-reader', rows: [], footer: '' }), 1000.4),
      { type: 'ask', ask: { id: 1, kind: 'question', question: 'Compile this agent?', default: 'yes', choices: [] } },
      at({ type: 'ask_closed', id: 1, answer: 'yes' }, 1010),
      at(ev({ kind: 'subagent', agent: 'compiler', text: 'compiling receipt-reader (spec 4d80f3d5)' }), 1010.2),
      at(ev({ kind: 'progress', text: 'stage 4 verify: 1500 calls, $0.3078' }), 2100),
      at(ev({ kind: 'plan_card', title: 'Result: receipt-reader v1', rows: [], footer: '' }), 2144),
      at({ type: 'turn_finished', text: '/make @receipt-reader.json', quiet: false, seconds: 1144.2 }, 2144.2),
    ];
    const { timeline } = restored({ messages: [], view });
    const work = timeline.items.find((i): i is Extract<Item, { kind: 'work' }> => i.kind === 'work')!;
    expect(workLabel(work)).toBe('Built the agent in 18m 54s');
  });

  it('keeps the turns saved before the record began, from the transcript, first', () => {
    const older: SavedHistory = {
      messages: [{ role: 'user', text: 'what is in this folder?' }, { role: 'assistant', text: 'An expense app.' }, ...TRANSCRIPT],
      view: [{ type: 'view', from: 0, to: 2 }, ...NOT_NOW, ...BUILD],
    };
    const { timeline } = restored(older);
    expect(users(timeline)).toEqual(['what is in this folder?', REQUEST, '/make @receipt-reader.json']);
    expect(timeline.items[1]).toMatchObject({ kind: 'answer', text: 'An expense app.' });
  });

  it('shows turns taken in the terminal where they came, from the transcript', () => {
    // The terminal keeps no record: the next desktop process marks what it never saw.
    const terminal = [{ role: 'user', text: 'what did you plan?' }, { role: 'assistant', text: 'A receipt reader, set aside.' }];
    const mixed: SavedHistory = {
      messages: [...TRANSCRIPT.slice(0, 4), ...terminal, ...TRANSCRIPT.slice(4)],
      view: [...NOT_NOW, { type: 'view', from: 4, to: 6 }, ...BUILD],
    };
    const { timeline, forge } = restored(mixed);
    expect(users(timeline)).toEqual([REQUEST, 'what did you plan?', '/make @receipt-reader.json']);
    expect(timeline.items.some((i) => i.kind === 'answer' && i.text === 'A receipt reader, set aside.')).toBe(true);
    expect(forgeModel(timeline).phase).toBe('licensed');
    expect(forge.stages.length).toBeGreaterThan(0);
    const keys = timeline.items.map((i) => i.key);
    expect(new Set(keys).size).toBe(keys.length);
  });

  it('reads an older conversation from its transcript alone', () => {
    const { timeline } = restored({ messages: TRANSCRIPT, view: null });
    expect(users(timeline)).toEqual([REQUEST]);
    expect(timeline.items.map((i) => i.kind)).toEqual(['user', 'work', 'answer']);
  });

  it('ends a turn the app closed during where its record ends', () => {
    const cut = BUILD.slice(0, BUILD.findIndex((m) => (m.event as { kind?: string } | undefined)?.kind === 'progress' && String((m.event as { text?: string }).text).startsWith('stage 3')) + 1);
    const { timeline, forge } = restored({ messages: TRANSCRIPT.slice(0, 4), view: [...NOT_NOW, ...cut] });
    expect(timeline.running).toBe(false);
    expect(timeline.thinking).toBeNull();
    const work = timeline.items.filter((i): i is Extract<Item, { kind: 'work' }> => i.kind === 'work').at(-1)!;
    expect(work.running).toBe(false);
    // It ended at the record's last time, not when the app opened again.
    expect(work.ended).toBe(1100 * 1000);
    expect(forgeModel(timeline).phase).toBe('building');
    expect(forge.stages.length).toBeGreaterThan(0);
  });

  it('never takes a new question for one an earlier process asked under the same number', () => {
    let { timeline } = restored(SAVED);
    // The new process numbers its questions from 1 again.
    timeline = fold(timeline, { type: 'turn_started', text: 'build another' });
    timeline = fold(timeline, { type: 'ask', ask: { id: 1, kind: 'question', question: 'Where will this agent live?', default: '1', choices: ['1', '2'] } });
    const live = () => timeline.items.filter((i): i is Extract<Item, { kind: 'ask' }> => i.kind === 'ask' && i.ask.id === 1);
    expect(live().map((a) => a.closed)).toEqual([true, false]);
    timeline = fold(timeline, { type: 'answered', id: 1, value: '2' });
    timeline = fold(timeline, { type: 'ask_closed', id: 1, answer: '2' });
    expect(live().map((a) => [a.closed, a.answer])).toEqual([[true, 'no'], [true, '2']]);
    // Keys stay unique: the reopened items' keys start with "h".
    const keys = timeline.items.map((i) => i.key);
    expect(new Set(keys).size).toBe(keys.length);
  });
});

describe('reopening it after a restart', () => {
  it('boots with the saved id and draws the conversation and the agent from the saved record', async () => {
    replies.onboarding = { profile_exists: true, choices: [] };
    replies.capture = { events: [] };
    replies.sessions = { sessions: [] };
    replies.history = SAVED;
    replies.agent = { name: 'receipt-reader', licensed: true, spec: { input_schema: { type: 'object', properties: { text: { type: 'string' } } } } };
    const id = useApp.getState().openConversation({ cwd: 'C:/p', kind: 'forge', resume: 'abc123def456', title: REQUEST });
    // While booting, the core says "Resumed ..."; the saved conversation makes it redundant.
    useApp.setState((s) => ({ convs: { ...s.convs, [id]: { ...s.convs[id], timeline: fold(s.convs[id].timeline, ev({ kind: 'notice', level: 'info', text: 'Resumed abc123def456: x (7 messages).' })) } } }));
    await act(() => useApp.getState().boot(id));
    expect(calls.find((c) => c.method === 'boot')?.params.resume).toBe('abc123def456');
    const after = useApp.getState().convs[id];
    expect(after.status).toBe('ready');
    expect(users(after.timeline)).toEqual([REQUEST, '/make @receipt-reader.json', '/export receipt-reader']);
    expect(after.timeline.items.some((i) => i.kind === 'notice' && i.text.startsWith('Resumed '))).toBe(false);
    expect(after.forge.stages.length).toBeGreaterThan(0);
    render(<ForgeWorkspace conv={after} />);
    expect(within(document.querySelector('.wizard-top') as HTMLElement).getByText('Receipt reader')).toBeTruthy();
    expect(currentStep()).toBe('6');
    await openTryIt();
  });
});
