/**
 * The control server: HTTP for small reads and prefs, one WebSocket that
 * multiplexes every open conversation's bridge process.
 *
 * WebSocket frames (JSON):
 *   page -> server  {t:'open', conv, cwd?}                start (or re-attach to) a conversation
 *                   {t:'req', conv, id, method, params}   forwarded to that bridge as is
 *                   {t:'close', conv}                     stop its process
 *   server -> page  {t:'opened', conv, replay, fresh}     replay = its recent messages
 *                   {t:'msg', conv, msg}                  a bridge message (event, ask, state, reply)
 *                   {t:'exit', conv, code, reason}        the process ended
 *                   {t:'convs', list}                     running conversations
 */
import fs from 'node:fs';
import type { IncomingMessage, Server, ServerResponse } from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { WebSocketServer, type WebSocket } from 'ws';
import { BridgeProcess, pythonCommand, type BridgeMessage } from './bridgeProcess.js';
import { isRequestAllowed } from './originGuard.js';
import { listFiles, openInEditor, detectEditors } from './workspaceFiles.js';
import { Terminals } from './terminals.js';
import { Routines, type Routine } from './routines.js';
import * as G from './git.js';

const REPLAY_LIMIT = 4_000;
// What a re-attached page is sent again; `forge` (the live board: stages, Forge's tool
// results and harness drafts) so a replayed build redraws its bar view and versions.
export const REPLAYED = new Set(['event', 'ask', 'ask_closed', 'state', 'turn_started', 'turn_finished', 'forge']);

export interface HubOptions {
  /** The Architect checkout (holds the `architect` package and its .venv). */
  root: string;
  /** Where the app keeps its own small files (prefs, drafts). */
  home?: string;
  env?: NodeJS.ProcessEnv;
  /** Tests: start something other than the real bridge. */
  spawn?: (root: string, env: NodeJS.ProcessEnv, cwd?: string) => BridgeProcess;
}

interface Conversation {
  id: string;
  process: BridgeProcess;
  log: BridgeMessage[];
  cwd?: string;
  started: number;
}

export function defaultHome(env: NodeJS.ProcessEnv = process.env): string {
  return env.ARCHITECT_DESKTOP_HOME || path.join(os.homedir(), '.architect', 'desktop');
}

export class Hub {
  readonly root: string;
  readonly home: string;
  readonly env: NodeJS.ProcessEnv;
  private readonly spawnBridge: NonNullable<HubOptions['spawn']>;
  private conversations = new Map<string, Conversation>();
  private sockets = new Set<WebSocket>();
  private wss: WebSocketServer | null = null;
  readonly terminals: Terminals;
  readonly routines: Routines;

  constructor(options: HubOptions) {
    this.root = options.root;
    this.home = options.home ?? defaultHome(options.env);
    this.env = options.env ?? process.env;
    this.spawnBridge = options.spawn ?? ((root, env, cwd) => new BridgeProcess(root, env, cwd));
    this.terminals = new Terminals((frame) => this.broadcast(frame), this.env);
    this.routines = new Routines(
      path.join(this.home, 'routines.json'),
      (routine) => this.startRoutine(routine),
      (list) => this.broadcast({ t: 'routines', list }),
    );
  }

