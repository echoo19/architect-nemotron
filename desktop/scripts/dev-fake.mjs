// `npm run dev:fake`: the dev server on the offline fixture workspace (see dev-fixture.mjs).
import { spawn } from 'node:child_process';

const root = process.env.ARCHITECT_FIXTURE_ROOT || (process.platform === 'win32' ? 'C:/t' : '/tmp/adt');
const child = spawn('npx', ['vite', '--port', '5173', '--strictPort'], {
  stdio: 'inherit',
  shell: process.platform === 'win32',
  env: { ...process.env, ARCHITECT_WORKSPACE: `${root}/adw`, ARCHITECT_DESKTOP_HOME: `${root}/adh` },
});
child.on('exit', (code) => process.exit(code ?? 0));
