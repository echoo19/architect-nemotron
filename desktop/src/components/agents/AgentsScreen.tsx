/**
 * Everything already built: every installed agent with its status (one broken
 * entry never hides the rest), its versions, a place to try it, and what it is
 * made of; plus the harness library, usage over time and the local HTTP service.
 */
import { IconBox, IconFile, IconHammer, IconPlayerPause, IconRefresh } from '@tabler/icons-react';
import clsx from 'clsx';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { prettyModel, rungName } from '../../lib/models';
import { reportRows } from '../../lib/report';
import { AS_BUILT, stoppedRow } from '../../lib/retake';
import { fmtUsd, type BarData, type ExamCounts } from '../../lib/viz';
import { useApp, type Conv } from '../../store/app';
import { ResultBody, TryIt } from '../forge/try';
import type { ExamReport } from '../forge/ExamChart';
import type { AgentDetail } from '../forge/ForgeWorkspace';
import { Button, IconButton } from '../ui/Button';
import { Card, CardBody, CardFooter, CardHeader, CardLead } from '../ui/Card';
import { ConfirmButton } from '../ui/ConfirmButton';
import { Facts, type Fact } from '../ui/Facts';
import { Field, Input } from '../ui/Field';
import { PageHeader } from '../ui/PageHeader';
import { Row, RowList } from '../ui/RowList';
import { Section } from '../ui/Section';
import { Segmented } from '../ui/Segmented';
import { StatusMark } from '../ui/StatusMark';
import { WorkingRegion } from '../ui/Working';
import { RetakePanel, useRetake } from './RetakePanel';

export interface AgentRow {
  name: string;
  revision?: number;
  spec_hash?: string;
  ok: boolean;
  error?: string;
  licensed?: boolean;
  floor_score?: number;
  floor_limit?: number;
  floor_kind?: string;
  task?: string;
  /** Why the agent's folder in the project refuses calls: its files changed after its exam. */
  stopped?: string;
  /** The score of the version in the project, when that passed a retake: what the agent's page shows. */
  placed?: { right: number; total: number; pass_mark: number | null; attempt: number };
}

type Tab = 'agents' | 'library' | 'usage' | 'service';

const TABS: Array<{ value: Tab; label: string }> = [
  { value: 'agents', label: 'Installed' },
  { value: 'library', label: 'Library' },
  { value: 'usage', label: 'Usage' },
  { value: 'service', label: 'Service' },
];

export interface Captured { kind: string; title?: string; rows?: Array<[string, string]>; text?: string; level?: string }

/** A conversation process to ask, preferring the one on screen. */
export function useHost(): Conv | undefined {
  return useApp((s) => {
    const active = s.prefs.active ? s.convs[s.prefs.active] : undefined;
    if (active?.status === 'ready') return active;
    return Object.values(s.convs).find((c) => c.status === 'ready');
  });
}

