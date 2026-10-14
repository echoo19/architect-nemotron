#!/usr/bin/env node
// Operator CLI for the spend relay's ledger. Codes are minted here and printed once;
// the ledger keeps only their SHA-256. Needs `npm run compile` first.
//
//   node admin.mjs global --cap 26
//   node admin.mjs mint --label judge --total 10 --daily 3
//   node admin.mjs list
//   node admin.mjs report --days 3
//
// The ledger is the local file (.data/ledger.json) unless RELAY_LEDGER=upstash (with
// UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN) or --ledger upstash. With the Upstash
// variables set, the file ledger needs --ledger file (or RELAY_LEDGER=file) by name.

import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const USAGE = `usage: node admin.mjs <command> [options]

commands:
  global --cap <usd>                       set the lifetime cap over every code
  opening-balance --usd <usd> | --from-file <ledger.json>
                                           count spend made on another ledger (the file
                                           ledger before a move to Upstash) against the
                                           global cap; setting it again replaces it;
                                           --from-file also switches that ledger off
  mint --label <name> --total <usd> [--daily <usd>] [--freeze-at <pct>] [--share <pct>] [--in-flight <n>]
                                           mint a code; it is printed once, on stdout
  mint --existing --label <name> --total <usd> [...] < <file>
                                           add a code that already exists (after a move to a
                                           new database): read from stdin, either the only
                                           code there or the one on a "<name>=<code>" line;
                                           it is never printed
  revoke <id|label>                        refuse a code from now on
  unfreeze <id|label>                      let a frozen code work again (see report)
  caps <id|label> [--total <usd>] [--daily <usd>] [--freeze-at <pct>] [--share <pct>] [--in-flight <n>]
  list [--json]                            codes and their spend (never the codes)
  report [--days <n>] [--events <n>] [--json]
                                           spend by code, by UTC day and model, recent calls
  disable | enable                         the ledger kill switch (every /v1 call gets 503)
  sweep [--all]                            book overdue reservations at their full amount;
                                           --all books every open one (after local.mjs was
                                           stopped mid-call)
  migrate [--wait <s>] [--force]           copy an Upstash ledger kept in the first layout
                                           (a hash per record) into the current one: switch
                                           the old one off, wait up to --wait s (300) for its
                                           calls in flight, copy, leave the new one off;
                                           --force copies again while the new one has served
                                           no call since the copy

ledger options:
  --ledger file|upstash    default: $RELAY_LEDGER, else file
  --file <path>            default: $RELAY_LEDGER_FILE, else .data/ledger.json here
  --prefix <prefix>        Redis key prefix (default: $RELAY_KEY_PREFIX, else "relay")

--freeze-at <pct> stops a code once the whole relay has spent pct% of the global cap
(e.g. 85 for development codes); judge codes keep the default, 100.
--share <pct> is the code's share of each model's per-minute budget and --in-flight <n>
the calls it may run at once (relay.config.json sets the defaults: 25% and 8).
A code is frozen when a call's reported usage cost more than the relay held for it.`;

const here = new URL(".", import.meta.url);
const built = new URL("./dist/src/index.js", here);
if (!existsSync(built)) {
  console.error("admin: run `npm run compile` first.");
  process.exit(1);
}
const relay = await import(built.href);
const config = relay.parseConfig(readFileSync(new URL("./relay.config.json", here), "utf8"));

/** Stops before the ledger is touched: nothing is open yet, so exiting at once is safe. */
function quit(message, code) {
  console.error(`admin: ${message}`);
  process.exit(code);
}

/** A refusal once the ledger may have been used; the command's catch prints it. */
class Stop extends Error {
  constructor(message, exitCode) {
    super(message);
    this.exitCode = exitCode;
  }
}

function fail(message, code = 1) {
  throw new Stop(message, code);
}

function parseArgs(argv) {
  const positional = [];
  const flags = {};
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--json" || arg === "--help" || arg === "-h" || arg === "--all" || arg === "--force" || arg === "--existing") {
      flags[arg.replace(/^-+/, "")] = true;
    } else if (arg.startsWith("--")) {
      const [name, inline] = arg.slice(2).split("=", 2);
      const value = inline ?? argv[i + 1];
      if (inline === undefined) i += 1;
      if (value === undefined || value.startsWith("--")) quit(`--${name} needs a value`, 2);
      flags[name] = value;
    } else {
      positional.push(arg);
    }
  }
  return { positional, flags };
}

