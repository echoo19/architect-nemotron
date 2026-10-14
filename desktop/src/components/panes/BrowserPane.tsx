/**
 * The Browser pane (Claude Code's browser pane, Codex's built-in browser): tabs, an
 * address bar that also searches, back / forward / reload, and the local dev servers
 * it finds, so you can see what Architect is building. In the app each page is a
 * sandboxed <webview>; in a plain browser it falls back to an iframe.
 */
import {
  IconArrowLeft, IconArrowRight, IconExternalLink, IconPlus, IconRefresh, IconServer, IconWorld, IconX,
} from '@tabler/icons-react';
import clsx from 'clsx';
import { createElement, useCallback, useEffect, useRef, useState } from 'react';
import { native, openLink } from '../../lib/native';
import { useApp, type Conv } from '../../store/app';
import { WorkingMark } from '../ui/Working';

interface Tab { id: string; url: string; title: string; loading: boolean; back: boolean; forward: boolean }
interface WebviewElement extends HTMLElement {
  loadURL(url: string): Promise<void>;
  goBack(): void;
  goForward(): void;
  reload(): void;
  stop(): void;
  canGoBack(): boolean;
  canGoForward(): boolean;
  getURL(): string;
}

const sessions = new Map<string, { tabs: Tab[]; active: string | null }>();
const newTab = (url = ''): Tab => ({ id: Math.random().toString(36).slice(2, 9), url, title: '', loading: false, back: false, forward: false });

/** What was typed, as an address: a URL, a host (localhost gets http), or a web search. */
export function toAddress(input: string): string {
  const text = input.trim();
  if (!text) return '';
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(text)) return text;
  if (/^(localhost|127\.0\.0\.1|\[::1\]|0\.0\.0\.0)(:\d+)?(\/.*)?$/i.test(text)) return `http://${text}`;
  if (/^:\d+(\/.*)?$/.test(text)) return `http://localhost${text}`;
  if (/^[\w-]+(\.[\w-]+)+(:\d+)?(\/\S*)?$/.test(text) && !/\s/.test(text)) return `https://${text}`;
  return `https://www.google.com/search?q=${encodeURIComponent(text)}`;
}

export function BrowserPane({ conv, visible }: { conv: Conv; visible: boolean }) {
  const pending = useApp((s) => s.dock.url);
  const [state, setState] = useState(() => sessions.get(conv.id) ?? { tabs: [], active: null });
  const update = useCallback((next: (prev: typeof state) => typeof state) => {
    setState((prev) => {
      const value = next(prev);
      sessions.set(conv.id, value);
      return value;
    });
  }, [conv.id]);

  const open = useCallback((url = '') => {
    const tab = newTab(url);
    update((prev) => ({ tabs: [...prev.tabs, tab], active: tab.id }));
  }, [update]);
  const patch = useCallback((id: string, values: Partial<Tab>) => {
    update((prev) => ({ ...prev, tabs: prev.tabs.map((t) => (t.id === id ? { ...t, ...values } : t)) }));
  }, [update]);
  const close = (id: string) => update((prev) => {
    const tabs = prev.tabs.filter((t) => t.id !== id);
    const index = prev.tabs.findIndex((t) => t.id === id);
    return { tabs, active: prev.active === id ? tabs[Math.max(0, index - 1)]?.id ?? null : prev.active };
  });

  // An address sent here (a localhost link in an answer, a server from the list),
  // taken once even if the effect runs twice.
  const taken = useRef<string | null>(null);
  useEffect(() => {
    if (!pending || taken.current === pending) return;
    taken.current = pending;
    useApp.setState((s) => ({ dock: { ...s.dock, url: undefined } }));
    // An empty tab is reused rather than left beside the new one.
    const blank = (sessions.get(conv.id) ?? state).tabs.find((t) => !t.url);
    if (blank) update((prev) => ({ tabs: prev.tabs.map((t) => (t.id === blank.id ? { ...t, url: pending } : t)), active: blank.id }));
    else open(pending);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pending]);
  // The pane opens with one empty tab, once.
  const seeded = useRef(false);
  useEffect(() => {
    if (!visible || seeded.current) return;
    seeded.current = true;
    // Not when an address is arriving in this same render: it gets its own tab.
    if (state.tabs.length === 0 && !pending && !taken.current) open();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible]);
  useEffect(() => native()?.onBrowserOpen?.((url) => open(url)), [open]);

  const active = state.tabs.find((t) => t.id === state.active) ?? null;

  return (
    <div className="browser-pane" data-keys-own="l t r">
      <div className="term-tabs browser-tabs" role="tablist" aria-label="Browser tabs">
        {state.tabs.map((tab) => (
          <div key={tab.id} className={clsx('term-tab', tab.id === state.active && 'is-on')}>
            <button type="button" role="tab" aria-selected={tab.id === state.active} className="term-tab-main" onClick={() => update((p) => ({ ...p, active: tab.id }))} title={tab.url}>
              {tab.loading ? <WorkingMark className="working-xs" /> : <IconWorld size={13} aria-hidden />}
              <span>{tab.title || hostOf(tab.url) || 'New tab'}</span>
            </button>
            <button type="button" className="term-tab-x" aria-label="Close tab" onClick={() => close(tab.id)}><IconX size={12} /></button>
          </div>
        ))}
        <button type="button" className="icon-button" aria-label="New tab" data-tip="New tab" data-tip-side="down" onClick={() => open()}><IconPlus size={14} /></button>
      </div>
      {state.tabs.map((tab) => (
        <BrowserTab key={tab.id} tab={tab} shown={tab.id === state.active} onChange={(v) => patch(tab.id, v)} onOpen={(url) => open(url)} />
      ))}
      {!active && <div className="pane-empty"><IconWorld size={26} stroke={1.5} aria-hidden /></div>}
    </div>
  );
}

