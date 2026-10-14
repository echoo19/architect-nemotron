/**
 * Geometry and words for the bar view and the exam chart, ported with types from the
 * evidence site's builders (site/design/components.js, SPEC.md). No DOM and no
 * framework: the React components draw what these return, so the desktop and the site
 * place every mark the same way. Colour lives in styles/viz.css, copied from the site.
 */

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));
const r1 = (v: number) => Math.round(v * 10) / 10;
const crisp = (v: number) => Math.round(v) + 0.5; // a 1px line on a pixel row
export const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);

/** A score as the CLI prints it: 0.86. */
export const fmtScore = (v: unknown): string => (finite(v) ? v.toFixed(2) : '');
/** An axis step: 0.8, 1.0. */
export const fmtStep = (v: number): string => v.toFixed(1);

/**
 * Dollars at the precision the number deserves: $1.37 and $0.43 to the cent, and under
 * ten cents two significant figures, so the cheapest model's cost per run still reads
 * ($0.043, $0.0018, $0.00018). A run that made no model calls costs $0.
 */
export function fmtUsd(v: unknown): string {
  if (!finite(v)) return '';
  if (v === 0) return '$0';
  if (v < 0) return `-${fmtUsd(-v)}`;
  if (v >= 0.1 || Number(v.toPrecision(2)) >= 0.1) return `$${v.toFixed(2)}`;
  if (v < 1e-8) return '<$0.00000001';
  const decimals = Math.min(8, 1 - Math.floor(Math.log10(v)));
  return `$${v.toFixed(decimals).replace(/0+$/, '').replace(/\.$/, '')}`;
}

/** Wall time: 0.4s, 41s, 18m 24s, 1h 02m. */
export function fmtSeconds(s: unknown): string {
  if (!finite(s)) return '';
  if (s < 10) return `${(Math.round(s * 10) / 10).toFixed(1)}s`;
  const t = Math.round(s);
  if (t < 60) return `${t}s`;
  if (t < 3600) return `${Math.floor(t / 60)}m ${String(t % 60).padStart(2, '0')}s`;
  return `${Math.floor(t / 3600)}h ${String(Math.floor((t % 3600) / 60)).padStart(2, '0')}m`;
}

/**
 * A build's bar view scale, set by the bar alone when the plan is made: half a unit on 0.1
 * steps, starting 0.2 below the bar, inside [0, 1]. A bar of 0.40 gives [0.2, 0.7]; 0.80
 * gives [0.5, 1]. The plan card sends the same (`domain`, architect/agent/build.py
 * `bar_domain`); this is for a plan without it. `widenDomain` grows it for a mark outside.
 */
export function barDomain(bar: number): [number, number] {
  if (!finite(bar)) return [0, 1];
  const low = clamp(Math.floor(Math.round((bar - 0.2) * 1e6) / 1e5) / 10, 0, 0.5);
  return [Math.round(low * 10) / 10, Math.round((low + 0.5) * 10) / 10];
}

/** A `[lo, hi]` scale as a plan card sends it, or nothing when it is not one. */
export function domainOf(value: unknown): [number, number] | undefined {
  if (!Array.isArray(value) || value.length !== 2) return undefined;
  const [lo, hi] = value;
  return finite(lo) && finite(hi) && lo >= 0 && hi <= 1 && hi - lo >= 0.1 ? [lo, hi] : undefined;
}

/**
 * A scale grown just enough that every value is on it: an end moves out to the next 0.1
 * step past a value beyond it, never past [0, 1], and an end no value is beyond stays where
 * it was. So the bar keeps its place while the marks fit, and a score of 0.72 on [0.2, 0.7]
 * is drawn at 0.72 on [0.2, 0.8], not at the edge beside a number that says 0.72.
 */
export function widenDomain([lo, hi]: [number, number], values: unknown[], step = 0.1): [number, number] {
  const vals = values.filter(finite);
  if (!vals.length) return [lo, hi];
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const low = min < lo ? Math.max(0, Math.floor(min / step + 1e-9) * step) : lo;
  const high = max > hi ? Math.min(1, Math.ceil(max / step - 1e-9) * step) : hi;
  return [Math.round(low * 100) / 100, Math.round(high * 100) / 100];
}

