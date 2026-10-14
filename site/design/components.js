/*
 * Architect's evidence visuals: the bar view, the exam chart, the gate scatter,
 * the receipt card and the hash line.
 *
 * Framework-free and DOM-free. Every builder takes a plain data object and
 * returns a markup string, so the same file renders in a browser, in Node for a
 * static site build, and in tests. Geometry lives in the layout*() functions;
 * the desktop's React port imports those unchanged and rewrites only the markup
 * as JSX (see SPEC.md, "Porting to the desktop").
 *
 * No colour is written here. Every mark carries a class from viz.css, which maps
 * the classes onto Lectern's tokens (desktop/src/styles/tokens.css) for light and
 * dark. The grammar is the same in every visual:
 *   a pine rule is the bar; solid passed, outlined failed, dotted invalid;
 *   a closed lock is licensed, an open lock is refused.
 *
 * Icons are Tabler Icons outline paths (MIT), the set the desktop already ships,
 * so a port can swap in <IconLock /> and <IconLockOpen /> with no visual change.
 */

// ---------------------------------------------------------------- text and numbers

const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
/** Escape any string that came from data before it enters markup. */
export const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ESC[c]);

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const r1 = (v) => Math.round(v * 10) / 10;
const crisp = (v) => Math.round(v) + 0.5; // a 1px line on a pixel row
const finite = (v) => typeof v === 'number' && Number.isFinite(v);

let seq = 0;
const uid = (prefix) => `${prefix}-${(seq += 1).toString(36)}`;

/** A score as the CLI prints it: 0.86. */
export const fmtScore = (v) => (finite(v) ? v.toFixed(2) : '');
/** An axis step: 0.8, 1.0. */
export const fmtStep = (v) => v.toFixed(1);
/** A count with thousands separators: 1,412,880. */
export const fmtInt = (n) => (finite(n) ? Math.round(n).toLocaleString('en-US') : '');
/** Tokens, compact: 980, 23.5K, 1.41M. */
export function fmtTokens(n) {
  if (!finite(n)) return '';
  if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e7 ? 1 : 2)}M`;
  if (n >= 1e4) return `${(n / 1e3).toFixed(1)}K`;
  return fmtInt(n);
}
/**
 * Dollars at the precision the number deserves: $1.37 and $0.43 to the cent, and under
 * ten cents two significant figures, so the cheapest model's cost still reads ($0.009,
 * $0.0018, $0.00018).
 */
export function fmtUsd(v) {
  if (!finite(v)) return '';
  if (v === 0) return '$0';
  if (v < 0) return `-${fmtUsd(-v)}`;
  if (v >= 0.1 || Number(v.toPrecision(2)) >= 0.1) return `$${v.toFixed(2)}`;
  if (v < 1e-8) return '<$0.00000001';
  const decimals = Math.min(8, 1 - Math.floor(Math.log10(v)));
  return `$${v.toFixed(decimals).replace(/0+$/, '').replace(/\.$/, '')}`;
}
/** Wall time: 41s, 18m 24s, 1h 02m. */
export function fmtSeconds(s) {
  if (!finite(s)) return '';
  const t = Math.round(s);
  if (t < 60) return `${t}s`;
  if (t < 3600) return `${Math.floor(t / 60)}m ${String(t % 60).padStart(2, '0')}s`;
  return `${Math.floor(t / 3600)}h ${String(Math.floor((t % 3600) / 60)).padStart(2, '0')}m`;
}
/** A UTC timestamp the way the take ledger writes it: 2026-10-18 15:42 UTC. */
export function fmtDate(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return esc(iso);
  return `${d.toISOString().slice(0, 10)} ${d.toISOString().slice(11, 16)} UTC`;
}

// ---------------------------------------------------------------- icons

/** Tabler Icons outline (MIT), 24-unit grid, stroke in grid units like the React package. */
export const ICON_PATHS = {
  lock: ['M5 13a2 2 0 0 1 2 -2h10a2 2 0 0 1 2 2v6a2 2 0 0 1 -2 2h-10a2 2 0 0 1 -2 -2v-6', 'M11 16a1 1 0 1 0 2 0a1 1 0 0 0 -2 0', 'M8 11v-4a4 4 0 1 1 8 0v4'],
  lockOpen: ['M5 13a2 2 0 0 1 2 -2h10a2 2 0 0 1 2 2v6a2 2 0 0 1 -2 2h-10a2 2 0 0 1 -2 -2l0 -6', 'M11 16a1 1 0 1 0 2 0a1 1 0 1 0 -2 0', 'M8 11v-5a4 4 0 0 1 8 0'],
  copy: ['M7 9.667a2.667 2.667 0 0 1 2.667 -2.667h8.666a2.667 2.667 0 0 1 2.667 2.667v8.666a2.667 2.667 0 0 1 -2.667 2.667h-8.666a2.667 2.667 0 0 1 -2.667 -2.667l0 -8.666', 'M4.012 16.737a2.005 2.005 0 0 1 -1.012 -1.737v-10c0 -1.1 .9 -2 2 -2h10c.75 0 1.158 .385 1.5 1'],
  circleCheck: ['M3 12a9 9 0 1 0 18 0a9 9 0 1 0 -18 0', 'M9 12l2 2l4 -4'],
  circleX: ['M3 12a9 9 0 1 0 18 0a9 9 0 1 0 -18 0', 'M10 10l4 4m0 -4l-4 4'],
  alertTriangle: ['M12 9v4', 'M10.363 3.591l-8.106 13.534a1.914 1.914 0 0 0 1.636 2.871h16.214a1.914 1.914 0 0 0 1.636 -2.87l-8.106 -13.536a1.914 1.914 0 0 0 -3.274 0', 'M12 16h.01'],
  sun: ['M8 12a4 4 0 1 0 8 0a4 4 0 1 0 -8 0', 'M3 12h1m8 -9v1m8 8h1m-9 8v1m-6.4 -15.4l.7 .7m12.1 -.7l-.7 .7m0 11.4l.7 .7m-12.1 -.7l-.7 .7'],
  moon: ['M12 3c.132 0 .263 0 .393 0a7.5 7.5 0 0 0 7.92 12.446a9 9 0 1 1 -8.313 -12.454l0 .008'],
  play: ['M7 4v16l13 -8z'],
};

const iconPaths = (name) => (ICON_PATHS[name] ?? []).map((d) => `<path d="${d}"/>`).join('');

/** A standalone icon, sized like <IconX size={size} stroke={stroke} />. */
export function iconSvg(name, { size = 16, stroke = 2, cls = '' } = {}) {
  return `<svg class="viz-icon ${cls}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${stroke}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${iconPaths(name)}</svg>`;
}

