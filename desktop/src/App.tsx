import { IconArrowsLeftRight, IconLayoutSidebarLeftExpand, IconX } from '@tabler/icons-react';
import { useEffect, useRef } from 'react';
import { native } from './lib/native';
import { trackNavigation } from './lib/navigation';
import { shortcutLabel, SHORTCUTS } from './lib/shortcuts';
import { AgentsScreen } from './components/agents/AgentsScreen';
import { ConversationView } from './components/chat/ConversationView';
import { ForgeWorkspace, isBuildPage, useSidebarShown } from './components/forge/ForgeWorkspace';
import { useForgeSidebar } from './components/forge/wizard/TopBar';
import { SettingsScreen } from './components/settings/SettingsScreen';
import { RoutinesScreen } from './components/routines/RoutinesScreen';
import { FolderPicker } from './components/shell/FolderPicker';
import { QuickOpen } from './components/panes/QuickOpen';
import { ErrorBoundary } from './components/ui/ErrorBoundary';
import { Dock } from './components/panes/Dock';
import { Palette } from './components/shell/Palette';
import { restoreZoom, Shortcuts } from './components/shell/Shortcuts';
import { Sidebar } from './components/shell/Sidebar';
import { Button } from './components/ui/Button';
import { Mark, WorkingRegion } from './components/ui/Working';
import { ROLE, showSidebar, useApp, type Conv } from './store/app';

