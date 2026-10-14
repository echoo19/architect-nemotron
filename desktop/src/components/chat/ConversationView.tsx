/**
 * One conversation: its header, the timeline and the composer, plus the state
 * the conversation is in (starting, resuming, needs sign-in, failed, stopped).
 * A new conversation opens on a centered start screen with the composer in the
 * middle; the first message moves it to the bottom, as Claude Code does.
 *
 * In a build (`transcript`), it is the rail beside the agent card: the timeline only,
 * under a small head with a close button. The card's composer is the one input.
 */
import {
  IconAlertTriangle, IconCircleX, IconCopy, IconDots, IconFileExport, IconFiles, IconFolder, IconGitCompare, IconGitFork, IconHammer, IconTerminal2,
  IconBinaryTree2, IconPin, IconWindowMaximize, IconFolderOpen, IconFolderX, IconLayoutSidebarRight, IconMessageQuestion, IconWorld, IconInfoCircle, IconMessageOff, IconPencil, IconPlus, IconRefresh, IconSearch, IconX,
} from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { CHAT_STARTERS } from '../../lib/icons';
import { native } from '../../lib/native';
import { useCountUp } from '../../lib/useCountUp';
import type { Ask } from '../../lib/timeline';
import { shortcutLabel, SHORTCUTS } from '../../lib/shortcuts';
import { errorText, ROLE, useApp, type Conv, type DockTab } from '../../store/app';
import { Onboarding } from '../onboarding/Onboarding';
import { Button, IconButton } from '../ui/Button';
import { Menu } from '../ui/Menu';
import { Mark, WorkingRegion } from '../ui/Working';
import { Composer } from './Composer';
import { BranchChip, CommitButton } from '../git/GitControls';
import { Dock } from '../panes/Dock';
import { TerminalPane } from '../panes/TerminalPane';
import { OpenIn } from '../panes/OpenIn';
import { Timeline } from './Timeline';
import { SideChat } from './SideChat';
import { TrustFolder } from './TrustFolder';
import { chooseFolder } from '../shell/folders';

export function folderName(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts.at(-1) ?? path;
}

/**
 * `heldAsk`: a question another control on screen answers (the build card's Build, its
 * question, the export dialog). `transcript`: the build's rail; `onClose` hides it.
 */
export function ConversationView({ conv, compact = false, heldAsk, transcript = false, onClose }: { conv: Conv; compact?: boolean; heldAsk?: number; transcript?: boolean; onClose?: () => void }) {
  const terminalPlace = useApp((st) => st.prefs.terminalPlace);
  const fullPanes = useApp((st) => !!st.dock.tab && !!st.dock.full);
  const answer = useApp((s) => s.answer);
  const send = useApp((s) => s.send);
  const open = useApp((s) => s.prefs.open.find((o) => o.id === conv.id));

  const title = conv.state.title || open?.title;
  useEffect(() => {
    if (!compact && !transcript) document.title = title ? `${title} - Architect` : 'Architect';
  }, [title, compact, transcript]);

  if (conv.status === 'setup') return <Onboarding conv={conv} />;
  if (conv.status === 'trust' && open) return <TrustFolder id={conv.id} cwd={open.worktree?.root ?? open.cwd} />;

  const onAnswer = (ask: Ask, value: string | null) => void answer(conv.id, ask.id, value);
  const onSend = (text: string) => void send(conv.id, text);
  const loadingHistory = conv.resuming || (conv.status === 'starting' && !!open?.session && conv.timeline.items.length === 0);
  const fresh = conv.timeline.items.length === 0 && !conv.timeline.running && !loadingHistory && conv.status !== 'failed';

  if (transcript) {
    return (
      <section className="conversation transcript">
        <header className="transcript-head">
          <h2 className="transcript-title">Transcript</h2>
          {onClose && <IconButton icon={IconX} label="Close the transcript" onClick={onClose} />}
        </header>
        {loadingHistory ? (
          <HistorySkeleton />
        ) : fresh ? (
          <p className="transcript-empty">Nothing yet</p>
        ) : (
          <Timeline timeline={conv.timeline} onAnswer={onAnswer} onSend={onSend} links={conv.links} compact heldAsk={heldAsk} />
        )}
        {conv.side?.open && <SideChat conv={conv} />}
      </section>
    );
  }

  return (
    <div className="conversation-row">
      <section className={`conversation${!compact && fullPanes ? ' is-behind-panes' : ''}`}>
        <ConversationHeader conv={conv} compact={compact} />
        {conv.status === 'failed' ? (
          <Failed conv={conv} />
        ) : conv.setup && conv.status === 'starting' ? (
          <WorkingRegion label={conv.setup} className="conversation-fill" />
        ) : loadingHistory ? (
          <HistorySkeleton />
        ) : fresh ? (
          <StartScreen conv={conv} compact={compact} />
        ) : (
          <Timeline timeline={conv.timeline} onAnswer={onAnswer} onSend={onSend} links={conv.links} compact={compact} heldAsk={heldAsk} />
        )}
        {conv.side?.open && <SideChat conv={conv} />}
        {conv.status === 'stopped' && <Stopped conv={conv} />}
        {conv.status !== 'failed' && !fresh && <Composer conv={conv} />}
        {!compact && terminalPlace === 'bottom' && <BottomPanel conv={conv} />}
      </section>
      {!compact && <Dock conv={conv} />}
    </div>
  );
}

