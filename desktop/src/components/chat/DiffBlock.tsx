/** A unified diff (what `/diff` shows): per-file +n/-n, the patch one click away. */
import { IconChevronRight } from '@tabler/icons-react';
import clsx from 'clsx';
import { useMemo, useState } from 'react';
import { Button } from '../ui/Button';

interface FileDiff {
  path: string;
  added: number;
  removed: number;
  lines: string[];
  /** A file git does not track yet: listed, not diffed. */
  untracked?: boolean;
}

export function parseDiff(text: string): FileDiff[] {
  const files: FileDiff[] = [];
  let current: FileDiff | null = null;
  let untracked = false;
  for (const line of text.split('\n')) {
    if (/^Untracked files:/.test(line)) {
      untracked = true;
      continue;
    }
    if (untracked) {
      const name = line.trim();
      if (name && !name.startsWith('...')) files.push({ path: name, added: 0, removed: 0, lines: [], untracked: true });
      continue;
    }
    const header = /^diff --git a\/(.+?) b\/(.+)$/.exec(line);
    if (header) {
      current = { path: header[2], added: 0, removed: 0, lines: [] };
      files.push(current);
      continue;
    }
    const plus = /^\+\+\+ (?:b\/)?(.+)$/.exec(line);
    if (plus && !current) {
      current = { path: plus[1], added: 0, removed: 0, lines: [] };
      files.push(current);
      continue;
    }
    if (!current) {
      current = { path: '', added: 0, removed: 0, lines: [] };
      files.push(current);
    }
    if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('index ')) continue;
    if (line.startsWith('+')) current.added += 1;
    else if (line.startsWith('-')) current.removed += 1;
    current.lines.push(line);
  }
  return files.filter((f) => f.path || f.lines.some((l) => l.trim()));
}

export function DiffBlock({ text, onUndo }: { text: string; onUndo?: () => void }) {
  const files = useMemo(() => parseDiff(text), [text]);
  if (!text.trim()) return null;
  const added = files.reduce((n, f) => n + f.added, 0);
  const removed = files.reduce((n, f) => n + f.removed, 0);
  return (
    <section className="diff-card ai-block">
      <div className="diff-head">
        <h2 className="diff-title">{files.length === 1 ? '1 file changed' : `${files.length} files changed`}</h2>
        <span className="diff-count num"><span className="tone-ok">+{added}</span> <span className="tone-err">-{removed}</span></span>
        {onUndo && <Button size="xs" variant="ghost" className="diff-undo" onClick={onUndo} data-tip="Undo the last turn's changes">Undo last turn</Button>}
      </div>
      <ul className="diff-files">
        {files.map((file, i) => <FileRow key={i} file={file} />)}
      </ul>
    </section>
  );
}

function FileRow({ file }: { file: FileDiff }) {
  const [open, setOpen] = useState(false);
  return (
    <li>
      <button type="button" className="diff-file" onClick={() => setOpen(!open)} aria-expanded={open}>
        <IconChevronRight size={14} stroke={1.5} className={clsx('work-chevron', open && 'is-open')} aria-hidden />
        <span className="diff-path">{file.path || 'changes'}</span>
        <span className="num"><span className="tone-ok">+{file.added}</span> <span className="tone-err">-{file.removed}</span></span>
      </button>
      {open && (
        <pre className="diff-body">
          {file.lines.map((line, i) => (
            <span key={i} className={clsx('diff-line', line.startsWith('+') && 'is-add', line.startsWith('-') && 'is-del', line.startsWith('@@') && 'is-hunk')}>
              {line || ' '}
              {'\n'}
            </span>
          ))}
        </pre>
      )}
    </li>
  );
}
