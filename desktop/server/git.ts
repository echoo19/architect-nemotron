/**
 * Git for the review pane, the header and worktrees (Codex's review pane and worktree
 * flows, Claude Code's branch and worktree options). Every call runs git in the
 * conversation's folder; nothing here touches a remote unless asked to push or open
 * a pull request.
 */
import { execFile } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

export interface GitResult { ok: boolean; stdout: string; stderr: string; code: number }

export function git(cwd: string, args: string[], input?: string, env: NodeJS.ProcessEnv = process.env): Promise<GitResult> {
  return run('git', args, cwd, input, env);
}

function run(cmd: string, args: string[], cwd: string, input: string | undefined, env: NodeJS.ProcessEnv): Promise<GitResult> {
  return new Promise((resolve) => {
    const child = execFile(cmd, args, { cwd, env, maxBuffer: 32 * 1024 * 1024, windowsHide: true }, (err, stdout, stderr) => {
      const code = err ? ((err as NodeJS.ErrnoException & { code?: number | string }).code === 'ENOENT' ? 127 : (typeof (err as { code?: unknown }).code === 'number' ? (err as { code: number }).code : 1)) : 0;
      resolve({ ok: !err, stdout: String(stdout), stderr: String(stderr), code });
    });
    if (input !== undefined) {
      child.stdin?.end(input);
    }
  });
}

export async function hasCommand(cmd: string, env: NodeJS.ProcessEnv = process.env): Promise<boolean> {
  const r = await run(process.platform === 'win32' ? 'where' : 'which', [cmd], process.cwd(), undefined, env);
  return r.ok;
}

export interface GitStatus {
  git: boolean;
  repo: boolean;
  root?: string;
  branch?: string | null;
  detached?: boolean;
  upstream?: string | null;
  ahead?: number;
  behind?: number;
  remote?: boolean;
  gh?: boolean;
  staged: number;
  unstaged: number;
  untracked: number;
  worktree?: { main: string; isLinked: boolean };
  defaultBranch?: string | null;
}

