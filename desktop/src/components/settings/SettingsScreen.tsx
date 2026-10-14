/**
 * Settings. Every control writes where the CLI keeps the same setting
 * (config.toml, the profile, credentials, ARCHITECT.md), through the bridge, so
 * the terminal and the app always agree.
 */
import {
  IconAdjustments, IconArchive, IconGitBranch, IconGitFork, IconBook2, IconTrash, IconCheck, IconCpu, IconKey, IconKeyboard, IconPalette, IconPlug, IconShieldCheck,
  IconCopy, IconDownload, IconSearch, IconX, IconInfoCircle, IconSettings, IconSparkles, IconStethoscope, IconTerminal2, IconTool, type Icon,
} from '@tabler/icons-react';
import clsx from 'clsx';
import { useCallback, useEffect, useMemo, useState } from 'react';
import type { Ask } from '../../lib/timeline';
import { native, openLink } from '../../lib/native';
import { isMac, recordingKeys, sameShortcut, SHORTCUT_ALIASES, SHORTCUT_NAMES, shortcutFromEvent, SHORTCUTS, shortcutLabel, type Shortcut } from '../../lib/shortcuts';
import { useCopy } from '../../lib/useCopy';
import { Mark } from '../ui/Working';
import { errorText, useApp, type Conv, type Theme } from '../../store/app';
import { CapturedView, useCapture, useHost } from '../agents/AgentsScreen';
import { AskCard } from '../chat/AskCard';
import { PERMISSION_LABELS } from '../chat/Composer';
import { humanize } from '../forge/SchemaForm';
import { shells, type ShellInfo } from '../panes/TerminalPane';
import { providerState, signInError, type ProviderChoice } from '../onboarding/Onboarding';
import { chooseFolder } from '../shell/folders';
import { useConfirm } from '../ui/useConfirm';
import { Badge } from '../ui/Badge';
import { Button, IconButton } from '../ui/Button';
import { Input, TextArea } from '../ui/Field';
import { PageHeader } from '../ui/PageHeader';
import { Segmented } from '../ui/Segmented';
import { StatusMark } from '../ui/StatusMark';
import { Working, WorkingRegion } from '../ui/Working';

const PANES = [
  { id: 'general', icon: IconSettings, label: 'General', keywords: 'terminal shell powershell bash wsl cmd default' },
  { id: 'models', icon: IconSparkles, label: 'Models and sign-in', keywords: 'provider login logout key api codex chatgpt claude ollama model nebius token factory nemotron nvidia' },
  { id: 'permissions', icon: IconShieldCheck, label: 'Permissions and budgets', keywords: 'approve ask auto budget money time compact' },
  { id: 'presets', icon: IconAdjustments, label: 'Presets and roles', keywords: 'preset role forge compile ladder routing subagent spawn' },
  { id: 'git', icon: IconGitBranch, label: 'Git and worktrees', keywords: 'git branch worktree prefix commit archive' },
  { id: 'tools', icon: IconPlug, label: 'Tools', keywords: 'mcp server tool' },
  { id: 'instructions', icon: IconBook2, label: 'Instructions', keywords: 'architect.md memory custom commands' },
  { id: 'appearance', icon: IconPalette, label: 'Appearance', keywords: 'theme dark light' },
  { id: 'shortcuts', icon: IconKeyboard, label: 'Keyboard shortcuts', keywords: 'keys shortcut' },
  { id: 'health', icon: IconStethoscope, label: 'Health check', keywords: 'doctor node pi problem fix' },
  { id: 'archived', icon: IconArchive, label: 'Archived', keywords: 'archive unarchive delete old conversations' },
  { id: 'about', icon: IconInfoCircle, label: 'About', keywords: 'version update electron node about' },
  { id: 'advanced', icon: IconTool, label: 'Advanced', keywords: 'workspace profile folder probe files' },
] as const satisfies ReadonlyArray<{ id: string; icon: Icon; label: string; keywords: string }>;

export function SettingsScreen() {
  const pane = useApp((s) => s.settingsPane);
  const setScreen = useApp((s) => s.setScreen);
  const host = useHost();
  const [query, setQuery] = useState('');
  const shown = PANES.filter((p) => !query || `${p.label} ${p.keywords}`.toLowerCase().includes(query.toLowerCase()));
  const needsHost = !['general', 'about', 'appearance', 'shortcuts', 'advanced', 'archived', 'git'].includes(pane);
  return (
    <section className="screen">
      <div className="screen-head drag"><PageHeader title="Settings" /></div>
      <div className="settings">
        <nav className="settings-nav" aria-label="Settings">
          <Input size="sm" className="settings-search" aria-label="Search settings" placeholder="Search settings" value={query} onChange={(e) => setQuery(e.target.value)} />
          {shown.map((p) => (
            <button key={p.id} type="button" className={clsx('nav-row', pane === p.id && 'is-active')} onClick={() => setScreen('settings', p.id)}>
              <p.icon size={16} stroke={1.5} aria-hidden />
              <span>{p.label}</span>
            </button>
          ))}
        </nav>
        <div className="settings-pane">
          <div className="settings-pane-inner">
            {host && <OpenAsks conv={host} />}
            {needsHost && !host ? (
              <p className="tone-mute">These settings are read through a conversation. Open one, then come back.</p>
            ) : pane === 'models' ? <ModelsPane host={host!} />
              : pane === 'permissions' ? <PermissionsPane host={host!} />
                : pane === 'presets' ? <PresetsPane host={host!} />
                  : pane === 'tools' ? <ToolsPane host={host!} />
                    : pane === 'instructions' ? <InstructionsPane host={host!} />
                      : pane === 'appearance' ? <AppearancePane />
                        : pane === 'shortcuts' ? <ShortcutsPane />
                          : pane === 'health' ? <HealthPane host={host!} />
                          : pane === 'archived' ? <ArchivedPane />
                          : pane === 'git' ? <GitPane host={host} />
                          : pane === 'general' ? <GeneralPane />
                          : pane === 'about' ? <AboutPane />
                            : <AdvancedPane />}
          </div>
        </div>
      </div>
    </section>
  );
}