/**
 * A score domain that ends at 1.0 and starts on a 0.1 step below the lowest value,
 * never narrower than `minSpan` (the site's charts; a build's bar view uses `barDomain`).
 */
export function scoreDomain(values: unknown[], { pad = 0.05, step = 0.1, minSpan = 0.3 } = {}): [number, number] {
  const vals = values.filter(finite);
  if (!vals.length) return [0, 1];
  let lo = Math.floor((Math.min(...vals) - pad) / step + 1e-9) * step;
  lo = clamp(lo, 0, 1 - minSpan);
  return [Math.round(lo * 100) / 100, 1];
}

function ticks([lo, hi]: [number, number], step = 0.1): number[] {
  const out: number[] = [];
  for (let v = lo; v <= hi + 1e-9; v += step) out.push(Math.round(v * 100) / 100);
  return out;
}

/** A signed report's counts: correct is null when the exam was not scored. */
export interface ExamCounts { cases: number; correct: number | null; invalid: number; overBudget: number }

/** One rung's exam as the bar view needs it: the signed score and its counts. */
export interface RungExam { score?: number | null; counts?: ExamCounts | null }

/** Exam score from a signed report's score, else its counts. */
export function examScore(exam: RungExam | undefined): number | undefined {
  if (!exam) return undefined;
  if (finite(exam.score)) return exam.score;
  const c = exam.counts;
  if (c && finite(c.correct) && c.cases > 0) return c.correct / c.cases;
  return undefined;
}

export type RungState = 'done' | 'running' | 'pending' | 'skipped';

export interface Rung {
  /** Stable key: the model id. */
  key: string;
  /** The short name in the name column ("Lightning"). */
  name: string;
  /** The full name for the tip ("Nemotron 3.5 Lightning"). */
  model: string;
  state: RungState;
  /** The best preset's practice score: the grey tick. */
  preset?: number;
  /** Forge's practice measures, oldest first: the dots. */
  practice?: number[];
  /** The practice score of what sat the exam (default: the best dot, else the preset). */
  examined?: number;
  exam?: RungExam;
  licensed?: boolean;
  ships?: boolean;
  /** Why a rung at or above the bar was still refused, or why it never sat the exam. */
  reason?: string;
  /** What one exam case cost on this model, from its signed report's mean (the result card's `mean_cost_usd`). */
  cost?: number;
  /** When this model's first stage reached the app (ms, from the bridge's `at`). */
  startedAt?: number;
  /** When the next model's first stage did, or the build ended, for the last one (ms). */
  endedAt?: number;
}

/** `domain`: the scale to start from (a build's plan sets one); it grows to fit every mark. */
export interface BarData { bar: number; domain?: [number, number]; rungs: Rung[] }

export interface BarRow {
  key: string;
  name: string;
  model: string;
  state: RungState;
  y: number;
  track: { x1: number; x2: number; y: number };
  tick: { x: number; y1: number; y2: number; value: number } | null;
  dots: Array<{ cx: number; cy: number; value: number; index: number; count: number; best: boolean; age: 'old' | 'new' }>;
  link: { x1: number; x2: number; y: number; from: 'forge' | 'preset'; practice: number } | null;
  ring: { cx: number; cy: number; r: number; score: number; exam?: RungExam; licensed: boolean; ships: boolean; reason?: string } | null;
  value: { x: number; y: number; text: string; licensed?: boolean; unscored?: boolean; tip?: string } | null;
  reason?: string;
}

export interface BarLayout {
  width: number;
  height: number;
  domain: [number, number];
  bar: number;
  nameX: number;
  plotX0: number;
  plotX1: number;
  narrow: boolean;
  rule: { x: number; y1: number; y2: number; label: { x: number; y: number } };
  rows: BarRow[];
  axis: Array<{ x: number; y: number; text: string }>;
}

/** A rung whose model has started: it ran, is running, or sat the exam (a rung the build never reached is not one). */
export function startedRung(rung: Rung): boolean {
  return rung.state === 'done' || rung.state === 'running' || (rung.state === 'skipped' && rung.reason !== undefined);
}

