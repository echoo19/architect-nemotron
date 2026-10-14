/**
 * Step 2, Plan (clarity spec §3, mockups 2-plan and 2b-plan-rules): everything between
 * the job leaving step 1 and the Build press.
 *
 *   reading, planning  Architect is reading the job and writing the plan: Stop, Add a detail
 *   question           the interview's question, answered from the bar
 *   plan               the plan to review: what the agent does, how Architect picks the
 *                      model; Back answers it no, Build agent answers it yes
 *   set_aside          the person said no: the same cards, Build this plan or Describe again
 *   blocked            the plan cannot run (a model has no price): Change settings
 *   stopped_plan       the turn ended before a plan: Try again
 *
 * Every number is the plan card's (its data, B2): the counts, the pass mark, the list
 * prices, the build's limits. A value the plan does not carry leaves its words out.
 */
import { IconArrowRight, IconFile, IconFileDescription, IconPencil, IconSquare } from '@tabler/icons-react';
import { useState, type ReactNode } from 'react';
import { friendlyLine, stageLine, type ForgeModel } from '../../../lib/forge';
import { prettyModel, rungName } from '../../../lib/models';
import { noticeLine, rerunCommand } from '../../../lib/vocab';
import { fmtUsd } from '../../../lib/viz';
import {
  buildLimits, caseCounts, caseNoun, casePlural, checkRules, checksInvalid, datasetOf, firstRule, givesSentence, ladderIds, listPrice,
  outputFields, passNeed, ruleParts, schemaNote, takesSentence, taskRules, blockedLine,
} from '../../../lib/wizard';
import type { Ask } from '../../../lib/timeline';
import { errorText, useApp, type Conv } from '../../../store/app';
import { Button } from '../../ui/Button';
import { Card, CardBody, CardHeader } from '../../ui/Card';
import { Working } from '../../ui/Working';
import { frozenFile, useBuildWithChanges, useChangedPlan } from '../PlanSheet';
import { Question, questionSubmits } from './Question';
import { RulesSheet } from './RulesSheet';
import type { ScreenProps } from './types';
import { WizardPage, type WizardPageProps } from './WizardPage';
import '../../../styles/wizard-plan.css';

// ---------------------------------------------------------------- words

