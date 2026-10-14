/**
 * Reads a chat-completions event stream as it passes through: it keeps the last usage
 * object and the completion id, and forwards whole events unchanged. The one event it may
 * drop is the usage-only chunk (empty choices) when the client never asked for it; the
 * relay always asks upstream for usage, because settlement depends on it.
 */

const BOUNDARY = /\r?\n\r?\n/;
/** Past this much undelimited text the tap stops inspecting and only forwards. */
const MAX_PENDING_CHARS = 8 * 1024 * 1024;

export class SseTap {
  usage: unknown = null;
  completionId: string | null = null;
  sawDone = false;
  /** The upstream sent an error event inside a 200 stream. */
  sawError = false;
  private pending = "";
  private blind = false;
  private readonly decoder = new TextDecoder();
  private readonly encoder = new TextEncoder();

  constructor(private readonly dropUsageOnlyChunk: boolean) {}

  /** Upstream bytes in; the bytes to forward now (whole events) out. */
  push(chunk: Uint8Array): Uint8Array | null {
    const text = this.decoder.decode(chunk, { stream: true });
    if (this.blind) return text ? this.encoder.encode(text) : null;
    this.pending += text;
    return this.drain(false);
  }

  /** End of the upstream stream: whatever is left. */
  end(): Uint8Array | null {
    const text = this.decoder.decode();
    if (this.blind) return text ? this.encoder.encode(text) : null;
    this.pending += text;
    return this.drain(true);
  }

  private drain(final: boolean): Uint8Array | null {
    let out = "";
    for (;;) {
      const match = BOUNDARY.exec(this.pending);
      if (!match) break;
      const end = match.index + match[0].length;
      const event = this.pending.slice(0, end);
      this.pending = this.pending.slice(end);
      if (this.keep(event)) out += event;
    }
    if (final && this.pending) {
      if (this.keep(this.pending)) out += this.pending;
      this.pending = "";
    } else if (this.pending.length > MAX_PENDING_CHARS) {
      out += this.pending;
      this.pending = "";
      this.blind = true;
    }
    return out ? this.encoder.encode(out) : null;
  }

  /** Inspects one event; false means drop it. */
  private keep(event: string): boolean {
    const data = event
      .split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).replace(/^ /, ""))
      .join("\n");
    if (!data) return true;
    if (data.trim() === "[DONE]") {
      this.sawDone = true;
      return true;
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(data);
    } catch {
      return true;
    }
    if (typeof parsed !== "object" || parsed === null) return true;
    const chunk = parsed as Record<string, unknown>;
    if (chunk.error !== undefined && chunk.error !== null) this.sawError = true;
    if (this.completionId === null && typeof chunk.id === "string") this.completionId = chunk.id;
    if (typeof chunk.usage === "object" && chunk.usage !== null) {
      this.usage = chunk.usage;
      if (this.dropUsageOnlyChunk && Array.isArray(chunk.choices) && chunk.choices.length === 0) return false;
    }
    return true;
  }
}

/** An SSE event carrying an OpenAI-style error, for a stream the relay has to end early. */
export function sseErrorEvent(message: string, type: string): Uint8Array {
  return new TextEncoder().encode(`data: ${JSON.stringify({ error: { message, type } })}\n\n`);
}
