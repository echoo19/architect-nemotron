/**
 * A question, an approval or a secret the turn is waiting on. It takes the
 * keyboard when it appears: Enter gives the safe default for a question and
 * allows once for an approval; Escape declines. Once answered it stays as a
 * one-line record of what was decided.
 *
 * Approvals keep the CLI's rules: allow once, allow for this conversation, or
 * not; no answer within five minutes is "not allowed" (decided by the bridge).
 *
 * An ask another control on screen answers (`held`: the build card's Build button, its
 * question, or the export dialog) shows no buttons of its own, so there is one way to
 * say yes. It reads as one line in the transcript; Enter still answers it when focused.
 */
import { IconCheck, IconX } from '@tabler/icons-react';
import clsx from 'clsx';
import { useEffect, useRef, useState } from 'react';
import { approvalIcon } from '../../lib/icons';
import type { Ask, Item } from '../../lib/timeline';
import { cleanQuestion, numbered } from '../../lib/asks';
import { useApp } from '../../store/app';
import { Button } from '../ui/Button';
import { Input } from '../ui/Field';
import { EditPreview } from './EditPreview';

export { EditPreview };

type AskItem = Extract<Item, { kind: 'ask' }>;

export function approvalTitle(action: string): string {
  if (action === 'write') return 'Create or replace a file';
  if (action === 'edit') return 'Edit a file';
  if (action === 'bash') return 'Run a command';
  if (action === 'export') return 'Put the agent into your project';
  if (action.startsWith('mcp ')) {
    const [server, tool] = action.slice(4).split('.');
    return `Use ${tool ?? 'a tool'} from ${server}`;
  }
  return `Allow ${action}`;
}

function pastTense(item: AskItem): string {
  const { ask, answer } = item;
  if (ask.kind === 'approval') {
    if (answer === 'yes') return 'Allowed once';
    if (answer === 'session') return 'Allowed for this conversation';
    if (answer === undefined) return 'No answer in time, so not allowed';
    return 'Not allowed';
  }
  if (ask.kind === 'secret') return answer ? 'Key entered' : 'Cancelled';
  if (answer === null) return 'Cancelled';
  if (answer === undefined) return 'Closed';
  const shown = answer === '' ? ask.default ?? '' : answer;
  const index = (ask.choices ?? []).indexOf(shown);
  const label = index >= 0 ? numbered(cleanQuestion(ask.question ?? ''), ask.choices ?? []).labels[index] : undefined;
  return shown ? `Answered: ${label ?? shown}` : 'Answered';
}

/** What the record shows beside its check or x: only what the icon cannot say. The rest is in the tooltip. */
function briefAnswer(item: AskItem): string {
  const { ask, answer } = item;
  if (ask.kind === 'approval') return answer === 'yes' ? 'Once' : answer === 'session' ? 'For this conversation' : '';
  if (ask.kind === 'secret' || answer === null || answer === undefined) return '';
  const shown = answer === '' ? ask.default ?? '' : answer;
  const index = (ask.choices ?? []).indexOf(shown);
  return (index >= 0 ? numbered(cleanQuestion(ask.question ?? ''), ask.choices ?? []).labels[index] : undefined) ?? shown;
}

export function AskCard({ item, onAnswer, held = false }: { item: AskItem; onAnswer: (ask: Ask, value: string | null) => void; held?: boolean }) {
  const { ask } = item;
  if (item.closed || item.answer !== undefined) {
    const declined = item.answer === 'no' || item.answer === null || (ask.kind === 'approval' && item.answer === undefined);
    return (
      <div className={clsx('ask-record', declined && 'is-declined')} data-tip={pastTense(item)} aria-label={pastTense(item)}>
        <span className="tl-chip">{declined ? <IconX size={14} stroke={1.5} aria-hidden /> : <IconCheck size={14} stroke={1.5} aria-hidden />}</span>
        <span className="ask-record-what">{ask.kind === 'approval' ? approvalTitle(ask.action ?? '') : numbered(cleanQuestion(ask.question ?? ''), ask.choices ?? []).prompt.split('\n')[0]}</span>
        {briefAnswer(item) && <span className="ask-record-answer">{briefAnswer(item)}</span>}
      </div>
    );
  }
  if (held) return <Held ask={ask} onAnswer={onAnswer} />;
  if (ask.kind === 'approval') return <Approval ask={ask} onAnswer={onAnswer} />;
  return <Question ask={ask} onAnswer={onAnswer} />;
}

