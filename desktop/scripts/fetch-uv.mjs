// The uv that ships inside the packaged app (resources/uv), so a clean machine needs no
// uv of its own: electron/python-launch.cjs uses it to set up Python on first start.
//
// Downloads the release asset pinned below for one platform and architecture, refuses it
// unless its sha256 matches the pin, and unpacks the uv binary into
// vendor/uv/<platform>-<arch>/ (ignored by git; binaries are never committed). The
// electron-builder beforePack hook (scripts/pack-hooks.mjs) calls this per target.
//
//   node scripts/fetch-uv.mjs [--platform win32|darwin|linux] [--arch x64|arm64] [--all]
//
// To move to a newer uv: change UV_VERSION and every hash, from the `.sha256` files
// published with that release on https://github.com/astral-sh/uv/releases.
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import zlib from 'node:zlib';

export const UV_VERSION = '0.12.3';
const ASSETS = {
  'win32-x64': ['uv-x86_64-pc-windows-msvc.zip', 'b23350c79e8ad0192b8124af13a0f17e8d4e4549524785e1aef389ae5a06990e'],
  'win32-arm64': ['uv-aarch64-pc-windows-msvc.zip', '4343217d668727b8a8eb5cad92389a1d2eeead93c89940d1b955ba1bb15462eb'],
  'darwin-arm64': ['uv-aarch64-apple-darwin.tar.gz', '546f7f8a6c70ff13a3a9d2bc958db3427298cebf3e0cb756f9177133b7068843'],
  'darwin-x64': ['uv-x86_64-apple-darwin.tar.gz', '4c9f52262a14da336e4a42ed24992d12d0c956acde87619e4611d321dffa602b'],
  'linux-x64': ['uv-x86_64-unknown-linux-gnu.tar.gz', '600cf9a742aca00d292673b16b5acffaa7b8c269a364ad0c2e79498dcb1fe101'],
  'linux-arm64': ['uv-aarch64-unknown-linux-gnu.tar.gz', 'bb66cb52e7b1823aed1183630d8d8e5c958840d584a4c55ec10a4cfc168dcca2'],
};
export const TARGETS = Object.keys(ASSETS);

const DESKTOP = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const VENDOR = path.join(DESKTOP, 'vendor', 'uv');
const DOWNLOADS = path.join(VENDOR, 'downloads');

const sha256 = (bytes) => crypto.createHash('sha256').update(bytes).digest('hex');
const readJson = (file) => {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return null;
  }
};

/** Where electron-builder.yml expects uv for this target. */
export function uvDir(platform, arch) {
  return path.join(VENDOR, `${platform}-${arch}`);
}

/** The archive, from the download cache or GitHub, only if it matches its pin. */
async function archive(file, pinned, log) {
  const cached = path.join(DOWNLOADS, file);
  if (fs.existsSync(cached)) {
    const bytes = fs.readFileSync(cached);
    if (sha256(bytes) === pinned) return bytes;
    fs.rmSync(cached);
  }
  const url = `https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${file}`;
  log(`fetch-uv: downloading ${url}`);
  const response = await fetch(url, { redirect: 'follow' });
  if (!response.ok) throw new Error(`fetch-uv: ${url} answered ${response.status}`);
  const bytes = Buffer.from(await response.arrayBuffer());
  const actual = sha256(bytes);
  if (actual !== pinned) throw new Error(`fetch-uv: ${file} has sha256 ${actual}, but ${pinned} is pinned. Refusing it.`);
  fs.mkdirSync(DOWNLOADS, { recursive: true });
  fs.writeFileSync(`${cached}.part`, bytes);
  fs.renameSync(`${cached}.part`, cached);
  return bytes;
}

