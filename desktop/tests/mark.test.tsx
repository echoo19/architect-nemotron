/**
 * Lectern's mark (components/ui/Working.tsx). A still render draws ONE traced outline,
 * the union of board, stem and foot, so no seam shows at the joints. The working loader
 * keeps its parts because it animates them: board, foot, then the middle column last,
 * with the bars wiped 20 units, two inside the column, so a clip edge snapped to the
 * device-pixel grid lands under solid ink. When motion is reduced it holds the outline
 * instead. The geometry checks sample the mark's space densely, so a slip in any path
 * string fails here.
 */
import fs from 'node:fs';
import path from 'node:path';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { BOARD, FOOT, INK_BOX, Mark, MARK_OUTLINE, WORKING_COLUMN, WorkingMark } from '../src/components/ui/Working';

/** The stem as Lectern's tenant config stores it; the outline and the column replace it. */
const STEM = 'M43 30.5 L57 27.5 L57 78.5 L43 81.5 Z';

type Pt = [number, number];
const polygon = (d: string): Pt[] => {
  const n = d.match(/-?\d+(?:\.\d+)?/g)!.map(Number);
  return Array.from({ length: n.length / 2 }, (_, i) => [n[2 * i], n[2 * i + 1]] as Pt);
};
/** Even-odd ray cast. */
const inside = (p: Pt[], x: number, y: number): boolean => {
  let hit = false;
  for (let i = 0, j = p.length - 1; i < p.length; j = i++) {
    const [xi, yi] = p[i];
    const [xj, yj] = p[j];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) hit = !hit;
  }
  return hit;
};
const area = (p: Pt[]) => Math.abs(p.reduce((s, [x, y], i) => s + x * p[(i + 1) % p.length][1] - p[(i + 1) % p.length][0] * y, 0)) / 2;

/** Points every 1/16 unit over the ink box and a margin, offset so that none lies on an edge. */
function differences(a: Pt[][], b: Pt[][]): number {
  let n = 0;
  for (let x = 24.0137; x < 76; x += 0.0625) {
    for (let y = 15.0071; y < 83; y += 0.0625) {
      if (a.some((p) => inside(p, x, y)) !== b.some((p) => inside(p, x, y))) n += 1;
    }
  }
  return n;
}

const paths = (html: string) => [...html.matchAll(/<path([^>]*)>/g)].map(([, attrs]) => ({
  d: /\bd="([^"]*)"/.exec(attrs)?.[1],
  cls: /\bclass="([^"]*)"/.exec(attrs)?.[1] ?? '',
}));

