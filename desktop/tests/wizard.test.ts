/**
 * The guided build's foundation (clarity spec §0, §6 S0): the step from the build's state,
 * the words with numbers in them, and the demo build's real numbers (tests/fixtures/demoBuild.ts).
 */
import { describe, expect, it } from 'vitest';
import { emptyFeed, feedMessage, forgeModel, ladderView, type ForgeModel } from '../src/lib/forge';
import { emptyTimeline } from '../src/lib/timeline';
import { exportRows } from '../src/lib/vocab';
import {
  approvalDiff, blockedLine, buildCost, CHECKS_INVALID, checksInvalid, previewChanges, saidSteps, exportPending, buildLimits, buildWork, caseNoun, casePlural, checkRules, connectPrompt, connectState, datasetOf,
  exportRefusal, exportSize, filePurpose, firstRule, flagLine, fmtDuration, givesSentence, joinAnd, ladderTag, listPrice, minutesLeft,
  noPassLines, outputFields, passNeed, priceRatio, projectName, projectPath, ruleParts, rungSeconds, schemaFields, schemaNote, shortBy,
  stepItems, takesSentence, taskRules, testsCommand, whyNotLine, wizardStep, type WizardView,
} from '../src/lib/wizard';
import { useApp, type Conv } from '../src/store/app';
import { describeSub } from '../src/components/forge/wizard/DescribeScreen';
import { BEFORE_PLAN } from './fixtures/forgeBuild';
import { ERR_BUILD, PLAN as OLD_PLAN, STOPPED_BUILD } from './fixtures/ladderBuild';
import {
  DEMO_BUILDING, DEMO_BUILDING_NOW, DEMO_CONNECT_APPROVE, DEMO_CONNECTED, DEMO_CWD, DEMO_DETAIL, DEMO_EXPORT_CONFIRM, DEMO_FILES,
  DEMO_FILES_WRITTEN, DEMO_NO_PASS, DEMO_PASSED, DEMO_PLAN, CONNECT_MSGS, EXPORT_MSGS, demoConv, type Msg,
} from './fixtures/demoBuild';

const ev = (event: Record<string, unknown>) => ({ type: 'event', event });

function at(msgs: Msg[], patch: Partial<Conv> = {}): { conv: Conv; model: ForgeModel } {
  const conv = demoConv(msgs, patch);
  return { conv, model: forgeModel(conv.timeline) };
}

function stepOf(msgs: Msg[], view?: WizardView, opts: { skipped?: boolean; patch?: Partial<Conv> } = {}) {
  const { conv, model } = at(msgs, opts.patch);
  return wizardStep(model, conv, view, { skipped: opts.skipped });
}

const NOW_CONNECTING = [...DEMO_FILES_WRITTEN, ...CONNECT_MSGS.slice(0, CONNECT_MSGS.findIndex((m) => m.type === 'ask'))];

