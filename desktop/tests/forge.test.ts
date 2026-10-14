import { describe, expect, it } from 'vitest';
import { declined, floorLimit, forgeModel, parseStage, scoreAndLimit } from '../src/lib/forge';
import { emptyTimeline, fold, type Timeline } from '../src/lib/timeline';
import { BEFORE_PLAN, BUILDING, DONE } from './fixtures/forgeBuild';

const play = (...parts: Record<string, unknown>[][]): Timeline =>
  parts.flat().reduce((t, m, i) => fold(t, m, 1000 + i * 1000), emptyTimeline());

describe('the Forge panel model', () => {
  it('is empty before anything is asked', () => {
    expect(forgeModel(emptyTimeline()).phase).toBe('empty');
  });

  it('shows the plan and the open approval', () => {
    const model = forgeModel(play(BEFORE_PLAN));
    expect(model.phase).toBe('plan');
    expect(model.name).toBe('invoice-extractor');
    expect(model.plan?.hash).toBe('1a2b3c4d');
    expect(model.approval?.id).toBe(2);
    expect(model.request).toContain('invoice number');
    expect(model.lines.some((l) => l.agent === 'floor')).toBe(true);
  });

  it('follows the stages while building', () => {
    const model = forgeModel(play(BEFORE_PLAN, BUILDING));
    expect(model.phase).toBe('building');
    expect(model.approval).toBeUndefined();
    expect(model.stage).toMatchObject({ stage: 3, calls: 31, spend: '$0.4100' });
    expect(model.stages.map((s) => s.stage)).toEqual([0, 1, 2, 3]);
  });

  it('reads the licensed result', () => {
    const model = forgeModel(play(BEFORE_PLAN, BUILDING, DONE));
    expect(model.phase).toBe('licensed');
    expect(model.result).toMatchObject({ name: 'invoice-extractor', revision: 1, licensed: true });
    expect(scoreAndLimit(model.result?.rows.find(([k]) => k === 'floor')?.[1])).toEqual({ score: 1, limit: 0.9 });
  });

  it('shows a plan answered Not now as set aside, until the next plan replaces it', () => {
    const notNow = [
      { type: 'answered', id: 2, value: 'no' },
      { type: 'ask_closed', id: 2, answer: 'no' },
      { type: 'event', event: { kind: 'tool_finished', call_id: 'c1', name: 'make', ok: true, summary: '{"cancelled": true, "stage": "plan", "spec_hash": "1a2b3c4d"}' } },
      { type: 'event', event: { kind: 'answer', text: 'Set aside. Nothing was built.' } },
      { type: 'turn_finished', text: '' },
    ];
    const model = forgeModel(play(BEFORE_PLAN, notNow));
    expect(model.phase).toBe('set_aside');
    expect(model.name).toBe('invoice-extractor');
    expect(model.approval).toBeUndefined();
    // A reopened conversation has only the bridge's word for the answer.
    expect(forgeModel(play(BEFORE_PLAN, notNow.slice(1))).phase).toBe('set_aside');
    // A question the person never answered (the turn was stopped) built nothing either.
    expect(forgeModel(play(BEFORE_PLAN, [{ type: 'ask_closed', id: 2, answer: null }, { type: 'turn_finished', text: '' }])).phase).toBe('set_aside');
    // The next /make starts a plan of its own.
    const next = [
      { type: 'turn_started', text: '/make @receipt-reader.json' },
      { type: 'event', event: { kind: 'plan_card', title: 'Plan: receipt-reader', rows: [['floor', "held_out_tasks >= 0.40 on 'receipts'"]], footer: 'spec 9f8e7d6c.' } },
      { type: 'ask', ask: { id: 3, kind: 'question', question: 'Compile this agent?', default: 'yes', choices: [] } },
    ];
    const replaced = forgeModel(play(BEFORE_PLAN, notNow, next));
    expect(replaced.phase).toBe('plan');
    expect(replaced.name).toBe('receipt-reader');
    expect(replaced.approval?.id).toBe(3);
  });

  it('reads Enter and yes on the plan as building, as build mode does', () => {
    expect(declined('')).toBe(false); // Enter takes the default, yes
    expect(declined(' Yes ')).toBe(false);
    expect(declined('y')).toBe(false);
    expect(declined('no')).toBe(true);
    expect(declined('later')).toBe(true);
    expect(declined(null)).toBe(true); // cancelled
    expect(declined(undefined)).toBe(false); // not known
    const enter = forgeModel(play(BEFORE_PLAN, [{ type: 'ask_closed', id: 2, answer: '' }]));
    expect(enter.phase).toBe('plan');
    expect(forgeModel(play(BEFORE_PLAN, [{ type: 'ask_closed', id: 2, answer: '' }], BUILDING.slice(2))).phase).toBe('building');
  });

  it('reads a failed result as not licensed', () => {
    const failed = DONE.map((m) => {
      const event = (m as { event?: { kind?: string; title?: string } }).event;
      return event?.kind === 'plan_card' ? { type: 'event', event: { ...event, title: 'Result: not licensed', rows: [['next', 'add real cases']] } } : m;
    });
    expect(forgeModel(play(BEFORE_PLAN, BUILDING, failed)).phase).toBe('not_licensed');
  });

  it('parses stage lines and floors', () => {
    expect(parseStage('stage 2 tune: 14 calls, $0.03')).toMatchObject({ stage: 2, name: 'tune', calls: 14 });
    // The heartbeat's repeat of a line adds the time in the stage after the spend.
    expect(parseStage('stage 2 tune: 28 calls, $0.0039 so far, 2.5 min in this stage (nvidia/nemotron-3-super-120b-a12b, rung 2 of 2)'))
      .toMatchObject({ stage: 2, name: 'tune', calls: 28, spend: '$0.0039' });
    expect(parseStage('stage 0 infer spec: one call to m ...')).toMatchObject({ stage: 0, name: 'infer spec' });
    expect(parseStage('floor: drafting')).toBeNull();
    expect(floorLimit("held_out_tasks >= 0.90 on 'x'")).toBe(0.9);
  });
});

describe('plain words', () => {
  it('turns Forge tool names and progress into sentences', async () => {
    const { friendlyLine, friendlyProgress } = await import('../src/lib/forge');
    expect(friendlyLine('forge_measure')).toBe('Measured it on the practice cases');
    expect(friendlyLine('forge_submit: accepted')).toBe('Submitted a candidate: accepted');
    expect(friendlyLine('edit src/a.py: ok')).toBe('edit src/a.py: ok');
    expect(friendlyProgress('stage 2 tune: 3 calls, $0.01')).toBe('Practicing on the cases');
    expect(friendlyProgress('floor: drafting 12 cases with m ...')).toBe('Drafting the practice and exam cases');
    expect(friendlyLine("'invoices' configured: 12 cases drafted, SYNTHETIC (6 tuning / 6 held-out)")).toBe('12 cases drafted, synthetic, 6 for practice and 6 for the exam');
    expect(friendlyLine('running v2 on nvidia/nemotron-3-super-120b-a12b')).toBe('running v2 on Nemotron 3 Super');
  });
});
