// Adapted from Hearth (github.com/echoo19/hearth, MIT). apps/editor/src/shortcuts.ts
/**
 * Keyboard shortcuts, and the one place that knows which modifier key this
 * computer uses.
 *
 * Hearth ships to macOS and to Windows, and the two disagree about the primary
 * modifier: Command on a Mac, Control everywhere else. Every shortcut in the
 * app is declared here as "mod" and resolved per machine, so no component ever
 * hardcodes one platform's key and quietly does nothing on the other.
 *
 * The label matters as much as the binding. A Windows user reading "⌘K" learns
 * nothing, so `shortcutLabel` renders the same declaration as "⌘K" or "Ctrl K"
 * depending on where it is being read.
 */

/**
 * Whether this is a Mac.
 *
 * `navigator.userAgentData` is the supported way to ask and is what Chromium
 * answers first; `navigator.platform` is deprecated but still populated, and is
 * the fallback for the older runtime. The user agent string is the last resort.
 * Anything unrecognised is treated as not-a-Mac, which is the safer default:
 * Control is the modifier on Windows and Linux both.
 */
export function isMac(): boolean {
  if (typeof navigator === 'undefined') return false;
  const data = (navigator as { userAgentData?: { platform?: string } }).userAgentData;
  if (data?.platform) return data.platform.toLowerCase().startsWith('mac');
  if (navigator.platform) return navigator.platform.toLowerCase().startsWith('mac');
  return /mac/i.test(navigator.userAgent ?? '');
}

/** One shortcut, written once, resolved per platform. */
export interface Shortcut {
  /** The physical key, compared case-insensitively against `event.key`. */
  key: string;
  /** Command on a Mac, Control on Windows and Linux. */
  mod?: boolean;
  shift?: boolean;
  /** Literal Alt/Option. Rare, and never the only modifier on a destructive action. */
  alt?: boolean;
  /** Literal Control on every platform (Ctrl+Tab switches conversations on a Mac too). */
  ctrl?: boolean;
  /** Never fires in a text field, even with the mod key (undo there undoes typing). */
  outsideText?: boolean;
}

/**
 * Does this keystroke match the declaration?
 *
 * The mod check is deliberately exclusive: on a Mac a shortcut declared with
 * `mod` requires Command and rejects Control, so Ctrl+K stays available to the
 * terminal, where it means something else entirely.
 */
export function matchesShortcut(event: KeyboardEvent, shortcut: Shortcut, mac = isMac()): boolean {
  if (event.key.toLowerCase() !== shortcut.key.toLowerCase()) return false;
  if (shortcut.ctrl) {
    return event.ctrlKey && !event.metaKey && (shortcut.shift === true) === event.shiftKey && (shortcut.alt === true) === event.altKey;
  }
  const wantMod = shortcut.mod === true;
  const gotMod = mac ? event.metaKey : event.ctrlKey;
  const otherMod = mac ? event.ctrlKey : event.metaKey;
  if (gotMod !== wantMod || otherMod) return false;
  if ((shortcut.shift === true) !== event.shiftKey) return false;
  if ((shortcut.alt === true) !== event.altKey) return false;
  return true;
}

/**
 * How this shortcut should be written on this machine.
 *
 * Mac convention packs the symbols together with no separator, because the
 * glyphs already read as keys. Windows spells them out and needs the gap.
 */
export function shortcutLabel(shortcut: Shortcut, mac = isMac()): string {
  const key = shortcut.key.length === 1 ? shortcut.key.toUpperCase() : shortcut.key;
  const parts: string[] = [];
  if (shortcut.ctrl) parts.push(mac ? '⌃' : 'Ctrl');
  if (shortcut.mod) parts.push(mac ? '⌘' : 'Ctrl');
  if (shortcut.alt) parts.push(mac ? '⌥' : 'Alt');
  if (shortcut.shift) parts.push(mac ? '⇧' : 'Shift');
  parts.push(key);
  return mac ? parts.join('') : parts.join('+');
}

/**
 * Is the person typing right now?
 *
 * A global shortcut must never eat a character someone is putting into a
 * message. Text fields and anything contenteditable are off limits, with one
 * exception: a shortcut that carries the mod key is not a character, so it
 * still fires. That is what lets the search shortcut work from inside the
 * composer, which is exactly where someone is most likely to press it.
 */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  const tag = target.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
}

