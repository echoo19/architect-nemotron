/**
 * Hash lines (site/design/SPEC.md, "Hash line"): a hash set the way people compare
 * them, mono and lowercase in groups of eight, two lines of four, with a copy button and
 * the match against what it should equal. Selecting the text copies the hash unbroken:
 * the groups are spans with nothing between them.
 */
import { IconAlertTriangle, IconCheck, IconCircleCheck, IconCopy } from '@tabler/icons-react';
import { hashHalves } from '../../lib/viz';
import { useCopy } from '../../lib/useCopy';

export interface HashLineProps {
  label: string;
  hash: string;
  /** true: it matches `matchName`; false: it differs; undefined: nothing to compare yet. */
  match?: boolean;
  matchName?: string;
  note?: string;
}

export function HashLine({ label, hash, match, matchName, note }: HashLineProps) {
  const { copied, copy } = useCopy();
  const value = hash.toLowerCase();
  const against = matchName ?? 'the published hash';
  return (
    <div className="hashline">
      <span className="hashline-label">{label}</span>
      <span className="hashline-value">
        <code className="hashline-hash">
          {hashHalves(value).map((half, h) => (
            <span key={h} className="hashline-half">
              {half.map((quarter, q) => (
                <span key={q} className="hashline-quarter">{quarter.map((group, g) => <span key={g}>{group}</span>)}</span>
              ))}
            </span>
          ))}
        </code>
      </span>
      <span className="hashline-state-cell">
        {match === true && (
          <span className="hashline-state is-match" role="img" aria-label={`Matches ${against}`} data-tip={`Matches ${against}`}>
            <IconCircleCheck size={15} stroke={2} aria-hidden />
          </span>
        )}
        {match === false && (
          <span className="hashline-state is-mismatch" role="img" aria-label={`Differs from ${against}`} data-tip={`Differs from ${against}`}>
            <IconAlertTriangle size={15} stroke={2} aria-hidden />
          </span>
        )}
      </span>
      <span className="hashline-action">
        <button type="button" className="viz-icon-button" aria-label={`Copy the ${label} hash`} data-tip={copied ? 'Copied' : 'Copy'} onClick={() => copy(value)}>
          {copied ? <IconCheck size={14} stroke={2} aria-hidden /> : <IconCopy size={14} stroke={2} aria-hidden />}
        </button>
      </span>
      {note && <span className="hashline-note">{note}</span>}
    </div>
  );
}

/** Several hash lines in one container, so they restack together on a narrow card. */
export function HashLines({ lines }: { lines: HashLineProps[] }) {
  if (!lines.length) return null;
  return (
    <div className="hashlines">
      {lines.map((line) => <HashLine key={line.label} {...line} />)}
    </div>
  );
}
