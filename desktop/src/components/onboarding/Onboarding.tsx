/**
 * First run: sign in to a model, choose it, choose a folder. One question at a
 * time. Each step is the CLI's own first-run flow (providers.signin) driven
 * through the bridge, so the same checks run and the same profile is written.
 */
import { IconArrowLeft, IconCpu, IconFolder, IconKey, IconTerminal2, type Icon } from '@tabler/icons-react';
import clsx from 'clsx';
import { Fragment, useCallback, useEffect, useState } from 'react';
import type { Ask } from '../../lib/timeline';
import { errorText, useApp, type Conv } from '../../store/app';
import { AskCard } from '../chat/AskCard';
import { Badge } from '../ui/Badge';
import { Button } from '../ui/Button';
import { Card, CardBody, CardFooter } from '../ui/Card';
import { Field, Input } from '../ui/Field';
import { Row, RowGroup, RowList } from '../ui/RowList';
import { StatusMark } from '../ui/StatusMark';
import { Mark, Working, WorkingRegion } from '../ui/Working';
import { chooseFolder } from '../shell/folders';
import { prettyModel } from '../../lib/models';

export interface ProviderChoice {
  id: string;
  label: string;
  kind: 'cli' | 'api' | 'local' | string;
  state?: string;
  ready?: boolean;
  detail?: string;
  /** What a key state holds: a key, or a judge code (Nebius through the judges' relay). */
  credential?: 'key' | 'judge_code' | null;
  recommended?: boolean;
  models?: string[];
}

interface ModelRow { id: string; price?: string | null }

const SHOWN_MODELS = 30;

/** A provider's name without the terminal's how ("Claude (claude CLI login)" reads "Claude"). */
export function providerName(label: string): string {
  return label.replace(/\s*\([^)]*\)\s*$/, '').trim() || label;
}

/** "~$1/$5 per M (plan)" reads "$1 in, $5 out per M"; anything else as it came. */
export function priceWords(price: string): string {
  const m = /^~?\$([\d.]+)\s*\/\s*\$([\d.]+) per M\b/.exec(price.trim());
  return m ? `$${m[1]} in, $${m[2]} out per M` : price;
}

/** The model to start on: the provider's own suggestion when it is listed (Nemotron 3 Super on Nebius), else the first. */
export function suggestedModel(models: ModelRow[], preferred?: string | null): string {
  return (preferred && models.some((m) => m.id === preferred) ? preferred : models[0]?.id) ?? '';
}

/** The list as shown, its first 30 rows: the suggested model moves to the top when it would fall past them. */
export function inView(models: ModelRow[], pick: string): ModelRow[] {
  const at = models.findIndex((m) => m.id === pick);
  return at < SHOWN_MODELS ? models : [models[at], ...models.slice(0, at), ...models.slice(at + 1)];
}

const KIND_HEADINGS: Record<string, string> = {
  cli: 'Subscription',
  api: 'API key',
  local: 'On this computer',
};

export function providerState(choice: ProviderChoice): string {
  switch (choice.state) {
    case 'signed_in': return 'Signed in';
    case 'cli_found': return choice.id === 'chatgpt' ? 'Installed, needs sign-in' : 'Installed';
    case 'cli_missing': return 'Not installed';
    case 'key_env': return 'Key found in your environment';
    // The CLI's words: "judge code stored", "key stored".
    case 'key_stored': return choice.credential === 'judge_code' ? 'Judge code stored' : 'Key stored';
    case 'no_key': return 'Needs a key';
    case 'reachable': return 'Running';
    case 'unreachable': return 'Not running';
    default: return choice.detail ?? '';
  }
}

type Step = 'provider' | 'signing' | 'model' | 'folder' | 'starting';

