/**
 * Routines (Claude Code's Routines, Codex's Scheduled): instructions that run on a
 * schedule, each run a conversation of its own in the routine's folder. The schedule
 * lives here in the server, so runs happen while the app is open even with no window
 * showing; a run missed while the app was closed runs once when it opens again.
 */
import fs from 'node:fs';
import path from 'node:path';

export type ScheduleKind = 'manual' | 'hourly' | 'daily' | 'weekdays' | 'weekly';
export interface Schedule { kind: ScheduleKind; /** "HH:MM", local time (hourly uses the minutes). */ time?: string; /** 0 Sunday .. 6 Saturday, for weekly. */ day?: number }
export interface Run { id: string; at: number; conv: string; status: 'running' | 'done' | 'failed'; seen: boolean; /** Why a run did not start, in a word (cwd_missing, no_provider...). */ error?: string }
export interface Routine {
  id: string;
  name: string;
  prompt: string;
  cwd: string;
  schedule: Schedule;
  active: boolean;
  created: number;
  next?: number | null;
  runs: Run[];
}

const KEEP_RUNS = 30;

function parseTime(time = '09:00'): [number, number] {
  const match = /^(\d{1,2}):(\d{2})$/.exec(time);
  const hours = match ? Math.min(23, Number(match[1])) : 9;
  const minutes = match ? Math.min(59, Number(match[2])) : 0;
  return [hours, minutes];
}

/** The next time a schedule fires after `after` (ms), or null for a manual routine. */
export function nextRun(schedule: Schedule, after: number): number | null {
  if (schedule.kind === 'manual') return null;
  const [hours, minutes] = parseTime(schedule.time);
  const start = new Date(after);
  if (schedule.kind === 'hourly') {
    const at = new Date(start);
    at.setSeconds(0, 0);
    at.setMinutes(minutes);
    if (at.getTime() <= after) at.setHours(at.getHours() + 1);
    return at.getTime();
  }
  for (let offset = 0; offset < 8; offset += 1) {
    const at = new Date(start);
    at.setDate(start.getDate() + offset);
    at.setHours(hours, minutes, 0, 0);
    if (at.getTime() <= after) continue;
    const day = at.getDay();
    if (schedule.kind === 'weekdays' && (day === 0 || day === 6)) continue;
    if (schedule.kind === 'weekly' && day !== (schedule.day ?? 1)) continue;
    return at.getTime();
  }
  return null;
}

export function describeSchedule(schedule: Schedule): string {
  const [hours, minutes] = parseTime(schedule.time);
  const clock = `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}`;
  const days = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
  switch (schedule.kind) {
    case 'manual': return 'When you run it';
    case 'hourly': return `Every hour at :${String(minutes).padStart(2, '0')}`;
    case 'daily': return `Every day at ${clock}`;
    case 'weekdays': return `Weekdays at ${clock}`;
    case 'weekly': return `${days[schedule.day ?? 1]}s at ${clock}`;
  }
}

/** Starts one run: opens a conversation in the routine's folder and sends its instructions. */
export type StartRun = (routine: Routine, runId: string) => Promise<string>;

export class Routines {
  private routines: Routine[];
  private timer: NodeJS.Timeout | null = null;

  constructor(private readonly file: string, private readonly start: StartRun, private readonly changed: (list: Routine[]) => void) {
    this.routines = Routines.read(file);
  }

  private static read(file: string): Routine[] {
    try {
      const data = JSON.parse(fs.readFileSync(file, 'utf8')) as unknown;
      return Array.isArray(data) ? (data as Routine[]) : [];
    } catch {
      return [];
    }
  }

  private write(): void {
    fs.mkdirSync(path.dirname(this.file), { recursive: true });
    const temporary = `${this.file}.tmp`;
    fs.writeFileSync(temporary, JSON.stringify(this.routines, null, 1), 'utf8');
    fs.renameSync(temporary, this.file);
    this.changed(this.list());
  }

  list(): Routine[] {
    return this.routines.map((r) => ({ ...r, runs: [...r.runs] }));
  }

  /** Create or replace a routine; its next run is worked out from now. */
  save(input: Partial<Routine> & { id: string }): Routine {
    const existing = this.routines.find((r) => r.id === input.id);
    const routine: Routine = {
      id: input.id,
      name: String(input.name ?? existing?.name ?? 'Routine').slice(0, 120) || 'Routine',
      prompt: String(input.prompt ?? existing?.prompt ?? ''),
      cwd: String(input.cwd ?? existing?.cwd ?? ''),
      schedule: input.schedule ?? existing?.schedule ?? { kind: 'manual' },
      active: input.active ?? existing?.active ?? true,
      created: existing?.created ?? Date.now(),
      runs: existing?.runs ?? [],
    };
    routine.next = routine.active ? nextRun(routine.schedule, Date.now()) : null;
    this.routines = existing ? this.routines.map((r) => (r.id === routine.id ? routine : r)) : [...this.routines, routine];
    this.write();
    return routine;
  }

  remove(id: string): boolean {
    const before = this.routines.length;
    this.routines = this.routines.filter((r) => r.id !== id);
    if (this.routines.length !== before) this.write();
    return this.routines.length !== before;
  }

  async runNow(id: string): Promise<Run | null> {
    const routine = this.routines.find((r) => r.id === id);
    if (!routine || !routine.prompt.trim()) return null;
    const runId = `run${Date.now().toString(36)}`;
    const run: Run = { id: runId, at: Date.now(), conv: '', status: 'running', seen: false };
    routine.runs = [run, ...routine.runs].slice(0, KEEP_RUNS);
    this.write();
    try {
      run.conv = await this.start(routine, runId);
    } catch (err) {
      run.status = 'failed';
      const code = (err as { code?: string }).code ?? (err as Error).message;
      run.error = typeof code === 'string' && /^[a-z_]{2,40}$/.test(code) ? code : 'failed';
    }
    this.write();
    return run;
  }

  /** A run's conversation finished a turn (or failed to start). */
  finished(conv: string, ok: boolean): void {
    for (const routine of this.routines) {
      const run = routine.runs.find((r) => r.conv === conv && r.status === 'running');
      if (run) {
        run.status = ok ? 'done' : 'failed';
        this.write();
        return;
      }
    }
  }

  markSeen(runIds: string[]): void {
    let any = false;
    for (const routine of this.routines) {
      for (const run of routine.runs) {
        if (runIds.includes(run.id) && !run.seen) {
          run.seen = true;
          any = true;
        }
      }
    }
    if (any) this.write();
  }

  /** Run what is due (including one catch-up for a run missed while closed). */
  async tick(now = Date.now()): Promise<void> {
    for (const routine of this.routines) {
      if (!routine.active) continue;
      if (routine.next === undefined) routine.next = nextRun(routine.schedule, now);
      if (routine.next !== null && routine.next !== undefined && routine.next <= now) {
        routine.next = nextRun(routine.schedule, now);
        await this.runNow(routine.id);
      }
    }
  }

  startTimer(everyMs = 30_000): void {
    if (this.timer) return;
    void this.tick();
    this.timer = setInterval(() => void this.tick(), everyMs);
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }
}
