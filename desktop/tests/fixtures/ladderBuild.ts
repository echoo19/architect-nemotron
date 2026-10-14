/**
 * A three-rung build with Forge on the first rung, as the bridge reports it: the plan
 * card's data, the live feed (stages with the ladder and practice scores, Forge's tool
 * results and harness drafts), and after the compile the result card's signed counts.
 * Shapes from architect/agent/build.py (`_plan_data`, `_exam_data`), compiler/__init__.py
 * (`_enter`) and compiler/forge_tools.py (`_notify`, `_measurement_view`). Forge's quick
 * measures run 8 of the 100 practice cases; its submissions run all 100, as the best
 * preset's score does. Placeholder numbers.
 */
const ev = (event: Record<string, unknown>) => ({ type: 'event', event });
const forge = (data: Record<string, unknown>) => ({ type: 'forge', data });

export const LADDER = ['nvidia/Nemotron-3_5-Lightning', 'nvidia/nemotron-3-super-120b-a12b', 'nvidia/Nemotron-3-Ultra-550b-a55b'];
export const SPEC = '3451bd1b13c4558a67a62bc042905ecbf05c03d24dcfb279964970c20b694f22';
export const PACK = '3eabb4d343f9f7a02c059dd5615df0684f86700d7edf929fecb151086de3e02f';
const where = (index: number) => ({ depth: 0, model: LADDER[index], model_index: index + 1, model_count: 3, ladder: LADDER });
const stage = (index: number, n: number, extra: Record<string, unknown> = {}) => forge({ kind: 'stage', stage: n, stages: [1, 2, 3, 4].filter((s) => s <= n), ...where(index), ...extra });
const counts = (correct: number, invalid = 0) => ({ cases: 100, correct, invalid, over_budget: 0 });
/** A Forge tool's measurement as `_measurement_view` sends it. */
export const measurement = (score: number, sampleCount: number) => ({ score, floor_limit: 0.4, floor_passed: score >= 0.4, sample_count: sampleCount });

export const HARNESS_1 = 'async def run(context, task):\n    reply = await context.call(task["text"])\n    return reply\n';
export const HARNESS_2 = 'async def run(context, task):\n    reply = await context.call(task["text"])\n    total = parse(reply)\n    return total\n';

export const PLAN: Record<string, unknown>[] = [
  { type: 'turn_started', text: 'Read a receipt and give back the total, subtotal, tax and item count' },
  ev({ kind: 'tool_started', call_id: 'c1', name: 'make', summary: 'receipt reader' }),
  ev({ kind: 'subagent', agent: 'floor', text: "'receipts' configured: CORD v2 · CC BY 4.0 · 200 receipts (100 tuning / 100 held-out)" }),
  ev({
    kind: 'plan_card', title: 'Plan: receipt-reader',
    rows: [
      ['task', 'Turn receipt text into total, subtotal, tax and item count'],
      ['spec', `${SPEC} (frozen, receipts-v1.json)`],
      ['engineer', `Forge (${LADDER[2]}) writes a custom harness; baseline to beat: extractor`],
      ['inputs', 'text'],
      ['outputs', 'item_count, subtotal, tax, total'],
      ['floor', "held_out_tasks >= 0.40 on 'receipts'"],
      ['floor data', 'CORD v2 · CC BY 4.0 · 200 receipts (100 tuning / 100 held-out)'],
      ['budget', '$0.01 and 60 s per run; compile up to $5.00 and 30 min'],
      ['forge pack', `${PACK} (no text from this task)`],
      ['model', `cheapest that passes over ${LADDER.join(', ')}`],
      ['install as', 'receipt-reader (installed agent)'],
    ],
    footer: `spec ${SPEC.slice(0, 8)}. Enter compiles; answer no to edit with /floor, /budget or /model first.`,
    data: { spec_hash: SPEC, bar: 0.4, domain: [0.2, 0.7], kind: 'held_out_tasks', ladder: LADDER, practice: 100, exam: 100, pack_hash: PACK, frozen: 'receipts-v1.json' },
  }),
  { type: 'ask', ask: { id: 2, kind: 'question', question: 'Compile this agent?', default: 'yes', choices: [] } },
];

/** Lightning: presets, then Forge (two harness versions), then its exam; Super starts. */
export const FORGING: Record<string, unknown>[] = [
  { type: 'answered', id: 2, value: 'yes' },
  { type: 'ask_closed', id: 2 },
  ev({ kind: 'progress', text: 'stage 1 select preset: 0 calls, $0.0000' }),
  stage(0, 1),
  stage(0, 2),
  ev({ kind: 'progress', text: 'stage 2 tune: 40 calls, $0.0200' }),
  stage(0, 3, { tuned: { score: 0.31, passed: false } }),
  ev({ kind: 'progress', text: 'stage 3 forge: 41 calls, $0.0210' }),
  forge({ kind: 'tool', tool: 'forge_session', event: 'started', depth: 0, result: { mode: 'agent', baseline: { score: 0.31, floor_passed: false } } }),
  forge({ kind: 'tool', tool: 'forge_check', depth: 0, result: { ok: true, code: null }, source: { path: 'harness.py', sha256: 'a1', text: HARNESS_1 } }),
  forge({ kind: 'tool', tool: 'forge_measure', depth: 0, result: { ok: true, measurement: measurement(0.375, 8) } }),
  forge({ kind: 'tool', tool: 'forge_submit', depth: 0, result: { ok: false, code: 'tuning_failed', accepted: false, measurement: measurement(0.36, 100) } }),
  forge({ kind: 'tool', tool: 'forge_measure', depth: 0, result: { ok: true, measurement: measurement(0.5, 8) }, source: { path: 'harness.py', sha256: 'b2', text: HARNESS_2 } }),
  forge({ kind: 'tool', tool: 'forge_submit', depth: 0, result: { ok: true, accepted: true, measurement: measurement(0.48, 100) } }),
  ev({ kind: 'subagent', agent: 'forge', text: 'forge_submit accepted' }),
  stage(0, 4, { tuned: { score: 0.31, passed: false }, examined: { score: 0.48, passed: true } }),
  ev({ kind: 'progress', text: 'stage 4 verify: 140 calls, $0.0900' }),
  stage(1, 1),
];

