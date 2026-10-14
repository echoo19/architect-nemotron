/** One vocabulary on screen: practice, exam, bar, licensed, refused (lib/vocab.ts, lib/timeline.ts). */
import { describe, expect, it } from 'vitest';
import { prettyModel, rungName } from '../src/lib/models';
import { emptyTimeline, fold, subagentTitle, workLabel, type Item } from '../src/lib/timeline';
import { forgeModel, friendlyLine } from '../src/lib/forge';
import {
  buildLabel, cardModels, cardRows, cardState, commandWords, examWords, exportRows, hashNote, noticeLine, noticeWords, planFacts, practiceCount,
  practiceWords, questionOptions, questionWords, reasonWords, rerunCommand, resultLead, screenWords,
} from '../src/lib/vocab';
import { BEFORE_PLAN, DONE } from './fixtures/forgeBuild';
import { ERR_BUILD, EXPORT_FILES, EXPORT_LINE, EXPORTED, EXPORTED_BUILD, EXPORTING, FORGING, LADDER, PLAN, SHIPPED, STOPPED_BUILD } from './fixtures/ladderBuild';

const card = (messages: Record<string, unknown>[], prefix: string) => {
  const event = messages.map((m) => (m as { event?: { kind?: string; title?: string; rows?: Array<[string, string]> } }).event)
    .find((e) => e?.kind === 'plan_card' && e.title?.startsWith(prefix));
  return event!.rows!;
};