  /**
   * One routine run: a conversation of its own in the routine's folder, started and
   * sent its instructions here, then added to the windows' conversation list.
   */
  private async startRoutine(routine: Routine): Promise<string> {
    const id = `routine-${Date.now().toString(36)}`;
    const { conversation } = this.open(id, routine.cwd);
    const bridge = conversation.process;
    await bridge.ready;
    const prefs = (readJson(path.join(this.home, 'prefs.json')) as Record<string, unknown> | null) ?? {};
    await bridge.request('boot', {
      cwd: routine.cwd,
      ...(typeof prefs.workspace === 'string' ? { workspace: prefs.workspace } : {}),
      ...(typeof prefs.profile === 'string' ? { profile: prefs.profile } : {}),
      probe: false,
    });
    const done = (message: BridgeMessage) => {
      if (message.type === 'turn_finished' && !message.quiet) {
        this.routines.finished(id, true);
        bridge.off('message', done);
      }
    };
    bridge.on('message', done);
    bridge.once('exit', () => this.routines.finished(id, false));
    await bridge.request('send', { text: routine.prompt });
    // Every window shows the run in its sidebar (prefs broadcast, as from another window).
    const file = path.join(this.home, 'prefs.json');
    const current = (readJson(file) as Record<string, unknown> | null) ?? {};
    const open = Array.isArray(current.open) ? current.open : [];
    const merged = { ...current, open: [...open, { id, cwd: routine.cwd, kind: 'chat', title: routine.name, routine: routine.id }] };
    writeJsonAtomic(file, merged);
    this.broadcast({ t: 'prefs', prefs: merged, from: 'routines' });
    return id;
  }

  // -- conversations ------------------------------------------------------------------

  list(): Array<{ id: string; cwd?: string; started: number; running: boolean }> {
    return [...this.conversations.values()].map((c) => ({
      id: c.id, cwd: c.cwd, started: c.started, running: !c.process.exited,
    }));
  }

  open(id: string, cwd?: string): { conversation: Conversation; fresh: boolean } {
    const existing = this.conversations.get(id);
    if (existing && !existing.process.exited) return { conversation: existing, fresh: false };
    const process = this.spawnBridge(this.root, this.env, cwd);
    const conversation: Conversation = { id, process, log: [], cwd, started: Date.now() };
    this.conversations.set(id, conversation);
    process.on('message', (msg: BridgeMessage) => {
      if (REPLAYED.has(String(msg.type))) {
        conversation.log.push(msg);
        if (conversation.log.length > REPLAY_LIMIT) conversation.log.splice(0, conversation.log.length - REPLAY_LIMIT);
      }
      this.broadcast({ t: 'msg', conv: id, msg });
    });
    process.on('exit', (code: number | null) => {
      this.broadcast({ t: 'exit', conv: id, code, reason: process.failure() });
      this.broadcast({ t: 'convs', list: this.list() });
    });
    this.broadcast({ t: 'convs', list: this.list() });
    return { conversation, fresh: true };
  }

  async close(id: string): Promise<void> {
    const conversation = this.conversations.get(id);
    if (!conversation) return;
    this.conversations.delete(id);
    await conversation.process.stop();
    this.broadcast({ t: 'convs', list: this.list() });
  }

  async closeAll(): Promise<void> {
    await Promise.all([...this.conversations.keys()].map((id) => this.close(id)));
    this.terminals.stopAll();
    this.routines.stop();
    for (const socket of this.sockets) socket.close();
    this.wss?.close();
  }

  // -- websocket ----------------------------------------------------------------------

  private broadcast(frame: Record<string, unknown>): void {
    const text = JSON.stringify(frame);
    for (const socket of this.sockets) {
      if (socket.readyState === socket.OPEN) socket.send(text);
    }
  }

  private send(socket: WebSocket, frame: Record<string, unknown>): void {
    if (socket.readyState === socket.OPEN) socket.send(JSON.stringify(frame));
  }

