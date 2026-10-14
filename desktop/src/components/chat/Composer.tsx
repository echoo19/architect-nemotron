/**
 * Where the person types. Enter sends (or queues while a turn runs), Shift+Enter
 * starts a new line, Shift+Tab cycles Chat, Build and Plan, Escape stops the
 * turn, `/` opens the command menu, `@` attaches a file from the folder, and Up
 * in an empty box brings back the last message. The bar under it holds what the
 * terminal's toolbar shows: mode, permission, model, context and spend.
 */
import { IconArrowsMinimize, IconArrowUp, IconMessage, IconChevronDown, IconChevronUp, IconListDetails, IconPaperclip, IconPhoto, IconPlayerStopFilled, IconPlus, IconShieldCheck, IconSparkles, IconX } from '@tabler/icons-react';
import clsx from 'clsx';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { PERMISSION_ICONS } from '../../lib/icons';
import { prettyModel, rungName } from '../../lib/models';
import { native } from '../../lib/native';
import { useDropSide } from '../../lib/useDropSide';
import { isMac, shortcutLabel, SHORTCUTS } from '../../lib/shortcuts';
import { commandWords } from '../../lib/vocab';
import { useApp, type Conv } from '../../store/app';
import { Menu, type MenuItem } from '../ui/Menu';
import { WorkingMark } from '../ui/Working';

interface CommandRow { name: string; usage: string; summary: string; custom: boolean }

const commandCache = new Map<string, CommandRow[]>();

/**
 * The commands a typed "/q" lists: names that start with q, else names that hold it, and
 * only when no name matches at all, the commands whose words mention it. So "/ma" lists
 * only /make, never every command whose description happens to contain "ma".
 */