describe('wizardStep, every row of the step table', () => {
  it('step 1: an empty build is Describe', () => {
    const conv = demoConv([]);
    expect(wizardStep(forgeModel(conv.timeline), conv)).toEqual({ step: 1, sub: 'describe' });
  });

  it('step 2: reading, the question, planning, the plan, set aside, blocked and stopped before the plan', () => {
    expect(stepOf([{ type: 'turn_started', text: 'Read a receipt', at: 1 }])).toEqual({ step: 2, sub: 'reading' });
    expect(stepOf(BEFORE_PLAN.slice(0, 5))).toEqual({ step: 2, sub: 'question' });
    expect(stepOf([...BEFORE_PLAN.slice(0, 7)])).toEqual({ step: 2, sub: 'planning' });
    expect(stepOf(DEMO_PLAN)).toEqual({ step: 2, sub: 'plan' });
    const setAside = [...DEMO_PLAN, { type: 'ask_closed', id: 3, answer: 'no', at: 1791364790 }, { type: 'turn_finished', text: '', at: 1791364790.1 }];
    expect(stepOf(setAside)).toEqual({ step: 2, sub: 'set_aside' });
    const blocked = [
      { type: 'turn_started', text: '/make x', at: 1 },
      ev({ kind: 'plan_card', title: 'Plan (blocked)', rows: [['task', 'x']], footer: 'blocked: model_unpriced', data: {} }),
      { type: 'turn_finished', text: '', at: 2 },
    ];
    expect(stepOf(blocked)).toEqual({ step: 2, sub: 'blocked' });
    expect(stepOf([...BEFORE_PLAN.slice(0, 7), { type: 'turn_finished', text: '', at: 3 }])).toEqual({ step: 2, sub: 'stopped_plan' });
  });

  it('step 3: building, and a build that stopped or was turned away after a model started', () => {
    expect(stepOf(DEMO_BUILDING)).toEqual({ step: 3, sub: 'building' });
    expect(stepOf(STOPPED_BUILD)).toEqual({ step: 3, sub: 'stopped_build' });
    expect(stepOf(ERR_BUILD)).toEqual({ step: 3, sub: 'stopped_build' });
  });

  it('step 4: passed, did not pass and not scored; a build that did not pass stays on Results', () => {
    expect(stepOf(DEMO_PASSED)).toEqual({ step: 4, sub: 'passed' });
    expect(stepOf(DEMO_NO_PASS)).toEqual({ step: 4, sub: 'no_pass' });
    expect(stepOf(DEMO_NO_PASS, 'try')).toEqual({ step: 4, sub: 'no_pass' });
    const unscored = DEMO_NO_PASS.map((m) => {
      const e = m.event as Record<string, unknown> | undefined;
      if (e?.kind !== 'plan_card' || !String(e.title).startsWith('Result')) return m;
      const data = { ...(e.data as Record<string, unknown>), exam: { cases: 100, correct: null, invalid: 40, over_budget: 0 } };
      return { ...m, event: { ...e, rows: [['floor', '0.00/0.95 (not scored · invalid 40 · over budget 0) (primary_invalid_exceeded)']], data } };
    });
    expect(stepOf(unscored)).toEqual({ step: 4, sub: 'unscored' });
  });

  it('steps 5 and 6 follow the view once a model passed', () => {
    expect(stepOf(DEMO_PASSED, 'try')).toEqual({ step: 5, sub: 'try' });
    expect(stepOf(DEMO_PASSED, 'add')).toEqual({ step: 6, sub: 'add_ready' });
    expect(stepOf([...DEMO_PASSED, EXPORT_MSGS[0]], 'add')).toEqual({ step: 6, sub: 'add_preparing' });
  });

  it('step 6: the export approval, writing, refused, then the connect step', () => {
    expect(stepOf(DEMO_EXPORT_CONFIRM)).toEqual({ step: 6, sub: 'add_confirm' });
    expect(stepOf(DEMO_EXPORT_CONFIRM, 'results')).toEqual({ step: 4, sub: 'passed' });
    expect(stepOf([...DEMO_EXPORT_CONFIRM, { type: 'answered', id: 4, value: 'yes' }])).toEqual({ step: 6, sub: 'add_writing' });
    const refused = [
      ...DEMO_PASSED,
      { type: 'turn_started', text: '/export receipt-reader', quiet: false, at: 1791365950.5 },
      ev({ kind: 'notice', level: 'error', text: 'Not exported (export_target_exists: agents/receipt_reader).' }),
      { type: 'turn_finished', text: '/export receipt-reader', at: 1791365950.7 },
    ];
    expect(stepOf(refused, 'add')).toEqual({ step: 6, sub: 'add_refused' });
    const { conv, model } = at(refused);
    expect(exportRefusal(model, conv.timeline.items)?.code).toBe('export_target_exists');

    expect(stepOf(DEMO_FILES_WRITTEN)).toEqual({ step: 6, sub: 'files_written' });
    expect(stepOf(DEMO_FILES_WRITTEN, undefined, { skipped: true })).toEqual({ step: 6, sub: 'connected' });
    expect(stepOf(NOW_CONNECTING)).toEqual({ step: 6, sub: 'connecting' });
    expect(stepOf(DEMO_CONNECT_APPROVE)).toEqual({ step: 6, sub: 'connect_approve' });
    expect(stepOf(DEMO_CONNECTED)).toEqual({ step: 6, sub: 'connected' });
    const question = [...NOW_CONNECTING, { type: 'ask', ask: { id: 9, kind: 'question', question: 'Which file does the job today?', default: null, choices: [] } }];
    expect(stepOf(question)).toEqual({ step: 6, sub: 'connect_question' });
    const failed = [...NOW_CONNECTING, ev({ kind: 'notice', level: 'error', text: 'The relay refused the call (too many requests).' }), { type: 'turn_finished', text: '', quiet: true, at: 1791473270 }];
    expect(stepOf(failed)).toEqual({ step: 6, sub: 'connect_failed' });
  });

  it('Starting keeps the step the build is on', () => {
    expect(stepOf(DEMO_PLAN, undefined, { patch: { status: 'starting' } })).toEqual({ step: 2, sub: 'plan' });
  });
});