  handleFrame(socket: WebSocket, raw: string): void {
    let frame: Record<string, unknown>;
    try {
      frame = JSON.parse(raw) as Record<string, unknown>;
    } catch {
      return;
    }
    const conv = typeof frame.conv === 'string' ? frame.conv : '';
    switch (frame.t) {
      case 'open': {
        if (!/^[A-Za-z0-9_-]{1,64}$/.test(conv)) return;
        const cwd = typeof frame.cwd === 'string' ? frame.cwd : undefined;
        const { conversation, fresh } = this.open(conv, cwd);
        this.send(socket, { t: 'opened', conv, fresh, replay: conversation.log });
        if (conversation.process.exited) {
          this.send(socket, { t: 'exit', conv, code: conversation.process.exited.code, reason: conversation.process.failure() });
        }
        return;
      }
      case 'req': {
        const conversation = this.conversations.get(conv);
        if (!conversation || conversation.process.exited) {
          this.send(socket, { t: 'msg', conv, msg: { id: frame.id, error: { code: 'not_running', message: '' } } });
          return;
        }
        conversation.process.sendRaw({ id: frame.id, method: frame.method, params: frame.params ?? {} });
        return;
      }
      case 'close':
        void this.close(conv);
        return;
      case 'pty-start': {
        const id = String(frame.id ?? '');
        if (!/^[A-Za-z0-9_-]{1,64}$/.test(id)) return;
        void this.terminals.start(id, typeof frame.cwd === 'string' ? frame.cwd : undefined, Number(frame.cols), Number(frame.rows), typeof frame.shell === 'string' ? frame.shell : undefined)
          .then(({ replay, exited }) => {
            if (replay) this.send(socket, { t: 'pty-replay', id, data: replay });
            if (exited) this.send(socket, { t: 'pty-exit', id, code: null });
          })
          .catch((err: Error) => this.send(socket, { t: 'pty-error', id, message: err.message }));
        return;
      }
      case 'pty-input':
        this.terminals.input(String(frame.id ?? ''), String(frame.data ?? ''));
        return;
      case 'pty-resize':
        this.terminals.resize(String(frame.id ?? ''), Number(frame.cols), Number(frame.rows));
        return;
      case 'pty-stop':
        this.terminals.stop(String(frame.id ?? ''));
        return;
      case 'list':
        this.send(socket, { t: 'convs', list: this.list() });
        return;
      default:
    }
  }

  attach(server: Server): void {
    this.wss = new WebSocketServer({ noServer: true });
    server.on('upgrade', (req, socket, head) => {
      const url = new URL(req.url ?? '/', 'http://localhost');
      if (url.pathname !== '/api/ws') return;
      const allowed = isRequestAllowed({ origin: req.headers.origin, host: req.headers.host });
      if (!allowed.ok) {
        socket.destroy();
        return;
      }
      this.wss!.handleUpgrade(req, socket, head, (ws) => {
        this.sockets.add(ws);
        ws.on('message', (data) => this.handleFrame(ws, data.toString()));
        ws.on('close', () => this.sockets.delete(ws));
        this.send(ws, { t: 'convs', list: this.list() });
      });
    });
  }

  // -- http ---------------------------------------------------------------------------

  /** Handle /api/* requests; returns false for any other path. */
  async handleHttp(req: IncomingMessage, res: ServerResponse): Promise<boolean> {
    const url = new URL(req.url ?? '/', 'http://localhost');
    if (!url.pathname.startsWith('/api/') || url.pathname === '/api/ws') return false;
    const allowed = isRequestAllowed({ origin: req.headers.origin, host: req.headers.host });
    if (!allowed.ok) {
      json(res, 403, { error: 'forbidden' });
      return true;
    }
    try {
      await this.route(url, req, res);
    } catch (err) {
      json(res, 500, { error: (err as Error).message || 'internal' });
    }
    return true;
  }

