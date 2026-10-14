/**
 * One Architect conversation: a `python -m architect.desktop.bridge` child process
 * speaking JSON lines (see architect/desktop/bridge.py for the protocol).
 *
 * The process outlives a page reload: the hub keeps it and replays its recent
 * messages to whoever attaches next.
 */
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { existsSync } from 'node:fs';
import path from 'node:path';

export type BridgeMessage = Record<string, unknown>;

export interface PythonCommand {
  command: string;
  args: string[];
  /** Extra environment for this process only. */
  env?: NodeJS.ProcessEnv;
}

/** How to start Python for the Architect checkout at `root`. */
export function pythonCommand(root: string, env: NodeJS.ProcessEnv = process.env): PythonCommand {
  const configured = env.ARCHITECT_PYTHON;
  if (configured) return { command: configured, args: ['-m', 'architect.desktop.bridge'] };
  const venv = process.platform === 'win32'
    ? path.join(root, '.venv', 'Scripts', 'python.exe')
    : path.join(root, '.venv', 'bin', 'python');
  if (existsSync(venv)) return { command: venv, args: ['-m', 'architect.desktop.bridge'] };
  // A packaged app: Electron runs a launcher that sets up Python once in the user's
  // app data with the uv the app ships with, then starts it (electron/python-launch.cjs).
  if (env.ARCHITECT_PACKAGED === '1' && env.ARCHITECT_LAUNCHER && env.ARCHITECT_NODE) {
    return { command: env.ARCHITECT_NODE, args: [env.ARCHITECT_LAUNCHER, root], env: { ELECTRON_RUN_AS_NODE: '1' } };
  }
  return { command: uvCommand(env), args: ['run', '--project', root, 'python', '-m', 'architect.desktop.bridge'] };
}

/** Where a packaged app's own files are, as Electron reports them (electron/main.ts). */
export interface PackagedApp {
  /** The app's binary; it also runs as Node (ELECTRON_RUN_AS_NODE=1). */
  execPath: string;
  /** process.resourcesPath: Architect's core (architect/) and uv (uv/) live here. */
  resourcesPath: string;
  /** The folder main.cjs runs from, inside app.asar. */
  mainDir: string;
  /** app.getPath('userData'). */
  userData: string;
}

/**
 * What a packaged app's bridge processes need: the launcher sets Python up with the
 * bundled uv (electron/python-launch.cjs), in the local app data on Windows, since a
 * signed app must not write inside itself; Pi runs on the app's own binary as Node.
 * Only ARCHITECT_* names, since terminals and the agent's commands inherit this.
 */
export function packagedEnvironment(app: PackagedApp, env: NodeJS.ProcessEnv = process.env): NodeJS.ProcessEnv {
  const win = process.platform === 'win32';
  const home = win && env.LOCALAPPDATA ? path.join(env.LOCALAPPDATA, 'Architect') : app.userData;
  const uv = path.join(app.resourcesPath, 'uv', win ? 'uv.exe' : 'uv');
  return {
    ARCHITECT_PYTHON_ENV: env.ARCHITECT_PYTHON_ENV || path.join(home, 'python'),
    ARCHITECT_PACKAGED: '1',
    ARCHITECT_NODE: app.execPath,
    // Unpacked from the asar archive (electron-builder.yml) so plain Node can run it.
    ARCHITECT_LAUNCHER: path.join(app.mainDir, 'python-launch.cjs').replace(`app.asar${path.sep}`, `app.asar.unpacked${path.sep}`),
    ARCHITECT_UV: env.ARCHITECT_UV || (existsSync(uv) ? uv : uvCommand(env)),
  };
}

/** uv by name, or where its installer puts it when the PATH does not say. */
export function uvCommand(env: NodeJS.ProcessEnv = process.env): string {
  if (env.ARCHITECT_UV) return env.ARCHITECT_UV;
  const home = env.HOME || env.USERPROFILE || '';
  const exe = process.platform === 'win32' ? 'uv.exe' : 'uv';
  const candidates = [path.join(home, '.local', 'bin', exe), path.join(home, '.cargo', 'bin', exe), '/opt/homebrew/bin/uv', '/usr/local/bin/uv'];
  const onPath = (env.PATH || env.Path || '').split(path.delimiter).some((dir) => dir && existsSync(path.join(dir, exe)));
  return onPath ? 'uv' : candidates.find((c) => existsSync(c)) ?? 'uv';
}

const STDERR_KEEP = 8_000;
const REQUEST_TIMEOUT_MS = 0; // turns can run for hours; the app cancels instead

