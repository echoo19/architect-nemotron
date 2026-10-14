/**
 * The ladder on the agent card (redesign v2 §3.5): one row per model, drawn once that
 * model starts, the pass mark as one pine rule through every row, and once the build
 * returns each model's exam score as a ring on its track with the count in a right
 * column and a lock beside it. Three states: the model that ships (solid ring, closed
 * lock, ink), one that passed but does not ship (open pine ring, closed lock, muted),
 * one that did not pass (open grey ring, open lock, faint). Lectern's
 * working mark waits at the start of the track while a model is being worked on.
 *
 * `showForge` (the Report) adds what the card leaves out: the best preset's practice
 * score as a grey tick, Forge's practice scores as pine dots, and the stem from the
 * practice score that sat the exam to its ring. `all` draws every model, started or not.
 * Geometry comes from lib/viz.ts, colour from styles/viz.css.
 */
import { IconLock, IconLockOpen } from '@tabler/icons-react';
import clsx from 'clsx';
import { useMemo, useRef } from 'react';
import { screenWords } from '../../lib/vocab';
import { barViewSummary, examScore, finite, fmtScore, layoutBarView, ringTip, startedRung, type BarData, type BarRow } from '../../lib/viz';
import { BOARD, FOOT, INK_BOX, MARK_OUTLINE, WORKING_COLUMN } from '../ui/Working';
import { useEntrances, useVizTip, useWidth, VizTip } from './vizHooks';

/** Lectern's working mark inside the chart, at the start of the running model's track (ui/Working.tsx's geometry and order). */
function WorkingAt({ x, y, height }: { x: number; y: number; height: number }) {
  const w = (height * 50) / 66;
  return (
    <svg className="viz-working lectern-working" x={x} y={y} width={w} height={height} viewBox={INK_BOX} fill="currentColor" aria-hidden>
      <path className="lectern-working-board" d={BOARD} />
      <path className="lectern-working-foot" d={FOOT} />
      <path className="lectern-working-column" d={WORKING_COLUMN} />
      <path className="lectern-working-still" d={MARK_OUTLINE} />
    </svg>
  );
}

const ROW = 36;
const RING = 8;
const LOCK = 14;
const TOP = 6;

/** How many cases a score is, out of `of`: the signed count when the exam has one. */
function countOf(row: BarRow, of?: number): string {
  const ring = row.ring!;
  const correct = ring.exam?.counts?.correct;
  if (finite(correct)) return String(correct);
  return String(Math.round(ring.score * (of ?? 100)));
}

/** The pass mark as cases: "40" of 100. */
export function passCount(bar: number, of?: number): string {
  return String(Math.ceil(bar * (of ?? 100) - 1e-9));
}

