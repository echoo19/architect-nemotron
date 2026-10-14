/**
 * App state. One zustand store split by concern: prefs (persisted to the control
 * server, so they survive restarts), conversations (one bridge process each), and
 * screen routing. The bridge is the source of truth for everything Architect
 * knows; this only holds what the page shows.
 */
import { create } from 'zustand';
import { emptyFeed, feedMessage, type ForgeFeed } from '../lib/forge';
import type { WizardView } from '../lib/wizard';
import { emptyTimeline, fold, fromHistory, type Item, type Timeline } from '../lib/timeline';
import { WINDOW_ONLY, windowId, windowRole } from '../lib/window';
import type { Routine } from '../lib/schedule';
import { applyBindings, type Shortcut } from '../lib/shortcuts';
import { Wire, wsUrl, type BridgeMessage } from '../lib/wire';

export type Theme = 'system' | 'light' | 'dark';
export type Screen = 'chat' | 'agents' | 'settings' | 'routines';
export type ConvKind = 'chat' | 'forge';

export interface OpenConv {
  id: string;
  cwd: string;
  kind: ConvKind;
  /** Architect's own session id, once known; used to resume after a restart. */
  session?: string;
  /** Set when the conversation runs in a worktree Architect made for it. */
  worktree?: { root: string; branch: string };
  /** Start this conversation in a new worktree on its first message. */
  wantWorktree?: boolean;
  title?: string;
}

export interface Prefs {
  theme: Theme;
  folders: string[];
  open: OpenConv[];
  active: string | null;
  drafts: Record<string, string>;
  detail: 'simple' | 'full';
  sidebar: boolean;
  workspace?: string;
  profile?: string;
  probe: boolean;
  /** Saved conversations hidden from the sidebar (Settings > Archived). */
  archived: string[];
  /** Pinned conversations (session id once saved, else the open conversation id). */
  pinned: string[];
  sidebarGroup: 'time' | 'folder';
  collapsed: string[];
  /** Transcript view: normal folds tool use; verbose shows every step and result. */
  verbose: boolean;
  /** Branch names for new worktrees and branches start with this. */
  branchPrefix: string;
  /** Start new conversations in their own worktree when the folder is a repository. */
  worktreeDefault: boolean;
  /** Remove a conversation's worktree when it is archived (Claude Code does). */
  removeWorktreeOnArchive: boolean;
  /** The Files pane shows its folder tree (Ctrl+Shift+E). */
  fileTree: boolean;
  /** Shell for new terminals; unset uses the system's usual one. */
  terminalShell?: string;
  /** Enter sends (Shift+Enter for a new line), or Ctrl/Cmd+Enter sends (Enter for a new line). */
  sendKey: 'enter' | 'mod-enter';
  /** Tell me when a turn finishes: never, only when I'm elsewhere, or always. */
  notifyTurns: 'never' | 'background' | 'always';
  /** Tell me when Architect needs an approval or an answer (when I'm elsewhere). */
  notifyAsks: boolean;
  /** Keep the computer awake while a turn runs. */
  preventSleep: boolean;
  /** Suggested prompts on a new conversation. */
  suggestions: boolean;
  /** Folders you have said Architect may work in (and everything under them). */
  trusted?: string[];
  /** Sidebar: show only this project's conversations. */
  sidebarProject?: string;
  /** Sidebar: which conversations to list (archived ones are otherwise hidden). */
  sidebarStatus: 'all' | 'archived';
  /** Projects removed from the sidebar (their conversations are kept). */
  hiddenProjects: string[];
  /** A message sent while Architect works: steer the running turn, or wait for it to finish. */
  followUp: 'steer' | 'queue';
  /** Your own keyboard shortcuts, by name (Settings > Keyboard shortcuts). */
  keybindings?: Record<string, Shortcut>;
  /** Where terminals open: in the panes on the right, or a panel under the conversation (Codex's bottom panel). */
  terminalPlace: 'right' | 'bottom';
  /** Keep this many of Architect's worktrees per repository; older ones are removed. */
  worktreeLimit: number;
  /** Remove worktrees beyond the limit automatically. */
  pruneWorktrees: boolean;
}

export interface BridgeState {
  started: boolean;
  session_id?: string;
  title?: string | null;
  state?: 'normal' | 'forge' | 'plan';
  mode?: string;
  permission?: 'ask' | 'auto-edit' | 'full-auto' | 'plan';
  model?: string;
  models?: string[];
  spent_usd?: number;
  budget_usd?: number;
  elapsed_s?: number;
  budget_s?: number;
  context_percent?: number | null;
  context_tokens?: number | null;
  tokens_in?: number;
  tokens_out?: number;
  unpriced?: boolean;
  in_flight?: number;
  busy?: boolean;
  queue?: string[];
  asks?: number[];
  cwd?: string;
  workspace?: string;
  profile_path?: string;
  unavailable?: string[];
  has_instructions?: boolean;
  mcp_tools?: number;
  supports_images?: boolean;
  pending_images?: string[];
}

export type ConvStatus = 'starting' | 'setup' | 'trust' | 'ready' | 'failed' | 'stopped';

export type { ForgeFeed } from '../lib/forge';

export interface Conv {
  id: string;
  status: ConvStatus;
  error?: string;
  timeline: Timeline;
  state: BridgeState;
  forge: ForgeFeed;
  links: string[];
  /** A turn finished while the person was looking elsewhere. */
  unread?: boolean;
  /** When something last happened here (ms), for the sidebar. */
  activity?: number;
  /** Files changed in the folder, from /diff after each turn; null when clean. */
  diff?: DiffStat | null;
  /** Loading a saved conversation's history. */
  resuming?: boolean;
  /** Spend when the current turn started, to show what the turn cost. */
  turnStartSpend?: number;
  awaitingMeta?: boolean;
  git?: GitStatus;
  /** Side questions (Ctrl/Cmd+;): answered from this conversation, never added to it or saved. */
  side?: SideChat;
  /** A packaged app's first start: what its launcher is doing to set up Python, until ready. */
  setup?: string;
}

export interface SideItem { id: number; question: string; answer: string; steps: string[]; done: boolean; error?: string; cost?: number }
export interface SideChat { open: boolean; items: SideItem[] }

/** `harness`: Forge's versions of the harness it is writing, as diffs (Forge conversations). */
export type DockTab = 'changes' | 'files' | 'terminal' | 'browser' | 'harness';
export interface Dock {
  tab: DockTab | null; file?: string; line?: number; /** An address for the Browser pane to open in a new tab. */ url?: string; /** The panes fill the window (Codex's full view). */ full?: boolean;
  /** The person brought the sidebar back while Harness sits beside a build (see `sidebarYields`). */
  keepSidebar?: boolean;
}