/** The same icon placed inside a chart at (cx, cy), drawn `size` px wide. */
function iconAt(name, cx, cy, size, stroke, cls) {
  const s = size / 24;
  return `<g class="${cls}" transform="translate(${r1(cx - size / 2)} ${r1(cy - size / 2)}) scale(${s.toFixed(4)})" fill="none" stroke-width="${stroke}" stroke-linecap="round" stroke-linejoin="round">${iconPaths(name)}</g>`;
}

/**
 * Lectern's working mark (desktop/src/components/ui/Working.tsx). The one loop allowed.
 * Board, foot, then the middle column last, so the bars' clipped ends sit under solid ink.
 * The fourth path, the one outline, shows only under reduced motion (motion.css).
 */
function workingAt(x, y, height) {
  const w = (height * 50) / 66;
  return `<svg class="viz-working lectern-working" x="${r1(x)}" y="${r1(y)}" width="${r1(w)}" height="${height}" viewBox="25 16 50 66" fill="currentColor" aria-hidden="true"><path class="lectern-working-board" d="M25 23 L57 16 L57 29 L25 36 Z"/><path class="lectern-working-foot" d="M43 69 L75 62 L75 75 L43 82 Z"/><path class="lectern-working-column" d="M43 19.0625 L57 16 L57 78.9375 L43 82 Z"/><path class="lectern-working-still" d="M25 23 L57 16 L57 65.9375 L75 62 L75 75 L43 82 L43 32.0625 L25 36 Z"/></svg>`;
}

// ---------------------------------------------------------------- shared scale

/**
 * A build's bar view scale, fixed by the bar alone so nothing slides as scores
 * arrive: half a unit on 0.1 steps, starting 0.2 below the bar, inside [0, 1].
 * A bar of 0.40 gives [0.2, 0.7]; 0.80 gives [0.5, 1].
 */
export function barDomain(bar) {
  if (!finite(bar)) return [0, 1];
  const low = clamp(Math.floor(Math.round((bar - 0.2) * 1e6) / 1e5) / 10, 0, 0.5);
  return [Math.round(low * 10) / 10, Math.round((low + 0.5) * 10) / 10];
}

/**
 * A score domain that ends at 1.0 and starts on a 0.1 step below the lowest
 * value, never narrower than `minSpan`, for charts with no bar to fix them. Pass
 * one fixed domain to every chart on a screen so positions compare across builds;
 * a build's bar view uses barDomain(bar).
 */
export function scoreDomain(values, { pad = 0.05, step = 0.1, minSpan = 0.3 } = {}) {
  const vals = values.filter(finite);
  if (!vals.length) return [0, 1];
  let lo = Math.floor((Math.min(...vals) - pad) / step + 1e-9) * step;
  lo = clamp(lo, 0, 1 - minSpan);
  return [Math.round(lo * 100) / 100, 1];
}

function ticks([lo, hi], step = 0.1) {
  const out = [];
  for (let v = lo; v <= hi + 1e-9; v += step) out.push(Math.round(v * 100) / 100);
  return out;
}

/** Exam score from a signed report's counts, or a bare score. */
export function examScore(exam) {
  if (!exam) return undefined;
  if (finite(exam.score)) return exam.score;
  if (finite(exam.correct) && finite(exam.cases) && exam.cases > 0) return exam.correct / exam.cases;
  return undefined;
}

// ---------------------------------------------------------------- bar view

/**
 * Bar view geometry.
 * data = {
 *   bar: 0.80, domain?: [0.5, 1],
 *   rungs: [{
 *     name: 'Lightning', model?: 'Nemotron 3.5 Lightning',
 *     state: 'done' | 'running' | 'pending' | 'skipped',
 *     preset?: 0.74,             // best preset's practice score: the grey tick
 *     practice?: [0.58, ...],    // the practice scores of Forge's submissions, oldest first: the dots
 *     examined?: 0.88,           // practice score of what sat the exam (default: max dot, else preset)
 *     exam?: { correct, cases, invalid } | { score },
 *     licensed?: boolean, ships?: boolean,
 *     reason?: 'over budget',    // why a rung at or above the bar was still refused
 *   }],
 * }
 */
export function layoutBarView(data, opts = {}) {
  const width = Math.max(300, opts.width ?? 572);
  const narrow = width < 440;
  const nameX = 0;
  const plotX0 = opts.labelWidth ?? (narrow ? 76 : 86);
  const valueW = narrow ? 40 : 48;
  const plotX1 = width - valueW - 12;
  const top = 34;
  const rowH = opts.rowHeight ?? 44;
  const rungs = data.rungs ?? [];
  const rowsBottom = top + rungs.length * rowH;
  const height = rowsBottom + 28;
  const bar = data.bar;

  const values = [bar];
  for (const r of rungs) values.push(r.preset, ...(r.practice ?? []), examScore(r.exam));
  const domain = data.domain ?? scoreDomain(values);
  const span = domain[1] - domain[0];
  const x = (v) => plotX0 + ((clamp(v, domain[0], domain[1]) - domain[0]) / span) * (plotX1 - plotX0);
  const xb = Math.round(x(bar));

  const rows = rungs.map((r, i) => {
    const y = top + rowH * (i + 0.5);
    const practice = (r.practice ?? []).filter(finite);
    const score = examScore(r.exam);
    const hasExam = r.state === 'done' && finite(score);
    const examined = finite(r.examined) ? r.examined : practice.length ? Math.max(...practice) : r.preset;
    // The preset sat the exam when no Forge measure did: Forge wrote harnesses, none was accepted.
    const fromPreset = finite(r.examined) ? r.examined === r.preset || !practice.includes(r.examined) : !practice.length;
    const bestIndex = fromPreset ? -1 : practice.lastIndexOf(examined);
    const half = Math.ceil(practice.length / 2);
    return {
      key: r.name,
      name: r.name,
      model: r.model ?? r.name,
      state: r.state ?? (hasExam ? 'done' : 'pending'),
      y,
      track: { x1: plotX0, x2: plotX1, y: crisp(y) },
      tick: finite(r.preset) ? { x: Math.round(x(r.preset)), y1: y - 9, y2: y + 9, value: r.preset } : null,
      dots: practice.map((v, j) => ({
        cx: r1(x(v)), cy: y, value: v, index: j, count: practice.length,
        best: j === bestIndex, age: j < practice.length - half ? 'old' : 'new',
      })),
      link: hasExam && finite(examined)
        ? { x1: r1(x(examined)), x2: r1(x(score)), y, from: fromPreset ? 'preset' : 'forge', practice: examined }
        : null,
      ring: hasExam ? { cx: r1(x(score)), cy: y, r: 11, score, exam: r.exam, licensed: !!r.licensed, ships: !!r.ships, reason: r.reason } : null,
      value: hasExam
        ? { x: width, y: y + 4.5, text: fmtScore(score), licensed: !!r.licensed }
        : r.state === 'done' && r.exam ? { x: width, y: y + 4.5, text: 'unscored', unscored: true, tip: `Exam not scored: ${r.exam.invalid ?? 0} invalid, ${r.exam.overBudget ?? 0} over budget, of ${r.exam.cases ?? '?'}` } : null,
    };
  });

  return {
    width, height, domain, bar, nameX, plotX0, plotX1,
    rule: { x: xb, y1: top - 8, y2: rowsBottom + 2, label: { x: xb, y: top - 15 } },
    rows,
    axis: ticks(domain).map((v) => ({ x: r1(x(v)), y: rowsBottom + 18, text: fmtStep(v) })),
  };
}

