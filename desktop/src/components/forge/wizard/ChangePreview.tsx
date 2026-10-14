/**
 * What an edit approval on the Add step will change (6a2): the file, then each change as
 * a line diff of its old text against its new text, so lines the edit keeps read as kept
 * instead of removed and added again. The core's preview carries at most a few lines of
 * each side of a change; past the point where a side was cut, the other side's lines are
 * shown faint and are not called removed or added (`previewChanges`).
 */
import { IconFileCode } from '@tabler/icons-react';
import clsx from 'clsx';
import { highlight, languageFor } from '../../../lib/highlight';
import { previewChanges, PREVIEW_LINES, type PreviewLine } from '../../../lib/wizard';
import { useApp } from '../../../store/app';

type Shown = PreviewLine | { kind: 'kept'; count: number };

/**
 * A change's lines with long runs of kept lines folded to "N lines kept": one kept line
 * stays on each side of an addition or removal, so the change reads at a glance.
 */
export function foldKept(lines: PreviewLine[], keep = 1): Shown[] {
  const out: Shown[] = [];
  let i = 0;
  while (i < lines.length) {
    if (lines[i].kind !== 'ctx') {
      out.push(lines[i]);
      i += 1;
      continue;
    }
    let end = i;
    while (end < lines.length && lines[end].kind === 'ctx') end += 1;
    const run = lines.slice(i, end);
    const head = i === 0 ? 0 : keep;
    const tail = end === lines.length ? 0 : keep;
    if (run.length > head + tail + 1) {
      out.push(...run.slice(0, head), { kind: 'kept', count: run.length - head - tail }, ...run.slice(run.length - tail));
    } else out.push(...run);
    i = end;
  }
  return out;
}

export function ChangePreview({ detail, path }: { detail: string; path?: string }) {
  const openDock = useApp((s) => s.openDock);
  const preview = previewChanges(detail);
  if (!preview) return <pre className="ask-detail">{detail}</pre>;
  const lang = languageFor(preview.path);
  const shown = path ?? preview.path;
  const many = preview.changes.length > 1;
  return (
    <div className="change-preview">
      <button type="button" className="edit-preview-head" onClick={() => openDock('files', preview.path)} title={`Open ${preview.path}`}>
        <IconFileCode size={14} stroke={1.5} aria-hidden />
        <span className="edit-preview-name mono">{shown}</span>
        <span className="edit-preview-meta">{many ? `The edit, in ${preview.changes.length} parts` : 'The edit'}</span>
      </button>
      {preview.changes.map((change, i) => (
        <section key={i} className="change-block" aria-label={many ? `Part ${i + 1} of ${preview.changes.length}` : 'The change'}>
          {many && <p className="change-block-head">Part {i + 1} of {preview.changes.length}</p>}
          <div className="edit-preview-body change-block-lines">
            {foldKept(change.lines.filter((l) => l.kind !== 'unseen')).map((line, j) => {
              if (line.kind === 'kept') {
                return (
                  <div key={j} className="change-line is-kept">
                    <span className="change-sign" />
                    <span className="change-kept">{line.count} {line.count === 1 ? 'line' : 'lines'} kept as they are</span>
                  </div>
                );
              }
              const html = highlight(line.text, lang);
              return (
                <div key={j} className={clsx('change-line', `is-${line.kind}`)}>
                  <span className="change-sign">{line.kind === 'add' ? '+' : line.kind === 'del' ? '-' : ''}</span>
                  <code className="change-code hljs" dangerouslySetInnerHTML={{ __html: html || ' ' }} />
                </div>
              );
            })}
          </div>
          {(change.mayGoOn || change.cut) && (
            <p className="change-cut">
              {change.cut
                ? `Architect shows only the first ${PREVIEW_LINES} lines of each part before you allow it, so the rest of this part is not shown.`
                : `Architect shows only the first ${PREVIEW_LINES} lines of each part before you allow it, so this part may go on past here.`}
            </p>
          )}
        </section>
      ))}
    </div>
  );
}
