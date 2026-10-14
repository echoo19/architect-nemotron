// @vitest-environment jsdom
/**
 * Settings: models and sign-in, and keyboard shortcuts.
 * The bridge is replaced by a recorded `request` (tests/helpers/flows.tsx).
 */
import { fireEvent, render, screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { SettingsScreen } from '../src/components/settings/SettingsScreen';
import { useApp } from '../src/store/app';
import { conv, installFlowsHarness, replies } from './helpers/flows';

installFlowsHarness();

describe('Models and sign-in in Settings', () => {
  beforeEach(() => {
    useApp.setState({ convs: { c1: conv() }, screen: 'settings', settingsPane: 'models' });
    replies.providers = { providers: [
      { id: 'nebius', label: 'Nebius Token Factory', kind: 'api', state: 'key_stored', ready: true, detail: 'judge code stored', credential: 'judge_code' },
      { id: 'openai', label: 'OpenAI API', kind: 'api', state: 'key_stored', ready: true, detail: 'key stored', credential: 'key' },
    ] };
    replies.models = { current: 'fake', models: [] };
    replies.config = { values: {} };
  });

  const row = (label: string) => [...document.querySelectorAll<HTMLElement>('.provider-row')].find((r) => r.textContent?.includes(label))!;

  it('offers to change a stored judge code as a code, and a key as a key', async () => {
    render(<SettingsScreen />);
    await screen.findByText('Nebius Token Factory');
    expect(row('Nebius').querySelector('[data-tip]')?.getAttribute('data-tip')).toBe('Judge code stored');
    expect(within(row('Nebius')).getByRole('button', { name: 'Change code' })).toBeTruthy();
    expect(row('OpenAI').querySelector('[data-tip]')?.getAttribute('data-tip')).toBe('Key stored');
    expect(within(row('OpenAI')).getByRole('button', { name: 'Change key' })).toBeTruthy();
  });

  it('says why the relay refused a spare code in the CLI\'s sentence, not as a refused key', async () => {
    const sentence = 'The relay refused that code: it is unknown or has been revoked.';
    replies.sign_in = { ok: false, code: 'unauthorized', message: sentence, models: [] };
    render(<SettingsScreen />);
    fireEvent.click(await screen.findByRole('button', { name: 'Change code' }));
    await screen.findByText(sentence);
    expect(screen.queryByText(/That key was refused/)).toBeNull();
  });
});

describe('Keyboard shortcuts in Settings', () => {
  beforeEach(() => {
    useApp.setState({ convs: { c1: conv() }, screen: 'settings', settingsPane: 'shortcuts' });
  });

  it('opens before any shortcut was changed', async () => {
    expect(useApp.getState().prefs.keybindings).toBeUndefined();
    render(<SettingsScreen />);
    await screen.findByRole('heading', { name: 'Keyboard shortcuts' });
    expect(screen.getByText('Search and commands')).toBeTruthy();
  });

  it('names the modes Chat, Build and Plan', async () => {
    render(<SettingsScreen />);
    await screen.findByText('Switch between Chat, Build and Plan');
    const list = document.querySelector('.kbd-list') as HTMLElement;
    expect(within(list).getByText('Show or hide the panes (in Build mode: open or close the report)')).toBeTruthy();
    expect(within(list).queryByText(/Forge/)).toBeNull();
  });
});
