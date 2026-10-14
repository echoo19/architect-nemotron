/**
 * Harness: Forge's versions of the harness it is writing, each as a diff from the one
 * before (or, for the first, the whole file), highlighted as Python. A version is what a
 * Forge tool read: Forge checks, measures and submits drafts, and every changed draft is
 * kept here in order with its practice score. Follows the newest version while Forge
 * works, until you pick an earlier one.
 */
import { IconChevronLeft, IconChevronRight, IconCircleCheck, IconFileCode, IconFileDiff } from '@tabler/icons-react';
import clsx from 'clsx';
import { useMemo, useState } from 'react';
import type { Draft } from '../../lib/forge';
import { highlight } from '../../lib/highlight';
import { diffCount, diffHunks } from '../../lib/linediff';
import type { Conv } from '../../store/app';

const TOOL_WORDS: Record<string, string> = {
  forge_check: 'checked',
  forge_dryrun: 'tried',
  forge_measure: 'measured',
  forge_submit: 'submitted',
  forge_spawn: 'used for a sub-harness',
};

export function HarnessPane({ conv }: { conv: Conv }) {
  const drafts = conv.forge.drafts ?? [];
  const [picked, setPicked] = useState<number | null>(null);
  const [whole, setWhole] = useState(false);
  const index = picked !== null && picked < drafts.length ? picked : drafts.length - 1;
  const draft = drafts[index];
  const before = index > 0 ? drafts[index - 1].text : '';

  if (!draft) {
    return (
      <div className="pane-empty harness-empty">
        <IconFileDiff size={26} stroke={1.5} aria-hidden />
        <p className="tone-faint">Forge's versions of the harness show here while it works.</p>
      </div>
    );
  }

  const go = (to: number) => setPicked(to >= drafts.length - 1 ? null : Math.max(0, to));
  return (
    <div className="harness-pane">
      <div className="harness-bar">
        <button type="button" className="icon-button" aria-label="Earlier version" data-tip="Earlier version" data-tip-side="down" disabled={index === 0} onClick={() => go(index - 1)}>
          <IconChevronLeft size={15} />
        </button>
        <span className="harness-version">
          Version <span className="num">{index + 1}</span> of <span className="num">{drafts.length}</span>
        </span>
        <button type="button" className="icon-button" aria-label="Later version" data-tip="Later version" data-tip-side="down" disabled={index === drafts.length - 1} onClick={() => go(index + 1)}>
          <IconChevronRight size={15} />
        </button>
        <Outcome draft={draft} />
        <span className="spacer" />
        {index > 0 && <Changed before={before} after={draft.text} />}
        <button
          type="button"
          className={clsx('icon-button', whole && 'is-on')}
          aria-pressed={whole}
          aria-label={whole ? 'Show the changes' : 'Show the whole file'}
          data-tip={whole ? 'Show the changes' : 'Show the whole file'}
          data-tip-side="down"
          onClick={() => setWhole(!whole)}
        >
          {whole ? <IconFileDiff size={15} /> : <IconFileCode size={15} />}
        </button>
      </div>
      <div className="harness-body change-lines">
        {whole || index === 0 ? <WholeFile text={draft.text} /> : <Diff before={before} after={draft.text} />}
      </div>
    </div>
  );
}

/**
 * What Forge did with this version: the tool, its practice score (on every practice case)
 * and acceptance. A quick measure on a few cases is not a practice score: it shows as the
 * cases it passed out of the ones it ran ("measured 7 of 8").
 */
function Outcome({ draft }: { draft: Draft }) {
  const score = draft.submitted ?? draft.measured;
  const quick = score === undefined ? draft.sampled : undefined;
  const tool = draft.submitted !== undefined ? 'forge_submit' : draft.measured !== undefined || quick ? 'forge_measure' : draft.tool;
  return (
    <span className="harness-outcome">
      <span>{TOOL_WORDS[tool] ?? 'read'}</span>
      {score !== undefined && <span className="num" data-tip="Practice score">{score.toFixed(2)}</span>}
      {quick && <QuickMeasure score={quick.score} cases={quick.cases} />}
      {draft.accepted && (
        <span className="harness-accepted" role="img" aria-label="Accepted" data-tip="Accepted: the best so far">
          <IconCircleCheck size={14} aria-hidden />
        </span>
      )}
    </span>
  );
}

/** A quick measure's result: "7 of 8" when it counts passes, else the score and its cases. */
function QuickMeasure({ score, cases }: { score: number; cases?: number }) {
  if (cases === undefined) return null;
  const passed = score * cases;
  const ran = `${cases} practice ${cases === 1 ? 'case' : 'cases'} this measure ran`;
  if (Math.abs(passed - Math.round(passed)) < 1e-6) {
    return <span data-tip={`Passed ${Math.round(passed)} of the ${ran}`}><span className="num">{Math.round(passed)}</span> of <span className="num">{cases}</span></span>;
  }
  return <span data-tip={`Scored ${score.toFixed(2)} on the ${ran}`}><span className="num">{score.toFixed(2)}</span> on <span className="num">{cases}</span></span>;
}

function Changed({ before, after }: { before: string; after: string }) {
  const { added, removed } = useMemo(() => diffCount(before, after), [before, after]);
  return <span className="num harness-count"><span className="tone-ok">+{added}</span> <span className="tone-err">-{removed}</span></span>;
}

/**
 * One line of the harness, wrapped rather than scrolled sideways: its indentation is kept
 * apart, so a long line wraps under its own start with a hanging indent.
 */
function Code({ text }: { text: string }) {
  const lead = /^[ \t]*/.exec(text)?.[0] ?? '';
  const rest = text.slice(lead.length);
  const html = highlight(rest, 'python');
  return (
    <code className={clsx('change-code is-wrapped', html !== null && 'hljs')}>
      {lead && <span className="change-lead">{lead}</span>}
      {html !== null
        ? <span className="change-rest" dangerouslySetInnerHTML={{ __html: html || ' ' }} />
        : <span className="change-rest">{rest || ' '}</span>}
    </code>
  );
}

function WholeFile({ text }: { text: string }) {
  const lines = useMemo(() => text.replace(/\r\n/g, '\n').replace(/\n$/, '').split('\n'), [text]);
  return (
    <div className="hunk">
      {lines.map((line, i) => (
        <div key={i} className="change-line is-ctx">
          <span className="change-no num">{i + 1}</span>
          <span className="change-sign" />
          <Code text={line} />
        </div>
      ))}
    </div>
  );
}

function Diff({ before, after }: { before: string; after: string }) {
  const hunks = useMemo(() => diffHunks(before, after), [before, after]);
  if (!hunks.length) return <p className="pane-note harness-same">Same as the version before.</p>;
  return (
    <>
      {hunks.map((hunk, h) => (
        <div key={h} className="hunk">
          <div className="change-line is-hunk">
            <span className="change-no" />
            <span className="change-sign" />
            <code className="change-code">{`@@ -${hunk.oldStart} +${hunk.newStart} @@`}</code>
          </div>
          {hunk.lines.map((line, i) => (
            <div key={i} className={clsx('change-line', `is-${line.kind}`)}>
              <span className="change-no num">{line.kind === 'del' ? line.oldNo : line.newNo}</span>
              <span className="change-sign">{line.kind === 'add' ? '+' : line.kind === 'del' ? '-' : ''}</span>
              <Code text={line.text} />
            </div>
          ))}
        </div>
      ))}
    </>
  );
}
