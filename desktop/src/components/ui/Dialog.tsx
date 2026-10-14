/**
 * The dialog (Lectern's dialog; design language §2.6, §9): centred over
 * a scrim (no blur), rounded-card, no edge, the overlay shadow. The title is the
 * question; a description is announced, never shown. Header 20 top, 24 sides, 12 under;
 * body 4 top, 24 sides and bottom; footer under a hairline, 16 top and 20 bottom, its
 * buttons at the end, 8 apart. It enters from 0.97 and 8px low over 200ms and leaves
 * over 150. Escape and the close button close it; focus stays inside while it is open
 * and goes back where it was when it closes.
 */
import { IconX } from '@tabler/icons-react';
import clsx from 'clsx';
import { useId, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { useOverlay } from './overlay';

export interface DialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The question, as the title. */
  title: string;
  /** Announced to a screen reader, never shown. */
  description?: string;
  /** sm 384, md 512, lg 672. */
  size?: 'sm' | 'md' | 'lg';
  /** The buttons, end-aligned: the safe one first, the action last. */
  footer?: ReactNode;
  /** Something at the footer's start (a link button). */
  footerStart?: ReactNode;
  hideClose?: boolean;
  children?: ReactNode;
  className?: string;
  /** Close when the scrim is clicked. */
  dismissOnScrim?: boolean;
}

export function Dialog({ open, onOpenChange, title, description, size = 'md', footer, footerStart, hideClose = false, children, className, dismissOnScrim = true }: DialogProps) {
  const panel = useRef<HTMLDivElement>(null);
  const { mounted, closing } = useOverlay(open, () => onOpenChange(false), panel);
  const titleId = useId();
  const descId = useId();
  if (!mounted) return null;
  return createPortal(
    <div className={clsx('ui-overlay', closing && 'is-closing')}>
      <div className="ui-scrim" aria-hidden onMouseDown={() => { if (dismissOnScrim) onOpenChange(false); }} />
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descId : undefined}
        tabIndex={-1}
        data-dialog=""
        className={clsx('ui-dialog', `ui-dialog-${size}`, className)}
      >
        <div className="ui-overlay-head">
          <h2 id={titleId} className="ui-overlay-title">{title}</h2>
          {description ? <p id={descId} className="sr-only">{description}</p> : null}
          {hideClose ? null : (
            <button type="button" className="ui-overlay-close" aria-label="Close" onClick={() => onOpenChange(false)}>
              <IconX size={16} stroke={1.5} aria-hidden />
            </button>
          )}
        </div>
        {children != null && children !== false ? <div className="ui-overlay-body">{children}</div> : null}
        {footer || footerStart ? (
          <div className="ui-overlay-footer">
            {footerStart ? <div className="ui-overlay-footer-start">{footerStart}</div> : null}
            <div className="ui-overlay-footer-actions">{footer}</div>
          </div>
        ) : null}
      </div>
    </div>,
    document.body,
  );
}
