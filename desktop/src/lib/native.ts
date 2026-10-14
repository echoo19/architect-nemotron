/** Native affordances from the Electron preload; undefined in a plain browser. */
export interface ArchitectNative {
  pickFolder(start?: string): Promise<string | null>;
  reveal(path: string): Promise<void>;
  openExternal(url: string): Promise<void>;
  openPath(path: string): Promise<void>;
  platform: string;
  onMenu?(cb: (command: string) => void): () => void;
  onFullScreen?(cb: (on: boolean) => void): () => void;
  version?(): Promise<{ app: string; electron: string; chrome: string; node: string }>;
  keepAwake?(on: boolean): Promise<void>;
  openWindow?(route: string, options?: { onTop?: boolean; width?: number; height?: number }): Promise<void>;
  setAlwaysOnTop?(on: boolean): Promise<boolean>;
  onBrowserOpen?(cb: (url: string) => void): () => void;
  toggleFullScreen?(): Promise<void>;
  zoom?(delta: number | null): number;
}

export function native(): ArchitectNative | undefined {
  return (window as unknown as { architectNative?: ArchitectNative }).architectNative;
}

export function openLink(url: string): void {
  const n = native();
  if (n) void n.openExternal(url);
  else window.open(url, '_blank', 'noopener');
}

/** What the system calls its file manager: Finder, Explorer, or just the folder. */
export function revealLabel(): string {
  const platform = native()?.platform ?? (navigator.platform.toLowerCase().startsWith('mac') ? 'darwin' : 'win32');
  return platform === 'darwin' ? 'Show in Finder' : platform === 'win32' ? 'Show in Explorer' : 'Show in folder';
}
