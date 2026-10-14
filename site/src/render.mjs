/*
 * Build-time renderers: data in, markup out, for every part of the page. The
 * poster's bar view comes from design/components.js unchanged, so the page and
 * the desktop app share one grammar. site.js later adds the tooltips, the copy
 * buttons, the theme switch and the video.
 */

import * as V from '../design/components.js';
import { MARK_OUTLINE, PLAY_FILLED, SITE_ICONS } from './icons.mjs';

Object.assign(V.ICON_PATHS, SITE_ICONS);
const { esc, fmtScore, fmtUsd } = V;

export const icon = (name, size = 16, stroke = 2, cls = '') => V.iconSvg(name, { size, stroke, cls });
const fmt3 = (v) => (Number.isFinite(v) ? v.toFixed(3) : '');
const num = (v) => `<span class="num">${esc(v)}</span>`;

/** Static charts get their own id prefix, so they never share an id with anything the browser adds. */
const staticIds = (html) => html.replace(/(id|aria-labelledby)="((?:bar|gate|exam)-[0-9a-z]+-t)"/g, '$1="s-$2"');

/** Lectern's mark as a standalone glyph. */
export const mark = (cls = 'brand-mark') => `<svg class="${cls}" viewBox="25 16 50 66" fill="currentColor" aria-hidden="true"><path d="${MARK_OUTLINE}"/></svg>`;

/** The agent page's two state marks, drawn here: a filled disc with a check (passed) or two bars (stopped). */
export const stateIcon = (state) => '<svg class="state-icon" width="22" height="22" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="11" fill="currentColor"/>'
  + (state === 'passed'
    ? '<path class="state-glyph" d="M7.5 12.4l3 3l6 -6.3" fill="none" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>'
    : '<path class="state-glyph" d="M9.4 8v8M14.6 8v8" fill="none" stroke-width="2.4" stroke-linecap="round"/>')
  + '</svg>';

export const copyButton = (text, label = 'Copy') => `<button type="button" class="viz-icon-button" data-copy="${esc(text)}" aria-label="${esc(label)}" data-tip="Copy">${icon('copy', 14)}</button>`;

const lock = (licensed, size = 15, ships = false) => {
  const label = licensed ? (ships ? 'Passed and shipped' : 'Passed') : 'Did not pass';
  return `<span class="lock ${licensed ? 'is-licensed' : 'is-refused'}" role="img" aria-label="${label}" data-tip="${label}">${icon(licensed ? 'lock' : 'lockOpen', size)}</span>`;
};

/** "A", "A and B", "A, B and C". */
const list = (items) => (items.length < 3 ? items.join(' and ') : `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`);

// ---------------------------------------------------------------- page parts

/** The banner names only what is still a stand-in: the results (results.json), or the video and the downloads (site.json). */
export function notice(placeholderFiles) {
  if (!placeholderFiles.length) return '';
  const results = placeholderFiles.includes('results');
  const site = placeholderFiles.includes('site');
  const what = results && site
    ? 'The results, the video and the download links are stand-ins until the evidence run and the release are done.'
    : results
      ? 'The results are stand-ins until the evidence run is done.'
      : 'The video and the download links are stand-ins until the release is done.';
  return `<div class="notice" role="note"><div class="wrap">${icon('alertTriangle', 16)}<p>Preview with placeholder data. ${what}</p></div></div>`;
}

/** The rungs of the build with the rule checks, cheapest first, in the shape the bar view takes. */
export function rungs(results) {
  return results.build.rungs.map((r) => ({
    name: r.name,
    model: r.model,
    state: 'done',
    preset: r.preset ?? undefined,
    practice: r.practice ?? undefined,
    examined: r.examined ?? undefined,
    exam: r.exam,
    licensed: r.licensed,
    ships: r.ships,
  }));
}

/** The video's poster: the bar view of the build with the rule checks, with no words, white on a flat pine tile, like the film's thumbnail. */
export function video(site, results) {
  const poster = staticIds(V.barView({ bar: results.bar, domain: results.domain, rungs: rungs(results) }, { width: 480, legend: false, labelWidth: 16, rowHeight: 64 }));
  const id = site.video.youtube_id;
  const label = id ? 'Play the video' : 'The video is not published yet';
  return `<figure class="video" data-youtube="${esc(id ?? '')}">`
    + `<div class="video-poster" aria-hidden="true">${poster}</div>`
    + `<button type="button" class="video-play"${id ? '' : ' aria-disabled="true"'} aria-label="${label}" data-tip="${label}">`
    + `<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="${PLAY_FILLED}"/></svg></button>`
    + (site.video.duration ? `<span class="video-time num" aria-hidden="true">${esc(site.video.duration)}</span>` : '')
    + '</figure>';
}

