/** Readable JSON errors in the OpenAI shape: {"error": {"message", "type", "code", ...}}. */

export interface ErrorBody {
  message: string;
  type: string;
  code?: string;
  [extra: string]: unknown;
}

export function errorResponse(
  status: number,
  body: ErrorBody,
  headers: Record<string, string> = {},
): Response {
  return new Response(JSON.stringify({ error: body }), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store", ...headers },
  });
}

export function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store", ...headers },
  });
}
