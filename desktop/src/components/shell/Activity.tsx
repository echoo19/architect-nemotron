/**
 * Activity (Codex's bell): the conversations that are waiting for you, working,
 * or finished while you were elsewhere, in one list. Ctrl/Cmd+Alt+U opens it.
 */
import { IconBell, IconCheck } from '@tabler/icons-react';
import clsx from 'clsx';
import { useEffect, useMemo, useRef, useState } from 'react';
import { shortcutLabel, SHORTCUTS } from '../../lib/shortcuts';
import { useApp, type Conv } from '../../store/app';
import { folderName } from '../chat/ConversationView';
import { WorkingMark } from '../ui/Working';
import { relativeTime } from './Sidebar';

type Kind = 'waiting' | 'working' | 'unread';
const FILTERS: Array<[Kind | 'all', string]> = [['all', 'All'], ['waiting', 'Needs you'], ['working', 'Working'], ['unread', 'Unread']];

function kindOf(conv: Conv): Kind | null {
  if (conv.timeline.items.some((i) => i.kind === 'ask' && !i.closed)) return 'waiting';
  if (conv.timeline.running || conv.state.busy) return 'working';
  if (conv.unread) return 'unread';
  return null;
}

const STATUS = { waiting: 'Needs your answer', working: 'Working', unread: 'Finished', done: 'Finished' } as Record<string, string>;

/** `quiet`: the bell is not drawn until it is opened (by its shortcut); the rail's head stays to its one flow. */
export function ActivityButton({ quiet = false }: { quiet?: boolean }) {
  const [open, setOpen] = useState(false);
  const convs = useApp((s) => s.convs);
  const prefs = useApp((s) => s.prefs);
  const rows = useMemo(() => prefs.open
    .map((o) => ({ open: o, conv: convs[o.id], kind: convs[o.id] ? kindOf(convs[o.id]!) : null }))
    .filter((r): r is { open: typeof r.open; conv: Conv; kind: Kind } => !!r.kind && !!r.conv)
    .sort((a, b) => ['waiting', 'working', 'unread'].indexOf(a.kind) - ['waiting', 'working', 'unread'].indexOf(b.kind) || (b.conv.activity ?? 0) - (a.conv.activity ?? 0)), [convs, prefs.open]);
  const needs = rows.filter((r) => r.kind !== 'working').length;

  useEffect(() => {
    const toggle = () => setOpen((o) => !o);
    window.addEventListener('architect:activity', toggle);
    return () => window.removeEventListener('architect:activity', toggle);
  }, []);

  return (
    <span className="bar-anchor">
      <button
        type="button"
        className={clsx('icon-button activity-button', open && 'is-on')}
        hidden={quiet && !open}
        aria-label={needs ? `Activity, ${needs} for you` : 'Activity'}
        aria-expanded={open}
        data-tip={open ? undefined : `Activity  ${shortcutLabel(SHORTCUTS.activity)}`}
        data-tip-side="down"
        onClick={() => setOpen(!open)}
      >
        <IconBell size={16} stroke={1.7} />
        {needs > 0 && <span className="activity-count" aria-hidden />}
      </button>
      {open && <ActivityPanel rows={rows} onClose={() => setOpen(false)} />}
    </span>
  );
}

function ActivityPanel({ rows, onClose }: { rows: Array<{ open: { id: string; cwd: string; title?: string }; conv: Conv; kind: Kind }>; onClose: () => void }) {
  const [filter, setFilter] = useState<Kind | 'all'>('all');
  const activate = useApp((s) => s.activate);
  const markUnread = useApp((s) => s.markUnread);
  const root = useRef<HTMLDivElement>(null);
  const shown = rows.filter((r) => filter === 'all' || r.kind === filter);

  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (root.current && !root.current.contains(e.target as Node) && !(e.target as Element).closest?.('.activity-button')) onClose();
    };
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('mousedown', onDown);
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('mousedown', onDown);
      window.removeEventListener('keydown', onKey);
    };
  }, [onClose]);

  return (
    <div ref={root} className="activity-panel" role="dialog" aria-label="Activity">
      <div className="activity-head">
        <div className="seg activity-filters">
          {FILTERS.map(([id, label]) => (
            <button key={id} type="button" className={clsx('seg-item', filter === id && 'seg-on')} onClick={() => setFilter(id)}>{label}</button>
          ))}
        </div>
        <button
          type="button"
          className="icon-button"
          aria-label="Mark all as read"
          data-tip="Mark all as read"
          data-tip-side="down"
          disabled={!rows.some((r) => r.kind === 'unread')}
          onClick={() => rows.forEach((r) => r.conv.unread && markUnread(r.conv.id, false))}
        >
          <IconCheck size={15} />
        </button>
      </div>
      <div className="activity-list">
        {shown.length === 0 ? (
          <div className="activity-empty">
            <IconBell size={22} stroke={1.5} aria-hidden />
            <span>{filter === 'all' ? 'Nothing needs you' : 'Nothing here'}</span>
          </div>
        ) : shown.map(({ open, conv, kind }) => (
          <button
            key={open.id}
            type="button"
            className="activity-row"
            onClick={() => {
              activate(open.id);
              onClose();
            }}
          >
            <span className={clsx('activity-lead', `is-${kind}`)} role="img" aria-label={STATUS[kind]} data-tip={STATUS[kind]}>
              {kind === 'working' ? <WorkingMark className="working-xs" /> : <span className="activity-dot" />}
            </span>
            <span className="activity-text">
              <span className="activity-title">{conv.state.title || open.title || 'New conversation'}</span>
              {/* The dot or working mark is the status; the line is just where. */}
              <span className="activity-sub">{folderName(open.cwd)}</span>
            </span>
            {conv.activity ? <span className="activity-time num">{relativeTime(conv.activity / 1000)}</span> : null}
          </button>
        ))}
      </div>
    </div>
  );
}