const { positional, flags } = parseArgs(process.argv.slice(2));
const command = positional[0];
if (!command || flags.help || flags.h) {
  console.log(USAGE);
  process.exit(command ? 0 : 2);
}

const env = { ...process.env };
if (flags.ledger) env.RELAY_LEDGER = flags.ledger;
if (flags.file) env.RELAY_LEDGER_FILE = flags.file;
if (flags.prefix) env.RELAY_KEY_PREFIX = flags.prefix;
let ledger;
try {
  ledger = relay.ledgerFromEnv(env, { host: "local", defaultFile: fileURLToPath(new URL("./.data/ledger.json", here)) });
} catch (err) {
  quit(err.message, 1);
}

const usd = (nano) => `$${relay.formatUsd(nano)}`;
const now = Date.now();
const today = relay.utcDay(now);

function money(name, { zero = false } = {}) {
  if (flags[name] === undefined) return undefined;
  let nano;
  try {
    nano = relay.usdToNano(String(flags[name]), `--${name}`);
  } catch (err) {
    fail(err.message, 2);
  }
  if ((zero ? nano < 0 : nano <= 0) || nano > relay.MAX_CAP_NANO) {
    fail(`--${name} must be ${zero ? "from $0" : "above $0"} and at most $${relay.formatUsd(relay.MAX_CAP_NANO)}`, 2);
  }
  return nano;
}

function whole(name, max, what) {
  if (flags[name] === undefined) return undefined;
  const n = Number(flags[name]);
  if (!Number.isInteger(n) || n < 1 || n > max) fail(`--${name} must be ${what} from 1 to ${max}`, 2);
  return n;
}

const percent = (name) => whole(name, 100, "a whole percent");

async function findCode(ref) {
  if (!ref) fail("name a code by its id or label", 2);
  const codes = await ledger.listCodes(today);
  const byLabel = codes.filter((c) => c.label === ref);
  if (byLabel.length === 1) return byLabel[0];
  const byId = /^[0-9a-f]{6,64}$/.test(ref) ? codes.filter((c) => c.hash.startsWith(ref)) : [];
  if (byId.length === 1) return byId[0];
  if (byId.length > 1) fail(`"${ref}" matches ${byId.length} codes; give more of the id`);
  fail(`no code with id or label "${ref}"`);
}

/**
 * The access code `mint --existing` adds, from stdin: the code on the one "<label>=<code>"
 * line for this label (the private codes file keeps them that way), else the only code there.
 * Errors never repeat what stdin held.
 */
async function codeFromStdin(label) {
  if (process.stdin.isTTY) fail("mint --existing reads the code from stdin: add < <file holding the code>", 2);
  let text = "";
  for await (const chunk of process.stdin) {
    text += chunk;
    if (text.length > 1_000_000) fail("mint --existing: stdin is too large to be a codes file", 2);
  }
  const lines = text.split(/\r?\n/).map((line) => /^\s*([A-Za-z0-9][A-Za-z0-9_.-]{0,63})\s*[=:]\s*(\S+)\s*$/.exec(line));
  const named = lines.filter((m) => m && m[1] === label).map((m) => m[2]);
  if (named.length > 1) fail(`mint --existing: stdin has ${named.length} lines for "${label}"`, 2);
  if (named.length === 1) {
    if (!relay.CODE_PATTERN.test(named[0])) fail(`mint --existing: the "${label}" line does not hold an access code (rc_ and 32 characters)`, 2);
    return named[0];
  }
  const codes = [...new Set(text.match(/(?<![A-Za-z0-9_-])rc_[A-Za-z0-9_-]{32}(?![A-Za-z0-9_-])/g) ?? [])];
  if (codes.length === 0) fail("mint --existing: no access code (rc_ and 32 characters) on stdin", 2);
  if (codes.length > 1) fail(`mint --existing: stdin holds ${codes.length} codes and no "${label}=" line; give it one code`, 2);
  return codes[0];
}

function table(rows, columns) {
  const widths = columns.map((col) => Math.max(col.length, ...rows.map((row) => String(row[col] ?? "").length)));
  const line = (cells) => cells.map((cell, i) => String(cell ?? "").padEnd(widths[i])).join("  ").trimEnd();
  return [line(columns), ...rows.map((row) => line(columns.map((col) => row[col])))].join("\n");
}