export function useCapture(host: Conv | undefined, command: string, deps: unknown[] = []) {
  const request = useApp((s) => s.request);
  const [events, setEvents] = useState<Captured[] | null>(null);
  const [error, setError] = useState('');
  const run = useCallback(async () => {
    if (!host) return;
    setEvents(null);
    try {
      const result = await request<{ events: Captured[] }>(host.id, 'capture', { text: command });
      setEvents(result.events);
      setError('');
    } catch (err) {
      setError((err as { code?: string }).code ?? 'failed');
      setEvents([]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [host?.id, command, request, ...deps]);
  useEffect(() => {
    void run();
  }, [run]);
  return { events, error, run };
}

export function CapturedView({ events, empty }: { events: Captured[] | null; empty: string }) {
  if (events === null) return <WorkingRegion label="Loading" />;
  if (!events.length) return empty ? <p className="screen-note">{empty}</p> : null;
  return (
    <div className="captured">
      {events.map((e, i) => {
        if (e.kind === 'rows' || e.kind === 'plan_card') {
          return (
            <section key={i} className="captured-rows">
              {e.title && <h3>{e.title}</h3>}
              {(e.rows ?? []).length === 0 ? <p className="screen-note">Nothing yet.</p> : (
                <dl className="kv">
                  {e.rows!.map(([k, v], j) => <div key={j} className="kv-row"><dt>{k}</dt><dd>{v}</dd></div>)}
                </dl>
              )}
            </section>
          );
        }
        if (e.kind === 'notice') return <p key={i} className={clsx('screen-note', e.level === 'error' ? 'tone-err' : e.level === 'warn' ? 'tone-warn' : '')}>{e.text}</p>;
        return <pre key={i} className="text-block">{e.text}</pre>;
      })}
    </div>
  );
}

export function AgentsScreen() {
  const host = useHost();
  const [tab, setTab] = useState<Tab>('agents');
  return (
    <section className="screen">
      <div className="screen-head drag">
        <PageHeader
          title="Your agents"
          actions={<Segmented kind="tabs" label="Your agents" size="sm" className="no-drag" options={TABS} value={tab} onChange={(v) => setTab(v as Tab)} />}
        />
      </div>
      {!host ? (
        <div className="screen-empty"><p className="screen-note">Open a conversation first. This screen asks it for your agents.</p></div>
      ) : tab === 'agents' ? (
        <Installed host={host} />
      ) : tab === 'library' ? (
        <Simple host={host} title="Library" command="/library" empty="The library is empty. It fills as agents are built." intro="Designs that worked before. New builds start from the closest one." />
      ) : tab === 'usage' ? (
        <Simple host={host} title="Usage" command="/telemetry" empty="No runs recorded yet." intro="Cost and quality of every agent run, by period." />
      ) : (
        <Service host={host} />
      )}
    </section>
  );
}

function Simple({ host, title, command, empty, intro }: { host: Conv; title: string; command: string; empty: string; intro: string }) {
  const { events, run } = useCapture(host, command);
  return (
    <div className="screen-body">
      <Card>
        <CardHeader action={<IconButton icon={IconRefresh} label="Refresh" onClick={() => void run()} />}>{title}</CardHeader>
        <CardBody>
          <p className="screen-note">{intro}</p>
          <CapturedView events={events} empty={empty} />
        </CardBody>
      </Card>
    </div>
  );
}

function Service({ host }: { host: Conv }) {
  const request = useApp((s) => s.request);
  const [port, setPort] = useState('8765');
  const [events, setEvents] = useState<Captured[] | null>([]);
  const act = async (text: string) => {
    setEvents(null);
    try {
      const result = await request<{ events: Captured[] }>(host.id, 'capture', { text });
      setEvents(result.events);
    } catch (err) {
      setEvents([{ kind: 'notice', level: 'error', text: `That did not work (${(err as { code?: string }).code ?? 'failed'}).` }]);
    }
  };
  return (
    <div className="screen-body">
      <Card>
        <CardHeader>Service</CardHeader>
        <CardBody>
          <p className="screen-note">Other programs on this computer can call your agents over HTTP. It stops when Architect closes.</p>
          <div className="service-row">
            <Field label="Port" htmlFor="service-port" className="service-port">
              <Input id="service-port" size="sm" className="num" value={port} onChange={(e) => setPort(e.target.value.replace(/\D/g, ''))} />
            </Field>
            <Button size="sm" onClick={() => void act('/serve stop')}>Stop</Button>
            <Button variant="ghost" size="sm" onClick={() => void act('/serve')}>Status</Button>
          </div>
          <CapturedView events={events} empty="" />
        </CardBody>
        <CardFooter>
          <Button variant="primary" size="sm" data-action="serve" onClick={() => void act(`/serve ${port}`)}>Start</Button>
        </CardFooter>
      </Card>
    </div>
  );
}

/** "0.51" as the exam score, two places, as the terminal prints it. */
const two = (n: number) => n.toFixed(2);

function Installed({ host }: { host: Conv }) {
  const request = useApp((s) => s.request);
  const [rows, setRows] = useState<AgentRow[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState('');
  const load = useCallback(async () => {
    try {
      const result = await request<{ agents: AgentRow[] }>(host.id, 'agents');
      setRows(result.agents);
      setError('');
      setSelected((s) => s ?? result.agents[0]?.name ?? null);
    } catch (err) {
      setError((err as { code?: string }).code ?? 'failed');
      setRows([]);
    }
  }, [host.id, request]);
  useEffect(() => {
    void load();
  }, [load]);

  if (rows === null) return <WorkingRegion label="Loading your agents" />;
  if (!rows.length) {
    return (
      <div className="screen-empty">
        <IconBox size={24} stroke={1.5} className="screen-empty-glyph" aria-hidden />
        <h2>No agents yet</h2>
        <p className="screen-note">{error ? `The list could not be read (${error}).` : 'Build one and it is listed here, ready to run.'}</p>
        <Button variant="primary" size="sm" onClick={() => useApp.getState().openConversation({ cwd: host.state.cwd ?? '', kind: 'forge' })}>
          <IconHammer size={16} stroke={1.5} aria-hidden /> Build an agent
        </Button>
      </div>
    );
  }
  return (
    <div className="agents-layout">
      <div className="agents-list">
        <RowList aria-label="Installed agents">
          {rows.map((row) => (
            <Row
              key={row.name}
              id={row.name}
              selected={selected === row.name}
              onSelect={() => setSelected(row.name)}
              leading={<StatusMark tone={!row.ok || row.stopped ? 'err' : row.licensed === false ? 'err' : 'ok'} icon={row.ok && row.stopped ? IconPlayerPause : undefined} label={rowStatus(row)} />}
              title={<>{row.name}{row.revision !== undefined && <span className="agent-row-rev num"> v{row.revision}</span>}</>}
              meta={!row.ok || row.stopped ? rowStatus(row) : row.placed
                ? <span className="num">{row.placed.right} / {row.placed.total}{row.placed.pass_mark != null ? `, pass mark ${row.placed.pass_mark}` : ''}</span>
                : row.floor_score !== undefined
                  ? <span className="num">{Math.round(row.floor_score * 100)} / 100{row.floor_limit !== undefined ? `, pass mark ${Math.round(row.floor_limit * 100)}` : ''}</span>
                  : undefined}
            />
          ))}
        </RowList>
      </div>
      <div className="agents-detail">
        {selected ? <AgentPage key={selected} host={host} name={selected} row={rows.find((r) => r.name === selected)} onChanged={load} /> : null}
      </div>
    </div>
  );
}

function rowStatus(row: AgentRow): string {
  if (!row.ok) return row.error === 'unlicensed' ? 'License check failed' : `Cannot load (${row.error})`;
  // The agent in the project stopped; the copy Architect keeps is as it was.
  const stopped = stoppedRow(row.stopped);
  if (stopped) return stopped;
  return row.licensed === false ? 'Did not pass' : 'Passed';
}

/** The signed report's exam counts (`held_out_counts`), in the shape the exam grid reads. */
function examCounts(value: unknown): ExamCounts | undefined {
  const c = value && typeof value === 'object' ? value as Record<string, unknown> : undefined;
  const n = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : undefined);
  const cases = n(c?.cases);
  if (!c || cases === undefined) return undefined;
  return { cases, correct: n(c.correct) ?? null, invalid: n(c.invalid) ?? 0, overBudget: n(c.over_budget) ?? 0 };
}

/** The installed agent as one rung and its exam: what the build's card shows at its result. */
function examOf(detail: AgentDetail, score: number | undefined, limit: number | undefined): { bar: BarData | null; exam: ExamReport | null; counts?: ExamCounts } {
  if (limit === undefined) return { bar: null, exam: null };
  const report = (detail.report ?? {}) as Record<string, unknown>;
  const counts = examCounts(report.held_out_counts);
  const id = typeof detail.model === 'string' ? detail.model : '';
  const licensed = detail.licensed === true;
  const bar: BarData | null = score === undefined ? null : {
    bar: limit,
    rungs: [{
      key: id || 'agent', name: id ? rungName(id) : 'This agent', model: id ? prettyModel(id) : 'This agent', state: 'done',
      exam: { score, counts: counts ?? null }, licensed, ships: licensed,
    }],
  };
  return { bar, exam: counts ? { counts, bar: limit, licensed } : null, counts };
}

function AgentPage({ host, name, row, onChanged }: { host: Conv; name: string; row?: AgentRow; onChanged: () => void }) {
  const request = useApp((s) => s.request);
  const send = useApp((s) => s.send);
  const activate = useApp((s) => s.activate);
  const [detail, setDetail] = useState<AgentDetail | null>(null);
  const [error, setError] = useState('');
  const { view: retake, setView: setRetake } = useRetake(host.id, name);
  const tryBox = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    try {
      setDetail(await request<AgentDetail>(host.id, 'agent', { name }));
    } catch (err) {
      setError((err as { code?: string }).code ?? 'failed');
    }
  }, [host.id, name, request]);
  useEffect(() => {
    void load();
  }, [load]);

  const report = (detail?.report ?? {}) as Record<string, unknown>;
  const score = typeof report.floor_score === 'number' ? report.floor_score : row?.floor_score;
  const limit = typeof report.floor_limit === 'number' ? report.floor_limit : row?.floor_limit;
  const history = useMemo(() => detail?.history ?? [], [detail]);
  const reported = reportRows(report);
  // One object per load, so the ladder and the grid do not lay out again on every render.
  const { bar, exam, counts } = useMemo(() => (detail ? examOf(detail, score, limit) : { bar: null, exam: null, counts: undefined }), [detail, score, limit]);

  const inConversation = (text: string) => {
    void send(host.id, text);
    activate(host.id);
  };

  if (error) return <p className="screen-note tone-err">This agent could not be read ({error}).</p>;
  if (!detail) return <WorkingRegion label="Loading" />;
  const passed = detail.licensed === true;
  const task = String(detail.spec?.task ?? row?.task ?? '');
  const facts: Fact[] = [
    ...(task ? [{ value: task, label: 'Job', wide: true }] : []),
    ...(report.held_out_count !== undefined ? [{ value: <span className="num">{String(report.held_out_count)}</span>, label: 'Exam cases' }] : []),
    // As the build card says it: "40 of 100" when the exam's size is known.
    ...(limit !== undefined ? [{ value: <span className="num">{counts ? `${Math.round(limit * counts.cases)} of ${counts.cases}` : two(limit)}</span>, label: 'Pass mark' }] : []),
    ...(typeof report.mean_cost_usd === 'number' ? [{ value: <span className="num">{fmtUsd(report.mean_cost_usd)}</span>, label: 'Cost per run' }] : []),
  ];
  return (
    <div className="agent-page">
      {retake && (
        <RetakePanel
          convId={host.id}
          name={name}
          view={retake}
          onView={(reply) => { setRetake(reply); onChanged(); }}
          outputSchema={detail.spec?.output_schema}
          onTry={passed ? () => tryBox.current?.querySelector<HTMLElement>('textarea, input')?.focus() : undefined}
          onReport={() => inConversation(`/report ${name}`)}
        />
      )}
      <Card data-phase={passed ? 'licensed' : 'not_licensed'}>
        {retake ? (
          // The panel above gives the verdict and the score of the agent in the project.
          // This card is the build's own record, so it carries neither a second time.
          <CardHeader as="h2"><span className="agent-page-name">{AS_BUILT}</span></CardHeader>
        ) : (
          <CardHeader
            as="h2"
            meta={(
              <>
                <StatusMark tone={passed ? 'ok' : 'err'} label={passed ? 'Passed' : 'Did not pass'} showLabel />
                {row?.revision !== undefined && <span className="num">v{row.revision}</span>}
              </>
            )}
            lead={score !== undefined && (
              <CardLead
                value={<span className="num">{counts && counts.correct !== null ? `${counts.correct} / ${counts.cases}` : two(score)}</span>}
                note={passed ? (typeof detail.model === 'string' && detail.model ? `${rungName(detail.model)} ships` : undefined) : 'Nothing shipped'}
                tone={passed ? 'neutral' : 'err'}
              />
            )}
          >
            <span className="agent-page-name">{name}</span>
          </CardHeader>
        )}
        <CardBody>
          <div className="agent-page-body">
            <Facts items={facts} />
            <ResultBody bar={bar} exam={exam} of={counts?.cases}>
              {passed && <div ref={tryBox}><TryIt conv={host} name={name} detail={detail} /></div>}
            </ResultBody>
          </div>
        </CardBody>
        <CardFooter
          start={(
            <span className="agent-page-more">
              <Button variant="ghost" size="sm" onClick={() => inConversation(`/recompile ${name}`)}>Rebuild</Button>
              <Button variant="ghost" size="sm" onClick={() => inConversation(`/report ${name}`)}>Full report</Button>
            </span>
          )}
        >
          <Button variant="primary" size="sm" data-action="add" onClick={() => inConversation(`/export ${name}`)}>Add to project</Button>
        </CardFooter>
      </Card>
      {detail.design && (
        <Section title="How it works">
          <pre className="design">{detail.design}</pre>
        </Section>
      )}
      <Section title="Takes and gives back" defaultOpen={false}>
        <div className="schema-pair">
          <div><h3>Takes</h3><pre className="text-block">{JSON.stringify(detail.spec?.input_schema ?? {}, null, 2)}</pre></div>
          <div><h3>Gives back</h3><pre className="text-block">{JSON.stringify(detail.spec?.output_schema ?? {}, null, 2)}</pre></div>
        </div>
      </Section>
      {history.length > 0 && (
        <Section title="Versions" defaultOpen={false}>
          <RowList aria-label="Versions">
            {history.map((h, i) => (
              <Row
                key={i}
                title={<span className="num">v{String(h.revision ?? '?')}</span>}
                meta={<span className="num">{String(h.spec_hash ?? '').slice(0, 8)}</span>}
                trailing={h.current ? <StatusMark tone="ok" label="Current" showLabel /> : (
                  <ConfirmButton
                    label="Switch to this version"
                    confirm={`Switch to v${String(h.revision)}`}
                    onConfirm={() => void request(host.id, 'rollback', { name, revision: h.revision }).then(() => { void load(); onChanged(); })}
                  />
                )}
              />
            ))}
          </RowList>
        </Section>
      )}
      {reported.length > 0 && (
        // The same rows as the build's report, in the app's words: exam score, bar, exam cases.
        <Section title="Test report" defaultOpen={false}>
          <div className="agent-report">
            <Facts items={reported.map(([label, value]) => ({ label, value: <span className="num">{value}</span> }))} />
          </div>
        </Section>
      )}
      {(detail.files?.length ?? 0) > 0 && (
        <Section title="Files" defaultOpen={false}>
          <RowList aria-label="Files">
            {detail.files!.map((f) => (
              <Row key={f.path} leading={<IconFile size={16} stroke={1.5} aria-hidden />} title={f.path} trailing={<span className="num">{f.size} B</span>} />
            ))}
          </RowList>
        </Section>
      )}
    </div>
  );
}