export function App() {
  const ready = useApp((s) => s.ready);
  const online = useApp((s) => s.online);
  const init = useApp((s) => s.init);
  const screen = useApp((s) => s.screen);
  const active = useApp((s) => s.prefs.active);
  const open = useApp((s) => s.prefs.open);
  const conv = useApp((s) => (active ? s.convs[active] : undefined));
  const toast = useApp((s) => s.toast);
  const splitConv = useApp((s) => (s.split ? s.convs[s.split] : undefined));
  const theme = useApp((s) => s.prefs.theme);
  // The sidebar steps aside while Harness sits beside a build (store/app.ts `sidebarYields`),
  // and on a build page until the person brings it back in this session (clarity spec §2.1).
  const activeKind = open.find((o) => o.id === active)?.kind;
  const buildPage = screen === 'chat' && !!conv && isBuildPage(conv, activeKind);
  const sidebar = useSidebarShown(buildPage);
  useForgeSidebarToggle(buildPage);

  // The window's own controls follow the theme (Electron titleBarOverlay).
  useEffect(() => {
    const n = native() as unknown as { setTitleBar?: (dark: boolean) => void; platform?: string } | undefined;
    const root = document.documentElement;
    if (n) root.classList.add('is-electron', n.platform === 'darwin' ? 'is-mac' : 'is-win');
    const sync = () => n?.setTitleBar?.(root.dataset.theme === 'dark');
    sync();
    const observer = new MutationObserver(sync);
    observer.observe(root, { attributes: true, attributeFilter: ['data-theme'] });
    // Full screen on macOS hides the traffic lights; the header takes their room back.
    const offFull = (n as unknown as { onFullScreen?: (cb: (on: boolean) => void) => () => void } | undefined)?.onFullScreen?.((on) => root.classList.toggle('is-fullscreen', on));
    return () => {
      observer.disconnect();
      offFull?.();
    };
  }, []);

  useEffect(() => {
    void init();
  }, [init]);

  useEffect(restoreZoom, []);
  useEffect(() => (ready ? trackNavigation() : undefined), [ready]);

  // Keep the computer awake while any conversation is working (Settings > General).
  const working = useApp((s) => s.prefs.preventSleep && Object.values(s.convs).some((c) => c.timeline.running));
  useEffect(() => {
    void native()?.keepAwake?.(working);
  }, [working]);

  // Edits made outside the app show up when you come back to it.
  useEffect(() => {
    const onFocus = () => {
      const { prefs, convs, refreshGit, refreshDiff } = useApp.getState();
      if (!prefs.active || convs[prefs.active]?.status !== 'ready') return;
      void refreshGit(prefs.active);
      void refreshDiff(prefs.active);
    };
    window.addEventListener('focus', onFocus);
    return () => window.removeEventListener('focus', onFocus);
  }, []);

  // Follow the system theme while the setting says "system".
  useEffect(() => {
    if (theme !== 'system') return;
    const query = window.matchMedia('(prefers-color-scheme: light)');
    const onChange = () => useApp.getState().setPrefs({ theme: 'system' });
    query.addEventListener('change', onChange);
    return () => query.removeEventListener('change', onChange);
  }, [theme]);

  if (!ready) return <WorkingRegion label="Opening Architect" className="app-loading" />;

  const kind = open.find((o) => o.id === active)?.kind ?? 'chat';

  // A popped-out conversation or pane: just that, filling its own window.
  if (ROLE.kind === 'chat' || ROLE.kind === 'pane') {
    return (
      <div className={`app is-popout is-popout-${ROLE.kind}`}>
        <main className="main no-sidebar">
          <ErrorBoundary label={ROLE.kind === 'chat' ? 'This conversation' : 'This pane'} resetKey={active}>
            {!conv ? (
              <div className="conversation-fill empty"><p className="tone-faint">This conversation was closed.</p></div>
            ) : ROLE.kind === 'pane' ? (
              <Dock conv={conv} full />
            ) : kind === 'forge' || conv.state.state === 'forge' ? <ForgeWorkspace conv={conv} /> : <ConversationView conv={conv} />}
          </ErrorBoundary>
        </main>
        <Shortcuts />
        {toast && (
          <div className={`toast toast-${toast.level}`} role="status"><span>{toast.text}</span></div>
        )}
      </div>
    );
  }

  return (
    <div className="app">
      {sidebar && <Sidebar />}
      <main className={sidebar ? "main" : "main no-sidebar"}>
        {!sidebar && !buildPage && (
          <button type="button" className="icon-button sidebar-show" aria-label="Show the sidebar" data-tip={`Show sidebar  ${shortcutLabel(SHORTCUTS.sidebar)}`} data-tip-side="down" onClick={showSidebar}>
            <IconLayoutSidebarLeftExpand size={17} stroke={1.7} />
          </button>
        )}
        {!online && (
          <div className="banner banner-warn" role="status">
            Lost the connection to Architect's background process. Reconnecting; nothing you typed is lost.
          </div>
        )}
        <ErrorBoundary label={screen === 'chat' ? 'This conversation' : screen === 'settings' ? 'Settings' : screen === 'routines' ? 'Routines' : 'Your agents'} resetKey={`${screen}:${active}`}>
          {screen === 'agents' ? (
            <AgentsScreen />
          ) : screen === 'routines' ? (
            <RoutinesScreen />
          ) : screen === 'settings' ? (
            <SettingsScreen />
          ) : conv ? (
            kind === 'forge' || conv.state.state === 'forge' ? <ForgeWorkspace conv={conv} /> : (
              <div className="split-row">
                <ConversationView conv={conv} />
                {splitConv && splitConv.id !== conv.id && <SplitPane conv={splitConv} />}
              </div>
            )
          ) : (
            <NoConversation />
          )}
        </ErrorBoundary>
      </main>
      <Shortcuts />
      <Palette />
      <QuickOpen />
      <FolderPicker />
      {toast && (
        <div className={`toast toast-${toast.level}`} role="status">
          <span>{toast.text}</span>
          {toast.action && (
            <button type="button" className="toast-action" onClick={() => toast.action?.run()}>
              {toast.action.label}
              {toast.action.label === 'Undo' && <kbd>{shortcutLabel(SHORTCUTS.undo)}</kbd>}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * On a build page the sidebar is hidden while the preferences still say shown, so the
 * first Mod+B there (or the sidebar search) brings it back for the session, rather than
 * flipping a preference nobody can see.
 */
function useForgeSidebarToggle(buildPage: boolean) {
  const onBuild = useRef(buildPage);
  onBuild.current = buildPage;
  useEffect(() => {
    const bringBack = () => {
      useForgeSidebar.setState({ on: true });
      if (!useApp.getState().prefs.sidebar) useApp.getState().setPrefs({ sidebar: true });
    };
    const offStore = useApp.subscribe((s, prev) => {
      if (s.prefs.sidebar === prev.prefs.sidebar || !onBuild.current || useForgeSidebar.getState().on) return;
      bringBack();
    });
    const onSearch = () => {
      if (!onBuild.current || useForgeSidebar.getState().on) return;
      bringBack();
      // The sidebar mounts now; give it the search it missed.
      requestAnimationFrame(() => window.dispatchEvent(new Event('architect:search-sidebar')));
    };
    window.addEventListener('architect:search-sidebar', onSearch);
    return () => {
      offStore();
      window.removeEventListener('architect:search-sidebar', onSearch);
    };
  }, []);
}

function NoConversation() {
  const openConversation = useApp((s) => s.openConversation);
  const folders = useApp((s) => s.prefs.folders);
  const homedir = useApp((s) => s.meta.homedir);
  return (
    <div className="conversation-fill empty no-conversation">
      <Mark className="no-conversation-mark text-accent" />
      <h1>No conversation open</h1>
      <p>Start one, or pick an earlier one on the left.</p>
      <div className="button-row">
        <Button variant="primary" size="sm" onClick={() => openConversation({ cwd: folders[0] ?? homedir ?? '', kind: 'forge' })}>Build an agent</Button>
        <Button size="sm" onClick={() => openConversation({ cwd: folders[0] ?? homedir ?? '' })}>New conversation</Button>
      </div>
    </div>
  );
}

/** The second conversation in split view (Ctrl/Cmd-click in the sidebar). */
function SplitPane({ conv }: { conv: Conv }) {
  const setSplit = useApp((s) => s.setSplit);
  const activate = useApp((s) => s.activate);
  return (
    <div className="split-pane">
      <div className="split-tools">
        <button type="button" className="icon-button" aria-label="Make this the main conversation" data-tip="Make this the main one" data-tip-side="down" onClick={() => { const id = conv.id; setSplit(null); activate(id); }}>
          <IconArrowsLeftRight size={15} />
        </button>
        <button type="button" className="icon-button" aria-label="Close split view" data-tip="Close split view" data-tip-side="down" onClick={() => setSplit(null)}>
          <IconX size={15} />
        </button>
      </div>
      <ConversationView conv={conv} compact />
    </div>
  );
}
