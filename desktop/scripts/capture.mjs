// Screenshot the real Electron window after running a script in the page.
//   node scripts/capture.mjs <out.png> [script.js] [waitMs]
// Expects the dev server (`npm run dev:fake`) on http://127.0.0.1:5173; the page's dev
// store is window.__architect and the wire is window.__wire.
import { spawnSync } from 'node:child_process';
import path from 'node:path';

const [out, script, wait] = process.argv.slice(2);
if (!out) {
  console.error('usage: node scripts/capture.mjs <out.png> [script.js] [waitMs]');
  process.exit(2);
}
const electron = path.join('node_modules', '.bin', process.platform === 'win32' ? 'electron.cmd' : 'electron');
// CAPTURE_SCALE=2 renders at 2x (for screenshots in docs).
const scale = process.env.CAPTURE_SCALE ? [`--force-device-scale-factor=${process.env.CAPTURE_SCALE}`] : [];
const result = spawnSync(electron, ['.', ...scale], {
  stdio: 'inherit',
  shell: process.platform === 'win32',
  timeout: 120_000,
  env: {
    ...process.env,
    ARCHITECT_DESKTOP_URL: process.env.ARCHITECT_DESKTOP_URL || 'http://127.0.0.1:5173',
    ARCHITECT_DESKTOP_CAPTURE: path.resolve(out),
    ...(script ? { ARCHITECT_DESKTOP_EVAL: path.resolve(script) } : {}),
    ARCHITECT_DESKTOP_WAIT: wait || '3000',
  },
});
process.exit(result.status ?? 1);
