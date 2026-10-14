/**
 * Origin/Host enforcement for the local control server.
 *
 * Adapted from Hearth (github.com/echoo19/hearth, apps/editor/server/originGuard.ts,
 * MIT). A hostile web page can point a browser at http://127.0.0.1:<port>/api/... as
 * easily as this app can, and WebSocket ignores CORS, so requests whose Origin is
 * not this server's own loopback origin are refused. No Origin (a non-browser
 * client) is allowed; a non-loopback Host (DNS rebinding) is refused.
 */

const ALLOWED_HOSTNAMES = new Set(['localhost', '127.0.0.1', '::1', '[::1]']);

export type OriginCheckResult = { ok: true } | { ok: false; reason: 'origin' | 'host' | 'port' };

function isAllowedHostname(hostname: string): boolean {
  return ALLOWED_HOSTNAMES.has(hostname.toLowerCase());
}

function hostnameFromHostHeader(host: string): string {
  const trimmed = host.trim();
  if (trimmed.startsWith('[')) {
    const end = trimmed.indexOf(']');
    return end === -1 ? trimmed : trimmed.slice(0, end + 1);
  }
  const colon = trimmed.lastIndexOf(':');
  return colon === -1 ? trimmed : trimmed.slice(0, colon);
}

function portFromHostHeader(host: string): string | null {
  const trimmed = host.trim();
  const start = trimmed.startsWith('[') ? trimmed.indexOf(']') : -1;
  const colon = start === -1 ? trimmed.lastIndexOf(':') : trimmed.indexOf(':', start);
  if (colon === -1 || colon === trimmed.length - 1) return null;
  const port = trimmed.slice(colon + 1);
  return /^\d+$/.test(port) ? port : null;
}

function portFromOrigin(origin: URL): string {
  if (origin.port !== '') return origin.port;
  return origin.protocol === 'https:' ? '443' : '80';
}

function parseLoopbackOrigin(origin: string): URL | null {
  let parsed: URL;
  try {
    parsed = new URL(origin);
  } catch {
    return null;
  }
  return isAllowedHostname(parsed.hostname) ? parsed : null;
}

export function isRequestAllowed(headers: { origin?: string; host?: string }): OriginCheckResult {
  const { origin, host } = headers;
  const parsedOrigin = origin === undefined ? null : parseLoopbackOrigin(origin);
  if (origin !== undefined && !parsedOrigin) return { ok: false, reason: 'origin' };
  if (host !== undefined) {
    if (!isAllowedHostname(hostnameFromHostHeader(host))) return { ok: false, reason: 'host' };
    const hostPort = portFromHostHeader(host);
    if (parsedOrigin && hostPort !== null && portFromOrigin(parsedOrigin) !== hostPort) {
      return { ok: false, reason: 'port' };
    }
  }
  return { ok: true };
}
