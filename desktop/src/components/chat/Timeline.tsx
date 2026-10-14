/**
 * The conversation: what the person said, what Architect answered, and between
 * them one fold per turn holding the work. Questions and approvals sit in line,
 * where the turn is waiting for them.
 */
import { IconAlertTriangle, IconArrowDown, IconCheck, IconChevronRight, IconCopy, IconCircleCheck, IconCornerDownRight, IconExternalLink, IconGitFork, IconHammer, IconInfoCircle, IconListCheck, IconPencil, IconX } from '@tabler/icons-react';
import clsx from 'clsx';
import { Fragment, memo, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { friendlyLine, friendlyProgress } from '../../lib/forge';
import { buildTitle, duration, failedStep, subagentTitle, toolName, workLabel, type Ask, type Item, type Step, type Timeline as TimelineData } from '../../lib/timeline';
import { cardModels, cardRows, noticeWords, practiceWords } from '../../lib/vocab';
import { HashLines, type HashLineProps } from '../forge/HashLine';
import { toolIcon } from '../../lib/icons';
import { shownRequest } from '../../lib/review';
import { openLink } from '../../lib/native';
import { useApp } from '../../store/app';
import { useCopy } from '../../lib/useCopy';
import { WorkingMark } from '../ui/Working';
import { IconButton } from '../ui/Button';
import { Card, CardBody, CardHeader } from '../ui/Card';
import { Facts } from '../ui/Facts';
import { AskCard } from './AskCard';
import { FindBar } from './FindBar';
import { DiffBlock } from './DiffBlock';
import { AgentMark } from '../ui/AgentMark';
import { Markdown } from './Markdown';

/** Where each conversation was scrolled to, when not at the bottom (for this run of the app). */
const SCROLL = new Map<string, number>();

export function Timeline({
  timeline, onAnswer, onSend, links, empty, compact = false, heldAsk,
}: {
  compact?: boolean;
  /** A question another control on screen answers: its card shows no buttons of its own. */
  heldAsk?: number;
  timeline: TimelineData;
  onAnswer: (ask: Ask, value: string | null) => void;
  onSend: (text: string) => void;
  links: string[];
  empty?: React.ReactNode;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);
  const [away, setAway] = useState(false);
  const [finding, setFinding] = useState(false);
  const verbose = useApp((s) => s.prefs.verbose);

  // Ctrl+F finds in this conversation.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'f') {
        e.preventDefault();
        setFinding(true);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
  const setDraft = useApp((s) => s.setDraft);
  const active = useApp((s) => s.prefs.active);

  // Coming back to a conversation returns to where you were reading in it.
  useLayoutEffect(() => {
    const el = scroller.current;
    const saved = active ? SCROLL.get(active) : undefined;
    if (!el) return;
    if (saved === undefined) {
      pinned.current = true;
      el.scrollTop = el.scrollHeight;
    } else {
      pinned.current = false;
      el.scrollTop = saved;
    }
  }, [active]);

  // Follow the bottom while the reader is there; leave them alone once they scroll up.
  useEffect(() => {
    const el = scroller.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  });

  const onScroll = () => {
    const el = scroller.current;
    if (!el) return;
    pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    setAway(el.scrollHeight - el.scrollTop - el.clientHeight > 400);
    if (active) {
      if (pinned.current) SCROLL.delete(active);
      else SCROLL.set(active, el.scrollTop);
    }
  };

  const jump = () => {
    const el = scroller.current;
    if (!el) return;
    pinned.current = true;
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
  };

  const onEdit = (text: string) => {
    if (active) setDraft(active, text);
    window.dispatchEvent(new CustomEvent('architect:draft', { detail: text }));
  };

  // "Fork from here" keeps your first n messages and their answers in a new conversation.
  const saved = useApp((st) => (active && st.convs[active]?.state.title ? st.convs[active]?.state.session_id : undefined));
  const onFork = saved && !timeline.running ? (n: number) => void useApp.getState().forkSession(saved, n) : undefined;

  const items = timeline.items;
  const ordinals = new Map<string, number>();
  for (const item of items) if (item.kind === 'user') ordinals.set(item.key, ordinals.size + 1);
  const lastItem = items.at(-1);
  const waitingOnAsk = items.some((i) => i.kind === 'ask' && !i.closed);
  const streaming = lastItem?.kind === 'answer' && lastItem.streaming;
  const workOpen = lastItem?.kind === 'work' && lastItem.running;
  const showWorking = timeline.running && !streaming && !workOpen && !waitingOnAsk;

  return (
    <div className="timeline-wrap">
      {finding && <FindBar root={() => scroller.current} onClose={() => setFinding(false)} />}
    <div className="timeline" ref={scroller} onScroll={onScroll}>
      <div className="timeline-inner">
        {items.length === 0 && !timeline.running && empty}
        {items.map((item, index) => (
          <ItemView
            key={item.key}
            item={item}
            last={index === items.length - 1}
            thinking={timeline.thinking}
            onAnswer={onAnswer}
            onSend={onSend}
            onEdit={onEdit}
            onFork={onFork && ordinals.has(item.key) ? () => onFork(ordinals.get(item.key)!) : undefined}
            compact={compact}
            verbose={verbose}
            held={item.kind === 'ask' && item.ask.id === heldAsk}
          />
        ))}
        {links.length > 0 && timeline.running && (
          <div className="notice notice-info">
            <span className="tl-chip"><IconExternalLink size={14} stroke={1.5} aria-hidden /></span>
            <span className="notice-text">
              Finish signing in in your browser. If it did not open,{' '}
              <button type="button" className="link-button" onClick={() => openLink(links[links.length - 1])}>open the sign-in page</button>.
            </span>
          </div>
        )}
        {showWorking && <WorkingRow label={thinkingLabel(timeline.thinking)} />}
      </div>
    </div>
      {away && (
        <button type="button" className="jump" onClick={jump} aria-label="Jump to the latest">
          <IconArrowDown size={14} stroke={1.5} aria-hidden />
          {timeline.running ? 'Working' : 'Latest'}
        </button>
      )}
    </div>
  );
}

function thinkingLabel(who: string | null): string {
  if (!who || who === 'architect') return 'Thinking';
  if (who === 'coder') return 'The coder is working';
  return `${who[0].toUpperCase()}${who.slice(1)} is working`;
}

export function WorkingRow({ label, since }: { label: string; since?: number }) {
  const [start] = useState(() => since ?? Date.now());
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  const seconds = Math.floor((now - start) / 1000);
  return (
    <div className="working-row" role="status">
      <span className="tl-chip"><WorkingMark className="working-xs text-accent" /></span>
      <span className="working-label">{label}</span>
      {seconds >= 5 && <span className="working-time num">{duration(seconds)}</span>}
    </div>
  );
}

const ItemView = memo(function ItemView({
  item, last, thinking, onAnswer, onSend, onEdit, onFork, compact, verbose, held,
}: {
  onFork?: () => void;
  compact: boolean;
  verbose: boolean;
  held: boolean;
  item: Item;
  last: boolean;
  thinking: string | null;
  onAnswer: (ask: Ask, value: string | null) => void;
  onSend: (text: string) => void;
  onEdit: (text: string) => void;
}) {
  switch (item.kind) {
    case 'user':
      return <UserMessage text={shownRequest(item.text)} at={item.at} steered={item.steered} onEdit={onEdit} onFork={onFork} />;
    case 'answer':
      return <Answer text={item.text} live={item.streaming} meta={item.meta} />;
    case 'work':
      return <WorkFold item={item} thinking={last ? thinking : null} verbose={verbose} />;
    case 'notice':
      return <Notice level={item.level} text={noticeWords(item.text)} />;
    case 'plan':
      return compact ? <PlanChip title={item.title} /> : <PlanCardView title={item.title} rows={item.rows} footer={item.footer} data={item.data} />;
    case 'rows':
      return <RowsBlock title={item.title} rows={item.rows} />;
    case 'text':
      return <TextBlock text={item.text} />;
    case 'diff':
      return <DiffBlock text={item.text} onUndo={() => onSend('/undo')} />;
    case 'ask':
      return <AskCard item={item} onAnswer={onAnswer} held={held} />;
    default:
      return null;
  }
});

function UserMessage({ text, at, steered, onEdit, onFork }: { text: string; at?: number; steered?: boolean; onEdit: (text: string) => void; onFork?: () => void }) {
  const command = text.startsWith('/') || text.startsWith('!');
  const { copied, copy } = useCopy();
  return (
    <div className="msg-user ai-block">
      <div className="msg-user-col">
        {steered && <span className="msg-steered"><IconCornerDownRight size={14} stroke={1.5} aria-hidden /> Sent while working</span>}
        <div className={clsx('msg-user-bubble', command && 'is-command')}>{command ? commandText(text) : text}</div>
        <div className="msg-actions msg-actions-end">
          {at && <Stamp at={at} />}
          <IconButton icon={copied ? IconCheck : IconCopy} label={copied ? 'Copied' : 'Copy'} tipSide="up" onClick={() => copy(text)} />
          <IconButton icon={IconPencil} label="Edit and send again" tipSide="up" onClick={() => onEdit(text)} />
          {onFork && <IconButton icon={IconGitFork} label="Fork from here" tipSide="up" onClick={onFork} />}
        </div>
      </div>
    </div>
  );
}

/**
 * A command as typed, set so a narrow bubble breaks it at a space or after a slash
 * ("/make @specs/" then "receipt-reader.spec.json"), never at a hyphen inside a name.
 */
function commandText(text: string): React.ReactNode[] {
  return text.split(/(\s+)/).map((word, i) => {
    if (!word || /^\s+$/.test(word)) return word;
    const parts = word.split(/(?<=\/)/);
    return (
      <Fragment key={i}>
        {parts.map((part, j) => (
          <Fragment key={j}>
            {j > 0 && <wbr />}
            {part.length <= 40 ? <span className="cmd-part">{part}</span> : part}
          </Fragment>
        ))}
      </Fragment>
    );
  });
}

/** When a message was sent or answered: the time on hover, the full date as its tip. */
function Stamp({ at }: { at: number }) {
  const date = new Date(at);
  const today = new Date().toDateString() === date.toDateString();
  const time = date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  const label = today ? time : `${date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}, ${time}`;
  return <time className="msg-time num" dateTime={date.toISOString()} data-tip={date.toLocaleString(undefined, { dateStyle: 'full', timeStyle: 'short' })}>{label}</time>;
}

function Answer({ text, live, meta }: { text: string; live: boolean; meta?: { seconds?: number; cost?: number; at?: number } }) {
  const { copied, copy } = useCopy();
  return (
    <div className="msg-answer">
      <Markdown text={text} live={live} />
      {!live && text.trim() && (
        <div className="msg-actions">
          <IconButton icon={copied ? IconCheck : IconCopy} label={copied ? 'Copied' : 'Copy'} tipSide="up" onClick={() => copy(text)} />
          {meta?.at && <Stamp at={meta.at} />}
          {meta?.seconds !== undefined && (
            <span className="msg-meta num">
              {duration(Math.max(1, Math.round(meta.seconds)))}
              {meta.cost !== undefined && meta.cost > 0 && <span>${meta.cost < 0.01 ? meta.cost.toFixed(4) : meta.cost.toFixed(3)}</span>}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

function WorkFold({ item, thinking, verbose }: { item: Extract<Item, { kind: 'work' }>; thinking: string | null; verbose: boolean }) {
  const [userOpen, setUserOpen] = useState<boolean | null>(null);
  const open = userOpen ?? (item.running || verbose);
  const active = [...item.steps].reverse().find((s) => s.state === 'running');
  const runningLabel = active
    ? stepTitle(active)
    : (item.progress ? friendlyProgress(item.progress) : null) ?? (thinking && thinking !== 'architect' ? thinkingLabel(thinking) : 'Thinking');
  const label = item.running ? (active && item.progress && (active.name === 'make' || active.name === 'forge') ? friendlyProgress(item.progress) : runningLabel) : workLabel(item);
  const failed = !item.running && item.steps.some(failedStep);

  return (
    <div className="work">
      <button type="button" className="work-head" onClick={() => setUserOpen(!open)} aria-expanded={open}>
        <span className={clsx('tl-chip work-icon', failed && 'is-warn')}>
          {item.running
            ? <WorkingMark className="working-xs text-accent" />
            : item.steps.some((s) => s.state === 'running')
              ? <IconHammer size={14} stroke={1.5} aria-hidden />
              : failed
              ? <IconAlertTriangle size={14} stroke={1.5} className="swap-enter" aria-hidden />
              : <IconCheck size={14} stroke={1.5} className="swap-enter" aria-hidden />}
        </span>
        <span key={label} className={clsx('work-label', !item.running && 'swap-enter')}>{label}</span>
        <IconChevronRight size={14} stroke={1.5} className={clsx('work-chevron', open && 'is-open')} aria-hidden />
      </button>
      <div className={clsx('chat-fold grid', open ? 'chat-fold-open' : 'chat-fold-shut')} aria-hidden={!open} {...((!open ? { inert: '' } : {}) as Record<string, string>)}>
        <div className="fold-body">
          <ol className="work-steps">
            {item.steps.map((step) => <StepRow key={step.id} step={step} verbose={verbose} />)}
            {item.running && item.progress && active && label !== friendlyProgress(item.progress) && <li className="work-progress">{friendlyProgress(item.progress)}</li>}
          </ol>
        </div>
      </div>
    </div>
  );
}

function stepTitle(step: Step): string {
  if (step.subagent) return subagentTitle(step.name);
  const name = toolName(step.name);
  const base = name.charAt(0).toUpperCase() + name.slice(1);
  if (step.name === 'code') return step.state === 'running' ? 'The coder is working' : 'The coder';
  if (step.name === 'make' || step.name === 'forge') return buildTitle(step);
  return base;
}

function StepIcon({ step }: { step: Step }) {
  if (step.state === 'running') return <span className="step-icon"><WorkingMark className="working-xs text-accent" /></span>;
  if (step.subagent) return <span className="step-icon"><AgentMark name={step.name} /></span>;
  const Icon = toolIcon(step.name);
  return <span className={clsx('step-icon', failedStep(step) && 'tone-err')}><Icon size={14} stroke={1.5} aria-hidden /></span>;
}

function StepRow({ step, verbose }: { step: Step; verbose: boolean }) {
  const [userOpen, setOpen] = useState<boolean | null>(null);
  const open = userOpen ?? verbose;
  const hasMore = step.lines.length > 0 || (step.result && step.result !== step.summary);
  return (
    <li className={clsx('step', `step-${step.state}`)}>
      <button type="button" className="step-head" onClick={() => setOpen(!open)} disabled={!hasMore} data-tip={failedStep(step) ? 'Failed' : undefined}>
        <StepIcon step={step} />
        {/* The fold's heading already says "The coder is working"; the row just names it. */}
        <span className="step-name">{step.name === 'code' ? 'The coder' : stepTitle(step)}</span>
        {step.summary && <span className={clsx('step-summary', !step.subagent && 'mono')}>{step.subagent ? friendlyLine(step.summary) : step.summary}</span>}
      </button>
      {(open || step.state === 'running') && step.lines.length > 0 && (
        <ul className="step-lines">
          {step.lines.slice(-40).map((line, i) => (
            <li key={i}><span className="step-agent"><AgentMark name={line.agent} size={12} /> {line.agent}</span> {friendlyLine(line.text)}</li>
          ))}
        </ul>
      )}
      {open && step.result && step.state !== 'running' && <pre className="step-result">{step.result}</pre>}
    </li>
  );
}

export function Notice({ level, text }: { level: 'info' | 'warn' | 'error'; text: string }) {
  return (
    <div className={clsx('notice', `notice-${level}`)} role={level === 'error' ? 'alert' : undefined}>
      <span className="tl-chip">
        {level === 'info' && <IconInfoCircle size={14} stroke={1.5} aria-hidden />}
        {level === 'error' && <IconX size={14} stroke={1.5} aria-hidden />}
        {level === 'warn' && <IconAlertTriangle size={14} stroke={1.5} aria-hidden />}
      </span>
      <span className="notice-text">{text}</span>
    </div>
  );
}

/**
 * In a build the card shows the plan and the result; the transcript keeps a one-line
 * fold for each that opens its sheet (the plan, or the report once there is a result).
 */
function PlanChip({ title }: { title: string }) {
  const openForgeSheet = useApp((s) => s.openForgeSheet);
  const result = title.startsWith('Result');
  return (
    <button type="button" className="plan-chip ai-block" onClick={() => openForgeSheet(result ? 'report' : 'plan')}>
      <span className="tl-chip">{result ? <IconCircleCheck size={14} stroke={1.5} aria-hidden /> : <IconListCheck size={14} stroke={1.5} aria-hidden />}</span>
      <span className="plan-chip-title">{title}</span>
      <IconChevronRight size={14} stroke={1.5} className="plan-chip-go" aria-hidden />
    </button>
  );
}

/** A build's plan or result card, in the app's words, with its hashes as hash lines. */
export function PlanCardView({ title, rows, footer, data }: { title: string; rows: Array<[string, string]>; footer: string; data?: Record<string, unknown> }) {
  const models = cardModels(data);
  const result = title.startsWith('Result');
  const hashes: HashLineProps[] = [];
  if (!result && typeof data?.spec_hash === 'string') hashes.push({ label: 'spec', hash: data.spec_hash, note: typeof data.frozen === 'string' ? `frozen, ${data.frozen}` : undefined });
  if (!result && typeof data?.pack_hash === 'string') hashes.push({ label: 'pack', hash: data.pack_hash });
  return (
    <Card className="plan-card ai-block" cellClassName="plan-card-cell" aria-label={title}>
      <CardHeader as="h3">{title}</CardHeader>
      <CardBody>
        <Facts columns={2} items={cardRows(rows, { result, models }).map(([label, value]) => ({ label, value, wide: value.length > 48 }))} />
        {hashes.length > 0 && <div className="plan-hashes"><HashLines lines={hashes} /></div>}
        {footer && !hashes.length && <p className="plan-footer">{practiceWords(footer)}</p>}
      </CardBody>
    </Card>
  );
}

function RowsBlock({ title, rows }: { title: string; rows: Array<[string, string]> }) {
  return (
    <section className="rows-block ai-block">
      {title && <h3>{title}</h3>}
      {rows.length === 0
        ? <p className="tone-faint">Nothing to show.</p>
        : (
          <dl className="kv">
            {rows.map(([label, value], i) => (
              <div key={i} className="kv-row">
                <dt>{label}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        )}
    </section>
  );
}

function TextBlock({ text }: { text: string }) {
  const multi = text.includes('\n') || text.length > 160;
  return multi ? <pre className="text-block ai-block">{text}</pre> : <p className="text-line ai-block">{text}</p>;
}