function ConversationHeader({ conv, compact }: { conv: Conv; compact: boolean }) {
  const send = useApp((s) => s.send);
  const open = useApp((s) => s.prefs.open.find((o) => o.id === conv.id));
  const restart = useApp((s) => s.restart);
  const closeConversation = useApp((s) => s.closeConversation);
  const dock = useApp((s) => s.dock);
  const openDock = useApp((s) => s.openDock);
  const [onTop, setOnTop] = useState(false);
  const [menu, setMenu] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const title = conv.state.title || open?.title || (open?.kind === 'forge' ? 'New agent' : 'New conversation');
  const cwd = conv.state.cwd || open?.cwd || '';
  const ready = conv.status === 'ready';
  const diff = conv.diff;

  return (
    <header className="conv-header drag">
      {open?.kind === 'forge' && <IconHammer size={16} className="conv-kind-icon" aria-hidden />}
      {renaming ? (
        <form
          className="conv-title-form no-drag"
          onSubmit={(e) => {
            e.preventDefault();
            const value = new FormData(e.currentTarget).get('title');
            setRenaming(false);
            if (typeof value === 'string' && value.trim()) void send(conv.id, `/title ${value.trim()}`, true);
          }}
        >
          <input name="title" className="field conv-title-input" defaultValue={title} autoFocus onBlur={() => setRenaming(false)} />
        </form>
      ) : (
        <button type="button" className="conv-title no-drag" onDoubleClick={() => ready && setRenaming(true)} onClick={() => ready && setRenaming(true)} aria-label={`${title}, rename`}>
          {title}
        </button>
      )}
      {!compact && cwd && (
        <button type="button" className="chip-button no-drag" onClick={() => native()?.reveal(cwd)} data-tip={cwd} data-tip-side="down">
          <IconFolder size={14} aria-hidden />
          {folderName(cwd)}
        </button>
      )}
      {!compact && <BranchChip conv={conv} />}
      <span className="spacer" />
      {diff && diff.added + diff.removed > 0 && (
        <button
          type="button"
          className={`diff-chip no-drag${dock.tab === 'changes' ? ' is-on' : ''}`}
          onClick={() => openDock('changes')}
          data-tip={`${diff.files} ${diff.files === 1 ? 'file' : 'files'} changed`}
          data-tip-side="down"
        >
          <IconGitCompare size={15} aria-hidden />
          <DiffCount added={diff.added} removed={diff.removed} />
        </button>
      )}
      {!compact && <CommitButton conv={conv} />}
      {!compact && (
        <div className="header-tools no-drag">
          <ViewsMenu conv={conv} />
          <button type="button" className={`icon-button${dock.tab === 'terminal' ? ' is-on' : ''}`} aria-label="Terminal" data-tip={`Terminal  ${shortcutLabel(SHORTCUTS.terminal)}`} data-tip-side="down" onClick={() => openDock('terminal')}>
            <IconTerminal2 size={17} stroke={1.7} />
          </button>
          {cwd && <OpenIn path={cwd} />}
        </div>
      )}
      {ROLE.kind === 'chat' && native()?.setAlwaysOnTop && (
        <button type="button" className={`icon-button no-drag${onTop ? ' is-on' : ''}`} aria-label="Keep on top" aria-pressed={onTop} data-tip={onTop ? 'Stop keeping on top' : 'Keep on top'} data-tip-side="down" onClick={() => void native()?.setAlwaysOnTop?.(!onTop).then(setOnTop)}>
          <IconPin size={16} stroke={1.7} />
        </button>
      )}
      <div className="bar-anchor no-drag">
        <button type="button" className="icon-button" aria-label="More" data-tip="More" data-tip-side="down" onClick={() => setMenu(!menu)}>
          <IconDots size={17} />
        </button>
        {menu && (
          <Menu
            side="down"
            align="end"
            width={240}
            onClose={() => setMenu(false)}
            items={[
              { id: 'rename', label: 'Rename', icon: IconPencil, disabled: !ready },
              { id: 'review', label: 'Review the changes', icon: IconSearch, disabled: !ready },
              { id: 'status', label: 'Details', icon: IconInfoCircle, disabled: !ready },
              { id: 'export', label: 'Save as Markdown', icon: IconFileExport, disabled: !ready },
              { id: 'copy', label: 'Copy the last answer', icon: IconCopy, disabled: !ready },
              ...(native()?.openWindow && ROLE.kind !== 'chat' ? [{ id: 'popout', label: 'Open in a new window', icon: IconWindowMaximize, section: ' ' }] : []),
              ...(ROLE.kind === 'chat' ? [{ id: 'ontop', label: onTop ? 'Stop keeping on top' : 'Keep on top', icon: IconPin, selected: onTop, section: ' ' }] : []),
              { id: 'restart', label: 'Restart', icon: IconRefresh },
              { id: 'close', label: 'Close', icon: IconX },
            ]}
            onPick={(id) => {
              setMenu(false);
              if (id === 'rename') setRenaming(true);
              else if (id === 'review') void send(conv.id, '/review');
              else if (id === 'status') void send(conv.id, '/status', true);
              else if (id === 'export') void send(conv.id, '/export', true);
              else if (id === 'copy') void send(conv.id, '/copy', true);
              else if (id === 'popout') void native()?.openWindow?.(`chat=${encodeURIComponent(conv.id)}`, { width: 640, height: 820 });
              else if (id === 'ontop') void native()?.setAlwaysOnTop?.(!onTop).then(setOnTop);
              else if (id === 'restart') restart(conv.id);
              else if (id === 'close') closeConversation(conv.id);
            }}
          />
        )}
      </div>
    </header>
  );
}

