/**
 * One step's page (clarity spec §2.1): the column with its status line, heading and one
 * sentence under it, the step's cards, and the bottom action bar. The props are fixed by
 * the spec; the shell slice owns the markup and styling behind them.
 *
 * The bar, left to right: Back, the secondary actions, the ask box (a one-line message to
 * Architect in this conversation; never Stop, which is its own secondary button), then on
 * the right the note and the primary.
 */
import type { ReactNode } from 'react';
import { useApp } from '../../../store/app';
import { BottomBar } from './BottomBar';

export interface BarSpec {
  back?: { label: string; run(): void; disabled?: boolean };
  secondary?: ReactNode[];
  /** The ask box's placeholder ("Ask Architect about the plan"); false or unset hides it. */
  ask?: { placeholder: string; disabled?: boolean } | false;
  note?: ReactNode;
  primary?: ReactNode;
  /**
   * The step has its own Stop (Stop build, Stop adding). Without one, the bar adds a
   * plain Stop while a turn runs that waits on nobody, so a question asked from the ask
   * box can always be stopped.
   */
  ownStop?: boolean;
}

export interface WizardPageProps {
  /** The line above the heading: a Working mark and what runs, or a StatusMark and the facts. */
  status?: ReactNode;
  title: ReactNode;
  /** One sentence that says what this screen shows. */
  sub?: ReactNode;
  children?: ReactNode;
  bar: BarSpec;
  /** An error to show under the sub (in --err); the first card gets the attention edge. */
  attention?: string;
  /** A warning to show under the sub (in --warn), with no action. */
  warning?: string;
  /** The action beside the attention line (Retry, Open settings). */
  attentionAction?: ReactNode;
}

/** The app lost its background process after it was up (the banner above the top bar says so). */
export function connectionLost(s: { ready: boolean; online: boolean }): boolean {
  return s.ready && !s.online;
}

export function WizardPage({ status, title, sub, children, bar, attention, warning, attentionAction }: WizardPageProps) {
  // With the connection lost, every control on the step waits; the Stepper (in the top
  // bar) still moves between finished steps.
  const lost = useApp(connectionLost);
  return (
    <div className="wizard-page" data-attention={attention ? '' : undefined}>
      <fieldset className="wizard-lock" disabled={lost}>
        <div className="wizard-scroll">
          <div className="wizard-column">
            <header className="wizard-head">
              {status && <div className="wizard-status">{status}</div>}
              <h1 className="wizard-title">{title}</h1>
              {sub && <p className="wizard-sub">{sub}</p>}
              {attention && (
                <p className="wizard-notice is-err" role="alert">
                  <span>{attention}</span>
                  {attentionAction}
                </p>
              )}
              {!attention && warning && <p className="wizard-notice is-warn" role="status">{warning}</p>}
            </header>
            <div className="wizard-body">{children}</div>
          </div>
        </div>
        <BottomBar bar={bar} />
      </fieldset>
    </div>
  );
}