describe('the stepper', () => {
  it('marks the steps before as done and the current one, with Results a button once there is a result', () => {
    const { conv, model } = at(DEMO_PASSED);
    const items = stepItems(wizardStep(model, conv), model);
    expect(items.map((i) => i.mark)).toEqual(['done', 'done', 'done', 'current', 'todo', 'todo']);
    expect(items.map((i) => i.view)).toEqual([undefined, undefined, undefined, 'results', 'try', 'add']);
    expect(items[3].current).toBe(true);
  });

  it('a build that did not pass: Results failed, Try it and Add locked and not buttons', () => {
    const { conv, model } = at(DEMO_NO_PASS);
    const items = stepItems(wizardStep(model, conv), model);
    expect(items.map((i) => i.mark)).toEqual(['done', 'done', 'done', 'failed', 'locked', 'locked']);
    expect(items[3].current).toBe(true);
    expect(items[4].view).toBeUndefined();
  });

  it('steps 1 to 3 are never buttons', () => {
    const { conv, model } = at(DEMO_BUILDING);
    expect(stepItems(wizardStep(model, conv), model).map((i) => i.view)).toEqual([undefined, undefined, undefined, undefined, undefined, undefined]);
  });
});

describe('the job in words, from the plan', () => {
  const { model } = at(DEMO_PLAN);

  it('names a case by the single string input: receipt, else case', () => {
    expect(caseNoun(model)).toBe('receipt');
    expect(casePlural('receipt', 100)).toBe('receipts');
    expect(casePlural('receipt', 1)).toBe('receipt');
    const old = forgeModel(demoConv(OLD_PLAN).timeline);
    expect(caseNoun(old)).toBe('case');
    const two = { ...model, plan: { ...model.plan!, data: { ...model.plan!.data, inputs: [{ name: 'a', type: 'string', nullable: false }, { name: 'b', type: 'string', nullable: false }] } } };
    expect(caseNoun(two)).toBe('case');
    expect(caseNoun(old, DEMO_DETAIL.spec.input_schema)).toBe('receipt');
  });

  it('says what goes in and comes back, in schema order', () => {
    expect(takesSentence(model)).toBe('The text of one receipt');
    expect(givesSentence(model)).toBe('Total, subtotal, tax and item count');
    expect(schemaNote(outputFields(model), 'receipt')).toBe('Amounts as whole numbers. A field the receipt does not print comes back empty (null).');
    expect(schemaNote([{ name: 'label', type: 'string', nullable: false }], 'case')).toBeUndefined();
    expect(schemaFields(DEMO_DETAIL.spec.output_schema).map((f) => f.name)).toEqual(['total', 'subtotal', 'tax', 'item_count']);
    expect(joinAnd(['a'])).toBe('a');
  });

  it("lists the task's seven rules and the two code checks", () => {
    const rules = taskRules(model);
    expect(rules).toHaveLength(7);
    expect(ruleParts(rules[0]).field).toBe('total');
    expect(firstRule(model)).toBe('total: the total amount printed on the receipt, including any tax and service, on a line such as TOTAL, GRAND TOTAL, TL or PAID');
    expect(checkRules(model)).toEqual([
      'Amounts keep every printed digit, so "226,500.00" is 22650000.',
      'A subtotal equal to the total is null unless the receipt prints that amount as a subtotal.',
    ]);
  });

  it('the cases, the pass mark, the limits and list prices', () => {
    expect(datasetOf(model)).toEqual({ name: 'CORD v2', public: true });
    expect(passNeed(model)).toBe(40);
    expect(buildLimits(model)).toEqual({ usd: 0.6, seconds: 3600, minutes: 60 });
    expect(listPrice(model, 'nvidia/Nemotron-3_5-Lightning')).toBe('$0.06 in · $0.24 out');
    expect(listPrice(model, 'nvidia/Nemotron-3-Ultra-550b-a55b')).toBe('$1.00 in · $3.00 out');
    expect([ladderTag(0, 3), ladderTag(1, 3), ladderTag(2, 3), ladderTag(0, 1)]).toEqual(['cheapest', undefined, 'most expensive', undefined]);
    expect(blockedLine('blocked: model_unpriced')).toMatch(/no known price/);
    expect(projectName(DEMO_CWD)).toBe('expense-app');
  });
});

