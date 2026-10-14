/**
 * The Plan sheet (redesign v2 §2.5): everything the card leaves out about the plan. The
 * spec as Architect will approve it (its rows, the task as written, the spec and Forge
 * pack hashes), then the settings for the next build. Its one primary is the card's: Build
 * while the plan waits for a yes, Build again after a refused, blocked or stopped build
 * (the overrides saved here apply to that run; `set_override` keeps them on the conductor).
 * Opened through the store: the card's overflow, Change settings, Change and retry, and
 * the plan chip in a normal chat.
 *
 * Build with changes (clarity spec, step 2): settings saved while the plan waits for a
 * yes are not in the plan on screen. Then the Plan screen's primary and this footer read
 * "Build with changes": that answers the plan no and sends the same /make again, so the
 * plan is drawn with the settings and approved again. A frozen plan (`/make @file.json`)
 * is built as its file says: build mode ignores a saved pass mark, budget or model there
 * (FROZEN_IGNORED in architect/agent/build.py), so only the other settings count.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { create } from 'zustand';
import { planField, type ForgeModel } from '../../lib/forge';
import { prettyModel } from '../../lib/models';
import { buildLabel, cardRows, hashNote, practiceWords, rerunCommand, screenWords, sheetRows } from '../../lib/vocab';
import { useApp, type Conv } from '../../store/app';
import { Button } from '../ui/Button';
import { CardSection } from '../ui/Card';
import { Facts } from '../ui/Facts';
import { Field, Input } from '../ui/Field';
import { Sheet } from '../ui/Sheet';
import { HashLines, type HashLineProps } from './HashLine';

/** The settings a frozen plan keeps from its file (architect/agent/build.py FROZEN_IGNORED). */
export const FROZEN_FIXED = new Set(['floor_limit', 'floor_file', 'budget_usd', 'budget_s', 'compile_usd', 'compile_s', 'model']);

/** Settings saved while a plan waited, by conversation: the plan's approval and the keys saved. */
interface PlanChanges {
  saved: Record<string, { approval: number; keys: string[] }>;
  mark(convId: string, approval: number, keys: string[]): void;
}

export const usePlanChanges = create<PlanChanges>((set) => ({
  saved: {},
  mark: (convId, approval, keys) => set((s) => {
    const before = s.saved[convId]?.approval === approval ? s.saved[convId].keys : [];
    return { saved: { ...s.saved, [convId]: { approval, keys: [...new Set([...before, ...keys])] } } };
  }),
}));

/** The plan's file when it is frozen ("receipt-reader.json"). */
export function frozenFile(model: ForgeModel): string | undefined {
  const frozen = model.plan?.data?.frozen;
  return typeof frozen === 'string' && frozen ? frozen : undefined;
}

/**
 * Whether settings saved since this plan was drawn would change it: some were saved while
 * its approval is open, and, on a frozen plan, one of them is not fixed by the file.
 */
export function useChangedPlan(conv: Pick<Conv, 'id'>, model: ForgeModel): boolean {
  const entry = usePlanChanges((s) => s.saved[conv.id]);
  const approval = model.approval?.id;
  if (approval === undefined || !entry || entry.approval !== approval) return false;
  const frozen = !!frozenFile(model);
  return entry.keys.some((key) => !frozen || !FROZEN_FIXED.has(key));
}

/** Build with changes: answer the waiting plan no, then send the same /make, so the plan is drawn again with the settings. */
export function useBuildWithChanges(conv: Pick<Conv, 'id'>, model: ForgeModel): () => Promise<void> {
  const answer = useApp((s) => s.answer);
  const send = useApp((s) => s.send);
  return useCallback(async () => {
    const approval = model.approval;
    if (!approval) return;
    await answer(conv.id, approval.id, 'no');
    await send(conv.id, rerunCommand(model));
  }, [answer, send, conv.id, model]);
}

/**
 * The plan's hashes as hash lines: the spec Architect will approve and the Forge pack it
 * builds with. They carry no state icon: a check beside them would read as "matches the
 * pre-registration", and the app has no pre-registration manifest to compare them with
 * yet (SPEC.md, "Hash line": nothing when unknown). Their notes say what is known.
 */
export function planHashes(model: ForgeModel): HashLineProps[] {
  const data = model.plan?.data ?? {};
  const lines: HashLineProps[] = [];
  const spec = typeof data.spec_hash === 'string' ? data.spec_hash : undefined;
  if (spec) {
    lines.push({ label: 'spec', hash: spec, note: typeof data.frozen === 'string' ? `frozen, ${data.frozen}` : undefined });
  }
  const pack = typeof data.pack_hash === 'string' ? data.pack_hash : undefined;
  if (pack) lines.push({ label: 'pack', hash: pack, note: hashNote(planField(model, 'forge pack')) });
  return lines;
}