const asset = (site, file) => `${site.repo}/releases/download/${encodeURIComponent(site.release.tag)}/${encodeURIComponent(file)}`;

/** One link per installer. The head script marks the visitor's system, and the CSS fills that one. */
export function downloads(site) {
  const r = site.release;
  const items = [
    ['windows', 'brandWindows', 'Windows', r.windows, 'Download for Windows'],
    ['mac-arm64', 'brandApple', 'Apple silicon', r.mac_arm64, 'Download for a Mac with Apple silicon'],
    ['mac-x64', 'brandApple', 'Intel', r.mac_x64, 'Download for an Intel Mac'],
  ];
  const links = items.map(([os, glyph, word, file, label]) => `<a class="dl" data-os="${os}" href="${esc(asset(site, file))}" aria-label="${esc(label)}" data-tip="${esc(file)}">${icon(glyph, 17)}<span>${word}</span></a>`).join('');
  return `<div class="dl-row">${links}</div>`;
}

export function commands(site) {
  const rows = site.commands.map((c) => `<li class="cmd"><span class="cmd-prompt" aria-hidden="true">$</span><code>${esc(c)}</code>${copyButton(c, 'Copy this command')}</li>`).join('');
  return `<ol class="cmds" aria-label="Commands for the terminal">${rows}</ol>`;
}

// ---------------------------------------------------------------- results

/**
 * The paragraph above the first table, from the data: the model the build ships against
 * the most accurate plain model, on the receipts no build step saw.
 */
export function resultsParagraph(results) {
  const shipped = results.build.rungs.find((r) => r.ships);
  const top = results.models.reduce((best, m) => (m.k > best.k ? m : best));
  const ship = shipped ? results.models.find((m) => m.name === shipped.model) : null;
  let plain = ` Each model read the same ${num(results.n)} receipts, which no build step saw.`;
  if (ship && ship !== top && top.cost_per_receipt_usd > 0) {
    const overlap = ship.ci[0] <= top.ci[1] && top.ci[0] <= ship.ci[1];
    const share = Math.round((ship.cost_per_receipt_usd / top.cost_per_receipt_usd) * 100);
    plain = ` On ${num(results.n)} receipts that no build step saw, plain ${esc(ship.short)} got ${num(ship.k)} right to ${esc(top.short)}'s ${num(top.k)}${overlap ? ', too close for this run to say which is more accurate (their 95% intervals overlap)' : ''}.`
      + ` ${esc(ship.short)}'s cost per receipt is about ${num(`${share}%`)} of ${esc(top.short)}'s.`;
  }
  return `The example is a receipt reader for a small expense app.${plain} These are the models alone, with the plain prompt and no rule checks.`;
}

/** How many right answers an exam of `cases` needs to reach the bar. */
const needed = (bar, cases) => Math.ceil(Math.round(bar * cases * 1e6) / 1e6);

/**
 * The rule checks: what they are and how they are held to the exam, then what the build
 * with them decided, and the second table, each rung's exam with the checks beside the
 * same exam without them. build.mjs makes sure the checks are the two these words describe.
 */
export function checks(results) {
  const b = results.build;
  const refused = b.rungs.filter((r) => !r.licensed);
  const shipped = b.rungs.find((r) => r.ships);
  let text = "The receipt reader's plan declares two rule checks from the user's own rules: amounts keep every printed digit, and a subtotal equal to the total is null unless the receipt prints it as a subtotal."
    + ' Each check is code that runs on every reply. The checks are part of the plan, so each exam tests the model and its checks together, and a pass covers both.';
  if (b.rungs.every((r) => r.exam.correct > r.before.exam.correct)) text += ' With the checks, every model got more exam receipts right.';
  const parts = [];
  const gap = refused.length === 1 ? needed(results.bar, refused[0].exam.cases) - refused[0].exam.correct : 0;
  if (gap > 0) parts.push(`${esc(refused[0].name)} missed the pass mark by ${gap === 1 ? 'one receipt' : `${num(gap)} receipts`}`);
  else if (refused.length) parts.push(`${list(refused.map((r) => esc(r.name)))} did not pass`);
  parts.push(shipped ? `${esc(shipped.name)} shipped${shipped.before.ships ? ' again' : ''}` : 'nothing shipped');
  const outcome = parts.join(', and ');
  text += ` ${outcome[0].toUpperCase()}${outcome.slice(1)}.`;
  return `<p class="section-lead results-lead results-next">${text}</p><div class="results-frame">${checksTable(results)}</div>${laterSits(results)}`;
}

/**
 * One line under the second table: what later builds of the same spec, which made the same
 * agents and sat the same exam, gave the cheapest rung, and whether one of them shipped it.
 */
