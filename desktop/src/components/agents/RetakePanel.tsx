/**
 * Where an agent stands in the project it was added to, at the top of its page.
 *
 * An agent Architect builds only runs if it passed its exam. Change it, and it stops
 * until it passes again. The panel says which of five things is true (passed, stopped,
 * retaking, did not pass, passed again) and offers what can be done about a change:
 * Retake exam, Try on practice and Undo. A retake's attempt, cases and cost are on the
 * page before its button is pressed, and the button asks once more, since an attempt
 * cannot be given back. An agent never added to a project has no panel.
 */
import clsx from 'clsx';
import { Fragment, useCallback, useEffect, useState, type ReactNode } from 'react';
import { prettyModel } from '../../lib/models';
import {
  answeredWords,
  attemptCost,
  attemptRows,
  BUILD_VERSION,
  cancelWords,
  casesWords,
  changedFact,
  compareNote,
  confirmWords,
  examFact,
  passedLead,
  practiceWords,
  REFUSED_BEFORE,
  retakeWords,
  rightWords,
  ruleWords,
  sameExamPassed,
  scoreWords,
  sourceWords,
  spentWords,
  stoppedNote,
  UNDO_WORDS,
  UNDONE,
  WRITE_FOLDER,
  type RetakeReply,
  type RetakeView,
} from '../../lib/retake';
import { reasonWords } from '../../lib/vocab';
import { useApp } from '../../store/app';
import { Button } from '../ui/Button';
import { Card, CardBody } from '../ui/Card';
import { Meter } from '../ui/Meter';
import { Working } from '../ui/Working';

const POLL_MS = 800;
const ACTION = { retake: 'The retake', practice: 'The practice run', undo: 'Undo', retake_cancel: 'Cancelling' } as const;

/** The agent's standing in its project, asked once and then followed while a run is open. */
export function useRetake(convId: string, name: string): { view: RetakeView | null; setView: (reply: RetakeReply) => void } {
  const request = useApp((s) => s.request);
  const [view, set] = useState<RetakeView | null>(null);
  const setView = useCallback((reply: RetakeReply) => set(reply && reply.placed ? reply : null), []);
  const load = useCallback(async () => {
    try {
      setView(await request<RetakeReply>(convId, 'retake_status', { name }));
    } catch {
      // A bridge without the method, or an agent it cannot place: no panel.
    }
  }, [convId, name, request, setView]);
  useEffect(() => {
    void load();
  }, [load]);
  const open = view?.state === 'retaking' || view?.practice?.running === true;
  useEffect(() => {
    if (!open) return undefined;
    const timer = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(timer);
  }, [open, load]);
  return { view, setView };
}

/** Numbers, dollars and file names in the mono face, as the build's figures are. */
export function Figures({ text }: { text: string }) {
  const parts = text.split(/(Nemotron [\d.]+ \w+|\b(?:artifact|agents)\/[\w./-]*\w|harness\.py|\$?\d+(?:[.,]\d+)*)/g);
  return (
    <>
      {parts.map((part, i) => (i % 2 === 1 && !part.startsWith('Nemotron') ? <span key={i} className="num retake-num">{part}</span> : <Fragment key={i}>{part}</Fragment>))}
    </>
  );
}

type Mark = 'passed' | 'stopped' | 'did_not_pass' | 'retaking';
const MARK_LABEL: Record<Mark, string> = { passed: 'Passed', stopped: 'Stopped', did_not_pass: 'Did not pass', retaking: 'Running' };

/** The verdict's shape: a check, a pause for stopped, a cross for did not pass. Each differs in outline, not only in colour. */
function VerdictMark({ kind }: { kind: Mark }) {
  if (kind === 'retaking') return <span className="retake-mark is-running"><Working size="md" label="Running" /></span>;
  return (
    <svg width="26" height="26" viewBox="0 0 26 26" role="img" aria-label={MARK_LABEL[kind]} className={clsx('retake-mark', `is-${kind}`)}>
      {kind === 'passed' && <><circle cx="13" cy="13" r="12" className="retake-mark-fill" /><path d="M7.5 13.4l3.6 3.6 7.4-7.6" className="retake-mark-on" strokeWidth="2.2" /></>}
      {kind === 'stopped' && <><circle cx="13" cy="13" r="12" className="retake-mark-fill" /><path d="M10 8.5v9M16 8.5v9" className="retake-mark-on" strokeWidth="2.6" /></>}
      {kind === 'did_not_pass' && <><circle cx="13" cy="13" r="11" className="retake-mark-ring" strokeWidth="2" /><path d="M9 9l8 8M17 9l-8 8" className="retake-mark-ring" strokeWidth="2.2" /></>}
    </svg>
  );
}