export interface BarViewOptions {
  width?: number;
  rowHeight?: number;
  labelWidth?: number;
  /** Which rungs get a row (the card draws a row once its model starts: `startedRung`). Unset, every rung. */
  rows?: (rung: Rung) => boolean;
  /** Room above the first row (the rule's label sits there by default). */
  top?: number;
  /** Room below the last row (the axis by default). */
  bottom?: number;
  /** The value column's width. */
  valueWidth?: number;
  /** The exam ring's radius. */
  ringRadius?: number;
}

/** Bar view geometry (SPEC.md, "Bar view"): one row per rung, the bar as a vertical rule. */
export function layoutBarView(data: BarData, opts: BarViewOptions = {}): BarLayout {
  const width = Math.max(300, opts.width ?? 572);
  const narrow = width < 440;
  const nameX = 0;
  const plotX0 = opts.labelWidth ?? (narrow ? 76 : 86);
  const valueW = opts.valueWidth ?? (narrow ? 40 : 48);
  const plotX1 = width - valueW - 12;
  const top = opts.top ?? 34;
  const rowH = opts.rowHeight ?? 44;
  const rungs = (data.rungs ?? []).filter((r) => (opts.rows ? opts.rows(r) : true));
  const rowsBottom = top + rungs.length * rowH;
  const height = rowsBottom + (opts.bottom ?? 28);
  const bar = data.bar;

  // Every value the view draws: the bar, the ticks and dots, and where an exam's stem
  // starts and its ring sits. A given scale grows to hold them all (`widenDomain`).
  const values: unknown[] = [bar];
  for (const r of rungs) {
    values.push(r.preset, ...(r.practice ?? []));
    if (r.state === 'done' && finite(examScore(r.exam))) values.push(examScore(r.exam), r.examined);
  }
  const domain = data.domain ? widenDomain(data.domain, values) : scoreDomain(values);
  const span = domain[1] - domain[0];
  const x = (v: number) => plotX0 + ((clamp(v, domain[0], domain[1]) - domain[0]) / span) * (plotX1 - plotX0);
  const xb = Math.round(x(bar));

  const rows = rungs.map((r, i): BarRow => {
    const y = top + rowH * (i + 0.5);
    const practice = (r.practice ?? []).filter(finite);
    const score = examScore(r.exam);
    const hasExam = r.state === 'done' && finite(score);
    const examined = finite(r.examined) ? r.examined : practice.length ? Math.max(...practice) : r.preset;
    // The preset sat the exam when no Forge measure did (Forge off, or none of its harnesses
    // accepted): the stem starts at the preset's practice score and is drawn as the preset's,
    // as on the site (site/design/components.js). Otherwise the emphasized dot is the one
    // that sat the exam, else the best so far.
    const fromPreset = finite(r.examined) ? r.examined === r.preset || !practice.includes(r.examined) : !practice.length;
    const bestIndex = fromPreset || !finite(examined) ? -1 : practice.lastIndexOf(examined);
    const half = Math.ceil(practice.length / 2);
    const counts = r.exam?.counts ?? undefined;
    return {
      key: r.key,
      name: r.name,
      model: r.model ?? r.name,
      state: r.state,
      y,
      track: { x1: plotX0, x2: plotX1, y: crisp(y) },
      tick: finite(r.preset) ? { x: Math.round(x(r.preset)), y1: y - 9, y2: y + 9, value: r.preset } : null,
      dots: practice.map((v, j) => ({
        cx: r1(x(v)), cy: y, value: v, index: j, count: practice.length,
        best: j === bestIndex, age: j < practice.length - half ? 'old' : 'new',
      })),
      link: hasExam && finite(examined)
        ? { x1: r1(x(examined)), x2: r1(x(score as number)), y, from: fromPreset ? 'preset' : 'forge', practice: examined }
        : null,
      ring: hasExam
        ? { cx: r1(x(score as number)), cy: y, r: opts.ringRadius ?? 11, score: score as number, exam: r.exam, licensed: !!r.licensed, ships: !!r.ships, reason: r.reason }
        : null,
      value: hasExam
        ? { x: width, y: y + 4.5, text: fmtScore(score), licensed: !!r.licensed }
        : r.state === 'done' && counts
          ? { x: width, y: y + 4.5, text: 'unscored', unscored: true, tip: `Exam not scored: ${counts.invalid} invalid, ${counts.overBudget} over budget, of ${counts.cases}` }
          : null,
      reason: r.reason,
    };
  });

  return {
    width, height, domain, bar, nameX, plotX0, plotX1, narrow,
    rule: { x: xb, y1: top - 8, y2: rowsBottom + 2, label: { x: xb, y: top - 15 } },
    rows,
    // Every 0.1 while the steps have room for their labels; every 0.2 on a narrow card.
    axis: ticks(domain, (plotX1 - plotX0) / (span / 0.1) < 34 ? 0.2 : 0.1).map((v) => ({ x: r1(x(v)), y: rowsBottom + 18, text: fmtStep(v) })),
  };
}

