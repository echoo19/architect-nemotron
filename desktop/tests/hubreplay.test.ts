/**
 * A page that re-attaches to a running conversation gets its recent messages again, the
 * Forge feed included, so a replayed build redraws its bar view and harness versions.
 */
import { EventEmitter } from 'node:events';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import type { WebSocket } from 'ws';
import type { BridgeProcess } from '../server/bridgeProcess';
import { Hub, REPLAYED } from '../server/hub';

class StubBridge extends EventEmitter {
  exited: { code: number | null } | null = null;
  ready = Promise.resolve();
  failure(): string {
    return '';
  }
  sendRaw(): void {}
  async stop(): Promise<void> {}
}

describe('the replay a re-attached page gets', () => {
  it('includes the Forge feed with the events, asks and state', () => {
    expect([...REPLAYED].sort()).toEqual(['ask', 'ask_closed', 'event', 'forge', 'state', 'turn_finished', 'turn_started']);
    const stub = new StubBridge();
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'adt-hub-'));
    const hub = new Hub({ root: home, home, spawn: () => stub as unknown as BridgeProcess });
    hub.open('c1');
    const stage = { type: 'forge', data: { kind: 'stage', stage: 1, model_index: 1, model_count: 3 } };
    const tool = { type: 'forge', data: { kind: 'tool', tool: 'forge_measure', depth: 0, result: { measurement: { score: 0.45 } } } };
    stub.emit('message', { type: 'event', event: { kind: 'plan_card', title: 'Plan: x', rows: [], data: { bar: 0.4 } } });
    stub.emit('message', stage);
    stub.emit('message', tool);
    stub.emit('message', { id: 7, result: {} }); // a reply is for its request only
    const sent: Array<Record<string, unknown>> = [];
    const socket = { readyState: 1, OPEN: 1, send: (text: string) => sent.push(JSON.parse(text) as Record<string, unknown>) };
    hub.handleFrame(socket as unknown as WebSocket, JSON.stringify({ t: 'open', conv: 'c1' }));
    const opened = sent.find((f) => f.t === 'opened')!;
    expect(opened.fresh).toBe(false);
    const replay = opened.replay as Array<Record<string, unknown>>;
    expect(replay.map((m) => m.type)).toEqual(['event', 'forge', 'forge']);
    expect(replay[1]).toEqual(stage);
    expect(replay[2]).toEqual(tool);
    hub.routines.stop();
    fs.rmSync(home, { recursive: true, force: true });
  });
});
