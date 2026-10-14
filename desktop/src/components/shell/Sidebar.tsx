/**
 * Conversations, the way Claude Code and Codex list them: a search box that matches
 * titles and what was said; pinned first; open conversations; then earlier ones
 * grouped by day or by folder in collapsible sections. Every row shows its state
 * (starting or working, needs you, unread) and offers archive on hover and the rest
 * on right-click.
 */
import {
  IconArchive, IconArrowLeft, IconArrowRight, IconBox, IconCalendarRepeat, IconWindowMaximize, IconCalendar, IconDots, IconEye, IconEyeOff, IconFolders, IconChevronRight, IconCopy, IconFilter, IconFolder, IconFolderOpen, IconGitFork, IconHammer,
  IconLayoutSidebarLeftCollapse, IconMail, IconMailOpened, IconMessageCircle, IconPencil, IconPin, IconPinnedOff,
  IconPlus, IconSearch, IconSettings, IconTrash, IconX,
} from '@tabler/icons-react';
import clsx from 'clsx';
import { useEffect, useMemo, useRef, useState } from 'react';
import { native, revealLabel } from '../../lib/native';
import { isMac, shortcutLabel, SHORTCUTS } from '../../lib/shortcuts';
import { goBack, goForward, useNavigation } from '../../lib/navigation';
import { ActivityButton } from './Activity';
import { unseenRuns } from '../../lib/schedule';
import { normPath, sidebarYields, useApp, type Conv, type OpenConv, type SessionRow } from '../../store/app';
import { Badge } from '../ui/Badge';
import { useConfirm } from '../ui/useConfirm';
import { Menu, type MenuItem } from '../ui/Menu';
import { Mark, WorkingMark } from '../ui/Working';
import { folderName } from '../chat/ConversationView';
import { chooseFolder } from './folders';