export function Onboarding({ conv }: { conv: Conv }) {
  const request = useApp((s) => s.request);
  const answer = useApp((s) => s.answer);
  const prefs = useApp((s) => s.prefs);
  const setPrefs = useApp((s) => s.setPrefs);
  const meta = useApp((s) => s.meta);
  const [step, setStep] = useState<Step>('provider');
  const [choices, setChoices] = useState<ProviderChoice[] | null>(null);
  const [provider, setProvider] = useState<string>('');
  const [models, setModels] = useState<ModelRow[]>([]);
  const [model, setModel] = useState('');
  const [error, setError] = useState('');
  const open = prefs.open.find((o) => o.id === conv.id);
  const [folder, setFolder] = useState(open?.cwd || prefs.folders[0] || meta.homedir || '');

  const load = useCallback(async () => {
    setError('');
    try {
      const result = await request<{ choices: ProviderChoice[] }>(conv.id, 'onboarding', prefs.workspace ? { workspace: prefs.workspace } : {});
      setChoices(result.choices);
      // A folder that runs on Nebius (the receipt example) came here to sign in to Nebius.
      const nebius = conv.error === 'sign_in_first' ? result.choices.find((c) => c.id === 'nebius') : undefined;
      const pick = nebius ?? result.choices.find((c) => c.recommended) ?? result.choices.find((c) => c.ready) ?? result.choices[0];
      if (pick) setProvider(pick.id);
    } catch (err) {
      setError(errorText((err as { code?: string }).code));
      setChoices([]);
    }
  }, [conv.id, conv.error, request, prefs.workspace]);

  useEffect(() => {
    void load();
  }, [load]);

  const signIn = async () => {
    setError('');
    setStep('signing');
    try {
      const result = await request<{ ok: boolean; code?: string; message?: string | null; models: ModelRow[]; default_model?: string | null }>(conv.id, 'sign_in', {
        provider, ...(prefs.workspace ? { workspace: prefs.workspace } : {}),
      });
      if (!result.ok) {
        // A judge code's failure comes worded (providers/judge.py), the same as in the CLI.
        setError(result.message || signInError(result.code));
        setStep('provider');
        return;
      }
      const pick = suggestedModel(result.models, result.default_model);
      setModels(inView(result.models, pick));
      setModel(pick);
      setStep('model');
    } catch (err) {
      setError(signInError((err as { code?: string }).code));
      setStep('provider');
    }
  };

  const chooseModel = async () => {
    setError('');
    try {
      await request(conv.id, 'choose_model', { provider, model, ...(prefs.workspace ? { workspace: prefs.workspace } : {}) });
      setStep('folder');
    } catch (err) {
      setError(errorText((err as { code?: string }).code));
    }
  };

  const start = () => {
    setStep('starting');
    if (open && folder !== open.cwd) {
      setPrefs({
        open: prefs.open.map((o) => (o.id === conv.id ? { ...o, cwd: folder } : o)),
        folders: [folder, ...prefs.folders.filter((f) => f !== folder)],
      });
      // The process started in the old folder; the conversation restarts in the chosen one.
      useApp.getState().restart(conv.id);
      return;
    }
    void useApp.getState().boot(conv.id);
  };

  const openAsks = conv.timeline.items.filter((i) => i.kind === 'ask' && !i.closed);
  const onAnswer = (ask: Ask, value: string | null) => void answer(conv.id, ask.id, value);
  const chosen = choices?.find((c) => c.id === provider);

  // A key or a judge code the sign-in waits on gets its own card; any other ask is its own card.
  const secret = step === 'signing' ? openAsks.find((i) => i.kind === 'ask' && i.ask.kind === 'secret') : undefined;
  const title = secret ? (takesCode(chosen) ? 'Paste your judge code' : 'Paste your key') : STEP_TITLES[step];

  return (
    <div className="onboarding">
      <div className="onboarding-column">
        <div className="onboarding-head">
          <Mark className="onboarding-mark text-accent" />
          <h1>{title}</h1>
          {step === 'provider' && conv.error === 'sign_in_first' && <p className="onboarding-sub">{errorText(conv.error)}</p>}
          {step === 'signing' && conv.links.length > 0 && (
            <p className="onboarding-sub">
              Finish in your browser. If it did not open, <a href={conv.links.at(-1)} target="_blank" rel="noreferrer">open the sign-in page</a>.
            </p>
          )}
        </div>
        {step === 'provider' && (
          <Card className="onboarding-card">
            {choices === null ? (
              <CardBody><WorkingRegion label="Looking for what is already set up" /></CardBody>
            ) : (
              <CardBody pad="flush">
                <ProviderList choices={choices} value={provider} onChange={setProvider} onConfirm={() => void signIn()} />
              </CardBody>
            )}
            {error && <p className="onboarding-error" role="alert">{error}</p>}
            <CardFooter>
              <Button variant="primary" size="sm" data-action="continue" disabled={!provider} onClick={() => void signIn()}>Continue</Button>
            </CardFooter>
          </Card>
        )}
        {step === 'signing' && (
          secret && secret.kind === 'ask' ? (
            <SecretCard key={secret.key} ask={secret.ask} code={takesCode(chosen)} onAnswer={onAnswer} />
          ) : (
            <div className="onboarding-asks">
              {openAsks.map((item) => item.kind === 'ask' && <AskCard key={item.key} item={item} onAnswer={onAnswer} />)}
              {openAsks.length === 0 && (
                <Card className="onboarding-card">
                  <CardBody><div className="onboarding-working"><Working size="sm" /><span>Checking the connection</span></div></CardBody>
                </Card>
              )}
            </div>
          )
        )}
        {step === 'model' && (
          <Card className="onboarding-card">
            <CardBody pad="flush">
              <div className="row-list choice-list" role="radiogroup" aria-label="Models">
                {models.slice(0, SHOWN_MODELS).map((m) => (
                  <button
                    key={m.id}
                    type="button"
                    role="radio"
                    aria-checked={model === m.id}
                    className={clsx('row is-interactive choice-row', model === m.id && 'is-on')}
                    onClick={() => setModel(m.id)}
                    onDoubleClick={() => void chooseModel()}
                  >
                    <span className="row-leading"><span className="radio-dot" aria-hidden /></span>
                    <span className="row-text"><span className="row-title" title={m.id}>{prettyModel(m.id)}</span></span>
                    {m.price && <span className="row-trailing num">{priceWords(m.price)}</span>}
                  </button>
                ))}
              </div>
            </CardBody>
            {error && <p className="onboarding-error" role="alert">{error}</p>}
            <CardFooter start={<Button variant="ghost" size="sm" onClick={() => setStep('provider')}><IconArrowLeft size={16} stroke={1.5} aria-hidden /> Back</Button>}>
              <Button variant="primary" size="sm" data-action="model" disabled={!model} onClick={() => void chooseModel()}>Use this model</Button>
            </CardFooter>
          </Card>
        )}
        {step === 'folder' && (
          <Card className="onboarding-card">
            <CardBody pad="flush">
              <RowList aria-label="Folder">
                <Row
                  leading={<IconFolder size={16} stroke={1.5} aria-hidden />}
                  title={folder || 'Choose a folder'}
                  trailing={<Button variant="ghost" size="xs" onClick={() => void chooseFolder(folder).then((f) => f && setFolder(f))}>Change</Button>}
                  className="folder-choice"
                />
              </RowList>
            </CardBody>
            <CardFooter>
              <Button variant="primary" size="sm" data-action="start" disabled={!folder} onClick={start}>Start</Button>
            </CardFooter>
          </Card>
        )}
        {step === 'starting' && <WorkingRegion label="Starting Architect" />}
      </div>
    </div>
  );
}

