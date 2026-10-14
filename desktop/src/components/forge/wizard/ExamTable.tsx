/**
 * How each model did on the exam (clarity spec §3, step 4; mockups 4-results, 4b, 4c): one
 * row per model with its score as a bar from 0 to the exam's size, the pass mark as a
 * dashed rule, the count it got right, whether it passed and what it cost per 1,000 cases.
 * Below it, the shipping model's exam as one dot per case (the existing ExamChart), folded
 * away until asked for.
 *
 * Every figure is the result card's: the signed counts per model (`ladderView`) and the
 * mean cost per case (B1). Nothing is drawn for a value the build did not report: a model
 * with no cost leaves the cost column out, an exam that was not scored says so.
 */
import { IconChevronDown } from '@tabler/icons-react';
import clsx from 'clsx';
import { useId, useState, type ReactNode } from 'react';
import type { Ladder } from '../../../lib/forge';
import { casePlural, rightOf, shortBy } from '../../../lib/wizard';
import { cardWords } from '../../../lib/vocab';
import { finite, fmtUsd, type Rung } from '../../../lib/viz';
import { Badge } from '../../ui/Badge';
import { Button } from '../../ui/Button';
import { Card, CardBody, CardHeader } from '../../ui/Card';
import { StatusMark } from '../../ui/StatusMark';
import { ExamChart } from '../ExamChart';

const WORDS = ['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten'];

/** A small count as a word ("three"), a larger one as digits. */
export function countWord(n: number): string {
  return WORDS[n] ?? String(n);
}

/** The exam's size: the result's counts, else the first model's. */
export function examSize(ladder: Ladder): number | undefined {
  return ladder.exam?.counts.cases ?? ladder.rungs.map((r) => r.exam?.counts?.cases).find(finite);
}

const pct = (v: number, of: number) => `${Math.max(0, Math.min(100, (v / of) * 100))}%`;

export interface ExamTableProps {
  ladder: Ladder;
  /** The pass mark, as cases. */
  need: number;
  /** What one case is called ("receipt"). */
  noun: string;
  /** The line under the rows: why not the top model, or what to change. */
  footer?: ReactNode;
  /**
   * The exam's size in cases, as the screen worked it out (the counts, else the plan).
   * Unknown, the table draws no axis and says no "out of".
   */
  n?: number;
}

export function ExamTable({ ladder, need, noun, footer, n }: ExamTableProps) {
  const rungs = ladder.rungs;
  const costs = rungs.some((r) => finite(r.cost));
  const ticks = n !== undefined ? [0, 1, 2, 3, 4, 5].map((i) => Math.round((i * n) / 5)) : [];
  const pm = n !== undefined ? pct(need, n) : undefined;
  return (
    <Card className="exam-table-card">
      <CardHeader
        meta={`${n !== undefined ? `The same ${n}-${noun} exam for each` : 'The same exam for each'}. A ${noun} counts only when the whole answer matches the answer key.`}
        action={(
          <ul className="exam-legend" aria-label="Key">
            <li><i className="exam-swatch is-pass" aria-hidden />Passed</li>
            <li><i className="exam-swatch is-fail" aria-hidden />Did not pass</li>
            <li><i className="exam-swatch is-mark" aria-hidden />Pass mark</li>
          </ul>
        )}
      >
        How the {countWord(rungs.length)} {rungs.length === 1 ? 'model' : 'models'} did
      </CardHeader>
      <CardBody pad="flush">
        <table className="exam-table">
          <thead>
            <tr>
              <th scope="col" className="exam-col-model">Model</th>
              <th scope="col" className="exam-col-bar">{n !== undefined ? `Exam score, out of ${n}` : 'Exam score'}</th>
              <th scope="col" className="exam-col-right">Right</th>
              <th scope="col" className="exam-col-result">Result</th>
              {costs && <th scope="col" className="exam-col-cost">Running cost per 1,000 {casePlural(noun)}</th>}
            </tr>
          </thead>
          <tbody>
            {rungs.map((rung) => <ExamRow key={rung.key} rung={rung} need={need} n={n} costs={costs} pm={pm} />)}
            {n !== undefined && pm !== undefined && <tr className="exam-axis-row" aria-hidden>
              <td />
              <td>
                <div className="exam-axis num">
                  {ticks.map((t) => <span key={t} style={{ left: pct(t, n) }}>{t}</span>)}
                  <span className="exam-axis-mark" style={{ left: pm }}>Pass mark {need}</span>
                </div>
              </td>
              <td colSpan={costs ? 3 : 2} />
            </tr>}
          </tbody>
        </table>
      </CardBody>
      {footer && <div className="exam-table-footer">{footer}</div>}
    </Card>
  );
}