/** Questions a settings action is waiting on (an API key, a server URL) show here. */
function OpenAsks({ conv }: { conv: Conv }) {
  const answer = useApp((s) => s.answer);
  const asks = conv.timeline.items.filter((i) => i.kind === 'ask' && !i.closed);
  if (!asks.length) return null;
  return (
    <div className="settings-asks">
      {asks.map((item) => item.kind === 'ask' && (
        <AskCard key={item.key} item={item} onAnswer={(ask: Ask, value) => void answer(conv.id, ask.id, value)} />
      ))}
    </div>
  );
}

function Setting({ label, help, children }: { label: string; help?: string; children: React.ReactNode }) {
  return (
    <div className="setting">
      <div className="setting-text">
        <span className="setting-label">{label}</span>
        {help && <span className="setting-help">{help}</span>}
      </div>
      <div className="setting-control">{children}</div>
    </div>
  );
}

function useRequest<T>(host: Conv | undefined, method: string, params: Record<string, unknown> = {}) {
  const request = useApp((s) => s.request);
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState('');
  const key = JSON.stringify(params);
  const load = useCallback(async () => {
    if (!host) return;
    try {
      setData(await request<T>(host.id, method, JSON.parse(key) as Record<string, unknown>));
      setError('');
    } catch (err) {
      setError((err as { code?: string }).code ?? 'failed');
    }
  }, [host?.id, method, key, request]);
  useEffect(() => {
    void load();
  }, [load]);
  return { data, error, load, setData };
}

// -- Models and sign-in -------------------------------------------------------------------

interface ModelRow { name: string; provider: string; label?: string; price?: string; current?: boolean; configured?: boolean }

function ModelsPane({ host }: { host: Conv }) {
  const request = useApp((s) => s.request);
  const send = useApp((s) => s.send);
  const providers = useRequest<{ providers: ProviderChoice[] }>(host, 'providers');
  const models = useRequest<{ current: string; models: ModelRow[] }>(host, 'models');
  const config = useRequest<{ values: Record<string, unknown> }>(host, 'config');
  const [busy, setBusy] = useState('');
  const [message, setMessage] = useState<{ text: string; error?: boolean } | null>(null);

  const signIn = async (id: string) => {
    setBusy(id);
    setMessage(null);
    try {
      const result = await request<{ ok: boolean; code?: string; message?: string | null; models: Array<{ id: string }> }>(host.id, 'sign_in', { provider: id });
      if (result.ok) {
        setMessage({ text: `Connected. ${result.models.length} models available.` });
        await providers.load();
        await models.load();
      } else {
        // A judge code's failure comes worded (providers/judge.py), as in the CLI and the first run.
        setMessage({ text: result.message || signInError(result.code), error: true });
      }
    } catch (err) {
      setMessage({ text: signInError((err as { code?: string }).code), error: true });
    } finally {
      setBusy('');
    }
  };

  const signOut = async (id: string) => {
    setBusy(id);
    try {
      await request(host.id, 'capture', { text: `/logout ${id}` });
      await providers.load();
    } finally {
      setBusy('');
    }
  };

  const useModel = async (row: ModelRow, asDefault: boolean) => {
    setMessage(null);
    try {
      if (!row.configured) await request(host.id, 'choose_model', { provider: row.provider, model: row.name });
      else await send(host.id, `/model ${row.name}`, true);
      if (asDefault) await request(host.id, 'set_config', { key: 'model', value: row.name });
      await models.load();
      await config.load();
      setMessage({ text: asDefault ? `${row.name} is now the default for new conversations.` : `This conversation now uses ${row.name}.` });
    } catch (err) {
      setMessage({ text: errorText((err as { code?: string }).code), error: true });
    }
  };

  const groups = useMemo(() => {
    const list = providers.data?.providers ?? [];
    return (['cli', 'api', 'local'] as const).map((kind) => ({ kind, rows: list.filter((p) => p.kind === kind) }));
  }, [providers.data]);

  const defaultModel = String(config.data?.values.model ?? '');
  return (
    <>
      <h1>Models and sign-in</h1>
      {message && <p className={message.error ? 'onboarding-error' : 'tone-ok'} role="status">{message.text}</p>}
      <div className="settings-group">
        <h2>Model</h2>
        {models.data === null ? <WorkingRegion label="Loading models" /> : (
          <>
            <Setting label="This conversation">
              <select className="field" value={models.data.current} onChange={(e) => {
                const row = models.data!.models.find((m) => m.name === e.target.value);
                if (row) void useModel(row, false);
              }}>
                {models.data.models.map((m) => <option key={`${m.provider}:${m.name}`} value={m.name}>{m.label ?? m.name}{m.price ? `  ${m.price}` : ''}</option>)}
              </select>
            </Setting>
            <Setting label="New conversations">
              <select className="field" value={defaultModel} onChange={(e) => {
                if (!e.target.value) {
                  void request(host.id, 'set_config', { key: 'model', value: null }).then(config.load);
                  return;
                }
                const row = models.data!.models.find((m) => m.name === e.target.value);
                if (row) void useModel(row, true);
              }}>
                <option value="">The profile's first model</option>
                {models.data.models.map((m) => <option key={`${m.provider}:${m.name}`} value={m.name}>{m.label ?? m.name}</option>)}
              </select>
            </Setting>
          </>
        )}
      </div>
      {groups.map((group) => group.rows.length > 0 && (
        <div key={group.kind} className="settings-group">
          <h2>{{ cli: 'Subscriptions', api: 'API keys', local: 'On this computer' }[group.kind]}</h2>
          {group.rows.map((p) => (
            <div key={p.id} className="provider-row">
              <div className="provider-id">
                {p.ready
                  ? <StatusMark tone="ok" label={providerState(p) || 'Ready'} className="provider-icon" />
                  : <span className="provider-icon">{p.kind === 'cli' ? <IconTerminal2 size={16} stroke={1.5} aria-hidden /> : p.kind === 'local' ? <IconCpu size={16} stroke={1.5} aria-hidden /> : <IconKey size={16} stroke={1.5} aria-hidden />}</span>}
                <div>
                <div className="provider-name">{p.label}</div>
                {/* A check already says it is ready; only a provider that needs something says so. */}
                {!p.ready && <div className="provider-detail">{providerState(p)}</div>}
                </div>
              </div>
              {busy === p.id ? <Working size="sm" label="Working" /> : <span />}
              <div className="button-row">
                <Button size="sm" disabled={!!busy} onClick={() => void signIn(p.id)}>
                  {p.ready ? (p.kind === 'api' ? (p.credential === 'judge_code' ? 'Change code' : 'Change key') : 'Check again') : p.kind === 'local' ? 'Connect' : p.kind === 'api' ? 'Add key' : 'Sign in'}
                </Button>
                {p.ready && p.kind !== 'local' && (
                  <Button variant="ghost" size="sm" disabled={!!busy} onClick={() => void signOut(p.id)}>Sign out</Button>
                )}
              </div>
            </div>
          ))}
        </div>
      ))}
    </>
  );
}

