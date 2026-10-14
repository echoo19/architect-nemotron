import { spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, describe, expect, it } from "vitest";
import { FileLedger } from "../src/ledger/file.js";
import { ledgerContract } from "./ledger-contract.js";
import { cleanupDirs, fileLedger, mint, tempDir } from "./helpers.js";

afterAll(() => cleanupDirs());

ledgerContract("FileLedger", async () => ({ ledger: fileLedger("contract") }));

const child = fileURLToPath(new URL("./fixtures/race-child.mjs", import.meta.url));
const built = fileURLToPath(new URL("../dist/src/ledger/file.js", import.meta.url));

function runChild(file: string, hash: string, attempts: number, amount: number): Promise<number> {
  return new Promise((resolve, reject) => {
    const proc = spawn(process.execPath, [child, file, hash, String(attempts), String(amount)], { stdio: ["ignore", "pipe", "pipe"] });
    let out = "";
    let err = "";
    proc.stdout.on("data", (d) => (out += d));
    proc.stderr.on("data", (d) => (err += d));
    proc.on("exit", (code) => (code === 0 ? resolve(Number(out.trim())) : reject(new Error(`child failed: ${err}`))));
  });
}

describe("FileLedger across processes", () => {
  it("holds the cap when four processes reserve at once", async () => {
    expect(existsSync(built), "run `npm run compile` first (npm test does)").toBe(true);
    const file = path.join(tempDir("procs"), "ledger.json");
    const ledger = new FileLedger(file);
    await ledger.setGlobalCap(1_000_000_000);
    const { hash } = await mint(ledger, { total: "0.000025" }); // 25,000 n$: room for exactly 25 calls of 1,000
    const won = await Promise.all([0, 1, 2, 3].map(() => runChild(file, hash, 20, 1000)));
    expect(won.reduce((a, b) => a + b, 0)).toBe(25);
    expect(await ledger.getCode(hash)).toMatchObject({ reserved: 25_000, spent: 0 });
    const state = JSON.parse(readFileSync(file, "utf8")) as { reservations: Record<string, unknown> };
    expect(Object.keys(state.reservations)).toHaveLength(25);
    expect(existsSync(`${file}.lock`)).toBe(false);
  });

  it("never stores the code itself", async () => {
    const file = path.join(tempDir("hash-only"), "ledger.json");
    const ledger = new FileLedger(file);
    const { code, hash } = await mint(ledger, { label: "hash-only" });
    const text = readFileSync(file, "utf8");
    expect(text).toContain(hash);
    expect(text).not.toContain(code);
    expect(text).not.toContain(code.slice(3));
  });
});
