import { useState } from 'react';

/** Two steps for anything that changes what is installed: the button, then a plain confirmation. */
export function ConfirmButton({ label, confirm, onConfirm }: { label: string; confirm: string; onConfirm: () => void }) {
  const [asking, setAsking] = useState(false);
  if (!asking) return <button type="button" className="link-button" onClick={() => setAsking(true)}>{label}</button>;
  return (
    <span className="confirm-inline">
      <button type="button" className="button button-small button-primary" onClick={() => { setAsking(false); onConfirm(); }}>{confirm}</button>
      <button type="button" className="button button-small button-quiet" onClick={() => setAsking(false)}>Cancel</button>
    </span>
  );
}
