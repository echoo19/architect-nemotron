/**
 * The segmented control (Lectern's segmented): one look, two meanings.
 *
 *   radio  an in-place choice of one of two to four, all visible: role radiogroup,
 *          items role radio with aria-checked (the interview's question).
 *   tabs   switches the view under it: role tablist, items role tab with aria-selected.
 *
 * A muted track (rounded-md, 2px inset, 32 or 36 tall) with one raised thumb that slides
 * under the labels over 200ms, and only when the selection changes: it is placed without
 * a transition on mount and on a resize. Words or icons, never both: an option with an
 * icon shows the icon alone with its label as name and tooltip. Arrow keys move and
 * select; Home and End jump.
 */
import type { Icon } from '@tabler/icons-react';
import clsx from 'clsx';
import { useCallback, useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react';

export interface SegmentedOption {
  value: string;
  label: string;
  /** Shown instead of the label (the label becomes its name and tooltip). */
  icon?: Icon;
  disabled?: boolean;
}

export interface SegmentedProps {
  kind?: 'radio' | 'tabs';
  /** The control's accessible name. */
  label: string;
  options: SegmentedOption[];
  value: string | undefined;
  onChange: (value: string) => void;
  /** sm 32, md 36. */
  size?: 'sm' | 'md';
  /** Full width, the options sharing it. */
  fill?: boolean;
  /** tabs: the id of the panel an option controls. */
  panelId?: (value: string) => string;
  className?: string;
}

export function Segmented({ kind = 'radio', label, options, value, onChange, size = 'sm', fill = false, panelId, className }: SegmentedProps) {
  const root = useRef<HTMLDivElement>(null);
  const [thumb, setThumb] = useState<{ x: number; w: number } | null>(null);
  const [animate, setAnimate] = useState(false);
  const last = useRef<string | undefined>(value);

  const measure = useCallback(() => {
    const el = root.current?.querySelector<HTMLElement>('[data-state="on"]');
    setThumb(el ? { x: el.offsetLeft, w: el.offsetWidth } : null);
  }, []);

  useLayoutEffect(() => {
    // Slide only when the selection moved from one option to another.
    setAnimate(last.current !== undefined && value !== undefined && last.current !== value);
    last.current = value;
    measure();
  }, [value, measure]);

  useLayoutEffect(() => {
    const el = root.current;
    if (!el || typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(() => {
      setAnimate(false);
      measure();
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [measure]);

  const enabled = options.filter((o) => !o.disabled);
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const at = enabled.findIndex((o) => o.value === value);
    let next = -1;
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (at + 1) % enabled.length;
    else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (at - 1 + enabled.length) % enabled.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = enabled.length - 1;
    if (next < 0 || !enabled[next]) return;
    event.preventDefault();
    onChange(enabled[next].value);
    const target = enabled[next].value;
    requestAnimationFrame(() => [...(root.current?.querySelectorAll<HTMLElement>('[data-value]') ?? [])].find((el) => el.dataset.value === target)?.focus());
  };

  const radio = kind === 'radio';
  const focusable = options.some((o) => o.value === value) ? value : enabled[0]?.value;
  return (
    <div
      ref={root}
      role={radio ? 'radiogroup' : 'tablist'}
      aria-label={label}
      data-kind={kind}
      className={clsx('ui-seg', `ui-seg-${size}`, fill && 'ui-seg-fill', className)}
      onKeyDown={onKeyDown}
    >
      {thumb ? (
        <span
          aria-hidden
          className={clsx('ui-seg-thumb', animate && 'is-moving')}
          style={{ transform: `translateX(${thumb.x}px)`, width: thumb.w }}
        />
      ) : null}
      {options.map((option) => {
        const on = option.value === value;
        const Glyph = option.icon;
        return (
          <button
            key={option.value}
            type="button"
            role={radio ? 'radio' : 'tab'}
            aria-checked={radio ? on : undefined}
            aria-selected={radio ? undefined : on}
            aria-controls={!radio && panelId ? panelId(option.value) : undefined}
            aria-label={Glyph ? option.label : undefined}
            data-tip={Glyph ? option.label : undefined}
            data-tip-side={Glyph ? 'down' : undefined}
            data-state={on ? 'on' : 'off'}
            data-value={option.value}
            tabIndex={option.value === focusable ? 0 : -1}
            disabled={option.disabled}
            className="ui-seg-item"
            onClick={() => { if (!on) onChange(option.value); }}
          >
            {Glyph ? <Glyph size={16} stroke={1.5} aria-hidden /> : option.label}
          </button>
        );
      })}
    </div>
  );
}
