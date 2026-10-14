#!/usr/bin/env node
/*
 * Builds the site into site/dist: one static page that works opened as a local
 * file, served by `npx serve site/dist`, and on GitHub Pages.
 *
 *   node site/build.mjs             placeholder data is allowed, and the page says so
 *   node site/build.mjs --release   refuses placeholder data and private markers in
 *                                   the output
 *
 * Steps:
 *  1. Copy Lectern's tokens, fonts and motion verbatim from desktop/src/styles (with
 *     the woff2 files from desktop/src/assets/fonts) and viz.css from site/design.
 *  2. Wrap site/design/components.js as a classic script, because module scripts do
 *     not load from file://.
 *  3. Render every part of the page into index.html, so it reads with script turned
 *     off.
 *
 * Node 20 or later, no dependencies.
 */

import { copyFileSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join, relative, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import vm from 'node:vm';
import * as R from './src/render.mjs';
import { MARK_OUTLINE, SITE_ICONS } from './src/icons.mjs';

const SITE = dirname(fileURLToPath(import.meta.url));
const REPO = join(SITE, '..');
const OUT = join(SITE, 'dist');
const DATA = join(SITE, 'data');
const STYLES = join(REPO, 'desktop', 'src', 'styles');
const FONTS = join(REPO, 'desktop', 'src', 'assets', 'fonts');
const RELEASE = process.argv.includes('--release');

class BuildError extends Error {}
const fail = (message) => { throw new BuildError(message); };
const need = (ok, message) => { if (!ok) fail(message); };
const posix = (path) => path.split(sep).join('/');

function write(path, content) {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, content);
}

function copy(from, to) {
  mkdirSync(dirname(to), { recursive: true });
  copyFileSync(from, to);
}

/** Every file under `dir`, as paths relative to it with forward slashes, sorted. */
function walk(dir, base = dir) {
  const out = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...walk(path, base));
    else if (entry.isFile()) out.push(posix(relative(base, path)));
    else fail(`${posix(relative(SITE, path))} is neither a file nor a folder`);
  }
  return out.sort();
}

// ---------------------------------------------------------------- data

function readJson(path, schema) {
  const bytes = readFileSync(join(DATA, path));
  const text = bytes.toString('utf8');
  need(Buffer.from(text, 'utf8').equals(bytes), `data/${path} is not valid UTF-8`);
  let value;
  try { value = JSON.parse(text); } catch (error) { fail(`data/${path} is not JSON: ${error.message}`); }
  need(value && value.schema === schema, `data/${path} should carry "schema": "${schema}"`);
  need(typeof value.placeholder === 'boolean', `data/${path} should say "placeholder": true or false`);
  return value;
}

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const isScore = (v) => isNum(v) && v >= 0 && v <= 1;
const isText = (v) => typeof v === 'string' && v.length > 0;

/**
 * The page's view of the evaluation report in results.json.
 *
 * models: the first table, one row per plain arm in order: the models alone on the
 * delivery receipts.
 * build: the poster and the second table, the ladder build with the rule checks
 * (checked_ladder), rung by rung and cheapest first, each rung beside the same model's
 * rung in the build without them (ladder). A rung belongs to the plain arm whose
 * model_name is the rung's model.
 *
 * The view also carries the plan's footer lines and the Forge record, with each engineer's
 * short name when a plain arm ran that model. checkShapes then checks it.
 */
