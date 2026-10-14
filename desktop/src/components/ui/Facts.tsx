/**
 * Facts (Lectern's facts; design language §4.1): what is true
 * about this thing, each value over its label. One column under a 400px card, two from
 * 400, three from 640 (`columns="auto"`); 16 between rows, 20 between columns. A `wide`
 * cell spans the row (the job sentence). Never a stack of label: value rows.
 */
import clsx from 'clsx';
import type { ReactNode } from 'react';

export interface Fact {
  value: ReactNode;
  /** Three words or fewer. */
  label: string;
  /** Span the whole row. */
  wide?: boolean;
  /** A stable key; the label is used when it is unique. */
  id?: string;
}

export interface FactsProps {
  items: Fact[];
  /** 'auto': 1, 2, then 3 columns as the card widens. 2: 1 then 2. 1: always one. */
  columns?: 'auto' | 1 | 2 | 3;
  className?: string;
}

export function Facts({ items, columns = 2, className }: FactsProps) {
  if (items.length === 0) return null;
  return (
    <dl data-body="facts" className={clsx('facts', `facts-cols-${columns}`, className)}>
      {items.map((fact, i) => (
        <div key={fact.id ?? `${fact.label}-${i}`} className={clsx('facts-cell', fact.wide && 'is-wide')}>
          <dt className="facts-label">{fact.label}</dt>
          <dd className="facts-value">{fact.value}</dd>
        </div>
      ))}
    </dl>
  );
}
