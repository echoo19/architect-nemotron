/**
 * FileLedger: the whole ledger in one JSON file, changed only under an exclusive lock
 * file (`<file>.lock`, created with O_EXCL) and replaced by an atomic rename. Calls in one
 * process queue behind each other; other processes (the admin CLI, a second relay) wait
 * on the lock. Events are appended to `<file minus .json>.events.jsonl`.
 *
 * For the local relay and tests only: on Vercel every instance would have its own file,
 * so the caps would not hold there (runtime.ts refuses that combination).
 */

import { promises as fs } from "node:fs";
import path from "node:path";
import {
  type LedgerState,
  type StoredCode,
  codeView,
  emptyState,
  expiredIn,
  normalizeState,
  pruneLimiter,
  reserveIn,
  settleIn,
} from "./state.js";
import {
  type CodePatch,
  type CodeView,
  type DayState,
  type GlobalState,
  type Ledger,
  type NewCode,
  type RelayEvent,
  type Report,
  type ReserveOutcome,
  type ReserveRequest,
  type SettleOutcome,
  type SettleRequest,
  type Snapshot,
  type UsageRow,
  decodeReservation,
  utcDay,
} from "./types.js";

const BUSY = new Set(["EEXIST", "EPERM", "EACCES", "EBUSY"]);
const RETRYABLE_IO = new Set(["EPERM", "EACCES", "EBUSY"]);
const EVENTS_ROTATE_BYTES = 16 * 1024 * 1024;
/** A lock file with no owner written yet, or any lock this old, is abandoned. */
const EMPTY_LOCK_MS = 10_000;
const STALE_LOCK_MS = 60_000;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function errCode(err: unknown): string {
  return typeof err === "object" && err !== null && "code" in err ? String((err as { code: unknown }).code) : "";
}

function processAlive(pid: number): boolean {
  if (!Number.isSafeInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return errCode(err) === "EPERM"; // exists, not ours to signal
  }
}

async function retryIo<T>(op: () => Promise<T>): Promise<T> {
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await op();
    } catch (err) {
      if (attempt >= 25 || !RETRYABLE_IO.has(errCode(err))) throw err;
      await sleep(4 * (attempt + 1));
    }
  }
}

export interface FileLedgerOptions {
  /** Give up waiting for the lock after this long (default 15 s). */
  lockTimeoutMs?: number;
}

export class FileLedger implements Ledger {
  readonly kind = "file" as const;
  readonly lockPath: string;
  readonly eventsPath: string;
  private queue: Promise<unknown> = Promise.resolve();

  constructor(
    readonly file: string,
    private readonly options: FileLedgerOptions = {},
  ) {
    this.lockPath = `${file}.lock`;
    this.eventsPath = `${file.replace(/\.json$/i, "")}.events.jsonl`;
  }

  // -- storage ---------------------------------------------------------------------

  private async lock(): Promise<() => Promise<void>> {
    const deadline = Date.now() + (this.options.lockTimeoutMs ?? 15_000);
    const token = `${process.pid} ${Date.now()} ${Math.random().toString(36).slice(2)}`;
    let delay = 1;
    for (;;) {
      try {
        const handle = await fs.open(this.lockPath, "wx");
        try {
          await handle.writeFile(token);
        } finally {
          await handle.close();
        }
        return async () => {
          await retryIo(() => fs.rm(this.lockPath, { force: true }));
        };
      } catch (err) {
        if (!BUSY.has(errCode(err))) throw err;
      }
      await this.clearDeadHolder();
      if (Date.now() > deadline) throw new Error(`ledger busy: ${this.lockPath} is still held`);
      await sleep(delay + Math.random() * delay);
      delay = Math.min(delay * 2, 25);
    }
  }

  /**
   * Removes the lock file if the process that wrote it no longer exists, or if it is far
   * older than any transaction (they take milliseconds), e.g. after a pid was reused.
   */
  private async clearDeadHolder(): Promise<void> {
    let seen: string;
    let age: number;
    try {
      seen = await fs.readFile(this.lockPath, "utf8");
      age = Date.now() - (await fs.stat(this.lockPath)).mtimeMs;
    } catch {
      return;
    }
    const dead = seen ? !processAlive(Number(seen.split(" ")[0])) : age > EMPTY_LOCK_MS;
    if (!dead && age < STALE_LOCK_MS) return;
    try {
      if ((await fs.readFile(this.lockPath, "utf8")) === seen) await fs.rm(this.lockPath, { force: true });
    } catch {
      // gone already, or busy: the next attempt sorts it out
    }
  }

