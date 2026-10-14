/**
 * A question before something that cannot be undone, as a Dialog the pane renders:
 * the question is the title, there is no body, and the two buttons name what each one
 * does. `ask` resolves true when the person picks the action.
 *
 *   const [ask, dialog] = useConfirm();
 *   if (await ask({ title: 'Discard your edits?', keep: 'Keep editing', action: 'Discard' })) ...
 *   return <>{...}{dialog}</>;
 */
import { useCallback, useRef, useState, type ReactNode } from 'react';
import { Button } from './Button';
import { Dialog } from './Dialog';

export interface ConfirmAsk {
  title: string;
  /** The safe button: "Keep editing". */
  keep: string;
  /** The button that acts: "Discard". */
  action: string;
  /** One short line under the title, only when the title cannot say where things go. */
  detail?: string;
  /** The action's look: destructive (the default) for what cannot be undone, primary otherwise. */
  tone?: 'destructive' | 'primary';
}

export function useConfirm(): [(ask: ConfirmAsk) => Promise<boolean>, ReactNode] {
  const [open, setOpen] = useState(false);
  // The last question stays on screen while the dialog plays its exit.
  const [shown, setShown] = useState<ConfirmAsk | null>(null);
  const resolver = useRef<((ok: boolean) => void) | null>(null);

  const ask = useCallback((next: ConfirmAsk) => new Promise<boolean>((resolve) => {
    resolver.current?.(false);
    resolver.current = resolve;
    setShown(next);
    setOpen(true);
  }), []);

  const done = (ok: boolean) => {
    const resolve = resolver.current;
    resolver.current = null;
    setOpen(false);
    resolve?.(ok);
  };

  const dialog = (
    <Dialog
      open={open}
      onOpenChange={(next) => { if (!next) done(false); }}
      title={shown?.title ?? ''}
      size="sm"
      hideClose
      footer={(
        <>
          <Button size="sm" onClick={() => done(false)}>{shown?.keep}</Button>
          <Button size="sm" variant={shown?.tone ?? 'destructive'} data-action="confirm" onClick={() => done(true)}>{shown?.action}</Button>
        </>
      )}
    >
      {shown?.detail ? <p className="text-small tone-mute">{shown.detail}</p> : null}
    </Dialog>
  );
  return [ask, dialog];
}