function ringMarkup(ring, row) {
  const verdict = ring.licensed ? 'passed' : 'did not pass';
  const exam = ring.exam && finite(ring.exam.correct) ? ` (${ring.exam.correct}/${ring.exam.cases})` : '';
  const tip = `${row.name}: exam ${fmtScore(ring.score)}${exam}, ${verdict}${ring.reason ? ` (${ring.reason})` : ''}${ring.ships ? ', ships' : ''}`;
  return `<g class="viz-ring ${ring.licensed ? 'is-licensed' : 'is-refused'}${ring.ships ? ' is-ships' : ''}" transform="translate(${ring.cx} ${ring.cy})" tabindex="0" role="img" aria-label="${esc(tip)}" data-tip="${esc(tip)}">`
    + `<circle class="viz-hit" r="17"/><circle class="viz-halo" r="${ring.r + 2.75}"/><circle class="viz-ring-shape" r="${ring.r}"/>`
    + iconAt(ring.licensed ? 'lock' : 'lockOpen', 0, 0, 14, 2.1, 'viz-lock')
    + '</g>';
}

/** One sentence that says what the bar view shows, for its accessible name. */
export function barViewSummary(data) {
  const parts = [`Pass mark ${fmtScore(data.bar)}.`];
  for (const r of data.rungs ?? []) {
    const bits = [];
    if (finite(r.preset)) bits.push(`best preset practice ${fmtScore(r.preset)}`);
    const p = (r.practice ?? []).filter(finite);
    if (p.length) {
      const lo = fmtScore(Math.min(...p));
      const hi = fmtScore(Math.max(...p));
      bits.push(`${p.length} Forge ${p.length === 1 ? 'submission' : 'submissions'}, practice ${lo === hi ? lo : `${lo} to ${hi}`}`);
    }
    const s = examScore(r.exam);
    if (r.state === 'done' && finite(s)) bits.push(`exam ${fmtScore(s)}, ${r.licensed ? 'passed' : 'did not pass'}${r.ships ? ', ships' : ''}`);
    else if (r.state === 'running') bits.push('exam running');
    else bits.push('not run');
    parts.push(`${r.name}: ${bits.join('; ')}.`);
  }
  return parts.join(' ');
}

/**
 * The bar view: one row per rung, a pine rule at the bar, the best preset's
 * practice score as a grey tick, Forge's submissions' practice scores as pine dots (older
 * ones lighter), and each rung's exam score as a ring with a lock.
 * opts: { width, legend = true, enter = [] } where enter lists mark keys that just
 * arrived ('Lightning:ring', 'Lightning:dot:7') and should play their entrance.
 */
export function barView(data, opts = {}) {
  const L = layoutBarView(data, opts);
  const enter = new Set(opts.enter ?? []);
  const ent = (key) => (enter.has(key) ? ' viz-enter' : '');
  const id = uid('bar');
  const out = [];
  out.push(`<svg class="viz-svg" width="${L.width}" height="${L.height}" viewBox="0 0 ${L.width} ${L.height}" role="group" aria-labelledby="${id}-t">`);
  out.push(`<title id="${id}-t">${esc(barViewSummary(data))}</title>`);

  for (const row of L.rows) {
    const shipped = row.ring?.ships ? ' is-ships' : '';
    out.push(`<g class="viz-row is-${row.state}${shipped}">`);
    out.push(`<line class="viz-track" x1="${row.track.x1}" x2="${row.track.x2}" y1="${row.track.y}" y2="${row.track.y}"/>`);
    const tipName = row.model === row.name ? row.name : `${row.name}: ${row.model}`;
    out.push(`<text class="viz-name" x="${L.nameX}" y="${r1(row.y + 4.5)}" data-tip="${esc(tipName)}">${esc(row.name)}</text>`);
    if (row.state === 'running') out.push(workingAt(L.width - 11, row.y - 7, 14));
    out.push('</g>');
  }

  // The rule sits under the marks so rings and dots cut it with their halos.
  out.push(`<line class="viz-rule" x1="${L.rule.x}" x2="${L.rule.x}" y1="${L.rule.y1}" y2="${L.rule.y2}" data-tip="Pass mark ${fmtScore(L.bar)}"/>`);

  for (const row of L.rows) {
    if (row.link) out.push(`<line class="viz-link is-${row.link.from}${ent(`${row.key}:ring`)}" x1="${row.link.x1}" x2="${row.link.x2}" y1="${row.y}" y2="${row.y}"/>`);
    if (row.tick) out.push(`<line class="viz-tick" x1="${row.tick.x}" x2="${row.tick.x}" y1="${row.tick.y1}" y2="${row.tick.y2}" data-tip="${esc(`Best preset: practice ${fmtScore(row.tick.value)}`)}"/>`);
    for (const d of row.dots.filter((dot) => !dot.best)) {
      out.push(`<circle class="viz-dot is-${d.age}${ent(`${row.key}:dot:${d.index}`)}" cx="${d.cx}" cy="${d.cy}" r="3" data-tip="${esc(`Forge submission ${d.index + 1} of ${d.count}: practice ${fmtScore(d.value)}`)}"/>`);
    }
    for (const d of row.dots.filter((dot) => dot.best)) {
      const what = row.ring ? 'sat the exam' : 'best so far';
      out.push(`<circle class="viz-dot is-best${ent(`${row.key}:dot:${d.index}`)}" cx="${d.cx}" cy="${d.cy}" r="4.5" data-tip="${esc(`Forge submission ${d.index + 1} of ${d.count}: practice ${fmtScore(d.value)}, ${what}`)}"/>`);
    }
    if (row.ring) out.push(`<g class="viz-ring-wrap${ent(`${row.key}:ring`)}">${ringMarkup(row.ring, row)}</g>`);
    if (row.value) {
      const kind = row.value.unscored ? 'is-unscored' : row.value.licensed ? 'is-licensed' : 'is-refused';
      const tip = row.value.tip ? ` data-tip="${esc(row.value.tip)}"` : '';
      out.push(`<text class="viz-value ${kind}${ent(`${row.key}:ring`)}" x="${row.value.x}" y="${r1(row.value.y)}" text-anchor="end"${tip}>${row.value.text}</text>`);
    }
  }

  out.push(`<text class="viz-rule-label" x="${L.rule.label.x}" y="${L.rule.label.y}" text-anchor="middle"><tspan class="viz-cap">pass mark</tspan><tspan class="viz-num" dx="5">${fmtScore(L.bar)}</tspan></text>`);
  out.push('<g class="viz-axis" aria-hidden="true">');
  for (const t of L.axis) out.push(`<text x="${t.x}" y="${t.y}" text-anchor="middle">${t.text}</text>`);
  out.push('</g></svg>');

  const legend = opts.legend === false ? '' : barLegend();
  return `<figure class="viz viz-barview" style="--viz-legend-indent:${L.nameX}px">${out.join('')}${legend}${barViewTable(data)}</figure>`;
}

