/** Routines: when each schedule fires next, and a run from start to finish. */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { describeSchedule, nextRun, Routines, type Routine } from '../server/routines';

const at = (s: string) => new Date(s).getTime();

describe('schedules', () => {
  it('works out the next run for each kind', () => {
    const wed = at('2026-10-07T10:30:00'); // a Wednesday
    expect(nextRun({ kind: 'manual' }, wed)).toBeNull();
    expect(new Date(nextRun({ kind: 'hourly', time: '00:15' }, wed)!).toString()).toBe(new Date('2026-10-07T11:15:00').toString());
    expect(nextRun({ kind: 'daily', time: '09:00' }, wed)).toBe(at('2026-10-08T09:00:00'));
    expect(nextRun({ kind: 'daily', time: '17:45' }, wed)).toBe(at('2026-10-07T17:45:00'));
    const fri = at('2026-10-09T18:00:00');
    expect(nextRun({ kind: 'weekdays', time: '08:00' }, fri)).toBe(at('2026-10-12T08:00:00')); // skips the weekend
    expect(nextRun({ kind: 'weekly', time: '09:00', day: 1 }, wed)).toBe(at('2026-10-12T09:00:00'));
    expect(describeSchedule({ kind: 'weekly', time: '9:05', day: 5 })).toBe('Fridays at 09:05');
  });
});

describe('routines', () => {
  it('runs a due routine once, records the run, and marks it finished and seen', async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'arout-'));
    const started: string[] = [];
    let lists = 0;
    const routines = new Routines(path.join(dir, 'routines.json'), async (r) => {
      started.push(r.name);
      return `conv-${started.length}`;
    }, () => { lists += 1; });
    const saved = routines.save({ id: 'r1', name: 'Morning check', prompt: 'Summarize open issues', cwd: dir, schedule: { kind: 'daily', time: '09:00' } });
    expect(saved.next).toBeGreaterThan(Date.now());

    // Not due yet; then due.
    await routines.tick(Date.now());
    expect(started).toEqual([]);
    await routines.tick(saved.next! + 1000);
    expect(started).toEqual(['Morning check']);
    const run = routines.list()[0].runs[0];
    expect(run).toMatchObject({ conv: 'conv-1', status: 'running', seen: false });

    routines.finished('conv-1', true);
    routines.markSeen([run.id]);
    expect(routines.list()[0].runs[0]).toMatchObject({ status: 'done', seen: true });
    expect(lists).toBeGreaterThan(2);

    // Saved to disk: a new instance reads it back; paused routines do not run.
    const again = new Routines(path.join(dir, 'routines.json'), async () => 'x', () => undefined);
    expect(again.list().map((r: Routine) => r.name)).toEqual(['Morning check']);
    again.save({ id: 'r1', active: false });
    await again.tick(Date.now() + 10 * 86_400_000);
    expect(again.list()[0].runs).toHaveLength(1);
  });
});