describe('the build, with the bridge times (B3) and costs (B1)', () => {
  it('times each model from its first stage to the next one, the last to the end of the build', () => {
    const conv = demoConv(DEMO_PASSED);
    const ladder = ladderView(forgeModel(conv.timeline), conv.forge)!;
    expect(ladder.rungs.map((r) => fmtDuration(rungSeconds(r)!))).toEqual(['12m 28s', '3m 13s', '3m 12s']);
    expect(ladder.rungs.map((r) => r.cost)).toEqual([5.83056e-05, 0.0002782949999999999, 0.0009273200000000001]);
    expect(ladder.rungs[2].cost).toBeCloseTo(0.000927, 6);
  });

  it('at 15:02: Lightning done, Super running for 2m 34s, Ultra waiting, 45 min left at most', () => {
    const conv = demoConv(DEMO_BUILDING);
    const ladder = ladderView(forgeModel(conv.timeline), conv.forge)!;
    expect(ladder.rungs.map((r) => r.state)).toEqual(['done', 'running', 'pending']);
    expect(fmtDuration(rungSeconds(ladder.rungs[0])!)).toBe('12m 28s');
    expect(ladder.rungs[1].endedAt).toBeUndefined();
    expect(fmtDuration(rungSeconds(ladder.rungs[1], DEMO_BUILDING_NOW)!)).toBe('2m 34s');
    expect(rungSeconds(ladder.rungs[2])).toBeUndefined();
    const work = buildWork(conv.timeline.items)!;
    expect(fmtDuration((DEMO_BUILDING_NOW - work.started) / 1000)).toBe('15m 2s');
    expect(minutesLeft(3600, DEMO_BUILDING_NOW - work.started)).toBe(45);
    expect(minutesLeft(60, 120_000)).toBe(0);
  });

  it("the build's clock runs from the Build press to the result: 18m 54s, $0.31", () => {
    const { conv, model } = at(DEMO_PASSED);
    const work = buildWork(conv.timeline.items)!;
    expect(fmtDuration((work.ended! - work.started) / 1000)).toBe('18m 54s');
    expect(buildCost(model)).toBe('$0.31');
  });

  it('a replay from before B3 has no times', () => {
    const conv = demoConv(STOPPED_BUILD);
    const ladder = ladderView(forgeModel(conv.timeline), conv.forge)!;
    expect(ladder.rungs.every((r) => r.startedAt === undefined && r.cost === undefined)).toBe(true);
  });

  it('keeps a forge message\'s `at` beside its data, and the end of the build\'s turn once', () => {
    let feed = feedMessage(emptyFeed(), { type: 'forge', at: 10, data: { kind: 'stage', stage: 1, depth: 0, model_index: 1 } });
    expect(feed.stages[0].at).toBe(10);
    feed = feedMessage(feed, { type: 'turn_finished', at: 20 });
    expect(feed.endedAt).toBe(20_000);
    expect(feedMessage(feed, { type: 'turn_finished', at: 30 }).endedAt).toBe(20_000);
    expect(feedMessage(emptyFeed(), { type: 'turn_finished', at: 30 }).endedAt).toBeUndefined();
  });
});

