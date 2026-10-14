/** The bar view and exam chart geometry (site/design/SPEC.md), ported to lib/viz.ts. */
import { describe, expect, it } from 'vitest';
import { barDomain, barViewSummary, domainOf, examSummary, fmtUsd, hashHalves, layoutBarView, layoutExam, scoreDomain, startedRung, widenDomain, type Rung } from '../src/lib/viz';

const rung = (patch: Partial<Rung>): Rung => ({ key: 'm', name: 'Lightning', model: 'Nemotron 3.5 Lightning', state: 'done', ...patch });

describe('the bar view', () => {
  it('draws the bar as one rule and each ring on its side of it', () => {
    const L = layoutBarView({
      bar: 0.8, domain: [0.5, 1],
      rungs: [
        rung({ key: 'a', name: 'Lightning', preset: 0.74, practice: [0.62, 0.79, 0.88], exam: { score: 0.86 }, licensed: true, ships: true }),
        rung({ key: 'b', name: 'Super', preset: 0.74, exam: { score: 0.71 }, licensed: false }),
        rung({ key: 'c', name: 'Ultra', state: 'running' }),
      ],
    }, { width: 578 });
    expect([L.plotX0, L.plotX1]).toEqual([86, 518]);
    expect(L.rule.x).toBe(345); // 86 + 0.6 * 432
    const [a, b, c] = L.rows;
    expect(a.ring!.cx).toBeGreaterThan(L.rule.x);
    expect(a.ring).toMatchObject({ licensed: true, ships: true });
    expect(a.value).toMatchObject({ text: '0.86', licensed: true });
    expect(a.dots.filter((d) => d.best).map((d) => d.value)).toEqual([0.88]);
    expect(a.link).toMatchObject({ from: 'forge' });
    expect(b.ring!.cx).toBeLessThan(L.rule.x);
    expect(b.link).toMatchObject({ from: 'preset', practice: 0.74 });
    expect(c.ring).toBeNull();
    expect(c.value).toBeNull();
    expect(L.height).toBe(34 + 3 * 44 + 28);
    expect(L.axis.map((t) => t.text)).toEqual(['0.5', '0.6', '0.7', '0.8', '0.9', '1.0']);
  });

  it('narrows its columns on a small card, and widens the scale for a score below it', () => {
    const L = layoutBarView({ bar: 0.8, domain: [0.5, 1], rungs: [rung({ exam: { score: 0.2 } })] }, { width: 400 });
    expect(L.plotX0).toBe(76);
    expect(L.domain).toEqual([0.2, 1]);
    expect(L.rows[0].ring!.cx).toBe(76);
    // A wide scale on a narrow card labels every other step, so the numbers never touch.
    const wide = layoutBarView({ bar: 0.4, domain: [0.2, 1], rungs: [rung({})] }, { width: 330 });
    expect(wide.axis.map((t) => t.text)).toEqual(['0.2', '0.4', '0.6', '0.8', '1.0']);
    const roomy = layoutBarView({ bar: 0.4, domain: [0.2, 1], rungs: [rung({})] }, { width: 578 });
    expect(roomy.axis).toHaveLength(9);
  });

  it('says what it shows in one sentence', () => {
    const text = barViewSummary({ bar: 0.4, rungs: [rung({ preset: 0.31, practice: [0.36, 0.48], exam: { score: 0.44 }, licensed: true, ships: true }), rung({ name: 'Super', state: 'running' })] });
    expect(text).toBe('Bar 0.40. Lightning: best preset practice 0.31; 2 Forge practice scores from 0.36 to 0.48; exam 0.44, licensed, ships. Super: working.');
    const unscored = barViewSummary({ bar: 0.4, rungs: [rung({ exam: { counts: { cases: 100, correct: null, invalid: 9, overBudget: 0 } }, licensed: false })] });
    expect(unscored).toBe('Bar 0.40. Lightning: exam not scored, refused.');
  });

  it("fixes a build's scale from the bar alone", () => {
    expect(barDomain(0.4)).toEqual([0.2, 0.7]);
    expect(barDomain(0.45)).toEqual([0.2, 0.7]);
    expect(barDomain(0.3)).toEqual([0.1, 0.6]);
    expect(barDomain(0.8)).toEqual([0.5, 1]);
    expect(barDomain(1)).toEqual([0.5, 1]);
    expect(barDomain(0.05)).toEqual([0, 0.5]);
    expect(domainOf([0.2, 0.7])).toEqual([0.2, 0.7]);
    expect(domainOf([0.7, 0.2])).toBeUndefined();
    expect(domainOf('0.2,0.7')).toBeUndefined();
  });

  it("widens the plan's scale for a mark beyond it, and only then", () => {
    expect(widenDomain([0.2, 0.7], [0.4, 0.31, 0.65])).toEqual([0.2, 0.7]);
    expect(widenDomain([0.2, 0.7], [0.4, 0.7])).toEqual([0.2, 0.7]);
    expect(widenDomain([0.2, 0.7], [0.4, 0.72])).toEqual([0.2, 0.8]);
    expect(widenDomain([0.2, 0.7], [0.4, 0.78, 0.71])).toEqual([0.2, 0.8]);
    expect(widenDomain([0.2, 0.7], [0.4, 0.15])).toEqual([0.1, 0.7]);
    expect(widenDomain([0.2, 0.7], [0.4, 0.05, 1])).toEqual([0, 1]);
    expect(widenDomain([0.5, 1], [0.8, null, undefined, Number.NaN])).toEqual([0.5, 1]);
  });

  it("draws a score above the plan's scale at its value, not at the edge", () => {
    // The checked ladder's numbers: a bar of 0.40 on [0.2, 0.7], Ultra's practice 0.78 and exam 0.72.
    const data = {
      bar: 0.4, domain: [0.2, 0.7] as [number, number],
      rungs: [
        rung({ key: 'l', name: 'Lightning', preset: 0.48, exam: { score: 0.42 }, licensed: true, ships: true }),
        rung({ key: 'u', name: 'Ultra', preset: 0.78, exam: { score: 0.72 }, licensed: true }),
      ],
    };
    const L = layoutBarView(data, { width: 578 });
    expect(L.domain).toEqual([0.2, 0.8]);
    const at = (v: number) => L.plotX0 + ((v - 0.2) / 0.6) * (L.plotX1 - L.plotX0);
    const ultra = L.rows[1];
    expect(ultra.ring!.cx).toBeCloseTo(at(0.72), 0);
    expect(ultra.ring!.cx).toBeLessThan(L.plotX1);
    expect(ultra.tick!.x).toBe(Math.round(at(0.78)));
    expect(ultra.value!.text).toBe('0.72');
    expect(L.rule.x).toBe(Math.round(at(0.4)));
    expect(L.axis.map((t) => t.text)).toEqual(['0.2', '0.3', '0.4', '0.5', '0.6', '0.7', '0.8']);
    // With every mark inside it, the plan's scale and the bar's place stay as they were.
    const inside = layoutBarView({ ...data, rungs: [data.rungs[0]] }, { width: 578 });
    expect(inside.domain).toEqual([0.2, 0.7]);
    expect(inside.rule.x).toBe(Math.round(L.plotX0 + 0.4 * (L.plotX1 - L.plotX0)));
  });

  it('leaves the scale alone for an exam that was not scored or a rung still working', () => {
    const unscored = layoutBarView({ bar: 0.4, domain: [0.2, 0.7], rungs: [rung({ exam: { counts: { cases: 100, correct: null, invalid: 9, overBudget: 0 } } })] }, { width: 578 });
    expect(unscored.domain).toEqual([0.2, 0.7]);
    expect(unscored.rows[0].value).toMatchObject({ text: 'unscored' });
    const running = layoutBarView({ bar: 0.4, domain: [0.2, 0.7], rungs: [rung({ state: 'running', exam: { score: 0.05 } })] }, { width: 578 });
    expect(running.domain).toEqual([0.2, 0.7]);
  });

  it('draws only the rows a filter keeps: on the card, a model once it starts', () => {
    const data = {
      bar: 0.4, domain: [0.2, 0.7] as [number, number],
      rungs: [
        rung({ key: 'l', name: 'Lightning', state: 'done' }),
        rung({ key: 's', name: 'Super', state: 'running' }),
        rung({ key: 'u', name: 'Ultra', state: 'pending' }),
        rung({ key: 'x', name: 'Extra', state: 'skipped' }),
      ],
    };
    const all = layoutBarView(data, { width: 578 });
    expect(all.rows.map((r) => r.key)).toEqual(['l', 's', 'u', 'x']);
    const started = layoutBarView(data, { width: 578, rows: startedRung, rowHeight: 36, top: 6 });
    expect(started.rows.map((r) => r.key)).toEqual(['l', 's']);
    // The rows close up: the second sits one row below the first, and the view is two rows tall.
    expect(started.rows[1].y - started.rows[0].y).toBe(36);
    expect(started.height).toBe(6 + 2 * 36 + 28);
    // A rung that sat no exam but says why still has its row.
    expect(startedRung(rung({ state: 'skipped', reason: 'too few exam cases' }))).toBe(true);
  });

  it('starts its scale on a step below the lowest value', () => {
    expect(scoreDomain([0.4, 0.31])).toEqual([0.2, 1]);
    expect(scoreDomain([0.95])).toEqual([0.7, 1]);
  });
});

