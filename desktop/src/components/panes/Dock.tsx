/**
 * The right-hand dock, like Claude Code's panes: Changes (the folder's diff), Files
 * (a viewer with highlighting and line numbers) and Terminal (a real shell in the
 * conversation's folder). Ctrl+Shift+D, Ctrl+P and Ctrl+` open them; the width is
 * dragged and remembered. In a Forge conversation, Harness shows Forge's versions of
 * the harness it is writing.
 */
import { IconArrowsMaximize, IconArrowsMinimize, IconFileCode, IconFileDiff, IconGitCompare, IconTerminal2, IconWindowMaximize, IconWorld, IconX } from '@tabler/icons-react';
import { native } from '../../lib/native';
import clsx from 'clsx';
import { useRef, useState } from 'react';
import { useApp, type Conv, type DockTab } from '../../store/app';
import { shortcutLabel, SHORTCUTS, type Shortcut } from '../../lib/shortcuts';
import { ErrorBoundary } from '../ui/ErrorBoundary';
import { BrowserPane } from './BrowserPane';
import { ChangesPane } from './ChangesPane';
import { FileViewer } from './FileViewer';
import { HarnessPane } from './HarnessPane';
import { TerminalPane } from './TerminalPane';

const TABS: Array<{ id: DockTab; label: string; icon: typeof IconX; key?: Shortcut }> = [
  { id: 'harness', label: 'Harness', icon: IconFileDiff },
  { id: 'changes', label: 'Changes', icon: IconGitCompare, key: SHORTCUTS.changes },
  { id: 'files', label: 'Files', icon: IconFileCode, key: SHORTCUTS.quickOpen },
  { id: 'terminal', label: 'Terminal', icon: IconTerminal2, key: SHORTCUTS.terminal },
  { id: 'browser', label: 'Browser', icon: IconWorld, key: SHORTCUTS.browser },
];

/** The Harness pane's least width beside a build. */
const HARNESS_MIN = 560;

/** Harness is a Forge conversation's tab (or any conversation Forge has worked in). */
function shown(id: DockTab, conv: Conv, terminalPlace: string): boolean {
  if (id === 'terminal') return terminalPlace !== 'bottom';
  if (id === 'harness') return conv.state.state === 'forge' || (conv.forge.drafts?.length ?? 0) > 0;
  return true;
}

/** `full`: the dock is the whole window (a popped-out pane). */
export function Dock({ conv, full = false }: { conv: Conv; full?: boolean }) {
  const dock = useApp((s) => s.dock);
  const openDock = useApp((s) => s.openDock);
  const terminalPlace = useApp((s) => s.prefs.terminalPlace);
  const [width, setWidth] = useState(() => Number(localStorage.getItem('architect-dock-w')) || 520);
  const root = useRef<HTMLElement>(null);
  if (!dock.tab) return null;

  const onDrag = (event: React.PointerEvent) => {
    const target = event.currentTarget as HTMLElement;
    target.setPointerCapture(event.pointerId);
    const right = root.current?.getBoundingClientRect().right ?? window.innerWidth;
    const move = (e: PointerEvent) => setWidth(Math.min(Math.max(320, right - e.clientX), Math.round(window.innerWidth * 0.7)));
    const up = () => {
      target.removeEventListener('pointermove', move);
      target.removeEventListener('pointerup', up);
      try {
        localStorage.setItem('architect-dock-w', String(Math.round((root.current?.getBoundingClientRect().width ?? width))));
      } catch {
        /* storage blocked */
      }
    };
    target.addEventListener('pointermove', move);
    target.addEventListener('pointerup', up);
  };

  // Harness sets code beside a build: it keeps room for a line of it (the agent panel keeps its own).
  const dockWidth = dock.tab === 'harness' ? Math.max(width, HARNESS_MIN) : width;
  return (
    <aside ref={root} className={clsx('dock', (full || dock.full) && 'is-full')} style={full || dock.full ? undefined : { width: dockWidth }} aria-label="Panes">
      {!full && <div className="dock-resize" onPointerDown={onDrag} role="separator" aria-orientation="vertical" />}
      <header className="dock-head drag">
        <div className="dock-tabs no-drag" role="tablist">
          {TABS.filter((t) => shown(t.id, conv, terminalPlace)).map(({ id, label, icon: Icon, key }) => (
            <button
              key={id}
              type="button"
              role="tab"
              aria-selected={dock.tab === id}
              className={clsx('dock-tab', dock.tab === id && 'is-on')}
              onClick={() => openDock(id)}
              data-tip={key ? shortcutLabel(key) : undefined}
              data-tip-side="down"
            >
              <Icon size={15} stroke={1.8} aria-hidden />
              {label}
              {id === 'changes' && conv.diff && conv.diff.added + conv.diff.removed > 0 && (
                <span className="dock-count num">{conv.diff.files}</span>
              )}
            </button>
          ))}
        </div>
        <span className="spacer" />
        {!full && (
          <button
            type="button"
            className={clsx('icon-button no-drag', dock.full && 'is-on')}
            aria-label={dock.full ? 'Back beside the conversation' : 'Fill the window'}
            data-tip={`${dock.full ? 'Back beside the conversation' : 'Fill the window'}  ${shortcutLabel(SHORTCUTS.layout)}`}
            data-tip-side="down"
            onClick={() => useApp.setState((s) => ({ dock: { ...s.dock, full: !s.dock.full } }))}
          >
            {dock.full ? <IconArrowsMinimize size={15} /> : <IconArrowsMaximize size={15} />}
          </button>
        )}
        {!full && native()?.openWindow && dock.tab && (
          <button
            type="button"
            className="icon-button no-drag"
            aria-label="Open in a new window"
            data-tip="Open in a new window"
            data-tip-side="down"
            onClick={() => {
              void native()?.openWindow?.(`pane=${dock.tab}&conv=${encodeURIComponent(conv.id)}`, { width: 720, height: 640 });
              openDock(null);
            }}
          >
            <IconWindowMaximize size={15} />
          </button>
        )}
        {!full && <button type="button" className="icon-button no-drag" aria-label="Close panes" data-tip={`Close  ${shortcutLabel(SHORTCUTS.closePane)}`} data-tip-side="down" onClick={() => openDock(null)}>
          <IconX size={15} />
        </button>}
      </header>
      <div className="dock-body">
        {dock.tab === 'harness' && <ErrorBoundary compact label="Harness" resetKey={conv.id}><HarnessPane conv={conv} /></ErrorBoundary>}
        {dock.tab === 'changes' && <ErrorBoundary compact label="Changes" resetKey={conv.id}><ChangesPane conv={conv} /></ErrorBoundary>}
        {dock.tab === 'files' && <ErrorBoundary compact label="This file" resetKey={dock.file}><FileViewer conv={conv} path={dock.file} line={dock.line} /></ErrorBoundary>}
        {/* The terminal stays mounted while the dock is open so its shell keeps its screen. */}
        {terminalPlace !== 'bottom' && (
          <div className={clsx('dock-terminal', dock.tab !== 'terminal' && 'is-hidden')}>
            <TerminalPane conv={conv} visible={dock.tab === 'terminal'} />
          </div>
        )}
        {/* Pages stay loaded while another pane is in front. */}
        <div className={clsx('dock-terminal', dock.tab !== 'browser' && 'is-hidden')}>
          <BrowserPane conv={conv} visible={dock.tab === 'browser'} />
        </div>
      </div>
    </aside>
  );
}
