/** App-wide keyboard shortcuts (declared in lib/shortcuts.ts). */
import { useEffect } from 'react';
import { goBack, goForward } from '../../lib/navigation';
import { native } from '../../lib/native';
import { useShortcut } from '../../lib/useShortcut';
import { SHORTCUTS } from '../../lib/shortcuts';
import { showSidebar, sidebarYields, useApp } from '../../store/app';
import { chooseFolder } from './folders';

function currentFolder(): string {
  const { prefs, meta } = useApp.getState();
  const active = prefs.open.find((o) => o.id === prefs.active);
  return active?.cwd ?? prefs.folders[0] ?? meta.homedir ?? '';
}

function step(delta: number): void {
  const { prefs, activate } = useApp.getState();
  if (!prefs.open.length) return;
  const index = prefs.open.findIndex((o) => o.id === prefs.active);
  const next = prefs.open[(index + delta + prefs.open.length) % prefs.open.length];
  activate(next.id);
}

const s = useApp.getState;

/** Zoom the whole window and remember it for the next launch. */
function zoom(delta: number | null): void {
  const level = native()?.zoom?.(delta);
  if (level === undefined) return;
  try {
    localStorage.setItem('architect-zoom', String(level));
  } catch {
    /* storage blocked */
  }
  s().showToast(`Text size ${Math.round(100 * 1.2 ** level)}%`);
}

/** The zoom saved last time, applied once at startup. */
export function restoreZoom(): void {
  try {
    const saved = Number(localStorage.getItem('architect-zoom'));
    if (saved) native()?.zoom?.(saved);
  } catch {
    /* storage blocked */
  }
}

/** What each shortcut does; the macOS menu bar runs the same actions. */
// The model and permission menus bind their own keys in the composer.
const ACTIONS = {
  palette: () => s().setPalette(!s().palette),
  newChat: () => s().openConversation({ cwd: currentFolder() }),
  newForge: () => s().openConversation({ cwd: currentFolder(), kind: 'forge' }),
  settings: () => s().setScreen(s().screen === 'settings' ? 'chat' : 'settings'),
  agents: () => s().setScreen(s().screen === 'agents' ? 'chat' : 'agents'),
  openFolder: () => void chooseFolder(currentFolder()).then((cwd) => cwd && s().openConversation({ cwd })),
  closeConv: () => {
    const active = s().prefs.active;
    if (active && s().screen === 'chat') s().closeConversation(active);
  },
  nextConv: () => step(1),
  prevConv: () => step(-1),
  sidebar: () => (sidebarYields(s()) ? showSidebar() : s().setPrefs({ sidebar: !s().prefs.sidebar })),
  closePane: () => (s().dock.tab ? s().openDock(null) : undefined),
  nextTab: () => step(1),
  prevTab: () => step(-1),
  archive: () => {
    const active = s().prefs.active;
    if (active && s().screen === 'chat') s().archiveConversation(active);
  },
  unread: () => {
    const active = s().prefs.active;
    if (active) s().markUnread(active, !s().convs[active]?.unread);
  },
  clearUnread: () => {
    for (const id of Object.keys(s().convs)) s().markUnread(id, false);
  },
  attention: () => {
    const { prefs, convs } = s();
    const start = Math.max(0, prefs.open.findIndex((o) => o.id === prefs.active));
    const order = [...prefs.open.slice(start + 1), ...prefs.open.slice(0, start + 1)];
    const needs = order.find((o) => convs[o.id]?.timeline.items.some((i) => i.kind === 'ask' && !i.closed))
      ?? order.find((o) => convs[o.id]?.unread);
    if (needs) s().activate(needs.id);
  },
  pin: () => {
    const open = s().prefs.open.find((o) => o.id === s().prefs.active);
    if (open) s().togglePin(open.session ?? open.id);
  },
  rename: () => window.dispatchEvent(new CustomEvent('architect:rename', { detail: s().prefs.active })),
  searchChats: () => {
    if (!s().prefs.sidebar || sidebarYields(s())) showSidebar();
    requestAnimationFrame(() => window.dispatchEvent(new Event('architect:search-sidebar')));
  },
  copyCwd: () => {
    const open = s().prefs.open.find((o) => o.id === s().prefs.active);
    if (open) void navigator.clipboard.writeText(open.cwd).then(() => s().showToast('Copied the folder path'));
  },
  quickOpen: () => s().setQuickOpen(!s().quickOpen),
  terminal: () => (s().prefs.terminalPlace === 'bottom' ? s().toggleBottom() : s().openDock('terminal')),
  fileTree: () => {
    if (s().dock.tab !== 'files') {
      s().setPrefs({ fileTree: true });
      s().openDock('files', s().dock.file);
    } else s().setPrefs({ fileTree: !s().prefs.fileTree });
  },
  panelView: () => {
    const { prefs, convs, dock } = s();
    const conv = prefs.active ? convs[prefs.active] : undefined;
    const forge = prefs.open.find((o) => o.id === prefs.active)?.kind === 'forge' || conv?.state.state === 'forge';
    // In a build, Mod+J opens or closes the Report sheet.
    if (forge) s().openForgeSheet(s().forgeSheet === 'report' ? null : 'report');
    else if (prefs.terminalPlace === 'bottom') s().toggleBottom();
    else s().openDock(dock.tab ? null : 'changes');
  },
  changes: () => s().openDock('changes'),
  viewMode: () => {
    const verbose = !s().prefs.verbose;
    s().setPrefs({ verbose });
    s().showToast(verbose ? 'Verbose view: every step and result' : 'Normal view: steps folded');
  },
  shortcuts: () => s().setScreen('settings', 'shortcuts'),
  zoomIn: () => zoom(0.5),
  zoomOut: () => zoom(-0.5),
  zoomReset: () => zoom(null),
  fullScreen: () => void native()?.toggleFullScreen?.(),
  back: goBack,
  undo: () => s().undoLast(),
  redo: () => s().redoLast(),
  redoWin: () => s().redoLast(),
  activity: () => window.dispatchEvent(new Event('architect:activity')),
  newChatAlt: () => s().openConversation({ cwd: currentFolder() }),
  sideChatAlt: () => {
    const active = s().prefs.active;
    if (active && s().screen === 'chat') s().toggleSide(active);
  },
  reviewAlt: () => s().openDock('changes'),
  browser: () => s().openDock('browser'),
  newWindow: () => void native()?.openWindow?.('window', { width: 1280, height: 860 }),
  layout: () => s().cycleLayout(),
  sideChat: () => {
    const active = s().prefs.active;
    if (active && s().screen === 'chat') s().toggleSide(active);
  },
  forward: goForward,
} satisfies Partial<Record<keyof typeof SHORTCUTS, () => void>>;