/** The key under the bar view: each entry is the mark itself, then one word. */
export function barLegend() {
  const tick = '<svg width="8" height="14" viewBox="0 0 8 14" aria-hidden="true"><line class="viz-tick" x1="4" x2="4" y1="2" y2="12"/></svg>';
  const dots = '<svg width="30" height="14" viewBox="0 0 30 14" aria-hidden="true"><line class="viz-link is-forge" x1="4" x2="25" y1="7" y2="7"/><circle class="viz-dot is-old" cx="4" cy="7" r="3"/><circle class="viz-dot is-new" cx="14" cy="7" r="3"/><circle class="viz-dot is-best" cx="25" cy="7" r="4"/></svg>';
  const ring = (licensed) => `<svg width="18" height="18" viewBox="-9 -9 18 18" aria-hidden="true"><g class="viz-ring ${licensed ? 'is-licensed' : 'is-refused'}"><circle class="viz-ring-shape" r="8"/>${iconAt(licensed ? 'lock' : 'lockOpen', 0, 0, 10, 2.2, 'viz-lock')}</g></svg>`;
  return '<div class="viz-legend" aria-hidden="true">'
    + `<span>${tick}best preset</span><span>${dots}practice</span>`
    + `<span>${ring(true)}passed</span><span>${ring(false)}did not pass</span>`
    + '</div>';
}

/** The bar view as a table: the same facts for screen readers and for print. */
export function barViewTable(data) {
  const rows = (data.rungs ?? []).map((r) => {
    const p = (r.practice ?? []).filter(finite);
    const s = examScore(r.exam);
    const exam = r.state === 'done' && finite(s) ? `${fmtScore(s)}${r.exam && finite(r.exam.correct) ? ` (${r.exam.correct}/${r.exam.cases})` : ''}` : r.state === 'running' ? 'running' : 'not run';
    const verdict = r.state === 'done' && finite(s) ? `${r.licensed ? 'passed' : 'did not pass'}${r.ships ? ', ships' : ''}` : '';
    return `<tr><th scope="row">${esc(r.model ?? r.name)}</th><td>${fmtScore(r.preset)}</td><td>${p.map(fmtScore).join(', ')}</td><td>${exam}</td><td>${verdict}</td></tr>`;
  });
  return `<div class="sr-only"><table class="viz-table"><caption>Pass mark ${fmtScore(data.bar)}</caption><thead><tr><th scope="col">Rung</th><th scope="col">Best preset, practice</th><th scope="col">Forge practice, in order</th><th scope="col">Exam</th><th scope="col">Decision</th></tr></thead><tbody>${rows.join('')}</tbody></table></div>`;
}

// ---------------------------------------------------------------- exam chart

/**
 * Exam chart geometry: a unit chart, ten to a row, ordered correct, missed,
 * invalid. The counts are all it knows; no per-case exam data leaves verify().
 * report = { cases: 100, correct: 86, invalid: 1, overBudget?: 0, bar: 0.80, licensed: true }
 */
export function layoutExam(report, opts = {}) {
  const d = opts.cell ?? 14;
  const gap = opts.gap ?? 6;
  const pitch = d + gap;
  const cols = 10;
  const n = Math.max(0, report.cases | 0);
  const rows = Math.max(1, Math.ceil(n / cols));
  const scored = finite(report.correct);
  const correct = scored ? clamp(report.correct, 0, n) : 0;
  const invalid = clamp(report.invalid ?? 0, 0, n - correct);
  const overBudget = clamp(report.overBudget ?? 0, 0, n - correct - invalid);
  const missed = scored ? n - correct - invalid - overBudget : 0;
  const labelW = opts.labelWidth ?? 70;
  const over = 7;
  const gx = labelW;
  const gy = 3;
  const gridW = cols * pitch - gap;
  const gridH = rows * pitch - gap;

  const kinds = scored
    ? [['correct', correct], ['missed', missed], ['invalid', invalid], ['over', overBudget]]
    : [['invalid', invalid], ['over', overBudget], ['unscored', n - invalid - overBudget]];
  const cells = [];
  for (const [kind, count] of kinds) {
    for (let k = 0; k < count; k += 1) {
      const i = cells.length;
      cells.push({ kind, cx: r1(gx + (i % cols) * pitch + d / 2), cy: r1(gy + Math.floor(i / cols) * pitch + d / 2) });
    }
  }

  // The bar as a count: the first `need` cells must be solid to clear it.
  let rule = null;
  const need = Math.ceil(report.bar * n - 1e-9);
  if (finite(report.bar) && need > 0 && need <= n) {
    const q = Math.floor(need / cols);
    const s = need % cols;
    const xL = gx - over;
    const xR = gx + gridW + over;
    if (s === 0) {
      const y = gy + q * pitch - gap / 2;
      rule = { d: `M${xL} ${y}H${xR}`, labelY: y };
    } else {
      const yt = gy + q * pitch - gap / 2;
      const yb = gy + (q + 1) * pitch - gap / 2;
      const xs = gx + s * pitch - gap / 2;
      rule = { d: `M${xL} ${yb}H${xs}V${yt}H${xR}`, labelY: yb };
    }
  }

  const labelBase = rule ? rule.labelY + 4 : 0;
  return {
    width: gx + gridW + over + 2, height: Math.ceil(Math.max(gy + gridH + 4, labelBase + 4)), cell: d, gap, pitch, n, cols, rows,
    counts: { correct, missed, invalid, overBudget, scored }, need, cells, rule,
    label: rule ? { x: gx - over - 7, y: rule.labelY + 4 } : null,
  };
}

