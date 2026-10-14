/**
 * Routines (Claude Code's Routines, Codex's Scheduled): instructions Architect runs
 * on a schedule in a folder you choose. Each run is a conversation of its own; runs
 * you have not opened yet are counted in the sidebar.
 */
import {
  IconCalendarRepeat, IconCheck, IconChevronRight, IconFolder, IconPlayerPlay, IconPlus, IconTrash, IconX,
} from '@tabler/icons-react';
import clsx from 'clsx';
import { useState } from 'react';
import { DAYS, describeSchedule, type Routine, type ScheduleKind } from '../../lib/schedule';
import { errorText, useApp } from '../../store/app';
import { folderName } from '../chat/ConversationView';
import { relativeTime } from '../shell/Sidebar';
import { chooseFolder } from '../shell/folders';
import { useConfirm } from '../ui/useConfirm';
import { Button } from '../ui/Button';
import { Input, TextArea } from '../ui/Field';
import { PageHeader } from '../ui/PageHeader';
import { Segmented } from '../ui/Segmented';
import { WorkingMark } from '../ui/Working';

const KINDS: Array<[ScheduleKind, string]> = [['manual', 'Manual'], ['hourly', 'Hourly'], ['daily', 'Daily'], ['weekdays', 'Weekdays'], ['weekly', 'Weekly']];

async function save(routine: Partial<Routine> & { id: string }): Promise<boolean> {
  const r = await fetch(`/api/routines/${routine.id}`, { method: 'PUT', body: JSON.stringify(routine) }).catch(() => null);
  return !!r?.ok;
}

export function RoutinesScreen() {
  const routines = useApp((s) => s.routines);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<Routine | null>(null);
  const folder = useApp((s) => s.prefs.open.find((o) => o.id === s.prefs.active)?.cwd ?? s.prefs.folders[0] ?? '');

  const startNew = () => {
    const id = `r${Date.now().toString(36)}`;
    setDraft({ id, name: '', prompt: '', cwd: folder, schedule: { kind: 'daily', time: '09:00' }, active: true, created: Date.now(), runs: [] });
    setEditing(id);
  };

  return (
    <section className="screen">
      <div className="screen-head drag">
        <PageHeader
          title="Routines"
          actions={(
            <Button variant="primary" size="sm" className="no-drag" onClick={startNew}>
              <IconPlus size={16} stroke={1.5} aria-hidden /> New routine
            </Button>
          )}
        />
      </div>
      <div className="routines">
        {routines.length === 0 && !draft && (
          <div className="routines-empty">
            <IconCalendarRepeat size={24} stroke={1.5} className="routines-empty-icon" aria-hidden />
            <h2>Let Architect do it on a schedule</h2>
            <p>A routine runs your instructions in a folder every morning, every hour, or when you say. Each run is a conversation you can open.</p>
            <Button variant="primary" size="sm" onClick={startNew}><IconPlus size={16} stroke={1.5} aria-hidden /> New routine</Button>
          </div>
        )}
        {draft && !routines.some((r) => r.id === draft.id) && (
          <RoutineEditor routine={draft} isNew onDone={() => { setDraft(null); setEditing(null); }} />
        )}
        {routines.map((routine) => (
          editing === routine.id
            ? <RoutineEditor key={routine.id} routine={routine} onDone={() => setEditing(null)} />
            : <RoutineCard key={routine.id} routine={routine} onEdit={() => setEditing(routine.id)} />
        ))}
      </div>
    </section>
  );
}