describe('results', () => {
  it('passed: 39, 64 and 72 right, Lightning 1 short, and why not Ultra', () => {
    const conv = demoConv(DEMO_PASSED);
    const ladder = ladderView(forgeModel(conv.timeline), conv.forge)!;
    expect(ladder.rungs.map((r) => r.exam?.counts?.correct)).toEqual([39, 64, 72]);
    expect(ladder.rungs.map((r) => shortBy(r, 40))).toEqual([1, undefined, undefined]);
    expect(whyNotLine(ladder)).toBe(
      'Why not Ultra? It passed too, but Super passed for less. If you need more than 64 right, raise the pass mark to between 65 and 72 with Edit plan and build again: then Super no longer passes and Ultra becomes the cheapest that does.',
    );
    // A plan file keeps its own pass mark over a saved change, so the line points at the file.
    expect(whyNotLine(ladder, 'receipt-reader.json')).toContain('raise the pass mark in receipt-reader.json to between 65 and 72 and build again:');
    expect(whyNotLine(ladder, 'receipt-reader.json')).not.toContain('Edit plan');
    expect(priceRatio(ladder.rungs[2].cost!, ladder.rungs[1].cost!)).toBe('more than 3 times');
    expect(priceRatio(3, 1)).toBe('3 times');
    expect(priceRatio(1, 1)).toBeUndefined();
  });

  it('no why-not line when no model that passed scored higher', () => {
    const conv = demoConv(DEMO_PASSED);
    const ladder = ladderView(forgeModel(conv.timeline), conv.forge)!;
    const alone = { ...ladder, rungs: ladder.rungs.map((r, i) => (i === 2 ? { ...r, licensed: false } : r)) };
    expect(whyNotLine(alone)).toBeUndefined();
    const noPass = demoConv(DEMO_NO_PASS);
    expect(whyNotLine(ladderView(forgeModel(noPass.timeline), noPass.forge)!)).toBeUndefined();
  });

  it('nothing passed at 95: each model short, and what pass mark would let one pass', () => {
    const { conv, model } = at(DEMO_NO_PASS);
    const ladder = ladderView(model, conv.forge)!;
    expect(passNeed(model)).toBe(95);
    expect(ladder.rungs.map((r) => shortBy(r, 95))).toEqual([56, 31, 23]);
    expect(noPassLines(model, ladder)).toEqual([
      'If 72 of 100 is good enough for this job, lower the pass mark to 72 or below and Ultra would pass.',
      'If 64 is enough, Super would pass and cost less to run.',
    ]);
  });
});

