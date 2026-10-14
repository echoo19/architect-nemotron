import { useCallback, useEffect, useRef, useState } from 'react';

/** Copy text; `copied` is true for a moment afterwards so the control can say so. */
export function useCopy(): { copied: boolean; copy: (text: string) => void } {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
  }, []);
  const copy = useCallback((text: string) => {
    void navigator.clipboard?.writeText(text).then(() => {
      setCopied(true);
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 1_400);
    });
  }, []);
  return { copied, copy };
}
