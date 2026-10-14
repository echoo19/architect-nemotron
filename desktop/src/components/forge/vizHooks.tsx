/**
 * What the evidence visuals share in the page: their container's width (they draw at
 * true size, so type never scales with the drawing), hover and focus readouts for the
 * marks inside an SVG (CSS tooltips cannot attach to SVG elements), and the one-time
 * entrance of a mark that just arrived.
 */
import { useCallback, useLayoutEffect, useRef, useState, type RefObject } from 'react';

/** The element's content width in CSS pixels, kept current as it resizes. */
export function useWidth(ref: RefObject<HTMLElement>): number {
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const read = () => setWidth(Math.floor(el.clientWidth));
    read();
    if (typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(read);
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref]);
  return width;
}

export interface Tip { text: string; x: number; y: number; below: boolean }

/**
 * Readouts for marks carrying `data-viz-tip`: hover and keyboard focus show the same
 * text (text only, never markup), above the mark, or below it near the top edge.
 */
export function useVizTip(root: RefObject<HTMLElement>) {
  const [tip, setTip] = useState<Tip | null>(null);
  const show = useCallback((target: EventTarget | null) => {
    const host = root.current;
    const mark = target instanceof Element ? target.closest('[data-viz-tip]') : null;
    if (!host || !mark || !host.contains(mark)) {
      setTip(null);
      return;
    }
    const box = mark.getBoundingClientRect();
    const frame = host.getBoundingClientRect();
    const below = box.top - frame.top < 30;
    setTip({
      text: mark.getAttribute('data-viz-tip') ?? '',
      x: box.left - frame.left + box.width / 2,
      y: below ? box.bottom - frame.top + 8 : box.top - frame.top - 8,
      below,
    });
  }, [root]);
  const handlers = {
    onPointerOver: (e: React.PointerEvent) => show(e.target),
    onPointerLeave: () => setTip(null),
    onFocus: (e: React.FocusEvent) => show(e.target),
    onBlur: () => setTip(null),
  };
  return { tip, handlers };
}

/** The readout itself: ink on the page colour, no shadow (viz.css `.viz-tip`). */
export function VizTip({ tip }: { tip: Tip | null }) {
  if (!tip) return null;
  return (
    <div
      className="viz-tip is-on viz-tip-local"
      role="tooltip"
      style={{ transform: `translate(calc(${Math.round(tip.x)}px - 50%), ${Math.round(tip.y)}px) translateY(${tip.below ? '0' : '-100%'})` }}
    >
      {tip.text}
    </div>
  );
}

/**
 * Marks that arrived since the last render play their entrance once; the first render
 * with a layout (a page load, a replayed build) draws everything at rest. Returns, for
 * each key, the order it arrived in within its batch (for spacing a batch's entrances).
 */
export function useEntrances(keys: string[] | null): Map<string, number> {
  const born = useRef<Map<string, { at: number; order: number }> | null>(null);
  const now = Date.now();
  const entering = new Map<string, number>();
  if (keys === null) return entering;
  if (born.current === null) {
    born.current = new Map(keys.map((key) => [key, { at: 0, order: 0 }]));
    return entering;
  }
  let order = 0;
  for (const key of keys) {
    const seen = born.current.get(key);
    if (!seen) {
      born.current.set(key, { at: now, order });
      entering.set(key, order);
      order += 1;
    } else if (now - seen.at < 600) {
      entering.set(key, seen.order);
    }
  }
  return entering;
}
