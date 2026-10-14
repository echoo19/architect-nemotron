// Bundle Electron main (and the server it imports) into dist-electron/main.cjs.
import { build } from 'esbuild';
import { copyFileSync, mkdirSync } from 'node:fs';

mkdirSync('dist-electron', { recursive: true });
await build({
  entryPoints: ['electron/main.ts'],
  outfile: 'dist-electron/main.cjs',
  bundle: true,
  platform: 'node',
  format: 'cjs',
  target: 'node20',
  external: ['electron', 'vite', 'bufferutil', 'utf-8-validate', '@lydell/node-pty'],
  sourcemap: true,
  logLevel: 'info',
});
copyFileSync('electron/preload.cjs', 'dist-electron/preload.cjs');
copyFileSync('electron/python-launch.cjs', 'dist-electron/python-launch.cjs');
copyFileSync('build/icon.png', 'dist-electron/tray.png');
