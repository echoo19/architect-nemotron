/**
 * The card (Lectern's card; design language §3): the one frame.
 *
 *   ┌───────────────────────────────────────────────┐  radius 16, 1px divider, bg-raised, no shadow
 *   │  Title                                     ⋯  │  header: inset sides, 16/20 top, 12 bottom
 *   │  receipt-reader · 4:12                        │  meta: data only, never a sentence
 *   │  64 / 100  Super ships                        │  lead (optional): heading size
 *   │  ░░░░░░░░░░░░░ one body ░░░░░░░░░░░░░░░░░░░░  │  body: inset sides, inset bottom
 *   │  $0.32                        [ Primary ]     │  footer: one meta value, one action
 *   └───────────────────────────────────────────────┘
 *
 * The card sits in a `card-cell`, the inline-size container its parts read: the side
 * inset (--card-x) is 16, then 20 from a 400px cell, then 24 from 640. `width` puts the
 * cell at a measure tier (form 640, list 1000), centred. Every other attribute
 * (`data-phase`, `className`, `aria-*`) lands on the card itself.
 */
import clsx from 'clsx';
import type { HTMLAttributes, ReactNode } from 'react';

export type CardWidth = 'form' | 'list';

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  /** A measure tier, centred: form 640, list 1000. Unset, the card fills its parent. */
  width?: CardWidth;
  /** `attention` draws the attention edge (an error the card is showing). */
  tone?: 'attention';
  /** Read once by a screen reader with the attention edge. */
  attentionNote?: string;
  /** Extra classes on the cell around the card. */
  cellClassName?: string;
}

export function Card({ width, tone, attentionNote, cellClassName, className, children, ...props }: CardProps) {
  return (
    <div className={clsx('card-cell', width && `card-cell-${width}`, cellClassName)}>
      <div
        data-card=""
        data-attention={tone === 'attention' ? '' : undefined}
        className={clsx('ui-card', tone === 'attention' && 'is-attention', className)}
        {...props}
      >
        {children}
        {tone === 'attention' && attentionNote ? <span className="sr-only">{attentionNote}</span> : null}
      </div>
    </div>
  );
}

export interface CardHeaderProps extends Omit<HTMLAttributes<HTMLDivElement>, 'title'> {
  /** The title: a noun naming the step, four words or fewer. */
  children: ReactNode;
  /** One line of data under the title: a name, a time, a status. Never a sentence. */
  meta?: ReactNode;
  /** One lead under the meta: `<CardLead/>`. */
  lead?: ReactNode;
  /** The trailing slot, at most two items: an IconButton, a Segmented. */
  action?: ReactNode;
  as?: 'h2' | 'h3';
}

export function CardHeader({ children, meta, lead, action, as: Heading = 'h2', className, ...props }: CardHeaderProps) {
  return (
    <div data-card-header="" className={clsx('ui-card-header', className)} {...props}>
      <div className="ui-card-titles">
        <Heading className="ui-card-title">{children}</Heading>
        {meta != null && meta !== false ? <div className="ui-card-meta">{meta}</div> : null}
        {lead != null && lead !== false ? <div className="ui-card-lead-slot">{lead}</div> : null}
      </div>
      {action != null && action !== false ? <div className="ui-card-action">{action}</div> : null}
    </div>
  );
}

export type CardBodyPad = 'card' | 'flush' | 'bleed';

export interface CardBodyProps extends HTMLAttributes<HTMLDivElement> {
  /** card: inset sides and bottom. flush: no sides (rows carry the inset), 8 top and bottom. bleed: edge to edge. */
  pad?: CardBodyPad;
}

export function CardBody({ pad = 'card', className, ...props }: CardBodyProps) {
  return <div data-card-body={pad} className={clsx('ui-card-body', `ui-card-body-${pad}`, className)} {...props} />;
}

export interface CardFooterProps {
  /** One meta value at the start: the build's cost, a status. */
  start?: ReactNode;
  /** A hairline above, after a flush body. */
  divided?: boolean;
  /** The children take the footer's full width as a column (a composer, a field with its button). */
  full?: boolean;
  /** One action: a Button size="sm". */
  children?: ReactNode;
  className?: string;
}

export function CardFooter({ start, divided = false, full = false, children, className }: CardFooterProps) {
  return (
    <div data-card-footer="" className={clsx('ui-card-footer', divided && 'is-divided', className)}>
      {start != null && start !== false ? <div className="ui-card-footer-start">{start}</div> : null}
      {children != null && children !== false ? (
        <div className={full ? 'ui-card-footer-full' : 'ui-card-footer-actions'}>{children}</div>
      ) : null}
    </div>
  );
}

export interface CardLeadProps {
  /** The one number the card states, in the heading size: "64 / 100". */
  value: ReactNode;
  /** A short line beside it: "Super ships". */
  note?: ReactNode;
  /** The note's tone. */
  tone?: 'neutral' | 'ok' | 'err';
  className?: string;
}

/** The header's lead: one heading-size figure, tabular, with an optional short note beside it. */
export function CardLead({ value, note, tone = 'neutral', className }: CardLeadProps) {
  return (
    <div data-card-lead="" className={clsx('ui-card-lead', className)}>
      <span className="ui-card-lead-value">{value}</span>
      {note != null && note !== false ? <span className={clsx('ui-card-lead-note', `is-${tone}`)}>{note}</span> : null}
    </div>
  );
}

export interface CardSectionProps {
  title: ReactNode;
  meta?: ReactNode;
  action?: ReactNode;
  footer?: ReactNode;
  children: ReactNode;
  id?: string;
  className?: string;
}

/** The card's anatomy with no frame: a section inside a sheet or a dialog. Sections stack 24 apart. */
export function CardSection({ title, meta, action, footer, children, id, className }: CardSectionProps) {
  return (
    <section id={id} data-card-section="" className={clsx('ui-card-section', className)}>
      <div className="ui-card-section-head">
        <div className="ui-card-titles">
          <h3 className="ui-card-title">{title}</h3>
          {meta != null && meta !== false ? <div className="ui-card-meta">{meta}</div> : null}
        </div>
        {action != null && action !== false ? <div className="ui-card-action">{action}</div> : null}
      </div>
      <div className="ui-card-section-body">{children}</div>
      {footer != null && footer !== false ? <div className="ui-card-section-footer">{footer}</div> : null}
    </section>
  );
}
