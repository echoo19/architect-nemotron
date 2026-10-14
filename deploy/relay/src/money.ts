/**
 * Money is integer nano-dollars (1e-9 USD) everywhere: in the handler, in the file
 * ledger and in Redis. Prices of at most 3 decimals per 1M tokens are whole nano-dollars
 * per token (0.06 $/1M = 60 n$/token), so every reservation and settlement is an exact
 * integer, in JavaScript and in Redis Lua alike.
 */

export const NANO_PER_USD = 1_000_000_000;

/** Caps are refused above this, which keeps every Lua product well inside 2^53. */
export const MAX_CAP_NANO = 10_000 * NANO_PER_USD;

function decimalToScaled(text: string, places: number, what: string): number {
  const match = /^(\d{1,12})(?:\.(\d+))?$/.exec(text.trim());
  if (!match) throw new Error(`${what}: "${text}" is not a plain decimal number`);
  const whole = match[1] ?? "0";
  const frac = match[2] ?? "";
  if (frac.length > places && /[1-9]/.test(frac.slice(places))) {
    throw new Error(`${what}: "${text}" has more than ${places} decimals`);
  }
  const scaled = Number(whole) * 10 ** places + Number((frac.slice(0, places) || "0").padEnd(places, "0"));
  if (!Number.isSafeInteger(scaled)) throw new Error(`${what}: "${text}" is too large`);
  return scaled;
}

/** "3", "0.25", "26.00" (dollars) to nano-dollars. */
export function usdToNano(text: string, what = "amount"): number {
  return decimalToScaled(text, 9, what);
}

/** A price in dollars per 1M tokens ("0.06") to whole nano-dollars per token (60). */
export function perMillionToNanoPerToken(text: string, what = "price"): number {
  return decimalToScaled(text, 3, what);
}

/** "0.06" ($ per 1M tokens) to "0.00000006" ($ per token), exactly, for /v1/models. */
export function perMillionToPerTokenString(text: string): string {
  const nanoPerToken = perMillionToNanoPerToken(text);
  return formatUsd(nanoPerToken, { minDecimals: 1 });
}

/** Nano-dollars to a dollar string with no rounding: 42840 -> "0.00004284". */
export function formatUsd(nano: number, opts: { minDecimals?: number } = {}): string {
  const minDecimals = opts.minDecimals ?? 2;
  const negative = nano < 0;
  const abs = Math.abs(Math.trunc(nano));
  const whole = Math.floor(abs / NANO_PER_USD);
  let frac = String(abs % NANO_PER_USD).padStart(9, "0").replace(/0+$/, "");
  if (frac.length < minDecimals) frac = frac.padEnd(minDecimals, "0");
  return `${negative ? "-" : ""}${whole}${frac ? "." + frac : ""}`;
}

/** Nano-dollars as a JSON number of dollars (for error bodies and reports). */
export function nanoToUsdNumber(nano: number): number {
  return Number(formatUsd(nano, { minDecimals: 0 }));
}