/** Whether the build can be run again from here: it was refused, blocked or stopped. */
export function canBuildAgain(model: ForgeModel, conv: Pick<Conv, 'status'>): boolean {
  if (model.approval || model.running) return false;
  if (model.phase === 'not_licensed' || model.phase === 'blocked') return true;
  if ((model.phase === 'planning' || model.phase === 'building') && !model.result) return true;
  return conv.status === 'stopped' && model.phase !== 'empty';
}

export function PlanSheet({ conv, model }: { conv: Conv; model: ForgeModel }) {
  const open = useApp((s) => s.forgeSheet === 'plan' && s.forgeSheetConv === conv.id);
  const section = useApp((s) => s.forgeSheetSection);
  const openForgeSheet = useApp((s) => s.openForgeSheet);
  const answer = useApp((s) => s.answer);
  const send = useApp((s) => s.send);
  const ready = conv.status === 'ready';
  const close = () => openForgeSheet(null);
  // Unsaved settings would be lost (or ignored) by Build: the primary waits until they are saved or cancelled.
  const [dirty, setDirty] = useState(false);
  const changed = useChangedPlan(conv, model);
  const buildWithChanges = useBuildWithChanges(conv, model);
  const mark = usePlanChanges((s) => s.mark);
  const approvalId = model.approval?.id;
  const onSaved = useCallback((keys: string[]) => {
    if (approvalId !== undefined && keys.length) mark(conv.id, approvalId, keys);
  }, [approvalId, conv.id, mark]);
  const frozen = frozenFile(model);

  // Change settings and Change and retry land on Next build.
  useEffect(() => {
    if (!open || section !== 'next') return undefined;
    const frame = requestAnimationFrame(() => document.getElementById('plan-next')?.scrollIntoView({ block: 'start' }));
    return () => cancelAnimationFrame(frame);
  }, [open, section]);

  const rerun = rerunCommand(model).trim() === '/make' ? undefined : rerunCommand(model);
  let footer: React.ReactNode = null;
  if (model.approval && changed) {
    footer = (
      <Button variant="primary" size="sm" data-action="build" disabled={!ready || dirty} onClick={() => { close(); void buildWithChanges(); }}>
        Build with changes
      </Button>
    );
  } else if (model.approval) {
    footer = (
      <Button variant="primary" size="sm" data-action="build" disabled={!ready || dirty} onClick={() => { close(); void answer(conv.id, model.approval!.id, 'yes'); }}>
        {buildLabel(model)}
      </Button>
    );
  } else if (rerun && canBuildAgain(model, conv)) {
    footer = (
      <Button variant="primary" size="sm" data-action="again" disabled={!ready || dirty} onClick={() => { close(); void send(conv.id, rerun); }}>
        Build again
      </Button>
    );
  }

  return (
    <Sheet
      open={open}
      onOpenChange={(o) => { if (!o) close(); }}
      title="Plan"
      name="plan"
      description="The settings for the next build and the spec"
      footer={footer}
      footerStart={dirty ? <span className="sheet-note">Save or cancel first</span> : undefined}
    >
      {/* While this plan waits for a yes, Build builds it as it stands: saved settings redraw it through Build with changes. */}
      <CardSection id="plan-next" title="Next build" meta={model.approval ? 'Saved changes apply when you click Build with changes' : undefined}>
        {frozen && <p className="sheet-note">{frozen} is built as written, so its pass mark, budgets and model stay as the file sets them.</p>}
        <Overrides conv={conv} onDirty={setDirty} onSaved={onSaved} />
      </CardSection>
      <SpecSection conv={conv} model={model} />
    </Sheet>
  );
}

function SpecSection({ conv, model }: { conv: Conv; model: ForgeModel }) {
  const models = Array.isArray(model.plan?.data?.ladder) ? (model.plan!.data!.ladder as string[]) : conv.state.models ?? [];
  const rows = useMemo(() => (model.plan ? cardRows(model.plan.rows, { models }) : []), [model.plan, models]);
  if (!model.plan) {
    return (
      <CardSection title="Spec">
        <p className="sheet-note">The plan appears after the questions.</p>
      </CardSection>
    );
  }
  // The task as written, when it runs past one line: the rows show its first line.
  const task = planField(model, 'task');
  const longTask = task && task.includes('\n') ? task : undefined;
  const hashes = planHashes(model);
  const footer = model.plan.footer && !model.approval && !hashes.length && !/^spec \w+\.?$/.test(model.plan.footer.trim()) ? practiceWords(model.plan.footer) : '';
  return (
    <CardSection title="Spec" meta={model.name}>
      <Facts columns={1} items={sheetRows(rows).map(([label, value], i) => ({ label, value, id: `${label}-${i}` }))} />
      {longTask && <div className="sheet-prose">{longTask}</div>}
      {hashes.length > 0 && <div className="plan-hashes"><HashLines lines={hashes} /></div>}
      {!hashes.length && model.plan.hash && <p className="sheet-note tabular">spec {model.plan.hash}</p>}
      {footer && <p className="sheet-note">{footer}</p>}
    </CardSection>
  );
}

interface OverrideField { key: string; label: string; kind: 'number' | 'text' | 'choice' | 'model'; choices?: Array<[string, string]> }