describe('the exam chart', () => {
  it('lays out a signed report\'s counts, the bar as the cell it needs', () => {
    const L = layoutExam({ counts: { cases: 100, correct: 86, invalid: 1, overBudget: 0 }, bar: 0.8 });
    const kinds = L.cells.reduce<Record<string, number>>((n, c) => ({ ...n, [c.kind]: (n[c.kind] ?? 0) + 1 }), {});
    expect(kinds).toEqual({ correct: 86, missed: 13, invalid: 1 });
    expect(L.need).toBe(80);
    expect(L.rule?.d.startsWith('M63 ')).toBe(true);
    expect(examSummary({ counts: { cases: 100, correct: 86, invalid: 1, overBudget: 0 }, bar: 0.8, licensed: true }))
      .toBe('Exam: 86 of 100 correct, 13 missed, 1 invalid. Bar 0.80 needs 80. Licensed.');
  });

  it('draws an unscored report as invalid and quiet units', () => {
    const L = layoutExam({ counts: { cases: 20, correct: null, invalid: 3, overBudget: 0 }, bar: 0.8 });
    expect(L.counts.scored).toBe(false);
    expect(L.cells.filter((c) => c.kind === 'invalid')).toHaveLength(3);
    expect(L.cells.filter((c) => c.kind === 'unscored')).toHaveLength(17);
    expect(L.height).toBeGreaterThan(30);
  });
});