  private async read(): Promise<LedgerState> {
    let text: string;
    try {
      text = await retryIo(() => fs.readFile(this.file, "utf8"));
    } catch (err) {
      if (errCode(err) === "ENOENT") return emptyState();
      throw err;
    }
    const state = JSON.parse(text) as LedgerState;
    if (state.version !== 1) throw new Error(`ledger: ${this.file} has unknown version ${String(state.version)}`);
    return normalizeState(state);
  }

  private async write(state: LedgerState): Promise<void> {
    const tmp = `${this.file}.tmp-${process.pid}-${Math.random().toString(36).slice(2)}`;
    await fs.writeFile(tmp, JSON.stringify(state));
    try {
      await retryIo(() => fs.rename(tmp, this.file));
    } catch (err) {
      await fs.rm(tmp, { force: true });
      throw err;
    }
  }

  private async appendEvents(events: RelayEvent[]): Promise<void> {
    if (events.length === 0) return;
    try {
      const stat = await fs.stat(this.eventsPath);
      if (stat.size > EVENTS_ROTATE_BYTES) {
        await retryIo(() => fs.rename(this.eventsPath, this.eventsPath.replace(/\.jsonl$/, ".1.jsonl")));
      }
    } catch (err) {
      if (errCode(err) !== "ENOENT") throw err;
    }
    await retryIo(() => fs.appendFile(this.eventsPath, events.map((e) => JSON.stringify(e) + "\n").join("")));
  }

  /** Runs `fn` on the current state under the lock; writes the state back when `write`. */
  private transact<T>(fn: (state: LedgerState) => T, write: boolean, events?: (result: T) => RelayEvent[]): Promise<T> {
    const run = async (): Promise<T> => {
      await fs.mkdir(path.dirname(path.resolve(this.file)), { recursive: true });
      const release = await this.lock();
      try {
        const state = await this.read();
        const result = fn(state);
        if (write) {
          await this.write(state);
          if (events) await this.appendEvents(events(result));
        }
        return result;
      } finally {
        await release();
      }
    };
    const next = this.queue.then(run, run);
    this.queue = next.catch(() => undefined);
    return next;
  }

  // -- the Ledger interface ----------------------------------------------------------

  getCode(hash: string) {
    return this.transact((s) => {
      const code = s.codes[hash];
      return code ? codeView(hash, code) : null;
    }, false);
  }

  getGlobal(): Promise<GlobalState> {
    return this.transact((s) => ({ ...s.global }), false);
  }

  getDay(hash: string, day: string): Promise<DayState> {
    return this.transact((s) => ({ ...(s.days[day]?.[hash] ?? { spent: 0, reserved: 0, requests: 0 }) }), false);
  }

  /** The three reads the handler made before Upstash needed them in one; a local read costs nothing. */
  async snapshot(hash: string, day: string): Promise<Snapshot> {
    const code = await this.getCode(hash);
    const [global, today] = await Promise.all([this.getGlobal(), this.getDay(hash, day)]);
    return { global, code, today };
  }

  reserve(request: ReserveRequest): Promise<ReserveOutcome> {
    return this.transact((s) => {
      const outcome = reserveIn(s, request);
      pruneLimiter(s);
      return outcome;
    }, true);
  }

  settle(request: SettleRequest): Promise<SettleOutcome> {
    return this.transact(
      (s) => ({ settled: settleIn(s, request) }),
      true,
      (result) => (result.settled ? [request.event] : []),
    );
  }

  sweep(nowMs: number, limit = 100, cutoffMs = nowMs): Promise<number> {
    let swept: RelayEvent[] = [];
    return this.transact(
      (s) => {
        swept = [];
        for (const { id, record } of expiredIn(s, cutoffMs, limit)) {
          const rec = decodeReservation(record);
          if (!rec) {
            delete s.reservations[id];
            continue;
          }
          const event: RelayEvent = {
            t: nowMs,
            id,
            code: rec.hash.slice(0, 12),
            model: rec.model,
            status: 0,
            kind: "full",
            why: "expired",
            reserved: rec.amount,
            actual: rec.amount,
            in: 0,
            out: 0,
            rsn: 0,
          };
          settleIn(s, {
            id,
            hash: rec.hash,
            model: rec.model,
            day: rec.day,
            window: rec.window,
            actual: rec.amount,
            promptTokens: 0,
            completionTokens: 0,
            reasoningTokens: 0,
            kind: "full",
            tokenCorrection: 0,
            event,
          });
          swept.push(event);
        }
        return swept.length;
      },
      true,
      () => swept,
    );
  }

