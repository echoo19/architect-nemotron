/**
 * The Files pane: the folder tree (Ctrl+Shift+E) beside a file you can read and
 * spot-edit, as in Claude Code's file pane. Edits are kept as a draft until saved,
 * so switching files or reloading loses nothing; Save never writes over a newer
 * version on disk without asking. Ctrl+G or Ctrl+L goes to a line. Images and PDFs
 * preview; Markdown can be read rendered.
 */
import {
  IconArrowBackUp, IconCopy, IconLayoutSidebar, IconDeviceFloppy, IconEye, IconFileCode, IconSearch,
} from '@tabler/icons-react';
import clsx from 'clsx';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { highlight, languageFor, languageName } from '../../lib/highlight';
import { isMac, shortcutLabel, SHORTCUTS } from '../../lib/shortcuts';
import { useCopy } from '../../lib/useCopy';
import { useApp, type Conv } from '../../store/app';
import { Markdown } from '../chat/Markdown';
import { useConfirm } from '../ui/useConfirm';
import { WorkingRegion } from '../ui/Working';
import { FileTree } from './FileTree';
import { OpenIn } from './OpenIn';

export function resolvePath(cwd: string | undefined, file: string): string {
  if (/^([a-zA-Z]:[\\/]|\/)/.test(file) || !cwd) return file;
  return `${cwd.replace(/[\\/]$/, '')}/${file}`;
}

const LINE = 20;
const HIGHLIGHT_LIMIT = 300_000;
const IMAGE = /\.(png|jpe?g|gif|webp|svg|ico|bmp)$/i;
const PDF = /\.pdf$/i;
const MARKDOWN = /\.(md|markdown)$/i;

const draftKey = (path: string) => `architect-file:${path}`;
function readDraft(path: string): string | null {
  try {
    return localStorage.getItem(draftKey(path));
  } catch {
    return null;
  }
}
function writeDraft(path: string, text: string | null): void {
  try {
    if (text === null) localStorage.removeItem(draftKey(path));
    else localStorage.setItem(draftKey(path), text);
  } catch {
    /* storage full or blocked: the draft still lives in the pane */
  }
}