function codeRow(c) {
  return {
    id: c.id,
    label: c.label,
    total: usd(c.capTotal),
    daily: usd(c.capDaily),
    freeze: `${c.freezePct}%`,
    share: `${c.sharePct ?? config.codeSharePct}%`,
    "max calls": c.maxInFlight ?? config.codeMaxInFlight,
    spent: usd(c.spent),
    today: usd(c.today.spent),
    "in flight": c.inFlight > 0 ? `${usd(c.reserved)} (${c.inFlight})` : usd(c.reserved),
    left: usd(Math.max(0, Math.min(c.capTotal - c.spent - c.reserved, c.capDaily - c.today.spent - c.today.reserved))),
    calls: c.requests,
    state: c.revoked ? "revoked" : c.frozen ? "frozen" : "active",
  };
}

const CODE_COLUMNS = ["id", "label", "total", "daily", "freeze", "share", "max calls", "spent", "today", "in flight", "left", "calls", "state"];

function settledText(e) {
  const notes = [
    e.why,
    e.reconcile ? "reconcile" : null,
    e.overshoot ? `over the hold by ${usd(e.overshoot)}` : null,
    e.reported ? `reported ${usd(e.reported)}` : null,
  ].filter(Boolean);
  return notes.length ? `${e.kind} (${notes.join(", ")})` : e.kind;
}

