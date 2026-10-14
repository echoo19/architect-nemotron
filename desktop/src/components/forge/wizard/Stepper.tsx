/**
 * The six steps in the top bar (clarity spec §2.1): a numbered circle and a label each,
 * joined by a hairline. Done is a filled pine circle with a check, the current step a
 * pine ring, a step not reached yet a hairline ring. Results is an err ring with an x
 * when nothing passed, and then Try it and Add are locked.
 *
 * Results, Try it and Add are buttons once they can be reached: a click shows that view
 * of the finished build (`wizardView`). Describe, Plan and Build are never buttons.
 */
import { IconCheck, IconLock, IconX } from '@tabler/icons-react';
import clsx from 'clsx';
import type { StepItem, StepMark } from '../../../lib/wizard';

/** What a screen reader hears after a step's label: its glyph says the same to the eye. */
const STATE_WORDS: Partial<Record<StepMark, string>> = { done: ', done', failed: ', did not pass', locked: ', locked' };

export function Stepper({ items, onPick }: { items: StepItem[]; onPick(item: StepItem): void }) {
  return (
    <nav className="wizard-steps" aria-label="Steps">
      <ol>
        {items.map((item, i) => {
          const inner = (
            <>
              <span className="wizard-step-dot" aria-hidden>
                {item.mark === 'done' ? <IconCheck size={12} stroke={2.25} />
                  : item.mark === 'failed' ? <IconX size={12} stroke={2} />
                    : item.mark === 'locked' ? <IconLock size={11} stroke={1.75} />
                      : item.step}
              </span>
              <span className="wizard-step-label">{item.label}</span>
              {STATE_WORDS[item.mark] && <span className="sr-only">{STATE_WORDS[item.mark]}</span>}
            </>
          );
          const linkDone = i > 0 && items[i - 1].mark === 'done' && item.mark !== 'todo' && item.mark !== 'locked';
          return (
            <li
              key={item.step}
              className={clsx('wizard-step', `is-${item.mark}`, item.current && 'is-current')}
              aria-current={item.current ? 'step' : undefined}
              data-step={item.step}
            >
              {i > 0 && <span className={clsx('wizard-step-link', linkDone && 'is-done')} aria-hidden />}
              {item.view ? (
                <button type="button" className="wizard-step-button no-drag" onClick={() => onPick(item)}>
                  {inner}
                </button>
              ) : (
                <span className="wizard-step-body">{inner}</span>
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