export function BarView({ data, of, showForge = false, all = false }: { data: BarData; of?: number; showForge?: boolean; all?: boolean }) {
  const box = useRef<HTMLElement>(null);
  const width = useWidth(box);
  const L = useMemo(
    () => (width > 0 ? layoutBarView(data, {
      width, rowHeight: ROW, top: TOP, bottom: 26, valueWidth: 24 + LOCK + 8, ringRadius: RING, labelWidth: width < 440 ? 76 : 96,
      rows: all ? undefined : startedRung,
    }) : null),
    [data, width, all],
  );
  const keys = L ? L.rows.flatMap((row) => [`${row.key}:row`, ...(row.ring ? [`${row.key}:ring`] : []), ...(showForge ? row.dots.map((d) => `${row.key}:dot:${d.index}`) : [])]) : null;
  const entering = useEntrances(keys);
  const { tip, handlers } = useVizTip(box);
  const enter = (key: string, name = 'viz-enter') => (entering.has(key) ? ` ${name}` : '');
  // A batch of rings arriving together (the build returned) lands one model at a time.
  const delay = (key: string) => (entering.has(key) && key.endsWith(':ring') ? { animationDelay: `${entering.get(key)! * 140}ms` } : undefined);
  const mark = passCount(data.bar, of);

  return (
    <figure ref={box} className="viz viz-barview" {...handlers}>
      {L && L.rows.length > 0 && (
        <svg className="viz-svg" width={L.width} height={L.height} viewBox={`0 0 ${L.width} ${L.height}`} role="group" aria-label={screenWords(barViewSummary(data), true)}>
          {L.rows.map((row) => (
            <g key={row.key} className={clsx('viz-row', `is-${row.state}`, row.ring?.ships && 'is-ships', enter(`${row.key}:row`, 'viz-row-enter'))}>
              <line className="viz-track" x1={row.track.x1} x2={row.track.x2} y1={row.track.y} y2={row.track.y} />
              <text className="viz-name" x={L.nameX} y={Math.round((row.y + 4.5) * 10) / 10} data-viz-tip={nameTip(row)}>{fitName(row.name, L.plotX0 - 10)}</text>
              {row.state === 'running' && <WorkingAt x={L.plotX0} y={row.y - 7} height={14} />}
            </g>
          ))}
          {/* The rule sits under the marks so rings and dots cut it with their halos. */}
          <line className="viz-rule" x1={L.rule.x} x2={L.rule.x} y1={L.rule.y1 + 4} y2={L.rule.y2 + 4} data-viz-tip={`Pass mark: ${mark} correct`} />
          {L.rows.map((row) => (
            <g key={row.key}>
              {showForge && row.link && <line className={clsx('viz-link', `is-${row.link.from}`, enter(`${row.key}:ring`))} style={delay(`${row.key}:ring`)} x1={row.link.x1} x2={row.link.x2} y1={row.y} y2={row.y} />}
              {showForge && row.tick && <line className="viz-tick" x1={row.tick.x} x2={row.tick.x} y1={row.tick.y1} y2={row.tick.y2} data-viz-tip={`Best starting point: practice ${fmtScore(row.tick.value)}`} />}
              {showForge && row.dots.map((dot) => (
                <circle
                  key={dot.index}
                  className={clsx('viz-dot', dot.best ? 'is-best' : `is-${dot.age}`, enter(`${row.key}:dot:${dot.index}`))}
                  cx={dot.cx} cy={dot.cy} r={dot.best ? 4.5 : 3}
                  data-viz-tip={`Practice score ${dot.index + 1} of ${dot.count}: ${fmtScore(dot.value)}`}
                />
              ))}
              {row.ring && <Ring row={row} className={enter(`${row.key}:ring`)} style={delay(`${row.key}:ring`)} />}
              {row.ring && (
                <g className={clsx('viz-result', enter(`${row.key}:ring`))} style={delay(`${row.key}:ring`)}>
                  <text className={clsx('viz-value', row.ring.ships ? 'is-ships' : !row.ring.licensed && 'is-refused')} x={L.width - LOCK - 8} y={Math.round((row.y + 4.5) * 10) / 10} textAnchor="end">{countOf(row, of)}</text>
                  {row.ring.licensed || row.ring.ships
                    ? <IconLock className={clsx('viz-lock is-closed', row.ring.ships && 'is-ships')} x={L.width - LOCK} y={row.y - LOCK / 2} width={LOCK} height={LOCK} stroke={1.75} aria-hidden />
                    : <IconLockOpen className="viz-lock is-open" x={L.width - LOCK} y={row.y - LOCK / 2} width={LOCK} height={LOCK} stroke={1.75} aria-hidden />}
                </g>
              )}
              {!row.ring && row.value?.unscored && (
                <text className="viz-value is-unscored" x={L.width} y={Math.round((row.y + 4.5) * 10) / 10} textAnchor="end" data-viz-tip={row.value.tip}>not scored</text>
              )}
            </g>
          ))}
          <text className="viz-rule-label" x={L.rule.x + 8} y={L.rule.y2 + 18} textAnchor={L.rule.x > L.width - 120 ? 'end' : 'start'}>
            pass mark <tspan className="viz-num">{mark}</tspan>
          </text>
        </svg>
      )}
      <BarTable data={data} of={of} />
      <VizTip tip={tip} />
    </figure>
  );
}

function Ring({ row, className, style }: { row: BarRow; className: string; style?: React.CSSProperties }) {
  const ring = row.ring!;
  const tip = ringTip(row.model === row.name ? row.name : row.model, ring);
  return (
    <g className={clsx('viz-ring-wrap', className)} style={style}>
      <g
        className={clsx('viz-ring', ring.licensed ? 'is-licensed' : 'is-refused', ring.ships && 'is-ships')}
        transform={`translate(${ring.cx} ${ring.cy})`}
        tabIndex={0}
        role="img"
        aria-label={tip}
        data-viz-tip={screenWords(tip)}
      >
        <circle className="viz-hit" r={14} />
        <circle className="viz-halo" r={ring.r + 2.5} />
        <circle className="viz-ring-shape" r={ring.r} />
      </g>
    </g>
  );
}

function nameTip(row: BarRow): string | undefined {
  const parts = [row.model !== row.name ? row.model : ''];
  if (row.state === 'running') parts.push('working on it now');
  else if (row.state === 'done' && !row.ring && !row.value) parts.push('its exam result comes when the build finishes');
  else if (row.state === 'skipped') parts.push('no exam');
  const text = parts.filter(Boolean).join(', ');
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : undefined;
}

/** A name that fits its column, cut with an ellipsis (the tip carries the whole name). */
function fitName(name: string, room: number): string {
  const max = Math.max(4, Math.floor(room / 7.2));
  return name.length > max ? `${name.slice(0, max - 1)}…` : name;
}

/** The same facts as a table, for screen readers, in the card's words. */
function BarTable({ data, of }: { data: BarData; of?: number }) {
  return (
    <div className="sr-only">
      <table className="viz-table">
        <caption>Pass mark {passCount(data.bar, of)} correct</caption>
        <thead>
          <tr><th scope="col">Model</th><th scope="col">Exam</th><th scope="col">Result</th></tr>
        </thead>
        <tbody>
          {data.rungs.map((r) => {
            const s = examScore(r.exam);
            const c = r.exam?.counts;
            const scored = r.state === 'done' && finite(s);
            const exam = scored ? (c && finite(c.correct) ? `${c.correct} of ${c.cases} correct` : `${Math.round((s as number) * 100)}%`) : r.state === 'done' && c ? 'not scored' : r.state === 'running' ? 'working on it' : 'not yet';
            const verdict = scored ? `${r.licensed ? 'passed' : 'did not pass'}${r.ships ? ', ships' : ''}` : r.state === 'done' && c ? 'did not pass' : '';
            return (
              <tr key={r.key}>
                <th scope="row">{r.model}</th>
                <td>{exam}</td>
                <td>{verdict}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
