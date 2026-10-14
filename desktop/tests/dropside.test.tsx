// @vitest-environment jsdom
/** A dropdown opens toward the room it has, and its chevron says which way. */
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { useDropSide } from '../src/lib/useDropSide';

function Trigger({ top }: { top: number }) {
  const drop = useDropSide<HTMLDivElement>(200);
  return (
    <div
      ref={(el) => {
        if (el) el.getBoundingClientRect = () => ({ top, bottom: top + 30, left: 0, right: 100, width: 100, height: 30, x: 0, y: top, toJSON: () => ({}) });
        (drop.ref as React.MutableRefObject<HTMLDivElement | null>).current = el;
      }}
    >
      <button type="button" onClick={drop.measure}>{drop.side}</button>
    </div>
  );
}

describe('useDropSide', () => {
  afterEach(cleanup);

  it('opens down with room below and up near the bottom of the window', () => {
    Object.defineProperty(window, 'innerHeight', { value: 900, configurable: true });
    render(<><Trigger top={100} /><Trigger top={820} /></>);
    const [high, low] = screen.getAllByRole('button');
    act(() => { high.click(); low.click(); });
    expect(high.textContent).toBe('down');
    expect(low.textContent).toBe('up');
  });
});
