// Smoke test of a packaged app as a clean machine would run it: no uv, no Node and no
// Python on PATH, and no system Node running anything. Run it with the app's own binary
// as Node, the way the app runs its helpers:
//
//   ELECTRON_RUN_AS_NODE=1 <app binary> scripts/smoke-packaged.mjs --app <app binary> [--work <dir>]
//
// where <app binary> is release/win-unpacked/Architect.exe, an installed Architect.exe,
// or Architect.app/Contents/MacOS/Architect. Steps, each printed as one JSON line:
//
// 1. bridge: start one conversation's bridge exactly as the app does (this binary runs
//    the bundled launcher as Node, which sets Python up with the bundled uv on first
//    start), then a $0 build through it: `/make @<spec>` on a fake model and a fake
//    six-case floor, approved with the defaults as Enter would, then run the agent it
//    licensed.
// 2. pi: run Pi, the coder's engine, once on this binary as Node with a fake model: it
//    writes a file, and runs a shell command that must not see ELECTRON_RUN_AS_NODE.
//
// Exits 0 only when both pass. Network is needed once, for Python and its packages.
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const option = (name) => {
  const index = process.argv.indexOf(`--${name}`);
  return index > 0 ? process.argv[index + 1] : undefined;
};
const WIN = process.platform === 'win32';
const app = path.resolve(option('app') ?? process.execPath);
const work = path.resolve(option('work') ?? fs.mkdtempSync(path.join(os.tmpdir(), 'arch-smoke-')));
const resources = process.platform === 'darwin' ? path.join(path.dirname(app), '..', 'Resources') : path.join(path.dirname(app), 'resources');
const root = path.join(resources, 'architect');
const launcher = path.join(resources, 'app.asar.unpacked', 'dist-electron', 'python-launch.cjs');
const pythonEnv = path.join(work, 'appdata', 'python');
const python = WIN ? path.join(pythonEnv, 'Scripts', 'python.exe') : path.join(pythonEnv, 'bin', 'python');
// As the launcher does: bytecode beside the environment, never in the app's own files.
const PYCACHE = ['-X', `pycache_prefix=${path.join(work, 'appdata', 'pycache')}`];
const caches = (dir) => fs.readdirSync(dir, { recursive: true }).filter((entry) => path.basename(String(entry)) === '__pycache__').length;
const cachesBefore = caches(resources);

function report(step, ok, details) {
  console.log(JSON.stringify({ step, ok, ...details }));
  if (!ok) process.exit(1);
}

// -- the environment of a clean machine ------------------------------------------------

const NAMES = ['node', 'npm', 'npx', 'uv', 'uvx', ...(WIN ? ['python', 'python3'] : [])];
const TOOLS = WIN ? NAMES.flatMap((name) => ['.exe', '.cmd', '.bat'].map((ext) => name + ext)) : NAMES;
const SYSTEM = new Set(['/usr/bin', '/bin', '/usr/sbin', '/sbin']);
const pathKey = Object.keys(process.env).find((key) => key.toUpperCase() === 'PATH') ?? 'PATH';
const entries = (process.env[pathKey] ?? '').split(path.delimiter).filter(Boolean);
const hasTool = (dir) => TOOLS.some((tool) => fs.existsSync(path.join(dir, tool)))
  || (!WIN && !SYSTEM.has(dir) && fs.existsSync(path.join(dir, 'python3')));
const kept = entries.filter((dir) => !hasTool(dir));
const base = { ...process.env, [pathKey]: kept.join(path.delimiter), UV_CACHE_DIR: path.join(work, 'uv-cache') };
for (const name of ['ELECTRON_RUN_AS_NODE', 'ARCHITECT_UV', 'ARCHITECT_PYTHON', 'VIRTUAL_ENV', 'PYTHONHOME', 'PYTHONPATH', 'UV_PROJECT_ENVIRONMENT', 'UV_PYTHON']) delete base[name];
const stillThere = kept.flatMap((dir) => TOOLS.filter((tool) => fs.existsSync(path.join(dir, tool))).map((tool) => path.join(dir, tool)));
report('clean-path', stillThere.length === 0 && fs.existsSync(launcher) && fs.existsSync(path.join(root, 'uv.lock')), {
  app, work, removed: entries.filter((dir) => !kept.includes(dir)), stillThere,
});

// -- step 1: a bridge, set up from nothing, and a $0 build through it --------------------

