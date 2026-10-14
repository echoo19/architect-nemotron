/**
 * The packaged app's launcher (electron/python-launch.cjs): the plain progress lines it
 * makes of uv's output, the sentence it leaves when setup cannot run, and the lock that
 * lets one window at a time set up Python. Offline: uv is missing or is not uv at all.
 */
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { packagedEnvironment, pythonCommand } from '../server/bridgeProcess';

const LAUNCHER = path.resolve(__dirname, '..', 'electron', 'python-launch.cjs');
const ROOT = path.resolve(__dirname, '..', '..');
const { progress, findUv } = createRequire(import.meta.url)(LAUNCHER) as {
  progress: (line: string) => string | null;
  findUv: (root: string, env: NodeJS.ProcessEnv) => string;
};

let tmp = '';
beforeEach(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'arch-launch-'));
});
afterEach(() => fs.rmSync(tmp, { recursive: true, force: true }));

const env = (extra: Record<string, string>) => ({ ...process.env, ARCHITECT_PYTHON_ENV: path.join(tmp, 'python'), ...extra });
const setupLines = (stdout: string) => stdout.trim().split('\n').filter(Boolean).map((line) => (JSON.parse(line) as { text: string }).text);
const lastLine = (stderr: string) => stderr.trim().split(/\r?\n/).at(-1);
const FIRST = 'Setting up Architect for its first start. This takes a few minutes.';

describe('progress lines', () => {
  it('turns what uv 0.12.3 prints on a first start into a few plain lines', () => {
    const uv = [
      'Downloading cpython-3.12.13-windows-x86_64-none (download) (20.9MiB)',
      'Downloaded cpython-3.12.13-windows-x86_64-none (download)',
      'Using CPython 3.12.13',
      'Creating virtual environment at: env',
      'Downloading pydantic-core (1.9MiB)',
      'Downloaded pydantic-core',
      'Prepared 27 packages in 6.48s',
      'Installed 27 packages in 411ms',
      '+ annotated-doc==0.0.5',
    ];
    expect(uv.map(progress).filter(Boolean)).toEqual([
      'Downloading Python 3.12.13 (20.9 MiB)',
      'Creating the Python environment',
      'Downloading packages',
      'Installed 27 packages',
    ]);
    expect(progress('Downloading cpython-3.12.13-macos-aarch64-none (download) (17.1MiB)')).toBe('Downloading Python 3.12.13 (17.1 MiB)');
  });

  it('uses the uv beside the app, unless ARCHITECT_UV names one', () => {
    const resources = path.join(tmp, 'resources');
    const bundled = path.join(resources, 'uv', process.platform === 'win32' ? 'uv.exe' : 'uv');
    fs.mkdirSync(path.dirname(bundled), { recursive: true });
    fs.writeFileSync(bundled, '');
    expect(findUv(path.join(resources, 'architect'), {})).toBe(bundled);
    expect(findUv(path.join(resources, 'architect'), { ARCHITECT_UV: '/opt/uv' })).toBe('/opt/uv');
    expect(findUv(path.join(tmp, 'elsewhere', 'architect'), {})).toBe('uv');
  });
});

describe('a packaged app', () => {
  const app = () => {
    const resources = path.join(tmp, 'Architect', 'resources');
    const uv = path.join(resources, 'uv', process.platform === 'win32' ? 'uv.exe' : 'uv');
    fs.mkdirSync(path.dirname(uv), { recursive: true });
    fs.writeFileSync(uv, '');
    return {
      uv,
      resources,
      paths: {
        execPath: path.join(tmp, 'Architect', 'Architect.exe'),
        resourcesPath: resources,
        mainDir: path.join(resources, 'app.asar', 'dist-electron'),
        userData: path.join(tmp, 'data'),
      },
    };
  };

  it('starts each conversation through the launcher, on its own binary, with the uv it ships', () => {
    const { uv, resources, paths } = app();
    const env = packagedEnvironment(paths, { LOCALAPPDATA: path.join(tmp, 'local'), PATH: '' });
    expect(env).toEqual({
      ARCHITECT_PYTHON_ENV: process.platform === 'win32' ? path.join(tmp, 'local', 'Architect', 'python') : path.join(tmp, 'data', 'python'),
      ARCHITECT_PACKAGED: '1',
      ARCHITECT_NODE: paths.execPath,
      ARCHITECT_LAUNCHER: path.join(resources, 'app.asar.unpacked', 'dist-electron', 'python-launch.cjs'),
      ARCHITECT_UV: uv,
    });
    const root = path.join(resources, 'architect');
    expect(pythonCommand(root, env)).toEqual({ command: paths.execPath, args: [env.ARCHITECT_LAUNCHER, root], env: { ELECTRON_RUN_AS_NODE: '1' } });
  });

  it('takes ARCHITECT_UV and ARCHITECT_PYTHON_ENV from the environment when set', () => {
    const { paths } = app();
    const env = packagedEnvironment(paths, { ARCHITECT_UV: '/opt/uv', ARCHITECT_PYTHON_ENV: path.join(tmp, 'py') });
    expect(env.ARCHITECT_UV).toBe('/opt/uv');
    expect(env.ARCHITECT_PYTHON_ENV).toBe(path.join(tmp, 'py'));
  });
});