export interface GitStatus {
  git: boolean;
  repo: boolean;
  root?: string;
  branch?: string | null;
  detached?: boolean;
  upstream?: string | null;
  ahead?: number;
  behind?: number;
  remote?: boolean;
  gh?: boolean;
  staged: number;
  unstaged: number;
  untracked: number;
  worktree?: { main: string; isLinked: boolean };
  defaultBranch?: string | null;
}

export interface DiffStat { files: number; added: number; removed: number; text: string }

export interface SessionRow {
  id: string;
  title: string;
  time: number;
  cwd: string;
  messages: number;
}

interface AppState {
  ready: boolean;
  online: boolean;
  prefs: Prefs;
  meta: { root?: string; home?: string; platform?: string; homedir?: string };
  convs: Record<string, Conv>;
  sessions: SessionRow[];
  screen: Screen;
  settingsPane: string;
  palette: boolean;
  toast: { text: string; level: 'info' | 'error'; action?: { label: string; run: () => void } } | null;
  /** Routines (scheduled instructions) and their runs, kept by the server. */
  routines: Routine[];
  refreshRoutines(): Promise<void>;
  /** App actions that can be taken back (archive, close, pin...), newest last. */
  undoStack: UndoEntry[];
  redoStack: UndoEntry[];

  init(): Promise<void>;
  setPrefs(patch: Partial<Prefs>): void;
  setScreen(screen: Screen, pane?: string): void;
  setPalette(open: boolean): void;
  /** The build page's sheets (redesign v2): which one is open, and where the Plan sheet scrolls to. Not saved. */
  forgeSheet: 'plan' | 'report' | null;
  forgeSheetSection?: 'next';
  /** The conversation the sheet was opened on (the active one then): a sheet opens only over its own build. */
  forgeSheetConv?: string;
  /** The person opened the build page's transcript rail. Not saved. */
  forgeTranscript: boolean;
  /**
   * Which finished step of a build the person is looking at (Results, Try it, Add), by
   * conversation. Unset, it follows the build (lib/wizard.ts `defaultView`). Not saved.
   */
  wizardView: Record<string, WizardView>;
  /** Set the step a build shows; null goes back to following the build. */
  setWizardView(convId: string, view: WizardView | null): void;
  /** The person chose to connect the agent in their code themselves (step 6 Skip), by conversation. Not saved. */
  wizardSkipped: Record<string, boolean>;
  setWizardSkipped(convId: string, skipped: boolean): void;
  openForgeSheet(which: 'plan' | 'report' | null, section?: 'next'): void;
  setForgeTranscript(open: boolean): void;
  showToast(text: string, level?: 'info' | 'error', action?: { label: string; run: () => void }): void;
  /** Record an action that can be undone; the toast says what happened and offers Undo. */
  pushUndo(label: string, undo: () => void, redo: () => void): void;
  undoLast(): void;
  redoLast(): void;

  openConversation(opts: { cwd: string; kind?: ConvKind; resume?: string; title?: string; activate?: boolean }): string;
  activate(id: string): void;
  closeConversation(id: string): void;
  archiveConversation(id: string): void;
  archiveSession(sessionId: string): void;
  unarchive(sessionId: string): void;
  togglePin(key: string): void;
  markUnread(id: string, unread?: boolean): void;
  deleteSession(sessionId: string): Promise<boolean>;
  /** Copy a saved conversation (from its start up to `upto` of your messages) into a new one and open it. */
  forkSession(sessionId: string, upto?: number): Promise<void>;
  /** Trust this conversation's folder and start it. */
  trustFolder(id: string): void;
  /** Show or hide the side question panel. */
  toggleSide(id: string, open?: boolean): void;
  /** Ask a side question: answered from this conversation, nothing added to it. */
  askSide(id: string, question: string): Promise<void>;
  cancelSide(id: string, sideId: number): void;
  /** Apply the worktree limit in this repository (open and pinned conversations are kept). */
  pruneWorktrees(cwd: string): Promise<void>;
  restart(id: string): void;
  setDraft(id: string, text: string): void;

  /** `steer` adds the message to a running turn instead of queueing it (the core reads it after its current step). */
  send(id: string, text: string, quiet?: boolean, steer?: boolean): Promise<void>;
  cancel(id: string): Promise<void>;
  answer(id: string, askId: number, value: string | null): Promise<void>;
  request<T = unknown>(id: string, method: string, params?: Record<string, unknown>): Promise<T>;
  refreshSessions(): Promise<void>;
  boot(id: string): Promise<void>;
  refreshDiff(id: string): Promise<void>;
  refreshGit(id: string): Promise<void>;
  gitCall<T = Record<string, unknown>>(id: string, op: string, body?: Record<string, unknown>): Promise<T>;
  moveConversation(id: string, cwd: string, worktree?: { root: string; branch: string } | null): void;
  sendWhenReady(id: string, text: string): void;
  /** The right-hand dock (Claude Code panes): which tab, and the file the viewer shows. */
  dock: Dock;
  openDock(tab: DockTab | null, file?: string, line?: number): void;
  /** Show an address in the Browser pane (a new tab). */
  openBrowser(url?: string): void;
  /** A second conversation beside the active one (Claude Code's split view); null closes it. */
  split: string | null;
  setSplit(id: string | null): void;
  /** The bottom panel (terminals, when they open at the bottom). */
  bottom: boolean;
  toggleBottom(open?: boolean): void;
  /** Panes beside the conversation, filling the window, or hidden (Codex's layout cycle). */
  cycleLayout(): void;
  quickOpen: boolean;
  setQuickOpen(open: boolean): void;
}

export const DEFAULT_PREFS: Prefs = {
  theme: 'system',
  folders: [],
  open: [],
  active: null,
  drafts: {},
  detail: 'simple',
  sidebar: true,
  probe: true,
  archived: [],
  pinned: [],
  sidebarGroup: 'time',
  collapsed: [],
  verbose: false,
  branchPrefix: 'architect/',
  worktreeDefault: false,
  removeWorktreeOnArchive: true,
  fileTree: true,
  sendKey: 'enter',
  notifyTurns: 'background',
  notifyAsks: true,
  preventSleep: true,
  suggestions: true,
  sidebarStatus: 'all',
  hiddenProjects: [],
  worktreeLimit: 15,
  terminalPlace: 'right',
  followUp: 'steer',
  pruneWorktrees: true,
};

export const wire = new Wire(typeof window === 'undefined' ? 'ws://127.0.0.1/api/ws' : wsUrl());

const newId = (): string => `c${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`;