  private async gitRoute(op: string, url: URL, req: IncomingMessage, res: ServerResponse): Promise<void> {
    const body = req.method === 'POST' ? await readJsonBody(req) : {};
    const cwd = path.resolve(String(body.cwd ?? url.searchParams.get('cwd') ?? ''));
    if (!fs.existsSync(cwd)) {
      json(res, 404, { error: 'cwd_missing' });
      return;
    }
    const file = typeof body.file === 'string' ? body.file : null;
    const reply = (r: G.GitResult & { url?: string }) => json(res, r.ok ? 200 : 400, { ...r, error: r.ok ? undefined : (r.stderr || r.stdout).trim().split('\n').pop() });
    switch (op) {
      case 'status': json(res, 200, await G.status(cwd)); return;
      case 'init': reply(await G.git(cwd, ['init'])); return;
      case 'diff': json(res, 200, { text: await G.diff(cwd, (url.searchParams.get('scope') ?? 'unstaged') as G.Scope, url.searchParams.get('base') ?? undefined) }); return;
      case 'branches': json(res, 200, { branches: await G.branches(cwd) }); return;
      case 'worktrees': json(res, 200, { worktrees: await G.worktrees(cwd) }); return;
      case 'stage': reply(await G.stageFile(cwd, file)); return;
      case 'unstage': reply(await G.unstageFile(cwd, file)); return;
      case 'revert': reply(await G.revertFile(cwd, file)); return;
      case 'apply': reply(await G.applyPatch(cwd, String(body.patch ?? ''), { cached: body.cached === true, reverse: body.reverse === true })); return;
      case 'commit': reply(await G.commit(cwd, String(body.message ?? ''), body.all === true)); return;
      case 'push': reply(await G.push(cwd)); return;
      case 'pr': reply(await G.pullRequest(cwd, String(body.title ?? ''), String(body.body ?? ''), body.draft === true, this.env)); return;
      case 'branch': reply(await G.createBranch(cwd, String(body.name ?? ''))); return;
      case 'worktree-add': {
        const r = await G.addWorktree(cwd, String(body.name ?? 'session'), typeof body.base === 'string' ? body.base : undefined, String(body.prefix ?? 'architect/'));
        json(res, r.ok ? 200 : 400, r);
        return;
      }
      case 'worktree-remove': reply(await G.removeWorktree(cwd, String(body.path ?? ''), body.force === true)); return;
      case 'worktree-restore': reply(await G.restoreWorktree(cwd, path.resolve(String(body.path ?? '')), String(body.branch ?? ''))); return;
      case 'worktree-prune': {
        const protect = Array.isArray(body.protect) ? body.protect.map(String) : [];
        json(res, 200, await G.pruneWorktrees(cwd, Math.max(1, Number(body.keep) || 15), protect));
        return;
      }
      case 'handoff': {
        const r = await G.handOff(cwd, path.resolve(String(body.to ?? '')));
        json(res, r.ok ? 200 : 400, r);
        return;
      }
      default: json(res, 404, { error: 'unknown' });
    }
  }