const STEP_TITLES: Record<Step, string> = {
  provider: 'Connect a model',
  signing: 'Signing in',
  model: 'Choose a model',
  folder: 'Where should Architect work?',
  starting: 'Starting Architect',
};

/** Nebius takes a judge code from the judges' relay in place of a key. */
function takesCode(choice?: ProviderChoice): boolean {
  return choice?.id === 'nebius' || choice?.credential === 'judge_code';
}

/** The key or judge code the sign-in asks for: one password field, then Check. */
function SecretCard({ ask, code, onAnswer }: { ask: Ask; code: boolean; onAnswer: (ask: Ask, value: string | null) => void }) {
  const [value, setValue] = useState('');
  // A server that may need no key asks with "Enter for none": an empty field then answers "no key".
  const optional = /enter for none/i.test(ask.question ?? '');
  return (
    <Card className="onboarding-card">
      <form onSubmit={(e) => { e.preventDefault(); if (value.trim() || optional) onAnswer(ask, value.trim()); }}>
        <CardBody>
          <Field label={code ? 'Judge code' : 'Key'} htmlFor="onboarding-secret">
            <Input id="onboarding-secret" type="password" autoFocus autoComplete="off" spellCheck={false} value={value} onChange={(e) => setValue(e.target.value)} />
          </Field>
        </CardBody>
        <CardFooter start={<Button variant="ghost" size="sm" onClick={() => onAnswer(ask, null)}>Cancel</Button>}>
          <Button type="submit" variant="primary" size="sm" data-action="check" disabled={!value.trim() && !optional}>{!value.trim() && optional ? 'No key' : code ? 'Check code' : 'Check key'}</Button>
        </CardFooter>
      </form>
    </Card>
  );
}

