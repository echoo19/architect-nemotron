/**
 * What an edit or a new file approval will change, as a diff (Claude Code's manual
 * mode): the file, then the lines removed and added, highlighted for its language.
 * The core sends a bounded preview ("path (2 edits)", then "- " and "+ " lines).
 */
import { IconFileCode } from '@tabler/icons-react';
import clsx from 'clsx';
import { highlight, languageFor } from '../../lib/highlight';
import { SAVING_STOPS, stopsAgent } from '../../lib/retake';
import { useApp } from '../../store/app';

export function parseApprovalPreview(detail: string): { path: string; meta: string; lines: Array<{ sign: '+' | '-' | ' '; text: string }> } | null {
  const [head, ...rest] = detail.split('\n');
  const match = /^(.*?) \(([^)]*)\)$/.exec(head ?? '');
  if (!match) return null;
  const lines = rest.map((line) => {
    if (line.startsWith('+ ') || line === '+') return { sign: '+' as const, text: line.slice(2) };
    if (line.startsWith('- ') || line === '-') return { sign: '-' as const, text: line.slice(2) };
    return { sign: ' ' as const, text: line };
  });
  return { path: match[1], meta: match[2], lines };
}

export function EditPreview({ detail }: { detail: string }) {
  const openDock = useApp((s) => s.openDock);
  const preview = parseApprovalPreview(detail);
  if (!preview) return <pre className="ask-detail">{detail}</pre>;
  const lang = languageFor(preview.path);
  const name = preview.path.split('\\').join('/').split('/').pop() ?? preview.path;
  return (
    <div className="edit-preview">
      <button type="button" className="edit-preview-head" onClick={() => openDock('files', preview.path)} title={`Open ${preview.path}`}>
        <IconFileCode size={14} stroke={1.5} aria-hidden />
        <span className="edit-preview-name">{name}</span>
        <span className="edit-preview-dir mono">{preview.path.slice(0, preview.path.length - name.length).replace(/[\\/]$/, '')}</span>
        <span className="edit-preview-meta">{preview.meta}</span>
      </button>
      {stopsAgent(preview.path) && <p className="edit-preview-stops">{SAVING_STOPS}</p>}
      <div className="edit-preview-body">
        {preview.lines.map((line, i) => {
          const more = /^\.\.\. \d+ more lines$/.test(line.text);
          const html = more ? null : highlight(line.text, lang);
          return (
            <div key={i} className={clsx('change-line', line.sign === '+' ? 'is-add' : line.sign === '-' ? 'is-del' : 'is-ctx', more && 'is-more')}>
              <span className="change-sign">{line.sign === ' ' ? '' : line.sign}</span>
              {html !== null
                ? <code className="change-code hljs" dangerouslySetInnerHTML={{ __html: html || ' ' }} />
                : <code className="change-code">{line.text || ' '}</code>}
            </div>
          );
        })}
      </div>
    </div>
  );
}