/** The shortcuts Architect binds, named so Settings > Shortcuts can list them. */
export const SHORTCUTS = {
  palette: { key: 'k', mod: true } as Shortcut,
  newChat: { key: 'n', mod: true } as Shortcut,
  newForge: { key: 'n', mod: true, alt: true } as Shortcut,
  settings: { key: ',', mod: true } as Shortcut,
  // Codex's Open folder; Claude Code's Ctrl+O verbose view stays literal Ctrl on a Mac
  // and moves to Ctrl+Alt+O on Windows, where Ctrl+O is Open folder.
  openFolder: { key: 'o', mod: true } as Shortcut,
  viewMode: (isMac() ? { key: 'o', ctrl: true } : { key: 'o', mod: true, alt: true }) as Shortcut,
  agents: { key: 'l', mod: true, shift: true } as Shortcut,
  closeConv: { key: 'w', mod: true } as Shortcut,
  nextConv: { key: ']', mod: true, shift: true } as Shortcut,
  prevConv: { key: '[', mod: true, shift: true } as Shortcut,
  sidebar: { key: 'b', mod: true } as Shortcut,
  closePane: { key: '\\', mod: true } as Shortcut,
  nextTab: { key: 'Tab', ctrl: true } as Shortcut,
  prevTab: { key: 'Tab', ctrl: true, shift: true } as Shortcut,
  archive: { key: 'a', mod: true, shift: true } as Shortcut,
  unread: { key: 'u', mod: true, shift: true } as Shortcut,
  clearUnread: { key: 'Escape', shift: true } as Shortcut,
  attention: { key: 'a', mod: true, alt: true } as Shortcut,
  pin: { key: 'p', mod: true, alt: true } as Shortcut,
  rename: { key: 'r', mod: true, alt: true } as Shortcut,
  searchChats: { key: 'f', mod: true, shift: true } as Shortcut,
  copyCwd: { key: 'c', mod: true, shift: true } as Shortcut,
  panelView: { key: 'j', mod: true } as Shortcut,
  modelMenu: { key: 'i', mod: true, shift: true } as Shortcut,
  permissionMenu: { key: 'm', mod: true, shift: true } as Shortcut,
  changes: { key: 'd', mod: true, shift: true } as Shortcut,
  shortcuts: { key: '/', mod: true } as Shortcut,
  quickOpen: { key: 'p', mod: true } as Shortcut,
  // Literal Ctrl on a Mac too: Cmd+` switches windows there.
  terminal: { key: '`', ctrl: true } as Shortcut,
  fileTree: { key: 'e', mod: true, shift: true } as Shortcut,
  zoomIn: { key: '=', mod: true } as Shortcut,
  zoomOut: { key: '-', mod: true } as Shortcut,
  zoomReset: { key: '0', mod: true } as Shortcut,
  fullScreen: { key: 'F11' } as Shortcut,
  back: { key: '[', mod: true } as Shortcut,
  undo: { key: 'z', mod: true, outsideText: true } as Shortcut,
  redo: { key: 'z', mod: true, shift: true, outsideText: true } as Shortcut,
  redoWin: { key: 'y', mod: true, outsideText: true } as Shortcut,
  activity: { key: 'u', mod: true, alt: true } as Shortcut,
  sideChat: { key: ';', mod: true } as Shortcut,
  browser: { key: 'b', mod: true, shift: true } as Shortcut,
  newWindow: { key: 'n', mod: true, shift: true } as Shortcut,
  layout: { key: 'b', mod: true, alt: true } as Shortcut,
  newChatAlt: { key: 'o', mod: true, shift: true } as Shortcut,
  sideChatAlt: { key: 's', mod: true, alt: true } as Shortcut,
  reviewAlt: { key: 'g', ctrl: true, shift: true } as Shortcut,
  forward: { key: ']', mod: true } as Shortcut,
} as const;