/** A dotted ring: round-capped zero-length dashes spaced around the circle. */
function dotted(r, spacing = 4.4) {
  const c = 2 * Math.PI * r;
  const k = Math.max(6, Math.round(c / spacing));
  return `0 ${(c / k).toFixed(3)}`;
}

function unitGlyph(kind, size = 12) {
  const r = size / 2;
  const inner = kind === 'correct' ? r : r - 0.75;
  const dash = kind === 'invalid' || kind === 'over' ? ` stroke-dasharray="${dotted(r - 0.9, 3.6)}"` : '';
  const rr = kind === 'invalid' || kind === 'over' ? r - 0.9 : inner;
  return `<svg width="${size}" height="${size}" viewBox="${-r} ${-r} ${size} ${size}" aria-hidden="true"><circle class="viz-unit is-${kind}" r="${rr}"${dash}/></svg>`;
}

export function examSummary(report) {
  const L = layoutExam(report);
  const c = L.counts;
  if (!c.scored) return `Exam not scored: ${report.invalid ?? 0} invalid, ${report.overBudget ?? 0} over budget, of ${L.n}.`;
  const verdict = typeof report.licensed === 'boolean' ? (report.licensed ? ' Licensed.' : ' Refused.') : '';
  return `Exam: ${c.correct} of ${L.n} correct, ${c.missed} missed, ${c.invalid} invalid. Bar ${fmtScore(report.bar)} needs ${L.need}.${verdict}`;
}

/**
 * The exam chart: a 10-wide unit chart of a signed report's counts (solid
 * correct, outlined missed, dotted invalid) with the bar drawn as a pine rule
 * after the cell the bar needs, beside the score, the lock and the key.
 * opts: { cell = 14, gap = 6, side = true }
 */
export function examChart(report, opts = {}) {
  const L = layoutExam(report, opts);
  const c = L.counts;
  const id = uid('exam');
  const out = [];
  out.push(`<svg class="viz-svg" width="${L.width}" height="${L.height}" viewBox="0 0 ${L.width} ${L.height}" role="img" aria-labelledby="${id}-t">`);
  out.push(`<title id="${id}-t">${esc(examSummary(report))}</title>`);
  const groups = {};
  for (const cell of L.cells) (groups[cell.kind] ??= []).push(cell);
  const tips = { correct: `${c.correct} correct`, missed: `${c.missed} missed`, invalid: `${c.invalid} invalid`, over: `${c.overBudget} over budget`, unscored: 'not scored' };
  for (const [kind, cells] of Object.entries(groups)) {
    out.push(`<g class="viz-units is-${kind}" data-tip="${esc(`${tips[kind]} of ${L.n}`)}">`);
    for (const cell of cells) {
      if (kind === 'correct') out.push(`<circle class="viz-unit is-correct" cx="${cell.cx}" cy="${cell.cy}" r="${L.cell / 2}"/>`);
      else if (kind === 'missed' || kind === 'unscored') out.push(`<circle class="viz-unit is-${kind}" cx="${cell.cx}" cy="${cell.cy}" r="${L.cell / 2 - 0.75}"/>`);
      else out.push(`<circle class="viz-unit is-${kind}" cx="${cell.cx}" cy="${cell.cy}" r="${L.cell / 2 - 0.9}" stroke-dasharray="${dotted(L.cell / 2 - 0.9)}"/>`);
    }
    out.push('</g>');
  }
  if (L.rule) {
    out.push(`<path class="viz-rule" d="${L.rule.d}" data-tip="${esc(`Bar ${fmtScore(report.bar)}: ${L.need} of ${L.n} correct`)}"/>`);
    out.push(`<text class="viz-rule-label" x="${L.label.x}" y="${r1(L.label.y)}" text-anchor="end"><tspan class="viz-cap">bar</tspan><tspan class="viz-num" dx="5">${fmtScore(report.bar)}</tspan></text>`);
  }
  out.push('</svg>');

  let side = '';
  if (opts.side !== false) {
    const lic = typeof report.licensed === 'boolean'
      ? `<span class="viz-verdict ${report.licensed ? 'is-licensed' : 'is-refused'}" role="img" aria-label="${report.licensed ? 'Licensed' : 'Refused'}" data-tip="${report.licensed ? 'Licensed' : 'Refused'}">${iconSvg(report.licensed ? 'lock' : 'lockOpen', { size: 18, stroke: 2 })}</span>`
      : '';
    const score = c.scored
      ? `<span class="viz-score num">${c.correct}<span class="viz-of">/${L.n}</span></span>`
      : '<span class="viz-score is-unscored">not scored</span>';
    const key = (kind, count, word) => `<li class="${count ? '' : 'is-zero'}">${unitGlyph(kind)}<span class="num">${count}</span><span>${word}</span></li>`;
    side = `<div class="viz-exam-side"><p class="viz-exam-score">${lic}${score}</p><ul class="viz-exam-key">`
      + (c.scored ? key('correct', c.correct, 'correct') + key('missed', c.missed, 'missed') : '')
      + key('invalid', c.invalid, 'invalid')
      + (c.overBudget || !c.scored ? key('over', c.overBudget, 'over budget') : '')
      + '</ul></div>';
  }
  return `<figure class="viz viz-exam">${out.join('')}${side}</figure>`;
}

// ---------------------------------------------------------------- gate scatter

/**
 * Gate scatter geometry. One point per harness: practice score across, exam score
 * up, the bar as a pine rule on both axes.
 * data = { bar: 0.80, domain?: [0.6, 1], points: [{ id, label?, practice, exam, licensed? }] }
 */
