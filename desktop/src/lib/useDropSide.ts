/**
 * Which way a dropdown should open from its trigger: down when there is room below,
 * up otherwise. The trigger's chevron uses the same answer, so it always points the
 * way the menu will appear.
 */
import { useCallback, useEffect, useRef, useState } from 'react';

export function useDropSide<T extends HTMLElement>(menuHeight = 280): { ref: React.RefObject<T>; side: 'up' | 'down'; measure: () => void } {
  const ref = useRef<T>(null);
  const [side, setSide] = useState<'up' | 'down'>('down');
  const measure = useCallback(() => {
    const rect = ref.current?.getBoundingClientRect();
    if (!rect) return;
    const below = window.innerHeight - rect.bottom;
    setSide(below >= menuHeight + 16 || below >= rect.top ? 'down' : 'up');
  }, [menuHeight]);
  useEffect(() => {
    measure();
    window.addEventListener('resize', measure);
    // Layout moves (the start screen's composer drops to the bottom after the first message).
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure);
    observer?.observe(document.body);
    return () => {
      window.removeEventListener('resize', measure);
      observer?.disconnect();
    };
  }, [measure]);
  return { ref, side, measure };
}