function BrowserTab({ tab, shown, onChange, onOpen }: { tab: Tab; shown: boolean; onChange: (v: Partial<Tab>) => void; onOpen: (url: string) => void }) {
  const view = useRef<WebviewElement | null>(null);
  const frame = useRef<HTMLIFrameElement | null>(null);
  const address = useRef<HTMLInputElement>(null);
  const [typed, setTyped] = useState(tab.url);
  const electron = !!native();

  useEffect(() => setTyped(tab.url), [tab.url]);
  useEffect(() => {
    if (shown && !tab.url) address.current?.focus();
  }, [shown, tab.url]);

  // Follow the page: address, title, loading, history.
  useEffect(() => {
    const el = view.current;
    if (!el) return;
    const sync = () => onChange({ url: el.getURL(), back: el.canGoBack(), forward: el.canGoForward() });
    const listeners: Array<[string, (e: Event) => void]> = [
      ['did-start-loading', () => onChange({ loading: true })],
      ['did-stop-loading', () => { onChange({ loading: false }); sync(); }],
      ['did-navigate', sync],
      ['did-navigate-in-page', sync],
      ['page-title-updated', (e) => onChange({ title: (e as unknown as { title: string }).title })],
    ];
    for (const [name, fn] of listeners) el.addEventListener(name, fn);
    return () => {
      for (const [name, fn] of listeners) el.removeEventListener(name, fn);
    };
    // The listeners belong to this element for its whole life.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [!!tab.url]);

  const go = (input: string) => {
    const url = toAddress(input);
    if (!url) return;
    if (view.current && tab.url) void view.current.loadURL(url).catch(() => undefined);
    onChange({ url, title: '', loading: !!electron });
  };

  return (
    <div className={clsx('browser-tab', !shown && 'is-hidden')}>
      <form className="browser-bar" onSubmit={(e) => { e.preventDefault(); go(typed); }}>
        <button type="button" className="icon-button" aria-label="Back" disabled={!tab.back} onClick={() => view.current?.goBack()}><IconArrowLeft size={15} /></button>
        <button type="button" className="icon-button" aria-label="Forward" disabled={!tab.forward} onClick={() => view.current?.goForward()}><IconArrowRight size={15} /></button>
        <button type="button" className="icon-button" aria-label="Reload" disabled={!tab.url} onClick={() => (view.current ? view.current.reload() : frame.current && (frame.current.src = tab.url))}><IconRefresh size={15} /></button>
        <input
          ref={address}
          className="browser-address mono"
          value={typed}
          placeholder="Search or type an address, like localhost:5173"
          spellCheck={false}
          onChange={(e) => setTyped(e.target.value)}
          onFocus={(e) => e.currentTarget.select()}
          onKeyDown={(e) => {
            if (e.key === 'Escape') {
              setTyped(tab.url);
              e.currentTarget.blur();
            }
          }}
        />
        <button type="button" className="icon-button" aria-label="Open in your browser" data-tip="Open in your browser" data-tip-side="down" disabled={!tab.url} onClick={() => openLink(tab.url)}><IconExternalLink size={15} /></button>
      </form>
      {tab.url ? (
        electron
          ? createElement('webview', { ref: view, src: tab.url, className: 'browser-view', partition: 'persist:architect-browser', allowpopups: 'true' })
          : <iframe ref={frame} className="browser-view" title={tab.title || tab.url} src={tab.url} sandbox="allow-scripts allow-forms allow-same-origin allow-popups" />
      ) : (
        <LocalServers onOpen={(url) => go(url)} onNewTab={onOpen} />
      )}
    </div>
  );
}

/** Dev servers answering on this computer, one click to preview. */
function LocalServers({ onOpen }: { onOpen: (url: string) => void; onNewTab: (url: string) => void }) {
  const [ports, setPorts] = useState<number[] | null>(null);
  const load = useCallback(() => {
    setPorts(null);
    void fetch('/api/ports').then((r) => r.json()).then((r: { ports: number[] }) => setPorts(r.ports)).catch(() => setPorts([]));
  }, []);
  useEffect(load, [load]);
  return (
    <div className="browser-start">
      <div className="browser-start-head">
        <IconServer size={15} aria-hidden />
        <span>Running on this computer</span>
        <button type="button" className="icon-button" aria-label="Look again" data-tip="Look again" onClick={load}><IconRefresh size={14} /></button>
      </div>
      {ports === null ? (
        <div className="browser-start-empty"><WorkingMark className="working-xs" /></div>
      ) : ports.length === 0 ? (
        <p className="browser-start-empty">No local servers found. Start one in the terminal, then look again.</p>
      ) : (
        <div className="browser-servers">
          {ports.map((port) => (
            <button key={port} type="button" className="browser-server" onClick={() => onOpen(`http://localhost:${port}`)}>
              <IconWorld size={16} stroke={1.6} aria-hidden />
              <span className="mono">localhost:{port}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return '';
  }
}