/** One sentence that says what the bar view shows, for its accessible name. */
export function barViewSummary(data: BarData): string {
  const parts = [`Bar ${fmtScore(data.bar)}.`];
  for (const r of data.rungs ?? []) {
    const bits: string[] = [];
    if (finite(r.preset)) bits.push(`best preset practice ${fmtScore(r.preset)}`);
    const p = (r.practice ?? []).filter(finite);
    if (p.length) bits.push(`${p.length} Forge practice ${p.length === 1 ? 'score' : 'scores'} from ${fmtScore(Math.min(...p))} to ${fmtScore(Math.max(...p))}`);
    const s = examScore(r.exam);
    if (r.state === 'done' && finite(s)) bits.push(`exam ${fmtScore(s)}, ${r.licensed ? 'licensed' : 'refused'}${r.ships ? ', ships' : ''}`);
    else if (r.state === 'running') bits.push('working');
    else if (r.state === 'done' && r.exam?.counts) bits.push('exam not scored, refused');
    else if (r.state === 'done') bits.push('exam results come when the build finishes');
    else bits.push('not run');
    parts.push(`${r.name}: ${bits.join('; ')}.`);
  }
  return parts.join(' ');
}

/** The tip a ring shows: "Lightning: exam 0.71 (71/100), refused". */
export function ringTip(name: string, ring: NonNullable<BarRow['ring']>): string {
  const c = ring.exam?.counts;
  const counted = c && finite(c.correct) ? ` (${c.correct}/${c.cases})` : '';
  return `${name}: exam ${fmtScore(ring.score)}${counted}, ${ring.licensed ? 'licensed' : 'refused'}${ring.reason ? ` (${ring.reason})` : ''}${ring.ships ? ', ships' : ''}`;
}

// ---------------------------------------------------------------- exam chart

export type UnitKind = 'correct' | 'missed' | 'invalid' | 'over' | 'unscored';

export interface ExamLayout {
  width: number;
  height: number;
  cell: number;
  gap: number;
  n: number;
  need: number;
  counts: { correct: number; missed: number; invalid: number; overBudget: number; scored: boolean };
  cells: Array<{ kind: UnitKind; cx: number; cy: number }>;
  rule: { d: string; labelY: number } | null;
  label: { x: number; y: number } | null;
}

/**
 * Exam chart geometry: a unit chart, ten to a row, ordered correct, missed, invalid.
 * The counts are all it knows: no per-case exam data leaves verify(), so units are
 * grouped by kind and never say which case was which.
 */