// -- Permissions and budgets ---------------------------------------------------------------

interface ConfigData {
  values: Record<string, unknown>;
  defaults: Record<string, unknown>;
  kinds: Record<string, string>;
  choices: Record<string, string[]>;
  ranges: Record<string, [number | null, number | null]>;
  help: Record<string, string>;
  sources: Record<string, string>;
  errors: string[];
  files: string[];
}

const CONFIG_WORDS: Record<string, { label: string; help?: string }> = {
  permission: { label: 'Permission for new conversations' },
  session_budget_usd: { label: 'Spending limit per conversation ($)' },
  session_minutes: { label: 'Time limit per conversation (minutes)' },
  auto_compact_percent: { label: 'Free up context when it is this full (%)', help: '0 turns it off' },
  web_fetch_allow_private: { label: 'Let Architect fetch pages on your local network' },
};

function PermissionsPane({ host }: { host: Conv }) {
  const request = useApp((s) => s.request);
  const { data, error, setData } = useRequest<ConfigData>(host, 'config');
  const [problem, setProblem] = useState('');
  const keys = ['permission', 'session_budget_usd', 'session_minutes', 'auto_compact_percent', 'web_fetch_allow_private'];
  const set = async (key: string, value: unknown) => {
    setProblem('');
    try {
      setData(await request<ConfigData>(host.id, 'set_config', { key, value }));
    } catch (err) {
      const e = err as { code?: string; detail?: string };
      setProblem(`${CONFIG_WORDS[key]?.label ?? key}: ${e.detail || e.code}`);
    }
  };
  if (error) return <p className="tone-err">{errorText(error)}</p>;
  if (!data) return <WorkingRegion label="Loading" />;
  return (
    <>
      <h1>Permissions and budgets</h1>
      {problem && <p className="onboarding-error">{problem}</p>}
      <div className="settings-group">
        {keys.map((key) => {
          const words = CONFIG_WORDS[key] ?? { label: humanize(key) };
          const value = data.values[key];
          const fromProject = data.sources[key] === 'project';
          const help = [words.help, fromProject ? 'Set by this project\'s .architect/config.toml, which wins here.' : ''].filter(Boolean).join(' ');
          return (
            <Setting key={key} label={words.label} help={help || undefined}>
              {key === 'permission' ? (
                <select className="field" value={String(value ?? 'ask')} onChange={(e) => void set(key, e.target.value)}>
                  {(data.choices[key] ?? ['ask', 'auto-edit', 'full-auto', 'plan']).map((c) => <option key={c} value={c}>{PERMISSION_LABELS[c]?.label ?? c}</option>)}
                </select>
              ) : data.kinds[key] === 'bool' ? (
                <input type="checkbox" checked={value === true} onChange={(e) => void set(key, e.target.checked ? 'true' : 'false')} />
              ) : (
                <Input
                  size="sm"
                  className="num"
                  aria-label={words.label}
                  key={String(value)}
                  defaultValue={value === null || value === undefined ? '' : String(value)}
                  placeholder={String(data.defaults[key] ?? '')}
                  inputMode="decimal"
                  onBlur={(e) => {
                    if (e.target.value !== String(value ?? '')) void set(key, e.target.value.trim() || null);
                  }}
                  onKeyDown={(e) => e.key === 'Enter' && (e.target as HTMLInputElement).blur()}
                />
              )}
            </Setting>
          );
        })}
      </div>
      <p className="tone-faint small">Saved to {data.files[0]}.</p>
    </>
  );
}

// -- Presets and roles ---------------------------------------------------------------------

interface Preset { name: string; description: string; components: string[]; tunable_params: Record<string, { values: unknown[]; default: unknown }>; spawnable: boolean }
interface ProfileData {
  path: string;
  editable: boolean;
  models: Record<string, { provider?: string; model?: string }>;
  compile: { model_ladder?: string[]; compile_budget?: { cost_usd?: number; compute_s?: number }; max_candidates?: number };
  forge: Record<string, unknown>;
  roles: { policy?: string; models?: Record<string, string | null>; builder_model?: string | null };
}

const FORGE_FIELDS: Array<{ key: string; label: string; kind: 'bool' | 'number' | 'choice'; choices?: string[] }> = [
  { key: 'enabled', label: 'Use Forge when building agents', kind: 'bool' },
  { key: 'mode', label: 'How Forge works', kind: 'choice', choices: ['agent', 'template'] },
  { key: 'trigger', label: 'When Forge runs', kind: 'choice', choices: ['always', 'failed'] },
  { key: 'max_usd', label: 'Forge spending limit (dollars)', kind: 'number' },
  { key: 'max_s', label: 'Forge time limit (seconds)', kind: 'number' },
  { key: 'max_submissions', label: 'Submissions', kind: 'number' },
  { key: 'max_measures', label: 'Measures', kind: 'number' },
  { key: 'max_spawns', label: 'Sub-harnesses', kind: 'number' },
];

const ROLE_WORDS: Record<string, string> = {
  inference: 'Writes the spec from your description',
  decomposition: 'Splits big jobs into steps',
  rewrite: 'Rewrites a harness that fell short',
  forge_agent: 'Forge, the harness engineer',
};

