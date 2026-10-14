// @vitest-environment jsdom
/**
 * Try it on Your agents, and its test report; the guided build's Try it and Add to
 * project steps on the demo build. Shared set-up in tests/helpers/forge.tsx.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AgentsScreen } from '../src/components/agents/AgentsScreen';
import { AddScreen } from '../src/components/forge/wizard/AddScreen';
import { TryScreen } from '../src/components/forge/wizard/TryScreen';
import { forgeModel, ladderView } from '../src/lib/forge';
import { connectPrompt, wizardStep } from '../src/lib/wizard';
import { useApp } from '../src/store/app';
import {
  DEMO_CONNECT_APPROVE, DEMO_CONNECTED, DEMO_CWD, DEMO_DETAIL, DEMO_EXPORT_CONFIRM, DEMO_FILES, DEMO_FILES_WRITTEN, DEMO_PASSED, DEMO_RECEIPT, DEMO_RUN, demoConv, type Msg,
} from './fixtures/demoBuild';
import { conv, installForgeHarness, replies } from './helpers/forge';

installForgeHarness();

describe('Try it on Your agents', () => {
  const RECEIPT = { name: 'receipt-reader', licensed: true, spec: { task: 'Read a receipt', input_schema: { type: 'object', properties: { text: { type: 'string' } }, required: ['text'] } }, history: [] };

  async function runOnce(): Promise<void> {
    render(<AgentsScreen />);
    await screen.findByText('Run');
    const form = document.querySelector('.agent-page form') as HTMLFormElement;
    fireEvent.change(form.querySelector('textarea')!, { target: { value: 'TOTAL 12.34' } });
    await act(async () => {
      fireEvent.submit(form);
    });
  }

  beforeEach(() => {
    replies.agents = { agents: [{ name: 'receipt-reader', revision: 1, ok: true, licensed: true, floor_score: 0.51, floor_limit: 0.4 }] };
    replies.agent = RECEIPT;
    useApp.setState({ convs: { c1: conv() }, screen: 'agents' });
  });

  it('says why the judges\' relay refused the run in the agent view\'s sentence, not its code', async () => {
    const sentence = 'This judge code has reached its daily limit. The day resets at 00:00 UTC; until then, use a spare code.';
    useApp.setState({
      request: vi.fn(async (_id: string, method: string) => {
        if (method === 'run_agent') throw Object.assign(new Error('relay_daily_limit'), { code: 'relay_daily_limit', detail: sentence });
        return replies[method] ?? {};
      }) as never,
    });
    await runOnce();
    await waitFor(() => expect(screen.getByText(sentence)).toBeTruthy());
    expect(screen.getByText('It could not run')).toBeTruthy();
    expect(screen.queryByText(/relay_daily_limit/)).toBeNull();
  });

  it('shows a run\'s output with its cost, time and model calls, as the agent view does', async () => {
    replies.run_agent = { agent: 'receipt-reader', output: { total: 1234 }, cost_usd: 0.0018, seconds: 3.62, calls: 2, run_id: 'r1', completion_id: 'chatcmpl-abc123' };
    await runOnce();
    await waitFor(() => expect(screen.getByText('chatcmpl-abc123')).toBeTruthy());
    expect(document.querySelector('.try-stats')!.textContent).toBe('$0.00183.6s2 calls');
    expect(screen.getByText(/"total": 1234/)).toBeTruthy();
    expect(screen.queryByText(/run_id/)).toBeNull();
  });

  it('gives the cost per run and the test report in the words and numbers the build\'s Details uses', async () => {
    replies.agent = {
      ...RECEIPT,
      report: { floor_score: 0.51, floor_limit: 0.4, floor_kind: 'held_out_tasks', held_out_count: 100, mean_cost_usd: 0.00028, cost_usd: 0.0009, compute_s: 1.2, passed: true, tokens: 900 },
    };
    render(<AgentsScreen />);
    await screen.findByText('Cost per run');
    // A sub-cent cost keeps two significant digits, as Try it and the terminal show it.
    expect(screen.getByText('$0.00028')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Test report' }));
    // The report is Facts in its own section: each value over its label.
    const rows = Object.fromEntries([...document.querySelectorAll('.agent-page .agent-report .facts-cell')].map((r) => [r.querySelector('dt')!.textContent, r.querySelector('dd')!.textContent]));
    expect(rows).toEqual({ 'Exam score': '0.51', Bar: '0.40', 'Exam cases': '100', 'Mean cost per run': '$0.00028', 'Highest cost per run': '$0.0009', 'Longest run': '1.2s' });
    expect(document.querySelector('.agent-page')!.textContent).not.toMatch(/floor|held.out|\$0\.0000\b/i);
  });

  it('shows the agent as the build card does: its status, the exam on the ladder and grid, one primary', async () => {
    replies.agent = {
      ...RECEIPT,
      model: 'nvidia/nemotron-3-super-120b-a12b',
      report: { floor_score: 0.64, floor_limit: 0.4, held_out_count: 100, held_out_counts: { cases: 100, correct: 64, invalid: 0, over_budget: 0 }, mean_cost_usd: 0.00028 },
    };
    render(<AgentsScreen />);
    await screen.findByText('Cost per run');
    const card = document.querySelector('.agent-page [data-card]') as HTMLElement;
    expect(card.querySelector('.ui-card-meta')!.textContent).toContain('Passed');
    expect(card.querySelector('.ui-card-lead-value')!.textContent).toBe('64 / 100');
    // One row on the ladder, for the model the agent runs on, and the exam grid beside its key.
    expect(card.querySelectorAll('.viz-barview .viz-row')).toHaveLength(1);
    expect(card.querySelector('.viz-barview')!.textContent).toContain('Super');
    expect(card.querySelector('.viz-units')).toBeTruthy();
    expect([...card.querySelectorAll('.facts-label')].map((l) => l.textContent)).toEqual(['Job', 'Exam cases', 'Pass mark', 'Cost per run']);
    expect([...card.querySelectorAll('.ui-card-footer .ui-button-primary')].map((b) => b.textContent)).toEqual(['Add to project']);
    expect(card.textContent).not.toMatch(/Licensed|\bbar\b|—/);
  });
});

// ---------------------------------------------------------------- the guided build's steps 5 and 6

describe('step 5, Try it', () => {
  function props(msgs: Msg[], detail: Record<string, unknown> | null = DEMO_DETAIL) {
    const c = demoConv(msgs);
    useApp.setState({ convs: { c1: c } });
    const model = forgeModel(c.timeline);
    return { conv: c, model, ladder: ladderView(model, c.forge), detail: detail as never, sub: 'try' as const };
  }

  it('says what to paste and what comes back before a run, with Run agent waiting for text and no sample button without one', () => {
    const { sample: _sample, ...noSample } = DEMO_DETAIL;
    render(<TryScreen {...props(DEMO_PASSED, noSample)} />);
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Try your agent on a receipt');
    // Super's exam cost per receipt is under a cent (B1).
    expect(screen.getByText(/each run costs a fraction of a cent/)).toBeTruthy();
    expect(screen.getByText('Run the agent to see its answer here: total, subtotal, tax and item count.')).toBeTruthy();
    expect(screen.getByLabelText('Receipt text').tagName).toBe('TEXTAREA');
    expect((screen.getByLabelText('Receipt text') as HTMLTextAreaElement).placeholder).toBe('Paste the text of one receipt');
    expect((screen.getByRole('button', { name: /Run agent/ }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText('Paste a receipt first')).toBeTruthy();
    expect(screen.queryByText(/Load sample/)).toBeNull();
    expect(screen.getByRole('button', { name: /Skip to Add to project/ })).toBeTruthy();
  });

  it('shows the sample button only when the agent has a sample, and it fills the field', () => {
    render(<TryScreen {...props(DEMO_PASSED, { ...DEMO_DETAIL, sample: DEMO_RECEIPT })} />);
    fireEvent.click(screen.getByRole('button', { name: /Load sample receipt/ }));
    expect((screen.getByLabelText('Receipt text') as HTMLTextAreaElement).value).toBe(DEMO_RECEIPT);
    expect((screen.getByRole('button', { name: /Run agent/ }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("gives the answer field by field in schema order, with the run's cost and time, and the raw JSON on request", async () => {
    replies.run_agent = DEMO_RUN;
    render(<TryScreen {...props(DEMO_PASSED)} />);
    fireEvent.change(screen.getByLabelText('Receipt text'), { target: { value: DEMO_RECEIPT } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Run agent/ }));
    });
    await screen.findByText('What your agent gave back');
    const rows = [...document.querySelectorAll('.try-table tr')].map((r) => [r.querySelector('th')!.textContent, r.querySelector('td')!.textContent]);
    expect(rows).toEqual([['Total', '45100'], ['Subtotal', '41000'], ['Tax', '4100'], ['Item count', '2']]);
    expect(screen.getByText('Cost of this run')).toBeTruthy();
    expect(screen.getByText('$0.00028')).toBeTruthy();
    expect(screen.getByText('1.1s')).toBeTruthy();
    expect(screen.getByText(/On the exam, Super got the whole answer right on 64 of 100\./)).toBeTruthy();
    expect(screen.queryByText('chatcmpl-a5ca0c4c610c99c7')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Show raw JSON' }));
    expect(screen.getByText('chatcmpl-a5ca0c4c610c99c7')).toBeTruthy();
    expect(document.querySelector('.try-raw .try-json')!.textContent).toContain('"total": 45100');
    expect(screen.getByRole('button', { name: /Run again/ })).toBeTruthy();
    expect(screen.getByRole('button', { name: /^Add to project/ }).className).toContain('ui-button-primary');
  });

  it('shows the error block when a run fails, with Retry', async () => {
    useApp.setState({
      request: vi.fn(async (_id: string, method: string) => {
        if (method === 'run_agent') throw Object.assign(new Error('relay_daily_limit'), { code: 'relay_daily_limit', detail: 'This judge code has reached its daily limit.' });
        return replies[method] ?? {};
      }) as never,
    });
    render(<TryScreen {...props(DEMO_PASSED)} />);
    fireEvent.change(screen.getByLabelText('Receipt text'), { target: { value: DEMO_RECEIPT } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Run agent/ }));
    });
    await screen.findByText('It could not run');
    expect(screen.getByText('This judge code has reached its daily limit.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
  });

  it('Skip to Add to project shows step 6 and starts the export', async () => {
    render(<TryScreen {...props(DEMO_PASSED)} />);
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Skip to Add to project/ }));
    });
    expect(useApp.getState().wizardView.c1).toBe('add');
    const request = useApp.getState().request as unknown as ReturnType<typeof vi.fn>;
    expect(request.mock.calls.find((c) => c[1] === 'send')?.[2]).toEqual({ text: '/export receipt-reader', quiet: false });
  });
});

describe('step 6, Add to project', () => {
  function screenFor(msgs: Msg[], skipped = false) {
    const c = demoConv(msgs);
    useApp.setState({ convs: { c1: c }, wizardView: { c1: 'add' } });
    const model = forgeModel(c.timeline);
    const { step, sub } = wizardStep(model, c, 'add', { skipped });
    expect(step).toBe(6);
    return { conv: c, model, ladder: ladderView(model, c.forge), detail: DEMO_DETAIL as never, sub };
  }
  const calls = () => (useApp.getState().request as unknown as ReturnType<typeof vi.fn>).mock.calls;
  const text = () => document.body.textContent ?? '';

  it('asks before writing the files: what goes where, nothing written yet, the 13 files on request', async () => {
    const p = screenFor(DEMO_EXPORT_CONFIRM);
    expect(p.sub).toBe('add_confirm');
    render(<AddScreen {...p} />);
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Add the agent to your project');
    expect(screen.getByText('Nothing has been written yet.')).toBeTruthy();
    expect(text()).toContain('13 files in one new folder, agents/receipt_reader/. Nothing in your app uses it yet. Writing the files changes no existing file.');
    expect(text()).toContain('13 files, 22 KB');
    expect(text()).toContain('C:\\rec\\expense-app');
    expect(document.querySelectorAll('.add-files [role="listitem"]')).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: /Show the files/ }));
    const files = [...document.querySelectorAll('.add-files .row-title')].map((r) => r.textContent);
    expect(files).toEqual(DEMO_FILES);
    expect(text()).toContain('How to call it');
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Write the files/ }));
    });
    expect(calls().find((c) => c[1] === 'answer')?.[2]).toEqual({ id: 4, value: 'yes' });
  });

  it('after the files, Connect it opens a new conversation in the project with the request typed in, not sent (fallback F1)', () => {
    const p = screenFor(DEMO_FILES_WRITTEN);
    expect(p.sub).toBe('files_written');
    render(<AddScreen {...p} />);
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Connect it in your code');
    expect(screen.getByText('Files written')).toBeTruthy();
    expect(text()).toContain('Switch it on and run the tests');
    fireEvent.click(screen.getByRole('button', { name: /^Connect it/ }));
    const s = useApp.getState();
    const opened = s.prefs.open.find((o) => o.id !== 'c1')!;
    expect(opened).toMatchObject({ cwd: DEMO_CWD, kind: 'chat', wantWorktree: false });
    expect(s.prefs.drafts[opened.id]).toBe(connectPrompt(p.model));
    // Nothing is sent: in a build made from a plan file, build mode would take it as a new build (Gate G1).
    expect(calls().some((c) => c[1] === 'send')).toBe(false);
    expect(s.wizardSkipped.c1).toBe(true);
    cleanup();
    s.activate('c1');
    render(<AddScreen {...screenFor(DEMO_FILES_WRITTEN, true)} />);
    expect(text()).toContain('Continue in the conversation');
    expect(text()).toContain('Connecting it continues in a new conversation');
    fireEvent.click(screen.getByRole('button', { name: 'Open the conversation' }));
    expect(useApp.getState().prefs.active).toBe(opened.id);
  });

  it('Back on the files question answers it no and returns to the step before', async () => {
    const p = screenFor(DEMO_EXPORT_CONFIRM);
    render(<AddScreen {...p} />);
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Back' }));
    });
    expect(calls().find((c) => c[1] === 'answer')?.[2]).toEqual({ id: 4, value: 'no' });
    expect(calls().some((c) => c[1] === 'send')).toBe(false);
    expect(useApp.getState().wizardView.c1).not.toBe('add');
  });

  it('says why when declining the files: no, then the note, then back', async () => {
    const p = screenFor(DEMO_EXPORT_CONFIRM);
    render(<AddScreen {...p} />);
    fireEvent.click(screen.getByRole('button', { name: "Don't write them, and tell Architect why" }));
    const why = screen.getByRole('textbox', { name: 'What should Architect do instead?' });
    fireEvent.change(why, { target: { value: 'Put it under src/agents instead' } });
    await act(async () => {
      fireEvent.keyDown(why, { key: 'Enter' });
    });
    const answered = calls().findIndex((c) => c[1] === 'answer');
    const sent = calls().findIndex((c) => c[1] === 'send');
    expect(calls()[answered][2]).toEqual({ id: 4, value: 'no' });
    expect(calls()[sent][2]).toMatchObject({ text: 'Put it under src/agents instead' });
    expect(answered).toBeLessThan(sent);
    expect(useApp.getState().wizardView.c1).not.toBe('add');
  });

  it('says why the files were not written: Try again sends the export again, and an existing folder opens', async () => {
    const refused = (text: string): Msg[] => [
      ...DEMO_PASSED,
      { type: 'turn_started', text: '/export receipt-reader', quiet: false, at: 1791365950.5 },
      { type: 'event', event: { kind: 'notice', level: 'warn', text } },
      { type: 'turn_finished', text: '/export receipt-reader', quiet: false, at: 1791365950.7 },
    ];
    const p = screenFor(refused('not exported (export_failed: the disk is full)'));
    expect(p.sub).toBe('add_refused');
    render(<AddScreen {...p} />);
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('The files were not written');
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    });
    expect(calls().find((c) => c[1] === 'send')?.[2]).toMatchObject({ text: '/export receipt-reader' });
    cleanup();
    const reveal = vi.fn(async () => undefined);
    (window as unknown as { architectNative?: unknown }).architectNative = { reveal, platform: 'win32' };
    try {
      const exists = screenFor(refused('not exported (export_target_exists: agents/receipt_reader)'));
      render(<AddScreen {...exists} />);
      expect(text()).toContain('agents/receipt_reader/ already exists in this project. Architect never overwrites files.');
      fireEvent.click(screen.getByRole('button', { name: 'Open folder' }));
      expect(reveal).toHaveBeenCalledWith(`${DEMO_CWD}/agents/receipt_reader/`);
    } finally {
      delete (window as unknown as { architectNative?: unknown }).architectNative;
    }
  });

  it("answers the edit no with Don't allow, and stops the coder with Stop adding", async () => {
    const p = screenFor(DEMO_CONNECT_APPROVE);
    render(<AddScreen {...p} />);
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: "Don't allow" }));
    });
    expect(calls().find((c) => c[1] === 'answer')?.[2]).toEqual({ id: 5, value: 'no' });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Stop adding/ }));
    });
    expect(calls().some((c) => c[1] === 'cancel')).toBe(true);
  });

  it('Skip leaves the connect part to the person', () => {
    const p = screenFor(DEMO_FILES_WRITTEN);
    render(<AddScreen {...p} />);
    fireEvent.click(screen.getByRole('button', { name: "Skip: I'll connect it myself" }));
    expect(useApp.getState().wizardSkipped.c1).toBe(true);
    cleanup();
    const skipped = screenFor(DEMO_FILES_WRITTEN, true);
    expect(skipped.sub).toBe('connected');
    render(<AddScreen {...skipped} />);
    expect(text()).toContain("The files are written. Connect it in your code when you're ready: the README says how.");
    expect(text()).toContain('Not connected');
  });

  it("asks to allow the edit with the preview's own counts, and leaves Allow for this conversation to the transcript", async () => {
    const p = screenFor(DEMO_CONNECT_APPROVE);
    expect(p.sub).toBe('connect_approve');
    render(<AddScreen {...p} />);
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Allow this edit to app/extract.py?');
    expect(text()).toContain('Nothing is saved until you allow it.');
    // The preview was cut at its line limit, so the page gives no line counts that would undercount the edit.
    expect(document.querySelector('.add-change .add-counts')).toBeNull();
    expect(text()).not.toContain('+15');
    expect(text()).toContain('The edit, in 2 parts');
    expect(text()).toContain('Part 1 of 2');
    // Lines both sides keep are context, not removed and added again; what lies past the cut is left out.
    const first = document.querySelectorAll('.change-block')[0];
    expect([...first.querySelectorAll('.change-line.is-add')].map((l) => l.textContent)).toEqual(['+import asyncio', '+from pathlib import Path']);
    expect(first.querySelectorAll('.change-line.is-del')).toHaveLength(0);
    expect(first.querySelectorAll('.change-line.is-unseen')).toHaveLength(0);
    expect(first.textContent).not.toContain('from pydantic import BaseModel');
    expect(first.textContent).toContain('Architect shows only the first 12 lines of each part before you allow it, so the rest of this part is not shown.');
    expect(screen.queryByText('Allow for this conversation')).toBeNull();
    // The coder's own numbered steps, as a list, not one quoted line.
    expect([...document.querySelectorAll('.add-said-steps li')].map((l) => l.textContent)).toEqual([
      'Add the imports after existing imports',
      'Change EXTRACTOR = "general-model" to EXTRACTOR = "receipt-agent"',
      'Replace the entire body of the extract() function with the new implementation',
    ]);
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Allow this edit/ }));
    });
    expect(calls().find((c) => c[1] === 'answer')?.[2]).toEqual({ id: 5, value: 'yes' });
  });

  it('once connected: the flag line to set, the edit the coder made, and the test command to run, with no test result', () => {
    const p = screenFor(DEMO_CONNECTED);
    expect(p.sub).toBe('connected');
    render(<AddScreen {...p} />);
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Your agent is in your project.');
    expect(text()).toContain('The files are written and extract() now calls the agent. One thing is left: switch it on.');
    expect(screen.getByText('ARCHITECT_FEATURE_4D80F3D5=1')).toBeTruthy();
    expect(screen.getByRole('button', { name: /Copy the flag line/ })).toBeTruthy();
    const connect = document.querySelector('[data-row="connect"]')!.textContent!;
    expect(connect).toContain('app/extract.py');
    expect(connect).toContain('+15 \u22123 lines');
    expect(connect).toContain('you allowed this edit');
    expect(text()).toContain("4D80F3D5 is the start of this agent's fingerprint");
    const tests = document.querySelector('[data-row="tests"]')!.textContent!;
    expect(tests).toContain('uv run pytest agents/receipt_reader/tests -p no:cacheprovider');
    expect(tests).not.toMatch(/passed/i);
    expect(text()).not.toMatch(/\d+ of \d+ passed/);
    expect(text()).toContain('from agents.receipt_reader import');
    expect(screen.getByRole('button', { name: /See it in Your agents/ })).toBeTruthy();
  });

  it('keeps every step 6 screen free of em dashes and the banned words', () => {
    for (const msgs of [DEMO_EXPORT_CONFIRM, DEMO_FILES_WRITTEN, DEMO_CONNECT_APPROVE, DEMO_CONNECTED]) {
      render(<AddScreen {...screenFor(msgs)} />);
      // Code (the diff, the import line, paths) and the coder's quoted words are not the app's words.
      const page = document.body.cloneNode(true) as HTMLElement;
      page.querySelectorAll('code, pre, q, .edit-preview').forEach((el) => el.remove());
      const words = page.textContent ?? '';
      expect(words).not.toMatch(/\u2014|\blicensed\b|\brefused\b|\bpreset\b|\bForge\b|\bharness\b|\brung\b|\bladder\b|held-out|\btuning\b/);
      cleanup();
    }
  });
});
