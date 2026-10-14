// @vitest-environment jsdom
/**
 * The redesign's primitives (components/ui): the classes a Button draws per variant and
 * size, its pending state, the Segmented control's two roles, a StatusMark's word, the
 * Dialog's Escape and focus return, the Sheet's data-sheet hook, and the card's parts.
 */
import { IconMessage } from '@tabler/icons-react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Badge } from '../src/components/ui/Badge';
import { Button, IconButton } from '../src/components/ui/Button';
import { Card, CardBody, CardFooter, CardHeader, CardLead } from '../src/components/ui/Card';
import { Dialog } from '../src/components/ui/Dialog';
import { Facts } from '../src/components/ui/Facts';
import { Field, TextArea } from '../src/components/ui/Field';
import { Meter } from '../src/components/ui/Meter';
import { PageHeader } from '../src/components/ui/PageHeader';
import { Row, RowList } from '../src/components/ui/RowList';
import { Segmented } from '../src/components/ui/Segmented';
import { Sheet } from '../src/components/ui/Sheet';
import { StatusMark } from '../src/components/ui/StatusMark';

afterEach(() => cleanup());

describe('Button', () => {
  it('draws its variant and size as classes, outline and md by default', () => {
    render(
      <>
        <Button>Cancel</Button>
        <Button variant="primary" size="sm" data-action="build">Build for up to $0.60</Button>
        <Button variant="ghost" size="sm">Use a spec file</Button>
      </>,
    );
    expect(screen.getByRole('button', { name: 'Cancel' }).className).toBe('ui-button ui-button-outline ui-button-md');
    const build = screen.getByRole('button', { name: 'Build for up to $0.60' });
    expect(build.className).toBe('ui-button ui-button-primary ui-button-sm');
    expect(build.getAttribute('data-action')).toBe('build');
    expect(build.getAttribute('type')).toBe('button');
    expect(screen.getByRole('button', { name: 'Use a spec file' }).classList.contains('ui-button-ghost')).toBe(true);
  });

  it('shows the Working mark while pending, keeps its label, and cannot be pressed', () => {
    const onClick = vi.fn();
    render(<Button variant="primary" pending onClick={onClick}>Run</Button>);
    const button = screen.getByRole('button', { name: 'Run' }) as HTMLButtonElement;
    expect(button.querySelector('.lectern-working')).not.toBeNull();
    expect(button.disabled).toBe(true);
    expect(button.getAttribute('aria-busy')).toBe('true');
    fireEvent.click(button);
    expect(onClick).not.toHaveBeenCalled();
  });

  it('names an icon button by its verb and gives it the verb as a tooltip', () => {
    render(<IconButton icon={IconMessage} label="Transcript" />);
    const button = screen.getByRole('button', { name: 'Transcript' });
    expect(button.getAttribute('data-tip')).toBe('Transcript');
    expect(button.querySelector('svg')).not.toBeNull();
  });
});

describe('Segmented', () => {
  function Choice({ kind }: { kind: 'radio' | 'tabs' }) {
    const [value, setValue] = useState('here');
    return (
      <Segmented
        kind={kind}
        label="Where will this agent live?"
        options={[{ value: 'here', label: 'Run it here' }, { value: 'software', label: 'Put it into software' }]}
        value={value}
        onChange={setValue}
      />
    );
  }

  it('is a radio group that moves aria-checked to the picked option', () => {
    render(<Choice kind="radio" />);
    const group = screen.getByRole('radiogroup', { name: 'Where will this agent live?' });
    const [here, software] = screen.getAllByRole('radio');
    expect(group).toBeTruthy();
    expect(here.getAttribute('aria-checked')).toBe('true');
    expect(software.getAttribute('aria-checked')).toBe('false');
    fireEvent.click(software);
    expect(here.getAttribute('aria-checked')).toBe('false');
    expect(software.getAttribute('aria-checked')).toBe('true');
    // Arrow keys move the choice too.
    fireEvent.keyDown(group, { key: 'ArrowLeft' });
    expect(here.getAttribute('aria-checked')).toBe('true');
  });

  it('is a tab list when it switches a view', () => {
    render(<Choice kind="tabs" />);
    expect(screen.getByRole('tablist')).toBeTruthy();
    const tabs = screen.getAllByRole('tab');
    expect(tabs[0].getAttribute('aria-selected')).toBe('true');
    expect(screen.queryByRole('radiogroup')).toBeNull();
  });
});

describe('StatusMark', () => {
  it('carries its word as its name, or prints it in a header meta', () => {
    render(
      <>
        <StatusMark tone="ok" label="Passed" />
        <StatusMark tone="err" label="Did not pass" showLabel />
      </>,
    );
    const mark = screen.getByRole('img', { name: 'Passed' });
    expect(mark.getAttribute('data-status-mark')).toBe('ok');
    expect(screen.getByText('Did not pass').closest('[data-status-mark]')?.getAttribute('data-status-mark')).toBe('err');
  });
});

