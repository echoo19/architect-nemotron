/**
 * electron-builder.yml's node-pty file set, checked with electron-builder's own matcher:
 * the per-platform package ships its native files and leaves its debug symbols (.pdb,
 * about 10 MB on Windows) out of the installer.
 */
import fs from 'node:fs';
import { createRequire } from 'node:module';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, expect, it } from 'vitest';

type Keep = (file: string, stat: fs.Stats) => boolean;
// app-builder-lib is electron-builder's engine; FileMatcher applies a file set's filter.
const { FileMatcher } = createRequire(import.meta.url)('app-builder-lib/out/fileMatcher') as {
  FileMatcher: new (from: string, to: string, expand: (text: string) => string, patterns: string[]) => { createFilter(): Keep };
};
const CONFIG = fs.readFileSync(path.resolve(__dirname, '..', 'electron-builder.yml'), 'utf8');

/** The filter of the file set copied from `from`, as electron-builder.yml lists it. */
function filterOf(from: string): string[] {
  const match = new RegExp(`- from: ${from}\\n\\s+to: ${from}\\n\\s+filter: (\\[.*\\])\\n`).exec(CONFIG);
  if (!match) throw new Error(`electron-builder.yml has no file set from ${from}`);
  return JSON.parse(match[1]) as string[];
}

let tmp = '';
beforeEach(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'arch-pack-'));
});
afterEach(() => fs.rmSync(tmp, { recursive: true, force: true }));

it("ships node-pty's native files without their debug symbols", () => {
  const from = path.join(tmp, '@lydell');
  const files = [
    'node-pty-win32-x64/conpty.node',
    'node-pty-win32-x64/conpty.pdb',
    'node-pty-win32-x64/conpty_console_list.node',
    'node-pty-win32-x64/conpty_console_list.pdb',
    'node-pty-win32-x64/package.json',
    'node-pty-darwin-arm64/pty.node',
    'node-pty-darwin-arm64/spawn-helper',
    'node-pty-linux-x64/pty.node',
  ];
  for (const file of files) {
    fs.mkdirSync(path.dirname(path.join(from, file)), { recursive: true });
    fs.writeFileSync(path.join(from, file), '');
  }
  const keep = new FileMatcher(from, 'node_modules/@lydell', (text) => text, filterOf('node_modules/@lydell')).createFilter();
  const kept = files.filter((file) => keep(path.join(from, file), fs.statSync(path.join(from, file))));
  expect(kept).toEqual(files.filter((file) => !file.endsWith('.pdb')));
});
