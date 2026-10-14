/**
 * Terminals in the conversation's folder (xterm.js over the hub's pty frames), in
 * tabs like Codex's: + opens another in the default shell, the arrow beside it picks
 * a shell, Ctrl+K clears. Shells outlive a reload; their tabs are remembered per
 * conversation for the session.
 */
import '@xterm/xterm/css/xterm.css';
import { IconChevronDown, IconEraser, IconPlus, IconTerminal2, IconX } from '@tabler/icons-react';
import { FitAddon } from '@xterm/addon-fit';
import { Terminal } from '@xterm/xterm';
import clsx from 'clsx';
import { useEffect, useRef, useState } from 'react';
import { isMac, shortcutLabel } from '../../lib/shortcuts';
import { useApp, wire, type Conv } from '../../store/app';
import { Menu } from '../ui/Menu';

function theme(): Record<string, string> {
  const css = getComputedStyle(document.documentElement);
  const color = (name: string) => `hsl(${css.getPropertyValue(name).trim()})`;
  return {
    background: color('--bg-well'),
    foreground: color('--text'),
    cursor: color('--accent-400'),
    cursorAccent: color('--bg-well'),
    selectionBackground: 'rgba(53, 156, 139, 0.32)',
    black: '#1d1d20', brightBlack: '#6e6e77',
    red: color('--err'), brightRed: color('--err'),
    green: color('--ok'), brightGreen: color('--ok'),
    yellow: color('--warn'), brightYellow: color('--warn'),
    blue: '#6aa7d8', brightBlue: '#8fc0e6',
    magenta: '#b48ed1', brightMagenta: '#c9a9e0',
    cyan: color('--accent-300'), brightCyan: color('--accent-200'),
    white: color('--text-mute'), brightWhite: color('--ink'),
  };
}

export interface ShellInfo { id: string; label: string }
let shellsCache: Promise<ShellInfo[]> | null = null;
export function shells(): Promise<ShellInfo[]> {
  shellsCache ??= fetch('/api/shells').then((r) => r.json()).then((r: { shells: ShellInfo[] }) => r.shells).catch(() => []);
  return shellsCache;
}

/** Tab names stay short; the menu uses the full names. */
const SHORT: Record<string, string> = { pwsh: 'pwsh', powershell: 'PowerShell', cmd: 'cmd', gitbash: 'bash', wsl: 'WSL' };

interface Tab { id: string; shell?: string; label: string; cwd?: string }

function loadTabs(conv: string): Tab[] {
  try {
    return JSON.parse(localStorage.getItem(`architect-terms:${conv}`) ?? '[]') as Tab[];
  } catch {
    return [];
  }
}
function saveTabs(conv: string, tabs: Tab[]): void {
  try {
    localStorage.setItem(`architect-terms:${conv}`, JSON.stringify(tabs));
  } catch {
    /* storage blocked */
  }
}

