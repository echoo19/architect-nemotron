/**
 * The button (Lectern's button; design language §11).
 *
 *   primary      accent fill: the page's next step, once per card
 *   outline      a border on the ground, no shadow (the default)
 *   ghost        no frame; bg-muted on hover
 *   destructive  err fill
 *   link         text only
 *
 * Heights match the fields (xs 28, sm 32, md 36, lg 40). Colour moves over 150ms on
 * --ease, a press scales to 0.97, nothing moves on hover. Focus is the solid accent
 * ring, 2px off the edge. `pending` leads with the Working mark and keeps the label.
 * `data-action` and any other attribute pass through to the <button>.
 */
import type { Icon } from '@tabler/icons-react';
import clsx from 'clsx';
import { forwardRef, type ButtonHTMLAttributes } from 'react';
import { Working } from './Working';

export type ButtonVariant = 'primary' | 'outline' | 'ghost' | 'destructive' | 'link';
export type ButtonSize = 'xs' | 'sm' | 'md' | 'lg';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** A request this button started is open: the Working mark leads, the button is disabled and busy. */
  pending?: boolean;
  /** Full width. */
  full?: boolean;
  /** A shortcut shown after the label, e.g. "Enter". */
  kbd?: string;
}

const PENDING_SIZE = { xs: 'xs', sm: 'sm', md: 'sm', lg: 'md' } as const;

export function buttonClass(variant: ButtonVariant = 'outline', size: ButtonSize = 'md', full = false): string {
  return clsx('ui-button', `ui-button-${variant}`, `ui-button-${size}`, full && 'ui-button-full');
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = 'outline', size = 'md', pending = false, full = false, kbd, className, children, disabled, type, ...props },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type ?? 'button'}
      className={clsx(buttonClass(variant, size, full), className)}
      disabled={pending || disabled}
      aria-busy={pending || undefined}
      {...props}
    >
      {pending ? <Working size={PENDING_SIZE[size]} className="ui-button-working" /> : null}
      {children}
      {kbd ? <kbd className="ui-button-kbd">{kbd}</kbd> : null}
    </button>
  );
});

export interface IconButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'children' | 'aria-label'> {
  icon: Icon;
  /** The verb, as aria-label and tooltip: "Transcript", "More actions". */
  label: string;
  /** xs 28 (a card header's trailing slot), sm 32, md 36. */
  size?: 'xs' | 'sm' | 'md';
  variant?: 'ghost' | 'outline';
  pending?: boolean;
  /** Turn the tooltip off (a menu opens from it). */
  tooltip?: boolean;
  /** Where the tooltip opens. */
  tipSide?: 'up' | 'down' | 'right';
}

/** An icon-only button for an unambiguous verb: the glyph at 16, the verb as name and tooltip. */
export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { icon: Glyph, label, size = 'xs', variant = 'ghost', pending = false, tooltip = true, tipSide = 'down', className, disabled, type, ...props },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type ?? 'button'}
      aria-label={label}
      aria-busy={pending || undefined}
      disabled={pending || disabled}
      data-tip={tooltip ? label : undefined}
      data-tip-side={tooltip && tipSide !== 'up' ? tipSide : undefined}
      className={clsx('ui-icon-button', `ui-icon-button-${variant}`, `ui-icon-button-${size}`, className)}
      {...props}
    >
      {pending ? <Working size="sm" /> : <Glyph size={16} stroke={1.5} aria-hidden />}
    </button>
  );
});
