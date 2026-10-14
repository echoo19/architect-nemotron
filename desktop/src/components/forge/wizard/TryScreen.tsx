/**
 * Step 5, Try it (clarity spec section 3, mockups 5 and 5b): paste one case, run the
 * agent on it exactly as the app will (`tryRun`, the installed agent), and read its
 * answer field by field. The run's cost and time are the bridge's own; the exam line is
 * the result card's count. Optional: Skip goes straight to Add to project.
 */
import { IconArrowRight, IconPlayerPlay, IconUpload } from '@tabler/icons-react';
import { useId, useMemo, useRef, useState } from 'react';
import { fmtSeconds, fmtUsd } from '../../../lib/viz';
import { resultLead, takesLabel } from '../../../lib/vocab';
import { caseNoun, joinAnd, outputFields, schemaFields, type SchemaField } from '../../../lib/wizard';
import { useApp } from '../../../store/app';
import { Button } from '../../ui/Button';
import { Card } from '../../ui/Card';
import { Working, WorkingRegion } from '../../ui/Working';
import { SchemaForm, singleString, type FormValues } from '../SchemaForm';
import { CompletionId, TryOutput, tryRun, type RunOutput } from '../try';
import { startAdd } from './AddScreen';
import type { ScreenProps } from './types';
import { WizardPage } from './WizardPage';
import '../../../styles/try-add.css';

const words = (name: string) => name.replace(/[_-]+/g, ' ').trim();
const sentenceCase = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

/** A value as the answer table shows it: null is Empty, a number or text as is, anything else as JSON. */
function shown(value: unknown): string {
  if (value === null || value === undefined) return 'Empty';
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return String(value);
  return JSON.stringify(value);
}

/** The sample input (B4), as the form's values: a string for a one-field input, else the object's fields. */
function sampleValues(sample: unknown, lone: string | undefined): FormValues | undefined {
  if (sample === undefined || sample === null) return undefined;
  if (lone) {
    const value = typeof sample === 'string' ? sample : (sample as Record<string, unknown>)[lone];
    return typeof value === 'string' ? { [lone]: value } : undefined;
  }
  if (typeof sample !== 'object') return undefined;
  const out: FormValues = {};
  for (const [k, v] of Object.entries(sample as Record<string, unknown>)) out[k] = typeof v === 'boolean' ? v : typeof v === 'string' ? v : JSON.stringify(v);
  return out;
}

