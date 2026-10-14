/**
 * The page header (Lectern's PageHeader): one row, at least 36
 * tall. The h1 is heading size and truncates; a count Badge may sit beside it; the meta
 * is body, mute, tabular; actions sit at the end, 8 apart.
 */
import clsx from 'clsx';
import type { ReactNode } from 'react';
import { Badge } from './Badge';

export interface PageHeaderProps {
  title: ReactNode;
  count?: number;
  meta?: ReactNode;
  /** At most a few: an IconButton, a Button size="sm". */
  actions?: ReactNode;
  className?: string;
}

export function PageHeader({ title, count, meta, actions, className }: PageHeaderProps) {
  return (
    <header data-page-header="" className={clsx('ui-page-header', className)}>
      <div className="ui-page-header-id">
        <h1 className="ui-page-title">{title}</h1>
        {count !== undefined ? <Badge>{count}</Badge> : null}
        {meta != null && meta !== false ? <div className="ui-page-meta">{meta}</div> : null}
      </div>
      {actions != null && actions !== false ? <div className="ui-page-actions">{actions}</div> : null}
    </header>
  );
}
