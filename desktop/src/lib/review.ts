/**
 * Local review (Codex's /review): ask for findings as `path:line: note`, then read
 * them back out of the answer so the Changes pane can show each beside its line.
 */
import type { Item } from './timeline';

export const REVIEW_FORMAT = 'Write each finding on its own line as path:line: what is wrong and why, most important first, then a one-line verdict.';
export const BRANCH_REVIEW = 'Review the changes on this branch against its base.';

export interface Finding { file: string; lineNo: number; text: string }

const LINE = /^\s*(?:[-*•]|\d+[.)])?\s*`?([^\s`:*]+\.[A-Za-z0-9]+)`?:(\d+)(?:[-–]\d+)?`?\s*[:—–-]\s*(.+?)\s*$/;

export function parseFindings(text: string): Finding[] {
  const out: Finding[] = [];
  for (const line of text.split('\n')) {
    const match = LINE.exec(line.replace(/\*\*/g, ''));
    if (match) out.push({ file: match[1].replace(/\\/g, '/').replace(/^\.\//, ''), lineNo: Number(match[2]), text: match[3] });
  }
  return out;
}

/** The findings of the latest review in a conversation (empty when the latest turn was not a review). */
export function latestFindings(items: Item[]): Finding[] {
  let review = -1;
  for (let i = items.length - 1; i >= 0; i -= 1) {
    const item = items[i];
    if (item.kind !== 'user') continue;
    if (item.text.startsWith('/review') || item.text.startsWith(BRANCH_REVIEW)) review = i;
    break;
  }
  if (review < 0) return [];
  const answer = items.slice(review + 1).find((i) => i.kind === 'answer');
  return answer && answer.kind === 'answer' ? parseFindings(answer.text) : [];
}

/** Does a finding's path name this diff file (findings may give a longer or shorter path)? */
export function sameFile(finding: string, file: string): boolean {
  const a = finding.toLowerCase();
  const b = file.toLowerCase();
  return a === b || a.endsWith(`/${b}`) || b.endsWith(`/${a}`);
}

/** How a review request reads in the conversation: what you asked, not the format note or the diff. */
export function shownRequest(text: string): string {
  if (text.startsWith(BRANCH_REVIEW)) return 'Review this branch against its base';
  if (text.startsWith('/review')) return text.replace(` ${REVIEW_FORMAT}`, '').trim();
  return text;
}