const COUNT_WORDS = ['no', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten'];

/** A small count as a word ("three"), a larger one as digits. */
export function countWord(n: number, capital = false): string {
  const word = COUNT_WORDS[n] ?? String(n);
  return capital ? word.charAt(0).toUpperCase() + word.slice(1) : word;
}

/** "Nemotron" when every model the plan tries is one, else nothing. */
export function modelFamily(ids: string[]): string | undefined {
  return ids.length && ids.every((id) => /nemotron/i.test(id)) ? 'Nemotron' : undefined;
}

/** The first rule for "For example": "total: the total amount ..." reads "total, the total amount ...". */
function exampleRule(model: ForgeModel): string | undefined {
  const rule = firstRule(model);
  if (!rule) return undefined;
  const { field, text } = ruleParts(rule);
  return field ? `${field.replace(/_/g, ' ')}, ${text}` : rule;
}

/**
 * The pass-mark step's words (C3): what the mark asks for, where it came from, and how it
 * changes. A frozen plan is built as its file says: build mode ignores a pass mark saved
 * in Edit plan (FROZEN_IGNORED), so the words send the person to the file, not the button.
 */
export function passSource(model: ForgeModel): string {
  const frozen = frozenFile(model);
  const need = passNeed(model);
  const { exam } = caseCounts(model);
  const noun = caseNoun(model);
  const asks = need !== undefined && exam !== undefined ? `At least ${need} of the ${exam} exam ${casePlural(noun, exam)} must be fully right` : '';
  const strict = outputFields(model).length > 1 ? `one wrong field makes a ${noun} wrong` : '';
  const lead = asks ? `${asks}${strict ? `, and ${strict}` : ''}. ` : strict ? `${strict.charAt(0).toUpperCase()}${strict.slice(1)}. ` : '';
  return frozen
    ? `${lead}Set in ${frozen}, the plan file you opened; change it there.`
    : `${lead}Architect's suggestion for this job. Change it with Edit plan.`;
}

/** "Code also checks every answer on two points, for example: ..." with the plan's own first check. */
function checksLine(checks: string[]): string | undefined {
  if (!checks.length) return undefined;
  const first = checks[0].trim().replace(/\.$/, '');
  return checks.length === 1
    ? `Code also checks every answer: ${first}.`
    : `Code also checks every answer on ${countWord(checks.length)} points, for example: ${first}.`;
}

/** What a model tries in practice: the tuned preset's setups, or Forge's own instructions. */
function practiceWords(model: ForgeModel): string {
  return model.plan?.data?.engineer === 'forge'
    ? 'Each model writes and tries its own instructions and keeps its best.'
    : 'Each model tries a few setups, such as rechecking its answer, and keeps its best.';
}

// ---------------------------------------------------------------- shared

/** An ask counts as answered at once: its controls go quiet before the bridge closes it. */
function answered(conv: Conv, ask: Ask | undefined): boolean {
  return !!ask && conv.timeline.items.some((i) => i.kind === 'ask' && i.ask.id === ask.id && i.answer !== undefined);
}

/** The status line while the conversation starts: the Working mark and its word. */
const STARTING = <><Working size="xs" />Starting</>;

/** The error or warning under the sub, with its action (shared state, clarity spec §2.2). */
function useNotice(conv: Conv, model: ForgeModel): Pick<WizardPageProps, 'attention' | 'warning' | 'attentionAction'> {
  const send = useApp((s) => s.send);
  const setScreen = useApp((s) => s.setScreen);
  const issue = noticeLine(model);
  if (!issue) return {};
  if (issue.level === 'warn') return { warning: issue.text };
  const action = issue.action === 'settings'
    ? <Button variant="link" size="sm" data-action="settings" onClick={() => setScreen('settings', 'models')}>Open settings</Button>
    : issue.action === 'retry'
      ? <Button variant="link" size="sm" data-action="retry" disabled={conv.status !== 'ready' || conv.timeline.running} onClick={() => void send(conv.id, rerunCommand(model))}>Retry</Button>
      : undefined;
  return { attention: issue.text, attentionAction: action };
}

const next = (label: string) => <>{label}<IconArrowRight size={16} stroke={1.5} aria-hidden /></>;

// ---------------------------------------------------------------- the screen

export function PlanScreen(props: ScreenProps) {
  switch (props.sub) {
    case 'question':
      return <QuestionPage {...props} />;
    case 'reading':
    case 'planning':
      return <PlanningPage {...props} />;
    case 'stopped_plan':
      return <StoppedPage {...props} />;
    default:
      return <ReviewPage {...props} />;
  }
}

function QuestionPage({ conv, model }: ScreenProps) {
  const answer = useApp((s) => s.answer);
  const notice = useNotice(conv, model);
  const ask = model.question;
  const ready = conv.status === 'ready';
  const disabled = !ready || answered(conv, ask);
  const external = !!ask && questionSubmits(ask);
  return (
    <WizardPage
      status={ready ? undefined : STARTING}
      title="One question before the plan"
      sub="Architect needs this to write the plan. Nothing runs yet."
      {...notice}
      bar={{
        back: { label: 'Back', run: () => undefined, disabled: true },
        ask: false,
        primary: external ? (
          <Button type="submit" form="wizard-question" variant="primary" size="md" data-action="answer" disabled={disabled}>Answer</Button>
        ) : undefined,
      }}
    >
      {ask && (
        <Card>
          <CardBody>
            <Question key={ask.id} ask={ask} formId="wizard-question" externalSubmit={external} disabled={disabled} onAnswer={(value) => void answer(conv.id, ask.id, value)} />
          </CardBody>
        </Card>
      )}
    </WizardPage>
  );
}

function PlanningPage({ conv, model, sub }: ScreenProps) {
  const cancel = useApp((s) => s.cancel);
  const notice = useNotice(conv, model);
  const ready = conv.status === 'ready';
  const reading = sub === 'reading';
  // The latest thing a subagent said (the case drafter's dataset line), in plain words.
  const said = [...model.lines].reverse().find((l) => l.text.trim())?.text;
  return (
    <WizardPage
      status={!ready ? STARTING : reading ? <><Working size="xs" />Reading the job</> : undefined}
      title="Writing the plan"
      sub="Architect is turning your sentence into a plan and choosing the practice and exam data. Nothing is built yet."
      {...notice}
      bar={{
        secondary: [
          <Button key="stop" variant="outline" size="md" data-action="stop" disabled={!ready || !conv.timeline.running} onClick={() => void cancel(conv.id)}>
            <IconSquare size={14} stroke={1.5} aria-hidden />Stop
          </Button>,
        ],
        ask: { placeholder: 'Add a detail', disabled: !ready },
        ownStop: true,
      }}
    >
      <Card>
        <CardBody>
          <div className="plan-wait" role="status">
            <p className="plan-wait-line"><Working size="sm" />{reading ? 'Reading the job' : stageLine(0)}</p>
            {!reading && said && <p className="plan-wait-detail">{friendlyLine(said)}</p>}
          </div>
        </CardBody>
      </Card>
    </WizardPage>
  );
}

function StoppedPage({ conv, model }: ScreenProps) {
  const send = useApp((s) => s.send);
  const restart = useApp((s) => s.restart);
  const issue = noticeLine(model);
  const dead = conv.status === 'stopped' || conv.status === 'failed';
  const sub = dead ? errorText(conv.error ?? 'stopped') : issue?.text ?? 'The plan was not written, and nothing was built.';
  const ready = conv.status === 'ready';
  return (
    <WizardPage
      title="Stopped before the plan"
      sub={sub}
      bar={{
        primary: (
          <Button
            variant="primary"
            size="md"
            data-action="again"
            disabled={dead ? false : !ready || conv.timeline.running}
            onClick={() => (dead ? restart(conv.id) : void send(conv.id, rerunCommand(model)))}
          >
            Try again
          </Button>
        ),
      }}
    />
  );
}

/** The plan to review, and the same cards once it was set aside or cannot run. */
function ReviewPage({ conv, model, sub }: ScreenProps) {
  const answer = useApp((s) => s.answer);
  const send = useApp((s) => s.send);
  const openForgeSheet = useApp((s) => s.openForgeSheet);
  const openConversation = useApp((s) => s.openConversation);
  const notice = useNotice(conv, model);
  const changed = useChangedPlan(conv, model);
  const buildWithChanges = useBuildWithChanges(conv, model);
  const [rules, setRules] = useState(false);
  const ready = conv.status === 'ready';
  const running = conv.timeline.running;
  const approval = model.approval;
  const waiting = !!approval && !answered(conv, approval);
  const frozen = frozenFile(model);
  const editPlan = () => openForgeSheet('plan', 'next');
  const limits = buildLimits(model);

  let title: string;
  let subLine: ReactNode;
  let bar: WizardPageProps['bar'];
  if (sub === 'set_aside') {
    title = 'Plan set aside';
    subLine = 'Nothing was built. You can build this plan as it is, or describe the job again.';
    bar = {
      secondary: [
        <Button key="again" variant="outline" size="md" data-action="describe" onClick={() => openConversation({ cwd: conv.state.cwd ?? '', kind: 'forge' })}>Describe again</Button>,
      ],
      ask: { placeholder: 'Ask Architect about the plan', disabled: !ready },
      primary: (
        <Button variant="primary" size="md" data-action="build" disabled={!ready || running} onClick={() => void send(conv.id, rerunCommand(model))}>
          {next('Build this plan')}
        </Button>
      ),
    };
  } else if (sub === 'blocked') {
    title = "This plan can't be built yet";
    subLine = blockedLine(model.plan?.footer ?? '');
    bar = {
      ask: { placeholder: 'Ask Architect about the plan', disabled: !ready },
      primary: <Button variant="primary" size="md" data-action="settings" onClick={editPlan}>Change settings</Button>,
    };
  } else {
    title = 'Review the plan';
    subLine = 'Check what the agent will do and how Architect will pick its model. Nothing runs until you click Build.';
    const note = limits.usd !== undefined || limits.minutes !== undefined ? (
      <span className="plan-note">
        {limits.usd !== undefined && <span>Costs at most {fmtUsd(limits.usd)}</span>}
        {limits.minutes !== undefined && <span>{limits.usd !== undefined ? 'and takes' : 'Takes'} at most {limits.minutes} min</span>}
      </span>
    ) : undefined;
    bar = {
      back: { label: 'Back', run: () => { if (approval) void answer(conv.id, approval.id, 'no'); }, disabled: !ready || !waiting },
      secondary: [
        frozen ? (
          <Button key="edit" variant="outline" size="md" data-action="edit-plan" onClick={editPlan}>
            <IconFileDescription size={16} stroke={1.5} aria-hidden />Plan details
          </Button>
        ) : (
          <Button key="edit" variant="outline" size="md" data-action="edit-plan" onClick={editPlan}>
            <IconPencil size={16} stroke={1.5} aria-hidden />Edit plan
          </Button>
        ),
      ],
      ask: { placeholder: 'Ask Architect about the plan', disabled: !ready },
      note,
      primary: changed ? (
        <Button variant="primary" size="md" data-action="build" disabled={!ready || !waiting} onClick={() => void buildWithChanges()}>
          {next('Build with changes')}
        </Button>
      ) : (
        <Button variant="primary" size="md" data-action="build" disabled={!ready || !waiting} onClick={() => { if (approval) void answer(conv.id, approval.id, 'yes'); }}>
          {next('Build agent')}
        </Button>
      ),
    };
  }

  const status = !ready ? STARTING : frozen ? <><IconFile size={14} stroke={1.5} aria-hidden />Opened from your plan file, {frozen}</> : undefined;
  return (
    <WizardPage status={status} title={title} sub={subLine} bar={bar} {...notice}>
      <div className="plan-step-cards">
        <JobCard model={model} onRules={() => setRules(true)} />
        <PickCard model={model} />
      </div>
      <RulesSheet
        open={rules}
        model={model}
        onClose={() => setRules(false)}
        onChangePlan={() => { setRules(false); editPlan(); }}
      />
    </WizardPage>
  );
}

// ---------------------------------------------------------------- the plan's two cards

/** Card 1, What the agent does: what it takes, what it gives back, its rules. */
export function JobCard({ model, onRules }: { model: ForgeModel; onRules: () => void }) {
  const noun = caseNoun(model);
  const takes = takesSentence(model);
  const gives = givesSentence(model);
  const note = schemaNote(outputFields(model), noun);
  const rules = taskRules(model);
  const example = exampleRule(model);
  const checks = checksLine(checkRules(model));
  const invalid = checksInvalid(model);
  return (
    <Card data-plan-card="job">
      <CardHeader>What the agent does</CardHeader>
      <CardBody>
        <dl className="plan-kv">
          {takes && (
            <>
              <dt>Takes</dt>
              <dd>{takes}</dd>
            </>
          )}
          {gives && (
            <>
              <dt>Gives back</dt>
              <dd>{gives}{note && <span className="plan-kv-note">{note}</span>}</dd>
            </>
          )}
          <dt>Rules</dt>
          {rules.length ? (
            <dd>
              {rules.length} {rules.length === 1 ? 'rule' : 'rules'} for reading a {noun}
              <Button variant="link" size="sm" data-action="rules" onClick={onRules}>View all {rules.length}</Button>
              {(example || checks) && (
                <span className="plan-kv-note">{[checks ? '' : example ? `First rule: ${example}.` : '', checks ?? ''].filter(Boolean).join(' ')}</span>
              )}
            </dd>
          ) : (
            <dd>
              The job as written
              <Button variant="link" size="sm" data-action="rules" onClick={onRules}>View</Button>
              {checks && <span className="plan-kv-note">{checks}</span>}
            </dd>
          )}
        </dl>
        {invalid && <p className="plan-kv-warn" role="status">The plan's code checks are not valid, so the build would stop before any model runs.</p>}
      </CardBody>
    </Card>
  );
}

/** Card 2, How Architect picks the model: practice, exam, the pass mark, the pick, then the models at list price. */
export function PickCard({ model }: { model: ForgeModel }) {
  const noun = caseNoun(model);
  const { practice, exam } = caseCounts(model);
  const need = passNeed(model);
  const ids = ladderIds(model);
  const family = modelFamily(ids);
  const dataset = datasetOf(model);
  const nouns = casePlural(noun);

  // "Three Nemotron models, cheapest first, one at a time, on 200 receipts from CORD v2, a public receipt dataset."
  const total = practice !== undefined && exam !== undefined ? practice + exam : undefined;
  const models = ids.length ? `${countWord(ids.length, true)} ${family ? `${family} ` : ''}${ids.length === 1 ? 'model' : 'models'}${ids.length > 1 ? ', cheapest first, one at a time' : ''}` : undefined;
  const source = total !== undefined
    ? `on ${total} ${casePlural(noun, total)}${dataset ? ` from ${dataset.name}${dataset.public ? `, a public ${noun} dataset` : ''}` : ''}`
    : undefined;
  const subLine = models ? `${[models, source].filter(Boolean).join(', ')}.` : undefined;

  const prices = ids.map((id) => listPrice(model, id));
  return (
    <Card data-plan-card="pick">
      <CardHeader>How Architect picks the model</CardHeader>
      <CardBody>
        {subLine && <p className="plan-sub plan-pick-sub">{subLine}</p>}
        <ol className="plan-method" aria-label="How the model is picked">
          <li>
            <p className="plan-method-title"><span className="plan-method-n">1</span>Practice</p>
            <p className="plan-method-text">{practice !== undefined ? `${practice} ${casePlural(noun, practice)}` : `${nouns.charAt(0).toUpperCase()}${nouns.slice(1)}`} with answers. {practiceWords(model)} These scores don't count.</p>
          </li>
          <li>
            <p className="plan-method-title"><span className="plan-method-n">2</span>Exam</p>
            <p className="plan-method-text">{exam !== undefined ? `${exam} other ${casePlural(noun, exam)}` : `Other ${nouns}`} it has never seen. A {noun} counts only when the whole answer matches the answer key.</p>
          </li>
          <li className="is-mark">
            <p className="plan-method-title"><span className="plan-method-n">3</span>{need !== undefined ? `Pass mark: ${need}${exam !== undefined ? ` of ${exam}` : ''}` : 'Pass mark'}</p>
            <p className="plan-method-text">{passSource(model)}</p>
          </li>
          <li>
            <p className="plan-method-title"><span className="plan-method-n">4</span>Pick</p>
            <p className="plan-method-text">The cheapest model that passes becomes your agent.</p>
          </li>
        </ol>
        {ids.length > 0 && (
          <>
            <div className="plan-tiles" role="list" aria-label="Models">
              {ids.map((id, i) => (
                <div key={id} className="plan-tile" role="listitem">
                  <span className="plan-tile-name">{rungName(id)}</span>
                  <span className="plan-tile-full">{prettyModel(id)}</span>
                  {prices[i] && <span className="plan-tile-price">{prices[i]}</span>}
                </div>
              ))}
            </div>
            {prices.some(Boolean) && <p className="plan-tiles-caption">List price per million tokens</p>}
          </>
        )}
      </CardBody>
    </Card>
  );
}
