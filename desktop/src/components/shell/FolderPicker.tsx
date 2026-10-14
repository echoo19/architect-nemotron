/** Folder browsing for the browser build (the desktop app uses the system dialog). */
import { IconArrowUp, IconFolder } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { create } from 'zustand';

interface PickerState {
  open: boolean;
  start?: string;
  resolve?: (value: string | null) => void;
  pick(start?: string): Promise<string | null>;
  done(value: string | null): void;
}

export const useFolderPicker = create<PickerState>((set, get) => ({
  open: false,
  pick(start) {
    return new Promise((resolve) => set({ open: true, start, resolve }));
  },
  done(value) {
    get().resolve?.(value);
    set({ open: false, resolve: undefined });
  },
}));

export function FolderPicker() {
  const { open, start, done } = useFolderPicker();
  const [dir, setDir] = useState<{ path: string; parent: string; dirs: string[] } | null>(null);
  const [error, setError] = useState('');

  const load = (path?: string) => {
    setError('');
    void fetch(`/api/dirs${path ? `?path=${encodeURIComponent(path)}` : ''}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('unreadable'))))
      .then(setDir)
      .catch(() => setError('That folder could not be read.'));
  };

  useEffect(() => {
    if (open) load(start);
  }, [open, start]);

  if (!open) return null;
  return (
    <div className="scrim" onMouseDown={() => done(null)}>
      <div className="dialog" role="dialog" aria-label="Choose a folder" onMouseDown={(e) => e.stopPropagation()}>
        <h2>Choose a folder</h2>
        <div className="picker-path mono">{dir?.path ?? ''}</div>
        {error && <p className="tone-err">{error}</p>}
        <ul className="picker-list">
          {dir && dir.parent !== dir.path && (
            <li><button type="button" className="picker-row" onClick={() => load(dir.parent)}><IconArrowUp size={14} aria-hidden /> Up one level</button></li>
          )}
          {dir?.dirs.map((d) => (
            <li key={d}>
              <button type="button" className="picker-row" onClick={() => load(`${dir.path}${dir.path.endsWith('\\') || dir.path.endsWith('/') ? '' : '/'}${d}`)}>
                <IconFolder size={14} aria-hidden /> {d}
              </button>
            </li>
          ))}
        </ul>
        <div className="button-row">
          <button type="button" className="button button-primary" disabled={!dir} onClick={() => done(dir?.path ?? null)}>Use this folder</button>
          <button type="button" className="button button-quiet" onClick={() => done(null)}>Cancel</button>
        </div>
      </div>
    </div>
  );
}