export function TryScreen({ conv, model, ladder, detail, detailError }: ScreenProps) {
  const request = useApp((s) => s.request);
  const setWizardView = useApp((s) => s.setWizardView);
  const formId = useId();
  const [values, setValues] = useState<FormValues>({});
  const [busy, setBusy] = useState(false);
  const [output, setOutput] = useState<RunOutput | null>(null);
  const [ran, setRan] = useState(false);
  const [raw, setRaw] = useState(false);
  const last = useRef<unknown>(undefined);

  const name = model.result?.name;
  const inputSchema = detail?.spec?.input_schema;
  const noun = caseNoun(model, inputSchema);
  const lead = resultLead(model);
  const ship = lead?.ships ?? ladder?.rungs.find((r) => r.ships)?.name;
  const cost = ladder?.rungs.find((r) => r.ships)?.cost;
  const outputs: SchemaField[] = useMemo(() => {
    const own = schemaFields(detail?.spec?.output_schema);
    return own.length ? own : outputFields(model);
  }, [detail, model]);
  const lone = singleString(inputSchema as never);
  const label = noun !== 'case' && lone ? `${sentenceCase(noun)} text` : takesLabel(model);
  const sample = sampleValues(detail?.sample, lone);
  const empty = lone ? !String(values[lone] ?? '').trim() : false;
  const ready = conv.status === 'ready';

  const run = async (input: unknown) => {
    if (!name) return;
    last.current = input;
    setBusy(true);
    setOutput(null);
    setRaw(false);
    try {
      const out = await tryRun(request, conv.id, name, input);
      setOutput(out);
      if (out.ok) setRan(true);
    } finally {
      setBusy(false);
    }
  };

  const costLine = cost !== undefined && cost < 0.01
    ? 'Optional: each run costs a fraction of a cent.'
    : cost !== undefined ? `Optional: each run costs about ${fmtUsd(cost)}.` : '';
  const add = () => startAdd(conv, model, 'try');

  return (
    <WizardPage
      title={`Try your agent on a ${noun}`}
      sub={`Paste a ${noun} and run it, exactly as your app will. ${costLine}`.trim()}
      bar={{
        back: { label: 'Back to results', run: () => setWizardView(conv.id, 'results') },
        ask: { placeholder: 'Ask Architect about the agent', disabled: !ready },
        primary: ran ? (
          <Button variant="primary" size="md" data-action="add" disabled={!ready || conv.timeline.running} onClick={add}>
            Add to project<IconArrowRight size={16} stroke={1.5} aria-hidden />
          </Button>
        ) : (
          <Button variant="outline" size="md" data-action="skip" disabled={!ready || conv.timeline.running} onClick={add}>
            Skip to Add to project<IconArrowRight size={16} stroke={1.5} aria-hidden />
          </Button>
        ),
      }}
    >
      <Card className="try-card">
        <div className="try-panes">
          <section className="try-pane try-input" aria-label="Input">
            {detail === null && detailError ? (
              <p className="add-line is-err">This agent could not be opened ({detailError}).</p>
            ) : detail === null ? (
              <WorkingRegion label="Opening the agent" />
            ) : (
              <SchemaForm
                id={formId}
                className="try-form"
                schema={inputSchema as never}
                busy={busy}
                onlyLabel={label}
                values={values}
                onValues={setValues}
                single={{ rows: 9, placeholder: `Paste the text of one ${noun}` }}
                onSubmit={(v) => void run(v)}
                actions={(
                  <div className="try-actions">
                    {sample && (
                      <Button variant="outline" size="sm" data-action="sample" onClick={() => setValues(sample)}>
                        <IconUpload size={14} stroke={1.5} aria-hidden />Load sample {noun}
                      </Button>
                    )}
                    <div className="try-run">
                      <Button type="submit" variant="primary" size="sm" data-action="run" pending={busy} disabled={empty || busy}>
                        <IconPlayerPlay size={14} stroke={1.5} aria-hidden />{ran ? 'Run again' : 'Run agent'}
                      </Button>
                      {empty && <span className="try-hint">{sample ? `Paste or load a ${noun} first` : `Paste a ${noun} first`}</span>}
                    </div>
                  </div>
                )}
              />
            )}
          </section>
          <section className="try-pane try-answer" aria-label={ran && output?.ok ? 'What your agent gave back' : 'What your agent gives back'}>
            <h2 className="try-pane-title">{output?.ok ? 'What your agent gave back' : 'What your agent gives back'}</h2>
            {busy ? (
              <p className="try-empty"><Working size="sm" />{ship ? `Running on ${ship}` : 'Running'}</p>
            ) : output && !output.ok ? (
              <TryOutput output={output} onRetry={() => void run(last.current)} />
            ) : output ? (
              <Answer output={output} outputs={outputs} noun={noun} ship={ship} lead={lead} raw={raw} onRaw={() => setRaw((r) => !r)} />
            ) : (
              <p className="try-empty is-box">
                {outputs.length ? `Run the agent to see its answer here: ${joinAnd(outputs.map((f) => words(f.name)))}.` : 'Run the agent to see its answer here.'}
              </p>
            )}
          </section>
        </div>
      </Card>
    </WizardPage>
  );
}

/** A run's answer: one row per output field in schema order, what to check, and what the run took. */
function Answer({ output, outputs, noun, ship, lead, raw, onRaw }: {
  output: RunOutput; outputs: SchemaField[]; noun: string; ship?: string; lead: ReturnType<typeof resultLead>; raw: boolean; onRaw: () => void;
}) {
  const value = output.value;
  const object = value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null;
  const names = object ? (outputs.length ? outputs.map((f) => f.name) : Object.keys(object)) : [];
  const integer = outputs.some((f) => f.type === 'integer');
  const json = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  return (
    <div className="try-result">
      {object ? (
        <table className="try-table">
          <tbody>
            {names.map((field) => (
              <tr key={field}>
                <th scope="row">{sentenceCase(words(field))}</th>
                <td className="num">{shown(object[field])}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <pre className="try-json">{json}</pre>
      )}
      <p className="try-note">
        {integer && `Amounts are whole numbers, written without dots or commas. Check them against the ${noun} on the left. `}
        {ship && lead && `On the exam, ${ship} got the whole answer right on ${lead.correct} of ${lead.of}.`}
      </p>
      <div className="try-facts">
        <dl>
          {ship && <div><dt>Model</dt><dd>{ship}</dd></div>}
          {output.cost !== undefined && <div><dt>Cost of this run</dt><dd className="num">{fmtUsd(output.cost)}</dd></div>}
          {output.seconds !== undefined && <div><dt>Time</dt><dd className="num">{fmtSeconds(output.seconds)}</dd></div>}
        </dl>
        <Button variant="link" size="sm" aria-expanded={raw} onClick={onRaw}>{raw ? 'Hide raw JSON' : 'Show raw JSON'}</Button>
      </div>
      {raw && (
        <div className="try-raw">
          <pre className="try-json">{json}</pre>
          {output.completion && <CompletionId id={output.completion} />}
        </div>
      )}
    </div>
  );
}