export function FileViewer({ conv, path, line }: { conv: Conv; path?: string; line?: number }) {
  const setQuickOpen = useApp((s) => s.setQuickOpen);
  const openDock = useApp((s) => s.openDock);
  const showTree = useApp((s) => s.prefs.fileTree);
  const setPrefs = useApp((s) => s.setPrefs);
  const cwd = conv.state.cwd ?? '';
  const full = path ? resolvePath(cwd, path) : undefined;
  const rel = path && cwd && full?.startsWith(cwd) ? full.slice(cwd.length).replace(/^[\\/]/, '').replace(/\\/g, '/') : path;

  return (
    <div className={clsx('files-pane', showTree && 'with-tree')}>
      {showTree && cwd && <FileTree cwd={cwd} current={rel} onOpen={(p) => openDock('files', p)} />}
      <div className="files-main">
        {full && path ? (
          <FileEditor key={full} full={full} path={path} line={line} onToggleTree={() => setPrefs({ fileTree: !showTree })} treeOn={showTree} />
        ) : (
          <div className="pane-empty">
            <IconFileCode size={26} stroke={1.5} aria-hidden />
            <button type="button" className="button" onClick={() => setQuickOpen(true)}>
              <IconSearch size={15} aria-hidden /> Open a file <kbd>{shortcutLabel(SHORTCUTS.quickOpen)}</kbd>
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

/** Text is kept with \n line ends (a textarea's own form); `crlf` restores them on save. */
interface Disk { text: string; mtime: number; crlf: boolean }
function fromWire(d: { text: string; mtime: number }): Disk {
  return { text: d.text.replace(/\r\n/g, '\n'), mtime: d.mtime, crlf: /\r\n/.test(d.text) };
}

function FileEditor({ full, path, line, treeOn, onToggleTree }: { full: string; path: string; line?: number; treeOn: boolean; onToggleTree: () => void }) {
  const { copied, copy } = useCopy();
  const showToast = useApp((s) => s.showToast);
  const [disk, setDisk] = useState<Disk | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraftState] = useState<string | null>(null);
  const [confirm, confirmDialog] = useConfirm();
  const [conflict, setConflict] = useState<Disk | null>(null);
  const [force, setForce] = useState(false);
  const [saving, setSaving] = useState(false);
  const [goto, setGoto] = useState(false);
  const [mark, setMark] = useState<number | null>(null);
  const [preview, setPreview] = useState(() => MARKDOWN.test(full));
  const scroller = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const kind = IMAGE.test(full) ? 'image' : PDF.test(full) ? 'pdf' : 'text';

  const setDraft = useCallback((text: string | null) => {
    setDraftState(text);
    writeDraft(full, text);
  }, [full]);

  const load = useCallback(async (): Promise<Disk | null> => {
    const r = await fetch(`/api/file?path=${encodeURIComponent(full)}`);
    if (!r.ok) {
      setError(r.status === 413 ? 'This file is too large to show.' : r.status === 404 ? 'This file no longer exists.' : 'This file could not be opened.');
      return null;
    }
    const data = fromWire((await r.json()) as { text: string; mtime: number });
    setDisk(data);
    setError(null);
    return data;
  }, [full]);

  useEffect(() => {
    if (kind !== 'text') return;
    void load().then((d) => {
      const saved = readDraft(full);
      if (d && saved !== null && saved !== d.text) setDraftState(saved);
      else if (saved !== null) writeDraft(full, null);
    }).catch(() => setError('This file could not be opened.'));
  }, [full, kind, load]);

  // Notice edits made on disk: reload quietly when nothing is unsaved, else ask.
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const diskRef = useRef(disk);
  diskRef.current = disk;
  useEffect(() => {
    if (kind !== 'text') return;
    const check = async () => {
      const known = diskRef.current;
      if (!known || document.hidden) return;
      const r = await fetch(`/api/file/stat?path=${encodeURIComponent(full)}`).catch(() => null);
      if (!r?.ok) return;
      const { mtime } = (await r.json()) as { mtime: number };
      if (Math.abs(mtime - known.mtime) <= 1) return;
      if (draftRef.current === null) void load();
      else {
        const fresh = await fetch(`/api/file?path=${encodeURIComponent(full)}`).then((x) => x.json() as Promise<{ text: string; mtime: number }>);
        setConflict(fromWire(fresh));
      }
    };
    const timer = setInterval(() => void check(), 3000);
    window.addEventListener('focus', check);
    return () => {
      clearInterval(timer);
      window.removeEventListener('focus', check);
    };
  }, [full, kind, load]);

  const text = draft ?? disk?.text ?? '';
  const dirty = draft !== null && draft !== disk?.text;
  const crlf = disk?.crlf ?? false;
  const lines = useMemo(() => text.split('\n'), [text]);
  const html = useMemo(() => {
    if (!disk) return '';
    const body = text.endsWith('\n') ? `${text} ` : text;
    return (text.length > HIGHLIGHT_LIMIT ? null : highlight(body, languageFor(full))) ?? escape(body);
  }, [text, disk, full]);
  const widest = useMemo(() => lines.reduce((m, l) => Math.max(m, l.length), 0), [lines]);

  const save = useCallback(async (overwrite = force) => {
    if (draft === null || !disk || saving) return;
    setSaving(true);
    try {
      const r = await fetch('/api/file', { method: 'PUT', body: JSON.stringify({ path: full, text: disk.crlf ? draft.replace(/\n/g, '\r\n') : draft, mtime: disk.mtime, force: overwrite }) });
      const data = (await r.json()) as { mtime?: number; text?: string; error?: string };
      if (r.status === 409) {
        setConflict(fromWire({ text: data.text ?? '', mtime: data.mtime ?? 0 }));
        return;
      }
      if (!r.ok) throw new Error(data.error);
      setDisk({ text: draft, mtime: data.mtime ?? Date.now(), crlf: disk.crlf });
      setDraft(null);
      setConflict(null);
      setForce(false);
    } catch {
      showToast('The file could not be saved.', 'error');
    } finally {
      setSaving(false);
    }
  }, [draft, disk, full, force, saving, setDraft, showToast]);

  const discard = async () => {
    const ok = await confirm({ title: `Discard your edits to ${basename(path)}?`, keep: 'Keep editing', action: 'Discard' });
    if (ok) {
      setDraft(null);
      setConflict(null);
      setForce(false);
    }
  };

  const jump = useCallback((n: number) => {
    const target = Math.max(1, Math.min(n, lines.length));
    setMark(target);
    const el = input.current;
    if (el) {
      const offset = lines.slice(0, target - 1).reduce((sum, l) => sum + l.length + 1, 0);
      el.focus({ preventScroll: true });
      el.setSelectionRange(offset, offset);
    }
    scroller.current?.scrollTo({ top: Math.max(0, (target - 1) * LINE - (scroller.current.clientHeight / 3)) });
  }, [lines]);

  // A line from a link (path:line) once the text is in.
  useEffect(() => {
    if (line && disk) requestAnimationFrame(() => jump(line));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [line, !!disk]);

  const onKeyDown = (event: React.KeyboardEvent) => {
    const mod = isMac() ? event.metaKey : event.ctrlKey;
    if (mod && !event.shiftKey && event.key.toLowerCase() === 's') {
      event.preventDefault();
      void save();
    } else if (mod && !event.shiftKey && (event.key.toLowerCase() === 'g' || event.key.toLowerCase() === 'l')) {
      event.preventDefault();
      event.stopPropagation();
      setGoto(true);
    }
  };

  const onInputKey = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Tab' && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault();
      const indent = /^\t/m.test(text) ? '\t' : '  ';
      document.execCommand('insertText', false, indent);
    }
  };

  const caretLine = () => {
    const el = input.current;
    return el ? el.value.slice(0, el.selectionStart).split('\n').length : 1;
  };
  const [cursor, setCursor] = useState(1);

  return (
    <div className="file-viewer" onKeyDown={onKeyDown} data-keys-own="s g l">
      <div className="file-bar">
        <button type="button" className={clsx('icon-button', treeOn && 'is-on')} aria-label="Folder tree" aria-pressed={treeOn} data-tip={`Folder tree  ${shortcutLabel(SHORTCUTS.fileTree)}`} data-tip-side="down" onClick={onToggleTree}>
          <IconLayoutSidebar size={15} />
        </button>
        <span className="file-name" title={full}>{basename(path)}</span>
        <span className="file-dir mono" title={full}>{dirname(path)}</span>
        {/* Save and Discard appear only with unsaved edits; a dot marks the name too. */}
        {dirty && <span className="file-dirty-dot" role="img" aria-label="Unsaved edits" data-tip="Unsaved edits" data-tip-side="down" />}
        <span className="spacer" />
        {dirty ? (
          <>
            <button type="button" className="icon-button" aria-label="Discard edits" data-tip="Discard edits" data-tip-side="down" onClick={() => void discard()}>
              <IconArrowBackUp size={15} />
            </button>
            <button type="button" className="button button-small button-primary" disabled={saving} onClick={() => void save()} data-tip={shortcutLabel({ key: 's', mod: true })} data-tip-side="down">
              <IconDeviceFloppy size={14} aria-hidden /> Save
            </button>
          </>
        ) : (
          <>
            {kind === 'text' && MARKDOWN.test(full) && (
              <button type="button" className={clsx('icon-button', preview && 'is-on')} aria-label="Preview" aria-pressed={preview} data-tip={preview ? 'Edit' : 'Preview'} data-tip-side="down" onClick={() => setPreview(!preview)}>
                <IconEye size={15} />
              </button>
            )}
            <button type="button" className="icon-button" aria-label="Copy path" data-tip={copied ? 'Copied' : 'Copy path'} data-tip-side="down" onClick={() => copy(full)}>
              <IconCopy size={14} />
            </button>
            <OpenIn path={full} compact />
          </>
        )}
      </div>

      {conflict && (
        <div className="file-conflict" role="alert">
          <span>{basename(path)} changed on disk while you were editing.</span>
          <button type="button" className="button button-small button-quiet" onClick={() => { setDisk(conflict); setDraft(null); setConflict(null); }}>Use the version on disk</button>
          <button type="button" className="button button-small" onClick={() => { setDisk((d) => (d ? { ...d, mtime: conflict.mtime } : d)); setForce(true); setConflict(null); }}>Keep mine</button>
        </div>
      )}

      {kind === 'image' ? (
        <div className="file-preview"><img src={`/api/raw?path=${encodeURIComponent(full)}`} alt={basename(path)} /></div>
      ) : kind === 'pdf' ? (
        <iframe className="file-pdf" title={basename(path)} src={`/api/raw?path=${encodeURIComponent(full)}`} />
      ) : error ? (
        <p className="pane-error">{error}</p>
      ) : !disk ? (
        <WorkingRegion label="Opening" />
      ) : preview && MARKDOWN.test(full) && !dirty ? (
        <div className="file-markdown"><Markdown text={text} live={false} /></div>
      ) : (
        <div ref={scroller} className="code-edit" onClick={(e) => { if (e.target === e.currentTarget) input.current?.focus(); }}>
          <div className="code-gutter num" aria-hidden style={{ height: lines.length * LINE }}>
            {lines.map((_, i) => <div key={i} className={clsx(i + 1 === cursor && 'is-cursor')}>{i + 1}</div>)}
          </div>
          <div className="code-layer" style={{ height: lines.length * LINE, minWidth: `calc(${widest + 4}ch + 24px)` }}>
            {mark && <div className="code-mark" style={{ top: (mark - 1) * LINE }} onAnimationEnd={() => setMark(null)} />}
            {/* hljs output is escaped HTML; plain text is escaped here. */}
            <pre className="code-hl hljs" aria-hidden dangerouslySetInnerHTML={{ __html: html }} />
            <textarea
              ref={input}
              className="code-input"
              value={text}
              wrap="off"
              spellCheck={false}
              autoCapitalize="off"
              autoComplete="off"
              aria-label={`Contents of ${basename(path)}`}
              onChange={(e) => {
                setDraft(e.target.value === disk.text ? null : e.target.value);
                setCursor(caretLine());
              }}
              onKeyUp={() => setCursor(caretLine())}
              onClick={() => setCursor(caretLine())}
              onKeyDown={onInputKey}
              onSelect={() => setCursor(caretLine())}
            />
          </div>
        </div>
      )}

      {kind === 'text' && disk && !(preview && MARKDOWN.test(full) && !dirty) && (
        <div className="file-status">
          <button type="button" className="file-status-item" onClick={() => setGoto(true)} data-tip={`Go to line  ${shortcutLabel({ key: 'g', mod: true })}`} data-tip-side="up">
            Ln {cursor}
          </button>
          <span className="file-status-item">{lines.length.toLocaleString()} lines</span>
          <span className="file-status-item">{crlf ? 'CRLF' : 'LF'}</span>
          <span className="file-status-item">{languageName(full)}</span>
        </div>
      )}

      {goto && <GoToLine max={lines.length} onClose={() => setGoto(false)} onGo={(n) => { setGoto(false); jump(n); }} />}
      {confirmDialog}
    </div>
  );
}

function GoToLine({ max, onGo, onClose }: { max: number; onGo: (n: number) => void; onClose: () => void }) {
  const [value, setValue] = useState('');
  return (
    <form
      className="goto"
      onSubmit={(e) => {
        e.preventDefault();
        const n = Number.parseInt(value, 10);
        if (Number.isFinite(n)) onGo(n);
      }}
    >
      <input
        autoFocus
        inputMode="numeric"
        value={value}
        placeholder={`Go to line (1 to ${max.toLocaleString()})`}
        onChange={(e) => setValue(e.target.value.replace(/[^0-9]/g, ''))}
        onKeyDown={(e) => {
          if (e.key === 'Escape') {
            e.stopPropagation();
            onClose();
          }
        }}
        onBlur={onClose}
      />
    </form>
  );
}

function dirname(p: string): string {
  const at = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'));
  return at > 0 ? p.slice(0, at) : '';
}

function basename(p: string): string {
  return p.slice(Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\')) + 1);
}

function escape(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