function fixture() {
  const ws = path.join(work, 'w');
  const project = path.join(work, 'proj');
  fs.mkdirSync(ws, { recursive: true });
  fs.mkdirSync(project, { recursive: true });
  const reply = (text) => ({ text, tokens_in: 8, tokens_out: 12, cost_usd: 0.00002, model: 'fake' });
  fs.writeFileSync(path.join(ws, 'cases.json'), JSON.stringify(Array.from({ length: 6 }, (_, i) => ({ case_id: String(i), task: {}, expected: 'ok' }))));
  fs.writeFileSync(path.join(ws, 'responses.json'), JSON.stringify(Array.from({ length: 40 }, () => reply('ok'))));
  fs.writeFileSync(path.join(ws, 'inference.json'), JSON.stringify(Array.from({ length: 4 }, () => reply('{"text": "ok"}'))));
  fs.writeFileSync(path.join(ws, 'session-profile.json'), JSON.stringify({
    workspace: '.',
    models: { fake: { provider: 'fake', fixture: 'responses.json' } },
    inference: { provider: 'fake', model: 'fake', fixture: 'inference.json' },
    floors: { tasks: { kind: 'held_out_tasks', path: 'cases.json' } },
    compile: { max_candidates: 1, enable_rewriting: false },
  }, null, 2));
  return { ws, project };
}

class Bridge {
  constructor(env) {
    this.lines = [];
    this.pending = new Map();
    this.nextId = 1;
    this.stderr = '';
    this.listeners = new Set();
    // As the app does for a packaged conversation (server/bridgeProcess.ts).
    this.child = spawn(app, [launcher, root], { cwd: work, env, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
    this.exited = new Promise((resolve) => this.child.on('exit', resolve));
    let rest = '';
    this.child.stdout.setEncoding('utf8');
    this.child.stdout.on('data', (chunk) => {
      const parts = (rest + chunk).split('\n');
      rest = parts.pop() ?? '';
      for (const part of parts) {
        if (!part.trim()) continue;
        let message;
        try {
          message = JSON.parse(part);
        } catch {
          continue;
        }
        this.lines.push(message);
        const waiter = this.pending.get(message.id);
        if (waiter && ('result' in message || 'error' in message)) {
          this.pending.delete(message.id);
          if (message.error) waiter.reject(new Error(`${message.error.code}: ${message.error.message ?? ''}`));
          else waiter.resolve(message.result);
        }
        for (const listener of this.listeners) listener(message);
      }
    });
    this.child.stderr.setEncoding('utf8');
    this.child.stderr.on('data', (chunk) => (this.stderr = (this.stderr + chunk).slice(-8000)));
  }

  request(method, params = {}) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.child.stdin.write(`${JSON.stringify({ id, method, params })}\n`);
    });
  }

  waitFor(test, ms, what) {
    return new Promise((resolve, reject) => {
      const found = this.lines.find(test);
      if (found) return resolve(found);
      const timer = setTimeout(() => {
        this.listeners.delete(listener);
        reject(new Error(`no ${what} within ${ms / 1000} s; last stderr: ${this.stderr.trim().split('\n').at(-1) ?? ''}`));
      }, ms);
      const listener = (message) => {
        if (!test(message)) return;
        clearTimeout(timer);
        this.listeners.delete(listener);
        resolve(message);
      };
      this.listeners.add(listener);
      this.exited.then((code) => {
        clearTimeout(timer);
        reject(new Error(`the bridge stopped (code ${code}): ${this.stderr.trim().split('\n').at(-1) ?? ''}`));
      });
    });
  }
}

async function bridgeStep() {
  const { ws, project } = fixture();
  // A first start must set up Python (and say so); a later one goes straight to Python.
  const firstStart = !fs.existsSync(pythonEnv);
  const started = Date.now();
  const bridge = new Bridge({
    ...base,
    ARCHITECT_PACKAGED: '1',
    ARCHITECT_NODE: app,
    ARCHITECT_LAUNCHER: launcher,
    ARCHITECT_PYTHON_ENV: pythonEnv,
    ELECTRON_RUN_AS_NODE: '1',
    PYTHONIOENCODING: 'utf-8',
    PYTHONUTF8: '1',
    PYTHONPATH: root,
  });
  try {
    await bridge.waitFor((m) => m.type === 'ready', 20 * 60_000, 'ready message');
    const setup = bridge.lines.filter((m) => m.type === 'setup').map((m) => m.text);
    const ready = (Date.now() - started) / 1000;
    const ping = await bridge.request('ping');
    // The spec, written by the bundled core itself so its hash is the one it computes.
    const specPath = path.join(project, 'smoke.json');
    const made = spawnSync(python, [...PYCACHE, '-c', SPEC_SCRIPT, specPath], { env: { ...base, PYTHONPATH: root, PYTHONUTF8: '1' }, encoding: 'utf8' });
    if (made.status !== 0) throw new Error(`could not write the spec: ${made.stderr.trim().split('\n').at(-1)}`);
    await bridge.request('boot', { cwd: project, workspace: ws, probe: false });
    bridge.listeners.add((message) => {
      // Every question gets its default, as Enter does in the app.
      if (message.type === 'ask') void bridge.request('answer', { id: message.ask.id, value: '' }).catch(() => undefined);
    });
    for (const message of bridge.lines.filter((m) => m.type === 'ask')) void bridge.request('answer', { id: message.ask.id, value: '' });
    await bridge.request('send', { text: `/make @"${specPath}"` });
    await bridge.waitFor((m) => m.type === 'turn_finished', 10 * 60_000, 'finished build');
    const { agents } = await bridge.request('agents');
    const agent = agents.find((row) => row.name === 'smoke');
    const run = await bridge.request('run_agent', { name: 'smoke', input: {} });
    const cards = bridge.lines.filter((m) => m.type === 'event' && m.event?.kind === 'plan_card').map((m) => m.event.title);
    await bridge.request('shutdown').catch(() => undefined);
    bridge.child.stdin.end();
    await Promise.race([bridge.exited, new Promise((r) => setTimeout(r, 10_000))]);
    const ok = (firstStart ? setup.length > 0 : setup.length === 0) && agent?.ok === true && agent?.licensed === true && run?.output === 'ok';
    report('bridge', ok, { firstStart, setup, readySeconds: ready, version: ping.version, cards, agent: agent && { name: agent.name, ok: agent.ok, licensed: agent.licensed }, run });
  } catch (err) {
    bridge.child.kill();
    report('bridge', false, { error: String(err.message ?? err), stderr: bridge.stderr.trim().split('\n').slice(-12) });
  }
}