const KIND_GLYPH: Record<string, Icon> = { cli: IconTerminal2, api: IconKey, local: IconCpu };

export function ProviderList({
  choices, value, onChange, onConfirm,
}: { choices: ProviderChoice[]; value: string; onChange: (id: string) => void; onConfirm?: () => void }) {
  const groups = ['cli', 'api', 'local'].map((kind) => ({ kind, rows: choices.filter((c) => c.kind === kind) })).filter((g) => g.rows.length);
  const other = choices.filter((c) => !['cli', 'api', 'local'].includes(c.kind));
  if (other.length) groups.push({ kind: 'other', rows: other });
  return (
    <div className="row-list provider-groups" role="radiogroup" aria-label="Providers">
      {groups.map((group) => {
        const Glyph = KIND_GLYPH[group.kind] ?? IconKey;
        return (
          <Fragment key={group.kind}>
            <RowGroup label={KIND_HEADINGS[group.kind] ?? 'Other'} />
            {group.rows.map((c) => (
              <button
                key={c.id}
                type="button"
                role="radio"
                aria-checked={value === c.id}
                className={clsx('row is-interactive choice-row', providerState(c) && 'has-meta', value === c.id && 'is-on')}
                onClick={() => onChange(c.id)}
                onDoubleClick={onConfirm}
              >
                <span className="row-leading"><Glyph size={16} stroke={1.5} aria-hidden /></span>
                <span className="row-text">
                  <span className="row-title" title={c.label}>{providerName(c.label)}</span>
                  {providerState(c) && <span className="row-meta choice-meta">{providerState(c)}</span>}
                </span>
                <span className="row-trailing">
                  {c.recommended && <Badge>Recommended</Badge>}
                  {c.ready && <StatusMark tone="ok" label={providerState(c) || 'Ready'} className="choice-state" />}
                </span>
              </button>
            ))}
          </Fragment>
        );
      })}
    </div>
  );
}

export function signInError(code?: string): string {
  switch (code) {
    case 'cli_missing': return 'That program is not installed. Install it, then try again.';
    case 'unauthorized': return 'That key was refused. Check it and paste it again.';
    case 'unreachable': return 'Nothing answered at that address. Start the server, then try again.';
    case 'timeout': return 'It took too long to answer. Check your connection, then try again.';
    case 'no_key': return 'A key is needed for this provider.';
    case 'cancelled': case 'setup_cancelled': return 'Sign-in was cancelled.';
    case 'login_failed': case 'codex_login_failed': return 'Sign-in did not finish. Try again.';
    default: return errorText(code);
  }
}