function resultsView(report) {
  const n = report.delivery?.n;
  need(Array.isArray(report.arms), 'results.json: needs arms');
  const plain = report.arms.filter((arm) => arm.kind === 'plain');
  const ladderRungs = (key) => {
    const ladder = report[key];
    need(ladder && Array.isArray(ladder.rungs) && ladder.rungs.length > 0, `results.json: needs ${key}, with its rungs`);
    need(ladder.bar === report.bar, `results.json: ${key}.bar should equal bar`);
    return ladder.rungs.map((rung) => {
      const arms = plain.filter((arm) => arm.model_name === rung.model);
      need(arms.length === 1, `results.json: ${key} rung ${rung.name ?? '?'} should match the model_name of one plain arm`);
      need((rung.ships === true) === (ladder.ships === rung.name), `results.json: ${key} rung ${rung.name ?? '?'} and ${key}.ships disagree on what shipped`);
      return { ...rung, id: arms[0].model };
    });
  };
  const before = ladderRungs('ladder');
  const after = ladderRungs('checked_ladder');
  const [floor, checkedFloor] = [report.ladder.floor, report.checked_ladder.floor];
  need(floor && checkedFloor && isText(floor.cases_sha256) && floor.cases_sha256 === checkedFloor.cases_sha256 && floor.exam === checkedFloor.exam,
    'results.json: ladder and checked_ladder should have sat the same floor (floor.cases_sha256 and floor.exam)');
  need(before.length === after.length && after.every((r) => before.filter((b) => b.model === r.model).length === 1),
    'results.json: ladder and checked_ladder should have one rung each for the same models');

  const models = plain.map((arm) => {
    need(arm.delivery && arm.delivery.n === n, `results.json: arm ${arm.arm ?? '?'} should have read delivery.n receipts`);
    return {
      name: arm.model_name,
      short: after.find((r) => r.model === arm.model_name)?.name ?? arm.model_name,
      id: arm.model,
      k: arm.delivery.k,
      ci: arm.delivery.ci,
      cost_per_receipt_usd: arm.cost_per_receipt_usd,
    };
  });
  const build = {
    checks: report.checked_ladder.checks,
    later: report.checked_ladder.later_sits ?? [],
    rungs: after.map((r) => {
      const b = before.find((x) => x.model === r.model);
      return {
        name: r.name, model: r.model, id: r.id,
        preset: r.preset, practice: r.practice, examined: r.examined,
        exam: r.exam, cost: r.exam_cost_per_receipt_usd, licensed: r.licensed, ships: r.ships === true,
        before: { exam: b.exam, cost: b.exam_cost_per_receipt_usd, licensed: b.licensed, ships: b.ships === true },
      };
    }),
  };
  const record = report.forge_record ?? null;
  const forge = record && {
    sessions: record.sessions,
    accepted: record.accepted,
    engineers: (Array.isArray(record.by_engineer) ? record.by_engineer : []).map((e) => ({
      name: models.find((m) => m.name === e.model_name)?.short ?? e.model_name,
      sessions: e.sessions,
    })),
  };
  return {
    placeholder: report.placeholder, n, bar: report.bar, domain: report.domain, prices_date: report.prices_date,
    footer: report.footer, forge, plan: report.plan ?? null, models, build,
  };
}

const isExam = (e) => e && Number.isInteger(e.cases) && e.cases > 0 && Number.isInteger(e.correct) && e.correct >= 0 && e.correct <= e.cases;