try {
  switch (command) {
    case "global": {
      const cap = money("cap");
      if (cap === undefined) fail("global needs --cap <usd>", 2);
      await ledger.setGlobalCap(cap);
      console.error(`admin: global cap set to ${usd(cap)}`);
      break;
    }
    case "opening-balance": {
      let amount = money("usd", { zero: true });
      let source = null;
      if (flags["from-file"] !== undefined) {
        if (amount !== undefined) fail("give --usd or --from-file, not both", 2);
        if (!existsSync(flags["from-file"])) fail(`no ledger file at ${flags["from-file"]}`, 2);
        source = new relay.FileLedger(flags["from-file"]);
        if (ledger.kind === "file" && path.resolve(source.file) === path.resolve(ledger.file)) fail("--from-file names the ledger being changed", 2);
        const old = await source.getGlobal();
        amount = old.spent + old.reserved;
        console.error(`admin: ${flags["from-file"]} has spent ${usd(old.spent)} with ${usd(old.reserved)} still held`);
      }
      if (amount === undefined) fail("opening-balance needs --usd <usd> or --from-file <ledger.json>", 2);
      const { previous } = await ledger.setOpeningBalance(amount);
      const g = await ledger.getGlobal();
      console.error(
        `admin: opening balance ${usd(amount)} (was ${usd(previous)}); this ledger now counts ${usd(g.spent)} spent ` +
          `against its ${usd(g.cap)} global cap`,
      );
      if (source) {
        // Its spend now counts here. Left on, the old ledger would keep a global cap of its own.
        await source.setDisabled(true);
        console.error(
          `admin: switched ${flags["from-file"]} off (kill switch on). To keep both ledgers instead, split the budget ` +
            `between their global caps and run: node admin.mjs enable --ledger file --file ${flags["from-file"]}`,
        );
      }
      break;
    }
    case "mint": {
      const label = flags.label;
      if (!label || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(label)) {
        fail("mint needs --label <name> (letters, digits, . _ -; at most 64)", 2);
      }
      const total = money("total");
      if (total === undefined) fail("mint needs --total <usd>", 2);
      const daily = money("daily") ?? total;
      const freezePct = percent("freeze-at") ?? 100;
      const sharePct = percent("share");
      const maxInFlight = whole("in-flight", 1000, "a number of calls");
      const existing = Boolean(flags.existing);
      const code = existing ? await codeFromStdin(label) : relay.newCode();
      const hash = await relay.hashCode(code);
      await ledger.addCode({
        hash,
        label,
        capTotal: total,
        capDaily: daily,
        freezePct,
        createdAt: now,
        ...(sharePct === undefined ? {} : { sharePct }),
        ...(maxInFlight === undefined ? {} : { maxInFlight }),
      });
      if (!existing) process.stdout.write(code + "\n");
      console.error(
        `admin: ${existing ? "added" : "minted"} ${relay.codeId(hash)} "${label}": total ${usd(total)}, daily ${usd(daily)}, freeze at ${freezePct}%, ` +
          `${sharePct ?? config.codeSharePct}% of each model's budget, ${maxInFlight ?? config.codeMaxInFlight} calls at once. ` +
          (existing
            ? "The code came from stdin and was not printed; this ledger keeps only its hash."
            : "The code above is shown once and is not stored; keep it now."),
      );
      break;
    }
    case "revoke": {
      const code = await findCode(positional[1]);
      await ledger.updateCode(code.hash, { revoked: true });
      console.error(`admin: revoked ${code.id} "${code.label}"`);
      break;
    }
    case "unfreeze": {
      const code = await findCode(positional[1]);
      if (!code.frozen) {
        console.error(`admin: ${code.id} "${code.label}" is not frozen`);
        break;
      }
      await ledger.updateCode(code.hash, { frozen: false });
      console.error(`admin: ${code.id} "${code.label}" works again`);
      break;
    }
    case "caps": {
      const code = await findCode(positional[1]);
      const patch = {};
      const total = money("total");
      const daily = money("daily");
      const freeze = percent("freeze-at");
      const share = percent("share");
      const inFlight = whole("in-flight", 1000, "a number of calls");
      if (total !== undefined) patch.capTotal = total;
      if (daily !== undefined) patch.capDaily = daily;
      if (freeze !== undefined) patch.freezePct = freeze;
      if (share !== undefined) patch.sharePct = share;
      if (inFlight !== undefined) patch.maxInFlight = inFlight;
      if (Object.keys(patch).length === 0) fail("caps needs --total, --daily, --freeze-at, --share or --in-flight", 2);
      await ledger.updateCode(code.hash, patch);
      const after = await ledger.getCode(code.hash);
      console.error(
        `admin: ${code.id} "${code.label}": total ${usd(after.capTotal)}, daily ${usd(after.capDaily)}, freeze at ${after.freezePct}%, ` +
          `${after.sharePct ?? config.codeSharePct}% of each model's budget, ${after.maxInFlight ?? config.codeMaxInFlight} calls at once`,
      );
      break;
    }
    case "list": {
      const codes = await ledger.listCodes(today);
      if (flags.json) {
        console.log(JSON.stringify(codes.map(({ hash, ...rest }) => ({ ...rest, hash_prefix: hash.slice(0, 12) })), null, 2));
      } else if (codes.length === 0) {
        console.log("no codes yet (node admin.mjs mint --label <name> --total <usd>)");
      } else {
        console.log(table(codes.map(codeRow), CODE_COLUMNS));
      }
      break;
    }
    case "report": {
      const dayCount = Number(flags.days ?? 7);
      const eventCount = Number(flags.events ?? 10);
      if (!Number.isInteger(dayCount) || dayCount < 1 || dayCount > 120) fail("--days must be 1 to 120", 2);
      if (!Number.isInteger(eventCount) || eventCount < 0 || eventCount > 1000) fail("--events must be 0 to 1000", 2);
      const days = Array.from({ length: dayCount }, (_, i) => relay.utcDay(now - i * 86_400_000));
      const report = await ledger.report({ days, events: eventCount, nowMs: now });
      if (flags.json) {
        const strip = report.codes.map(({ hash, ...rest }) => ({ ...rest, hash_prefix: hash.slice(0, 12) }));
        console.log(JSON.stringify({ ...report, codes: strip }, null, 2));
        break;
      }
      const g = report.global;
      const used = g.spent + g.reserved;
      const share = g.cap > 0 ? (100 * used) / g.cap : 0;
      const crossed = [50, 70, 85, 100].filter((level) => g.cap > 0 && share >= level);
      console.log(
        `global  cap ${usd(g.cap)}  spent ${usd(g.spent)}${g.opening ? ` (${usd(g.opening)} carried over)` : ""}  in flight ${usd(g.reserved)}` +
          `  left ${usd(Math.max(0, g.cap - used))}  (${share.toFixed(1)}% used)  kill switch ${g.disabled ? "ON" : "off"}`,
      );
      console.log(`alerts  ${crossed.length ? crossed.map((l) => `${l}%`).join(", ") + " of the global cap crossed" : "none"}`);
      console.log(`open    ${report.openReservations} reservation(s), ${report.expiredReservations} overdue (sweep books them in full)`);
      const frozen = report.codes.filter((c) => c.frozen && !c.revoked);
      if (frozen.length) {
        console.log(
          `frozen  ${frozen.map((c) => `${c.id} "${c.label}"`).join(", ")}: a call cost more than its hold; ` +
            "check the call, then node admin.mjs unfreeze <id|label>",
        );
      }
      const toReconcile = report.days.reduce((sum, d) => sum + d.models.reduce((s, m) => s + m.reconcile, 0), 0);
      if (toReconcile) {
        console.log(`check   ${toReconcile} call(s) booked in full after a Token Factory error (see "reconcile"); compare with the console`);
      }
      console.log("");
      console.log(report.codes.length ? table(report.codes.map(codeRow), CODE_COLUMNS) : "no codes");
      console.log("");
      console.log("usage by UTC day and model (out includes reasoning; ledger $ at list price, cached input at full price)");
      const usageRows = [];
      for (const { day, models } of report.days) {
        for (const m of models) {
          usageRows.push({
            day,
            model: m.model,
            calls: m.requests,
            in: m.promptTokens,
            out: m.completionTokens,
            reasoning: m.reasoningTokens,
            ledger: usd(m.cost),
            "from usage": m.usage,
            "full hold": m.full,
            released: m.released,
            reconcile: m.reconcile,
          });
        }
      }
      console.log(
        usageRows.length
          ? table(usageRows, ["day", "model", "calls", "in", "out", "reasoning", "ledger", "from usage", "full hold", "released", "reconcile"])
          : "  none in this period",
      );
      if (eventCount > 0) {
        console.log("");
        console.log("recent calls (newest first)");
        const rows = report.events.map((e) => ({
          time: new Date(e.t).toISOString().replace(/\.\d+Z$/, "Z"),
          code: e.code,
          model: e.model,
          status: e.status,
          settled: settledText(e),
          held: usd(e.reserved),
          cost: usd(e.actual),
          in: e.in,
          out: e.out,
          ms: e.ms ?? "",
          completion: e.completion ?? e.request_id ?? "",
        }));
        console.log(rows.length ? table(rows, ["time", "code", "model", "status", "settled", "held", "cost", "in", "out", "ms", "completion"]) : "  none");
      }
      break;
    }
    case "disable":
    case "enable": {
      await ledger.setDisabled(command === "disable");
      console.error(`admin: kill switch ${command === "disable" ? "ON: every /v1 call now gets 503" : "off"}`);
      break;
    }
    case "sweep": {
      const swept = await ledger.sweep(now, 1000, flags.all ? Number.MAX_SAFE_INTEGER : now);
      console.error(`admin: booked ${swept} ${flags.all ? "open" : "overdue"} reservation(s) at their full amount`);
      break;
    }
    case "migrate": {
      if (ledger.kind !== "upstash") fail("migrate works on the Upstash ledger (RELAY_LEDGER=upstash or --ledger upstash)", 2);
      const wait = Number(flags.wait ?? 300);
      if (!Number.isFinite(wait) || wait < 0 || wait > 3600) fail("--wait must be 0 to 3600 seconds", 2);
      const summary = await ledger.migrateFromV1({
        force: Boolean(flags.force),
        waitMs: wait * 1000,
        log: (line) => console.error(`admin: ${line}`),
      });
      const g = summary.global;
      console.error(
        `admin: copied ${summary.codes} code(s), ${summary.codeDays} daily figure(s), ${summary.usageDays} day(s) of usage, ` +
          `${summary.events} event(s) and ${summary.openReservations} open reservation(s): global cap ${usd(g.cap)}, ` +
          `spent ${usd(g.spent)}${g.opening ? ` (${usd(g.opening)} carried over)` : ""}, in flight ${usd(g.reserved)}. ` +
          "The new ledger is switched off; deploy the relay, check report, then run enable.",
      );
      break;
    }
    default:
      console.error(USAGE);
      process.exit(2);
  }
} catch (err) {
  console.error(`admin: ${err instanceof Error ? err.message : String(err)}`);
  // Not process.exit(): on Windows it can abort the process while the Upstash client is still
  // closing a connection (a libuv assertion), and the exit code is lost.
  process.exitCode = err instanceof Stop ? err.exitCode : 1;
}