/**
 * A new conversation: the mark, one question, the composer and a few ways in. A build
 * conversation never shows this: ForgeWorkspace opens it on the Describe card.
 */
function StartScreen({ conv, compact }: { conv: Conv; compact: boolean }) {
  const starters = useApp((st) => st.prefs.suggestions);
  const send = useApp((s) => s.send);
  const setScreen = useApp((s) => s.setScreen);
  const ready = conv.status === 'ready';
  const folder = folderName(conv.state.cwd ?? '');
  return (
    <div className="start">
      <div className={`start-inner${compact ? ' is-compact' : ''}`}>
        <Mark className="start-mark-svg text-accent" />
        <h1 className="start-title">What are we working on?</h1>
        {/* The header's folder chip says it when it is shown. */}
        {folder && compact && <p className="start-folder"><IconFolder size={14} aria-hidden /> {folder}</p>}
        <Composer conv={conv} />
        {conv.git?.repo && !compact && <WorktreeRow conv={conv} />}
        {starters && (
          <div className="examples starters">
            <span className="caption">Start with</span>
            {CHAT_STARTERS.map(({ title, prompt }) => (
              <button key={title} type="button" className="example-row" disabled={!ready} onClick={() => void send(conv.id, prompt)}>{title}</button>
            ))}
            {!conv.state.has_instructions && (
              <button type="button" className="example-row" disabled={!ready} onClick={() => void send(conv.id, '/init')}>Write ARCHITECT.md for this project</button>
            )}
          </div>
        )}
        {(conv.state.unavailable?.length ?? 0) > 0 && (
          <button type="button" className="start-warning" onClick={() => setScreen('settings', 'health')}>
            <IconAlertTriangle size={14} aria-hidden />
            {conv.state.unavailable!.join(', ')} not installed
          </button>
        )}
      </div>
    </div>
  );
}

