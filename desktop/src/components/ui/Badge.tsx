/**
 * A count (Lectern's badge): 20 tall, rounded-sm, micro tabular.
 * `count` is the actionable count (items waiting on this person: Needs you), accent
 * filled; a count that only describes, or a short neutral tag ("Recommended"), is
 * `neutral`. A status word is a StatusMark, not a badge.
 */
import clsx from 'clsx';
import type { HTMLAttributes } from 'react';

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  variant?: 'neutral' | 'count';
}

export function Badge({ variant = 'neutral', className, ...props }: BadgeProps) {
  return <span data-badge={variant} className={clsx('ui-badge', `ui-badge-${variant}`, className)} {...props} />;
}
