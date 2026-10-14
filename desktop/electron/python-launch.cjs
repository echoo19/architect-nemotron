/**
 * Starts a bridge process for a packaged app. Electron runs this file as Node
 * (ELECTRON_RUN_AS_NODE=1): `python-launch.cjs <architect root>`.
 *
 * The app's copy of Architect's core is read-only, so its Python environment lives in
 * the user's app data (ARCHITECT_PYTHON_ENV, set by electron/main.ts). The first launch,
 * and any launch after an update changes uv.lock, uses the uv that ships with the app
 * (resources/uv) to download a Python build of its own and install the locked
 * dependencies; every other launch goes straight to Python.
 *
 * While it sets up, it writes {"type":"setup","text":...} lines on stdout, which the app
 * shows in place of the conversation, and keeps uv's full output in setup.log beside the
 * environment. If setup fails, its last stderr line says what happened in plain words
 * (the app shows that line). Python then inherits this process's pipes, so the bridge
 * protocol never passes through here.
 */
'use strict';
const { spawn } = require('node:child_process');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

// The Python the app runs on; uv downloads the newest build of it that it knows.
const PYTHON = '3.12';
// Raise this to make every install set its environment up again.
const SETUP_VERSION = 2;
const STALE_LOCK_MS = 30 * 60 * 1000;
const WIN = process.platform === 'win32';
const ANSI = /\u001b\[[0-9;]*[A-Za-z]/g;

/** One protocol line on stdout, written at once (never through process.stdout's buffer). */
function say(text) {
  try {
    fs.writeSync(1, `${JSON.stringify({ type: 'setup', text })}\n`);
  } catch {
    /* the app went away; setup goes on */
  }
}

/** The plain sentence the app shows, as the last line on stderr, then stop. */
function fail(text, code) {
  try {
    fs.writeSync(2, `${text}\n`);
  } catch {
    /* nothing to tell */
  }
  process.exit(code);
}

/** uv's own words, as a short line for the app; null for lines it need not show. */
function progress(line) {
  let match = /^Downloading cpython-(\d+\.\d+\.\d+)\S*.*?(?:\((\d[\d.]*)\s*([KMG]iB)\))?$/.exec(line);
  if (match) return `Downloading Python ${match[1]}${match[2] ? ` (${match[2]} ${match[3]})` : ''}`;
  if (/^Creating virtual environment/.test(line)) return 'Creating the Python environment';
  if (/^Downloading \S/.test(line)) return 'Downloading packages';
  match = /^Installed (\d+) packages?/.exec(line);
  if (match) return `Installed ${match[1]} packages`;
  return null;
}

/** Where one environment and its companions live: all beside ARCHITECT_PYTHON_ENV. */
function layout(root, envDir) {
  const home = path.dirname(envDir);
  return {
    root,
    envDir,
    home,
    python: WIN ? path.join(envDir, 'Scripts', 'python.exe') : path.join(envDir, 'bin', 'python'),
    stamp: path.join(envDir, '.architect-setup'),
    log: path.join(home, 'setup.log'),
    lock: `${envDir}.setup-lock`,
    runtime: path.join(home, 'python-runtime'),
    pycache: path.join(home, 'pycache'),
  };
}

/** The uv the app ships with (resources/uv), unless ARCHITECT_UV names another. */
function findUv(root, env = process.env) {
  if (env.ARCHITECT_UV) return env.ARCHITECT_UV;
  const bundled = path.join(root, '..', 'uv', WIN ? 'uv.exe' : 'uv');
  return fs.existsSync(bundled) ? bundled : 'uv';
}

/** The environment exists, its base Python is still there, and it matches this lock. */
function ready(place, wanted) {
  try {
    if (!fs.existsSync(place.python) || fs.readFileSync(place.stamp, 'utf8').trim() !== wanted) return false;
    const base = /^home\s*=\s*(.+)$/m.exec(fs.readFileSync(path.join(place.envDir, 'pyvenv.cfg'), 'utf8'));
    return !base || fs.existsSync(base[1].trim());
  } catch {
    return false;
  }
}

function alive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return err.code === 'EPERM';
  }
}

