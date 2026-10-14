/**
 * W6 review: is the bar view drawn from the right sources, and does it agree with the
 * rest of the panel? Each `it.fails` pins a defect found in review (it passes while the
 * defect is there); turn it back into `it` once fixed. Fixed so far: practice dots are
 * whole-partition scores only, an unscored exam reads "unscored", and the stem of an exam
 * the preset sat starts at the preset.
 *
 * Shapes are the bridge's: forge_measure and forge_submit results carry
 * `measurement.sample_count` (compiler/forge_tools.py `_measurement_view`). forge_measure
 * runs on at most 32 practice cases (8 by default, ForgeSettings.measure_cases);
 * forge_submit runs on the whole practice partition, as Stage 2's best preset (the grey
 * tick) does, and as the bar is meant.
 */
import { describe, expect, it } from 'vitest';
import { emptyFeed, feedMessage, forgeModel, ladderView, type ForgeFeed } from '../src/lib/forge';
import { diffLines } from '../src/lib/linediff';
import { emptyTimeline, fold, type Timeline } from '../src/lib/timeline';
import { layoutBarView } from '../src/lib/viz';
import { FORGING, LADDER, PLAN, SHIPPED, SPEC } from './fixtures/ladderBuild';

const ev = (event: Record<string, unknown>) => ({ type: 'event', event });
const forge = (data: Record<string, unknown>) => ({ type: 'forge', data });
const where = (index: number) => ({ depth: 0, model: LADDER[index], model_index: index + 1, model_count: 3, ladder: LADDER });
const stage = (index: number, n: number, extra: Record<string, unknown> = {}) => forge({ kind: 'stage', stage: n, stages: [1, 2, 3, 4].filter((s) => s <= n), ...where(index), ...extra });
const tool = (name: string, result: Record<string, unknown>) => forge({ kind: 'tool', tool: name, depth: 0, result });
const measurement = (score: number, sampleCount: number) => ({ score, floor_limit: 0.4, floor_passed: score >= 0.4, sample_count: sampleCount });

function play(...parts: Record<string, unknown>[][]): { timeline: Timeline; feed: ForgeFeed } {
  let timeline = emptyTimeline();
  let feed = emptyFeed();
  parts.flat().forEach((msg, i) => {
    feed = feedMessage(feed, msg);
    timeline = fold(timeline, msg, 1000 + i * 1000);
  });
  return { timeline, feed };
}

const view = (...parts: Record<string, unknown>[][]) => {
  const { timeline, feed } = play(...parts);
  return ladderView(forgeModel(timeline), feed)!;
};

/** Lightning's Stages 1 to 3 (preset 0.31 in practice), then the given Forge tool results. */
const lightning = (...tools: Record<string, unknown>[]) => [
  { type: 'answered', id: 2, value: 'yes' },
  { type: 'ask_closed', id: 2 },
  stage(0, 1),
  stage(0, 2),
  stage(0, 3, { tuned: { score: 0.31, passed: false } }),
  tool('forge_session', { mode: 'agent', baseline: { score: 0.31, floor_passed: false } }),
  ...tools,
];

describe('the bar view\'s practice dots', () => {
  it('are scores on the whole practice partition, like the grey tick they are drawn against', () => {
    // Forge measures a draft on its default 8 practice cases (7 of 8 pass), then submits
    // it, which measures all 100: 0.42. Only the 100-case score compares with the
    // preset's 0.31 and the bar's 0.40.
    const v = view(PLAN, lightning(
      tool('forge_measure', { ok: true, measurement: measurement(0.875, 8) }),
      tool('forge_submit', { ok: true, accepted: true, measurement: measurement(0.42, 100) }),
    ));
    expect(v.rungs[0].preset).toBe(0.31);
    expect(v.rungs[0].practice).toEqual([0.42]);
  });
});

describe('the stem from practice to the exam ring', () => {
  /** Forge's submissions were refused, so Stage 2's preset (practice 0.31) sat the exam. */
  const presetSat = [
    ...lightning(
      tool('forge_submit', { ok: false, code: 'tuning_failed', accepted: false, measurement: measurement(0.31, 100) }),
      tool('forge_submit', { ok: false, code: 'tuning_failed', accepted: false, measurement: measurement(0.36, 100) }),
    ),
    stage(0, 4, { tuned: { score: 0.31, passed: false }, examined: { score: 0.31, passed: false } }),
    stage(1, 1),
  ];
  const refused = ev({
    kind: 'plan_card', title: 'Result: not licensed', rows: [['floor', '0.29/0.40']], footer: '',
    data: {
      spec_hash: SPEC, licensed: false, bar: 0.4, kind: 'held_out_tasks', model: LADDER[0], score: 0.29,
      exam: { cases: 100, correct: 29, invalid: 0, over_budget: 0 },
      rungs: [{ model: LADDER[0], preset: 'extractor', licensed: false, ships: false, score: 0.29, limit: 0.4, cases: 100, counts: { cases: 100, correct: 29, invalid: 0, over_budget: 0 }, codes: ['primary_floor_failed', 'tuning_failed'] }],
    },
  });

  it('starts at the preset, not at a Forge dot, when the preset sat the exam', () => {
    const v = view(PLAN, presetSat, [refused, { type: 'turn_finished', text: '' }]);
    const row = layoutBarView({ bar: v.bar, domain: v.domain, rungs: v.rungs }, { width: 578 }).rows[0];
    expect(row.ring).not.toBeNull();
    // Drawn as the preset's, from the preset's tick at 0.31, and the refused submission
    // that also scored 0.31 is not the dot that "sat the exam".
    expect(row.link).toMatchObject({ from: 'preset', practice: 0.31 });
    expect(Math.abs(row.link!.x1 - row.tick!.x)).toBeLessThan(1);
    expect(row.dots.filter((dot) => dot.best)).toEqual([]);
  });
});

