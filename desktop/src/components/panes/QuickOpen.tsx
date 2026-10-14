/** Ctrl+P: find a file in the conversation's folder and open it in the viewer. */
import { IconFileCode } from '@tabler/icons-react';
import { useEffect, useMemo, useState } from 'react';
import { useApp } from '../../store/app';
import { Menu, type MenuItem } from '../ui/Menu';

const cache = new Map<string, string[]>();

/** Subsequence match, scored: contiguous runs and name hits first. */
export function fuzzyScore(query: string, path: string): number {
  if (!query) return 0;
  const q = query.toLowerCase();
  const p = path.toLowerCase();
  const name = p.slice(p.lastIndexOf('/') + 1);
  if (name.startsWith(q)) return 1000 - name.length;
  if (name.includes(q)) return 800 - name.length;
  if (p.includes(q)) return 600 - p.length;
  let qi = 0;
  let score = 0;
  let run = 0;
  for (let i = 0; i < p.length && qi < q.length; i += 1) {
    if (p[i] === q[qi]) {
      qi += 1;
      run += 1;
      score += run * 2;
    } else run = 0;
  }
  return qi === q.length ? score - p.length / 10 : -1;
}

export function QuickOpen() {
  const open = useApp((s) => s.quickOpen);
  const setQuickOpen = useApp((s) => s.setQuickOpen);
  const openDock = useApp((s) => s.openDock);
  const cwd = useApp((s) => {
    const active = s.prefs.active ? s.convs[s.prefs.active] : undefined;
    return active?.state.cwd ?? s.prefs.open.find((o) => o.id === s.prefs.active)?.cwd ?? '';
  });
  const [files, setFiles] = useState<string[]>(cache.get(cwd) ?? []);
  const [query, setQuery] = useState('');

  useEffect(() => {
    if (!open || !cwd) return;
    setQuery('');
    void fetch(`/api/files?root=${encodeURIComponent(cwd)}`)
      .then((r) => r.json() as Promise<{ files: string[] }>)
      .then((r) => {
        cache.set(cwd, r.files);
        setFiles(r.files);
      })
      .catch(() => undefined);
  }, [open, cwd]);

  const items: MenuItem[] = useMemo(() => {
    const ranked = query
      ? files.map((f) => ({ f, s: fuzzyScore(query, f) })).filter((x) => x.s >= 0).sort((a, b) => b.s - a.s).slice(0, 60).map((x) => x.f)
      : files.slice(0, 60);
    return ranked.map((f) => {
      const slash = f.lastIndexOf('/');
      return { id: f, label: f.slice(slash + 1), hint: slash > 0 ? f.slice(0, slash) : undefined, icon: IconFileCode };
    });
  }, [files, query]);

  if (!open) return null;
  const close = () => setQuickOpen(false);
  return (
    <div className="scrim scrim-top" onMouseDown={close}>
      <div className="palette" onMouseDown={(e) => e.stopPropagation()}>
        <Menu
          items={items}
          filter
          side="down"
          width={620}
          empty={files.length ? 'No file matches' : 'Reading the folder'}
          onClose={close}
          onQuery={setQuery}
          onPick={(id) => {
            close();
            openDock('files', id);
          }}
        />
      </div>
    </div>
  );
}