function PresetsPane({ host }: { host: Conv }) {
  const request = useApp((s) => s.request);
  const presets = useRequest<{ presets: Preset[] }>(host, 'presets');
  const profile = useRequest<ProfileData>(host, 'profile');
  const [problem, setProblem] = useState('');
  const set = async (path: string[], value: unknown) => {
    setProblem('');
    try {
      profile.setData(await request<ProfileData>(host.id, 'set_profile', { path, value }));
    } catch (err) {
      const e = err as { code?: string; detail?: string };
      setProblem(e.code === 'invalid_profile' ? `That value was not accepted (${e.detail}). Nothing changed.` : errorText(e.code));
    }
  };
  const setRole = async (role: string, model: string | null) => {
    setProblem('');
    try {
      profile.setData(await request<ProfileData>(host.id, 'set_role', { role, model }));
    } catch (err) {
      const e = err as { code?: string; detail?: string };
      setProblem(e.code === 'invalid_profile' ? `That was not accepted (${e.detail}). Nothing changed.` : errorText(e.code));
    }
  };
  const p = profile.data;
  const models = p ? Object.keys(p.models) : [];
  return (
    <>
      <h1>Presets and roles</h1>
      {problem && <p className="onboarding-error">{problem}</p>}
      {p && (
        <div className="settings-group">
          <h2>Architect's own roles</h2>
          {!p.editable && <p className="tone-mute">This profile is a TOML file; edit it directly ({p.path}).</p>}
          {Object.entries(ROLE_WORDS).map(([role, words]) => (
            <Setting key={role} label={words}>
              <select className="field" disabled={!p.editable} value={String(p.roles.models?.[role] ?? '')} onChange={(e) => void setRole(role, e.target.value || null)}>
                <option value="">Default</option>
                {models.map((m) => <option key={m} value={m}>{m}</option>)}
              </select>
            </Setting>
          ))}
          <Setting label="How models are picked for these roles">
            <select className="field" disabled={!p.editable} value={String(p.roles.policy ?? 'pinned')} onChange={(e) => void set(['roles', 'policy'], e.target.value)}>
              <option value="pinned">Pinned</option>
              <option value="evidence">Evidence</option>
            </select>
          </Setting>
        </div>
      )}
      {p && (
        <div className="settings-group">
          <h2>Building agents</h2>
          <Setting label="Models to try, cheapest first" help="Comma separated">
            <Input
              size="sm"
              aria-label="Models to try"
              disabled={!p.editable}
              key={(p.compile.model_ladder ?? []).join(',')}
              defaultValue={(p.compile.model_ladder ?? []).join(', ')}
              onBlur={(e) => {
                const list = e.target.value.split(',').map((s) => s.trim()).filter(Boolean);
                if (list.join(',') !== (p.compile.model_ladder ?? []).join(',')) void set(['compile', 'model_ladder'], list);
              }}
            />
          </Setting>
          <Setting label="Build spending limit (dollars)">
            <NumberInput disabled={!p.editable} value={p.compile.compile_budget?.cost_usd} onCommit={(v) => void set(['compile', 'compile_budget', 'cost_usd'], v)} />
          </Setting>
          <Setting label="Build time limit (seconds)">
            <NumberInput disabled={!p.editable} value={p.compile.compile_budget?.compute_s} onCommit={(v) => void set(['compile', 'compile_budget', 'compute_s'], v)} />
          </Setting>
          {FORGE_FIELDS.map((f) => (
            <Setting key={f.key} label={f.label}>
              {f.kind === 'bool' ? (
                <input type="checkbox" disabled={!p.editable} checked={p.forge[f.key] === true} onChange={(e) => void set(['compile', 'forge', f.key], e.target.checked)} />
              ) : f.kind === 'choice' ? (
                <select className="field" disabled={!p.editable} value={String(p.forge[f.key] ?? '')} onChange={(e) => void set(['compile', 'forge', f.key], e.target.value)}>
                  {f.choices!.map((c) => <option key={c} value={c}>{c === 'agent' ? 'An agent writes the harness' : c === 'template' ? 'Use a template, no agent' : c === 'always' ? 'Every build' : 'Only when the preset falls short'}</option>)}
                </select>
              ) : (
                <NumberInput disabled={!p.editable} value={p.forge[f.key] as number | undefined} onCommit={(v) => void set(['compile', 'forge', f.key], v)} />
              )}
            </Setting>
          ))}
        </div>
      )}
      <div className="settings-group">
        <h2>Presets</h2>
        <p className="tone-faint small">Fixed designs every agent starts from; each build tunes them within these ranges.</p>
        {presets.data === null ? <WorkingRegion label="Loading" /> : presets.data.presets.map((preset) => (
          <div key={preset.name} className="preset-card">
            <h3>{preset.name}</h3>
            {preset.description && <p className="tone-mute small">{preset.description}</p>}
            <div className="preset-meta">
              {preset.spawnable && <Badge>Can run on its own</Badge>}
              {preset.components.map((c) => <Badge key={c}>{c}</Badge>)}
            </div>
            {Object.keys(preset.tunable_params).length > 0 && (
              <dl className="kv">
                {Object.entries(preset.tunable_params).map(([name, span]) => (
                  <div key={name} className="kv-row"><dt>{humanize(name)}</dt><dd className="mono small">{span.values.map(String).join(', ')} (starts at {String(span.default)})</dd></div>
                ))}
              </dl>
            )}
          </div>
        ))}
      </div>
    </>
  );
}

function NumberInput({ value, onCommit, disabled }: { value: number | undefined | null; onCommit: (v: number) => void; disabled?: boolean }) {
  return (
    <Input
      size="sm"
      className="num"
      disabled={disabled}
      key={String(value)}
      defaultValue={value === undefined || value === null ? '' : String(value)}
      inputMode="decimal"
      onBlur={(e) => {
        const n = Number(e.target.value);
        if (e.target.value.trim() && Number.isFinite(n) && n !== value) onCommit(n);
      }}
      onKeyDown={(e) => e.key === 'Enter' && (e.target as HTMLInputElement).blur()}
    />
  );
}

// -- Tools (MCP) ---------------------------------------------------------------------------

interface McpServer { name: string; status: string; transport?: string; target?: string; tools?: number; error?: string; enabled: boolean; tool_names: string[] }

