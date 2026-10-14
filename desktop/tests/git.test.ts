/**
 * The review pane's git operations against a real temporary repository: status,
 * scopes, staging a single hunk, reverting, committing, worktrees and hand off.
 */
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { beforeEach, describe, expect, it } from 'vitest';
import * as G from '../server/git';

let repo: string;
const sh = (...args: string[]) => execFileSync('git', args, { cwd: repo, stdio: 'pipe' }).toString();

beforeEach(() => {
  repo = fs.mkdtempSync(path.join(os.tmpdir(), 'agit-'));
  sh('init', '-q', '-b', 'main');
  sh('config', 'user.email', 't@example.com');
  sh('config', 'user.name', 't');
  fs.writeFileSync(path.join(repo, 'a.txt'), 'one\ntwo\nthree\nfour\nfive\nsix\nseven\neight\nnine\nten\n');
  sh('add', '.');
  sh('commit', '-qm', 'start');
});

describe('review pane git', () => {
  it('reports status and splits unstaged from staged', async () => {
    fs.writeFileSync(path.join(repo, 'a.txt'), 'ONE\ntwo\nthree\nfour\nfive\nsix\nseven\neight\nnine\nTEN\n');
    fs.writeFileSync(path.join(repo, 'new.txt'), 'hello\n');
    const st = await G.status(repo);
    expect(st).toMatchObject({ repo: true, branch: 'main', unstaged: 1, untracked: 1, staged: 0 });
    const unstaged = await G.diff(repo, 'unstaged');
    expect(unstaged).toContain('+ONE');
    expect(unstaged).toContain('new.txt');
    expect(await G.diff(repo, 'staged')).toBe('');
  });

  it('stages one hunk of a file and reverts the rest', async () => {
    fs.writeFileSync(path.join(repo, 'a.txt'), 'ONE\ntwo\nthree\nfour\nfive\nsix\nseven\neight\nnine\nTEN\n');
    const full = await G.diff(repo, 'unstaged');
    const header = full.slice(0, full.indexOf('@@'));
    const hunks = full.slice(full.indexOf('@@')).split(/(?=^@@)/m);
    expect(hunks.length).toBe(2);
    const staged = await G.applyPatch(repo, header + hunks[0], { cached: true });
    expect(staged.ok).toBe(true);
    expect(await G.diff(repo, 'staged')).toContain('+ONE');
    expect(await G.diff(repo, 'unstaged')).toContain('+TEN');
    const reverted = await G.revertFile(repo, 'a.txt');
    expect(reverted.ok).toBe(true);
    expect(await G.diff(repo, 'unstaged')).toBe('');
    const done = await G.commit(repo, 'first line', false);
    expect(done.ok).toBe(true);
    expect(sh('log', '--oneline').split('\n')[0]).toContain('first line');
  });

  it('makes a worktree on its own branch and hands its changes back', async () => {
    const wt = await G.addWorktree(repo, 'Fix the Thing', undefined, 'architect/');
    expect(wt.ok).toBe(true);
    expect(wt.branch).toBe('architect/fix-the-thing');
    expect(fs.existsSync(path.join(wt.path!, 'a.txt'))).toBe(true);
    const list = await G.worktrees(repo);
    expect(list.some((w) => w.architect && w.branch === 'architect/fix-the-thing')).toBe(true);
    expect((await G.status(repo)).untracked).toBe(0); // the worktrees folder is excluded

    fs.writeFileSync(path.join(wt.path!, 'a.txt'), 'changed in the worktree\n');
    const handed = await G.handOff(wt.path!, repo);
    expect(handed).toMatchObject({ ok: true, files: 1 });
    expect(fs.readFileSync(path.join(repo, 'a.txt'), 'utf8').replace(/\r\n/g, '\n')).toBe('changed in the worktree\n');
    expect(await G.diff(wt.path!, 'unstaged')).toBe('');

    const removed = await G.removeWorktree(repo, wt.path!, true);
    expect(removed.ok).toBe(true);
  });

  it('keeps the newest worktrees, and never protected or uncommitted ones', async () => {
    const made: string[] = [];
    for (const name of ['one', 'two', 'three', 'four']) {
      const wt = await G.addWorktree(repo, name, undefined, 'architect/');
      made.push(wt.path!);
      const t = new Date(Date.now() - (10 - made.length) * 60_000);
      fs.utimesSync(wt.path!, t, t); // one is oldest, four is newest
    }
    fs.writeFileSync(path.join(made[1], 'a.txt'), 'unsaved work\n'); // two has uncommitted work
    const out = await G.pruneWorktrees(repo, 1, [made[0]]); // one is open somewhere
    expect(out.removed.map((p) => path.basename(p))).toEqual(['three']);
    expect(fs.existsSync(made[0]) && fs.existsSync(made[1]) && fs.existsSync(made[3])).toBe(true);
  });

  it('brings a removed worktree back on its branch', async () => {
    const wt = await G.addWorktree(repo, 'keep me', undefined, 'architect/');
    fs.writeFileSync(path.join(wt.path!, 'b.txt'), 'kept\n');
    execFileSync('git', ['add', '.'], { cwd: wt.path! });
    execFileSync('git', ['commit', '-qm', 'work in the worktree'], { cwd: wt.path! });
    expect((await G.removeWorktree(repo, wt.path!, false)).ok).toBe(true);
    expect(fs.existsSync(wt.path!)).toBe(false);

    const back = await G.restoreWorktree(repo, wt.path!, wt.branch!);
    expect(back.ok).toBe(true);
    expect(fs.readFileSync(path.join(wt.path!, 'b.txt'), 'utf8').replace(/\r\n/g, '\n')).toBe('kept\n');
    expect((await G.restoreWorktree(repo, path.join(repo, 'x'), 'architect/nope')).ok).toBe(false);
  });

  it('says plainly when a folder is not a repository', async () => {
    const plain = fs.mkdtempSync(path.join(os.tmpdir(), 'aplain-'));
    expect(await G.status(plain)).toMatchObject({ git: true, repo: false });
  });
});
