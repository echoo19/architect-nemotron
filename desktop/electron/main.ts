/**
 * Architect desktop: Electron main.
 *
 * Like Hearth's shell, main holds no agent logic. It starts the loopback control
 * server (server/serve.ts), which runs one Architect bridge process per open
 * conversation, and loads the window from it. The preload exposes only native
 * affordances (folder picker, reveal in folder, open external, menu commands).
 */
import { app, BrowserWindow, dialog, ipcMain, Menu, nativeImage, powerSaveBlocker, shell, Tray, type MenuItemConstructorOptions } from 'electron';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { packagedEnvironment } from '../server/bridgeProcess.js';
import { startServer, type Running } from '../server/serve.js';

const DEV_URL = process.env.ARCHITECT_DESKTOP_URL; // e.g. http://localhost:5173 with `npm run dev`
const PORT = Number(process.env.ARCHITECT_DESKTOP_PORT) || 47817;
const BACKGROUND = '#131316'; // Lectern campus dark --bg (240 6% 8%)
const MAC = process.platform === 'darwin';

app.setName('Architect');

/**
 * Where Architect's Python core lives. A packaged app carries it in its resources
 * (see electron-builder.yml); from a checkout it is the repository around desktop/.
 */
function architectRoot(): string {
  if (process.env.ARCHITECT_ROOT) return process.env.ARCHITECT_ROOT;
  const bundled = path.join(process.resourcesPath ?? '', 'architect');
  if (app.isPackaged && fs.existsSync(path.join(bundled, 'pyproject.toml'))) return bundled;
  return path.resolve(__dirname, '..', '..');
}
const ROOT = architectRoot();
const UI_ROOT = path.resolve(__dirname, '..', 'dist');

/**
 * Apps opened from the Finder or the Dock get a bare PATH (/usr/bin:/bin:...), so
 * uv, git, gh and editor commands installed by Homebrew would be missing. Read the
 * login shell's environment once, as Terminal would see it.
 */
function adoptLoginShellPath(): void {
  if (process.platform === 'win32') return;
  const loginShell = process.env.SHELL || (MAC ? '/bin/zsh' : '/bin/bash');
  try {
    const marker = '__ARCHITECT_PATH__';
    const out = execFileSync(loginShell, ['-ilc', `printf '${marker}%s${marker}' "$PATH"`], {
      encoding: 'utf8', timeout: 5000, stdio: ['ignore', 'pipe', 'ignore'], env: { ...process.env, ARCHITECT_PATH_PROBE: '1' },
    });
    const found = out.split(marker)[1];
    if (found) process.env.PATH = [...new Set([...found.split(':'), ...(process.env.PATH ?? '').split(':')])].filter(Boolean).join(':');
  } catch {
    // A slow or broken shell profile: keep what we have, plus the usual places.
  }
  const usual = ['/opt/homebrew/bin', '/usr/local/bin', path.join(process.env.HOME ?? '', '.local', 'bin'), path.join(process.env.HOME ?? '', '.cargo', 'bin')];
  const parts = (process.env.PATH ?? '').split(':');
  process.env.PATH = [...parts, ...usual.filter((p) => !parts.includes(p) && fs.existsSync(p))].join(':');
}

/**
 * A packaged app sets Python up with the uv it ships and runs Pi on this binary as Node
 * (server/bridgeProcess.ts); keep Electron's RunAsNode fuse on. A checkout uses its .venv.
 */
function pythonEnvironment(): NodeJS.ProcessEnv {
  if (!app.isPackaged) return {};
  return packagedEnvironment({
    execPath: process.execPath, resourcesPath: process.resourcesPath, mainDir: __dirname, userData: app.getPath('userData'),
  });
}

let running: Running | null = null;
let win: BrowserWindow | null = null;
let tray: Tray | null = null;

/**
 * Windows and Linux: Architect stays in the system tray when its window closes (as
 * Codex does), so routines keep running; the tray opens it again or quits.
 * macOS keeps it in the Dock and menu bar instead.
 */