/** One file from a zip archive, found by its base name (stored or deflated entries). */
function fromZip(bytes, name) {
  let end = -1;
  for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 65_557); i -= 1) {
    if (bytes.readUInt32LE(i) === 0x06054b50) {
      end = i;
      break;
    }
  }
  if (end < 0) throw new Error('fetch-uv: not a zip archive');
  let at = bytes.readUInt32LE(end + 16);
  for (let n = bytes.readUInt16LE(end + 10); n > 0; n -= 1) {
    if (bytes.readUInt32LE(at) !== 0x02014b50) throw new Error('fetch-uv: broken zip directory');
    const method = bytes.readUInt16LE(at + 10);
    const packed = bytes.readUInt32LE(at + 20);
    const size = bytes.readUInt32LE(at + 24);
    const nameLength = bytes.readUInt16LE(at + 28);
    const local = bytes.readUInt32LE(at + 42);
    const entry = bytes.toString('utf8', at + 46, at + 46 + nameLength);
    at += 46 + nameLength + bytes.readUInt16LE(at + 30) + bytes.readUInt16LE(at + 32);
    if (path.posix.basename(entry) !== name) continue;
    if (packed === 0xffffffff || size === 0xffffffff) throw new Error('fetch-uv: zip64 entries are not supported');
    const start = local + 30 + bytes.readUInt16LE(local + 26) + bytes.readUInt16LE(local + 28);
    const data = bytes.subarray(start, start + packed);
    const out = method === 0 ? Buffer.from(data) : method === 8 ? zlib.inflateRawSync(data) : null;
    if (!out) throw new Error(`fetch-uv: zip method ${method} is not supported`);
    if (out.length !== size) throw new Error(`fetch-uv: ${entry} unpacked to ${out.length} bytes, not ${size}`);
    return out;
  }
  throw new Error(`fetch-uv: ${name} is not in the archive`);
}

/** One regular file from a .tar.gz archive, found by its base name. */
function fromTarGz(bytes, name) {
  const tar = zlib.gunzipSync(bytes);
  for (let at = 0; at + 512 <= tar.length;) {
    const header = tar.subarray(at, at + 512);
    if (header.every((byte) => byte === 0)) break;
    const field = (offset, length) => header.toString('utf8', offset, offset + length).replace(/\0[\s\S]*$/, '');
    const prefix = field(345, 155);
    const entry = prefix ? `${prefix}/${field(0, 100)}` : field(0, 100);
    const size = parseInt(field(124, 12).trim() || '0', 8);
    const type = header[156];
    const start = at + 512;
    if ((type === 0 || type === 48) && path.posix.basename(entry) === name) return Buffer.from(tar.subarray(start, start + size));
    at = start + Math.ceil(size / 512) * 512;
  }
  throw new Error(`fetch-uv: ${name} is not in the archive`);
}

/** The pinned uv for one target, unpacked into vendor/uv/<platform>-<arch>/; returns its path. */
export async function fetchUv({ platform = process.platform, arch = process.arch, log = console.log } = {}) {
  const target = `${platform}-${arch}`;
  const asset = ASSETS[target];
  if (!asset) throw new Error(`fetch-uv: no uv is pinned for ${target} (scripts/fetch-uv.mjs)`);
  const [file, pinned] = asset;
  const exe = platform === 'win32' ? 'uv.exe' : 'uv';
  const dir = uvDir(platform, arch);
  const binary = path.join(dir, exe);
  const record = path.join(dir, 'uv-source.json');
  const known = readJson(record);
  if (known?.archiveSha256 === pinned && fs.existsSync(binary) && sha256(fs.readFileSync(binary)) === known.sha256) return binary;
  const bytes = await archive(file, pinned, log);
  const uv = file.endsWith('.zip') ? fromZip(bytes, exe) : fromTarGz(bytes, exe);
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(binary, uv, { mode: 0o755 });
  fs.chmodSync(binary, 0o755);
  // Shipped beside the binary: where it came from, for anyone checking the app.
  fs.writeFileSync(record, `${JSON.stringify({
    version: UV_VERSION, asset: file, archiveSha256: pinned, sha256: sha256(uv),
    source: `https://github.com/astral-sh/uv/releases/tag/${UV_VERSION}`,
  }, null, 2)}\n`);
  log(`fetch-uv: uv ${UV_VERSION} for ${target} in ${path.relative(DESKTOP, dir)}`);
  return binary;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const option = (name) => {
    const index = process.argv.indexOf(`--${name}`);
    return index > 0 ? process.argv[index + 1] : undefined;
  };
  const targets = process.argv.includes('--all')
    ? TARGETS
    : [`${option('platform') ?? process.platform}-${option('arch') ?? process.arch}`];
  for (const target of targets) {
    const [platform, arch] = target.split('-');
    await fetchUv({ platform, arch });
  }
}
