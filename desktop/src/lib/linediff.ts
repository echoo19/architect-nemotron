/**
 * A line diff of two versions of one file, as unified hunks: the common head and tail are
 * set aside, Myers' algorithm finds the shortest edit for the rest, and each hunk keeps a
 * few lines of context. Used for Forge's harness versions, which never leave memory, so
 * there is no patch text to parse.
 */

export interface DiffLine { kind: 'ctx' | 'add' | 'del'; text: string; oldNo?: number; newNo?: number }
export interface DiffHunk { oldStart: number; newStart: number; lines: DiffLine[] }

/** Past this many edits the middle is shown as removed then added, whole. */
const MAX_EDITS = 1500;

function split(text: string): string[] {
  if (!text) return [];
  const lines = text.replace(/\r\n/g, '\n').split('\n');
  if (lines.at(-1) === '') lines.pop();
  return lines;
}

/** The edit script turning `a` into `b`: 0 keep, -1 delete from a, 1 insert from b. */
function myers(a: string[], b: string[]): number[] | null {
  const n = a.length;
  const m = b.length;
  const max = n + m;
  const offset = max + 1;
  let v = new Int32Array(2 * max + 3);
  const trace: Int32Array[] = [];
  for (let d = 0; d <= Math.min(max, MAX_EDITS); d += 1) {
    trace.push(v.slice());
    for (let k = -d; k <= d; k += 2) {
      let x = k === -d || (k !== d && v[offset + k - 1] < v[offset + k + 1]) ? v[offset + k + 1] : v[offset + k - 1] + 1;
      let y = x - k;
      while (x < n && y < m && a[x] === b[y]) {
        x += 1;
        y += 1;
      }
      v[offset + k] = x;
      if (x >= n && y >= m) {
        // Walk the trace back to the start.
        const ops: number[] = [];
        let cx = n;
        let cy = m;
        for (let back = d; back > 0; back -= 1) {
          const prev = trace[back];
          const kk = cx - cy;
          const down = kk === -back || (kk !== back && prev[offset + kk - 1] < prev[offset + kk + 1]);
          const pk = down ? kk + 1 : kk - 1;
          const px = prev[offset + pk];
          const py = px - pk;
          while (cx > px && cy > py) {
            ops.push(0);
            cx -= 1;
            cy -= 1;
          }
          ops.push(down ? 1 : -1);
          cx = px;
          cy = py;
        }
        while (cx > 0 && cy > 0) {
          ops.push(0);
          cx -= 1;
          cy -= 1;
        }
        return ops.reverse();
      }
    }
    v = v.slice();
  }
  return null;
}

/** Every line of `before` and `after` in order, each kept, removed or added. */
export function diffLines(before: string, after: string): DiffLine[] {
  const a = split(before);
  const b = split(after);
  let head = 0;
  while (head < a.length && head < b.length && a[head] === b[head]) head += 1;
  let tail = 0;
  while (tail < a.length - head && tail < b.length - head && a[a.length - 1 - tail] === b[b.length - 1 - tail]) tail += 1;
  const midA = a.slice(head, a.length - tail);
  const midB = b.slice(head, b.length - tail);
  const whole = [...midA.map(() => -1), ...midB.map(() => 1)];
  const ops = midA.length && midB.length ? myers(midA, midB) ?? whole : whole;
  const out: DiffLine[] = [];
  let oldNo = 1;
  let newNo = 1;
  for (let i = 0; i < head; i += 1) out.push({ kind: 'ctx', text: a[i], oldNo: oldNo++, newNo: newNo++ });
  let ia = 0;
  let ib = 0;
  for (const op of ops) {
    if (op === 0) {
      out.push({ kind: 'ctx', text: midA[ia], oldNo: oldNo++, newNo: newNo++ });
      ia += 1;
      ib += 1;
    } else if (op === -1) {
      out.push({ kind: 'del', text: midA[ia], oldNo: oldNo++ });
      ia += 1;
    } else {
      out.push({ kind: 'add', text: midB[ib], newNo: newNo++ });
      ib += 1;
    }
  }
  for (let i = a.length - tail; i < a.length; i += 1) out.push({ kind: 'ctx', text: a[i], oldNo: oldNo++, newNo: newNo++ });
  return out;
}

/** The changed lines with `context` lines around them, as hunks. */
export function diffHunks(before: string, after: string, context = 3): DiffHunk[] {
  const lines = diffLines(before, after);
  const changed = lines.map((line) => line.kind !== 'ctx');
  const hunks: DiffHunk[] = [];
  let i = 0;
  while (i < lines.length) {
    if (!changed[i]) {
      i += 1;
      continue;
    }
    const start = Math.max(0, i - context);
    let end = i;
    // Extend while the next change is within two contexts of this one.
    for (let j = i; j < lines.length; j += 1) {
      if (changed[j]) end = j;
      else if (j - end > context * 2) break;
    }
    const stop = Math.min(lines.length, end + context + 1);
    const slice = lines.slice(start, stop);
    const first = slice[0];
    hunks.push({
      oldStart: first.oldNo ?? (slice.find((l) => l.oldNo !== undefined)?.oldNo ?? 0),
      newStart: first.newNo ?? (slice.find((l) => l.newNo !== undefined)?.newNo ?? 0),
      lines: slice,
    });
    i = stop;
  }
  return hunks;
}

/** Lines added and removed from `before` to `after`. */
export function diffCount(before: string, after: string): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const line of diffLines(before, after)) {
    if (line.kind === 'add') added += 1;
    else if (line.kind === 'del') removed += 1;
  }
  return { added, removed };
}