  private async route(url: URL, req: IncomingMessage, res: ServerResponse): Promise<void> {
    const name = url.pathname.slice('/api/'.length);
    if (name === 'meta' && req.method === 'GET') {
      const python = pythonCommand(this.root, this.env);
      json(res, 200, {
        root: this.root, home: this.home, platform: process.platform,
        python: python.command, homedir: os.homedir(),
      });
      return;
    }
    if (name === 'prefs') {
      const file = path.join(this.home, 'prefs.json');
      if (req.method === 'GET') {
        json(res, 200, readJson(file) ?? {});
        return;
      }
      if (req.method === 'PUT') {
        const body = await readBody(req);
        const value = JSON.parse(body) as unknown;
        if (!value || typeof value !== 'object' || Array.isArray(value)) {
          json(res, 400, { error: 'invalid' });
          return;
        }
        // Merged, so a second window can save without its own window-only settings
        // (which conversation it shows) overwriting the main window's.
        const merged = { ...((readJson(file) as Record<string, unknown> | null) ?? {}), ...(value as Record<string, unknown>) };
        writeJsonAtomic(file, merged);
        json(res, 200, { ok: true });
        this.broadcast({ t: 'prefs', prefs: merged, from: String(req.headers['x-architect-window'] ?? '') });
        return;
      }
    }
    if (name === 'file' && req.method === 'GET') {
      // A text file the agent touched, for the file and diff views. Read-only, size-capped.
      const target = url.searchParams.get('path') ?? '';
      const resolved = path.resolve(target);
      const stat = fs.statSync(resolved, { throwIfNoEntry: false });
      if (!stat || !stat.isFile()) {
        json(res, 404, { error: 'not_found' });
        return;
      }
      if (stat.size > 2_000_000) {
        json(res, 413, { error: 'too_large' });
        return;
      }
      json(res, 200, { path: resolved, text: fs.readFileSync(resolved, 'utf8'), mtime: stat.mtimeMs });
      return;
    }
    if (name === 'raw' && req.method === 'GET') {
      // Images and PDFs for the file pane's previews.
      const resolved = path.resolve(url.searchParams.get('path') ?? '');
      const type = RAW_TYPES[path.extname(resolved).toLowerCase()];
      const stat = fs.statSync(resolved, { throwIfNoEntry: false });
      if (!type || !stat?.isFile() || stat.size > 50_000_000) {
        json(res, 404, { error: 'not_found' });
        return;
      }
      res.writeHead(200, { 'content-type': type, 'content-length': stat.size, 'cache-control': 'no-store' });
      fs.createReadStream(resolved).pipe(res);
      return;
    }
    if (name === 'file/stat' && req.method === 'GET') {
      const stat = fs.statSync(path.resolve(url.searchParams.get('path') ?? ''), { throwIfNoEntry: false });
      json(res, stat?.isFile() ? 200 : 404, stat?.isFile() ? { mtime: stat.mtimeMs } : { error: 'not_found' });
      return;
    }
    if (name === 'file' && req.method === 'PUT') {
      // A spot edit from the file pane. Only existing files, and never over a newer
      // version on disk unless the person chose to overwrite it.
      const body = await readJsonBody(req);
      const resolved = path.resolve(String(body.path ?? ''));
      const stat = fs.statSync(resolved, { throwIfNoEntry: false });
      if (!stat || !stat.isFile()) {
        json(res, 404, { error: 'not_found' });
        return;
      }
      if (body.force !== true && typeof body.mtime === 'number' && Math.abs(stat.mtimeMs - body.mtime) > 1) {
        json(res, 409, { error: 'changed', mtime: stat.mtimeMs, text: fs.readFileSync(resolved, 'utf8') });
        return;
      }
      const temporary = `${resolved}.architect-${process.pid}.tmp`;
      fs.writeFileSync(temporary, String(body.text ?? ''), 'utf8');
      fs.renameSync(temporary, resolved);
      json(res, 200, { ok: true, mtime: fs.statSync(resolved).mtimeMs });
      return;
    }
    if (name === 'ports' && req.method === 'GET') {
      // Local servers to preview in the Browser pane: common dev ports that answer.
      const own = Number(new URL(`http://${req.headers.host ?? 'localhost'}`).port);
      json(res, 200, { ports: (await openPorts()).filter((p) => p !== own) });
      return;
    }
    if (name === 'routines' && req.method === 'GET') {
      json(res, 200, { routines: this.routines.list() });
      return;
    }
    if (name === 'routines/seen' && req.method === 'POST') {
      const body = await readJsonBody(req);
      this.routines.markSeen(Array.isArray(body.runs) ? body.runs.map(String) : []);
      json(res, 200, { ok: true });
      return;
    }
    const routine = /^routines\/([A-Za-z0-9_-]{1,64})(\/run)?$/.exec(name);
    if (routine) {
      const [, id, run] = routine;
      if (run && req.method === 'POST') {
        const started = await this.routines.runNow(id);
        json(res, started ? 200 : 404, started ? { run: started } : { error: 'not_found' });
      } else if (req.method === 'PUT') {
        const body = await readJsonBody(req);
        if (typeof body.cwd === 'string' && !fs.existsSync(body.cwd)) {
          json(res, 400, { error: 'cwd_missing' });
          return;
        }
        json(res, 200, { routine: this.routines.save({ ...(body as Partial<Routine>), id }) });
      } else if (req.method === 'DELETE') {
        json(res, this.routines.remove(id) ? 200 : 404, { ok: true });
      } else json(res, 405, { error: 'method' });
      return;
    }
    if (name === 'shells' && req.method === 'GET') {
      json(res, 200, { shells: this.terminals.list().map(({ id, label }) => ({ id, label })) });
      return;
    }
    if (name === 'upload' && req.method === 'POST') {
      // A pasted or picked image with no path of its own: kept under the app's home
      // so the conversation can attach it like any other file.
      const raw = (url.searchParams.get('name') || 'image.png').replace(/[^A-Za-z0-9._-]/g, '_').slice(-80);
      const dir = path.join(this.home, 'uploads');
      fs.mkdirSync(dir, { recursive: true });
      const file = path.join(dir, `${Date.now().toString(36)}-${raw}`);
      const body = await readBuffer(req, 20_000_000);
      fs.writeFileSync(file, body);
      json(res, 200, { path: file });
      return;
    }
    if (name.startsWith('git/')) {
      await this.gitRoute(name.slice(4), url, req, res);
      return;
    }
    if (name === 'files' && req.method === 'GET') {
      // Quick open: files under a folder, skipping build and VCS folders.
      const root = path.resolve(url.searchParams.get('root') || os.homedir());
      json(res, 200, { root, files: listFiles(root) });
      return;
    }
    if (name === 'editors' && req.method === 'GET') {
      json(res, 200, { editors: await detectEditors(this.env) });
      return;
    }
    if (name === 'open-in' && req.method === 'POST') {
      const body = JSON.parse(await readBody(req)) as { editor?: string; path?: string; line?: number };
      const ok = await openInEditor(String(body.editor ?? ''), String(body.path ?? ''), body.line, this.env);
      json(res, ok ? 200 : 400, { ok });
      return;
    }
    if (name === 'dirs' && req.method === 'GET') {
      // Folder browsing for the browser build (Electron uses the native picker).
      const dir = path.resolve(url.searchParams.get('path') || os.homedir());
      const entries = fs.readdirSync(dir, { withFileTypes: true })
        .filter((e) => e.isDirectory() && !e.name.startsWith('.'))
        .map((e) => e.name)
        .sort((a, b) => a.localeCompare(b));
      json(res, 200, { path: dir, parent: path.dirname(dir), dirs: entries });
      return;
    }
    json(res, 404, { error: 'unknown' });
  }
}