/**
 * The transcript's copy of an ask the card or a dialog answers: the question (or the
 * approval's title) as one line, no buttons. It never takes focus by itself, so the
 * control that owns the ask keeps it; once focused, Enter and Escape still answer.
 */
function Held({ ask, onAnswer }: { ask: Ask; onAnswer: (ask: Ask, value: string | null) => void }) {
  const card = useRef<HTMLDivElement>(null);
  const approval = ask.kind === 'approval';
  const text = approval ? approvalTitle(ask.action ?? '') : numbered(cleanQuestion(ask.question ?? ''), ask.choices ?? []).prompt;
  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.target !== card.current) return;
    if (event.key === 'Enter') {
      event.preventDefault();
      onAnswer(ask, approval ? 'yes' : '');
    } else if (event.key === 'Escape') {
      event.preventDefault();
      onAnswer(ask, approval ? 'no' : null);
    }
  };
  return (
    <div ref={card} className={clsx('ask-card is-held ai-block', approval && 'is-approval')} tabIndex={-1} onKeyDown={onKeyDown}>
      <p className="ask-question">{text}</p>
    </div>
  );
}

function Approval({ ask, onAnswer }: { ask: Ask; onAnswer: (ask: Ask, value: string | null) => void }) {
  const card = useRef<HTMLDivElement>(null);
  const ActionIcon = approvalIcon(ask.action ?? '');
  const [why, setWhy] = useState<string | null>(null);
  // Not allowed, plus what to do instead: the note goes to Architect right after.
  const rejectWithNote = () => {
    onAnswer(ask, 'no');
    const note = why?.trim();
    const { prefs, send } = useApp.getState();
    if (note && prefs.active) void send(prefs.active, note);
  };
  useEffect(() => {
    card.current?.focus();
  }, []);
  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.target !== card.current) return;
    if (event.key === 'Enter') {
      event.preventDefault();
      onAnswer(ask, 'yes');
    } else if (event.key === 'Escape') {
      event.preventDefault();
      onAnswer(ask, 'no');
    }
  };
  return (
    <div ref={card} className="ask-card is-approval ai-block" tabIndex={0} onKeyDown={onKeyDown} role="group" aria-label={approvalTitle(ask.action ?? '')}>
      <div className="ask-head">
        <span className="tl-chip"><ActionIcon size={14} stroke={1.5} aria-hidden /></span>
        <h2 className="ask-title">{approvalTitle(ask.action ?? '')}</h2>
      </div>
      {ask.detail && (ask.action === 'write' || ask.action === 'edit'
        ? <EditPreview detail={ask.detail} />
        : <pre className={clsx('ask-detail', ask.action === 'bash' && 'is-command')}>{ask.action === 'bash' ? `$ ${ask.detail}` : ask.detail}</pre>)}
      {why !== null ? (
        <form className="ask-form ask-why" onSubmit={(e) => { e.preventDefault(); rejectWithNote(); }}>
          <Input
            size="sm"
            autoFocus
            value={why}
            aria-label="What should Architect do instead?"
            placeholder="What should Architect do instead?"
            onChange={(e) => setWhy(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Escape') {
                e.preventDefault();
                e.stopPropagation();
                setWhy(null);
                card.current?.focus();
              }
            }}
          />
          <Button type="submit" variant="primary" size="sm">{why.trim() ? "Don't allow and send" : "Don't allow"}</Button>
        </form>
      ) : (
        <div className="ask-buttons">
          <Button variant="primary" size="sm" kbd="Enter" onClick={() => onAnswer(ask, 'yes')}>Allow</Button>
          <Button size="sm" onClick={() => onAnswer(ask, 'session')}>Allow for this conversation</Button>
          <Button variant="ghost" size="sm" kbd="Esc" onClick={() => onAnswer(ask, 'no')}>Don't allow</Button>
          <Button variant="link" size="sm" className="ask-why-link" onClick={() => setWhy('')}>Say why</Button>
        </div>
      )}
    </div>
  );
}