describe('add to project', () => {
  it('the export: 13 files in one new folder, 22 KB, each with what it is for', () => {
    const { model } = at(DEMO_EXPORT_CONFIRM);
    expect(exportRows(model).files).toEqual(DEMO_FILES);
    expect(exportRows(model).folder).toBe('agents/receipt_reader/');
    expect(exportSize(model)).toBe('13 files, 22 KB');
    expect(filePurpose('README.md')).toBe('How to call it');
    expect(filePurpose('artifact/harness.py')).toBe('The agent itself');
    expect(filePurpose('tests/test_receipt_reader.py')).toBe('Its own tests');
    expect(filePurpose('other.txt')).toBeUndefined();
  });

  it('written: the flag line, the export\'s own test command and the connect prompt', () => {
    const { model } = at(DEMO_FILES_WRITTEN);
    expect(flagLine(model)).toBe('ARCHITECT_FEATURE_4D80F3D5=1');
    expect(testsCommand(model)).toBe('uv run pytest agents/receipt_reader/tests -p no:cacheprovider');
    expect(connectPrompt(model)).toBe(
      "Connect the agent in agents/receipt_reader/ to this project's code. Read agents/receipt_reader/README.md and the code that does this job today, then change that call so it uses the agent's run(). Keep the feature flag off and do not run the tests; I will do both.",
    );
  });

  it('the edit approval: its path in the project, its line counts and the function it changes', () => {
    const { conv } = at(DEMO_CONNECT_APPROVE);
    const c = connectState(conv.timeline.items);
    expect(c.started).toBe(true);
    expect(c.open?.action).toBe('edit');
    const diff = approvalDiff(c.open?.detail)!;
    expect(projectPath(diff.path, DEMO_CWD)).toBe('app/extract.py');
    expect(diff.meta).toBe('2 edits');
    expect(diff.fn).toBe('extract');
    expect(diff.added).toBeGreaterThan(0);
    expect(c.said).toMatch(/^Now I'll make the requested modifications/);
  });

  it('connected: one edit allowed, and what the coder reports it changed (+15 -3)', () => {
    const { conv } = at(DEMO_CONNECTED);
    const c = connectState(conv.timeline.items);
    expect(c.allowed).toBe(1);
    expect(c.applied).toEqual([{ action: 'edit', path: 'C:/rec/expense-app/app/extract.py', added: 15, removed: 3 }]);
    expect(c.open).toBeUndefined();
    expect(conv.timeline.running).toBe(false);
  });

  it('nothing after the Exported card is no connect', () => {
    expect(connectState(emptyTimeline().items)).toMatchObject({ started: false, allowed: 0 });
  });
});

describe('the store', () => {
  it('keeps the step a build shows per conversation, and the Skip flag', () => {
    useApp.getState().setWizardView('c1', 'try');
    expect(useApp.getState().wizardView).toEqual({ c1: 'try' });
    useApp.getState().setWizardView('c1', null);
    expect(useApp.getState().wizardView).toEqual({});
    useApp.getState().setWizardSkipped('c1', true);
    expect(useApp.getState().wizardSkipped).toEqual({ c1: true });
    useApp.getState().setWizardSkipped('c1', false);
    expect(useApp.getState().wizardSkipped).toEqual({});
  });
});

it('formats durations', () => {
  expect(fmtDuration(45)).toBe('45s');
  expect(fmtDuration(748.171)).toBe('12m 28s');
  expect(fmtDuration(3725)).toBe('1h 2m');
});

describe('the clarity fixes', () => {
  it('calls step 6 Preparing only while an /export waits for its answer, not for any running turn', () => {
    const asking: Msg[] = [...DEMO_PASSED, { type: 'turn_started', text: 'Why did Lightning miss?', quiet: false, at: 1791366000 }];
    expect(stepOf(asking, 'add')).toEqual({ step: 6, sub: 'add_ready' });
    expect(stepOf([...DEMO_PASSED, EXPORT_MSGS[0]], 'add')).toEqual({ step: 6, sub: 'add_preparing' });
    const { conv } = at(DEMO_EXPORT_CONFIRM);
    expect(exportPending(conv.timeline.items)).toBe(false);
  });

  it('calls step 6 Connecting only once the coder took the job, not while the export turn ends', () => {
    // The export's own turn still running after the Exported card (the tape's order) is not the coder reading code.
    const ending = DEMO_FILES_WRITTEN.filter((m) => m.type !== 'turn_finished' || m.text !== '/export receipt-reader');
    expect(at(ending).conv.timeline.running).toBe(true);
    expect(stepOf(ending)).toEqual({ step: 6, sub: 'files_written' });
    expect(stepOf(NOW_CONNECTING)).toEqual({ step: 6, sub: 'connecting' });
  });

  it('never shows the invalid-checks sentence as a rule', () => {
    const invalid = DEMO_PLAN.map((m) => {
      const e = m.event as Record<string, unknown> | undefined;
      if (e?.kind !== 'plan_card') return m;
      return { ...m, event: { ...e, data: { ...(e.data as Record<string, unknown>), checks: [CHECKS_INVALID] } } };
    });
    const { model } = at(invalid);
    expect(checkRules(model)).toEqual([]);
    expect(checksInvalid(model)).toBe(true);
    expect(checksInvalid(at(DEMO_PLAN).model)).toBe(false);
  });

  it('names no model family or count before the profile list arrives', () => {
    expect(describeSub(undefined)).toBe('Describe one small task. Architect tests several models on example cases of that task and gives you the cheapest one that does it well.');
    expect(describeSub([])).toContain('tests several models');
    expect(describeSub(['nvidia/Nemotron-3_5-Lightning', 'nvidia/nemotron-3-super-120b-a12b', 'nvidia/Nemotron-3-Ultra-550b-a55b'])).toContain('tests three NVIDIA Nemotron models');
  });

  it('reads the edit preview as changes: kept lines as context, and nothing past a cut called removed', () => {
    const { conv } = at(DEMO_CONNECT_APPROVE);
    const preview = previewChanges(connectState(conv.timeline.items).open?.detail)!;
    expect(preview.changes).toHaveLength(2);
    expect(preview.cut).toBe(true);
    const [imports, body] = preview.changes;
    expect(imports.cut).toBe(true);
    expect(imports.lines.filter((l) => l.kind === 'add').map((l) => l.text)).toEqual(['import asyncio', 'from pathlib import Path']);
    expect(imports.lines.filter((l) => l.kind === 'del')).toEqual([]);
    expect(imports.lines.filter((l) => l.kind === 'unseen').map((l) => l.text)).toEqual(['from pydantic import BaseModel, Field, ValidationError', '']);
    // The new body is exactly at the preview's limit: it may go on, but nothing is hidden.
    expect(body.cut).toBe(false);
    expect(body.mayGoOn).toBe(true);
    expect(body.lines[0]).toEqual({ kind: 'ctx', text: 'def extract(text: str) -> Receipt:' });
    expect(body.lines.filter((l) => l.kind === 'del').map((l) => l.text.trim())).toEqual([
      `"""Read one receipt. Architect's receipt agent will replace this call."""`, 'return ask_general_model(text)',
    ]);
    expect(body.added).toBe(11);
    // A whole preview: plain counts.
    const small = previewChanges('a.py (1 edit)\n- x = 1\n- y = 2\n+ x = 1\n+ y = 3')!;
    expect(small.cut).toBe(false);
    expect(small.changes[0]).toMatchObject({ added: 1, removed: 1, cut: false, mayGoOn: false });
    // A write's "... N more lines" marks it cut.
    expect(previewChanges('b.py (40 bytes, 20 lines)\n+ a\n+ ... 19 more lines')!.cut).toBe(true);
    expect(previewChanges('not a preview')).toBeNull();
  });

  it("lists the coder's numbered steps as steps, else its first sentences", () => {
    expect(saidSteps("Now I'll make the requested modifications to app/extract.py: 1. Add the imports 2. Change EXTRACTOR 3. Replace the body")).toEqual({
      lead: "Now I'll make the requested modifications to app/extract.py",
      steps: ['Add the imports', 'Change EXTRACTOR', 'Replace the body'],
    });
    expect(saidSteps('I read the file. It has one extractor. Then more.')).toEqual({ lead: 'I read the file. It has one extractor.', steps: [] });
    expect(saidSteps(undefined)).toEqual({ steps: [] });
  });
});