describe('a licensed build that was not installed', () => {
  // build.py `_result`: licensed, but the installed version is kept ("not_better"); the
  // card title says "(not installed)" and its data says licensed.
  const notInstalled = SHIPPED.map((msg) => {
    const event = (msg as { event?: Record<string, unknown> }).event;
    if (event?.kind !== 'plan_card') return msg;
    return { ...msg, event: { ...event, title: 'Result: receipt-reader (not installed)' } };
  });

  it.fails('is not shown as refused beside a licensed exam chart', () => {
    const { timeline, feed } = play(PLAN, FORGING, notInstalled);
    const model = forgeModel(timeline);
    const v = ladderView(model, feed)!;
    expect(v.exam?.licensed).toBe(true); // the chart's closed lock, the ring's "licensed, ships"
    expect(model.phase).not.toBe('not_licensed'); // today: "Refused, so nothing was installed."
  });
});

describe('a rung whose exam was not scored', () => {
  // runtime/evaluation.py: too many invalid outputs (or a budget refusal, or a floor error)
  // leaves the partition unscored: counts.correct is null and the signed floor_score is
  // the 0.0 placeholder. SPEC.md: such a rung shows "unscored", with its counts in the tip.
  const unscored = SHIPPED.map((msg) => {
    const event = (msg as { event?: { kind?: string; data?: { rungs?: Array<Record<string, unknown>> } } }).event;
    if (event?.kind !== 'plan_card' || !event.data?.rungs) return msg;
    const rungs = event.data.rungs.map((r, i) => (i === 0
      ? { ...r, licensed: false, ships: false, score: 0, counts: { cases: 100, correct: null, invalid: 9, over_budget: 0 }, codes: ['primary_floor_failed', 'primary_evaluation_failed', 'primary_invalid_exceeded'] }
      : i === 1 ? { ...r, ships: true } : r));
    return { ...msg, event: { ...event, data: { ...event.data, rungs } } };
  });

  it('shows "unscored", not an exam of 0.00', () => {
    const v = view(PLAN, FORGING, unscored);
    const row = layoutBarView({ bar: v.bar, domain: v.domain, rungs: v.rungs }, { width: 578 }).rows[0];
    // Not a refused ring at 0.00, and the scale stays the plan's.
    expect(row.value?.unscored).toBe(true);
    expect(row.ring).toBeNull();
    expect(v.domain[0]).toBeGreaterThan(0);
  });
});

describe('a build that ran out of compile budget', () => {
  // compiler/__init__.py: BudgetExceeded ends the compile with failures ("compile_budget",)
  // and no rungs, after Lightning already sat the exam; build.py sends `rungs: []`.
  const outOfBudget = [
    ev({
      kind: 'plan_card', title: 'Result: not licensed', rows: [['floor', '- (compile_budget)']], footer: '',
      data: { spec_hash: SPEC, licensed: false, bar: 0.4, kind: 'held_out_tasks', model: null, score: null, exam: null, rungs: [] },
    }),
    { type: 'turn_finished', text: '' },
  ];

  it.fails('does not say a rung that sat the exam had no exam, without a reason', () => {
    const v = view(PLAN, FORGING, outOfBudget);
    const [lightning] = v.rungs;
    // Today every rung is "skipped" with no reason; Lightning's name tip reads "No exam".
    expect(lightning.state === 'skipped' && lightning.reason === undefined).toBe(false);
  });
});

describe('the harness diff', () => {
  /** Lines of `diffLines` that make up each side. */
  const sides = (before: string, after: string) => {
    const lines = diffLines(before, after);
    return {
      old: lines.filter((l) => l.kind !== 'add').map((l) => l.text),
      new: lines.filter((l) => l.kind !== 'del').map((l) => l.text),
      edits: lines.filter((l) => l.kind !== 'ctx').length,
    };
  };
  const lcs = (a: string[], b: string[]) => {
    const t = Array.from({ length: a.length + 1 }, () => new Array<number>(b.length + 1).fill(0));
    for (let i = a.length - 1; i >= 0; i -= 1) for (let j = b.length - 1; j >= 0; j -= 1) t[i][j] = a[i] === b[j] ? t[i + 1][j + 1] + 1 : Math.max(t[i + 1][j], t[i][j + 1]);
    return t[0][0];
  };

  it('rebuilds both versions and makes no more edits than needed', () => {
    let seed = 7;
    const rand = (n: number) => {
      seed = (seed * 1103515245 + 12345) % 2147483648;
      return seed % n;
    };
    for (let round = 0; round < 400; round += 1) {
      const a = Array.from({ length: rand(14) }, () => `line ${rand(5)}`);
      const b = Array.from({ length: rand(14) }, () => `line ${rand(5)}`);
      const before = a.length ? `${a.join('\n')}\n` : '';
      const after = b.length ? `${b.join('\n')}\n` : '';
      const s = sides(before, after);
      expect(s.old).toEqual(a);
      expect(s.new).toEqual(b);
      expect(s.edits).toBe(a.length + b.length - 2 * lcs(a, b));
    }
  });
});