export const SHIPPED: Record<string, unknown>[] = [
  stage(1, 2),
  stage(1, 4, { tuned: { score: 0.5, passed: true }, examined: { score: 0.5, passed: true } }),
  stage(2, 1),
  stage(2, 2),
  stage(2, 4, { tuned: { score: 0.49, passed: true }, examined: { score: 0.49, passed: true } }),
  ev({
    kind: 'plan_card', title: 'Result: receipt-reader v1',
    rows: [
      ['engineer', 'a custom harness written by Forge (40 nodes, 900 bytes, 0 licensed sub-harnesses) after 1 submissions and 2 measures; it outranked the tuned extractor baseline on tuning'],
      ['floor', '0.44/0.40 on 100 held-out cases (correct 44/100 · invalid 1)'],
      ['cost', '$0.41 to build; up to $0.01 and 60 s per run'],
      ['destination', 'run it here (installed)'],
      ['next', '/run receipt-reader <input>'],
    ],
    footer: '',
    data: {
      spec_hash: SPEC, pack_hash: PACK, licensed: true, bar: 0.4, kind: 'held_out_tasks', model: LADDER[0], score: 0.44, exam: counts(44, 1),
      rungs: [
        { model: LADDER[0], preset: 'extractor', licensed: true, ships: true, score: 0.44, limit: 0.4, cases: 100, counts: counts(44, 1), codes: [] },
        { model: LADDER[1], preset: 'extractor', licensed: true, ships: false, score: 0.47, limit: 0.4, cases: 100, counts: counts(47), codes: [] },
        { model: LADDER[2], preset: 'extractor', licensed: false, ships: false, score: 0.38, limit: 0.4, cases: 100, counts: counts(38), codes: ['primary_floor_failed'] },
      ],
    },
  }),
  ev({ kind: 'tool_finished', call_id: 'c1', name: 'make', ok: true, summary: 'licensed' }),
  { type: 'turn_finished', text: '' },
];

/** The end of a build that stopped before its result: the turn ends with no result card. */
const STOP: Record<string, unknown>[] = [
  ev({ kind: 'tool_finished', call_id: 'c1', name: 'make', ok: false, summary: '{"error": "cancelled"}' }),
  { type: 'turn_finished', text: '' },
];

/** A build stopped partway through the ladder (Stop, or the app closed): no result. */
export const STOPPED_BUILD: Record<string, unknown>[] = [...PLAN, ...FORGING, ...STOP];

/** A build the judges' relay turned away mid-ladder: an error notice, then the turn ends with no result. */
export const ERR_NOTICE = 'The relay refused the call (too many requests). Try again in a minute.';
export const ERR_BUILD: Record<string, unknown>[] = [...PLAN, ...FORGING, ev({ kind: 'notice', level: 'error', text: ERR_NOTICE }), ...STOP];

/** The export after the build ships: the export plan, its approval (yes), the Exported card. Shapes from architect/agent/export.py. */
export const EXPORT_FILES = ['README.md', 'agent.py', 'receipt-reader.spec.json', 'report.signed.json'];
export const EXPORT_LINE = 'from agents.receipt_reader import FLAG, enabled, run, verify';
export const EXPORTING: Record<string, unknown>[] = [
  { type: 'turn_started', text: '/export receipt-reader' },
  ev({ kind: 'progress', text: 'export: staging receipt_reader from receipt-reader v1 ...' }),
  ev({
    kind: 'plan_card', title: 'Export plan: agents/receipt_reader/',
    rows: [
      ['agent', 'receipt-reader v1: licensed, floor 0.44/0.40, extractor on nvidia/Nemotron-3_5-Lightning'],
      ['writes', 'agents/receipt_reader/ (one new directory; nothing existing is changed)'],
      ['files', EXPORT_FILES.join('\n')],
      ['size', '5 files, 14 KB'],
    ],
    footer: 'Nothing has been written into the repository yet.',
  }),
  { type: 'ask', ask: { id: 3, kind: 'approval', action: 'export', detail: 'receipt_reader (5 files, 14 KB) into agents/receipt_reader/ (one new directory; feature flag OFF; human review required)' } },
];
export const EXPORTED: Record<string, unknown>[] = [
  { type: 'ask_closed', id: 3, answer: 'yes' },
  ev({
    kind: 'plan_card', title: 'Exported: agents/receipt_reader/',
    rows: [
      ['verified', 'files, pins, sub-harnesses and the Stage 4 license: ok'],
      ['python', `# Call the exported agent from Python (run from the repository root).\nimport asyncio\n\n${EXPORT_LINE}\n\ncheck = verify()`],
      ['enable', 'after review: ARCHITECT_AGENT_RECEIPT_READER=1'],
    ],
    footer: 'Human review required; the feature flag is OFF until set.',
  }),
  { type: 'turn_finished', text: '' },
];
export const EXPORTED_BUILD: Record<string, unknown>[] = [...PLAN, ...FORGING, ...SHIPPED, ...EXPORTING, ...EXPORTED];
