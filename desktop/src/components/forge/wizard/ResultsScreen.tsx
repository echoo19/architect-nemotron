/**
 * Step 4, Results (clarity spec §3; mockups 4-results, 4c-results-exam, 4b-no-pass): what
 * the exam found and which model became the agent. `passed` names the cheapest model past
 * the pass mark and why not a higher scorer; `no_pass` says how far each model fell short
 * and what pass mark would change that; `unscored` says why the exam has no score.
 *
 * The words are made from the result card alone: the signed counts per model, their mean
 * cost per case (B1), the plan's pass mark and the build's clock and cost.
 */
import { IconArrowRight, IconFileDescription, IconPencil } from '@tabler/icons-react';
import clsx from 'clsx';
import type { ReactNode } from 'react';
import type { ForgeModel, Ladder } from '../../../lib/forge';
import {
  buildCost, buildLimits, buildWork, caseNoun, casePlural, fmtDuration, noPassLines, passNeed, priceRatio, resultCodes, rightOf, whyNotLine,
} from '../../../lib/wizard';
import { cardWords, examCount, jobLine, noticeLine, practiceCount, refusalReason } from '../../../lib/vocab';
import { fmtUsd, type Rung } from '../../../lib/viz';
import { useApp } from '../../../store/app';
import { Button } from '../../ui/Button';
import { Card } from '../../ui/Card';
import { StatusMark } from '../../ui/StatusMark';
import { frozenFile } from '../PlanSheet';
import { Working } from '../../ui/Working';
import { ExamDots, examSize, ExamTable } from './ExamTable';
import type { ScreenProps } from './types';
import { useClock } from './useClock';
import { WizardPage } from './WizardPage';

/** The line above the heading: passed or not, how long the build took and what it cost. */
function ResultStatus({ passed, clock, cost, starting }: { passed: boolean; clock?: number; cost?: string; starting: boolean }) {
  if (starting) return <span className="build-status"><Working size="xs" /><b>Starting</b></span>;
  return (
    <span className="build-status">
      <StatusMark tone={passed ? 'ok' : 'err'} label={passed ? 'Passed' : 'Did not pass'} showLabel />
      {clock !== undefined && <><span className="build-sep" aria-hidden /><span>Built in <span className="num">{fmtDuration(clock / 1000)}</span></span></>}
      {cost && <><span className="build-sep" aria-hidden /><span>One-time build cost <span className="num">{cost}</span></span></>}
    </span>
  );
}

/**
 * The plan the build ran: its job and its numbers, each with a link to the Plan sheet.
 * On a build that did not pass the pass mark is the fact to look at, so it is marked.
 */
function PlanSummary({ model, models, need, noun, flagMark, onOpen }: { model: ForgeModel; models: number; need?: number; noun: string; flagMark: boolean; onOpen: () => void }) {
  const job = jobLine(model);
  const practice = practiceCount(model);
  const exam = examCount(model);
  const limits = buildLimits(model);
  const parts: ReactNode[] = [`${models} ${models === 1 ? 'model' : 'models'}`];
  if (practice !== undefined && exam !== undefined) parts.push(`${practice} practice + ${exam} exam ${casePlural(noun)}`);
  if (need !== undefined) parts.push(<span key="mark" className={clsx('plan-summary-mark', flagMark && 'is-flagged')}>pass mark {need}</span>);
  if (limits.usd !== undefined && limits.minutes !== undefined) parts.push(`up to ${fmtUsd(limits.usd)} and ${limits.minutes} min`);
  return (
    <Card className="plan-summary" aria-label="Built from this plan">
      <dl className="plan-summary-rows">
        {job && (
          <div className="plan-summary-row">
            <dt>Job</dt>
            <dd>{job}</dd>
            <Button variant="link" size="sm" data-action="view-job" onClick={onOpen}>View</Button>
          </div>
        )}
        <div className="plan-summary-row">
          <dt>Plan</dt>
          <dd>{parts.map((p, i) => <span key={i} className="plan-summary-part">{p}</span>)}</dd>
          <Button variant="link" size="sm" data-action="view-plan" onClick={onOpen}>View plan</Button>
        </div>
      </dl>
    </Card>
  );
}

/** The model the result ships, and the highest scorer that also passed (when it scored more). */
function shipAndTop(ladder: Ladder): { ship?: Rung; top?: Rung } {
  const ship = ladder.rungs.find((r) => r.ships);
  const c = ship ? rightOf(ship) : undefined;
  if (!ship || c === undefined) return { ship };
  let top: Rung | undefined;
  for (const r of ladder.rungs) {
    const s = rightOf(r);
    if (r !== ship && r.licensed && s !== undefined && s > c && (!top || s > (rightOf(top) ?? -1))) top = r;
  }
  return { ship, top };
}

