/**
 * The folder tree beside the file view (Codex's Ctrl+Shift+E). Folders first, the
 * open file's folders expanded, a filter that turns the tree into ranked matches,
 * arrow keys to move, and a right-click menu on every row.
 */
import {
  IconBraces, IconChevronRight, IconFileCode, IconFileText, IconFolder, IconFolderOpen, IconPhoto, IconRefresh, IconSearch, type Icon,
} from '@tabler/icons-react';
import clsx from 'clsx';
import { useEffect, useMemo, useRef, useState } from 'react';
import { PathMenu, type PathTarget } from './PathMenu';
import { fuzzyScore } from './QuickOpen';

interface Node { name: string; path: string; folder: boolean; children: Node[] }

export function buildTree(files: string[]): Node[] {
  const root: Node = { name: '', path: '', folder: true, children: [] };
  for (const file of files) {
    let node = root;
    const parts = file.split('/');
    parts.forEach((part, i) => {
      const folder = i < parts.length - 1;
      const path = parts.slice(0, i + 1).join('/');
      let child = node.children.find((c) => c.name === part && c.folder === folder);
      if (!child) {
        child = { name: part, path, folder, children: [] };
        node.children.push(child);
      }
      node = child;
    });
  }
  const sort = (n: Node) => {
    n.children.sort((a, b) => (a.folder === b.folder ? a.name.localeCompare(b.name, undefined, { numeric: true }) : a.folder ? -1 : 1));
    n.children.forEach(sort);
  };
  sort(root);
  return root.children;
}

