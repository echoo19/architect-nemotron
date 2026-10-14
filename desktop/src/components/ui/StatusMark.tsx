/**
 * A status as a shape in its colour (Lectern's status mark; design
 * language §2.7). Each tone has its own outline, so the state survives greyscale:
 *
 *   ok       circle with a check      warn     triangle
 *   err      circle with a cross      neutral  dashed circle
 *   accent   circle with a dot (live)
 *
 * The word is the accessible name and the tooltip. `showLabel` prints it beside the
 * glyph: only in a card header's meta, where the status is the thing's condition.
 */
import {
  IconAlertTriangle, IconCircleCheck, IconCircleDashed, IconCircleDot, IconCircleX, type Icon,
} from '@tabler/icons-react';
import clsx from 'clsx';

export type StatusTone = 'ok' | 'warn' | 'err' | 'neutral' | 'accent';

export const STATUS_GLYPH: Record<StatusTone, Icon> = {
  ok: IconCircleCheck,
  warn: IconAlertTriangle,
  err: IconCircleX,
  neutral: IconCircleDashed,
  accent: IconCircleDot,
};

export interface StatusMarkProps {
  tone: StatusTone;
  /** The status word: "Passed", "Did not pass", "Set aside". */
  label: string;
  /** A domain glyph in place of the tone's own. */
  icon?: Icon;
  /** sm 14, md 16. */
  size?: 'sm' | 'md';
  /** Header meta only: the word beside the glyph. */
  showLabel?: boolean;
  className?: string;
}

export function StatusMark({ tone, label, icon, size = 'md', showLabel = false, className }: StatusMarkProps) {
  const Glyph = icon ?? STATUS_GLYPH[tone];
  const px = size === 'sm' ? 14 : 16;
  if (showLabel) {
    return (
      <span data-status-mark={tone} className={clsx('ui-status', 'has-label', `is-${tone}`, className)}>
        <span className="ui-status-glyph"><Glyph size={px} stroke={1.5} aria-hidden /></span>
        <span className="ui-status-word">{label}</span>
      </span>
    );
  }
  return (
    <span
      data-status-mark={tone}
      role="img"
      aria-label={label}
      data-tip={label}
      className={clsx('ui-status', `is-${tone}`, className)}
    >
      <Glyph size={px} stroke={1.5} aria-hidden />
    </span>
  );
}