describe('the plan card in the app\'s words', () => {
  it('names the bar, practice and exam, and the models by name', () => {
    const rows = Object.fromEntries(cardRows(card(PLAN, 'Plan'), { models: LADDER }));
    expect(rows.Bar).toBe('0.40');
    expect(rows['Practice and exam']).toBe('CORD v2 · CC BY 4.0 · 200 receipts (100 practice / 100 exam)');
    expect(rows.Models).toBe('The cheapest one that clears the bar: Nemotron 3.5 Lightning, Nemotron 3 Super or Nemotron 3 Ultra');
    expect(rows.Engineer).toBe('Forge (Nemotron 3 Ultra) writes a custom harness; preset to beat: extractor');
    expect(rows.Takes).toBe('text');
    expect(Object.fromEntries(cardRows([['destination', 'run it here: /run r, `architect run`, HTTP through /serve']])).Destination)
      .toBe('Run it here: /run r, architect run, HTTP through /serve');
    // The hashes leave the rows: the card draws them as hash lines.
    expect(Object.keys(rows)).not.toContain('Spec');
    expect(Object.keys(rows)).not.toContain('Forge pack');
    expect(JSON.stringify(rows)).not.toMatch(/held.out|tuning|held_out_tasks|floor/i);
  });

  it('starts each sentence row with a capital and keeps field lists, names and figures as written', () => {
    const rows = Object.fromEntries(cardRows([
      ['task', 'pull the total out of receipt text'],
      ['engineer', 'tuned preset only: extractor (Forge off)'],
      ['inputs', 'receipt'],
      ['outputs', 'item_count, subtotal, tax, total'],
      ['invalid output', 'counts as a miss, at most 2 of 100 held-out runs'],
      ['budget', '$0.02 and 60 s per run; compile up to $0.60 and 60 min'],
      ['forge budget', 'up to $1.20 and 45 min on top of the compile budget (6 submissions)'],
      ['destination', 'into software: agents/receipt_reader/ in this repository after the license'],
      ['install as', 'receipt-reader (installed agent)'],
      ['note', 'input_schema normalized into a JSON Schema object'],
    ]));
    const notes = cardRows([
      ['note', 'per-run budget raised to $0.04 and 180 s so two model calls fit'],
      ['note', 'specs/receipt-reader.json: frozen'],
    ]).map(([, value]) => value);
    expect(notes).toEqual(['Per-run budget raised to $0.04 and 180 s so two model calls fit', 'specs/receipt-reader.json: frozen']);
    expect(rows.Task).toBe('pull the total out of receipt text');
    expect(rows.Engineer).toBe('Tuned preset only: extractor (Forge off)');
    expect(rows.Takes).toBe('receipt');
    expect(rows['Gives back']).toBe('item_count, subtotal, tax, total');
    expect(rows['Invalid output']).toBe('Counts as a miss, at most 2 of 100 exam runs');
    expect(rows.Budget).toBe('$0.02 and 60 s per run; build up to $0.60 and 60 min');
    expect(rows['Forge budget']).toBe('Up to $1.20 and 45 min on top of the build budget (6 submissions)');
    expect(rows.Destination).toBe('Into software: agents/receipt_reader/ in this repository after the license');
    expect(rows['Install as']).toBe('receipt-reader (installed agent)');
    expect(rows.Note).toBe('input_schema normalized into a JSON Schema object');
  });

  it('reads the result\'s exam row in the same words', () => {
    const rows = Object.fromEntries(cardRows(card(SHIPPED, 'Result'), { result: true, models: LADDER }));
    expect(rows.Exam).toBe('0.44 on 100 exam cases, bar 0.40 (44 of 100 correct, 1 invalid)');
    expect(rows.Engineer).toContain('outranked the extractor preset in practice');
    expect(JSON.stringify(rows)).not.toMatch(/held.out|tuning|tuned|baseline/i);
  });

  it('names each rung of the result card\'s ladder row by model', () => {
    const models = cardModels({ rungs: LADDER.map((model) => ({ model })) });
    const raw = 'Nemotron-3_5-Lightning 25/100 refused · nemotron-3-super-120b-a12b 51/100 licensed, ships · Nemotron-3-Ultra-550b-a55b 49/100 licensed';
    const rows = Object.fromEntries(cardRows([['ladder', raw]], { result: true, models }));
    expect(rows.Ladder).toBe('Nemotron 3.5 Lightning 25/100 refused · Nemotron 3 Super 51/100 licensed, ships · Nemotron 3 Ultra 49/100 licensed');
    expect(cardModels({ ladder: LADDER })).toEqual(LADDER);
    expect(cardModels(undefined)).toEqual([]);
  });

  it('reads the export plan\'s agent row as the exam and the bar', () => {
    const rows = Object.fromEntries(cardRows([['agent', 'r v1: licensed, floor 0.51/0.40, extractor on nvidia/nemotron-3-super-120b-a12b']], { models: LADDER }));
    expect(rows.Agent).toBe('r v1: licensed, exam 0.51, bar 0.40, extractor on Nemotron 3 Super');
    expect(JSON.stringify(rows)).not.toMatch(/floor/i);
  });

  it('turns a refusal\'s codes into reasons', () => {
    expect(examWords('0.71/0.80 (correct 71/100 · invalid 2) (primary_floor_failed)'))
      .toBe('0.71, bar 0.80 (71 of 100 correct, 2 invalid; below the bar on the exam)');
    expect(examWords('- (compile_budget)')).toBe('No score (the build ran out of budget)');
    expect(examWords('1.00/0.90 on 6 held-out cases (correct 6/6 · invalid 0) (SYNTHETIC floor)'))
      .toBe('1.00 on 6 exam cases, bar 0.90 (6 of 6 correct, 0 invalid; synthetic cases)');
    expect(practiceWords("configured floor 'tasks': 6 cases (3 tuning / 3 held-out)")).toBe("6 cases from 'tasks' (3 practice / 3 exam)");
    // The install notice is one short sentence: what Forge did is in Details.
    expect(noticeWords('installed r v1: floor 0.44/0.40, $0.15 spent. a custom harness written by Forge (225 nodes, 1299 bytes, 0 licensed sub-harnesses) after 2 submissions and 3 measures; it outranked the tuned extractor baseline on tuning'))
      .toBe('Installed r, version 1. Exam 0.44, bar 0.40, $0.15 spent.');
    expect(noticeWords('installed r v2: floor 1.00/0.90 (SYNTHETIC floor), $0.02 spent.'))
      .toBe('Installed r, version 2. Exam 1.00 on synthetic cases, bar 0.90, $0.02 spent.');
    expect(noticeWords('licensed, but not installed as r: the installed r is better')).toBe('Licensed, but not installed as r: the installed r is better');
    expect(noticeWords('specs/r.json: not usable (bad_spec); keeping r')).toBe('specs/r.json: not usable (bad_spec); keeping r');
    expect(noticeWords('not licensed (primary_floor_failed, tuning_failed); next: add more real cases with /floor add <file>'))
      .toBe('Refused (below the bar on the exam, below the bar in practice). Next: add more real cases with /floor add <file>');
  });
});