function ToolsPane({ host }: { host: Conv }) {
  const request = useApp((s) => s.request);
  const mcp = useRequest<{ servers: McpServer[]; problems: string[] }>(host, 'mcp');
  const [name, setName] = useState('');
  const [target, setTarget] = useState('');
  const [busy, setBusy] = useState(false);
  const [output, setOutput] = useState<Array<{ kind: string; text?: string; level?: string }>>([]);
  const run = async (text: string) => {
    setBusy(true);
    try {
      const result = await request<{ events: Array<{ kind: string; text?: string; level?: string }> }>(host.id, 'capture', { text });
      setOutput(result.events.filter((e) => e.kind === 'notice'));
      await mcp.load();
    } catch (err) {
      setOutput([{ kind: 'notice', level: 'error', text: errorText((err as { code?: string }).code) }]);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <h1>Tools</h1>
      {output.map((o, i) => <p key={i} className={o.level === 'error' ? 'tone-err' : o.level === 'warn' ? 'tone-warn' : 'tone-mute'}>{o.text}</p>)}
      <div className="settings-group">
        {mcp.data === null ? <WorkingRegion label="Loading" /> : mcp.data.servers.length === 0 ? <p className="tone-faint">No servers yet.</p> : mcp.data.servers.map((s) => (
          <div key={s.name} className={clsx('provider-row', !s.enabled && 'is-off')}>
            <div>
              <div className="provider-name">{s.name}</div>
              <div className="provider-detail">
                {/* Off shows as the dimmed row and the Turn on button. */}
                {s.enabled ? (s.status === 'ready' ? `${s.tools ?? s.tool_names.length} tools` : s.error ? `Not working: ${s.error}` : s.status) : ''}
                {s.target ? `  ${s.target}` : ''}
              </div>
            </div>
            <span />
            <div className="button-row">
              <Button size="sm" disabled={busy} onClick={() => void run(`/mcp ${s.enabled ? 'disable' : 'enable'} ${s.name}`)}>{s.enabled ? 'Turn off' : 'Turn on'}</Button>
              <Button variant="ghost" size="sm" disabled={busy} onClick={() => void run(`/mcp restart ${s.name}`)}>Restart</Button>
              <Button variant="destructive" size="sm" disabled={busy} onClick={() => void run(`/mcp remove ${s.name}`)}>Remove</Button>
            </div>
          </div>
        ))}
      </div>
      <form
        className="settings-group"
        onSubmit={(e) => {
          e.preventDefault();
          if (!name.trim() || !target.trim()) return;
          const isUrl = /^https?:\/\//.test(target.trim());
          void run(`/mcp add ${name.trim()} ${isUrl ? `--url ${target.trim()}` : target.trim()}`).then(() => {
            setName('');
            setTarget('');
          });
        }}
      >
        <h2>Add a server</h2>
        <Setting label="Name"><Input size="sm" aria-label="Name" value={name} onChange={(e) => setName(e.target.value.replace(/\s/g, '-'))} placeholder="github" /></Setting>
        <Setting label="Command or URL"><Input size="sm" aria-label="Command or URL" value={target} onChange={(e) => setTarget(e.target.value)} placeholder="npx -y @modelcontextprotocol/server-github" /></Setting>
        <div className="button-row"><Button type="submit" variant="primary" size="sm" disabled={busy || !name || !target}>Add</Button></div>
      </form>
    </>
  );
}

// -- Instructions --------------------------------------------------------------------------

function InstructionsPane({ host }: { host: Conv }) {
  const [scope, setScope] = useState<'project' | 'global'>('project');
  const request = useApp((s) => s.request);
  const memory = useRequest<{ path: string; text: string; exists: boolean }>(host, 'memory', { scope });
  const commands = useRequest<{ commands: Array<{ name: string; summary: string; custom: boolean }> }>(host, 'commands');
  const [text, setText] = useState('');
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    if (memory.data) setText(memory.data.text);
  }, [memory.data]);
  const dirty = memory.data ? text !== memory.data.text : false;
  return (
    <>
      <h1>Instructions</h1>
      <Segmented
        kind="tabs"
        label="Which instructions"
        options={[{ value: 'project', label: 'This project' }, { value: 'global', label: 'Everywhere' }]}
        value={scope}
        onChange={(v) => setScope(v as 'project' | 'global')}
        className="settings-scope"
      />
      {memory.data === null ? <WorkingRegion label="Loading" /> : (
        <>
          <p className="tone-faint small mono">{memory.data.path}{memory.data.exists ? '' : ' (not created yet)'}</p>
          <TextArea className="memory-editor" aria-label="Instructions" value={text} onChange={(e) => { setText(e.target.value); setSaved(false); }} spellCheck={false} />
          <div className="button-row">
            <Button
              variant="primary"
              size="sm"
              disabled={!dirty}
              onClick={() => void request(host.id, 'save_memory', { scope, text }).then(() => { setSaved(true); void memory.load(); })}
            >
              Save
            </Button>
            {saved && <span className="tone-ok">Saved. Architect uses it from the next message.</span>}
          </div>
        </>
      )}
      <div className="settings-group">
        <h2>Your commands</h2>
        <p className="tone-faint small">Markdown files in .architect/commands become /commands.</p>
        {(commands.data?.commands ?? []).filter((c) => c.custom).map((c) => (
          <div key={c.name} className="kbd-row"><span className="mono">{c.name}</span><span className="tone-faint">{c.summary}</span></div>
        ))}
      </div>
    </>
  );
}

// -- Appearance, shortcuts, health, advanced -----------------------------------------------

function AppearancePane() {
  const theme = useApp((s) => s.prefs.theme);
  const setPrefs = useApp((s) => s.setPrefs);
  return (
    <>
      <h1>Appearance</h1>
      <Setting label="Theme">
        <Segmented
          label="Theme"
          options={(['system', 'light', 'dark'] as Theme[]).map((t) => ({ value: t, label: { system: 'Match the system', light: 'Light', dark: 'Dark' }[t] }))}
          value={theme}
          onChange={(v) => setPrefs({ theme: v as Theme })}
        />
      </Setting>
    </>
  );
}

/**
 * No shortcut changed yet. One shared empty map: a new `{}` from the store selector on
 * every read would rerender the pane without end.
 */
const NO_BINDINGS: Record<string, Shortcut> = {};

