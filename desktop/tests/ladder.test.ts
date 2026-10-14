/** The bar view's data: rungs from the plan, the live feed and, after the compile, the result card. */
import { describe, expect, it } from 'vitest';
import { emptyFeed, feedMessage, forgeModel, ladderView, type ForgeFeed } from '../src/lib/forge';
import { layoutBarView } from '../src/lib/viz';
import { emptyTimeline, fold, type Timeline } from '../src/lib/timeline';
import { FORGING, HARNESS_1, HARNESS_2, LADDER, PLAN, SHIPPED, SPEC } from './fixtures/ladderBuild';

function play(...parts: Record<string, unknown>[][]): { timeline: Timeline; feed: ForgeFeed } {
  let timeline = emptyTimeline();
  let feed = emptyFeed();
  parts.flat().forEach((msg, i) => {
    feed = feedMessage(feed, msg);
    timeline = fold(timeline, msg, 1000 + i * 1000);
  });
  return { timeline, feed };
}

function ladder(...parts: Record<string, unknown>[][]) {
  const { timeline, feed } = play(...parts);
  return { view: ladderView(forgeModel(timeline), feed), feed, model: forgeModel(timeline) };
}

describe('the ladder before the build', () => {
  it('lists every rung by its tier, waiting, under the plan\'s bar', () => {
    const { view, model } = ladder(PLAN);
    expect(model.plan?.hash).toBe(SPEC);
    expect(view?.bar).toBe(0.4);
    expect(view?.rungs.map((r) => [r.name, r.model, r.state])).toEqual([
      ['Lightning', 'Nemotron 3.5 Lightning', 'pending'],
      ['Super', 'Nemotron 3 Super', 'pending'],
      ['Ultra', 'Nemotron 3 Ultra', 'pending'],
    ]);
    expect(view?.exam).toBeUndefined();
    expect(view?.finished).toBe(false);
  });

  it('takes its scale from the plan, and makes the same from the bar when the plan has none', () => {
    const { view } = ladder(PLAN);
    expect(view?.domain).toEqual([0.2, 0.7]);
    const bare = PLAN.map((msg) => {
      const event = (msg as { event?: { kind?: string; data?: Record<string, unknown> } }).event;
      if (event?.kind !== 'plan_card') return msg;
      const data = { ...event.data };
      delete data.domain;
      return { ...msg, event: { ...event, data } };
    });
    expect(ladder(bare).view?.domain).toEqual([0.2, 0.7]);
  });

  it('keeps that scale while scores fall inside it, and the bar view widens it for one outside', () => {
    expect(ladder(PLAN, FORGING, SHIPPED).view?.domain).toEqual([0.2, 0.7]);
    expect(layoutBarView(ladder(PLAN, FORGING, SHIPPED).view!, { width: 578 }).domain).toEqual([0.2, 0.7]);
    const low = [...FORGING.slice(0, 8), { type: 'forge', data: { kind: 'tool', tool: 'forge_submit', depth: 0, result: { ok: false, accepted: false, measurement: { score: 0.05, sample_count: 100 } } } }];
    const { view } = ladder(PLAN, low);
    expect(view?.domain).toEqual([0.2, 0.7]); // the plan's, as sent
    expect(layoutBarView(view!, { width: 578 }).domain).toEqual([0, 0.7]); // as drawn: 0.05 sits at 0.05
  });
});