describe('the conversation\'s fold lines', () => {
  it('says what the case drafter did instead of "Used floor"', () => {
    let t = emptyTimeline();
    t = fold(t, { type: 'turn_started', text: '/make x' }, 1000);
    t = fold(t, { type: 'event', event: { kind: 'subagent', agent: 'floor', text: "'x' configured: 6 cases (3 tuning / 3 held-out)" } }, 1000);
    t = fold(t, { type: 'turn_finished', text: '' }, 43_000);
    const work = t.items.find((i) => i.kind === 'work') as Extract<Item, { kind: 'work' }>;
    expect(workLabel(work)).toBe('Set up the practice and exam cases in 42s');
  });

  it('names a run of an installed agent and a build by what they did', () => {
    const label = (lines: Array<[string, string]>, seconds: number) => {
      let t = emptyTimeline();
      t = fold(t, { type: 'turn_started', text: '/make x' }, 1000);
      for (const [agent, text] of lines) t = fold(t, { type: 'event', event: { kind: 'subagent', agent, text } }, 1000);
      t = fold(t, { type: 'turn_finished', text: '' }, 1000 + seconds * 1000);
      return workLabel(t.items.find((i) => i.kind === 'work') as Extract<Item, { kind: 'work' }>);
    };
    expect(label([['receipt-reader', 'running v2 on nvidia/nemotron-3-super']], 1)).toBe('Ran receipt-reader in 1s');
    expect(label([['floor', "'x' configured: 6 cases (3 tuning / 3 held-out)"], ['compiler', 'stage 1/4: select'], ['compiler', 'stage 4/4: verify']], 151))
      .toBe('Built the agent in 2m 31s');
  });
});

describe('model names', () => {
  it('reads Nemotron ids by name and a rung by its tier', () => {
    expect(LADDER.map(prettyModel)).toEqual(['Nemotron 3.5 Lightning', 'Nemotron 3 Super', 'Nemotron 3 Ultra']);
    expect(LADDER.map(rungName)).toEqual(['Lightning', 'Super', 'Ultra']);
    expect(rungName('claude-haiku-4-5-20251001')).toBe('Claude Haiku 4.5');
    expect(rungName('fake')).toBe('fake');
  });
});

describe('one verb for the build', () => {
  it("asks to build, titles the compiler's line Build and says build budget", () => {
    expect(questionWords('Compile this agent?')).toBe('Build this agent?');
    expect(subagentTitle('compiler')).toBe('Build');
    expect(friendlyLine('compiling receipt-reader (spec ba94f72c)')).toBe('receipt-reader');
    const rows = Object.fromEntries(cardRows(card(PLAN, 'Plan'), { models: LADDER }));
    expect(rows.Budget).toBe('$0.01 and 60 s per run; build up to $5.00 and 30 min');
    expect(JSON.stringify(rows)).not.toMatch(/compile/i);
    expect(practiceWords('raise the compile budget: /budget compile 10.00 60')).toBe('raise the build budget: /budget build 10.00 60');
  });
});

describe('the command menu', () => {
  it("says what commands do in the app's words, with no third-party names", () => {
    const agents = commandWords({ name: '/agents', usage: '/agents', summary: 'Installed agents, versions, license and floor' });
    expect(agents.summary).toBe('Installed agents, their versions, licenses and bars');
    const budget = commandWords({ name: '/budget', usage: '/budget [session <usd> [min] | run <usd> <s> | compile <usd> [min]]', summary: 'Show or change the session, per-run or compile budget' });
    expect(budget.usage).toBe('/budget [session <usd> [min] | run <usd> <s> | build <usd> [min]]');
    expect(budget.summary).toBe('Show or change the session, per-run or build budget');
    expect(commandWords({ name: '/make', usage: '/make <description>', summary: 'Build an agent for one request: interview, plan card, build, exam' }).summary)
      .toBe('Build an agent from a description or a spec file');
    expect(commandWords({ name: '/logout', usage: '/logout <provider>', summary: "Forget a provider's stored key (or sign out of chatgpt)" }).summary)
      .not.toMatch(/chatgpt/i);
  });
});

describe('the pack note', () => {
  it('says what the pack holds', () => {
    expect(hashNote('3eab (no text from this task)')).toBe('holds no text from this task');
    expect(hashNote("3eab (2 matches with this task's text)")).toBe('holds text from this task in 2 places');
    expect(hashNote('3451 (frozen, receipt-reader.spec.json)')).toBe('frozen, receipt-reader.spec.json');
  });
});

// ---------------------------------------------------------------- the agent card (redesign v2 §3.6)

const folded = (...parts: Record<string, unknown>[][]) => parts.flat().reduce((t, m) => fold(t, m), emptyTimeline());
const modelOf = (...parts: Record<string, unknown>[][]) => forgeModel(folded(...parts));
const withData = (data: Record<string, unknown>) => PLAN.map((msg) => {
  const event = msg.event as Record<string, unknown> | undefined;
  return event?.kind === 'plan_card' ? { ...msg, event: { ...event, data: { ...(event.data as object), ...data } } } : msg;
});
const withRow = (messages: Record<string, unknown>[], row: [string, string]) => messages.map((msg) => {
  const event = msg.event as Record<string, unknown> | undefined;
  return event?.kind === 'plan_card' && String(event.title).startsWith('Plan') ? { ...msg, event: { ...event, rows: [...(event.rows as Array<[string, string]>), row] } } : msg;
});

