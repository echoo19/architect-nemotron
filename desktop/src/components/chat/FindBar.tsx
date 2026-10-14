/**
 * Find in the conversation (Ctrl+F; Enter or Ctrl+G next, Shift+Enter or Ctrl+Shift+G
 * previous; Esc closes). Matches are painted with the CSS Highlight API so the
 * transcript's own DOM is never rewritten.
 */
import { IconChevronDown, IconChevronUp, IconX } from '@tabler/icons-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { IconButton } from '../ui/Button';

type HighlightRegistry = { set(name: string, h: unknown): void; delete(name: string): void };
const registry = (): HighlightRegistry | undefined => (globalThis.CSS as unknown as { highlights?: HighlightRegistry })?.highlights;
const HighlightCtor = (globalThis as unknown as { Highlight?: new (...ranges: Range[]) => unknown }).Highlight;

function findRanges(root: HTMLElement, query: string): Range[] {
  const ranges: Range[] = [];
  if (!query) return ranges;
  const needle = query.toLowerCase();
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let node = walker.nextNode() as Text | null;
  while (node) {
    const text = node.data.toLowerCase();
    let at = text.indexOf(needle);
    while (at !== -1) {
      const range = document.createRange();
      range.setStart(node, at);
      range.setEnd(node, at + needle.length);
      ranges.push(range);
      at = text.indexOf(needle, at + needle.length);
    }
    node = walker.nextNode() as Text | null;
  }
  return ranges;
}

export function FindBar({ root, onClose }: { root: () => HTMLElement | null; onClose: () => void }) {
  const [query, setQuery] = useState(() => window.getSelection()?.toString().slice(0, 80) ?? '');
  const [index, setIndex] = useState(0);
  const [count, setCount] = useState(0);
  const ranges = useRef<Range[]>([]);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    input.current?.focus();
    input.current?.select();
    return () => {
      registry()?.delete('find');
      registry()?.delete('find-current');
    };
  }, []);

  const paint = useCallback((list: Range[], current: number) => {
    const reg = registry();
    if (!reg || !HighlightCtor) return;
    reg.set('find', new HighlightCtor(...list));
    if (list[current]) {
      reg.set('find-current', new HighlightCtor(list[current]));
      const rect = list[current].getBoundingClientRect();
      const el = list[current].startContainer.parentElement;
      if (el && (rect.top < 80 || rect.bottom > window.innerHeight - 160)) el.scrollIntoView({ block: 'center' });
    } else reg.delete('find-current');
  }, []);

  useEffect(() => {
    const el = root();
    if (!el) return;
    ranges.current = findRanges(el, query);
    setCount(ranges.current.length);
    setIndex(0);
    paint(ranges.current, 0);
  }, [query, root, paint]);

  const step = (delta: number) => {
    if (!ranges.current.length) return;
    const next = (index + delta + ranges.current.length) % ranges.current.length;
    setIndex(next);
    paint(ranges.current, next);
  };

  return (
    <div className="find-bar" role="search">
      <input
        ref={input}
        className="find-input"
        aria-label="Find in conversation"
        placeholder="Find in conversation"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Escape') onClose();
          else if (e.key === 'Enter') step(e.shiftKey ? -1 : 1);
          else if ((e.key === 'g' || e.key === 'G') && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            step(e.shiftKey ? -1 : 1);
          }
        }}
      />
      <span className="find-count num">{query ? (count ? `${index + 1} of ${count}` : 'No matches') : ''}</span>
      <IconButton icon={IconChevronUp} label="Previous" onClick={() => step(-1)} disabled={!count} />
      <IconButton icon={IconChevronDown} label="Next" onClick={() => step(1)} disabled={!count} />
      <IconButton icon={IconX} label="Close" onClick={onClose} />
    </div>
  );
}