function Verdict({ kind, children }: { kind: Mark; children: ReactNode }) {
  return (
    <div className="retake-verdict">
      <VerdictMark kind={kind} />
      <h2 className={clsx('retake-title', (kind === 'stopped' || kind === 'did_not_pass') && 'is-stop')}>{children}</h2>
    </div>
  );
}

function FactsTable({ rows }: { rows: Array<[string, ReactNode]> }) {
  if (!rows.length) return null;
  return (
    <table className="retake-facts">
      <tbody>
        {rows.map(([label, value]) => <tr key={label}><th scope="row">{label}</th><td>{value}</td></tr>)}
      </tbody>
    </table>
  );
}

/** One thing to do, with what it does and costs under its button. */
function Choice({ children, action }: { children: ReactNode; action: ReactNode }) {
  return <div className="retake-choice"><div className="retake-choice-action">{action}</div><div className="retake-choice-text">{children}</div></div>;
}

/** A button that asks once more before it acts: a retake uses an attempt, an undo discards an edit. */
function Twice({ label, confirm, primary, pending, disabled, action, onConfirm }: {
  label: string; confirm: string; primary?: boolean; pending?: boolean; disabled?: boolean; action: string; onConfirm: () => void;
}) {
  const [asking, setAsking] = useState(false);
  if (!asking || pending) {
    return <Button variant={primary ? 'primary' : 'outline'} data-action={action} pending={pending} disabled={disabled} onClick={() => setAsking(true)}>{label}</Button>;
  }
  return (
    <span className="retake-twice">
      <Button variant="primary" data-action={`${action}-confirm`} onClick={() => { setAsking(false); onConfirm(); }}>{confirm}</Button>
      <Button variant="ghost" onClick={() => setAsking(false)}>Not now</Button>
    </span>
  );
}