function ShortcutsPane() {
  const bindings = useApp((st) => st.prefs.keybindings ?? NO_BINDINGS);
  const setPrefs = useApp((st) => st.setPrefs);
  const [query, setQuery] = useState('');
  const [byKeys, setByKeys] = useState<Shortcut | null>(null);
  const [listening, setListening] = useState(false);
  const [recording, setRecording] = useState<keyof typeof SHORTCUTS | null>(null);
  const [conflict, setConflict] = useState<string | null>(null);
  const [ask, dialog] = useConfirm();
  const extra: Array<[string, string]> = [
    ['Send', 'Enter'],
    ['New line', 'Shift+Enter'],
    ['Switch between Chat, Build and Plan', 'Shift+Tab'],
    ['Stop the current turn', 'Esc'],
    ['Allow an approval once', 'Enter'],
    ['Decline an approval', 'Esc'],
    ['Commands', '/'],
    ['Attach a file from the folder', '@'],
    ['Bring back the last message', 'Up'],
  ];
  const names = (Object.keys(SHORTCUTS) as Array<keyof typeof SHORTCUTS>).filter((k) => !SHORTCUT_ALIASES[k]);
  const aliasesOf = (k: keyof typeof SHORTCUTS) => (Object.keys(SHORTCUT_ALIASES) as Array<keyof typeof SHORTCUTS>).filter((a) => SHORTCUT_ALIASES[a] === k);
  const shown = names.filter((k) => {
    if (byKeys) return [k, ...aliasesOf(k)].some((n) => sameShortcut(SHORTCUTS[n], byKeys));
    return !query || `${SHORTCUT_NAMES[k]} ${shortcutLabel(SHORTCUTS[k])}`.toLowerCase().includes(query.toLowerCase());
  });

  // Capture the next keystroke, for a new binding or for searching by keys.
  useEffect(() => {
    if (!recording && !listening) return;
    recordingKeys.on = true;
    const onKey = (event: KeyboardEvent) => {
      event.preventDefault();
      event.stopPropagation();
      if (event.key === 'Escape' && !event.ctrlKey && !event.metaKey && !event.shiftKey) {
        setRecording(null);
        setListening(false);
        return;
      }
      const next = shortcutFromEvent(event);
      if (!next) return;
      if (listening) {
        setByKeys(next);
        setListening(false);
        return;
      }
      if (!recording) return;
      if (!next.mod && !next.ctrl && !next.alt && next.key.length === 1) {
        setConflict('Use a modifier (Ctrl, Alt or the Command key) so typing is never taken over.');
        return;
      }
      const taken = (Object.keys(SHORTCUTS) as Array<keyof typeof SHORTCUTS>).find((n) => n !== recording && SHORTCUT_ALIASES[n] !== recording && sameShortcut(SHORTCUTS[n], next));
      setConflict(taken ? `${shortcutLabel(next)} was ${SHORTCUT_NAMES[taken]}; that one is now unbound.` : null);
      const updated: Record<string, Shortcut> = { ...bindings, [recording]: next };
      if (taken) updated[taken] = { key: '' };
      setPrefs({ keybindings: updated });
      setRecording(null);
    };
    window.addEventListener('keydown', onKey, true);
    return () => {
      window.removeEventListener('keydown', onKey, true);
      recordingKeys.on = false;
    };
  }, [recording, listening, bindings, setPrefs]);

  const reset = (k?: keyof typeof SHORTCUTS) => {
    if (!k) setPrefs({ keybindings: {} });
    else {
      const next = { ...bindings };
      delete next[k];
      setPrefs({ keybindings: next });
    }
    setConflict(null);
  };

  return (
    <>
      <h1>Keyboard shortcuts</h1>
      <div className="kbd-tools">
        <label className="kbd-search ui-field-frame">
          <IconSearch size={14} stroke={1.5} aria-hidden />
          <input value={byKeys ? shortcutLabel(byKeys) : query} readOnly={!!byKeys} placeholder="Search shortcuts" onChange={(e) => setQuery(e.target.value)} />
          {(byKeys || query) && <IconButton icon={IconX} label="Clear" tooltip={false} onClick={() => { setByKeys(null); setQuery(''); }} />}
        </label>
        <Button size="sm" aria-pressed={listening} className={clsx(listening && 'is-on')} onClick={() => { setByKeys(null); setListening(!listening); }}>
          <IconKeyboard size={14} stroke={1.5} aria-hidden /> {listening ? 'Press the keys' : 'Search by keys'}
        </Button>
        <Button variant="ghost" size="sm" disabled={!Object.keys(bindings).length} onClick={() => void ask({ title: 'Put every shortcut back as Architect ships it?', keep: 'Cancel', action: 'Reset all' }).then((ok) => ok && reset())}>
          Reset all
        </Button>
      </div>
      {conflict && <p className="kbd-conflict">{conflict}</p>}
      <div className="kbd-list">
        {shown.map((k) => {
          const custom = k in bindings;
          const unbound = !SHORTCUTS[k].key;
          return (
            <div key={k} className={clsx('kbd-row', recording === k && 'is-recording')}>
              <span>{SHORTCUT_NAMES[k]}</span>
              <span className="kbd-keys">
                {custom && <Button variant="link" size="xs" onClick={() => reset(k)}>Reset</Button>}
                <button type="button" className="kbd-edit" onClick={() => { setConflict(null); setRecording(recording === k ? null : k); }} data-tip={recording === k ? undefined : 'Change'}>
                  {recording === k ? <span className="kbd-recording">Press the new keys…</span> : <kbd className={clsx(custom && 'is-custom')}>{unbound ? 'None' : shortcutLabel(SHORTCUTS[k])}</kbd>}
                </button>
                {aliasesOf(k).map((a) => <kbd key={a}>{shortcutLabel(SHORTCUTS[a])}</kbd>)}
              </span>
            </div>
          );
        })}
        {!byKeys && !query && extra.map(([name, key]) => <div key={name} className="kbd-row is-fixed"><span>{name}</span><kbd>{key}</kbd></div>)}
        {shown.length === 0 && <p className="tone-faint small">No shortcut matches.</p>}
      </div>
      {dialog}
    </>
  );
}

function HealthPane({ host }: { host: Conv }) {
  const { events, run } = useCapture(host, '/doctor');
  return (
    <>
      <h1>Health check</h1>
      <div className="button-row"><Button size="sm" onClick={() => void run()}>Check again</Button></div>
      <CapturedView events={events} empty="Nothing to report." />
    </>
  );
}