const SPEC_SCRIPT = `
import sys
from architect.spec import Budget, FloorDefinition, FloorKind, Lifecycle, Spec
spec = Spec(task="Return ok", input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
            output_schema={"type": "string"}, domain="synthesizer",
            floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="tasks"),
            budget=Budget(), lifecycle=Lifecycle("install"))
open(sys.argv[1], "w", encoding="utf-8").write(spec.model_dump_json())
`;

// -- step 2: Pi on this binary as Node ----------------------------------------------------

const PI_SCRIPT = `
import asyncio, json, os, sys
from pathlib import Path
from architect.backends.pi import PiBackend, PiConfig
from architect.models import FakeAdapter, ModelResponse
from architect.runtime import RunContext
from architect.spec import Budget

repo = Path(sys.argv[1]).resolve()
repo.mkdir(parents=True, exist_ok=True)
def reply(text):
    return ModelResponse(text=text, model="fake", tokens_in=10, tokens_out=10, cost_usd=0)
def tool(name, **args):
    return json.dumps({"tool_calls": [{"id": "call1", "name": name, "arguments": args}]})
async def main():
    ctx = RunContext(FakeAdapter(responses=[
        reply(tool("write", path="hello.txt", content="hello\\n")),
        reply(tool("bash", command='printf "%s" "\${ELECTRON_RUN_AS_NODE-unset}" > env.txt')),
        reply('{"ok":true}'),
    ]), Budget(cost_usd=1, compute_s=120), repository=repo)
    backend = PiBackend(PiConfig(allowed_tools=("write", "bash"), allow_shell=True))
    pack = {"tools": ["write", "bash"], "prompt_fragment": "Write the file.",
            "context_policy": {"max_characters": 2048}, "subagent_rules": ["Bounded"]}
    output = await backend.run(ctx, "write hello.txt", pack)
    evidence = backend.last_evidence
    print(json.dumps({"output": output, "file": (repo / "hello.txt").read_text(encoding="utf-8"),
                      "shellSaw": (repo / "env.txt").read_text(encoding="utf-8"),
                      "exitCode": evidence.exit_code, "extension": evidence.extension_loaded,
                      "versions": evidence.versions, "node": os.environ["ARCHITECT_NODE"]}))
asyncio.run(main())
`;

function piStep() {
  const repo = path.join(work, 'pi-repo');
  const run = spawnSync(python, [...PYCACHE, '-c', PI_SCRIPT, repo], {
    env: { ...base, ARCHITECT_NODE: app, PYTHONPATH: root, PYTHONUTF8: '1' }, encoding: 'utf8', timeout: 5 * 60_000,
  });
  let result = null;
  try {
    result = JSON.parse(run.stdout.trim().split('\n').at(-1));
  } catch {
    /* reported below */
  }
  const ok = run.status === 0 && result?.output === '{"ok":true}' && result.file === 'hello\n'
    && result.shellSaw === 'unset' && result.exitCode === 0 && result.extension === true;
  report('pi', ok, result ?? { status: run.status, stderr: run.stderr.trim().split('\n').slice(-12) });
}

await bridgeStep();
piStep();
// A signed Mac app's files are read-only; nothing here may need to write into them.
report('app-files-untouched', caches(resources) === cachesBefore, { newPycacheFolders: caches(resources) - cachesBefore });
console.log(JSON.stringify({ smoke: 'ok', work }));
