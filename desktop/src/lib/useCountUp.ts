/**
 * A number that counts to its new value instead of jumping (Codex's animated diff
 * stats). Respects reduced motion: then it just changes.
 */
import { useEffect, useRef, useState } from 'react';

export function useCountUp(value: number, ms = 420): number {
  const [shown, setShown] = useState(value);
  const from = useRef(value);
  useEffect(() => {
    const start = from.current;
    if (start === value) return;
    const reduce = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    if (reduce) {
      from.current = value;
      setShown(value);
      return;
    }
    const began = performance.now();
    let frame = 0;
    const step = (now: number) => {
      const t = Math.min(1, (now - began) / ms);
      const eased = 1 - (1 - t) ** 3;
      const next = Math.round(start + (value - start) * eased);
      setShown(next);
      from.current = next;
      if (t < 1) frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [value, ms]);
  return shown;
}