function ExamRow({ rung, need, n, costs, pm }: { rung: Rung; need: number; n?: number; costs: boolean; pm?: string }) {
  const right = rightOf(rung);
  const scored = rung.state === 'done' && right !== undefined && n !== undefined;
  const short = scored ? shortBy(rung, need) : undefined;
  let result: ReactNode;
  if (rung.state !== 'done') {
    result = <span className="exam-result"><StatusMark tone="neutral" label="Not run" showLabel /></span>;
  } else if (rung.licensed) {
    result = <span className="exam-result"><StatusMark tone="ok" label="Passed" showLabel /></span>;
  } else {
    const why = short !== undefined ? `${short} short` : rung.reason ? cardWords(rung.reason) : undefined;
    result = (
      <span className="exam-result">
        <StatusMark tone="err" label="Did not pass" showLabel />
        {why && <small className="exam-short">{why}</small>}
      </span>
    );
  }
  return (
    <tr className={clsx('exam-row', rung.ships && 'is-ships')} data-model={rung.key}>
      <th scope="row" className="exam-model">
        <span className="exam-model-name">{rung.name}{rung.ships && <Badge className="exam-tag">Your agent</Badge>}</span>
        {rung.model !== rung.name && <span className="exam-model-full">{rung.model}</span>}
      </th>
      <td>
        <div className="exam-bar" role="img" aria-label={scored ? `${right} of ${n} right` : 'Not scored'}>
          <span className="exam-bar-track" />
          {scored && <span className={clsx('exam-bar-fill', rung.licensed ? 'is-pass' : 'is-fail')} style={{ width: pct(right, n) }} />}
          {pm !== undefined && <span className="exam-bar-mark" style={{ left: pm }} />}
        </div>
      </td>
      <td className="exam-right num">{scored ? right : rung.state === 'done' ? 'Not scored' : ''}</td>
      <td>{result}</td>
      {costs && <td className="exam-cost num">{finite(rung.cost) ? fmtUsd(rung.cost * 1000) : ''}</td>}
    </tr>
  );
}

/**
 * The shipping model's exam, one dot per case, folded until "Show": its header keeps the
 * counts in view (right, wrong, and only when there are any, wrong format and over budget).
 * The dots know only the counts, so the words beside them say so.
 */
export function ExamDots({ ladder, need, noun }: { ladder: Ladder; need: number; noun: string }) {
  const [open, setOpen] = useState(false);
  const body = useId();
  const exam = ladder.exam;
  const c = exam?.counts;
  if (!exam || !c || c.correct === null) return null;
  const n = c.cases;
  const right = c.correct;
  const wrong = n - right;
  const missed = Math.max(0, wrong - c.invalid - c.overBudget);
  const name = exam.name;
  const nouns = casePlural(noun);
  return (
    <Card className="exam-dots-card">
      <div className="exam-dots-head">
        <h2 className="ui-card-title">{name}&apos;s exam score as {n} dots</h2>
        <ul className="exam-dots-key">
          <li><i className="exam-dot is-right" aria-hidden /><b className="num">{right}</b> right</li>
          <li><i className="exam-dot is-wrong" aria-hidden /><b className="num">{wrong}</b> wrong</li>
          {c.invalid > 0 && <li><i className="exam-dot is-invalid" aria-hidden /><b className="num">{c.invalid}</b> wrong format</li>}
          {c.overBudget > 0 && <li><i className="exam-dot is-invalid" aria-hidden /><b className="num">{c.overBudget}</b> over budget</li>}
        </ul>
        <Button variant="ghost" size="sm" className="exam-dots-toggle" aria-expanded={open} aria-controls={body} onClick={() => setOpen((o) => !o)}>
          {open ? 'Hide' : 'Show'}
          <IconChevronDown size={16} stroke={1.5} aria-hidden className={clsx('exam-dots-chevron', open && 'is-open')} />
        </Button>
      </div>
      {open && (
        <div id={body} className="exam-dots-body">
          <ExamChart report={{ counts: c, bar: exam.bar, licensed: exam.licensed }} cell={12} gap={3} passLine />
          <div className="exam-dots-words">
            <p className="exam-dots-lead">
              Architect keeps only the count, so these dots can&apos;t show which {nouns} {name} got wrong.
            </p>
            <p>
              Not keeping which ones means no one can tune an agent on the exam. Right ones are drawn first, and a {noun} is right only when its whole answer matches the answer key.
            </p>
            <p className="is-mute">
              {missed} had at least one field off.{' '}
              {c.invalid === 0 ? 'Every answer came back in the right format.' : `${c.invalid} came back in the wrong format.`}
              {c.overBudget > 0 ? ` ${c.overBudget} went over the budget for one run.` : ''}
              {' '}The dashed line is the pass mark: {name} needed {need} and got {right}.
            </p>
          </div>
        </div>
      )}
    </Card>
  );
}
