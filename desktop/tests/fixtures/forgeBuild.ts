/** A build in build mode as the bridge reports it (strings from architect/agent/build.py). */
const ev = (event: Record<string, unknown>) => ({ type: 'event', event });

export const BEFORE_PLAN: Record<string, unknown>[] = [
  { type: 'turn_started', text: 'Pull the invoice number, date and total out of invoice text' },
  ev({ kind: 'thinking', active: true }),
  ev({ kind: 'tool_started', call_id: 'c1', name: 'make', summary: 'invoice extractor' }),
  ev({ kind: 'progress', text: 'interview: reading the request with claude-sonnet-5 ...' }),
  { type: 'ask', ask: { id: 1, kind: 'question', question: 'Where will this agent live?\n  [1] Run it here (installed)\n  [2] Put it into software\nChoose', default: '1', choices: ['1', '2'] } },
  { type: 'ask_closed', id: 1 },
  ev({ kind: 'progress', text: 'stage 0 infer spec: one call to claude-sonnet-5 ...' }),
  ev({ kind: 'progress', text: 'floor: drafting 12 cases with claude-sonnet-5 ...' }),
  ev({ kind: 'subagent', agent: 'floor', text: "'invoices' configured: 12 cases drafted, SYNTHETIC (6 tuning / 6 held-out)" }),
  ev({
    kind: 'plan_card', title: 'Plan: invoice-extractor',
    rows: [
      ['task', 'Extract invoice number, date and total from invoice text'],
      ['engineer', 'Forge (claude-sonnet-5) writes a custom harness; baseline to beat: extractor'],
      ['inputs', 'text'],
      ['outputs', 'invoice_number, date, total'],
      ['floor', "held_out_tasks >= 0.90 on 'invoices'"],
      ['budget', '$0.02 and 60s per run; compile up to $5.00 and 30m'],
      ['install as', 'invoice-extractor (installed agent)'],
    ],
    footer: 'spec 1a2b3c4d. Enter compiles; answer no to edit with /floor, /budget or /model first.',
  }),
  { type: 'ask', ask: { id: 2, kind: 'question', question: 'Compile this agent?', default: 'yes', choices: [] } },
];

export const BUILDING: Record<string, unknown>[] = [
  { type: 'answered', id: 2, value: 'yes' },
  { type: 'ask_closed', id: 2 },
  ev({ kind: 'progress', text: 'stage 1 select preset: 0 calls, $0.0000' }),
  ev({ kind: 'progress', text: 'stage 2 tune: 14 calls, $0.0300' }),
  ev({ kind: 'progress', text: 'stage 3 forge: 31 calls, $0.4100' }),
  ev({ kind: 'subagent', agent: 'forge', text: 'forge_measure' }),
];

export const DONE: Record<string, unknown>[] = [
  ev({ kind: 'progress', text: 'stage 4 verify: 37 calls, $0.5200' }),
  ev({
    kind: 'plan_card', title: 'Result: invoice-extractor v1',
    rows: [
      ['design', 'Two passes: locate fields, then verify totals.'],
      ['floor', '1.00/0.90 on 6 held-out SYNTHETIC cases'],
      ['cost', '$0.97 to build; up to $0.02 and 60s per run'],
      ['destination', 'run it here'],
      ['next', '/run invoice-extractor <text>'],
    ],
    footer: '',
  }),
  ev({ kind: 'tool_finished', call_id: 'c1', name: 'make', ok: true, summary: 'licensed' }),
  ev({ kind: 'answer', text: 'invoice-extractor is installed.' }),
  { type: 'turn_finished', text: '' },
];
