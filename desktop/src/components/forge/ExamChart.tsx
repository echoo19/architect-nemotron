/**
 * The exam grid (site/design/SPEC.md, "Exam chart"; redesign v2 §3.5): a unit chart of a
 * signed report's counts, ten to a row, correct cases solid, misses outlined, invalid
 * outputs dotted. Beside it, only the key ("64 correct", "36 missed"); the score itself is
 * the card's lead, and the pass mark is the ladder's (one device per fact), so the grid
 * draws no rule of its own (its tip still says how many it needs), except on the guided
 * build's dot card, which asks for it (`passLine`).
 * It knows only the counts: no per-case exam data leaves verify(), so units are grouped by
 * kind and never say which case was which. It arrives once, after the build returns, and
 * fades in as one piece.
 */
import clsx from 'clsx';
import { useMemo, useRef } from 'react';
import { dotted, examSummary, layoutExam, type ExamCounts, type UnitKind } from '../../lib/viz';
import { screenWords } from '../../lib/vocab';
import { useVizTip, VizTip } from './vizHooks';

export interface ExamReport {
  counts: ExamCounts;
  bar: number;
  licensed?: boolean;
}

function UnitGlyph({ kind, size = 10 }: { kind: UnitKind; size?: number }) {
  const r = size / 2;
  const ring = kind === 'invalid' || kind === 'over';
  const radius = kind === 'correct' ? r : ring ? r - 0.9 : r - 0.75;
  return (
    <svg width={size} height={size} viewBox={`${-r} ${-r} ${size} ${size}`} aria-hidden>
      <circle className={`viz-unit is-${kind}`} r={radius} strokeDasharray={ring ? dotted(r - 0.9, 3) : undefined} />
    </svg>
  );
}

/**
 * `report` is the signed report's counts and the bar; nothing else is read from it, so
 * the chart cannot draw anything but counts. `arrive` fades it in once (the report just
 * came in); a page load or a replayed build draws it at rest. `cell` and `gap` size the
 * units: 12 and 3 on a wide card, 10 and 2 on a narrow one. `passLine` (the guided build's
 * dot card) draws the pass mark on the grid as a dashed line after the cells that must be
 * solid, labelled on the right, and leaves the key to the card's header.
 */
export function ExamChart({ report, arrive = false, cell = 12, gap = 3, passLine = false }: { report: ExamReport; name?: string; arrive?: boolean; cell?: number; gap?: number; passLine?: boolean }) {
  const box = useRef<HTMLElement>(null);
  const counts = report.counts;
  const L = useMemo(
    () => layoutExam({ counts: { cases: counts.cases, correct: counts.correct, invalid: counts.invalid, overBudget: counts.overBudget }, bar: report.bar }, { cell, gap, labelWidth: 0, split: passLine ? 8 : 0 }),
    [counts.cases, counts.correct, counts.invalid, counts.overBudget, report.bar, cell, gap, passLine],
  );
  const { tip, handlers } = useVizTip(box);
  const c = L.counts;
  const rule = passLine ? L.rule : null;
  const width = rule ? L.width + 96 : L.width;
  const groups = new Map<UnitKind, typeof L.cells>();
  for (const unit of L.cells) groups.set(unit.kind, [...(groups.get(unit.kind) ?? []), unit]);
  const tips: Record<UnitKind, string> = {
    correct: `${c.correct} correct`, missed: `${c.missed} missed`, invalid: `${c.invalid} invalid`,
    over: `${c.overBudget} over budget`, unscored: 'not scored',
  };
  const key = (kind: UnitKind, count: number | null, word: string) => (
    <li className={`is-${kind}`}>
      <UnitGlyph kind={kind} />
      {count !== null && <span className="num">{count}</span>}
      <span>{word}</span>
    </li>
  );

  return (
    <figure ref={box} className={clsx('viz viz-exam', arrive && 'viz-arrive')} {...handlers}>
      <svg className="viz-svg" width={width} height={L.height} viewBox={`0 0 ${width} ${L.height}`} role="img" aria-label={screenWords(examSummary({ counts, bar: report.bar, licensed: report.licensed }))} data-viz-tip={L.need ? `Pass mark: ${L.need} of ${L.n} correct` : undefined}>
        {[...groups.entries()].map(([kind, units]) => (
          <g key={kind} className={`viz-units is-${kind}`} data-viz-tip={`${tips[kind]} of ${L.n}`}>
            {units.map((unit, i) => (kind === 'correct'
              ? <circle key={i} className="viz-unit is-correct" cx={unit.cx} cy={unit.cy} r={L.cell / 2} />
              : kind === 'missed' || kind === 'unscored'
                ? <circle key={i} className={`viz-unit is-${kind}`} cx={unit.cx} cy={unit.cy} r={L.cell / 2 - 0.75} />
                : <circle key={i} className={`viz-unit is-${kind}`} cx={unit.cx} cy={unit.cy} r={L.cell / 2 - 0.9} strokeDasharray={dotted(L.cell / 2 - 0.9, 3)} />))}
          </g>
        ))}
        {rule && (
          <g className="viz-pass-line">
            <path className="viz-pass-rule" d={rule.d} />
            <text className="viz-pass-label" x={L.width + 4} y={rule.labelY + 4}>Pass mark {L.need}</text>
          </g>
        )}
      </svg>
      {!passLine && <ul className="viz-exam-key">
        {c.scored && key('correct', c.correct, 'correct')}
        {c.scored && key('missed', c.missed, 'missed')}
        {c.invalid > 0 && key('invalid', c.invalid, 'invalid')}
        {c.overBudget > 0 && key('over', c.overBudget, 'over budget')}
        {!c.scored && key('unscored', null, 'not scored')}
      </ul>}
      <VizTip tip={tip} />
    </figure>
  );
}