export function layoutExam(report: { counts: ExamCounts; bar: number }, opts: { cell?: number; gap?: number; labelWidth?: number; split?: number } = {}): ExamLayout {
  const d = opts.cell ?? 14;
  const gap = opts.gap ?? 6;
  const pitch = d + gap;
  const cols = 10;
  const n = Math.max(0, Math.floor(report.counts.cases));
  const rows = Math.max(1, Math.ceil(n / cols));
  const scored = finite(report.counts.correct);
  const correct = scored ? clamp(report.counts.correct as number, 0, n) : 0;
  const invalid = clamp(report.counts.invalid ?? 0, 0, n - correct);
  const overBudget = clamp(report.counts.overBudget ?? 0, 0, n - correct - invalid);
  const missed = scored ? n - correct - invalid - overBudget : 0;
  const labelW = opts.labelWidth ?? 70;
  const over = 7;
  const gx = labelW;
  const gy = 3;
  const gridW = cols * pitch - gap;

  // The bar as a count: the first `need` cells must be solid to clear it. `split` widens
  // the gaps the rule runs through, so it never crosses a row of units.
  const need = Math.ceil(report.bar * n - 1e-9);
  const ruled = finite(report.bar) && need > 0 && need <= n;
  const q = Math.floor(need / cols);
  const s = need % cols;
  const split = ruled ? Math.max(0, opts.split ?? 0) : 0;
  const bounds = ruled ? (s === 0 ? [q] : [q, q + 1]) : [];
  const before = (row: number) => bounds.filter((b) => b <= row).length;
  const rowY = (row: number) => gy + row * pitch + split * before(row);
  const gapY = (row: number) => rowY(row) - gap / 2 - (bounds.includes(row) ? split / 2 : 0);
  const gridH = rowY(rows - 1) + d - gy;

  const kinds: Array<[UnitKind, number]> = scored
    ? [['correct', correct], ['missed', missed], ['invalid', invalid], ['over', overBudget]]
    : [['invalid', invalid], ['over', overBudget], ['unscored', n - invalid - overBudget]];
  const cells: ExamLayout['cells'] = [];
  for (const [kind, count] of kinds) {
    for (let k = 0; k < count; k += 1) {
      const i = cells.length;
      cells.push({ kind, cx: r1(gx + (i % cols) * pitch + d / 2), cy: r1(rowY(Math.floor(i / cols)) + d / 2) });
    }
  }

  let rule: ExamLayout['rule'] = null;
  if (ruled) {
    const xL = gx - over;
    const xR = gx + gridW + over;
    if (s === 0) {
      const y = gapY(q);
      rule = { d: `M${xL} ${y}H${xR}`, labelY: y };
    } else {
      const yt = gapY(q);
      const yb = gapY(q + 1);
      const xs = gx + s * pitch - gap / 2;
      rule = { d: `M${xL} ${yb}H${xs}V${yt}H${xR}`, labelY: yb };
    }
  }

  const labelBase = rule ? rule.labelY + 4 : 0;
  return {
    width: gx + gridW + over + 2,
    height: Math.ceil(Math.max(gy + gridH + 4, labelBase + 4)),
    cell: d, gap, n, need,
    counts: { correct, missed, invalid, overBudget, scored },
    cells,
    rule,
    label: rule ? { x: gx - over - 7, y: rule.labelY + 4 } : null,
  };
}

/** A dotted ring: round-capped zero-length dashes spaced around the circle. */
export function dotted(r: number, spacing = 4.4): string {
  const c = 2 * Math.PI * r;
  const k = Math.max(6, Math.round(c / spacing));
  return `0 ${(c / k).toFixed(3)}`;
}

export function examSummary(report: { counts: ExamCounts; bar: number; licensed?: boolean }): string {
  const L = layoutExam(report);
  const c = L.counts;
  if (!c.scored) return `Exam not scored: ${report.counts.invalid} invalid, ${report.counts.overBudget} over budget, of ${L.n}.`;
  const verdict = typeof report.licensed === 'boolean' ? (report.licensed ? ' Licensed.' : ' Refused.') : '';
  return `Exam: ${c.correct} of ${L.n} correct, ${c.missed} missed, ${c.invalid} invalid. Bar ${fmtScore(report.bar)} needs ${L.need}.${verdict}`;
}

// ---------------------------------------------------------------- hash line

/** A hash the way people compare them: lowercase, groups of eight, two lines of four. */
export function hashHalves(hash: string): string[][][] {
  const groups = hash.toLowerCase().match(/.{1,8}/g) ?? [];
  const halves = [groups.slice(0, 4), groups.slice(4)].filter((g) => g.length);
  return halves.map((half) => [half.slice(0, 2), half.slice(2)].filter((q) => q.length));
}
