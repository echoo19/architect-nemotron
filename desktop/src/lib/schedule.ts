/** Routines as the page sees them (server/routines.ts keeps and runs them). */
export type ScheduleKind = 'manual' | 'hourly' | 'daily' | 'weekdays' | 'weekly';
export interface Schedule { kind: ScheduleKind; time?: string; day?: number }
export interface RoutineRun { id: string; at: number; conv: string; status: 'running' | 'done' | 'failed'; seen: boolean; error?: string }
export interface Routine {
  id: string;
  name: string;
  prompt: string;
  cwd: string;
  schedule: Schedule;
  active: boolean;
  created: number;
  next?: number | null;
  runs: RoutineRun[];
}

export const DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];

export function describeSchedule(schedule: Schedule): string {
  const [h, m] = (schedule.time ?? '09:00').split(':').map(Number);
  const clock = new Date(2000, 0, 1, h || 0, m || 0).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  switch (schedule.kind) {
    case 'manual': return 'When you run it';
    case 'hourly': return `Every hour at :${String(m || 0).padStart(2, '0')}`;
    case 'daily': return `Every day at ${clock}`;
    case 'weekdays': return `Weekdays at ${clock}`;
    case 'weekly': return `${DAYS[schedule.day ?? 1]}s at ${clock}`;
  }
}

export function unseenRuns(routines: Routine[]): number {
  // Only runs with a conversation to open count; one that never started cannot be read.
  return routines.reduce((n, r) => n + r.runs.filter((run) => !run.seen && run.status === 'done').length, 0);
}