/** Another window's launcher may be setting up the same environment: one at a time. */
async function withLock(place, work) {
  fs.mkdirSync(place.home, { recursive: true });
  let told = false;
  for (;;) {
    try {
      fs.mkdirSync(place.lock);
      fs.writeFileSync(path.join(place.lock, 'pid'), String(process.pid));
      break;
    } catch (err) {
      if (err.code !== 'EEXIST') throw err;
      let owner = 0;
      let age = 0;
      try {
        age = Date.now() - fs.statSync(place.lock).mtimeMs;
        owner = Number(fs.readFileSync(path.join(place.lock, 'pid'), 'utf8')) || 0;
      } catch {
        /* just made, or just removed */
      }
      if ((owner && !alive(owner)) || age > STALE_LOCK_MS) {
        fs.rmSync(place.lock, { recursive: true, force: true });
        continue;
      }
      if (!told) say('Waiting for another window to finish setting up');
      told = true;
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
  }
  try {
    return await work();
  } finally {
    fs.rmSync(place.lock, { recursive: true, force: true });
  }
}

/** `uv sync` into the app's environment, with the app's own Python build. */
function setup(place, uv) {
  say(fs.existsSync(place.python)
    ? 'Updating Architect for this version. This takes a minute.'
    : 'Setting up Architect for its first start. This takes a few minutes.');
  const args = ['sync', '--frozen', '--no-install-project', '--no-dev', '--python', PYTHON, '--project', place.root];
  const env = {
    ...process.env,
    UV_PROJECT_ENVIRONMENT: place.envDir,
    UV_PYTHON_INSTALL_DIR: place.runtime,
    UV_PYTHON_PREFERENCE: 'only-managed',
    UV_PYTHON_DOWNLOADS: 'automatic',
    NO_COLOR: '1',
  };
  delete env.ELECTRON_RUN_AS_NODE;
  delete env.VIRTUAL_ENV;
  const log = fs.openSync(place.log, 'w');
  fs.writeSync(log, `${new Date().toISOString()} ${uv} ${args.join(' ')}\n`);
  const errors = [];
  let shown = '';
  const onLine = (raw) => {
    const line = raw.replace(ANSI, '').trim();
    if (!line) return;
    fs.writeSync(log, `${line}\n`);
    if (/^error:/i.test(line)) errors.push(line.replace(/^error:\s*/i, ''));
    const text = progress(line);
    if (text && text !== shown) say((shown = text));
  };
  return new Promise((resolve) => {
    let settled = false;
    let child;
    const stop = () => child?.kill();
    const finish = (result) => {
      if (settled) return;
      settled = true;
      process.off('SIGTERM', stop);
      fs.closeSync(log);
      resolve(result);
    };
    try {
      child = spawn(uv, args, { env, stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true });
    } catch (err) {
      finish({ error: err });
      return;
    }
    process.on('SIGTERM', stop);
    for (const stream of [child.stdout, child.stderr]) {
      let rest = '';
      stream.setEncoding('utf8');
      stream.on('data', (chunk) => {
        const lines = (rest + chunk).split(/\r?\n|\r/);
        rest = lines.pop() ?? '';
        lines.forEach(onLine);
      });
      stream.on('end', () => onLine(rest));
    }
    child.on('error', (err) => finish({ error: err }));
    // 'close' comes after both streams have ended, so every line is in the log by then.
    child.on('close', (code) => finish({ code, reason: errors[0] }));
  });
}

function startPython(place) {
  const env = { ...process.env };
  delete env.ELECTRON_RUN_AS_NODE;
  // The environment holds the dependencies only; the core is imported from the app's files.
  const paths = (env.PYTHONPATH ?? '').split(path.delimiter).filter(Boolean);
  if (!paths.includes(place.root)) env.PYTHONPATH = [place.root, ...paths].join(path.delimiter);
  // Bytecode goes beside the environment: a signed app's own files are read-only.
  const args = ['-X', `pycache_prefix=${place.pycache}`, '-m', 'architect.desktop.bridge'];
  const child = spawn(place.python, args, { env, stdio: 'inherit', windowsHide: true });
  for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP']) process.on(signal, () => child.kill(signal));
  child.on('exit', (code, signal) => process.exit(code ?? (signal ? 1 : 0)));
  child.on('error', (err) => fail(`Python could not start: ${err.message}`, 4));
}

async function main() {
  const root = process.argv[2];
  const envDir = process.env.ARCHITECT_PYTHON_ENV;
  if (!root || !envDir) fail('python-launch: missing the Architect folder or ARCHITECT_PYTHON_ENV', 2);
  const place = layout(root, envDir);
  let lock;
  try {
    lock = fs.readFileSync(path.join(root, 'uv.lock'));
  } catch {
    fail(`Architect's files are incomplete (no uv.lock in ${root}). Reinstall Architect.`, 2);
  }
  const wanted = crypto.createHash('sha256').update(lock).update(`\n${PYTHON}\n${SETUP_VERSION}\n`).digest('hex');
  if (!ready(place, wanted)) {
    const uv = findUv(root);
    const result = await withLock(place, async () => {
      if (ready(place, wanted)) return null; // another window finished it meanwhile
      const outcome = await setup(place, uv);
      if (!outcome.error && outcome.code === 0) fs.writeFileSync(place.stamp, wanted);
      return outcome;
    });
    if (result?.error) {
      fail(result.error.code === 'ENOENT'
        ? `Architect could not find its copy of uv (${uv}). Reinstall Architect.`
        : `uv could not start (${result.error.message}). Reinstall Architect.`, 3);
    }
    if (result && result.code !== 0) {
      const what = result.reason || `uv stopped with code ${result.code}`;
      fail(`Setup did not finish: ${what}. The first start downloads Python and Architect's packages, `
        + `so check the internet connection, then try again. Full log: ${place.log}`, 3);
    }
    if (result) say('Starting Architect');
  }
  startPython(place);
}

if (require.main === module) {
  main().catch((err) => fail(`Setup did not finish: ${err.message}`, 3));
}

module.exports = { PYTHON, progress, layout, findUv };