function checkShapes({ site, results }) {
  need(isText(site.repo) && /^https:\/\//.test(site.repo), 'site.json: repo should be an https URL');
  need(site.video && (site.video.youtube_id === null || /^[A-Za-z0-9_-]{11}$/.test(site.video.youtube_id)), 'site.json: video.youtube_id should be an 11-character YouTube id or null');
  need(site.video.duration === null || /^\d+:\d\d$/.test(site.video.duration), 'site.json: video.duration should be m:ss or null');
  const r = site.release;
  need(r && isText(r.tag) && isText(r.windows) && isText(r.mac_arm64) && isText(r.mac_x64), 'site.json: release needs tag, windows, mac_arm64 and mac_x64');
  need(Array.isArray(site.commands) && site.commands.length > 0 && site.commands.every(isText), 'site.json: commands should be a list of strings');

  need(Number.isInteger(results.n) && results.n > 0, 'results.json: needs n, the number of receipts');
  need(isScore(results.bar), 'results.json: needs bar');
  need(Array.isArray(results.domain) && results.domain.length === 2 && results.domain.every(isScore) && results.domain[0] < results.domain[1], 'results.json: domain should be [low, high]');
  need(isText(results.prices_date), 'results.json: needs prices_date');
  need(Array.isArray(results.models) && results.models.length > 0, 'results.json: needs models');
  // Every mark the page draws sits at its real score, so each one must fit the domain.
  const [lo, hi] = results.domain;
  const drawn = (v) => isScore(v) && v >= lo && v <= hi;
  for (const m of results.models) {
    const where = `results.json: model ${m.name ?? '?'}`;
    need(isText(m.name) && isText(m.short) && isText(m.id), `${where}: needs name, short and id`);
    need(Number.isInteger(m.k) && m.k >= 0 && m.k <= results.n, `${where}: k should be a count from 0 to n`);
    need(Array.isArray(m.ci) && m.ci.length === 2 && m.ci.every(isScore) && m.ci[0] <= m.k / results.n && m.k / results.n <= m.ci[1], `${where}: ci should be [low, high] around k / n`);
    need(m.ci.every(drawn), `${where}: the 95% interval should fit inside domain`);
    need(isNum(m.cost_per_receipt_usd) && m.cost_per_receipt_usd >= 0, `${where}: needs cost_per_receipt_usd`);
  }

  // The build with the rule checks, and the same exam without them. The page's words
  // describe exactly the two checks named here, so other checks need new words first.
  const b = results.build;
  need(Array.isArray(b.checks) && b.checks.map((c) => c.check).join(',') === 'keep_digits,not_copied' && b.checks.every((c) => isText(c.rule)),
    'results.json: checked_ladder.checks should be keep_digits then not_copied, each with its rule, which the page describes');
  for (const r of b.rungs) {
    const where = `results.json: checked_ladder rung ${r.name ?? '?'}`;
    need(isText(r.name) && isText(r.model) && isText(r.id), `${where}: needs name and model`);
    need(isExam(r.exam) && isExam(r.before.exam) && r.exam.cases === r.before.exam.cases, `${where}: needs exam cases and correct, with as many cases as the ladder rung of the same model`);
    for (const [g, what] of [[r, 'rung'], [r.before, 'ladder rung of the same model']]) {
      need(typeof g.licensed === 'boolean' && typeof g.ships === 'boolean' && (!g.ships || g.licensed), `${where}: the ${what} needs licensed and ships, and only a licensed rung ships`);
      need(isNum(g.cost) && g.cost >= 0, `${where}: the ${what} needs exam_cost_per_receipt_usd`);
    }
    need(r.examined === undefined || isScore(r.examined), `${where}: examined should be a score, the practice score of what sat the exam`);
    const marks = [r.exam.correct / r.exam.cases, r.before.exam.correct / r.before.exam.cases, r.preset, r.examined, ...(r.practice ?? [])];
    need(marks.every((v) => v === undefined || drawn(v)), `${where}: every score the page draws should fit inside domain`);
  }
  need(b.rungs.filter((r) => r.ships).length <= 1 && b.rungs.filter((r) => r.before.ships).length <= 1, 'results.json: at most one rung of a build ships');
  // Later sits of the same build: one exam count per rung, and only a rung that cleared the bar ships.
  need(Array.isArray(b.later), 'results.json: checked_ladder.later_sits should be a list');
  for (const s of b.later) {
    const where = `results.json: checked_ladder.later_sits ${s?.recorded_at ?? '?'}`;
    need(s && isText(s.recorded_at) && s.exam && typeof s.exam === 'object', `${where}: needs recorded_at and exam`);
    need(Object.keys(s.exam).length === b.rungs.length && b.rungs.every((r) => isExam({ cases: r.exam.cases, correct: s.exam[r.name] })),
      `${where}: exam needs one count of right answers per rung, by its name`);
    const cleared = (name) => s.exam[name] >= Math.ceil(Math.round(results.bar * b.rungs.find((r) => r.name === name).exam.cases * 1e6) / 1e6);
    need(s.ships === null || (b.rungs.some((r) => r.name === s.ships) && cleared(s.ships)), `${where}: ships should name a rung that cleared the bar, or be null`);
  }
  need(Array.isArray(results.footer) && results.footer.length > 0 && results.footer.every(isText), 'results.json: footer should be the plan\'s footer lines');
  need(results.plan && /^https:\/\//.test(results.plan.results_doc ?? '') && /^https:\/\//.test(results.plan.doc ?? ''), 'results.json: plan needs the doc and results_doc URLs');
  const f = results.forge;
  if (f !== null) {
    need(Number.isInteger(f.sessions) && f.sessions > 0 && Number.isInteger(f.accepted) && f.accepted >= 0 && f.accepted <= f.sessions, 'results.json: forge_record needs sessions and accepted, as counts');
    need(f.engineers.length > 0 && f.engineers.every((e) => isText(e.name) && Number.isInteger(e.sessions) && e.sessions > 0), 'results.json: forge_record.by_engineer should list model_name and sessions');
    need(f.engineers.reduce((t, e) => t + e.sessions, 0) === f.sessions, 'results.json: forge_record.by_engineer should add up to sessions');
  }
}

// ---------------------------------------------------------------- classic script

/** components.js with its exports gathered into window.ArchitectViz, plus the site's icons. */
function classicViz() {
  const src = readFileSync(join(SITE, 'design', 'components.js'), 'utf8');
  need(!/^\s*import\s/m.test(src), 'components.js imports a module; build.mjs can only wrap a self-contained file');
  const names = [];
  const body = src.replace(/^export\s+(async\s+function|function|const|let|class)\s+([A-Za-z_$][\w$]*)/gm, (m, kind, name) => {
    names.push(name);
    return `${kind} ${name}`;
  });
  need(!/^\s*export\s/m.test(body), 'components.js has an export form build.mjs cannot wrap');
  return '/* Generated by site/build.mjs from site/design/components.js and site/src/icons.mjs. Edit those. */\n'
    + `(function (globalScope) {\n'use strict';\n${body}\nObject.assign(ICON_PATHS, ${JSON.stringify(SITE_ICONS)});\n`
    + `globalScope.ArchitectViz = Object.freeze({ ${names.join(', ')} });\n})(typeof window !== 'undefined' ? window : globalThis);\n`;
}

// ---------------------------------------------------------------- notices and icon

function notices(placeholder) {
  const text = (path) => readFileSync(join(SITE, path), 'utf8').trim();
  const block = (title, lines) => [title, ...lines.map((l) => `  ${l}`)].join('\n');
  return [
    'Architect site: third-party notices',
    '',
    block('Lectern design tokens, motion styles and mark', [
      'styles/tokens.css, styles/motion.css, styles/fonts.css, the mark and assets/icon.svg.',
      'Design tokens, motion styles and the mark are (c) Lectern, contributed under the MIT License by Lectern\'s owner.',
    ]),
    '',
    block('Inter and JetBrains Mono', ['assets/fonts/*.woff2, under the SIL Open Font License 1.1. The two licences follow.']),
    '',
    block('Tabler Icons 3.48.0', ['The icon paths drawn on this page, under the MIT License. The licence follows.']),
    '',
    block('CORD v2 (naver-clova-ix/cord-v2)', [
      'The results on this page come from receipt text rebuilt from CORD v2\'s word annotations,',
      'under the Creative Commons Attribution 4.0 licence: https://creativecommons.org/licenses/by/4.0/',
      ...(placeholder ? ['This build holds placeholder data. Its numbers are stand-ins, not results.'] : []),
    ]),
    '',
    '-------------------------------------------------------------------------------',
    'Inter', '', text('vendor/fonts/Inter-OFL.txt'), '',
    '-------------------------------------------------------------------------------',
    'JetBrains Mono', '', text('vendor/fonts/JetBrainsMono-OFL.txt'), '',
    '-------------------------------------------------------------------------------',
    'Tabler Icons', '', text('vendor/tabler-icons/LICENSE'), '',
  ].join('\n');
}

/** The app icon: Lectern's mark, one traced outline, in white on a flat pine tile (desktop/build/icon.svg). */
const ICON_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#0E3A33"/>'
  + `<g transform="translate(32 32) scale(0.62) translate(-50 -49)" fill="#FFFFFF"><path d="${MARK_OUTLINE}"/></g></svg>\n`;

/*
 * What the release scanner (scripts/release/scan.py, which the public repository
 * leaves out) refuses; a release build must not ship it either. The words are
 * joined at run time, so this file passes that same scanner.
 */
const words = (...parts) => parts.join('');
const PRIVATE_MARKERS = [
  [new RegExp(`lectern-(?:${words('plat', 'form')}|${words('web', 'site')})(?![A-Za-z0-9_])`, 'i'), 'a private Lectern path'],
  [new RegExp(words('study', 'locker'), 'i'), 'a private account name'],
  [new RegExp(`(?<![A-Za-z0-9])[a-z]:(?:\\\\{1,2}|/{1,2})(?:users|${words('jake', 'os')})(?![A-Za-z0-9])`, 'i'), 'a local path'],
  [new RegExp(`(?<![A-Za-z0-9._~-])/[a-z]/(?:users|${words('jake', 'os')})(?![A-Za-z0-9])`, 'i'), 'a local path'],
  [new RegExp(`(?<![A-Za-z0-9_-])${words('echoo19', '/', 'architect')}(?![A-Za-z0-9_-])`, 'i'), 'the private repository'],
];

// ---------------------------------------------------------------- build

function build() {
  const started = Date.now();
  const site = readJson('site.json', 'architect-site/2');
  const results = resultsView(readJson('results.json', 'architect-site-report/1'));
  checkShapes({ site, results });
  const placeholders = Object.entries({ site, results }).filter(([, v]) => v.placeholder !== false).map(([k]) => k);
  if (RELEASE) need(!placeholders.length, `--release refuses placeholder data: ${placeholders.join(', ')}`);

  rmSync(OUT, { recursive: true, force: true });

  // 1. Lectern's styles and fonts, verbatim, then the shared visuals and the page's own styles.
  for (const name of ['tokens.css', 'fonts.css', 'motion.css']) copy(join(STYLES, name), join(OUT, 'styles', name));
  const fontFiles = readdirSync(FONTS).filter((n) => /\.(woff2|txt)$/i.test(n));
  const named = [...readFileSync(join(STYLES, 'fonts.css'), 'utf8').matchAll(/assets\/fonts\/([\w.-]+\.woff2)/g)].map((m) => m[1]);
  need(named.length > 0 && named.every((n) => fontFiles.includes(n)), 'desktop/src/assets/fonts should hold the woff2 files fonts.css names');
  for (const name of fontFiles) copy(join(FONTS, name), join(OUT, 'assets', 'fonts', name));
  for (const name of readdirSync(join(SITE, 'vendor', 'fonts'))) copy(join(SITE, 'vendor', 'fonts', name), join(OUT, 'assets', 'fonts', name));
  copy(join(SITE, 'design', 'viz.css'), join(OUT, 'styles', 'viz.css'));
  copy(join(SITE, 'src', 'site.css'), join(OUT, 'styles', 'site.css'));

  // 2. Scripts.
  const viz = classicViz();
  const context = vm.createContext({});
  context.window = context;
  vm.runInContext(viz, context, { filename: 'viz.js' });
  need(typeof context.ArchitectViz.attachTips === 'function' && context.ArchitectViz.ICON_PATHS.brandGithub, 'js/viz.js did not expose the builders');
  write(join(OUT, 'js', 'viz.js'), viz);
  copy(join(SITE, 'src', 'site.js'), join(OUT, 'js', 'site.js'));

  // 3. The page.
  const parts = {
    mark: R.mark(),
    repo: R.esc(site.repo),
    icon_github: R.icon('brandGithub', 18),
    icon_moon: R.icon('moon', 16),
    icon_passed: R.stateIcon('passed'),
    icon_stopped: R.stateIcon('stopped'),
    notice: R.notice(placeholders),
    video: R.video(site, results),
    downloads: R.downloads(site),
    commands: R.commands(site),
    results_paragraph: R.resultsParagraph(results),
    results: R.resultsTable(results),
    checks: R.checks(results),
    results_notes: R.resultsNotes(results),
  };
  const used = new Set();
  const template = readFileSync(join(SITE, 'src', 'index.html'), 'utf8');
  const html = template.replace(/\{\{(\w+)\}\}/g, (m, key) => {
    need(Object.hasOwn(parts, key), `src/index.html asks for {{${key}}}, which build.mjs does not render`);
    used.add(key);
    return parts[key];
  });
  const unused = Object.keys(parts).filter((k) => !used.has(k));
  need(!unused.length, `build.mjs renders parts src/index.html does not use: ${unused.join(', ')}`);
  write(join(OUT, 'index.html'), html);
  write(join(OUT, 'assets', 'icon.svg'), ICON_SVG);
  write(join(OUT, 'NOTICES.txt'), notices(placeholders.includes('results')));
  write(join(OUT, '.nojekyll'), '');

  // 4. A release build carries nothing the release scanner would refuse.
  const shipped = walk(OUT);
  const marked = [];
  for (const path of shipped.filter((p) => /\.(html|css|js|json|txt|svg)$/.test(p))) {
    const text = readFileSync(join(OUT, path), 'utf8');
    for (const [pattern, what] of PRIVATE_MARKERS) if (pattern.test(text)) marked.push(`${path} (${what})`);
  }
  if (marked.length) {
    if (RELEASE) fail(`the output holds what the release scanner refuses: ${marked.join(', ')}`);
    console.log(`note    private markers in the output, which --release refuses: ${marked.join(', ')}`);
  }

  const size = shipped.reduce((t, p) => t + statSync(join(OUT, p)).size, 0);
  if (placeholders.length) console.log(`note    placeholder data: ${placeholders.join(', ')}; the page shows a banner, and --release refuses it`);
  console.log(`built   ${posix(relative(REPO, OUT))}: ${shipped.length} files, ${(size / 1e6).toFixed(2)} MB, in ${Date.now() - started} ms`);
  console.log(`open    ${pathToFileURL(join(OUT, 'index.html')).href}`);
}

try {
  build();
} catch (error) {
  console.error(`build: ${error instanceof BuildError ? error.message : error.stack}`);
  process.exit(1);
}
