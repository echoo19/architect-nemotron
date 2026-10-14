// @vitest-environment jsdom
/**
 * Answering an approval or a question with the keyboard.
 * The bridge is replaced by a recorded `request` (tests/helpers/flows.tsx).
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { AskCard } from '../src/components/chat/AskCard';
import { type Item } from '../src/lib/timeline';
import { installFlowsHarness } from './helpers/flows';

installFlowsHarness();

describe('approvals', () => {
  const item: Extract<Item, { kind: 'ask' }> = {
    kind: 'ask', key: 'a1', closed: false,
    ask: { id: 1, kind: 'approval', action: 'bash', detail: 'npm test' },
  };

  it('Enter allows once and Escape declines, as in the terminal', () => {
    const onAnswer = vi.fn();
    render(<AskCard item={item} onAnswer={onAnswer} />);
    expect(screen.getByRole('heading', { name: 'Run a command' })).toBeTruthy();
    expect(screen.getByText('$ npm test')).toBeTruthy();
    const card = screen.getByRole('group');
    fireEvent.keyDown(card, { key: 'Enter' });
    fireEvent.keyDown(card, { key: 'Escape' });
    expect(onAnswer.mock.calls.map((c) => c[1])).toEqual(['yes', 'no']);
  });

  it('can allow for the rest of the conversation, and records the decision', () => {
    const onAnswer = vi.fn();
    const { rerender } = render(<AskCard item={item} onAnswer={onAnswer} />);
    fireEvent.click(screen.getByText('Allow for this conversation'));
    expect(onAnswer).toHaveBeenCalledWith(item.ask, 'session');
    rerender(<AskCard item={{ ...item, closed: true, answer: 'session' }} onAnswer={onAnswer} />);
    expect(screen.getByText('For this conversation')).toBeTruthy();
  });

  it('turns numbered interview choices into buttons with their labels', () => {
    const onAnswer = vi.fn();
    const question: Extract<Item, { kind: 'ask' }> = {
      kind: 'ask', key: 'a2', closed: false,
      ask: { id: 2, kind: 'question', question: 'Where will this agent live?\n  [1] Run it here\n  [2] Put it into software\nChoose', default: '1', choices: ['1', '2'] },
    };
    render(<AskCard item={question} onAnswer={onAnswer} />);
    fireEvent.click(screen.getByText('Put it into software'));
    expect(onAnswer).toHaveBeenCalledWith(question.ask, '2');
  });

  it('holds the export approval when a dialog answers it: one line, no buttons', () => {
    const onAnswer = vi.fn();
    const exportAsk: Extract<Item, { kind: 'ask' }> = {
      kind: 'ask', key: 'a3', closed: false,
      ask: { id: 3, kind: 'approval', action: 'export', detail: 'agents/receipt_reader/' },
    };
    render(<AskCard item={exportAsk} onAnswer={onAnswer} held />);
    const card = document.querySelector('.ask-card.is-held.is-approval') as HTMLElement;
    expect(card.textContent).toBe('Put the agent into your project');
    expect(screen.queryAllByRole('button')).toHaveLength(0);
    // It never takes focus from the dialog that owns it.
    expect(document.activeElement).not.toBe(card);
    expect(onAnswer).not.toHaveBeenCalled();
  });

  it('holds a numbered question the card answers: the prompt only', () => {
    const question: Extract<Item, { kind: 'ask' }> = {
      kind: 'ask', key: 'a4', closed: false,
      ask: { id: 4, kind: 'question', question: 'Where will this agent live?\n  [1] Run it here\n  [2] Put it into software\nChoose', default: '1', choices: ['1', '2'] },
    };
    render(<AskCard item={question} onAnswer={vi.fn()} held />);
    expect(document.querySelector('.ask-card.is-held')).toBeTruthy();
    expect(screen.queryAllByRole('button')).toHaveLength(0);
    expect(screen.queryByText('Put it into software')).toBeNull();
  });

  it('draws its buttons with the shared Button: Allow is the one primary', () => {
    render(<AskCard item={item} onAnswer={vi.fn()} />);
    const primaries = document.querySelectorAll('.ask-card .ui-button-primary');
    expect(primaries).toHaveLength(1);
    expect(primaries[0].textContent).toBe('AllowEnter');
  });
});

describe('the connect edit in the transcript', () => {
  it('keeps Allow for this conversation on an edit approval, with its diff, where the wizard step does not offer it', () => {
    const onAnswer = vi.fn();
    const edit: Extract<Item, { kind: 'ask' }> = {
      kind: 'ask', key: 'a5', closed: false,
      ask: { id: 5, kind: 'approval', action: 'edit', detail: 'C:/rec/expense-app/app/extract.py (2 edits)\n- return ask_general_model(text)\n+ return run(text)' },
    };
    render(<AskCard item={edit} onAnswer={onAnswer} />);
    expect(document.querySelector('.edit-preview')).toBeTruthy();
    fireEvent.click(screen.getByText('Allow for this conversation'));
    expect(onAnswer).toHaveBeenCalledWith(edit.ask, 'session');
  });

  it("says that saving an edit to an added agent's files stops the agent, and says nothing on any other edit", () => {
    const edit = (detail: string): Extract<Item, { kind: 'ask' }> => ({
      kind: 'ask', key: 'a3', closed: false, ask: { id: 3, kind: 'approval', action: 'edit', detail },
    });
    const { unmount } = render(<AskCard item={edit('agents/receipt_reader/artifact/config.json (1 edit)\n- "model": "a"\n+ "model": "b"')} onAnswer={vi.fn()} />);
    expect(screen.getByText('Saving this stops your agent until it passes its exam again.')).toBeTruthy();
    unmount();
    render(<AskCard item={edit('app/extract.py (1 edit)\n- a\n+ b')} onAnswer={vi.fn()} />);
    expect(document.querySelector('.edit-preview')).toBeTruthy();
    expect(screen.queryByText(/Saving this stops/)).toBeNull();
  });

  it('shares its diff view with the wizard', async () => {
    const card = await import('../src/components/chat/AskCard');
    const preview = await import('../src/components/chat/EditPreview');
    expect(card.EditPreview).toBe(preview.EditPreview);
  });
});