function Question({ ask, onAnswer }: { ask: Ask; onAnswer: (ask: Ask, value: string | null) => void }) {
  const [value, setValue] = useState('');
  const field = useRef<HTMLInputElement>(null);
  const card = useRef<HTMLDivElement>(null);
  const choices = ask.choices ?? [];
  const secret = ask.kind === 'secret';
  const yesNo = choices.length === 0 && /^(y|yes|n|no)$/i.test(ask.default ?? '') && !secret;
  const question = cleanQuestion(ask.question ?? '');
  const options = numbered(question, choices);

  useEffect(() => {
    if (choices.length === 0 && !yesNo) field.current?.focus();
    else card.current?.focus();
  }, [choices.length, yesNo]);

  const submit = (answer: string | null) => onAnswer(ask, answer);

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      submit(null);
      return;
    }
    if (event.target !== card.current) return;
    if (event.key === 'Enter') {
      event.preventDefault();
      submit('');
    } else if (/^[1-9]$/.test(event.key) && choices[Number(event.key) - 1] !== undefined) {
      submit(choices[Number(event.key) - 1]);
    } else if (yesNo && /^[yn]$/i.test(event.key)) {
      submit(event.key.toLowerCase() === 'y' ? 'yes' : 'no');
    }
  };

  const yesDefault = /^y/i.test(ask.default ?? '');
  const noDefault = /^n/i.test(ask.default ?? '');

  return (
    <div ref={card} className="ask-card ai-block" tabIndex={-1} onKeyDown={onKeyDown}>
      <p className="ask-question">{options.prompt}</p>
      {choices.length > 0 ? (
        <div className="ask-choices" role="group" aria-label="Choices">
          {choices.map((choice, i) => (
            <button
              key={choice}
              type="button"
              className={clsx('choice', choice === ask.default && 'is-default')}
              onClick={() => submit(choice)}
            >
              <kbd>{i + 1}</kbd>
              <span>{options.labels[i] ?? choice}</span>
            </button>
          ))}
        </div>
      ) : yesNo ? (
        <div className="ask-buttons">
          <Button variant={yesDefault ? 'primary' : 'outline'} size="sm" kbd={yesDefault ? 'Enter' : undefined} onClick={() => submit('yes')}>Yes</Button>
          <Button variant={noDefault ? 'primary' : 'outline'} size="sm" kbd={noDefault ? 'Enter' : undefined} onClick={() => submit('no')}>No</Button>
        </div>
      ) : (
        <form
          className="ask-form"
          onSubmit={(e) => {
            e.preventDefault();
            submit(value);
          }}
        >
          <Input
            ref={field}
            size="sm"
            type={secret ? 'password' : 'text'}
            autoComplete="off"
            spellCheck={!secret}
            value={value}
            aria-label={secret ? 'Key' : 'Your answer'}
            placeholder={ask.default ? `${ask.default}` : secret ? 'Paste it here' : 'Your answer'}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Escape') {
                e.preventDefault();
                submit(null);
              }
            }}
          />
          <Button type="submit" variant="primary" size="sm">{secret ? 'Save' : value || !ask.default ? 'Send' : 'Use this'}</Button>
        </form>
      )}
      {!secret && !yesNo && <Button variant="link" size="sm" className="ask-cancel" onClick={() => submit(null)}>Cancel</Button>}
    </div>
  );
}