export function layoutScatter(data, opts = {}) {
  const width = Math.max(300, opts.width ?? 560);
  const small = width < 460;
  const padL = small ? 36 : 42;
  const padR = small ? 10 : 14;
  const padT = 30;
  const padB = small ? 40 : 44;
  const S = width - padL - padR;
  const height = padT + S + padB;
  const bar = data.bar;
  const pts = (data.points ?? []).filter((p) => finite(p.practice) && finite(p.exam));
  const domain = data.domain ?? scoreDomain([bar, ...pts.flatMap((p) => [p.practice, p.exam])], { pad: 0.04 });
  const span = domain[1] - domain[0];
  const sx = (v) => padL + ((clamp(v, domain[0], domain[1]) - domain[0]) / span) * S;
  const sy = (v) => padT + S - ((clamp(v, domain[0], domain[1]) - domain[0]) / span) * S;
  const plot = { x0: padL, y0: padT, x1: padL + S, y1: padT + S, size: S };
  const xb = Math.round(sx(bar));
  const yb = Math.round(sy(bar));
  const r = small ? 4 : 4.5;

  const points = pts.map((p) => {
    const passedPractice = p.practice >= bar;
    const passedExam = typeof p.licensed === 'boolean' ? p.licensed : p.exam >= bar;
    return {
      ...p, cx: r1(sx(p.practice)), cy: r1(sy(p.exam)), r, passedPractice, passedExam,
      zone: passedPractice ? (passedExam ? 'kept' : 'caught') : 'context',
    };
  });
  const passed = points.filter((p) => p.passedPractice).length;
  const caught = points.filter((p) => p.zone === 'caught').length;

  // The caught quadrant: right of the practice bar, below the exam bar.
  const quad = { x: xb, y: yb, w: plot.x1 - xb, h: plot.y1 - yb };

  // The annotation goes in a corner of the quadrant that no point covers.
  const lines = quad.w >= 214 ? ['passed practice, failed exam'] : ['passed practice,', 'failed exam'];
  const boxW = Math.max(66, ...lines.map((l) => l.length * 6.7)) + 4;
  const boxH = 24 + lines.length * 16;
  const pad = 10;
  const corners = [
    { x: quad.x + quad.w - pad - boxW, y: quad.y + quad.h - pad - boxH, anchor: 'end' },
    { x: quad.x + pad, y: quad.y + quad.h - pad - boxH, anchor: 'start' },
    { x: quad.x + quad.w - pad - boxW, y: quad.y + pad, anchor: 'end' },
    { x: quad.x + pad, y: quad.y + pad, anchor: 'start' },
  ];
  const hits = (c) => points.filter((p) => p.cx + p.r + 3 > c.x && p.cx - p.r - 3 < c.x + boxW && p.cy + p.r + 3 > c.y && p.cy - p.r - 3 < c.y + boxH).length;
  let note = null;
  if (quad.w > boxW + 2 * pad && quad.h > boxH + 2 * pad) {
    const best = corners.map((c) => ({ ...c, hits: hits(c) })).sort((a, b) => a.hits - b.hits)[0];
    note = { ...best, w: boxW, h: boxH, lines, tx: best.anchor === 'end' ? best.x + boxW : best.x };
  }

  return {
    width, height, plot, domain, bar, xb, yb, quad, points, passed, caught, note,
    axisX: ticks(domain).map((v) => ({ x: r1(sx(v)), y: plot.y1 + 18, text: fmtStep(v) })),
    axisY: ticks(domain).map((v) => ({ x: padL - 8, y: r1(sy(v) + 4), text: fmtStep(v) })),
    diagonal: { x1: sx(domain[0]), y1: sy(domain[0]), x2: sx(1), y2: sy(1) },
  };
}

export function scatterSummary(data) {
  const L = layoutScatter(data);
  return `Gate scatter of ${L.points.length} harnesses, practice score across and exam score up, bar ${fmtScore(data.bar)} on both. ${L.passed} met the bar in practice; ${L.caught} of them fell below it on the exam.`;
}

/**
 * The gate scatter. opts: { width, lit = true } where lit tints the caught
 * quadrant, keeps the harnesses that passed practice in pine and greys the rest.
 * Toggle the figure's `is-lit` class to play the cold open's reveal.
 */
export function gateScatter(data, opts = {}) {
  const L = layoutScatter(data, opts);
  const lit = opts.lit !== false;
  const id = uid('gate');
  const P = L.plot;
  const out = [];
  out.push(`<svg class="viz-svg" width="${L.width}" height="${L.height}" viewBox="0 0 ${L.width} ${L.height}" role="img" aria-labelledby="${id}-t">`);
  out.push(`<title id="${id}-t">${esc(scatterSummary(data))}</title>`);
  out.push(`<rect class="viz-quad" x="${L.quad.x}" y="${L.quad.y}" width="${L.quad.w}" height="${L.quad.h}"/>`);
  out.push(`<rect class="viz-frame" x="${P.x0 + 0.5}" y="${P.y0 + 0.5}" width="${P.size - 1}" height="${P.size - 1}"/>`);
  out.push(`<line class="viz-diagonal" x1="${r1(L.diagonal.x1)}" y1="${r1(L.diagonal.y1)}" x2="${r1(L.diagonal.x2)}" y2="${r1(L.diagonal.y2)}"/>`);
  out.push(`<line class="viz-rule" x1="${L.xb}" x2="${L.xb}" y1="${P.y0 - 6}" y2="${P.y1}" data-tip="${esc(`Bar ${fmtScore(L.bar)} on practice`)}"/>`);
  out.push(`<line class="viz-rule" x1="${P.x0}" x2="${P.x1 + 6}" y1="${L.yb}" y2="${L.yb}" data-tip="${esc(`Bar ${fmtScore(L.bar)} on the exam`)}"/>`);

  const order = { context: 0, kept: 1, caught: 2 };
  const pts = [...L.points].sort((a, b) => order[a.zone] - order[b.zone]);
  out.push('<g class="viz-points">');
  for (const p of pts) {
    const name = p.label ?? `Harness ${p.id}`;
    const tip = `${name}: practice ${fmtScore(p.practice)}, exam ${fmtScore(p.exam)}, ${p.passedExam ? 'licensed' : 'refused'}`;
    const shape = p.passedExam ? `<circle class="viz-pt-shape" r="${p.r}"/>` : `<circle class="viz-pt-shape" r="${p.r - 0.75}"/>`;
    out.push(`<g class="viz-pt is-${p.zone} ${p.passedExam ? 'is-solid' : 'is-hollow'}" transform="translate(${p.cx} ${p.cy})" data-tip="${esc(tip)}"><circle class="viz-hit" r="12"/>${shape}</g>`);
  }
  out.push('</g>');

  if (L.note) {
    const n = L.note;
    const lineY = (i) => r1(n.y + 34 + i * 16);
    out.push(`<g class="viz-note" aria-hidden="true"><text class="viz-note-count" x="${r1(n.tx)}" y="${r1(n.y + 16)}" text-anchor="${n.anchor}"><tspan class="viz-num-strong">${L.caught}</tspan><tspan class="viz-num-soft" dx="6">of ${L.passed}</tspan></text>`);
    n.lines.forEach((line, i) => out.push(`<text class="viz-note-text" x="${r1(n.tx)}" y="${lineY(i)}" text-anchor="${n.anchor}">${esc(line)}</text>`));
    out.push('</g>');
  }

  out.push(`<text class="viz-rule-label" x="${L.xb}" y="${P.y0 - 13}" text-anchor="middle"><tspan class="viz-cap">bar</tspan><tspan class="viz-num" dx="5">${fmtScore(L.bar)}</tspan></text>`);
  out.push('<g class="viz-axis" aria-hidden="true">');
  for (const t of L.axisX) out.push(`<text x="${t.x}" y="${t.y}" text-anchor="middle">${t.text}</text>`);
  for (const t of L.axisY) out.push(`<text x="${t.x}" y="${t.y}" text-anchor="end">${t.text}</text>`);
  out.push(`<text class="viz-cap" x="${P.x1}" y="${P.y1 + 36}" text-anchor="end">practice</text>`);
  out.push(`<text class="viz-cap" x="${P.x0 - 8}" y="${P.y0 - 13}" text-anchor="start">exam</text>`);
  out.push('</g></svg>');
  const below = L.note ? '' : `<p class="viz-scatter-note" aria-hidden="true"><span class="viz-swatch"></span><span class="num viz-strong">${L.caught}</span><span class="num">of ${L.passed}</span><span>passed practice, failed exam</span></p>`;
  return `<figure class="viz viz-scatter${lit ? ' is-lit' : ''}">${out.join('')}${below}${scatterTable(data)}</figure>`;
}