function buildTray(): void {
  if (MAC || tray || process.env.ARCHITECT_DESKTOP_CAPTURE || process.env.ARCHITECT_DESKTOP_SMOKE) return;
  // 'better', not the default 'best': at 16 px the default filter rings, and dark pixels hug the white mark.
  const image = nativeImage.createFromPath(path.join(__dirname, 'tray.png')).resize({ width: 16, height: 16, quality: 'better' });
  tray = new Tray(image);
  tray.setToolTip('Architect');
  const show = (command?: string) => {
    if (win) {
      win.show();
      win.focus();
      if (command) win.webContents.send('architect:menu', command);
    } else void createWindow().then(() => command && win?.webContents.send('architect:menu', command));
  };
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Open Architect', click: () => show() },
    { label: 'New Conversation', click: () => show('new-chat') },
    { label: 'Routines', click: () => show('routines') },
    { type: 'separator' },
    { label: 'Quit Architect', click: () => app.quit() },
  ]));
  tray.on('click', () => show());
}

function send(command: string): void {
  if (!win) return;
  if (win.isMinimized()) win.restore();
  win.show();
  win.webContents.send('architect:menu', command);
}

/** The macOS menu bar. Windows and Linux have none: the app's own UI covers it. */
function buildMenu(): void {
  if (!MAC) {
    Menu.setApplicationMenu(null);
    return;
  }
  const item = (label: string, command: string, accelerator?: string): MenuItemConstructorOptions => ({ label, accelerator, click: () => send(command) });
  const template: MenuItemConstructorOptions[] = [
    {
      label: 'Architect',
      submenu: [
        { role: 'about', label: 'About Architect' },
        { type: 'separator' },
        item('Settings…', 'settings', 'Cmd+,'),
        item('Keyboard Shortcuts', 'shortcuts', 'Cmd+/'),
        { type: 'separator' },
        { role: 'services' },
        { type: 'separator' },
        { role: 'hide', label: 'Hide Architect' },
        { role: 'hideOthers' },
        { role: 'unhide' },
        { type: 'separator' },
        { role: 'quit', label: 'Quit Architect' },
      ],
    },
    {
      label: 'File',
      submenu: [
        item('New Conversation', 'new-chat', 'Cmd+N'),
        item('Build an Agent', 'new-forge', 'Cmd+Alt+N'),
        { label: 'New Window', accelerator: 'Cmd+Shift+N', click: () => send('new-window') },
        item('Open Folder…', 'open-folder', 'Cmd+O'),
        { type: 'separator' },
        item('Open File…', 'quick-open', 'Cmd+P'),
        { type: 'separator' },
        item('Close Conversation', 'close-conv', 'Cmd+W'),
      ],
    },
    { role: 'editMenu' },
    {
      label: 'View',
      submenu: [
        item('Search and Commands', 'palette', 'Cmd+K'),
        item('Toggle Sidebar', 'sidebar', 'Cmd+B'),
        item('Changes', 'changes', 'Cmd+Shift+D'),
        item('Terminal', 'terminal', 'Ctrl+`'),
        item('Side Question', 'side-chat', 'Cmd+;'),
        item('Browser', 'browser', 'Cmd+Shift+B'),
        item('Folder Tree', 'file-tree', 'Cmd+Shift+E'),
        { type: 'separator' },
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
        ...(app.isPackaged ? [] : [{ type: 'separator' } as const, { role: 'reload' } as const, { role: 'toggleDevTools' } as const]),
      ],
    },
    {
      label: 'Conversation',
      submenu: [
        item('Next Conversation', 'next-conv', 'Cmd+Shift+]'),
        item('Previous Conversation', 'prev-conv', 'Cmd+Shift+['),
        { type: 'separator' },
        item('Rename', 'rename', 'Cmd+Alt+R'),
        item('Pin', 'pin', 'Cmd+Alt+P'),
        item('Mark as Unread', 'unread', 'Cmd+Shift+U'),
        item('Archive', 'archive', 'Cmd+Shift+A'),
      ],
    },
    { role: 'windowMenu' },
    {
      role: 'help',
      submenu: [
        { label: 'Architect on GitHub', click: () => void shell.openExternal('https://github.com/echoo19/architect-nemotron') },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

async function createWindow(): Promise<void> {
  let url = DEV_URL;
  if (!url) {
    // A steady port keeps the page's origin (and so its local storage) the same
    // from one launch to the next; any free port if another app holds it.
    // On macOS the app outlives its window; a new window reuses the same server.
    const options = { root: ROOT, env: { ...process.env, ...pythonEnvironment() } };
    running ??= await startServer(UI_ROOT, options, PORT).catch(() => startServer(UI_ROOT, options, 0));
    url = running.url;
  }
  win = new BrowserWindow({
    width: 1360,
    height: 880,
    minWidth: 760,
    minHeight: 520,
    title: 'Architect',
    backgroundColor: BACKGROUND,
    autoHideMenuBar: true,
    // No native title bar: the app's header strips drag the window. macOS keeps
    // its traffic lights, centred on the 48px header; Windows and Linux get native
    // controls drawn over the top-right corner in the theme's colours.
    titleBarStyle: MAC ? 'hiddenInset' : 'hidden',
    ...(MAC
      ? { trafficLightPosition: { x: 18, y: 17 } }
      : { titleBarOverlay: { color: BACKGROUND, symbolColor: '#A9A9B1', height: 48 } }),
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      // The Browser pane: pages in <webview>, each sandboxed (see will-attach-webview).
      webviewTag: true,
    },
  });
  // Pages in the Browser pane get no preload, no Node and their own session.
  win.webContents.on('will-attach-webview', (_event, prefs, params) => {
    delete prefs.preload;
    prefs.nodeIntegration = false;
    prefs.contextIsolation = true;
    prefs.sandbox = true;
    params.partition = 'persist:architect-browser';
  });
  // A page that opens a new window opens a new Browser tab instead.
  win.webContents.on('did-attach-webview', (_event, contents) => {
    contents.setWindowOpenHandler(({ url: next }) => {
      win?.webContents.send('architect:browser-open', next);
      return { action: 'deny' };
    });
  });
  win.on('closed', () => {
    win = null;
  });
  win.on('enter-full-screen', () => win?.webContents.send('architect:full-screen', true));
  win.on('leave-full-screen', () => win?.webContents.send('architect:full-screen', false));
  win.webContents.setWindowOpenHandler(({ url: external }) => {
    void shell.openExternal(external);
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (event, target) => {
    if (!target.startsWith(url!)) {
      event.preventDefault();
      void shell.openExternal(target);
    }
  });
  await win.loadURL(url);

  if (process.env.ARCHITECT_DESKTOP_CAPTURE) {
    // Development: run a script in the page (ARCHITECT_DESKTOP_EVAL), then save a
    // screenshot of the real window, title bar and all, and quit.
    await new Promise((r) => setTimeout(r, 2500));
    const script = process.env.ARCHITECT_DESKTOP_EVAL;
    if (script) await win.webContents.executeJavaScript(fs.readFileSync(script, 'utf8')).catch((err: unknown) => console.error(String(err)));
    await new Promise((r) => setTimeout(r, Number(process.env.ARCHITECT_DESKTOP_WAIT) || 1500));
    const image = await win.webContents.capturePage();
    fs.writeFileSync(process.env.ARCHITECT_DESKTOP_CAPTURE, image.toPNG());
    app.exit(0);
    return;
  }

  if (process.env.ARCHITECT_DESKTOP_SMOKE) {
    // Self-test: the server answers through the window's own origin and the page
    // mounted; then quit with a code a script can check.
    try {
      const meta = await win.webContents.executeJavaScript("fetch('/api/meta').then(r => r.json())") as { root?: string; python?: string };
      await new Promise((r) => setTimeout(r, 1500));
      const mounted = await win.webContents.executeJavaScript("!!document.querySelector('.app, .working-region')") as boolean;
      const { ARCHITECT_UV: uv, ARCHITECT_PYTHON_ENV: pythonEnv } = pythonEnvironment();
      console.log(JSON.stringify({ smoke: 'ok', root: meta.root, python: meta.python, mounted, packaged: app.isPackaged, uv, pythonEnv }));
      app.exit(meta.root && mounted ? 0 : 1);
    } catch (err) {
      console.error(JSON.stringify({ smoke: 'failed', error: String(err) }));
      app.exit(1);
    }
  }
}

ipcMain.handle('architect:pick-folder', async (_event, start?: string) => {
  const options: Electron.OpenDialogOptions = {
    properties: ['openDirectory', 'createDirectory'],
    defaultPath: typeof start === 'string' ? start : undefined,
  };
  const result = win ? await dialog.showOpenDialog(win, options) : await dialog.showOpenDialog(options);
  return result.canceled ? null : result.filePaths[0] ?? null;
});
ipcMain.handle('architect:title-bar', (event, dark: boolean) => {
  const target = BrowserWindow.fromWebContents(event.sender);
  if (!target || MAC) return;
  // Lectern campus --bg and --text-mute, dark and light.
  target.setTitleBarOverlay(dark ? { color: '#131316', symbolColor: '#A9A9B1' } : { color: '#FFFFFF', symbolColor: '#62626A' });
});
ipcMain.handle('architect:reveal', (_event, target: string) => {
  if (typeof target === 'string') shell.showItemInFolder(target);
});
ipcMain.handle('architect:open-external', (_event, target: string) => {
  if (typeof target === 'string' && /^https?:\/\//.test(target)) void shell.openExternal(target);
});
ipcMain.handle('architect:open-path', (_event, target: string) => {
  if (typeof target === 'string') void shell.openPath(target);
});
let awake: number | null = null;
ipcMain.handle('architect:keep-awake', (_event, on: boolean) => {
  if (on && awake === null) awake = powerSaveBlocker.start('prevent-app-suspension');
  else if (!on && awake !== null) {
    powerSaveBlocker.stop(awake);
    awake = null;
  }
});
// More windows on the same server: a second app window, or one conversation or pane
// popped out (route in the URL hash). They share conversations through the server.
const extraWindows = new Set<BrowserWindow>();
ipcMain.handle('architect:open-window', (_event, route: string, options?: { onTop?: boolean; width?: number; height?: number }) => {
  if (!win || typeof route !== 'string') return;
  const base = win.webContents.getURL().split('#')[0];
  const child = new BrowserWindow({
    width: options?.width ?? 760,
    height: options?.height ?? 820,
    minWidth: 420,
    minHeight: 360,
    title: 'Architect',
    backgroundColor: BACKGROUND,
    autoHideMenuBar: true,
    alwaysOnTop: options?.onTop === true,
    titleBarStyle: MAC ? 'hiddenInset' : 'hidden',
    ...(MAC
      ? { trafficLightPosition: { x: 18, y: 17 } }
      : { titleBarOverlay: { color: BACKGROUND, symbolColor: '#A9A9B1', height: 48 } }),
    webPreferences: { preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true, nodeIntegration: false, webviewTag: true },
  });
  extraWindows.add(child);
  child.on('closed', () => extraWindows.delete(child));
  child.webContents.setWindowOpenHandler(({ url: external }) => {
    void shell.openExternal(external);
    return { action: 'deny' };
  });
  void child.loadURL(`${base}#${route}`);
});
ipcMain.handle('architect:always-on-top', (event, on: boolean) => {
  BrowserWindow.fromWebContents(event.sender)?.setAlwaysOnTop(!!on, 'floating');
  return !!on;
});
ipcMain.handle('architect:toggle-full-screen', () => {
  if (win) win.setFullScreen(!win.isFullScreen());
});
ipcMain.handle('architect:version', () => ({
  app: app.getVersion(), electron: process.versions.electron, chrome: process.versions.chrome, node: process.versions.node,
}));

// One window: a second launch focuses the first.
if (!process.env.ARCHITECT_DESKTOP_CAPTURE && !process.env.ARCHITECT_DESKTOP_SMOKE && !app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (!win) return;
    if (win.isMinimized()) win.restore();
    win.focus();
  });

  app.whenReady().then(async () => {
    adoptLoginShellPath();
    app.setAboutPanelOptions({
      applicationName: 'Architect',
      applicationVersion: app.getVersion(),
      copyright: 'Copyright (c) 2026 echoo19 and Architect contributors',
    });
    buildMenu();
    buildTray();
    await createWindow();
    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) void createWindow();
    });
  }).catch((err: unknown) => {
    dialog.showErrorBox('Architect could not start', String((err as Error)?.message ?? err));
    app.quit();
  });
}

let quitting = false;
app.on('before-quit', (event) => {
  if (quitting || !running) return;
  // Conversations are saved by their bridge processes as they stop.
  event.preventDefault();
  quitting = true;
  void running.close().finally(() => app.quit());
});

app.on('window-all-closed', () => {
  // macOS apps stay open with no window; the Dock icon brings one back. Elsewhere the
  // tray keeps Architect running (routines go on); without a tray, quit.
  if (!MAC && !tray) app.quit();
});