let saveTimer: ReturnType<typeof setTimeout> | null = null;
function persist(prefs: Prefs): void {
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    // Other windows save the shared settings only; which conversation each window
    // shows stays its own (the main window's is the one restored at launch).
    const body: Partial<Prefs> = { ...prefs };
    if (ROLE.kind !== 'main') for (const key of WINDOW_ONLY) delete body[key];
    void fetch('/api/prefs', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', 'X-Architect-Window': windowId },
      body: JSON.stringify(body),
    }).catch(() => undefined);
  }, 250);
}

/** What this window is (main, another app window, or a popped-out conversation or pane). */
export const ROLE = windowRole();

export function applyTheme(theme: Theme): void {
  const dark = theme === 'dark' || (theme === 'system' && !window.matchMedia('(prefers-color-scheme: light)').matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  try {
    localStorage.setItem('architect-theme', theme === 'system' ? '' : theme);
  } catch {
    /* storage may be blocked */
  }
}

/** Count a unified diff's files and lines. */
export function diffStat(text: string): DiffStat | null {
  if (!text.trim()) return null;
  let files = 0;
  let added = 0;
  let removed = 0;
  let untracked = false;
  for (const line of text.split('\n')) {
    if (/^Untracked files:/.test(line)) {
      untracked = true;
      continue;
    }
    if (untracked) {
      if (line.trim() && !line.trim().startsWith('...')) files += 1;
      continue;
    }
    if (line.startsWith('diff --git') || (line.startsWith('+++ ') && !text.includes('diff --git'))) files += 1;
    else if (line.startsWith('+') && !line.startsWith('+++')) added += 1;
    else if (line.startsWith('-') && !line.startsWith('---')) removed += 1;
  }
  return { files: Math.max(files, added + removed > 0 ? 1 : 0), added, removed, text };
}

/**
 * The sidebar steps aside while Forge's harness versions sit beside a build, so the agent
 * card and the diff keep the room they need; it comes back when Harness closes, or at
 * once when the person asks for it.
 */
export function sidebarYields(s: Pick<AppState, 'screen' | 'dock' | 'prefs' | 'convs'>): boolean {
  if (s.screen !== 'chat' || s.dock.tab !== 'harness' || s.dock.full || s.dock.keepSidebar) return false;
  const id = s.prefs.active;
  const conv = id ? s.convs[id] : undefined;
  if (!conv) return false;
  return s.prefs.open.find((o) => o.id === id)?.kind === 'forge' || conv.state.state === 'forge';
}

/** Bring the sidebar back: shown in the preferences, and kept while Harness stays open. */
export function showSidebar(): void {
  const s = useApp.getState();
  if (!s.prefs.sidebar) s.setPrefs({ sidebar: true });
  if (s.dock.tab) useApp.setState({ dock: { ...s.dock, keepSidebar: true } });
}

/**
 * A re-attached conversation rebuilt from its process's recent messages, the Forge board
 * included (the same folds as live messages). Messages carry when they were sent (`at`:
 * turns, events and settled questions): the fold runs at those times, so a replayed turn's
 * work keeps its real length ("Built the agent in 2m 31s", from Build it, not from the
 * /make before the plan card), and a quiet turn (Try it) shows as running, as it does live.
 */
export function rebuild(replay: BridgeMessage[], start: Timeline = emptyTimeline(), feed: ForgeFeed = emptyFeed()): { timeline: Timeline; forge: ForgeFeed; state?: BridgeState } {
  let timeline = start;
  let forge = feed;
  let state: BridgeState | undefined;
  let at = Date.now();
  for (const msg of replay) {
    if (typeof msg.at === 'number') at = msg.at * 1000;
    forge = feedMessage(forge, msg);
    if (msg.type === 'state') state = msg.state as BridgeState;
    else if (msg.type === 'turn_started' && msg.quiet) timeline = { ...timeline, running: true };
    else timeline = fold(timeline, msg, at);
  }
  return { timeline, forge, ...(state ? { state } : {}) };
}

/** A saved conversation as the bridge's `history` gives it (architect/desktop/bridge.py). */
export interface SavedHistory {
  messages: Array<Record<string, unknown>>;
  /**
   * What it showed: the bridge messages it was drawn from, in order. A `{type: 'view', from,
   * to}` among them stands for transcript messages `from` to `to`, which it never showed.
   */
  view?: BridgeMessage[] | null;
}

/**
 * A saved conversation, reopened as it was shown. The transcript alone holds only what the
 * model read: a slash command's turn (/make @spec.json, /export), the plan and result cards,
 * the build and its Forge feed, and Try it's runs are not in it. The bridge keeps the
 * messages it sent beside the transcript (`view`), and they fold here as live messages do.
 * Turns that record never saw (saved before it began, or added in the terminal) come from
 * the transcript, where it marks them. A turn the app closed during ends where its record
 * does, and no question is left open. Keys start with "h" and the questions' ids came from
 * another process, so nothing the new process sends is taken for one of these.
 */
export function restored(history: SavedHistory): { timeline: Timeline; forge: ForgeFeed } {
  const view = history.view ?? [];
  let timeline = emptyTimeline();
  let forge = emptyFeed();
  if (view.some((msg) => msg.type !== 'view')) {
    let last: number | undefined;
    let run: BridgeMessage[] = [];
    const play = () => {
      if (run.length) ({ timeline, forge } = rebuild(run, timeline, forge));
      run = [];
      if (timeline.running) timeline = fold(timeline, { type: 'turn_finished', quiet: true }, last !== undefined ? last * 1000 : Date.now());
    };
    for (const msg of view) {
      if (msg.type !== 'view') {
        if (typeof msg.at === 'number') last = msg.at;
        run.push(msg);
        continue;
      }
      play();
      const from = Math.max(0, Number(msg.from) || 0);
      timeline = fromHistory(history.messages.slice(from, Math.max(from, Number(msg.to) || 0)), timeline);
    }
    play();
  } else {
    timeline = fromHistory(history.messages);
  }
  const items = timeline.items.map((item): Item => ({ ...item, ...(item.kind === 'ask' ? { closed: true } : {}), key: `h${item.key}` }));
  return { timeline: { ...timeline, items, running: false, thinking: null }, forge };
}

/** A desktop notification when the person is not looking at this conversation. */
function notify(title: string, body: string, onClick: () => void): void {
  try {
    if (typeof Notification === 'undefined' || Notification.permission !== 'granted') return;
    const note = new Notification(title, { body, silent: false });
    note.onclick = () => {
      window.focus();
      onClick();
    };
  } catch {
    /* notifications unavailable */
  }
}

const emptyConv = (id: string): Conv => ({
  id, status: 'starting', timeline: emptyTimeline(), state: { started: false }, forge: emptyFeed(), links: [],
});

/** What a conversation's process is asked to do once it is up, keyed by conversation. */
let initStarted = false;
const pendingBoot = new Map<string, { resume?: string }>();

/** Is this folder, or one it sits inside, trusted? Case-insensitive on Windows. */
/** A path compared the way the system compares it: forward slashes, Windows drives case-insensitive. */
export function normPath(p: string): string {
  const unix = p.replace(/\\/g, '/').replace(/\/+$/, '');
  return /^[a-zA-Z]:/.test(unix) ? unix.toLowerCase() : unix;
}

export function isTrusted(cwd: string, trusted: string[]): boolean {
  const norm = normPath;
  const target = norm(cwd);
  return trusted.some((t) => {
    const root = norm(t);
    return target === root || target.startsWith(`${root}/`);
  });
}

/** True while an undo or redo runs, so its own steps are not recorded as new actions. */
let replaying = false;

export interface UndoEntry { label: string; undo: () => void; redo: () => void }
const pendingSend = new Map<string, string>();

export const useApp = create<AppState>((set, get) => {
  const patchConv = (id: string, update: (conv: Conv) => Partial<Conv>): void => {
    set((s) => {
      const conv = s.convs[id];
      if (!conv) return s;
      return { convs: { ...s.convs, [id]: { ...conv, ...update(conv) } } };
    });
  };

  const onMessage = (id: string, msg: BridgeMessage): void => {
    const type = msg.type;
    // Before Python starts, a packaged app's launcher reports its setup (electron/python-launch.cjs).
    if (type === 'setup') {
      patchConv(id, () => ({ setup: typeof msg.text === 'string' ? msg.text : undefined }));
      return;
    }
    if (type === 'ready') {
      patchConv(id, (conv) => (conv.setup === undefined ? {} : { setup: undefined }));
      return;
    }
    if (type === 'side' || type === 'side_done') {
      patchConv(id, (conv) => {
        const side = conv.side ?? { open: true, items: [] };
        const known = side.items.some((item) => item.id === msg.id);
        const all = known ? side.items : [...side.items, { id: Number(msg.id), question: '', answer: '', steps: [], done: false }];
        const items = all.map((item) => {
          if (item.id !== msg.id) return item;
          if (type === 'side_done') {
            return { ...item, done: true, answer: typeof msg.text === 'string' ? msg.text : item.answer, error: msg.error as string | undefined, cost: msg.cost as number | undefined };
          }
          const event = msg.event as { kind?: string; text?: string; summary?: string; name?: string };
          if (event.kind === 'delta') return { ...item, answer: item.answer + (event.text ?? '') };
          if (event.kind === 'answer') return { ...item, answer: event.text ?? item.answer };
          if (event.kind === 'tool_started') return { ...item, steps: [...item.steps, event.summary || event.name || 'a step'] };
          return item;
        });
        return { side: { ...side, items } };
      });
      return;
    }
    if (type === 'state') {
      const state = msg.state as BridgeState;
      const before = get().convs[id];
      patchConv(id, () => ({ state, status: state.started ? 'ready' : get().convs[id]?.status ?? 'starting' }));
      // The state after a turn carries its spend: show what the turn cost, then
      // look at what changed in the folder.
      if (before?.awaitingMeta && !state.busy) {
        const cost = Math.max(0, (state.spent_usd ?? 0) - (before.turnStartSpend ?? 0));
        patchConv(id, (conv) => ({ awaitingMeta: false, timeline: fold(conv.timeline, { type: 'turn_meta', cost }) }));
        void get().refreshDiff(id);
        void get().refreshGit(id);
      }
      // Remember the Architect session id so a restart resumes this conversation.
      const open = get().prefs.open.find((o) => o.id === id);
      // Only a saved conversation has a title; an empty one has nothing to resume.
      if (open && state.session_id && state.title && (open.session !== state.session_id || open.title !== (state.title ?? open.title))) {
        get().setPrefs({
          open: get().prefs.open.map((o) => (o.id === id ? { ...o, session: state.session_id, title: state.title ?? o.title } : o)),
        });
      }
      return;
    }
    if (type === 'forge') {
      patchConv(id, (conv) => ({ forge: feedMessage(conv.forge, msg) }));
      return;
    }
    // A new plan starts a new build's Forge feed (the cards themselves fold below).
    if (type === 'event' && (msg.event as Record<string, unknown>)?.kind === 'plan_card') {
      patchConv(id, (conv) => {
        const forge = feedMessage(conv.forge, msg);
        return forge === conv.forge ? {} : { forge };
      });
    }
    if (type === 'event' && (msg.event as Record<string, unknown>)?.kind === 'link') {
      const url = String((msg.event as Record<string, unknown>).url ?? '');
      patchConv(id, (conv) => ({ links: [...conv.links, url] }));
      return;
    }
    if (type === 'turn_finished') {
      // The build's turn ended: the feed keeps when, for the last model's time.
      patchConv(id, (conv) => {
        const forge = feedMessage(conv.forge, msg);
        return forge === conv.forge ? {} : { forge };
      });
      // A turn may have saved or renamed the conversation.
      void get().refreshSessions();
      if (!msg.quiet) {
        const away = get().prefs.active !== id || get().screen !== 'chat' || document.hidden;
        patchConv(id, () => ({ awaitingMeta: true, activity: Date.now(), ...(away ? { unread: true } : {}) }));
        const when = get().prefs.notifyTurns;
        if (when === 'always' || (when === 'background' && (away || !document.hasFocus()))) {
          const title = get().convs[id]?.state.title || 'Architect';
          notify(title, 'Finished', () => get().activate(id));
        }
      } else if (String(msg.text ?? '').startsWith('/undo')) {
        void get().refreshDiff(id);
        void get().refreshGit(id);
      }
    }
    if (type === 'turn_started' && !msg.quiet) {
      patchConv(id, (conv) => ({ turnStartSpend: conv.state.spent_usd ?? 0, activity: Date.now() }));
    }
    if (type === 'ask') {
      const away = get().prefs.active !== id || get().screen !== 'chat' || document.hidden || !document.hasFocus();
      if (away && get().prefs.notifyAsks) {
        const ask = msg.ask as { kind?: string; question?: string };
        const title = get().convs[id]?.state.title || 'Architect';
        notify(title, ask.kind === 'approval' ? 'Needs your OK to continue' : ask.question?.split('\n')[0] ?? 'Has a question for you', () => get().activate(id));
      }
    }
    if (type === 'turn_started' && msg.quiet) {
      patchConv(id, (conv) => ({ timeline: { ...conv.timeline, running: true } }));
      return;
    }
    patchConv(id, (conv) => {
      const next = fold(conv.timeline, msg);
      return next === conv.timeline ? {} : { timeline: next };
    });
  };

  wire.onMessage(onMessage);
  wire.onStatus((online) => set({ online }));
  wire.onAnyFrame((frame) => {
    if (frame.t === 'routines') set({ routines: frame.list as Routine[] });
  });
  // Settings changed in another window: take them, keep this window's own view.
  wire.onAnyFrame((frame) => {
    if (frame.t !== 'prefs' || frame.from === windowId || !get().ready) return;
    const mine = get().prefs;
    const incoming = { ...DEFAULT_PREFS, ...(frame.prefs as Partial<Prefs>), active: mine.active, sidebar: mine.sidebar };
    set({ prefs: incoming });
    if (incoming.theme !== mine.theme) applyTheme(incoming.theme);
    applyBindings(incoming.keybindings);
    if (ROLE.kind === 'chat' || ROLE.kind === 'pane') {
      // A pop-out closes with its conversation.
      if (ROLE.conv && !incoming.open.some((o) => o.id === ROLE.conv)) window.close();
      return;
    }
    // Conversations opened or closed in the other window.
    const before = new Set(mine.open.map((o) => o.id));
    const after = new Set(incoming.open.map((o) => o.id));
    for (const open of incoming.open) {
      if (before.has(open.id)) continue;
      patchConv(open.id, () => emptyConv(open.id));
      if (open.session) pendingBoot.set(open.id, { resume: open.session });
      wire.open(open.id, open.cwd);
    }
    set((st) => {
      const convs = { ...st.convs };
      for (const id of before) if (!after.has(id)) delete convs[id];
      const active = st.prefs.active && after.has(st.prefs.active) ? st.prefs.active : incoming.open[0]?.id ?? null;
      return { convs, prefs: { ...st.prefs, active } };
    });
  });
  wire.onExit((id, _code, reason) => {
    patchConv(id, (conv) => ({
      status: conv.status === 'ready' ? 'stopped' : 'failed',
      error: reason || 'stopped',
      setup: undefined,
      timeline: { ...conv.timeline, running: false, thinking: null },
    }));
  });
  wire.onOpened((id, replay, fresh) => {
    if (fresh) {
      patchConv(id, () => ({ status: 'starting', timeline: emptyTimeline() }));
      void get().boot(id);
      return;
    }
    // Re-attached after a reload: rebuild the view from the process's recent messages.
    const { timeline, forge, state } = rebuild(replay);
    // Asks that were closed before the reload must not look open.
    patchConv(id, () => ({ timeline, forge, ...(state ? { state, status: 'ready' as const } : {}) }));
    if (!state) void get().boot(id);
    else {
      void loadHistoryIfEmpty(id);
      void get().refreshGit(id);
      void get().refreshDiff(id);
      void get().refreshSessions();
    }
  });

  const loadHistoryIfEmpty = async (id: string): Promise<void> => {
    const conv = get().convs[id];
    // Empty apart from the core's own "Resumed ..." notice counts as empty.
    if (!conv || conv.timeline.items.some((i) => !(i.kind === 'notice' && i.text.startsWith('Resumed ')))) return;
    try {
      const history = await get().request<SavedHistory>(id, 'history');
      if (!history.messages.length && !history.view?.length) return;
      // The process is still running: whether a turn is, is the replay's to say.
      patchConv(id, (now) => {
        const past = restored(history);
        return { timeline: { ...past.timeline, running: now.timeline.running, thinking: now.timeline.thinking }, forge: past.forge };
      });
    } catch {
      /* nothing saved yet */
    }
  };

  return {
    ready: false,
    online: false,
    prefs: DEFAULT_PREFS,
    meta: {},
    convs: {},
    sessions: [],
    screen: 'chat',
    settingsPane: 'models',
    palette: false,
    forgeSheet: null,
    forgeSheetSection: undefined,
    forgeSheetConv: undefined,
    forgeTranscript: false,
    wizardView: {},
    wizardSkipped: {},
    toast: null,
    routines: [],
    undoStack: [],
    redoStack: [],
    dock: { tab: null },
    quickOpen: false,
    setQuickOpen(open) {
      set({ quickOpen: open });
    },
    split: null,
    setSplit(id) {
      set({ split: id && id !== get().prefs.active ? id : null });
    },

    bottom: false,
    toggleBottom(open) {
      set((s) => ({ bottom: open ?? !s.bottom }));
    },

    cycleLayout() {
      const { dock } = get();
      if (!dock.tab) set({ dock: { ...dock, tab: dock.file ? 'files' : 'changes', full: false } });
      else if (!dock.full) set({ dock: { ...dock, full: true } });
      else set({ dock: { ...dock, tab: null, full: false } });
    },

    openBrowser(url) {
      set((s) => ({ dock: { ...s.dock, tab: 'browser', ...(url ? { url } : {}) } }));
    },

    openDock(tab, file, line) {
      const dock = get().dock;
      if (tab === null || (tab === dock.tab && file === undefined)) set({ dock: { ...dock, tab: null, full: false, keepSidebar: false } });
      else set({ dock: { ...dock, tab, ...(file !== undefined ? { file, line } : {}), keepSidebar: tab === dock.tab ? dock.keepSidebar : false } });
    },


    async refreshGit(id) {
      const cwd = get().convs[id]?.state.cwd ?? get().prefs.open.find((o) => o.id === id)?.cwd;
      if (!cwd) return;
      try {
        const git = (await fetch(`/api/git/status?cwd=${encodeURIComponent(cwd)}`).then((r) => r.json())) as GitStatus;
        patchConv(id, () => ({ git }));
      } catch {
        /* the next turn tries again */
      }
    },

    async gitCall(id, op, body = {}) {
      const cwd = get().convs[id]?.state.cwd ?? get().prefs.open.find((o) => o.id === id)?.cwd ?? '';
      const res = await fetch(`/api/git/${op}`, { method: 'POST', body: JSON.stringify({ cwd, ...body }) });
      const data = (await res.json()) as Record<string, unknown>;
      if (!res.ok) throw Object.assign(new Error(String(data.error ?? 'git_failed')), { code: 'git_failed', detail: String(data.error ?? '') });
      void get().refreshGit(id);
      void get().refreshDiff(id);
      return data as never;
    },

    moveConversation(id, cwd, worktree) {
      const prefs = get().prefs;
      get().setPrefs({ open: prefs.open.map((o) => (o.id === id ? { ...o, cwd, worktree: worktree ?? undefined } : o)) });
      get().restart(id);
    },

    sendWhenReady(id, text) {
      pendingSend.set(id, text);
    },

    async refreshDiff(id) {
      try {
        const result = await get().request<{ events: Array<{ kind: string; text?: string }> }>(id, 'capture', { text: '/diff' });
        const text = result.events.filter((e) => e.kind === 'diff' || e.kind === 'text').map((e) => e.text ?? '').join('\n');
        const isDiff = result.events.some((e) => e.kind === 'diff');
        patchConv(id, () => ({ diff: isDiff ? diffStat(text) : null }));
      } catch {
        /* busy or not started: the next turn tries again */
      }
    },

    async init() {
      if (initStarted) return;
      initStarted = true;
      const [prefs, meta] = await Promise.all([
        fetch('/api/prefs').then((r) => r.json()).catch(() => ({})) as Promise<Partial<Prefs>>,
        fetch('/api/meta').then((r) => r.json()).catch(() => ({})) as Promise<AppState['meta']>,
      ]);
      const merged: Prefs = { ...DEFAULT_PREFS, ...prefs, drafts: { ...(prefs.drafts ?? {}) } };
      applyBindings(merged.keybindings);
      // Before trust existed, every folder in use was already trusted in effect.
      if (!merged.trusted) merged.trusted = [...new Set([...merged.folders, ...merged.open.map((o) => o.worktree?.root ?? o.cwd)].filter(Boolean))];
      applyTheme(merged.theme);
      set({ prefs: merged, meta });
      wire.connect();
      // A popped-out conversation or pane attaches to its one conversation only.
      const solo = (ROLE.kind === 'chat' || ROLE.kind === 'pane') ? merged.open.find((o) => o.id === ROLE.conv) : undefined;
      if (solo) set({ prefs: { ...merged, active: solo.id }, ...(ROLE.kind === 'pane' ? { dock: { tab: (ROLE.pane ?? 'terminal') as DockTab } } : {}) });
      const attach = solo ? [solo] : merged.open;
      const convs: Record<string, Conv> = {};
      for (const open of attach) convs[open.id] = emptyConv(open.id);
      set({ convs });
      for (const open of attach) {
        if (open.session) pendingBoot.set(open.id, { resume: open.session });
        wire.open(open.id, open.cwd);
      }
      if (solo) {
        // A pop-out shows its one conversation; nothing else to set up.
      } else if (!merged.open.length) {
        get().openConversation({ cwd: merged.folders[0] ?? meta.homedir ?? '' });
      } else if (!merged.active || !merged.open.some((o) => o.id === merged.active)) {
        get().setPrefs({ active: merged.open[0].id });
      }
      void get().refreshRoutines();
      set({ ready: true });
    },

    setPrefs(patch) {
      const prefs = { ...get().prefs, ...patch };
      if ('keybindings' in patch) applyBindings(prefs.keybindings);
      set({ prefs });
      if (patch.theme) applyTheme(patch.theme);
      persist(prefs);
    },

    setScreen(screen, pane) {
      set({ screen, ...(pane ? { settingsPane: pane } : {}) });
    },

    setPalette(open) {
      set({ palette: open });
    },

    openForgeSheet(which, section) {
      set({ forgeSheet: which, forgeSheetSection: which === 'plan' ? section : undefined, forgeSheetConv: which ? get().prefs.active ?? undefined : undefined });
    },

    setForgeTranscript(open) {
      set({ forgeTranscript: open });
    },

    setWizardView(convId, view) {
      const next = { ...get().wizardView };
      if (view) next[convId] = view;
      else delete next[convId];
      set({ wizardView: next });
    },

    setWizardSkipped(convId, skipped) {
      const next = { ...get().wizardSkipped };
      if (skipped) next[convId] = true;
      else delete next[convId];
      set({ wizardSkipped: next });
    },

    showToast(text, level = 'info', action) {
      const toast = { text, level, action };
      set({ toast });
      setTimeout(() => {
        if (get().toast === toast) set({ toast: null });
      }, action ? 6_000 : 4_000);
    },

    pushUndo(label, undo, redo) {
      if (replaying) return;
      set((s) => ({ undoStack: [...s.undoStack, { label, undo, redo }].slice(-30), redoStack: [] }));
      get().showToast(label, 'info', { label: 'Undo', run: () => get().undoLast() });
    },

    undoLast() {
      const entry = get().undoStack.at(-1);
      if (!entry) return;
      set((s) => ({ undoStack: s.undoStack.slice(0, -1), redoStack: [...s.redoStack, entry], toast: null }));
      replaying = true;
      try {
        entry.undo();
      } finally {
        replaying = false;
      }
      get().showToast('Undone.');
    },

    redoLast() {
      const entry = get().redoStack.at(-1);
      if (!entry) return;
      set((s) => ({ redoStack: s.redoStack.slice(0, -1), undoStack: [...s.undoStack, entry], toast: null }));
      replaying = true;
      try {
        entry.redo();
      } finally {
        replaying = false;
      }
      get().showToast(entry.label);
    },

    openConversation({ cwd, kind = 'chat', resume, title, activate = true }) {
      // A saved conversation that is already open is just brought forward.
      if (resume) {
        const existing = get().prefs.open.find((o) => o.session === resume);
        if (existing) {
          if (activate) get().activate(existing.id);
          return existing.id;
        }
      }
      const id = newId();
      if (resume) pendingBoot.set(id, { resume });
      set((s) => ({ convs: { ...s.convs, [id]: emptyConv(id) } }));
      const prefs = get().prefs;
      const folders = cwd ? [cwd, ...prefs.folders.filter((f) => f !== cwd)].slice(0, 12) : prefs.folders;
      get().setPrefs({
        open: [...prefs.open, { id, cwd, kind, session: resume, title, wantWorktree: !resume && kind === 'chat' && prefs.worktreeDefault }],
        active: activate ? id : prefs.active,
        folders,
      });
      if (activate) set({ screen: 'chat' });
      wire.open(id, cwd);
      return id;
    },

    activate(id) {
      get().setPrefs({ active: id });
      // Looking at a routine's run counts as having seen it.
      const seen = get().routines.flatMap((r) => r.runs).filter((run) => run.conv === id && !run.seen).map((run) => run.id);
      if (seen.length) void fetch('/api/routines/seen', { method: 'POST', body: JSON.stringify({ runs: seen }) });
      set({ screen: 'chat' });
      patchConv(id, () => ({ unread: false }));
      if (get().convs[id]?.status === 'ready') void get().refreshGit(id);
    },

    archiveConversation(id) {
      const open = get().prefs.open.find((o) => o.id === id);
      const session = get().convs[id]?.state.session_id && get().convs[id]?.state.title ? get().convs[id]!.state.session_id : open?.session;
      const removesWorktree = !!open?.worktree && get().prefs.removeWorktreeOnArchive;
      const wasReplaying = replaying;
      replaying = true;
      try {
        get().closeConversation(id);
        if (session) get().archiveSession(session);
      } finally {
        replaying = wasReplaying;
      }
      if (session && open && !removesWorktree) {
        get().pushUndo('Archived.', () => {
          get().unarchive(session);
          get().openConversation({ cwd: open.cwd, kind: open.kind, resume: session, title: open.title });
        }, () => {
          const again = get().prefs.open.find((o) => o.session === session);
          if (again) get().archiveConversation(again.id);
        });
      }
      if (open?.worktree && get().prefs.removeWorktreeOnArchive) {
        void fetch('/api/git/worktree-remove', { method: 'POST', body: JSON.stringify({ cwd: open.worktree.root, path: open.cwd, force: false }) })
          .then(async (r) => (r.ok ? get().showToast('Archived; its worktree was removed') : get().showToast('Archived. The worktree has uncommitted work, so it was kept.')));
      }
    },

    archiveSession(sessionId) {
      const prefs = get().prefs;
      if (prefs.archived.includes(sessionId)) return;
      const wasPinned = prefs.pinned.includes(sessionId);
      get().setPrefs({ archived: [sessionId, ...prefs.archived], pinned: prefs.pinned.filter((k) => k !== sessionId) });
      get().pushUndo('Archived.', () => {
        get().unarchive(sessionId);
        if (wasPinned) get().togglePin(sessionId);
      }, () => get().archiveSession(sessionId));
    },

    unarchive(sessionId) {
      if (!get().prefs.archived.includes(sessionId)) return;
      get().setPrefs({ archived: get().prefs.archived.filter((k) => k !== sessionId) });
      get().pushUndo('Unarchived.', () => get().archiveSession(sessionId), () => get().unarchive(sessionId));
    },

    togglePin(key) {
      const pinned = get().prefs.pinned;
      const pinning = !pinned.includes(key);
      get().setPrefs({ pinned: pinning ? [key, ...pinned] : pinned.filter((k) => k !== key) });
      get().pushUndo(pinning ? 'Pinned.' : 'Unpinned.', () => get().togglePin(key), () => get().togglePin(key));
    },

    markUnread(id, unread = true) {
      patchConv(id, () => ({ unread }));
    },

    async deleteSession(sessionId) {
      const host = Object.values(get().convs).find((c) => c.status === 'ready' && c.state.session_id !== sessionId);
      if (!host) return false;
      try {
        await get().request(host.id, 'delete_session', { id: sessionId });
      } catch {
        return false;
      }
      get().setPrefs({ archived: get().prefs.archived.filter((k) => k !== sessionId), pinned: get().prefs.pinned.filter((k) => k !== sessionId) });
      await get().refreshSessions();
      return true;
    },

    async pruneWorktrees(cwd) {
      const { prefs, sessions } = get();
      if (!prefs.pruneWorktrees || !cwd) return;
      const pinned = new Set(prefs.pinned);
      const protect = [...prefs.open.map((o) => o.cwd), ...sessions.filter((r) => pinned.has(r.id)).map((r) => r.cwd)].filter(Boolean);
      try {
        const r = await fetch('/api/git/worktree-prune', { method: 'POST', body: JSON.stringify({ cwd, keep: prefs.worktreeLimit, protect }) });
        const out = (await r.json()) as { removed?: string[] };
        if (out.removed?.length) get().showToast(`Removed ${out.removed.length} old ${out.removed.length === 1 ? 'worktree' : 'worktrees'} (keeping the newest ${prefs.worktreeLimit})`);
      } catch {
        /* cleanup waits for the next worktree */
      }
    },

    async refreshRoutines() {
      try {
        const r = (await fetch('/api/routines').then((x) => x.json())) as { routines: Routine[] };
        set({ routines: r.routines });
      } catch {
        /* the next change brings them */
      }
    },

    toggleSide(id, open) {
      patchConv(id, (conv) => {
        const side = conv.side ?? { open: false, items: [] };
        return { side: { ...side, open: open ?? !side.open } };
      });
    },

    async askSide(id, question) {
      const text = question.trim();
      if (!text) return;
      try {
        const { id: sideId } = await get().request<{ id: number }>(id, 'side', { text });
        patchConv(id, (conv) => {
          const side = conv.side ?? { open: true, items: [] };
          // The answer may already be streaming in; keep what arrived.
          const exists = side.items.some((i) => i.id === sideId);
          const items = exists
            ? side.items.map((i) => (i.id === sideId ? { ...i, question: text } : i))
            : [...side.items, { id: sideId, question: text, answer: '', steps: [], done: false }];
          return { side: { open: true, items } };
        });
      } catch (err) {
        get().showToast(errorText((err as { code?: string }).code), 'error');
      }
    },

    cancelSide(id, sideId) {
      void get().request(id, 'side_cancel', { id: sideId }).catch(() => undefined);
    },

    trustFolder(id) {
      const open = get().prefs.open.find((o) => o.id === id);
      if (!open) return;
      const root = open.worktree?.root ?? open.cwd;
      get().setPrefs({ trusted: [...new Set([...(get().prefs.trusted ?? []), root])] });
      patchConv(id, () => ({ status: 'starting' }));
      void get().boot(id);
    },

    async forkSession(sessionId, upto) {
      const { convs, prefs } = get();
      const host = (prefs.active && convs[prefs.active]?.status === 'ready' ? convs[prefs.active] : undefined)
        ?? Object.values(convs).find((c) => c.status === 'ready');
      if (!host) {
        get().showToast('Open a conversation first, then fork.', 'error');
        return;
      }
      try {
        const fork = await get().request<{ id: string; title: string; cwd: string }>(host.id, 'fork_session', { id: sessionId, ...(upto ? { upto } : {}) });
        await get().refreshSessions();
        get().openConversation({ cwd: fork.cwd || host.state.cwd || '', resume: fork.id, title: fork.title });
      } catch (err) {
        get().showToast(err instanceof Error && err.message === 'busy' ? 'Wait for this turn to finish, then fork.' : 'The conversation could not be forked.', 'error');
      }
    },

    closeConversation(id) {
      const closing = get().prefs.open.find((o) => o.id === id);
      const savedAs = get().convs[id]?.state.title ? get().convs[id]?.state.session_id : closing?.session;
      if (closing && savedAs) {
        get().pushUndo('Closed. It is saved under Earlier.', () => {
          get().openConversation({ cwd: closing.cwd, kind: closing.kind, resume: savedAs, title: closing.title });
        }, () => {
          const again = get().prefs.open.find((o) => o.session === savedAs);
          if (again) get().closeConversation(again.id);
        });
      }
      wire.close(id);
      const prefs = get().prefs;
      const open = prefs.open.filter((o) => o.id !== id);
      const drafts = { ...prefs.drafts };
      delete drafts[id];
      const index = prefs.open.findIndex((o) => o.id === id);
      const active = prefs.active === id ? (open[Math.max(0, index - 1)]?.id ?? null) : prefs.active;
      get().setPrefs({ open, drafts, active });
      set((s) => {
        const convs = { ...s.convs };
        delete convs[id];
        return { convs };
      });
      void get().refreshSessions();
    },

    restart(id) {
      const open = get().prefs.open.find((o) => o.id === id);
      if (!open) return;
      if (open.session) pendingBoot.set(id, { resume: open.session });
      patchConv(id, () => ({ status: 'starting', error: undefined, setup: undefined }));
      wire.close(id);
      setTimeout(() => wire.open(id, open.cwd), 300);
    },

    setDraft(id, text) {
      const drafts = { ...get().prefs.drafts };
      if (text) drafts[id] = text;
      else delete drafts[id];
      get().setPrefs({ drafts });
    },

    async boot(id) {
      const open = get().prefs.open.find((o) => o.id === id);
      if (!open) return;
      const prefs = get().prefs;
      const plan = pendingBoot.get(id) ?? {};
      pendingBoot.delete(id);
      // First run: no profile yet means the sign-in screen, not a boot.
      try {
        const onboarding = await get().request<{ profile_exists: boolean }>(id, 'onboarding', {
          ...(prefs.workspace ? { workspace: prefs.workspace } : {}), ...(open.cwd ? { cwd: open.cwd } : {}), probe: false,
        });
        if (!onboarding.profile_exists && !prefs.profile) {
          patchConv(id, () => ({ status: 'setup' }));
          return;
        }
      } catch {
        /* an older bridge without onboarding: boot runs the CLI's own first run */
      }
      // A folder's own instructions and tool servers can run code: ask first.
      if (open.cwd && !isTrusted(open.cwd, get().prefs.trusted ?? [])) {
        pendingBoot.set(id, plan);
        patchConv(id, () => ({ status: 'trust' }));
        return;
      }
      try {
        const booted = await get().request<{ resume_failed?: string }>(id, 'boot', {
          cwd: open.cwd || undefined,
          workspace: prefs.workspace,
          profile: prefs.profile,
          probe: prefs.probe,
          resume: plan.resume,
        });
        if (open.kind === 'forge') await get().request(id, 'set_state', { state: 'forge' });
        if (plan.resume && booted.resume_failed) {
          // The saved conversation is gone (deleted, or saved in another workspace).
          get().setPrefs({ open: get().prefs.open.map((o) => (o.id === id ? { ...o, session: undefined, title: undefined } : o)) });
          get().showToast(`"${open.title ?? 'That conversation'}" could not be found, so this is a new conversation in ${open.cwd.split('\\').join('/').split('/').filter(Boolean).pop()}.`, 'error');
        } else if (plan.resume) {
          patchConv(id, () => ({ resuming: true }));
          const history = await get().request<SavedHistory>(id, 'history');
          // The saved messages go first. While booting, the core only says "Resumed ...",
          // which the history itself makes redundant; anything else that arrived stays after it.
          patchConv(id, (conv) => {
            const past = restored(history);
            const arrived = conv.timeline.items.filter((i) => !(i.kind === 'notice' && i.text.startsWith('Resumed ')));
            if (arrived.some((i) => i.kind === 'user')) return { resuming: false };
            return { resuming: false, timeline: { ...conv.timeline, items: [...past.timeline.items, ...arrived] }, forge: past.forge };
          });
        }
        patchConv(id, () => ({ status: 'ready', resuming: false, error: undefined }));
        const queued = pendingSend.get(id);
        if (queued) {
          pendingSend.delete(id);
          void get().send(id, queued);
        }
        void get().refreshDiff(id);
        void get().refreshGit(id);
        void get().refreshSessions();
      } catch (err) {
        const code = (err as { code?: string }).code ?? 'failed';
        // The folder's own profile runs on Nebius and no key is stored yet (the receipt
        // example opened first): sign in here, then it starts. The sign-in says why.
        const status = code === 'sign_in_first' ? 'setup' : 'failed';
        patchConv(id, () => ({ status, error: code, resuming: false }));
      }
    },

    async send(id, text, quiet = false, steer = false) {
      try {
        await get().request(id, 'send', { text, quiet, ...(steer ? { steer: true } : {}) });
      } catch (err) {
        get().showToast(errorText((err as { code?: string }).code), 'error');
        throw err;
      }
    },

    async cancel(id) {
      await get().request(id, 'cancel').catch(() => undefined);
    },

    async answer(id, askId, value) {
      onMessage(id, { type: 'answered', id: askId, value });
      await get().request(id, 'answer', { id: askId, value }).catch(() => undefined);
    },

    request(id, method, params = {}) {
      return wire.request(id, method, params);
    },

    async refreshSessions() {
      const ready = Object.values(get().convs).find((c) => c.status === 'ready');
      if (!ready) return;
      try {
        const result = await get().request<{ sessions: SessionRow[] }>(ready.id, 'sessions');
        set({ sessions: result.sessions });
      } catch {
        /* keep the last list */
      }
    },
  };
});

/** Plain words for the bridge's error codes. */
export function errorText(code?: string): string {
  switch (code) {
    case 'offline': return 'Lost the connection to Architect. Reconnecting.';
    case 'not_running': case 'stopped': return 'This conversation stopped. Restart it to continue.';
    case 'not_started': return 'This conversation is still starting.';
    case 'busy': return 'Wait for the current turn to finish.';
    case 'empty': return 'Type a message first.';
    case 'no_provider': return 'No model is set up yet. Sign in to a provider in Settings.';
    case 'invalid_profile': return 'The profile file could not be read. Check it in Settings > Advanced.';
    case 'model_not_configured': return 'That model is not in your profile. Pick another one.';
    case 'cwd_missing': return 'That folder no longer exists. Choose another one.';
    case 'setup_cancelled': return 'Setup was cancelled.';
    case 'provider_not_logged_in': return 'You are signed out of that provider. Sign in again in Settings.';
    case 'provider_unavailable': return 'That provider did not answer. Check your connection, then try again.';
    case 'provider_unreachable': return "Architect could not reach the model's address. Check the connection, then try again.";
    case 'sign_in_first': return 'This folder runs on Nebius Token Factory. Sign in with your judge code or a Nebius key first.';
    default: return code ? `That did not work (${code}).` : 'That did not work.';
  }
}
