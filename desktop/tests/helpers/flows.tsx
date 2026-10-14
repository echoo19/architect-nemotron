/**
 * The flows tests' shared set-up (moved from flows.test.tsx): a conversation, and the
 * bridge replaced by a recorded `request`. `calls` and `replies` are emptied before
 * each test; a test sets `replies[method]` to answer a call.
 */
import { cleanup } from '@testing-library/react';
import { afterEach, beforeEach, vi } from 'vitest';
import { emptyTimeline } from '../../src/lib/timeline';
import { DEFAULT_PREFS, useApp, type Conv } from '../../src/store/app';

export function conv(patch: Partial<Conv> = {}): Conv {
  return {
    id: 'c1',
    status: 'ready',
    timeline: emptyTimeline(),
    state: { started: true, state: 'normal', permission: 'ask', model: 'fake', models: ['fake'], cwd: 'C:/p', spent_usd: 0, budget_usd: 25, context_percent: 3 },
    forge: { stages: [], tools: [] },
    links: [],
    ...patch,
  };
}

export const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
export const replies: Record<string, unknown> = {};

/** Registers the per-test set-up; call once at the top of a test file. */
export function installFlowsHarness(): void {
  beforeEach(() => {
    calls.length = 0;
    for (const key of Object.keys(replies)) delete replies[key];
    // matchMedia and scrollIntoView are not in jsdom.
    window.matchMedia = window.matchMedia ?? ((() => ({ matches: false, addEventListener() {}, removeEventListener() {} })) as never);
    Element.prototype.scrollIntoView = () => undefined;
    global.fetch = vi.fn(async () => new Response('{}')) as never;
    const request = vi.fn(async (_id: string, method: string, params: Record<string, unknown> = {}) => {
      calls.push({ method, params });
      const reply = replies[method];
      if (reply instanceof Error) throw reply;
      return typeof reply === 'function' ? (reply as (p: unknown) => unknown)(params) : reply ?? {};
    });
    useApp.setState({
      prefs: { ...DEFAULT_PREFS, open: [{ id: 'c1', cwd: 'C:/p', kind: 'chat' }], active: 'c1' },
      request: request as never,
      send: (async (id: string, text: string, quiet?: boolean, steer?: boolean) => { await request(id, 'send', { text, quiet, steer: !!steer }); }) as never,
      cancel: (async (id: string) => { await request(id, 'cancel'); }) as never,
      convs: {},
    });
  });

  afterEach(() => cleanup());
}