export async function status(cwd: string): Promise<GitStatus> {
  const empty: GitStatus = { git: true, repo: false, staged: 0, unstaged: 0, untracked: 0 };
  if (!fs.existsSync(cwd)) return empty;
  const top = await git(cwd, ['rev-parse', '--show-toplevel']);
  if (top.code === 127) return { ...empty, git: false };
  if (!top.ok) return empty;
  const root = top.stdout.trim();
  const [br, porcelain, remotes, common, gitDir] = await Promise.all([
    git(cwd, ['status', '--porcelain=v2', '--branch']),
    git(cwd, ['status', '--porcelain=v1', '-uall']),
    git(cwd, ['remote']),
    git(cwd, ['rev-parse', '--git-common-dir']),
    git(cwd, ['rev-parse', '--git-dir']),
  ]);
  let branch: string | null = null;
  let upstream: string | null = null;
  let ahead = 0;
  let behind = 0;
  for (const line of br.stdout.split('\n')) {
    if (line.startsWith('# branch.head ')) branch = line.slice(14).trim();
    else if (line.startsWith('# branch.upstream ')) upstream = line.slice(18).trim();
    else if (line.startsWith('# branch.ab ')) {
      const m = /\+(\d+) -(\d+)/.exec(line);
      if (m) {
        ahead = Number(m[1]);
        behind = Number(m[2]);
      }
    }
  }
  let staged = 0;
  let unstaged = 0;
  let untracked = 0;
  for (const line of porcelain.stdout.split('\n')) {
    if (!line) continue;
    if (line.startsWith('??')) untracked += 1;
    else {
      if (line[0] !== ' ') staged += 1;
      if (line[1] !== ' ') unstaged += 1;
    }
  }
  const commonDir = path.resolve(cwd, common.stdout.trim());
  const ownDir = path.resolve(cwd, gitDir.stdout.trim());
  const isLinked = commonDir !== ownDir;
  const def = await git(cwd, ['symbolic-ref', '--short', 'refs/remotes/origin/HEAD']);
  return {
    git: true,
    repo: true,
    root,
    branch: branch === '(detached)' ? null : branch,
    detached: branch === '(detached)',
    upstream,
    ahead,
    behind,
    remote: remotes.stdout.trim().length > 0,
    gh: await ghAvailable(),
    staged,
    unstaged,
    untracked,
    worktree: { main: path.dirname(commonDir), isLinked },
    defaultBranch: def.ok ? def.stdout.trim().replace(/^origin\//, '') : null,
  };
}

let ghCache: Promise<boolean> | null = null;
function ghAvailable(): Promise<boolean> {
  ghCache ??= hasCommand('gh');
  return ghCache;
}

export type Scope = 'unstaged' | 'staged' | 'branch';

/** A unified diff for a review scope. Unstaged includes untracked files as new-file diffs. */
export async function diff(cwd: string, scope: Scope, base?: string): Promise<string> {
  if (scope === 'staged') return (await git(cwd, ['diff', '--cached', '--no-color'])).stdout;
  if (scope === 'branch') {
    const target = base || (await defaultBase(cwd));
    if (!target) return '';
    return (await git(cwd, ['diff', '--no-color', `${target}...HEAD`])).stdout;
  }
  const tracked = (await git(cwd, ['diff', '--no-color'])).stdout;
  const others = (await git(cwd, ['ls-files', '--others', '--exclude-standard'])).stdout.split('\n').filter(Boolean).slice(0, 200);
  let extra = '';
  for (const file of others) {
    const r = await git(cwd, ['diff', '--no-color', '--no-index', '--', process.platform === 'win32' ? 'NUL' : '/dev/null', file]);
    extra += r.stdout;
  }
  return tracked + extra;
}

async function defaultBase(cwd: string): Promise<string | null> {
  for (const candidate of ['origin/HEAD', 'origin/main', 'origin/master', 'main', 'master']) {
    const r = await git(cwd, ['rev-parse', '--verify', '--quiet', candidate]);
    if (r.ok) return candidate;
  }
  return null;
}

/** Apply a patch to the index (stage) or the worktree, forward or reversed. */
export async function applyPatch(cwd: string, patch: string, opts: { cached?: boolean; reverse?: boolean }): Promise<GitResult> {
  const args = ['apply', '--whitespace=nowarn', '--unidiff-zero'];
  if (opts.cached) args.push('--cached');
  if (opts.reverse) args.push('-R');
  args.push('-');
  return git(cwd, args, patch.endsWith('\n') ? patch : `${patch}\n`);
}

export async function stageFile(cwd: string, file: string | null): Promise<GitResult> {
  return git(cwd, file ? ['add', '--', file] : ['add', '-A']);
}

export async function unstageFile(cwd: string, file: string | null): Promise<GitResult> {
  return git(cwd, file ? ['restore', '--staged', '--', file] : ['restore', '--staged', '.']);
}

/** Throw away working-tree changes to a file (or all); a new file is deleted. */
export async function revertFile(cwd: string, file: string | null): Promise<GitResult> {
  if (!file) {
    const a = await git(cwd, ['checkout', '--', '.']);
    const b = await git(cwd, ['clean', '-fd']);
    return { ...a, ok: a.ok && b.ok, stderr: a.stderr + b.stderr };
  }
  const tracked = await git(cwd, ['ls-files', '--error-unmatch', '--', file]);
  if (!tracked.ok) {
    try {
      fs.rmSync(path.join(cwd, file), { force: true });
      return { ok: true, stdout: '', stderr: '', code: 0 };
    } catch (err) {
      return { ok: false, stdout: '', stderr: String(err), code: 1 };
    }
  }
  return git(cwd, ['checkout', '--', file]);
}

export async function commit(cwd: string, message: string, all: boolean): Promise<GitResult> {
  if (all) {
    const add = await git(cwd, ['add', '-A']);
    if (!add.ok) return add;
  }
  return git(cwd, ['commit', '-m', message]);
}

export async function push(cwd: string): Promise<GitResult> {
  const st = await status(cwd);
  if (st.upstream) return git(cwd, ['push']);
  return git(cwd, ['push', '-u', 'origin', st.branch ?? 'HEAD']);
}

export async function pullRequest(cwd: string, title: string, body: string, draft: boolean, env: NodeJS.ProcessEnv): Promise<GitResult & { url?: string }> {
  const args = ['pr', 'create', '--title', title, '--body', body || ' '];
  if (draft) args.push('--draft');
  const r = await run('gh', args, cwd, undefined, env);
  const url = /https:\/\/\S+/.exec(r.stdout)?.[0];
  return { ...r, url };
}

export async function createBranch(cwd: string, name: string): Promise<GitResult> {
  return git(cwd, ['switch', '-c', name]);
}

export async function branches(cwd: string): Promise<string[]> {
  const r = await git(cwd, ['branch', '--format=%(refname:short)', '--sort=-committerdate']);
  return r.stdout.split('\n').map((b) => b.trim()).filter(Boolean).slice(0, 100);
}

// -- worktrees ----------------------------------------------------------------------------

export interface Worktree { path: string; branch: string | null; head: string; main: boolean; architect: boolean }

export async function worktrees(cwd: string): Promise<Worktree[]> {
  const r = await git(cwd, ['worktree', 'list', '--porcelain']);
  if (!r.ok) return [];
  const out: Worktree[] = [];
  let current: Partial<Worktree> = {};
  for (const line of `${r.stdout}\n`.split('\n')) {
    if (line.startsWith('worktree ')) current = { path: path.normalize(line.slice(9).trim()) };
    else if (line.startsWith('HEAD ')) current.head = line.slice(5).trim();
    else if (line.startsWith('branch ')) current.branch = line.slice(7).trim().replace(/^refs\/heads\//, '');
    else if (line === '' && current.path) {
      out.push({
        path: current.path,
        branch: current.branch ?? null,
        head: current.head ?? '',
        main: out.length === 0,
        architect: /[\\/]\.architect[\\/]worktrees[\\/]/.test(current.path),
      });
      current = {};
    }
  }
  return out;
}

/**
 * A fresh worktree for one conversation, under <repo>/.architect/worktrees/<name>, on a
 * new branch `<prefix><name>` from `base` (default: the current HEAD). Uncommitted
 * changes stay where they are.
 */
export async function addWorktree(cwd: string, name: string, base: string | undefined, prefix: string): Promise<{ ok: boolean; path?: string; branch?: string; error?: string }> {
  const top = await git(cwd, ['rev-parse', '--show-toplevel']);
  if (!top.ok) return { ok: false, error: 'not_a_repo' };
  const root = top.stdout.trim();
  const safe = name.toLowerCase().replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40) || 'session';
  let dir = path.join(root, '.architect', 'worktrees', safe);
  let branch = `${prefix}${safe}`;
  for (let i = 2; fs.existsSync(dir) || (await git(cwd, ['rev-parse', '--verify', '--quiet', branch])).ok; i += 1) {
    dir = path.join(root, '.architect', 'worktrees', `${safe}-${i}`);
    branch = `${prefix}${safe}-${i}`;
  }
  ensureIgnored(root);
  const r = await git(cwd, ['worktree', 'add', '-b', branch, dir, base || 'HEAD']);
  if (!r.ok) return { ok: false, error: r.stderr.trim().split('\n').pop() };
  copyIncluded(root, dir);
  return { ok: true, path: dir, branch };
}

/** Keep the worktrees folder out of the main checkout's status. */
function ensureIgnored(root: string): void {
  const exclude = path.join(root, '.git', 'info', 'exclude');
  try {
    const text = fs.existsSync(exclude) ? fs.readFileSync(exclude, 'utf8') : '';
    if (!text.includes('.architect/worktrees')) fs.appendFileSync(exclude, `${text.endsWith('\n') || !text ? '' : '\n'}.architect/worktrees/\n`);
  } catch {
    /* a worktree of a worktree or a read-only repo: status may list it */
  }
}

/** Files named in .worktreeinclude (gitignored ones like .env) are copied into new worktrees. */
function copyIncluded(root: string, dir: string): void {
  const list = path.join(root, '.worktreeinclude');
  if (!fs.existsSync(list)) return;
  for (const raw of fs.readFileSync(list, 'utf8').split('\n')) {
    const rel = raw.trim();
    if (!rel || rel.startsWith('#') || rel.includes('..')) continue;
    const from = path.join(root, rel);
    if (fs.existsSync(from)) {
      fs.mkdirSync(path.dirname(path.join(dir, rel)), { recursive: true });
      fs.cpSync(from, path.join(dir, rel), { recursive: true });
    }
  }
}

export async function removeWorktree(cwd: string, dir: string, force: boolean): Promise<GitResult> {
  return git(cwd, ['worktree', 'remove', ...(force ? ['--force'] : []), dir]);
}

/**
 * Hand off: carry a checkout's uncommitted changes to another checkout of the same
 * repository (worktree to local or back). The source is left clean on success.
 */
export async function handOff(from: string, to: string): Promise<{ ok: boolean; files: number; error?: string }> {
  const patch = await diff(from, 'unstaged');
  const staged = (await git(from, ['diff', '--cached', '--no-color'])).stdout;
  const all = staged + patch;
  if (!all.trim()) return { ok: true, files: 0 };
  const files = (all.match(/^diff --git /gm) ?? []).length;
  const apply = await git(to, ['apply', '--whitespace=nowarn', '-'], all.endsWith('\n') ? all : `${all}\n`);
  if (!apply.ok) return { ok: false, files, error: apply.stderr.trim().split('\n').pop() };
  await git(from, ['reset', '--hard', '-q']);
  await git(from, ['clean', '-fdq']);
  return { ok: true, files };
}

/**
 * Codex's worktree cleanup: keep the newest `keep` of Architect's own worktrees and
 * remove older ones. Never removes a protected path (open or pinned conversations)
 * or one with uncommitted work (git refuses without --force, which this never uses).
 */
export async function pruneWorktrees(cwd: string, keep: number, protect: string[]): Promise<{ removed: string[]; kept: number }> {
  const norm = (p: string) => path.resolve(p).toLowerCase();
  const guarded = new Set(protect.map(norm));
  const own = (await worktrees(cwd)).filter((w) => w.architect && !w.main);
  const dated = own.map((w) => {
    let time = 0;
    try {
      time = fs.statSync(w.path).mtimeMs;
    } catch {
      /* missing folder: oldest */
    }
    return { ...w, time };
  }).sort((a, b) => b.time - a.time);
  const removed: string[] = [];
  for (const w of dated.slice(Math.max(0, keep))) {
    if (guarded.has(norm(w.path))) continue;
    const r = await removeWorktree(cwd, w.path, false);
    if (r.ok) removed.push(w.path);
  }
  return { removed, kept: dated.length - removed.length };
}

/**
 * Bring back a removed worktree on its branch (Codex restores a deleted worktree's
 * chat): forget the stale entry, then check the branch out in the same folder.
 */
export async function restoreWorktree(cwd: string, dir: string, branch: string): Promise<GitResult> {
  await git(cwd, ['worktree', 'prune']);
  const exists = await git(cwd, ['rev-parse', '--verify', '--quiet', `refs/heads/${branch}`]);
  if (!exists.ok) return { ok: false, stdout: '', stderr: `The branch ${branch} no longer exists.`, code: 1 };
  const r = await git(cwd, ['worktree', 'add', dir, branch]);
  if (r.ok) copyIncluded(cwd, dir);
  return r;
}
