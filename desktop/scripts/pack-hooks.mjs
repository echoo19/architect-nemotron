// electron-builder hooks (electron-builder.yml: beforePack, afterPack).
//
// Before packing each platform and architecture: fetch its pinned uv
// (scripts/fetch-uv.mjs) and stage Pi's dependencies for it (scripts/stage-pi.mjs).
// After packing: refuse an app that could not start on a clean machine, one with no uv,
// no Pi dependencies, no Python core or lockfile, or a uv that is not the pinned one, and
// one without the release public key and the signed receipt results that `architect
// verify` checks; and give an unsigned Mac test build an ad-hoc signature so that it
// launches.
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fetchUv, uvDir } from './fetch-uv.mjs';
import { stagePi } from './stage-pi.mjs';

const { Arch } = createRequire(import.meta.url)('builder-util');

function target(context) {
  const platform = context.electronPlatformName === 'mas' ? 'darwin' : context.electronPlatformName;
  return { platform, arch: Arch[context.arch] };
}

export async function beforePack(context) {
  const { platform, arch } = target(context);
  if (arch === 'universal') throw new Error('Build each Mac architecture on its own (-- --arm64 or -- --x64): uv and Pi are per architecture.');
  await fetchUv({ platform, arch });
  stagePi({ platform, arch });
}

export async function afterPack(context) {
  const { platform, arch } = target(context);
  const resources = platform === 'darwin'
    ? path.join(context.appOutDir, `${context.packager.appInfo.productFilename}.app`, 'Contents', 'Resources')
    : path.join(context.appOutDir, 'resources');
  const exe = platform === 'win32' ? 'uv.exe' : 'uv';
  const pi = 'architect/architect/backends/pi-node';
  const required = {
    uv: `uv/${exe}`,
    "uv's license": 'uv/LICENSE-MIT.txt',
    "Architect's Python core": 'architect/architect/desktop/bridge.py',
    // What `architect verify` needs to check the published results on a clean machine.
    'the release public key': 'architect/architect/keys/architect-ed25519.pub',
    'the signed receipt results': 'architect/architect/published/receipts/SIGNED.json',
    'pyproject.toml': 'architect/pyproject.toml',
    'uv.lock': 'architect/uv.lock',
    'the Pi bridge': `${pi}/bridge.mjs`,
    "Pi's dependencies": `${pi}/node_modules/@mariozechner/pi-coding-agent/package.json`,
    'the Python launcher': 'app.asar.unpacked/dist-electron/python-launch.cjs',
  };
  const missing = Object.entries(required).filter(([, file]) => !fs.existsSync(path.join(resources, file))).map(([name]) => name);
  if (missing.length) throw new Error(`afterPack: the app in ${resources} has no ${missing.join(', ')}`);
  const uv = path.join(resources, 'uv', exe);
  const pinned = JSON.parse(fs.readFileSync(path.join(uvDir(platform, arch), 'uv-source.json'), 'utf8')).sha256;
  if (crypto.createHash('sha256').update(fs.readFileSync(uv)).digest('hex') !== pinned) throw new Error(`afterPack: ${uv} is not the pinned uv`);
  if (platform !== 'win32' && !(fs.statSync(uv).mode & 0o111)) throw new Error(`afterPack: ${uv} is not executable`);
  if (fs.existsSync(path.join(resources, pi, 'node_modules', '.bin'))) throw new Error('afterPack: the app ships an unstaged copy of Pi\'s node_modules');
  console.log(`afterPack: ${platform}-${arch} carries uv, Pi's dependencies and Architect's Python core`);
  if (platform === 'darwin' && context.packager.platformSpecificBuildOptions.identity === null) {
    // An unsigned test build (-c.mac.identity=null): electron-builder signs nothing, and
    // Apple silicon will not run an app whose signature no longer matches its files.
    const bundle = path.join(context.appOutDir, `${context.packager.appInfo.productFilename}.app`);
    execFileSync('codesign', ['--force', '--deep', '--sign', '-', bundle], { stdio: 'inherit' });
    console.log(`afterPack: ad-hoc signed ${bundle}`);
  }
}
