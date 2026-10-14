/**
 * Try it and the exam block, shared by the wizard's Try it step and Your agents (moved
 * here from AgentView, clarity spec section 5): run an installed agent on one input
 * (`tryRun`), the form and its output (`TryIt`, `TryOutput`), and the exam block
 * (`ResultBody`).
 */
import { useRef, useState, type ReactNode } from 'react';
import { useCopy } from '../../lib/useCopy';
import { fmtSeconds, fmtUsd, type BarData } from '../../lib/viz';
import { errorText, useApp, type Conv } from '../../store/app';
import { Button } from '../ui/Button';
import { Working } from '../ui/Working';
import { BarView } from './BarView';
import { ExamChart, type ExamReport } from './ExamChart';
import type { AgentDetail } from './ForgeWorkspace';
import { SchemaForm } from './SchemaForm';
import { useWidth } from './vizHooks';

// ---------------------------------------------------------------- the result

/**
 * The exam block, shared by the card and Your agents: the ladder frozen at its exam
 * scores, the exam grid with its key, then whatever follows (Try it), a hairline between
 * each. `of` is the exam's case count.
 */
export function ResultBody({ bar, exam, of, arrive = false, children }: { bar: BarData | null; exam: ExamReport | null; of?: number; arrive?: boolean; children?: ReactNode }) {
  const box = useRef<HTMLDivElement>(null);
  const width = useWidth(box);
  const wide = width >= 760;
  return (
    <div ref={box} className="agent-result">
      {bar && <div className="agent-result-part"><BarView data={bar} of={of ?? exam?.counts.cases} /></div>}
      {exam && (
        <div className="agent-result-part agent-exam">
          <ExamChart report={exam} arrive={arrive} cell={wide ? 12 : 10} gap={wide ? 3 : 2} />
        </div>
      )}
      {children && <div className="agent-result-part">{children}</div>}
    </div>
  );
}

// ---------------------------------------------------------------- Try it

/** What a Try it run gave back: the output with its cost, time, calls and completion id, or the one sentence a failed run gives back. */
export interface RunOutput { ok: boolean; value: unknown; cost?: number; seconds?: number; calls?: number; completion?: string }

type Request = ReturnType<typeof useApp.getState>['request'];

/**
 * Run an installed agent on one input, for Try it here and on Your agents. A run that
 * fails comes back as one sentence: the bridge's own when it sends one (a refusal by the
 * judges' relay names its limit, as the CLI does), else the app's words for its code.
 */
export async function tryRun(request: Request, conv: string, name: string, input: unknown): Promise<RunOutput> {
  try {
    const result = await request<Record<string, unknown>>(conv, 'run_agent', { name, input });
    if (result && typeof result === 'object' && 'error' in result) return { ok: false, value: result };
    return {
      ok: true,
      value: result?.output ?? result,
      cost: typeof result?.cost_usd === 'number' ? result.cost_usd : undefined,
      seconds: typeof result?.seconds === 'number' ? result.seconds : undefined,
      calls: typeof result?.calls === 'number' ? result.calls : undefined,
      completion: typeof result?.completion_id === 'string' ? result.completion_id : undefined,
    };
  } catch (err) {
    const e = err as { code?: string; detail?: string };
    return { ok: false, value: e.detail || errorText(e.code) };
  }
}

/** Try it in the card's body: the agent's input as fields with Run beside them, the output under them. */
export function TryIt({ conv, name, detail, error, label }: { conv: Conv; name: string; detail: AgentDetail | null; error?: string; label?: string }) {
  const request = useApp((s) => s.request);
  const [busy, setBusy] = useState(false);
  const [output, setOutput] = useState<RunOutput | null>(null);
  const last = useRef<unknown>(undefined);
  const run = async (input: unknown) => {
    last.current = input;
    setBusy(true);
    setOutput(null);
    try {
      setOutput(await tryRun(request, conv.id, name, input));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="try-it" aria-label="Try it">
      {detail === null && error
        ? <p className="agent-line is-err">This agent could not be opened ({error}).</p>
        : detail === null
          ? <Working size="sm" label="Loading" />
          : <SchemaForm className="try-form" schema={detail.spec?.input_schema as never} busy={busy} onlyLabel={label} onSubmit={(v) => void run(v)} />}
      {output && <TryOutput output={output} onRetry={busy ? undefined : () => void run(last.current)} />}
    </section>
  );
}

/** A Try it run's answer: the output and one line of what the run took, or the one sentence a failed run gives back. */
export function TryOutput({ output, onRetry }: { output: RunOutput; onRetry?: () => void }) {
  if (!output.ok) {
    return (
      <div className="try-output is-error">
        <p className="try-error">
          <span className="try-error-word">It could not run</span>
          {onRetry && <Button size="xs" data-action="retry" onClick={onRetry}>Retry</Button>}
        </p>
        <p className="try-error-why">{typeof output.value === 'string' ? output.value : JSON.stringify(output.value)}</p>
      </div>
    );
  }
  return (
    <div className="try-output">
      <pre className="try-json">{typeof output.value === 'string' ? output.value : JSON.stringify(output.value, null, 2)}</pre>
      <RunStats output={output} />
      {output.completion && <CompletionId id={output.completion} />}
    </div>
  );
}

/** What the run cost, how long it took and how many model calls it made: "$0.00028, 0.8s, 1 call". */
export function RunStats({ output }: { output: RunOutput }) {
  return (
    <p className="try-stats">
      {output.cost !== undefined && <span className="num" data-tip="Cost of this run">{fmtUsd(output.cost)}</span>}
      {output.seconds !== undefined && <span className="num" data-tip="Time this run took">{fmtSeconds(output.seconds)}</span>}
      {output.calls !== undefined && (
        <span data-tip="Model calls this run made"><span className="num">{output.calls}</span> {output.calls === 1 ? 'call' : 'calls'}</span>
      )}
    </p>
  );
}

/** The provider's id for the run's last completion, to find it in the provider's logs. */
export function CompletionId({ id }: { id: string }) {
  const { copied, copy } = useCopy();
  return (
    <p className="try-completion">
      <span className="try-completion-id" title={id}>{id}</span>
      <Button variant="ghost" size="xs" aria-label="Copy the completion id" onClick={() => copy(id)}>{copied ? 'Copied' : 'Copy'}</Button>
    </p>
  );
}

