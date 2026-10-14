/**
 * The build's clocks: now, once a second while something runs, and a fold's elapsed time
 * (the build counts from the Build press, timeline.ts). Format with `fmtDuration` (lib/wizard.ts).
 */
import { useEffect, useState } from 'react';
import type { Item } from '../../../lib/timeline';

type Work = Extract<Item, { kind: 'work' }>;

/** Now, once a second while `ticking`. */
export function useNow(ticking: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!ticking) return undefined;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [ticking]);
  return now;
}

/**
 * A fold's elapsed ms: counting while it runs, frozen when it ended. Under a second says
 * nothing (a replay with no times), so it is undefined.
 */
export function useClock(work: Work | undefined): number | undefined {
  const live = !!work && work.running && !work.ended;
  const now = useNow(live);
  if (!work?.started) return undefined;
  const ms = (work.ended ?? (live ? now : work.started)) - work.started;
  return ms >= 1000 ? ms : undefined;
}