describe('Dialog', () => {
  function Host() {
    const [open, setOpen] = useState(false);
    return (
      <>
        <button type="button" onClick={() => setOpen(true)}>Add to project</button>
        <Dialog
          open={open}
          onOpenChange={setOpen}
          title="Add receipt-reader to this project?"
          footer={<><Button onClick={() => setOpen(false)}>Don't add</Button><Button variant="primary">Add to project</Button></>}
        />
      </>
    );
  }

  it('closes on Escape and gives focus back to what opened it', async () => {
    vi.useFakeTimers();
    try {
      render(<Host />);
      const opener = screen.getByRole('button', { name: 'Add to project' });
      opener.focus();
      fireEvent.click(opener);
      const dialog = screen.getByRole('dialog', { name: 'Add receipt-reader to this project?' });
      expect(dialog.getAttribute('aria-modal')).toBe('true');
      expect(screen.getByRole('button', { name: "Don't add" })).toBeTruthy();
      fireEvent.keyDown(document, { key: 'Escape' });
      expect(document.activeElement).toBe(opener);
      await act(async () => { vi.advanceTimersByTime(200); });
      expect(screen.queryByRole('dialog')).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('Sheet', () => {
  it('carries data-sheet and its title', () => {
    render(<Sheet open onOpenChange={() => undefined} title="Plan" name="plan">Spec</Sheet>);
    const sheet = document.querySelector('[data-sheet="plan"]') as HTMLElement;
    expect(sheet).not.toBeNull();
    expect(sheet.getAttribute('role')).toBe('dialog');
    expect(screen.getByRole('dialog', { name: 'Plan' })).toBe(sheet);
  });
});

describe('the card and its bodies', () => {
  it('puts the title, meta, lead, facts and one footer action in the card', () => {
    render(
      <Card width="list" className="agent-card" data-phase="licensed">
        <CardHeader meta={<StatusMark tone="ok" label="Passed" showLabel />} lead={<CardLead value="64 / 100" note="Super ships" tone="ok" />}>Exam result</CardHeader>
        <CardBody>
          <Facts items={[{ value: 'Read a receipt', label: 'Job', wide: true }, { value: '40 of 100', label: 'Pass mark' }]} />
        </CardBody>
        <CardFooter start="$0.32"><Button variant="primary" size="sm" data-action="add">Add to project</Button></CardFooter>
      </Card>,
    );
    const card = document.querySelector('.agent-card[data-phase="licensed"]') as HTMLElement;
    expect(card.classList.contains('ui-card')).toBe(true);
    expect(card.parentElement?.classList.contains('card-cell-list')).toBe(true);
    expect(screen.getByRole('heading', { name: 'Exam result' })).toBeTruthy();
    expect(card.querySelector('.ui-card-lead-value')?.textContent).toBe('64 / 100');
    expect([...card.querySelectorAll('.facts-cell')].map((c) => c.textContent)).toEqual(['JobRead a receipt', 'Pass mark40 of 100']);
    expect(card.querySelector('.facts-cell.is-wide')).not.toBeNull();
    const footer = card.querySelector('[data-card-footer]') as HTMLElement;
    expect(footer.querySelectorAll('button')).toHaveLength(1);
    expect(footer.textContent).toContain('$0.32');
  });

  it('draws the attention edge with a note for a screen reader', () => {
    render(<Card tone="attention" attentionNote="The build stopped"><CardHeader>Building</CardHeader></Card>);
    const card = document.querySelector('.ui-card') as HTMLElement;
    expect(card.classList.contains('is-attention')).toBe(true);
    expect(screen.getByText('The build stopped').className).toBe('sr-only');
  });

  it('lists rows, and shows the empty state when there are none', () => {
    const { rerender } = render(<RowList empty={<p>Nothing yet</p>}>{[]}</RowList>);
    expect(screen.getByText('Nothing yet')).toBeTruthy();
    rerender(<RowList><Row title="agent.py" /><Row title="report.signed.json" meta="Signed" /></RowList>);
    expect(screen.getAllByRole('listitem')).toHaveLength(2);
    expect(document.querySelector('.row.has-meta')?.textContent).toBe('report.signed.jsonSigned');
  });

  it('labels a field above it and wires its error', () => {
    render(<Field label="Receipt" htmlFor="r" error="It could not run"><TextArea id="r" invalid /></Field>);
    const box = screen.getByLabelText('Receipt');
    expect(box.getAttribute('aria-invalid')).toBe('true');
    expect(screen.getByRole('alert').textContent).toBe('It could not run');
  });

  it('heads a page with its title, a count and actions; meters and badges carry their numbers', () => {
    render(
      <>
        <PageHeader title="Receipt reader" count={3} actions={<IconButton icon={IconMessage} label="Transcript" />} />
        <Meter value={12} max={20} label="Context" />
        <Badge variant="count">2</Badge>
      </>,
    );
    expect(screen.getByRole('heading', { level: 1, name: 'Receipt reader' })).toBeTruthy();
    expect(screen.getByRole('progressbar', { name: 'Context' }).getAttribute('aria-valuenow')).toBe('60');
    expect(screen.getByText('2').getAttribute('data-badge')).toBe('count');
  });
});