describe("the card's words", () => {
  it('says pass mark, passed, did not pass, practice and exam, and a Nemotron by its tier when short', () => {
    expect(screenWords('Licensed: above the bar on held-out cases after tuning')).toBe('Passed: above the pass mark on exam cases after practice');
    expect(screenWords('refused, not licensed')).toBe('did not pass, did not pass');
    expect(screenWords('Nemotron 3.5 Lightning and Nemotron 3 Super')).toBe('Nemotron 3.5 Lightning and Nemotron 3 Super');
    expect(screenWords('Nemotron 3.5 Lightning and Nemotron 3 Super', true)).toBe('Lightning and Super');
  });

  it('turns the plan into Facts, the job first and wide', () => {
    const facts = planFacts(modelOf(withData({ build_usd: 0.6, build_s: 3600 })));
    expect(facts[0]).toEqual({ value: 'Turn receipt text into total, subtotal, tax and item count', label: 'Job', wide: true });
    expect(facts.slice(1).map((f) => [f.label, f.value])).toEqual([
      ['Takes', 'Text'], ['Gives back', 'Item count, subtotal, tax, total'], ['Cases', '100 practice, 100 exam'],
      ['Pass mark', '40 of 100'], ['Models tried', 'Lightning, Super, Ultra'], ['Build limit', '$0.60, 60 min'],
    ]);
    // An older plan with no data: the rows still say the limit; no case counts, so the pass mark is a share.
    const older = Object.fromEntries(planFacts(modelOf(BEFORE_PLAN)).map((f) => [f.label, f.value]));
    expect(older).toEqual({
      Job: 'Extract invoice number, date and total from invoice text', Takes: 'Text', 'Gives back': 'Invoice number, date, total',
      'Pass mark': '90%', 'Build limit': '$5.00, 30 min',
    });
    // A spec with rule checks gets one more cell.
    expect(planFacts(modelOf(withRow(PLAN, ['checks', 'Totals must add up. Dates are ISO.']))).at(-1)).toEqual({ value: '2 checks after each reply', label: 'Rules' });
  });

  it('names the build button by its limit', () => {
    expect(buildLabel(modelOf(withData({ build_usd: 0.6 })))).toBe('Build for up to $0.60');
    expect(buildLabel(modelOf(BEFORE_PLAN))).toBe('Build it');
  });

  it("pairs a numbered question's choices with their words, parentheses dropped", () => {
    const ask = { id: 1, kind: 'question' as const, question: 'Where will this agent live?\n  [1] Run it here (installed)\n  [2] Put it into software\nChoose', default: '1', choices: ['1', '2'] };
    expect(questionOptions(ask)).toEqual([{ value: '1', label: 'Run it here' }, { value: '2', label: 'Put it into software' }]);
    expect(questionOptions({ id: 2, kind: 'question', question: 'Name it?', choices: [] })).toBeNull();
  });

  it('lists what the export wrote and the line that imports it', () => {
    expect(exportRows(modelOf(EXPORTED_BUILD))).toEqual({ folder: 'agents/receipt_reader/', files: EXPORT_FILES, line: EXPORT_LINE });
    expect(exportRows(modelOf(PLAN))).toEqual({ files: [] });
  });

  it('reads the exam result as correct of the cases, and who ships', () => {
    expect(resultLead(modelOf(PLAN, FORGING, SHIPPED))).toEqual({ correct: 44, of: 100, passed: true, ships: 'Lightning' });
    // An older result card: from its floor row.
    expect(resultLead(modelOf(BEFORE_PLAN, [{ type: 'answered', id: 2, value: 'yes' }], DONE))).toEqual({ correct: 6, of: 6, passed: true });
    expect(resultLead(modelOf(PLAN))).toBeNull();
  });

  it("counts the practice cases from the plan's data, else its floor data row", () => {
    expect(practiceCount(modelOf(PLAN))).toBe(100);
    const older = withRow(BEFORE_PLAN, ['floor data', "'invoices' configured: 12 cases drafted, SYNTHETIC (6 tuning / 6 held-out)"]);
    expect(practiceCount(modelOf(older))).toBe(6);
    expect(practiceCount(modelOf(BEFORE_PLAN))).toBeUndefined();
  });

  it("builds again from the spec file when there is one, else from the person's sentence through /make", () => {
    // The plan says it came from receipts-v1.json.
    expect(rerunCommand(modelOf(PLAN))).toBe('/make @receipts-v1.json');
    // The person's own /make @file wins.
    const own = [{ type: 'turn_started', text: '/make @specs/receipt-reader.json' }, ...PLAN.slice(1)];
    expect(rerunCommand(modelOf(own))).toBe('/make @specs/receipt-reader.json');
    // A description: build mode reads a later bare sentence as chat, so /make says it is a build.
    expect(rerunCommand(modelOf(BEFORE_PLAN))).toBe('/make Pull the invoice number, date and total out of invoice text');
  });

  it('says the last problem in one sentence, and what can be done about it', () => {
    expect(noticeLine(modelOf(ERR_BUILD))).toEqual({ level: 'err', text: 'The relay refused the call (too many requests).', action: 'retry' });
    const key = [...PLAN, { type: 'event', event: { kind: 'notice', level: 'error', text: 'The judge code was not accepted (401). Paste it again in Settings.' } }];
    expect(noticeLine(modelOf(key))).toMatchObject({ level: 'err', action: 'settings' });
    const warn = [...PLAN, { type: 'event', event: { kind: 'notice', level: 'warn', text: 'per-run budget is tight for the bar' } }];
    expect(noticeLine(modelOf(warn))).toEqual({ level: 'warn', text: 'Per-run budget is tight for the pass mark', action: null });
    expect(noticeLine(modelOf(PLAN))).toBeNull();
  });

  it("derives the card state from the phase, the conversation and the export's answer", () => {
    const state = (timeline: ReturnType<typeof folded>, status: 'ready' | 'starting' | 'failed' = 'ready') => cardState(forgeModel(timeline), { status, timeline });
    expect(state(folded(PLAN))).toBe('plan');
    expect(state(folded(PLAN), 'starting')).toBe('starting');
    expect(state(folded(PLAN, FORGING))).toBe('building');
    expect(state(folded(STOPPED_BUILD))).toBe('stopped');
    expect(state(folded(PLAN, FORGING), 'failed')).toBe('stopped');
    expect(state(folded(PLAN, FORGING, SHIPPED))).toBe('licensed');
    // A finished build keeps its result when the conversation dies.
    expect(state(folded(PLAN, FORGING, SHIPPED), 'failed')).toBe('licensed');
    expect(state(folded(PLAN, FORGING, SHIPPED, EXPORTING))).toBe('exporting');
    expect(state(folded(PLAN, FORGING, SHIPPED, EXPORTING, EXPORTED.slice(0, 2)))).toBe('adding');
    expect(state(folded(PLAN, FORGING, SHIPPED, EXPORTING, [{ type: 'ask_closed', id: 3, answer: 'no' }, { type: 'turn_finished', text: '' }]))).toBe('result');
    expect(state(folded(EXPORTED_BUILD))).toBe('exported');
  });
});