describe("Lectern's mark", () => {
  it('draws a still mark as one path, the traced outline, on the ink box', () => {
    const html = renderToStaticMarkup(<Mark className="brand-mark" />);
    expect(html).toContain(`viewBox="${INK_BOX}"`);
    expect(paths(html)).toEqual([{ d: MARK_OUTLINE, cls: '' }]);
  });

  it('traces exactly the union of board, stem and foot', () => {
    const outline = polygon(MARK_OUTLINE);
    expect(differences([outline], [BOARD, STEM, FOOT].map(polygon))).toBe(0);
    // One outline, eight corners, and the area of the union: 1349.125 square units.
    expect(outline).toHaveLength(8);
    expect(area(outline)).toBe(1349.125);
  });

  it('draws the loader as board, foot, then the column, the same silhouette as the outline', () => {
    const html = renderToStaticMarkup(<WorkingMark />);
    expect(paths(html)).toEqual([
      { d: BOARD, cls: 'lectern-working-board' },
      { d: FOOT, cls: 'lectern-working-foot' },
      { d: WORKING_COLUMN, cls: 'lectern-working-column' },
      // Hidden unless motion is reduced; then it stands in for the three parts.
      { d: MARK_OUTLINE, cls: 'lectern-working-still' },
    ]);
    expect(differences([BOARD, FOOT, WORKING_COLUMN].map(polygon), [polygon(MARK_OUTLINE)])).toBe(0);
    // The column holds the stem and both joints.
    expect(differences([polygon(WORKING_COLUMN)], [polygon(WORKING_COLUMN), polygon(STEM)])).toBe(0);
  });

  it('wipes each bar 20 units, two inside the column, and keeps the timings', () => {
    const css = fs.readFileSync(path.join(__dirname, '..', 'src', 'styles', 'motion.css'), 'utf8');
    const keyframes = Object.fromEntries([...css.matchAll(/@keyframes\s+([\w-]+)\s*\{([\s\S]*?)\n\}/g)]
      .map(([, name, body]) => [name, body.replace(/\s+/g, ' ').trim()]));
    // A bar holding retracted is hidden too, so the line state is the column's edges alone.
    expect(keyframes['lectern-retract-board']).toBe('0%, 6% { clip-path: inset(0 0 0 20px); visibility: hidden; } 40%, 60% { clip-path: inset(0 0 0 0); visibility: visible; } 94%, 100% { clip-path: inset(0 0 0 20px); visibility: hidden; }');
    expect(keyframes['lectern-retract-foot']).toBe('0%, 6% { clip-path: inset(0 20px 0 0); visibility: hidden; } 40%, 60% { clip-path: inset(0 0 0 0); visibility: visible; } 94%, 100% { clip-path: inset(0 20px 0 0); visibility: hidden; }');
    expect(css).toMatch(/\.lectern-working-board \{\s*animation: lectern-retract-board 1\.8s cubic-bezier\(0\.65, 0, 0\.35, 1\) infinite;\s*\}/);
    // The foot holds its first frame through its delay, so a new loader opens on the line.
    expect(css).toMatch(/\.lectern-working-foot \{\s*animation: lectern-retract-foot 1\.8s cubic-bezier\(0\.65, 0, 0\.35, 1\) infinite;\s*animation-delay: 0\.18s;\s*animation-fill-mode: backwards;\s*\}/);
    // The inset counts from each bar's own box: the board spans x 25..57, the foot 43..75,
    // and the column 43..57. Retracted, each bar ends 2 units inside the column.
    const xs = (d: string) => polygon(d).map(([x]) => x);
    const [board, foot, column] = [BOARD, FOOT, WORKING_COLUMN].map(xs);
    expect(Math.min(...board) + 20 - Math.min(...column)).toBe(2);
    expect(Math.max(...column) - (Math.max(...foot) - 20)).toBe(2);
  });

  it('holds the one outline, not the three parts, when motion is reduced', () => {
    const css = fs.readFileSync(path.join(__dirname, '..', 'src', 'styles', 'motion.css'), 'utf8').replace(/\s+/g, ' ');
    const hidden = css.indexOf('.lectern-working-still { display: none; }');
    const reduced = css.search(/@media \(prefers-reduced-motion: reduce\) \{[^@]*\.lectern-working-board, \.lectern-working-foot, \.lectern-working-column \{ display: none !important; \} \.lectern-working-still \{ display: inline; \}/);
    expect(hidden).toBeGreaterThanOrEqual(0);
    // Same specificity, so the reduced-motion rule has to come later to win.
    expect(reduced).toBeGreaterThan(hidden);
  });

  it('is the outline the app icon draws, under the icon grid transform', () => {
    const svg = fs.readFileSync(path.join(__dirname, '..', 'build', 'icon.svg'), 'utf8');
    expect([...svg.matchAll(/<path\b[^>]*\bd="([^"]*)"/g)].map(([, d]) => d)).toEqual([MARK_OUTLINE]);
    expect(svg).toContain('transform="translate(512 512) scale(7.2) translate(-50 -49)"');
  });

  it('is the geometry the site copies by hand', () => {
    const read = (...parts: string[]) => fs.readFileSync(path.join(__dirname, '..', '..', ...parts), 'utf8');
    expect(read('site', 'src', 'icons.mjs')).toContain(`MARK_OUTLINE = '${MARK_OUTLINE}'`);
    expect(read('site', 'design', 'proto.html')).toContain(`d="${MARK_OUTLINE}"`);
    // The site's in-chart loader: board, foot, the column, then the reduced-motion outline.
    const components = read('site', 'design', 'components.js');
    const at = [BOARD, FOOT, WORKING_COLUMN, MARK_OUTLINE].map((d) => components.indexOf(`d="${d}"`));
    expect(at.every((i) => i >= 0)).toBe(true);
    expect(at).toEqual([...at].sort((a, b) => a - b));
  });
});