export const SHORTCUT_NAMES: Record<keyof typeof SHORTCUTS, string> = {
  palette: 'Search and commands',
  newChat: 'New conversation',
  newForge: 'Build a new agent',
  settings: 'Settings',
  openFolder: 'New conversation in another folder',
  viewMode: 'Switch between normal and verbose view',
  agents: 'Your agents',
  closeConv: 'Close conversation',
  nextConv: 'Next conversation',
  prevConv: 'Previous conversation',
  sidebar: 'Show or hide the sidebar',
  closePane: 'Close the panes',
  nextTab: 'Next conversation',
  prevTab: 'Previous conversation',
  archive: 'Archive the conversation',
  unread: 'Mark as unread',
  clearUnread: 'Clear all unread',
  attention: 'Go to the conversation that needs you',
  pin: 'Pin or unpin',
  rename: 'Rename',
  searchChats: 'Search conversations',
  copyCwd: 'Copy the folder path',
  panelView: 'Show or hide the panes (in Build mode: open or close the report)',
  modelMenu: 'Choose a model',
  permissionMenu: 'Choose what Architect may do',
  changes: 'Changes',
  shortcuts: 'Keyboard shortcuts',
  quickOpen: 'Open a file',
  terminal: 'Terminal',
  fileTree: 'Folder tree',
  zoomIn: 'Make text bigger',
  zoomOut: 'Make text smaller',
  zoomReset: 'Actual size',
  fullScreen: 'Full screen',
  back: 'Back',
  undo: 'Undo the last action',
  redo: 'Redo',
  redoWin: 'Redo',
  activity: 'Activity',
  sideChat: 'Side question',
  browser: 'Browser',
  newWindow: 'New window',
  layout: 'Panes beside, full window, or hidden',
  newChatAlt: 'New conversation',
  sideChatAlt: 'Side question',
  reviewAlt: 'Changes',
  forward: 'Forward',
};

/** Second keys for the same action (Codex's and Claude Code's both work); listed with the first. */
export const SHORTCUT_ALIASES: Partial<Record<keyof typeof SHORTCUTS, keyof typeof SHORTCUTS>> = {
  newChatAlt: 'newChat',
  sideChatAlt: 'sideChat',
  reviewAlt: 'changes',
  redoWin: 'redo',
  nextTab: 'nextConv',
  prevTab: 'prevConv',
};

/** The shipped bindings, kept for Reset. */
export const DEFAULT_SHORTCUTS: Record<keyof typeof SHORTCUTS, Shortcut> = Object.fromEntries(
  Object.entries(SHORTCUTS).map(([name, s]) => [name, { ...s }]),
) as Record<keyof typeof SHORTCUTS, Shortcut>;

/**
 * Your own bindings (Settings > Keyboard shortcuts), applied over the shipped ones.
 * The table is updated in place, so every label and menu shows the keys you chose.
 */
export function applyBindings(overrides: Partial<Record<string, Shortcut>> = {}): void {
  for (const name of Object.keys(SHORTCUTS) as Array<keyof typeof SHORTCUTS>) {
    const target = SHORTCUTS[name] as Shortcut;
    for (const field of Object.keys(target) as Array<keyof Shortcut>) delete target[field];
    Object.assign(target, overrides[name] ?? DEFAULT_SHORTCUTS[name]);
  }
}

/** A keystroke as a binding; null for a lone modifier or a key that cannot be bound. */
export function shortcutFromEvent(event: KeyboardEvent | React.KeyboardEvent, mac = isMac()): Shortcut | null {
  const key = event.key;
  if (['Control', 'Shift', 'Alt', 'Meta', 'CapsLock', 'Dead', 'Unidentified'].includes(key)) return null;
  const shortcut: Shortcut = { key: key.length === 1 ? key.toLowerCase() : key };
  if (mac ? event.metaKey : event.ctrlKey) shortcut.mod = true;
  if (mac && event.ctrlKey) shortcut.ctrl = true;
  if (event.shiftKey) shortcut.shift = true;
  if (event.altKey) shortcut.alt = true;
  return shortcut;
}

export function sameShortcut(a: Shortcut, b: Shortcut): boolean {
  return a.key.toLowerCase() === b.key.toLowerCase() && !!a.mod === !!b.mod && !!a.ctrl === !!b.ctrl && !!a.shift === !!b.shift && !!a.alt === !!b.alt;
}

/** True while Settings records a new binding: shortcuts stay quiet so the keys can be captured. */
export const recordingKeys = { on: false };
