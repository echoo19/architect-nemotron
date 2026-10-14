/**
 * Step 3, Build (clarity spec §3, mockup 3-building, with C1): one row per model, cheapest
 * first, each with its practice and exam and how long it took. No row shows an exam score,
 * a pass or a fail: the scores reach the app only with the result (the live feed carries
 * practice alone), so a finished model reads "Done" with a neutral check until Results.
 *
 * Times come from the bridge's stage stamps (B3 `at`): a model runs from its first stage to
 * the next model's first stage. A replay with no stamps leaves the durations out. The
 * build's own clock counts from the Build press, and its time left is the plan's cap.
 */
import { IconCheck, IconPencil, IconSquare } from '@tabler/icons-react';
import clsx from 'clsx';
import type { ReactNode } from 'react';
import { buildLimits, buildWork, caseNoun, casePlural, fmtDuration, ladderTag, minutesLeft, rungSeconds } from '../../../lib/wizard';
import { cardWords, examCount, noticeLine, passMark, practiceCount, rerunCommand } from '../../../lib/vocab';
import { fmtUsd, type Rung } from '../../../lib/viz';
import { errorText, useApp } from '../../../store/app';
import { Button } from '../../ui/Button';
import { Card, CardBody, CardHeader } from '../../ui/Card';
import { StatusMark } from '../../ui/StatusMark';
import { Working } from '../../ui/Working';
import { countWord } from './ExamTable';
import type { ScreenProps } from './types';
import { useClock, useNow } from './useClock';
import { WizardPage } from './WizardPage';

type RowState = 'done' | 'running' | 'waiting' | 'stopped' | 'skipped';

/**
 * The Working mark inside a pine ring: on its own, a frame of the mark's motion is a thin
 * upright bar that reads as a text cursor; in the ring it reads as "running", like the
 * done and waiting circles beside it.
 */
export function LiveMark({ size = 'sm' }: { size?: 'sm' | 'xs' }) {
  return <span className={clsx('live-ring', `is-${size}`)} aria-hidden><Working size="xs" /></span>;
}

/** The last top-level stage the feed saw for the model at `index` (0-based). */
function stageOf(stages: Array<Record<string, unknown>>, index: number): number | undefined {
  const own = stages.filter((s) => Number(s.depth ?? 0) === 0 && s.model_index === index + 1);
  const stage = own.at(-1)?.stage;
  return typeof stage === 'number' ? stage : undefined;
}

/** The model the build was on when it stopped (0-based), from the feed's last stage. */
function lastStarted(stages: Array<Record<string, unknown>>): number | undefined {
  const at = stages.filter((s) => Number(s.depth ?? 0) === 0).at(-1)?.model_index;
  return typeof at === 'number' ? at - 1 : undefined;
}

