/**
 * The Report sheet (redesign v2 §2.6): the technical record of a build, out of the card's
 * way. The exam (the grid with its key, who signed it, the signed numbers), the build
 * (the five stages, each with its calls and spend, and the build log folded under them),
 * what Forge did when it ran (practice evidence only: Forge never sees the exam), the
 * versions installed, the files and the hashes. Opened through the store: the card's
 * overflow, Mod+J, the plan chip on a result, and Test report on Your agents.
 */
import {
  IconCertificate, IconFile, IconFileDescription, IconFileDiff, IconHammer, IconRoute, IconTargetArrow, type Icon,
} from '@tabler/icons-react';
import clsx from 'clsx';
import { useMemo } from 'react';
import { friendlyLine, ladderView, STAGE_WORDS, type ForgeModel } from '../../lib/forge';
import { prettyModel } from '../../lib/models';
import { reportRows, rowsOf } from '../../lib/report';
import { subagentTitle } from '../../lib/timeline';
import { fmtSeconds, fmtUsd } from '../../lib/viz';
import { screenWords } from '../../lib/vocab';
import { useApp, type Conv } from '../../store/app';
import { IconButton } from '../ui/Button';
import { CardSection } from '../ui/Card';
import { ConfirmButton } from '../ui/ConfirmButton';
import { Facts } from '../ui/Facts';
import { Row, RowList } from '../ui/RowList';
import { Sheet } from '../ui/Sheet';
import { StatusMark } from '../ui/StatusMark';
import { ExamChart } from './ExamChart';
import type { AgentDetail } from './ForgeWorkspace';
import { HashLines, type HashLineProps } from './HashLine';
import { planHashes } from './PlanSheet';

const STAGE_GLYPHS: Record<number, Icon> = {
  0: IconFileDescription,
  1: IconRoute,
  2: IconTargetArrow,
  3: IconHammer,
  4: IconCertificate,
};

export function ReportSheet({ conv, model, detail, reloadDetail }: { conv: Conv; model: ForgeModel; detail: AgentDetail | null; reloadDetail: () => void }) {
  const open = useApp((s) => s.forgeSheet === 'report' && s.forgeSheetConv === conv.id);
  const openForgeSheet = useApp((s) => s.openForgeSheet);
  return (
    <Sheet open={open} onOpenChange={(o) => { if (!o) openForgeSheet(null); }} title="Report" name="report" description="The exam, the build, the files and the hashes">
      <ExamSection conv={conv} model={model} detail={detail} />
      <BuildSection conv={conv} model={model} detail={detail} />
      <ForgeSection conv={conv} detail={detail} />
      {detail && <VersionsSection conv={conv} detail={detail} reload={reloadDetail} />}
      {(detail?.files?.length ?? 0) > 0 && (
        <CardSection title="Files">
          <RowList aria-label="Files">
            {detail!.files!.map((f) => (
              <Row key={f.path} title={f.path} leading={<IconFile size={16} stroke={1.5} aria-hidden />} trailing={<span className="tabular sheet-mute">{f.size} B</span>} />
            ))}
          </RowList>
        </CardSection>
      )}
      <HashSection model={model} detail={detail} />
    </Sheet>
  );
}

function ExamSection({ conv, model, detail }: { conv: Conv; model: ForgeModel; detail: AgentDetail | null }) {
  const ladder = useMemo(() => ladderView(model, conv.forge), [model, conv.forge]);
  const report = (detail?.report ?? {}) as Record<string, unknown>;
  const rows = reportRows(report);
  if (!ladder?.exam && !detail) {
    return (
      <CardSection title="Exam">
        <p className="sheet-note">{model.result ? 'This build has no exam record.' : 'The exam comes after the build.'}</p>
      </CardSection>
    );
  }
  return (
    <CardSection title="Exam" meta={ladder?.exam ? ladder.exam.model || ladder.exam.name : undefined}>
      <div className="report-exam">
        {ladder?.exam && <ExamChart report={ladder.exam} name={ladder.exam.name} />}
        {detail && (
          <StatusMark
            tone={detail.licensed ? 'ok' : 'neutral'}
            label={detail.licensed ? 'Signed by this computer' : 'Not signed'}
            showLabel
          />
        )}
        {rows.length > 0 && <Facts columns={2} items={rows.map(([label, value]) => ({ label, value }))} />}
      </div>
    </CardSection>
  );
}