function laterSits(results) {
  const sits = results.build.later;
  if (!sits.length) return '';
  const first = results.build.rungs[0];
  const counts = sits.map((s) => s.exam[first.name]);
  const shipped = sits.filter((s) => s.ships === first.name).map((s) => s.exam[first.name]);
  const how = sits.length === 1 ? 'One later build' : `${['Two', 'Three', 'Four'][sits.length - 2] ?? num(sits.length)} later builds`;
  let text = `${how} of the same plan made the same agents and took the same exam: ${esc(first.name)} got ${list(counts.map(num))}`;
  if (shipped.length) text += `, and at ${list(shipped.map(num))} it passed and shipped`;
  return `<p class="results-note results-sits">${text}.</p>`;
}

/**
 * The notes under the tables: what counts as right, why the checks exist and what they
 * cost, Forge's record with a Nemotron engineer, where to check the numbers, and the
 * footer lines the evaluation plan asks every results table to carry.
 */
export function resultsNotes(results) {
  const f = results.forge;
  let forge = '';
  if (f) {
    const who = list(f.engineers.map((e) => `${num(e.sessions)} with ${esc(e.name)}`));
    forge = " Forge is Architect's coding agent: it writes a custom harness (the code around the model calls) to try to beat the tuned preset, a built-in strategy tuned on the practice receipts."
      + (f.accepted === 0
        ? ` In ${num(f.sessions)} sessions with a Nemotron model as its engineer, ${who}, none of its harnesses was accepted, so a build on Nemotron ships the tuned preset.`
        : ` In ${num(f.sessions)} sessions with a Nemotron model as its engineer, ${who}, it had a harness accepted in ${num(f.accepted)}.`);
  }
  // Each exam's cost per receipt, as dollars per 1,000 receipts rounded to the cent.
  const cents = (usd) => Math.round(usd * 1000 * 100);
  const sameCost = results.build.rungs.every((r) => cents(r.cost) === cents(r.before.cost));
  const text = 'A receipt counts only when its total, subtotal, tax and item count are all exact.'
    + ' The checks were added after a re-run of a pilot on other receipts, where nearly every field Ultra and Lightning got wrong broke a rule the prompt states, most often by giving a subtotal where the receipt prints none.'
    + (sameCost
      ? ` They make no model calls, and each exam cost the same per ${num('1,000')} receipts as without them, to the cent.`
      : ' They make no model calls.');
  const verify = `To check the signed results offline, run <code>uv run architect verify --published receipts</code> in a checkout, or read the <a href="${esc(results.plan.results_doc)}">full results</a> and the <a href="${esc(results.plan.doc)}">evaluation plan</a>.`;
  const foot = results.footer.map((line) => `<li>${esc(line)}</li>`).join('');
  return [text, forge.trim(), verify].filter(Boolean).map((p) => `<p class="results-note">${p}</p>`).join('') + `<ul class="results-foot" aria-label="Notes that apply to the tables">${foot}</ul>`;
}

/**
 * A small chart on the domain's scale, the same in both tables so their marks compare:
 * the track, the bar's rule, then the marks `draw` adds with the scale it is given.
 */
function mini(results, draw, w = 112) {
  const [lo, hi] = results.domain;
  const pad = 5;
  const h = 18;
  const x = (v) => Math.round((pad + ((Math.min(hi, Math.max(lo, v)) - lo) / (hi - lo)) * (w - 2 * pad)) * 10) / 10;
  return `<svg class="mini" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true">`
    + `<line class="viz-track" x1="${pad}" x2="${w - pad}" y1="9.5" y2="9.5"/>`
    + `<line class="viz-rule" x1="${Math.round(x(results.bar))}" x2="${Math.round(x(results.bar))}" y1="2" y2="${h - 2}"/>`
    + draw(x, 9)
    + '</svg>';
}

/** A point: solid when it passed, outlined when it did not. */
const point = (cx, cy, passed) => `<circle class="mini-pt ${passed ? 'is-solid' : 'is-hollow'}" cx="${cx}" cy="${cy}" r="${passed ? 4 : 3.4}"/>`;

/** Exact-match accuracy: the 95% interval and the score. */
const accuracyMini = (m, results) => mini(results, (x, y) => {
  const score = m.k / results.n;
  return `<line class="mini-ci" x1="${x(m.ci[0])}" x2="${x(m.ci[1])}" y1="${y}" y2="${y}"/>${point(x(score), y, score >= results.bar)}`;
});

/** One exam without the checks, as a gray tick, joined to the same exam with them, as a point. */
const gainMini = (r, results) => mini(results, (x, y) => {
  const [then, now] = [r.before.exam, r.exam].map((e) => x(e.correct / e.cases));
  return `<line class="mini-link" x1="${then}" x2="${now}" y1="${y}" y2="${y}"/>`
    + `<line class="mini-ref" x1="${then}" x2="${then}" y1="${y - 5}" y2="${y + 5}"/>`
    + point(now, y, r.licensed);
});

