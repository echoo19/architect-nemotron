/**
 * The Forge view tests' shared set-up (moved from forgeview.test.tsx): a replayed build,
 * a build conversation, and a bridge whose `request` answers from `replies` (emptied
 * before each test). jsdom lays nothing out, so every container gets the card's width.
 */
import { cleanup } from '@testing-library/react';
import { afterAll, afterEach, beforeAll, beforeEach, vi } from 'vitest';
import { emptyFeed, feedMessage, type ForgeFeed } from '../../src/lib/forge';
import { emptyTimeline, fold, type Timeline } from '../../src/lib/timeline';
import { DEFAULT_PREFS, useApp, type Conv } from '../../src/store/app';

export function replayed(...parts: Record<string, unknown>[][]): { timeline: Timeline; forge: ForgeFeed } {
  let timeline = emptyTimeline();
  let forge = emptyFeed();
  for (const msg of parts.flat()) {
    forge = feedMessage(forge, msg);
    timeline = fold(timeline, msg);
  }
  return { timeline, forge };
}

export function conv(patch: Partial<Conv> = {}): Conv {
  return {
    id: 'c1', status: 'ready', timeline: emptyTimeline(), forge: emptyFeed(), links: [],
    state: { started: true, state: 'forge', permission: 'ask', model: 'fake', models: ['fake'], cwd: 'C:/p' },
    ...patch,
  };
}

export const replies: Record<string, unknown> = {};

/** Registers the per-file and per-test set-up; call once at the top of a test file. */
export function installForgeHarness(): void {
  const width = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientWidth');

  beforeAll(() => {
    // jsdom lays nothing out: give every container the agent card's width.
    Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, get: () => 578 });
  });
  afterAll(() => {
    if (width) Object.defineProperty(HTMLElement.prototype, 'clientWidth', width);
  });

  beforeEach(() => {
    for (const key of Object.keys(replies)) delete replies[key];
    window.matchMedia = window.matchMedia ?? ((() => ({ matches: false, addEventListener() {}, removeEventListener() {} })) as never);
    Element.prototype.scrollIntoView = () => undefined;
    global.fetch = vi.fn(async () => new Response('{}')) as never;
    const request = vi.fn(async (_id: string, method: string) => replies[method] ?? {});
    useApp.setState({
      prefs: { ...DEFAULT_PREFS, open: [{ id: 'c1', cwd: 'C:/p', kind: 'forge' }], active: 'c1' },
      request: request as never,
      convs: {},
      dock: { tab: null },
      wizardView: {},
      wizardSkipped: {},
    });
  });

  afterEach(() => cleanup());
}
