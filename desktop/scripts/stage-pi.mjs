// Pi's Node dependencies for the packaged app, so the coder and Forge run with no npm
// on the user's machine (Pi itself runs on the app's own Electron binary as Node).
//
// A clean `npm ci` of architect/backends/pi-node's committed lockfile for one platform
// and architecture, into vendor/pi-node/<platform>-<arch>/node_modules (ignored by git),
// which electron-builder ships beside bridge.mjs. Install scripts stay off: koffi ships
// prebuilt binaries for every platform, and the others only print notes. Then it leaves
// out what Pi never loads: source maps, type declarations, .bin links and other
// platforms' koffi builds. That halves the size and the file count, and keeps installed
// paths well inside the Windows path limit. The electron-builder beforePack hook
// (scripts/pack-hooks.mjs) calls this per target.
//
//   node scripts/stage-pi.mjs [--platform win32|darwin|linux] [--arch x64|arm64]
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const DESKTOP = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SOURCE = path.join(DESKTOP, '..', 'architect', 'backends', 'pi-node');
const VENDOR = path.join(DESKTOP, 'vendor', 'pi-node');
// Raise when what is left out changes, so every staged copy is made again.
const PRUNE_VERSION = 1;
const DROPPED = /\.(map|d\.ts|d\.mts|d\.cts)$/;
const NEEDED = ['@mariozechner/pi-coding-agent/package.json', '@mariozechner/pi-ai/package.json', '@mariozechner/pi-agent-core/package.json'];

const readJson = (file) => {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return null;
  }
};

/** Where electron-builder.yml expects Pi's node_modules for this target. */
export function piDir(platform, arch) {
  return path.join(VENDOR, `${platform}-${arch}`);
}

function npm(args, cwd) {
  const cli = process.env.npm_execpath; // set when this runs under `npm run`
  if (cli && /npm-cli\.c?js$/.test(cli)) execFileSync(process.execPath, [cli, ...args], { cwd, stdio: 'inherit' });
  else if (process.platform === 'win32') execFileSync('cmd.exe', ['/d', '/s', '/c', 'npm', ...args], { cwd, stdio: 'inherit' });
  else execFileSync('npm', args, { cwd, stdio: 'inherit' });
}

/** Remove what Pi never loads; returns how many files went. */
function prune(modules, platform, arch) {
  const keep = new Set([`${platform}_${arch}`, ...(platform === 'linux' ? [`musl_${arch}`] : [])]);
  let removed = 0;
  const count = (dir) => fs.readdirSync(dir, { withFileTypes: true, recursive: true }).filter((entry) => !entry.isDirectory()).length;
  const walk = (dir) => {
    const koffiBuilds = path.basename(dir) === 'koffi' && path.basename(path.dirname(dir)) === 'build';
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        if (entry.name === '.bin' || (koffiBuilds && !keep.has(entry.name))) {
          removed += count(full);
          fs.rmSync(full, { recursive: true, force: true });
        } else {
          walk(full);
        }
      } else if (DROPPED.test(entry.name)) {
        fs.rmSync(full, { force: true });
        removed += 1;
      }
    }
  };
  walk(modules);
  return removed;
}

/** Pi's node_modules for one target, staged once per lockfile; returns the staging folder. */
export function stagePi({ platform = process.platform, arch = process.arch, log = console.log } = {}) {
  const dir = piDir(platform, arch);
  const modules = path.join(dir, 'node_modules');
  const manifest = fs.readFileSync(path.join(SOURCE, 'package.json'));
  const lock = fs.readFileSync(path.join(SOURCE, 'package-lock.json'));
  const key = crypto.createHash('sha256').update(manifest).update(lock).update(`\n${platform}\n${arch}\n${PRUNE_VERSION}\n`).digest('hex');
  const record = path.join(dir, 'staged.json');
  if (readJson(record)?.key === key && NEEDED.every((file) => fs.existsSync(path.join(modules, file)))) return dir;
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'package.json'), manifest);
  fs.writeFileSync(path.join(dir, 'package-lock.json'), lock);
  const args = ['ci', '--omit=dev', '--ignore-scripts', '--no-audit', '--no-fund'];
  if (platform !== process.platform) args.push(`--os=${platform}`);
  if (arch !== process.arch) args.push(`--cpu=${arch}`);
  log(`stage-pi: npm ${args.join(' ')} for ${platform}-${arch}`);
  npm(args, dir);
  const removed = prune(modules, platform, arch);
  const missing = NEEDED.filter((file) => !fs.existsSync(path.join(modules, file)));
  if (missing.length) throw new Error(`stage-pi: npm ci left out ${missing.join(', ')}`);
  const koffi = path.join(modules, 'koffi', 'build', 'koffi');
  if (fs.existsSync(koffi) && !fs.readdirSync(koffi).length) throw new Error(`stage-pi: koffi has no build for ${platform}-${arch}`);
  fs.writeFileSync(record, `${JSON.stringify({ key, platform, arch, removedFiles: removed }, null, 2)}\n`);
  log(`stage-pi: Pi's dependencies for ${platform}-${arch} in ${path.relative(DESKTOP, modules)} (${removed} files left out)`);
  return dir;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const option = (name) => {
    const index = process.argv.indexOf(`--${name}`);
    return index > 0 ? process.argv[index + 1] : undefined;
  };
  stagePi({ platform: option('platform') ?? process.platform, arch: option('arch') ?? process.arch });
}
