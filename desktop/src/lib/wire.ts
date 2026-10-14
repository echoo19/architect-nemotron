/**
 * The page's side of the control server: one WebSocket, reconnecting, that
 * carries every conversation (see server/hub.ts for the frames).
 */

export type BridgeMessage = Record<string, unknown> & { type?: string; id?: number };

export interface BridgeError extends Error {
  code: string;
  detail?: string;
}

type Listener = (conv: string, msg: BridgeMessage) => void;
type ExitListener = (conv: string, code: number | null, reason: string) => void;
type OpenListener = (conv: string, replay: BridgeMessage[], fresh: boolean) => void;
type StatusListener = (online: boolean) => void;

interface Pending {
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
}

export class Wire {
  private socket: WebSocket | null = null;
  private nextId = 1;
  private pending = new Map<string, Pending>();
  private queue: string[] = [];
  private listeners = new Set<Listener>();
  private exitListeners = new Set<ExitListener>();
  private openListeners = new Set<OpenListener>();
  private statusListeners = new Set<StatusListener>();
  private frameListeners = new Set<(frame: Record<string, unknown>) => void>();
  private opened = new Map<string, string | undefined>(); // conv -> cwd, re-opened on reconnect
  private retry = 0;
  online = false;

  constructor(private readonly url: string) {}

  connect(): void {
    const socket = new WebSocket(this.url);
    this.socket = socket;
    socket.onopen = () => {
      this.retry = 0;
      this.setOnline(true);
      for (const [conv, cwd] of this.opened) socket.send(JSON.stringify({ t: 'open', conv, cwd }));
      for (const line of this.queue.splice(0)) socket.send(line);
    };
    socket.onmessage = (event) => this.onFrame(String(event.data));
    socket.onclose = () => {
      this.setOnline(false);
      for (const [key, waiter] of this.pending) {
        waiter.reject(Object.assign(new Error('offline'), { code: 'offline' }));
        this.pending.delete(key);
      }
      const delay = Math.min(5_000, 250 * 2 ** this.retry++);
      setTimeout(() => this.connect(), delay);
    };
  }

  private setOnline(value: boolean): void {
    this.online = value;
    for (const listener of this.statusListeners) listener(value);
  }

  private onFrame(text: string): void {
    let frame: Record<string, unknown>;
    try {
      frame = JSON.parse(text) as Record<string, unknown>;
    } catch {
      return;
    }
    for (const listener of this.frameListeners) listener(frame);
    const conv = String(frame.conv ?? '');
    if (frame.t === 'msg') {
      const msg = frame.msg as BridgeMessage;
      const key = `${conv}:${String(msg.id)}`;
      if (typeof msg.id === 'number' && this.pending.has(key) && ('result' in msg || 'error' in msg)) {
        const waiter = this.pending.get(key)!;
        this.pending.delete(key);
        if ('error' in msg) {
          const error = msg.error as { code?: string; message?: string };
          waiter.reject(Object.assign(new Error(error?.code ?? 'error'), {
            code: error?.code ?? 'error', detail: error?.message,
          }));
        } else {
          waiter.resolve(msg.result);
        }
        return;
      }
      for (const listener of this.listeners) listener(conv, msg);
    } else if (frame.t === 'opened') {
      for (const listener of this.openListeners) {
        listener(conv, (frame.replay as BridgeMessage[]) ?? [], frame.fresh === true);
      }
    } else if (frame.t === 'exit') {
      for (const [key, waiter] of this.pending) {
        if (key.startsWith(`${conv}:`)) {
          waiter.reject(Object.assign(new Error('stopped'), { code: 'stopped' }));
          this.pending.delete(key);
        }
      }
      for (const listener of this.exitListeners) {
        listener(conv, (frame.code as number | null) ?? null, String(frame.reason ?? ''));
      }
    }
  }

  private sendFrame(frame: Record<string, unknown>): void {
    const line = JSON.stringify(frame);
    if (this.socket && this.socket.readyState === WebSocket.OPEN) this.socket.send(line);
    else this.queue.push(line);
  }

  /** Any frame (terminals use their own pty-* frames). */
  onAnyFrame(listener: (frame: Record<string, unknown>) => void): () => void {
    this.frameListeners.add(listener);
    return () => this.frameListeners.delete(listener);
  }

  send(frame: Record<string, unknown>): void {
    this.sendFrame(frame);
  }

  open(conv: string, cwd?: string): void {
    this.opened.set(conv, cwd);
    this.sendFrame({ t: 'open', conv, cwd });
  }

  close(conv: string): void {
    this.opened.delete(conv);
    this.sendFrame({ t: 'close', conv });
  }

  request<T = unknown>(conv: string, method: string, params: Record<string, unknown> = {}): Promise<T> {
    const id = this.nextId++;
    return new Promise<T>((resolve, reject) => {
      this.pending.set(`${conv}:${id}`, { resolve: resolve as (v: unknown) => void, reject });
      this.sendFrame({ t: 'req', conv, id, method, params });
    });
  }

  /** Deliver a message as if a bridge had sent it (tests and development only). */
  inject(conv: string, msg: BridgeMessage): void {
    for (const listener of this.listeners) listener(conv, msg);
  }

  onMessage(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  onExit(listener: ExitListener): () => void {
    this.exitListeners.add(listener);
    return () => this.exitListeners.delete(listener);
  }

  onOpened(listener: OpenListener): () => void {
    this.openListeners.add(listener);
    return () => this.openListeners.delete(listener);
  }

  onStatus(listener: StatusListener): () => void {
    this.statusListeners.add(listener);
    return () => this.statusListeners.delete(listener);
  }
}

export function wsUrl(): string {
  const { protocol, host } = window.location;
  return `${protocol === 'https:' ? 'wss' : 'ws'}://${host}/api/ws`;
}
