// @vitest-environment jsdom
/**
 * Sending, queueing and stopping from the composer, its modes and its command menu.
 * The bridge is replaced by a recorded `request` (tests/helpers/flows.tsx).
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Composer, DESCRIBE_EXAMPLES } from '../src/components/chat/Composer';
import { emptyTimeline } from '../src/lib/timeline';
import { useApp } from '../src/store/app';
import { calls, conv, installFlowsHarness, replies } from './helpers/flows';

installFlowsHarness();

describe('the composer', () => {
  it('sends on Enter, keeps Shift+Enter as a new line, and clears', async () => {
    render(<Composer conv={conv()} />);
    const box = screen.getByLabelText('Message') as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: 'hello' } });
    fireEvent.keyDown(box, { key: 'Enter', shiftKey: true });
    expect(calls.find((c) => c.method === 'send')).toBeUndefined();
    fireEvent.keyDown(box, { key: 'Enter' });
    await waitFor(() => expect(calls.find((c) => c.method === 'send')?.params.text).toBe('hello'));
    expect(box.value).toBe('');
  });

  it('sends only on Ctrl+Enter when the setting asks for it', async () => {
    useApp.setState({ prefs: { ...useApp.getState().prefs, sendKey: 'mod-enter' } });
    render(<Composer conv={conv()} />);
    const box = screen.getByLabelText('Message') as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: 'two lines' } });
    fireEvent.keyDown(box, { key: 'Enter' });
    expect(calls.find((c) => c.method === 'send')).toBeUndefined();
    fireEvent.keyDown(box, { key: 'Enter', ctrlKey: true });
    await waitFor(() => expect(calls.find((c) => c.method === 'send')?.params.text).toBe('two lines'));
  });

  it('stops a running turn with Escape, and steers it or queues for after it', async () => {
    const running = conv({ timeline: { ...emptyTimeline(), running: true } });
    render(<Composer conv={running} />);
    const box = screen.getByLabelText('Message') as HTMLTextAreaElement;
    expect(box.placeholder.toLowerCase()).toContain('add to what architect is doing');
    fireEvent.change(box, { target: { value: 'also check the tests' } });
    fireEvent.keyDown(box, { key: 'Enter' });
    await waitFor(() => expect(calls.find((c) => c.method === 'send')?.params).toMatchObject({ text: 'also check the tests', steer: true }));
    fireEvent.change(box, { target: { value: 'then deploy' } });
    fireEvent.keyDown(box, { key: 'Enter', ctrlKey: true, shiftKey: true });
    await waitFor(() => expect(calls.filter((c) => c.method === 'send')[1]?.params).toMatchObject({ text: 'then deploy', steer: false }));
    fireEvent.keyDown(box, { key: 'Escape' });
    await waitFor(() => expect(calls.some((c) => c.method === 'cancel')).toBe(true));
  });

  it('cycles Chat, Build and Plan with Shift+Tab', async () => {
    render(<Composer conv={conv()} />);
    // Build mode is called Build; Forge is the optional harness engineer, not a mode.
    const modes = within(screen.getByRole('radiogroup')).getAllByRole('radio');
    expect(modes.map((m) => m.getAttribute('aria-label'))).toEqual(['Chat', 'Build', 'Plan']);
    expect(modes[1].getAttribute('data-tip')).toBe('Build');
    fireEvent.keyDown(screen.getByLabelText('Message'), { key: 'Tab', shiftKey: true });
    // Its key on the wire stays forge, the key saved conversations use.
    await waitFor(() => expect(calls.find((c) => c.method === 'set_state')?.params.state).toBe('forge'));
  });

  it('under a build card, is the field and Send only: no modes, permission, model or attach', async () => {
    useApp.setState({ prefs: { ...useApp.getState().prefs, open: [{ id: 'c1', cwd: 'C:/p', kind: 'forge' }] } });
    render(<Composer conv={conv({ state: { ...conv().state, state: 'forge' } })} variant="build" />);
    expect(screen.queryByRole('radiogroup')).toBeNull();
    expect(screen.queryByText('Ask first')).toBeNull();
    expect(screen.queryByText('fake')).toBeNull();
    expect(screen.queryByLabelText('Attach an image')).toBeNull();
    expect(document.querySelector('.composer.build-composer.ui-field-frame')).toBeTruthy();
    const box = screen.getByLabelText('Message') as HTMLTextAreaElement;
    // Shift+Tab does not leave build mode from a build conversation.
    fireEvent.keyDown(box, { key: 'Tab', shiftKey: true });
    fireEvent.change(box, { target: { value: 'use the second sheet' } });
    fireEvent.keyDown(box, { key: 'Enter' });
    await waitFor(() => expect(calls.find((c) => c.method === 'send')?.params.text).toBe('use the second sheet'));
    expect(calls.find((c) => c.method === 'set_state')).toBeUndefined();
  });

  it('lets a chat switched into build mode switch back with Shift+Tab from the build composer', async () => {
    render(<Composer conv={conv({ state: { ...conv().state, state: 'forge' } })} variant="build" />);
    fireEvent.keyDown(screen.getByLabelText('Message'), { key: 'Tab', shiftKey: true });
    await waitFor(() => expect(calls.find((c) => c.method === 'set_state')).toBeTruthy());
  });

  it('shows Send, not Stop, while the turn waits on an answer, and has no placeholder under a build', () => {
    const waiting = { ...emptyTimeline(), running: true, items: [{ kind: 'ask' as const, key: 'a2', ask: { id: 2, kind: 'question' as const, question: 'Build this agent?' }, closed: false }] };
    render(<Composer conv={conv({ timeline: waiting as never })} variant="build" />);
    expect(document.querySelector('.composer .send-button.is-stop')).toBeNull();
    expect(document.querySelector('.composer .send-button')).toBeTruthy();
    expect((screen.getByLabelText('Message') as HTMLTextAreaElement).placeholder).toBe('');
  });

  it('under a running build, its Send is the Stop button', async () => {
    render(<Composer conv={conv({ timeline: { ...emptyTimeline(), running: true } })} variant="build" />);
    const stop = document.querySelector('.composer .send-button.is-stop') as HTMLElement;
    expect(stop.getAttribute('aria-label')).toBe('Stop (Esc)');
    fireEvent.click(stop);
    await waitFor(() => expect(calls.some((c) => c.method === 'cancel')).toBe(true));
  });

  it('shows Build as the mode while the conversation is in build mode', () => {
    render(<Composer conv={conv({ state: { ...conv().state, state: 'forge' } })} />);
    const on = screen.getByRole('radio', { checked: true });
    expect(on.getAttribute('aria-label')).toBe('Build');
    expect(on.textContent).toBe('Build');
    expect((screen.getByLabelText('Message') as HTMLTextAreaElement).placeholder).toBe('Describe an agent to build');
    expect(screen.queryByText('Forge')).toBeNull();
  });

  it('opens a saved Build an agent conversation in build mode', async () => {
    useApp.setState({
      prefs: { ...useApp.getState().prefs, open: [{ id: 'c1', cwd: 'C:/p', kind: 'forge' }], trusted: ['C:/p'] },
      convs: { c1: conv({ status: 'starting', state: { started: false } }) },
    });
    replies.onboarding = { profile_exists: true, choices: [] };
    await act(() => useApp.getState().boot('c1'));
    expect(calls.find((c) => c.method === 'set_state')?.params).toEqual({ state: 'forge' });
    expect(useApp.getState().convs.c1).toMatchObject({ status: 'ready' });
  });

  it('waits a moment before listing every command on a bare /, so typing a command never flashes the list', async () => {
    replies.commands = { commands: [{ name: '/status', usage: '/status', summary: 'Session status', custom: false }, { name: '/make', usage: '/make <request>', summary: 'Build an agent', custom: false }] };
    render(<Composer conv={conv({ id: 'c-flash' })} />);
    const box = screen.getByLabelText('Message');
    fireEvent.change(box, { target: { value: '/' } });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 40));
    });
    expect(screen.queryByText('Session status')).toBeNull();
    fireEvent.change(box, { target: { value: '/ma' } });
    await screen.findByText('Build an agent from a description or a spec file');
    expect(screen.queryByText('Session status')).toBeNull();
    fireEvent.change(box, { target: { value: '/' } });
    await screen.findByText('Session status');
  });

  it('lists the commands whose names start with what was typed, before any description', async () => {
    replies.commands = { commands: [
      { name: '/compact', usage: '/compact', summary: 'Summarize old tool results to one line', custom: false },
      { name: '/doctor', usage: '/doctor', summary: 'Check providers, local models, workspace and disk', custom: false },
      { name: '/make', usage: '/make <description>', summary: 'Build an agent for one request: interview, plan card, build, exam', custom: false },
      { name: '/image', usage: '/image <path>', summary: 'Attach an image to your next message', custom: false },
    ] };
    render(<Composer conv={conv({ id: 'c-prefix' })} />);
    const box = screen.getByLabelText('Message');
    fireEvent.change(box, { target: { value: '/ma' } });
    await screen.findByText('/make <description>');
    expect(screen.queryByText('/compact')).toBeNull();
    expect(screen.queryByText('/image <path>')).toBeNull();
    // No name holds "disk": then, and only then, the descriptions are searched.
    fireEvent.change(box, { target: { value: '/disk' } });
    await screen.findByText('/doctor');
  });

  it('opens the command menu on / and runs the picked command', async () => {
    replies.commands = { commands: [{ name: '/status', usage: '/status', summary: 'Session status', custom: false }] };
    const ready = conv({ id: 'c-menu' });
    render(<Composer conv={ready} />);
    const box = screen.getByLabelText('Message');
    fireEvent.change(box, { target: { value: '/st' } });
    await screen.findByText('Session status');
    fireEvent.keyDown(window, { key: 'Enter' });
    await waitFor(() => expect(calls.find((c) => c.method === 'send')?.params.text).toBe('/status'));
  });
});

describe('the Describe examples', () => {
  // A drafted exam grades by exact match. Live, the meeting-transcript example asked for
  // free-text lists of decisions and action items: the three models scored 0, 1 and 0 of 6,
  // so a first click on it ended "not licensed". Each example's answer is a label from a
  // list the sentence gives, or a few fields it names.
  it('asks only for answers an exact-match exam can grade', () => {
    expect(DESCRIBE_EXAMPLES).toHaveLength(4);
    for (const example of DESCRIBE_EXAMPLES) {
      expect(example).not.toMatch(/transcript|action items|decisions|summar/i);
      const fields = /give back the [a-z ,]+ and [a-z ]+|Pull the [a-z ,]+ and [a-z ]+ out of/.test(example);
      const labels = / (as|into) [a-z ]+, [a-z ]+ or [a-z ]+$/.test(example);
      expect(fields || labels, example).toBe(true);
    }
    expect(DESCRIBE_EXAMPLES[3]).toBe('Label a product review as positive, negative or neutral');
  });
});

