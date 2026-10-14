/** A collapsible section of a details page (moved from forge/DetailsView). */
import { IconChevronRight, type Icon } from '@tabler/icons-react';
import clsx from 'clsx';
import { useState } from 'react';

export function Section({ title, children, defaultOpen = true, side, icon: SectionIcon }: { title: string; children: React.ReactNode; defaultOpen?: boolean; side?: React.ReactNode; icon?: Icon }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section className="details-section">
      <div className="details-head">
        <button type="button" className="details-toggle" onClick={() => setOpen(!open)} aria-expanded={open}>
          {SectionIcon && <span className="section-icon"><SectionIcon size={15} stroke={1.8} aria-hidden /></span>}
          <h2>{title}</h2>
          <IconChevronRight size={14} className={clsx('work-chevron section-chevron', open && 'is-open')} aria-hidden />
        </button>
        {side}
      </div>
      {open && <div className="details-body">{children}</div>}
    </section>
  );
}
