/** Files in a working folder (quick open) and handing a path to the person's editor. */
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const SKIP = new Set(['.git', 'node_modules', '__pycache__', '.venv', 'venv', 'dist', 'build', 'dist-electron',
  '.mypy_cache', '.pytest_cache', '.ruff_cache', '.next', '.turbo', 'coverage', '.idea', '.vscode']);
const LIMIT = 25_000;

/** Relative paths under `root`, breadth first, capped. */
export function listFiles(root: string): string[] {
  const out: string[] = [];
  const queue: string[] = [''];
  while (queue.length && out.length < LIMIT) {
    const rel = queue.shift()!;
    let entries: fs.Dirent[];
    try {
      entries = fs.readdirSync(path.join(root, rel), { withFileTypes: true });
    } catch {
      continue;
    }
    for (const entry of entries) {
      const child = rel ? `${rel}/${entry.name}` : entry.name;
      if (entry.isDirectory()) {
        if (!SKIP.has(entry.name) && !entry.name.startsWith('.')) queue.push(child);
      } else if (entry.isFile()) {
        out.push(child);
        if (out.length >= LIMIT) break;
      }
    }
  }
  return out;
}

const EDITORS: Array<{ id: string; label: string; command: string; app: string; cli: string }> = [
  { id: 'vscode', label: 'VS Code', command: 'code', app: 'Visual Studio Code', cli: 'Contents/Resources/app/bin/code' },
  { id: 'cursor', label: 'Cursor', command: 'cursor', app: 'Cursor', cli: 'Contents/Resources/app/bin/cursor' },
  { id: 'windsurf', label: 'Windsurf', command: 'windsurf', app: 'Windsurf', cli: 'Contents/Resources/app/bin/windsurf' },
  { id: 'zed', label: 'Zed', command: 'zed', app: 'Zed', cli: 'Contents/MacOS/cli' },
];

let cached: Array<{ id: string; label: string }> | null = null;

/** On a Mac an editor is often only an app, with its command never added to the PATH. */
function macApp(editor: (typeof EDITORS)[number]): string | null {
  if (process.platform !== 'darwin') return null;
  for (const root of ['/Applications', path.join(process.env.HOME ?? '', 'Applications')]) {
    const bundle = path.join(root, `${editor.app}.app`);
    if (fs.existsSync(bundle)) return bundle;
  }
  return null;
}

function onPath(command: string, env: NodeJS.ProcessEnv): boolean {
  const finder = process.platform === 'win32' ? 'where' : 'which';
  return spawnSync(finder, [command], { env, windowsHide: true }).status === 0;
}

export async function detectEditors(env: NodeJS.ProcessEnv): Promise<Array<{ id: string; label: string }>> {
  if (cached) return cached;
  cached = EDITORS.filter((e) => onPath(e.command, env) || macApp(e) !== null).map(({ id, label }) => ({ id, label }));
  return cached;
}

export async function openInEditor(id: string, target: string, line: number | undefined, env: NodeJS.ProcessEnv): Promise<boolean> {
  const editor = EDITORS.find((e) => e.id === id);
  if (!editor || !target || !fs.existsSync(target)) return false;
  const goto = line && id !== 'zed' ? ['--goto', `${target}:${line}`] : [line ? `${target}:${line}` : target];
  let command = editor.command;
  if (!onPath(command, env)) {
    const bundle = macApp(editor);
    if (!bundle) return false;
    const cli = path.join(bundle, editor.cli);
    if (fs.existsSync(cli)) command = cli;
    else {
      // No command line tool inside: let macOS open the file with the app.
      spawn('open', ['-a', bundle, target], { env, detached: true, stdio: 'ignore' }).unref();
      return true;
    }
  }
  try {
    const child = spawn(command, goto, { env, detached: true, stdio: 'ignore', shell: process.platform === 'win32', windowsHide: true });
    child.unref();
    return true;
  } catch {
    return false;
  }
}