async function readJsonBody(req: IncomingMessage): Promise<Record<string, unknown>> {
  const text = await readBody(req);
  const value = text ? (JSON.parse(text) as unknown) : {};
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : {};
}

function json(res: ServerResponse, status: number, body: unknown): void {
  res.statusCode = status;
  res.setHeader('Content-Type', 'application/json');
  res.setHeader('Cache-Control', 'no-store');
  res.end(JSON.stringify(body));
}

function readBody(req: IncomingMessage, limit = 4_000_000): Promise<string> {
  return readBuffer(req, limit).then((b) => b.toString('utf8'));
}

function readBuffer(req: IncomingMessage, limit: number): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks: Buffer[] = [];
    req.on('data', (chunk: Buffer) => {
      size += chunk.length;
      if (size > limit) {
        reject(new Error('too_large'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}

export function readJson(file: string): unknown {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8')) as unknown;
  } catch {
    return null;
  }
}

export function writeJsonAtomic(file: string, value: unknown): void {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temporary = `${file}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify(value, null, 1), 'utf8');
  fs.renameSync(temporary, file);
}

const RAW_TYPES: Record<string, string> = {
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp',
  '.svg': 'image/svg+xml', '.ico': 'image/x-icon', '.bmp': 'image/bmp', '.pdf': 'application/pdf',
};

const DEV_PORTS = [3000, 3001, 4000, 4200, 4321, 5000, 5173, 5174, 5500, 6006, 8000, 8080, 8081, 8888, 9000];

/** Which common development ports have something listening on loopback (quick connect probes). */
async function openPorts(): Promise<number[]> {
  const { Socket } = await import('node:net');
  const probe = (port: number) => new Promise<boolean>((resolve) => {
    const socket = new Socket();
    const done = (open: boolean) => {
      socket.destroy();
      resolve(open);
    };
    socket.setTimeout(250);
    socket.once('connect', () => done(true));
    socket.once('timeout', () => done(false));
    socket.once('error', () => done(false));
    socket.connect(port, '127.0.0.1');
  });
  const open = await Promise.all(DEV_PORTS.map(async (port) => ((await probe(port)) ? port : 0)));
  return open.filter(Boolean);
}
