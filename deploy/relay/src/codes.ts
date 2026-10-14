/**
 * Access codes. A code is `rc_` plus 32 base64url characters (192 random bits). Only its
 * SHA-256 is ever stored; the first 12 hex characters of that hash are the code's id in
 * listings and logs. The code itself is shown once, when it is minted.
 */

export const CODE_PREFIX = "rc_";
export const CODE_PATTERN = /^rc_[A-Za-z0-9_-]{32}$/;
const encoder = new TextEncoder();

function toHex(bytes: Uint8Array): string {
  let out = "";
  for (const byte of bytes) out += byte.toString(16).padStart(2, "0");
  return out;
}

function toBase64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** A new random access code (Web Crypto, so it runs on any host). */
export function newCode(): string {
  const bytes = new Uint8Array(24);
  crypto.getRandomValues(bytes);
  return CODE_PREFIX + toBase64Url(bytes);
}

export async function hashCode(code: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", encoder.encode(code));
  return toHex(new Uint8Array(digest));
}

export function codeId(hash: string): string {
  return hash.slice(0, 12);
}

/** The bearer token of an Authorization header, or null. */
export function bearerToken(header: string | null): string | null {
  const match = /^Bearer[ \t]+([^\s]+)[ \t]*$/i.exec(header ?? "");
  return match ? (match[1] ?? null) : null;
}

/** A fresh reservation id (not secret; it names one call in the ledger and the logs). */
export function newReservationId(): string {
  const bytes = new Uint8Array(12);
  crypto.getRandomValues(bytes);
  return "r_" + toHex(bytes);
}
