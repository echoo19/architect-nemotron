// @vitest-environment jsdom
/**
 * Try it's form: a long text input (the receipt reader's one field) starts as a text box a
 * few lines high, grows with what is pasted up to 12 lines, then scrolls; a short field
 * starts at one line and grows the same way.
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { GROW_LINES, grownHeight, SchemaForm, startRows } from '../src/components/forge/SchemaForm';

const RECEIPT = { type: 'object', properties: { receipt: { type: 'string' } }, required: ['receipt'] };
const LINE = 20; // px, the line height the mocked style reports
const CHROME = 18; // padding 8 + 8 and borders 1 + 1
let content = 0; // the scrollHeight the mocked layout reports

beforeEach(() => {
  content = 0;
  Object.defineProperty(HTMLTextAreaElement.prototype, 'scrollHeight', { configurable: true, get: () => content });
  const real = window.getComputedStyle;
  vi.spyOn(window, 'getComputedStyle').mockImplementation((el: Element) => {
    if (!(el instanceof HTMLTextAreaElement)) return real(el);
    return { lineHeight: `${LINE}px`, fontSize: '13px', paddingTop: '8px', paddingBottom: '8px', borderTopWidth: '1px', borderBottomWidth: '1px' } as CSSStyleDeclaration;
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('the height of a growing text box', () => {
  it('follows its content between its first rows and 12 lines, then scrolls', () => {
    expect(GROW_LINES).toBe(12);
    expect(grownHeight(0, LINE, CHROME, 5)).toEqual({ height: 5 * LINE + CHROME, scrolls: false });
    expect(grownHeight(200, LINE, CHROME, 1)).toEqual({ height: 200, scrolls: false });
    expect(grownHeight(900, LINE, CHROME, 1)).toEqual({ height: 12 * LINE + CHROME, scrolls: true });
  });

  it('starts a long text a few lines high and a short one at one line', () => {
    expect(startRows('receipt', true)).toBe(3); // the agent's whole input
    expect(startRows('invoice_text', false)).toBe(3);
    expect(startRows('emailBody', false)).toBe(3);
    expect(startRows('currency', false)).toBe(1);
    expect(startRows('invoice_number', false)).toBe(1); // an invoice's number, not its text
  });
});

describe("Try it's text box", () => {
  it('grows with a pasted receipt and scrolls past 12 lines', () => {
    const onSubmit = vi.fn();
    render(<SchemaForm schema={RECEIPT} busy={false} onSubmit={onSubmit} />);
    const box = screen.getByRole('textbox') as HTMLTextAreaElement;
    expect(box.rows).toBe(3);
    expect(box.style.height).toBe(`${3 * LINE + CHROME}px`);
    content = 7 * LINE + CHROME - 2; // seven lines of text, padding included
    fireEvent.change(box, { target: { value: 'line\n'.repeat(7) } });
    expect(box.style.height).toBe(`${7 * LINE + CHROME}px`);
    expect(box.style.overflowY).toBe('hidden');
    content = 40 * LINE + 16; // a long receipt
    const receipt = Array.from({ length: 40 }, (_, i) => `1 ITEM ${i} 12.000`).join('\n');
    fireEvent.change(box, { target: { value: receipt } });
    expect(box.style.height).toBe(`${GROW_LINES * LINE + CHROME}px`);
    expect(box.style.overflowY).toBe('auto');
    fireEvent.click(screen.getByText('Run'));
    expect(onSubmit).toHaveBeenCalledWith({ receipt });
  });

  it('puts Run beside the fields, outline and small, and labels each field', () => {
    render(<SchemaForm className="try-form" schema={RECEIPT} busy={false} onSubmit={() => undefined} />);
    const form = document.querySelector('form.try-form')!;
    const run = screen.getByRole('button', { name: 'Run' });
    expect(run.closest('form')).toBe(form);
    expect(run.className).toContain('ui-button-outline');
    expect(run.className).toContain('ui-button-sm');
    expect(run.getAttribute('data-action')).toBe('run');
    // Run follows the fields, not under them in a row of its own.
    expect(run.parentElement).toBe(form);
    expect(form.querySelector('.schema-fields')!.nextElementSibling).toBe(run);
    expect(screen.getByLabelText('Receipt').tagName).toBe('TEXTAREA');
  });

  it('keeps Run while a run is open, with the working mark', () => {
    render(<SchemaForm schema={RECEIPT} busy onSubmit={() => undefined} />);
    const run = screen.getByRole('button', { name: 'Run' }) as HTMLButtonElement;
    expect(run.disabled).toBe(true);
    expect(run.querySelector('.lectern-working')).toBeTruthy();
  });

  it('starts a short field at one line, which still grows', () => {
    render(<SchemaForm schema={{ type: 'object', properties: { currency: { type: 'string' }, total: { type: 'number' } } }} busy={false} onSubmit={() => undefined} />);
    const box = screen.getByRole('textbox') as HTMLTextAreaElement;
    expect(box.rows).toBe(1);
    content = 3 * LINE + 16;
    fireEvent.change(box, { target: { value: 'a\nb\nc' } });
    expect(box.style.height).toBe(`${3 * LINE + CHROME}px`);
  });
});

describe("Try it's paste box (the wizard's step 5)", () => {
  it('draws a lone string field as a fixed mono text area with a placeholder, and lets the screen own the values and the Run button', () => {
    const onSubmit = vi.fn();
    const onValues = vi.fn();
    const { rerender } = render(
      <SchemaForm
        id="try"
        schema={RECEIPT}
        busy={false}
        onSubmit={onSubmit}
        onlyLabel="Receipt text"
        values={{}}
        onValues={onValues}
        single={{ rows: 9, placeholder: 'Paste the text of one receipt' }}
        actions={<button type="submit">Run agent</button>}
      />,
    );
    const box = screen.getByLabelText('Receipt text') as HTMLTextAreaElement;
    expect(box.rows).toBe(9);
    expect(box.className).toContain('mono');
    expect(box.placeholder).toBe('Paste the text of one receipt');
    // The screen's own button takes Run's place.
    expect(screen.queryByRole('button', { name: 'Run' })).toBeNull();
    fireEvent.change(box, { target: { value: 'TOTAL 5' } });
    expect(onValues).toHaveBeenCalledWith({ receipt: 'TOTAL 5' });
    rerender(
      <SchemaForm id="try" schema={RECEIPT} busy={false} onSubmit={onSubmit} values={{ receipt: 'TOTAL 5' }} onValues={onValues}
        single={{ rows: 9 }} actions={<button type="submit">Run agent</button>} />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Run agent' }));
    expect(onSubmit).toHaveBeenCalledWith({ receipt: 'TOTAL 5' });
  });

  it('keeps the growing fields for an input of more than one field', () => {
    render(<SchemaForm schema={{ type: 'object', properties: { currency: { type: 'string' }, total: { type: 'number' } } }} busy={false} single={{ rows: 9 }} onSubmit={() => undefined} />);
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).rows).toBe(1);
    expect(screen.getByRole('button', { name: 'Run' })).toBeTruthy();
  });
});