function BuildSection({ conv, model, detail }: { conv: Conv; model: ForgeModel; detail: AgentDetail | null }) {
  const feed = conv.forge.stages;
  const latest = feed.at(-1) as { model?: string; model_index?: number; model_count?: number } | undefined;
  if (!model.stages.length && !feed.length && !model.lines.length && !detail?.design) return null;
  return (
    <CardSection
      title="Build"
      meta={latest?.model_count && latest.model_count > 1 ? `Model ${latest.model_index ?? '?'} of ${latest.model_count}${latest.model ? `, ${screenWords(prettyModel(latest.model), true)}` : ''}` : undefined}
    >
      {detail?.design && <div className="sheet-prose">{detail.design}</div>}
      <ol className="report-stages">
        {[0, 1, 2, 3, 4].map((n) => {
          const stage = [...model.stages].reverse().find((s) => s.stage === n);
          const now = model.stage?.stage === n && model.running;
          const Glyph = STAGE_GLYPHS[n];
          // A spend of nothing ("$0.0000") is left out; the call count says it.
          const spent = stage?.spend && Number(stage.spend.replace(/[$,]/g, '')) > 0 ? stage.spend : '';
          const meta = [stage?.calls !== undefined ? `${stage.calls} ${stage.calls === 1 ? 'call' : 'calls'}` : '', spent].filter(Boolean).join(', ') || (stage && !now ? 'Done' : '');
          return (
            <li key={n} className={clsx('report-stage', !stage && 'is-pending', now && 'is-now')}>
              <span className="report-stage-chip"><Glyph size={14} stroke={1.5} aria-hidden /></span>
              <span className="report-stage-title">{STAGE_WORDS[n].title}</span>
              <span className="report-stage-meta tabular">{meta}</span>
            </li>
          );
        })}
      </ol>
      {model.lines.length > 0 && (
        <details className="report-log">
          <summary>Build log</summary>
          <ol className="log">
            {model.lines.map((line, i) => (
              <li key={i}><span className="log-agent">{subagentTitle(line.agent)}</span>{friendlyLine(line.text)}</li>
            ))}
          </ol>
        </details>
      )}
    </CardSection>
  );
}

interface Measurement { score?: number }

const LEFT_KEYS: Record<string, string> = { check: 'checks', dryrun: 'dryruns', measure: 'measures', submit: 'submissions', spawn: 'spawns' };
const BOARD_LABELS: Record<string, string> = { check: 'Checks', dryrun: 'Dry runs', measure: 'Measures', submit: 'Submissions', spawn: 'Sub-harnesses' };

/** Forge's record (forge.json) in the app's words; anything not listed is left out. */
const FORGE_ROWS: Array<[string, string, (v: unknown) => string]> = [
  ['agent_model', 'Engineer', (v) => prettyModel(String(v))],
  ['play_model', 'Runs on', (v) => prettyModel(String(v))],
  ['mode', 'Mode', String],
  ['accepted', 'Accepted', String],
  ['agent_cost_usd', 'Engineer cost', fmtUsd],
  ['wall_s', 'Time', fmtSeconds],
  ['harness_nodes', 'Harness nodes', String],
  ['harness_bytes', 'Harness bytes', String],
  ['verifications_for_spec', 'Exams for this spec', String],
];