function Attempts({ view }: { view: RetakeView }) {
  if (view.attempts.length < 2) return null;
  const unit = view.unit.charAt(0).toUpperCase() + view.unit.slice(1);
  return (
    <section className="retake-attempts">
      <h3>Attempts</h3>
      <table>
        <thead>
          <tr>
            <th scope="col">No.</th>
            <th scope="col">What was examined</th>
            <th scope="col">{unit}</th>
            <th scope="col" className="is-end">Score</th>
            <th scope="col" className="is-end">Result</th>
          </tr>
        </thead>
        <tbody>
          {attemptRows(view).map((row) => (
            <tr key={row.number} className={clsx(row.current && 'is-current')}>
              <td className="num retake-num">{row.number}</td>
              <td>{row.what}</td>
              <td><Figures text={row.cases} /></td>
              <td className="num retake-num is-end">{row.score}</td>
              <td className="is-end">{row.result}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="retake-note">{compareNote(view.unit)}</p>
    </section>
  );
}

export interface RetakePanelProps {
  convId: string;
  name: string;
  view: RetakeView;
  onView: (reply: RetakeReply) => void;
  /** The agent's answer format, to say what counts as right. */
  outputSchema?: unknown;
  onTry?: () => void;
  onReport?: () => void;
}

export function RetakePanel({ convId, name, view, onView, outputSchema, onTry, onReport }: RetakePanelProps) {
  const request = useApp((s) => s.request);
  const [busy, setBusy] = useState<'' | 'retake' | 'practice' | 'undo' | 'retake_cancel'>('');
  const [error, setError] = useState('');
  const [undone, setUndone] = useState(false);
  // "Keep the change and edit again": the did-not-pass page steps aside for that attempt.
  const [kept, setKept] = useState<number | null>(null);

  const act = async (method: 'retake' | 'practice' | 'undo' | 'retake_cancel') => {
    setBusy(method);
    setError('');
    try {
      const reply = await request<RetakeReply>(convId, method, { name });
      setUndone(method === 'undo');
      onView(reply);
    } catch (err) {
      // The bridge's own lines when it has them; else the action by name and why, in words.
      const e = err as { code?: string; detail?: string };
      setError(e.detail || `${ACTION[method]} did not go through: ${reasonWords(e.code ?? 'failed')}.`);
    } finally {
      setBusy('');
    }
  };

  const { unit, plan, last, progress } = view;
  const page = view.state === 'did_not_pass' && kept === last?.number ? 'stopped' : view.state;
  const note = view.note ? <p className="retake-note">{view.note}</p> : null;
  const failed = error ? <p className="retake-line is-err" role="alert">{error}</p> : null;
  const undo = (primary: boolean) => (
    <Choice action={<Twice label="Undo" confirm="Put back the version that passed" action="undo" primary={primary} pending={busy === 'undo'} disabled={!!busy} onConfirm={() => void act('undo')} />}>
      <p>{UNDO_WORDS}</p>
    </Choice>
  );

  let body: ReactNode;
  if (page === 'retaking' && progress) {
    body = (
      <>
        <Verdict kind="retaking"><Figures text={`Retaking the exam. Attempt ${progress.attempt}.`} /></Verdict>
        <p className="retake-lead"><Figures text={answeredWords(progress)} /></p>
        <Meter className="retake-meter" size="row" value={progress.done} max={progress.total ?? 1} label="Answered" />
        {spentWords(progress) && <p className="retake-line"><Figures text={spentWords(progress)} /></p>}
        {casesWords(progress) && <p className="retake-line"><Figures text={casesWords(progress)} /></p>}
        <div className="retake-row">
          <Button data-action="retake-cancel" pending={busy === 'retake_cancel'} onClick={() => void act('retake_cancel')}>Cancel retake</Button>
        </div>
        <p className="retake-note"><Figures text={cancelWords(progress)} /></p>
        {failed}
      </>
    );
  } else if (page === 'did_not_pass' && last) {
    const need = last.pass_mark ?? plan?.pass_mark ?? view.pass_mark;
    const cost = attemptCost(last);
    body = (
      <>
        <Verdict kind="did_not_pass"><Figures text={`${scoreWords(last.right, last.total, need)} Did not pass.`} /></Verdict>
        <p className="retake-lead"><Figures text={`${sourceWords(last.cases, last.number, last.total, unit)} Your app's calls stay stopped.`} /></p>
        <FactsTable rows={changedFact(view) ? [['Changed', <Figures key="c" text={changedFact(view)} />]] : []} />
        <div className="retake-choices" data-count="2">
          {view.can_undo && undo(true)}
          <Choice action={<Button data-action="keep" disabled={!!busy} onClick={() => setKept(last.number)}>Keep the change and edit again</Button>}>
            <p>Your agent stays stopped while you edit. Trying a change on practice {unit} is not an attempt.</p>
          </Choice>
        </div>
        {failed}
        {(cost || view.note) && <p className="retake-note retake-foot"><Figures text={[cost, view.note].filter(Boolean).join(' ')} /></p>}
      </>
    );
  } else if (page === 'passed') {
    const again = view.attempt > 1;
    const model = view.model ? prettyModel(view.model) : '';
    const changed = view.attempts.find((a) => a.number === view.attempt)?.what;
    body = again ? (
      <>
        <Verdict kind="passed"><Figures text={scoreWords(view.right, view.total, view.pass_mark)} /><br />Passed. It is your agent.</Verdict>
        <p className="retake-lead"><Figures text={`${sourceWords(view.cases, view.attempt, view.total, unit)} Your app's calls work again.`} /></p>
        {view.cases === 'same_exam' && <p className="retake-line"><Figures text={sameExamPassed(view)} /></p>}
        <FactsTable rows={[
          ...(changed ? [['Changed', <Figures key="c" text={changed} />] as [string, ReactNode]] : []),
          ...(model ? [['Model', model] as [string, ReactNode]] : []),
          ['In your project', <span key="f" className="num retake-num">{view.folder}</span>],
        ]} />
        <Attempts view={view} />
        {note}
      </>
    ) : (
      <>
        <Verdict kind="passed">Passed. It is your agent.</Verdict>
        {undone && <p className="retake-line" role="status"><Figures text={UNDONE} /></p>}
        {passedLead(view) && <p className="retake-lead"><Figures text={passedLead(view)} /></p>}
        <p className="retake-line">{rightWords(unit, outputSchema)}</p>
        <FactsTable rows={[
          ...(model ? [['Model', model] as [string, ReactNode]] : []),
          ['Exam', <Figures key="e" text={examFact(view)} />],
          ['In your project', <span key="f" className="num retake-num">{view.folder}</span>],
        ]} />
        {(onTry || onReport) && (
          <div className="retake-row">
            {onTry && <Button variant="primary" data-action="retake-try" onClick={onTry}>Try your agent</Button>}
            {onReport && <Button data-action="retake-report" onClick={onReport}>Full report</Button>}
          </div>
        )}
        <p className="retake-line retake-rule">{ruleWords(view.folder, view.retakes)}</p>
        <Attempts view={view} />
        {note}
      </>
    );
  } else {
    // Stopped: the sentence and the line under it are Python's, the same as in the app and the terminal.
    const [sentence, reason, ...more] = view.lines;
    const before = last && last.result !== 'started' && (
      last.result === 'cancelled'
        ? `Attempt ${last.number} was cancelled. It still counts as an attempt.`
        : last.result === 'not_placed'
          ? `Attempt ${last.number} passed, but its folder was not written.`
          : `Attempt ${last.number} did not pass: ${scoreWords(last.right, last.total, last.pass_mark ?? plan?.pass_mark)}`
    );
    const offers = view.can_retake && plan;
    body = (
      <>
        <Verdict kind="stopped"><Figures text={sentence ?? 'Stopped.'} /></Verdict>
        {reason && <p className="retake-lead"><Figures text={reason} /></p>}
        {more.map((line) => <p key={line} className="retake-line"><Figures text={line} /></p>)}
        <p className="retake-line">{[REFUSED_BEFORE, stoppedNote(view)].filter(Boolean).join(' ')}</p>
        {before && <p className="retake-line"><Figures text={before} /></p>}
        {offers ? (
          <div className="retake-choices" data-count="3">
            <Choice action={<Twice label={plan.place_only ? WRITE_FOLDER : 'Retake exam'} confirm={confirmWords(plan)} action="retake" primary pending={busy === 'retake'} disabled={!!busy || view.practice?.running} onConfirm={() => void act('retake')} />}>
              <p><Figures text={retakeWords(plan)} /></p>
            </Choice>
            <Choice action={<Button data-action="practice" pending={busy === 'practice' || view.practice?.running} disabled={!!busy} onClick={() => void act('practice')}>Try on practice</Button>}>
              <p><Figures text={practiceWords(plan)} /></p>
              {view.practice?.running && <p role="status">Scoring the change on practice {unit}.</p>}
              {view.practice && !view.practice.running && (view.practice.lines ?? []).slice(0, 1).map((line) => (
                <p key={line} className="retake-result" role="status"><Figures text={line} /></p>
              ))}
            </Choice>
            {view.can_undo && undo(false)}
          </div>
        ) : view.can_undo ? (
          <div className="retake-choices" data-count="1">{undo(true)}</div>
        ) : null}
        {(view.refused ?? []).map((line) => <p key={line} className="retake-line is-err" role="alert">{line}</p>)}
        {failed}
        <Attempts view={view} />
      </>
    );
  }

  return (
    <>
      <Card className="retake" data-retake={page}>
        <CardBody>{body}</CardBody>
      </Card>
      {(page !== 'passed' || view.attempt > 1) && <p className="screen-note retake-below">{BUILD_VERSION}</p>}
    </>
  );
}