export function scatterTable(data) {
  const L = layoutScatter(data);
  const rows = L.points.map((p) => `<tr><th scope="row">${esc(p.label ?? `Harness ${p.id}`)}</th><td>${fmtScore(p.practice)}</td><td>${fmtScore(p.exam)}</td><td>${p.passedExam ? 'licensed' : 'refused'}</td></tr>`);
  return `<div class="sr-only"><table class="viz-table"><caption>${esc(scatterSummary(data))}</caption><thead><tr><th scope="col">Harness</th><th scope="col">Practice</th><th scope="col">Exam</th><th scope="col">Decision</th></tr></thead><tbody>${rows.join('')}</tbody></table></div>`;
}

// ---------------------------------------------------------------- hash line

/**
 * A hash the way people compare them: mono, in eight groups of eight, one
 * copy button. Selecting the text copies it unbroken. match = true draws the
 * check against the published value (tip names it); false draws a warning.
 * h = { label: 'spec', hash, match?: boolean, matchName?: 'prereg-2026-10-11', note?: 'frozen, receipts.json' }
 */
export function hashLine(h) {
  const hash = String(h.hash ?? '').toLowerCase();
  const groups = hash.match(/.{1,8}/g) ?? [];
  const halves = [groups.slice(0, 4), groups.slice(4)].filter((g) => g.length);
  const label = esc(h.label ?? 'hash');
  let state = '';
  if (h.match === true) state = `<span class="hashline-state is-match" role="img" aria-label="${esc(`Matches ${h.matchName ?? 'the published hash'}`)}" data-tip="${esc(`Matches ${h.matchName ?? 'the published hash'}`)}">${iconSvg('circleCheck', { size: 15, stroke: 2 })}</span>`;
  if (h.match === false) state = `<span class="hashline-state is-mismatch" role="img" aria-label="${esc(`Differs from ${h.matchName ?? 'the published hash'}`)}" data-tip="${esc(`Differs from ${h.matchName ?? 'the published hash'}`)}">${iconSvg('alertTriangle', { size: 15, stroke: 2 })}</span>`;
  const note = h.note ? `<span class="hashline-note">${esc(h.note)}</span>` : '';
  return `<div class="hashline"><span class="hashline-label">${label}</span>`
    + `<span class="hashline-value"><code class="hashline-hash">${halves.map((half) => `<span class="hashline-half">${[half.slice(0, 2), half.slice(2)].filter((q) => q.length).map((q) => `<span class="hashline-quarter">${q.map((g) => `<span>${esc(g)}</span>`).join('')}</span>`).join('')}</span>`).join('')}</code></span>`
    + `<span class="hashline-state-cell">${state}</span>`
    + `<span class="hashline-action"><button type="button" class="viz-icon-button" data-copy="${esc(hash)}" aria-label="${esc(`Copy ${h.label ?? 'hash'}`)}" data-tip="Copy">${iconSvg('copy', { size: 14, stroke: 2 })}</button></span>`
    + `${note}</div>`;
}

/** Several hash lines in one container, so they restack together on a narrow card. */
export const hashLines = (lines) => `<div class="hashlines">${lines.map((h) => hashLine(h)).join('')}</div>`;

/**
 * The signing key, in the hash line's grid: fingerprint in groups of four, the
 * algorithm after it, and the browser's verdict where the check ran.
 * sig = { alg: 'Ed25519', key: hex fingerprint, verified?: boolean }
 */
export function signatureLine(sig) {
  const key = (String(sig.key ?? '').toLowerCase().match(/.{1,4}/g) ?? []).map((g) => `<span>${esc(g)}</span>`).join('');
  const alg = esc(sig.alg ?? 'Ed25519');
  let state = '';
  if (sig.verified === true) state = `<span class="hashline-state is-match" role="img" aria-label="${alg} signature verified in this browser" data-tip="${alg} signature verified in this browser">${iconSvg('circleCheck', { size: 15, stroke: 2 })}</span>`;
  if (sig.verified === false) state = `<span class="hashline-state is-mismatch" role="img" aria-label="${alg} signature does not match" data-tip="${alg} signature does not match">${iconSvg('circleX', { size: 15, stroke: 2 })}</span>`;
  return `<div class="hashline is-key"><span class="hashline-label">key</span>`
    + `<span class="hashline-value"><code class="hashline-hash"><span class="hashline-half">${key}</span></code><span class="hashline-alg">${alg}</span></span>`
    + `<span class="hashline-state-cell">${state}</span><span class="hashline-action"></span></div>`;
}

// ---------------------------------------------------------------- receipt card

/**
 * The signed run summary as a receipt: what ran, what it used, what the exam
 * said, and the proof.
 * r = {
 *   name: 'receipt-reader', date: ISO string, placeholder?: true,
 *   roles: [{ role: 'Build', model: 'Nemotron 3 Ultra', calls, tokens, cost, seconds }],
 *   exam: { cases, correct, invalid, bar }, licensed: boolean,
 *   pack: sha256 hex, spec?: sha256 hex, prereg?: 'prereg-2026-10-11', specMatch?, packMatch?,
 *   signature?: { alg: 'Ed25519', key: hex fingerprint, verified?: boolean },
 * }
 */
