/**
 * Lectern's working mark: the one indeterminate loader, ported from Lectern's
 * design system (LecternWorkingMark, Working, WorkingRegion and the mark's
 * geometry). The motion is in styles/motion.css. © Lectern, MIT: see
 * THIRD_PARTY_NOTICES.md.
 *
 * Mount it only while a request is open; unmount the moment the result lands. It is
 * held 120ms before it shows (motion.css), so a fast answer never flashes it; a caller
 * that can keep it a little longer uses `useWorkingShown` so it stays at least 300ms.
 */
import clsx from 'clsx';
import { useEffect, useRef, useState } from 'react';

/** The mark's ink inside its 100x100 space: x 25..75, y 16..82. */
export const INK_BOX = '25 16 50 66';
/** The bars the loader retracts. The stem is not drawn on its own any more. */
export const BOARD = 'M25 23 L57 16 L57 29 L25 36 Z';
export const FOOT = 'M43 69 L75 62 L75 75 L43 82 Z';
/**
 * The loader's middle: the stem plus the parts of the board and the foot that lie over
 * it, the mark's column x 43..57 as one shape (Lectern's WORKING_COLUMN). It is drawn
 * last and holds both joints, and motion.css wipes the bars 2 units past its edges, so
 * a clip edge snapped to the device-pixel grid lands under solid ink instead of leaving
 * a notch in the line state. With the bars it is the same silhouette as MARK_OUTLINE.
 */
export const WORKING_COLUMN = 'M43 19.0625 L57 16 L57 78.9375 L43 82 Z';
/**
 * The still mark as one path: board, stem and foot traced as their union, the same
 * silhouette in the same space (Lectern's traced outline). Three overlapping paths are
 * anti-aliased one at a time, which left a faint seam and a nub at each joint; one
 * path has one edge. Every still render draws this. build/icon.svg and the site's
 * icons.mjs carry the same string; change them together.
 */
export const MARK_OUTLINE = 'M25 23 L57 16 L57 65.9375 L75 62 L75 75 L43 82 L43 32.0625 L25 36 Z';

export function WorkingMark({ className, label }: { className?: string; label?: string }) {
  return (
    <svg
      viewBox={INK_BOX}
      className={clsx('lectern-working', className)}
      fill="currentColor"
      role={label ? 'status' : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
    >
      {label ? <title>{label}</title> : null}
      <path className="lectern-working-board" d={BOARD} />
      <path className="lectern-working-foot" d={FOOT} />
      {/* Last, so the bars' clipped ends sit under solid ink and leave no seam. */}
      <path className="lectern-working-column" d={WORKING_COLUMN} />
      {/* Shown only under reduced motion, in place of the three parts (motion.css). */}
      <path className="lectern-working-still" d={MARK_OUTLINE} />
    </svg>
  );
}

/** The still mark, as one outline (the app's own icon draws the same path). */
export function Mark({ className }: { className?: string }) {
  return (
    <svg viewBox={INK_BOX} className={className} fill="currentColor" aria-hidden>
      <path d={MARK_OUTLINE} />
    </svg>
  );
}

const SIZES = { xs: 'working-xs', sm: 'working-sm', md: 'working-md', lg: 'working-lg' } as const;

export function Working({ size = 'sm', className, label }: { size?: keyof typeof SIZES; className?: string; label?: string }) {
  return <WorkingMark className={clsx(SIZES[size], 'text-accent', className)} label={label} />;
}

/** A region waiting on one request with nothing honest to draw yet. */
export function WorkingRegion({ label = 'Loading', className }: { label?: string; className?: string }) {
  return (
    <div role="status" aria-busy="true" className={clsx('working-region', className)}>
      <WorkingMark className="working-lg text-accent" />
      <span>{label}</span>
    </div>
  );
}

/**
 * Whether a Working mark should be on screen for `active`: not before `hold` ms have
 * passed (a fast answer never flashes it), and once shown, for at least `min` ms.
 */
export function useWorkingShown(active: boolean, { hold = 120, min = 300 }: { hold?: number; min?: number } = {}): boolean {
  const [shown, setShown] = useState(false);
  const since = useRef(0);
  useEffect(() => {
    if (active && !shown) {
      const t = setTimeout(() => { since.current = Date.now(); setShown(true); }, hold);
      return () => clearTimeout(t);
    }
    if (!active && shown) {
      const left = Math.max(0, min - (Date.now() - since.current));
      const t = setTimeout(() => setShown(false), left);
      return () => clearTimeout(t);
    }
    return undefined;
  }, [active, shown, hold, min]);
  return shown;
}