const interval = (ci) => `[${fmt3(ci[0])}, ${fmt3(ci[1])}]`;

const modelCell = (full, short, id) => `<th scope="row" class="c-model"><span class="m-full" data-tip="${esc(id)}">${esc(full)}</span><span class="m-short" data-tip="${esc(id)}">${esc(short)}</span></th>`;

const headRow = (cols) => cols.map(([cls, word, tip]) => `<th scope="col" class="${cls}"${tip ? ` data-tip="${esc(tip)}"` : ''}>${word}</th>`).join('');

function row(m, results) {
  const n = results.n;
  const score = m.k / n;
  const tip = `${m.k} of ${n} receipts exact, 95% interval ${fmt3(m.ci[0])} to ${fmt3(m.ci[1])}`;
  return '<tr>'
    + modelCell(m.name, m.short, m.id)
    + `<td class="c-exact" data-tip="${esc(tip)}"><span class="exact">${accuracyMini(m, results)}<span class="num score">${fmt3(score)}</span><span class="num k">${m.k}/${n}</span></span><span class="num ci-inline">${interval(m.ci)}</span></td>`
    + `<td class="c-ci num">${interval(m.ci)}</td>`
    + `<td class="c-cost num" data-tip="${esc(`$${m.cost_per_receipt_usd} per receipt`)}">${fmtUsd(m.cost_per_receipt_usd * 1000)}</td>`
    + '</tr>';
}

/** The first table: the models alone, with the plain prompt, on the receipts no build step saw. */
export function resultsTable(results) {
  const head = headRow([
    ['c-model', 'Model<span class="h-long">, plain prompt</span>', ''],
    ['c-exact', `Exact on ${results.n}`, `Share of the ${results.n} receipts with all four values exact. The vertical line is the pass mark of ${fmtScore(results.bar)}, the dot is the share, and the short horizontal line is the 95% interval`],
    ['c-ci', '95% interval', 'The range the true share most likely falls in, given these receipts (a Wilson score interval)'],
    ['c-cost', 'Per 1,000<span class="h-long"> receipts</span>', `Model cost of 1,000 receipts at Token Factory list prices of ${results.prices_date}, reasoning included`],
  ]);
  return `<table class="results viz" aria-label="The models alone, with the plain prompt, on ${results.n} receipts no build step saw"><thead><tr>${head}</tr></thead><tbody>${results.models.map((m) => row(m, results)).join('')}</tbody></table>`;
}

const verdict = (g) => (g.licensed ? (g.ships ? 'passed and shipped' : 'passed') : 'did not pass');

const examCell = (g, cls = '') => `<span class="exam-cell${cls}">${lock(g.licensed, 15, g.ships)}<span class="num">${g.exam.correct}</span><span class="num of">/${g.exam.cases}</span></span>`;

function checkRow(r, results) {
  const gain = r.exam.correct - r.before.exam.correct;
  const tip = `${r.name}: ${r.exam.correct} of ${r.exam.cases} with the checks, ${verdict(r)}. ${r.before.exam.correct} without them, ${verdict(r.before)}`;
  return `<tr class="${r.ships ? 'is-ships' : ''}">`
    + modelCell(r.model, r.name, r.id)
    + `<td class="c-with" data-tip="${esc(tip)}"><span class="exact">${gainMini(r, results)}${examCell(r)}</span></td>`
    + `<td class="c-without">${examCell(r.before, ' is-before')}</td>`
    + `<td class="c-gain num">${gain > 0 ? '+' : gain < 0 ? '\u2212' : ''}${Math.abs(gain)}</td>`
    + '</tr>';
}

/** The second table: each rung's exam with the rule checks beside the same exam without them. */
export function checksTable(results) {
  const b = results.build;
  const cases = b.rungs[0].exam.cases;
  const head = headRow([
    ['c-model', 'Model', ''],
    ['c-with', 'With checks', `The build's exam with the rule checks, on ${cases} receipts it never saw. The vertical line is the pass mark of ${fmtScore(results.bar)}, the gray tick is the same exam without the checks, and the dot is the exam with them. A closed lock means passed, an open one did not pass`],
    ['c-without', 'Without', `The same ${cases} exam receipts in the build without the checks`],
    ['c-gain', 'Gain', 'Exam receipts right with the checks, minus those right without them'],
  ]);
  return `<table class="results viz" aria-label="The build's exam on ${cases} receipts, with the rule checks and without them"><thead><tr>${head}</tr></thead><tbody>${b.rungs.map((r) => checkRow(r, results)).join('')}</tbody></table>`;
}

export { esc };