export function BuildScreen({ conv, model, ladder, sub }: ScreenProps) {
  const send = useApp((s) => s.send);
  const cancel = useApp((s) => s.cancel);
  const openForgeSheet = useApp((s) => s.openForgeSheet);
  const setScreen = useApp((s) => s.setScreen);
  const setWizardView = useApp((s) => s.setWizardView);
  const stopped = sub === 'stopped_build';
  const ready = conv.status === 'ready';
  const elapsed = useClock(buildWork(conv.timeline.items));
  const now = useNow(!stopped);

  const noun = caseNoun(model);
  const practice = practiceCount(model);
  const exam = examCount(model);
  const rungs = ladder?.rungs ?? [];
  const n = rungs.length;
  const all = n ? countWord(n) : undefined;
  const limits = buildLimits(model);
  const mark = passMark(model);
  const notice = noticeLine(model);
  const rerun = () => void send(conv.id, rerunCommand(model));
  const stoppedAt = stopped && !ladder?.finished ? lastStarted(conv.forge.stages) : undefined;

  const rowState = (rung: Rung, i: number): RowState => {
    if (i === stoppedAt) return 'stopped';
    if (rung.state === 'running') return stopped ? 'stopped' : 'running';
    if (rung.state === 'done') return 'done';
    if (rung.state === 'skipped') return 'skipped';
    return 'waiting';
  };

  // ---- the words above the list
  const sub1 = [
    practice !== undefined && exam !== undefined
      ? `Each model practises on ${practice} ${casePlural(noun, practice)}, then takes the exam on ${exam} it has never seen.`
      : 'Each model practises first, then takes an exam it has never seen.',
    all ? `They run one at a time, cheapest first, and all ${all} take the same exam even after one passes, so you can compare what each gets right for its price.` : '',
  ].filter(Boolean).join(' ');

  let title: string;
  let subText: ReactNode;
  let status: ReactNode;
  if (!stopped) {
    title = 'Building your agent';
    subText = sub1;
    const left = limits.seconds !== undefined && elapsed !== undefined ? minutesLeft(limits.seconds, elapsed) : undefined;
    const cap = [limits.minutes !== undefined ? `${limits.minutes} min` : '', limits.usd !== undefined ? fmtUsd(limits.usd) : ''].filter(Boolean).join(' or ');
    status = conv.status === 'starting' ? (
      <span className="build-status"><Working size="xs" /><b>Starting</b></span>
    ) : (
      <span className="build-status">
        <LiveMark size="xs" />
        <b>Building</b>
        {elapsed !== undefined && <><span className="build-sep" aria-hidden /><span><span className="num">{fmtDuration(elapsed / 1000)}</span> elapsed</span></>}
        {left !== undefined && (
          <>
            <span className="build-sep" aria-hidden />
            <span><span className="num">{left} min</span> left at most{cap ? `: it stops on its own at ${cap}` : ''}</span>
          </>
        )}
      </span>
    );
  } else {
    title = 'The build stopped';
    subText = notice?.text
      ?? (conv.status === 'failed' || conv.status === 'stopped' ? errorText(conv.error ?? 'stopped') : 'You stopped the build.');
    status = (
      <span className="build-status">
        <StatusMark tone="neutral" label="Stopped" showLabel />
        {elapsed !== undefined && <><span className="build-sep" aria-hidden /><span>after <span className="num">{fmtDuration(elapsed / 1000)}</span></span></>}
      </span>
    );
  }

  const retry = notice?.level === 'err' && notice.action === 'retry';
  const bar = stopped
    ? {
      secondary: [
        <Button key="plan" variant="outline" size="md" data-action="change-plan" onClick={() => openForgeSheet('plan', 'next')}>
          <IconPencil size={16} stroke={1.5} aria-hidden />Change the plan
        </Button>,
        ...(notice?.action === 'settings'
          ? [<Button key="settings" variant="outline" size="md" data-action="settings" onClick={() => setScreen('settings', 'models')}>Open settings</Button>]
          : []),
      ],
      ask: { placeholder: 'Ask Architect about the build', disabled: !ready },
      primary: (
        <Button variant="primary" size="md" data-action={retry ? 'retry' : 'again'} disabled={!ready || conv.timeline.running} onClick={rerun}>
          {retry ? 'Retry' : 'Build again'}
        </Button>
      ),
    }
    : {
      secondary: [
        <Button key="stop" variant="outline" size="md" data-action="stop" disabled={!conv.timeline.running} onClick={() => void cancel(conv.id)}>
          <IconSquare size={16} stroke={1.5} aria-hidden />Stop build
        </Button>,
      ],
      ask: { placeholder: 'Ask Architect about the build', disabled: !ready },
      note: all ? `Results open when all ${all} are done.` : 'Results open when the build is done.',
      ownStop: true,
      primary: (
        <Button variant="primary" size="md" data-action="results" disabled={!model.result} onClick={() => setWizardView(conv.id, 'results')}>
          See results
        </Button>
      ),
    };

  return (
    <WizardPage status={status} title={title} sub={subText} bar={bar}>
      <Card className="build-card" aria-label="The models">
        <CardHeader meta={[mark ? `Pass mark: ${mark}.` : '', all ? `Exam scores arrive together when all ${all} are done.` : 'Exam scores arrive when the build is done.'].filter(Boolean).join(' ')}>
          {all ? `${all.charAt(0).toUpperCase()}${all.slice(1)} ${n === 1 ? 'model' : 'models'}${n > 1 ? ', cheapest first' : ''}` : 'The models'}
        </CardHeader>
        <CardBody pad="flush">
          {rungs.length ? (
            <ol className="build-rows">
              {rungs.map((rung, i) => (
                <BuildRow
                  key={rung.key}
                  rung={rung}
                  state={rowState(rung, i)}
                  stage={stageOf(conv.forge.stages, i)}
                  forge={model.plan?.data?.engineer === 'forge'}
                  tag={ladderTag(i, n)}
                  previous={i > 0 ? rungs[i - 1].name : undefined}
                  now={now}
                />
              ))}
            </ol>
          ) : (
            <p className="build-empty"><Working size="sm" /><span>Starting the first model</span></p>
          )}
        </CardBody>
      </Card>
    </WizardPage>
  );
}