function AdvancedPane() {
  const prefs = useApp((s) => s.prefs);
  const setPrefs = useApp((s) => s.setPrefs);
  const meta = useApp((s) => s.meta);
  return (
    <>
      <h1>Advanced</h1>
      <Setting label="Workspace folder">
        <div className="button-row">
          <span className="mono small">{prefs.workspace ?? 'Default (~/.architect)'}</span>
          <Button size="sm" onClick={() => void chooseFolder(prefs.workspace ?? meta.homedir).then((f) => f && setPrefs({ workspace: f }))}>Change</Button>
          {prefs.workspace && <Button variant="ghost" size="sm" onClick={() => setPrefs({ workspace: undefined })}>Use the default</Button>}
        </div>
      </Setting>
      <Setting label="Profile file">
        <Input size="sm" aria-label="Profile file" key={prefs.profile ?? ''} defaultValue={prefs.profile ?? ''} onBlur={(e) => setPrefs({ profile: e.target.value.trim() || undefined })} />
      </Setting>
      <Setting label="Check the sign-in on first run">
        <input type="checkbox" checked={prefs.probe} onChange={(e) => setPrefs({ probe: e.target.checked })} />
      </Setting>
      <Setting label="Architect checkout"><span className="mono small">{meta.root}</span></Setting>
      <Setting label="App files"><span className="mono small">{meta.home}</span></Setting>
    </>
  );
}

function ArchivedPane() {
  const sessions = useApp((s) => s.sessions);
  const archived = useApp((s) => s.prefs.archived);
  const unarchive = useApp((s) => s.unarchive);
  const deleteSession = useApp((s) => s.deleteSession);
  const rows = sessions.filter((s) => archived.includes(s.id));
  const [ask, dialog] = useConfirm();
  return (
    <>
      <h1>Archived</h1>
      <div className="settings-group">
        {rows.length === 0 ? <p className="tone-faint archived-empty">Nothing archived.</p> : rows.map((row) => (
          <div key={row.id} className="provider-row">
            <div className="provider-id">
              <span className="provider-icon"><IconArchive size={16} stroke={1.5} aria-hidden /></span>
              <div>
                <div className="provider-name">{row.title}</div>
                <div className="provider-detail">{row.cwd ? `${row.cwd.split(/[\\/]/).filter(Boolean).pop()}, ` : ''}{new Date(row.time * 1000).toLocaleDateString()}</div>
              </div>
            </div>
            <span />
            <div className="button-row">
              <Button size="sm" onClick={() => unarchive(row.id)}>Unarchive</Button>
              <IconButton icon={IconTrash} label="Delete" onClick={() => void ask({ title: `Delete ${row.title} from this computer for good?`, keep: 'Keep it', action: 'Delete' }).then((ok) => ok && deleteSession(row.id))} />
            </div>
          </div>
        ))}
      </div>
      {dialog}
    </>
  );
}

interface WorktreeRow { path: string; branch: string | null; main: boolean; architect: boolean }

function GitPane({ host }: { host?: Conv }) {
  const prefs = useApp((s) => s.prefs);
  const setPrefs = useApp((s) => s.setPrefs);
  const [rows, setRows] = useState<WorktreeRow[] | null>(null);
  const cwd = host?.state.cwd ?? prefs.folders[0] ?? '';
  const load = useCallback(() => {
    if (!cwd) return;
    void fetch('/api/git/worktrees?cwd=' + encodeURIComponent(cwd)).then((r) => r.json()).then((r: { worktrees: WorktreeRow[] }) => setRows(r.worktrees)).catch(() => setRows([]));
  }, [cwd]);
  useEffect(load, [load]);
  const main = rows?.find((r) => r.main);
  const [ask, dialog] = useConfirm();
  const remove = (row: WorktreeRow) => {
    void ask({ title: `Remove the worktree ${row.branch ?? row.path.split(/[\\/]/).pop()}?`, keep: 'Keep it', action: 'Remove' }).then(async (ok) => {
      if (!ok) return;
      const r = await fetch('/api/git/worktree-remove', { method: 'POST', body: JSON.stringify({ cwd: main?.path ?? cwd, path: row.path, force: true }) });
      if (!r.ok) useApp.getState().showToast('The worktree could not be removed.', 'error');
      load();
    });
  };
  return (
    <>
      <h1>Git and worktrees</h1>
      <div className="settings-group">
        <Setting label="Start new conversations in a worktree" help="Each gets its own branch and folder, so parallel work never collides">
          <input type="checkbox" checked={prefs.worktreeDefault} onChange={(e) => setPrefs({ worktreeDefault: e.target.checked })} />
        </Setting>
        <Setting label="Branch prefix">
          <Input size="sm" aria-label="Branch prefix" value={prefs.branchPrefix} onChange={(e) => setPrefs({ branchPrefix: e.target.value.replace(/\s+/g, '-') })} />
        </Setting>
        <Setting label="Remove a conversation's worktree when it is archived" help="Worktrees with uncommitted work are kept">
          <input type="checkbox" checked={prefs.removeWorktreeOnArchive} onChange={(e) => setPrefs({ removeWorktreeOnArchive: e.target.checked })} />
        </Setting>
        <Setting label="Keep only the newest worktrees" help="Open and pinned conversations, and worktrees with uncommitted work, are always kept">
          <span className="inline-controls">
            <Input type="number" size="sm" aria-label="Worktrees to keep" className="num worktree-limit" min={1} max={100} value={prefs.worktreeLimit} disabled={!prefs.pruneWorktrees} onChange={(e) => setPrefs({ worktreeLimit: Math.max(1, Math.min(100, Number(e.target.value) || 15)) })} />
            <input type="checkbox" checked={prefs.pruneWorktrees} onChange={(e) => setPrefs({ pruneWorktrees: e.target.checked })} aria-label="Remove older worktrees automatically" />
          </span>
        </Setting>
      </div>
      <div className="settings-group">
        <h2>Worktrees{main ? ` of ${main.path.split(/[\\/]/).pop()}` : ''}</h2>
        {rows === null ? <WorkingRegion label="Loading" /> : rows.length === 0 ? <p className="tone-faint archived-empty">This folder isn't a git repository.</p> : rows.map((row) => (
          <div key={row.path} className="provider-row">
            <div className="provider-id">
              <span className={row.main ? 'provider-icon is-ready' : 'provider-icon'}>{row.main ? <IconGitBranch size={16} stroke={1.5} aria-hidden /> : <IconGitFork size={16} stroke={1.5} aria-hidden />}</span>
              <div>
                <div className="provider-name mono">{row.branch ?? 'detached'}</div>
                <div className="provider-detail">{row.main ? 'Your checkout' : row.path}</div>
              </div>
            </div>
            <span />
            {!row.main && <Button variant="ghost" size="sm" onClick={() => remove(row)}>Remove</Button>}
          </div>
        ))}
        <p className="tone-faint small">A .worktreeinclude file in the repository lists ignored files (like .env) to copy into new worktrees.</p>
      </div>
      {dialog}
    </>
  );
}