/** Where the first message will run: this checkout, or a fresh worktree on its own branch. */
function WorktreeRow({ conv }: { conv: Conv }) {
  const open = useApp((s) => s.prefs.open.find((o) => o.id === conv.id));
  const setPrefs = useApp((s) => s.setPrefs);
  const on = !!open?.wantWorktree;
  const toggle = () => {
    const prefs = useApp.getState().prefs;
    setPrefs({ open: prefs.open.map((o) => (o.id === conv.id ? { ...o, wantWorktree: !on } : o)) });
  };
  return (
    <div className="worktree-row">
      <button type="button" role="switch" aria-checked={on} className={on ? 'worktree-toggle is-on' : 'worktree-toggle'} onClick={toggle} data-tip="Work on a separate branch in its own folder, so this checkout stays as it is">
        <IconGitFork size={14} aria-hidden />
        New worktree
        <span className="switch-track" aria-hidden><span className="switch-thumb" /></span>
      </button>
    </div>
  );
}

/** While a saved conversation loads: the shape of a conversation, not a spinner. */
function HistorySkeleton() {
  return (
    <div className="timeline" aria-busy="true" aria-label="Loading the conversation">
      <div className="timeline-inner skeleton">
        <div className="skel skel-user" />
        <div className="skel skel-line" style={{ width: '38%' }} />
        <div className="skel skel-line" style={{ width: '92%' }} />
        <div className="skel skel-line" style={{ width: '84%' }} />
        <div className="skel skel-line" style={{ width: '61%' }} />
        <div className="skel skel-user" style={{ width: '34%' }} />
        <div className="skel skel-line" style={{ width: '88%' }} />
        <div className="skel skel-line" style={{ width: '72%' }} />
      </div>
    </div>
  );
}