describe('stepWords', () => {
  it('says what to do next without the terminal command that does it', async () => {
    const { stepWords } = await import('../src/lib/vocab');
    expect(stepWords('raise the budget with /budget, then build again')).toBe('Raise the budget, then build again.');
    expect(stepWords('raise the compile budget: /budget compile 1.20 2 (it ran out before Stage 4 finished)')).toBe('Raise the compile budget.');
    expect(stepWords('choose a configured model: /model fake')).toBe('Choose a configured model.');
    expect(stepWords('describe the inputs and outputs field by field and try /make again')).toBe('Describe the inputs and outputs field by field and try again.');
  });
});

describe('a refused retake in plain words', () => {
  it('says why without the code', () => {
    expect(reasonWords('undo_only')).toBe('this change can only be undone');
    expect(reasonWords('exam_no_retakes')).toBe('this exam takes no retakes');
    expect(reasonWords('no_copy')).toBe('this computer has no copy of the version that passed');
    // Words a newcomer reads without knowing what a profile, a preset or a plan file is.
    expect(reasonWords('edit_invalid')).toBe('the edited file has a mistake to fix first');
    expect(reasonWords('model_not_in_profile')).toBe("the model is not in Architect's settings here");
    expect(reasonWords('not_a_preset_agent')).toBe('Forge wrote this agent, and its exam cannot be retaken');
    expect(reasonWords('exam_changed')).toBe("the exam's cases are not the ones this agent passed on");
    expect(reasonWords('not_placed')).toBe('not added to a project');
  });
});