/** What Forge did, only when it ran: the board as facts, its budget left, the best practice score. */
function ForgeSection({ conv, detail }: { conv: Conv; detail: AgentDetail | null }) {
  const openDock = useApp((s) => s.openDock);
  // Forge's own session only: a sub-harness's session counts in its own build.
  const all = conv.forge.tools as Array<{ tool?: string; depth?: number; result?: Record<string, unknown> }>;
  const summary = useMemo(() => {
    const tools = all.filter((t) => !t.depth);
    const counts: Record<string, number> = {};
    let left: Record<string, number> | undefined;
    let cases: Array<{ index?: number; passed?: boolean; score?: number; failure?: string }> = [];
    let best: number | undefined;
    for (const entry of tools) {
      const name = String(entry.tool ?? '').replace(/^forge_/, '');
      if (!name || name === 'session') continue;
      counts[name] = (counts[name] ?? 0) + 1;
      const result = entry.result ?? {};
      const m = (result.measurement ?? (result as Measurement)) as Measurement;
      if (result.left && typeof result.left === 'object') left = result.left as Record<string, number>;
      if (Array.isArray(result.cases)) cases = result.cases as typeof cases;
      if (result.accepted === true && typeof m.score === 'number') best = Math.max(best ?? 0, m.score);
    }
    return { counts, left, cases, best, any: tools.length > 0 };
  }, [all]);
  const record = (detail?.forge ?? {}) as Record<string, unknown>;
  const hasRecord = Object.keys(record).length > 0;
  if (!summary.any && !hasRecord) return null;
  const { counts, left, cases, best } = summary;
  const versions = conv.forge.drafts?.length ?? 0;
  const board = ['check', 'dryrun', 'measure', 'submit', 'spawn'].map((k) => {
    const remaining = left && typeof left[LEFT_KEYS[k]] === 'number' ? left[LEFT_KEYS[k]] : undefined;
    return { label: BOARD_LABELS[k], value: <span className="tabular">{counts[k] ?? 0}{remaining !== undefined ? <span className="sheet-mute">, {remaining} left</span> : null}</span> };
  });
  if (left && typeof left.usd === 'number') board.push({ label: 'Budget left', value: <span className="tabular">{fmtUsd(left.usd)}{typeof left.seconds === 'number' ? `, ${fmtSeconds(left.seconds)}` : ''}</span> });
  if (best !== undefined) board.push({ label: 'Best in practice', value: <span className="tabular">{best.toFixed(2)}</span> });
  return (
    <CardSection
      title="Forge"
      action={versions > 0 ? <IconButton icon={IconFileDiff} label="Harness versions" onClick={() => openDock('harness')} /> : undefined}
    >
      {summary.any && <Facts columns={2} items={board} />}
      {cases.length > 0 && (
        <div className="case-grid" aria-label="Practice cases in the latest measure">
          {cases.map((c, i) => (
            <span key={i} className={clsx('case-cell', c.passed ? 'is-pass' : 'is-fail')} data-tip={`Practice case ${c.index ?? i}: ${c.passed ? 'passed' : c.failure ?? 'failed'}${typeof c.score === 'number' ? ` (${c.score.toFixed(2)})` : ''}`} />
          ))}
        </div>
      )}
      {hasRecord && (
        <div className="report-forge-record">
          <h4 className="sheet-subhead">What Forge did</h4>
          <Facts columns={2} items={rowsOf(record, FORGE_ROWS).map(([label, value]) => ({ label, value }))} />
        </div>
      )}
    </CardSection>
  );
}

/** The versions installed under this name; any but the current one can be switched back to. */
function VersionsSection({ conv, detail, reload }: { conv: Conv; detail: AgentDetail; reload: () => void }) {
  const request = useApp((s) => s.request);
  const history = detail.history ?? [];
  if (!history.length) return null;
  return (
    <CardSection title="Versions">
      <RowList aria-label="Versions">
        {history.map((h, i) => (
          <Row
            key={i}
            title={<span className="tabular">v{String(h.revision ?? '?')}</span>}
            meta={<span className="tabular">{String(h.spec_hash ?? '').slice(0, 8)}</span>}
            trailing={h.current ? <StatusMark tone="ok" label="Current" showLabel /> : (
              <ConfirmButton
                label="Switch to this version"
                confirm={`Switch ${detail.name ?? 'it'} to v${String(h.revision)}`}
                onConfirm={() => void request(conv.id, 'rollback', { name: detail.name, revision: h.revision }).then(reload)}
              />
            )}
          />
        ))}
      </RowList>
    </CardSection>
  );
}

function HashSection({ model, detail }: { model: ForgeModel; detail: AgentDetail | null }) {
  const lines: HashLineProps[] = [...planHashes(model)];
  const forge = (detail?.forge ?? {}) as Record<string, unknown>;
  if (detail?.spec_hash && !lines.some((l) => l.label === 'spec')) lines.push({ label: 'spec', hash: detail.spec_hash });
  if (typeof forge.pack_sha256 === 'string' && !lines.some((l) => l.label === 'pack')) lines.push({ label: 'pack', hash: forge.pack_sha256 });
  if (!lines.length) return null;
  return (
    <CardSection title="Hashes">
      <div className="report-hashes"><HashLines lines={lines} /></div>
    </CardSection>
  );
}