function Failed({ conv }: { conv: Conv }) {
  const restart = useApp((s) => s.restart);
  const setScreen = useApp((s) => s.setScreen);
  const closeConversation = useApp((s) => s.closeConversation);
  const openConversation = useApp((s) => s.openConversation);
  const open = useApp((s) => s.prefs.open.find((o) => o.id === conv.id));
  const reason = conv.error ?? '';
  const code = /^[a-z_]+$/.test(reason) ? reason : '';
  const startFresh = (cwd: string) => {
    closeConversation(conv.id);
    openConversation({ cwd });
  };
  // A saved conversation that is gone, or a folder that is gone, is not a crash.
  if (code === 'not_found' && open?.session) {
    return (
      <div className="conversation-fill failure">
        <div className="failure-icon"><IconMessageOff size={22} aria-hidden /></div>
        <h1>This conversation could not be found</h1>
        <p>It may have been deleted, or it was saved in another workspace.</p>
        <div className="button-row">
          <button type="button" className="button button-primary" onClick={() => startFresh(open.cwd)}>
            <IconPlus size={15} aria-hidden /> New conversation in {folderName(open.cwd)}
          </button>
          <button type="button" className="button button-quiet" onClick={() => closeConversation(conv.id)}>Close</button>
        </div>
      </div>
    );
  }
  if (code === 'cwd_missing') {
    return (
      <div className="conversation-fill failure">
        <div className="failure-icon"><IconFolderX size={22} aria-hidden /></div>
        <h1>{open?.worktree ? 'This worktree was removed' : open ? `${folderName(open.cwd)} is no longer there` : 'The folder is no longer there'}</h1>
        <p>{open?.worktree ? `Its branch ${open.worktree.branch} still has everything committed there.` : 'It was moved, renamed or deleted.'}{open?.session ? ' The conversation itself is saved.' : ''}</p>
        <div className="button-row">
          {open?.worktree && (
            <button type="button" className="button button-primary" onClick={() => void fetch('/api/git/worktree-restore', {
              method: 'POST',
              body: JSON.stringify({ cwd: open.worktree!.root, path: open.cwd, branch: open.worktree!.branch }),
            }).then(async (r) => {
              if (r.ok) restart(conv.id);
              else useApp.getState().showToast(String(((await r.json()) as { error?: string }).error ?? 'The worktree could not be recreated.'), 'error');
            })}>
              <IconGitFork size={15} aria-hidden /> Recreate the worktree
            </button>
          )}
          <button type="button" className={open?.worktree ? 'button' : 'button button-primary'} onClick={() => void chooseFolder(open?.cwd).then((cwd) => {
            if (!cwd) return;
            useApp.getState().moveConversation(conv.id, cwd);
            restart(conv.id);
          })}>
            <IconFolderOpen size={15} aria-hidden /> Choose its new place
          </button>
          <button type="button" className="button button-quiet" onClick={() => closeConversation(conv.id)}>Close</button>
        </div>
      </div>
    );
  }
  return (
    <div className="conversation-fill failure card-error" role="alert">
      <IconCircleX size={24} stroke={1.5} className="card-error-glyph" aria-hidden />
      <h1>Architect could not start</h1>
      <p>{code ? errorText(code) : 'The Architect process stopped before it was ready.'}</p>
      {!code && reason && <pre className="failure-detail">{reason}</pre>}
      <div className="button-row">
        <Button variant="primary" size="sm" data-action="retry" onClick={() => restart(conv.id)}>Retry</Button>
        <Button size="sm" onClick={() => setScreen('settings', 'health')}>Health check</Button>
        <Button size="sm" onClick={() => setScreen('settings', 'models')}>Sign-in and models</Button>
        <Button variant="ghost" size="sm" onClick={() => closeConversation(conv.id)}>Close</Button>
      </div>
    </div>
  );
}

export function Stopped({ conv }: { conv: Conv }) {
  const restart = useApp((s) => s.restart);
  return (
    <div className="stopped card-error" role="status">
      <IconCircleX size={24} stroke={1.5} className="card-error-glyph" aria-hidden />
      <span>This conversation stopped. It is saved.</span>
      <Button variant="primary" size="sm" data-action="retry" onClick={() => restart(conv.id)}>Retry</Button>
    </div>
  );
}

