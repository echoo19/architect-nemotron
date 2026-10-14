/**
 * A compact meter (Lectern's meter): 4px tall, full radius, the fill on
 * a sink track. Data, never decoration: it sits beside the number it encodes or carries
 * it as its name. The fill is the full bar revealed by `clip-path: inset()`, so the round
 * caps never squash; it renders at its value and wipes over 200ms only when the value
 * changes. A non-zero value never draws thinner than 4px.
 */
import clsx from 'clsx';
import type { CSSProperties } from 'react';

export type MeterTone = 'accent' | 'ok' | 'warn' | 'err' | 'neutral';

function reveal(frac: number): CSSProperties {
  if (frac <= 0) return { clipPath: 'inset(0 100% 0 0 round 9999px)' };
  if (frac >= 1) return { clipPath: 'inset(0 0 0 0 round 9999px)' };
  return { clipPath: `inset(0 calc(100% - max(${(frac * 100).toFixed(3)}%, 4px)) 0 0 round 9999px)` };
}

export interface MeterProps {
  value: number;
  /** With max omitted, value is a 0 to 1 fraction. */
  max?: number;
  tone?: MeterTone;
  /** row: 48 wide, beside its figure. fill: the width of its slot. */
  size?: 'row' | 'fill';
  /** The accessible name: what is being filled. */
  label: string;
  className?: string;
}

export function Meter({ value, max = 1, tone = 'accent', size = 'fill', label, className }: MeterProps) {
  const safe = max > 0 ? max : 1;
  const frac = Math.min(1, Math.max(0, value / safe));
  return (
    <div
      role="progressbar"
      aria-valuenow={Math.round(frac * 100)}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={label}
      data-meter={size}
      className={clsx('ui-meter', `ui-meter-${size}`, className)}
    >
      <div className={clsx('ui-meter-fill', `is-${tone}`)} style={reveal(frac)} />
    </div>
  );
}