function fileIcon(name: string): Icon {
  const ext = name.slice(name.lastIndexOf('.') + 1).toLowerCase();
  if (['png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'ico'].includes(ext)) return IconPhoto;
  if (['json', 'jsonc', 'toml', 'yaml', 'yml'].includes(ext)) return IconBraces;
  if (['md', 'txt', 'rst', 'log'].includes(ext)) return IconFileText;
  return IconFileCode;
}

const filesCache = new Map<string, string[]>();

export function FileTree({ cwd, current, onOpen }: { cwd: string; current?: string; onOpen: (rel: string) => void }) {
  const [files, setFiles] = useState<string[] | null>(filesCache.get(cwd) ?? null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState('');
  const [menu, setMenu] = useState<PathTarget | null>(null);
  const list = useRef<HTMLDivElement>(null);

  const load = () => {
    void fetch(`/api/files?root=${encodeURIComponent(cwd)}`)
      .then((r) => r.json() as Promise<{ files: string[] }>)
      .then((r) => {
        filesCache.set(cwd, r.files);
        setFiles(r.files);
      })
      .catch(() => setFiles([]));
  };
  useEffect(load, [cwd]);

  // The open file's folders stay expanded.
  useEffect(() => {
    if (!current) return;
    const parts = current.replace(/\\/g, '/').split('/');
    setExpanded((prev) => {
      const next = new Set(prev);
      for (let i = 1; i < parts.length; i += 1) next.add(parts.slice(0, i).join('/'));
      return next;
    });
  }, [current]);

  const tree = useMemo(() => buildTree(files ?? []), [files]);
  const matches = useMemo(() => {
    if (!query.trim() || !files) return null;
    return files.map((f) => ({ f, s: fuzzyScore(query.trim(), f) })).filter((x) => x.s >= 0).sort((a, b) => b.s - a.s).slice(0, 200).map((x) => x.f);
  }, [files, query]);

  const toggle = (path: string, open?: boolean) => setExpanded((prev) => {
    const next = new Set(prev);
    if (open ?? !next.has(path)) next.add(path);
    else next.delete(path);
    return next;
  });

  const full = (rel: string) => `${cwd.replace(/[\\/]$/, '')}/${rel}`;
  const onContext = (event: React.MouseEvent, rel: string, folder: boolean) => {
    event.preventDefault();
    setMenu({ x: event.clientX, y: event.clientY, path: full(rel), rel, folder });
  };

  // Arrow keys walk the visible rows; Right and Left open and close folders.
  const onKeyDown = (event: React.KeyboardEvent) => {
    const rows = [...(list.current?.querySelectorAll<HTMLButtonElement>('.tree-row') ?? [])];
    const at = rows.indexOf(document.activeElement as HTMLButtonElement);
    const row = rows[at];
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      rows[Math.max(0, Math.min(rows.length - 1, at + (event.key === 'ArrowDown' ? 1 : -1)))]?.focus();
    } else if (row?.dataset.folder && (event.key === 'ArrowRight' || event.key === 'ArrowLeft')) {
      event.preventDefault();
      toggle(row.dataset.path ?? '', event.key === 'ArrowRight');
    }
  };

  const render = (nodes: Node[], depth: number): React.ReactNode => nodes.map((node) => {
    const open = expanded.has(node.path);
    const FileIcon = node.folder ? (open ? IconFolderOpen : IconFolder) : fileIcon(node.name);
    return (
      <div key={node.path} role="none">
        <button
          type="button"
          role="treeitem"
          aria-expanded={node.folder ? open : undefined}
          aria-selected={node.path === current}
          className={clsx('tree-row', node.path === current && 'is-current')}
          style={{ paddingLeft: 8 + depth * 14 }}
          data-path={node.path}
          data-folder={node.folder ? '1' : undefined}
          onClick={() => (node.folder ? toggle(node.path) : onOpen(node.path))}
          onContextMenu={(e) => onContext(e, node.path, node.folder)}
          title={node.path}
        >
          {node.folder ? <IconChevronRight size={12} className={clsx('tree-chevron', open && 'is-open')} aria-hidden /> : <span className="tree-chevron" />}
          <FileIcon size={15} stroke={1.6} className={clsx('tree-icon', node.folder && 'is-folder')} aria-hidden />
          <span className="tree-name">{node.name}</span>
        </button>
        {node.folder && open && <div role="group">{render(node.children, depth + 1)}</div>}
      </div>
    );
  });

  return (
    <div className="file-tree">
      <div className="tree-search">
        <IconSearch size={13} aria-hidden />
        <input
          value={query}
          placeholder="Filter files"
          spellCheck={false}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Escape' && query) {
              e.stopPropagation();
              setQuery('');
            } else if (e.key === 'ArrowDown') {
              e.preventDefault();
              list.current?.querySelector<HTMLButtonElement>('.tree-row')?.focus();
            } else if (e.key === 'Enter' && matches?.[0]) onOpen(matches[0]);
          }}
        />
        <button type="button" className="icon-button" aria-label="Refresh" data-tip="Refresh" onClick={load}><IconRefresh size={13} /></button>
      </div>
      <div ref={list} className="tree-list" role="tree" aria-label="Files" onKeyDown={onKeyDown}>
        {files === null ? (
          <div className="tree-skeleton">{Array.from({ length: 8 }, (_, i) => <span key={i} className="skel" style={{ width: `${40 + ((i * 37) % 45)}%` }} />)}</div>
        ) : matches ? (
          matches.length ? matches.map((rel) => {
            const slash = rel.lastIndexOf('/');
            const FileIcon = fileIcon(rel);
            return (
              <button key={rel} type="button" role="treeitem" className={clsx('tree-row', rel === current && 'is-current')} data-path={rel} onClick={() => onOpen(rel)} onContextMenu={(e) => onContext(e, rel, false)} title={rel}>
                <FileIcon size={15} stroke={1.6} className="tree-icon" aria-hidden />
                <span className="tree-name">{rel.slice(slash + 1)}</span>
                {slash > 0 && <span className="tree-dir">{rel.slice(0, slash)}</span>}
              </button>
            );
          }) : <p className="tree-empty">No file matches</p>
        ) : tree.length ? render(tree, 0) : <p className="tree-empty">This folder is empty</p>}
      </div>
      {menu && <PathMenu target={menu} onClose={() => setMenu(null)} />}
    </div>
  );
}
