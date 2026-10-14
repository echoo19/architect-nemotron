/**
 * Integrated terminals (Claude Code's and Codex's terminal pane): one shell per
 * terminal id, in the conversation's folder, carried over the hub's WebSocket.
 * A terminal outlives a page reload; re-attaching replays its recent output.
 *
 *   page -> server  {t:'pty-start', id, cwd, cols, rows}  start (or re-attach)
 *                   {t:'pty-input', id, data} {t:'pty-resize', id, cols, rows} {t:'pty-stop', id}
 *   server -> page  {t:'pty-data', id, data} {t:'pty-exit', id, code} {t:'pty-replay', id, data}
 */
import { existsSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

type Pty = {
  onData(cb: (data: string) => void): void;
  onExit(cb: (e: { exitCode: number }) => void): void;
  write(data: string): void;
  resize(cols: number, rows: number): void;
  kill(): void;
};

const SCROLLBACK = 200_000;

interface Term { pty: Pty; buffer: string; exited: boolean }

export interface Shell { id: string; label: string; file: string; args: string[] }

/** Shells this computer has, in the order Codex lists them on Windows. */
export function availableShells(env: NodeJS.ProcessEnv): Shell[] {
  if (process.platform !== 'win32') {
    const login = env.SHELL || (process.platform === 'darwin' ? '/bin/zsh' : '/bin/bash');
    const out: Shell[] = [{ id: 'default', label: path.basename(login), file: login, args: ['-l'] }];
    for (const file of ['/bin/zsh', '/bin/bash', '/usr/bin/fish']) {
      if (file !== login && existsSync(file)) out.push({ id: path.basename(file), label: path.basename(file), file, args: ['-l'] });
    }
    return out;
  }
  const out: Shell[] = [];
  const pwsh = onPath('pwsh.exe', env);
  if (pwsh) out.push({ id: 'pwsh', label: 'PowerShell 7', file: pwsh, args: ['-NoLogo'] });
  out.push({ id: 'powershell', label: 'Windows PowerShell', file: 'powershell.exe', args: ['-NoLogo'] });
  out.push({ id: 'cmd', label: 'Command Prompt', file: 'cmd.exe', args: [] });
  const programFiles = [env.ProgramFiles, env['ProgramFiles(x86)'], env.LOCALAPPDATA && path.join(env.LOCALAPPDATA, 'Programs')].filter(Boolean) as string[];
  const bash = programFiles.map((root) => path.join(root, 'Git', 'bin', 'bash.exe')).find((file) => existsSync(file));
  if (bash) out.push({ id: 'gitbash', label: 'Git Bash', file: bash, args: ['--login', '-i'] });
  const wsl = path.join(env.SystemRoot || 'C:\\Windows', 'System32', 'wsl.exe');
  if (existsSync(wsl)) out.push({ id: 'wsl', label: 'WSL', file: wsl, args: [] });
  return out;
}

function onPath(name: string, env: NodeJS.ProcessEnv): string | null {
  for (const dir of (env.PATH || env.Path || '').split(path.delimiter)) {
    if (dir && existsSync(path.join(dir, name))) return path.join(dir, name);
  }
  return null;
}

export class Terminals {
  private terms = new Map<string, Term>();
  private shells: Shell[] | null = null;

  constructor(private readonly broadcast: (frame: Record<string, unknown>) => void, private readonly env: NodeJS.ProcessEnv) {}

  list(): Shell[] {
    this.shells ??= availableShells(this.env);
    return this.shells;
  }

  private shell(id?: string): Shell {
    const shells = this.list();
    return shells.find((s) => s.id === id) ?? shells.find((s) => s.id === 'powershell') ?? shells[0];
  }

  async start(id: string, cwd: string | undefined, cols: number, rows: number, shellId?: string): Promise<{ replay: string; exited: boolean }> {
    const existing = this.terms.get(id);
    if (existing) return { replay: existing.buffer, exited: existing.exited };
    // Loaded lazily so the server still runs where the native module is missing.
    const { spawn } = (await import('@lydell/node-pty')) as unknown as { spawn: (file: string, args: string[], opts: Record<string, unknown>) => Pty };
    const { file, args } = this.shell(shellId);
    const pty = spawn(file, args, {
      name: 'xterm-256color',
      cols: Math.max(20, cols || 80),
      rows: Math.max(5, rows || 24),
      cwd: cwd && existsSync(cwd) ? cwd : os.homedir(),
      env: { ...this.env, TERM: 'xterm-256color', COLORTERM: 'truecolor' },
    });
    const term: Term = { pty, buffer: '', exited: false };
    this.terms.set(id, term);
    pty.onData((data) => {
      term.buffer = (term.buffer + data).slice(-SCROLLBACK);
      this.broadcast({ t: 'pty-data', id, data });
    });
    pty.onExit(({ exitCode }) => {
      term.exited = true;
      this.broadcast({ t: 'pty-exit', id, code: exitCode });
    });
    return { replay: '', exited: false };
  }

  input(id: string, data: string): void {
    const term = this.terms.get(id);
    if (term && !term.exited) term.pty.write(data);
  }

  resize(id: string, cols: number, rows: number): void {
    const term = this.terms.get(id);
    if (term && !term.exited && cols > 0 && rows > 0) {
      try {
        term.pty.resize(cols, rows);
      } catch {
        /* resizing a closing pty */
      }
    }
  }

  stop(id: string): void {
    const term = this.terms.get(id);
    if (!term) return;
    this.terms.delete(id);
    try {
      term.pty.kill();
    } catch {
      /* already gone */
    }
  }

  stopAll(): void {
    for (const id of [...this.terms.keys()]) this.stop(id);
  }
}
