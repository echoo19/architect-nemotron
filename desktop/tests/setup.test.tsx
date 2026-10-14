// @vitest-environment jsdom
/**
 * A packaged app's first start: while the launcher sets up Python, a conversation shows
 * the launcher's plain progress line in place of the start screen; the line goes when
 * the bridge is ready, and a failed setup shows the launcher's own sentence.
 */
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ConversationView } from '../src/components/chat/ConversationView';
import { ForgeWorkspace } from '../src/components/forge/ForgeWorkspace';
import { emptyTimeline } from '../src/lib/timeline';
import { DEFAULT_PREFS, useApp, wire, type Conv } from '../src/store/app';

const starting = (): Conv => ({
  id: 'c1', status: 'starting', timeline: emptyTimeline(), state: { started: false }, forge: { stages: [], tools: [] }, links: [],
});
const current = () => useApp.getState().convs.c1;
/** A frame from the control server, as the socket would deliver it. */
const frame = (value: Record<string, unknown>) => (wire as unknown as { onFrame(text: string): void }).onFrame(JSON.stringify(value));

beforeEach(() => {
  window.matchMedia = window.matchMedia ?? ((() => ({ matches: false, addEventListener() {}, removeEventListener() {} })) as never);
  global.fetch = vi.fn(async () => new Response('{}')) as never;
  useApp.setState({
    prefs: { ...DEFAULT_PREFS, open: [{ id: 'c1', cwd: 'C:/p', kind: 'chat' }], active: 'c1' },
    convs: { c1: starting() },
  });
});

afterEach(() => cleanup());

it('shows the setup line in place of the start screen, then clears it when ready', () => {
  act(() => wire.inject('c1', { type: 'setup', text: 'Setting up Architect for its first start. This takes a few minutes.' }));
  act(() => wire.inject('c1', { type: 'setup', text: 'Downloading Python 3.12.13 (20.9 MiB)' }));
  expect(current().setup).toBe('Downloading Python 3.12.13 (20.9 MiB)');
  expect(current().timeline.items).toEqual([]);
  const { rerender } = render(<ConversationView conv={current()} compact />);
  expect(screen.getByRole('status').textContent).toBe('Downloading Python 3.12.13 (20.9 MiB)');
  expect(screen.queryByText('What are we working on?')).toBeNull();
  act(() => wire.inject('c1', { type: 'ready' }));
  expect(current().setup).toBeUndefined();
  rerender(<ConversationView conv={current()} compact />);
  expect(screen.getByText('What are we working on?')).toBeTruthy();
});

it('opens a chat on What are we working on? and a build on its Describe step', () => {
  act(() => wire.inject('c1', { type: 'ready' }));
  const { unmount } = render(<ConversationView conv={current()} />);
  expect(screen.getByText('What are we working on?')).toBeTruthy();
  expect(screen.queryByText('What should the agent do?')).toBeNull();
  unmount();
  useApp.setState({ prefs: { ...useApp.getState().prefs, open: [{ id: 'c1', cwd: 'C:/p', kind: 'forge' }] } });
  render(<ForgeWorkspace conv={{ ...current(), state: { ...current().state, state: 'forge' } }} />);
  expect(document.querySelector('nav[aria-label="Steps"] [aria-current="step"]')!.getAttribute('data-step')).toBe('1');
  expect(screen.queryByText('What are we working on?')).toBeNull();
});

it('shows the launcher\'s own sentence when setup fails', () => {
  const reason = "Setup did not finish: Request failed after 3 retries. The first start downloads Python and Architect's packages, "
    + 'so check the internet connection, then try again. Full log: {home}/AppData/Local/Architect/setup.log';
  act(() => wire.inject('c1', { type: 'setup', text: 'Downloading packages' }));
  act(() => frame({ t: 'exit', conv: 'c1', code: 3, reason }));
  expect(current()).toMatchObject({ status: 'failed', error: reason });
  expect(current().setup).toBeUndefined();
  render(<ConversationView conv={current()} compact />);
  expect(screen.getByText('Architect could not start')).toBeTruthy();
  expect(screen.getByText(reason)).toBeTruthy();
});
