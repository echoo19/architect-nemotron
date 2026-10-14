/**
 * A row list (Lectern's row list; design language §4.1): which
 * ones, in what order. Rows run edge to edge with a hairline between them and carry the
 * card's inset themselves; 48 tall for one line, 60 for two. One kind of leading per
 * list (a 16px glyph, a StatusMark). The title and the meta truncate.
 */
import clsx from 'clsx';
import { Children, type ReactNode } from 'react';

export interface RowProps {
  title: ReactNode;
  /** A second line: data, never a sentence. */
  meta?: ReactNode;
  /** One leading visual, the same kind for every row in the list. */
  leading?: ReactNode;
  /** A value, a StatusMark, a Badge or a small Button. */
  trailing?: ReactNode;
  /** Makes the row a button. */
  onSelect?: () => void;
  /** The row the detail is showing (`aria-current`). */
  selected?: boolean;
  id?: string;
  className?: string;
}

export function Row({ title, meta, leading, trailing, onSelect, selected = false, id, className }: RowProps) {
  const hasMeta = meta != null && meta !== false;
  const text = (
    <>
      <span className="row-title">{title}</span>
      {hasMeta ? <span className="row-meta">{meta}</span> : null}
    </>
  );
  return (
    <div
      role="listitem"
      data-row={id ?? ''}
      aria-current={selected ? 'true' : undefined}
      className={clsx('row', hasMeta && 'has-meta', onSelect && 'is-interactive', selected && 'is-selected', className)}
    >
      {leading != null && leading !== false ? <span className="row-leading">{leading}</span> : null}
      <div className="row-text">
        {onSelect ? <button type="button" className="row-target" onClick={onSelect}>{text}</button> : text}
      </div>
      {trailing != null && trailing !== false ? <div className="row-trailing">{trailing}</div> : null}
    </div>
  );
}

/** Heads a run of rows: small, medium weight, mute; 16 above. */
export function RowGroup({ label, count, action }: { label: ReactNode; count?: number; action?: ReactNode }) {
  return (
    <div role="presentation" data-row-group="" className="row-group">
      <span className="row-group-label">{label}</span>
      <span className="row-group-side">
        {count != null ? <span className="row-group-count">{count}</span> : null}
        {action}
      </span>
    </div>
  );
}

export interface RowListProps {
  /** Rows and RowGroups. */
  children: ReactNode;
  /** Shown when there is no row. */
  empty?: ReactNode;
  className?: string;
  'aria-label'?: string;
}

export function RowList({ children, empty = null, className, ...props }: RowListProps) {
  if (Children.toArray(children).length === 0) return <>{empty}</>;
  return (
    <div role="list" data-body="row-list" className={clsx('row-list', className)} aria-label={props['aria-label']}>
      {children}
    </div>
  );
}
