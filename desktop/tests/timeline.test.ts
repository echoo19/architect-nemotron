import { describe, expect, it } from 'vitest';
import { buildTitle, emptyTimeline, failedStep, fold, fromHistory, workLabel, type Item, type Timeline } from '../src/lib/timeline';

const ev = (event: Record<string, unknown>) => ({ type: 'event', event });
const run = (msgs: Record<string, unknown>[], start: Timeline = emptyTimeline()) =>
  msgs.reduce((t, m, i) => fold(t, m, 1_000 + i * 1_000), start);

describe('fold', () => {
  it('turns a turn into user, work and answer', () => {
    const t = run([
      { type: 'turn_started', text: 'look around' },
      ev({ kind: 'thinking', active: true }),
      ev({ kind: 'tool_started', call_id: 'c1', name: 'list_dir', summary: '.' }),
      ev({ kind: 'tool_finished', call_id: 'c1', name: 'list_dir', ok: true, summary: '3 entries' }),
      ev({ kind: 'delta', text: 'Here ' }),
      ev({ kind: 'delta', text: 'it is' }),
      ev({ kind: 'answer', text: 'Here it is.' }),
      { type: 'turn_finished', text: 'look around' },
    ]);
    expect(t.items.map((i) => i.kind)).toEqual(['user', 'work', 'answer']);
    const work = t.items[1] as Extract<Item, { kind: 'work' }>;
    expect(work.running).toBe(false);
    expect(work.steps[0]).toMatchObject({ name: 'list_dir', state: 'ok', result: '3 entries' });
    expect(workLabel(work)).toBe('Looked in a folder in 5s');
    expect(t.items[2]).toMatchObject({ text: 'Here it is.', streaming: false });
    expect(t.running).toBe(false);
  });

  it('starts a new block for words that come after a tool call', () => {
    // A filmed take showed "...agent.Now I'll" when the model spoke, called a tool and spoke again.
    const t = run([
      { type: 'turn_started', text: 'build it' },
      ev({ kind: 'delta', text: "I'll build the agent." }),
      ev({ kind: 'tool_started', call_id: 'c1', name: 'make', summary: 'receipt reader' }),
      ev({ kind: 'tool_finished', call_id: 'c1', name: 'make', ok: true, summary: 'licensed' }),
      ev({ kind: 'delta', text: "Now I'll check it." }),
      ev({ kind: 'answer', text: "Now I'll check it." }),
      { type: 'turn_finished', text: 'build it' },
    ]);
    expect(t.items.map((i) => i.kind)).toEqual(['user', 'answer', 'work', 'answer']);
    expect(t.items[1]).toMatchObject({ text: "I'll build the agent.", streaming: false });
    expect(t.items[3]).toMatchObject({ text: "Now I'll check it.", streaming: false });
  });

  it('keeps one progress line per running build stage', () => {
    const t = run([
      { type: 'turn_started', text: '/make @receipt-reader.json' },
      ev({ kind: 'progress', text: 'stage 1 select preset: 0 calls, $0.00 (nvidia/a, rung 1 of 2)' }),
      ev({ kind: 'progress', text: 'stage 2 tune: 0 calls, $0.00 (nvidia/a, rung 1 of 2)' }),
      ev({ kind: 'progress', text: 'stage 2 tune: 40 calls, $0.02 (nvidia/a, rung 1 of 2)' }),
      ev({ kind: 'progress', text: 'stage 2 tune: 90 calls, $0.05 (nvidia/a, rung 1 of 2)' }),
      ev({ kind: 'progress', text: 'stage 4 verify: 90 calls, $0.05 (nvidia/a, rung 1 of 2)' }),
      ev({ kind: 'progress', text: 'floor: drafting 12 cases with m ...' }),
      ev({ kind: 'progress', text: 'floor: drafting 12 cases with m ...' }),
    ]);
    const work = t.items.find((i) => i.kind === 'work') as Extract<Item, { kind: 'work' }>;
    expect(work.progressLog).toEqual([
      'stage 1 select preset: 0 calls, $0.00 (nvidia/a, rung 1 of 2)',
      'stage 2 tune: 90 calls, $0.05 (nvidia/a, rung 1 of 2)',
      'stage 4 verify: 90 calls, $0.05 (nvidia/a, rung 1 of 2)',
      'floor: drafting 12 cases with m ...',
      'floor: drafting 12 cases with m ...',
    ]);
  });

  it('nests subagent lines under the running step and marks failures', () => {
    const t = run([
      { type: 'turn_started', text: 'fix it' },
      ev({ kind: 'tool_started', call_id: 'c1', name: 'code', summary: 'fix the bug' }),
      ev({ kind: 'subagent', agent: 'coder', text: 'edit src/a.py: ok' }),
      ev({ kind: 'tool_finished', call_id: 'c1', name: 'code', ok: false, summary: 'coder_failed' }),
      { type: 'turn_finished' },
    ]);
    const work = t.items[1] as Extract<Item, { kind: 'work' }>;
    expect(work.steps[0].lines).toEqual([{ agent: 'coder', text: 'edit src/a.py: ok' }]);
    expect(work.steps[0].state).toBe('error');
    expect(workLabel(work)).toContain('1 failed');
  });

  describe('build steps say what they did', () => {
    type Work = Extract<Item, { kind: 'work' }>;
    const works = (t: Timeline) => t.items.filter((i) => i.kind === 'work') as Work[];
    // Tool results arrive as the CLI's one-line summaries: the JSON, cut at 100 characters.
    const declined = '{"cancelled": true, "stage": "plan", "spec_hash": "4d80f3d580cac16d3b384eb7f8ca7d84990bd48b8fca0482cca4..."';
    const refused = '{"error": "one_build_per_turn", "detail": "A build already ran in this turn. Tell the user what happened; they ...';
    const plan = (answer: string) => [
      { type: 'ask', ask: { id: 1, kind: 'question', question: 'Compile this agent?', default: 'yes', choices: [] } },
      { type: 'answered', id: 1, value: answer },
      { type: 'ask_closed', id: 1, answer },
    ];

    it('never calls a plan set aside built, and a second build refused before it ran never failed', () => {
      // A filmed take: after Not now the model listed the agents and called make again. The
      // second make was refused (one build per turn) and its fold read "Built the agent in 1s,
      // 1 failed" with a warning mark; the first make's fold read "Built the agent" too.
      const t = run([
        { type: 'turn_started', text: "Read a receipt's text and give back the total" },
        ev({ kind: 'tool_started', call_id: 'm1', name: 'make', summary: "make(Read a receipt's text)" }),
        ...plan('no'),
        ev({ kind: 'tool_finished', call_id: 'm1', name: 'make', ok: true, summary: declined }),
        ev({ kind: 'tool_started', call_id: 'a1', name: 'agents', summary: 'agents()' }),
        ev({ kind: 'tool_finished', call_id: 'a1', name: 'agents', ok: true, summary: '0 agents' }),
        ev({ kind: 'answer', text: "Now I'll invoke the make tool." }),
        ev({ kind: 'tool_started', call_id: 'm2', name: 'make', summary: 'make(receipt reader)' }),
        ev({ kind: 'tool_finished', call_id: 'm2', name: 'make', ok: false, summary: refused }),
        ev({ kind: 'answer', text: 'The system allows only one build per turn.' }),
        { type: 'turn_finished' },
      ]);
      const [first, , second] = works(t);
      expect(workLabel(first)).toBe('Planned the agent in 5s');
      expect(buildTitle(first.steps[0])).toBe('Planned the agent');
      expect(workLabel(second)).toBe('Skipped a second build');
      expect(failedStep(second.steps[0])).toBe(false);
      expect(buildTitle(second.steps[0])).toBe('Skipped a second build');
    });

    it('says a cancelled question, a failed build and a build that ran apart', () => {
      const label = (ok: boolean, summary: string) => workLabel(works(run([
        { type: 'turn_started', text: 'an agent that tags support emails' },
        ev({ kind: 'tool_started', call_id: 'm', name: 'make', summary: 'make(x)' }),
        ev({ kind: 'tool_finished', call_id: 'm', name: 'make', ok, summary }),
        { type: 'turn_finished' },
      ]))[0]);
      expect(label(true, '{"cancelled": true, "stage": "interview"}')).toBe('Asked about the agent in 2s');
      expect(label(false, '{"error": "tool_failed", "detail": "RuntimeError"}')).toBe('Tried to build the agent in 2s, 1 failed');
      expect(label(false, 'interrupted')).toBe('Tried to build the agent in 2s, 1 failed');
      expect(label(true, '{"licensed": true, "installed": "tags-support-emails", "revision": 1, "floor": "1.00/0.90"...')).toBe('Built the agent in 2s');
      expect(label(true, '{"licensed": false, "compiled": true, "outcome": "This build was refused, so nothing was installed...')).toBe('Built the agent in 2s');
      // Live: a build that stopped at case drafting, 9 seconds in, read "Built the agent".
      expect(label(true, '{"licensed": false, "compiled": false, "failures": ["floor_draft_failed"], "next_step": "the model...')).toBe('Tried to build the agent in 2s');
    });

    it('reads the same from a saved conversation', () => {
      const t = fromHistory([
        { role: 'user', text: 'an agent that tags support emails' },
        { role: 'assistant', tool_calls: [{ id: 'm1', name: 'make', arguments: { description: 'x' } }] },
        { role: 'tool', call_id: 'm1', name: 'make', text: '{"cancelled": true, "stage": "plan", "spec_hash": "abc"}' },
        { role: 'assistant', text: 'Set aside. Nothing was built.' },
        { role: 'user', text: 'build it twice' },
        { role: 'assistant', tool_calls: [{ id: 'm2', name: 'make', arguments: { description: 'y' } }] },
        { role: 'tool', call_id: 'm2', name: 'make', text: '{"licensed": true, "installed": "y"}' },
        { role: 'assistant', tool_calls: [{ id: 'm3', name: 'make', arguments: { description: 'y' } }] },
        { role: 'tool', call_id: 'm3', name: 'make', ok: false, text: '{"error": "one_build_per_turn", "detail": "A build already ran in this turn."}' },
        { role: 'assistant', text: 'Built y once.' },
      ]);
      expect(works(t).map((w) => w.steps.map(buildTitle))).toEqual([['Planned the agent'], ['Built the agent', 'Skipped a second build']]);
      expect(works(t).map(workLabel)).toEqual(['Planned the agent', 'Used 2 tools']);
    });
  });

  it('keeps asks in line and records the answer', () => {
    let t = run([
      { type: 'turn_started', text: 'go' },
      { type: 'ask', ask: { id: 3, kind: 'approval', action: 'bash', detail: 'ls' } },
    ]);
    expect(t.items[1]).toMatchObject({ kind: 'ask', closed: false });
    t = fold(t, { type: 'answered', id: 3, value: 'session' });
    t = fold(t, { type: 'ask_closed', id: 3 });
    expect(t.items[1]).toMatchObject({ kind: 'ask', closed: true, answer: 'session' });
  });

  it('keeps the answer on a replayed card, from the bridge, never a secret', () => {
    // A page that re-attaches never saw its own echo: ask_closed carries the answer.
    const t = run([
      { type: 'turn_started', text: '/make @specs/receipt-reader.spec.json' },
      { type: 'ask', ask: { id: 2, kind: 'question', question: 'Compile this agent?', default: 'yes', choices: [] } },
      { type: 'ask_closed', id: 2, answer: '' },
      { type: 'ask', ask: { id: 3, kind: 'secret', question: 'Paste your key' } },
      { type: 'ask_closed', id: 3, answer: 'sk-should-never-come' },
      { type: 'ask', ask: { id: 4, kind: 'approval', action: 'bash', detail: 'ls' } },
      { type: 'ask_closed', id: 4 },
    ]);
    const asks = t.items.filter((i) => i.kind === 'ask') as Array<Extract<Item, { kind: 'ask' }>>;
    expect(asks.map((a) => [a.closed, a.answer])).toEqual([[true, ''], [true, undefined], [true, undefined]]);
  });

  it('shows command output blocks and notices', () => {
    const t = run([
      ev({ kind: 'rows', title: 'Status', rows: [['model', 'fake']] }),
      ev({ kind: 'notice', text: 'careful', level: 'warn' }),
      ev({ kind: 'diff', text: 'diff --git a/x b/x\n+1' }),
      ev({ kind: 'plan_card', title: 'Plan: x', rows: [['floor', '0.9']], footer: 'spec abc' }),
    ]);
    expect(t.items.map((i) => i.kind)).toEqual(['rows', 'notice', 'diff', 'plan']);
  });

  it('never mutates the timeline it was given', () => {
    const before = run([{ type: 'turn_started', text: 'a' }, ev({ kind: 'tool_started', call_id: 'c', name: 'grep', summary: 'x' })]);
    const snapshot = JSON.stringify(before);
    fold(before, ev({ kind: 'tool_finished', call_id: 'c', name: 'grep', ok: true, summary: 'y' }));
    expect(JSON.stringify(before)).toBe(snapshot);
  });
});

describe('fromHistory', () => {
  it('rebuilds a saved conversation', () => {
    const t = fromHistory([
      { role: 'user', text: 'hi\n\n<file path="a">x</file>' },
      { role: 'assistant', tool_calls: [{ id: 'c1', name: 'read_file', arguments: { path: 'a.py' } }] },
      { role: 'tool', call_id: 'c1', name: 'read_file', text: 'contents' },
      { role: 'assistant', text: 'Done.' },
    ]);
    expect(t.items.map((i) => i.kind)).toEqual(['user', 'work', 'answer']);
    expect(t.items[0]).toMatchObject({ text: 'hi' });
  });
});

describe('patches', () => {
  it('ends a hunk at text after the patch', async () => {
    const { parsePatch } = await import('../src/lib/patch');
    const text = 'diff --git a/x.ts b/x.ts\n--- a/x.ts\n+++ b/x.ts\n@@ -1,1 +1,1 @@\n-a\n+b\n\nUntracked files:\n  y.ts\n';
    const [file] = parsePatch(text);
    expect(file.hunks[0].lines).toEqual(['-a', '+b']);
  });
});