export function ResultsScreen({ conv, model, ladder, sub }: ScreenProps) {
  const openForgeSheet = useApp((s) => s.openForgeSheet);
  const setWizardView = useApp((s) => s.setWizardView);
  const clock = useClock(buildWork(conv.timeline.items));
  const ready = conv.status === 'ready';
  const starting = conv.status === 'starting';
  const noun = caseNoun(model);
  const nouns = casePlural(noun);
  const need = passNeed(model);
  const cost = buildCost(model);
  const notice = noticeLine(model);
  const models = ladder?.rungs.length ?? 0;
  const n = (ladder && examSize(ladder)) ?? examCount(model);
  const plan = () => openForgeSheet('plan');
  const report = () => openForgeSheet('report');
  const changePlan = () => openForgeSheet('plan', 'next');

  const fullReport = (
    <Button key="report" variant="ghost" size="md" data-action="report" onClick={report}>
      <IconFileDescription size={16} stroke={1.5} aria-hidden />Full report
    </Button>
  );
  const changeAndRetry = (
    <Button variant="primary" size="md" data-action="change-plan" disabled={!ready} onClick={changePlan}>
      <IconPencil size={16} stroke={1.5} aria-hidden />Change the plan and retry
    </Button>
  );
  const warning = notice?.level === 'warn' ? notice.text : undefined;
  const attention = notice?.level === 'err' ? notice.text : undefined;
  const summary = (flag: boolean) => <PlanSummary model={model} models={models} need={need} noun={noun} flagMark={flag} onOpen={plan} />;

  if (sub === 'passed') {
    const { ship, top } = ladder ? shipAndTop(ladder) : {};
    const c = ship ? rightOf(ship) : undefined;
    const t = top ? rightOf(top) : undefined;
    const largest = !!top && !!ladder && ladder.rungs.at(-1) === top;
    const ratio = top && ship && top.cost !== undefined && ship.cost !== undefined ? priceRatio(top.cost, ship.cost) : undefined;
    const name = ship?.name ?? 'Your model';
    const frozen = frozenFile(model);
    const why = ladder ? whyNotLine(ladder, frozen) : undefined;
    return (
      <WizardPage
        status={<ResultStatus passed clock={clock} cost={cost} starting={starting} />}
        title={`${name} passed. It is your agent.`}
        sub={(
          <>
            {c !== undefined && n !== undefined && <>{name} got <b>{c} of {n}</b> exam {nouns} exactly right{n - c > 0 ? `; each of the other ${n - c} had at least one field off` : ''}. </>}
            {top && t !== undefined && c !== undefined && (
              <>{largest ? `The largest model, ${top.name},` : top.name} got <b>{t}</b>: {t - c} more, at {ratio && ratio !== 'a higher price' ? `${ratio} the price` : 'a higher price'}. </>
            )}
            {name} is the cheapest model that reached the pass mark{need !== undefined ? ` of ${need}` : ''}{frozen ? `, set in ${frozen}` : ''}, so it is your agent.
          </>
        )}
        warning={warning}
        attention={attention}
        bar={{
          secondary: [fullReport],
          ask: { placeholder: 'Ask Architect about the results', disabled: !ready },
          primary: (
            <Button variant="primary" size="md" data-action="try" disabled={starting} onClick={() => setWizardView(conv.id, 'try')}>
              Try your agent<IconArrowRight size={16} stroke={1.5} aria-hidden />
            </Button>
          ),
        }}
      >
        {ladder && need !== undefined && <ExamTable ladder={ladder} need={need} noun={noun} n={n} footer={why} />}
        {ladder && need !== undefined && <ExamDots ladder={ladder} need={need} noun={noun} />}
        <section className="results-plan">
          <h2 className="results-plan-title">Built from this plan</h2>
          {summary(false)}
        </section>
      </WizardPage>
    );
  }

  if (sub === 'no_pass') {
    const scored = ladder?.rungs.filter((r) => rightOf(r) !== undefined) ?? [];
    const best = scored.reduce<Rung | undefined>((a, r) => (!a || (rightOf(r) ?? -1) > (rightOf(a) ?? -1) ? r : a), undefined);
    const b = best ? rightOf(best) : undefined;
    const lines = ladder ? noPassLines(model, ladder) : [];
    const limits = buildLimits(model);
    const again = limits.usd !== undefined && limits.minutes !== undefined
      ? `A new pass mark means building again: every model takes the exam again, up to ${fmtUsd(limits.usd)} and ${limits.minutes} min.`
      : 'A new pass mark means building again: every model takes the exam again.';
    return (
      <WizardPage
        status={<ResultStatus passed={false} clock={clock} cost={cost} starting={starting} />}
        title={need !== undefined ? `No model reached the pass mark of ${need}.` : 'No model reached the pass mark.'}
        sub={(
          <>
            {best && b !== undefined && <>The best was {best.name}, with <b>{b}{n !== undefined ? ` of ${n}` : ''}</b> right. </>}
            No agent was made and nothing was added to your project. Your description and test {nouns} are kept.
          </>
        )}
        warning={warning}
        attention={attention}
        bar={{
          secondary: [fullReport],
          note: 'Try it and Add to project open once a model passes.',
          primary: changeAndRetry,
        }}
      >
        {ladder && need !== undefined && (
          <ExamTable
            ladder={ladder}
            need={need}
            noun={noun}
            n={n}
            footer={lines.length ? <><b>What you can change.</b> {lines.join(' ')} {again}</> : undefined}
          />
        )}
        <section className="results-plan">
          <h2 className="results-plan-title">Built from this plan</h2>
          {summary(true)}
        </section>
      </WizardPage>
    );
  }

  // Unscored: the exam has no score (too many invalid answers, a budget refusal).
  const shipped = typeof model.result?.data?.model === 'string' ? model.result.data.model : undefined;
  const codes = resultCodes(model);
  const why = (shipped && codes[shipped] ? refusalReason(codes[shipped]) : undefined)
    ?? refusalReason(Object.values(codes).flat());
  const reason = why ? `Not scored: ${cardWords(why)}.` : undefined;
  return (
    <WizardPage
      status={<ResultStatus passed={false} clock={clock} cost={cost} starting={starting} />}
      title="The exam could not be scored."
      sub={reason}
      warning={warning}
      attention={attention}
      bar={{ secondary: [fullReport], note: 'Try it and Add to project open once a model passes.', primary: changeAndRetry }}
    >
      {ladder && need !== undefined && <ExamTable ladder={ladder} need={need} noun={noun} n={n} />}
      <section className="results-plan">
        <h2 className="results-plan-title">Built from this plan</h2>
        {summary(true)}
      </section>
    </WizardPage>
  );
}