describe('the hash line', () => {
  it('sets a hash in two lines of four groups of eight', () => {
    const halves = hashHalves('3451BD1B13c4558a67a62bc042905ecbf05c03d24dcfb279964970c20b694f22');
    expect(halves).toEqual([[['3451bd1b', '13c4558a'], ['67a62bc0', '42905ecb']], [['f05c03d2', '4dcfb279'], ['964970c2', '0b694f22']]]);
  });
});

describe('money', () => {
  it('keeps two significant figures under ten cents, so the cheapest run still reads', () => {
    expect(fmtUsd(0)).toBe('$0');
    expect(fmtUsd(0.00018)).toBe('$0.00018');
    expect(fmtUsd(0.0002)).toBe('$0.0002');
    expect(fmtUsd(0.0018)).toBe('$0.0018');
    expect(fmtUsd(0.009)).toBe('$0.009');
    expect(fmtUsd(0.043)).toBe('$0.043');
    expect(fmtUsd(0.0999)).toBe('$0.10');
    expect(fmtUsd(0.15)).toBe('$0.15');
    expect(fmtUsd(1.372)).toBe('$1.37');
    expect(fmtUsd(Number.NaN)).toBe('');
  });
});

describe("the stem from practice to the exam ring", () => {
  // site/design/components.js draws the same: what sat the exam decides where the stem
  // starts and whose style it has.
  const at = (r: Partial<Rung>) => layoutBarView({ bar: 0.4, domain: [0.2, 0.7], rungs: [rung(r)] }, { width: 578 }).rows[0];

  it("is the preset's, from its tick, when Forge was off", () => {
    const row = at({ preset: 0.55, exam: { score: 0.51 }, licensed: true });
    expect(row.link).toMatchObject({ from: 'preset', practice: 0.55 });
    expect(Math.abs(row.link!.x1 - row.tick!.x)).toBeLessThan(1);
  });

  it("is the preset's when Forge wrote harnesses and none was accepted", () => {
    // The stage that entered the exam says the preset's 0.31 sat it.
    const row = at({ preset: 0.31, practice: [0.36, 0.1], examined: 0.31, exam: { score: 0.27 }, licensed: false });
    expect(row.link).toMatchObject({ from: 'preset', practice: 0.31 });
    expect(Math.abs(row.link!.x1 - row.tick!.x)).toBeLessThan(1);
    expect(row.dots.some((dot) => dot.best)).toBe(false);
  });

  it("is Forge's, from the dot that sat the exam, when its harness was accepted", () => {
    const row = at({ preset: 0.31, practice: [0.36, 0.48], examined: 0.48, exam: { score: 0.44 }, licensed: true });
    expect(row.link).toMatchObject({ from: 'forge', practice: 0.48 });
    expect(row.dots.filter((dot) => dot.best).map((dot) => dot.value)).toEqual([0.48]);
  });
});

describe('layoutExam split', () => {
  it('opens the gap the pass line runs through, so the line crosses no row of units', () => {
    const counts = { cases: 100, correct: 64, invalid: 0, overBudget: 0 };
    const tight = layoutExam({ counts, bar: 0.4 }, { cell: 12, gap: 3, labelWidth: 0 });
    const open = layoutExam({ counts, bar: 0.4 }, { cell: 12, gap: 3, labelWidth: 0, split: 8 });
    const y = open.rule!.labelY;
    // Rows 1 to 4 sit above the line and rows 5 on below it, each a whole cell clear.
    expect(open.cells.filter((c) => c.cy < y).every((c) => c.cy + 6 < y - 4)).toBe(true);
    expect(open.cells.filter((c) => c.cy > y).every((c) => c.cy - 6 > y + 4)).toBe(true);
    expect(open.cells.filter((c) => c.cy < y)).toHaveLength(40);
    expect(open.height).toBe(tight.height + 8);
    // Without a split the layout is as before.
    expect(layoutExam({ counts, bar: 0.4 }, { cell: 12, gap: 3, labelWidth: 0, split: 0 })).toEqual(tight);
  });
});
