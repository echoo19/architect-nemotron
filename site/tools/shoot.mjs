#!/usr/bin/env node
/*
 * Screenshots of the built site (site/dist) into site/shots, with one headless
 * Chrome for every capture, plus the checks a reviewer would otherwise do by eye:
 * console errors, sideways scroll, text outside its chart and fonts that did not
 * load.
 *
 *   node site/build.mjs && node site/tools/shoot.mjs [--file] [--only=name,name]
 *
 * --file opens the page from file:// (the local preview); without it a small
 * static server on 127.0.0.1 serves site/dist, as GitHub Pages would.
 *
 * Needs puppeteer-core (not a dependency of this repository): set PUPPETEER_DIR
 * to a folder whose node_modules holds it, for example after
 * `npm install --prefix <dir> puppeteer-core`. CHROME points at the browser;
 * the default is Chrome's usual Windows path.
 */

import { createReadStream, existsSync, mkdirSync, statSync } from 'node:fs';
import http from 'node:http';
import { createRequire } from 'node:module';
import { dirname, extname, join, normalize } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const SITE = join(dirname(fileURLToPath(import.meta.url)), '..');
const DIST = join(SITE, 'dist');
const OUT = join(SITE, 'shots');
const CHROME = process.env.CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const require = createRequire(join(process.env.PUPPETEER_DIR || process.cwd(), 'package.json'));
const puppeteer = require('puppeteer-core');
const useFile = process.argv.includes('--file');
const only = (process.argv.find((a) => a.startsWith('--only=')) || '').slice(7).split(',').filter(Boolean);
if (!existsSync(join(DIST, 'index.html'))) throw new Error('build the site first: node site/build.mjs');

