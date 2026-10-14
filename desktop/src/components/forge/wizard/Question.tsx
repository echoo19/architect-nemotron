/**
 * The interview's question (moved from AgentView, clarity spec step 2): a numbered
 * question as a radio Segmented (a list of radio rows past four choices), a free-text one
 * as a field, a secret as a password field, a yes or no as two buttons. Answers send the
 * choice, never its words.
 *
 * On the wizard the bar's primary submits the form: it carries `form={formId}`, so the
 * card keeps only Cancel (and Use the default). A yes or no keeps its two buttons in the
 * card; the bar has no primary then (`questionSubmits`).
 */
import clsx from 'clsx';
import { useEffect, useRef, useState } from 'react';
import { cleanQuestion, numbered } from '../../../lib/asks';
import type { Ask } from '../../../lib/timeline';
import { questionOptions } from '../../../lib/vocab';
import { Button } from '../../ui/Button';
import { Input, TextArea } from '../../ui/Field';
import { Segmented } from '../../ui/Segmented';

/** A yes or no question: its default says so. */
const YES_NO = /^(y|yes|n|no)$/i;

/** Whether the question is answered by a submit (the bar's Answer), not by its own Yes and No. */
export function questionSubmits(ask: Ask): boolean {
  return !!questionOptions(ask) || ask.kind === 'secret' || !YES_NO.test(ask.default ?? '');
}

export interface QuestionProps {
  ask: Ask;
  disabled: boolean;
  onAnswer: (value: string | null) => void;
  /** The form's id, for a submit button outside it (the bar's Answer). */
  formId?: string;
  /** The submit lives outside the form: the card keeps only Cancel. */
  externalSubmit?: boolean;
}

export function Question({ ask, disabled, onAnswer, formId, externalSubmit = false }: QuestionProps) {
  const options = questionOptions(ask);
  const [choice, setChoice] = useState<string | undefined>(() => (options?.some((o) => o.value === ask.default) ? ask.default! : options?.[0]?.value));
  const [text, setText] = useState('');
  const field = useRef<HTMLInputElement & HTMLTextAreaElement>(null);
  const form = useRef<HTMLFormElement>(null);
  // The question takes the keyboard when it arrives: a field gets the caret; a choice or a
  // yes or no gets the form itself (its keys work, and no ring shows before a key is pressed).
  useEffect(() => {
    const target = form.current?.querySelector<HTMLElement>('input, textarea') ?? form.current;
    target?.focus({ preventScroll: true });
  }, []);
  const prompt = numbered(cleanQuestion(ask.question ?? ''), ask.choices ?? []).prompt || cleanQuestion(ask.question ?? '');
  const secret = ask.kind === 'secret';
  const yesNo = !options && !secret && YES_NO.test(ask.default ?? '');
  const long = (ask.default ?? '').length > 60;

  const submit = (event?: React.FormEvent) => {
    event?.preventDefault();
    if (disabled) return;
    if (options) {
      if (choice !== undefined) onAnswer(choice);
    } else if (text.trim()) onAnswer(text);
    else if (!secret && ask.default) onAnswer(ask.default);
  };

  return (
    <form
      ref={form}
      id={formId}
      tabIndex={-1}
      className="wizard-question"
      onSubmit={submit}
      onKeyDown={(e) => {
        // Escape cancels the question, as it does on the transcript's copy; 1 to 9 pick a choice, y and n answer a yes or no.
        if (disabled || e.nativeEvent.isComposing) return;
        const typing = (e.target as HTMLElement).matches('input, textarea');
        if (e.key === 'Escape') {
          e.preventDefault();
          onAnswer(null);
        } else if (e.key === 'Enter' && e.target === e.currentTarget && !yesNo) {
          submit(e);
        } else if (!typing && options && /^[1-9]$/.test(e.key) && options[Number(e.key) - 1]) {
          e.preventDefault();
          setChoice(options[Number(e.key) - 1].value);
        } else if (!typing && yesNo && /^[yn]$/i.test(e.key)) {
          e.preventDefault();
          onAnswer(e.key.toLowerCase() === 'y' ? 'yes' : 'no');
        }
      }}
    >
      <p className="wizard-question-text">{prompt}</p>
      {options && options.length <= 4 && (
        <Segmented kind="radio" label={prompt || 'Answer'} options={options} value={choice} onChange={setChoice} size="md" fill />
      )}
      {options && options.length > 4 && (
        <div role="radiogroup" aria-label={prompt || 'Answer'} className="wizard-choices">
          {options.map((o) => (
            <button key={o.value} type="button" role="radio" aria-checked={choice === o.value} className={clsx('wizard-choice', choice === o.value && 'is-on')} onClick={() => setChoice(o.value)}>
              {o.label}
            </button>
          ))}
        </div>
      )}
      {!options && secret && (
        <Input ref={field} type="password" aria-label={prompt || 'Secret'} value={text} disabled={disabled} onChange={(e) => setText(e.target.value)} />
      )}
      {!options && !secret && !yesNo && (long
        ? <TextArea ref={field} aria-label={prompt || 'Answer'} value={text} placeholder={ask.default ?? undefined} disabled={disabled} onChange={(e) => setText(e.target.value)} />
        : <Input ref={field} aria-label={prompt || 'Answer'} value={text} placeholder={ask.default ?? undefined} disabled={disabled} onChange={(e) => setText(e.target.value)} />)}
      {yesNo ? (
        <div className="wizard-question-actions">
          <Button variant="link" size="sm" className="wizard-question-cancel" disabled={disabled} onClick={() => onAnswer(null)}>Cancel</Button>
          <Button size="sm" data-action="answer" disabled={disabled} onClick={() => onAnswer('yes')}>Yes</Button>
          <Button size="sm" disabled={disabled} onClick={() => onAnswer('no')}>No</Button>
        </div>
      ) : (
        <div className="wizard-question-actions">
          <Button variant="link" size="sm" className="wizard-question-cancel" disabled={disabled} onClick={() => onAnswer(null)}>Cancel</Button>
          {!options && !secret && ask.default && <Button variant="ghost" size="sm" disabled={disabled} onClick={() => onAnswer(ask.default!)}>Use the default</Button>}
          {!externalSubmit && <Button type="submit" variant="primary" size="sm" data-action="answer" disabled={disabled || (secret && !text)}>{secret ? 'Save' : 'Answer'}</Button>}
        </div>
      )}
    </form>
  );
}
