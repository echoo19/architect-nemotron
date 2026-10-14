/** "Open in VS Code / Cursor / ..." (Codex's Open button), for a folder or a file. */
import { IconChevronDown, IconCode, IconFolder } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { native, revealLabel } from '../../lib/native';
import { normPath, useApp } from '../../store/app';
import { Menu } from '../ui/Menu';

let editorsCache: Promise<Array<{ id: string; label: string }>> | null = null;
export function editors(): Promise<Array<{ id: string; label: string }>> {
  editorsCache ??= fetch('/api/editors').then((r) => r.json()).then((r: { editors: Array<{ id: string; label: string }> }) => r.editors).catch(() => []);
  return editorsCache;
}

/** The project a path belongs to: the open conversation's folder it sits in, if any. */
function projectOf(path: string): string | null {
  const norm = normPath(path);
  const roots = useApp.getState().prefs.open.map((o) => o.worktree?.root ?? o.cwd).filter(Boolean).map(normPath);
  return roots.filter((r) => norm === r || norm.startsWith(`${r}/`)).sort((a, b) => b.length - a.length)[0] ?? null;
}

/** Codex's preferred Open editor, with a per-project choice over the general one. */
function preferred(path: string): string | null {
  try {
    const project = projectOf(path);
    return (project && localStorage.getItem(`architect-editor:${project}`)) || localStorage.getItem('architect-editor');
  } catch {
    return null;
  }
}

export function openIn(editor: string, path: string, line?: number): void {
  try {
    localStorage.setItem('architect-editor', editor);
    const project = projectOf(path);
    if (project) localStorage.setItem(`architect-editor:${project}`, editor);
  } catch {
    /* storage blocked */
  }
  void fetch('/api/open-in', { method: 'POST', body: JSON.stringify({ editor, path, line }) });
}

export function OpenIn({ path, compact = false }: { path: string; compact?: boolean }) {
  const [list, setList] = useState<Array<{ id: string; label: string }>>([]);
  const [menu, setMenu] = useState(false);
  useEffect(() => {
    void editors().then(setList);
  }, []);
  const n = native();
  const main = list.find((e) => e.id === preferred(path)) ?? list[0];
  const items = [
    ...list.map((e) => ({ id: e.id, label: e.label, icon: IconCode, selected: e.id === main?.id })),
    ...(n ? [{ id: '__reveal', label: revealLabel(), icon: IconFolder }] : []),
  ];
  if (!items.length) return null;
  return (
    <div className="open-in bar-anchor no-drag">
      <button
        type="button"
        className="open-in-main"
        onClick={() => (main ? openIn(main.id, path) : void n?.reveal(path))}
        data-tip={main ? `Open in ${main.label}` : revealLabel()}
        data-tip-side="down"
      >
        <IconCode size={15} aria-hidden />
        {!compact && <span>{main ? main.label : 'Open'}</span>}
      </button>
      <button type="button" className="open-in-more" aria-label="Open with" onClick={() => setMenu(!menu)}>
        <IconChevronDown size={13} />
      </button>
      {menu && (
        <Menu
          side="down"
          align="end"
          width={200}
          items={items}
          onClose={() => setMenu(false)}
          onPick={(id) => {
            setMenu(false);
            if (id === '__reveal') void n?.reveal(path);
            else openIn(id, path);
          }}
        />
      )}
    </div>
  );
}