function Seg<T extends string>({ label, value, options, onChange }: { label: string; value: T; options: Array<[T, string]>; onChange: (v: T) => void }) {
  return <Segmented label={label} options={options.map(([id, words]) => ({ value: id, label: words }))} value={value} onChange={(v) => onChange(v as T)} />;
}

function GeneralPane() {
  const prefs = useApp((s) => s.prefs);
  const setPrefs = useApp((s) => s.setPrefs);
  const [list, setList] = useState<ShellInfo[]>([]);
  const [permission, setPermission] = useState(() => (typeof Notification === 'undefined' ? 'denied' : Notification.permission));
  useEffect(() => {
    void shells().then(setList);
  }, []);
  const mod = isMac() ? '⌘' : 'Ctrl';
  return (
    <>
      <h1>General</h1>
      <div className="settings-group">
        <h2>Composer</h2>
        <Setting label="Send with">
          <Seg label="Send with" value={prefs.sendKey} onChange={(sendKey) => setPrefs({ sendKey })} options={[['enter', 'Enter'], ['mod-enter', `${mod}+Enter`]]} />
        </Setting>
        <Setting label="A message sent while Architect works" help={`${mod}+Shift+Enter does the other`}>
          <Seg label="A message sent while Architect works" value={prefs.followUp} onChange={(followUp) => setPrefs({ followUp })} options={[['steer', 'Steers it'], ['queue', 'Waits its turn']]} />
        </Setting>
        <Setting label="Steps and command output" help={`${isMac() ? '⌃' : 'Ctrl+Alt+'}O switches in a conversation`}>
          <Seg label="Steps and command output" value={prefs.verbose ? 'all' : 'folded'} onChange={(v) => setPrefs({ verbose: v === 'all' })} options={[['folded', 'Folded'], ['all', 'Every step and result']]} />
        </Setting>
        <Setting label="Suggested prompts on a new conversation">
          <input type="checkbox" checked={prefs.suggestions} onChange={(e) => setPrefs({ suggestions: e.target.checked })} />
        </Setting>
      </div>
      <div className="settings-group">
        <h2>Notifications</h2>
        <Setting label="When a turn finishes">
          <Seg label="When a turn finishes" value={prefs.notifyTurns} onChange={(notifyTurns) => setPrefs({ notifyTurns })} options={[['never', 'Never'], ['background', "When I'm away"], ['always', 'Always']]} />
        </Setting>
        <Setting label="When Architect needs an approval or an answer">
          <input type="checkbox" checked={prefs.notifyAsks} onChange={(e) => setPrefs({ notifyAsks: e.target.checked })} />
        </Setting>
        {permission === 'default' && (
          <Setting label="Notifications are off for this app">
            <Button size="sm" onClick={() => void Notification.requestPermission().then(setPermission)}>Turn on</Button>
          </Setting>
        )}
      </div>
      <div className="settings-group">
        <h2>While working</h2>
        <Setting label="Keep the computer awake while a turn runs">
          <input type="checkbox" checked={prefs.preventSleep} onChange={(e) => setPrefs({ preventSleep: e.target.checked })} />
        </Setting>
      </div>
      <div className="settings-group">
        <h2>Terminal</h2>
        <Setting label="Terminals open">
          <Seg label="Terminals open" value={prefs.terminalPlace} onChange={(terminalPlace) => setPrefs({ terminalPlace })} options={[['right', 'On the right'], ['bottom', 'At the bottom']]} />
        </Setting>
        <Setting label="Default shell">
          <select className="field" value={prefs.terminalShell ?? ''} onChange={(e) => setPrefs({ terminalShell: e.target.value || undefined })}>
            <option value="">{list.find((sh) => sh.id === 'powershell')?.label ?? list[0]?.label ?? 'System default'}</option>
            {list.filter((sh) => sh.id !== 'powershell').map((sh) => <option key={sh.id} value={sh.id}>{sh.label}</option>)}
          </select>
        </Setting>
      </div>
    </>
  );
}

function AboutPane() {
  const meta = useApp((s) => s.meta);
  const [v, setV] = useState<{ app: string; electron: string; chrome: string; node: string } | null>(null);
  const { copied, copy } = useCopy();
  useEffect(() => {
    void native()?.version?.().then(setV);
  }, []);
  const lines = [
    ['Architect', v?.app ?? '0.1.0'],
    ['Electron', v?.electron],
    ['Chromium', v?.chrome],
    ['Node', v?.node],
    ['Python core', meta.root],
    ['Platform', meta.platform],
  ].filter((row): row is [string, string] => !!row[1]);
  return (
    <>
      <h1>About</h1>
      <div className="about-card">
        <Mark className="about-mark-svg text-accent" />
        <div>
          <div className="about-name">Architect</div>
          <div className="about-version num">Version {v?.app ?? '0.1.0'}</div>
        </div>
        <span className="spacer" />
        <Button size="sm" onClick={() => copy(lines.map(([k, val]) => `${k}: ${val}`).join('\n'))}>
          {copied ? <IconCheck size={14} stroke={1.5} aria-hidden /> : <IconCopy size={14} stroke={1.5} aria-hidden />} {copied ? 'Copied' : 'Copy details'}
        </Button>
        <Button size="sm" onClick={() => openLink('https://github.com/echoo19/architect-nemotron/releases')}>
          <IconDownload size={14} stroke={1.5} aria-hidden /> Check for updates
        </Button>
      </div>
      <div className="settings-group about-rows">
        {/* The version is on the card above; Copy details still includes it. */}
        {lines.filter(([k]) => k !== 'Architect').map(([k, val]) => (
          <div key={k} className="about-row"><span>{k}</span><span className="mono">{val}</span></div>
        ))}
      </div>
    </>
  );
}
