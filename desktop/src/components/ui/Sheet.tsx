/**
 * The sheet (Lectern's sheet; design language §2.6): a 480 panel at the
 * window's right edge, inset 8 so its corners read as a card, rounded-card, the overlay
 * shadow, over a scrim with no blur. It runs beside the page rather than over its
 * middle (the build's Plan and Report). The header and footer are always whole; the body
 * scrolls. It enters over 200ms (fade, 8px in from the right) and leaves over 150. The
 * root carries `data-sheet` so a test or a film can find it.
 */
import { IconX } from '@tabler/icons-react';
import clsx from 'clsx';
import { useId, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { useOverlay } from './overlay';

export interface SheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  /** `data-sheet` on the panel: "plan", "report". */
  name: string;
  /** Announced to a screen reader, never shown. */
  description?: string;
  /** One primary, at the end. */
  footer?: ReactNode;
  /** One meta value at the footer's start (why the primary waits). */
  footerStart?: ReactNode;
  children?: ReactNode;
  className?: string;
}

export function Sheet({ open, onOpenChange, title, name, description, footer, footerStart, children, className }: SheetProps) {
  const panel = useRef<HTMLDivElement>(null);
  const { mounted, closing } = useOverlay(open, () => onOpenChange(false), panel);
  const titleId = useId();
  const descId = useId();
  if (!mounted) return null;
  return createPortal(
    <div className={clsx('ui-overlay', 'is-sheet', closing && 'is-closing')}>
      <div className="ui-scrim" aria-hidden onMouseDown={() => onOpenChange(false)} />
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descId : undefined}
        tabIndex={-1}
        data-sheet={name}
        className={clsx('ui-sheet', className)}
      >
        <div className="ui-overlay-head">
          <h2 id={titleId} className="ui-overlay-title">{title}</h2>
          {description ? <p id={descId} className="sr-only">{description}</p> : null}
          <button type="button" className="ui-overlay-close" aria-label="Close" onClick={() => onOpenChange(false)}>
            <IconX size={16} stroke={1.5} aria-hidden />
          </button>
        </div>
        <div className="ui-overlay-body ui-sheet-body">{children}</div>
        {footer ? (
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