/** Claude Code's Views menu: every pane, with its key, and which are showing. */
function ViewsMenu({ conv }: { conv: Conv }) {
  const [open, setOpen] = useState(false);
  const dock = useApp((s) => s.dock);
  const openDock = useApp((s) => s.openDock);
  const tree = useApp((s) => s.prefs.fileTree);
  const sideOpen = !!conv.side?.open;
  const items = [
    { id: 'changes', label: 'Changes', icon: IconGitCompare, hint: shortcutLabel(SHORTCUTS.changes), selected: dock.tab === 'changes' },
    { id: 'files', label: 'Files', icon: IconFiles, hint: shortcutLabel(SHORTCUTS.quickOpen), selected: dock.tab === 'files' },
    { id: 'tree', label: 'Folder tree', icon: IconBinaryTree2, hint: shortcutLabel(SHORTCUTS.fileTree), selected: dock.tab === 'files' && tree },
    { id: 'terminal', label: 'Terminal', icon: IconTerminal2, hint: shortcutLabel(SHORTCUTS.terminal), selected: dock.tab === 'terminal' },
    { id: 'browser', label: 'Browser', icon: IconWorld, hint: shortcutLabel(SHORTCUTS.browser), selected: dock.tab === 'browser' },
    { id: 'side', label: 'Side question', icon: IconMessageQuestion, hint: shortcutLabel(SHORTCUTS.sideChat), selected: sideOpen, section: ' ' },
  ];
  return (
    <span className="bar-anchor">
      <button type="button" className={`icon-button${open || dock.tab ? ' is-on' : ''}`} aria-label="Views" aria-expanded={open} data-tip={open ? undefined : 'Views'} data-tip-side="down" onClick={() => setOpen(!open)}>
        <IconLayoutSidebarRight size={17} stroke={1.7} />
      </button>
      {open && (
        <Menu
          side="down"
          align="end"
          width={250}
          items={items}
          onClose={() => setOpen(false)}
          onPick={(id) => {
            setOpen(false);
            const state = useApp.getState();
            if (id === 'side') state.toggleSide(conv.id);
            else if (id === 'tree') {
              state.setPrefs({ fileTree: true });
              state.openDock('files', state.dock.file);
            } else openDock(dock.tab === id ? null : (id as DockTab));
          }}
        />
      )}
    </span>
  );
}

/** The header's +added -removed, counting to new values as a turn changes files. */
function DiffCount({ added, removed }: { added: number; removed: number }) {
  const a = useCountUp(added);
  const r = useCountUp(removed);
  return (
    <>
      <span className="num tone-ok">+{a}</span>
      <span className="num tone-err">-{r}</span>
    </>
  );
}

/** Terminals under the conversation (Codex's bottom panel; Settings > General > Terminals open). */
function BottomPanel({ conv }: { conv: Conv }) {
  const open = useApp((s) => s.bottom);
  const toggleBottom = useApp((s) => s.toggleBottom);
  const [height, setHeight] = useState(() => Number(localStorage.getItem('architect-bottom-h')) || 260);
  const [mounted, setMounted] = useState(open);
  useEffect(() => {
    if (open) setMounted(true);
  }, [open]);
  if (!mounted) return null;
  const onDrag = (event: React.PointerEvent) => {
    const target = event.currentTarget as HTMLElement;
    target.setPointerCapture(event.pointerId);
    const startY = event.clientY;
    const startH = height;
    const move = (e: PointerEvent) => setHeight(Math.max(120, Math.min(window.innerHeight * 0.7, startH + (startY - e.clientY))));
    const up = () => {
      target.removeEventListener('pointermove', move);
      target.removeEventListener('pointerup', up);
      try {
        localStorage.setItem('architect-bottom-h', String(Math.round(height)));
      } catch {
        /* storage blocked */
      }
    };
    target.addEventListener('pointermove', move);
    target.addEventListener('pointerup', up);
  };
  return (
    <div className={`bottom-panel${open ? '' : ' is-hidden'}`} style={{ height }}>
      <div className="bottom-resize" onPointerDown={onDrag} role="separator" aria-orientation="horizontal" />
      <button type="button" className="icon-button bottom-close" aria-label="Hide the terminal" data-tip={`Hide  ${shortcutLabel(SHORTCUTS.panelView)}`} onClick={() => toggleBottom(false)}>
        <IconX size={14} />
      </button>
      <TerminalPane conv={conv} visible={open} />
    </div>
  );
}
