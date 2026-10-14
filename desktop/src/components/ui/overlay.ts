/**
 * What a Dialog and a Sheet share: they stay mounted through a short exit, Escape
 * closes the topmost one, Tab keeps focus inside, and focus goes back to whatever had
 * it when the overlay opened.
 */
import { useEffect, useRef, useState, type RefObject } from 'react';

const stack: symbol[] = [];

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** `mounted` stays true for `exitMs` after `open` turns false, with `closing` set, so the exit can play. */
export function useOverlay(open: boolean, onClose: () => void, panel: RefObject<HTMLElement | null>, exitMs = 150): { mounted: boolean; closing: boolean } {
  const [mounted, setMounted] = useState(open);
  const [closing, setClosing] = useState(false);
  const returnTo = useRef<HTMLElement | null>(null);
  const close = useRef(onClose);
  close.current = onClose;

  // Mount at once on open; on close, play the exit and give focus back.
  useEffect(() => {
    if (open) {
      returnTo.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      setMounted(true);
      setClosing(false);
      return undefined;
    }
    if (!mounted) return undefined;
    setClosing(true);
    const back = returnTo.current;
    if (back && document.contains(back)) back.focus();
    const t = setTimeout(() => { setMounted(false); setClosing(false); }, exitMs);
    return () => clearTimeout(t);
  }, [open]);

  // While open: the topmost overlay takes Escape and keeps Tab inside its panel.
  useEffect(() => {
    if (!open) return undefined;
    const id = Symbol('overlay');
    stack.push(id);
    requestAnimationFrame(() => {
      const el = panel.current;
      if (el && !el.contains(document.activeElement)) el.focus();
    });
    const onKey = (event: KeyboardEvent) => {
      if (stack[stack.length - 1] !== id) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        close.current();
        return;
      }
      if (event.key !== 'Tab') return;
      const el = panel.current;
      if (!el) return;
      const items = [...el.querySelectorAll<HTMLElement>(FOCUSABLE)];
      if (items.length === 0) { event.preventDefault(); el.focus(); return; }
      const first = items[0], last = items[items.length - 1];
      const at = document.activeElement;
      if (event.shiftKey && (at === first || at === el)) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && at === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener('keydown', onKey, true);
    return () => {
      document.removeEventListener('keydown', onKey, true);
      const i = stack.indexOf(id);
      if (i >= 0) stack.splice(i, 1);
    };
  }, [open, panel]);

  return { mounted: mounted || open, closing: closing && !open };
}