export function receiptCard(r) {
  const roles = r.roles ?? [];
  const sum = (k) => roles.reduce((t, x) => t + (finite(x[k]) ? x[k] : 0), 0);
  const ex = r.exam ?? {};
  const scored = finite(ex.correct);
  const verdict = typeof r.licensed === 'boolean'
    ? `<span class="viz-verdict ${r.licensed ? 'is-licensed' : 'is-refused'}" role="img" aria-label="${r.licensed ? 'Licensed' : 'Refused'}" data-tip="${r.licensed ? 'Licensed' : 'Refused'}">${iconSvg(r.licensed ? 'lock' : 'lockOpen', { size: 18, stroke: 2 })}</span>`
    : '';
  const meta = [`invalid <span class="num">${ex.invalid ?? 0}</span>`, finite(ex.bar) ? `bar <span class="num">${fmtScore(ex.bar)}</span>` : ''].filter(Boolean).join('<span class="sep" aria-hidden="true">·</span>');

  // "Nemotron 3.5 Lightning": on a narrow card the family is hidden from sight (not from
  // screen readers) and the tier carries the row.
  const model = (name) => {
    const words = String(name ?? '').trim().split(/\s+/);
    const tier = words.pop() ?? '';
    return words.length ? `<span class="receipt-family">${esc(words.join(' '))} </span>${esc(tier)}` : esc(tier);
  };
  const rows = roles.map((x) => `<tr><th scope="row"><span class="receipt-who"><span class="receipt-role">${esc(x.role)}</span><span class="receipt-model">${model(x.model)}</span></span></th>`
    + `<td class="num">${fmtInt(x.calls)}</td><td class="num" title="${fmtInt(x.tokens)} tokens">${fmtTokens(x.tokens)}</td><td class="num">${fmtUsd(x.cost)}</td><td class="num">${fmtSeconds(x.seconds)}</td></tr>`).join('');

  const sig = r.signature ? signatureLine(r.signature) : '';

  return `<article class="receipt" aria-label="${esc(`Signed run summary for ${r.name}${r.placeholder ? ', placeholder data' : ''}`)}">`
    + '<header class="receipt-head">'
    + `<div class="receipt-id"><h3 class="receipt-name">${esc(r.name)}</h3><time class="receipt-date num" datetime="${esc(r.date)}">${fmtDate(r.date)}</time></div>`
    + `<div class="receipt-result"><p class="receipt-score">${verdict}${scored ? `<span class="num">${ex.correct}<span class="viz-of">/${ex.cases}</span></span>` : '<span>not scored</span>'}</p><p class="receipt-meta">exam<span class="sep" aria-hidden="true">·</span>${meta}</p></div>`
    + '</header>'
    + '<table class="receipt-table"><thead><tr><th scope="col"><span class="sr-only">Role and model</span></th><th scope="col">calls</th><th scope="col">tokens</th><th scope="col">cost</th><th scope="col">time</th></tr></thead>'
    + `<tbody>${rows}</tbody>`
    + `<tfoot><tr><th scope="row">total</th><td class="num">${fmtInt(sum('calls'))}</td><td class="num" title="${fmtInt(sum('tokens'))} tokens">${fmtTokens(sum('tokens'))}</td><td class="num">${fmtUsd(sum('cost'))}</td><td class="num">${fmtSeconds(sum('seconds'))}</td></tr></tfoot></table>`
    + '<footer class="receipt-proof hashlines">'
    + (r.pack ? hashLine({ label: 'pack', hash: r.pack, match: r.packMatch, matchName: r.prereg }) : '')
    + (r.spec ? hashLine({ label: 'spec', hash: r.spec, match: r.specMatch, matchName: r.prereg }) : '')
    + sig
    + '</footer></article>';
}

// ---------------------------------------------------------------- browser glue

/**
 * Hover and focus readouts for anything carrying data-tip, styled like the
 * desktop's tooltip. Text only (textContent), never markup. Returns a detach.
 */
export function attachTips(root = document) {
  const tip = document.createElement('div');
  tip.className = 'viz-tip';
  tip.setAttribute('role', 'tooltip');
  document.body.appendChild(tip);
  let current = null;
  const show = (el) => {
    if (el === current) return;
    current = el;
    if (!el) { tip.classList.remove('is-on'); return; }
    tip.textContent = el.getAttribute('data-tip');
    const box = el.getBoundingClientRect();
    tip.classList.add('is-on');
    const w = tip.offsetWidth;
    const h = tip.offsetHeight;
    const left = clamp(box.left + box.width / 2 - w / 2, 8, window.innerWidth - w - 8);
    // Above the mark, or below it when the window's top edge would cut the tip off.
    const top = box.top - h - 8 >= 4 ? box.top - h - 8 : box.bottom + 8;
    tip.style.transform = `translate(${Math.round(left + window.scrollX)}px, ${Math.round(top + window.scrollY)}px)`;
  };
  const find = (e) => (e.target instanceof Element ? e.target.closest('[data-tip]') : null);
  const over = (e) => show(find(e));
  const out = (e) => { if (!e.relatedTarget || !(e.relatedTarget instanceof Element) || !e.relatedTarget.closest('[data-tip]')) show(null); };
  const focus = (e) => show(find(e));
  const blur = () => show(null);
  const copy = async (e) => {
    const btn = e.target instanceof Element ? e.target.closest('[data-copy]') : null;
    if (!btn) return;
    try { await navigator.clipboard.writeText(btn.getAttribute('data-copy')); btn.setAttribute('data-tip', 'Copied'); current = null; show(btn); setTimeout(() => btn.setAttribute('data-tip', 'Copy'), 1400); } catch { /* clipboard refused: the text is still selectable */ }
  };
  root.addEventListener('pointerover', over);
  root.addEventListener('pointerout', out);
  root.addEventListener('focusin', focus);
  root.addEventListener('focusout', blur);
  root.addEventListener('click', copy);
  return () => {
    root.removeEventListener('pointerover', over);
    root.removeEventListener('pointerout', out);
    root.removeEventListener('focusin', focus);
    root.removeEventListener('focusout', blur);
    root.removeEventListener('click', copy);
    tip.remove();
  };
}

/** Render into `el` at its own width, and again whenever that width changes. */
export function mount(el, render) {
  let last = -1;
  const draw = () => {
    const w = Math.floor(el.clientWidth);
    if (w === last || w <= 0) return;
    last = w;
    el.innerHTML = render(w);
  };
  draw();
  if (typeof ResizeObserver !== 'undefined') new ResizeObserver(draw).observe(el);
  return draw;
}