/** Menu bar commands (electron/main.ts) and the actions they run. */
const MENU: Record<string, () => void> = {
  settings: () => s().setScreen('settings'),
  shortcuts: ACTIONS.shortcuts,
  'new-chat': ACTIONS.newChat,
  'new-forge': ACTIONS.newForge,
  'open-folder': ACTIONS.openFolder,
  'quick-open': () => s().setQuickOpen(true),
  'close-conv': ACTIONS.closeConv,
  palette: () => s().setPalette(true),
  sidebar: ACTIONS.sidebar,
  changes: ACTIONS.changes,
  terminal: ACTIONS.terminal,
  'side-chat': ACTIONS.sideChat,
  browser: ACTIONS.browser,
  routines: () => s().setScreen('routines'),
  'new-window': ACTIONS.newWindow,
  'file-tree': ACTIONS.fileTree,
  'next-conv': ACTIONS.nextConv,
  'prev-conv': ACTIONS.prevConv,
  rename: ACTIONS.rename,
  pin: ACTIONS.pin,
  unread: ACTIONS.unread,
  archive: ACTIONS.archive,
};

const NAMES = Object.keys(ACTIONS) as Array<keyof typeof ACTIONS>;

export function Shortcuts() {
  // Re-bind when you change a shortcut (the table is updated in place).
  useApp((st) => st.prefs.keybindings);
  // A fixed list, so the hooks run in the same order on every render.
  for (const name of NAMES) {
    // eslint-disable-next-line react-hooks/rules-of-hooks
    useShortcut(SHORTCUTS[name], ACTIONS[name]);
  }
  useEffect(() => native()?.onMenu?.((command) => MENU[command]?.()), []);
  useEffect(() => {
    // The mouse's own back and forward buttons.
    const onMouse = (event: MouseEvent) => {
      if (event.button === 3) goBack();
      else if (event.button === 4) goForward();
      else return;
      event.preventDefault();
    };
    window.addEventListener('mouseup', onMouse);
    return () => window.removeEventListener('mouseup', onMouse);
  }, []);
  return null;
}