describe('when setup cannot run', () => {
  it('says plainly that its copy of uv is missing', () => {
    const missing = path.join(tmp, 'no-uv', 'uv.exe');
    const run = spawnSync(process.execPath, [LAUNCHER, ROOT], { env: env({ ARCHITECT_UV: missing }), encoding: 'utf8', timeout: 60_000 });
    expect(run.status).toBe(3);
    expect(setupLines(run.stdout)).toEqual([FIRST]);
    expect(lastLine(run.stderr)).toBe(`Architect could not find its copy of uv (${missing}). Reinstall Architect.`);
    expect(fs.existsSync(path.join(tmp, 'python.setup-lock'))).toBe(false);
  });

  it('names the failure, the likely cause and the log when uv fails', () => {
    // Node in place of uv: it exits 1 on `sync`, as uv does on a failed download.
    const run = spawnSync(process.execPath, [LAUNCHER, ROOT], { env: env({ ARCHITECT_UV: process.execPath }), encoding: 'utf8', timeout: 60_000 });
    expect(run.status).toBe(3);
    const log = path.join(tmp, 'setup.log');
    const line = lastLine(run.stderr) ?? '';
    expect(line).toMatch(/^Setup did not finish: .+\. The first start downloads Python and Architect's packages, so check the internet connection, then try again\. Full log: /);
    expect(line.endsWith(log)).toBe(true);
    const text = fs.readFileSync(log, 'utf8');
    expect(text).toContain('sync --frozen --no-install-project --no-dev --python 3.12 --project');
    expect(text).toMatch(/Cannot find module/);
  });
});

describe('one window at a time', () => {
  it('takes over a lock left by a process that is gone', () => {
    const lock = path.join(tmp, 'python.setup-lock');
    fs.mkdirSync(lock, { recursive: true });
    fs.writeFileSync(path.join(lock, 'pid'), '2147483646');
    const run = spawnSync(process.execPath, [LAUNCHER, ROOT], { env: env({ ARCHITECT_UV: path.join(tmp, 'none') }), encoding: 'utf8', timeout: 60_000 });
    expect(setupLines(run.stdout)).toEqual([FIRST]);
    expect(run.status).toBe(3);
  });

  it('waits while another live window holds the lock, then goes on', async () => {
    const lock = path.join(tmp, 'python.setup-lock');
    fs.mkdirSync(lock, { recursive: true });
    fs.writeFileSync(path.join(lock, 'pid'), String(process.pid));
    const child = spawn(process.execPath, [LAUNCHER, ROOT], { env: env({ ARCHITECT_UV: path.join(tmp, 'none') }) });
    let stdout = '';
    child.stdout.on('data', (chunk: Buffer) => (stdout += chunk.toString()));
    const exited = new Promise<number | null>((resolve) => child.on('exit', resolve));
    const start = Date.now();
    while (!stdout.includes('Waiting') && Date.now() - start < 20_000) await new Promise((r) => setTimeout(r, 50));
    expect(setupLines(stdout)).toEqual(['Waiting for another window to finish setting up']);
    fs.rmSync(lock, { recursive: true, force: true });
    expect(await exited).toBe(3);
    expect(setupLines(stdout)).toEqual(['Waiting for another window to finish setting up', FIRST]);
  }, 30_000);
});