export function slashMatches<C extends { name: string; summary: string }>(commands: C[], query: string): C[] {
  const q = query.toLowerCase();
  if (!q) return commands;
  const bare = (c: C) => c.name.replace(/^\//, '').toLowerCase();
  const starts = commands.filter((c) => bare(c).startsWith(q));
  if (starts.length) return starts;
  const holds = commands.filter((c) => bare(c).includes(q));
  if (holds.length) return holds;
  return commands.filter((c) => c.summary.toLowerCase().includes(q));
}

/** Commands the app answers itself, listed with the core's. */
const APP_COMMANDS: CommandRow[] = [
  { name: 'fork', usage: '/fork', summary: 'Copy this conversation into a new one', custom: false },
  { name: 'settings', usage: '/settings', summary: 'Open Settings', custom: false },
  { name: 'btw', usage: '/btw <question>', summary: 'Ask a side question; nothing is added to the conversation', custom: false },
];

export const PERMISSION_LABELS: Record<string, { label: string; short: string; detail: string }> = {
  ask: { label: 'Ask before changes', short: 'Ask first', detail: 'Asks before it edits files or runs commands' },
  'auto-edit': { label: 'Edit files freely', short: 'Edits on', detail: 'Edits without asking; still asks before commands' },
  'full-auto': { label: 'Work on its own', short: 'Autonomous', detail: 'Edits and runs commands without asking, where your profile allows commands' },
  plan: { label: 'Read only', short: 'Read only', detail: 'Plans and explains; changes nothing' },
};


/**
 * The modes as the person sees them. Build mode keeps its old key, `forge`, which the
 * bridge and saved conversations use; Forge itself is the optional harness engineer.
 */
export const STATE_LABELS: Record<string, string> = { normal: 'Chat', forge: 'Build', plan: 'Plan' };
const STATES = ['normal', 'forge', 'plan'] as const;

/**
 * `variant`:
 * - 'chat': the full composer, one field frame with its controls in a trailing row.
 * - 'build': under the build card, the field and Send (Stop while a turn runs), nothing else.
 *   It answers the coder, steers or stops a build, and sends `/make @file`. Its placeholder
 *   is `hint`, the caller's word for what typing does at this step.
 * - 'describe': the field of the build's first step, four lines, nothing else. The page
 *   around it holds the examples (they fill it through the draft event) and the step's
 *   primary (it sends as Mod+Enter in the field does).
 * - 'bar': the ask box in a build step's action bar. One line (an input, not a text area),
 *   a chat glyph, the placeholder `hint`, and Send. Never Stop: a step that stops has its
 *   own button, and Escape here only closes a menu.
 */
export type ComposerVariant = 'chat' | 'build' | 'describe' | 'bar';

interface ComposerProps {
  conv: Conv;
  autoFocus?: boolean;
  variant?: ComposerVariant;
  labelledBy?: string;
  /** The placeholder of the build and bar variants. */
  hint?: string;
  /** The bar variant waits (the conversation is starting, or the step says so). */
  disabled?: boolean;
}

/** The examples under the Describe card's field: a click puts one in the field. */
export const DESCRIBE_EXAMPLES = [
  "Read a receipt's text and give back the total, subtotal, tax and item count.",
  'Pull the invoice number, date, vendor and total out of invoice text',
  'Sort support emails into billing, bug report or question',
  'Label a product review as positive, negative or neutral',
];

export function Composer({ conv, autoFocus = true, variant = 'chat', labelledBy, hint = '', disabled = false }: ComposerProps) {
  const full = variant === 'chat';
  // The ask box keeps no draft: the conversation's draft is the Describe field's.
  const bar = variant === 'bar';
  const draft = useApp((s) => (bar ? '' : s.prefs.drafts[conv.id] ?? ''));
  const setDraft = useApp((s) => s.setDraft);
  const send = useApp((s) => s.send);
  const cancel = useApp((s) => s.cancel);
  const request = useApp((s) => s.request);
  const [text, setText] = useState(draft);

  // "Edit and send again" on a message puts its text here.
  useEffect(() => {
    const onDraft = (event: Event) => {
      setText(String((event as CustomEvent).detail ?? ''));
      requestAnimationFrame(() => {
        const el = box.current;
        if (el) {
          el.focus();
          el.setSelectionRange(el.value.length, el.value.length);
        }
      });
    };
    window.addEventListener('architect:draft', onDraft);
    return () => window.removeEventListener('architect:draft', onDraft);
  }, []);
  const [menu, setMenu] = useState<null | 'slash' | 'mention' | 'model' | 'permission' | 'context'>(null);
  const [commands, setCommands] = useState<CommandRow[]>(commandCache.get(conv.id) ?? []);
  const [mentions, setMentions] = useState<string[]>([]);
  const [dragging, setDragging] = useState(false);
  const box = useRef<HTMLTextAreaElement & HTMLInputElement>(null);
  // The / and @ menus open toward the room there is: up from the composer at the bottom,
  // down from the start screen's composer (its heading is above it).
  const shell = useDropSide<HTMLDivElement>(380);
  const state = conv.state;
  const ready = conv.status === 'ready';
  const running = conv.timeline.running || state.busy;
  const asking = conv.timeline.items.some((i) => i.kind === 'ask' && !i.closed);
  // A turn waiting on an answer is not working: Stop shows only while something runs and
  // nothing is asked, so a plan or a question never sits over a Stop square.
  const stoppable = running && !asking;
  // A chat that was switched into build mode can switch back from the build composer.
  const openKind = useApp((st) => st.prefs.open.find((o) => o.id === conv.id)?.kind);
  const canCycle = full || (openKind !== undefined && openKind !== 'forge');

  // The draft is saved as it is typed, so nothing is lost on a crash or a restart.
  useEffect(() => {
    if (bar) return undefined;
    const timer = setTimeout(() => setDraft(conv.id, text), 300);
    return () => clearTimeout(timer);
  }, [text, conv.id, setDraft, bar]);

  useEffect(() => {
    if (autoFocus && !asking) box.current?.focus();
  }, [conv.id, autoFocus, asking]);

  // Grow with the text up to a third of the window. An empty box fits its placeholder, so
  // a narrow column shows the whole hint on two lines instead of cutting it off.
  const fit = useCallback(() => {
    const el = box.current;
    if (!el || bar) return;
    el.style.height = 'auto';
    const height = el.value ? el.scrollHeight : Math.max(el.scrollHeight, placeholderHeight(el));
    el.style.height = `${Math.min(height, Math.round(window.innerHeight / 3))}px`;
  }, [bar]);
  useEffect(() => {
    const el = box.current;
    if (!el || bar || typeof ResizeObserver === 'undefined') return undefined;
    let width = el.clientWidth;
    const observer = new ResizeObserver(() => {
      if (el.clientWidth === width) return;
      width = el.clientWidth;
      fit();
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [fit, bar]);

  const loadCommands = useCallback(async () => {
    if (commandCache.has(conv.id)) return;
    try {
      const result = await request<{ commands: CommandRow[] }>(conv.id, 'commands');
      const names = new Set(result.commands.map((c) => c.name));
      const all = [...result.commands, ...APP_COMMANDS.filter((c) => !names.has(c.name))].sort((x, y) => x.name.localeCompare(y.name));
      commandCache.set(conv.id, all);
      setCommands(all);
    } catch {
      /* the menu stays empty; typing a command still works */
    }
  }, [conv.id, request]);

  const slashQuery = /^\/(\S*)$/.exec(text)?.[1];
  const mentionMatch = /(?:^|\s)@([^\s@]*)$/.exec(text);
  const mentionQuery = mentionMatch?.[1];

  useEffect(() => {
    if (slashQuery !== undefined && ready) {
      void loadCommands();
      setMenu((m) => (m === null || m === 'slash' ? 'slash' : m));
    } else if (mentionQuery !== undefined && ready) {
      setMenu((m) => (m === null || m === 'mention' ? 'mention' : m));
    } else {
      setMenu((m) => (m === 'slash' || m === 'mention' ? null : m));
    }
  }, [slashQuery, mentionQuery, ready, loadCommands]);

  // A bare "/" shows every command only after a short pause, so typing "/make" never
  // flashes the whole list on the way; once letters follow, the menu shows at once.
  const [slashOpen, setSlashOpen] = useState(false);
  useEffect(() => {
    if (menu !== 'slash') {
      setSlashOpen(false);
      return undefined;
    }
    if (slashQuery) {
      setSlashOpen(true);
      return undefined;
    }
    const timer = setTimeout(() => setSlashOpen(true), 180);
    return () => clearTimeout(timer);
  }, [menu, slashQuery]);
  const measureShell = shell.measure;
  useEffect(() => {
    if (menu === 'slash' || menu === 'mention') measureShell();
  }, [menu, measureShell]);

  useEffect(() => {
    if (menu !== 'mention' || mentionQuery === undefined) return;
    let live = true;
    const timer = setTimeout(() => {
      void request<{ paths: string[] }>(conv.id, 'mentions', { query: mentionQuery })
        .then((r) => live && setMentions(r.paths))
        .catch(() => undefined);
    }, 80);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [menu, mentionQuery, conv.id, request]);

  const slashItems: MenuItem[] = useMemo(() => {
    const q = (slashQuery ?? '').toLowerCase();
    return slashMatches(commands.map((c) => ({ ...c, ...commandWords(c) })), q)
      .map((c) => ({ id: c.name, label: c.usage, detail: c.summary, section: c.custom ? 'Your commands' : undefined }));
  }, [commands, slashQuery]);

  const followUp = useApp((st) => st.prefs.followUp);
  // While a turn runs: steer it (read after its current step) or queue for after it.
  const submit = useCallback(async (opposite = false) => {
    const message = text.trim();
    if (!message || !ready) return;
    setText('');
    setDraft(conv.id, '');
    // In the app, settings live on their own screen (Claude Code's /config does the same).
    if (/^\/(config|settings)$/i.test(message)) {
      useApp.getState().setScreen('settings');
      return;
    }
    const side = /^\/(btw|side)(?:\s+([\s\S]*))?$/i.exec(message);
    if (side) {
      if (side[2]?.trim()) void useApp.getState().askSide(conv.id, side[2]);
      else useApp.getState().toggleSide(conv.id, true);
      return;
    }
    if (/^\/model$/i.test(message)) {
      setText('');
      setMenu('model');
      return;
    }
    if (/^\/fork$/i.test(message)) {
      if (conv.state.session_id && conv.state.title) void useApp.getState().forkSession(conv.state.session_id);
      else useApp.getState().showToast('Send a message first; an empty conversation has nothing to fork.', 'error');
      return;
    }
    // A build that needs a tool the computer lacks says so as it starts (the Describe card has no room for it).
    if (variant === 'describe' && (conv.state.unavailable?.length ?? 0) > 0) {
      useApp.getState().showToast(`${conv.state.unavailable!.join(', ')} not installed. Settings has a health check.`, 'error');
    }
    const open = useApp.getState().prefs.open.find((o) => o.id === conv.id);
    if (open?.wantWorktree && !open.worktree && conv.git?.repo && conv.timeline.items.length === 0) {
      await startInWorktree(conv, message);
      return;
    }
    try {
      const steer = running && (followUp === 'steer') !== opposite;
      await send(conv.id, message, false, steer);
    } catch {
      setText(message); // nothing was sent; give it back
    }
  }, [text, ready, conv.id, conv.state.unavailable, variant, send, setDraft, running, followUp]);

  const lastSent = useMemo(() => {
    const users = conv.timeline.items.filter((i) => i.kind === 'user');
    const last = users.at(-1);
    return last && last.kind === 'user' ? last.text : '';
  }, [conv.timeline.items]);

  const cycleState = async () => {
    const current = state.state ?? 'normal';
    const next = STATES[(STATES.indexOf(current) + 1) % STATES.length];
    await request(conv.id, 'set_state', { state: next }).catch(() => undefined);
  };

  const sendKey = useApp((st) => st.prefs.sendKey);
  const onKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement | HTMLInputElement>) => {
    if (menu === 'slash' || menu === 'mention') {
      if (['ArrowDown', 'ArrowUp', 'Enter', 'Tab'].includes(event.key)) return; // the menu handles them
    }
    const modEnter = isMac() ? event.metaKey : event.ctrlKey;
    const sends = sendKey === 'mod-enter' ? modEnter : !event.shiftKey && !modEnter;
    if (event.key === 'Enter' && (sends || (sendKey === 'enter' && modEnter)) && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void submit(event.shiftKey && modEnter);
    } else if (event.key === 'Tab' && event.shiftKey && canCycle) {
      event.preventDefault();
      void cycleState();
    } else if (event.key === 'Escape') {
      if (menu) {
        setMenu(null);
      } else if (running && !bar) {
        event.preventDefault();
        void cancel(conv.id);
      }
    } else if (event.key === 'ArrowUp' && !text && lastSent && !bar) {
      event.preventDefault();
      setText(lastSent);
    }
  };

  const pickSlash = (name: string) => {
    const command = commands.find((c) => c.name === name);
    const takesArgs = command ? /[<[]/.test(command.usage) : false;
    setMenu(null);
    if (takesArgs) {
      setText(`${name} `);
      box.current?.focus();
    } else {
      setText('');
      void send(conv.id, name);
    }
  };

  const pickMention = (path: string) => {
    setText((t) => t.replace(/@([^\s@]*)$/, `@${path.includes(' ') ? `"${path}"` : path} `));
    setMenu(null);
    box.current?.focus();
  };

  const attachPaths = async (paths: string[]) => {
    for (const path of paths) {
      try {
        await request(conv.id, 'attach_image', { path });
      } catch {
        // Not an image: text files inside the folder attach by mention.
        const cwd = state.cwd ?? '';
        const rel = path.startsWith(cwd) ? path.slice(cwd.length).replace(/^[\\/]/, '') : null;
        if (rel) setText((t) => `${t}${t && !t.endsWith(' ') ? ' ' : ''}@${rel} `);
        else useApp.getState().showToast('Only images can be attached from outside this folder.', 'error');
      }
    }
    box.current?.focus();
  };

  // "Attach as context" from a path's menu in the panes.
  const attachRef = useRef(attachPaths);
  attachRef.current = attachPaths;
  useEffect(() => {
    const onAttach = (event: Event) => void attachRef.current([String((event as CustomEvent).detail ?? '')]);
    window.addEventListener('architect:attach', onAttach);
    return () => window.removeEventListener('architect:attach', onAttach);
  }, []);

  const onDrop = (event: React.DragEvent) => {
    event.preventDefault();
    setDragging(false);
    const n = native() as unknown as { pathForFile?: (f: File) => string } | undefined;
    const paths = [...event.dataTransfer.files].map((f) => n?.pathForFile?.(f) ?? '').filter(Boolean);
    if (paths.length) void attachPaths(paths);
    else useApp.getState().showToast('Drop files from your computer to attach them.', 'error');
  };

  const onPaste = (event: React.ClipboardEvent) => {
    const image = [...event.clipboardData.items].find((i) => i.type.startsWith('image/'));
    if (!image) return;
    const file = image.getAsFile();
    if (!file) return;
    event.preventDefault();
    void fetch(`/api/upload?name=${encodeURIComponent(file.name || 'pasted.png')}`, { method: 'POST', body: file })
      .then((r) => r.json() as Promise<{ path?: string }>)
      .then((r) => (r.path ? attachPaths([r.path]) : undefined))
      .catch(() => useApp.getState().showToast('Could not attach that image.', 'error'));
  };

  const pickFiles = async () => {
    const input = document.createElement('input');
    input.type = 'file';
    input.multiple = true;
    input.accept = 'image/*';
    input.onchange = () => {
      const n = native() as unknown as { pathForFile?: (f: File) => string } | undefined;
      const files = [...(input.files ?? [])];
      const paths = files.map((f) => n?.pathForFile?.(f) ?? '').filter(Boolean);
      if (paths.length) {
        void attachPaths(paths);
        return;
      }
      for (const file of files) {
        void fetch(`/api/upload?name=${encodeURIComponent(file.name)}`, { method: 'POST', body: file })
          .then((r) => r.json() as Promise<{ path?: string }>)
          .then((r) => (r.path ? attachPaths([r.path]) : undefined));
      }
    };
    input.click();
  };

  // Under a build the caller says what typing does at this step (`hint`).
  const placeholder = variant === 'describe'
    ? 'Say what goes in and what should come back'
    : variant === 'build' || bar
      ? hint
      : !ready
      ? 'Starting…'
      : running
        ? (followUp === 'steer' ? 'Add to what Architect is doing' : 'Queue a message for after this turn')
        : state.state === 'forge'
            ? 'Describe an agent to build'
            : state.state === 'plan'
              ? 'Ask for a plan'
              : 'Ask Architect anything';
  useEffect(fit, [text, placeholder, fit]);

  const takeQueued = (taken: string) => setText((t) => (t ? `${t}\n${taken}` : taken));
  const menus = (
    <>
      {menu === 'slash' && slashOpen && (
        <Menu passive side={shell.side} items={slashItems} onPick={pickSlash} onClose={() => setMenu(null)} width={560} empty="No command matches" />
      )}
      {menu === 'mention' && (
        <Menu
          passive
          side={shell.side}
          items={mentions.map((p) => ({ id: p, label: p }))}
          onPick={pickMention}
          onClose={() => setMenu(null)}
          width={480}
          empty="No file matches"
        />
      )}
    </>
  );
  const input = bar ? (
    <input
      ref={box}
      type="text"
      className="composer-input"
      value={text}
      placeholder={placeholder || undefined}
      disabled={disabled || !ready}
      onChange={(e) => setText(e.target.value)}
      onKeyDown={onKeyDown}
      aria-label={hint || 'Message'}
      autoComplete="off"
      spellCheck={false}
    />
  ) : (
    <textarea
      ref={box}
      className="composer-input"
      value={text}
      rows={variant === 'describe' ? 4 : 1}
      placeholder={placeholder || undefined}
      disabled={!ready && conv.status !== 'starting'}
      onChange={(e) => setText(e.target.value)}
      onKeyDown={onKeyDown}
      onPaste={full ? onPaste : undefined}
      aria-label={labelledBy ? undefined : 'Message'}
      aria-labelledby={labelledBy}
    />
  );
  const sendOrStop = stoppable && !text.trim() ? (
    <button type="button" className="send-button is-stop" onClick={() => void cancel(conv.id)} aria-label="Stop (Esc)" data-tip="Stop  Esc">
      <IconPlayerStopFilled size={14} />
    </button>
  ) : (
    <button type="button" className="send-button" data-action="send" onClick={() => void submit()} disabled={!ready || !text.trim()} aria-label={running ? (followUp === 'steer' ? 'Send now' : 'Queue message') : 'Send'}>
      <IconArrowUp size={16} />
    </button>
  );

  if (variant === 'describe') {
    return (
      <div ref={shell.ref} className="composer describe-field ui-field-frame">
        {menus}
        {input}
      </div>
    );
  }

  if (bar) {
    return (
      <div ref={shell.ref} className={clsx('composer bar-composer ui-field-frame', (disabled || !ready) && 'is-disabled')}>
        {menus}
        <IconMessage size={16} stroke={1.5} className="bar-composer-glyph" aria-hidden />
        {input}
        <button type="button" className="bar-composer-send" data-action="ask" onClick={() => void submit()} disabled={disabled || !ready || !text.trim()} aria-label="Send">
          <IconArrowUp size={14} stroke={1.75} />
        </button>
      </div>
    );
  }

  if (variant === 'build') {
    return (
      <div className="composer-wrap build-composer-wrap">
        {(state.queue?.length ?? 0) > 0 && <Queue conv={conv} onTake={takeQueued} />}
        <div ref={shell.ref} className="composer build-composer ui-field-frame">
          {menus}
          {input}
          <div className="build-composer-send">{sendOrStop}</div>
        </div>
      </div>
    );
  }

  return (
    <div className="composer-wrap">
      {(state.queue?.length ?? 0) > 0 && <Queue conv={conv} onTake={takeQueued} />}
      {(state.pending_images?.length ?? 0) > 0 && (
        <div className="attachments">
          {state.pending_images!.map((name, i) => (
            <span key={`${name}${i}`} className="attachment">
              <IconPhoto size={14} aria-hidden />
              <span className="attachment-name">{name}</span>
              <button type="button" className="attachment-x" aria-label={`Remove ${name}`} onClick={() => void request(conv.id, 'detach_image', { index: i })}>
                <IconX size={12} />
              </button>
            </span>
          ))}
        </div>
      )}
      <div
        ref={shell.ref}
        className={clsx('composer chat-composer ui-field-frame', dragging && 'is-dragging')}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        {menus}
        {input}
        <div className="composer-bar">
          <StateSwitch conv={conv} />
          {ready ? (
            <>
              <PermissionButton conv={conv} open={menu === 'permission'} setOpen={(o) => setMenu(o ? 'permission' : null)} />
              <ModelButton conv={conv} open={menu === 'model'} setOpen={(o) => setMenu(o ? 'model' : null)} />
            </>
          ) : conv.status === 'starting' || conv.status === 'trust' ? (
            // Codex: while a conversation starts or resumes, controls it cannot use yet are hidden.
            // The placeholder already says it is starting; the mark alone shows the wait.
            <span className="composer-loading" role="status" aria-label={conv.resuming || useApp.getState().prefs.open.find((o) => o.id === conv.id)?.session ? 'Loading the conversation' : 'Starting'}>
              <WorkingMark className="working-xs text-accent" />
            </span>
          ) : null}
          {/\bplan\b/i.test(text) && (state.state ?? 'normal') !== 'plan' && !text.startsWith('/') && ready && (
            <button type="button" className="plan-hint" onClick={() => void request(conv.id, 'set_state', { state: 'plan' })} data-tip={isMac() ? '⇧Tab cycles modes' : 'Shift+Tab cycles modes'}>
              <IconListDetails size={13} aria-hidden /> Plan first
            </button>
          )}
          <span className="composer-spacer" />
          <ViewToggle />
          {ready && <Usage conv={conv} open={menu === 'context'} setOpen={(o) => setMenu(o ? 'context' : null)} />}
          <button type="button" className="icon-button" onClick={() => void pickFiles()} aria-label="Attach an image" data-tip="Attach an image" disabled={!ready}>
            <IconPaperclip size={17} />
          </button>
          {sendOrStop}
        </div>
      </div>
    </div>
  );
}

/** Messages queued for after the turn; Edit takes one back into the field. */
function Queue({ conv, onTake }: { conv: Conv; onTake: (text: string) => void }) {
  const request = useApp((s) => s.request);
  return (
    <div className="queue">
      {conv.state.queue!.map((queued, i) => (
        <div key={i} className="queue-row">
          <span className="queue-label">Next</span>
          <span className="queue-text">{queued}</span>
          <button
            type="button"
            className="link-button"
            onClick={() => void request<{ taken: string[] }>(conv.id, 'unqueue', { index: i }).then((r) => onTake(r.taken.join('\n')))}
          >
            Edit
          </button>
        </div>
      ))}
    </div>
  );
}

function ViewToggle() {
  const verbose = useApp((s) => s.prefs.verbose);
  const setPrefs = useApp((s) => s.setPrefs);
  return (
    <button
      type="button"
      className={clsx('icon-button', 'view-toggle', verbose && 'is-on')}
      aria-pressed={verbose}
      aria-label="Verbose view"
      data-tip={`${verbose ? 'Verbose' : 'Normal'} view  ${shortcutLabel(SHORTCUTS.viewMode)}`}
      onClick={() => setPrefs({ verbose: !verbose })}
    >
      <IconListDetails size={17} stroke={1.7} />
    </button>
  );
}

function StateSwitch({ conv }: { conv: Conv }) {
  const request = useApp((s) => s.request);
  const current = conv.state.state ?? 'normal';
  return (
    <div className="seg seg-icons" role="radiogroup" aria-label={`Mode (${isMac() ? '⇧Tab' : 'Shift+Tab'} cycles)`}>
      {STATES.map((s) => {
        const on = current === s;
        return (
          <button
            key={s}
            type="button"
            role="radio"
            aria-checked={on}
            aria-label={STATE_LABELS[s]}
            data-tip={on ? undefined : STATE_LABELS[s]}
            className={clsx('seg-item', on && 'seg-on')}
            disabled={conv.status !== 'ready'}
            onClick={() => void request(conv.id, 'set_state', { state: s })}
          >
            {/* Words only (Lectern: a segmented control is words or icons, never both). */}
            <span className="seg-label">{STATE_LABELS[s]}</span>
          </button>
        );
      })}
    </div>
  );
}

function PermissionButton({ conv, open, setOpen }: { conv: Conv; open: boolean; setOpen: (o: boolean) => void }) {
  const request = useApp((s) => s.request);
  const current = conv.state.permission ?? 'ask';
  useMenuShortcut('m', open, setOpen);
  const drop = useDropSide<HTMLDivElement>(190);
  if (current === 'plan') return null; // plan mode says it already
  const Icon = PERMISSION_ICONS[current] ?? IconShieldCheck;
  const items: MenuItem[] = (['ask', 'auto-edit', 'full-auto'] as const).map((p) => ({
    id: p, label: PERMISSION_LABELS[p].label, detail: PERMISSION_LABELS[p].detail, selected: p === current,
    icon: PERMISSION_ICONS[p],
  }));
  return (
    <div className="bar-anchor" ref={drop.ref}>
      <button
        type="button"
        className={clsx('pill', current === 'full-auto' && 'pill-warn')}
        onClick={() => { drop.measure(); setOpen(!open); }}
        disabled={conv.status !== 'ready'}
        data-tip={open ? undefined : `What it may do  ${shortcutLabel(SHORTCUTS.permissionMenu)}`}
      >
        <Icon size={15} stroke={1.8} aria-hidden />
        <span>{PERMISSION_LABELS[current]?.short ?? current}</span>
        <Chevron side={drop.side} />
      </button>
      {open && (
        <Menu
          items={items}
          side={drop.side}
          width={340}
          onClose={() => setOpen(false)}
          onPick={(id) => {
            setOpen(false);
            void request(conv.id, 'set_permission', { permission: id });
          }}
        />
      )}
    </div>
  );
}

/**
 * The height a box needs to show its placeholder whole. Measured on a hidden copy beside
 * it (same classes, same width), so the box's own value and undo history stay untouched.
 */
function placeholderHeight(el: HTMLTextAreaElement): number {
  if (!el.placeholder || !el.parentElement) return 0;
  const probe = el.cloneNode(false) as HTMLTextAreaElement;
  probe.removeAttribute('id');
  probe.setAttribute('aria-hidden', 'true');
  probe.tabIndex = -1;
  probe.value = el.placeholder;
  probe.style.cssText = `position:absolute;visibility:hidden;pointer-events:none;height:auto;width:${el.getBoundingClientRect().width}px`;
  el.parentElement.appendChild(probe);
  const height = probe.scrollHeight;
  probe.remove();
  return height;
}

function ModelButton({ conv, open, setOpen }: { conv: Conv; open: boolean; setOpen: (o: boolean) => void }) {
  const send = useApp((s) => s.send);
  const setScreen = useApp((s) => s.setScreen);
  const models = conv.state.models ?? [];
  const current = conv.state.model ?? '';
  useMenuShortcut('i', open, setOpen);
  // Readable names first; the id under one only when the name hides it.
  const items: MenuItem[] = models.map((m) => ({ id: m, label: prettyModel(m), detail: prettyModel(m) !== m ? m : undefined, selected: m === current }));
  const drop = useDropSide<HTMLDivElement>(Math.min(360, 60 + models.length * 36));
  return (
    <div className="bar-anchor" ref={drop.ref}>
      <button
        type="button"
        className="pill"
        onClick={() => { drop.measure(); setOpen(!open); }}
        disabled={conv.status !== 'ready'}
        data-tip={open ? undefined : `Model  ${shortcutLabel(SHORTCUTS.modelMenu)}`}
      >
        <IconSparkles size={15} stroke={1.8} aria-hidden />
        {/* A narrow composer shows a Nemotron by its tier ("Super"); the tip has the whole name. */}
        <span className="pill-model">
          <span className="pill-model-full">{prettyModel(current) || 'Model'}</span>
          <span className="pill-model-short">{rungName(current) || 'Model'}</span>
        </span>
        <Chevron side={drop.side} />
      </button>
      {open && (
        <Menu
          items={items}
          side={drop.side}
          width={320}
          filter={models.length > 8}
          onClose={() => setOpen(false)}
          onPick={(id) => {
            setOpen(false);
            if (id !== current) void send(conv.id, `/model ${id}`, true);
          }}
          footer={
            <button type="button" className="menu-footer-action" onClick={() => setScreen('settings', 'models')}>
              <IconPlus size={14} aria-hidden /> Add a model or sign in
            </button>
          }
        />
      )}
    </div>
  );
}

/** Points the way the menu will open. */
function Chevron({ side }: { side: 'up' | 'down' }) {
  return side === 'up'
    ? <IconChevronUp size={13} className="pill-chevron" aria-hidden />
    : <IconChevronDown size={13} className="pill-chevron" aria-hidden />;
}

export { prettyModel } from '../../lib/models';

/** Mod+Shift+<key> opens a composer menu (Claude Code's model/permission openers). */
function useMenuShortcut(key: string, open: boolean, setOpen: (o: boolean) => void) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const mod = isMac() ? event.metaKey : event.ctrlKey;
      if (mod && event.shiftKey && event.key.toLowerCase() === key) {
        event.preventDefault();
        setOpen(!open);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [key, open, setOpen]);
}

/** Context fill as a ring and the conversation's spend, with the detail one click away. */
function Usage({ conv, open, setOpen }: { conv: Conv; open: boolean; setOpen: (o: boolean) => void }) {
  const send = useApp((s) => s.send);
  const state = conv.state;
  if (!state.started) return null;
  const percent = state.context_percent ?? 0;
  const tone = percent >= 80 ? 'tone-err' : percent >= 50 ? 'tone-warn' : '';
  const tokens = state.context_tokens ?? 0;
  const spent = state.spent_usd ?? 0;
  const budget = state.budget_usd ?? 0;
  const r = 6.5;
  const c = 2 * Math.PI * r;
  return (
    <div className="bar-anchor">
      <button type="button" className={clsx('usage', tone)} onClick={() => setOpen(!open)} aria-label={`Context ${Math.round(percent)}% full`} data-tip={open ? undefined : 'Context and spend'}>
        <svg width="17" height="17" viewBox="0 0 17 17" aria-hidden>
          <circle cx="8.5" cy="8.5" r={r} fill="none" className="usage-track" strokeWidth="2" />
          <circle cx="8.5" cy="8.5" r={r} fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeDasharray={`${Math.max(0.5, (c * percent) / 100)} ${c}`} transform="rotate(-90 8.5 8.5)" />
        </svg>
        {!state.unpriced && <span className="num">${spent < 1 ? spent.toFixed(2) : spent.toFixed(2)}</span>}
      </button>
      {open && (
        <div className="popover popover-up usage-pop">
          <div className="usage-row">
            <span>Context</span>
            <span className="num">{Math.round(percent)}%{tokens ? ` of ${Math.round(tokens / 1000)}k` : ''}</span>
          </div>
          <div className="usage-bar"><span style={{ width: `${Math.min(100, percent)}%` }} /></div>
          <div className="usage-row">
            <span>Spent</span>
            <span className="num">{state.unpriced ? 'Unknown' : `$${spent.toFixed(3)} of $${budget.toFixed(2)}`}</span>
          </div>
          {!state.unpriced && <div className="usage-bar"><span style={{ width: `${Math.min(100, budget ? (spent / budget) * 100 : 0)}%` }} /></div>}
          <div className="popover-actions">
            <button type="button" className="button button-small" onClick={() => { setOpen(false); void send(conv.id, '/compact', true); }}>
              <IconArrowsMinimize size={14} aria-hidden /> Free up context
            </button>
            <button type="button" className="button button-small button-quiet" onClick={() => { setOpen(false); void send(conv.id, '/context'); }}>Breakdown</button>
          </div>
        </div>
      )}
    </div>
  );
}

/** First message with the worktree option on: make the worktree, move there, then send. */
async function startInWorktree(conv: Conv, message: string): Promise<void> {
  const s = useApp.getState();
  const root = conv.git?.root ?? conv.state.cwd ?? '';
  try {
    const name = message.split(/\s+/).slice(0, 5).join('-');
    const wt = await s.gitCall<{ path: string; branch: string }>(conv.id, 'worktree-add', { name, prefix: s.prefs.branchPrefix });
    void useApp.getState().pruneWorktrees(wt.path);
    s.sendWhenReady(conv.id, message);
    s.moveConversation(conv.id, wt.path, { root, branch: wt.branch });
  } catch (err) {
    s.showToast(`Could not make a worktree: ${(err as { detail?: string }).detail ?? 'git failed'}. Sent here instead.`, 'error');
    await s.send(conv.id, message);
  }
}

export function RemoveButton({ onClick, label }: { onClick: () => void; label: string }) {
  return (
    <button type="button" className="icon-button" onClick={onClick} aria-label={label}>
      <IconX size={14} />
    </button>
  );
}
