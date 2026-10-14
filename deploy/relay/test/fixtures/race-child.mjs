// One racing process for test/ledger-file.test.ts: tries `attempts` reservations of
// `amount` against the shared ledger file and prints how many it won.
import { FileLedger } from "../../dist/src/ledger/file.js";

const [file, hash, attempts, amount] = process.argv.slice(2);
const ledger = new FileLedger(file);
const window = Math.floor(Date.now() / 60_000);
let won = 0;
const tries = Array.from({ length: Number(attempts) }, async (_, i) => {
  const nowMs = Date.now();
  const outcome = await ledger.reserve({
    id: `r_${process.pid}_${i}`,
    hash,
    model: "nvidia/Nemotron-3_5-Lightning",
    day: new Date(nowMs).toISOString().slice(0, 10),
    window,
    elapsedMs: 0,
    amount: Number(amount),
    rateTokens: 1,
    rpm: 100_000,
    tpm: 100_000_000,
    nowMs,
    deadlineMs: nowMs + 600_000,
    defaultSharePct: 100,
    defaultMaxInFlight: 1000,
  });
  if (outcome.ok) won += 1;
  else if (outcome.reason !== "cap_total") throw new Error(`unexpected refusal ${outcome.reason}`);
});
await Promise.all(tries);
process.stdout.write(String(won));