export class BridgeProcess extends EventEmitter {
  readonly child: ChildProcessWithoutNullStreams;
  private buffer = '';
  private nextId = 1;
  private pending = new Map<number, { resolve: (v: unknown) => void; reject: (e: Error) => void }>();
  stderr = '';
  exited: { code: number | null } | null = null;
  ready: Promise<void>;

  constructor(root: string, env: NodeJS.ProcessEnv = process.env, cwd?: string) {
    super();
    const { command, args, env: extra } = pythonCommand(root, env);
    this.child = spawn(command, args, {
      cwd: cwd && existsSync(cwd) ? cwd : root,
      env: { ...env, ...extra, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1', PYTHONPATH: joinPath(root, env.PYTHONPATH) },
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    });
    this.child.stdout.setEncoding('utf8');
    this.child.stderr.setEncoding('utf8');
    this.child.stdout.on('data', (chunk: string) => this.onData(chunk));
    this.child.stderr.on('data', (chunk: string) => {
      this.stderr = (this.stderr + chunk).slice(-STDERR_KEEP);
    });
    this.ready = new Promise((resolve, reject) => {
      const onMessage = (message: BridgeMessage) => {
        if (message.type === 'ready') {
          this.off('message', onMessage);
          resolve();
        }
      };
      this.on('message', onMessage);
      this.once('exit', () => reject(new Error(this.failure())));
      this.child.once('error', (err) => reject(err));
    });
    this.ready.catch(() => undefined);
    this.child.on('error', (err) => {
      this.stderr += `\n${err.message}`;
    });
    this.child.on('exit', (code) => {
      this.exited = { code };
      for (const { reject } of this.pending.values()) reject(new Error('bridge_exited'));
      this.pending.clear();
      this.emit('exit', code);
    });
  }

  /** A short, secret-free reason the process stopped (the last stderr line that says something). */
  failure(): string {
    const lines = this.stderr.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
    return lines.at(-1) ?? 'bridge_exited';
  }

  private onData(chunk: string): void {
    this.buffer += chunk;
    let newline = this.buffer.indexOf('\n');
    while (newline !== -1) {
      const line = this.buffer.slice(0, newline).trim();
      this.buffer = this.buffer.slice(newline + 1);
      if (line) this.onLine(line);
      newline = this.buffer.indexOf('\n');
    }
  }

  private onLine(line: string): void {
    let message: BridgeMessage;
    try {
      message = JSON.parse(line) as BridgeMessage;
    } catch {
      return;
    }
    const id = message.id;
    if (typeof id === 'number' && this.pending.has(id) && ('result' in message || 'error' in message)) {
      const waiter = this.pending.get(id)!;
      this.pending.delete(id);
      if ('error' in message) {
        const error = message.error as { code?: string; message?: string };
        const err = new Error(error?.code ?? 'error') as Error & { code?: string; detail?: string };
        err.code = error?.code;
        err.detail = error?.message;
        waiter.reject(err);
      } else {
        waiter.resolve(message.result);
      }
      return;
    }
    this.emit('message', message);
  }

  /** Send one raw request line (the id is the caller's). */
  sendRaw(message: BridgeMessage): void {
    if (this.exited) return;
    this.child.stdin.write(JSON.stringify(message) + '\n');
  }

  /** A request this process answers; resolves with its result. */
  request<T = unknown>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    if (this.exited) return Promise.reject(new Error('bridge_exited'));
    const id = -this.nextId++; // negative ids are the server's own; the page uses positive ones
    return new Promise<T>((resolve, reject) => {
      this.pending.set(id, { resolve: resolve as (v: unknown) => void, reject });
      this.child.stdin.write(JSON.stringify({ id, method, params }) + '\n');
      if (REQUEST_TIMEOUT_MS > 0) setTimeout(() => reject(new Error('timeout')), REQUEST_TIMEOUT_MS);
    });
  }

  async stop(graceMs = 3_000): Promise<void> {
    if (this.exited) return;
    const done = new Promise<void>((resolve) => this.once('exit', () => resolve()));
    try {
      this.child.stdin.write(JSON.stringify({ id: 0, method: 'shutdown', params: {} }) + '\n');
      this.child.stdin.end();
    } catch {
      /* already closing */
    }
    const timer = setTimeout(() => this.child.kill(), graceMs);
    await done;
    clearTimeout(timer);
  }
}

function joinPath(root: string, existing?: string): string {
  return existing ? `${root}${path.delimiter}${existing}` : root;
}