function RoutineCard({ routine, onEdit }: { routine: Routine; onEdit: () => void }) {
  const activate = useApp((s) => s.activate);
  const openConversations = useApp((s) => s.prefs.open);
  const running = routine.runs.some((r) => r.status === 'running');
  const unseen = routine.runs.filter((r) => !r.seen && r.status === 'done').length;
  return (
    <article className={clsx('routine-card', !routine.active && 'is-paused')}>
      <button type="button" className="routine-main" onClick={onEdit}>
        {running && <WorkingMark className="working-xs routine-working" />}
        <span className="routine-text">
          <span className="routine-name">
            {routine.name || 'Untitled routine'}
            {unseen > 0 && <span className="routine-new-dot" role="img" aria-label={`${unseen} new ${unseen === 1 ? 'run' : 'runs'}`} data-tip={`${unseen} new ${unseen === 1 ? 'run' : 'runs'}`} />}
          </span>
          {/* The one meta line. Paused shows in the switch and the dimmed card, so the line keeps the schedule. */}
          <span className="routine-sub" data-tip={routine.active && routine.next ? `Next run ${relativeFuture(routine.next)}` : undefined}>
            {describeSchedule(routine.schedule)} · {folderName(routine.cwd)}
          </span>
        </span>
        <IconChevronRight size={16} stroke={1.5} aria-hidden className="tone-faint" />
      </button>
      <div className="routine-actions">
        <button
          type="button"
          role="switch"
          aria-checked={routine.active}
          aria-label="Run on schedule"
          data-tip={routine.active ? 'On: runs on schedule' : 'Paused'}
          className={clsx('worktree-toggle', routine.active && 'is-on')}
          onClick={() => void save({ id: routine.id, active: !routine.active })}
        >
          <span className="switch-track" aria-hidden><span className="switch-thumb" /></span>
        </button>
        <Button size="sm" disabled={running || !routine.prompt.trim()} onClick={() => void fetch(`/api/routines/${routine.id}/run`, { method: 'POST' })}>
          <IconPlayerPlay size={14} stroke={1.5} aria-hidden /> Run now
        </Button>
      </div>
      {routine.runs.length > 0 && (
        <ul className="routine-runs">
          {routine.runs.slice(0, 5).map((run) => {
            const open = openConversations.some((o) => o.id === run.conv);
            // The icon is the status; the words live in the tooltip and for screen readers.
            const status = run.status === 'running' ? 'Running'
              : run.status === 'done' ? (open ? 'Finished' : 'Finished, conversation closed')
              : run.error ? `Did not start: ${errorText(run.error).replace(/\.$/, '')}` : 'Did not finish';
            return (
              <li key={run.id}>
                <button type="button" className={clsx('routine-run', !run.seen && run.status === 'done' && 'is-new')} disabled={!open} aria-label={`${status}, ${relativeTime(run.at / 1000)} ago`} data-tip={`${status} · ${relativeTime(run.at / 1000)} ago`} onClick={() => { activate(run.conv); useApp.getState().setScreen('chat'); }}>
                  {run.status === 'running' ? <WorkingMark className="working-xs" /> : run.status === 'done' ? <IconCheck size={14} stroke={1.5} aria-hidden /> : <IconX size={14} stroke={1.5} aria-hidden className="tone-err" />}
                  <span>{new Date(run.at).toLocaleString(undefined, { weekday: 'short', hour: 'numeric', minute: '2-digit' })}</span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </article>
  );
}

function RoutineEditor({ routine, isNew = false, onDone }: { routine: Routine; isNew?: boolean; onDone: () => void }) {
  const [value, setValue] = useState(routine);
  const [saving, setSaving] = useState(false);
  const [ask, dialog] = useConfirm();
  const set = (patch: Partial<Routine>) => setValue((v) => ({ ...v, ...patch }));
  const valid = value.name.trim() && value.prompt.trim() && value.cwd;
  return (
    <article className="routine-card is-editing">
      <div className="routine-form">
        <Input className="routine-name-input" aria-label="Name" autoFocus placeholder="Name it, like Morning check" value={value.name} onChange={(e) => set({ name: e.target.value })} />
        <TextArea className="routine-prompt" aria-label="Instructions" rows={4} placeholder="What should Architect do each time? For example: look for failing tests and summarize what changed since yesterday." value={value.prompt} onChange={(e) => set({ prompt: e.target.value })} />
        <div className="routine-row">
          <Button size="sm" onClick={() => void chooseFolder(value.cwd).then((cwd) => cwd && set({ cwd }))}>
            <IconFolder size={14} stroke={1.5} aria-hidden /> {value.cwd ? folderName(value.cwd) : 'Choose a folder'}
          </Button>
          <Segmented
            label="When it runs"
            options={KINDS.map(([kind, label]) => ({ value: kind, label }))}
            value={value.schedule.kind}
            onChange={(kind) => set({ schedule: { ...value.schedule, kind: kind as ScheduleKind } })}
          />
          {value.schedule.kind === 'weekly' && (
            <select className="field routine-day" aria-label="Day" value={value.schedule.day ?? 1} onChange={(e) => set({ schedule: { ...value.schedule, day: Number(e.target.value) } })}>
              {DAYS.map((d, i) => <option key={d} value={i}>{d}</option>)}
            </select>
          )}
          {value.schedule.kind !== 'manual' && (
            <Input type="time" size="sm" className="routine-time num" value={value.schedule.time ?? '09:00'} onChange={(e) => set({ schedule: { ...value.schedule, time: e.target.value } })} />
          )}
        </div>
        <div className="routine-row routine-foot">
          {!isNew && (
            <Button variant="ghost" size="sm" onClick={() => void ask({ title: `Delete ${routine.name || 'this routine'}? Its past runs stay.`, keep: 'Keep it', action: 'Delete' }).then(async (ok) => {
              if (!ok) return;
              await fetch(`/api/routines/${routine.id}`, { method: 'DELETE' });
              onDone();
            })}>
              <IconTrash size={14} stroke={1.5} aria-hidden /> Delete
            </Button>
          )}
          <span className="spacer" />
          <span className="tone-faint small" data-tip="Runs happen while Architect is open; turn on Keep the computer awake in Settings > General for long ones">{describeSchedule(value.schedule)}</span>
          <Button size="sm" onClick={onDone}>Cancel</Button>
          <Button variant="primary" size="sm" pending={saving} disabled={!valid} onClick={() => {
            setSaving(true);
            void save({ ...value, name: value.name.trim() }).then((ok) => {
              setSaving(false);
              if (ok) onDone();
              else useApp.getState().showToast('The routine could not be saved.', 'error');
            });
          }}>
            {isNew ? 'Create routine' : 'Save'}
          </Button>
        </div>
      </div>
      {dialog}
    </article>
  );
}

function relativeFuture(at: number): string {
  const minutes = Math.round((at - Date.now()) / 60_000);
  if (minutes < 1) return 'now';
  if (minutes < 60) return `in ${minutes} min`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `in ${hours} h`;
  return new Date(at).toLocaleDateString(undefined, { weekday: 'short', hour: 'numeric', minute: '2-digit' });
}
