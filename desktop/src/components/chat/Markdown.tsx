// Adapted from Hearth (github.com/echoo19/hearth, MIT), apps/editor/src/components/chat/Markdown.tsx.
import { IconCheck, IconCopy } from '@tabler/icons-react';
import React, { useMemo, useState } from 'react';
import { isPlainProse, parseMarkdown, type MdBlock, type MdList, type MdSpan, type MdTable } from '../../lib/markdown';
import { highlight, languageFor } from '../../lib/highlight';
import { native } from '../../lib/native';
import { useCopy } from '../../lib/useCopy';
import { useApp } from '../../store/app';
import { PathMenu, type PathTarget } from '../panes/PathMenu';
import { IconButton } from '../ui/Button';
import { MdImage, MermaidBlock } from './RichMedia';
import { useSmoothStream } from './useSmoothStream';

/** A path on this machine: opens in the Files pane (at its line for path:12); right-click for more. */
function PathSpan({ target, text }: { target: string; text: string }) {
  const n = native();
  const openDock = useApp((st) => st.openDock);
  const cwd = useApp((st) => (st.prefs.active ? st.convs[st.prefs.active]?.state.cwd : undefined)) ?? '';
  const [menu, setMenu] = useState<PathTarget | null>(null);
  const match = /^(.*?):(\d+)(?::\d+)?$/.exec(target);
  const file = match ? match[1] : target;
  const line = match ? Number(match[2]) : undefined;
  const folder = /[\\/]$/.test(file);
  const rel = cwd && file.startsWith(cwd) ? file.slice(cwd.length).replace(/^[\\/]/, '') : file;
  return (
    <>
      <button
        type="button"
        className="md-path"
        onClick={() => (n && folder ? void n.reveal(file) : openDock('files', file, line))}
        onContextMenu={(e) => {
          e.preventDefault();
          setMenu({ x: e.clientX, y: e.clientY, path: file, rel, folder });
        }}
      >
        {text}
      </button>
      {menu && <PathMenu target={menu} onClose={() => setMenu(null)} />}
    </>
  );
}

function Spans({ spans }: { spans: MdSpan[] }) {
  return (
    <>
      {spans.map((span, index) => {
        switch (span.kind) {
          case 'text':
            return <React.Fragment key={index}>{span.text}</React.Fragment>;
          case 'code':
            return (
              <code key={index} className="md-code-span">
                {/^#(?:[0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i.test(span.text) && <span className="md-swatch" style={{ background: span.text }} aria-hidden />}
                {span.text}
              </code>
            );
          case 'strong':
            return <strong key={index}><Spans spans={span.spans} /></strong>;
          case 'em':
            return <em key={index}><Spans spans={span.spans} /></em>;
          case 'link':
            return (
              <a
                key={index}
                className="md-link"
                href={span.href}
                target="_blank"
                rel="noopener noreferrer"
                onClick={(e) => {
                  // A local server opens in the Browser pane; anything else in your browser.
                  if (/^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(:\d+)?(\/|$)/i.test(span.href) && !(e.ctrlKey || e.metaKey)) {
                    e.preventDefault();
                    useApp.getState().openBrowser(span.href);
                  }
                }}
              >
                <Spans spans={span.spans} />
              </a>
            );
          case 'image':
            return <MdImage key={index} src={span.src} alt={span.alt} />;
          default:
            return <PathSpan key={index} target={span.target} text={span.text} />;
        }
      })}
    </>
  );
}

function List({ list }: { list: MdList }) {
  const items = list.items.map((item, index) => (
    <li key={index} className="md-item">
      <Spans spans={item.spans} />
      {item.children ? <List list={item.children} /> : null}
    </li>
  ));
  return list.ordered
    ? <ol className="md-list md-block" start={list.start}>{items}</ol>
    : <ul className="md-list md-block">{items}</ul>;
}

function CodeBlock({ lang, text, pending }: { lang: string; text: string; pending: boolean }) {
  const { copied, copy } = useCopy();
  return (
    <div className="md-code md-block">
      <div className="md-code-bar">
        <span className="md-code-lang">{lang === '' ? 'text' : lang}</span>
        {!pending && (
          <IconButton icon={copied ? IconCheck : IconCopy} label={copied ? 'Copied' : 'Copy code'} className="md-code-copy" onClick={() => copy(text)} />
        )}
      </div>
      <CodeBody lang={lang} text={text} pending={pending} />
    </div>
  );
}

function CodeBody({ lang, text, pending }: { lang: string; text: string; pending: boolean }) {
  const html = useMemo(() => (pending ? null : highlight(text, languageFor(lang || ''))), [lang, text, pending]);
  return html !== null
    ? <pre className="md-code-body"><code className="hljs" dangerouslySetInnerHTML={{ __html: html }} /></pre>
    : <pre className="md-code-body"><code>{text}</code></pre>;
}

function Table({ table }: { table: MdTable }) {
  return (
    <div className="md-table-scroll md-block" role="group" aria-label="Table" tabIndex={0}>
      <table className="md-table">
        <thead>
          <tr>
            {table.head.map((cell, index) => (
              <th key={index} data-align={table.align[index] ?? undefined} scope="col"><Spans spans={cell} /></th>
            ))}
          </tr>
        </thead>
        <tbody>
          {table.rows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {row.map((cell, index) => (
                <td key={index} data-align={table.align[index] ?? undefined}><Spans spans={cell} /></td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Block({ block, live }: { block: MdBlock; live: boolean }) {
  switch (block.kind) {
    case 'paragraph':
      return <p className="msg-text md-block"><Spans spans={block.spans} /></p>;
    case 'heading': {
      const Tag = `h${Math.min(block.level + 1, 6)}` as 'h2' | 'h3' | 'h4' | 'h5' | 'h6';
      return <Tag className={`md-heading md-h${Math.min(block.level, 6)} md-block`}><Spans spans={block.spans} /></Tag>;
    }
    case 'list':
      return <List list={block} />;
    case 'table':
      return <Table table={block} />;
    default:
      if (block.lang.toLowerCase() === 'mermaid' && !(live && !block.closed)) return <MermaidBlock text={block.text} />;
      return <CodeBlock lang={block.lang} text={block.text} pending={live && !block.closed} />;
  }
}

export function Markdown({ text, live }: { text: string; live: boolean }) {
  const shown = useSmoothStream(text, live);
  const blocks = useMemo(() => parseMarkdown(shown, live), [shown, live]);
  if (isPlainProse(blocks, shown)) {
    return <p className={live ? 'msg-text ai-caret' : 'msg-text'}>{shown}</p>;
  }
  if (blocks.length === 0) return live ? <p className="msg-text ai-caret" /> : null;
  return (
    <div className={live ? 'md md-live' : 'md'}>
      {blocks.map((block, index) => <Block key={index} block={block} live={live} />)}
    </div>
  );
}