/** One sub-step: a check when it is done, the Working mark while it runs, nothing before. */
function Part({ state, children }: { state: 'done' | 'running' | 'todo'; children: ReactNode }) {
  return (
    <span className={clsx('build-part', `is-${state}`)}>
      {state === 'done' && <IconCheck size={14} stroke={1.75} aria-hidden />}
      {state === 'running' && <LiveMark size="xs" />}
      <span>{children}</span>
    </span>
  );
}

function BuildRow({ rung, state, stage, forge, tag, previous, now }: {
  rung: Rung; state: RowState; stage?: number; forge: boolean; tag?: string; previous?: string; now: number;
}) {
  const live = state === 'running';
  const seconds = rungSeconds(rung, live ? now : undefined);
  const duration = seconds !== undefined ? fmtDuration(seconds) : undefined;
  // Stages 1 to 3 are practice, stage 4 the exam.
  const onExam = stage !== undefined && stage >= 4;
  const practice: 'done' | 'running' | 'todo' = state === 'done' || onExam ? 'done' : live ? 'running' : 'todo';
  const examPart: 'done' | 'running' | 'todo' = state === 'done' ? 'done' : live && onExam ? 'running' : 'todo';
  const practiceWord = live && !onExam && stage === 3 && forge ? 'Practice: writing its own instructions' : 'Practice';
  const examWord = live && onExam ? 'Taking the exam' : 'Exam';

  let glyph: ReactNode;
  let word: string;
  let detail: string | undefined;
  switch (state) {
    case 'done':
      glyph = <span className="build-glyph is-done"><IconCheck size={14} stroke={1.75} aria-hidden /></span>;
      word = 'Done';
      detail = duration;
      break;
    case 'running':
      glyph = <span className="build-glyph is-running"><Working size="sm" /></span>;
      word = 'Running';
      detail = duration ? `${duration} so far` : undefined;
      break;
    case 'stopped':
      glyph = <span className="build-glyph is-stopped"><IconSquare size={12} stroke={1.75} aria-hidden /></span>;
      word = 'Stopped';
      detail = duration;
      break;
    case 'skipped':
      glyph = <span className="build-glyph is-waiting" aria-hidden />;
      word = 'Skipped';
      detail = rung.reason ? cardWords(rung.reason) : undefined;
      break;
    default:
      glyph = <span className="build-glyph is-waiting" aria-hidden />;
      word = 'Waiting';
      detail = previous ? `starts after ${previous}` : undefined;
  }

  return (
    <li className={clsx('build-row', `is-${state}`)} data-model={rung.key}>
      {glyph}
      <div className="build-main">
        <div className="build-name">
          <b>{rung.name}</b>
          <span className="build-full">{rung.model !== rung.name ? rung.model : ''}{tag ? `${rung.model !== rung.name ? ' · ' : ''}${tag}` : ''}</span>
        </div>
        <div className="build-parts">
          <Part state={practice}>{practiceWord}</Part>
          <Part state={examPart}>{examWord}</Part>
        </div>
      </div>
      <div className="build-when">
        <span className="build-word">{word}</span>
        {detail && <span className="build-detail num">{detail}</span>}
      </div>
    </li>
  );
}