const TYPES = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.json': 'application/json', '.woff2': 'font/woff2', '.svg': 'image/svg+xml', '.txt': 'text/plain; charset=utf-8',
};
let server = null;
let base;
if (useFile) {
  base = pathToFileURL(join(DIST, 'index.html')).href;
} else {
  server = http.createServer((req, res) => {
    let path = decodeURIComponent(new URL(req.url, 'http://x').pathname);
    if (path.endsWith('/')) path += 'index.html';
    const file = normalize(join(DIST, path));
    if (!file.startsWith(normalize(DIST)) || !existsSync(file) || statSync(file).isDirectory()) { res.writeHead(404); res.end(); return; }
    res.writeHead(200, { 'content-type': TYPES[extname(file)] || 'application/octet-stream', 'cache-control': 'no-store' });
    createReadStream(file).pipe(res);
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  base = `http://127.0.0.1:${server.address().port}/index.html`;
}
mkdirSync(OUT, { recursive: true });

const browser = await puppeteer.launch({
  executablePath: CHROME, headless: true,
  args: ['--hide-scrollbars', '--force-color-profile=srgb', '--disable-gpu', '--no-first-run', '--no-default-browser-check', '--disable-extensions'],
});
const problems = [];
const shots = [];
try {
  const page = await browser.newPage();
  page.on('console', (m) => { if (m.type() === 'error' || m.type() === 'warning') problems.push(`console ${m.type()}: ${m.text()}`); });
  page.on('pageerror', (e) => problems.push(`page error: ${e.message}`));
  page.on('requestfailed', (r) => { if (!/youtube/.test(r.url())) problems.push(`request failed: ${r.url()}`); });

  const settle = (ms = 250) => new Promise((r) => setTimeout(r, ms));
  async function open({ width, height = 900, theme = 'light', query = '' }) {
    await page.setViewport({ width, height, deviceScaleFactor: 1 });
    const q = new URLSearchParams(query);
    q.set('theme', theme);
    await page.goto('about:blank');
    await page.goto(`${base}?${q}`, { waitUntil: 'load' });
    await page.evaluate(async () => { await document.fonts.ready; });
    await page.waitForFunction(() => document.documentElement.classList.contains('is-ready'), { timeout: 5000 }).catch(() => problems.push(`${width}px ${theme}: site.js never finished starting`));
    await settle();
  }
  async function shot(name, { full = false } = {}) {
    if (only.length && !only.includes(name)) return;
    const path = join(OUT, `${name}.png`);
    await page.screenshot({ path, fullPage: full });
    shots.push(path);
  }
  async function scrollTo(selector, offset) {
    const y = await page.evaluate((sel, off) => document.querySelector(sel).getBoundingClientRect().top + window.scrollY - off, selector, offset);
    await page.evaluate((top) => window.scrollTo(0, top), y);
    await settle(150);
  }
  async function audit(label) {
    const found = await page.evaluate(() => {
      const out = [];
      if (document.documentElement.scrollWidth > window.innerWidth + 1) out.push(`scrolls sideways: ${document.documentElement.scrollWidth} > ${window.innerWidth}`);
      for (const svg of document.querySelectorAll('svg.viz-svg')) {
        if (svg.closest('.video-poster') || !svg.getClientRects().length) continue;
        const sb = svg.getBoundingClientRect();
        for (const t of svg.querySelectorAll('text')) {
          const b = t.getBoundingClientRect();
          if (!b.width) continue;
          if (b.left < sb.left - 1 || b.right > sb.right + 1 || b.top < sb.top - 1 || b.bottom > sb.bottom + 1) out.push(`text outside its chart: "${t.textContent}"`);
        }
      }
      for (const el of document.querySelectorAll('body *')) {
        if (!el.getClientRects().length || el.closest('.sr-only, svg') || el.localName === 'svg') continue;
        const r = el.getBoundingClientRect();
        if (r.right > window.innerWidth + 1 && getComputedStyle(el).position !== 'fixed' && !el.closest('.results-frame')) out.push(`past the right edge: <${el.localName} class="${el.className}"> ends at ${Math.round(r.right)}`);
      }
      for (const family of ['Inter', 'JetBrains Mono']) {
        const faces = [...document.fonts].filter((f) => f.family.replace(/"/g, '') === family);
        if (!faces.some((f) => f.status === 'loaded')) out.push(`font not loaded: ${family} (${faces.map((f) => f.status).join(', ') || 'no face'})`);
      }
      return out.slice(0, 12);
    });
    for (const f of found) problems.push(`${label}: ${f}`);
  }

  // Desktop, light: the first screen, the whole page, then each part at viewport size.
  await open({ width: 1440 });
  await audit('1440 light');
  await shot('desktop-top');
  await shot('desktop-full', { full: true });
  await scrollTo('#stops', 92);
  await shot('desktop-stops');
  await scrollTo('#trial', 76);
  await shot('desktop-trial');
  await scrollTo('#try', 76);
  await shot('desktop-try');
  await scrollTo('#results', 76);
  await shot('desktop-results');
  await scrollTo('.results-next', 92);
  await shot('desktop-checks');

  // Dark.
  await open({ width: 1440, theme: 'dark' });
  await audit('1440 dark');
  await shot('desktop-dark-top');
  await scrollTo('#stops', 92);
  await shot('desktop-dark-stops');
  await scrollTo('#try', 76);
  await shot('desktop-dark-try');
  await scrollTo('#results', 76);
  await shot('desktop-dark-results');
  await scrollTo('.results-next', 92);
  await shot('desktop-dark-checks');

  // Phone width: the whole page, then one screen per part so each reads at full size.
  await open({ width: 390, height: 844, query: 'os=mac-arm64' });
  await audit('390 light');
  await shot('phone-full', { full: true });
  await shot('phone-top');
  await scrollTo('#stops', 76);
  await shot('phone-stops');
  await scrollTo('#trial', 68);
  await shot('phone-trial');
  await scrollTo('#try', 68);
  await shot('phone-try');
  await scrollTo('#results', 68);
  await shot('phone-results');
  await scrollTo('.results-next', 84);
  await shot('phone-checks');
  await open({ width: 390, height: 844, theme: 'dark', query: 'os=mac-arm64' });
  await audit('390 dark');
  await shot('phone-dark-top');
  await scrollTo('#stops', 76);
  await shot('phone-dark-stops');
  await scrollTo('#results', 68);
  await shot('phone-dark-results');
  await scrollTo('.results-next', 84);
  await shot('phone-dark-checks');
} finally {
  await browser.close();
  if (server) server.close();
}

for (const s of shots) console.log(`shot    ${s}`);
if (problems.length) {
  console.log(`\n${problems.length} problems:`);
  for (const p of problems) console.log(`  ${p}`);
  process.exitCode = 1;
} else {
  console.log('\nno problems found');
}
