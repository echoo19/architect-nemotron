/** Forge's harness versions as diffs (lib/linediff.ts). */
import { describe, expect, it } from 'vitest';
import { diffCount, diffHunks, diffLines } from '../src/lib/linediff';

const rebuild = (before: string, after: string) => {
  const lines = diffLines(before, after);
  return {
    before: lines.filter((l) => l.kind !== 'add').map((l) => l.text),
    after: lines.filter((l) => l.kind !== 'del').map((l) => l.text),
  };
};

/** A small deterministic generator, so a failure names its seed. */
function random(seed: number): () => number {
  let state = seed;
  return () => {
    state = (state * 1103515245 + 12345) % 2147483648;
    return state / 2147483648;
  };
}

describe('line diffs', () => {
  it('finds nothing between equal texts', () => {
    expect(diffHunks('a\nb\n', 'a\nb\n')).toEqual([]);
    expect(diffCount('a\nb\n', 'a\nb\n')).toEqual({ added: 0, removed: 0 });
  });

  it('shows one changed line with its context and line numbers', () => {
    const before = ['def run(context, task):', '    a = 1', '    b = 2', '    c = 3', '    return a'].join('\n');
    const after = before.replace('    b = 2', '    b = 20');
    const [hunk, ...rest] = diffHunks(before, after, 1);
    expect(rest).toEqual([]);
    expect(hunk.lines.map((l) => [l.kind, l.text])).toEqual([
      ['ctx', '    a = 1'], ['del', '    b = 2'], ['add', '    b = 20'], ['ctx', '    c = 3'],
    ]);
    expect(hunk.oldStart).toBe(2);
    expect(hunk.lines[1].oldNo).toBe(3);
    expect(hunk.lines[2].newNo).toBe(3);
  });

  it('handles a first version, an emptied file and lines added at either end', () => {
    expect(diffCount('', 'a\nb')).toEqual({ added: 2, removed: 0 });
    expect(diffCount('a\nb', '')).toEqual({ added: 0, removed: 2 });
    expect(rebuild('b\nc', 'a\nb\nc\nd')).toEqual({ before: ['b', 'c'], after: ['a', 'b', 'c', 'd'] });
  });

  it('turns any version into any other', () => {
    const next = random(7);
    const words = ['x = 1', 'y = 2', 'return x', 'pass', 'reply = await context.call(t)', '# note', ''];
    for (let round = 0; round < 200; round += 1) {
      const make = () => Array.from({ length: Math.floor(next() * 12) }, () => words[Math.floor(next() * words.length)]).join('\n');
      const before = make();
      const after = make();
      const split = (t: string) => (t ? t.split('\n') : []);
      const got = rebuild(before, after);
      // A trailing empty line is the file's final newline, not a line of its own.
      const trim = (lines: string[]) => (lines.at(-1) === '' ? lines.slice(0, -1) : lines);
      expect(got.before, `round ${round}`).toEqual(trim(split(before)));
      expect(got.after, `round ${round}`).toEqual(trim(split(after)));
    }
  });
});
