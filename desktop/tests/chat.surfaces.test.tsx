// @vitest-environment jsdom
/**
 * The conversation's surfaces on the redesign's primitives: the plan in a build
 * transcript opens its sheet, the plan in a normal chat is a Facts card, the working
 * line has no shimmer, and a pane's "discard" question is a Dialog.
 */
import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { PlanCardView, Timeline } from '../src/components/chat/Timeline';
import { useConfirm } from '../src/components/ui/useConfirm';
import { emptyTimeline, type Item } from '../src/lib/timeline';
import { useApp } from '../src/store/app';
import { installFlowsHarness } from './helpers/flows';

installFlowsHarness();

const plan = (title: string): Item => ({
  kind: 'plan', key: `p-${title}`, title, footer: '',
  rows: [['task', 'Read a receipt and give back the total'], ['bar', '40 of 100']],
});

function transcript(items: Item[], running = false) {
  return render(
    <Timeline timeline={{ ...emptyTimeline(), items, running }} onAnswer={vi.fn()} onSend={vi.fn()} links={[]} compact />,
  );
}

describe('the plan in a build transcript', () => {
  it('is a one-line fold that opens the plan sheet', () => {
    useApp.setState({ forgeSheet: null });
    transcript([plan('Plan')]);
    fireEvent.click(document.querySelector('.plan-chip')!);
    expect(useApp.getState().forgeSheet).toBe('plan');
  });

  it('opens the report sheet once it is a result', () => {
    useApp.setState({ forgeSheet: null });
    transcript([plan('Result: passed')]);
    fireEvent.click(document.querySelector('.plan-chip')!);
    expect(useApp.getState().forgeSheet).toBe('report');
  });
});

describe('the plan in a normal chat', () => {
  it('is a compact card of facts, value over label', () => {
    render(<PlanCardView title="Plan" rows={[['task', 'Read a receipt'], ['bar', '40 of 100']]} footer="" />);
    expect(document.querySelector('.plan-card.ui-card')).toBeTruthy();
    expect(document.querySelectorAll('.plan-card .facts-cell').length).toBeGreaterThan(0);
    expect(document.querySelector('.plan-card .kv-row')).toBeNull();
  });
});

describe('the working line', () => {
  it('leads with the chip and does not shimmer', () => {
    transcript([], true);
    const row = screen.getByRole('status');
    expect(row.querySelector('.tl-chip')).toBeTruthy();
    expect(document.querySelector('.chat-shimmer')).toBeNull();
  });

  it('gives each user message its block, not a bubble radius token', () => {
    transcript([{ kind: 'user', key: 'u1', text: 'Hello' } as Item]);
    expect(document.querySelector('.msg-user-bubble')!.textContent).toBe('Hello');
    expect(screen.getByRole('button', { name: 'Copy' }).className).toContain('ui-icon-button');
  });
});

describe('a discard question in a pane', () => {
  function Harness({ onResult }: { onResult: (ok: boolean) => void }) {
    const [ask, dialog] = useConfirm();
    return (
      <>
        <button type="button" onClick={() => void ask({ title: 'Discard your edits to a.py?', keep: 'Keep editing', action: 'Discard' }).then(onResult)}>Go</button>
        {dialog}
      </>
    );
  }

  it('asks in a Dialog whose buttons name the action, and resolves with the choice', async () => {
    const onResult = vi.fn();
    render(<Harness onResult={onResult} />);
    fireEvent.click(screen.getByText('Go'));
    const dialog = screen.getByRole('dialog', { name: 'Discard your edits to a.py?' });
    expect(dialog).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Keep editing' })).toBeTruthy();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Discard' }));
    });
    expect(onResult).toHaveBeenCalledWith(true);
  });

  it('resolves false when kept', async () => {
    const onResult = vi.fn();
    render(<Harness onResult={onResult} />);
    fireEvent.click(screen.getByText('Go'));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Keep editing' }));
    });
    expect(onResult).toHaveBeenCalledWith(false);
  });
});