  addCode(code: NewCode): Promise<void> {
    return this.transact((s) => {
      if (s.codes[code.hash]) throw new Error("ledger: that code already exists");
      if (Object.values(s.codes).some((c) => c.label === code.label)) {
        throw new Error(`ledger: the label "${code.label}" is taken`);
      }
      const stored: StoredCode = {
        label: code.label,
        capTotal: code.capTotal,
        capDaily: code.capDaily,
        freezePct: code.freezePct,
        revoked: false,
        frozen: false,
        createdAt: code.createdAt,
        spent: 0,
        reserved: 0,
        requests: 0,
        inFlight: 0,
      };
      if (code.sharePct !== undefined) stored.sharePct = code.sharePct;
      if (code.maxInFlight !== undefined) stored.maxInFlight = code.maxInFlight;
      s.codes[code.hash] = stored;
    }, true);
  }

  updateCode(hash: string, patch: CodePatch): Promise<boolean> {
    return this.transact((s) => {
      const code = s.codes[hash];
      if (!code) return false;
      if (patch.capTotal !== undefined) code.capTotal = patch.capTotal;
      if (patch.capDaily !== undefined) code.capDaily = patch.capDaily;
      if (patch.freezePct !== undefined) code.freezePct = patch.freezePct;
      if (patch.sharePct !== undefined) code.sharePct = patch.sharePct;
      if (patch.maxInFlight !== undefined) code.maxInFlight = patch.maxInFlight;
      if (patch.revoked !== undefined) code.revoked = patch.revoked;
      if (patch.frozen !== undefined) code.frozen = patch.frozen;
      return true;
    }, true);
  }

  listCodes(day: string): Promise<CodeView[]> {
    return this.transact(
      (s) =>
        Object.entries(s.codes)
          .map(([hash, code]) => ({
            ...codeView(hash, code),
            today: { ...(s.days[day]?.[hash] ?? { spent: 0, reserved: 0, requests: 0 }) },
          }))
          .sort((a, b) => a.createdAt - b.createdAt),
      false,
    );
  }

  setGlobalCap(cap: number): Promise<void> {
    return this.transact((s) => {
      s.global.cap = cap;
    }, true);
  }

  setDisabled(disabled: boolean): Promise<void> {
    return this.transact((s) => {
      s.global.disabled = disabled;
    }, true);
  }

  setOpeningBalance(amount: number): Promise<{ previous: number }> {
    return this.transact((s) => {
      const previous = s.global.opening;
      s.global.spent += amount - previous;
      s.global.opening = amount;
      return { previous };
    }, true);
  }

  async report(options: { days: string[]; events: number; nowMs: number }): Promise<Report> {
    const today = utcDay(options.nowMs);
    const base = await this.transact((s) => {
      const days = options.days.map((day) => ({
        day,
        models: Object.entries(s.usage[day] ?? {})
          .map(
            ([model, u]): UsageRow => ({
              model,
              requests: u.n,
              promptTokens: u.in,
              completionTokens: u.out,
              reasoningTokens: u.rsn,
              cost: u.cost,
              usage: u.usage,
              full: u.full,
              released: u.released,
              reconcile: u.reconcile ?? 0,
            }),
          )
          .sort((a, b) => a.model.localeCompare(b.model)),
      }));
      const held = Object.values(s.reservations);
      return {
        nowMs: options.nowMs,
        global: { ...s.global },
        codes: Object.entries(s.codes)
          .map(([hash, code]) => ({
            ...codeView(hash, code),
            today: { ...(s.days[today]?.[hash] ?? { spent: 0, reserved: 0, requests: 0 }) },
          }))
          .sort((a, b) => a.createdAt - b.createdAt),
        days,
        openReservations: held.length,
        expiredReservations: held.filter((h) => h.deadline <= options.nowMs).length,
      };
    }, false);
    return { ...base, events: await this.recentEvents(options.events) };
  }

  private async recentEvents(count: number): Promise<RelayEvent[]> {
    if (count <= 0) return [];
    let text = "";
    try {
      text = await retryIo(() => fs.readFile(this.eventsPath, "utf8"));
    } catch (err) {
      if (errCode(err) !== "ENOENT") throw err;
    }
    const lines = text.split("\n").filter(Boolean);
    return lines
      .slice(-count)
      .reverse()
      .map((line) => JSON.parse(line) as RelayEvent);
  }
}
