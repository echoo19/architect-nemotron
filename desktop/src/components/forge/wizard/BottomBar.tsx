/**
 * The step's action bar (clarity spec §2.1), under the column and as wide as it:
 *
 *   [Back]  [secondary actions]  [Ask Architect about ...  ↑]        [note]  [Primary →]
 *
 * The ask box is the one-line composer (`Composer variant="bar"`): it sends into this
 * build's conversation, as the build composer did. It is never Stop; a step that can stop
 * passes "Stop build" as a secondary action (and `ownStop`); on any other step a running
 * turn that waits on nobody gets a plain Stop beside the secondaries. The note sits right-aligned beside the
 * primary, in two short lines at most. A screen that wants a second action beside the
 * primary (Edit plan, Don't allow) passes both as its `primary`.
 */
import { IconArrowLeft, IconArrowUp, IconMessage, IconSquare } from '@tabler/icons-react';
import clsx from 'clsx';
import { createContext, useContext, useState } from 'react';
import { useApp, type Conv } from '../../../store/app';
import { Composer } from '../../chat/Composer';
import { Button } from '../../ui/Button';
import { connectionLost, type BarSpec } from './WizardPage';

/** The build the page belongs to (ForgeWorkspace provides it); the ask box sends into it. */
export const WizardConv = createContext<Conv | null>(null);

export function BottomBar({ bar }: { bar: BarSpec }) {
  const conv = useContext(WizardConv);
  const cancel = useApp((s) => s.cancel);
  // A turn that runs with no question waiting (one asked from the ask box, a Try it you
  // started) can be stopped from any step; a step with its own Stop shows only that one.
  const waitingOnPerson = !!conv?.timeline.items.some((i) => i.kind === 'ask' && !i.closed && i.answer === undefined);
  const stoppable = !!conv && conv.timeline.running && !waitingOnPerson && !bar.ownStop;
  return (
    <div className="wizard-bar" role="group" aria-label="Step actions">
      <div className="wizard-bar-in">
        <div className="wizard-bar-start">
          {bar.back && (
            <Button variant="ghost" size="md" data-action="back" disabled={bar.back.disabled} onClick={() => bar.back?.run()}>
              <IconArrowLeft size={16} stroke={1.5} aria-hidden />
              {bar.back.label}
            </Button>
          )}
          {bar.secondary?.map((node, i) => <span key={i} className="wizard-bar-secondary">{node}</span>)}
          {stoppable && (
            <span className="wizard-bar-secondary">
              <Button variant="outline" size="md" data-action="stop-turn" onClick={() => conv && void cancel(conv.id)}>
                <IconSquare size={14} stroke={1.5} aria-hidden />Stop
              </Button>
            </span>
          )}
          {bar.ask && <AskBox placeholder={bar.ask.placeholder} disabled={bar.ask.disabled} />}
        </div>
        <div className="wizard-bar-end">
          {bar.note && <div className="wizard-bar-note">{bar.note}</div>}
          {bar.primary && <div className="wizard-bar-primary">{bar.primary}</div>}
        </div>
      </div>
    </div>
  );
}

/** The one-line ask: this build's conversation, else the active one. */
function AskBox({ placeholder, disabled }: { placeholder: string; disabled?: boolean }) {
  const provided = useContext(WizardConv);
  const active = useApp((s) => (provided ? undefined : s.prefs.active ? s.convs[s.prefs.active] : undefined));
  const lost = useApp(connectionLost);
  const conv = provided ?? active;
  if (!conv) return <PlainAsk placeholder={placeholder} disabled={disabled || lost} />;
  return <Composer conv={conv} variant="bar" hint={placeholder} disabled={disabled || lost} autoFocus={false} />;
}

/**
 * The same box for a page drawn outside a build page (a step on its own): the text goes
 * to the active conversation by id, without the composer's menus.
 */
function PlainAsk({ placeholder, disabled }: { placeholder: string; disabled?: boolean }) {
  const active = useApp((s) => s.prefs.active);
  const send = useApp((s) => s.send);
  const [text, setText] = useState('');
  const off = disabled || !active;
  const submit = () => {
    const message = text.trim();
    if (!message || !active || off) return;
    setText('');
    void send(active, message).catch(() => setText(message));
  };
  return (
    <div className={clsx('composer bar-composer ui-field-frame', off && 'is-disabled')}>
      <IconMessage size={16} stroke={1.5} className="bar-composer-glyph" aria-hidden />
      <input
        type="text"
        className="composer-input"
        aria-label={placeholder}
        placeholder={placeholder}
        value={text}
        disabled={off}
        autoComplete="off"
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && !e.nativeEvent.isComposing) {
            e.preventDefault();
            submit();
          }
        }}
      />
      <button type="button" className="bar-composer-send" data-action="ask" aria-label="Send" disabled={off || !text.trim()} onClick={submit}>
        <IconArrowUp size={14} stroke={1.75} />
      </button>
    </div>
  );
}