export function relativeTime(seconds: number): string {
  const delta = Date.now() / 1000 - seconds;
  if (delta < 60) return 'now';
  if (delta < 3600) return `${Math.floor(delta / 60)}m`;
  if (delta < 86_400) return `${Math.floor(delta / 3600)}h`;
  if (delta < 7 * 86_400) return `${Math.floor(delta / 86_400)}d`;
  return new Date(seconds * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function dayGroup(seconds: number): string {
  const day = new Date(seconds * 1000);
  day.setHours(0, 0, 0, 0);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const days = Math.round((today.getTime() - day.getTime()) / 86_400_000);
  if (days <= 0) return 'Today';
  if (days === 1) return 'Yesterday';
  if (days < 7) return 'This week';
  if (days < 31) return 'This month';
  return 'Older';
}

interface SearchHit extends SessionRow { excerpt: string }

export function Sidebar() {
  const [confirm, confirmDialog] = useConfirm();
  const prefs = useApp((s) => s.prefs);
  const yields = useApp(sidebarYields);
  const convs = useApp((s) => s.convs);
  const sessions = useApp((s) => s.sessions);
  const screen = useApp((s) => s.screen);
  const openConversation = useApp((s) => s.openConversation);
  const setScreen = useApp((s) => s.setScreen);
  const setPrefs = useApp((s) => s.setPrefs);
  const refreshSessions = useApp((s) => s.refreshSessions);
  const request = useApp((s) => s.request);
  const [newMenu, setNewMenu] = useState(false);
  const [filterMenu, setFilterMenu] = useState(false);
  const [query, setQuery] = useState('');
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const searchBox = useRef<HTMLInputElement>(null);

  useEffect(() => {
    void refreshSessions();
  }, [refreshSessions]);

  // Search: titles here at once, conversation text through the bridge after a pause.
  useEffect(() => {
    const q = query.trim();
    if (!q) {
      setHits(null);
      return;
    }
    const host = Object.values(useApp.getState().convs).find((c) => c.status === 'ready');
    if (!host) return;
    const timer = setTimeout(() => {
      void request<{ results: SearchHit[] }>(host.id, 'search_sessions', { query: q })
        .then((r) => setHits(r.results))
        .catch(() => setHits([]));
    }, 220);
    return () => clearTimeout(timer);
  }, [query, request]);

  useEffect(() => {
    const onFocus = () => searchBox.current?.focus();
    window.addEventListener('architect:search-sidebar', onFocus);
    return () => window.removeEventListener('architect:search-sidebar', onFocus);
  }, []);

  const routines = useApp((st) => st.routines);
  const archived = useMemo(() => new Set(prefs.archived), [prefs.archived]);
  const pinned = useMemo(() => new Set(prefs.pinned), [prefs.pinned]);
  const openSessions = useMemo(() => new Set(prefs.open.map((o) => o.session).filter(Boolean)), [prefs.open]);
  const pinKey = (open: OpenConv) => open.session ?? open.id;

  // Project filter and removed projects (compared as paths, not strings).
  const hidden = useMemo(() => new Set(prefs.hiddenProjects.map(normPath)), [prefs.hiddenProjects]);
  const onlyProject = prefs.sidebarProject ? normPath(prefs.sidebarProject) : null;
  const shownFolder = (cwd: string) => {
    const key = normPath(cwd || '');
    return onlyProject ? key === onlyProject : !hidden.has(key);
  };
  const showArchived = prefs.sidebarStatus === 'archived';
  const projects = useMemo(() => {
    const seen = new Map<string, string>();
    for (const row of sessions) if (row.cwd && !seen.has(normPath(row.cwd))) seen.set(normPath(row.cwd), row.cwd);
    for (const o of prefs.open) if (o.cwd && !seen.has(normPath(o.cwd))) seen.set(normPath(o.cwd), o.cwd);
    return [...seen.values()];
  }, [sessions, prefs.open]);
  const filtered = !!onlyProject || showArchived;

  const openPinned = prefs.open.filter((o) => pinned.has(pinKey(o)) && shownFolder(o.cwd));
  const openRest = prefs.open.filter((o) => !pinned.has(pinKey(o)) && shownFolder(o.cwd));
  const savedPinned = sessions.filter((s) => pinned.has(s.id) && !openSessions.has(s.id) && !archived.has(s.id) && shownFolder(s.cwd));
  const saved = sessions.filter((s) => !openSessions.has(s.id) && archived.has(s.id) === showArchived && (showArchived || !pinned.has(s.id)) && shownFolder(s.cwd)).slice(0, 120);

  const groups = useMemo(() => {
    const out: Array<{ label: string; rows: SessionRow[] }> = [];
    for (const row of saved) {
      const label = prefs.sidebarGroup === 'folder' ? (row.cwd ? folderName(row.cwd) : 'No folder') : dayGroup(row.time);
      const group = out.find((g) => g.label === label);
      if (group) group.rows.push(row);
      else out.push({ label, rows: [row] });
    }
    return out;
  }, [saved, prefs.sidebarGroup]);

  const archiveProject = (cwd: string) => {
    const rows = sessions.filter((r) => normPath(r.cwd) === normPath(cwd) && !archived.has(r.id));
    void confirm({ title: `Archive ${rows.length} ${rows.length === 1 ? 'conversation' : 'conversations'} in ${folderName(cwd)}?`, detail: 'They come back from Settings, Archived.', keep: 'Cancel', action: 'Archive', tone: 'primary' })
      .then((ok) => {
        if (!ok) return;
        const keys = rows.map((r) => r.id);
        setPrefs({ archived: [...keys, ...prefs.archived.filter((k) => !keys.includes(k))] });
        useApp.getState().pushUndo(`Archived ${keys.length} in ${folderName(cwd)}.`, () => setPrefs({ archived: useApp.getState().prefs.archived.filter((k) => !keys.includes(k)) }), () => setPrefs({ archived: [...keys, ...useApp.getState().prefs.archived] }));
      });
  };
  const removeProject = (cwd: string) => {
    setPrefs({ hiddenProjects: [...prefs.hiddenProjects, cwd], sidebarProject: undefined });
    useApp.getState().pushUndo(`Removed ${folderName(cwd)} from the sidebar.`, () => setPrefs({ hiddenProjects: useApp.getState().prefs.hiddenProjects.filter((x) => x !== cwd) }), () => setPrefs({ hiddenProjects: [...useApp.getState().prefs.hiddenProjects, cwd] }));
  };

  const activeConv = prefs.active ? prefs.open.find((o) => o.id === prefs.active) : undefined;
  const currentFolder = activeConv?.cwd ?? prefs.folders[0] ?? '';
  const toggleSection = (label: string) => setPrefs({
    collapsed: prefs.collapsed.includes(label) ? prefs.collapsed.filter((c) => c !== label) : [...prefs.collapsed, label],
  });

  if (!prefs.sidebar || yields) return null;
  const newRuns = unseenRuns(routines);

  const q = query.trim().toLowerCase();
  const openMatches = q ? prefs.open.filter((o) => (convs[o.id]?.state.title || o.title || '').toLowerCase().includes(q)) : [];

  const filterControl = (
    <div className="bar-anchor">
      <button type="button" className={clsx('icon-button tiny', filtered && 'is-on')} aria-label="Filter and group" data-tip="Filter and group" data-tip-side="down" onClick={() => setFilterMenu(!filterMenu)}>
        <IconFilter size={14} stroke={1.5} />
      </button>
      {filterMenu && (
        <Menu
          side="down"
          align="end"
          width={240}
          onClose={() => setFilterMenu(false)}
          items={[
            { id: 'g:time', label: 'By date', icon: IconCalendar, selected: prefs.sidebarGroup === 'time', section: 'Group' },
            { id: 'g:folder', label: 'By project', icon: IconFolder, selected: prefs.sidebarGroup === 'folder', section: 'Group' },
            { id: 's:all', label: 'Conversations', icon: IconMessageCircle, selected: !showArchived, section: 'Show' },
            { id: 's:archived', label: 'Archived', icon: IconArchive, selected: showArchived, section: 'Show' },
            { id: 'p:', label: 'Every project', icon: IconFolders, selected: !onlyProject, section: 'Project' },
            ...projects.filter((x) => !hidden.has(normPath(x))).slice(0, 8).map((x) => ({ id: `p:${x}`, label: folderName(x), detail: x, icon: IconFolder, selected: onlyProject === normPath(x), section: 'Project' })),
            ...(prefs.hiddenProjects.length ? [{ id: 'unhide', label: `Show ${prefs.hiddenProjects.length} removed ${prefs.hiddenProjects.length === 1 ? 'project' : 'projects'}`, icon: IconEye, section: 'Project' }] : []),
          ]}
          onPick={(id) => {
            setFilterMenu(false);
            if (id.startsWith('g:')) setPrefs({ sidebarGroup: id.slice(2) as 'time' | 'folder' });
            else if (id.startsWith('s:')) setPrefs({ sidebarStatus: id.slice(2) as 'all' | 'archived' });
            else if (id.startsWith('p:')) setPrefs({ sidebarProject: id.slice(2) || undefined });
            else if (id === 'unhide') setPrefs({ hiddenProjects: [] });
          }}
        />
      )}
    </div>
  );

  return (
    <nav className="sidebar" aria-label="Conversations">
      <div className="sidebar-head drag">
        <div className="brand">
          <Mark className="brand-mark text-accent" />
          <span className="brand-word">Architect</span>
        </div>
        <span className="head-nav no-drag">
          <ActivityButton quiet />
          <NavButtons />
          <button type="button" className="icon-button" aria-label="Hide the sidebar" data-tip={`Hide sidebar  ${shortcutLabel(SHORTCUTS.sidebar)}`} data-tip-side="down" onClick={() => setPrefs({ sidebar: false })}>
            <IconLayoutSidebarLeftCollapse size={17} stroke={1.5} />
          </button>
        </span>
      </div>

      <div className="sidebar-actions">
        {/* Building an agent is the app's one flow: it leads the rail. */}
        <div className="new-split bar-anchor">
          <button type="button" className="new-button" onClick={() => openConversation({ cwd: currentFolder, kind: 'forge' })} data-tip={shortcutLabel(SHORTCUTS.newForge)} data-tip-side="down">
            <IconHammer size={16} stroke={1.5} aria-hidden />
            <span>New agent</span>
          </button>
          <button type="button" className="new-more" aria-label="New agent in another folder" data-tip="Another folder" data-tip-side="down" onClick={() => setNewMenu(!newMenu)}>
            <IconFolderOpen size={16} stroke={1.5} />
          </button>
          {newMenu && (
            <Menu
              side="down"
              align="end"
              width={260}
              onClose={() => setNewMenu(false)}
              items={[
                ...prefs.folders.slice(0, 6).map((f) => ({ id: `f:${f}`, label: folderName(f), detail: f, icon: IconFolder, section: 'Recent' })),
                { id: 'choose', label: 'Choose a folder', icon: IconFolderOpen },
              ]}
              onPick={(id) => {
                setNewMenu(false);
                if (id === 'choose') void chooseFolder(currentFolder).then((cwd) => cwd && openConversation({ cwd, kind: 'forge' }));
                else openConversation({ cwd: id.slice(2), kind: 'forge' });
              }}
            />
          )}
        </div>
        <button type="button" className="nav-row" onClick={() => openConversation({ cwd: currentFolder })}>
          <IconPlus size={16} stroke={1.5} aria-hidden />
          <span>New conversation</span>
          <kbd className="nav-kbd">{shortcutLabel(SHORTCUTS.newChat)}</kbd>
        </button>
        <label className="sidebar-search">
          <IconSearch size={15} stroke={1.5} aria-hidden />
          <input
            ref={searchBox}
            placeholder="Search conversations"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === 'Escape' && setQuery('')}
          />
          {query && (
            <button type="button" className="search-clear" aria-label="Clear search" onClick={() => setQuery('')}>
              <IconX size={13} />
            </button>
          )}
        </label>
      </div>

      <div className="sidebar-scroll">
        {q ? (
          <ul className="conv-list">
            {openMatches.map((open, index) => (
              <OpenRow key={open.id} index={index} open={open} conv={convs[open.id]} active={screen === 'chat' && prefs.active === open.id} pinned={pinned.has(pinKey(open))} />
            ))}
            {(hits ?? []).filter((h) => !openSessions.has(h.id)).map((hit) => (
              <SavedRow key={hit.id} session={hit} excerpt={hit.excerpt} query={q} archived={archived.has(hit.id)} pinned={pinned.has(hit.id)} />
            ))}
            {hits !== null && hits.length === 0 && openMatches.length === 0 && <li className="sidebar-empty">Nothing matches.</li>}
          </ul>
        ) : (
          <>
            {(openPinned.length > 0 || savedPinned.length > 0) && (
              <Section label="Pinned" icon={IconPin} collapsed={prefs.collapsed.includes('Pinned')} onToggle={() => toggleSection('Pinned')}>
                {openPinned.map((open) => (
                  <OpenRow key={open.id} index={prefs.open.indexOf(open)} open={open} conv={convs[open.id]} active={screen === 'chat' && prefs.active === open.id} pinned />
                ))}
                {savedPinned.map((session) => <SavedRow key={session.id} session={session} pinned />)}
              </Section>
            )}
            {openRest.length > 0 && (
              <ul className="conv-list conv-open">
                {openRest.map((open) => (
                  <OpenRow key={open.id} index={prefs.open.indexOf(open)} open={open} conv={convs[open.id]} active={screen === 'chat' && prefs.active === open.id} pinned={false} />
                ))}
              </ul>
            )}
            {(filtered || (groups.length > 0 && prefs.sidebarGroup === 'folder')) && (
              <div className="sidebar-group-head">
                <span>{showArchived ? 'Archived' : 'Earlier'}</span>
                {onlyProject && (
                  <button type="button" className="filter-chip" onClick={() => setPrefs({ sidebarProject: undefined })} aria-label="Show every project">
                    <IconFolder size={12} aria-hidden /> {folderName(prefs.sidebarProject ?? '')} <IconX size={11} aria-hidden />
                  </button>
                )}
                {filterControl}
              </div>
            )}
            {groups.map((group, gi) => (
              <Section
                key={group.label}
                label={group.label}
                icon={prefs.sidebarGroup === 'folder' ? IconFolder : undefined}
                collapsed={prefs.collapsed.includes(group.label)}
                onToggle={() => toggleSection(group.label)}
                actions={gi === 0 && !filtered && prefs.sidebarGroup !== 'folder' ? filterControl : prefs.sidebarGroup === 'folder' && group.rows[0]?.cwd ? (
                  <ProjectMenu
                    cwd={group.rows[0].cwd}
                    onNew={() => openConversation({ cwd: group.rows[0].cwd })}
                    onOnly={() => setPrefs({ sidebarProject: group.rows[0].cwd })}
                    onArchive={() => archiveProject(group.rows[0].cwd)}
                    onRemove={() => removeProject(group.rows[0].cwd)}
                  />
                ) : undefined}
              >
                {group.rows.map((session) => <SavedRow key={session.id} session={session} pinned={false} />)}
              </Section>
            ))}
          </>
        )}
      </div>

      <div className="sidebar-bottom">
        <button type="button" className={clsx('nav-row', screen === 'routines' && 'is-active')} onClick={() => setScreen('routines')}>
          <IconCalendarRepeat size={16} stroke={1.5} aria-hidden />
          <span>Routines</span>
          {newRuns > 0 && <Badge className="nav-badge" aria-label={`${newRuns} new`}>{newRuns}</Badge>}
        </button>
        <button type="button" className={clsx('nav-row', screen === 'agents' && 'is-active')} onClick={() => setScreen('agents')}>
          <IconBox size={16} stroke={1.5} aria-hidden />
          <span>Your agents</span>
        </button>
        <button type="button" className={clsx('nav-row', screen === 'settings' && 'is-active')} onClick={() => setScreen('settings')}>
          <IconSettings size={16} stroke={1.5} aria-hidden />
          <span>Settings</span>
          <kbd className="nav-kbd">{shortcutLabel(SHORTCUTS.settings)}</kbd>
        </button>
      </div>
      {confirmDialog}
    </nav>
  );
}

function Section({ label, icon: Icon, collapsed, onToggle, actions, children }: { label: string; icon?: typeof IconPin; collapsed: boolean; onToggle: () => void; actions?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="conv-group">
      <div className="sidebar-heading-row">
        <button type="button" className="sidebar-heading" onClick={onToggle} aria-expanded={!collapsed}>
          <IconChevronRight size={12} className={clsx('work-chevron', !collapsed && 'is-open')} aria-hidden />
          {Icon && <Icon size={12} stroke={1.5} aria-hidden />}
          <span>{label}</span>
        </button>
        {actions}
      </div>
      {!collapsed && <ul className="conv-list">{children}</ul>}
    </section>
  );
}

function OpenRow({ index, open, conv, active, pinned }: { index: number; open: OpenConv; conv?: Conv; active: boolean; pinned: boolean }) {
  const send = useApp((s) => s.send);
  const activate = useApp((s) => s.activate);
  const closeConversation = useApp((s) => s.closeConversation);
  const archiveConversation = useApp((s) => s.archiveConversation);
  const togglePin = useApp((s) => s.togglePin);
  const markUnread = useApp((s) => s.markUnread);
  const [menu, setMenu] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const title = conv?.state.title || open.title || (open.kind === 'forge' ? 'New agent' : 'New conversation');
  const asking = conv?.timeline.items.some((i) => i.kind === 'ask' && !i.closed);
  const busy = conv?.timeline.running || conv?.state.busy;
  const starting = !conv || conv.status === 'starting' || conv.resuming;
  const KindIcon = open.kind === 'forge' ? IconHammer : IconMessageCircle;

  useEffect(() => {
    const onRename = (e: Event) => {
      if ((e as CustomEvent).detail === open.id && conv?.status === 'ready') setRenaming(true);
    };
    window.addEventListener('architect:rename', onRename);
    return () => window.removeEventListener('architect:rename', onRename);
  }, [open.id, conv?.status]);

  let lead: React.ReactNode = <KindIcon size={15} stroke={1.5} aria-hidden />;
  if (busy || (starting && !asking)) lead = <WorkingMark className="working-xs text-accent" label={starting ? 'Starting' : 'Working'} />;

  let status: React.ReactNode = null;
  if (asking) status = <span className="conv-needs" role="img" aria-label="Needs you" data-tip="Needs you" data-tip-side="right" />;
  else if (conv?.status === 'failed') status = <span className="conv-status tone-err">Failed</span>;
  else if (conv?.status === 'stopped') status = <span className="conv-status tone-faint">Stopped</span>;
  else if (conv?.status === 'setup') status = <span className="conv-status is-ask">Sign in</span>;
  else if (conv?.status === 'trust') status = <span className="conv-status is-ask">Trust?</span>;
  else if (index < 9) status = <kbd className="conv-kbd">{isMac() ? '⌘' : 'Ctrl+'}{index + 1}</kbd>;

  const key = open.session ?? open.id;
  const items: MenuItem[] = [
    { id: 'rename', label: 'Rename', icon: IconPencil, hint: shortcutLabel(SHORTCUTS.rename), disabled: conv?.status !== 'ready' },
    { id: 'pin', label: pinned ? 'Unpin' : 'Pin', icon: pinned ? IconPinnedOff : IconPin, hint: shortcutLabel(SHORTCUTS.pin) },
    { id: 'unread', label: conv?.unread ? 'Mark as read' : 'Mark as unread', icon: conv?.unread ? IconMailOpened : IconMail, hint: shortcutLabel(SHORTCUTS.unread) },
    { id: 'fork', label: 'Fork', icon: IconGitFork, disabled: !conv?.state.session_id || !conv.state.title },
    { id: 'copy-id', label: 'Copy conversation ID', icon: IconCopy, disabled: !conv?.state.session_id },
    ...(native()?.openWindow ? [{ id: 'popout', label: 'Open in a new window', icon: IconWindowMaximize }] : []),
    { id: 'copy-cwd', label: 'Copy folder path', icon: IconCopy },
    { id: 'reveal', label: `Show ${folderName(open.cwd)}`, icon: IconFolder, disabled: !native() },
    { id: 'archive', label: 'Archive', icon: IconArchive, hint: shortcutLabel(SHORTCUTS.archive) },
    { id: 'close', label: 'Close', icon: IconX, hint: shortcutLabel(SHORTCUTS.closeConv) },
  ];

  return (
    <li className="conv-item" onContextMenu={(e) => { e.preventDefault(); setMenu(true); }}>
      {renaming ? (
        <form
          className="conv-rename"
          onSubmit={(e) => {
            e.preventDefault();
            const value = new FormData(e.currentTarget).get('t');
            setRenaming(false);
            if (typeof value === 'string' && value.trim()) void send(open.id, `/title ${value.trim()}`, true);
          }}
        >
          <input name="t" className="field" defaultValue={title} autoFocus onBlur={() => setRenaming(false)} onKeyDown={(e) => e.key === 'Escape' && setRenaming(false)} />
        </form>
      ) : (
        <button
          type="button"
          className={clsx('conv-row', active && 'is-active', conv?.unread && 'is-unread')}
          onClick={(e) => (e.ctrlKey || e.metaKey ? useApp.getState().setSplit(open.id) : activate(open.id))}
          title={active ? undefined : `${isMac() ? '⌘' : 'Ctrl'}-click to open beside this one`}
          onDoubleClick={() => conv?.status === 'ready' && setRenaming(true)}
          aria-current={active ? 'page' : undefined}
        >
          <span className="conv-lead">{lead}</span>
          <span className="conv-row-title">{title}</span>
          <span className="conv-row-side">{status}</span>
        </button>
      )}
      {!renaming && (
        <span className="row-actions">
          <button type="button" className="row-action" onClick={() => archiveConversation(open.id)} aria-label="Archive" data-tip="Archive" data-tip-side="down">
            <IconArchive size={14} stroke={1.5} />
          </button>
        </span>
      )}
      {menu && (
        <div className="conv-menu">
          <Menu
            side="down"
            align="end"
            width={250}
            onClose={() => setMenu(false)}
            items={items}
            onPick={(id) => {
              setMenu(false);
              if (id === 'rename') setRenaming(true);
              else if (id === 'fork' && conv?.state.session_id) void useApp.getState().forkSession(conv.state.session_id);
              else if (id === 'pin') togglePin(key);
              else if (id === 'unread') markUnread(open.id, !conv?.unread);
              else if (id === 'copy-id') void navigator.clipboard.writeText(conv?.state.session_id ?? '');
              else if (id === 'copy-cwd') void navigator.clipboard.writeText(open.cwd);
              else if (id === 'popout') void native()?.openWindow?.(`chat=${encodeURIComponent(open.id)}`, { width: 640, height: 820 });
              else if (id === 'reveal') void native()?.reveal(open.cwd);
              else if (id === 'archive') archiveConversation(open.id);
              else closeConversation(open.id);
            }}
          />
        </div>
      )}
    </li>
  );
}

function SavedRow({ session, pinned, excerpt, query, archived }: { session: SessionRow; pinned: boolean; excerpt?: string; query?: string; archived?: boolean }) {
  const openConversation = useApp((s) => s.openConversation);
  const archiveSession = useApp((s) => s.archiveSession);
  const unarchive = useApp((s) => s.unarchive);
  const togglePin = useApp((s) => s.togglePin);
  const deleteSession = useApp((s) => s.deleteSession);
  const prefs = useApp((s) => s.prefs);
  const [menu, setMenu] = useState(false);
  const [confirm, confirmDialog] = useConfirm();
  const open = () => openConversation({ cwd: session.cwd || prefs.folders[0] || '', resume: session.id, title: session.title });
  return (
    <li className="conv-item" onContextMenu={(e) => { e.preventDefault(); setMenu(true); }}>
      <button
        type="button"
        className={clsx('conv-row is-saved', excerpt && 'has-excerpt')}
        onClick={(e) => {
          if (e.ctrlKey || e.metaKey) {
            const id = openConversation({ cwd: session.cwd || prefs.folders[0] || '', resume: session.id, title: session.title, activate: false });
            useApp.getState().setSplit(id);
          } else open();
        }}
      >
        <span className="conv-lead">{pinned ? <IconPin size={14} stroke={1.5} aria-hidden /> : archived ? <IconArchive size={14} stroke={1.5} aria-hidden /> : null}</span>
        <span className="conv-row-text">
          <span className="conv-row-title">{session.title}</span>
          {excerpt && <span className="conv-row-excerpt"><Highlight text={excerpt} query={query ?? ''} /></span>}
        </span>
        <span className="conv-row-time num">{relativeTime(session.time)}</span>
      </button>
      <span className="row-actions">
        <button type="button" className="row-action" onClick={() => (archived ? unarchive(session.id) : archiveSession(session.id))} aria-label={archived ? 'Unarchive' : 'Archive'} data-tip={archived ? 'Unarchive' : 'Archive'} data-tip-side="down">
          <IconArchive size={14} stroke={1.5} />
        </button>
      </span>
      {menu && (
        <div className="conv-menu">
          <Menu
            side="down"
            align="end"
            width={230}
            onClose={() => setMenu(false)}
            items={[
              { id: 'open', label: 'Open', icon: IconMessageCircle },
              { id: 'pin', label: pinned ? 'Unpin' : 'Pin', icon: pinned ? IconPinnedOff : IconPin },
              { id: 'fork', label: 'Fork', icon: IconGitFork },
              { id: 'copy-id', label: 'Copy conversation ID', icon: IconCopy },
              { id: 'archive', label: archived ? 'Unarchive' : 'Archive', icon: IconArchive },
              { id: 'delete', label: 'Delete', icon: IconTrash },
            ]}
            onPick={(id) => {
              setMenu(false);
              if (id === 'open') open();
              else if (id === 'fork') void useApp.getState().forkSession(session.id);
              else if (id === 'pin') togglePin(session.id);
              else if (id === 'copy-id') void navigator.clipboard.writeText(session.id);
              else if (id === 'archive') (archived ? unarchive(session.id) : archiveSession(session.id));
              else {
                void confirm({ title: `Delete "${session.title}" for good?`, keep: 'Keep it', action: 'Delete' })
                  .then((ok) => ok && deleteSession(session.id));
              }
            }}
          />
        </div>
      )}
      {confirmDialog}
    </li>
  );
}

function Highlight({ text, query }: { text: string; query: string }) {
  const at = query ? text.toLowerCase().indexOf(query) : -1;
  if (at < 0) return <>{text}</>;
  return <>{text.slice(0, at)}<mark>{text.slice(at, at + query.length)}</mark>{text.slice(at + query.length)}</>;
}

/** Back and forward through where you have been. */
export function NavButtons() {
  const canBack = useNavigation((n) => n.back.length > 0);
  const canForward = useNavigation((n) => n.forward.length > 0);
  return (
    <>
      <button type="button" className="icon-button" aria-label="Back" data-tip={`Back  ${shortcutLabel(SHORTCUTS.back)}`} data-tip-side="down" disabled={!canBack} onClick={goBack}>
        <IconArrowLeft size={16} stroke={1.5} />
      </button>
      <button type="button" className="icon-button" aria-label="Forward" data-tip={`Forward  ${shortcutLabel(SHORTCUTS.forward)}`} data-tip-side="down" disabled={!canForward} onClick={goForward}>
        <IconArrowRight size={16} stroke={1.5} />
      </button>
    </>
  );
}

/** A project's own menu (Codex's three dots on a project). */
function ProjectMenu({ cwd, onNew, onOnly, onArchive, onRemove }: { cwd: string; onNew: () => void; onOnly: () => void; onArchive: () => void; onRemove: () => void }) {
  const [open, setOpen] = useState(false);
  const n = native();
  return (
    <span className="bar-anchor project-menu">
      <button type="button" className="row-action" aria-label={`${folderName(cwd)} options`} onClick={() => setOpen(!open)}>
        <IconDots size={14} stroke={1.5} />
      </button>
      {open && (
        <Menu
          side="down"
          align="end"
          width={240}
          onClose={() => setOpen(false)}
          items={[
            { id: 'new', label: 'New conversation here', icon: IconPlus },
            { id: 'only', label: 'Show only this project', icon: IconFilter },
            { id: 'copy', label: 'Copy path', icon: IconCopy },
            ...(n ? [{ id: 'reveal', label: revealLabel(), icon: IconFolderOpen }] : []),
            { id: 'archive', label: 'Archive its conversations', icon: IconArchive, section: ' ' },
            { id: 'remove', label: 'Remove from sidebar', icon: IconEyeOff, section: ' ' },
          ]}
          onPick={(id) => {
            setOpen(false);
            if (id === 'new') onNew();
            else if (id === 'only') onOnly();
            else if (id === 'copy') void navigator.clipboard.writeText(cwd);
            else if (id === 'reveal') void n?.reveal(cwd);
            else if (id === 'archive') onArchive();
            else onRemove();
          }}
        />
      )}
    </span>
  );
}
