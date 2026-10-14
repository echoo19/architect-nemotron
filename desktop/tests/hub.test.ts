/**
 * The bridge between the page and Architect's core, end to end: the real control
 * server, the real Python bridge process, a fake model (offline, $0).
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import WebSocket from 'ws';
import { startServer, type Running } from '../server/serve';

const ROOT = path.resolve(__dirname, '..', '..');

function fakeWorkspace(dir: string): void {
  const reply = (text: string) => ({ text, tokens_in: 4, tokens_out: 6, cost_usd: 0.0001, model: 'fake' });
  const answer = JSON.stringify({ text: 'Hello from the fake model.' });
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'responses.json'), JSON.stringify(Array.from({ length: 20 }, () => reply(answer))));
  fs.writeFileSync(path.join(dir, 'session-profile.json'), JSON.stringify({
    workspace: '.',
    models: { fake: { provider: 'fake', fixture: 'responses.json' } },
    inference: { provider: 'fake', model: 'fake', fixture: 'responses.json' },
    compile: { max_candidates: 1, enable_rewriting: false },
  }));
}

class Client {
  frames: Record<string, unknown>[] = [];
  private nextId = 1;
  constructor(readonly ws: WebSocket) {
    ws.on('message', (data) => this.frames.push(JSON.parse(data.toString()) as Record<string, unknown>));
  }
  send(frame: Record<string, unknown>): void {
    this.ws.send(JSON.stringify(frame));
  }
  async waitFor(test: (f: Record<string, unknown>) => boolean, ms = 60_000): Promise<Record<string, unknown>> {
    const start = Date.now();
    while (Date.now() - start < ms) {
      const found = this.frames.find(test);
      if (found) return found;
      await new Promise((r) => setTimeout(r, 25));
    }
    throw new Error('timed out');
  }
  async request(conv: string, method: string, params: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
    const id = this.nextId++;
    this.send({ t: 'req', conv, id, method, params });
    const frame = await this.waitFor((f) => f.t === 'msg' && (f.msg as { id?: number }).id === id);
    return frame.msg as Record<string, unknown>;
  }
}

let server: Running;
let tmp: string;

beforeAll(async () => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'adt-'));
  fakeWorkspace(path.join(tmp, 'w'));
  fs.mkdirSync(path.join(tmp, 'ui'));
  fs.writeFileSync(path.join(tmp, 'ui', 'index.html'), '<!doctype html><title>t</title>');
  server = await startServer(path.join(tmp, 'ui'), {
    root: ROOT,
    home: path.join(tmp, 'home'),
    env: { ...process.env, ARCHITECT_WORKSPACE: path.join(tmp, 'w') },
  });
});

afterAll(async () => {
  await server?.close();
});

function connect(origin?: string): Promise<WebSocket> {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(`ws://127.0.0.1:${server.port}/api/ws`, origin ? { origin } : {});
    ws.once('open', () => resolve(ws));
    ws.once('error', reject);
  });
}

describe('control server', () => {
  it('serves the page and saves prefs atomically', async () => {
    const page = await fetch(server.url).then((r) => r.text());
    expect(page).toContain('<title>t</title>');
    const put = await fetch(`${server.url}/api/prefs`, { method: 'PUT', body: JSON.stringify({ theme: 'dark' }) });
    expect(put.status).toBe(200);
    const prefs = await fetch(`${server.url}/api/prefs`).then((r) => r.json());
    expect(prefs).toEqual({ theme: 'dark' });

    // Another window saves only shared settings: merged in, and told to every window.
    const client = new Client(await connect());
    await fetch(`${server.url}/api/prefs`, { method: 'PUT', headers: { 'X-Architect-Window': 'w2' }, body: JSON.stringify({ verbose: true }) });
    expect(await fetch(`${server.url}/api/prefs`).then((r) => r.json())).toEqual({ theme: 'dark', verbose: true });
    const told = await client.waitFor((f) => f.t === 'prefs', 5000);
    expect(told).toMatchObject({ from: 'w2', prefs: { theme: 'dark', verbose: true } });
    client.ws.close();
  });

  it('saves a file edit, but not over a newer version on disk', async () => {
    const file = path.join(tmp, 'edit.txt');
    fs.writeFileSync(file, 'one\r\ntwo\r\n');
    const read = await fetch(`${server.url}/api/file?path=${encodeURIComponent(file)}`).then((r) => r.json()) as { text: string; mtime: number };
    expect(read.text).toBe('one\r\ntwo\r\n');

    const saved = await fetch(`${server.url}/api/file`, { method: 'PUT', body: JSON.stringify({ path: file, text: 'one\r\nTWO\r\n', mtime: read.mtime }) });
    expect(saved.status).toBe(200);
    expect(fs.readFileSync(file, 'utf8')).toBe('one\r\nTWO\r\n');

    // Someone else writes it; a save based on the old version is refused with theirs.
    fs.writeFileSync(file, 'theirs\n');
    const later = new Date(Date.now() + 5000);
    fs.utimesSync(file, later, later);
    const stale = await fetch(`${server.url}/api/file`, { method: 'PUT', body: JSON.stringify({ path: file, text: 'mine\n', mtime: read.mtime }) });
    expect(stale.status).toBe(409);
    expect((await stale.json() as { text: string }).text).toBe('theirs\n');
    expect(fs.readFileSync(file, 'utf8')).toBe('theirs\n');

    const forced = await fetch(`${server.url}/api/file`, { method: 'PUT', body: JSON.stringify({ path: file, text: 'mine\n', force: true }) });
    expect(forced.status).toBe(200);
    expect(fs.readFileSync(file, 'utf8')).toBe('mine\n');

    const missing = await fetch(`${server.url}/api/file`, { method: 'PUT', body: JSON.stringify({ path: path.join(tmp, 'nope.txt'), text: 'x' }) });
    expect(missing.status).toBe(404);
    expect(fs.existsSync(path.join(tmp, 'nope.txt'))).toBe(false);
  });

  it('lists the shells this computer has', async () => {
    const { shells } = await fetch(`${server.url}/api/shells`).then((r) => r.json()) as { shells: Array<{ id: string }> };
    expect(shells.length).toBeGreaterThan(0);
    if (process.platform === 'win32') expect(shells.map((s) => s.id)).toContain('powershell');
  });

  it('refuses requests from other origins', async () => {
    const res = await fetch(`${server.url}/api/meta`, { headers: { origin: 'https://evil.example' } });
    expect(res.status).toBe(403);
    await expect(connect('https://evil.example')).rejects.toBeTruthy();
  });
});

describe('a conversation through the real bridge', () => {
  it('boots, answers a message, runs a slash command and answers state', async () => {
    const client = new Client(await connect());
    const conv = 'ctest1';
    client.send({ t: 'open', conv, cwd: tmp });
    await client.waitFor((f) => f.t === 'opened' && f.conv === conv);

    const boot = await client.request(conv, 'boot', { cwd: tmp, probe: false });
    expect(boot.error).toBeUndefined();
    expect((boot.result as { model: string }).model).toBe('fake');

    await client.request(conv, 'send', { text: 'hello' });
    const answer = await client.waitFor((f) => f.t === 'msg' && (f.msg as { event?: { kind?: string } }).event?.kind === 'answer');
    expect(((answer.msg as { event: { text: string } }).event.text)).toBe('Hello from the fake model.');
    await client.waitFor((f) => f.t === 'msg' && (f.msg as { type?: string }).type === 'turn_finished');

    await client.request(conv, 'send', { text: '/status', quiet: true });
    await client.waitFor((f) => f.t === 'msg' && (f.msg as { event?: { kind?: string } }).event?.kind === 'rows');

    const state = await client.request(conv, 'state');
    expect((state.result as { session_id: string }).session_id).toMatch(/^[0-9a-f]{12}$/);

    const sessions = await client.request(conv, 'sessions');
    expect((sessions.result as { sessions: Array<{ title: string }> }).sessions[0].title).toBe('hello');
    client.ws.close();
  });

  it('replays recent messages to a page that reconnects, and stops on close', async () => {
    const first = new Client(await connect());
    const conv = 'ctest2';
    first.send({ t: 'open', conv, cwd: tmp });
    await first.waitFor((f) => f.t === 'opened');
    await first.request(conv, 'boot', { cwd: tmp, probe: false });
    await first.request(conv, 'send', { text: 'remember me' });
    await first.waitFor((f) => f.t === 'msg' && (f.msg as { type?: string }).type === 'turn_finished');
    first.ws.close();

    const second = new Client(await connect());
    second.send({ t: 'open', conv, cwd: tmp });
    const opened = await second.waitFor((f) => f.t === 'opened' && f.conv === conv);
    expect(opened.fresh).toBe(false);
    const replay = opened.replay as Array<{ type?: string; text?: string }>;
    expect(replay.some((m) => m.type === 'turn_started' && m.text === 'remember me')).toBe(true);

    second.send({ t: 'close', conv });
    await second.waitFor((f) => f.t === 'exit' && f.conv === conv);
    second.ws.close();
  });
});