const OVERRIDE_FIELDS: OverrideField[] = [
  { key: 'floor_limit', label: 'Pass mark (0 to 1)', kind: 'number' },
  { key: 'floor_file', label: 'Cases file', kind: 'text' },
  { key: 'engineer', label: 'Harness', kind: 'choice', choices: [['', 'Default'], ['forge', 'Written for this job'], ['preset', 'A ready-made one']] },
  { key: 'model', label: 'Model', kind: 'model' },
  { key: 'name', label: 'Install name', kind: 'text' },
  { key: 'budget_usd', label: 'Per run, dollars', kind: 'number' },
  { key: 'budget_s', label: 'Per run, seconds', kind: 'number' },
  { key: 'compile_usd', label: 'Build, dollars', kind: 'number' },
  { key: 'compile_s', label: 'Build, seconds', kind: 'number' },
  { key: 'forge_usd', label: 'Forge, dollars', kind: 'number' },
  { key: 'forge_s', label: 'Forge, seconds', kind: 'number' },
];

const shown = (v: unknown): string => (v === undefined || v === null ? '' : String(v));

/**
 * The next build's settings as one inline form: Save and Cancel appear only once a field
 * changes. Saving sends each changed field to the conductor; Reset clears them all.
 */
function Overrides({ conv, onDirty, onSaved }: { conv: Conv; onDirty?: (dirty: boolean) => void; onSaved?: (keys: string[]) => void }) {
  const request = useApp((s) => s.request);
  const [saved, setSaved] = useState<Record<string, unknown>>({});
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const ready = conv.status === 'ready';

  const load = useCallback(async () => {
    try {
      const result = await request<{ overrides: Record<string, unknown> }>(conv.id, 'overrides');
      setSaved(result.overrides ?? {});
    } catch {
      /* older bridge: the fields start empty */
    }
  }, [conv.id, request]);
  useEffect(() => {
    if (ready) void load();
  }, [ready, load]);

  const value = (key: string) => (key in draft ? draft[key] : shown(saved[key]));
  const changed = OVERRIDE_FIELDS.filter((f) => f.key in draft && draft[f.key].trim() !== shown(saved[f.key]));
  const dirty = changed.length > 0;
  useEffect(() => onDirty?.(dirty), [dirty, onDirty]);

  const save = async () => {
    setError('');
    for (const field of changed) {
      const raw = draft[field.key].trim();
      if (field.kind === 'number' && raw !== '' && !Number.isFinite(Number(raw))) {
        setError(`${field.label} needs a number`);
        return;
      }
    }
    setBusy(true);
    try {
      for (const field of changed) {
        const raw = draft[field.key].trim();
        const v = raw === '' ? null : field.kind === 'number' ? Number(raw) : raw;
        try {
          await request(conv.id, 'set_override', { key: field.key, value: v });
        } catch (err) {
          const e = err as { code?: string; detail?: string };
          setError(`${field.label}: ${e.detail || e.code || 'not accepted'}`);
          await load();
          return;
        }
      }
      setDraft({});
      onSaved?.(changed.map((f) => f.key));
      await load();
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      className="overrides"
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      <div className="overrides-grid">
        {OVERRIDE_FIELDS.map((field) => {
          const id = `override-${field.key}`;
          const set = (v: string) => setDraft((d) => ({ ...d, [field.key]: v }));
          return (
            <Field key={field.key} label={field.label} htmlFor={id}>
              {field.kind === 'choice' || field.kind === 'model' ? (
                <select id={id} className="ui-input ui-input-sm" disabled={!ready} value={value(field.key)} onChange={(e) => set(e.target.value)}>
                  {field.kind === 'choice'
                    ? field.choices!.map(([v, l]) => <option key={v} value={v}>{l}</option>)
                    : (
                      <>
                        <option value="">Cheapest past the pass mark</option>
                        {(conv.state.models ?? []).map((m) => <option key={m} value={m}>{screenWords(prettyModel(m), true)}</option>)}
                      </>
                    )}
                </select>
              ) : (
                <Input id={id} size="sm" disabled={!ready} value={value(field.key)} inputMode={field.kind === 'number' ? 'decimal' : undefined} onChange={(e) => set(e.target.value)} />
              )}
            </Field>
          );
        })}
      </div>
      {error && <p role="alert" className="ui-field-error">{error}</p>}
      <div className="overrides-actions">
        <Button variant="link" size="sm" disabled={!ready || busy} onClick={() => void request(conv.id, 'clear_overrides').then(() => {
          setDraft({});
          onSaved?.(Object.keys(saved).filter((key) => saved[key] !== undefined && saved[key] !== null));
          return load();
        })}>Reset</Button>
        {dirty && (
          <>
            <Button variant="ghost" size="sm" disabled={busy} onClick={() => { setDraft({}); setError(''); }}>Cancel</Button>
            <Button variant="primary" size="sm" type="submit" pending={busy} disabled={!ready}>Save</Button>
          </>
        )}
      </div>
    </form>
  );
}
