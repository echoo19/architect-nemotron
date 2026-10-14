/**
 * Pictures and diagrams in answers. An image opens in a viewer (fit or actual size,
 * wheel to zoom, download, Escape to close); a ```mermaid block draws as a diagram
 * in Lectern's flat colours, with its source one click away and a plain message if
 * it does not parse.
 */
import { IconCode, IconDownload, IconSchema, IconX, IconZoomIn, IconZoomOut } from '@tabler/icons-react';
import { useEffect, useId, useState } from 'react';
import { createPortal } from 'react-dom';
import { IconButton } from '../ui/Button';

export function imageUrl(src: string): string {
  return /^https:\/\//.test(src) ? src : `/api/raw?path=${encodeURIComponent(src)}`;
}

export function MdImage({ src, alt }: { src: string; alt: string }) {
  const [open, setOpen] = useState(false);
  const [failed, setFailed] = useState(false);
  if (failed) return <span className="md-image-missing">{alt || src}</span>;
  return (
    <>
      <button type="button" className="md-image" onClick={() => setOpen(true)} aria-label={alt ? `View ${alt}` : 'View image'}>
        <img src={imageUrl(src)} alt={alt} loading="lazy" onError={() => setFailed(true)} />
      </button>
      {open && <Lightbox src={src} alt={alt} onClose={() => setOpen(false)} />}
    </>
  );
}

function Lightbox({ src, alt, onClose }: { src: string; alt: string; onClose: () => void }) {
  const [scale, setScale] = useState<number | null>(null); // null fits the window
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        onClose();
      } else if (e.key === '+' || e.key === '=') setScale((s) => Math.min(8, (s ?? 1) * 1.25));
      else if (e.key === '-') setScale((s) => Math.max(0.1, (s ?? 1) / 1.25));
      else if (e.key === '0') setScale(null);
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [onClose]);
  const name = src.split(/[\\/]/).pop()?.split('?')[0] || 'image';
  return createPortal(
    <div className="lightbox" role="dialog" aria-label={alt || name} onClick={onClose}>
      <div className="lightbox-bar" onClick={(e) => e.stopPropagation()}>
        <span className="lightbox-name">{alt || name}</span>
        <span className="num lightbox-scale">{scale === null ? 'Fit' : `${Math.round(scale * 100)}%`}</span>
        <IconButton icon={IconZoomOut} label="Zoom out" size="sm" onClick={() => setScale((s) => Math.max(0.1, (s ?? 1) / 1.25))} />
        <IconButton icon={IconZoomIn} label="Zoom in" size="sm" onClick={() => setScale((s) => Math.min(8, (s ?? 1) * 1.25))} />
        <a className="ui-icon-button ui-icon-button-ghost ui-icon-button-sm" href={imageUrl(src)} download={name} aria-label="Download" data-tip="Download" data-tip-side="down"><IconDownload size={16} stroke={1.5} aria-hidden /></a>
        <IconButton icon={IconX} label="Close" size="sm" onClick={onClose} />
      </div>
      <div
        className="lightbox-stage"
        onWheel={(e) => setScale((s) => Math.max(0.1, Math.min(8, (s ?? 1) * (e.deltaY < 0 ? 1.1 : 1 / 1.1))))}
      >
        <img
          src={imageUrl(src)}
          alt={alt}
          className={scale === null ? 'is-fit' : undefined}
          style={scale === null ? undefined : { width: 'auto', maxWidth: 'none', maxHeight: 'none', transform: `scale(${scale})` }}
          onClick={(e) => {
            e.stopPropagation();
            setScale((s) => (s === null ? 1 : null));
          }}
        />
      </div>
    </div>,
    document.body,
  );
}

let mermaidLoad: Promise<typeof import('mermaid').default> | null = null;

/** Mermaid, loaded the first time a diagram appears, themed flat in Lectern's colours. */
function loadMermaid(dark: boolean): Promise<typeof import('mermaid').default> {
  mermaidLoad ??= import('mermaid').then((m) => m.default);
  return mermaidLoad.then((mermaid) => {
    // The tokens' values as hex (Mermaid takes literal colours): --text, --border,
    // --text-mute, --accent-100/700 (dark: --accent-200/700), --bg-raised, --bg-muted, --bg-subtle.
    const ink = dark ? '#F1F0EF' : '#1E1D1A';
    const line = dark ? '#474643' : '#D8D7D4';
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: 'strict',
      fontFamily: '"Inter", ui-sans-serif, system-ui, sans-serif',
      theme: 'base',
      themeVariables: {
        background: 'transparent',
        primaryColor: dark ? '#283633' : '#E8F1EF',
        primaryTextColor: ink,
        primaryBorderColor: dark ? '#679188' : '#275049',
        secondaryColor: dark ? '#2B2B29' : '#F3F3F1',
        tertiaryColor: dark ? '#222220' : '#FFFFFF',
        lineColor: dark ? '#B0AFAB' : '#5F5E58',
        textColor: ink,
        mainBkg: dark ? '#283633' : '#E8F1EF',
        nodeBorder: dark ? '#679188' : '#275049',
        clusterBkg: dark ? '#151514' : '#FAF9F8',
        clusterBorder: line,
        edgeLabelBackground: dark ? '#222220' : '#FFFFFF',
        fontSize: '13px',
      },
      flowchart: { curve: 'basis', htmlLabels: false },
    });
    return mermaid;
  });
}

export function MermaidBlock({ text }: { text: string }) {
  const id = useId().replace(/[^a-zA-Z0-9]/g, '');
  const [svg, setSvg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [source, setSource] = useState(false);
  useEffect(() => {
    let live = true;
    const dark = document.documentElement.dataset.theme === 'dark';
    void loadMermaid(dark)
      .then((mermaid) => mermaid.render(`mmd${id}`, text))
      .then((out) => live && setSvg(out.svg))
      .catch((err: unknown) => live && setError(String((err as Error)?.message ?? err).split('\n')[0]));
    return () => {
      live = false;
    };
  }, [id, text]);
  return (
    <div className="md-diagram md-block">
      <div className="md-code-bar">
        <span className="md-code-lang">Diagram</span>
        <IconButton icon={source ? IconSchema : IconCode} label={source ? 'Show the diagram' : 'Show the source'} className="md-code-copy" onClick={() => setSource(!source)} />
      </div>
      {source || error ? (
        <>
          {error && <p className="md-diagram-error">This diagram could not be drawn: {error}</p>}
          <pre className="md-code-body"><code>{text}</code></pre>
        </>
      ) : svg ? (
        // Mermaid's output with securityLevel strict (no scripts, no HTML labels).
        <div className="md-diagram-svg" dangerouslySetInnerHTML={{ __html: svg }} />
      ) : (
        <div className="md-diagram-svg is-loading" />
      )}
    </div>
  );
}