describe('the ladder while Forge works', () => {
  it('shows the best preset as the tick, Forge\'s practice scores as dots and the running rung', () => {
    const working = FORGING.slice(0, FORGING.findIndex((m) => (m as { data?: { stage?: number } }).data?.stage === 4));
    const { view } = ladder(PLAN, working);
    const [lightning, superRung, ultra] = view!.rungs;
    // The submissions ran all 100 practice cases; the quick measures on 8 are not dots.
    expect(lightning).toMatchObject({ state: 'running', preset: 0.31, practice: [0.36, 0.48] });
    expect(lightning.examined).toBeUndefined();
    expect(superRung.state).toBe('pending');
    expect(ultra.state).toBe('pending');
  });

  it('keeps a finished rung\'s exam back until the compile returns', () => {
    const { view } = ladder(PLAN, FORGING);
    const [lightning, superRung] = view!.rungs;
    expect(lightning).toMatchObject({ state: 'done', preset: 0.31, examined: 0.48 });
    expect(lightning.exam).toBeUndefined();
    expect(lightning.licensed).toBeUndefined();
    expect(superRung.state).toBe('running');
    expect(view!.exam).toBeUndefined();
  });

  it('keeps each harness version Forge worked on, with its practice scores', () => {
    const { feed } = ladder(PLAN, FORGING);
    expect(feed.practice).toBe(100);
    expect(feed.drafts).toEqual([
      { sha256: 'a1', text: HARNESS_1, tool: 'forge_check', rung: 1, sampled: { score: 0.375, cases: 8 }, submitted: 0.36, accepted: false },
      { sha256: 'b2', text: HARNESS_2, tool: 'forge_measure', rung: 1, sampled: { score: 0.5, cases: 8 }, submitted: 0.48, accepted: true },
    ]);
    // The text lives in the drafts only, not in the tool list.
    expect(JSON.stringify(feed.tools)).not.toContain('context.call');
  });
});

describe('the ladder after the compile', () => {
  it('draws every rung\'s signed exam score and decision, and the shipped rung\'s counts', () => {
    const { view } = ladder(PLAN, FORGING, SHIPPED);
    const [lightning, superRung, ultra] = view!.rungs;
    expect(lightning).toMatchObject({ state: 'done', licensed: true, ships: true, exam: { score: 0.44, counts: { cases: 100, correct: 44, invalid: 1, overBudget: 0 } } });
    expect(superRung).toMatchObject({ state: 'done', licensed: true, ships: false, exam: { score: 0.47 } });
    expect(ultra).toMatchObject({ state: 'done', licensed: false, exam: { score: 0.38 } });
    expect(ultra.reason).toBeUndefined(); // below the bar says it already
    expect(view!.exam).toEqual({ counts: { cases: 100, correct: 44, invalid: 1, overBudget: 0 }, bar: 0.4, licensed: true, name: 'Lightning', model: 'Nemotron 3.5 Lightning', score: 0.44 });
    expect(view!.finished).toBe(true);
  });

  it('says why a rung at the bar was refused, or why one never sat the exam', () => {
    const result = SHIPPED.map((msg) => {
      const event = (msg as { event?: { kind?: string; data?: { rungs?: Array<Record<string, unknown>> } } }).event;
      if (event?.kind !== 'plan_card' || !event.data?.rungs) return msg;
      const rungs = event.data.rungs.map((r, i) => (i === 1 ? { ...r, licensed: false, codes: ['primary_budget_failed'] } : i === 2 ? { ...r, score: null, counts: null, codes: ['selection_failed'] } : r));
      return { ...msg, event: { ...event, data: { ...event.data, rungs } } };
    });
    const { view } = ladder(PLAN, FORGING, result);
    expect(view!.rungs[1]).toMatchObject({ licensed: false, reason: 'over budget on the exam' });
    expect(view!.rungs[2]).toMatchObject({ state: 'skipped', reason: 'no preset fits this model' });
  });

  it('starts a new feed with the next plan, and a replay folds to the same board', () => {
    const live = ladder(PLAN, FORGING, SHIPPED);
    const again = ladder(PLAN, FORGING, SHIPPED);
    expect(again.feed).toEqual(live.feed);
    const card = PLAN.find((m) => (m as { event?: { kind?: string } }).event?.kind === 'plan_card')!;
    const next = feedMessage(live.feed, card);
    expect(next).toEqual({ ...emptyFeed(), practice: 100 });
    expect(LADDER).toHaveLength(3);
  });
});
