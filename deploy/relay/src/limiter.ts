/**
 * The limiter is a sliding one-minute window kept in the ledger: the current minute's
 * count plus the previous minute's, weighted by how much of it still overlaps the last 60
 * seconds. Each model has one window for every code, and each code has its own window per
 * model, holding a share of the model's budget. These helpers mirror the ledger's admission
 * rules and work out how long a refused call has to wait.
 */

export const WINDOW_MS = 60_000;

export function windowOf(nowMs: number): number {
  return Math.floor(nowMs / WINDOW_MS);
}

/**
 * The ledger's rule. `cur`/`prev` are the counts of this and the previous minute,
 * `elapsed` the ms into this minute. Tokens (allowOversize) admit a call larger than the
 * whole budget once the window is empty, so an oversized prompt waits instead of failing
 * forever; requests never need that.
 */
export function admits(
  cur: number,
  prev: number,
  elapsed: number,
  limit: number,
  need: number,
  allowOversize: boolean,
): boolean {
  const busy = cur * WINDOW_MS + prev * (WINDOW_MS - elapsed);
  if (allowOversize && busy <= 0) return true;
  return busy + need * WINDOW_MS <= limit * WINDOW_MS;
}

/** A code's per-minute budget for one model: `pct` percent of the relay's, never below 1. */
export function shareOf(limit: number, pct: number): number {
  return Math.max(1, Math.floor((limit * pct) / 100));
}

/**
 * The ledger's rule for a code's own token budget. A call must fit in what is left of it,
 * with one exception: a code with no call running may start a call of any size while its
 * recent use is within budget. So a call larger than the whole share waits for the code's
 * earlier calls to age out of the window, never for an empty minute that a busy client
 * would never see.
 */
export function codeAdmitsTokens(
  cur: number,
  prev: number,
  elapsed: number,
  limit: number,
  need: number,
  inFlight: number,
): boolean {
  if (admits(cur, prev, elapsed, limit, need, false)) return true;
  return inFlight === 0 && admits(cur, prev, elapsed, limit, 0, false);
}

/** Milliseconds until `admits` holds if no other call arrives meanwhile (0 = now). */
export function waitMs(
  cur: number,
  prev: number,
  elapsed: number,
  limit: number,
  need: number,
  allowOversize: boolean,
): number {
  const W = WINDOW_MS;
  if (admits(cur, prev, elapsed, limit, need, allowOversize)) return 0;
  const rest = W - elapsed;
  if (need > limit) {
    // Only an empty window takes it: this minute's own calls must age out completely.
    if (!allowOversize) return Number.POSITIVE_INFINITY;
    return cur > 0 ? rest + W : rest;
  }
  if (cur + need <= limit && prev > 0) {
    // Still this minute: the previous minute's weight shrinks until the call fits.
    const at = W - ((limit - cur - need) * W) / prev;
    return Math.max(1, Math.ceil(at - elapsed));
  }
  // Next minute: this minute becomes the previous one and fades.
  if (cur <= 0) return Math.max(1, rest);
  const at = W - ((limit - need) * W) / cur;
  return Math.max(1, Math.ceil(rest + Math.max(0, at)));
}
