// @vitest-environment jsdom
/**
 * The first-run sign-in, model and folder.
 * The bridge is replaced by a recorded `request` (tests/helpers/flows.tsx).
 */
import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { emptyTimeline } from '../src/lib/timeline';
import { inView, Onboarding, suggestedModel } from '../src/components/onboarding/Onboarding';
import { errorText, useApp } from '../src/store/app';
import { calls, conv, installFlowsHarness, replies } from './helpers/flows';

installFlowsHarness();

describe('first run', () => {
  it('signs in, chooses a model, then a folder', async () => {
    replies.onboarding = {
      profile_exists: false,
      choices: [
        { id: 'claude', label: 'Claude (claude CLI login)', kind: 'cli', state: 'cli_found', ready: true, recommended: true },
        { id: 'openai', label: 'OpenAI API', kind: 'api', state: 'no_key', ready: false },
      ],
    };
    replies.sign_in = { ok: true, models: [{ id: 'claude-haiku-4-5', price: '$1/$5 per M' }, { id: 'claude-sonnet-5' }] };
    replies.choose_model = { profile_path: 'w/session-profile.json' };
    render(<Onboarding conv={conv({ status: 'setup' })} />);
    // The provider by its name; how it signs in is the terminal's detail.
    await screen.findByText('Claude');
    expect(screen.getByText('Recommended')).toBeTruthy();
    fireEvent.click(screen.getByText('Continue'));
    await screen.findByText('Choose a model');
    fireEvent.click(screen.getByText('Claude Sonnet 5'));
    expect(screen.getByText('$1 in, $5 out per M')).toBeTruthy();
    fireEvent.click(screen.getByText('Use this model'));
    await screen.findByText('Where should Architect work?');
    expect(calls.find((c) => c.method === 'sign_in')?.params.provider).toBe('claude');
    expect(calls.find((c) => c.method === 'choose_model')?.params).toMatchObject({ provider: 'claude', model: 'claude-sonnet-5' });
  });

  it('starts on Nemotron 3 Super on Nebius, although Nano sorts first', async () => {
    replies.onboarding = { profile_exists: false, choices: [{ id: 'nebius', label: 'Nebius Token Factory', kind: 'api', state: 'no_key', ready: false, recommended: true }] };
    replies.sign_in = {
      ok: true,
      default_model: 'nvidia/nemotron-3-super-120b-a12b',
      models: ['nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B', 'nvidia/Nemotron-3-Ultra-550b-a55b', 'nvidia/Nemotron-3_5-Lightning', 'nvidia/nemotron-3-super-120b-a12b'].map((id) => ({ id })),
    };
    replies.choose_model = { profile_path: 'w/session-profile.json' };
    render(<Onboarding conv={conv({ status: 'setup' })} />);
    await screen.findByText('Nebius Token Factory');
    fireEvent.click(screen.getByText('Continue'));
    await screen.findByText('Choose a model');
    const checked = screen.getAllByRole('radio').filter((row) => row.getAttribute('aria-checked') === 'true');
    // By its name; the id is in the row's tooltip.
    expect(checked.map((row) => row.textContent)).toEqual(['Nemotron 3 Super']);
    expect(checked[0].querySelector('.row-title')!.getAttribute('title')).toBe('nvidia/nemotron-3-super-120b-a12b');
    fireEvent.click(screen.getByText('Use this model'));
    await screen.findByText('Where should Architect work?');
    expect(calls.find((c) => c.method === 'choose_model')?.params).toMatchObject({ provider: 'nebius', model: 'nvidia/nemotron-3-super-120b-a12b' });
  });

  it('keeps a suggested model in view when the list is long', () => {
    const models = Array.from({ length: 40 }, (_, i) => ({ id: `m${String(i).padStart(2, '0')}` }));
    expect(suggestedModel(models, 'm35')).toBe('m35');
    expect(inView(models, 'm35')[0].id).toBe('m35');
    expect(inView(models, 'm03')).toBe(models);
    expect(suggestedModel(models, 'absent')).toBe('m00');
    expect(suggestedModel([], 'absent')).toBe('');
  });

  it('shows a judge code\'s refusal in the words the CLI uses', async () => {
    const sentence = 'The judges\' relay is switched off for now and refuses model calls. Try again later.';
    replies.onboarding = { profile_exists: false, choices: [{ id: 'nebius', label: 'Nebius Token Factory', kind: 'api', state: 'no_key', ready: false, recommended: true }] };
    replies.sign_in = { ok: false, code: 'relay_off', message: sentence, models: [] };
    render(<Onboarding conv={conv({ status: 'setup' })} />);
    await screen.findByText('Nebius Token Factory');
    fireEvent.click(screen.getByText('Continue'));
    await screen.findByText(sentence);
    expect(screen.queryByText(/relay_off/)).toBeNull();
  });

  it('opens the sign-in on Nebius when a folder that runs on Nebius has no key yet', async () => {
    // The receipt example opened before any sign-in: its own profile names Nebius models.
    useApp.setState({
      prefs: { ...useApp.getState().prefs, open: [{ id: 'c1', cwd: 'C:/p/examples/expense-app', kind: 'chat' }], trusted: ['C:/p'] },
      convs: { c1: conv({ status: 'starting', state: { started: false } }) },
    });
    replies.onboarding = {
      profile_exists: true,
      choices: [
        { id: 'claude', label: 'Claude (claude CLI login)', kind: 'cli', state: 'cli_found', ready: true, recommended: true },
        { id: 'nebius', label: 'Nebius Token Factory', kind: 'api', state: 'no_key', ready: false },
      ],
    };
    replies.boot = Object.assign(new Error('sign_in_first'), { code: 'sign_in_first' });
    await act(() => useApp.getState().boot('c1'));
    const stopped = useApp.getState().convs.c1;
    expect(stopped).toMatchObject({ status: 'setup', error: 'sign_in_first' });
    render(<Onboarding conv={stopped} />);
    await screen.findByText('This folder runs on Nebius Token Factory. Sign in with your judge code or a Nebius key first.');
    const checked = screen.getAllByRole('radio').filter((row) => row.getAttribute('aria-checked') === 'true');
    expect(checked.map((row) => row.textContent)).toEqual([expect.stringContaining('Nebius Token Factory')]);
    expect(screen.queryByText(/missing_api_key|sign_in_first/)).toBeNull();
    // Signed in: the same folder starts, and the reason goes.
    replies.boot = {};
    await act(() => useApp.getState().boot('c1'));
    expect(useApp.getState().convs.c1).toMatchObject({ status: 'ready', error: undefined });
  });

  it('words an address that does not answer, and a missing sign-in, without a code', () => {
    expect(errorText('provider_unreachable')).toBe("Architect could not reach the model's address. Check the connection, then try again.");
    expect(errorText('sign_in_first')).not.toMatch(/sign_in_first|missing_api_key/);
    expect(errorText('provider_unreachable')).not.toMatch(/signed out/);
  });

  it('says what went wrong when a key is refused', async () => {
    replies.onboarding = { profile_exists: false, choices: [{ id: 'openai', label: 'OpenAI API', kind: 'api', state: 'no_key', ready: false, recommended: true }] };
    replies.sign_in = { ok: false, code: 'unauthorized', models: [] };
    render(<Onboarding conv={conv({ status: 'setup' })} />);
    await screen.findByText('OpenAI API');
    fireEvent.click(screen.getByText('Continue'));
    await screen.findByText('That key was refused. Check it and paste it again.');
  });

  it('calls a stored judge code a judge code, as the CLI does', async () => {
    replies.onboarding = {
      profile_exists: false,
      choices: [
        { id: 'nebius', label: 'Nebius Token Factory', kind: 'api', state: 'key_stored', ready: true, detail: 'judge code stored', credential: 'judge_code', recommended: true },
        { id: 'openai', label: 'OpenAI API', kind: 'api', state: 'key_stored', ready: true, detail: 'key stored', credential: 'key' },
      ],
    };
    render(<Onboarding conv={conv({ status: 'setup' })} />);
    await screen.findByText('Judge code stored');
    expect(screen.getByText('Key stored')).toBeTruthy();
    expect(screen.queryByText('Key saved')).toBeNull();
  });

  it('marks a stored provider with an ok StatusMark, and one that needs a key with none', async () => {
    replies.onboarding = {
      profile_exists: false,
      choices: [
        { id: 'nebius', label: 'Nebius Token Factory', kind: 'api', state: 'key_stored', ready: true, credential: 'judge_code', recommended: true },
        { id: 'openai', label: 'OpenAI API', kind: 'api', state: 'no_key', ready: false },
      ],
    };
    render(<Onboarding conv={conv({ status: 'setup' })} />);
    await screen.findByText('Nebius Token Factory');
    const row = (label: string) => screen.getAllByRole('radio').find((r) => r.textContent?.includes(label))!;
    expect(row('Nebius').querySelector('[data-status-mark="ok"]')!.getAttribute('aria-label')).toBe('Judge code stored');
    expect(row('Nebius').querySelector('.row-meta')!.textContent).toContain('Judge code stored');
    expect(row('OpenAI').querySelector('[data-status-mark]')).toBeNull();
    expect(row('OpenAI').textContent).toContain('Needs a key');
    // Grouped under plain headings; the one primary is Continue.
    expect(screen.getByText('API key')).toBeTruthy();
    expect([...document.querySelectorAll('.ui-button-primary')].map((b) => b.textContent)).toEqual(['Continue']);
  });

  it('asks for a judge code in its own field and checks it', async () => {
    replies.onboarding = { profile_exists: false, choices: [{ id: 'nebius', label: 'Nebius Token Factory', kind: 'api', state: 'no_key', ready: false, recommended: true }] };
    replies.sign_in = new Promise(() => {});
    const answer = vi.fn(async () => {});
    useApp.setState({ answer: answer as never });
    const timeline = { ...emptyTimeline(), items: [{ kind: 'ask' as const, key: 'a1', ask: { id: 7, kind: 'secret' as const, question: 'Key' }, closed: false }] };
    render(<Onboarding conv={conv({ status: 'setup', timeline })} />);
    await screen.findByText('Nebius Token Factory');
    fireEvent.click(screen.getByText('Continue'));
    await screen.findByText('Paste your judge code');
    fireEvent.change(screen.getByLabelText('Judge code'), { target: { value: ' jc-123 ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Check code' }));
    expect(answer).toHaveBeenCalledWith('c1', 7, 'jc-123');
  });

  it('lets a server that may need no key go on with an empty field', async () => {
    replies.onboarding = { profile_exists: false, choices: [{ id: 'custom', label: 'Your own server', kind: 'local', state: 'no_key', ready: false }] };
    replies.sign_in = new Promise(() => {});
    const answer = vi.fn(async () => {});
    useApp.setState({ answer: answer as never });
    const question = 'Paste your Your own server API key (hidden; stored in C:/w/.architect/credentials.json, never logged; Enter for none)';
    const timeline = { ...emptyTimeline(), items: [{ kind: 'ask' as const, key: 'a1', ask: { id: 8, kind: 'secret' as const, question }, closed: false }] };
    render(<Onboarding conv={conv({ status: 'setup', timeline })} />);
    await screen.findByText('Your own server');
    fireEvent.click(screen.getByText('Continue'));
    const none = await screen.findByRole('button', { name: 'No key' });
    expect((none as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(none);
    expect(answer).toHaveBeenCalledWith('c1', 8, '');
  });
});