export function TerminalPane({ conv, visible }: { conv: Conv; visible: boolean }) {
  const preferred = useApp((s) => s.prefs.terminalShell);
  const [list, setList] = useState<ShellInfo[]>([]);
  const [tabs, setTabs] = useState<Tab[]>(() => loadTabs(conv.id));
  const [active, setActive] = useState<string | null>(() => tabs[0]?.id ?? null);
  const [menu, setMenu] = useState(false);
  const [clear, setClear] = useState<{ id: string; n: number } | null>(null);
  const clearActive = () => active && setClear((c) => ({ id: active, n: (c?.n ?? 0) + 1 }));

  useEffect(() => {
    void shells().then(setList);
  }, []);
  useEffect(() => saveTabs(conv.id, tabs), [conv.id, tabs]);

  const add = (shellId?: string, cwd?: string) => {
    const shell = list.find((s) => s.id === (shellId ?? preferred)) ?? list.find((s) => s.id === 'powershell') ?? list[0];
    const id = `${conv.id}-t${Date.now().toString(36)}`;
    const same = tabs.filter((t) => t.shell === shell?.id).length;
    const label = `${SHORT[shell?.id ?? ''] ?? shell?.label ?? 'Terminal'}${same ? ` ${same + 1}` : ''}`;
    setTabs((prev) => [...prev, { id, shell: shell?.id, label, cwd }]);
    setActive(id);
  };

  // The first terminal opens with the pane.
  useEffect(() => {
    if (visible && tabs.length === 0 && list.length) add();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible, list.length]);

  // "Open in terminal" on a folder.
  const addRef = useRef(add);
  addRef.current = add;
  useEffect(() => {
    const onOpen = (event: Event) => addRef.current(undefined, String((event as CustomEvent).detail ?? ''));
    window.addEventListener('architect:terminal', onOpen);
    return () => window.removeEventListener('architect:terminal', onOpen);
  }, []);

  const close = (id: string) => {
    wire.send({ t: 'pty-stop', id });
    setTabs((prev) => {
      const next = prev.filter((t) => t.id !== id);
      if (active === id) {
        const index = prev.findIndex((t) => t.id === id);
        setActive(next[Math.max(0, index - 1)]?.id ?? null);
      }
      return next;
    });
  };

  const restart = (old: string) => {
    const id = `${conv.id}-t${Date.now().toString(36)}`;
    wire.send({ t: 'pty-stop', id: old });
    setTabs((prev) => prev.map((t) => (t.id === old ? { ...t, id } : t)));
    setActive(id);
  };

  return (
    <div
      className="terminal-pane"
      data-keys-own="k"
      onKeyDownCapture={(e) => {
        // Cmd+K on a Mac, Ctrl+K elsewhere (Ctrl+K stays the shell's own key on a Mac).
        if ((isMac() ? e.metaKey : e.ctrlKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'k') {
          e.preventDefault();
          e.stopPropagation();
          clearActive();
        }
      }}
    >
      <div className="term-tabs" role="tablist" aria-label="Terminals">
        {tabs.map((tab) => (
          <div key={tab.id} className={clsx('term-tab', tab.id === active && 'is-on')} role="presentation">
            <button type="button" role="tab" aria-selected={tab.id === active} className="term-tab-main" onClick={() => setActive(tab.id)} onAuxClick={(e) => e.button === 1 && close(tab.id)}>
              <IconTerminal2 size={13} aria-hidden />
              <span>{tab.label}</span>
            </button>
            <button type="button" className="term-tab-x" aria-label={`Close ${tab.label}`} onClick={() => close(tab.id)}><IconX size={12} /></button>
          </div>
        ))}
        <div className="term-new bar-anchor">
          <button type="button" className="icon-button" aria-label="New terminal" data-tip="New terminal" data-tip-side="down" onClick={() => add()}><IconPlus size={14} /></button>
          {list.length > 1 && (
            <button type="button" className="icon-button term-new-more" aria-label="New terminal with" onClick={() => setMenu(!menu)}><IconChevronDown size={12} /></button>
          )}
          {menu && (
            <Menu
              side="down"
              align="end"
              width={220}
              items={list.map((s) => ({ id: s.id, label: s.label, icon: IconTerminal2, selected: s.id === (preferred ?? 'powershell') }))}
              onClose={() => setMenu(false)}
              onPick={(id) => {
                setMenu(false);
                add(id);
              }}
            />
          )}
        </div>
        <span className="spacer" />
        {active && (
          <button type="button" className="icon-button" aria-label="Clear" data-tip={`Clear  ${shortcutLabel({ key: 'k', mod: true })}`} data-tip-side="down" onClick={clearActive}>
            <IconEraser size={14} />
          </button>
        )}
      </div>
      <div className="term-stack">
        {tabs.map((tab) => (
          <Shell key={tab.id} tab={tab} cwd={tab.cwd || conv.state.cwd} visible={visible && tab.id === active} clears={clear?.id === tab.id ? clear.n : 0} onRestart={() => restart(tab.id)} />
        ))}
        {tabs.length === 0 && list.length > 0 && (
          <div className="pane-empty">
            <IconTerminal2 size={26} stroke={1.5} aria-hidden />
            <button type="button" className="button" onClick={() => add()}><IconPlus size={15} aria-hidden /> New terminal</button>
          </div>
        )}
      </div>
    </div>
  );
}

function Shell({ tab, cwd, visible, clears, onRestart }: { tab: Tab; cwd?: string; visible: boolean; clears: number; onRestart: () => void }) {
  const host = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const [exited, setExited] = useState(false);
  const id = tab.id;

  useEffect(() => {
    if (!host.current) return;
    const term = new Terminal({
      fontFamily: '"JetBrains Mono", ui-monospace, monospace',
      fontSize: 13,
      lineHeight: 1.25,
      cursorBlink: false,
      allowProposedApi: false,
      scrollback: 5000,
      theme: theme(),
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host.current);
    termRef.current = term;
    fitRef.current = fit;
    setExited(false);
    try {
      fit.fit();
    } catch {
      /* not laid out yet */
    }
    const off = wire.onAnyFrame((frame) => {
      if (frame.id !== id) return;
      if (frame.t === 'pty-data' || frame.t === 'pty-replay') term.write(String(frame.data ?? ''));
      else if (frame.t === 'pty-exit') setExited(true);
      else if (frame.t === 'pty-error') term.write(`\r\nThe terminal could not start: ${String(frame.message ?? '')}\r\n`);
    });
    wire.send({ t: 'pty-start', id, cwd, shell: tab.shell, cols: term.cols, rows: term.rows });
    const input = term.onData((data) => wire.send({ t: 'pty-input', id, data }));
    const resize = term.onResize(({ cols, rows }) => wire.send({ t: 'pty-resize', id, cols, rows }));
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => {
      try {
        fit.fit();
      } catch {
        /* hidden */
      }
    });
    observer?.observe(host.current);
    const themeObserver = new MutationObserver(() => {
      term.options.theme = theme();
    });
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    return () => {
      off();
      input.dispose();
      resize.dispose();
      observer?.disconnect();
      themeObserver.disconnect();
      term.dispose();
      termRef.current = null;
    };
    // The shell lives as long as its tab; restarting gives the tab a new id.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  useEffect(() => {
    if (!visible) return;
    requestAnimationFrame(() => {
      try {
        fitRef.current?.fit();
      } catch {
        /* hidden */
      }
      termRef.current?.focus();
    });
  }, [visible]);

  useEffect(() => {
    if (clears) {
      termRef.current?.clear();
      termRef.current?.focus();
    }
  }, [clears]);

  return (
    <div className={clsx('term-shell', !visible && 'is-hidden')}>
      <div ref={host} className="terminal-host" />
      {exited && (
        <div className="terminal-exited">
          <span>The shell ended.</span>
          <button type="button" className="button button-small" onClick={onRestart}>Start again</button>
        </div>
      )}
    </div>
  );
}
