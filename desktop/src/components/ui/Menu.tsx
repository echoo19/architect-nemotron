/**
 * A small popover menu: opens from a trigger, arrow keys move, Enter picks,
 * Escape closes, typing filters when there are many items. Rendered in place
 * (absolutely positioned above or below its anchor).
 */
import clsx from 'clsx';
import { IconCheck, type Icon } from '@tabler/icons-react';
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';

export interface MenuItem {
  id: string;
  label: string;
  hint?: string;
  detail?: string;
  selected?: boolean;
  disabled?: boolean;
  section?: string;
  icon?: Icon;
}

export function Menu({
  items, onPick, onClose, side = 'up', align = 'start', filter = false, width, empty = 'Nothing here', footer, passive = false, onQuery,
}: {
  items: MenuItem[];
  onPick: (id: string) => void;
  onClose: () => void;
  side?: 'up' | 'down';
  align?: 'start' | 'end';
  filter?: boolean;
  width?: number;
  empty?: string;
  footer?: ReactNode;
  /** Keep focus where it is (the composer) and take only the navigation keys. */
  passive?: boolean;
  /** Rank items yourself: the menu reports the query instead of filtering. */
  onQuery?: (query: string) => void;
}) {
  const [query, setQuery] = useState('');
  const shown = useMemo(() => {
    if (!query || onQuery) return items;
    const q = query.toLowerCase();
    return items.filter((i) => `${i.label} ${i.hint ?? ''} ${i.detail ?? ''}`.toLowerCase().includes(q));
  }, [items, query, onQuery]);
  const [index, setIndex] = useState(() => Math.max(0, items.findIndex((i) => i.selected)));
  const root = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!passive) (filter ? input.current : root.current)?.focus();
  }, [filter, passive]);

  useEffect(() => {
    if (index >= shown.length) setIndex(Math.max(0, shown.length - 1));
  }, [shown.length, index]);

  useEffect(() => {
    if (!passive) return;
    const onKey = (event: KeyboardEvent) => {
      if (!['ArrowDown', 'ArrowUp', 'Enter', 'Tab', 'Escape'].includes(event.key)) return;
      if (event.key === 'Enter' && event.shiftKey) return;
      if (event.key === 'Tab' && event.shiftKey) return;
      event.preventDefault();
      event.stopPropagation();
      if (event.key === 'ArrowDown') setIndex((i) => Math.min(shown.length - 1, i + 1));
      else if (event.key === 'ArrowUp') setIndex((i) => Math.max(0, i - 1));
      else if (event.key === 'Escape') onClose();
      else {
        const item = shown[index];
        if (item && !item.disabled) onPick(item.id);
        else onClose();
      }
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [passive, shown, index, onPick, onClose]);

  useEffect(() => {
    const onDown = (event: MouseEvent) => {
      if (root.current && !root.current.contains(event.target as Node)) onClose();
    };
    window.addEventListener('mousedown', onDown);
    return () => window.removeEventListener('mousedown', onDown);
  }, [onClose]);

  useEffect(() => {
    root.current?.querySelector('[data-active="true"]')?.scrollIntoView({ block: 'nearest' });
  }, [index]);

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setIndex((i) => Math.min(shown.length - 1, i + 1));
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      setIndex((i) => Math.max(0, i - 1));
    } else if (event.key === 'Enter') {
      event.preventDefault();
      const item = shown[index];
      if (item && !item.disabled) onPick(item.id);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      onClose();
    } else if (/^[1-9]$/.test(event.key) && !filter) {
      const item = shown[Number(event.key) - 1];
      if (item && !item.disabled) onPick(item.id);
    }
  };

  let lastSection: string | undefined;
  return (
    <div
      ref={root}
      className={clsx('menu', `menu-${side}`, `menu-${align}`)}
      style={width ? { width } : undefined}
      role="menu"
      tabIndex={-1}
      onKeyDown={onKeyDown}
    >
      {filter && (
        <input
          ref={input}
          className="menu-filter"
          placeholder="Search"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            onQuery?.(e.target.value);
            setIndex(0);
          }}
        />
      )}
      <div className="menu-items">
        {shown.length === 0 && <div className="menu-empty">{empty}</div>}
        {shown.map((item, i) => {
          const heading = item.section && item.section !== lastSection ? item.section : null;
          lastSection = item.section;
          return (
            <div key={item.id}>
              {heading && <div className="menu-section">{heading}</div>}
              <button
                type="button"
                role="menuitem"
                className={clsx('menu-item', item.selected && 'is-selected')}
                data-active={i === index}
                disabled={item.disabled}
                onMouseEnter={() => setIndex(i)}
                onClick={() => onPick(item.id)}
              >
                {item.icon && <item.icon size={16} stroke={1.8} className="menu-icon" aria-hidden />}
                <span className="menu-label">
                  {item.label}
                  {item.detail && <span className="menu-detail">{item.detail}</span>}
                </span>
                {item.hint && <span className="menu-hint">{item.hint}</span>}
                {item.selected && <IconCheck size={15} className="menu-check" aria-hidden />}
              </button>
            </div>
          );
        })}
      </div>
      {footer && <div className="menu-footer">{footer}</div>}
    </div>
  );
}
