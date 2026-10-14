/**
 * Runs the Web-standard handler inside node:http (the local relay). The client's
 * disconnect aborts the Request's signal and cancels the response stream, which is what
 * stops the upstream call and settles it.
 */

import type { IncomingMessage, ServerResponse } from "node:http";
import { Readable } from "node:stream";

type Handle = (request: Request) => Promise<Response>;

export function toNodeHandler(handle: Handle, baseUrl: string) {
  return async (req: IncomingMessage, res: ServerResponse): Promise<void> => {
    const abort = new AbortController();
    let reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
    res.on("close", () => {
      if (res.writableFinished) return;
      abort.abort(new Error("client_closed"));
      reader?.cancel(new Error("client_closed")).catch(() => undefined);
    });

    const headers = new Headers();
    for (const [key, value] of Object.entries(req.headers)) {
      if (value === undefined) continue;
      try {
        if (Array.isArray(value)) for (const item of value) headers.append(key, item);
        else headers.set(key, value);
      } catch {
        // a header undici will not carry; the relay never needs it
      }
    }
    const method = (req.method ?? "GET").toUpperCase();
    const init: RequestInit & { duplex?: "half" } = { method, headers, signal: abort.signal };
    if (method !== "GET" && method !== "HEAD") {
      init.body = Readable.toWeb(req) as unknown as ReadableStream<Uint8Array>;
      init.duplex = "half";
    }

    let response: Response;
    try {
      response = await handle(new Request(new URL(req.url ?? "/", baseUrl), init));
    } catch {
      if (!res.headersSent) res.writeHead(500, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: { message: "The relay hit an internal error.", type: "relay_error" } }));
      return;
    }
    const out: Record<string, string> = {};
    response.headers.forEach((value, key) => {
      out[key] = value;
    });
    res.writeHead(response.status, out);
    if (!response.body || method === "HEAD") {
      res.end();
      return;
    }
    reader = response.body.getReader();
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        if (res.destroyed) break;
        if (!res.write(value)) {
          await new Promise<void>((resolve) => {
            const go = () => {
              res.off("drain", go);
              res.off("close", go);
              resolve();
            };
            res.on("drain", go);
            res.on("close", go);
          });
        }
      }
      res.end();
    } catch {
      res.destroy();
    }
  };
}
